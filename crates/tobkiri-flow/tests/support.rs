//! Test-only Core frame adapter. It preserves actual compiler descriptors and
//! prepaid control ownership, while deliberately providing no Gate authority.

use std::{
    any::Any,
    collections::{BTreeMap, BTreeSet},
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};

use tobkiri_canon::{
    AllocationBudget, AllocationError, AllocationRequest, ControlHandle, Digest,
    ids::{PackId, ShapeRef},
};
use tobkiri_flow::{
    ActiveCodeFrame, FlowNodeKind, FlowNodePosition, FrameContinuation, OverlayPosition,
    OverlayTransition, ReachedNode, StepError,
};
use tobkiri_gate::{
    AllocationReason, BudgetContext, CallCx, CancellationToken, CompiledActivation, ControlKind,
    ControlReservation, CoreControl, LoopLimits, Principal, ResourceLimits,
};
use tobkiri_manifest::{
    FileEntry, PackManifest, PortDeclaration, Ports, RuntimeSpec, ShapeDefinition, ShapeKind,
    VerifiedPack, build_tpk, verify_tpk,
};
use tobkiri_profile::{
    CallNodeKind, CodeLocation, CodeOwner, CompiledCallWitness, CompiledProfile, FlowDocument,
    Profile, ResolvedFlow, Step, StepKind,
};

const TEST_CONTROL_RECORD_BYTES: u64 = 4096;
const TEST_ALLOCATION_BYTES: u64 = 8 * 1024 * 1024;
const TEST_ALLOCATION_ELEMENTS: u64 = 1_000_000;
const TEST_ALLOCATION_WORK: u64 = 8_000_000;
static NEXT_TEST_FRAME_ID: AtomicU64 = AtomicU64::new(1);

pub struct TestBudget {
    maximum: u64,
    used: AtomicU64,
}

impl TestBudget {
    pub fn new() -> Self {
        Self {
            maximum: TEST_ALLOCATION_BYTES,
            used: AtomicU64::new(0),
        }
    }
}

impl Default for TestBudget {
    fn default() -> Self {
        Self::new()
    }
}

impl AllocationBudget for TestBudget {
    fn reserve(&self, request: AllocationRequest) -> Result<(), AllocationError> {
        if request.elements > TEST_ALLOCATION_ELEMENTS || request.work > TEST_ALLOCATION_WORK {
            return Err(AllocationError::Limit);
        }
        if request.depth > 64 {
            return Err(AllocationError::Depth);
        }
        self.used
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |used| {
                used.checked_add(request.bytes)
                    .filter(|next| *next <= self.maximum)
            })
            .map(|_| ())
            .map_err(|_| AllocationError::Limit)
    }

    fn release(&self, bytes: u64) {
        self.used.fetch_sub(bytes, Ordering::AcqRel);
    }
}

pub fn budget() -> Arc<dyn AllocationBudget> {
    Arc::new(TestBudget::new())
}

pub fn root_context(limits: LoopLimits) -> CallCx {
    root_context_with_timeout(limits, Duration::from_secs(2))
}

pub fn root_context_with_timeout(limits: LoopLimits, timeout: Duration) -> CallCx {
    root_context_with_output_and_timeout(limits, 4096, timeout)
}

pub fn root_context_with_output_bytes(limits: LoopLimits, output_bytes: u64) -> CallCx {
    root_context_with_output_and_timeout(limits, output_bytes, Duration::from_secs(2))
}

fn root_context_with_output_and_timeout(
    limits: LoopLimits,
    output_bytes: u64,
    timeout: Duration,
) -> CallCx {
    let control = CoreControl::default();
    let deadline = Instant::now() + timeout;
    let mut resource_limits = ResourceLimits::unlimited();
    resource_limits.output_bytes = output_bytes;
    let cancellation =
        CancellationToken::new_with_control(&control).expect("bounded test cancellation capacity");
    CallCx::try_root_with_control(
        tobkiri_canon::ids::ProfileId::parse("prof_test").unwrap(),
        Principal::User,
        tobkiri_canon::ids::RequestId::parse("req_test").unwrap(),
        cancellation,
        deadline,
        resource_limits,
        control,
    )
    .expect("bounded test context capacity")
    .with_budget(BudgetContext::new(limits, deadline).unwrap())
    .unwrap()
}

