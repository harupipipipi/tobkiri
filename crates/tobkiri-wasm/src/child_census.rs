//! Exact-generation custody for separately scheduled native work.
//!
//! A VM Store's destruction says nothing about these tasks. The trusted host
//! derives the generation and execution IDs from its actual installed Core
//! frame, admits work here BEFORE starting it, then seals this census while
//! retiring that same generation. These IDs confer no authority or funding.
//!
//! The physical task must await every native body it starts (including blocking
//! work), and destroy its resources before returning. A stream close, token
//! notification, RunResult, terminal ACK, or detached-child wrapper is NOT an
//! acceptable task. Audit/terminal delivery must not await the parent whose
//! retirement is waiting here. Guest buffers keep their separate owner leases.
//!
//! Q28: slot registration and Init commit under `state` are the start
//! linearization point. A previously committed task may first poll after seal;
//! it remains in the census and its retained close companion receives intent.
//! No task body, close callback, await, or captured-payload destruction runs
//! synchronously under this mutex. Tokio spawn does not synchronously poll.

use std::alloc::Layout;
use std::fmt;
use std::future::Future;
use std::mem::size_of;
use std::panic::{AssertUnwindSafe, catch_unwind};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, Weak};
#[cfg(test)]
use std::task::Wake;
use std::task::{Context, Poll, Waker};

use tobkiri_canon::{
    Digest, LeasedShared,
    ids::{PackHash, PackId, ProfileId},
};
use tobkiri_flow::{StreamCloseControl, StreamCloseReason};
use tobkiri_gate::{ControlKind, ControlReservation, CoreControl};
use tokio::runtime::Handle;
use tokio::sync::{Mutex as AsyncMutex, oneshot};

const MAX_CHILDREN: usize = 256;
const MAX_FUTURE_BYTES: usize = 256 * 1024;
// Bounded trusted-native control allowances, not an exact Tokio allocator/RSS
// claim. The work's external buffers, FD/IO and interpreter leases are separate.
const GENERATION_CONTROL: u64 = 1024;
const RECORD_CONTROL: u64 = 2048;
const TASK_CONTROL: u64 = 4096;
const MONITOR_CONTROL: u64 = 4096;
const WAITER_CONTROL: u64 = 512;

#[path = "native_data_io.rs"]
mod native_data_io;
pub use native_data_io::{
    NativeDataIo, NativeDataIoError, NativeInspectionCheckpoint, NativeInspectionOutput,
};

// Independent fixtures share the process-wide execution owner. Keep their
// maintenance scans from consuming another fixture's original Joins or waking
// its observers; intentional concurrency within each fixture is unchanged.
// Acquire once at the outer test boundary, never from a spawned child/helper.
#[cfg(test)]
static CENSUS_FIXTURE: AsyncMutex<()> = AsyncMutex::const_new(());

#[cfg(test)]
pub(crate) async fn lock_census_fixture() -> tokio::sync::MutexGuard<'static, ()> {
    CENSUS_FIXTURE.lock().await
}

#[cfg(test)]
fn lock_census_fixture_blocking() -> tokio::sync::MutexGuard<'static, ()> {
    CENSUS_FIXTURE.blocking_lock()
}

// A conservative requested-layout bound for the pinned repr(Rust) field set.
// Rounding EACH field to the greatest field alignment covers every field
// permutation and the final tail padding. This is not a proxy ABI assertion.
fn requested_struct_bound(fields: &[Layout]) -> Result<Layout, CensusError> {
    let alignment = fields.iter().map(Layout::align).max().unwrap_or(1);
    let mut size = 0usize;
    for field in fields {
        let rounded = field
            .size()
            .checked_add(alignment - 1)
            .map(|bytes| bytes / alignment * alignment)
            .ok_or(CensusError::Capacity)?;
        size = size.checked_add(rounded).ok_or(CensusError::Capacity)?;
    }
    Layout::from_size_align(size, alignment).map_err(|_| CensusError::Capacity)
}

// Pinned alloc::sync::ArcInner has strong, weak and data fields. The actual
// payload's alignment is included; the bound also covers its final Arc block.
fn arc_requested_bound<T>() -> Result<u64, CensusError> {
    let count = Layout::new::<std::sync::atomic::AtomicUsize>();
    let layout = requested_struct_bound(&[count, count, Layout::new::<T>()])?;
    u64::try_from(layout.size()).map_err(|_| CensusError::Capacity)
}

fn darwin_mutex_requested_bound() -> Result<u64, CensusError> {
    // Pinned aarch64-apple-darwin PAL pthread_mutex_t field set.
    let layout = requested_struct_bound(&[
        Layout::new::<std::os::raw::c_long>(),
        Layout::new::<[u8; 56]>(),
    ])?;
    u64::try_from(layout.size()).map_err(|_| CensusError::Capacity)
}

/// Captured from the actual instance slot, never from guest request labels.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NativeGenerationId {
    pub profile: ProfileId,
    pub pack: PackId,
    pub hash: PackHash,
    pub revision: Digest,
    pub security_epoch: u64,
    pub generation: u64,
}

/// Captured from the actual Core execution frame before child admission.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NativeExecutionId {
    pub frame: Digest,
    pub request: Digest,
    pub activation: u64,
}

/// A monotonically assigned slot, meaningful only with its exact generation.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct NativeWorkId(u64);
impl NativeWorkId {
    pub fn get(self) -> u64 {
        self.0
    }
}

/// Closed metadata only; guest text and task panic payloads never escape here.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CensusError {
    Invalid,
    Capacity,
    Sealed,
    StateUnavailable,
    LaunchUnknown,
    TaskUnknown,
    ReceiptLost,
    CloseUnknown,
}
impl fmt::Display for CensusError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Invalid => "invalid native work census admission",
            Self::Capacity => "native work census capacity exhausted",
            Self::Sealed => "native work generation is retiring",
            Self::StateUnavailable => "native work census state is unavailable",
            Self::LaunchUnknown => "native work launch completion is unknown",
            Self::TaskUnknown => "native work physical completion is unknown",
            Self::ReceiptLost => "native work physical receipt was lost",
            Self::CloseUnknown => "native work close notification is unknown",
        })
    }
}
impl std::error::Error for CensusError {}

/// The trusted task must preserve uncertainty from any nested native Join.
/// A normal supervisor return after losing its blocking-work receipt cannot
/// turn into a physical success merely because that supervisor itself joined.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum NativeWorkError {
    CompletionUnknown,
}
pub type NativeWorkResult = Result<(), NativeWorkError>;

/// Trusted submission contract for cancellation BEFORE the task is published.
/// This is cleanup responsibility, not an authority or completion assertion.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum UnstartedCleanupPolicy {
    /// Safe default: the capture may contain already-started IO, a transaction,
    /// another task, or any asynchronous cleanup responsibility. Destroying the
    /// unpublished future cannot produce a physical completion receipt. Such
    /// work needs its real typed completion path, or remains unknown here.
    #[default]
    RequiresCompletion,
    /// The trusted adapter has verified ALL captures are unstarted work or
    /// resources whose cleanup completes through ordinary synchronous RAII
    /// (for example an unused FD/buffer). No already-started IO, transaction,
    /// other task or asynchronous destruction responsibility is captured.
    /// Only unpublished status PLUS successful payload Drop outside all guards
    /// can establish NeverStarted. A Drop panic remains unknown.
    #[cfg(test)]
    SynchronousDrop,
}

#[derive(Clone, Copy)]
enum PhysicalCompletion {
    Joined,
    // Used only when this module itself proves no work spawn was attempted.
    #[cfg(test)]
    NeverStarted,
}
type PhysicalResult = Result<PhysicalCompletion, CensusError>;

struct Completion {
    receiver: Option<oneshot::Receiver<PhysicalResult>>,
    result: Option<PhysicalResult>,
}
struct LaunchCompletion {
    receiver: Option<oneshot::Receiver<Result<(), CensusError>>>,
    result: Option<Result<(), CensusError>>,
}
struct WorkRecord {
    id: NativeWorkId,
    execution: NativeExecutionId,
    close: Option<Weak<dyn StreamCloseControl>>,
    close_result: Mutex<Option<Result<(), CensusError>>>,
    completion: AsyncMutex<Completion>,
    started: AsyncMutex<LaunchCompletion>,
    launch_allowed: AtomicBool,
    execution_slot: execution_owner::SlotId,
    changed: Arc<tokio::sync::Notify>,
    control: CoreControl,
    // The monitor retains this record until its body Join resolves. The
    // independent process owner holds both Joins; this record has only its ID.
    _runtime: Handle,
}
/// External funding follows the data Arc through its actual deallocation.
/// The LeasedShared block is freed BEFORE its inline ControlReservation can
/// refund, so every allocation inside this bundle is covered through its
/// physical last drop; its storage_bytes is included in the precharge below.
type Funding = LeasedShared<(), ControlReservation>;
#[derive(Clone)]
struct FundedWorkRecord {
    record: Arc<WorkRecord>,
    _control: Funding,
}
impl std::ops::Deref for FundedWorkRecord {
    type Target = WorkRecord;
    fn deref(&self) -> &Self::Target {
        &self.record
    }
}
impl fmt::Debug for WorkRecord {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("WorkRecord")
            .field("id", &self.id)
            .field("execution", &self.execution)
            .finish_non_exhaustive()
    }
}
impl WorkRecord {
    fn try_completion(&self) -> Option<PhysicalResult> {
        let mut completion = self.completion.try_lock().ok()?;
        if let Some(result) = completion.result {
            return Some(result);
        }
        let result = match completion.receiver.as_mut()?.try_recv() {
            Ok(result) => result,
            Err(oneshot::error::TryRecvError::Empty) => return None,
            Err(oneshot::error::TryRecvError::Closed) => Err(CensusError::ReceiptLost),
        };
        completion.receiver.take();
        completion.result = Some(result);
        Some(result)
    }

    // Completion-cache check under the async guard with no other guard held;
    // released before any Notify wait, progress callback or other await.
    async fn cached_completion(&self) -> Option<PhysicalResult> {
        let mut completion = self.completion.lock().await;
        if let Some(result) = completion.result {
            return Some(result);
        }
        let result = match completion.receiver.as_mut()?.try_recv() {
            Ok(result) => result,
            Err(oneshot::error::TryRecvError::Empty) => return None,
            Err(oneshot::error::TryRecvError::Closed) => Err(CensusError::ReceiptLost),
        };
        completion.receiver.take();
        completion.result = Some(result);
        Some(result)
    }

    async fn wait_physical(&self) -> PhysicalResult {
        let _waiter = self
            .control
            .reserve(ControlKind::Waiter, WAITER_CONTROL)
            .map_err(|_| CensusError::Capacity)?;
        loop {
            let changed = self.changed.clone().notified_owned();
            tokio::pin!(changed);
            changed.as_mut().enable();
            execution_owner::progress(self.execution_slot);
            if let Some(result) = self.cached_completion().await {
                return result;
            }
            changed.await;
        }
    }

    fn notify_close(&self, reason: StreamCloseReason) -> Result<(), CensusError> {
        {
            let mut notification = self
                .close_result
                .lock()
                .map_err(|_| CensusError::StateUnavailable)?;
            if let Some(result) = *notification {
                return result;
            }
            // No callback is invoked under this guard. A concurrent notifier
            // conservatively observes in-progress/unknown, never false success.
            *notification = Some(Err(CensusError::CloseUnknown));
        }
        // Work owns the strong companion through destruction of its future.
        // A missing weak target may mean the task already destroyed it; only
        // the Join receipt, not this observation, proves completion.
        let result = self
            .close
            .as_ref()
            .and_then(Weak::upgrade)
            .map(|control| {
                catch_unwind(AssertUnwindSafe(|| control.record_close(reason)))
                    .map_err(|_| CensusError::CloseUnknown)?
                    .map_err(|_| CensusError::CloseUnknown)
            })
            .unwrap_or(Ok(()));
        *self
            .close_result
            .lock()
            .map_err(|_| CensusError::StateUnavailable)? = Some(result);
        result
    }
}

struct CensusState {
    closing: Option<StreamCloseReason>,
    next_id: u64,
    #[cfg(test)]
    completed: u64,
    #[cfg(test)]
    never_started: u64,
    records: Vec<FundedWorkRecord>,
}
struct CensusInner {
    generation: NativeGenerationId,
    control: CoreControl,
    runtime: Handle,
    max_children: usize,
    state: Mutex<CensusState>,
    _control: ControlReservation,
}

/// One account for one exact Store generation, retained beyond VM destruction.
/// Cloning does not allocate or create admission capacity.
#[derive(Clone)]
pub struct GenerationChildCensus {
    inner: Arc<CensusInner>,
}

/// Observer of one original work record's physical Join. Cancelling a wait
/// retains that record and its same receiver; this conveys no task authority.
#[derive(Clone)]
pub(crate) struct NativeWorkCompletion {
    record: FundedWorkRecord,
}
impl NativeWorkCompletion {
    pub(crate) fn notify_close(&self, reason: StreamCloseReason) -> Result<(), CensusError> {
        self.record.notify_close(reason)
    }
    /// Publication only: the independent owner already holds the body Join.
    /// This does not certify execution, cleanup or physical completion.
    pub(crate) async fn wait_started(&self) -> Result<(), CensusError> {
        let mut started = self.record.started.lock().await;
        if let Some(result) = started.result {
            return result;
        }
        let result = match started.receiver.as_mut() {
            Some(receiver) => receiver.await.unwrap_or(Err(CensusError::ReceiptLost)),
            None => Err(CensusError::ReceiptLost),
        };
        started.receiver.take();
        started.result = Some(result);
        result
    }
    pub(crate) async fn wait_joined(&self) -> Result<(), CensusError> {
        #[cfg(test)]
        {
            match self.record.wait_physical().await? {
                PhysicalCompletion::Joined => Ok(()),
                PhysicalCompletion::NeverStarted => Err(CensusError::TaskUnknown),
            }
        }
        #[cfg(not(test))]
        self.record.wait_physical().await.map(|_| ())
    }
}

// This extra owner prevents a failed spawn's wrapper destruction from dropping
// captured Core/IO payload under the publication mutex. Spawn only receives an
// Arc clone; the outer owner is destroyed after releasing every census guard.
struct LaunchCarrier<F>(Mutex<Option<F>>);
impl<F> LaunchCarrier<F> {
    fn destroy_unstarted(&self) -> Result<(), CensusError> {
        let future = {
            let mut slot = self.0.lock().map_err(|_| CensusError::StateUnavailable)?;
            slot.take().ok_or(CensusError::TaskUnknown)?
        };
        // Extract explicitly rather than relying on last Arc destruction: the
        // submitting thread may still retain its carrier during this race.
        // The actual F is destroyed here, outside both carrier and census locks.
        catch_unwind(AssertUnwindSafe(|| drop(future))).map_err(|_| CensusError::TaskUnknown)
    }
}

/// Independent process custody. Records and task futures carry only SlotId;
/// no receipt/census owns this pool or a slot containing its own actual Join.
mod execution_owner {
    use super::*;
    use std::sync::OnceLock;
    use std::sync::atomic::{AtomicU8, AtomicU64};
    use tokio::task::{JoinError, JoinHandle};

