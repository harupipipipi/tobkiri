//! Infrastructure for managing "host tables" of resources in the component
//! model.
//!
//! This module is mostly an implementation detail of `ResourceAny` where it
//! tracks the ability for the host to have a handle to any resource, be it
//! either guest-defined or host-defined. The `HostResourceData` type is
//! stored inside of a `Store<T>` and `HostResourceTables` provides temporary
//! access to this information.
//!
//! This provides operations in terms of "lift" and "lower" terminology where
//! the idea is that a resource is "lowered" to the host meaning that metadata
//! about the resource is inserted into `HostResourceData` through
//! `HostResourceTables`. This produces a `HostResourceIndex` which can
//! then be used to access it later on. The `HostResourceIndex` can later be
//! used to "lift" the resource from the table back into its constituent parts
//! or remove it from the table.
//!
//! This tracks borrow state where you can, for example, lift a borrow from a
//! `HostResourceIndex` that represents an "own". This will activate
//! borrow-tracking infrastructure to ensure that the borrow is deleted before
//! the own is removed or otherwise taken out.

use crate::component::FundedVec;
use crate::prelude::*;
use crate::runtime::component::RuntimeInstance;
use crate::runtime::vm::component::{
    PendingActivation, PendingOwnerKey, PendingOwnTicket, PendingTransferError,
    PendingTransferStage, ResourceTables, TypedResource, TypedResourceIndex,
};
use crate::runtime::vm::{SendSyncPtr, VMFuncRef};
use crate::store::StoreOpaque;
use core::ptr::NonNull;
use wasmtime_environ::component::TypeResourceTableIndex;

/// Metadata tracking the state of resources within a `Store`.
///
/// This is a borrowed structure created from a `Store` piecemeal from below.
/// The `ResourceTables` type holds most of the raw information and this
/// structure tacks on a reference to `HostResourceData` to track generation
/// numbers of host indices.
pub struct HostResourceTables<'a> {
    tables: ResourceTables<'a>,
    host_resource_data: &'a mut HostResourceData,
}

/// Metadata for host-owned resources owned within a `Store`.
///
/// This metadata is used to prevent the ABA problem with indices handed out as
/// part of `Resource` and `ResourceAny`. Those structures are `Copy` meaning
/// that it's easy to reuse them, possibly accidentally. To prevent issues in
/// the host Wasmtime attaches both an index (within `ResourceTables`) as well
/// as a 32-bit generation counter onto each `HostResourceIndex` which the host
/// actually holds in `Resource` and `ResourceAny`.
///
/// This structure holds a list which is a parallel list to the "list of reps"
/// that's stored within `ResourceTables` elsewhere in the `Store`. This
/// parallel list holds the last known generation of each element in the table.
/// The generation is then compared on access to make sure it's the same.
///
/// Whenever a slot in the table is allocated the `cur_generation` field is
/// pushed at the corresponding index of `generation_of_table_slot`. Whenever
/// a field is accessed the current value of `generation_of_table_slot` is
/// checked against the generation of the index. Whenever a slot is deallocated
/// the generation is incremented. Put together this means that any access of a
/// deallocated slot should deterministically provide an error.
#[derive(Default)]
pub struct HostResourceData {
    cur_generation: u32,
    table_slot_metadata: FundedVec<TableSlot>,
}

#[derive(Copy, Clone)]
pub struct TableSlot {
    generation: u32,
    pub(super) instance: Option<RuntimeInstance>,
    pub(super) dtor: Option<SendSyncPtr<VMFuncRef>>,
}

/// Host representation of an index into a table slot.
///
/// This is morally (u32, u32) but is encoded as a 64-bit integer. The low
/// 32-bits are the table index and the upper 32-bits are the generation
/// counter.
#[derive(PartialEq, Eq, Debug, Copy, Clone)]
#[repr(transparent)]
pub struct HostResourceIndex(u64);

impl HostResourceIndex {
    pub(super) fn new(idx: u32, generation: u32) -> HostResourceIndex {
        HostResourceIndex(u64::from(idx) | (u64::from(generation) << 32))
    }

    pub(super) const fn index(&self) -> u32 {
        (self.0 & 0xffffffff) as u32
    }

    pub(super) const fn generation(&self) -> u32 {
        (self.0 >> 32) as u32
    }
}

/// Host-side custody for a pending `own` entry created through
/// [`HostResourceTables::host_resource_lower_own_pending`].
///
/// Bundles the raw-table [`PendingOwnTicket`] with the generation-stamped
/// [`HostResourceIndex`] so that both the slot identity and the host
/// metadata generation are validated before any mutation. Deliberately not
/// `Copy` or `Clone`: it carries rollback custody. Dropping it abandons
/// custody only; the entry stays pending and funded.
#[allow(dead_code)]
#[derive(Debug)]
pub(crate) struct PendingHostOwnTicket {
    ticket: PendingOwnTicket,
    index: HostResourceIndex,
}

#[allow(dead_code)]
impl PendingHostOwnTicket {
    /// Trusted crate-private constructor; the pending lowering path is the
    /// sole production caller.
    pub(crate) fn new(ticket: PendingOwnTicket, index: HostResourceIndex) -> Self {
        Self { ticket, index }
    }

    /// The generation-stamped host index of the pending entry.
    pub(crate) fn host_index(&self) -> HostResourceIndex {
        self.index
    }

    /// The raw-table custody ticket.
    pub(crate) fn ticket(&self) -> &PendingOwnTicket {
        &self.ticket
    }

