use super::{MAX_OUTPUT_BYTES, evaluate, normalize_stream_result};
use ordered_python_compat::{PyValue, loads};

fn route(models: &str, providers: &str, health: &str, requirements: &str) -> Option<String> {
    evaluate(&format!(
        r#"{{"models":[{models}],"execution_providers":[{providers}],"health":{health},"requirements":{requirements},"decision_time":100}}"#
    ))
}

const WILDCARD: &str = r#"{"provider_instance_id":"wild","routing_keys":"*"}"#;

fn field<'a>(value: &'a PyValue, name: &str) -> &'a str {
    value.get(name).unwrap().as_str().unwrap()
}

#[test]
fn stream_result_preserves_the_original_projection_and_nested_order() {
    let request = r#"{"request_id":" \u2003request\u00a0","provider_attempt":"２_１","value":{"events":[{"type":"text_delta","delta":{"z":1,"a":true},"sequence":99},{"type":"tool_intent_delta","tool_intent":{"z":null,"a":"雪"}},{"type":"error","error_code":"failed","extra":"drop"}]}}"#;
    let result = normalize_stream_result(request).unwrap();
    assert_eq!(
        result,
        r#"{"events":[{"request_id":"request","sequence":0,"type":"text_delta","delta":{"z":1,"a":true},"tool_intent":null,"usage":null,"finish_reason":null,"error_code":null,"provider_attempt":21},{"request_id":"request","sequence":1,"type":"tool_intent_delta","delta":null,"tool_intent":{"z":null,"a":"雪"},"usage":null,"finish_reason":null,"error_code":null,"provider_attempt":21},{"request_id":"request","sequence":2,"type":"error","delta":null,"tool_intent":null,"usage":null,"finish_reason":null,"error_code":"failed","provider_attempt":21}]}"#
    );
}

#[test]
fn stream_result_declines_invalid_requests_and_streams() {
    for request in [
        "[]",
        r#"{"request_id":"r","value":[{"type":"finish","usage":NaN}]}"#,
        r#"{"request_id":"  ","value":[{"type":"finish"}]}"#,
        r#"{"request_id":"r","provider_attempt":[],"value":[{"type":"unknown"}]}"#,
        r#"{"request_id":"r","provider_attempt":{},"value":[{"type":"text_delta"}]}"#,
        r#"{"request_id":"r","provider_attempt":"bad-int","value":[{"type":"finish"}]}"#,
        r#"{"request_id":"r","value":[{"type":"finish"},{"type":"usage"}]}"#,
    ] {
        assert!(normalize_stream_result(request).is_none(), "{request}");
    }
}

