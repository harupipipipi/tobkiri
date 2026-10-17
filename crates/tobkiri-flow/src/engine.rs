mod execution;

use std::{
    collections::BTreeMap,
    future::Future,
    panic::AssertUnwindSafe,
    pin::Pin,
    sync::{
        Arc, OnceLock, Weak,
        atomic::{AtomicU64, Ordering},
    },
    task::{Context, Poll},
    time::Instant,
};

use futures::{FutureExt, StreamExt, future::BoxFuture, stream};
use tobkiri_canon::{
    AllocationBudget, AllocationError, AllocationRequest, ControlHandle, CoreBuffer, CoreRef,
    CoreValue,
};
use tobkiri_gate::{
    BudgetContext, CallCx, CancellationToken, ControlKind, ControlReservation, CoreControl,
    LoopLimits,
};
use tobkiri_profile::{
    CodeLocation, CompiledProfile, ErrorAction, ResolvedFlow, ResolvedOverlay, Step, StepKind,
};
use tokio::sync::{Mutex, mpsc, oneshot};

use crate::{
    ActiveCodeFrame, AllocationReason, CoreStream, Environment, FlowNodeKind, FlowNodePosition,
    FlowRunner, PortInvoker, RunEvent, RunEventKind, RunJournal, StepError, StepOutput, evaluate,
    expression::boolean,
};
use crate::{FrameContinuation, OverlayPosition, ReachedNode};

const RUN_EVENT_QUEUE_CAPACITY: usize = 32;
const RUN_EVENT_METADATA_CONTROL_BYTES: u64 = 32 * 1024;
const MAX_RUN_EVENT_BUFFER_BYTES: u64 = 1024 * 1024;
const RUN_TASK_CONTROL_BYTES: u64 = 4096;
const RUN_WAITER_CONTROL_BYTES: u64 = 512;

/// One run's events, cancellation and completion. Dropping the event receiver
/// does not cancel the run; cancellation is an explicit trusted host action.
pub struct RunHandle {
    pub events: mpsc::Receiver<RunEvent>,
    pub cancel: CancellationToken,
    pub result: RunResult,
    pub(super) stream_budget: Arc<dyn AllocationBudget>,
    pub(super) event_budget: Arc<dyn AllocationBudget>,
    pub(super) max_batch_bytes: u64,
}

/// Awaitable external result. Nested runs still signal terminal status through
/// the same channel but deliberately do not create an unused byte buffer.
pub struct RunResult {
    receiver: oneshot::Receiver<RunCompletionReceipt>,
}

pub(crate) enum RunCompletion {
    Buffer(CoreBuffer),
    Intermediate,
}

/// Sent only after the interpreter's actual execution-owned values are gone.
/// An ordinary failed result is not evidence that stream cleanup failed.
pub(crate) struct RunCompletionReceipt {
    pub(crate) outcome: Result<RunCompletion, StepError>,
    pub(crate) cleanup: Result<(), StepError>,
}

pub(crate) fn is_no_replay(error: &StepError) -> bool {
    matches!(
        error.code.as_str(),
        "filesystem-commit-unknown" | "storage-sql-commit-unknown" | "storage-pending-data-move"
    )
}

pub(crate) fn completion_result<T>(
    outcome: Result<T, StepError>,
    cleanup: Result<(), StepError>,
) -> Result<T, StepError> {
    match (outcome, cleanup) {
        (Err(error), _) if is_no_replay(&error) => Err(error),
        (_, Err(error)) => Err(error),
        (outcome, Ok(())) => outcome,
    }
}

