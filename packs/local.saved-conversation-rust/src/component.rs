//! Conversation owner ports for the admitted Profile.

wit_bindgen::generate!({
    path: ["../../crates/tobkiri-wit/wit/core-v1-2/core.wit", "wit"],
    world: "local:saved-conversation/pack@1.0.0",
    generate_all,
});

use crate::{
    legacy_confirmation::{self, ConfirmationError},
    legacy_message_append::{self, AppendError},
    legacy_read::{self, ReadError},
    saved_model::{SelectionError, select},
};
use ordered_python_compat::{CodecError, loads};
use tobkiri::core::{
    clock, flow, storage,
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

impl exports::local::saved_conversation::legacy_read::Guest for Pack {
    fn get(document: Option<String>, conversation_id: String) -> Result<String, Error> {
        let profile = flow::current_profile()?;
        legacy_read::get(&profile, document.as_deref(), &conversation_id).map_err(read_error)
    }
}

impl exports::local::saved_conversation::legacy_linked_context::Guest for Pack {
    fn get(document: Option<String>, conversation_id: String) -> Result<String, Error> {
        let profile = flow::current_profile()?;
        legacy_read::get_linked(&profile, document.as_deref(), &conversation_id).map_err(read_error)
    }
}

impl exports::local::saved_conversation::legacy_receipt::Guest for Pack {
    fn get(document: Option<String>, turn_id: String) -> Result<String, Error> {
        let profile = flow::current_profile()?;
        legacy_read::saved_receipt(&profile, document.as_deref(), &turn_id).map_err(read_error)
    }
}

impl exports::local::saved_conversation::legacy_confirmation::Guest for Pack {
    fn confirm(document: String, turn: String) -> Result<String, Error> {
        let profile = flow::current_profile()?;
        legacy_confirmation::confirm(&profile, &document, &turn).map_err(confirmation_error)
    }
}

impl exports::local::saved_conversation::legacy_append::Guest for Pack {
    fn append(document: String, request: String) -> Result<String, Error> {
        let profile = flow::current_profile()?;
        legacy_message_append::append(&profile, &document, &request, || {
            i64::try_from(clock::now_ms()).map_err(|_| AppendError::Limit)
        })
        .map_err(append_error)
    }
}

export!(Pack);

fn append_error(error: AppendError) -> Error {
    match error {
        AppendError::Read(error) => read_error(error),
        AppendError::Invalid => failure(ErrorCode::Invalid, "saved message append is invalid"),
        AppendError::Conflict => failure(ErrorCode::Conflict, "saved message revision changed"),
        AppendError::NotFound => {
            failure(ErrorCode::NotFound, "saved message context is unavailable")
        }
        AppendError::Compatibility => failure(
            ErrorCode::Unavailable,
            "saved append needs the original Conversation owner",
        ),
        AppendError::Limit => failure(
            ErrorCode::LimitExceeded,
            "saved message append exceeds limits",
        ),
    }
}

fn confirmation_error(error: ConfirmationError) -> Error {
    match error {
        ConfirmationError::Read(error) => read_error(error),
        ConfirmationError::Terminal => failure(
            ErrorCode::Denied,
            "saved completion requires a durable terminal",
        ),
        ConfirmationError::Receipt => {
            failure(ErrorCode::Denied, "saved completion receipt does not match")
        }
        ConfirmationError::Compatibility => failure(
            ErrorCode::Unavailable,
            "saved completion needs the original Conversation owner",
        ),
        ConfirmationError::Limit => {
            failure(ErrorCode::LimitExceeded, "saved completion exceeds limits")
        }
    }
}

fn read_error(error: ReadError) -> Error {
    match error {
        ReadError::InvalidIdentifier => failure(ErrorCode::Invalid, "conversation id is invalid"),
        ReadError::InvalidDocument => failure(ErrorCode::Invalid, "conversation store is invalid"),
        ReadError::Profile => failure(ErrorCode::Denied, "saved conversation Profile is invalid"),
        ReadError::NotFound => failure(ErrorCode::NotFound, "conversation is unavailable"),
        ReadError::Limits => failure(
            ErrorCode::LimitExceeded,
            "saved conversation exceeds limits",
        ),
        ReadError::LegacyCodec => failure(
            ErrorCode::Unavailable,
            "saved conversation needs the legacy JSON codec",
        ),
        ReadError::LegacyReceiptInventory => failure(
            ErrorCode::Unavailable,
            "saved receipt inventory needs the original Conversation reader",
        ),
        ReadError::Internal => failure(ErrorCode::Internal, "saved conversation read failed"),
    }
}

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
