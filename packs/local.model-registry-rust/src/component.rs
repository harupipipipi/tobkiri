//! Registry ports for the authenticated Profile.

#[cfg(test)]
#[path = "component_legacy_list_tests.rs"]
mod legacy_list_tests;

wit_bindgen::generate!({
    path: ["../../crates/tobkiri-wit/wit/core-v1-2/core.wit", "wit"],
    world: "local:model-registry/pack@1.0.0",
    generate_all,
});

use crate::{
    SourceError,
    component_store::DocumentStore,
    presentation::project_list,
    registry::Registry,
    routing,
    settings_save::{captured_save_result, normalize, present_saved},
};
use ordered_python_compat::{CodecError, PyValue, dumps, loads, utc_timestamp_from_millis};
use tobkiri::core::{
    clock, flow,
    types::{Error, ErrorCode},
};

const PROVIDER_RESOURCE: &str = "local.provider-registry.resource@1.0";
const MAX_LEGACY_DOCUMENT_BYTES: usize = 1024 * 1024;

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
        let (_, document) = DocumentStore::read()?;
        list_models(&profile, document)
    }

    fn save_model(payload: String) -> Result<String, Error> {
        let profile = flow::current_profile()?;
        let request = normalize(&loads(&payload).map_err(codec_error)?).map_err(source_error)?;
        validate_provider(&profile, &request)?;
        let (store, document) = DocumentStore::read()?;
        let registry = Registry::new(&profile, document).map_err(source_error)?;
        let now = utc_timestamp_from_millis(clock::now_ms()).ok_or_else(|| {
            failure(
                ErrorCode::Unavailable,
                "model Registry clock is unavailable",
            )
        })?;
        let (updated, result) = registry
            .apply_settings(&request, &now)
            .map_err(source_error)?;
        let captured = captured_save_result(&request, &result).map_err(source_error)?;
        let projected = present_saved(&captured).map_err(source_error)?;
        let reply = encode_reply(&projected, "model Settings response")?;
        if let Some(updated) = updated {
            store.commit(&dumps(&updated).map_err(codec_error)?)?;
        }
        Ok(reply)
    }
}

impl exports::local::model_registry::legacy_settings::Guest for Pack {
    fn list_models(document: Option<String>) -> Result<String, Error> {
        let profile = flow::current_profile()?;
        list_legacy_models(&profile, document.as_deref())
    }
}

impl exports::local::model_registry::routing::Guest for Pack {
    fn resolve_model(identifier: String) -> Result<String, Error> {
        let profile = flow::current_profile()?;
        let (_, document) = DocumentStore::read()?;
        let registry = Registry::new(&profile, document).map_err(source_error)?;
        let route = routing::resolve_model(&registry, identifier).map_err(source_error)?;
        encode_reply(&route, "model route response")
    }
}

export!(Pack);

fn list_legacy_models(profile: &str, document: Option<&str>) -> Result<String, Error> {
    let document = document
        .map(|document| {
            if document.len() > MAX_LEGACY_DOCUMENT_BYTES {
                return Err(failure(
                    ErrorCode::LimitExceeded,
                    "model Registry document exceeds limits",
                ));
            }
            decode_document(document)
        })
        .transpose()?;
    list_models(profile, document)
}

fn list_models(profile: &str, document: Option<PyValue>) -> Result<String, Error> {
    let registry = Registry::new(profile, document).map_err(source_error)?;
    let snapshot = registry.snapshot().map_err(source_error)?;
    let projected = project_list(&snapshot).map_err(source_error)?;
    encode_reply(&projected, "model Settings response")
}

fn validate_provider(profile: &str, request: &PyValue) -> Result<(), Error> {
    let Some((expected, provider)) = request.get("provider_registry_revision").zip(
        request
            .get("record")
            .and_then(|record| record.get("metadata"))
            .and_then(|metadata| metadata.get("provider_connection_id"))
            .and_then(PyValue::as_str),
    ) else {
        return Err(failure(
            ErrorCode::Denied,
            "model route provider binding is invalid",
        ));
    };
    let args = serde_json::json!({"payload":{"profile_id":profile}}).to_string();
    let snapshot = flow::call_port(PROVIDER_RESOURCE, "snapshot", &args)?;
    let snapshot = loads(&snapshot).map_err(codec_error)?;
    if snapshot.get("revision") != Some(expected) {
        return Err(failure(
            ErrorCode::Denied,
            "model route provider connection changed",
        ));
    }
    let providers = snapshot
        .get("providers")
        .and_then(PyValue::as_array)
        .ok_or_else(|| {
            failure(
                ErrorCode::Denied,
                "model route provider registry is invalid",
            )
        })?;
    let matches = providers
        .iter()
        .filter(|row| {
            row.get("provider_instance_id").and_then(PyValue::as_str) == Some(provider)
                && matches!(row.get("enabled"), Some(PyValue::Bool(true)))
        })
        .count();
    if matches != 1 {
        return Err(failure(
            ErrorCode::Denied,
            "model route provider connection is unavailable",
        ));
    }
    Ok(())
}

fn encode_reply(projected: &PyValue, subject: &str) -> Result<String, Error> {
    let encoded = dumps(projected).map_err(codec_error)?;
    tobkiri_canon::parse(encoded.as_bytes(), tobkiri_canon::Variant::StrictV1).map_err(
        |error| match error {
            tobkiri_canon::Error::Limit(_) => failure(
                ErrorCode::LimitExceeded,
                &format!("{subject} exceeds limits"),
            ),
            _ => failure(ErrorCode::Invalid, &format!("{subject} is invalid")),
        },
    )?;
    Ok(encoded)
}

pub(super) fn decode_document(document: &str) -> Result<PyValue, Error> {
    // This private document preserves Python floats and wide integers.
    loads(document).map_err(codec_error)
}

fn source_error(error: SourceError) -> Error {
    match (error.exception_class, error.message.as_str()) {
        ("ValueError", "model registry profile binding is invalid") => {
            failure(ErrorCode::Denied, "model Registry Profile is invalid")
        }
        ("ModelRegistryConflict", _) => failure(ErrorCode::Conflict, "model Registry conflict"),
        ("ModelRouteNotFound", _) => failure(ErrorCode::NotFound, &error.message),
        ("ModelRouteDenied", _) => failure(ErrorCode::Denied, &error.message),
        ("ModelRouteInvalid", _) => failure(ErrorCode::Invalid, &error.message),
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

pub(super) fn failure(code: ErrorCode, message: &str) -> Error {
    Error {
        code,
        message: message.into(),
    }
}
