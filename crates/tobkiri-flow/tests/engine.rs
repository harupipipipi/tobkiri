mod support;

use std::{
    collections::BTreeMap,
    sync::{
        Arc, Mutex,
        atomic::{AtomicUsize, Ordering},
    },
    time::{Duration, Instant},
};

use async_trait::async_trait;
use tobkiri_canon::{
    AllocationBudget, AllocationError, AllocationKind, AllocationRequest, CanonValue,
    ControlHandle, CoreValue, Digest,
    ids::{OpName, PackHash, PackId, ShapeRef},
};
use tobkiri_flow::{
    ActiveCodeFrame, CoreStream, FlowEngine, FlowRunner, PortInvoker, ReachedNode, RunEvent,
    RunEventKind, RunJournal, StepError, StepOutput, StreamCloseControl, StreamCloseReason,
    StreamSource,
};
use tobkiri_gate::{CallCx, ChargeKind, ControlKind, LoopLimits, Principal};
use tobkiri_profile::*;

fn shape() -> ShapeRef {
    ShapeRef::parse("local.fixture.io@1.0").unwrap()
}
fn value(json: &str) -> CanonValue {
    tobkiri_canon::parse(json.as_bytes(), tobkiri_canon::Variant::StrictV1).unwrap()
}
fn owned(value: CanonValue) -> CoreValue {
    CoreValue::try_from_borrowed(&value, &support::budget()).unwrap()
}
fn owned_json(json: &str) -> CoreValue {
    owned(value(json))
}
fn canonical(value: &CoreValue) -> CanonValue {
    let encoded = value.encode_guest(&support::budget()).unwrap();
    tobkiri_canon::parse(encoded.as_bytes(), tobkiri_canon::Variant::StrictV1).unwrap()
}
fn step(id: &str, kind: StepKind, out: Option<&str>) -> Step {
    Step {
        id: Some(id.into()),
        role: None,
        overlay: OverlayAccess::Closed,
        kind,
        out: out.map(str::to_owned),
        stream: None,
    }
}
fn call(id: &str, op: &str, args: CanonValue, out: Option<&str>) -> Step {
    step(
        id,
        StepKind::Call(PortCall {
            shape: shape(),
            op: OpName::parse(op).unwrap(),
            args,
        }),
        out,
    )
}
fn limits() -> LoopLimits {
    LoopLimits {
        max_iterations: 8,
        max_wall_ms: 1000,
        max_cost_units: 100,
    }
}
fn context() -> CallCx {
    support::root_context(limits())
}
fn charge(cx: &CallCx, kind: ChargeKind) -> Result<(), StepError> {
    cx.budget()
        .ok_or_else(|| StepError::new("unavailable", "fixture execution budget missing"))?
        .charge(kind)
        .map(|_| ())
        .map_err(|error| StepError::new(error.code(), error.to_string()))
}
fn flow(steps: Vec<Step>) -> ResolvedFlow {
    ResolvedFlow {
        owner: PackId::parse("local.fixture").unwrap(),
        hash: PackHash::new(Digest::hash(b"fixture")),
        document: FlowDocument {
            format: "tobkiri.flow/1".into(),
            id: "test".into(),
            implements: Some(support::flow_shape()),
            trigger: Trigger { input: shape() },
            loop_limits: "profile-default".into(),
            steps,
            on_error: ErrorPolicy {
                default: ErrorAction::Fail,
                steps: BTreeMap::new(),
            },
        },
        ordered_overlays: BTreeMap::new(),
        call_witnesses: BTreeMap::new(),
        plan_digest: Digest::hash(b"frozen"),
    }
}

trait FixtureRun {
    fn run_fixture(
        &self,
        cx: CallCx,
        plan: Arc<ResolvedFlow>,
        input: CoreValue,
        limits: LoopLimits,
    ) -> Result<tobkiri_flow::RunHandle, StepError>;
}

impl FixtureRun for FlowEngine {
    fn run_fixture(
        &self,
        cx: CallCx,
        plan: Arc<ResolvedFlow>,
        input: CoreValue,
        limits: LoopLimits,
    ) -> Result<tobkiri_flow::RunHandle, StepError> {
        let profile = support::compiled_profile(std::slice::from_ref(&*plan));
        let key = support::flow_key(&profile, &plan);
        let flow = support::flow_handle(&profile, &key, cx.control());
        support::run_fixture(self, cx, &flow, input, limits)
    }
}

fn run_raw_fixture(
    engine: &FlowEngine,
    cx: CallCx,
    plan: Arc<ResolvedFlow>,
    input: CoreValue,
    limits: LoopLimits,
) -> Result<tobkiri_flow::RunHandle, StepError> {
    let flow = support::raw_flow_handle(&cx, (*plan).clone());
    let frame = support::raw_frame(&cx, &flow);
    engine.run(frame, flow, input, limits)
}

fn run_with_uncompiled_overlay(
    engine: &FlowEngine,
    cx: CallCx,
    plan: ResolvedFlow,
    input: CoreValue,
    limits: LoopLimits,
) -> Result<tobkiri_flow::RunHandle, StepError> {
    let overlays = plan.ordered_overlays.clone();
    let mut base = plan;
    base.ordered_overlays.clear();
    let profile = support::compiled_profile(&[base.clone()]);
    let key = support::flow_key(&profile, &base);
    let mut resolved = profile.flows[&key].clone();
    resolved.ordered_overlays = overlays;
    let flow = support::raw_flow_handle(&cx, resolved);
    let frame = support::raw_frame(&cx, &flow);
    engine.run(frame, flow, input, limits)
}
fn overlay(body: Vec<Step>) -> ResolvedOverlay {
    let document = OverlayDocument {
        format: "tobkiri.overlay/1".into(),
        id: "user-change".into(),
        target: OverlayTarget::Step {
            flow: "test".into(),
            step: "base".into(),
        },
        order: OrderConstraints::default(),
        body,
    };
    let bytes = serde_json::to_vec(&document).expect("fixture overlay serializes");
    let canonical = tobkiri_canon::parse(&bytes, tobkiri_canon::Variant::StrictV1)
        .expect("fixture overlay is canonical");
    let ast_hash = tobkiri_canon::digest(&canonical, tobkiri_canon::Variant::StrictV1)
        .expect("fixture overlay hashes");
    ResolvedOverlay {
        owner: None,
        hash: None,
        document,
        ast_hash,
    }
}

#[derive(Default)]
struct Journal(Mutex<Vec<RunEvent>>);
#[async_trait]
impl RunJournal for Journal {
    async fn append(
        &self,
        _: &CallCx,
        event: &RunEvent,
        _: &Arc<dyn tobkiri_canon::AllocationBudget>,
    ) -> Result<(), StepError> {
        self.0.lock().unwrap().push(event.clone());
        Ok(())
    }
}

