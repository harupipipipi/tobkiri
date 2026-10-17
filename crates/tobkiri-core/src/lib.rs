//! In-process Core and Session facade shared by desktop and terminal hosts.

pub use tobkiri_canon::{
    AllocationBudget, AllocationError, AllocationKind, AllocationRequest, CanonValue, CoreBuffer,
    CoreBufferBuilder, CoreRef, CoreValue, Digest, MAX_BYTES, Variant, digest, encode, ids, parse,
};
pub use tobkiri_flow::{CoreStream, RunEvent, RunEventKind, RunHandle, StepError};
pub use tobkiri_gate::{
    ApprovalAnswer, ApprovalPrompt, EffectRequest, HostError, HostIo, PermissionSet, Risk,
    TrustedPrompt, UiEmbed,
};
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
mod core;
mod error;
mod execution_frames;
mod housekeeping;
mod inputs;
mod journal;
mod outcomes;
#[cfg(test)]
mod policy_tests;
mod profiles;
mod session;
#[cfg(test)]
mod test_support;
mod tracked_stream;

pub use core::{Core, CoreConfig, ProfileRecoveryStatus};
pub use error::CoreError;
pub use journal::AuditRunJournal;
pub use session::{
    NativeInput, NativeInputCompletion, NativeInputEvent, NativeInputRouteResult,
    NativeInputRouteStatus, NativeInputRunHandle, NativeInputStatus, Session, UI_STREAM_MAX_BATCH,
    UiAttachment, UiCallResult, UserPortOutput, UserStreamHandle,
};
