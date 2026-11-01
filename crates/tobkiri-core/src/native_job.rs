//! Host-owned job lifetime; no worker pool, Python Context or effect authority.
//! The fixed production callback/worker must separately prove nested storage,
//! actual scope and Join custody. This opt-in candidate does not admit it.
use crate::native_state_registry::{NativeRef, NativeRegistry};
use std::sync::{
    Condvar, Mutex, MutexGuard, OnceLock,
    atomic::{AtomicBool, Ordering},
};
use std::time::Instant;
use tobkiri_gate::{
    CapacityKind, GateError, NativeSetupAccount, NativeSetupInvocation,
    NativeSharedStateReservation,
};

const SYNC_STORAGE_BYTES: u64 = 1024;
type Namespace = NativeRef<()>;
type OwnerRef = NativeRef<Owner>;
type StateRef<T> = NativeRef<State<T>>;
type JobRef<T> = NativeRef<Job<T>>;

struct State<T> {
    process: u32,
    account: NativeSetupAccount,
    membership: NativeSharedStateReservation,
    namespace: Namespace,
    maximum: usize,
    registry: Mutex<NativeRegistry<Job<T>>>,
}
struct Owner {
    namespace: Namespace,
    fenced: AtomicBool,
}
struct Job<T> {
    process: u32,
    operation: NativeSetupInvocation,
    membership: NativeSharedStateReservation,
    owner: OwnerRef,
    deadline: Instant,
    state: Mutex<JobState>,
    changed: Condvar,
    // Inline T is prepaid in this exact block; immutable views retain it.
    result: OnceLock<T>,
}
#[derive(Clone, Copy, PartialEq, Eq)]
enum Phase {
    Queued,
    Running,
    Finished,
}
#[derive(Clone, Copy)]
enum Outcome {
    Pending,
    Cancelled,
    Ready,
    Failed(NativeJobFailure),
}
struct JobState {
    phase: Phase,
    outcome: Outcome,
}

pub struct NativeJobs<T>(StateRef<T>);
impl<T> Clone for NativeJobs<T> {
    fn clone(&self) -> Self {
        Self(self.0.clone())
    }
}
#[derive(Clone)]
pub struct NativeJobOwner(OwnerRef);
pub struct NativeJobFuture<T>(JobRef<T>);
impl<T> Clone for NativeJobFuture<T> {
    fn clone(&self) -> Self {
        Self(self.0.clone())
    }
}
pub struct NativeJobResult<T>(JobRef<T>);
impl<T> Clone for NativeJobResult<T> {
    fn clone(&self) -> Self {
        Self(self.0.clone())
    }
}
impl<T> NativeJobResult<T> {
    pub fn get(&self) -> &T {
        self.0
            .get()
            .result
            .get()
            .expect("published native job result")
    }
}

/// Only the queue/worker owns this non-cloneable ticket. Caller Future drop,
/// cancel and owner fence do not destroy it or finish actual running work.
pub struct NativeJobTicket<T> {
    coordinator: NativeJobs<T>,
    job: JobRef<T>,
}

/// A borrowed real operation/cancellation boundary, not a grant. It cannot
/// extend its operation deadline or claim an extra Store.
pub struct NativeJobContext<'a, T> {
    job: &'a JobRef<T>,
}

