//! Native DATA codec allocations, funded by the original shared root account.
use std::{sync::Mutex, time::Instant};
use tobkiri_canon::{AllocationBudget, AllocationError, AllocationRequest, MAX_DEPTH};
use tobkiri_gate::{CoreCancellation, GateError, NativeSetupReservation};
use wasmtime::component::{HostAllocationBudget, HostAllocationError, HostAllocationRequest};

struct State {
    live_bytes: u64,
    work_remaining: u64,
    invalid: bool,
}
pub(crate) struct NativeAllocationBudget {
    process: u32,
    capacity: NativeSetupReservation,
    deadline: Instant,
    cancel: CoreCancellation,
    retirement: tokio_util::sync::CancellationToken,
    state: Mutex<State>,
}
impl crate::allocation::CustodyOwner for NativeAllocationBudget {}
impl NativeAllocationBudget {
    pub(crate) fn new(
        capacity: NativeSetupReservation,
        deadline: Instant,
        cancel: CoreCancellation,
        retirement: tokio_util::sync::CancellationToken,
        work: u64,
    ) -> Self {
        Self {
            process: std::process::id(),
            capacity,
            deadline,
            cancel,
            retirement,
            state: Mutex::new(State {
                live_bytes: 0,
                work_remaining: work,
                invalid: false,
            }),
        }
    }
    fn active(&self) -> Result<(), AllocationError> {
        if self.process != std::process::id()
            || Instant::now() >= self.deadline
            || self.cancel.is_cancelled()
            || self.retirement.is_cancelled()
        {
            return Err(AllocationError::Unavailable);
        }
        Ok(())
    }
    fn reserve_checked(&self, request: AllocationRequest) -> Result<(), AllocationError> {
        self.active()?;
        // Check the real root/PID before taking this adapter's local mutex.
        self.capacity
            .allocated_bytes()
            .map_err(|_| AllocationError::Unavailable)?;
        if request.depth as usize > MAX_DEPTH {
            return Err(AllocationError::Depth);
        }
        let mut state = self
            .state
            .lock()
            .map_err(|_| AllocationError::Unavailable)?;
        if state.invalid {
            return Err(AllocationError::Unavailable);
        }
        let work_remaining = state
            .work_remaining
            .checked_sub(request.work.max(request.elements))
            .ok_or(AllocationError::Limit)?;
        let live_bytes = state
            .live_bytes
            .checked_add(request.bytes)
            .ok_or(AllocationError::Overflow)?;
        // Recheck after mutex waiting, before actual physical admission.
        self.active()?;
        self.capacity
            .resize(live_bytes)
            .map_err(|error| match error {
                GateError::Limit(_) => AllocationError::Limit,
                _ => AllocationError::Unavailable,
            })?;
        state.live_bytes = live_bytes;
        state.work_remaining = work_remaining;
        Ok(())
    }
    fn release_known(&self, bytes: u64) {
        // Expiry/cancellation stop admission, never accounting cleanup.
        if self.process != std::process::id() || self.capacity.allocated_bytes().is_err() {
            return;
        }
        let mut state = self.state.lock().unwrap_or_else(|error| error.into_inner());
        let Some(live_bytes) = state.live_bytes.checked_sub(bytes) else {
            state.invalid = true;
            return;
        };
        if self.capacity.resize(live_bytes).is_err() {
            state.invalid = true;
            return;
        }
        state.live_bytes = live_bytes;
    }
}
impl AllocationBudget for NativeAllocationBudget {
    fn reserve(&self, request: AllocationRequest) -> Result<(), AllocationError> {
        self.reserve_checked(request)
    }
    fn release(&self, bytes: u64) {
        self.release_known(bytes);
    }
}
impl HostAllocationBudget for NativeAllocationBudget {
    fn reserve(&self, request: HostAllocationRequest) -> Result<(), HostAllocationError> {
        self.reserve_checked(AllocationRequest {
            kind: tobkiri_canon::AllocationKind::Temporary,
            bytes: request.bytes,
            elements: request.elements,
            depth: request.depth,
            work: request.work,
        })
        .map_err(|error| match error {
            AllocationError::Unavailable => HostAllocationError::Unavailable,
            AllocationError::Limit => HostAllocationError::Limit,
            AllocationError::Overflow => HostAllocationError::Overflow,
            AllocationError::Depth => HostAllocationError::Depth,
            AllocationError::Allocator => HostAllocationError::Allocator,
            AllocationError::Invalid => HostAllocationError::Invalid,
        })
    }
    fn release(&self, bytes: u64) {
        self.release_known(bytes);
    }
}
