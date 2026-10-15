//! Core-private execution provenance. Preparation borrows actual frozen state;
//! it cannot be reconstructed from a serialized owner/node descriptor.
//!
//! A successful first reservation precedes every owned execution carrier.
//! Administrative construction below retains existing handles only. It creates
//! no node, active registration, runtime-value budget or guest control pool.

use crate::CoreError;
use std::{
    collections::{BTreeMap, BTreeSet},
    ptr,
    sync::{
        Arc, Mutex, Weak,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    time::Instant,
};
use tobkiri_canon::{
    AllocationBudget, ControlHandle, CoreValue, Digest,
    ids::{PackHash, PackId, ShapeRef},
};
use tobkiri_flow::{
    ActiveCodeFrame, FlowNodeKind, FlowNodePosition,
    FlowUserInvocationPreparation as FlowUserPreparation, FrameContinuation, GuestFlowFrame,
    OverlayPosition, OverlayTransition, PortInvoker, ReachedNode, StepError,
    UiInvocationPreparation as FlowUiPreparation,
};
use tobkiri_gate::{
    AcknowledgedTerminal, ActiveFrameMetadata, ActiveFrameVerifier, AllocationFrameScope,
    AllocationReason, CallCx, ChargeKind, CompiledActivation, CompiledCallAuthority,
    CompiledCallDescriptor, CompiledCodeOwner, CompiledNodeKind, ControlKind, ControlReservation,
    CoreControl, DispatchPermit, EffectRequest, FlowEndpointRegistration, FrameDispatch,
    FrameVerifierRegistration, FrozenFlowTarget, Gate, GateEngine, GateError, IngressRoute,
    PreparedCodeOwner, PreparedFrameEntry, PreparedFrameMetadata, PreparedFrameVerifier, Principal,
    SourceReadMetadata, SourceReadReceipt,
};
use tobkiri_wasm::{
    HandleSourceRequest, NativeInputPreparation, ProviderFrame, ProviderOrigin, ProviderRequest,
    UiInvocation,
};

/// Shared authentication state has no Gate, invoker or active frame edge.
/// Gate/PackHost may retain projections of this allocation without forming a
/// Gate/host -> verifier/bridge -> frame/host ownership cycle.
struct FactoryAuth {
    identity: Digest,
    /// The canonical Core-owned profile catalog. Its bounded load lifetime is
    /// managed by Session; this handle shares the catalog and never clones AST.
    /// The wrapper itself is retained under the bounded native Core account so
    /// Flow/witness projections keep the exact canonical Arc alive.
    compiled: ControlHandle<Arc<CompiledProfile>>,
    closing: AtomicBool,
    activation: AtomicU64,
    /// Global lock order for this factory is admission -> one node state ->
    /// Gate/ledger. Closing, retirement, allocation and claims all serialize
    /// through this lock so a stale scope cannot race a successful reserve.
    admission: Mutex<()>,
    /// Weak identity indexes only; caller-owned preparations and frames retain
    /// their own Arcs. This prevents auth -> state -> provider -> auth cycles.
    native_preparations: Mutex<BTreeMap<usize, Weak<CoreNativeInputPreparation>>>,
    ui_preparations: Mutex<BTreeMap<usize, Weak<CoreUiInvocationPreparation>>>,
    user_preparations: Mutex<BTreeMap<usize, Weak<CoreUserInvocationPreparation>>>,
    live_provider_frames: Mutex<BTreeMap<usize, Weak<CoreProviderFrame>>>,
}
impl FactoryAuth {
    /// Publish only while this factory and any still-required caller are live.
    /// The caller retains the strong payload outside this function, so every
    /// failure drops retirement guards AFTER admission/registry locks end.
    fn publish_weak<T>(
        &self,
        registry: &Mutex<BTreeMap<usize, Weak<T>>>,
        value: &Arc<T>,
        maximum: usize,
        caller: Option<&ReachedState>,
    ) -> Result<(), GateError> {
        let _admission = self.admission.lock().map_err(|_| GateError::Unavailable)?;
        if self.closing.load(Ordering::Acquire) {
            return Err(GateError::Unavailable);
        }
        if let Some(caller) = caller {
            let lifecycle = caller.state.lock().map_err(|_| GateError::Unavailable)?;
            if !lifecycle.live || lifecycle.acknowledged {
                return Err(denied("provider caller retired before entry publication"));
            }
        }
        let key = Arc::as_ptr(value) as usize;
        let mut registry = registry.lock().map_err(|_| GateError::Unavailable)?;
        if registry.len() >= maximum || registry.contains_key(&key) {
            return Err(GateError::Unavailable);
        }
        registry.insert(key, Arc::downgrade(value));
        Ok(())
    }
}
/// Only the interpreter's execution guard keeps a node active. Allocation proof
/// and retained-value owners keep its physical storage, never its liveness.
struct NodeLifecycle {
    live: bool,
    claimed: bool,
    expected_args: Option<Digest>,
    expected_target: Option<usize>,
    provided_flow_key: Option<Digest>,
    source_receipt_claimed: bool,
    compiled_prepared: bool,
    output_digest: Option<Digest>,
    acknowledged: bool,
    stream_ready: bool,
    ack_evidence: Option<ControlHandle<AcknowledgedEvidence>>,
    /// Covers the exact copied target retained by Gate's temporary compiled
    /// activation through final dispatch. The actual target is still checked
    /// against the frozen witness and caller request.
    target_control: Option<ControlReservation>,
    /// Bounds a digest retained while a preclaim entry crosses the final
    /// physical admission boundary.
    args_control: Option<ControlReservation>,
}
struct ReachedState {
    auth: ControlHandle<FactoryAuth>,
    provider: ControlHandle<CoreProviderAuthority>,
    context: CallCx,
    entry_context: CallCx,
    entry_evidence: Option<ControlHandle<tobkiri_gate::ExecutionEntryEvidence>>,
    /// Exact key into the immutable canonical catalog. Retaining this bounded
    /// key never copies or independently owns compiler AST storage.
    flow_key: String,
    code: Option<CodeLocation>,
    step_address: Option<usize>,
    /// The base Flow step whose ordered overlay selection created this code
    /// location. Overlay IDs alone are not unique across transition sites.
    overlay_base_address: Option<usize>,
    kind: Option<FlowNodeKind>,
    witness: Option<ControlHandle<CompiledCallWitness>>,
    overlay_index: Option<usize>,
    activation: u64,
    frame: Digest,
    path: Digest,
    metadata: ActiveFrameMetadata,
    origin: ReachedOrigin,
    state: Mutex<NodeLifecycle>,
    // Full prepaid control inventory, not merely sizeof an erased handle.
    retained_bytes: u64,
}
enum ReachedOrigin {
    Flow,
    NativeArguments(NativeInputStateData),
    UiArguments(UiInvocationStateData),
    UserArguments(UserInvocationStateData),
    Provider(ProviderStateData),
    HandleSource(HandleSourceStateData),
}
struct NativeInputStateData {
    index: usize,
    invocation: Digest,
    source: tobkiri_canon::ids::InputSourceId,
    view: ShapeRef,
    op: tobkiri_canon::ids::OpName,
    target: tobkiri_gate::FrozenPortTarget,
}
struct ProviderStateData {
    view: ShapeRef,
    op: tobkiri_canon::ids::OpName,
    target: tobkiri_gate::FrozenPortTarget,
    grant: tobkiri_gate::GrantId,
    args: Digest,
    native: Option<NativeInputStateData>,
    ui: Option<UiInvocationStateData>,
    user: Option<UserInvocationStateData>,
}
struct UiInvocationStateData {
    invocation: Digest,
    session: tobkiri_canon::ids::UiSessionId,
    generation: u64,
    receiver: Digest,
    view: ShapeRef,
    op: tobkiri_canon::ids::OpName,
    target: tobkiri_gate::FrozenPortTarget,
}
struct UserInvocationStateData {
    invocation: Digest,
    document: Digest,
    revision: Digest,
    epoch: u64,
    view: ShapeRef,
    op: tobkiri_canon::ids::OpName,
    target: tobkiri_gate::FrozenPortTarget,
}
struct HandleSourceStateData {
    kind: tobkiri_wasm::HandleKind,
    local_id: u64,
    authority: ShapeRef,
    generation: u64,
    parent_frame: Digest,
    parent_path: Digest,
}
struct AllocationProof {
    node: ControlHandle<ReachedState>,
    metadata: ActiveFrameMetadata,
}
struct ReachedActivationProof {
    node: ControlHandle<ReachedState>,
}
struct CompletedReadProof {
    allocation: ControlHandle<AllocationProof>,
    original_input: CallCx,
    source: SourceReadMetadata,
    acknowledged: ControlHandle<AcknowledgedEvidence>,
}
struct AcknowledgedEvidence {
    grant: tobkiri_gate::GrantId,
    succeeded: bool,
}
struct NativeInputFrame {
    state: ControlHandle<ReachedState>,
    _retirement: ControlHandle<NodeExecutionGuard>,
    used: AtomicBool,
    _reservation: ControlReservation,
}
struct CoreNativeInputPreparation {
    frame: ControlHandle<NativeInputFrame>,
    _reservation: ControlReservation,
}
struct CoreUiInvocationPreparation {
    state: ControlHandle<ReachedState>,
    _retirement: ControlHandle<NodeExecutionGuard>,
    used: AtomicBool,
    _reservation: ControlReservation,
}
struct CoreUserInvocationPreparation {
    state: ControlHandle<ReachedState>,
    _retirement: ControlHandle<NodeExecutionGuard>,
    used: AtomicBool,
    _reservation: ControlReservation,
}
/// Exact frozen target and one-shot UI argument proof returned only after the
/// Session has checked its registered launcher attachment. The stored native
/// reservation follows the wrapper and its preparation through final dispatch.
pub(crate) struct PreparedUiInvocation {
    invocation: Digest,
    target: FrozenTarget,
    preparation: Arc<CoreUiInvocationPreparation>,
    _reservation: ControlReservation,
}
impl PreparedUiInvocation {
    pub(crate) fn invocation(&self) -> &Digest {
        &self.invocation
    }

    pub(crate) fn target(&self) -> &FrozenTarget {
        &self.target
    }

    pub(crate) fn preparation(&self) -> Arc<dyn FlowUiPreparation> {
        self.preparation.clone()
    }
}
/// The exact User document identity and frozen endpoint are prepared before
/// borrowed CLI arguments are materialized under the User's own policy cap.
/// This proof is distinct from UI, NativeInput, and Flow AST entry evidence.
pub(crate) struct PreparedUserInvocation {
    invocation: Digest,
    target: FrozenTarget,
    preparation: Arc<CoreUserInvocationPreparation>,
    _reservation: ControlReservation,
}
impl PreparedUserInvocation {
    pub(crate) fn invocation(&self) -> &Digest {
        &self.invocation
    }

    pub(crate) fn target(&self) -> &FrozenTarget {
        &self.target
    }

    pub(crate) fn preparation(&self) -> Arc<dyn FlowUserPreparation> {
        self.preparation.clone()
    }
}
/// A one-shot, non-authoritative selection of one exact canonical Flow for a
/// live guest provider call. The selected target metadata is native-control
/// funded; only a separately consumed FlowRun terminal can turn it into a
/// callee frame.
pub(crate) struct PreparedGuestFlow<'a> {
    factory: &'a ExecutionFactory,
    caller: &'a CallCx,
    provider: &'a dyn GuestFlowFrame,
    provider_frame: &'a CoreProviderFrame,
    requested_shape: &'a ShapeRef,
    arguments_digest: &'a Digest,
    flow_key: &'a str,
    flow: &'a ResolvedFlow,
    target: FrozenFlowTarget,
    used: AtomicBool,
    _reservation: ControlReservation,
}
impl PreparedGuestFlow<'_> {
    pub(crate) fn flow_key(&self) -> &str {
        self.flow_key
    }

    pub(crate) fn target(&self) -> &FrozenFlowTarget {
        &self.target
    }

    pub(crate) fn requested_shape(&self) -> &ShapeRef {
        self.requested_shape
    }

    pub(crate) fn arguments_digest(&self) -> &Digest {
        self.arguments_digest
    }
}
struct CoreProviderFrame {
    state: ControlHandle<ReachedState>,
    _retirement: ControlHandle<NodeExecutionGuard>,
    _reservation: ControlReservation,
}
impl GuestFlowFrame for CoreProviderFrame {
    fn as_any(&self) -> &dyn std::any::Any {
        self
    }
}
struct PreparedNativeEntry<'a> {
    provider: &'a CoreProviderAuthority,
    caller: &'a CallCx,
    entry: &'a CompiledInputEntry,
    target: &'a tobkiri_gate::FrozenPortTarget,
    source: &'a tobkiri_canon::ids::InputSourceId,
    index: usize,
    invocation: Digest,
    frame: Digest,
    node: Digest,
    activation: u64,
    path: Digest,
    state: Mutex<PreparationState>,
}
struct PreparedUiEntry<'a, 'entry> {
    provider: &'a CoreProviderAuthority,
    caller: &'a CallCx,
    entry: &'a UiInvocation<'entry>,
    target: &'a tobkiri_gate::FrozenPortTarget,
    frame: &'a Digest,
    node: &'a Digest,
    activation: u64,
    path: &'a Digest,
    state: Mutex<PreparationState>,
}
struct PreparedUserEntry<'a, 'entry> {
    provider: &'a CoreProviderAuthority,
    caller: &'a CallCx,
    invocation: &'a tobkiri_flow::FlowUserInvocation<'entry>,
    document: &'a Digest,
    revision: &'a Digest,
    epoch: u64,
    target: &'a tobkiri_gate::FrozenPortTarget,
    frame: &'a Digest,
    node: &'a Digest,
    activation: u64,
    path: &'a Digest,
    state: Mutex<PreparationState>,
}
struct ParentFlowCall<'a> {
    frame_identity: Digest,
    node: &'a ReachedNode,
    flow_key: &'a str,
    args: &'a Digest,
    target: &'a FrozenFlowTarget,
}
#[derive(Clone, Copy)]
struct GuestFlowCall<'a> {
    provider_entry: &'a dyn GuestFlowFrame,
    provider_frame: &'a CoreProviderFrame,
    requested_shape: &'a ShapeRef,
    target: &'a FrozenFlowTarget,
}
#[derive(Clone, Copy)]
struct NativeFlowCall<'a> {
    routing_permit: &'a DispatchPermit,
    receipt: &'a SourceReadReceipt,
    route: &'a IngressRoute,
    target: &'a FrozenFlowTarget,
}
/// This guard is held ONLY by RunCx/operation/continuation clones, not by Gate
/// scope proofs. Final guard drop synchronizes retirement with new work.
struct NodeExecutionGuard(ControlHandle<ReachedState>);
impl Drop for NodeExecutionGuard {
    fn drop(&mut self) {
        let auth = &self.0.auth;
        let Ok(_admission) = auth.admission.lock() else {
            // Poisoning cannot reopen admission: every verifier treats it as
            // denial and Gate work requires this lock to succeed.
            return;
        };
        if let Ok(mut state) = self.0.state.lock() {
            state.live = false;
        }
    }
}
struct CoreVerifier {
    auth: ControlHandle<FactoryAuth>,
}
struct FactoryRegistrations {
    // Frame callbacks may verify Flow endpoints and enter compiled calls.
    // Keep their original RAII tokens behind separate locks so those exact
    // nested checks never reacquire an already-held registration mutex.
    frame: Mutex<Option<FrameVerifierRegistration>>,
    calls: Mutex<Option<CompiledCallAuthority>>,
    flow_endpoints: Mutex<Option<FlowEndpointRegistration>>,
}
/// Runtime shared by Flow frames and the bridge adapter. It intentionally has
/// no PortInvoker edge, so a PackHost may retain the bridge without a cycle.
/// Its fixed metadata uses the native bounded Core account, never guest funds.
pub(crate) struct CoreProviderAuthority {
    auth: ControlHandle<FactoryAuth>,
    gate: Arc<GateEngine>,
    control: CoreControl,
    registrations: FactoryRegistrations,
}
impl CoreProviderAuthority {
    pub(crate) fn close(&self) {
        let (native, ui, user, providers) = if let Ok(_admission) = self.auth.admission.lock() {
            self.auth.closing.store(true, Ordering::Release);
            let native = self
                .auth
                .native_preparations
                .lock()
                .map(|mut entries| std::mem::take(&mut *entries))
                .unwrap_or_default();
            let providers = self
                .auth
                .live_provider_frames
                .lock()
                .map(|mut entries| std::mem::take(&mut *entries))
                .unwrap_or_default();
            let ui = self
                .auth
                .ui_preparations
                .lock()
                .map(|mut entries| std::mem::take(&mut *entries))
                .unwrap_or_default();
            let user = self
                .auth
                .user_preparations
                .lock()
                .map(|mut entries| std::mem::take(&mut *entries))
                .unwrap_or_default();
            (native, ui, user, providers)
        } else {
            self.auth.closing.store(true, Ordering::Release);
            (
                BTreeMap::new(),
                BTreeMap::new(),
                BTreeMap::new(),
                BTreeMap::new(),
            )
        };
        // Execution guards also take admission while retiring, so release all
        // registry-held carriers only after the admission lock is dropped.
        drop(native);
        drop(ui);
        drop(user);
        drop(providers);
        let frame = self
            .registrations
            .frame
            .lock()
            .ok()
            .and_then(|mut registration| registration.take());
        let calls = self
            .registrations
            .calls
            .lock()
            .ok()
            .and_then(|mut registration| registration.take());
        let flow_endpoints = self
            .registrations
            .flow_endpoints
            .lock()
            .ok()
            .and_then(|mut registration| registration.take());
        // Token destructors can reenter admission. All registration and
        // admission guards have ended before any original RAII token drops.
        drop(frame);
        drop(calls);
        drop(flow_endpoints);
    }
    fn with_registration<T>(
        &self,
        use_registration: impl FnOnce(&FrameVerifierRegistration) -> Result<T, GateError>,
    ) -> Result<T, GateError> {
        if self.auth.closing.load(Ordering::Acquire) {
            return Err(GateError::Unavailable);
        }
        let registrations = self
            .registrations
            .frame
            .lock()
            .map_err(|_| GateError::Unavailable)?;
        let registration = registrations.as_ref().ok_or(GateError::Unavailable)?;
        use_registration(registration)
    }
    fn with_compiled_registration<T>(
        &self,
        use_registration: impl FnOnce(&CompiledCallAuthority) -> Result<T, GateError>,
    ) -> Result<T, GateError> {
        if self.auth.closing.load(Ordering::Acquire) {
            return Err(GateError::Unavailable);
        }
        let registrations = self
            .registrations
            .calls
            .lock()
            .map_err(|_| GateError::Unavailable)?;
        let registration = registrations.as_ref().ok_or(GateError::Unavailable)?;
        use_registration(registration)
    }
    fn with_flow_registration<T>(
        &self,
        use_registration: impl FnOnce(&FlowEndpointRegistration) -> Result<T, GateError>,
    ) -> Result<T, GateError> {
        if self.auth.closing.load(Ordering::Acquire) {
            return Err(GateError::Unavailable);
        }
        let registrations = self
            .registrations
            .flow_endpoints
            .lock()
            .map_err(|_| GateError::Unavailable)?;
        let registration = registrations.as_ref().ok_or(GateError::Unavailable)?;
        use_registration(registration)
    }

    pub(crate) fn prepare_native_input_with_handle(
        &self,
        provider_handle: ControlHandle<CoreProviderAuthority>,
        _control: &dyn PortInvoker,
        caller: &CallCx,
        entry: &CompiledInputEntry,
        target: &tobkiri_gate::FrozenPortTarget,
    ) -> Result<Arc<dyn NativeInputPreparation>, StepError> {
        let compiled = self.auth.compiled.get();
        self.gate.check_context(caller).map_err(step)?;
        if self.auth.closing.load(Ordering::Acquire)
            || caller.revision() != Some(&compiled.plan_digest)
            || !compiled
                .input_entries()
                .iter()
                .any(|candidate| ptr::eq(candidate, entry))
            || entry.profile_revision() != &compiled.plan_digest
            || target.pack != *entry.source_pack()
            || target.hash != *entry.source_hash()
        {
            return Err(step(denied(
                "native Input is not the exact frozen source selection",
            )));
        }
        let source = match caller.principal() {
            tobkiri_gate::Principal::Input { source } => source,
            _ => {
                return Err(step(denied(
                    "native Input requires the original Input principal",
                )));
            }
        };
        // This verifies the current exact source binding and current owner cap;
        // the returned number is inspection only and never funds CoreValue.
        self.gate
            .input_capacity_limit(caller, entry.exact_input_view(), entry.operation(), target)
            .map_err(step)?;
        let index = compiled
            .input_entries()
            .iter()
            .position(|candidate| ptr::eq(candidate, entry))
            .ok_or_else(|| step(denied("native Input source disappeared from its catalog")))?;
        let activation = next_sequence(&self.auth.activation).map_err(step)?;
        let invocation = identity(&[
            b"tobkiri.core-native-input-invocation/1\0",
            self.auth.identity.as_str().as_bytes(),
            caller.request().as_str().as_bytes(),
            source.as_str().as_bytes(),
            &index.to_le_bytes(),
            &activation.to_le_bytes(),
            shape_identity(entry.exact_input_view())
                .map_err(step)?
                .as_str()
                .as_bytes(),
            entry.operation().as_str().as_bytes(),
            target.pack.as_str().as_bytes(),
            target.hash.as_str().as_bytes(),
            shape_identity(&target.provided)
                .map_err(step)?
                .as_str()
                .as_bytes(),
        ])
        .map_err(step)?;
        let frame = identity(&[
            b"tobkiri.core-native-input-frame/1\0",
            self.auth.identity.as_str().as_bytes(),
            invocation.as_str().as_bytes(),
        ])
        .map_err(step)?;
        let node = identity(&[
            b"tobkiri.core-native-input-node/1\0",
            invocation.as_str().as_bytes(),
        ])
        .map_err(step)?;
        let path = identity(&[
            b"tobkiri.core-native-input-path/1\0",
            frame.as_str().as_bytes(),
            node.as_str().as_bytes(),
        ])
        .map_err(step)?;
        let prepared = PreparedNativeEntry {
            provider: self,
            caller,
            entry,
            target,
            source,
            index,
            invocation,
            frame,
            node,
            activation,
            path,
            state: Mutex::new(PreparationState::Fresh),
        };
        let mut created = None;
        self.gate
            .with_frame_preparation(
                caller,
                &prepared,
                AllocationReason::RuntimeValue,
                &mut |funding| {
                    if funding.limit == 0 {
                        return Err(GateError::Unavailable);
                    }
                    let control = caller.control();
                    let native_bytes = native_input_data_storage_bytes(entry, source, target)
                        .ok_or(GateError::Unavailable)?;
                    let metadata_bytes = funding
                        .metadata
                        .caller
                        .profile()
                        .clone_storage_bytes()
                        .checked_add(
                            funding
                                .metadata
                                .caller
                                .principal()
                                .clone_storage_bytes()
                                .map_err(|_| GateError::Unavailable)?,
                        )
                        .and_then(|bytes| {
                            bytes.checked_add(
                                funding.metadata.caller.request().clone_storage_bytes(),
                            )
                        })
                        .and_then(|bytes| {
                            bytes.checked_add(code_owner_pack_storage_bytes(
                                entry.source_pack(),
                                entry.source_hash(),
                            ))
                        })
                        .ok_or(GateError::Unavailable)?;
                    let state_bytes =
                        ControlHandle::<ReachedState>::storage_bytes::<ControlReservation>()
                            .checked_add(native_bytes)
                            .and_then(|bytes| bytes.checked_add(metadata_bytes))
                            .and_then(|bytes| u64::try_from(bytes).ok())
                            .ok_or(GateError::Unavailable)?;
                    let state_reservation = control.reserve(ControlKind::Frame, state_bytes)?;
                    let metadata = ActiveFrameMetadata {
                        profile: caller
                            .profile()
                            .try_clone_precharged()
                            .map_err(|_| GateError::Unavailable)?,
                        revision: compiled.plan_digest.clone(),
                        security_epoch: caller.security_epoch(),
                        principal: caller
                            .principal()
                            .try_clone_precharged()
                            .map_err(|_| GateError::Unavailable)?,
                        request: caller
                            .request()
                            .try_clone_precharged()
                            .map_err(|_| GateError::Unavailable)?,
                        frame: prepared.frame.clone(),
                        node: prepared.node.clone(),
                        activation: prepared.activation,
                        owner: CompiledCodeOwner::Pack {
                            pack: entry
                                .source_pack()
                                .try_clone_precharged()
                                .map_err(|_| GateError::Unavailable)?,
                            hash: entry.source_hash().clone(),
                        },
                        reason: AllocationReason::RuntimeValue,
                        path: prepared.path.clone(),
                        transitive_memory_bytes: None,
                    };
                    let data = native_input_data_precharged(
                        entry,
                        source,
                        target,
                        prepared.index,
                        &prepared.invocation,
                    )?;
                    let state = ControlHandle::try_new_precharged(
                        ReachedState {
                            auth: self.auth.clone(),
                            provider: provider_handle.clone(),
                            context: caller
                                .try_clone_precharged()
                                .map_err(|_| GateError::Unavailable)?,
                            entry_context: caller
                                .try_clone_precharged()
                                .map_err(|_| GateError::Unavailable)?,
                            entry_evidence: None,
                            flow_key: String::new(),
                            code: None,
                            step_address: None,
                            overlay_base_address: None,
                            kind: None,
                            witness: None,
                            overlay_index: None,
                            activation: prepared.activation,
                            frame: prepared.frame.clone(),
                            path: prepared.path.clone(),
                            metadata,
                            origin: ReachedOrigin::NativeArguments(data),
                            state: Mutex::new(NodeLifecycle {
                                live: true,
                                claimed: false,
                                expected_args: None,
                                expected_target: None,
                                provided_flow_key: None,
                                source_receipt_claimed: false,
                                compiled_prepared: false,
                                output_digest: None,
                                acknowledged: false,
                                stream_ready: false,
                                ack_evidence: None,
                                target_control: None,
                                args_control: None,
                            }),
                            retained_bytes: state_bytes,
                        },
                        state_reservation,
                    )
                    .map_err(|_| GateError::Unavailable)?;
                    created = Some(state);
                    Ok(())
                },
            )
            .map_err(step)?;
        let state = created.ok_or_else(|| step(GateError::Unavailable))?;
        // PreparedNativeEntry's callback holds admission. Construct guards
        // only after it returns: allocation failure must be able to retire.
        let control = caller.control();
        let retirement = funded_handle(
            control,
            ControlKind::Frame,
            0,
            NodeExecutionGuard(state.clone()),
        )
        .map_err(step)?;
        let frame = funded_handle(
            control,
            ControlKind::Frame,
            0,
            NativeInputFrame {
                state,
                _retirement: retirement,
                used: AtomicBool::new(false),
                _reservation: control
                    .reserve(
                        ControlKind::Frame,
                        u64::try_from(
                            ControlHandle::<NativeInputFrame>::storage_bytes::<ControlReservation>(
                            )
                            .checked_add(128)
                            .ok_or_else(|| step(GateError::Unavailable))?,
                        )
                        .map_err(|_| step(GateError::Unavailable))?,
                    )
                    .map_err(|error| step(GateError::from(error)))?,
            },
        )
        .map_err(step)?;
        let reservation = self
            .control
            .reserve(
                ControlKind::Registration,
                u64::try_from(std::mem::size_of::<CoreNativeInputPreparation>() + 128)
                    .map_err(|_| step(GateError::Unavailable))?,
            )
            .map_err(|error| step(GateError::from(error)))?;
        let preparation: Arc<CoreNativeInputPreparation> = Arc::new(CoreNativeInputPreparation {
            frame: frame.clone(),
            _reservation: reservation,
        });
        self.auth
            .publish_weak(&self.auth.native_preparations, &preparation, 256, None)
            .map_err(step)?;
        let preparation: Arc<dyn NativeInputPreparation> = preparation;
        Ok(preparation)
    }

