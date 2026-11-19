//! The production port has no access to host effects.

wit_bindgen::generate!({
    path: ["../../crates/tobkiri-wit/wit/core-v1-2/core.wit", "registry-wit"],
    world: "local:application-presentation/pack@1.0.0",
    generate_all,
});

use exports::local::application_presentation::presentation::Guest;
use tobkiri::core::types::{Error, ErrorCode};

struct Pack;

impl exports::tobkiri::core::lifecycle::Guest for Pack {
    fn init(_config: String) -> Result<(), Error> {
        Ok(())
    }

    fn shutdown() {}
}

impl Guest for Pack {
    fn read(input: String) -> Result<String, Error> {
        crate::invoke(crate::OPERATION, &input).map_err(|message| Error {
            code: if message == crate::INPUT_TOO_LARGE {
                ErrorCode::LimitExceeded
            } else {
                ErrorCode::Invalid
            },
            message: message.to_owned(),
        })
    }
}

export!(Pack);
