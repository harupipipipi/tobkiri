//! Raw assertions retained from the former Wasm port_proxy integration suite.
//! This is a cfg(test) child of `session`, so a received UI-created stream can
//! transfer once from its actual registry into the test's cleanup custody.
//! No provider frame, approval, bridge or authority proof is manufactured.

use super::*;
use crate::test_support::RuntimeFixture;
use async_trait::async_trait;
use std::sync::atomic::{AtomicUsize, Ordering};
use tobkiri_canon::Variant;
use tobkiri_gate::{ApprovalStore, Mediator, MediatorChain, Verdict, WasmEntryKind};
use tobkiri_store::{Approvals, DataRoot};

const GENERATION: u64 = 7;
const SINK_FAILURE: &str = "fixture outcome sink failure";

fn input_shape() -> ShapeRef {
    ShapeRef::parse("local.fixture.input@1.0").unwrap()
}

async fn attachment(session: &Session) -> UiAttachment {
    session.bind_ui_generation(GENERATION).unwrap();
    let mut attachments = session.ui_attachments().unwrap();
    assert_eq!(attachments.len(), 1);
    attachments.remove(0)
}

async fn raw_stream(session: &Session, ui: &UiAttachment) -> CoreStream {
    let buffer = session
        .ui_invoke_received(
            &ui.session,
            &ui.host_session,
            GENERATION,
            &ui.receiver,
            input_shape(),
            OpName::parse("stream").unwrap(),
            &CanonValue::Obj(BTreeMap::new()),
        )
        .await
        .unwrap();
    let opened = tobkiri_canon::parse(buffer.as_bytes(), Variant::StrictV1).unwrap();
    assert_eq!(
        opened.get("kind").and_then(CanonValue::as_str),
        Some("stream")
    );
    let id = u64::try_from(opened.get("handle").and_then(CanonValue::as_i64).unwrap()).unwrap();
    let entry = session.streams.lock().unwrap().remove(&id).unwrap();
    assert_eq!(entry.ui, ui.session);
    // Transfer the live source itself; do not reconstruct its context or mint
    // another source lease. Registry removal prevents duplicate Session close.
    let source = entry.state.lock().await.take().unwrap();
    assert!(source.pending_buffer.is_none());
    source.stream
}

struct OutcomeFault {
    connection: rusqlite::Connection,
    active: bool,
}

impl OutcomeFault {
    fn install(fixture: &RuntimeFixture) -> Self {
        let path = fixture
            .root
            .path()
            .join(DataRoot::profile_relative(&fixture.profile.id, "audit.sqlite").unwrap());
        let connection = rusqlite::Connection::open_with_flags(
            path,
            rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE,
        )
        .unwrap();
        connection
            .execute_batch(
                "CREATE TRIGGER port_proxy_outcome_fault BEFORE INSERT ON audit
                 WHEN instr(CAST(NEW.body AS TEXT), '\"kind\":\"outcome\"') > 0
                 BEGIN SELECT RAISE(FAIL, 'fixture outcome sink failure'); END;",
            )
            .unwrap();
        Self {
            connection,
            active: true,
        }
    }

    fn clear(&mut self) {
        self.connection
            .execute_batch("DROP TRIGGER port_proxy_outcome_fault;")
            .unwrap();
        self.active = false;
    }
}

impl Drop for OutcomeFault {
    fn drop(&mut self) {
        if self.active {
            let _ = self
                .connection
                .execute_batch("DROP TRIGGER IF EXISTS port_proxy_outcome_fault;");
        }
    }
}

fn cancelled_outcomes(fixture: &RuntimeFixture) -> usize {
    fixture
        .audit_events()
        .iter()
        .filter(|event| {
            event.kind == "outcome"
                && event
                    .payload
                    .get("outcome")
                    .and_then(|outcome| outcome.get("status"))
                    .and_then(CanonValue::as_str)
                    == Some("cancelled")
        })
        .count()
}

fn pending_cancelled(fixture: &RuntimeFixture) -> Vec<tobkiri_gate::GrantId> {
    let approvals = Approvals::open(&fixture.root, &fixture.profile.id).unwrap();
    approvals
        .pending_terminal(&fixture.profile.id, 100)
        .unwrap()
        .into_iter()
        .filter(|terminal| terminal.outcome == Outcome::Cancelled)
        .map(|terminal| terminal.reservation.id)
        .collect()
}

