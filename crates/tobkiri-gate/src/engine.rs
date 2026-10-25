use crate::protocol::strict_bytes;
use crate::*;
use async_trait::async_trait;
use ed25519_dalek::{Signature, VerifyingKey};
use std::{
    collections::{BTreeMap, BTreeSet},
    sync::atomic::{AtomicU64, Ordering},
    sync::{Arc, Mutex},
    time::{SystemTime, UNIX_EPOCH},
};
use tobkiri_canon::{
    Digest, Variant,
    ids::{OpName, PackHash, PackId, ProfileId, RequestId, ShapeRef},
};

#[path = "permission_preparation.rs"]
mod permission_preparation;
pub use permission_preparation::{
    PermissionApprovalBatch, PermissionCommit, PreparedPermissionApproval,
};
use permission_preparation::{PreparedPermissionTarget, retained_permission_bytes};

fn try_clone_string_precharged(value: &str) -> Result<String, tobkiri_canon::AllocationError> {
    let mut copy = String::new();
    copy.try_reserve_exact(value.len())
        .map_err(|_| tobkiri_canon::AllocationError::Allocator)?;
    copy.push_str(value);
    Ok(copy)
}

fn cancellation_result(
    cx: &CallCx,
    result: Result<CancellationCause, CancellationError>,
) -> GateError {
    if result.is_err() {
        return GateError::Unavailable;
    }
    // Notification origin never decides durable terminal disposition. Inspect
    // the actual original deadline; terminal CAS independently verifies cause.
    cx.check_active().err().unwrap_or(GateError::Cancelled)
}

/// Three independent authority ceilings. Approvals are fetched from core storage.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PackPolicy {
    pub declaration: PermissionSet,
    pub ceiling: PermissionSet,
    pub pack_limits: PermissionSet,
}
/// Trusted Core's exact verified physical Input source and narrow routing view.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct InputSourceBinding {
    pub revision: Digest,
    pub pack: PackId,
    pub hash: PackHash,
    pub entry_view: ShapeRef,
    pub entry_op: OpName,
    pub target: FrozenPortTarget,
    pub declaration: PermissionSet,
    pub projection: PermissionSet,
}
type InputSourceBindings = BTreeMap<(String, String, Digest, ShapeRef, OpName), InputSourceBinding>;
type InputSourceGroups = BTreeMap<(String, String, Digest), BTreeSet<(ShapeRef, OpName)>>;
impl PackPolicy {
    pub fn maximum(&self) -> PermissionSet {
        self.declaration
            .intersect(&self.ceiling)
            .intersect(&self.pack_limits)
    }
}

/// Non-forgeable, non-cloneable one-time dispatch authority.
pub struct Grant {
    pub(crate) reservation: Reservation,
    pub(crate) request: EffectRequest,
    pub(crate) limits: ResourceLimits,
    pub(crate) trusted_approval: Option<TrustedApprovalEvidence>,
    pub(crate) approvals: Arc<dyn ApprovalStore>,
    pub(crate) audit: Arc<dyn AuditSink>,
    pub(crate) consumed: std::sync::atomic::AtomicBool,
    pub(crate) consumption_attempted: std::sync::atomic::AtomicBool,
}
impl std::fmt::Debug for Grant {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Grant")
            .field("id", &self.reservation.id)
            .finish_non_exhaustive()
    }
}
impl Grant {
    pub fn id(&self) -> &GrantId {
        &self.reservation.id
    }
    pub fn effect(&self) -> &EffectRequest {
        &self.request
    }
    pub fn args_digest(&self) -> &Digest {
        &self.reservation.args
    }
    pub fn limits(&self) -> &ResourceLimits {
        &self.limits
    }
    pub fn expires_unix_ms(&self) -> u64 {
        self.reservation.expires_unix_ms
    }
    /// Consumes the durable reservation atomically before an effect can execute.
    pub fn consume(
        self,
        cx: &CallCx,
        request: &EffectRequest,
        args: &Digest,
    ) -> Result<DispatchPermit, GateError> {
        self.consume_owned(cx, request, args).into_legacy_result()
    }
    /// Native ownership-preserving consumption. An error from the durable
    /// store is not proof that its transaction did not commit.
    pub fn consume_owned(
        self,
        cx: &CallCx,
        request: &EffectRequest,
        args: &Digest,
    ) -> ConsumptionAttempt {
        crate::consumption::consume(self, cx, request, args)
    }
}
fn deadline_unix_ms(deadline: std::time::Instant) -> Result<u64, GateError> {
    let remaining = deadline
        .saturating_duration_since(std::time::Instant::now())
        .as_millis();
    unix_now_ms()?
        .checked_add(
            u64::try_from(remaining).map_err(|_| GateError::Limit("deadline range".into()))?,
        )
        .filter(|n| *n <= 9_007_199_254_740_991)
        .ok_or_else(|| GateError::Limit("deadline range".into()))
}
fn instant_expiry_bound(
    expires: u64,
    ceiling: std::time::Instant,
) -> Result<std::time::Instant, GateError> {
    let remaining = expires.saturating_sub(unix_now_ms()?);
    Ok(std::time::Instant::now()
        .checked_add(std::time::Duration::from_millis(remaining))
        .map_or(ceiling, |bound| bound.min(ceiling)))
}
fn unix_now_ms() -> Result<u64, GateError> {
    u64::try_from(
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| GateError::Invalid("system clock before Unix epoch".into()))?
            .as_millis(),
    )
    .map_err(|_| GateError::Limit("system clock outside supported range".into()))
}
fn abandon_reservation(approvals: &dyn ApprovalStore, audit: &dyn AuditSink, id: &GrantId) {
    if let Ok(Some(delivery)) = approvals.abandon_terminal(id) {
        let record = delivery.decision;
        if !delivery.acknowledged
            && audit
                .append_idempotent(&record.event_id, record.event.clone())
                .is_ok()
        {
            let _ = approvals.acknowledge_terminal(&record.event_id, &record.event);
        }
    }
}
/// Ensures a denied or cancelled authorization never leaks a pending reservation.
struct PendingReservation<'a> {
    approvals: &'a dyn ApprovalStore,
    audit: &'a dyn AuditSink,
    id: GrantId,
    armed: bool,
}
impl Drop for PendingReservation<'_> {
    fn drop(&mut self) {
        if self.armed {
            abandon_reservation(self.approvals, self.audit, &self.id);
        }
    }
}
impl Drop for Grant {
    fn drop(&mut self) {
        if !self.consumed.load(Ordering::Acquire)
            && !self.consumption_attempted.load(Ordering::Acquire)
        {
            abandon_reservation(
                self.approvals.as_ref(),
                self.audit.as_ref(),
                &self.reservation.id,
            );
        }
    }
}
/// Proof that a single reservation has been consumed. Its constructor is private.
#[derive(Debug)]
pub struct DispatchPermit {
    pub(crate) reservation: Reservation,
    pub(crate) request: EffectRequest,
    pub(crate) limits: ResourceLimits,
    pub(crate) deadline: std::time::Instant,
    pub(crate) trusted_approval: Option<TrustedApprovalEvidence>,
    // On successful handoff the bounded process slot is immediately reusable.
    // Original store/context custody moves with the existing consumed permit.
    pub(crate) _consumption_owner: crate::consumption::SuccessfulConsumption,
}
/// Immutable descriptive provenance copied only after first-entry funding.
/// This is NOT a permit: it cannot consume, dispatch, settle or own an outcome.
/// Core's private live invocation state must separately authenticate every use.
pub struct ExecutionEntryEvidence {
    grant: GrantId,
    effect: EffectRequest,
    args: Digest,
    profile: ProfileId,
    principal: Principal,
    request: RequestId,
    revision: Option<Digest>,
    epoch: u64,
    limits: ResourceLimits,
    deadline: std::time::Instant,
    trusted_approval: Option<TrustedApprovalEvidence>,
}
impl ExecutionEntryEvidence {
    /// Descriptive original approval; usable only through authenticated live
    /// Core state, never as an independent permission or parent-chain proof.
    pub fn trusted_approval(&self) -> Option<&TrustedApprovalEvidence> {
        self.trusted_approval.as_ref()
    }
    pub fn grant(&self) -> &GrantId {
        &self.grant
    }
    pub fn effect(&self) -> &EffectRequest {
        &self.effect
    }
    pub fn args_digest(&self) -> &Digest {
        &self.args
    }
    pub fn revision(&self) -> Option<&Digest> {
        self.revision.as_ref()
    }
    pub fn deadline(&self) -> std::time::Instant {
        self.deadline
    }
    pub fn limits(&self) -> &ResourceLimits {
        &self.limits
    }
    pub fn matches_context(&self, cx: &CallCx) -> bool {
        self.profile == *cx.profile()
            && self.principal == *cx.principal()
            && self.request == *cx.request()
            && self.revision.as_ref() == cx.revision()
            && self.epoch == cx.security_epoch()
    }
}
impl DispatchPermit {
    /// Complete dynamic provenance copy; the enclosing funded Core control
    /// layout is additional. Only real execution-entry effects are supported.
    pub fn execution_entry_evidence_storage_bytes(
        &self,
    ) -> Result<usize, tobkiri_canon::AllocationError> {
        use tobkiri_canon::AllocationError;
        let effect = match &self.request {
            EffectRequest::FlowRun {
                implements,
                flow_owner: (pack, _),
                flow_key,
                ..
            } => implements
                .clone_storage_bytes()
                .checked_add(pack.clone_storage_bytes())
                .and_then(|bytes| bytes.checked_add(flow_key.len()))
                .ok_or(AllocationError::Overflow)?,
            EffectRequest::PortCall { shape, op } => shape
                .clone_storage_bytes()
                .checked_add(op.clone_storage_bytes())
                .ok_or(AllocationError::Overflow)?,
            EffectRequest::WasmEntry { pack, .. } => pack.clone_storage_bytes(),
            _ => return Err(AllocationError::Unavailable),
        };
        [
            self.reservation.id.clone_storage_bytes(),
            self.reservation.profile.clone_storage_bytes(),
            self.reservation.principal.clone_storage_bytes()?,
            self.reservation.request.clone_storage_bytes(),
            effect,
        ]
        .into_iter()
        .try_fold(0usize, |total, part| {
            total.checked_add(part).ok_or(AllocationError::Overflow)
        })
    }
    pub fn try_execution_entry_evidence_precharged(
        &self,
    ) -> Result<ExecutionEntryEvidence, tobkiri_canon::AllocationError> {
        use tobkiri_canon::AllocationError;
        let effect = match &self.request {
            EffectRequest::FlowRun {
                implements,
                flow_owner: (pack, hash),
                flow_key,
                target_digest,
            } => EffectRequest::FlowRun {
                implements: implements.try_clone_precharged()?,
                flow_owner: (pack.try_clone_precharged()?, hash.clone()),
                flow_key: try_clone_string_precharged(flow_key)?,
                target_digest: target_digest.clone(),
            },
            EffectRequest::PortCall { shape, op } => EffectRequest::PortCall {
                shape: shape.try_clone_precharged()?,
                op: op.try_clone_precharged()?,
            },
            EffectRequest::WasmEntry { pack, hash, entry } => EffectRequest::WasmEntry {
                pack: pack.try_clone_precharged()?,
                hash: hash.clone(),
                entry: entry.clone(),
            },
            _ => return Err(AllocationError::Unavailable),
        };
        Ok(ExecutionEntryEvidence {
            grant: self.reservation.id.try_clone_precharged()?,
            effect,
            args: self.reservation.args.clone(),
            profile: self.reservation.profile.try_clone_precharged()?,
            principal: self.reservation.principal.try_clone_precharged()?,
            request: self.reservation.request.try_clone_precharged()?,
            revision: self.reservation.revision.clone(),
            epoch: self.reservation.epoch,
            limits: self.limits.clone(),
            deadline: self.deadline,
            trusted_approval: self.trusted_approval.clone(),
        })
    }
    /// The actual answer retained from this permit's original authorization.
    /// An unprompted dispatch has no evidence, even if an earlier permission
    /// installation or a different dispatch was interactively approved.
    pub fn trusted_approval(&self) -> Option<&TrustedApprovalEvidence> {
        self.trusted_approval.as_ref()
    }
    pub fn id(&self) -> &GrantId {
        &self.reservation.id
    }
    pub fn limits(&self) -> &ResourceLimits {
        &self.limits
    }
    pub fn effect(&self) -> &EffectRequest {
        &self.request
    }
    pub fn args_digest(&self) -> &Digest {
        &self.reservation.args
    }
    pub fn revision(&self) -> Option<&Digest> {
        self.reservation.revision.as_ref()
    }
    pub fn deadline(&self) -> std::time::Instant {
        self.deadline
    }
    pub fn matches_context(&self, cx: &CallCx) -> bool {
        self.reservation.profile == *cx.profile()
            && self.reservation.principal == *cx.principal()
            && self.reservation.request == *cx.request()
            && self.reservation.revision.as_ref() == cx.revision()
            && self.reservation.epoch == cx.security_epoch()
    }
}

#[derive(PartialEq, Eq)]
struct UserPolicy {
    profile_hash: Digest,
    declaration: PermissionSet,
    ceiling: PermissionSet,
    maximum: PermissionSet,
}
type UserPolicies = BTreeMap<(String, Digest, u64), UserPolicy>;
type OperationViews = BTreeMap<(String, Digest), BTreeMap<ShapeRef, BTreeSet<OpName>>>;
type LeaseCancellations = BTreeMap<GrantId, (ProfileId, CancellationToken, std::time::Instant)>;
type RevisionBudgets = BTreeMap<(String, Digest), BTreeMap<(PackId, PackHash), ResourceLimits>>;

/// Typed failure of the admission supervisor's metadata-only delivery pass
/// over one profile's existing outbox. The audit sink's own `Unavailable`
/// answer at the idempotent append boundary is the only failure given a
/// dedicated origin tag; every other failure retains the original typed
/// `GateError`.
#[derive(Debug)]
pub enum DeliveryFault {
    /// `AuditSink::append_idempotent` returned `GateError::Unavailable`
    /// while delivering an existing immutable terminal record — the known
    /// metadata-sink origin that admission supervision may treat as
    /// transient and resolve by a later durable delivery.
    SinkUnavailable,
    /// Recovery, pending-scan, acknowledgement, admission or any other
    /// failure observed with its original typed error.
    Gate(GateError),
}
impl DeliveryFault {
    /// Stable machine code for supervision health reporting. The tagged sink
    /// outage keeps the `unavailable` code it surfaced with.
    pub fn code(&self) -> &'static str {
        match self {
            Self::SinkUnavailable => GateError::Unavailable.code(),
            Self::Gate(error) => error.code(),
        }
    }
    /// The original gate error when the failure is not the tagged sink
    /// boundary; `None` for `SinkUnavailable`.
    pub fn gate_error(&self) -> Option<&GateError> {
        match self {
            Self::Gate(error) => Some(error),
            Self::SinkUnavailable => None,
        }
    }
}

