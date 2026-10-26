//! Inactive native Setup Store mechanism; no Profile, Pack or host IO authority.
use crate::WasmEngine;
use crate::aggregate_limits::{AggregateLimits, PhysicalCapacity};
use crate::engine::STORE_STRUCTURE_CONTROL_BYTES;
use crate::execution_epoch::check_epoch;
use crate::instance_capacity::WASM_STACK_BYTES;
use anyhow::{Result, bail};
use std::{
    sync::{Arc, Mutex},
    time::Instant,
};
use tobkiri_gate::{
    CapacityKind, ControlKind, ControlReservation, CoreCancellation, GateError,
    NATIVE_SETUP_MEMORY_BYTES, NativeSetupInvocation, NativeSetupReservation,
};
use wasmtime::Store;

const FUEL: u64 = 2_000_000_000;
const TABLE_ELEMENTS: usize = 65_536;
const PHYSICAL_METADATA_BYTES: u64 = 1024;

#[cfg(test)]
#[path = "native_setup_store_tests.rs"]
mod tests;

#[path = "native_setup_entry.rs"]
mod entry;
pub use entry::{
    NativeDataBinding, NativeDataEntry, NativeDataError, NativeDataErrorContract, NativeDataExport,
    NativeDataObservation, NativeDataProgram, NativeDataResultContract, NativeDataResultEntry,
    NativeDataSuccessType, NativeDataValue,
};

struct NativePhysical {
    reservation: NativeSetupReservation,
    charged: Mutex<u64>,
}
impl NativePhysical {
    fn change(&self, bytes: u64, grow: bool) -> Result<u64, GateError> {
        // Reject inherited native accounts BEFORE this object's mutex.
        self.reservation.allocated_bytes()?;
        let mut charged = self.charged.lock().map_err(|_| GateError::Unavailable)?;
        let next = if grow {
            charged.checked_add(bytes)
        } else {
            charged.checked_sub(bytes)
        }
        .ok_or(GateError::Unavailable)?;
        self.reservation.resize(next)?;
        *charged = next;
        Ok(bytes)
    }
}
impl PhysicalCapacity for NativePhysical {
    fn memory_growth(&self, current: usize, desired: usize) -> Result<u64, GateError> {
        let bytes = desired
            .checked_sub(current)
            .and_then(|v| u64::try_from(v).ok())
            .ok_or_else(|| GateError::Limit("native Setup memory growth overflow".into()))?;
        self.change(bytes, true)
    }
    fn table_growth(&self, current: usize, desired: usize) -> Result<u64, GateError> {
        let bytes = desired
            .checked_sub(current)
            .and_then(|v| u64::try_from(v).ok())
            .and_then(|v| v.checked_mul(size_of::<usize>() as u64))
            .and_then(|v| v.checked_mul(2))
            .ok_or_else(|| GateError::Limit("native Setup table growth overflow".into()))?;
        self.change(bytes, true)
    }
    fn failed_growth(&self, bytes: u64) -> Result<(), GateError> {
        self.change(bytes, false).map(|_| ())
    }
}

struct PhysicalOwner {
    physical: Option<Arc<NativePhysical>>,
    control: Option<ControlReservation>,
}
impl PhysicalOwner {
    fn new(invocation: &NativeSetupInvocation) -> Result<Self> {
        // This also validates process/root before any local admission mutex.
        let reservation = invocation.reserve(CapacityKind::GuestLinear, WASM_STACK_BYTES)?;
        let control = invocation
            .control()
            .reserve(ControlKind::Frame, PHYSICAL_METADATA_BYTES)?;
        let physical = Arc::new(NativePhysical {
            reservation,
            charged: Mutex::new(WASM_STACK_BYTES),
        });
        Ok(Self {
            physical: Some(physical),
            control: Some(control),
        })
    }
    fn physical(&self) -> Arc<NativePhysical> {
        self.physical.as_ref().expect("live physical owner").clone()
    }
}
impl Drop for PhysicalOwner {
    fn drop(&mut self) {
        // The outer owner drops only AFTER Store/native allocation destruction.
        // No Weak handle to this private allocation is exposed.
        drop(self.physical.take());
        drop(self.control.take());
    }
}

struct NativeState<T> {
    data: T,
    invocation: NativeSetupInvocation,
    owners: crate::AllocationOwners,
    captured: Option<crate::NativeCapturedInputs>,
    capture_claimed: bool,
    clock_observation: NativeDataObservation,
    limits: AggregateLimits,
    deadline: Instant,
    cancel: CoreCancellation,
    cancel_observed: Option<Instant>,
    retirement: tokio_util::sync::CancellationToken,
}

