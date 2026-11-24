//! Generic component execution, authority-mediated imports and typed ports.

mod abi;
mod aggregate_limits;
mod allocation;
#[cfg(debug_assertions)]
mod allocation_timing;
mod child_census;
mod core_imports;
mod core_modules;
mod dispatch;
mod effect_values;
mod effects;
mod engine;
mod entry;
mod entry_diagnostics;
mod execution_epoch;
pub mod filesystem;
pub use effects::filesystem_imports::FilesystemAccess;
mod handle_metadata;
mod handle_transfer;
mod input_forwarding;
pub use input_forwarding::NativeInputHandles;
mod handles;
mod host;
mod imports;
mod instance_capacity;
mod native_allocation;
mod native_captured_inputs;
pub use native_captured_inputs::{NativeCapturedFile, NativeCapturedInputs};
mod native_preparation;
mod native_setup_store;
pub use native_setup_store::{
    NativeDataBinding, NativeDataEntry, NativeDataError, NativeDataErrorContract, NativeDataExport,
    NativeDataObservation, NativeDataProgram, NativeDataResultContract, NativeDataResultEntry,
    NativeDataSuccessType, NativeDataValue, NativeImportBinding, NativeImportContext,
    NativeImportInput, NativeImportOperation, NativeImportState, NativeSetupStore,
};
mod network;
mod output;
mod owned_values;
mod storage_values;
mod values;
mod wasi;
mod wasi_effects;

/// Collect only physically idle allocation owners, including after the final
/// PackHost is dropped. Live strong/weak users retain their original funding.
pub use allocation::reap_custody as reap_allocation_owners;
pub use child_census::{
    NativeDataIo, NativeDataIoError, NativeExecutionId, NativeInspectionCheckpoint,
    NativeInspectionOutput,
};
pub use engine::{InvocationState, WasmEngine};
pub use entry::{
    CleanupLifecycle, CleanupLifecycleAttempt, FlowUserInvocation, FlowUserInvocationPreparation,
    HandleSourceRequest, NativeIdAttempt, NativeInputPreparation, ProviderBindAttempt,
    ProviderEntryBridge, ProviderFrame, ProviderOrigin, ProviderRequest, UiInvocation,
    UiInvocationPreparation,
};
pub use handles::{HandleEntry, HandleKind, HandleTable};
pub use host::{PackHost, ProviderOperationScope};
pub use imports::{WASI_SUBSET, validate_imports};
pub use owned_values::ValueOwners as AllocationOwners;
pub use values::{core_to_val, val_to_core};
pub use wasi::{RestrictedWasi, link_restricted_wasi};
