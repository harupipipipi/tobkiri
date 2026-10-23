use super::*;
use crate::test_support::{RuntimeFixture, within_session};
use async_trait::async_trait;
use std::sync::atomic::{AtomicUsize, Ordering};
use tobkiri_gate::ApprovalStore;
use tobkiri_store::Approvals;

const GENERATION: u64 = 7;
async fn setup() -> (RuntimeFixture, Session, UiAttachment) {
    setup_with_permissions(crate::test_support::permissions(&["fixture"])).await
}
async fn setup_with_permissions(
    permissions: tobkiri_gate::PermissionSet,
) -> (RuntimeFixture, Session, UiAttachment) {
    setup_with_permissions_and_timing(permissions, None).await
}
async fn setup_with_permissions_and_timing(
    permissions: tobkiri_gate::PermissionSet,
    timing: Option<Arc<crate::test_support::AuditTiming>>,
) -> (RuntimeFixture, Session, UiAttachment) {
    let mut fixture = RuntimeFixture::with_permissions(&["fixture"], true, permissions).await;
    if let Some(timing) = timing {
        fixture.core.set_audit_timing(timing);
    }
    let session = fixture.session().await;
    let primary = session.ui.lock().unwrap().keys().next().unwrap().clone();
    let pack = session.compiled.profile.startup.ui.as_ref().unwrap();
    let hash = session
        .compiled
        .profile
        .packs
        .iter()
        .find(|pin| &pin.id == pack)
        .unwrap()
        .sha256
        .clone();
    for id in ["ui_second", "ui_good_cleanup"] {
        session
            .register_ui(
                UiSessionId::parse(id).unwrap(),
                UiSessionId::parse(&format!("host_{id}")).unwrap(),
                pack.clone(),
                hash.clone(),
                BTreeSet::from([input_shape()]),
            )
            .unwrap();
    }
    session.bind_ui_generation(GENERATION).unwrap();
    let attachment = session
        .ui_attachments()
        .unwrap()
        .into_iter()
        .find(|attachment| attachment.session == primary)
        .unwrap();
    (fixture, session, attachment)
}
fn input_shape() -> ShapeRef {
    ShapeRef::parse("local.fixture.input@1.0").unwrap()
}
fn empty_args() -> CanonValue {
    CanonValue::Obj(BTreeMap::new())
}
async fn invoke(
    session: &Session,
    attachment: &UiAttachment,
    shape: ShapeRef,
    op: &str,
    args: &CanonValue,
) -> Result<CoreBuffer, CoreError> {
    session
        .ui_invoke_received(
            &attachment.session,
            &attachment.host_session,
            GENERATION,
            &attachment.receiver,
            shape,
            OpName::parse(op).unwrap(),
            args,
        )
        .await
}
fn extra_attachment(session: &Session, source: &UiAttachment, id: &str) -> UiSessionId {
    let token = UiSessionId::parse(id).unwrap();
    let attachment = session
        .ui_attachments()
        .unwrap()
        .into_iter()
        .find(|attachment| attachment.session == token)
        .unwrap();
    assert_eq!(attachment.pack, source.pack);
    assert_eq!(attachment.hash, source.hash);
    assert_eq!(attachment.generation, Some(GENERATION));
    token
}

#[tokio::test]
async fn forged_approved_payload_cannot_bypass_ui_gate() {
    let (fixture, session, attachment) = setup().await;
    Approvals::open(&fixture.root, &fixture.profile.id)
        .unwrap()
        .set_permissions(
            &fixture.profile.id,
            &attachment.pack,
            &attachment.hash,
            &tobkiri_gate::PermissionSet::default(),
        )
        .unwrap();
    let forged = CanonValue::Obj(BTreeMap::from([(
        "approved".into(),
        CanonValue::Bool(true),
    )]));
    assert!(
        invoke(&session, &attachment, input_shape(), "read", &forged)
            .await
            .is_err()
    );
    assert!(session.streams.lock().unwrap().is_empty());
    session.close().await.unwrap();
}