#[derive(Default)]
struct DelayedTerminalJournal {
    entered: tokio::sync::Notify,
    release: tokio::sync::Notify,
    terminal_appends: AtomicUsize,
}
#[async_trait]
impl RunJournal for DelayedTerminalJournal {
    async fn append(
        &self,
        _: &CallCx,
        event: &RunEvent,
        _: &Arc<dyn tobkiri_canon::AllocationBudget>,
    ) -> Result<(), StepError> {
        if matches!(
            event.kind,
            RunEventKind::Completed { .. }
                | RunEventKind::Failed { .. }
                | RunEventKind::Cancelled { .. }
        ) {
            self.entered.notify_one();
            self.release.notified().await;
            self.terminal_appends.fetch_add(1, Ordering::SeqCst);
        }
        Ok(())
    }
}

struct LostCompletionJournal;
#[async_trait]
impl RunJournal for LostCompletionJournal {
    async fn append(
        &self,
        _: &CallCx,
        event: &RunEvent,
        _: &Arc<dyn tobkiri_canon::AllocationBudget>,
    ) -> Result<(), StepError> {
        if matches!(
            event.kind,
            RunEventKind::Completed { .. }
                | RunEventKind::Failed { .. }
                | RunEventKind::Cancelled { .. }
        ) {
            panic!("fixture interpreter loses its completion sender");
        }
        Ok(())
    }
}

struct ExecutionDropJournal {
    start: Arc<tokio::sync::Notify>,
    dropping: Option<tokio::sync::oneshot::Sender<()>>,
    release: Mutex<std::sync::mpsc::Receiver<()>>,
}
#[async_trait]
impl RunJournal for ExecutionDropJournal {
    async fn append(
        &self,
        _: &CallCx,
        event: &RunEvent,
        _: &Arc<dyn tobkiri_canon::AllocationBudget>,
    ) -> Result<(), StepError> {
        if matches!(event.kind, RunEventKind::Started) {
            self.start.notified().await;
        }
        Ok(())
    }
}
impl Drop for ExecutionDropJournal {
    fn drop(&mut self) {
        if let Some(dropping) = self.dropping.take() {
            let _ = dropping.send(());
        }
        let _ = self.release.get_mut().unwrap().recv();
    }
}
struct ReleaseExecutionDrop(std::sync::mpsc::Sender<()>);
impl Drop for ReleaseExecutionDrop {
    fn drop(&mut self) {
        // Release the blocked runtime worker even if an assertion panics.
        let _ = self.0.send(());
    }
}

#[derive(Default)]
struct Fake {
    calls: Mutex<Vec<(String, CoreValue, Principal, Instant)>>,
    emitted: Mutex<Vec<CoreValue>>,
    reached: Mutex<Vec<(String, CodeOwner, FrozenTarget)>>,
    active: AtomicUsize,
    max_active: AtomicUsize,
    stream_cancelled: Arc<AtomicUsize>,
    stream_closed: Arc<AtomicUsize>,
    stream_pending_entered: Arc<tokio::sync::Notify>,
    stream_pending_finished: Arc<AtomicUsize>,
    stream_close_gate: Option<Arc<StreamCloseGate>>,
}
#[derive(Default)]
struct StreamCloseGate {
    entered: tokio::sync::Notify,
    release: tokio::sync::Notify,
}
#[derive(Default)]
struct SourcePullClose {
    cancelled: tokio_util::sync::CancellationToken,
}
impl StreamCloseControl for SourcePullClose {
    fn record_close(&self, _: StreamCloseReason) -> Result<(), StepError> {
        // This companion belongs to exactly one Source. It wakes its retained
        // pull; it neither closes that source nor certifies physical cleanup.
        self.cancelled.cancel();
        Ok(())
    }
}
struct Source {
    values: Vec<CoreValue>,
    position: usize,
    cancelled: Arc<AtomicUsize>,
    closed: Arc<AtomicUsize>,
    pending: bool,
    fail_close: bool,
    close_gate: Option<Arc<StreamCloseGate>>,
    pull_close: Arc<SourcePullClose>,
    pending_entered: Arc<tokio::sync::Notify>,
    pending_finished: Arc<AtomicUsize>,
}
#[async_trait]
impl StreamSource for Source {
    async fn pull(&mut self, max: u32) -> Result<CoreValue, StepError> {
        if self.pending && self.position > 0 {
            self.pending_entered.notify_one();
            self.pull_close.cancelled.cancelled().await;
            self.pending_finished.fetch_add(1, Ordering::SeqCst);
            return Err(StepError::new(
                "cancelled",
                "fixture pending pull was closed",
            ));
        }
        let end = (self.position + max.min(1) as usize).min(self.values.len());
        let mut batch = CoreValue::array_builder(&support::budget(), end - self.position)
            .map_err(|_| StepError::new("unavailable", "fixture stream allocation failed"))?;
        for value in &self.values[self.position..end] {
            batch
                .push(value.share())
                .map_err(|_| StepError::new("unavailable", "fixture stream allocation failed"))?;
        }
        self.position = end;
        batch
            .finish()
            .map_err(|_| StepError::new("unavailable", "fixture stream allocation failed"))
    }
    fn cancel(&mut self) {
        self.cancelled.fetch_add(1, Ordering::SeqCst);
        self.pull_close.cancelled.cancel();
    }
    fn close_control(&self) -> Option<Arc<dyn StreamCloseControl>> {
        Some(self.pull_close.clone())
    }
    async fn close(&mut self) -> Result<(), StepError> {
        self.closed.fetch_add(1, Ordering::SeqCst);
        self.cancel();
        if let Some(gate) = &self.close_gate {
            gate.entered.notify_one();
            gate.release.notified().await;
        }
        if self.fail_close {
            Err(StepError::new("internal", "fixture cleanup failed"))
        } else {
            Ok(())
        }
    }
}
#[async_trait]
impl PortInvoker for Fake {
    async fn call(
        &self,
        cx: &CallCx,
        _: &ShapeRef,
        op: &OpName,
        args: CoreValue,
    ) -> Result<StepOutput, StepError> {
        charge(cx, ChargeKind::PortDispatch)?;
        self.calls.lock().unwrap().push((
            op.to_string(),
            args.clone(),
            cx.principal().clone(),
            cx.deadline(),
        ));
        match op.as_str() {
            "fail" => Err(StepError::new("unavailable", "provider failed")),
            "trap" => panic!("isolated guest simulation"),
            "increment" => {
                if let Some(n) = args.as_i64() {
                    let next = n
                        .checked_add(1)
                        .ok_or_else(|| StepError::new("limit-exceeded", "integer overflow"))?;
                    Ok(StepOutput::Value(
                        CoreValue::int(next, &support::budget()).map_err(|_| {
                            StepError::new("unavailable", "fixture allocation failed")
                        })?,
                    ))
                } else {
                    Err(StepError::new("invalid", "integer required"))
                }
            }
            "delay" => {
                let active = self.active.fetch_add(1, Ordering::SeqCst) + 1;
                self.max_active.fetch_max(active, Ordering::SeqCst);
                let delay = if let Some(n) = args.as_i64() {
                    n as u64
                } else {
                    10
                };
                tokio::time::sleep(Duration::from_millis(delay)).await;
                self.active.fetch_sub(1, Ordering::SeqCst);
                Ok(StepOutput::Value(args))
            }
            "stream" | "pending-stream" | "cleanup-fails" | "growth-stream" => {
                Ok(StepOutput::Stream(CoreStream::new(Source {
                    values: if op.as_str() == "growth-stream" {
                        (0..9).map(|_| owned_json("0")).collect()
                    } else {
                        vec![
                            owned_json("{\"kind\":\"delta\",\"text\":\"a\"}"),
                            owned_json("{\"kind\":\"done\",\"text\":\"ab\"}"),
                        ]
                    },
                    position: 0,
                    cancelled: self.stream_cancelled.clone(),
                    closed: self.stream_closed.clone(),
                    pending: op.as_str() == "pending-stream",
                    fail_close: op.as_str() == "cleanup-fails",
                    close_gate: self.stream_close_gate.clone(),
                    pull_close: Arc::new(SourcePullClose::default()),
                    pending_entered: self.stream_pending_entered.clone(),
                    pending_finished: self.stream_pending_finished.clone(),
                })))
            }
            _ => Ok(StepOutput::Value(args)),
        }
    }
    async fn emit(&self, cx: &CallCx, _: &ShapeRef, event: CoreValue) -> Result<(), StepError> {
        charge(cx, ChargeKind::ForwardedFrame)?;
        self.emitted.lock().unwrap().push(event);
        Ok(())
    }

