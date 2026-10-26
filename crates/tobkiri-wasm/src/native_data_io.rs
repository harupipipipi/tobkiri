//! Native-only snapshot/read custody on the existing funded execution kernel.
//!
//! No Profile, Pack frame or guest-selected path is manufactured here. Native
//! callers retain this handle independently of the Store and drive close from
//! Core cleanup. Store destruction stops admission; it cannot finish OS IO.
//! Task allowances have the existing kernel's guarantees, not an exact Tokio
//! allocator/RSS or new thread-stack/TLS accounting claim.
use super::{
    CensusError, Completion, Funding, MONITOR_CONTROL, NativeWorkError, NativeWorkResult,
    PhysicalCompletion, PhysicalResult, RECORD_CONTROL, TASK_CONTROL, WAITER_CONTROL,
    arc_requested_bound, darwin_mutex_requested_bound, execution_owner,
};
use crate::{AllocationOwners, NativeCapturedInputs};
use std::{
    ffi::OsString,
    fmt,
    path::PathBuf,
    sync::{Arc, Mutex, OnceLock},
    time::Instant,
};
use tobkiri_canon::{AllocationKind, AllocationRequest, CoreBuffer, Digest, LeasedShared};
use tobkiri_gate::{
    ControlKind, ControlReservation, CoreCancellation, GateError, NativeSetupInvocation,
};
use tobkiri_store::{NativeFileSnapshot, NativeSetupOwnerLease, NativeSnapshotSource};
use tokio::{
    runtime::Handle,
    sync::{Mutex as AsyncMutex, Notify, oneshot},
};
use tokio_util::sync::CancellationToken;

#[path = "native_inspection_output.rs"]
mod inspection;
pub use inspection::{NativeInspectionCheckpoint, NativeInspectionOutput};

/// Closed execution failures, never an approval, native verification or grant.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum NativeDataIoError {
    /// Another read/capture or its unconsumed completion owns the original slot.
    Busy,
    /// Original bounded control/callback work is unavailable.
    Capacity,
    /// The original native invocation or its fixed capture was retired.
    Retired,
    /// The original absolute deadline expired.
    Deadline,
    /// The original cancellation was signalled.
    Cancelled,
    /// Required native scope or fixed capture is unavailable.
    Unavailable,
    /// The native snapshot or fixed-range read failed.
    Io,
    /// The original task/Join/destruction receipt is uncertain.
    CompletionUnknown,
}
impl fmt::Display for NativeDataIoError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Busy => "native DATA IO slot is occupied",
            Self::Capacity => "native DATA IO control capacity exhausted",
            Self::Retired => "native DATA IO invocation was retired",
            Self::Deadline => "original native DATA deadline exceeded",
            Self::Cancelled => "original native DATA cancellation signalled",
            Self::Unavailable => "native DATA IO scope is unavailable",
            Self::Io => "native captured IO failed",
            Self::CompletionUnknown => "native DATA physical IO completion is unknown",
        })
    }
}
impl std::error::Error for NativeDataIoError {}
type Result<T> = std::result::Result<T, NativeDataIoError>;

enum Value {
    Bytes(CoreBuffer),
    Snapshot(NativeFileSnapshot),
}
struct Scope {
    process: u32,
    invocation: NativeSetupInvocation,
    owners: AllocationOwners,
    cancel: CoreCancellation,
    retirement: CancellationToken,
    deadline: Instant,
    captured: OnceLock<NativeCapturedInputs>,
}
impl Scope {
    fn local_active(&self) -> Result<()> {
        if self.retirement.is_cancelled() {
            return Err(NativeDataIoError::Retired);
        }
        if Instant::now() >= self.deadline {
            return Err(NativeDataIoError::Deadline);
        }
        if self.cancel.is_cancelled() {
            return Err(NativeDataIoError::Cancelled);
        }
        Ok(())
    }
    fn work(&self, work: u64) -> Result<()> {
        // The real root/PID is checked before any local IO-state mutex.
        self.invocation
            .allocation_domain()
            .map_err(|_| NativeDataIoError::Unavailable)?;
        self.local_active()?;
        self.owners
            .graph()
            .reserve(AllocationRequest {
                kind: AllocationKind::Temporary,
                bytes: 0,
                elements: 0,
                depth: 0,
                work,
            })
            .map_err(|_| NativeDataIoError::Capacity)?;
        Ok(())
    }
}