// Private two-phase boundary: only the real context computes the checkpoint.
// The retained job, original clock and current cancel state are checked again
// at entry; this record is not exported as operation/effect authority.
struct PreparedStart<'a, T> {
    job: &'a JobRef<T>,
    checkpoint: Result<(), NativeJobFailure>,
}
impl<T> PreparedStart<'_, T> {
    fn enter(self) -> bool {
        let job = self.job.get();
        if job.process != std::process::id() {
            return false;
        }
        let mut state = held(&job.state);
        if !matches!(state.outcome, Outcome::Pending) {
            false
        } else if let Err(error) = self.checkpoint {
            state.outcome = Outcome::Failed(error);
            job.changed.notify_all();
            false
        } else if Instant::now() >= job.deadline {
            state.outcome = Outcome::Failed(NativeJobFailure::Deadline);
            job.changed.notify_all();
            false
        } else {
            state.phase = Phase::Running;
            true
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum NativeJobFailure {
    Cancelled,
    Deadline,
    ScopeUnavailable,
    CallbackPanicked,
}
pub enum NativeJobObservation<T> {
    Ready(NativeJobResult<T>),
    Cancelled,
    TimedOut,
    Failed(NativeJobFailure),
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct NativeJobsSnapshot {
    pub admitted: usize,
    pub queued: usize,
    pub running: usize,
}

fn held<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}
fn storage<T>() -> Result<u64, GateError> {
    (NativeRef::<T>::storage_bytes() as u64)
        .checked_add(SYNC_STORAGE_BYTES)
        .ok_or(GateError::Unavailable)
}
fn allocate<T>(value: T, funding: NativeSharedStateReservation) -> Result<NativeRef<T>, GateError> {
    NativeRef::try_new_precharged(value, funding).map_err(|_| GateError::Unavailable)
}

impl<T: Send + Sync + 'static> NativeJobs<T> {
    /// `maximum` only bounds admissions; it never raises the real account cap.
    /// Original worker/queue policy belongs to the adapter, not this mechanism.
    pub fn new(account: &NativeSetupAccount, maximum: usize) -> Result<Self, GateError> {
        if maximum == 0 {
            return Err(GateError::Limit("native job admission capacity".into()));
        }
        let funding =
            account.reserve_shared_state(CapacityKind::VfsBuffer, storage::<State<T>>()?)?;
        let ns_funding = account.reserve_shared_state(CapacityKind::VfsBuffer, storage::<()>()?)?;
        let namespace = allocate((), ns_funding)?;
        let state = State {
            process: std::process::id(),
            account: account.clone(),
            membership: funding.clone(),
            namespace,
            maximum,
            registry: Mutex::new(NativeRegistry::default()),
        };
        allocate(state, funding).map(Self)
    }
    pub fn new_owner(&self) -> Result<NativeJobOwner, GateError> {
        let state = self.0.get();
        let funding = state
            .account
            .reserve_shared_state(CapacityKind::VfsBuffer, storage::<Owner>()?)?;
        allocate(
            Owner {
                namespace: state.namespace.clone(),
                fenced: AtomicBool::new(false),
            },
            funding,
        )
        .map(NativeJobOwner)
    }
    fn check_owner(&self, owner: &NativeJobOwner) -> Result<(), GateError> {
        let state = self.0.get();
        if state.process != std::process::id() {
            return Err(GateError::Unavailable);
        }
        if !std::ptr::eq(state.namespace.get(), owner.0.get().namespace.get()) {
            return Err(GateError::Denied(
                "native job owner belongs to another namespace".into(),
            ));
        }
        Ok(())
    }
    pub fn submit(
        &self,
        owner: &NativeJobOwner,
        operation: &NativeSetupInvocation,
        wait_deadline: Instant,
    ) -> Result<(NativeJobFuture<T>, NativeJobTicket<T>), GateError> {
        self.check_owner(owner)?;
        let state = self.0.get();
        state.membership.check_account_for(operation)?;
        let deadline = wait_deadline.min(operation.deadline()?);
        let mut registry = held(&state.registry);
        // Fence and submit share this same lock. No old-owner job can escape
        // cancellation by being enrolled between a snapshot and the fence.
        if owner.0.get().fenced.load(Ordering::Acquire) {
            return Err(GateError::Cancelled);
        }
        if Instant::now() >= deadline {
            return Err(GateError::Deadline);
        }
        if registry.entries.len() >= state.maximum {
            return Err(GateError::Limit("native job admission capacity".into()));
        }
        let replacement = registry.prepare_slot(&state.account)?;
        let funding = state
            .account
            .reserve_shared_state(CapacityKind::VfsBuffer, storage::<Job<T>>()?)?;
        let value = Job {
            process: state.process,
            operation: operation.clone(),
            membership: funding.clone(),
            owner: owner.0.clone(),
            deadline,
            state: Mutex::new(JobState {
                phase: Phase::Queued,
                outcome: Outcome::Pending,
            }),
            changed: Condvar::new(),
            result: OnceLock::new(),
        };
        let job = allocate(value, funding)?;
        if Instant::now() >= operation.deadline()?.min(deadline) {
            return Err(GateError::Deadline);
        }
        registry.publish(job.clone(), replacement);
        Ok((
            NativeJobFuture(job.clone()),
            NativeJobTicket {
                coordinator: self.clone(),
                job,
            },
        ))
    }
    /// Mark this real owner fenced, then cancel only its pending Futures.
    /// Existing completed results remain completed; no admission is reclaimed.
    pub fn fence(&self, owner: &NativeJobOwner) -> Result<(), GateError> {
        self.check_owner(owner)?;
        let registry = held(&self.0.get().registry);
        owner.0.get().fenced.store(true, Ordering::Release);
        for job in &registry.entries {
            if std::ptr::eq(job.get().owner.get(), owner.0.get()) {
                cancel(job);
            }
        }
        Ok(())
    }
    pub fn snapshot(&self) -> Result<NativeJobsSnapshot, GateError> {
        if self.0.get().process != std::process::id() {
            return Err(GateError::Unavailable);
        }
        let registry = held(&self.0.get().registry);
        let mut result = NativeJobsSnapshot {
            admitted: registry.entries.len(),
            queued: 0,
            running: 0,
        };
        for job in &registry.entries {
            match held(&job.get().state).phase {
                Phase::Queued => result.queued += 1,
                Phase::Running => result.running += 1,
                Phase::Finished => {}
            }
        }
        Ok(result)
    }
}

fn cancel<T>(job: &JobRef<T>) -> bool {
    let mut state = held(&job.get().state);
    match state.outcome {
        Outcome::Pending => {
            state.outcome = Outcome::Cancelled;
            job.get().changed.notify_all();
            true
        }
        Outcome::Cancelled => true,
        Outcome::Ready | Outcome::Failed(_) => false,
    }
}
impl<T> NativeJobContext<'_, T> {
    fn prepare_start(&self) -> PreparedStart<'_, T> {
        PreparedStart {
            job: self.job,
            checkpoint: self.checkpoint(),
        }
    }
    pub fn operation(&self) -> &NativeSetupInvocation {
        &self.job.get().operation
    }
    pub fn checkpoint(&self) -> Result<(), NativeJobFailure> {
        let job = self.job.get();
        job.membership
            .check_account_for(&job.operation)
            .map_err(|_| NativeJobFailure::ScopeUnavailable)?;
        let deadline = job
            .operation
            .deadline()
            .map_err(|_| NativeJobFailure::ScopeUnavailable)?
            .min(job.deadline);
        if matches!(held(&job.state).outcome, Outcome::Cancelled) {
            return Err(NativeJobFailure::Cancelled);
        }
        if Instant::now() >= deadline {
            return Err(NativeJobFailure::Deadline);
        }
        Ok(())
    }
}
impl<T> NativeJobFuture<T> {
    pub fn cancel(&self) -> Result<bool, GateError> {
        // Reject inherited-process mutex access before touching the job state.
        if self.0.get().process != std::process::id() {
            return Err(GateError::Unavailable);
        }
        Ok(cancel(&self.0))
    }
    /// Future observation, not operation admission. A domain adapter must also
    /// preserve its original pre-wait deadline branch and typed error mapping.
    pub fn wait_until(&self, wait_deadline: Instant) -> Result<NativeJobObservation<T>, GateError> {
        let job = self.0.get();
        if job.process != std::process::id() {
            return Err(GateError::Unavailable);
        }
        // Observation/cancellation may outlive root revocation. They use the
        // already fixed deadline and issue no operation or effect admission.
        let deadline = wait_deadline.min(job.deadline);
        let mut state = held(&job.state);
        loop {
            match state.outcome {
                Outcome::Ready => {
                    return Ok(NativeJobObservation::Ready(NativeJobResult(self.0.clone())));
                }
                Outcome::Cancelled => return Ok(NativeJobObservation::Cancelled),
                Outcome::Failed(error) => return Ok(NativeJobObservation::Failed(error)),
                Outcome::Pending => {}
            }
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                state.outcome = Outcome::Cancelled;
                job.changed.notify_all();
                return Ok(NativeJobObservation::TimedOut);
            }
            let (next, _) = job
                .changed
                .wait_timeout(state, remaining)
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            state = next;
        }
    }
}
impl<T: Send + Sync + 'static> NativeJobTicket<T> {
    /// Execute only in a separately owned/funded worker. The callback port must
    /// finish and Join all its work before return AND unwind. T's nested heaps,
    /// closures/stacks and actual Pack Context need their own retained funding.
    /// Scalar fixtures prove this state machine, not those production duties.
    pub fn run(
        self,
        callback: impl FnOnce(&NativeJobContext<'_, T>) -> Result<T, NativeJobFailure>,
    ) {
        // Refuse before root validation or ANY job/registry mutex. Arbitrary
        // callback/T destructors after an unsupported non-exec fork are not
        // made safe by this guard and remain a production-owner obligation.
        if self.coordinator.0.get().process != std::process::id() {
            return;
        }
        let context = NativeJobContext { job: &self.job };
        // Queued cancellation and callback-start admission have ONE winner
        // under the same state lock, after genuine root/scope validation.
        if !context.prepare_start().enter() {
            // An uncalled port's captured cleanup also precedes Finish and
            // must never run under either state/registry lock.
            drop(callback);
            return;
        }
        let mut outcome = Some(callback(&context));
        // Native root validation occurs outside state/registry locks. Publish
        // and cancellation are then arbitrated under ONE actual state lock.
        let scope = context.operation().deadline();
        let mut discard = None;
        {
            let job = self.job.get();
            let mut state = held(&job.state);
            if matches!(state.outcome, Outcome::Pending) {
                match outcome.as_ref().expect("callback returned an outcome") {
                    Err(error) => state.outcome = Outcome::Failed(*error),
                    Ok(_) => match scope {
                        Ok(original) if Instant::now() < original.min(job.deadline) => {
                            let Some(Ok(value)) = outcome.take() else {
                                unreachable!()
                            };
                            match job.result.set(value) {
                                Ok(()) => state.outcome = Outcome::Ready,
                                Err(value) => {
                                    discard = Some(value);
                                    state.outcome =
                                        Outcome::Failed(NativeJobFailure::ScopeUnavailable);
                                }
                            }
                        }
                        Ok(_) => state.outcome = Outcome::Failed(NativeJobFailure::Deadline),
                        Err(_) => {
                            state.outcome = Outcome::Failed(NativeJobFailure::ScopeUnavailable)
                        }
                    },
                }
                job.changed.notify_all();
            }
        }
        // T destruction (including discarded results after a winning cancel)
        // must occur outside both state and registry locks, before Finish.
        drop(outcome);
        drop(discard);
        // self Drop runs only after the callback and its cleanup have ended.
    }
}
impl<T> Drop for NativeJobTicket<T> {
    fn drop(&mut self) {
        // A non-exec child cannot acquire a mutex inherited from vanished
        // threads. Releasing inherited custody never authorizes new work.
        if self.coordinator.0.get().process != std::process::id() {
            return;
        }
        let mut registry = held(&self.coordinator.0.get().registry);
        let mut state = held(&self.job.get().state);
        if matches!(state.outcome, Outcome::Pending) {
            state.outcome = match state.phase {
                Phase::Queued => Outcome::Cancelled, // explicit queue discard
                Phase::Running => Outcome::Failed(NativeJobFailure::CallbackPanicked),
                Phase::Finished => Outcome::Failed(NativeJobFailure::ScopeUnavailable),
            };
        }
        state.phase = Phase::Finished;
        let removed = registry.remove(&self.job);
        self.job.get().changed.notify_all();
        drop(state);
        drop(registry);
        drop(removed);
    }
}

#[cfg(all(test, target_os = "macos", target_arch = "aarch64"))]
#[path = "native_job_tests.rs"]
mod tests;
