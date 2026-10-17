use std::{
    collections::{BTreeMap, BTreeSet},
    future::Future,
    pin::Pin,
    sync::{
        Arc, Mutex, Weak,
        atomic::{AtomicU64, Ordering},
    },
    task::{Context, Poll},
    time::{Duration, Instant},
};

use crate::execution_frames::ExecutionFactory;
use tobkiri_canon::{
    AllocationBudget, AllocationError, AllocationRequest, CanonValue, ControlHandle, CoreBuffer,
    CoreBufferBuilder, CoreValue, Digest,
    ids::{InputSourceId, OpName, PackHash, PackId, RequestId, ShapeRef, UiSessionId},
};
use tobkiri_flow::{
    CoreStream, FinalOutputKind, FinalPortOutput, FlowEngine, FlowRunner, FlowUserInvocation,
    GuestFlowFrame, PortInvoker, RunEvent, RunHandle, RunJournal, StepError,
};
use tobkiri_gate::{
    BudgetContext, CallCx, CancellationToken, ControlKind, ControlReservation, DispatchPermit,
    EffectRequest, Gate, GateEngine, GateError, HostIo, IngressRoute, Outcome, Principal,
};
use tobkiri_profile::{CompiledInputEntry, CompiledProfile, ResolvedFlow};
use tokio_util::sync::CancellationToken as TokioCancellationToken;

use crate::CoreError;

/// The serialized result for a channel bound to a Core UI session.
#[derive(Clone, Debug, serde::Serialize, serde::Deserialize)]
#[serde(tag = "kind", rename_all = "lowercase", deny_unknown_fields)]
pub enum UiCallResult {
    /// A complete canonical value.
    Value { value: CanonValue },
    /// An opaque Core-owned handle scoped to the requesting UI session.
    Stream { handle: u64 },
}

/// A native Input result paired with the private proof of its actual physical
/// source read. The payload cannot be submitted after detaching that evidence.
pub struct NativeInput {
    source: InputSourceId,
    view: ShapeRef,
    operation: OpName,
    context: CallCx,
    payload: CoreValue,
    receipt: tobkiri_gate::SourceReadReceipt,
}

/// Progress from one destination in a frozen NativeInput trigger plan.
/// `route_index` indexes the exact route list exposed by the run handle.
pub struct NativeInputEvent {
    pub route_index: u32,
    pub event: RunEvent,
}

/// Overall NativeInput fanout status, computed without reparsing its output.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum NativeInputStatus {
    Succeeded,
    Failed,
    Cancelled,
}

/// Final status for one route. No route is omitted: routes that could not be
/// started after cancellation or expiry are explicitly marked Unstarted.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum NativeInputRouteStatus {
    Succeeded,
    Denied,
    Failed,
    Cancelled,
    Unstarted,
    RetentionLimit,
}

/// A compact, route-ordered outcome summary for one frozen destination.
#[derive(Clone, Copy, Debug)]
pub struct NativeInputRouteResult {
    pub route_index: u32,
    pub status: NativeInputRouteStatus,
    /// Stable audit-safe code only; guest error text is never copied here.
    pub error_code: Option<&'static str>,
}

/// Settled fanout result plus the bounded native envelope containing each
/// route's status and any producer-encoded output values.
pub struct NativeInputCompletion {
    pub status: NativeInputStatus,
    pub routes: Vec<NativeInputRouteResult>,
    pub buffer: CoreBuffer,
    _control: Arc<NativeInputRunControl>,
}

/// Awaitable completion for one NativeInput fanout.
pub struct NativeInputRunResult(
    tokio::sync::oneshot::Receiver<Result<NativeInputCompletion, StepError>>,
);
impl Future for NativeInputRunResult {
    type Output = Result<NativeInputCompletion, StepError>;

    fn poll(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
        match Pin::new(&mut self.0).poll(cx) {
            Poll::Pending => Poll::Pending,
            Poll::Ready(Ok(result)) => Poll::Ready(result),
            Poll::Ready(Err(_)) => Poll::Ready(Err(StepError::new(
                "internal",
                "NativeInput completion channel closed",
            ))),
        }
    }
}

/// One sequential, ordered run over all destinations in the frozen Input plan.
pub struct NativeInputRunHandle {
    pub events: tokio::sync::mpsc::Receiver<NativeInputEvent>,
    pub cancel: CancellationToken,
    pub result: NativeInputRunResult,
    control: Arc<NativeInputRunControl>,
}
impl NativeInputRunHandle {
    /// Exact immutable destination list, in Profile order.
    pub fn routes(&self) -> &[IngressRoute] {
        &self.control.routes
    }
}

const NATIVE_INPUT_EVENT_QUEUE: usize = 32;
const NATIVE_INPUT_RETAINED_OUTPUT_MAX: usize = 4 * 1024 * 1024;
const NATIVE_INPUT_ENVELOPE_MAX: usize = 5 * 1024 * 1024;

struct NativeInputRunControl {
    routes: Vec<IngressRoute>,
    _reservation: ControlReservation,
    _event_queue: ControlReservation,
    _retained_output: ControlReservation,
    _waiter: ControlReservation,
    _task: ControlReservation,
    transport_budget: Arc<dyn AllocationBudget>,
}

/// Native transport allocation budget for the final bounded route envelope.
/// Guest outputs remain separately owned by their producing Flow budgets.
struct NativeInputTransportBudget {
    limit: u64,
    used: AtomicU64,
    _reservation: ControlReservation,
}
impl NativeInputTransportBudget {
    fn new(
        control: &tobkiri_gate::CoreControl,
        max_buffer_bytes: usize,
    ) -> Result<Arc<dyn AllocationBudget>, CoreError> {
        let limit = u64::try_from(max_buffer_bytes)
            .ok()
            .and_then(|bytes| bytes.checked_mul(2))
            .ok_or_else(|| CoreError::Invalid("NativeInput envelope bound overflow".into()))?;
        let reservation_bytes = limit
            .checked_add(std::mem::size_of::<Self>() as u64)
            .ok_or_else(|| CoreError::Invalid("NativeInput control bound overflow".into()))?;
        let reservation = control.reserve(ControlKind::Queue, reservation_bytes)?;
        Ok(Arc::new(Self {
            limit,
            used: AtomicU64::new(0),
            _reservation: reservation,
        }))
    }
}
impl AllocationBudget for NativeInputTransportBudget {
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

/// Sole owner of one already-consumed FlowRun or virtual-ingress permit while
/// Core prepares/executes the exact entry. A frame may borrow the permit only
/// synchronously during prepared verification; terminalization consumes this
/// wrapper so the same grant cannot acquire a second outcome owner.
pub(crate) struct ConsumedEntryTerminal {
    permit: DispatchPermit,
    gate: Arc<GateEngine>,
    proposal: Option<Outcome>,
    finished: bool,
}
impl ConsumedEntryTerminal {
    pub(crate) fn new(permit: DispatchPermit, gate: Arc<GateEngine>) -> Self {
        Self {
            permit,
            gate,
            proposal: None,
            finished: false,
        }
    }

    /// Borrow the exact consumed permit for Core's synchronous frame verifier.
    pub(crate) fn permit(&self) -> &DispatchPermit {
        &self.permit
    }

