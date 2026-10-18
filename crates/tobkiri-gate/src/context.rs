use crate::{
    BudgetContext, BudgetError, ControlKind, ControlReservation, CoreCancellation, CoreControl,
    CoreControlError, GateError, ResourceLimits, control::default_control_account,
};
use serde::{Deserialize, Serialize};
use std::time::Instant;
use tobkiri_canon::{
    Digest,
    ids::{InputSourceId, PackHash, PackId, ProfileId, RequestId, UiSessionId},
};
/// Compatibility spelling for the funded Core handle. There is deliberately
/// no infallible `new`, Tokio fallback or uncharged child-token constructor.
pub type CancellationToken = CoreCancellation;

/// The principal is derived by core, never by an argument payload.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "kebab-case", deny_unknown_fields)]
pub enum Principal {
    Pack {
        pack: PackId,
        hash: PackHash,
    },
    PackUi {
        pack: PackId,
        hash: PackHash,
        ui: UiSessionId,
    },
    Input {
        source: InputSourceId,
    },
    User,
}
impl Principal {
    /// Heap storage needed by a fallible retained copy; inline fields belong to
    /// the enclosing prepaid control allocation.
    pub fn clone_storage_bytes(&self) -> Result<usize, tobkiri_canon::AllocationError> {
        use tobkiri_canon::AllocationError;
        match self {
            Self::Pack { pack, .. } => Ok(pack.clone_storage_bytes()),
            Self::PackUi { pack, ui, .. } => pack
                .clone_storage_bytes()
                .checked_add(ui.clone_storage_bytes())
                .ok_or(AllocationError::Overflow),
            Self::Input { source } => Ok(source.clone_storage_bytes()),
            Self::User => Ok(0),
        }
    }
    pub fn try_clone_precharged(&self) -> Result<Self, tobkiri_canon::AllocationError> {
        Ok(match self {
            Self::Pack { pack, hash } => Self::Pack {
                pack: pack.try_clone_precharged()?,
                hash: hash.clone(),
            },
            Self::PackUi { pack, hash, ui } => Self::PackUi {
                pack: pack.try_clone_precharged()?,
                hash: hash.clone(),
                ui: ui.try_clone_precharged()?,
            },
            Self::Input { source } => Self::Input {
                source: source.try_clone_precharged()?,
            },
            Self::User => Self::User,
        })
    }
    pub fn pack(&self) -> Option<(&PackId, &PackHash)> {
        match self {
            Self::Pack { pack, hash } | Self::PackUi { pack, hash, .. } => Some((pack, hash)),
            _ => None,
        }
    }
}

/// Core-owned context. Child calls cannot extend the original deadline or limits.
#[derive(Clone, Debug)]
pub struct CallCx {
    // Context clones share these immutable heap-backed values. Cloning a CallCx
    // is allocation-free and retains the original bounded control reservation.
    profile: std::sync::Arc<ProfileId>,
    principal: std::sync::Arc<Principal>,
    request: std::sync::Arc<RequestId>,
    parent: Option<std::sync::Arc<RequestId>>,
    cancel: CancellationToken,
    deadline: Instant,
    limits: ResourceLimits,
    security_epoch: u64,
    interactive_approval: bool,
    revision: Option<Digest>,
    depth: u16,
    executing_pairs: std::sync::Arc<Vec<(PackId, PackHash)>>,
    budget: Option<std::sync::Arc<BudgetContext>>,
    // Sticky evidence shared by the complete admitted call tree. A successful
    // import does not prove delivery of the eventual export/stream result.
    native_effects: std::sync::Arc<NativeEffectProgress>,
    control: CoreControl,
    _control_reservation: std::sync::Arc<ControlReservation>,
    extra_control_reservations: std::sync::Arc<Vec<std::sync::Arc<ControlReservation>>>,
    #[cfg(feature = "closed-request-metrics")]
    observation: Option<crate::closed_metrics::Observation>,
}

#[derive(Debug)]
struct NativeEffectProgress {
    filesystem_mutation: std::sync::atomic::AtomicBool,
    // Descendants retain the allocation's actual funding after their root
    // context is dropped; this object grants no authority to perform effects.
    _funding: std::sync::Arc<ControlReservation>,
}
impl CallCx {
    /// Legacy constructor backed by the process-wide bounded compatibility
    /// account. Production Core should pass its one account to
    /// `try_root_with_control` and retain it across Gate and Session records.
    pub fn root(
        profile: ProfileId,
        principal: Principal,
        request: RequestId,
        cancel: CancellationToken,
        deadline: Instant,
        limits: ResourceLimits,
    ) -> Self {
        Self::try_root_with_control(
            profile,
            principal,
            request,
            cancel,
            deadline,
            limits,
            default_control_account(),
        )
        .expect("bounded default context capacity exhausted")
    }

