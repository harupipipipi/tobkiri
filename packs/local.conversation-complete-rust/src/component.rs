//! The component exposes only DATA operations.

wit_bindgen::generate!({
    path: ["../../crates/tobkiri-wit/wit/core-v1-2/core.wit", "wit"],
    world: "local:conversation-complete/pack@1.0.0",
    generate_all,
});

use exports::local::conversation_complete::data::Guest;
use tobkiri::core::types::{Error, ErrorCode};

struct Pack;

impl exports::tobkiri::core::lifecycle::Guest for Pack {
    fn init(_config: String) -> Result<(), Error> {
        Ok(())
    }

    fn shutdown() {}
}

impl Guest for Pack {
    fn prepare(input: String) -> Result<String, Error> {
        crate::prepare(&input).map_err(component_error)
    }

    fn finish(input: String) -> Result<String, Error> {
        crate::finish(&input).map_err(component_error)
    }
}

fn component_error(error: crate::Error) -> Error {
    Error {
        code: match error {
            crate::Error::Invalid(_) => ErrorCode::Invalid,
            crate::Error::Capacity => ErrorCode::LimitExceeded,
        },
        message: error.to_string(),
    }
}

export!(Pack);
