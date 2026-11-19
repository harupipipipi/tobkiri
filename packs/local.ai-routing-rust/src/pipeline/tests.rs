use super::{MAX_OUTPUT_BYTES, decide_failover, prepare_request};
use ordered_python_compat::{PyValue, loads};

#[test]
fn prepare_preserves_original_field_order_and_opaque_data() {
    let request = r#"{"decision_time":"　１_２.５　","deadline":[0],"request_id":"\u001c turn-é\u001f","messages":{"z":{},"a":null},"input":{"nested":[-0.0,9223372036854775808]},"parameters":[["z",1],"é😀",{"b":false,"c":2},["z",[true]]],"tools":"é😀","requirements":{"ignored":true,"thinking":false,"modalities":["text"],"health_max_age":null},"credential_handle":"opaque:example","idempotency_key":{"z":2,"a":1},"allow_failover":"false","policy_revision":[true,null,0.00001],"conversation_id":[0],"profile_id":false,"model_profile_id":0,"model_reference":{"z":1,"a":2},"ignored":1}"#;
    let expected = r#"{"request_id":"turn-é","decision_time":12.5,"deadline":72.5,"messages":["z","a"],"input":{"nested":[-0.0,9223372036854775808]},"parameters":{"z":[true],"é":"😀","b":"c"},"tools":["é","😀"],"requirements":{"thinking":false,"modalities":["text"],"health_max_age":null},"credential_handle":"opaque:example","idempotency_key":{"z":2,"a":1},"allow_failover":true,"policy_revision":"[True, None, 1e-05]","conversation_id":[0],"profile_id":false,"model_profile_id":0,"model_reference":{"z":1,"a":2}}"#;
    assert_eq!(prepare_request(request).as_deref(), Some(expected));
}

#[test]
fn prepare_coerces_defaults_and_keeps_the_other_credential_prefix() {
    let request = r#"{"decision_time":-0.0,"deadline":true,"request_id":{"z":null,"a":false},"messages":false,"parameters":0,"tools":"","requirements":[],"credential_handle":"credential:","policy_revision":[]}"#;
    let expected = r#"{"request_id":"{'z': None, 'a': False}","decision_time":-0.0,"deadline":1.0,"messages":[],"input":null,"parameters":{},"tools":[],"requirements":{},"credential_handle":"credential:","idempotency_key":null,"allow_failover":false,"policy_revision":"","conversation_id":null,"profile_id":null,"model_profile_id":null,"model_reference":null}"#;
    assert_eq!(prepare_request(request).as_deref(), Some(expected));
}

#[test]
fn prepare_declines_original_exceptions_and_unsupported_codec_values() {
    for request in [
        r#"[]"#,
        r#"{"request_id":"r"}"#,
        r#"{"request_id":"　","decision_time":1}"#,
        r#"{"request_id":"r","decision_time":1,"deadline":1}"#,
        r#"{"request_id":"r","decision_time":"nan"}"#,
        r#"{"request_id":"r","decision_time":1,"deadline":"inf"}"#,
        r#"{"request_id":"r","decision_time":1e100}"#,
        r#"{"request_id":"r","decision_time":1,"credential_handle":" opaque:secret"}"#,
        r#"{"request_id":"r","decision_time":1,"credential_handle":[]}"#,
        r#"{"request_id":"r","decision_time":1,"messages":true}"#,
        r#"{"request_id":"r","decision_time":1,"tools":1}"#,
        r#"{"request_id":"r","decision_time":1,"parameters":"xy"}"#,
        r#"{"request_id":"r","decision_time":1,"parameters":[[5,1]]}"#,
        r#"{"request_id":"r","decision_time":1,"parameters":[["k","v","late"]]}"#,
        r#"{"request_id":"r","decision_time":1,"input":{"k":1,"k":2}}"#,
        r#"{"request_id":"r","decision_time":1,"input":"\ud800"}"#,
    ] {
        assert_eq!(prepare_request(request), None, "{request}");
    }
}