    /// Construct a root context after reserving its bounded native control
    /// record. The reservation is separate from guest capacity and grants no
    /// authority.
    pub fn try_root_with_control(
        profile: ProfileId,
        principal: Principal,
        request: RequestId,
        cancel: CancellationToken,
        deadline: Instant,
        limits: ResourceLimits,
        control: CoreControl,
    ) -> Result<Self, GateError> {
        if !cancel.uses_control(&control) {
            return Err(GateError::Denied(
                "context cancellation uses another Core control account".into(),
            ));
        }
        let bytes = root_control_bytes(&profile, &principal, &request)?;
        let reservation = std::sync::Arc::new(control.reserve(ControlKind::Context, bytes)?);
        let native_effects = std::sync::Arc::new(NativeEffectProgress {
            filesystem_mutation: std::sync::atomic::AtomicBool::new(false),
            _funding: reservation.clone(),
        });
        Ok(Self {
            profile: std::sync::Arc::new(profile),
            principal: std::sync::Arc::new(principal),
            request: std::sync::Arc::new(request),
            parent: None,
            cancel,
            deadline,
            limits,
            security_epoch: 0,
            interactive_approval: true,
            revision: None,
            depth: 0,
            executing_pairs: std::sync::Arc::new(Vec::new()),
            budget: None,
            native_effects,
            control,
            _control_reservation: reservation,
            extra_control_reservations: std::sync::Arc::new(Vec::new()),
            #[cfg(feature = "closed-request-metrics")]
            observation: None,
        })
    }

    /// Shared native Core account used for bounded context, waiter, registry
    /// and scheduling metadata.
    pub fn control(&self) -> &CoreControl {
        &self.control
    }
    /// Native opt-in diagnostics only. A descendant cannot swap its request's
    /// bank or borrow control capacity from another Core account.
    #[cfg(feature = "closed-request-metrics")]
    pub fn with_observation(
        mut self,
        observation: &crate::closed_metrics::Observation,
    ) -> Result<Self, GateError> {
        if self.depth != 0 || self.observation.is_some() || !observation.uses_control(&self.control)
        {
            return Err(GateError::Denied(
                "observation account or root mismatch".into(),
            ));
        }
        self.observation = Some(observation.clone());
        Ok(self)
    }
    #[cfg(feature = "closed-request-metrics")]
    pub fn observation(&self) -> Option<&crate::closed_metrics::Observation> {
        self.observation.as_ref()
    }
    /// Trusted native host only: record a consumed filesystem mutation just
    /// before dispatch. This cannot authorize work and can never be cleared.
    /// Ancestors and descendants must preserve uncertain delivery after this
    /// point, even if an individual import previously returned successfully.
    pub fn note_filesystem_mutation(&self) {
        self.native_effects
            .filesystem_mutation
            .store(true, std::sync::atomic::Ordering::Release);
    }
    /// Whether this admitted call tree may have changed physical files.
    /// This is conservative replay evidence, not a success or commit receipt.
    pub fn filesystem_mutation_possible(&self) -> bool {
        self.native_effects
            .filesystem_mutation
            .load(std::sync::atomic::Ordering::Acquire)
    }
    /// Dynamic copies only; the enclosing context/control block and any owned
    /// destination inputs must be reserved separately before construction.
    pub fn clone_storage_bytes(&self) -> Result<usize, tobkiri_canon::AllocationError> {
        let mut bytes = self.base_copy_storage_bytes(0)?;
        for part in [
            self.principal.clone_storage_bytes()?,
            self.request.clone_storage_bytes(),
            self.parent
                .as_ref()
                .map_or(0, |request| request.clone_storage_bytes()),
        ] {
            bytes = bytes
                .checked_add(part)
                .ok_or(tobkiri_canon::AllocationError::Overflow)?;
        }
        Ok(bytes)
    }
    /// Profile, parent request, inherited pair/budget storage. The destination
    /// principal/request and supplied funded cancellation child are moved in.
    pub fn callee_storage_bytes(&self) -> Result<usize, tobkiri_canon::AllocationError> {
        self.base_copy_storage_bytes(0)?
            .checked_add(self.request.clone_storage_bytes())
            .ok_or(tobkiri_canon::AllocationError::Overflow)
    }
    fn base_copy_storage_bytes(
        &self,
        extra_pairs: usize,
    ) -> Result<usize, tobkiri_canon::AllocationError> {
        use tobkiri_canon::AllocationError;
        let mut bytes = self
            .profile
            .clone_storage_bytes()
            .checked_add(
                self.executing_pairs
                    .len()
                    .checked_add(extra_pairs)
                    .and_then(|count| count.checked_mul(std::mem::size_of::<(PackId, PackHash)>()))
                    .ok_or(AllocationError::Overflow)?,
            )
            .ok_or(AllocationError::Overflow)?;
        for (pack, _) in self.executing_pairs.iter() {
            bytes = bytes
                .checked_add(pack.clone_storage_bytes())
                .ok_or(AllocationError::Overflow)?;
        }
        if let Some(budget) = &self.budget {
            let scope_bytes = budget
                .clone_storage_bytes()
                .map_err(|_| AllocationError::Overflow)?;
            bytes = bytes
                .checked_add(usize::try_from(scope_bytes).map_err(|_| AllocationError::Overflow)?)
                .ok_or(AllocationError::Overflow)?;
        }
        Ok(bytes)
    }
    fn copy_pairs_precharged(
        &self,
        extra: usize,
    ) -> Result<Vec<(PackId, PackHash)>, tobkiri_canon::AllocationError> {
        use tobkiri_canon::AllocationError;
        let length = self
            .executing_pairs
            .len()
            .checked_add(extra)
            .ok_or(AllocationError::Overflow)?;
        let mut pairs = Vec::new();
        pairs
            .try_reserve_exact(length)
            .map_err(|_| AllocationError::Allocator)?;
        for (pack, hash) in self.executing_pairs.iter() {
            pairs.push((pack.try_clone_precharged()?, hash.clone()));
        }
        Ok(pairs)
    }
    pub fn try_clone_precharged(&self) -> Result<Self, tobkiri_canon::AllocationError> {
        // CallCx is an immutable snapshot handle. Its heap-backed data and
        // control reservation are Arc-shared, so cloning creates no new
        // retained context record or unaccounted duplicate metadata.
        Ok(self.clone())
    }

