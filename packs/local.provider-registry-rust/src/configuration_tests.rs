use super::*;
use crate::state::DocumentStore;
use std::collections::BTreeMap;
use tobkiri_canon::{CanonValue, Variant};

#[derive(Clone, Copy)]
enum WriteBehavior {
    Normal,
    FailBeforeWrite,
    RejectValue,
    RejectConflict,
    CommitThenFail,
    CommitChangedRouteThenFail,
}

struct Store {
    document: Option<PyValue>,
    behavior: WriteBehavior,
    read_calls: usize,
    write_calls: usize,
    reject_reads_after_write: bool,
    events: Vec<&'static str>,
}

impl Store {
    fn new(behavior: WriteBehavior) -> Self {
        Self {
            document: None,
            behavior,
            read_calls: 0,
            write_calls: 0,
            reject_reads_after_write: false,
            events: Vec::new(),
        }
    }
}

impl DocumentStore for Store {
    fn read(&mut self) -> SourceResult<Option<PyValue>> {
        self.read_calls += 1;
        self.events.push("read");
        if self.reject_reads_after_write && self.write_calls != 0 {
            return Err(SourceError::new("OSError", "snapshot unavailable"));
        }
        Ok(self.document.clone())
    }

    fn write(&mut self, document: &PyValue) -> SourceResult<()> {
        self.write_calls += 1;
        self.events.push("write");
        match self.behavior {
            WriteBehavior::RejectValue => Err(SourceError::value("rejected write")),
            WriteBehavior::RejectConflict => Err(SourceError::conflict("stale write")),
            WriteBehavior::Normal => {
                self.document = Some(document.clone());
                Ok(())
            }
            WriteBehavior::FailBeforeWrite => Err(SourceError::new("OSError", "write failed")),
            WriteBehavior::CommitThenFail => {
                self.document = Some(document.clone());
                Err(SourceError::new("OSError", "acknowledgement lost"))
            }
            WriteBehavior::CommitChangedRouteThenFail => {
                let mut changed = document.clone();
                let providers = changed.get("providers").expect("state providers");
                let provider = providers.get("provider.fixture").expect("saved provider");
                let mut provider = provider.clone();
                put(&mut provider, "endpoint", text("https://other.example/v1"));
                put(
                    &mut changed,
                    "providers",
                    object([("provider.fixture", provider)]),
                );
                self.document = Some(changed);
                Err(SourceError::new("OSError", "partial acknowledgement"))
            }
        }
    }
}

#[derive(Default)]
struct Credentials {
    calls: Vec<&'static str>,
    active_handles: Vec<String>,
    create_ack_lost: bool,
    bad_ack: bool,
    revoke_fails: bool,
    received_scopes: Vec<String>,
}

impl CredentialPort for Credentials {
    type Error = ();

    fn create(
        &mut self,
        request: CredentialCreate<'_>,
    ) -> Result<CredentialCreateAck, Self::Error> {
        self.calls.push("create");
        assert_eq!(request.api_key, "fixture-secret-123");
        assert_eq!(request.consumer_pack_id, "fixture.consumer");
        self.received_scopes = request.scopes.iter().map(|scope| (*scope).into()).collect();
        let handle = "credential:fixture".to_owned();
        self.active_handles.push(handle.clone());
        if self.create_ack_lost {
            return Err(());
        }
        Ok(CredentialCreateAck {
            handle: Some(handle),
            profile_id: Some(request.profile_id.into()),
            consumer_pack_id: Some(request.consumer_pack_id.into()),
            provider_instance_id: Some(if self.bad_ack {
                "provider.other".into()
            } else {
                request.provider_instance_id.into()
            }),
        })
    }

    fn revoke(&mut self, _profile_id: &str, handle: &str) -> Result<(), Self::Error> {
        self.calls.push("revoke");
        if self.revoke_fails {
            return Err(());
        }
        self.active_handles.retain(|active| active != handle);
        Ok(())
    }
}

fn request() -> PyValue {
    object([
        ("connection_name", text("fixture")),
        ("protocol", text("openai-compatible")),
        ("endpoint", text("https://provider.example/v1")),
        ("key_value", text("fixture-secret-123")),
    ])
}