    const SLOTS: usize = 256;
    const CHANGED: u8 = 1;
    const REPOLL: u8 = 2;
    static NEXT_ID: AtomicU64 = AtomicU64::new(1);
    const EMPTY: u64 = 0;
    const AVAILABLE: u64 = 1;
    const BORROWED: u64 = 2;
    const RETIRING: u64 = 3;
    const PHASE_MASK: u64 = 3;
    const MAX_INCARNATION: u64 = u64::MAX >> 2;
    // Pinned, poison-free guard: ONLY exclusive try_write/ordinary Drop.
    // Gate ownership keeps raw state 0/WRITER_BIT, closing slow/park paths.
    // Root must bind the actual dependency graph with deadlock_detection OFF.
    struct OwnerCell {
        value: parking_lot::RwLock<Option<FundedSlot>>,
        gate: AtomicU64,
        requested: AtomicU64,
    }
    impl OwnerCell {
        const fn new() -> Self {
            Self {
                value: parking_lot::RwLock::new(None),
                gate: AtomicU64::new(EMPTY),
                requested: AtomicU64::new(0),
            }
        }
    }
    static OWNER: [OwnerCell; SLOTS] = [const { OwnerCell::new() }; SLOTS];
    fn encoded(id: SlotId, phase: u64) -> u64 {
        (id.0 << 2) | phase
    }
    fn incarnation(value: u64) -> SlotId {
        SlotId(value >> 2)
    }

    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    pub(super) struct SlotId(u64);
    #[derive(Clone)]
    struct FundedSlot {
        slot: Arc<Slot>,
        // Field order: the actual Slot Arc block dies before its funding.
        _control: Funding,
    }
    struct Slot {
        id: SlotId,
        state: parking_lot::RwLock<State>,
        state_gate: AtomicU8,
        cells: [OnceLock<Box<parking_lot::RwLock<JoinState>>>; 2],
        cell_gates: [AtomicU8; 2],
        attempted: [AtomicBool; 2],
        no_task: [AtomicBool; 2],
        destroyed: [AtomicBool; 2],
        waker_destroyed: [AtomicBool; 2],
        monitor_result: OnceLock<PhysicalResult>,
        finalize_requested: AtomicBool,
        unknown: AtomicBool,
        disposal_unknown: AtomicBool,
        finalized: AtomicBool,
        unpublished: AtomicBool,
        remove_requested: AtomicBool,
        wake: Arc<OwnerWake>,
        #[cfg(test)]
        panic_monitor: AtomicBool,
    }
    struct State {
        prepared: [Option<Box<parking_lot::RwLock<JoinState>>>; 2],
        controls: Option<(ControlReservation, ControlReservation)>,
        physical: Option<oneshot::Sender<PhysicalResult>>,
        finalizing: bool,
    }
    #[derive(Clone, Copy)]
    pub(super) enum Kind {
        Body,
        Monitor,
    }
    impl Kind {
        fn index(self) -> usize {
            match self {
                Self::Body => 0,
                Self::Monitor => 1,
            }
        }
    }
    #[derive(Clone, Copy)]
    enum Outcome {
        Body(Result<NativeWorkResult, CensusError>),
        Monitor(Result<PhysicalResult, CensusError>),
    }
    enum ActualJoin {
        Body(JoinHandle<NativeWorkResult>),
        Monitor(JoinHandle<PhysicalResult>),
    }
    fn preflight_poll_thread() -> bool {
        // EVERY actual poll path calls this guard-free on its current thread.
        // Only a closed ready(()) future is polled: never the original Join
        // or a scheduled/user future. On Darwin this can register Tokio
        // Context's TLS destructor and grow std's DTORS Vec, so it must run
        // before custody/OWNER/state/census guard acquisition. The subsequent
        // same-thread call contains no await, yield or migration.
        let mut preflight = tokio::task::unconstrained(std::future::ready(()));
        catch_unwind(AssertUnwindSafe(|| {
            std::pin::Pin::new(&mut preflight).poll(&mut Context::from_waker(Waker::noop()))
        }))
        .is_ok()
    }
    fn poll_readiness<T>(
        join: &mut JoinHandle<T>,
        cx: &mut Context<'_>,
    ) -> Poll<Result<T, JoinError>> {
        // One readiness poll of the SAME Join. This stack-only reference
        // wrapper excludes cooperative-budget defer/self-wake; it never
        // polls the scheduled body or loops. The pinned runtime/TLS closure
        // and limited guard exception are recorded with this private source.
        let mut readiness = tokio::task::unconstrained(join);
        std::pin::Pin::new(&mut readiness).poll(cx)
    }
    impl ActualJoin {
        fn poll(&mut self, cx: &mut Context<'_>) -> Poll<(Outcome, Option<JoinError>)> {
            match self {
                Self::Body(join) => match poll_readiness(join, cx) {
                    Poll::Pending => Poll::Pending,
                    Poll::Ready(Ok(result)) => Poll::Ready((Outcome::Body(Ok(result)), None)),
                    Poll::Ready(Err(error)) => {
                        Poll::Ready((Outcome::Body(Err(CensusError::TaskUnknown)), Some(error)))
                    }
                },
                Self::Monitor(join) => match poll_readiness(join, cx) {
                    Poll::Pending => Poll::Pending,
                    Poll::Ready(Ok(result)) => Poll::Ready((Outcome::Monitor(Ok(result)), None)),
                    Poll::Ready(Err(error)) => {
                        Poll::Ready((Outcome::Monitor(Err(CensusError::TaskUnknown)), Some(error)))
                    }
                },
            }
        }
    }
    struct JoinState {
        // The original Join stays in this stable allocation through Pending,
        // poison and observer unwind. It leaves only after actual Ready.
        join: Option<ActualJoin>,
        polled: bool,
        poll_unknown: bool,
        outcome: Option<Outcome>,
        baseline_waker: Option<Waker>,
    }
    impl JoinState {
        fn empty() -> Self {
            Self {
                join: None,
                polled: false,
                poll_unknown: false,
                outcome: None,
                baseline_waker: None,
            }
        }
    }
    struct OwnerWake {
        changed: Arc<tokio::sync::Notify>,
        debt: [AtomicBool; 2],
        requests: [AtomicBool; 3],
        guards: std::sync::atomic::AtomicUsize,
        duty: AtomicU8,
        #[cfg(test)]
        wakes: AtomicU64,
        #[cfg(test)]
        repolls: AtomicU64,
        #[cfg(test)]
        pause: OnceLock<Arc<WakePause>>,
    }
    struct PollWake {
        owner: Arc<OwnerWake>,
        kind: Kind,
    }
    struct WakerRelease {
        // Weak storage is destroyed before the funding companion below.
        slot: Weak<Slot>,
        kind: Kind,
        _control: Option<Funding>,
    }
    impl Drop for WakerRelease {
        fn drop(&mut self) {
            #[cfg(test)]
            let mut witness = None;
            // LeasedShared has ALREADY freed the actual PollWake block.
            // A transient funded strong pin is not a Waker->Slot cycle.
            if let Some(slot) = self.slot.upgrade() {
                let Some(control) = self._control.as_ref() else {
                    return;
                };
                let entry = FundedSlot {
                    slot,
                    _control: control.clone(),
                };
                #[cfg(test)]
                if let Some(pause) = entry.slot.wake.pause.get()
                    && pause.kind.index() == self.kind.index()
                {
                    witness = Some(pause.deallocated.clone());
                }
                entry.slot.waker_destroyed[self.kind.index()].store(true, Ordering::Release);
                request_finalize(&entry);
            }
            // Includes the ArcInner storage retained solely by this Weak.
            // The last funding companion is released AFTER that storage.
            drop(std::mem::replace(&mut self.slot, Weak::new()));
            drop(self._control.take());
            #[cfg(test)]
            if let Some(witness) = witness {
                let _ = witness.send(());
            }
        }
    }
    impl OwnerWake {
        fn owes_poll(&self, kind: Kind) -> bool {
            self.debt[kind.index()].load(Ordering::Acquire)
        }
        fn mark(&self, bits: u8) {
            self.duty.fetch_or(bits, Ordering::AcqRel);
        }
        // A poll-cell wake records only atomic duty while any cell/state guard
        // is live. The final raw unlock hands that duty to this outside path.
        fn flush(&self) {
            if self.guards.load(Ordering::Acquire) != 0 {
                return;
            }
            let duty = self.duty.swap(0, Ordering::AcqRel);
            if duty == 0 {
                return;
            }
            #[cfg(test)]
            if duty & REPOLL != 0 {
                self.repolls.fetch_add(1, Ordering::AcqRel);
            }
            self.changed.notify_waiters();
        }
    }
    impl tobkiri_canon::ownership::LeasedWake for PollWake {
        fn wake_by_ref(&self) {
            self.owner.debt[self.kind.index()].store(true, Ordering::Release);
            #[cfg(test)]
            self.owner.wakes.fetch_add(1, Ordering::AcqRel);
            self.owner.mark(CHANGED);
            self.owner.flush();
            #[cfg(test)]
            if let Some(pause) = self.owner.pause.get() {
                if matches!(self.kind, Kind::Monitor) {
                    let _ = pause.monitor_ready.send(());
                }
                if self.kind.index() == pause.kind.index() {
                    let _ = pause.entered.send(());
                    // Fixture-only: hand the current Tokio worker core and
                    // its nonstealable LIFO slot off BEFORE this existing
                    // synchronous pause. Only the two multi-thread trailer
                    // tests install `pause`; no product task/custody changes.
                    let _ = tokio::task::block_in_place(|| {
                        pause.resume.lock().expect("test wake pause").recv()
                    });
                }
            }
        }
    }

