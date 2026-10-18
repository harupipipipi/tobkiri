//! Implementation of the canonical-ABI related intrinsics for resources in the
//! component model.
//!
//! This module contains all the relevant gory details of the
//! component model related to lifting and lowering resources. For example
//! intrinsics like `resource.new` will bottom out in calling this file, and
//! this is where resource tables are actually defined and modified.
//!
//! The main types in this file are:
//!
//! * `ResourceTables` - the "here's everything" context which is required to
//!   perform canonical ABI operations.
//!
//! * `CallContext` - per-task information about active calls and borrows
//!   and runtime state tracking that to ensure that everything is handled
//!   correctly.
//!
//! Individual operations are exposed through methods on `ResourceTables` for
//! lifting/lowering/etc. This does mean though that some other fiddly bits
//! about ABI details can be found in lifting/lowering throughout Wasmtime,
//! namely in the `Resource<T>` and `ResourceAny` types.

use super::{HandleTable, InstanceState, PendingActivation, RemovedResource};
use crate::bail_bug;
use crate::component::store::ComponentTaskState;
use crate::component::{FundedVec, HostAllocationOwner};
use crate::prelude::*;
use crate::store::StoreId;
use core::error::Error;
use core::fmt;
use core::mem;
use core::num::NonZeroU64;
use wasmtime_environ::component::{
    ComponentTypes, RuntimeComponentInstanceIndex, TypeResourceTableIndex,
};
use wasmtime_environ::prelude::TryPrimaryMap;

/// Contextual state necessary to perform resource-related operations.
///
/// This state a bit odd since it has a few optional bits, but the idea is that
/// whenever this is constructed the bits required to perform operations are
/// always `Some`. For example:
///
/// * During lifting and lowering both `guest_table` and `host_table` are
///   `Some`.
/// * During wasm's own intrinsics only `guest_table` is `Some`.
/// * During embedder-invoked resource destruction calls only `host_table` is
///   `Some`.
///
/// This is all packaged up into one state though to make it easier to operate
/// on and to centralize handling of the state related to resources due to how
/// critical it is for correctness.
pub struct ResourceTables<'a> {
    pub(crate) allocation_owner: HostAllocationOwner,
    /// Runtime state for all resources defined in a component.
    ///
    /// This is required whenever a `TypeResourceTableIndex`, for example, is
    /// provided as it's the lookup where that happens. Not present during
    /// embedder-originating operations though such as
    /// `ResourceAny::resource_drop` which won't consult this table as it's
    /// only operating over the host table.
    pub guest: Option<(
        &'a mut TryPrimaryMap<RuntimeComponentInstanceIndex, InstanceState>,
        &'a ComponentTypes,
    )>,

    /// Runtime state for resources currently owned by the host.
    ///
    /// This is the single table used by the host stored within `Store<T>`. Host
    /// resources will point into this and effectively have the same semantics
    /// as-if they're in-component resources. The major distinction though is
    /// that this is a heterogeneous table instead of only containing a single
    /// type.
    pub host_table: &'a mut HandleTable,

    /// Task information about calls actively in use to track information such
    /// as borrow counts.
    pub task_state: &'a mut ComponentTaskState,

    /// Identifier for the current "scope" which is used for various functions
    /// on `task_state` above to mutate borrows/etc of the current scope.
    pub current_scope_id: Option<u32>,
}

/// Typed representation of a "rep" for a resource.
///
/// All resources in the component model are stored in a single heterogeneous
/// table so this type is used to disambiguate what everything is. This is the
/// representation of a resource stored at-rest in memory.
#[derive(Debug)]
pub enum TypedResource {
    /// A resource defined by the host.
    ///
    /// The meaning of the 32-bit integer here is up to the embedder, it
    /// otherwise is not used within the runtime here.
    Host(u32),

    /// A resource defined within a component.
    Component {
        /// This is an integer supplied by the component itself when this
        /// resource was created. Typically this is a pointer into linear
        /// memory for a component.
        rep: u32,

        /// The type of this component resource.
        ///
        /// This is used, within the context of a component, to keep track of
        /// what the type of `rep` is. This is then used when getting/removing
        /// from the table to ensure that the guest does indeed have the right
        /// permission to access this slot.
        ty: TypeResourceTableIndex,
    },
}

impl TypedResource {
    /// The raw `rep` carried by this resource, without a type check.
    ///
    /// Provisional operations use this only after identity has been
    /// established by the slot itself.
    #[allow(dead_code)]
    pub(super) fn untyped_rep(&self) -> u32 {
        match self {
            Self::Host(rep) | Self::Component { rep, .. } => *rep,
        }
    }

