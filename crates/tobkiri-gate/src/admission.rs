//! Private Core dispatch authority. Descriptors describe immutable compiled
//! code; only a Core-held execution factory can mint a reached activation.
//! Neither descriptors nor permits are exposed through WIT or payload fields.
use crate::protocol::strict_bytes;
use crate::*;
use std::{
    collections::{BTreeMap, BTreeSet},
    mem::size_of,
    sync::{
        Arc, Mutex, Weak,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
    time::Instant,
};
use tobkiri_canon::{
    Digest,
    ids::{OpName, PackHash, PackId, ProfileId, RequestId, ShapeRef},
};

#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct FrozenPortTarget {
    pub pack: PackId,
    pub hash: PackHash,
    pub provided: ShapeRef,
}
/// Immutable topology for one actual verified `manifest.flows` entry.
/// Registration identifies a possible endpoint; it does not grant FlowRun.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FrozenFlowTarget {
    pub profile: ProfileId,
    pub revision: Digest,
    pub owner: PackId,
    pub owner_hash: PackHash,
    pub flow_key: String,
    /// StrictV1 digest of the verified FlowDocument itself.
    pub flow_ast: Digest,
    /// Compiler-frozen plan identity for this exact Flow.
    pub plan_digest: Digest,
    pub implements: ShapeRef,
    pub input_view: ShapeRef,
    /// Core-verified signature of the exact Flow trigger input view.
    pub entry_signature: Digest,
}
impl FrozenFlowTarget {
    /// Stable target identity used by every FlowRun grant and frame proof.
    pub fn digest(&self) -> Result<Digest, GateError> {
        Ok(Digest::hash(&strict_bytes(&(
            "tobkiri-frozen-flow-target-v1",
            &self.profile,
            &self.revision,
            &self.owner,
            &self.owner_hash,
            &self.flow_key,
            &self.flow_ast,
            &self.plan_digest,
            &self.implements,
            &self.input_view,
            &self.entry_signature,
        ))?))
    }
    pub fn clone_storage_bytes(&self) -> Result<usize, tobkiri_canon::AllocationError> {
        self.profile
            .clone_storage_bytes()
            .checked_add(self.owner.clone_storage_bytes())
            .and_then(|bytes| bytes.checked_add(self.flow_key.capacity()))
            .and_then(|bytes| bytes.checked_add(self.implements.clone_storage_bytes()))
            .and_then(|bytes| bytes.checked_add(self.input_view.clone_storage_bytes()))
            .ok_or(tobkiri_canon::AllocationError::Overflow)
    }
}
/// Revision-scoped frozen Flow topology retained under CoreControl.
pub struct FlowEndpointBindings {
    profile: ProfileId,
    revision: Digest,
    endpoints: Vec<FrozenFlowTarget>,
    registrations: AtomicUsize,
    _reservation: ControlReservation,
}
/// Read-only handle to one immutable Flow catalog registration.
/// It conveys no authority and is not a permit. The exact revision catalog
/// remains registered while at least one owning Core factory holds a token.
pub struct FlowEndpointRegistration {
    catalog: Arc<FlowEndpointBindings>,
    catalog_weak: Weak<FlowEndpointBindings>,
    registry: Weak<Mutex<BTreeMap<ScopeKey, Weak<FlowEndpointBindings>>>>,
    key: ScopeKey,
    _reservation: ControlReservation,
}
impl FlowEndpointRegistration {
    pub fn profile(&self) -> &ProfileId {
        &self.catalog.profile
    }
    pub fn revision(&self) -> &Digest {
        &self.catalog.revision
    }
    pub fn endpoints(&self) -> &[FrozenFlowTarget] {
        &self.catalog.endpoints
    }
}
impl Drop for FlowEndpointRegistration {
    fn drop(&mut self) {
        let Some(registry) = self.registry.upgrade() else {
            return;
        };
        let Ok(mut registry) = registry.lock() else {
            // The catalog itself still drops its bounded reservation. A stale
            // weak map entry is harmless and can be replaced on next register.
            let _ = self.catalog.registrations.fetch_update(
                Ordering::AcqRel,
                Ordering::Acquire,
                |count| count.checked_sub(1),
            );
            return;
        };
        let previous = self
            .catalog
            .registrations
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |count| {
                count.checked_sub(1)
            })
            .ok();
        if previous == Some(1)
            && registry
                .get(&self.key)
                .is_some_and(|catalog| catalog.ptr_eq(&self.catalog_weak))
        {
            registry.remove(&self.key);
        }
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum CompiledCodeOwner {
    Pack {
        pack: PackId,
        hash: PackHash,
    },
    User {
        profile_ast: Digest,
    },
    /// Real registered User policy document identity for preclaim actions.
    UserDocument {
        document: Digest,
    },
}
impl CompiledCodeOwner {
    pub fn clone_storage_bytes(&self) -> usize {
        match self {
            Self::Pack { pack, .. } => pack.clone_storage_bytes(),
            Self::User { .. } | Self::UserDocument { .. } => 0,
        }
    }
    pub fn try_clone_precharged(&self) -> Result<Self, tobkiri_canon::AllocationError> {
        Ok(match self {
            Self::Pack { pack, hash } => Self::Pack {
                pack: pack.try_clone_precharged()?,
                hash: hash.clone(),
            },
            Self::User { profile_ast } => Self::User {
                profile_ast: profile_ast.clone(),
            },
            Self::UserDocument { document } => Self::UserDocument {
                document: document.clone(),
            },
        })
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CompiledNodeKind {
    StepCall,
    FixedStreamEmit,
}
/// Trusted Core copies these metadata from A's sealed compiler witness. This
/// input alone has no authority; guest data is never accepted by registration.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CompiledCallDescriptor {
    pub id: Digest,
    pub ast_hash: Digest,
    pub owner: CompiledCodeOwner,
    pub revision: Digest,
    pub flow: String,
    pub node: String,
    pub kind: CompiledNodeKind,
    pub shape: ShapeRef,
    pub op: OpName,
    pub targets: BTreeSet<FrozenPortTarget>,
}
type ScopeKey = (ProfileId, Digest);
/// Exact requested views and their frozen provider targets, supplied by Core.
pub type PortBindings = BTreeMap<ShapeRef, BTreeSet<FrozenPortTarget>>;
struct CompiledScope {
    profile: ProfileId,
    revision: Digest,
    factory_identity: Digest,
    registration_token: u64,
    gate_identity: Arc<()>,
    closed: AtomicBool,
    descriptors: BTreeMap<Digest, CompiledCallDescriptor>,
    _reservation: ControlReservation,
}
struct FrozenBindings {
    bindings: PortBindings,
    _reservation: ControlReservation,
}
#[derive(Default)]
pub(crate) struct AdmissionRegistry {
    bindings: Mutex<BTreeMap<ScopeKey, FrozenBindings>>,
    flow_endpoints: Arc<Mutex<BTreeMap<ScopeKey, Weak<FlowEndpointBindings>>>>,
}
/// Core retains this factory privately beside its immutable compiled plan. It
/// must never be handed to a guest, UI payload, callee or callback. Dropping
/// the noncloneable authority closes exactly this registration.
pub struct CompiledCallAuthority {
    scope: Arc<CompiledScope>,
}
impl CompiledCallAuthority {
    pub fn factory_identity(&self) -> &Digest {
        &self.scope.factory_identity
    }
    pub fn registration_token(&self) -> u64 {
        self.scope.registration_token
    }
}
impl Drop for CompiledCallAuthority {
    fn drop(&mut self) {
        self.scope.closed.store(true, Ordering::Release);
    }
}
pub struct CompiledExecution {
    scope: Arc<CompiledScope>,
    binding: ContextBinding,
}
/// A fresh reached-node activation; possessing a reusable descriptor cannot
/// construct this type. Each actual stream forwarding mints a fresh activation.
pub struct CompiledActivation {
    scope: Arc<CompiledScope>,
    binding: ContextBinding,
    descriptor: Digest,
    frame: Digest,
    activation: u64,
    target: FrozenPortTarget,
    frame_scope: AllocationFrameScope,
}
#[derive(Clone)]
pub(crate) struct ContextBinding(Arc<BoundContext>);
struct BoundContext {
    profile: ProfileId,
    principal: Principal,
    request: RequestId,
    revision: Digest,
    epoch: u64,
    deadline: Instant,
    cancel: CancellationToken,
    limits: ResourceLimits,
    budget: BudgetContext,
    _reservation: ControlReservation,
}
impl ContextBinding {
    pub(crate) fn check_borrowed(bound: &CallCx, cx: &CallCx) -> Result<(), GateError> {
        bound.check_active()?;
        cx.check_active()?;
        let budget = bound.budget().ok_or(GateError::Unavailable)?;
        if bound.profile() != cx.profile()
            || bound.principal() != cx.principal()
            || bound.request() != cx.request()
            || bound.revision() != cx.revision()
            || bound.security_epoch() != cx.security_epoch()
            || cx.deadline() > bound.deadline()
            || !cx.limits().is_subset_of(bound.limits())
            || cx.budget().is_none_or(|current| !current.inherits(budget))
        {
            return Err(GateError::Denied("funded context binding mismatch".into()));
        }
        Ok(())
    }
    pub(crate) fn new(cx: &CallCx) -> Result<Self, GateError> {
        cx.check_active()?;
        let revision = cx
            .revision()
            .ok_or_else(|| GateError::Denied("admission requires frozen revision".into()))?;
        let budget = cx.budget().ok_or_else(|| {
            GateError::Denied("admission requires inherited execution budget".into())
        })?;
        let budget_bytes = usize::try_from(
            budget
                .clone_storage_bytes()
                .map_err(|_| GateError::Unavailable)?,
        )
        .map_err(|_| GateError::Limit("context binding size overflow".into()))?;
        let dynamic_bytes = cx
            .profile()
            .clone_storage_bytes()
            .checked_add(
                cx.principal()
                    .clone_storage_bytes()
                    .map_err(|_| GateError::Unavailable)?,
            )
            .and_then(|bytes| bytes.checked_add(cx.request().clone_storage_bytes()))
            .and_then(|bytes| bytes.checked_add(budget_bytes))
            .and_then(|bytes| {
                bytes
                    .checked_add(size_of::<BoundContext>())
                    .and_then(|n| n.checked_add(size_of::<ControlReservation>()))
            })
            .ok_or_else(|| GateError::Limit("context binding size overflow".into()))?;
        let reservation = cx.control().reserve(
            ControlKind::Context,
            u64::try_from(dynamic_bytes)
                .map_err(|_| GateError::Limit("context binding size overflow".into()))?,
        )?;
        let record = BoundContext {
            profile: cx
                .profile()
                .try_clone_precharged()
                .map_err(|_| GateError::Unavailable)?,
            principal: cx
                .principal()
                .try_clone_precharged()
                .map_err(|_| GateError::Unavailable)?,
            request: cx
                .request()
                .try_clone_precharged()
                .map_err(|_| GateError::Unavailable)?,
            revision: revision.clone(),
            epoch: cx.security_epoch(),
            deadline: cx.deadline(),
            cancel: cx.cancel().clone(),
            limits: cx.limits().clone(),
            budget: budget
                .try_clone_precharged()
                .map_err(|_| GateError::Unavailable)?,
            _reservation: reservation,
        };
        Ok(Self(Arc::new(record)))
    }
    pub(crate) fn check(&self, cx: &CallCx) -> Result<(), GateError> {
        let this = self.0.as_ref();
        cx.check_active()?;
        if this.cancel.is_cancelled() {
            return Err(GateError::Cancelled);
        }
        if Instant::now() >= this.deadline {
            return Err(GateError::Deadline);
        }
        if this.profile != *cx.profile()
            || this.principal != *cx.principal()
            || this.request != *cx.request()
            || cx.revision() != Some(&this.revision)
            || this.epoch != cx.security_epoch()
            || cx.deadline() > this.deadline
            || !cx.limits().is_subset_of(&this.limits)
            || cx
                .budget()
                .is_none_or(|budget| !budget.inherits(&this.budget))
        {
            return Err(GateError::Denied(
                "admission context binding mismatch".into(),
            ));
        }
        this.budget.check()?;
        Ok(())
    }
}
impl CompiledExecution {
    /// Legacy digest-only activation is unavailable: use Gate::reach_compiled
    /// with the same authenticated Core proof used for allocation.
    pub fn reach(
        &self,
        _cx: &CallCx,
        _descriptor: &Digest,
        _frame: Digest,
        _target: FrozenPortTarget,
    ) -> Result<CompiledActivation, GateError> {
        Err(GateError::Unavailable)
    }
}
/// Keeps the original unconsumed Grant through UI outer checks and final
/// dispatch. It is deliberately noncloneable and has no payload constructor.
pub struct PortAdmission {
    grant: Grant,
    binding: ContextBinding,
    target: FrozenPortTarget,
    // Only Some carries the same sealed compiled activation; None remains
    // ordinary dynamic admission. The proof stays in the existing envelope.
    origin: Option<CompiledActivation>,
}
impl std::fmt::Debug for PortAdmission {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("PortAdmission")
            .field("grant", self.grant.id())
            .finish_non_exhaustive()
    }
}
impl PortAdmission {
    pub fn id(&self) -> &GrantId {
        self.grant.id()
    }
    pub fn target(&self) -> &FrozenPortTarget {
        &self.target
    }
    pub fn effect(&self) -> &EffectRequest {
        self.grant.effect()
    }
    pub fn args_digest(&self) -> &Digest {
        self.grant.args_digest()
    }
    /// False only for the sealed, reached compiled descriptor. Callee contexts
    /// and guest imports never inherit this structural exception.
    pub fn requires_dynamic_consumes(&self) -> bool {
        self.origin.is_none()
    }
}
/// Final dispatcher owns this one consumed dispatch and its one terminal result.
pub struct AdmittedDispatch {
    permit: DispatchPermit,
    target: FrozenPortTarget,
    _charge: ChargedUnit,
}
impl AdmittedDispatch {
    pub fn permit(&self) -> &DispatchPermit {
        &self.permit
    }
    pub fn target(&self) -> &FrozenPortTarget {
        &self.target
    }
    pub fn into_permit(self) -> DispatchPermit {
        self.permit
    }
    /// Transfer the single consumed permit and its exact frozen target into
    /// Core's private provider-entry bridge. The physical cost was charged at
    /// claim and cannot be refunded by dropping this wrapper.
    pub fn into_parts(self) -> (DispatchPermit, FrozenPortTarget) {
        (self.permit, self.target)
    }
}

impl GateEngine {
    pub fn reach_compiled(
        &self,
        cx: &CallCx,
        execution: &CompiledExecution,
        frame_scope: AllocationFrameScope,
        descriptor: &Digest,
        target: FrozenPortTarget,
    ) -> Result<CompiledActivation, GateError> {
        execution.binding.check(cx)?;
        self.check_scope(&execution.scope, cx)?;
        let metadata = self.authenticate_frame_scope(cx, &frame_scope)?;
        if frame_scope.factory_identity() != &execution.scope.factory_identity {
            return Err(GateError::Denied(
                "compiled registration belongs to another Core factory".into(),
            ));
        }
        let witness = execution
            .scope
            .descriptors
            .get(descriptor)
            .ok_or_else(|| GateError::Denied("unknown compiled call descriptor".into()))?;
        if metadata.node != *descriptor
            || metadata.owner != witness.owner
            || metadata.revision != witness.revision
            || !witness.targets.contains(&target)
        {
            return Err(GateError::Denied(
                "compiled activation differs from reached Core proof".into(),
            ));
        }
        let frame = metadata.frame.clone();
        let activation = metadata.activation;
        Ok(CompiledActivation {
            scope: execution.scope.clone(),
            binding: ContextBinding::new(cx)?,
            descriptor: descriptor.clone(),
            frame,
            activation,
            target,
            frame_scope,
        })
    }
    pub(crate) fn frozen_target_registered(
        &self,
        profile: &ProfileId,
        revision: &Digest,
        view: &ShapeRef,
        target: &FrozenPortTarget,
    ) -> Result<bool, GateError> {
        Ok(self
            .admissions
            .bindings
            .lock()
            .map_err(|_| GateError::Persistence("port binding registry poisoned".into()))?
            .get(&(profile.clone(), revision.clone()))
            .and_then(|record| record.bindings.get(view))
            .is_some_and(|targets| targets.contains(target)))
    }
    pub fn register_port_bindings(
        &self,
        profile: &ProfileId,
        revision: &Digest,
        bindings: PortBindings,
    ) -> Result<(), GateError> {
        let bytes = port_bindings_storage_bytes(profile, revision, &bindings)?;
        let mut registry = self
            .admissions
            .bindings
            .lock()
            .map_err(|_| GateError::Persistence("port binding registry poisoned".into()))?;
        let key = (profile.clone(), revision.clone());
        if let Some(old) = registry.get(&key) {
            if old.bindings != bindings {
                return Err(GateError::Denied(
                    "frozen port bindings are immutable".into(),
                ));
            }
            return Ok(());
        }
        if registry.len() >= 4096 {
            return Err(GateError::Limit("port binding revision capacity".into()));
        }
        let reservation = self.control().reserve(ControlKind::Catalog, bytes)?;
        registry.insert(
            key,
            FrozenBindings {
                bindings,
                _reservation: reservation,
            },
        );
        Ok(())
    }

