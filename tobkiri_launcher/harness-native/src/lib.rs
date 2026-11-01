//! Harness nativeKit DATA adapters. No native Python runtime or effect authority.
//!
//! These inactive adapters do not replace the existing Launcher flow. The
//! complete original Setup, native inspection, approval and conversation paths
//! must pass parity before the product default changes.

mod bootstrap_metadata;
mod bootstrap_review;
mod bootstrap_runtime;
pub use bootstrap_review::{
    BootstrapReviewCleanupFailure, BootstrapReviewFailure, BootstrapReviewOptions,
    BootstrapReviewOwner, BootstrapReviewReachpoint,
};
mod exception_graph;
mod init_diagnostic;
mod init_trace;
mod original_authority;
mod preparation;
pub use original_authority::{
    OriginalAuthorityAdmission, OriginalAuthorityCause, OriginalAuthorityCleanup,
    OriginalAuthorityCloseReceipt, OriginalAuthorityEpoch, OriginalAuthorityError,
    OriginalAuthorityIoError, OriginalAuthorityMaterial, OriginalAuthorityOwner,
    OriginalAuthorityRead, OriginalAuthoritySnapshot, OriginalAuthorityStage,
    read_original_authority,
};
mod source_metadata;
#[cfg(target_os = "macos")]
mod source_resolve;
#[cfg(all(
    test,
    feature = "thread-capabilities-ci",
    target_os = "macos",
    target_arch = "aarch64"
))]
mod thread_capabilities_actual_ci;
pub use bootstrap_metadata::{
    BootstrapInputOwner, BootstrapModule, BootstrapModuleIndex, BootstrapRoots,
};
pub use bootstrap_runtime::{
    BootstrapBuiltinKind, BootstrapCleanupFailure, BootstrapDenied, BootstrapDeniedFailure,
    BootstrapDeniedRaise, BootstrapDeniedStage, BootstrapDomainCleanupFailure,
    BootstrapGetterError, BootstrapIdentityFailure, BootstrapIdentityOutcome,
    BootstrapIdentityStage, BootstrapIdentityValue, BootstrapRecoveryCatch, NativeBootstrap,
};
pub use init_diagnostic::OriginalInitializationFailure;
pub use init_trace::{InitializationRejection, InitializationTrace};
pub use preparation::{
    NativeComponentOwner, NativeComponentPreparation, NativePreparationObservation,
    PreparedNativeComponent, PreparedNativeDataProgram,
};
pub use source_metadata::{ModuleMetadataPolicy, ORIGINAL_PROFILE_SOURCE_METADATA};
pub use tobkiri_canon::ControlHandle as OriginalSourceHandle;
pub use wasmtime::component::Component as NativeHarnessComponent;
#[cfg(unix)]
mod fd_protocol;
#[cfg(unix)]
mod fd_read;
#[cfg(unix)]
mod fd_stat;
pub use exception_graph::{
    NativeErrorGraph, NativeErrorGraphError, NativeErrorKind, NativeErrorNode,
};
#[cfg(unix)]
pub use fd_protocol::{NativeFdOutcome, NativeFdProtocol};
#[cfg(unix)]
pub use fd_read::write_fd_read;
#[cfg(unix)]
pub use fd_stat::write_fd_stat;

// Generic Unicode and the separate migration App policy share one implementation.
// This alone does not adopt a product Setup/GUI route.
mod unicode15_1;
mod unicode15_1_data;
// Original App policy stays separate from generic Unicode/Core mechanisms.
mod app_bootstrap_identity;
