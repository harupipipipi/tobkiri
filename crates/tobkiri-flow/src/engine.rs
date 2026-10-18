mod execution;

use std::{
    collections::BTreeMap,
    future::Future,
    panic::AssertUnwindSafe,
    pin::Pin,
    sync::{
        Arc, OnceLock, Weak,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    task::{Context, Poll},
    time::Instant,
};

use futures::{FutureExt, StreamExt, future::BoxFuture, stream};
use tobkiri_canon::{
    AllocationBudget, AllocationError, AllocationRequest, ControlHandle, CoreBuffer, CoreRef,
    CoreValue, Digest,
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
    FlowRunner, PortInvoker, RunEvent, RunEventKind, RunJournal, StepError, StepOutput,
    StreamCloseControl, StreamCloseReason, evaluate, expression::boolean,
};
use crate::{FrameContinuation, OverlayPosition, ReachedNode};

const RUN_EVENT_QUEUE_CAPACITY: usize = 32;
const RUN_EVENT_METADATA_CONTROL_BYTES: u64 = 32 * 1024;
const MAX_RUN_EVENT_BUFFER_BYTES: u64 = 1024 * 1024;
const RUN_TASK_CONTROL_BYTES: u64 = 4096;
const RUN_WAITER_CONTROL_BYTES: u64 = 512;
const MAX_RUN_TASK_FUTURE_BYTES: usize = 256 * 1024;

/// The one actual interpreter future, created only after native control
/// admission. A spawner must own and await this task itself; a RunResult,
/// terminal event or wrapper awaiting parent terminal delivery is no substitute.
pub struct RunTask {
    future: BoxFuture<'static, RunTaskPhysicalResult>,
    // Keep native task capacity until the actual future is destroyed.
    _reservation: ControlReservation,
}
impl RunTask {
    fn new<F>(future: F, control: &CoreControl) -> Result<Self, StepError>
    where
        F: Future<Output = RunTaskPhysicalResult> + Send + 'static,
    {
        if std::mem::size_of::<F>() > MAX_RUN_TASK_FUTURE_BYTES {
            return Err(StepError::new(
                "limit-exceeded",
                "Flow task capture bound exceeded",
            ));
        }
        let bytes = RUN_TASK_CONTROL_BYTES
            .checked_add(std::mem::size_of::<F>() as u64)
            .ok_or_else(|| StepError::new("limit-exceeded", "Flow task control bound overflow"))?;
        let reservation = control
            .reserve(ControlKind::Task, bytes)
            .map_err(|_| StepError::new("limit-exceeded", "native Flow task capacity exhausted"))?;
        Ok(Self {
            future: Box::pin(future),
            _reservation: reservation,
        })
    }

    /// Admit the actual trusted native cleanup future before boxing it. The
    /// future must own/await its real work and return physical completion only
    /// after releasing it; business results and terminal delivery are separate.
    /// This constructor grants no authority to start guest work.
    pub fn cleanup<F>(future: F, control: &CoreControl) -> Result<Self, StepError>
    where
        F: Future<Output = RunTaskPhysicalResult> + Send + 'static,
    {
        Self::new(future, control)
    }
}
impl Future for RunTask {
    type Output = RunTaskPhysicalResult;
    fn poll(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
        self.get_mut().future.as_mut().poll(cx)
    }
}

/// Physical custody is independent from the interpreter's business/audit result.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RunTaskPhysicalError {
    CompletionUnknown,
}
pub type RunTaskPhysicalResult = Result<(), RunTaskPhysicalError>;

/// Exact private-frame lifetime identity; it supplies no execution authority.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RunTaskExecution {
    pub frame: Digest,
    pub request: Digest,
    pub activation: u64,
}

/// Independent physical completion of the same task admitted by the spawner.
/// Wait cancellation must retain the original Join/receiver and cached error.
#[async_trait::async_trait]
pub trait RunTaskCompletion: Send + Sync {
    /// Confirm the original task was published and its actual Join is owned.
    /// A pre-admitted but unpublished slot is not enough to start its sibling.
    async fn wait_started(&self) -> Result<(), StepError> {
        Err(StepError::new(
            "unavailable",
            "Flow task launch receipt is unavailable",
        ))
    }

    async fn wait(&self) -> Result<(), StepError>;
}

/// Trusted native task custody. Admit before publishing the actual task and
/// retain its original physical completion independently of parent terminals.
pub trait RunTaskSpawner: Send + Sync {
    fn spawn(
        &self,
        task: RunTask,
        close: Arc<dyn StreamCloseControl>,
    ) -> Result<Arc<dyn RunTaskCompletion>, StepError>;

    /// Register the actual child before its body is published. Implementations
    /// must retain bounded child custody in the parent's completion receipt.
    fn child(
        &self,
        _execution: RunTaskExecution,
        _cancel: CancellationToken,
    ) -> Result<Arc<dyn RunTaskSpawner>, StepError> {
        Err(StepError::new(
            "unavailable",
            "Flow child task custody is unavailable",
        ))
    }

    /// Take the paired cleanup sibling admitted with this body, before start.
    /// It belongs to the caller parent, never to the body whose source it closes.
    fn cleanup_child(
        &self,
        _execution: RunTaskExecution,
        _cancel: CancellationToken,
    ) -> Result<Arc<dyn RunTaskSpawner>, StepError> {
        Err(StepError::new(
            "unavailable",
            "Flow cleanup task custody is unavailable",
        ))
    }

    /// Observe this exact body's Join plus its independently registered children.
    /// Waiting must not seal or wait on an own-inclusive VM generation census.
    fn completion(&self) -> Result<Arc<dyn RunTaskCompletion>, StepError> {
        Err(StepError::new(
            "unavailable",
            "Flow task completion custody is unavailable",
        ))
    }