/// The core gate cannot be replaced by a mediator or an untrusted Pack.
pub struct GateEngine {
    pub(crate) approvals: Arc<dyn ApprovalStore>,
    audit: Arc<dyn AuditSink>,
    prompt: Arc<dyn TrustedPrompt>,
    mediators: MediatorChain,
    policies: Mutex<BTreeMap<(String, String, String), PackPolicy>>,
    inputs: Mutex<BTreeMap<(String, String), PermissionSet>>,
    input_sources: Mutex<InputSourceBindings>,
    input_groups: Mutex<InputSourceGroups>,
    pub(crate) frames: crate::frame::FrameRegistry,
    pub(crate) ingress: crate::ingress::IngressRegistry,
    users: Mutex<UserPolicies>,
    revisions: Mutex<RevisionBudgets>,
    pub(crate) operation_views: Mutex<OperationViews>,
    lease_cancellations: Mutex<LeaseCancellations>,
    pub(crate) admissions: crate::admission::AdmissionRegistry,
    control: CoreControl,
    identity: Arc<()>,
    next_registration: AtomicU64,
}
impl GateEngine {
    fn require_admission(&self, profile: &ProfileId) -> Result<(), GateError> {
        match self.approvals.admission_status(profile)? {
            AdmissionStatus::Allowed => Ok(()),
            AdmissionStatus::IntegrityFault { code } => Err(GateError::Integrity(code)),
        }
    }
    fn notice_integrity(&self, profile: &ProfileId, error: &GateError) -> Result<(), GateError> {
        if let GateError::Integrity(code) = error {
            self.approvals.latch_integrity(profile, *code)?;
        }
        Ok(())
    }
    fn deliver_terminal(&self, delivery: &TerminalDelivery) -> Result<(), GateError> {
        if delivery.acknowledged {
            return Ok(());
        }
        let decision = &delivery.decision;
        if let Err(error) = self
            .audit
            .append_idempotent(&decision.event_id, decision.event.clone())
        {
            self.notice_integrity(&decision.reservation.profile, &error)?;
            return Err(error);
        }
        self.approvals
            .acknowledge_terminal(&decision.event_id, &decision.event)
    }
    pub(crate) async fn audit_denial_result<T>(
        &self,
        cx: &CallCx,
        kind: &'static str,
        result: Result<T, GateError>,
    ) -> Result<T, GateError> {
        match result {
            Ok(value) => Ok(value),
            Err(denial) => {
                if let Err(audit) = self.notice_integrity(cx.profile(), &denial) {
                    return Err(GateError::AuditFailure {
                        denial: Box::new(denial),
                        audit: Box::new(audit),
                    });
                }
                match self.record_denial(cx, kind, denial.code()).await {
                    Ok(()) => Err(denial),
                    Err(audit) => {
                        let audit = match self.notice_integrity(cx.profile(), &audit) {
                            Ok(()) => audit,
                            Err(persistence) => GateError::AuditFailure {
                                denial: Box::new(audit),
                                audit: Box::new(persistence),
                            },
                        };
                        Err(GateError::AuditFailure {
                            denial: Box::new(denial),
                            audit: Box::new(audit),
                        })
                    }
                }
            }
        }
    }
    pub fn new(
        approvals: Arc<dyn ApprovalStore>,
        audit: Arc<dyn AuditSink>,
        prompt: Arc<dyn TrustedPrompt>,
        mediators: MediatorChain,
    ) -> Self {
        Self::new_with_control(
            approvals,
            audit,
            prompt,
            mediators,
            crate::control::default_control_account(),
        )
    }

    /// Production Core injects one account shared with its contexts, retained
    /// catalogs and execution records. This account is an internal runtime
    /// bound and grants no Profile permission.
    pub fn new_with_control(
        approvals: Arc<dyn ApprovalStore>,
        audit: Arc<dyn AuditSink>,
        prompt: Arc<dyn TrustedPrompt>,
        mediators: MediatorChain,
        control: CoreControl,
    ) -> Self {
        Self {
            approvals,
            audit,
            prompt,
            mediators,
            policies: Mutex::new(BTreeMap::new()),
            inputs: Mutex::new(BTreeMap::new()),
            input_sources: Mutex::new(BTreeMap::new()),
            input_groups: Mutex::new(BTreeMap::new()),
            frames: crate::frame::FrameRegistry::default(),
            ingress: crate::ingress::IngressRegistry::default(),
            users: Mutex::new(BTreeMap::new()),
            revisions: Mutex::new(BTreeMap::new()),
            operation_views: Mutex::new(BTreeMap::new()),
            lease_cancellations: Mutex::new(BTreeMap::new()),
            admissions: Default::default(),
            control,
            identity: Arc::new(()),
            next_registration: AtomicU64::new(1),
        }
    }

    pub fn control(&self) -> &CoreControl {
        &self.control
    }