    // Explicit normal completion avoids thread::panicking TLS access while
    // any gate is owned. An unwinding/partial guard latches Unknown using
    // only atomics; the raw guard unlocks before the handoff field drops.
    struct StateUnlock {
        entry: FundedSlot,
    }
    impl Drop for StateUnlock {
        fn drop(&mut self) {
            let slot = &self.entry.slot;
            slot.state_gate.store(AVAILABLE as u8, Ordering::Release);
            if slot.wake.requests[0].swap(false, Ordering::AcqRel) {
                slot.wake.mark(REPOLL);
            }
            slot.wake.guards.fetch_sub(1, Ordering::AcqRel);
            slot.wake.flush();
            if slot.no_task[Kind::Body.index()].load(Ordering::Acquire)
                && !slot.attempted[Kind::Body.index()].load(Ordering::Acquire)
            {
                release_unstarted_cell(&self.entry);
            }
            if slot.finalize_requested.load(Ordering::Acquire) {
                request_finalize(&self.entry);
            }
        }
    }
    struct StateGuard<'a> {
        guard: parking_lot::RwLockWriteGuard<'a, State>,
        _unlock: StateUnlock,
        completed: bool,
    }
    impl StateGuard<'_> {
        fn complete(&mut self) {
            self.completed = true;
        }
    }
    impl Drop for StateGuard<'_> {
        fn drop(&mut self) {
            if !self.completed {
                self._unlock
                    .entry
                    .slot
                    .unknown
                    .store(true, Ordering::Release);
            }
        }
    }
    impl std::ops::Deref for StateGuard<'_> {
        type Target = State;
        fn deref(&self) -> &State {
            &self.guard
        }
    }
    impl std::ops::DerefMut for StateGuard<'_> {
        fn deref_mut(&mut self) -> &mut State {
            &mut self.guard
        }
    }
    fn state_guard<'a>(
        entry: &'a FundedSlot,
        guard: parking_lot::RwLockWriteGuard<'a, State>,
    ) -> StateGuard<'a> {
        entry.slot.wake.guards.fetch_add(1, Ordering::AcqRel);
        StateGuard {
            guard,
            _unlock: StateUnlock {
                entry: entry.clone(),
            },
            completed: false,
        }
    }
    fn try_state(entry: &FundedSlot) -> Option<StateGuard<'_>> {
        let slot = &entry.slot;
        if slot
            .state_gate
            .compare_exchange(
                AVAILABLE as u8,
                BORROWED as u8,
                Ordering::AcqRel,
                Ordering::Acquire,
            )
            .is_err()
        {
            slot.wake.requests[0].store(true, Ordering::Release);
            if slot
                .state_gate
                .compare_exchange(
                    AVAILABLE as u8,
                    BORROWED as u8,
                    Ordering::AcqRel,
                    Ordering::Acquire,
                )
                .is_err()
            {
                return None;
            }
        }
        match slot.state.try_write() {
            Some(guard) => Some(state_guard(entry, guard)),
            None => {
                slot.unknown.store(true, Ordering::Release);
                slot.state_gate.store(RETIRING as u8, Ordering::Release);
                None
            }
        }
    }

    struct CellUnlock<'a> {
        slot: &'a Slot,
        kind: Kind,
    }
    impl Drop for CellUnlock<'_> {
        fn drop(&mut self) {
            let index = self.kind.index();
            self.slot.cell_gates[index].store(AVAILABLE as u8, Ordering::Release);
            if self.slot.wake.requests[index + 1].swap(false, Ordering::AcqRel) {
                self.slot.wake.mark(REPOLL);
            }
            self.slot.wake.guards.fetch_sub(1, Ordering::AcqRel);
            self.slot.wake.flush();
        }
    }
    struct CellGuard<'a> {
        guard: parking_lot::RwLockWriteGuard<'a, JoinState>,
        _unlock: CellUnlock<'a>,
        completed: bool,
    }
    impl CellGuard<'_> {
        fn complete(&mut self) {
            self.completed = true;
        }
    }
    impl Drop for CellGuard<'_> {
        fn drop(&mut self) {
            if !self.completed {
                self._unlock.slot.unknown.store(true, Ordering::Release);
            }
        }
    }
    impl std::ops::Deref for CellGuard<'_> {
        type Target = JoinState;
        fn deref(&self) -> &JoinState {
            &self.guard
        }
    }
    impl std::ops::DerefMut for CellGuard<'_> {
        fn deref_mut(&mut self) -> &mut JoinState {
            &mut self.guard
        }
    }
    fn try_cell(slot: &Slot, kind: Kind) -> Option<CellGuard<'_>> {
        let cell = slot.cells[kind.index()].get()?;
        if slot.cell_gates[kind.index()]
            .compare_exchange(
                AVAILABLE as u8,
                BORROWED as u8,
                Ordering::AcqRel,
                Ordering::Acquire,
            )
            .is_err()
        {
            slot.wake.requests[kind.index() + 1].store(true, Ordering::Release);
            if slot.cell_gates[kind.index()]
                .compare_exchange(
                    AVAILABLE as u8,
                    BORROWED as u8,
                    Ordering::AcqRel,
                    Ordering::Acquire,
                )
                .is_err()
            {
                return None;
            }
        }
        match cell.try_write() {
            Some(guard) => {
                slot.wake.guards.fetch_add(1, Ordering::AcqRel);
                Some(CellGuard {
                    guard,
                    _unlock: CellUnlock { slot, kind },
                    completed: false,
                })
            }
            None => {
                slot.unknown.store(true, Ordering::Release);
                slot.cell_gates[kind.index()].store(RETIRING as u8, Ordering::Release);
                None
            }
        }
    }

    // No normal guard is reconstructed by the retirement path. A normal
    // holder unlocks its raw mutex before publishing Available and checking
    // the same incarnation's retirement request exactly once.
    struct OwnerUnlock {
        cell: &'static OwnerCell,
        id: SlotId,
        entry: Option<FundedSlot>,
        armed: bool,
        #[cfg(test)]
        pause: Option<Arc<OwnerPause>>,
    }
    impl OwnerUnlock {
        fn finish(&mut self) {
            if !self.armed {
                return;
            }
            self.armed = false;
            // Raw guard field has ALREADY unlocked, on every exit/unwind.
            self.cell
                .gate
                .store(encoded(self.id, AVAILABLE), Ordering::Release);
            #[cfg(test)]
            if let Some(pause) = &self.pause {
                pause.wait(1);
            }
            let requested = self
                .cell
                .requested
                .compare_exchange(self.id.0, 0, Ordering::AcqRel, Ordering::Acquire)
                .is_ok();
            if let Some(entry) = &self.entry {
                if entry.slot.remove_requested.load(Ordering::Acquire)
                    && self
                        .cell
                        .gate
                        .compare_exchange(
                            encoded(self.id, AVAILABLE),
                            encoded(self.id, RETIRING),
                            Ordering::AcqRel,
                            Ordering::Acquire,
                        )
                        .is_ok()
                {
                    retire(self.cell, entry);
                }
                #[cfg(test)]
                if let Some(pause) = &self.pause {
                    pause.wait(2);
                }
                // A replacement borrower after Available either wins that
                // CAS and inherits the same sticky request, or this dedicated
                // retire winner excludes every new borrower.
                if requested {
                    entry.slot.wake.mark(REPOLL);
                    entry.slot.wake.flush();
                }
            }
        }
    }
    impl Drop for OwnerUnlock {
        fn drop(&mut self) {
            self.finish();
        }
    }
    struct OwnerGuard {
        guard: parking_lot::RwLockWriteGuard<'static, Option<FundedSlot>>,
        _unlock: OwnerUnlock,
        poisoned: bool,
        completed: bool,
    }
    impl Drop for OwnerGuard {
        fn drop(&mut self) {
            if !self.completed
                && let Some(entry) = &self._unlock.entry
            {
                entry.slot.unknown.store(true, Ordering::Release);
            }
        }
    }
    impl std::ops::Deref for OwnerGuard {
        type Target = Option<FundedSlot>;
        fn deref(&self) -> &Self::Target {
            &self.guard
        }
    }
    impl std::ops::DerefMut for OwnerGuard {
        fn deref_mut(&mut self) -> &mut Self::Target {
            &mut self.guard
        }
    }
    fn owner_guard(
        cell: &'static OwnerCell,
        id: SlotId,
        guard: parking_lot::RwLockWriteGuard<'static, Option<FundedSlot>>,
        poisoned: bool,
    ) -> OwnerGuard {
        let entry = guard.clone();
        if poisoned && let Some(entry) = &entry {
            entry.slot.unknown.store(true, Ordering::Release);
        }
        OwnerGuard {
            guard,
            _unlock: OwnerUnlock {
                cell,
                id,
                entry,
                armed: true,
                #[cfg(test)]
                pause: None,
            },
            poisoned,
            completed: false,
        }
    }
    fn claimed_owner(cell: &'static OwnerCell, id: SlotId) -> Option<OwnerGuard> {
        // Gate ownership makes this raw lock noncontended in the actual
        // protocol. Unexpected failure quarantines the incarnation: the real
        // funded value stays inside, and the cell is never reported Empty.
        match cell.value.try_write() {
            Some(guard) => Some(owner_guard(cell, id, guard, false)),
            None => {
                cell.gate.store(encoded(id, RETIRING), Ordering::Release);
                None
            }
        }
    }
    fn owner_cell(id: SlotId) -> Option<&'static OwnerCell> {
        OWNER
            .iter()
            .find(|cell| incarnation(cell.gate.load(Ordering::Acquire)) == id)
    }
    fn try_owner(id: SlotId) -> Option<OwnerGuard> {
        let cell = owner_cell(id)?;
        let available = encoded(id, AVAILABLE);
        let borrowed = encoded(id, BORROWED);
        match cell
            .gate
            .compare_exchange(available, borrowed, Ordering::AcqRel, Ordering::Acquire)
        {
            Ok(_) => return claimed_owner(cell, id),
            Err(value) if incarnation(value) != id || value & PHASE_MASK == EMPTY => return None,
            Err(_) => {}
        }
        // Incarnations are globally monotonic: a stale old requester cannot
        // overwrite or consume a newer record's request in this reused cell.
        cell.requested.fetch_max(id.0, Ordering::AcqRel);
        match cell
            .gate
            .compare_exchange(available, borrowed, Ordering::AcqRel, Ordering::Acquire)
        {
            Ok(_) => claimed_owner(cell, id),
            Err(_) => None,
        }
    }
    enum Lookup {
        Busy,
        Missing,
        Found(FundedSlot),
    }
    fn lookup(id: SlotId) -> Lookup {
        if let Some(mut owner) = try_owner(id) {
            let result = match owner.as_ref().filter(|entry| entry.slot.id == id).cloned() {
                Some(entry) => Lookup::Found(entry),
                None => Lookup::Missing,
            };
            owner.completed = true;
            return result;
        }
        match owner_cell(id) {
            Some(cell) if cell.gate.load(Ordering::Acquire) & PHASE_MASK != EMPTY => Lookup::Busy,
            _ => Lookup::Missing,
        }
    }
    fn insert_owner(entry: &FundedSlot) -> Result<(), CensusError> {
        for cell in &OWNER {
            let previous = cell.gate.load(Ordering::Acquire);
            if previous & PHASE_MASK != EMPTY {
                continue;
            }
            if cell
                .gate
                .compare_exchange(
                    previous,
                    encoded(entry.slot.id, BORROWED),
                    Ordering::AcqRel,
                    Ordering::Acquire,
                )
                .is_err()
            {
                continue;
            }
            let Some(mut owner) = claimed_owner(cell, entry.slot.id) else {
                return Err(CensusError::StateUnavailable);
            };
            if owner.poisoned || owner.is_some() {
                entry.slot.unknown.store(true, Ordering::Release);
                owner._unlock.armed = false;
                cell.gate
                    .store(encoded(entry.slot.id, RETIRING), Ordering::Release);
                return Err(CensusError::StateUnavailable);
            }
            *owner = Some(entry.clone());
            // Normal release now has this same funded incarnation pinned;
            // no Arc/control final destruction can occur under its raw guard.
            owner._unlock.entry = Some(entry.clone());
            owner.completed = true;
            return Ok(());
        }
        Err(CensusError::Capacity)
    }
    fn retire(cell: &'static OwnerCell, pin: &FundedSlot) {
        // Dedicated exact-incarnation winner only. This function never makes
        // a normal OwnerGuard and cannot recursively trigger its Drop path.
        let removed = match cell.value.try_write() {
            Some(mut raw) => {
                if raw
                    .as_ref()
                    .is_some_and(|entry| entry.slot.id == pin.slot.id && removable(entry))
                {
                    raw.take()
                } else {
                    None
                }
            }
            None => {
                pin.slot.unknown.store(true, Ordering::Release);
                return;
            }
        };
        // The raw guard is gone before either the gate is reusable or any
        // final funded Arc/Notify/control owner is destroyed.
        cell.gate.store(
            encoded(
                pin.slot.id,
                if removed.is_some() { EMPTY } else { AVAILABLE },
            ),
            Ordering::Release,
        );
        let requested = cell
            .requested
            .compare_exchange(pin.slot.id.0, 0, Ordering::AcqRel, Ordering::Acquire)
            .is_ok();
        if requested {
            pin.slot.wake.mark(REPOLL);
            pin.slot.wake.flush();
        }
        drop(removed);
    }

    pub(super) fn metadata_bytes() -> Result<u64, CensusError> {
        // All private custody guards use the source-closed allocation-free
        // exclusive RwLock path. Only existing Notify needs a Darwin PAL box.
        let pal = darwin_mutex_requested_bound()?;
        let cells = size_of::<parking_lot::RwLock<JoinState>>()
            .checked_mul(2)
            .ok_or(CensusError::Capacity)?;
        let wakers = LeasedShared::<PollWake, WakerRelease>::storage_bytes()
            .checked_mul(2)
            .ok_or(CensusError::Capacity)?;
        arc_requested_bound::<Slot>()?
            .checked_add(arc_requested_bound::<OwnerWake>()?)
            .and_then(|bytes| bytes.checked_add(u64::try_from(wakers).ok()?))
            .and_then(|bytes| bytes.checked_add(arc_requested_bound::<tokio::sync::Notify>().ok()?))
            .and_then(|bytes| bytes.checked_add(u64::try_from(cells).ok()?))
            .and_then(|bytes| bytes.checked_add(pal))
            .ok_or(CensusError::Capacity)
    }
    pub(super) struct UnpublishedAdmission {
        entry: FundedSlot,
    }
    impl UnpublishedAdmission {
        pub(super) fn id(&self) -> SlotId {
            self.entry.slot.id
        }
    }
    impl Drop for UnpublishedAdmission {
        fn drop(&mut self) {
            if !self.entry.slot.attempted[Kind::Monitor.index()].load(Ordering::Acquire) {
                self.entry.slot.unpublished.store(true, Ordering::Release);
                self.entry
                    .slot
                    .remove_requested
                    .store(true, Ordering::Release);
                remove(&self.entry);
            }
        }
    }
    pub(super) fn reserve(
        control: Funding,
        task: ControlReservation,
        monitor: ControlReservation,
        physical: oneshot::Sender<PhysicalResult>,
        changed: Arc<tokio::sync::Notify>,
    ) -> Result<(UnpublishedAdmission, Publication), CensusError> {
        let body = Box::new(parking_lot::RwLock::new(JoinState::empty()));
        let monitor_cell = Box::new(parking_lot::RwLock::new(JoinState::empty()));
        let state = parking_lot::RwLock::new(State {
            prepared: [Some(body), Some(monitor_cell)],
            controls: Some((task, monitor)),
            physical: Some(physical),
            finalizing: false,
        });
        let id = SlotId(
            NEXT_ID
                .fetch_update(Ordering::AcqRel, Ordering::Acquire, |id| {
                    id.checked_add(1).filter(|next| *next <= MAX_INCARNATION)
                })
                .map_err(|_| CensusError::Capacity)?,
        );
        let wake = Arc::new(OwnerWake {
            changed,
            debt: [AtomicBool::new(false), AtomicBool::new(false)],
            requests: [
                AtomicBool::new(false),
                AtomicBool::new(false),
                AtomicBool::new(false),
            ],
            guards: std::sync::atomic::AtomicUsize::new(0),
            duty: AtomicU8::new(0),
            #[cfg(test)]
            wakes: AtomicU64::new(0),
            #[cfg(test)]
            repolls: AtomicU64::new(0),
            #[cfg(test)]
            pause: OnceLock::new(),
        });
        let entry = FundedSlot {
            slot: Arc::new(Slot {
                id,
                state,
                state_gate: AtomicU8::new(AVAILABLE as u8),
                cells: [OnceLock::new(), OnceLock::new()],
                cell_gates: [
                    AtomicU8::new(AVAILABLE as u8),
                    AtomicU8::new(AVAILABLE as u8),
                ],
                attempted: [AtomicBool::new(false), AtomicBool::new(false)],
                no_task: [AtomicBool::new(false), AtomicBool::new(false)],
                destroyed: [AtomicBool::new(false), AtomicBool::new(false)],
                waker_destroyed: [AtomicBool::new(false), AtomicBool::new(false)],
                monitor_result: OnceLock::new(),
                finalize_requested: AtomicBool::new(false),
                unknown: AtomicBool::new(false),
                disposal_unknown: AtomicBool::new(false),
                finalized: AtomicBool::new(false),
                unpublished: AtomicBool::new(false),
                remove_requested: AtomicBool::new(false),
                wake,
                #[cfg(test)]
                panic_monitor: AtomicBool::new(false),
            }),
            _control: control,
        };
        // Both blocks use the authentic LeasedShared plan. Their immutable
        // payload has no strong Slot edge; each plan holds a Weak plus the
        // same funding token through block deallocation and Weak release.
        let make_waker = |kind| {
            LeasedShared::try_new_precharged(
                PollWake {
                    owner: entry.slot.wake.clone(),
                    kind,
                },
                WakerRelease {
                    slot: Arc::downgrade(&entry.slot),
                    kind,
                    _control: Some(entry._control.clone()),
                },
            )
            .map(LeasedShared::into_waker)
            .map_err(|_| CensusError::Capacity)
        };
        let baselines = [make_waker(Kind::Body)?, make_waker(Kind::Monitor)?];
        let monitor_prepared = {
            let Some(mut state) = try_state(&entry) else {
                return Err(CensusError::StateUnavailable);
            };
            for (cell, waker) in state.prepared.iter_mut().zip(baselines) {
                let Some(cell) = cell.as_mut() else {
                    return Err(CensusError::StateUnavailable);
                };
                cell.get_mut().baseline_waker = Some(waker);
            }
            // This entry is still private. Extract the monitor's prewarmed
            // cell before maintenance can borrow its process-owner slot.
            let prepared = state.prepared[Kind::Monitor.index()]
                .take()
                .ok_or(CensusError::LaunchUnknown)?;
            state.complete();
            prepared
        };
        let publication = Publication {
            entry: entry.clone(),
            kind: Kind::Monitor,
            prepared: Some(monitor_prepared),
            committed: false,
            installed: false,
        };
        let admission = UnpublishedAdmission { entry };
        insert_owner(&admission.entry)?;
        #[cfg(test)]
        pause_reserved_admission(admission.id());
        Ok((admission, publication))
    }

    // A one-shot publisher-local seam pauses outside every custody guard.
    // Tests borrow the real OWNER cell, not a synthetic contention flag.
    #[cfg(test)]
    std::thread_local! {
        static ADMISSION_PAUSE: std::cell::RefCell<Option<AdmissionPause>> = const {
            std::cell::RefCell::new(None)
        };
    }
    #[cfg(test)]
    struct AdmissionPause {
        reserved: std::sync::mpsc::SyncSender<SlotId>,
        resume: std::sync::mpsc::Receiver<()>,
    }
    #[cfg(test)]
    pub(super) fn set_admission_pause(
        reserved: std::sync::mpsc::SyncSender<SlotId>,
        resume: std::sync::mpsc::Receiver<()>,
    ) {
        ADMISSION_PAUSE.with(|pause| {
            assert!(
                pause
                    .borrow_mut()
                    .replace(AdmissionPause { reserved, resume })
                    .is_none()
            );
        });
    }
    #[cfg(test)]
    fn pause_reserved_admission(id: SlotId) {
        let pause = ADMISSION_PAUSE.with(|pause| pause.borrow_mut().take());
        if let Some(pause) = pause {
            pause
                .reserved
                .send(id)
                .expect("reserved admission observer");
            pause.resume.recv().expect("resume reserved admission");
        }
    }

    pub(super) struct Publication {
        entry: FundedSlot,
        kind: Kind,
        prepared: Option<Box<parking_lot::RwLock<JoinState>>>,
        committed: bool,
        installed: bool,
    }
    pub(super) enum Prepared {
        Busy,
        Ready(Publication),
    }
    pub(super) fn prepare(id: SlotId, kind: Kind) -> Result<Prepared, CensusError> {
        let entry = match lookup(id) {
            Lookup::Busy => return Ok(Prepared::Busy),
            Lookup::Missing => return Err(CensusError::ReceiptLost),
            Lookup::Found(entry) => entry,
        };
        let prepared = {
            let Some(mut state) = try_state(&entry) else {
                return Ok(Prepared::Busy);
            };
            if entry.slot.unknown.load(Ordering::Acquire) {
                return Err(CensusError::StateUnavailable);
            }
            let prepared = state.prepared[kind.index()]
                .take()
                .ok_or(CensusError::LaunchUnknown)?;
            state.complete();
            prepared
        };
        Ok(Prepared::Ready(Publication {
            entry,
            kind,
            prepared: Some(prepared),
            committed: false,
            installed: false,
        }))
    }
    impl Publication {
        // Called under census start-linearization guard: atomic metadata only.
        pub(super) fn commit(&mut self) -> Result<(), CensusError> {
            let index = self.kind.index();
            if self.entry.slot.no_task[index].load(Ordering::Acquire)
                || self.entry.slot.cells[index].get().is_some()
            {
                return Err(CensusError::LaunchUnknown);
            }
            self.entry.slot.attempted[index]
                .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
                .map_err(|_| CensusError::LaunchUnknown)?;
            self.committed = true;
            Ok(())
        }
        fn spawn<T>(
            mut self,
            spawn: impl FnOnce() -> JoinHandle<T>,
            wrap: impl FnOnce(JoinHandle<T>) -> ActualJoin,
        ) -> Result<(), CensusError> {
            if !self.committed {
                return Err(CensusError::LaunchUnknown);
            }
            let Some(mut prepared) = self.prepared.take() else {
                return Err(CensusError::LaunchUnknown);
            };
            // No OWNER/state/cell/census guard is held by the call sites.
            let join = match catch_unwind(AssertUnwindSafe(spawn)) {
                Ok(join) => join,
                Err(_) => {
                    self.entry.slot.unknown.store(true, Ordering::Release);
                    return Err(CensusError::LaunchUnknown);
                }
            };
            // No await/callback/fallible wrap/early return after original Join
            // creation. This uniquely owned prewarmed box receives that Join.
            let state = prepared.get_mut();
            state.join = Some(wrap(join));
            // Private API: only the single attempted-CAS winner initializes;
            // observers ONLY get(). Thus this initializer always runs, once.
            // Supported std/runtime closure remains separately source-bound.
            self.entry.slot.cells[self.kind.index()].get_or_init(move || prepared);
            self.installed = true;
            Ok(())
        }
        pub(super) fn spawn_monitor(
            self,
            spawn: impl FnOnce() -> JoinHandle<PhysicalResult>,
        ) -> Result<(), CensusError> {
            self.spawn(spawn, ActualJoin::Monitor)
        }
        pub(super) fn spawn_body(
            self,
            spawn: impl FnOnce() -> JoinHandle<NativeWorkResult>,
        ) -> Result<(), CensusError> {
            self.spawn(spawn, ActualJoin::Body)
        }
    }
    impl Drop for Publication {
        fn drop(&mut self) {
            if !self.committed {
                self.entry.slot.no_task[self.kind.index()].store(true, Ordering::Release);
            } else if !self.installed {
                // Init-pending is UNKNOWN, never NoTask/destruction/refund.
                self.entry.slot.unknown.store(true, Ordering::Release);
            }
            self.entry.slot.wake.mark(CHANGED);
            self.entry.slot.wake.flush();
        }
    }

    fn no_body_entry(entry: &FundedSlot) {
        if !entry.slot.attempted[Kind::Body.index()].load(Ordering::Acquire) {
            entry.slot.no_task[Kind::Body.index()].store(true, Ordering::Release);
            release_unstarted_cell(entry);
        }
    }
    fn release_unstarted_cell(entry: &FundedSlot) {
        if !entry.slot.no_task[Kind::Body.index()].load(Ordering::Acquire)
            || entry.slot.attempted[Kind::Body.index()].load(Ordering::Acquire)
        {
            return;
        }
        if entry
            .slot
            .state_gate
            .compare_exchange(
                AVAILABLE as u8,
                RETIRING as u8,
                Ordering::AcqRel,
                Ordering::Acquire,
            )
            .is_err()
        {
            return;
        }
        let prepared = match entry.slot.state.try_write() {
            Some(mut raw) => {
                let mut prepared = raw.prepared[Kind::Body.index()].take();
                if prepared
                    .as_mut()
                    .is_some_and(|cell| cell.get_mut().join.is_some())
                {
                    // An unexpected original Join is retained, never dropped
                    // merely because the NoTask proof was inconsistent.
                    raw.prepared[Kind::Body.index()] = prepared.take();
                    entry.slot.unknown.store(true, Ordering::Release);
                    entry.slot.disposal_unknown.store(true, Ordering::Release);
                }
                prepared
            }
            None => {
                entry.slot.unknown.store(true, Ordering::Release);
                return;
            }
        };
        entry
            .slot
            .state_gate
            .store(AVAILABLE as u8, Ordering::Release);
        if entry.slot.wake.requests[0].swap(false, Ordering::AcqRel) {
            entry.slot.wake.mark(REPOLL);
            entry.slot.wake.flush();
        }
        // Fresh/no-task cell has no original Join. Its baseline Waker and
        // box deallocate only AFTER the dedicated raw State guard unlocks.
        drop(prepared);
        if entry.slot.finalize_requested.load(Ordering::Acquire) {
            request_finalize(entry);
        }
    }
    pub(super) fn no_body(id: SlotId) {
        if let Lookup::Found(entry) = lookup(id) {
            no_body_entry(&entry);
        }
    }
    fn poll_join(entry: &FundedSlot, kind: Kind, monitor_poll: bool) -> Option<Outcome> {
        if !preflight_poll_thread() {
            return None;
        }
        let wake_pin = entry.slot.wake.clone();
        let disposal = {
            let mut cell = try_cell(&entry.slot, kind)?;
            if let Some(outcome) = cell.outcome {
                cell.complete();
                return Some(outcome);
            }
            if cell.join.is_none() || cell.poll_unknown {
                cell.complete();
                return None;
            }
            #[cfg(test)]
            if monitor_poll
                && matches!(kind, Kind::Body)
                && entry.slot.panic_monitor.swap(false, Ordering::AcqRel)
            {
                panic!("native monitor test panic during borrowed Join poll");
            }
            #[cfg(not(test))]
            let _ = monitor_poll;
            if cell.polled && !wake_pin.owes_poll(kind) {
                cell.complete();
                return None;
            }
            // The baseline token predates this guard. Every private adapter
            // clone/drop and replaced Tokio trailer Waker is nonfinal while
            // that token stays in the cell OR the outer disposal tuple.
            let Some(waker) = cell.baseline_waker.as_ref().cloned() else {
                entry.slot.unknown.store(true, Ordering::Release);
                cell.complete();
                return None;
            };
            let mut cx = Context::from_waker(&waker);
            cell.polled = true;
            wake_pin.debt[kind.index()].swap(false, Ordering::AcqRel);
            let ready = catch_unwind(AssertUnwindSafe(|| {
                cell.join.as_mut().map(|join| join.poll(&mut cx))
            }));
            let result = match ready {
                Ok(Some(Poll::Ready((outcome, error)))) => {
                    cell.outcome = Some(outcome);
                    if let Outcome::Monitor(result) = outcome {
                        let physical = match result {
                            Ok(result) => result,
                            Err(error) => Err(error),
                        };
                        // Only the same actual Ready winner initializes this
                        // inline closed Copy result. Observers use get only.
                        let _ = entry.slot.monitor_result.set(physical);
                    }
                    Ok(Some((
                        cell.join.take(),
                        error,
                        outcome,
                        cell.baseline_waker.take(),
                    )))
                }
                Ok(_) => Ok(None),
                Err(payload) => {
                    entry.slot.unknown.store(true, Ordering::Release);
                    cell.poll_unknown = true;
                    Err(payload)
                }
            };
            cell.complete();
            result
        };
        let disposal = match disposal {
            Ok(disposal) => disposal,
            Err(payload) => {
                let _ = catch_unwind(AssertUnwindSafe(|| drop(payload)));
                wake_pin.mark(CHANGED);
                wake_pin.flush();
                return None;
            }
        };
        let (join, error, outcome, baseline) = disposal?;
        let destroyed = catch_unwind(AssertUnwindSafe(|| {
            drop(join);
            drop(error);
        }))
        .is_ok();
        entry.slot.destroyed[kind.index()].store(destroyed, Ordering::Release);
        if !destroyed {
            entry.slot.disposal_unknown.store(true, Ordering::Release);
        }
        // No further clone or actual poll is possible after the cached Ready.
        // Tokio's trailer may still own the sole other token. Its final block
        // deallocation, not this Join Drop, produces the second receipt.
        drop(baseline);
        request_finalize(entry);
        wake_pin.mark(CHANGED);
        wake_pin.flush();
        Some(outcome)
    }
    pub(super) async fn wait_body(
        id: SlotId,
        changed: &Arc<tokio::sync::Notify>,
    ) -> Result<NativeWorkResult, CensusError> {
        loop {
            if !preflight_poll_thread() {
                return Err(CensusError::TaskUnknown);
            }
            let notified = changed.clone().notified_owned();
            tokio::pin!(notified);
            notified.as_mut().enable();
            match lookup(id) {
                Lookup::Found(entry) => {
                    if let Some(Outcome::Body(result)) = poll_join(&entry, Kind::Body, true) {
                        return result;
                    }
                }
                Lookup::Missing => return Err(CensusError::ReceiptLost),
                Lookup::Busy => {}
            }
            // Same existing signal, enabled before OWNER lookup; no Slot or
            // owner Arc is retained in the monitor future across this await.
            notified.await;
        }
    }
    fn completion_resources_gone(slot: &Slot) -> bool {
        slot.destroyed[Kind::Monitor.index()].load(Ordering::Acquire)
            && (slot.destroyed[Kind::Body.index()].load(Ordering::Acquire)
                || slot.no_task[Kind::Body.index()].load(Ordering::Acquire))
            && slot
                .waker_destroyed
                .iter()
                .all(|destroyed| destroyed.load(Ordering::Acquire))
            && slot.monitor_result.get().is_some()
            && !slot.disposal_unknown.load(Ordering::Acquire)
    }
    fn removable(entry: &FundedSlot) -> bool {
        let slot = &entry.slot;
        (slot.unpublished.load(Ordering::Acquire)
            && !slot.attempted[Kind::Monitor.index()].load(Ordering::Acquire))
            || (slot.finalized.load(Ordering::Acquire) && completion_resources_gone(slot))
    }
    fn request_finalize(entry: &FundedSlot) {
        if entry.slot.finalized.load(Ordering::Acquire) {
            remove(entry);
            return;
        }
        // This request is emitted ONLY after every prerequisite is monotonic
        // Ready/deallocated. A dedicated winner never chases partial events.
        if !completion_resources_gone(&entry.slot) {
            return;
        }
        entry.slot.finalize_requested.store(true, Ordering::Release);
        if entry
            .slot
            .state_gate
            .compare_exchange(
                AVAILABLE as u8,
                RETIRING as u8,
                Ordering::AcqRel,
                Ordering::Acquire,
            )
            .is_err()
        {
            return;
        }
        let finished = match entry.slot.state.try_write() {
            Some(mut raw) => {
                if raw.finalizing {
                    None
                } else {
                    raw.finalizing = true;
                    let result = entry
                        .slot
                        .monitor_result
                        .get()
                        .copied()
                        .unwrap_or(Err(CensusError::TaskUnknown));
                    let result = if entry.slot.unknown.load(Ordering::Acquire) {
                        Err(CensusError::TaskUnknown)
                    } else {
                        result
                    };
                    Some((raw.controls.take(), raw.physical.take(), result))
                }
            }
            None => {
                // Gate-exclusive raw failure retains original funded State
                // and Unknown custody; never pretend it was finalized/freed.
                entry.slot.unknown.store(true, Ordering::Release);
                return;
            }
        };
        // Dedicated finalizer has no normal StateGuard/recursive Drop path.
        // Its raw guard is gone before either the gate or callbacks release.
        entry
            .slot
            .state_gate
            .store(AVAILABLE as u8, Ordering::Release);
        if entry.slot.wake.requests[0].swap(false, Ordering::AcqRel) {
            entry.slot.wake.mark(REPOLL);
            entry.slot.wake.flush();
        }
        if let Some((controls, physical, result)) = finished {
            drop(controls);
            if let Some(physical) = physical {
                let _ = physical.send(result);
            }
            entry.slot.finalized.store(true, Ordering::Release);
            entry.slot.wake.mark(CHANGED);
            entry.slot.wake.flush();
            remove(entry);
        }
    }
    fn remove(entry: &FundedSlot) {
        entry.slot.remove_requested.store(true, Ordering::Release);
        let Some(cell) = owner_cell(entry.slot.id) else {
            return;
        };
        if cell
            .gate
            .compare_exchange(
                encoded(entry.slot.id, AVAILABLE),
                encoded(entry.slot.id, RETIRING),
                Ordering::AcqRel,
                Ordering::Acquire,
            )
            .is_ok()
        {
            retire(cell, entry);
        }
        // Same Borrowed delegates to its actual holder's unlock; Retiring
        // means a dedicated winner already owns this request; Empty or a
        // different incarnation means the old funded slot was detached.
    }
    pub(super) fn progress(id: SlotId) {
        if !preflight_poll_thread() {
            return;
        }
        let Lookup::Found(entry) = lookup(id) else {
            return;
        };
        if entry.slot.unpublished.load(Ordering::Acquire) {
            remove(&entry);
            return;
        }
        let monitor = poll_join(&entry, Kind::Monitor, false);
        if monitor.is_some() {
            no_body_entry(&entry);
            let _ = poll_join(&entry, Kind::Body, false);
        }
        if entry.slot.finalized.load(Ordering::Acquire) {
            remove(&entry);
        }
    }
    pub(super) fn maintain(except: Option<SlotId>) {
        if !preflight_poll_thread() {
            return;
        }
        for cell in &OWNER {
            let gate = cell.gate.load(Ordering::Acquire);
            let id = incarnation(gate);
            if gate & PHASE_MASK != EMPTY && Some(id) != except {
                progress(id);
            }
        }
    }

    #[cfg(test)]
    struct WakePause {
        kind: Kind,
        entered: std::sync::mpsc::SyncSender<()>,
        resume: Mutex<std::sync::mpsc::Receiver<()>>,
        monitor_ready: std::sync::mpsc::SyncSender<()>,
        deallocated: std::sync::mpsc::SyncSender<()>,
    }
    #[cfg(test)]
    pub(super) struct WakePauseControl {
        pub(super) entered: std::sync::mpsc::Receiver<()>,
        pub(super) resume: std::sync::mpsc::SyncSender<()>,
        pub(super) monitor_ready: std::sync::mpsc::Receiver<()>,
        pub(super) deallocated: std::sync::mpsc::Receiver<()>,
    }
    #[cfg(test)]
    pub(super) fn pause_actual_trailer_wake(id: SlotId, kind: Kind) -> Option<WakePauseControl> {
        let Lookup::Found(entry) = lookup(id) else {
            return None;
        };
        let (entered_tx, entered) = std::sync::mpsc::sync_channel(1);
        let (resume, resume_rx) = std::sync::mpsc::sync_channel(1);
        let (monitor_tx, monitor_ready) = std::sync::mpsc::sync_channel(1);
        let (deallocated_tx, deallocated) = std::sync::mpsc::sync_channel(1);
        let pause = Arc::new(WakePause {
            kind,
            entered: entered_tx,
            resume: Mutex::new(resume_rx),
            monitor_ready: monitor_tx,
            deallocated: deallocated_tx,
        });
        entry.slot.wake.pause.set(pause).ok()?;
        Some(WakePauseControl {
            entered,
            resume,
            monitor_ready,
            deallocated,
        })
    }
    #[cfg(test)]
    pub(super) fn prime_original_body_pending(id: SlotId) -> bool {
        if !preflight_poll_thread() {
            return false;
        }
        let Lookup::Found(entry) = lookup(id) else {
            return false;
        };
        poll_join(&entry, Kind::Body, false).is_none()
    }
    #[cfg(test)]
    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    pub(super) enum OriginalMonitorProbe {
        OwnerBusy,
        OwnerMissing,
        MonitorUnpublished,
        MonitorCellBusy,
        MonitorPresent {
            finished: bool,
        },
        MonitorConsumed {
            outcome_cached: bool,
            monitor_result_cached: bool,
            destroyed: bool,
        },
        UnexpectedBody,
    }
    #[cfg(test)]
    pub(super) fn original_monitor_probe(id: SlotId) -> OriginalMonitorProbe {
        let entry = match lookup(id) {
            Lookup::Found(entry) => entry,
            Lookup::Busy => return OriginalMonitorProbe::OwnerBusy,
            Lookup::Missing => return OriginalMonitorProbe::OwnerMissing,
        };
        if entry.slot.cells[Kind::Monitor.index()].get().is_none() {
            return OriginalMonitorProbe::MonitorUnpublished;
        }
        let Some(mut cell) = try_cell(&entry.slot, Kind::Monitor) else {
            return OriginalMonitorProbe::MonitorCellBusy;
        };
        let probe = match cell.join.as_ref() {
            Some(ActualJoin::Monitor(join)) => OriginalMonitorProbe::MonitorPresent {
                finished: join.is_finished(),
            },
            Some(ActualJoin::Body(_)) => OriginalMonitorProbe::UnexpectedBody,
            None => OriginalMonitorProbe::MonitorConsumed {
                outcome_cached: cell.outcome.is_some(),
                monitor_result_cached: entry.slot.monitor_result.get().is_some(),
                destroyed: entry.slot.destroyed[Kind::Monitor.index()].load(Ordering::Acquire),
            },
        };
        cell.complete();
        probe
    }
    #[cfg(test)]
    pub(super) struct OwnerPause {
        at: u8,
        entered: std::sync::mpsc::SyncSender<()>,
        resume: Mutex<std::sync::mpsc::Receiver<()>>,
    }
    #[cfg(test)]
    impl OwnerPause {
        fn wait(&self, stage: u8) {
            if self.at == stage {
                let _ = self.entered.send(());
                let _ = self.resume.lock().expect("test owner pause").recv();
            }
        }
    }
    #[cfg(test)]
    pub(super) fn owner_pause(
        at: u8,
    ) -> (
        Arc<OwnerPause>,
        std::sync::mpsc::Receiver<()>,
        std::sync::mpsc::SyncSender<()>,
    ) {
        let (entered_tx, entered) = std::sync::mpsc::sync_channel(1);
        let (resume, resume_rx) = std::sync::mpsc::sync_channel(1);
        (
            Arc::new(OwnerPause {
                at,
                entered: entered_tx,
                resume: Mutex::new(resume_rx),
            }),
            entered,
            resume,
        )
    }
    #[cfg(test)]
    pub(super) fn with_actual_owner_guard<R>(
        id: SlotId,
        pause: Option<Arc<OwnerPause>>,
        f: impl FnOnce() -> R,
    ) -> Option<R> {
        let mut owner = try_owner(id)?;
        if owner.as_ref().is_none_or(|entry| entry.slot.id != id) {
            owner.completed = true;
            return None;
        }
        owner._unlock.pause = pause;
        let result = f();
        owner.completed = true;
        Some(result)
    }
    #[cfg(test)]
    pub(super) fn owner_attached(id: SlotId) -> bool {
        owner_cell(id).is_some_and(|cell| {
            let gate = cell.gate.load(Ordering::Acquire);
            incarnation(gate) == id && gate & PHASE_MASK != EMPTY
        })
    }

    // Compatibility with all20 frozen tests: this lease owns the ACTUAL
    // cell borrow gate after its inspection guard unlocks. It never owns or
    // returns the original Join, and no alternate test-only busy flag exists.
    #[cfg(test)]
    pub(super) struct PollLease {
        entry: FundedSlot,
        kind: Kind,
    }
    #[cfg(test)]
    impl Drop for PollLease {
        fn drop(&mut self) {
            self.entry.slot.cell_gates[self.kind.index()].store(AVAILABLE as u8, Ordering::Release);
            let requested =
                self.entry.slot.wake.requests[self.kind.index() + 1].swap(false, Ordering::AcqRel);
            if requested || self.entry.slot.wake.owes_poll(self.kind) {
                self.entry.slot.wake.mark(REPOLL);
                self.entry.slot.wake.flush();
            }
        }
    }
    #[cfg(test)]
    pub(super) fn borrow_join(id: SlotId, kind: Kind) -> Option<PollLease> {
        let Lookup::Found(entry) = lookup(id) else {
            return None;
        };
        let cell = entry.slot.cells[kind.index()].get()?;
        entry.slot.cell_gates[kind.index()]
            .compare_exchange(
                AVAILABLE as u8,
                BORROWED as u8,
                Ordering::AcqRel,
                Ordering::Acquire,
            )
            .ok()?;
        let live = match cell.try_write() {
            Some(raw) => raw.join.is_some() && raw.outcome.is_none(),
            None => {
                entry.slot.unknown.store(true, Ordering::Release);
                entry.slot.cell_gates[kind.index()].store(RETIRING as u8, Ordering::Release);
                return None;
            }
        };
        // The raw lock is already gone, while the same gate remains Borrowed.
        let lease = PollLease { entry, kind };
        if live {
            Some(lease)
        } else {
            drop(lease);
            None
        }
    }
    #[cfg(test)]
    pub(super) fn panic_monitor(id: SlotId) {
        if let Lookup::Found(entry) = lookup(id) {
            entry.slot.panic_monitor.store(true, Ordering::Release);
            entry.slot.wake.mark(CHANGED);
            entry.slot.wake.flush();
        }
    }
    #[cfg(test)]
    pub(super) fn try_observe_state(id: SlotId) -> bool {
        match lookup(id) {
            Lookup::Found(entry) => match try_state(&entry) {
                Some(mut guard) => {
                    guard.complete();
                    true
                }
                None => false,
            },
            _ => false,
        }
    }
    #[cfg(test)]
    pub(super) fn with_state_guard<R>(id: SlotId, f: impl FnOnce() -> R) -> Option<R> {
        let Lookup::Found(entry) = lookup(id) else {
            return None;
        };
        let mut guard = try_state(&entry)?;
        let result = f();
        guard.complete();
        Some(result)
    }
    #[cfg(test)]
    pub(super) fn request_state(id: SlotId) {
        if let Lookup::Found(entry) = lookup(id) {
            entry.slot.wake.requests[0].store(true, Ordering::Release);
        }
    }
    #[cfg(test)]
    pub(super) fn state_requested(id: SlotId) -> bool {
        matches!(lookup(id), Lookup::Found(entry) if entry.slot.wake.requests[0].load(Ordering::Acquire))
    }
    #[cfg(test)]
    pub(super) fn wake_count(id: SlotId) -> u64 {
        match lookup(id) {
            Lookup::Found(entry) => entry.slot.wake.wakes.load(Ordering::Acquire),
            _ => 0,
        }
    }
    #[cfg(test)]
    pub(super) fn repoll_count(id: SlotId) -> u64 {
        match lookup(id) {
            Lookup::Found(entry) => entry.slot.wake.repolls.load(Ordering::Acquire),
            _ => 0,
        }
    }
}

