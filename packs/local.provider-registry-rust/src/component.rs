//! Wasm boundary for Registry operations under Core authority.

#[path = "component_legacy_stage.rs"]
mod legacy_stage;

wit_bindgen::generate!({
    path: ["../../crates/tobkiri-wit/wit/core-v1-2/core.wit", "wit"],
    world: "local:provider-registry/pack@1.0.0",
    generate_all,
});

use crate::{
    component_store::SqlDocumentStore,
    configuration::{
        configured_result, prepare_configuration, stage_local_configuration,
        validate_legacy_local_configuration,
    },
    failure::{SourceError, SourceResult},
    host::{
        MANAGE_OPERATION, READONLY_RESOURCE_OPERATION, invoke_manage, invoke_readonly,
        provider_connection_snapshot,
    },
    model_access_projection,
    registry::Registry,
    routing,
    state::{Clock, DocumentStore, object, put, text},
};
use exports::local::provider_registry::configuration::LocalConnection;
use ordered_python_compat::{CodecError, PyValue, dumps, dumps_bounded, loads};
use tobkiri::core::{
    clock, flow,
    types::{CallbackHandle, Error, ErrorCode},
};

// Same byte ceiling as the current Registry's private SQL document.
const MAX_DOCUMENT_BYTES: usize = 1024 * 1024;

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

impl exports::local::provider_registry::legacy_resource::Guest for Pack {
    fn snapshot(document: Option<String>, settings_projection: bool) -> Result<String, Error> {
        legacy_snapshot(
            &current_profile()?,
            document.as_deref(),
            settings_projection,
        )
    }
}

fn legacy_snapshot(
    profile: &str,
    document: Option<&str>,
    settings_projection: bool,
) -> Result<String, Error> {
    let snapshot = observed_snapshot(profile, document)?;
    let reply = if settings_projection {
        provider_connection_snapshot(&snapshot).map_err(source_error)?
    } else {
        snapshot
    };
    encode_owner_reply(&reply)
}

impl exports::local::provider_registry::model_access_projection::Guest for Pack {
    fn project(document: Option<String>, request: String) -> Result<String, Error> {
        project_model_access(&current_profile()?, document.as_deref(), &request)
    }
}

fn project_model_access(
    profile: &str,
    document: Option<&str>,
    request: &str,
) -> Result<String, Error> {
    if request.len() > MAX_DOCUMENT_BYTES {
        return Err(boundary_error(
            ErrorCode::LimitExceeded,
            "model access projection request exceeds limits",
        ));
    }
    let request = loads(request).map_err(codec_error)?;
    let snapshot = observed_snapshot(profile, document)?;
    let result = model_access_projection::project(&snapshot, &request).map_err(source_error)?;
    let reply = match request.get("phase").and_then(PyValue::as_str) {
        Some("policy") => result,
        Some("prepare-catalog" | "project-catalog") => {
            object([("result", result), ("snapshot", snapshot)])
        }
        _ => {
            return Err(boundary_error(
                ErrorCode::Invalid,
                "model access projection phase is invalid",
            ));
        }
    };
    encode_owner_reply(&reply)
}

fn observed_snapshot(profile: &str, document: Option<&str>) -> Result<PyValue, Error> {
    let mut registry = Registry::new(
        &text(profile),
        ObservedRegistry([document].into_iter()),
        ObservationClock,
    )
    .map_err(source_error)?;
    registry.snapshot().map_err(source_error)
}

fn encode_owner_reply(reply: &PyValue) -> Result<String, Error> {
    // Core validates the envelope without interpreting Python's numeric values.
    let owner = dumps_bounded(reply, MAX_DOCUMENT_BYTES).map_err(codec_error)?;
    let envelope = object([("owner_result", text(owner))]);
    let encoded = dumps_bounded(&envelope, MAX_DOCUMENT_BYTES).map_err(codec_error)?;
    core_json(&encoded)?;
    Ok(encoded)
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
        let mut registry =
            Registry::new(&text(profile), ObservedRegistry(observed), ObservationClock)
                .map_err(source_error)?;
        let plan =
            prepare_configuration(&mut registry, &local_request(request)).map_err(source_error)?;
        encode_reply(&plan)
    }
}

