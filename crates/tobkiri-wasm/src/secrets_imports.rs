//! Supervised Secret reads. Authority comes only from the original ordinary
//! Grant; the actual Store generation owns every accepted blocking join.

use crate::{
    child_census::NativeWorkError,
    host::{NativeWorkSpawner, PackHost, Shared},
};
use anyhow::{Context, Result};
use std::{alloc::Layout, future::Future, pin::Pin, sync::Arc, task::Poll, time::Instant};
use tobkiri_canon::{Digest, LeasedShared};
use tobkiri_flow::StepError;
use tobkiri_gate::{
    CallCx, CancellationFuture, ConsumptionAttempt, ControlKind, ControlReservation,
    DispatchPermit, EffectRequest, Gate, GateError, Grant, GrantId, Outcome,
};
use tobkiri_store::{DataRoot, SecretCompletion, SecretWork, SecretWorkResult, StoreError};
use tokio::{sync::oneshot, time::Sleep};

// The exact prepaid block is physically freed before its release plan refunds.
type Funding = LeasedShared<(), ControlReservation>;

/// Closed, heap-free handoff metadata. No native diagnostic or backend error
/// crosses an oneshot; original Gate owners keep their own authority/custody.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u8)]
enum GateFailure {
    Cancelled,
    Deadline,
    Limit,
    Invalid,
    Denied,
    ApprovalRequired,
    Unavailable,
}
impl GateFailure {
    fn from_gate(error: GateError) -> Self {
        Self::from_ref(&error)
    }
    fn from_ref(error: &GateError) -> Self {
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
    fn into_error(self) -> GateError {
        match self {
            Self::Cancelled => GateError::Cancelled,
            Self::Deadline => GateError::Deadline,
            Self::Limit => GateError::Limit("secret lease limit".into()),
            Self::Invalid => GateError::Invalid("secret lease invalid".into()),
            Self::Denied => GateError::Denied("secret operation denied".into()),
            Self::ApprovalRequired => GateError::ApprovalRequired,
            Self::Unavailable => GateError::Unavailable,
        }
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum SecretFailure {
    Gate(GateFailure),
    NotFound,
    Denied,
    Invalid,
    Quota,
    Unavailable,
    CommitUnknown,
    PendingDataMove,
}
impl SecretFailure {
    fn from_store(error: StoreError) -> Self {
        Self::from_store_ref(&error)
    }
    fn from_store_ref(error: &StoreError) -> Self {
        match error {
            StoreError::Gate(error) => Self::Gate(GateFailure::from_ref(error)),
            StoreError::NotFound => Self::NotFound,
            StoreError::Denied(_) => Self::Denied,
            StoreError::Invalid(_) | StoreError::Canon(_) => Self::Invalid,
            StoreError::Quota => Self::Quota,
            StoreError::CommitUnknown => Self::CommitUnknown,
            StoreError::PendingDataMove => Self::PendingDataMove,
            _ => Self::Unavailable,
        }
    }
    fn from_conversion(error: anyhow::Error) -> Self {
        // Project by sealed Rust types once. Original context/source/backtrace
        // is dropped here, before the delivery handshake or terminal wait.
        if let Some(error) = error.downcast_ref::<GateError>() {
            Self::Gate(GateFailure::from_ref(error))
        } else if let Some(error) = error.downcast_ref::<StoreError>() {
            Self::from_store_ref(error)
        } else {
            Self::Unavailable
        }
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u8)]
enum FailureCode {
    HostEffect,
    Deadline,
    NativeWork,
    StorageCommitUnknown,
    StoragePendingMove,
}
impl FailureCode {
    fn text(self) -> &'static str {
        match self {
            Self::HostEffect => "host-effect",
            Self::Deadline => "deadline-failure-unknown",
            Self::NativeWork => "secret-native-work",
            Self::StorageCommitUnknown => "storage-sql-commit-unknown",
            Self::StoragePendingMove => "storage-pending-data-move",
        }
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum SecretDisposition {
    Succeeded,
    Failed(FailureCode),
    Cancelled,
}
impl SecretDisposition {
    fn into_outcome(self) -> Outcome {
        match self {
            Self::Succeeded => Outcome::Succeeded,
            Self::Cancelled => Outcome::Cancelled,
            Self::Failed(code) => Outcome::Failed {
                code: code.text().into(),
            },
        }
    }
}
fn disposition<T>(
    result: &std::result::Result<T, SecretFailure>,
    cx: &CallCx,
) -> SecretDisposition {
    if result.is_ok() {
        return SecretDisposition::Succeeded;
    }
    // Preserve effect_outcome's priority using sealed typed metadata, never
    // a code/message/String comparison or an untrusted diagnostic.
    match result {
        Err(SecretFailure::CommitUnknown) => {
            return SecretDisposition::Failed(FailureCode::StorageCommitUnknown);
        }
        Err(SecretFailure::PendingDataMove) => {
            return SecretDisposition::Failed(FailureCode::StoragePendingMove);
        }
        _ => (),
    }
    if Instant::now() >= cx.deadline() {
        SecretDisposition::Failed(FailureCode::Deadline)
    } else if cx.cancel().is_cancelled() {
        SecretDisposition::Cancelled
    } else {
        SecretDisposition::Failed(FailureCode::HostEffect)
    }
}
struct SecretOutput {
    result: std::result::Result<SecretWorkResult, SecretFailure>,
    output_bytes: u64,
}
impl SecretOutput {
    fn failed(error: SecretFailure) -> Self {
        Self {
            result: Err(error),
            output_bytes: 0,
        }
    }
}

// Declaration order is destruction order: the one shared Task charge outlives
// every channel, timer, retained host handle and prepared path that it funds.
struct CallerCustody<F> {
    output_rx: oneshot::Receiver<SecretOutput>,
    delivery_tx: oneshot::Sender<SecretDisposition>,
    terminal_rx: oneshot::Receiver<std::result::Result<(), GateFailure>>,
    convert: F,
    funding: Funding,
}
struct SecretCustody {
    grant: Grant,
    request: EffectRequest,
    args: Digest,
    root: DataRoot,
    shared: Arc<Shared>,
    cx: CallCx,
    gate: Arc<dyn Gate>,
    id: GrantId,
    output_tx: oneshot::Sender<SecretOutput>,
    delivery_rx: oneshot::Receiver<SecretDisposition>,
    terminal_tx: oneshot::Sender<std::result::Result<(), GateFailure>>,
    deadline: Pin<Box<Sleep>>,
    cancelled: Pin<Box<CancellationFuture>>,
    #[cfg(test)]
    hooks: Option<Arc<tests::Hooks>>,
    funding: Funding,
}
struct BlockingCustody {
    work: SecretWork,
    shared: Arc<Shared>,
    #[cfg(test)]
    hooks: Option<Arc<tests::Hooks>>,
    // The blocking closure itself retains the original funding, including if
    // its observer disappears. The cache is borrowed only inside this body.
    _funding: Funding,
}
impl BlockingCustody {
    fn execute(self) -> SecretCompletion {
        #[cfg(test)]
        if let Some(hooks) = &self.hooks {
            hooks.before_blocking();
        }
        let completion = self.work.execute(&self.shared.vault);
        #[cfg(test)]
        if let Some(hooks) = &self.hooks {
            hooks.after_blocking();
        }
        completion
    }
}

// This one-capture factory lets the admission formula measure the actual
// closure type used by the pinned Tokio blocking pool, without constructing it.
fn blocking_body(work: BlockingCustody) -> impl FnOnce() -> SecretCompletion + Send + 'static {
    move || work.execute()
}

// Source-bound requested-layout admission for Rust1.94 / Tokio1.53.3 /
// Anyhow1.0.104 with this workspace's stable Tokio cfg (no tracing, taskdump,
// test-util or tokio_unstable). This is fixed handoff metadata admission, not
// an RSS bound. Shared cancellation PAL storage, pool queues/worker stacks and
// optional diagnostic Backtrace backing follow their existing native policies.
// Dedicated scheduler cells/outer payload boxes and the returned fixed Anyhow
// still require their own physical-before-refund owner proof; a larger sum
// cannot establish that lifetime condition.
fn c_layout(fields: &[Layout]) -> Option<Layout> {
    let mut result = Layout::from_size_align(0, 1).ok()?;
    for field in fields {
        result = result.extend(*field).ok()?.0;
    }
    Some(result.pad_to_align())
}
fn rust_layout_bound(fields: &[Layout]) -> Option<Layout> {
    // Rust field order is not promised. Any permutation has at most n-1
    // internal padding runs, each smaller than the maximum field alignment.
    let align = fields.iter().map(Layout::align).max().unwrap_or(1);
    let bytes = fields
        .iter()
        .try_fold(0usize, |n, field| n.checked_add(field.size()))?;
    let padding = fields.len().saturating_sub(1).checked_mul(align - 1)?;
    Some(
        Layout::from_size_align(bytes.checked_add(padding)?, align)
            .ok()?
            .pad_to_align(),
    )
}
fn arc_layout_bytes(value: Layout) -> Option<usize> {
    // alloc::sync::ArcInner is repr(C): strong, weak, inline data.
    Some(c_layout(&[Layout::new::<[std::sync::atomic::AtomicUsize; 2]>(), value])?.size())
}
fn channel_bytes<T>() -> Option<usize> {
    // Tokio oneshot::Inner: AtomicUsize, UnsafeCell<Option<T>>, and two
    // UnsafeCell<MaybeUninit<Waker>> task slots. There is no channel Mutex.
    let inner = rust_layout_bound(&[
        Layout::new::<std::sync::atomic::AtomicUsize>(),
        Layout::new::<Option<T>>(),
        Layout::new::<std::task::Waker>(),
        Layout::new::<std::task::Waker>(),
    ])?;
    arc_layout_bytes(inner)
}
fn blocking_task_bytes<G, F>(_: G) -> Option<usize>
where
    G: FnOnce(BlockingCustody) -> F,
    F: FnOnce() -> SecretCompletion,
{
    // pool::spawn_blocking optionally boxes the closure at exactly this
    // threshold; BlockingTask then contains Option<F> (or Option<Box<F>>).
    let threshold = if cfg!(debug_assertions) { 2048 } else { 16384 };
    let (future, extra_box) = if std::mem::size_of::<F>() > threshold {
        (Layout::new::<Option<Box<F>>>(), std::mem::size_of::<F>())
    } else {
        (Layout::new::<Option<F>>(), 0)
    };
    let output = Layout::new::<std::result::Result<SecretCompletion, tokio::task::JoinError>>();
    let payload = Layout::from_size_align(
        future.size().max(output.size()),
        future.align().max(output.align()),
    )
    .ok()?;
    // Stage is repr(C) enum Running(Future)/Finished(Result)/Consumed.
    let stage = c_layout(&[Layout::new::<std::ffi::c_int>(), payload])?;
    type Hook = Option<Arc<dyn Fn() + Send + Sync>>;
    let header = c_layout(&[
        Layout::new::<std::sync::atomic::AtomicUsize>(),
        Layout::new::<Option<std::ptr::NonNull<()>>>(),
        Layout::new::<*const ()>(),
        Layout::new::<Option<std::num::NonZeroU64>>(),
        // Actual stable cfg uses a ZST. Including the optional latency field
        // is an explicit source-derived over-admission, not hidden margin.
        Layout::new::<Option<std::num::NonZeroU64>>(),
    ])?;
    let hooks = rust_layout_bound(&[Layout::new::<Hook>()])?;
    let schedule = rust_layout_bound(&[hooks])?;
    let core = c_layout(&[schedule, Layout::new::<std::num::NonZeroU64>(), stage])?;
    let pointers = rust_layout_bound(&[Layout::new::<*const ()>(), Layout::new::<*const ()>()])?;
    let trailer = rust_layout_bound(&[pointers, Layout::new::<Option<std::task::Waker>>(), hooks])?;
    let cell = c_layout(&[header, core, trailer])?;
    let cache_align = if cfg!(any(
        target_arch = "x86_64",
        target_arch = "aarch64",
        target_arch = "powerpc64"
    )) {
        128
    } else if cfg!(target_arch = "s390x") {
        256
    } else if cfg!(any(
        target_arch = "arm",
        target_arch = "mips",
        target_arch = "mips64",
        target_arch = "sparc",
        target_arch = "hexagon"
    )) {
        32
    } else if cfg!(target_arch = "m68k") {
        16
    } else {
        64
    };
    Layout::from_size_align(cell.size(), cell.align().max(cache_align))
        .ok()?
        .pad_to_align()
        .size()
        .checked_add(extra_box)
}
fn supervisor_bytes<G, F>(_: G) -> usize
where
    G: FnOnce(SecretCustody) -> F,
    F: Future,
{
    std::mem::size_of::<F>()
}
fn fixed_error_bytes() -> Option<usize> {
    // Anyhow ErrorImpl<E> is repr(C): vtable pointer, inline Backtrace option,
    // E. Only one fixed error is reconstructed after terminal selection.
    let prefix = [
        Layout::new::<*const ()>(),
        Layout::new::<Option<std::backtrace::Backtrace>>(),
    ];
    let error = [
        Layout::new::<GateError>(),
        Layout::new::<StepError>(),
        Layout::new::<StoreError>(),
    ]
    .into_iter()
    .try_fold(0usize, |max, object| {
        Some(max.max(c_layout(&[prefix[0], prefix[1], object])?.size()))
    })?;
    // Longest fixed Gate String or fixed StepError code+message. Original
    // backend diagnostics and their source chains are never imported.
    let strings = "secret operation denied"
        .len()
        .max("limit-exceeded".len() + "secret output limit exceeded".len())
        .max("unavailable".len() + "secret operation unavailable".len());
    error.checked_add(strings)
}
fn metadata_bytes<T, F>() -> Option<usize> {
    // Each distinct owned allocation plus simultaneous inline handoff owners:
    // exact funding block; 3 channel Arcs; 2 timer/waiter boxes + owned waiter;
    // original blocking cell/optional closure box; actual supervisor future;
    // caller/result, completion/output and blocking invocation move overlap;
    // one fixed Outcome String and at most one returned fixed Anyhow error.
    let parts = [
        Funding::storage_bytes(),
        channel_bytes::<SecretOutput>()?,
        channel_bytes::<SecretDisposition>()?,
        channel_bytes::<std::result::Result<(), GateFailure>>()?,
        std::mem::size_of::<Sleep>(),
        std::mem::size_of::<CancellationFuture>(),
        std::mem::size_of::<tokio_util::sync::WaitForCancellationFutureOwned>(),
        blocking_task_bytes(blocking_body)?,
        supervisor_bytes(supervise),
        std::mem::size_of::<CallerCustody<F>>(),
        std::mem::size_of::<std::result::Result<T, SecretFailure>>(),
        std::mem::size_of::<SecretCompletion>(),
        std::mem::size_of::<SecretOutput>(),
        std::mem::size_of::<BlockingCustody>(),
        std::mem::size_of::<Outcome>(),
        [
            FailureCode::HostEffect,
            FailureCode::Deadline,
            FailureCode::NativeWork,
            FailureCode::StorageCommitUnknown,
            FailureCode::StoragePendingMove,
        ]
        .into_iter()
        .map(|code| code.text().len())
        .max()?,
        fixed_error_bytes()?,
    ];
    parts.into_iter().try_fold(0usize, usize::checked_add)
}

struct Dispatched {
    permit: DispatchPermit,
    cx: CallCx,
}
impl Dispatched {
    async fn narrow(&mut self, gate: &dyn Gate) -> std::result::Result<(), GateError> {
        let narrowed = self.cx.clone().narrow_limits(self.permit.limits());
        gate.narrow_lease(&mut self.permit, &narrowed).await?;
        let narrowed = narrowed.cap_deadline(self.permit.deadline());
        gate.watch_lease(&narrowed, &self.permit)?;
        gate.check_context(&narrowed)?;
        narrowed.check_active()?;
        if Instant::now() >= self.permit.deadline() {
            return Err(GateError::Deadline);
        }
        self.cx = narrowed;
        Ok(())
    }
}

// Reconstruct only fixed public errors after receiving heap-free metadata.
fn failure(error: SecretFailure) -> anyhow::Error {
    match error {
        SecretFailure::Gate(error) => error.into_error().into(),
        SecretFailure::NotFound => StepError::new("not-found", "secret not found").into(),
        SecretFailure::Denied => StepError::new("denied", "secret operation denied").into(),
        SecretFailure::Invalid => StepError::new("invalid", "invalid secret operation").into(),
        SecretFailure::Quota => {
            StepError::new("limit-exceeded", "secret output limit exceeded").into()
        }
        SecretFailure::Unavailable => {
            StepError::new("unavailable", "secret operation unavailable").into()
        }
        SecretFailure::CommitUnknown => StoreError::CommitUnknown.into(),
        SecretFailure::PendingDataMove => StoreError::PendingDataMove.into(),
    }
}
fn cleanup_business(error: &GateError) -> SecretFailure {
    SecretFailure::Gate(match error {
        GateError::Cancelled => GateFailure::Cancelled,
        GateError::Deadline => GateFailure::Deadline,
        GateError::Limit(_) => GateFailure::Limit,
        GateError::Invalid(_) => GateFailure::Invalid,
        _ => GateFailure::Unavailable,
    })
}

async fn supervise(mut custody: SecretCustody) -> std::result::Result<(), NativeWorkError> {
    if let Err(error) = custody.gate.check_context(&custody.cx) {
        let _ = custody
            .output_tx
            .send(SecretOutput::failed(SecretFailure::Gate(
                GateFailure::from_gate(error),
            )));
        let _ = custody.terminal_tx.send(Ok(()));
        return Ok(());
    }
    // cancelled() reports a failed waiter admission on its first poll.
    // Poll exactly once before consume: Pending retains this SAME waiter,
    // while an admission failure/cancel refuses the still-unconsumed grant.
    let cancellation = std::future::poll_fn(|cx| {
        Poll::Ready(match custody.cancelled.as_mut().poll(cx) {
            Poll::Pending => Ok(()),
            Poll::Ready(Ok(_)) => Err(GateError::Cancelled),
            Poll::Ready(Err(_)) => Err(GateError::Unavailable),
        })
    })
    .await;
    if let Err(error) = cancellation {
        let _ = custody
            .output_tx
            .send(SecretOutput::failed(SecretFailure::Gate(
                GateFailure::from_gate(error),
            )));
        let _ = custody.terminal_tx.send(Ok(()));
        return Ok(());
    }
    #[cfg(test)]
    if let Some(hooks) = &custody.hooks {
        hooks.before_consume().await;
    }
    // Revalidate the ORIGINAL context immediately before consume; the probe
    // never renews its deadline or changes its cancellation lineage.
    if let Err(error) = custody.cx.check_active() {
        let _ = custody
            .output_tx
            .send(SecretOutput::failed(SecretFailure::Gate(
                GateFailure::from_gate(error),
            )));
        let _ = custody.terminal_tx.send(Ok(()));
        return Ok(());
    }
    // Move only the original ordinary grant. Every handoff allocation,
    // capture, channel and timer already exists under the original funding.
    let attempt = custody
        .grant
        .consume_owned(&custody.cx, &custody.request, &custody.args);
    let consumed = match attempt {
        ConsumptionAttempt::BeforeConsumed(error) => {
            let _ = custody
                .output_tx
                .send(SecretOutput::failed(SecretFailure::Gate(
                    GateFailure::from_gate(error),
                )));
            let _ = custody.terminal_tx.send(Ok(()));
            return Ok(());
        }
        ConsumptionAttempt::CommitUnknown(unknown) => {
            let resolved = matches!(unknown.reconcile(), Ok(true));
            let error = cleanup_business(unknown.error());
            let _ = custody.output_tx.send(SecretOutput::failed(error));
            let _ = custody.terminal_tx.send(Ok(()));
            drop(unknown);
            return if resolved {
                Ok(())
            } else {
                Err(NativeWorkError::CompletionUnknown)
            };
        }
        ConsumptionAttempt::Consumed(consumed) => consumed,
    };
    let original_cx = consumed.context().clone();
    let permit = match consumed.into_dispatch() {
        Ok(permit) => permit,
        Err(phase) => {
            // The Grant field has moved into the owned consume phase. Keep the
            // remaining prepared custody in this supervisor until settlement.
            let business = cleanup_business(phase.error());
            let resolved = matches!(phase.settle_failure(custody.gate.as_ref()).await, Ok(true));
            let _ = custody.output_tx.send(SecretOutput::failed(business));
            let _ = custody.terminal_tx.send(Ok(()));
            drop(phase);
            return if resolved {
                Ok(())
            } else {
                Err(NativeWorkError::CompletionUnknown)
            };
        }
    };
    let mut dispatched = Dispatched {
        permit,
        cx: original_cx,
    };
    if let Err(error) = dispatched.narrow(custody.gate.as_ref()).await {
        let _ = custody
            .output_tx
            .send(SecretOutput::failed(SecretFailure::Gate(
                GateFailure::from_gate(error),
            )));
        let terminal = custody
            .gate
            .record_outcome(
                custody.id,
                SecretDisposition::Failed(FailureCode::NativeWork).into_outcome(),
            )
            .await;
        let _ = custody
            .terminal_tx
            .send(terminal.map_err(GateFailure::from_gate));
        drop(dispatched);
        return Ok(());
    }
    // Resetting the already allocated timer can only shorten its original bound.
    custody
        .deadline
        .as_mut()
        .reset(dispatched.cx.deadline().into());
    let work = BlockingCustody {
        work: SecretWork::new(custody.root, dispatched.cx, dispatched.permit),
        shared: custody.shared.clone(),
        #[cfg(test)]
        hooks: custody.hooks.clone(),
        _funding: custody.funding.clone(),
    };
    // Never select cancellation/deadline against this JoinHandle and never
    // abort or detach it. Close intent may suppress output, not physical IO.
    let mut completion = match tokio::task::spawn_blocking(blocking_body(work)).await {
        Ok(completion) => completion,
        Err(_) => {
            let _ = custody
                .output_tx
                .send(SecretOutput::failed(SecretFailure::Unavailable));
            let terminal = custody
                .gate
                .record_outcome(
                    custody.id,
                    SecretDisposition::Failed(FailureCode::NativeWork).into_outcome(),
                )
                .await;
            let _ = custody
                .terminal_tx
                .send(terminal.map_err(GateFailure::from_gate));
            return Err(NativeWorkError::CompletionUnknown);
        }
    };
    let task_cx = completion.context().clone();
    let output = SecretOutput {
        output_bytes: task_cx.limits().output_bytes,
        result: completion.take().map_err(SecretFailure::from_store),
    };
    let delivered = custody.output_tx.send(output).is_ok();
    let selected = if delivered {
        tokio::select! {
            result = custody.delivery_rx => result.unwrap_or(SecretDisposition::Cancelled),
            _ = custody.cancelled.as_mut() => SecretDisposition::Cancelled,
            _ = custody.deadline.as_mut() => SecretDisposition::Failed(FailureCode::Deadline),
        }
    } else {
        SecretDisposition::Cancelled
    };
    // One proposal, on the original ID. Gate preserves an earlier immutable
    // winner and acknowledged outbox; delivery failure never selects anew.
    let terminal = custody
        .gate
        .record_outcome(custody.id, selected.into_outcome())
        .await;
    let _ = custody
        .terminal_tx
        .send(terminal.map_err(GateFailure::from_gate));
    drop(completion);
    // Physical completion is distinct from guest conversion/business failure.
    Ok(())
}

/// The caller converts under its original Store/value owners; the accepted
/// supervisor retains the original permit/completion through that decision.
/// This helper captures no PackState, Store reference or guest value in the
/// Send + static native payload.
pub(super) async fn call<T, F>(
    host: &PackHost,
    cx: &CallCx,
    spawner: NativeWorkSpawner,
    grant: Grant,
    convert: F,
) -> Result<T>
where
    F: FnOnce(SecretWorkResult, u64) -> Result<T>,
{
    let name_bytes = match grant.effect() {
        EffectRequest::Secret { name, .. } if name.len() <= 240 => name.len(),
        _ => return Err(StepError::new("denied", "secret effect required").into()),
    };
    // Reserve before every new handoff allocation/capture. Values and existing
    // shared backend objects retain their original owners and policy bounds.
    let bytes = metadata_bytes::<T, F>()
        .and_then(|n| n.checked_add(host.shared.root.path().as_os_str().as_encoded_bytes().len()))
        .and_then(|n| n.checked_add(name_bytes))
        .and_then(|n| n.checked_add(grant.id().clone_storage_bytes()))
        .context("secret handoff size overflow")?;
    let reservation = cx
        .control()
        .reserve(ControlKind::Task, u64::try_from(bytes)?)?;
    let funding = Funding::try_new_precharged((), reservation)?;
    let id = grant.id().try_clone_precharged()?;
    let request = grant.effect().clone();
    let args = grant.args_digest().clone();
    let root = host.shared.root.as_ref().clone();
    let deadline = Box::pin(tokio::time::sleep_until(cx.deadline().into()));
    let cancelled = Box::pin(cx.cancel().cancelled());
    let (output_tx, output_rx) = oneshot::channel();
    let (delivery_tx, delivery_rx) = oneshot::channel();
    let (terminal_tx, terminal_rx) = oneshot::channel();
    let caller = CallerCustody {
        output_rx,
        delivery_tx,
        terminal_rx,
        convert,
        funding,
    };
    let custody = SecretCustody {
        grant,
        request,
        args,
        root,
        shared: host.shared.clone(),
        cx: cx.clone(),
        gate: host.shared.gate.clone(),
        id,
        output_tx,
        delivery_rx,
        terminal_tx,
        deadline,
        cancelled,
        #[cfg(test)]
        hooks: None,
        funding: caller.funding.clone(),
    };
    spawner.bind_cancel(cx.cancel().clone())?;
    // Rejection drops an unconsumed grant outside the registry guards. After
    // acceptance, only the native supervisor may consume and settle it.
    spawner.spawn(supervise(custody))?;
    let output = caller
        .output_rx
        .await
        .unwrap_or_else(|_| SecretOutput::failed(SecretFailure::Unavailable));
    let result = output
        .result
        .and_then(|value| {
            (caller.convert)(value, output.output_bytes).map_err(SecretFailure::from_conversion)
        })
        .and_then(|value| {
            cx.check_active()
                .map_err(|error| SecretFailure::Gate(GateFailure::from_gate(error)))?;
            Ok(value)
        });
    let _ = caller.delivery_tx.send(disposition(&result, cx));
    // Keep the business error heap-free while waiting for the original
    // terminal result. Terminal failure retains its original precedence.
    let terminal = caller
        .terminal_rx
        .await
        .map_err(|_| SecretFailure::Unavailable)
        .and_then(|result| result.map_err(SecretFailure::Gate));
    if let Err(error) = terminal {
        return Err(failure(error));
    }
    result
        .and_then(|value| {
            cx.check_active()
                .map_err(|error| SecretFailure::Gate(GateFailure::from_gate(error)))?;
            Ok(value)
        })
        .map_err(failure)
}

#[cfg(test)]
#[path = "secrets_imports_tests.rs"]
mod tests;
