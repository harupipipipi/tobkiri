//! Preallocation adapter for opaque guest values. A budget is bound to one
//! verified entry; retained values keep its physical charge and OS owner lock.
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicU8, AtomicU32, Ordering},
};
use tobkiri_canon::{
    AllocationBudget, AllocationError, AllocationKind, AllocationRequest, MAX_DEPTH,
};
use tobkiri_gate::{
    AllocationFrameScope, CallCx, CapacityKind, CapacityOwner, CapacityProvenance,
    CapacityReservation, ControlKind, ControlReservation, Gate,
};

use crate::host::Shared;
use crate::owned_values::ValueOwners;
use wasmtime::component::{HostAllocationBudget, HostAllocationError, HostAllocationRequest};

// Prepaid bound for this adapter/control block and its inline mutex state.
// The ledger independently prepays its reservation metadata. This is not a
// bound on Wasmtime's uninstrumented runtime allocations or a coverage claim.
const ADAPTER_CAPACITY: u64 = 32 * 1024;
#[cfg(not(test))]
const MAX_CUSTODY: usize = 1024;
#[cfg(test)]
const MAX_CUSTODY: usize = 8;
static CUSTODY_PROCESS: AtomicU32 = AtomicU32::new(0);
static CUSTODY: Mutex<[Option<Custody>; MAX_CUSTODY]> = Mutex::new([const { None }; MAX_CUSTODY]);

struct Custody {
    owner: Option<Arc<PackAllocationBudget>>,
    // External to the adapter payload: its charge survives final payload AND
    // control-block deallocation, including the inline accounting token itself.
    capacity: Option<CapacityReservation>,
    control: Option<ControlReservation>,
}
impl Drop for Custody {
    fn drop(&mut self) {
        drop(self.owner.take());
        drop(self.capacity.take());
        drop(self.control.take());
    }
}
fn check_custody_process() -> anyhow::Result<()> {
    let process = std::process::id();
    let _ = CUSTODY_PROCESS.compare_exchange(0, process, Ordering::AcqRel, Ordering::Acquire);
    if CUSTODY_PROCESS.load(Ordering::Acquire) != process {
        anyhow::bail!("allocation custody inherited across fork")
    }
    Ok(())
}
fn reap_locked(slots: &mut [Option<Custody>; MAX_CUSTODY]) -> usize {
    let mut freed = 0;
    for slot in slots {
        let idle = slot
            .as_ref()
            .and_then(|custody| custody.owner.as_ref())
            .is_some_and(|owner| Arc::strong_count(owner) == 1 && Arc::weak_count(owner) == 0);
        if idle {
            drop(slot.take());
            freed += 1;
        }
    }
    freed
}
/// Opportunistic collection cannot use timeouts to refund still-owned memory.
/// With no subsequent factory/reap call, custody and its OS lock remain charged.
pub(crate) fn reap_custody() -> anyhow::Result<usize> {
    check_custody_process()?;
    let mut slots = CUSTODY
        .lock()
        .map_err(|_| anyhow::anyhow!("allocation custody poisoned"))?;
    Ok(reap_locked(&mut slots))
}

struct AllocationState {
    live_bytes: u64,
    work_remaining: u64,
    invalid: bool,
}

