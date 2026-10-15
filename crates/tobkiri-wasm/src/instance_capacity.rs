//! Stable-owner occupancy for live/warm component linear memory and tables.
//! The outer PackInstance retains this reservation AFTER dropping its Store.
use crate::host::Shared;
use anyhow::{Context, Result, bail};
use std::sync::{Arc, Mutex};
use tobkiri_gate::{
    AllocationFrameScope, CallCx, CapacityKind, CapacityLedger, CapacityOwner, CapacityProvenance,
    CapacityReservation, Gate, GateError,
};

/// Fixed Wasm stack bound selected by WasmEngine. Other fixed native Core
/// metadata is bounded separately and is not hidden in guest memory funding.
pub(crate) const WASM_STACK_BYTES: u64 = 512 * 1024;
struct Entry {
    cx: CallCx,
    scope: AllocationFrameScope,
}
struct State {
    charged: u64,
    entry: Option<Entry>,
}
pub(crate) struct InstanceCapacity {
    gate: Arc<dyn Gate>,
    ledger: CapacityLedger,
    owner: CapacityOwner,
    reservation: CapacityReservation,
    state: Mutex<State>,
}
impl InstanceCapacity {
    pub(crate) fn new(
        shared: &Shared,
        cx: &CallCx,
        scope: AllocationFrameScope,
    ) -> Result<Arc<Self>> {
        shared.capacity.check_domain()?;
        let owner = scope.capacity_owner();
        let (pack, hash) = cx
            .principal()
            .pack()
            .context("component physical owner must be verified Pack")?;
        if owner != CapacityOwner::pack_owner(cx.profile().clone(), pack.clone())
            || scope.pack_hash() != Some(hash)
        {
            bail!("component physical owner differs from actual provider frame")
        }
        let binding = shared
            .capacity
            .bind_owner(Arc::new(shared.root.owner_lease(cx.profile(), pack)?))?;
        let provenance = CapacityProvenance {
            hash: Some(hash.clone()),
            revision: scope.revision().clone(),
            security_epoch: scope.security_epoch(),
            request: cx.request().clone(),
            store: None,
            session: Some(scope.frame().clone()),
        };
        let mut reservation = None;
        shared
            .gate
            .with_frame_allocation(cx, &scope, &mut |limit| {
                if reservation.is_some() {
                    return Err(GateError::Unavailable);
                }
                reservation = Some(shared.capacity.reserve(
                    owner.clone(),
                    provenance.clone(),
                    CapacityKind::GuestLinear,
                    WASM_STACK_BYTES,
                    limit,
                )?);
                Ok(())
            })?;
        drop(binding);
        Ok(Arc::new(Self {
            gate: shared.gate.clone(),
            ledger: shared.capacity.clone(),
            owner,
            reservation: reservation.context("component physical admission missing")?,
            state: Mutex::new(State {
                charged: WASM_STACK_BYTES,
                entry: Some(Entry {
                    cx: cx.clone(),
                    scope,
                }),
            }),
        }))
    }
    /// Only a real reached provider activation may admit growth. Replacing the
    /// scope never releases warm bytes or changes the stable funding account.
    pub(crate) fn enter(&self, cx: &CallCx, scope: AllocationFrameScope) -> Result<()> {
        if scope.capacity_owner() != self.owner {
            bail!("warm component owner changed")
        }
        let mut state = self.state.lock().map_err(|_| GateError::Unavailable)?;
        self.gate.with_frame_allocation(cx, &scope, &mut |limit| {
            // Equal-size resize is also a fresh admission. A reduced current
            // approval must not admit an already oversized warm instance.
            self.ledger.check_limit(&self.owner, limit)?;
            self.reservation.resize(state.charged, limit)
        })?;
        state.entry = Some(Entry {
            cx: cx.clone(),
            scope,
        });
        Ok(())
    }
    pub(crate) fn leave(&self) {
        self.state
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .entry
            .take();
    }
    fn admit(&self, bytes: u64) -> Result<u64, GateError> {
        let mut state = self.state.lock().map_err(|_| GateError::Unavailable)?;
        let next = state
            .charged
            .checked_add(bytes)
            .ok_or_else(|| GateError::Limit("component physical occupancy overflow".into()))?;
        let entry = state.entry.as_ref().ok_or(GateError::Unavailable)?;
        self.gate
            .with_frame_allocation(&entry.cx, &entry.scope, &mut |limit| {
                self.reservation.resize(next, limit)
            })?;
        state.charged = next;
        Ok(bytes)
    }
    pub(crate) fn memory_growth(&self, current: usize, desired: usize) -> Result<u64, GateError> {
        // The stable account tracks accessible guest bytes across all stores.
        // Allocator move/virtual-address transients are bounded runtime work,
        // not a second logical guest-memory allocation or a whole-RSS claim.
        let growth = desired
            .checked_sub(current)
            .and_then(|n| u64::try_from(n).ok())
            .ok_or_else(|| GateError::Limit("memory growth size overflow".into()))?;
        self.admit(growth)
    }
    pub(crate) fn table_growth(&self, current: usize, desired: usize) -> Result<u64, GateError> {
        // Supported tables are funcref only (GC/continuations disabled). Pinned
        // VM table elements are pointer-sized; TryVec may double its capacity.
        // Reserve a conservative per-entry allowance for newly reachable
        // entries; successful growth stays charged until actual Store drop.
        let bytes = desired
            .checked_sub(current)
            .and_then(|n| u64::try_from(n).ok())
            .and_then(|n| n.checked_mul(size_of::<usize>() as u64))
            .and_then(|n| n.checked_mul(2))
            .ok_or_else(|| GateError::Limit("table capacity overflow".into()))?;
        self.admit(bytes)
    }
    /// Called only AFTER pinned Wasmtime reports an allocation/grow failure;
    /// newly allocated temporaries have already been destroyed by that path.
    pub(crate) fn failed_growth(&self, bytes: u64) -> Result<(), GateError> {
        let mut state = self.state.lock().map_err(|_| GateError::Unavailable)?;
        let next = state
            .charged
            .checked_sub(bytes)
            .ok_or(GateError::Unavailable)?;
        self.reservation.resize(next, 0)?;
        state.charged = next;
        Ok(())
    }
}
