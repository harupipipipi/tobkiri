//! Fixed Gate inspection of private Core state and funded historical records.
use super::{
    AllocationReason, CallCx, CompiledCodeOwner, ControlHandle, ControlReservation, CoreVerifier,
    GateError, MutexGuard, NodeLifecycle, PreparedCodeOwner, ReachedState, allocation_proof,
    check_same_context, denied, invocation_record::InvocationRecord,
};
use crate::funded_arc::FundedArc;
use std::{any::Any, sync::atomic::Ordering, time::Instant};
use tobkiri_gate::{
    BorrowedActiveFrameMetadata, ReachedEvidenceView, ReachedInvocationMetadata,
    TypedReachedInvocation,
};

/// Both guards are borrowed from the one original admission/retirement path.
/// No constructor, cloned authority or native owner escapes this module.
struct LiveInvocationProof<'a> {
    node: &'a ReachedState,
    lifecycle: &'a MutexGuard<'a, NodeLifecycle>,
    _admission: &'a MutexGuard<'a, ()>,
    reason: AllocationReason,
}
impl TypedReachedInvocation for LiveInvocationProof<'_> {
    fn authenticate<'a>(
        &'a self,
        cx: &CallCx,
        reason: AllocationReason,
    ) -> Result<ReachedInvocationMetadata<'a>, GateError> {
        let node = self.node;
        if reason != self.reason
            || node.auth.closing.load(Ordering::Acquire)
            || !self.lifecycle.live
            || self.lifecycle.acknowledged
        {
            return Err(denied(
                "invocation inspection has no live original activation",
            ));
        }
        check_same_context(&node.context, cx)?;
        node.context.check_active()?;
        node.entry_context.check_active()?;
        let entry = node.entry_evidence.as_ref().ok_or(GateError::Unavailable)?;
        if !entry.matches_context(&node.entry_context)
            || entry.deadline() <= Instant::now()
            || cx.deadline() > entry.deadline()
        {
            return Err(denied(
                "invocation inspection differs from its original entry",
            ));
        }
        let metadata = &node.metadata;
        let owner = match &metadata.owner {
            CompiledCodeOwner::Pack { pack, hash } => PreparedCodeOwner::Pack { pack, hash },
            CompiledCodeOwner::User { profile_ast } => PreparedCodeOwner::User { profile_ast },
            CompiledCodeOwner::UserDocument { document } => {
                PreparedCodeOwner::UserDocument { document }
            }
        };
        Ok(ReachedInvocationMetadata {
            invocation: &node.frame,
            entry_grant: entry.grant(),
            entry_effect: entry.effect(),
            entry_args: entry.args_digest(),
            entry_context: &node.entry_context,
            entry_deadline: entry.deadline(),
            current: &node.context,
            active: BorrowedActiveFrameMetadata {
                profile: &metadata.profile,
                revision: &metadata.revision,
                security_epoch: metadata.security_epoch,
                principal: &metadata.principal,
                request: &metadata.request,
                frame: &metadata.frame,
                node: &metadata.node,
                activation: metadata.activation,
                owner,
                reason,
                path: &metadata.path,
                transitive_memory_bytes: metadata.transitive_memory_bytes,
            },
        })
    }
}

/// The independently prepaid carrier shares only the original record's funds.
/// It deliberately contains no ReachedState, provider frame or execution guard.
struct InvocationEvidenceReference {
    record: FundedArc<InvocationRecord>,
}

pub(super) fn with_invocation(
    verifier: &CoreVerifier,
    cx: &CallCx,
    proof: &(dyn Any + Send + Sync),
    reason: AllocationReason,
    inspect: &mut dyn FnMut(&dyn TypedReachedInvocation) -> Result<(), GateError>,
) -> Result<(), GateError> {
    // Immutable/frozen checks use the existing verifier before taking the
    // original locks. The stack proof below never reacquires either lock.
    let proof = verifier.validate(cx, proof, reason)?;
    let admission = verifier
        .auth
        .admission
        .lock()
        .map_err(|_| GateError::Unavailable)?;
    let lifecycle = proof
        .node
        .state
        .lock()
        .map_err(|_| GateError::Unavailable)?;
    let locked = LiveInvocationProof {
        node: &proof.node,
        lifecycle: &lifecycle,
        _admission: &admission,
        reason,
    };
    locked.authenticate(cx, reason)?;
    // This is Gate's fixed synchronous inspection, not caller/guest code.
    inspect(&locked)
}

pub(super) fn storage_bytes() -> Result<u64, GateError> {
    u64::try_from(
        ControlHandle::<InvocationEvidenceReference>::storage_bytes::<ControlReservation>(),
    )
    .map_err(|_| GateError::Unavailable)
}

fn retain(
    record: FundedArc<InvocationRecord>,
    funding: ControlReservation,
) -> Result<ControlHandle<dyn Any + Send + Sync>, GateError> {
    let reference =
        ControlHandle::try_new_precharged(InvocationEvidenceReference { record }, funding)
            .map_err(|_| GateError::Unavailable)?;
    Ok(reference.project(|value| value as &(dyn Any + Send + Sync)))
}