pub(crate) struct PackAllocationBudget {
    process: u32,
    gate: Arc<dyn Gate>,
    cx: CallCx,
    funding: Funding,
    capacity: CapacityReservation,
    state: Mutex<AllocationState>,
    failure: Arc<AtomicU8>,
}
enum Funding {
    Frame(AllocationFrameScope),
    #[cfg(test)]
    Caller,
}
impl Funding {
    fn with_admission(
        &self,
        gate: &dyn Gate,
        cx: &CallCx,
        admit: &mut dyn FnMut(u64) -> Result<(), tobkiri_gate::GateError>,
    ) -> Result<(), tobkiri_gate::GateError> {
        match self {
            // Core holds the actual node-state lock through this fixed ledger
            // admission. A preceding limit snapshot would race node retirement.
            Self::Frame(scope) => gate.with_frame_allocation(cx, scope, admit),
            #[cfg(test)]
            Self::Caller => admit(gate.capacity_limit(cx)?),
        }
    }
    fn owner(&self, _cx: &CallCx) -> Result<CapacityOwner, tobkiri_gate::GateError> {
        match self {
            Self::Frame(scope) => Ok(scope.capacity_owner()),
            #[cfg(test)]
            Self::Caller => CapacityOwner::from_context(_cx),
        }
    }
    fn provenance(&self, cx: &CallCx) -> Result<CapacityProvenance, tobkiri_gate::GateError> {
        match self {
            Self::Frame(scope) => Ok(CapacityProvenance {
                hash: scope.pack_hash().cloned(),
                revision: scope.revision().clone(),
                security_epoch: scope.security_epoch(),
                request: cx.request().clone(),
                store: None,
                session: Some(scope.frame().clone()),
            }),
            #[cfg(test)]
            Self::Caller => CapacityProvenance::from_context(cx, None, None),
        }
    }
}
impl PackAllocationBudget {
    pub(crate) fn for_frame(
        shared: &Shared,
        cx: &CallCx,
        scope: AllocationFrameScope,
    ) -> anyhow::Result<ValueOwners> {
        Self::create(
            shared.gate.clone(),
            &shared.capacity,
            &shared.root,
            cx,
            Funding::Frame(scope),
        )
    }
    fn create(
        gate: Arc<dyn Gate>,
        ledger: &tobkiri_gate::CapacityLedger,
        root: &tobkiri_store::DataRoot,
        cx: &CallCx,
        funding: Funding,
    ) -> anyhow::Result<ValueOwners> {
        ledger.check_domain()?;
        check_custody_process()?;
        // Adapter IDs/proofs/locks are bounded native Core control. Only
        // guest-shaped owned allocations consume the stable owner payload fund.
        let control = cx.control().reserve(ControlKind::Frame, ADAPTER_CAPACITY)?;
        // Known-dead owners must be reaped before proposing another charge;
        // otherwise an exhausted pool of idle metadata could never recover.
        {
            let mut slots = CUSTODY
                .lock()
                .map_err(|_| anyhow::anyhow!("allocation custody poisoned"))?;
            reap_locked(&mut slots);
        }
        // Identity/provenance never supplies the limit. Gate checks persisted
        // approval, epoch and the exact frozen revision before every admission.
        let owner = funding.owner(cx)?;
        let proof = match &owner {
            CapacityOwner::Pack { profile, pack } => root.owner_lease(profile, pack)?,
            CapacityOwner::User { profile } => root.user_owner_lease(profile)?,
        };
        // The trusted context clone includes bounded ID strings and ancestry
        // metadata. Refuse an unbounded native-created context before copying.
        if cx.executing_pairs().len() > 32
            || cx.profile().as_str().len() > 128
            || cx.request().as_str().len() > 128
            || cx.parent().is_some_and(|id| id.as_str().len() > 128)
            || cx
                .executing_pairs()
                .iter()
                .any(|(pack, hash)| pack.as_str().len() > 128 || hash.hex().len() != 64)
        {
            anyhow::bail!("allocation context metadata exceeds its fixed custody bound")
        }
        let _binding = ledger.bind_owner(Arc::new(proof))?;
        let mut proposal = Some((owner, funding.provenance(cx)?));
        let mut capacity = None;
        funding.with_admission(gate.as_ref(), cx, &mut |limit| {
            // The closure is single-use, fixed, synchronous ledger work. It
            // cannot select an owner, reenter Core, await or invoke an effect.
            let (owner, provenance) = proposal
                .take()
                .ok_or(tobkiri_gate::GateError::Unavailable)?;
            capacity =
                Some(ledger.reserve(owner, provenance, CapacityKind::AbiMarshal, 0, limit)?);
            Ok(())
        })?;
        let capacity = capacity.ok_or(tobkiri_gate::GateError::Unavailable)?;
        let context_bound = 32
            * (128 + 71 + size_of::<(tobkiri_canon::ids::PackId, tobkiri_canon::ids::PackHash)>())
            + 7 * 128
            + 1024;
        // The factory must independently prepay actual transitive proof/control
        // storage before creating its owned scope. Reporting a nonzero count is
        // only an inventory check, never evidence of that prepayment. The new
        // embedded scope's bounded ID copies are charged by this adapter.
        let scope_copy_bound = match &funding {
            Funding::Frame(scope) => {
                if scope.retained_control_bytes() == 0
                    || AllocationFrameScope::scope_layout_bytes() > size_of::<Funding>()
                {
                    anyhow::bail!("allocation scope custody inventory unavailable")
                }
                4096
            }
            #[cfg(test)]
            Funding::Caller => 0,
        };
        if u64::try_from(
            size_of::<Self>() + size_of::<Custody>() + context_bound + scope_copy_bound,
        )? > ADAPTER_CAPACITY
        {
            anyhow::bail!("allocation adapter exceeds its prepaid storage bound")
        }
        // Factory and reaper share this lock. No private owner/Weak/cache is
        // exported. Even an unexpected trusted Weak keeps custody charged.
        let mut slots = CUSTODY
            .lock()
            .map_err(|_| anyhow::anyhow!("allocation custody poisoned"))?;
        reap_locked(&mut slots);
        let slot = slots
            .iter_mut()
            .find(|slot| slot.is_none())
            .ok_or(tobkiri_gate::GateError::Unavailable)?;
        let failure = Arc::new(AtomicU8::new(0));
        let owner = Arc::new(Self {
            process: std::process::id(),
            gate,
            cx: cx.clone(),
            funding,
            capacity: capacity.clone(),
            state: Mutex::new(AllocationState {
                live_bytes: 0,
                work_remaining: cx.limits().fuel_per_call,
                invalid: false,
            }),
            failure: failure.clone(),
        });
        let graph = owner.clone() as Arc<dyn AllocationBudget>;
        let component = owner.clone() as Arc<dyn HostAllocationBudget>;
        *slot = Some(Custody {
            owner: Some(owner),
            capacity: Some(capacity),
            control: Some(control),
        });
        Ok(ValueOwners {
            graph,
            component,
            allocation_failure: Some(failure),
        })
    }