    /// The provisional owner record this entry belongs to.
    pub(crate) fn owner(&self) -> PendingOwnerKey {
        self.ticket.owner()
    }
}

impl<'a> HostResourceTables<'a> {
    pub fn new_host(store: &'a mut StoreOpaque) -> Result<HostResourceTables<'a>> {
        let (tables, data) = store.component_resource_tables_and_host_resource_data(None)?;
        Ok(HostResourceTables::from_parts(tables, data))
    }

    pub fn from_parts(
        tables: ResourceTables<'a>,
        host_resource_data: &'a mut HostResourceData,
    ) -> Self {
        HostResourceTables {
            tables,
            host_resource_data,
        }
    }

    /// Lifts an `own` resource that resides in the host's tables at the `idx`
    /// specified into its `rep`.
    ///
    /// # Errors
    ///
    /// Returns an error if `idx` doesn't point to a valid owned resource, or
    /// if `idx` can't be lifted as an `own` (e.g. it has active borrows).
    pub fn host_resource_lift_own(&mut self, idx: HostResourceIndex) -> Result<u32> {
        // Reject pending entries before any generation bookkeeping runs so
        // the entry stays reachable for record-authorized rollback.
        self.tables
            .resource_reject_pending(TypedResourceIndex::Host(idx.index()))?;
        let (idx, _) = self.validate_host_index(idx, true)?;
        self.tables.resource_lift_own(TypedResourceIndex::Host(idx))
    }

    /// See [`HostResourceTables::host_resource_lift_own`].
    pub fn host_resource_lift_borrow(&mut self, idx: HostResourceIndex) -> Result<u32> {
        let (idx, _) = self.validate_host_index(idx, false)?;
        self.tables
            .resource_lift_borrow(TypedResourceIndex::Host(idx))
    }

    /// Lowers an `own` resource to be owned by the host.
    ///
    /// This returns a new index into the host's set of resource tables which
    /// will point to the `rep` specified. The returned index is suitable for
    /// conversion into either [`Resource`] or [`ResourceAny`].
    ///
    /// The `dtor` and instance `instance` are specified as well to know what
    /// destructor to run when this resource is destroyed.
    pub fn host_resource_lower_own(
        &mut self,
        rep: u32,
        dtor: Option<NonNull<VMFuncRef>>,
        instance: Option<RuntimeInstance>,
    ) -> Result<HostResourceIndex> {
        let idx = self.tables.resource_lower_own(TypedResource::Host(rep))?;
        match self.new_host_index(idx, dtor, instance) {
            Ok(index) => Ok(index),
            Err(error) => {
                // This exact own was just inserted with no active lends. Our
                // exclusive access prevents it from changing while metadata is
                // allocated, so it remains private until an index is returned.
                let index = TypedResourceIndex::Host(idx);
                if self.tables.resource_rep(index)? != rep {
                    bail!("new host resource changed before metadata rollback");
                }
                let recovered_rep = self.tables.resource_lift_own(index)?;
                if recovered_rep != rep {
                    bail!("host resource metadata rollback returned a different representation");
                }
                // Removing occupancy keeps any live table capacity funded. No
                // host handle escaped, so the caller still manages the rep.
                Err(error)
            }
        }
    }

    /// Lowers an `own` resource to be provisionally owned by the host.
    ///
    /// Like [`HostResourceTables::host_resource_lower_own`] but the new entry
    /// is *pending*: all ordinary access rejects it until a prepared
    /// activation flips it active through the returned custody. `owner`
    /// identifies the reserved store-side record authorized to roll back,
    /// transfer, or activate the entry.
    ///
    /// On metadata-funding denial the pending entry is rolled back through
    /// its own ticket, preserving the original `HostAllocationError`
    /// exactly like the active-entry path above.
    #[allow(dead_code)]
    pub(crate) fn host_resource_lower_own_pending(
        &mut self,
        rep: u32,
        dtor: Option<NonNull<VMFuncRef>>,
        instance: Option<RuntimeInstance>,
        owner: PendingOwnerKey,
    ) -> Result<PendingHostOwnTicket> {
        let ticket = self
            .tables
            .resource_pending_own_insert(TypedResource::Host(rep), owner)?;
        match self.new_host_index(ticket.index().raw_index(), dtor, instance) {
            Ok(index) => Ok(PendingHostOwnTicket::new(ticket, index)),
            Err(error) => {
                // This exact pending entry was just inserted with no other
                // handle to it; exclusive access keeps it private until a
                // ticket is returned.
                if let Err(rollback) = self.tables.resource_pending_remove(&ticket) {
                    bail!(
                        "pending host resource metadata allocation failed ({error:#}) \
                         and its rollback failed too ({rollback:#}); \
                         the entry remains pending and retained"
                    );
                }
                // Occupancy is removed while retained capacity stays funded,
                // same as the active-entry rollback.
                Err(error)
            }
        }
    }

    /// Record-authorized rollback of a pending host `own` entry.
    ///
    /// Validates the generation-stamped host index and the raw-table ticket
    /// before mutating; stale or foreign custody is a closed error. No
    /// destructor is dispatched and no borrow bookkeeping is touched.
    #[allow(dead_code)]
    pub(crate) fn host_resource_pending_remove(
        &mut self,
        pending: &PendingHostOwnTicket,
    ) -> Result<u32> {
        // Check the generation without bumping it first: failing here must
        // not retire a still-pending entry's index.
        self.validate_host_index(pending.index, false)?;
        // Then check the raw slot identity before mutating anything.
        self.tables.resource_pending_validate(pending.ticket())?;
        // Both confirmed: remove the raw entry, then retire the host index.
        let rep = self.tables.resource_pending_remove(pending.ticket())?;
        self.validate_host_index(pending.index, true)?;
        Ok(rep)
    }

    /// Validates a pending host `own` entry and returns an exclusive
    /// activation guard for it.
    ///
    /// All fallible checks happen here: the host-index generation, the raw
    /// slot's owner key, typed index, representation, and lend state. The
    /// returned guard's activation is infallible and performs no
    /// reservations; dropping it leaves the entry pending and charged.
    ///
    /// This does not authenticate any acknowledgement and is not wired to a
    /// production caller; later stages invoke it only after a real
    /// same-permit ACK and complete access closure.
    #[allow(dead_code)]
    pub(crate) fn host_resource_pending_prepare(
        &mut self,
        pending: &PendingHostOwnTicket,
    ) -> Result<PendingActivation<'_>> {
        self.validate_host_index(pending.index, false)?;
        self.tables.resource_pending_prepare(pending.ticket())
    }