    /// Bind one authenticated Pack UI receive to its exact registered source
    /// identity before the argument graph is materialized. This preclaim phase
    /// derives funding from the actual PackUi principal; neither the selected
    /// target nor this descriptor can sponsor the UI-owned value.
    fn prepare_ui_invocation_with_handle(
        &self,
        provider_handle: ControlHandle<CoreProviderAuthority>,
        caller: &CallCx,
        entry: &UiInvocation<'_>,
    ) -> Result<Arc<CoreUiInvocationPreparation>, StepError> {
        let compiled = self.auth.compiled.get();
        self.gate.check_context(caller).map_err(step)?;
        let (pack, hash, ui) = match caller.principal() {
            Principal::PackUi { pack, hash, ui } => (pack, hash, ui),
            _ => {
                return Err(step(denied(
                    "UI invocation requires its registered PackUi principal",
                )));
            }
        };
        if self.auth.closing.load(Ordering::Acquire)
            || caller.revision() != Some(&compiled.plan_digest)
            || entry.generation == 0
            || ui != entry.session
            || compiled.profile.startup.ui.as_ref() != Some(pack)
            || entry.receiver != &crate::session::ui_receiver_identity(entry.session)
            || !compiled.bindings.contains_key(entry.view)
            || !compiled
                .profile
                .packs
                .iter()
                .any(|pin| pin.id == *pack && pin.sha256 == *hash)
        {
            return Err(step(denied(
                "UI invocation differs from its frozen registered receiver",
            )));
        }
        let target = self
            .gate
            .resolve_frozen_target(caller, entry.view, entry.op)
            .map_err(step)?;
        if !same_profile_gate_target(entry.target, &target) {
            return Err(step(denied(
                "UI invocation target differs from the unique frozen endpoint",
            )));
        }

        let activation = next_sequence(&self.auth.activation).map_err(step)?;
        let invocation = entry.invocation;
        let frame = identity(&[
            b"tobkiri.core-ui-invocation-frame/1\0",
            self.auth.identity.as_str().as_bytes(),
            invocation.as_str().as_bytes(),
            entry.session.as_str().as_bytes(),
            &entry.generation.to_le_bytes(),
            entry.receiver.as_str().as_bytes(),
            caller.request().as_str().as_bytes(),
            &activation.to_le_bytes(),
        ])
        .map_err(step)?;
        let node = identity(&[
            b"tobkiri.core-ui-invocation-node/1\0",
            frame.as_str().as_bytes(),
            shape_identity(entry.view)
                .map_err(step)?
                .as_str()
                .as_bytes(),
            entry.op.as_str().as_bytes(),
        ])
        .map_err(step)?;
        let path = identity(&[
            b"tobkiri.core-ui-invocation-path/1\0",
            frame.as_str().as_bytes(),
            target.pack.as_str().as_bytes(),
            target.hash.as_str().as_bytes(),
            shape_identity(&target.provided)
                .map_err(step)?
                .as_str()
                .as_bytes(),
        ])
        .map_err(step)?;
        let prepared = PreparedUiEntry {
            provider: self,
            caller,
            entry,
            target: &target,
            frame: &frame,
            node: &node,
            activation,
            path: &path,
            state: Mutex::new(PreparationState::Fresh),
        };
        let mut created = None;
        self.gate
            .with_frame_preparation(
                caller,
                &prepared,
                AllocationReason::RuntimeValue,
                &mut |funding| {
                    if funding.limit == 0 {
                        return Err(GateError::Unavailable);
                    }
                    let ui_bytes = ui_invocation_data_storage_bytes(entry, &target)
                        .ok_or(GateError::Unavailable)?;
                    let metadata_bytes = caller
                        .profile()
                        .clone_storage_bytes()
                        .checked_add(
                            caller
                                .principal()
                                .clone_storage_bytes()
                                .map_err(|_| GateError::Unavailable)?,
                        )
                        .and_then(|bytes| bytes.checked_add(caller.request().clone_storage_bytes()))
                        .and_then(|bytes| {
                            bytes.checked_add(code_owner_pack_storage_bytes(pack, hash))
                        })
                        .ok_or(GateError::Unavailable)?;
                    let state_bytes =
                        ControlHandle::<ReachedState>::storage_bytes::<ControlReservation>()
                            .checked_add(ui_bytes)
                            .and_then(|bytes| bytes.checked_add(metadata_bytes))
                            .and_then(|bytes| u64::try_from(bytes).ok())
                            .ok_or(GateError::Unavailable)?;
                    let state_reservation =
                        caller.control().reserve(ControlKind::Frame, state_bytes)?;
                    let metadata = ActiveFrameMetadata {
                        profile: caller
                            .profile()
                            .try_clone_precharged()
                            .map_err(|_| GateError::Unavailable)?,
                        revision: compiled.plan_digest.clone(),
                        security_epoch: caller.security_epoch(),
                        principal: caller
                            .principal()
                            .try_clone_precharged()
                            .map_err(|_| GateError::Unavailable)?,
                        request: caller
                            .request()
                            .try_clone_precharged()
                            .map_err(|_| GateError::Unavailable)?,
                        frame: frame.clone(),
                        node: node.clone(),
                        activation,
                        owner: CompiledCodeOwner::Pack {
                            pack: pack
                                .try_clone_precharged()
                                .map_err(|_| GateError::Unavailable)?,
                            hash: hash.clone(),
                        },
                        reason: AllocationReason::RuntimeValue,
                        path: path.clone(),
                        transitive_memory_bytes: None,
                    };
                    let state = ControlHandle::try_new_precharged(
                        ReachedState {
                            auth: self.auth.clone(),
                            provider: provider_handle.clone(),
                            context: caller
                                .try_clone_precharged()
                                .map_err(|_| GateError::Unavailable)?,
                            entry_context: caller
                                .try_clone_precharged()
                                .map_err(|_| GateError::Unavailable)?,
                            entry_evidence: None,
                            flow_key: String::new(),
                            code: None,
                            step_address: None,
                            overlay_base_address: None,
                            kind: None,
                            witness: None,
                            overlay_index: None,
                            activation,
                            frame: frame.clone(),
                            path: path.clone(),
                            metadata,
                            origin: ReachedOrigin::UiArguments(
                                ui_invocation_data_precharged(entry, &target)
                                    .map_err(|_| GateError::Unavailable)?,
                            ),
                            state: Mutex::new(NodeLifecycle {
                                live: true,
                                claimed: false,
                                expected_args: None,
                                expected_target: None,
                                provided_flow_key: None,
                                source_receipt_claimed: false,
                                compiled_prepared: false,
                                output_digest: None,
                                acknowledged: false,
                                stream_ready: false,
                                ack_evidence: None,
                                target_control: None,
                                args_control: None,
                            }),
                            retained_bytes: state_bytes,
                        },
                        state_reservation,
                    )
                    .map_err(|_| GateError::Unavailable)?;
                    created = Some(state);
                    Ok(())
                },
            )
            .map_err(step)?;
        let state = created.ok_or_else(|| step(GateError::Unavailable))?;
        let retirement = funded_handle(
            caller.control(),
            ControlKind::Frame,
            0,
            NodeExecutionGuard(state.clone()),
        )
        .map_err(step)?;
        // The caller retains this guard through argument materialization and
        // final admission. Failed publication drops it outside admission.
        let reservation = caller
            .control()
            .reserve(
                ControlKind::Registration,
                u64::try_from(std::mem::size_of::<CoreUiInvocationPreparation>() + 160)
                    .map_err(|_| step(GateError::Unavailable))?,
            )
            .map_err(|error| step(GateError::from(error)))?;
        let preparation = Arc::new(CoreUiInvocationPreparation {
            state,
            _retirement: retirement,
            used: AtomicBool::new(false),
            _reservation: reservation,
        });
        self.auth
            .publish_weak(&self.auth.ui_preparations, &preparation, 1024, None)
            .map_err(step)?;
        Ok(preparation)
    }

    fn prepare_user_invocation_with_handle(
        &self,
        provider_handle: ControlHandle<CoreProviderAuthority>,
        caller: &CallCx,
        entry: &tobkiri_flow::FlowUserInvocation<'_>,
        document: &Digest,
        target: &tobkiri_gate::FrozenPortTarget,
    ) -> Result<Arc<CoreUserInvocationPreparation>, StepError> {
        let compiled = self.auth.compiled.get();
        self.gate.check_context(caller).map_err(step)?;
        if self.auth.closing.load(Ordering::Acquire)
            || caller.revision() != Some(&compiled.plan_digest)
            || !matches!(caller.principal(), Principal::User)
            || !same_profile_gate_target(entry.target, target)
            || self
                .gate
                .resolve_frozen_target(caller, entry.view, entry.op)
                .map_err(step)?
                != *target
        {
            return Err(step(denied(
                "User invocation differs from its current registered context or endpoint",
            )));
        }
        // Inspection only: Gate binds the supplied document to its registered
        // User policy, exact revision/epoch, current approval and explicit cap.
        self.gate
            .code_owner_execution_limits(caller, PreparedCodeOwner::UserDocument { document })
            .map_err(step)?;
        let activation = next_sequence(&self.auth.activation).map_err(step)?;
        let revision = &compiled.plan_digest;
        let frame = identity(&[
            b"tobkiri.core-user-invocation-frame/1\0",
            self.auth.identity.as_str().as_bytes(),
            entry.invocation.as_str().as_bytes(),
            document.as_str().as_bytes(),
            revision.as_str().as_bytes(),
            &caller.security_epoch().to_le_bytes(),
            caller.request().as_str().as_bytes(),
            &activation.to_le_bytes(),
        ])
        .map_err(step)?;
        let node = identity(&[
            b"tobkiri.core-user-invocation-node/1\0",
            frame.as_str().as_bytes(),
            shape_identity(entry.view)
                .map_err(step)?
                .as_str()
                .as_bytes(),
            entry.op.as_str().as_bytes(),
        ])
        .map_err(step)?;
        let path = identity(&[
            b"tobkiri.core-user-invocation-path/1\0",
            frame.as_str().as_bytes(),
            document.as_str().as_bytes(),
            target.pack.as_str().as_bytes(),
            target.hash.as_str().as_bytes(),
            shape_identity(&target.provided)
                .map_err(step)?
                .as_str()
                .as_bytes(),
        ])
        .map_err(step)?;
        let prepared = PreparedUserEntry {
            provider: self,
            caller,
            invocation: entry,
            document,
            revision,
            epoch: caller.security_epoch(),
            target,
            frame: &frame,
            node: &node,
            activation,
            path: &path,
            state: Mutex::new(PreparationState::Fresh),
        };
        let mut created = None;
        self.gate
            .with_frame_preparation(
                caller,
                &prepared,
                AllocationReason::RuntimeValue,
                &mut |funding| {
                    if funding.limit == 0 {
                        return Err(GateError::Unavailable);
                    }
                    let data_bytes = user_invocation_data_storage_bytes(entry, target)
                        .ok_or(GateError::Unavailable)?;
                    let metadata_bytes = caller
                        .profile()
                        .clone_storage_bytes()
                        .checked_add(
                            caller
                                .principal()
                                .clone_storage_bytes()
                                .map_err(|_| GateError::Unavailable)?,
                        )
                        .and_then(|bytes| bytes.checked_add(caller.request().clone_storage_bytes()))
                        .ok_or(GateError::Unavailable)?;
                    let state_bytes =
                        ControlHandle::<ReachedState>::storage_bytes::<ControlReservation>()
                            .checked_add(data_bytes)
                            .and_then(|bytes| bytes.checked_add(metadata_bytes))
                            .and_then(|bytes| u64::try_from(bytes).ok())
                            .ok_or(GateError::Unavailable)?;
                    let reservation = caller.control().reserve(ControlKind::Frame, state_bytes)?;
                    let metadata = ActiveFrameMetadata {
                        profile: caller
                            .profile()
                            .try_clone_precharged()
                            .map_err(|_| GateError::Unavailable)?,
                        revision: revision.clone(),
                        security_epoch: prepared.epoch,
                        principal: caller
                            .principal()
                            .try_clone_precharged()
                            .map_err(|_| GateError::Unavailable)?,
                        request: caller
                            .request()
                            .try_clone_precharged()
                            .map_err(|_| GateError::Unavailable)?,
                        frame: frame.clone(),
                        node: node.clone(),
                        activation,
                        owner: CompiledCodeOwner::UserDocument {
                            document: document.clone(),
                        },
                        reason: AllocationReason::RuntimeValue,
                        path: path.clone(),
                        transitive_memory_bytes: None,
                    };
                    let state = ControlHandle::try_new_precharged(
                        ReachedState {
                            auth: self.auth.clone(),
                            provider: provider_handle.clone(),
                            context: caller
                                .try_clone_precharged()
                                .map_err(|_| GateError::Unavailable)?,
                            entry_context: caller
                                .try_clone_precharged()
                                .map_err(|_| GateError::Unavailable)?,
                            entry_evidence: None,
                            flow_key: String::new(),
                            code: None,
                            step_address: None,
                            overlay_base_address: None,
                            kind: None,
                            witness: None,
                            overlay_index: None,
                            activation,
                            frame: frame.clone(),
                            path: path.clone(),
                            metadata,
                            origin: ReachedOrigin::UserArguments(user_invocation_data_precharged(
                                entry,
                                document,
                                revision,
                                prepared.epoch,
                                target,
                            )?),
                            state: Mutex::new(NodeLifecycle {
                                live: true,
                                claimed: false,
                                expected_args: None,
                                expected_target: None,
                                provided_flow_key: None,
                                source_receipt_claimed: false,
                                compiled_prepared: false,
                                output_digest: None,
                                acknowledged: false,
                                stream_ready: false,
                                ack_evidence: None,
                                target_control: None,
                                args_control: None,
                            }),
                            retained_bytes: state_bytes,
                        },
                        reservation,
                    )
                    .map_err(|_| GateError::Unavailable)?;
                    created = Some(state);
                    Ok(())
                },
            )
            .map_err(step)?;
        let state = created.ok_or_else(|| step(GateError::Unavailable))?;
        let retirement = funded_handle(
            caller.control(),
            ControlKind::Frame,
            0,
            NodeExecutionGuard(state.clone()),
        )
        .map_err(step)?;
        let reservation = caller
            .control()
            .reserve(
                ControlKind::Registration,
                u64::try_from(std::mem::size_of::<CoreUserInvocationPreparation>() + 160)
                    .map_err(|_| step(GateError::Unavailable))?,
            )
            .map_err(|error| step(GateError::from(error)))?;
        let preparation = Arc::new(CoreUserInvocationPreparation {
            state,
            _retirement: retirement,
            used: AtomicBool::new(false),
            _reservation: reservation,
        });
        self.auth
            .publish_weak(&self.auth.user_preparations, &preparation, 1024, None)
            .map_err(step)?;
        Ok(preparation)
    }

    pub(crate) fn validate_prepare(&self, request: &ProviderRequest<'_>) -> Result<(), StepError> {
        self.validate_provider_request(request, false)
    }

    fn validate_provider_request(
        &self,
        request: &ProviderRequest<'_>,
        after_final_claim: bool,
    ) -> Result<(), StepError> {
        self.gate.check_context(request.caller).map_err(step)?;
        if self.auth.closing.load(Ordering::Acquire) {
            return Err(step(GateError::Unavailable));
        }
        match request.origin {
            ProviderOrigin::Reached { frame, node } => {
                let reached = node
                    .core_proof::<ReachedActivationProof>()
                    .ok_or_else(|| step(GateError::Unavailable))?;
                let state = &reached.node;
                if !ControlHandle::ptr_eq(&self.auth, &state.auth)
                    || !same_context_pair(frame.context(), request.caller)
                    || frame.identity() != state.frame
                    || !matches!(state.origin, ReachedOrigin::Flow)
                    || !matches!(
                        state.kind,
                        Some(FlowNodeKind::PortCall | FlowNodeKind::StreamEmit)
                    )
                {
                    return Err(step(denied(
                        "provider origin is not a live reached Flow call",
                    )));
                }
                check_frozen_node(state).map_err(step)?;
                let witness = state
                    .witness
                    .as_ref()
                    .ok_or_else(|| step(GateError::Unavailable))?;
                let _admission = self
                    .auth
                    .admission
                    .lock()
                    .map_err(|_| step(GateError::Unavailable))?;
                if self.auth.closing.load(Ordering::Acquire) {
                    return Err(step(GateError::Unavailable));
                }
                let lifecycle = state
                    .state
                    .lock()
                    .map_err(|_| step(GateError::Unavailable))?;
                let target = lifecycle
                    .expected_target
                    .and_then(|index| witness.targets().get(index));
                if !lifecycle.live
                    || lifecycle.claimed != after_final_claim
                    || lifecycle.expected_args.as_ref() != Some(request.args_digest)
                    || witness.shape() != request.view
                    || witness.op() != request.op
                    || !target.is_some_and(|target| {
                        target.pack == request.target.pack
                            && target.hash == request.target.hash
                            && target.provided == request.target.provided
                    })
                {
                    return Err(step(denied(
                        "provider request differs from its actual compiled call",
                    )));
                }
            }
            ProviderOrigin::NativeInput { entry, preparation } => {
                let key = Arc::as_ptr(preparation) as *const () as usize;
                let preparation = self
                    .auth
                    .native_preparations
                    .lock()
                    .map_err(|_| step(GateError::Unavailable))?
                    .get(&key)
                    .cloned()
                    .and_then(|preparation| preparation.upgrade())
                    .ok_or_else(|| step(denied("native preparation is not from this factory")))?;
                let frame = &preparation.frame;
                let state = &frame.state;
                let ReachedOrigin::NativeArguments(native) = &state.origin else {
                    return Err(step(GateError::Unavailable));
                };
                let tobkiri_gate::Principal::Input { source } = request.caller.principal() else {
                    return Err(step(denied("native read lost its original Input context")));
                };
                if !ControlHandle::ptr_eq(&self.auth, &state.auth)
                    || !same_context_pair(&state.context, request.caller)
                    || !ptr::eq(
                        self.auth
                            .compiled
                            .get()
                            .input_entries()
                            .get(native.index)
                            .ok_or_else(|| step(GateError::Unavailable))?,
                        entry,
                    )
                    || source != &native.source
                    || request.view != &native.view
                    || request.op != &native.op
                    || request.target.pack != native.target.pack
                    || request.target.hash != native.target.hash
                    || request.target.provided != native.target.provided
                    || !frame.used.load(Ordering::Acquire)
                {
                    return Err(step(denied(
                        "provider request differs from its actual native read",
                    )));
                }
                self.gate
                    .input_capacity_limit(request.caller, request.view, request.op, request.target)
                    .map_err(step)?;
                let _admission = self
                    .auth
                    .admission
                    .lock()
                    .map_err(|_| step(GateError::Unavailable))?;
                if self.auth.closing.load(Ordering::Acquire) {
                    return Err(step(GateError::Unavailable));
                }
                let mut lifecycle = state
                    .state
                    .lock()
                    .map_err(|_| step(GateError::Unavailable))?;
                if !lifecycle.live || lifecycle.claimed || lifecycle.acknowledged {
                    return Err(step(denied(
                        "native source argument activation is no longer live",
                    )));
                }
                if after_final_claim {
                    if lifecycle.expected_args.as_ref() != Some(request.args_digest) {
                        return Err(step(denied(
                            "native source dispatch differs from its prepared arguments",
                        )));
                    }
                } else {
                    if lifecycle.expected_args.is_some() {
                        return Err(step(denied(
                            "native source argument digest was already bound",
                        )));
                    }
                    let digest_bytes = std::mem::size_of::<Digest>()
                        .checked_add(32)
                        .ok_or_else(|| step(GateError::Unavailable))?;
                    let digest_bytes =
                        u64::try_from(digest_bytes).map_err(|_| step(GateError::Unavailable))?;
                    let reservation = request
                        .caller
                        .control()
                        .reserve(ControlKind::Frame, digest_bytes)
                        .map_err(|error| step(GateError::from(error)))?;
                    lifecycle.expected_args = Some(request.args_digest.clone());
                    lifecycle.args_control = Some(reservation);
                }
            }
            ProviderOrigin::UiInvocation { entry, preparation } => {
                let key = Arc::as_ptr(preparation) as *const () as usize;
                let preparation = self
                    .auth
                    .ui_preparations
                    .lock()
                    .map_err(|_| step(GateError::Unavailable))?
                    .get(&key)
                    .cloned()
                    .and_then(|preparation| preparation.upgrade())
                    .ok_or_else(|| step(denied("UI preparation is not from this factory")))?;
                let state = &preparation.state;
                let ReachedOrigin::UiArguments(ui) = &state.origin else {
                    return Err(step(GateError::Unavailable));
                };
                let Principal::PackUi {
                    pack,
                    hash,
                    ui: principal_session,
                } = request.caller.principal()
                else {
                    return Err(step(denied("UI dispatch lost its PackUi caller")));
                };
                if !ControlHandle::ptr_eq(&self.auth, &state.auth)
                    || !same_context_pair(&state.context, request.caller)
                    || ui.invocation != *entry.invocation
                    || ui.session != *entry.session
                    || ui.generation != entry.generation
                    || ui.receiver != *entry.receiver
                    || ui.view != *entry.view
                    || ui.op != *entry.op
                    || !same_profile_gate_target(entry.target, &ui.target)
                    || ui.receiver != crate::session::ui_receiver_identity(&ui.session)
                    || principal_session != &ui.session
                    || !self
                        .auth
                        .compiled
                        .get()
                        .profile
                        .packs
                        .iter()
                        .any(|pin| pin.id == *pack && pin.sha256 == *hash)
                    || request.view != entry.view
                    || request.op != entry.op
                    || !same_profile_gate_target(entry.target, request.target)
                {
                    return Err(step(denied(
                        "provider request differs from its exact registered UI receive",
                    )));
                }
                self.gate
                    .resolve_frozen_target(request.caller, request.view, request.op)
                    .map_err(step)
                    .and_then(|target| {
                        if target == *request.target {
                            Ok(())
                        } else {
                            Err(step(denied("UI provider target changed after preparation")))
                        }
                    })?;
                let _admission = self
                    .auth
                    .admission
                    .lock()
                    .map_err(|_| step(GateError::Unavailable))?;
                if self.auth.closing.load(Ordering::Acquire) {
                    return Err(step(GateError::Unavailable));
                }
                let mut lifecycle = state
                    .state
                    .lock()
                    .map_err(|_| step(GateError::Unavailable))?;
                if !lifecycle.live
                    || lifecycle.claimed
                    || lifecycle.acknowledged
                    || !lifecycle.compiled_prepared
                {
                    return Err(step(denied(
                        "UI argument phase is not live or its digest changed",
                    )));
                }
                if after_final_claim {
                    if lifecycle.expected_args.as_ref() != Some(request.args_digest) {
                        return Err(step(denied(
                            "UI dispatch did not retain its pre-materialized argument digest",
                        )));
                    }
                } else if lifecycle.expected_args.is_none() {
                    let digest_bytes = std::mem::size_of::<Digest>()
                        .checked_add(32)
                        .ok_or_else(|| step(GateError::Unavailable))?;
                    let digest_bytes =
                        u64::try_from(digest_bytes).map_err(|_| step(GateError::Unavailable))?;
                    let reservation = request
                        .caller
                        .control()
                        .reserve(ControlKind::Frame, digest_bytes)
                        .map_err(|error| step(GateError::from(error)))?;
                    lifecycle.expected_args = Some(request.args_digest.clone());
                    lifecycle.args_control = Some(reservation);
                } else {
                    return Err(step(denied(
                        "UI argument digest was already bound to a prior dispatch attempt",
                    )));
                }
            }
            ProviderOrigin::UserInvocation { entry, preparation } => {
                let key = Arc::as_ptr(preparation) as *const () as usize;
                let preparation = self
                    .auth
                    .user_preparations
                    .lock()
                    .map_err(|_| step(GateError::Unavailable))?
                    .get(&key)
                    .cloned()
                    .and_then(|preparation| preparation.upgrade())
                    .ok_or_else(|| step(denied("User preparation is not from this factory")))?;
                let state = &preparation.state;
                let ReachedOrigin::UserArguments(user) = &state.origin else {
                    return Err(step(GateError::Unavailable));
                };
                if !matches!(request.caller.principal(), Principal::User)
                    || !ControlHandle::ptr_eq(&self.auth, &state.auth)
                    || !same_context_pair(&state.context, request.caller)
                    || user.invocation != *entry.invocation
                    || user.revision != self.auth.compiled.get().plan_digest
                    || user.epoch != request.caller.security_epoch()
                    || user.view != *entry.view
                    || user.op != *entry.op
                    || !same_profile_gate_target(entry.target, &user.target)
                    || request.view != entry.view
                    || request.op != entry.op
                    || !same_profile_gate_target(entry.target, request.target)
                {
                    return Err(step(denied(
                        "provider request differs from its exact registered User receive",
                    )));
                }
                self.gate
                    .code_owner_execution_limits(
                        request.caller,
                        PreparedCodeOwner::UserDocument {
                            document: &user.document,
                        },
                    )
                    .map_err(step)?;
                self.gate
                    .resolve_frozen_target(request.caller, request.view, request.op)
                    .map_err(step)
                    .and_then(|target| {
                        if target == *request.target && target == user.target {
                            Ok(())
                        } else {
                            Err(step(denied(
                                "User provider target changed after preparation",
                            )))
                        }
                    })?;
                let _admission = self
                    .auth
                    .admission
                    .lock()
                    .map_err(|_| step(GateError::Unavailable))?;
                if self.auth.closing.load(Ordering::Acquire) {
                    return Err(step(GateError::Unavailable));
                }
                let mut lifecycle = state
                    .state
                    .lock()
                    .map_err(|_| step(GateError::Unavailable))?;
                if !lifecycle.live
                    || lifecycle.claimed
                    || lifecycle.acknowledged
                    || !lifecycle.compiled_prepared
                {
                    return Err(step(denied(
                        "User argument phase is not live or its digest changed",
                    )));
                }
                if after_final_claim {
                    if lifecycle.expected_args.as_ref() != Some(request.args_digest) {
                        return Err(step(denied(
                            "User dispatch did not retain its pre-materialized digest",
                        )));
                    }
                } else if lifecycle.expected_args.is_none() {
                    let digest_bytes = std::mem::size_of::<Digest>()
                        .checked_add(32)
                        .ok_or_else(|| step(GateError::Unavailable))?;
                    let reservation = request
                        .caller
                        .control()
                        .reserve(
                            ControlKind::Frame,
                            u64::try_from(digest_bytes)
                                .map_err(|_| step(GateError::Unavailable))?,
                        )
                        .map_err(|error| step(GateError::from(error)))?;
                    lifecycle.expected_args = Some(request.args_digest.clone());
                    lifecycle.args_control = Some(reservation);
                } else {
                    return Err(step(denied(
                        "User argument digest was already bound to a prior dispatch attempt",
                    )));
                }
            }
            ProviderOrigin::Guest { frame } => {
                let key = Arc::as_ptr(frame) as *const () as usize;
                let provider_frame = self
                    .auth
                    .live_provider_frames
                    .lock()
                    .map_err(|_| step(GateError::Unavailable))?
                    .get(&key)
                    .cloned()
                    .and_then(|frame| frame.upgrade())
                    .ok_or_else(|| step(denied("guest provider caller is not a Core frame")))?;
                let state = &provider_frame.state;
                let Principal::Pack { pack: id, hash } = request.caller.principal() else {
                    return Err(step(denied("User code cannot spoof a Pack caller")));
                };
                if !ControlHandle::ptr_eq(&self.auth, &state.auth)
                    || !same_context_pair(frame.context(), request.caller)
                    || !matches!(
                        &state.metadata.owner,
                        CompiledCodeOwner::Pack { pack, hash: owner_hash }
                            if pack == id && owner_hash == hash
                    )
                {
                    return Err(step(denied("dynamic provider caller context changed")));
                }
                let _admission = self
                    .auth
                    .admission
                    .lock()
                    .map_err(|_| step(GateError::Unavailable))?;
                if self.auth.closing.load(Ordering::Acquire) {
                    return Err(step(GateError::Unavailable));
                }
                let lifecycle = state
                    .state
                    .lock()
                    .map_err(|_| step(GateError::Unavailable))?;
                if !lifecycle.live || lifecycle.acknowledged {
                    return Err(step(denied("dynamic provider caller has retired")));
                }
            }
            ProviderOrigin::Direct => {
                return Err(step(denied(
                    "provider entry has no authenticated direct UI or native source",
                )));
            }
        }
        Ok(())
    }

