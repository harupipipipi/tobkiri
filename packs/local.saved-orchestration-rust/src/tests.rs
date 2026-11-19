use super::*;
use serde_json::json;
use std::{path::Path, process::Command};

#[test]
fn strict_wire_rejects_duplicates_extra_fields_and_unbounded_data() {
    for document in [
        r#"{"request":{},"request":{}}"#,
        r#"{"request":{"n":1.2}}"#,
        r#"{"request":{"n":9007199254740992}}"#,
        r#"{"request":{},"_session_id":"injected"}"#,
        r#"{"request":{},"state":{},"outcome":{}}"#,
    ] {
        assert!(
            matches!(advance(document), Err(Error::Invalid(_))),
            "{document}"
        );
    }
    assert_eq!(advance(&" ".repeat(STATE_LIMIT + 1)), Err(Error::Capacity));
}

#[test]
fn original_json_byte_order_and_step_limits() {
    let value = json!({"🙂": 0, "é": ["\t", "日本語"], "a": "\u{001c}"});
    let expected = r#"{"a":"\u001c","é":["\t","日本語"],"🙂":0}"#;
    assert_eq!(
        canonical_bytes(&value, expected.len()).unwrap(),
        expected.as_bytes()
    );
    assert_eq!(
        canonical_bytes(&value, expected.len() - 1),
        Err(Error::Capacity)
    );
    let deep = (0..13).fold(Value::Null, |item, _| Value::Array(vec![item]));
    assert!(matches!(
        canonical_bytes(&deep, STATE_LIMIT),
        Err(Error::Invalid(_))
    ));
}

#[test]
#[ignore = "requires an explicit Python interpreter for the original guest oracle"]
fn original_python_owns_the_differential_expectations() {
    let python = std::env::var_os("TOBKIRI_SAVED_ORCHESTRATION_ORACLE_PYTHON")
        .expect("set TOBKIRI_SAVED_ORCHESTRATION_ORACLE_PYTHON");
    let source = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tobkiri_runtime");
    let oracle = Command::new(python)
        .current_dir(source)
        .args(["-B", "-c", include_str!("oracle_cases.py")])
        .output()
        .unwrap();
    assert!(
        oracle.status.success(),
        "{}",
        String::from_utf8_lossy(&oracle.stderr)
    );
    let cases: Vec<Value> = serde_json::from_slice(&oracle.stdout).unwrap();
    assert!(cases.len() >= 40);
    for case in cases {
        let input = serde_json::to_string(&case["input"]).unwrap();
        if case["ok"] == false {
            assert!(
                matches!(advance(&input), Err(Error::Invalid(_))),
                "{}",
                case["name"]
            );
        } else {
            let expected = serde_json::to_string(&case["output"]).unwrap();
            assert_eq!(
                advance(&input).as_deref(),
                Ok(expected.as_str()),
                "{}",
                case["name"]
            );
        }
    }
}