#[tokio::test]
async fn ui_principal_comes_from_core_token_and_undeclared_ports_are_rejected() {
    let (_fixture, session, attachment) = setup().await;
    let output = invoke(&session, &attachment, input_shape(), "read", &empty_args())
        .await
        .unwrap();
    assert_eq!(output.as_bytes(), br#"{"kind":"value","value":42}"#);
    assert!(
        invoke(
            &session,
            &attachment,
            ShapeRef::parse("local.fixture.other@1.0").unwrap(),
            "read",
            &empty_args()
        )
        .await
        .is_err()
    );
    assert!(
        session
            .ui_invoke_received(
                &attachment.session,
                &attachment.host_session,
                GENERATION,
                &Digest::hash(b"forged receiver"),
                input_shape(),
                OpName::parse("read").unwrap(),
                &empty_args()
            )
            .await
            .is_err()
    );
    let handle = open_stream(
        &session,
        &attachment.session,
        "stream",
        TokioCancellationToken::new(),
    )
    .await;
    let entry = session
        .streams
        .lock()
        .unwrap()
        .get(&handle)
        .unwrap()
        .clone();
    assert_eq!(
        entry.cx.principal(),
        &Principal::PackUi {
            pack: attachment.pack.clone(),
            hash: attachment.hash.clone(),
            ui: attachment.session.clone()
        }
    );
    session.close().await.unwrap();
}

#[tokio::test]
async fn profile_epoch_change_invalidates_old_ui_session() {
    let (_fixture, session, attachment) = setup().await;
    session
        .gate
        .revoke_epoch(&session.compiled.profile.id)
        .unwrap();
    assert!(
        invoke(&session, &attachment, input_shape(), "read", &empty_args())
            .await
            .is_err()
    );
    session.close().await.unwrap();
}

// Pure source fixtures exercise Session cleanup independently of guest ABI
// behavior. Both their retained values and final encoded batches have a finite
// allocation owner. Normal stream opening below uses the real Wasm component.
struct Events {
    remaining: u32,
    cancelled: Arc<AtomicUsize>,
    waiting: bool,
    fail_close: bool,
}
#[async_trait]
impl tobkiri_flow::StreamSource for Events {
    async fn pull(&mut self, max: u32) -> Result<CoreValue, StepError> {
        if self.waiting {
            std::future::pending::<()>().await;
        }
        let count = self.remaining.min(max);
        self.remaining -= count;
        Ok(crate::test_support::batch(
            (0..count).map(|_| crate::test_support::value(&CanonValue::Null)),
        ))
    }
    async fn pull_final(&mut self, max: u32) -> Result<CoreBuffer, StepError> {
        self.pull(max)
            .await?
            .encode_guest(&crate::test_support::value_budget())
            .map_err(|_| StepError::new("limit-exceeded", "fixture encoding budget"))
    }
    fn cancel(&mut self) {
        self.remaining = 0;
        self.cancelled.fetch_add(1, Ordering::SeqCst);
    }
    async fn close(&mut self) -> Result<(), StepError> {
        self.cancel();
        if self.fail_close {
            Err(StepError::new("internal", "fixture cleanup failed"))
        } else {
            Ok(())
        }
    }
}
fn install_source(
    session: &Session,
    ui: &UiSessionId,
    source: Events,
    external_cancel: TokioCancellationToken,
) -> u64 {
    let handle = session.next_stream.fetch_add(1, Ordering::SeqCst);
    let close_control = session
        .ui_operation_close_control(ui, None, external_cancel.clone())
        .unwrap();
    let principal = session.ui.lock().unwrap();
    let principal = principal.get(ui).unwrap();
    let cx = CallCx::try_root_with_control(
        session.compiled.profile.id.clone(),
        Principal::PackUi {
            pack: principal.pack.clone(),
            hash: principal.hash.clone(),
            ui: ui.clone(),
        },
        close_control.request.clone(),
        close_control.token.clone(),
        deadline_after(1000).unwrap(),
        session.compiled.profile.ceiling.wasm.clone(),
        session.gate.control().clone(),
    )
    .unwrap()
    .with_security_epoch(session.epoch)
    .with_revision(session.compiled.plan_digest.clone());
    let control_reservation = Arc::new(
        session
            .gate
            .control()
            .reserve(
                ControlKind::Registration,
                super::ui::ui_stream_entry_control_bytes(ui).unwrap(),
            )
            .unwrap(),
    );
    session.streams.lock().unwrap().insert(
        handle,
        UiStreamEntryLease::new(
            Arc::new(UiStreamEntry {
                ui: ui.clone(),
                cx,
                external_cancel,
                close_control,
                source_control: None,
                state: tokio::sync::Mutex::new(Some(UiStreamState {
                    stream: CoreStream::new(source),
                    output_bytes: 0,
                    pending_buffer: None,
                })),
            }),
            control_reservation,
        ),
    );
    handle
}
async fn open_stream(
    session: &Session,
    ui: &UiSessionId,
    op: &str,
    cancel: TokioCancellationToken,
) -> u64 {
    let attachment = session
        .ui_attachments()
        .unwrap()
        .into_iter()
        .find(|attachment| &attachment.session == ui)
        .unwrap();
    let envelope = session
        .ui_invoke_received_with_cancel(
            ui,
            &attachment.host_session,
            GENERATION,
            &attachment.receiver,
            input_shape(),
            OpName::parse(op).unwrap(),
            &empty_args(),
            cancel,
        )
        .await
        .unwrap();
    match serde_json::from_slice::<UiCallResult>(envelope.as_bytes()).unwrap() {
        UiCallResult::Stream { handle } => handle,
        _ => panic!("expected stream"),
    }
}

#[tokio::test]
async fn actual_stream_retains_its_original_parent_metadata_after_native_retirement() {
    let (_fixture, session, attachment) = setup().await;
    open_stream(
        &session,
        &attachment.session,
        "stream",
        TokioCancellationToken::new(),
    )
    .await;
    let check_retired_metadata = session
        .execution_factory
        .as_ref()
        .unwrap()
        .retained_stream_parent_check();
    session.close().await.unwrap();
    check_retired_metadata();
}

fn batch_len(buffer: CoreBuffer) -> usize {
    match tobkiri_canon::parse(buffer.as_bytes(), tobkiri_canon::Variant::StrictV1).unwrap() {
        CanonValue::Arr(values) => values.len(),
        _ => panic!("expected array batch"),
    }
}

#[tokio::test]
async fn ui_stream_scope_bounds_and_eof_are_core_enforced() {
    let (_fixture, session, attachment) = setup().await;
    let second = extra_attachment(&session, &attachment, "ui_second");
    let handle = open_stream(
        &session,
        &attachment.session,
        "stream",
        TokioCancellationToken::new(),
    )
    .await;
    assert!(
        session
            .ui_stream_pull_buffer(&second, handle, 1)
            .await
            .is_err()
    );
    assert!(session.ui_stream_cancel(&second, handle).await.is_err());
    assert!(
        session
            .ui_stream_pull_buffer(&attachment.session, handle, 0)
            .await
            .is_err()
    );
    assert!(
        session
            .ui_stream_pull_buffer(&attachment.session, handle, 33)
            .await
            .is_err()
    );
    assert_eq!(
        batch_len(
            session
                .ui_stream_pull_buffer(&attachment.session, handle, 2)
                .await
                .unwrap()
        ),
        2
    );
    assert_eq!(
        batch_len(
            session
                .ui_stream_pull_buffer(&attachment.session, handle, 2)
                .await
                .unwrap()
        ),
        1
    );
    assert_eq!(
        batch_len(
            session
                .ui_stream_pull_buffer(&attachment.session, handle, 2)
                .await
                .unwrap()
        ),
        0
    );
    assert!(
        session
            .ui_stream_pull_buffer(&attachment.session, handle, 1)
            .await
            .is_err()
    );
    assert!(session.streams.lock().unwrap().is_empty());
    session.close().await.unwrap();
}

#[tokio::test]
async fn ui_stream_stale_epoch_and_external_cancellation_release_handles() {
    let (_fixture, session, attachment) = setup().await;
    let counter = Arc::new(AtomicUsize::new(0));
    let cancel = TokioCancellationToken::new();
    let handle = install_source(
        &session,
        &attachment.session,
        Events {
            remaining: 3,
            cancelled: counter.clone(),
            waiting: false,
            fail_close: false,
        },
        cancel.clone(),
    );
    cancel.cancel();
    assert!(
        session
            .ui_stream_pull_buffer(&attachment.session, handle, 1)
            .await
            .is_err()
    );
    assert!(session.streams.lock().unwrap().is_empty());
    let handle = install_source(
        &session,
        &attachment.session,
        Events {
            remaining: 3,
            cancelled: counter.clone(),
            waiting: false,
            fail_close: false,
        },
        TokioCancellationToken::new(),
    );
    session
        .gate
        .revoke_epoch(&session.compiled.profile.id)
        .unwrap();
    assert!(
        session
            .ui_stream_pull_buffer(&attachment.session, handle, 1)
            .await
            .is_err()
    );
    assert!(session.streams.lock().unwrap().is_empty());
    assert!(counter.load(Ordering::SeqCst) >= 2);
    session.close().await.unwrap();
}

#[tokio::test]
async fn frame_detach_interrupts_pending_pull_and_closes_assets() {
    let (_fixture, session, attachment) = setup().await;
    let counter = Arc::new(AtomicUsize::new(0));
    assert_eq!(
        session
            .verified_ui_asset(&attachment.session, "ui/main.html")
            .unwrap(),
        b"verified"
    );
    assert!(
        session
            .verified_ui_asset(&attachment.session, "../config.json")
            .is_err()
    );
    assert!(
        session
            .verified_ui_asset(&UiSessionId::parse("ui_forged").unwrap(), "ui/main.html")
            .is_err()
    );
    let handle = install_source(
        &session,
        &attachment.session,
        Events {
            remaining: 3,
            cancelled: counter.clone(),
            waiting: true,
            fail_close: false,
        },
        TokioCancellationToken::new(),
    );
    let pull = session.ui_stream_pull_buffer(&attachment.session, handle, 1);
    let close = async {
        tokio::task::yield_now().await;
        session.detach_ui(&attachment.session).await.unwrap();
    };
    let (result, ()) = tokio::time::timeout(Duration::from_millis(250), async {
        tokio::join!(pull, close)
    })
    .await
    .unwrap();
    assert!(result.is_err());
    assert!(session.streams.lock().unwrap().is_empty());
    assert!(
        session
            .verified_ui_asset(&attachment.session, "ui/main.html")
            .is_err()
    );
    assert!(counter.load(Ordering::SeqCst) > 0);
    session.close().await.unwrap();
}

#[tokio::test]
async fn ui_handle_capacity_and_session_close_are_bounded() {
    let mut permissions = crate::test_support::permissions(&["fixture"]);
    // Keep all 64 handles live while exercising the capacity boundary. The
    // independent original-deadline tests use their short explicit budgets.
    permissions.wasm.wall_ms = 30_000;
    permissions.loops.max_wall_ms = 30_000;
    let timing = Arc::new(crate::test_support::AuditTiming::default());
    let (_fixture, session, attachment) =
        setup_with_permissions_and_timing(permissions, Some(timing.clone())).await;
    for _ in 0..UI_STREAMS_PER_FRAME {
        open_stream(
            &session,
            &attachment.session,
            "stream",
            TokioCancellationToken::new(),
        )
        .await;
    }
    assert!(
        invoke(
            &session,
            &attachment,
            input_shape(),
            "stream",
            &empty_args()
        )
        .await
        .is_err()
    );
    assert_eq!(session.streams.lock().unwrap().len(), UI_STREAMS_PER_FRAME);
    timing.observe_close(session.close()).await.unwrap();
    assert!(session.streams.lock().unwrap().is_empty());
    assert!(
        invoke(&session, &attachment, input_shape(), "read", &empty_args())
            .await
            .is_err()
    );
}

#[tokio::test]
async fn scalar_invoke_still_succeeds_at_full_stream_capacity() {
    let mut permissions = crate::test_support::permissions(&["fixture"]);
    // Scalar operations reserve no stream slot and must keep working while
    // every per-frame handle is occupied by a live stream.
    permissions.wasm.wall_ms = 30_000;
    permissions.loops.max_wall_ms = 30_000;
    let timing = Arc::new(crate::test_support::AuditTiming::default());
    let (_fixture, session, attachment) =
        setup_with_permissions_and_timing(permissions, Some(timing.clone())).await;
    for _ in 0..UI_STREAMS_PER_FRAME {
        open_stream(
            &session,
            &attachment.session,
            "stream",
            TokioCancellationToken::new(),
        )
        .await;
    }
    assert_eq!(session.streams.lock().unwrap().len(), UI_STREAMS_PER_FRAME);
    assert_eq!(
        invoke(&session, &attachment, input_shape(), "read", &empty_args())
            .await
            .unwrap()
            .as_bytes(),
        br#"{"kind":"value","value":42}"#
    );
    assert!(
        invoke(
            &session,
            &attachment,
            input_shape(),
            "stream",
            &empty_args()
        )
        .await
        .is_err()
    );
    assert_eq!(session.streams.lock().unwrap().len(), UI_STREAMS_PER_FRAME);
    timing.observe_close(session.close()).await.unwrap();
}

#[tokio::test]
async fn completed_scalar_provider_entries_reuse_generation_operation_index() {
    let mut permissions = crate::test_support::permissions(&["fixture"]);
    permissions.wasm.wall_ms = 30_000;
    permissions.loops.max_wall_ms = 30_000;
    let (_fixture, session, attachment) = setup_with_permissions(permissions).await;
    let expected = br#"{"kind":"value","value":42}"#;
    let warmup = invoke(&session, &attachment, input_shape(), "read", &empty_args())
        .await
        .unwrap();
    assert_eq!(warmup.as_bytes(), expected);
    drop(warmup);
    let registrations = session
        .gate
        .control()
        .usage()
        .count(tobkiri_gate::ControlKind::Registration);

    // Each call enters the actual component in the same live Store. Completed
    // scalar entries must not turn its bounded weak index into a history quota.
    for _ in 0..257 {
        let output = invoke(&session, &attachment, input_shape(), "read", &empty_args())
            .await
            .unwrap();
        assert_eq!(output.as_bytes(), expected);
        drop(output);
        assert_eq!(
            session
                .gate
                .control()
                .usage()
                .count(tobkiri_gate::ControlKind::Registration),
            registrations,
        );
    }
    assert!(session.streams.lock().unwrap().is_empty());
    session.close().await.unwrap();
}

#[tokio::test]
async fn ui_request_cancellation_keeps_original_context_and_outcome() {
    let (fixture, session, attachment) = setup().await;
    let counter = Arc::new(AtomicUsize::new(0));
    let cancel = TokioCancellationToken::new();
    let handle = install_source(
        &session,
        &attachment.session,
        Events {
            remaining: 3,
            cancelled: counter.clone(),
            waiting: true,
            fail_close: false,
        },
        cancel.clone(),
    );
    let entry = session
        .streams
        .lock()
        .unwrap()
        .get(&handle)
        .unwrap()
        .clone();
    let request = entry.cx.request().clone();
    let call = session.ui_stream_pull_buffer(&attachment.session, handle, 1);
    let stop = async {
        tokio::task::yield_now().await;
        cancel.cancel();
    };
    let (result, ()) = tokio::time::timeout(Duration::from_millis(250), async {
        tokio::join!(call, stop)
    })
    .await
    .unwrap();
    assert!(matches!(result, Err(CoreError::Step(StepError { code, .. })) if code == "cancelled"));
    assert_eq!(entry.cx.request(), &request);
    assert!(entry.cx.cancel().is_cancelled());
    assert!(counter.load(Ordering::SeqCst) > 0);
    assert!(session.streams.lock().unwrap().is_empty());
    // Also exercise an actual yielding component call. Its single received
    // request retains its native source and durable cancellation outcome.
    let cancel = TokioCancellationToken::new();
    let args = empty_args();
    let call = session.ui_invoke_received_with_cancel(
        &attachment.session,
        &attachment.host_session,
        GENERATION,
        &attachment.receiver,
        input_shape(),
        OpName::parse("wait").unwrap(),
        &args,
        cancel.clone(),
    );
    let marker = CanonValue::Str(
        tobkiri_gate::EffectRequest::Clock
            .digest()
            .unwrap()
            .to_string(),
    );
    let stop = async {
        // The fixture's wait body calls this gated Clock import before its
        // loop. Wait for that actual guest effect rather than racing argument
        // preparation or treating a pre-admission cancellation as execution.
        let started = tokio::time::timeout(Duration::from_secs(1), async {
            loop {
                if fixture.audit_events().iter().any(|event| event.kind == "dispatch" && matches!(&event.principal, Principal::Pack { pack, hash } if pack == &attachment.pack && hash == &attachment.hash) && matches!(&event.payload, CanonValue::Obj(fields) if fields.get("effect_digest") == Some(&marker))) { break; }
                tokio::task::yield_now().await;
            }
        }).await.is_ok();
        cancel.cancel();
        started
    };
    let (result, started) = within_session(&session, "started guest UI cancellation", async {
        tokio::join!(call, stop)
    })
    .await;
    assert!(
        started,
        "guest wait body never reached its gated Clock effect"
    );
    assert!(result.is_err());
    let cancelled = tobkiri_canon::parse(
        &serde_json::to_vec(&tobkiri_gate::Outcome::Cancelled).unwrap(),
        tobkiri_canon::Variant::StrictV1,
    )
    .unwrap();
    assert!(fixture.audit_events().iter().any(|event| event.kind == "outcome" && matches!(&event.principal, Principal::PackUi { ui, .. } if ui == &attachment.session) && matches!(&event.payload, CanonValue::Obj(fields) if fields.get("outcome") == Some(&cancelled))));
    session.close().await.unwrap();
}

#[tokio::test]
async fn host_facade_futures_can_run_on_threaded_runtime() {
    fn is_send<T: Send>(_: T) {}
    let (_fixture, session, attachment) = setup().await;
    is_send(session.ui_invoke_received(
        &attachment.session,
        &attachment.host_session,
        GENERATION,
        &attachment.receiver,
        input_shape(),
        OpName::parse("read").unwrap(),
        &empty_args(),
    ));
    is_send(session.ui_stream_pull_buffer(&attachment.session, 1, 1));
    is_send(session.detach_ui(&attachment.session));
    is_send(session.close());
    session.close().await.unwrap();
}

#[tokio::test]
async fn production_session_runner_uses_one_engine_and_frozen_revision() {
    let (fixture, session, _attachment) = setup().await;
    assert!(Arc::ptr_eq(&session.engine, &session.clone().engine));
    let entry = session.input_entries()[0].clone();
    let source = InputSourceId::parse(entry.source_pack().as_str()).unwrap();
    let mut stale = within_session(
        &session,
        "stale-revision physical Input read",
        session.read_input(
            source.clone(),
            entry.exact_input_view().clone(),
            entry.operation().clone(),
        ),
    )
    .await
    .unwrap();
    stale.context = CallCx::try_root_with_control(
        fixture.profile.id.clone(),
        stale.context.principal().clone(),
        RequestId::parse("req_stale").unwrap(),
        session
            .shutdown
            .child_with_control(session.gate.control())
            .unwrap(),
        deadline_after(1000).unwrap(),
        fixture.profile.ceiling.wasm.clone(),
        session.gate.control().clone(),
    )
    .unwrap()
    .with_security_epoch(session.epoch)
    .with_revision(Digest::hash(b"wrong-revision"));
    assert!(
        within_session(
            &session,
            "stale-revision NativeInput rejection",
            session.submit_native_input(stale)
        )
        .await
        .is_err()
    );
    let input = within_session(
        &session,
        "fresh physical Input read",
        session.read_input(
            source.clone(),
            entry.exact_input_view().clone(),
            entry.operation().clone(),
        ),
    )
    .await
    .unwrap();
    let mut run = within_session(
        &session,
        "fresh NativeInput admission",
        session.submit_native_input(input),
    )
    .await
    .unwrap();
    let (result, failures) = within_session(
        &session,
        "fresh NativeInput progress and completion",
        async {
            let mut failures = Vec::new();
            while let Some(event) = run.events.recv().await {
                crate::test_support::capture_run_failure(&event.event, &mut failures);
            }
            (run.result.await.unwrap(), failures)
        },
    )
    .await;
    assert_eq!(
        result.status,
        NativeInputStatus::Succeeded,
        "{}",
        crate::test_support::native_run_diagnostic(&result, &failures)
    );
    within_session(
        &session,
        "Session cleanup after NativeInput",
        session.close(),
    )
    .await
    .unwrap();
    assert!(
        session
            .read_input(
                source,
                entry.exact_input_view().clone(),
                entry.operation().clone()
            )
            .await
            .is_err()
    );
}

#[tokio::test]
async fn native_input_trigger_cannot_skip_flow_run_authority() {
    let (fixture, session, attachment) = setup().await;
    let entry = session.input_entries()[0].clone();
    let input = within_session(
        &session,
        "authority-test physical Input read",
        session.read_input(
            InputSourceId::parse(entry.source_pack().as_str()).unwrap(),
            entry.exact_input_view().clone(),
            entry.operation().clone(),
        ),
    )
    .await
    .unwrap();
    let target = session
        .gate
        .resolve_frozen_flow_target(
            &input.context,
            &ShapeRef::parse("local.fixture.turn@1.0").unwrap(),
            Some("local.fixture/main"),
        )
        .unwrap();
    let request = tobkiri_gate::EffectRequest::FlowRun {
        implements: target.implements.clone(),
        flow_owner: (target.owner.clone(), target.owner_hash.clone()),
        flow_key: target.flow_key.clone(),
        target_digest: target.digest().unwrap(),
    };
    let expected = CanonValue::Str(request.digest().unwrap().to_string());
    let mut reduced = fixture.profile.ceiling.clone();
    reduced.ports.remove("local.fixture.turn@1.0");
    Approvals::open(&fixture.root, &fixture.profile.id)
        .unwrap()
        .set_permissions(
            &fixture.profile.id,
            &attachment.pack,
            &attachment.hash,
            &reduced,
        )
        .unwrap();
    let denied = within_session(
        &session,
        "authority-test NativeInput admission",
        session.submit_native_input(input),
    )
    .await;
    match denied {
        Err(CoreError::Gate(tobkiri_gate::GateError::Denied(reason))) => assert_eq!(
            reason,
            "ingress exceeds current source or destination authority"
        ),
        Err(error) => panic!("unexpected ingress failure: {error}"),
        Ok(_) => panic!("NativeInput admitted a route after its source authority was revoked"),
    }
    assert!(fixture.audit_events().iter().all(|event| event.kind != "dispatch" || !matches!(&event.payload, CanonValue::Obj(fields) if fields.get("effect_digest") == Some(&expected))));
    within_session(
        &session,
        "Session cleanup after denied NativeInput",
        session.close(),
    )
    .await
    .unwrap();
}

#[tokio::test]
async fn ui_cancel_surfaces_source_cleanup_failure() {
    let (_fixture, session, attachment) = setup().await;
    let counter = Arc::new(AtomicUsize::new(0));
    let handle = install_source(
        &session,
        &attachment.session,
        Events {
            remaining: 3,
            cancelled: counter.clone(),
            waiting: false,
            fail_close: true,
        },
        TokioCancellationToken::new(),
    );
    assert!(
        session
            .ui_stream_cancel(&attachment.session, handle)
            .await
            .is_err()
    );
    assert!(session.streams.lock().unwrap().is_empty());
    assert!(counter.load(Ordering::SeqCst) > 0);
    session.close().await.unwrap();
}

#[tokio::test]
async fn session_close_cleans_all_frames_even_if_one_cleanup_fails() {
    let (_fixture, session, attachment) = setup().await;
    let second = extra_attachment(&session, &attachment, "ui_good_cleanup");
    let counter = Arc::new(AtomicUsize::new(0));
    for (ui, fail_close) in [(&attachment.session, true), (&second, false)] {
        install_source(
            &session,
            ui,
            Events {
                remaining: 3,
                cancelled: counter.clone(),
                waiting: false,
                fail_close,
            },
            TokioCancellationToken::new(),
        );
    }
    assert!(session.close().await.is_err());
    assert!(session.streams.lock().unwrap().is_empty());
    assert!(session.ui.lock().unwrap().is_empty());
    assert!(counter.load(Ordering::SeqCst) >= 2);
}

struct SmallStreamOutput;
#[async_trait]
impl tobkiri_gate::Mediator for SmallStreamOutput {
    async fn review(&self, prompt: &tobkiri_gate::ApprovalPrompt) -> tobkiri_gate::Verdict {
        if matches!(
            prompt.effect,
            tobkiri_gate::EffectRequest::WasmEntry {
                entry: tobkiri_gate::WasmEntryKind::StreamOpen { .. },
                ..
            }
        ) {
            let mut allowed = crate::test_support::permissions(&["fixture"]);
            allowed.wasm.output_bytes = 16;
            tobkiri_gate::Verdict::Constrain(allowed)
        } else {
            tobkiri_gate::Verdict::Pass
        }
    }
}

#[tokio::test]
async fn stream_uses_own_entry_mediator_budget_across_multiple_pulls() {
    let fixture = RuntimeFixture::new(&["fixture"], true).await;
    assert_eq!(fixture.profile.ceiling.wasm.output_bytes, 1024 * 1024);
    let session = fixture
        .mediated_session(tobkiri_gate::MediatorChain::new(vec![Arc::new(
            SmallStreamOutput,
        )]))
        .await;
    session.bind_ui_generation(GENERATION).unwrap();
    let attachment = session.ui_attachments().unwrap().remove(0);
    let handle = open_stream(
        &session,
        &attachment.session,
        "stream",
        TokioCancellationToken::new(),
    )
    .await;
    // Each batch individually fits the 16-byte entry limit. Their accumulated
    // 11 + 6 bytes must fail under that stream's actual mediated admission,
    // independently of the much larger source Pack and Profile limits.
    assert_eq!(
        batch_len(
            session
                .ui_stream_pull_buffer(&attachment.session, handle, 2)
                .await
                .unwrap()
        ),
        2
    );
    let error = match session
        .ui_stream_pull_buffer(&attachment.session, handle, 1)
        .await
    {
        Err(error) => error,
        Ok(_) => panic!("stream ignored its own accumulated output limit"),
    };
    assert!(matches!(error, CoreError::Step(StepError { code, .. }) if code == "limit-exceeded"));
    assert!(session.streams.lock().unwrap().is_empty());
    assert_eq!(
        invoke(&session, &attachment, input_shape(), "read", &empty_args())
            .await
            .unwrap()
            .as_bytes(),
        br#"{"kind":"value","value":42}"#
    );
    session.close().await.unwrap();
}

// Sibling cleanup tests borrow the actual verified Session fixture. Keep
// private-field access inside this existing cfg(test) child module; these
// helpers create no authority, replacement owner or production API.
impl Session {
    pub(crate) async fn stream_physical_completion_for_test(
        &self,
        handle: u64,
    ) -> tobkiri_flow::StreamPhysicalCompletion {
        let entry = self.streams.lock().unwrap().get(&handle).unwrap().clone();
        let source = entry.state.lock().await;
        source
            .as_ref()
            .unwrap()
            .stream
            .physical_completion()
            .expect("actual component source physical receipt")
    }

    pub(crate) fn operation_intent_factory_for_test(&self) -> Arc<ExecutionFactory> {
        self.execution_factory
            .as_ref()
            .expect("real fixture execution factory")
            .clone()
    }

    pub(crate) fn operation_intent_ui_registration_for_test(
        &self,
    ) -> (UiSessionId, PackId, PackHash, BTreeSet<ShapeRef>) {
        let sessions = self.ui.lock().expect("real fixture UI registry");
        let (ui, principal) = sessions.iter().next().expect("real fixture UI principal");
        (
            ui.clone(),
            principal.pack.clone(),
            principal.hash.clone(),
            principal.ports.clone(),
        )
    }
}
