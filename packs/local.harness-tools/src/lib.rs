//! Harness tools execute only through Core-gated private storage/clock/random.
pub mod engine;

#[cfg(any(target_arch = "wasm32", test))]
mod sql_wire;

#[cfg(target_arch = "wasm32")]
mod guest;
