//! Original-grant Audit custody through the actual last canonical ABI hook.
//! SQL is synchronous inside the accepted native supervisor. Its returned
//! delivery owns the same sealed nested Audit permit; conversion is data only.

use crate::{
    InvocationState, ProviderFrame,
    child_census::NativeWorkError,
    effect_values::{EffectValues, text, uint},
    host::{PackHost, PackState},
    owned_values::ValueOwners,
};
use anyhow::{Context, Result};
use std::{alloc::Layout, future::Future, pin::Pin, sync::Arc, task::Poll, time::Instant};
use tobkiri_canon::{CoreValue, LeasedShared};
use tobkiri_flow::StepError;
use tobkiri_gate::{
    CallCx, CancellationFuture, ControlKind, ControlReservation, DispatchPermit,
    EffectRequest, Gate, GateError, Grant, GrantId, Outcome, TerminalDisposition,
};
use tobkiri_store::{
    AuditLog, GuestAuditAppend, GuestAuditAttempt, GuestAuditDelivery,
    GuestAuditDispatch, GuestAuditOutput, GuestAuditRead, StoreError,
};
use tokio::{sync::oneshot, time::Sleep};
use wasmtime::{AsContextMut, StoreContextMut};
use wasmtime::component::{
    BudgetedFuture, BudgetedVecBuilder, HostAllocationOwner, HostDeliveryCompletion,
    HostDeliveryCompletionAuthority, HostDeliveryFinalizer, HostDeliveryKind,
    HostDeliveryProtocol, HostDeliveryReady, Val,
};

// Each endpoint and inline capture drops before its last shared metadata owner.
type Funding = LeasedShared<(), ControlReservation>;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Failure {
    Cancelled,
    Deadline,
    Limit,
    Invalid,
    Denied,
    ApprovalRequired,
    NotFound,
    Conflict,
    Unavailable,
    CommitUnknown,
}
impl Failure {
    fn gate(error: &GateError) -> Self {
        match error {
            GateError::Cancelled => Self::Cancelled,
            GateError::Deadline => Self::Deadline,
            GateError::Limit(_) => Self::Limit,
            GateError::Invalid(_) => Self::Invalid,
            GateError::Denied(_) => Self::Denied,
            GateError::ApprovalRequired => Self::ApprovalRequired,
            _ => Self::Unavailable,
        }
    }
    fn store(error: &StoreError) -> Self {
        match error {
            StoreError::Gate(error) => Self::gate(error),
            StoreError::Denied(_) => Self::Denied,
            StoreError::Invalid(_) | StoreError::Canon(_) => Self::Invalid,
            StoreError::Quota => Self::Limit,
            StoreError::NotFound => Self::NotFound,
            StoreError::Conflict(_) => Self::Conflict,
            StoreError::CommitUnknown => Self::CommitUnknown,
            // Integrity remains typed on the retained Store delivery. It is
            // not a new public WIT enum variant or an interpolated diagnostic.
            _ => Self::Unavailable,
        }
    }
    fn conversion(error: &anyhow::Error) -> Self {
        if let Some(error) = error.downcast_ref::<GateError>() {
            Self::gate(error)
        } else if let Some(error) = error.downcast_ref::<StoreError>() {
            Self::store(error)
        } else {
            Self::Unavailable
        }
    }
    fn public(self) -> anyhow::Error {
        match self {
            Self::CommitUnknown => StoreError::CommitUnknown.into(),
            Self::Cancelled => GateError::Cancelled.into(),
            Self::Deadline => GateError::Deadline.into(),
            Self::Limit => StepError::new("limit-exceeded", "audit operation limit exceeded").into(),
            Self::Invalid => StepError::new("invalid", "invalid audit operation").into(),
            Self::Denied => StepError::new("denied", "audit operation denied").into(),
            Self::ApprovalRequired => GateError::ApprovalRequired.into(),
            Self::NotFound => StepError::new("not-found", "audit row not found").into(),
            Self::Conflict => StepError::new("conflict", "audit operation conflict").into(),
            Self::Unavailable => StepError::new("unavailable", "audit operation unavailable").into(),
        }
    }
}

