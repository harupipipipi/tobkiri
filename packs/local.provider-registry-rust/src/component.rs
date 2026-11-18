//! Wasm boundary for Registry operations under Core authority.

wit_bindgen::generate!({
    path: ["../../crates/tobkiri-wit/wit/core-v1-2/core.wit", "wit"],
    world: "local:provider-registry/pack@1.0.0",
    generate_all,
});

use crate::{
    component_store::SqlDocumentStore,
    configuration::{configured_result, prepare_configuration, stage_local_configuration},
    failure::{SourceError, SourceResult},
    host::{MANAGE_OPERATION, READONLY_RESOURCE_OPERATION, invoke_manage, invoke_readonly},
    registry::Registry,
    routing,
    state::{Clock, DocumentStore, object, put, text},
};
use exports::local::provider_registry::configuration::LocalConnection;
use ordered_python_compat::{CodecError, PyValue, dumps, loads};
use tobkiri::core::{
    clock, flow,
    types::{Error, ErrorCode},
};

struct Pack;

impl exports::tobkiri::core::lifecycle::Guest for Pack {
    fn init(_config: String) -> Result<(), Error> {
        Ok(())
    }

    fn shutdown() {}
}

impl exports::local::provider_registry::resource::Guest for Pack {
    fn snapshot(payload: String) -> Result<String, Error> {
        let BoundRequest {
            mut registry,
            profile,
            payload,
        } = BoundRequest::open(&payload)?;
        let reply = invoke_readonly(
            &mut registry,
            &profile,
            READONLY_RESOURCE_OPERATION,
            &payload,
        )
        .map_err(source_error)?;
        encode_reply(&reply)
    }
}

impl exports::local::provider_registry::manage::Guest for Pack {
    fn apply(payload: String) -> Result<String, Error> {
        let BoundRequest {
            mut registry,
            profile,
            payload,
        } = BoundRequest::open(&payload)?;
        let reply = invoke_manage(&mut registry, &profile, MANAGE_OPERATION, &payload)
            .map_err(source_error)?;
        // The pure Registry has staged its update. A reply outside Core's JSON
        // domain must fail before the database compare-and-swap.
        let reply = encode_reply(&reply)?;
        registry.store_mut().commit().map_err(source_error)?;
        Ok(reply)
    }
}

impl exports::local::provider_registry::routing::Guest for Pack {
    fn resolve_local(provider: String) -> Result<String, Error> {
        let mut registry = open_registry(&current_profile()?)?;
        let route = routing::resolve_local(&mut registry, &provider).map_err(source_error)?;
        encode_reply(&route)
    }
}

impl exports::local::provider_registry::configuration::Guest for Pack {
    fn prepare_local(request: LocalConnection) -> Result<String, Error> {
        let mut registry = open_registry(&current_profile()?)?;
        let payload = local_request(request);
        let plan = prepare_configuration(&mut registry, &payload).map_err(source_error)?;
        encode_reply(&plan)
    }
}

impl exports::local::provider_registry::configuration_commit::Guest for Pack {
    fn execute_local(request: LocalConnection, plan_digest: String) -> Result<String, Error> {
        let expected = tobkiri_canon::Digest::parse(&plan_digest).map_err(|_| {
            boundary_error(
                ErrorCode::Invalid,
                "provider configuration digest is invalid",
            )
        })?;
        let mut registry = open_registry(&current_profile()?)?;
        let payload = local_request(request);
        let plan = prepare_configuration(&mut registry, &payload).map_err(source_error)?;
        let plan_bytes = dumps(&plan).map_err(codec_error)?;
        let canonical = core_json(&plan_bytes)?;
        let current = tobkiri_canon::digest(&canonical, tobkiri_canon::Variant::StrictV1)
            .map_err(canon_error)?;
        if current != expected {
            return Err(boundary_error(
                ErrorCode::Denied,
                "provider configuration changed after preparation",
            ));
        }
        let saved =
            stage_local_configuration(&mut registry, &payload, &plan).map_err(source_error)?;
        // Check the new record and revision, as in manage.apply, before
        // dispatching the consumed pending write.
        encode_reply(&saved)?;
        let reply = encode_reply(&configured_result(
            saved.get("provider").expect("a staged save has a provider"),
        ))?;
        registry.store_mut().commit().map_err(source_error)?;
        Ok(reply)
    }
}