struct Pending {
    slot: execution_owner::SlotId,
    changed: Arc<Notify>,
    physical: AsyncMutex<Completion>,
    value: Mutex<Option<Result<Value>>>,
}
type Request = LeasedShared<Pending, Funding>;
struct State {
    retired: bool,
    next: u64,
    pending: Option<(u64, Request)>,
    cleanup_waiting: bool,
}
struct Inner {
    scope: Scope,
    runtime: Handle,
    state: Mutex<State>,
}
// One cleanup waiter is reserved before any task. Shutdown at full admission
// does not depend on obtaining fresh Task/Queue/Waiter capacity.
type Held = LeasedShared<Inner, (ControlReservation, ControlReservation)>;

/// A closed native invocation's actual IO custody, independent of its Store.
/// Cloning shares original admission and accounting; it creates no capacity.
#[derive(Clone)]
pub struct NativeDataIo(Held);

impl NativeDataIo {
    pub(crate) fn new(
        invocation: NativeSetupInvocation,
        owners: AllocationOwners,
        cancel: CoreCancellation,
        retirement: CancellationToken,
    ) -> Result<Self> {
        let deadline = invocation
            .deadline()
            .map_err(|_| NativeDataIoError::Unavailable)?;
        let scope = Scope {
            process: std::process::id(),
            invocation,
            owners,
            cancel,
            retirement,
            deadline,
            captured: OnceLock::new(),
        };
        scope.work(1)?;
        let runtime = Handle::try_current().map_err(|_| NativeDataIoError::Unavailable)?;
        let bytes = (Held::storage_bytes() as u64)
            .checked_add(darwin_mutex_requested_bound().map_err(|_| NativeDataIoError::Capacity)?)
            .ok_or(NativeDataIoError::Capacity)?;
        let control = scope
            .invocation
            .control()
            .reserve(ControlKind::Registration, bytes)
            .map_err(|_| NativeDataIoError::Capacity)?;
        let waiter = scope
            .invocation
            .control()
            .reserve(
                ControlKind::Waiter,
                WAITER_CONTROL
                    .checked_add(cleanup_future_bytes() as u64)
                    .ok_or(NativeDataIoError::Capacity)?,
            )
            .map_err(|_| NativeDataIoError::Capacity)?;
        let state = Mutex::new(State {
            retired: false,
            next: 1,
            pending: None,
            cleanup_waiting: false,
        });
        drop(
            state
                .try_lock()
                .map_err(|_| NativeDataIoError::Unavailable)?,
        );
        Held::try_new_precharged(
            Inner {
                scope,
                runtime,
                state,
            },
            (control, waiter),
        )
        .map(Self)
        .map_err(|_| NativeDataIoError::Capacity)
    }

    pub(crate) fn bind_captured_inputs(&self, inputs: NativeCapturedInputs) -> Result<()> {
        self.0.get().scope.work(1)?;
        if !inputs
            .belongs_to(&self.0.get().scope.invocation)
            .map_err(|_| NativeDataIoError::Unavailable)?
        {
            return Err(NativeDataIoError::Unavailable);
        }
        self.0
            .get()
            .scope
            .captured
            .set(inputs)
            .map_err(|_| NativeDataIoError::Unavailable)
    }

    /// Read only a bound immutable file ordinal/range. Returned DATA keeps the
    /// original 64MiB owner; caller/ABI copies must retain that same accounting.
    pub async fn read_range(&self, ordinal: u32, offset: u64, length: u32) -> Result<CoreBuffer> {
        let captured = self
            .0
            .get()
            .scope
            .captured
            .get()
            .ok_or(NativeDataIoError::Unavailable)?
            .clone();
        let value = self
            .execute(move || {
                captured
                    .read_range(ordinal, offset, length)
                    .map(Value::Bytes)
                    .map_err(|_| NativeDataIoError::Io)
            })
            .await?;
        match value {
            Value::Bytes(bytes) => Ok(bytes),
            _ => Err(NativeDataIoError::CompletionUnknown),
        }
    }

