use tobkiri_flow::StepError;
use tobkiri_profile::Conflict;

/// Errors at the trusted in-process Core boundary.
#[derive(Debug, thiserror::Error)]
pub enum CoreError {
    #[error("{original}; cleanup also failed: {cleanup}")]
    Cleanup {
        original: Box<CoreError>,
        cleanup: Box<CoreError>,
    },
    #[error("invalid configuration: {0}")]
    Invalid(String),
    #[error("Profile has conflicts: {0:?}")]
    Conflicts(Vec<Conflict>),
    #[error("Profile or artifact not found: {0}")]
    NotFound(String),
    #[error("authority: {0}")]
    Gate(#[from] tobkiri_gate::GateError),
    #[error("Flow: {0}")]
    Step(#[from] StepError),
    #[error("storage: {0}")]
    Io(#[from] std::io::Error),
    #[error("document: {0}")]
    Json(#[from] serde_json::Error),
    #[error("canonical document: {0}")]
    Canon(#[from] tobkiri_canon::CanonError),
    #[error("store: {0}")]
    Store(#[from] tobkiri_store::StoreError),
    #[error("Pack: {0}")]
    Manifest(#[from] tobkiri_manifest::ManifestError),
    #[error("Profile: {0}")]
    Profile(#[from] tobkiri_profile::ProfileError),
    #[error("runtime: {0}")]
    Runtime(String),
}

impl From<tobkiri_gate::CoreControlError> for CoreError {
    fn from(error: tobkiri_gate::CoreControlError) -> Self {
        Self::Gate(error.into())
    }
}

impl CoreError {
    pub fn profile_save_profile(&self) -> Option<&tobkiri_canon::ids::ProfileId> {
        match self {
            Self::Store(error) => error.profile_save_profile(),
            Self::Cleanup { original, cleanup } => {
                if original.profile_commit_progress().is_some() {
                    original.profile_save_profile()
                } else {
                    cleanup.profile_save_profile()
                }
            }
            _ => None,
        }
    }
    pub fn profile_save_operation(&self) -> Option<&tobkiri_gate::GrantId> {
        match self {
            Self::Store(error) => error.profile_save_operation(),
            Self::Cleanup { original, cleanup } => {
                if original.profile_commit_progress().is_some() {
                    original.profile_save_operation()
                } else {
                    cleanup.profile_save_operation()
                }
            }
            _ => None,
        }
    }

    /// Preserve Profile commit uncertainty at the native facade boundary.
    /// The original error has precedence if cleanup also reports uncertainty;
    /// this observation is not an aggregate commit/rollback outcome.
    pub fn profile_commit_progress(&self) -> Option<crate::ProfileCommitProgress> {
        match self {
            Self::Store(error) => error.profile_commit_progress(),
            Self::Cleanup { original, cleanup } => original
                .profile_commit_progress()
                .or_else(|| cleanup.profile_commit_progress()),
            _ => None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ProfileCommitProgress;

    fn uncertain(progress: ProfileCommitProgress) -> CoreError {
        tobkiri_store::StoreError::ProfileCommitUncertain {
            operation: None,
            progress,
            source: Box::new(tobkiri_store::StoreError::Io(std::io::Error::other(
                "private-source-path-must-not-be-rendered",
            ))),
        }
        .into()
    }

    #[test]
    fn profile_commit_progress_survives_core_and_cleanup_without_source_text() {
        for progress in [
            ProfileCommitProgress::EpochCommitAttempted,
            ProfileCommitProgress::EpochAdvanced,
            ProfileCommitProgress::HeadReplaced,
        ] {
            let error = uncertain(progress);
            assert_eq!(error.profile_commit_progress(), Some(progress));
            assert!(error.to_string().contains("profile-commit-uncertain"));
            assert!(!error.to_string().contains("private-source-path"));
            let wrapped = CoreError::Cleanup {
                original: Box::new(error),
                cleanup: Box::new(CoreError::Invalid("fixture cleanup".into())),
            };
            assert_eq!(wrapped.profile_commit_progress(), Some(progress));
            assert!(!wrapped.to_string().contains("private-source-path"));
        }
    }

    #[test]
    fn ordinary_and_sql_commit_errors_do_not_claim_profile_commit_progress() {
        for error in [
            CoreError::Invalid("fixture".into()),
            CoreError::from(tobkiri_store::StoreError::CommitUnknown),
            CoreError::from(tobkiri_store::StoreError::Conflict("fixture".into())),
        ] {
            assert_eq!(error.profile_commit_progress(), None);
        }
    }
}
