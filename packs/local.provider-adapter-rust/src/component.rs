wit_bindgen::generate!({
    path: ["../../crates/tobkiri-wit/wit/core-v1-2/core.wit", "wit"],
    world: "local:provider-adapter/pack@1.0.0",
    generate_all,
});

use exports::local::provider_adapter::{local_completion, local_request};
use tobkiri::core::types::{Error, ErrorCode};

use crate::PolicyError;

struct Pack;

impl exports::tobkiri::core::lifecycle::Guest for Pack {
    fn init(_config: String) -> Result<(), Error> {
        Ok(())
    }

    fn shutdown() {}
}

impl local_completion::Guest for Pack {
    fn project(response: String, stream: bool) -> Result<String, Error> {
        Ok(crate::project_local_completion(&response, stream).unwrap_or_default())
    }
}

impl local_request::Guest for Pack {
    fn compile(request: String) -> Result<String, Error> {
        Ok(crate::compile_local_chat_request(&request).unwrap_or_default())
    }

    fn compile_with_policy(request: String, policy: String) -> Result<String, Error> {
        crate::compile_local_chat_request_with_policy(&request, &policy)
            .map(Option::unwrap_or_default)
            .map_err(|error| Error {
                code: match error {
                    PolicyError::Denied => ErrorCode::Denied,
                    PolicyError::InvalidModel | PolicyError::RouteOverride => ErrorCode::Invalid,
                },
                message: error.message().into(),
            })
    }
}

export!(Pack);
