//! Harness tools execute only through Core-gated private storage/clock/random.
pub mod engine;

#[cfg(target_arch = "wasm32")]
mod guest;