    pub(super) fn rep(&self, access_ty: &TypedResourceIndex) -> Result<u32> {
        match (self, access_ty) {
            (Self::Host(rep), TypedResourceIndex::Host(_)) => Ok(*rep),
            (Self::Host(_), expected) => bail!(ResourceTypeMismatch {
                expected: *expected,
                found: "host resource",
            }),
            (Self::Component { rep, ty }, TypedResourceIndex::Component { ty: expected, .. }) => {
                if ty == expected {
                    Ok(*rep)
                } else {
                    bail!(ResourceTypeMismatch {
                        expected: *access_ty,
                        found: "a different guest-defined resource",
                    })
                }
            }
            (Self::Component { .. }, expected) => bail!(ResourceTypeMismatch {
                expected: *expected,
                found: "guest-defined resource",
            }),
        }
    }
}

/// An index used to access a resource.
///
/// This reflects how index operations are always accompanied not only with a
/// 32-bit index but additionally with a type. For example a `resource.drop`
/// intrinsic in a guest takes only a 32-bit integer argument, but it
/// inherently is used to only drop one type of resource which is additionally
/// ascribed here.
#[derive(Debug, Copy, Clone)]
pub enum TypedResourceIndex {
    /// A host resource at the given index is being accessed.
    Host(u32),

    /// A guest resource is being accessed.
    Component {
        /// The index supplied by the guest being accessed.
        index: u32,

        /// The fully-specific type of this resource.
        ty: TypeResourceTableIndex,
    },
}

impl TypedResourceIndex {
    pub(crate) fn raw_index(&self) -> u32 {
        match self {
            Self::Host(index) | Self::Component { index, .. } => *index,
        }
    }

    fn desc(&self) -> &'static str {
        match self {
            Self::Host(_) => "host resource",
            Self::Component { .. } => "guest-defined resource",
        }
    }
}

/// Identity of the reserved store-side record that provisionally owns a
/// pending `own` resource entry.
///
/// A key is a descriptive identity only: it names the actual `StoreId` and
/// the nonzero serial of the reserved record the entry belongs to. Copying
/// a key does not grant or duplicate ownership; ownership is exercised
/// through [`PendingOwnTicket`] custody minted by table insertion.
///
/// The future `Store` record factory is the intended sole production
/// minting boundary for these keys; this stage only exposes the trusted
/// construction seam below.
#[allow(dead_code)]
#[derive(Debug, Copy, Clone, PartialEq, Eq)]
pub(crate) struct PendingOwnerKey {
    store: StoreId,
    serial: NonZeroU64,
}

#[allow(dead_code)]
impl PendingOwnerKey {
    /// Trusted crate-private construction seam for provisional owners.
    ///
    /// `store` must be an actual `StoreId` and `serial` must be a fresh,
    /// nonzero reserved-record serial minted for that store. Exercised with
    /// `StoreId::allocate` in tests until the real `Store` record allocator
    /// exists; a zero serial is a closed error.
    pub(crate) fn new_stage_a_seam(store: StoreId, serial: u64) -> Result<Self> {
        match NonZeroU64::new(serial) {
            Some(serial) => Ok(Self { store, serial }),
            None => bail!("pending owner record serial must be nonzero"),
        }
    }
}

/// Custody token authorizing provisional operations on exactly one pending
/// `own` entry.
///
/// A ticket binds the provisional owner key, the exact typed index, and the
/// representation recorded when the entry was inserted. Every authorized
/// operation revalidates all of these against the live slot before
/// mutating, so a stale, foreign, or mismatched ticket is a closed error.
///
/// Tickets are deliberately not `Copy` or `Clone`: they carry rollback
/// custody of a specific table entry. Dropping a ticket abandons custody
/// only — it never removes, activates, or releases the entry, which stays
/// pending and funded until an authorized operation or physical table
/// destruction.
#[allow(dead_code)]
#[derive(Debug)]
pub(crate) struct PendingOwnTicket {
    owner: PendingOwnerKey,
    index: TypedResourceIndex,
    rep: u32,
}

#[allow(dead_code)]
impl PendingOwnTicket {
    /// Trusted crate-private constructor; the table insertion paths are the
    /// sole production callers.
    ///
    /// A forged ticket cannot mutate anything: authorized operations always
    /// revalidate owner, index, type, and representation against the live
    /// slot before touching it.
    pub(crate) fn new(owner: PendingOwnerKey, index: TypedResourceIndex, rep: u32) -> Self {
        Self {
            owner,
            index,
            rep,
        }
    }

