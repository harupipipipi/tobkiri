//! The completed loader and the exact replacement rows used by recovery.
use super::*;

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
    ) -> Result<()> {
        let selected = self.io.recovery_identity_variant(op, profile)?;
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
            .verify(op, &self.io.budget, selected.variant())
    }
}
