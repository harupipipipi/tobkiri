//! Keep the complete legacy document in the Profile's private DATA store.

wit_bindgen::generate!({
    path: ["../../crates/tobkiri-wit/wit/core-v1-2/core.wit", "wit"],
    world: "local:model-registry/pack@1.0.0",
    generate_all,
});

use crate::{SourceError, presentation::project_list, registry::Registry};
use ordered_python_compat::{CodecError, PyValue, dumps, loads};
use tobkiri::core::{
    flow, storage,
    types::{Error, ErrorCode},
};

const STORE: &str = "model-registry";
const DOCUMENT: &str = "document";

struct Pack;

impl exports::tobkiri::core::lifecycle::Guest for Pack {
    fn init(_config: String) -> Result<(), Error> {
        Ok(())
    }

    fn shutdown() {}
}

impl exports::local::model_registry::settings::Guest for Pack {
    fn list_models() -> Result<String, Error> {
        let profile = flow::current_profile()?;
        let store = storage::open(STORE)?;
        let document = store
            .kv_get(DOCUMENT)?
            .as_deref()
            .map(decode_document)
            .transpose()?;
        let registry = Registry::new(&profile, document).map_err(source_error)?;
        let snapshot = registry.snapshot().map_err(source_error)?;
        let projected = project_list(&snapshot).map_err(source_error)?;
        let encoded = dumps(&projected).map_err(codec_error)?;
        tobkiri_canon::parse(encoded.as_bytes(), tobkiri_canon::Variant::StrictV1).map_err(
            |error| match error {
                tobkiri_canon::Error::Limit(_) => failure(
                    ErrorCode::LimitExceeded,
                    "model Settings response exceeds limits",
                ),
                _ => failure(ErrorCode::Invalid, "model Settings response is invalid"),
            },
        )?;
        Ok(encoded)
    }
}

export!(Pack);

fn decode_document(stored: &[u8]) -> Result<PyValue, Error> {
    // The DATA value is a JSON string. Keep the inner document's Python
    // numeric values opaque to Core JSON, including floats and wide integers.
    let document: String = serde_json::from_slice(stored)
        .map_err(|_| failure(ErrorCode::Invalid, "model Registry DATA is invalid"))?;
    loads(&document).map_err(codec_error)
}

fn source_error(error: SourceError) -> Error {
    match (error.exception_class, error.message.as_str()) {
        ("ValueError", "model registry profile binding is invalid") => {
            failure(ErrorCode::Denied, "model Registry Profile is invalid")
        }
        _ => failure(ErrorCode::Invalid, "model Registry document is invalid"),
    }
}

fn codec_error(error: CodecError) -> Error {
    match error {
        CodecError::Invalid | CodecError::NonFinite => {
            failure(ErrorCode::Invalid, "model Registry JSON is invalid")
        }
        _ => failure(
            ErrorCode::LimitExceeded,
            "model Registry JSON exceeds limits",
        ),
    }
}

fn failure(code: ErrorCode, message: &str) -> Error {
    Error {
        code,
        message: message.into(),
    }
}
