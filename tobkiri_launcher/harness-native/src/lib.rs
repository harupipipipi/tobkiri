//! Harness nativeKit DATA adapters. No native Python runtime or effect authority.
//!
//! These inactive adapters do not replace the existing Launcher flow. The
//! complete original Setup, native inspection, approval and conversation paths
//! must pass parity before the product default changes.

mod exception_graph;
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
