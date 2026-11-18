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
pub mod record;
pub mod registry;
pub mod state;

#[cfg(test)]
mod oracle_tests;
#[cfg(test)]
mod source_evidence;
