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
            .is_some_and(|owner| {
                Arc::strong_count(owner) == 1
                    && Arc::weak_count(owner) == 0
                    && owner.conversion_reapable()
            });
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
    mode: AllocationMode,
}
#[derive(Clone, Copy)]
enum AllocationMode {
    Metered,
    Conversion { bytes: u64, sealed: bool },
}
/// A synchronous conversion cannot leave allocation permission in an escaped
/// allocator clone. This guard also seals during unwinding, before custody can
/// release the actual adapter/control block.
struct SealConversion(Arc<PackAllocationBudget>);
impl Drop for SealConversion {
    fn drop(&mut self) {
        let mut state = self
            .0
            .state
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        if let AllocationMode::Conversion { sealed, .. } = &mut state.mode {
            *sealed = true;
        }
    }
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
        Ok(Self::create_inner(gate, ledger, root, cx, funding, None)?.0)
    }

    /// A distinct owner for one pure synchronous conversion. The original
    /// graph prepays work; this cannot replenish its remaining fuel. Only the
    /// actual frame admission may reserve this carrier's complete byte bound.
    pub(crate) fn with_prepaid_frame_conversion<T>(
        shared: &Shared,
        cx: &CallCx,
        scope: AllocationFrameScope,
        source: &Arc<dyn AllocationBudget>,
        bound: AllocationRequest,
        convert: impl FnOnce(&ValueOwners) -> anyhow::Result<T>,
    ) -> anyhow::Result<T> {
        Self::with_prepaid_conversion(
            shared.gate.clone(),
            &shared.capacity,
            &shared.root,
            cx,
            Funding::Frame(scope),
            source,
            bound,
            convert,
        )
    }

    #[allow(clippy::too_many_arguments)]
    fn with_prepaid_conversion<T>(
        gate: Arc<dyn Gate>,
        ledger: &tobkiri_gate::CapacityLedger,
        root: &tobkiri_store::DataRoot,
        cx: &CallCx,
        funding: Funding,
        source: &Arc<dyn AllocationBudget>,
        bound: AllocationRequest,
        convert: impl FnOnce(&ValueOwners) -> anyhow::Result<T>,
    ) -> anyhow::Result<T> {
        // No payload inspection or allocation precedes this work admission.
        source.reserve(AllocationRequest { bytes: 0, ..bound })?;
        let (owners, owner) = Self::create_inner(gate, ledger, root, cx, funding, Some(bound))?;
        let seal = SealConversion(owner);
        let result = convert(&owners);
        drop(seal);
        result
    }

    fn create_inner(
        gate: Arc<dyn Gate>,
        ledger: &tobkiri_gate::CapacityLedger,
        root: &tobkiri_store::DataRoot,
        cx: &CallCx,
        funding: Funding,
        conversion: Option<AllocationRequest>,
    ) -> anyhow::Result<(ValueOwners, Arc<Self>)> {
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
            capacity = Some(ledger.reserve(
                owner,
                provenance,
                CapacityKind::AbiMarshal,
                conversion.map_or(0, |bound| bound.bytes),
                limit,
            )?);
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
                work_remaining: conversion.map_or(cx.limits().fuel_per_call, |bound| bound.work),
                invalid: false,
                mode: conversion.map_or(AllocationMode::Metered, |bound| {
                    AllocationMode::Conversion {
                        bytes: bound.bytes,
                        sealed: false,
                    }
                }),
            }),
            failure: failure.clone(),
        });
        let graph = owner.clone() as Arc<dyn AllocationBudget>;
        let component = owner.clone() as Arc<dyn HostAllocationBudget>;
        *slot = Some(Custody {
            owner: Some(owner.clone()),
            capacity: Some(capacity),
            control: Some(control),
        });
        Ok((
            ValueOwners {
                graph,
                component,
                allocation_failure: Some(failure),
            },
            owner,
        ))
    }

    fn conversion_reapable(&self) -> bool {
        let Ok(state) = self.state.lock() else {
            return false;
        };
        match state.mode {
            AllocationMode::Metered => true,
            AllocationMode::Conversion { sealed, .. } => {
                sealed && !state.invalid && state.live_bytes == 0
            }
        }
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
        if let AllocationMode::Conversion { bytes, sealed } = state.mode {
            #[cfg(feature = "closed-request-metrics")]
            let observation = tobkiri_gate::closed_metrics::span(
                self.cx.observation(),
                tobkiri_gate::closed_metrics::Metric::AllocationConversion,
            );
            #[cfg(feature = "closed-request-metrics")]
            observation.add_units(request.bytes, work);
            let result = (|| {
                if sealed {
                    return Err(AllocationError::Unavailable);
                }
                self.cx
                    .check_active()
                    .map_err(|_| AllocationError::Unavailable)?;
                if live_bytes > bytes {
                    return Err(AllocationError::Limit);
                }
                // This only consumes already admitted carrier capacity. No Gate,
                // effect, SQL or growth occurs inside the pure conversion window.
                state.live_bytes = live_bytes;
                state.work_remaining = work_remaining;
                Ok(())
            })();
            #[cfg(feature = "closed-request-metrics")]
            observation.finish();
            return result;
        }
        #[cfg(feature = "closed-request-metrics")]
        let observation = tobkiri_gate::closed_metrics::span(
            self.cx.observation(),
            tobkiri_gate::closed_metrics::Metric::AllocationMetered,
        );
        #[cfg(feature = "closed-request-metrics")]
        observation.add_units(request.bytes, work);
        #[cfg(feature = "closed-request-metrics")]
        let _sync_observation = tobkiri_gate::closed_metrics::enter_sync(self.cx.observation());
        let result = (|| {
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
        })();
        #[cfg(feature = "closed-request-metrics")]
        observation.finish();
        result
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
        if matches!(state.mode, AllocationMode::Conversion { .. }) {
            // Freed suballocations may be reused inside the finite carrier,
            // but its full external charge survives all payload/Arc storage.
            state.live_bytes = live_bytes;
            return;
        }
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
        context_with_fuel(100_000)
    }
    fn context_with_fuel(fuel: u64) -> CallCx {
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
                fuel_per_call: fuel,
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

    #[derive(Default)]
    struct CountingGate {
        admissions: std::sync::atomic::AtomicUsize,
        effects: std::sync::atomic::AtomicUsize,
    }
    #[async_trait::async_trait]
    impl Gate for CountingGate {
        fn check_context(&self, cx: &CallCx) -> Result<(), tobkiri_gate::GateError> {
            cx.check_active()
        }
        async fn authorize(
            &self,
            _: &CallCx,
            _: EffectRequest,
            _: Digest,
        ) -> Result<Grant, tobkiri_gate::GateError> {
            self.effects.fetch_add(1, Ordering::SeqCst);
            Err(tobkiri_gate::GateError::Unavailable)
        }
        async fn record_outcome(
            &self,
            _: GrantId,
            _: Outcome,
        ) -> Result<(), tobkiri_gate::GateError> {
            self.effects.fetch_add(1, Ordering::SeqCst);
            Err(tobkiri_gate::GateError::Unavailable)
        }
        async fn record_denial(
            &self,
            _: &CallCx,
            _: &'static str,
            _: &'static str,
        ) -> Result<(), tobkiri_gate::GateError> {
            self.effects.fetch_add(1, Ordering::SeqCst);
            Err(tobkiri_gate::GateError::Unavailable)
        }
        fn capacity_limit(&self, cx: &CallCx) -> Result<u64, tobkiri_gate::GateError> {
            cx.check_active()?;
            self.admissions.fetch_add(1, Ordering::SeqCst);
            Ok(16 * 1024 * 1024)
        }
    }

    #[cfg(all(
        feature = "closed-request-metrics",
        any(
            windows,
            all(target_os = "macos", target_arch = "aarch64"),
            all(target_os = "linux", target_arch = "x86_64", target_env = "gnu")
        )
    ))]
    #[test]
    fn closed_metrics_real_store_separates_metered_and_prepaid_conversion_sql() {
        use tobkiri_gate::{
            ApprovalAnswer, ApprovalPrompt, ApprovalStore, GateEngine, MediatorChain, PackPolicy,
            PermissionSet, TrustedPrompt,
            closed_metrics::{Metric, Observation},
        };
        struct NoPrompt;
        #[async_trait::async_trait]
        impl TrustedPrompt for NoPrompt {
            async fn prompt_approval(&self, _: ApprovalPrompt) -> ApprovalAnswer {
                ApprovalAnswer::Deny
            }
        }
        let _serial = TEST_CUSTODY.lock().unwrap();
        for length in [1usize, 1024] {
            let directory = tempfile::tempdir().unwrap();
            let root = private_root(&directory);
            let cx = context();
            let approvals = Arc::new(tobkiri_store::Approvals::open(&root, cx.profile()).unwrap());
            let audit = Arc::new(
                tobkiri_store::AuditLog::open(
                    &root,
                    cx.profile(),
                    tobkiri_store::AuditQuota::default(),
                )
                .unwrap(),
            );
            let gate = Arc::new(GateEngine::new_with_control(
                approvals.clone(),
                audit,
                Arc::new(NoPrompt),
                MediatorChain::default(),
                cx.control().clone(),
            ));
            let (pack, hash) = cx.principal().pack().unwrap();
            let policy = PermissionSet {
                wasm: cx.limits().clone(),
                ..Default::default()
            };
            approvals
                .set_permissions(cx.profile(), pack, hash, &policy)
                .unwrap();
            gate.register_pack(
                cx.profile(),
                pack,
                hash,
                PackPolicy {
                    declaration: policy.clone(),
                    ceiling: policy.clone(),
                    pack_limits: policy,
                },
            )
            .unwrap();
            gate.register_revision(
                cx.profile(),
                cx.revision().unwrap(),
                std::collections::BTreeMap::from([(
                    (pack.clone(), hash.clone()),
                    cx.limits().clone(),
                )]),
            )
            .unwrap();
            let observation = Observation::try_new(cx.control()).unwrap();
            let epoch = gate.epoch(cx.profile()).unwrap();
            let cx = cx
                .with_security_epoch(epoch)
                .with_observation(&observation)
                .unwrap();
            let ledger = tobkiri_gate::CapacityLedger::new();
            let source =
                PackAllocationBudget::create(gate.clone(), &ledger, &root, &cx, Funding::Caller)
                    .unwrap();
            let before = observation.snapshot();
            let payload = "x".repeat(length);
            let value = PackAllocationBudget::with_prepaid_conversion(
                gate.clone(),
                &ledger,
                &root,
                &cx,
                Funding::Caller,
                source.graph(),
                AllocationRequest {
                    kind: AllocationKind::Temporary,
                    bytes: 4096,
                    elements: 0,
                    depth: 0,
                    work: 4096,
                },
                |owners| Ok(CoreValue::string(&payload, owners.graph())?),
            )
            .unwrap();
            let after = observation.snapshot();
            let count = |snapshot: &tobkiri_gate::closed_metrics::Snapshot| {
                [
                    Metric::GuardFaultSelect,
                    Metric::GuardEpochSelect,
                    Metric::GuardPackPermissionsSelect,
                ]
                .iter()
                .map(|metric| snapshot.slots[*metric as usize].started)
                .sum::<u64>()
            };
            // This trusted Caller fixture uses capacity_limit (4 SQL), not an
            // authenticated guest Frame (which has its own guard sequence).
            // The two real admissions stay constant as payload size grows.
            assert_eq!(count(&after) - count(&before), 8);
            assert_eq!(
                after.slots[Metric::AllocationMetered as usize].started
                    - before.slots[Metric::AllocationMetered as usize].started,
                1
            );
            assert!(
                after.slots[Metric::AllocationConversion as usize].returned
                    > before.slots[Metric::AllocationConversion as usize].returned
            );
            drop(value);
            drop(source);
            reap_custody().unwrap();
        }
    }

    #[cfg(all(
        feature = "closed-request-metrics",
        any(
            windows,
            all(target_os = "macos", target_arch = "aarch64"),
            all(target_os = "linux", target_arch = "x86_64", target_env = "gnu")
        )
    ))]
    #[test]
    fn closed_metrics_denied_allocations_return_and_panic_timer_drops() {
        use tobkiri_gate::closed_metrics::{Metric, Observation};
        let _serial = TEST_CUSTODY.lock().unwrap();
        for conversion in [false, true] {
            let directory = tempfile::tempdir().unwrap();
            let root = private_root(&directory);
            let ledger = tobkiri_gate::CapacityLedger::new();
            let cx = context();
            let observation = Observation::try_new(cx.control()).unwrap();
            let cx = cx.with_observation(&observation).unwrap();
            let gate = Arc::new(CountingGate::default());
            let source =
                PackAllocationBudget::create(gate.clone(), &ledger, &root, &cx, Funding::Caller)
                    .unwrap();
            let request = AllocationRequest {
                kind: AllocationKind::Temporary,
                bytes: 2,
                elements: 0,
                depth: 0,
                work: 1,
            };
            if conversion {
                PackAllocationBudget::with_prepaid_conversion(
                    gate.clone(),
                    &ledger,
                    &root,
                    &cx,
                    Funding::Caller,
                    source.graph(),
                    AllocationRequest {
                        bytes: 1,
                        work: 16,
                        ..request
                    },
                    |owners| {
                        let before =
                            observation.snapshot().slots[Metric::AllocationConversion as usize];
                        let result = owners.graph().reserve(request);
                        assert!(matches!(result, Err(AllocationError::Limit)));
                        let after =
                            observation.snapshot().slots[Metric::AllocationConversion as usize];
                        assert_eq!(after.started - before.started, 1);
                        assert_eq!(after.returned - before.returned, 1);
                        assert_eq!(after.dropped - before.dropped, 0);
                        Ok(())
                    },
                )
                .unwrap();
            } else {
                cx.cancel().cancel();
                let before = observation.snapshot().slots[Metric::AllocationMetered as usize];
                let result = source.graph().reserve(request);
                assert!(matches!(result, Err(AllocationError::Unavailable)));
                let after = observation.snapshot().slots[Metric::AllocationMetered as usize];
                assert_eq!(after.started - before.started, 1);
                assert_eq!(after.returned - before.returned, 1);
                assert_eq!(after.dropped - before.dropped, 0);
            }
            assert_eq!(gate.effects.load(Ordering::SeqCst), 0);
            drop(source);
            reap_custody().unwrap();
            assert_eq!(
                ledger
                    .snapshot(&CapacityOwner::from_context(&cx).unwrap())
                    .unwrap()
                    .charged_bytes,
                0
            );
        }
        // Exercise the exact production timer's panic Drop independently. A
        // panic inside the allocator's locked state would poison and retain
        // its real custody; this numeric regression must not clear that proof.
        let observation = Observation::try_new(&Default::default()).unwrap();
        let panic = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            observation.span(Metric::AllocationMetered).measure(|| {
                panic!("fixed metrics fixture panic");
            });
        }));
        assert!(panic.is_err());
        let slot = observation.snapshot().slots[Metric::AllocationMetered as usize];
        assert_eq!(slot.started, 1);
        assert_eq!(slot.returned, 0);
        assert_eq!(slot.dropped, 1);
    }

    #[test]
    fn prepaid_fixed_kv_has_constant_admissions_and_identical_request_digest() {
        let _serial = TEST_CUSTODY.lock().unwrap();
        for length in [0usize, 1, 256, 6144] {
            let directory = tempfile::tempdir().unwrap();
            let root = private_root(&directory);
            let ledger = tobkiri_gate::CapacityLedger::new();
            let cx = context_with_fuel(1_000_000);
            let identity = CapacityOwner::from_context(&cx).unwrap();
            let gate = Arc::new(CountingGate::default());
            let source =
                PackAllocationBudget::create(gate.clone(), &ledger, &root, &cx, Funding::Caller)
                    .unwrap();
            let key = "key\"\\\n\u{1}日本語";
            let payload: Vec<u8> = (0..length).map(|n| n as u8).collect();
            let value = crate::effect_values::EffectValues(&source)
                .bytes(&payload)
                .unwrap();
            let old_arguments = CoreValue::object_shared(
                [
                    Ok(("op", CoreValue::string("kv-put", source.graph()).unwrap())),
                    Ok(("key", CoreValue::string(key, source.graph()).unwrap())),
                    Ok((
                        "value",
                        crate::effect_values::EffectValues(&source)
                            .wire(&value)
                            .unwrap(),
                    )),
                ]
                .into_iter(),
                source.graph(),
            )
            .unwrap();
            let old =
                tobkiri_store::FixedDataRequest::from_value(old_arguments, source.graph()).unwrap();
            let expected = old.digest().clone();
            drop(old);
            let baseline = ledger.snapshot(&identity).unwrap().charged_bytes;
            let before = gate.admissions.load(Ordering::SeqCst);
            let bound = CoreValue::kv_put_conversion_bound(key.len(), length).unwrap();
            let (fixed, escaped) = PackAllocationBudget::with_prepaid_conversion(
                gate.clone(),
                &ledger,
                &root,
                &cx,
                Funding::Caller,
                source.graph(),
                bound.allocation,
                |owners| {
                    Ok((
                        crate::effects::fixed_kv_put_conversion(
                            key,
                            &value,
                            owners,
                            bound.validation_work,
                        )?,
                        owners.graph().clone(),
                    ))
                },
            )
            .unwrap();
            assert_eq!(gate.admissions.load(Ordering::SeqCst) - before, 2);
            assert_eq!(gate.effects.load(Ordering::SeqCst), 0);
            assert_eq!(fixed.digest(), &expected);
            assert!(matches!(
                fixed.operation(),
                tobkiri_store::FixedDataOperation::Put
            ));
            assert_eq!(
                escaped.reserve(AllocationRequest {
                    kind: AllocationKind::Temporary,
                    bytes: 0,
                    elements: 0,
                    depth: 0,
                    work: 0,
                }),
                Err(AllocationError::Unavailable)
            );
            let alias = fixed.arguments().clone();
            let charged = ledger.snapshot(&identity).unwrap().charged_bytes;
            assert!(charged >= baseline + bound.allocation.bytes);
            drop(fixed);
            drop(escaped);
            reap_custody().unwrap();
            assert_eq!(ledger.snapshot(&identity).unwrap().charged_bytes, charged);
            drop(alias);
            reap_custody().unwrap();
            assert_eq!(ledger.snapshot(&identity).unwrap().charged_bytes, baseline);
            drop(value);
            drop(source);
            reap_custody().unwrap();
            assert_eq!(ledger.snapshot(&identity).unwrap().charged_bytes, 0);
        }
    }

    #[test]
    fn prepaid_network_digest_has_constant_admissions_and_retains_sealed_owner_custody() {
        let _serial = TEST_CUSTODY.lock().unwrap();
        for length in [0usize, 1024, 6144] {
            let directory = tempfile::tempdir().unwrap();
            let root = private_root(&directory);
            let ledger = tobkiri_gate::CapacityLedger::new();
            let cx = context_with_fuel(1_000_000);
            let identity = CapacityOwner::from_context(&cx).unwrap();
            let gate = Arc::new(CountingGate::default());
            let source =
                PackAllocationBudget::create(gate.clone(), &ledger, &root, &cx, Funding::Caller)
                    .unwrap();
            let (request, names) =
                crate::effect_values::network_digest_fixture(&source, Some(length), 2, 1, 2);
            let expected = crate::effect_values::baseline_network_digest(&request, &names, &source);
            let baseline = ledger.snapshot(&identity).unwrap().charged_bytes;
            let before = gate.admissions.load(Ordering::SeqCst);
            let prepared = crate::effect_values::EffectValues(&source)
                .prepare_network_digest(&request, &names)
                .unwrap();
            let bound = prepared.bound();
            let (actual, escaped) = PackAllocationBudget::with_prepaid_conversion(
                gate.clone(),
                &ledger,
                &root,
                &cx,
                Funding::Caller,
                source.graph(),
                bound,
                |owners| Ok((prepared.digest(owners)?, owners.graph().clone())),
            )
            .unwrap();
            assert_eq!(actual, expected);
            // Two paid length-collection admissions, original work admission,
            // and actual carrier capacity admission; none scale with body bytes.
            assert_eq!(gate.admissions.load(Ordering::SeqCst) - before, 4);
            assert_eq!(gate.effects.load(Ordering::SeqCst), 0);
            assert_eq!(
                escaped.reserve(AllocationRequest {
                    kind: AllocationKind::Temporary,
                    bytes: 0,
                    elements: 0,
                    depth: 0,
                    work: 0,
                }),
                Err(AllocationError::Unavailable)
            );
            let charged = ledger.snapshot(&identity).unwrap().charged_bytes;
            assert!(charged >= baseline + bound.bytes);
            let weak = Arc::downgrade(&escaped);
            drop(escaped);
            reap_custody().unwrap();
            assert_eq!(ledger.snapshot(&identity).unwrap().charged_bytes, charged);
            drop(weak);
            reap_custody().unwrap();
            assert_eq!(ledger.snapshot(&identity).unwrap().charged_bytes, baseline);
            drop(request);
            drop(source);
            reap_custody().unwrap();
            assert_eq!(ledger.snapshot(&identity).unwrap().charged_bytes, 0);
        }
    }

    #[test]
    fn prepaid_network_digest_denials_cannot_enter_effects_or_leave_an_open_budget_clone() {
        let _serial = TEST_CUSTODY.lock().unwrap();
        for case in 0..5 {
            let directory = tempfile::tempdir().unwrap();
            let root = private_root(&directory);
            let ledger = tobkiri_gate::CapacityLedger::new();
            let cx = context_with_fuel(1_000_000);
            let identity = CapacityOwner::from_context(&cx).unwrap();
            let gate = Arc::new(CountingGate::default());
            let (source, owner) = PackAllocationBudget::create_inner(
                gate.clone(),
                &ledger,
                &root,
                &cx,
                Funding::Caller,
                None,
            )
            .unwrap();
            let (request, names) =
                crate::effect_values::network_digest_fixture(&source, Some(1024), 2, 1, 2);
            let prepared = crate::effect_values::EffectValues(&source)
                .prepare_network_digest(&request, &names)
                .unwrap();
            let mut bound = prepared.bound();
            match case {
                0 => {
                    let remaining = owner.state.lock().unwrap().work_remaining;
                    source
                        .graph()
                        .reserve(AllocationRequest {
                            kind: AllocationKind::Temporary,
                            bytes: 0,
                            elements: 0,
                            depth: 0,
                            work: remaining.checked_sub(bound.work - 1).unwrap(),
                        })
                        .unwrap();
                }
                1 => bound.bytes = 0,
                2 => bound.work = 1,
                3 => bound.depth = (MAX_DEPTH + 1) as u32,
                4 => (),
                _ => unreachable!(),
            }
            let mut entered = false;
            let mut escaped = None;
            let result = PackAllocationBudget::with_prepaid_conversion(
                gate.clone(),
                &ledger,
                &root,
                &cx,
                Funding::Caller,
                source.graph(),
                bound,
                |owners| {
                    entered = true;
                    escaped = Some(owners.graph().clone());
                    if case == 4 {
                        cx.cancel().cancel();
                    }
                    prepared.digest(owners)
                },
            );
            assert!(result.is_err());
            assert_eq!(entered, matches!(case, 1 | 2 | 4));
            assert_eq!(gate.effects.load(Ordering::SeqCst), 0);
            if let Some(escaped) = escaped {
                assert!(
                    escaped
                        .reserve(AllocationRequest {
                            kind: AllocationKind::Temporary,
                            bytes: 0,
                            elements: 0,
                            depth: 0,
                            work: 0,
                        })
                        .is_err()
                );
            }
            drop(request);
            drop(source);
            drop(owner);
            reap_custody().unwrap();
            assert_eq!(ledger.snapshot(&identity).unwrap().charged_bytes, 0);
        }
    }

    #[test]
    fn prepaid_conversion_rejects_work_before_entry_and_never_replenishes_source() {
        let _serial = TEST_CUSTODY.lock().unwrap();
        let directory = tempfile::tempdir().unwrap();
        let root = private_root(&directory);
        let ledger = tobkiri_gate::CapacityLedger::new();
        let cx = context_with_fuel(3);
        let (source, owner) = PackAllocationBudget::create_inner(
            Arc::new(CountingGate::default()),
            &ledger,
            &root,
            &cx,
            Funding::Caller,
            None,
        )
        .unwrap();
        let entered = std::sync::atomic::AtomicBool::new(false);
        let mut bound = AllocationRequest {
            kind: AllocationKind::Temporary,
            bytes: 128,
            elements: 0,
            depth: 0,
            work: 4,
        };
        assert!(
            PackAllocationBudget::with_prepaid_conversion(
                Arc::new(CountingGate::default()),
                &ledger,
                &root,
                &cx,
                Funding::Caller,
                source.graph(),
                bound,
                |_| {
                    entered.store(true, Ordering::SeqCst);
                    Ok(())
                },
            )
            .is_err()
        );
        assert!(!entered.load(Ordering::SeqCst));
        assert_eq!(owner.state.lock().unwrap().work_remaining, 3);
        bound.work = 2;
        PackAllocationBudget::with_prepaid_conversion(
            Arc::new(CountingGate::default()),
            &ledger,
            &root,
            &cx,
            Funding::Caller,
            source.graph(),
            bound,
            |_| Ok(()),
        )
        .unwrap();
        assert_eq!(owner.state.lock().unwrap().work_remaining, 1);
        assert!(
            PackAllocationBudget::with_prepaid_conversion(
                Arc::new(CountingGate::default()),
                &ledger,
                &root,
                &cx,
                Funding::Caller,
                source.graph(),
                bound,
                |_| Ok(()),
            )
            .is_err()
        );
        drop(source);
        drop(owner);
        reap_custody().unwrap();
    }

    #[test]
    fn prepaid_conversion_bounds_and_cancel_fail_before_any_effect() {
        let _serial = TEST_CUSTODY.lock().unwrap();
        for case in 0..4 {
            let directory = tempfile::tempdir().unwrap();
            let root = private_root(&directory);
            let ledger = tobkiri_gate::CapacityLedger::new();
            let cx = context();
            let gate = Arc::new(CountingGate::default());
            let source =
                PackAllocationBudget::create(gate.clone(), &ledger, &root, &cx, Funding::Caller)
                    .unwrap();
            let bound = AllocationRequest {
                kind: AllocationKind::Temporary,
                bytes: if case == 0 { 1 } else { 4096 },
                elements: 0,
                depth: 0,
                work: if case == 1 { 1 } else { 100 },
            };
            let result = PackAllocationBudget::with_prepaid_conversion(
                gate.clone(),
                &ledger,
                &root,
                &cx,
                Funding::Caller,
                source.graph(),
                bound,
                |owners| {
                    if case == 2 {
                        owners.graph().reserve(AllocationRequest {
                            depth: (MAX_DEPTH + 1) as u32,
                            bytes: 0,
                            work: 0,
                            ..bound
                        })?;
                    }
                    if case == 3 {
                        cx.cancel().cancel();
                    }
                    Ok(CoreValue::string("bounded payload", owners.graph())?)
                },
            );
            assert!(result.is_err());
            assert_eq!(gate.effects.load(Ordering::SeqCst), 0);
            drop(source);
            reap_custody().unwrap();
            assert_eq!(
                ledger
                    .snapshot(&CapacityOwner::from_context(&cx).unwrap())
                    .unwrap()
                    .charged_bytes,
                0
            );
        }
    }

    #[test]
    fn prepaid_conversion_error_and_panic_seal_but_retain_escaped_payload_and_weak() {
        let _serial = TEST_CUSTODY.lock().unwrap();
        for panic in [false, true] {
            let directory = tempfile::tempdir().unwrap();
            let root = private_root(&directory);
            let ledger = tobkiri_gate::CapacityLedger::new();
            let cx = context();
            let identity = CapacityOwner::from_context(&cx).unwrap();
            let source = PackAllocationBudget::create(
                Arc::new(CountingGate::default()),
                &ledger,
                &root,
                &cx,
                Funding::Caller,
            )
            .unwrap();
            let baseline = ledger.snapshot(&identity).unwrap().charged_bytes;
            let mut escaped = None;
            let mut allocator = None;
            let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                PackAllocationBudget::with_prepaid_conversion::<()>(
                    Arc::new(CountingGate::default()),
                    &ledger,
                    &root,
                    &cx,
                    Funding::Caller,
                    source.graph(),
                    AllocationRequest {
                        kind: AllocationKind::Temporary,
                        bytes: 4096,
                        elements: 0,
                        depth: 0,
                        work: 100,
                    },
                    |owners| {
                        escaped = Some(CoreValue::string("retained", owners.graph())?);
                        allocator = Some(owners.graph().clone());
                        if panic {
                            panic!("fixture conversion abort");
                        }
                        anyhow::bail!("fixture conversion error")
                    },
                )
            }));
            assert!(matches!(result, Err(_) | Ok(Err(_))));
            let allocator = allocator.take().unwrap();
            assert!(CoreValue::null(&allocator).is_err());
            let weak = {
                let slots = CUSTODY.lock().unwrap();
                let owner = slots
                    .iter()
                    .flatten()
                    .filter_map(|slot| slot.owner.as_ref())
                    .find(|owner| {
                        matches!(
                            owner.state.lock().unwrap().mode,
                            AllocationMode::Conversion { .. }
                        )
                    })
                    .unwrap();
                Arc::downgrade(owner)
            };
            let charged = ledger.snapshot(&identity).unwrap().charged_bytes;
            drop(allocator);
            reap_custody().unwrap();
            assert_eq!(ledger.snapshot(&identity).unwrap().charged_bytes, charged);
            drop(escaped.take());
            reap_custody().unwrap();
            assert_eq!(ledger.snapshot(&identity).unwrap().charged_bytes, charged);
            drop(weak);
            reap_custody().unwrap();
            assert_eq!(ledger.snapshot(&identity).unwrap().charged_bytes, baseline);
            drop(source);
            reap_custody().unwrap();
        }
    }

    #[test]
    fn prepaid_byte_validation_is_funded_and_rejects_non_u8_without_effects() {
        let _serial = TEST_CUSTODY.lock().unwrap();
        let directory = tempfile::tempdir().unwrap();
        let root = private_root(&directory);
        let ledger = tobkiri_gate::CapacityLedger::new();
        let cx = context();
        let gate = Arc::new(CountingGate::default());
        let source =
            PackAllocationBudget::create(gate.clone(), &ledger, &root, &cx, Funding::Caller)
                .unwrap();
        let value = crate::effect_values::EffectValues(&source)
            .list(
                [
                    wasmtime::component::Val::U8(1),
                    wasmtime::component::Val::U16(2),
                ]
                .into_iter(),
            )
            .unwrap();
        let bound = CoreValue::kv_put_conversion_bound(3, 2).unwrap();
        let mut entered = false;
        let result = PackAllocationBudget::with_prepaid_conversion(
            gate.clone(),
            &ledger,
            &root,
            &cx,
            Funding::Caller,
            source.graph(),
            bound.allocation,
            |owners| {
                entered = true;
                crate::effects::fixed_kv_put_conversion(
                    "key",
                    &value,
                    owners,
                    bound.validation_work,
                )
            },
        );
        assert!(entered);
        assert!(result.is_err());
        assert_eq!(gate.effects.load(Ordering::SeqCst), 0);
        drop(value);
        drop(source);
        reap_custody().unwrap();
    }
}
