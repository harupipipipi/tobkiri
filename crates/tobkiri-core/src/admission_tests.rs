use super::*;
use async_trait::async_trait;
use std::{
    collections::{BTreeMap, BTreeSet},
    sync::{
        Mutex,
        atomic::{AtomicBool, Ordering},
    },
    time::Instant,
};
use tobkiri_canon::{
    Digest,
    ids::{InputSourceId, OpName, RequestId, ShapeRef},
};
use tobkiri_gate::{
    ApprovalAnswer, ApprovalPrompt, ApprovalStore, AuditEvent, AuditSink, CallCx, EffectRequest,
    Gate, MediatorChain, Outcome, Principal, ResourceLimits, TrustedPrompt,
};
use tobkiri_store::{Approvals, DataRoot};

struct Prompt;
#[async_trait]
impl TrustedPrompt for Prompt {
    async fn prompt_approval(&self, _: ApprovalPrompt) -> ApprovalAnswer {
        ApprovalAnswer::Approve
    }
}
#[derive(Default)]
struct Sink {
    fail: AtomicBool,
    fail_once: AtomicBool,
    one_shot_seen: AtomicBool,
    events: Mutex<BTreeMap<Digest, Vec<u8>>>,
}
impl AuditSink for Sink {
    fn append(&self, _: AuditEvent) -> Result<(), GateError> {
        Ok(())
    }
    fn append_idempotent(&self, id: &Digest, event: AuditEvent) -> Result<(), GateError> {
        if self.fail.load(Ordering::SeqCst) {
            return Err(GateError::Unavailable);
        }
        if self.fail_once.swap(false, Ordering::SeqCst) {
            self.one_shot_seen.store(true, Ordering::SeqCst);
            return Err(GateError::Persistence(
                "fixture one-shot audit error".into(),
            ));
        }
        crate::test_support::append_once(&self.events, id, &event, || Ok(()))
    }
}
fn setup() -> (Arc<GateEngine>, Arc<Sink>, ProfileId, tempfile::TempDir) {
    let dir = tempfile::tempdir().unwrap();
    let root = DataRoot::open(std::fs::canonicalize(dir.path()).unwrap()).unwrap();
    let profile = ProfileId::parse("prof_recovery").unwrap();
    let sink = Arc::new(Sink::default());
    let gate = Arc::new(GateEngine::new(
        Arc::new(Approvals::open(&root, &profile).unwrap()),
        sink.clone(),
        Arc::new(Prompt),
        MediatorChain::default(),
    ));
    (gate, sink, profile, dir)
}
async fn consumed(
    gate: &GateEngine,
    profile: &ProfileId,
    wall: Duration,
) -> (CallCx, tobkiri_gate::DispatchPermit) {
    let shape = ShapeRef::parse("local.fixture.input@1.0").unwrap();
    let source = InputSourceId::parse("local.input").unwrap();
    let limits = ResourceLimits {
        fuel_per_call: 1000,
        memory_mib: 8,
        wall_ms: 1000,
        table_elements: 64,
        output_bytes: 4096,
    };
    let revision = Digest::hash(b"recovery-plan");
    gate.register_input(
        profile,
        &source,
        BTreeSet::from([shape.to_string()]),
        limits.clone(),
    )
    .unwrap();
    gate.register_operation_views(
        profile,
        &revision,
        BTreeMap::from([(
            shape.clone(),
            BTreeSet::from([OpName::parse("submit").unwrap()]),
        )]),
    )
    .unwrap();
    let cx = CallCx::root(
        profile.clone(),
        Principal::Input { source },
        RequestId::parse("req_recovery").unwrap(),
        CancellationToken::new(),
        Instant::now() + wall,
        limits,
    )
    .with_revision(revision);
    let request = EffectRequest::PortCall {
        shape,
        op: OpName::parse("submit").unwrap(),
    };
    let args = Digest::hash(b"args");
    let permit = gate
        .authorize(&cx, request.clone(), args.clone())
        .await
        .unwrap()
        .consume(&cx, &request, &args)
        .unwrap();
    gate.watch_lease(&cx, &permit).unwrap();
    (cx, permit)
}
async fn undelivered(gate: &GateEngine, sink: &Sink, profile: &ProfileId) {
    let (_, permit) = consumed(gate, profile, Duration::from_secs(1)).await;
    sink.fail.store(true, Ordering::SeqCst);
    assert!(
        gate.record_outcome(permit.id().clone(), Outcome::Succeeded)
            .await
            .is_err()
    );
}
#[test]
fn admission_without_runtime_is_unavailable_but_sync_recovery_is_allowed() {
    let (gate, _, profile, _dir) = setup();
    let registry = Admissions::default();
    let barrier = registry.install(profile, gate).unwrap();
    assert!(matches!(
        {
            use std::future::Future;
            let mut future = std::pin::pin!(barrier.ensure_ready());
            let mut cx = std::task::Context::from_waker(std::task::Waker::noop());
            match future.as_mut().poll(&mut cx) {
                std::task::Poll::Ready(result) => result,
                std::task::Poll::Pending => panic!("runtime rejection must not await"),
            }
        },
        Err(CoreError::Gate(GateError::Unavailable))
    ));
}
#[tokio::test]
async fn startup_denies_until_recovered_watermark_is_delivered_then_retries() {
    let (gate, sink, profile, _dir) = setup();
    undelivered(&gate, &sink, &profile).await;
    let barrier = Barrier::new(profile.clone(), gate.clone(), CancellationToken::new());
    assert!(barrier.ensure_ready().await.is_err());
    assert!(!barrier.state.lock().await.ready);
    sink.fail.store(false, Ordering::SeqCst);
    barrier.ensure_ready().await.unwrap();
    assert!(barrier.state.lock().await.ready);
    assert_eq!(gate.flush_outbox(&profile).await.unwrap(), 0);
    assert_eq!(sink.events.lock().unwrap().len(), 1);
}
#[tokio::test]
async fn an_ended_supervisor_requires_a_fresh_barrier_drain() {
    let (gate, sink, profile, _dir) = setup();
    let barrier = Barrier::new(profile.clone(), gate.clone(), CancellationToken::new());
    barrier.ensure_ready().await.unwrap();
    let task = barrier.state.lock().await.supervisor.take().unwrap();
    task.abort();
    let _ = task.await;
    undelivered(&gate, &sink, &profile).await;
    assert!(barrier.ensure_ready().await.is_err());
    sink.fail.store(false, Ordering::SeqCst);
    barrier.ensure_ready().await.unwrap();
    assert_eq!(gate.flush_outbox(&profile).await.unwrap(), 0);
}
#[tokio::test]
async fn stopped_session_does_not_restart_its_supervisor() {
    let (gate, _, profile, _dir) = setup();
    let stop = CancellationToken::new();
    let barrier = Barrier::new(profile, gate, stop.clone());
    barrier.ensure_ready().await.unwrap();
    stop.cancel();
    assert!(barrier.ensure_ready().await.is_err());
}

