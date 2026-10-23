//! Descriptive dispatch history from an authenticated live Core scope.
//! References are opaque locators, never effect permission or terminal custody.
use super::*;

/// Identity/limits only, with no cancellation token, mutable budget or native
/// execution handle. Like every view here, these fields are descriptions.
pub struct ReachedContextView<'a> {
    pub profile: &'a ProfileId,
    pub principal: &'a Principal,
    pub request: &'a RequestId,
    pub revision: Option<&'a Digest>,
    pub security_epoch: u64,
    pub deadline: std::time::Instant,
    pub limits: &'a ResourceLimits,
}
impl<'a> From<&'a CallCx> for ReachedContextView<'a> {
    fn from(cx: &'a CallCx) -> Self {
        Self {
            profile: cx.profile(),
            principal: cx.principal(),
            request: cx.request(),
            revision: cx.revision(),
            security_epoch: cx.security_epoch(),
            deadline: cx.deadline(),
            limits: cx.limits(),
        }
    }
}

/// Immutable historical data. Reading it does not authorize a later effect.
/// Ancestors may have finished; the original leaf must authenticate separately.
pub struct ReachedEvidenceView<'a> {
    pub invocation: &'a Digest,
    pub entry: &'a ExecutionEntryEvidence,
    pub entry_context: ReachedContextView<'a>,
    pub current: ReachedContextView<'a>,
    pub active: &'a ActiveFrameMetadata,
    pub has_parent: bool,
}

/// A Core-created metadata reference bound to the original exact leaf. Parent
/// selection changes only the historical node, never the authenticated leaf.
/// There is no guest/Serde constructor, raw Grant selector or native owner.
pub struct ReachedEvidenceRef {
    registration: RegistrationHandle,
    leaf: ControlHandle<dyn Any + Send + Sync>,
    selected: ControlHandle<dyn Any + Send + Sync>,
}
impl ReachedEvidenceRef {
    /// Fixed by-value carrier inventory for a native resource owner to prepay
    /// before retaining it in a table. Its private metadata is separately funded.
    pub fn layout_bytes() -> usize {
        std::mem::size_of::<Self>()
    }
}

fn scope_preparation<'a>(
    cx: &'a CallCx,
    scope: &'a AllocationFrameScope,
    proof: &'a dyn TypedReachedInvocation,
) -> PreparedFrameMetadata<'a> {
    let owner = match scope.owner() {
        CompiledCodeOwner::Pack { pack, hash } => PreparedCodeOwner::Pack { pack, hash },
        CompiledCodeOwner::User { profile_ast } => PreparedCodeOwner::User { profile_ast },
        CompiledCodeOwner::UserDocument { document } => {
            PreparedCodeOwner::UserDocument { document }
        }
    };
    PreparedFrameMetadata {
        caller: cx,
        entry: PreparedFrameEntry::ReachedInvocation(proof),
        owner,
        frame: scope.frame(),
        node: scope.node(),
        activation: scope.activation(),
        path: scope.path(),
        transitive_memory_bytes: scope.metadata.transitive_memory_bytes,
    }
}

