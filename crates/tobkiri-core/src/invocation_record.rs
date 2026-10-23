//! Invocation metadata/lifecycle storage, separate from native task ownership.
//! A record reference never owns a NodeExecutionGuard or prolongs execution.
use super::{
    ActiveFrameMetadata, CallCx, CodeLocation, CompiledCallWitness, ControlHandle,
    CoreProviderAuthority, Digest, FactoryAuth, FlowNodeKind, NativeInputForwarding, NodeLifecycle,
    PreparedReachedState, ReachedOrigin, RunTaskSpawner,
};
use crate::funded_arc::{FundedArc, Funding};
use std::{
    ops::Deref,
    sync::{Arc, Mutex},
};
use tobkiri_gate::GateError;

/// These fields contain only existing entry/context/frozen-code metadata and
/// the ONE original lifecycle mutex. Native task/operation owners stay out.
pub(super) struct InvocationRecord {
    pub(super) auth: ControlHandle<FactoryAuth>,
    pub(super) context: CallCx,
    pub(super) entry_context: CallCx,
    pub(super) entry_evidence: Option<ControlHandle<tobkiri_gate::ExecutionEntryEvidence>>,
    pub(super) flow_key: String,
    pub(super) code: Option<CodeLocation>,
    pub(super) step_address: Option<usize>,
    pub(super) overlay_base_address: Option<usize>,
    pub(super) kind: Option<FlowNodeKind>,
    pub(super) witness: Option<ControlHandle<CompiledCallWitness>>,
    pub(super) overlay_index: Option<usize>,
    pub(super) activation: u64,
    pub(super) frame: Digest,
    pub(super) path: Digest,
    pub(super) metadata: ActiveFrameMetadata,
    pub(super) origin: ReachedOrigin,
    pub(super) state: Mutex<NodeLifecycle>,
}

pub(super) struct ReachedState {
    // Native ownership is destroyed with the actual State, before its record
    // reference. Later weak record references cannot retain these owners.
    pub(super) provider: ControlHandle<CoreProviderAuthority>,
    pub(super) task_spawner: Option<Arc<dyn RunTaskSpawner>>,
    pub(super) provider_operation: Option<tobkiri_wasm::ProviderOperationScope>,
    pub(super) input_forwarding: Option<ControlHandle<NativeInputForwarding>>,
    pub(super) invocation: FundedArc<InvocationRecord>,
    pub(super) retained_bytes: u64,
}

impl Deref for ReachedState {
    type Target = InvocationRecord;
    fn deref(&self) -> &Self::Target {
        self.invocation.get()
    }
}

/// Fixed bundle: the erased State block, original Funding block, and bounded
/// native Arc record allocation including its header/alignment. Owned dynamic
/// metadata is added by the existing caller before the same reservation.
pub(super) fn reached_state_storage_bytes() -> Result<usize, GateError> {
    let record = crate::ui_operation_delivery::arc_owned_bytes::<InvocationRecord>()?;
    ControlHandle::<ReachedState>::storage_bytes::<Funding>()
        .checked_add(Funding::storage_bytes())
        .and_then(|bytes| bytes.checked_add(record))
        .ok_or(GateError::Unavailable)
}

/// Funding is constructed BEFORE metadata copies. The State and strong/weak
/// record carriers retain that same original bundle; no copied release plan
/// can refund it before the final physical control block is gone.
pub(super) fn new_reached_state(
    prepared: PreparedReachedState,
    funding: Funding,
) -> Result<ControlHandle<ReachedState>, GateError> {
    let invocation = FundedArc::new(
        InvocationRecord {
            auth: prepared.auth,
            context: prepared.context,
            entry_context: prepared.entry_context,
            entry_evidence: prepared.entry_evidence,
            flow_key: prepared.flow_key,
            code: prepared.code,
            step_address: prepared.step_address,
            overlay_base_address: prepared.overlay_base_address,
            kind: prepared.kind,
            witness: prepared.witness,
            overlay_index: prepared.overlay_index,
            activation: prepared.activation,
            frame: prepared.frame,
            path: prepared.path,
            metadata: prepared.metadata,
            origin: prepared.origin,
            state: prepared.state,
        },
        funding.clone(),
    );
    let reached = ReachedState {
        provider: prepared.provider,
        task_spawner: prepared.task_spawner,
        provider_operation: prepared.provider_operation,
        input_forwarding: prepared.input_forwarding,
        invocation,
        retained_bytes: prepared.retained_bytes,
    };
    ControlHandle::try_new_precharged(reached, funding).map_err(|_| GateError::Unavailable)
}

