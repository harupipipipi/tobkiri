use ordered_python_compat::loads;

use super::*;

const CREATED: &str = "2026-11-19T04:55:00Z";
const UPDATED: &str = "2026-11-20T00:00:00Z";

fn create_request(expected_revision: i32) -> PyValue {
    loads(&format!(
        r#"{{"operation":"create","expected_revision":{expected_revision},"provider_registry_revision":0,"record":{{"model_profile_id":"relay/local","model_id":"instruct-v1","display_name":"作業\nモデル","metadata":{{"provider_connection_id":"connection/local"}}}}}}"#
    ))
    .unwrap()
}

fn save_request(expected_revision: i32, cap: u32) -> PyValue {
    let mut request = create_request(expected_revision);
    set_field(&mut request, "operation", string("save"));
    set_field(&mut request, "preserve_existing", PyValue::Bool(true));
    let mut record = request.get("record").unwrap().clone();
    set_field(&mut record, "enabled", PyValue::Bool(true));
    set_field(
        &mut record,
        "parameters",
        object([("max_tokens", PyValue::Int(cap.to_string()))]),
    );
    set_field(&mut request, "record", record);
    request
}

fn owner(document: &str) -> Registry {
    Registry::new("profile-1", Some(loads(document).unwrap())).unwrap()
}

#[test]
fn create_matches_the_python_owner_hash_and_preserves_revision_on_confirmation() {
    let empty = Registry::new("profile-1", None).unwrap();
    let (staged, response) = empty.apply_settings(&create_request(0), CREATED).unwrap();
    let document = staged.unwrap();
    assert_eq!(document.get("migration"), Some(&PyValue::Null));
    assert_eq!(
        response.get("store_revision"),
        Some(&PyValue::Int("1".into()))
    );
    let profile = response.get("profile").unwrap();
    assert_eq!(
        profile.get("created_at").and_then(PyValue::as_str),
        Some(CREATED)
    );
    assert_eq!(
        profile.get("updated_at").and_then(PyValue::as_str),
        Some(CREATED)
    );
    assert_eq!(
        profile.get("record_hash").and_then(PyValue::as_str),
        Some("sha256:64c1a1c164c68f69b8f0175731d8481196e9b01f990003dac0048547b161df3b")
    );
    let updated = Registry::new("profile-1", Some(document)).unwrap();
    let (staged, confirmed) = updated.apply_settings(&create_request(1), UPDATED).unwrap();
    assert!(staged.is_none());
    assert_eq!(confirmed, response);
    assert_eq!(
        updated
            .apply_settings(&create_request(0), UPDATED)
            .unwrap_err(),
        conflict("model registry revision is stale")
    );
}

#[test]
fn save_matches_the_python_owner_hash_and_keeps_legacy_policy_fields() {
    let registry = owner(
        r#"{
            "version":"rumi.model-registry.store.v1","profile_id":"profile-1","revision":1,
            "profiles":{"relay/local":{
                "model_profile_id":"relay/local","model_id":"instruct-v1","display_name":"作業\nモデル",
                "requirements":{"preferred_provider_instance_id":"connection/local",
                                "maximum_cost":{"é":0.5,"zero":-0.0},"unlisted":30},
                "parameters":{"temperature":0.1,"max_tokens":512,"api_key":"remove"},
                "metadata":{"token_healing":true,"Auth-Token":"remove"},
                "credential_handle":"opaque:fixture","enabled":false,"unlisted":true,
                "record_revision":"00099999999999999999999","created_at":"2026-11-19T04:55:00Z"
            }},"aliases":{"relay/default":"relay/local"},"foreign":{"order":[4,2]}
        }"#,
    );
    let (document, result) = registry
        .apply_settings(&save_request(1, 1024), UPDATED)
        .unwrap();
    let document = document.unwrap();
    assert_eq!(document.get("foreign"), registry.document.get("foreign"));
    assert_eq!(document.get("aliases"), registry.document.get("aliases"));
    assert_eq!(
        result.get("store_revision"),
        Some(&PyValue::Int("2".into()))
    );
    let profile = result.get("profile").unwrap();
    assert_eq!(
        profile,
        &loads(
            r#"{
               "model_profile_id":"relay/local","display_name":"作業\nモデル","model_id":"instruct-v1",
               "requirements":{"preferred_provider_instance_id":"connection/local","maximum_cost":{"é":0.5,"zero":-0.0}},
               "credential_handle":"opaque:fixture","parameters":{"temperature":0.1,"max_tokens":1024},
               "enabled":true,"metadata":{"token_healing":true,"provider_connection_id":"connection/local"},
               "created_at":"2026-11-19T04:55:00Z","updated_at":"2026-11-20T00:00:00Z",
               "record_revision":100000000000000000000,
               "record_hash":"sha256:8328c70c7188eb213f84d413f6fb2ea3b4e1ead3ff938e09f4e5fe1ba4fb2a36"
            }"#
        )
        .unwrap()
    );
}