    /// Freeze a trusted native-selected source on the managed IO worker. Paths
    /// and root leases come only from native inspection, never a guest import.
    pub async fn capture_file_snapshot(
        &self,
        root: &NativeSetupOwnerLease,
        source: NativeSnapshotSource<'_>,
    ) -> Result<NativeFileSnapshot> {
        let source = OwnedSource::new(self, root, source)?;
        let held = self.clone();
        let value = self
            .execute(move || {
                let source = source.get();
                let scope = &held.0.get().scope;
                let mut charge = |work| {
                    scope.work(work).map_err(|error| match error {
                        NativeDataIoError::Deadline => GateError::Deadline,
                        NativeDataIoError::Cancelled | NativeDataIoError::Retired => {
                            GateError::Cancelled
                        }
                        _ => GateError::Unavailable,
                    })
                };
                source
                    .root
                    .capture_file_snapshot(
                        &scope.invocation,
                        NativeSnapshotSource {
                            root: &source.path,
                            relative: &source.relative,
                            length: source.length,
                            sha256: &source.sha256,
                        },
                        &scope.cancel,
                        &mut charge,
                    )
                    .map(Value::Snapshot)
                    .map_err(|_| NativeDataIoError::Io)
            })
            .await?;
        match value {
            Value::Snapshot(snapshot) => Ok(snapshot),
            _ => Err(NativeDataIoError::CompletionUnknown),
        }
    }

    async fn execute<F>(&self, work: F) -> Result<Value>
    where
        F: FnOnce() -> Result<Value> + Send + 'static,
    {
        self.0.get().scope.work(1)?;
        let waiter = self
            .0
            .get()
            .scope
            .invocation
            .control()
            .reserve(
                ControlKind::Waiter,
                WAITER_CONTROL
                    .checked_add(read_future_bytes() as u64)
                    .ok_or(NativeDataIoError::Capacity)?,
            )
            .map_err(|_| NativeDataIoError::Capacity)?;
        let (id, request) = self.submit(work)?;
        let mut guard = ReadGuard {
            io: self.clone(),
            completed: false,
        };
        let result = await_read(self.clone(), id, request).await;
        if result.is_ok() {
            guard.completed = true;
        }
        drop(waiter);
        result
    }

