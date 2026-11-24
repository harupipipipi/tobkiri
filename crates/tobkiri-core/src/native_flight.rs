//! Native in-progress coordination, without result sharing or effect authority.
//!
//! This opt-in candidate is not a production realm binding or Pack adapter.
//! Keys are complete opaque bytes, never interpreted or replaced by digests.
use crate::native_state_registry::NativeRegistry;
use std::sync::{Condvar, Mutex, MutexGuard};
use std::time::Instant;
use tobkiri_canon::LeasedShared;
use tobkiri_gate::{
    CapacityKind, GateError, NativeSetupAccount, NativeSetupInvocation,
    NativeSharedStateReservation,
};

// Covers supported native mutex/condition storage in addition to the exact
// leased block. It is prepaid in the SAME payload account, not a new allowance.
const SYNC_STORAGE_BYTES: u64 = 1024;
type StateRef = LeasedShared<State, NativeSharedStateReservation>;
type FlightRef = LeasedShared<Flight, NativeSharedStateReservation>;

/// An explicit candidate namespace. Matching bytes in another coordinator do
/// not join; mapping this identity to the original interpreter realm is TODO.
#[derive(Clone)]
pub struct NativeFlights(StateRef);

struct State {
    account: NativeSetupAccount,
    membership: NativeSharedStateReservation,
    registry: Mutex<Registry>,
}
type Registry = NativeRegistry<Flight>;
struct Flight {
    key: Vec<u8>,
    state: Mutex<FlightState>,
    completed: Condvar,
}
#[derive(Default)]
struct FlightState {
    completed: bool,
    // Original accounting: timeout/abandonment leaves; notification does not
    // decrement successful waiters. This is not a storage reference count.
    waiters: u64,
}

/// Only the leader receives its own callback outcome. Waiters receive a
/// completion observation, never the outcome, error object or a success flag.
pub enum NativeFlightJoin<T> {
    Leader(T),
    Waiter(NativeFlightWaiter),
}

/// One enrolled waiter, retaining the original operation and physical flight.
pub struct NativeFlightWaiter {
    flight: FlightRef,
    operation: NativeSetupInvocation,
    counted: bool,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum NativeFlightObservation {
    Completed,
    TimedOut,
}

// Poison recovery only permits internal custody cleanup/notification. It does
// not authorize work, conceal the callback panic or attest successful work.
fn held<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}

impl NativeFlights {
    pub fn new(account: &NativeSetupAccount) -> Result<Self, GateError> {
        let bytes = (StateRef::storage_bytes() as u64)
            .checked_add(SYNC_STORAGE_BYTES)
            .ok_or(GateError::Unavailable)?;
        let funding = account.reserve_shared_state(CapacityKind::VfsBuffer, bytes)?;
        let state = State {
            account: account.clone(),
            membership: funding.clone(),
            registry: Mutex::new(Registry::default()),
        };
        StateRef::try_new_precharged(state, funding)
            .map(Self)
            .map_err(|_| GateError::Unavailable)
    }