fn control_handle<T: Send + Sync + 'static>(
    control: &CoreControl,
    kind: ControlKind,
    build: impl FnOnce() -> T,
) -> ControlHandle<T> {
    let reservation = control_reservation::<T>(control, kind);
    let value = build();
    prepaid_handle(value, reservation)
}

fn control_reservation<T: Send + Sync + 'static>(
    control: &CoreControl,
    kind: ControlKind,
) -> ControlReservation {
    let bytes = u64::try_from(ControlHandle::<T>::storage_bytes::<ControlReservation>())
        .unwrap()
        .checked_add(TEST_CONTROL_RECORD_BYTES)
        .expect("test control record bound");
    control
        .reserve(kind, bytes)
        .expect("bounded test CoreControl capacity")
}

fn prepaid_handle<T: Send + Sync + 'static>(
    value: T,
    reservation: ControlReservation,
) -> ControlHandle<T> {
    ControlHandle::try_new_precharged(value, reservation).expect("test control storage is prepaid")
}

pub fn compiled_profile(plans: &[ResolvedFlow]) -> Arc<CompiledProfile> {
    let packs = fixture_packs(plans);
    let mut profile = fixture_profile(&packs);
    profile.user_overlays = plans
        .iter()
        .flat_map(|plan| plan.ordered_overlays.values())
        .flatten()
        .filter(|overlay| overlay.owner.is_none() && overlay.hash.is_none())
        .map(|overlay| overlay.document.clone())
        .collect();
    Arc::new(
        tobkiri_profile::compile(&profile, &packs)
            .unwrap_or_else(|errors| panic!("test Profile did not compile: {errors:?}")),
    )
}

pub fn flow_key(profile: &CompiledProfile, source: &ResolvedFlow) -> String {
    profile
        .flows
        .iter()
        .find(|(_, candidate)| {
            candidate.owner == source.owner && candidate.document.id == source.document.id
        })
        .map(|(key, _)| key.clone())
        .unwrap_or_else(|| panic!("compiled test Flow {} is missing", source.document.id))
}

pub fn flow_handle(
    profile: &Arc<CompiledProfile>,
    key: &str,
    control: &CoreControl,
) -> ControlHandle<ResolvedFlow> {
    let catalog = control_handle(control, ControlKind::Catalog, || Arc::clone(profile));
    catalog.project(|profile| {
        profile
            .flows
            .get(key)
            .unwrap_or_else(|| panic!("compiled test Flow key {key} is missing"))
    })
}

pub fn active_frame(
    cx: &CallCx,
    flow: &ControlHandle<ResolvedFlow>,
) -> ControlHandle<dyn ActiveCodeFrame> {
    active_frame_with_budget(cx, flow, None)
}

pub fn active_frame_with_budget(
    cx: &CallCx,
    flow: &ControlHandle<ResolvedFlow>,
    allocation: Option<Arc<dyn AllocationBudget>>,
) -> ControlHandle<dyn ActiveCodeFrame> {
    let owner = CodeOwner::Pack {
        id: flow.get().owner.clone(),
        hash: flow.get().hash.clone(),
    };
    let principal = principal_for_owner(&owner);
    let context = cx
        .try_child(principal)
        .expect("bounded test frame context capacity");
    let flow_address = std::ptr::from_ref(flow.get()).addr();
    let identity = frame_identity(&flow.get().plan_digest, &CodeLocation::Flow);
    control_handle(cx.control(), ControlKind::Frame, || TestActiveCodeFrame {
        context,
        owner,
        code: CodeLocation::Flow,
        flow_address,
        identity,
        control: cx.control().clone(),
        allocation,
    })
    .project(|frame| frame as &dyn ActiveCodeFrame)
}

pub fn active_frame_for_provided(
    cx: &CallCx,
    flow: &ControlHandle<ResolvedFlow>,
) -> ControlHandle<dyn ActiveCodeFrame> {
    active_frame(cx, flow)
}

pub fn run_fixture(
    engine: &tobkiri_flow::FlowEngine,
    cx: CallCx,
    flow: &ControlHandle<ResolvedFlow>,
    input: tobkiri_canon::CoreValue,
    limits: LoopLimits,
) -> Result<tobkiri_flow::RunHandle, StepError> {
    engine.run(active_frame(&cx, flow), flow.clone(), input, limits)
}