    fn reserve_checked(&self, request: AllocationRequest) -> Result<(), AllocationError> {
        if self.process != std::process::id() {
            self.failure.store(1, Ordering::Release);
            return Err(AllocationError::Unavailable);
        }
        if request.depth as usize > MAX_DEPTH {
            return Err(AllocationError::Depth);
        }
        let mut state = self.state.lock().map_err(|_| {
            self.failure.store(2, Ordering::Release);
            AllocationError::Unavailable
        })?;
        if state.invalid {
            // Preserve the original refund diagnostic if that invalidated it.
            let _ = self
                .failure
                .compare_exchange(0, 3, Ordering::AcqRel, Ordering::Acquire);
            return Err(AllocationError::Unavailable);
        }
        // Zero-byte traversal still consumes bounded host work. Elements may
        // repeat aliases; the caller charges every actual owned copy separately.
        let work = request.work.max(request.elements);
        let work_remaining = state
            .work_remaining
            .checked_sub(work)
            .ok_or(AllocationError::Limit)?;
        let live_bytes = state
            .live_bytes
            .checked_add(request.bytes)
            .ok_or(AllocationError::Overflow)?;
        let allocated = live_bytes;
        let mut admitted = false;
        self.funding
            .with_admission(self.gate.as_ref(), &self.cx, &mut |limit| {
                if admitted {
                    return Err(tobkiri_gate::GateError::Unavailable);
                }
                self.capacity.resize(allocated, limit)?;
                admitted = true;
                Ok(())
            })
            .map_err(|error| {
                use tobkiri_gate::GateError;
                let code = match &error {
                    GateError::Denied(_) => 4,
                    GateError::Deadline => 5,
                    GateError::Cancelled => 6,
                    GateError::Persistence(_)
                    | GateError::Integrity(_)
                    | GateError::AuditFailure { .. } => 7,
                    GateError::Unavailable => 8,
                    GateError::Limit(_) => 9,
                    _ => 10,
                };
                self.failure.store(code, Ordering::Release);
                match error {
                    GateError::Limit(_) => AllocationError::Limit,
                    _ => AllocationError::Unavailable,
                }
            })?;
        if !admitted {
            self.failure.store(11, Ordering::Release);
            return Err(AllocationError::Unavailable);
        }
        state.live_bytes = live_bytes;
        state.work_remaining = work_remaining;
        Ok(())
    }

