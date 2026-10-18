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
    reservation: Reservation,
    request: EffectRequest,
    limits: ResourceLimits,
    approvals: Arc<dyn ApprovalStore>,
    audit: Arc<dyn AuditSink>,
    consumed: bool,
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
        mut self,
        cx: &CallCx,
        request: &EffectRequest,
        args: &Digest,
    ) -> Result<DispatchPermit, GateError> {
        #[cfg(feature = "closed-request-metrics")]
        let _sync_observation = crate::closed_metrics::enter_sync(cx.observation());
        cx.check_active()?;
        if self.reservation.profile != *cx.profile()
            || self.reservation.principal != *cx.principal()
            || self.reservation.request != *cx.request()
            || self.reservation.revision.as_ref() != cx.revision()
            || self.request != *request
            || self.reservation.effect != request.digest()?
            || self.reservation.args != *args
        {
            return Err(GateError::Denied("grant binding mismatch".into()));
        }
        let epoch = self.approvals.security_epoch(cx.profile())?;
        if epoch != self.reservation.epoch || epoch != cx.security_epoch() {
            return Err(GateError::StaleEpoch);
        }
        let mut deadline = cx.deadline_for_limits(&cx.limits().intersect(&self.limits));
        self.reservation = self
            .approvals
            .consume_with_deadline(&self.reservation, deadline_unix_ms(deadline)?)?;
        deadline = instant_expiry_bound(self.reservation.expires_unix_ms, deadline)?;
        self.consumed = true;
        Ok(DispatchPermit {
            reservation: self.reservation.clone(),
            request: self.request.clone(),
            limits: self.limits.clone(),
            deadline,
        })
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
        if !self.consumed {
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
    reservation: Reservation,
    request: EffectRequest,
    limits: ResourceLimits,
    deadline: std::time::Instant,
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
}
impl ExecutionEntryEvidence {
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
        })
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
        if permit.request.digest()? != permit.reservation.effect
            || permit.reservation.original_expires_unix_ms.is_none()
        {
            return Err(GateError::Denied(
                "cleanup requires an exact consumed permit".into(),
            ));
        }
        // Bounded native scratch for the Store's bounded reservation/terminal
        // records and validation copies. No guest execution allowance is used.
        let _scratch = self
            .control
            .reserve(ControlKind::Frame, CLEANUP_SNAPSHOT_CONTROL_BYTES)?;
        let mut invoked = false;
        let result = self
            .approvals
            .with_cleanup_snapshot(&permit.reservation, &mut |authority| {
                if invoked {
                    return Err(GateError::Unavailable);
                }
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
                    return Err(GateError::Denied(
                        "cleanup snapshot differs from consumed permit".into(),
                    ));
                }
                invoked = true;
                inspect(CleanupSnapshot {
                    authority,
                    limits: permit.limits(),
                    original_deadline: permit.deadline(),
                    observed_at: std::time::Instant::now(),
                })
            });
        match result {
            Ok(()) if !invoked => Err(GateError::Unavailable),
            result => result,
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
        let local = {
            let mut leases = self
                .lease_cancellations
                .lock()
                .map_err(|_| GateError::Persistence("lease cancellation lock poisoned".into()))?;
            for decision in &expired {
                if let Some((_, token, _)) = leases.remove(&decision.reservation.id) {
                    token.notify_deadline();
                }
            }
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
        let mut leases = self
            .lease_cancellations
            .lock()
            .map_err(|_| GateError::Persistence("lease cancellation lock poisoned".into()))?;
        for (id, expired) in completed {
            if leases.get(&id).is_some_and(|(p, _, _)| p == profile) {
                if let Some((_, token, _)) = leases.remove(&id) {
                    if expired {
                        token.notify_deadline();
                    }
                }
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
        #[cfg(feature = "closed-request-metrics")]
        let _sync_observation = crate::closed_metrics::enter_sync(cx.observation());
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
            Ok(policy.maximum().intersect(&approved))
        } else if let Principal::Input { source } = cx.principal() {
            self.inputs
                .lock()
                .map_err(|_| GateError::Persistence("input policy lock poisoned".into()))?
                .get(&(cx.profile().to_string(), source.to_string()))
                .cloned()
                .ok_or_else(|| GateError::Denied("unbound input source".into()))
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
            Ok(policy.maximum.intersect(&approved))
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
        #[cfg(feature = "closed-request-metrics")]
        let _sync_observation = crate::closed_metrics::enter_sync(cx.observation());
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
        if !matches!(cx.principal(), Principal::User) {
            return Err(GateError::Denied(
                "User approval is a trusted core operation".into(),
            ));
        }
        cx.check_active()?;
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
        if additions.has_additions() {
            self.obtain_approval(cx, prompt).await?;
        }
        cx.check_active()?;
        if self.epoch(cx.profile())? != cx.security_epoch() {
            return Err(GateError::StaleEpoch);
        }
        self.event(cx, &reservation, "user-permission-update", None)?;
        self.approvals.dispatch(&reservation.id)?;
        let terminal = self.approvals.commit_permission_effect(
            &reservation,
            deadline_unix_ms(cx.deadline())?,
            PermissionEffect::User {
                profile_hash,
                revision,
                epoch: target_epoch,
                permissions: &narrowed,
            },
        )?;
        self.deliver_terminal(&terminal)?;
        if terminal.decision.outcome != Outcome::Succeeded {
            return Err(GateError::Deadline);
        }
        Ok(())
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
    async fn obtain_approval(&self, cx: &CallCx, prompt: ApprovalPrompt) -> Result<(), GateError> {
        if !cx.interactive_approval() {
            return Err(GateError::ApprovalRequired);
        }
        let cancellation = self
            .prompt
            .cancellation_waiter(cx)
            .map_err(|_| GateError::Unavailable)?;
        #[cfg(feature = "closed-request-metrics")]
        let observation = crate::closed_metrics::span(
            cx.observation(),
            crate::closed_metrics::Metric::ApprovalWait,
        );
        let answer = tokio::select! {result=cancellation=>return Err(cancellation_result(cx,result)),_ = tokio::time::sleep_until(cx.deadline().into())=>return Err(GateError::Deadline),answer=self.prompt.prompt_approval(prompt.clone())=>answer};
        #[cfg(feature = "closed-request-metrics")]
        observation.finish();
        cx.check_active()?;
        if self.epoch(cx.profile())? != cx.security_epoch() {
            return Err(GateError::StaleEpoch);
        }
        match answer {
            ApprovalAnswer::Approve => Ok(()),
            ApprovalAnswer::Deny => Err(GateError::Denied("user declined".into())),
            ApprovalAnswer::Unavailable => Err(GateError::ApprovalRequired),
            ApprovalAnswer::Attestation(att) => self.verify_attestation(&prompt, &att),
        }
    }
    /// Approve only additions and bind retained permissions to the new Pack hash.
    pub async fn approve_pack_permissions(
        &self,
        cx: &CallCx,
        pack: &PackId,
        old_hash: Option<&PackHash>,
        new_hash: &PackHash,
    ) -> Result<(), GateError> {
        if !matches!(cx.principal(), Principal::User) {
            return Err(GateError::Denied(
                "permission installation is a core user operation".into(),
            ));
        }
        cx.check_active()?;
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
        if additions.has_additions() {
            self.obtain_approval(cx, prompt).await?;
        }
        cx.check_active()?;
        if self.epoch(cx.profile())? != cx.security_epoch() {
            return Err(GateError::StaleEpoch);
        }
        self.event(cx, &reservation, "permission-update", None)?;
        self.approvals.dispatch(&reservation.id)?;
        let terminal = self.approvals.commit_permission_effect(
            &reservation,
            deadline_unix_ms(cx.deadline())?,
            PermissionEffect::Pack {
                pack,
                hash: new_hash,
                permissions: &approved,
            },
        )?;
        self.deliver_terminal(&terminal)?;
        if terminal.decision.outcome != Outcome::Succeeded {
            return Err(GateError::Deadline);
        }
        Ok(())
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
        cx: &CallCx,
        res: &Reservation,
        kind: &str,
        outcome: Option<&Outcome>,
    ) -> Result<(), GateError> {
        #[cfg(feature = "closed-request-metrics")]
        let _sync_observation = crate::closed_metrics::enter_sync(cx.observation());
        #[cfg(not(feature = "closed-request-metrics"))]
        let _ = cx;
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
        cx.check_active()?;
        if self.epoch(cx.profile())? != cx.security_epoch() {
            return Err(GateError::StaleEpoch);
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
        let reservation = self.reservation(cx, &request, args)?;
        let mut pending = PendingReservation {
            approvals: self.approvals.as_ref(),
            audit: self.audit.as_ref(),
            id: reservation.id.clone(),
            armed: true,
        };
        let prompt = self.prompt_for(
            cx,
            request.clone(),
            &reservation,
            format!("{} effect ({})", request.kind(), reservation.effect),
        );
        let effective = self.mediate(&prompt, effective, cx).await?;
        if !allowed(&effective) {
            return Err(GateError::Denied(
                "mediator narrowed away the effect".into(),
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
        if first_public_write || per_effect || matches!(cx.principal(), Principal::User) {
            self.obtain_approval(cx, prompt).await?;
        }
        cx.check_active()?;
        if self.epoch(cx.profile())? != cx.security_epoch() {
            return Err(GateError::StaleEpoch);
        }
        self.event(cx, &reservation, "dispatch", None)?;
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
            limits: effective.wasm.intersect(cx.limits()),
            approvals: self.approvals.clone(),
            audit: self.audit.clone(),
            consumed: false,
        })
    }
}

const ACKNOWLEDGED_TERMINAL_CONTROL_BYTES: u64 = 4096;
const CLEANUP_SNAPSHOT_CONTROL_BYTES: u64 = 32 * 1024;

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
        #[cfg(feature = "closed-request-metrics")]
        let _sync_observation = crate::closed_metrics::enter_sync(cx.observation());
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
        let effective = self.effective_permissions(cx)?;
        if let Some((pack, hash)) = cx.principal().pack() {
            let limits = self.frozen_limits(cx, pack, hash)?;
            if limits.fuel_per_call == 0
                || limits.memory_mib == 0
                || limits.wall_ms == 0
                || !limits.is_subset_of(&effective.wasm)
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
    async fn record_denial(
        &self,
        cx: &CallCx,
        kind: &'static str,
        code: &'static str,
    ) -> Result<(), GateError> {
        // No await in this body: the thread-local scope ends in this poll.
        #[cfg(feature = "closed-request-metrics")]
        let _sync_observation = crate::closed_metrics::enter_sync(cx.observation());
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
        let leases = self
            .lease_cancellations
            .lock()
            .map_err(|_| GateError::Persistence("lease cancellation lock poisoned".into()))?;
        let mut count = 0;
        for (owner, token, deadline) in leases.values() {
            if owner == profile && now >= *deadline && !token.is_cancelled() {
                token.notify_deadline();
                count += 1;
            }
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
            let token = self
                .lease_cancellations
                .lock()
                .map_err(|_| GateError::Persistence("lease cancellation lock poisoned".into()))?
                .remove(permit.id());
            if matches!(
                terminal.decision.disposition,
                TerminalDisposition::ExpiredCleanupUnknown | TerminalDisposition::ExpiredBeforeUse
            ) {
                if let Some((_, token, _)) = token {
                    token.notify_deadline();
                }
            }
            return Err(GateError::Consumed);
        }
        Ok(())
    }
    async fn record_outcome(&self, grant: GrantId, outcome: Outcome) -> Result<(), GateError> {
        let terminal = self.approvals.finish_terminal(&grant, &outcome)?;
        self.lease_cancellations
            .lock()
            .map_err(|_| GateError::Persistence("lease cancellation lock poisoned".into()))?
            .remove(&grant);
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
        if let Some((_, token, _)) = self
            .lease_cancellations
            .lock()
            .map_err(|_| GateError::Persistence("lease cancellation lock poisoned".into()))?
            .remove(permit.id())
        {
            if matches!(
                terminal.decision.disposition,
                TerminalDisposition::ExpiredBeforeUse | TerminalDisposition::ExpiredCleanupUnknown
            ) {
                token.notify_deadline();
            }
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
