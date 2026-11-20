use std::{path::Path, process::Command};

use serde_json::Value;
use tobkiri_pack_workflow_messages_rust::{Error, build};

#[test]
fn rejects_ambiguous_or_oversized_wire_input() {
    for input in [
        r#"{"conversation":{"messages":[]},"conversation":{},"system_prompt":null}"#,
        r#"{"conversation":{"messages":[],"bad":"\ud800"},"system_prompt":null}"#,
    ] {
        assert!(build(input).is_err());
    }
    assert_eq!(
        build(&" ".repeat(512 * 1024 + 1)),
        Err(Error::Capacity("json input exceeds size limit"))
    );
}

#[test]
fn rejects_noncanonical_tool_argument_strings() {
    for arguments in [
        r#"{"a":1,"a":2}"#,
        r#"{"number": NaN}"#,
        r#"{"number": Infinity}"#,
        r#"{"surrogate": "\ud800"}"#,
        r#"{"missing": }"#,
    ] {
        let payload = serde_json::json!({
            "conversation": {
                "current_node_id": "one",
                "messages": [{
                    "id": "one",
                    "role": "assistant",
                    "status": "complete",
                    "content": "done",
                    "metadata": {
                        "saved_tool_messages": [
                            {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [{
                                    "id": "call.1",
                                    "type": "function",
                                    "function": {"name": "example", "arguments": arguments},
                                }],
                            },
                            {"role": "tool", "tool_call_id": "call.1", "content": "result"},
                        ],
                    },
                    "tool_logs": [{
                        "tool_name": "example",
                        "tool_call_id": "call.1",
                        "arguments": {"a": 2},
                        "result": "result",
                    }],
                }],
            },
            "system_prompt": null,
        });
        assert!(
            build(&payload.to_string()).is_err(),
            "accepted noncanonical tool arguments {arguments}",
        );
    }
}

#[test]
fn preserves_empty_history_and_text_block_order() {
    assert_eq!(
        build(r#"{"system_prompt":null,"conversation":{"messages":[]}}"#).unwrap(),
        r#"{"messages":[]}"#,
    );
    assert_eq!(
        build(concat!(
            r#"{"conversation":{"current_node_id":"known","messages":["#,
            r#"{"status":"complete","role":"user","id":"known","content":["#,
            r#"{"type":"text","text":"日本語"},{"text":"🌱","type":"text"}]}]},"system_prompt":null}"#
        ))
        .unwrap(),
        r#"{"messages":[{"content":[{"text":"日本語","type":"text"},{"text":"🌱","type":"text"}],"role":"user"}]}"#,
    );
}

#[test]
#[ignore = "requires an explicit Python interpreter for the original guest oracle"]
fn matches_production_python_messages_and_denials() {
    let python = std::env::var_os("TOBKIRI_WORKFLOW_MESSAGES_ORACLE_PYTHON")
        .expect("set TOBKIRI_WORKFLOW_MESSAGES_ORACLE_PYTHON");
    let source = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/export_reference.py");
    let oracle = Command::new(python)
        .args(["-I", "-S", "-B"])
        .arg(source)
        .output()
        .unwrap();
    assert!(
        oracle.status.success(),
        "{}",
        String::from_utf8_lossy(&oracle.stderr)
    );

    let cases: Vec<Value> = serde_json::from_slice(&oracle.stdout).unwrap();
    assert!(cases.len() >= 60);
    for case in cases {
        let input = serde_json::to_string(&case["input"]).unwrap();
        let actual = build(&input);
        let name = &case["name"];
        if case["output"].is_null() {
            let error = actual.expect_err("the production stage refused this input");
            if let Some(expected) = case["error"].as_str() {
                assert_eq!(error.to_string(), expected, "{name}");
            }
            assert_eq!(
                matches!(error, Error::Capacity(_)),
                case["code"] == "LimitExceeded",
                "{name}",
            );
        } else {
            let expected = serde_json::to_string(&case["output"]).unwrap();
            assert_eq!(actual.as_deref(), Ok(expected.as_str()), "{name}");
        }
    }
}
