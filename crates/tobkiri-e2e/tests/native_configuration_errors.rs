//! The real Store -> Core -> native error adapter, without GUI authority claims.
use tobkiri_core::{CoreError, ProfileCommitProgress};
use tobkiri_launcher_core_host::{HostError, core_error};
use tobkiri_store::StoreError;

#[test]
fn native_profile_commit_errors_retain_uncertainty_without_source_text() {
    for progress in [
        ProfileCommitProgress::EpochCommitAttempted,
        ProfileCommitProgress::EpochAdvanced,
        ProfileCommitProgress::HeadReplaced,
    ] {
        let store = StoreError::ProfileCommitUncertain {
            operation: None,
            progress,
            source: Box::new(StoreError::Io(std::io::Error::other(
                "private-configuration-source",
            ))),
        };
        let error = CoreError::Cleanup {
            original: Box::new(CoreError::from(store)),
            cleanup: Box::new(CoreError::Invalid("fixture cleanup".into())),
        };
        let native = core_error(error);
        assert!(matches!(
            native,
            HostError::Rejected("profile-commit-uncertain")
        ));
        assert!(!native.to_string().contains("private-configuration-source"));
    }
}

#[test]
fn native_configuration_codes_depend_on_typed_errors_not_message_text() {
    for (error, expected) in [
        (
            CoreError::Invalid("profile-commit-uncertain".into()),
            "invalid",
        ),
        (CoreError::Conflicts(vec![]), "conflict"),
        (CoreError::NotFound("fixture".into()), "not-found"),
    ] {
        assert!(matches!(core_error(error), HostError::Rejected(code) if code == expected));
    }
}