    fn submit<F>(&self, work: F) -> Result<(u64, Request)>
    where
        F: FnOnce() -> Result<Value> + Send + 'static,
    {
        let inner = self.0.get();
        inner.scope.work(1)?;
        let record_bytes = execution_owner::metadata_bytes()
            .map_err(|_| NativeDataIoError::Capacity)?
            .checked_add(Request::storage_bytes() as u64)
            .and_then(|n| n.checked_add(Funding::storage_bytes() as u64))
            .and_then(|n| n.checked_add(arc_requested_bound::<Notify>().ok()?))
            .and_then(|n| n.checked_add(darwin_mutex_requested_bound().ok()?))
            .and_then(|n| n.checked_add(RECORD_CONTROL))
            .ok_or(NativeDataIoError::Capacity)?;
        let funding = Funding::try_new_precharged(
            (),
            inner
                .scope
                .invocation
                .control()
                .reserve(ControlKind::Queue, record_bytes)
                .map_err(|_| NativeDataIoError::Capacity)?,
        )
        .map_err(|_| NativeDataIoError::Capacity)?;
        let body_control = inner
            .scope
            .invocation
            .control()
            .reserve(
                ControlKind::Task,
                TASK_CONTROL
                    .checked_add(size_of::<F>() as u64)
                    .ok_or(NativeDataIoError::Capacity)?,
            )
            .map_err(|_| NativeDataIoError::Capacity)?;
        let monitor_control = inner
            .scope
            .invocation
            .control()
            .reserve(
                ControlKind::Task,
                MONITOR_CONTROL
                    .checked_add(size_of::<F>() as u64)
                    .ok_or(NativeDataIoError::Capacity)?,
            )
            .map_err(|_| NativeDataIoError::Capacity)?;
        let (physical_tx, physical_rx) = oneshot::channel();
        let changed = Arc::new(Notify::new());
        changed.notify_waiters();
        let (admission, mut publication) = execution_owner::reserve(
            funding.clone(),
            body_control,
            monitor_control,
            physical_tx,
            changed.clone(),
        )
        .map_err(|_| NativeDataIoError::Capacity)?;
        let slot = admission.id();
        let value = Mutex::new(None);
        drop(
            value
                .try_lock()
                .map_err(|_| NativeDataIoError::Unavailable)?,
        );
        let request = Request::try_new_precharged(
            Pending {
                slot,
                changed,
                physical: AsyncMutex::new(Completion {
                    receiver: Some(physical_rx),
                    result: None,
                }),
                value,
            },
            funding,
        )
        .map_err(|_| NativeDataIoError::Capacity)?;
        let active = inner.scope.local_active();
        let id = {
            let mut state = inner
                .state
                .lock()
                .map_err(|_| NativeDataIoError::Unavailable)?;
            if state.retired {
                return Err(NativeDataIoError::Retired);
            }
            active?;
            if state.pending.is_some() {
                return Err(NativeDataIoError::Busy);
            }
            let id = state.next;
            let next = id.checked_add(1).ok_or(NativeDataIoError::Capacity)?;
            publication
                .commit()
                .map_err(|_| NativeDataIoError::CompletionUnknown)?;
            state.next = next;
            state.pending = Some((id, request.clone()));
            id
        };
        let io = self.clone();
        let retained = request.clone();
        let started = publication.spawn_monitor(|| {
            inner
                .runtime
                .spawn(async move { run_monitor(io, id, retained, work).await })
        });
        if started.is_err() {
            self.retire();
            return Err(NativeDataIoError::CompletionUnknown);
        }
        drop(admission);
        Ok((id, request))
    }

    /// Stop admission/publishing without blocking the GUI/executor on OS IO.
    /// In-flight work and the original completion remain independently owned.
    pub fn retire(&self) {
        let inner = self.0.get();
        if inner.scope.process != std::process::id() {
            return;
        }
        let discarded = {
            let mut state = inner.state.lock().unwrap_or_else(|e| e.into_inner());
            state.retired = true;
            state.pending.as_ref().and_then(|(_, request)| {
                request
                    .get()
                    .value
                    .lock()
                    .unwrap_or_else(|e| e.into_inner())
                    .take()
            })
        };
        inner.scope.retirement.cancel();
        inner.scope.cancel.cancel();
        drop(discarded);
    }

    /// Core cleanup drives the original Joins after Store/consumer destruction.
    /// This wait does not reuse the expired DATA deadline or renew a lease. A
    /// permanently blocked IO remains pending and retains its original funding.
    pub async fn retire_and_wait(&self) -> Result<()> {
        if self.0.get().scope.process != std::process::id() {
            return Err(NativeDataIoError::Unavailable);
        }
        self.retire();
        let request = {
            let mut state = self
                .0
                .get()
                .state
                .lock()
                .map_err(|_| NativeDataIoError::Unavailable)?;
            if state.cleanup_waiting {
                return Err(NativeDataIoError::Busy);
            }
            state.cleanup_waiting = true;
            state
                .pending
                .as_ref()
                .map(|(id, request)| (*id, request.clone()))
        };
        let _guard = CleanupGuard(self.clone());
        await_cleanup(self.clone(), request).await
    }
}

struct ReadGuard {
    io: NativeDataIo,
    completed: bool,
}
impl Drop for ReadGuard {
    fn drop(&mut self) {
        if !self.completed {
            self.io.retire();
        }
    }
}
struct CleanupGuard(NativeDataIo);
impl Drop for CleanupGuard {
    fn drop(&mut self) {
        if self.0.0.get().scope.process != std::process::id() {
            return;
        }
        self.0
            .0
            .get()
            .state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .cleanup_waiting = false;
    }
}