#[test]
fn failover_uses_precise_integer_coercion_and_ordered_guards() {
    let request = r#"{"allow_failover":"false","idempotency_key":[false],"tools":{},"error_code":"invalid_response","attempt":" １２_３４５６７８９０１２３４５６７８９０１２３４５６７８９０ ","candidate_count":123456789012345678901234567891,"deadline":true,"decision_time":-0.0}"#;
    let expected = r#"{"allowed":true,"checks":{"explicitly_allowed":true,"idempotency_bound":true,"tool_free":true,"retryable_error":true,"candidate_available":true,"deadline_remaining":true},"reason":"replay_safe_failover"}"#;
    assert_eq!(decide_failover(request).as_deref(), Some(expected));

    let request = r#"{"allow_failover":[],"idempotency_key":"","tools":" ","error_code":" quota","attempt":9007199254740992.0,"candidate_count":"9007199254740992","deadline":"NaN","decision_time":0}"#;
    let expected = r#"{"allowed":false,"checks":{"explicitly_allowed":false,"idempotency_bound":false,"tool_free":false,"retryable_error":false,"candidate_available":false,"deadline_remaining":false},"reason":"explicitly_allowed"}"#;
    assert_eq!(decide_failover(request).as_deref(), Some(expected));
}

#[test]
fn failover_uses_the_first_failed_guard_through_the_last_check() {
    let cases = [
        ("{}", "explicitly_allowed"),
        (r#"{"allow_failover":true}"#, "idempotency_bound"),
        (
            r#"{"allow_failover":true,"idempotency_key":true,"tools":[null]}"#,
            "tool_free",
        ),
        (
            r#"{"allow_failover":true,"idempotency_key":true,"error_code":"timeout"}"#,
            "retryable_error",
        ),
        (
            r#"{"allow_failover":true,"idempotency_key":true,"error_code":"provider_unavailable","attempt":-2.8,"candidate_count":-1}"#,
            "candidate_available",
        ),
        (
            r#"{"allow_failover":true,"idempotency_key":true,"error_code":"quota","attempt":[],"candidate_count":"+２","deadline":{},"decision_time":1}"#,
            "deadline_remaining",
        ),
        (
            r#"{"allow_failover":true,"idempotency_key":true,"error_code":"quota","attempt":"-２","candidate_count":2.9,"deadline":"Infinity","decision_time":1}"#,
            "replay_safe_failover",
        ),
    ];
    for (request, reason) in cases {
        let result = loads(&decide_failover(request).unwrap()).unwrap();
        assert_eq!(
            result.get("reason").and_then(PyValue::as_str),
            Some(reason),
            "{request}"
        );
    }
}

#[test]
fn failover_declines_raised_conversions_even_when_earlier_guards_would_fail() {
    for request in [
        r#"null"#,
        r#"{"attempt":" "}"#,
        r#"{"attempt":[1]}"#,
        r#"{"candidate_count":"1.0"}"#,
        r#"{"deadline":1e999}"#,
    ] {
        assert_eq!(decide_failover(request), None, "{request}");
    }
    let enormous = "9".repeat(309);
    assert!(decide_failover(&format!(r#"{{"candidate_count":{enormous}}}"#)).is_some());
    assert_eq!(
        decide_failover(&format!(r#"{{"deadline":{enormous}}}"#)),
        None
    );
}

#[test]
fn gateway_ports_bound_encoded_input_and_prepare_output_bytes() {
    for (seed, operation) in [
        (
            r#"{"request_id":"r","decision_time":1}"#,
            prepare_request as fn(&str) -> Option<String>,
        ),
        ("{}", decide_failover),
    ] {
        let mut request = seed.to_owned();
        request.push_str(&" ".repeat(MAX_OUTPUT_BYTES - request.len()));
        assert!(operation(&request).is_some());
        request.push(' ');
        assert_eq!(operation(&request), None);
    }

    let with_input =
        |value: &str| format!(r#"{{"request_id":"r","decision_time":1,"input":"{value}"}}"#);
    let base = prepare_request(&with_input("")).unwrap().len();
    let mut value = "é".repeat((MAX_OUTPUT_BYTES - base) / 2);
    value.push_str(&"x".repeat((MAX_OUTPUT_BYTES - base) % 2));
    assert_eq!(
        prepare_request(&with_input(&value)).unwrap().len(),
        MAX_OUTPUT_BYTES
    );
    value.push('x');
    assert_eq!(prepare_request(&with_input(&value)), None);
}
