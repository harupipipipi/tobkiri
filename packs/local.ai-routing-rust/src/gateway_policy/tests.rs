use super::project_gateway_policy;

#[test]
fn merges_model_requirements_and_appends_the_stricter_output_limit() {
    let request = r#"{
        "phase":"model-profile","identifier":"saved-alias",
        "resolved_profile_id":["profile"],"model_id":"model",
        "profile_requirements":{"before":true,"modalities":[" image ","text"],
            "same":1,"tool_calling":[0],"minimum_context":"٤_٠٩٦",
            "maximum_cost":"1.4","preferred_model_id":null,"capabilities":"ignored"},
        "request_requirements":{"later":3,"same":2,"modalities":[null,"text"],
            "capabilities":[" tool_calling ","\u001c","𐐀","é"],"thinking":"yes",
            "minimum_context":2000.75,"maximum_cost":true},
        "profile_parameters":{"max_tokens":400,"n":1,"saved":true,"best_of":2,
            "max_output_tokens":1,"temperature":0.5},
        "request_parameters":[["request_only","yes"],["max_completion_tokens",300],
            ["best_of",1],["max_output_tokens",250],["temperature",0],["request_only",false]]
    }"#;
    assert_eq!(
        project_gateway_policy(request).as_deref(),
        Some(concat!(
            r#"{"model_profile_id":"['profile']","requirements":{"before":true,"modalities":["None","image","text"],"same":2,"tool_calling":true,"minimum_context":4096,"maximum_cost":1.0,"preferred_model_id":null,"capabilities":["tool_calling","é","𐐀"],"later":3,"thinking":true},"#,
            r#""parameters":{"n":1,"saved":true,"best_of":1,"temperature":0,"request_only":false,"max_tokens":250}}"#,
        ))
    );
}

#[test]
fn keeps_the_original_positions_for_updated_fields_and_drops_saved_aliases() {
    let request = r#"{
        "phase":"model-profile","identifier":"alias","resolved_profile_id":[],
        "model_id":{"selected":false},
        "profile_requirements":{"capabilities":"saved","minimum_context":-3},
        "request_requirements":{"capabilities":["\u001f"],"minimum_context":-1.9,"maximum_cost":[0]},
        "profile_parameters":{"max_completion_tokens":25,"max_output_tokens":20,"x":false},
        "request_parameters":["βé",{"z":1,"v":9}]
    }"#;
    assert_eq!(
        project_gateway_policy(request).as_deref(),
        Some(concat!(
            r#"{"model_profile_id":"alias","requirements":{"capabilities":["\u001f"],"minimum_context":-1,"maximum_cost":[0],"tool_calling":false,"thinking":false,"preferred_model_id":"{'selected': False}"},"#,
            r#""parameters":{"x":false,"β":"é","z":"v"}}"#,
        ))
    );
}

#[test]
fn normalizes_route_requirements_with_python_list_sorting_and_scalar_coercions() {
    let request = r#"{"phase":"route-requirement","requirements":{
        "modalities":["\u001f","\u0085","　image ",false,null,"alpha","α","image"],
        "capabilities":[["\u0000","it's"],{"a":true},3,1.0," None ",null],
        "tool_calling":{},"thinking":[false],"minimum_context":" -١٢.٣ ",
        "request_surface":0,"data_residency":4,"maximum_cost":" -١.٢ ",
        "preferred_model_id":{},"preferred_provider_id":[true],
        "preferred_provider_instance_id":" ","health_max_age":"NaN"
    }}"#;
    assert!(project_gateway_policy(request).is_none());
    let request = request.replace("-١٢.٣", "-١٢");
    assert_eq!(
        project_gateway_policy(&request).as_deref(),
        Some(concat!(
            r#"{"modalities":["False","None","alpha","image","α"],"capabilities":["1.0","3","None","['\\x00', \"it's\"]","{'a': True}"],"#,
            r#""tool_calling":false,"thinking":true,"minimum_context":0,"request_surface":"chat","data_residency":"4","maximum_cost":-1.2,"preferred_model_id":null,"preferred_provider_id":"[True]","preferred_provider_instance_id":" ","health_max_age":0.0}"#,
        ))
    );
}

