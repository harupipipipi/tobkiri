//! Store-owned provisional delivery and the last-fallible ordinary ABI hook.
//! The explicit Secret protocol admits at most one provisional record per
//! callback; legacy host calls retain their original result arity and behavior.
//! Production enablement still requires C's real independent keeper participants,
//! access closure, and original-supervisor/same-permit ACK verification.
#![allow(dead_code)]

mod value;
pub use value::HostDeliveryKind;
#[cfg(feature = "async")]
pub use value::{HostDeliveryCompletion, HostDeliveryCompletionAuthority,
    HostDeliveryFinalizer, HostDeliveryProtocol, HostDeliveryReady, HostDeliveryReservation};
#[cfg(feature = "async")]
use value::DeliveryFinalizer;

use crate::component::resources::{HostResourceIndex, PendingHostOwnTicket};
use crate::component::{FundedVec, HostAllocationError, HostAllocationKind, HostAllocationLease,
    HostAllocationOwner, HostAllocationRequest, Instance, ResourceType};
#[cfg(feature = "async")]
use crate::component::ResourceAny;
use crate::prelude::*;
use crate::runtime::vm::component::{PendingOwnTicket, PendingOwnerKey, PendingTransferStage};
use crate::store::{StoreId, StoreOpaque};
use crate::{AsContextMut, Store, StoreContextMut};
use core::any::Any;
use core::mem::size_of;
use wasmtime_environ::component::{ComponentTypes, InterfaceType, TypeFuncIndex, TypeResourceTableIndex};

/// A separately funded outer carrier for an already prepared shared keeper.
///
/// Prepare independent carriers for runtime, actual data owner, and actual
/// payload owner BEFORE generating/consuming a resource. `K` must itself retain
/// its existing funded shared-control storage; this carrier does not invent a
/// shared control allocation or authenticate an ACK. Moving it with extracted
/// data or a typed-deleted payload preserves that participant's real lifetime.
/// No reference to the record arena is stored here.
pub struct ResourceDeliveryAttachment {
    payload: Option<Box<dyn Any + Send + Sync>>,
    lease: Option<HostAllocationLease>,
}

impl core::fmt::Debug for ResourceDeliveryAttachment {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        f.write_str("ResourceDeliveryAttachment(funded custody)")
    }
}

impl ResourceDeliveryAttachment {
    /// Fund the exact outer `Box<K>` before its fallible physical allocation.
    /// Any heap allocations inside `K` require their own original custody.
    pub fn new<K: Any + Send + Sync>(
        payload: K,
        owner: &HostAllocationOwner,
    ) -> core::result::Result<Self, HostAllocationError> {
        let mut lease = HostAllocationLease::new(owner);
        lease.charge(HostAllocationKind::Metadata, size_of::<K>(), 1, 0, 1)?;
        let payload: Box<dyn Any + Send + Sync> = try_new::<Box<K>>(payload)
            .map_err(|_| HostAllocationError::Allocator)?;
        Ok(Self { payload: Some(payload), lease: Some(lease) })
    }
}

impl Drop for ResourceDeliveryAttachment {
    fn drop(&mut self) {
        // The local lease is an unwind guard too: Box drop glue destroys its
        // allocation before unwinding reaches this local reservation. This is
        // distinct from any shared keeper/payload lease retained inside K.
        let lease = self.lease.take();
        let payload = self.payload.take();
        drop(payload);
        drop(lease);
    }
}

impl<T: 'static> Store<T> {
    /// Installs the runtime participant before any provisional record exists.
    ///
    /// Failure returns the identical carrier, without dropping or refunding it.
    /// This alone never enables delivery: B2's production profile issuer and
    /// C's independent data/payload attachments and late ACK are mandatory.
    pub fn resource_delivery_runtime_attachment(
        &mut self,
        attachment: ResourceDeliveryAttachment,
    ) -> core::result::Result<(), (HostAllocationError, ResourceDeliveryAttachment)> {
        self.as_context_mut().resource_delivery_runtime_attachment(attachment)
    }
}

impl<T: 'static> StoreContextMut<'_, T> {
    /// See [`Store::resource_delivery_runtime_attachment`].
    pub fn resource_delivery_runtime_attachment(
        &mut self,
        attachment: ResourceDeliveryAttachment,
    ) -> core::result::Result<(), (HostAllocationError, ResourceDeliveryAttachment)> {
        let opaque = &mut **self.0;
        if opaque.component_delivery_keeper.is_some()
            || !opaque.component_data().delivery_records.is_empty()
        {
            return Err((HostAllocationError::Unavailable, attachment));
        }
        opaque.component_delivery_keeper = Some(attachment);
        Ok(())
    }
}

/// A descriptive record identity. It carries no resource or completion proof.
#[derive(Debug, Copy, Clone, PartialEq, Eq)]
pub(crate) struct DeliveryRecordId {
    store: StoreId,
    slot: u16,
    owner: PendingOwnerKey,
}

/// Issued only by the actual supported callback boundary before generation.
/// The cfg(test) B1 fixture issuer remains separate from production.
pub(crate) struct DeliveryCallProof {
    store: StoreId,
    invocation: Option<u64>,
}

#[derive(Debug, Copy, Clone, PartialEq, Eq)]
pub(crate) enum DeliveryStage {
    Reserved,
    ValuePending,
    Delivered,
    Nominal,
    HostPending,
    GuestPending,
    Active,
    Unknown,
    Retired,
}

struct GuestCustody {
    instance: Instance,
    ticket: PendingOwnTicket,
}

struct DeliveryRecord {
    id: DeliveryRecordId,
    kind: HostDeliveryKind,
    stage: DeliveryStage,
    // WASI payload is owned by the real ResourceTable/data/payload participants.
    // These are identity and exact runtime-table custody, not strong payload
    // or keeper references. Keeping both tickets in Unknown is conservative.
    nominal: Option<u32>,
    host: Option<PendingHostOwnTicket>,
    guest: Option<GuestCustody>,
    invocation: Option<u64>,
    resource_type: Option<ResourceType>,
}

#[derive(Default)]
pub(crate) struct DeliveryRecords {
    records: FundedVec<DeliveryRecord>,
    serial: u64,
    callback_serial: u64,
    current: Option<DeliveryCall>,
}

impl DeliveryRecords {
    const MAX_RECORDS: usize = 1024;

    fn is_empty(&self) -> bool {
        self.records.is_empty()
    }

    fn reserve(&mut self, store: StoreId, owner: &HostAllocationOwner) -> Result<DeliveryRecordId> {
        self.reserve_kind(store, owner, HostDeliveryKind::Resource)
    }

    fn reserve_kind(&mut self, store: StoreId, owner: &HostAllocationOwner,
        kind: HostDeliveryKind) -> Result<DeliveryRecordId>
    {
        let serial = self.serial.checked_add(1).ok_or(HostAllocationError::Overflow)?;
        let key = PendingOwnerKey::new_stage_a_seam(store, serial)?;
        let free = self.records.iter().position(|r| r.stage == DeliveryStage::Retired
            || (r.kind == HostDeliveryKind::Value && r.stage == DeliveryStage::Delivered));
        let slot = free.unwrap_or(self.records.len());
        if slot >= Self::MAX_RECORDS {
            return Err(HostAllocationError::Limit.into());
        }
        let id = DeliveryRecordId {
            store,
            slot: u16::try_from(slot).map_err(|_| HostAllocationError::Overflow)?,
            owner: key,
        };
        let record = DeliveryRecord {
            id, kind, stage: if kind == HostDeliveryKind::Value { DeliveryStage::ValuePending }
                else { DeliveryStage::Reserved }, nominal: None, host: None, guest: None,
            invocation: None, resource_type: None,
        };
        if let Some(slot) = free {
            owner.reserve(HostAllocationRequest {
                kind: HostAllocationKind::Resource, bytes: 0, elements: 1, depth: 0, work: 1,
            })?;
            self.records[slot] = record;
        } else {
            self.records.push(record, owner)?;
        }
        self.serial = serial;
        Ok(id)
    }

    fn get_mut(&mut self, id: DeliveryRecordId) -> Result<&mut DeliveryRecord> {
        let record = self.records.get_mut(usize::from(id.slot))
            .ok_or(HostAllocationError::Invalid)?;
        if record.id != id || record.stage == DeliveryStage::Retired {
            return Err(HostAllocationError::Invalid.into());
        }
        Ok(record)
    }
}

impl StoreOpaque {
    pub(crate) fn resource_delivery_reserve(
        &mut self,
        proof: &DeliveryCallProof,
    ) -> Result<DeliveryRecordId> {
        self.resource_delivery_reserve_kind(proof, HostDeliveryKind::Resource)
    }

    fn resource_delivery_reserve_kind(
        &mut self, proof: &DeliveryCallProof, kind: HostDeliveryKind,
    ) -> Result<DeliveryRecordId> {
        // Actual Store/profile/attachment admission precedes all generation.
        // For a production invocation, the live typed callback scope below
        // already admitted the SAME native fiber before BlockingContext::with
        // took its exclusive context pointers to poll this callback future.
        // can_block() is deliberately false while those pointers are taken.
        if proof.store != self.id()
            || (kind == HostDeliveryKind::Resource && self.component_delivery_keeper.is_none())
        {
            return Err(HostAllocationError::Unavailable.into());
        }
        #[cfg(feature = "call-hook")]
        return Err(HostAllocationError::Unavailable.into());
        #[cfg(feature = "component-model-async")]
        if self.concurrency_support() {
            return Err(HostAllocationError::Unavailable.into());
        }
        if let Some(invocation) = proof.invocation {
            let call = self.component_data().delivery_records.current.as_ref()
                .ok_or(HostAllocationError::Unavailable)?;
            if !call.supported || call.invocation != invocation || call.record.is_some() {
                return Err(HostAllocationError::Unavailable.into());
            }
        } else if !self.can_block() {
            // Only the historical cfg(test) B1 issuer has no invocation. It
            // must still prove an actual currently available native fiber;
            // a missing live callback never inherits production admission.
            return Err(HostAllocationError::Unavailable.into());
        }
        if kind == HostDeliveryKind::Value {
            self.resource_delivery_validate_value_results()?;
        }
        let owner = self.host_allocation_owner()?;
        let store = self.id();
        let records = &mut self.component_data_mut().delivery_records;
        let id = records.reserve_kind(store, &owner, kind)?;
        if let Some(invocation) = proof.invocation {
            records.get_mut(id)?.invocation = Some(invocation);
            records.current.as_mut().unwrap().record = Some(id);
        }
        Ok(id)
    }

    fn check_delivery_id(&self, id: DeliveryRecordId) -> Result<()> {
        if id.store != self.id() {
            return Err(HostAllocationError::Invalid.into());
        }
        Ok(())
    }

    fn resource_delivery_validate_value_results(&self) -> Result<()> {
        let call = self.component_data().delivery_records.current.as_ref()
            .filter(|call| call.supported).ok_or(HostAllocationError::Unavailable)?;
        let owner = self.host_allocation_owner()?;
        let instance = call.instance.id().get(self);
        let types = instance.component().types();
        // Inspect the ACTUAL canonical host callback type, including every
        // variant/result alternative. No caller bool or sample Val proves this.
        for ty in types[types[call.ty].results].types.iter() {
            resource_free_result_type(types, *ty, &owner, 1)?;
        }
        Ok(())
    }

    pub(crate) fn resource_delivery_note_nominal(
        &mut self, id: DeliveryRecordId, nominal: u32,
    ) -> Result<()> {
        self.check_delivery_id(id)?;
        let record = self.component_data_mut().delivery_records.get_mut(id)?;
        if record.stage != DeliveryStage::Reserved || record.nominal.is_some() {
            return Err(HostAllocationError::Invalid.into());
        }
        record.nominal = Some(nominal);
        record.stage = DeliveryStage::Nominal;
        Ok(())
    }

