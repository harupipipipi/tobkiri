use super::{ErrorCode, PyValue, loads};
use super::{MAX_LEGACY_DOCUMENT_BYTES, NATIVE_CONTAINER_DEPTH, resolve};
use ordered_python_compat::dumps;

#[test]
fn legacy_resolve_matches_original_model_owner_results() {
    let fixture = loads(include_str!("../evidence/original-python-read.json")).unwrap();
    let mut count = 0;
    for case in fixture.get("cases").unwrap().as_array().unwrap() {
        if case.get("action").unwrap().as_str() != Some("resolve") {
            continue;
        }
        count += 1;
        let id = case.get("id").unwrap().as_str().unwrap();
        let profile = case.get("profile_id").unwrap().as_str().unwrap();
        let identifier = case.get("identifier").unwrap().as_str().unwrap().into();
        let document = case.get("document").unwrap();
        let document = (!matches!(document, PyValue::Null)).then(|| dumps(document).unwrap());
        let reply = resolve(profile, document.as_deref(), identifier);
        match case.get("result") {
            Some(PyValue::Null) => {
                let error = reply.expect_err(id);
                assert!(matches!(error.code, ErrorCode::NotFound), "{id}");
                assert_eq!(error.message, "model profile or alias is unknown", "{id}");
            }
            Some(expected) => {
                let encoded = reply.unwrap_or_else(|error| panic!("{id}: {error:?}"));
                let outer = loads(&encoded).unwrap();
                let fields = outer.as_object().unwrap();
                assert_eq!(fields.len(), 1, "{id}");
                assert_eq!(fields[0].0, "owner_result", "{id}");
                assert_eq!(
                    fields[0].1.as_str().unwrap(),
                    dumps(expected).unwrap(),
                    "{id}"
                );
            }
            None => {
                let error = reply.expect_err(id);
                assert!(matches!(error.code, ErrorCode::Invalid), "{id}");
                assert_eq!(
                    error.message,
                    case.get("error")
                        .unwrap()
                        .get("message")
                        .unwrap()
                        .as_str()
                        .unwrap(),
                    "{id}",
                );
            }
        }
    }
    assert_eq!(count, 9);
}

#[test]
fn owner_result_preserves_cpython_numbers_and_private_profile_fields() {
    let document = r#"{
        "version":"rumi.model-registry.store.v1",
        "profile_id":"fixture",
        "revision":184467440737095516160,
        "profiles":{"saved":{
            "credential_handle":"credential:synthetic",
            "parameters":{"temperature":-0.0,"top_p":2e-7,"seed":9007199254740993},
            "requirements":{"preferred_provider_instance_id":"local"},
            "metadata":{"requires_credentials":true,"policy":{"z":1,"a":"日本語"}}
        }},
        "aliases":{"current":"saved"}
    }"#;
    let encoded = resolve("fixture", Some(document), "current".into()).unwrap();
    tobkiri_canon::parse(encoded.as_bytes(), tobkiri_canon::Variant::StrictV1).unwrap();
    let outer = loads(&encoded).unwrap();
    assert_eq!(
        outer.get("owner_result").unwrap().as_str().unwrap(),
        concat!(
            r#"{"requested_id":"current","resolved_profile_id":"saved","aliased":true,"profile":{"credential_handle":"credential:synthetic","#,
            r#""parameters":{"temperature":-0.0,"top_p":2e-07,"seed":9007199254740993},"requirements":{"preferred_provider_instance_id":"local"},"#,
            r#""metadata":{"requires_credentials":true,"policy":{"z":1,"a":"日本語"}}},"store_revision":184467440737095516160}"#,
        ),
    );
}

#[test]
fn legacy_resolve_validates_input_before_signaling_codec_compatibility() {
    for malformed in ["null", "{", "[]"] {
        let invalid_id = resolve("fixture", Some(malformed), "a b".into()).unwrap_err();
        assert!(matches!(invalid_id.code, ErrorCode::Invalid));
        assert_eq!(invalid_id.message, "model registry identifier is invalid");
        let invalid_doc = resolve("fixture", Some(malformed), "a".into()).unwrap_err();
        assert!(matches!(invalid_doc.code, ErrorCode::Invalid));
    }

    for opaque in [r#"{"key":1,"key":2}"#, "NaN", r#""\ud800""#] {
        let document = format!(
            r#"{{"version":"rumi.model-registry.store.v1","profile_id":"fixture",
                "revision":0,"profiles":{{}},"aliases":{{}},"opaque":{opaque}}}"#,
        );
        let error = resolve("fixture", Some(&document), "a".into()).unwrap_err();
        assert!(matches!(error.code, ErrorCode::Unavailable));
        let error = resolve("foreign", Some(&document), "a".into()).unwrap_err();
        assert!(matches!(error.code, ErrorCode::Denied));
    }
}

#[test]
fn legacy_resolve_bounds_input_output_and_recursion_compatibility() {
    let base = r#"{"version":"rumi.model-registry.store.v1","profile_id":"fixture",
        "revision":0,"profiles":{},"aliases":{}}"#;
    let document_at_limit = format!(
        "{base}{}",
        " ".repeat(MAX_LEGACY_DOCUMENT_BYTES - base.len()),
    );
    let error = resolve("fixture", Some(&document_at_limit), "a".into()).unwrap_err();
    assert!(matches!(error.code, ErrorCode::NotFound));
    let oversized_input = format!("{document_at_limit} ");
    let error = resolve("fixture", Some(&oversized_input), "a".into()).unwrap_err();
    assert!(matches!(error.code, ErrorCode::LimitExceeded));

    let deeply_nested = format!(
        r#"{}, "opaque":{}0{}}}"#,
        base.strip_suffix('}').unwrap(),
        "[".repeat(NATIVE_CONTAINER_DEPTH),
        "]".repeat(NATIVE_CONTAINER_DEPTH),
    );
    let error = resolve("fixture", Some(&deeply_nested), "a".into()).unwrap_err();
    assert!(matches!(error.code, ErrorCode::Unavailable));

    let escaped_text = "\\\"".repeat(MAX_LEGACY_DOCUMENT_BYTES / 3);
    let oversized_output = format!(
        r#"{{"version":"rumi.model-registry.store.v1","profile_id":"fixture","revision":0,
        "profiles":{{"a":{{"opaque":"{escaped_text}"}}}},"aliases":{{}}}}"#,
    );
    assert!(oversized_output.len() <= MAX_LEGACY_DOCUMENT_BYTES);
    let error = resolve("fixture", Some(&oversized_output), "a".into()).unwrap_err();
    assert!(matches!(error.code, ErrorCode::LimitExceeded));
}