    pub(crate) fn enter_provider_with_handle(
        &self,
        provider_handle: ControlHandle<CoreProviderAuthority>,
        request: &ProviderRequest<'_>,
        consumed: &DispatchPermit,
        actual_args: &CoreValue,
    ) -> Result<Arc<dyn ProviderFrame>, StepError> {
        self.validate_provider_request(request, true)?;
        if !consumed.matches_context(request.caller)
            || consumed.args_digest() != request.args_digest
            || consumed.deadline() <= Instant::now()
            || !matches!(consumed.effect(), EffectRequest::PortCall { shape, op } if shape == request.view && op == request.op)
        {
            return Err(step(denied(
                "consumed provider dispatch differs from its request",
            )));
        }
        let (native_frame, ui_frame, user_frame, call_path, live_caller) = match request.origin {
            ProviderOrigin::Reached { frame: _, node } => {
                let reached = node
                    .core_proof::<ReachedActivationProof>()
                    .ok_or_else(|| step(GateError::Unavailable))?;
                let state = reached.node.clone();
                let _admission = self
                    .auth
                    .admission
                    .lock()
                    .map_err(|_| step(GateError::Unavailable))?;
                if self.auth.closing.load(Ordering::Acquire) {
                    return Err(step(GateError::Unavailable));
                }
                let lifecycle = state
                    .state
                    .lock()
                    .map_err(|_| step(GateError::Unavailable))?;
                if !lifecycle.live || !lifecycle.claimed {
                    return Err(step(denied("compiled provider dispatch was not claimed")));
                }
                drop(lifecycle);
                (None, None, None, state.path.clone(), Some(state))
            }
            ProviderOrigin::NativeInput { preparation, .. } => {
                let key = Arc::as_ptr(preparation) as *const () as usize;
                let preparation = self
                    .auth
                    .native_preparations
                    .lock()
                    .map_err(|_| step(GateError::Unavailable))?
                    .remove(&key)
                    .and_then(|preparation| preparation.upgrade())
                    .ok_or_else(|| step(denied("native preparation was already consumed")))?;
                let frame = preparation.frame.clone();
                let state = frame.state.clone();
                if !matches!(&state.origin, ReachedOrigin::NativeArguments(_)) {
                    return Err(step(GateError::Unavailable));
                }
                let _admission = self
                    .auth
                    .admission
                    .lock()
                    .map_err(|_| step(GateError::Unavailable))?;
                if self.auth.closing.load(Ordering::Acquire) {
                    return Err(step(GateError::Unavailable));
                }
                let mut lifecycle = state
                    .state
                    .lock()
                    .map_err(|_| step(GateError::Unavailable))?;
                if !lifecycle.live
                    || lifecycle.claimed
                    || lifecycle.expected_args.as_ref() != Some(request.args_digest)
                {
                    return Err(step(denied(
                        "native source read is not the exact claimed input",
                    )));
                }
                lifecycle.claimed = true;
                lifecycle.live = false;
                lifecycle.expected_args = None;
                lifecycle.args_control.take();
                drop(lifecycle);
                drop(_admission);
                (Some(frame), None, None, state.path.clone(), None)
            }
            ProviderOrigin::UiInvocation { preparation, .. } => {
                let key = Arc::as_ptr(preparation) as *const () as usize;
                let preparation = self
                    .auth
                    .ui_preparations
                    .lock()
                    .map_err(|_| step(GateError::Unavailable))?
                    .remove(&key)
                    .and_then(|preparation| preparation.upgrade())
                    .ok_or_else(|| step(denied("UI preparation was already consumed")))?;
                let state = preparation.state.clone();
                if !matches!(&state.origin, ReachedOrigin::UiArguments(_)) {
                    return Err(step(GateError::Unavailable));
                }
                let _admission = self
                    .auth
                    .admission
                    .lock()
                    .map_err(|_| step(GateError::Unavailable))?;
                if self.auth.closing.load(Ordering::Acquire) {
                    return Err(step(GateError::Unavailable));
                }
                let mut lifecycle = state
                    .state
                    .lock()
                    .map_err(|_| step(GateError::Unavailable))?;
                if !lifecycle.live
                    || lifecycle.claimed
                    || lifecycle.acknowledged
                    || !lifecycle.compiled_prepared
                    || lifecycle.expected_args.as_ref() != Some(request.args_digest)
                {
                    return Err(step(denied(
                        "UI argument phase was not sealed for this exact dispatch",
                    )));
                }
                lifecycle.claimed = true;
                lifecycle.live = false;
                lifecycle.expected_args = None;
                lifecycle.args_control.take();
                drop(lifecycle);
                drop(_admission);
                let frame = state;
                let path = frame.path.clone();
                (None, Some(frame), None, path, None)
            }
            ProviderOrigin::UserInvocation { preparation, .. } => {
                let key = Arc::as_ptr(preparation) as *const () as usize;
                let preparation = self
                    .auth
                    .user_preparations
                    .lock()
                    .map_err(|_| step(GateError::Unavailable))?
                    .remove(&key)
                    .and_then(|preparation| preparation.upgrade())
                    .ok_or_else(|| step(denied("User preparation was already consumed")))?;
                let state = preparation.state.clone();
                if !matches!(&state.origin, ReachedOrigin::UserArguments(_)) {
                    return Err(step(GateError::Unavailable));
                }
                let _admission = self
                    .auth
                    .admission
                    .lock()
                    .map_err(|_| step(GateError::Unavailable))?;
                if self.auth.closing.load(Ordering::Acquire) {
                    return Err(step(GateError::Unavailable));
                }
                let mut lifecycle = state
                    .state
                    .lock()
                    .map_err(|_| step(GateError::Unavailable))?;
                if !lifecycle.live
                    || lifecycle.claimed
                    || lifecycle.acknowledged
                    || !lifecycle.compiled_prepared
                    || lifecycle.expected_args.as_ref() != Some(request.args_digest)
                {
                    return Err(step(denied(
                        "User argument phase was not sealed for this exact dispatch",
                    )));
                }
                lifecycle.claimed = true;
                lifecycle.live = false;
                lifecycle.expected_args = None;
                lifecycle.args_control.take();
                drop(lifecycle);
                drop(_admission);
                let path = state.path.clone();
                (None, None, Some(state), path, None)
            }
            ProviderOrigin::Guest { frame } => {
                let key = Arc::as_ptr(frame) as *const () as usize;
                let provider_frame = self
                    .auth
                    .live_provider_frames
                    .lock()
                    .map_err(|_| step(GateError::Unavailable))?
                    .get(&key)
                    .cloned()
                    .and_then(|frame| frame.upgrade())
                    .ok_or_else(|| step(denied("dynamic call lost its live provider caller")))?;
                (
                    None,
                    None,
                    None,
                    provider_frame.state.path.clone(),
                    Some(provider_frame.state.clone()),
                )
            }
            ProviderOrigin::Direct => {
                return Err(step(denied("direct provider entry is unavailable")));
            }
        };
        let _ = actual_args;
        let owner_limits = self
            .gate
            .code_owner_execution_limits(
                request.caller,
                PreparedCodeOwner::Pack {
                    pack: &request.target.pack,
                    hash: &request.target.hash,
                },
            )
            .map_err(step)?;
        let context = request
            .caller
            .try_callee(
                tobkiri_gate::Principal::Pack {
                    pack: request
                        .target
                        .pack
                        .try_clone_precharged()
                        .map_err(|_| step(GateError::Unavailable))?,
                    hash: request.target.hash.clone(),
                },
                owner_limits,
            )
            .map_err(step)?;
        let entry_context = request
            .caller
            .try_clone_precharged()
            .map_err(|_| step(GateError::Unavailable))?;
        let evidence_dynamic = consumed
            .execution_entry_evidence_storage_bytes()
            .map_err(|_| step(GateError::Unavailable))?;
        let evidence_bytes =
            ControlHandle::<tobkiri_gate::ExecutionEntryEvidence>::storage_bytes::<
                ControlReservation,
            >()
            .checked_add(evidence_dynamic)
            .and_then(|bytes| u64::try_from(bytes).ok())
            .ok_or_else(|| step(GateError::Unavailable))?;
        let evidence_reservation = self
            .control
            .reserve(ControlKind::Frame, evidence_bytes)
            .map_err(|error| step(GateError::from(error)))?;
        let evidence = consumed
            .try_execution_entry_evidence_precharged()
            .map_err(|_| step(GateError::Unavailable))?;
        let evidence = ControlHandle::try_new_precharged(evidence, evidence_reservation)
            .map_err(|_| step(GateError::Unavailable))?;
        let activation = next_sequence(&self.auth.activation).map_err(step)?;
        let frame = identity(&[
            b"tobkiri.core-provider-frame/1\0",
            self.auth.identity.as_str().as_bytes(),
            call_path.as_str().as_bytes(),
            consumed.id().as_str().as_bytes(),
            &activation.to_le_bytes(),
        ])
        .map_err(step)?;
        let node = identity(&[
            b"tobkiri.core-provider-node/1\0",
            frame.as_str().as_bytes(),
            request.args_digest.as_str().as_bytes(),
        ])
        .map_err(step)?;
        let path = identity(&[
            b"tobkiri.core-provider-path/1\0",
            call_path.as_str().as_bytes(),
            frame.as_str().as_bytes(),
            request.target.pack.as_str().as_bytes(),
            request.target.hash.as_str().as_bytes(),
        ])
        .map_err(step)?;
        let native_source = match native_frame.as_ref() {
            Some(frame) => match &frame.state.origin {
                ReachedOrigin::NativeArguments(native) => Some(native),
                _ => return Err(step(GateError::Unavailable)),
            },
            None => None,
        };
        let ui_source = match ui_frame.as_ref() {
            Some(frame) => match &frame.origin {
                ReachedOrigin::UiArguments(ui) => Some(ui),
                _ => return Err(step(GateError::Unavailable)),
            },
            None => None,
        };
        let user_source = match user_frame.as_ref() {
            Some(frame) => match &frame.origin {
                ReachedOrigin::UserArguments(user) => Some(user),
                _ => return Err(step(GateError::Unavailable)),
            },
            None => None,
        };
        let metadata_bytes = provider_metadata_storage_bytes(
            request.caller,
            request.target,
            request.view,
            request.op,
            native_source,
            ui_source,
            user_source,
        )
        .ok_or_else(|| step(GateError::Unavailable))?;
        let state_bytes = ControlHandle::<ReachedState>::storage_bytes::<ControlReservation>()
            .checked_add(metadata_bytes)
            .and_then(|bytes| u64::try_from(bytes).ok())
            .ok_or_else(|| step(GateError::Unavailable))?;
        let state_reservation = self
            .control
            .reserve(ControlKind::Frame, state_bytes)
            .map_err(|error| step(GateError::from(error)))?;
        let metadata = ActiveFrameMetadata {
            profile: context
                .profile()
                .try_clone_precharged()
                .map_err(|_| step(GateError::Unavailable))?,
            revision: context
                .revision()
                .ok_or_else(|| step(denied("provider context lost its frozen revision")))?
                .clone(),
            security_epoch: context.security_epoch(),
            principal: context
                .principal()
                .try_clone_precharged()
                .map_err(|_| step(GateError::Unavailable))?,
            request: context
                .request()
                .try_clone_precharged()
                .map_err(|_| step(GateError::Unavailable))?,
            frame: frame.clone(),
            node: node.clone(),
            activation,
            owner: CompiledCodeOwner::Pack {
                pack: request
                    .target
                    .pack
                    .try_clone_precharged()
                    .map_err(|_| step(GateError::Unavailable))?,
                hash: request.target.hash.clone(),
            },
            reason: AllocationReason::RuntimeValue,
            path: path.clone(),
            transitive_memory_bytes: None,
        };
        let native_data = native_source
            .map(clone_native_input_data_precharged)
            .transpose()
            .map_err(step)?;
        let ui_data = ui_source
            .map(clone_ui_invocation_data_precharged)
            .transpose()
            .map_err(step)?;
        let user_data = user_source
            .map(clone_user_invocation_data_precharged)
            .transpose()
            .map_err(step)?;
        let state = ControlHandle::try_new_precharged(
            ReachedState {
                auth: self.auth.clone(),
                provider: provider_handle,
                context,
                entry_context,
                entry_evidence: Some(evidence),
                flow_key: String::new(),
                code: None,
                step_address: None,
                overlay_base_address: None,
                kind: None,
                witness: None,
                overlay_index: None,
                activation,
                frame: frame.clone(),
                path: path.clone(),
                metadata,
                origin: ReachedOrigin::Provider(ProviderStateData {
                    view: request.view.clone(),
                    op: request.op.clone(),
                    target: clone_gate_target(request.target)
                        .map_err(|_| step(GateError::Unavailable))?,
                    grant: consumed
                        .id()
                        .try_clone_precharged()
                        .map_err(|_| step(GateError::Unavailable))?,
                    args: request.args_digest.clone(),
                    native: native_data,
                    ui: ui_data,
                    user: user_data,
                }),
                state: Mutex::new(NodeLifecycle {
                    live: true,
                    claimed: true,
                    expected_args: Some(request.args_digest.clone()),
                    expected_target: None,
                    provided_flow_key: None,
                    source_receipt_claimed: false,
                    compiled_prepared: true,
                    output_digest: None,
                    acknowledged: false,
                    stream_ready: false,
                    ack_evidence: None,
                    target_control: None,
                    args_control: None,
                }),
                retained_bytes: state_bytes,
            },
            state_reservation,
        )
        .map_err(|_| step(GateError::Unavailable))?;
        let retirement = funded_handle(
            &self.control,
            ControlKind::Frame,
            0,
            NodeExecutionGuard(state.clone()),
        )
        .map_err(step)?;
        let frame_reservation = self
            .control
            .reserve(
                ControlKind::Frame,
                u64::try_from(std::mem::size_of::<CoreProviderFrame>() + 192)
                    .map_err(|_| step(GateError::Unavailable))?,
            )
            .map_err(|error| step(GateError::from(error)))?;
        let provider_frame = Arc::new(CoreProviderFrame {
            state: state.clone(),
            _retirement: retirement,
            _reservation: frame_reservation,
        });
        // Construction above performs no guest work. Publish under the same
        // short critical section as close/retirement, rechecking the actual
        // reached/guest caller after all fallible native construction.
        self.auth
            .publish_weak(
                &self.auth.live_provider_frames,
                &provider_frame,
                1024,
                live_caller.as_ref().map(ControlHandle::get),
            )
            .map_err(step)?;
        let provider_frame: Arc<dyn ProviderFrame> = provider_frame;
        Ok(provider_frame)
    }

    /// Create the exact active source lifetime for one already-consumed
    /// StreamOpen/CallbackOpen. The caller retains the sole raw permit and
    /// later supplies its immutable Gate ACK after source cleanup.
    pub(crate) fn enter_handle_source_with_handle(
        &self,
        provider_handle: ControlHandle<CoreProviderAuthority>,
        request: &HandleSourceRequest<'_>,
        consumed: &DispatchPermit,
    ) -> Result<Arc<dyn ProviderFrame>, StepError> {
        self.gate.check_context(request.owner).map_err(step)?;
        if self.auth.closing.load(Ordering::Acquire)
            || request.generation == 0
            || !consumed.matches_context(request.owner)
            || consumed.deadline() <= Instant::now()
            || consumed.revision() != request.owner.revision()
        {
            return Err(step(denied(
                "handle source lost its live owner or consumed entry",
            )));
        }
        let parent_key = Arc::as_ptr(request.provider) as *const () as usize;
        let parent_frame = self
            .auth
            .live_provider_frames
            .lock()
            .map_err(|_| step(GateError::Unavailable))?
            .get(&parent_key)
            .cloned()
            .and_then(|frame| frame.upgrade())
            .ok_or_else(|| step(denied("handle source parent is not a Core provider frame")))?;
        let parent = &parent_frame.state;
        let (owner_pack, owner_hash) = match &parent.metadata.owner {
            CompiledCodeOwner::Pack { pack, hash } => (pack, hash),
            CompiledCodeOwner::User { .. } => {
                return Err(step(denied("User UI cannot own a guest handle source")));
            }
            CompiledCodeOwner::UserDocument { .. } => {
                return Err(step(denied(
                    "User invocation cannot own a guest handle source",
                )));
            }
        };
        if !ControlHandle::ptr_eq(&self.auth, &parent.auth)
            || check_same_context(request.provider.context(), request.owner).is_err()
            || !matches!(
                request.owner.principal(),
                Principal::Pack { pack, hash } if pack == owner_pack && hash == owner_hash
            )
            || request.owner.revision() != Some(&self.auth.compiled.get().plan_digest)
        {
            return Err(step(denied(
                "handle source owner differs from its actual provider activation",
            )));
        }
        let expected_entry = match request.kind {
            tobkiri_wasm::HandleKind::Stream => tobkiri_gate::WasmEntryKind::StreamOpen {
                handle: request.local_id,
            },
            tobkiri_wasm::HandleKind::Callback => tobkiri_gate::WasmEntryKind::CallbackOpen {
                handle: request.local_id,
            },
        };
        if !matches!(
            consumed.effect(),
            EffectRequest::WasmEntry { pack, hash, entry }
                if pack == owner_pack && hash == owner_hash && entry == &expected_entry
        ) {
            return Err(step(denied(
                "handle source permit does not match its actual Pack handle",
            )));
        }

        // Serialize this new source activation with the actual parent node's
        // terminal and retirement. A returned source gets its own guard before
        // the parent PortCall is allowed to retire.
        let _admission = self
            .auth
            .admission
            .lock()
            .map_err(|_| step(GateError::Unavailable))?;
        if self.auth.closing.load(Ordering::Acquire) {
            return Err(step(GateError::Unavailable));
        }
        let parent_lifecycle = parent
            .state
            .lock()
            .map_err(|_| step(GateError::Unavailable))?;
        if !parent_lifecycle.live || parent_lifecycle.acknowledged {
            return Err(step(denied("handle source parent activation retired")));
        }
        drop(parent_lifecycle);
        // No armed retirement guard may be created/dropped while admission
        // is held. Publication below rechecks both parent and factory liveness.
        drop(_admission);

        let activation = next_sequence(&self.auth.activation).map_err(step)?;
        let frame = identity(&[
            b"tobkiri.core-handle-source-frame/1\0",
            self.auth.identity.as_str().as_bytes(),
            parent.frame.as_str().as_bytes(),
            parent.path.as_str().as_bytes(),
            consumed.id().as_str().as_bytes(),
            &request.local_id.to_le_bytes(),
            &request.generation.to_le_bytes(),
            &[request.kind as u8],
            &activation.to_le_bytes(),
        ])
        .map_err(step)?;
        let node = identity(&[
            b"tobkiri.core-handle-source-node/1\0",
            frame.as_str().as_bytes(),
            shape_identity(request.authority)
                .map_err(step)?
                .as_str()
                .as_bytes(),
        ])
        .map_err(step)?;
        let path = identity(&[
            b"tobkiri.core-handle-source-path/1\0",
            parent.path.as_str().as_bytes(),
            frame.as_str().as_bytes(),
            shape_identity(request.authority)
                .map_err(step)?
                .as_str()
                .as_bytes(),
        ])
        .map_err(step)?;
        let evidence_dynamic = consumed
            .execution_entry_evidence_storage_bytes()
            .map_err(|_| step(GateError::Unavailable))?;
        let evidence_bytes =
            ControlHandle::<tobkiri_gate::ExecutionEntryEvidence>::storage_bytes::<
                ControlReservation,
            >()
            .checked_add(evidence_dynamic)
            .and_then(|bytes| u64::try_from(bytes).ok())
            .ok_or_else(|| step(GateError::Unavailable))?;
        let source_dynamic = std::mem::size_of::<HandleSourceStateData>()
            .checked_add(request.authority.clone_storage_bytes())
            .and_then(|bytes| bytes.checked_add(128))
            .ok_or_else(|| step(GateError::Unavailable))?;
        let metadata_dynamic = request
            .owner
            .profile()
            .clone_storage_bytes()
            .checked_add(
                request
                    .owner
                    .principal()
                    .clone_storage_bytes()
                    .map_err(|_| step(GateError::Unavailable))?,
            )
            .and_then(|bytes| bytes.checked_add(request.owner.request().clone_storage_bytes()))
            .and_then(|bytes| {
                bytes.checked_add(code_owner_pack_storage_bytes(owner_pack, owner_hash))
            })
            .ok_or_else(|| step(GateError::Unavailable))?;
        let state_bytes = ControlHandle::<ReachedState>::storage_bytes::<ControlReservation>()
            .checked_add(source_dynamic)
            .and_then(|bytes| bytes.checked_add(metadata_dynamic))
            .and_then(|bytes| u64::try_from(bytes).ok())
            .ok_or_else(|| step(GateError::Unavailable))?;
        let frame_bytes = u64::try_from(
            std::mem::size_of::<CoreProviderFrame>()
                .checked_add(192)
                .ok_or_else(|| step(GateError::Unavailable))?,
        )
        .map_err(|_| step(GateError::Unavailable))?;

        // Reserve every retained control block before cloning source IDs,
        // contexts, target authority or entry evidence.
        let state_reservation = self
            .control
            .reserve(ControlKind::Frame, state_bytes)
            .map_err(|error| step(GateError::from(error)))?;
        let evidence_reservation = self
            .control
            .reserve(ControlKind::Frame, evidence_bytes)
            .map_err(|error| step(GateError::from(error)))?;
        let frame_reservation = self
            .control
            .reserve(ControlKind::Frame, frame_bytes)
            .map_err(|error| step(GateError::from(error)))?;
        let evidence = consumed
            .try_execution_entry_evidence_precharged()
            .map_err(|_| step(GateError::Unavailable))?;
        let evidence = ControlHandle::try_new_precharged(evidence, evidence_reservation)
            .map_err(|_| step(GateError::Unavailable))?;
        let context = request
            .owner
            .try_clone_precharged()
            .map_err(|_| step(GateError::Unavailable))?;
        let entry_context = request
            .owner
            .try_clone_precharged()
            .map_err(|_| step(GateError::Unavailable))?;
        let metadata = ActiveFrameMetadata {
            profile: request
                .owner
                .profile()
                .try_clone_precharged()
                .map_err(|_| step(GateError::Unavailable))?,
            revision: self.auth.compiled.get().plan_digest.clone(),
            security_epoch: request.owner.security_epoch(),
            principal: request
                .owner
                .principal()
                .try_clone_precharged()
                .map_err(|_| step(GateError::Unavailable))?,
            request: request
                .owner
                .request()
                .try_clone_precharged()
                .map_err(|_| step(GateError::Unavailable))?,
            frame: frame.clone(),
            node: node.clone(),
            activation,
            owner: CompiledCodeOwner::Pack {
                pack: owner_pack
                    .try_clone_precharged()
                    .map_err(|_| step(GateError::Unavailable))?,
                hash: owner_hash.clone(),
            },
            reason: AllocationReason::RuntimeValue,
            path: path.clone(),
            transitive_memory_bytes: None,
        };
        let state = ControlHandle::try_new_precharged(
            ReachedState {
                auth: self.auth.clone(),
                provider: provider_handle,
                context,
                entry_context,
                entry_evidence: Some(evidence),
                flow_key: String::new(),
                code: None,
                step_address: None,
                overlay_base_address: None,
                kind: None,
                witness: None,
                overlay_index: None,
                activation,
                frame,
                path,
                metadata,
                origin: ReachedOrigin::HandleSource(HandleSourceStateData {
                    kind: request.kind,
                    local_id: request.local_id,
                    authority: request
                        .authority
                        .try_clone_precharged()
                        .map_err(|_| step(GateError::Unavailable))?,
                    generation: request.generation,
                    parent_frame: parent.frame.clone(),
                    parent_path: parent.path.clone(),
                }),
                state: Mutex::new(NodeLifecycle {
                    live: true,
                    claimed: true,
                    expected_args: None,
                    expected_target: None,
                    provided_flow_key: None,
                    source_receipt_claimed: false,
                    compiled_prepared: true,
                    output_digest: None,
                    acknowledged: false,
                    stream_ready: false,
                    ack_evidence: None,
                    target_control: None,
                    args_control: None,
                }),
                retained_bytes: state_bytes,
            },
            state_reservation,
        )
        .map_err(|_| step(GateError::Unavailable))?;
        let retirement = funded_handle(
            &self.control,
            ControlKind::Frame,
            0,
            NodeExecutionGuard(state.clone()),
        )
        .map_err(step)?;
        let provider_frame = Arc::new(CoreProviderFrame {
            state: state.clone(),
            _retirement: retirement,
            _reservation: frame_reservation,
        });
        self.auth
            .publish_weak(
                &self.auth.live_provider_frames,
                &provider_frame,
                1024,
                Some(parent.get()),
            )
            .map_err(step)?;
        let provider_frame: Arc<dyn ProviderFrame> = provider_frame;
        Ok(provider_frame)
    }
}
impl Drop for CoreProviderAuthority {
    fn drop(&mut self) {
        self.close();
    }
}

/// One actual root or formal-callee Flow invocation. The catalog and owner
/// stay frozen; each `reach_node` below must borrow the actual AST object used
/// by the interpreter. This frame has no new grant, cost or terminal custody.
struct ActiveFlowFrame {
    auth: ControlHandle<FactoryAuth>,
    provider: ControlHandle<CoreProviderAuthority>,
    gate: Arc<GateEngine>,
    invoker: Arc<dyn PortInvoker>,
    context: CallCx,
    entry_context: CallCx,
    entry_evidence: ControlHandle<tobkiri_gate::ExecutionEntryEvidence>,
    flow_key: String,
    code: CodeLocation,
    overlay_base_address: Option<usize>,
    overlay_index: Option<usize>,
    owner: CodeOwner,
    frame: Digest,
    path: Digest,
    activation: u64,
    /// A dynamic guest Flow remains a child of its actual provider activation.
    /// Cloning this shared guard does not mint another retirement capability.
    guest_parent: Option<ControlHandle<NodeExecutionGuard>>,
}

/// Core's continuation binds the one exact overlay activation to the captured
/// base frame. `Next` can consume it once; merely holding the overlay ID cannot
/// restore caller authority.
struct CapturedBaseContinuation {
    base: ControlHandle<dyn ActiveCodeFrame>,
    overlay_frame: Digest,
    overlay_path: Digest,
    used: AtomicBool,
}
fn allocation_proof(
    proof: &(dyn std::any::Any + Send + Sync),
) -> Result<&AllocationProof, GateError> {
    proof
        .downcast_ref::<AllocationProof>()
        .or_else(|| {
            proof
                .downcast_ref::<CompletedReadProof>()
                .map(|p| p.allocation.get())
        })
        .ok_or(GateError::Unavailable)
}
fn reached_proof(
    proof: &(dyn std::any::Any + Send + Sync),
) -> Result<&ReachedActivationProof, GateError> {
    proof
        .downcast_ref::<ReachedActivationProof>()
        .ok_or(GateError::Unavailable)
}
impl CoreVerifier {
    fn validate<'a>(
        &self,
        cx: &CallCx,
        proof: &'a (dyn std::any::Any + Send + Sync),
        reason: AllocationReason,
    ) -> Result<&'a AllocationProof, GateError> {
        let proof = allocation_proof(proof)?;
        let node = &proof.node;
        let metadata = &proof.metadata;
        if self.auth.closing.load(Ordering::Acquire)
            || !ControlHandle::ptr_eq(&self.auth, &node.auth)
            || metadata.reason != reason
            || !same_node_metadata(metadata, &node.metadata)
            || metadata.frame != node.frame
            || metadata.activation != node.activation
            || metadata.path != node.path
            || metadata.profile != self.auth.compiled.get().profile.id
            || metadata.revision != self.auth.compiled.get().plan_digest
            || metadata.security_epoch != node.context.security_epoch()
            || metadata.principal != *node.context.principal()
            || metadata.request != *node.context.request()
            || node.activation == 0
        {
            return Err(denied(
                "private Core node provenance does not match its frame",
            ));
        }
        check_same_context(&node.context, cx)?;
        check_frozen_node(node)?;
        Ok(proof)
    }
}
impl ActiveFrameVerifier for CoreVerifier {
    fn factory_identity(&self) -> &Digest {
        &self.auth.identity
    }
    fn close_registration(&self) {
        if let Ok(_admission) = self.auth.admission.lock() {
            self.auth.closing.store(true, Ordering::Release);
        } else {
            // Poisoned admission is permanently closed by the atomic fallback.
            self.auth.closing.store(true, Ordering::Release);
        }
    }
    fn authenticate<'a>(
        &self,
        cx: &CallCx,
        proof: &'a (dyn std::any::Any + Send + Sync),
        reason: AllocationReason,
    ) -> Result<&'a ActiveFrameMetadata, GateError> {
        let proof = self.validate(cx, proof, reason)?;
        // This is intentionally immutable inspection. Gate invokes it while
        // holding `with_allocation`'s admission/node critical section; taking
        // either mutex here would recursively deadlock. Live work is admitted
        // only through `with_allocation` and `claim_dispatch` below.
        Ok(&proof.metadata)
    }
    fn bound_context<'a>(
        &self,
        proof: &'a (dyn std::any::Any + Send + Sync),
    ) -> Result<&'a CallCx, GateError> {
        let proof = allocation_proof(proof)?;
        if self.auth.closing.load(Ordering::Acquire)
            || !ControlHandle::ptr_eq(&self.auth, &proof.node.auth)
        {
            return Err(GateError::Unavailable);
        }
        Ok(&proof.node.context)
    }
    fn retained_control_bytes(
        &self,
        proof: &(dyn std::any::Any + Send + Sync),
    ) -> Result<u64, GateError> {
        let proof = allocation_proof(proof)?;
        if !ControlHandle::ptr_eq(&self.auth, &proof.node.auth) || proof.node.retained_bytes == 0 {
            return Err(GateError::Unavailable);
        }
        let proof_bytes = ControlHandle::<AllocationProof>::storage_bytes::<ControlReservation>()
            .checked_add(
                proof
                    .metadata
                    .clone_storage_bytes()
                    .map_err(|_| GateError::Unavailable)?,
            )
            .and_then(|bytes| u64::try_from(bytes).ok())
            .ok_or(GateError::Unavailable)?;
        proof
            .node
            .retained_bytes
            .checked_add(proof_bytes)
            .ok_or(GateError::Unavailable)
    }
    fn with_allocation(
        &self,
        cx: &CallCx,
        proof: &(dyn std::any::Any + Send + Sync),
        reason: AllocationReason,
        admit: &mut dyn FnMut(&ActiveFrameMetadata) -> Result<(), GateError>,
    ) -> Result<(), GateError> {
        let proof = self.validate(cx, proof, reason)?;
        let _admission = self
            .auth
            .admission
            .lock()
            .map_err(|_| GateError::Unavailable)?;
        if self.auth.closing.load(Ordering::Acquire) {
            return Err(GateError::Unavailable);
        }
        let state = proof
            .node
            .state
            .lock()
            .map_err(|_| GateError::Unavailable)?;
        if !state.live {
            return Err(denied("Core node has retired"));
        }
        // Same mutex as retirement/claim. Fixed synchronous Gate capacity work
        // only; no guest/effect callback, await or reentry into this state.
        admit(&proof.metadata)
    }
    fn claim_dispatch(
        &self,
        cx: &CallCx,
        proof: &(dyn std::any::Any + Send + Sync),
        dispatch: FrameDispatch<'_>,
    ) -> Result<(), GateError> {
        let raw = allocation_proof(proof)?;
        let proof = self.validate(cx, proof, raw.metadata.reason)?;
        let _admission = self
            .auth
            .admission
            .lock()
            .map_err(|_| GateError::Unavailable)?;
        if self.auth.closing.load(Ordering::Acquire) {
            return Err(GateError::Unavailable);
        }
        let mut state = proof
            .node
            .state
            .lock()
            .map_err(|_| GateError::Unavailable)?;
        let witness = proof.node.witness.as_ref().ok_or(GateError::Unavailable)?;
        let target = state.expected_target.and_then(|i| witness.targets().get(i));
        if !state.live
            || state.claimed
            || !matches!(
                proof.node.kind,
                Some(FlowNodeKind::PortCall | FlowNodeKind::StreamEmit)
            )
            || witness.shape() != dispatch.view
            || witness.op() != dispatch.op
            || state.expected_args.as_ref() != Some(dispatch.args)
            || !target.is_some_and(|target| {
                target.pack == dispatch.target.pack
                    && target.hash == dispatch.target.hash
                    && target.provided == dispatch.target.provided
            })
        {
            return Err(denied(
                "final dispatch is not this live reached Core operation",
            ));
        }
        state.claimed = true;
        Ok(())
    }
    fn source_context<'a>(
        &self,
        proof: &'a (dyn std::any::Any + Send + Sync),
    ) -> Result<&'a CallCx, GateError> {
        let completed = proof
            .downcast_ref::<CompletedReadProof>()
            .ok_or(GateError::Unavailable)?;
        if !ControlHandle::ptr_eq(&self.auth, &completed.allocation.get().node.auth) {
            return Err(GateError::Unavailable);
        }
        Ok(&completed.original_input)
    }
    fn claim_source_read<'a>(
        &self,
        original: &CallCx,
        proof: &'a (dyn std::any::Any + Send + Sync),
        payload: &Digest,
    ) -> Result<&'a SourceReadMetadata, GateError> {
        let completed = proof
            .downcast_ref::<CompletedReadProof>()
            .ok_or(GateError::Unavailable)?;
        let allocation = self.validate(
            &completed.allocation.get().node.context,
            proof,
            AllocationReason::RuntimeValue,
        )?;
        check_same_context(&completed.original_input, original)?;
        let _admission = allocation
            .node
            .auth
            .admission
            .lock()
            .map_err(|_| GateError::Unavailable)?;
        if allocation.node.auth.closing.load(Ordering::Acquire) {
            return Err(GateError::Unavailable);
        }
        let mut state = allocation
            .node
            .state
            .lock()
            .map_err(|_| GateError::Unavailable)?;
        if !state.live
            || state.source_receipt_claimed
            || &completed.source.payload != payload
            || state.output_digest.as_ref() != Some(payload)
            || !state.acknowledged
            || !completed.acknowledged.succeeded
            || completed.acknowledged.grant != completed.source.read_grant
            || state
                .ack_evidence
                .as_ref()
                .is_none_or(|ack| !ack.succeeded || ack.grant != completed.source.read_grant)
            || completed.source.frame != allocation.metadata.frame
            || completed.source.activation != allocation.metadata.activation
            || completed.source.path != allocation.metadata.path
            || !matches!(
                &allocation.node.origin,
                ReachedOrigin::Provider(ProviderStateData {
                    native: Some(native),
                    grant,
                    ..
                }) if native.source == completed.source.source
                    && native.view == completed.source.view
                    && native.op == completed.source.op
                    && native.target == completed.source.target
                    && grant == &completed.source.read_grant
            )
        {
            return Err(denied(
                "native source completion is absent, stale or already claimed",
            ));
        }
        state.source_receipt_claimed = true;
        Ok(&completed.source)
    }
}

fn check_frozen_node(node: &ReachedState) -> Result<(), GateError> {
    match &node.origin {
        ReachedOrigin::Flow => check_frozen_flow_node(node),
        ReachedOrigin::NativeArguments(native) => check_native_arguments_node(node, native),
        ReachedOrigin::UiArguments(ui) => check_ui_arguments_node(node, ui),
        ReachedOrigin::UserArguments(user) => check_user_arguments_node(node, user),
        ReachedOrigin::Provider(provider) => check_provider_node(node, provider),
        ReachedOrigin::HandleSource(source) => check_handle_source_node(node, source),
    }
}

fn check_user_arguments_node(
    node: &ReachedState,
    user: &UserInvocationStateData,
) -> Result<(), GateError> {
    let compiled = node.auth.compiled.get();
    let CompiledCodeOwner::UserDocument { document } = &node.metadata.owner else {
        return Err(denied("User argument owner is not its registered document"));
    };
    let target = node.provider.get().gate.resolve_frozen_target(
        &node.entry_context,
        &user.view,
        &user.op,
    )?;
    if !same_context_pair(&node.context, &node.entry_context)
        || !matches!(node.context.principal(), Principal::User)
        || !matches!(node.entry_context.principal(), Principal::User)
        || node.entry_context.revision() != Some(&user.revision)
        || user.revision != compiled.plan_digest
        || node.context.security_epoch() != user.epoch
        || node.entry_context.security_epoch() != user.epoch
        || document != &user.document
        || target != user.target
        || node.entry_evidence.is_some()
        || node.code.is_some()
        || node.kind.is_some()
        || node.witness.is_some()
        || !node.flow_key.is_empty()
    {
        return Err(denied(
            "User argument node differs from its exact registered document and target",
        ));
    }
    node.provider.get().gate.code_owner_execution_limits(
        &node.entry_context,
        PreparedCodeOwner::UserDocument {
            document: &user.document,
        },
    )?;
    Ok(())
}

fn check_ui_arguments_node(
    node: &ReachedState,
    ui: &UiInvocationStateData,
) -> Result<(), GateError> {
    let compiled = node.auth.compiled.get();
    let Principal::PackUi {
        pack,
        hash,
        ui: principal_session,
    } = node.entry_context.principal()
    else {
        return Err(denied(
            "UI argument entry lost its registered PackUi principal",
        ));
    };
    let CompiledCodeOwner::Pack {
        pack: owner,
        hash: owner_hash,
    } = &node.metadata.owner
    else {
        return Err(denied(
            "UI argument owner is not the registered source Pack",
        ));
    };
    let target =
        node.provider
            .get()
            .gate
            .resolve_frozen_target(&node.entry_context, &ui.view, &ui.op)?;
    if !same_context_pair(&node.context, &node.entry_context)
        || node.entry_context.revision() != Some(&compiled.plan_digest)
        || principal_session != &ui.session
        || ui.generation == 0
        || ui.receiver != crate::session::ui_receiver_identity(&ui.session)
        || compiled.profile.startup.ui.as_ref() != Some(pack)
        || !compiled
            .profile
            .packs
            .iter()
            .any(|pin| pin.id == *pack && pin.sha256 == *hash)
        || owner != pack
        || owner_hash != hash
        || target != ui.target
        || node.entry_evidence.is_some()
        || node.code.is_some()
        || node.kind.is_some()
        || node.witness.is_some()
        || !node.flow_key.is_empty()
    {
        return Err(denied(
            "UI argument node differs from its exact registered receive and target",
        ));
    }
    Ok(())
}

