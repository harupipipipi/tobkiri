//! Core authenticates actual execution state; Gate checks its current funding.
//! These capabilities have no Serde/WIT representation. Registered metadata
//! alone never constitutes a reached-node proof or execution permission.
use crate::{admission::ContextBinding, *};
use std::{
    any::Any,
    sync::atomic::{AtomicBool, AtomicU64, Ordering},
};
use tobkiri_canon::{
    ControlHandle, Digest,
    ids::{PackHash, ProfileId, RequestId},
};
#[path = "reached_evidence.rs"]
mod reached_evidence;
pub use reached_evidence::{ReachedContextView, ReachedEvidenceRef, ReachedEvidenceView};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AllocationReason {
    RuntimeValue,
    EventFrame,
    ArgumentDigest,
    StreamBatch,
    OutputBuffer,
}

/// Borrowed funding owner from Core's actual prepared entry, never a selector
/// supplied by guest JSON or by possession of a registered descriptor.
#[derive(Clone, Copy)]
pub enum PreparedCodeOwner<'a> {
    Pack {
        pack: &'a tobkiri_canon::ids::PackId,
        hash: &'a PackHash,
    },
    User {
        profile_ast: &'a Digest,
    },
    /// Real current User policy document, not a synthetic guest AST identity.
    UserDocument {
        document: &'a Digest,
    },
}
/// Descriptive views returned only by Core's authenticated private invocation
/// verifier. Copying these fields is never an entry or funding proof.
pub struct PreparedUiInvocation<'a> {
    pub invocation: &'a Digest,
    pub session: &'a tobkiri_canon::ids::UiSessionId,
    pub generation: u64,
    pub receiver: &'a Digest,
    pub view: &'a tobkiri_canon::ids::ShapeRef,
    pub op: &'a tobkiri_canon::ids::OpName,
    pub target: &'a FrozenPortTarget,
}
pub struct PreparedNativeInput<'a> {
    pub invocation: &'a Digest,
    pub source: &'a tobkiri_canon::ids::InputSourceId,
    pub view: &'a tobkiri_canon::ids::ShapeRef,
    pub op: &'a tobkiri_canon::ids::OpName,
    pub target: &'a FrozenPortTarget,
}
/// Exact trusted User/CLI action before it materializes arguments or claims
/// its ordinary PortCall. Core's private proof still binds the invocation.
pub struct PreparedUserInvocation<'a> {
    pub invocation: &'a Digest,
    pub user_document: &'a Digest,
    pub revision: &'a Digest,
    pub epoch: u64,
    pub view: &'a tobkiri_canon::ids::ShapeRef,
    pub op: &'a tobkiri_canon::ids::OpName,
    pub target: &'a FrozenPortTarget,
}
/// Borrowed, authenticated view of the invocation that owns a reached node.
/// These values describe evidence held by Core's private live state; they do
/// not convey the consumed permit or terminal custody. Core must validate the
/// exact initial entry/arguments, frozen plan, actual creator and entry/overlay/
/// resume path before returning this view.
pub struct ReachedInvocationMetadata<'a> {
    pub invocation: &'a Digest,
    pub entry_grant: &'a GrantId,
    pub entry_effect: &'a EffectRequest,
    pub entry_args: &'a Digest,
    pub entry_context: &'a CallCx,
    pub entry_deadline: std::time::Instant,
    pub current: &'a CallCx,
    pub active: BorrowedActiveFrameMetadata<'a>,
}
/// Core-only current-state attestation for subsequent control preparation.
/// Implement this on a stack proof borrowing the SAME already-locked node
/// state used by `PreparedFrameVerifier::with_preparation`. Authentication must
/// reject terminal/cancelled/expired/retired invocations and closed admission,
/// and bind the actual current activation, node, creator and provenance. It
/// must not acquire that node lock recursively, construct proof from immutable
/// descriptors alone, or infer liveness from a grant ID. This interface grants
/// neither PortCall authority nor another consume, terminal outcome or cost.
pub trait TypedReachedInvocation {
    fn authenticate<'a>(
        &'a self,
        cx: &CallCx,
        reason: AllocationReason,
    ) -> Result<ReachedInvocationMetadata<'a>, GateError>;
}
pub enum PreparedFrameEntry<'a> {
    /// FIRST entry preparation borrows the caller's sole consumed permit. It
    /// stays with caller terminal custody and is never moved into a value.
    InitialEntry(&'a DispatchPermit),
    /// Later preparation requires actual live Core state, not the initial
    /// permit or possession of a compiled descriptor.
    ReachedInvocation(&'a dyn TypedReachedInvocation),
    /// Actual received UI/User invocation before argument admission/claim.
    UiInvocation(PreparedUiInvocation<'a>),
    /// Actual Core read-entry invocation from the explicit frozen Input record.
    NativeInput(PreparedNativeInput<'a>),
    /// Actual trusted User/CLI action before its one ordinary PortCall claim.
    UserInvocation(PreparedUserInvocation<'a>),
}
/// Stack-only view into one authenticated Core entry/activation. The consumed
/// permit stays owned by the caller; preparation does not duplicate terminal
/// custody. Core verifies exact frozen plan/node/target/result-generation path.
pub struct PreparedFrameMetadata<'a> {
    pub caller: &'a CallCx,
    pub entry: PreparedFrameEntry<'a>,
    pub owner: PreparedCodeOwner<'a>,
    pub frame: &'a Digest,
    pub node: &'a Digest,
    pub activation: u64,
    pub path: &'a Digest,
    pub transitive_memory_bytes: Option<u64>,
}
/// No owned verifier registration or ArcAny proof is needed to reserve the
/// first real frame/control allocation. Core's private stack verifier enforces
/// the SAME semantics as its later registered verifier under the same active
/// state. It may call the fixed synchronous callback exactly once. Borrowed
/// proof/metadata cannot escape; transfer the same activation after funding.
pub trait PreparedFrameVerifier {
    fn with_preparation(
        &self,
        cx: &CallCx,
        reason: AllocationReason,
        admit: &mut dyn FnMut(PreparedFrameMetadata<'_>) -> Result<(), GateError>,
    ) -> Result<(), GateError>;
}
pub struct PreparedFrameFunding<'a> {
    pub metadata: PreparedFrameMetadata<'a>,
    pub limit: u64,
}

/// Immutable metadata returned only after Core verifies its own private proof.
/// `node` is the exact sealed witness ID for a witnessed port node. `path`
/// commits the actual entry/overlay/resume provenance, not a guest label.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ActiveFrameMetadata {
    pub profile: ProfileId,
    pub revision: Digest,
    pub security_epoch: u64,
    pub principal: Principal,
    pub request: RequestId,
    pub frame: Digest,
    pub node: Digest,
    pub activation: u64,
    pub owner: CompiledCodeOwner,
    pub reason: AllocationReason,
    pub path: Digest,
    pub transitive_memory_bytes: Option<u64>,
}
/// Allocation-free view of a Core-authenticated fresh reached node. The private
/// factory may borrow the current context and frozen AST while the first
/// reservation is still pending; owned metadata is built only after funding.
pub struct BorrowedActiveFrameMetadata<'a> {
    pub profile: &'a ProfileId,
    pub revision: &'a Digest,
    pub security_epoch: u64,
    pub principal: &'a Principal,
    pub request: &'a RequestId,
    pub frame: &'a Digest,
    pub node: &'a Digest,
    pub activation: u64,
    pub owner: PreparedCodeOwner<'a>,
    pub reason: AllocationReason,
    pub path: &'a Digest,
    pub transitive_memory_bytes: Option<u64>,
}
impl ActiveFrameMetadata {
    /// Heap copies only. The actual enclosing proof/control layout must also be
    /// prepaid and retained until physical deallocation.
    pub fn clone_storage_bytes(&self) -> Result<usize, tobkiri_canon::AllocationError> {
        use tobkiri_canon::AllocationError;
        self.profile
            .clone_storage_bytes()
            .checked_add(self.principal.clone_storage_bytes()?)
            .and_then(|bytes| bytes.checked_add(self.request.clone_storage_bytes()))
            .and_then(|bytes| bytes.checked_add(self.owner.clone_storage_bytes()))
            .ok_or(AllocationError::Overflow)
    }
    pub fn try_clone_precharged(&self) -> Result<Self, tobkiri_canon::AllocationError> {
        Ok(Self {
            profile: self.profile.try_clone_precharged()?,
            revision: self.revision.clone(),
            security_epoch: self.security_epoch,
            principal: self.principal.try_clone_precharged()?,
            request: self.request.try_clone_precharged()?,
            frame: self.frame.clone(),
            node: self.node.clone(),
            activation: self.activation,
            owner: self.owner.try_clone_precharged()?,
            reason: self.reason,
            path: self.path.clone(),
            transitive_memory_bytes: self.transitive_memory_bytes,
        })
    }
}
/// Exact physical dispatch proposal presented to Core's private linear node.
pub struct FrameDispatch<'a> {
    pub view: &'a tobkiri_canon::ids::ShapeRef,
    pub op: &'a tobkiri_canon::ids::OpName,
    pub args: &'a Digest,
    pub target: &'a FrozenPortTarget,
}
/// Trusted Core injects one verifier for its private execution factory. It must
/// downcast and check the actual active frame/reached node/reason/path on EVERY
/// call. Comparing caller-supplied metadata or an owner registration is invalid.
/// Verifiers retain only prepaid authentication state, never a Gate-owning
/// factory. Shared storage has no Weak or ownership cycle.
pub trait ActiveFrameVerifier: Send + Sync {
    /// Immutable identity of this ACTUAL Core factory, stored in its prepaid
    /// private authentication state. A plan/owner digest is not a factory ID.
    fn factory_identity(&self) -> &Digest;
    /// Close new preparation/claims under the same Core admission critical
    /// section as retirement/allocation/dispatch. No asynchronous cleanup is
    /// implied; already claimed terminal/cleanup custody remains valid.
    fn close_registration(&self);
    /// Allocation-free immutable proof inspection. Do not lock Core's node
    /// state here: Gate also calls this while `with_allocation` already holds
    /// that state. New allocation authority exists only inside with_allocation;
    /// a successful inspection alone never establishes current liveness.
    fn authenticate<'a>(
        &self,
        cx: &CallCx,
        proof: &'a (dyn Any + Send + Sync),
        reason: AllocationReason,
    ) -> Result<&'a ActiveFrameMetadata, GateError>;
    /// Borrow the immutable funded context inside this actual private proof.
    fn bound_context<'a>(
        &self,
        _proof: &'a (dyn Any + Send + Sync),
    ) -> Result<&'a CallCx, GateError> {
        Err(GateError::Unavailable)
    }
    /// Fixed Gate inspection only, under the original admission and SAME node
    /// mutex. The stack proof borrows those guards and never recursively locks.
    /// No guest/effect callback, await or cascading final-owner drop is allowed.
    fn with_reached_invocation(
        &self,
        _cx: &CallCx,
        _proof: &(dyn Any + Send + Sync),
        _reason: AllocationReason,
        _inspect: &mut dyn FnMut(&dyn TypedReachedInvocation) -> Result<(), GateError>,
    ) -> Result<(), GateError> {
        Err(GateError::Unavailable)
    }
    /// Exact private carrier layout, including its original release plan.
    fn reached_evidence_storage_bytes(&self) -> Result<u64, GateError> {
        Err(GateError::Unavailable)
    }
    /// Called only by Gate's fixed inspection while its live proof is locked.
    /// Retain metadata only; native execution/terminal custody must stay out.
    fn retain_reached_evidence(
        &self,
        _proof: &(dyn Any + Send + Sync),
        _funding: ControlReservation,
    ) -> Result<ControlHandle<dyn Any + Send + Sync>, GateError> {
        Err(GateError::Unavailable)
    }
    /// Compare actual private leaf object identity, never descriptive fields.
    fn match_reached_evidence(
        &self,
        _proof: &(dyn Any + Send + Sync),
        _reference: &(dyn Any + Send + Sync),
    ) -> Result<(), GateError> {
        Err(GateError::Unavailable)
    }
    /// Immutable historical description only; this grants no current work.
    fn reached_evidence_view<'a>(
        &self,
        _reference: &'a (dyn Any + Send + Sync),
    ) -> Result<ReachedEvidenceView<'a>, GateError> {
        Err(GateError::Unavailable)
    }
    /// The parent is fixed by the original authenticated dispatch. The caller
    /// prepaid this carrier and keeps the original leaf binding separately.
    fn retain_reached_parent(
        &self,
        _reference: &(dyn Any + Send + Sync),
        _funding: ControlReservation,
    ) -> Result<ControlHandle<dyn Any + Send + Sync>, GateError> {
        Err(GateError::Unavailable)
    }
    /// Borrow the original Input caller inside a completed source proof.
    fn source_context<'a>(
        &self,
        _proof: &'a (dyn Any + Send + Sync),
    ) -> Result<&'a CallCx, GateError> {
        Err(GateError::Unavailable)
    }
    /// Complete retained proof/control bookkeeping, independently prepaid by
    /// the Core factory. A sizeof opaque handle is not a transitive inventory.
    fn retained_control_bytes(&self, _proof: &(dyn Any + Send + Sync)) -> Result<u64, GateError> {
        Err(GateError::Unavailable)
    }
    /// Hold Core's actual node-admission critical section through this fixed
    /// synchronous allocation callback. Retirement and dispatch claim use the
    /// same state. Call exactly once while live; never invoke guest/effect code
    /// or await. No callback may reenter Core's frame state.
    fn with_allocation(
        &self,
        _cx: &CallCx,
        _proof: &(dyn Any + Send + Sync),
        _reason: AllocationReason,
        _admit: &mut dyn FnMut(&ActiveFrameMetadata) -> Result<(), GateError>,
    ) -> Result<(), GateError> {
        Err(GateError::Unavailable)
    }
    /// Atomically claim this actual Core activation once at final dispatch.
    /// Funding checks do not claim it or create another activation counter.
    fn claim_dispatch(
        &self,
        _cx: &CallCx,
        _proof: &(dyn Any + Send + Sync),
        _dispatch: FrameDispatch<'_>,
    ) -> Result<(), GateError> {
        Err(GateError::Unavailable)
    }
    /// Claim one real, completed NativeInput read from this factory's live
    /// provider-output node. Verify the actual consumed physical-read permit,
    /// original Input caller, frozen endpoint and exact produced payload, and
    /// reject unknown physical cleanup. Mint before provider retirement/terminal;
    /// the resulting receipt conveys immutable evidence only.
    fn claim_source_read<'a>(
        &self,
        _original_input: &CallCx,
        _proof: &'a (dyn Any + Send + Sync),
        _payload: &Digest,
    ) -> Result<&'a crate::SourceReadMetadata, GateError> {
        Err(GateError::Unavailable)
    }
}
type VerifierHandle = ControlHandle<dyn ActiveFrameVerifier>;
/// Each sealed record is its own exact index token. There is no growing central
/// map, infallible tree insertion, uncharged anchor, or permanent verifier pin.
pub(crate) struct FrameRegistry {
    instance: u64,
    process: u32,
}
impl Default for FrameRegistry {
    fn default() -> Self {
        static NEXT: AtomicU64 = AtomicU64::new(1);
        Self {
            instance: NEXT
                .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |next| {
                    next.checked_add(1)
                })
                .unwrap_or(0),
            process: std::process::id(),
        }
    }
}
pub(crate) struct FrameRegistrationRecord {
    gate: u64,
    process: u32,
    profile: ProfileId,
    revision: Digest,
    factory: Digest,
    live: AtomicBool,
    verifier: VerifierHandle,
}
impl FrameRegistrationRecord {
    pub(crate) fn factory_identity(&self) -> &Digest {
        &self.factory
    }
}
impl std::ops::Deref for FrameRegistrationRecord {
    type Target = dyn ActiveFrameVerifier;
    fn deref(&self) -> &Self::Target {
        self.verifier.get()
    }
}
type RegistrationHandle = ControlHandle<FrameRegistrationRecord>;
/// Noncloneable Session/factory token. Core synchronizes parallel first-entry
/// construction and publishes only one token for its actual factory. Drop
/// closes THAT factory's admission and invalidates exactly this record; scopes
/// retain its original physical funds until their final actual drop.
pub struct FrameVerifierRegistration(pub(crate) RegistrationHandle);
impl Drop for FrameVerifierRegistration {
    fn drop(&mut self) {
        if self.0.process == std::process::id() {
            self.0.verifier.close_registration();
        }
        self.0.live.store(false, Ordering::Release);
    }
}
/// One exact allocation scope. A budget takes it by value and retains it while
/// its outputs exist; sharing the budget does not clone this owned metadata.
pub struct AllocationFrameScope {
    registration: RegistrationHandle,
    proof: ControlHandle<dyn Any + Send + Sync>,
    metadata: ControlHandle<ActiveFrameMetadata>,
    binding: ControlHandle<CallCx>,
    retained_control_bytes: u64,
}
impl std::fmt::Debug for AllocationFrameScope {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("AllocationFrameScope")
            .field("frame", &self.metadata.frame)
            .field("node", &self.metadata.node)
            .field("reason", &self.metadata.reason)
            .finish_non_exhaustive()
    }
}
impl AllocationFrameScope {
    pub fn factory_identity(&self) -> &Digest {
        &self.registration.factory
    }