    /// Supervise the original lease before execution or any other fallible
    /// post-consume work. The owner retains this wrapper if supervision fails
    /// and must still submit one terminal proposal.
    pub(crate) fn watch(&self, cx: &CallCx) -> Result<(), GateError> {
        if self.finished {
            return Err(GateError::Consumed);
        }
        self.gate.watch_lease(cx, &self.permit)
    }

    /// Submit the immutable terminal proposal. Once the original deadline is
    /// due, Gate settles the deadline family from this exact permit instead of
    /// accepting a caller-selected cancellation or success outcome.
    pub(crate) async fn finish(&mut self, outcome: Outcome) -> Result<(), GateError> {
        if let Some(proposal) = &self.proposal {
            if proposal != &outcome {
                return Err(GateError::Denied(
                    "consumed Flow terminal proposal is immutable".into(),
                ));
            }
        } else {
            self.proposal = Some(outcome.clone());
        }
        if self.finished {
            return Ok(());
        }
        if std::time::Instant::now() >= self.permit.deadline() {
            self.gate.settle_deadline(&self.permit).await.map(|_| ())
        } else {
            self.gate
                .record_outcome(self.permit.id().clone(), outcome)
                .await
        }?;
        self.finished = true;
        Ok(())
    }
}
/// Maximum number of events accepted by one UI stream pull.
pub const UI_STREAM_MAX_BATCH: u32 = 32;
const UI_STREAMS_PER_FRAME: usize = 64;
const UI_STREAMS_TOTAL: usize = 1024;
const UI_STREAM_MAX_BUFFER_BYTES: usize = 1024 * 1024;
struct UiStreamEntry {
    ui: UiSessionId,
    cx: CallCx,
    external_cancel: TokioCancellationToken,
    state: tokio::sync::Mutex<Option<UiStreamState>>,
}
struct UiStreamState {
    stream: CoreStream,
    output_bytes: u64,
    pending_buffer: Option<tobkiri_canon::CoreBuffer>,
}
#[derive(Clone)]
pub(crate) struct UiMaterial {
    pub entry: String,
    pub files: BTreeMap<String, Vec<u8>>,
}

struct UiPrincipal {
    pack: PackId,
    hash: PackHash,
    ports: BTreeSet<ShapeRef>,
    host_session: UiSessionId,
    /// Filled exactly once by the trusted launcher handshake before this
    /// attachment is published to its renderer.
    generation: Option<u64>,
    /// Core-derived from the actual Core-minted session token. This is an
    /// attachment binding, never a caller-selected principal or authority.
    receiver: Digest,
    cancel: CancellationToken,
}

pub(crate) mod provider_entry;
#[cfg(test)]
#[path = "session_tests.rs"]
mod tests;
mod ui;
mod user_ports;
pub use user_ports::{UserPortOutput, UserStreamHandle};

/// A Core-minted invocation token paired with the trusted host's rendered frame.
#[derive(Clone, Debug)]
pub struct UiAttachment {
    pub session: UiSessionId,
    pub host_session: UiSessionId,
    pub generation: Option<u64>,
    pub receiver: Digest,
    pub pack: PackId,
    pub hash: PackHash,
    pub entry: String,
}

/// Derive a stable renderer-receiver binding from the actual Core session ID.
/// The caller cannot supply either the domain or bytes used for this identity.
pub(crate) fn ui_receiver_identity(session: &UiSessionId) -> Digest {
    const DOMAIN: &[u8] = b"tobkiri.core-ui-receiver/1\0";
    let value = session.as_str().as_bytes();
    let mut bytes = [0u8; 384];
    let domain_end = DOMAIN.len();
    bytes[..domain_end].copy_from_slice(DOMAIN);
    let length_end = domain_end + 8;
    bytes[domain_end..length_end].copy_from_slice(&(value.len() as u64).to_le_bytes());
    let end = length_end + value.len();
    bytes[length_end..end].copy_from_slice(value);
    Digest::hash(&bytes[..end])
}

/// A running, immutable Profile snapshot. Input and UI identities are minted by
/// Core and resolved from this snapshot, independently of any payload fields.
#[derive(Clone)]
pub struct Session {
    state: Arc<SessionState>,
    runner: Arc<dyn FlowRunner>,
}
#[doc(hidden)]
pub struct SessionState {
    pub(crate) compiled: Arc<CompiledProfile>,
    /// Exact committed Profile/User document revision approved when this
    /// Session started. Kept distinct from the compiled execution plan hash.
    user_document: Option<Digest>,
    pub(crate) gate: Arc<GateEngine>,
    native_ingress: crate::inputs::NativeIngresses,
    pub(crate) invoker: Arc<dyn PortInvoker>,
    pub(crate) host: Arc<dyn HostIo>,
    pub(crate) engine: Arc<FlowEngine>,
    execution_factory: Option<Arc<ExecutionFactory>>,
    // PackHost retains this same bridge Arc; this field makes Session's
    // registration lifetime explicit and keeps its native control reservation
    // through shutdown, even while a host retains the invoker.
    _provider_bridge: Option<Arc<dyn tobkiri_wasm::ProviderEntryBridge>>,
    ui: Mutex<BTreeMap<UiSessionId, UiPrincipal>>,
    epoch: u64,
    shutdown: CancellationToken,
    materials: Mutex<BTreeMap<(PackId, PackHash), UiMaterial>>,
    streams: Mutex<BTreeMap<u64, Arc<UiStreamEntry>>>,
    user_streams: Arc<user_ports::Registry>,
    next_stream: AtomicU64,
    runtime: tokio::runtime::Handle,
    admission: crate::admission::Barrier,
}
impl std::ops::Deref for Session {
    type Target = SessionState;
    fn deref(&self) -> &Self::Target {
        &self.state
    }
}
#[async_trait::async_trait]
impl FlowRunner for Session {
    async fn run_provided_flow(
        &self,
        cx: &CallCx,
        caller: &ControlHandle<dyn tobkiri_flow::ActiveCodeFrame>,
        node: &tobkiri_flow::ReachedNode,
        target: ControlHandle<ResolvedFlow>,
        input: CoreValue,
    ) -> Result<CoreStream, StepError> {
        self.state
            .run_provided_flow(cx, caller, node, target, input)
            .await
    }

    async fn run_provided_flow_canonical(
        &self,
        cx: &CallCx,
        caller: &ControlHandle<dyn tobkiri_flow::ActiveCodeFrame>,
        node: &tobkiri_flow::ReachedNode,
        flow_key: &str,
        args_digest: &tobkiri_canon::Digest,
        input: CoreValue,
    ) -> Result<CoreStream, StepError> {
        self.state
            .run_provided_flow_canonical(cx, caller, node, flow_key, args_digest, input)
            .await
    }

    async fn run_guest_flow(
        &self,
        cx: &CallCx,
        provider: &dyn GuestFlowFrame,
        requested_shape: &ShapeRef,
        arguments_digest: &Digest,
        input: CoreValue,
    ) -> Result<CoreStream, StepError> {
        self.state
            .run_guest_flow(cx, provider, requested_shape, arguments_digest, input)
            .await
    }
}

struct SessionRunner(Weak<SessionState>);
#[async_trait::async_trait]
impl FlowRunner for SessionRunner {
    async fn run_provided_flow(
        &self,
        cx: &CallCx,
        caller: &ControlHandle<dyn tobkiri_flow::ActiveCodeFrame>,
        node: &tobkiri_flow::ReachedNode,
        target: ControlHandle<ResolvedFlow>,
        input: CoreValue,
    ) -> Result<CoreStream, StepError> {
        let state = self
            .0
            .upgrade()
            .ok_or_else(|| StepError::new("unavailable", "Session closed"))?;
        state
            .run_provided_flow(cx, caller, node, target, input)
            .await
    }