impl exports::local::provider_registry::legacy_configuration::Guest for Pack {
    fn prepare_local(
        request: LocalConnection,
        snapshot: Option<String>,
        preflight: Option<String>,
    ) -> Result<String, Error> {
        let observed = [snapshot.as_deref(), preflight.as_deref()].into_iter();
        let profile = current_profile()?;
        let mut registry = Registry::new(&text(profile), ObservedRegistry(observed), CoreClock)
            .map_err(source_error)?;
        let plan =
            prepare_configuration(&mut registry, &local_request(request)).map_err(source_error)?;
        encode_reply(&plan)
    }
}

struct ObservedRegistry<'a>(std::array::IntoIter<Option<&'a str>, 2>);

impl DocumentStore for ObservedRegistry<'_> {
    fn read(&mut self) -> SourceResult<Option<PyValue>> {
        // Same byte ceiling as the current Registry's private SQL document.
        const MAX_DOCUMENT_BYTES: usize = 1024 * 1024;
        self.0
            .next()
            .ok_or_else(|| SourceError::value("provider registry observation missing"))?
            .map(|raw| {
                if raw.len() > MAX_DOCUMENT_BYTES {
                    return Err(SourceError::value(
                        "provider registry observation is too large",
                    ));
                }
                loads(raw)
                    .map_err(|_| SourceError::value("provider registry observation is invalid"))
            })
            .transpose()
    }

    fn write(&mut self, _: &PyValue) -> SourceResult<()> {
        Err(SourceError::permission(
            "provider registry observations are read-only",
        ))
    }
}

fn local_request(request: LocalConnection) -> PyValue {
    let mut payload = object([
        ("connection_name", PyValue::String(request.connection_name)),
        ("endpoint", PyValue::String(request.endpoint)),
        ("protocol", text("local-openai-compatible")),
        ("key_value", text("")),
    ]);
    if let Some(catalog_id) = request.catalog_provider_id {
        put(
            &mut payload,
            "catalog_provider_id",
            PyValue::String(catalog_id),
        );
    }
    if let Some(display_name) = request.display_name {
        put(&mut payload, "display_name", PyValue::String(display_name));
    }
    if let Some(capabilities) = request.capabilities {
        put(
            &mut payload,
            "capabilities",
            PyValue::Array(capabilities.into_iter().map(PyValue::String).collect()),
        );
    }
    payload
}

export!(Pack);

struct BoundRequest {
    registry: Registry<SqlDocumentStore, CoreClock>,
    profile: String,
    payload: PyValue,
}

impl BoundRequest {
    fn open(payload: &str) -> Result<Self, Error> {
        let profile = current_profile()?;
        let payload = loads(payload).map_err(codec_error)?;
        let registry = open_registry(&profile)?;
        Ok(Self {
            registry,
            profile,
            payload,
        })
    }
}

fn current_profile() -> Result<String, Error> {
    flow::current_profile()
        .map_err(|error| boundary_error(error.code, "provider registry profile is unavailable"))
}

fn open_registry(profile: &str) -> Result<Registry<SqlDocumentStore, CoreClock>, Error> {
    Registry::new(
        &PyValue::String(profile.into()),
        SqlDocumentStore::new(),
        CoreClock,
    )
    .map_err(|_| boundary_error(ErrorCode::Denied, "provider registry profile is invalid"))
}

fn encode_reply(value: &PyValue) -> Result<String, Error> {
    let encoded = dumps(value).map_err(codec_error)?;
    core_json(&encoded)?;
    Ok(encoded)
}

fn core_json(encoded: &str) -> Result<tobkiri_canon::CanonValue, Error> {
    tobkiri_canon::parse(encoded.as_bytes(), tobkiri_canon::Variant::StrictV1).map_err(canon_error)
}

fn canon_error(error: tobkiri_canon::Error) -> Error {
    match error {
        tobkiri_canon::Error::Limit(_) => boundary_error(
            ErrorCode::LimitExceeded,
            "provider registry reply exceeds Core JSON limits",
        ),
        _ => boundary_error(
            ErrorCode::Invalid,
            "provider registry reply is outside the Core JSON domain",
        ),
    }
}