impl GenerationChildCensus {
    /// Bounded maintenance at a trusted outside-Store/native-lock boundary.
    pub(crate) fn maintain_executions() {
        execution_owner::maintain(None);
    }
    pub(crate) fn control(&self) -> &CoreControl {
        &self.inner.control
    }
    pub fn new(
        control: CoreControl,
        runtime: Handle,
        generation: NativeGenerationId,
        max_children: usize,
    ) -> Result<Self, CensusError> {
        if generation.generation == 0 || !(1..=MAX_CHILDREN).contains(&max_children) {
            return Err(CensusError::Invalid);
        }
        let bytes = GENERATION_CONTROL
            .checked_add(size_of::<CensusInner>() as u64)
            .and_then(|n| n.checked_add((max_children * size_of::<FundedWorkRecord>()) as u64))
            .and_then(|n| n.checked_add(generation.profile.as_str().len() as u64))
            .and_then(|n| n.checked_add(generation.pack.as_str().len() as u64))
            .ok_or(CensusError::Capacity)?;
        let reservation = control
            .reserve(ControlKind::Registration, bytes)
            .map_err(|_| CensusError::Capacity)?;
        let mut records = Vec::new();
        records
            .try_reserve_exact(max_children)
            .map_err(|_| CensusError::Capacity)?;
        Ok(Self {
            inner: Arc::new(CensusInner {
                generation,
                control,
                runtime,
                max_children,
                state: Mutex::new(CensusState {
                    closing: None,
                    next_id: 1,
                    #[cfg(test)]
                    completed: 0,
                    #[cfg(test)]
                    never_started: 0,
                    records,
                }),
                _control: reservation,
            }),
        })
    }

    pub fn generation(&self) -> &NativeGenerationId {
        &self.inner.generation
    }

