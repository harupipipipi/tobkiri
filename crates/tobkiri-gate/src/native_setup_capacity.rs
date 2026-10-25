//! Fixed native Setup DATA funding, separate from every Pack authority path.
use super::{CapacityKind, CapacityLedger, GateError, LeasedShared};
use crate::{ControlKind, ControlReservation, CoreControl};
use std::{collections::BTreeMap, sync::Arc};
use tobkiri_canon::Digest;

#[cfg(test)]
#[path = "native_setup_capacity_tests.rs"]
mod tests;

/// Original native UI payload cap, shared by workers and retained outputs.
pub const NATIVE_SETUP_MEMORY_BYTES: u64 = 64 * 1024 * 1024;
const MAX_RECORDS: usize = 4096;
const ACCOUNT_METADATA_BYTES: u64 = 4096;
const RECORD_METADATA_BYTES: u64 = 4096;
const INVOCATION_METADATA_BYTES: u64 = 1024;
const MAX_NATIVE_LEDGERS: usize = 4096;
const CARRIER_BYTES_PER_LEDGER: u64 = 1024;

// The permit holder is itself native bookkeeping. Its Arc allocation must not
// become unfunded between dropping the permit payload and freeing the block.
// Prepay this bounded carrier tier through actual process-registry lifetime.
struct CarrierRegistry {
    ledgers: BTreeMap<usize, super::WeakCapacityLedger>,
    _funding: ControlReservation,
}
static CARRIERS: std::sync::Mutex<Option<CarrierRegistry>> = std::sync::Mutex::new(None);
static CARRIER_PROCESS: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(0);

fn admit_metadata_carrier(ledger: &CapacityLedger, control: &CoreControl) -> Result<(), GateError> {
    let process = std::process::id();
    if let Err(original) = CARRIER_PROCESS.compare_exchange(
        0,
        process,
        std::sync::atomic::Ordering::AcqRel,
        std::sync::atomic::Ordering::Acquire,
    ) && original != process
    {
        return Err(GateError::Unavailable);
    }
    let mut carriers = CARRIERS.lock().map_err(|_| GateError::Unavailable)?;
    if carriers.is_none() {
        let funding = control.reserve(
            ControlKind::Registration,
            MAX_NATIVE_LEDGERS as u64 * CARRIER_BYTES_PER_LEDGER,
        )?;
        *carriers = Some(CarrierRegistry {
            ledgers: BTreeMap::new(),
            _funding: funding,
        });
    }
    let registry = carriers.as_mut().ok_or(GateError::Unavailable)?;
    registry
        .ledgers
        .retain(|_, held| held.inner.strong_count() != 0);
    // A retained Weak pins this exact allocation, preventing pointer reuse.
    let key = Arc::as_ptr(&ledger.0) as usize;
    if !registry.ledgers.contains_key(&key) {
        if registry.ledgers.len() >= MAX_NATIVE_LEDGERS {
            return Err(GateError::Limit(
                "native Setup ledger registry capacity".into(),
            ));
        }
        registry.ledgers.insert(key, ledger.downgrade());
    }
    Ok(())
}

