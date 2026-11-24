//! Neutral final-ABI delivery identity and independently funded protocol erasure.
//! This module neither grants execution authority nor authenticates a terminal ACK.

/// Fixed before the first domain effect in one opt-in callback invocation.
/// Value admits only genuinely resource-free result types; Resource retains
/// the exact provisional ticket, keeper and prepared activation requirements.
#[derive(Debug, Copy, Clone, PartialEq, Eq)]
pub enum HostDeliveryKind {
    /// An ordinary owned value with no resource anywhere in its declared result.
    Value,
    /// A provisional resource whose actual runtime slot must remain pending.
    Resource,
}

#[cfg(feature = "async")]
mod protocol {
    use super::HostDeliveryKind;
    use crate::component::delivery::{
        DeliveryCallProof, DeliveryRecordId, ResourceDeliveryCompletion,
        ResourceDeliveryFinalizer, ResourceDeliveryReady,
    };
    use crate::component::{BudgetedFuture, HostAllocationError, HostAllocationKind,
        HostAllocationLease, HostAllocationOwner};
    use crate::prelude::*;
    use crate::StoreContextMut;
    use core::future::Future;
    use core::mem::size_of;
    use core::pin::Pin;
    use core::task::{Context, Poll};

    /// Descriptive reservation identity. Dropping it does not settle a job.
    pub struct HostDeliveryReservation {
        pub(in crate::runtime::component::delivery) id: DeliveryRecordId,
        pub(in crate::runtime::component::delivery) invocation: u64,
        pub(in crate::runtime::component::delivery) kind: HostDeliveryKind,
    }

    /// Unique non-Copy token moved to the ORIGINAL job's existing supervisor.
    /// It is neither an execution permit nor a terminal acknowledgement.
    pub struct HostDeliveryCompletionAuthority {
        pub(in crate::runtime::component::delivery) id: DeliveryRecordId,
        pub(in crate::runtime::component::delivery) invocation: u64,
        pub(in crate::runtime::component::delivery) kind: HostDeliveryKind,
    }

    /// Once-issued challenge from the last-fallible canonical ABI boundary.
    /// The challenge alone cannot authorize completion or resource activation.
    pub struct HostDeliveryReady {
        pub(in crate::runtime::component::delivery) id: DeliveryRecordId,
        pub(in crate::runtime::component::delivery) invocation: u64,
        pub(in crate::runtime::component::delivery) kind: HostDeliveryKind,
    }

    /// Exact kind/Store/callback/record-bound authority plus ready pairing.
    /// The trusted adapter must authenticate the ORIGINAL immutable terminal
    /// outcome/winner and SAME sealed consumed permit before producing this.
    pub struct HostDeliveryCompletion {
        pub(in crate::runtime::component::delivery) id: DeliveryRecordId,
        pub(in crate::runtime::component::delivery) invocation: u64,
        pub(in crate::runtime::component::delivery) kind: HostDeliveryKind,
    }

    impl HostDeliveryCompletionAuthority {
        /// Call only after the original supervisor's actual same-permit ACK.
        /// Scratch allocation/ACK/winner validation remain fallible in that
        /// supervisor. General Ok or another parent permit cannot substitute.
        pub fn complete_after_verified_original_ack(
            self,
            ready: HostDeliveryReady,
        ) -> Result<HostDeliveryCompletion> {
            if self.id != ready.id || self.invocation != ready.invocation || self.kind != ready.kind {
                return Err(HostAllocationError::Invalid.into());
            }
            Ok(HostDeliveryCompletion { id: self.id, invocation: self.invocation, kind: self.kind })
        }
    }