fn check_handle_source_node(
    node: &ReachedState,
    source: &HandleSourceStateData,
) -> Result<(), GateError> {
    let Some(evidence) = node.entry_evidence.as_ref() else {
        return Err(denied(
            "handle source lacks its consumed source-open evidence",
        ));
    };
    let CompiledCodeOwner::Pack { pack, hash } = &node.metadata.owner else {
        return Err(denied("handle source owner is not a Pack"));
    };
    let expected_entry = match source.kind {
        tobkiri_wasm::HandleKind::Stream => tobkiri_gate::WasmEntryKind::StreamOpen {
            handle: source.local_id,
        },
        tobkiri_wasm::HandleKind::Callback => tobkiri_gate::WasmEntryKind::CallbackOpen {
            handle: source.local_id,
        },
    };
    if !matches!(
        evidence.effect(),
        EffectRequest::WasmEntry { pack: evidence_pack, hash: evidence_hash, entry }
            if evidence_pack == pack && evidence_hash == hash && entry == &expected_entry
    ) || !evidence.matches_context(&node.entry_context)
        || !matches!(node.context.principal(), Principal::Pack { pack: current, hash: current_hash } if current == pack && current_hash == hash)
        || !matches!(node.entry_context.principal(), Principal::Pack { pack: current, hash: current_hash } if current == pack && current_hash == hash)
        || node.context.revision() != Some(&node.auth.compiled.get().plan_digest)
        || source.generation == 0
        || node.code.is_some()
        || node.kind.is_some()
        || node.witness.is_some()
        || !node.flow_key.is_empty()
    {
        return Err(denied("handle source differs from its exact consumed open"));
    }
    Ok(())
}

fn check_native_arguments_node(
    node: &ReachedState,
    native: &NativeInputStateData,
) -> Result<(), GateError> {
    let compiled = node.auth.compiled.get();
    let entry = compiled
        .input_entries()
        .get(native.index)
        .ok_or_else(|| denied("native entry is absent from the frozen compiler catalog"))?;
    let tobkiri_gate::Principal::Input { source } = node.context.principal() else {
        return Err(denied(
            "native argument node lost its original Input principal",
        ));
    };
    let tobkiri_gate::Principal::Input {
        source: entry_source,
    } = node.entry_context.principal()
    else {
        return Err(denied("native argument node entry context is not Input"));
    };
    let CompiledCodeOwner::Pack { pack, hash } = &node.metadata.owner else {
        return Err(denied("native argument owner is not its exact source Pack"));
    };
    if source != &native.source
        || entry_source != &native.source
        || entry.source_pack() != pack
        || hash != entry.source_hash()
        || entry.exact_input_view() != &native.view
        || entry.operation() != &native.op
        || entry.profile_revision() != &compiled.plan_digest
        || entry.source_pack() != &native.target.pack
        || entry.source_hash() != &native.target.hash
        || entry.exact_input_view() != &native.view
        || entry.operation() != &native.op
        || node.entry_evidence.is_some()
    {
        return Err(denied(
            "native argument node differs from its exact frozen Input entry",
        ));
    }
    Ok(())
}

fn check_provider_node(node: &ReachedState, provider: &ProviderStateData) -> Result<(), GateError> {
    let Some(evidence) = node.entry_evidence.as_ref() else {
        return Err(denied("provider frame has no consumed-entry evidence"));
    };
    let CompiledCodeOwner::Pack { pack, hash } = &node.metadata.owner else {
        return Err(denied("provider frame owner is not a Pack"));
    };
    if pack != &provider.target.pack
        || hash != &provider.target.hash
        || !matches!(node.context.principal(), tobkiri_gate::Principal::Pack { pack: current, hash: current_hash } if current == pack && current_hash == hash)
        || !evidence.matches_context(&node.entry_context)
        || evidence.args_digest() != &provider.args
        || !matches!(evidence.effect(), EffectRequest::PortCall { shape, op } if shape == &provider.view && op == &provider.op)
        || node.code.is_some()
        || node.kind.is_some()
        || node.witness.is_some()
        || !node.flow_key.is_empty()
    {
        return Err(denied(
            "provider state differs from its exact consumed PortCall",
        ));
    }
    if let Some(native) = &provider.native {
        check_native_source_binding(node, native)?;
    }
    if let Some(ui) = &provider.ui {
        check_ui_provider_binding(node, provider, ui)?;
    }
    if let Some(user) = &provider.user {
        check_user_provider_binding(node, provider, user)?;
    }
    if (provider.native.is_some() as u8)
        + (provider.ui.is_some() as u8)
        + (provider.user.is_some() as u8)
        > 1
    {
        return Err(denied("provider has multiple private source origins"));
    }
    Ok(())
}

fn check_user_provider_binding(
    node: &ReachedState,
    provider: &ProviderStateData,
    user: &UserInvocationStateData,
) -> Result<(), GateError> {
    if !matches!(node.entry_context.principal(), Principal::User)
        || node.entry_context.revision() != Some(&user.revision)
        || node.entry_context.security_epoch() != user.epoch
        || user.revision != node.auth.compiled.get().plan_digest
        || user.view != provider.view
        || user.op != provider.op
        || user.target != provider.target
    {
        return Err(denied(
            "provider entry differs from its User invocation proof",
        ));
    }
    node.provider.get().gate.code_owner_execution_limits(
        &node.entry_context,
        PreparedCodeOwner::UserDocument {
            document: &user.document,
        },
    )?;
    Ok(())
}

/// Validate a dynamic Flow caller while its factory admission lock is already
/// held. The trait object is only a locator: authority comes from downcasting
/// to the private Core carrier and checking its original consumed PortCall,
/// exact context, and still-live activation.
fn validate_guest_provider_under_admission(
    auth: &ControlHandle<FactoryAuth>,
    provider_control: &ControlHandle<CoreProviderAuthority>,
    gate: &GateEngine,
    compiled: &CompiledProfile,
    provider: &dyn GuestFlowFrame,
    provider_frame: &CoreProviderFrame,
    caller: &CallCx,
) -> Result<(), GateError> {
    let actual = provider
        .as_any()
        .downcast_ref::<CoreProviderFrame>()
        .ok_or_else(|| denied("dynamic Flow caller is not a Core-owned provider frame"))?;
    if !ptr::eq(actual, provider_frame)
        || !ControlHandle::ptr_eq(auth, &provider_frame.state.auth)
        || !ControlHandle::ptr_eq(provider_control, &provider_frame.state.provider)
        || auth.closing.load(Ordering::Acquire)
    {
        return Err(denied(
            "dynamic Flow provider belongs to another or closed factory",
        ));
    }

    check_context(compiled, caller)?;
    gate.check_context(caller)?;
    let state = &provider_frame.state;
    if !same_context_pair(&state.context, caller)
        || !caller.cancellation().inherits(state.context.cancellation())
        || !caller
            .budget()
            .zip(state.context.budget())
            .is_some_and(|(current, bound)| current.inherits(bound))
    {
        return Err(denied(
            "dynamic Flow caller context is not the active provider context",
        ));
    }
    let CompiledCodeOwner::Pack { pack, hash } = &state.metadata.owner else {
        return Err(denied("dynamic Flow provider owner is not a Pack"));
    };
    let Principal::Pack {
        pack: current_pack,
        hash: current_hash,
    } = caller.principal()
    else {
        return Err(denied("dynamic Flow caller has no Pack principal"));
    };
    if current_pack != pack || current_hash != hash {
        return Err(denied(
            "dynamic Flow caller differs from the active component Pack",
        ));
    }
    let ReachedOrigin::Provider(provider_state) = &state.origin else {
        return Err(denied(
            "dynamic Flow caller is not an active provider invocation",
        ));
    };
    check_provider_node(state, provider_state)?;

    let lifecycle = state.state.lock().map_err(|_| GateError::Unavailable)?;
    if !lifecycle.live
        || !lifecycle.claimed
        || lifecycle.acknowledged
        || !lifecycle.compiled_prepared
        || lifecycle.expected_args.as_ref() != Some(&provider_state.args)
    {
        return Err(denied("dynamic Flow provider invocation is no longer live"));
    }
    Ok(())
}

fn validate_live_guest_provider(
    auth: &ControlHandle<FactoryAuth>,
    provider_control: &ControlHandle<CoreProviderAuthority>,
    gate: &GateEngine,
    compiled: &CompiledProfile,
    provider: &dyn GuestFlowFrame,
    provider_frame: &CoreProviderFrame,
    caller: &CallCx,
) -> Result<(), GateError> {
    let _admission = auth.admission.lock().map_err(|_| GateError::Unavailable)?;
    validate_guest_provider_under_admission(
        auth,
        provider_control,
        gate,
        compiled,
        provider,
        provider_frame,
        caller,
    )
}

fn check_ui_provider_binding(
    node: &ReachedState,
    provider: &ProviderStateData,
    ui: &UiInvocationStateData,
) -> Result<(), GateError> {
    let compiled = node.auth.compiled.get();
    let Principal::PackUi {
        pack: source_pack,
        hash: source_hash,
        ui: session,
    } = node.entry_context.principal()
    else {
        return Err(denied("provider UI origin lost its registered caller"));
    };
    if session != &ui.session
        || ui.receiver != crate::session::ui_receiver_identity(&ui.session)
        || ui.generation == 0
        || compiled.profile.startup.ui.as_ref() != Some(source_pack)
        || !compiled
            .profile
            .packs
            .iter()
            .any(|pin| pin.id == *source_pack && pin.sha256 == *source_hash)
        || ui.view != provider.view
        || ui.op != provider.op
        || ui.target != provider.target
    {
        return Err(denied("provider entry differs from the UI receive proof"));
    }
    Ok(())
}

fn check_native_source_binding(
    node: &ReachedState,
    native: &NativeInputStateData,
) -> Result<(), GateError> {
    let entry = node
        .auth
        .compiled
        .get()
        .input_entries()
        .get(native.index)
        .ok_or_else(|| denied("provider source entry is absent from frozen catalog"))?;
    let tobkiri_gate::Principal::Input { source } = node.entry_context.principal() else {
        return Err(denied(
            "provider source context is not the original Input principal",
        ));
    };
    if source != &native.source
        || entry.source_pack() != &native.target.pack
        || entry.source_hash() != &native.target.hash
        || entry.exact_input_view() != &native.view
        || entry.operation() != &native.op
        || entry.profile_revision() != &node.auth.compiled.get().plan_digest
    {
        return Err(denied(
            "provider source read lost its exact frozen Input binding",
        ));
    }
    Ok(())
}

fn check_frozen_flow_node(node: &ReachedState) -> Result<(), GateError> {
    let code = node
        .code
        .as_ref()
        .ok_or_else(|| denied("Flow activation has no actual code location"))?;
    let kind = node
        .kind
        .ok_or_else(|| denied("Flow activation has no actual node kind"))?;
    let flow = node
        .auth
        .compiled
        .get()
        .flows
        .get(node.flow_key.as_str())
        .ok_or_else(|| denied("node Flow key is absent from the frozen compiler catalog"))?;
    let implements = flow
        .document
        .implements
        .as_ref()
        .ok_or_else(|| denied("active Flow has no frozen implements endpoint"))?;
    let evidence = node
        .entry_evidence
        .as_ref()
        .ok_or_else(|| denied("active Flow lacks its consumed FlowRun evidence"))?;
    let target_digest = node.provider.get().with_flow_registration(|registration| {
        if registration.profile() != &node.auth.compiled.get().profile.id
            || registration.revision() != &node.auth.compiled.get().plan_digest
        {
            return Err(denied("Flow registration belongs to another revision"));
        }
        let target = registration
            .endpoints()
            .iter()
            .find(|target| target.flow_key == node.flow_key)
            .ok_or_else(|| denied("active Flow is absent from its registered endpoint catalog"))?;
        if target.profile != node.auth.compiled.get().profile.id
            || target.revision != node.auth.compiled.get().plan_digest
            || target.owner != flow.owner
            || target.owner_hash != flow.hash
            || target.flow_key != node.flow_key
            || target.plan_digest != flow.plan_digest
            || target.implements != *implements
            || target.input_view != flow.document.trigger.input
        {
            return Err(denied(
                "registered Flow endpoint differs from the canonical compiler entry",
            ));
        }
        target.digest()
    })?;
    if !matches!(
        evidence.effect(),
        EffectRequest::FlowRun {
            implements: granted_implements,
            flow_owner,
            flow_key,
            target_digest: granted_target,
        }
            if granted_implements == implements
                && flow_owner.0 == flow.owner
                && flow_owner.1 == flow.hash
                && flow_key.as_str() == node.flow_key.as_str()
                && granted_target == &target_digest
    ) {
        return Err(denied(
            "active Flow lacks exact FlowRun key and frozen-target custody",
        ));
    }

    // OverlayEntry is reached at the base Flow step that selected a particular
    // ordered overlay. Its step pointer therefore belongs to the base Flow AST,
    // while all later overlay nodes belong to the overlay body itself.
    let overlay = match (code, node.overlay_base_address, node.overlay_index) {
        (CodeLocation::Flow, None, None) => None,
        (CodeLocation::Flow, _, _) => {
            return Err(denied("base Flow location carries overlay provenance"));
        }
        (code, Some(base_address), Some(index)) => {
            let base = find_step(&flow.document.steps, base_address)
                .ok_or_else(|| denied("overlay base is not a frozen Flow step"))?;
            let id = base
                .id
                .as_deref()
                .ok_or_else(|| denied("overlay base has no compiler ID"))?;
            let selected = flow
                .ordered_overlays
                .get(id)
                .and_then(|items| items.get(index))
                .ok_or_else(|| denied("overlay is not at this frozen ordered position"))?;
            if !code_matches_overlay(code, selected) {
                return Err(denied(
                    "overlay code location does not match its compiler entry",
                ));
            }
            Some(selected)
        }
        (_, _, _) => return Err(denied("overlay location is missing exact transition data")),
    };
    let owner_matches = match (&node.metadata.owner, overlay) {
        (CompiledCodeOwner::Pack { pack, hash }, None) => pack == &flow.owner && hash == &flow.hash,
        (
            CompiledCodeOwner::Pack { pack, hash },
            Some(ResolvedOverlay {
                owner: Some(owner),
                hash: Some(owner_hash),
                ..
            }),
        ) => pack == owner && hash == owner_hash,
        (
            CompiledCodeOwner::User { profile_ast },
            Some(ResolvedOverlay {
                owner: None,
                hash: None,
                ast_hash,
                ..
            }),
        ) => profile_ast == ast_hash,
        _ => false,
    };
    if !owner_matches {
        return Err(denied(
            "reached code owner differs from the actual frozen AST",
        ));
    }

    let step = match (kind, node.step_address, overlay) {
        (FlowNodeKind::FlowEntry, None, None) => None,
        // FlowReturn is a trusted completion phase, not an AST node. The
        // interpreter supplies the last actual step it executed (or None for
        // an empty body). An overlay return retains its exact selected overlay
        // location even when that body is empty.
        (FlowNodeKind::FlowReturn, None, _) => None,
        (FlowNodeKind::EventFrame, None, None) => None,
        (FlowNodeKind::OverlayEntry, _, None) => {
            return Err(denied("overlay entry has no exact frozen overlay location"));
        }
        (FlowNodeKind::OverlayEntry, Some(address), Some(overlay)) => {
            let base = find_step(&flow.document.steps, address)
                .ok_or_else(|| denied("overlay entry is not attached to a frozen base step"))?;
            if node.overlay_base_address != Some(address) {
                return Err(denied("overlay entry moved away from its actual base step"));
            }
            let id = base
                .id
                .as_deref()
                .ok_or_else(|| denied("overlay base step has no frozen compiler ID"))?;
            let selected = flow
                .ordered_overlays
                .get(id)
                .and_then(|items| node.overlay_index.and_then(|index| items.get(index)));
            if !selected.is_some_and(|candidate| ptr::eq(candidate, overlay)) {
                return Err(denied("overlay entry is not the actual ordered transition"));
            }
            Some(base)
        }
        (FlowNodeKind::FlowEntry, _, _)
        | (FlowNodeKind::OverlayEntry, _, _)
        | (FlowNodeKind::EventFrame, None, Some(_)) => {
            return Err(denied("lifecycle node has an invalid frozen location"));
        }
        (_, Some(address), Some(overlay)) => Some(
            find_step(&overlay.document.body, address)
                .ok_or_else(|| denied("node AST pointer is not in its frozen overlay"))?,
        ),
        (_, Some(address), None) => Some(
            find_step(&flow.document.steps, address)
                .ok_or_else(|| denied("node AST pointer is not in its frozen Flow"))?,
        ),
        (_, None, _) => {
            return Err(denied(
                "step activation is missing its actual AST reference",
            ));
        }
    };

    let expected_witness = match (kind, step) {
        (FlowNodeKind::PortCall, Some(step)) => {
            let StepKind::Call(call) = &step.kind else {
                return Err(denied("port activation does not point at a Call step"));
            };
            Some((step, CallNodeKind::Call, &call.shape, Some(&call.op)))
        }
        (FlowNodeKind::StreamEmit, Some(step)) => {
            let route = step
                .stream
                .as_ref()
                .ok_or_else(|| denied("stream activation has no frozen output route"))?;
            Some((step, CallNodeKind::StreamEmit, &route.emit, None))
        }
        (FlowNodeKind::ProvidedFlow, Some(step)) => {
            if !matches!(&step.kind, StepKind::Flow(_)) {
                return Err(denied("provided-Flow activation is not at a Flow step"));
            }
            None
        }
        (FlowNodeKind::Step, Some(step)) => {
            if matches!(&step.kind, StepKind::Call(_) | StepKind::Flow(_)) {
                return Err(denied("generic step activation points at an effect step"));
            }
            None
        }
        (FlowNodeKind::EventFrame | FlowNodeKind::ResumeBase, Some(_)) => None,
        (FlowNodeKind::OverlayEntry, Some(step)) => {
            let _ = step;
            None
        }
        (FlowNodeKind::FlowReturn, Some(_)) => None,
        (FlowNodeKind::FlowEntry | FlowNodeKind::EventFrame, None) => None,
        _ => return Err(denied("activation kind and frozen AST location disagree")),
    };

    match (expected_witness, node.witness.as_ref()) {
        (Some((step, kind, shape, op)), Some(witness)) => {
            let id = step
                .id
                .as_deref()
                .ok_or_else(|| denied("compiled call step has no stable ID"))?;
            let key = witness.key();
            let actual = flow
                .call_witnesses
                .iter()
                .find(|(candidate, _)| {
                    candidate.code == *code && candidate.node == id && candidate.kind == kind
                })
                .map(|(_, value)| value)
                .ok_or_else(|| denied("compiled call witness is missing from the frozen Flow"))?;
            if !ptr::eq(actual, witness.get())
                || key.code != *code
                || key.node != id
                || key.kind != kind
                || witness.profile_revision() != &node.auth.compiled.get().plan_digest
                || witness.flow() != node.flow_key
                || overlay.is_some_and(|overlay| witness.ast_hash() != overlay.ast_hash())
                || witness.shape() != shape
                || op.is_some_and(|op| witness.op() != op)
                || (kind == CallNodeKind::StreamEmit && witness.op().as_str() != "emit")
                || !code_owner_matches(witness.owner(), flow, overlay)
            {
                return Err(denied(
                    "node witness is cloned or disagrees with its frozen AST",
                ));
            }
        }
        (None, None) => (),
        _ => {
            return Err(denied(
                "only a witnessed physical node may retain a descriptor",
            ));
        }
    }
    Ok(())
}
fn find_step(steps: &[Step], address: usize) -> Option<&Step> {
    for step in steps {
        if ptr::from_ref(step).addr() == address {
            return Some(step);
        }
        let found = match &step.kind {
            StepKind::Branch(branch) => {
                find_step(&branch.then, address).or_else(|| find_step(&branch.otherwise, address))
            }
            StepKind::Parallel(parallel) => find_step(&parallel.body, address),
            StepKind::Loop(loop_) => find_step(&loop_.body, address),
            _ => None,
        };
        if found.is_some() {
            return found;
        }
    }
    None
}

fn flow_step_at_actual_location<'a>(
    flow: &'a ResolvedFlow,
    code: Option<&CodeLocation>,
    overlay_base_address: Option<usize>,
    overlay_index: Option<usize>,
    address: usize,
) -> Option<&'a Step> {
    match (code, overlay_base_address, overlay_index) {
        (Some(CodeLocation::Flow), None, None) => find_step(&flow.document.steps, address),
        (Some(code), Some(base_address), Some(index))
            if matches!(
                code,
                CodeLocation::PackOverlay { .. } | CodeLocation::UserOverlay { .. }
            ) =>
        {
            let base = find_step(&flow.document.steps, base_address)?;
            let id = base.id.as_deref()?;
            let overlay = flow.ordered_overlays.get(id)?.get(index)?;
            if !code_matches_overlay(code, overlay) {
                return None;
            }
            find_step(&overlay.document.body, address)
        }
        _ => None,
    }
}

fn code_matches_overlay(code: &CodeLocation, overlay: &ResolvedOverlay) -> bool {
    match (code, &overlay.owner, &overlay.hash) {
        (CodeLocation::PackOverlay { id }, Some(owner), Some(_)) => {
            id.strip_prefix(owner.as_str())
                .and_then(|s| s.strip_prefix('/'))
                == Some(overlay.document.id.as_str())
        }
        (CodeLocation::UserOverlay { id }, None, None) => {
            id.strip_prefix("user:") == Some(overlay.document.id.as_str())
        }
        _ => false,
    }
}

fn code_owner_matches(
    actual: &CodeOwner,
    flow: &ResolvedFlow,
    overlay: Option<&ResolvedOverlay>,
) -> bool {
    match (actual, overlay) {
        (CodeOwner::Pack { id, hash }, None) => id == &flow.owner && hash == &flow.hash,
        (
            CodeOwner::Pack { id, hash },
            Some(ResolvedOverlay {
                owner: Some(owner),
                hash: Some(owner_hash),
                ..
            }),
        ) => id == owner && hash == owner_hash,
        (
            CodeOwner::User,
            Some(ResolvedOverlay {
                owner: None,
                hash: None,
                ..
            }),
        ) => true,
        _ => false,
    }
}

impl ActiveCodeFrame for ActiveFlowFrame {
    fn context(&self) -> &CallCx {
        &self.context
    }

    fn code_owner(&self) -> &CodeOwner {
        &self.owner
    }

    fn identity(&self) -> Digest {
        self.frame.clone()
    }

    fn reach_node(
        &self,
        cx: &CallCx,
        position: FlowNodePosition<'_>,
    ) -> Result<ReachedNode, StepError> {
        check_same_context(&self.context, cx).map_err(step)?;
        if self.auth.closing.load(Ordering::Acquire)
            || !ptr::eq(
                self.auth
                    .compiled
                    .get()
                    .flows
                    .get(self.flow_key.as_str())
                    .ok_or_else(|| step(denied("Flow key is not in the frozen catalog")))?,
                position.flow,
            )
            || position.code != &self.code
        {
            return Err(step(denied(
                "reached location differs from this actual frozen frame",
            )));
        }

        let flow = position.flow;
        let overlay = match (
            &position.code,
            self.overlay_base_address,
            self.overlay_index,
        ) {
            (CodeLocation::Flow, None, None) => None,
            (CodeLocation::Flow, _, _) => {
                return Err(step(denied(
                    "base Flow frame contains overlay transition data",
                )));
            }
            (_, Some(base_address), Some(index)) => {
                let base = find_step(&flow.document.steps, base_address)
                    .ok_or_else(|| step(denied("overlay base step is no longer frozen")))?;
                let id = base
                    .id
                    .as_deref()
                    .ok_or_else(|| step(denied("overlay base step has no compiler ID")))?;
                let selected = flow
                    .ordered_overlays
                    .get(id)
                    .and_then(|chain| chain.get(index))
                    .ok_or_else(|| step(denied("overlay index is outside the frozen chain")))?;
                if !code_matches_overlay(&position.code, selected)
                    || !code_owner_matches(&self.owner, flow, Some(selected))
                {
                    return Err(step(denied("overlay code owner or location changed")));
                }
                Some(selected)
            }
            _ => return Err(step(denied("overlay frame lacks exact chain provenance"))),
        };

        let step_address = position.step.map(|step| ptr::from_ref(step).addr());
        if let Some(actual_step) = position.step {
            let steps = match overlay {
                Some(overlay) => &overlay.document.body,
                None => &flow.document.steps,
            };
            if find_step(steps, ptr::from_ref(actual_step).addr())
                .is_none_or(|frozen| !ptr::eq(frozen, actual_step))
            {
                // OverlayEntry is anchored to the actual base step that chose
                // the wrapper, not a step in the wrapper body.
                if position.kind != FlowNodeKind::OverlayEntry
                    || overlay.is_none()
                    || find_step(&flow.document.steps, ptr::from_ref(actual_step).addr())
                        .is_none_or(|frozen| !ptr::eq(frozen, actual_step))
                {
                    return Err(step(denied("step pointer is not in its actual frozen AST")));
                }
            }
        }

        match (position.kind, position.step, position.witness) {
            (FlowNodeKind::FlowEntry | FlowNodeKind::EventFrame, None, None)
            | (FlowNodeKind::FlowReturn, _, None)
            | (FlowNodeKind::OverlayEntry, Some(_), None)
            | (FlowNodeKind::ResumeBase, Some(_), None)
            | (FlowNodeKind::Step, Some(_), None)
            | (FlowNodeKind::ProvidedFlow, Some(_), None)
            | (FlowNodeKind::PortCall | FlowNodeKind::StreamEmit, Some(_), Some(_)) => {}
            _ => return Err(step(denied("node kind and actual AST location disagree"))),
        }

        let actual_witness = match (position.kind, position.step) {
            (FlowNodeKind::PortCall, Some(actual_step)) => {
                let id = actual_step
                    .id
                    .as_deref()
                    .ok_or_else(|| step(denied("Call step has no stable compiler ID")))?;
                flow.call_witnesses.iter().find_map(|(key, witness)| {
                    (key.code.eq(position.code) && key.node == id && key.kind == CallNodeKind::Call)
                        .then_some(witness)
                })
            }
            (FlowNodeKind::StreamEmit, Some(actual_step)) => {
                let id = actual_step
                    .id
                    .as_deref()
                    .ok_or_else(|| step(denied("stream step has no stable compiler ID")))?;
                flow.call_witnesses.iter().find_map(|(key, witness)| {
                    (key.code.eq(position.code)
                        && key.node == id
                        && key.kind == CallNodeKind::StreamEmit)
                        .then_some(witness)
                })
            }
            _ => None,
        };
        if !option_ptr_eq(actual_witness, position.witness) {
            return Err(step(denied(
                "reached descriptor is not the exact frozen compiler witness",
            )));
        }

        let activation = next_sequence(&self.auth.activation).map_err(step)?;
        let node = if let Some(witness) = actual_witness {
            witness.id().clone()
        } else {
            identity(&[
                b"tobkiri.core-reached-node/1\0",
                self.frame.as_str().as_bytes(),
                &activation.to_le_bytes(),
                &[position.kind as u8],
                &step_address.unwrap_or(0).to_le_bytes(),
                code_location_bytes(position.code),
            ])
            .map_err(step)?
        };
        let path = identity(&[
            b"tobkiri.core-reached-path/1\0",
            self.path.as_str().as_bytes(),
            node.as_str().as_bytes(),
            &activation.to_le_bytes(),
        ])
        .map_err(step)?;
        let metadata_owner_bytes =
            compiled_owner_storage_bytes(&self.owner, overlay).map_err(step)?;
        let metadata_dynamic = self
            .context
            .profile()
            .clone_storage_bytes()
            .checked_add(
                self.context
                    .principal()
                    .clone_storage_bytes()
                    .map_err(|_| step(GateError::Unavailable))?,
            )
            .and_then(|bytes| bytes.checked_add(self.context.request().clone_storage_bytes()))
            .and_then(|bytes| bytes.checked_add(metadata_owner_bytes))
            .ok_or_else(|| step(GateError::Unavailable))?;
        let dynamic = self
            .flow_key
            .len()
            .checked_add(code_location_storage_bytes(position.code))
            .and_then(|bytes| bytes.checked_add(metadata_dynamic))
            .ok_or_else(|| step(GateError::Unavailable))?;
        let bytes = ControlHandle::<ReachedState>::storage_bytes::<ControlReservation>()
            .checked_add(dynamic)
            .and_then(|bytes| u64::try_from(bytes).ok())
            .ok_or_else(|| step(GateError::Unavailable))?;
        let reservation = self
            .context
            .control()
            .reserve(ControlKind::Frame, bytes)
            .map_err(|error| step(GateError::from(error)))?;
        let metadata = ActiveFrameMetadata {
            profile: self
                .context
                .profile()
                .try_clone_precharged()
                .map_err(|_| step(GateError::Unavailable))?,
            revision: self
                .context
                .revision()
                .ok_or_else(|| step(denied("frame has no frozen revision")))?
                .clone(),
            security_epoch: self.context.security_epoch(),
            principal: self
                .context
                .principal()
                .try_clone_precharged()
                .map_err(|_| step(GateError::Unavailable))?,
            request: self
                .context
                .request()
                .try_clone_precharged()
                .map_err(|_| step(GateError::Unavailable))?,
            frame: self.frame.clone(),
            node: node.clone(),
            activation,
            owner: compiled_owner(&self.owner, overlay).map_err(step)?,
            reason: AllocationReason::RuntimeValue,
            path: path.clone(),
            transitive_memory_bytes: None,
        };
        let witness = actual_witness
            .map(|witness| {
                self.auth
                    .compiled
                    .try_project(|catalog| {
                        catalog
                            .flows
                            .get(self.flow_key.as_str())
                            .and_then(|flow| {
                                flow.call_witnesses
                                    .get(witness.key())
                                    .filter(|current| ptr::eq(*current, witness))
                            })
                            .ok_or(GateError::Unavailable)
                    })
                    .map_err(step)
            })
            .transpose()?;

        let node = ControlHandle::try_new_precharged(
            ReachedState {
                auth: self.auth.clone(),
                provider: self.provider.clone(),
                context: self
                    .context
                    .try_clone_precharged()
                    .map_err(|_| step(GateError::Unavailable))?,
                entry_context: self
                    .entry_context
                    .try_clone_precharged()
                    .map_err(|_| step(GateError::Unavailable))?,
                entry_evidence: Some(self.entry_evidence.clone()),
                flow_key: try_owned_string(&self.flow_key)
                    .map_err(|_| step(GateError::Unavailable))?,
                code: Some(
                    clone_code_location(&position.code)
                        .map_err(|_| step(GateError::Unavailable))?,
                ),
                step_address,
                overlay_base_address: self.overlay_base_address,
                kind: Some(position.kind),
                witness: witness.clone(),
                overlay_index: self.overlay_index,
                activation,
                frame: self.frame.clone(),
                path: path.clone(),
                metadata,
                origin: ReachedOrigin::Flow,
                state: Mutex::new(NodeLifecycle {
                    live: true,
                    claimed: false,
                    expected_args: None,
                    expected_target: None,
                    provided_flow_key: None,
                    source_receipt_claimed: false,
                    compiled_prepared: false,
                    output_digest: None,
                    acknowledged: false,
                    stream_ready: false,
                    ack_evidence: None,
                    target_control: None,
                    args_control: None,
                }),
                retained_bytes: bytes,
            },
            reservation,
        )
        .map_err(|_| step(GateError::Unavailable))?;

        let proof_bytes =
            ControlHandle::<ReachedActivationProof>::storage_bytes::<ControlReservation>();
        let proof = funded_handle(
            self.context.control(),
            ControlKind::Frame,
            0,
            ReachedActivationProof { node: node.clone() },
        )
        .map_err(step)?;
        let _ = proof_bytes;
        let proof = proof.project(|proof| proof as &(dyn std::any::Any + Send + Sync));
        let retirement = funded_handle(
            self.context.control(),
            ControlKind::Frame,
            0,
            NodeExecutionGuard(node.clone()),
        )
        .map_err(step)?;
        let retirement = retirement.project(|guard| guard as &(dyn std::any::Any + Send + Sync));
        Ok(ReachedNode::from_prepaid(proof, retirement, witness))
    }