fn canonical_reference(request: &Request<'_>) -> CanonValue {
    let mut fields = BTreeMap::new();
    if let Some(capabilities) = &request.submitted_capabilities {
        fields.insert(
            "capabilities".into(),
            CanonValue::Arr(
                capabilities
                    .iter()
                    .map(|value| CanonValue::Str((*value).into()))
                    .collect(),
            ),
        );
    }
    if let Some(value) = request.catalog_provider_id {
        fields.insert("catalog_provider_id".into(), CanonValue::Str(value.into()));
    }
    fields.insert(
        "connection_name".into(),
        CanonValue::Str(request.connection_name.into()),
    );
    if let Some(value) = request.display_name {
        fields.insert("display_name".into(), CanonValue::Str(value.into()));
    }
    fields.insert(
        "endpoint".into(),
        CanonValue::Str(request.submitted_endpoint.into()),
    );
    fields.insert(
        "key_value".into(),
        CanonValue::Str(request.key_value.into()),
    );
    fields.insert("protocol".into(), CanonValue::Str(request.protocol.into()));
    CanonValue::Obj(fields)
}

fn local_request() -> PyValue {
    object([
        ("connection_name", text("local-fixture")),
        ("protocol", text("local-openai-compatible")),
        ("endpoint", text("http://127.0.0.1:1234/v1/")),
        ("key_value", text("")),
        ("display_name", text("Local API")),
        ("catalog_provider_id", text("openai_compatible")),
    ])
}

struct TestClock;

impl Clock for TestClock {
    fn now(&mut self) -> SourceResult<String> {
        Ok("2026-10-22T00:00:00Z".into())
    }
}

fn registry(behavior: WriteBehavior) -> Registry<Store, TestClock> {
    Registry::new(&text("defaults"), Store::new(behavior), TestClock).expect("valid Registry")
}

fn execute_payload(request: &PyValue, plan: PyValue) -> PyValue {
    object([("request", request.clone()), ("plan", plan)])
}

#[test]
fn prepare_keeps_raw_digest_and_does_not_write_or_include_the_key() {
    let mut registry = registry(WriteBehavior::Normal);
    let request = request();
    let plan = prepare_configuration(&mut registry, &request).unwrap();
    assert_eq!(
        plan.get("expected_revision"),
        Some(&PyValue::Int("0".into()))
    );
    assert_eq!(
        plan.get("endpoint"),
        Some(&text("https://provider.example/v1"))
    );
    assert!(plan.get("credential_scopes").is_none());
    assert_eq!(registry.store().document, None);
    let validated = configuration_request(&request).unwrap();
    let expected_digest = canonical_digest(&validated).unwrap();
    assert_eq!(
        plan.get("request_digest").and_then(PyValue::as_str),
        Some(expected_digest.as_str())
    );
    assert!(!format!("{plan:?}").contains("fixture-secret-123"));
}

#[test]
fn typed_zeroizing_encoder_matches_canonical_reference_for_nonsecret_input() {
    let input = object([
        ("protocol", text("openai-compatible")),
        ("key_value", text("synthetic-not-a-secret")),
        (
            "endpoint",
            text("https://provider.example/v1/\u{1}\"\\rocket-🚀"),
        ),
        ("display_name", text("日本語 \"quoted\" \\ path 🌙")),
        ("connection_name", text("fixture")),
        ("catalog_provider_id", text("catalog_fixture")),
        (
            "capabilities",
            PyValue::Array(vec![text("ai.stream"), text("ai.generate")]),
        ),
    ]);
    let validated = configuration_request(&input).unwrap();
    let expected = canonical_reference(&validated);
    let expected_bytes = tobkiri_canon::encode(&expected, Variant::StrictV1).unwrap();
    let expected_digest = tobkiri_canon::digest(&expected, Variant::StrictV1).unwrap();
    let encoded = canonical_bytes(&validated).unwrap();

    assert_eq!(encoded.as_slice(), expected_bytes);
    assert_eq!(
        canonical_digest(&validated).unwrap(),
        expected_digest.as_str()
    );
    assert!(encoded.windows(2).any(|bytes| bytes == b"\\u"));
    assert!(
        encoded
            .windows("日本語".len())
            .any(|bytes| bytes == "日本語".as_bytes())
    );
    assert!(
        encoded
            .windows("🚀".len())
            .any(|bytes| bytes == "🚀".as_bytes())
    );
    assert!(encoded.windows(2).any(|bytes| bytes == b"\\\""));
    assert!(encoded.windows(2).any(|bytes| bytes == b"\\\\"));

    let omitted_optionals = object([
        ("key_value", text("synthetic-not-a-secret")),
        ("endpoint", text("https://provider.example/v1")),
        ("connection_name", text("minimal")),
        ("protocol", text("openai-compatible")),
    ]);
    let validated = configuration_request(&omitted_optionals).unwrap();
    let expected = canonical_reference(&validated);
    assert_eq!(
        canonical_bytes(&validated).unwrap().as_slice(),
        tobkiri_canon::encode(&expected, Variant::StrictV1).unwrap()
    );
    assert_eq!(
        canonical_digest(&validated).unwrap(),
        tobkiri_canon::digest(&expected, Variant::StrictV1)
            .unwrap()
            .as_str()
    );
}