    pub(crate) fn resource_delivery_insert_host(
        &mut self, id: DeliveryRecordId,
    ) -> Result<HostResourceIndex> {
        self.check_delivery_id(id)?;
        let (mut tables, records) = self.component_delivery_tables_and_records(None)?;
        let record = records.get_mut(id)?;
        if record.stage != DeliveryStage::Nominal {
            return Err(HostAllocationError::Invalid.into());
        }
        let rep = record.nominal.ok_or(HostAllocationError::Invalid)?;
        // Mark uncertainty before entering the fallible insertion. A panic
        // cannot make this a replayable nominal-only record.
        record.stage = DeliveryStage::Unknown;
        match tables.host_resource_lower_own_pending(rep, None, None, id.owner) {
            Ok(ticket) => {
                let index = ticket.host_index();
                record.host = Some(ticket);
                record.stage = DeliveryStage::HostPending;
                Ok(index)
            }
            Err(error) => Err(error),
        }
    }

    /// `ty` must come from this exact instance's validated canonical lowering.
    /// B2 supplies that boundary; no public caller can manufacture this operation.
    pub(crate) fn resource_delivery_transfer_to_guest(
        &mut self, id: DeliveryRecordId, instance: Instance, ty: TypeResourceTableIndex,
    ) -> Result<u32> {
        self.check_delivery_id(id)?;
        if instance.id().store_id() != self.id() {
            return Err(HostAllocationError::Invalid.into());
        }
        let (mut tables, records) = self.component_delivery_tables_and_records(Some(instance))?;
        let record = records.get_mut(id)?;
        if record.stage != DeliveryStage::HostPending {
            return Err(HostAllocationError::Invalid.into());
        }
        let host = record.host.as_ref().ok_or(HostAllocationError::Invalid)?;
        record.stage = DeliveryStage::Unknown;
        match tables.host_resource_pending_transfer_to_guest(host, ty) {
            Ok(ticket) => {
                let index = ticket.index().raw_index();
                record.host = None;
                record.guest = Some(GuestCustody { instance, ticket });
                record.stage = DeliveryStage::GuestPending;
                Ok(index)
            }
            Err(mut failure) => {
                // Preserve every ticket, including SourceRemove with a live
                // destination after host metadata retirement failed.
                if let Some(ticket) = failure.destination.take() {
                    record.guest = Some(GuestCustody { instance, ticket });
                } else if matches!(failure.stage, PendingTransferStage::DestinationInsert) {
                    record.stage = DeliveryStage::HostPending;
                }
                // The record now owns the error's returned custody; return
                // the original downcastable error, never a stringified clone.
                Err(failure.error)
            }
        }
    }

    /// Remove only an exactly confirmed pending runtime entry. The nominal
    /// WASI payload remains owned/funded and is NOT declared destroyed here.
    pub(crate) fn resource_delivery_rollback_runtime(
        &mut self, id: DeliveryRecordId,
    ) -> Result<u32> {
        self.check_delivery_id(id)?;
        let instance = {
            let record = self.component_data_mut().delivery_records.get_mut(id)?;
            match record.stage {
                DeliveryStage::HostPending => None,
                DeliveryStage::GuestPending => Some(record.guest.as_ref()
                    .ok_or(HostAllocationError::Invalid)?.instance),
                _ => return Err(HostAllocationError::Invalid.into()),
            }
        };
        let (mut tables, records) = self.component_delivery_tables_and_records(instance)?;
        let record = records.get_mut(id)?;
        let was_host = record.stage == DeliveryStage::HostPending;
        record.stage = DeliveryStage::Unknown;
        let rep = if was_host {
            tables.host_resource_pending_remove(record.host.as_ref()
                .ok_or(HostAllocationError::Invalid)?)?
        } else {
            tables.guest_resource_pending_remove(&record.guest.as_ref()
                .ok_or(HostAllocationError::Invalid)?.ticket)?
        };
        if Some(rep) != record.nominal {
            return Err(HostAllocationError::Invalid.into());
        }
        record.host = None;
        record.guest = None;
        record.stage = DeliveryStage::Nominal;
        Ok(rep)
    }

    pub(crate) fn resource_delivery_mark_unknown(&mut self, id: DeliveryRecordId) -> Result<()> {
        self.check_delivery_id(id)?;
        self.component_data_mut().delivery_records.get_mut(id)?.stage = DeliveryStage::Unknown;
        Ok(())
    }

    pub(crate) fn resource_delivery_retire_empty(&mut self, id: DeliveryRecordId) -> Result<()> {
        self.check_delivery_id(id)?;
        let record = self.component_data_mut().delivery_records.get_mut(id)?;
        if record.stage != DeliveryStage::Reserved || record.nominal.is_some()
            || record.host.is_some() || record.guest.is_some()
        {
            return Err(HostAllocationError::Invalid.into());
        }
        record.stage = DeliveryStage::Retired;
        Ok(())
    }

    #[cfg(test)]
    fn delivery_test_proof(&mut self) -> DeliveryCallProof {
        DeliveryCallProof { store: self.id(), invocation: None }
    }

    #[cfg(test)]
    fn delivery_test_stage(&mut self, id: DeliveryRecordId) -> Result<DeliveryStage> {
        self.check_delivery_id(id)?;
        Ok(self.component_data_mut().delivery_records.get_mut(id)?.stage)
    }
}

/// Charge each bounded type-walk node before inspecting children. This shares
/// existing actual component funding and the canonical depth limit. No new
/// vector, shared control or resource allowance is created for admission.
fn resource_free_result_type(
    types: &ComponentTypes,
    ty: InterfaceType,
    owner: &HostAllocationOwner,
    depth: u32,
) -> Result<()> {
    if depth > 64 { return Err(HostAllocationError::Depth.into()); }
    owner.reserve(HostAllocationRequest {
        kind: HostAllocationKind::Temporary, bytes: 0, elements: 1, depth, work: 1,
    })?;
    let next = depth + 1;
    match ty {
        InterfaceType::Own(_) | InterfaceType::Borrow(_) | InterfaceType::Future(_)
        | InterfaceType::Stream(_) | InterfaceType::ErrorContext(_)
        | InterfaceType::FixedLengthList(_) => return Err(HostAllocationError::Unavailable.into()),
        InterfaceType::List(index) => resource_free_result_type(types, types[index].element, owner, next)?,
        InterfaceType::Map(index) => {
            resource_free_result_type(types, types[index].key, owner, next)?;
            resource_free_result_type(types, types[index].value, owner, next)?;
        }
        InterfaceType::Record(index) => {
            for field in types[index].fields.iter() {
                resource_free_result_type(types, field.ty, owner, next)?;
            }
        }
        InterfaceType::Tuple(index) => {
            for ty in types[index].types.iter() {
                resource_free_result_type(types, *ty, owner, next)?;
            }
        }
        InterfaceType::Variant(index) => {
            for ty in types[index].cases.values().flatten() {
                resource_free_result_type(types, *ty, owner, next)?;
            }
        }
        InterfaceType::Option(index) => resource_free_result_type(types, types[index].ty, owner, next)?,
        InterfaceType::Result(index) => {
            for ty in [types[index].ok, types[index].err].into_iter().flatten() {
                resource_free_result_type(types, ty, owner, next)?;
            }
        }
        InterfaceType::Bool | InterfaceType::S8 | InterfaceType::U8 | InterfaceType::S16
        | InterfaceType::U16 | InterfaceType::S32 | InterfaceType::U32 | InterfaceType::S64
        | InterfaceType::U64 | InterfaceType::Float32 | InterfaceType::Float64
        | InterfaceType::Char | InterfaceType::String | InterfaceType::Enum(_)
        | InterfaceType::Flags(_) => {}
    }
    Ok(())
}

/// One actual ordinary host-callback invocation. A record belongs to this
/// invocation, independently of the lifetime of its callback/result values.
struct DeliveryCall {
    invocation: u64,
    instance: Instance,
    ty: TypeFuncIndex,
    supported: bool,
    record: Option<DeliveryRecordId>,
    // The callback adapter is separate from identity/ticket-only arena slots.
    // C's original job/supervisor must independently outlive this adapter.
    #[cfg(feature = "async")]
    finalizer: Option<DeliveryFinalizer>,
}

/// Owns the typed Store borrow so normal errors, cancellation and unwinding all
/// close the same callback frame. No raw pointer or detached cleanup is needed.
pub(crate) struct DeliveryCallbackScope<'a, T: 'static> {
    store: StoreContextMut<'a, T>,
    previous: Option<DeliveryCall>,
}

impl<'a, T: 'static> DeliveryCallbackScope<'a, T> {
    pub(crate) fn enter(
        store: StoreContextMut<'a, T>,
        instance: Instance,
        ty: TypeFuncIndex,
        native_async_sync_abi: bool,
    ) -> Result<Self> {
        // This runs before host-task creation or parameter lifting, including
        // when guest realloc attempts to call another host import during lower.
        if store.0.component_data().delivery_records.current.as_ref()
            .is_some_and(|call| call.record.is_some())
        {
            return Err(HostAllocationError::Unavailable.into());
        }
        let mut supported = native_async_sync_abi && store.0.can_block();
        #[cfg(feature = "call-hook")]
        { supported = false; }
        #[cfg(feature = "component-model-async")]
        { supported &= !store.0.concurrency_support(); }
        let records = &mut store.0.component_data_mut().delivery_records;
        // Exhaustion disables only the opt-in protocol; legacy callbacks never
        // acquire a new failure merely because this additional counter ended.
        let invocation = match records.callback_serial.checked_add(1) {
            Some(next) => { records.callback_serial = next; next }
            None => { supported = false; 0 }
        };
        let previous = records.current.replace(DeliveryCall {
            invocation, instance, ty, supported, record: None,
            #[cfg(feature = "async")]
            finalizer: None,
        });
        Ok(Self { store, previous })
    }

    pub(crate) fn store(&mut self) -> &mut StoreContextMut<'a, T> {
        &mut self.store
    }

    /// The caller invokes this only after its entire result lower, scope exit,
    /// caller reentry and host-task deletion have succeeded.
    pub(crate) fn finish(&mut self) -> Result<()> {
        #[cfg(feature = "async")]
        { self.store.0.resource_delivery_final_abi_commit() }
        #[cfg(not(feature = "async"))]
        { Ok(()) }
    }
}

impl<T: 'static> Drop for DeliveryCallbackScope<'_, T> {
    fn drop(&mut self) {
        let current = {
            let records = &mut self.store.0.component_data_mut().delivery_records;
            let current = records.current.take();
            if let Some(id) = current.as_ref().and_then(|call| call.record) {
                if let Ok(record) = records.get_mut(id) {
                    if !matches!(record.stage, DeliveryStage::Active | DeliveryStage::Delivered | DeliveryStage::Retired) {
                        // An unsuccessful boundary cannot authorize a guessed
                        // rollback. The exact tickets and original funds remain.
                        record.stage = DeliveryStage::Unknown;
                    }
                }
            }
            // After activation this is only primitive frame restoration;
            // the adapter and wait future have already been destroyed.
            records.current = self.previous.take();
            current
        };
        // A failed callback may still own its adapter. Destroy it only after
        // releasing the arena borrow. No original C job/keeper is inferred done.
        drop(current);
    }
}

/// Identity of a reservation in the opt-in single-resource Secret protocol.
/// Dropping this value never removes a pending entry or completes an ACK.
#[cfg(feature = "async")]
pub struct ResourceDeliveryReservation {
    id: DeliveryRecordId,
    invocation: u64,
}

