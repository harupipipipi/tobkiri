//! Additive translation of the original Python Provider Registry.
//!
//! The default build provides Registry and configuration logic for caller-supplied
//! ports. The `component` feature wires the Registry to the current execution
//! Profile and its private Core store.

#[cfg(feature = "component")]
mod component;
#[cfg(feature = "component")]
mod component_store;
pub mod configuration;
pub mod failure;
pub mod host;
mod legacy_local_chat;
pub mod record;
pub mod registry;
#[cfg(any(feature = "component", test))]
mod routing;
pub mod state;

pub use legacy_local_chat::{
    LegacyLocalChatSelection, LegacyLocalChatSourceError, resolve_legacy_local_chat_address,
};

#[cfg(test)]
mod oracle_tests;
#[cfg(test)]
mod source_evidence;