impl exports::local::provider_registry::legacy_configuration_live::Guest for Pack {
    fn prepare_local(request: LocalConnection, observe: CallbackHandle) -> Result<String, Error> {
        let mut registry = live_registry(observe)?;
        let plan = prepare_configuration(&mut registry, &local_request(request))
            .map_err(|error| registry.store().map_error(error))?;
        encode_reply(&plan)
    }
}

impl exports::local::provider_registry::legacy_configuration_commit::Guest for Pack {
    fn validate_local(
        request: LocalConnection,
        plan: String,
        observe: CallbackHandle,
    ) -> Result<String, Error> {
        let plan = loads(&plan).map_err(codec_error)?;
        let mut registry = live_registry(observe)?;
        let prepared =
            validate_legacy_local_configuration(&mut registry, &local_request(request), &plan)
                .map_err(|error| registry.store().map_error(error))?;
        encode_reply(&prepared)
    }
}

struct ObservationClock;

impl Clock for ObservationClock {
    fn now(&mut self) -> SourceResult<String> {
        Err(SourceError::permission(
            "provider registry observations have no clock",
        ))
    }
}

struct ObservedRegistry<'a, const N: usize>(std::array::IntoIter<Option<&'a str>, N>);

impl<const N: usize> DocumentStore for ObservedRegistry<'_, N> {
    fn read(&mut self) -> SourceResult<Option<PyValue>> {
        self.0
            .next()
            .ok_or_else(|| SourceError::value("provider registry observation missing"))?
            .map(decode_observation)
            .transpose()
    }

    fn write(&mut self, _: &PyValue) -> SourceResult<()> {
        Err(SourceError::permission(
            "provider registry observations are read-only",
        ))
    }
}

struct LiveObservedRegistry {
    observe: CallbackHandle,
    read_error: Option<ErrorCode>,
}

fn live_registry(
    observe: CallbackHandle,
) -> Result<Registry<LiveObservedRegistry, ObservationClock>, Error> {
    let profile = current_profile()?;
    let store = LiveObservedRegistry::new(observe);
    Registry::new(&text(profile), store, ObservationClock).map_err(source_error)
}

impl LiveObservedRegistry {
    fn new(observe: CallbackHandle) -> Self {
        Self {
            observe,
            read_error: None,
        }
    }

    fn map_error(&self, error: SourceError) -> Error {
        match self.read_error {
            Some(code) => boundary_error(code, "provider registry observation failed"),
            None => source_error(error),
        }
    }
}

impl DocumentStore for LiveObservedRegistry {
    fn read(&mut self) -> SourceResult<Option<PyValue>> {
        let reply = flow::invoke_callback(self.observe, "null").map_err(|error| {
            self.read_error = Some(error.code);
            SourceError::value("provider registry observation failed")
        })?;
        // The envelope can escape each raw byte as six ASCII bytes, plus quotes.
        if reply.len() > 6 * MAX_DOCUMENT_BYTES + 2 {
            return Err(SourceError::value(
                "provider registry observation is too large",
            ));
        }
        let raw = serde_json::from_str::<Option<String>>(&reply)
            .map_err(|_| SourceError::value("provider registry observation reply is invalid"))?;
        raw.as_deref().map(decode_observation).transpose()
    }

    fn write(&mut self, _: &PyValue) -> SourceResult<()> {
        Err(SourceError::permission(
            "provider registry observations are read-only",
        ))
    }
}