/// Unique authority which C must move to the ORIGINAL physical-job supervisor.
/// It is a vendor identity bridge, not a Gate permit, grant, or ACK receipt.
/// Neither callback success nor the ABI-ready challenge can replace it.
#[cfg(feature = "async")]
pub struct ResourceDeliveryCompletionAuthority {
    id: DeliveryRecordId,
    invocation: u64,
}

/// Issued exactly once at the last-fallible ABI boundary, after all result
/// lowering and task work succeeded. This challenge alone cannot activate.
#[cfg(feature = "async")]
pub struct ResourceDeliveryReady {
    id: DeliveryRecordId,
    invocation: u64,
}

/// A consumed matching authority/challenge pair. The real C adapter may produce
/// this ONLY after the original supervisor verifies the immutable terminal
/// outcome, acknowledged winner and SAME sealed DispatchPermit. Vendor fixtures
/// exercise identity and lifetime only; they do not authenticate Gate.
#[cfg(feature = "async")]
pub struct ResourceDeliveryCompletion {
    id: DeliveryRecordId,
    invocation: u64,
}

#[cfg(feature = "async")]
impl ResourceDeliveryCompletionAuthority {
    /// Trusted C verification boundary. Call only after the original physical
    /// job's authenticated same-permit terminal ACK; that ACK remains fallible.
    /// This consumes both non-Copy tokens and rejects a cross-store, cross-call
    /// or cross-record pairing. It never mutates or activates the runtime slot.
    pub fn complete_after_verified_original_ack(
        self,
        ready: ResourceDeliveryReady,
    ) -> Result<ResourceDeliveryCompletion> {
        if self.id != ready.id || self.invocation != ready.invocation {
            return Err(HostAllocationError::Invalid.into());
        }
        Ok(ResourceDeliveryCompletion { id: self.id, invocation: self.invocation })
    }
}

/// Explicit new opt-in protocol for one resource from one host callback.
///
/// Before reserving, C must have prepared the SAME funded keeper independently
/// for actual runtime, RestrictedWasi/data and HostSecret payload, installed an
/// owned WASI table and closed every pending rep/backend/access/alias/drop path.
/// It must place the returned completion authority in the original supervisor,
/// which alone authenticates the original immutable outcome and same permit ACK.
/// The original C supervisor owns custody independently of the callback adapter
/// and wait future: destroying either must not cancel, refund or replace its job.
/// No production implementation or Secret activation is installed by B2.
#[cfg(feature = "async")]
pub trait ResourceDeliverySecretProtocol: Send + Sync + 'static {
    /// Signal ABI-ready and build the owned original-supervisor wait. This runs
    /// outside runtime table/record guards; no Store or guest access is provided.
    /// Construction, polling and destruction may fail/unwind BEFORE activation.
    fn abi_ready(
        &mut self,
        ready: ResourceDeliveryReady,
    ) -> Result<crate::component::BudgetedFuture<'static, Result<ResourceDeliveryCompletion>>>;
}

/// Separately funded erasure for a preprepared original-supervisor adapter.
/// The outer Box's physical allocation is distinct from its payload/control
/// funding, just as for ResourceDeliveryAttachment.
#[cfg(feature = "async")]
pub struct ResourceDeliveryFinalizer {
    protocol: Option<Box<dyn ResourceDeliverySecretProtocol>>,
    lease: Option<HostAllocationLease>,
}

#[cfg(feature = "async")]
impl ResourceDeliveryFinalizer {
    /// Prepare this before first generation or grant consume. This grants no
    /// Gate authority and does not establish C's required participant proof.
    pub fn new<P: ResourceDeliverySecretProtocol>(
        protocol: P,
        owner: &HostAllocationOwner,
    ) -> core::result::Result<Self, HostAllocationError> {
        let mut lease = HostAllocationLease::new(owner);
        lease.charge(HostAllocationKind::Metadata, size_of::<P>(), 1, 0, 1)?;
        let protocol: Box<dyn ResourceDeliverySecretProtocol> =
            try_new::<Box<P>>(protocol).map_err(|_| HostAllocationError::Allocator)?;
        Ok(Self { protocol: Some(protocol), lease: Some(lease) })
    }
}

#[cfg(feature = "async")]
impl Drop for ResourceDeliveryFinalizer {
    fn drop(&mut self) {
        let lease = self.lease.take();
        let protocol = self.protocol.take();
        drop(protocol);
        drop(lease);
    }
}

#[cfg(feature = "async")]
impl<T: 'static> StoreContextMut<'_, T> {
    /// Reserve before the first resource generation/push or grant consume.
    /// Only an actual ordinary async host callback on a native fiber can opt in.
    /// A second reservation in this explicitly single-resource protocol fails
    /// before generating/consuming the second resource and retains the first.
    pub fn resource_delivery_secret_reserve(
        &mut self,
        finalizer: ResourceDeliveryFinalizer,
    ) -> Result<(ResourceDeliveryReservation, ResourceDeliveryCompletionAuthority)> {
        let invocation = self.0.component_data().delivery_records.current.as_ref()
            .filter(|call| call.supported && call.record.is_none())
            .ok_or(HostAllocationError::Unavailable)?.invocation;
        let proof = DeliveryCallProof { store: self.0.id(), invocation: Some(invocation) };
        let id = self.0.resource_delivery_reserve(&proof)?;
        self.0.component_data_mut().delivery_records.current.as_mut()
            .ok_or(HostAllocationError::Unavailable)?.finalizer = Some(DeliveryFinalizer::Resource(finalizer));
        Ok((ResourceDeliveryReservation { id, invocation },
            ResourceDeliveryCompletionAuthority { id, invocation }))
    }

    /// Register the exact real nominal rep after the C-owned table push. This
    /// creates a PENDING host own entry directly; no active insertion occurs.
    /// A ResourceAny copy carries description only. The canonical lower must
    /// revalidate the record, callback, type, instance and generation ticket.
    pub fn resource_delivery_secret_resource(
        &mut self,
        reservation: &ResourceDeliveryReservation,
        nominal: u32,
        ty: ResourceType,
    ) -> Result<ResourceAny> {
        self.0.resource_delivery_current_record(reservation.id, reservation.invocation)?;
        if self.0.component_data_mut().delivery_records.get_mut(reservation.id)?.kind
            != HostDeliveryKind::Resource
        { return Err(HostAllocationError::Invalid.into()); }
        self.0.resource_delivery_note_nominal(reservation.id, nominal)?;
        self.0.component_data_mut().delivery_records.get_mut(reservation.id)?.resource_type = Some(ty);
        let index = self.0.resource_delivery_insert_host(reservation.id)?;
        Ok(ResourceAny::new_pending(index, ty, reservation.id))
    }
}

#[cfg(feature = "async")]
impl StoreOpaque {
    fn resource_delivery_current_record(&self, id: DeliveryRecordId, invocation: u64) -> Result<()> {
        self.check_delivery_id(id)?;
        let call = self.component_data().delivery_records.current.as_ref()
            .ok_or(HostAllocationError::Unavailable)?;
        if !call.supported || call.invocation != invocation || call.record != Some(id) {
            return Err(HostAllocationError::Invalid.into());
        }
        Ok(())
    }

    pub(crate) fn resource_delivery_lower_pending(
        &mut self,
        id: DeliveryRecordId,
        index: HostResourceIndex,
        resource_type: ResourceType,
        instance: Instance,
        ty: TypeResourceTableIndex,
    ) -> Result<u32> {
        let call = self.component_data().delivery_records.current.as_ref()
            .ok_or(HostAllocationError::Unavailable)?;
        let invocation = call.invocation;
        if call.instance.id() != instance.id() || call.finalizer.is_none() {
            return Err(HostAllocationError::Invalid.into());
        }
        self.resource_delivery_current_record(id, invocation)?;
        let record = self.component_data_mut().delivery_records.get_mut(id)?;
        if record.kind != HostDeliveryKind::Resource
            || record.invocation != Some(invocation)
            || record.resource_type != Some(resource_type)
            || record.host.as_ref().map(|ticket| ticket.host_index()) != Some(index)
        {
            return Err(HostAllocationError::Invalid.into());
        }
        self.resource_delivery_transfer_to_guest(id, instance, ty)
    }

    fn resource_delivery_final_abi_commit(&mut self) -> Result<()> {
        let Some(call) = self.component_data().delivery_records.current.as_ref() else {
            return Ok(());
        };
        let Some(id) = call.record else { return Ok(()); };
        let invocation = call.invocation;
        let instance = call.instance;
        self.resource_delivery_current_record(id, invocation)?;
        let record = self.component_data_mut().delivery_records.get_mut(id)?;
        let kind = record.kind;
        if record.invocation != Some(invocation) { return Err(HostAllocationError::Invalid.into()); }
        match kind {
            HostDeliveryKind::Resource => {
                if record.stage != DeliveryStage::GuestPending
                    || record.guest.as_ref().map(|guest| guest.instance.id()) != Some(instance.id())
                { return Err(HostAllocationError::Invalid.into()); }
            }
            HostDeliveryKind::Value => {
                if record.stage != DeliveryStage::ValuePending || record.nominal.is_some()
                    || record.host.is_some() || record.guest.is_some() || record.resource_type.is_some()
                { return Err(HostAllocationError::Invalid.into()); }
            }
        }
        let finalizer = self.component_data_mut().delivery_records.current.as_mut()
            .ok_or(HostAllocationError::Unavailable)?.finalizer.take()
            .ok_or(HostAllocationError::Unavailable)?;
        // This callback and funded outer Box Drop run outside record/table
        // guards. A kind-bound ready challenge is minted once at this LAST hook.
        let future = finalizer.into_wait(HostDeliveryReady { id, invocation, kind })?;
        self.with_blocking(move |store, cx| {
            match kind {
                HostDeliveryKind::Value => {
                    // A plain value has no runtime resource slot to activate.
                    // Its real Canon/Val graph and original job retain their
                    // own actual owners; the runtime keeps this exact record.
                    let record = store.component_data_mut().delivery_records.get_mut(id)?;
                    let completion = cx.block_on(future)??;
                    if completion.id != id || completion.invocation != invocation || completion.kind != kind {
                        return Err(HostAllocationError::Invalid.into());
                    }
                    // The complete owned wait/future has already been destroyed.
                    // No allocation, callback or fallible conversion follows.
                    record.stage = DeliveryStage::Delivered;
                    Ok(())
                }
                HostDeliveryKind::Resource => {
                    let (mut tables, records) = store.component_delivery_tables_and_records(Some(instance))?;
                    let record = records.get_mut(id)?;
                    let guest = record.guest.as_ref().ok_or(HostAllocationError::Invalid)?;
                    let activation = tables.guest_resource_pending_prepare(&guest.ticket)?;
                    // block_on owns/destroys the whole stack-only wait adapter
                    // and funded concrete future BEFORE accepting completion.
                    let completion = cx.block_on(future)??;
                    if completion.id != id || completion.invocation != invocation || completion.kind != kind {
                        return Err(HostAllocationError::Invalid.into());
                    }
                    record.stage = DeliveryStage::Active;
                    activation.activate();
                    Ok(())
                }
            }
        })
    }

}

#[cfg(all(test, feature = "async", feature = "cranelift", feature = "wat", not(feature = "call-hook")))]
mod tests {
    use super::*;
    use crate::component::resources::HostResourceTables;
    use crate::component::{Component, HostAllocationBudget, HostAllocationShared, Linker,
        Resource, ResourceTable, ResourceType};
    use crate::{AsContextMut, Config, Engine};
    use core::future::Future;
    use core::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
    use std::sync::{Arc, Mutex};

    type Events = Arc<Mutex<Vec<&'static str>>>;

