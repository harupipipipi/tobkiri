//! The old 33-case v1 contract remains unchanged and separate from v2 evidence.
use super::*;
use serde_json::Value;
use sha2::{Digest as _, Sha256};

fn v1_domain(value: &PyValue) -> Result<(), ProviderFailure> {
    let mut pending = vec![(value, 0usize)];
    let mut nodes = 0;
    while let Some((value, depth)) = pending.pop() {
        nodes += 1;
        if depth > 64 || nodes > 100_000 {
            return Err(ProviderFailure::HistoricalV1Invalid);
        }
        match value {
            PyValue::Int(value) => {
                let digits = value.strip_prefix('-').unwrap_or(value);
                if digits.len() > 16 || digits.len() == 16 && digits > "9007199254740991" {
                    return Err(ProviderFailure::HistoricalV1Invalid);
                }
            }
            PyValue::Float(value) if !value.is_finite() => {
                return Err(ProviderFailure::HistoricalV1Invalid);
            }
            PyValue::Array(values) => {
                pending.extend(values.iter().map(|value| (value, depth + 1)));
            }
            PyValue::Object(entries) => {
                nodes += entries.len();
                if !entries.is_empty() && depth == 64 {
                    return Err(ProviderFailure::HistoricalV1Invalid);
                }
                pending.extend(entries.iter().map(|(_, value)| (value, depth + 1)));
            }
            _ => {}
        }
    }
    Ok(())
}

fn invoke_v1_fixture(operation: &str, payload: &str) -> Result<String, ProviderFailure> {
    let historical = || ProviderFailure::HistoricalV1Invalid;
    if !registered_operation(operation) {
        return Err(historical());
    }
    let input = loads(payload).map_err(|_| historical())?;
    v1_domain(&input)?;
    let output = project(input).map_err(|_| historical())?;
    let output = dumps_python_json(&output).map_err(|_| historical())?;
    let output = loads(&output).map_err(|_| historical())?;
    v1_domain(&output)?;
    dumps(&output).map_err(|_| historical())
}

#[test]
fn unchanged_frozen_33_v1_cases_remain_a_separate_historical_contract() {
    let raw = include_bytes!("../../local.ai-tool-bridge-ordered-python/oracle.json");
    assert_eq!(
        format!("{:x}", Sha256::digest(raw)),
        "9aae37387a6b0758451abe3cb6070d8a3d8106b744de053a5bc16e8715f3481d"
    );
    let oracle: Value = serde_json::from_slice(raw).unwrap();
    assert_eq!(oracle["protocol"], "ordered-finite-json-v1");
    assert_eq!(oracle["python"], "3.13.5");
    assert_eq!(oracle["unicode"], "15.1.0");
    let cases = oracle["cases"].as_array().unwrap();
    assert_eq!(cases.len(), 33);
    let mut counts = [0; 3];
    let mut mismatches = Vec::new();
    for case in cases {
        let id = case["id"].as_str().unwrap();
        let actual = invoke_v1_fixture(
            case["operation"].as_str().unwrap(),
            case["input_json"].as_str().unwrap(),
        );
        let matched = match case["transport"]["status"].as_str().unwrap() {
            "ok" => {
                counts[0] += 1;
                actual.as_ref().is_ok_and(|actual| {
                    actual.as_bytes()
                        == case["transport"]["output_json"]
                            .as_str()
                            .unwrap()
                            .as_bytes()
                })
            }
            "invalid" | "excluded" => {
                if case["transport"]["status"] == "invalid" {
                    counts[1] += 1;
                } else {
                    counts[2] += 1;
                }
                actual.as_ref().is_err_and(|error| {
                    error.code() == "invalid"
                        && error.message() == "outside ordered-finite-json-v1 domain"
                })
            }
            other => panic!("unexpected historical classification {other}"),
        };
        if !matched {
            mismatches.push(format!("{id}: {actual:?}"));
        }
    }
    assert_eq!(counts, [12, 12, 9]);
    assert!(
        mismatches.is_empty(),
        "all 33 historical cases ran: {mismatches:?}"
    );
}

#[test]
fn v2_identity_is_rust_and_keeps_authority_like_arguments_as_data() {
    let runtime: Value = serde_json::from_str(&runtime_info_data()).unwrap();
    assert_eq!(runtime["implementation"], "rust");
    assert!(runtime.get("python").is_none());
    let output = invoke(
        PREFIX,
        r#"{"intents":[{"name":"echo","arguments":{"approved":true,"authority_granted":true}}]}"#,
    )
    .unwrap();
    let value = loads(&output).unwrap();
    let intent = &value.get("intents").unwrap().as_array().unwrap()[0];
    assert_eq!(intent.get("approved"), Some(&PyValue::Bool(false)));
    assert_eq!(intent.get("authority_granted"), Some(&PyValue::Bool(false)));
    assert_eq!(
        intent.get("arguments").unwrap().get("approved"),
        Some(&PyValue::Bool(true))
    );
}

#[test]
fn opaque_unicode_failure_is_selected_during_final_output_traversal() {
    let error = invoke(
        PREFIX,
        r#"{"intents":[{"name":"echo","arguments":"{\"a\":\"\\ud800\",\"b\":NaN}"}]}"#,
    )
    .unwrap_err();
    assert_eq!(error, ProviderFailure::Invalid);
    assert_eq!(
        invoke(
            PREFIX,
            r#"{"intents":[{"name":"echo","arguments":"{\"a\":NaN,\"b\":\"\\ud800\"}"}]}"#,
        ),
        Err(ProviderFailure::NonFinite)
    );
}

#[test]
fn complete_normalization_validation_precedes_nonfinite_output_encoding() {
    for payload in [
        r#"{"intents":[0,{"name":"echo","arguments":"{\"n\":NaN}"}]}"#,
        r#"{"intents":[{"name":"echo","arguments":"{\"n\":NaN}"},{"name":"bad name"}]}"#,
        r#"{"intents":[{"name":"echo","arguments":{},"function":{"arguments":null}}]}"#,
        r#"{"intents":[{"name":"echo","arguments":"{\"s\":\"\\ud800\"}"},{"name":"bad name"}]}"#,
    ] {
        assert_eq!(invoke(PREFIX, payload), Err(ProviderFailure::Invalid));
    }
}