async fn wait_physical(request: &Request) -> PhysicalResult {
    loop {
        let changed = request.get().changed.clone().notified_owned();
        tokio::pin!(changed);
        changed.as_mut().enable();
        execution_owner::progress(request.get().slot);
        let result = {
            let mut completion = request.get().physical.lock().await;
            match completion.result {
                Some(result) => Some(result),
                None => match completion.receiver.as_mut() {
                    Some(receiver) => match receiver.try_recv() {
                        Ok(result) => {
                            completion.receiver.take();
                            completion.result = Some(result);
                            Some(result)
                        }
                        Err(oneshot::error::TryRecvError::Empty) => None,
                        Err(oneshot::error::TryRecvError::Closed) => {
                            completion.receiver.take();
                            completion.result = Some(Err(CensusError::ReceiptLost));
                            completion.result
                        }
                    },
                    None => Some(Err(CensusError::ReceiptLost)),
                },
            }
        };
        if let Some(result) = result {
            return result;
        }
        changed.await;
    }
}

async fn await_read(io: NativeDataIo, id: u64, request: Request) -> Result<Value> {
    let scope = &io.0.get().scope;
    let completion = tokio::select! {
        biased;
        _ = scope.retirement.cancelled() => return Err(NativeDataIoError::Retired),
        cancel = scope.cancel.cancelled() => return Err(if cancel.is_ok() { NativeDataIoError::Cancelled } else { NativeDataIoError::Capacity }),
        _ = tokio::time::sleep_until(scope.deadline.into()) => return Err(NativeDataIoError::Deadline),
        result = wait_physical(&request) => result,
    };
    match completion {
        Ok(PhysicalCompletion::Joined) => {}
        _ => return Err(NativeDataIoError::CompletionUnknown),
    }
    scope.work(1)?;
    let active = scope.local_active();
    let (pending, value) = {
        let mut state =
            io.0.get()
                .state
                .lock()
                .map_err(|_| NativeDataIoError::Unavailable)?;
        if state.retired {
            return Err(NativeDataIoError::Retired);
        }
        active?;
        if state
            .pending
            .as_ref()
            .is_none_or(|(current, _)| *current != id)
        {
            return Err(NativeDataIoError::CompletionUnknown);
        }
        let value = request
            .get()
            .value
            .lock()
            .map_err(|_| NativeDataIoError::CompletionUnknown)?
            .take();
        (state.pending.take(), value)
    };
    drop(pending);
    value.ok_or(NativeDataIoError::CompletionUnknown)?
}
async fn await_cleanup(io: NativeDataIo, request: Option<(u64, Request)>) -> Result<()> {
    if let Some((id, request)) = request {
        match wait_physical(&request).await {
            Ok(PhysicalCompletion::Joined) => {}
            _ => return Err(NativeDataIoError::CompletionUnknown),
        }
        let pending = {
            let mut state =
                io.0.get()
                    .state
                    .lock()
                    .map_err(|_| NativeDataIoError::Unavailable)?;
            if state
                .pending
                .as_ref()
                .is_some_and(|(current, _)| *current == id)
            {
                state.pending.take()
            } else {
                None
            }
        };
        drop(pending);
    }
    Ok(())
}
fn read_future_bytes() -> usize {
    fn size<F: std::future::Future>(_: fn(NativeDataIo, u64, Request) -> F) -> usize {
        size_of::<F>()
    }
    size(await_read)
}
fn cleanup_future_bytes() -> usize {
    fn size<F: std::future::Future>(_: fn(NativeDataIo, Option<(u64, Request)>) -> F) -> usize {
        size_of::<F>()
    }
    size(await_cleanup)
}