    /// Resolve an exact operation to the sole frozen provider for this
    /// context's current Profile revision. This returns bounded routing
    /// metadata only; caller permission and final dispatch are still checked
    /// by normal admission and claim.
    pub fn resolve_frozen_target(
        &self,
        cx: &CallCx,
        view: &ShapeRef,
        op: &OpName,
    ) -> Result<FrozenPortTarget, GateError> {
        self.check_context(cx)?;
        self.validate_exact_operation(cx, view, op)?;
        let revision = cx
            .revision()
            .ok_or_else(|| GateError::Denied("missing frozen port revision".into()))?;
        let target = {
            let registry = self
                .admissions
                .bindings
                .lock()
                .map_err(|_| GateError::Persistence("port binding registry poisoned".into()))?;
            let targets = registry
                .get(&(cx.profile().clone(), revision.clone()))
                .and_then(|record| record.bindings.get(view))
                .ok_or_else(|| GateError::Denied("view has no frozen provider binding".into()))?;
            if targets.len() != 1 {
                return Err(GateError::Denied(
                    "view does not have exactly one frozen provider binding".into(),
                ));
            }
            targets
                .iter()
                .next()
                .cloned()
                .ok_or_else(|| GateError::Denied("view has no frozen provider binding".into()))?
        };
        self.check_frozen_provider(cx, &target.pack, &target.hash)?;
        Ok(target)
    }