    async fn run_provided_flow_canonical(
        &self,
        cx: &CallCx,
        caller: &ControlHandle<dyn tobkiri_flow::ActiveCodeFrame>,
        node: &tobkiri_flow::ReachedNode,
        flow_key: &str,
        args_digest: &tobkiri_canon::Digest,
        input: CoreValue,
    ) -> Result<CoreStream, StepError> {
        let state = self
            .0
            .upgrade()
            .ok_or_else(|| StepError::new("unavailable", "Session closed"))?;
        state
            .run_provided_flow_canonical(cx, caller, node, flow_key, args_digest, input)
            .await
    }

    async fn run_guest_flow(
        &self,
        cx: &CallCx,
        provider: &dyn GuestFlowFrame,
        requested_shape: &ShapeRef,
        arguments_digest: &Digest,
        input: CoreValue,
    ) -> Result<CoreStream, StepError> {
        let state = self
            .0
            .upgrade()
            .ok_or_else(|| StepError::new("unavailable", "Session closed"))?;
        state
            .run_guest_flow(cx, provider, requested_shape, arguments_digest, input)
            .await
    }
}

impl Session {
    /// Enabled input Pack identifiers from this frozen Profile.
    pub fn input_sources(&self) -> Vec<InputSourceId> {
        self.compiled
            .input_entries()
            .iter()
            .map(|entry| {
                InputSourceId::parse(entry.source_pack().as_str())
                    .expect("validated Pack ID is an input source ID")
            })
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect()
    }

    /// Input shapes consumed by this Session's Flows.
    pub fn input_shapes(&self) -> Vec<ShapeRef> {
        self.compiled
            .input_entries()
            .iter()
            .map(|entry| entry.exact_input_view().clone())
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect()
    }

    /// Frozen physical Input selections. Callers select one complete
    /// source/view/operation record; these fields are never independently
    /// cross-combined into a new endpoint.
    pub fn input_entries(&self) -> &[CompiledInputEntry] {
        self.compiled.input_entries()
    }

    /// Ask an enabled input Pack for a payload through the same gated port path.
    /// The CLI does not manufacture input on behalf of a Pack or read around it.
    pub async fn read_input(
        &self,
        source: InputSourceId,
        shape: ShapeRef,
        operation: OpName,
    ) -> Result<NativeInput, CoreError> {
        self.read_input_with_cancel(source, shape, operation, TokioCancellationToken::new())
            .await
    }

    /// Read an exact frozen Input entry while keeping cancellation attached to
    /// the physical source invocation until its cleanup and terminal receipt
    /// complete. Dropping the host future after a grant was consumed would lose
    /// the source terminal owner, so cancellation signals the CallCx and then
    /// continues polling the same read to its acknowledged end.
    pub async fn read_input_with_cancel(
        &self,
        source: InputSourceId,
        shape: ShapeRef,
        operation: OpName,
        external_cancel: TokioCancellationToken,
    ) -> Result<NativeInput, CoreError> {
        if external_cancel.is_cancelled() {
            return Err(CoreError::Step(StepError::new(
                "cancelled",
                "NativeInput read cancelled before admission",
            )));
        }
        self.ensure_started().await?;
        self.check_snapshot()?;
        let source_pack = PackId::parse(source.as_str())?;
        let entry = self
            .compiled
            .input_entry(&source_pack, &shape, &operation)
            .ok_or_else(|| {
                CoreError::Invalid(
                    "physical Input view and operation do not match a frozen Profile entry".into(),
                )
            })?;
        let deadline = deadline_after(self.compiled.profile.ceiling.wasm.wall_ms)?;
        let budget = BudgetContext::new(self.compiled.profile.ceiling.loops.clone(), deadline)
            .map_err(|error| CoreError::Invalid(error.to_string()))?;
        let cx = CallCx::try_root_with_control(
            self.compiled.profile.id.clone(),
            Principal::Input {
                source: source.clone(),
            },
            RequestId::new(format!("req_{}", ulid::Ulid::new()))?,
            self.shutdown.child_with_control(self.gate.control())?,
            deadline,
            self.compiled.profile.ceiling.wasm.clone(),
            self.gate.control().clone(),
        )?
        .with_security_epoch(self.epoch)
        .with_revision(self.compiled.plan_digest.clone())
        .with_budget(budget)?;
        if external_cancel.is_cancelled() {
            return Err(CoreError::Step(StepError::new(
                "cancelled",
                "NativeInput read cancelled before admission",
            )));
        }
        let mut read = Box::pin(self.invoker.read_native_input(&cx, entry));
        let completed = tokio::select! {
            result = &mut read => Some(result),
            _ = external_cancel.cancelled() => {
                cx.cancel().cancel();
                None
            }
        };
        let read_result = match completed {
            Some(result) => result,
            // Keep polling the same physical read after cancellation so its
            // provider cleanup and terminal receipt are not abandoned.
            None => (&mut read).await,
        };
        let (payload, receipt) = read_result.map_err(CoreError::from)?;
        // The future borrows `cx`; release that borrow before storing `cx`
        // in the returned NativeInput. It has already completed (including
        // cancellation cleanup) at this point.
        drop(read);
        let evidence = receipt.metadata();
        if evidence.source != source || evidence.view != shape || evidence.op != operation {
            return Err(CoreError::Invalid(
                "Input endpoint does not match its sealed physical-read receipt".into(),
            ));
        }
        Ok(NativeInput {
            source,
            view: shape,
            operation,
            context: cx,
            payload,
            receipt,
        })
    }
    pub(crate) fn new_with_ingress(
        compiled: Arc<CompiledProfile>,
        gate: Arc<GateEngine>,
        invoker: Arc<dyn PortInvoker>,
        journal: Arc<dyn RunJournal>,
        host: Arc<dyn HostIo>,
        epoch: u64,
        user_document: Digest,
        shutdown: CancellationToken,
        native_ingress: crate::inputs::NativeIngresses,
        execution_factory: ExecutionFactory,
        provider_bridge: Arc<dyn tobkiri_wasm::ProviderEntryBridge>,
    ) -> Self {
        Self::new_inner(
            compiled,
            gate,
            invoker,
            journal,
            host,
            epoch,
            Some(user_document),
            shutdown,
            native_ingress,
            Some(execution_factory),
            Some(provider_bridge),
        )
    }