// A single fixed-size slot in the existing preadmitted native task state. It
// never retains a guest diagnostic, grows with the number of streams, or mints
// native Store/Fs error authority from a string: these are existing facades.
#[derive(Clone, Copy)]
enum CleanupFailure {
    Incomplete,
    FilesystemCommitUnknown,
    StorageCommitUnknown,
    StoragePendingMove,
}
impl CleanupFailure {
    fn from_error(error: &StepError) -> Self {
        match error.code.as_str() {
            "filesystem-commit-unknown" => Self::FilesystemCommitUnknown,
            "storage-sql-commit-unknown" => Self::StorageCommitUnknown,
            "storage-pending-data-move" => Self::StoragePendingMove,
            _ => Self::Incomplete,
        }
    }
    fn error(self) -> StepError {
        match self {
            Self::Incomplete => StepError::new("internal", "Flow cleanup completion is unknown"),
            Self::FilesystemCommitUnknown => StepError::new(
                "filesystem-commit-unknown",
                "Filesystem mutation may have applied; do not replay",
            ),
            Self::StorageCommitUnknown => StepError::new(
                "storage-sql-commit-unknown",
                "Storage commit is unconfirmed; do not replay",
            ),
            Self::StoragePendingMove => StepError::new(
                "storage-pending-data-move",
                "Storage data move remains pending; do not replay",
            ),
        }
    }
}

#[derive(Default)]
struct CleanupReceipt(std::sync::Mutex<Option<CleanupFailure>>);
impl CleanupReceipt {
    fn record(&self, failure: CleanupFailure) {
        let mut stored = self.0.lock().unwrap_or_else(|error| error.into_inner());
        if stored.is_none() || matches!(*stored, Some(CleanupFailure::Incomplete)) {
            *stored = Some(failure);
        }
    }
    fn result(&self) -> Result<(), StepError> {
        match self.0.lock() {
            Ok(stored) => stored.map_or(Ok(()), |failure| Err(failure.error())),
            Err(error) => Err(error
                .into_inner()
                .unwrap_or(CleanupFailure::Incomplete)
                .error()),
        }
    }
    fn stream(&self) -> StreamCleanup<'_> {
        StreamCleanup {
            receipt: self,
            finished: false,
        }
    }
}

/// Created synchronously when the interpreter obtains a real source. Dropping
/// its future before the exact close returns cannot certify physical cleanup.
struct StreamCleanup<'a> {
    receipt: &'a CleanupReceipt,
    finished: bool,
}
impl StreamCleanup<'_> {
    async fn close(&mut self, source: &mut CoreStream) -> Result<(), StepError> {
        let result = source.close().await;
        if let Err(error) = &result {
            self.receipt.record(CleanupFailure::from_error(error));
        }
        self.finished = true;
        result
    }
}
impl Drop for StreamCleanup<'_> {
    fn drop(&mut self) {
        if !self.finished {
            self.receipt.record(CleanupFailure::Incomplete);
        }
    }
}

/// Native-only fixed transport capacity for progress/status metadata and
/// bounded serialized event batches. Guest CoreValue children retain their
/// original CodeOwner leases when borrowed into these envelopes.
struct NativeTransportBudget {
    limit: u64,
    used: AtomicU64,
    _reservation: ControlReservation,
}
impl NativeTransportBudget {
    fn new(
        control: &CoreControl,
        output_limit: u64,
    ) -> Result<Arc<dyn AllocationBudget>, StepError> {
        let limit = output_limit
            .checked_mul(4)
            .and_then(|bytes| bytes.checked_add(RUN_EVENT_METADATA_CONTROL_BYTES))
            .ok_or_else(|| StepError::new("limit-exceeded", "stream transport bound overflow"))?;
        let control_bytes = u64::try_from(std::mem::size_of::<Self>())
            .map_err(|_| StepError::new("limit-exceeded", "stream control bound overflow"))?;
        let reserved = limit
            .checked_add(control_bytes)
            .ok_or_else(|| StepError::new("limit-exceeded", "stream control bound overflow"))?;
        let reservation = control
            .reserve(ControlKind::Queue, reserved)
            .map_err(|_| StepError::new("limit-exceeded", "native stream capacity exhausted"))?;
        // The reservation includes a conservative fixed estimate for the
        // bounded Tokio event channel and its records.
        Ok(Arc::new(Self {
            limit,
            used: AtomicU64::new(0),
            _reservation: reservation,
        }))
    }
}
impl AllocationBudget for NativeTransportBudget {
    fn reserve(&self, request: AllocationRequest) -> Result<(), AllocationError> {
        if request.bytes > self.limit || request.elements > 1_000_000 || request.work > 8_000_000 {
            return Err(AllocationError::Limit);
        }
        if request.depth > 64 {
            return Err(AllocationError::Depth);
        }
        self.used
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |used| {
                used.checked_add(request.bytes)
                    .filter(|next| *next <= self.limit)
            })
            .map(|_| ())
            .map_err(|_| AllocationError::Limit)
    }

    fn release(&self, bytes: u64) {
        let _ = self
            .used
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |used| {
                used.checked_sub(bytes)
            });
    }
}

