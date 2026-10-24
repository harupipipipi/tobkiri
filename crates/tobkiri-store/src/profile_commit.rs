//! Facts observed before a failed Profile head/epoch commit returned.
//!
//! These are not a commit receipt, a current snapshot or proof of rollback.
//! A caller must reconcile durable state before deciding what to do next.

use crate::StoreError;

/// Last observed step of a failed commit, without a success or rollback claim.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ProfileCommitProgress {
    /// SQLite COMMIT was attempted; its error does not prove the acquired epoch.
    EpochCommitAttempted,
    /// Epoch advancement succeeded; head replacement was not confirmed.
    EpochAdvanced,
    /// The head rename succeeded, but directory durability was not confirmed.
    HeadReplaced,
}

impl std::fmt::Display for ProfileCommitProgress {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Self::EpochCommitAttempted => "epoch-commit-attempted",
            Self::EpochAdvanced => "epoch-advanced",
            Self::HeadReplaced => "head-replaced",
        })
    }
}

impl StoreError {
    /// The last observed step for this failed Profile commit. This never means
    /// that the candidate is current, durable, approved or safe to replay.
    pub fn profile_commit_progress(&self) -> Option<ProfileCommitProgress> {
        match self {
            Self::ProfileCommitUncertain { progress, .. } => Some(*progress),
            _ => None,
        }
    }
}
