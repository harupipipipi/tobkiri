//! In-process Core and Session facade shared by desktop and terminal hosts.

pub use tobkiri_canon::{
    AllocationBudget, AllocationError, AllocationKind, AllocationRequest, CanonValue, CoreBuffer,
    CoreBufferBuilder, CoreRef, CoreValue, Digest, MAX_BYTES, Variant, digest, encode, ids, parse,
};
pub use tobkiri_flow::{CoreStream, RunEvent, RunEventKind, RunHandle, StepError};
pub use tobkiri_gate::{
    ApprovalAnswer, ApprovalPrompt, EffectRequest, HostError, HostIo, NativeSetupInvocation,
    PermissionSet, Risk, TrustedPrompt, UiEmbed,
};
pub use tobkiri_store::ProfileCommitProgress;
/// Generic Pack authoring and verification functions exposed by Core.
pub mod pack {
    pub use tobkiri_manifest::{
        PackManifest, PackSignature, build_tpk, parse_manifest, sign_manifest, verify_directory,
        verify_tpk,
    };
}
/// Structural Profile parsing exposed by the Core facade.
pub mod profile {
    pub use tobkiri_profile::{
        ConflictReport, Profile, PublishedProfile, parse_profile, parse_published,
    };
}

mod admission;
mod capacity;
mod completion;
mod consumed_entry_terminal;
mod core;
mod error;
mod execution_frames;
mod filesystem_registry;
mod funded_arc;
mod housekeeping;
mod inputs;
mod journal;
#[cfg(unix)]
mod native_declared_artifact;
/// Opt-in native coordination candidate, separate from the product route.
#[cfg(feature = "native-shared-state")]
#[doc(hidden)]
pub mod native_flight;
mod native_profile_review;
mod outcomes;
#[cfg(test)]
mod policy_tests;
mod process_authorization;
mod process_catalog;
mod profile_review;
mod profile_save_review;
mod profiles;
mod runtime_owner;
mod session;
#[cfg(test)]
mod test_support;
mod tracked_stream;
mod ui_operation_delivery;
mod ui_operation_preparation;

pub use core::{Core, CoreConfig, ProfileRecoveryStatus};
pub use error::CoreError;
pub use execution_frames::UiOperationPublishAttempt;
pub use filesystem_registry::{FilesystemRights, FilesystemRoot};
pub use journal::AuditRunJournal;
#[cfg(unix)]
pub use native_declared_artifact::{
    NativeArtifactDeclaration, NativeArtifactError, NativeArtifactInspection,
    NativeDeclaredArtifactSource,
};
pub use process_catalog::ProcessRegistrationError;
pub use profile_review::{
    FilesystemPermissionReview, PermissionReview, ProfileReview, ProfileReviewCapacity,
    ProfileReviewConflict, ProfileReviewError, ProfileReviewIdentity, ProfileReviewPack,
    ProfileReviewPackLimit, ReviewedTarget, SecretPermissionReview, StoragePermissionReview,
    TargetReviewStatus, UserOriginReview,
};
pub use profile_save_review::ProfileSavePrecondition;
pub use runtime_owner::RuntimeOwner;
pub use session::{
    NativeInput, NativeInputCompletion, NativeInputEvent, NativeInputRouteResult,
    NativeInputRouteStatus, NativeInputRunHandle, NativeInputStatus, Session, UI_STREAM_MAX_BATCH,
    UiAttachment, UiCallResult, UiInvokeRequest, UiOperationCloseControl, UiOperationCloseHandle,
    UiStreamCleanupControl, UiStreamCleanupOwner,
};
pub use ui_operation_preparation::{UiOperationPreparation, UiOperationTermination};

/// Immutable verified source bytes retaining their original Core load charge.
pub use tobkiri_manifest::source::SourceBytes as VerifiedSourceBytes;
