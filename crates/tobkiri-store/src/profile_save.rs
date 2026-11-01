//! Durable permission/epoch decision coordinated with the existing filesystem
//! head. Constructors and policy/cleanup reads never acquire the Profile lock.
use super::*;
use serde::{Deserialize, Serialize};
use tobkiri_gate::{ControlReservation, CoreControl, PermissionApprovalBatch};

#[cfg(unix)]
const BOOKKEEPING_BYTES: u64 = 128 * 1024;
#[cfg(unix)]
const MAX_DECISIONS: i64 = MAX_TERMINAL_BYTES / MAX_TERMINAL_ENTRY_BYTES;
#[cfg(unix)]
const FORMAT: &str = "tobkiri.profile-save/1";

#[cfg(all(test, unix))]
#[path = "profile_save_tests.rs"]
mod tests;

pub(super) fn initialize(tx: &rusqlite::Transaction<'_>) -> Result<(), StoreError> {
    tx.execute_batch(
        "CREATE TABLE IF NOT EXISTS profile_save_decisions(
             operation TEXT PRIMARY KEY CHECK(length(CAST(operation AS BLOB)) BETWEEN 1 AND 128),
             body BLOB NOT NULL CHECK(length(CAST(body AS BLOB)) BETWEEN 1 AND 2048),
             settled INTEGER NOT NULL DEFAULT 0 CHECK(settled IN(0,1)));
         CREATE TABLE IF NOT EXISTS profile_save_members(
             operation TEXT NOT NULL REFERENCES profile_save_decisions(operation) DEFERRABLE INITIALLY DEFERRED,
             ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 0 AND 4095),
             grant_id TEXT NOT NULL UNIQUE CHECK(length(CAST(grant_id AS BLOB)) BETWEEN 1 AND 128),
             PRIMARY KEY(operation,ordinal));
         CREATE TRIGGER IF NOT EXISTS profile_save_members_no_delete BEFORE DELETE ON profile_save_members
             BEGIN SELECT RAISE(ABORT,'save membership is permanent'); END;
         CREATE TRIGGER IF NOT EXISTS profile_save_members_no_update BEFORE UPDATE ON profile_save_members
             BEGIN SELECT RAISE(ABORT,'save membership is immutable'); END;
         CREATE UNIQUE INDEX IF NOT EXISTS one_pending_profile_save
             ON profile_save_decisions((1)) WHERE settled=0;
         CREATE TRIGGER IF NOT EXISTS profile_save_no_delete
             BEFORE DELETE ON profile_save_decisions
             BEGIN SELECT RAISE(ABORT,'save decision is permanent'); END;
         CREATE TRIGGER IF NOT EXISTS profile_save_no_rewrite
             BEFORE UPDATE OF operation,body ON profile_save_decisions
             BEGIN SELECT RAISE(ABORT,'save decision is immutable'); END;
         CREATE TRIGGER IF NOT EXISTS profile_save_no_unsettle
             BEFORE UPDATE OF settled ON profile_save_decisions
             WHEN NEW.settled<OLD.settled
             BEGIN SELECT RAISE(ABORT,'save outcome cannot reset'); END;",
    )?;
    Ok(())
}

pub(super) fn check_pending(pending: bool) -> Result<(), GateError> {
    if pending {
        Err(GateError::Persistence(
            "Profile save recovery pending".into(),
        ))
    } else {
        Ok(())
    }
}

pub(super) fn deny_pending(conn: &rusqlite::Connection) -> Result<(), GateError> {
    let pending = conn
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM profile_save_decisions WHERE settled=0)",
            [],
            |row| row.get(0),
        )
        .map_err(persistence)?;
    check_pending(pending)
}

#[derive(PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Decision {
    format: String,
    profile: ProfileId,
    operation: GrantId,
    request: tobkiri_canon::ids::RequestId,
    previous: Option<Digest>,
    revision: Digest,
    plan: Digest,
    from_epoch: u64,
    epoch: u64,
    batch: Digest,
    members: u32,
}

/// Native evidence of this exact durable save outcome. It is not a current
/// Profile snapshot, product activation receipt or authority to execute/retry.
/// Retains its actual native bookkeeping account; it cannot be cloned or
/// deserialized from a UI/Pack response.
pub struct ProfileSaveReceipt {
    decision: Decision,
    _retained: ControlReservation,
}

/// Descriptive native lookup identity. Never a grant or replay permit.
#[derive(Debug)]
pub struct ProfileSaveOperation {
    profile: ProfileId,
    grant: GrantId,
}
impl ProfileSaveOperation {
    pub fn profile(&self) -> &ProfileId {
        &self.profile
    }
    pub fn grant(&self) -> &GrantId {
        &self.grant
    }
}

