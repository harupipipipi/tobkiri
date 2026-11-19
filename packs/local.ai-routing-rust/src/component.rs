wit_bindgen::generate!({
    path: ["../../crates/tobkiri-wit/wit/core-v1-2/core.wit", "wit"],
    world: "local:ai-routing/pack@1.0.0",
    generate_all,
});

use exports::local::ai_routing::{routing, stream_result, usage_cost};
use tobkiri::core::types::Error;

struct Pack;

impl exports::tobkiri::core::lifecycle::Guest for Pack {
    fn init(_config: String) -> Result<(), Error> {
        Ok(())
    }

    fn shutdown() {}
}

impl routing::Guest for Pack {
    fn evaluate(request: String) -> Result<String, Error> {
        Ok(crate::evaluate(&request).unwrap_or_default())
    }
}

impl stream_result::Guest for Pack {
    fn normalize(request: String) -> Result<String, Error> {
        Ok(crate::normalize_stream_result(&request).unwrap_or_default())
    }
}

impl usage_cost::Guest for Pack {
    fn calculate(request: String) -> Result<String, Error> {
        Ok(crate::calculate_usage_cost(&request).unwrap_or_default())
    }
}

export!(Pack);