#[cfg(test)]
mod tests {
    use super::super::control_admission_tests::{
        admit_native_frame, core_frame, finish_native_frame, new_factory,
    };
    use super::*;
    use tobkiri_gate::{ControlKind, Outcome};
    use tobkiri_wasm::NativeIdAttempt;

    #[tokio::test]
    async fn retained_invocation_record_does_not_keep_original_execution_live() {
        let fixture = crate::test_support::RuntimeFixture::new(&["fixture"], false).await;
        let session = fixture.session().await;
        let factory = new_factory(&session);
        let (frame, permit, _) = admit_native_frame(&factory, "record-original-retirement").await;
        let record = core_frame(&frame).state.invocation.clone();
        let weak = record.downgrade();
        let frame_weak = Arc::downgrade(&frame);
        assert!(record.state.lock().unwrap().live);
        assert_eq!(record.entry_evidence.as_ref().unwrap().grant(), permit.id());

        // Drop the actual frame and its original NodeExecutionGuard, before
        // any ACK. Holding metadata must not replace that execution owner.
        drop(frame);
        assert!(frame_weak.upgrade().is_none());
        drop(frame_weak);
        let upgraded = weak.upgrade().unwrap();
        assert!(FundedArc::ptr_eq(&record, &upgraded));
        {
            let lifecycle = upgraded.state.lock().unwrap();
            assert!(!lifecycle.live);
            assert!(!lifecycle.acknowledged);
        }
        factory
            .gate
            .record_outcome(
                permit.id().clone(),
                Outcome::Failed {
                    code: "unavailable".into(),
                },
            )
            .await
            .unwrap();
        drop(upgraded);
        drop(record);
        assert!(weak.upgrade().is_none());
        drop(weak);
        factory.close();
        session.close().await.unwrap();
    }

    #[tokio::test]
    async fn retired_actual_record_weak_block_keeps_its_original_bundle_funded() {
        let fixture = crate::test_support::RuntimeFixture::new(&["fixture"], false).await;
        let session = fixture.session().await;
        let factory = new_factory(&session);
        let (frame, permit, _) = admit_native_frame(&factory, "record-weak-original-funding").await;
        let record = core_frame(&frame).state.invocation.clone();
        let retained_bytes = core_frame(&frame).state.retained_bytes;
        let weak = record.downgrade();
        let other_weak = weak.clone();
        let control = record.context.control().clone();
        finish_native_frame(&factory, &frame, &permit)
            .await
            .unwrap();
        {
            let lifecycle = record.state.lock().unwrap();
            assert!(!lifecycle.live && lifecycle.acknowledged);
        }
        assert!(matches!(
            frame.try_native_execution_id(),
            NativeIdAttempt::Uncertain(_)
        ));
        drop(frame);
        // An upgraded metadata reference observes the same retired mutex.
        assert!(!weak.upgrade().unwrap().state.lock().unwrap().live);
        drop(record);
        assert_eq!(weak.strong_count(), 0);
        assert!(weak.upgrade().is_none());
        let charged = control.usage();
        drop(weak);
        assert_eq!(control.usage().bytes, charged.bytes);
        assert_eq!(
            control.usage().count(ControlKind::Frame),
            charged.count(ControlKind::Frame)
        );
        drop(other_weak);
        assert_eq!(control.usage().bytes, charged.bytes - retained_bytes);
        assert_eq!(
            control.usage().count(ControlKind::Frame),
            charged.count(ControlKind::Frame) - 1
        );
        factory.close();
        session.close().await.unwrap();
    }
}