impl ProfileSaveReceipt {
    /// Retain the exact operation if later audit delivery fails after this
    /// receipt was acquired. The display never renders private source text.
    pub fn delivery_uncertainty(&self, source: StoreError) -> StoreError {
        StoreError::ProfileCommitUncertain {
            progress: crate::ProfileCommitProgress::HeadReplaced,
            operation: Some(ProfileSaveOperation {
                profile: self.decision.profile.clone(),
                grant: self.decision.operation.clone(),
            }),
            source: Box::new(source),
        }
    }
    pub fn operation(&self) -> &GrantId {
        &self.decision.operation
    }
    pub fn profile(&self) -> &ProfileId {
        &self.decision.profile
    }
    pub fn revision(&self) -> &Digest {
        &self.decision.revision
    }
    pub fn plan(&self) -> &Digest {
        &self.decision.plan
    }
    pub fn epoch(&self) -> u64 {
        self.decision.epoch
    }
}

#[cfg(unix)]
fn uncertain(
    decision: &Decision,
    progress: crate::ProfileCommitProgress,
    source: StoreError,
) -> StoreError {
    StoreError::ProfileCommitUncertain {
        progress,
        operation: Some(ProfileSaveOperation {
            profile: decision.profile.clone(),
            grant: decision.operation.clone(),
        }),
        source: Box::new(source),
    }
}

#[cfg(unix)]
#[path = "profile_save_sql.rs"]
mod sql;

#[cfg(unix)]
fn head(directory: &DataRoot) -> Result<Option<Digest>, StoreError> {
    match directory.read("head", 256) {
        Ok(bytes) => Ok(Some(
            Digest::parse(
                std::str::from_utf8(&bytes)
                    .map_err(|_| StoreError::Invalid("invalid Profile head".into()))?,
            )
            .map_err(|_| StoreError::Invalid("invalid Profile head".into()))?,
        )),
        Err(StoreError::Io(error)) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error),
    }
}

#[cfg(unix)]
fn complete(
    directory: &DataRoot,
    approvals: &Approvals,
    d: &Decision,
    sync_directory: impl FnOnce(&std::fs::File) -> std::io::Result<()>,
) -> Result<(), StoreError> {
    let Some((recorded, settled)) = approvals.save_decision(Some(&d.operation))? else {
        return Err(StoreError::Integrity(IntegrityFaultCode::AuthorityDatabase));
    };
    if settled || recorded != *d {
        return Err(StoreError::Integrity(IntegrityFaultCode::AuthorityDatabase));
    }
    let current = head(directory)?;
    if current.as_ref() != d.previous.as_ref() && current.as_ref() != Some(&d.revision) {
        return Err(StoreError::Conflict(
            "pending save has an unrelated head".into(),
        ));
    }
    directory.verify_profile_revision(&d.revision)?;
    // Also sync the actual pinned directory explicitly. Do not depend on a
    // virtual VFS pathname successfully naming the rollback-journal directory.
    directory
        .directory_capability()
        .sync_all()
        .map_err(|source| {
            uncertain(
                d,
                crate::ProfileCommitProgress::EpochAdvanced,
                source.into(),
            )
        })?;
    let mut renamed = false;
    directory
        .write_atomic_with_directory_sync(
            std::path::Path::new("head"),
            d.revision.as_str().as_bytes(),
            |dir| {
                renamed = true;
                sync_directory(dir)
            },
        )
        .map_err(|source| {
            uncertain(
                d,
                if renamed {
                    crate::ProfileCommitProgress::HeadReplaced
                } else {
                    crate::ProfileCommitProgress::EpochAdvanced
                },
                source,
            )
        })?;
    approvals
        .settle_profile_save(d)
        .map_err(|source| uncertain(d, crate::ProfileCommitProgress::HeadReplaced, source))?;
    directory
        .directory_capability()
        .sync_all()
        .map_err(|source| uncertain(d, crate::ProfileCommitProgress::HeadReplaced, source.into()))
}