fn codec_error(error: CodecError) -> Error {
    match error {
        CodecError::Invalid | CodecError::NonFinite => {
            boundary_error(ErrorCode::Invalid, "provider registry JSON is invalid")
        }
        _ => boundary_error(
            ErrorCode::LimitExceeded,
            "provider registry JSON exceeds limits",
        ),
    }
}

fn source_error(error: SourceError) -> Error {
    let (code, message) = match error.exception_class {
        "ProviderRegistryConflict" => (ErrorCode::Conflict, "provider registry revision is stale"),
        "StorageConflict" => (ErrorCode::Conflict, "provider registry storage conflict"),
        "PermissionError" => (ErrorCode::Denied, "provider registry request is denied"),
        "ApprovalRequired" => (
            ErrorCode::ApprovalRequired,
            "provider registry approval is required",
        ),
        "StorageCancelled" => (
            ErrorCode::Cancelled,
            "provider registry storage was cancelled",
        ),
        "StorageLimitExceeded" => (
            ErrorCode::LimitExceeded,
            "provider registry storage exceeds limits",
        ),
        "StorageUnavailable" | "ClockUnavailable" => (
            ErrorCode::Unavailable,
            "provider registry operation is unavailable",
        ),
        "KeyError" if error.message == "'provider instance is unknown'" => {
            (ErrorCode::NotFound, "provider instance is unknown")
        }
        "ValueError" if error.message == "provider registry profile binding is invalid" => {
            (ErrorCode::Denied, "provider registry profile is invalid")
        }
        "ValueError" | "TypeError" | "AttributeError" | "OverflowError" => {
            (ErrorCode::Invalid, "provider registry data is invalid")
        }
        _ => (ErrorCode::Internal, "provider registry operation failed"),
    };
    boundary_error(code, message)
}

fn boundary_error(code: ErrorCode, message: &str) -> Error {
    Error {
        code,
        message: message.into(),
    }
}

struct CoreClock;

impl Clock for CoreClock {
    fn now(&mut self) -> SourceResult<String> {
        utc_timestamp(clock::now_ms())
    }
}

fn utc_timestamp(ms: u64) -> SourceResult<String> {
    ordered_python_compat::utc_timestamp_from_millis(ms)
        .ok_or_else(|| SourceError::new("ClockUnavailable", "clock date is out of range"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn core_milliseconds_become_original_utc_seconds() {
        for (ms, expected) in [
            (0, "1970-01-01T00:00:00Z"),
            (999, "1970-01-01T00:00:00Z"),
            (951_827_696_789, "2000-02-29T12:34:56Z"),
            (4_107_542_400_000, "2100-03-01T00:00:00Z"),
            (253_402_300_799_999, "9999-12-31T23:59:59Z"),
        ] {
            assert_eq!(utc_timestamp(ms).unwrap(), expected);
        }
        assert_eq!(
            utc_timestamp(u64::MAX).unwrap_err().exception_class,
            "ClockUnavailable"
        );
    }

    #[test]
    fn reply_encoder_enforces_core_numeric_and_depth_limits() {
        assert_eq!(
            encode_reply(&PyValue::Int("9007199254740991".into())).unwrap(),
            "9007199254740991"
        );
        for value in [PyValue::Int("9007199254740992".into()), PyValue::Float(1.0)] {
            assert!(matches!(
                encode_reply(&value).unwrap_err().code,
                ErrorCode::Invalid
            ));
        }
        let mut value = PyValue::Null;
        for _ in 0..=tobkiri_canon::MAX_DEPTH {
            value = PyValue::Array(vec![value]);
        }
        assert!(matches!(
            encode_reply(&value).unwrap_err().code,
            ErrorCode::LimitExceeded
        ));
    }

    #[test]
    fn source_errors_keep_boundary_codes_without_exposing_details() {
        for (error, expected) in [
            (SourceError::permission("private input"), ErrorCode::Denied),
            (SourceError::conflict("private input"), ErrorCode::Conflict),
            (SourceError::value("private input"), ErrorCode::Invalid),
            (
                SourceError::new("StorageError", "private input"),
                ErrorCode::Internal,
            ),
        ] {
            let error = source_error(error);
            assert_eq!(error.code as u8, expected as u8);
            assert!(!error.message.contains("private input"));
        }
    }
}