impl RunResult {
    pub(crate) fn into_receiver(self) -> oneshot::Receiver<RunCompletionReceipt> {
        self.receiver
    }
}

impl Future for RunResult {
    type Output = Result<CoreBuffer, StepError>;

    fn poll(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
        match Pin::new(&mut self.get_mut().receiver).poll(cx) {
            Poll::Pending => Poll::Pending,
            Poll::Ready(Err(_)) => Poll::Ready(Err(StepError::new(
                "internal",
                "Flow completion channel closed",
            ))),
            Poll::Ready(Ok(receipt)) => {
                Poll::Ready(match completion_result(receipt.outcome, receipt.cleanup) {
                    Err(error) => Err(error),
                    Ok(RunCompletion::Buffer(buffer)) => Ok(buffer),
                    Ok(RunCompletion::Intermediate) => Err(StepError::new(
                        "invalid",
                        "an internal Flow completion has no external output buffer",
                    )),
                })
            }
        }
    }
}

/// A preflighted Flow entry that has not started executing yet.
///
/// Core uses this split to validate frame ownership and construct the immutable
/// run state before charging a nested Flow entry. `start` is the single point
/// that schedules interpreter work.
pub struct PreparedRun {
    state: Arc<RunState>,
    flow_cx: RunCx,
    flow: FlowSelection,
    environment: Environment,
    events: mpsc::Receiver<RunEvent>,
    completion: oneshot::Sender<RunCompletionReceipt>,
    result: RunResult,
    cancel: CancellationToken,
    final_output: bool,
    stream_budget: Arc<dyn AllocationBudget>,
    event_budget: Arc<dyn AllocationBudget>,
    max_batch_bytes: u64,
}

