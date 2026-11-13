//! The fresh Setup caller's Host prefix after the actual catalog and ID getter.
use super::*;
use tobkiri_canon::AllocationBudget;

/// The reached branch of the original candidate selector. The caller owns the
/// exact loaded Pack catalog and invokes its denied factory when requested.
pub(crate) enum CandidateSelection {
    BundledCatalog,
    Denied(&'static str),
    ActiveAuthority,
}

/// The two funded arenas move into the same review as the fixed candidate root.
/// Neither persistence store retains a lock after selection.
struct ActiveCandidate {
    #[cfg(unix)]
    _pointer: active_profile::LoadedPointer,
    #[cfg(unix)]
    _registered: definitions::RegisteredProfile,
    #[cfg(unix)]
    _workspace: Funded<PathBuf>,
}

type ActiveLookup = std::result::Result<ActiveCandidate, &'static str>;

/// Native-only borrows for the original activation factory. The Store and its
/// key never leave this synchronous scope or become guest data.
#[cfg(unix)]
#[cfg_attr(not(test), allow(dead_code))] // The activation factory is not yet bound.
pub(crate) struct RegisteredActivationScope<'scope, 'store, 'host, S> {
    pub(crate) activation_root: &'scope Path,
    pub(crate) workspace: &'scope Path,
    pub(crate) profile_id: &'scope str,
    _authority: &'scope super::super::Store<'store, 'host, S>,
}

#[cfg(all(test, unix))]
impl<S> RegisteredActivationScope<'_, '_, '_, S> {
    pub(crate) fn authority_observation(&self) -> (&Path, bool) {
        (&self._authority.path, self._authority.closed)
    }
}

/// Native snapshot DATA held by this review after the original Authority closes.
pub(crate) struct ResolverAuthority {
    _material: OriginalAuthorityMaterial,
    snapshot_digest: Digest,
    _bundle_lock_digest: Digest,
}
impl ResolverAuthority {
    pub(crate) fn snapshot_digest(&self) -> &Digest {
        &self.snapshot_digest
    }
    pub(crate) fn security_epoch(&self) -> &super::super::OriginalAuthorityEpoch {
        match &self._material {
            OriginalAuthorityMaterial::Genesis { epoch, .. }
            | OriginalAuthorityMaterial::Existing { epoch, .. } => epoch,
        }
    }
}

#[cfg(all(test, unix))]
impl ResolverAuthority {
    pub(super) fn observation(&self) -> (&OriginalAuthorityMaterial, &Digest, &Digest) {
        (
            &self._material,
            &self.snapshot_digest,
            &self._bundle_lock_digest,
        )
    }
}

#[derive(Default, PartialEq, Eq)]
enum Progress {
    #[default]
    Selecting,
    SelectionStarted,
    SelectedBundled,
    #[cfg(unix)]
    SelectedRegistered,
    #[cfg(unix)]
    #[cfg_attr(not(test), allow(dead_code))] // The activation factory is not yet bound.
    RegisteredAuthorityStarted,
    Resolving,
    SnapshotStarted,
    PredecessorStarted,
    InactivePredecessorStarted,
}

#[derive(Default)]
pub(super) struct FreshSetup {
    recoveries: u8,
    root: Option<Funded<PathBuf>>,
    cancellation: Option<CoreCancellation>,
    progress: Progress,
    #[cfg(unix)]
    active_candidate: Option<ActiveCandidate>,
    resolver_root: Option<Funded<PathBuf>>,
}
impl FreshSetup {
    pub(super) fn record_recovery(&mut self, cancellation: &CoreCancellation) {
        if self.recoveries == 0 {
            self.cancellation = Some(cancellation.clone());
        }
        if self.same_cancellation(cancellation) {
            self.recoveries += 1;
        }
    }

    fn same_cancellation(&self, cancellation: &CoreCancellation) -> bool {
        self.cancellation
            .as_ref()
            .is_some_and(|held| held.same_node(cancellation))
    }
}

impl PublicationRead {
    /// Freeze the actual Host selection at the original caller's root scope,
    /// before catalog loading, after both recovery occurrences returned None.
    pub(crate) fn acquire_candidate_root<S: crate::BootstrapReviewOwner>(
        &mut self,
        owner: &S,
        cancellation: &CoreCancellation,
    ) -> Result<()> {
        if self.fresh.recoveries != 2 || self.fresh.root.is_some() || !self.cleanup.verified() {
            return Err(OriginalAuthorityError::unsupported(
                OriginalAuthorityStage::Guard,
                "fresh Setup recovery is not complete",
            ));
        }
        let op = self.candidate_operation(owner, cancellation)?;
        let root = read_root(&op)?;
        self.fresh.root = Some(root);
        Ok(())
    }

