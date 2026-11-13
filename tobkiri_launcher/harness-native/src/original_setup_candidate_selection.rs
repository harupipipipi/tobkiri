//! The fresh Setup caller's Host prefix after the actual catalog and ID getter.
use super::*;
use tobkiri_canon::AllocationBudget;

/// The reached branch of the original candidate selector. The caller owns the
/// exact loaded Pack catalog and invokes its denied factory when requested.
// The two funded arenas move directly into Review without a new allocation.
#[allow(clippy::large_enum_variant)]
pub(crate) enum CandidateSelection {
    BundledCatalog,
    Denied(&'static str),
    ActiveAuthority(ActiveCandidate),
}

/// Data from this exact verified pointer and definition lookup, kept in Review
/// for the active Authority branch. Neither store retains a live lock here.
pub(crate) struct ActiveCandidate {
    #[cfg(unix)]
    _pointer: active_profile::LoadedPointer,
    #[cfg(unix)]
    _registered: definitions::RegisteredProfile,
    #[cfg(unix)]
    _workspace: Funded<PathBuf>,
}

/// Native snapshot DATA held by this review after the original Authority closes.
pub(crate) struct ResolverAuthority {
    _material: OriginalAuthorityMaterial,
    _snapshot_digest: Digest,
    _bundle_lock_digest: Digest,
}

#[cfg(all(test, unix))]
impl ResolverAuthority {
    pub(super) fn observation(&self) -> (&OriginalAuthorityMaterial, &Digest, &Digest) {
        (
            &self._material,
            &self._snapshot_digest,
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
    Resolving,
    SnapshotStarted,
}

#[derive(Default)]
pub(super) struct FreshSetup {
    recoveries: u8,
    root: Option<Funded<PathBuf>>,
    cancellation: Option<CoreCancellation>,
    progress: Progress,
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
        let result = select(&op, root, profile_id, include_source_additions, budget)?;
        if matches!(result, CandidateSelection::BundledCatalog) {
            self.fresh.progress = Progress::SelectedBundled;
        }
        Ok(result)
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
            Progress::Resolving | Progress::SnapshotStarted
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
                _snapshot_digest: read.snapshot_digest.expect("requested Authority snapshot"),
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
    include_source_additions: bool,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CandidateSelection> {
    let fallback = |message| {
        if include_source_additions {
            CandidateSelection::Denied(message)
        } else {
            CandidateSelection::BundledCatalog
        }
    };
    let _path_capacity = op.reserve(
        OriginalAuthorityStage::Path,
        root.as_os_str().len().saturating_mul(2).saturating_add(256),
    )?;
    let pointer_path = root.join("profiles/active.json");
    // Follow the original two path operations, including a dangling symlink.
    let present = pointer_path.exists() || pointer_path.is_symlink();
    op.check(OriginalAuthorityStage::Source)?;
    if !present {
        return Ok(fallback("source update requires an active Profile"));
    }
    #[cfg(unix)]
    {
        let Some(pointer) = active_profile::load(op, root)? else {
            return Ok(fallback("source update requires the active Profile"));
        };
        if !pointer.profile_id().is(profile_id) {
            return Ok(fallback("source update requires the active Profile"));
        }
        let Some(registered) = definitions::get_profile(op, root, pointer.profile_id(), budget)?
        else {
            return Ok(fallback("source update requires a registered Profile"));
        };
        let workspace = workspace_path(op, root, profile_id)?;
        Ok(CandidateSelection::ActiveAuthority(ActiveCandidate {
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