impl PreparedRun {
    /// Start the already preflighted interpreter task.
    pub fn start(self) -> Result<RunHandle, StepError> {
        let Self {
            state,
            flow_cx,
            flow,
            mut environment,
            events,
            completion,
            result,
            cancel,
            final_output,
            stream_budget,
            event_budget,
            max_batch_bytes,
        } = self;
        let cx = flow_cx.call.clone();
        let run = async move {
            let outcome = async {
                let flow = flow.resolve(state.compiled.as_deref())?;
                state.event(&flow_cx, RunEventKind::Started).await?;
                let code = CodeLocation::Flow;
                let execute = state.execute_steps(
                    &flow_cx,
                    &flow,
                    &code,
                    &flow.document.steps,
                    &mut environment,
                    None,
                    0,
                );
                // Every run crosses its actual frozen FlowReturn transition.
                // Only an externally returned run serializes there; a nested
                // Flow keeps its CoreValue intermediate and avoids an unused
                // CoreBuffer allocation.
                let control = tokio::select! {
                    _ = flow_cx.cancel().cancelled() => Err(StepError::new("cancelled", "run cancelled by its input source")),
                    _ = tokio::time::sleep_until(flow_cx.deadline().into()) => Err(StepError::new("cancelled", "original run deadline expired")),
                    result = execute => result,
                }?;
                let value = control.value();
                let origin = control.origin().cloned().unwrap_or_else(|| CompletionOrigin {
                    frame: flow_cx.frame.clone(),
                    call: cx.clone(),
                    code: CodeLocation::Flow,
                    step_address: None,
                });
                let return_code = origin.code;
                let return_step = match origin.step_address {
                    Some(address) => Some(
                        find_completion_step(flow, &return_code, address).ok_or_else(|| {
                            StepError::new(
                                "invalid",
                                "Flow completion step is absent from the frozen AST",
                            )
                        })?,
                    ),
                    None => None,
                };
                let position = crate::FlowNodePosition {
                    flow,
                    code: &return_code,
                    step: return_step,
                    kind: crate::FlowNodeKind::FlowReturn,
                    witness: None,
                };
                let (buffer, output_activation) = if final_output {
                    let return_cx = RunCx::at_node(
                        origin.frame,
                        origin.call,
                        position,
                        crate::AllocationReason::OutputBuffer,
                    )?;
                    let buffer = value
                        .encode_guest(&return_cx.allocation)
                        .map_err(|error| StepError::new("limit-exceeded", error.to_string()))?;
                    (Some(buffer), return_cx.node.clone())
                } else {
                    // The transition remains real even though no final
                    // serialization scope is requested for this internal run.
                    let node = origin.frame.reach_node(&origin.call, position)?;
                    (None, node)
                };
                // Keep this actual output activation live through recording
                // the terminal event; retaining FlowEntry would not authorize
                // a FlowReturn allocation or lifecycle transition.
                Ok::<_, StepError>((value, buffer, output_activation))
            }.await;
            // ErrorPolicy may continue ordinary execution after a failed step,
            // but cannot certify a successful Flow while real source cleanup
            // is unknown. This check is synchronous and never clears a slot.
            let outcome = match outcome {
                Ok(value) => state.cleanup.result().map(|()| value),
                Err(error) => Err(error),
            };
            let terminal = match &outcome {
                Ok((value, _, _)) => RunEventKind::Completed {
                    value: value.clone(),
                    cost_units: cx.budget().map(BudgetContext::cost_units).unwrap_or(0),
                },
                Err(error) if error.code == "cancelled" => RunEventKind::Cancelled {
                    reason: error.message.clone(),
                },
                Err(error) => RunEventKind::Failed {
                    error: error.clone(),
                },
            };
            // Record the final event even when the cancelled token is already set.
            let recorded = state.final_event(&flow_cx, terminal).await;
            if let Err(error) = &recorded {
                state.cleanup.record(CleanupFailure::from_error(error));
            }
            let outcome = match outcome {
                Ok((value, buffer, output_activation)) => {
                    let result = recorded.map(|()| buffer);
                    drop(output_activation);
                    drop(value);
                    result.map(|buffer| match buffer {
                        Some(buffer) => RunCompletion::Buffer(buffer),
                        None => RunCompletion::Intermediate,
                    })
                }
                Err(error) => Err(error),
            };
            // The receipt certifies interpreter termination, not merely that a
            // terminal event was selected. Release execution-owned values,
            // frames and native task/waiter custody before waking observers;
            // only the returned buffer or queued events transfer ownership.
            let cleanup = state.cleanup.result();
            drop(environment);
            drop(flow_cx);
            drop(flow);
            drop(cx);
            drop(state);
            let _ = completion.send(RunCompletionReceipt { outcome, cleanup });
        };
        std::panic::catch_unwind(AssertUnwindSafe(|| tokio::spawn(run)))
            .map_err(|_| StepError::new("unavailable", "Flow runtime is unavailable"))?;
        Ok(RunHandle {
            events,
            cancel,
            result,
            stream_budget,
            event_budget,
            max_batch_bytes,
        })
    }
}

/// Effect-independent interpreter over an immutable compiled Flow catalog.
pub struct FlowEngine {
    invoker: Arc<dyn PortInvoker>,
    journal: Arc<dyn RunJournal>,
    legacy_catalog: BTreeMap<String, ControlHandle<ResolvedFlow>>,
    compiled: Option<Arc<CompiledProfile>>,
    runner: Arc<OnceLock<Weak<dyn FlowRunner>>>,
}

