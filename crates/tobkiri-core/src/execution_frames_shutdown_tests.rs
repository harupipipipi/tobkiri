//! Real component regression for one shutdown retiring a shared Store while
//! another source's coherent cause observation is temporarily unavailable.
use super::*;
use crate::{Session, UiAttachment, session::UiCallResult, test_support::RuntimeFixture};

fn source_frames(factory: &ExecutionFactory) -> BTreeMap<usize, Arc<CoreProviderFrame>> {
    factory
        .provider
        .auth
        .live_provider_frames
        .lock()
        .unwrap()
        .iter()
        .filter_map(|(key, frame)| {
            let frame = frame.upgrade()?;
            matches!(&frame.state.origin, ReachedOrigin::HandleSource(_)).then_some((*key, frame))
        })
        .collect()
}

async fn open(session: &Session, ui: &UiAttachment) -> u64 {
    let envelope = session
        .ui_invoke_received(
            &ui.session,
            &ui.host_session,
            7,
            &ui.receiver,
            ShapeRef::parse("local.fixture.input@1.0").unwrap(),
            tobkiri_canon::ids::OpName::parse("stream").unwrap(),
            &crate::CanonValue::Obj(BTreeMap::new()),
        )
        .await
        .unwrap();
    match serde_json::from_slice::<UiCallResult>(envelope.as_bytes()).unwrap() {
        UiCallResult::Stream { handle } => handle,
        _ => panic!("the actual fixture component must return a stream"),
    }
}

#[tokio::test]
async fn shutdown_keeps_its_cause_when_a_busy_sibling_outlives_shared_store_retirement() {
    let mut permissions = crate::test_support::permissions(&["fixture"]);
    // Match the existing live-handle capacity fixture; the original two-second
    // cleanup grace is unchanged. No clock or lifecycle result is fabricated.
    permissions.wasm.wall_ms = 30_000;
    permissions.loops.max_wall_ms = 30_000;
    let fixture = RuntimeFixture::with_permissions(&["fixture"], true, permissions).await;
    let session = fixture.session().await;
    session.bind_ui_generation(7).unwrap();
    let ui = session
        .ui_attachments()
        .unwrap()
        .into_iter()
        .next()
        .unwrap();
    let factory = session.operation_intent_factory_for_test();
    let first = open(&session, &ui).await;
    let before = source_frames(&factory);
    let physical = session.stream_physical_completion_for_test(first).await;
    let second = open(&session, &ui).await;
    assert_ne!(first, second);
    let mut added = source_frames(&factory)
        .into_iter()
        .filter(|(key, _)| !before.contains_key(key));
    let (_, blocked) = added.next().expect("second actual source frame");
    assert!(added.next().is_none());
    let notification = blocked
        .cleanup
        .lock()
        .unwrap()
        .as_ref()
        .unwrap()
        .upgrade()
        .unwrap();

    let (entered, ready) = std::sync::mpsc::channel();
    let (release, released) = std::sync::mpsc::channel();
    let holder = std::thread::spawn(move || {
        let guard = blocked.state.state.lock().unwrap();
        entered.send(()).unwrap();
        // Dropping the sender on unwind also releases this real guard.
        let _ = released.recv();
        drop(guard);
    });
    ready.recv().unwrap();
    // The actual Core state guard prevents a coherent selection callback.
    // Its accepted intent must still acknowledge notification without
    // pretending that the original physical receipt has completed.
    notification
        .record_close(StreamCloseReason::Shutdown)
        .unwrap();
    {
        let pending = physical.wait();
        tokio::pin!(pending);
        assert!(
            std::future::Future::poll(
                pending.as_mut(),
                &mut std::task::Context::from_waker(std::task::Waker::noop()),
            )
            .is_pending()
        );
    }
    let observe_and_release = async move {
        // The same original operation receipt confirms actual Store/native
        // completion. This observer creates no cleanup work or terminal.
        let completed = physical.wait().await;
        release.send(()).unwrap();
        holder.join().unwrap();
        completed
    };
    let (closed, physical) = tokio::join!(session.close(), observe_and_release);
    physical.unwrap();
    closed.unwrap();
}
