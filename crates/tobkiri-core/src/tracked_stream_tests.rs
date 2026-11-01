use super::*;
use std::{
    collections::{BTreeMap, BTreeSet},
    sync::{
        Mutex,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
    time::{Duration, Instant},
};
use tobkiri_canon::{
    CanonValue, CoreValue, Digest, Variant,
    ids::{InputSourceId, OpName, ProfileId, RequestId, ShapeRef},
};

use tobkiri_gate::{
    ApprovalAnswer, ApprovalPrompt, ApprovalStore, AuditEvent, AuditSink, EffectRequest, GateError,
    MediatorChain, Principal, ResourceLimits, TrustedPrompt,
};
use tobkiri_store::{Approvals, DataRoot};

#[derive(Default)]
struct Audit {
    events: Mutex<Vec<AuditEvent>>,
    fail: AtomicBool,
    delivered: Mutex<BTreeMap<Digest, Vec<u8>>>,
}
impl AuditSink for Audit {
    fn append(&self, event: AuditEvent) -> Result<(), GateError> {
        if event.kind == "outcome" && self.fail.load(Ordering::SeqCst) {
            return Err(GateError::Persistence("fixture audit unavailable".into()));
        }
        self.events.lock().unwrap().push(event);
        Ok(())
    }
    fn append_idempotent(&self, id: &Digest, event: AuditEvent) -> Result<(), GateError> {
        crate::test_support::append_once(&self.delivered, id, &event, || self.append(event.clone()))
    }
}
struct Prompt;
#[async_trait]
impl TrustedPrompt for Prompt {
    async fn prompt_approval(&self, _: ApprovalPrompt) -> ApprovalAnswer {
        ApprovalAnswer::Deny
    }
}
struct Source {
    frames: Vec<CoreValue>,
    cancelled: Arc<AtomicUsize>,
}
#[async_trait]
impl StreamSource for Source {
    async fn pull(&mut self, max: u32) -> Result<CoreValue, StepError> {
        let count = self.frames.len().min(max as usize);
        Ok(crate::test_support::batch(self.frames.drain(..count)))
    }
    fn cancel(&mut self) {
        self.cancelled.fetch_add(1, Ordering::SeqCst);
    }
}
struct Fixture {
    gate: Arc<GateEngine>,
    audit: Arc<Audit>,
    cx: CallCx,
    grant: GrantId,
    approvals: Arc<Approvals>,
    _dir: tempfile::TempDir,
}
async fn fixture() -> Fixture {
    let dir = tempfile::tempdir().unwrap();
    let root = DataRoot::open(std::fs::canonicalize(dir.path()).unwrap()).unwrap();
    let profile = ProfileId::parse("prof_stream").unwrap();
    let source = InputSourceId::parse("local.input").unwrap();
    let shape = ShapeRef::parse("local.fixture.input@1.0").unwrap();
    let audit = Arc::new(Audit::default());
    let approvals = Arc::new(Approvals::open(&root, &profile).unwrap());
    let gate = Arc::new(GateEngine::new(
        approvals.clone(),
        audit.clone(),
        Arc::new(Prompt),
        MediatorChain::default(),
    ));
    let limits = ResourceLimits {
        fuel_per_call: 100000,
        memory_mib: 8,
        wall_ms: 1000,
        table_elements: 1024,
        output_bytes: 1048576,
    };
    gate.register_input(
        &profile,
        &source,
        BTreeSet::from([shape.to_string()]),
        limits.clone(),
    )
    .unwrap();
    let revision = Digest::hash(b"tracked-view");
    gate.register_operation_views(
        &profile,
        &revision,
        BTreeMap::from([(
            shape.clone(),
            [OpName::parse("submit").unwrap()].into_iter().collect(),
        )]),
    )
    .unwrap();
    let cx = CallCx::root(
        profile,
        Principal::Input { source },
        RequestId::parse("req_stream").unwrap(),
        tobkiri_gate::CancellationToken::new(),
        Instant::now() + Duration::from_secs(1),
        limits,
    )
    .with_revision(revision);
    let request = EffectRequest::PortCall {
        shape,
        op: OpName::parse("submit").unwrap(),
    };
    let args = Digest::hash(b"input");
    let grant = gate
        .authorize(&cx, request.clone(), args.clone())
        .await
        .unwrap()
        .consume(&cx, &request, &args)
        .unwrap()
        .id()
        .clone();
    Fixture {
        gate,
        audit,
        cx,
        grant,
        approvals,
        _dir: dir,
    }
}
impl Fixture {
    fn wrap(&self, source: CoreStream) -> CoreStream {
        CoreStream::new(TrackedStream::new(
            source,
            self.gate.clone(),
            self.cx.clone(),
            self.grant.clone(),
            Handle::current(),
        ))
    }
    fn outcomes(&self) -> Vec<CanonValue> {
        self.audit
            .events
            .lock()
            .unwrap()
            .iter()
            .filter(|event| event.kind == "outcome")
            .filter_map(|event| match &event.payload {
                CanonValue::Obj(fields) => fields.get("outcome").cloned(),
                _ => None,
            })
            .collect()
    }
}
fn outcome(value: Outcome) -> CanonValue {
    tobkiri_canon::parse(&serde_json::to_vec(&value).unwrap(), Variant::StrictV1).unwrap()
}
fn completed(cx: &CallCx) -> CoreValue {
    RunEvent {
        request: cx.request().clone(),
        plan_digest: Digest::hash(b"frozen"),
        sequence: 1,
        kind: RunEventKind::Completed {
            value: crate::test_support::value(&CanonValue::Null),
            cost_units: 1,
        },
    }
    .to_value(&crate::test_support::value_budget())
    .unwrap()
}
#[tokio::test]
async fn silent_eof_is_failed_and_never_a_success() {
    let fixture = fixture().await;
    let counter = Arc::new(AtomicUsize::new(0));
    let mut stream = fixture.wrap(CoreStream::new(Source {
        frames: Vec::new(),
        cancelled: counter.clone(),
    }));
    assert_eq!(stream.pull(1).await.unwrap_err().code, "internal");
    assert_eq!(
        fixture.outcomes(),
        vec![outcome(Outcome::Failed {
            code: "internal".into()
        })]
    );
    stream.close().await.unwrap();
    assert!(counter.load(Ordering::SeqCst) > 0);
}
#[tokio::test]
async fn aborted_subflow_channel_is_failed() {
    let fixture = fixture().await;
    // The production RunHandle constructor is private because it owns native
    // transport reservations. Exercise the same abrupt channel EOF at the
    // TrackedStream boundary without fabricating those reservations.
    struct AbortedChannel(tokio::sync::mpsc::Receiver<CoreValue>);
    #[async_trait]
    impl StreamSource for AbortedChannel {
        async fn pull(&mut self, _: u32) -> Result<CoreValue, StepError> {
            match self.0.recv().await {
                Some(value) => Ok(crate::test_support::batch(std::iter::once(value))),
                None => Err(StepError::new(
                    "internal",
                    "subflow producer ended without terminal",
                )),
            }
        }
        fn cancel(&mut self) {
            self.0.close();
        }
    }
    let (events_tx, events) = tokio::sync::mpsc::channel(1);
    let task = tokio::spawn(async move {
        let _producer = events_tx;
        std::future::pending::<()>().await;
    });
    task.abort();
    assert!(task.await.unwrap_err().is_cancelled());
    let source = CoreStream::new(AbortedChannel(events));
    let mut stream = fixture.wrap(source);
    assert_eq!(stream.pull(1).await.unwrap_err().code, "internal");
    assert_eq!(
        fixture.outcomes(),
        vec![outcome(Outcome::Failed {
            code: "internal".into()
        })]
    );
}
#[tokio::test]
async fn completed_terminal_is_preserved_at_eof_and_close() {
    let fixture = fixture().await;
    let mut stream = fixture.wrap(CoreStream::new(Source {
        frames: vec![completed(&fixture.cx)],
        cancelled: Arc::new(AtomicUsize::new(0)),
    }));
    assert_eq!(stream.pull(1).await.unwrap().array().unwrap().len(), 1);
    assert!(fixture.outcomes().is_empty());
    assert!(stream.pull(1).await.unwrap().array().unwrap().is_empty());
    stream.close().await.unwrap();
    stream.close().await.unwrap();
    assert_eq!(fixture.outcomes(), vec![outcome(Outcome::Succeeded)]);
}
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn non_runtime_thread_cancel_completes_on_captured_runtime() {
    let fixture = fixture().await;
    let counter = Arc::new(AtomicUsize::new(0));
    let stream = fixture.wrap(CoreStream::new(Source {
        frames: Vec::new(),
        cancelled: counter.clone(),
    }));
    let mut stream = std::thread::spawn(move || {
        assert!(Handle::try_current().is_err());
        let mut stream = stream;
        stream.cancel();
        stream
    })
    .join()
    .unwrap();
    tokio::time::timeout(Duration::from_millis(250), stream.close())
        .await
        .unwrap()
        .unwrap();
    assert!(counter.load(Ordering::SeqCst) > 0);
    assert_eq!(fixture.outcomes(), vec![outcome(Outcome::Cancelled)]);
}
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn non_runtime_thread_drop_still_records_cleanup() {
    let fixture = fixture().await;
    let stream = fixture.wrap(CoreStream::new(Source {
        frames: Vec::new(),
        cancelled: Arc::new(AtomicUsize::new(0)),
    }));
    std::thread::spawn(move || {
        assert!(Handle::try_current().is_err());
        drop(stream);
    })
    .join()
    .unwrap();
    tokio::time::timeout(Duration::from_millis(250), async {
        while fixture.outcomes().is_empty() {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    assert_eq!(fixture.outcomes(), vec![outcome(Outcome::Cancelled)]);
}
#[tokio::test]
async fn failed_audit_is_visible_after_cancel_and_on_repeated_close() {
    let fixture = fixture().await;
    fixture.audit.fail.store(true, Ordering::SeqCst);
    let mut stream = fixture.wrap(CoreStream::new(Source {
        frames: Vec::new(),
        cancelled: Arc::new(AtomicUsize::new(0)),
    }));
    stream.cancel();
    let error = stream.pull(1).await.unwrap_err();
    assert_eq!(error.audit_code(), "internal");
    assert_eq!(stream.close().await.unwrap_err(), error);
    assert_eq!(stream.close().await.unwrap_err(), error);
    assert!(fixture.outcomes().is_empty());
}
#[tokio::test]
async fn explicit_close_surfaces_audit_failure() {
    let fixture = fixture().await;
    fixture.audit.fail.store(true, Ordering::SeqCst);
    let mut stream = fixture.wrap(CoreStream::new(Source {
        frames: vec![completed(&fixture.cx)],
        cancelled: Arc::new(AtomicUsize::new(0)),
    }));
    assert_eq!(stream.pull(1).await.unwrap().array().unwrap().len(), 1);
    assert!(stream.close().await.is_err());
    assert!(fixture.outcomes().is_empty());
}

#[tokio::test]
async fn close_retries_same_terminal_proposal_without_repeating_physical_cleanup() {
    let fixture = fixture().await;
    fixture.audit.fail.store(true, Ordering::SeqCst);
    let counter = Arc::new(AtomicUsize::new(0));
    let mut stream = fixture.wrap(CoreStream::new(Source {
        frames: vec![completed(&fixture.cx)],
        cancelled: counter.clone(),
    }));
    stream.pull(1).await.unwrap();
    assert!(stream.close().await.is_err());
    let calls = counter.load(Ordering::SeqCst);
    assert!(calls > 0);
    let before = fixture
        .approvals
        .terminal_delivery(&fixture.grant)
        .unwrap()
        .unwrap();
    assert!(!before.acknowledged);
    fixture.audit.fail.store(false, Ordering::SeqCst);
    stream.close().await.unwrap();
    stream.close().await.unwrap();
    let after = fixture
        .approvals
        .terminal_delivery(&fixture.grant)
        .unwrap()
        .unwrap();
    assert!(after.acknowledged);
    assert_eq!(
        serde_json::to_vec(&after.decision).unwrap(),
        serde_json::to_vec(&before.decision).unwrap()
    );
    assert_eq!(counter.load(Ordering::SeqCst), calls);
    assert_eq!(fixture.outcomes(), vec![outcome(Outcome::Succeeded)]);
    assert_eq!(fixture.audit.delivered.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn cancel_then_pull_failure_can_retry_only_its_audit() {
    let fixture = fixture().await;
    fixture.audit.fail.store(true, Ordering::SeqCst);
    let counter = Arc::new(AtomicUsize::new(0));
    let mut stream = fixture.wrap(CoreStream::new(Source {
        frames: Vec::new(),
        cancelled: counter.clone(),
    }));
    stream.cancel();
    assert!(stream.pull(1).await.is_err());
    let calls = counter.load(Ordering::SeqCst);
    fixture.audit.fail.store(false, Ordering::SeqCst);
    // No producer-owned EOF batch was observed before cancellation. A
    // delivery retry may settle the same receipt but cannot invent one.
    assert_eq!(stream.pull(1).await.unwrap_err().code, "unavailable");
    stream.close().await.unwrap();
    assert_eq!(counter.load(Ordering::SeqCst), calls);
    assert_eq!(fixture.outcomes(), vec![outcome(Outcome::Cancelled)]);
}

#[tokio::test]
async fn cancelled_terminal_retry_after_deadline_preserves_original_winner() {
    let fixture = fixture().await;
    fixture.audit.fail.store(true, Ordering::SeqCst);
    let counter = Arc::new(AtomicUsize::new(0));
    let mut stream = fixture.wrap(CoreStream::new(Source {
        frames: Vec::new(),
        cancelled: counter.clone(),
    }));
    stream.cancel();
    assert!(stream.close().await.is_err());
    let cleanup_calls = counter.load(Ordering::SeqCst);
    let pending = fixture
        .approvals
        .terminal_delivery(&fixture.grant)
        .unwrap()
        .unwrap();
    assert!(!pending.acknowledged);
    assert_eq!(pending.decision.outcome, Outcome::Cancelled);

    tokio::time::sleep_until((fixture.cx.deadline() + Duration::from_millis(10)).into()).await;
    fixture.audit.fail.store(false, Ordering::SeqCst);
    stream.close().await.unwrap();
    stream.close().await.unwrap();
    let delivered = fixture
        .approvals
        .terminal_delivery(&fixture.grant)
        .unwrap()
        .unwrap();
    assert!(delivered.acknowledged);
    assert_eq!(
        serde_json::to_vec(&pending.decision).unwrap(),
        serde_json::to_vec(&delivered.decision).unwrap()
    );
    assert_eq!(counter.load(Ordering::SeqCst), cleanup_calls);
    assert_eq!(fixture.outcomes(), vec![outcome(Outcome::Cancelled)]);
    assert_eq!(fixture.audit.delivered.lock().unwrap().len(), 1);
}

struct FailingClose(Arc<AtomicUsize>);
#[async_trait]
impl StreamSource for FailingClose {
    async fn pull(&mut self, _: u32) -> Result<CoreValue, StepError> {
        Ok(crate::test_support::batch(std::iter::empty()))
    }
    fn cancel(&mut self) {}
    async fn close(&mut self) -> Result<(), StepError> {
        self.0.fetch_add(1, Ordering::SeqCst);
        Err(StepError::new("trap", "fixture physical cleanup failure"))
    }
}
#[tokio::test]
async fn retry_delivers_failed_cleanup_proposal_but_never_claims_cleanup_succeeded() {
    let fixture = fixture().await;
    fixture.audit.fail.store(true, Ordering::SeqCst);
    let closes = Arc::new(AtomicUsize::new(0));
    let mut stream = fixture.wrap(CoreStream::new(FailingClose(closes.clone())));
    let error = stream.close().await.unwrap_err();
    assert_eq!(error.code, "trap");
    fixture.audit.fail.store(false, Ordering::SeqCst);
    assert_eq!(stream.close().await.unwrap_err(), error);
    assert_eq!(stream.close().await.unwrap_err(), error);
    assert_eq!(closes.load(Ordering::SeqCst), 1);
    assert_eq!(
        fixture.outcomes(),
        vec![outcome(Outcome::Failed {
            code: "trap".into()
        })]
    );
}

struct WaitingClose {
    started: Arc<tokio::sync::Notify>,
    release: Arc<tokio::sync::Notify>,
}
#[async_trait]
impl StreamSource for WaitingClose {
    async fn pull(&mut self, _: u32) -> Result<CoreValue, StepError> {
        Ok(crate::test_support::batch(std::iter::empty()))
    }
    fn cancel(&mut self) {}
    async fn close(&mut self) -> Result<(), StepError> {
        self.started.notify_one();
        self.release.notified().await;
        Ok(())
    }
}
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn abandoned_close_future_retains_cleanup_failure_receipt() {
    let fixture = fixture().await;
    fixture.audit.fail.store(true, Ordering::SeqCst);
    let started = Arc::new(tokio::sync::Notify::new());
    let release = Arc::new(tokio::sync::Notify::new());
    let mut stream = fixture.wrap(CoreStream::new(WaitingClose {
        started: started.clone(),
        release: release.clone(),
    }));
    assert!(
        tokio::time::timeout(Duration::from_millis(20), stream.close())
            .await
            .is_err()
    );
    started.notified().await;
    release.notify_one();
    let error = tokio::time::timeout(Duration::from_millis(250), stream.close())
        .await
        .unwrap()
        .unwrap_err();
    assert_eq!(stream.close().await.unwrap_err(), error);
    assert!(fixture.outcomes().is_empty());
}
#[test]
fn owning_runtime_shutdown_is_an_error_and_does_not_claim_a_terminal_audit() {
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .unwrap();
    let (fixture, mut stream) = runtime.block_on(async {
        let fixture = fixture().await;
        let stream = fixture.wrap(CoreStream::new(Source {
            frames: Vec::new(),
            cancelled: Arc::new(AtomicUsize::new(0)),
        }));
        (fixture, stream)
    });
    runtime.shutdown_timeout(Duration::from_millis(100));
    stream.cancel();
    let error = futures_close(&mut stream).unwrap_err();
    assert_eq!(error.code, "internal");
    assert!(fixture.outcomes().is_empty());
    assert_eq!(futures_close(&mut stream).unwrap_err(), error);
}
fn futures_close(stream: &mut CoreStream) -> Result<(), StepError> {
    // A fresh executor only observes the captured runtime's aborted receipt;
    // it cannot pretend to finish cleanup owned by the shut-down runtime.
    let observer = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    observer.block_on(stream.close())
}
