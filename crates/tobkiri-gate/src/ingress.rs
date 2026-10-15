//! Private Core ingress evidence. A physical read receipt is not allocation,
//! dispatch or routing authority and has no guest/WIT/Serde representation.
use crate::{admission::ContextBinding, protocol::strict_bytes, *};
use std::{
    any::Any,
    collections::BTreeMap,
    sync::{Arc, Mutex},
};
use tobkiri_canon::{
    ControlHandle, Digest,
    ids::{InputSourceId, OpName, ProfileId, ShapeRef},
};

/// Returned only after the registered Core verifier claims its actual consumed
/// physical-read/output node once. Core independently prepays retained metadata.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SourceReadMetadata {
    pub source: InputSourceId,
    pub view: ShapeRef,
    pub op: OpName,
    pub target: FrozenPortTarget,
    pub payload: Digest,
    pub read_grant: GrantId,
    pub frame: Digest,
    pub activation: u64,
    pub path: Digest,
}

/// One immutable native source-read evidence ticket. Core moves it together
/// with the original read context and CoreValue into a single virtual submit.
pub struct SourceReadReceipt {
    // Retain the actual issuing registration, never a newly attached token.
    // Cloning its record handle does not clone the Session's RAII owner token.
    registration: ControlHandle<crate::frame::FrameRegistrationRecord>,
    binding: ControlHandle<CallCx>,
    metadata: ControlHandle<SourceReadMetadata>,
    _reservation: ControlReservation,
}
impl std::fmt::Debug for SourceReadReceipt {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("SourceReadReceipt")
            .field("read_grant", &self.metadata.read_grant)
            .finish_non_exhaustive()
    }
}
impl SourceReadReceipt {
    /// Identity of the original Core factory that authenticated this read.
    /// The receipt retains its private registration; this identity alone
    /// cannot construct a receipt or authorize routing.
    pub fn factory_identity(&self) -> &Digest {
        self.registration.factory_identity()
    }

    pub fn metadata(&self) -> &SourceReadMetadata {
        self.metadata.get()
    }

    /// Verify the original immutable Input context after Core carries this
    /// private receipt through a native route claim. The receipt has no public
    /// constructor and this check grants no routing or dispatch authority.
    pub fn validate_context(&self, cx: &CallCx) -> Result<(), GateError> {
        ContextBinding::check_borrowed(self.binding.get(), cx)
    }
}

/// Exact ordered Flow destination from Core's frozen trigger plan. `flow`
/// commits the actual Flow endpoint/AST identity, independently of its view.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct IngressRoute {
    pub implements: ShapeRef,
    pub target: FrozenFlowTarget,
}
/// NativeInput policy copied only from its verified Input declaration and exact
/// compiled Profile trigger plan. Registration describes no authority by itself.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CoreIngressPolicy {
    pub id: Digest,
    pub source: InputSourceId,
    pub view: ShapeRef,
    pub op: OpName,
    pub target: FrozenPortTarget,
    pub routes: Vec<IngressRoute>,
}
impl CoreIngressPolicy {
    fn source_digest(&self) -> Result<Digest, GateError> {
        Ok(Digest::hash(&strict_bytes(&(
            &self.source,
            &self.view,
            &self.op,
            &self.target.pack,
            &self.target.hash,
            &self.target.provided,
        ))?))
    }
    fn routes_digest(&self) -> Result<Digest, GateError> {
        let routes = self
            .routes
            .iter()
            .map(|route| Ok((&route.implements, route.target.digest()?)))
            .collect::<Result<Vec<_>, GateError>>()?;
        Ok(Digest::hash(&strict_bytes(&routes)?))
    }
    fn effect(&self, invocation: Digest) -> Result<EffectRequest, GateError> {
        Ok(EffectRequest::VirtualInputSubmit {
            ingress: self.id.clone(),
            invocation,
            source: self.source_digest()?,
            routes: self.routes_digest()?,
        })
    }
    pub(crate) fn allows_effect(
        &self,
        request: &EffectRequest,
        permissions: &PermissionSet,
    ) -> bool {
        let EffectRequest::VirtualInputSubmit {
            ingress,
            invocation,
            source,
            routes,
        } = request
        else {
            return false;
        };
        ingress == &self.id
            && self
                .source_digest()
                .is_ok_and(|expected| &expected == source)
            && self
                .routes_digest()
                .is_ok_and(|expected| &expected == routes)
            && self
                .effect(invocation.clone())
                .is_ok_and(|expected| &expected == request)
            && permissions.ports.contains(&self.view.to_string())
            && self
                .routes
                .iter()
                .all(|route| permissions.ports.contains(&route.implements.to_string()))
    }
}
struct RegisteredIngress {
    profile: ProfileId,
    revision: Digest,
    policy: CoreIngressPolicy,
    _reservation: ControlReservation,
}
type IngressKey = (ProfileId, Digest, Digest);
#[derive(Default)]
pub(crate) struct IngressRegistry(Mutex<BTreeMap<IngressKey, Arc<RegisteredIngress>>>);
/// Core keeps this beside the exact immutable compiled trigger plan.
pub struct IngressPolicyRegistration(Arc<RegisteredIngress>);
impl IngressPolicyRegistration {
    /// Borrow the immutable ordered destinations retained by this registration.
    /// Reading topology creates no dispatch or routing authority.
    pub fn routes(&self) -> &[IngressRoute] {
        &self.0.policy.routes
    }
}
/// One unconsumed zero-cost routing grant plus its independent native evidence.
pub struct VirtualInputAdmission {
    grant: Grant,
    registration: Arc<RegisteredIngress>,
    receipt: SourceReadReceipt,
}
impl VirtualInputAdmission {
    pub fn id(&self) -> &GrantId {
        self.grant.id()
    }
    pub fn effect(&self) -> &EffectRequest {
        self.grant.effect()
    }
    pub fn routes(&self) -> &[IngressRoute] {
        &self.registration.policy.routes
    }
}