    /// Storage estimate for a new context that replaces its budget ledger.
    pub fn clone_with_budget_storage_bytes(
        &self,
        budget: &BudgetContext,
    ) -> Result<usize, tobkiri_canon::AllocationError> {
        use tobkiri_canon::AllocationError;
        let old = self
            .budget
            .as_deref()
            .map(BudgetContext::clone_storage_bytes)
            .transpose()
            .map_err(|_| AllocationError::Overflow)?
            .unwrap_or(0);
        let new = budget
            .clone_storage_bytes()
            .map_err(|_| AllocationError::Overflow)?;
        self.clone_storage_bytes()?
            .checked_sub(usize::try_from(old).map_err(|_| AllocationError::Overflow)?)
            .and_then(|bytes| bytes.checked_add(usize::try_from(new).ok()?))
            .ok_or(AllocationError::Overflow)
    }

    /// Create a context snapshot with an inherited budget ledger. Heap-backed
    /// identity fields remain shared; the changed ledger/control record is
    /// reserved before its new Arc storage is retained.
    pub fn try_clone_with_budget_precharged(
        &self,
        budget: BudgetContext,
    ) -> Result<Self, GateError> {
        match self.budget.as_deref() {
            Some(parent) if !budget.inherits(parent) => return Err(GateError::Unavailable),
            None if self.depth != 0 => return Err(GateError::Unavailable),
            _ => (),
        }
        budget.check().map_err(GateError::from)?;
        let dynamic = usize::try_from(
            budget
                .clone_storage_bytes()
                .map_err(|_| GateError::Unavailable)?,
        )
        .map_err(|_| GateError::Unavailable)?;
        let bytes = std::mem::size_of::<Self>()
            .checked_add(dynamic)
            .and_then(|bytes| bytes.checked_add(std::mem::size_of::<ControlReservation>()))
            .and_then(|bytes| {
                bytes.checked_add(self.extra_reservations_copy_storage_bytes(1).ok()?)
            })
            .ok_or(GateError::Unavailable)?;
        let reservation = self.control.reserve(
            ControlKind::Context,
            u64::try_from(bytes).map_err(|_| GateError::Unavailable)?,
        )?;
        let mut context = self.clone();
        context.deadline = self.deadline.min(budget.deadline());
        context.budget = Some(std::sync::Arc::new(budget));
        context
            .push_extra_reservation(reservation)
            .map_err(|_| GateError::Unavailable)?;
        Ok(context)
    }

