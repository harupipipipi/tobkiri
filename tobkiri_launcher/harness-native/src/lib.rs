//! Harness nativeKit DATA adapters. No native Python runtime or effect authority.
//!
//! These inactive adapters do not replace the existing Launcher flow. The
//! complete original Setup, native inspection, approval and conversation paths
//! must pass parity before the product default changes.

mod bootstrap_metadata;
mod bootstrap_review;
mod bootstrap_runtime;
pub use bootstrap_review::{
    BootstrapCatalogSource, BootstrapReviewCleanupFailure, BootstrapReviewFailure,
    BootstrapReviewOptions, BootstrapReviewOwner, BootstrapReviewReachpoint,
};
mod exception_graph;
mod init_diagnostic;
mod init_trace;
mod original_authority;
#[cfg(unix)]
mod original_local_chat;
#[cfg(unix)]
mod original_provider_prepare_selection;
#[cfg(unix)]
mod original_saved_orchestration;
#[cfg(unix)]
pub use original_local_chat::{
    LegacyLocalChatSelection, LegacyLocalChatSourceError, OriginalLocalChatCheck,
    OriginalLocalChatError, OriginalLocalChatPolicy, OriginalLocalChatPolicyError,
    OriginalLocalChatRequest, compile_original_local_chat, observe_original_local_chat_policy,
    post_original_local_chat, project_original_local_chat,
};
#[cfg(unix)]
pub use original_saved_orchestration::{
    SavedOrchestrationStepError, invoke_original_saved_orchestration_port,
};
#[cfg(unix)]
mod original_saved_turn_settlement;
#[cfg(unix)]
pub use original_saved_turn_settlement::{
    LegacySavedTurnSettlementError, LegacySavedTurnSettlementRequest, LegacySavedTurnStartRequest,
    OriginalSavedTurnCheck, OriginalSavedTurnDecision, OriginalSavedTurnRow,
    OriginalSavedTurnStartInput, OriginalSavedTurnStartOutcome, OriginalSavedTurnStartStep,
    begin_legacy_saved_turn_from_admitted_port, claim_legacy_saved_turn_from_admitted_port,
    invoke_original_saved_turn_begin_port, invoke_original_saved_turn_claim_port,
    invoke_original_saved_turn_port, invoke_original_saved_turn_read_port, read_legacy_saved_turn,
    settle_legacy_saved_turn, settle_legacy_saved_turn_from_admitted_port,
};
mod preparation;
#[cfg(unix)]
pub use original_authority::{OriginalActivationInspection, OriginalActiveProfile};
pub use original_authority::{
    OriginalAuthorityAdmission, OriginalAuthorityCause, OriginalAuthorityCleanup,
    OriginalAuthorityCloseReceipt, OriginalAuthorityEpoch, OriginalAuthorityError,
    OriginalAuthorityIoError, OriginalAuthorityMaterial, OriginalAuthorityOwner,
    OriginalAuthorityRead, OriginalAuthoritySnapshot, OriginalAuthorityStage,
    read_original_authority,
};
#[cfg(unix)]
pub use original_provider_prepare_selection::{
    OriginalAiRoutingSelection, OriginalConversationAppendChoice,
    OriginalConversationConfirmationChoice, OriginalConversationReadSelection,
    OriginalModelResolveRole, OriginalModelResolveSelection, OriginalModelSaveSelection,
    OriginalModelSettingsSelection, OriginalProviderPrepareSelection, OriginalRoutePin,
    OriginalRoutePin as OriginalProviderPreparePin, OriginalRouteSelectionError,
    OriginalRouteSelectionError as OriginalModelResolveSelectionError,
    OriginalRouteSelectionError as OriginalModelSaveSelectionError,
    OriginalRouteSelectionError as OriginalModelSettingsSelectionError,
    OriginalRouteSelectionError as OriginalProviderPrepareSelectionError,
    OriginalSavedLocalChatChoice, OriginalSavedLocalChatRole, OriginalSavedOrchestrationChoice,
    OriginalSavedReceiptRole, OriginalSavedReceiptSelection, OriginalSavedTurnSettlementChoice,
    OriginalSavedTurnSettlementRole, OriginalSavedTurnStartChoice, OriginalStreamResultSelection,
    OriginalUsageCostChoice, select_original_saved_turn_settlement,
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
    BootstrapCatalogDelivery, BootstrapInputOwner, BootstrapModule, BootstrapModuleIndex,
    BootstrapProfileSchemas, BootstrapRoots,
};
pub use bootstrap_runtime::{
    BootstrapActivationCandidate, BootstrapActivationDecision, BootstrapBuiltinKind,
    BootstrapCleanupFailure, BootstrapDenied, BootstrapDeniedFailure, BootstrapDeniedRaise,
    BootstrapDeniedStage, BootstrapDomainCleanupFailure, BootstrapGetterError,
    BootstrapIdentityFailure, BootstrapIdentityOutcome, BootstrapIdentityStage,
    BootstrapIdentityValue, BootstrapRecoveryCatch, NativeBootstrap,
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
