//! Additive translation of the original Python Provider Registry.
//!
//! Registry and configuration logic use caller-supplied decoded store, clock
//! and credential ports. This crate opens no real Profile or credential store
//! and supplies neither production routing nor proof of Rust/Wasm/GUI parity.

pub mod configuration;
pub mod failure;
pub mod host;
pub mod record;
pub mod registry;
pub mod state;

#[cfg(test)]
mod oracle_tests;