impl GateEngine {
    pub fn register_native_ingress(
        &self,
        profile: &ProfileId,
        revision: &Digest,
        policy: CoreIngressPolicy,
    ) -> Result<IngressPolicyRegistration, GateError> {
        if policy.routes.is_empty() || policy.routes.len() > 256 {
            return Err(GateError::Limit("native ingress route count".into()));
        }
        let bytes = ingress_policy_storage_bytes(profile, &policy)?;
        if !self.frozen_target_registered(profile, revision, &policy.view, &policy.target)? {
            return Err(GateError::Denied("ingress source is not frozen".into()));
        }
        for route in &policy.routes {
            self.frozen_limits_for(
                profile,
                revision,
                &route.target.owner,
                &route.target.owner_hash,
            )?;
            if route.implements != route.target.implements
                || !self.frozen_flow_target_registered(profile, revision, &route.target)?
            {
                return Err(GateError::Denied("ingress route is not frozen".into()));
            }
        }
        let mut registry = self
            .ingress
            .0
            .lock()
            .map_err(|_| GateError::Persistence("ingress registry poisoned".into()))?;
        let key = (profile.clone(), revision.clone(), policy.id.clone());
        if let Some(old) = registry.get(&key) {
            if old.policy != policy {
                return Err(GateError::Denied(
                    "frozen ingress policy is immutable".into(),
                ));
            }
            return Ok(IngressPolicyRegistration(old.clone()));
        }
        if registry.len() >= 4096 {
            return Err(GateError::Limit("ingress registry capacity".into()));
        }
        let reservation = self.control().reserve(ControlKind::Catalog, bytes)?;
        let registered = Arc::new(RegisteredIngress {
            profile: profile.clone(),
            revision: revision.clone(),
            policy,
            _reservation: reservation,
        });
        registry.insert(key, registered.clone());
        Ok(IngressPolicyRegistration(registered))
    }
    fn check_native_ingress(
        &self,
        cx: &CallCx,
        registration: &Arc<RegisteredIngress>,
        receipt: &SourceReadReceipt,
        payload: &Digest,
    ) -> Result<(), GateError> {
        self.check_context(cx)?;
        self.check_frame_registration(cx, &receipt.registration)?;
        ContextBinding::check_borrowed(receipt.binding.get(), cx)?;
        let policy = &registration.policy;
        if registration.profile != *cx.profile()
            || cx.revision() != Some(&registration.revision)
            || receipt.metadata.payload != *payload
            || receipt.metadata.source != policy.source
            || receipt.metadata.view != policy.view
            || receipt.metadata.op != policy.op
            || receipt.metadata.target != policy.target
        {
            return Err(GateError::Denied(
                "native ingress evidence differs from frozen source".into(),
            ));
        }
        if !self
            .ingress
            .0
            .lock()
            .map_err(|_| GateError::Persistence("ingress registry poisoned".into()))?
            .get(&(
                registration.profile.clone(),
                registration.revision.clone(),
                policy.id.clone(),
            ))
            .is_some_and(|current| Arc::ptr_eq(current, registration))
        {
            return Err(GateError::Denied(
                "ingress policy belongs to another gate".into(),
            ));
        }
        let source = self.registered_input_source(cx, &policy.view, &policy.op)?;
        if source.entry_view != policy.view
            || source.entry_op != policy.op
            || source.target != policy.target
        {
            return Err(GateError::Denied("ingress source endpoint changed".into()));
        }
        self.input_capacity_limit(cx, &policy.view, &policy.op, &policy.target)?;
        let source_rights = self.effective_owner_permissions(
            cx,
            PreparedCodeOwner::Pack {
                pack: &source.pack,
                hash: &source.hash,
            },
        )?;
        let input_rights = self.effective_permissions(cx)?;
        for route in &policy.routes {
            if route.implements != route.target.implements
                || !source_rights.ports.contains(&route.implements.to_string())
                || !input_rights.ports.contains(&route.implements.to_string())
                || !self.frozen_flow_target_registered(
                    cx.profile(),
                    &registration.revision,
                    &route.target,
                )?
            {
                return Err(GateError::Denied(
                    "ingress exceeds current source or destination authority".into(),
                ));
            }
            self.check_frozen_provider(cx, &route.target.owner, &route.target.owner_hash)?;
        }
        // A claimed source output is evidence only; successful durable read
        // terminal/delivery is required before it may route anything.
        let read = self
            .approvals
            .terminal_delivery(&receipt.metadata.read_grant)?
            .ok_or(GateError::Unavailable)?;
        if !read.acknowledged
            || read.decision.outcome != Outcome::Succeeded
            || !read.decision.reservation.principal.eq(cx.principal())
            || read.decision.reservation.profile != *cx.profile()
            || read.decision.reservation.request != *cx.request()
            || read.decision.reservation.revision.as_ref() != cx.revision()
            || read.decision.reservation.epoch != cx.security_epoch()
            || read.decision.reservation.effect
                != (EffectRequest::PortCall {
                    shape: policy.view.clone(),
                    op: policy.op.clone(),
                })
                .digest()?
        {
            return Err(GateError::Denied(
                "physical source read has no successful matching terminal".into(),
            ));
        }
        Ok(())
    }
    pub async fn admit_virtual_input(
        &self,
        cx: &CallCx,
        registration: &IngressPolicyRegistration,
        receipt: SourceReadReceipt,
        invocation: Digest,
        payload: Digest,
    ) -> Result<VirtualInputAdmission, GateError> {
        let result = async {
            self.check_native_ingress(cx, &registration.0, &receipt, &payload)?;
            let request = registration.0.policy.effect(invocation)?;
            let grant = self
                .authorize_inner_with_ingress(cx, request, payload, Some(&registration.0.policy))
                .await?;
            Ok(VirtualInputAdmission {
                grant,
                registration: registration.0.clone(),
                receipt,
            })
        }
        .await;
        self.audit_denial_result(cx, "virtual-input-submit", result)
            .await
    }
    pub async fn claim_virtual_input(
        &self,
        cx: &CallCx,
        admission: VirtualInputAdmission,
    ) -> Result<DispatchPermit, GateError> {
        self.claim_virtual_input_with_receipt(cx, admission)
            .await
            .map(|(permit, _receipt)| permit)
    }