#[test]
fn source_shape_rejects_empty_arrays_null_optionals_and_duplicate_keys() {
    let mut empty_capabilities = request();
    put(
        &mut empty_capabilities,
        "capabilities",
        PyValue::Array(Vec::new()),
    );
    let error = configuration_request(&empty_capabilities)
        .err()
        .expect("empty capability array is rejected by the source shape");
    assert_eq!(
        error.message,
        "provider configuration capabilities are invalid"
    );

    let mut null_capabilities = request();
    put(&mut null_capabilities, "capabilities", PyValue::Null);
    let error = configuration_request(&null_capabilities)
        .err()
        .expect("null capabilities is rejected by the source shape");
    assert_eq!(
        error.message,
        "provider configuration capabilities are invalid"
    );

    for field in ["catalog_provider_id", "display_name"] {
        let mut null_optional = request();
        put(&mut null_optional, field, PyValue::Null);
        let error = configuration_request(&null_optional)
            .err()
            .expect("null optional strings are rejected by the source shape");
        assert_eq!(error.message, "provider configuration fields are invalid");
    }

    let mut entries = request().into_object().expect("request is an object");
    entries.push(("connection_name".into(), text("duplicate")));
    let duplicate = PyValue::Object(entries);
    let error = configuration_request(&duplicate)
        .err()
        .expect("duplicate wire keys must not reach the typed encoder");
    assert_eq!(error.message, "provider configuration fields are invalid");
}

#[test]
fn invalid_port_spellings_are_rejected_before_registry_read() {
    for endpoint in [
        "https://provider.example:١٢٣/v1",
        "https://provider.example:1_23/v1",
        "https://provider.example:+123/v1",
    ] {
        let mut registry_value = registry(WriteBehavior::Normal);
        let request_value = object([
            ("connection_name", text("fixture")),
            ("protocol", text("openai-compatible")),
            ("endpoint", text(endpoint)),
            ("key_value", text("fixture-secret-123")),
        ]);
        let error = prepare_configuration(&mut registry_value, &request_value)
            .expect_err("unsupported port spelling is rejected during request validation");
        assert_eq!(error.message, "provider endpoint is invalid");
        assert_eq!(registry_value.store().read_calls, 0);
    }
}

#[test]
fn changed_plan_is_rejected_before_the_credential_owner_is_called() {
    let mut registry = registry(WriteBehavior::Normal);
    let mut credentials = Credentials::default();
    let request = request();
    let mut plan = prepare_configuration(&mut registry, &request).unwrap();
    put(&mut plan, "endpoint", text("https://foreign.example/v1"));
    let error = execute_configuration(
        &mut registry,
        &mut credentials,
        &execute_payload(&request, plan),
        "fixture.consumer",
    )
    .unwrap_err();
    assert_eq!(error.exception_class, "PermissionError");
    assert!(credentials.calls.is_empty());
    assert!(registry.store().document.is_none());
}

#[test]
fn failed_registry_write_revokes_only_the_unreferenced_new_handle() {
    let mut registry = registry(WriteBehavior::FailBeforeWrite);
    let mut credentials = Credentials::default();
    let request = request();
    let plan = prepare_configuration(&mut registry, &request).unwrap();
    let error = execute_configuration(
        &mut registry,
        &mut credentials,
        &execute_payload(&request, plan),
        "fixture.consumer",
    )
    .unwrap_err();
    assert_eq!(error.message, "provider connection save was not confirmed");
    assert_eq!(credentials.calls, ["create", "revoke"]);
    assert!(credentials.active_handles.is_empty());
    assert!(registry.store().document.is_none());
}