    fn budget_at_reached_node(
        &self,
        cx: &CallCx,
        node: &ReachedNode,
        reason: AllocationReason,
    ) -> Result<Arc<dyn AllocationBudget>, StepError> {
        check_same_context(&self.context, cx).map_err(step)?;
        let reached = node
            .core_proof::<ReachedActivationProof>()
            .ok_or_else(|| step(GateError::Unavailable))?;
        if !ControlHandle::ptr_eq(&self.auth, &reached.node.auth) {
            return Err(step(denied("reached node belongs to another factory")));
        }
        if reached.node.frame != self.frame {
            return Err(step(denied("reached node belongs to another frame")));
        }
        let proof = new_allocation_proof(self.context.control(), reached.node.clone(), reason)
            .map_err(step)?;
        let proof = proof.project(|proof| proof as &(dyn std::any::Any + Send + Sync));
        let scope = self
            .provider
            .with_registration(|registration| {
                self.gate
                    .mint_allocation_scope(cx, registration, proof, reason)
            })
            .map_err(step)?;
        self.invoker.allocation_budget_for_frame(cx, scope)
    }

    fn retire_node(&self, _node: &ReachedNode) {
        // The RunCx and any actual continuations own the one retirement guard.
        // Gate scopes retain only the immutable proof, so dropping the final
        // interpreter carrier synchronously closes this activation.
    }

    fn compiled_activation(
        &self,
        cx: &CallCx,
        node: &ReachedNode,
        arguments_digest: &Digest,
        target: &FrozenTarget,
    ) -> Result<CompiledActivation, StepError> {
        check_same_context(&self.context, cx).map_err(step)?;
        let reached = node
            .core_proof::<ReachedActivationProof>()
            .ok_or_else(|| step(GateError::Unavailable))?;
        let state = &reached.node;
        if !ControlHandle::ptr_eq(&self.auth, &state.auth)
            || state.frame != self.frame
            || !matches!(
                state.kind,
                Some(FlowNodeKind::PortCall | FlowNodeKind::StreamEmit)
            )
        {
            return Err(step(denied(
                "compiled activation is not an actual witnessed call",
            )));
        }
        check_frozen_node(state).map_err(step)?;
        let witness = state
            .witness
            .as_ref()
            .ok_or_else(|| step(GateError::Unavailable))?;
        let target_index = witness
            .targets()
            .iter()
            .position(|candidate| candidate == target)
            .ok_or_else(|| step(denied("target differs from exact frozen call site")))?;
        let target_bytes = frozen_target_storage_bytes(target)
            .checked_mul(4)
            .and_then(|bytes| bytes.checked_add(512))
            .and_then(|bytes| u64::try_from(bytes).ok())
            .ok_or_else(|| step(GateError::Unavailable))?;
        let target_control = self
            .context
            .control()
            .reserve(ControlKind::Frame, target_bytes)
            .map_err(|error| step(GateError::from(error)))?;
        let frozen_target = frozen_target_to_gate(target).map_err(step)?;
        {
            let _admission = self
                .auth
                .admission
                .lock()
                .map_err(|_| step(GateError::Unavailable))?;
            if self.auth.closing.load(Ordering::Acquire) {
                return Err(step(GateError::Unavailable));
            }
            let mut lifecycle = state
                .state
                .lock()
                .map_err(|_| step(GateError::Unavailable))?;
            if !lifecycle.live || lifecycle.claimed || lifecycle.compiled_prepared {
                return Err(step(denied("compiled activation node is not live")));
            }
            lifecycle.expected_args = Some(arguments_digest.clone());
            lifecycle.expected_target = Some(target_index);
            lifecycle.target_control = Some(target_control);
            lifecycle.compiled_prepared = true;
        }
        let proof = new_allocation_proof(
            self.context.control(),
            reached.node.clone(),
            AllocationReason::ArgumentDigest,
        )
        .map_err(step)?;
        let proof = proof.project(|proof| proof as &(dyn std::any::Any + Send + Sync));
        let scope = self
            .provider
            .with_registration(|registration| {
                let scope = self.gate.mint_allocation_scope(
                    cx,
                    registration,
                    proof,
                    AllocationReason::ArgumentDigest,
                )?;
                self.provider.with_compiled_registration(|authority| {
                    let execution = self.gate.begin_compiled_execution(cx, authority)?;
                    self.gate
                        .reach_compiled(cx, &execution, scope, witness.id(), frozen_target)
                })
            })
            .map_err(step)?;
        Ok(scope)
    }

    fn enter_verified_overlay(
        &self,
        cx: &CallCx,
        caller_node: &ReachedNode,
        position: OverlayPosition<'_>,
    ) -> Result<OverlayTransition, StepError> {
        check_same_context(&self.context, cx).map_err(step)?;
        let reached = caller_node
            .core_proof::<ReachedActivationProof>()
            .ok_or_else(|| step(GateError::Unavailable))?;
        let base_state = &reached.node;
        let flow = self
            .auth
            .compiled
            .get()
            .flows
            .get(self.flow_key.as_str())
            .ok_or_else(|| step(GateError::Unavailable))?;
        if !ControlHandle::ptr_eq(&self.auth, &base_state.auth)
            || !ptr::eq(position.flow, flow)
            || position.step.id.as_deref().is_none_or(|id| {
                !flow
                    .ordered_overlays
                    .get(id)
                    .and_then(|chain| chain.get(position.index))
                    .is_some_and(|candidate| ptr::eq(candidate, position.overlay))
            })
            || position.overlay.owner.as_ref().is_some() != position.overlay.hash.is_some()
            || base_state.code != Some(CodeLocation::Flow)
            || base_state.step_address != Some(ptr::from_ref(position.step).addr())
        {
            return Err(step(denied(
                "overlay is not this exact live ordered transition",
            )));
        }
        let _admission = self
            .auth
            .admission
            .lock()
            .map_err(|_| step(GateError::Unavailable))?;
        if self.auth.closing.load(Ordering::Acquire) {
            return Err(step(GateError::Unavailable));
        }
        let lifecycle = base_state
            .state
            .lock()
            .map_err(|_| step(GateError::Unavailable))?;
        if !lifecycle.live || lifecycle.claimed {
            return Err(step(denied("overlay caller activation is no longer live")));
        }
        drop(lifecycle);

        let overlay_id_len = position
            .overlay
            .owner
            .as_ref()
            .map_or(5, |owner| owner.as_str().len())
            .checked_add(position.overlay.document.id.len())
            .ok_or_else(|| step(GateError::Unavailable))?;
        let owner_dynamic = overlay_owner_storage_bytes(position.overlay)
            .map_err(|_| step(GateError::Unavailable))?;
        let frame_bytes = ControlHandle::<ActiveFlowFrame>::storage_bytes::<ControlReservation>()
            .checked_add(owner_dynamic)
            .and_then(|bytes| bytes.checked_add(overlay_id_len))
            .and_then(|bytes| u64::try_from(bytes).ok())
            .ok_or_else(|| step(GateError::Unavailable))?;
        let cont_bytes =
            ControlHandle::<CapturedBaseContinuation>::storage_bytes::<ControlReservation>();
        let frame_reservation = self
            .context
            .control()
            .reserve(ControlKind::Frame, frame_bytes)
            .map_err(|error| step(GateError::from(error)))?;
        let continuation_reservation = self
            .context
            .control()
            .reserve(
                ControlKind::Frame,
                u64::try_from(cont_bytes).map_err(|_| step(GateError::Unavailable))?,
            )
            .map_err(|error| step(GateError::from(error)))?;

        // This immutable copy preserves the exact base invocation. It is not a
        // new FlowRun, permission, budget or authority transition.
        let base_copy = self.clone_frame_precharged().map_err(step)?;
        let overlay_frame_id = identity(&[
            b"tobkiri.core-overlay-frame/1\0",
            self.frame.as_str().as_bytes(),
            &ptr::from_ref(position.step).addr().to_le_bytes(),
            &position.index.to_le_bytes(),
            position.overlay.ast_hash().as_str().as_bytes(),
        ])
        .map_err(step)?;
        let overlay_path = identity(&[
            b"tobkiri.core-overlay-path/1\0",
            self.path.as_str().as_bytes(),
            overlay_frame_id.as_str().as_bytes(),
            &position.index.to_le_bytes(),
        ])
        .map_err(step)?;
        let code =
            if let (Some(owner), Some(_hash)) = (&position.overlay.owner, &position.overlay.hash) {
                CodeLocation::PackOverlay {
                    id: try_join_overlay_id(owner.as_str(), &position.overlay.document.id)
                        .map_err(|_| step(GateError::Unavailable))?,
                }
            } else {
                CodeLocation::UserOverlay {
                    id: try_join_overlay_id("user:", &position.overlay.document.id)
                        .map_err(|_| step(GateError::Unavailable))?,
                }
            };
        let owner = overlay_code_owner(position.overlay).map_err(step)?;
        let overlay = ControlHandle::try_new_precharged(
            ActiveFlowFrame {
                auth: self.auth.clone(),
                provider: self.provider.clone(),
                gate: self.gate.clone(),
                invoker: self.invoker.clone(),
                context: self
                    .context
                    .try_clone_precharged()
                    .map_err(|_| step(GateError::Unavailable))?,
                entry_context: self
                    .entry_context
                    .try_clone_precharged()
                    .map_err(|_| step(GateError::Unavailable))?,
                entry_evidence: self.entry_evidence.clone(),
                flow_key: try_owned_string(&self.flow_key)
                    .map_err(|_| step(GateError::Unavailable))?,
                code,
                overlay_base_address: Some(ptr::from_ref(position.step).addr()),
                overlay_index: Some(position.index),
                owner,
                frame: overlay_frame_id.clone(),
                path: overlay_path.clone(),
                activation: next_sequence(&self.auth.activation).map_err(step)?,
                guest_parent: self.guest_parent.clone(),
            },
            frame_reservation,
        )
        .map_err(|_| step(GateError::Unavailable))?;
        let continuation = ControlHandle::try_new_precharged(
            CapturedBaseContinuation {
                base: base_copy.project(|frame| frame as &dyn ActiveCodeFrame),
                overlay_frame: overlay_frame_id,
                overlay_path,
                used: AtomicBool::new(false),
            },
            continuation_reservation,
        )
        .map_err(|_| step(GateError::Unavailable))?;
        let frame = overlay.project(|frame| frame as &dyn ActiveCodeFrame);
        Ok(OverlayTransition {
            frame,
            continuation: FrameContinuation::from_prepaid(
                continuation.project(|proof| proof as &(dyn std::any::Any + Send + Sync)),
            ),
        })
    }

    fn resume_captured_base(
        &self,
        cx: &CallCx,
        overlay_node: &ReachedNode,
        continuation: &FrameContinuation,
    ) -> Result<ControlHandle<dyn ActiveCodeFrame>, StepError> {
        check_same_context(&self.context, cx).map_err(step)?;
        let continuation = continuation
            .core_proof::<CapturedBaseContinuation>()
            .ok_or_else(|| step(GateError::Unavailable))?;
        let reached = overlay_node
            .core_proof::<ReachedActivationProof>()
            .ok_or_else(|| step(GateError::Unavailable))?;
        let state = &reached.node;
        let expected_path = identity(&[
            b"tobkiri.core-reached-path/1\0",
            continuation.overlay_path.as_str().as_bytes(),
            state.metadata.node.as_str().as_bytes(),
            &state.activation.to_le_bytes(),
        ])
        .map_err(step)?;
        if !ControlHandle::ptr_eq(&self.auth, &state.auth)
            || state.frame != continuation.overlay_frame
            || state.path != expected_path
        {
            return Err(step(denied(
                "Next continuation does not match this overlay",
            )));
        }
        let _admission = self
            .auth
            .admission
            .lock()
            .map_err(|_| step(GateError::Unavailable))?;
        if self.auth.closing.load(Ordering::Acquire) {
            return Err(step(GateError::Unavailable));
        }
        let lifecycle = state
            .state
            .lock()
            .map_err(|_| step(GateError::Unavailable))?;
        if !lifecycle.live {
            return Err(step(denied("overlay node has retired before Next")));
        }
        drop(lifecycle);
        if continuation.used.swap(true, Ordering::AcqRel) {
            return Err(step(denied(
                "captured base continuation was already resumed",
            )));
        }
        Ok(continuation.base.clone())
    }
}

impl ActiveFlowFrame {
    fn clone_frame_precharged(&self) -> Result<ControlHandle<Self>, GateError> {
        let dynamic = code_owner_storage_bytes(&self.owner)
            .checked_add(self.flow_key.len())
            .and_then(|bytes| bytes.checked_add(code_location_storage_bytes(&self.code)))
            .ok_or(GateError::Unavailable)?;
        let bytes = ControlHandle::<Self>::storage_bytes::<ControlReservation>()
            .checked_add(dynamic)
            .and_then(|bytes| u64::try_from(bytes).ok())
            .ok_or(GateError::Unavailable)?;
        let reservation = self.context.control().reserve(ControlKind::Frame, bytes)?;
        ControlHandle::try_new_precharged(
            Self {
                auth: self.auth.clone(),
                provider: self.provider.clone(),
                gate: self.gate.clone(),
                invoker: self.invoker.clone(),
                context: self
                    .context
                    .try_clone_precharged()
                    .map_err(|_| GateError::Unavailable)?,
                entry_context: self
                    .entry_context
                    .try_clone_precharged()
                    .map_err(|_| GateError::Unavailable)?,
                entry_evidence: self.entry_evidence.clone(),
                flow_key: try_owned_string(&self.flow_key).map_err(|_| GateError::Unavailable)?,
                code: clone_code_location(&self.code).map_err(|_| GateError::Unavailable)?,
                overlay_base_address: self.overlay_base_address,
                overlay_index: self.overlay_index,
                owner: clone_code_owner_precharged(&self.owner)
                    .map_err(|_| GateError::Unavailable)?,
                frame: self.frame.clone(),
                path: self.path.clone(),
                activation: self.activation,
                guest_parent: self.guest_parent.clone(),
            },
            reservation,
        )
        .map_err(|_| GateError::Unavailable)
    }
}

impl NativeInputPreparation for CoreNativeInputPreparation {
    fn argument_scope(&self) -> Result<AllocationFrameScope, StepError> {
        if self
            .frame
            .used
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .is_err()
        {
            return Err(step(denied("native argument scope was already issued")));
        }
        let state = &self.frame.state;
        let proof = match new_allocation_proof(
            state.context.control(),
            state.clone(),
            AllocationReason::RuntimeValue,
        ) {
            Ok(proof) => proof,
            Err(error) => {
                self.frame.used.store(false, Ordering::Release);
                return Err(step(error));
            }
        };
        let proof = proof.project(|proof| proof as &(dyn std::any::Any + Send + Sync));
        let result = state
            .provider
            .with_registration(|registration| {
                state.provider.get().gate.mint_allocation_scope(
                    &state.context,
                    registration,
                    proof,
                    AllocationReason::RuntimeValue,
                )
            })
            .map_err(step);
        if result.is_err() {
            self.frame.used.store(false, Ordering::Release);
        }
        result
    }
}

impl FlowUiPreparation for CoreUiInvocationPreparation {
    fn argument_scope(&self) -> Result<AllocationFrameScope, StepError> {
        issue_ui_argument_scope(self)
    }
}

impl FlowUserPreparation for CoreUserInvocationPreparation {
    fn argument_scope(&self) -> Result<AllocationFrameScope, StepError> {
        issue_user_argument_scope(self)
    }
}

fn issue_user_argument_scope(
    preparation: &CoreUserInvocationPreparation,
) -> Result<AllocationFrameScope, StepError> {
    if preparation
        .used
        .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
        .is_err()
    {
        return Err(step(denied("User argument scope was already issued")));
    }
    let state = &preparation.state;
    let auth = &state.auth;
    let prepared = (|| {
        let _admission = auth
            .admission
            .lock()
            .map_err(|_| step(GateError::Unavailable))?;
        if auth.closing.load(Ordering::Acquire) {
            return Err(step(GateError::Unavailable));
        }
        let mut lifecycle = state
            .state
            .lock()
            .map_err(|_| step(GateError::Unavailable))?;
        if !lifecycle.live || lifecycle.claimed || lifecycle.compiled_prepared {
            return Err(step(denied(
                "User argument activation is not fresh and live",
            )));
        }
        lifecycle.compiled_prepared = true;
        Ok(())
    })();
    if let Err(error) = prepared {
        preparation.used.store(false, Ordering::Release);
        return Err(error);
    }
    let proof = match new_allocation_proof(
        state.context.control(),
        state.clone(),
        AllocationReason::RuntimeValue,
    ) {
        Ok(proof) => proof,
        Err(error) => {
            reset_user_argument_phase(preparation);
            return Err(step(error));
        }
    };
    let proof = proof.project(|proof| proof as &(dyn std::any::Any + Send + Sync));
    let result = state
        .provider
        .with_registration(|registration| {
            state.provider.get().gate.mint_allocation_scope(
                &state.context,
                registration,
                proof,
                AllocationReason::RuntimeValue,
            )
        })
        .map_err(step);
    if result.is_err() {
        reset_user_argument_phase(preparation);
    }
    result
}

fn reset_user_argument_phase(preparation: &CoreUserInvocationPreparation) {
    let state = &preparation.state;
    if let Ok(_admission) = state.auth.admission.lock()
        && let Ok(mut lifecycle) = state.state.lock()
        && lifecycle.live
        && !lifecycle.claimed
    {
        lifecycle.compiled_prepared = false;
    }
    preparation.used.store(false, Ordering::Release);
}

fn issue_ui_argument_scope(
    preparation: &CoreUiInvocationPreparation,
) -> Result<AllocationFrameScope, StepError> {
    if preparation
        .used
        .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
        .is_err()
    {
        return Err(step(denied("UI argument scope was already issued")));
    }
    let state = &preparation.state;
    let auth = &state.auth;
    let prepared = (|| {
        let _admission = auth
            .admission
            .lock()
            .map_err(|_| step(GateError::Unavailable))?;
        if auth.closing.load(Ordering::Acquire) {
            return Err(step(GateError::Unavailable));
        }
        let mut lifecycle = state
            .state
            .lock()
            .map_err(|_| step(GateError::Unavailable))?;
        if !lifecycle.live || lifecycle.claimed || lifecycle.compiled_prepared {
            return Err(step(denied("UI argument activation is not fresh and live")));
        }
        lifecycle.compiled_prepared = true;
        Ok(())
    })();
    if let Err(error) = prepared {
        preparation.used.store(false, Ordering::Release);
        return Err(error);
    }
    let proof = match new_allocation_proof(
        state.context.control(),
        state.clone(),
        AllocationReason::RuntimeValue,
    ) {
        Ok(proof) => proof,
        Err(error) => {
            reset_ui_argument_phase(preparation);
            return Err(step(error));
        }
    };
    let proof = proof.project(|proof| proof as &(dyn std::any::Any + Send + Sync));
    let result = state
        .provider
        .with_registration(|registration| {
            state.provider.get().gate.mint_allocation_scope(
                &state.context,
                registration,
                proof,
                AllocationReason::RuntimeValue,
            )
        })
        .map_err(step);
    if result.is_err() {
        reset_ui_argument_phase(preparation);
    }
    result
}

fn reset_ui_argument_phase(preparation: &CoreUiInvocationPreparation) {
    let state = &preparation.state;
    if let Ok(_admission) = state.auth.admission.lock()
        && let Ok(mut lifecycle) = state.state.lock()
        && lifecycle.live
        && !lifecycle.claimed
    {
        lifecycle.compiled_prepared = false;
    }
    preparation.used.store(false, Ordering::Release);
}

impl Drop for CoreUiInvocationPreparation {
    fn drop(&mut self) {
        let state = &self.state;
        let removed = if let Ok(mut registry) = state.auth.ui_preparations.lock() {
            registry.remove(&(self as *const Self as usize))
        } else {
            None
        };
        drop(removed);
    }
}

impl Drop for CoreUserInvocationPreparation {
    fn drop(&mut self) {
        if let Ok(mut registry) = self.state.auth.user_preparations.lock() {
            registry.remove(&(self as *const Self as usize));
        }
    }
}

impl Drop for CoreNativeInputPreparation {
    fn drop(&mut self) {
        let state = &self.frame.state;
        if let Ok(mut registry) = state.auth.native_preparations.lock() {
            registry.remove(&(self as *const Self as usize));
        }
    }
}

impl ProviderFrame for CoreProviderFrame {
    fn context(&self) -> &CallCx {
        &self.state.context
    }

    fn allocation_scope(
        &self,
        reason: AllocationReason,
    ) -> Result<AllocationFrameScope, StepError> {
        let allowed = matches!(
            reason,
            AllocationReason::RuntimeValue
                | AllocationReason::ArgumentDigest
                | AllocationReason::OutputBuffer
                | AllocationReason::StreamBatch
                | AllocationReason::EventFrame
        );
        if !allowed {
            return Err(step(denied(
                "provider frame allocation reason is not supported",
            )));
        }
        let proof = new_allocation_proof(self.state.context.control(), self.state.clone(), reason)
            .map_err(step)?;
        let proof = proof.project(|proof| proof as &(dyn std::any::Any + Send + Sync));
        self.state
            .provider
            .with_registration(|registration| {
                self.state.provider.get().gate.mint_allocation_scope(
                    &self.state.context,
                    registration,
                    proof,
                    reason,
                )
            })
            .map_err(step)
    }

    fn output_ready(
        &self,
        result: &CoreValue,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<(), StepError> {
        let digest = result
            .digest_guest(budget)
            .map_err(|_| step(GateError::Unavailable))?;
        let auth = &self.state.auth;
        let _admission = auth
            .admission
            .lock()
            .map_err(|_| step(GateError::Unavailable))?;
        if auth.closing.load(Ordering::Acquire) {
            return Err(step(GateError::Unavailable));
        }
        let mut lifecycle = self
            .state
            .state
            .lock()
            .map_err(|_| step(GateError::Unavailable))?;
        if !matches!(&self.state.origin, ReachedOrigin::Provider(_))
            || !lifecycle.live
            || lifecycle.acknowledged
            || lifecycle.output_digest.is_some()
            || lifecycle.stream_ready
        {
            return Err(step(denied(
                "provider output was already sealed or node retired",
            )));
        }
        lifecycle.output_digest = Some(digest);
        Ok(())
    }

    fn stream_ready(&self) -> Result<(), StepError> {
        let auth = &self.state.auth;
        let _admission = auth
            .admission
            .lock()
            .map_err(|_| step(GateError::Unavailable))?;
        if auth.closing.load(Ordering::Acquire) {
            return Err(step(GateError::Unavailable));
        }
        let mut lifecycle = self
            .state
            .state
            .lock()
            .map_err(|_| step(GateError::Unavailable))?;
        if !matches!(
            &self.state.origin,
            ReachedOrigin::Provider(ProviderStateData { native: None, .. })
        ) || !lifecycle.live
            || lifecycle.acknowledged
            || lifecycle.output_digest.is_some()
            || lifecycle.stream_ready
        {
            return Err(step(denied("provider stream node is not live")));
        }
        lifecycle.stream_ready = true;
        Ok(())
    }

    fn terminal_acknowledged(&self, delivery: &AcknowledgedTerminal) -> Result<(), StepError> {
        let evidence = self
            .state
            .entry_evidence
            .as_ref()
            .ok_or_else(|| step(GateError::Unavailable))?;
        if delivery.grant_id() != evidence.grant() {
            return Err(step(denied("terminal receipt is not this provider grant")));
        }
        let succeeded = delivery.outcome() == &tobkiri_gate::Outcome::Succeeded
            && delivery.disposition() == &tobkiri_gate::TerminalDisposition::Completed;
        let native_success = succeeded
            && matches!(
                &self.state.origin,
                ReachedOrigin::Provider(ProviderStateData {
                    native: Some(_),
                    ..
                })
            );
        let grant = delivery
            .grant_id()
            .try_clone_precharged()
            .map_err(|_| step(GateError::Unavailable))?;
        let ack = funded_handle(
            self.state.context.control(),
            ControlKind::Frame,
            0,
            AcknowledgedEvidence { grant, succeeded },
        )
        .map_err(step)?;
        let auth = &self.state.auth;
        let _admission = auth
            .admission
            .lock()
            .map_err(|_| step(GateError::Unavailable))?;
        let mut lifecycle = self
            .state
            .state
            .lock()
            .map_err(|_| step(GateError::Unavailable))?;
        if lifecycle.acknowledged {
            let same_ack = lifecycle.ack_evidence.as_ref().is_some_and(|previous| {
                previous.grant == *delivery.grant_id() && previous.succeeded == succeeded
            });
            return if same_ack {
                Ok(())
            } else {
                Err(step(denied(
                    "provider terminal retry differs from its acknowledged winner",
                )))
            };
        }
        if !lifecycle.live {
            return Err(step(denied("provider terminal was already acknowledged")));
        }
        if succeeded {
            let output_ready = lifecycle.output_digest.is_some() || lifecycle.stream_ready;
            let source_cleanup = matches!(&self.state.origin, ReachedOrigin::HandleSource(_));
            if !output_ready && !source_cleanup {
                return Err(step(denied(
                    "successful provider terminal has no sealed output or cleaned source",
                )));
            }
        }
        lifecycle.acknowledged = true;
        lifecycle.ack_evidence = Some(ack);
        if !native_success {
            lifecycle.live = false;
        }
        let retire = !native_success;
        drop(lifecycle);
        drop(_admission);
        if retire {
            if let Ok(mut live) = auth.live_provider_frames.lock() {
                live.remove(&(self as *const Self as usize));
            }
        }
        Ok(())
    }

    fn take_source_read_receipt(&self) -> Result<tobkiri_gate::SourceReadReceipt, StepError> {
        let native = match &self.state.origin {
            ReachedOrigin::Provider(ProviderStateData {
                native: Some(native),
                ..
            }) => native,
            _ => return Err(step(denied("provider frame is not a NativeInput read"))),
        };
        let (digest, ack) = {
            let lifecycle = self
                .state
                .state
                .lock()
                .map_err(|_| step(GateError::Unavailable))?;
            if !lifecycle.live || !lifecycle.acknowledged || lifecycle.source_receipt_claimed {
                return Err(step(denied("source read is not acknowledged and live")));
            }
            (
                lifecycle
                    .output_digest
                    .as_ref()
                    .ok_or_else(|| step(denied("source output was not sealed")))?
                    .clone(),
                lifecycle
                    .ack_evidence
                    .as_ref()
                    .ok_or_else(|| step(denied("source terminal lacks successful Gate ACK")))?
                    .clone(),
            )
        };
        if !ack.succeeded {
            return Err(step(denied("source terminal did not succeed")));
        }
        let allocation = new_allocation_proof(
            self.state.context.control(),
            self.state.clone(),
            AllocationReason::RuntimeValue,
        )
        .map_err(step)?;
        let source_bytes = native_source_metadata_storage_bytes(native, &ack.grant)
            .ok_or_else(|| step(GateError::Unavailable))?;
        let receipt_bytes =
            ControlHandle::<CompletedReadProof>::storage_bytes::<ControlReservation>()
                .checked_add(source_bytes)
                .and_then(|bytes| u64::try_from(bytes).ok())
                .ok_or_else(|| step(GateError::Unavailable))?;
        let reservation = self
            .state
            .context
            .control()
            .reserve(ControlKind::Frame, receipt_bytes)
            .map_err(|error| step(GateError::from(error)))?;
        let source = SourceReadMetadata {
            source: native
                .source
                .try_clone_precharged()
                .map_err(|_| step(GateError::Unavailable))?,
            view: native
                .view
                .try_clone_precharged()
                .map_err(|_| step(GateError::Unavailable))?,
            op: native
                .op
                .try_clone_precharged()
                .map_err(|_| step(GateError::Unavailable))?,
            target: clone_gate_target(&native.target).map_err(|_| step(GateError::Unavailable))?,
            payload: digest.clone(),
            read_grant: ack
                .grant
                .try_clone_precharged()
                .map_err(|_| step(GateError::Unavailable))?,
            frame: self.state.frame.clone(),
            activation: self.state.activation,
            path: self.state.path.clone(),
        };
        let completed = ControlHandle::try_new_precharged(
            CompletedReadProof {
                allocation,
                original_input: self
                    .state
                    .entry_context
                    .try_clone_precharged()
                    .map_err(|_| step(GateError::Unavailable))?,
                source,
                acknowledged: ack,
            },
            reservation,
        )
        .map_err(|_| step(GateError::Unavailable))?;
        let proof = completed.project(|proof| proof as &(dyn std::any::Any + Send + Sync));
        let receipt = self
            .state
            .provider
            .with_registration(|registration| {
                self.state.provider.get().gate.mint_source_read_receipt(
                    &self.state.entry_context,
                    registration,
                    proof,
                    // The verifier rechecks and claims this same output under
                    // the node mutex. Reenter Gate only after the earlier
                    // lifecycle guard has dropped.
                    digest,
                )
            })
            .map_err(|error| step(GateError::from(error)))?;
        let auth = &self.state.auth;
        let _admission = auth
            .admission
            .lock()
            .map_err(|_| step(GateError::Unavailable))?;
        // `claim_source_read` already linearized under this same admission
        // lock and marked the one-shot receipt claimed. If Session close won
        // first, that claim failed; if the claim won first, finish returning
        // its immutable receipt even if close began before this final retire.
        let mut lifecycle = self
            .state
            .state
            .lock()
            .map_err(|_| step(GateError::Unavailable))?;
        if !lifecycle.live || !lifecycle.source_receipt_claimed {
            return Err(step(denied(
                "source receipt was not claimed against its live output",
            )));
        }
        lifecycle.live = false;
        drop(lifecycle);
        drop(_admission);
        if let Ok(mut live) = auth.live_provider_frames.lock() {
            live.remove(&(self as *const Self as usize));
        }
        Ok(receipt)
    }
}
impl Drop for CoreProviderFrame {
    fn drop(&mut self) {
        if let Ok(mut live) = self.state.auth.live_provider_frames.lock() {
            live.remove(&(self as *const Self as usize));
        }
    }
}

fn funded_handle<T: Send + Sync + 'static>(
    control: &CoreControl,
    kind: ControlKind,
    extra_bytes: usize,
    value: T,
) -> Result<ControlHandle<T>, GateError> {
    let bytes = ControlHandle::<T>::storage_bytes::<ControlReservation>()
        .checked_add(extra_bytes)
        .and_then(|bytes| u64::try_from(bytes).ok())
        .ok_or(GateError::Unavailable)?;
    let reservation = control.reserve(kind, bytes)?;
    ControlHandle::try_new_precharged(value, reservation).map_err(|_| GateError::Unavailable)
}