    fn new_inner(
        compiled: Arc<CompiledProfile>,
        gate: Arc<GateEngine>,
        invoker: Arc<dyn PortInvoker>,
        journal: Arc<dyn RunJournal>,
        host: Arc<dyn HostIo>,
        epoch: u64,
        user_document: Option<Digest>,
        shutdown: CancellationToken,
        native_ingress: crate::inputs::NativeIngresses,
        execution_factory: Option<ExecutionFactory>,
        provider_bridge: Option<Arc<dyn tobkiri_wasm::ProviderEntryBridge>>,
    ) -> Self {
        let outcomes = Arc::new(Mutex::new(BTreeMap::new()));
        let journal = Arc::new(crate::completion::CompletionJournal {
            inner: journal,
            gate: gate.clone(),
            pending: outcomes,
        });
        let admission = crate::admission::Barrier::new(
            compiled.profile.id.clone(),
            gate.clone(),
            shutdown.clone(),
        );
        let state = Arc::new(SessionState {
            admission,
            compiled: compiled.clone(),
            user_document,
            gate,
            native_ingress,
            engine: Arc::new(
                FlowEngine::new(invoker.clone(), journal).with_compiled_profile(compiled.clone()),
            ),
            execution_factory: execution_factory.map(Arc::new),
            _provider_bridge: provider_bridge,
            invoker,
            host,
            ui: Mutex::new(BTreeMap::new()),
            epoch,
            shutdown,
            materials: Mutex::new(BTreeMap::new()),
            streams: Mutex::new(BTreeMap::new()),
            user_streams: Arc::new(user_ports::Registry::default()),
            next_stream: AtomicU64::new(1),
            runtime: tokio::runtime::Handle::current(),
        });
        let runner: Arc<dyn FlowRunner> = Arc::new(SessionRunner(Arc::downgrade(&state)));
        state
            .engine
            .set_flow_runner(Arc::downgrade(&runner))
            .expect("fresh engine runner slot");
        Self { state, runner }
    }
    pub(crate) async fn ensure_started(&self) -> Result<(), CoreError> {
        self.admission.ensure_ready().await
    }
    pub(crate) fn weak_runner(&self) -> Weak<dyn FlowRunner> {
        Arc::downgrade(&self.runner)
    }
    pub(crate) fn install_ui_material(
        &self,
        pack: PackId,
        hash: PackHash,
        material: UiMaterial,
    ) -> Result<(), CoreError> {
        self.materials
            .lock()
            .map_err(|_| CoreError::Runtime("UI materials poisoned".into()))?
            .insert((pack, hash), material);
        Ok(())
    }

    /// Submit a trusted input Pack's declared shape through the gate and Flow.
    pub async fn submit(
        &self,
        source: InputSourceId,
        input_shape: ShapeRef,
        payload: CoreValue,
    ) -> Result<RunHandle, CoreError> {
        let wall = self.compiled.profile.ceiling.wasm.wall_ms;
        self.submit_before(source, input_shape, payload, deadline_after(wall)?)
            .await
    }

    /// Route only the opaque result returned by this Session's verified
    /// physical Input read. Payload-only submission remains unavailable.
    pub async fn submit_native_input(
        &self,
        input: NativeInput,
    ) -> Result<NativeInputRunHandle, CoreError> {
        self.ensure_started().await?;
        self.check_snapshot()?;
        self.state.submit_native_input(input).await
    }

    /// Preserve an input's original deadline; this API can only narrow it to the
    /// Profile ceiling. Cancellation propagates through every nested operation.
    pub async fn submit_before(
        &self,
        source: InputSourceId,
        input_shape: ShapeRef,
        payload: CoreValue,
        original_deadline: Instant,
    ) -> Result<RunHandle, CoreError> {
        let _ = (source, input_shape, payload, original_deadline);
        Err(CoreError::Step(StepError::new(
            "unavailable",
            "Input routing requires a verified source-read receipt and exact ingress policy",
        )))
    }

    fn check_snapshot(&self) -> Result<(), CoreError> {
        if self.shutdown.is_cancelled() {
            return Err(CoreError::Invalid("Session closed".into()));
        }
        if self.gate.epoch(&self.compiled.profile.id)? != self.epoch {
            return Err(CoreError::Invalid(
                "Profile changed; start a fresh Session before submitting input".into(),
            ));
        }
        Ok(())
    }

    /// Invoke a declared repair or utility port from a trusted Core user action.
    /// User authority is the explicit User policy intersected with the ceiling
    /// and its document/revision/epoch approval; callees retain their own policy.
    pub async fn call_user_port(
        &self,
        shape: ShapeRef,
        operation: OpName,
        args: &CanonValue,
    ) -> Result<tobkiri_canon::CoreBuffer, CoreError> {
        self.call_user_value(shape, operation, args).await
    }

    /// The digest used by every run in this Session.
    pub fn plan_digest(&self) -> &tobkiri_canon::Digest {
        &self.compiled.plan_digest
    }
}

fn deadline_after(wall_ms: u64) -> Result<Instant, CoreError> {
    Instant::now()
        .checked_add(Duration::from_millis(wall_ms))
        .ok_or_else(|| CoreError::Invalid("wall ceiling exceeds the supported clock range".into()))
}

fn step_outcome(error: &StepError) -> Outcome {
    if error.code == "cancelled" {
        Outcome::Cancelled
    } else {
        Outcome::Failed {
            code: error.audit_code().into(),
        }
    }
}

fn gate_step_error(error: GateError) -> StepError {
    StepError::new(error.code(), "FlowRun admission or lifecycle check failed")
}

fn native_allocation_error(error: AllocationError) -> StepError {
    StepError::new(
        match error {
            AllocationError::Limit | AllocationError::Overflow | AllocationError::Depth => {
                "limit-exceeded"
            }
            AllocationError::Invalid => "invalid",
            _ => "unavailable",
        },
        "bounded NativeInput output allocation failed",
    )
}

fn stable_gate_error(error: &GateError) -> &'static str {
    match error.code() {
        "denied" => "denied",
        "approval-required" => "approval-required",
        "cancelled" => "cancelled",
        "deadline" => "deadline",
        "not-found" => "not-found",
        "invalid" => "invalid",
        "limit-exceeded" => "limit-exceeded",
        "conflict" => "conflict",
        "stale-epoch" => "conflict",
        "unavailable" => "unavailable",
        _ => "internal",
    }
}

fn native_status_name(status: NativeInputStatus) -> &'static [u8] {
    match status {
        NativeInputStatus::Succeeded => b"succeeded",
        NativeInputStatus::Failed => b"failed",
        NativeInputStatus::Cancelled => b"cancelled",
    }
}

fn native_route_status_name(status: NativeInputRouteStatus) -> &'static [u8] {
    match status {
        NativeInputRouteStatus::Succeeded => b"succeeded",
        NativeInputRouteStatus::Denied => b"denied",
        NativeInputRouteStatus::Failed => b"failed",
        NativeInputRouteStatus::Cancelled => b"cancelled",
        NativeInputRouteStatus::Unstarted => b"unstarted",
        NativeInputRouteStatus::RetentionLimit => b"retention-limit",
    }
}

fn append_u32(builder: &mut CoreBufferBuilder, value: u32) -> Result<(), StepError> {
    let mut bytes = [0u8; 10];
    let mut at = bytes.len();
    let mut value = value;
    loop {
        at -= 1;
        bytes[at] = b'0' + (value % 10) as u8;
        value /= 10;
        if value == 0 {
            break;
        }
    }
    builder
        .append(&bytes[at..])
        .map_err(native_allocation_error)
}