    pub fn owner(&self) -> &CompiledCodeOwner {
        &self.metadata.owner
    }
    pub fn capacity_owner(&self) -> CapacityOwner {
        match self.owner() {
            CompiledCodeOwner::Pack { pack, .. } => {
                CapacityOwner::pack_owner(self.metadata.profile.clone(), pack.clone())
            }
            CompiledCodeOwner::User { .. } | CompiledCodeOwner::UserDocument { .. } => {
                CapacityOwner::user_owner(self.metadata.profile.clone())
            }
        }
    }
    pub fn pack_hash(&self) -> Option<&PackHash> {
        match self.owner() {
            CompiledCodeOwner::Pack { hash, .. } => Some(hash),
            CompiledCodeOwner::User { .. } | CompiledCodeOwner::UserDocument { .. } => None,
        }
    }
    pub fn profile(&self) -> &ProfileId {
        &self.metadata.profile
    }
    pub fn revision(&self) -> &Digest {
        &self.metadata.revision
    }
    pub fn security_epoch(&self) -> u64 {
        self.metadata.security_epoch
    }
    pub fn frame(&self) -> &Digest {
        &self.metadata.frame
    }
    pub fn node(&self) -> &Digest {
        &self.metadata.node
    }
    pub fn activation(&self) -> u64 {
        self.metadata.activation
    }
    pub fn reason(&self) -> AllocationReason {
        self.metadata.reason
    }
    pub fn path(&self) -> &Digest {
        &self.metadata.path
    }
    /// Inventory only: metadata/context are projections of the already funded
    /// proof. This count is never a substitute for actual prepaid custody.
    pub fn retained_control_bytes(&self) -> u64 {
        self.retained_control_bytes
    }
    pub fn scope_layout_bytes() -> usize {
        std::mem::size_of::<Self>()
    }
}
impl GateEngine {
    pub(crate) fn claim_frame_dispatch(
        &self,
        cx: &CallCx,
        scope: &AllocationFrameScope,
        dispatch: FrameDispatch<'_>,
    ) -> Result<(), GateError> {
        self.authenticate_frame_scope(cx, scope)?;
        scope
            .registration
            .claim_dispatch(cx, scope.proof.as_ref(), dispatch)
    }
    pub fn register_frame_verifier(
        &self,
        _profile: &ProfileId,
        _revision: &Digest,
        _verifier: ControlHandle<dyn ActiveFrameVerifier>,
    ) -> Result<FrameVerifierRegistration, GateError> {
        // An owned verifier handle does not pay a second registration record.
        Err(GateError::Unavailable)
    }
    /// Complete registration allocation inventory: exact carrier/control and
    /// its sole dynamic key copy. The verifier/authentication state has its own
    /// independently retained first-entry reservation. No hidden index exists.
    pub fn frame_verifier_registration_storage_bytes<R>(
        profile: &ProfileId,
    ) -> Result<usize, tobkiri_canon::AllocationError> {
        ControlHandle::<FrameRegistrationRecord>::storage_bytes::<R>()
            .checked_add(profile.clone_storage_bytes())
            .ok_or(tobkiri_canon::AllocationError::Overflow)
    }
    /// FIRST actual creator reservation prepays this complete record and R's
    /// owned storage. Payload/control deallocation precedes release-plan drop.
    pub fn register_frame_verifier_precharged<R>(
        &self,
        profile: &ProfileId,
        revision: &Digest,
        verifier: ControlHandle<dyn ActiveFrameVerifier>,
        release_plan: R,
    ) -> Result<FrameVerifierRegistration, GateError>
    where
        R: Send + Sync + 'static,
    {
        if self.frames.instance == 0 || self.frames.process != std::process::id() {
            return Err(GateError::Unavailable);
        }
        let profile = profile
            .try_clone_precharged()
            .map_err(|_| GateError::Unavailable)?;
        let factory = verifier.factory_identity().clone();
        let record = FrameRegistrationRecord {
            gate: self.frames.instance,
            process: self.frames.process,
            profile,
            revision: revision.clone(),
            factory,
            live: AtomicBool::new(true),
            verifier,
        };
        let record = ControlHandle::try_new_precharged(record, release_plan)
            .map_err(|_| GateError::Unavailable)?;
        Ok(FrameVerifierRegistration(record))
    }
    pub(crate) fn check_frame_registration(
        &self,
        cx: &CallCx,
        registration: &RegistrationHandle,
    ) -> Result<(), GateError> {
        let revision = cx.revision().ok_or(GateError::Unavailable)?;
        if registration.process != std::process::id()
            || self.frames.process != std::process::id()
            || registration.gate != self.frames.instance
            || registration.profile != *cx.profile()
            || registration.revision != *revision
            || !registration.live.load(Ordering::Acquire)
            || registration.factory != *registration.verifier.factory_identity()
        {
            return Err(GateError::Denied(
                "frame verifier belongs to another Gate/revision".into(),
            ));
        }
        Ok(())
    }
    pub fn mint_allocation_scope(
        &self,
        cx: &CallCx,
        registration: &FrameVerifierRegistration,
        proof: ControlHandle<dyn Any + Send + Sync>,
        reason: AllocationReason,
    ) -> Result<AllocationFrameScope, GateError> {
        self.check_context(cx)?;
        self.check_frame_registration(cx, &registration.0)?;
        let mut scope = None;
        let mut callback_error = None;
        let verified = registration
            .0
            .with_allocation(cx, proof.as_ref(), reason, &mut |current| {
                if scope.is_some() || callback_error.is_some() {
                    callback_error = Some(GateError::Denied("scope callback repeated".into()));
                    return Err(GateError::Unavailable);
                }
                let result = (|| {
                    self.check_context(cx)?;
                    self.check_frame_registration(cx, &registration.0)?;
                    check_metadata(cx, current, reason)?;
                    let metadata = proof
                        .try_project(|private| registration.0.authenticate(cx, private, reason))?;
                    if metadata.get() != current {
                        return Err(GateError::Denied("active frame proof changed".into()));
                    }
                    let binding =
                        proof.try_project(|private| registration.0.bound_context(private))?;
                    ContextBinding::check_borrowed(binding.get(), cx)?;
                    let retained_control_bytes =
                        registration.0.retained_control_bytes(proof.as_ref())?;
                    if retained_control_bytes == 0 {
                        return Err(GateError::Unavailable);
                    }
                    self.frame_capacity_from_metadata(cx, current)?;
                    Ok(AllocationFrameScope {
                        registration: registration.0.clone(),
                        proof: proof.clone(),
                        metadata,
                        binding,
                        retained_control_bytes,
                    })
                })();
                match result {
                    Ok(value) => {
                        scope = Some(value);
                        Ok(())
                    }
                    Err(error) => {
                        callback_error = Some(error);
                        Err(GateError::Unavailable)
                    }
                }
            });
        if let Some(error) = callback_error {
            return Err(error);
        }
        verified?;
        scope.ok_or(GateError::Unavailable)
    }
    pub(crate) fn authenticate_frame_scope<'a>(
        &self,
        cx: &CallCx,
        scope: &'a AllocationFrameScope,
    ) -> Result<&'a ActiveFrameMetadata, GateError> {
        self.check_context(cx)?;
        ContextBinding::check_borrowed(scope.binding.get(), cx)?;
        self.check_frame_registration(cx, &scope.registration)?;
        let metadata = scope
            .registration
            .authenticate(cx, scope.proof.as_ref(), scope.reason())?;
        check_metadata(cx, metadata, scope.reason())?;
        if metadata != scope.metadata.get() {
            return Err(GateError::Denied("active frame proof changed".into()));
        }
        Ok(scope.metadata.get())
    }
    pub(crate) fn frame_capacity_limit_inner(
        &self,
        cx: &CallCx,
        scope: &AllocationFrameScope,
    ) -> Result<u64, GateError> {
        let frame = self.authenticate_frame_scope(cx, scope)?;
        self.frame_capacity_from_metadata(cx, frame)
    }
    /// The caller holds Core's node-admission state. This path must not recurse
    /// through authenticate, which could acquire that state a second time.
    fn frame_capacity_from_metadata(
        &self,
        cx: &CallCx,
        frame: &ActiveFrameMetadata,
    ) -> Result<u64, GateError> {
        let owner = match &frame.owner {
            CompiledCodeOwner::Pack { pack, hash } => PreparedCodeOwner::Pack { pack, hash },
            CompiledCodeOwner::User { profile_ast } => PreparedCodeOwner::User { profile_ast },
            CompiledCodeOwner::UserDocument { document } => {
                PreparedCodeOwner::UserDocument { document }
            }
        };
        let own = self.code_owner_memory_limit(cx, owner)?;
        // Unrelated effective-principal numeric limits cannot become this
        // producer's funds. Only separately verified transitive constraints meet.
        let cap = frame
            .transitive_memory_bytes
            .map_or(own, |constraint| own.min(constraint));
        if cap == 0 {
            return Err(GateError::Unavailable);
        }
        Ok(cap)
    }
    pub(crate) fn with_frame_preparation_inner(
        &self,
        cx: &CallCx,
        prepared: &dyn PreparedFrameVerifier,
        reason: AllocationReason,
        admit: &mut dyn FnMut(PreparedFrameFunding<'_>) -> Result<(), GateError>,
    ) -> Result<(), GateError> {
        self.check_context(cx)?;
        let mut called = false;
        let mut callback_error = None;
        let verified = prepared.with_preparation(cx, reason, &mut |metadata| {
            if called {
                callback_error = Some(GateError::Denied(
                    "frame preparation callback repeated".into(),
                ));
                return Err(GateError::Unavailable);
            }
            called = true;
            let result = (|| {
                self.check_context(cx)?;
                metadata.caller.check_active()?;
                if metadata.caller.profile() != cx.profile()
                    || metadata.caller.revision() != cx.revision()
                    || metadata.caller.security_epoch() != cx.security_epoch()
                    || cx.deadline() > metadata.caller.deadline()
                    || metadata.activation == 0
                {
                    return Err(GateError::Denied("prepared entry binding mismatch".into()));
                }
                self.check_prepared_entry(cx, &metadata, reason)?;
                let own = self.code_owner_memory_limit(cx, metadata.owner)?;
                let limit = metadata
                    .transitive_memory_bytes
                    .map_or(own, |cap| own.min(cap));
                if limit == 0 {
                    return Err(GateError::Unavailable);
                }
                // Fixed B first-capacity reservation only. It must acquire the
                // actual retained root owner lease before any owned frame/proof
                // allocation and retain it through fallible construction.
                admit(PreparedFrameFunding { metadata, limit })
            })();
            if let Err(error) = result {
                callback_error = Some(error);
                return Err(GateError::Unavailable);
            }
            Ok(())
        });
        if let Some(error) = callback_error {
            return Err(error);
        }
        verified?;
        if !called {
            return Err(GateError::Unavailable);
        }
        Ok(())
    }
    fn check_prepared_entry(
        &self,
        cx: &CallCx,
        metadata: &PreparedFrameMetadata<'_>,
        reason: AllocationReason,
    ) -> Result<(), GateError> {
        match &metadata.entry {
            PreparedFrameEntry::InitialEntry(entry) => {
                if !entry.matches_context(metadata.caller)
                    || entry.deadline() <= std::time::Instant::now()
                    || cx.deadline() > entry.deadline()
                    || matches!(metadata.owner, PreparedCodeOwner::UserDocument { .. })
                {
                    return Err(GateError::Denied(
                        "consumed invocation entry mismatch".into(),
                    ));
                }
                if self.approvals.terminal_delivery(entry.id())?.is_some() {
                    return Err(GateError::Denied("invocation already terminal".into()));
                }
            }
            PreparedFrameEntry::ReachedInvocation(proof) => {
                let reached = proof.authenticate(cx, reason)?;
                ContextBinding::check_borrowed(reached.current, cx)?;
                check_borrowed_metadata(cx, &reached.active, reason)?;
                let owner_matches = match (metadata.owner, reached.active.owner) {
                    (
                        PreparedCodeOwner::Pack { pack, hash },
                        PreparedCodeOwner::Pack {
                            pack: actual_pack,
                            hash: actual_hash,
                        },
                    ) => pack == actual_pack && hash == actual_hash,
                    (
                        PreparedCodeOwner::User { profile_ast },
                        PreparedCodeOwner::User {
                            profile_ast: actual_ast,
                        },
                    ) => profile_ast == actual_ast,
                    _ => false,
                };
                if !owner_matches
                    || reached.active.frame != metadata.frame
                    || reached.active.node != metadata.node
                    || reached.active.activation != metadata.activation
                    || reached.active.path != metadata.path
                    || reached.active.transitive_memory_bytes != metadata.transitive_memory_bytes
                    || reached.entry_context.profile() != cx.profile()
                    || reached.entry_context.revision() != cx.revision()
                    || reached.entry_context.security_epoch() != cx.security_epoch()
                    || cx.deadline() > reached.entry_context.deadline()
                    || cx.deadline() > reached.entry_deadline
                    || reached.entry_deadline <= std::time::Instant::now()
                {
                    return Err(GateError::Denied(
                        "reached invocation binding mismatch".into(),
                    ));
                }
                reached.entry_context.check_active()?;
                if self
                    .approvals
                    .terminal_delivery(reached.entry_grant)?
                    .is_some()
                {
                    return Err(GateError::Denied("invocation already terminal".into()));
                }
            }
            PreparedFrameEntry::UiInvocation(ui) => {
                if !matches!(
                    reason,
                    AllocationReason::RuntimeValue | AllocationReason::ArgumentDigest
                ) || ui.generation == 0
                    || cx.principal() != metadata.caller.principal()
                    || cx.request() != metadata.caller.request()
                {
                    return Err(GateError::Denied("UI preclaim invocation mismatch".into()));
                }
                match (metadata.caller.principal(), metadata.owner) {
                    (Principal::User, PreparedCodeOwner::User { .. }) => (),
                    (
                        Principal::PackUi {
                            pack,
                            hash,
                            ui: session,
                        },
                        PreparedCodeOwner::Pack {
                            pack: owner,
                            hash: owner_hash,
                        },
                    ) if pack == owner && hash == owner_hash && session == ui.session => (),
                    _ => {
                        return Err(GateError::Denied(
                            "UI funding owner is not its actual caller".into(),
                        ));
                    }
                }
                self.validate_exact_operation(metadata.caller, ui.view, ui.op)?;
                let revision = cx.revision().ok_or(GateError::Unavailable)?;
                if !self.frozen_target_registered(cx.profile(), revision, ui.view, ui.target)?
                    || !self
                        .effective_permissions(metadata.caller)?
                        .ports
                        .contains(&ui.view.to_string())
                {
                    return Err(GateError::Denied(
                        "UI preclaim exceeds existing exact authority".into(),
                    ));
                }
            }
            PreparedFrameEntry::NativeInput(input) => {
                if !matches!(
                    reason,
                    AllocationReason::RuntimeValue | AllocationReason::ArgumentDigest
                ) || !matches!(metadata.caller.principal(),Principal::Input{source} if source==input.source)
                    || cx.principal() != metadata.caller.principal()
                    || cx.request() != metadata.caller.request()
                {
                    return Err(GateError::Denied(
                        "native preclaim invocation mismatch".into(),
                    ));
                }
                self.with_registered_input_source(metadata.caller,input.view,input.op,|binding| {
                if &binding.target != input.target
                    || !matches!(metadata.owner,PreparedCodeOwner::Pack{pack,hash} if pack==&binding.pack && hash==&binding.hash)
                {
                    return Err(GateError::Denied(
                        "native funding owner differs from exact source".into(),
                    ));
                }
                    Ok(())
                })?;
                self.input_capacity_limit(metadata.caller, input.view, input.op, input.target)?;
            }
            PreparedFrameEntry::UserInvocation(user) => {
                if !matches!(
                    reason,
                    AllocationReason::RuntimeValue | AllocationReason::ArgumentDigest
                ) || !matches!(metadata.caller.principal(), Principal::User)
                    || cx.principal() != metadata.caller.principal()
                    || cx.request() != metadata.caller.request()
                {
                    return Err(GateError::Denied(
                        "User preclaim invocation mismatch".into(),
                    ));
                }
                self.validate_user_policy_document(
                    metadata.caller,
                    user.user_document,
                    user.revision,
                    user.epoch,
                )?;
                if !matches!(
                    metadata.owner,
                    PreparedCodeOwner::UserDocument { document }
                        if document == user.user_document
                ) {
                    return Err(GateError::Denied(
                        "User funding owner differs from registered document".into(),
                    ));
                }
                self.validate_exact_operation(metadata.caller, user.view, user.op)?;
                let revision = cx.revision().ok_or(GateError::Unavailable)?;
                if !self.frozen_target_registered(cx.profile(), revision, user.view, user.target)?
                    || !self
                        .effective_permissions(metadata.caller)?
                        .ports
                        .contains(&user.view.to_string())
                {
                    return Err(GateError::Denied(
                        "User preclaim exceeds current exact authority".into(),
                    ));
                }
                self.check_frozen_provider(metadata.caller, &user.target.pack, &user.target.hash)?;
            }
        }
        Ok(())
    }
    pub(crate) fn with_frame_allocation_inner(
        &self,
        cx: &CallCx,
        scope: &AllocationFrameScope,
        admit: &mut dyn FnMut(u64) -> Result<(), GateError>,
    ) -> Result<(), GateError> {
        // Drop every Gate registry guard/SQLite transaction before waiting on
        // Core's node state. Inside it the fixed order is Core -> Gate -> DB.
        self.check_context(cx)?;
        ContextBinding::check_borrowed(scope.binding.get(), cx)?;
        self.check_frame_registration(cx, &scope.registration)?;
        let mut called = false;
        let mut callback_error = None;
        let verified = scope.registration.with_allocation(
            cx,
            scope.proof.as_ref(),
            scope.reason(),
            &mut |metadata| {
                if called {
                    callback_error = Some(GateError::Denied("allocation callback repeated".into()));
                    return Err(GateError::Unavailable);
                }
                called = true;
                let result = (|| {
                    self.check_context(cx)?;
                    self.check_frame_registration(cx, &scope.registration)?;
                    ContextBinding::check_borrowed(scope.binding.get(), cx)?;
                    check_metadata(cx, metadata, scope.reason())?;
                    if metadata != scope.metadata.get() {
                        return Err(GateError::Denied("active frame proof changed".into()));
                    }
                    let cap = self.frame_capacity_from_metadata(cx, metadata)?;
                    // Fixed trusted capacity reserve/resize only, no guest/effect
                    // callback, reentrant Core operation or async suspension.
                    admit(cap)
                })();
                if let Err(error) = result {
                    callback_error = Some(error);
                    return Err(GateError::Unavailable);
                }
                Ok(())
            },
        );
        if let Some(error) = callback_error {
            return Err(error);
        }
        verified?;
        if !called {
            return Err(GateError::Unavailable);
        }
        Ok(())
    }
}
fn check_metadata(
    cx: &CallCx,
    metadata: &ActiveFrameMetadata,
    reason: AllocationReason,
) -> Result<(), GateError> {
    if metadata.profile != *cx.profile()
        || cx.revision() != Some(&metadata.revision)
        || metadata.security_epoch != cx.security_epoch()
        || metadata.principal != *cx.principal()
        || metadata.request != *cx.request()
        || metadata.activation == 0
        || metadata.reason != reason
    {
        return Err(GateError::Denied(
            "active frame proof binding mismatch".into(),
        ));
    }
    Ok(())
}
fn check_borrowed_metadata(
    cx: &CallCx,
    metadata: &BorrowedActiveFrameMetadata<'_>,
    reason: AllocationReason,
) -> Result<(), GateError> {
    if metadata.profile != cx.profile()
        || cx.revision() != Some(metadata.revision)
        || metadata.security_epoch != cx.security_epoch()
        || metadata.principal != cx.principal()
        || metadata.request != cx.request()
        || metadata.activation == 0
        || metadata.reason != reason
    {
        return Err(GateError::Denied(
            "active frame proof binding mismatch".into(),
        ));
    }
    Ok(())
}
