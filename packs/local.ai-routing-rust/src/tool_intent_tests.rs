use super::normalize_tool_intents;

const MAX_BYTES: usize = 1024 * 1024;

#[test]
fn tool_intents_keep_gateway_descriptor_and_argument_order_without_authority() {
    let payload = r#"{"request_id":"\u001c req\u3000","intents":[{"id":"  provider-id  ","name":"ignored","function":{"name":"\u00a0demo.v1:two-test_3\u001f","arguments":"{\"z\":-0.0,\"a\":{\"y\":2,\"x\":1},\"z\":1e-7,\"approved\":true,\"esc\":\"\\\\[]\\\"{}\",\"surrogate\":\"\\ud800\",\"surrogate\":\"😀\"}"},"authority_granted":true,"approved":true,"approval_status":"approved","executes":true},{"name":"outer","function":{"name":false},"arguments":{"beta":2,"alpha":1},"id":0},{"name":7,"function":"ignored"}]}"#;
    let expected = r#"{"intents":[{"intent_id":"  provider-id  ","request_id":"req","operation":"demo.v1:two-test_3","arguments":{"z":1e-07,"a":{"y":2,"x":1},"approved":true,"esc":"\\[]\"{}","surrogate":"😀"},"authority_granted":false,"approved":false,"approval_status":"unrequested","executes":false},{"intent_id":"req:tool:1","request_id":"req","operation":"outer","arguments":{"beta":2,"alpha":1},"authority_granted":false,"approved":false,"approval_status":"unrequested","executes":false},{"intent_id":"req:tool:2","request_id":"req","operation":"7","arguments":{},"authority_granted":false,"approved":false,"approval_status":"unrequested","executes":false}]}"#;
    assert_eq!(normalize_tool_intents(payload).as_deref(), Some(expected));
}

#[test]
fn tool_intents_use_python_truthiness_and_container_stringification() {
    let payload = r#"{"request_id":["café",false],"intents":[{"id":{"x":"a'b","y":[null,false,-0.0]},"name":"valid","function":{"name":[]}}]}"#;
    let expected = r#"{"intents":[{"intent_id":"{'x': \"a'b\", 'y': [None, False, -0.0]}","request_id":"['café', False]","operation":"valid","arguments":{},"authority_granted":false,"approved":false,"approval_status":"unrequested","executes":false}]}"#;
    assert_eq!(normalize_tool_intents(payload).as_deref(), Some(expected));

    for payload in [r#"{"intents":null}"#, r#"{"intents":{"name":"ignored"}}"#] {
        assert_eq!(
            normalize_tool_intents(payload).as_deref(),
            Some(r#"{"intents":[]}"#)
        );
    }
}

#[test]
fn tool_intents_decline_invalid_and_unsupported_inputs_without_partial_results() {
    for payload in [
        r#"[]"#,
        r#"{"intents":[{"name":"ok"},0]}"#,
        r#"{"intents":[{"name":"ok","function":{"name":"bad name"}}]}"#,
        r#"{"intents":[{"name":"ok","function":{"arguments":null},"arguments":{}}]}"#,
        r#"{"intents":[{"name":"ok","arguments":"[]"}]}"#,
        r#"{"intents":[{"name":"ok","arguments":"{\"x\":}"}]}"#,
        r#"{"intents":[{"name":"ok","arguments":"{\"x\":-Infinity}"}]}"#,
        r#"{"intents":[{"name":"ok","arguments":"{\"x\":\"\\ud800\"}"}]}"#,
        r#"{"intents":[{"name":"ok","arguments":{"x":1,"x":2}}]}"#,
    ] {
        assert_eq!(normalize_tool_intents(payload), None, "{payload}");
    }
}

#[test]
fn tool_intents_bound_the_request_and_wrapped_response_in_bytes() {
    let mut request = String::from(r#"{"request_id":"req","intents":[]}"#);
    request.push_str(&" ".repeat(MAX_BYTES - request.len()));
    assert!(normalize_tool_intents(&request).is_some());
    request.push(' ');
    assert_eq!(normalize_tool_intents(&request), None);

    let prefix = r#"{"request_id":"req","intents":[{"name":"echo","arguments":{"s":""#;
    let suffix = r#""}}]}"#;
    let empty_output_bytes = normalize_tool_intents(&format!("{prefix}{suffix}"))
        .unwrap()
        .len();
    let request = |extra| {
        format!(
            "{prefix}{}{suffix}",
            "x".repeat(MAX_BYTES - empty_output_bytes + extra)
        )
    };
    assert_eq!(
        normalize_tool_intents(&request(0)).unwrap().len(),
        MAX_BYTES
    );
    assert_eq!(normalize_tool_intents(&request(1)), None);
    let many_intents = r#"{"name":"echo"},"#.repeat(64);
    let repeated_id = format!(
        r#"{{"request_id":"{}","intents":[{many_intents}{{"name":"echo"}}]}}"#,
        "é".repeat(8192),
    );
    assert_eq!(normalize_tool_intents(&repeated_id), None);
}

#[test]
fn tool_intents_decline_deep_or_wide_embedded_json_before_projecting() {
    let deep = format!(
        r#"{{"intents":[{{"name":"echo","arguments":"{{\"a\":{}0{}}}"}}]}}"#,
        "[".repeat(128),
        "]".repeat(128),
    );
    assert_eq!(normalize_tool_intents(&deep), None);
    let wide = format!(
        r#"{{"intents":[{{"name":"echo","arguments":"{{\"a\":[{}0]}}"}}]}}"#,
        "0,".repeat(100_000),
    );
    assert_eq!(normalize_tool_intents(&wide), None);
}