fn decode_observation(raw: &str) -> SourceResult<PyValue> {
    if raw.len() > MAX_DOCUMENT_BYTES {
        return Err(SourceError::value(
            "provider registry observation is too large",
        ));
    }
    loads(raw).map_err(|_| SourceError::value("provider registry observation is invalid"))
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

    fn snapshot_owner(encoded: &str) -> String {
        let reply = loads(encoded).unwrap();
        let fields = reply.as_object().unwrap();
        assert_eq!(fields.len(), 1);
        assert_eq!(fields[0].0, "owner_result");
        fields[0].1.as_str().unwrap().to_owned()
    }

    #[test]
    fn legacy_snapshot_matches_original_raw_and_settings_owner_replies() {
        const DOCUMENT: &str = include_str!(
            "../evidence/original-python/cases/ui-redacted-versus-raw-host-readers/actions/host-ui-reader.store-before.json"
        );
        const RAW: &str = include_str!(
            "../evidence/original-python/cases/ui-redacted-versus-raw-host-readers/actions/raw-snapshot.result.json"
        );
        const SETTINGS: &str = include_str!(
            "../evidence/original-python/cases/ui-redacted-versus-raw-host-readers/actions/host-ui-reader.result.json"
        );

        for (projection, expected) in [(false, RAW), (true, SETTINGS)] {
            let reply = legacy_snapshot("defaults", Some(DOCUMENT), projection).unwrap();
            assert_eq!(snapshot_owner(&reply), expected.trim());
        }

        let document = DOCUMENT.replace(
            "\"private_note\": \"synthetic-private-marker\"",
            r#""private_note": {"decimal": -0.0, "exponent": 2e-7, "wide": 9007199254740993}"#,
        );
        let raw = legacy_snapshot("defaults", Some(&document), false).unwrap();
        let settings = legacy_snapshot("defaults", Some(&document), true).unwrap();
        assert!(snapshot_owner(&raw).contains(
            r#""private_note":{"decimal":-0.0,"exponent":2e-07,"wide":9007199254740993}"#
        ));
        assert_eq!(snapshot_owner(&settings), SETTINGS.trim());
    }

    #[test]
    fn legacy_snapshot_preserves_empty_state_and_profile_binding() {
        let raw = legacy_snapshot("defaults", None, false).unwrap();
        let settings = legacy_snapshot("defaults", None, true).unwrap();
        assert_eq!(
            snapshot_owner(&raw),
            r#"{"version":"rumi.provider-registry.store.v1","profile_id":"defaults","revision":0,"providers":[]}"#
        );
        assert_eq!(
            snapshot_owner(&settings),
            r#"{"revision":0,"providers":[]}"#
        );
        for projection in [false, true] {
            let error = legacy_snapshot(
                "defaults",
                Some(
                    r#"{"version":"rumi.provider-registry.store.v1","profile_id":"foreign","revision":0,"providers":{}}"#,
                ),
                projection,
            )
            .unwrap_err();
            assert!(matches!(error.code, ErrorCode::Denied));
        }
    }

    #[test]
    fn model_access_projection_keeps_registry_observations_in_catalog_phases() {
        const DOCUMENT: &str = include_str!(
            "../evidence/original-python/cases/ui-redacted-versus-raw-host-readers/actions/host-ui-reader.store-before.json"
        );
        let document = DOCUMENT.replace("1700000000.75", "9007199254740993");
        let document = Some(document.as_str());
        let policy = project_model_access(
            "defaults",
            document,
            r#"{"phase":"policy","provider_instance_id":"connection/work"}"#,
        )
        .unwrap();
        let policy = snapshot_owner(&policy);
        let decoded = loads(&policy).unwrap();
        assert!(decoded.get("model_access").is_some());
        assert!(decoded.get("snapshot").is_none());
        assert!(!policy.contains("synthetic-registry-key"));

        let legacy = legacy_snapshot("defaults", document, false).unwrap();
        let expected_snapshot = loads(&snapshot_owner(&legacy)).unwrap();
        for (request, result_key) in [
            (
                r#"{"phase":"prepare-catalog","provider_instance_id":"connection/work"}"#,
                "provider_id",
            ),
            (
                r#"{"phase":"project-catalog","provider_instance_id":"connection/work","catalog":{"models":[],"providers":[{"provider_id":"vendor"}]}}"#,
                "models",
            ),
        ] {
            let reply = project_model_access("defaults", document, request).unwrap();
            let owner = snapshot_owner(&reply);
            assert!(owner.contains(r#""observed_at":9007199254740993"#));
            let decoded = loads(&owner).unwrap();
            assert_eq!(decoded.get("snapshot"), Some(&expected_snapshot));
            assert!(decoded.get("result").unwrap().get(result_key).is_some());
        }
    }

    #[test]
    fn model_access_projection_rejects_oversize_requests_and_foreign_documents() {
        let oversized = " ".repeat(MAX_DOCUMENT_BYTES + 1);
        let error = project_model_access("defaults", None, &oversized).unwrap_err();
        assert!(matches!(error.code, ErrorCode::LimitExceeded));

        let error = project_model_access(
            "defaults",
            Some(
                r#"{"version":"rumi.provider-registry.store.v1","profile_id":"foreign","revision":0,"providers":{}}"#,
            ),
            r#"{"phase":"policy","provider_instance_id":"connection/work"}"#,
        )
        .unwrap_err();
        assert!(matches!(error.code, ErrorCode::Denied));
    }

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