    fn push_extra_reservation(
        &mut self,
        reservation: ControlReservation,
    ) -> Result<(), tobkiri_canon::AllocationError> {
        let len = self
            .extra_control_reservations
            .len()
            .checked_add(1)
            .ok_or(tobkiri_canon::AllocationError::Overflow)?;
        let mut reservations = Vec::new();
        reservations
            .try_reserve_exact(len)
            .map_err(|_| tobkiri_canon::AllocationError::Allocator)?;
        reservations.extend(self.extra_control_reservations.iter().cloned());
        reservations.push(std::sync::Arc::new(reservation));
        self.extra_control_reservations = std::sync::Arc::new(reservations);
        Ok(())
    }

    fn extra_reservations_copy_storage_bytes(
        &self,
        extra: usize,
    ) -> Result<usize, tobkiri_canon::AllocationError> {
        let count = self
            .extra_control_reservations
            .len()
            .checked_add(extra)
            .ok_or(tobkiri_canon::AllocationError::Overflow)?;
        std::mem::size_of::<Vec<std::sync::Arc<ControlReservation>>>()
            .checked_add(
                count
                    .checked_mul(std::mem::size_of::<std::sync::Arc<ControlReservation>>())
                    .ok_or(tobkiri_canon::AllocationError::Overflow)?,
            )
            .ok_or(tobkiri_canon::AllocationError::Overflow)
    }

    /// Core supplies actual verified principal/limits and a separately funded
    /// direct child. This method itself conveys no entry/dispatch authority.
    pub fn try_callee_precharged(
        &self,
        principal: Principal,
        request: RequestId,
        limits: ResourceLimits,
        cancellation: CoreCancellation,
    ) -> Result<Self, GateError> {
        self.check_active()?;
        if self.depth >= 32
            || request == *self.request
            || !cancellation.is_direct_child_of(&self.cancel)
            || !cancellation.uses_control(&self.control)
        {
            return Err(GateError::Unavailable);
        }
        let bytes = callee_record_bytes(self, &principal, &request)?;
        let reservation = self.control.reserve(ControlKind::Context, bytes)?;
        self.build_callee(principal, request, limits, cancellation, reservation)
    }

    /// Creates a Core callee context with a newly generated request identity.
    /// Capacity is reserved before that identity or the child token is built.
    pub fn try_callee(
        &self,
        principal: Principal,
        limits: ResourceLimits,
    ) -> Result<Self, GateError> {
        self.check_active()?;
        self.ensure_child_depth()?;
        let bytes = callee_record_bytes_max_request(self, &principal)?;
        let reservation = self.control.reserve(ControlKind::Context, bytes)?;
        let mut entropy = [0u8; 16];
        getrandom::fill(&mut entropy).map_err(|_| GateError::Unavailable)?;
        let request = RequestId::new(format!("req_{}", hex::encode(entropy)))
            .map_err(|error| GateError::Invalid(error.to_string()))?;
        let cancellation = self.cancel.child_with_control(&self.control)?;
        self.build_callee(principal, request, limits, cancellation, reservation)
    }

    /// Same as `try_callee`, using an exact request ID prepared by trusted Core.
    pub fn try_callee_with_request(
        &self,
        principal: Principal,
        request: RequestId,
        limits: ResourceLimits,
    ) -> Result<Self, GateError> {
        self.check_active()?;
        self.ensure_child_depth()?;
        if request == *self.request {
            return Err(GateError::Unavailable);
        }
        let bytes = callee_record_bytes(self, &principal, &request)?;
        let reservation = self.control.reserve(ControlKind::Context, bytes)?;
        let cancellation = self.cancel.child_with_control(&self.control)?;
        self.build_callee(principal, request, limits, cancellation, reservation)
    }

    /// Create an ordinary child with the same limits and inherited authority.
    pub fn try_child(&self, principal: Principal) -> Result<Self, GateError> {
        self.try_callee(principal, self.limits.clone())
    }

    /// Compatibility child constructor. Trusted production paths should use
    /// `try_child` and propagate a capacity denial. The infallible legacy
    /// surface returns an inert snapshot on denial instead of panicking; its
    /// `check_active` always fails the depth guard.
    pub fn child(&self, principal: Principal) -> Self {
        self.try_child(principal)
            .unwrap_or_else(|_| self.inert_denied_snapshot())
    }