    /// Claim the exact zero-cost routing reservation while returning the
    /// source-read evidence that was authenticated in the same admission.
    /// Core retains the returned permit as sole terminal custodian and moves
    /// the noncloneable receipt alongside its separate FlowRun invocation.
    pub async fn claim_virtual_input_with_receipt(
        &self,
        cx: &CallCx,
        admission: VirtualInputAdmission,
    ) -> Result<(DispatchPermit, SourceReadReceipt), GateError> {
        let result = async {
            self.check_native_ingress(
                cx,
                &admission.registration,
                &admission.receipt,
                admission.grant.args_digest(),
            )?;
            let effect = admission.grant.effect().clone();
            let args = admission.grant.args_digest().clone();
            let permit = admission.grant.consume(cx, &effect, &args)?;
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
            // Routing intentionally has no ordinary physical PortDispatch charge.
            Ok((permit, admission.receipt))
        }
        .await;
        self.audit_denial_result(cx, "virtual-input-submit", result)
            .await
    }
    pub fn mint_source_read_receipt(
        &self,
        original_input: &CallCx,
        registration: &FrameVerifierRegistration,
        private_provider_output: ControlHandle<dyn Any + Send + Sync>,
        payload: Digest,
    ) -> Result<SourceReadReceipt, GateError> {
        self.check_context(original_input)?;
        self.check_frame_registration(original_input, &registration.0)?;
        let Principal::Input { source } = original_input.principal() else {
            return Err(GateError::Denied(
                "physical source receipt requires Input caller".into(),
            ));
        };
        // Reserve a conservative upper bound for the native evidence snapshot
        // before projecting any provider-owned metadata into the receipt.
        let receipt_reservation = original_input
            .control()
            .reserve(ControlKind::Frame, SOURCE_READ_RECEIPT_CONTROL_BYTES)?;
        let binding =
            private_provider_output.try_project(|proof| registration.0.source_context(proof))?;
        ContextBinding::check_borrowed(binding.get(), original_input)?;
        let metadata = private_provider_output.try_project(|proof| {
            registration
                .0
                .claim_source_read(original_input, proof, &payload)
        })?;
        if &metadata.source != source || metadata.payload != payload || metadata.activation == 0 {
            return Err(GateError::Denied(
                "physical source receipt binding mismatch".into(),
            ));
        }
        // This exact query validates the verified source endpoint, current
        // approval/epoch and its own explicit memory policy, without relabeling
        // the original Input principal or approving any downstream effect.
        self.input_capacity_limit(
            original_input,
            &metadata.view,
            &metadata.op,
            &metadata.target,
        )?;
        Ok(SourceReadReceipt {
            registration: registration.0.clone(),
            binding,
            metadata,
            _reservation: receipt_reservation,
        })
    }
}

