//! Native save preconditions, including a not-yet-published Profile head.
#[cfg(unix)]
use crate::Approvals;
use crate::{DataRoot, StoreError};
use tobkiri_canon::{Digest, ids::ProfileId};
#[cfg(unix)]
use tobkiri_gate::ApprovalStore;

impl DataRoot {
    /// Prepare native authority metadata and capture a coherent head/epoch pair.
    ///
    /// A fresh Profile may acquire its directory and empty authority schema,
    /// but no revision, approval, permission effect or runtime is created.
    /// An absent head does not imply epoch zero: prior native revocation or an
    /// interrupted attempt may already have authority metadata. Pending durable
    /// saves remain fenced; this operation never completes their recovery.
    /// Facts are not readiness or an approval; ordinary save/admission checks
    /// still apply, including an integrity fault that may remain inspectable.
    pub fn profile_save_snapshot(
        &self,
        profile: &ProfileId,
    ) -> Result<(Option<Digest>, u64), StoreError> {
        #[cfg(unix)]
        {
            // Same established order as head commit: pinned Profile inode lock
            // -> pinned authority database. Do not hold a SQL transaction over
            // UI interaction or invoke recovery from the database constructor.
            let lock = self.profile_lock(profile, ".revision.lock")?;
            let head = match lock.directory.read("head", 256) {
                Ok(raw) => Some(
                    Digest::parse(
                        std::str::from_utf8(&raw)
                            .map_err(|_| StoreError::Invalid("invalid Profile head".into()))?,
                    )
                    .map_err(|error| StoreError::Canon(error.to_string()))?,
                ),
                Err(StoreError::Io(error)) if error.kind() == std::io::ErrorKind::NotFound => None,
                Err(error) => return Err(error),
            };
            let approvals = Approvals::open_pinned(&lock.directory, profile)?;
            // Decision/head writers require the same Profile lock, so pending
            // status cannot change between this check and the epoch read.
            // Revocation may advance epoch independently; the returned pair
            // describes its actual read point under a stable head, and a later
            // change is rejected by Core::save_profile_at_epoch.
            approvals.ensure_no_pending_profile_save()?;
            Ok((head, approvals.security_epoch(profile)?))
        }
        #[cfg(not(unix))]
        {
            let _ = profile;
            Err(StoreError::Unavailable(
                "Profile snapshot directory locking is unsupported".into(),
            ))
        }
    }
}