#[test]
fn committed_registry_write_with_lost_ack_is_confirmed_without_revoke() {
    let mut registry = registry(WriteBehavior::CommitThenFail);
    let mut credentials = Credentials::default();
    let request = request();
    let plan = prepare_configuration(&mut registry, &request).unwrap();
    let result = execute_configuration(
        &mut registry,
        &mut credentials,
        &execute_payload(&request, plan),
        "fixture.consumer",
    )
    .unwrap();
    assert_eq!(credentials.calls, ["create"]);
    assert_eq!(result.get("configured"), Some(&PyValue::Bool(true)));
    assert_eq!(credentials.active_handles, ["credential:fixture"]);
    assert_eq!(
        registry.snapshot().unwrap().get("revision"),
        Some(&PyValue::Int("1".into()))
    );
}

#[test]
fn referenced_but_unconfirmed_handle_is_never_revoked() {
    let mut registry = registry(WriteBehavior::CommitChangedRouteThenFail);
    let mut credentials = Credentials::default();
    let request = request();
    let plan = prepare_configuration(&mut registry, &request).unwrap();
    let error = execute_configuration(
        &mut registry,
        &mut credentials,
        &execute_payload(&request, plan),
        "fixture.consumer",
    )
    .unwrap_err();
    assert_eq!(error.message, "provider connection save was not confirmed");
    assert_eq!(credentials.calls, ["create"]);
    assert_eq!(credentials.active_handles, ["credential:fixture"]);
}

#[test]
fn unknown_or_malformed_create_ack_is_not_retried_or_compensated() {
    let request = request();
    for (lost_ack, bad_ack) in [(true, false), (false, true)] {
        let mut registry = registry(WriteBehavior::Normal);
        let mut credentials = Credentials {
            create_ack_lost: lost_ack,
            bad_ack,
            ..Credentials::default()
        };
        let plan = prepare_configuration(&mut registry, &request).unwrap();
        let error = execute_configuration(
            &mut registry,
            &mut credentials,
            &execute_payload(&request, plan),
            "fixture.consumer",
        )
        .unwrap_err();
        assert_eq!(error.message, "provider credential save was not confirmed");
        assert_eq!(credentials.calls, ["create"]);
        assert_eq!(credentials.active_handles, ["credential:fixture"]);
        assert!(!error.message.contains("fixture-secret-123"));
        assert!(registry.store().document.is_none());
    }
}

#[test]
fn local_provider_canonicalizes_endpoint_without_calling_credentials() {
    let mut registry = registry(WriteBehavior::Normal);
    let mut credentials = Credentials::default();
    let request = local_request();
    let validated = configuration_request(&request).unwrap();
    assert_eq!(validated.endpoint.as_ref(), "http://127.0.0.1:1234/v1");
    assert_eq!(validated.submitted_endpoint, "http://127.0.0.1:1234/v1/");
    let expected_digest =
        tobkiri_canon::digest(&canonical_reference(&validated), Variant::StrictV1).unwrap();
    let plan = prepare_configuration(&mut registry, &request).unwrap();
    assert_eq!(
        plan.get("request_digest").and_then(PyValue::as_str),
        Some(expected_digest.as_str())
    );
    assert_eq!(
        plan.get("endpoint").and_then(PyValue::as_str),
        Some("http://127.0.0.1:1234/v1")
    );
    let result = execute_configuration(
        &mut registry,
        &mut credentials,
        &execute_payload(&request, plan),
        "fixture.consumer",
    )
    .unwrap();
    assert!(credentials.calls.is_empty());
    assert_eq!(
        result.get("provider_instance_id").and_then(PyValue::as_str),
        Some("provider.local-fixture")
    );
    let snapshot = registry.snapshot().unwrap();
    let provider = &snapshot.get("providers").unwrap().as_array().unwrap()[0];
    assert_eq!(provider.get("credential_handle"), Some(&PyValue::Null));
    assert_eq!(
        provider.get("metadata").unwrap().get("catalog_provider_id"),
        Some(&text("openai_compatible"))
    );
}