impl GateEngine {
    /// Retain only the entry metadata reached by this exact live private scope.
    /// The fixed Gate callback runs under Core's original admission/node locks;
    /// callers cannot supply callbacks, metadata, parent IDs or execution proof.
    pub fn inspect_reached_evidence(
        &self,
        cx: &CallCx,
        scope: &AllocationFrameScope,
    ) -> Result<ReachedEvidenceRef, GateError> {
        self.authenticate_frame_scope(cx, scope)?;
        let bytes = scope.registration.reached_evidence_storage_bytes()?;
        if bytes == 0 {
            return Err(GateError::Unavailable);
        }
        let mut funding = Some(self.control().reserve(ControlKind::Frame, bytes)?);
        let mut called = false;
        let mut retained = None;
        let mut callback_error = None;
        let verified = scope.registration.with_reached_invocation(
            cx,
            scope.proof.as_ref(),
            scope.reason(),
            &mut |proof| {
                if called {
                    callback_error = Some(GateError::Denied("evidence callback repeated".into()));
                    return Err(GateError::Unavailable);
                }
                called = true;
                let result = (|| {
                    self.check_context(cx)?;
                    self.check_frame_registration(cx, &scope.registration)?;
                    self.check_prepared_entry(
                        cx,
                        &scope_preparation(cx, scope, proof),
                        scope.reason(),
                    )?;
                    scope.registration.retain_reached_evidence(
                        scope.proof.as_ref(),
                        funding.take().ok_or(GateError::Unavailable)?,
                    )
                })();
                match result {
                    Ok(reference) => {
                        retained = Some(reference);
                        Ok(())
                    }
                    Err(error) => {
                        callback_error = Some(error);
                        Err(GateError::Unavailable)
                    }
                }
            },
        );
        // All potentially final reference/funding drops occur after Core's
        // synchronous callback and its admission/lifecycle guards have ended.
        if let Some(error) = callback_error {
            return Err(error);
        }
        verified?;
        let leaf = retained.ok_or(GateError::Unavailable)?;
        Ok(ReachedEvidenceRef {
            registration: scope.registration.clone(),
            selected: leaf.clone(),
            leaf,
        })
    }

    fn check_reached_evidence(
        &self,
        cx: &CallCx,
        scope: &AllocationFrameScope,
        reference: &ReachedEvidenceRef,
    ) -> Result<(), GateError> {
        self.authenticate_frame_scope(cx, scope)?;
        if !ControlHandle::ptr_eq(&scope.registration, &reference.registration) {
            return Err(GateError::Denied(
                "evidence belongs to another factory".into(),
            ));
        }
        let mut called = false;
        let mut callback_error = None;
        let verified = scope.registration.with_reached_invocation(
            cx,
            scope.proof.as_ref(),
            scope.reason(),
            &mut |proof| {
                if called {
                    callback_error = Some(GateError::Denied("evidence callback repeated".into()));
                    return Err(GateError::Unavailable);
                }
                called = true;
                let result = (|| {
                    self.check_context(cx)?;
                    self.check_frame_registration(cx, &scope.registration)?;
                    self.check_prepared_entry(
                        cx,
                        &scope_preparation(cx, scope, proof),
                        scope.reason(),
                    )?;
                    scope
                        .registration
                        .match_reached_evidence(scope.proof.as_ref(), reference.leaf.as_ref())
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

    /// Read historical evidence after freshly authenticating the original
    /// live leaf. Returned borrows describe a point-in-time inspection only;
    /// no subsequent secrets/network/storage admission may treat them as proof.
    pub fn reached_evidence_view<'a>(
        &self,
        cx: &CallCx,
        scope: &AllocationFrameScope,
        reference: &'a ReachedEvidenceRef,
    ) -> Result<ReachedEvidenceView<'a>, GateError> {
        self.check_reached_evidence(cx, scope, reference)?;
        reference
            .registration
            .reached_evidence_view(reference.selected.as_ref())
    }

    /// Select the actual immutable parent without changing the original leaf
    /// binding. Each retained private carrier is separately prepaid; the
    /// original chain remains charged. There is no arbitrary traversal depth.
    pub fn reached_evidence_parent(
        &self,
        cx: &CallCx,
        scope: &AllocationFrameScope,
        reference: &ReachedEvidenceRef,
    ) -> Result<Option<ReachedEvidenceRef>, GateError> {
        let view = self.reached_evidence_view(cx, scope, reference)?;
        if !view.has_parent {
            return Ok(None);
        }
        let bytes = reference.registration.reached_evidence_storage_bytes()?;
        if bytes == 0 {
            return Err(GateError::Unavailable);
        }
        let funding = self.control().reserve(ControlKind::Frame, bytes)?;
        let selected = reference
            .registration
            .retain_reached_parent(reference.selected.as_ref(), funding)?;
        Ok(Some(ReachedEvidenceRef {
            registration: reference.registration.clone(),
            leaf: reference.leaf.clone(),
            selected,
        }))
    }
}