pub fn raw_frame(
    cx: &CallCx,
    flow: &ControlHandle<ResolvedFlow>,
) -> ControlHandle<dyn ActiveCodeFrame> {
    active_frame(cx, flow)
}

pub fn raw_flow_handle(cx: &CallCx, flow: ResolvedFlow) -> ControlHandle<ResolvedFlow> {
    control_handle(cx.control(), ControlKind::Catalog, || flow)
}

#[derive(Clone)]
struct TestNodeProof {
    frame_identity: Digest,
    flow_address: usize,
    code: CodeLocation,
    step_address: Option<usize>,
    step_id: Option<String>,
    kind: FlowNodeKind,
    is_next: bool,
    witness_id: Option<Digest>,
    retired: Arc<AtomicBool>,
}

struct TestRetirement {
    _retired: Arc<AtomicBool>,
}

struct TestContinuation {
    base_context: CallCx,
    base_owner: CodeOwner,
    base_identity: Digest,
    base_code: CodeLocation,
    overlay_identity: Digest,
    overlay_code: CodeLocation,
    flow_address: usize,
    control: CoreControl,
    allocation: Option<Arc<dyn AllocationBudget>>,
    resumed: AtomicBool,
}

struct TestActiveCodeFrame {
    context: CallCx,
    owner: CodeOwner,
    code: CodeLocation,
    flow_address: usize,
    identity: Digest,
    control: CoreControl,
    allocation: Option<Arc<dyn AllocationBudget>>,
}

impl TestActiveCodeFrame {
    fn check_node<'a>(&self, node: &'a ReachedNode) -> Result<&'a TestNodeProof, StepError> {
        let proof = node
            .core_proof::<TestNodeProof>()
            .ok_or_else(|| StepError::new("denied", "test node proof is missing"))?;
        if proof.frame_identity != self.identity
            || proof.flow_address != self.flow_address
            || proof.retired.load(Ordering::Acquire)
        {
            return Err(StepError::new("denied", "test node proof is stale"));
        }
        Ok(proof)
    }

    fn expected_owner(flow: &ResolvedFlow, code: &CodeLocation) -> Option<CodeOwner> {
        match code {
            CodeLocation::Flow => Some(CodeOwner::Pack {
                id: flow.owner.clone(),
                hash: flow.hash.clone(),
            }),
            CodeLocation::UserOverlay { .. } => Some(CodeOwner::User),
            CodeLocation::PackOverlay { id } => flow
                .ordered_overlays
                .values()
                .flatten()
                .find(|overlay| {
                    overlay
                        .owner
                        .as_ref()
                        .is_some_and(|owner| id == &format!("{owner}/{}", overlay.document.id))
                })
                .and_then(|overlay| {
                    Some(CodeOwner::Pack {
                        id: overlay.owner.clone()?,
                        hash: overlay.hash.clone()?,
                    })
                }),
        }
    }
}

impl ActiveCodeFrame for TestActiveCodeFrame {
    fn context(&self) -> &CallCx {
        &self.context
    }

    fn code_owner(&self) -> &CodeOwner {
        &self.owner
    }