    async fn call_reached(
        &self,
        cx: &CallCx,
        frame: &ControlHandle<dyn ActiveCodeFrame>,
        node: &ReachedNode,
        arguments_digest: &Digest,
        args: CoreValue,
    ) -> Result<StepOutput, StepError> {
        let witness = support::check_reached_call(frame, node)?;
        let actual_digest = args
            .digest_guest(&support::budget())
            .map_err(|error| StepError::new("limit-exceeded", error.to_string()))?;
        if &actual_digest != arguments_digest {
            return Err(StepError::new(
                "denied",
                "test dispatch arguments do not match their digest",
            ));
        }
        self.reached.lock().unwrap().push((
            witness.op().to_string(),
            frame.code_owner().clone(),
            witness.targets()[0].clone(),
        ));
        self.call(cx, witness.shape(), witness.op(), args).await
    }

    async fn emit_reached(
        &self,
        cx: &CallCx,
        frame: &ControlHandle<dyn ActiveCodeFrame>,
        node: &ReachedNode,
        event_digest: &Digest,
        event: CoreValue,
    ) -> Result<(), StepError> {
        let witness = support::check_reached_call(frame, node)?;
        let actual_digest = event
            .digest_guest(&support::budget())
            .map_err(|error| StepError::new("limit-exceeded", error.to_string()))?;
        if &actual_digest != event_digest || witness.op().as_str() != "emit" {
            return Err(StepError::new(
                "denied",
                "test stream frame does not match its compiler witness",
            ));
        }
        self.reached.lock().unwrap().push((
            witness.op().to_string(),
            frame.code_owner().clone(),
            witness.targets()[0].clone(),
        ));
        self.emit(cx, witness.shape(), event).await
    }
}

struct NeverRunSubflow;

#[async_trait]
impl FlowRunner for NeverRunSubflow {
    async fn run_provided_flow(
        &self,
        _: &CallCx,
        _: &ControlHandle<dyn ActiveCodeFrame>,
        _: &ReachedNode,
        _: ControlHandle<ResolvedFlow>,
        _: CoreValue,
    ) -> Result<CoreStream, StepError> {
        Err(StepError::new(
            "unavailable",
            "ambiguous subflow fixture must not be entered",
        ))
    }
}

async fn finish(
    run: Result<tobkiri_flow::RunHandle, StepError>,
) -> (Result<CoreValue, StepError>, Vec<RunEvent>) {
    let mut run = match run {
        Ok(run) => run,
        Err(error) => return (Err(error), Vec::new()),
    };
    let mut events = Vec::new();
    while let Some(event) = run.events.recv().await {
        events.push(event);
    }
    let result = run.result.await.and_then(|buffer| {
        let canonical = tobkiri_canon::parse(buffer.as_bytes(), tobkiri_canon::Variant::StrictV1)
            .map_err(|error| StepError::new("invalid", error.to_string()))?;
        CoreValue::try_from_borrowed(&canonical, &support::budget())
            .map_err(|error| StepError::new("limit-exceeded", error.to_string()))
    });
    (result, events)
}

#[tokio::test]
async fn stream_is_teed_to_output_and_collect_without_bypassing_flow() {
    let fake = Arc::new(Fake::default());
    let journal = Arc::new(Journal::default());
    let mut generate = call("generate", "stream", value("{}"), None);
    generate.stream = Some(StreamRoute {
        emit: shape(),
        collect: Some("turn".into()),
    });
    let plan = Arc::new(flow(vec![
        generate,
        call("save", "echo", value("{\"events\":\"$turn\"}"), None),
    ]));
    let (result, events) = finish(FlowEngine::new(fake.clone(), journal.clone()).run_fixture(
        // Production example Profiles allow 4 MiB of serialized output. This
        // small stream must not reserve that many Node references up front.
        support::root_context_with_output_bytes(limits(), 4 * 1024 * 1024),
        plan.clone(),
        owned_json("{}"),
        limits(),
    ))
    .await;
    assert_eq!(fake.emitted.lock().unwrap().len(), 2);
    assert_eq!(
        canonical(&result.unwrap()),
        value(
            "{\"events\":[{\"kind\":\"delta\",\"text\":\"a\"},{\"kind\":\"done\",\"text\":\"ab\"}]}"
        )
    );
    let event_plan = events.first().unwrap().plan_digest.clone();
    assert!(
        events.iter().enumerate().all(|(i, event)| {
            event.sequence == i as u64 + 1 && event.plan_digest == event_plan
        })
    );
    assert_eq!(journal.0.lock().unwrap().len(), events.len());
    assert!(fake.stream_cancelled.load(Ordering::SeqCst) > 0);
    let reached = fake.reached.lock().unwrap();
    assert_eq!(reached.len(), 4);
    assert_eq!(reached.iter().filter(|(op, _, _)| op == "emit").count(), 2);
    assert!(reached.iter().all(|(_, _, target)| {
        target.pack.as_str() == "local.provider" && target.provided == shape()
    }));
}

struct SequenceCeiling {
    backing: support::TestBudget,
    maximum: u64,
    denied: AtomicUsize,
}
impl AllocationBudget for SequenceCeiling {
    fn reserve(&self, request: AllocationRequest) -> Result<(), AllocationError> {
        if request.kind == AllocationKind::Sequence
            && request.bytes > 0
            && request.elements > self.maximum
        {
            self.denied.fetch_add(1, Ordering::SeqCst);
            return Err(AllocationError::Limit);
        }
        self.backing.reserve(request)
    }
    fn release(&self, bytes: u64) {
        self.backing.release(bytes);
    }
}
fn run_with_collection_budget(
    engine: &FlowEngine,
    cx: CallCx,
    plan: ResolvedFlow,
    allocation: Arc<dyn AllocationBudget>,
) -> Result<tobkiri_flow::RunHandle, StepError> {
    let profile = support::compiled_profile(std::slice::from_ref(&plan));
    let key = support::flow_key(&profile, &plan);
    let flow = support::flow_handle(&profile, &key, cx.control());
    let frame = support::active_frame_with_budget(&cx, &flow, Some(allocation));
    engine.run(frame, flow, owned(CanonValue::Null), limits())
}
fn collection_step(op: &str) -> Step {
    let mut generate = call("generate", op, CanonValue::Null, None);
    generate.stream = Some(StreamRoute {
        emit: shape(),
        collect: Some("turn".into()),
    });
    generate
}