    /// Borrow only the original registered record, independently of seal or
    /// terminal delivery. Unknown records remain registered and cannot be
    /// replaced through this observer.
    pub(crate) fn work_completion(
        &self,
        id: NativeWorkId,
    ) -> Result<NativeWorkCompletion, CensusError> {
        let state = self
            .inner
            .state
            .lock()
            .map_err(|_| CensusError::StateUnavailable)?;
        let record = state
            .records
            .iter()
            .find(|record| record.id == id)
            .ok_or(CensusError::ReceiptLost)?
            .clone();
        Ok(NativeWorkCompletion { record })
    }

    /// Admit and publish exactly one real physical task. The supplied future
    /// must own/await all its native bodies and must not wait for this parent's
    /// terminal/reaper. Its close companion must not strongly own this census.
    /// The wrapper retains the companion even before the task's first poll.
    /// The trusted adapter must choose `unstarted` from actual capture/resource
    /// semantics; this generic module cannot inspect those obligations. Use
    /// RequiresCompletion for every unverified or asynchronous cleanup capture.
    ///
    /// A returned ID is unpublished resource metadata only. Host resource/stream
    /// publication must independently check the same generation is still live;
    /// sealing here never authorizes a late resource publication. This entry
    /// first polls/destroys completed original Joins, so its trusted caller must
    /// hold no Core, generation, census, DB or Store guard. Host paths that have
    /// already performed maintenance use the private prepared entry instead.
    #[cfg(test)]
    pub fn admit_and_spawn<F>(
        &self,
        execution: NativeExecutionId,
        close: Option<Arc<dyn StreamCloseControl>>,
        unstarted: UnstartedCleanupPolicy,
        future: F,
    ) -> Result<NativeWorkId, CensusError>
    where
        F: Future<Output = NativeWorkResult> + Send + 'static,
    {
        self.reap_completed()?;
        self.admit_and_spawn_prepared(execution, close, unstarted, future)
    }

    /// The trusted host has already performed outside-guard maintenance. This
    /// path only reserves/publishes; it never polls or destroys an actual Join.
    pub(crate) fn admit_and_spawn_prepared<F>(
        &self,
        execution: NativeExecutionId,
        close: Option<Arc<dyn StreamCloseControl>>,
        _unstarted: UnstartedCleanupPolicy,
        future: F,
    ) -> Result<NativeWorkId, CensusError>
    where
        F: Future<Output = NativeWorkResult> + Send + 'static,
    {
        if execution.activation == 0 || size_of::<F>() > MAX_FUTURE_BYTES {
            return Err(CensusError::Invalid);
        }
        let owner_bytes = execution_owner::metadata_bytes()?;
        let record_bytes = RECORD_CONTROL
            .checked_add(arc_requested_bound::<WorkRecord>()?)
            .and_then(|bytes| bytes.checked_add(owner_bytes))
            .and_then(|bytes| bytes.checked_add(Funding::storage_bytes() as u64))
            .and_then(|bytes| bytes.checked_mul(2))
            .ok_or(CensusError::Capacity)?;
        let record_control = Funding::try_new_precharged(
            (),
            self.inner
                .control
                .reserve(ControlKind::Queue, record_bytes)
                .map_err(|_| CensusError::Capacity)?,
        )
        .map_err(|_| CensusError::Capacity)?;
        let carrier_bytes = arc_requested_bound::<LaunchCarrier<F>>()?
            .checked_add(darwin_mutex_requested_bound()?)
            .ok_or(CensusError::Capacity)?;
        let task_control = self
            .inner
            .control
            .reserve(
                ControlKind::Task,
                TASK_CONTROL
                    .checked_add(carrier_bytes)
                    .ok_or(CensusError::Capacity)?,
            )
            .map_err(|_| CensusError::Capacity)?;
        let monitor_control = self
            .inner
            .control
            .reserve(ControlKind::Task, MONITOR_CONTROL)
            .map_err(|_| CensusError::Capacity)?;
        let (physical_tx, physical_rx) = oneshot::channel();
        let (started_tx, started_rx) = oneshot::channel();
        let changed = Arc::new(tokio::sync::Notify::new());
        // Fresh, unshared and with no registered Waker: pinned notify_waiters
        // takes its waiter mutex and returns through the empty-list branch.
        // This prepays/initializes Darwin's lazy PAL mutex before publication.
        changed.notify_waiters();
        let (_admission, mut monitor_publication) = execution_owner::reserve(
            record_control.clone(),
            task_control,
            monitor_control,
            physical_tx,
            changed.clone(),
        )?;
        let execution_slot = _admission.id();
        let carrier_payload = Mutex::new(Some(future));
        {
            // Fresh, unshared and fully charged, before every outer guard.
            let prewarmed = carrier_payload
                .try_lock()
                .map_err(|_| CensusError::StateUnavailable)?;
            drop(prewarmed);
        }
        let carrier = Arc::new(LaunchCarrier(carrier_payload));
        let weak_close = close.as_ref().map(Arc::downgrade);
        let mut state = self
            .inner
            .state
            .lock()
            .map_err(|_| CensusError::StateUnavailable)?;
        if state.closing.is_some() {
            return Err(CensusError::Sealed);
        }
        if state.records.len() == self.inner.max_children {
            return Err(CensusError::Capacity);
        }
        let id = NativeWorkId(state.next_id);
        state.next_id = state.next_id.checked_add(1).ok_or(CensusError::Capacity)?;
        let record = FundedWorkRecord {
            record: Arc::new(WorkRecord {
                id,
                execution,
                close: weak_close,
                close_result: Mutex::new(None),
                completion: AsyncMutex::new(Completion {
                    receiver: Some(physical_rx),
                    result: None,
                }),
                started: AsyncMutex::new(LaunchCompletion {
                    receiver: Some(started_rx),
                    result: None,
                }),
                launch_allowed: AtomicBool::new(true),
                execution_slot,
                changed,
                control: self.inner.control.clone(),
                _runtime: self.inner.runtime.clone(),
            }),
            _control: record_control,
        };
        state.records.push(record.clone());

        // Commit Init under the same census guard as seal, then release it
        // BEFORE trusted spawn. A committed Init is already included in
        // retirement and is never reclassified as no task merely because the
        // same original Join has not yet been installed.
        monitor_publication.commit()?;
        drop(state);

        // The admitted monitor publishes the body directly into independent
        // process custody. There is NO Join channel/attach handoff that can
        // lose custody between start and transfer. That same owner retains the
        // monitor Join, independently of waiting futures and retiring hosts.
        let monitor_record = record.clone();
        let monitor_inner = self.inner.clone();
        let monitor_carrier = carrier.clone();
        let monitor_close = close.clone();
        let monitor = monitor_publication.spawn_monitor(|| {
            self.inner.runtime.spawn(async move {
                let mut launch = Some((monitor_carrier.clone(), monitor_close.clone()));
                // Q28 start linearizes here, under the same lock as seal.
                // A reserved/unpublished slot is still included. If sealing
                // won before publication, no task body may start afterwards.
                let task = loop {
                    let changed = monitor_record.changed.clone().notified_owned();
                    tokio::pin!(changed);
                    changed.as_mut().enable();
                    let mut publication =
                        match execution_owner::prepare(execution_slot, execution_owner::Kind::Body)
                        {
                            Ok(execution_owner::Prepared::Busy) => {
                                changed.await;
                                continue;
                            }
                            Ok(execution_owner::Prepared::Ready(publication)) => publication,
                            Err(error) => break Err(error),
                        };
                    let start = {
                        match monitor_inner.state.lock() {
                            Ok(state) => {
                                if state.closing.is_some()
                                    || !monitor_record.launch_allowed.load(Ordering::Acquire)
                                {
                                    Ok(false)
                                } else {
                                    publication.commit().map(|()| true)
                                }
                            }
                            Err(_) => Err(CensusError::StateUnavailable),
                        }
                    };
                    match start {
                        Ok(true) => {
                            let (task_carrier, task_close) =
                                launch.take().expect("native work launch taken twice");
                            let body_inner = monitor_inner.clone();
                            break publication
                                .spawn_body(|| {
                                    monitor_inner.runtime.spawn(async move {
                                        let body_census =
                                            GenerationChildCensus { inner: body_inner };
                                        let _ =
                                            body_census.reap_completed_except(Some(execution_slot));
                                        let future = {
                                            let mut slot = task_carrier
                                                .0
                                                .lock()
                                                .expect("native work carrier poisoned");
                                            slot.take().expect("native work started twice")
                                        };
                                        let result = future.await;
                                        drop(task_close);
                                        let _ =
                                            body_census.reap_completed_except(Some(execution_slot));
                                        result
                                    })
                                })
                                .map(Some);
                        }
                        Ok(false) => {
                            drop(publication);
                            break Ok(None);
                        }
                        Err(error) => {
                            drop(publication);
                            break Err(error);
                        }
                    }
                };
                drop(launch);
                let result = match task {
                    Ok(Some(())) => {
                        // The process owner now holds the original body Join.
                        // Payload acceptance alone never sends launch proof.
                        let _ = started_tx.send(Ok(()));
                        match execution_owner::wait_body(execution_slot, &monitor_record.changed)
                            .await
                        {
                            Ok(Ok(())) => Ok(PhysicalCompletion::Joined),
                            Ok(Err(NativeWorkError::CompletionUnknown)) | Err(_) => {
                                Err(CensusError::TaskUnknown)
                            }
                        }
                    }
                    Ok(None) => {
                        execution_owner::no_body(execution_slot);
                        let _ = started_tx.send(Err(CensusError::Sealed));
                        let _destroyed = monitor_carrier.destroy_unstarted();
                        #[cfg(test)]
                        {
                            match (_unstarted, _destroyed) {
                                (UnstartedCleanupPolicy::SynchronousDrop, Ok(())) => {
                                    Ok(PhysicalCompletion::NeverStarted)
                                }
                                _ => Err(CensusError::TaskUnknown),
                            }
                        }
                        #[cfg(not(test))]
                        Err(CensusError::TaskUnknown)
                    }
                    Err(error) => {
                        let _ = started_tx.send(Err(error));
                        Err(error)
                    }
                };
                // A never-started future may own a Core frame or IO. Destroy it
                // BEFORE reporting that its reserved slot contains no work,
                // outside every publication/lifecycle synchronous guard.
                drop(monitor_carrier);
                drop(monitor_close);
                drop(monitor_record);
                drop(monitor_inner);
                execution_owner::maintain(Some(execution_slot));
                result
            })
        });
        match monitor {
            Ok(()) => {}
            Err(_) => {
                // Even an anomalously published monitor cannot spawn work
                // after this latch, because its start check takes this mutex.
                record.launch_allowed.store(false, Ordering::Release);
                drop(carrier);
                drop(close);
                return Err(CensusError::LaunchUnknown);
            }
        }
        drop(carrier); // Any never-started captured payload drops outside locks.
        drop(close);
        Ok(id)
    }

    /// Reuse only slots whose actual Join succeeded (or provably never began).
    /// Unknown records are never removed to manufacture an empty census.
    pub fn reap_completed(&self) -> Result<(), CensusError> {
        self.reap_completed_except(None)
    }

    fn reap_completed_except(
        &self,
        except: Option<execution_owner::SlotId>,
    ) -> Result<(), CensusError> {
        // Actual Join polling/destruction precedes every census mutex. The
        // remaining loop touches funded immutable receipts only.
        execution_owner::maintain(except);
        let mut cursor = 0;
        let mut remaining = self.inner.max_children;
        while remaining != 0 {
            remaining -= 1;
            let candidate = {
                let state = self
                    .inner
                    .state
                    .lock()
                    .map_err(|_| CensusError::StateUnavailable)?;
                if state.closing.is_some() {
                    return Ok(());
                }
                state.records.get(cursor).cloned()
            };
            let Some(candidate) = candidate else {
                break;
            };
            // Tokio's async-mutex guard Drop can wake an original cache
            // observer. Both acquisition and release happen before taking
            // the outer census guard, including a cached/Empty fast path.
            let Some(Ok(_completion)) = candidate.try_completion() else {
                cursor += 1;
                continue;
            };
            let removed = {
                let mut state = self
                    .inner
                    .state
                    .lock()
                    .map_err(|_| CensusError::StateUnavailable)?;
                if state.closing.is_some() {
                    return Ok(());
                }
                let position = state
                    .records
                    .iter()
                    .position(|record| record.id == candidate.id);
                position
                    .map(|position| -> Result<_, CensusError> {
                        #[cfg(test)]
                        {
                            state.completed = state
                                .completed
                                .checked_add(1)
                                .ok_or(CensusError::Capacity)?;
                            if matches!(_completion, PhysicalCompletion::NeverStarted) {
                                state.never_started = state
                                    .never_started
                                    .checked_add(1)
                                    .ok_or(CensusError::Capacity)?;
                            }
                        }
                        Ok(state.records.swap_remove(position))
                    })
                    .transpose()?
            };
            // The record could release the last close/proof/control owner.
            // Never run such destruction under the publication mutex.
            drop(removed);
        }
        Ok(())
    }

    /// Close admission under the publication mutex. This performs NO callback,
    /// token notification, await, or strong record destruction. It may run under
    /// the host's generation/lifecycle guards. Release those guards before using
    /// the returned handle's notify/wait methods.
    pub fn seal(&self, reason: StreamCloseReason) -> Result<GenerationRetirement, CensusError> {
        {
            let mut state = self
                .inner
                .state
                .lock()
                .map_err(|_| CensusError::StateUnavailable)?;
            state.closing.get_or_insert(reason);
        }
        Ok(GenerationRetirement {
            inner: self.inner.clone(),
        })
    }
}

/// Retains the exact sealed census, its runtime, records and original receipts.
/// Dropping/abandoning a wait does not abort a task, monitor or receipt receiver.
#[derive(Clone)]
pub struct GenerationRetirement {
    inner: Arc<CensusInner>,
}
impl GenerationRetirement {
    /// Notify each original child companion. MUST be called outside ALL host
    /// synchronous guards. This does not prove cleanup or select an audit cause.
    pub fn notify_close(&self) -> Result<(), CensusError> {
        let mut first_error = None;
        let (reason, length) = {
            let state = self
                .inner
                .state
                .lock()
                .map_err(|_| CensusError::StateUnavailable)?;
            (
                state.closing.ok_or(CensusError::StateUnavailable)?,
                state.records.len(),
            )
        };
        for index in 0..length {
            let record = {
                let state = self
                    .inner
                    .state
                    .lock()
                    .map_err(|_| CensusError::StateUnavailable)?;
                state.records[index].clone()
            };
            if let Err(error) = record.notify_close(reason) {
                first_error.get_or_insert(error);
            }
        }
        first_error.map_or(Ok(()), Err)
    }

    /// Await the actual original Join for EVERY admitted/unpublished task.
    /// Caller timeouts may return unknown but must retain this handle; they do
    /// not release physical work or authorize a replacement cleanup/replay.
    pub async fn wait_physical(&self) -> Result<GenerationPhysicalReceipt, CensusError> {
        let _waiter = self
            .inner
            .control
            .reserve(ControlKind::Waiter, WAITER_CONTROL)
            .map_err(|_| CensusError::Capacity)?;
        let length = {
            let state = self
                .inner
                .state
                .lock()
                .map_err(|_| CensusError::StateUnavailable)?;
            if state.closing.is_none() {
                return Err(CensusError::StateUnavailable);
            }
            state.records.len()
        };
        #[cfg(test)]
        let (mut completed, mut never_started) = {
            let state = self
                .inner
                .state
                .lock()
                .map_err(|_| CensusError::StateUnavailable)?;
            (state.completed, state.never_started)
        };
        let mut first_error = None;
        for index in 0..length {
            let record = {
                let state = self
                    .inner
                    .state
                    .lock()
                    .map_err(|_| CensusError::StateUnavailable)?;
                state.records[index].clone()
            };
            let physical = record.wait_physical().await;
            #[cfg(test)]
            if let Ok(completion) = physical {
                completed = completed.checked_add(1).ok_or(CensusError::Capacity)?;
                if matches!(completion, PhysicalCompletion::NeverStarted) {
                    never_started = never_started.checked_add(1).ok_or(CensusError::Capacity)?;
                }
            }
            if let Err(error) = physical {
                first_error.get_or_insert(error);
            }
        }
        if let Some(error) = first_error {
            return Err(error);
        }
        Ok(GenerationPhysicalReceipt {
            _inner: self.inner.clone(),
            #[cfg(test)]
            completed,
            #[cfg(test)]
            never_started,
        })
    }
}

