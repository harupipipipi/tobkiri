//! Read the model reference from the admitted Profile's conversation DATA.

wit_bindgen::generate!({
    path: ["../../crates/tobkiri-wit/wit/core-v1-2/core.wit", "wit"],
    world: "local:saved-conversation/pack@1.0.0",
    generate_all,
});

use crate::saved_model::{SelectionError, select};
use ordered_python_compat::{CodecError, loads};
use tobkiri::core::{
    flow, storage,
    types::{Error, ErrorCode},
};

struct Pack;

impl exports::tobkiri::core::lifecycle::Guest for Pack {
    fn init(_config: String) -> Result<(), Error> {
        Ok(())
    }

    fn shutdown() {}
}

impl exports::local::saved_conversation::model::Guest for Pack {
    fn select(conversation_id: String, conversation_revision: u64) -> Result<String, Error> {
        let profile = flow::current_profile()?;
        let store = storage::open("saved-conversation")?;
        let stored = store
            .kv_get("document")?
            .ok_or_else(|| failure(ErrorCode::NotFound, "saved conversation is unavailable"))?;
        let text: String = serde_json::from_slice(&stored)
            .map_err(|_| failure(ErrorCode::Invalid, "saved conversation DATA is invalid"))?;
        let document = loads(&text).map_err(|error| match error {
            CodecError::Invalid | CodecError::NonFinite => {
                failure(ErrorCode::Invalid, "saved conversation DATA is invalid")
            }
            _ => failure(
                ErrorCode::LimitExceeded,
                "saved conversation DATA exceeds limits",
            ),
        })?;
        select(&document, &profile, &conversation_id, conversation_revision)
            .map(str::to_owned)
            .map_err(selection_error)
    }
}

export!(Pack);

fn selection_error(error: SelectionError) -> Error {
    match error {
        SelectionError::Invalid => {
            failure(ErrorCode::Invalid, "saved conversation model is invalid")
        }
        SelectionError::NotFound => {
            failure(ErrorCode::NotFound, "saved conversation is unavailable")
        }
        SelectionError::Stale => {
            failure(ErrorCode::Conflict, "saved conversation revision changed")
        }
        SelectionError::Profile => {
            failure(ErrorCode::Denied, "saved conversation Profile is invalid")
        }
        SelectionError::Context => failure(
            ErrorCode::Denied,
            "saved conversation parent context needs resolution",
        ),
    }
}

fn failure(code: ErrorCode, message: &str) -> Error {
    Error {
        code,
        message: message.into(),
    }
}