impl FlowEngine {
    /// Construct the engine with mandatory port and journal boundaries.
    pub fn new(invoker: Arc<dyn PortInvoker>, journal: Arc<dyn RunJournal>) -> Self {
        Self {
            invoker,
            journal,
            legacy_catalog: BTreeMap::new(),
            compiled: None,
            runner: Arc::new(OnceLock::new()),
        }
    }

    /// Install the immutable, compiled subflow catalog before accepting inputs.
    pub fn with_flows(mut self, flows: BTreeMap<String, ControlHandle<ResolvedFlow>>) -> Self {
        self.legacy_catalog = flows;
        self
    }

    /// Install the Session's one canonical immutable compiler catalog. Flow
    /// ASTs remain borrowed from this shared profile and are never cloned into
    /// a second map or wrapped individually.
    pub fn with_compiled_profile(mut self, compiled: Arc<CompiledProfile>) -> Self {
        self.compiled = Some(compiled);
        self.legacy_catalog.clear();
        self
    }

    /// Install Core's single gated Flow runner once, without retaining Core.
    pub fn set_flow_runner(&self, runner: Weak<dyn FlowRunner>) -> Result<(), StepError> {
        self.runner
            .set(runner)
            .map_err(|_| StepError::new("conflict", "Flow runner was already installed"))
    }

    /// Run a frozen plan under the original cancellation token and deadline.
    pub fn run(
        &self,
        frame: ControlHandle<dyn ActiveCodeFrame>,
        flow: ControlHandle<ResolvedFlow>,
        input: CoreValue,
        limits: LoopLimits,
    ) -> Result<RunHandle, StepError> {
        self.prepare(frame, flow, input, limits)?.start()
    }

    /// Run a Flow borrowed by exact key from the canonical compiled Profile.
    pub fn run_canonical(
        &self,
        frame: ControlHandle<dyn ActiveCodeFrame>,
        flow_key: &str,
        input: CoreValue,
        limits: LoopLimits,
    ) -> Result<RunHandle, StepError> {
        self.prepare_canonical(frame, flow_key, input, limits)?
            .start()
    }

    /// Run a nested Flow as internal CoreValue events without constructing an
    /// unused final CoreBuffer. The caller still owns the returned stream and
    /// its normal cleanup/terminal path.
    pub fn run_intermediate(
        &self,
        frame: ControlHandle<dyn ActiveCodeFrame>,
        flow: ControlHandle<ResolvedFlow>,
        input: CoreValue,
        limits: LoopLimits,
    ) -> Result<crate::CoreStream, StepError> {
        self.prepare_mode(frame, flow, input, limits, false)?
            .start()
            .map(RunHandle::into_stream)
    }

    /// Run a canonical nested Flow and expose its CoreValue events without
    /// serializing an unused final buffer.
    pub fn run_canonical_intermediate(
        &self,
        frame: ControlHandle<dyn ActiveCodeFrame>,
        flow_key: &str,
        input: CoreValue,
        limits: LoopLimits,
    ) -> Result<crate::CoreStream, StepError> {
        self.prepare_canonical_mode(frame, flow_key, input, limits, false)?
            .start()
            .map(RunHandle::into_stream)
    }

    /// Resolve ownership and construct run state without scheduling guest work.
    ///
    /// This lets Core perform all admission and inherited-scope checks before a
    /// nested Flow cost is charged immediately before [`PreparedRun::start`].
    pub fn prepare(
        &self,
        frame: ControlHandle<dyn ActiveCodeFrame>,
        flow: ControlHandle<ResolvedFlow>,
        input: CoreValue,
        limits: LoopLimits,
    ) -> Result<PreparedRun, StepError> {
        self.prepare_mode(frame, flow, input, limits, true)
    }

    /// Prepare an external run from the canonical Profile catalog.
    pub fn prepare_canonical(
        &self,
        frame: ControlHandle<dyn ActiveCodeFrame>,
        flow_key: &str,
        input: CoreValue,
        limits: LoopLimits,
    ) -> Result<PreparedRun, StepError> {
        self.prepare_canonical_mode(frame, flow_key, input, limits, true)
    }

