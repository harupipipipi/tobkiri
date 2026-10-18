//! Generic component execution, authority-mediated imports and typed ports.

mod abi;
mod allocation;
mod core_imports;
mod core_source_close;
mod dispatch;
mod effect_values;
mod effects;
mod engine;
mod entry;
mod entry_diagnostics;
pub mod filesystem;
mod generation_retirement;
mod handle_transfer;
mod handles;
mod host;
mod imports;
mod instance_capacity;
mod network;
mod network_diagnostics;
mod output;
mod owned_values;
mod storage_values;
mod values;
mod wasi;
mod wasi_effects;

pub use engine::{InvocationState, WasmEngine};
pub use entry::{
    FlowUserInvocation, FlowUserInvocationPreparation, HandleSourceRequest, NativeInputPreparation,
    ProviderEntryBridge, ProviderFrame, ProviderOrigin, ProviderRequest, UiInvocation,
    UiInvocationPreparation,
};
pub use handles::{HandleEntry, HandleKind, HandleTable};
pub use host::PackHost;
pub use imports::{WASI_SUBSET, validate_imports};
pub use owned_values::ValueOwners as AllocationOwners;
pub use values::{core_to_val, val_to_core};
pub use wasi::{RestrictedWasi, link_restricted_wasi};