    struct Meter {
        live: AtomicU64,
        requests: AtomicUsize,
        deny_at: AtomicUsize,
        released: &'static str,
        events: Events,
    }
    impl Meter {
        fn new(events: &Events, released: &'static str) -> Arc<Self> {
            Arc::new(Self {
                live: AtomicU64::new(0), requests: AtomicUsize::new(0),
                deny_at: AtomicUsize::new(0), released, events: events.clone(),
            })
        }
        fn owner(self: &Arc<Self>) -> HostAllocationOwner { self.clone() }
        fn deny_next(&self) {
            self.deny_at.store(self.requests.load(Ordering::SeqCst) + 1, Ordering::SeqCst);
        }
        fn allow(&self) { self.deny_at.store(0, Ordering::SeqCst); }
    }
    impl HostAllocationBudget for Meter {
        fn reserve(&self, request: HostAllocationRequest) -> core::result::Result<(), HostAllocationError> {
            let n = self.requests.fetch_add(1, Ordering::SeqCst) + 1;
            if n == self.deny_at.load(Ordering::SeqCst) { return Err(HostAllocationError::Limit); }
            self.live.fetch_add(request.bytes, Ordering::SeqCst);
            Ok(())
        }
        fn release(&self, bytes: u64) {
            let old = self.live.fetch_sub(bytes, Ordering::SeqCst);
            assert!(old >= bytes, "released live storage twice");
            if bytes != 0 { self.events.lock().unwrap().push(self.released); }
        }
    }

    struct FinalControl { events: Events, meter: Arc<Meter> }
    impl Drop for FinalControl {
        fn drop(&mut self) {
            assert!(self.meter.live.load(Ordering::SeqCst) > 0);
            self.events.lock().unwrap().push("shared-payload-drop");
        }
    }
    struct Participant {
        role: &'static str,
        shared: HostAllocationShared<FinalControl>,
        storage: Option<Arc<Meter>>,
        events: Events,
    }
    impl Drop for Participant {
        fn drop(&mut self) {
            if let Some(storage) = &self.storage {
                // Actual runtime/data allocations have already been freed and
                // their own leases released before this participant dies.
                assert_eq!(storage.live.load(Ordering::SeqCst), 0, "{} ended early", self.role);
            }
            let _ = self.shared.value();
            self.events.lock().unwrap().push(self.role);
        }
    }
    struct Payload {
        keeper: ResourceDeliveryAttachment,
        events: Events,
    }
    impl Drop for Payload {
        fn drop(&mut self) {
            let _ = &self.keeper;
            self.events.lock().unwrap().push("payload-drop");
        }
    }
    struct Data {
        table: ResourceTable,
        keeper: ResourceDeliveryAttachment,
    }
    struct Fixture {
        store: Store<Data>,
        payload: Option<ResourceDeliveryAttachment>,
        events: Events,
        runtime: Arc<Meter>,
        data: Arc<Meter>,
        carriers: Arc<Meter>,
        control: Arc<Meter>,
    }

    fn fixture() -> Fixture {
        let events = Arc::new(Mutex::new(Vec::new()));
        let runtime = Meter::new(&events, "runtime-storage-release");
        let data = Meter::new(&events, "data-storage-release");
        let carriers = Meter::new(&events, "carrier-box-release");
        let control = Meter::new(&events, "shared-block-release");
        let shared = HostAllocationShared::new(
            FinalControl { events: events.clone(), meter: control.clone() },
            HostAllocationLease::new(&control.owner()),
        ).unwrap();
        let participant = |role, storage| ResourceDeliveryAttachment::new(Participant {
            role, shared: shared.clone(), storage, events: events.clone(),
        }, &carriers.owner()).unwrap();
        // All three independent participants are allocated before resource
        // generation. The production C carrier is actual LeasedShared; this
        // vendor test uses the existing funded Shared with the same real
        // last-reference physical-deallocation-before-lease semantics.
        let runtime_keeper = participant("runtime-participant-drop", Some(runtime.clone()));
        let data_keeper = participant("data-participant-drop", Some(data.clone()));
        let payload_keeper = participant("payload-participant-drop", None);
        drop(shared);
        let mut config = Config::new();
        config.wasm_component_model(true);
        let engine = Engine::new(&config).unwrap();
        let mut table = ResourceTable::new_owned(&data.owner());
        table.set_max_capacity(1024);
        let mut store = Store::new(&engine, Data { table, keeper: data_keeper });
        store.host_allocation_budget(runtime.owner());
        store.resource_delivery_runtime_attachment(runtime_keeper).unwrap();
        Fixture { store, payload: Some(payload_keeper), events, runtime, data, carriers, control }
    }

    async fn seed(f: &mut Fixture) -> (DeliveryRecordId, u32, HostResourceIndex) {
        let payload = f.payload.take().unwrap();
        let events = f.events.clone();
        f.store.as_context_mut().on_fiber(move |store| -> Result<_> {
            let proof = store.0.delivery_test_proof();
            let id = store.0.resource_delivery_reserve(&proof)?;
            let resource = store.data_mut().table.push(Payload { keeper: payload, events })?;
            let rep = resource.rep();
            store.0.resource_delivery_note_nominal(id, rep)?;
            let index = store.0.resource_delivery_insert_host(id)?;
            Ok((id, rep, index))
        }).await.unwrap().unwrap()
    }