    fn prepare_canonical_mode(
        &self,
        frame: ControlHandle<dyn ActiveCodeFrame>,
        flow_key: &str,
        input: CoreValue,
        limits: LoopLimits,
        final_output: bool,
    ) -> Result<PreparedRun, StepError> {
        if flow_key.is_empty() || flow_key.len() > 512 {
            return Err(StepError::new("invalid", "invalid canonical Flow key"));
        }
        let compiled = self.compiled.as_ref().ok_or_else(|| {
            StepError::new(
                "unavailable",
                "canonical compiled Flow catalog is unavailable",
            )
        })?;
        let flow = compiled.flows.get(flow_key).ok_or_else(|| {
            StepError::new("not-found", "Flow key is absent from the compiled catalog")
        })?;
        self.prepare_selection(
            frame,
            FlowSelection::Canonical(flow_key.to_owned()),
            flow,
            input,
            limits,
            final_output,
        )
    }

    fn prepare_mode(
        &self,
        frame: ControlHandle<dyn ActiveCodeFrame>,
        flow: ControlHandle<ResolvedFlow>,
        input: CoreValue,
        limits: LoopLimits,
        final_output: bool,
    ) -> Result<PreparedRun, StepError> {
        self.prepare_selection(
            frame,
            FlowSelection::Legacy(flow.clone()),
            flow.get(),
            input,
            limits,
            final_output,
        )
    }

    fn prepare_selection(
        &self,
        frame: ControlHandle<dyn ActiveCodeFrame>,
        selection: FlowSelection,
        flow: &ResolvedFlow,
        input: CoreValue,
        limits: LoopLimits,
        final_output: bool,
    ) -> Result<PreparedRun, StepError> {
        if !limits.validate() {
            return Err(StepError::new(
                "invalid",
                "Profile loop ceilings are required and must be positive",
            ));
        }
        let cx = Arc::new(
            frame
                .context()
                .try_clone_precharged()
                .map_err(|error| StepError::new("limit-exceeded", error.to_string()))?,
        );
        let code = CodeLocation::Flow;
        let flow_cx = RunCx::at_node(
            frame.clone(),
            cx,
            FlowNodePosition {
                flow,
                code: &code,
                step: None,
                kind: FlowNodeKind::FlowEntry,
                witness: None,
            },
            AllocationReason::RuntimeValue,
        )?;
        // Progress/status transport is trusted native control data. It uses
        // its own fixed CoreControl ceiling and never borrows the guest output
        // allowance; guest CoreValue payloads keep their original leases.
        let output_limit = MAX_RUN_EVENT_BUFFER_BYTES;
        let transport_budget = NativeTransportBudget::new(flow_cx.call.control(), output_limit)?;
        let stream_budget = transport_budget.clone();
        let event_budget = transport_budget;
        let task_reservation = flow_cx
            .call
            .control()
            .reserve(ControlKind::Task, RUN_TASK_CONTROL_BYTES)
            .map_err(|_| StepError::new("limit-exceeded", "native Flow task capacity exhausted"))?;
        let waiter_reservation = flow_cx
            .call
            .control()
            .reserve(ControlKind::Waiter, RUN_WAITER_CONTROL_BYTES)
            .map_err(|_| {
                StepError::new("limit-exceeded", "native Flow waiter capacity exhausted")
            })?;
        let allocation = flow_cx.allocation.clone();
        let environment = Environment::with_input(input, &allocation)?;
        let (sender, events) = mpsc::channel(RUN_EVENT_QUEUE_CAPACITY);
        let (completion, result) = oneshot::channel();
        let cancel = flow_cx.call.cancel().clone();
        let state = Arc::new(RunState {
            invoker: self.invoker.clone(),
            journal: self.journal.clone(),
            event_budget: event_budget.clone(),
            _task_reservation: task_reservation,
            _waiter_reservation: waiter_reservation,
            bus: Mutex::new(EventBus {
                sequence: 0,
                sender,
            }),
            output_bytes: AtomicU64::new(0),
            cleanup: CleanupReceipt::default(),
            limits,
            legacy_catalog: self.legacy_catalog.clone(),
            compiled: self.compiled.clone(),
            root_plan_digest: flow.plan_digest.clone(),
            request: flow_cx.call.request().clone(),
            runner: self.runner.clone(),
        });
        Ok(PreparedRun {
            state,
            flow_cx,
            flow: selection,
            environment,
            events,
            completion,
            result: RunResult { receiver: result },
            cancel,
            final_output,
            stream_budget,
            event_budget,
            max_batch_bytes: output_limit,
        })
    }
}