    /// Resolve a received, authenticated User request before argument
    /// allocation. Rejection is attributed once to that original caller;
    /// success grants no authority and emits no event. Internal target
    /// rechecks continue to use the pure resolver above.
    pub async fn resolve_received_user_target(
        &self,
        cx: &CallCx,
        view: &ShapeRef,
        op: &OpName,
    ) -> Result<FrozenPortTarget, GateError> {
        let result = self.resolve_frozen_target(cx, view, op);
        self.audit_denial_result(cx, "port-call", result).await
    }

    /// Register the actual verified Flow endpoints for one immutable Profile
    /// revision. Re-registration of the same snapshot is idempotent; a changed
    /// snapshot for the same revision is denied. The retained catalog has no
    /// permission or dispatch authority of its own.
    pub fn register_flow_endpoints(
        &self,
        profile: &ProfileId,
        revision: &Digest,
        mut endpoints: Vec<FrozenFlowTarget>,
    ) -> Result<FlowEndpointRegistration, GateError> {
        if endpoints.len() > 65_536 || endpoints.capacity() > 65_536 {
            return Err(GateError::Limit("frozen Flow endpoint count".into()));
        }
        for target in &endpoints {
            if target.profile != *profile
                || target.revision != *revision
                || target.flow_key.is_empty()
                || target.flow_key.len() > 512
            {
                return Err(GateError::Denied(
                    "Flow endpoint differs from its Profile revision".into(),
                ));
            }
            let _ = self.frozen_limits_for(profile, revision, &target.owner, &target.owner_hash)?;
        }
        endpoints.sort_unstable_by(|left, right| left.flow_key.cmp(&right.flow_key));
        if endpoints
            .windows(2)
            .any(|pair| pair[0].flow_key == pair[1].flow_key)
        {
            return Err(GateError::Denied(
                "duplicate exact Flow endpoint key".into(),
            ));
        }
        {
            let views = self
                .operation_views
                .lock()
                .map_err(|_| GateError::Persistence("operation view registry poisoned".into()))?;
            let revision_views = views
                .get(&(profile.to_string(), revision.clone()))
                .ok_or_else(|| GateError::Denied("Flow revision views are not frozen".into()))?;
            if endpoints.iter().any(|target| {
                !revision_views.contains_key(&target.implements)
                    || !revision_views.contains_key(&target.input_view)
            }) {
                return Err(GateError::Denied(
                    "Flow endpoint uses an unknown exact view".into(),
                ));
            }
        }
        let bytes = flow_endpoints_storage_bytes(profile, &endpoints)?;
        let reservation = self.control().reserve(ControlKind::Catalog, bytes)?;
        let registration_bytes = u64::try_from(
            std::mem::size_of::<FlowEndpointRegistration>()
                .checked_add(profile.clone_storage_bytes())
                .and_then(|n| n.checked_add(64))
                .ok_or_else(|| GateError::Limit("Flow registration size overflow".into()))?,
        )
        .ok()
        .ok_or_else(|| GateError::Limit("Flow registration size overflow".into()))?;
        let registration_reservation = self
            .control()
            .reserve(ControlKind::Registration, registration_bytes)?;
        let candidate = Arc::new(FlowEndpointBindings {
            profile: profile.clone(),
            revision: revision.clone(),
            endpoints,
            registrations: AtomicUsize::new(1),
            _reservation: reservation,
        });
        let key = (profile.clone(), revision.clone());
        let mut registry = self
            .admissions
            .flow_endpoints
            .lock()
            .map_err(|_| GateError::Persistence("Flow endpoint registry poisoned".into()))?;
        // A poisoned-lock cleanup can leave dead weak entries. Reclaim those
        // before enforcing the bounded number of retained revision records.
        registry.retain(|_, catalog| catalog.strong_count() != 0);
        if let Some(current) = registry.get(&key).and_then(Weak::upgrade) {
            if current.endpoints != candidate.endpoints {
                return Err(GateError::Denied(
                    "frozen Flow endpoint catalog is immutable".into(),
                ));
            }
            current
                .registrations
                .fetch_update(Ordering::AcqRel, Ordering::Acquire, |count| {
                    count.checked_add(1)
                })
                .map_err(|_| GateError::Limit("Flow registration count overflow".into()))?;
            let catalog_weak = Arc::downgrade(&current);
            return Ok(FlowEndpointRegistration {
                catalog: current,
                catalog_weak,
                registry: Arc::downgrade(&self.admissions.flow_endpoints),
                key,
                _reservation: registration_reservation,
            });
        }
        if registry.len() >= 4096 {
            return Err(GateError::Limit(
                "frozen Flow endpoint revision capacity".into(),
            ));
        }
        let catalog_weak = Arc::downgrade(&candidate);
        registry.insert(key.clone(), catalog_weak.clone());
        Ok(FlowEndpointRegistration {
            catalog: candidate,
            catalog_weak,
            registry: Arc::downgrade(&self.admissions.flow_endpoints),
            key,
            _reservation: registration_reservation,
        })
    }