/// Native-only prototype Store custody. It exposes no linker/import surface or
/// raw Store extraction. Codecs share one retained allocation owner; capture,
/// entry IO and the actual UI runner still need connection. Construction does
/// not authorize an effect.
pub struct NativeSetupStore<T: 'static> {
    store: Option<Store<NativeState<T>>>,
    physical: Option<PhysicalOwner>,
    // Keep the original bounded ticker alive until AFTER actual Store drop.
    _engine: WasmEngine,
}
impl WasmEngine {
    /// Create one bounded native UI Store without a fabricated CallCx/Profile.
    /// Fuel and the original invocation deadline are never renewed between calls.
    pub fn native_setup_store<T: Send + 'static>(
        &self,
        invocation: NativeSetupInvocation,
        data: T,
    ) -> Result<NativeSetupStore<T>> {
        let deadline = invocation.deadline()?;
        if Instant::now() >= deadline {
            bail!("original native Setup deadline exceeded");
        }
        if !self.shares_control(invocation.control()) {
            bail!("native Setup engine uses another control account");
        }
        invocation.claim_store()?;
        let physical = PhysicalOwner::new(&invocation)?;
        let structure_control = invocation
            .control()
            .reserve(ControlKind::Frame, STORE_STRUCTURE_CONTROL_BYTES)?;
        let cancel = CoreCancellation::new_with_control(invocation.control())?;
        let retirement = tokio_util::sync::CancellationToken::new();
        let owners =
            crate::allocation::native_owners(&invocation, deadline, &cancel, &retirement, FUEL)?;
        let state = NativeState {
            data,
            invocation,
            owners,
            captured: None,
            capture_claimed: false,
            clock_observation: NativeDataObservation::default(),
            limits: AggregateLimits {
                memory_cap: usize::try_from(NATIVE_SETUP_MEMORY_BYTES)?,
                table_cap: TABLE_ELEMENTS,
                memory_used: 0,
                table_used: 0,
                last_memory_growth: 0,
                last_table_growth: 0,
                physical: Some(physical.physical()),
                last_physical_memory: 0,
                last_physical_table: 0,
            },
            deadline,
            cancel,
            cancel_observed: None,
            retirement,
        };
        let mut store = Store::new(self.engine(), state);
        let retained = physical.physical();
        store.limiter(move |state| {
            // Pinned Wasmtime drops this closure AFTER native StoreOpaque.
            let _ = (&structure_control, &retained);
            &mut state.limits
        });
        store.set_fuel(FUEL)?;
        store.fuel_async_yield_interval(Some(10_000))?;
        store.set_epoch_deadline(1);
        store.epoch_deadline_callback(|mut store| {
            let state = store.data_mut();
            check_epoch(
                state.deadline,
                &state.cancel,
                Some(&state.retirement),
                &mut state.cancel_observed,
            )
        });
        Ok(NativeSetupStore {
            store: Some(store),
            physical: Some(physical),
            _engine: self.clone(),
        })
    }
}
impl<T: 'static> NativeSetupStore<T> {
    /// Seal one fixed ordinal inventory before entry execution. Failure also
    /// consumes this binding attempt; neither files nor funding can be replaced.
    pub fn seal_captured_inputs(
        &mut self,
        files: &[crate::NativeCapturedFile<'_>],
    ) -> Result<crate::NativeCapturedInputs> {
        let state = self.store.as_mut().expect("live native Store").data_mut();
        if state.capture_claimed {
            bail!("native capture inventory already claimed");
        }
        state.capture_claimed = true;
        let capture = crate::NativeCapturedInputs::seal(&state.invocation, &state.owners, files)?;
        state.captured = Some(capture.clone());
        Ok(capture)
    }
    pub fn captured_inputs(&self) -> Option<&crate::NativeCapturedInputs> {
        self.store
            .as_ref()
            .expect("live native Store")
            .data()
            .captured
            .as_ref()
    }
    /// Freeze native-selected source DATA using the original account, deadline
    /// and host-work budget. Call this synchronous IO primitive on a funded
    /// managed worker; cancellation does not complete an in-flight OS operation.
    pub fn capture_file_snapshot(
        &self,
        root: &tobkiri_store::NativeSetupOwnerLease,
        source: tobkiri_store::NativeSnapshotSource<'_>,
    ) -> Result<tobkiri_store::NativeFileSnapshot> {
        let state = self.store.as_ref().expect("live native Store").data();
        let mut check_work = |work| {
            state
                .owners
                .graph()
                .reserve(tobkiri_canon::AllocationRequest {
                    kind: tobkiri_canon::AllocationKind::Temporary,
                    bytes: 0,
                    elements: 0,
                    depth: 0,
                    work,
                })
                .map_err(|error| match error {
                    tobkiri_canon::AllocationError::Limit => {
                        GateError::Limit("native capture host work exceeded".into())
                    }
                    _ => GateError::Unavailable,
                })
        };
        Ok(root.capture_file_snapshot(&state.invocation, source, &state.cancel, &mut check_work)?)
    }

    /// Borrow the single original codec owner; clones share its work/capacity.
    pub fn allocation_owners(&self) -> &crate::AllocationOwners {
        &self
            .store
            .as_ref()
            .expect("live native Store")
            .data()
            .owners
    }
    pub fn data(&self) -> &T {
        &self.store.as_ref().expect("live native Store").data().data
    }
    pub fn original_deadline(&self) -> Instant {
        self.store
            .as_ref()
            .expect("live native Store")
            .data()
            .deadline
    }
    pub fn fuel_remaining(&self) -> Result<u64> {
        Ok(self.store.as_ref().expect("live native Store").get_fuel()?)
    }
    pub fn cancellation(&self) -> &CoreCancellation {
        &self
            .store
            .as_ref()
            .expect("live native Store")
            .data()
            .cancel
    }
    /// Begin immediate retirement; physical capacity remains until actual drop.
    pub fn retire(&self) {
        let state = self.store.as_ref().expect("live native Store").data();
        state.retirement.cancel();
        state.cancel.cancel();
    }
}
impl<T: 'static> Drop for NativeSetupStore<T> {
    fn drop(&mut self) {
        self.retire();
        drop(self.store.take());
        drop(self.physical.take());
        let _ = crate::allocation::reap_custody();
    }
}