enum Prepared {
    Append(GuestAuditAppend),
    Read(GuestAuditRead),
}
impl Prepared {
    fn request(&self) -> EffectRequest {
        match self {
            Self::Append(value) => value.request(),
            Self::Read(value) => value.request(),
        }
    }
    fn args(&self) -> &tobkiri_canon::Digest {
        match self {
            Self::Append(value) => value.args_digest(),
            Self::Read(value) => value.args_digest(),
        }
    }
    fn consume(self, grant: Grant) -> GuestAuditAttempt {
        match self {
            Self::Append(value) => value.consume(grant),
            Self::Read(value) => value.consume(grant),
        }
    }
}

enum Output {
    Appended,
    Rows(CoreValue),
}
struct AuditOutput {
    result: std::result::Result<Output, Failure>,
    no_replay: bool,
}
impl AuditOutput {
    fn failed(error: Failure) -> Self {
        Self { result: Err(error), no_replay: false }
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Conversion {
    Prepared,
    Failed,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum WaitEnd {
    Returned,
    Abandoned,
}

struct CallerCustody {
    output: oneshot::Receiver<AuditOutput>,
    converted: oneshot::Sender<Conversion>,
    _funding: Funding,
}
struct AuditCustody {
    prepared: Prepared,
    grant: Grant,
    log: Arc<AuditLog>,
    gate: Arc<dyn Gate>,
    cx: CallCx,
    id: GrantId,
    // SAME actual parent frame is retained as lifetime custody only. Its
    // parent PortCall terminal_acknowledged method is never used for Audit.
    _frame: Arc<dyn ProviderFrame>,
    output: oneshot::Sender<AuditOutput>,
    converted: oneshot::Receiver<Conversion>,
    ready: oneshot::Receiver<HostDeliveryReady>,
    complete: oneshot::Sender<wasmtime::Result<HostDeliveryCompletion>>,
    wait_end: oneshot::Receiver<WaitEnd>,
    authority: HostDeliveryCompletionAuthority,
    deadline: Pin<Box<Sleep>>,
    cancelled: Pin<Box<CancellationFuture>>,
    _funding: Funding,
}

/// This adapter carries no Grant, permit, Store, prepared input or SQL handle.
/// Its default Drop closes Ready before handoff, failing the original job.
struct AuditAbiAdapter {
    ready: Option<oneshot::Sender<HostDeliveryReady>>,
    complete: Option<oneshot::Receiver<wasmtime::Result<HostDeliveryCompletion>>>,
    wait_end: Option<oneshot::Sender<WaitEnd>>,
    owner: HostAllocationOwner,
    funding: Funding,
}
struct WaitCustody {
    complete: oneshot::Receiver<wasmtime::Result<HostDeliveryCompletion>>,
    end: Option<oneshot::Sender<WaitEnd>>,
    returned: bool,
    _funding: Funding,
}
impl Drop for WaitCustody {
    fn drop(&mut self) {
        if let Some(end) = self.end.take() {
            let state = if self.returned { WaitEnd::Returned } else { WaitEnd::Abandoned };
            // This is destruction metadata, never an ACK or a delivery vote.
            let _ = end.send(state);
        }
    }
}
/// A concrete Future owns the entire Drop guard as a field. An async block
/// must not split its captures or end the guard's local scope on Ready: the
/// SAME guard stays alive until BudgetedFuture destroys its physical payload.
struct AuditAbiWait {
    custody: WaitCustody,
}
impl Future for AuditAbiWait {
    type Output = wasmtime::Result<HostDeliveryCompletion>;

    fn poll(
        self: Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
    ) -> Poll<Self::Output> {
        let wait = &mut self.get_mut().custody;
        match Pin::new(&mut wait.complete).poll(cx) {
            Poll::Pending => Poll::Pending,
            Poll::Ready(Ok(Ok(completion))) => {
                // Read by WaitCustody::drop when the owned Future itself is
                // destroyed. Returning Ready does not drop or signal it here.
                wait.returned = true;
                Poll::Ready(Ok(completion))
            }
            Poll::Ready(Ok(Err(error))) => Poll::Ready(Err(error)),
            Poll::Ready(Err(_)) => Poll::Ready(Err(wasmtime::Error::msg(
                "audit original supervisor unavailable",
            ))),
        }
    }
}

impl HostDeliveryProtocol for AuditAbiAdapter {
    fn abi_ready(
        &mut self,
        ready: HostDeliveryReady,
    ) -> wasmtime::Result<BudgetedFuture<'static, wasmtime::Result<HostDeliveryCompletion>>> {
        let sender = self.ready.take().ok_or_else(|| wasmtime::Error::msg("audit ABI handoff repeated"))?;
        let wait = WaitCustody {
            complete: self.complete.take().ok_or_else(|| wasmtime::Error::msg("audit completion missing"))?,
            end: self.wait_end.take(),
            returned: false,
            _funding: self.funding.clone(),
        };
        // Allocate the real wait BEFORE exposing Ready. Admission failure
        // drops its real endpoints and reports Unknown to the same supervisor.
        let future = BudgetedFuture::new(AuditAbiWait { custody: wait }, &self.owner, 0)?;
        if sender.send(ready).is_err() {
            drop(future);
            return Err(wasmtime::Error::msg("audit original supervisor unavailable"));
        }
        // The adapter's outer Box may now be destroyed. The distinct wait
        // owns its endpoints/funding and the accepted native job owns authority.
        Ok(future)
    }
}

// The same source-bound Tokio oneshot layout recipe used by Secret custody:
// channel Arc header, rust-layout upper bound, then physical inline state.
fn c_layout(fields: &[Layout]) -> Option<Layout> {
    let mut result = Layout::from_size_align(0, 1).ok()?;
    for field in fields {
        result = result.extend(*field).ok()?.0;
    }
    Some(result.pad_to_align())
}
fn rust_layout_bound(fields: &[Layout]) -> Option<Layout> {
    let align = fields.iter().map(Layout::align).max().unwrap_or(1);
    let bytes = fields.iter().try_fold(0usize, |n, field| n.checked_add(field.size()))?;
    let padding = fields.len().saturating_sub(1).checked_mul(align - 1)?;
    Some(Layout::from_size_align(bytes.checked_add(padding)?, align).ok()?.pad_to_align())
}
fn channel_bytes<T>() -> Option<usize> {
    let inner = rust_layout_bound(&[
        Layout::new::<std::sync::atomic::AtomicUsize>(),
        Layout::new::<Option<T>>(),
        Layout::new::<std::task::Waker>(),
        Layout::new::<std::task::Waker>(),
    ])?;
    Some(c_layout(&[Layout::new::<[std::sync::atomic::AtomicUsize; 2]>(), inner])?.size())
}
fn fixed_error_bytes() -> Option<usize> {
    // Actual Anyhow1.0.104 ErrorImpl<E> and Wasmtime47.0.4 repr(C)
    // ConcreteError<E>: vtable, optional inline Backtrace, inline E. Including
    // Backtrace even when disabled is conservative; optional captured native
    // Backtrace backing follows the existing native diagnostic policy.
    let prefix = [Layout::new::<*const ()>(), Layout::new::<Option<std::backtrace::Backtrace>>()];
    let anyhow = [Layout::new::<GateError>(), Layout::new::<StepError>(), Layout::new::<StoreError>()]
        .into_iter().try_fold(0usize, |maximum, error| {
            Some(maximum.max(c_layout(&[prefix[0], prefix[1], error])?.size()))
        })?;
    let wasm = [Layout::new::<anyhow::Error>(), Layout::new::<&'static str>()]
        .into_iter().try_fold(0usize, |maximum, error| {
            Some(maximum.max(c_layout(&[prefix[0], prefix[1], error])?.size()))
        })?;
    // Two simultaneous closed error packets: caller conversion and native ABI
    // failure, including an Anyhow wrapper inside a Wasmtime error. Fixed public
    // code+message storage is bounded below; no backend diagnostic is imported.
    anyhow.checked_add(wasm)?.checked_add(128)?.checked_mul(2)
}
fn metadata_bytes() -> Option<usize> {
    // The native spawner separately reserves the exact concrete future and
    // both payload Boxes. Component owner independently funds the adapter,
    // record arena and ABI wait. No charge here replaces those actual owners.
    [
        Funding::storage_bytes(),
        channel_bytes::<AuditOutput>()?,
        channel_bytes::<Conversion>()?,
        channel_bytes::<HostDeliveryReady>()?,
        channel_bytes::<wasmtime::Result<HostDeliveryCompletion>>()?,
        channel_bytes::<WaitEnd>()?,
        std::mem::size_of::<Sleep>(),
        std::mem::size_of::<CancellationFuture>(),
        std::mem::size_of::<CallerCustody>(),
        std::mem::size_of::<AuditOutput>(),
        std::mem::size_of::<Outcome>(),
        // Expected immutable outcome and the one proposal coexist during ACK.
        512,
        fixed_error_bytes()?,
    ].into_iter().try_fold(0usize, usize::checked_add)
}

#[expect(
    clippy::large_enum_variant,
    reason = "Keep consumed dispatch and delivery inline to avoid a new fallible heap allocation after Grant consumption"
)]
enum Executed {
    Delivery(GuestAuditDelivery),
    Refused(GuestAuditDispatch, Failure),
}
impl Executed {
    fn permit(&self) -> &DispatchPermit {
        match self {
            Self::Delivery(value) => value.permit(),
            Self::Refused(value, _) => value.permit(),
        }
    }
    fn deadline(&self) -> Instant {
        match self {
            Self::Delivery(value) => value.deadline(),
            Self::Refused(value, _) => value.deadline(),
        }
    }
    fn output(&self) -> AuditOutput {
        match self {
            Self::Delivery(value) => AuditOutput {
                result: value.result().map(|value| match value {
                    GuestAuditOutput::Appended => Output::Appended,
                    GuestAuditOutput::Rows(rows) => Output::Rows(rows.share()),
                }).map_err(Failure::store),
                no_replay: value.is_no_replay(),
            },
            Self::Refused(_, failure) => AuditOutput::failed(*failure),
        }
    }
    fn no_replay(&self) -> bool {
        matches!(self, Self::Delivery(value) if value.is_no_replay())
    }
    fn outcome(&self, abi_prepared: bool) -> Outcome {
        match self {
            Self::Delivery(value) => value.terminal_outcome(abi_prepared),
            Self::Refused(_, failure) => match failure {
                Failure::Cancelled => Outcome::Cancelled,
                Failure::Deadline | Failure::Limit => Outcome::Failed { code: "limit-exceeded".into() },
                _ => Outcome::Failed { code: "unavailable".into() },
            },
        }
    }
}

async fn supervise(mut custody: AuditCustody) -> std::result::Result<(), NativeWorkError> {
    // Admission/waiter construction and its first fallible poll finish before
    // consume. No refresh, new CallCx root, new grant or replacement deadline.
    let active = custody.gate.check_context(&custody.cx)
        .and_then(|()| custody.cx.check_active());
    let cancellation = if active.is_ok() {
        std::future::poll_fn(|cx| Poll::Ready(match custody.cancelled.as_mut().poll(cx) {
            Poll::Pending => Ok(()),
            Poll::Ready(Ok(_)) => Err(GateError::Cancelled),
            Poll::Ready(Err(_)) => Err(GateError::Unavailable),
        })).await
    } else {
        active
    };
    if let Err(error) = cancellation {
        let _ = custody.output.send(AuditOutput::failed(Failure::gate(&error)));
        // No consumed dispatch exists, hence no honest completion authority.
        return Err(NativeWorkError::CompletionUnknown);
    }
    let dispatch = match custody.prepared.consume(custody.grant) {
        GuestAuditAttempt::Ready(dispatch) => dispatch,
        GuestAuditAttempt::BeforeConsumed(error) => {
            let _ = custody.output.send(AuditOutput::failed(Failure::gate(&error)));
            return Err(NativeWorkError::CompletionUnknown);
        }
        GuestAuditAttempt::Cleanup(cleanup) => {
            let failure = Failure::gate(cleanup.error());
            let _settled = cleanup.settle_failure(custody.gate.as_ref()).await;
            let _ = custody.output.send(AuditOutput::failed(failure));
            drop(cleanup);
            // Actual cleanup custody was retained through settlement; neither
            // settlement nor a bool can manufacture a DispatchPermit receipt.
            return Err(NativeWorkError::CompletionUnknown);
        }
        GuestAuditAttempt::CommitUnknown(unknown) => {
            let failure = Failure::gate(unknown.error());
            let _reconciled = unknown.reconcile();
            let _ = custody.output.send(AuditOutput::failed(failure));
            drop(unknown);
            return Err(NativeWorkError::CompletionUnknown);
        }
    };
    let narrowed = custody.cx.clone().narrow_limits(dispatch.limits()).cap_deadline(dispatch.deadline());
    let watched = custody.gate.watch_lease(&narrowed, dispatch.permit())
        .and_then(|()| custody.gate.check_context(&narrowed))
        .and_then(|()| narrowed.check_active());
    // This one synchronous call contains all connection/TX/statement/Rows
    // lifetimes. The strong original frame and value owners are already held
    // above it. No Gate, ABI, timer, terminal or user callback runs inside it.
    let executed = match watched {
        Ok(()) => Executed::Delivery(dispatch.execute(&custody.log)),
        Err(error) => Executed::Refused(dispatch, Failure::gate(&error)),
    };
    // SQL resources are ALL gone before output sharing, conversion, terminal
    // outcome allocation, Gate callbacks or destruction of retained custody.
    custody.deadline.as_mut().reset(executed.deadline().into());
    let handed = custody.output.send(executed.output()).is_ok();
    let conversion = if handed {
        tokio::select! {
            value = &mut custody.converted => value.unwrap_or(Conversion::Failed),
            _ = custody.cancelled.as_mut() => Conversion::Failed,
            _ = custody.deadline.as_mut() => Conversion::Failed,
        }
    } else {
        Conversion::Failed
    };
    let ready = tokio::select! {
        ready = &mut custody.ready => ready.ok(),
        _ = custody.cancelled.as_mut() => None,
        _ = custody.deadline.as_mut() => None,
    };
    let live = ready.is_some() && !custody.complete.is_closed()
        && Instant::now() < executed.deadline()
        && narrowed.check_active().is_ok();
    let outcome = executed.outcome(live && conversion == Conversion::Prepared);
    // The Gate may preserve an earlier immutable winner. Record only once,
    // then authenticate that exact winner against the SAME sealed Audit permit.
    // No parent frame terminal API is called with this nested effect.
    let recorded = custody.gate.record_outcome(custody.id, outcome.clone()).await;
    let acknowledged = if recorded.is_ok() {
        custody.gate.acknowledged_terminal(executed.permit()).ok()
    } else {
        None
    };
    let verified = acknowledged.as_ref().is_some_and(|ack| {
        ack.grant_id() == executed.permit().id()
            && ack.outcome() == &outcome
            && *ack.disposition() == TerminalDisposition::Completed
    });
    // ACK scratch admission, outbox delivery, receipt construction and current
    // context are fallible. No vendor capability is produced on any failure.
    let completion = match ready {
        Some(ready) if live && verified && !custody.complete.is_closed()
            && narrowed.check_active().is_ok()
            && Instant::now() < executed.deadline() => {
            custody.authority.complete_after_verified_original_ack(ready)
        }
        _ if executed.no_replay() => Err(wasmtime::Error::from_anyhow(StoreError::CommitUnknown.into())),
        _ => Err(wasmtime::Error::msg("audit original terminal delivery unknown")),
    };
    let accepted = completion.is_ok();
    let sent = custody.complete.send(completion).is_ok();
    // The ABI wait must actually return the capability and be destroyed; its
    // Drop notification is metadata only. Retain the original permit, result,
    // frame and funding until this same final wait ends, including cancellation.
    let ended = custody.wait_end.await.ok();
    drop(acknowledged);
    drop(executed);
    if accepted && sent && ended == Some(WaitEnd::Returned) {
        Ok(())
    } else {
        Err(NativeWorkError::CompletionUnknown)
    }
}

pub(super) async fn invoke(
    store: &mut StoreContextMut<'_, InvocationState<PackState>>,
    host: &PackHost,
    cx: &CallCx,
    operation: &str,
    params: &[Val],
    owners: &ValueOwners,
) -> Result<Vec<Val>> {
    let codec = EffectValues(owners);
    let prepared = match (operation, params) {
        ("append", [kind, payload]) => Prepared::Append(GuestAuditAppend::prepare(
            cx, CoreValue::string(text(kind)?, owners.graph())?, codec.parse(payload)?, owners.graph(),
        )?),
        ("read", [after, max]) => Prepared::Read(GuestAuditRead::prepare(
            cx, uint(after)?, u32::try_from(uint(max)?)?, owners.graph(),
        )?),
        _ => return Err(StepError::new("invalid", "invalid audit operation").into()),
    };
    let frame = store.data().data.frame.as_ref()
        .context("audit import lacks actual provider frame")?.clone();
    let grant = host.shared.gate.authorize(cx, prepared.request(), prepared.args().clone()).await?;
    let spawner = host.native_work_spawner(&store.data().data).await?;
    let bytes = metadata_bytes().and_then(|n| n.checked_add(grant.id().clone_storage_bytes()))
        .context("audit handoff size overflow")?;
    let reservation = cx.control().reserve(ControlKind::Task, u64::try_from(bytes)?)?;
    let funding = Funding::try_new_precharged((), reservation)?;
    let id = grant.id().try_clone_precharged()?;
    let deadline = Box::pin(tokio::time::sleep_until(cx.deadline().into()));
    let cancelled = Box::pin(cx.cancel().cancelled());
    let (output_tx, output_rx) = oneshot::channel();
    let (converted_tx, converted_rx) = oneshot::channel();
    let (ready_tx, ready_rx) = oneshot::channel();
    let (complete_tx, complete_rx) = oneshot::channel();
    let (wait_end_tx, wait_end_rx) = oneshot::channel();
    let adapter = AuditAbiAdapter {
        ready: Some(ready_tx), complete: Some(complete_rx), wait_end: Some(wait_end_tx),
        owner: owners.component().clone(), funding: funding.clone(),
    };
    let finalizer = HostDeliveryFinalizer::new(adapter, owners.component())?;
    // Real Value-kind admission validates actual callback result types and is
    // fixed BEFORE original consume/SQL. No fake Resource or runtime keeper.
    let (_record, authority) = store.as_context_mut()
        .host_delivery_reserve(HostDeliveryKind::Value, finalizer)?;
    let caller = CallerCustody { output: output_rx, converted: converted_tx, _funding: funding.clone() };
    let custody = AuditCustody {
        prepared, grant, log: host.audit_log().clone(), gate: host.shared.gate.clone(),
        cx: cx.clone(), id, _frame: frame, output: output_tx, converted: converted_rx,
        ready: ready_rx, complete: complete_tx, wait_end: wait_end_rx, authority,
        deadline, cancelled, _funding: funding,
    };
    // Rejection drops the still-unconsumed original Grant after native guards.
    spawner.bind_cancel(cx.cancel().clone())?;
    spawner.spawn(supervise(custody))?;
    let output = caller.output.await.unwrap_or_else(|_| AuditOutput::failed(Failure::Unavailable));
    let result = output.result.and_then(|output| {
        let result = match output {
            Output::Appended => codec.ok(None),
            Output::Rows(rows) => {
                let rows = rows.array().context("audit output is not an owned row array");
                rows.and_then(|rows| {
                    let mut result = BudgetedVecBuilder::new(rows.len(), owners.component(), 0)?;
                    for row in rows.iter() {
                        result.push(codec.string(row.as_str().context("audit output row is not JSON text")?)?)?;
                    }
                    codec.ok(Some(Val::List(result.finish()?)))
                })
            }
        };
        result.map_err(|error| Failure::conversion(&error))
    }).and_then(|values| cx.check_active().map(|()| values).map_err(|error| Failure::gate(&error)));
    // Conversion Prepared is NOT delivery evidence. Only the runtime-created
    // Ready at the last canonical boundary can permit actual terminal success.
    let converted = if result.is_ok() { Conversion::Prepared } else { Conversion::Failed };
    let _ = caller.converted.send(converted);
    result.map_err(|failure| {
        if output.no_replay { StoreError::CommitUnknown.into() } else { failure.public() }
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};
    use tobkiri_gate::CoreControl;
    use wasmtime::component::{HostAllocationBudget, HostAllocationError, HostAllocationRequest};

    // Allocation-custody tests only: no Gate, Grant, DispatchPermit, Core
    // frame, Ready or completion capability is fabricated by these fixtures.
    struct ComponentBudget(AtomicU64);
    impl HostAllocationBudget for ComponentBudget {
        fn reserve(&self, request: HostAllocationRequest) -> std::result::Result<(), HostAllocationError> {
            self.0.fetch_add(request.bytes, Ordering::AcqRel);
            Ok(())
        }
        fn release(&self, bytes: u64) {
            self.0.fetch_sub(bytes, Ordering::AcqRel);
        }
    }
    fn funding(control: &CoreControl) -> Funding {
        let reservation = control.reserve(ControlKind::Task, metadata_bytes().unwrap() as u64).unwrap();
        Funding::try_new_precharged((), reservation).unwrap()
    }

    #[tokio::test]
    async fn audit_abi_adapter_drop_closes_channels_after_physical_erasure() {
        let control = CoreControl::default();
        let charged = funding(&control);
        let component = Arc::new(ComponentBudget(AtomicU64::new(0)));
        let owner: HostAllocationOwner = component.clone();
        let (ready_tx, ready_rx) = oneshot::channel();
        let (complete_tx, complete_rx) = oneshot::channel();
        let (end_tx, end_rx) = oneshot::channel();
        let adapter = AuditAbiAdapter {
            ready: Some(ready_tx), complete: Some(complete_rx), wait_end: Some(end_tx),
            owner: owner.clone(), funding: charged.clone(),
        };
        let finalizer = HostDeliveryFinalizer::new(adapter, &owner).unwrap();
        let native_funding = charged.clone();
        drop(charged);
        assert_eq!(control.usage().count(ControlKind::Task), 1);
        assert!(component.0.load(Ordering::Acquire) > 0);
        drop(finalizer);
        assert!(ready_rx.await.is_err());
        assert!(complete_tx.is_closed());
        assert!(end_rx.await.is_err());
        assert_eq!(component.0.load(Ordering::Acquire), 0);
        assert_eq!(control.usage().count(ControlKind::Task), 1);
        drop(complete_tx);
        drop(native_funding);
        assert_eq!(control.usage().count(ControlKind::Task), 0);
    }

    #[tokio::test]
    async fn audit_abi_wait_drop_reports_abandoned_without_ready_or_ack() {
        let control = CoreControl::default();
        let charged = funding(&control);
        let component = Arc::new(ComponentBudget(AtomicU64::new(0)));
        let owner: HostAllocationOwner = component.clone();
        let (complete_tx, complete_rx) = oneshot::channel();
        let (end_tx, end_rx) = oneshot::channel();
        let wait = WaitCustody {
            complete: complete_rx, end: Some(end_tx), returned: false, _funding: charged.clone(),
        };
        let future = BudgetedFuture::new(AuditAbiWait { custody: wait }, &owner, 0).unwrap();
        let native_funding = charged.clone();
        drop(charged);
        assert_eq!(control.usage().count(ControlKind::Task), 1);
        assert!(component.0.load(Ordering::Acquire) > 0);
        drop(future);
        assert_eq!(end_rx.await.unwrap(), WaitEnd::Abandoned);
        assert!(complete_tx.is_closed());
        assert_eq!(component.0.load(Ordering::Acquire), 0);
        assert_eq!(control.usage().count(ControlKind::Task), 1);
        drop(complete_tx);
        drop(native_funding);
        assert_eq!(control.usage().count(ControlKind::Task), 0);
    }
}