    /// Resolve an exact Flow key, or require a unique endpoint for the
    /// requested implements view. This is metadata resolution only; ordinary
    /// FlowRun permission, approval, grant consumption and frame checks remain.
    pub fn resolve_frozen_flow_target(
        &self,
        cx: &CallCx,
        implements: &ShapeRef,
        exact_flow_key: Option<&str>,
    ) -> Result<FrozenFlowTarget, GateError> {
        self.check_context(cx)?;
        let revision = cx
            .revision()
            .ok_or_else(|| GateError::Denied("missing frozen Flow revision".into()))?;
        let target = {
            let registry =
                self.admissions.flow_endpoints.lock().map_err(|_| {
                    GateError::Persistence("Flow endpoint registry poisoned".into())
                })?;
            let catalog = registry
                .get(&(cx.profile().clone(), revision.clone()))
                .and_then(Weak::upgrade)
                .ok_or_else(|| GateError::Denied("Flow endpoint catalog is not frozen".into()))?;
            let target = if let Some(key) = exact_flow_key {
                catalog
                    .endpoints
                    .binary_search_by(|target| target.flow_key.as_str().cmp(key))
                    .ok()
                    .and_then(|index| catalog.endpoints.get(index))
                    .filter(|target| &target.implements == implements)
                    .ok_or_else(|| {
                        GateError::Denied("exact Flow endpoint is not registered".into())
                    })?
            } else {
                let mut matching = catalog
                    .endpoints
                    .iter()
                    .filter(|target| &target.implements == implements);
                let first = matching
                    .next()
                    .ok_or_else(|| GateError::Denied("Flow view has no frozen endpoint".into()))?;
                if matching.next().is_some() {
                    return Err(GateError::Denied(
                        "Flow view has ambiguous frozen endpoints".into(),
                    ));
                }
                first
            };
            target.clone()
        };
        self.check_frozen_provider(cx, &target.owner, &target.owner_hash)?;
        Ok(target)
    }

