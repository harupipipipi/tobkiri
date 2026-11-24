//! Native save-state observation for an existing or first-run configuration UI.
use serde::Serialize;
use tobkiri_canon::{Digest, ids::ProfileId};

/// Facts to bind into a native review, never approval or activation authority.
/// The caller must retain the exact candidate and this target identity; these
/// fields alone do not certify an application-specific confirmation or receipt.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct ProfileSavePrecondition {
    pub profile_id: ProfileId,
    pub head: Option<Digest>,
    pub security_epoch: u64,
}
