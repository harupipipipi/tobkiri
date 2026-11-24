use ordered_python_compat::{
    CodecError, PyValue, PythonJson, dumps_python_json, loads, loads_python_json,
};

fn opaque_dumps(input: &str) -> Result<String, CodecError> {
    dumps_python_json(&loads_python_json(input)?)
}

#[test]
fn opaque_decoded_duplicates_replace_the_first_position_only() {
    let input = r#"{"z":1,"a":2,"\u007a":3,"a":4}"#;
    assert_eq!(loads(input), Err(CodecError::Invalid));
    assert_eq!(opaque_dumps(input).unwrap(), r#"{"z":3,"a":4}"#);
}

#[test]
fn opaque_nonfinite_values_are_kept_until_the_finite_output_boundary() {
    for input in ["NaN", "Infinity", "-Infinity", "1e400", "-1e400"] {
        assert!(loads_python_json(input).is_ok());
        assert_eq!(opaque_dumps(input), Err(CodecError::NonFinite));
        assert_eq!(loads(input), Err(CodecError::NonFinite));
    }
    // An overwritten nonfinite value is absent from Python's final dict.
    assert_eq!(opaque_dumps(r#"{"x":NaN,"x":0}"#).unwrap(), r#"{"x":0}"#);
}

#[test]
fn opaque_input_has_no_new_node_cap_before_duplicate_collapse() {
    let mut text = String::from("{");
    for index in 0..100_001 {
        if index != 0 {
            text.push(',');
        }
        text.push_str(r#""x":0"#);
    }
    text.push('}');
    assert_eq!(opaque_dumps(&text).unwrap(), r#"{"x":0}"#);
}

#[test]
fn opaque_integer_and_float_identity_keep_python_rules() {
    assert_eq!(
        opaque_dumps("[-0,-0.0,1e-400,-1e-400]").unwrap(),
        "[0,-0.0,0.0,-0.0]"
    );
    assert!(loads_python_json(&"9".repeat(4300)).is_ok());
    assert_eq!(
        loads_python_json(&"9".repeat(4301)).unwrap_err(),
        CodecError::IntegerLimit
    );
}

#[test]
fn lone_surrogates_survive_parsing_and_fail_only_at_output() {
    for input in [r#"{"x":"\ud800"}"#, r#"{"\udfff":0}"#] {
        assert!(loads_python_json(input).is_ok());
        assert_eq!(opaque_dumps(input), Err(CodecError::Invalid));
        assert_eq!(loads(input), Err(CodecError::Invalid));
    }
    assert_eq!(
        opaque_dumps(r#"{"x":"\ud83d\ude00"}"#).unwrap(),
        "{\"x\":\"😀\"}"
    );
    assert_eq!(
        opaque_dumps(r#"{"x":"\\ud800"}"#).unwrap(),
        r#"{"x":"\\ud800"}"#
    );
    // Replacement drops the original value, including its lone surrogate.
    assert_eq!(
        opaque_dumps(r#"{"x":"\ud800","\u0078":0}"#).unwrap(),
        r#"{"x":0}"#
    );
}

#[test]
fn opaque_keys_use_decoded_python_code_points_without_marker_collisions() {
    assert_eq!(
        opaque_dumps(r#"{"😀":1,"\ud83d\ude00":2,"\\ud800":3}"#).unwrap(),
        "{\"😀\":2,\"\\\\ud800\":3}"
    );
    // Distinct surrogate keys must not collapse to an empty scalar key. The
    // first surrogate's overwritten NaN disappears, so its key fails first.
    assert_eq!(
        opaque_dumps(r#"{"\ud800":NaN,"\ud801":1,"\uD800":0}"#),
        Err(CodecError::Invalid)
    );
}

#[test]
fn final_output_traversal_selects_surrogate_or_nonfinite_in_order() {
    for input in [
        r#"[NaN,"\ud800"]"#,
        r#"{"n":NaN,"s":"\ud800"}"#,
        r#"{"n":NaN,"\ud800":0}"#,
    ] {
        assert_eq!(opaque_dumps(input), Err(CodecError::NonFinite));
    }
    for input in [
        r#"["\ud800",NaN]"#,
        r#"{"s":"\ud800","n":NaN}"#,
        r#"{"\ud800":0,"n":NaN}"#,
    ] {
        assert_eq!(opaque_dumps(input), Err(CodecError::Invalid));
    }
    for malformed in [r#"["\ud800",]"#, r#"{"\ud800":0 "x":NaN}"#] {
        assert_eq!(
            loads_python_json(malformed).unwrap_err(),
            CodecError::Invalid
        );
    }
}

#[test]
fn mixed_output_keeps_native_and_opaque_strings_separate() {
    let value = PythonJson::object([
        ("native".into(), PyValue::String("\\ud800".into()).into()),
        (
            "opaque".into(),
            loads_python_json(r#"{"literal":"\\ud800"}"#).unwrap(),
        ),
    ]);
    assert_eq!(
        dumps_python_json(&value).unwrap(),
        r#"{"native":"\\ud800","opaque":{"literal":"\\ud800"}}"#
    );
}