    /// Read-only exact membership check for a Core frame proof. This reports
    /// frozen topology only and does not grant or consume FlowRun authority.
    pub fn frozen_flow_target_registered(
        &self,
        profile: &ProfileId,
        revision: &Digest,
        target: &FrozenFlowTarget,
    ) -> Result<bool, GateError> {
        let registry = self
            .admissions
            .flow_endpoints
            .lock()
            .map_err(|_| GateError::Persistence("Flow endpoint registry poisoned".into()))?;
        Ok(registry
            .get(&(profile.clone(), revision.clone()))
            .and_then(Weak::upgrade)
            .is_some_and(|catalog| catalog.endpoints.contains(target)))
    }

    pub(crate) fn frozen_flow_effect_matches(
        &self,
        cx: &CallCx,
        implements: &ShapeRef,
        owner: &PackId,
        owner_hash: &PackHash,
        flow_key: &str,
        target_digest: &Digest,
    ) -> Result<bool, GateError> {
        let Some(revision) = cx.revision() else {
            return Ok(false);
        };
        let registry = self
            .admissions
            .flow_endpoints
            .lock()
            .map_err(|_| GateError::Persistence("Flow endpoint registry poisoned".into()))?;
        let Some(catalog) = registry
            .get(&(cx.profile().clone(), revision.clone()))
            .and_then(Weak::upgrade)
        else {
            return Ok(false);
        };
        let Some(target) = catalog
            .endpoints
            .binary_search_by(|target| target.flow_key.as_str().cmp(flow_key))
            .ok()
            .and_then(|index| catalog.endpoints.get(index))
        else {
            return Ok(false);
        };
        Ok(target.implements == *implements
            && target.owner == *owner
            && target.owner_hash == *owner_hash
            && target.digest()? == *target_digest)
    }