/// Trusted native adapter contract, never accepted from Wasm, WIT or IPC.
/// Implementations must retain the actual exclusive, descriptor-pinned root
/// lock, reject inherited processes, and return one stable physical domain.
/// This public Rust trait does not by itself authenticate an implementer.
pub trait NativeSetupLease: Send + Sync {
    fn validate(&self) -> Result<(), GateError>;
    fn allocation_domain(&self) -> Digest;
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct NativeSetupProvenance {
    pub account_generation: u64,
    pub invocation: u64,
}

struct Record {
    allocated: u64,
}
#[derive(Default)]
struct Occupancy {
    used: u64,
    records: BTreeMap<u64, Record>,
    invalid: bool,
}
pub(super) struct State {
    generation: u64,
    domain: Digest,
    proof: Arc<dyn NativeSetupLease>,
    control: CoreControl,
    occupancy: std::sync::Mutex<Occupancy>,
}

/// Fixed-purpose physical account. It has no Profile, principal or Gate API.
/// All handles retain the SAME ledger, real root proof and original control.
#[derive(Clone)]
pub struct NativeSetupAccount {
    ledger: CapacityLedger,
    state: Arc<State>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct NativeSetupSnapshot {
    pub account_generation: u64,
    pub limit_bytes: u64,
    pub charged_bytes: u64,
    pub reservations: usize,
}

impl CapacityLedger {
    /// Trusted Core-only bridge for its native Setup DATA worker. The proof is
    /// acquired by the native broker; no guest-selected proof/owner/cap/domain
    /// or synthetic Profile/revision/request is accepted. Existing Pack reserve
    /// and Gate frame APIs are deliberately separate and remain unchanged.
    pub fn native_setup_account(
        &self,
        control: &CoreControl,
        acquire: impl FnOnce() -> Result<Arc<dyn NativeSetupLease>, GateError>,
    ) -> Result<NativeSetupAccount, GateError> {
        self.validate_process()?;
        // Do native broker work before taking an accounting mutex.
        let proof = acquire()?;
        proof.validate()?;
        let domain = proof.allocation_domain();
        admit_metadata_carrier(self, control)?;
        let mut slot = self
            .0
            .native_setup
            .lock()
            .map_err(|_| GateError::Unavailable)?;
        let mut ledger_domain = self.0.domain.lock().map_err(|_| GateError::Unavailable)?;
        if ledger_domain.as_ref().is_some_and(|held| held != &domain) {
            return Err(GateError::Denied(
                "native Setup belongs to another physical root".into(),
            ));
        }
        if let Some(state) = slot.as_ref() {
            if state.domain != domain {
                return Err(GateError::Denied(
                    "native Setup account domain changed".into(),
                ));
            }
            let account = NativeSetupAccount {
                ledger: self.clone(),
                state: state.clone(),
            };
            drop(ledger_domain);
            drop(slot);
            account.check_live()?;
            return Ok(account);
        }
        let mut metadata = self
            .0
            .native_setup_metadata
            .lock()
            .map_err(|_| GateError::Unavailable)?;
        if metadata.is_none() {
            // Covers the bounded account/slot/Arc/weak-ledger metadata until
            // actual destruction, including a retained weak Core registry key.
            *metadata = Some(control.reserve(ControlKind::Registration, ACCOUNT_METADATA_BYTES)?);
        }
        let generation = self.next_native_setup_id()?;
        let state = Arc::new(State {
            generation,
            domain: domain.clone(),
            proof,
            control: control.clone(),
            occupancy: std::sync::Mutex::new(Occupancy::default()),
        });
        if ledger_domain.is_none() {
            *ledger_domain = Some(domain);
        }
        *slot = Some(state.clone());
        Ok(NativeSetupAccount {
            ledger: self.clone(),
            state,
        })
    }

    fn next_native_setup_id(&self) -> Result<u64, GateError> {
        self.validate_process()?;
        self.0
            .next
            .fetch_update(
                std::sync::atomic::Ordering::AcqRel,
                std::sync::atomic::Ordering::Acquire,
                |id| id.checked_add(1),
            )
            .map_err(|_| GateError::Limit("native Setup identities exhausted".into()))
    }
}

impl NativeSetupAccount {
    fn check_live(&self) -> Result<(), GateError> {
        self.ledger.validate_process()?;
        self.state.proof.validate()?;
        if self.state.proof.allocation_domain() != self.state.domain {
            return Err(GateError::Denied("native Setup root proof changed".into()));
        }
        Ok(())
    }

    /// Actual native invocation identity, never an invented Gate request.
    pub fn begin(&self) -> Result<NativeSetupInvocation, GateError> {
        self.check_live()?;
        let control = self
            .state
            .control
            .reserve(ControlKind::Frame, INVOCATION_METADATA_BYTES)?;
        if LeasedShared::<Invocation, ControlReservation>::storage_bytes() as u64
            > INVOCATION_METADATA_BYTES
        {
            return Err(GateError::Limit(
                "native Setup invocation metadata layout".into(),
            ));
        }
        let invocation = self.ledger.next_native_setup_id()?;
        let value = Invocation {
            account: self.clone(),
            provenance: NativeSetupProvenance {
                account_generation: self.state.generation,
                invocation,
            },
        };
        LeasedShared::try_new_precharged(value, control)
            .map(NativeSetupInvocation)
            .map_err(|_| GateError::Unavailable)
    }