    fn reach_node(
        &self,
        cx: &CallCx,
        position: FlowNodePosition<'_>,
    ) -> Result<ReachedNode, StepError> {
        if std::ptr::from_ref(position.flow).addr() != self.flow_address
            || position.code != &self.code
            || cx.profile() != self.context.profile()
            || cx.request() != self.context.request()
            || cx.deadline() > self.context.deadline()
            || cx.principal() != self.context.principal()
            || Self::expected_owner(position.flow, position.code).as_ref() != Some(&self.owner)
        {
            return Err(StepError::new(
                "denied",
                "test frame does not match this frozen Flow node",
            ));
        }
        let step_address = position.step.map(|step| std::ptr::from_ref(step).addr());
        let step_id = position.step.and_then(|step| step.id.clone());
        match (position.kind, position.witness, position.step) {
            (FlowNodeKind::PortCall, Some(witness), Some(step)) => {
                if !has_frozen_witness(position.flow, witness)
                    || witness.owner() != &self.owner
                    || witness.key().code != *position.code
                    || witness.key().node != step.id.as_deref().unwrap_or_default()
                    || witness.key().kind != CallNodeKind::Call
                    || !matches!(&step.kind, StepKind::Call(call) if call.shape == *witness.shape() && call.op == *witness.op())
                {
                    return Err(StepError::new(
                        "denied",
                        "compiler witness does not match this Flow call",
                    ));
                }
            }
            (FlowNodeKind::StreamEmit, Some(witness), Some(step)) => {
                if !has_frozen_witness(position.flow, witness)
                    || witness.owner() != &self.owner
                    || witness.key().code != *position.code
                    || witness.key().node != step.id.as_deref().unwrap_or_default()
                    || witness.key().kind != CallNodeKind::StreamEmit
                    || witness.op().as_str() != "emit"
                    || step.stream.is_none()
                {
                    return Err(StepError::new(
                        "denied",
                        "compiler witness does not match this Flow stream route",
                    ));
                }
            }
            (FlowNodeKind::PortCall | FlowNodeKind::StreamEmit, _, _) => {
                return Err(StepError::new("denied", "Flow effect witness is missing"));
            }
            (_, Some(_), _) => {
                return Err(StepError::new(
                    "denied",
                    "non-effect Flow node carries an unexpected witness",
                ));
            }
            _ => (),
        }
        let proof_reservation =
            control_reservation::<TestNodeProof>(&self.control, ControlKind::Frame);
        let retirement_reservation =
            control_reservation::<TestRetirement>(&self.control, ControlKind::Frame);
        let retired = Arc::new(AtomicBool::new(false));
        let proof = prepaid_handle(
            TestNodeProof {
                frame_identity: self.identity.clone(),
                flow_address: self.flow_address,
                code: position.code.clone(),
                step_address,
                step_id,
                kind: position.kind,
                is_next: position
                    .step
                    .is_some_and(|step| matches!(&step.kind, StepKind::Next(_))),
                witness_id: position.witness.map(|witness| witness.id().clone()),
                retired: retired.clone(),
            },
            proof_reservation,
        )
        .project(|proof| proof as &(dyn Any + Send + Sync));
        let retirement =
            prepaid_handle(TestRetirement { _retired: retired }, retirement_reservation)
                .project(|retirement| retirement as &(dyn Any + Send + Sync));
        let witness = position
            .witness
            .map(|witness| control_handle(&self.control, ControlKind::Frame, || witness.clone()));
        Ok(ReachedNode::from_prepaid(proof, retirement, witness))
    }

    fn budget_at_reached_node(
        &self,
        cx: &CallCx,
        node: &ReachedNode,
        _reason: AllocationReason,
    ) -> Result<Arc<dyn AllocationBudget>, StepError> {
        self.check_node(node)?;
        if cx.profile() != self.context.profile() || cx.request() != self.context.request() {
            return Err(StepError::new("denied", "test allocation context mismatch"));
        }
        Ok(self.allocation.clone().unwrap_or_else(budget))
    }

    fn retire_node(&self, node: &ReachedNode) {
        if let Some(proof) = node.core_proof::<TestNodeProof>()
            && proof.frame_identity == self.identity
            && proof.flow_address == self.flow_address
        {
            proof.retired.store(true, Ordering::Release);
        }
    }

    fn compiled_activation(
        &self,
        _cx: &CallCx,
        _node: &ReachedNode,
        _arguments_digest: &Digest,
        _target: &tobkiri_profile::FrozenTarget,
    ) -> Result<CompiledActivation, StepError> {
        Err(StepError::new(
            "unavailable",
            "test frame does not mint Gate activations",
        ))
    }