fn native_input_envelope(
    budget: &Arc<dyn AllocationBudget>,
    status: NativeInputStatus,
    routes: &[NativeInputRouteResult],
    outputs: &[Option<CoreBuffer>],
) -> Result<CoreBuffer, StepError> {
    if routes.len() != outputs.len() {
        return Err(StepError::new(
            "internal",
            "NativeInput result index mismatch",
        ));
    }
    let mut builder = CoreBuffer::builder(budget, NATIVE_INPUT_ENVELOPE_MAX);
    builder
        .append(b"{\"routes\":[")
        .map_err(native_allocation_error)?;
    for (index, (route, output)) in routes.iter().zip(outputs).enumerate() {
        if index != 0 {
            builder.append(b",").map_err(native_allocation_error)?;
        }
        builder
            .append(b"{\"error\":")
            .map_err(native_allocation_error)?;
        if let Some(error) = route.error_code {
            builder.append(b"\"").map_err(native_allocation_error)?;
            builder
                .append(error.as_bytes())
                .map_err(native_allocation_error)?;
            builder.append(b"\"").map_err(native_allocation_error)?;
        } else {
            builder.append(b"null").map_err(native_allocation_error)?;
        }
        builder
            .append(b",\"index\":")
            .map_err(native_allocation_error)?;
        append_u32(&mut builder, route.route_index)?;
        builder
            .append(b",\"result\":")
            .map_err(native_allocation_error)?;
        if let Some(output) = output {
            builder
                .append(output.as_bytes())
                .map_err(native_allocation_error)?;
        } else {
            builder.append(b"null").map_err(native_allocation_error)?;
        }
        builder
            .append(b",\"status\":\"")
            .map_err(native_allocation_error)?;
        builder
            .append(native_route_status_name(route.status))
            .map_err(native_allocation_error)?;
        builder.append(b"\"}").map_err(native_allocation_error)?;
    }
    builder
        .append(b"],\"status\":\"")
        .map_err(native_allocation_error)?;
    builder
        .append(native_status_name(status))
        .map_err(native_allocation_error)?;
    builder.append(b"\"}").map_err(native_allocation_error)?;
    builder.finish().map_err(native_allocation_error)
}

async fn finish_terminal_retry(terminal: &mut ConsumedEntryTerminal, outcome: Outcome) {
    loop {
        match terminal.finish(outcome.clone()).await {
            Ok(()) => return,
            Err(_) => tokio::time::sleep(Duration::from_millis(50)).await,
        }
    }
}

fn native_parent_cancelled(cancel: &CancellationToken, input: &NativeInput) -> bool {
    cancel.is_cancelled() || input.context.cancel().is_cancelled()
}

fn native_step_error_code(error: &StepError, input: &NativeInput) -> &'static str {
    if error.code == "cancelled" && matches!(input.context.check_active(), Err(GateError::Deadline))
    {
        "deadline"
    } else {
        error.audit_code()
    }
}

fn native_stop_reason(
    cancel: &CancellationToken,
    input: &NativeInput,
    gate: &GateEngine,
) -> Option<&'static str> {
    if let Err(error) = input.context.check_active() {
        return Some(match error {
            GateError::Deadline => "deadline",
            GateError::Cancelled => "cancelled",
            GateError::Limit(_) => "limit-exceeded",
            GateError::StaleEpoch => "conflict",
            _ => "unavailable",
        });
    }
    if native_parent_cancelled(cancel, input) {
        return Some("cancelled");
    }
    match gate.epoch(input.context.profile()) {
        Ok(epoch) if epoch == input.context.security_epoch() => None,
        Ok(_) => Some("conflict"),
        Err(_) => Some("unavailable"),
    }
}