    fn release_known(&self, bytes: u64) {
        if self.process != std::process::id() {
            return;
        }
        // Release follows actual payload/control-block deallocation. Revocation
        // does not prevent accounting cleanup or refund any still-live storage.
        let mut state = self.state.lock().unwrap_or_else(|error| error.into_inner());
        let Some(live_bytes) = state.live_bytes.checked_sub(bytes) else {
            self.failure.store(12, Ordering::Release);
            state.invalid = true;
            return;
        };
        if self.capacity.resize(live_bytes, 0).is_err() {
            self.failure.store(13, Ordering::Release);
            state.invalid = true;
            return;
        }
        state.live_bytes = live_bytes;
    }
}
impl HostAllocationBudget for PackAllocationBudget {
    fn reserve(&self, request: HostAllocationRequest) -> Result<(), HostAllocationError> {
        self.reserve_checked(AllocationRequest {
            kind: AllocationKind::Temporary,
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
impl AllocationBudget for PackAllocationBudget {
    fn reserve(&self, request: AllocationRequest) -> Result<(), AllocationError> {
        self.reserve_checked(request)
    }
    fn release(&self, bytes: u64) {
        self.release_known(bytes);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tobkiri_canon::{
        CoreValue, Digest,
        ids::{PackHash, PackId, ProfileId, RequestId},
    };
    use tobkiri_gate::{EffectRequest, Grant, GrantId, Outcome, Principal, ResourceLimits};
    static TEST_CUSTODY: Mutex<()> = Mutex::new(());
    struct FundedTestGate;
    #[async_trait::async_trait]
    impl Gate for FundedTestGate {
        fn check_context(&self, cx: &CallCx) -> Result<(), tobkiri_gate::GateError> {
            cx.check_active()
        }
        async fn authorize(
            &self,
            _: &CallCx,
            _: EffectRequest,
            _: Digest,
        ) -> Result<Grant, tobkiri_gate::GateError> {
            Err(tobkiri_gate::GateError::Unavailable)
        }
        async fn record_outcome(
            &self,
            _: GrantId,
            _: Outcome,
        ) -> Result<(), tobkiri_gate::GateError> {
            Err(tobkiri_gate::GateError::Unavailable)
        }
        async fn record_denial(
            &self,
            _: &CallCx,
            _: &'static str,
            _: &'static str,
        ) -> Result<(), tobkiri_gate::GateError> {
            Err(tobkiri_gate::GateError::Unavailable)
        }
        fn capacity_limit(&self, cx: &CallCx) -> Result<u64, tobkiri_gate::GateError> {
            cx.check_active()?;
            if cx.principal().pack().is_none() {
                return Err(tobkiri_gate::GateError::Unavailable);
            }
            Ok(1024 * 1024)
        }
    }
    fn context() -> CallCx {
        let control = tobkiri_gate::CoreControl::default();
        CallCx::try_root_with_control(
            ProfileId::parse("capacity-fixture").unwrap(),
            Principal::Pack {
                pack: PackId::parse("local.capacity-test").unwrap(),
                hash: PackHash::new(Digest::hash(b"capacity-test")),
            },
            RequestId::parse("allocation-custody").unwrap(),
            tobkiri_gate::CoreCancellation::new_with_control(&control).unwrap(),
            std::time::Instant::now() + std::time::Duration::from_secs(30),
            ResourceLimits {
                fuel_per_call: 100_000,
                memory_mib: 1,
                wall_ms: 30_000,
                table_elements: 32,
                output_bytes: 4096,
            },
            control,
        )
        .unwrap()
        .with_revision(Digest::hash(b"custody-revision"))
    }
    fn private_root(directory: &tempfile::TempDir) -> tobkiri_store::DataRoot {
        // tempfile's existing parent need not be private under the test
        // process umask. DataRoot creates this actual child with mode0700.
        tobkiri_store::DataRoot::open(directory.path().canonicalize().unwrap().join("data"))
            .unwrap()
    }
    #[test]
    fn opaque_value_and_final_owner_control_block_stay_charged_until_actual_reap() {
        let _serial = TEST_CUSTODY.lock().unwrap();
        let directory = tempfile::tempdir().unwrap();
        let root = private_root(&directory);
        let ledger = tobkiri_gate::CapacityLedger::new();
        let cx = context();
        let owner = CapacityOwner::from_context(&cx).unwrap();
        let owners = PackAllocationBudget::create(
            Arc::new(FundedTestGate),
            &ledger,
            &root,
            &cx,
            Funding::Caller,
        )
        .unwrap();
        let value = CoreValue::string("retained physical value", &owners.graph).unwrap();
        let shared = value.clone();
        let peak = ledger.snapshot(&owner).unwrap().charged_bytes;
        assert!(peak > tobkiri_gate::capacity_reservation_bookkeeping_bytes());
        assert_eq!(cx.control().usage().count(ControlKind::Frame), 1);
        drop(owners);
        drop(value);
        reap_custody().unwrap();
        assert_eq!(ledger.snapshot(&owner).unwrap().charged_bytes, peak);
        drop(shared);
        assert_eq!(
            ledger.snapshot(&owner).unwrap().charged_bytes,
            tobkiri_gate::capacity_reservation_bookkeeping_bytes()
        );
        reap_custody().unwrap();
        assert_eq!(ledger.snapshot(&owner).unwrap().charged_bytes, 0);
        assert_eq!(cx.control().usage().count(ControlKind::Frame), 0);
    }
    #[test]
    fn private_weak_reference_cannot_refund_an_undeallocated_control_block() {
        let _serial = TEST_CUSTODY.lock().unwrap();
        let directory = tempfile::tempdir().unwrap();
        let root = private_root(&directory);
        let ledger = tobkiri_gate::CapacityLedger::new();
        let cx = context();
        let identity = CapacityOwner::from_context(&cx).unwrap();
        let owners = PackAllocationBudget::create(
            Arc::new(FundedTestGate),
            &ledger,
            &root,
            &cx,
            Funding::Caller,
        )
        .unwrap();
        // Only this private regression can observe a Weak. Public APIs never
        // export it; a trusted accidental Weak must nevertheless quarantine.
        let weak = {
            let slots = CUSTODY.lock().unwrap();
            let custody = slots
                .iter()
                .flatten()
                .find(|entry| {
                    entry
                        .capacity
                        .as_ref()
                        .is_some_and(|lease| lease.owner() == &identity)
                })
                .unwrap();
            Arc::downgrade(custody.owner.as_ref().unwrap())
        };
        drop(owners);
        reap_custody().unwrap();
        assert!(ledger.snapshot(&identity).unwrap().charged_bytes > 0);
        assert!(weak.upgrade().is_some());
        drop(weak);
        reap_custody().unwrap();
        assert_eq!(ledger.snapshot(&identity).unwrap().charged_bytes, 0);
    }
    #[test]
    fn concurrent_last_reference_release_and_factory_reap_preserve_live_charge() {
        let _serial = TEST_CUSTODY.lock().unwrap();
        let directory = tempfile::tempdir().unwrap();
        let root = Arc::new(private_root(&directory));
        let ledger = tobkiri_gate::CapacityLedger::new();
        let cx = context();
        let identity = CapacityOwner::from_context(&cx).unwrap();
        let owners = PackAllocationBudget::create(
            Arc::new(FundedTestGate),
            &ledger,
            &root,
            &cx,
            Funding::Caller,
        )
        .unwrap();
        let live = CoreValue::string("original", &owners.graph).unwrap();
        drop(owners);
        let barrier = Arc::new(std::sync::Barrier::new(2));
        let other_barrier = barrier.clone();
        let other_ledger = ledger.clone();
        let other_root = root.clone();
        let other_cx = cx.clone();
        let task = std::thread::spawn(move || {
            other_barrier.wait();
            PackAllocationBudget::create(
                Arc::new(FundedTestGate),
                &other_ledger,
                &other_root,
                &other_cx,
                Funding::Caller,
            )
            .unwrap()
        });
        barrier.wait();
        drop(live);
        let next = task.join().unwrap();
        let next_value = CoreValue::string("new", &next.graph).unwrap();
        reap_custody().unwrap();
        assert!(
            ledger.snapshot(&identity).unwrap().charged_bytes
                > tobkiri_gate::capacity_reservation_bookkeeping_bytes()
        );
        drop(next_value);
        drop(next);
        reap_custody().unwrap();
        assert_eq!(ledger.snapshot(&identity).unwrap().charged_bytes, 0);
    }
    #[test]
    fn fixed_custody_exhaustion_denies_and_only_real_last_drop_recovers() {
        let _serial = TEST_CUSTODY.lock().unwrap();
        let directory = tempfile::tempdir().unwrap();
        let root = private_root(&directory);
        let ledger = tobkiri_gate::CapacityLedger::new();
        let cx = context();
        let identity = CapacityOwner::from_context(&cx).unwrap();
        let owners: Vec<_> = (0..MAX_CUSTODY)
            .map(|_| {
                PackAllocationBudget::create(
                    Arc::new(FundedTestGate),
                    &ledger,
                    &root,
                    &cx,
                    Funding::Caller,
                )
                .unwrap()
            })
            .collect();
        let before = ledger.snapshot(&identity).unwrap().charged_bytes;
        assert!(
            PackAllocationBudget::create(
                Arc::new(FundedTestGate),
                &ledger,
                &root,
                &cx,
                Funding::Caller
            )
            .is_err()
        );
        assert_eq!(ledger.snapshot(&identity).unwrap().charged_bytes, before);
        drop(owners);
        // A factory can reclaim actually dead private owners before admission,
        // and does not require a timeout-based bookkeeping refund.
        let next = PackAllocationBudget::create(
            Arc::new(FundedTestGate),
            &ledger,
            &root,
            &cx,
            Funding::Caller,
        )
        .unwrap();
        assert_eq!(
            ledger.snapshot(&identity).unwrap().charged_bytes,
            tobkiri_gate::capacity_reservation_bookkeeping_bytes()
        );
        drop(next);
        reap_custody().unwrap();
        assert_eq!(ledger.snapshot(&identity).unwrap().charged_bytes, 0);
    }
}