    fn enter_verified_overlay(
        &self,
        cx: &CallCx,
        caller_node: &ReachedNode,
        position: OverlayPosition<'_>,
    ) -> Result<OverlayTransition, StepError> {
        let caller = self.check_node(caller_node)?;
        let overlays = position
            .flow
            .ordered_overlays
            .get(position.step.id.as_deref().unwrap_or_default())
            .ok_or_else(|| StepError::new("denied", "test overlay chain is missing"))?;
        if std::ptr::from_ref(position.flow).addr() != self.flow_address
            || caller.step_address != Some(std::ptr::from_ref(position.step).addr())
            || !std::ptr::eq(
                overlays.get(position.index).ok_or_else(|| {
                    StepError::new("denied", "test overlay index is outside the frozen chain")
                })?,
                position.overlay,
            )
        {
            return Err(StepError::new(
                "denied",
                "test overlay does not match the frozen chain position",
            ));
        }
        let (owner, principal) = match (&position.overlay.owner, &position.overlay.hash) {
            (Some(id), Some(hash)) => (
                CodeOwner::Pack {
                    id: id.clone(),
                    hash: hash.clone(),
                },
                Principal::Pack {
                    pack: id.clone(),
                    hash: hash.clone(),
                },
            ),
            (None, None) => (CodeOwner::User, Principal::User),
            _ => {
                return Err(StepError::new(
                    "invalid",
                    "test overlay has inconsistent owner metadata",
                ));
            }
        };
        let context = cx
            .try_child(principal)
            .map_err(|error| StepError::new("limit-exceeded", error.to_string()))?;
        let overlay_id = position.overlay.owner.as_ref().map_or_else(
            || format!("user:{}", position.overlay.document.id),
            |owner| format!("{owner}/{}", position.overlay.document.id),
        );
        let code = if position.overlay.owner.is_some() {
            CodeLocation::PackOverlay { id: overlay_id }
        } else {
            CodeLocation::UserOverlay { id: overlay_id }
        };
        let identity = frame_identity(position.overlay.ast_hash(), &code);
        let overlay_identity = identity.clone();
        let overlay_code = code.clone();
        let flow_address = self.flow_address;
        let control = self.control.clone();
        let frame = control_handle(&self.control, ControlKind::Frame, || TestActiveCodeFrame {
            context,
            owner,
            code,
            flow_address,
            identity,
            control,
            allocation: self.allocation.clone(),
        })
        .project(|frame| frame as &dyn ActiveCodeFrame);
        let base_context = self
            .context
            .try_clone_precharged()
            .map_err(|error| StepError::new("limit-exceeded", error.to_string()))?;
        let base_owner = self.owner.clone();
        let base_identity = self.identity.clone();
        let base_code = self.code.clone();
        let flow_address = self.flow_address;
        let control = self.control.clone();
        let continuation = control_handle(&self.control, ControlKind::Frame, || TestContinuation {
            base_context,
            base_owner,
            base_identity,
            base_code,
            overlay_identity,
            overlay_code,
            flow_address,
            control,
            allocation: self.allocation.clone(),
            resumed: AtomicBool::new(false),
        })
        .project(|continuation| continuation as &(dyn Any + Send + Sync));
        Ok(OverlayTransition {
            frame,
            continuation: FrameContinuation::from_prepaid(continuation),
        })
    }

    fn resume_captured_base(
        &self,
        cx: &CallCx,
        overlay_node: &ReachedNode,
        continuation: &FrameContinuation,
    ) -> Result<ControlHandle<dyn ActiveCodeFrame>, StepError> {
        let proof = self.check_node(overlay_node)?;
        let continuation = continuation
            .core_proof::<TestContinuation>()
            .ok_or_else(|| StepError::new("denied", "test continuation proof is missing"))?;
        if !proof.is_next
            || proof.frame_identity != continuation.overlay_identity
            || proof.code != continuation.overlay_code
            || continuation.flow_address != self.flow_address
            || cx.profile() != self.context.profile()
            || cx.request() != self.context.request()
        {
            return Err(StepError::new("denied", "test continuation is stale"));
        }
        continuation
            .resumed
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .map_err(|_| StepError::new("denied", "test continuation was already resumed"))?;
        let context = continuation
            .base_context
            .try_clone_precharged()
            .map_err(|error| StepError::new("limit-exceeded", error.to_string()))?;
        let owner = continuation.base_owner.clone();
        let code = continuation.base_code.clone();
        let flow_address = continuation.flow_address;
        let identity = continuation.base_identity.clone();
        let control = continuation.control.clone();
        Ok(
            control_handle(&self.control, ControlKind::Frame, || TestActiveCodeFrame {
                context,
                owner,
                code,
                flow_address,
                identity,
                control,
                allocation: continuation.allocation.clone(),
            })
            .project(|frame| frame as &dyn ActiveCodeFrame),
        )
    }

