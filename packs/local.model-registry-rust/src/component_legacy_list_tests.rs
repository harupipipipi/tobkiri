use super::{MAX_LEGACY_DOCUMENT_BYTES, dumps, list_legacy_models, loads, tobkiri};
use tobkiri::core::types::ErrorCode;

#[test]
fn legacy_list_matches_original_settings_projection() {
    let fixture = loads(include_str!("../evidence/original-python-read.json")).unwrap();
    for case in fixture.get("cases").unwrap().as_array().unwrap() {
        if case.get("action").unwrap().as_str() != Some("settings") {
            continue;
        }
        let id = case.get("id").unwrap().as_str().unwrap();
        let profile = case.get("profile_id").unwrap().as_str().unwrap();
        let document = case.get("document").unwrap();
        let document = (!matches!(document, ordered_python_compat::PyValue::Null))
            .then(|| dumps(document).unwrap());
        let reply = list_legacy_models(profile, document.as_deref());
        if let Some(expected) = case.get("result") {
            assert_eq!(reply.unwrap(), dumps(expected).unwrap(), "{id}");
        } else {
            assert!(
                matches!(reply.unwrap_err().code, ErrorCode::Invalid),
                "{id}"
            );
        }
    }
}

#[test]
fn legacy_list_distinguishes_absence_from_invalid_data() {
    assert_eq!(
        list_legacy_models("fixture", None).unwrap(),
        r#"{"profiles":[],"count":0,"registry_revision":0}"#,
    );
    for invalid in ["null", "", "{", "[]"] {
        let error = list_legacy_models("fixture", Some(invalid)).unwrap_err();
        assert!(matches!(error.code, ErrorCode::Invalid));
    }

    let foreign = r#"{"version":"rumi.model-registry.store.v1","profile_id":"foreign",
        "revision":0,"profiles":{},"aliases":{}}"#;
    let error = list_legacy_models("fixture", Some(foreign)).unwrap_err();
    assert!(matches!(error.code, ErrorCode::Denied));
}

#[test]
fn legacy_list_checks_raw_input_size_and_strict_output() {
    let mut document = String::from(
        r#"{"version":"rumi.model-registry.store.v1","profile_id":"fixture",
            "revision":0,"profiles":{},"aliases":{}}"#,
    );
    document.extend(std::iter::repeat_n(
        ' ',
        MAX_LEGACY_DOCUMENT_BYTES - document.len(),
    ));
    assert!(list_legacy_models("fixture", Some(&document)).is_ok());
    document.push(' ');
    let error = list_legacy_models("fixture", Some(&document)).unwrap_err();
    assert!(matches!(error.code, ErrorCode::LimitExceeded));

    let wide_revision = r#"{"version":"rumi.model-registry.store.v1","profile_id":"fixture",
        "revision":9007199254740993,"profiles":{},"aliases":{}}"#;
    let error = list_legacy_models("fixture", Some(wide_revision)).unwrap_err();
    assert!(matches!(error.code, ErrorCode::Invalid));
    assert_eq!(error.message, "model Settings response is invalid");
}
