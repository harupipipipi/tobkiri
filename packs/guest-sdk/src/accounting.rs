//! Public usage formatting. Missing receipts must never appear as zero cost.
use serde_json::Value;

pub fn format_usage(value: &Value, prompt: Option<u32>, completion: Option<u32>) -> String {
    let tokens = value.get("tokens").unwrap_or(value);
    let number = |name: &str, fallback: Option<u32>| {
        tokens[name]
            .as_u64()
            .or(fallback.map(u64::from))
            .map(|v| v.to_string())
            .unwrap_or_else(|| "unknown".into())
    };
    let cost = if value.get("cost_known") == Some(&Value::Bool(false)) {
        "unknown"
    } else {
        value["cost_usd"].as_str().unwrap_or("unknown")
    };
    let coverage = if value.get("tokens_complete") == Some(&Value::Bool(false)) {
        " confirmed-receipts-only"
    } else {
        ""
    };
    format!(
        "input={} output={} total={} cached={} reasoning={} USD={}{}",
        number("input_tokens", prompt),
        number("output_tokens", completion),
        number("total_tokens", None),
        number("cached_input_tokens", None),
        number("reasoning_tokens", None),
        cost,
        coverage
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn aggregate_and_single_provider_receipts_format_without_double_counting() {
        assert_eq!(
            format_usage(
                &json!({"total_tokens":30,"cost_usd":"0.0001"}),
                Some(21),
                Some(9)
            ),
            "input=21 output=9 total=30 cached=unknown reasoning=unknown USD=0.0001"
        );
        assert!(
            format_usage(
                &json!({"tokens":{"input_tokens":80,"output_tokens":20},"cost_known":false}),
                None,
                None
            )
            .ends_with("USD=unknown")
        );
        assert!(
            format_usage(&json!({"tokens":{"input_tokens":80}}), Some(5), None)
                .starts_with("input=80 ")
        );
    }
    #[test]
    fn missing_usage_and_explicit_zero_are_distinct() {
        assert!(format_usage(&json!({}), None, None).contains("input=unknown"));
        assert!(format_usage(&json!({"cost_usd":"0"}), Some(0), Some(0)).ends_with("USD=0"));
    }
}