    fn identity(&self) -> Digest {
        self.identity.clone()
    }
}

fn frame_identity(plan: &Digest, code: &CodeLocation) -> Digest {
    let nonce = NEXT_TEST_FRAME_ID.fetch_add(1, Ordering::Relaxed);
    Digest::hash(format!("test-frame\0{plan}\0{code:?}\0{nonce}").as_bytes())
}

fn principal_for_owner(owner: &CodeOwner) -> Principal {
    match owner {
        CodeOwner::Pack { id, hash } => Principal::Pack {
            pack: id.clone(),
            hash: hash.clone(),
        },
        CodeOwner::User => Principal::User,
    }
}

fn has_frozen_witness(flow: &ResolvedFlow, witness: &CompiledCallWitness) -> bool {
    flow.call_witnesses
        .get(witness.key())
        .is_some_and(|frozen| frozen.id() == witness.id())
}

pub fn check_reached_call<'a>(
    frame: &ControlHandle<dyn ActiveCodeFrame>,
    node: &'a ReachedNode,
) -> Result<&'a CompiledCallWitness, StepError> {
    let proof = node
        .core_proof::<TestNodeProof>()
        .ok_or_else(|| StepError::new("denied", "test node proof is missing"))?;
    if proof.frame_identity != frame.identity()
        || proof.retired.load(Ordering::Acquire)
        || !matches!(
            proof.kind,
            FlowNodeKind::PortCall | FlowNodeKind::StreamEmit
        )
    {
        return Err(StepError::new("denied", "test reached-node proof is stale"));
    }
    let witness = node
        .witness()
        .ok_or_else(|| StepError::new("denied", "test call witness is missing"))?;
    if Some(witness.id()) != proof.witness_id.as_ref()
        || witness.owner() != frame.code_owner()
        || witness.key().code != proof.code
        || witness.key().node != proof.step_id.as_deref().unwrap_or_default()
        || witness.targets().len() != 1
        || witness.targets()[0].pack != PackId::parse("local.provider").unwrap()
        || witness.targets()[0].provided != *witness.shape()
        || witness.key().kind
            != if proof.kind == FlowNodeKind::StreamEmit {
                CallNodeKind::StreamEmit
            } else {
                CallNodeKind::Call
            }
        || proof.step_address.is_none()
    {
        return Err(StepError::new(
            "denied",
            "test compiler witness does not match reached activation",
        ));
    }
    Ok(witness)
}

fn fixture_packs(plans: &[ResolvedFlow]) -> Vec<VerifiedPack> {
    let mut operations = BTreeSet::from(["emit".to_owned(), "read".to_owned(), "run".to_owned()]);
    let mut grouped: BTreeMap<PackId, Vec<(String, FlowDocument)>> = BTreeMap::new();
    for plan in plans {
        let mut document = plan.document.clone();
        document.trigger.input = input_shape();
        normalize_steps(&mut document.steps, &mut operations);
        for (step_id, chain) in &plan.ordered_overlays {
            open_target(&mut document.steps, step_id, &role_shape());
            for overlay in chain {
                collect_steps(&overlay.document.body, &mut operations);
            }
        }
        grouped
            .entry(plan.owner.clone())
            .or_default()
            .push((document.id.clone(), document));
    }

    let fixture_owner = PackId::parse("local.fixture").unwrap();
    let fixture_flows = grouped.remove(&fixture_owner).unwrap_or_default();
    let mut packs = vec![
        provider_pack(),
        flow_pack(fixture_owner, fixture_flows, Some(&operations)),
    ];
    for (owner, flows) in grouped {
        packs.push(flow_pack(owner, flows, None));
    }
    packs
}

fn normalize_steps(steps: &mut [Step], operations: &mut BTreeSet<String>) {
    for step in steps {
        match &mut step.kind {
            StepKind::Call(call) => {
                operations.insert(call.op.to_string());
            }
            StepKind::Flow(_) => (),
            StepKind::Branch(branch) => {
                normalize_steps(&mut branch.then, operations);
                normalize_steps(&mut branch.otherwise, operations);
            }
            StepKind::Parallel(parallel) => {
                normalize_steps(&mut parallel.body, operations);
            }
            StepKind::Loop(loop_step) => {
                normalize_steps(&mut loop_step.body, operations);
            }
            StepKind::Return(_) | StepKind::Next(_) => (),
        }
        if let Some(route) = &step.stream {
            operations.insert("emit".into());
            let _ = &route.emit;
        }
    }
}

