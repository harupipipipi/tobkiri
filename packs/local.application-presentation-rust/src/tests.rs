use super::*;
use serde_json::json;

fn read(payload: &Value) -> Result<String, &'static str> {
    invoke(OPERATION, &payload.to_string())
}

#[test]
fn unicode_and_duplicate_options_only_change_the_four_model_fields() {
    let options = json!([
        {"label": "雪🦊", "value": "ローカル/モデル"},
        {"value": "ローカル/モデル", "label": "雪🦊"},
    ]);
    let result = read(&json!({
        "kind": "ui", "model_options": options, "profile_id": "表示用",
        "_session_id": "context"
    }))
    .unwrap();
    let mut projected: Value = serde_json::from_str(&result).unwrap();
    let model_fields = projected["sections"]
        .as_array_mut()
        .unwrap()
        .iter_mut()
        .find(|section| section["id"] == "models")
        .unwrap()["fields"]
        .as_array_mut()
        .unwrap();
    let mut patched = Vec::new();
    for field in model_fields {
        let id = field["id"].as_str().unwrap();
        if MODEL_FIELDS.contains(&id) {
            patched.push(id.to_owned());
            // Serializing each value also checks the insertion order of its keys.
            assert_eq!(field["options"].to_string(), options.to_string());
            field["options"] = json!([]);
        }
    }
    assert_eq!(patched, MODEL_FIELDS);
    assert_eq!(
        projected.to_string(),
        serde_json::from_str::<Value>(UI).unwrap().to_string()
    );
    assert_eq!(
        read(&json!({"kind": "ui", "model_options": []})).unwrap(),
        read(&json!({"kind": "ui"})).unwrap()
    );
}

#[test]
fn command_presentation_preserves_order_and_unavailable_bindings() {
    let result = read(&json!({"kind": "commands", "_session_id": "session"})).unwrap();
    let commands: Value = serde_json::from_str(&result).unwrap();
    let items = commands["commands"].as_array().unwrap();
    assert_eq!(items.len(), 50);
    assert_eq!(items[0]["canonical_id"], "defaultspack:help");
    assert!(items.iter().all(|command| {
        command["availability"]["status"] == "unavailable"
            && command["availability"]["reason_code"] == "canonical_binding_missing"
    }));
    assert_eq!(result, read(&json!({"kind": "commands"})).unwrap());
}

#[test]
fn malformed_requests_return_the_corresponding_guest_error() {
    assert_eq!(
        invoke("defaultspack.execute", "{}"),
        Err("application presentation operation is invalid")
    );
    for payload in ["{", "[]", r#"{"kind":"ui","approved":true}"#] {
        assert_eq!(invoke(OPERATION, payload), Err(INVALID_FIELDS));
    }

    for payload in [
        json!({}),
        json!({"kind": false}),
        json!({"kind": "execute"}),
        json!({"kind": "commands", "model_options": []}),
        json!({"kind": "commands", "model_options": null}),
    ] {
        assert_eq!(
            read(&payload),
            Err("application presentation kind is invalid")
        );
    }
    for payload in [
        json!({"kind": "ui", "model_options": {}}),
        json!({"kind": "ui", "model_options": vec![json!({}); MAX_OPTIONS + 1]}),
    ] {
        assert_eq!(read(&payload), Err("model display options are invalid"));
    }
    for option in [
        json!([]),
        json!({"value": "model"}),
        json!({"value": "model", "label": "Model", "api_key": "ignored"}),
    ] {
        assert_eq!(
            read(&json!({"kind": "ui", "model_options": [option]})),
            Err("model display option fields are invalid")
        );
    }
    for value in [
        json!(true),
        json!(""),
        json!("雪".repeat(MAX_OPTION_CHARS + 1)),
    ] {
        assert_eq!(
            read(&json!({"kind": "ui", "model_options": [{"value": value, "label": "model"}]})),
            Err("model display option value is invalid")
        );
    }
    assert!(
        read(&json!({
            "kind": "ui",
            "model_options": [{"value": "🦊".repeat(MAX_OPTION_CHARS), "label": "model"}]
        }))
        .is_ok()
    );
}

#[test]
fn limit_counts_python_ascii_escapes_and_separator_spaces() {
    let base_length = r#"{"kind": "commands", "_session_id": ""}"#.len();
    let unicode = "🦊雪\u{7f}\n\"\\\u{0}";
    let escaped_length = 12 + 6 + 6 + 2 + 2 + 2 + 6;
    let at_limit = format!(
        "{unicode}{}",
        "x".repeat(INPUT_LIMIT - base_length - escaped_length)
    );
    assert!(read(&json!({"kind": "commands", "_session_id": at_limit})).is_ok());
    assert_eq!(
        read(&json!({"kind": "commands", "_session_id": format!("{at_limit}x")})),
        Err("application presentation input exceeds limit")
    );
}
