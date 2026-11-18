use super::*;
use ordered_python_compat::loads;

fn document(body: &str) -> PyValue {
    loads(&format!(
        r#"{{"version":"{STORE_VERSION}","profile_id":"example",{body}}}"#
    ))
    .unwrap()
}

fn assert_error(result: Result<PyValue, SourceError>, class: &str, message: &str) {
    let error = result.unwrap_err();
    assert_eq!(error.exception_class, class);
    assert_eq!(error.message, message);
}

#[test]
fn absent_document_has_empty_revision_zero_and_misses() {
    let registry = Registry::new("\u{1c}example\u{a0}", None).unwrap();
    assert_eq!(
        registry.snapshot().unwrap(),
        document(r#""revision":0,"profiles":[],"aliases":{}"#),
    );
    assert_eq!(registry.get(&string("route")).unwrap(), PyValue::Null);
    assert_eq!(registry.resolve(&string("route")).unwrap(), PyValue::Null);
}

#[test]
fn snapshot_sorts_saved_keys_and_preserves_raw_record_order_and_values() {
    let registry = Registry::new(
        "example",
        Some(document(
            r#""revision":123456789012345678901234567890,"profiles":{"z":{"opaque":-0.0,"enabled":false},"a":{"display_name":"A","credential_handle":"opaque:a"}},"aliases":{"z":"z","a":"z"}"#,
        )),
    )
    .unwrap();
    let snapshot = registry.snapshot().unwrap();
    assert_eq!(
        snapshot,
        document(
            r#""revision":123456789012345678901234567890,"profiles":[{"display_name":"A","credential_handle":"opaque:a"},{"opaque":-0.0,"enabled":false}],"aliases":{"a":"z","z":"z"}"#,
        )
    );
    let record = registry.get(&string("z")).unwrap();
    let Some(PyValue::Float(opaque)) = record.get("opaque") else {
        panic!("raw float field missing");
    };
    assert_eq!(opaque.to_bits(), (-0.0f64).to_bits());
}

#[test]
fn aliases_override_exact_names_and_follow_only_one_hop() {
    let registry = Registry::new(
        "example",
        Some(document(
            r#""revision":false,"profiles":{"route":{"value":"direct"},"target":{"value":"alias"},"42":{},"invalid id":{}},"aliases":{"route":"target","two-hops":"route","target":"target","raw":"invalid id","dangling":"missing","scalar":true,"array":[],"object":{}}"#,
        )),
    )
    .unwrap();
    assert_eq!(
        registry.get(&string("route")).unwrap().get("value"),
        Some(&string("direct"))
    );
    let route = registry.resolve(&string("\u{1c}route\u{a0}")).unwrap();
    assert_eq!(
        route,
        loads(r#"{"requested_id":"route","resolved_profile_id":"target","aliased":true,"profile":{"value":"alias"},"store_revision":false}"#).unwrap()
    );
    assert_eq!(
        registry
            .resolve(&string("two-hops"))
            .unwrap()
            .get("resolved_profile_id"),
        Some(&string("route"))
    );
    assert_eq!(
        registry.resolve(&string("target")).unwrap().get("aliased"),
        Some(&PyValue::Bool(false))
    );
    assert_eq!(
        registry
            .resolve(&string("raw"))
            .unwrap()
            .get("resolved_profile_id"),
        Some(&string("invalid id"))
    );
    assert!(
        registry
            .get(&PyValue::Int("42".into()))
            .unwrap()
            .as_object()
            .is_some()
    );
    for key in ["missing", "scalar", "dangling"] {
        assert_eq!(registry.resolve(&string(key)).unwrap(), PyValue::Null);
    }
    assert_error(
        registry.resolve(&string("array")),
        "TypeError",
        "unhashable type: 'list'",
    );
    assert_error(
        registry.resolve(&string("object")),
        "TypeError",
        "unhashable type: 'dict'",
    );
}

#[test]
fn validation_and_missing_revision_follow_source_evaluation_order() {
    let invalid_version = Registry::new("example", Some(PyValue::Null)).unwrap();
    assert_error(
        invalid_version.get(&PyValue::Null),
        "ValueError",
        "model registry store version is invalid",
    );
    assert_error(
        invalid_version.resolve(&PyValue::Null),
        "ValueError",
        "model registry identifier is invalid",
    );
    let foreign =
        Registry::new("foreign", Some(document(r#""profiles":[],"aliases":{}"#))).unwrap();
    assert_error(
        foreign.snapshot(),
        "ValueError",
        "model registry profile binding is invalid",
    );
    let invalid_shape =
        Registry::new("example", Some(document(r#""profiles":[],"aliases":{}"#))).unwrap();
    assert_error(
        invalid_shape.snapshot(),
        "ValueError",
        "model registry store is invalid",
    );
    let no_revision = Registry::new(
        "example",
        Some(document(
            r#""profiles":{"existing":{},"malformed":true},"aliases":{}"#,
        )),
    )
    .unwrap();
    assert_error(no_revision.snapshot(), "KeyError", "'revision'");
    assert_error(
        no_revision.resolve(&string("existing")),
        "KeyError",
        "'revision'",
    );
    assert_eq!(
        no_revision.resolve(&string("missing")).unwrap(),
        PyValue::Null
    );
    assert_eq!(
        no_revision.get(&string("malformed")).unwrap(),
        PyValue::Null
    );
    assert!(
        no_revision
            .get(&string("existing"))
            .unwrap()
            .as_object()
            .is_some()
    );
}

#[test]
fn snapshot_applies_dict_conversion_to_saved_sequences() {
    let registry = Registry::new(
        "example",
        Some(document(
            r#""revision":null,"profiles":{"two":[["b",1],"界語",{"x":0,"y":0},["b",2]],"empty":""},"aliases":{}"#,
        )),
    )
    .unwrap();
    assert_eq!(
        registry.snapshot().unwrap(),
        document(r#""revision":null,"profiles":[{},{"b":2,"界":"語","x":"y"}],"aliases":{}"#)
    );
    assert_eq!(registry.get(&string("two")).unwrap(), PyValue::Null);
    let invalid = Registry::new(
        "example",
        Some(document(
            r#""revision":0,"profiles":{"a":[[42,"value"]],"b":[[["unhashable"],0]]},"aliases":{}"#,
        )),
    )
    .unwrap();
    assert_error(invalid.snapshot(), "TypeError", "unhashable type: 'list'");
    let unrepresentable = Registry::new(
        "example",
        Some(document(
            r#""revision":0,"profiles":{"a":[[42,"value"]]},"aliases":{}"#,
        )),
    )
    .unwrap();
    assert_error(
        unrepresentable.snapshot(),
        "ObservationBoundary",
        "non-string Python dictionary keys are outside this observation representation",
    );
}