fn new_allocation_proof(
    control: &CoreControl,
    node: ControlHandle<ReachedState>,
    reason: AllocationReason,
) -> Result<ControlHandle<AllocationProof>, GateError> {
    let dynamic = node
        .metadata
        .clone_storage_bytes()
        .map_err(|_| GateError::Unavailable)?;
    let bytes = ControlHandle::<AllocationProof>::storage_bytes::<ControlReservation>()
        .checked_add(dynamic)
        .and_then(|bytes| u64::try_from(bytes).ok())
        .ok_or(GateError::Unavailable)?;
    let reservation = control.reserve(ControlKind::Frame, bytes)?;
    let mut metadata = node
        .metadata
        .try_clone_precharged()
        .map_err(|_| GateError::Unavailable)?;
    metadata.reason = reason;
    ControlHandle::try_new_precharged(AllocationProof { node, metadata }, reservation)
        .map_err(|_| GateError::Unavailable)
}

fn same_context_pair(left: &CallCx, right: &CallCx) -> bool {
    check_same_context(left, right).is_ok() && check_same_context(right, left).is_ok()
}

fn clone_gate_target(
    target: &tobkiri_gate::FrozenPortTarget,
) -> Result<tobkiri_gate::FrozenPortTarget, tobkiri_canon::AllocationError> {
    Ok(tobkiri_gate::FrozenPortTarget {
        pack: target.pack.try_clone_precharged()?,
        hash: target.hash.clone(),
        provided: target.provided.try_clone_precharged()?,
    })
}

fn gate_target_to_profile(
    target: &tobkiri_gate::FrozenPortTarget,
) -> Result<FrozenTarget, tobkiri_canon::AllocationError> {
    Ok(FrozenTarget {
        pack: target.pack.try_clone_precharged()?,
        hash: target.hash.clone(),
        provided: target.provided.try_clone_precharged()?,
    })
}

fn same_profile_gate_target(profile: &FrozenTarget, gate: &tobkiri_gate::FrozenPortTarget) -> bool {
    profile.pack == gate.pack && profile.hash == gate.hash && profile.provided == gate.provided
}

fn gate_target_storage_bytes(target: &tobkiri_gate::FrozenPortTarget) -> usize {
    target
        .pack
        .clone_storage_bytes()
        .saturating_add(target.hash.clone_storage_bytes())
        .saturating_add(target.provided.clone_storage_bytes())
}

fn native_input_data_storage_bytes(
    entry: &CompiledInputEntry,
    source: &tobkiri_canon::ids::InputSourceId,
    target: &tobkiri_gate::FrozenPortTarget,
) -> Option<usize> {
    std::mem::size_of::<NativeInputStateData>()
        .checked_add(source.clone_storage_bytes())
        .and_then(|bytes| bytes.checked_add(entry.exact_input_view().clone_storage_bytes()))
        .and_then(|bytes| bytes.checked_add(entry.operation().clone_storage_bytes()))
        .and_then(|bytes| bytes.checked_add(gate_target_storage_bytes(target)))
        .and_then(|bytes| bytes.checked_add(256))
}

fn native_input_data_precharged(
    entry: &CompiledInputEntry,
    source: &tobkiri_canon::ids::InputSourceId,
    target: &tobkiri_gate::FrozenPortTarget,
    index: usize,
    invocation: &Digest,
) -> Result<NativeInputStateData, GateError> {
    Ok(NativeInputStateData {
        index,
        invocation: invocation.clone(),
        source: source
            .try_clone_precharged()
            .map_err(|_| GateError::Unavailable)?,
        view: entry
            .exact_input_view()
            .try_clone_precharged()
            .map_err(|_| GateError::Unavailable)?,
        op: entry
            .operation()
            .try_clone_precharged()
            .map_err(|_| GateError::Unavailable)?,
        target: clone_gate_target(target).map_err(|_| GateError::Unavailable)?,
    })
}

fn ui_invocation_data_storage_bytes(
    entry: &UiInvocation<'_>,
    target: &tobkiri_gate::FrozenPortTarget,
) -> Option<usize> {
    std::mem::size_of::<UiInvocationStateData>()
        .checked_add(entry.session.clone_storage_bytes())
        .and_then(|bytes| bytes.checked_add(entry.view.clone_storage_bytes()))
        .and_then(|bytes| bytes.checked_add(entry.op.clone_storage_bytes()))
        .and_then(|bytes| bytes.checked_add(gate_target_storage_bytes(target)))
        .and_then(|bytes| bytes.checked_add(256))
}

fn ui_invocation_data_precharged(
    entry: &UiInvocation<'_>,
    target: &tobkiri_gate::FrozenPortTarget,
) -> Result<UiInvocationStateData, GateError> {
    Ok(UiInvocationStateData {
        invocation: entry.invocation.clone(),
        session: entry
            .session
            .try_clone_precharged()
            .map_err(|_| GateError::Unavailable)?,
        generation: entry.generation,
        receiver: entry.receiver.clone(),
        view: entry
            .view
            .try_clone_precharged()
            .map_err(|_| GateError::Unavailable)?,
        op: entry
            .op
            .try_clone_precharged()
            .map_err(|_| GateError::Unavailable)?,
        target: clone_gate_target(target).map_err(|_| GateError::Unavailable)?,
    })
}

fn clone_ui_invocation_data_precharged(
    data: &UiInvocationStateData,
) -> Result<UiInvocationStateData, GateError> {
    Ok(UiInvocationStateData {
        invocation: data.invocation.clone(),
        session: data
            .session
            .try_clone_precharged()
            .map_err(|_| GateError::Unavailable)?,
        generation: data.generation,
        receiver: data.receiver.clone(),
        view: data
            .view
            .try_clone_precharged()
            .map_err(|_| GateError::Unavailable)?,
        op: data
            .op
            .try_clone_precharged()
            .map_err(|_| GateError::Unavailable)?,
        target: clone_gate_target(&data.target).map_err(|_| GateError::Unavailable)?,
    })
}

fn ui_invocation_data_storage_bytes_from_data(data: &UiInvocationStateData) -> Option<usize> {
    std::mem::size_of::<UiInvocationStateData>()
        .checked_add(data.session.clone_storage_bytes())
        .and_then(|bytes| bytes.checked_add(data.view.clone_storage_bytes()))
        .and_then(|bytes| bytes.checked_add(data.op.clone_storage_bytes()))
        .and_then(|bytes| bytes.checked_add(gate_target_storage_bytes(&data.target)))
        .and_then(|bytes| bytes.checked_add(256))
}

fn user_invocation_data_storage_bytes(
    entry: &tobkiri_flow::FlowUserInvocation<'_>,
    target: &tobkiri_gate::FrozenPortTarget,
) -> Option<usize> {
    std::mem::size_of::<UserInvocationStateData>()
        .checked_add(entry.view.clone_storage_bytes())?
        .checked_add(entry.op.clone_storage_bytes())?
        .checked_add(gate_target_storage_bytes(target))?
        .checked_add(256)
}

fn user_invocation_data_precharged(
    entry: &tobkiri_flow::FlowUserInvocation<'_>,
    document: &Digest,
    revision: &Digest,
    epoch: u64,
    target: &tobkiri_gate::FrozenPortTarget,
) -> Result<UserInvocationStateData, GateError> {
    Ok(UserInvocationStateData {
        invocation: entry.invocation.clone(),
        document: document.clone(),
        revision: revision.clone(),
        epoch,
        view: entry
            .view
            .try_clone_precharged()
            .map_err(|_| GateError::Unavailable)?,
        op: entry
            .op
            .try_clone_precharged()
            .map_err(|_| GateError::Unavailable)?,
        target: clone_gate_target(target).map_err(|_| GateError::Unavailable)?,
    })
}

fn clone_user_invocation_data_precharged(
    data: &UserInvocationStateData,
) -> Result<UserInvocationStateData, GateError> {
    Ok(UserInvocationStateData {
        invocation: data.invocation.clone(),
        document: data.document.clone(),
        revision: data.revision.clone(),
        epoch: data.epoch,
        view: data
            .view
            .try_clone_precharged()
            .map_err(|_| GateError::Unavailable)?,
        op: data
            .op
            .try_clone_precharged()
            .map_err(|_| GateError::Unavailable)?,
        target: clone_gate_target(&data.target).map_err(|_| GateError::Unavailable)?,
    })
}

fn user_invocation_data_storage_bytes_from_data(data: &UserInvocationStateData) -> Option<usize> {
    std::mem::size_of::<UserInvocationStateData>()
        .checked_add(data.view.clone_storage_bytes())?
        .checked_add(data.op.clone_storage_bytes())?
        .checked_add(gate_target_storage_bytes(&data.target))?
        .checked_add(256)
}

fn clone_native_input_data_precharged(
    data: &NativeInputStateData,
) -> Result<NativeInputStateData, GateError> {
    Ok(NativeInputStateData {
        index: data.index,
        invocation: data.invocation.clone(),
        source: data
            .source
            .try_clone_precharged()
            .map_err(|_| GateError::Unavailable)?,
        view: data
            .view
            .try_clone_precharged()
            .map_err(|_| GateError::Unavailable)?,
        op: data
            .op
            .try_clone_precharged()
            .map_err(|_| GateError::Unavailable)?,
        target: clone_gate_target(&data.target).map_err(|_| GateError::Unavailable)?,
    })
}

fn provider_metadata_storage_bytes(
    caller: &CallCx,
    target: &tobkiri_gate::FrozenPortTarget,
    view: &ShapeRef,
    op: &tobkiri_canon::ids::OpName,
    native: Option<&NativeInputStateData>,
    ui: Option<&UiInvocationStateData>,
    user: Option<&UserInvocationStateData>,
) -> Option<usize> {
    let dynamic = caller
        .profile()
        .clone_storage_bytes()
        .checked_add(caller.principal().clone_storage_bytes().ok()?)?
        .checked_add(caller.request().clone_storage_bytes())?
        .checked_add(target.pack.clone_storage_bytes())?
        .checked_add(view.clone_storage_bytes())?
        .checked_add(op.clone_storage_bytes())?
        .checked_add(gate_target_storage_bytes(target))?
        .checked_add(native.map_or(0, |data| {
            native_input_data_storage_bytes_from_data(data).unwrap_or(usize::MAX)
        }))?
        .checked_add(ui.map_or(0, |data| {
            ui_invocation_data_storage_bytes_from_data(data).unwrap_or(usize::MAX)
        }))?
        .checked_add(user.map_or(0, |data| {
            user_invocation_data_storage_bytes_from_data(data).unwrap_or(usize::MAX)
        }))?;
    Some(dynamic)
}

fn native_input_data_storage_bytes_from_data(data: &NativeInputStateData) -> Option<usize> {
    std::mem::size_of::<NativeInputStateData>()
        .checked_add(data.source.clone_storage_bytes())?
        .checked_add(data.view.clone_storage_bytes())?
        .checked_add(data.op.clone_storage_bytes())?
        .checked_add(gate_target_storage_bytes(&data.target))?
        .checked_add(256)
}

fn native_source_metadata_storage_bytes(
    native: &NativeInputStateData,
    grant: &tobkiri_gate::GrantId,
) -> Option<usize> {
    std::mem::size_of::<SourceReadMetadata>()
        .checked_add(native.source.clone_storage_bytes())?
        .checked_add(native.view.clone_storage_bytes())?
        .checked_add(native.op.clone_storage_bytes())?
        .checked_add(gate_target_storage_bytes(&native.target))?
        .checked_add(grant.clone_storage_bytes())?
        .checked_add(128)
}

fn try_owned_string(value: &str) -> Result<String, std::collections::TryReserveError> {
    let mut owned = String::new();
    owned.try_reserve_exact(value.len())?;
    owned.push_str(value);
    Ok(owned)
}

fn try_join_overlay_id(
    prefix: &str,
    id: &str,
) -> Result<String, std::collections::TryReserveError> {
    let mut value = String::new();
    value.try_reserve_exact(prefix.len().saturating_add(id.len()).saturating_add(1))?;
    value.push_str(prefix);
    if !prefix.ends_with(':') {
        value.push('/');
    }
    value.push_str(id);
    Ok(value)
}

fn clone_code_location(
    code: &CodeLocation,
) -> Result<CodeLocation, std::collections::TryReserveError> {
    Ok(match code {
        CodeLocation::Flow => CodeLocation::Flow,
        CodeLocation::PackOverlay { id } => CodeLocation::PackOverlay {
            id: try_owned_string(id)?,
        },
        CodeLocation::UserOverlay { id } => CodeLocation::UserOverlay {
            id: try_owned_string(id)?,
        },
    })
}

fn code_location_storage_bytes(code: &CodeLocation) -> usize {
    match code {
        CodeLocation::Flow => 0,
        CodeLocation::PackOverlay { id } | CodeLocation::UserOverlay { id } => id.len(),
    }
}

fn code_location_bytes(code: &CodeLocation) -> &[u8] {
    match code {
        CodeLocation::Flow => b"flow",
        CodeLocation::PackOverlay { id } => id.as_bytes(),
        CodeLocation::UserOverlay { id } => id.as_bytes(),
    }
}

fn code_owner_pack_storage_bytes(pack: &PackId, hash: &PackHash) -> usize {
    pack.clone_storage_bytes()
        .saturating_add(hash.clone_storage_bytes())
}

fn code_owner_storage_bytes(owner: &CodeOwner) -> usize {
    match owner {
        CodeOwner::Pack { id, hash } => code_owner_pack_storage_bytes(id, hash),
        CodeOwner::User => 0,
    }
}

fn clone_code_owner_precharged(
    owner: &CodeOwner,
) -> Result<CodeOwner, tobkiri_canon::AllocationError> {
    Ok(match owner {
        CodeOwner::Pack { id, hash } => CodeOwner::Pack {
            id: id.try_clone_precharged()?,
            hash: hash.try_clone_precharged()?,
        },
        CodeOwner::User => CodeOwner::User,
    })
}

fn clone_pack_owner_precharged(
    id: &PackId,
    hash: &PackHash,
) -> Result<CodeOwner, tobkiri_canon::AllocationError> {
    Ok(CodeOwner::Pack {
        id: id.try_clone_precharged()?,
        hash: hash.try_clone_precharged()?,
    })
}

fn overlay_owner_storage_bytes(overlay: &ResolvedOverlay) -> Result<usize, GateError> {
    let code_id = match (&overlay.owner, &overlay.hash) {
        (Some(owner), Some(_)) => owner
            .as_str()
            .len()
            .checked_add(1)
            .and_then(|bytes| bytes.checked_add(overlay.document.id.len()))
            .ok_or(GateError::Unavailable)?,
        (None, None) => "user:"
            .len()
            .checked_add(overlay.document.id.len())
            .ok_or(GateError::Unavailable)?,
        _ => return Err(GateError::Unavailable),
    };
    let owner = match (&overlay.owner, &overlay.hash) {
        (Some(owner), Some(hash)) => code_owner_pack_storage_bytes(owner, hash),
        (None, None) => 0,
        _ => return Err(GateError::Unavailable),
    };
    code_id.checked_add(owner).ok_or(GateError::Unavailable)
}

fn overlay_code_owner(overlay: &ResolvedOverlay) -> Result<CodeOwner, GateError> {
    match (&overlay.owner, &overlay.hash) {
        (Some(owner), Some(hash)) => Ok(CodeOwner::Pack {
            id: owner
                .try_clone_precharged()
                .map_err(|_| GateError::Unavailable)?,
            hash: hash
                .try_clone_precharged()
                .map_err(|_| GateError::Unavailable)?,
        }),
        (None, None) => Ok(CodeOwner::User),
        _ => Err(GateError::Unavailable),
    }
}

fn compiled_owner_storage_bytes(
    owner: &CodeOwner,
    overlay: Option<&ResolvedOverlay>,
) -> Result<usize, GateError> {
    match (owner, overlay) {
        (CodeOwner::Pack { id, hash }, None) => Ok(code_owner_pack_storage_bytes(id, hash)),
        (
            CodeOwner::Pack { .. },
            Some(ResolvedOverlay {
                owner: Some(id),
                hash: Some(hash),
                ..
            }),
        ) => Ok(code_owner_pack_storage_bytes(id, hash)),
        (
            CodeOwner::User,
            Some(ResolvedOverlay {
                owner: None,
                hash: None,
                ..
            }),
        ) => Ok(0),
        _ => Err(GateError::Unavailable),
    }
}

fn compiled_owner(
    owner: &CodeOwner,
    overlay: Option<&ResolvedOverlay>,
) -> Result<CompiledCodeOwner, GateError> {
    match (owner, overlay) {
        (CodeOwner::Pack { id, hash }, None) => Ok(CompiledCodeOwner::Pack {
            pack: id
                .try_clone_precharged()
                .map_err(|_| GateError::Unavailable)?,
            hash: hash
                .try_clone_precharged()
                .map_err(|_| GateError::Unavailable)?,
        }),
        (
            CodeOwner::Pack { .. },
            Some(ResolvedOverlay {
                owner: Some(id),
                hash: Some(hash),
                ..
            }),
        ) => Ok(CompiledCodeOwner::Pack {
            pack: id
                .try_clone_precharged()
                .map_err(|_| GateError::Unavailable)?,
            hash: hash
                .try_clone_precharged()
                .map_err(|_| GateError::Unavailable)?,
        }),
        (
            CodeOwner::User,
            Some(
                overlay @ ResolvedOverlay {
                    owner: None,
                    hash: None,
                    ..
                },
            ),
        ) => Ok(CompiledCodeOwner::User {
            profile_ast: overlay.ast_hash().clone(),
        }),
        _ => Err(GateError::Unavailable),
    }
}

fn frozen_target_storage_bytes(target: &FrozenTarget) -> usize {
    target
        .pack
        .clone_storage_bytes()
        .saturating_add(target.hash.clone_storage_bytes())
        .saturating_add(target.provided.clone_storage_bytes())
}

fn frozen_target_to_gate(
    target: &FrozenTarget,
) -> Result<tobkiri_gate::FrozenPortTarget, GateError> {
    Ok(tobkiri_gate::FrozenPortTarget {
        pack: target
            .pack
            .try_clone_precharged()
            .map_err(|_| GateError::Unavailable)?,
        hash: target
            .hash
            .try_clone_precharged()
            .map_err(|_| GateError::Unavailable)?,
        provided: target
            .provided
            .try_clone_precharged()
            .map_err(|_| GateError::Unavailable)?,
    })
}

fn option_ptr_eq<T>(left: Option<&T>, right: Option<&T>) -> bool {
    match (left, right) {
        (Some(left), Some(right)) => ptr::eq(left, right),
        (None, None) => true,
        _ => false,
    }
}

fn flow_key_digest(key: &str) -> Result<Digest, GateError> {
    identity(&[b"tobkiri.flow-key/1\0", key.as_bytes()])
}

fn descriptor_build_bytes(compiled: &CompiledProfile) -> Result<usize, GateError> {
    let count = compiled
        .flows
        .values()
        .try_fold(0usize, |total, flow| {
            total.checked_add(flow.call_witnesses.len())
        })
        .ok_or(GateError::Unavailable)?;
    if count > 65_536 {
        return Err(GateError::Limit("compiled descriptor count".into()));
    }
    let mut bytes = std::mem::size_of::<BTreeMap<Digest, CompiledCallDescriptor>>()
        .checked_add(count.checked_mul(256).ok_or(GateError::Unavailable)?)
        .ok_or(GateError::Unavailable)?;
    for (flow_key, flow) in &compiled.flows {
        for witness in flow.call_witnesses.values() {
            let owner_bytes = match witness.owner() {
                CodeOwner::Pack { id, .. } => id.clone_storage_bytes(),
                CodeOwner::User => 0,
            };
            let mut item = std::mem::size_of::<(Digest, CompiledCallDescriptor)>()
                .checked_add(owner_bytes)
                .and_then(|n| n.checked_add(flow_key.len()))
                .and_then(|n| n.checked_add(witness.key().node.len()))
                .and_then(|n| n.checked_add(witness.shape().clone_storage_bytes()))
                .and_then(|n| n.checked_add(witness.op().clone_storage_bytes()))
                .and_then(|n| n.checked_add(128))
                .ok_or(GateError::Unavailable)?;
            for target in witness.targets() {
                item = item
                    .checked_add(std::mem::size_of::<tobkiri_gate::FrozenPortTarget>())
                    .and_then(|n| n.checked_add(target.pack.clone_storage_bytes()))
                    .and_then(|n| n.checked_add(target.provided.clone_storage_bytes()))
                    .and_then(|n| n.checked_add(64))
                    .ok_or(GateError::Unavailable)?;
            }
            bytes = bytes.checked_add(item).ok_or(GateError::Unavailable)?;
        }
    }
    Ok(bytes)
}

fn build_compiled_descriptors(
    compiled: &CompiledProfile,
) -> Result<BTreeMap<Digest, CompiledCallDescriptor>, GateError> {
    let mut descriptors = BTreeMap::new();
    for (flow_key, flow) in &compiled.flows {
        for (key, witness) in &flow.call_witnesses {
            let owner = match witness.owner() {
                CodeOwner::Pack { id, hash } => CompiledCodeOwner::Pack {
                    pack: id.clone(),
                    hash: hash.clone(),
                },
                CodeOwner::User => CompiledCodeOwner::User {
                    profile_ast: witness.ast_hash().clone(),
                },
            };
            let kind = match key.kind {
                CallNodeKind::Call => CompiledNodeKind::StepCall,
                CallNodeKind::StreamEmit => CompiledNodeKind::FixedStreamEmit,
            };
            let targets = witness
                .targets()
                .iter()
                .map(|target| tobkiri_gate::FrozenPortTarget {
                    pack: target.pack.clone(),
                    hash: target.hash.clone(),
                    provided: target.provided.clone(),
                })
                .collect::<BTreeSet<_>>();
            let descriptor = CompiledCallDescriptor {
                id: witness.id().clone(),
                ast_hash: witness.ast_hash().clone(),
                owner,
                revision: witness.profile_revision().clone(),
                flow: flow_key.clone(),
                node: key.node.clone(),
                kind,
                shape: witness.shape().clone(),
                op: witness.op().clone(),
                targets,
            };
            if descriptors
                .insert(witness.id().clone(), descriptor)
                .is_some()
            {
                return Err(denied("duplicate compiled witness identity"));
            }
        }
    }
    Ok(descriptors)
}

/// Register the exact manifest Flow catalog before input routes or execution
/// admission can refer to it. Endpoints come only from the one canonical
/// compiler result; they are independent of ordinary `ports.provides` and
/// carry no FlowRun authority.
pub(crate) fn register_flow_endpoints(
    compiled: &CompiledProfile,
    gate: &GateEngine,
) -> Result<FlowEndpointRegistration, CoreError> {
    if compiled.flows.len() > 65_536 {
        return Err(CoreError::Gate(GateError::Limit(
            "frozen Flow endpoint count".into(),
        )));
    }
    let temporary_bytes = flow_endpoint_build_bytes(compiled)?;
    let _temporary = gate.control().reserve(
        ControlKind::Catalog,
        u64::try_from(temporary_bytes).map_err(|_| GateError::Unavailable)?,
    )?;
    let mut endpoints = Vec::new();
    endpoints
        .try_reserve_exact(compiled.flows.len())
        .map_err(|_| GateError::Unavailable)?;
    for (key, flow) in &compiled.flows {
        let implements = flow
            .document
            .implements
            .as_ref()
            .ok_or_else(|| CoreError::Gate(denied("frozen Flow has no implements endpoint")))?;
        let input_view = &flow.document.trigger.input;
        let view = compiled.views.get(input_view).ok_or_else(|| {
            CoreError::Gate(denied("Flow trigger input has no exact verified ShapeDef"))
        })?;
        if key.is_empty()
            || key.len() > MAX_FLOW_KEY
            || !compiled
                .profile
                .packs
                .iter()
                .any(|pin| pin.id == flow.owner && pin.sha256 == flow.hash)
            || key_for_flow(compiled, flow) != Some(key.as_str())
        {
            return Err(CoreError::Gate(denied(
                "Flow endpoint differs from its canonical pinned catalog entry",
            )));
        }
        let ast = crate::profiles::strict_bytes(&flow.document)?;
        let flow_ast = Digest::hash(&ast);
        let signature = crate::profiles::strict_bytes(&(
            "tobkiri-flow-entry-signature-v1",
            input_view,
            &view.wit,
        ))?;
        endpoints.push(FrozenFlowTarget {
            profile: compiled
                .profile
                .id
                .try_clone_precharged()
                .map_err(|_| GateError::Unavailable)?,
            revision: compiled.plan_digest.clone(),
            owner: flow
                .owner
                .try_clone_precharged()
                .map_err(|_| GateError::Unavailable)?,
            owner_hash: flow.hash.clone(),
            flow_key: try_owned_string(key).map_err(|_| GateError::Unavailable)?,
            flow_ast,
            plan_digest: flow.plan_digest.clone(),
            implements: implements
                .try_clone_precharged()
                .map_err(|_| GateError::Unavailable)?,
            input_view: input_view
                .try_clone_precharged()
                .map_err(|_| GateError::Unavailable)?,
            entry_signature: Digest::hash(&signature),
        });
    }
    Ok(gate.register_flow_endpoints(&compiled.profile.id, &compiled.plan_digest, endpoints)?)
}

/// Count serialized source without materializing it, then reserve a generous
/// bound before strict canonicalization builds the temporary tree and bytes.
/// This is native Core catalog bookkeeping, not guest allocation authority.
fn flow_endpoint_build_bytes(compiled: &CompiledProfile) -> Result<usize, CoreError> {
    // An empty Flow catalog is valid for a utility-only Profile. Its
    // temporary Vec still has fixed native control metadata; do not submit a
    // zero-byte reservation (which CoreControl correctly rejects).
    let mut total = std::mem::size_of::<FrozenFlowTarget>()
        .checked_mul(compiled.flows.len())
        .and_then(|bytes| bytes.checked_add(std::mem::size_of::<Vec<FrozenFlowTarget>>()))
        .ok_or(GateError::Unavailable)?;
    for (key, flow) in &compiled.flows {
        let mut flow_count = JsonByteCount::default();
        serde_json::to_writer(&mut flow_count, &flow.document)?;
        let view = compiled
            .views
            .get(&flow.document.trigger.input)
            .ok_or_else(|| CoreError::Gate(denied("Flow input view is not verified")))?;
        let mut signature_count = JsonByteCount::default();
        serde_json::to_writer(
            &mut signature_count,
            &(
                "tobkiri-flow-entry-signature-v1",
                &flow.document.trigger.input,
                &view.wit,
            ),
        )?;
        let source_bytes = flow_count
            .0
            .checked_add(signature_count.0)
            .and_then(|bytes| bytes.checked_add(key.len()))
            .and_then(|bytes| bytes.checked_add(flow.owner.clone_storage_bytes()))
            .and_then(|bytes| bytes.checked_add(flow.document.trigger.input.clone_storage_bytes()))
            .and_then(|bytes| {
                bytes.checked_add(flow.document.implements.as_ref()?.clone_storage_bytes())
            })
            .ok_or(GateError::Unavailable)?;
        total = total
            .checked_add(source_bytes.checked_mul(32).ok_or(GateError::Unavailable)?)
            .ok_or(GateError::Unavailable)?;
    }
    Ok(total)
}

#[derive(Default)]
struct JsonByteCount(usize);
impl std::io::Write for JsonByteCount {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        self.0 = self
            .0
            .checked_add(bytes.len())
            .ok_or_else(|| std::io::Error::new(std::io::ErrorKind::Other, "JSON size overflow"))?;
        Ok(bytes.len())
    }

    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

fn build_provider_authority(
    compiled: Arc<CompiledProfile>,
    gate: Arc<GateEngine>,
    identity: Digest,
) -> Result<ControlHandle<CoreProviderAuthority>, GateError> {
    let control = gate.control().clone();
    let catalog = funded_handle(&control, ControlKind::Catalog, 0, compiled.clone())?;
    let auth = funded_handle(
        &control,
        ControlKind::Registration,
        0,
        FactoryAuth {
            identity: identity.clone(),
            compiled: catalog,
            closing: AtomicBool::new(false),
            activation: AtomicU64::new(1),
            admission: Mutex::new(()),
            native_preparations: Mutex::new(BTreeMap::new()),
            ui_preparations: Mutex::new(BTreeMap::new()),
            user_preparations: Mutex::new(BTreeMap::new()),
            live_provider_frames: Mutex::new(BTreeMap::new()),
        },
    )?;

    // Temporary descriptor storage is bounded before constructing the map.
    // Gate takes its own retained reservation when publishing the exact
    // factory-bound authority; this temporary reservation covers preparation.
    let build_reservation = control.reserve(
        ControlKind::Catalog,
        u64::try_from(descriptor_build_bytes(compiled.as_ref())?)
            .map_err(|_| GateError::Unavailable)?,
    )?;
    let descriptors = build_compiled_descriptors(compiled.as_ref())?;
    let calls = gate.register_compiled_calls(
        &identity,
        &compiled.profile.id,
        &compiled.plan_digest,
        descriptors,
    )?;
    drop(build_reservation);
    let flow_endpoints =
        register_flow_endpoints(compiled.as_ref(), &gate).map_err(|error| match error {
            CoreError::Gate(error) => error,
            _ => GateError::Unavailable,
        })?;

    let verifier = funded_handle(
        &control,
        ControlKind::Registration,
        0,
        CoreVerifier { auth: auth.clone() },
    )?;
    let verifier = verifier.project(|verifier| verifier as &dyn ActiveFrameVerifier);
    let registration_bytes = GateEngine::frame_verifier_registration_storage_bytes::<
        ControlReservation,
    >(&compiled.profile.id)
    .map_err(|_| GateError::Unavailable)?;
    let registration = gate.register_frame_verifier_precharged(
        &compiled.profile.id,
        &compiled.plan_digest,
        verifier,
        control.reserve(
            ControlKind::Registration,
            u64::try_from(registration_bytes).map_err(|_| GateError::Unavailable)?,
        )?,
    )?;

    funded_handle(
        &control,
        ControlKind::Registration,
        0,
        CoreProviderAuthority {
            auth,
            gate,
            control: control.clone(),
            registrations: FactoryRegistrations {
                frame: Mutex::new(Some(registration)),
                calls: Mutex::new(Some(calls)),
                flow_endpoints: Mutex::new(Some(flow_endpoints)),
            },
        },
    )
}
use tobkiri_profile::{
    CallNodeKind, CodeLocation, CodeOwner, CompiledCallWitness, CompiledInputEntry,
    CompiledProfile, FrozenTarget, ResolvedFlow, ResolvedOverlay, Step, StepKind,
};

const MAX_FLOW_KEY: usize = 512;
const MAX_NODE_KEY: usize = 1024;
const MAX_CONTEXT_ID: usize = 128;

/// The Session owns this administrative holder by value before the first actual
/// entry. All fields refer to already existing trusted immutable services/plan.
pub(crate) struct ExecutionFactory {
    compiled: Arc<CompiledProfile>,
    gate: Arc<GateEngine>,
    invoker: Arc<dyn PortInvoker>,
    identity: Digest,
    provider: ControlHandle<CoreProviderAuthority>,
}
impl ExecutionFactory {
    pub(crate) fn new(
        compiled: Arc<CompiledProfile>,
        gate: Arc<GateEngine>,
        invoker: Arc<dyn PortInvoker>,
    ) -> Result<Self, CoreError> {
        let frozen = compiled.as_ref();
        if frozen.flows.len() > 65_536
            || frozen.flows.iter().any(|(flow_key, flow)| {
                flow_key.is_empty()
                    || flow_key.len() > MAX_FLOW_KEY
                    || !frozen
                        .profile
                        .packs
                        .iter()
                        .any(|pin| pin.id == flow.owner && pin.sha256 == flow.hash)
                    || flow.call_witnesses.iter().any(|(call_key, witness)| {
                        call_key != witness.key()
                            || call_key.node.len() > MAX_NODE_KEY
                            || witness.profile_revision() != &frozen.plan_digest
                            || witness.flow() != flow_key.as_str()
                    })
            })
        {
            return Err(CoreError::Gate(denied(
                "invalid immutable execution catalog",
            )));
        }
        let mut entropy = [0_u8; 32];
        getrandom::fill(&mut entropy).map_err(|_| GateError::Unavailable)?;
        let identity = identity(&[
            b"tobkiri.core-factory/2\0",
            frozen.profile.id.as_str().as_bytes(),
            frozen.plan_digest.as_str().as_bytes(),
            &entropy,
        ])?;
        let provider = build_provider_authority(compiled.clone(), gate.clone(), identity.clone())?;
        Ok(Self {
            compiled,
            gate,
            invoker,
            identity,
            provider,
        })
    }

