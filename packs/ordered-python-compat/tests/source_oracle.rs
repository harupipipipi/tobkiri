//! Expected bytes and repr strings below come from the unchanged, frozen
//! CPython 3.13.5 oracle. This test never manufactures expected JSON with Rust.

use ordered_python_compat::{CodecError, MAX_BYTES, PyValue, dumps, loads, pyrepr};

#[test]
fn frozen_original_python_outputs_keep_exact_bytes_and_repr() {
    let raw = include_str!("../../local.ai-tool-bridge-ordered-python/oracle.json");
    assert_eq!(
        tobkiri_canon::hash_bytes(raw.as_bytes()).as_str(),
        "sha256:9aae37387a6b0758451abe3cb6070d8a3d8106b744de053a5bc16e8715f3481d"
    );
    let oracle: serde_json::Value = serde_json::from_str(raw).unwrap();
    assert_eq!(oracle["python"], "3.13.5");
    assert_eq!(oracle["unicode"], "15.1.0");
    let mut covered = 0;
    for case in oracle["cases"].as_array().unwrap() {
        if case["transport"]["status"] != "ok" {
            continue;
        }
        let bytes = case["transport"]["output_json"].as_str().unwrap();
        let value = loads(bytes).unwrap();
        assert_eq!(dumps(&value).unwrap(), bytes, "{}", case["id"]);
        assert_eq!(
            pyrepr(&value),
            case["original"]["repr"].as_str().unwrap(),
            "{}",
            case["id"]
        );
        covered += 1;
    }
    assert!(covered > 0);
}

#[test]
fn decoded_duplicate_keys_are_rejected_even_when_spelled_differently() {
    assert_eq!(loads(r#"{"a":1,"\u0061":2}"#), Err(CodecError::Invalid));
    let value = PyValue::object([
        ("z".into(), PyValue::Int("1".into())),
        ("a".into(), PyValue::Int("2".into())),
        ("z".into(), PyValue::Int("3".into())),
    ]);
    assert_eq!(dumps(&value).unwrap(), r#"{"z":3,"a":2}"#);
}

#[test]
fn integer_and_float_negative_zero_remain_distinct() {
    let value = loads("[-0,-0.0,1e-400,-1e-400]").unwrap();
    let values = value.as_array().unwrap();
    assert_eq!(values[0], PyValue::Int("0".into()));
    let bits = |value: &PyValue| match value {
        PyValue::Float(value) => value.to_bits(),
        _ => panic!("expected binary64"),
    };
    assert_eq!(bits(&values[1]), (-0.0f64).to_bits());
    assert_eq!(bits(&values[2]), 0.0f64.to_bits());
    assert_eq!(bits(&values[3]), (-0.0f64).to_bits());
}

#[test]
fn v2_digit_and_node_budgets_preserve_the_original_boundaries() {
    let integer = "9".repeat(4300);
    assert_eq!(dumps(&loads(&integer).unwrap()).unwrap(), integer);
    assert_eq!(loads(&"9".repeat(4301)), Err(CodecError::IntegerLimit));
    // Root array plus 99999 scalar values is exactly 100000 nodes.
    let exact = format!("[{}]", vec!["0"; 99_999].join(","));
    assert!(loads(&exact).is_ok());
    let exceeded = format!("[{}]", vec!["0"; 100_000].join(","));
    assert_eq!(loads(&exceeded), Err(CodecError::NodeLimit));
}

#[test]
fn malformed_syntax_and_nonfinite_values_fail_closed() {
    for malformed in [
        "",
        "[1,]",
        "{\"a\":1,}",
        "[}",
        "{]",
        "01",
        "-01",
        "+1",
        ".1",
        "1.",
        "1e",
        "true false",
        "[1 2]",
        "{\"a\" 1}",
        "{\"a\":}",
        "\"\\ud800\"",
    ] {
        assert!(loads(malformed).is_err(), "{malformed}");
    }
    for token in ["NaN", "Infinity", "-Infinity", "1e400"] {
        assert_eq!(loads(token), Err(CodecError::NonFinite));
    }
    assert_eq!(
        dumps(&PyValue::Float(f64::INFINITY)),
        Err(CodecError::NonFinite)
    );
}

#[test]
fn maximum_node_chain_is_dropped_without_recursive_destructors() {
    let text = format!("{}0{}", "[".repeat(99_999), "]".repeat(99_999));
    let value = loads(&text).unwrap();
    assert_eq!(dumps(&value).unwrap(), text);
    let cloned = value.clone();
    assert_eq!(dumps(&cloned).unwrap(), text);
    drop(cloned);
    drop(value);
    // The error branch must safely dispose of the completed deep prefix too.
    assert!(matches!(
        loads(&format!("{text}x")),
        Err(CodecError::Invalid)
    ));
}

#[test]
fn exact_input_byte_bound_and_expanding_string_output_keep_the_v2_limit() {
    let text = format!("\"{}\"", "a".repeat(MAX_BYTES - 2));
    let value = loads(&text).unwrap();
    assert_eq!(dumps(&value).unwrap(), text);
    assert!(matches!(
        loads(&format!("{text} ")),
        Err(CodecError::ByteLimit)
    ));
    // Each NUL uses six JSON bytes. The serializer must stop at the output
    // envelope while escaping a much smaller native string.
    assert_eq!(
        dumps(&PyValue::String("\0".repeat(MAX_BYTES / 6 + 1))),
        Err(CodecError::ByteLimit)
    );
}