    pub(crate) fn same_gate_identity(&self, identity: &Arc<()>) -> bool {
        Arc::ptr_eq(&self.identity, identity)
    }
    pub(crate) fn registration_gate_identity(&self) -> Arc<()> {
        self.identity.clone()
    }
    pub(crate) fn next_registration_token(&self) -> Result<u64, GateError> {
        self.next_registration
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |next| {
                next.checked_add(1)
            })
            .map_err(|_| GateError::Limit("compiled registration identities exhausted".into()))
    }

    /// Recheck current frozen limits for an already authenticated prepared
    /// CodeOwner. This is a policy lookup only; the owner value is not entry,
    /// frame, dispatch, accounting or permit authority.
    pub fn code_owner_execution_limits(
        &self,
        cx: &CallCx,
        owner: PreparedCodeOwner<'_>,
    ) -> Result<ResourceLimits, GateError> {
        self.check_context(cx)?;
        self.code_owner_limits_inner(cx, owner)
    }

    /// Read the immutable terminal winner for this exact sealed consumed
    /// permit after successful audit acknowledgement. The returned receipt is
    /// descriptive evidence only; it cannot consume or settle another permit.
    pub fn acknowledged_terminal(
        &self,
        permit: &DispatchPermit,
    ) -> Result<AcknowledgedTerminal, GateError> {
        if permit.request.digest()? != permit.reservation.effect {
            return Err(GateError::Denied(
                "consumed permit effect binding is inconsistent".into(),
            ));
        }
        let reservation = self
            .control
            .reserve(ControlKind::Frame, ACKNOWLEDGED_TERMINAL_CONTROL_BYTES)?;
        let terminal = self
            .approvals
            .terminal_delivery(permit.id())?
            .ok_or(GateError::Unavailable)?;
        if !terminal.acknowledged
            || !terminal_matches_permit(&terminal.decision.reservation, permit)
            || matches!(&terminal.decision.outcome, Outcome::Failed { code } if code.len() > 256)
        {
            return Err(GateError::Denied(
                "terminal receipt is unacknowledged or differs from consumed permit".into(),
            ));
        }
        let decision = terminal.decision;
        Ok(AcknowledgedTerminal {
            grant_id: decision.reservation.id,
            outcome: decision.outcome,
            disposition: decision.disposition,
            _reservation: reservation,
        })
    }
    fn cleanup_snapshot_guard(
        &self,
        permit: &DispatchPermit,
    ) -> Result<crate::ControlReservation, GateError> {
        if permit.request.digest()? != permit.reservation.effect
            || permit.reservation.original_expires_unix_ms.is_none()
        {
            return Err(GateError::Denied(
                "cleanup requires an exact consumed permit".into(),
            ));
        }
        // Bounded native scratch for the Store's bounded reservation/terminal
        // records and validation copies. No guest execution allowance is used.
        Ok(self
            .control
            .reserve(ControlKind::Frame, CLEANUP_SNAPSHOT_CONTROL_BYTES)?)
    }
    fn cleanup_snapshot_inspect<'a>(
        state: &'a mut CleanupSnapshotInspection,
        permit: &'a DispatchPermit,
        inspect: &'a mut dyn FnMut(CleanupSnapshot<'_>) -> Result<(), GateError>,
    ) -> impl FnMut(CleanupAuthoritySnapshot<'_>) -> Result<(), GateError> + 'a {
        move |authority| {
            if state.entered {
                state.failed = true;
                return Err(GateError::Unavailable);
            }
            // Entry itself consumes the adapter's single callback opportunity,
            // even when its supplied binding fails validation below. A later
            // busy result cannot turn that failed inspection into a safe retry.
            state.entered = true;
            if !cleanup_reservation_matches_permit(authority.reservation, permit)
                || authority.terminal.is_some_and(|terminal| {
                    !cleanup_reservation_matches_permit(&terminal.decision.reservation, permit)
                        || matches!(
                            terminal.decision.disposition,
                            TerminalDisposition::AbandonedBeforeUse
                                | TerminalDisposition::ExpiredBeforeUse
                        )
                })
            {
                state.failed = true;
                return Err(GateError::Denied(
                    "cleanup snapshot differs from consumed permit".into(),
                ));
            }
            state.invoked = true;
            let result = inspect(CleanupSnapshot {
                authority,
                limits: permit.limits(),
                original_deadline: permit.deadline(),
                observed_at: std::time::Instant::now(),
            });
            state.failed |= result.is_err();
            result
        }
    }
    /// Observe one sealed consumed permit without ordinary admission or
    /// cancellation checks. Caller-held lifecycle/generation/selection guards
    /// precede this call; `inspect` may only select native metadata, with no
    /// locks, Gate/Core reentry, notifications or await. Release all guards
    /// before waking tokens. Teardown failure cannot revoke a native selection.
    pub fn with_cleanup_snapshot(
        &self,
        permit: &DispatchPermit,
        inspect: &mut dyn FnMut(CleanupSnapshot<'_>) -> Result<(), GateError>,
    ) -> Result<(), GateError> {
        let _scratch = self.cleanup_snapshot_guard(permit)?;
        let mut state = CleanupSnapshotInspection::default();
        let result = self.approvals.with_cleanup_snapshot(
            &permit.reservation,
            &mut Self::cleanup_snapshot_inspect(&mut state, permit, inspect),
        );
        match result {
            Ok(()) if !state.invoked || state.failed => Err(GateError::Unavailable),
            result => result,
        }
    }
    /// Typed attempt variant of `with_cleanup_snapshot`. The same consumed
    /// permit validation, bounded scratch, exact reservation/terminal
    /// binding and at-most-once `inspect` rules apply. A reported
    /// `RetryableBusyBeforeCallback` is defensively rejected if `inspect`
    /// was entered, including entry rejected by binding validation: busy is
    /// authentic only before the adapter calls back. Ignored callback errors
    /// and repeated entries cannot produce `Selected`.
    pub fn try_cleanup_snapshot(
        &self,
        permit: &DispatchPermit,
        inspect: &mut dyn FnMut(CleanupSnapshot<'_>) -> Result<(), GateError>,
    ) -> CleanupSnapshotAttempt {
        let _scratch = match self.cleanup_snapshot_guard(permit) {
            Ok(scratch) => scratch,
            Err(error) => return CleanupSnapshotAttempt::Uncertain(error),
        };
        let mut state = CleanupSnapshotInspection::default();
        let attempt = self.approvals.try_cleanup_snapshot(
            &permit.reservation,
            &mut Self::cleanup_snapshot_inspect(&mut state, permit, inspect),
        );
        match attempt {
            CleanupSnapshotAttempt::Selected if !state.invoked || state.failed => {
                CleanupSnapshotAttempt::Uncertain(GateError::Unavailable)
            }
            CleanupSnapshotAttempt::RetryableBusyBeforeCallback if state.entered => {
                CleanupSnapshotAttempt::Uncertain(GateError::Unavailable)
            }
            attempt => attempt,
        }
    }
    pub fn register_operation_views(
        &self,
        profile: &ProfileId,
        revision: &Digest,
        views: BTreeMap<ShapeRef, BTreeSet<OpName>>,
    ) -> Result<(), GateError> {
        let mut registry = self
            .operation_views
            .lock()
            .map_err(|_| GateError::Persistence("operation view lock poisoned".into()))?;
        let key = (profile.to_string(), revision.clone());
        if registry.get(&key).is_some_and(|old| old != &views) {
            return Err(GateError::Denied(
                "compiled operation views are immutable".into(),
            ));
        }
        registry.insert(key, views);
        Ok(())
    }
    /// Persists due decisions and signals cancellation; never claims guest cleanup.
    pub fn recover_profile(&self, profile: &ProfileId) -> Result<u64, GateError> {
        self.recover_profile_at(profile, unix_now_ms()?)
    }
    pub fn recover_profile_at(
        &self,
        profile: &ProfileId,
        now_unix_ms: u64,
    ) -> Result<u64, GateError> {
        self.cancel_due_leases(profile)?;
        let expired = self.approvals.expire_terminal(profile, now_unix_ms)?;
        for decision in &expired {
            let removed = {
                let mut leases = self.lease_cancellations.lock().map_err(|_| {
                    GateError::Persistence("lease cancellation lock poisoned".into())
                })?;
                leases.remove(&decision.reservation.id)
            };
            if let Some((_, token, _)) = removed {
                token.notify_deadline();
            }
        }
        let local = {
            let leases = self
                .lease_cancellations
                .lock()
                .map_err(|_| GateError::Persistence("lease cancellation lock poisoned".into()))?;
            leases
                .iter()
                .filter(|(_, (p, _, _))| p == profile)
                .map(|(id, _)| id.clone())
                .collect::<Vec<_>>()
        };
        // Never hold the live-hook mutex across SQLite reads. A durable terminal
        // decision is immutable, so a new hook for the same ID must also retire.
        let mut completed = Vec::new();
        for id in local {
            if let Some(terminal) = self.approvals.terminal_delivery(&id)? {
                completed.push((
                    id,
                    matches!(
                        terminal.decision.disposition,
                        TerminalDisposition::ExpiredCleanupUnknown
                            | TerminalDisposition::ExpiredBeforeUse
                    ),
                ));
            }
        }
        for (id, expired) in completed {
            let removed = {
                let mut leases = self.lease_cancellations.lock().map_err(|_| {
                    GateError::Persistence("lease cancellation lock poisoned".into())
                })?;
                if leases.get(&id).is_some_and(|(p, _, _)| p == profile) {
                    leases.remove(&id)
                } else {
                    None
                }
            };
            if let Some((_, token, _)) = removed
                && expired
            {
                token.notify_deadline();
            }
        }
        self.require_admission(profile)?;
        Ok(expired.len() as u64)
    }
    pub fn outbox_watermark(&self, profile: &ProfileId) -> Result<u64, GateError> {
        self.approvals.terminal_watermark(profile)
    }
    pub async fn flush_outbox_through(
        &self,
        profile: &ProfileId,
        watermark: u64,
    ) -> Result<u64, GateError> {
        let mut count = 0;
        loop {
            let batch = self
                .approvals
                .pending_terminal_through(profile, 256, watermark)?;
            if batch.is_empty() {
                self.require_admission(profile)?;
                return Ok(count);
            }
            for decision in batch {
                self.deliver_terminal(&TerminalDelivery {
                    decision,
                    acknowledged: false,
                })?;
                count += 1;
                tokio::task::yield_now().await;
            }
        }
    }
    /// Retry only audit delivery, never the effect. Startup admission awaits this.
    pub async fn flush_outbox(&self, profile: &ProfileId) -> Result<u64, GateError> {
        let mut count = 0;
        loop {
            let batch = self.approvals.pending_terminal(profile, 256)?;
            if batch.is_empty() {
                self.require_admission(profile)?;
                return Ok(count);
            }
            for decision in batch {
                self.deliver_terminal(&TerminalDelivery {
                    decision,
                    acknowledged: false,
                })?;
                count += 1;
                tokio::task::yield_now().await;
            }
        }
    }
    /// The admission supervisor's bounded metadata-only pass over the
    /// profile's existing leases and outbox: the same work, in the same
    /// order, as `recover_profile` + `flush_outbox` + the admission check,
    /// but the audit sink's own `Unavailable` at the idempotent append
    /// boundary is reported with its real origin instead of an
    /// indistinguishable `GateError`. It retries delivery of existing
    /// immutable terminal records only — never an effect re-run, a new
    /// grant, a terminal overwrite or a physical-cleanup claim.
    pub async fn supervised_metadata_delivery(
        &self,
        profile: &ProfileId,
    ) -> Result<(), DeliveryFault> {
        self.recover_profile(profile).map_err(DeliveryFault::Gate)?;
        loop {
            let batch = self
                .approvals
                .pending_terminal(profile, 256)
                .map_err(DeliveryFault::Gate)?;
            if batch.is_empty() {
                self.require_admission(profile)
                    .map_err(DeliveryFault::Gate)?;
                return Ok(());
            }
            for decision in batch {
                self.deliver_terminal_for_supervision(&TerminalDelivery {
                    decision,
                    acknowledged: false,
                })?;
                tokio::task::yield_now().await;
            }
        }
    }
    /// `deliver_terminal` with the failure's real origin preserved: the
    /// sink's literal `Unavailable` at the append boundary is tagged
    /// `SinkUnavailable`; every other failure keeps its original error.
    fn deliver_terminal_for_supervision(
        &self,
        delivery: &TerminalDelivery,
    ) -> Result<(), DeliveryFault> {
        let decision = &delivery.decision;
        // Another delivery path can acknowledge this row after the supervisor
        // captured its batch. Re-read the durable decision before asking the
        // sink to verify and append the same event again. A cached ACK alone
        // is insufficient: both the immutable record and payload must match.
        let current = self
            .approvals
            .terminal_delivery(&decision.reservation.id)
            .map_err(DeliveryFault::Gate)?;
        let matches = match &current {
            Some(current) => {
                strict_bytes(&current.decision).map_err(DeliveryFault::Gate)?
                    == strict_bytes(decision).map_err(DeliveryFault::Gate)?
            }
            None => false,
        };
        if !matches {
            let error = GateError::Integrity(IntegrityFaultCode::TerminalRecord);
            self.notice_integrity(&decision.reservation.profile, &error)
                .map_err(DeliveryFault::Gate)?;
            return Err(DeliveryFault::Gate(error));
        }
        if current.is_some_and(|current| current.acknowledged) {
            // This idempotent ACK revalidates the current durable payload too;
            // it must neither rewrite an ACK nor mask intervening corruption.
            return self
                .approvals
                .acknowledge_terminal(&decision.event_id, &decision.event)
                .map_err(DeliveryFault::Gate);
        }
        if let Err(error) = self
            .audit
            .append_idempotent(&decision.event_id, decision.event.clone())
        {
            // A failed integrity notice keeps its own persistence origin;
            // only the sink's literal Unavailable is re-tagged.
            self.notice_integrity(&decision.reservation.profile, &error)
                .map_err(DeliveryFault::Gate)?;
            return Err(match error {
                GateError::Unavailable => DeliveryFault::SinkUnavailable,
                other => DeliveryFault::Gate(other),
            });
        }
        self.approvals
            .acknowledge_terminal(&decision.event_id, &decision.event)
            .map_err(DeliveryFault::Gate)
    }
    pub fn epoch(&self, profile: &ProfileId) -> Result<u64, GateError> {
        self.approvals.security_epoch(profile)
    }
    /// Core registers one immutable verified revision and its exact effective budgets.
    pub fn register_revision(
        &self,
        profile: &ProfileId,
        revision: &Digest,
        limits: BTreeMap<(PackId, PackHash), ResourceLimits>,
    ) -> Result<(), GateError> {
        let mut revisions = self
            .revisions
            .lock()
            .map_err(|_| GateError::Persistence("revision lock poisoned".into()))?;
        let key = (profile.to_string(), revision.clone());
        if let Some(existing) = revisions.get(&key) {
            if existing != &limits {
                return Err(GateError::Denied(
                    "compiled revision budgets are immutable".into(),
                ));
            }
            return Ok(());
        }
        revisions.insert(key, limits);
        Ok(())
    }
    pub(crate) fn frozen_limits(
        &self,
        cx: &CallCx,
        pack: &PackId,
        hash: &PackHash,
    ) -> Result<ResourceLimits, GateError> {
        let revision = cx
            .revision()
            .ok_or_else(|| GateError::Denied("missing compiled revision".into()))?;
        self.frozen_limits_for(cx.profile(), revision, pack, hash)
    }
    pub(crate) fn frozen_limits_for(
        &self,
        profile: &ProfileId,
        revision: &Digest,
        pack: &PackId,
        hash: &PackHash,
    ) -> Result<ResourceLimits, GateError> {
        self.revisions
            .lock()
            .map_err(|_| GateError::Persistence("revision lock poisoned".into()))?
            .get(&(profile.to_string(), revision.clone()))
            .and_then(|map| map.get(&(pack.clone(), hash.clone())))
            .cloned()
            .ok_or_else(|| {
                GateError::Denied("execution pair missing from compiled revision budget map".into())
            })
    }
    pub(crate) fn validate_exact_operation(
        &self,
        cx: &CallCx,
        view: &ShapeRef,
        op: &OpName,
    ) -> Result<(), GateError> {
        let revision = cx.revision().ok_or(GateError::Unavailable)?;
        if !self
            .operation_views
            .lock()
            .map_err(|_| GateError::Persistence("operation view lock poisoned".into()))?
            .get(&(cx.profile().to_string(), revision.clone()))
            .and_then(|views| views.get(view))
            .is_some_and(|operations| operations.contains(op))
        {
            return Err(GateError::Denied(
                "operation absent from exact frozen view".into(),
            ));
        }
        Ok(())
    }
    /// Registration only describes constraints; it does not approve a permission.
    pub fn register_pack(
        &self,
        profile: &ProfileId,
        pack: &PackId,
        hash: &PackHash,
        policy: PackPolicy,
    ) -> Result<(), GateError> {
        let mut policies = self
            .policies
            .lock()
            .map_err(|_| GateError::Persistence("policy lock poisoned".into()))?;
        let key = (profile.to_string(), pack.to_string(), hash.to_string());
        if policies.get(&key).is_some_and(|old| old != &policy) {
            return Err(GateError::Denied("frozen Pack policy is immutable".into()));
        }
        policies.insert(key, policy);
        Ok(())
    }
    /// Core registers a resolved input binding; it permits only its exact ports.
    pub fn register_input(
        &self,
        profile: &ProfileId,
        source: &tobkiri_canon::ids::InputSourceId,
        ports: std::collections::BTreeSet<String>,
        limits: ResourceLimits,
    ) -> Result<(), GateError> {
        let p = PermissionSet {
            ports,
            wasm: limits,
            ..Default::default()
        };
        let mut inputs = self
            .inputs
            .lock()
            .map_err(|_| GateError::Persistence("input policy lock poisoned".into()))?;
        let key = (profile.to_string(), source.to_string());
        if inputs.get(&key).is_some_and(|old| old != &p) {
            return Err(GateError::Denied("frozen input policy is immutable".into()));
        }
        inputs.insert(key, p);
        Ok(())
    }
    /// Core copies the exact explicit frozen Input record; single-entry helper.
    pub fn register_input_source(
        &self,
        profile: &ProfileId,
        source: &tobkiri_canon::ids::InputSourceId,
        binding: InputSourceBinding,
    ) -> Result<(), GateError> {
        self.register_input_sources(profile, source, vec![binding])
    }
    /// Atomically describe one immutable source's complete explicit entry set.
    /// The own-input exception covers these records only, never all provides.
    pub fn register_input_sources(
        &self,
        profile: &ProfileId,
        source: &tobkiri_canon::ids::InputSourceId,
        bindings: Vec<InputSourceBinding>,
    ) -> Result<(), GateError> {
        let first = bindings
            .first()
            .ok_or_else(|| GateError::Denied("empty explicit Input entry set".into()))?;
        if source.as_str() != first.pack.as_str() {
            return Err(GateError::Denied(
                "Input source is not the frozen source Pack".into(),
            ));
        }
        if bindings.len() > 256 {
            return Err(GateError::Limit("Input entry count".into()));
        }
        let entries = bindings
            .iter()
            .map(|binding| (binding.entry_view.clone(), binding.entry_op.clone()))
            .collect::<BTreeSet<_>>();
        if entries.len() != bindings.len()
            || bindings.iter().any(|binding| {
                binding.revision != first.revision
                    || binding.pack != first.pack
                    || binding.hash != first.hash
                    || binding.declaration != first.declaration
                    || binding.projection != first.projection
            })
        {
            return Err(GateError::Denied(
                "Input entries differ from one frozen source policy".into(),
            ));
        }
        let own_views = entries
            .iter()
            .map(|(view, _)| view.clone())
            .collect::<BTreeSet<_>>();
        for binding in &bindings {
            self.validate_input_source_binding(profile, binding, &own_views)?;
        }
        let group_key = (
            profile.to_string(),
            source.to_string(),
            first.revision.clone(),
        );
        let mut groups = self
            .input_groups
            .lock()
            .map_err(|_| GateError::Persistence("Input group registry poisoned".into()))?;
        if groups.get(&group_key).is_some_and(|old| old != &entries) {
            return Err(GateError::Denied(
                "frozen Input entry set is immutable".into(),
            ));
        }
        if groups.len() >= 4096 && !groups.contains_key(&group_key) {
            return Err(GateError::Limit("Input group capacity".into()));
        }
        let mut registry = self
            .input_sources
            .lock()
            .map_err(|_| GateError::Persistence("Input source registry poisoned".into()))?;
        for binding in &bindings {
            let key = (
                profile.to_string(),
                source.to_string(),
                binding.revision.clone(),
                binding.entry_view.clone(),
                binding.entry_op.clone(),
            );
            if registry.get(&key).is_some_and(|old| old != binding) {
                return Err(GateError::Denied("frozen Input source is immutable".into()));
            }
        }
        let new_count = bindings
            .iter()
            .filter(|binding| {
                !registry.contains_key(&(
                    profile.to_string(),
                    source.to_string(),
                    binding.revision.clone(),
                    binding.entry_view.clone(),
                    binding.entry_op.clone(),
                ))
            })
            .count();
        if registry
            .len()
            .checked_add(new_count)
            .is_none_or(|count| count > 4096)
        {
            return Err(GateError::Limit("Input source registry capacity".into()));
        }
        self.register_input(
            profile,
            source,
            first.projection.ports.clone(),
            first.projection.wasm.clone(),
        )?;
        for binding in bindings {
            registry.insert(
                (
                    profile.to_string(),
                    source.to_string(),
                    binding.revision.clone(),
                    binding.entry_view.clone(),
                    binding.entry_op.clone(),
                ),
                binding,
            );
        }
        groups.insert(group_key, entries);
        Ok(())
    }
    fn validate_input_source_binding(
        &self,
        profile: &ProfileId,
        binding: &InputSourceBinding,
        own_views: &BTreeSet<ShapeRef>,
    ) -> Result<(), GateError> {
        let policy = self
            .policies
            .lock()
            .map_err(|_| GateError::Persistence("policy lock poisoned".into()))?
            .get(&(
                profile.to_string(),
                binding.pack.to_string(),
                binding.hash.to_string(),
            ))
            .cloned()
            .ok_or_else(|| GateError::Denied("unregistered Input source Pack".into()))?;
        let maximum = policy.maximum();
        let routing_only = PermissionSet {
            ports: binding.projection.ports.clone(),
            wasm: binding.projection.wasm.clone(),
            ..Default::default()
        };
        if policy.declaration != binding.declaration
            || routing_only != binding.projection
            || !binding.projection.wasm.is_subset_of(&maximum.wasm)
            || binding.target.pack != binding.pack
            || binding.target.hash != binding.hash
            || !self.frozen_target_registered(
                profile,
                &binding.revision,
                &binding.entry_view,
                &binding.target,
            )?
        {
            return Err(GateError::Denied(
                "Input source differs from original verified binding".into(),
            ));
        }
        if !self
            .operation_views
            .lock()
            .map_err(|_| GateError::Persistence("operation view lock poisoned".into()))?
            .get(&(profile.to_string(), binding.revision.clone()))
            .and_then(|views| views.get(&binding.entry_view))
            .is_some_and(|ops| ops.contains(&binding.entry_op))
        {
            return Err(GateError::Denied(
                "Input source operation is not in its exact view".into(),
            ));
        }
        if binding.projection.ports.iter().any(|port| {
            !own_views.iter().any(|view| port == &view.to_string()) && !maximum.ports.contains(port)
        }) {
            return Err(GateError::Denied(
                "Input projection exceeds original literal authority".into(),
            ));
        }
        self.frozen_limits_for(profile, &binding.revision, &binding.pack, &binding.hash)?;
        Ok(())
    }
    pub fn registered_input_source(
        &self,
        cx: &CallCx,
        view: &ShapeRef,
        op: &OpName,
    ) -> Result<InputSourceBinding, GateError> {
        self.with_registered_input_source(cx, view, op, |binding| Ok(binding.clone()))
    }
    pub(crate) fn with_registered_input_source<T>(
        &self,
        cx: &CallCx,
        view: &ShapeRef,
        op: &OpName,
        inspect: impl FnOnce(&InputSourceBinding) -> Result<T, GateError>,
    ) -> Result<T, GateError> {
        let Principal::Input { source } = cx.principal() else {
            return Err(GateError::Denied(
                "native ingress requires Input principal".into(),
            ));
        };
        let revision = cx.revision().ok_or(GateError::Unavailable)?;
        let bindings = self
            .input_sources
            .lock()
            .map_err(|_| GateError::Persistence("Input source registry poisoned".into()))?;
        let binding = bindings
            .iter()
            .find(|((profile, input, rev, entry_view, entry_op), _)| {
                profile == cx.profile().as_str()
                    && input == source.as_str()
                    && rev == revision
                    && entry_view == view
                    && entry_op == op
            })
            .map(|(_, binding)| binding)
            .ok_or_else(|| GateError::Denied("unregistered exact Input source".into()))?;
        inspect(binding)
    }
    /// Legacy registration conveys no explicit revision-bound User authority.
    pub fn register_user_policy(
        &self,
        _profile: &ProfileId,
        _ceiling: PermissionSet,
    ) -> Result<(), GateError> {
        Err(GateError::Unavailable)
    }
    pub fn register_user_policy_for_revision(
        &self,
        profile: &ProfileId,
        profile_hash: &Digest,
        revision: &Digest,
        epoch: u64,
        declaration: PermissionSet,
        ceiling: PermissionSet,
    ) -> Result<(), GateError> {
        let policy = UserPolicy {
            profile_hash: profile_hash.clone(),
            maximum: declaration.intersect(&ceiling),
            declaration,
            ceiling,
        };
        let mut users = self
            .users
            .lock()
            .map_err(|_| GateError::Persistence("user policy lock poisoned".into()))?;
        let key = (profile.to_string(), revision.clone(), epoch);
        if users.get(&key).is_some_and(|old| old != &policy) {
            return Err(GateError::Denied("frozen User policy is immutable".into()));
        }
        users.insert(key, policy);
        Ok(())
    }
    pub(crate) fn validate_user_policy_document(
        &self,
        cx: &CallCx,
        document: &Digest,
        revision: &Digest,
        epoch: u64,
    ) -> Result<(), GateError> {
        if !matches!(cx.principal(), Principal::User)
            || cx.revision() != Some(revision)
            || cx.security_epoch() != epoch
        {
            return Err(GateError::Denied(
                "User document is not bound to this invocation".into(),
            ));
        }
        let policy_matches = self
            .users
            .lock()
            .map_err(|_| GateError::Persistence("user policy lock poisoned".into()))?
            .get(&(cx.profile().to_string(), revision.clone(), epoch))
            .is_some_and(|policy| &policy.profile_hash == document);
        if !policy_matches {
            return Err(GateError::Denied(
                "User document is not the registered policy document".into(),
            ));
        }
        // Resolve the real, current approval as well as the frozen policy.
        let _ = self.effective_permissions(cx)?;
        Ok(())
    }
    fn validate_permission_views(
        &self,
        cx: &CallCx,
        permissions: &PermissionSet,
    ) -> Result<(), GateError> {
        if permissions.ports.is_empty() {
            return Ok(());
        }
        let revision = cx.revision().ok_or_else(|| {
            GateError::Denied("explicit port authority requires a compiled revision".into())
        })?;
        let registries = self
            .operation_views
            .lock()
            .map_err(|_| GateError::Persistence("operation view lock poisoned".into()))?;
        let views = registries
            .get(&(cx.profile().to_string(), revision.clone()))
            .ok_or_else(|| GateError::Denied("compiled exact views unregistered".into()))?;
        for literal in &permissions.ports {
            let shape = ShapeRef::parse(literal)
                .map_err(|_| GateError::Denied("invalid literal permission view".into()))?;
            if !views.contains_key(&shape) {
                return Err(GateError::Denied(
                    "literal permission view absent from frozen revision".into(),
                ));
            }
        }
        Ok(())
    }
    /// Invalidate all outstanding dispatch grants after a security change.
    pub fn revoke_epoch(&self, profile: &ProfileId) -> Result<u64, GateError> {
        self.approvals.advance_security_epoch(profile)
    }
    pub fn effective_permissions(&self, cx: &CallCx) -> Result<PermissionSet, GateError> {
        self.with_effective_permission_layers(cx, |layers| {
            if layers.len() == 1 {
                return layers[0].clone();
            }
            layers[2..]
                .iter()
                .fold(layers[0].intersect(layers[1]), |effective, layer| {
                    effective.intersect(layer)
                })
        })
    }
    /// Read and validate the same original layers on every call. Computing
    /// only numeric limits avoids copying discarded effect-permission sets;
    /// this is neither cached authority nor a substitute for any live check.
    fn effective_execution_limits(&self, cx: &CallCx) -> Result<ResourceLimits, GateError> {
        self.with_effective_permission_layers(cx, |layers| {
            layers[1..]
                .iter()
                .fold(layers[0].wasm.clone(), |limits, layer| {
                    limits.intersect(&layer.wasm)
                })
        })
    }
    /// Fixed internal projections run under the original policy guard, after
    /// the current approval read and every original view validation. Each
    /// branch supplies at least one borrowed layer; no references escape.
    fn with_effective_permission_layers<T>(
        &self,
        cx: &CallCx,
        project: impl FnOnce(&[&PermissionSet]) -> T,
    ) -> Result<T, GateError> {
        if let Some((pack, hash)) = cx.principal().pack() {
            let policies = self
                .policies
                .lock()
                .map_err(|_| GateError::Persistence("policy lock poisoned".into()))?;
            let policy = policies
                .get(&(cx.profile().to_string(), pack.to_string(), hash.to_string()))
                .ok_or_else(|| GateError::Denied("unregistered principal".into()))?;
            let approved = self
                .approvals
                .permissions(cx.profile(), pack, hash)?
                .ok_or(GateError::ApprovalRequired)?;
            self.validate_permission_views(cx, &policy.declaration)?;
            self.validate_permission_views(cx, &policy.ceiling)?;
            self.validate_permission_views(cx, &policy.pack_limits)?;
            self.validate_permission_views(cx, &approved)?;
            Ok(project(&[
                &policy.declaration,
                &policy.ceiling,
                &policy.pack_limits,
                &approved,
            ]))
        } else if let Principal::Input { source } = cx.principal() {
            let inputs = self
                .inputs
                .lock()
                .map_err(|_| GateError::Persistence("input policy lock poisoned".into()))?;
            let permissions = inputs
                .get(&(cx.profile().to_string(), source.to_string()))
                .ok_or_else(|| GateError::Denied("unbound input source".into()))?;
            Ok(project(&[permissions]))
        } else {
            let revision = cx
                .revision()
                .ok_or_else(|| GateError::Denied("User has no frozen explicit policy".into()))?;
            let users = self
                .users
                .lock()
                .map_err(|_| GateError::Persistence("user policy lock poisoned".into()))?;
            let policy = users
                .get(&(
                    cx.profile().to_string(),
                    revision.clone(),
                    cx.security_epoch(),
                ))
                .ok_or_else(|| GateError::Denied("User default deny".into()))?;
            let approved = self
                .approvals
                .user_permissions(
                    cx.profile(),
                    &policy.profile_hash,
                    revision,
                    cx.security_epoch(),
                )?
                .ok_or(GateError::ApprovalRequired)?;
            self.validate_permission_views(cx, &policy.declaration)?;
            self.validate_permission_views(cx, &policy.ceiling)?;
            self.validate_permission_views(cx, &approved)?;
            Ok(project(&[&policy.maximum, &approved]))
        }
    }
    /// Read the actual creator's four original layers without manufacturing a
    /// child context, changing effective authority, or allocating a token.
    pub(crate) fn effective_owner_permissions(
        &self,
        cx: &CallCx,
        owner: PreparedCodeOwner<'_>,
    ) -> Result<PermissionSet, GateError> {
        match owner {
            PreparedCodeOwner::Pack { pack, hash } => {
                let policies = self
                    .policies
                    .lock()
                    .map_err(|_| GateError::Persistence("policy lock poisoned".into()))?;
                let policy = policies
                    .iter()
                    .find(|((profile, id, digest), _)| {
                        profile == cx.profile().as_str()
                            && id == pack.as_str()
                            && digest == hash.as_str()
                    })
                    .map(|(_, policy)| policy)
                    .ok_or(GateError::Unavailable)?;
                let approved = self
                    .approvals
                    .permissions(cx.profile(), pack, hash)?
                    .ok_or(GateError::ApprovalRequired)?;
                self.validate_permission_views(cx, &policy.declaration)?;
                self.validate_permission_views(cx, &policy.ceiling)?;
                self.validate_permission_views(cx, &policy.pack_limits)?;
                self.validate_permission_views(cx, &approved)?;
                Ok(policy.maximum().intersect(&approved))
            }
            PreparedCodeOwner::User { .. } => self.effective_user_owner_permissions(cx, None),
            PreparedCodeOwner::UserDocument { document } => {
                self.effective_user_owner_permissions(cx, Some(document))
            }
        }
    }
    fn effective_user_owner_permissions(
        &self,
        cx: &CallCx,
        expected_document: Option<&Digest>,
    ) -> Result<PermissionSet, GateError> {
        if expected_document.is_some() && !matches!(cx.principal(), Principal::User) {
            return Err(GateError::Denied(
                "User document funding requires the actual User principal".into(),
            ));
        }
        let revision = cx.revision().ok_or(GateError::Unavailable)?;
        let users = self
            .users
            .lock()
            .map_err(|_| GateError::Persistence("User policy lock poisoned".into()))?;
        let policy = users
            .get(&(
                cx.profile().to_string(),
                revision.clone(),
                cx.security_epoch(),
            ))
            .ok_or(GateError::Unavailable)?;
        if expected_document.is_some_and(|document| document != &policy.profile_hash) {
            return Err(GateError::Denied(
                "User funding document differs from registered policy".into(),
            ));
        }
        let approved = self
            .approvals
            .user_permissions(
                cx.profile(),
                &policy.profile_hash,
                revision,
                cx.security_epoch(),
            )?
            .ok_or(GateError::ApprovalRequired)?;
        self.validate_permission_views(cx, &policy.declaration)?;
        self.validate_permission_views(cx, &policy.ceiling)?;
        self.validate_permission_views(cx, &approved)?;
        Ok(policy.maximum.intersect(&approved))
    }
    pub(crate) fn check_frozen_provider(
        &self,
        cx: &CallCx,
        pack: &PackId,
        hash: &PackHash,
    ) -> Result<(), GateError> {
        let limits = self.frozen_limits(cx, pack, hash)?;
        let approved = self.code_owner_limits_inner(cx, PreparedCodeOwner::Pack { pack, hash })?;
        if limits.fuel_per_call == 0
            || limits.memory_mib == 0
            || limits.wall_ms == 0
            || !limits.is_subset_of(&approved)
        {
            return Err(GateError::Unavailable);
        }
        Ok(())
    }
    /// Owner funding lookup does not construct another principal/request or an
    /// owned frame/control object. Authority records remain trusted metadata.
    fn code_owner_limits_inner(
        &self,
        cx: &CallCx,
        owner: PreparedCodeOwner<'_>,
    ) -> Result<ResourceLimits, GateError> {
        let limits = match owner {
            PreparedCodeOwner::Pack { pack, hash } => {
                let maximum = {
                    let policies = self
                        .policies
                        .lock()
                        .map_err(|_| GateError::Persistence("policy lock poisoned".into()))?;
                    let policy = policies
                        .iter()
                        .find(|((profile, id, digest), _)| {
                            profile == cx.profile().as_str()
                                && id == pack.as_str()
                                && digest == hash.as_str()
                        })
                        .map(|(_, policy)| policy)
                        .ok_or_else(|| GateError::Denied("unregistered funding owner".into()))?;
                    self.validate_permission_views(cx, &policy.declaration)?;
                    self.validate_permission_views(cx, &policy.ceiling)?;
                    self.validate_permission_views(cx, &policy.pack_limits)?;
                    policy
                        .declaration
                        .wasm
                        .intersect(&policy.ceiling.wasm)
                        .intersect(&policy.pack_limits.wasm)
                };
                let approved = self
                    .approvals
                    .permissions(cx.profile(), pack, hash)?
                    .ok_or(GateError::ApprovalRequired)?;
                self.validate_permission_views(cx, &approved)?;
                maximum
                    .intersect(&approved.wasm)
                    .intersect(&self.frozen_limits(cx, pack, hash)?)
            }
            PreparedCodeOwner::User { .. } => self.user_owner_limits(cx, None)?,
            PreparedCodeOwner::UserDocument { document } => {
                self.user_owner_limits(cx, Some(document))?
            }
        };
        Ok(limits)
    }
    fn user_owner_limits(
        &self,
        cx: &CallCx,
        expected_document: Option<&Digest>,
    ) -> Result<ResourceLimits, GateError> {
        if expected_document.is_some() && !matches!(cx.principal(), Principal::User) {
            return Err(GateError::Denied(
                "User document funding requires the actual User principal".into(),
            ));
        }
        let revision = cx.revision().ok_or(GateError::Unavailable)?;
        let (maximum, profile_hash) = {
            let policies = self
                .users
                .lock()
                .map_err(|_| GateError::Persistence("User policy lock poisoned".into()))?;
            let policy = policies
                .get(&(
                    cx.profile().to_string(),
                    revision.clone(),
                    cx.security_epoch(),
                ))
                .ok_or_else(|| GateError::Denied("User default deny".into()))?;
            if expected_document.is_some_and(|document| document != &policy.profile_hash) {
                return Err(GateError::Denied(
                    "User funding document differs from registered policy".into(),
                ));
            }
            self.validate_permission_views(cx, &policy.declaration)?;
            self.validate_permission_views(cx, &policy.ceiling)?;
            (policy.maximum.wasm.clone(), policy.profile_hash.clone())
        };
        let approved = self
            .approvals
            .user_permissions(cx.profile(), &profile_hash, revision, cx.security_epoch())?
            .ok_or(GateError::ApprovalRequired)?;
        self.validate_permission_views(cx, &approved)?;
        Ok(maximum.intersect(&approved.wasm))
    }
    pub(crate) fn code_owner_memory_limit(
        &self,
        cx: &CallCx,
        owner: PreparedCodeOwner<'_>,
    ) -> Result<u64, GateError> {
        self.code_owner_limits_inner(cx, owner)?
            .memory_mib
            .checked_mul(1024 * 1024)
            .filter(|bytes| *bytes > 0)
            .ok_or(GateError::Unavailable)
    }
    pub async fn approve_user_permissions(
        &self,
        cx: &CallCx,
        profile_hash: &Digest,
        revision: &Digest,
        target_epoch: u64,
        maximum: &PermissionSet,
    ) -> Result<(), GateError> {
        self.prepare_user_permissions(cx, profile_hash, revision, target_epoch, maximum)
            .await?
            .commit()
    }

    /// Obtain an exact native approval without installing policy or completing
    /// its permission effect. Drop abandons that same reservation.
    pub async fn prepare_user_permissions<'a>(
        &'a self,
        cx: &'a CallCx,
        profile_hash: &'a Digest,
        revision: &'a Digest,
        target_epoch: u64,
        maximum: &PermissionSet,
    ) -> Result<PreparedPermissionApproval<'a>, GateError> {
        if !matches!(cx.principal(), Principal::User) {
            return Err(GateError::Denied(
                "User approval is a trusted core operation".into(),
            ));
        }
        cx.check_active()?;
        // Pending configuration decisions are queued metadata, not execution
        // frames. The owned policy collections are charged separately below.
        let mut retained = self.control.reserve(ControlKind::Queue, 4096)?;
        if self.epoch(cx.profile())? != cx.security_epoch() {
            return Err(GateError::StaleEpoch);
        }
        if target_epoch < cx.security_epoch()
            || target_epoch > cx.security_epoch().saturating_add(1)
        {
            return Err(GateError::Denied(
                "User approval epoch must be current or next committed epoch".into(),
            ));
        }
        let old = self
            .approvals
            .user_permissions(cx.profile(), profile_hash, revision, target_epoch)?
            .unwrap_or_default();
        let additions = lattice_diff(&old, maximum);
        let request = EffectRequest::UserPermissions {
            profile_hash: profile_hash.clone(),
            revision: revision.clone(),
            epoch: target_epoch,
        };
        let args = Digest::hash(&strict_bytes(&(
            profile_hash,
            revision,
            target_epoch,
            maximum,
        ))?);
        let reservation = self.reservation(cx, &request, args)?;
        let _pending = PendingReservation {
            approvals: self.approvals.as_ref(),
            audit: self.audit.as_ref(),
            id: reservation.id.clone(),
            armed: true,
        };
        let mut prompt = self.prompt_for(
            cx,
            request,
            &reservation,
            String::from_utf8(strict_bytes(&additions)?)
                .map_err(|e| GateError::Invalid(e.to_string()))?,
        );
        if additions.native || additions.cross_profile_storage {
            prompt.risk = Risk::Dangerous;
        }
        let narrowed = self.mediate(&prompt, maximum.clone(), cx).await?;
        retained.try_grow(retained_permission_bytes(&narrowed)?)?;
        if additions.has_additions() {
            self.obtain_approval(cx, prompt).await?;
        }
        cx.check_active()?;
        if self.epoch(cx.profile())? != cx.security_epoch() {
            return Err(GateError::StaleEpoch);
        }
        Ok(PreparedPermissionApproval::new(
            self,
            cx,
            reservation,
            PreparedPermissionTarget::User {
                profile_hash,
                revision,
                epoch: target_epoch,
            },
            narrowed,
            _pending,
            retained,
        ))
    }

    fn reservation(
        &self,
        cx: &CallCx,
        req: &EffectRequest,
        args: Digest,
    ) -> Result<Reservation, GateError> {
        self.recover_profile(cx.profile())?;
        self.approvals.reserve_fresh(&ReservationRequest::fresh(
            cx,
            req.digest()?,
            args,
            deadline_unix_ms(cx.deadline())?,
        )?)
    }
    fn prompt_for(
        &self,
        cx: &CallCx,
        req: EffectRequest,
        res: &Reservation,
        summary: String,
    ) -> ApprovalPrompt {
        ApprovalPrompt {
            reservation: res.id.clone(),
            profile: cx.profile().clone(),
            principal: cx.principal().clone(),
            request: cx.request().clone(),
            risk: req.risk(),
            effect: req,
            args_digest: res.args.clone(),
            security_epoch: res.epoch,
            summary,
        }
    }
    async fn mediate(
        &self,
        prompt: &ApprovalPrompt,
        permissions: PermissionSet,
        cx: &CallCx,
    ) -> Result<PermissionSet, GateError> {
        let mut narrowed = permissions;
        for mediator in &self.mediators.reviews {
            cx.check_active()?;
            let cancellation = self
                .prompt
                .cancellation_waiter(cx)
                .map_err(|_| GateError::Unavailable)?;
            let verdict = tokio::select! {
                result=cancellation=>return Err(cancellation_result(cx,result)),
                _=tokio::time::sleep_until(cx.deadline().into())=>return Err(GateError::Deadline),
                verdict=mediator.review(prompt)=>verdict,
            };
            match verdict {
                Verdict::Pass => {}
                Verdict::Deny(reason) => return Err(GateError::Denied(reason)),
                Verdict::Constrain(p) => {
                    if !p.is_subset_of(&narrowed) {
                        return Err(GateError::Denied(
                            "mediator attempted to widen authority".into(),
                        ));
                    }
                    narrowed = p;
                }
            }
        }
        Ok(narrowed)
    }
    async fn obtain_approval(
        &self,
        cx: &CallCx,
        prompt: ApprovalPrompt,
    ) -> Result<TrustedApprovalEvidence, GateError> {
        if !cx.interactive_approval() {
            return Err(GateError::ApprovalRequired);
        }
        let cancellation = self
            .prompt
            .cancellation_waiter(cx)
            .map_err(|_| GateError::Unavailable)?;
        let snapshot = prompt.snapshot_digest()?;
        let answer = tokio::select! {result=cancellation=>return Err(cancellation_result(cx,result)),_ = tokio::time::sleep_until(cx.deadline().into())=>return Err(GateError::Deadline),answer=self.prompt.prompt_approval(prompt.clone())=>answer};
        cx.check_active()?;
        if self.epoch(cx.profile())? != cx.security_epoch() {
            return Err(GateError::StaleEpoch);
        }
        let method = match answer {
            ApprovalAnswer::Approve => TrustedApprovalMethod::Prompt,
            ApprovalAnswer::Deny => return Err(GateError::Denied("user declined".into())),
            ApprovalAnswer::Unavailable => return Err(GateError::ApprovalRequired),
            ApprovalAnswer::Attestation(att) => {
                self.verify_attestation(&prompt, &att)?;
                TrustedApprovalMethod::DeviceAttestation
            }
        };
        Ok(TrustedApprovalEvidence::accepted(
            snapshot,
            method,
            prompt.risk,
        ))
    }
    /// Approve only additions and bind retained permissions to the new Pack hash.
    pub async fn approve_pack_permissions(
        &self,
        cx: &CallCx,
        pack: &PackId,
        old_hash: Option<&PackHash>,
        new_hash: &PackHash,
    ) -> Result<(), GateError> {
        self.prepare_pack_permissions(cx, pack, old_hash, new_hash)
            .await?
            .commit()
    }

    /// Prepare only additions using the ordinary approval path. This value is
    /// neither effective policy nor a successful Profile-commit receipt.
    pub async fn prepare_pack_permissions<'a>(
        &'a self,
        cx: &'a CallCx,
        pack: &'a PackId,
        old_hash: Option<&PackHash>,
        new_hash: &'a PackHash,
    ) -> Result<PreparedPermissionApproval<'a>, GateError> {
        if !matches!(cx.principal(), Principal::User) {
            return Err(GateError::Denied(
                "permission installation is a core user operation".into(),
            ));
        }
        cx.check_active()?;
        let mut retained = self.control.reserve(ControlKind::Queue, 4096)?;
        let maximum = {
            let policies = self
                .policies
                .lock()
                .map_err(|_| GateError::Persistence("policy lock poisoned".into()))?;
            policies
                .get(&(
                    cx.profile().to_string(),
                    pack.to_string(),
                    new_hash.to_string(),
                ))
                .ok_or_else(|| GateError::Denied("unregistered Pack revision".into()))?
                .maximum()
        };
        let old = match old_hash {
            Some(hash) => self
                .approvals
                .permissions(cx.profile(), pack, hash)?
                .unwrap_or_default(),
            None => PermissionSet::default(),
        };
        let additions = lattice_diff(&old, &maximum);
        let effect = EffectRequest::Permissions {
            pack: pack.clone(),
            hash: new_hash.clone(),
        };
        let args = Digest::hash(&strict_bytes(&additions)?);
        let reservation = self.reservation(cx, &effect, args)?;
        let _pending = PendingReservation {
            approvals: self.approvals.as_ref(),
            audit: self.audit.as_ref(),
            id: reservation.id.clone(),
            armed: true,
        };
        let summary = String::from_utf8(strict_bytes(&additions)?)
            .map_err(|e| GateError::Invalid(e.to_string()))?;
        let mut prompt = self.prompt_for(cx, effect, &reservation, summary);
        if additions.native || additions.cross_profile_storage {
            prompt.risk = Risk::Dangerous;
        }
        let approved = self.mediate(&prompt, maximum, cx).await?;
        retained.try_grow(retained_permission_bytes(&approved)?)?;
        if additions.has_additions() {
            self.obtain_approval(cx, prompt).await?;
        }
        cx.check_active()?;
        if self.epoch(cx.profile())? != cx.security_epoch() {
            return Err(GateError::StaleEpoch);
        }
        Ok(PreparedPermissionApproval::new(
            self,
            cx,
            reservation,
            PreparedPermissionTarget::Pack {
                pack,
                hash: new_hash,
            },
            approved,
            _pending,
            retained,
        ))
    }
    fn verify_attestation(
        &self,
        p: &ApprovalPrompt,
        a: &DeviceAttestation,
    ) -> Result<(), GateError> {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| GateError::Invalid("system clock before Unix epoch".into()))?
            .as_millis();
        if a.profile != p.profile
            || a.principal != p.principal
            || a.reservation != p.reservation
            || a.request != p.request
            || a.effect != p.effect.digest()?
            || a.args != p.args_digest
            || a.epoch != p.security_epoch
            || u128::from(a.expires_unix_ms) <= now
            || u128::from(a.expires_unix_ms) > now + 300_000
            || a.nonce.is_empty()
            || a.nonce.len() > 128
        {
            return Err(GateError::Denied(
                "attestation binding or expiry mismatch".into(),
            ));
        }
        let key = self
            .approvals
            .paired_device_key(&p.profile, &a.device)?
            .ok_or_else(|| GateError::Denied("unpaired device".into()))?;
        let mut unsigned = a.clone();
        unsigned.signature.clear();
        let bytes = strict_bytes(&unsigned)?;
        let signature: [u8; 64] = a
            .signature
            .as_slice()
            .try_into()
            .map_err(|_| GateError::Denied("invalid attestation signature".into()))?;
        VerifyingKey::from_bytes(&key)
            .map_err(|_| GateError::Denied("invalid paired device key".into()))?
            .verify_strict(&bytes, &Signature::from_bytes(&signature))
            .map_err(|_| GateError::Denied("forged attestation".into()))?;
        if !self
            .approvals
            .claim_attestation(&p.profile, &a.device, &a.nonce, a.expires_unix_ms)?
        {
            return Err(GateError::Denied("replayed attestation".into()));
        }
        Ok(())
    }
    fn event(
        &self,
        res: &Reservation,
        kind: &str,
        outcome: Option<&Outcome>,
    ) -> Result<(), GateError> {
        let mut object = serde_json::Map::new();
        object.insert(
            "grant".into(),
            serde_json::Value::String(res.id.to_string()),
        );
        object.insert(
            "effect_digest".into(),
            serde_json::Value::String(res.effect.to_string()),
        );
        object.insert(
            "args_digest".into(),
            serde_json::Value::String(res.args.to_string()),
        );
        if let Some(outcome) = outcome {
            object.insert(
                "outcome".into(),
                serde_json::to_value(outcome).map_err(|e| GateError::Invalid(e.to_string()))?,
            );
        }
        let payload = tobkiri_canon::parse(
            &serde_json::to_vec(&object).map_err(|e| GateError::Invalid(e.to_string()))?,
            Variant::StrictV1,
        )
        .map_err(|e| GateError::Invalid(e.to_string()))?;
        self.audit.append(AuditEvent {
            profile: res.profile.clone(),
            principal: res.principal.clone(),
            request: res.request.clone(),
            kind: kind.into(),
            payload,
        })
    }
    pub(crate) async fn authorize_inner(
        &self,
        cx: &CallCx,
        request: EffectRequest,
        args: Digest,
    ) -> Result<Grant, GateError> {
        self.authorize_inner_with_ingress(cx, request, args, None)
            .await
    }
    pub(crate) async fn authorize_inner_with_ingress(
        &self,
        cx: &CallCx,
        request: EffectRequest,
        args: Digest,
        ingress: Option<&CoreIngressPolicy>,
    ) -> Result<Grant, GateError> {
        self.authorize_shared_inner(cx, request, args, ingress, None)
            .await
    }
    /// Dedicated committed-launch authorization. `check_context` supplies the
    /// admission/liveness/epoch/revision/frozen-budget checks; the binding
    /// must exactly match the Proc/Pty request before any reservation,
    /// mediator or prompt work begins. Gate derives the durable args digest
    /// only from the binding and never sees a caller key, raw argv/env or
    /// native identity. The binding is data consistency only, never a proven
    /// FD capability or live caller frame.
    pub(crate) async fn authorize_process_launch_inner(
        &self,
        cx: &CallCx,
        request: EffectRequest,
        binding: &crate::ProcessLaunchBinding,
    ) -> Result<Grant, GateError> {
        self.check_context(cx)?;
        let program = match &request {
            EffectRequest::Proc { program } if binding.pty().is_none() => program,
            EffectRequest::Pty { program } if binding.pty().is_some() => program,
            _ => {
                return Err(GateError::Denied(
                    "request does not match the committed launch kind".into(),
                ));
            }
        };
        if program.as_str() != binding.program_id() {
            return Err(GateError::Denied(
                "launch program differs from the committed binding".into(),
            ));
        }
        self.authorize_shared_inner(
            cx,
            request,
            binding.args_digest().clone(),
            None,
            Some(binding),
        )
        .await
    }
    /// Shared reservation/mediation/approval/dispatch pipeline. Proc/Pty
    /// effects cannot enter it without a committed binding: a bare request
    /// fails closed before any fresh reservation, mediator or prompt.
    async fn authorize_shared_inner(
        &self,
        cx: &CallCx,
        request: EffectRequest,
        args: Digest,
        ingress: Option<&CoreIngressPolicy>,
        binding: Option<&crate::ProcessLaunchBinding>,
    ) -> Result<Grant, GateError> {
        cx.check_active()?;
        if self.epoch(cx.profile())? != cx.security_epoch() {
            return Err(GateError::StaleEpoch);
        }
        if matches!(
            &request,
            EffectRequest::Proc { .. } | EffectRequest::Pty { .. }
        ) {
            let Some(binding) = binding else {
                return Err(GateError::Denied(
                    "process launch requires a committed binding".into(),
                ));
            };
            if !binding.limits().is_subset_of(cx.limits()) {
                return Err(GateError::Denied(
                    "committed launch exceeds live context limits".into(),
                ));
            }
        }
        let effective = self.effective_permissions(cx)?;
        match &request {
            EffectRequest::PortCall { shape, op } => {
                let revision = cx.revision().ok_or_else(|| {
                    GateError::Denied("missing compiled operation revision".into())
                })?;
                let views = self
                    .operation_views
                    .lock()
                    .map_err(|_| GateError::Persistence("operation view lock poisoned".into()))?;
                if !views
                    .get(&(cx.profile().to_string(), revision.clone()))
                    .and_then(|v| v.get(shape))
                    .is_some_and(|ops| ops.contains(op))
                {
                    return Err(GateError::Denied(
                        "operation absent from exact frozen shape view".into(),
                    ));
                }
            }
            EffectRequest::WasmEntry { pack, hash, entry } => {
                if matches!(entry, WasmEntryKind::Init) && cx.interactive_approval() {
                    return Err(GateError::Denied(
                        "init context must be non-interactive".into(),
                    ));
                }
                let limits = self.frozen_limits(cx, pack, hash)?;
                if limits.fuel_per_call == 0
                    || limits.memory_mib == 0
                    || limits.wall_ms == 0
                    || !limits.is_subset_of(&effective.wasm)
                {
                    return Err(GateError::Denied(
                        "execution budget exceeds approved limits".into(),
                    ));
                }
            }
            EffectRequest::FlowRun {
                flow_owner: (pack, hash),
                implements,
                flow_key,
                target_digest,
            } => {
                if flow_key.is_empty()
                    || flow_key.len() > 512
                    || !self.frozen_flow_effect_matches(
                        cx,
                        implements,
                        pack,
                        hash,
                        flow_key,
                        target_digest,
                    )?
                {
                    return Err(GateError::Denied(
                        "FlowRun differs from its exact frozen endpoint".into(),
                    ));
                }
                self.check_frozen_provider(cx, pack, hash)?;
            }
            _ => {}
        }
        let allowed = |permissions: &PermissionSet| match (&request, ingress) {
            (EffectRequest::VirtualInputSubmit { .. }, Some(policy)) => {
                policy.allows_effect(&request, permissions)
            }
            _ => request.allowed_by(permissions, cx),
        };
        if !allowed(&effective) {
            return Err(GateError::Denied(
                "effect exceeds effective permissions".into(),
            ));
        }
        if let Some(binding) = binding
            && !binding.limits().is_subset_of(&effective.wasm)
        {
            return Err(GateError::Denied(
                "committed launch exceeds approved limits".into(),
            ));
        }
        let reservation = self.reservation(cx, &request, args)?;
        let mut pending = PendingReservation {
            approvals: self.approvals.as_ref(),
            audit: self.audit.as_ref(),
            id: reservation.id.clone(),
            armed: true,
        };
        let prompt = match binding {
            Some(binding) => {
                // The committed binding is the trusted summary source; its
                // explicit unsandboxed Dangerous fact overrides the generic
                // elevated risk before mediators and trusted consent see the
                // prompt.
                let mut prompt =
                    self.prompt_for(cx, request.clone(), &reservation, launch_summary(binding));
                prompt.risk = binding.risk();
                prompt
            }
            None => self.prompt_for(
                cx,
                request.clone(),
                &reservation,
                format!("{} effect ({})", request.kind(), reservation.effect),
            ),
        };
        let effective = self.mediate(&prompt, effective, cx).await?;
        if !allowed(&effective) {
            return Err(GateError::Denied(
                "mediator narrowed away the effect".into(),
            ));
        }
        if let Some(binding) = binding
            && !binding.limits().is_subset_of(&effective.wasm)
        {
            return Err(GateError::Denied(
                "committed launch exceeds mediated limits".into(),
            ));
        }
        let first_public_write = match &request {
            EffectRequest::Storage {
                profile,
                owner,
                store,
                tier: StoreTier::Public,
                operation,
            } if operation.is_write() => {
                let (writer, _) = cx
                    .principal()
                    .pack()
                    .ok_or_else(|| GateError::Denied("storage writer must be a Pack".into()))?;
                !self
                    .approvals
                    .public_write_approved(profile, writer, owner, store)?
            }
            _ => false,
        };
        let per_effect = matches!(
            &request,
            EffectRequest::Proc { .. }
                | EffectRequest::Pty { .. }
                | EffectRequest::Secret { reveal: true, .. }
                | EffectRequest::Native { .. }
                | EffectRequest::UserGlobalStorage { .. }
                | EffectRequest::Fs {
                    operation: FsOperation::Write
                        | FsOperation::Rename
                        | FsOperation::Remove
                        | FsOperation::Mkdir,
                    ..
                }
                | EffectRequest::Storage {
                    operation: StoreOperation::Transfer,
                    ..
                }
        );
        let trusted_approval =
            if first_public_write || per_effect || matches!(cx.principal(), Principal::User) {
                Some(self.obtain_approval(cx, prompt).await?)
            } else {
                None
            };
        cx.check_active()?;
        if self.epoch(cx.profile())? != cx.security_epoch() {
            return Err(GateError::StaleEpoch);
        }
        self.event(&reservation, "dispatch", None)?;
        if first_public_write {
            if let EffectRequest::Storage {
                profile,
                owner,
                store,
                ..
            } = &request
            {
                let (writer, _) = cx.principal().pack().unwrap();
                self.approvals.dispatch_public_write(
                    &reservation,
                    &PublicWriteApproval {
                        profile: profile.clone(),
                        writer: writer.clone(),
                        owner: owner.clone(),
                        store: store.clone(),
                    },
                )?;
            }
        } else {
            self.approvals.dispatch(&reservation.id)?;
        }
        pending.armed = false;
        Ok(Grant {
            reservation,
            request,
            trusted_approval,
            // A committed launch grants exactly its committed limits, even
            // when the surrounding context and ceilings are wider.
            limits: match binding {
                Some(binding) => binding.limits().clone(),
                None => effective.wasm.intersect(cx.limits()),
            },
            approvals: self.approvals.clone(),
            audit: self.audit.clone(),
            consumed: std::sync::atomic::AtomicBool::new(false),
            consumption_attempted: std::sync::atomic::AtomicBool::new(false),
        })
    }
}