#[test]
fn explicit_audio_scopes_are_sorted_and_default_scopes_stay_text_only() {
    let mut first_registry = registry(WriteBehavior::Normal);
    let mut credentials = Credentials::default();
    let mut first_request = request();
    put(
        &mut first_request,
        "capabilities",
        PyValue::Array(vec![text("ai.audio.transcribe"), text("ai.generate")]),
    );
    let plan = prepare_configuration(&mut first_registry, &first_request).unwrap();
    let expected_scopes = [text("ai.audio.transcribe"), text("ai.generate")];
    assert_eq!(
        plan.get("credential_scopes").and_then(PyValue::as_array),
        Some(expected_scopes.as_slice())
    );
    execute_configuration(
        &mut first_registry,
        &mut credentials,
        &execute_payload(&first_request, plan),
        "fixture.consumer",
    )
    .unwrap();
    assert_eq!(
        credentials.received_scopes,
        ["ai.audio.transcribe", "ai.generate"]
    );

    let mut second_registry = registry(WriteBehavior::Normal);
    let mut second_credentials = Credentials::default();
    let second_request = request();
    let plan = prepare_configuration(&mut second_registry, &second_request).unwrap();
    execute_configuration(
        &mut second_registry,
        &mut second_credentials,
        &execute_payload(&second_request, plan),
        "fixture.consumer",
    )
    .unwrap();
    assert_eq!(
        second_credentials.received_scopes,
        ["ai.generate", "ai.stream"]
    );
}

#[test]
fn plan_mapping_order_matches_python_dictionary_equality() {
    let mut left = object([("a", PyValue::Int("1".into())), ("b", text("x"))]);
    let right = object([("b", text("x")), ("a", PyValue::Bool(true))]);
    assert!(python_equal(&left, &right));
    put(&mut left, "a", PyValue::Float(1.5));
    assert!(!python_equal(&left, &right));
}

#[test]
fn known_local_rejection_never_reads_acknowledgement_evidence() {
    for behavior in [WriteBehavior::RejectValue, WriteBehavior::RejectConflict] {
        let mut registry = registry(behavior);
        let mut credentials = Credentials::default();
        let request = local_request();
        let plan = prepare_configuration(&mut registry, &request).unwrap();
        registry.store_mut().reject_reads_after_write = true;
        let error = execute_configuration(
            &mut registry,
            &mut credentials,
            &execute_payload(&request, plan),
            "fixture.consumer",
        )
        .unwrap_err();
        assert_eq!(error, connection_save_error());
        assert_eq!(registry.store().write_calls, 1);
        assert_eq!(registry.store().events.last(), Some(&"write"));
        assert!(credentials.calls.is_empty());
    }
}

#[test]
fn uncertain_local_ack_reads_once_and_confirms_the_full_record() {
    let mut registry = registry(WriteBehavior::CommitThenFail);
    let mut credentials = Credentials::default();
    let request = local_request();
    let plan = prepare_configuration(&mut registry, &request).unwrap();
    let result = execute_configuration(
        &mut registry,
        &mut credentials,
        &execute_payload(&request, plan),
        "fixture.consumer",
    )
    .unwrap();
    assert_eq!(result.get("configured"), Some(&PyValue::Bool(true)));
    let events = &registry.store().events;
    let write = events.iter().position(|event| *event == "write").unwrap();
    assert_eq!(&events[write + 1..], &["read"]);
    assert_eq!(registry.store().write_calls, 1);
    assert!(credentials.calls.is_empty());
}

#[test]
fn remote_known_rejection_reads_snapshot_before_compensating() {
    for behavior in [WriteBehavior::RejectValue, WriteBehavior::RejectConflict] {
        for unreadable in [false, true] {
            let mut registry = registry(behavior);
            let mut credentials = Credentials::default();
            let request = request();
            let plan = prepare_configuration(&mut registry, &request).unwrap();
            registry.store_mut().reject_reads_after_write = unreadable;
            let error = execute_configuration(
                &mut registry,
                &mut credentials,
                &execute_payload(&request, plan),
                "fixture.consumer",
            )
            .unwrap_err();
            assert_eq!(error, connection_save_error());
            let events = &registry.store().events;
            let write = events.iter().position(|event| *event == "write").unwrap();
            assert_eq!(&events[write + 1..], &["read"]);
            if unreadable {
                assert_eq!(credentials.calls, ["create"]);
                assert_eq!(credentials.active_handles, ["credential:fixture"]);
            } else {
                assert_eq!(credentials.calls, ["create", "revoke"]);
                assert!(credentials.active_handles.is_empty());
            }
        }
    }
}