async fn run_native_fanout(
    compiled: Arc<CompiledProfile>,
    gate: Arc<GateEngine>,
    engine: Arc<FlowEngine>,
    execution_factory: Option<Arc<ExecutionFactory>>,
    source_pack: PackId,
    input: NativeInput,
    mut routing_terminal: ConsumedEntryTerminal,
    control: Arc<NativeInputRunControl>,
    mut summaries: Vec<NativeInputRouteResult>,
    mut outputs: Vec<Option<CoreBuffer>>,
    event_tx: tokio::sync::mpsc::Sender<NativeInputEvent>,
    cancel: CancellationToken,
) -> Result<NativeInputCompletion, StepError> {
    let entry = match compiled.input_entry(&source_pack, &input.view, &input.operation) {
        Some(entry) => entry,
        None => {
            // The route permit has already been consumed by the time this task
            // starts. Even an immutable-catalog invariant failure must settle
            // that exact grant instead of dropping its sole terminal owner.
            finish_terminal_retry(
                &mut routing_terminal,
                Outcome::Failed {
                    code: "input-entry-unavailable".into(),
                },
            )
            .await;
            return Err(StepError::new(
                "invalid",
                "Input entry left the frozen catalog",
            ));
        }
    };

    let mut retained_bytes = 0usize;
    let mut parent_cancelled = native_parent_cancelled(&cancel, &input);
    for (route_index, route) in control.routes.iter().enumerate() {
        let route_index = match u32::try_from(route_index) {
            Ok(index) => index,
            Err(_) => {
                finish_terminal_retry(
                    &mut routing_terminal,
                    Outcome::Failed {
                        code: "native-route-index-overflow".into(),
                    },
                )
                .await;
                return Err(StepError::new(
                    "limit-exceeded",
                    "NativeInput route index overflow",
                ));
            }
        };
        let stop_reason = if parent_cancelled {
            Some("cancelled")
        } else {
            native_stop_reason(&cancel, &input, &gate)
        };
        if let Some(reason) = stop_reason {
            parent_cancelled |= reason == "cancelled";
            summaries.push(NativeInputRouteResult {
                route_index,
                status: NativeInputRouteStatus::Unstarted,
                error_code: Some(reason),
            });
            outputs.push(None);
            continue;
        }

        let flow_key = match crate::inputs::route_flow_key(&compiled, entry, route) {
            Ok(key) => key,
            Err(error) => {
                summaries.push(NativeInputRouteResult {
                    route_index,
                    status: NativeInputRouteStatus::Failed,
                    error_code: Some("invalid"),
                });
                outputs.push(None);
                let _ = error;
                continue;
            }
        };
        let target_digest = match route.target.digest() {
            Ok(digest) => digest,
            Err(_) => {
                summaries.push(NativeInputRouteResult {
                    route_index,
                    status: NativeInputRouteStatus::Failed,
                    error_code: Some("invalid"),
                });
                outputs.push(None);
                continue;
            }
        };
        let request = EffectRequest::FlowRun {
            implements: route.target.implements.clone(),
            flow_owner: (route.target.owner.clone(), route.target.owner_hash.clone()),
            flow_key: route.target.flow_key.clone(),
            target_digest,
        };
        let grant = match gate
            .authorize(
                &input.context,
                request.clone(),
                input.receipt.metadata().payload.clone(),
            )
            .await
        {
            Ok(grant) => grant,
            Err(error) => {
                let code = stable_gate_error(&error);
                let status = if matches!(code, "denied" | "approval-required") {
                    NativeInputRouteStatus::Denied
                } else if code == "cancelled" {
                    NativeInputRouteStatus::Cancelled
                } else {
                    NativeInputRouteStatus::Failed
                };
                if status == NativeInputRouteStatus::Cancelled
                    && native_parent_cancelled(&cancel, &input)
                {
                    parent_cancelled = true;
                }
                summaries.push(NativeInputRouteResult {
                    route_index,
                    status,
                    error_code: Some(code),
                });
                outputs.push(None);
                continue;
            }
        };
        if let Some(reason) = native_stop_reason(&cancel, &input, &gate) {
            // Authorization may have completed while the parent expired,
            // changed revision, or cancelled. Do not consume a new FlowRun
            // grant after any of those boundaries.
            parent_cancelled |= reason == "cancelled";
            summaries.push(NativeInputRouteResult {
                route_index,
                status: NativeInputRouteStatus::Unstarted,
                error_code: Some(reason),
            });
            outputs.push(None);
            continue;
        }
        let args_digest = input.receipt.metadata().payload.clone();
        let permit = match grant.consume(&input.context, &request, &args_digest) {
            Ok(permit) => permit,
            Err(error) => {
                let code = stable_gate_error(&error);
                let status = if matches!(code, "denied" | "approval-required") {
                    NativeInputRouteStatus::Denied
                } else if code == "cancelled" {
                    NativeInputRouteStatus::Cancelled
                } else {
                    NativeInputRouteStatus::Failed
                };
                if status == NativeInputRouteStatus::Cancelled
                    && native_parent_cancelled(&cancel, &input)
                {
                    parent_cancelled = true;
                }
                summaries.push(NativeInputRouteResult {
                    route_index,
                    status,
                    error_code: Some(code),
                });
                outputs.push(None);
                continue;
            }
        };
        let mut terminal = ConsumedEntryTerminal::new(permit, gate.clone());
        if let Err(error) = terminal.watch(&input.context) {
            let code = stable_gate_error(&error);
            let outcome = if code == "cancelled" {
                Outcome::Cancelled
            } else {
                Outcome::Failed { code: code.into() }
            };
            finish_terminal_retry(&mut terminal, outcome).await;
            let status = if code == "cancelled" {
                parent_cancelled |= native_parent_cancelled(&cancel, &input);
                NativeInputRouteStatus::Cancelled
            } else {
                NativeInputRouteStatus::Failed
            };
            summaries.push(NativeInputRouteResult {
                route_index,
                status,
                error_code: Some(code),
            });
            outputs.push(None);
            continue;
        }

        let entered = match execution_factory.as_ref() {
            Some(factory) => factory.enter_native_flow(
                &input.context,
                &routing_terminal,
                &input.receipt,
                route,
                flow_key,
                &args_digest,
                &terminal,
            ),
            None => Err(StepError::new(
                "unavailable",
                "Session has no verified execution factory",
            )),
        };
        let frame = match entered {
            Ok(frame) => frame,
            Err(error) => {
                finish_terminal_retry(&mut terminal, step_outcome(&error)).await;
                let code = native_step_error_code(&error, &input);
                let status = if code == "cancelled" {
                    parent_cancelled |= native_parent_cancelled(&cancel, &input);
                    NativeInputRouteStatus::Cancelled
                } else {
                    NativeInputRouteStatus::Failed
                };
                summaries.push(NativeInputRouteResult {
                    route_index,
                    status,
                    error_code: Some(code),
                });
                outputs.push(None);
                continue;
            }
        };

        let prepared = engine.prepare_canonical(
            frame,
            flow_key,
            input.payload.share(),
            compiled.profile.ceiling.loops.clone(),
        );
        let mut run = match prepared.and_then(|prepared| prepared.start()) {
            Ok(run) => run,
            Err(error) => {
                finish_terminal_retry(&mut terminal, step_outcome(&error)).await;
                let code = native_step_error_code(&error, &input);
                let status = if code == "cancelled" {
                    parent_cancelled |= native_parent_cancelled(&cancel, &input);
                    NativeInputRouteStatus::Cancelled
                } else {
                    NativeInputRouteStatus::Failed
                };
                summaries.push(NativeInputRouteResult {
                    route_index,
                    status,
                    error_code: Some(code),
                });
                outputs.push(None);
                continue;
            }
        };

        let mut forwarding = true;
        let mut child_cancelled = false;
        loop {
            let event = tokio::select! {
                _ = cancel.cancelled(), if !child_cancelled => {
                    parent_cancelled = true;
                    child_cancelled = true;
                    run.cancel.cancel();
                    continue;
                }
                event = run.events.recv() => event,
            };
            let Some(event) = event else {
                break;
            };
            if forwarding {
                let tagged = NativeInputEvent { route_index, event };
                tokio::select! {
                    result = event_tx.send(tagged) => {
                        if result.is_err() { forwarding = false; }
                    }
                    _ = cancel.cancelled(), if !child_cancelled => {
                        parent_cancelled = true;
                        child_cancelled = true;
                        run.cancel.cancel();
                        forwarding = false;
                    }
                }
            }
        }
        let run_result = run.result.await;
        let (route_status, error_code, output) = match run_result {
            Ok(buffer) => {
                finish_terminal_retry(&mut terminal, Outcome::Succeeded).await;
                let length = buffer.len();
                match retained_bytes.checked_add(length) {
                    Some(next) if next <= NATIVE_INPUT_RETAINED_OUTPUT_MAX => {
                        retained_bytes = next;
                        (NativeInputRouteStatus::Succeeded, None, Some(buffer))
                    }
                    _ => (
                        NativeInputRouteStatus::RetentionLimit,
                        Some("limit-exceeded"),
                        None,
                    ),
                }
            }
            Err(error) => {
                let outcome = step_outcome(&error);
                finish_terminal_retry(&mut terminal, outcome).await;
                let code = native_step_error_code(&error, &input);
                let status = if code == "cancelled" {
                    parent_cancelled |= native_parent_cancelled(&cancel, &input);
                    NativeInputRouteStatus::Cancelled
                } else {
                    NativeInputRouteStatus::Failed
                };
                (status, Some(code), None)
            }
        };
        summaries.push(NativeInputRouteResult {
            route_index,
            status: route_status,
            error_code,
        });
        outputs.push(output);
    }

    let stop_reason = native_stop_reason(&cancel, &input, &gate);
    let status = if parent_cancelled || stop_reason == Some("cancelled") {
        NativeInputStatus::Cancelled
    } else if stop_reason.is_some() {
        NativeInputStatus::Failed
    } else if summaries
        .iter()
        .all(|route| route.status == NativeInputRouteStatus::Succeeded)
    {
        NativeInputStatus::Succeeded
    } else {
        NativeInputStatus::Failed
    };
    let routing_outcome = match status {
        NativeInputStatus::Succeeded => Outcome::Succeeded,
        NativeInputStatus::Failed => Outcome::Failed {
            code: "native-input-route-failed".into(),
        },
        NativeInputStatus::Cancelled => Outcome::Cancelled,
    };
    let buffer = native_input_envelope(&control.transport_budget, status, &summaries, &outputs);
    if buffer.is_err() {
        finish_terminal_retry(
            &mut routing_terminal,
            Outcome::Failed {
                code: "native-input-envelope-failed".into(),
            },
        )
        .await;
        return Err(buffer.expect_err("checked error above"));
    }
    finish_terminal_retry(&mut routing_terminal, routing_outcome).await;
    Ok(NativeInputCompletion {
        status,
        routes: summaries,
        buffer: buffer.expect("envelope was checked as successful"),
        _control: control,
    })
}