    /// The provisional owner record this entry belongs to.
    pub(crate) fn owner(&self) -> PendingOwnerKey {
        self.owner
    }

    /// The exact typed index this entry occupies.
    pub(crate) fn index(&self) -> TypedResourceIndex {
        self.index
    }

    /// The representation recorded at insertion.
    pub(crate) fn rep(&self) -> u32 {
        self.rep
    }
}

/// Stage reached by [`ResourceTables::resource_pending_transfer_own`] when
/// it failed.
#[allow(dead_code)]
#[derive(Debug, Copy, Clone)]
pub(crate) enum PendingTransferStage {
    /// Validating the source identity failed; nothing was mutated.
    SourceValidation,
    /// Inserting the funded destination pending entry failed; the confirmed
    /// source entry remains pending and occupied.
    DestinationInsert,
    /// Removing the confirmed source failed after the destination was
    /// inserted; the inserted destination was rolled back and only the
    /// source remains pending.
    SourceRemove,
    /// Removing the confirmed source failed and rolling back the inserted
    /// destination failed too; both entries remain pending and the
    /// destination's ticket is returned for custody.
    DestinationRollback,
}

/// Bounded failure custody returned by a failed pending transfer.
///
/// `error` preserves the original error, including downcastable
/// `HostAllocationError` values from funding denial. `destination` is only
/// `Some` at stage `DestinationRollback` and carries custody of the
/// inserted destination entry that could not be rolled back.
#[allow(dead_code)]
#[derive(Debug)]
pub(crate) struct PendingTransferError {
    /// The first error encountered.
    pub error: crate::Error,
    /// The stage at which the transfer stopped.
    pub stage: PendingTransferStage,
    /// Custody of a still-pending inserted destination entry, if any.
    pub destination: Option<PendingOwnTicket>,
}

impl fmt::Display for PendingTransferError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "pending resource transfer failed at stage {:?}: {}",
            self.stage, self.error
        )
    }
}

impl Error for PendingTransferError {}

/// State related to borrows for a specific call.
#[derive(Default)]
pub struct CallContext {
    lenders: FundedVec<TypedResourceIndex>,
    borrow_count: u32,
}

