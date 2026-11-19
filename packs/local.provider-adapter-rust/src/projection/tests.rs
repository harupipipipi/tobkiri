use super::*;

fn check(raw: &str, stream: bool, expected: &str) {
    assert_eq!(
        project_local_completion(raw, stream).as_deref(),
        Some(expected),
        "{raw}"
    );
}

#[test]
fn projects_first_choice_and_preserves_order_and_python_numbers() {
    check(
        r#"{"usage":{"total_tokens":2,"prompt_tokens":1.0,"extra":-0.0},"choices":[{"finish_reason":"tool_calls","message":{"content":"猫🦊","tool_calls":[{"id":"call_1","function":{"name":"lookup","arguments":"{}"}}]}},{"message":{"content":"ignored"}}]}"#,
        false,
        r#"{"output":"猫🦊","tool_intents":[{"id":"call_1","function":{"name":"lookup","arguments":"{}"}}],"usage":{"total_tokens":2,"prompt_tokens":1.0,"extra":-0.0},"finish_reason":"tool_calls"}"#,
    );
}

#[test]
fn stream_uses_python_str_and_keeps_non_object_intents_only_in_terminal_fields() {
    check(
        r#"{"choices":[{"message":{"content":{"s":"猫\u200b","n":null,"v":[true,-0.0,1e-7]},"tool_calls":[{"id":"one"},9,{},"ignored",null]},"finish_reason":false}],"usage":{"completion_tokens":9}}"#,
        true,
        r#"{"output":{"s":"猫​","n":null,"v":[true,-0.0,1e-07]},"tool_intents":[{"id":"one"},9,{},"ignored",null],"usage":{"completion_tokens":9},"finish_reason":false,"events":[{"type":"text_delta","delta":"{'s': '猫\\u200b', 'n': None, 'v': [True, -0.0, 1e-07]}"},{"type":"tool_intent_delta","tool_intent":{"id":"one"}},{"type":"tool_intent_delta","tool_intent":{}},{"type":"usage","usage":{"completion_tokens":9}},{"type":"finish","finish_reason":false}]}"#,
    );
}

#[test]
fn matches_empty_and_falsy_python_values() {
    for raw in [
        "{}",
        r#"{"choices":{"message":{"content":"ignored"}},"usage":0}"#,
        r#"{"choices":[null,{"message":{"content":"ignored"}}],"usage":false}"#,
        r#"{"choices":[{"message":{"content":null,"tool_calls":""}}],"usage":[]}"#,
    ] {
        check(
            raw,
            true,
            r#"{"output":"","tool_intents":[],"usage":{},"finish_reason":null,"events":[{"type":"usage","usage":{}},{"type":"finish","finish_reason":null}]}"#,
        );
    }
    for output in ["false", "0", "-0.0", "[]", "{}"] {
        check(
            &format!(r#"{{"choices":[{{"message":{{"content":{output},"tool_calls":-0.0}}}}]}}"#),
            true,
            &format!(
                r#"{{"output":{output},"tool_intents":[],"usage":{{}},"finish_reason":null,"events":[{{"type":"usage","usage":{{}}}},{{"type":"finish","finish_reason":null}}]}}"#
            ),
        );
    }
}

#[test]
fn supports_unicode_iterables_and_replaces_duplicate_keys_in_place() {
    check(
        r#"{"choices":[{"message":{"content":"original","tool_calls":"猫🦊","\u0063ontent":{"p":0,"q":1,"p":2}}}],"usage":[["cat",1],"鳥🦜",{"one":16,"two":17},["cat",123456789012345678901234567890]]}"#,
        false,
        r#"{"output":{"p":2,"q":1},"tool_intents":["猫","🦊"],"usage":{"cat":123456789012345678901234567890,"鳥":"🦜","one":"two"},"finish_reason":null}"#,
    );
    check(
        r#"{"choices":[{"message":{"content":"escaped: \"[{},]:\\","tool_calls":{"a":1,"b":2,"\u0061":3}}}]}"#,
        false,
        r#"{"output":"escaped: \"[{},]:\\","tool_intents":["a","b"],"usage":{},"finish_reason":null}"#,
    );
}

#[test]
fn leaves_invalid_and_unrepresentable_values_to_python() {
    for raw in [
        "{",
        "null",
        "[]",
        "\u{feff}{}",
        "{\"a\":\"\u{0001}\"}",
        r#"{"choices":[{"message":{"content":"\ud800"}}]}"#,
        r#"{"choices":[{"message":{"content":NaN}}]}"#,
        r#"{"choices":[{"message":{"content":1e1000}}]}"#,
        r#"{"choices":[{"message":{"tool_calls":1}}]}"#,
        r#"{"usage":[[1,4],["1",5]]}"#,
        r#"{"usage":[["a",1],["b"]]}"#,
        r#"{"usage":[false]}"#,
        r#"{"usage":"あ"}"#,
    ] {
        for stream in [false, true] {
            assert_eq!(project_local_completion(raw, stream), None, "{raw}");
        }
    }
}

#[test]
fn leaves_over_budget_responses_and_projections_to_python() {
    let raw = format!("{{}}{}", " ".repeat(MAX_BYTES - 2));
    assert!(project_local_completion(&raw, false).is_some());
    assert!(project_local_completion(&(raw + " "), false).is_none());

    let output = "a".repeat(MAX_BYTES / 2);
    let raw = format!(r#"{{"choices":[{{"message":{{"content":"{output}"}}}}]}}"#);
    assert!(project_local_completion(&raw, false).is_some());
    assert!(project_local_completion(&raw, true).is_none());

    let left = "[".repeat(MAX_PROJECTION_DEPTH);
    let right = "]".repeat(MAX_PROJECTION_DEPTH);
    let raw = format!(r#"{{"shadowed":{left}0{right},"shadowed":0}}"#);
    assert!(project_local_completion(&raw, false).is_none());

    let calls = "a".repeat(MAX_NODES + 1);
    let raw = format!(r#"{{"choices":[{{"message":{{"tool_calls":"{calls}"}}}}]}}"#);
    assert!(project_local_completion(&raw, false).is_none());
}