    pub fn register_compiled_calls(
        &self,
        factory_identity: &Digest,
        profile: &ProfileId,
        revision: &Digest,
        descriptors: BTreeMap<Digest, CompiledCallDescriptor>,
    ) -> Result<CompiledCallAuthority, GateError> {
        if descriptors.len() > 65536 {
            return Err(GateError::Limit("compiled descriptor count".into()));
        }
        let bytes =
            compiled_descriptors_storage_bytes(factory_identity, profile, revision, &descriptors)?;
        // Reserve before retaining descriptors or allocating a scope control
        // record. The registration itself is the per-factory lifetime token.
        let reservation = self.control().reserve(ControlKind::Registration, bytes)?;
        let bindings = self
            .admissions
            .bindings
            .lock()
            .map_err(|_| GateError::Persistence("port binding registry poisoned".into()))?;
        let frozen = bindings
            .get(&(profile.clone(), revision.clone()))
            .ok_or_else(|| GateError::Denied("missing frozen port bindings".into()))?;
        for (id, descriptor) in &descriptors {
            match &descriptor.owner {
                CompiledCodeOwner::Pack { pack, hash } => {
                    let _ = self.frozen_limits_for(profile, revision, pack, hash)?;
                }
                CompiledCodeOwner::User { profile_ast } if profile_ast != &descriptor.ast_hash => {
                    return Err(GateError::Denied(
                        "User descriptor must retain its actual AST provenance".into(),
                    ));
                }
                CompiledCodeOwner::User { .. } => (),
                CompiledCodeOwner::UserDocument { .. } => {
                    return Err(GateError::Denied(
                        "User document cannot own a compiled guest call".into(),
                    ));
                }
            }
            if id != &descriptor.id
                || &descriptor.revision != revision
                || descriptor.flow.is_empty()
                || descriptor.node.is_empty()
                || descriptor.flow.len() > 512
                || descriptor.node.len() > 1024
                || descriptor.targets.is_empty()
                || descriptor.targets.len() > 256
                || !descriptor.targets.iter().all(|target| {
                    frozen
                        .bindings
                        .get(&descriptor.shape)
                        .is_some_and(|set| set.contains(target))
                })
                || (descriptor.kind == CompiledNodeKind::FixedStreamEmit
                    && descriptor.op.as_str() != "emit")
            {
                return Err(GateError::Denied(
                    "compiled descriptor does not match frozen plan bindings".into(),
                ));
            }
        }
        let registration_token = self.next_registration_token()?;
        let scope = Arc::new(CompiledScope {
            profile: profile.clone(),
            revision: revision.clone(),
            factory_identity: factory_identity.clone(),
            registration_token,
            gate_identity: self.registration_gate_identity(),
            closed: AtomicBool::new(false),
            descriptors,
            _reservation: reservation,
        });
        Ok(CompiledCallAuthority { scope })
    }
    pub fn begin_compiled_execution(
        &self,
        cx: &CallCx,
        authority: &CompiledCallAuthority,
    ) -> Result<CompiledExecution, GateError> {
        self.check_context(cx)?;
        let binding = ContextBinding::new(cx)?;
        self.check_scope(&authority.scope, cx)?;
        Ok(CompiledExecution {
            scope: authority.scope.clone(),
            binding,
        })
    }
    fn check_scope(&self, scope: &Arc<CompiledScope>, cx: &CallCx) -> Result<(), GateError> {
        if scope.closed.load(Ordering::Acquire)
            || !self.same_gate_identity(&scope.gate_identity)
            || scope.profile != *cx.profile()
            || cx.revision() != Some(&scope.revision)
        {
            return Err(GateError::Denied(
                "compiled execution registration is closed or belongs to another Gate/revision"
                    .into(),
            ));
        }
        Ok(())
    }
    fn check_code_owner(
        &self,
        cx: &CallCx,
        descriptor: &CompiledCallDescriptor,
    ) -> Result<(), GateError> {
        let owner = match &descriptor.owner {
            CompiledCodeOwner::Pack { pack, hash } => PreparedCodeOwner::Pack { pack, hash },
            CompiledCodeOwner::User { profile_ast } => PreparedCodeOwner::User { profile_ast },
            CompiledCodeOwner::UserDocument { document } => {
                PreparedCodeOwner::UserDocument { document }
            }
        };
        let effective = self.effective_owner_permissions(cx, owner)?;
        if !effective.ports.contains(&descriptor.shape.to_string()) {
            return Err(GateError::Denied(
                "compiled code owner exceeds original current authority".into(),
            ));
        }
        Ok(())
    }
    fn check_port_target(
        &self,
        cx: &CallCx,
        shape: &ShapeRef,
        target: &FrozenPortTarget,
    ) -> Result<(), GateError> {
        let revision = cx
            .revision()
            .ok_or_else(|| GateError::Denied("missing frozen port revision".into()))?;
        let bindings = self
            .admissions
            .bindings
            .lock()
            .map_err(|_| GateError::Persistence("port binding registry poisoned".into()))?;
        if !bindings
            .get(&(cx.profile().clone(), revision.clone()))
            .and_then(|record| record.bindings.get(shape))
            .is_some_and(|set| set.contains(target))
        {
            return Err(GateError::Denied(
                "dispatch target is not the frozen binding".into(),
            ));
        }
        drop(bindings);
        self.check_frozen_provider(cx, &target.pack, &target.hash)
    }
    pub(crate) async fn admit_port_inner(
        &self,
        cx: &CallCx,
        shape: ShapeRef,
        op: OpName,
        args: Digest,
        target: FrozenPortTarget,
    ) -> Result<PortAdmission, GateError> {
        self.check_context(cx)?;
        let binding = ContextBinding::new(cx)?;
        self.check_port_target(cx, &shape, &target)?;
        let grant = self
            .authorize_inner(cx, EffectRequest::PortCall { shape, op }, args)
            .await?;
        Ok(PortAdmission {
            grant,
            binding,
            target,
            origin: None,
        })
    }
    pub(crate) async fn admit_compiled_inner(
        &self,
        cx: &CallCx,
        activation: CompiledActivation,
        args: Digest,
    ) -> Result<PortAdmission, GateError> {
        activation.binding.check(cx)?;
        self.authenticate_frame_scope(cx, &activation.frame_scope)?;
        self.check_scope(&activation.scope, cx)?;
        if activation.frame_scope.factory_identity() != &activation.scope.factory_identity {
            return Err(GateError::Denied(
                "compiled registration belongs to another Core factory".into(),
            ));
        }
        let descriptor = activation
            .scope
            .descriptors
            .get(&activation.descriptor)
            .ok_or_else(|| GateError::Denied("compiled descriptor disappeared".into()))?;
        self.check_port_target(cx, &descriptor.shape, &activation.target)?;
        self.check_code_owner(cx, descriptor)?;
        let binding = ContextBinding::new(cx)?;
        let grant = self
            .authorize_inner(
                cx,
                EffectRequest::PortCall {
                    shape: descriptor.shape.clone(),
                    op: descriptor.op.clone(),
                },
                args,
            )
            .await?;
        let target = activation.target.clone();
        Ok(PortAdmission {
            grant,
            binding,
            target,
            origin: Some(activation),
        })
    }
    pub(crate) async fn claim_port_inner(
        &self,
        cx: &CallCx,
        admission: PortAdmission,
        shape: &ShapeRef,
        op: &OpName,
        args: &Digest,
        target: &FrozenPortTarget,
    ) -> Result<AdmittedDispatch, GateError> {
        self.check_context(cx)?;
        admission.binding.check(cx)?;
        let request = EffectRequest::PortCall {
            shape: shape.clone(),
            op: op.clone(),
        };
        if admission.target != *target
            || admission.grant.effect() != &request
            || admission.grant.args_digest() != args
        {
            return Err(GateError::Denied(
                "final port dispatch binding mismatch".into(),
            ));
        }
        self.check_port_target(cx, shape, target)?;
        if let Some(activation) = &admission.origin {
            activation.binding.check(cx)?;
            let metadata = self.authenticate_frame_scope(cx, &activation.frame_scope)?;
            self.check_scope(&activation.scope, cx)?;
            if activation.frame_scope.factory_identity() != &activation.scope.factory_identity {
                return Err(GateError::Denied(
                    "compiled registration belongs to another Core factory".into(),
                ));
            }
            let descriptor = &activation.scope.descriptors[&activation.descriptor];
            self.check_code_owner(cx, descriptor)?;
            if &descriptor.shape != shape
                || &descriptor.op != op
                || activation.target != *target
                || activation.activation == 0
                || activation.frame != metadata.frame
                || activation.activation != metadata.activation
                || activation.descriptor != metadata.node
            {
                return Err(GateError::Denied("compiled final dispatch mismatch".into()));
            }
            let _live_frame = &activation.frame;
            self.claim_frame_dispatch(
                cx,
                &activation.frame_scope,
                FrameDispatch {
                    view: shape,
                    op,
                    args,
                    target,
                },
            )?;
        }
        let permit = admission.grant.consume(cx, &request, args)?;
        if let Err(error) = self.watch_lease(cx, &permit) {
            self.record_outcome(
                permit.id().clone(),
                Outcome::Failed {
                    code: error.code().into(),
                },
            )
            .await?;
            return Err(error);
        }
        let charge = cx
            .budget()
            .ok_or_else(|| GateError::Denied("missing inherited dispatch budget".into()))?
            .charge(ChargeKind::PortDispatch);
        match charge {
            Ok(charge) => Ok(AdmittedDispatch {
                permit,
                target: target.clone(),
                _charge: charge,
            }),
            Err(error) => {
                self.record_outcome(
                    permit.id().clone(),
                    Outcome::Failed {
                        code: error.code().into(),
                    },
                )
                .await?;
                Err(error.into())
            }
        }
    }
}

fn add_control_bytes(total: &mut usize, bytes: usize) -> Result<(), GateError> {
    *total = total
        .checked_add(bytes)
        .ok_or_else(|| GateError::Limit("Core registry size overflow".into()))?;
    Ok(())
}

fn port_bindings_storage_bytes(
    profile: &ProfileId,
    revision: &Digest,
    bindings: &PortBindings,
) -> Result<u64, GateError> {
    // The fixed-size revision is part of the retained scope key below; the
    // conservative layout estimate already accounts for the key itself.
    let _ = revision;
    if bindings.len() > 4096 {
        return Err(GateError::Limit("frozen port binding count".into()));
    }
    let mut bytes = size_of::<FrozenBindings>()
        .checked_add(size_of::<ScopeKey>())
        .and_then(|n| n.checked_add(512))
        .ok_or_else(|| GateError::Limit("Core registry size overflow".into()))?;
    add_control_bytes(&mut bytes, profile.clone_storage_bytes())?;
    let mut target_count = 0usize;
    for (shape, targets) in bindings {
        target_count = target_count
            .checked_add(targets.len())
            .ok_or_else(|| GateError::Limit("frozen port target count overflow".into()))?;
        if target_count > 65_536 {
            return Err(GateError::Limit("frozen port target count".into()));
        }
        add_control_bytes(
            &mut bytes,
            size_of::<(ShapeRef, BTreeSet<FrozenPortTarget>)>()
                .checked_add(shape.clone_storage_bytes())
                .and_then(|n| n.checked_add(64))
                .ok_or_else(|| GateError::Limit("Core registry size overflow".into()))?,
        )?;
        for target in targets {
            add_control_bytes(
                &mut bytes,
                size_of::<FrozenPortTarget>()
                    .checked_add(target.pack.clone_storage_bytes())
                    .and_then(|n| n.checked_add(target.provided.clone_storage_bytes()))
                    .and_then(|n| n.checked_add(64))
                    .ok_or_else(|| GateError::Limit("Core registry size overflow".into()))?,
            )?;
        }
    }
    u64::try_from(bytes).map_err(|_| GateError::Limit("Core registry size overflow".into()))
}

fn flow_endpoints_storage_bytes(
    profile: &ProfileId,
    endpoints: &Vec<FrozenFlowTarget>,
) -> Result<u64, GateError> {
    let mut bytes = std::mem::size_of::<FlowEndpointBindings>()
        .checked_add(std::mem::size_of::<ScopeKey>())
        .and_then(|n| n.checked_add(256))
        .and_then(|n| n.checked_add(profile.clone_storage_bytes()))
        .and_then(|n| {
            n.checked_add(
                endpoints
                    .capacity()
                    .checked_mul(std::mem::size_of::<FrozenFlowTarget>())?,
            )
        })
        .ok_or_else(|| GateError::Limit("Flow endpoint storage size overflow".into()))?;
    for endpoint in endpoints {
        bytes = bytes
            .checked_add(
                endpoint
                    .clone_storage_bytes()
                    .map_err(|_| GateError::Limit("Flow endpoint storage size overflow".into()))?,
            )
            .ok_or_else(|| GateError::Limit("Flow endpoint storage size overflow".into()))?;
    }
    u64::try_from(bytes).map_err(|_| GateError::Limit("Flow endpoint storage size overflow".into()))
}

fn compiled_descriptors_storage_bytes(
    factory_identity: &Digest,
    profile: &ProfileId,
    revision: &Digest,
    descriptors: &BTreeMap<Digest, CompiledCallDescriptor>,
) -> Result<u64, GateError> {
    let mut bytes = size_of::<CompiledScope>()
        .checked_add(size_of::<CompiledCallAuthority>())
        .and_then(|n| n.checked_add(1024))
        .ok_or_else(|| GateError::Limit("compiled descriptor size overflow".into()))?;
    add_control_bytes(&mut bytes, profile.clone_storage_bytes())?;
    for descriptor in descriptors.values() {
        let mut item = size_of::<(Digest, CompiledCallDescriptor)>()
            .checked_add(descriptor.owner.clone_storage_bytes())
            .and_then(|n| n.checked_add(descriptor.flow.len()))
            .and_then(|n| n.checked_add(descriptor.node.len()))
            .and_then(|n| n.checked_add(descriptor.shape.clone_storage_bytes()))
            .and_then(|n| n.checked_add(descriptor.op.clone_storage_bytes()))
            .and_then(|n| n.checked_add(128))
            .ok_or_else(|| GateError::Limit("compiled descriptor size overflow".into()))?;
        for target in &descriptor.targets {
            item = item
                .checked_add(size_of::<FrozenPortTarget>())
                .and_then(|n| n.checked_add(target.pack.clone_storage_bytes()))
                .and_then(|n| n.checked_add(target.provided.clone_storage_bytes()))
                .and_then(|n| n.checked_add(64))
                .ok_or_else(|| GateError::Limit("compiled descriptor size overflow".into()))?;
        }
        add_control_bytes(&mut bytes, item)?;
    }
    // The profile, revision and factory identity are immutable registration
    // bindings; their fixed digest fields are included in the scope layout.
    let _ = revision;
    let _ = factory_identity;
    u64::try_from(bytes).map_err(|_| GateError::Limit("compiled descriptor size overflow".into()))
}

#[cfg(test)]
mod flow_target_tests {
    use super::*;

