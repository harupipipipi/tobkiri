//! Independent Rust/Wasm candidate for the original pure usage-cost projection.
//! The existing WIT identity is a compatibility surface, not a Python runtime
//! claim. DATA coercion, ordering and encoding belong to the shared compat crate.

wit_bindgen::generate!({
    path: [
        "../../crates/tobkiri-wit/wit/core/core.wit",
        "../local.ai-usage-cost-python/wit",
    ],
    world: "local:ai-usage-cost-python/pack@1.0.0",
    generate_all,
});

use ordered_python_compat::MAX_BYTES;
use tobkiri::core::types::{Error, ErrorCode};
pub use tobkiri_ai_usage_cost_core::ProviderFailure;

/// Pure DATA adapter: one projection, no Host, billing, fallback or authority.
pub fn invoke(operation: &str, payload: &str) -> Result<String, ProviderFailure> {
    tobkiri_ai_usage_cost_core::invoke(operation, payload, MAX_BYTES)
}

fn into_wit(failure: ProviderFailure) -> Error {
    Error {
        code: match failure {
            ProviderFailure::Input => ErrorCode::Invalid,
            ProviderFailure::Validation | ProviderFailure::Arithmetic | ProviderFailure::Output => {
                ErrorCode::Internal
            }
            ProviderFailure::Resource => ErrorCode::LimitExceeded,
        },
        message: failure.message().to_owned(),
    }
}

const RUNTIME_INFO: &str = concat!(
    "{\"implementation\":\"Rust\",\"candidate\":\"local.ai-usage-cost-rust\",",
    "\"version\":\"",
    env!("CARGO_PKG_VERSION"),
    "\",\"protocol\":\"ordered-finite-json-v2\"}"
);

struct Pack;

impl exports::tobkiri::core::lifecycle::Guest for Pack {
    fn init(_config: String) -> Result<(), Error> {
        Ok(())
    }
    fn shutdown() {}
}

impl exports::local::ai_usage_cost_python::usage_cost::Guest for Pack {
    fn runtime_info() -> String {
        RUNTIME_INFO.to_owned()
    }
    fn calculate(operation: String, payload: String) -> Result<String, Error> {
        invoke(&operation, &payload).map_err(into_wit)
    }
}

#[cfg(target_arch = "wasm32")]
export!(Pack);
