//! Additive translation of the original Python Provider Registry.
//!
//! The pure helpers do not open a profile, store, credential or network port.
//! They supply neither production routing nor proof of Rust/Wasm parity.

pub mod failure;
pub mod host;
pub mod record;
pub mod registry;
pub mod state;

#[cfg(test)]
mod oracle_tests;
