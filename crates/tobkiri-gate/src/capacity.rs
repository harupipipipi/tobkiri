//! Physical capacity is owned by stable Profile/Pack, never a request or revision.
//! Callers obtain a fresh verified limit from Gate before reserve/growth. This
//! ledger grants no authority and cannot infer an approved limit from provenance.
//!
//! Inventory: at most 4096 owner states, 1024 live preparation guards globally,
//! and 4096 physical records per owner. Each physical record prepays 4096 bytes
//! for its exact LeasedShared block, bounded ID copies, record/node slack and
//! retained owner-state references. These bounds are not a whole-memory proof:
//! initial ledger/owner/preparation BTreeMap nodes and Arc control storage still
//! need a separately funded permanent registry/preparation custody path before
//! enabling untrusted execution under a complete aggregate-memory claim.
use crate::{CallCx, GateError};
use std::{
    collections::BTreeMap,
    sync::{
        Arc, Mutex, Weak,
        atomic::{AtomicU64, Ordering},
    },
};
use tobkiri_canon::{
    Digest, LeasedShared,
    ids::{PackHash, PackId, ProfileId, RequestId},
};

#[path = "native_setup_capacity.rs"]
mod native_setup;
pub use native_setup::*;

/// Stable funding identity; hashes/epochs/stores do not create separate pools.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum CapacityOwner {
    Pack { profile: ProfileId, pack: PackId },
    // The current established User principal is one account per Profile.
    User { profile: ProfileId },
}
impl CapacityOwner {
    pub fn pack_owner(profile: ProfileId, pack: PackId) -> Self {
        Self::Pack { profile, pack }
    }
    pub fn user_owner(profile: ProfileId) -> Self {
        Self::User { profile }
    }
    pub fn profile(&self) -> &ProfileId {
        match self {
            Self::Pack { profile, .. } | Self::User { profile } => profile,
        }
    }
    pub fn pack(&self) -> Option<&PackId> {
        match self {
            Self::Pack { pack, .. } => Some(pack),
            Self::User { .. } => None,
        }
    }
    /// Identity only. Gate::capacity_limit supplies the independent authority check.
    pub fn from_context(cx: &CallCx) -> Result<Self, GateError> {
        if let Some((pack, _)) = cx.principal().pack() {
            Ok(Self::pack_owner(cx.profile().clone(), pack.clone()))
        } else if matches!(cx.principal(), crate::Principal::User) {
            Ok(Self::user_owner(cx.profile().clone()))
        } else {
            Err(GateError::Denied(
                "capacity requires a stable verified allocation owner".into(),
            ))
        }
    }
}
/// Trace binding only; changing any field never refunds or mints capacity.
#[derive(Clone, Debug)]
pub struct CapacityProvenance {
    pub hash: Option<PackHash>,
    pub revision: Digest,
    pub security_epoch: u64,
    pub request: RequestId,
    pub store: Option<Digest>,
    pub session: Option<Digest>,
}
impl CapacityProvenance {
    pub fn from_context(
        cx: &CallCx,
        store: Option<Digest>,
        session: Option<Digest>,
    ) -> Result<Self, GateError> {
        CapacityOwner::from_context(cx)?;
        Ok(Self {
            hash: cx.principal().pack().map(|(_, hash)| hash.clone()),
            revision: cx
                .revision()
                .cloned()
                .ok_or_else(|| GateError::Denied("capacity requires a frozen revision".into()))?,
            security_epoch: cx.security_epoch(),
            request: cx.request().clone(),
            store,
            session,
        })
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum CapacityKind {
    GuestLinear,
    SqlLinear,
    HostStack,
    Table,
    AbiMarshal,
    VfsBuffer,
    Runtime,
    Bookkeeping,
}
/// Trusted Core injects Store's unforgeable OS-lock lease. Guest interfaces
/// never accept implementations of this trait or caller-supplied domain IDs.
pub trait AllocationOwnerLease: Send + Sync {
    fn owner(&self) -> &CapacityOwner;
    /// Stable physical DataRoot identity, produced only by the trusted broker.
    fn allocation_domain(&self) -> Digest;
    /// Reject inherited/forked or invalid proof before accounting mutex access.
    /// Implementations are injected by trusted Core, never guest payloads.
    fn validate(&self) -> Result<(), GateError> {
        Err(GateError::Unavailable)
    }
}
const RESERVATION_BOOKKEEP_BYTES: u64 = 4096;
/// Conservative prepaid reservation metadata. The actual shared-block layout
/// is checked before construction; this does not certify uninstrumented runtime
/// or the ledger's bounded registry allocations as whole-memory coverage.
pub const fn capacity_reservation_bookkeeping_bytes() -> u64 {
    RESERVATION_BOOKKEEP_BYTES
}
const MAX_OWNERS: usize = 4096;
const MAX_RESERVATIONS_PER_OWNER: usize = 4096;
struct Record {
    kind: CapacityKind,
    allocated: u64,
    verified_limit: u64,
}
#[derive(Default)]
struct OwnerState {
    used: u64,
    records: BTreeMap<u64, Record>,
    preparations: BTreeMap<u64, Weak<dyn AllocationOwnerLease>>,
}
struct Inner {
    process: u32,
    owners: Mutex<BTreeMap<CapacityOwner, Arc<Mutex<OwnerState>>>>,
    next: AtomicU64,
    domain: Mutex<Option<Digest>>,
    native_setup: Mutex<Option<Arc<native_setup::State>>>,
    // Also retained by weak ledger handles: native bookkeeping is not freed
    // merely because a Core or the last strong ledger reference disappeared.
    native_setup_metadata: Arc<Mutex<Option<crate::ControlReservation>>>,
}

/// Shared by concurrent and old Sessions/PackHosts for one authority domain.
#[derive(Clone)]
pub struct CapacityLedger(Arc<Inner>);
impl Default for CapacityLedger {
    fn default() -> Self {
        Self(Arc::new(Inner {
            process: std::process::id(),
            owners: Mutex::new(BTreeMap::new()),
            next: AtomicU64::new(1),
            domain: Mutex::new(None),
            native_setup: Mutex::new(None),
            native_setup_metadata: Arc::new(Mutex::new(None)),
        }))
    }
}
impl CapacityLedger {
    pub fn new() -> Self {
        Self::default()
    }
    /// A registry must retain a weak handle to the actual inner ledger, so
    /// outstanding physical reservations prevent reopening an unfunded pool.
    pub fn downgrade(&self) -> WeakCapacityLedger {
        WeakCapacityLedger {
            inner: Arc::downgrade(&self.0),
            _native_setup_metadata: self.0.native_setup_metadata.clone(),
        }
    }
    fn validate_process(&self) -> Result<(), GateError> {
        if self.0.process != std::process::id() {
            return Err(GateError::Denied(
                "capacity ledger inherited across fork".into(),
            ));
        }
        Ok(())
    }
    /// Validate the physical/process domain before downstream authority code
    /// can touch an inherited persistence or accounting mutex.
    pub fn check_domain(&self) -> Result<(), GateError> {
        self.validate_process()
    }
    /// Bind a real OS-exclusive owner lease before any allocation admission.
    /// Different roots can never be rebound into the same authority ledger.
    pub fn bind_owner(
        &self,
        proof: Arc<dyn AllocationOwnerLease>,
    ) -> Result<CapacityBinding, GateError> {
        self.validate_process()?;
        proof.validate()?;
        let mut owners = self
            .0
            .owners
            .lock()
            .map_err(|_| GateError::Persistence("capacity ledger poisoned".into()))?;
        if !owners.contains_key(proof.owner()) && owners.len() >= MAX_OWNERS {
            return Err(GateError::Limit("capacity owner bookkeeping limit".into()));
        }
        let preparations = owners.values().try_fold(0usize, |count, state| {
            let state = state
                .lock()
                .map_err(|_| GateError::Persistence("capacity owner poisoned".into()))?;
            count
                .checked_add(state.preparations.len())
                .ok_or_else(|| GateError::Limit("capacity preparation count overflow".into()))
        })?;
        if preparations >= 1024 {
            return Err(GateError::Unavailable);
        }
        let mut domain = self
            .0
            .domain
            .lock()
            .map_err(|_| GateError::Persistence("capacity domain poisoned".into()))?;
        let requested = proof.allocation_domain();
        if domain.as_ref().is_some_and(|domain| domain != &requested) {
            return Err(GateError::Denied(
                "capacity proof belongs to another physical root".into(),
            ));
        }
        if domain.is_none() {
            *domain = Some(requested);
        }
        let owner = proof.owner().clone();
        let state = owners.entry(owner).or_default();
        let mut state = state
            .lock()
            .map_err(|_| GateError::Persistence("capacity owner poisoned".into()))?;
        let id = self
            .0
            .next
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |id| id.checked_add(1))
            .map_err(|_| GateError::Limit("capacity identities exhausted".into()))?;
        state.preparations.insert(id, Arc::downgrade(&proof));
        Ok(CapacityBinding {
            ledger: self.0.clone(),
            proof: Some(proof),
            id,
        })
    }
    /// Reserve before allocating. `verified_limit` must be Gate's fresh minimum
    /// of live approval, frozen effective policy and proposed/narrowed call cap.
    pub fn reserve(
        &self,
        owner: CapacityOwner,
        provenance: CapacityProvenance,
        kind: CapacityKind,
        allocated: u64,
        verified_limit: u64,
    ) -> Result<CapacityReservation, GateError> {
        self.validate_process()?;
        let charged = allocated
            .checked_add(RESERVATION_BOOKKEEP_BYTES)
            .ok_or_else(|| GateError::Limit("capacity size overflow".into()))?;
        if charged > verified_limit {
            return Err(GateError::Limit("aggregate Pack capacity exceeded".into()));
        }
        let owners = self
            .0
            .owners
            .lock()
            .map_err(|_| GateError::Persistence("capacity ledger poisoned".into()))?;
        let state = owners.get(&owner).cloned().ok_or_else(|| {
            GateError::Denied("capacity requires the actual owner allocation lock".into())
        })?;
        let locked = state
            .lock()
            .map_err(|_| GateError::Persistence("capacity owner poisoned".into()))?;
        let owner_lease = locked
            .preparations
            .values()
            .find_map(Weak::upgrade)
            .ok_or_else(|| {
                GateError::Denied("capacity requires the actual owner allocation lock".into())
            })?;
        // The ledger PID is checked before any mutex. Broker proof validation
        // must also precede the mutation phase, and a preparation released
        // during validation cannot silently recreate an unfunded owner state.
        drop(locked);
        drop(owners);
        owner_lease.validate()?;
        let owners = self
            .0
            .owners
            .lock()
            .map_err(|_| GateError::Persistence("capacity ledger poisoned".into()))?;
        if !owners
            .get(&owner)
            .is_some_and(|current| Arc::ptr_eq(current, &state))
        {
            return Err(GateError::Denied(
                "capacity preparation ended before admission".into(),
            ));
        }
        let mut locked = state
            .lock()
            .map_err(|_| GateError::Persistence("capacity owner poisoned".into()))?;
        let result = (|| {
            if locked.records.len() >= MAX_RESERVATIONS_PER_OWNER {
                return Err(GateError::Limit(
                    "capacity reservation bookkeeping limit".into(),
                ));
            }
            let admission_limit = locked
                .records
                .values()
                .fold(verified_limit, |limit, record| {
                    limit.min(record.verified_limit)
                });
            let used = locked
                .used
                .checked_add(charged)
                .filter(|used| *used <= admission_limit)
                .ok_or_else(|| GateError::Limit("aggregate Pack capacity exceeded".into()))?;
            let id = self
                .0
                .next
                .fetch_update(Ordering::AcqRel, Ordering::Acquire, |id| id.checked_add(1))
                .map_err(|_| GateError::Limit("capacity identities exhausted".into()))?;
            locked.records.insert(
                id,
                Record {
                    kind,
                    allocated,
                    verified_limit,
                },
            );
            locked.used = used;
            Ok(id)
        })();
        drop(locked);
        // ReleasePlan takes this same mutex. It must be able to unwind a
        // failed fallible shared allocation without recursively locking it.
        drop(owners);
        let id = result?;
        let payload = LeasePayload {
            ledger: self.0.clone(),
            owner,
            state: state.clone(),
            id,
            provenance,
            _owner_lease: owner_lease.clone(),
        };
        let release = ReleasePlan {
            ledger: Some(self.0.clone()),
            state: Some(state),
            id,
            _owner_lease: Some(owner_lease),
        };
        if LeasedShared::<LeasePayload, ReleasePlan>::storage_bytes() as u64
            > RESERVATION_BOOKKEEP_BYTES
        {
            drop(payload);
            drop(release);
            return Err(GateError::Unavailable);
        }
        Ok(CapacityReservation(
            LeasedShared::try_new_precharged(payload, release)
                .map_err(|_| GateError::Limit("capacity reservation allocation failed".into()))?,
        ))
    }
    /// Check admission without refunding existing physical leases on revocation.
    pub fn check_limit(&self, owner: &CapacityOwner, verified_limit: u64) -> Result<(), GateError> {
        self.validate_process()?;
        let owners = self
            .0
            .owners
            .lock()
            .map_err(|_| GateError::Persistence("capacity ledger poisoned".into()))?;
        if let Some(state) = owners.get(owner) {
            let state = state
                .lock()
                .map_err(|_| GateError::Persistence("capacity owner poisoned".into()))?;
            let admission_limit = state
                .records
                .values()
                .fold(verified_limit, |limit, record| {
                    limit.min(record.verified_limit)
                });
            if state.used > admission_limit {
                return Err(GateError::Limit(
                    "existing Pack capacity exceeds the proposed cap".into(),
                ));
            }
        }
        Ok(())
    }
    pub fn snapshot(&self, owner: &CapacityOwner) -> Result<CapacitySnapshot, GateError> {
        self.validate_process()?;
        let owners = self
            .0
            .owners
            .lock()
            .map_err(|_| GateError::Persistence("capacity ledger poisoned".into()))?;
        let Some(state) = owners.get(owner) else {
            return Ok(CapacitySnapshot::default());
        };
        let state = state
            .lock()
            .map_err(|_| GateError::Persistence("capacity owner poisoned".into()))?;
        let mut by_kind = BTreeMap::new();
        for record in state.records.values() {
            *by_kind.entry(record.kind).or_insert(0u64) += record.allocated;
        }
        by_kind.insert(
            CapacityKind::Bookkeeping,
            by_kind
                .get(&CapacityKind::Bookkeeping)
                .copied()
                .unwrap_or(0)
                + (state.records.len() as u64) * RESERVATION_BOOKKEEP_BYTES,
        );
        Ok(CapacitySnapshot {
            charged_bytes: state.used,
            reservations: state.records.len(),
            by_kind,
        })
    }
}
/// Explicit preparation occupancy. Dropping an unused binding releases the
/// actual OS owner proof; the weak ledger cache alone cannot pin exclusion.
/// Keep it alive until the first reserve has captured its own strong proof.
pub struct CapacityBinding {
    ledger: Arc<Inner>,
    proof: Option<Arc<dyn AllocationOwnerLease>>,
    id: u64,
}
impl Drop for CapacityBinding {
    fn drop(&mut self) {
        if self.ledger.process != std::process::id() {
            return;
        }
        let Some(proof) = self.proof.take() else {
            return;
        };
        let mut owners = self
            .ledger
            .owners
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        if let Some(state) = owners.get(proof.owner()) {
            let remove = {
                let mut state = state.lock().unwrap_or_else(|error| error.into_inner());
                state.preparations.remove(&self.id);
                state.records.is_empty() && state.used == 0 && state.preparations.is_empty()
            };
            if remove {
                owners.remove(proof.owner());
            }
        }
        // Factory/admission and the final preparation proof release share
        // this critical section; a weak registry never creates a second pool.
        drop(proof);
    }
}
/// Opaque weak handle to the funded inner state; only zero ownership can expire it.
#[derive(Clone)]
pub struct WeakCapacityLedger {
    // Field order matters: destroy the weak allocation BEFORE returning its
    // separately funded native bookkeeping, including in registry pruning.
    inner: Weak<Inner>,
    _native_setup_metadata: Arc<Mutex<Option<crate::ControlReservation>>>,
}
impl WeakCapacityLedger {
    pub fn upgrade(&self) -> Option<CapacityLedger> {
        self.inner.upgrade().map(CapacityLedger)
    }
}
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct CapacitySnapshot {
    pub charged_bytes: u64,
    pub reservations: usize,
    pub by_kind: BTreeMap<CapacityKind, u64>,
}
struct LeasePayload {
    ledger: Arc<Inner>,
    owner: CapacityOwner,
    state: Arc<Mutex<OwnerState>>,
    id: u64,
    provenance: CapacityProvenance,
    _owner_lease: Arc<dyn AllocationOwnerLease>,
}
/// Shared ownership of ONE actual capacity, not permission to clone an owned
/// buffer. Every additional owned copy must reserve a separate lease.
#[derive(Clone)]
pub struct CapacityReservation(LeasedShared<LeasePayload, ReleasePlan>);
impl CapacityReservation {
    pub fn owner(&self) -> &CapacityOwner {
        &self.0.get().owner
    }
    pub fn provenance(&self) -> &CapacityProvenance {
        &self.0.get().provenance
    }
    /// Growth is reserved before allocation. Shrink is allowed only after known
    /// deallocation or a failed underlying memory growth, including revoked cx.
    pub fn resize(&self, allocated: u64, verified_limit: u64) -> Result<(), GateError> {
        if self.0.get().ledger.process != std::process::id() {
            return Err(GateError::Denied(
                "capacity lease inherited across fork".into(),
            ));
        }
        self.0.get()._owner_lease.validate()?;
        let mut state = self
            .0
            .get()
            .state
            .lock()
            .map_err(|_| GateError::Persistence("capacity owner poisoned".into()))?;
        let previous = state
            .records
            .get(&self.0.get().id)
            .ok_or_else(|| GateError::Persistence("capacity lease missing".into()))?
            .allocated;
        let used = state
            .used
            .checked_sub(previous)
            .and_then(|used| used.checked_add(allocated))
            .ok_or_else(|| GateError::Limit("capacity size overflow".into()))?;
        let admission_limit = state
            .records
            .values()
            .fold(verified_limit, |limit, record| {
                limit.min(record.verified_limit)
            });
        if allocated > previous && used > admission_limit {
            return Err(GateError::Limit("aggregate Pack capacity exceeded".into()));
        }
        let record = state
            .records
            .get_mut(&self.0.get().id)
            .expect("checked above");
        record.allocated = allocated;
        // A successfully admitted narrower live entry cannot be forgotten by
        // later growth under a larger revision. Zero is the cleanup-only path,
        // never a new funding cap, so it must not poison a retained live lease.
        if verified_limit > 0 {
            record.verified_limit = record.verified_limit.min(verified_limit);
        }
        state.used = used;
        Ok(())
    }
    pub fn allocated_bytes(&self) -> Result<u64, GateError> {
        if self.0.get().ledger.process != std::process::id() {
            return Err(GateError::Denied(
                "capacity lease inherited across fork".into(),
            ));
        }
        self.0.get()._owner_lease.validate()?;
        self.0
            .get()
            .state
            .lock()
            .map_err(|_| GateError::Persistence("capacity owner poisoned".into()))?
            .records
            .get(&self.0.get().id)
            .map(|record| record.allocated)
            .ok_or_else(|| GateError::Persistence("capacity lease missing".into()))
    }
}
struct ReleasePlan {
    ledger: Option<Arc<Inner>>,
    state: Option<Arc<Mutex<OwnerState>>>,
    id: u64,
    _owner_lease: Option<Arc<dyn AllocationOwnerLease>>,
}
impl Drop for ReleasePlan {
    fn drop(&mut self) {
        let Some(ledger) = self.ledger.take() else {
            return;
        };
        // Forked accounting memory must never wait on parent mutexes.
        if ledger.process != std::process::id() {
            return;
        }
        let mut owners = ledger
            .owners
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let Some(state) = self.state.take() else {
            return;
        };
        let mut locked = state.lock().unwrap_or_else(|error| error.into_inner());
        let removed = locked.records.remove(&self.id);
        if locked.records.is_empty() {
            // BTreeMap can retain an empty root allocation. Physically drop it
            // while the final record's bookkeeping is still funded.
            drop(std::mem::take(&mut locked.records));
        }
        if let Some(record) = removed
            && let Some(used) = locked
                .used
                .checked_sub(record.allocated + RESERVATION_BOOKKEEP_BYTES)
        {
            locked.used = used;
        }
        if locked.records.is_empty() && locked.used == 0 && locked.preparations.is_empty() {
            owners.retain(|_, other| !Arc::ptr_eq(other, &state));
        }
        drop(locked);
        // Native LeasedShared has already destroyed payload and control block.
        // Retire remaining map/control metadata and the actual OS proof while
        // admission is still serialized; no new pool can see an early refund.
        drop(state);
        drop(self._owner_lease.take());
        drop(owners);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::AtomicUsize;

    fn owner(pack: &str) -> CapacityOwner {
        CapacityOwner::pack_owner(
            ProfileId::parse("capacity-profile").unwrap(),
            PackId::parse(pack).unwrap(),
        )
    }
    struct TestProof(CapacityOwner, Digest, Option<Arc<AtomicUsize>>);
    impl AllocationOwnerLease for TestProof {
        fn owner(&self) -> &CapacityOwner {
            &self.0
        }
        fn allocation_domain(&self) -> Digest {
            self.1.clone()
        }
        fn validate(&self) -> Result<(), GateError> {
            Ok(())
        }
    }
    impl Drop for TestProof {
        fn drop(&mut self) {
            if let Some(counter) = &self.2 {
                counter.fetch_add(1, Ordering::SeqCst);
            }
        }
    }
    fn proof(pack: &str) -> Arc<dyn AllocationOwnerLease> {
        Arc::new(TestProof(owner(pack), Digest::hash(b"test-root"), None))
    }
    fn ledger() -> (CapacityLedger, Vec<CapacityBinding>) {
        let ledger = CapacityLedger::new();
        let bindings = ["local.owner", "local.one", "local.two"]
            .into_iter()
            .map(|pack| ledger.bind_owner(proof(pack)).unwrap())
            .collect();
        (ledger, bindings)
    }
    fn provenance(revision: &str) -> CapacityProvenance {
        CapacityProvenance {
            hash: Some(PackHash::new(Digest::hash(revision.as_bytes()))),
            revision: Digest::hash(revision.as_bytes()),
            security_epoch: 1,
            request: RequestId::parse(revision).unwrap(),
            store: Some(Digest::hash(revision.as_bytes())),
            session: None,
        }
    }
    const META: u64 = RESERVATION_BOOKKEEP_BYTES;

    #[test]
    fn changed_hash_revision_store_request_does_not_create_another_pool() {
        let (ledger, _bindings) = ledger();
        let owner = owner("local.owner");
        let limit = 6000 + META;
        let first = ledger
            .reserve(
                owner.clone(),
                provenance("old"),
                CapacityKind::GuestLinear,
                6000,
                limit,
            )
            .unwrap();
        for proposed in [limit, 24000] {
            assert!(
                ledger
                    .reserve(
                        owner.clone(),
                        provenance("new"),
                        CapacityKind::SqlLinear,
                        3000,
                        proposed
                    )
                    .is_err()
            );
            assert_eq!(ledger.snapshot(&owner).unwrap().charged_bytes, limit);
        }
        drop(first);
        assert_eq!(ledger.snapshot(&owner).unwrap().charged_bytes, 0);
        let second = ledger
            .reserve(
                owner.clone(),
                provenance("larger"),
                CapacityKind::SqlLinear,
                11000,
                24000,
            )
            .unwrap();
        assert_eq!(ledger.snapshot(&owner).unwrap().charged_bytes, 11000 + META);
        assert!(ledger.check_limit(&owner, limit).is_err());
        drop(second);
    }
    #[test]
    fn failed_growth_preserves_previous_charge_and_cleanup_is_the_only_release() {
        let (ledger, _bindings) = ledger();
        let owner = owner("local.owner");
        let limit = 3000 + META;
        let lease = ledger
            .reserve(
                owner.clone(),
                provenance("one"),
                CapacityKind::GuestLinear,
                1000,
                limit,
            )
            .unwrap();
        assert!(lease.resize(4000, limit).is_err());
        assert_eq!(lease.allocated_bytes().unwrap(), 1000);
        lease.resize(2000, limit).unwrap();
        lease.resize(1000, 0).unwrap();
        let unfinished_io = lease.clone();
        drop(lease);
        assert_eq!(ledger.snapshot(&owner).unwrap().charged_bytes, 1000 + META);
        drop(unfinished_io);
        assert_eq!(ledger.snapshot(&owner).unwrap().charged_bytes, 0);
    }
    #[test]
    fn different_stable_packs_have_independent_funds_and_overflow_denies() {
        let (ledger, _bindings) = ledger();
        let one = ledger
            .reserve(
                owner("local.one"),
                provenance("a"),
                CapacityKind::AbiMarshal,
                1,
                1 + META,
            )
            .unwrap();
        let two = ledger
            .reserve(
                owner("local.two"),
                provenance("b"),
                CapacityKind::AbiMarshal,
                1,
                1 + META,
            )
            .unwrap();
        assert!(
            ledger
                .reserve(
                    owner("local.one"),
                    provenance("c"),
                    CapacityKind::AbiMarshal,
                    u64::MAX,
                    u64::MAX
                )
                .is_err()
        );
        assert_eq!(
            ledger.snapshot(one.owner()).unwrap().charged_bytes,
            1 + META
        );
        assert_eq!(
            ledger.snapshot(two.owner()).unwrap().charged_bytes,
            1 + META
        );
    }
    #[test]
    fn concurrent_admission_has_no_check_then_allocate_race() {
        let (ledger, _bindings) = ledger();
        let owner = owner("local.owner");
        let barrier = Arc::new(std::sync::Barrier::new(8));
        let jobs: Vec<_> = (0..8)
            .map(|_| {
                let ledger = ledger.clone();
                let owner = owner.clone();
                let barrier = barrier.clone();
                std::thread::spawn(move || {
                    barrier.wait();
                    ledger.reserve(
                        owner,
                        provenance("parallel"),
                        CapacityKind::VfsBuffer,
                        1000,
                        2 * (1000 + META),
                    )
                })
            })
            .collect();
        let leases: Vec<_> = jobs
            .into_iter()
            .filter_map(|job| job.join().unwrap().ok())
            .collect();
        assert_eq!(leases.len(), 2);
        assert_eq!(
            ledger.snapshot(&owner).unwrap().charged_bytes,
            2 * (1000 + META)
        );
        drop(leases);
        assert_eq!(ledger.snapshot(&owner).unwrap().charged_bytes, 0);
    }
    #[test]
    fn unbound_owner_denies_and_inner_registry_survives_retained_allocation() {
        let empty = CapacityLedger::new();
        assert!(
            empty
                .reserve(
                    owner("local.owner"),
                    provenance("one"),
                    CapacityKind::GuestLinear,
                    1,
                    1 + META
                )
                .is_err()
        );
        let (ledger, bindings) = ledger();
        let weak = ledger.downgrade();
        assert!(
            ledger
                .bind_owner(Arc::new(TestProof(
                    owner("local.owner"),
                    Digest::hash(b"another-root"),
                    None
                )))
                .is_err()
        );
        let lease = ledger
            .reserve(
                owner("local.owner"),
                provenance("one"),
                CapacityKind::GuestLinear,
                1,
                1 + META,
            )
            .unwrap();
        drop(bindings);
        drop(ledger);
        assert_eq!(
            weak.upgrade()
                .unwrap()
                .snapshot(lease.owner())
                .unwrap()
                .charged_bytes,
            1 + META
        );
        drop(lease);
        assert!(weak.upgrade().is_none());
    }
    #[test]
    fn failed_zero_reservation_keeps_preparation_valid_and_unused_drop_releases_proof() {
        let ledger = CapacityLedger::new();
        let dropped = Arc::new(AtomicUsize::new(0));
        let binding = ledger
            .bind_owner(Arc::new(TestProof(
                owner("local.owner"),
                Digest::hash(b"test-root"),
                Some(dropped.clone()),
            )))
            .unwrap();
        assert!(
            ledger
                .reserve(
                    owner("local.owner"),
                    provenance("failed"),
                    CapacityKind::Runtime,
                    0,
                    0
                )
                .is_err()
        );
        let reservation = ledger
            .reserve(
                owner("local.owner"),
                provenance("valid"),
                CapacityKind::Runtime,
                1,
                META + 1,
            )
            .unwrap();
        drop(reservation);
        assert_eq!(dropped.load(Ordering::SeqCst), 0);
        drop(binding);
        assert_eq!(dropped.load(Ordering::SeqCst), 1);
        assert!(ledger.0.owners.lock().unwrap().is_empty());
    }
    #[test]
    fn live_clone_retains_owner_proof_until_final_physical_shared_drop() {
        assert!(LeasedShared::<LeasePayload, ReleasePlan>::storage_bytes() as u64 <= META);
        let ledger = CapacityLedger::new();
        let dropped = Arc::new(AtomicUsize::new(0));
        let binding = ledger
            .bind_owner(Arc::new(TestProof(
                owner("local.owner"),
                Digest::hash(b"test-root"),
                Some(dropped.clone()),
            )))
            .unwrap();
        let reservation = ledger
            .reserve(
                owner("local.owner"),
                provenance("retained"),
                CapacityKind::Runtime,
                1,
                META + 1,
            )
            .unwrap();
        let pending = reservation.clone();
        drop(binding);
        drop(reservation);
        assert_eq!(dropped.load(Ordering::SeqCst), 0);
        assert_eq!(
            ledger
                .snapshot(&owner("local.owner"))
                .unwrap()
                .charged_bytes,
            META + 1
        );
        drop(pending);
        assert_eq!(dropped.load(Ordering::SeqCst), 1);
        assert_eq!(
            ledger
                .snapshot(&owner("local.owner"))
                .unwrap()
                .charged_bytes,
            0
        );
    }
    #[test]
    fn concurrent_preparations_do_not_evict_each_others_retry_authority() {
        let ledger = CapacityLedger::new();
        let first = ledger.bind_owner(proof("local.owner")).unwrap();
        let second = ledger.bind_owner(proof("local.owner")).unwrap();
        drop(second);
        assert!(
            ledger
                .reserve(
                    owner("local.owner"),
                    provenance("low"),
                    CapacityKind::Runtime,
                    0,
                    0
                )
                .is_err()
        );
        let reservation = ledger
            .reserve(
                owner("local.owner"),
                provenance("retry"),
                CapacityKind::Runtime,
                1,
                META + 1,
            )
            .unwrap();
        drop(first);
        assert_eq!(
            ledger.snapshot(reservation.owner()).unwrap().reservations,
            1
        );
        drop(reservation);
        assert!(ledger.0.owners.lock().unwrap().is_empty());
    }
    #[test]
    fn final_release_and_new_admission_use_one_funded_owner_state() {
        let (ledger, bindings) = ledger();
        let owner = owner("local.owner");
        let barrier = Arc::new(std::sync::Barrier::new(2));
        let lease = ledger
            .reserve(
                owner.clone(),
                provenance("initial"),
                CapacityKind::Runtime,
                1,
                META + 1,
            )
            .unwrap();
        let worker_ledger = ledger.clone();
        let worker_owner = owner.clone();
        let worker_barrier = barrier.clone();
        let worker = std::thread::spawn(move || {
            worker_barrier.wait();
            for _ in 0..1000 {
                if let Ok(next) = worker_ledger.reserve(
                    worker_owner.clone(),
                    provenance("next"),
                    CapacityKind::Runtime,
                    1,
                    META + 1,
                ) {
                    return next;
                }
                std::thread::yield_now();
            }
            panic!("admission did not recover after actual final release");
        });
        barrier.wait();
        drop(lease);
        let next = worker.join().unwrap();
        assert_eq!(ledger.snapshot(&owner).unwrap().charged_bytes, META + 1);
        drop(bindings);
        drop(next);
        assert_eq!(ledger.snapshot(&owner).unwrap().charged_bytes, 0);
    }
}