    /// Compatibility callee constructor. Trusted production paths should use
    /// `try_callee` and propagate a capacity denial. The infallible legacy
    /// surface returns an inert snapshot on denial instead of panicking.
    pub fn callee(&self, principal: Principal, limits: ResourceLimits) -> Self {
        self.try_callee(principal, limits)
            .unwrap_or_else(|_| self.inert_denied_snapshot())
    }

    fn inert_denied_snapshot(&self) -> Self {
        let mut denied = self.clone();
        denied.depth = u16::MAX;
        denied.cancel = self.cancel.inert_view();
        denied
    }

    fn ensure_child_depth(&self) -> Result<(), GateError> {
        if self.depth >= 32 {
            Err(GateError::Limit("call depth exceeds 32".into()))
        } else {
            Ok(())
        }
    }

    fn build_callee(
        &self,
        principal: Principal,
        request: RequestId,
        limits: ResourceLimits,
        cancellation: CoreCancellation,
        reservation: ControlReservation,
    ) -> Result<Self, GateError> {
        Ok(Self {
            profile: self.profile.clone(),
            principal: std::sync::Arc::new(principal),
            request: std::sync::Arc::new(request),
            parent: Some(self.request.clone()),
            cancel: cancellation,
            deadline: self.deadline_for_limits(&limits),
            limits,
            security_epoch: self.security_epoch,
            interactive_approval: self.interactive_approval,
            revision: self.revision.clone(),
            depth: self.depth + 1,
            executing_pairs: self.executing_pairs.clone(),
            budget: self.budget.clone(),
            native_effects: self.native_effects.clone(),
            control: self.control.clone(),
            _control_reservation: std::sync::Arc::new(reservation),
            extra_control_reservations: self.extra_control_reservations.clone(),
            #[cfg(feature = "closed-request-metrics")]
            observation: self.observation.clone(),
        })
    }
    pub fn profile(&self) -> &ProfileId {
        &self.profile
    }
    pub fn principal(&self) -> &Principal {
        &self.principal
    }
    pub fn request(&self) -> &RequestId {
        &self.request
    }
    pub fn parent(&self) -> Option<&RequestId> {
        self.parent.as_deref()
    }
    pub fn cancel(&self) -> &CancellationToken {
        &self.cancel
    }
    pub fn cancellation(&self) -> &CancellationToken {
        &self.cancel
    }
    pub fn deadline(&self) -> Instant {
        self.deadline
    }
    /// Return a snapshot bounded by an additional native deadline. This only
    /// shortens the original deadline and preserves identity, permissions,
    /// resource limits, cancellation lineage and the shared budget ledger.
    pub fn cap_deadline(mut self, deadline: Instant) -> Self {
        self.deadline = self.deadline.min(deadline);
        self
    }
    pub fn limits(&self) -> &ResourceLimits {
        &self.limits
    }
    /// Accounting only; this never proves compiled call origin or authority.
    pub fn budget(&self) -> Option<&BudgetContext> {
        self.budget.as_deref()
    }
    /// Root admission installs one ledger. Descendants can only retain that
    /// ledger and every ancestor scope; they cannot reset or omit a cap.
    pub fn with_budget(mut self, budget: BudgetContext) -> Result<Self, GateError> {
        match self.budget.as_deref() {
            Some(parent) if !budget.inherits(parent) => {
                return Err(GateError::Denied(
                    "execution budget cannot reset ancestor scopes".into(),
                ));
            }
            None if self.depth != 0 => {
                return Err(GateError::Denied(
                    "descendant cannot create an execution budget".into(),
                ));
            }
            _ => (),
        }
        budget.check().map_err(GateError::from)?;
        let bytes = budget
            .clone_storage_bytes()
            .map_err(|_| GateError::Unavailable)?
            .checked_add(std::mem::size_of::<BudgetContext>() as u64)
            .and_then(|bytes| bytes.checked_add(std::mem::size_of::<ControlReservation>() as u64))
            .and_then(|bytes| {
                u64::try_from(self.extra_reservations_copy_storage_bytes(1).ok()?)
                    .ok()
                    .and_then(|extra| bytes.checked_add(extra))
            })
            .ok_or(GateError::Unavailable)?;
        let reservation = self.control.reserve(ControlKind::Context, bytes)?;
        self.push_extra_reservation(reservation)
            .map_err(|_| GateError::Unavailable)?;
        self.deadline = self.deadline.min(budget.deadline());
        self.budget = Some(std::sync::Arc::new(budget));
        Ok(self)
    }
    pub fn security_epoch(&self) -> u64 {
        self.security_epoch
    }
    /// Core derives a callee context from a verified (PackId, PackHash) limit map.
    /// Fuel/memory belong to that callee; cancellation and the earlier deadline persist.
    pub fn deadline_for_limits(&self, limits: &ResourceLimits) -> Instant {
        self.deadline.min(
            Instant::now()
                .checked_add(std::time::Duration::from_millis(limits.wall_ms))
                .unwrap_or(self.deadline),
        )
    }
    pub fn interactive_approval(&self) -> bool {
        self.interactive_approval
    }
    pub fn with_interactive_approval(mut self, allowed: bool) -> Self {
        self.interactive_approval &= allowed;
        self
    }
    pub fn non_interactive(self) -> Self {
        self.with_interactive_approval(false)
    }
    pub fn try_non_interactive_precharged(&self) -> Result<Self, tobkiri_canon::AllocationError> {
        let mut cx = self.try_clone_precharged()?;
        cx.interactive_approval = false;
        Ok(cx)
    }
    /// A context is frozen to its first compiled revision; children inherit it.
    pub fn with_revision(mut self, revision: Digest) -> Self {
        if self.revision.is_none() {
            self.revision = Some(revision);
        }
        self
    }
    pub fn revision(&self) -> Option<&Digest> {
        self.revision.as_ref()
    }
    pub fn depth(&self) -> u16 {
        self.depth
    }
    pub fn executing_pairs(&self) -> &[(PackId, PackHash)] {
        self.executing_pairs.as_slice()
    }
    pub fn running_pack_storage_bytes(
        &self,
        pack: &PackId,
    ) -> Result<usize, tobkiri_canon::AllocationError> {
        self.clone_storage_bytes()?
            .checked_add(std::mem::size_of::<(PackId, PackHash)>())
            .and_then(|bytes| bytes.checked_add(pack.clone_storage_bytes()))
            .ok_or(tobkiri_canon::AllocationError::Overflow)
    }
    pub fn try_with_running_pack_precharged(
        mut self,
        pack: &PackId,
        hash: &PackHash,
    ) -> Result<Self, tobkiri_canon::AllocationError> {
        let bytes = self
            .executing_pairs
            .iter()
            .try_fold(
                std::mem::size_of::<Vec<(PackId, PackHash)>>(),
                |total, (item, _)| {
                    total
                        .checked_add(item.clone_storage_bytes())
                        .and_then(|n| n.checked_add(std::mem::size_of::<(PackId, PackHash)>()))
                        .ok_or(tobkiri_canon::AllocationError::Overflow)
                },
            )?
            .checked_add(std::mem::size_of::<(PackId, PackHash)>())
            .and_then(|n| n.checked_add(pack.clone_storage_bytes()))
            .and_then(|n| n.checked_add(std::mem::size_of::<ControlReservation>()))
            .and_then(|n| n.checked_add(self.extra_reservations_copy_storage_bytes(1).ok()?))
            .ok_or(tobkiri_canon::AllocationError::Overflow)?;
        let reservation = self
            .control
            .reserve(
                ControlKind::Context,
                u64::try_from(bytes).map_err(|_| tobkiri_canon::AllocationError::Overflow)?,
            )
            .map_err(control_allocation_error)?;
        let mut pairs = self.copy_pairs_precharged(1)?;
        pairs.push((pack.try_clone_precharged()?, hash.clone()));
        self.executing_pairs = std::sync::Arc::new(pairs);
        self.push_extra_reservation(reservation)?;
        Ok(self)
    }
    pub fn without_interactive_approval(self) -> Self {
        self.with_interactive_approval(false)
    }
    /// Core snapshots a policy epoch before dispatch. Guest code has no CallCx.
    pub fn with_security_epoch(mut self, epoch: u64) -> Self {
        self.security_epoch = epoch;
        self
    }
    pub fn narrow_limits(mut self, limits: &ResourceLimits) -> Self {
        self.limits = self.limits.intersect(limits);
        self.deadline = self.deadline.min(
            Instant::now()
                .checked_add(std::time::Duration::from_millis(self.limits.wall_ms))
                .unwrap_or(self.deadline),
        );
        self
    }
    pub fn check_active(&self) -> Result<(), GateError> {
        if self.depth > 32 {
            return Err(GateError::Limit("call depth exceeds 32".into()));
        }
        if Instant::now() >= self.deadline {
            Err(GateError::Deadline)
        } else if self.cancel.is_cancelled() {
            Err(GateError::Cancelled)
        } else {
            if let Some(budget) = &self.budget {
                budget.check().map_err(GateError::from)?;
            }
            Ok(())
        }
    }
}