    fn target() -> FrozenFlowTarget {
        let profile = ProfileId::parse("profile_flow_target").unwrap();
        let revision = Digest::hash(b"compiled-revision");
        let owner = PackId::parse("local.owner").unwrap();
        let input_view = ShapeRef::parse("local.owner.input@1.0").unwrap();
        FrozenFlowTarget {
            profile,
            revision,
            owner,
            owner_hash: PackHash::new(Digest::hash(b"verified-pack")),
            flow_key: "local.owner/example".into(),
            flow_ast: Digest::hash(b"actual-flow-document"),
            plan_digest: Digest::hash(b"compiled-flow-plan"),
            implements: ShapeRef::parse("local.owner.workflow@1.0").unwrap(),
            input_view,
            entry_signature: Digest::hash(b"verified-entry-signature"),
        }
    }

    #[test]
    fn flow_target_digest_is_stable_and_binds_key_and_ast() {
        let original = target();
        assert_eq!(original.digest().unwrap(), original.digest().unwrap());

        let mut another_key = original.clone();
        another_key.flow_key.push_str("-other");
        assert_ne!(original.digest().unwrap(), another_key.digest().unwrap());

        let mut another_ast = original.clone();
        another_ast.flow_ast = Digest::hash(b"different-flow-document");
        assert_ne!(original.digest().unwrap(), another_ast.digest().unwrap());
    }