    /// Removes only the exact record-owned pending guest entry. Host metadata
    /// stays private; the existing raw operation validates every ticket field.
    pub(crate) fn guest_resource_pending_remove(
        &mut self,
        pending: &PendingOwnTicket,
    ) -> Result<u32> {
        if !matches!(pending.index(), TypedResourceIndex::Component { .. }) {
            bail!("pending guest ticket required");
        }
        self.tables.resource_pending_remove(pending)
    }

    /// Prepare only an exact pending guest ticket without exposing raw tables.
    pub(crate) fn guest_resource_pending_prepare(
        &mut self,
        pending: &PendingOwnTicket,
    ) -> Result<PendingActivation<'_>> {
        if !matches!(pending.index(), TypedResourceIndex::Component { .. }) {
            bail!("pending guest ticket required");
        }
        self.tables.resource_pending_prepare(pending)
    }

    /// Transfers a pending host `own` entry into the guest table `ty`.
    ///
    /// The destination pending entry is inserted and funded before the
    /// confirmed source is removed, so destination funding denial leaves the
    /// source exactly pending and occupied. On success the source
    /// `HostResourceIndex` is retired and the guest-side custody ticket is
    /// returned; the entry remains pending throughout.
    #[allow(dead_code)]
    pub(crate) fn host_resource_pending_transfer_to_guest(
        &mut self,
        pending: &PendingHostOwnTicket,
        ty: TypeResourceTableIndex,
    ) -> Result<PendingOwnTicket, PendingTransferError> {
        // Staleness check only; no bookkeeping is mutated yet.
        if let Err(error) = self.validate_host_index(pending.index, false) {
            return Err(PendingTransferError {
                error,
                stage: PendingTransferStage::SourceValidation,
                destination: None,
            });
        }
        let dst = TypedResource::Component {
            ty,
            rep: pending.ticket().rep(),
        };
        match self
            .tables
            .resource_pending_transfer_own(pending.ticket(), dst)
        {
            Ok(destination) => {
                // The source slot is gone for good; retire its host index.
                if let Err(error) = self.validate_host_index(pending.index, true) {
                    return Err(PendingTransferError {
                        error,
                        stage: PendingTransferStage::SourceRemove,
                        destination: Some(destination),
                    });
                }
                Ok(destination)
            }
            Err(failure) => Err(failure),
        }
    }

    /// See [`HostResourceTables::host_resource_lower_own`].
    pub fn host_resource_lower_borrow(&mut self, rep: u32) -> Result<HostResourceIndex> {
        let idx = self
            .tables
            .resource_lower_borrow(TypedResource::Host(rep))?;
        Ok(self.new_host_index(idx, None, None)?)
    }

    /// Validates that `idx` is still valid for the host tables, notably
    /// ensuring that the generation listed in `idx` is the same as the
    /// last recorded generation of the slot itself.
    ///
    /// The `is_removal` option indicates whether or not this table access will
    /// end up removing the element from the host table. In such a situation the
    /// current generation number is incremented.
    fn validate_host_index(
        &mut self,
        idx: HostResourceIndex,
        is_removal: bool,
    ) -> Result<(u32, Option<TableSlot>)> {
        let actual = usize::try_from(idx.index())
            .ok()
            .and_then(|i| self.host_resource_data.table_slot_metadata.get(i).copied());

        // If `idx` is out-of-bounds then skip returning an error. In such a
        // situation the operation that this is guarding will return a more
        // precise error, such as a lift operation.
        if let Some(actual) = actual {
            if actual.generation != idx.generation() {
                bail!("host-owned resource was already de-allocated");
            }
        }

        // Bump the current generation of this is a removal to ensure any
        // future item placed in this slot can't be pointed to by the `idx`
        // provided above.
        if is_removal {
            self.host_resource_data.cur_generation += 1;
        }

        Ok((idx.index(), actual))
    }

    /// Creates a new `HostResourceIndex` which will point to the raw table
    /// slot provided by `idx`.
    ///
    /// This will register metadata necessary to track the current generation
    /// in the returned `HostResourceIndex` as well.
    fn new_host_index(
        &mut self,
        idx: u32,
        dtor: Option<NonNull<VMFuncRef>>,
        instance: Option<RuntimeInstance>,
    ) -> Result<HostResourceIndex> {
        let list = &mut self.host_resource_data.table_slot_metadata;
        let info = TableSlot {
            generation: self.host_resource_data.cur_generation,
            instance,
            dtor: dtor.map(SendSyncPtr::new),
        };
        match list.get_mut(idx as usize) {
            Some(slot) => *slot = info,
            None => {
                // Resource handles start at 1, not zero, so push two elements
                // for the first resource handle.
                if list.is_empty() {
                    assert_eq!(idx, 1);
                    list.push(
                        TableSlot {
                            generation: 0,
                            instance: None,
                            dtor: None,
                        },
                        &self.tables.allocation_owner,
                    )?;
                }
                assert_eq!(idx as usize, list.len());
                list.push(info, &self.tables.allocation_owner)?;
            }
        }

        Ok(HostResourceIndex::new(idx, info.generation))
    }

    /// Drops a host-owned resource from host tables.
    ///
    /// This method will attempt to interpret `idx` as pointing to either a
    /// `borrow` or `own` resource with the `expected` type specified. This
    /// method will then return the underlying `rep` if it points to an `own`
    /// resource which can then be further processed for destruction.
    ///
    /// # Errors
    ///
    /// Returns an error if `idx` doesn't point to a valid resource, points to
    /// an `own` with active borrows, or if it doesn't have the type `expected`
    /// in the host tables.
    pub(super) fn host_resource_drop(
        &mut self,
        idx: HostResourceIndex,
    ) -> Result<Option<(u32, TableSlot)>> {
        // Reject pending entries before the removal bookkeeping below runs,
        // so provisional entries cannot be dropped through this path and
        // their host index stays reachable for authorized operations.
        self.tables
            .resource_reject_pending(TypedResourceIndex::Host(idx.index()))?;
        let (idx, slot) = self.validate_host_index(idx, true)?;
        match self.tables.resource_drop(TypedResourceIndex::Host(idx))? {
            Some(rep) => Ok(Some((rep, slot.unwrap()))),
            None => Ok(None),
        }
    }

    /// Lowers an `own` resource into the guest, converting the `rep` specified
    /// into a guest-local index.
    ///
    /// The `ty` provided is which table to put this into.
    pub fn guest_resource_lower_own(
        &mut self,
        rep: u32,
        ty: TypeResourceTableIndex,
    ) -> Result<u32> {
        self.tables
            .resource_lower_own(TypedResource::Component { ty, rep })
    }

    /// Lowers a `borrow` resource into the guest, converting the `rep`
    /// specified into a guest-local index.
    ///
    /// The `ty` provided is which table to put this into.
    ///
    /// Note that this cannot be used in isolation because lowering a borrow
    /// into a guest has a special case where `rep` is returned directly if `ty`
    /// belongs to the component being lowered into. That property must be
    /// handled by the caller of this function.
    pub fn guest_resource_lower_borrow(
        &mut self,
        rep: u32,
        ty: TypeResourceTableIndex,
    ) -> Result<u32> {
        self.tables
            .resource_lower_borrow(TypedResource::Component { ty, rep })
    }

    /// Lifts an `own` resource from the `idx` specified from the table `ty`.
    ///
    /// This will lookup the appropriate table in the guest and return the `rep`
    /// corresponding to `idx` if it's valid.
    pub fn guest_resource_lift_own(
        &mut self,
        index: u32,
        ty: TypeResourceTableIndex,
    ) -> Result<u32> {
        self.tables
            .resource_lift_own(TypedResourceIndex::Component { ty, index })
    }

    /// Lifts a `borrow` resource from the `idx` specified from the table `ty`.
    ///
    /// This will lookup the appropriate table in the guest and return the `rep`
    /// corresponding to `idx` if it's valid.
    pub fn guest_resource_lift_borrow(
        &mut self,
        index: u32,
        ty: TypeResourceTableIndex,
    ) -> Result<u32> {
        self.tables
            .resource_lift_borrow(TypedResourceIndex::Component { ty, index })
    }

    /// Completes a call into the component instance, validating that it's ok to
    /// complete by ensuring the are no remaining active borrows.
    #[inline]
    pub fn validate_scope_exit(&mut self) -> Result<()> {
        self.tables.validate_scope_exit()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::component::store::ComponentTaskState;
    use crate::component::{
        HostAllocationBudget, HostAllocationError, HostAllocationKind, HostAllocationOwner,
        HostAllocationRequest,
    };
    use crate::runtime::vm::component::{HandleTable, RemovedResource};
    use crate::store::StoreId;
    use alloc::sync::Arc;
    use core::sync::atomic::{AtomicU64, AtomicUsize, Ordering};

    struct Budget {
        requests: AtomicUsize,
        deny_at: AtomicUsize,
        used: AtomicU64,
    }

    impl Budget {
        fn new(deny_at: usize) -> Self {
            Self {
                requests: AtomicUsize::new(0),
                deny_at: AtomicUsize::new(deny_at),
                used: AtomicU64::new(0),
            }
        }

        fn allow(&self) {
            self.deny_at.store(0, Ordering::SeqCst);
        }
    }

    impl HostAllocationBudget for Budget {
        fn reserve(
            &self,
            request: HostAllocationRequest,
        ) -> core::result::Result<(), HostAllocationError> {
            assert_eq!(request.kind, HostAllocationKind::Resource);
            let ordinal = self.requests.fetch_add(1, Ordering::SeqCst) + 1;
            if ordinal == self.deny_at.load(Ordering::SeqCst) {
                return Err(HostAllocationError::Limit);
            }
            self.used
                .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |used| {
                    used.checked_add(request.bytes)
                })
                .map(|_| ())
                .map_err(|_| HostAllocationError::Overflow)
        }

        fn release(&self, bytes: u64) {
            self.used
                .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |used| {
                    used.checked_sub(bytes)
                })
                .expect("release only accepted, physically freed storage");
        }
    }

    struct Tables {
        owner: HostAllocationOwner,
        raw: HandleTable,
        data: HostResourceData,
        task_state: ComponentTaskState,
    }

    impl Tables {
        fn new(budget: &Arc<Budget>) -> Self {
            Self {
                owner: budget.clone(),
                raw: HandleTable::default(),
                data: HostResourceData::default(),
                task_state: ComponentTaskState::NotConcurrent(Default::default()),
            }
        }

        fn host(&mut self) -> HostResourceTables<'_> {
            HostResourceTables::from_parts(
                ResourceTables {
                    allocation_owner: self.owner.clone(),
                    guest: None,
                    host_table: &mut self.raw,
                    task_state: &mut self.task_state,
                    current_scope_id: None,
                },
                &mut self.data,
            )
        }
    }

    fn assert_limit(error: &crate::Error) {
        assert!(
            matches!(
                error.downcast_ref::<HostAllocationError>(),
                Some(HostAllocationError::Limit)
            ),
            "{error:#}"
        );
    }

    fn key(serial: u64) -> PendingOwnerKey {
        PendingOwnerKey::new_stage_a_seam(StoreId::allocate(), serial).unwrap()
    }

    #[test]
    fn lower_own_rolls_back_when_dummy_metadata_funding_is_denied() {
        // Reservations are raw slot, metadata dummy slot, metadata real slot.
        let budget = Arc::new(Budget::new(2));
        {
            let mut tables = Tables::new(&budget);
            let error = tables
                .host()
                .host_resource_lower_own(10, None, None)
                .unwrap_err();
            assert_limit(&error);
            assert_eq!(budget.requests.load(Ordering::SeqCst), 2);
            assert!(tables.raw.is_empty());
            assert!(tables.data.table_slot_metadata.is_empty());
            // Logical rollback must not release retained raw table capacity.
            assert!(budget.used.load(Ordering::SeqCst) > 0);

            budget.allow();
            let index = tables
                .host()
                .host_resource_lower_own(20, None, None)
                .unwrap();
            assert_eq!(index.index(), 1);
            assert_eq!(tables.host().host_resource_lift_own(index).unwrap(), 20);
            assert!(tables.raw.is_empty());

            let replacement = tables
                .host()
                .host_resource_lower_own(30, None, None)
                .unwrap();
            assert_eq!(replacement.index(), index.index());
            assert_ne!(replacement.generation(), index.generation());
            assert!(tables.host().host_resource_lift_own(index).is_err());
            assert_eq!(
                tables.host().host_resource_lift_own(replacement).unwrap(),
                30
            );
            assert!(tables.raw.is_empty());
        }
        assert_eq!(budget.used.load(Ordering::SeqCst), 0);
    }

    #[test]
    fn lower_own_rolls_back_when_real_metadata_funding_is_denied() {
        let budget = Arc::new(Budget::new(3));
        {
            let mut tables = Tables::new(&budget);
            let error = tables
                .host()
                .host_resource_lower_own(40, None, None)
                .unwrap_err();
            assert_limit(&error);
            assert_eq!(budget.requests.load(Ordering::SeqCst), 3);
            assert!(tables.raw.is_empty());
            // Dummy metadata remains real funded storage after denial.
            assert_eq!(tables.data.table_slot_metadata.len(), 1);
            assert!(budget.used.load(Ordering::SeqCst) > 0);

            budget.allow();
            let index = tables
                .host()
                .host_resource_lower_own(50, None, None)
                .unwrap();
            assert_eq!(index.index(), 1);
            assert_eq!(tables.data.table_slot_metadata.len(), 2);
            let (rep, slot) = tables.host().host_resource_drop(index).unwrap().unwrap();
            assert_eq!(rep, 50);
            assert!(slot.instance.is_none());
            assert!(slot.dtor.is_none());
            assert!(tables.raw.is_empty());
            assert!(tables.host().host_resource_drop(index).is_err());
        }
        assert_eq!(budget.used.load(Ordering::SeqCst), 0);
    }

    #[test]
    fn lower_own_metadata_denial_preserves_other_handles_and_reuses_failed_slot() {
        let budget = Arc::new(Budget::new(0));
        {
            let mut tables = Tables::new(&budget);
            let first = tables
                .host()
                .host_resource_lower_own(60, None, None)
                .unwrap();
            assert_eq!(first.index(), 1);
            assert_eq!(budget.requests.load(Ordering::SeqCst), 3);
            // Grow raw storage successfully, then deny new host metadata.
            budget.deny_at.store(5, Ordering::SeqCst);
            let error = tables
                .host()
                .host_resource_lower_own(70, None, None)
                .unwrap_err();
            assert_limit(&error);
            assert_eq!(budget.requests.load(Ordering::SeqCst), 5);
            assert_eq!(tables.data.table_slot_metadata.len(), 2);
            assert_eq!(
                tables
                    .raw
                    .resource_rep(TypedResourceIndex::Host(first.index()))
                    .unwrap(),
                60
            );
            assert!(
                tables
                    .raw
                    .resource_rep(TypedResourceIndex::Host(2))
                    .is_err()
            );

            budget.allow();
            let retry = tables
                .host()
                .host_resource_lower_own(80, None, None)
                .unwrap();
            assert_eq!(retry.index(), 2);
            assert_eq!(tables.host().host_resource_lift_own(first).unwrap(), 60);
            assert_eq!(tables.host().host_resource_lift_own(retry).unwrap(), 80);
            assert!(tables.raw.is_empty());
            assert!(budget.used.load(Ordering::SeqCst) > 0);
        }
        assert_eq!(budget.used.load(Ordering::SeqCst), 0);
    }

    #[test]
    fn lower_own_raw_funding_denial_leaves_metadata_and_table_empty() {
        let budget = Arc::new(Budget::new(1));
        {
            let mut tables = Tables::new(&budget);
            let error = tables
                .host()
                .host_resource_lower_own(90, None, None)
                .unwrap_err();
            assert_limit(&error);
            assert_eq!(budget.requests.load(Ordering::SeqCst), 1);
            assert!(tables.raw.is_empty());
            assert!(tables.data.table_slot_metadata.is_empty());
            assert_eq!(budget.used.load(Ordering::SeqCst), 0);

            budget.allow();
            let index = tables
                .host()
                .host_resource_lower_own(100, None, None)
                .unwrap();
            assert_eq!(index.index(), 1);
            assert_eq!(tables.host().host_resource_lift_own(index).unwrap(), 100);
            assert!(tables.raw.is_empty());
        }
        assert_eq!(budget.used.load(Ordering::SeqCst), 0);
    }

    #[test]
    fn pending_host_own_rejects_ordinary_access_until_authorized_rollback() {
        let budget = Arc::new(Budget::new(0));
        {
            let mut tables = Tables::new(&budget);
            let owner = key(1);
            let pending = tables
                .host()
                .host_resource_lower_own_pending(77, None, None, owner)
                .unwrap();
            let hri = pending.host_index();
            assert_eq!(hri.index(), 1);
            assert_eq!(pending.owner(), owner);
            assert_eq!(pending.ticket().rep(), 77);

            // Every ordinary access path rejects the pending entry first.
            assert!(tables.host().host_resource_lift_own(hri).is_err());
            assert!(tables.host().host_resource_lift_borrow(hri).is_err());
            assert!(tables.host().host_resource_drop(hri).is_err());
            assert!(tables
                .raw
                .resource_rep(TypedResourceIndex::Host(hri.index()))
                .is_err());
            assert!(tables
                .raw
                .resource_lend(TypedResourceIndex::Host(hri.index()))
                .is_err());
            assert!(tables
                .raw
                .resource_undo_lend(TypedResourceIndex::Host(hri.index()))
                .is_err());
            assert!(tables
                .raw
                .remove_resource(TypedResourceIndex::Host(hri.index()))
                .is_err());

            // The rejected accesses did not consume the entry: authorized
            // validation still passes and rollback succeeds exactly once.
            tables
                .raw
                .resource_pending_validate(pending.ticket())
                .unwrap();
            assert_eq!(
                tables.host().host_resource_pending_remove(&pending).unwrap(),
                77
            );
            assert!(tables
                .host()
                .host_resource_pending_remove(&pending)
                .is_err());
            assert!(tables.raw.is_empty());
            assert!(tables.host().host_resource_drop(hri).is_err());
        }
        assert_eq!(budget.used.load(Ordering::SeqCst), 0);
    }

    #[test]
    fn pending_foreign_and_stale_custody_are_closed_errors() {
        let budget = Arc::new(Budget::new(0));
        {
            let mut tables = Tables::new(&budget);
            // A zero serial can never mint a key.
            assert!(PendingOwnerKey::new_stage_a_seam(StoreId::allocate(), 0).is_err());
            let store = StoreId::allocate();
            let owner = PendingOwnerKey::new_stage_a_seam(store, 1).unwrap();
            let pending = tables
                .host()
                .host_resource_lower_own_pending(9, None, None, owner)
                .unwrap();
            let hri = pending.host_index();
            let index = TypedResourceIndex::Host(hri.index());

            // A ticket naming a foreign store, or a different serial in the
            // same store, cannot touch the entry.
            for foreign_owner in [
                PendingOwnerKey::new_stage_a_seam(StoreId::allocate(), 1).unwrap(),
                PendingOwnerKey::new_stage_a_seam(store, 2).unwrap(),
            ] {
                let foreign = PendingHostOwnTicket::new(
                    PendingOwnTicket::new(foreign_owner, index, 9),
                    hri,
                );
                assert!(tables
                    .host()
                    .host_resource_pending_remove(&foreign)
                    .is_err());
                assert!(tables
                    .host()
                    .host_resource_pending_prepare(&foreign)
                    .is_err());
            }

            // A wrong rep or a wrong index shape is a closed error too;
            // check at the raw table to avoid guest-table routing.
            let wrong_rep = PendingOwnTicket::new(owner, index, 10);
            assert!(tables.raw.resource_pending_validate(&wrong_rep).is_err());
            let wrong_shape = PendingOwnTicket::new(
                owner,
                TypedResourceIndex::Component {
                    index: hri.index(),
                    ty: TypeResourceTableIndex::from_u32(0),
                },
                9,
            );
            assert!(tables.raw.resource_pending_validate(&wrong_shape).is_err());

            // The entry survived every rejected access.
            tables
                .raw
                .resource_pending_validate(pending.ticket())
                .unwrap();

            // After rollback plus slot reuse the retired host index is stale.
            tables.host().host_resource_pending_remove(&pending).unwrap();
            let replacement = tables
                .host()
                .host_resource_lower_own_pending(19, None, None, owner)
                .unwrap();
            assert_eq!(replacement.host_index().index(), hri.index());
            assert_ne!(replacement.host_index().generation(), hri.generation());
            let stale = PendingHostOwnTicket::new(
                PendingOwnTicket::new(owner, index, 19),
                hri,
            );
            assert!(tables.host().host_resource_pending_remove(&stale).is_err());
            assert!(tables
                .host()
                .host_resource_pending_prepare(&stale)
                .is_err());

            // Ordinary active entries are unaffected by all of this.
            let active = tables.host().host_resource_lower_own(5, None, None).unwrap();
            assert_eq!(tables.host().host_resource_lift_own(active).unwrap(), 5);
            tables
                .host()
                .host_resource_pending_remove(&replacement)
                .unwrap();
            assert!(tables.raw.is_empty());
        }
        assert_eq!(budget.used.load(Ordering::SeqCst), 0);
    }

    #[test]
    fn pending_guest_own_rejects_ordinary_access_then_activates() {
        let budget = Arc::new(Budget::new(0));
        {
            let owner: HostAllocationOwner = budget.clone();
            let mut guest = HandleTable::default();
            guest.set_allocation_owner(&owner);
            let owner_key = key(7);
            let ty = TypeResourceTableIndex::from_u32(3);
            let wrong_ty = TypeResourceTableIndex::from_u32(4);
            let ticket = guest
                .resource_own_insert_pending(TypedResource::Component { ty, rep: 55 }, owner_key)
                .unwrap();
            let index = ticket.index().raw_index();
            assert_eq!(index, 1);

            // Ordinary typed accesses reject while pending.
            let typed = TypedResourceIndex::Component { index, ty };
            assert!(guest.resource_rep(typed).is_err());
            assert!(guest.resource_lend(typed).is_err());
            assert!(guest.resource_undo_lend(typed).is_err());
            assert!(guest.remove_resource(typed).is_err());
            let wrong = TypedResourceIndex::Component {
                index,
                ty: wrong_ty,
            };
            assert!(guest.resource_rep(wrong).is_err());
            assert!(guest.remove_resource(wrong).is_err());
            // Non-resource handle kinds reject it as well.
            assert!(guest.waitable_rep(index).is_err());
            assert!(guest.error_context_rep(index).is_err());
            // A ticket with a foreign key, wrong type, or wrong rep fails.
            assert!(guest
                .resource_pending_validate(&PendingOwnTicket::new(key(8), typed, 55))
                .is_err());
            assert!(guest
                .resource_pending_validate(&PendingOwnTicket::new(owner_key, wrong, 55))
                .is_err());
            assert!(guest
                .resource_pending_validate(&PendingOwnTicket::new(owner_key, typed, 56))
                .is_err());

            // Prepared activation flips it to an ordinary active entry.
            guest.resource_pending_prepare(&ticket).unwrap().activate();
            assert_eq!(guest.resource_rep(typed).unwrap(), 55);
            assert!(guest.resource_pending_validate(&ticket).is_err());
            match guest.remove_resource(typed).unwrap() {
                RemovedResource::Own { rep } => assert_eq!(rep, 55),
                _ => panic!("expected an `own` resource"),
            }
            assert!(guest.is_empty());
        }
        assert_eq!(budget.used.load(Ordering::SeqCst), 0);
    }

    #[test]
    fn pending_transfer_denied_destination_funding_is_recovered() {
        let budget = Arc::new(Budget::new(0));
        {
            let mut tables = Tables::new(&budget);
            let owner = key(5);
            let active = tables.host().host_resource_lower_own(60, None, None).unwrap();
            let pending = tables
                .host()
                .host_resource_lower_own_pending(70, None, None, owner)
                .unwrap();
            assert_eq!(pending.host_index().index(), 2);
            let requests = budget.requests.load(Ordering::SeqCst);
            budget.deny_at.store(requests + 1, Ordering::SeqCst);

            let failure = {
                let mut host = tables.host();
                host.tables
                    .resource_pending_transfer_own(pending.ticket(), TypedResource::Host(70))
                    .unwrap_err()
            };
            assert!(matches!(
                failure.stage,
                PendingTransferStage::DestinationInsert
            ));
            assert_limit(&failure.error);
            assert!(failure.destination.is_none());

            // The confirmed source stayed exactly pending and occupied, and
            // the denied destination leaked no occupied slot.
            assert!(tables
                .host()
                .host_resource_lift_own(pending.host_index())
                .is_err());
            assert!(tables
                .raw
                .resource_rep(TypedResourceIndex::Host(3))
                .is_err());

            // A funded retry transfers the same identity, rep, and type into
            // a destination that is still pending.
            budget.allow();
            let dst = {
                let mut host = tables.host();
                host.tables
                    .resource_pending_transfer_own(pending.ticket(), TypedResource::Host(70))
                    .unwrap()
            };
            assert_eq!(dst.index().raw_index(), 3);
            assert_eq!(dst.rep(), 70);
            assert_eq!(dst.owner(), pending.owner());
            assert!(tables.raw.resource_reject_pending(3).is_err());
            assert!(tables
                .raw
                .resource_rep(TypedResourceIndex::Host(3))
                .is_err());

            // The unrelated active entry still works.
            assert_eq!(tables.host().host_resource_lift_own(active).unwrap(), 60);

            // The old source index is no longer usable after the confirmed
            // transfer; retire the source host index at the metadata layer.
            assert!(tables
                .raw
                .resource_pending_validate(pending.ticket())
                .is_err());
            assert!(tables
                .raw
                .resource_rep(TypedResourceIndex::Host(2))
                .is_err());
            tables
                .host()
                .validate_host_index(pending.host_index(), true)
                .unwrap();

            // The destination rolls back through its own ticket.
            assert_eq!(tables.raw.resource_pending_remove(&dst).unwrap(), 70);
            assert!(tables.raw.is_empty());
        }
        assert_eq!(budget.used.load(Ordering::SeqCst), 0);
    }

    #[test]
    fn pending_lower_own_rolls_back_when_dummy_metadata_funding_is_denied() {
        let budget = Arc::new(Budget::new(2));
        {
            let mut tables = Tables::new(&budget);
            let owner = key(11);
            let error = tables
                .host()
                .host_resource_lower_own_pending(10, None, None, owner)
                .unwrap_err();
            assert_limit(&error);
            assert_eq!(budget.requests.load(Ordering::SeqCst), 2);
            assert!(tables.raw.is_empty());
            assert!(tables.data.table_slot_metadata.is_empty());
            // Logical rollback must not release retained raw table capacity.
            assert!(budget.used.load(Ordering::SeqCst) > 0);

            budget.allow();
            let pending = tables
                .host()
                .host_resource_lower_own_pending(20, None, None, owner)
                .unwrap();
            assert_eq!(pending.host_index().index(), 1);
            assert_eq!(
                tables.host().host_resource_pending_remove(&pending).unwrap(),
                20
            );
            assert!(tables.raw.is_empty());
        }
        assert_eq!(budget.used.load(Ordering::SeqCst), 0);
    }

    #[test]
    fn pending_lower_own_rolls_back_when_real_metadata_funding_is_denied() {
        let budget = Arc::new(Budget::new(3));
        {
            let mut tables = Tables::new(&budget);
            let owner = key(13);
            let error = tables
                .host()
                .host_resource_lower_own_pending(40, None, None, owner)
                .unwrap_err();
            assert_limit(&error);
            assert_eq!(budget.requests.load(Ordering::SeqCst), 3);
            assert!(tables.raw.is_empty());
            // Dummy metadata remains real funded storage after denial.
            assert_eq!(tables.data.table_slot_metadata.len(), 1);
            assert!(budget.used.load(Ordering::SeqCst) > 0);

            budget.allow();
            let pending = tables
                .host()
                .host_resource_lower_own_pending(50, None, None, owner)
                .unwrap();
            assert_eq!(pending.host_index().index(), 1);
            assert_eq!(tables.data.table_slot_metadata.len(), 2);
            // Abandoning custody keeps the entry pending, inaccessible, and
            // funded until physical table destruction.
            drop(pending);
            assert!(!tables.raw.is_empty());
            assert!(tables
                .raw
                .resource_rep(TypedResourceIndex::Host(1))
                .is_err());
            assert!(budget.used.load(Ordering::SeqCst) > 0);
        }
        assert_eq!(budget.used.load(Ordering::SeqCst), 0);
    }

    #[test]
    fn pending_prepare_drop_and_activation_commit() {
        let budget = Arc::new(Budget::new(0));
        {
            let mut tables = Tables::new(&budget);
            let owner = key(17);
            let pending = tables
                .host()
                .host_resource_lower_own_pending(66, None, None, owner)
                .unwrap();
            let hri = pending.host_index();

            // A dropped preparation leaves the entry pending and charged.
            {
                let mut host = tables.host();
                let prep = host.host_resource_pending_prepare(&pending).unwrap();
                drop(prep);
            }
            assert!(tables.host().host_resource_lift_own(hri).is_err());
            assert!(tables.host().host_resource_drop(hri).is_err());

            // Invalid identity is rejected during preparation, before any
            // commit point.
            let wrong_rep = PendingHostOwnTicket::new(
                PendingOwnTicket::new(owner, TypedResourceIndex::Host(hri.index()), 67),
                hri,
            );
            assert!(tables
                .host()
                .host_resource_pending_prepare(&wrong_rep)
                .is_err());

            // The infallible commit performs no owner reservation or
            // allocation: arm a denial and assert the request count is
            // unchanged across `activate`.
            let requests = {
                let mut host = tables.host();
                let prep = host.host_resource_pending_prepare(&pending).unwrap();
                let requests = budget.requests.load(Ordering::SeqCst);
                budget.deny_at.store(requests + 1, Ordering::SeqCst);
                prep.activate();
                requests
            };
            assert_eq!(budget.requests.load(Ordering::SeqCst), requests);

            // Now active: ordinary access works, and a second preparation is
            // rejected since nothing is pending anymore.
            assert_eq!(
                tables
                    .raw
                    .resource_rep(TypedResourceIndex::Host(hri.index()))
                    .unwrap(),
                66
            );
            let (rep, is_own) = tables
                .raw
                .resource_lend(TypedResourceIndex::Host(hri.index()))
                .unwrap();
            assert_eq!((rep, is_own), (66, true));
            tables
                .raw
                .resource_undo_lend(TypedResourceIndex::Host(hri.index()))
                .unwrap();
            assert!(tables
                .host()
                .host_resource_pending_prepare(&pending)
                .is_err());
            assert_eq!(tables.host().host_resource_lift_own(hri).unwrap(), 66);
            assert!(tables.raw.is_empty());
        }
        assert_eq!(budget.used.load(Ordering::SeqCst), 0);
    }
}