fn root_control_bytes(
    profile: &ProfileId,
    principal: &Principal,
    request: &RequestId,
) -> Result<u64, GateError> {
    let dynamic = profile
        .clone_storage_bytes()
        .checked_add(
            principal
                .clone_storage_bytes()
                .map_err(|_| GateError::Unavailable)?,
        )
        .and_then(|bytes| bytes.checked_add(request.clone_storage_bytes()))
        .and_then(|bytes| bytes.checked_add(std::mem::size_of::<CallCx>()))
        .and_then(|bytes| bytes.checked_add(std::mem::size_of::<NativeEffectProgress>()))
        .and_then(|bytes| bytes.checked_add(2 * std::mem::size_of::<usize>()))
        .ok_or_else(|| GateError::Limit("context size overflow".into()))?;
    u64::try_from(dynamic).map_err(|_| GateError::Limit("context size overflow".into()))
}

fn callee_record_bytes(
    parent: &CallCx,
    principal: &Principal,
    request: &RequestId,
) -> Result<u64, GateError> {
    let dynamic = parent
        .callee_storage_bytes()
        .map_err(|_| GateError::Unavailable)?
        .checked_add(
            principal
                .clone_storage_bytes()
                .map_err(|_| GateError::Unavailable)?,
        )
        .and_then(|bytes| bytes.checked_add(request.clone_storage_bytes()))
        .and_then(|bytes| bytes.checked_add(std::mem::size_of::<CallCx>()))
        .ok_or_else(|| GateError::Limit("callee context size overflow".into()))?;
    u64::try_from(dynamic).map_err(|_| GateError::Limit("callee context size overflow".into()))
}