#[test]
fn stream_result_accepts_the_exact_output_limit_and_declines_larger_results() {
    fn request(usage: &str) -> String {
        format!(r#"{{"request_id":"r","value":[{{"type":"finish","usage":"{usage}"}}]}}"#)
    }

    let envelope_size = normalize_stream_result(&request("")).unwrap().len();
    let usage = "a".repeat(MAX_OUTPUT_BYTES - envelope_size);
    let at_limit = normalize_stream_result(&request(&usage)).unwrap();
    assert_eq!(at_limit.len(), MAX_OUTPUT_BYTES);

    let over_limit = request(&format!("{usage}a"));
    assert!(normalize_stream_result(&over_limit).is_none());
    assert_eq!(
        tobkiri_ai_stream_normalizer_core::normalize(
            "normalize",
            &over_limit,
            ordered_python_compat::MAX_BYTES,
        )
        .unwrap()
        .len(),
        MAX_OUTPUT_BYTES + 1
    );
}

#[test]
fn emits_the_original_field_order_and_coercions() {
    let model = r#"{"model_id":"\u001c m \u3000","provider_id":" p ","provider_model_id":["雪",false],"available":0,"modalities":["text","text"," "],"capabilities":[null,true,1," \t"," Ω ","Ω"],"context_length":"１_０００","input_cost":"-0","output_cost":"1e-7","priority":9223372036854775809,"catalog_revision":{"a":2,"b":1}}"#;
    let result = route(
        model,
        WILDCARD,
        r#"{"wild":{"status":["degraded"],"observed_at":"40"}}"#,
        r#"{"minimum_context":true,"maximum_cost":1,"health_max_age":0}"#,
    )
    .unwrap();
    let descriptor = model.replace("\\u3000", "\u{3000}");
    let candidate = format!(
        r#"{{"model_id":"m","provider_model_id":"['雪', False]","provider_id":"p","execution_provider_instance_id":"wild","health_provider_instance_id":"wild","catalog_provider_instance_id":"","catalog_revision":"{{'a': 2, 'b': 1}}","capabilities":[" Ω ","1","None","True","Ω"],"modalities":["text"],"context_length":1000,"input_cost":-0.0,"output_cost":1e-07,"priority":9223372036854775809,"health":"['degraded']","health_observed_at":40.0,"descriptor":{descriptor}}}"#
    );
    assert_eq!(
        result,
        format!(
            r#"{{"selected":{candidate},"candidates":[{candidate}],"excluded":[],"deterministic":true}}"#
        )
    );
}

#[test]
fn exact_matches_precede_hints_and_wildcards_and_count_duplicate_instances() {
    let providers = [
        WILDCARD,
        r#"{"provider_instance_id":"wild2","routing_keys":["*"]}"#,
        r#"{"provider_instance_id":"hint"}"#,
        r#"{"provider_instance_id":"exact","routing_keys":["p","duplicate-key"]}"#,
        r#"{"provider_instance_id":"exact","routing_keys":["duplicate-key"]}"#,
    ]
    .join(",");
    let models = r#"
        {"model_id":"exact","provider_id":"p","execution_provider_instance_id":"hint","modalities":"text"},
        {"model_id":"hint","provider_id":"x","execution_provider_instance_id":" hint ","modalities":"text"},
        {"model_id":"ambiguous wild","provider_id":"x"},
        {"model_id":"ambiguous exact","provider_id":"duplicate-key","execution_provider_instance_id":"hint"},
        {"model_id":"literal asterisk","provider_id":"*","execution_provider_instance_id":"hint"},
        true
    "#;
    let result = loads(&route(models, &providers, "{}", "{}").unwrap()).unwrap();
    let candidates = result.get("candidates").unwrap().as_array().unwrap();
    assert_eq!(candidates.len(), 2);
    assert_eq!(
        field(&candidates[0], "execution_provider_instance_id"),
        "exact"
    );
    assert_eq!(
        field(&candidates[1], "execution_provider_instance_id"),
        "hint"
    );
    let excluded = result.get("excluded").unwrap().as_array().unwrap();
    assert_eq!(
        excluded
            .iter()
            .map(|item| (field(item, "model_id"), field(item, "reason")))
            .collect::<Vec<_>>(),
        [
            ("ambiguous wild", "execution_provider_ambiguous"),
            ("ambiguous exact", "execution_provider_ambiguous"),
            ("literal asterisk", "execution_provider_ambiguous"),
        ]
    );

    let result =
        loads(&route(r#"{"model_id":" m ","provider_id":" p "}"#, "", "{}", "{}").unwrap())
            .unwrap();
    let excluded = &result.get("excluded").unwrap().as_array().unwrap()[0];
    assert_eq!(field(excluded, "model_id"), " m ");
    assert_eq!(field(excluded, "provider_id"), " p ");
    assert_eq!(field(excluded, "reason"), "execution_provider_unresolved");
}

#[test]
fn records_first_exclusion_in_original_order() {
    let cases = [
        (
            r#","available":false"#,
            r#"{"thinking":true}"#,
            "model_unavailable",
        ),
        (
            "",
            r#"{"modalities":"image","tool_calling":1}"#,
            "modality_mismatch",
        ),
        (
            "",
            r#"{"tool_calling":"false","thinking":1}"#,
            "tool_calling_mismatch",
        ),
        (
            "",
            r#"{"thinking":[0],"capabilities":"stream"}"#,
            "thinking_mismatch",
        ),
        (
            "",
            r#"{"capabilities":"stream","minimum_context":1}"#,
            "capability_mismatch",
        ),
        (
            r#","context_length":"9223372036854775808""#,
            r#"{"minimum_context":9223372036854775809,"data_residency":"jp"}"#,
            "context_length_mismatch",
        ),
        (
            r#","request_surfaces":"full""#,
            r#"{"data_residency":"jp"}"#,
            "request_surface_mismatch",
        ),
        (
            "",
            r#"{"data_residency":"jp","maximum_cost":0}"#,
            "data_residency_mismatch",
        ),
        ("", r#"{"maximum_cost":0}"#, "cost_unknown"),
        (
            r#","input_cost":"0.2","output_cost":0.5"#,
            r#"{"maximum_cost":0.5}"#,
            "cost_policy_mismatch",
        ),
        (
            r#","health_provider_instance_id":"offline""#,
            "{}",
            "health_unavailable",
        ),
        (
            r#","health_provider_instance_id":"blocked""#,
            "{}",
            "health_denied",
        ),
        (
            r#","health_provider_instance_id":"malformed""#,
            "{}",
            "health_invalid",
        ),
    ];
    let health = r#"{"offline":{"status":"unavailable","observed_at":40},"blocked":{"status":"denied"},"malformed":{"status":"invalid"}}"#;
    for (fields, requirements, reason) in cases {
        let model = format!(r#"{{"model_id":"m","modalities":"text"{fields}}}"#);
        let result = loads(&route(&model, WILDCARD, health, requirements).unwrap()).unwrap();
        assert!(matches!(result.get("selected"), Some(PyValue::Null)));
        let excluded = &result.get("excluded").unwrap().as_array().unwrap()[0];
        assert_eq!(field(excluded, "reason"), reason);
    }
}

#[test]
fn sorts_preferences_health_exact_integers_costs_and_stable_ties() {
    let models = [
        r#""label":"bad status","model_id":"a","provider_id":"q","health_provider_instance_id":"weird""#,
        r#""label":"stale health","model_id":"a","provider_id":"q","health_provider_instance_id":"stale""#,
        r#""label":"degraded health","model_id":"a","provider_id":"q","health_provider_instance_id":"degraded""#,
        r#""label":"matching model","model_id":"wanted","provider_id":"q""#,
        r#""label":"matching provider","model_id":"a","provider_id":"preferred""#,
        r#""label":"matching execution","model_id":"z","provider_id":"p","execution_provider_instance_id":"hint""#,
        r#""label":"large priority","model_id":"a","provider_id":"q","health_provider_instance_id":"good","priority":"9223372036854775809""#,
        r#""label":"smaller priority","model_id":"z","provider_id":"q","health_provider_instance_id":"good","priority":9223372036854775808"#,
        r#""label":"cost unknown","model_id":"a","provider_id":"q","health_provider_instance_id":"good","priority":-9223372036854775808"#,
        r#""label":"later revision","model_id":"a","provider_id":"q","health_provider_instance_id":"good","priority":-9223372036854775808,"input_cost":-0.0,"output_cost":1,"catalog_revision":"B""#,
        r#""label":"first stable tie","model_id":"a","provider_id":"q","health_provider_instance_id":"good","priority":-9223372036854775808,"input_cost":-0.0,"output_cost":1,"catalog_revision":"A""#,
        r#""label":"second stable tie","model_id":"a","provider_id":"q","health_provider_instance_id":"good","priority":-9223372036854775808,"input_cost":0.0,"output_cost":1,"catalog_revision":"A""#,
        r#""label":"cheaper output","model_id":"z","provider_id":"q","health_provider_instance_id":"good","priority":-9223372036854775808,"input_cost":0.0,"output_cost":0"#,
        r#""label":"smallest priority","model_id":"z","provider_id":"q","health_provider_instance_id":"good","priority":-9223372036854775809"#,
    ]
    .map(|fields| format!(r#"{{{fields},"modalities":"text"}}"#))
    .join(",");
    let providers = format!(r#"{WILDCARD},{{"provider_instance_id":"hint"}}"#);
    let result = loads(
        &route(
            &models,
            &providers,
            r#"{"weird":{"status":{"unexpected":true}},"stale":{"status":"denied","observed_at":39},"degraded":{"status":"degraded","observed_at":40},"good":{"status":"available","observed_at":100}}"#,
            r#"{"preferred_provider_instance_id":"hint","preferred_provider_id":"preferred","preferred_model_id":"wanted"}"#,
        )
        .unwrap(),
    )
    .unwrap();
    let candidates = result.get("candidates").unwrap().as_array().unwrap();
    assert_eq!(result.get("selected"), candidates.first());
    assert_eq!(
        candidates
            .iter()
            .map(|candidate| field(candidate.get("descriptor").unwrap(), "label"))
            .collect::<Vec<_>>(),
        [
            "matching execution",
            "matching provider",
            "matching model",
            "smallest priority",
            "cheaper output",
            "first stable tie",
            "second stable tie",
            "later revision",
            "cost unknown",
            "smaller priority",
            "large priority",
            "degraded health",
            "stale health",
            "bad status",
        ]
    );
    assert_eq!(field(&candidates[12], "health"), "unknown");
    assert_eq!(field(&candidates[13], "health"), "{'unexpected': True}");
}

#[test]
fn declines_unsupported_codec_conversion_and_output_without_partial_result() {
    for invalid in [
        "{}",
        r#"{"decision_time":"tomorrow"}"#,
        r#"{"decision_time":false,"decision_time":true}"#,
        r#"{"decision_time":0,"ignored":"\ud800"}"#,
        r#"{"decision_time":1e999}"#,
        "[]",
    ] {
        assert_eq!(evaluate(invalid), None, "{invalid}");
    }
    let too_large_as_float = "9".repeat(400);
    let request = format!(r#"{{"decision_time":{too_large_as_float}}}"#);
    assert_eq!(evaluate(&request), None);
    let model = r#"{"model_id":"m","modalities":"text","input_cost":"inf","output_cost":1}"#;
    assert_eq!(route(model, WILDCARD, "{}", "{}"), None);
    assert!(
        route(model, WILDCARD, "{}", r#"{"maximum_cost":0}"#)
            .unwrap()
            .contains("cost_policy_mismatch")
    );

    let descriptor = "x".repeat(MAX_OUTPUT_BYTES / 2);
    let model = format!(r#"{{"model_id":"m","modalities":"text","blob":"{descriptor}"}}"#);
    assert_eq!(route(&model, WILDCARD, "{}", "{}"), None);
}

#[test]
fn preserves_empty_routing_and_finite_outcomes_from_python_defaults() {
    let expected = r#"{"selected":null,"candidates":[],"excluded":[],"deterministic":true}"#;
    assert_eq!(
        evaluate(r#"{"models":{},"execution_providers":"malformed","health":[],"requirements":true,"decision_time":" NaN "}"#).as_deref(),
        Some(expected)
    );
    let model = r#"{"model_id":"m","modalities":"text","context_length":-1.9,"priority":"bogus","input_cost":"?","output_cost":false}"#;
    let result = loads(
        &route(
            model,
            WILDCARD,
            r#"{"wild":[]}"#,
            r#"{"minimum_context":-1}"#,
        )
        .unwrap(),
    )
    .unwrap();
    let selected = result.get("selected").unwrap();
    assert_eq!(
        selected.get("context_length"),
        Some(&PyValue::Int("-1".into()))
    );
    assert_eq!(selected.get("priority"), Some(&PyValue::Int("100".into())));
    assert_eq!(selected.get("input_cost"), Some(&PyValue::Null));
    assert_eq!(selected.get("output_cost"), Some(&PyValue::Float(0.0)));
    assert_eq!(field(selected, "health"), "unknown");
}