#[test]
fn uses_text_and_chat_defaults_for_non_mapping_requirements() {
    assert_eq!(
        project_gateway_policy(r#"{"phase":"route-requirement","requirements":[1]}"#).as_deref(),
        Some(concat!(
            r#"{"modalities":["text"],"capabilities":[],"tool_calling":false,"thinking":false,"minimum_context":0,"request_surface":"chat","data_residency":null,"#,
            r#""maximum_cost":null,"preferred_model_id":null,"preferred_provider_id":null,"preferred_provider_instance_id":null,"health_max_age":60.0}"#,
        ))
    );
}

#[test]
fn applies_python_float_ordering_and_accepts_large_integers_without_rounding() {
    let request = r#"{
        "phase":"model-profile","identifier":"alias",
        "profile_requirements":{"minimum_context":9223372036854775809,"maximum_cost":-0.0},
        "request_requirements":{"minimum_context":9223372036854775808,"maximum_cost":"NaN"},
        "request_parameters":{"max_output_tokens":9223372036854775809,
            "max_completion_tokens":9223372036854775808}
    }"#;
    assert_eq!(
        project_gateway_policy(request).as_deref(),
        Some(concat!(
            r#"{"model_profile_id":"alias","requirements":{"minimum_context":9223372036854775809,"maximum_cost":-0.0,"tool_calling":false,"thinking":false,"preferred_model_id":""},"#,
            r#""parameters":{"max_tokens":9223372036854775808}}"#,
        ))
    );
}

#[test]
fn declines_when_python_would_raise_or_output_unrepresentable_data() {
    for (field, fragment) in [
        ("profile_parameters", r#"{"max_tokens":false}"#),
        ("profile_parameters", r#"{"max_tokens":131073}"#),
        ("profile_parameters", r#"{"max_tokens":0,"n":1}"#),
        ("request_parameters", r#"{"n":true}"#),
        ("request_parameters", r#"{"best_of":2}"#),
        ("request_parameters", r#"{"max_tokens":1.0}"#),
        ("request_parameters", r#"{"max_completion_tokens":0}"#),
        ("request_parameters", r#"{"max_output_tokens":-1}"#),
        ("request_parameters", r#"[[1,"non-string key"]]"#),
        ("request_parameters", r#"[["invalid"]]"#),
        ("profile_requirements", r#"{"minimum_context":[0]}"#),
        ("request_requirements", r#"{"maximum_cost":"Infinity"}"#),
        ("profile_requirements", r#"{"note":"\ud800"}"#),
    ] {
        let request =
            format!(r#"{{"phase":"model-profile","identifier":"id","{field}":{fragment}}}"#);
        assert!(project_gateway_policy(&request).is_none(), "{request}");
    }

    for requirement in [
        r#"{"minimum_context":"１２.０"}"#,
        r#"{"health_max_age":"Infinity"}"#,
        r#"{"maximum_cost":"NaN"}"#,
    ] {
        let request = format!(r#"{{"phase":"route-requirement","requirements":{requirement}}}"#);
        assert!(project_gateway_policy(&request).is_none(), "{request}");
    }

    let huge_integer = "9".repeat(309);
    let request = format!(
        r#"{{"phase":"route-requirement","requirements":{{"maximum_cost":{huge_integer}}}}}"#
    );
    assert!(project_gateway_policy(&request).is_none());
}

#[test]
fn bounds_input_depth_size_and_expanded_output() {
    let prefix = r#"{"phase":"route-requirement","requirements":{"capabilities":["#;
    let suffix = "]}}";
    let nested = "[".repeat(129) + "0" + &"]".repeat(129);
    let oversized = format!(r#""{}""#, "a".repeat(super::MAX_OUTPUT_BYTES));
    let expanded_list = format!(r#"["{}"]"#, "\u{0085}".repeat(130_000));
    let companion_list = format!(r#"["{}"]"#, "\u{009f}".repeat(100_000));
    let expanded = format!("{expanded_list},{companion_list}");
    let request = format!("{prefix}{expanded}{suffix}");
    assert!(request.len() < super::MAX_OUTPUT_BYTES);
    assert!(project_gateway_policy(&request).is_none());

    for fragment in [nested, oversized] {
        let request = format!("{prefix}{fragment}{suffix}");
        assert!(project_gateway_policy(&request).is_none());
    }
}