    /// Trusted integration surface for one delivery in one actual host callback.
    /// All real value graphs and physical jobs retain their own existing funded
    /// custody. Original job/permit custody must outlive adapter or wait Drop.
    /// A ready callback signals normal ABI return readiness, not invisibility
    /// of intermediate writes to guest memory during canonical realloc/lower.
    pub trait HostDeliveryProtocol: Send + Sync + 'static {
        /// Called once after whole result lower, scope/reentry and task deletion.
        /// No Store context/table guard is given to this callback or its wait.
        /// The adapter Box is destroyed before this funded future first polls;
        /// handoff must disarm its failure signal. Future Drop must preserve the
        /// ORIGINAL supervisor/job and signal truthful failure or uncertainty.
        fn abi_ready(
            &mut self,
            ready: HostDeliveryReady,
        ) -> Result<BudgetedFuture<'static, Result<HostDeliveryCompletion>>>;
    }

    /// Separately funded outer Box for an already prepared integration adapter.
    pub struct HostDeliveryFinalizer {
        protocol: Option<Box<dyn HostDeliveryProtocol>>,
        lease: Option<HostAllocationLease>,
    }

    impl HostDeliveryFinalizer {
        /// Fund this Box before generation, grant consumption or domain effects.
        /// Its captures/channels/job/control allocations need their own original
        /// funding; this constructor supplies no execution/terminal authority.
        pub fn new<P: HostDeliveryProtocol>(
            protocol: P,
            owner: &HostAllocationOwner,
        ) -> core::result::Result<Self, HostAllocationError> {
            let mut lease = HostAllocationLease::new(owner);
            lease.charge(HostAllocationKind::Metadata, size_of::<P>(), 1, 0, 1)?;
            let protocol: Box<dyn HostDeliveryProtocol> =
                try_new::<Box<P>>(protocol).map_err(|_| HostAllocationError::Allocator)?;
            Ok(Self { protocol: Some(protocol), lease: Some(lease) })
        }
    }

    impl Drop for HostDeliveryFinalizer {
        fn drop(&mut self) {
            let lease = self.lease.take();
            let protocol = self.protocol.take();
            drop(protocol);
            drop(lease);
        }
    }

    impl<T: 'static> StoreContextMut<'_, T> {
        /// Opt in before any resource generation, grant consumption or domain
        /// effect. Kind is immutable and bound into ready/completion identity.
        /// Exactly one opt-in record is admitted for this callback. Value's
        /// actual declared result graph is checked resource-free before effects;
        /// only Resource requires the independent runtime keeper attachment.
        pub fn host_delivery_reserve(
            &mut self,
            kind: HostDeliveryKind,
            finalizer: HostDeliveryFinalizer,
        ) -> Result<(HostDeliveryReservation, HostDeliveryCompletionAuthority)> {
            let invocation = self.0.component_data().delivery_records.current.as_ref()
                .filter(|call| call.supported && call.record.is_none())
                .ok_or(HostAllocationError::Unavailable)?.invocation;
            let proof = DeliveryCallProof { store: self.0.id(), invocation: Some(invocation) };
            let id = self.0.resource_delivery_reserve_kind(&proof, kind)?;
            self.0.component_data_mut().delivery_records.current.as_mut()
                .ok_or(HostAllocationError::Unavailable)?.finalizer = Some(DeliveryFinalizer::Host(finalizer));
            Ok((HostDeliveryReservation { id, invocation, kind },
                HostDeliveryCompletionAuthority { id, invocation, kind }))
        }

        /// Register an exact real nominal rep for a Resource-kind reservation.
        /// Value cannot be switched into a Resource or fabricate a pending slot.
        pub fn host_delivery_resource(
            &mut self,
            reservation: &HostDeliveryReservation,
            nominal: u32,
            ty: crate::component::ResourceType,
        ) -> Result<crate::component::ResourceAny> {
            if reservation.kind != HostDeliveryKind::Resource {
                return Err(HostAllocationError::Invalid.into());
            }
            self.0.resource_delivery_current_record(reservation.id, reservation.invocation)?;
            if self.0.component_data_mut().delivery_records.get_mut(reservation.id)?.kind
                != HostDeliveryKind::Resource
            { return Err(HostAllocationError::Invalid.into()); }
            self.0.resource_delivery_note_nominal(reservation.id, nominal)?;
            self.0.component_data_mut().delivery_records.get_mut(reservation.id)?.resource_type = Some(ty);
            let index = self.0.resource_delivery_insert_host(reservation.id)?;
            Ok(crate::component::ResourceAny::new_pending(index, ty, reservation.id))
        }
    }

    /// Old resource API remains source-compatible with the frozen B2 tests.
    /// Both erasures enter the same neutral final hook and identity checks.
    pub(in crate::runtime::component::delivery) enum DeliveryFinalizer {
        Resource(ResourceDeliveryFinalizer),
        Host(HostDeliveryFinalizer),
    }

    impl DeliveryFinalizer {
        pub(in crate::runtime::component::delivery) fn into_wait(
            self,
            ready: HostDeliveryReady,
        ) -> Result<DeliveryWaitFuture> {
            match self {
                Self::Host(mut finalizer) => {
                    let future = finalizer.protocol.as_mut()
                        .ok_or(HostAllocationError::Unavailable)?.abi_ready(ready)?;
                    drop(finalizer);
                    Ok(DeliveryWaitFuture::Host(future))
                }
                Self::Resource(mut finalizer) => {
                    if ready.kind != HostDeliveryKind::Resource {
                        return Err(HostAllocationError::Invalid.into());
                    }
                    let future = finalizer.protocol.as_mut()
                        .ok_or(HostAllocationError::Unavailable)?.abi_ready(ResourceDeliveryReady {
                            id: ready.id, invocation: ready.invocation,
                        })?;
                    drop(finalizer);
                    Ok(DeliveryWaitFuture::Resource(future))
                }
            }
        }
    }

    /// Stack-only output adaptation: no additional allocation, callback or ACK.
    /// block_on owns and destroys the entire enum/future before final commit.
    pub(in crate::runtime::component::delivery) enum DeliveryWaitFuture {
        Resource(BudgetedFuture<'static, Result<ResourceDeliveryCompletion>>),
        Host(BudgetedFuture<'static, Result<HostDeliveryCompletion>>),
    }

    impl Future for DeliveryWaitFuture {
        type Output = Result<HostDeliveryCompletion>;
        fn poll(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
            match self.get_mut() {
                Self::Host(future) => Pin::new(future).poll(cx),
                Self::Resource(future) => Pin::new(future).poll(cx).map(|result| result.map(|done| {
                    HostDeliveryCompletion {
                        id: done.id, invocation: done.invocation, kind: HostDeliveryKind::Resource,
                    }
                })),
            }
        }
    }
}

#[cfg(feature = "async")]
pub use protocol::{HostDeliveryCompletion, HostDeliveryCompletionAuthority,
    HostDeliveryFinalizer, HostDeliveryProtocol, HostDeliveryReady, HostDeliveryReservation};
#[cfg(feature = "async")]
pub(super) use protocol::DeliveryFinalizer;