    /// Keep the same root and original operation live across Pack awaits.
    pub(crate) fn check_candidate_root<S: crate::BootstrapReviewOwner>(
        &self,
        owner: &S,
        cancellation: &CoreCancellation,
    ) -> Result<()> {
        if self.fresh.root.is_none() {
            return Err(OriginalAuthorityError::unsupported(
                OriginalAuthorityStage::Guard,
                "fresh Setup root is not held",
            ));
        }
        self.candidate_operation(owner, cancellation)?
            .check(OriginalAuthorityStage::Source)
    }

    /// Use the fixed root once with the ID already validated by Setup policy.
    /// Store errors leave this caller as they occurred outside recovery.
    pub(crate) fn candidate_selection<S: crate::BootstrapReviewOwner>(
        &mut self,
        owner: &S,
        cancellation: &CoreCancellation,
        profile_id: &str,
        include_source_additions: bool,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<CandidateSelection> {
        let Some(root) = &self.fresh.root else {
            return Err(OriginalAuthorityError::unsupported(
                OriginalAuthorityStage::Guard,
                "fresh Setup root is not held",
            ));
        };
        if self.fresh.progress != Progress::Selecting {
            return Err(OriginalAuthorityError::unsupported(
                OriginalAuthorityStage::Guard,
                "fresh Setup candidate selection already started",
            ));
        }
        self.fresh.progress = Progress::SelectionStarted;
        let op = self.candidate_operation(owner, cancellation)?;
        match select(&op, root, profile_id, budget)? {
            Err(message) if include_source_additions => Ok(CandidateSelection::Denied(message)),
            Err(_) => {
                self.fresh.progress = Progress::SelectedBundled;
                Ok(CandidateSelection::BundledCatalog)
            }
            Ok(candidate) => {
                #[cfg(unix)]
                {
                    self.fresh.active_candidate = Some(candidate);
                    self.fresh.progress = Progress::SelectedRegistered;
                }
                #[cfg(not(unix))]
                let _ = candidate;
                Ok(CandidateSelection::ActiveAuthority)
            }
        }
    }

    /// Open Authority once, only for the exact held registered candidate.
    /// The caller must supply the actual factory body before enabling this
    /// operation; returning a boundary diagnostic is not a factory binding.
    #[cfg(unix)]
    #[cfg_attr(not(test), allow(dead_code))] // The activation factory is not yet bound.
    pub(crate) fn with_registered_activation_authority<S: crate::BootstrapReviewOwner, T>(
        &mut self,
        owner: &S,
        cancellation: &CoreCancellation,
        body: impl FnOnce(RegisteredActivationScope<'_, '_, '_, S>) -> Result<T>,
    ) -> Result<T> {
        self.registered_activation_authority(
            owner,
            cancellation,
            body,
            #[cfg(test)]
            None,
        )
    }

    #[cfg(unix)]
    #[cfg_attr(not(test), allow(dead_code))] // The activation factory is not yet bound.
    fn registered_activation_authority<S: crate::BootstrapReviewOwner, T>(
        &mut self,
        owner: &S,
        cancellation: &CoreCancellation,
        body: impl FnOnce(RegisteredActivationScope<'_, '_, '_, S>) -> Result<T>,
        #[cfg(test)] fixture: Option<&super::super::super::tests::Fixture>,
    ) -> Result<T> {
        if self.fresh.progress != Progress::SelectedRegistered {
            return Err(OriginalAuthorityError::unsupported(
                OriginalAuthorityStage::Guard,
                "registered Setup candidate is not held for Authority entry",
            ));
        }
        self.fresh.progress = Progress::RegisteredAuthorityStarted;
        if !self.cleanup.verified() {
            return Err(OriginalAuthorityError::unsupported(
                OriginalAuthorityStage::Guard,
                "registered Setup candidate cleanup is uncertain",
            ));
        }
        let root = self.fresh.root.as_ref().expect("registered candidate root");
        let candidate = self
            .fresh
            .active_candidate
            .as_ref()
            .expect("registered Setup candidate");
        let op = self.candidate_operation(owner, cancellation)?;
        #[cfg(test)]
        let op = Operation { fixture, ..op };
        let workspace = candidate._workspace.as_path();
        super::super::with_registered_store(&op, root, |authority| {
            // Workspace ends with the exact ASCII ID verified by Setup and
            // matched against the active pointer. No ID getter is repeated.
            let profile_id = workspace
                .file_name()
                .and_then(|name| name.to_str())
                .expect("verified workspace Profile ID");
            let _paths = op.reserve(
                OriginalAuthorityStage::Path,
                workspace
                    .as_os_str()
                    .len()
                    .saturating_mul(2)
                    .saturating_add(256),
            )?;
            let activation_root = workspace.join("activation");
            body(RegisteredActivationScope {
                activation_root: &activation_root,
                workspace,
                profile_id,
                _authority: authority,
            })
        })
    }

    #[cfg(all(test, unix))]
    pub(super) fn registered_activation_authority_close_fault<S: crate::BootstrapReviewOwner, T>(
        &mut self,
        owner: &S,
        cancellation: &CoreCancellation,
        body: impl FnOnce(RegisteredActivationScope<'_, '_, '_, S>) -> Result<T>,
    ) -> Result<T> {
        let fixture = super::super::super::tests::Fixture {
            clock: 1000.0,
            mutation: Default::default(),
            after_close_fault: true,
        };
        self.registered_activation_authority(owner, cancellation, body, Some(&fixture))
    }

    /// The original resolver captures user-data again before its bundle call.
    pub(crate) fn acquire_resolver_root<S: crate::BootstrapReviewOwner>(
        &mut self,
        owner: &S,
        cancellation: &CoreCancellation,
    ) -> Result<()> {
        if self.fresh.progress != Progress::SelectedBundled {
            return Err(OriginalAuthorityError::unsupported(
                OriginalAuthorityStage::Guard,
                "bundled Setup candidate is not held",
            ));
        }
        let root = read_root(&self.candidate_operation(owner, cancellation)?)?;
        self.fresh.resolver_root = Some(root);
        self.fresh.progress = Progress::Resolving;
        Ok(())
    }

    pub(crate) fn check_resolver_root<S: OriginalAuthorityOwner>(
        &self,
        owner: &S,
        cancellation: &CoreCancellation,
    ) -> Result<()> {
        if !matches!(
            self.fresh.progress,
            Progress::Resolving
                | Progress::SnapshotStarted
                | Progress::PredecessorStarted
                | Progress::InactivePredecessorStarted
        ) {
            return Err(OriginalAuthorityError::unsupported(
                OriginalAuthorityStage::Guard,
                "Setup resolver root is not ready",
            ));
        }
        self.candidate_operation(owner, cancellation)?
            .check(OriginalAuthorityStage::Source)
    }

    /// Read Authority from the second selected root using the second lock digest.
    /// A plain path denial remains a request to the original App factory.
    pub(crate) fn resolver_authority_snapshot<S: crate::BootstrapReviewOwner>(
        &mut self,
        owner: &S,
        cancellation: &CoreCancellation,
        bundle_lock_digest: Digest,
    ) -> Result<Option<ResolverAuthority>> {
        if self.fresh.progress != Progress::Resolving {
            return Err(OriginalAuthorityError::unsupported(
                OriginalAuthorityStage::Guard,
                "Setup resolver root is not ready",
            ));
        }
        self.fresh.progress = Progress::SnapshotStarted;
        let root = self.fresh.resolver_root.as_ref().expect("resolver root");
        let op = self.candidate_operation(owner, cancellation)?;
        op.check(OriginalAuthorityStage::Source)?;
        match super::super::read_selected(&op, root, Some(bundle_lock_digest.as_str())) {
            Ok(read) => Ok(Some(ResolverAuthority {
                _material: read.material,
                snapshot_digest: read.snapshot_digest.expect("requested Authority snapshot"),
                _bundle_lock_digest: bundle_lock_digest,
            })),
            Err(error)
                if error.class == super::super::CAPTURE_DENIAL_CLASS
                    && error.message == super::super::AUTHORITY_PATH_DENIAL =>
            {
                Ok(None)
            }
            Err(error) => Err(error),
        }
    }

    /// The post-resolve call reuses the already selected resolver user-data root.
    pub(crate) fn bootstrap_predecessor_inputs<S: OriginalAuthorityOwner>(
        &mut self,
        owner: &S,
        cancellation: &CoreCancellation,
        profile_id: &str,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<definitions::BootstrapPredecessorInputs> {
        self.check_resolver_root(owner, cancellation)?;
        if self.fresh.progress != Progress::SnapshotStarted {
            return Err(OriginalAuthorityError::unsupported(
                OriginalAuthorityStage::Guard,
                "Setup predecessor is outside the resolved candidate",
            ));
        }
        self.fresh.progress = Progress::PredecessorStarted;
        #[cfg(unix)]
        {
            let root = self.fresh.resolver_root.as_ref().expect("resolver root");
            let op = self.candidate_operation(owner, cancellation)?;
            definitions::bootstrap_predecessor_inputs(&op, root, profile_id, budget)
        }
        #[cfg(not(unix))]
        {
            let _ = (profile_id, budget);
            Err(OriginalAuthorityError::unsupported(
                OriginalAuthorityStage::Source,
                "Windows Setup predecessor persistence/ACL",
            ))
        }
    }

    /// Recheck the original paths after the Pack pin predecessor returned None.
    pub(crate) fn inactive_predecessor_has_registry<S: OriginalAuthorityOwner>(
        &mut self,
        owner: &S,
        cancellation: &CoreCancellation,
        profile_id: &str,
    ) -> Result<bool> {
        self.check_resolver_root(owner, cancellation)?;
        if self.fresh.progress != Progress::PredecessorStarted || !self.cleanup.verified() {
            return Err(OriginalAuthorityError::unsupported(
                OriginalAuthorityStage::Guard,
                "inactive predecessor is outside the resolved candidate",
            ));
        }
        self.fresh.progress = Progress::InactivePredecessorStarted;
        let op = self.candidate_operation(owner, cancellation)?;
        let root = self.fresh.resolver_root.as_ref().expect("resolver root");
        let _paths = op.reserve(
            OriginalAuthorityStage::Path,
            root.as_os_str()
                .len()
                .saturating_mul(4)
                .saturating_add(profile_id.len())
                .saturating_add(8192),
        )?;
        let active_paths = [
            root.join("profiles/active.json"),
            root.join("workspaces")
                .join(profile_id)
                .join("activation/active.json"),
        ];
        for path in active_paths {
            if predecessor_path_present(&op, &path)? {
                return Ok(false);
            }
        }
        predecessor_path_present(&op, &root.join("profiles/index.json"))
    }

    fn candidate_operation<'a, S: OriginalAuthorityOwner>(
        &'a self,
        owner: &'a S,
        cancellation: &'a CoreCancellation,
    ) -> Result<Operation<'a, S>> {
        if !self.fresh.same_cancellation(cancellation) {
            return Err(OriginalAuthorityError::unsupported(
                OriginalAuthorityStage::Guard,
                "fresh Setup cancellation changed",
            ));
        }
        Ok(Operation {
            owner,
            invocation: &self.invocation,
            cancellation,
            deadline: self
                .invocation
                .deadline()
                .map_err(|_| OriginalAuthorityError::unavailable())?,
            pid: std::process::id(),
            cleanup: self.cleanup.clone(),
            #[cfg(test)]
            fixture: None,
        })
    }
}

fn predecessor_path_present<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    path: &Path,
) -> Result<bool> {
    op.check(OriginalAuthorityStage::Source)?;
    let present = path.exists() || path.is_symlink();
    op.check(OriginalAuthorityStage::Source)?;
    Ok(present)
}

fn read_root<S: crate::BootstrapReviewOwner>(op: &Operation<'_, S>) -> Result<Funded<PathBuf>> {
    op.owner.with_original_review_user_data(|path| {
        let bound = path
            .as_os_str()
            .len()
            .checked_mul(16)
            .and_then(|n| n.checked_add(16384))
            .ok_or_else(|| OriginalAuthorityError::capacity(OriginalAuthorityStage::Path))?;
        let capacity = op.reserve(OriginalAuthorityStage::Path, bound)?;
        // The persistence layer's OS-owned alias normalization handles an
        // absent root without admitting redirected persistence ancestors.
        let value = canonical(path)?;
        if value.capacity() > bound {
            return Err(OriginalAuthorityError::capacity(
                OriginalAuthorityStage::Path,
            ));
        }
        op.check(OriginalAuthorityStage::Path)?;
        Ok(Funded {
            value,
            _capacity: capacity,
        })
    })?
}

fn select<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    root: &Path,
    profile_id: &str,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<ActiveLookup> {
    let _path_capacity = op.reserve(
        OriginalAuthorityStage::Path,
        root.as_os_str().len().saturating_mul(2).saturating_add(256),
    )?;
    let pointer_path = root.join("profiles/active.json");
    // Follow the original two path operations, including a dangling symlink.
    let present = pointer_path.exists() || pointer_path.is_symlink();
    op.check(OriginalAuthorityStage::Source)?;
    if !present {
        return Ok(Err("source update requires an active Profile"));
    }
    #[cfg(unix)]
    {
        let Some(pointer) = active_profile::load(op, root)? else {
            return Ok(Err("source update requires the active Profile"));
        };
        if !pointer.profile_id().is(profile_id) {
            return Ok(Err("source update requires the active Profile"));
        }
        let Some(registered) = definitions::get_profile(op, root, pointer.profile_id(), budget)?
        else {
            return Ok(Err("source update requires a registered Profile"));
        };
        let workspace = workspace_path(op, root, profile_id)?;
        Ok(Ok(ActiveCandidate {
            _pointer: pointer,
            _registered: registered,
            _workspace: workspace,
        }))
    }
    #[cfg(not(unix))]
    {
        let _ = (profile_id, budget);
        Err(OriginalAuthorityError::unsupported(
            OriginalAuthorityStage::Source,
            "Windows active Profile persistence/ACL",
        ))
    }
}
