//! The completed loader and the exact replacement rows used by recovery.
use super::*;
pub(in crate::original_authority::io::publication) use artifact_identity::ArtifactTreeIdentity;
use shell_inputs::RecoveryShellVariant;
use std::sync::Mutex;

/// Result of checking the current catalog binding and the selected package bytes.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum ActiveArtifactVerification {
    Current,
    SuccessorNeedsReconfirmation,
}

/// Borrow the loader only during a synchronous Host artifact or definition
/// check. The same loader is available to Pack imports during a Store call.
pub(crate) struct RecoveryCatalogAccess<'a> {
    loader: &'a Mutex<Option<CatalogIo>>,
    profiles: &'a CoreValue,
    admitted: Option<&'a CoreValue>,
}

impl<'a> RecoveryCatalogAccess<'a> {
    pub(crate) fn new(
        loader: &'a Mutex<Option<CatalogIo>>,
        profiles: &'a CoreValue,
        admitted: Option<&'a CoreValue>,
    ) -> anyhow::Result<Self> {
        anyhow::ensure!(
            loader
                .lock()
                .unwrap_or_else(|error| error.into_inner())
                .is_some(),
            "recovery catalog loader is missing"
        );
        Ok(Self {
            loader,
            profiles,
            admitted,
        })
    }

    pub(in crate::original_authority::io::publication) fn with_inputs<T>(
        &self,
        run: impl FnOnce(RecoveryCatalogInputs<'_>) -> Result<T>,
    ) -> Result<T> {
        let loader = self
            .loader
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let loader = loader.as_ref().ok_or_else(|| {
            OriginalAuthorityError::unsupported(
                OriginalAuthorityStage::Source,
                "recovery catalog loader is missing",
            )
        })?;
        run(RecoveryCatalogInputs::new(
            loader,
            self.profiles,
            self.admitted,
        ))
    }
}

pub(crate) struct RecoveryCatalogInputs<'a> {
    io: &'a CatalogIo,
    profiles: &'a CoreValue,
    admitted: Option<&'a CoreValue>,
}

impl<'a> RecoveryCatalogInputs<'a> {
    pub(crate) fn new(
        io: &'a CatalogIo,
        profiles: &'a CoreValue,
        admitted: Option<&'a CoreValue>,
    ) -> Self {
        Self {
            io,
            profiles,
            admitted,
        }
    }

    pub(in crate::original_authority::io::publication) fn verify_committed_artifact<
        S: crate::BootstrapReviewOwner,
    >(
        &self,
        op: &Operation<'_, S>,
        profile: &CoreValue,
        profile_id: &str,
    ) -> Result<ArtifactTreeIdentity> {
        self.io.verify_exact_artifact(op, profile, |op, selected| {
            self.io.verify_recovery_shell_binding(
                op,
                selected,
                profile_id,
                self.profiles,
                self.admitted,
            )
        })
    }

    /// Verify the exact active package or classify a physically verified successor.
    pub(in crate::original_authority::io::publication) fn verify_active_artifact<
        S: crate::BootstrapReviewOwner,
    >(
        &self,
        op: &Operation<'_, S>,
        profile: &CoreValue,
        profile_id: &str,
    ) -> Result<ActiveArtifactVerification> {
        self.io.verify_catalogued_active_artifact(op, |op| {
            self.io
                .active_artifact_variant(op, profile, profile_id, self.profiles, self.admitted)
        })
    }

    /// Compare the publication candidate to the same registered definition the App loaded.
    pub(in crate::original_authority::io::publication) fn verify_registered_definition<
        S: crate::BootstrapReviewOwner,
    >(
        &self,
        op: &Operation<'_, S>,
        profile_id: &str,
        expected_digest: &str,
    ) -> Result<()> {
        self.io.require_recovery_catalog(op)?;
        let changed = || {
            raw(
                recovery_workspace::APP_DENIED,
                "Profile publication definition changed",
            )
        };
        let source = shell_inputs::registered_profile(self.profiles, profile_id, &self.io.budget)?
            .ok_or_else(changed)?;
        let actual = source
            .digest_guest(&self.io.budget)
            .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source))?;
        op.check(OriginalAuthorityStage::Source)?;
        if actual.as_str() != expected_digest {
            return Err(changed());
        }
        Ok(())
    }

    pub(in crate::original_authority::io::publication) fn require_committed_artifact_identity<
        S: crate::BootstrapReviewOwner,
    >(
        &self,
        op: &Operation<'_, S>,
        profile: &CoreValue,
        expected: &ArtifactTreeIdentity,
    ) -> Result<()> {
        self.io
            .require_exact_artifact_identity(op, profile, expected)
    }
}

impl CatalogIo {
    pub(in crate::original_authority::io::publication) fn verify_bundled_active_artifact<
        S: crate::BootstrapReviewOwner,
    >(
        &self,
        op: &Operation<'_, S>,
        profile: &CoreValue,
        profile_id: &str,
    ) -> Result<ActiveArtifactVerification> {
        self.verify_catalogued_active_artifact(op, |op| {
            self.bundled_active_variant(op, profile, profile_id)
        })
    }

