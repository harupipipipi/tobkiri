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