    fn pos(events: &[&'static str], event: &'static str) -> usize {
        events.iter().position(|e| *e == event).unwrap()
    }
    fn assert_final(runtime: &Meter, data: &Meter, carriers: &Meter, control: &Meter) {
        assert_eq!(runtime.live.load(Ordering::SeqCst), 0);
        assert_eq!(data.live.load(Ordering::SeqCst), 0);
        assert_eq!(carriers.live.load(Ordering::SeqCst), 0);
        assert_eq!(control.live.load(Ordering::SeqCst), 0);
    }

    #[test]
    fn b1_attachment_denial_duplicate_and_unwind_keep_outer_box_funded() {
        struct Dropped(Arc<AtomicUsize>, bool);
        impl Drop for Dropped {
            fn drop(&mut self) {
                self.0.fetch_add(1, Ordering::SeqCst);
                if self.1 { panic!("deterministic participant drop unwind"); }
            }
        }
        let mut f = fixture();
        let dropped = Arc::new(AtomicUsize::new(0));
        let baseline = f.carriers.live.load(Ordering::SeqCst);
        f.carriers.deny_next();
        assert_eq!(ResourceDeliveryAttachment::new(Dropped(dropped.clone(), false),
            &f.carriers.owner()).unwrap_err(), HostAllocationError::Limit);
        assert_eq!(dropped.load(Ordering::SeqCst), 1);
        assert_eq!(f.carriers.live.load(Ordering::SeqCst), baseline);
        f.carriers.allow();
        let extra = ResourceDeliveryAttachment::new(Dropped(dropped.clone(), false),
            &f.carriers.owner()).unwrap();
        let (error, retained) = f.store.resource_delivery_runtime_attachment(extra).unwrap_err();
        assert_eq!(error, HostAllocationError::Unavailable);
        assert_eq!(dropped.load(Ordering::SeqCst), 1);
        assert!(f.carriers.live.load(Ordering::SeqCst) > baseline);
        drop(retained);
        assert_eq!(dropped.load(Ordering::SeqCst), 2);
        assert_eq!(f.carriers.live.load(Ordering::SeqCst), baseline);
        #[cfg(panic = "unwind")]
        {
            let panicking = ResourceDeliveryAttachment::new(Dropped(dropped.clone(), true),
                &f.carriers.owner()).unwrap();
            assert!(std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                drop(panicking);
            })).is_err());
            assert_eq!(dropped.load(Ordering::SeqCst), 3);
            assert_eq!(f.carriers.live.load(Ordering::SeqCst), baseline);
        }
        let Fixture { store, payload, events: _, runtime, data, carriers, control } = f;
        drop(store);
        drop(payload);
        assert_final(&runtime, &data, &carriers, &control);
    }

    #[tokio::test]
    async fn b1_store_drop_keeps_runtime_and_payload_custody() {
        // No B1 activation API exists. The active case is an ordinary legacy
        // active runtime entry; it is physical lifetime evidence, not an ACK.
        for mode in ["pending", "unknown", "legacy-active"] {
            let mut f = fixture();
            let (id, rep, index) = seed(&mut f).await;
            f.store.as_context_mut().on_fiber(|store| -> Result<()> {
                if mode == "unknown" {
                    store.0.resource_delivery_mark_unknown(id)?;
                } else if mode == "legacy-active" {
                    assert_eq!(store.0.resource_delivery_rollback_runtime(id)?, rep);
                    HostResourceTables::new_host(store.0)?.host_resource_lower_own(rep, None, None)?;
                } else {
                    assert!(HostResourceTables::new_host(store.0)?.host_resource_lift_own(index).is_err());
                }
                Ok(())
            }).await.unwrap().unwrap();
            let Fixture { store, payload, events, runtime, data, carriers, control } = f;
            assert!(payload.is_none());
            drop(store);
            assert_final(&runtime, &data, &carriers, &control);
            let events = events.lock().unwrap();
            assert!(pos(&events, "payload-drop") < pos(&events, "data-participant-drop"));
            assert!(pos(&events, "data-participant-drop") < pos(&events, "runtime-participant-drop"));
            assert!(pos(&events, "runtime-participant-drop") < pos(&events, "shared-payload-drop"));
            assert!(pos(&events, "shared-payload-drop") < pos(&events, "shared-block-release"));
            assert_eq!(events.iter().filter(|e| **e == "shared-block-release").count(), 1);
        }
    }

    #[tokio::test]
    async fn b1_into_data_and_typed_fixture_keep_independent_custody() {
        let mut f = fixture();
        let (_id, rep, _index) = seed(&mut f).await;
        let Fixture { store, payload, events, runtime, data, carriers, control } = f;
        assert!(payload.is_none());
        let mut extracted = store.into_data();
        assert_eq!(runtime.live.load(Ordering::SeqCst), 0);
        assert!(data.live.load(Ordering::SeqCst) > 0);
        assert!(control.live.load(Ordering::SeqCst) > 0);
        assert!(!events.lock().unwrap().contains(&"shared-payload-drop"));
        // Real ResourceTable typed delete frees its outer Box while returned
        // Payload and its independent participant remain alive. Actual
        // HostSecret/supervisor tests are mandatory Stage C, not claimed here.
        let retained = extracted.table.delete(Resource::<Payload>::new_own(rep)).unwrap();
        let _ = &extracted.keeper;
        drop(extracted);
        assert_eq!(data.live.load(Ordering::SeqCst), 0);
        assert!(control.live.load(Ordering::SeqCst) > 0);
        assert!(carriers.live.load(Ordering::SeqCst) > 0);
        assert!(!events.lock().unwrap().contains(&"payload-drop"));
        drop(retained);
        assert_final(&runtime, &data, &carriers, &control);
        let events = events.lock().unwrap();
        assert!(pos(&events, "runtime-participant-drop") < pos(&events, "data-participant-drop"));
        assert!(pos(&events, "data-participant-drop") < pos(&events, "payload-drop"));
        assert!(pos(&events, "payload-drop") < pos(&events, "shared-payload-drop"));
        assert!(pos(&events, "shared-payload-drop") < pos(&events, "shared-block-release"));
    }

    #[tokio::test]
    async fn b1_reservation_denial_is_before_generation_and_identity_never_repeats() {
        let mut f = fixture();
        let proof = f.store.as_context_mut().0.delivery_test_proof();
        assert!(f.store.as_context_mut().0.resource_delivery_reserve(&proof).is_err());
        let meter = f.runtime.clone();
        f.store.as_context_mut().on_fiber(move |store| -> Result<()> {
            let proof = store.0.delivery_test_proof();
            let runtime_keeper = store.0.component_delivery_keeper.take().unwrap();
            assert!(store.0.resource_delivery_reserve(&proof).is_err());
            store.0.component_delivery_keeper = Some(runtime_keeper);
            meter.deny_next();
            let mut generated = 0;
            let preparation = (|| -> Result<_> {
                let id = store.0.resource_delivery_reserve(&proof)?;
                generated += 1;
                store.data_mut().table.push(42_u32)?;
                Ok(id)
            })();
            let error = preparation.unwrap_err();
            assert_eq!(error.downcast_ref::<HostAllocationError>(), Some(&HostAllocationError::Limit));
            assert_eq!(generated, 0);
            // The actual effect table has no generated entry after refusal.
            assert!(store.data_mut().table.delete(Resource::<u32>::new_own(0)).is_err());
            meter.allow();
            let first = store.0.resource_delivery_reserve(&proof)?;
            store.0.resource_delivery_retire_empty(first)?;
            let second = store.0.resource_delivery_reserve(&proof)?;
            assert_eq!(first.slot, second.slot);
            assert_ne!(first.owner, second.owner);
            assert!(store.0.resource_delivery_note_nominal(first, 0).is_err());
            for _ in 1..DeliveryRecords::MAX_RECORDS {
                store.0.resource_delivery_reserve(&proof)?;
            }
            assert_eq!(store.0.resource_delivery_reserve(&proof).unwrap_err()
                .downcast_ref::<HostAllocationError>(), Some(&HostAllocationError::Limit));
            store.0.component_data_mut().delivery_records.serial = u64::MAX;
            assert_eq!(store.0.resource_delivery_reserve(&proof).unwrap_err()
                .downcast_ref::<HostAllocationError>(), Some(&HostAllocationError::Overflow));
            Ok(())
        }).await.unwrap().unwrap();
        // The prepared but unused payload participant is still a real owner.
        let Fixture { store, payload, events: _, runtime, data, carriers, control } = f;
        drop(store);
        assert!(control.live.load(Ordering::SeqCst) > 0);
        drop(payload);
        assert_final(&runtime, &data, &carriers, &control);
    }

    #[tokio::test]
    async fn b1_actual_instance_transfer_denial_retains_source_then_rolls_back() {
        let mut f = fixture();
        let component = Component::new(f.store.engine(), r#"
            (component
                (import "r" (type $r (sub resource)))
                (core func $drop (canon resource.drop $r))
                (core module $m
                    (import "" "drop" (func $drop (param i32)))
                    (func (export "take") (param i32)
                        local.get 0
                        call $drop))
                (core instance $i (instantiate $m
                    (with "" (instance (export "drop" (func $drop))))))
                (func (export "take") (param "r" (own $r))
                    (canon lift (core func $i "take"))))
        "#).unwrap();
        let mut linker = Linker::new(f.store.engine());
        linker.root().resource("r", ResourceType::host::<Payload>(), |_, _| Ok(())).unwrap();
        let instance = linker.instantiate_async(&mut f.store, &component).await.unwrap();
        let (id, rep, host_index) = seed(&mut f).await;
        let meter = f.runtime.clone();
        f.store.as_context_mut().on_fiber(move |store| -> Result<()> {
            let ty = TypeResourceTableIndex::from_u32(0);
            meter.deny_next();
            let error = store.0.resource_delivery_transfer_to_guest(id, instance, ty).unwrap_err();
            assert_eq!(error.downcast_ref::<HostAllocationError>(), Some(&HostAllocationError::Limit));
            assert_eq!(store.0.delivery_test_stage(id)?, DeliveryStage::HostPending);
            assert!(HostResourceTables::new_host(store.0)?.host_resource_lift_own(host_index).is_err());
            meter.allow();
            let index = store.0.resource_delivery_transfer_to_guest(id, instance, ty)?;
            assert_eq!(store.0.delivery_test_stage(id)?, DeliveryStage::GuestPending);
            assert!(store.0.component_resource_tables(Some(instance))?.resource_rep(
                crate::runtime::vm::component::TypedResourceIndex::Component { ty, index }
            ).is_err());
            assert_eq!(store.0.resource_delivery_rollback_runtime(id)?, rep);
            assert_eq!(store.0.delivery_test_stage(id)?, DeliveryStage::Nominal);
            assert!(store.0.resource_delivery_rollback_runtime(id).is_err());
            // Nominal payload destruction is separate and still has a real owner.
            assert!(store.data_mut().table.get(&Resource::<Payload>::new_own(rep)).is_ok());
            Ok(())
        }).await.unwrap().unwrap();
        let Fixture { store, payload, events: _, runtime, data, carriers, control } = f;
        assert!(payload.is_none());
        drop(store);
        assert_final(&runtime, &data, &carriers, &control);
    }

    #[test]
    fn b1_canceled_real_fiber_retains_unknown_record_and_payload() {
        let mut f = fixture();
        let payload = f.payload.take().unwrap();
        let events = f.events.clone();
        let record = Arc::new(Mutex::new(None));
        let saved = record.clone();
        {
            let mut context = f.store.as_context_mut();
            let mut future = Box::pin(context.on_fiber(move |store| -> Result<()> {
                let proof = store.0.delivery_test_proof();
                let id = store.0.resource_delivery_reserve(&proof)?;
                let nominal = store.data_mut().table.push(Payload { keeper: payload, events })?.rep();
                store.0.resource_delivery_note_nominal(id, nominal)?;
                store.0.resource_delivery_insert_host(id)?;
                store.0.resource_delivery_mark_unknown(id)?;
                *saved.lock().unwrap() = Some(id);
                store.0.with_blocking(|_, cx| {
                    cx.block_on(core::pin::pin!(core::future::pending::<()>()))
                })
            }));
            let mut cx = core::task::Context::from_waker(std::task::Waker::noop());
            assert!(future.as_mut().poll(&mut cx).is_pending());
            // FiberFuture drop resumes its original fiber with cancellation;
            // no replacement task, ACK, activation, or guessed rollback exists.
            drop(future);
        }
        let id = record.lock().unwrap().unwrap();
        assert_eq!(f.store.as_context_mut().0.delivery_test_stage(id).unwrap(), DeliveryStage::Unknown);
        assert!(f.store.as_context_mut().0.resource_delivery_rollback_runtime(id).is_err());
        assert!(f.runtime.live.load(Ordering::SeqCst) > 0);
        assert!(f.data.live.load(Ordering::SeqCst) > 0);
        assert!(f.control.live.load(Ordering::SeqCst) > 0);
        assert!(!f.events.lock().unwrap().contains(&"payload-drop"));
        let Fixture { store, payload, events: _, runtime, data, carriers, control } = f;
        assert!(payload.is_none());
        drop(store);
        assert_final(&runtime, &data, &carriers, &control);
    }

    #[derive(Copy, Clone)]
    enum B2Wait { Complete, Error, Pending, WrongCompletion, DenyFuture, PanicDrop }

    struct B2Probe {
        authority: Mutex<Option<ResourceDeliveryCompletionAuthority>>,
        record: Mutex<Option<DeliveryRecordId>>,
        entered: AtomicUsize,
        generated: AtomicUsize,
        ready: AtomicUsize,
        polled: AtomicUsize,
        protocol_dropped: AtomicUsize,
        future_dropped: AtomicUsize,
        after_future_requests: AtomicUsize,
        meter: Arc<Meter>,
        events: Events,
    }
    impl B2Probe {
        fn new(f: &Fixture) -> Arc<Self> {
            Arc::new(Self {
                authority: Mutex::new(None), record: Mutex::new(None),
                entered: AtomicUsize::new(0), generated: AtomicUsize::new(0),
                ready: AtomicUsize::new(0), polled: AtomicUsize::new(0),
                protocol_dropped: AtomicUsize::new(0), future_dropped: AtomicUsize::new(0),
                after_future_requests: AtomicUsize::new(0),
                meter: f.runtime.clone(), events: f.events.clone(),
            })
        }
    }
    struct B2Protocol { probe: Arc<B2Probe>, wait: B2Wait, owner: HostAllocationOwner }
    impl Drop for B2Protocol {
        fn drop(&mut self) {
            self.probe.protocol_dropped.fetch_add(1, Ordering::SeqCst);
            self.probe.events.lock().unwrap().push("b2-protocol-drop");
        }
    }
    impl ResourceDeliverySecretProtocol for B2Protocol {
        fn abi_ready(&mut self, ready: ResourceDeliveryReady)
            -> Result<crate::component::BudgetedFuture<'static, Result<ResourceDeliveryCompletion>>>
        {
            self.probe.ready.fetch_add(1, Ordering::SeqCst);
            self.probe.events.lock().unwrap().push("b2-abi-ready");
            let authority = self.probe.authority.lock().unwrap().take()
                .ok_or(HostAllocationError::Invalid)?;
            if matches!(self.wait, B2Wait::DenyFuture) { self.probe.meter.deny_next(); }
            Ok(crate::component::BudgetedFuture::new(B2WaitFuture {
                probe: self.probe.clone(), wait: self.wait,
                ready: Some(ready), authority: Some(authority),
            }, &self.owner, 0)?)
        }
    }
    struct B2WaitFuture {
        probe: Arc<B2Probe>,
        wait: B2Wait,
        ready: Option<ResourceDeliveryReady>,
        authority: Option<ResourceDeliveryCompletionAuthority>,
    }
    impl Future for B2WaitFuture {
        type Output = Result<ResourceDeliveryCompletion>;
        fn poll(mut self: core::pin::Pin<&mut Self>, _: &mut core::task::Context<'_>)
            -> core::task::Poll<Self::Output>
        {
            self.probe.polled.fetch_add(1, Ordering::SeqCst);
            assert_eq!(self.probe.protocol_dropped.load(Ordering::SeqCst), 1);
            use core::task::Poll;
            match self.wait {
                B2Wait::Pending => Poll::Pending,
                B2Wait::Error => Poll::Ready(Err(HostAllocationError::Unavailable.into())),
                B2Wait::WrongCompletion => {
                    // Deliberately invalid vendor fixture token. No production
                    // code can construct its private fields or bypass C's ACK.
                    let ready = self.ready.take().unwrap();
                    Poll::Ready(Ok(ResourceDeliveryCompletion {
                        id: ready.id, invocation: ready.invocation + 1,
                    }))
                }
                B2Wait::Complete | B2Wait::DenyFuture | B2Wait::PanicDrop => Poll::Ready(self.authority.take().unwrap()
                    .complete_after_verified_original_ack(self.ready.take().unwrap())),
            }
        }
    }
    impl Drop for B2WaitFuture {
        fn drop(&mut self) {
            self.probe.future_dropped.fetch_add(1, Ordering::SeqCst);
            self.probe.after_future_requests.store(
                self.probe.meter.requests.load(Ordering::SeqCst), Ordering::SeqCst);
            self.probe.events.lock().unwrap().push("b2-wait-drop");
            if matches!(self.wait, B2Wait::PanicDrop) { panic!("deterministic late wait Drop unwind"); }
        }
    }

    fn b2_finalizer(probe: &Arc<B2Probe>, wait: B2Wait) -> ResourceDeliveryFinalizer {
        ResourceDeliveryFinalizer::new(B2Protocol {
            probe: probe.clone(), wait, owner: probe.meter.owner(),
        }, &probe.meter.owner()).unwrap()
    }

    const B2_SINGLE: &str = r#"
        (component
            (import "r" (type $r (sub resource)))
            (import "make" (func $make (result (own $r))))
            (core func $make (canon lower (func $make)))
            (core module $m
                (import "" "make" (func $make (result i32)))
                (func (export "run") (result i32)
                    call $make drop i32.const 7))
            (core instance $i (instantiate $m
                (with "" (instance (export "make" (func $make))))))
            (func (export "run") (result u32) (canon lift (core func $i "run"))))
    "#;
    const B2_LATER_RESULT: &str = r#"
        (component
            (import "r" (type $r (sub resource)))
            (type $pair (tuple (own $r) u32))
            (import "make" (func $make (result $pair)))
            (core module $mem (memory (export "memory") 1))
            (core instance $mem (instantiate $mem))
            (alias core export $mem "memory" (core memory $memory))
            (core func $make (canon lower (func $make) (memory $memory)))
            (core module $m
                (import "" "make" (func $make (param i32)))
                (func (export "run") (result i32)
                    i32.const 0 call $make i32.const 7))
            (core instance $i (instantiate $m
                (with "" (instance (export "make" (func $make))))))
            (func (export "run") (result u32) (canon lift (core func $i "run"))))
    "#;

    // Real registered func_new_async_owned -> canonical lower -> final hook.
    // Each callback creates its funded finalizer before reserve and generates
    // only after reserve. These are vendor identity fixtures, NOT Gate ACKs.
    async fn b2_function(
        f: &mut Fixture,
        wait: B2Wait,
        result_mode: u8, // 0 single, 1 late type error, 2 copied own twice, 3 void guest return
        second_reserve: bool,
        nested: Option<crate::component::Func>,
    ) -> (crate::component::Func, Arc<B2Probe>) {
        let probe = B2Probe::new(f);
        let callback_probe = probe.clone();
        let payload = Arc::new(Mutex::new(f.payload.take()));
        let mut linker = Linker::<Data>::new(f.store.engine());
        linker.root().resource("r", ResourceType::host::<Payload>(), |_, _| Ok(())).unwrap();
        linker.root().func_new_async_owned("make", &f.runtime.owner(),
            move |mut store, _, _, results| {
                let owner = callback_probe.meter.owner();
                let future_owner = owner.clone();
                let probe = callback_probe.clone();
                let payload = payload.clone();
                crate::component::BudgetedFuture::new(async move {
                    probe.entered.fetch_add(1, Ordering::SeqCst);
                    let (reservation, authority) = store.resource_delivery_secret_reserve(
                        b2_finalizer(&probe, wait))?;
                    *probe.record.lock().unwrap() = Some(reservation.id);
                    *probe.authority.lock().unwrap() = Some(authority);
                    if second_reserve {
                        // No second generator/consume is entered on refusal.
                        let denied = store.resource_delivery_secret_reserve(b2_finalizer(&probe, wait));
                        assert!(denied.is_err());
                        return Err(HostAllocationError::Unavailable.into());
                    }
                    probe.generated.fetch_add(1, Ordering::SeqCst);
                    let nominal = store.data_mut().table.push(Payload {
                        keeper: payload.lock().unwrap().take().unwrap(), events: probe.events.clone(),
                    })?.rep();
                    let resource = store.resource_delivery_secret_resource(
                        &reservation, nominal, ResourceType::host::<Payload>())?;
                    if let Some(nested) = nested {
                        let error = nested.call_async(&mut store, &[], &mut []).await.unwrap_err();
                        assert_eq!(error.downcast_ref::<HostAllocationError>(),
                            Some(&HostAllocationError::Unavailable));
                        return Err(error);
                    }
                    if matches!(result_mode, 1 | 2) {
                        let mut tuple = crate::component::BudgetedVecBuilder::new(2, &owner, 0)?;
                        tuple.push(crate::component::Val::Resource(resource))?;
                        tuple.push(if result_mode == 2 {
                            crate::component::Val::Resource(resource) // same Copy descriptor
                        } else { crate::component::Val::Bool(false) })?; // expected u32
                        results[0] = crate::component::Val::Tuple(tuple.finish()?);
                    } else {
                        results[0] = crate::component::Val::Resource(resource);
                    }
                    Ok(())
                }, &future_owner, 0)
            }).unwrap();
        let source = match result_mode {
            0 => B2_SINGLE.to_owned(),
            2 => B2_LATER_RESULT.replace("(tuple (own $r) u32)", "(tuple (own $r) (own $r))"),
            3 => B2_SINGLE.replace("(result i32)\n                    call $make drop i32.const 7",
                    "call $make drop")
                .replace("(func (export \"run\") (result u32)", "(func (export \"run\")"),
            _ => B2_LATER_RESULT.to_owned(),
        };
        let component = Component::new(f.store.engine(), &source).unwrap();
        let instance = linker.instantiate_async(&mut f.store, &component).await.unwrap();
        (instance.get_func(&mut f.store, "run").unwrap(), probe)
    }

    fn b2_assert_pending(f: &mut Fixture, probe: &B2Probe, stage: DeliveryStage) {
        let id = probe.record.lock().unwrap().unwrap();
        let context = f.store.as_context_mut();
        let opaque = &mut *context.0;
        assert_eq!(opaque.delivery_test_stage(id).unwrap(), stage);
        let (mut tables, records) = opaque.component_delivery_tables_and_records(None).unwrap();
        let record = records.get_mut(id).unwrap();
        if let Some(host) = &record.host {
            assert!(tables.host_resource_lift_own(host.host_index()).is_err());
        }
        assert!(f.runtime.live.load(Ordering::SeqCst) > 0);
        assert!(f.control.live.load(Ordering::SeqCst) > 0);
    }

    #[tokio::test]
    async fn b2_actual_callback_final_hook_activates_after_wait_and_future_drop() {
        let mut f = fixture();
        let (function, probe) = b2_function(&mut f, B2Wait::Complete, 0, false, None).await;
        let mut result = [crate::component::Val::Bool(false)];
        function.call_async(&mut f.store, &[], &mut result).await.unwrap();
        assert!(matches!(result[0], crate::component::Val::U32(7)));
        assert_eq!(probe.ready.load(Ordering::SeqCst), 1);
        assert_eq!(probe.polled.load(Ordering::SeqCst), 1);
        assert_eq!(probe.future_dropped.load(Ordering::SeqCst), 1);
        let id = probe.record.lock().unwrap().unwrap();
        assert_eq!(f.store.as_context_mut().0.delivery_test_stage(id).unwrap(), DeliveryStage::Active);
        let (instance, ticket) = {
            let context = f.store.as_context_mut();
            let record = &context.0.component_data().delivery_records.records[usize::from(id.slot)];
            let guest = record.guest.as_ref().unwrap();
            (guest.instance, guest.ticket.index())
        };
        // Actual canonical guest entry is usable only after the final hook.
        f.store.as_context_mut().on_fiber(|store| -> Result<()> {
            assert!(store.0.component_resource_tables(Some(instance))?.resource_rep(ticket).is_ok());
            Ok(())
        }).await.unwrap().unwrap();
        assert_eq!(probe.generated.load(Ordering::SeqCst), 1);
        let events = f.events.lock().unwrap();
        assert!(pos(&events, "b2-abi-ready") < pos(&events, "b2-protocol-drop"));
        assert!(pos(&events, "b2-protocol-drop") < pos(&events, "b2-wait-drop"));
    }

    #[tokio::test]
    async fn b2_later_real_result_lower_failure_never_waits_or_activates_first_resource() {
        let mut f = fixture();
        let (function, probe) = b2_function(&mut f, B2Wait::Complete, 1, false, None).await;
        assert!(function.call_async(&mut f.store, &[], &mut [crate::component::Val::Bool(false)])
            .await.is_err());
        b2_assert_pending(&mut f, &probe, DeliveryStage::Unknown);
        let id = probe.record.lock().unwrap().unwrap();
        let context = f.store.as_context_mut();
        let record = &context.0.component_data().delivery_records.records[usize::from(id.slot)];
        assert!(record.host.is_none());
        assert!(record.guest.is_some(), "earlier real resource lower was executed");
        assert_eq!(probe.ready.load(Ordering::SeqCst), 0);
        assert_eq!(probe.generated.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn b2_copied_resource_cannot_authorize_second_real_lower() {
        let mut f = fixture();
        let (function, probe) = b2_function(&mut f, B2Wait::Complete, 2, false, None).await;
        assert!(function.call_async(&mut f.store, &[], &mut [crate::component::Val::Bool(false)])
            .await.is_err());
        b2_assert_pending(&mut f, &probe, DeliveryStage::Unknown);
        let id = probe.record.lock().unwrap().unwrap();
        let context = f.store.as_context_mut();
        let record = &context.0.component_data().delivery_records.records[usize::from(id.slot)];
        assert!(record.guest.is_some());
        assert!(record.host.is_none());
        assert_eq!(probe.ready.load(Ordering::SeqCst), 0);
    }

    #[tokio::test]
    async fn b2_prepared_activation_has_no_later_reservation_or_callback_before_void_guest_return() {
        let mut f = fixture();
        let (function, probe) = b2_function(&mut f, B2Wait::Complete, 3, false, None).await;
        function.call_async(&mut f.store, &[], &mut []).await.unwrap();
        assert_eq!(probe.meter.requests.load(Ordering::SeqCst),
            probe.after_future_requests.load(Ordering::SeqCst));
        assert_eq!(probe.entered.load(Ordering::SeqCst), 1);
        assert_eq!(probe.ready.load(Ordering::SeqCst), 1);
        assert_eq!(probe.future_dropped.load(Ordering::SeqCst), 1);
        let id = probe.record.lock().unwrap().unwrap();
        assert_eq!(f.store.as_context_mut().0.delivery_test_stage(id).unwrap(), DeliveryStage::Active);
    }

    #[tokio::test]
    async fn b2_dropped_actual_guest_preparation_preserves_pending_entry_without_reservation() {
        let mut f = fixture();
        let mut linker = Linker::<Data>::new(f.store.engine());
        linker.root().resource("r", ResourceType::host::<Payload>(), |_, _| Ok(())).unwrap();
        linker.root().func_new("make", |_, _, _, _| unreachable!()).unwrap();
        let component = Component::new(f.store.engine(), B2_SINGLE).unwrap();
        let instance = linker.instantiate_async(&mut f.store, &component).await.unwrap();
        let (id, _, _) = seed(&mut f).await;
        let meter = f.runtime.clone();
        f.store.as_context_mut().on_fiber(move |store| -> Result<()> {
            store.0.resource_delivery_transfer_to_guest(id, instance, TypeResourceTableIndex::from_u32(0))?;
            let index = {
                let (mut tables, records) = store.0.component_delivery_tables_and_records(Some(instance))?;
                let ticket = &records.get_mut(id)?.guest.as_ref().unwrap().ticket;
                let before = meter.requests.load(Ordering::SeqCst);
                meter.deny_next();
                drop(tables.guest_resource_pending_prepare(ticket)?);
                assert_eq!(meter.requests.load(Ordering::SeqCst), before);
                meter.allow();
                ticket.index()
            };
            assert!(store.0.component_resource_tables(Some(instance))?.resource_rep(index).is_err());
            assert_eq!(store.0.delivery_test_stage(id)?, DeliveryStage::GuestPending);
            Ok(())
        }).await.unwrap().unwrap();
    }

    #[cfg(panic = "unwind")]
    #[tokio::test]
    async fn b2_late_future_drop_unwind_cannot_activate_the_prepared_slot() {
        let mut f = fixture();
        let (function, probe) = b2_function(&mut f, B2Wait::PanicDrop, 0, false, None).await;
        assert!(std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let mut results = [crate::component::Val::Bool(false)];
            let mut call = Box::pin(function.call_async(&mut f.store, &[], &mut results));
            let mut cx = core::task::Context::from_waker(std::task::Waker::noop());
            let _ = call.as_mut().poll(&mut cx);
        })).is_err());
        b2_assert_pending(&mut f, &probe, DeliveryStage::Unknown);
        assert_eq!(probe.future_dropped.load(Ordering::SeqCst), 1);
        assert!(!f.events.lock().unwrap().contains(&"payload-drop"));
    }

    #[tokio::test]
    async fn b2_late_wait_error_or_mismatched_completion_retains_unknown_custody() {
        for wait in [B2Wait::Error, B2Wait::WrongCompletion, B2Wait::DenyFuture] {
            let mut f = fixture();
            let (function, probe) = b2_function(&mut f, wait, 0, false, None).await;
            assert!(function.call_async(&mut f.store, &[], &mut [crate::component::Val::Bool(false)])
                .await.is_err());
            b2_assert_pending(&mut f, &probe, DeliveryStage::Unknown);
            assert_eq!(probe.ready.load(Ordering::SeqCst), 1);
            assert_eq!(probe.future_dropped.load(Ordering::SeqCst), 1);
            assert!(!f.events.lock().unwrap().contains(&"payload-drop"));
        }
    }

    #[tokio::test]
    async fn b2_late_wait_cancellation_keeps_original_store_record_and_payload_funded() {
        let mut f = fixture();
        let (function, probe) = b2_function(&mut f, B2Wait::Pending, 0, false, None).await;
        {
            let mut results = [crate::component::Val::Bool(false)];
            let mut call = Box::pin(function.call_async(&mut f.store, &[], &mut results));
            let mut cx = core::task::Context::from_waker(std::task::Waker::noop());
            assert!(call.as_mut().poll(&mut cx).is_pending());
            assert_eq!(probe.ready.load(Ordering::SeqCst), 1);
            assert_eq!(probe.future_dropped.load(Ordering::SeqCst), 0);
            drop(call);
        }
        b2_assert_pending(&mut f, &probe, DeliveryStage::Unknown);
        assert_eq!(probe.future_dropped.load(Ordering::SeqCst), 1);
        assert!(!f.events.lock().unwrap().contains(&"payload-drop"));
    }

    #[tokio::test]
    async fn b2_second_optin_reservation_fails_before_second_generation() {
        let mut f = fixture();
        let (function, probe) = b2_function(&mut f, B2Wait::Complete, 0, true, None).await;
        assert!(function.call_async(&mut f.store, &[], &mut [crate::component::Val::Bool(false)])
            .await.is_err());
        b2_assert_pending(&mut f, &probe, DeliveryStage::Unknown);
        assert_eq!(probe.entered.load(Ordering::SeqCst), 1);
        assert_eq!(probe.generated.load(Ordering::SeqCst), 0);
        assert_eq!(probe.ready.load(Ordering::SeqCst), 0);
    }

    #[tokio::test]
    async fn b2_pending_nested_real_callback_is_rejected_before_entry() {
        let mut f = fixture();
        let entered = Arc::new(AtomicUsize::new(0));
        let counted = entered.clone();
        let mut linker = Linker::<Data>::new(f.store.engine());
        linker.root().func_new("nested", move |_, _, _, _| {
            counted.fetch_add(1, Ordering::SeqCst);
            Ok(())
        }).unwrap();
        let component = Component::new(f.store.engine(), r#"
            (component
                (import "nested" (func $nested))
                (core func $nested (canon lower (func $nested)))
                (core module $m
                    (import "" "nested" (func $nested))
                    (func (export "run") call $nested))
                (core instance $i (instantiate $m
                    (with "" (instance (export "nested" (func $nested))))))
                (func (export "run") (canon lift (core func $i "run"))))
        "#).unwrap();
        let instance = linker.instantiate_async(&mut f.store, &component).await.unwrap();
        let nested = instance.get_func(&mut f.store, "run").unwrap();
        let (function, probe) = b2_function(&mut f, B2Wait::Complete, 0, false, Some(nested)).await;
        assert!(function.call_async(&mut f.store, &[], &mut [crate::component::Val::Bool(false)])
            .await.is_err());
        assert_eq!(entered.load(Ordering::SeqCst), 0);
        b2_assert_pending(&mut f, &probe, DeliveryStage::Unknown);
        assert_eq!(probe.generated.load(Ordering::SeqCst), 1);
        assert_eq!(probe.ready.load(Ordering::SeqCst), 0);
    }

    #[tokio::test]
    async fn b2_sync_registration_and_missing_attachment_refuse_before_generation() {
        let mut f = fixture();
        let generated = Arc::new(AtomicUsize::new(0));
        let count = generated.clone();
        let probe = B2Probe::new(&f);
        let callback_probe = probe.clone();
        let mut linker = Linker::<Data>::new(f.store.engine());
        linker.root().resource("r", ResourceType::host::<Payload>(), |_, _| Ok(())).unwrap();
        linker.root().func_new("make", move |mut store, _, _, _| {
            // It IS on a native fiber, but is not an ordinary async registration.
            assert!(store.0.can_block());
            store.resource_delivery_secret_reserve(b2_finalizer(&callback_probe, B2Wait::Complete))?;
            count.fetch_add(1, Ordering::SeqCst);
            Ok(())
        }).unwrap();
        let component = Component::new(f.store.engine(), B2_SINGLE).unwrap();
        let instance = linker.instantiate_async(&mut f.store, &component).await.unwrap();
        let function = instance.get_func(&mut f.store, "run").unwrap();
        assert!(function.call_async(&mut f.store, &[], &mut [crate::component::Val::Bool(false)])
            .await.is_err());
        assert_eq!(generated.load(Ordering::SeqCst), 0);
        assert!(probe.record.lock().unwrap().is_none());

        let mut missing = fixture();
        let runtime = missing.store.as_context_mut().0.component_delivery_keeper.take().unwrap();
        let (function, probe) = b2_function(&mut missing, B2Wait::Complete, 0, false, None).await;
        assert!(function.call_async(&mut missing.store, &[], &mut [crate::component::Val::Bool(false)])
            .await.is_err());
        assert_eq!(probe.generated.load(Ordering::SeqCst), 0);
        assert!(probe.record.lock().unwrap().is_none());
        // Put the same participant back; this is not a new keeper/authority.
        missing.store.as_context_mut().0.component_delivery_keeper = Some(runtime);
    }

    #[test]
    fn b2_completion_authority_rejects_cross_store_record_and_callback_pairing() {
        // Non-Copy issuer/challenge pairing itself does not authenticate Gate.
        let mut first = fixture();
        let mut second = fixture();
        let first_id = first.store.as_context_mut().0.id();
        let second_id = second.store.as_context_mut().0.id();
        let id = DeliveryRecordId {
            store: first_id, slot: 0,
            owner: PendingOwnerKey::new_stage_a_seam(first_id, 1).unwrap(),
        };
        for wrong in [
            DeliveryRecordId { store: second_id, ..id },
            DeliveryRecordId { slot: 1, ..id },
            DeliveryRecordId { owner: PendingOwnerKey::new_stage_a_seam(first_id, 2).unwrap(), ..id },
        ] {
            assert!(ResourceDeliveryCompletionAuthority { id, invocation: 1 }
                .complete_after_verified_original_ack(ResourceDeliveryReady { id: wrong, invocation: 1 })
                .is_err());
        }
        assert!(ResourceDeliveryCompletionAuthority { id, invocation: 1 }
            .complete_after_verified_original_ack(ResourceDeliveryReady { id, invocation: 2 }).is_err());
    }

    #[derive(Copy, Clone)]
    enum VWait { Complete, Pending, Error, WrongKindCompletion, WrongKindReady }
    #[derive(Copy, Clone)]
    enum VResult { Scalar, Void, InvalidDestination, LaterTypeFailure, ResourceBranch, SecondReserve, KindSwitch, TakenBlockingContext }
    struct VProbe {
        authority: Mutex<Option<HostDeliveryCompletionAuthority>>,
        record: Mutex<Option<DeliveryRecordId>>,
        effects: AtomicUsize,
        ready: AtomicUsize,
        adapter_dropped: AtomicUsize,
        wait_dropped: AtomicUsize,
        requests_at_wait_drop: AtomicUsize,
        meter: Arc<Meter>,
    }
    struct VProtocol { probe: Arc<VProbe>, wait: VWait }
    impl Drop for VProtocol {
        fn drop(&mut self) { self.probe.adapter_dropped.fetch_add(1, Ordering::SeqCst); }
    }
    impl HostDeliveryProtocol for VProtocol {
        fn abi_ready(&mut self, ready: HostDeliveryReady)
            -> Result<crate::component::BudgetedFuture<'static, Result<HostDeliveryCompletion>>>
        {
            self.probe.ready.fetch_add(1, Ordering::SeqCst);
            let authority = self.probe.authority.lock().unwrap().take().unwrap();
            Ok(crate::component::BudgetedFuture::new(VFuture {
                probe: self.probe.clone(), wait: self.wait, ready: Some(ready), authority: Some(authority),
            }, &self.probe.meter.owner(), 0)?)
        }
    }
    struct VFuture {
        probe: Arc<VProbe>, wait: VWait,
        ready: Option<HostDeliveryReady>, authority: Option<HostDeliveryCompletionAuthority>,
    }
    impl Future for VFuture {
        type Output = Result<HostDeliveryCompletion>;
        fn poll(mut self: core::pin::Pin<&mut Self>, _: &mut core::task::Context<'_>)
            -> core::task::Poll<Self::Output>
        {
            use core::task::Poll;
            assert_eq!(self.probe.adapter_dropped.load(Ordering::SeqCst), 1);
            match self.wait {
                VWait::Pending => Poll::Pending,
                VWait::Error => Poll::Ready(Err(HostAllocationError::Unavailable.into())),
                VWait::WrongKindCompletion => {
                    let ready = self.ready.take().unwrap();
                    Poll::Ready(Ok(HostDeliveryCompletion {
                        id: ready.id, invocation: ready.invocation, kind: HostDeliveryKind::Resource,
                    }))
                }
                VWait::WrongKindReady => {
                    let mut ready = self.ready.take().unwrap();
                    ready.kind = HostDeliveryKind::Resource;
                    Poll::Ready(self.authority.take().unwrap().complete_after_verified_original_ack(ready))
                }
                VWait::Complete => Poll::Ready(self.authority.take().unwrap()
                    .complete_after_verified_original_ack(self.ready.take().unwrap())),
            }
        }
    }
    impl Drop for VFuture {
        fn drop(&mut self) {
            self.probe.wait_dropped.fetch_add(1, Ordering::SeqCst);
            self.probe.requests_at_wait_drop.store(
                self.probe.meter.requests.load(Ordering::SeqCst), Ordering::SeqCst);
        }
    }
    fn value_finalizer(probe: &Arc<VProbe>, wait: VWait) -> HostDeliveryFinalizer {
        HostDeliveryFinalizer::new(VProtocol { probe: probe.clone(), wait }, &probe.meter.owner()).unwrap()
    }
    const VALUE_SCALAR: &str = r#"
        (component
            (import "make" (func $make (result u32)))
            (core func $make (canon lower (func $make)))
            (core module $m
                (import "" "make" (func $make (result i32)))
                (func (export "run") (result i32) call $make))
            (core instance $i (instantiate $m
                (with "" (instance (export "make" (func $make))))))
            (func (export "run") (result u32) (canon lift (core func $i "run"))))
    "#;
    const VALUE_PAIR: &str = r#"
        (component
            (type $pair (tuple u32 u32))
            (import "make" (func $make (result $pair)))
            (core module $mem (memory (export "memory") 1))
            (core instance $mem (instantiate $mem))
            (alias core export $mem "memory" (core memory $memory))
            (core func $make (canon lower (func $make) (memory $memory)))
            (core module $m
                (import "" "make" (func $make (param i32)))
                (func (export "run") (result i32) i32.const 0 call $make i32.const 41))
            (core instance $i (instantiate $m
                (with "" (instance (export "make" (func $make))))))
            (func (export "run") (result u32) (canon lift (core func $i "run"))))
    "#;

    // A genuinely resource-free Store<()> fixture, not a fake resource keeper.
    // Protocol completion here proves neutral vendor identity only, never Gate.
    async fn value_function(
        result_mode: VResult, wait: VWait, deny_admission: bool,
    ) -> (Store<()>, crate::component::Func, Arc<VProbe>) {
        let events = Arc::new(Mutex::new(Vec::new()));
        let meter = Meter::new(&events, "value-storage-release");
        let mut config = Config::new();
        config.wasm_component_model(true);
        let engine = Engine::new(&config).unwrap();
        let mut store = Store::new(&engine, ());
        store.host_allocation_budget(meter.owner());
        assert!(store.as_context_mut().0.component_delivery_keeper.is_none());
        let probe = Arc::new(VProbe {
            authority: Mutex::new(None), record: Mutex::new(None), effects: AtomicUsize::new(0),
            ready: AtomicUsize::new(0), adapter_dropped: AtomicUsize::new(0),
            wait_dropped: AtomicUsize::new(0), requests_at_wait_drop: AtomicUsize::new(0), meter,
        });
        let callback_probe = probe.clone();
        let callback_owner = probe.meter.owner();
        let mut linker = Linker::<()>::new(store.engine());
        if matches!(result_mode, VResult::ResourceBranch) {
            linker.root().resource("r", ResourceType::host::<u32>(), |_, _| Ok(())).unwrap();
        }
        linker.root().func_new_async_owned("make", &probe.meter.owner(), move |mut store, _, _, results| {
            let owner = callback_owner.clone();
            let future_owner = owner.clone();
            let probe = callback_probe.clone();
            crate::component::BudgetedFuture::new(async move {
                let finalizer = value_finalizer(&probe, wait);
                if deny_admission { probe.meter.deny_next(); }
                if matches!(result_mode, VResult::TakenBlockingContext) {
                    // This is the real func_new_async_owned callback poll,
                    // inside the original exclusive BlockingContext::with.
                    assert!(!store.0.can_block());
                    let call = store.0.component_data().delivery_records.current.as_ref().unwrap();
                    assert!(call.supported);
                    assert_ne!(call.invocation, 0);
                    assert!(call.record.is_none());
                }
                let (reservation, authority) = store.host_delivery_reserve(HostDeliveryKind::Value, finalizer)?;
                *probe.record.lock().unwrap() = Some(reservation.id);
                *probe.authority.lock().unwrap() = Some(authority);
                if matches!(result_mode, VResult::KindSwitch) {
                    assert!(store.host_delivery_resource(&reservation, 0, ResourceType::host::<u32>()).is_err());
                    return Err(HostAllocationError::Unavailable.into());
                }
                probe.effects.fetch_add(1, Ordering::SeqCst);
                if matches!(result_mode, VResult::SecondReserve) {
                    assert!(store.host_delivery_reserve(HostDeliveryKind::Value, value_finalizer(&probe, wait)).is_err());
                    return Err(HostAllocationError::Unavailable.into());
                }
                if matches!(result_mode, VResult::InvalidDestination | VResult::LaterTypeFailure) {
                    let mut tuple = crate::component::BudgetedVecBuilder::new(2, &owner, 0)?;
                    tuple.push(crate::component::Val::U32(41))?;
                    tuple.push(if matches!(result_mode, VResult::LaterTypeFailure) {
                        crate::component::Val::Bool(false)
                    } else { crate::component::Val::U32(42) })?;
                    results[0] = crate::component::Val::Tuple(tuple.finish()?);
                } else {
                    results[0] = crate::component::Val::U32(41);
                }
                Ok(())
            }, &future_owner, 0)
        }).unwrap();
        let source = match result_mode {
            VResult::InvalidDestination => VALUE_PAIR.replace("i32.const 0 call $make", "i32.const 65535 call $make"),
            VResult::LaterTypeFailure => VALUE_PAIR.to_owned(),
            VResult::ResourceBranch => VALUE_PAIR
                .replace("(type $pair (tuple u32 u32))", "(import \"r\" (type $r (sub resource)))\n(type $pair (tuple u32 (option (own $r))))"),
            VResult::Void => VALUE_SCALAR
                .replace("(func (export \"run\") (result i32) call $make)", "(func (export \"run\") call $make drop)")
                .replace("(func (export \"run\") (result u32)", "(func (export \"run\")"),
            _ => VALUE_SCALAR.to_owned(),
        };
        let component = Component::new(store.engine(), &source).unwrap();
        let instance = linker.instantiate_async(&mut store, &component).await.unwrap();
        let function = instance.get_func(&mut store, "run").unwrap();
        (store, function, probe)
    }

    fn assert_value_stage(store: &mut Store<()>, probe: &VProbe, stage: DeliveryStage) {
        let id = probe.record.lock().unwrap().unwrap();
        let context = store.as_context_mut();
        let record = &context.0.component_data().delivery_records.records[usize::from(id.slot)];
        assert_eq!(record.id, id);
        assert_eq!(record.kind, HostDeliveryKind::Value);
        assert_eq!(record.stage, stage);
        assert!(record.nominal.is_none() && record.host.is_none() && record.guest.is_none());
        assert!(record.resource_type.is_none());
    }

    #[tokio::test]
    async fn neutral_value_only_real_callback_requires_no_resource_attachment_or_ticket() {
        let (mut store, function, probe) = value_function(VResult::Scalar, VWait::Complete, false).await;
        let mut results = [crate::component::Val::Bool(false)];
        function.call_async(&mut store, &[], &mut results).await.unwrap();
        assert!(matches!(results[0], crate::component::Val::U32(41)));
        assert_value_stage(&mut store, &probe, DeliveryStage::Delivered);
        assert_eq!(probe.effects.load(Ordering::SeqCst), 1);
        assert_eq!(probe.ready.load(Ordering::SeqCst), 1);
        assert_eq!(probe.wait_dropped.load(Ordering::SeqCst), 1);
        assert!(store.as_context_mut().0.component_delivery_keeper.is_none());
        drop(store);
        assert_eq!(probe.meter.live.load(Ordering::SeqCst), 0);
    }

    #[tokio::test]
    async fn neutral_value_final_wait_drop_precedes_infallible_return_and_record_reuse_is_fresh() {
        let (mut store, function, probe) = value_function(VResult::Void, VWait::Complete, false).await;
        function.call_async(&mut store, &[], &mut []).await.unwrap();
        assert_eq!(probe.meter.requests.load(Ordering::SeqCst), probe.requests_at_wait_drop.load(Ordering::SeqCst));
        let first = probe.record.lock().unwrap().unwrap();
        assert_value_stage(&mut store, &probe, DeliveryStage::Delivered);
        // The finished Value slot has no payload/tickets and may be reused with
        // a NEW serial. Reset only fixture counters, never runtime identity.
        probe.adapter_dropped.store(0, Ordering::SeqCst);
        function.call_async(&mut store, &[], &mut []).await.unwrap();
        let second = probe.record.lock().unwrap().unwrap();
        assert_eq!(first.slot, second.slot);
        assert_ne!(first.owner, second.owner);
        assert!(store.as_context_mut().0.component_data_mut().delivery_records.get_mut(first).is_err());
        assert_value_stage(&mut store, &probe, DeliveryStage::Delivered);
    }

    #[tokio::test]
    async fn neutral_value_actual_late_abi_destination_and_result_failures_never_issue_ready() {
        for mode in [VResult::InvalidDestination, VResult::LaterTypeFailure] {
            let (mut store, function, probe) = value_function(mode, VWait::Complete, false).await;
            assert!(function.call_async(&mut store, &[], &mut [crate::component::Val::Bool(false)]).await.is_err());
            assert_eq!(probe.effects.load(Ordering::SeqCst), 1);
            assert_eq!(probe.ready.load(Ordering::SeqCst), 0);
            assert_value_stage(&mut store, &probe, DeliveryStage::Unknown);
            assert!(probe.meter.live.load(Ordering::SeqCst) > 0);
        }
    }

    #[tokio::test]
    async fn neutral_value_canceled_actual_final_wait_keeps_unknown_funded_record() {
        let (mut store, function, probe) = value_function(VResult::Scalar, VWait::Pending, false).await;
        {
            let mut results = [crate::component::Val::Bool(false)];
            let mut call = Box::pin(function.call_async(&mut store, &[], &mut results));
            let mut cx = core::task::Context::from_waker(std::task::Waker::noop());
            assert!(call.as_mut().poll(&mut cx).is_pending());
            assert_eq!(probe.ready.load(Ordering::SeqCst), 1);
            assert_eq!(probe.wait_dropped.load(Ordering::SeqCst), 0);
            drop(call);
        }
        assert_value_stage(&mut store, &probe, DeliveryStage::Unknown);
        assert_eq!(probe.wait_dropped.load(Ordering::SeqCst), 1);
        assert!(probe.meter.live.load(Ordering::SeqCst) > 0);
        drop(store);
        assert_eq!(probe.meter.live.load(Ordering::SeqCst), 0);
    }

    #[tokio::test]
    async fn neutral_value_errors_and_cross_kind_ready_or_completion_cannot_deliver() {
        for wait in [VWait::Error, VWait::WrongKindReady, VWait::WrongKindCompletion] {
            let (mut store, function, probe) = value_function(VResult::Scalar, wait, false).await;
            assert!(function.call_async(&mut store, &[], &mut [crate::component::Val::Bool(false)]).await.is_err());
            assert_eq!(probe.ready.load(Ordering::SeqCst), 1);
            assert_eq!(probe.wait_dropped.load(Ordering::SeqCst), 1);
            assert_value_stage(&mut store, &probe, DeliveryStage::Unknown);
        }
    }

    #[tokio::test]
    async fn neutral_value_actual_resource_result_branch_and_owner_denial_refuse_before_effects() {
        for (mode, denial) in [(VResult::ResourceBranch, false), (VResult::Scalar, true)] {
            let (mut store, function, probe) = value_function(mode, VWait::Complete, denial).await;
            assert!(function.call_async(&mut store, &[], &mut [crate::component::Val::Bool(false)]).await.is_err());
            assert_eq!(probe.effects.load(Ordering::SeqCst), 0);
            assert_eq!(probe.ready.load(Ordering::SeqCst), 0);
            assert!(probe.record.lock().unwrap().is_none());
            assert!(store.as_context_mut().0.component_data().delivery_records.records.is_empty());
        }
    }

    #[tokio::test]
    async fn neutral_value_cannot_switch_to_resource_or_consume_second_optin_effect() {
        for (mode, effects) in [(VResult::KindSwitch, 0), (VResult::SecondReserve, 1)] {
            let (mut store, function, probe) = value_function(mode, VWait::Complete, false).await;
            assert!(function.call_async(&mut store, &[], &mut [crate::component::Val::Bool(false)]).await.is_err());
            assert_eq!(probe.effects.load(Ordering::SeqCst), effects);
            assert_eq!(probe.ready.load(Ordering::SeqCst), 0);
            assert_value_stage(&mut store, &probe, DeliveryStage::Unknown);
        }
    }

    #[tokio::test]
    async fn neutral_live_callback_admits_same_original_taken_blocking_context() {
        let (mut store, function, probe) = value_function(VResult::TakenBlockingContext, VWait::Complete, false).await;
        let mut results = [crate::component::Val::Bool(false)];
        function.call_async(&mut store, &[], &mut results).await.unwrap();
        assert!(matches!(results[0], crate::component::Val::U32(41)));
        assert_value_stage(&mut store, &probe, DeliveryStage::Delivered);
        assert_eq!(probe.effects.load(Ordering::SeqCst), 1);
        assert_eq!(probe.ready.load(Ordering::SeqCst), 1);
        assert_eq!(probe.adapter_dropped.load(Ordering::SeqCst), 1);
        assert_eq!(probe.wait_dropped.load(Ordering::SeqCst), 1);
        drop(store);
        assert_eq!(probe.meter.live.load(Ordering::SeqCst), 0);
    }
}
