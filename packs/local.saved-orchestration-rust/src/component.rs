//! The component exposes a bounded computation with no Host effect imports.

wit_bindgen::generate!({
    path: ["../../crates/tobkiri-wit/wit/core-v1-2/core.wit", "wit"],
    world: "local:saved-orchestration/pack@1.0.0",
    generate_all,
});

use exports::local::saved_orchestration::legacy_step::Guest;
use tobkiri::core::types::{Error, ErrorCode};

struct Pack;

impl exports::tobkiri::core::lifecycle::Guest for Pack {
    fn init(_config: String) -> Result<(), Error> {
        Ok(())
    }

    fn shutdown() {}
}

impl Guest for Pack {
    fn advance(input: String) -> Result<String, Error> {
        crate::advance(&input).map_err(|error| Error {
            code: match error {
                crate::Error::Invalid(_) => ErrorCode::Invalid,
                crate::Error::Capacity => ErrorCode::LimitExceeded,
            },
            message: error.to_string(),
        })
    }
}

export!(Pack);
