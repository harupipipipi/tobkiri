wit_bindgen::generate!({
    path: ["../../crates/tobkiri-wit/wit/core-v1-2/core.wit", "wit"],
    world: "local:provider-adapter/pack@1.0.0",
    generate_all,
});

use exports::local::provider_adapter::local_completion::Guest;
use tobkiri::core::types::Error;

struct Pack;

impl exports::tobkiri::core::lifecycle::Guest for Pack {
    fn init(_config: String) -> Result<(), Error> {
        Ok(())
    }

    fn shutdown() {}
}

impl Guest for Pack {
    fn project(response: String, stream: bool) -> Result<String, Error> {
        Ok(crate::project_local_completion(&response, stream).unwrap_or_default())
    }
}

export!(Pack);