const ACKNOWLEDGED_TERMINAL_CONTROL_BYTES: u64 = 4096;
const CLEANUP_SNAPSHOT_CONTROL_BYTES: u64 = 32 * 1024;

#[derive(Default)]
struct CleanupSnapshotInspection {
    entered: bool,
    invoked: bool,
    failed: bool,
}

fn cleanup_reservation_matches_permit(reservation: &Reservation, permit: &DispatchPermit) -> bool {
    let expected = &permit.reservation;
    reservation.id == expected.id
        && reservation.profile == expected.profile
        && reservation.principal == expected.principal
        && reservation.request == expected.request
        && reservation.effect == expected.effect
        && reservation.args == expected.args
        && reservation.epoch == expected.epoch
        && reservation.revision == expected.revision
        && reservation.expires_unix_ms <= expected.expires_unix_ms
        && reservation.original_expires_unix_ms == expected.original_expires_unix_ms
        && reservation.authority == expected.authority
}

fn terminal_matches_permit(reservation: &Reservation, permit: &DispatchPermit) -> bool {
    let expected = &permit.reservation;
    reservation.id == expected.id
        && reservation.profile == expected.profile
        && reservation.principal == expected.principal
        && reservation.request == expected.request
        && reservation.effect == expected.effect
        && reservation.args == expected.args
        && reservation.epoch == expected.epoch
        && reservation.revision == expected.revision
        && reservation.expires_unix_ms == expected.expires_unix_ms
        && reservation.original_expires_unix_ms == expected.original_expires_unix_ms
        && reservation.authority == expected.authority
}