pub(super) fn retain_leaf(
    verifier: &CoreVerifier,
    proof: &(dyn Any + Send + Sync),
    funding: ControlReservation,
) -> Result<ControlHandle<dyn Any + Send + Sync>, GateError> {
    let node = &allocation_proof(proof)?.node;
    if !ControlHandle::ptr_eq(&verifier.auth, &node.auth) {
        return Err(GateError::Unavailable);
    }
    // The fixed Gate caller already holds the same actual node mutex. Do not
    // lock it again or copy a native owner into the returned metadata carrier.
    retain(node.invocation.clone(), funding)
}

fn record<'a>(
    verifier: &CoreVerifier,
    reference: &'a (dyn Any + Send + Sync),
) -> Result<&'a InvocationRecord, GateError> {
    let record = &reference
        .downcast_ref::<InvocationEvidenceReference>()
        .ok_or(GateError::Unavailable)?
        .record;
    if !ControlHandle::ptr_eq(&verifier.auth, &record.auth) {
        return Err(GateError::Unavailable);
    }
    Ok(record)
}

pub(super) fn matches_leaf(
    verifier: &CoreVerifier,
    proof: &(dyn Any + Send + Sync),
    reference: &(dyn Any + Send + Sync),
) -> Result<(), GateError> {
    let node = &allocation_proof(proof)?.node;
    let reference = reference
        .downcast_ref::<InvocationEvidenceReference>()
        .ok_or(GateError::Unavailable)?;
    if !ControlHandle::ptr_eq(&verifier.auth, &node.auth)
        || !FundedArc::ptr_eq(&node.invocation, &reference.record)
    {
        return Err(denied("evidence belongs to another actual invocation"));
    }
    Ok(())
}

pub(super) fn view<'a>(
    verifier: &CoreVerifier,
    reference: &'a (dyn Any + Send + Sync),
) -> Result<ReachedEvidenceView<'a>, GateError> {
    let record = record(verifier, reference)?;
    Ok(ReachedEvidenceView {
        invocation: &record.frame,
        entry: record
            .entry_evidence
            .as_deref()
            .ok_or(GateError::Unavailable)?,
        entry_context: (&record.entry_context).into(),
        current: (&record.context).into(),
        active: &record.metadata,
        has_parent: record.parent.is_some(),
    })
}

pub(super) fn retain_parent(
    verifier: &CoreVerifier,
    reference: &(dyn Any + Send + Sync),
    funding: ControlReservation,
) -> Result<ControlHandle<dyn Any + Send + Sync>, GateError> {
    let parent = record(verifier, reference)?
        .parent
        .as_ref()
        .ok_or(GateError::Unavailable)?;
    if !ControlHandle::ptr_eq(&verifier.auth, &parent.auth) {
        return Err(GateError::Unavailable);
    }
    retain(parent.clone(), funding)
}

#[cfg(test)]
mod tests {
    use super::super::control_admission_tests::{
        admit_native_frame, core_frame, finish_native_frame, new_factory,
    };
    use super::*;
    use std::sync::Arc;
    use tobkiri_gate::{ControlKind, Gate};

