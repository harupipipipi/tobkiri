//! Harness nativeKit DATA adapters. No native Python runtime or effect authority.
//!
//! These inactive adapters do not replace the existing Launcher flow. The
//! complete original Setup, native inspection, approval and conversation paths
//! must pass parity before the product default changes.

mod bootstrap_metadata;
mod bootstrap_runtime;
mod exception_graph;
mod init_diagnostic;
mod init_trace;
mod source_metadata;
#[cfg(target_os = "macos")]
mod source_resolve;
pub use bootstrap_metadata::{
    BootstrapInputOwner, BootstrapModule, BootstrapModuleIndex, BootstrapRoots,
};
pub use bootstrap_runtime::{BootstrapCleanupFailure, NativeBootstrap};
pub use init_diagnostic::OriginalInitializationFailure;
pub use init_trace::{InitializationRejection, InitializationTrace};
pub use source_metadata::{ModuleMetadataPolicy, ORIGINAL_PROFILE_SOURCE_METADATA};
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