/// Finite redacted launch summary built only from immutable binding getters
/// plus fixed text: committed program/catalog/cwd display identities,
/// argv/env counts, PTY presence/geometry, all limits, versions, key
/// identity and the full args digest, plus the explicit unsandboxed
/// Dangerous warning. Raw argv/env values, env names and native-identity
/// payloads never enter Gate.
fn launch_summary(binding: &crate::ProcessLaunchBinding) -> String {
    let pty = match binding.pty() {
        Some(pty) => format!("{}x{}", pty.cols(), pty.rows()),
        None => String::from("none"),
    };
    let limits = binding.limits();
    format!(
        "DANGEROUS unsandboxed process launch: program={} catalog={} cwd={} argv={} env={} pty={} limits=fuel:{},mem_mib:{},wall_ms:{},table:{},out:{} encoding=v{} key_version=v{} key_id={} args_digest={}",
        binding.program_id(),
        binding.catalog_label(),
        binding.cwd_label(),
        binding.argv_count(),
        binding.env_count(),
        pty,
        limits.fuel_per_call,
        limits.memory_mib,
        limits.wall_ms,
        limits.table_elements,
        limits.output_bytes,
        binding.encoding_version(),
        binding.key_version(),
        binding.key_identity(),
        binding.args_digest().hex(),
    )
}