impl ResourceTables<'_> {
    fn table_for_resource(&mut self, resource: &TypedResource) -> &mut HandleTable {
        let owner = self.allocation_owner.clone();
        let table = match resource {
            TypedResource::Host(_) => &mut *self.host_table,
            TypedResource::Component { ty, .. } => {
                let (states, types) = self.guest.as_mut().unwrap();
                states[types[*ty].unwrap_concrete_instance()].handle_table()
            }
        };
        table.set_allocation_owner(&owner);
        table
    }

    fn table_for_index(&mut self, index: &TypedResourceIndex) -> &mut HandleTable {
        let owner = self.allocation_owner.clone();
        let table = match index {
            TypedResourceIndex::Host(_) => &mut *self.host_table,
            TypedResourceIndex::Component { ty, .. } => {
                let (states, types) = self.guest.as_mut().unwrap();
                states[types[*ty].unwrap_concrete_instance()].handle_table()
            }
        };
        table.set_allocation_owner(&owner);
        table
    }

    /// Implementation of the `resource.new` canonical intrinsic.
    ///
    /// Note that this is the same as `resource_lower_own`.
    pub fn resource_new(&mut self, resource: TypedResource) -> Result<u32> {
        self.table_for_resource(&resource)
            .resource_own_insert(resource)
    }

    /// Implementation of the `resource.rep` canonical intrinsic.
    ///
    /// This one's one of the simpler ones: "just get the rep please"
    pub fn resource_rep(&mut self, index: TypedResourceIndex) -> Result<u32> {
        self.table_for_index(&index).resource_rep(index)
    }

    /// Implementation of the `resource.drop` canonical intrinsic minus the
    /// actual invocation of the destructor.
    ///
    /// This will drop the handle at the `index` specified, removing it from
    /// the specified table. This operation can fail if:
    ///
    /// * The index is invalid.
    /// * The index points to an `own` resource which has active borrows.
    /// * The index's type is mismatched with the entry in the table's type.
    ///
    /// Otherwise this will return `Some(rep)` if the destructor for `rep` needs
    /// to run. If `None` is returned then that means a `borrow` handle was
    /// removed and no destructor is necessary.
    pub fn resource_drop(&mut self, index: TypedResourceIndex) -> Result<Option<u32>> {
        match self.table_for_index(&index).remove_resource(index)? {
            RemovedResource::Own { rep } => Ok(Some(rep)),
            RemovedResource::Borrow { scope } => {
                self.task_state.call_context(scope)?.borrow_count -= 1;
                Ok(None)
            }
        }
    }

    /// Inserts a new "own" handle into the specified table.
    ///
    /// This will insert the specified representation into the specified type
    /// table.
    ///
    /// Note that this operation is infallible, and additionally that this is
    /// the same as `resource_new` implementation-wise.
    ///
    /// This is an implementation of the canonical ABI `lower_own` function.
    pub fn resource_lower_own(&mut self, resource: TypedResource) -> Result<u32> {
        self.table_for_resource(&resource)
            .resource_own_insert(resource)
    }

    /// Attempts to remove an "own" handle from the specified table and its
    /// index.
    ///
    /// This operation will fail if `index` is invalid, if it's a `borrow`
    /// handle, if the own handle has currently been "lent" as a borrow, or if
    /// `index` has a different type in the table than the index.
    ///
    /// This is an implementation of the canonical ABI `lift_own` function.
    pub fn resource_lift_own(&mut self, index: TypedResourceIndex) -> Result<u32> {
        match self.table_for_index(&index).remove_resource(index)? {
            RemovedResource::Own { rep } => Ok(rep),
            RemovedResource::Borrow { .. } => bail!("cannot lift own resource from a borrow"),
        }
    }

    fn current_scope_id(&self) -> Result<u32> {
        match self.current_scope_id {
            Some(id) => Ok(id),
            None => bail_bug!("no current scope"),
        }
    }

    /// Extracts the underlying resource representation by lifting a "borrow"
    /// from the tables.
    ///
    /// This primarily employs dynamic tracking when a borrow is created from an
    /// "own" handle to ensure that the "own" handle isn't dropped while the
    /// borrow is active and additionally that when the current call scope
    /// returns the lend operation is undone.
    ///
    /// This is an implementation of the canonical ABI `lift_borrow` function.
    pub fn resource_lift_borrow(&mut self, index: TypedResourceIndex) -> Result<u32> {
        let (rep, is_own) = self.table_for_index(&index).resource_lend(index)?;
        if is_own {
            let current = self.current_scope_id()?;
            self.task_state
                .call_context(current)?
                .lenders
                .push(index, &self.allocation_owner)?;
        }
        Ok(rep)
    }

    /// Records a new `borrow` resource with the given representation within the
    /// current call scope.
    ///
    /// This requires that a call scope is active. Additionally the number of
    /// active borrows in the latest scope will be increased and must be
    /// decreased through a future use of `resource_drop` before the current
    /// call scope exits.
    ///
    /// This some of the implementation of the canonical ABI `lower_borrow`
    /// function. The other half of this implementation is located on
    /// `VMComponentContext` which handles the special case of avoiding borrow
    /// tracking entirely.
    pub fn resource_lower_borrow(&mut self, resource: TypedResource) -> Result<u32> {
        let scope = self.current_scope_id()?;
        let cx = self.task_state.call_context(scope)?;
        cx.borrow_count = cx.borrow_count.checked_add(1).unwrap();
        self.table_for_resource(&resource)
            .resource_borrow_insert(resource, scope)
    }

    /// Validates that the current scope can be exited.
    ///
    /// This will ensure that this context's active borrows have all been
    /// dropped. This will then commit the lend decrements back to the owned
    /// resources that were originally passed in.
    #[inline]
    pub fn validate_scope_exit(&mut self) -> Result<()> {
        let cx = self.task_state.call_context(self.current_scope_id()?)?;
        if cx.borrow_count > 0 {
            bail!("borrow handles still remain at the end of the call")
        }
        for lender in mem::take(&mut cx.lenders) {
            // Note the panics here which should never get triggered in theory
            // due to the dynamic tracking of borrows and such employed for
            // resources.
            self.table_for_index(&lender)
                .resource_undo_lend(lender)
                .unwrap();
        }
        Ok(())
    }

    /// Inserts a new provisional `own` resource into the table selected by
    /// `resource`.
    ///
    /// The entry is pending: every ordinary accessor rejects it until a
    /// prepared activation flips it active. Returns the custody ticket
    /// minted by the table; `owner` identifies the reserved store record
    /// authorized to roll back, transfer, or activate the entry.
    #[allow(dead_code)]
    pub(crate) fn resource_pending_own_insert(
        &mut self,
        resource: TypedResource,
        owner: PendingOwnerKey,
    ) -> Result<PendingOwnTicket> {
        self.table_for_resource(&resource)
            .resource_own_insert_pending(resource, owner)
    }

    /// Rejects `index` when it points at a pending `own` entry.
    ///
    /// This performs no mutation and exists so callers can fail before
    /// running their own removal or generation bookkeeping.
    pub(crate) fn resource_reject_pending(&mut self, index: TypedResourceIndex) -> Result<()> {
        self.table_for_index(&index)
            .resource_reject_pending(index.raw_index())
    }

    /// Validates `ticket` against the live slot without mutating anything.
    ///
    /// The owner key, typed index, and representation must all match the
    /// pending entry exactly.
    #[allow(dead_code)]
    pub(crate) fn resource_pending_validate(&mut self, ticket: &PendingOwnTicket) -> Result<()> {
        self.table_for_index(&ticket.index())
            .resource_pending_validate(ticket)
    }

    /// Record-authorized rollback of a pending `own` entry.
    ///
    /// Removes the entry only if `ticket` exactly identifies it. No
    /// destructor is dispatched and no borrow bookkeeping is touched;
    /// pending entries always have a zero lend count.
    #[allow(dead_code)]
    pub(crate) fn resource_pending_remove(&mut self, ticket: &PendingOwnTicket) -> Result<u32> {
        self.table_for_index(&ticket.index())
            .resource_pending_remove(ticket)
    }

    /// Validates `ticket` and returns an exclusive activation guard.
    ///
    /// The returned guard performs only the already-prepared
    /// pending-to-active state flip — no allocation, callback, await, or
    /// revalidation. Dropping it leaves the entry pending and charged. This
    /// does not authenticate any acknowledgement; later stages are
    /// responsible for invoking it only after a real same-permit ACK.
    #[allow(dead_code)]
    pub(crate) fn resource_pending_prepare(
        &mut self,
        ticket: &PendingOwnTicket,
    ) -> Result<PendingActivation<'_>> {
        self.table_for_index(&ticket.index())
            .resource_pending_prepare(ticket)
    }

    /// Transfers a pending `own` entry identified by `ticket` into `dst`'s
    /// table, preserving its pending state, representation, and owner.
    ///
    /// Transactional: the source identity is fully validated first, the
    /// funded destination pending entry is inserted before the confirmed
    /// source is removed, and every failure returns bounded custody via
    /// [`PendingTransferError`] describing the exact stage reached.
    #[allow(dead_code)]
    pub(crate) fn resource_pending_transfer_own(
        &mut self,
        ticket: &PendingOwnTicket,
        dst: TypedResource,
    ) -> Result<PendingOwnTicket, PendingTransferError> {
        if dst.untyped_rep() != ticket.rep() {
            return Err(PendingTransferError {
                error: crate::Error::msg("pending transfer representation mismatch"),
                stage: PendingTransferStage::SourceValidation,
                destination: None,
            });
        }
        if let Err(error) = self.resource_pending_validate(ticket) {
            return Err(PendingTransferError {
                error,
                stage: PendingTransferStage::SourceValidation,
                destination: None,
            });
        }
        let destination = match self.resource_pending_own_insert(dst, ticket.owner()) {
            Ok(ticket) => ticket,
            Err(error) => {
                return Err(PendingTransferError {
                    error,
                    stage: PendingTransferStage::DestinationInsert,
                    destination: None,
                });
            }
        };
        if let Err(error) = self.resource_pending_remove(ticket) {
            return match self.resource_pending_remove(&destination) {
                Ok(_) => Err(PendingTransferError {
                    error,
                    stage: PendingTransferStage::SourceRemove,
                    destination: None,
                }),
                Err(_) => Err(PendingTransferError {
                    error,
                    stage: PendingTransferStage::DestinationRollback,
                    destination: Some(destination),
                }),
            };
        }
        Ok(destination)
    }
}

#[derive(Debug)]
struct ResourceTypeMismatch {
    expected: TypedResourceIndex,
    found: &'static str,
}

impl fmt::Display for ResourceTypeMismatch {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "handle index {} used with the wrong type, \
             expected {} but found {}",
            self.expected.raw_index(),
            self.expected.desc(),
            self.found,
        )
    }
}

impl Error for ResourceTypeMismatch {}
