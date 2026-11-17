//! The completed loader and the exact replacement rows used by recovery.
use super::*;
use artifact_identity::ArtifactTreeIdentity;

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
        let selected = self.io.recovery_identity_variant(op, profile)?;
        let (root, parent) = self.artifact_anchor()?;
        let identity = artifact_identity::capture(
            op,
            &self.io.budget,
            root,
            selected.relative(),
            parent,
            None,
        )?;
        self.io.verify_recovery_shell_binding(
            op,
            &selected,
            profile_id,
            self.profiles,
            self.admitted,
        )?;
        artifact_identity::SelectedPackage::new(root, selected.relative(), parent, &identity)
            .verify(op, &self.io.budget, selected.variant())?;
        self.require_committed_artifact_identity(op, profile, &identity)?;
        Ok(identity)
    }

    pub(in crate::original_authority::io::publication) fn require_committed_artifact_identity<
        S: crate::BootstrapReviewOwner,
    >(
        &self,
        op: &Operation<'_, S>,
        profile: &CoreValue,
        expected: &ArtifactTreeIdentity,
    ) -> Result<()> {
        let selected = self.io.recovery_identity_variant(op, profile)?;
        let (root, parent) = self.artifact_anchor()?;
        let current = artifact_identity::capture(
            op,
            &self.io.budget,
            root,
            selected.relative(),
            parent,
            None,
        )?;
        artifact_identity::require_same(op, &self.io.budget, expected, &current)
    }

    fn artifact_anchor(&self) -> Result<(&Path, Identity)> {
        let root = self.io.artifact_root.as_deref().ok_or_else(|| {
            raw(
                recovery_workspace::APP_DENIED,
                "active Profile Shell artifact is unavailable",
            )
        })?;
        let parent = self.io.artifact_parent_identity.ok_or_else(|| {
            OriginalAuthorityError::unsupported(
                OriginalAuthorityStage::Path,
                "committed recovery artifact parent is not held",
            )
        })?;
        Ok((root, parent))
    }
}