fn collect_steps(steps: &[Step], operations: &mut BTreeSet<String>) {
    for step in steps {
        match &step.kind {
            StepKind::Call(call) => {
                operations.insert(call.op.to_string());
            }
            StepKind::Branch(branch) => {
                collect_steps(&branch.then, operations);
                collect_steps(&branch.otherwise, operations);
            }
            StepKind::Parallel(parallel) => collect_steps(&parallel.body, operations),
            StepKind::Loop(loop_step) => collect_steps(&loop_step.body, operations),
            StepKind::Flow(_) | StepKind::Return(_) | StepKind::Next(_) => (),
        }
        if step.stream.is_some() {
            operations.insert("emit".into());
        }
    }
}

fn open_target(steps: &mut [Step], id: &str, role: &ShapeRef) {
    for step in steps {
        if step.id.as_deref() == Some(id) {
            step.overlay = tobkiri_profile::OverlayAccess::Open;
            step.role = Some(role.clone());
        }
        match &mut step.kind {
            StepKind::Branch(branch) => {
                open_target(&mut branch.then, id, role);
                open_target(&mut branch.otherwise, id, role);
            }
            StepKind::Parallel(parallel) => open_target(&mut parallel.body, id, role),
            StepKind::Loop(loop_step) => open_target(&mut loop_step.body, id, role),
            _ => (),
        }
    }
}

fn fixture_profile(packs: &[VerifiedPack]) -> Profile {
    let mut permissions = tobkiri_gate::PermissionSet::default();
    permissions.ports.extend([
        port_shape().to_string(),
        flow_shape().to_string(),
        child_flow_shape().to_string(),
        role_shape().to_string(),
    ]);
    permissions.loops = LoopLimits {
        max_iterations: 8,
        max_wall_ms: 1000,
        max_cost_units: 100,
    };
    let user_permissions = permissions.clone();
    Profile {
        format: "tobkiri.profile/1".into(),
        id: tobkiri_canon::ids::ProfileId::parse("prof_test").unwrap(),
        name: "Flow interpreter test fixture".into(),
        packs: packs
            .iter()
            .map(|pack| tobkiri_profile::PackPin {
                id: pack.manifest.id.clone(),
                version: pack.manifest.version.clone(),
                sha256: pack.hash.clone(),
                source: format!("fixture:{}", pack.manifest.id),
            })
            .collect(),
        publishers: BTreeMap::new(),
        ceiling: permissions.clone(),
        user_permissions: Some(user_permissions),
        pack_limits: packs
            .iter()
            .map(|pack| {
                (
                    pack.manifest.id.clone(),
                    pack.manifest.permissions.intersect(&permissions),
                )
            })
            .collect(),
        bindings: BTreeMap::new(),
        overlay_order: Vec::new(),
        user_overlays: Vec::new(),
        startup: tobkiri_profile::Startup {
            inputs: Vec::new(),
            ui: None,
        },
    }
}

fn contract_assets(
    operations: &BTreeSet<String>,
) -> (BTreeMap<String, Vec<u8>>, Vec<ShapeDefinition>) {
    let mut wit = String::from("package local:fixture@1.0.0; interface io { ");
    for operation in operations {
        if operation == "stream" {
            wit.push_str("%stream");
        } else {
            wit.push_str(operation);
        }
        wit.push_str(": func(); ");
    }
    wit.push_str(
        "} interface input { read: func(); } interface role {} interface workflow { run: func(); } interface child-workflow { run: func(); }",
    );
    let mut files = BTreeMap::from([("wit/contract.wit".into(), wit.into_bytes())]);
    let shapes = [
        (port_shape(), ShapeKind::Port, "io"),
        (input_shape(), ShapeKind::Port, "input"),
        (role_shape(), ShapeKind::Role, "role"),
        (flow_shape(), ShapeKind::Flow, "workflow"),
        (child_flow_shape(), ShapeKind::Flow, "child-workflow"),
    ]
    .into_iter()
    .map(|(reference, kind, interface)| {
        files.insert(
            format!("wit/{interface}.shape.json"),
            br#"{"cardinality":"one","streams":{},"callbacks":{}}"#.to_vec(),
        );
        ShapeDefinition {
            name: reference.name,
            version: reference.version,
            kind,
            wit: "wit/contract.wit".into(),
        }
    })
    .collect();
    (files, shapes)
}