#[async_trait]
impl Gate for GateEngine {
    /// Object-safe delegation to the tested inherent cleanup snapshot. The
    /// A1 validation (exact consumed permit binding, bounded scratch, single
    /// coherent `inspect` invocation) is intentionally not duplicated here.
    fn with_cleanup_snapshot(
        &self,
        permit: &DispatchPermit,
        inspect: &mut dyn FnMut(CleanupSnapshot<'_>) -> Result<(), GateError>,
    ) -> Result<(), GateError> {
        GateEngine::with_cleanup_snapshot(self, permit, inspect)
    }

    /// Object-safe delegation to the tested inherent typed attempt.
    fn try_cleanup_snapshot(
        &self,
        permit: &DispatchPermit,
        inspect: &mut dyn FnMut(CleanupSnapshot<'_>) -> Result<(), GateError>,
    ) -> CleanupSnapshotAttempt {
        GateEngine::try_cleanup_snapshot(self, permit, inspect)
    }

    fn acknowledged_terminal(
        &self,
        permit: &DispatchPermit,
    ) -> Result<AcknowledgedTerminal, GateError> {
        GateEngine::acknowledged_terminal(self, permit)
    }
    fn admission_status(&self, profile: &ProfileId) -> Result<AdmissionStatus, GateError> {
        self.approvals.admission_status(profile)
    }
    async fn admit_port(
        &self,
        cx: &CallCx,
        shape: ShapeRef,
        op: OpName,
        args: Digest,
        target: FrozenPortTarget,
    ) -> Result<PortAdmission, GateError> {
        let result = self.admit_port_inner(cx, shape, op, args, target).await;
        self.audit_denial_result(cx, "port-call", result).await
    }
    async fn admit_compiled(
        &self,
        cx: &CallCx,
        activation: CompiledActivation,
        args: Digest,
    ) -> Result<PortAdmission, GateError> {
        let result = self.admit_compiled_inner(cx, activation, args).await;
        self.audit_denial_result(cx, "port-call", result).await
    }
    async fn claim_port(
        &self,
        cx: &CallCx,
        admission: PortAdmission,
        shape: &ShapeRef,
        op: &OpName,
        args: &Digest,
        target: &FrozenPortTarget,
    ) -> Result<AdmittedDispatch, GateError> {
        let result = self
            .claim_port_inner(cx, admission, shape, op, args, target)
            .await;
        self.audit_denial_result(cx, "port-call", result).await
    }
    fn check_context(&self, cx: &CallCx) -> Result<(), GateError> {
        self.require_admission(cx.profile())?;
        cx.check_active()?;
        if self.epoch(cx.profile())? != cx.security_epoch() {
            return Err(GateError::StaleEpoch);
        }
        let revision = cx
            .revision()
            .ok_or_else(|| GateError::Denied("missing compiled revision".into()))?;
        if !self
            .revisions
            .lock()
            .map_err(|_| GateError::Persistence("revision lock poisoned".into()))?
            .contains_key(&(cx.profile().to_string(), revision.clone()))
        {
            return Err(GateError::Denied("unregistered compiled revision".into()));
        }
        let effective = self.effective_execution_limits(cx)?;
        if let Some((pack, hash)) = cx.principal().pack() {
            let limits = self.frozen_limits(cx, pack, hash)?;
            if limits.fuel_per_call == 0
                || limits.memory_mib == 0
                || limits.wall_ms == 0
                || !limits.is_subset_of(&effective)
                || !cx.limits().is_subset_of(&limits)
            {
                return Err(GateError::Denied(
                    "context exceeds frozen approved execution budget".into(),
                ));
            }
        }
        Ok(())
    }
    async fn authorize(
        &self,
        cx: &CallCx,
        request: EffectRequest,
        args: Digest,
    ) -> Result<Grant, GateError> {
        let kind = request.kind();
        let result = self.authorize_inner(cx, request, args).await;
        self.audit_denial_result(cx, kind, result).await
    }
    /// Committed-launch entry: the same denial-audited pipeline; the binding
    /// supplies the full HMAC args digest and the exact committed limits.
    /// Bare Proc/Pty requests through `authorize` still fail closed.
    async fn authorize_process_launch(
        &self,
        cx: &CallCx,
        request: EffectRequest,
        binding: &crate::ProcessLaunchBinding,
    ) -> Result<Grant, GateError> {
        let kind = request.kind();
        let result = self
            .authorize_process_launch_inner(cx, request, binding)
            .await;
        self.audit_denial_result(cx, kind, result).await
    }
    async fn record_denial(
        &self,
        cx: &CallCx,
        kind: &'static str,
        code: &'static str,
    ) -> Result<(), GateError> {
        let payload = tobkiri_canon::parse(
            &strict_bytes(&(kind, code, cx.security_epoch()))?,
            Variant::StrictV1,
        )
        .map_err(|e| GateError::Invalid(e.to_string()))?;
        self.audit.append(AuditEvent {
            profile: cx.profile().clone(),
            principal: cx.principal().clone(),
            request: cx.request().clone(),
            kind: "denied".into(),
            payload,
        })
    }
    fn capacity_limit(&self, cx: &CallCx) -> Result<u64, GateError> {
        self.check_context(cx)?;
        let effective = self.effective_permissions(cx)?;
        let cap = match cx.principal() {
            Principal::Pack { pack, hash } | Principal::PackUi { pack, hash, .. } => effective
                .wasm
                .memory_mib
                .min(self.frozen_limits(cx, pack, hash)?.memory_mib),
            // effective_permissions already intersects the explicit original
            // User declaration/ceiling and current document/revision/epoch
            // approval. No Pack pair or synthetic hash can supply User funds.
            Principal::User => {
                if effective.wasm.memory_mib == 0 || cx.limits().memory_mib == 0 {
                    return Err(GateError::Unavailable);
                }
                effective.wasm.memory_mib
            }
            Principal::Input { .. } => return Err(GateError::Unavailable),
        };
        cap.min(cx.limits().memory_mib)
            .checked_mul(1024 * 1024)
            .filter(|limit| *limit > 0)
            .ok_or_else(|| GateError::Limit("invalid approved memory capacity".into()))
    }
    fn input_capacity_limit(
        &self,
        cx: &CallCx,
        view: &ShapeRef,
        op: &OpName,
        target: &FrozenPortTarget,
    ) -> Result<u64, GateError> {
        self.check_context(cx)?;
        let Principal::Input { .. } = cx.principal() else {
            return Err(GateError::Denied(
                "native Input funding requires original Input principal".into(),
            ));
        };
        let revision = cx
            .revision()
            .ok_or_else(|| GateError::Denied("Input funding lacks frozen revision".into()))?;
        self.with_registered_input_source(cx, view, op, |binding| {
            if &binding.target != target
                || &binding.entry_view != view
                || &binding.entry_op != op
                || !self.frozen_target_registered(
                    cx.profile(),
                    revision,
                    &binding.entry_view,
                    target,
                )?
            {
                return Err(GateError::Denied("Input funding target changed".into()));
            }
            let cap = self
                .code_owner_memory_limit(
                    cx,
                    PreparedCodeOwner::Pack {
                        pack: &binding.pack,
                        hash: &binding.hash,
                    },
                )?
                .min(
                    binding
                        .projection
                        .wasm
                        .memory_mib
                        .checked_mul(1024 * 1024)
                        .ok_or(GateError::Unavailable)?,
                );
            Some(cap)
                .filter(|cap| *cap > 0)
                .ok_or(GateError::Unavailable)
        })
    }
    fn frame_capacity_limit(
        &self,
        cx: &CallCx,
        scope: &AllocationFrameScope,
    ) -> Result<u64, GateError> {
        self.frame_capacity_limit_inner(cx, scope)
    }
    fn with_frame_allocation(
        &self,
        cx: &CallCx,
        scope: &AllocationFrameScope,
        admit: &mut dyn FnMut(u64) -> Result<(), GateError>,
    ) -> Result<(), GateError> {
        self.with_frame_allocation_inner(cx, scope, admit)
    }
    fn with_frame_preparation(
        &self,
        cx: &CallCx,
        prepared: &dyn PreparedFrameVerifier,
        reason: AllocationReason,
        admit: &mut dyn FnMut(PreparedFrameFunding<'_>) -> Result<(), GateError>,
    ) -> Result<(), GateError> {
        self.with_frame_preparation_inner(cx, prepared, reason, admit)
    }
    fn cancel_due_leases(&self, profile: &ProfileId) -> Result<u64, GateError> {
        let now = std::time::Instant::now();
        let mut count = 0;
        // watch_lease admits at most 4096 hooks. Retain one existing funded
        // node at a time without allocating or removing its original hook.
        for _ in 0..4096 {
            let token = {
                let leases = self.lease_cancellations.lock().map_err(|_| {
                    GateError::Persistence("lease cancellation lock poisoned".into())
                })?;
                leases.values().find_map(|(owner, token, deadline)| {
                    (owner == profile && now >= *deadline && !token.is_cancelled())
                        .then(|| token.clone())
                })
            };
            let Some(token) = token else {
                break;
            };
            // Tokio can synchronously wake and drop user Wakers. Both the
            // notification and this owning retain's Drop precede the next
            // lock acquisition, so those callbacks may reenter this Gate.
            token.notify_deadline();
            count += 1;
            drop(token);
        }
        // Keep every hook until its durable terminal decision exists. Repeated
        // ticks never renew its deadline or imply successful physical cleanup.
        Ok(count)
    }
    fn watch_lease(&self, cx: &CallCx, permit: &DispatchPermit) -> Result<(), GateError> {
        cx.check_active()?;
        if !permit.matches_context(cx) {
            return Err(GateError::Denied("lease context mismatch".into()));
        }
        if self.epoch(cx.profile())? != cx.security_epoch() {
            return Err(GateError::StaleEpoch);
        }
        {
            let mut leases = self
                .lease_cancellations
                .lock()
                .map_err(|_| GateError::Persistence("lease cancellation lock poisoned".into()))?;
            if leases.len() >= 4096 && !leases.contains_key(permit.id()) {
                return Err(GateError::Limit("supervised lease capacity".into()));
            }
            leases.entry(permit.id().clone()).or_insert_with(|| {
                (
                    cx.profile().clone(),
                    cx.cancel().clone(),
                    cx.deadline().min(permit.deadline()),
                )
            });
        }
        if let Some(terminal) = self.approvals.terminal_delivery(permit.id())? {
            let token = {
                let mut leases = self.lease_cancellations.lock().map_err(|_| {
                    GateError::Persistence("lease cancellation lock poisoned".into())
                })?;
                leases.remove(permit.id())
            };
            if matches!(
                terminal.decision.disposition,
                TerminalDisposition::ExpiredCleanupUnknown | TerminalDisposition::ExpiredBeforeUse
            ) && let Some((_, token, _)) = token
            {
                token.notify_deadline();
            }
            return Err(GateError::Consumed);
        }
        Ok(())
    }
    async fn record_outcome(&self, grant: GrantId, outcome: Outcome) -> Result<(), GateError> {
        let terminal = self.approvals.finish_terminal(&grant, &outcome)?;
        let removed = {
            let mut leases = self
                .lease_cancellations
                .lock()
                .map_err(|_| GateError::Persistence("lease cancellation lock poisoned".into()))?;
            leases.remove(&grant)
        };
        drop(removed);
        self.deliver_terminal(&terminal)?;
        if terminal.decision.outcome != outcome {
            return Err(GateError::Deadline);
        }
        Ok(())
    }
    async fn settle_deadline(
        &self,
        permit: &DispatchPermit,
    ) -> Result<TerminalDelivery, GateError> {
        // The sealed consumed permit supplies authority; a caller-supplied ID or
        // deadline reason alone can never choose this terminal family.
        // A backwards wall-clock adjustment must not extend the original live
        // Instant deadline. Persist the trusted due bound before settlement.
        let reservation = if std::time::Instant::now() >= permit.deadline {
            match self
                .approvals
                .narrow_consumed(&permit.reservation, unix_now_ms()?)
            {
                Ok(reservation) => reservation,
                Err(GateError::Consumed) => permit.reservation.clone(),
                Err(error) => return Err(error),
            }
        } else {
            permit.reservation.clone()
        };
        let terminal = self.approvals.settle_deadline(&reservation)?;
        let removed = {
            let mut leases = self
                .lease_cancellations
                .lock()
                .map_err(|_| GateError::Persistence("lease cancellation lock poisoned".into()))?;
            leases.remove(permit.id())
        };
        if let Some((_, token, _)) = removed
            && matches!(
                terminal.decision.disposition,
                TerminalDisposition::ExpiredBeforeUse | TerminalDisposition::ExpiredCleanupUnknown
            )
        {
            token.notify_deadline();
        }
        self.deliver_terminal(&terminal)?;
        Ok(TerminalDelivery {
            acknowledged: true,
            ..terminal
        })
    }
    async fn narrow_lease(
        &self,
        permit: &mut DispatchPermit,
        cx: &CallCx,
    ) -> Result<(), GateError> {
        if !permit.matches_context(cx) {
            return Err(GateError::Denied("lease context mismatch".into()));
        }
        let limits = permit.limits.intersect(cx.limits());
        let deadline = permit.deadline.min(cx.deadline_for_limits(&limits));
        let reservation = self
            .approvals
            .narrow_consumed(&permit.reservation, deadline_unix_ms(deadline)?)?;
        let deadline = instant_expiry_bound(reservation.expires_unix_ms, deadline)?;
        permit.reservation = reservation;
        permit.limits = limits;
        permit.deadline = deadline;
        if let Some((profile, _, bound)) = self
            .lease_cancellations
            .lock()
            .map_err(|_| GateError::Persistence("lease cancellation lock poisoned".into()))?
            .get_mut(permit.id())
        {
            if *profile != *cx.profile() {
                return Err(GateError::Denied(
                    "lease supervision context mismatch".into(),
                ));
            }
            *bound = (*bound).min(deadline);
        }
        Ok(())
    }
}

#[cfg(test)]
mod cleanup_snapshot_attempt_tests {
    use super::*;
    use std::time::{Duration, Instant};

    fn authority_snapshot(
        reservation: &Reservation,
        now_unix_ms: u64,
    ) -> CleanupAuthoritySnapshot<'_> {
        CleanupAuthoritySnapshot {
            current_epoch: 0,
            reservation,
            now_unix_ms,
            terminal: None,
        }
    }

    #[derive(Clone, Copy)]
    enum Fault {
        None,
        DoubleCallback,
        IgnoreCallbackError,
        BusyAfterCallback,
        BusyAfterInvalidBinding,
        SelectedWithoutCallback,
        BusyBeforeCallback,
        UnavailableBeforeCallback,
        PersistenceBeforeCallback,
        PersistenceAfterCallback,
    }

    #[derive(Clone, Copy, PartialEq, Eq)]
    enum Phase {
        Reserved,
        Dispatched,
        Consumed,
    }

    struct NativeRow {
        reservation: Reservation,
        phase: Phase,
    }

    #[derive(Default)]
    struct NativeState {
        sequence: u64,
        row: Option<NativeRow>,
    }

    // Only snapshot delivery is fault-injected. The reservation is created
    // from Gate's fresh request, dispatched, and consumed by the real Grant.
    struct NativeStore {
        state: Mutex<NativeState>,
        fault: Fault,
        permissions: PermissionSet,
    }

    impl NativeStore {
        fn reserve_fresh(&self, request: &ReservationRequest) -> Result<Reservation, GateError> {
            let mut state = self.state.lock().unwrap();
            if state.row.is_some() {
                return Err(GateError::Consumed);
            }
            state.sequence += 1;
            let reservation = request.bind(
                Digest::hash(b"cleanup-snapshot-test-authority"),
                1,
                state.sequence,
            )?;
            state.row = Some(NativeRow {
                reservation: reservation.clone(),
                phase: Phase::Reserved,
            });
            Ok(reservation)
        }

        fn dispatch(&self, id: &GrantId) -> Result<(), GateError> {
            let mut state = self.state.lock().unwrap();
            let row = state.row.as_mut().ok_or(GateError::Unavailable)?;
            if row.reservation.id != *id || row.phase != Phase::Reserved {
                return Err(GateError::Consumed);
            }
            row.phase = Phase::Dispatched;
            Ok(())
        }

        fn consume_with_deadline(
            &self,
            expected: &Reservation,
            expires: u64,
        ) -> Result<Reservation, GateError> {
            let mut state = self.state.lock().unwrap();
            let row = state.row.as_mut().ok_or(GateError::Unavailable)?;
            if row.phase != Phase::Dispatched
                || strict_bytes(&row.reservation)? != strict_bytes(expected)?
            {
                return Err(GateError::Consumed);
            }
            row.reservation.expires_unix_ms = row.reservation.expires_unix_ms.min(expires);
            row.phase = Phase::Consumed;
            Ok(row.reservation.clone())
        }

        fn snapshot(
            &self,
            expected: &Reservation,
            inspect: &mut dyn FnMut(CleanupAuthoritySnapshot<'_>) -> Result<(), GateError>,
        ) -> CleanupSnapshotAttempt {
            let state = self.state.lock().unwrap();
            let row = state.row.as_ref().unwrap();
            assert!(row.phase == Phase::Consumed);
            assert_eq!(
                strict_bytes(&row.reservation).unwrap(),
                strict_bytes(expected).unwrap()
            );
            let now_unix_ms = unix_now_ms().unwrap();
            match self.fault {
                Fault::None => match inspect(authority_snapshot(&row.reservation, now_unix_ms)) {
                    Ok(()) => CleanupSnapshotAttempt::Selected,
                    Err(error) => CleanupSnapshotAttempt::Uncertain(error),
                },
                Fault::DoubleCallback => {
                    let _ = inspect(authority_snapshot(&row.reservation, now_unix_ms));
                    let _ = inspect(authority_snapshot(&row.reservation, now_unix_ms));
                    CleanupSnapshotAttempt::Selected
                }
                Fault::IgnoreCallbackError => {
                    let _ = inspect(authority_snapshot(&row.reservation, now_unix_ms));
                    CleanupSnapshotAttempt::Selected
                }
                Fault::BusyAfterCallback => {
                    let _ = inspect(authority_snapshot(&row.reservation, now_unix_ms));
                    CleanupSnapshotAttempt::RetryableBusyBeforeCallback
                }
                Fault::BusyAfterInvalidBinding => {
                    let mut wrong_binding = row.reservation.clone();
                    wrong_binding.args = Digest::hash(b"different-arguments");
                    let _ = inspect(authority_snapshot(&wrong_binding, now_unix_ms));
                    CleanupSnapshotAttempt::RetryableBusyBeforeCallback
                }
                Fault::SelectedWithoutCallback => CleanupSnapshotAttempt::Selected,
                Fault::BusyBeforeCallback => CleanupSnapshotAttempt::RetryableBusyBeforeCallback,
                Fault::UnavailableBeforeCallback => {
                    CleanupSnapshotAttempt::Uncertain(GateError::Unavailable)
                }
                Fault::PersistenceBeforeCallback => CleanupSnapshotAttempt::Uncertain(
                    GateError::Persistence("snapshot acquisition failed".into()),
                ),
                Fault::PersistenceAfterCallback => {
                    let _ = inspect(authority_snapshot(&row.reservation, now_unix_ms));
                    CleanupSnapshotAttempt::Uncertain(GateError::Persistence(
                        "snapshot teardown failed".into(),
                    ))
                }
            }
        }
    }

    struct LegacyAdapter {
        native: Arc<NativeStore>,
    }

    struct TypedAdapter {
        native: Arc<NativeStore>,
    }

    // Both adapters use the same honest native reservation transitions. The
    // legacy adapter deliberately uses ApprovalStore's actual default typed
    // bridge; only TypedAdapter can report a native pre-callback busy result.
    macro_rules! impl_snapshot_adapter {
        ($adapter:ty $(, $typed:item)?) => {
            impl ApprovalStore for $adapter {
                fn admission_status(&self, _: &ProfileId) -> Result<AdmissionStatus, GateError> {
                    Ok(AdmissionStatus::Allowed)
                }
                fn latch_integrity(
                    &self,
                    _: &ProfileId,
                    _: IntegrityFaultCode,
                ) -> Result<(), GateError> {
                    Err(GateError::Unavailable)
                }
                fn security_epoch(&self, _: &ProfileId) -> Result<u64, GateError> {
                    Ok(0)
                }
                fn advance_security_epoch(&self, _: &ProfileId) -> Result<u64, GateError> {
                    Err(GateError::Unavailable)
                }
                fn permissions(
                    &self,
                    _: &ProfileId,
                    _: &PackId,
                    _: &PackHash,
                ) -> Result<Option<PermissionSet>, GateError> {
                    Ok(Some(self.native.permissions.clone()))
                }
                fn set_permissions(
                    &self,
                    _: &ProfileId,
                    _: &PackId,
                    _: &PackHash,
                    _: &PermissionSet,
                ) -> Result<(), GateError> {
                    Err(GateError::Unavailable)
                }
                fn user_permissions(
                    &self,
                    _: &ProfileId,
                    _: &Digest,
                    _: &Digest,
                    _: u64,
                ) -> Result<Option<PermissionSet>, GateError> {
                    Err(GateError::Unavailable)
                }
                fn set_user_permissions(
                    &self,
                    _: &ProfileId,
                    _: &Digest,
                    _: &Digest,
                    _: u64,
                    _: &PermissionSet,
                ) -> Result<(), GateError> {
                    Err(GateError::Unavailable)
                }
                fn reserve(&self, _: &Reservation) -> Result<(), GateError> {
                    Err(GateError::Unavailable)
                }
                fn reserve_fresh(
                    &self,
                    request: &ReservationRequest,
                ) -> Result<Reservation, GateError> {
                    self.native.reserve_fresh(request)
                }
                fn dispatch(&self, id: &GrantId) -> Result<(), GateError> {
                    self.native.dispatch(id)
                }
                fn dispatch_public_write(
                    &self,
                    _: &Reservation,
                    _: &PublicWriteApproval,
                ) -> Result<(), GateError> {
                    Err(GateError::Unavailable)
                }
                fn abandon(&self, _: &GrantId) -> Result<Option<Reservation>, GateError> {
                    Err(GateError::Unavailable)
                }
                fn sweep_expired(&self, _: &ProfileId, _: u64) -> Result<u64, GateError> {
                    Err(GateError::Unavailable)
                }
                fn consume(&self, _: &Reservation) -> Result<(), GateError> {
                    Err(GateError::Unavailable)
                }
                fn consume_with_deadline(
                    &self,
                    reservation: &Reservation,
                    expires: u64,
                ) -> Result<Reservation, GateError> {
                    self.native.consume_with_deadline(reservation, expires)
                }
                fn with_cleanup_snapshot(
                    &self,
                    expected: &Reservation,
                    inspect: &mut dyn FnMut(CleanupAuthoritySnapshot<'_>) -> Result<(), GateError>,
                ) -> Result<(), GateError> {
                    match self.native.snapshot(expected, inspect) {
                        CleanupSnapshotAttempt::Selected => Ok(()),
                        CleanupSnapshotAttempt::RetryableBusyBeforeCallback => {
                            Err(GateError::Unavailable)
                        }
                        CleanupSnapshotAttempt::Uncertain(error) => Err(error),
                    }
                }
                $($typed)?
                fn finish(&self, _: &GrantId, _: &Outcome) -> Result<Reservation, GateError> {
                    Err(GateError::Unavailable)
                }
                fn terminal_delivery(
                    &self,
                    _: &GrantId,
                ) -> Result<Option<TerminalDelivery>, GateError> {
                    Ok(None)
                }
                fn finish_terminal(
                    &self,
                    _: &GrantId,
                    _: &Outcome,
                ) -> Result<TerminalDelivery, GateError> {
                    Err(GateError::Unavailable)
                }
                fn abandon_terminal(
                    &self,
                    _: &GrantId,
                ) -> Result<Option<TerminalDelivery>, GateError> {
                    Err(GateError::Unavailable)
                }
                fn expire_terminal(
                    &self,
                    _: &ProfileId,
                    _: u64,
                ) -> Result<Vec<TerminalDecision>, GateError> {
                    Ok(Vec::new())
                }
                fn pending_terminal(
                    &self,
                    _: &ProfileId,
                    _: u32,
                ) -> Result<Vec<TerminalDecision>, GateError> {
                    Ok(Vec::new())
                }
                fn terminal_watermark(&self, _: &ProfileId) -> Result<u64, GateError> {
                    Ok(0)
                }
                fn pending_terminal_through(
                    &self,
                    _: &ProfileId,
                    _: u32,
                    _: u64,
                ) -> Result<Vec<TerminalDecision>, GateError> {
                    Ok(Vec::new())
                }
                fn acknowledge_terminal(&self, _: &Digest, _: &AuditEvent) -> Result<(), GateError> {
                    Err(GateError::Unavailable)
                }
                fn public_write_approved(
                    &self,
                    _: &ProfileId,
                    _: &PackId,
                    _: &PackId,
                    _: &str,
                ) -> Result<bool, GateError> {
                    Ok(false)
                }
                fn approve_public_write(
                    &self,
                    _: &ProfileId,
                    _: &PackId,
                    _: &PackId,
                    _: &str,
                ) -> Result<(), GateError> {
                    Err(GateError::Unavailable)
                }
                fn paired_device_key(
                    &self,
                    _: &ProfileId,
                    _: &str,
                ) -> Result<Option<[u8; 32]>, GateError> {
                    Ok(None)
                }
                fn claim_attestation(
                    &self,
                    _: &ProfileId,
                    _: &str,
                    _: &str,
                    _: u64,
                ) -> Result<bool, GateError> {
                    Err(GateError::Unavailable)
                }
            }
        };
    }

    impl_snapshot_adapter!(LegacyAdapter);
    impl_snapshot_adapter!(
        TypedAdapter,
        fn try_cleanup_snapshot(
            &self,
            expected: &Reservation,
            inspect: &mut dyn FnMut(CleanupAuthoritySnapshot<'_>) -> Result<(), GateError>,
        ) -> CleanupSnapshotAttempt {
            self.native.snapshot(expected, inspect)
        }
    );

    #[derive(Default)]
    struct NativeAudit {
        dispatches: Mutex<Vec<AuditEvent>>,
    }

    impl AuditSink for NativeAudit {
        fn append(&self, event: AuditEvent) -> Result<(), GateError> {
            self.dispatches.lock().unwrap().push(event);
            Ok(())
        }
    }

    struct NoPrompt;

    #[async_trait]
    impl TrustedPrompt for NoPrompt {
        async fn prompt_approval(&self, _: ApprovalPrompt) -> ApprovalAnswer {
            panic!("native Clock dispatch must not require an interactive approval")
        }
    }

    #[test]
    fn exact_flow_reregistration_shares_a_full_catalog_but_keeps_handle_limits() {
        let control = CoreControl::new(crate::CoreControlLimits {
            max_catalogs: 1,
            max_registrations: 2,
            ..crate::CoreControlLimits::default()
        })
        .unwrap();
        let gate = GateEngine::new_with_control(
            Arc::new(TypedAdapter {
                native: Arc::new(NativeStore {
                    state: Mutex::new(NativeState::default()),
                    fault: Fault::None,
                    permissions: PermissionSet::default(),
                }),
            }),
            Arc::new(NativeAudit::default()),
            Arc::new(NoPrompt),
            MediatorChain::default(),
            control.clone(),
        );
        let target = crate::FrozenFlowTarget {
            profile: ProfileId::parse("profile_flow_catalog").unwrap(),
            revision: Digest::hash(b"revision"),
            owner: PackId::parse("local.owner").unwrap(),
            owner_hash: PackHash::new(Digest::hash(b"pack")),
            flow_key: "local.owner/workflow".into(),
            flow_ast: Digest::hash(b"flow"),
            plan_digest: Digest::hash(b"plan"),
            implements: ShapeRef::parse("local.owner.workflow@1.0").unwrap(),
            input_view: ShapeRef::parse("local.owner.input@1.0").unwrap(),
            entry_signature: Digest::hash(b"signature"),
        };
        let register_revision = |revision: &Digest| {
            gate.register_revision(
                &target.profile,
                revision,
                BTreeMap::from([(
                    (target.owner.clone(), target.owner_hash.clone()),
                    PermissionSet::default().wasm,
                )]),
            )
            .unwrap();
            gate.register_operation_views(
                &target.profile,
                revision,
                BTreeMap::from([
                    (target.implements.clone(), BTreeSet::new()),
                    (target.input_view.clone(), BTreeSet::new()),
                ]),
            )
            .unwrap();
        };
        register_revision(&target.revision);
        let register = |target: &crate::FrozenFlowTarget| {
            gate.register_flow_endpoints(&target.profile, &target.revision, vec![target.clone()])
        };
        let first = register(&target).unwrap();
        let original_bytes = control.usage().bytes;
        let mut changed = target.clone();
        changed.flow_ast = Digest::hash(b"different-flow");
        assert!(matches!(register(&changed), Err(GateError::Denied(_))));
        assert_eq!(control.usage().bytes, original_bytes);

        let second = register(&target).unwrap();
        assert!(std::ptr::eq(first.endpoints(), second.endpoints()));
        assert_eq!(control.usage().count(ControlKind::Catalog), 1);
        assert_eq!(control.usage().count(ControlKind::Registration), 2);
        assert!(matches!(register(&target), Err(GateError::Limit(_))));
        drop(first);
        assert!(
            gate.frozen_flow_target_registered(&target.profile, &target.revision, &target)
                .unwrap()
        );

        let mut other_revision = target.clone();
        other_revision.revision = Digest::hash(b"other-revision");
        register_revision(&other_revision.revision);
        assert!(matches!(
            register(&other_revision),
            Err(GateError::Limit(_))
        ));
        assert_eq!(control.usage().bytes, original_bytes);
        drop(second);
        assert!(
            !gate
                .frozen_flow_target_registered(&target.profile, &target.revision, &target)
                .unwrap()
        );
        assert_eq!(control.usage().bytes, 0);
        assert_eq!(control.usage().count(ControlKind::Catalog), 0);
        assert_eq!(control.usage().count(ControlKind::Registration), 0);
        drop(register(&other_revision).unwrap());
        assert_eq!(control.usage().bytes, 0);
    }

    async fn consumed_permit(fault: Fault, typed: bool) -> (GateEngine, DispatchPermit) {
        let control = CoreControl::default();
        let limits = ResourceLimits {
            fuel_per_call: 1_000,
            memory_mib: 1,
            wall_ms: 60_000,
            table_elements: 16,
            output_bytes: 1_024,
        };
        let permissions = PermissionSet {
            wasm: limits.clone(),
            ..PermissionSet::default()
        };
        let native = Arc::new(NativeStore {
            state: Mutex::new(NativeState::default()),
            fault,
            permissions: permissions.clone(),
        });
        let approvals: Arc<dyn ApprovalStore> = if typed {
            Arc::new(TypedAdapter {
                native: native.clone(),
            })
        } else {
            Arc::new(LegacyAdapter {
                native: native.clone(),
            })
        };
        let audit = Arc::new(NativeAudit::default());
        let gate = GateEngine::new_with_control(
            approvals,
            audit.clone(),
            Arc::new(NoPrompt),
            MediatorChain::default(),
            control.clone(),
        );
        let profile = ProfileId::new("cleanup-snapshot-tests").unwrap();
        let pack = PackId::new("fixture.cleanup").unwrap();
        let hash = PackHash::new(Digest::hash(b"cleanup-snapshot-test-pack"));
        gate.register_pack(
            &profile,
            &pack,
            &hash,
            PackPolicy {
                declaration: permissions.clone(),
                ceiling: permissions.clone(),
                pack_limits: permissions,
            },
        )
        .unwrap();
        let cx = CallCx::try_root_with_control(
            profile,
            Principal::Pack { pack, hash },
            RequestId::new("cleanup-snapshot-request").unwrap(),
            CancellationToken::new_with_control(&control).unwrap(),
            Instant::now() + Duration::from_secs(60),
            limits,
            control,
        )
        .unwrap();
        let request = EffectRequest::Clock;
        let args = Digest::hash(b"cleanup-snapshot-clock-arguments");
        let grant = gate
            .authorize(&cx, request.clone(), args.clone())
            .await
            .unwrap();
        let permit = grant.consume(&cx, &request, &args).unwrap();
        assert!(permit.matches_context(&cx));
        assert!(permit.reservation.authority.is_some());
        assert!(permit.reservation.original_expires_unix_ms.is_some());
        assert!(native.state.lock().unwrap().row.as_ref().unwrap().phase == Phase::Consumed);
        let events = audit.dispatches.lock().unwrap();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].kind, "dispatch");
        drop(events);
        (gate, permit)
    }

    fn assert_unavailable(attempt: CleanupSnapshotAttempt) {
        assert!(matches!(
            attempt,
            CleanupSnapshotAttempt::Uncertain(GateError::Unavailable)
        ));
    }

    #[tokio::test]
    async fn typed_selected_requires_one_successful_exact_callback() {
        for typed in [false, true] {
            let (gate, permit) = consumed_permit(Fault::None, typed).await;
            let mut calls = 0;
            let attempt = gate.try_cleanup_snapshot(&permit, &mut |snapshot| {
                calls += 1;
                assert_eq!(snapshot.authority.current_epoch, 0);
                assert_eq!(
                    strict_bytes(snapshot.authority.reservation).unwrap(),
                    strict_bytes(&permit.reservation).unwrap()
                );
                assert!(snapshot.authority.terminal.is_none());
                assert_eq!(snapshot.limits, permit.limits());
                assert_eq!(snapshot.original_deadline, permit.deadline());
                assert!(snapshot.observed_at <= Instant::now());
                Ok(())
            });
            assert!(matches!(attempt, CleanupSnapshotAttempt::Selected));
            assert_eq!(calls, 1);
        }
    }

    #[tokio::test]
    async fn ignored_second_callback_cannot_report_selected() {
        for typed in [false, true] {
            let (gate, permit) = consumed_permit(Fault::DoubleCallback, typed).await;
            let mut calls = 0;
            let attempt = gate.try_cleanup_snapshot(&permit, &mut |_| {
                calls += 1;
                Ok(())
            });
            assert_unavailable(attempt);
            assert_eq!(calls, 1);
        }
    }

    #[tokio::test]
    async fn ignored_callback_error_cannot_report_selected() {
        for typed in [false, true] {
            let (gate, permit) = consumed_permit(Fault::IgnoreCallbackError, typed).await;
            let mut calls = 0;
            let attempt = gate.try_cleanup_snapshot(&permit, &mut |_| {
                calls += 1;
                Err(GateError::Cancelled)
            });
            assert_unavailable(attempt);
            assert_eq!(calls, 1);
        }
    }

    #[tokio::test]
    async fn typed_busy_is_valid_only_before_any_callback_entry() {
        for (fault, expected_calls) in [
            (Fault::BusyAfterCallback, 1),
            (Fault::BusyAfterInvalidBinding, 0),
        ] {
            let (gate, permit) = consumed_permit(fault, true).await;
            let mut calls = 0;
            let attempt = gate.try_cleanup_snapshot(&permit, &mut |_| {
                calls += 1;
                Ok(())
            });
            assert_unavailable(attempt);
            assert_eq!(calls, expected_calls);
        }
        let (gate, permit) = consumed_permit(Fault::BusyBeforeCallback, true).await;
        let mut calls = 0;
        let attempt = gate.try_cleanup_snapshot(&permit, &mut |_| {
            calls += 1;
            Ok(())
        });
        assert!(matches!(
            attempt,
            CleanupSnapshotAttempt::RetryableBusyBeforeCallback
        ));
        assert_eq!(calls, 0);
    }

    #[tokio::test]
    async fn selected_without_callback_is_uncertain() {
        for typed in [false, true] {
            let (gate, permit) = consumed_permit(Fault::SelectedWithoutCallback, typed).await;
            let mut calls = 0;
            let attempt = gate.try_cleanup_snapshot(&permit, &mut |_| {
                calls += 1;
                Ok(())
            });
            assert_unavailable(attempt);
            assert_eq!(calls, 0);
        }
    }

    #[tokio::test]
    async fn default_legacy_errors_never_become_busy() {
        for fault in [
            Fault::UnavailableBeforeCallback,
            Fault::PersistenceBeforeCallback,
        ] {
            let (gate, permit) = consumed_permit(fault, false).await;
            let mut calls = 0;
            let attempt = gate.try_cleanup_snapshot(&permit, &mut |_| {
                calls += 1;
                Ok(())
            });
            match fault {
                Fault::UnavailableBeforeCallback => assert_unavailable(attempt),
                Fault::PersistenceBeforeCallback => assert!(matches!(
                    attempt,
                    CleanupSnapshotAttempt::Uncertain(GateError::Persistence(message))
                        if message == "snapshot acquisition failed"
                )),
                _ => unreachable!(),
            }
            assert_eq!(calls, 0);
        }
    }

    #[tokio::test]
    async fn typed_callback_and_teardown_errors_preserve_outer_uncertainty() {
        for typed in [false, true] {
            let (gate, permit) = consumed_permit(Fault::None, typed).await;
            let mut calls = 0;
            let attempt = gate.try_cleanup_snapshot(&permit, &mut |_| {
                calls += 1;
                Err(GateError::Cancelled)
            });
            assert!(matches!(
                attempt,
                CleanupSnapshotAttempt::Uncertain(GateError::Cancelled)
            ));
            assert_eq!(calls, 1);

            let (gate, permit) = consumed_permit(Fault::PersistenceAfterCallback, typed).await;
            let mut calls = 0;
            let attempt = gate.try_cleanup_snapshot(&permit, &mut |_| {
                calls += 1;
                Err(GateError::Cancelled)
            });
            assert!(matches!(
                attempt,
                CleanupSnapshotAttempt::Uncertain(GateError::Persistence(message))
                    if message == "snapshot teardown failed"
            ));
            assert_eq!(calls, 1);
        }
    }

    #[tokio::test]
    async fn legacy_api_preserves_propagated_callback_and_teardown_errors() {
        let (gate, permit) = consumed_permit(Fault::None, false).await;
        let error = gate
            .with_cleanup_snapshot(&permit, &mut |_| Err(GateError::Cancelled))
            .unwrap_err();
        assert!(matches!(error, GateError::Cancelled));

        let (gate, permit) = consumed_permit(Fault::PersistenceAfterCallback, false).await;
        let mut calls = 0;
        let error = gate
            .with_cleanup_snapshot(&permit, &mut |_| {
                calls += 1;
                Ok(())
            })
            .unwrap_err();
        assert!(matches!(
            error,
            GateError::Persistence(message) if message == "snapshot teardown failed"
        ));
        assert_eq!(calls, 1);
    }

    #[tokio::test]
    async fn legacy_api_rejects_ignored_callback_failures_and_missing_callback() {
        for fault in [
            Fault::DoubleCallback,
            Fault::IgnoreCallbackError,
            Fault::SelectedWithoutCallback,
        ] {
            let (gate, permit) = consumed_permit(fault, false).await;
            let mut calls = 0;
            let result = gate.with_cleanup_snapshot(&permit, &mut |_| {
                calls += 1;
                if matches!(fault, Fault::IgnoreCallbackError) {
                    Err(GateError::Cancelled)
                } else {
                    Ok(())
                }
            });
            assert!(matches!(result, Err(GateError::Unavailable)));
            assert_eq!(
                calls,
                usize::from(!matches!(fault, Fault::SelectedWithoutCallback))
            );
        }
    }

    mod lease_cancellation_tests {
        use super::*;
        use std::{
            future::Future,
            sync::{
                Weak,
                atomic::{AtomicUsize, Ordering},
            },
            task::{Context, Poll, Wake, Waker},
        };

        #[derive(Default)]
        struct Observation {
            wakes: AtomicUsize,
            drops: AtomicUsize,
            reentered: AtomicUsize,
            blocked: AtomicUsize,
        }

        struct ReentrantWake {
            gate: Weak<GateEngine>,
            profile: ProfileId,
            observation: Arc<Observation>,
        }

        impl ReentrantWake {
            fn reenter(&self) {
                let Some(gate) = self.gate.upgrade() else {
                    self.observation.blocked.fetch_add(1, Ordering::AcqRel);
                    return;
                };
                // A try-lock makes the old under-lock implementation fail an
                // assertion instead of hanging the entire test process.
                let unlocked = match gate.lease_cancellations.try_lock() {
                    Ok(guard) => {
                        drop(guard);
                        true
                    }
                    Err(_) => false,
                };
                if unlocked && matches!(gate.cancel_due_leases(&self.profile), Ok(0)) {
                    self.observation.reentered.fetch_add(1, Ordering::AcqRel);
                } else {
                    self.observation.blocked.fetch_add(1, Ordering::AcqRel);
                }
            }
        }

        impl Wake for ReentrantWake {
            fn wake(self: Arc<Self>) {
                self.observation.wakes.fetch_add(1, Ordering::AcqRel);
                self.reenter();
            }

            fn wake_by_ref(self: &Arc<Self>) {
                self.observation.wakes.fetch_add(1, Ordering::AcqRel);
                self.reenter();
            }
        }

        impl Drop for ReentrantWake {
            fn drop(&mut self) {
                self.observation.drops.fetch_add(1, Ordering::AcqRel);
                self.reenter();
            }
        }

        async fn watched_lease() -> (Arc<GateEngine>, CallCx, DispatchPermit) {
            let control = CoreControl::default();
            let limits = ResourceLimits {
                fuel_per_call: 1_000,
                memory_mib: 1,
                wall_ms: 60_000,
                table_elements: 16,
                output_bytes: 1_024,
            };
            let permissions = PermissionSet {
                wasm: limits.clone(),
                ..PermissionSet::default()
            };
            let native = Arc::new(NativeStore {
                state: Mutex::new(NativeState::default()),
                fault: Fault::None,
                permissions: permissions.clone(),
            });
            let gate = Arc::new(GateEngine::new_with_control(
                Arc::new(LegacyAdapter { native }),
                Arc::new(NativeAudit::default()),
                Arc::new(NoPrompt),
                MediatorChain::default(),
                control.clone(),
            ));
            let profile = ProfileId::new("lease-wake-tests").unwrap();
            let pack = PackId::new("fixture.lease-wake").unwrap();
            let hash = PackHash::new(Digest::hash(b"lease-wake-test-pack"));
            gate.register_pack(
                &profile,
                &pack,
                &hash,
                PackPolicy {
                    declaration: permissions.clone(),
                    ceiling: permissions.clone(),
                    pack_limits: permissions,
                },
            )
            .unwrap();
            let cx = CallCx::try_root_with_control(
                profile,
                Principal::Pack { pack, hash },
                RequestId::new("lease-wake-request").unwrap(),
                CancellationToken::new_with_control(&control).unwrap(),
                Instant::now() + Duration::from_secs(60),
                limits,
                control,
            )
            .unwrap();
            let request = EffectRequest::Clock;
            let args = Digest::hash(b"lease-wake-clock-arguments");
            let grant = gate
                .authorize(&cx, request.clone(), args.clone())
                .await
                .unwrap();
            let permit = grant.consume(&cx, &request, &args).unwrap();
            gate.watch_lease(&cx, &permit).unwrap();
            (gate, cx, permit)
        }

        #[tokio::test]
        async fn due_wake_and_last_waker_drop_reenter_without_retiring_original_hook() {
            let (gate, cx, permit) = watched_lease().await;
            assert_eq!(gate.cancel_due_leases(cx.profile()).unwrap(), 0);
            let due = Instant::now();
            {
                let mut leases = gate.lease_cancellations.lock().unwrap();
                let (_, token, deadline) = leases.get_mut(permit.id()).unwrap();
                assert!(token.same_node(cx.cancel()));
                // Only the private wake hook's scheduling instant is made
                // due. The real consumed permit and durable row stay exact;
                // notifying a deadline never selects a terminal disposition.
                *deadline = due;
            }
            assert_eq!(
                gate.cancel_due_leases(&ProfileId::new("another-profile").unwrap())
                    .unwrap(),
                0
            );
            assert!(!cx.cancel().is_cancelled());
            let observation = Arc::new(Observation::default());
            let waker = Waker::from(Arc::new(ReentrantWake {
                gate: Arc::downgrade(&gate),
                profile: cx.profile().clone(),
                observation: observation.clone(),
            }));
            let mut waiter = std::pin::pin!(cx.cancel().cancelled());
            {
                let mut task = Context::from_waker(&waker);
                assert!(waiter.as_mut().poll(&mut task).is_pending());
            }
            // Only the actual registered Tokio waiter retains this Waker.
            // Its final Drop must occur during notification, before repoll.
            drop(waker);
            assert_eq!(observation.drops.load(Ordering::Acquire), 0);
            assert_eq!(gate.cancel_due_leases(cx.profile()).unwrap(), 1);
            assert_eq!(observation.wakes.load(Ordering::Acquire), 1);
            assert_eq!(observation.drops.load(Ordering::Acquire), 1);
            assert_eq!(observation.reentered.load(Ordering::Acquire), 2);
            assert_eq!(observation.blocked.load(Ordering::Acquire), 0);
            assert_eq!(gate.cancel_due_leases(cx.profile()).unwrap(), 0);
            assert_eq!(cx.cancel().cause(), Some(CancellationCause::Deadline));
            {
                let leases = gate.lease_cancellations.lock().unwrap();
                let (owner, token, deadline) = leases.get(permit.id()).unwrap();
                assert_eq!(owner, cx.profile());
                assert!(token.same_node(cx.cancel()));
                assert_eq!(*deadline, due);
                assert_eq!(leases.len(), 1);
            }
            gate.with_cleanup_snapshot(&permit, &mut |snapshot| {
                assert!(snapshot.authority.terminal.is_none());
                assert_eq!(snapshot.authority.reservation.id, *permit.id());
                assert_eq!(
                    snapshot.authority.reservation.expires_unix_ms,
                    permit.reservation.expires_unix_ms
                );
                Ok(())
            })
            .unwrap();
            let noop = Waker::noop();
            let mut task = Context::from_waker(noop);
            assert!(matches!(
                waiter.as_mut().poll(&mut task),
                Poll::Ready(Ok(CancellationCause::Deadline))
            ));
        }
    }
}