    pub(in crate::original_authority::io::publication) fn verify_bundled_pending_artifact<
        S: crate::BootstrapReviewOwner,
    >(
        &self,
        op: &Operation<'_, S>,
        profile: &CoreValue,
        profile_id: &str,
    ) -> Result<ArtifactTreeIdentity> {
        self.verify_exact_artifact(op, profile, |op, selected| {
            self.verify_bundled_shell_binding(op, selected, profile_id)
        })
    }

    pub(in crate::original_authority::io::publication) fn verify_initial_active_artifact<
        S: crate::BootstrapReviewOwner,
    >(
        &self,
        op: &Operation<'_, S>,
        profile: &CoreValue,
        profile_id: &str,
        source_profile: &CoreValue,
    ) -> Result<ActiveArtifactVerification> {
        self.verify_catalogued_active_artifact(op, |op| {
            self.initial_active_variant(op, profile, profile_id, source_profile)
        })
    }

    fn verify_catalogued_active_artifact<S: crate::BootstrapReviewOwner>(
        &self,
        op: &Operation<'_, S>,
        select: impl FnOnce(&Operation<'_, S>) -> Result<(CoreValue, ActiveArtifactVerification)>,
    ) -> Result<ActiveArtifactVerification> {
        let mut outcome = ActiveArtifactVerification::Current;
        artifact_identity::bounded(
            op,
            || {
                artifact_identity::artifact_rejected(
                    "packaged artifact verification deadline exceeded",
                )
            },
            |op| {
                let (variant, classification) = select(op)?;
                outcome = classification;
                let (root, parent) = self.artifact_anchor()?;
                artifact_identity::verify_active_package(op, &self.budget, root, parent, &variant)?;
                Ok(outcome)
            },
        )
        .map_err(|mut error| {
            if outcome == ActiveArtifactVerification::SuccessorNeedsReconfirmation
                && let Some(OriginalAuthorityCause::Python {
                    class: "tobkiri_protocol.errors.ProtocolError",
                    message,
                    ..
                }) = &error.cause
            {
                error.message = format!("verified successor Shell artifact rejected: {message}");
            }
            error
        })
    }

    /// Verify the Shell for the exact independently reread bundled Profile.
    pub(in crate::original_authority::io::publication) fn verify_initial_artifact<
        S: crate::BootstrapReviewOwner,
    >(
        &self,
        op: &Operation<'_, S>,
        profile: &CoreValue,
        profile_id: &str,
        source_profile: &CoreValue,
    ) -> Result<ArtifactTreeIdentity> {
        self.verify_exact_artifact(op, profile, |op, selected| {
            self.verify_initial_shell_binding(op, selected, profile_id, source_profile)
        })
    }

    fn verify_exact_artifact<S: crate::BootstrapReviewOwner>(
        &self,
        op: &Operation<'_, S>,
        profile: &CoreValue,
        check_binding: impl FnOnce(&Operation<'_, S>, &RecoveryShellVariant) -> Result<()>,
    ) -> Result<ArtifactTreeIdentity> {
        let (selected, identity) =
            artifact_identity::bounded(op, artifact_identity::identity_timeout, |op| {
                self.capture_exact_artifact(op, profile)
            })?;
        artifact_identity::bounded(
            op,
            || {
                artifact_identity::artifact_rejected(
                    "packaged artifact verification deadline exceeded",
                )
            },
            |op| {
                check_binding(op, &selected)?;
                let (root, parent) = self.artifact_anchor()?;
                artifact_identity::SelectedPackage::new(
                    root,
                    selected.relative(),
                    parent,
                    &identity,
                )
                .verify(op, &self.budget, selected.variant())
            },
        )?;
        self.require_exact_artifact_identity(op, profile, &identity)?;
        Ok(identity)
    }

    pub(in crate::original_authority::io::publication) fn require_exact_artifact_identity<
        S: crate::BootstrapReviewOwner,
    >(
        &self,
        op: &Operation<'_, S>,
        profile: &CoreValue,
        expected: &ArtifactTreeIdentity,
    ) -> Result<()> {
        artifact_identity::bounded(op, artifact_identity::identity_timeout, |op| {
            let (_, current) = self.capture_exact_artifact(op, profile)?;
            artifact_identity::require_same(op, &self.budget, expected, &current)
        })
    }

    fn capture_exact_artifact<S: crate::BootstrapReviewOwner>(
        &self,
        op: &Operation<'_, S>,
        profile: &CoreValue,
    ) -> Result<(RecoveryShellVariant, ArtifactTreeIdentity)> {
        let selected = self.recovery_identity_variant(op, profile)?;
        let (root, parent) = self.artifact_anchor()?;
        let current =
            artifact_identity::capture(op, &self.budget, root, selected.relative(), parent, None)?;
        Ok((selected, current))
    }

    fn artifact_anchor(&self) -> Result<(&Path, Identity)> {
        let root = self.artifact_root.as_deref().ok_or_else(|| {
            raw(
                recovery_workspace::APP_DENIED,
                "active Profile Shell artifact is unavailable",
            )
        })?;
        let parent = self.artifact_parent_identity.ok_or_else(|| {
            OriginalAuthorityError::unsupported(
                OriginalAuthorityStage::Path,
                "committed recovery artifact parent is not held",
            )
        })?;
        Ok((root, parent))
    }
}