    /// Run synchronous owned work, or enroll behind work already in progress.
    /// The registry lock is released before either callback execution or wait.
    ///
    /// Trusted callback contract: on BOTH return and unwind, every owned native
    /// worker must have physically ended and its necessary Join completed before
    /// the callback scope ends. Synchronous work and scoped threads satisfy this;
    /// detached work does not. The callback must use the same original operation
    /// scope/deadline at each effect boundary. This helper grants no such rights.
    /// There is no exposed completion setter or cancellation-to-completion path.
    pub fn run_or_join<T>(
        &self,
        operation: &NativeSetupInvocation,
        key: &[u8],
        work: impl FnOnce() -> T,
    ) -> Result<NativeFlightJoin<T>, GateError> {
        let state = self.0.get();
        state.membership.check_account_for(operation)?;
        if Instant::now() >= operation.deadline()? {
            return Err(GateError::Deadline);
        }
        let mut registry = held(&state.registry);
        // Contention/allocation time remains inside the SAME original clock.
        if Instant::now() >= operation.deadline()? {
            return Err(GateError::Deadline);
        }
        if let Some(flight) = registry.entries.iter().find(|f| f.get().key == key) {
            let mut status = held(&flight.get().state);
            status.waiters = status
                .waiters
                .checked_add(1)
                .ok_or_else(|| GateError::Limit("native flight waiter count".into()))?;
            return Ok(NativeFlightJoin::Waiter(NativeFlightWaiter {
                flight: flight.clone(),
                operation: operation.clone(),
                counted: true,
            }));
        }
        // Construct ALL pending allocations before publishing any registration.
        // A failed admission leaves the existing vector/records unchanged.
        let pending_registry = registry.prepare_slot(&state.account)?;
        let flight = new_flight(&state.account, key)?;
        if Instant::now() >= operation.deadline()? {
            return Err(GateError::Deadline);
        }
        registry.publish(flight.clone(), pending_registry);
        let completion = WorkCompletion {
            coordinator: self.clone(),
            flight,
            _operation: operation.clone(),
        };
        drop(registry);
        let outcome = work();
        // The actual callback return/unwind is the only completion boundary.
        drop(completion);
        Ok(NativeFlightJoin::Leader(outcome))
    }
}

fn new_flight(account: &NativeSetupAccount, key: &[u8]) -> Result<FlightRef, GateError> {
    let bytes = (FlightRef::storage_bytes() as u64)
        .checked_add(SYNC_STORAGE_BYTES)
        .and_then(|n| n.checked_add(u64::try_from(key.len()).ok()?))
        .ok_or(GateError::Unavailable)?;
    let funding = account.reserve_shared_state(CapacityKind::VfsBuffer, bytes)?;
    let mut owned_key = Vec::new();
    owned_key
        .try_reserve_exact(key.len())
        .map_err(|_| GateError::Unavailable)?;
    if owned_key.capacity() != key.len() {
        return Err(GateError::Unavailable);
    }
    owned_key.extend_from_slice(key);
    let flight = Flight {
        key: owned_key,
        state: Mutex::new(FlightState::default()),
        completed: Condvar::new(),
    };
    FlightRef::try_new_precharged(flight, funding).map_err(|_| GateError::Unavailable)
}

struct WorkCompletion {
    coordinator: NativeFlights,
    flight: FlightRef,
    _operation: NativeSetupInvocation,
}
impl Drop for WorkCompletion {
    fn drop(&mut self) {
        let mut registry = held(&self.coordinator.0.get().registry);
        // Physical object identity prevents an old flight removing a new one.
        let removed = registry.remove(&self.flight);
        drop(registry);
        drop(removed);
        let mut status = held(&self.flight.get().state);
        status.completed = true;
        self.flight.get().completed.notify_all();
    }
}

impl NativeFlightWaiter {
    /// This wait upper bound can only shorten the retained operation's original
    /// absolute deadline. Timeout never removes or finishes native work.
    pub fn wait_until(
        mut self,
        wait_deadline: Instant,
    ) -> Result<NativeFlightObservation, GateError> {
        let deadline = wait_deadline.min(self.operation.deadline()?);
        let mut status = held(&self.flight.get().state);
        loop {
            if status.completed {
                self.counted = false;
                return Ok(NativeFlightObservation::Completed);
            }
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                status.waiters -= 1;
                self.counted = false;
                return Ok(NativeFlightObservation::TimedOut);
            }
            let (next, _) = self
                .flight
                .get()
                .completed
                .wait_timeout(status, remaining)
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            status = next;
        }
    }
}
impl Drop for NativeFlightWaiter {
    fn drop(&mut self) {
        if self.counted {
            // One non-cloneable handle owns this enrollment. Successful waits
            // disarm it without altering the original notification counter.
            held(&self.flight.get().state).waiters -= 1;
            self.counted = false;
        }
    }
}

#[cfg(all(test, target_os = "macos", target_arch = "aarch64"))]
#[path = "native_flight_tests.rs"]
mod tests;