const SOURCE_READ_RECEIPT_CONTROL_BYTES: u64 = 4096;

fn ingress_policy_storage_bytes(
    profile: &ProfileId,
    policy: &CoreIngressPolicy,
) -> Result<u64, GateError> {
    let mut bytes = std::mem::size_of::<RegisteredIngress>()
        .checked_add(std::mem::size_of::<CoreIngressPolicy>())
        .and_then(|n| n.checked_add(profile.clone_storage_bytes()))
        .and_then(|n| n.checked_add(policy.source.clone_storage_bytes()))
        .and_then(|n| n.checked_add(policy.view.clone_storage_bytes()))
        .and_then(|n| n.checked_add(policy.op.clone_storage_bytes()))
        .and_then(|n| n.checked_add(policy.target.pack.clone_storage_bytes()))
        .and_then(|n| n.checked_add(policy.target.provided.clone_storage_bytes()))
        .and_then(|n| {
            n.checked_add(
                policy
                    .routes
                    .len()
                    .checked_mul(std::mem::size_of::<IngressRoute>())?,
            )
        })
        .ok_or_else(|| GateError::Limit("ingress policy storage size overflow".into()))?;
    for route in &policy.routes {
        bytes = bytes
            .checked_add(route.implements.clone_storage_bytes())
            .and_then(|n| n.checked_add(route.target.clone_storage_bytes().ok()?))
            .and_then(|n| n.checked_add(std::mem::size_of::<ControlReservation>()))
            .ok_or_else(|| GateError::Limit("ingress policy storage size overflow".into()))?;
    }
    u64::try_from(bytes)
        .map_err(|_| GateError::Limit("ingress policy storage size overflow".into()))
}
