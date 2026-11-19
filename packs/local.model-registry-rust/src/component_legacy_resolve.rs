//! Private owner resolution over the original Profile's Registry.

use super::{
    MAX_LEGACY_DOCUMENT_BYTES, codec_error, failure, legacy_codec_required, source_error,
    tobkiri::core::types::{Error, ErrorCode},
};
use crate::registry::{Registry, identifier, object};
use ordered_python_compat::{CodecError, PyValue, dumps_bounded, loads, loads_python_json};

// The original decoder has interpreter- and caller-dependent recursion
// headroom. Leave deeper cases to the original owner, as for Conversation reads.
const NATIVE_CONTAINER_DEPTH: usize = 256;

pub(super) fn resolve(
    profile: &str,
    document: Option<&str>,
    requested: String,
) -> Result<String, Error> {
    let mut registry = Registry::new(profile, None).map_err(source_error)?;
    let requested = PyValue::String(requested);
    // The old owner validates its argument before decoding the store.
    identifier(&requested).map_err(|error| failure(ErrorCode::Invalid, &error.message))?;
    if let Some(document) = document {
        registry.document = decode_document(&registry, document)?;
    }
    if !within_container_depth(&registry.document, NATIVE_CONTAINER_DEPTH) {
        registry.read().map_err(source_error)?;
        return Err(legacy_codec_required());
    }
    let resolved = registry.resolve(&requested).map_err(source_error)?;
    if matches!(resolved, PyValue::Null) {
        return Err(failure(
            ErrorCode::NotFound,
            "model profile or alias is unknown",
        ));
    }

    // Keep owner metadata, floats, wide integers, and field order private
    // inside the JSON string so Core only interprets the outer object.
    let owner = dumps_bounded(&resolved, MAX_LEGACY_DOCUMENT_BYTES).map_err(codec_error)?;
    let reply = object([("owner_result", PyValue::String(owner))]);
    dumps_bounded(&reply, MAX_LEGACY_DOCUMENT_BYTES).map_err(codec_error)
}

fn decode_document(registry: &Registry, document: &str) -> Result<PyValue, Error> {
    if document.len() > MAX_LEGACY_DOCUMENT_BYTES {
        return Err(failure(
            ErrorCode::LimitExceeded,
            "model Registry document exceeds limits",
        ));
    }
    match loads(document) {
        Ok(document) => Ok(document),
        Err(CodecError::Invalid | CodecError::NonFinite) => {
            let document = loads_python_json(document).map_err(codec_error)?;
            registry
                .validate_legacy_document(&document)
                .map_err(source_error)?;
            Err(legacy_codec_required())
        }
        Err(error) => Err(codec_error(error)),
    }
}

fn within_container_depth(value: &PyValue, remaining: usize) -> bool {
    match value {
        PyValue::Array(values) => {
            remaining > 0
                && values
                    .iter()
                    .all(|value| within_container_depth(value, remaining - 1))
        }
        PyValue::Object(fields) => {
            remaining > 0
                && fields
                    .iter()
                    .all(|(_, value)| within_container_depth(value, remaining - 1))
        }
        _ => true,
    }
}

#[cfg(test)]
#[path = "component_legacy_resolve_tests.rs"]
mod tests;