    pub fn snapshot(&self) -> Result<NativeSetupSnapshot, GateError> {
        self.check_live()?;
        let occupancy = self
            .state
            .occupancy
            .lock()
            .map_err(|_| GateError::Unavailable)?;
        if occupancy.invalid {
            return Err(GateError::Unavailable);
        }
        Ok(NativeSetupSnapshot {
            account_generation: self.state.generation,
            limit_bytes: NATIVE_SETUP_MEMORY_BYTES,
            charged_bytes: occupancy.used,
            reservations: occupancy.records.len(),
        })
    }
}

struct Invocation {
    account: NativeSetupAccount,
    provenance: NativeSetupProvenance,
}
/// Shared invocation custody, with one funded record through actual destruction.
#[derive(Clone)]
pub struct NativeSetupInvocation(LeasedShared<Invocation, ControlReservation>);
impl NativeSetupInvocation {
    pub fn provenance(&self) -> NativeSetupProvenance {
        self.0.get().provenance
    }

    /// Reserve actual guest/stack/ABI/buffer bytes BEFORE allocation. The caller
    /// supplies no funding limit or owner; metadata has its separate permit.
    pub fn reserve(
        &self,
        kind: CapacityKind,
        bytes: u64,
    ) -> Result<NativeSetupReservation, GateError> {
        let account = &self.0.get().account;
        account.check_live()?;
        let control = account
            .state
            .control
            .reserve(ControlKind::Frame, RECORD_METADATA_BYTES)?;
        if LeasedShared::<Payload, ReleasePlan>::storage_bytes() as u64 > RECORD_METADATA_BYTES {
            return Err(GateError::Limit(
                "native Setup reservation metadata layout".into(),
            ));
        }
        let id = account.ledger.next_native_setup_id()?;
        {
            let mut occupancy = account
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
        }
        let plan = ReleasePlan {
            account: Some(account.clone()),
            id,
            _control: control,
        };
        let value = Payload {
            account: account.clone(),
            id,
            kind,
            provenance: self.provenance(),
        };
        LeasedShared::try_new_precharged(value, plan)
            .map(NativeSetupReservation)
            .map_err(|_| GateError::Unavailable)
    }
}

struct Payload {
    account: NativeSetupAccount,
    id: u64,
    kind: CapacityKind,
    provenance: NativeSetupProvenance,
}
/// Shared custody of ONE physical allocation, not permission to copy a buffer.
#[derive(Clone)]
pub struct NativeSetupReservation(LeasedShared<Payload, ReleasePlan>);
impl NativeSetupReservation {
    pub fn provenance(&self) -> NativeSetupProvenance {
        self.0.get().provenance
    }
    pub fn kind(&self) -> CapacityKind {
        self.0.get().kind
    }

    /// Reserve growth first; shrink only after actual deallocation or failure
    /// of the attempted underlying growth, as with the ordinary Pack limiter.
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

struct ReleasePlan {
    account: Option<NativeSetupAccount>,
    id: u64,
    _control: ControlReservation,
}
impl Drop for ReleasePlan {
    fn drop(&mut self) {
        let Some(account) = self.account.take() else {
            return;
        };
        // Never lock a mutex inherited from a process with vanished threads.
        if account.ledger.validate_process().is_err() {
            return;
        }
        let mut occupancy = account
            .state
            .occupancy
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        let record = occupancy.records.remove(&self.id);
        if occupancy.records.is_empty() {
            drop(std::mem::take(&mut occupancy.records));
        }
        if let Some(record) = record {
            if let Some(next) = occupancy.used.checked_sub(record.allocated) {
                occupancy.used = next;
            } else {
                occupancy.invalid = true;
            }
        } else {
            occupancy.invalid = true;
        }
        // LeasedShared already freed its payload/block. Destroy remaining map
        // metadata and account custody BEFORE returning this control permit.
        drop(occupancy);
        drop(account);
    }
}