    /// Close only this exact one-use mailbox when it has not accepted a body.
    /// Accepted payloads remain owned and untouched; retain the original Join
    /// and receivers. Mailbox closure or normal task exit is neither a
    /// NeverStarted receipt nor proof of physical cleanup success.
    fn abandon_unstarted(&self) -> Result<(), StepError> {
        Err(StepError::new(
            "unavailable",
            "Flow unstarted task abandonment is unavailable",
        ))
    }

    /// Conservatively mark the ORIGINAL captured generation before starting a
    /// child whose physical completion is not registered here, or before an
    /// in-flight native source future is abandoned. This grants no authority.
    /// Generation implementations must latch uncertainty even if subsequent
    /// live validation returns an error; Drop cannot return that failure.
    fn before_unregistered_child(&self) -> Result<(), StepError> {
        Err(StepError::new(
            "unavailable",
            "native child cleanup census is unavailable",
        ))
    }
}

struct TaskReceipt {
    receiver: Option<oneshot::Receiver<Result<(), StepError>>>,
    result: Option<Result<(), StepError>>,
}
struct TokioRunCompletion {
    receipt: Mutex<TaskReceipt>,
    control: CoreControl,
    _reservation: ControlReservation,
}
#[async_trait::async_trait]
impl RunTaskCompletion for TokioRunCompletion {
    async fn wait(&self) -> Result<(), StepError> {
        let _waiter = self
            .control
            .reserve(ControlKind::Waiter, RUN_WAITER_CONTROL_BYTES)
            .map_err(|_| {
                StepError::new(
                    "limit-exceeded",
                    "Flow completion waiter capacity exhausted",
                )
            })?;
        let mut receipt = self.receipt.lock().await;
        if let Some(result) = &receipt.result {
            return result.clone();
        }
        // Borrow rather than take before await: abandoning a wait cannot lose
        // the only receipt, nor cause another cleanup task to be launched.
        let result = match receipt.receiver.as_mut() {
            Some(receiver) => receiver.await.unwrap_or_else(|_| Err(task_unknown())),
            None => Err(task_unknown()),
        };
        receipt.receiver.take();
        receipt.result = Some(result.clone());
        result
    }
}
fn task_unknown() -> StepError {
    StepError::new("component", "Flow task physical completion is unknown")
}
struct TokioRunTaskSpawner {
    control: CoreControl,
    runtime: tokio::runtime::Handle,
}
impl RunTaskSpawner for TokioRunTaskSpawner {
    fn before_unregistered_child(&self) -> Result<(), StepError> {
        // This ordinary native launcher makes no VM-generation cleanup claim.
        Ok(())
    }
    fn spawn(
        &self,
        task: RunTask,
        close: Arc<dyn StreamCloseControl>,
    ) -> Result<Arc<dyn RunTaskCompletion>, StepError> {
        let reservation = self
            .control
            .reserve(ControlKind::Queue, 4096)
            .map_err(|_| StepError::new("limit-exceeded", "Flow completion capacity exhausted"))?;
        let monitor_control = self
            .control
            .reserve(ControlKind::Task, RUN_TASK_CONTROL_BYTES)
            .map_err(|_| StepError::new("limit-exceeded", "Flow monitor capacity exhausted"))?;
        let (sender, receiver) = oneshot::channel();
        let completion = Arc::new(TokioRunCompletion {
            receipt: Mutex::new(TaskReceipt {
                receiver: Some(receiver),
                result: None,
            }),
            control: self.control.clone(),
            _reservation: reservation,
        });
        let retained = completion.clone();
        let runtime = self.runtime.clone();
        // The pre-admitted monitor itself publishes and owns the one real
        // Join. A caller cannot drop a transfer receiver between spawn/attach.
        let monitor = std::panic::catch_unwind(AssertUnwindSafe(|| {
            self.runtime.spawn(async move {
                let joined = std::panic::catch_unwind(AssertUnwindSafe(|| runtime.spawn(task)));
                let result = match joined {
                    Ok(join) => join
                        .await
                        .map_err(|_| task_unknown())
                        .and_then(|physical| physical.map_err(|_| task_unknown())),
                    Err(_) => Err(task_unknown()),
                };
                drop(close);
                let _ = sender.send(result);
                drop(retained);
                drop(monitor_control);
            })
        }))
        .map_err(|_| StepError::new("unavailable", "Flow runtime is unavailable"))?;
        drop(monitor); // Detach only the owning monitor, never the actual Join.
        Ok(completion)
    }
}

/// Acyclic cancellation companion: it owns only bounded native control and
/// tokens, never the interpreter, its parent terminal or its Join receipt.
struct RunCloseControl {
    cancel: CancellationToken,
    delivery_stop: CancellationToken,
    execution_finished: std::sync::atomic::AtomicBool,
    _reservation: ControlReservation,
}
impl RunCloseControl {
    async fn deliver(
        &self,
        sender: &mpsc::Sender<RunEvent>,
        event: RunEvent,
        cancel: &CancellationToken,
        authority_cancel: &CancellationToken,
        deadline: Instant,
    ) {
        // Cancellation stops waiting for capacity; it does not discard an
        // already journaled terminal/status event when the funded queue has
        // room. Explicit stream close still closes its receiver independently.
        let event = match sender.try_send(event) {
            Ok(()) | Err(mpsc::error::TrySendError::Closed(_)) => return,
            Err(mpsc::error::TrySendError::Full(event)) => event,
        };
        tokio::select! {
            biased;
            _ = self.delivery_stop.cancelled() => {},
            _ = cancel.cancelled() => {},
            _ = authority_cancel.cancelled() => {},
            _ = tokio::time::sleep_until(deadline.into()) => {},
            _ = sender.send(event) => {},
        }
    }
}
impl StreamCloseControl for RunCloseControl {
    fn record_close(&self, _reason: StreamCloseReason) -> Result<(), StepError> {
        // Release event-channel backpressure BEFORE waking source cancellation.
        // final_event still awaits its journal; this is not a terminal receipt.
        self.delivery_stop.cancel();
        if !self.execution_finished.load(Ordering::Acquire) {
            self.cancel.cancel();
        }
        Ok(())
    }
}

