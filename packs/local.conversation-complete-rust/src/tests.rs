use serde_json::{Value, json};

use super::*;

fn decoded(result: Result<String>) -> Value {
    serde_json::from_str(&result.unwrap()).unwrap()
}

#[test]
fn preparation_forwards_only_the_original_fields() {
    let input = r#"{"messages":[{"role":"user","content":"雪🦀"}],"model":"local/雪","model_reference":"other","params":{"temperature":0.2},"requirements":{"request_surface":"caller"},"tools":[{}]}"#;
    assert_eq!(
        prepare(input).unwrap(),
        r#"{"messages":[{"content":"雪🦀","role":"user"}],"model_reference":"local/雪","requirements":{"request_surface":"defaultspack.conversation"}}"#
    );
}

#[test]
fn preparation_rejects_non_initial_or_out_of_bounds_data() {
    for input in [
        r#"[[{}]]"#,
        r#"{"messages":[]}"#,
        r#"{"messages":[null]}"#,
        r#"{"messages":[{}],"continuation":null}"#,
        r#"{"messages":[{}],"bridge_result":false}"#,
        r#"{"messages":[{}],"model":null}"#,
        r#"{"messages":[{}],"model":"model\u007f"}"#,
        r#"{"messages":[{}],"model":"model\u0085"}"#,
        r#"{"messages":[{}],"model":""}"#,
        r#"{"messages":[{"sampling":0.25}]}"#,
        r#"{"messages":[{"sampling":9007199254740992}]}"#,
        r#"{"messages":[{"":true}]}"#,
    ] {
        assert!(matches!(prepare(input), Err(Error::Invalid(_))), "{input}");
    }
    let too_many = json!({"messages": vec![json!({}); 65]}).to_string();
    assert!(matches!(prepare(&too_many), Err(Error::Invalid(_))));
    let deepest = |levels: usize| {
        let mut nested = Value::Null;
        for _ in 0..levels {
            nested = json!([nested]);
        }
        json!({"messages":[{"extra":nested}]}).to_string()
    };
    assert!(prepare(&deepest(6)).is_ok());
    assert!(matches!(prepare(&deepest(7)), Err(Error::Invalid(_))));
    let max_model = "🦀".repeat(256);
    assert!(prepare(&json!({"messages":[{}],"model":max_model}).to_string()).is_ok());
    assert!(matches!(
        prepare(&json!({"messages":[{}],"model":"🦀".repeat(257)}).to_string()),
        Err(Error::Invalid(_))
    ));
}

#[test]
fn preparation_checks_code_points_and_compact_utf8_byte_capacity() {
    let input = |size: usize| {
        json!({"messages":[{"content":"é".repeat(8_192)},{"content":"a".repeat(16_384)},
            {"content":"a".repeat(16_384)},{"content":"a".repeat(size)}]})
        .to_string()
    };
    let overhead = prepare(&input(0)).unwrap().len();
    let remaining = MAX_REQUEST_BYTES - overhead;
    let result = prepare(&input(remaining)).unwrap();
    assert_eq!(result.len(), MAX_REQUEST_BYTES);
    assert_eq!(prepare(&input(remaining + 1)), Err(Error::Capacity));
    assert!(matches!(
        prepare(&json!({"messages":[{"content":"a".repeat(16_385)}]}).to_string()),
        Err(Error::Invalid(_))
    ));
}

#[test]
fn completion_keeps_provider_fields_and_projects_only_mapping_blocks_and_intents() {
    let tool = json!({
        "intent_id": 42,
        "operation": {"b": 1, "a": false},
        "arguments": [["key", 1], "雪🦀", {"a":0,"z":0}, ["key", 2]],
    });
    let output = json!([null, "ignored", {"type":"text","text":"雪"}]);
    let result = decoded(finish(
        &json!({"status":"ok","value":{
            "output":output,"other":null,"tool_intents":[false,tool],
            "tool_calls":"overwrite","content":"overwrite"
        }})
        .to_string(),
    ));
    assert_eq!(
        result,
        json!({
            "content":[{"type":"text","text":"雪"},{"type":"tool_use","id":"42",
                "name":"{'a': False, 'b': 1}","input":{"key":2,"雪":"🦀","a":"z"}}],
            "tool_calls":[tool],"output":output,"tool_intents":[false,tool],"other":null
        })
    );
    assert_eq!(
        decoded(finish(
            r#"{"status":"ok","value":{"output":false,"tool_intents":"ignored"}}"#
        ))["content"],
        json!([{"type":"text","text":"False"}])
    );
}

#[test]
fn completion_checks_exact_outcomes_and_passes_a_bounded_provider_error() {
    let failure = json!({"code":"PROVIDER_UNAVAILABLE","message":" 再接続 "});
    assert_eq!(
        decoded(finish(
            &json!({"status":"error","error":failure}).to_string()
        )),
        json!({"content":[],"tool_calls":[],"error":failure})
    );
    for input in [
        r#"{"status":"ok","value":[]}"#,
        r#"{"status":"ok","value":{},"error":{}}"#,
        r#"{"status":"ok","value":{"output":{}}}"#,
        r#"{"status":"ok","value":{"tool_intents":3}}"#,
        r#"{"status":"ok","value":{"tool_intents":[{"arguments":[[0,1]]}]}}"#,
        r#"{"status":"error","error":{"code":"lowercase","message":"bad"}}"#,
        r#"{"status":"error","error":{"code":"E","message":"\u001c\u0085"}}"#,
        r#"{"status":"error","error":{"code":"E","message":"m","extra":1}}"#,
    ] {
        assert!(matches!(finish(input), Err(Error::Invalid(_))), "{input}");
    }
    let oversized = json!({"status":"ok","value":{"output":vec!["🦀".repeat(8_192); 16]}});
    assert_eq!(finish(&oversized.to_string()), Err(Error::Capacity));
}
