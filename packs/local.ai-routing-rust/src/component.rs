wit_bindgen::generate!({
    path: ["../../crates/tobkiri-wit/wit/core-v1-2/core.wit", "wit"],
    world: "local:ai-routing/pack@1.0.0",
    generate_all,
});

use exports::local::ai_routing::routing;
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

export!(Pack);