/// One run's events, cancellation and completion. Dropping the event receiver
/// does not cancel the run; cancellation is an explicit trusted host action.
pub struct RunHandle {
    pub events: mpsc::Receiver<RunEvent>,
    pub cancel: CancellationToken,
    pub result: RunResult,
    pub(super) stream_budget: Arc<dyn AllocationBudget>,
    pub(super) event_budget: Arc<dyn AllocationBudget>,
    pub(super) max_batch_bytes: u64,
    pub(super) task_completion: Arc<dyn RunTaskCompletion>,
    pub(super) generation_bound: bool,
    pub(super) close_control: Arc<dyn StreamCloseControl>,
}

/// Awaitable external result. Nested runs still signal terminal status through
/// the same channel but deliberately do not create an unused byte buffer.
pub struct RunResult {
    receiver: oneshot::Receiver<Result<RunCompletion, StepError>>,
}

pub(crate) enum RunCompletion {
    Buffer(CoreBuffer),
    Intermediate,
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
    fn allocate(
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
    pub(crate) fn into_receiver(self) -> oneshot::Receiver<Result<RunCompletion, StepError>> {
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
            Poll::Ready(Ok(Err(error))) => Poll::Ready(Err(error)),
            Poll::Ready(Ok(Ok(RunCompletion::Buffer(buffer)))) => Poll::Ready(Ok(buffer)),
            Poll::Ready(Ok(Ok(RunCompletion::Intermediate))) => Poll::Ready(Err(StepError::new(
                "invalid",
                "an internal Flow completion has no external output buffer",
            ))),
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
    completion: oneshot::Sender<Result<RunCompletion, StepError>>,
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
        if let Some(spawner) = self.flow_cx.frame.task_spawner() {
            // Only the actual prepared private frame supplies this lifetime
            // context. It is never inferred from a principal or Pack label.
            return self.start_with_spawner(spawner);
        }
        let spawner = TokioRunTaskSpawner {
            control: self.flow_cx.call.control().clone(),
            runtime: tokio::runtime::Handle::try_current()
                .map_err(|_| StepError::new("unavailable", "Flow runtime is unavailable"))?,
        };
        self.start_using(&spawner, None)
    }

    /// Publish through the actual provider generation's admitted task custody.
    pub fn start_with_spawner(
        self,
        spawner: Arc<dyn RunTaskSpawner>,
    ) -> Result<RunHandle, StepError> {
        self.start_using(spawner.as_ref(), Some(spawner.clone()))
    }

    fn start_using(
        self,
        spawner: &dyn RunTaskSpawner,
        generation_spawner: Option<Arc<dyn RunTaskSpawner>>,
    ) -> Result<RunHandle, StepError> {
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
        let generation_bound = generation_spawner.is_some();
        if let Some(spawner) = generation_spawner {
            state
                .task_spawner
                .set(spawner)
                .map_err(|_| StepError::new("conflict", "Flow task spawner is already frozen"))?;
        }
        let cx = flow_cx.call.clone();
        let task_control = cx.control().clone();
        let close_control = state.close.clone();
        let run = async move {
            let outcome = async {
                let flow = flow.resolve(state.compiled.as_deref())?;
                state.event(&flow_cx, RunEventKind::Started).await?;
                let code = CodeLocation::Flow;
                let execute = state.execute_steps(
                    &flow_cx,
                    StepSequence {
                        flow,
                        code: &code,
                        steps: &flow.document.steps,
                    },
                    &mut environment,
                    None,
                    0,
                );
                // Every run crosses its actual frozen FlowReturn transition.
                // Only an externally returned run serializes there; a nested
                // Flow keeps its CoreValue intermediate and avoids an unused
                // CoreBuffer allocation.
                let control = await_run_work(
                    &flow_cx,
                    execute,
                    StepError::new("cancelled", "original run deadline expired"),
                )
                .await?;
                let value = control.value();
                let origin = control
                    .origin()
                    .cloned()
                    .unwrap_or_else(|| CompletionOrigin {
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
            }
            .await;
            state
                .close
                .execution_finished
                .store(true, Ordering::Release);
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
            drop(environment);
            drop(flow_cx);
            drop(flow);
            drop(cx);
            let physical = if state.physical_unknown.load(Ordering::Acquire) {
                Err(RunTaskPhysicalError::CompletionUnknown)
            } else {
                Ok(())
            };
            drop(state);
            let _ = completion.send(outcome);
            physical
        };
        let task = RunTask::new(run, &task_control)?;
        let task_completion = spawner.spawn(task, close_control.clone())?;
        Ok(RunHandle {
            events,
            cancel,
            result,
            stream_budget,
            event_budget,
            max_batch_bytes,
            task_completion,
            generation_bound,
            close_control,
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

    /// Canonical nested Flow whose actual interpreter task is registered with
    /// its owning provider generation before any work can start.
    pub fn run_canonical_intermediate_with_spawner(
        &self,
        frame: ControlHandle<dyn ActiveCodeFrame>,
        flow_key: &str,
        input: CoreValue,
        limits: LoopLimits,
        spawner: Arc<dyn RunTaskSpawner>,
    ) -> Result<crate::CoreStream, StepError> {
        self.prepare_canonical_mode(frame, flow_key, input, limits, false)?
            .start_with_spawner(spawner)
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
        let transport_budget =
            NativeTransportBudget::allocate(flow_cx.call.control(), output_limit)?;
        let stream_budget = transport_budget.clone();
        let event_budget = transport_budget;
        let run_reservation = flow_cx
            .call
            .control()
            .reserve(ControlKind::Frame, RUN_TASK_CONTROL_BYTES)
            .map_err(|_| {
                StepError::new("limit-exceeded", "native Flow state capacity exhausted")
            })?;
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
        let close_reservation = flow_cx
            .call
            .control()
            .reserve(
                ControlKind::Registration,
                std::mem::size_of::<RunCloseControl>() as u64 + 128,
            )
            .map_err(|_| {
                StepError::new("limit-exceeded", "Flow close control capacity exhausted")
            })?;
        let delivery_stop =
            CancellationToken::new_with_control(flow_cx.call.control()).map_err(|_| {
                StepError::new("limit-exceeded", "Flow delivery control capacity exhausted")
            })?;
        let close = Arc::new(RunCloseControl {
            cancel: cancel.clone(),
            delivery_stop,
            execution_finished: std::sync::atomic::AtomicBool::new(false),
            _reservation: close_reservation,
        });
        let state = Arc::new(RunState {
            invoker: self.invoker.clone(),
            journal: self.journal.clone(),
            event_budget: event_budget.clone(),
            _run_reservation: run_reservation,
            _waiter_reservation: waiter_reservation,
            bus: Mutex::new(EventBus {
                sequence: 0,
                sender,
            }),
            output_bytes: AtomicU64::new(0),
            limits,
            legacy_catalog: self.legacy_catalog.clone(),
            compiled: self.compiled.clone(),
            root_plan_digest: flow.plan_digest.clone(),
            request: flow_cx.call.request().clone(),
            runner: self.runner.clone(),
            close,
            task_spawner: OnceLock::new(),
            physical_unknown: AtomicBool::new(false),
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
    _run_reservation: ControlReservation,
    _waiter_reservation: ControlReservation,
    bus: Mutex<EventBus>,
    output_bytes: AtomicU64,
    limits: LoopLimits,
    legacy_catalog: BTreeMap<String, ControlHandle<ResolvedFlow>>,
    compiled: Option<Arc<CompiledProfile>>,
    runner: Arc<OnceLock<Weak<dyn FlowRunner>>>,
    root_plan_digest: tobkiri_canon::Digest,
    request: tobkiri_canon::ids::RequestId,
    close: Arc<RunCloseControl>,
    task_spawner: OnceLock<Arc<dyn RunTaskSpawner>>,
    physical_unknown: AtomicBool,
}

/// Borrow the same canonical AST objects for one execution sequence.
struct StepSequence<'a> {
    flow: &'a ResolvedFlow,
    code: &'a CodeLocation,
    steps: &'a [Step],
}

// This field is intentionally FIRST in CleanupFuture: abandonment must mark
// the exact original generation BEFORE destroying a pending native future,
// including cancellation, loop timeout and parallel sibling failure. The hook
// only disables a cleanup-success claim; it never authorizes child execution.
struct CleanupAbandonment<'a> {
    spawner: Option<&'a Arc<dyn RunTaskSpawner>>,
    armed: bool,
}
impl Drop for CleanupAbandonment<'_> {
    fn drop(&mut self) {
        if self.armed
            && let Some(spawner) = self.spawner
        {
            let _ = spawner.before_unregistered_child();
        }
    }
}
struct CleanupFuture<'a, T> {
    abandonment: CleanupAbandonment<'a>,
    future: BoxFuture<'a, Result<T, StepError>>,
    _reservation: ControlReservation,
}
impl<T> Future for CleanupFuture<'_, T> {
    type Output = Result<T, StepError>;
    fn poll(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
        let this = self.get_mut();
        let result = this.future.as_mut().poll(cx);
        if result.is_ready() {
            this.abandonment.armed = false;
        }
        result
    }
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
    // Interpreter-only cooperative stop. It never substitutes for the actual
    // authority context, nor cancels an unrelated sibling/parent authority.
    stop: CancellationToken,
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
        let stop = call.cancel().clone();
        Ok(Self {
            call,
            allocation,
            frame,
            node,
            stop,
        })
    }

    fn with_stop(mut self, stop: CancellationToken) -> Self {
        self.stop = stop;
        self
    }
}

/// Cancellation selects the business result, but never destroys unique native
/// work. Keep polling this SAME future through cleanup before returning it.
async fn await_run_work<F, T>(
    cx: &RunCx,
    future: F,
    deadline_error: StepError,
) -> Result<T, StepError>
where
    F: Future<Output = Result<T, StepError>>,
{
    await_retained_work(&cx.stop, cx.cancel(), cx.deadline(), future, deadline_error).await
}

async fn await_retained_work<F, T>(
    stop: &CancellationToken,
    authority_cancel: &CancellationToken,
    deadline: Instant,
    future: F,
    deadline_error: StepError,
) -> Result<T, StepError>
where
    F: Future<Output = Result<T, StepError>>,
{
    tokio::pin!(future);
    let error = tokio::select! {
        biased;
        _ = stop.cancelled() => StepError::new("cancelled", "run cancelled"),
        _ = authority_cancel.cancelled() => StepError::new("cancelled", "run authority cancelled"),
        _ = tokio::time::sleep_until(deadline.into()) => deadline_error,
        result = future.as_mut() => return result,
    };
    stop.cancel();
    let _ = future.await;
    Err(error)
}

/// Stop lazy admission on the first failure, while draining all futures that
/// were already created. No child/cleanup future is dropped by `?` here.
async fn drain_parallel<S, T>(
    pending: S,
    stop: &CancellationToken,
    mut accept: impl FnMut(T) -> Result<(), StepError>,
) -> Result<(), StepError>
where
    S: futures::Stream<Item = Result<T, StepError>>,
{
    futures::pin_mut!(pending);
    let mut failure = None;
    while let Some(item) = pending.next().await {
        if let Err(error) = item.and_then(&mut accept)
            && failure.is_none()
        {
            failure = Some(error);
            stop.cancel();
        }
    }
    match failure {
        Some(error) => Err(error),
        None => Ok(()),
    }
}

async fn close_source_physical(
    source: &mut CoreStream,
) -> (Result<(), StepError>, RunTaskPhysicalResult) {
    let physical = source.physical_completion();
    let business = source.close().await;
    let physical = match physical {
        Some(receipt) => receipt
            .wait()
            .await
            .map_err(|_| RunTaskPhysicalError::CompletionUnknown),
        None => Err(RunTaskPhysicalError::CompletionUnknown),
    };
    (business, physical)
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
    fn before_unregistered_child(&self) -> Result<(), StepError> {
        self.task_spawner
            .get()
            .map_or(Ok(()), |spawner| spawner.before_unregistered_child())
    }

    async fn close_source(&self, source: &mut CoreStream) -> Result<(), StepError> {
        // Business/audit close failures are not themselves physical failures.
        // Neither close Ok nor an absent companion certifies actual cleanup.
        let (business, physical) = close_source_physical(source).await;
        if physical.is_err() {
            self.physical_unknown.store(true, Ordering::Release);
        }
        business
    }

    async fn cleanup_scope<'a, F, T>(
        &'a self,
        control: &CoreControl,
        future: F,
    ) -> Result<T, StepError>
    where
        F: Future<Output = Result<T, StepError>> + Send + 'a,
        T: Send + 'a,
    {
        let bytes = RUN_TASK_CONTROL_BYTES
            .checked_add(std::mem::size_of::<F>() as u64)
            .ok_or_else(|| {
                StepError::new("limit-exceeded", "native cleanup scope bound overflow")
            })?;
        let reservation = control.reserve(ControlKind::Frame, bytes).map_err(|_| {
            StepError::new("limit-exceeded", "native cleanup scope capacity exhausted")
        })?;
        CleanupFuture {
            abandonment: CleanupAbandonment {
                spawner: self.task_spawner.get(),
                armed: true,
            },
            future: Box::pin(future),
            _reservation: reservation,
        }
        .await
    }

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
        self.close
            .deliver(&bus.sender, event, &cx.stop, cx.cancel(), cx.deadline())
            .await;
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
        if cx.stop.is_cancelled() {
            return Err(StepError::new("cancelled", "Flow scope cancelled"));
        }
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

#[cfg(test)]
mod task_tests {
    use super::*;
    use std::sync::atomic::AtomicBool;
    use std::time::Duration;
    use tobkiri_canon::{Digest, ids::RequestId};

    fn close_control(control: &CoreControl) -> Arc<RunCloseControl> {
        let reservation = control.reserve(ControlKind::Registration, 4096).unwrap();
        Arc::new(RunCloseControl {
            cancel: CancellationToken::new_with_control(control).unwrap(),
            delivery_stop: CancellationToken::new_with_control(control).unwrap(),
            execution_finished: AtomicBool::new(false),
            _reservation: reservation,
        })
    }
    fn event(sequence: u64) -> RunEvent {
        RunEvent {
            request: RequestId::parse("req_task_join").unwrap(),
            plan_digest: Digest::hash(b"native-task-test"),
            sequence,
            kind: RunEventKind::Started,
        }
    }
    fn native_spawner(control: &CoreControl) -> TokioRunTaskSpawner {
        TokioRunTaskSpawner {
            control: control.clone(),
            runtime: tokio::runtime::Handle::current(),
        }
    }
    fn handle(
        control: &CoreControl,
        close: Arc<RunCloseControl>,
        result: oneshot::Receiver<Result<RunCompletion, StepError>>,
        task_completion: Arc<dyn RunTaskCompletion>,
        events: mpsc::Receiver<RunEvent>,
    ) -> RunHandle {
        let budget = NativeTransportBudget::allocate(control, 4096).unwrap();
        RunHandle {
            events,
            cancel: close.cancel.clone(),
            result: RunResult { receiver: result },
            stream_budget: budget.clone(),
            event_budget: budget,
            max_batch_bytes: 4096,
            task_completion,
            generation_bound: true,
            close_control: close,
        }
    }

    #[tokio::test]
    async fn actual_join_survives_abandoned_wait_and_finishes_after_payload_drop() {
        struct Payload(Arc<AtomicBool>);
        impl Drop for Payload {
            fn drop(&mut self) {
                self.0.store(true, Ordering::Release);
            }
        }
        let control = CoreControl::default();
        let close = close_control(&control);
        let dropped = Arc::new(AtomicBool::new(false));
        let payload = Payload(dropped.clone());
        let (started, observed) = oneshot::channel();
        let (release, blocked) = oneshot::channel();
        let task = RunTask::new(
            async move {
                let _ = started.send(());
                let _ = blocked.await;
                drop(payload);
                Ok(())
            },
            &control,
        )
        .unwrap();
        let physical = native_spawner(&control).spawn(task, close.clone()).unwrap();
        observed.await.unwrap();
        assert!(
            tokio::time::timeout(Duration::from_millis(1), physical.wait())
                .await
                .is_err()
        );
        assert!(!dropped.load(Ordering::Acquire));
        assert_eq!(control.usage().count(ControlKind::Task), 2);
        release.send(()).unwrap();
        physical.wait().await.unwrap();
        physical.wait().await.unwrap();
        assert!(dropped.load(Ordering::Acquire));
        assert_eq!(control.usage().count(ControlKind::Task), 0);
        drop(physical);
        drop(close);
        assert_eq!(control.usage().bytes, 0);
    }

    #[tokio::test]
    async fn eof_and_run_result_cannot_complete_close_before_actual_task_join() {
        let control = CoreControl::default();
        let close = close_control(&control);
        let (events, observer) = mpsc::channel(1);
        let (completion, result) = oneshot::channel();
        let (release, blocked) = oneshot::channel();
        let task = RunTask::new(
            async move {
                let _ = completion.send(Ok(RunCompletion::Intermediate));
                drop(events); // Both EOF and RunResult precede physical termination.
                let _ = blocked.await;
                Ok(())
            },
            &control,
        )
        .unwrap();
        let physical = native_spawner(&control).spawn(task, close.clone()).unwrap();
        let mut stream = handle(&control, close, result, physical, observer).into_stream();
        assert!(
            tokio::time::timeout(Duration::from_millis(1), stream.pull(1))
                .await
                .is_err()
        );
        assert!(
            tokio::time::timeout(Duration::from_millis(1), stream.close())
                .await
                .is_err()
        );
        release.send(()).unwrap();
        stream.close().await.unwrap();
        stream.close().await.unwrap();
    }

    #[tokio::test]
    async fn lost_result_channel_is_preserved_after_the_real_task_joins() {
        let control = CoreControl::default();
        let close = close_control(&control);
        let (_events, observer) = mpsc::channel(1);
        let (completion, result) = oneshot::channel();
        let (started, observed) = oneshot::channel();
        let (release, blocked) = oneshot::channel();
        let task = RunTask::new(
            async move {
                drop(completion);
                let _ = started.send(());
                let _ = blocked.await;
                Ok(())
            },
            &control,
        )
        .unwrap();
        let physical = native_spawner(&control).spawn(task, close.clone()).unwrap();
        let mut stream = handle(&control, close, result, physical, observer).into_stream();
        observed.await.unwrap();
        assert!(
            tokio::time::timeout(Duration::from_millis(1), stream.close())
                .await
                .is_err()
        );
        release.send(()).unwrap();
        for _ in 0..2 {
            let error = stream.close().await.unwrap_err();
            assert_eq!(error.code, "internal");
            assert_eq!(error.message, "Flow completion channel closed");
        }
    }

    #[tokio::test]
    async fn cancelled_result_cannot_mask_physical_task_panic() {
        let control = CoreControl::default();
        let close = close_control(&control);
        let (_events, observer) = mpsc::channel(1);
        let (completion, result) = oneshot::channel();
        let task = RunTask::new(
            async move {
                let _ = completion.send(Err(StepError::new("cancelled", "run cancelled")));
                panic!("fixture actual interpreter panic");
            },
            &control,
        )
        .unwrap();
        let physical = native_spawner(&control).spawn(task, close.clone()).unwrap();
        let mut stream = handle(&control, close, result, physical, observer).into_stream();
        for _ in 0..2 {
            let error = stream.close().await.unwrap_err();
            assert_eq!(error.code, "component");
            assert_eq!(error.message, "Flow task physical completion is unknown");
        }
    }

    #[tokio::test]
    async fn close_releases_full_delivery_queue_before_source_wakeup() {
        let control = CoreControl::default();
        let close = close_control(&control);
        let (sender, _receiver) = mpsc::channel(1);
        sender.send(event(1)).await.unwrap();
        let deadline = Instant::now() + Duration::from_secs(60);
        let delivery = close.deliver(&sender, event(2), &close.cancel, &close.cancel, deadline);
        tokio::pin!(delivery);
        assert!(futures::poll!(delivery.as_mut()).is_pending());
        let observed_close = close.clone();
        let observer = tokio::spawn(async move {
            observed_close.cancel.cancelled().await.unwrap();
            assert!(observed_close.delivery_stop.is_cancelled());
        });
        close.record_close(StreamCloseReason::Manual).unwrap();
        tokio::time::timeout(Duration::from_secs(1), delivery)
            .await
            .unwrap();
        observer.await.unwrap();
    }

    #[tokio::test]
    async fn completed_execution_close_stops_delivery_without_cancelling_final_journal_context() {
        let control = CoreControl::default();
        let close = close_control(&control);
        close.execution_finished.store(true, Ordering::Release);
        close.record_close(StreamCloseReason::Manual).unwrap();
        assert!(close.delivery_stop.is_cancelled());
        assert!(!close.cancel.is_cancelled());
    }

    struct ConservativeSpawner {
        marked: Arc<AtomicBool>,
        reject: bool,
    }
    impl RunTaskSpawner for ConservativeSpawner {
        fn spawn(
            &self,
            _: RunTask,
            _: Arc<dyn StreamCloseControl>,
        ) -> Result<Arc<dyn RunTaskCompletion>, StepError> {
            Err(StepError::new(
                "unavailable",
                "fixture has no native launch",
            ))
        }
        fn before_unregistered_child(&self) -> Result<(), StepError> {
            self.marked.store(true, Ordering::Release);
            if self.reject {
                Err(StepError::new("unavailable", "original generation retired"))
            } else {
                Ok(())
            }
        }
    }

    #[tokio::test]
    async fn pending_source_drop_marks_original_generation_before_destroying_future_even_on_error()
    {
        struct Payload {
            marked: Arc<AtomicBool>,
            dropped: Arc<AtomicBool>,
        }
        impl Drop for Payload {
            fn drop(&mut self) {
                assert!(self.marked.load(Ordering::Acquire));
                self.dropped.store(true, Ordering::Release);
            }
        }
        let control = CoreControl::default();
        let marked = Arc::new(AtomicBool::new(false));
        let dropped = Arc::new(AtomicBool::new(false));
        let spawner: Arc<dyn RunTaskSpawner> = Arc::new(ConservativeSpawner {
            marked: marked.clone(),
            reject: true,
        });
        let payload = Payload {
            marked: marked.clone(),
            dropped: dropped.clone(),
        };
        let (_release, blocked) = oneshot::channel::<()>();
        let mut pending = Box::pin(CleanupFuture {
            abandonment: CleanupAbandonment {
                spawner: Some(&spawner),
                armed: true,
            },
            future: Box::pin(async move {
                let _ = blocked.await;
                drop(payload);
                Ok(())
            }),
            _reservation: control.reserve(ControlKind::Frame, 4096).unwrap(),
        });
        assert!(futures::poll!(pending.as_mut()).is_pending());
        drop(pending);
        assert!(marked.load(Ordering::Acquire));
        assert!(dropped.load(Ordering::Acquire));
        assert_eq!(control.usage().bytes, 0);
    }

    #[tokio::test]
    async fn normally_completed_source_scope_does_not_taint_generation() {
        let control = CoreControl::default();
        let marked = Arc::new(AtomicBool::new(false));
        let spawner: Arc<dyn RunTaskSpawner> = Arc::new(ConservativeSpawner {
            marked: marked.clone(),
            reject: false,
        });
        CleanupFuture {
            abandonment: CleanupAbandonment {
                spawner: Some(&spawner),
                armed: true,
            },
            future: Box::pin(async { Ok(()) }),
            _reservation: control.reserve(ControlKind::Frame, 4096).unwrap(),
        }
        .await
        .unwrap();
        assert!(!marked.load(Ordering::Acquire));
        assert_eq!(control.usage().bytes, 0);
    }

    struct DropObserved(Arc<AtomicBool>);
    impl Drop for DropObserved {
        fn drop(&mut self) {
            self.0.store(true, Ordering::Release);
        }
    }

    #[tokio::test]
    async fn cancellation_retains_the_same_pending_work_until_cleanup_finishes() {
        let control = CoreControl::default();
        let stop = CancellationToken::new_with_control(&control).unwrap();
        let dropped = Arc::new(AtomicBool::new(false));
        let payload = DropObserved(dropped.clone());
        let (release, blocked) = oneshot::channel();
        let mut work = Box::pin(await_retained_work(
            &stop,
            &stop,
            Instant::now() + Duration::from_secs(60),
            async move {
                let _ = blocked.await;
                drop(payload);
                Ok(())
            },
            StepError::new("cancelled", "deadline"),
        ));
        assert!(futures::poll!(work.as_mut()).is_pending());
        stop.cancel();
        assert!(futures::poll!(work.as_mut()).is_pending());
        assert!(!dropped.load(Ordering::Acquire));
        release.send(()).unwrap();
        assert_eq!(work.await.unwrap_err().code, "cancelled");
        assert!(dropped.load(Ordering::Acquire));
    }

    #[tokio::test]
    async fn loop_deadline_retains_unfinished_work_instead_of_dropping_it() {
        let control = CoreControl::default();
        let stop = CancellationToken::new_with_control(&control).unwrap();
        let dropped = Arc::new(AtomicBool::new(false));
        let payload = DropObserved(dropped.clone());
        let (release, blocked) = oneshot::channel();
        let mut work = Box::pin(await_retained_work(
            &stop,
            &stop,
            Instant::now(),
            async move {
                let _ = blocked.await;
                drop(payload);
                Ok(())
            },
            StepError::new("limit-exceeded", "loop wall ceiling exceeded"),
        ));
        // Poll the same future until the elapsed timer has fired, without
        // releasing its actual pending work or using a sleep as ordering.
        while !stop.is_cancelled() {
            assert!(futures::poll!(work.as_mut()).is_pending());
            tokio::task::yield_now().await;
        }
        assert!(!dropped.load(Ordering::Acquire));
        release.send(()).unwrap();
        assert_eq!(work.await.unwrap_err().code, "limit-exceeded");
        assert!(dropped.load(Ordering::Acquire));
    }

    #[tokio::test]
    async fn first_parallel_error_stops_admission_but_drains_started_sibling() {
        let control = CoreControl::default();
        let stop = CancellationToken::new_with_control(&control).unwrap();
        let sibling_started = Arc::new(tokio::sync::Notify::new());
        let created = Arc::new(AtomicU64::new(0));
        let dropped = Arc::new(AtomicBool::new(false));
        let (release, blocked) = oneshot::channel();
        let mut blocked = Some(blocked);
        let pending = stream::iter(0..3)
            .take_while(|_| futures::future::ready(!stop.is_cancelled()))
            .map(|index| {
                created.fetch_add(1, Ordering::AcqRel);
                let started = sibling_started.clone();
                let receiver = if index == 1 { blocked.take() } else { None };
                let payload = (index == 1).then(|| DropObserved(dropped.clone()));
                async move {
                    if index == 0 {
                        started.notified().await;
                        return Err(StepError::new("invalid", "first branch failed"));
                    }
                    if let Some(receiver) = receiver {
                        started.notify_one();
                        let _ = receiver.await;
                        drop(payload);
                    }
                    Ok(index)
                }
            })
            .buffer_unordered(2);
        let mut drained = Box::pin(drain_parallel(pending, &stop, |_| Ok(())));
        while !stop.is_cancelled() {
            assert!(futures::poll!(drained.as_mut()).is_pending());
            tokio::task::yield_now().await;
        }
        assert!(stop.is_cancelled());
        assert_eq!(created.load(Ordering::Acquire), 2);
        assert!(!dropped.load(Ordering::Acquire));
        // The actual sibling still owns its payload and receiver until this
        // release; a dropped sibling would make send fail.
        release.send(()).unwrap();
        let error = drained.await.unwrap_err();
        assert_eq!(error.code, "invalid");
        assert_eq!(error.message, "first branch failed");
        assert_eq!(created.load(Ordering::Acquire), 2);
        assert!(dropped.load(Ordering::Acquire));
    }

    struct PhysicalSource {
        receipt: Option<Arc<dyn RunTaskCompletion>>,
        audit_failure: bool,
    }
    #[async_trait::async_trait]
    impl crate::StreamSource for PhysicalSource {
        async fn pull(&mut self, _: u32) -> Result<CoreValue, StepError> {
            Err(StepError::new("invalid", "fixture has no values"))
        }
        fn cancel(&mut self) {}
        fn physical_completion(&self) -> Option<crate::StreamPhysicalCompletion> {
            self.receipt
                .clone()
                .map(crate::StreamPhysicalCompletion::from_task)
        }
        async fn close(&mut self) -> Result<(), StepError> {
            if self.audit_failure {
                Err(StepError::new(
                    "audit-failure",
                    "fixture audit delivery failed",
                ))
            } else {
                Ok(())
            }
        }
    }

    #[tokio::test]
    async fn audit_error_is_separate_from_the_same_actual_physical_join() {
        let control = CoreControl::default();
        let close = close_control(&control);
        let dropped = Arc::new(AtomicBool::new(false));
        let payload = DropObserved(dropped.clone());
        let (release, blocked) = oneshot::channel();
        let task = RunTask::cleanup(
            async move {
                let _ = blocked.await;
                drop(payload);
                Ok(())
            },
            &control,
        )
        .unwrap();
        let receipt = native_spawner(&control).spawn(task, close).unwrap();
        let mut source = CoreStream::new(PhysicalSource {
            receipt: Some(receipt.clone()),
            audit_failure: true,
        });
        // Abandoning the close waiter cannot abandon/relaunch the actual work.
        {
            let mut waiting = Box::pin(close_source_physical(&mut source));
            assert!(futures::poll!(waiting.as_mut()).is_pending());
        }
        assert!(!dropped.load(Ordering::Acquire));
        release.send(()).unwrap();
        let (business, physical) = close_source_physical(&mut source).await;
        assert_eq!(business.unwrap_err().code, "audit-failure");
        physical.unwrap();
        receipt.wait().await.unwrap();
        assert!(dropped.load(Ordering::Acquire));
    }

    #[tokio::test]
    async fn successful_close_without_a_companion_never_proves_physical_completion() {
        let mut source = CoreStream::new(PhysicalSource {
            receipt: None,
            audit_failure: false,
        });
        let (business, physical) = close_source_physical(&mut source).await;
        business.unwrap();
        assert_eq!(physical, Err(RunTaskPhysicalError::CompletionUnknown));
    }

    #[tokio::test]
    async fn typed_physical_unknown_survives_successful_business_completion() {
        let control = CoreControl::default();
        let close = close_control(&control);
        let (_events, observer) = mpsc::channel(1);
        let (completion, result) = oneshot::channel();
        let task = RunTask::cleanup(
            async move {
                let _ = completion.send(Ok(RunCompletion::Intermediate));
                Err(RunTaskPhysicalError::CompletionUnknown)
            },
            &control,
        )
        .unwrap();
        let receipt = native_spawner(&control).spawn(task, close.clone()).unwrap();
        let mut stream = handle(&control, close, result, receipt.clone(), observer).into_stream();
        for _ in 0..2 {
            assert_eq!(stream.close().await.unwrap_err().code, "component");
            assert_eq!(receipt.wait().await.unwrap_err().code, "component");
        }
    }

    #[tokio::test]
    async fn ordinary_business_close_does_not_turn_unknown_receipt_into_success() {
        let control = CoreControl::default();
        let close = close_control(&control);
        let (_events, observer) = mpsc::channel(1);
        let (completion, result) = oneshot::channel();
        let task = RunTask::cleanup(
            async move {
                let _ = completion.send(Ok(RunCompletion::Intermediate));
                Err(RunTaskPhysicalError::CompletionUnknown)
            },
            &control,
        )
        .unwrap();
        let receipt = native_spawner(&control).spawn(task, close.clone()).unwrap();
        let mut run = handle(&control, close, result, receipt.clone(), observer);
        run.generation_bound = false;
        let mut stream = run.into_stream();
        stream.close().await.unwrap();
        assert_eq!(
            stream
                .physical_completion()
                .unwrap()
                .wait()
                .await
                .unwrap_err()
                .code,
            "component"
        );
        assert_eq!(receipt.wait().await.unwrap_err().code, "component");
    }

    #[tokio::test]
    async fn failed_business_result_can_have_a_known_actual_physical_join() {
        let control = CoreControl::default();
        let close = close_control(&control);
        let (_events, observer) = mpsc::channel(1);
        let (completion, result) = oneshot::channel();
        let task = RunTask::cleanup(
            async move {
                let _ = completion.send(Err(StepError::new("denied", "fixture business failure")));
                Ok(())
            },
            &control,
        )
        .unwrap();
        let receipt = native_spawner(&control).spawn(task, close.clone()).unwrap();
        let mut stream = handle(&control, close, result, receipt.clone(), observer).into_stream();
        assert_eq!(stream.close().await.unwrap_err().code, "denied");
        receipt.wait().await.unwrap();
        stream.physical_completion().unwrap().wait().await.unwrap();
    }
}