async fn run_monitor<F>(io: NativeDataIo, id: u64, request: Request, work: F) -> PhysicalResult
where
    F: FnOnce() -> Result<Value> + Send + 'static,
{
    let slot = request.get().slot;
    let mut work = Some(work);
    loop {
        let changed = request.get().changed.clone().notified_owned();
        tokio::pin!(changed);
        changed.as_mut().enable();
        let mut publication = match execution_owner::prepare(slot, execution_owner::Kind::Body) {
            Ok(execution_owner::Prepared::Busy) => {
                changed.await;
                continue;
            }
            Ok(execution_owner::Prepared::Ready(publication)) => publication,
            Err(error) => return Err(error),
        };
        let active = io.0.get().scope.local_active().is_ok();
        let run_work = {
            let state =
                io.0.get()
                    .state
                    .lock()
                    .map_err(|_| CensusError::StateUnavailable)?;
            // A retired admission still gets a tracked destruction body. It
            // drops the unstarted capture outside all locks; it never calls
            // the input operation. Cleanup has its own actual original Join.
            publication.commit()?;
            !state.retired
                && active
                && state
                    .pending
                    .as_ref()
                    .is_some_and(|(current, _)| *current == id)
        };
        let body_io = io.clone();
        let body_request = request.clone();
        let body_work = work.take().expect("native IO work taken twice");
        publication.spawn_body(|| {
            io.0.get().runtime.spawn_blocking(move || {
                let value = if run_work {
                    match body_io.0.get().scope.work(1) {
                        Ok(()) => body_work(),
                        Err(error) => Err(error),
                    }
                } else {
                    drop(body_work);
                    Err(NativeDataIoError::Retired)
                };
                let active = body_io.0.get().scope.local_active().is_ok();
                let discarded = {
                    let state = body_io
                        .0
                        .get()
                        .state
                        .lock()
                        .map_err(|_| NativeWorkError::CompletionUnknown)?;
                    if state.retired
                        || !active
                        || state
                            .pending
                            .as_ref()
                            .is_none_or(|(current, _)| *current != id)
                    {
                        Some(value)
                    } else {
                        let mut output = body_request
                            .get()
                            .value
                            .lock()
                            .map_err(|_| NativeWorkError::CompletionUnknown)?;
                        if output.is_some() {
                            return Err(NativeWorkError::CompletionUnknown);
                        }
                        *output = Some(value);
                        None
                    }
                };
                drop(discarded);
                NativeWorkResult::Ok(())
            })
        })?;
        break;
    }
    let result = match execution_owner::wait_body(slot, &request.get().changed).await {
        Ok(Ok(())) => Ok(PhysicalCompletion::Joined),
        _ => Err(CensusError::TaskUnknown),
    };
    drop(work);
    drop(request);
    drop(io);
    execution_owner::maintain(Some(slot));
    result
}

struct OwnedSource {
    root: NativeSetupOwnerLease,
    path: PathBuf,
    relative: String,
    length: u64,
    sha256: Digest,
}
impl OwnedSource {
    fn new(
        io: &NativeDataIo,
        root: &NativeSetupOwnerLease,
        source: NativeSnapshotSource<'_>,
    ) -> Result<LeasedShared<Self, ControlReservation>> {
        let scope = &io.0.get().scope;
        scope.work(1)?;
        let bytes = LeasedShared::<Self, ControlReservation>::storage_bytes()
            .checked_add(source.root.as_os_str().len())
            .and_then(|n| n.checked_add(source.relative.len()))
            .and_then(|n| u64::try_from(n).ok())
            .ok_or(NativeDataIoError::Capacity)?;
        let control = scope
            .invocation
            .control()
            .reserve(ControlKind::Frame, bytes)
            .map_err(|_| NativeDataIoError::Capacity)?;
        let mut path = OsString::new();
        path.try_reserve_exact(source.root.as_os_str().len())
            .map_err(|_| NativeDataIoError::Capacity)?;
        let mut relative = String::new();
        relative
            .try_reserve_exact(source.relative.len())
            .map_err(|_| NativeDataIoError::Capacity)?;
        if path.capacity() != source.root.as_os_str().len()
            || relative.capacity() != source.relative.len()
        {
            return Err(NativeDataIoError::Capacity);
        }
        path.push(source.root.as_os_str());
        relative.push_str(source.relative);
        LeasedShared::try_new_precharged(
            Self {
                root: root.clone(),
                path: PathBuf::from(path),
                relative,
                length: source.length,
                sha256: source.sha256.clone(),
            },
            control,
        )
        .map_err(|_| NativeDataIoError::Capacity)
    }
}

#[cfg(test)]
#[path = "native_data_io_tests.rs"]
mod tests;