struct EventBus {
    sequence: u64,
    sender: mpsc::Sender<RunEvent>,
}
struct RunState {
    invoker: Arc<dyn PortInvoker>,
    journal: Arc<dyn RunJournal>,
    event_budget: Arc<dyn AllocationBudget>,
    _task_reservation: ControlReservation,
    _waiter_reservation: ControlReservation,
    bus: Mutex<EventBus>,
    output_bytes: AtomicU64,
    cleanup: CleanupReceipt,
    limits: LoopLimits,
    legacy_catalog: BTreeMap<String, ControlHandle<ResolvedFlow>>,
    compiled: Option<Arc<CompiledProfile>>,
    runner: Arc<OnceLock<Weak<dyn FlowRunner>>>,
    root_plan_digest: tobkiri_canon::Digest,
    request: tobkiri_canon::ids::RequestId,
}

enum FlowSelection {
    Canonical(String),
    Legacy(ControlHandle<ResolvedFlow>),
}
impl FlowSelection {
    fn resolve<'a>(
        &'a self,
        compiled: Option<&'a CompiledProfile>,
    ) -> Result<&'a ResolvedFlow, StepError> {
        match self {
            Self::Canonical(key) => compiled
                .and_then(|profile| profile.flows.get(key))
                .ok_or_else(|| StepError::new("not-found", "canonical Flow is unavailable")),
            Self::Legacy(flow) => Ok(flow.get()),
        }
    }
}

#[derive(Clone)]
enum Control {
    Value(CoreValue),
    Return(CoreValue),
    Reached {
        value: CoreValue,
        origin: CompletionOrigin,
        returned: bool,
    },
}
impl Control {
    fn value(&self) -> CoreValue {
        match self {
            Self::Value(value) | Self::Return(value) => value.clone(),
            Self::Reached { value, .. } => value.clone(),
        }
    }

    fn origin(&self) -> Option<&CompletionOrigin> {
        match self {
            Self::Reached { origin, .. } => Some(origin),
            Self::Value(_) | Self::Return(_) => None,
        }
    }

    fn is_return(&self) -> bool {
        matches!(self, Self::Return(_) | Self::Reached { returned: true, .. })
    }
}

/// The actual code activation that produced the selected Flow completion.
/// The AST location is recovered by address from the same immutable catalog,
/// so this metadata never owns or clones a guest AST node.
#[derive(Clone)]
struct CompletionOrigin {
    frame: ControlHandle<dyn ActiveCodeFrame>,
    call: Arc<CallCx>,
    code: CodeLocation,
    step_address: Option<usize>,
}

fn find_completion_step<'a>(
    flow: &'a ResolvedFlow,
    code: &CodeLocation,
    address: usize,
) -> Option<&'a Step> {
    fn find(steps: &[Step], address: usize) -> Option<&Step> {
        for step in steps {
            if std::ptr::from_ref(step).addr() == address {
                return Some(step);
            }
            let nested = match &step.kind {
                StepKind::Branch(branch) => {
                    find(&branch.then, address).or_else(|| find(&branch.otherwise, address))
                }
                StepKind::Parallel(parallel) => find(&parallel.body, address),
                StepKind::Loop(repeat) => find(&repeat.body, address),
                _ => None,
            };
            if nested.is_some() {
                return nested;
            }
        }
        None
    }

    match code {
        CodeLocation::Flow => find(&flow.document.steps, address),
        CodeLocation::PackOverlay { id } | CodeLocation::UserOverlay { id } => flow
            .ordered_overlays
            .values()
            .flatten()
            .filter(|overlay| {
                let actual = match (&overlay.owner, &overlay.hash) {
                    (Some(owner), Some(_)) => format!("{owner}/{}", overlay.document.id),
                    (None, None) => format!("user:{}", overlay.document.id),
                    _ => return false,
                };
                &actual == id
            })
            .find_map(|overlay| find(&overlay.document.body, address)),
    }
}