fn provider_pack() -> VerifiedPack {
    verified_pack(
        "local.provider",
        BTreeMap::new(),
        Vec::new(),
        Ports {
            provides: vec![port_declaration(&port_shape())],
            consumes: Vec::new(),
            untyped: Vec::new(),
        },
        Vec::new(),
    )
}

fn flow_pack(
    owner: PackId,
    flows: Vec<(String, FlowDocument)>,
    operations: Option<&BTreeSet<String>>,
) -> VerifiedPack {
    let mut files = BTreeMap::new();
    let mut paths = Vec::new();
    for (id, flow) in flows {
        let path = format!("flows/{id}.json");
        files.insert(path.clone(), serde_json::to_vec(&flow).unwrap());
        paths.push(path);
    }
    let shapes = operations.map_or_else(Vec::new, |operations| {
        let (contract_files, shapes) = contract_assets(operations);
        files.extend(contract_files);
        shapes
    });
    let mut permissions = tobkiri_gate::PermissionSet::default();
    permissions
        .ports
        .extend([port_shape().to_string(), flow_shape().to_string()]);
    if owner.as_str() == "local.fixture" {
        permissions.ports.insert(child_flow_shape().to_string());
    }
    let mut pack = verified_pack(
        owner.as_str(),
        files,
        shapes,
        Ports {
            provides: Vec::new(),
            consumes: vec![port_declaration(&port_shape())],
            untyped: Vec::new(),
        },
        paths,
    );
    pack.manifest.permissions = permissions;
    // Re-verify after adding the source Pack's explicit permission declaration.
    verify_tpk(
        &build_tpk(&pack.manifest, &pack.files, None).unwrap(),
        &BTreeMap::new(),
    )
    .unwrap()
}

fn verified_pack(
    id: &str,
    files: BTreeMap<String, Vec<u8>>,
    shapes: Vec<ShapeDefinition>,
    ports: Ports,
    flows: Vec<String>,
) -> VerifiedPack {
    let mut permissions = tobkiri_gate::PermissionSet::default();
    if id == "local.fixture" || id == "local.other" {
        permissions
            .ports
            .extend([port_shape().to_string(), flow_shape().to_string()]);
    }
    if id == "local.fixture" {
        permissions.ports.insert(child_flow_shape().to_string());
    }
    let manifest = PackManifest {
        format: "tobkiri.pack/1".into(),
        id: PackId::parse(id).unwrap(),
        version: "1.0.0".into(),
        publisher_key: None,
        runtime: RuntimeSpec::Data,
        shapes,
        ports,
        inputs: Vec::new(),
        files: files
            .iter()
            .map(|(path, bytes)| FileEntry {
                path: path.clone(),
                sha256: Digest::hash(bytes),
                size: bytes.len() as u64,
            })
            .collect(),
        flows,
        overlays: Vec::new(),
        stores: Vec::new(),
        permissions,
        ui: None,
    };
    verify_tpk(
        &build_tpk(&manifest, &files, None).unwrap(),
        &BTreeMap::new(),
    )
    .unwrap()
}

fn port_declaration(reference: &ShapeRef) -> PortDeclaration {
    PortDeclaration {
        shape: reference.name.clone(),
        version: reference.version,
        optional: false,
    }
}

pub fn port_shape() -> ShapeRef {
    ShapeRef::parse("local.fixture.io@1.0").unwrap()
}

pub fn input_shape() -> ShapeRef {
    ShapeRef::parse("local.fixture.input@1.0").unwrap()
}

pub fn role_shape() -> ShapeRef {
    ShapeRef::parse("local.fixture.role@1.0").unwrap()
}

pub fn flow_shape() -> ShapeRef {
    ShapeRef::parse("local.fixture.workflow@1.0").unwrap()
}

pub fn child_flow_shape() -> ShapeRef {
    ShapeRef::parse("local.fixture.child-workflow@1.0").unwrap()
}