#[tokio::test]
async fn supervisor_expires_consumed_lease_without_waiting_for_new_admission() {
    let (gate, sink, profile, _dir) = setup();
    let barrier = Barrier::new(profile.clone(), gate.clone(), CancellationToken::new());
    barrier.ensure_ready().await.unwrap();
    let (cx, _permit) = consumed(&gate, &profile, Duration::from_millis(250)).await;
    tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            if cx.cancel().is_cancelled() && sink.events.lock().unwrap().len() == 1 {
                break;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    let events = sink.events.lock().unwrap();
    let event: AuditEvent = serde_json::from_slice(events.values().next().unwrap()).unwrap();
    let tobkiri_canon::CanonValue::Obj(payload) = event.payload else {
        panic!("terminal audit payload");
    };
    assert_eq!(
        payload["disposition"],
        tobkiri_canon::CanonValue::Str("expired-cleanup-unknown".into())
    );
    let tobkiri_canon::CanonValue::Obj(outcome) = &payload["outcome"] else {
        panic!("terminal outcome");
    };
    assert_eq!(
        outcome["status"],
        tobkiri_canon::CanonValue::Str("failed".into())
    );
    assert_eq!(
        outcome["code"],
        tobkiri_canon::CanonValue::Str("deadline-failure-unknown".into())
    );
}

#[tokio::test]
async fn supervisor_survives_transient_persistence_and_cancels_deadlines_during_delivery_backoff() {
    let (gate, sink, profile, _dir) = setup();
    let barrier = Barrier::new(profile.clone(), gate.clone(), CancellationToken::new());
    barrier.ensure_ready().await.unwrap();
    undelivered(&gate, &sink, &profile).await;
    sink.fail_once.store(true, Ordering::SeqCst);
    sink.fail.store(false, Ordering::SeqCst);
    let (cx, _permit) = consumed(&gate, &profile, Duration::from_millis(250)).await;
    tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            if sink.one_shot_seen.load(Ordering::SeqCst)
                && cx.cancel().is_cancelled()
                && sink.events.lock().unwrap().len() == 2
                // Delivery appends before the supervisor joins that job and
                // publishes recovered health on its next tick. Observe the
                // completed recovery, not just the sink-side write.
                && barrier.health_error().is_none()
            {
                break;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    assert!(
        barrier
            .state
            .lock()
            .await
            .supervisor
            .as_ref()
            .is_some_and(|task| !task.is_finished())
    );
    assert!(barrier.health_error().is_none());
    barrier.close().await.unwrap();
}

#[tokio::test]
async fn persistent_delivery_error_is_visible_on_close_while_deadline_supervision_continues() {
    let (gate, sink, profile, _dir) = setup();
    let barrier = Barrier::new(profile.clone(), gate.clone(), CancellationToken::new());
    barrier.ensure_ready().await.unwrap();
    undelivered(&gate, &sink, &profile).await;
    let (cx, _permit) = consumed(&gate, &profile, Duration::from_millis(250)).await;
    tokio::time::timeout(Duration::from_secs(2), async {
        while !cx.cancel().is_cancelled() || barrier.health_error().is_none() {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    assert!(barrier.close().await.is_err());
}

#[tokio::test]
async fn durable_integrity_fault_denies_ready_fast_path_without_stopping_existing_deadlines() {
    let (gate, _sink, profile, dir) = setup();
    let barrier = Barrier::new(profile.clone(), gate.clone(), CancellationToken::new());
    barrier.ensure_ready().await.unwrap();
    let (due, _permit) = consumed(&gate, &profile, Duration::from_millis(250)).await;
    let (_, independent) = consumed(&gate, &profile, Duration::from_secs(1)).await;
    let root = DataRoot::open(std::fs::canonicalize(dir.path()).unwrap()).unwrap();
    let approvals = Approvals::open(&root, &profile).unwrap();
    approvals
        .latch_integrity(&profile, IntegrityFaultCode::AuditIdentityConflict)
        .unwrap();
    assert!(matches!(
        barrier.ensure_ready().await,
        Err(CoreError::Gate(GateError::Integrity(
            IntegrityFaultCode::AuditIdentityConflict
        )))
    ));
    // Independent fully verified existing work may settle, but its success
    // cannot reopen admission or clear the durable fault.
    gate.record_outcome(independent.id().clone(), Outcome::Succeeded)
        .await
        .unwrap();
    assert!(barrier.ensure_ready().await.is_err());
    tokio::time::timeout(Duration::from_secs(1), async {
        while !due.cancel().is_cancelled() {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    assert!(
        barrier
            .state
            .lock()
            .await
            .supervisor
            .as_ref()
            .is_some_and(|task| !task.is_finished())
    );
    assert!(matches!(
        gate.admission_status(&profile).unwrap(),
        AdmissionStatus::IntegrityFault {
            code: IntegrityFaultCode::AuditIdentityConflict
        }
    ));
    assert!(barrier.close().await.is_err());
}