#[tokio::test]
async fn explicit_close_and_cancel_then_pull_surface_audit_sink_failure() {
    let fixture = RuntimeFixture::new(&["fixture"], true).await;
    let session = fixture.session().await;
    let ui = attachment(&session).await;
    let mut first = raw_stream(&session, &ui).await;
    let mut fault = OutcomeFault::install(&fixture);
    let error = first.close().await.unwrap_err();
    assert!(error.message.contains(SINK_FAILURE));
    assert_eq!(first.close().await.unwrap_err().message, error.message);
    let first_pending = pending_cancelled(&fixture);
    assert_eq!(first_pending.len(), 1);
    fault.clear();
    first.close().await.unwrap();
    assert!(first.pull(1).await.unwrap().array().unwrap().is_empty());
    assert_eq!(cancelled_outcomes(&fixture), 1);

    let mut second = raw_stream(&session, &ui).await;
    let mut fault = OutcomeFault::install(&fixture);
    second.cancel();
    let error = second.pull(1).await.unwrap_err();
    assert!(error.message.contains(SINK_FAILURE));
    assert_eq!(second.close().await.unwrap_err().message, error.message);
    let second_pending = pending_cancelled(&fixture);
    assert_eq!(second_pending.len(), 1);
    // Two distinct real source terminals reached the failing physical sink;
    // a repeated cached error from one source cannot satisfy this assertion.
    let failed_outcomes = first_pending
        .into_iter()
        .chain(second_pending)
        .collect::<BTreeSet<_>>();
    assert!(failed_outcomes.len() >= 2);
    fault.clear();
    second.close().await.unwrap();
    assert_eq!(cancelled_outcomes(&fixture), 2);
    session.close().await.unwrap();
}

struct SourceLimits {
    pack: PackId,
    hash: PackHash,
    current: tobkiri_gate::PermissionSet,
    output_bytes: u64,
    wall_ms: u64,
    stream_reviews: Arc<AtomicUsize>,
    other_reviews: Arc<AtomicUsize>,
}

#[async_trait]
impl Mediator for SourceLimits {
    async fn review(&self, prompt: &tobkiri_gate::ApprovalPrompt) -> Verdict {
        if matches!(
            prompt.effect,
            EffectRequest::WasmEntry {
                entry: WasmEntryKind::StreamOpen { .. },
                ..
            }
        ) {
            self.stream_reviews.fetch_add(1, Ordering::SeqCst);
            assert_eq!(prompt.principal.pack(), Some((&self.pack, &self.hash)));
            // Start from this verified source's four actual policy layers.
            // P20 changes only output_bytes; its requested five-second wall
            // cap must not widen RuntimeFixture's current one-second cap.
            let mut limits = self.current.clone();
            limits.wasm.output_bytes = limits.wasm.output_bytes.min(self.output_bytes);
            limits.wasm.wall_ms = limits.wasm.wall_ms.min(self.wall_ms);
            Verdict::Constrain(limits)
        } else {
            self.other_reviews.fetch_add(1, Ordering::SeqCst);
            Verdict::Pass
        }
    }
}

fn source_mediator(
    fixture: &RuntimeFixture,
    output_bytes: u64,
    wall_ms: u64,
) -> (MediatorChain, Arc<AtomicUsize>, Arc<AtomicUsize>) {
    assert_eq!(fixture.profile.packs.len(), 1);
    let pin = &fixture.profile.packs[0];
    let verified =
        tobkiri_manifest::verify_tpk(&std::fs::read(&pin.source).unwrap(), &BTreeMap::new())
            .unwrap();
    assert_eq!(verified.manifest.id, pin.id);
    assert_eq!(verified.hash, pin.sha256);
    let approvals = Approvals::open(&fixture.root, &fixture.profile.id).unwrap();
    let approved = approvals
        .permissions(&fixture.profile.id, &pin.id, &pin.sha256)
        .unwrap()
        .unwrap();
    let current = verified
        .manifest
        .permissions
        .intersect(&fixture.profile.ceiling)
        .intersect(&fixture.profile.pack_limits[&pin.id])
        .intersect(&approved);
    let stream_reviews = Arc::new(AtomicUsize::new(0));
    let other_reviews = Arc::new(AtomicUsize::new(0));
    let chain = MediatorChain::new(vec![Arc::new(SourceLimits {
        pack: pin.id.clone(),
        hash: pin.sha256.clone(),
        current,
        output_bytes,
        wall_ms,
        stream_reviews: stream_reviews.clone(),
        other_reviews: other_reviews.clone(),
    })]);
    (chain, stream_reviews, other_reviews)
}