#[tokio::test]
async fn collection_initial_admission_denial_never_enters_provider() {
    // Three slots admit the ordinary input binding, but deny the collection's
    // distinct eight-slot initial reservation before the provider is entered.
    let allocation = Arc::new(SequenceCeiling {
        backing: support::TestBudget::new(),
        maximum: 3,
        denied: AtomicUsize::new(0),
    });
    let fake = Arc::new(Fake::default());
    let engine = FlowEngine::new(fake.clone(), Arc::new(Journal::default()));
    let (result, events) = finish(run_with_collection_budget(
        &engine,
        support::root_context_with_output_bytes(limits(), 4 * 1024 * 1024),
        flow(vec![collection_step("stream")]),
        allocation.clone(),
    ))
    .await;
    assert_eq!(result.unwrap_err().code, "limit-exceeded");
    assert_eq!(allocation.denied.load(Ordering::SeqCst), 1);
    assert!(fake.calls.lock().unwrap().is_empty());
    assert!(fake.emitted.lock().unwrap().is_empty());
    assert_eq!(fake.stream_cancelled.load(Ordering::SeqCst), 0);
    assert_eq!(fake.stream_closed.load(Ordering::SeqCst), 0);
    assert!(events.iter().any(|event| matches!(
        &event.kind,
        RunEventKind::StepFailed { step, error, .. }
            if step == "generate" && error.code == "limit-exceeded"
    )));
}