#[test]
fn uncertain_registry_and_cleanup_errors_preserve_the_new_handle() {
    for unreadable in [false, true] {
        let mut registry = registry(WriteBehavior::FailBeforeWrite);
        let mut credentials = Credentials {
            revoke_fails: true,
            ..Credentials::default()
        };
        let request = request();
        let plan = prepare_configuration(&mut registry, &request).unwrap();
        registry.store_mut().reject_reads_after_write = unreadable;
        let error = execute_configuration(
            &mut registry,
            &mut credentials,
            &execute_payload(&request, plan),
            "fixture.consumer",
        )
        .unwrap_err();
        assert_eq!(
            error,
            if unreadable {
                connection_save_error()
            } else {
                cleanup_error()
            }
        );
        assert_eq!(
            credentials.calls,
            if unreadable {
                vec!["create"]
            } else {
                vec!["create", "revoke"]
            }
        );
        assert_eq!(credentials.active_handles, ["credential:fixture"]);
        assert!(!error.message.contains("fixture-secret-123"));
    }
}

#[test]
fn duplicate_plan_fields_cannot_replace_missing_fields_or_panic() {
    let mut registry = registry(WriteBehavior::Normal);
    let mut credentials = Credentials::default();
    let request = request();
    let plan = prepare_configuration(&mut registry, &request).unwrap();
    let endpoint = plan.get("endpoint").unwrap().clone();
    let repeated = PyValue::Object(vec![
        ("endpoint".into(), endpoint);
        plan.as_object().unwrap().len()
    ]);
    let error = execute_configuration(
        &mut registry,
        &mut credentials,
        &execute_payload(&request, repeated),
        "fixture.consumer",
    )
    .unwrap_err();
    assert_eq!(error.exception_class, "PermissionError");
    assert!(credentials.calls.is_empty());
    assert_eq!(registry.store().write_calls, 0);
}

#[test]
fn absent_and_empty_remote_ports_preserve_original_url_spelling() {
    for endpoint in [
        "https://provider.example/v1",
        "https://provider.example:/v1",
    ] {
        let mut owner = registry(WriteBehavior::Normal);
        let mut payload = request();
        put(&mut payload, "endpoint", text(endpoint));
        let plan = prepare_configuration(&mut owner, &payload).unwrap();
        assert_eq!(plan.get("endpoint"), Some(&text(endpoint)));
        assert_eq!(owner.store().write_calls, 0);
    }
}

#[test]
fn preparation_matches_authenticated_original_python_cases() {
    use ordered_python_compat::loads;
    use std::path::Path;

    const FIXTURE: &[u8] = include_bytes!("../evidence/configuration-prepare-python.json");
    let expected_fixture = "fee32566f76b4cca0f7efd3d456f675ff51b7999c0f97a18e44f74699499dd13";
    assert_eq!(format!("{:x}", Sha256::digest(FIXTURE)), expected_fixture);
    let fixture: serde_json::Value = serde_json::from_slice(FIXTURE).unwrap();
    assert_eq!(
        fixture["schema"],
        "io.tobkiri.original-provider-configuration-prepare.v1"
    );
    assert_eq!(fixture["network_attempts"], serde_json::json!([]));
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    for source in fixture["sources"]
        .as_array()
        .unwrap()
        .iter()
        .chain(std::iter::once(&fixture["producer"]))
    {
        let relative = source["path"].as_str().unwrap();
        assert!(relative.starts_with("tobkiri_runtime/") || relative.starts_with("tools/quality/"));
        assert!(!relative.split('/').any(|part| part == ".."));
        let bytes = std::fs::read(root.join(relative)).unwrap();
        assert_eq!(
            format!("{:x}", Sha256::digest(&bytes)),
            source["sha256"].as_str().unwrap(),
            "{relative}"
        );
    }
    let cases = fixture["cases"].as_array().unwrap();
    assert_eq!(cases.len(), 23);
    for case in cases {
        let id = case["id"].as_str().unwrap();
        let input = loads(&case["request"].to_string()).unwrap();
        let mut owner = registry(WriteBehavior::Normal);
        let outcome = prepare_configuration(&mut owner, &input);
        if let Some(expected) = case.get("result") {
            let expected = loads(&expected.to_string()).unwrap();
            assert!(python_equal(&outcome.unwrap(), &expected), "{id}");
        } else {
            let error = outcome.unwrap_err();
            assert_eq!(
                error.exception_class,
                case["error"]["class"].as_str().unwrap(),
                "{id}"
            );
            assert_eq!(
                error.message,
                case["error"]["message"].as_str().unwrap(),
                "{id}"
            );
        }
        assert_eq!(owner.store().write_calls, 0, "{id}");
        assert!(owner.store().document.is_none(), "{id}");
    }
}