/// Constructed only after seal plus successful original physical Join census.
/// This proves native task completion ONLY, not VM destruction, Gate authority,
/// terminal ACK, success/cancellation disposition or cleanup of detached work.
pub struct GenerationPhysicalReceipt {
    // Retains the same funded census/receipts even when no descriptive accessor
    // is needed by production. This lifetime owner is part of the proof.
    _inner: Arc<CensusInner>,
    #[cfg(test)]
    completed: u64,
    #[cfg(test)]
    never_started: u64,
}
#[cfg(test)]
impl GenerationPhysicalReceipt {
    pub fn generation(&self) -> &NativeGenerationId {
        &self._inner.generation
    }
    pub fn completed(&self) -> u64 {
        self.completed
    }
    pub fn joined(&self) -> u64 {
        self.completed - self.never_started
    }
    pub fn never_started(&self) -> u64 {
        self.never_started
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
    use std::time::Duration;
    use tobkiri_gate::CoreControlLimits;

    fn generation(number: u64) -> NativeGenerationId {
        NativeGenerationId {
            profile: ProfileId::parse("census-test").unwrap(),
            pack: PackId::parse("local.worker").unwrap(),
            hash: PackHash::new(Digest::hash(b"verified-component")),
            revision: Digest::hash(b"frozen-revision"),
            security_epoch: 7,
            generation: number,
        }
    }
    fn execution(activation: u64) -> NativeExecutionId {
        NativeExecutionId {
            frame: Digest::hash(b"actual-private-frame"),
            request: Digest::hash(b"actual-request"),
            activation,
        }
    }
    fn census(control: &CoreControl, capacity: usize) -> GenerationChildCensus {
        GenerationChildCensus::new(control.clone(), Handle::current(), generation(1), capacity)
            .unwrap()
    }
    fn execution_slots(census: &GenerationChildCensus) -> Vec<execution_owner::SlotId> {
        census
            .inner
            .state
            .lock()
            .unwrap()
            .records
            .iter()
            .map(|record| record.execution_slot)
            .collect()
    }

    // A physical receipt certifies the original task/resource completion. A
    // parallel bounded maintenance observer may still own funded immutable
    // metadata after sending it. Wait for this fixture's exact metadata owners
    // to release their actual allocations before asserting the final refund.
    // Never clear/reclassify Unknown, sweep other fixtures, or relax Task2's
    // immediate post-Join assertion.
    async fn await_metadata_deallocation(control: &CoreControl, slots: &[execution_owner::SlotId]) {
        tokio::time::timeout(Duration::from_secs(2), async {
            loop {
                for &slot in slots {
                    execution_owner::progress(slot);
                }
                if control.usage().bytes == 0 {
                    return;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("original fixture metadata observers must release their actual funded allocations");
    }

    struct Intent {
        notified: Arc<AtomicBool>,
    }
    impl StreamCloseControl for Intent {
        fn record_close(&self, _reason: StreamCloseReason) -> Result<(), tobkiri_flow::StepError> {
            self.notified.store(true, Ordering::Release);
            Ok(())
        }
    }

    #[tokio::test]
    async fn unpolled_unpublished_work_is_included_and_keeps_close_intent() {
        let _census_fixture = lock_census_fixture().await;
        let control = CoreControl::default();
        let census = census(&control, 1);
        let notified = Arc::new(AtomicBool::new(false));
        let observed = notified.clone();
        let body_observed = notified.clone();
        let actual_dropped = Arc::new(AtomicBool::new(false));
        struct Payload(Arc<AtomicBool>);
        impl Drop for Payload {
            fn drop(&mut self) {
                self.0.store(true, Ordering::Release);
            }
        }
        let payload = Payload(actual_dropped.clone());
        census
            .admit_and_spawn(
                execution(1),
                Some(Arc::new(Intent { notified })),
                UnstartedCleanupPolicy::SynchronousDrop,
                async move {
                    assert!(body_observed.load(Ordering::Acquire));
                    drop(payload);
                    Ok(())
                },
            )
            .unwrap();
        // Current-thread runtime has not polled either child task yet.
        assert!(!actual_dropped.load(Ordering::Acquire));
        let retired = census.seal(StreamCloseReason::Manual).unwrap();
        retired.notify_close().unwrap();
        assert!(observed.load(Ordering::Acquire));
        let receipt = retired.wait_physical().await.unwrap();
        assert!(actual_dropped.load(Ordering::Acquire));
        assert_eq!(receipt.completed(), 1);
        assert_eq!(receipt.joined(), 0);
        assert_eq!(receipt.never_started(), 1);
        assert_eq!(receipt.generation(), census.generation());
        assert!(matches!(
            census.admit_and_spawn(
                execution(2),
                None,
                UnstartedCleanupPolicy::SynchronousDrop,
                async { Ok(()) }
            ),
            Err(CensusError::Sealed)
        ));
        let slots = execution_slots(&census);
        drop(receipt);
        drop(retired);
        drop(census);
        await_metadata_deallocation(&control, &slots).await;
        assert_eq!(control.usage().bytes, 0);
    }

    #[tokio::test(flavor = "current_thread")]
    async fn original_work_observer_awaits_join_without_seal_and_survives_abandonment() {
        let _census_fixture = lock_census_fixture().await;
        let control = CoreControl::default();
        let census = census(&control, 1);
        let (started, observe_start) = oneshot::channel();
        let (release, blocked) = oneshot::channel();
        let id = census
            .admit_and_spawn(
                execution(1),
                None,
                UnstartedCleanupPolicy::RequiresCompletion,
                async move {
                    let _ = started.send(());
                    let _ = blocked.await;
                    Ok(())
                },
            )
            .unwrap();
        let completion = census.work_completion(id).unwrap();
        {
            // This runtime has not yielded since publication. Tokio spawn does
            // not poll synchronously, so the body Join cannot be published
            // yet. Poll once directly: a zero-duration timeout could yield and
            // let the monitor publish before its timer is observed.
            let mut waiting = Box::pin(completion.wait_started());
            let mut context = std::task::Context::from_waker(std::task::Waker::noop());
            assert!(matches!(
                waiting.as_mut().poll(&mut context),
                std::task::Poll::Pending
            ));
            // Abandon only this wait; the original launch receiver stays put.
        }
        observe_start.await.unwrap();
        completion.wait_started().await.unwrap();
        completion.wait_started().await.unwrap();
        assert!(
            tokio::time::timeout(Duration::from_millis(1), completion.wait_joined())
                .await
                .is_err()
        );
        assert_eq!(control.usage().count(ControlKind::Task), 2);
        release.send(()).unwrap();
        completion.wait_joined().await.unwrap();
        // Waiting this original work does not require sealing its own parent.
        // Reaping may free the registry slot, but never substitutes its proof.
        census.reap_completed().unwrap();
        completion.wait_joined().await.unwrap();
        let retired = census.seal(StreamCloseReason::Manual).unwrap();
        assert_eq!(retired.wait_physical().await.unwrap().joined(), 1);
        let slots = [completion.record.execution_slot];
        drop(completion);
        drop(retired);
        drop(census);
        await_metadata_deallocation(&control, &slots).await;
        assert_eq!(control.usage().bytes, 0);
    }

    #[tokio::test]
    async fn completed_receipts_do_not_retain_scheduled_task_slots() {
        let _census_fixture = lock_census_fixture().await;
        let control = CoreControl::default();
        let census = census(&control, 4);
        let mut receipts = Vec::new();
        for activation in 1..=300 {
            let id = census
                .admit_and_spawn(
                    execution(activation),
                    None,
                    UnstartedCleanupPolicy::RequiresCompletion,
                    async { Ok(()) },
                )
                .unwrap();
            let receipt = census.work_completion(id).unwrap();
            receipt.wait_joined().await.unwrap();
            assert_eq!(control.usage().count(ControlKind::Task), 0);
            receipts.push(receipt);
        }
        // These are the same original immutable proofs, not replacement jobs.
        for receipt in &receipts {
            receipt.wait_joined().await.unwrap();
        }
        let retired = census.seal(StreamCloseReason::Manual).unwrap();
        assert_eq!(retired.wait_physical().await.unwrap().joined(), 300);
        assert_eq!(control.usage().count(ControlKind::Queue), 300);
        let slots: Vec<_> = receipts
            .iter()
            .map(|receipt| receipt.record.execution_slot)
            .collect();
        drop(retired);
        drop(census);
        drop(receipts);
        await_metadata_deallocation(&control, &slots).await;
        assert_eq!(control.usage().bytes, 0);
    }

    #[tokio::test(flavor = "current_thread")]
    async fn maintenance_preserves_all_original_completion_observers() {
        let _census_fixture = lock_census_fixture().await;
        struct ObserverWake(AtomicUsize);
        impl Wake for ObserverWake {
            fn wake(self: Arc<Self>) {
                self.wake_by_ref();
            }
            fn wake_by_ref(self: &Arc<Self>) {
                self.0.fetch_add(1, Ordering::AcqRel);
            }
        }
        let control = CoreControl::default();
        let census = census(&control, 1);
        let (started, observed) = oneshot::channel();
        let (release, blocked) = oneshot::channel();
        let id = census
            .admit_and_spawn(
                execution(1),
                None,
                UnstartedCleanupPolicy::RequiresCompletion,
                async move {
                    let _ = started.send(());
                    let _ = blocked.await;
                    Ok(())
                },
            )
            .unwrap();
        let completion = census.work_completion(id).unwrap();
        observed.await.unwrap();
        let mut first = Box::pin(tokio::task::unconstrained(completion.wait_joined()));
        let mut second = Box::pin(tokio::task::unconstrained(completion.wait_joined()));
        // The body is explicitly blocked. Register both original waiters before
        // independent maintenance polls either Join with its stable owner Wake.
        let first_wake = Arc::new(ObserverWake(AtomicUsize::new(0)));
        let second_wake = Arc::new(ObserverWake(AtomicUsize::new(0)));
        let first_waker = Waker::from(first_wake.clone());
        let second_waker = Waker::from(second_wake.clone());
        assert!(
            first
                .as_mut()
                .poll(&mut Context::from_waker(&first_waker))
                .is_pending()
        );
        assert!(
            second
                .as_mut()
                .poll(&mut Context::from_waker(&second_waker))
                .is_pending()
        );
        GenerationChildCensus::maintain_executions();
        assert_eq!(control.usage().count(ControlKind::Task), 2);
        // A legitimate progress/REPOLL notification may wake an observer, but
        // cannot complete it while the actual body is still blocked. Rearm
        // these same original futures before measuring completion's wakes.
        assert!(
            first
                .as_mut()
                .poll(&mut Context::from_waker(&first_waker))
                .is_pending()
        );
        assert!(
            second
                .as_mut()
                .poll(&mut Context::from_waker(&second_waker))
                .is_pending()
        );
        let first_before_release = first_wake.0.load(Ordering::Acquire);
        let second_before_release = second_wake.0.load(Ordering::Acquire);
        release.send(()).unwrap();
        tokio::time::timeout(Duration::from_secs(2), async {
            while first_wake.0.load(Ordering::Acquire) <= first_before_release
                || second_wake.0.load(Ordering::Acquire) <= second_before_release
            {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("maintenance must preserve both registered observer wakes");
        let (first_result, second_result) = tokio::time::timeout(Duration::from_secs(2), async {
            tokio::join!(first, second)
        })
        .await
        .expect("both original observers must receive the actual Join wake");
        first_result.unwrap();
        second_result.unwrap();
        assert_eq!(control.usage().count(ControlKind::Task), 0);
        census.reap_completed().unwrap();
        completion.wait_joined().await.unwrap();
        let slots = [completion.record.execution_slot];
        drop(completion);
        drop(census);
        await_metadata_deallocation(&control, &slots).await;
        assert_eq!(control.usage().bytes, 0);
    }

    #[tokio::test]
    async fn monitor_poll_panic_restores_original_body_join_and_retains_custody() {
        let _census_fixture = lock_census_fixture().await;
        struct ActualBody(Arc<AtomicBool>);
        impl Drop for ActualBody {
            fn drop(&mut self) {
                self.0.store(true, Ordering::Release);
            }
        }
        let control = CoreControl::default();
        let census = census(&control, 1);
        let destroyed = Arc::new(AtomicBool::new(false));
        let retained = ActualBody(destroyed.clone());
        let (started, observed) = oneshot::channel();
        let (release, blocked) = oneshot::channel();
        let id = census
            .admit_and_spawn(
                execution(1),
                None,
                UnstartedCleanupPolicy::RequiresCompletion,
                async move {
                    let _retained = retained;
                    let _ = started.send(());
                    let _ = blocked.await;
                    Ok(())
                },
            )
            .unwrap();
        let receipt = census.work_completion(id).unwrap();
        observed.await.unwrap();
        execution_owner::panic_monitor(receipt.record.execution_slot);
        for _ in 0..8 {
            tokio::task::yield_now().await;
        }
        assert!(
            tokio::time::timeout(Duration::from_millis(1), receipt.wait_joined())
                .await
                .is_err()
        );
        assert!(!destroyed.load(Ordering::Acquire));
        assert_eq!(control.usage().count(ControlKind::Task), 2);
        release.send(()).unwrap();
        assert!(matches!(
            receipt.wait_joined().await,
            Err(CensusError::TaskUnknown)
        ));
        assert!(destroyed.load(Ordering::Acquire));
        assert_eq!(control.usage().count(ControlKind::Task), 0);
        // Ending both actual tasks does not clear the immutable Unknown result
        // or make this physical census empty/successful.
        census.reap_completed().unwrap();
        assert!(matches!(
            census.admit_and_spawn(
                execution(2),
                None,
                UnstartedCleanupPolicy::RequiresCompletion,
                async { Ok(()) }
            ),
            Err(CensusError::Capacity)
        ));
        let retired = census.seal(StreamCloseReason::Unknown).unwrap();
        assert!(matches!(
            retired.wait_physical().await,
            Err(CensusError::TaskUnknown)
        ));
        let slots = [receipt.record.execution_slot];
        drop(retired);
        drop(receipt);
        drop(census);
        await_metadata_deallocation(&control, &slots).await;
        assert_eq!(control.usage().bytes, 0);
    }

    #[tokio::test]
    async fn abandoned_wait_retains_same_join_and_resources_until_real_completion() {
        let _census_fixture = lock_census_fixture().await;
        let control = CoreControl::default();
        let census = census(&control, 1);
        let starts = Arc::new(AtomicUsize::new(0));
        let actual_starts = starts.clone();
        let (release, blocked) = oneshot::channel();
        let (started, observe_start) = oneshot::channel();
        census
            .admit_and_spawn(
                execution(1),
                None,
                UnstartedCleanupPolicy::RequiresCompletion,
                async move {
                    actual_starts.fetch_add(1, Ordering::AcqRel);
                    let _ = started.send(());
                    let _ = blocked.await;
                    Ok(())
                },
            )
            .unwrap();
        observe_start.await.unwrap();
        let retired = census.seal(StreamCloseReason::Manual).unwrap();
        let before = control.usage().bytes;
        assert!(
            tokio::time::timeout(Duration::from_millis(1), retired.wait_physical())
                .await
                .is_err()
        );
        assert_eq!(starts.load(Ordering::Acquire), 1);
        assert_eq!(control.usage().bytes, before);
        assert_eq!(control.usage().count(ControlKind::Task), 2);
        release.send(()).unwrap();
        assert_eq!(retired.wait_physical().await.unwrap().joined(), 1);
        assert_eq!(retired.wait_physical().await.unwrap().completed(), 1);
        assert_eq!(starts.load(Ordering::Acquire), 1);
        assert_eq!(control.usage().count(ControlKind::Task), 0);
        let slots = execution_slots(&census);
        drop(retired);
        drop(census);
        await_metadata_deallocation(&control, &slots).await;
        assert_eq!(control.usage().bytes, 0);
    }

    #[tokio::test]
    async fn panic_is_cached_unknown_and_never_reaped_as_empty_success() {
        let _census_fixture = lock_census_fixture().await;
        let control = CoreControl::default();
        let census = census(&control, 1);
        census
            .admit_and_spawn(
                execution(1),
                None,
                UnstartedCleanupPolicy::SynchronousDrop,
                async { panic!("private panic payload") },
            )
            .unwrap();
        for _ in 0..8 {
            tokio::task::yield_now().await;
        }
        census.reap_completed().unwrap();
        assert!(matches!(
            census.admit_and_spawn(
                execution(2),
                None,
                UnstartedCleanupPolicy::SynchronousDrop,
                async { Ok(()) }
            ),
            Err(CensusError::Capacity)
        ));
        let retired = census.seal(StreamCloseReason::Unknown).unwrap();
        assert!(matches!(
            retired.wait_physical().await,
            Err(CensusError::TaskUnknown)
        ));
        assert!(matches!(
            retired.wait_physical().await,
            Err(CensusError::TaskUnknown)
        ));
        assert_eq!(control.usage().count(ControlKind::Task), 0);
        assert_eq!(control.usage().count(ControlKind::Queue), 1);
    }

    #[tokio::test]
    async fn only_confirmed_completed_slots_are_reused_without_new_generation_funds() {
        let _census_fixture = lock_census_fixture().await;
        let control = CoreControl::default();
        let census = census(&control, 1);
        let mut previous = 0;
        for activation in 1..=4 {
            let id = census
                .admit_and_spawn(
                    execution(activation),
                    None,
                    UnstartedCleanupPolicy::SynchronousDrop,
                    async { Ok(()) },
                )
                .unwrap();
            assert!(id.get() > previous);
            previous = id.get();
            for _ in 0..8 {
                tokio::task::yield_now().await;
            }
            census.reap_completed().unwrap();
            assert_eq!(control.usage().count(ControlKind::Task), 0);
        }
        let retired = census.seal(StreamCloseReason::Retired).unwrap();
        assert_eq!(retired.wait_physical().await.unwrap().completed(), 4);
    }

    #[tokio::test]
    async fn task_capacity_failure_is_before_work_publication() {
        let _census_fixture = lock_census_fixture().await;
        let control = CoreControl::new(CoreControlLimits {
            max_tasks: 1, // Both the actual task and its monitor must be admitted.
            ..CoreControlLimits::default()
        })
        .unwrap();
        let census = census(&control, 1);
        let ran = Arc::new(AtomicBool::new(false));
        let actual = ran.clone();
        assert!(matches!(
            census.admit_and_spawn(
                execution(1),
                None,
                UnstartedCleanupPolicy::SynchronousDrop,
                async move {
                    actual.store(true, Ordering::Release);
                    Ok(())
                }
            ),
            Err(CensusError::Capacity)
        ));
        tokio::task::yield_now().await;
        assert!(!ran.load(Ordering::Acquire));
        assert_eq!(control.usage().count(ControlKind::Task), 0);
        let retired = census.seal(StreamCloseReason::Retired).unwrap();
        assert_eq!(retired.wait_physical().await.unwrap().completed(), 0);
    }

    #[tokio::test]
    async fn normal_supervisor_join_does_not_hide_lost_nested_native_completion() {
        let _census_fixture = lock_census_fixture().await;
        let control = CoreControl::default();
        let census = census(&control, 1);
        let (started, observed) = oneshot::channel();
        census
            .admit_and_spawn(
                execution(1),
                None,
                UnstartedCleanupPolicy::SynchronousDrop,
                async move {
                    let _ = started.send(());
                    Err(NativeWorkError::CompletionUnknown)
                },
            )
            .unwrap();
        observed.await.unwrap();
        let retired = census.seal(StreamCloseReason::Unknown).unwrap();
        assert!(matches!(
            retired.wait_physical().await,
            Err(CensusError::TaskUnknown)
        ));
        census.reap_completed().unwrap();
        assert_eq!(control.usage().count(ControlKind::Task), 0);
        assert_eq!(control.usage().count(ControlKind::Queue), 1);
    }

    #[tokio::test]
    async fn unpublished_async_cleanup_responsibility_is_unknown_even_after_normal_drop() {
        let _census_fixture = lock_census_fixture().await;
        assert_eq!(
            UnstartedCleanupPolicy::default(),
            UnstartedCleanupPolicy::RequiresCompletion
        );
        struct CleanupResponsibility(Arc<AtomicBool>);
        impl Drop for CleanupResponsibility {
            fn drop(&mut self) {
                // This observes destruction only. It deliberately supplies no
                // typed receipt for an asynchronous cleanup responsibility.
                self.0.store(true, Ordering::Release);
            }
        }
        let control = CoreControl::default();
        let census = census(&control, 1);
        let destroyed = Arc::new(AtomicBool::new(false));
        let responsibility = CleanupResponsibility(destroyed.clone());
        census
            .admit_and_spawn(
                execution(1),
                None,
                UnstartedCleanupPolicy::default(),
                async move {
                    drop(responsibility);
                    Ok(())
                },
            )
            .unwrap();
        // No external work is detached by this test. It proves the default
        // policy cannot promote normal destruction to a cleanup receipt.
        let retired = census.seal(StreamCloseReason::Manual).unwrap();
        assert!(matches!(
            retired.wait_physical().await,
            Err(CensusError::TaskUnknown)
        ));
        assert!(destroyed.load(Ordering::Acquire));
        assert!(matches!(
            retired.wait_physical().await,
            Err(CensusError::TaskUnknown)
        ));
        assert_eq!(control.usage().count(ControlKind::Task), 0);
        assert_eq!(control.usage().count(ControlKind::Queue), 1);
    }

    #[tokio::test]
    async fn unpublished_synchronous_drop_panic_is_cached_unknown() {
        let _census_fixture = lock_census_fixture().await;
        struct PanicOnDrop;
        impl Drop for PanicOnDrop {
            fn drop(&mut self) {
                panic!("fixture synchronous cleanup failed");
            }
        }
        let control = CoreControl::default();
        let census = census(&control, 1);
        let payload = PanicOnDrop;
        census
            .admit_and_spawn(
                execution(1),
                None,
                UnstartedCleanupPolicy::SynchronousDrop,
                async move {
                    drop(payload);
                    Ok(())
                },
            )
            .unwrap();
        let retired = census.seal(StreamCloseReason::Manual).unwrap();
        for _ in 0..2 {
            assert!(matches!(
                retired.wait_physical().await,
                Err(CensusError::TaskUnknown)
            ));
        }
        assert_eq!(control.usage().count(ControlKind::Task), 0);
        assert_eq!(control.usage().count(ControlKind::Queue), 1);
    }

    #[tokio::test]
    async fn rejected_launch_destroys_captured_payload_outside_publication_mutex() {
        let _census_fixture = lock_census_fixture().await;
        struct Payload {
            census: Weak<CensusInner>,
            dropped: Arc<AtomicBool>,
        }
        impl Drop for Payload {
            fn drop(&mut self) {
                let census = self.census.upgrade().unwrap();
                assert!(census.state.try_lock().is_ok());
                self.dropped.store(true, Ordering::Release);
            }
        }
        let control = CoreControl::default();
        let census = census(&control, 1);
        let dropped = Arc::new(AtomicBool::new(false));
        let payload = Payload {
            census: Arc::downgrade(&census.inner),
            dropped: dropped.clone(),
        };
        let retired = census.seal(StreamCloseReason::Retired).unwrap();
        assert!(matches!(
            census.admit_and_spawn(
                execution(1),
                None,
                UnstartedCleanupPolicy::SynchronousDrop,
                async move {
                    drop(payload);
                    Ok(())
                }
            ),
            Err(CensusError::Sealed)
        ));
        assert!(dropped.load(Ordering::Acquire));
        assert_eq!(retired.wait_physical().await.unwrap().completed(), 0);
    }

    #[derive(Clone, Copy)]
    enum ReservedAdmissionCase {
        Open,
        Sealed,
        Capacity,
    }

    async fn admission_with_borrowed_reserved_owner(case: ReservedAdmissionCase) {
        let _census_fixture = lock_census_fixture().await;
        struct Payload {
            census: Weak<CensusInner>,
            dropped: Arc<AtomicBool>,
        }
        impl Drop for Payload {
            fn drop(&mut self) {
                let census = self.census.upgrade().unwrap();
                assert!(census.state.try_lock().is_ok());
                self.dropped.store(true, Ordering::Release);
            }
        }
        let control = CoreControl::default();
        let census = census(&control, 1);
        let existing = if matches!(case, ReservedAdmissionCase::Capacity) {
            Some(
                census
                    .admit_and_spawn(
                        execution(1),
                        None,
                        UnstartedCleanupPolicy::SynchronousDrop,
                        async { Ok(()) },
                    )
                    .unwrap(),
            )
        } else {
            None
        };
        let baseline_tasks = control.usage().count(ControlKind::Task);
        let baseline_bytes = control.usage().bytes;
        let dropped = Arc::new(AtomicBool::new(false));
        let runs = Arc::new(AtomicUsize::new(0));
        let payload = Payload {
            census: Arc::downgrade(&census.inner),
            dropped: dropped.clone(),
        };
        let (reserved_tx, reserved_rx) = std::sync::mpsc::sync_channel(1);
        let (resume_tx, resume_rx) = std::sync::mpsc::sync_channel(1);
        let (result_tx, result_rx) = std::sync::mpsc::sync_channel(1);
        let publisher_census = census.clone();
        let actual_runs = runs.clone();
        let publisher = std::thread::spawn(move || {
            execution_owner::set_admission_pause(reserved_tx, resume_rx);
            let result = publisher_census.admit_and_spawn(
                execution(2),
                None,
                UnstartedCleanupPolicy::SynchronousDrop,
                async move {
                    actual_runs.fetch_add(1, Ordering::AcqRel);
                    drop(payload);
                    Ok(())
                },
            );
            result_tx.send(result).unwrap();
        });
        let slot = reserved_rx.recv_timeout(Duration::from_secs(2)).unwrap();
        let mut retired = if matches!(case, ReservedAdmissionCase::Sealed) {
            Some(census.seal(StreamCloseReason::Manual).unwrap())
        } else {
            None
        };
        let result = execution_owner::with_actual_owner_guard(slot, None, || {
            // A real foreign maintenance borrower holds the exact reserved
            // incarnation while the publisher must finish without its lock.
            resume_tx.send(()).unwrap();
            let result = result_rx.recv_timeout(Duration::from_secs(2)).unwrap();
            match case {
                ReservedAdmissionCase::Open => assert!(result.is_ok(), "{result:?}"),
                ReservedAdmissionCase::Sealed => {
                    assert!(matches!(result, Err(CensusError::Sealed)))
                }
                ReservedAdmissionCase::Capacity => {
                    assert!(matches!(result, Err(CensusError::Capacity)))
                }
            }
            assert_eq!(runs.load(Ordering::Acquire), 0);
            assert_eq!(dropped.load(Ordering::Acquire), result.is_err());
            assert_eq!(
                census.inner.state.lock().unwrap().records.len(),
                usize::from(existing.is_some()) + usize::from(result.is_ok())
            );
            // Rejection cannot refund the reservation while this original
            // owner borrower still pins its actual prepared allocations.
            assert_eq!(control.usage().count(ControlKind::Task), baseline_tasks + 2);
            assert!(control.usage().bytes > baseline_bytes);
            assert!(execution_owner::owner_attached(slot));
            result
        })
        .expect("reserved owner must be available to the foreign borrower");
        publisher.join().unwrap();
        if result.is_err() {
            assert!(!execution_owner::owner_attached(slot));
            assert_eq!(control.usage().count(ControlKind::Task), baseline_tasks);
            assert_eq!(control.usage().bytes, baseline_bytes);
        }
        let slots = execution_slots(&census);
        let admitted = result.ok().or(existing);
        if let Some(id) = admitted {
            let completion = census.work_completion(id).unwrap();
            tokio::time::timeout(Duration::from_secs(2), completion.wait_joined())
                .await
                .unwrap()
                .unwrap();
        }
        if retired.is_none() {
            retired = Some(census.seal(StreamCloseReason::Manual).unwrap());
        }
        let retired = retired.unwrap();
        let receipt = tokio::time::timeout(Duration::from_secs(2), retired.wait_physical())
            .await
            .unwrap()
            .unwrap();
        assert_eq!(receipt.completed(), u64::from(admitted.is_some()));
        assert_eq!(receipt.joined(), u64::from(admitted.is_some()));
        assert_eq!(receipt.never_started(), 0);
        assert_eq!(
            runs.load(Ordering::Acquire),
            usize::from(matches!(case, ReservedAdmissionCase::Open))
        );
        assert!(dropped.load(Ordering::Acquire));
        assert_eq!(control.usage().count(ControlKind::Task), 0);
        // The physical receipt deliberately owns the same funded census.
        // Destroy this original observer before asking for its final refund.
        drop(receipt);
        drop(retired);
        drop(census);
        await_metadata_deallocation(&control, &slots).await;
        assert_eq!(control.usage().bytes, 0);
    }

    #[tokio::test(flavor = "current_thread")]
    async fn reserved_monitor_admits_while_original_owner_is_borrowed() {
        admission_with_borrowed_reserved_owner(ReservedAdmissionCase::Open).await;
    }

    #[tokio::test(flavor = "current_thread")]
    async fn reserved_monitor_seal_rejection_keeps_borrowed_funding() {
        admission_with_borrowed_reserved_owner(ReservedAdmissionCase::Sealed).await;
    }

    #[tokio::test(flavor = "current_thread")]
    async fn reserved_monitor_capacity_rejection_keeps_borrowed_funding() {
        admission_with_borrowed_reserved_owner(ReservedAdmissionCase::Capacity).await;
    }

    #[tokio::test]
    async fn seal_and_publication_race_has_no_unregistered_or_late_child() {
        let _census_fixture = lock_census_fixture().await;
        let control = CoreControl::default();
        let census = census(&control, 16);
        let barrier = Arc::new(std::sync::Barrier::new(9));
        let actual = Arc::new(AtomicUsize::new(0));
        let mut publishers = Vec::new();
        for activation in 1..=8 {
            let child_census = census.clone();
            let start = barrier.clone();
            let runs = actual.clone();
            publishers.push(std::thread::spawn(move || {
                start.wait();
                child_census.admit_and_spawn(
                    execution(activation),
                    None,
                    UnstartedCleanupPolicy::SynchronousDrop,
                    async move {
                        runs.fetch_add(1, Ordering::AcqRel);
                        Ok(())
                    },
                )
            }));
        }
        barrier.wait();
        let retired = census.seal(StreamCloseReason::Manual).unwrap();
        let mut accepted = 0;
        for publisher in publishers {
            match publisher.join().unwrap() {
                Ok(_) => accepted += 1,
                Err(CensusError::Sealed) => {}
                result => panic!("unexpected admission result: {result:?}"),
            }
        }
        let receipt = retired.wait_physical().await.unwrap();
        assert_eq!(receipt.completed(), accepted);
        assert_eq!(actual.load(Ordering::Acquire) as u64, receipt.joined());
        assert_eq!(receipt.joined() + receipt.never_started(), accepted);
        assert!(matches!(
            census.admit_and_spawn(
                execution(9),
                None,
                UnstartedCleanupPolicy::SynchronousDrop,
                async { Ok(()) }
            ),
            Err(CensusError::Sealed)
        ));
    }

    #[test]
    fn runtime_shutdown_is_unknown_not_an_empty_generation_receipt() {
        let _census_fixture = lock_census_fixture_blocking();
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let control = CoreControl::default();
        let census =
            GenerationChildCensus::new(control.clone(), runtime.handle().clone(), generation(1), 1)
                .unwrap();
        census
            .admit_and_spawn(
                execution(1),
                None,
                UnstartedCleanupPolicy::SynchronousDrop,
                async {
                    std::future::pending::<()>().await;
                    Ok(())
                },
            )
            .unwrap();
        let retired = census.seal(StreamCloseReason::Shutdown).unwrap();
        runtime.shutdown_timeout(Duration::from_millis(10));
        let observer = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        assert!(observer.block_on(retired.wait_physical()).is_err());
        assert_eq!(control.usage().count(ControlKind::Task), 0);
        assert_eq!(control.usage().count(ControlKind::Queue), 1);
    }

    #[tokio::test]
    async fn close_callback_can_reenter_census_without_locking_or_starting_late_work() {
        let _census_fixture = lock_census_fixture().await;
        struct ReentrantClose(GenerationChildCensus);
        impl StreamCloseControl for ReentrantClose {
            fn record_close(
                &self,
                _reason: StreamCloseReason,
            ) -> Result<(), tobkiri_flow::StepError> {
                // A strong census reference in a close control is deliberately
                // forbidden in production; this test checks callback lock order.
                let state = self.0.inner.state.try_lock().unwrap();
                assert!(state.closing.is_some());
                drop(state);
                assert!(matches!(
                    self.0.admit_and_spawn(
                        execution(2),
                        None,
                        UnstartedCleanupPolicy::SynchronousDrop,
                        async { Ok(()) }
                    ),
                    Err(CensusError::Sealed)
                ));
                Ok(())
            }
        }
        let control = CoreControl::default();
        let census = census(&control, 1);
        census
            .admit_and_spawn(
                execution(1),
                Some(Arc::new(ReentrantClose(census.clone()))),
                UnstartedCleanupPolicy::SynchronousDrop,
                async { Ok(()) },
            )
            .unwrap();
        let retired = census.seal(StreamCloseReason::Shutdown).unwrap();
        retired.notify_close().unwrap();
        retired.wait_physical().await.unwrap();
    }

    async fn blocked_child(
        census: &GenerationChildCensus,
        activation: u64,
    ) -> (NativeWorkCompletion, oneshot::Sender<()>) {
        let (started, observed) = oneshot::channel();
        let (release, blocked) = oneshot::channel();
        let id = census
            .admit_and_spawn(
                execution(activation),
                None,
                UnstartedCleanupPolicy::RequiresCompletion,
                async move {
                    let _ = started.send(());
                    let _ = blocked.await;
                    Ok(())
                },
            )
            .unwrap();
        let completion = census.work_completion(id).unwrap();
        observed.await.unwrap();
        (completion, release)
    }

    #[tokio::test(flavor = "current_thread")]
    async fn borrowed_join_real_wake_is_repolled_after_restore() {
        let _census_fixture = lock_census_fixture().await;
        let control = CoreControl::default();
        let census = census(&control, 1);
        let (completion, release) = blocked_child(&census, 1).await;
        let slot = completion.record.execution_slot;
        let mut wait = Box::pin(completion.wait_joined());
        // One real poll stores the OwnerWake inside the actual monitor Join.
        assert!(
            wait.as_mut()
                .poll(&mut Context::from_waker(Waker::noop()))
                .is_pending()
        );
        // A foreign maintenance borrower holds the original monitor Join.
        let lease = execution_owner::borrow_join(slot, execution_owner::Kind::Monitor)
            .expect("original monitor Join published");
        release.send(()).unwrap();
        // The real body and monitor completion wakes arrive while borrowed;
        // the waiter consumes them but cannot access the borrowed Join.
        tokio::time::timeout(Duration::from_secs(2), async {
            while execution_owner::wake_count(slot) < 2 {
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("real task completion wakes must reach the slot waker");
        assert!(
            tokio::time::timeout(Duration::from_millis(50), wait.as_mut())
                .await
                .is_err()
        );
        // Restoring arranges a fresh poll of the SAME original Join; no
        // unrelated maintenance call rescues the receipt.
        drop(lease);
        tokio::time::timeout(Duration::from_secs(2), wait.as_mut())
            .await
            .expect("restored original Join must be repolled")
            .unwrap();
        drop(wait);
        completion.wait_joined().await.unwrap();
        assert_eq!(control.usage().count(ControlKind::Task), 0);
        let slots = [slot];
        drop(completion);
        drop(census);
        await_metadata_deallocation(&control, &slots).await;
        assert_eq!(control.usage().bytes, 0);
    }

    #[tokio::test(flavor = "current_thread")]
    async fn state_wait_request_survives_unlock_in_both_crossing_orders() {
        let _census_fixture = lock_census_fixture().await;
        let control = CoreControl::default();
        let census = census(&control, 1);
        let (completion, release) = blocked_child(&census, 1).await;
        let slot = completion.record.execution_slot;
        let mut wait = Box::pin(completion.wait_joined());
        assert!(
            wait.as_mut()
                .poll(&mut Context::from_waker(Waker::noop()))
                .is_pending()
        );
        // Order 1: request lands while a foreign thread holds the guard, and
        // that holder's own unlock handoff serves it.
        let (locked_tx, locked_rx) = std::sync::mpsc::channel::<()>();
        let (drop_tx, drop_rx) = std::sync::mpsc::channel::<()>();
        let holder = std::thread::spawn(move || {
            execution_owner::with_state_guard(slot, || {
                let _ = locked_tx.send(());
                let _ = drop_rx.recv();
            })
        });
        locked_rx.recv().unwrap();
        assert!(!execution_owner::try_observe_state(slot));
        assert!(execution_owner::state_requested(slot));
        let repolls = execution_owner::repoll_count(slot);
        drop_tx.send(()).unwrap();
        assert!(holder.join().unwrap().is_some());
        assert!(!execution_owner::state_requested(slot));
        assert!(execution_owner::repoll_count(slot) > repolls);
        // Order 2: a request landing after the unlock stays visible until the
        // next real unlock consumes and serves it; nothing sleeps forever.
        execution_owner::request_state(slot);
        let repolls = execution_owner::repoll_count(slot);
        assert!(execution_owner::try_observe_state(slot));
        assert!(!execution_owner::state_requested(slot));
        assert!(execution_owner::repoll_count(slot) > repolls);
        release.send(()).unwrap();
        tokio::time::timeout(Duration::from_secs(2), wait.as_mut())
            .await
            .expect("observer must be renotified after the unlock")
            .unwrap();
        drop(wait);
        assert_eq!(control.usage().count(ControlKind::Task), 0);
        let slots = [slot];
        drop(completion);
        drop(census);
        await_metadata_deallocation(&control, &slots).await;
        assert_eq!(control.usage().bytes, 0);
    }

    #[tokio::test]
    async fn contended_completion_cache_serves_the_same_physical_receipt() {
        let _census_fixture = lock_census_fixture().await;
        let control = CoreControl::default();
        let census = census(&control, 1);
        let (completion, release) = blocked_child(&census, 1).await;
        let second = census.work_completion(completion.record.id).unwrap();
        // Hold the completion guard so both async observers must queue on it.
        let held = completion.record.completion.lock().await;
        let wait_first = tokio::spawn({
            let completion = completion.clone();
            async move { completion.wait_joined().await }
        });
        let wait_second = tokio::spawn({
            let second = second.clone();
            async move { second.wait_joined().await }
        });
        for _ in 0..4 {
            tokio::task::yield_now().await;
        }
        assert!(!wait_first.is_finished() && !wait_second.is_finished());
        release.send(()).unwrap();
        drop(held);
        let (first_result, second_result) = tokio::time::timeout(Duration::from_secs(2), async {
            tokio::join!(wait_first, wait_second)
        })
        .await
        .expect("contended cache acquisition must not sleep forever");
        first_result.unwrap().unwrap();
        second_result.unwrap().unwrap();
        // The original receiver survived; later observers share the same cache.
        completion.wait_joined().await.unwrap();
        second.wait_joined().await.unwrap();
        assert_eq!(control.usage().count(ControlKind::Task), 0);
        let slots = [completion.record.execution_slot];
        drop(completion);
        drop(second);
        drop(census);
        await_metadata_deallocation(&control, &slots).await;
        assert_eq!(control.usage().bytes, 0);
    }

    #[tokio::test(flavor = "current_thread")]
    async fn pending_join_without_real_wake_creates_no_repoll_obligations() {
        let _census_fixture = lock_census_fixture().await;
        let control = CoreControl::default();
        let census = census(&control, 1);
        let (completion, release) = blocked_child(&census, 1).await;
        let slot = completion.record.execution_slot;
        let repolls = execution_owner::repoll_count(slot);
        // Repeated foreign borrow/Pending-restore cycles create no obligations.
        for _ in 0..4 {
            let lease = execution_owner::borrow_join(slot, execution_owner::Kind::Monitor)
                .expect("original monitor Join published");
            assert_eq!(execution_owner::wake_count(slot), 0);
            drop(lease);
            assert_eq!(execution_owner::repoll_count(slot), repolls);
        }
        // Bounded maintenance polls do not self-grow wake or repoll counts.
        for _ in 0..4 {
            execution_owner::progress(slot);
        }
        assert_eq!(execution_owner::wake_count(slot), 0);
        assert_eq!(execution_owner::repoll_count(slot), repolls);
        assert!(
            tokio::time::timeout(Duration::from_millis(50), completion.wait_joined())
                .await
                .is_err()
        );
        // A real wake still settles; actual task destruction precedes release.
        release.send(()).unwrap();
        tokio::time::timeout(Duration::from_secs(2), completion.wait_joined())
            .await
            .expect("real wake must settle the receipt")
            .unwrap();
        assert_eq!(control.usage().count(ControlKind::Task), 0);
        let slots = [slot];
        drop(completion);
        drop(census);
        await_metadata_deallocation(&control, &slots).await;
        assert_eq!(control.usage().bytes, 0);
    }

    #[tokio::test(flavor = "current_thread")]
    async fn completed_cache_unlock_wakes_two_original_observers_without_rescue() {
        let _census_fixture = lock_census_fixture().await;
        struct CountWake(AtomicUsize);
        impl Wake for CountWake {
            fn wake(self: Arc<Self>) {
                self.wake_by_ref();
            }
            fn wake_by_ref(self: &Arc<Self>) {
                self.0.fetch_add(1, Ordering::AcqRel);
            }
        }

        let control = CoreControl::default();
        let census = census(&control, 1);
        let (completion, release) = blocked_child(&census, 1).await;
        let slot = completion.record.execution_slot;
        let first = census.work_completion(completion.record.id).unwrap();
        let second = census.work_completion(completion.record.id).unwrap();
        release.send(()).unwrap();
        completion.wait_joined().await.unwrap();
        assert_eq!(control.usage().count(ControlKind::Task), 0);

        // Establish a real completed original Join and its populated cache
        // BEFORE measuring the cache mutex's independent handoff. No task,
        // receipt channel or execution-owner progress remains to rescue it.
        let held = completion.record.completion.lock().await;
        assert!(matches!(held.result, Some(Ok(PhysicalCompletion::Joined))));
        assert!(held.receiver.is_none());
        let first_wake = Arc::new(CountWake(AtomicUsize::new(0)));
        let second_wake = Arc::new(CountWake(AtomicUsize::new(0)));
        let first_waker = Waker::from(first_wake.clone());
        let second_waker = Waker::from(second_wake.clone());
        // Exclude ambient cooperative-budget Pending/self-wakes: these are
        // the two actual wait_joined futures, each queued on the real mutex.
        let mut first_wait = Box::pin(tokio::task::unconstrained(first.wait_joined()));
        let mut second_wait = Box::pin(tokio::task::unconstrained(second.wait_joined()));
        assert!(
            first_wait
                .as_mut()
                .poll(&mut Context::from_waker(&first_waker))
                .is_pending()
        );
        assert!(
            second_wait
                .as_mut()
                .poll(&mut Context::from_waker(&second_waker))
                .is_pending()
        );
        assert_eq!(first_wake.0.load(Ordering::Acquire), 0);
        assert_eq!(second_wake.0.load(Ordering::Acquire), 0);

        // No await, sleep, yield, explicit notification or maintenance call
        // occurs between parking both originals and proving both real wakes.
        drop(held);
        assert!(
            first_wake.0.load(Ordering::Acquire) > 0,
            "the real cache unlock must wake its first queued original"
        );
        assert_eq!(second_wake.0.load(Ordering::Acquire), 0);
        assert!(matches!(
            first_wait
                .as_mut()
                .poll(&mut Context::from_waker(&first_waker)),
            Poll::Ready(Ok(()))
        ));
        assert!(
            second_wake.0.load(Ordering::Acquire) > 0,
            "the first original's real guard release must wake the second"
        );
        assert!(matches!(
            second_wait
                .as_mut()
                .poll(&mut Context::from_waker(&second_waker)),
            Poll::Ready(Ok(()))
        ));
        drop(first_wait);
        drop(second_wait);
        completion.wait_joined().await.unwrap();
        assert_eq!(control.usage().count(ControlKind::Task), 0);
        drop(first_waker);
        drop(second_waker);
        drop(first);
        drop(second);
        drop(completion);
        drop(census);
        // Cleanup waits begin only after all measured liveness assertions.
        await_metadata_deallocation(&control, &[slot]).await;
        assert_eq!(control.usage().bytes, 0);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 3)]
    async fn actual_trailer_last_waker_retires_without_observers_at_all_owner_crossings() {
        let _census_fixture = lock_census_fixture().await;
        // Both real trailer orders: body remains the last Waker block after
        // monitor Ready, or monitor itself remains the last Waker block.
        for kind in [execution_owner::Kind::Body, execution_owner::Kind::Monitor] {
            for crossing in 0..3 {
                let diagnostic_kind = if matches!(kind, execution_owner::Kind::Body) {
                    "body"
                } else {
                    "monitor"
                };
                eprintln!(
                    "CENSUS_REV2 fixture=owner kind={diagnostic_kind} crossing={crossing} phase=fixture_begin"
                );
                let control = CoreControl::default();
                let census = census(&control, 1);
                let (completion, release) = blocked_child(&census, 1).await;
                eprintln!(
                    "CENSUS_REV2 fixture=owner kind={diagnostic_kind} crossing={crossing} phase=body_started"
                );
                let slot = completion.record.execution_slot;
                let pause = execution_owner::pause_actual_trailer_wake(slot, kind).unwrap();
                assert!(execution_owner::prime_original_body_pending(slot));
                eprintln!(
                    "CENSUS_REV2 fixture=owner kind={diagnostic_kind} crossing={crossing} phase=body_join_pending_primed"
                );
                let mut wait = Box::pin(tokio::task::unconstrained(completion.wait_joined()));
                assert!(
                    wait.as_mut()
                        .poll(&mut Context::from_waker(Waker::noop()))
                        .is_pending()
                );
                eprintln!(
                    "CENSUS_REV2 fixture=owner kind={diagnostic_kind} crossing={crossing} phase=original_monitor_pending_primed"
                );
                release.send(()).unwrap();
                eprintln!(
                    "CENSUS_REV2 fixture=owner kind={diagnostic_kind} crossing={crossing} phase=body_released_waiting_actual_wake"
                );
                // These are receipts from the actual dedicated Wakers called
                // by Tokio after its COMPLETE bit, not shadow completion bits.
                pause.entered.recv().unwrap();
                eprintln!(
                    "CENSUS_REV2 fixture=owner kind={diagnostic_kind} crossing={crossing} phase=actual_target_wake_entered_waiting_monitor_wake"
                );
                pause.monitor_ready.recv().unwrap();
                eprintln!(
                    "CENSUS_REV2 fixture=owner kind={diagnostic_kind} crossing={crossing} phase=actual_monitor_wake_entered"
                );
                assert_eq!(
                    execution_owner::original_monitor_probe(slot),
                    execution_owner::OriginalMonitorProbe::MonitorPresent { finished: true }
                );
                eprintln!(
                    "CENSUS_REV2 fixture=owner kind={diagnostic_kind} crossing={crossing} phase=actual_monitor_complete"
                );
                assert!(
                    wait.as_mut()
                        .poll(&mut Context::from_waker(Waker::noop()))
                        .is_pending()
                );
                eprintln!(
                    "CENSUS_REV2 fixture=owner kind={diagnostic_kind} crossing={crossing} phase=original_join_ready_polled"
                );
                // Actual original Ready/Join Drop cannot refund these tasks
                // while the real Tokio trailer still owns its last token.
                assert_eq!(control.usage().count(ControlKind::Task), 2);

                let (owner_pause, owner_at_crossing, resume_owner) =
                    execution_owner::owner_pause(crossing);
                let (locked_tx, locked_rx) = std::sync::mpsc::sync_channel(1);
                let (unlock_tx, unlock_rx) = std::sync::mpsc::sync_channel(1);
                let holder = std::thread::spawn(move || {
                    execution_owner::with_actual_owner_guard(slot, Some(owner_pause), || {
                        locked_tx.send(()).unwrap();
                        unlock_rx.recv().unwrap();
                    })
                });
                locked_rx.recv().unwrap();
                eprintln!(
                    "CENSUS_REV2 fixture=owner kind={diagnostic_kind} crossing={crossing} phase=foreign_holder_acquired"
                );
                drop(wait);
                drop(completion);
                drop(census);
                eprintln!(
                    "CENSUS_REV2 fixture=owner kind={diagnostic_kind} crossing={crossing} phase=zero_original_observers"
                );
                // From here through actual detach there are ZERO original
                // receipt observers and ZERO progress/maintenance calls.
                if crossing != 0 {
                    unlock_tx.send(()).unwrap();
                    owner_at_crossing.recv().unwrap();
                }
                // crossing0: request before raw unlock. crossing1: after gate
                // Available before request check. crossing2: after that check.
                pause.resume.send(()).unwrap();
                eprintln!(
                    "CENSUS_REV2 fixture=owner kind={diagnostic_kind} crossing={crossing} phase=actual_target_wake_resumed_waiting_dealloc"
                );
                pause.deallocated.recv().unwrap();
                eprintln!(
                    "CENSUS_REV2 fixture=owner kind={diagnostic_kind} crossing={crossing} phase=actual_waker_deallocated"
                );
                assert_eq!(control.usage().count(ControlKind::Task), 0);
                if crossing == 0 {
                    assert!(execution_owner::owner_attached(slot));
                    unlock_tx.send(()).unwrap();
                } else {
                    assert!(!execution_owner::owner_attached(slot));
                    // The old incarnation's actual holder still pins and
                    // funds its own detached metadata until its normal exit.
                    assert!(control.usage().bytes > 0);
                    resume_owner.send(()).unwrap();
                }
                assert!(holder.join().unwrap().is_some());
                eprintln!(
                    "CENSUS_REV2 fixture=owner kind={diagnostic_kind} crossing={crossing} phase=foreign_holder_returned"
                );
                assert!(!execution_owner::owner_attached(slot));
                assert_eq!(control.usage().bytes, 0);
            }
        }
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 3)]
    async fn last_actual_trailer_release_hands_finalization_to_foreign_state_holder() {
        let _census_fixture = lock_census_fixture().await;
        for kind in [execution_owner::Kind::Body, execution_owner::Kind::Monitor] {
            let diagnostic_kind = if matches!(kind, execution_owner::Kind::Body) {
                "body"
            } else {
                "monitor"
            };
            eprintln!("CENSUS_REV2 fixture=state kind={diagnostic_kind} phase=fixture_begin");
            let control = CoreControl::default();
            let census = census(&control, 1);
            let (completion, release) = blocked_child(&census, 1).await;
            eprintln!("CENSUS_REV2 fixture=state kind={diagnostic_kind} phase=body_started");
            let slot = completion.record.execution_slot;
            let pause = execution_owner::pause_actual_trailer_wake(slot, kind).unwrap();
            assert!(execution_owner::prime_original_body_pending(slot));
            eprintln!(
                "CENSUS_REV2 fixture=state kind={diagnostic_kind} phase=body_join_pending_primed"
            );
            let mut wait = Box::pin(tokio::task::unconstrained(completion.wait_joined()));
            assert!(
                wait.as_mut()
                    .poll(&mut Context::from_waker(Waker::noop()))
                    .is_pending()
            );
            eprintln!(
                "CENSUS_REV2 fixture=state kind={diagnostic_kind} phase=original_monitor_pending_primed"
            );
            release.send(()).unwrap();
            eprintln!(
                "CENSUS_REV2 fixture=state kind={diagnostic_kind} phase=body_released_waiting_actual_wake"
            );
            pause.entered.recv().unwrap();
            eprintln!(
                "CENSUS_REV2 fixture=state kind={diagnostic_kind} phase=actual_target_wake_entered_waiting_monitor_wake"
            );
            pause.monitor_ready.recv().unwrap();
            eprintln!(
                "CENSUS_REV2 fixture=state kind={diagnostic_kind} phase=actual_monitor_wake_entered"
            );
            assert_eq!(
                execution_owner::original_monitor_probe(slot),
                execution_owner::OriginalMonitorProbe::MonitorPresent { finished: true }
            );
            eprintln!(
                "CENSUS_REV2 fixture=state kind={diagnostic_kind} phase=actual_monitor_complete"
            );
            assert!(
                wait.as_mut()
                    .poll(&mut Context::from_waker(Waker::noop()))
                    .is_pending()
            );
            eprintln!(
                "CENSUS_REV2 fixture=state kind={diagnostic_kind} phase=original_join_ready_polled"
            );
            let (locked_tx, locked_rx) = std::sync::mpsc::sync_channel(1);
            let (unlock_tx, unlock_rx) = std::sync::mpsc::sync_channel(1);
            let holder = std::thread::spawn(move || {
                execution_owner::with_state_guard(slot, || {
                    locked_tx.send(()).unwrap();
                    unlock_rx.recv().unwrap();
                })
            });
            locked_rx.recv().unwrap();
            eprintln!(
                "CENSUS_REV2 fixture=state kind={diagnostic_kind} phase=foreign_holder_acquired"
            );
            drop(wait);
            drop(completion);
            drop(census);
            eprintln!(
                "CENSUS_REV2 fixture=state kind={diagnostic_kind} phase=zero_original_observers"
            );
            // The actual final trailer block now deallocates with no receipt
            // observer. Its request crosses a real foreign State guard.
            pause.resume.send(()).unwrap();
            eprintln!(
                "CENSUS_REV2 fixture=state kind={diagnostic_kind} phase=actual_target_wake_resumed_waiting_dealloc"
            );
            pause.deallocated.recv().unwrap();
            eprintln!(
                "CENSUS_REV2 fixture=state kind={diagnostic_kind} phase=actual_waker_deallocated"
            );
            assert!(execution_owner::owner_attached(slot));
            assert_eq!(control.usage().count(ControlKind::Task), 2);
            unlock_tx.send(()).unwrap();
            assert!(holder.join().unwrap().is_some());
            eprintln!(
                "CENSUS_REV2 fixture=state kind={diagnostic_kind} phase=foreign_holder_returned"
            );
            // No poll, notification rescue, timer or maintenance call occurs
            // between final block deallocation and this actual holder exit.
            assert!(!execution_owner::owner_attached(slot));
            assert_eq!(control.usage().bytes, 0);
        }
    }
}