impl DataRoot {
    /// Commit one already verified/compiled Core candidate and exact prepared
    /// approvals. Holds the original Profile lock through decision/head/outcome.
    /// A postdecision failure remains fenced and exposes its exact operation.
    pub fn commit_prepared_profile(
        &self,
        batch: &PermissionApprovalBatch<'_, '_>,
        previous: Option<&Digest>,
        revision: &Digest,
    ) -> Result<ProfileSaveReceipt, StoreError> {
        #[cfg(unix)]
        {
            self.commit_prepared_profile_inner(
                batch,
                previous,
                revision,
                || Ok(()),
                std::fs::File::sync_all,
            )
        }
        #[cfg(not(unix))]
        {
            let _ = (batch, previous, revision);
            Err(StoreError::Unavailable(
                "Profile save coordination unavailable".into(),
            ))
        }
    }

    #[cfg(unix)]
    fn commit_prepared_profile_inner(
        &self,
        batch: &PermissionApprovalBatch<'_, '_>,
        previous: Option<&Digest>,
        revision: &Digest,
        before_head: impl FnOnce() -> Result<(), StoreError>,
        sync_directory: impl FnOnce(&std::fs::File) -> std::io::Result<()>,
    ) -> Result<ProfileSaveReceipt, StoreError> {
        let retained = batch.reserve_metadata(BOOKKEEPING_BYTES)?;
        let first = batch
            .iter()
            .next()
            .ok_or_else(|| StoreError::Invalid("empty save".into()))?;
        let lock = self.profile_lock(&first.reservation.profile, ".revision.lock")?;
        batch.check_active()?;
        if head(&lock.directory)?.as_ref() != previous {
            return Err(StoreError::Conflict("Profile head changed".into()));
        }
        lock.directory.verify_profile_revision(revision)?;
        let approvals = Approvals::open_pinned(&lock.directory, &first.reservation.profile)?;
        let decision =
            approvals.checked_save(approvals.decide_profile_save(batch, previous, revision))?;
        before_head().map_err(|source| {
            uncertain(
                &decision,
                crate::ProfileCommitProgress::EpochAdvanced,
                source,
            )
        })?;
        approvals
            .checked_save(complete(
                &lock.directory,
                &approvals,
                &decision,
                sync_directory,
            ))
            .map_err(|source| {
                if source.profile_commit_progress().is_some() {
                    source
                } else {
                    uncertain(
                        &decision,
                        crate::ProfileCommitProgress::EpochAdvanced,
                        source,
                    )
                }
            })?;
        Ok(ProfileSaveReceipt {
            decision,
            _retained: retained,
        })
    }

    /// Complete only a previously decided save. Never repeats prompts, policy
    /// effects or epoch advancement. Constructors/getters do not call recovery.
    pub fn recover_profile_save(
        &self,
        profile: &ProfileId,
        control: &CoreControl,
    ) -> Result<Option<ProfileSaveReceipt>, StoreError> {
        #[cfg(unix)]
        {
            let retained = control.reserve(tobkiri_gate::ControlKind::Queue, BOOKKEEPING_BYTES)?;
            let lock = self.profile_lock(profile, ".revision.lock")?;
            let approvals = Approvals::open_pinned(&lock.directory, profile)?;
            let Some((decision, _)) = approvals.checked_save(approvals.save_decision(None))? else {
                return Ok(None);
            };
            approvals
                .checked_save(complete(
                    &lock.directory,
                    &approvals,
                    &decision,
                    std::fs::File::sync_all,
                ))
                .map_err(|source| {
                    if source.profile_commit_progress().is_some() {
                        source
                    } else {
                        uncertain(
                            &decision,
                            crate::ProfileCommitProgress::EpochAdvanced,
                            source,
                        )
                    }
                })?;
            Ok(Some(ProfileSaveReceipt {
                decision,
                _retained: retained,
            }))
        }
        #[cfg(not(unix))]
        {
            let _ = (profile, control);
            Err(StoreError::Unavailable(
                "Profile save recovery unavailable".into(),
            ))
        }
    }

    /// Read an exact past settled outcome. It is not proof of current activation.
    pub fn profile_save_outcome(
        &self,
        profile: &ProfileId,
        operation: &GrantId,
        control: &CoreControl,
    ) -> Result<Option<ProfileSaveReceipt>, StoreError> {
        #[cfg(unix)]
        {
            let retained = control.reserve(tobkiri_gate::ControlKind::Queue, BOOKKEEPING_BYTES)?;
            let approvals = Approvals::open(self, profile)?;
            Ok(approvals
                .save_decision(Some(operation))?
                .and_then(|(decision, settled)| {
                    settled.then_some(ProfileSaveReceipt {
                        decision,
                        _retained: retained,
                    })
                }))
        }
        #[cfg(not(unix))]
        {
            let _ = (profile, operation, control);
            Err(StoreError::Unavailable(
                "Profile save outcomes unavailable".into(),
            ))
        }
    }
}