#[test]
fn selector_confirmation_retains_existing_policy_and_disabled_routes_remain_reserved() {
    let document = r#"{
        "version":"rumi.model-registry.store.v1","profile_id":"profile-1","revision":7,
        "profiles":{"relay/local":{
            "model_profile_id":"relay/local","model_id":"instruct-v1","display_name":"作業\nモデル",
            "requirements":{"preferred_provider_instance_id":"connection/local"},
            "parameters":{"max_tokens":256,"temperature":0.25},"enabled":true
        }},
        "aliases":{"relay/alias":"relay/local"}
    }"#;
    let (staged, result) = owner(document)
        .apply_settings(&create_request(7), UPDATED)
        .unwrap();
    assert!(staged.is_none());
    assert_eq!(
        result
            .get("profile")
            .unwrap()
            .get("parameters")
            .unwrap()
            .get("max_tokens"),
        Some(&PyValue::Int("256".into()))
    );
    let disabled = owner(&document.replace("true", "false"));
    assert_eq!(
        disabled
            .apply_settings(&create_request(7), UPDATED)
            .unwrap_err(),
        conflict("model profile ID is already reserved")
    );
    let mismatched = owner(&document.replace("instruct-v1", "other-model"));
    assert_eq!(
        mismatched
            .apply_settings(&save_request(7, 1024), UPDATED)
            .unwrap_err(),
        value_error("model profile route identity changed")
    );
    let alias = owner(
        &document
            .replace("relay/local", "relay/other")
            .replace("relay/alias", "relay/local"),
    );
    assert_eq!(
        alias
            .apply_settings(&create_request(7), UPDATED)
            .unwrap_err(),
        conflict("model profile ID is already reserved")
    );
}

#[test]
fn no_op_equality_uses_python_numeric_and_dictionary_comparison() {
    let left = loads(r#"{"one":true,"other":[9007199254740992,-0.0,{"a":false,"b":4}]}"#).unwrap();
    let right = loads(r#"{"other":[9007199254740992.0,0,{"b":4.0,"a":0.0}],"one":1}"#).unwrap();
    assert!(python_equal(&left, &right));
    assert!(!python_equal(
        &loads("9007199254740993").unwrap(),
        &loads("9007199254740992.0").unwrap(),
    ));
}

#[test]
fn save_normalizes_legacy_parameter_pairs_after_python_key_deduplication() {
    let registry = owner(
        r#"{
            "version":"rumi.model-registry.store.v1","profile_id":"profile-1","revision":1,
            "profiles":{"relay/local":{
                "model_profile_id":"relay/local","model_id":"instruct-v1","display_name":"作業\nモデル",
                "requirements":{"preferred_provider_instance_id":"connection/local"},
                "parameters":[[true,"first"],[1,"second"],["True","string wins"],[1.0,"third"],
                              [-0.0,0.25],[0,0.5],["max_tokens",512],["API_KEY","drop"],"ab"]
            }},"aliases":{}
        }"#,
    );
    let (_, result) = registry
        .apply_settings(&save_request(1, 1024), UPDATED)
        .unwrap();
    let parameters = result.get("profile").unwrap().get("parameters").unwrap();
    assert_eq!(
        parameters,
        &loads(r#"{"True":"string wins","-0.0":0.5,"max_tokens":1024,"a":"b"}"#).unwrap()
    );
}
