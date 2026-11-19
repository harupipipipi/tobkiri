//! Additive Rust candidate for the original pure tool bridge's v2 adapter.
//! The original v2 WIT identity is an ABI, while runtime-info names Rust.

wit_bindgen::generate!({
    path: [
        "../../crates/tobkiri-wit/wit/core/core.wit",
        "../local.ai-tool-bridge-ordered-python/wit/pack-v2.wit",
    ],
    world: "local:ai-tool-bridge-ordered-python/pack@2.0.0",
    generate_all,
});

use tobkiri::core::types::{Error, ErrorCode};
pub use tobkiri_ai_tool_bridge_core::{ProviderFailure, invoke};

/// Report the unchanged runtime identity of the standalone v2 adapter.
pub fn runtime_info_data() -> String {
    r#"{"implementation":"rust","pack_id":"local.ai-tool-bridge-rust","protocol":"ordered-finite-json-v2"}"#.into()
}

struct Pack;
impl exports::tobkiri::core::lifecycle::Guest for Pack {
    fn init(_config: String) -> Result<(), Error> {
        Ok(())
    }
    fn shutdown() {}
}
impl exports::local::ai_tool_bridge_ordered_python::bridge::Guest for Pack {
    fn runtime_info() -> String {
        runtime_info_data()
    }
    fn normalize(operation: String, payload: String) -> Result<String, Error> {
        invoke(&operation, &payload).map_err(|failure| Error {
            code: match failure {
                ProviderFailure::Invalid => ErrorCode::Invalid,
                ProviderFailure::NonFinite => ErrorCode::Internal,
                ProviderFailure::Resource => ErrorCode::LimitExceeded,
            },
            message: failure.message().into(),
        })
    }
}

#[cfg(target_arch = "wasm32")]
export!(Pack);

#[cfg(test)]
mod tests {
    #[test]
    fn v2_runtime_identity_is_rust() {
        let runtime: serde_json::Value = serde_json::from_str(&super::runtime_info_data()).unwrap();
        assert_eq!(runtime["implementation"], "rust");
        assert!(runtime.get("python").is_none());
    }
}