impl SessionState {
    fn ensure_input(&self, input: &NativeInput) -> Result<(), CoreError> {
        self.execution_factory
            .as_ref()
            .ok_or(GateError::Unavailable)?
            .validate_source_receipt(&input.receipt)?;
        input.context.check_active()?;
        input.receipt.validate_context(&input.context)?;
        let metadata = input.receipt.metadata();
        if metadata.source != input.source
            || metadata.view != input.view
            || metadata.op != input.operation
        {
            return Err(CoreError::Invalid(
                "NativeInput differs from its sealed source-read receipt".into(),
            ));
        }
        Ok(())
    }

    async fn submit_native_input(
        &self,
        input: NativeInput,
    ) -> Result<NativeInputRunHandle, CoreError> {
        self.ensure_input(&input)?;

        let source_pack = PackId::parse(input.source.as_str())?;
        let entry = self
            .compiled
            .input_entry(&source_pack, &input.view, &input.operation)
            .ok_or_else(|| CoreError::Invalid("native Input is outside the frozen plan".into()))?;
        let ingress_key = (
            input.source.clone(),
            input.view.clone(),
            input.operation.clone(),
        );
        let registration = self
            .native_ingress
            .get(&ingress_key)
            .ok_or_else(|| CoreError::Invalid("native Input has no registered ingress".into()))?;
        let receipt_metadata = input.receipt.metadata();
        let payload_digest = receipt_metadata.payload.clone();
        let invocation = crate::inputs::native_invocation_identity(receipt_metadata)?;

        // Resolve every route against the exact frozen Input entry before the
        // one-shot routing claim. This is validation only; Gate rechecks all
        // source, route, approval and caller rights at claim time.
        for route in registration.routes() {
            let _ = crate::inputs::route_flow_key(&self.compiled, entry, route)?;
        }

        let route_count = registration.routes().len();
        if route_count == 0 || route_count > 256 {
            return Err(CoreError::Invalid("native route count is invalid".into()));
        }
        let route_bytes = registration
            .routes()
            .iter()
            .try_fold(
                std::mem::size_of::<NativeInputRunControl>(),
                |sum, route| {
                    let dynamic = route
                        .implements
                        .clone_storage_bytes()
                        .checked_add(route.target.clone_storage_bytes().ok()?)?;
                    sum.checked_add(std::mem::size_of::<IngressRoute>())?
                        .checked_add(dynamic)
                },
            )
            .ok_or_else(|| CoreError::Invalid("native route control size overflow".into()))?;
        let route_bytes = route_bytes
            .checked_add(
                route_count
                    .checked_mul(
                        std::mem::size_of::<NativeInputRouteResult>()
                            + std::mem::size_of::<Option<CoreBuffer>>(),
                    )
                    .ok_or_else(|| {
                        CoreError::Invalid("native route result size overflow".into())
                    })?,
            )
            .ok_or_else(|| CoreError::Invalid("native route control size overflow".into()))?;
        let route_bytes = u64::try_from(route_bytes)
            .map_err(|_| CoreError::Invalid("native route control size overflow".into()))?;
        let control = self.gate.control();
        let route_reservation = control.reserve(ControlKind::Catalog, route_bytes.max(1))?;
        let queue_bytes = (NATIVE_INPUT_EVENT_QUEUE as u64)
            .checked_mul(std::mem::size_of::<NativeInputEvent>() as u64)
            .and_then(|bytes| bytes.checked_add(4096))
            .ok_or_else(|| CoreError::Invalid("native event queue size overflow".into()))?;
        let event_reservation = control.reserve(ControlKind::Queue, queue_bytes)?;
        let retained_reservation =
            control.reserve(ControlKind::Queue, NATIVE_INPUT_RETAINED_OUTPUT_MAX as u64)?;
        let waiter_reservation = control.reserve(ControlKind::Waiter, 1024)?;
        let task_reservation = control.reserve(ControlKind::Task, 8192)?;
        let transport_budget = NativeInputTransportBudget::new(control, NATIVE_INPUT_ENVELOPE_MAX)?;

        let mut summaries = Vec::new();
        summaries
            .try_reserve_exact(route_count)
            .map_err(|_| CoreError::Runtime("native route result allocation failed".into()))?;
        let mut outputs = Vec::new();
        outputs
            .try_reserve_exact(route_count)
            .map_err(|_| CoreError::Runtime("native route output allocation failed".into()))?;

        let mut routes = Vec::new();
        routes
            .try_reserve_exact(route_count)
            .map_err(|_| CoreError::Runtime("native route list allocation failed".into()))?;
        routes.extend(registration.routes().iter().cloned());
        let run_control = Arc::new(NativeInputRunControl {
            routes,
            _reservation: route_reservation,
            _event_queue: event_reservation,
            _retained_output: retained_reservation,
            _waiter: waiter_reservation,
            _task: task_reservation,
            transport_budget,
        });

        // All native queues, retained-route metadata and bounded output capacity
        // are admitted before consuming the virtual routing grant.
        let admission = self
            .gate
            .admit_virtual_input(
                &input.context,
                registration,
                input.receipt,
                invocation,
                payload_digest,
            )
            .await?;
        let (permit, receipt) = self
            .gate
            .claim_virtual_input_with_receipt(&input.context, admission)
            .await?;
        let mut routing_terminal = ConsumedEntryTerminal::new(permit, self.gate.clone());
        if let Err(error) = routing_terminal.watch(&input.context) {
            let code = stable_gate_error(&error);
            let outcome = if code == "cancelled" {
                Outcome::Cancelled
            } else {
                Outcome::Failed { code: code.into() }
            };
            finish_terminal_retry(&mut routing_terminal, outcome).await;
            return Err(CoreError::Gate(error));
        }

        let cancel = input.context.cancel().clone();
        let (event_tx, events) = tokio::sync::mpsc::channel(NATIVE_INPUT_EVENT_QUEUE);
        let (result_tx, result_rx) = tokio::sync::oneshot::channel();
        let compiled = self.compiled.clone();
        let gate = self.gate.clone();
        let engine = self.engine.clone();
        let execution_factory = self.execution_factory.clone();
        let task_control = run_control.clone();
        let task_cancel = cancel.clone();
        let task_input = NativeInput {
            source: input.source,
            view: input.view,
            operation: input.operation,
            context: input.context,
            payload: input.payload,
            receipt,
        };
        let runtime = self.runtime.clone();
        runtime.spawn(async move {
            let result = run_native_fanout(
                compiled,
                gate,
                engine,
                execution_factory,
                source_pack,
                task_input,
                routing_terminal,
                task_control,
                summaries,
                outputs,
                event_tx,
                task_cancel,
            )
            .await;
            let _ = result_tx.send(result);
        });

        Ok(NativeInputRunHandle {
            events,
            cancel,
            result: NativeInputRunResult(result_rx),
            control: run_control,
        })
    }

