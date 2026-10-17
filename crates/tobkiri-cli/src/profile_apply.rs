//! Same-identity structural edits through Core's normal approval and head CAS.

use super::{bounded_read, profile_input};
use anyhow::{Context, Result, bail};
use serde::Serialize;
use std::path::{Path, PathBuf};
use tobkiri_core::{
    Core, CoreError, Digest,
    ids::ProfileId,
    profile::{Profile, PublishedProfile, parse_profile, parse_published},
};

enum Document {
    Profile(Profile),
    Published(PublishedProfile),
}

pub(super) struct Definition {
    document: Document,
    origin: PathBuf,
}

/// Parse once before opening Core. Identity-bearing documents must name the
/// explicit target; a publication contains no identity or saved approvals.
pub(super) fn load(target: &ProfileId, path: &Path) -> Result<Definition> {
    let bytes = bounded_read(path, tobkiri_core::MAX_BYTES)?;
    let document = match parse_profile(&bytes) {
        Ok(profile) => {
            if &profile.id != target {
                bail!("Profile definition id must match --profile; apply cannot change identity");
            }
            Document::Profile(profile)
        }
        Err(profile_error) => {
            let published = parse_published(&bytes).map_err(|_| {
                anyhow::Error::new(profile_error)
                    .context("parse apply definition; published Profile fallback also failed")
            })?;
            Document::Published(published)
        }
    };
    Ok(Definition {
        document,
        origin: path.to_path_buf(),
    })
}

impl Definition {
    fn materialize(self, current: &Profile) -> Result<Profile> {
        let mut proposed = match self.document {
            Document::Profile(profile) => profile,
            Document::Published(document) => {
                document.to_profile(current.id.clone(), current.name.clone())
            }
        };
        if proposed.id != current.id {
            bail!("Profile definition id must match the existing Profile");
        }
        profile_input::resolve_sources(
            proposed.packs.iter_mut().map(|pin| &mut pin.source),
            &self.origin,
        )?;
        Ok(proposed)
    }
}

#[derive(Debug, Serialize)]
pub(super) struct Receipt {
    pub profile: ProfileId,
    pub applied_head: Digest,
    pub observed_head: Digest,
    pub observed_epoch: u64,
    pub applied_revision_is_current: bool,
}

fn observe_committed(
    profile: &ProfileId,
    applied_head: Digest,
    snapshot: Result<(Digest, u64), CoreError>,
) -> Result<Receipt> {
    let (observed_head, observed_epoch) = snapshot.with_context(|| {
        format!(
            "Profile {profile} update committed at {applied_head}; could not read its head/epoch; reload before any further edit"
        )
    })?;
    Ok(Receipt {
        profile: profile.clone(),
        applied_revision_is_current: observed_head == applied_head,
        applied_head,
        observed_head,
        observed_epoch,
    })
}

pub(super) async fn apply(
    core: &Core,
    target: &ProfileId,
    expected_head: &Digest,
    definition: Definition,
) -> Result<Receipt> {
    // A missing Profile is an error, never a create/import fallback. This
    // precheck precedes source resolution, Pack verification and new approvals.
    let (current, current_head) = core
        .profile(target)
        .context("load existing Profile for apply")?;
    if &current_head != expected_head {
        bail!("Profile head changed; reload before applying the edit");
    }
    let proposed = definition.materialize(&current)?;
    // Core independently checks this expectation and performs the final atomic
    // CAS after verified compilation and ordinary trusted permission approval.
    let applied_head = core
        .save_profile(proposed, Some(expected_head.clone()))
        .await?;
    observe_committed(target, applied_head, core.profile_head_snapshot(target))
}

#[cfg(test)]
#[path = "profile_apply_tests.rs"]
mod tests;