#[tokio::test]
async fn collection_growth_denial_awaits_actual_close_without_replaying_producer() {
    let allocation = Arc::new(SequenceCeiling {
        backing: support::TestBudget::new(),
        maximum: 8,
        denied: AtomicUsize::new(0),
    });
    let gate = Arc::new(StreamCloseGate::default());
    let fake = Arc::new(Fake {
        stream_close_gate: Some(gate.clone()),
        ..Fake::default()
    });
    let engine = FlowEngine::new(fake.clone(), Arc::new(Journal::default()));
    let cx = support::root_context_with_output_bytes(limits(), 4 * 1024 * 1024);
    let control = cx.control().clone();
    let run = run_with_collection_budget(
        &engine,
        cx,
        flow(vec![collection_step("growth-stream")]),
        allocation.clone(),
    )
    .unwrap();
    let mut result = Box::pin(run.result);
    let mut events = run.events;
    tokio::time::timeout(Duration::from_secs(1), gate.entered.notified())
        .await
        .expect("growth denial reached the producer's real close operation");
    assert!(futures::poll!(&mut result).is_pending());
    // Both the interpreter and its owning actual-Join monitor are scheduled
    // tasks, independently admitted before either can start.
    assert_eq!(control.usage().count(ControlKind::Task), 2);
    assert!(control.usage().count(ControlKind::Frame) > 0);
    assert_eq!(allocation.denied.load(Ordering::SeqCst), 1);
    assert_eq!(fake.calls.lock().unwrap().len(), 1);
    assert_eq!(fake.stream_cancelled.load(Ordering::SeqCst), 1);
    assert_eq!(fake.stream_closed.load(Ordering::SeqCst), 1);
    gate.release.notify_one();
    while events.recv().await.is_some() {}
    assert_eq!(result.await.unwrap_err().code, "limit-exceeded");
    assert_eq!(control.usage().count(ControlKind::Task), 0);
    assert_eq!(control.usage().count(ControlKind::Frame), 0);
    assert_eq!(fake.calls.lock().unwrap().len(), 1);
    assert!(fake.stream_cancelled.load(Ordering::SeqCst) >= 1);
    assert_eq!(fake.stream_closed.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn collection_encoding_overhead_denial_closes_started_producer() {
    let fake = Arc::new(Fake::default());
    // Both individual events fit the run's raw output allowance together, but
    // their collection additionally requires brackets and a separating comma.
    let raw_bytes =
        br#"{"kind":"delta","text":"a"}"#.len() + br#"{"kind":"done","text":"ab"}"#.len();
    let (result, events) = finish(
        FlowEngine::new(fake.clone(), Arc::new(Journal::default())).run_fixture(
            support::root_context_with_output_bytes(limits(), raw_bytes as u64),
            Arc::new(flow(vec![collection_step("stream")])),
            owned(CanonValue::Null),
            limits(),
        ),
    )
    .await;
    assert_eq!(result.unwrap_err().code, "limit-exceeded");
    assert_eq!(fake.calls.lock().unwrap().len(), 1);
    assert_eq!(fake.emitted.lock().unwrap().len(), 2);
    assert!(fake.stream_cancelled.load(Ordering::SeqCst) >= 1);
    assert_eq!(fake.stream_closed.load(Ordering::SeqCst), 1);
    assert!(events.iter().any(|event| matches!(
        &event.kind,
        RunEventKind::StepFailed { step, error, .. }
            if step == "generate" && error.code == "limit-exceeded"
    )));
}

#[tokio::test]
async fn overlay_next_wraps_once_and_preserves_original_deadline() {
    let fake = Arc::new(Fake::default());
    let mut plan = flow(vec![call("base", "echo", value("\"$input\""), None)]);
    plan.ordered_overlays.insert(
        "base".into(),
        vec![overlay(vec![
            call("inspect", "echo", value("\"$input\""), Some("checked")),
            step(
                "next",
                StepKind::Next(Next {
                    args: value("\"changed\""),
                }),
                Some("r"),
            ),
            step("return", StepKind::Return(value("{\"ok\":\"$r\"}")), None),
        ])],
    );
    let cx = context();
    let deadline = cx.deadline();
    let (result, _) = finish(
        FlowEngine::new(fake.clone(), Arc::new(Journal::default())).run_fixture(
            cx,
            Arc::new(plan),
            owned_json("\"original\""),
            limits(),
        ),
    )
    .await;
    assert_eq!(canonical(&result.unwrap()), value("\"changed\""));
    let calls = fake.calls.lock().unwrap();
    assert_eq!(calls.len(), 2);
    assert!(matches!(calls[0].2, Principal::User));
    assert!(matches!(calls[1].2, Principal::Pack { .. }));
    assert!(calls.iter().all(|c| c.3 == deadline));
    let reached = fake.reached.lock().unwrap();
    assert!(matches!(reached[0].1, CodeOwner::User));
    assert!(matches!(reached[1].1, CodeOwner::Pack { .. }));
}

#[tokio::test]
async fn calling_next_twice_fails_after_only_one_original_dispatch() {
    let fake = Arc::new(Fake::default());
    let mut plan = flow(vec![call("base", "echo", CanonValue::Null, None)]);
    plan.ordered_overlays.insert(
        "base".into(),
        vec![overlay(vec![
            step(
                "first",
                StepKind::Next(Next {
                    args: CanonValue::Null,
                }),
                None,
            ),
            step(
                "second",
                StepKind::Next(Next {
                    args: CanonValue::Null,
                }),
                None,
            ),
        ])],
    );
    let engine = FlowEngine::new(fake.clone(), Arc::new(Journal::default()));
    let (result, _) = finish(run_with_uncompiled_overlay(
        &engine,
        context(),
        plan,
        owned(CanonValue::Null),
        limits(),
    ))
    .await;
    assert_eq!(result.unwrap_err().code, "invalid");
    assert_eq!(fake.calls.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn replacement_and_deletion_never_dispatch_original_step() {
    for body in [
        vec![step(
            "return",
            StepKind::Return(value("{\"ok\":\"replacement\"}")),
            None,
        )],
        Vec::new(),
    ] {
        let fake = Arc::new(Fake::default());
        let mut plan = flow(vec![call("base", "echo", CanonValue::Null, None)]);
        plan.ordered_overlays
            .insert("base".into(), vec![overlay(body)]);
        let (result, _) = finish(
            FlowEngine::new(fake.clone(), Arc::new(Journal::default())).run_fixture(
                context(),
                Arc::new(plan),
                owned(CanonValue::Null),
                limits(),
            ),
        )
        .await;
        assert!(result.is_ok());
        assert!(fake.calls.lock().unwrap().is_empty());
    }
}

#[tokio::test]
async fn iteration_cost_and_wall_limits_stop_run() {
    for (body, bounds) in [
        (
            vec![call("work", "echo", CanonValue::Null, None)],
            LoopLimits {
                max_iterations: 2,
                ..limits()
            },
        ),
        (
            vec![call("work", "echo", CanonValue::Null, None)],
            LoopLimits {
                max_cost_units: 1,
                ..limits()
            },
        ),
        (
            vec![call("work", "delay", CanonValue::Int(100), None)],
            LoopLimits {
                max_wall_ms: 5,
                ..limits()
            },
        ),
    ] {
        let repeat = step(
            "repeat",
            StepKind::Loop(Loop {
                condition: CanonValue::Bool(true),
                body,
                limits: None,
            }),
            None,
        );
        let (result, _) = finish(
            FlowEngine::new(Arc::new(Fake::default()), Arc::new(Journal::default())).run_fixture(
                context(),
                Arc::new(flow(vec![repeat])),
                owned(CanonValue::Null),
                bounds,
            ),
        )
        .await;
        assert_eq!(result.unwrap_err().code, "limit-exceeded");
    }
}

#[tokio::test]
async fn loop_cannot_widen_defaults_or_run_without_profile_limits() {
    let repeat = step(
        "repeat",
        StepKind::Loop(Loop {
            condition: CanonValue::Bool(true),
            body: Vec::new(),
            limits: Some(LoopLimits {
                max_iterations: 9,
                ..limits()
            }),
        }),
        None,
    );
    let engine = FlowEngine::new(Arc::new(Fake::default()), Arc::new(Journal::default()));
    let cx = context();
    let (result, _) = finish(run_raw_fixture(
        &engine,
        cx.clone(),
        Arc::new(flow(vec![repeat])),
        owned(CanonValue::Null),
        limits(),
    ))
    .await;
    assert_eq!(result.unwrap_err().code, "invalid");
    let (result, _) = finish(run_raw_fixture(
        &engine,
        cx,
        Arc::new(flow(Vec::new())),
        owned(CanonValue::Null),
        LoopLimits::default(),
    ))
    .await;
    assert_eq!(result.unwrap_err().code, "invalid");
}

#[tokio::test]
async fn parallel_is_bounded_and_collected_in_input_order() {
    let fake = Arc::new(Fake::default());
    let parallel = step(
        "parallel",
        StepKind::Parallel(Parallel {
            for_each: value("[30,5,10,1]"),
            binding: "item".into(),
            max: 2,
            body: vec![call("work", "delay", value("\"$item\""), None)],
        }),
        None,
    );
    let (result, _) = finish(
        FlowEngine::new(fake.clone(), Arc::new(Journal::default())).run_fixture(
            context(),
            Arc::new(flow(vec![parallel])),
            owned(CanonValue::Null),
            limits(),
        ),
    )
    .await;
    assert_eq!(canonical(&result.unwrap()), value("[30,5,10,1]"));
    assert_eq!(fake.max_active.load(Ordering::SeqCst), 2);
}

#[tokio::test]
async fn provider_trap_is_a_step_error_and_engine_runs_again() {
    let fake = Arc::new(Fake::default());
    let engine = FlowEngine::new(fake, Arc::new(Journal::default()));
    let mut plan = flow(vec![
        call("bad", "trap", CanonValue::Null, None),
        call("good", "echo", value("\"alive\""), None),
    ]);
    plan.document
        .on_error
        .steps
        .insert("bad".into(), ErrorAction::Continue);
    let (result, events) =
        finish(engine.run_fixture(context(), Arc::new(plan), owned(CanonValue::Null), limits()))
            .await;
    assert_eq!(canonical(&result.unwrap()), value("\"alive\""));
    assert!(events.iter().any(
        |e| matches!(&e.kind,RunEventKind::StepFailed{error,continued:true,..}if error.code=="trap")
    ));
    let (result, _) = finish(engine.run_fixture(
        context(),
        Arc::new(flow(vec![call(
            "another",
            "echo",
            CanonValue::Bool(true),
            None,
        )])),
        owned(CanonValue::Null),
        limits(),
    ))
    .await;
    assert_eq!(canonical(&result.unwrap()), CanonValue::Bool(true));
}

#[tokio::test]
async fn cancellation_releases_pending_stream_and_records_reason() {
    let fake = Arc::new(Fake::default());
    let mut generate = call("generate", "pending-stream", CanonValue::Null, None);
    generate.stream = Some(StreamRoute {
        emit: shape(),
        collect: Some("turn".into()),
    });
    let mut run = FlowEngine::new(fake.clone(), Arc::new(Journal::default()))
        .run_fixture(
            context(),
            Arc::new(flow(vec![generate])),
            owned(CanonValue::Null),
            limits(),
        )
        .unwrap();
    while let Some(event) = run.events.recv().await {
        if matches!(event.kind, RunEventKind::Emitted { .. }) {
            tokio::time::timeout(
                Duration::from_secs(1),
                fake.stream_pending_entered.notified(),
            )
            .await
            .expect("same source entered its retained pending pull");
            run.cancel.cancel();
            break;
        }
    }
    let (result, events) = finish(Ok(run)).await;
    assert_eq!(result.unwrap_err().code, "cancelled");
    assert!(fake.stream_cancelled.load(Ordering::SeqCst) > 0);
    assert_eq!(fake.stream_pending_finished.load(Ordering::SeqCst), 1);
    assert_eq!(fake.stream_closed.load(Ordering::SeqCst), 1);
    assert!(
        events
            .iter()
            .any(|e| matches!(e.kind, RunEventKind::Cancelled { .. }))
    );
}

#[tokio::test]
async fn cancelled_run_stream_close_retains_completion_after_abandoned_wait() {
    let fake = Arc::new(Fake::default());
    let journal = Arc::new(DelayedTerminalJournal::default());
    let cx = context();
    let control = cx.control().clone();
    let mut generate = call("generate", "pending-stream", CanonValue::Null, None);
    generate.stream = Some(StreamRoute {
        emit: shape(),
        collect: Some("turn".into()),
    });
    let mut run = FlowEngine::new(fake.clone(), journal.clone())
        .run_fixture(
            cx,
            Arc::new(flow(vec![generate])),
            owned(CanonValue::Null),
            limits(),
        )
        .unwrap();
    loop {
        let event = run.events.recv().await.expect("running interpreter event");
        if matches!(event.kind, RunEventKind::Emitted { .. }) {
            break;
        }
    }

    tokio::time::timeout(
        Duration::from_secs(1),
        fake.stream_pending_entered.notified(),
    )
    .await
    .expect("same source entered its retained pending pull");
    let mut stream = run.into_stream();
    stream.cancel();
    tokio::time::timeout(Duration::from_secs(1), journal.entered.notified())
        .await
        .expect("cancelled interpreter reached its actual terminal journal");
    // The blocked interpreter and its actual-Join monitor remain funded even
    // when a close observer is abandoned.
    assert_eq!(control.usage().count(ControlKind::Task), 2);
    assert!(control.usage().count(ControlKind::Frame) > 0);

    let mut close = Box::pin(stream.close());
    assert!(futures::poll!(&mut close).is_pending());
    drop(close);
    assert_eq!(control.usage().count(ControlKind::Task), 2);
    assert_eq!(journal.terminal_appends.load(Ordering::SeqCst), 0);

    let mut close = Box::pin(stream.close());
    assert!(futures::poll!(&mut close).is_pending());
    journal.release.notify_one();
    tokio::time::timeout(Duration::from_secs(1), close)
        .await
        .expect("same interpreter completed")
        .unwrap();
    // The receipt must follow destruction of execution-owned frame and native
    // task/waiter custody, even while the closed stream observer remains alive.
    assert_eq!(control.usage().count(ControlKind::Task), 0);
    assert_eq!(control.usage().count(ControlKind::Waiter), 0);
    assert_eq!(control.usage().count(ControlKind::Frame), 0);
    stream.close().await.unwrap();
    assert_eq!(journal.terminal_appends.load(Ordering::SeqCst), 1);
    assert_eq!(fake.calls.lock().unwrap().len(), 1);
    assert!(fake.stream_cancelled.load(Ordering::SeqCst) > 0);
    assert_eq!(fake.stream_pending_finished.load(Ordering::SeqCst), 1);
    assert_eq!(fake.stream_closed.load(Ordering::SeqCst), 1);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn run_stream_completion_follows_actual_execution_custody_drop() {
    let start = Arc::new(tokio::sync::Notify::new());
    let (dropping, dropped) = tokio::sync::oneshot::channel();
    let (release, released) = std::sync::mpsc::channel();
    let release = ReleaseExecutionDrop(release);
    let cx = context();
    let control = cx.control().clone();
    // Once this temporary engine is gone, only the actual interpreter's
    // RunState owns the journal. Its destructor gates that state's real drop.
    let run = FlowEngine::new(
        Arc::new(Fake::default()),
        Arc::new(ExecutionDropJournal {
            start: start.clone(),
            dropping: Some(dropping),
            release: Mutex::new(released),
        }),
    )
    .run_fixture(
        cx,
        Arc::new(flow(vec![call("echo", "echo", CanonValue::Null, None)])),
        owned(CanonValue::Null),
        limits(),
    )
    .unwrap();
    let mut stream = run.into_stream();
    let mut close = Box::pin(stream.close());
    assert!(futures::poll!(&mut close).is_pending());
    start.notify_one();
    tokio::time::timeout(Duration::from_secs(1), dropped)
        .await
        .expect("interpreter reached destruction of its real RunState")
        .unwrap();
    // Execution destruction has not finished; retain both the interpreter's
    // Task reservation and the monitor that owns that exact Join.
    assert_eq!(control.usage().count(ControlKind::Task), 2);
    assert!(futures::poll!(&mut close).is_pending());
    release.0.send(()).unwrap();
    tokio::time::timeout(Duration::from_secs(1), close)
        .await
        .expect("interpreter released execution custody")
        .unwrap();
    assert_eq!(control.usage().count(ControlKind::Task), 0);
    assert_eq!(control.usage().count(ControlKind::Waiter), 0);
    assert_eq!(control.usage().count(ControlKind::Frame), 0);
}

#[tokio::test]
async fn run_stream_close_preserves_actual_interpreter_failure() {
    let fake = Arc::new(Fake::default());
    let journal = Arc::new(DelayedTerminalJournal::default());
    let run = FlowEngine::new(fake.clone(), journal.clone())
        .run_fixture(
            context(),
            Arc::new(flow(vec![call("fail", "fail", CanonValue::Null, None)])),
            owned(CanonValue::Null),
            limits(),
        )
        .unwrap();
    tokio::time::timeout(Duration::from_secs(1), journal.entered.notified())
        .await
        .expect("failed interpreter reached its actual terminal journal");
    let mut stream = run.into_stream();
    let mut close = Box::pin(stream.close());
    assert!(futures::poll!(&mut close).is_pending());
    journal.release.notify_one();
    let error = tokio::time::timeout(Duration::from_secs(1), close)
        .await
        .expect("failed interpreter completed")
        .unwrap_err();
    assert_eq!(error.code, "unavailable");
    assert_eq!(stream.close().await.unwrap_err(), error);
    assert_eq!(journal.terminal_appends.load(Ordering::SeqCst), 1);
    assert_eq!(fake.calls.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn run_stream_close_preserves_lost_interpreter_completion() {
    let run = FlowEngine::new(Arc::new(Fake::default()), Arc::new(LostCompletionJournal))
        .run_fixture(
            context(),
            Arc::new(flow(vec![call("echo", "echo", CanonValue::Null, None)])),
            owned(CanonValue::Null),
            limits(),
        )
        .unwrap();
    let mut stream = run.into_stream();
    let error = tokio::time::timeout(Duration::from_secs(1), stream.close())
        .await
        .expect("interpreter dropped its completion sender")
        .unwrap_err();
    assert_eq!(error.code, "internal");
    assert_eq!(error.message, "Flow completion channel closed");
    assert_eq!(stream.close().await.unwrap_err(), error);
}

#[tokio::test]
async fn branches_and_subflows_follow_implements_in_frozen_catalog() {
    let fake = Arc::new(Fake::default());
    let mut sub = flow(vec![call("echo", "echo", value("\"$input\""), None)]);
    sub.owner = PackId::parse("local.other").unwrap();
    sub.document.id = "child".into();
    sub.document.implements = Some(support::child_flow_shape());
    let branch = step(
        "branch",
        StepKind::Branch(Branch {
            condition: value("{\"eq\":[\"$input\",true]}"),
            then: vec![step(
                "child",
                StepKind::Flow(SubFlow {
                    implements: support::child_flow_shape(),
                    args: value("\"yes\""),
                }),
                None,
            )],
            otherwise: vec![call("no", "echo", value("\"no\""), None)],
        }),
        None,
    );
    let parent = flow(vec![branch]);
    let profile = support::compiled_profile(&[parent.clone(), sub.clone()]);
    let parent_key = support::flow_key(&profile, &parent);
    let sub_key = support::flow_key(&profile, &sub);
    let cx = context();
    let parent_handle = support::flow_handle(&profile, &parent_key, cx.control());
    let sub_handle = support::flow_handle(&profile, &sub_key, cx.control());
    let engine = Arc::new(
        FlowEngine::new(fake.clone(), Arc::new(Journal::default()))
            .with_flows(BTreeMap::from([("child".into(), sub_handle)])),
    );
    struct Runner {
        engine: Arc<FlowEngine>,
    }
    #[async_trait]
    impl FlowRunner for Runner {
        async fn run_provided_flow(
            &self,
            cx: &CallCx,
            _: &ControlHandle<dyn ActiveCodeFrame>,
            _: &ReachedNode,
            target: ControlHandle<ResolvedFlow>,
            input: CoreValue,
        ) -> Result<CoreStream, StepError> {
            charge(cx, ChargeKind::SubFlow)?;
            let frame = support::active_frame_for_provided(cx, &target);
            self.engine.run_intermediate(frame, target, input, limits())
        }
    }
    let runner: Arc<dyn FlowRunner> = Arc::new(Runner {
        engine: engine.clone(),
    });
    engine.set_flow_runner(Arc::downgrade(&runner)).unwrap();
    let (result, _) = finish(engine.run(
        support::active_frame(&cx, &parent_handle),
        parent_handle,
        owned(CanonValue::Bool(true)),
        limits(),
    ))
    .await;
    assert_eq!(canonical(&result.unwrap()), value("\"yes\""));
    assert!(
        matches!(&fake.calls.lock().unwrap()[0].2,Principal::Pack{pack,..}if pack.as_str()=="local.other")
    );
}

#[tokio::test]
async fn narrower_loop_cost_stops_before_the_next_effect() {
    let fake = Arc::new(Fake::default());
    let repeat = step(
        "repeat",
        StepKind::Loop(Loop {
            condition: CanonValue::Bool(true),
            body: vec![
                call("one", "echo", CanonValue::Null, None),
                call("two", "echo", CanonValue::Null, None),
            ],
            limits: Some(LoopLimits {
                max_cost_units: 1,
                ..limits()
            }),
        }),
        None,
    );
    let (result, _) = finish(
        FlowEngine::new(fake.clone(), Arc::new(Journal::default())).run_fixture(
            context(),
            Arc::new(flow(vec![repeat])),
            owned(CanonValue::Null),
            limits(),
        ),
    )
    .await;
    assert_eq!(result.unwrap_err().code, "limit-exceeded");
    assert_eq!(fake.calls.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn original_deadline_stops_a_pending_call_without_extending_it() {
    let fake = Arc::new(Fake::default());
    let plan = flow(vec![call("wait", "delay", CanonValue::Int(500), None)]);
    let profile = support::compiled_profile(std::slice::from_ref(&plan));
    let key = support::flow_key(&profile, &plan);
    let cx = support::root_context_with_timeout(limits(), Duration::from_millis(100));
    let flow = support::flow_handle(&profile, &key, cx.control());
    let (result, events) = finish(FlowEngine::new(fake, Arc::new(Journal::default())).run(
        support::active_frame(&cx, &flow),
        flow,
        owned(CanonValue::Null),
        limits(),
    ))
    .await;
    assert_eq!(result.unwrap_err().code, "cancelled");
    assert!(
        events
            .iter()
            .any(|event| matches!(event.kind, RunEventKind::Cancelled { .. }))
    );
}

#[tokio::test]
async fn parallel_work_inside_overlay_can_wrap_the_original_step() {
    let fake = Arc::new(Fake::default());
    let mut plan = flow(vec![call("base", "echo", CanonValue::Null, None)]);
    let parallel = step(
        "inspect",
        StepKind::Parallel(Parallel {
            for_each: value("[1,2]"),
            binding: "item".into(),
            max: 2,
            body: vec![call("branch", "echo", value("\"$item\""), None)],
        }),
        Some("parts"),
    );
    plan.ordered_overlays.insert(
        "base".into(),
        vec![overlay(vec![
            parallel,
            step(
                "continue",
                StepKind::Next(Next {
                    args: value("\"$parts\""),
                }),
                Some("r"),
            ),
            step("result", StepKind::Return(value("{\"ok\":\"$r\"}")), None),
        ])],
    );
    let (result, _) = finish(
        FlowEngine::new(fake.clone(), Arc::new(Journal::default())).run_fixture(
            context(),
            Arc::new(plan),
            owned(CanonValue::Null),
            limits(),
        ),
    )
    .await;
    assert_eq!(canonical(&result.unwrap()), value("[1,2]"));
    assert_eq!(fake.calls.lock().unwrap().len(), 3);
}

#[tokio::test]
async fn parallel_overlay_branch_cannot_dispatch_next() {
    let fake = Arc::new(Fake::default());
    let mut plan = flow(vec![call("base", "echo", CanonValue::Null, None)]);
    let parallel = step(
        "parallel",
        StepKind::Parallel(Parallel {
            for_each: value("[1,2]"),
            binding: "item".into(),
            max: 2,
            body: vec![step(
                "next",
                StepKind::Next(Next {
                    args: CanonValue::Null,
                }),
                None,
            )],
        }),
        None,
    );
    plan.ordered_overlays
        .insert("base".into(), vec![overlay(vec![parallel])]);
    let engine = FlowEngine::new(fake.clone(), Arc::new(Journal::default()));
    let (result, _) = finish(run_with_uncompiled_overlay(
        &engine,
        context(),
        plan,
        owned(CanonValue::Null),
        limits(),
    ))
    .await;
    assert_eq!(result.unwrap_err().code, "invalid");
    assert!(fake.calls.lock().unwrap().is_empty());
}

#[tokio::test]
async fn return_cannot_contain_both_success_and_error() {
    let fake = Arc::new(Fake::default());
    let mut plan = flow(vec![call("base", "echo", CanonValue::Null, None)]);
    plan.ordered_overlays.insert(
        "base".into(),
        vec![overlay(vec![step(
            "return",
            StepKind::Return(value(
                "{\"ok\":true,\"error\":{\"code\":\"blocked\",\"message\":\"no\"}}",
            )),
            None,
        )])],
    );
    let (result, _) = finish(
        FlowEngine::new(fake.clone(), Arc::new(Journal::default())).run_fixture(
            context(),
            Arc::new(plan),
            owned(CanonValue::Null),
            limits(),
        ),
    )
    .await;
    assert_eq!(result.unwrap_err().code, "invalid");
    assert!(fake.calls.lock().unwrap().is_empty());
}

#[tokio::test]
async fn overlay_error_strings_from_guest_values_are_closed_before_lifting() {
    let fake = Arc::new(Fake::default());
    let mut plan = flow(vec![call("base", "echo", value("\"$input\""), None)]);
    plan.ordered_overlays.insert(
        "base".into(),
        vec![overlay(vec![step(
            "return",
            StepKind::Return(value(
                "{\"error\":{\"code\":\"$input.code\",\"message\":\"$input.message\"}}",
            )),
            None,
        )])],
    );
    let input = owned(value(
        "{\"code\":\"attacker-controlled-code\",\"message\":\"private guest payload\"}",
    ));
    let (result, _) = finish(
        FlowEngine::new(fake.clone(), Arc::new(Journal::default())).run_fixture(
            context(),
            Arc::new(plan),
            input,
            limits(),
        ),
    )
    .await;
    let error = result.unwrap_err();
    assert_eq!(error.code, "internal");
    assert_eq!(error.message, "overlay returned an error");
    assert!(!error.message.contains("private guest payload"));
    assert!(fake.calls.lock().unwrap().is_empty());
}

#[tokio::test]
async fn ambiguous_subflow_implements_never_selects_a_provider() {
    let fake = Arc::new(Fake::default());
    let first = Arc::new(flow(vec![call(
        "first",
        "echo",
        CanonValue::Bool(true),
        None,
    )]));
    let mut second = (*first).clone();
    second.owner = PackId::parse("local.second").unwrap();
    let cx = context();
    let first = support::raw_flow_handle(&cx, (*first).clone());
    let second = support::raw_flow_handle(&cx, second);
    let runner: Arc<dyn FlowRunner> = Arc::new(NeverRunSubflow);
    let engine =
        FlowEngine::new(fake.clone(), Arc::new(Journal::default())).with_flows(BTreeMap::from([
            ("first".into(), first),
            ("second".into(), second),
        ]));
    engine.set_flow_runner(Arc::downgrade(&runner)).unwrap();
    let base = flow(vec![step(
        "sub",
        StepKind::Flow(SubFlow {
            implements: support::flow_shape(),
            args: CanonValue::Null,
        }),
        None,
    )]);
    let base = support::raw_flow_handle(&cx, base);
    let (result, _) = finish(engine.run(
        support::raw_frame(&cx, &base),
        base,
        owned(CanonValue::Null),
        limits(),
    ))
    .await;
    assert_eq!(result.unwrap_err().code, "conflict");
    assert!(fake.calls.lock().unwrap().is_empty());
}

#[tokio::test]
async fn idless_effect_nodes_are_rejected_before_fake_dispatch() {
    let mut anonymous = call("unused", "fail", CanonValue::Null, None);
    anonymous.id = None;
    let mut plan = flow(vec![anonymous]);
    plan.document
        .on_error
        .steps
        .insert("anonymous".into(), ErrorAction::Continue);
    let fake = Arc::new(Fake::default());
    let engine = FlowEngine::new(fake.clone(), Arc::new(Journal::default()));
    let (result, _) = finish(run_raw_fixture(
        &engine,
        context(),
        Arc::new(plan),
        owned(CanonValue::Null),
        limits(),
    ))
    .await;
    assert_eq!(result.unwrap_err().code, "invalid");
    assert!(fake.calls.lock().unwrap().is_empty());
}

#[tokio::test]
async fn huge_loop_wall_ceiling_cannot_panic_the_runtime() {
    let repeat = step(
        "repeat",
        StepKind::Loop(Loop {
            condition: CanonValue::Bool(true),
            body: Vec::new(),
            limits: None,
        }),
        None,
    );
    let bounds = LoopLimits {
        max_wall_ms: u64::MAX,
        max_iterations: 1,
        ..limits()
    };
    let engine = FlowEngine::new(Arc::new(Fake::default()), Arc::new(Journal::default()));
    let (result, _) = finish(run_raw_fixture(
        &engine,
        context(),
        Arc::new(flow(vec![repeat])),
        owned(CanonValue::Null),
        bounds,
    ))
    .await;
    assert!(matches!(
        result.unwrap_err().code.as_str(),
        "invalid" | "limit-exceeded"
    ));
}

#[tokio::test]
async fn subflow_stream_events_reach_parent_observer_once() {
    let fake = Arc::new(Fake::default());
    let mut generate = call("generate", "stream", CanonValue::Null, None);
    generate.stream = Some(StreamRoute {
        emit: shape(),
        collect: None,
    });
    let mut child = flow(vec![generate]);
    child.document.id = "stream-child".into();
    child.document.implements = Some(support::child_flow_shape());
    let mut parent = flow(vec![step(
        "child",
        StepKind::Flow(SubFlow {
            implements: support::child_flow_shape(),
            args: CanonValue::Null,
        }),
        None,
    )]);
    parent.document.id = "stream-parent".into();
    let profile = support::compiled_profile(&[parent.clone(), child.clone()]);
    let parent_key = support::flow_key(&profile, &parent);
    let child_key = support::flow_key(&profile, &child);
    let cx = context();
    let parent_handle = support::flow_handle(&profile, &parent_key, cx.control());
    let child_handle = support::flow_handle(&profile, &child_key, cx.control());
    let engine = Arc::new(
        FlowEngine::new(fake.clone(), Arc::new(Journal::default()))
            .with_flows(BTreeMap::from([("child".into(), child_handle)])),
    );
    struct Runner {
        engine: Arc<FlowEngine>,
    }
    #[async_trait]
    impl FlowRunner for Runner {
        async fn run_provided_flow(
            &self,
            cx: &CallCx,
            _: &ControlHandle<dyn ActiveCodeFrame>,
            _: &ReachedNode,
            target: ControlHandle<ResolvedFlow>,
            input: CoreValue,
        ) -> Result<CoreStream, StepError> {
            charge(cx, ChargeKind::SubFlow)?;
            let frame = support::active_frame_for_provided(cx, &target);
            self.engine.run_intermediate(frame, target, input, limits())
        }
    }
    let runner: Arc<dyn FlowRunner> = Arc::new(Runner {
        engine: engine.clone(),
    });
    engine.set_flow_runner(Arc::downgrade(&runner)).unwrap();
    let (result, events) = finish(engine.run(
        support::active_frame(&cx, &parent_handle),
        parent_handle,
        owned(CanonValue::Null),
        limits(),
    ))
    .await;
    result.unwrap();
    assert_eq!(
        events
            .iter()
            .filter(|event| matches!(event.kind, RunEventKind::Emitted { .. }))
            .count(),
        2
    );
    assert_eq!(fake.emitted.lock().unwrap().len(), 2);
}

#[tokio::test]
async fn provider_stream_cleanup_failure_does_not_complete_the_flow() {
    let fake = Arc::new(Fake::default());
    let mut generate = call("generate", "cleanup-fails", CanonValue::Null, None);
    generate.stream = Some(StreamRoute {
        emit: shape(),
        collect: Some("turn".into()),
    });
    let plan = Arc::new(flow(vec![
        generate,
        call("save", "echo", CanonValue::Str("$turn".into()), None),
    ]));
    let (result, events) = finish(
        FlowEngine::new(fake.clone(), Arc::new(Journal::default())).run_fixture(
            context(),
            plan,
            owned(CanonValue::Null),
            limits(),
        ),
    )
    .await;
    assert_eq!(result.unwrap_err().code, "internal");
    assert_eq!(fake.calls.lock().unwrap().len(), 1);
    assert!(matches!(
        events.last().unwrap().kind,
        RunEventKind::Failed { .. }
    ));
    assert!(
        !events
            .iter()
            .any(|event| matches!(event.kind, RunEventKind::Completed { .. }))
    );
}