    /// A bridge-safe, funded projection of this exact Session factory. It
    /// shares its canonical catalog and live registrations, and retains no
    /// PortInvoker/PackHost edge.
    pub(crate) fn provider_authority(&self) -> ControlHandle<CoreProviderAuthority> {
        self.provider.clone()
    }

    /// The receipt's identity comes from its original private registration,
    /// not the receiving Session's catalog or a caller-supplied descriptor.
    pub(crate) fn validate_source_receipt(
        &self,
        receipt: &SourceReadReceipt,
    ) -> Result<(), GateError> {
        if receipt.factory_identity() != &self.identity {
            return Err(denied("native source read belongs to another Core factory"));
        }
        Ok(())
    }

    /// Synchronously closes this factory's registration before Session
    /// releases its catalog and other execution state.
    pub(crate) fn close(&self) {
        self.provider.close();
    }

    /// Borrow the actual immutable compiler value by exact catalog key. The
    /// returned Flow is never cloned or independently wrapped.
    pub(crate) fn frozen_flow(&self, key: &str) -> Result<&ResolvedFlow, StepError> {
        self.compiled
            .flows
            .get(key)
            .ok_or_else(|| step(denied("Flow is absent from this frozen revision")))
    }

    pub(crate) fn frozen_flow_target(
        &self,
        caller: &CallCx,
        key: &str,
    ) -> Result<FrozenFlowTarget, StepError> {
        check_context(self.compiled.as_ref(), caller).map_err(step)?;
        self.gate.check_context(caller).map_err(step)?;
        if self
            .provider
            .get()
            .auth
            .get()
            .closing
            .load(Ordering::Acquire)
            || !Arc::ptr_eq(
                &self.compiled,
                self.provider.get().auth.get().compiled.get(),
            )
        {
            return Err(step(denied("Flow endpoint factory is closed or changed")));
        }
        let flow = self
            .compiled
            .flows
            .get(key)
            .ok_or_else(|| step(denied("Flow key is absent from the frozen catalog")))?;
        let implements = flow
            .document
            .implements
            .as_ref()
            .ok_or_else(|| step(denied("Flow has no implements endpoint")))?;
        let target = self
            .gate
            .resolve_frozen_flow_target(caller, implements, Some(key))
            .map_err(step)?;
        self.provider
            .get()
            .with_flow_registration(|registration| {
                let exact = registration.endpoints().iter().any(|candidate| {
                    candidate == &target
                        && candidate.profile == self.compiled.profile.id
                        && candidate.revision == self.compiled.plan_digest
                        && candidate.flow_key == key
                        && candidate.owner == flow.owner
                        && candidate.owner_hash == flow.hash
                        && candidate.plan_digest == flow.plan_digest
                        && candidate.implements == *implements
                        && candidate.input_view == flow.document.trigger.input
                });
                if !exact
                    || registration.profile() != &self.compiled.profile.id
                    || registration.revision() != &self.compiled.plan_digest
                    || !self.gate.frozen_flow_target_registered(
                        caller.profile(),
                        caller.revision().ok_or(GateError::Unavailable)?,
                        &target,
                    )?
                {
                    return Err(denied(
                        "Flow target is not this exact registered catalog entry",
                    ));
                }
                Ok(())
            })
            .map_err(step)?;
        Ok(target)
    }

    /// Resolve one exact, canonical Flow before the caller authorizes and
    /// consumes its ordinary FlowRun effect. This only returns bounded frozen
    /// metadata; it creates no AST authority, grant, or execution frame.
    pub(crate) fn prepare_guest_flow<'a>(
        &'a self,
        caller: &'a CallCx,
        provider: &'a dyn GuestFlowFrame,
        requested_shape: &'a ShapeRef,
        arguments_digest: &'a Digest,
    ) -> Result<PreparedGuestFlow<'a>, StepError> {
        check_context(self.compiled.as_ref(), caller).map_err(step)?;
        self.gate.check_context(caller).map_err(step)?;
        if !Arc::ptr_eq(&self.compiled, self.provider.auth.get().compiled.get())
            || !Arc::ptr_eq(&self.gate, &self.provider.gate)
            || self.identity != self.provider.auth.get().identity
        {
            return Err(step(denied("guest Flow factory catalog identity changed")));
        }
        let provider_frame = provider
            .as_any()
            .downcast_ref::<CoreProviderFrame>()
            .ok_or_else(|| step(denied("guest Flow caller is not a Core provider frame")))?;
        validate_live_guest_provider(
            &self.provider.auth,
            &self.provider,
            &self.gate,
            self.compiled.as_ref(),
            provider,
            provider_frame,
            caller,
        )
        .map_err(step)?;

        let mut selected = None;
        for (key, flow) in &self.compiled.flows {
            if flow.document.implements.as_ref() == Some(requested_shape) {
                if selected.replace((key.as_str(), flow)).is_some() {
                    return Err(step(denied(
                        "guest Flow endpoint selects multiple canonical targets",
                    )));
                }
            }
        }
        let (flow_key, flow) = selected.ok_or_else(|| {
            step(denied(
                "guest Flow endpoint is absent from the frozen catalog",
            ))
        })?;
        if key_for_flow(self.compiled.as_ref(), flow) != Some(flow_key)
            || !self
                .compiled
                .profile
                .packs
                .iter()
                .any(|pin| pin.id == flow.owner && pin.sha256 == flow.hash)
        {
            return Err(step(denied(
                "guest Flow target is not the exact pinned compiler value",
            )));
        }
        self.gate
            .code_owner_execution_limits(
                caller,
                PreparedCodeOwner::Pack {
                    pack: &flow.owner,
                    hash: &flow.hash,
                },
            )
            .map_err(step)?;

        let target = self.frozen_flow_target(caller, flow_key)?;
        if target.implements != *requested_shape
            || target.owner != flow.owner
            || target.owner_hash != flow.hash
            || target.flow_key != flow_key
        {
            return Err(step(denied(
                "dynamic Flow differs from its frozen endpoint",
            )));
        }
        let target_dynamic = target
            .clone_storage_bytes()
            .map_err(|_| step(GateError::Unavailable))?
            .checked_add(std::mem::size_of::<FrozenFlowTarget>())
            .ok_or_else(|| step(GateError::Unavailable))?;
        let reservation_bytes =
            u64::try_from(target_dynamic).map_err(|_| step(GateError::Unavailable))?;
        let reservation = self
            .gate
            .control()
            .reserve(ControlKind::Frame, reservation_bytes)
            .map_err(|error| step(GateError::from(error)))?;
        Ok(PreparedGuestFlow {
            factory: self,
            caller,
            provider,
            provider_frame,
            requested_shape,
            arguments_digest,
            flow_key,
            flow,
            target,
            used: AtomicBool::new(false),
            _reservation: reservation,
        })
    }

    /// Turn the one-shot metadata selection into a real Pack-owned Flow frame
    /// only after its separate FlowRun permit has been consumed by Session.
    /// The caller retains terminal custody on every success/failure path.
    pub(crate) fn enter_guest_flow(
        &self,
        prepared: &PreparedGuestFlow<'_>,
        terminal: &crate::session::ConsumedEntryTerminal,
    ) -> Result<ControlHandle<dyn ActiveCodeFrame>, StepError> {
        if prepared
            .used
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .is_err()
        {
            return Err(step(denied("guest Flow selection was already consumed")));
        }
        if !ptr::eq(prepared.factory, self)
            || !Arc::ptr_eq(&self.compiled, self.provider.auth.get().compiled.get())
            || key_for_flow(self.compiled.as_ref(), prepared.flow) != Some(prepared.flow_key)
            || prepared.flow.document.implements.as_ref() != Some(prepared.requested_shape)
            || prepared.target.owner != prepared.flow.owner
            || prepared.target.owner_hash != prepared.flow.hash
            || prepared.target.implements != *prepared.requested_shape
            || prepared.target.flow_key != prepared.flow_key
            || !self
                .gate
                .frozen_flow_target_registered(
                    &self.compiled.profile.id,
                    &self.compiled.plan_digest,
                    &prepared.target,
                )
                .map_err(step)?
        {
            return Err(step(denied("guest Flow selection changed before entry")));
        }
        let guest = GuestFlowCall {
            provider_entry: prepared.provider,
            provider_frame: prepared.provider_frame,
            requested_shape: prepared.requested_shape,
            target: &prepared.target,
        };
        let entry = self.prepare_flow_entry(
            prepared.caller,
            prepared.flow,
            prepared.arguments_digest,
            terminal.permit(),
            &prepared.target,
            FlowEntryMode::Guest,
            None,
            None,
            Some(guest),
        )?;
        self.activate_flow_entry(entry)
    }

    /// Prepare the actual Launcher-bound UI receive before argument
    /// materialization. The caller may supply only the already-registered
    /// attachment fields; the endpoint is resolved from Gate's immutable
    /// profile/revision binding and must be unique for this exact view.
    pub(crate) fn prepare_ui_invocation(
        &self,
        caller: &CallCx,
        session: &tobkiri_canon::ids::UiSessionId,
        generation: u64,
        receiver: &Digest,
        view: &ShapeRef,
        op: &tobkiri_canon::ids::OpName,
    ) -> Result<PreparedUiInvocation, StepError> {
        self.gate.check_context(caller).map_err(step)?;
        let auth = self.provider.get().auth.get();
        let sequence = next_sequence(&auth.activation).map_err(step)?;
        let invocation = identity(&[
            b"tobkiri.core-ui-received-invocation/1\0",
            self.identity.as_str().as_bytes(),
            caller.request().as_str().as_bytes(),
            session.as_str().as_bytes(),
            &generation.to_le_bytes(),
            receiver.as_str().as_bytes(),
            &sequence.to_le_bytes(),
        ])
        .map_err(step)?;
        let gate_target = self
            .gate
            .resolve_frozen_target(caller, view, op)
            .map_err(step)?;
        let target_bytes = gate_target_storage_bytes(&gate_target)
            .checked_add(160)
            .ok_or_else(|| step(GateError::Unavailable))?;
        let reservation = self
            .gate
            .control()
            .reserve(
                ControlKind::Registration,
                u64::try_from(
                    std::mem::size_of::<PreparedUiInvocation>()
                        .checked_add(target_bytes)
                        .ok_or_else(|| step(GateError::Unavailable))?,
                )
                .map_err(|_| step(GateError::Unavailable))?,
            )
            .map_err(|error| step(GateError::from(error)))?;
        let target =
            gate_target_to_profile(&gate_target).map_err(|_| step(GateError::Unavailable))?;
        let entry = UiInvocation {
            invocation: &invocation,
            session,
            generation,
            receiver,
            view,
            op,
            target: &target,
        };
        let preparation = self.provider.get().prepare_ui_invocation_with_handle(
            self.provider.clone(),
            caller,
            &entry,
        )?;
        Ok(PreparedUiInvocation {
            invocation,
            target,
            preparation,
            _reservation: reservation,
        })
    }

    pub(crate) async fn prepare_user_invocation(
        &self,
        caller: &CallCx,
        view: &ShapeRef,
        op: &tobkiri_canon::ids::OpName,
        user_document: &Digest,
    ) -> Result<PreparedUserInvocation, CoreError> {
        check_context(self.compiled.as_ref(), caller).map_err(step)?;
        self.gate.check_context(caller).map_err(step)?;
        if !matches!(caller.principal(), Principal::User)
            || caller.revision() != Some(&self.compiled.plan_digest)
            || self
                .provider
                .get()
                .auth
                .get()
                .closing
                .load(Ordering::Acquire)
        {
            return Err(step(denied("User invocation has no live registered context")).into());
        }
        self.gate
            .code_owner_execution_limits(
                caller,
                PreparedCodeOwner::UserDocument {
                    document: user_document,
                },
            )
            .map_err(step)?;
        let target = self
            .gate
            .resolve_received_user_target(caller, view, op)
            .await?;
        let sequence = next_sequence(&self.provider.get().auth.get().activation).map_err(step)?;
        let sequence_bytes = sequence.to_le_bytes();
        let invocation = identity(&[
            b"tobkiri.core-user-received-invocation/1\0",
            self.identity.as_str().as_bytes(),
            caller.request().as_str().as_bytes(),
            user_document.as_str().as_bytes(),
            caller
                .revision()
                .ok_or_else(|| step(GateError::Unavailable))?
                .as_str()
                .as_bytes(),
            &caller.security_epoch().to_le_bytes(),
            &sequence_bytes,
            shape_identity(view).map_err(step)?.as_str().as_bytes(),
            op.as_str().as_bytes(),
        ])
        .map_err(step)?;
        let target_bytes = gate_target_storage_bytes(&target)
            .checked_add(std::mem::size_of::<PreparedUserInvocation>() + 192)
            .ok_or_else(|| step(GateError::Unavailable))?;
        let reservation = self
            .gate
            .control()
            .reserve(
                ControlKind::Registration,
                u64::try_from(target_bytes).map_err(|_| step(GateError::Unavailable))?,
            )
            .map_err(|error| step(GateError::from(error)))?;
        let profile_target =
            gate_target_to_profile(&target).map_err(|_| step(GateError::Unavailable))?;
        let entry = tobkiri_flow::FlowUserInvocation {
            invocation: &invocation,
            view,
            op,
            target: &profile_target,
        };
        let preparation = self.provider.get().prepare_user_invocation_with_handle(
            self.provider.clone(),
            caller,
            &entry,
            user_document,
            &target,
        )?;
        Ok(PreparedUserInvocation {
            invocation,
            target: profile_target,
            preparation,
            _reservation: reservation,
        })
    }

    /// Build the actual root Flow activation from the permit already consumed
    /// by the caller. Core borrows it synchronously and retains only immutable
    /// evidence; `ConsumedEntryTerminal` remains the sole outcome owner.
    pub(crate) fn enter_root_flow(
        &self,
        caller: &CallCx,
        flow_key: &str,
        actual_args: &Digest,
        terminal: &crate::session::ConsumedEntryTerminal,
    ) -> Result<ControlHandle<dyn ActiveCodeFrame>, StepError> {
        let flow = self.frozen_flow(flow_key)?;
        let target = self.frozen_flow_target(caller, flow_key)?;
        let prepared = self.prepare_flow_entry(
            caller,
            flow,
            actual_args,
            terminal.permit(),
            &target,
            FlowEntryMode::Root,
            None,
            None,
            None,
        )?;
        self.activate_flow_entry(prepared)
    }

    /// Enter the exact selected Flow only from its live actual FlowRun step.
    /// The parent's node is claimed once only after the new bounded Core frame
    /// has been built; the supplied terminal retains its own grant.
    pub(crate) fn enter_provided_flow(
        &self,
        caller: &CallCx,
        caller_frame: &ControlHandle<dyn ActiveCodeFrame>,
        caller_node: &ReachedNode,
        exact_target: &FrozenFlowTarget,
        flow_key: &str,
        actual_args: &Digest,
        terminal: &crate::session::ConsumedEntryTerminal,
    ) -> Result<ControlHandle<dyn ActiveCodeFrame>, StepError> {
        let flow = self.frozen_flow(flow_key)?;
        let target = self.frozen_flow_target(caller, flow_key)?;
        let implements = flow
            .document
            .implements
            .as_ref()
            .ok_or_else(|| step(denied("selected Flow has no implements endpoint")))?;
        if &target != exact_target
            || target.owner != flow.owner
            || target.owner_hash != flow.hash
            || &target.implements != implements
            || target.flow_key != flow_key
        {
            return Err(step(denied(
                "provided Flow differs from its exact frozen target",
            )));
        }
        let parent = ParentFlowCall {
            frame_identity: caller_frame.identity(),
            node: caller_node,
            flow_key,
            args: actual_args,
            target: &target,
        };
        let prepared = self.prepare_flow_entry(
            caller,
            flow,
            actual_args,
            terminal.permit(),
            &target,
            FlowEntryMode::Provided,
            Some(parent),
            None,
            None,
        )?;
        self.activate_flow_entry(prepared)
    }

    /// Enter one exact frozen Input-trigger Flow route. The virtual-input
    /// permit and the route's separate FlowRun permit remain owned by Session;
    /// Core borrows both while binding the physical read receipt to the actual
    /// selected route and constructing the Pack-owned Flow frame.
    pub(crate) fn enter_native_flow(
        &self,
        caller: &CallCx,
        routing_terminal: &crate::session::ConsumedEntryTerminal,
        receipt: &SourceReadReceipt,
        route: &IngressRoute,
        flow_key: &str,
        actual_args: &Digest,
        flow_terminal: &crate::session::ConsumedEntryTerminal,
    ) -> Result<ControlHandle<dyn ActiveCodeFrame>, StepError> {
        let flow = self.frozen_flow(flow_key)?;
        let target = self.frozen_flow_target(caller, flow_key)?;
        if target != route.target {
            return Err(step(denied(
                "native Input route target differs from the registered frozen Flow",
            )));
        }
        let native = NativeFlowCall {
            routing_permit: routing_terminal.permit(),
            receipt,
            route,
            target: &target,
        };
        let prepared = self.prepare_flow_entry(
            caller,
            flow,
            actual_args,
            flow_terminal.permit(),
            &target,
            FlowEntryMode::NativeInput,
            None,
            Some(native),
            None,
        )?;
        self.activate_flow_entry(prepared)
    }

    fn activate_flow_entry(
        &self,
        prepared: PreparedFlowEntry<'_>,
    ) -> Result<ControlHandle<dyn ActiveCodeFrame>, StepError> {
        let flow = prepared.flow;
        let flow_key = prepared.flow_key();
        // The consumed lease can be earlier than the original caller deadline
        // after durable expiry binding. Funding and the actual callee must use
        // that exact bound while retaining the original identity and ledger.
        let admitted_caller = prepared
            .caller
            .try_clone_precharged()
            .map_err(|_| step(GateError::Unavailable))?
            .cap_deadline(prepared.permit.deadline());
        let evidence_dynamic = prepared
            .permit
            .execution_entry_evidence_storage_bytes()
            .map_err(|error| StepError::new("unavailable", error.to_string()))?;
        let frame_dynamic = code_owner_pack_storage_bytes(&flow.owner, &flow.hash)
            .checked_add(flow_key.len())
            .ok_or_else(|| StepError::new("unavailable", "frame storage overflow"))?;
        let frame_bytes = ControlHandle::<ActiveFlowFrame>::storage_bytes::<ControlReservation>()
            .checked_add(frame_dynamic)
            .and_then(|bytes| u64::try_from(bytes).ok())
            .ok_or_else(|| StepError::new("unavailable", "frame storage overflow"))?;
        let evidence_bytes =
            ControlHandle::<tobkiri_gate::ExecutionEntryEvidence>::storage_bytes::<
                ControlReservation,
            >()
            .checked_add(evidence_dynamic)
            .and_then(|bytes| u64::try_from(bytes).ok())
            .ok_or_else(|| StepError::new("unavailable", "entry evidence storage overflow"))?;
        let control = self.gate.control();
        let mut result = None;
        self.gate
            .with_frame_preparation(
                &admitted_caller,
                &prepared,
                AllocationReason::RuntimeValue,
                &mut |funding| {
                    // Reserve both independently retained control blocks before
                    // copying permit/context metadata or constructing strings.
                    let frame_reservation = control.reserve(ControlKind::Frame, frame_bytes)?;
                    let evidence_reservation =
                        control.reserve(ControlKind::Frame, evidence_bytes)?;
                    let evidence = prepared
                        .permit
                        .try_execution_entry_evidence_precharged()
                        .map_err(|_| GateError::Unavailable)?;
                    let evidence =
                        ControlHandle::try_new_precharged(evidence, evidence_reservation)
                            .map_err(|_| GateError::Unavailable)?;
                    let context = match prepared.mode {
                        FlowEntryMode::Root => admitted_caller
                            .try_clone_precharged()
                            .map_err(|_| GateError::Unavailable)?,
                        FlowEntryMode::Provided | FlowEntryMode::Guest => {
                            let limits = self.gate.code_owner_execution_limits(
                                &admitted_caller,
                                PreparedCodeOwner::Pack {
                                    pack: &flow.owner,
                                    hash: &flow.hash,
                                },
                            )?;
                            admitted_caller.try_callee(
                                tobkiri_gate::Principal::Pack {
                                    pack: flow
                                        .owner
                                        .try_clone_precharged()
                                        .map_err(|_| GateError::Unavailable)?,
                                    hash: flow.hash.clone(),
                                },
                                limits,
                            )?
                        }
                        FlowEntryMode::NativeInput => {
                            // The verifier has bound this entry to the actual
                            // source-read receipt, virtual-input routing claim,
                            // and exact frozen route before this funded phase.
                            if prepared.native.is_none() {
                                return Err(GateError::Unavailable);
                            }
                            let limits = self.gate.code_owner_execution_limits(
                                &admitted_caller,
                                PreparedCodeOwner::Pack {
                                    pack: &flow.owner,
                                    hash: &flow.hash,
                                },
                            )?;
                            admitted_caller.try_callee(
                                tobkiri_gate::Principal::Pack {
                                    pack: flow
                                        .owner
                                        .try_clone_precharged()
                                        .map_err(|_| GateError::Unavailable)?,
                                    hash: flow.hash.clone(),
                                },
                                limits,
                            )?
                        }
                    };
                    let entry_context = admitted_caller
                        .try_clone_precharged()
                        .map_err(|_| GateError::Unavailable)?;
                    let flow_key =
                        try_owned_string(flow_key).map_err(|_| GateError::Unavailable)?;
                    let owner = clone_pack_owner_precharged(&flow.owner, &flow.hash)
                        .map_err(|_| GateError::Unavailable)?;
                    let frame = ActiveFlowFrame {
                        auth: self.provider.auth.clone(),
                        provider: self.provider.clone(),
                        gate: self.gate.clone(),
                        invoker: self.invoker.clone(),
                        context,
                        entry_context,
                        entry_evidence: evidence,
                        flow_key,
                        code: CodeLocation::Flow,
                        overlay_base_address: None,
                        overlay_index: None,
                        owner,
                        frame: prepared.frame.clone(),
                        path: prepared.path.clone(),
                        activation: prepared.activation,
                        guest_parent: prepared
                            .guest
                            .as_ref()
                            .map(|guest| guest.provider_frame._retirement.clone()),
                    };
                    let handle = ControlHandle::try_new_precharged(frame, frame_reservation)
                        .map_err(|_| GateError::Unavailable)?;
                    if funding.limit == 0 {
                        return Err(GateError::Unavailable);
                    }
                    result = Some(handle.project(|frame| frame as &dyn ActiveCodeFrame));
                    Ok(())
                },
            )
            .map_err(step)?;
        prepared.transfer().map_err(step)?;
        result.ok_or_else(|| step(GateError::Unavailable))
    }

    fn prepare_flow_entry<'a>(
        &'a self,
        caller: &'a CallCx,
        flow: &'a ResolvedFlow,
        actual_args: &'a Digest,
        consumed: &'a DispatchPermit,
        target: &'a FrozenFlowTarget,
        mode: FlowEntryMode,
        parent: Option<ParentFlowCall<'a>>,
        native: Option<NativeFlowCall<'a>>,
        guest: Option<GuestFlowCall<'a>>,
    ) -> Result<PreparedFlowEntry<'a>, StepError> {
        check_context(self.compiled.as_ref(), caller).map_err(step)?;
        self.gate.check_context(caller).map_err(step)?;
        let origin_is_exact = matches!(
            (mode, parent.is_some(), native.is_some(), guest.is_some()),
            (FlowEntryMode::Root, false, false, false)
                | (FlowEntryMode::Provided, true, false, false)
                | (FlowEntryMode::NativeInput, false, true, false)
                | (FlowEntryMode::Guest, false, false, true)
        );
        if !origin_is_exact {
            return Err(step(denied(
                "Flow entry mode has conflicting or missing origin evidence",
            )));
        }
        let key = key_for_flow(self.compiled.as_ref(), flow)
            .ok_or_else(|| step(denied("Flow is a cloned or foreign compiler value")))?;
        validate_flow_permit(caller, flow, key, target, actual_args, consumed).map_err(step)?;
        let activation = next_sequence(&self.provider.auth.activation).map_err(step)?;
        let frame = identity(&[
            b"tobkiri.core-frame/1",
            self.identity.as_str().as_bytes(),
            &activation.to_le_bytes(),
        ])
        .map_err(step)?;
        let native_target = native
            .map(|native| shape_identity(&native.target.implements))
            .transpose()
            .map_err(step)?;
        let native_implements = native
            .map(|native| shape_identity(&native.route.implements))
            .transpose()
            .map_err(step)?;
        let native_route_target = native
            .map(|native| native.route.target.digest())
            .transpose()
            .map_err(step)?;
        let guest_requested_shape = guest
            .map(|guest| shape_identity(guest.requested_shape))
            .transpose()
            .map_err(step)?;
        let target_digest = target.digest().map_err(step)?;
        let path = identity(&[
            b"tobkiri.core-flow-entry/1",
            frame.as_str().as_bytes(),
            key.as_bytes(),
            target_digest.as_str().as_bytes(),
            flow.plan_digest.as_str().as_bytes(),
            consumed.id().as_str().as_bytes(),
            actual_args.as_str().as_bytes(),
            match mode {
                FlowEntryMode::Root => b"root",
                FlowEntryMode::Provided => b"provided",
                FlowEntryMode::NativeInput => b"native-input",
                FlowEntryMode::Guest => b"guest",
            },
            native.map_or(b"".as_slice(), |native| {
                native.receipt.metadata().read_grant.as_str().as_bytes()
            }),
            native.map_or(b"".as_slice(), |native| {
                native.receipt.metadata().frame.as_str().as_bytes()
            }),
            native.map_or(b"".as_slice(), |native| {
                native.routing_permit.id().as_str().as_bytes()
            }),
            native_route_target
                .as_ref()
                .map_or(b"".as_slice(), |target| target.as_str().as_bytes()),
            native.map_or(b"".as_slice(), |native| {
                native.target.owner.as_str().as_bytes()
            }),
            native.map_or(b"".as_slice(), |native| {
                native.target.owner_hash.as_str().as_bytes()
            }),
            native_target
                .as_ref()
                .map_or(b"".as_slice(), |target| target.as_str().as_bytes()),
            native_implements
                .as_ref()
                .map_or(b"".as_slice(), |shape| shape.as_str().as_bytes()),
            guest.map_or(b"".as_slice(), |guest| {
                guest.provider_frame.state.frame.as_str().as_bytes()
            }),
            guest.map_or(b"".as_slice(), |guest| {
                guest.provider_frame.state.path.as_str().as_bytes()
            }),
            guest.map_or(b"".as_slice(), |guest| {
                guest.provider_frame.state.metadata.node.as_str().as_bytes()
            }),
            guest_requested_shape
                .as_ref()
                .map_or(b"".as_slice(), |shape| shape.as_str().as_bytes()),
            guest.map_or(b"".as_slice(), |guest| {
                guest.target.owner.as_str().as_bytes()
            }),
            guest.map_or(b"".as_slice(), |guest| {
                guest.target.owner_hash.as_str().as_bytes()
            }),
        ])
        .map_err(step)?;
        Ok(PreparedFlowEntry {
            factory: self,
            caller,
            flow,
            target,
            args: actual_args,
            permit: consumed,
            mode,
            parent,
            native,
            guest,
            frame,
            node: flow.plan_digest.clone(),
            activation,
            path,
            state: Mutex::new(PreparationState::Fresh),
        })
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum FlowEntryMode {
    Root,
    Provided,
    NativeInput,
    Guest,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum PreparationState {
    Fresh,
    Funded,
    Transferred,
    Denied,
}
/// Stack-only proof: every reference points into the caller's actual invocation
/// or this factory's immutable compiler value. No public constructor/Any input.
pub(crate) struct PreparedFlowEntry<'a> {
    factory: &'a ExecutionFactory,
    caller: &'a CallCx,
    flow: &'a ResolvedFlow,
    target: &'a FrozenFlowTarget,
    args: &'a Digest,
    permit: &'a DispatchPermit,
    mode: FlowEntryMode,
    parent: Option<ParentFlowCall<'a>>,
    native: Option<NativeFlowCall<'a>>,
    guest: Option<GuestFlowCall<'a>>,
    frame: Digest,
    node: Digest,
    activation: u64,
    path: Digest,
    state: Mutex<PreparationState>,
}
impl PreparedFlowEntry<'_> {
    fn flow_key(&self) -> &str {
        key_for_flow(self.factory.compiled.as_ref(), self.flow).unwrap_or("")
    }

    /// Transfer the SAME successfully funded activation exactly once. This
    /// performs no allocation/refund and cannot create independent activations.
    fn transfer(&self) -> Result<(), GateError> {
        let mut state = self.state.lock().map_err(|_| GateError::Unavailable)?;
        if *state != PreparationState::Funded {
            return Err(denied("entry preparation is not uniquely funded"));
        }
        self.verify(self.caller)?;
        let _admission = self
            .factory
            .provider
            .auth
            .admission
            .lock()
            .map_err(|_| GateError::Unavailable)?;
        if self.factory.provider.auth.closing.load(Ordering::Acquire) {
            return Err(GateError::Unavailable);
        }
        validate_flow_permit(
            self.caller,
            self.flow,
            self.flow_key(),
            self.target,
            self.args,
            self.permit,
        )?;

        if let Some(parent) = &self.parent {
            let reached = parent
                .node
                .core_proof::<ReachedActivationProof>()
                .ok_or(GateError::Unavailable)?;
            let node = &reached.node;
            if !ControlHandle::ptr_eq(&node.auth, &self.factory.provider.auth) {
                return Err(denied("Flow caller belongs to another Core factory"));
            }
            let mut lifecycle = node.state.lock().map_err(|_| GateError::Unavailable)?;
            if !lifecycle.live || lifecycle.claimed {
                return Err(denied("provided Flow call activation is not live"));
            }
            // Charge only after the exact caller step is still live, while its
            // admission lock remains held through the one-shot claim.
            self.caller
                .budget()
                .ok_or_else(|| denied("nested Flow has no inherited budget"))?
                .charge(ChargeKind::SubFlow)?;
            lifecycle.expected_args = Some(parent.args.clone());
            lifecycle.provided_flow_key =
                Some(flow_key_digest(parent.flow_key).map_err(|_| GateError::Unavailable)?);
            lifecycle.claimed = true;
        } else if self.mode == FlowEntryMode::Guest {
            self.verify_guest_origin_under_admission()?;
            self.caller
                .budget()
                .ok_or_else(|| denied("nested Flow has no inherited budget"))?
                .charge(ChargeKind::SubFlow)?;
        } else if self.mode == FlowEntryMode::NativeInput {
            self.verify_native_origin()?;
            self.caller
                .budget()
                .ok_or_else(|| denied("nested Flow has no inherited budget"))?
                .charge(ChargeKind::SubFlow)?;
        } else if self.mode != FlowEntryMode::Root {
            return Err(denied(
                "nested Flow mode lacks its actual invocation origin",
            ));
        }
        *state = PreparationState::Transferred;
        Ok(())
    }
    fn verify(&self, cx: &CallCx) -> Result<(), GateError> {
        let _admission = self
            .factory
            .provider
            .auth
            .admission
            .lock()
            .map_err(|_| GateError::Unavailable)?;
        if self.factory.provider.auth.closing.load(Ordering::Acquire) {
            return Err(GateError::Unavailable);
        }
        check_context(self.factory.compiled.as_ref(), cx)?;
        check_same_context(self.caller, cx)?;
        self.factory.gate.check_context(cx)?;
        if key_for_flow(self.factory.compiled.as_ref(), self.flow).is_none() {
            return Err(denied("entry no longer points at its frozen Flow"));
        }
        validate_flow_permit(
            self.caller,
            self.flow,
            self.flow_key(),
            self.target,
            self.args,
            self.permit,
        )?;
        // The mode is sealed by the actual Core entry method; neither a caller
        // principal name nor possession of a registered owner chooses it.
        match (self.mode, &self.parent, &self.native, &self.guest) {
            (FlowEntryMode::Root, None, None, None) => Ok(()),
            (FlowEntryMode::NativeInput, None, Some(_), None) => self.verify_native_origin(),
            (FlowEntryMode::Guest, None, None, Some(_)) => {
                self.verify_guest_origin_under_admission()
            }
            (FlowEntryMode::Provided, Some(parent), None, None) => {
                let target_key = key_for_flow(self.factory.compiled.as_ref(), self.flow)
                    .ok_or_else(|| denied("provided Flow is not in the frozen catalog"))?;
                if parent.flow_key != target_key || parent.args != self.args {
                    return Err(denied("provided Flow arguments or exact target changed"));
                }
                let reached = parent
                    .node
                    .core_proof::<ReachedActivationProof>()
                    .ok_or(GateError::Unavailable)?;
                let node = &reached.node;
                if !ControlHandle::ptr_eq(&node.auth, &self.factory.provider.auth)
                    || node.frame != parent.frame_identity
                    || node.kind != Some(FlowNodeKind::ProvidedFlow)
                {
                    return Err(denied("caller frame is not this live FlowRun step"));
                }
                let flow = self
                    .factory
                    .compiled
                    .flows
                    .get(parent.flow_key)
                    .ok_or_else(|| denied("provided target is absent from frozen catalog"))?;
                if flow.owner != parent.target.owner
                    || flow.hash != parent.target.owner_hash
                    || flow.document.implements.as_ref() != Some(&parent.target.implements)
                    || parent.target != self.target
                {
                    return Err(denied(
                        "FlowRun target differs from its exact frozen callee",
                    ));
                }
                let parent_flow = self
                    .factory
                    .compiled
                    .flows
                    .get(node.flow_key.as_str())
                    .ok_or(GateError::Unavailable)?;
                let parent_step = node
                    .step_address
                    .and_then(|address| {
                        flow_step_at_actual_location(
                            parent_flow,
                            node.code.as_ref(),
                            node.overlay_base_address,
                            node.overlay_index,
                            address,
                        )
                    })
                    .ok_or_else(|| denied("FlowRun step is not in the caller's frozen AST"))?;
                let StepKind::Flow(subflow) = &parent_step.kind else {
                    return Err(denied("caller activation is not a FlowRun step"));
                };
                if subflow.implements != parent.target.implements {
                    return Err(denied("FlowRun endpoint differs from its exact target"));
                }
                let key_digest = flow_key_digest(parent.flow_key)?;
                let lifecycle = node.state.lock().map_err(|_| GateError::Unavailable)?;
                if lifecycle.claimed
                    || lifecycle
                        .provided_flow_key
                        .as_ref()
                        .is_some_and(|key| key != &key_digest)
                    || lifecycle
                        .expected_args
                        .as_ref()
                        .is_some_and(|args| args != parent.args)
                {
                    return Err(denied("caller node does not select this exact Flow"));
                }
                drop(lifecycle);
                check_frozen_node(node)?;
                Ok(())
            }
            _ => Err(denied("Flow entry mode lacks its actual invocation origin")),
        }
    }

    fn verify_guest_origin_under_admission(&self) -> Result<(), GateError> {
        let guest = self.guest.as_ref().ok_or(GateError::Unavailable)?;
        let expected_target_digest = self.target.digest()?;
        let expected_implements = self
            .flow
            .document
            .implements
            .as_ref()
            .ok_or_else(|| denied("dynamic Flow target has no implements endpoint"))?;
        if expected_implements != guest.requested_shape
            || guest.target.owner != self.flow.owner
            || guest.target.owner_hash != self.flow.hash
            || guest.target.implements != *guest.requested_shape
            || guest.target != self.target
            || guest.target.flow_key != self.flow_key()
            || !matches!(
                self.permit.effect(),
                EffectRequest::FlowRun {
                    implements,
                    flow_owner,
                    flow_key,
                    target_digest,
                }
                    if implements == guest.requested_shape
                        && flow_owner.0 == self.flow.owner
                        && flow_owner.1 == self.flow.hash
                        && flow_key == self.flow_key()
                        && target_digest == &expected_target_digest
            )
        {
            return Err(denied(
                "dynamic Flow permit differs from its exact frozen target",
            ));
        }
        validate_guest_provider_under_admission(
            &self.factory.provider.auth,
            &self.factory.provider,
            &self.factory.gate,
            self.factory.compiled.as_ref(),
            guest.provider_entry,
            guest.provider_frame,
            self.caller,
        )
    }

    fn verify_native_origin(&self) -> Result<(), GateError> {
        let native = self.native.as_ref().ok_or(GateError::Unavailable)?;
        self.factory.validate_source_receipt(native.receipt)?;
        native.receipt.validate_context(self.caller)?;
        let receipt = native.receipt.metadata();
        let Principal::Input { source } = self.caller.principal() else {
            return Err(denied(
                "native Flow entry lost its original Input principal",
            ));
        };
        let implements = self
            .flow
            .document
            .implements
            .as_ref()
            .ok_or_else(|| denied("native route Flow has no implements endpoint"))?;
        if receipt.source != *source
            || receipt.payload != *self.args
            || native.routing_permit.args_digest() != &receipt.payload
            || !native.routing_permit.matches_context(self.caller)
            || native.routing_permit.deadline() <= Instant::now()
            || !matches!(
                native.routing_permit.effect(),
                EffectRequest::VirtualInputSubmit { .. }
            )
            || native.route.implements != *implements
            || native.target != self.target
            || native.route.target != *self.target
            || native.target.owner != self.flow.owner
            || native.target.owner_hash != self.flow.hash
            || native.target.implements != *implements
            || native.target.flow_key != self.flow_key()
        {
            return Err(denied(
                "native receipt, routing claim or Flow route differs from this entry",
            ));
        }
        let entries = self.factory.compiled.input_entries();
        let mut exact_entry = None;
        for entry in entries {
            if entry.profile_revision() == &self.factory.compiled.plan_digest
                && entry.source_pack() == &receipt.target.pack
                && entry.source_hash() == &receipt.target.hash
                && entry.exact_input_view() == &receipt.view
                && entry.operation() == &receipt.op
            {
                if exact_entry.replace(entry).is_some() {
                    return Err(denied("native route matches multiple frozen Input records"));
                }
            }
        }
        let exact_entry = exact_entry
            .ok_or_else(|| denied("native route is not named by the exact frozen Input record"))?;
        let route_key = crate::inputs::route_flow_key(
            self.factory.compiled.as_ref(),
            exact_entry,
            native.route,
        )
        .map_err(|_| denied("native route differs from the exact frozen Input trigger"))?;
        if route_key != self.flow_key() {
            return Err(denied("native route selected a different frozen Flow key"));
        }
        self.factory.gate.input_capacity_limit(
            self.caller,
            &receipt.view,
            &receipt.op,
            &receipt.target,
        )?;
        Ok(())
    }
}
impl PreparedFrameVerifier for PreparedFlowEntry<'_> {
    fn with_preparation(
        &self,
        cx: &CallCx,
        reason: AllocationReason,
        admit: &mut dyn FnMut(PreparedFrameMetadata<'_>) -> Result<(), GateError>,
    ) -> Result<(), GateError> {
        let mut state = self.state.lock().map_err(|_| GateError::Unavailable)?;
        if *state != PreparationState::Fresh || reason != AllocationReason::RuntimeValue {
            return Err(denied(
                "prepared entry was reused or changed allocation reason",
            ));
        }
        if let Err(error) = self.verify(cx) {
            *state = PreparationState::Denied;
            return Err(error);
        }
        let admitted = admit(PreparedFrameMetadata {
            caller: self.caller,
            entry: PreparedFrameEntry::InitialEntry(self.permit),
            owner: PreparedCodeOwner::Pack {
                pack: &self.flow.owner,
                hash: &self.flow.hash,
            },
            frame: &self.frame,
            node: &self.node,
            activation: self.activation,
            path: &self.path,
            // No unrelated caller memory/fuel minimum is invented here. Cost,
            // active loops, deadline and cancellation remain in the caller Cx.
            transitive_memory_bytes: None,
        });
        *state = if admitted.is_ok() {
            PreparationState::Funded
        } else {
            PreparationState::Denied
        };
        admitted
    }
}