    async fn run_provided_flow(
        &self,
        cx: &CallCx,
        caller: &ControlHandle<dyn tobkiri_flow::ActiveCodeFrame>,
        node: &tobkiri_flow::ReachedNode,
        target: ControlHandle<ResolvedFlow>,
        input: CoreValue,
    ) -> Result<CoreStream, StepError> {
        let _ = (cx, caller, node, target, input);
        Err(StepError::new(
            "unavailable",
            "verified provided-Flow frame transition is not integrated yet",
        ))
    }

    async fn run_provided_flow_canonical(
        &self,
        cx: &CallCx,
        caller: &ControlHandle<dyn tobkiri_flow::ActiveCodeFrame>,
        node: &tobkiri_flow::ReachedNode,
        flow_key: &str,
        args_digest: &tobkiri_canon::Digest,
        input: CoreValue,
    ) -> Result<CoreStream, StepError> {
        cx.check_active().map_err(gate_step_error)?;
        if cx.revision() != Some(&self.compiled.plan_digest)
            || self.gate.epoch(cx.profile()).map_err(gate_step_error)? != cx.security_epoch()
        {
            return Err(StepError::new("cancelled", "caller Flow context is stale"));
        }
        let flow = self
            .compiled
            .flows
            .get(flow_key)
            .ok_or_else(|| StepError::new("not-found", "Flow key is not in this Session"))?;
        let Some(factory) = self.execution_factory.as_ref() else {
            return Err(StepError::new(
                "unavailable",
                "Session execution factory is unavailable",
            ));
        };
        let target = factory.frozen_flow_target(cx, flow_key)?;
        let Some(implements) = flow.document.implements.as_ref() else {
            return Err(StepError::new(
                "denied",
                "selected Flow has no implements endpoint",
            ));
        };
        if target.profile != self.compiled.profile.id
            || target.revision != self.compiled.plan_digest
            || target.owner != flow.owner
            || target.owner_hash != flow.hash
            || target.flow_key != flow_key
            || target.plan_digest != flow.plan_digest
            || &target.implements != implements
        {
            return Err(StepError::new(
                "denied",
                "requested Flow target differs from the frozen compiled entry",
            ));
        }
        let target_digest = target.digest().map_err(gate_step_error)?;
        let request = EffectRequest::FlowRun {
            implements: target.implements.clone(),
            flow_owner: (target.owner.clone(), target.owner_hash.clone()),
            flow_key: target.flow_key.clone(),
            target_digest,
        };
        let grant = self
            .gate
            .authorize(cx, request.clone(), args_digest.clone())
            .await
            .map_err(gate_step_error)?;
        let permit = grant
            .consume(cx, &request, args_digest)
            .map_err(gate_step_error)?;
        let mut terminal = ConsumedEntryTerminal::new(permit, self.gate.clone());
        if let Err(error) = terminal.watch(cx) {
            let mapped = gate_step_error(error);
            finish_terminal_retry(&mut terminal, step_outcome(&mapped)).await;
            return Err(mapped);
        }
        let Some(factory) = self.execution_factory.as_ref() else {
            let error = StepError::new("unavailable", "Session execution factory is unavailable");
            finish_terminal_retry(&mut terminal, step_outcome(&error)).await;
            return Err(error);
        };
        let frame = match factory.enter_provided_flow(
            cx,
            caller,
            node,
            &target,
            flow_key,
            args_digest,
            &terminal,
        ) {
            Ok(frame) => frame,
            Err(error) => {
                finish_terminal_retry(&mut terminal, step_outcome(&error)).await;
                return Err(error);
            }
        };
        let stream = match self.engine.run_canonical_intermediate(
            frame,
            flow_key,
            input,
            self.compiled.profile.ceiling.loops.clone(),
        ) {
            Ok(stream) => stream,
            Err(error) => {
                finish_terminal_retry(&mut terminal, step_outcome(&error)).await;
                return Err(error);
            }
        };
        Ok(CoreStream::new(
            crate::tracked_stream::TrackedStream::new_with_terminal(
                stream,
                self.gate.clone(),
                cx.clone(),
                terminal,
                self.runtime.clone(),
            ),
        ))
    }

    async fn run_guest_flow(
        &self,
        cx: &CallCx,
        provider: &dyn GuestFlowFrame,
        requested_shape: &ShapeRef,
        arguments_digest: &Digest,
        input: CoreValue,
    ) -> Result<CoreStream, StepError> {
        let Some(factory) = self.execution_factory.as_ref() else {
            return Err(StepError::new(
                "unavailable",
                "Session execution factory is unavailable",
            ));
        };
        let prepared =
            factory.prepare_guest_flow(cx, provider, requested_shape, arguments_digest)?;
        let target = prepared.target();
        let flow_key = prepared.flow_key();
        let target_digest = target.digest().map_err(gate_step_error)?;
        let request = EffectRequest::FlowRun {
            implements: target.implements.clone(),
            flow_owner: (target.owner.clone(), target.owner_hash.clone()),
            flow_key: target.flow_key.clone(),
            target_digest,
        };
        let grant = self
            .gate
            .authorize(cx, request.clone(), prepared.arguments_digest().clone())
            .await
            .map_err(gate_step_error)?;
        let permit = grant
            .consume(cx, &request, prepared.arguments_digest())
            .map_err(gate_step_error)?;
        let mut terminal = ConsumedEntryTerminal::new(permit, self.gate.clone());
        if let Err(error) = terminal.watch(cx) {
            let mapped = gate_step_error(error);
            finish_terminal_retry(&mut terminal, step_outcome(&mapped)).await;
            return Err(mapped);
        }
        let frame = match factory.enter_guest_flow(&prepared, &terminal) {
            Ok(frame) => frame,
            Err(error) => {
                finish_terminal_retry(&mut terminal, step_outcome(&error)).await;
                return Err(error);
            }
        };
        let stream = match self.engine.run_canonical_intermediate(
            frame,
            flow_key,
            input,
            self.compiled.profile.ceiling.loops.clone(),
        ) {
            Ok(stream) => stream,
            Err(error) => {
                finish_terminal_retry(&mut terminal, step_outcome(&error)).await;
                return Err(error);
            }
        };
        Ok(CoreStream::new(
            crate::tracked_stream::TrackedStream::new_with_terminal(
                stream,
                self.gate.clone(),
                cx.clone(),
                terminal,
                self.runtime.clone(),
            ),
        ))
    }
}

impl Drop for SessionState {
    fn drop(&mut self) {
        self.shutdown.cancel();
        if let Some(factory) = &self.execution_factory {
            factory.close();
        }
        user_ports::drop_registry(self.user_streams.clone(), &self.runtime);
        let entries = self
            .streams
            .get_mut()
            .map(std::mem::take)
            .unwrap_or_default();
        for (_, entry) in entries {
            entry.cx.cancel().cancel();
            // Drop has no Result channel. Cleanup still runs on the Session's
            // owning runtime; explicit Session.close is the observable path.
            let cleanup = async move {
                if let Some(mut source) = entry.state.lock().await.take() {
                    source.stream.close().await.map_err(CoreError::from)?;
                }
                Ok::<(), CoreError>(())
            };
            // Drop cannot return an audit error. A stopped Tokio runtime drops
            // the task; a defensive panic guard also prevents a second panic
            // during unwinding. Durable expiry records unknown cleanup honestly.
            let _scheduled = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                self.runtime.spawn(cleanup)
            }));
        }
    }
}