    #[test]
    fn flow_endpoint_catalog_lives_until_the_last_exact_registration_drops() {
        let control = CoreControl::default();
        let target = target();
        let key = (target.profile.clone(), target.revision.clone());
        let catalog_reservation = control
            .reserve(ControlKind::Catalog, 128)
            .expect("bounded catalog reservation");
        let catalog = Arc::new(FlowEndpointBindings {
            profile: target.profile.clone(),
            revision: target.revision.clone(),
            endpoints: vec![target],
            registrations: AtomicUsize::new(2),
            _reservation: catalog_reservation,
        });
        let catalog_weak = Arc::downgrade(&catalog);
        let registry = Arc::new(Mutex::new(BTreeMap::from([(
            key.clone(),
            catalog_weak.clone(),
        )])));
        let make_registration = || FlowEndpointRegistration {
            catalog: catalog.clone(),
            catalog_weak: catalog_weak.clone(),
            registry: Arc::downgrade(&registry),
            key: key.clone(),
            _reservation: control
                .reserve(ControlKind::Registration, 32)
                .expect("bounded registration reservation"),
        };

        let first = make_registration();
        let second = make_registration();
        drop(first);
        assert_eq!(registry.lock().unwrap().len(), 1);
        assert_eq!(control.usage().count(ControlKind::Catalog), 1);

        drop(second);
        assert!(registry.lock().unwrap().is_empty());
        drop(catalog);
        assert_eq!(control.usage().count(ControlKind::Catalog), 0);
        assert_eq!(control.usage().count(ControlKind::Registration), 0);
    }
}
