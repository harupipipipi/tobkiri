//! Account-owned native state capacity; no execution or effect authority.
use super::*;

struct SharedPayload {
    account: NativeSetupAccount,
    id: u64,
    kind: CapacityKind,
}

/// Shared custody of one native state allocation in the original physical
/// account. It has no invocation provenance, clock, fuel, cancellation or Store
/// claim. Cloning retains one allocation; it does not fund a copy of its bytes.
#[derive(Clone)]
pub struct NativeSharedStateReservation(LeasedShared<SharedPayload, ReleasePlan>);

impl NativeSetupAccount {
    /// Prepay native shared state before allocating it. This reuses the same
    /// retained process/root proof, 64 MiB occupancy and control ledger; the
    /// caller cannot provide a limit, serialized owner or replacement domain.
    pub fn reserve_shared_state(
        &self,
        kind: CapacityKind,
        bytes: u64,
    ) -> Result<NativeSharedStateReservation, GateError> {
        self.check_live()?;
        let control = self
            .state
            .control
            .reserve(ControlKind::Frame, RECORD_METADATA_BYTES)?;
        if LeasedShared::<SharedPayload, ReleasePlan>::storage_bytes() as u64
            > RECORD_METADATA_BYTES
        {
            return Err(GateError::Limit(
                "native shared state reservation metadata layout".into(),
            ));
        }
        let id = self.ledger.next_native_setup_id()?;
        {
            let mut occupancy = self
                .state
                .occupancy
                .lock()
                .map_err(|_| GateError::Unavailable)?;
            if occupancy.invalid {
                return Err(GateError::Unavailable);
            }
            if occupancy.records.len() >= MAX_RECORDS {
                return Err(GateError::Limit("native Setup reservation count".into()));
            }
            let next = occupancy
                .used
                .checked_add(bytes)
                .filter(|used| *used <= NATIVE_SETUP_MEMORY_BYTES)
                .ok_or_else(|| {
                    GateError::Limit("aggregate native Setup capacity exceeded".into())
                })?;
            occupancy.records.insert(id, Record { allocated: bytes });
            occupancy.used = next;
            occupancy.peak = occupancy.peak.max(next);
        }
        let plan = ReleasePlan {
            account: Some(self.clone()),
            id,
            _control: control,
        };
        let payload = SharedPayload {
            account: self.clone(),
            id,
            kind,
        };
        LeasedShared::try_new_precharged(payload, plan)
            .map(NativeSharedStateReservation)
            .map_err(|_| GateError::Unavailable)
    }
}

impl NativeSharedStateReservation {
    /// Validate actual retained account membership. Allocation custody alone
    /// never admits an operation, verifies artifact bytes or grants native IO.
    pub fn check_account_for(&self, operation: &NativeSetupInvocation) -> Result<(), GateError> {
        let account = &self.0.get().account;
        let other = &operation.0.get().account;
        account.check_live()?;
        other.check_live()?;
        if !Arc::ptr_eq(&account.state, &other.state)
            || !Arc::ptr_eq(&account.ledger.0, &other.ledger.0)
        {
            return Err(GateError::Denied(
                "native shared state belongs to another actual account".into(),
            ));
        }
        Ok(())
    }

    pub fn kind(&self) -> CapacityKind {
        self.0.get().kind
    }

    /// Reserve growth first; shrink only after deallocation or failed growth.
    /// Resizing capacity does not renew any operation or its execution budget.
    pub fn resize(&self, bytes: u64) -> Result<(), GateError> {
        let value = self.0.get();
        value.account.check_live()?;
        let mut occupancy = value
            .account
            .state
            .occupancy
            .lock()
            .map_err(|_| GateError::Unavailable)?;
        if occupancy.invalid {
            return Err(GateError::Unavailable);
        }
        let previous = occupancy
            .records
            .get(&value.id)
            .ok_or(GateError::Unavailable)?
            .allocated;
        let next = occupancy
            .used
            .checked_sub(previous)
            .and_then(|used| used.checked_add(bytes))
            .filter(|used| *used <= NATIVE_SETUP_MEMORY_BYTES)
            .ok_or_else(|| GateError::Limit("aggregate native Setup capacity exceeded".into()))?;
        occupancy
            .records
            .get_mut(&value.id)
            .ok_or(GateError::Unavailable)?
            .allocated = bytes;
        occupancy.used = next;
        occupancy.peak = occupancy.peak.max(next);
        Ok(())
    }

    pub fn allocated_bytes(&self) -> Result<u64, GateError> {
        let value = self.0.get();
        value.account.check_live()?;
        let occupancy = value
            .account
            .state
            .occupancy
            .lock()
            .map_err(|_| GateError::Unavailable)?;
        if occupancy.invalid {
            return Err(GateError::Unavailable);
        }
        occupancy
            .records
            .get(&value.id)
            .map(|record| record.allocated)
            .ok_or(GateError::Unavailable)
    }
}