fn callee_record_bytes_max_request(
    parent: &CallCx,
    principal: &Principal,
) -> Result<u64, GateError> {
    let dynamic = parent
        .callee_storage_bytes()
        .map_err(|_| GateError::Unavailable)?
        .checked_add(
            principal
                .clone_storage_bytes()
                .map_err(|_| GateError::Unavailable)?,
        )
        .and_then(|bytes| bytes.checked_add(36)) // `req_` plus 128 random bits in hex.
        .and_then(|bytes| bytes.checked_add(std::mem::size_of::<CallCx>()))
        .ok_or_else(|| GateError::Limit("callee context size overflow".into()))?;
    u64::try_from(dynamic).map_err(|_| GateError::Limit("callee context size overflow".into()))
}

fn control_allocation_error(error: CoreControlError) -> tobkiri_canon::AllocationError {
    match error {
        CoreControlError::Invalid => tobkiri_canon::AllocationError::Unavailable,
        CoreControlError::Limit => tobkiri_canon::AllocationError::Limit,
    }
}

impl From<BudgetError> for GateError {
    fn from(error: BudgetError) -> Self {
        match error {
            BudgetError::Deadline => Self::Deadline,
            BudgetError::Allocation
            | BudgetError::Cost
            | BudgetError::Iterations
            | BudgetError::ScopeDepth => Self::Limit(error.to_string()),
            BudgetError::InvalidLimits | BudgetError::ContextMismatch => {
                Self::Invalid(error.to_string())
            }
            BudgetError::Poisoned => Self::Persistence(error.to_string()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;
    fn root() -> CallCx {
        CallCx::root(
            ProfileId::parse("prof_context").unwrap(),
            Principal::User,
            RequestId::parse("req_context").unwrap(),
            CancellationToken::new(),
            Instant::now() + Duration::from_secs(30),
            ResourceLimits {
                wall_ms: 30_000,
                ..Default::default()
            },
        )
    }

    #[test]
    fn native_mutation_evidence_survives_children_and_isolated_roots_stay_clean() {
        let parent = root();
        let sibling = parent.try_child(Principal::User).unwrap();
        let child = parent.try_child(Principal::User).unwrap();
        let independent = root();
        assert!(!parent.filesystem_mutation_possible());
        child.note_filesystem_mutation();
        assert!(parent.filesystem_mutation_possible());
        assert!(sibling.filesystem_mutation_possible());
        assert!(!independent.filesystem_mutation_possible());
        drop(parent);
        let descendant = child.try_child(Principal::User).unwrap();
        drop(child);
        assert!(descendant.filesystem_mutation_possible());
    }
    #[test]
    fn call_context_clones_share_one_admitted_record_and_account() {
        let control = CoreControl::new(crate::CoreControlLimits {
            // One slot is the root cancellation node and one is the context
            // record; context aliases share the latter reservation.
            max_contexts: 2,
            ..Default::default()
        })
        .unwrap();
        let cancellation = CoreCancellation::new_with_control(&control).unwrap();
        let context = CallCx::try_root_with_control(
            ProfileId::parse("prof_context").unwrap(),
            Principal::User,
            RequestId::parse("req_context").unwrap(),
            cancellation,
            Instant::now() + Duration::from_secs(30),
            ResourceLimits::default(),
            control.clone(),
        )
        .unwrap();
        let cloned = context.clone();
        assert_eq!(control.usage().count(ControlKind::Context), 2);
        assert!(matches!(
            cloned.try_child(Principal::User),
            Err(GateError::Limit(_))
        ));
        drop(context);
        assert_eq!(control.usage().count(ControlKind::Context), 2);
        drop(cloned);
        assert_eq!(control.usage().count(ControlKind::Context), 0);
    }

    #[test]
    fn root_context_rejects_cancellation_from_another_control_account() {
        let context_control = CoreControl::default();
        let cancellation_control = CoreControl::default();
        let cancellation = CoreCancellation::new_with_control(&cancellation_control).unwrap();
        let result = CallCx::try_root_with_control(
            ProfileId::parse("prof_context").unwrap(),
            Principal::User,
            RequestId::parse("req_context").unwrap(),
            cancellation,
            Instant::now() + Duration::from_secs(30),
            ResourceLimits::default(),
            context_control,
        );
        assert!(matches!(result, Err(GateError::Denied(_))));
    }

    #[test]
    fn cancellation_is_scoped_and_noninteractive_revision_are_sticky() {
        let revision = Digest::hash(b"a");
        let parent = root().with_revision(revision.clone()).non_interactive();
        let child = parent
            .child(Principal::User)
            .with_interactive_approval(true)
            .with_revision(Digest::hash(b"b"));
        assert!(!child.interactive_approval());
        assert_eq!(child.revision(), Some(&revision));
        child.cancel().cancel();
        assert!(!parent.cancel().is_cancelled());
        let sibling = parent.child(Principal::User);
        parent.cancel().cancel();
        assert!(sibling.cancel().is_cancelled());
    }
    #[test]
    fn excessive_depth_and_huge_wall_caps_cannot_panic_or_extend_deadline() {
        let parent = root();
        let callee = parent
            .try_callee(
                Principal::User,
                ResourceLimits {
                    wall_ms: u64::MAX,
                    ..Default::default()
                },
            )
            .unwrap();
        assert!(callee.deadline() <= parent.deadline());
        let narrowed = parent.clone().narrow_limits(&ResourceLimits {
            wall_ms: 1,
            ..ResourceLimits::unlimited()
        });
        assert_eq!(narrowed.limits().wall_ms, 1);
        assert!(narrowed.deadline() < parent.deadline());
        let unchanged = parent
            .clone()
            .cap_deadline(parent.deadline() + Duration::from_secs(5));
        assert_eq!(unchanged.deadline(), parent.deadline());
        assert_eq!(unchanged.profile(), parent.profile());
        assert_eq!(unchanged.principal(), parent.principal());
        assert_eq!(unchanged.request(), parent.request());
        assert_eq!(unchanged.limits(), parent.limits());
        let shortened = parent
            .clone()
            .cap_deadline(parent.deadline() - Duration::from_millis(1));
        assert!(shortened.deadline() < parent.deadline());
        assert_eq!(shortened.profile(), parent.profile());
        assert_eq!(shortened.principal(), parent.principal());
        assert_eq!(shortened.request(), parent.request());
        assert_eq!(shortened.limits(), parent.limits());
        let mut child = parent;
        for _ in 0..32 {
            child = child.try_child(Principal::User).unwrap();
        }
        child.check_active().unwrap();
        assert!(matches!(
            child.try_child(Principal::User),
            Err(GateError::Limit(_))
        ));
        assert!(matches!(
            child.child(Principal::User).check_active(),
            Err(GateError::Limit(_))
        ));
        let denied = child.child(Principal::User);
        denied.cancel().cancel();
        assert!(!child.cancel().is_cancelled());
    }
}