impl PreparedFrameVerifier for PreparedNativeEntry<'_> {
    fn with_preparation(
        &self,
        cx: &CallCx,
        reason: AllocationReason,
        admit: &mut dyn FnMut(PreparedFrameMetadata<'_>) -> Result<(), GateError>,
    ) -> Result<(), GateError> {
        let mut phase = self.state.lock().map_err(|_| GateError::Unavailable)?;
        if *phase != PreparationState::Fresh || reason != AllocationReason::RuntimeValue {
            return Err(denied(
                "native Input preparation was reused or changed reason",
            ));
        }
        let _admission = self
            .provider
            .auth
            .admission
            .lock()
            .map_err(|_| GateError::Unavailable)?;
        if self.provider.auth.closing.load(Ordering::Acquire)
            || !check_same_context(self.caller, cx).is_ok()
            || self.entry.profile_revision() != &self.provider.auth.compiled.get().plan_digest
            || self
                .provider
                .auth
                .compiled
                .get()
                .input_entries()
                .get(self.index)
                .is_none_or(|entry| !ptr::eq(entry, self.entry))
            || self.entry.source_pack() != &self.target.pack
            || self.entry.source_hash() != &self.target.hash
            || !matches!(
                self.caller.principal(),
                tobkiri_gate::Principal::Input { source } if source == self.source
            )
        {
            *phase = PreparationState::Denied;
            return Err(denied("native Input source identity or context changed"));
        }
        if let Err(error) = self.provider.gate.input_capacity_limit(
            self.caller,
            self.entry.exact_input_view(),
            self.entry.operation(),
            self.target,
        ) {
            *phase = PreparationState::Denied;
            return Err(error);
        }
        let result = admit(PreparedFrameMetadata {
            caller: self.caller,
            entry: PreparedFrameEntry::NativeInput(tobkiri_gate::PreparedNativeInput {
                invocation: &self.invocation,
                source: self.source,
                view: self.entry.exact_input_view(),
                op: self.entry.operation(),
                target: self.target,
            }),
            owner: PreparedCodeOwner::Pack {
                pack: self.entry.source_pack(),
                hash: self.entry.source_hash(),
            },
            frame: &self.frame,
            node: &self.node,
            activation: self.activation,
            path: &self.path,
            transitive_memory_bytes: None,
        });
        *phase = if result.is_ok() {
            PreparationState::Funded
        } else {
            PreparationState::Denied
        };
        result
    }
}

impl PreparedFrameVerifier for PreparedUiEntry<'_, '_> {
    fn with_preparation(
        &self,
        cx: &CallCx,
        reason: AllocationReason,
        admit: &mut dyn FnMut(PreparedFrameMetadata<'_>) -> Result<(), GateError>,
    ) -> Result<(), GateError> {
        let mut phase = self.state.lock().map_err(|_| GateError::Unavailable)?;
        if *phase != PreparationState::Fresh || reason != AllocationReason::RuntimeValue {
            return Err(denied(
                "UI invocation preparation was reused or changed reason",
            ));
        }
        let _admission = self
            .provider
            .auth
            .admission
            .lock()
            .map_err(|_| GateError::Unavailable)?;
        let compiled = self.provider.auth.compiled.get();
        let (pack, hash, ui) = match self.caller.principal() {
            Principal::PackUi { pack, hash, ui } => (pack, hash, ui),
            _ => {
                *phase = PreparationState::Denied;
                return Err(denied("UI entry lost its registered PackUi caller"));
            }
        };
        if self.provider.auth.closing.load(Ordering::Acquire)
            || !check_same_context(self.caller, cx).is_ok()
            || self.caller.revision() != Some(&compiled.plan_digest)
            || self.entry.generation == 0
            || ui != self.entry.session
            || self.entry.receiver != &crate::session::ui_receiver_identity(self.entry.session)
            || compiled.profile.startup.ui.as_ref() != Some(pack)
            || !compiled
                .profile
                .packs
                .iter()
                .any(|pin| pin.id == *pack && pin.sha256 == *hash)
            || !same_profile_gate_target(self.entry.target, self.target)
        {
            *phase = PreparationState::Denied;
            return Err(denied("UI invocation receiver or frozen source changed"));
        }
        let result = admit(PreparedFrameMetadata {
            caller: self.caller,
            entry: PreparedFrameEntry::UiInvocation(tobkiri_gate::PreparedUiInvocation {
                invocation: self.entry.invocation,
                session: self.entry.session,
                generation: self.entry.generation,
                receiver: self.entry.receiver,
                view: self.entry.view,
                op: self.entry.op,
                target: self.target,
            }),
            owner: PreparedCodeOwner::Pack { pack, hash },
            frame: self.frame,
            node: self.node,
            activation: self.activation,
            path: self.path,
            transitive_memory_bytes: None,
        });
        *phase = if result.is_ok() {
            PreparationState::Funded
        } else {
            PreparationState::Denied
        };
        result
    }
}

impl PreparedFrameVerifier for PreparedUserEntry<'_, '_> {
    fn with_preparation(
        &self,
        cx: &CallCx,
        reason: AllocationReason,
        admit: &mut dyn FnMut(PreparedFrameMetadata<'_>) -> Result<(), GateError>,
    ) -> Result<(), GateError> {
        let mut phase = self.state.lock().map_err(|_| GateError::Unavailable)?;
        if *phase != PreparationState::Fresh || reason != AllocationReason::RuntimeValue {
            return Err(denied(
                "User invocation preparation was reused or changed allocation reason",
            ));
        }
        let _admission = self
            .provider
            .auth
            .admission
            .lock()
            .map_err(|_| GateError::Unavailable)?;
        let compiled = self.provider.auth.compiled.get();
        if self.provider.auth.closing.load(Ordering::Acquire)
            || check_same_context(self.caller, cx).is_err()
            || self.caller.revision() != Some(self.revision)
            || self.revision != &compiled.plan_digest
            || self.caller.security_epoch() != self.epoch
            || !same_profile_gate_target(self.invocation.target, self.target)
            || !matches!(self.caller.principal(), Principal::User)
            || self.provider.gate.resolve_frozen_target(
                self.caller,
                self.invocation.view,
                self.invocation.op,
            )? != *self.target
        {
            *phase = PreparationState::Denied;
            return Err(denied("User invocation document or context changed"));
        }
        self.provider.gate.code_owner_execution_limits(
            self.caller,
            PreparedCodeOwner::UserDocument {
                document: self.document,
            },
        )?;
        let result = admit(PreparedFrameMetadata {
            caller: self.caller,
            entry: PreparedFrameEntry::UserInvocation(tobkiri_gate::PreparedUserInvocation {
                invocation: self.invocation.invocation,
                user_document: self.document,
                revision: self.revision,
                epoch: self.epoch,
                view: self.invocation.view,
                op: self.invocation.op,
                target: self.target,
            }),
            owner: PreparedCodeOwner::UserDocument {
                document: self.document,
            },
            frame: self.frame,
            node: self.node,
            activation: self.activation,
            path: self.path,
            transitive_memory_bytes: None,
        });
        *phase = if result.is_ok() {
            PreparationState::Funded
        } else {
            PreparationState::Denied
        };
        result
    }
}

fn validate_flow_permit(
    caller: &CallCx,
    flow: &ResolvedFlow,
    flow_key: &str,
    target: &FrozenFlowTarget,
    actual_args: &Digest,
    permit: &DispatchPermit,
) -> Result<(), GateError> {
    caller.check_active()?;
    if !permit.matches_context(caller)
        || permit.args_digest() != actual_args
        || permit.deadline() <= Instant::now()
        || permit.revision() != caller.revision()
    {
        return Err(denied(
            "consumed Flow entry does not match its actual invocation",
        ));
    }
    let implements = flow
        .document
        .implements
        .as_ref()
        .ok_or_else(|| denied("FlowRun target has no implements endpoint"))?;
    let target_digest = target.digest()?;
    if target.profile != *caller.profile()
        || target.revision != *caller.revision().ok_or(GateError::Unavailable)?
        || target.owner != flow.owner
        || target.owner_hash != flow.hash
        || target.flow_key != flow_key
        || target.plan_digest != flow.plan_digest
        || target.implements != *implements
        || target.input_view != flow.document.trigger.input
    {
        return Err(denied(
            "FlowRun target differs from its frozen compiler entry",
        ));
    }
    match permit.effect() {
        EffectRequest::FlowRun {
            implements: permit_implements,
            flow_owner,
            flow_key: permit_key,
            target_digest: permit_target,
        } if flow_owner.0 == flow.owner
            && flow_owner.1 == flow.hash
            && permit_implements == implements
            && permit_key == flow_key
            && permit_target == &target_digest =>
        {
            Ok(())
        }
        _ => Err(denied(
            "consumed permit is not this exact provided Flow invocation",
        )),
    }
}

fn check_context(compiled: &CompiledProfile, cx: &CallCx) -> Result<(), GateError> {
    cx.check_active()?;
    if cx.profile() != &compiled.profile.id
        || cx.revision() != Some(&compiled.plan_digest)
        || cx.profile().as_str().len() > MAX_CONTEXT_ID
        || cx.request().as_str().len() > MAX_CONTEXT_ID
        || cx.executing_pairs().len() > 32
        || cx.budget().is_none()
    {
        return Err(denied("execution context is not this frozen plan"));
    }
    Ok(())
}
fn check_same_context(bound: &CallCx, cx: &CallCx) -> Result<(), GateError> {
    bound.check_active()?;
    cx.check_active()?;
    if bound.profile() != cx.profile()
        || bound.principal() != cx.principal()
        || bound.request() != cx.request()
        || bound.revision() != cx.revision()
        || bound.security_epoch() != cx.security_epoch()
        || cx.deadline() > bound.deadline()
        || !cx.limits().is_subset_of(bound.limits())
        || bound
            .budget()
            .is_none_or(|parent| cx.budget().is_none_or(|current| !current.inherits(parent)))
    {
        return Err(denied("execution provenance context changed"));
    }
    Ok(())
}
fn key_for_flow<'a>(compiled: &'a CompiledProfile, flow: &ResolvedFlow) -> Option<&'a str> {
    compiled
        .flows
        .iter()
        .find_map(|(key, frozen)| ptr::eq(frozen, flow).then_some(key.as_str()))
}
fn next_sequence(counter: &AtomicU64) -> Result<u64, GateError> {
    counter
        .fetch_update(Ordering::AcqRel, Ordering::Acquire, |old| {
            old.checked_add(1)
        })
        .map_err(|_| GateError::Unavailable)
}
fn identity(parts: &[&[u8]]) -> Result<Digest, GateError> {
    let mut bytes = [0u8; 2048];
    let mut at = 0usize;
    for part in parts {
        let end = at
            .checked_add(8)
            .and_then(|n| n.checked_add(part.len()))
            .filter(|end| *end <= bytes.len())
            .ok_or(GateError::Unavailable)?;
        bytes[at..at + 8].copy_from_slice(&(part.len() as u64).to_le_bytes());
        bytes[at + 8..end].copy_from_slice(part);
        at = end;
    }
    Ok(Digest::hash(&bytes[..at]))
}
fn shape_identity(shape: &ShapeRef) -> Result<Digest, GateError> {
    let major = shape.version.major.to_le_bytes();
    let minor = shape.version.minor.to_le_bytes();
    identity(&[
        b"tobkiri.core-shape-reference/1\0",
        shape.name.as_str().as_bytes(),
        &major,
        &minor,
    ])
}
fn denied(message: &'static str) -> GateError {
    GateError::Denied(message.into())
}
fn same_node_metadata(left: &ActiveFrameMetadata, right: &ActiveFrameMetadata) -> bool {
    left.profile == right.profile
        && left.revision == right.revision
        && left.security_epoch == right.security_epoch
        && left.principal == right.principal
        && left.request == right.request
        && left.frame == right.frame
        && left.node == right.node
        && left.activation == right.activation
        && left.owner == right.owner
        && left.path == right.path
        && left.transitive_memory_bytes == right.transitive_memory_bytes
}
fn step(error: GateError) -> StepError {
    StepError::new(error.code(), "Core execution provenance rejected")
}

#[cfg(test)]
mod control_admission_tests {
    use super::*;
    use tobkiri_canon::ids::ProfileId;
    use tobkiri_gate::{CoreControlError, CoreControlLimits, PermissionSet};

    struct NoPrompt;
    #[async_trait::async_trait]
    impl tobkiri_gate::TrustedPrompt for NoPrompt {
        async fn prompt_approval(
            &self,
            _: tobkiri_gate::ApprovalPrompt,
        ) -> tobkiri_gate::ApprovalAnswer {
            panic!("catalog registration must not request execution approval")
        }
    }

    fn source_context(factory: &ExecutionFactory, control: &CoreControl, request: &str) -> CallCx {
        let entry = &factory.compiled.input_entries()[0];
        let limits = factory.compiled.profile.pack_limits[entry.source_pack()]
            .wasm
            .clone();
        let deadline = Instant::now() + std::time::Duration::from_millis(limits.wall_ms);
        // Production read_input retains the original invocation's shared
        // loop/cost/deadline budget. An unbudgeted context is not execution
        // provenance, even when its descriptive fields match themselves.
        let budget = tobkiri_gate::BudgetContext::new(
            factory.compiled.profile.ceiling.loops.clone(),
            deadline,
        )
        .unwrap();
        CallCx::try_root_with_control(
            factory.compiled.profile.id.clone(),
            Principal::Input {
                source: tobkiri_canon::ids::InputSourceId::parse(entry.source_pack().as_str())
                    .unwrap(),
            },
            tobkiri_canon::ids::RequestId::parse(request).unwrap(),
            tobkiri_gate::CoreCancellation::new_with_control(control).unwrap(),
            deadline,
            limits,
            control.clone(),
        )
        .unwrap()
        .with_security_epoch(factory.gate.epoch(&factory.compiled.profile.id).unwrap())
        .with_revision(factory.compiled.plan_digest.clone())
        .with_budget(budget)
        .unwrap()
    }

    fn prepare_source(
        factory: &ExecutionFactory,
        caller: &CallCx,
    ) -> Result<Arc<dyn NativeInputPreparation>, StepError> {
        let entry = &factory.compiled.input_entries()[0];
        let target = factory
            .gate
            .resolve_frozen_target(caller, entry.exact_input_view(), entry.operation())
            .map_err(step)?;
        factory.provider.prepare_native_input_with_handle(
            factory.provider.clone(),
            factory.invoker.as_ref(),
            caller,
            entry,
            &target,
        )
    }

    #[tokio::test]
    async fn native_preparation_control_failure_retires_outside_admission_lock() {
        let fixture = crate::test_support::RuntimeFixture::new(&["fixture"], false).await;
        let session = fixture.session().await;
        let factory = Arc::new(
            ExecutionFactory::new(
                session.compiled.clone(),
                session.gate.clone(),
                session.invoker.clone(),
            )
            .unwrap(),
        );
        // The real registered source's private state fits. Its following
        // retirement-guard reservation must fail, exercising payload cleanup
        // after the admission-held preparation callback has returned.
        let control = CoreControl::new(CoreControlLimits {
            max_frames: 1,
            ..Default::default()
        })
        .unwrap();
        let caller = source_context(&factory, &control, "native-control-failure");
        let worker_factory = factory.clone();
        let (send, receive) = std::sync::mpsc::sync_channel(1);
        let worker = std::thread::spawn(move || {
            let result = prepare_source(&worker_factory, &caller);
            send.send(result.err().map(|error| error.code)).unwrap();
        });
        let error = receive
            .recv_timeout(std::time::Duration::from_secs(3))
            .expect("failed preparation must not recursively lock admission");
        worker.join().unwrap();
        assert_eq!(error.as_deref(), Some("limit-exceeded"));
        assert_eq!(control.usage().count(ControlKind::Frame), 0);
        assert!(
            factory
                .provider
                .auth
                .native_preparations
                .lock()
                .unwrap()
                .is_empty()
        );
        assert!(factory.provider.auth.admission.try_lock().is_ok());
        factory.close();
        session.close().await.unwrap();
    }

    #[tokio::test]
    async fn abandoned_source_preparations_release_registry_and_publication_checks_retirement() {
        let fixture = crate::test_support::RuntimeFixture::new(&["fixture"], false).await;
        let session = fixture.session().await;
        let factory = ExecutionFactory::new(
            session.compiled.clone(),
            session.gate.clone(),
            session.invoker.clone(),
        )
        .unwrap();
        let control = CoreControl::new(CoreControlLimits {
            max_frames: 16,
            ..Default::default()
        })
        .unwrap();
        // Exceed the actual native registry cap over the factory lifetime,
        // while abandoning each real preparation before physical dispatch.
        for index in 0..257 {
            let caller = source_context(&factory, &control, &format!("abandoned-native-{index}"));
            drop(prepare_source(&factory, &caller).unwrap());
            assert!(
                factory
                    .provider
                    .auth
                    .native_preparations
                    .lock()
                    .unwrap()
                    .is_empty()
            );
            assert_eq!(control.usage().count(ControlKind::Frame), 0);
        }
        let caller = source_context(&factory, &control, "retired-native-source");
        let preparation = prepare_source(&factory, &caller).unwrap();
        let concrete = factory
            .provider
            .auth
            .native_preparations
            .lock()
            .unwrap()
            .values()
            .next()
            .unwrap()
            .upgrade()
            .unwrap();
        let retired_state = concrete.frame.state.clone();
        drop(concrete);
        drop(preparation);
        assert!(!retired_state.state.lock().unwrap().live);
        // A retained state/value proof does not keep its execution live.
        let registry = Mutex::new(BTreeMap::new());
        assert!(
            factory
                .provider
                .auth
                .publish_weak(&registry, &Arc::new(()), 1, Some(retired_state.get()),)
                .is_err()
        );
        assert!(registry.lock().unwrap().is_empty());
        drop(retired_state);
        assert_eq!(control.usage().count(ControlKind::Frame), 0);

        let caller = source_context(&factory, &control, "closing-native-source");
        let preparation = prepare_source(&factory, &caller).unwrap();
        let concrete = factory
            .provider
            .auth
            .native_preparations
            .lock()
            .unwrap()
            .values()
            .next()
            .unwrap()
            .upgrade()
            .unwrap();
        factory.close();
        assert!(
            factory
                .provider
                .auth
                .native_preparations
                .lock()
                .unwrap()
                .is_empty()
        );
        assert!(
            factory
                .provider
                .auth
                .publish_weak(
                    &factory.provider.auth.native_preparations,
                    &concrete,
                    256,
                    None,
                )
                .is_err()
        );
        assert!(
            factory
                .provider
                .auth
                .native_preparations
                .lock()
                .unwrap()
                .is_empty()
        );
        drop(concrete);
        drop(preparation);
        assert_eq!(control.usage().count(ControlKind::Frame), 0);
        session.close().await.unwrap();
    }

    fn utility_only_profile() -> CompiledProfile {
        let profile = tobkiri_profile::Profile {
            format: "tobkiri.profile/1".into(),
            id: ProfileId::parse("utility-only-catalog").unwrap(),
            name: "utility only".into(),
            packs: Vec::new(),
            publishers: BTreeMap::new(),
            ceiling: PermissionSet {
                wasm: tobkiri_gate::ResourceLimits {
                    fuel_per_call: 100_000,
                    memory_mib: 16,
                    wall_ms: 1000,
                    table_elements: 1024,
                    output_bytes: 1024,
                },
                loops: tobkiri_gate::LoopLimits {
                    max_iterations: 1,
                    max_wall_ms: 1000,
                    max_cost_units: 1,
                },
                ..Default::default()
            },
            user_permissions: None,
            pack_limits: BTreeMap::new(),
            bindings: BTreeMap::new(),
            overlay_order: Vec::new(),
            user_overlays: Vec::new(),
            startup: tobkiri_profile::Startup {
                inputs: Vec::new(),
                ui: None,
            },
        };
        tobkiri_profile::compile(&profile, &[]).unwrap()
    }

    #[test]
    fn utility_only_compiled_profile_registers_empty_flow_catalog_with_bounded_control() {
        let compiled = utility_only_profile();
        assert!(compiled.flows.is_empty());
        let directory = tempfile::tempdir().unwrap();
        let root = tobkiri_store::DataRoot::open(directory.path().canonicalize().unwrap()).unwrap();
        let approvals =
            Arc::new(tobkiri_store::Approvals::open(&root, &compiled.profile.id).unwrap());
        let audit = Arc::new(
            tobkiri_store::AuditLog::open(
                &root,
                &compiled.profile.id,
                tobkiri_store::AuditQuota::default(),
            )
            .unwrap(),
        );
        let control = CoreControl::default();
        let gate = GateEngine::new_with_control(
            approvals,
            audit,
            Arc::new(NoPrompt),
            Default::default(),
            control.clone(),
        );
        gate.register_operation_views(&compiled.profile.id, &compiled.plan_digest, BTreeMap::new())
            .unwrap();
        let baseline = control.usage();
        let registration = register_flow_endpoints(&compiled, &gate).unwrap();
        assert!(registration.endpoints().is_empty());
        assert_eq!(registration.profile(), &compiled.profile.id);
        assert_eq!(registration.revision(), &compiled.plan_digest);
        assert_eq!(
            control.usage().count(ControlKind::Catalog),
            baseline.count(ControlKind::Catalog) + 1,
            "the temporary build reservation must retire, leaving only the retained catalog"
        );
        drop(registration);
        assert_eq!(control.usage().bytes, baseline.bytes);
        assert_eq!(
            control.usage().count(ControlKind::Catalog),
            baseline.count(ControlKind::Catalog)
        );
    }

    #[test]
    fn empty_catalog_reservation_still_obeys_byte_cap_and_rejects_raw_zero() {
        let bytes =
            u64::try_from(flow_endpoint_build_bytes(&utility_only_profile()).unwrap()).unwrap();
        assert_eq!(bytes, std::mem::size_of::<Vec<FrozenFlowTarget>>() as u64);
        let control = CoreControl::new(CoreControlLimits {
            total_bytes: bytes - 1,
            ..Default::default()
        })
        .unwrap();
        assert!(matches!(
            control.reserve(ControlKind::Catalog, bytes),
            Err(CoreControlError::Limit)
        ));
        assert!(matches!(
            control.reserve(ControlKind::Catalog, 0),
            Err(CoreControlError::Invalid)
        ));
        assert_eq!(control.usage().bytes, 0);
        assert_eq!(control.usage().count(ControlKind::Catalog), 0);
    }
}