#[derive(Clone)]
struct RunCx {
    call: Arc<CallCx>,
    allocation: Arc<dyn AllocationBudget>,
    frame: ControlHandle<dyn ActiveCodeFrame>,
    node: ReachedNode,
}
impl std::ops::Deref for RunCx {
    type Target = CallCx;
    fn deref(&self) -> &CallCx {
        &self.call
    }
}
impl RunCx {
    fn at_node(
        frame: ControlHandle<dyn ActiveCodeFrame>,
        call: Arc<CallCx>,
        position: FlowNodePosition<'_>,
        reason: AllocationReason,
    ) -> Result<Self, StepError> {
        let node = frame.reach_node(&call, position)?;
        let allocation = frame.budget_at_reached_node(&call, &node, reason)?;
        Ok(Self {
            call,
            allocation,
            frame,
            node,
        })
    }
}

struct NextCx<'a> {
    step: &'a Step,
    overlays: &'a [ResolvedOverlay],
    overlay_index: usize,
    input: CoreValue,
    called: std::sync::atomic::AtomicBool,
    output: std::sync::Mutex<Option<CoreValue>>,
    allow_next: bool,
    base: RunCx,
    base_code: CodeLocation,
    overlay_frame: ControlHandle<dyn ActiveCodeFrame>,
    continuation: FrameContinuation,
}

impl RunState {
    async fn event(&self, cx: &RunCx, kind: RunEventKind) -> Result<(), StepError> {
        self.check(&cx.call)?;
        self.final_event(cx, kind).await
    }

    async fn final_event(&self, cx: &RunCx, kind: RunEventKind) -> Result<(), StepError> {
        let mut bus = self.bus.lock().await;
        bus.sequence = bus
            .sequence
            .checked_add(1)
            .ok_or_else(|| StepError::new("limit-exceeded", "run event sequence exhausted"))?;
        let event = RunEvent {
            request: self.request.clone(),
            plan_digest: self.root_plan_digest.clone(),
            sequence: bus.sequence,
            kind,
        };
        self.journal
            .append(&cx.call, &event, &self.event_budget)
            .await?;
        // Observers may disconnect without affecting a persisted run. The original
        // deadline bounds backpressure if an observer stops draining its receiver.
        let _ = tokio::time::timeout_at(cx.deadline().into(), bus.sender.send(event)).await;
        Ok(())
    }

    fn check(&self, cx: &CallCx) -> Result<(), StepError> {
        if cx.cancel().is_cancelled() {
            return Err(StepError::new("cancelled", "run cancelled"));
        }
        if Instant::now() >= cx.deadline() {
            return Err(StepError::new("cancelled", "original run deadline expired"));
        }
        Ok(())
    }

    fn check_run(&self, cx: &RunCx) -> Result<(), StepError> {
        self.check(&cx.call)?;
        cx.budget()
            .ok_or_else(|| StepError::new("unavailable", "Flow execution budget is missing"))?
            .check()
            .map_err(|error| StepError::new(error.code(), error.to_string()))
    }

    fn output_size(&self, cx: &RunCx, value: &CoreValue) -> Result<(), StepError> {
        let bytes = value
            .encode_guest(&cx.allocation)
            .map_err(|error| StepError::new("limit-exceeded", error.to_string()))?
            .len() as u64;
        self.output_bytes
            .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |used| {
                used.checked_add(bytes)
                    .filter(|total| *total <= cx.limits().output_bytes)
            })
            .map(|_| ())
            .map_err(|_| StepError::new("limit-exceeded", "run output byte ceiling exceeded"))
    }
}
