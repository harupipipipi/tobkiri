//! Exact source selection is required before any runtime or authority exists.
use tobkiri_canon::ids::{OpName, PackId, ShapeRef};
use tobkiri_profile::InputEntry;

fn entry() -> serde_json::Value {
    serde_json::json!({
        "source_pack": "local.physical-input",
        "exact_input_view": "local.contracts.input@1.7",
        "operation": "receive"
    })
}

#[test]
fn exact_record_roundtrips_without_operation_or_version_inference() {
    let parsed: InputEntry = serde_json::from_value(entry()).unwrap();
    assert_eq!(
        parsed.source_pack,
        PackId::parse("local.physical-input").unwrap()
    );
    assert_eq!(
        parsed.exact_input_view,
        ShapeRef::parse("local.contracts.input@1.7").unwrap()
    );
    assert_eq!(parsed.operation, OpName::parse("receive").unwrap());
    assert_eq!(serde_json::to_value(parsed).unwrap(), entry());
}

#[test]
fn old_pack_id_only_entry_has_a_named_error() {
    let error = serde_json::from_value::<InputEntry>(serde_json::json!("local.physical-input"))
        .unwrap_err()
        .to_string();
    assert!(error.contains("Pack-ID-only"));
    assert!(error.contains("exact_input_view"));
}

#[test]
fn every_selection_field_is_required_and_unknown_fields_are_rejected() {
    for missing in ["source_pack", "exact_input_view", "operation"] {
        let mut value = entry();
        value.as_object_mut().unwrap().remove(missing);
        let error = serde_json::from_value::<InputEntry>(value)
            .unwrap_err()
            .to_string();
        assert!(error.contains(missing), "{error}");
    }
    let mut value = entry();
    value["args"] = serde_json::json!({});
    assert!(serde_json::from_value::<InputEntry>(value).is_err());
}

#[test]
fn exact_view_and_operation_names_cannot_be_empty_or_major_only() {
    for invalid in ["local.contracts.input@1", "local.contracts.input", ""] {
        let mut value = entry();
        value["exact_input_view"] = serde_json::json!(invalid);
        assert!(serde_json::from_value::<InputEntry>(value).is_err());
    }
    let mut value = entry();
    value["operation"] = serde_json::json!("");
    assert!(serde_json::from_value::<InputEntry>(value).is_err());
}

#[test]
fn duplicate_record_fields_are_not_collapsed_before_validation() {
    let bytes = br#"{"source_pack":"local.physical-input","exact_input_view":"local.contracts.input@1.7","operation":"receive","operation":"other"}"#;
    assert!(serde_json::from_slice::<InputEntry>(bytes).is_err());
}
