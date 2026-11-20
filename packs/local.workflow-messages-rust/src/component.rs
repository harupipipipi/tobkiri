//! Pure DATA export for the Workflow message stage.

wit_bindgen::generate!({
    path: ["../../crates/tobkiri-wit/wit/core-v1-2/core.wit", "wit"],
    world: "local:workflow-messages/pack@1.0.0",
    generate_all,
});

use exports::local::workflow_messages::data::Guest;
use tobkiri::core::types::{Error, ErrorCode};

struct Pack;

impl exports::tobkiri::core::lifecycle::Guest for Pack {
    fn init(_config: String) -> Result<(), Error> {
        Ok(())
    }

    fn shutdown() {}
}

impl Guest for Pack {
    fn build(input: String) -> Result<String, Error> {
        crate::build(&input).map_err(|error| Error {
            code: match error {
                crate::Error::Invalid(_) | crate::Error::Canonical(_) => ErrorCode::Invalid,
                crate::Error::Capacity(_) => ErrorCode::LimitExceeded,
            },
            message: error.to_string(),
        })
    }
}

export!(Pack);