    #[tokio::test]
    async fn actual_entry_reference_is_prepaid_bound_and_rejected_after_retirement() {
        let fixture = crate::test_support::RuntimeFixture::new(&["fixture"], false).await;
        let session = fixture.session().await;
        let factory = new_factory(&session);
        let (frame, permit, args) = admit_native_frame(&factory, "evidence-original").await;
        let entry = core_frame(&frame).state.entry_evidence.as_ref().unwrap();
        assert!(frame.context().deadline() <= entry.deadline());
        assert!(core_frame(&frame).state.entry_context.deadline() <= entry.deadline());
        let scope = frame
            .allocation_scope(AllocationReason::RuntimeValue)
            .unwrap();
        let control = frame.context().control();
        let before = control.usage();
        // Reproduce another test's opportunistic process-wide collection.
        // Completed argument custody must already be gone; the actual live
        // frame must remain charged, with no looser counter comparison.
        std::thread::spawn(|| tobkiri_wasm::reap_allocation_owners().unwrap())
            .join()
            .unwrap();
        assert_eq!(control.usage(), before);
        let reference = factory
            .gate
            .inspect_reached_evidence(frame.context(), &scope)
            .unwrap();
        assert_eq!(
            control.usage().bytes,
            before.bytes + storage_bytes().unwrap()
        );
        assert_eq!(
            control.usage().count(ControlKind::Frame),
            before.count(ControlKind::Frame) + 1
        );
        let view = factory
            .gate
            .reached_evidence_view(frame.context(), &scope, &reference)
            .unwrap();
        assert_eq!(view.entry.grant(), permit.id());
        assert_eq!(view.entry.args_digest(), &args);
        assert_eq!(view.entry.effect(), permit.effect());
        assert_eq!(view.entry.trusted_approval(), permit.trusted_approval());
        assert_eq!(view.entry_context.profile, frame.context().profile());
        // Entry records the real caller; try_callee creates its own request.
        assert_eq!(view.entry_context.request.as_str(), "evidence-original");
        assert_ne!(view.entry_context.request, frame.context().request());
        assert_eq!(
            view.entry_context.principal,
            core_frame(&frame).state.entry_context.principal()
        );
        assert!(!view.has_parent);
        assert!(
            factory
                .gate
                .reached_evidence_parent(frame.context(), &scope, &reference)
                .unwrap()
                .is_none()
        );
        drop(reference);
        assert_eq!(control.usage().bytes, before.bytes);
        assert_eq!(
            control.usage().count(ControlKind::Frame),
            before.count(ControlKind::Frame)
        );

        let reference = factory
            .gate
            .inspect_reached_evidence(frame.context(), &scope)
            .unwrap();
        let (other, other_permit, _) = admit_native_frame(&factory, "evidence-other").await;
        let other_scope = other
            .allocation_scope(AllocationReason::RuntimeValue)
            .unwrap();
        let other_reference = factory
            .gate
            .inspect_reached_evidence(other.context(), &other_scope)
            .unwrap();
        assert_eq!(
            factory
                .gate
                .reached_evidence_view(other.context(), &other_scope, &other_reference)
                .unwrap()
                .entry
                .grant(),
            other_permit.id()
        );
        drop(other_reference);
        assert!(
            factory
                .gate
                .reached_evidence_view(other.context(), &other_scope, &reference)
                .is_err()
        );
        let expired = frame.context().clone().cap_deadline(Instant::now());
        assert!(
            factory
                .gate
                .reached_evidence_view(&expired, &scope, &reference)
                .is_err()
        );
        let record_weak = core_frame(&frame).state.invocation.downgrade();
        let retained_bytes = core_frame(&frame).state.retained_bytes;
        let frame_weak = Arc::downgrade(&frame);
        finish_native_frame(&factory, &frame, &permit)
            .await
            .unwrap();
        let charged = control.usage();
        assert!(
            factory
                .gate
                .inspect_reached_evidence(frame.context(), &scope)
                .is_err()
        );
        assert!(
            factory
                .gate
                .reached_evidence_view(frame.context(), &scope, &reference)
                .is_err()
        );
        assert_eq!(control.usage().bytes, charged.bytes);
        drop(scope);
        // The opaque reference keeps history, not the real frame/retirement.
        let control = control.clone();
        drop(frame);
        assert!(frame_weak.upgrade().is_none());
        assert!(!record_weak.upgrade().unwrap().state.lock().unwrap().live);
        drop(reference);
        assert!(record_weak.upgrade().is_none());
        let charged = control.usage();
        drop(record_weak);
        assert_eq!(control.usage().bytes, charged.bytes - retained_bytes);
        assert_eq!(
            control.usage().count(ControlKind::Frame),
            charged.count(ControlKind::Frame) - 1
        );
        finish_native_frame(&factory, &other, &other_permit)
            .await
            .unwrap();
        drop(other_scope);
        drop(other);
        factory.close();
        session.close().await.unwrap();
    }

    #[tokio::test]
    async fn actual_entry_reference_rejects_each_epoch_revocation_and_factory_closure() {
        // Fresh actual invocations isolate the two causes: factory closure
        // must reject while the original epoch/context and node are still live.
        for close_factory in [false, true] {
            let fixture = crate::test_support::RuntimeFixture::new(&["fixture"], false).await;
            let session = fixture.session().await;
            let factory = new_factory(&session);
            let (frame, permit, _) = admit_native_frame(&factory, "evidence-revoked").await;
            let scope = frame
                .allocation_scope(AllocationReason::RuntimeValue)
                .unwrap();
            let reference = factory
                .gate
                .inspect_reached_evidence(frame.context(), &scope)
                .unwrap();
            if close_factory {
                factory.gate.check_context(frame.context()).unwrap();
                assert!(core_frame(&frame).state.state.lock().unwrap().live);
                factory.close();
            } else {
                factory
                    .gate
                    .revoke_epoch(frame.context().profile())
                    .unwrap();
            }
            assert!(
                factory
                    .gate
                    .reached_evidence_view(frame.context(), &scope, &reference)
                    .is_err()
            );
            assert!(
                factory
                    .gate
                    .reached_evidence_parent(frame.context(), &scope, &reference)
                    .is_err()
            );
            assert!(
                factory
                    .gate
                    .inspect_reached_evidence(frame.context(), &scope)
                    .is_err()
            );
            finish_native_frame(&factory, &frame, &permit)
                .await
                .unwrap();
            factory.close();
            drop(reference);
            drop(scope);
            drop(frame);
            session.close().await.unwrap();
        }
    }
}