#[tokio::test]
async fn acknowledged_cleanup_retries_the_same_immutable_cancel_winner_after_deadline() {
    let fixture = RuntimeFixture::new(&["fixture"], true).await;
    let (mediators, _, _) = source_mediator(&fixture, 1024 * 1024, 500);
    let session = fixture.mediated_session(mediators).await;
    let ui = attachment(&session).await;
    let mut warm = raw_stream(&session, &ui).await;
    warm.close().await.unwrap();
    let before = fixture.audit_events().len();
    let mut stream = raw_stream(&session, &ui).await;
    let mut fault = OutcomeFault::install(&fixture);
    // Cleanup really completes before the source's independent deadline;
    // only its physical terminal INSERT fails.
    assert!(
        stream
            .close()
            .await
            .unwrap_err()
            .message
            .contains(SINK_FAILURE)
    );
    tokio::time::sleep(Duration::from_millis(750)).await;
    fault.clear();
    stream.close().await.unwrap();
    stream.close().await.unwrap();
    let events = fixture.audit_events();
    let terminals = events[before..]
        .iter()
        .filter(|event| {
            event.kind == "outcome"
                && event
                    .payload
                    .get("outcome")
                    .and_then(|outcome| outcome.get("status"))
                    .and_then(CanonValue::as_str)
                    == Some("cancelled")
        })
        .collect::<Vec<_>>();
    assert_eq!(
        terminals.len(),
        1,
        "sink retry must not replay cleanup or replace the winner"
    );
    assert_eq!(
        terminals[0].payload.get("disposition"),
        Some(&CanonValue::Str("completed".into()))
    );
    assert!(!events[before..].iter().any(|event| {
        event
            .payload
            .get("outcome")
            .and_then(|outcome| outcome.get("code"))
            .and_then(CanonValue::as_str)
            == Some("deadline-failure-unknown")
    }));
    session.close().await.unwrap();
}

#[tokio::test]
async fn stream_uses_own_entry_mediator_budget_across_multiple_pulls() {
    let fixture = RuntimeFixture::new(&["fixture"], true).await;
    assert_eq!(fixture.profile.ceiling.wasm.output_bytes, 1024 * 1024);
    assert_eq!(
        fixture
            .profile
            .pack_limits
            .values()
            .next()
            .unwrap()
            .wasm
            .output_bytes,
        1024 * 1024
    );
    let (mediators, stream_reviews, other_reviews) = source_mediator(&fixture, 16, 5000);
    let session = fixture.mediated_session(mediators).await;
    let ui = attachment(&session).await;
    let mut stream = raw_stream(&session, &ui).await;
    assert_eq!(stream_reviews.load(Ordering::SeqCst), 1);
    assert!(other_reviews.load(Ordering::SeqCst) > 0);
    // The real fixture emits JSON nulls: 11 bytes then 6 bytes. Each batch
    // fits independently; only the source-open mediator's cumulative 16-byte
    // limit can reject the third item while Pack/Profile remain at 1 MiB.
    assert_eq!(stream.pull(2).await.unwrap().array().unwrap().len(), 2);
    assert_eq!(stream.pull(1).await.unwrap_err().code, "limit-exceeded");
    assert_eq!(
        stream_reviews.load(Ordering::SeqCst),
        1,
        "pulls must not readmit the source"
    );
    drop(stream);
    let buffer = session
        .ui_invoke_received(
            &ui.session,
            &ui.host_session,
            GENERATION,
            &ui.receiver,
            input_shape(),
            OpName::parse("read").unwrap(),
            &CanonValue::Obj(BTreeMap::new()),
        )
        .await
        .unwrap();
    assert_eq!(buffer.as_bytes(), br#"{"kind":"value","value":42}"#);
    session.close().await.unwrap();
}
