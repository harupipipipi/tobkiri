//! Wire normalization for OpenAI-compatible streaming providers.
//! Reasoning content is never returned as a user event or persisted here.

use serde_json::{Value, json};

/// Build a request without imposing a completion cap when zero means default.
pub fn request_body(
    model: &str,
    messages: Vec<Value>,
    max_tokens: u32,
    provider_order: Option<&[String]>,
) -> Value {
    let mut value = json!({
        "model": model, "messages": messages, "stream": true,
        "stream_options": {"include_usage": true}, "temperature": 0,
    });
    if max_tokens > 0 {
        value["max_tokens"] = json!(max_tokens);
    }
    if let Some(order) = provider_order {
        value["provider"] = json!({"require_parameters": true, "order": order});
        value["reasoning"] = json!({"exclude": true});
    }
    value
}

/// Validate completion evidence and normalize only public stream information.
#[derive(Default)]
pub struct ChatStream {
    text: String,
    finished: bool,
    done: bool,
}

impl ChatStream {
    /// Whether the response reached both a successful finish and SSE DONE.
    pub fn is_done(&self) -> bool {
        self.done
    }

    /// Consume one decoded SSE data frame.
    pub fn accept(&mut self, frame: &str) -> Result<Vec<String>, &'static str> {
        if self.done {
            return Err("provider sent data after completion");
        }
        if frame == "[DONE]" {
            if !self.finished {
                return Err("provider ended without successful finish evidence");
            }
            self.done = true;
            return Ok(vec![crate::done_event(&self.text)]);
        }
        let value: Value =
            serde_json::from_str(frame).map_err(|_| "provider sent invalid SSE JSON")?;
        if !value.is_object() || value.get("error").is_some_and(|v| !v.is_null()) {
            return Err("provider stream failed");
        }
        let mut events = Vec::new();
        if let Some(choices) = value.get("choices") {
            let choices = choices.as_array().ok_or("provider choices are invalid")?;
            for choice in choices {
                if choice.get("index").and_then(Value::as_u64).unwrap_or(0) != 0 {
                    continue;
                }
                if let Some(calls) = choice.pointer("/delta/tool_calls") {
                    if !calls.is_null() && calls.as_array().is_none_or(|v| !v.is_empty()) {
                        return Err("provider returned unrequested native tool calls");
                    }
                }
                if let Some(content) = choice.pointer("/delta/content").filter(|v| !v.is_null()) {
                    let text = content.as_str().ok_or("provider content is invalid")?;
                    if !text.is_empty() {
                        if self.finished {
                            return Err("provider sent content after finish");
                        }
                        if self.text.len().saturating_add(text.len()) > 4 * 1024 * 1024 {
                            return Err("generated text exceeds four MiB");
                        }
                        self.text.push_str(text);
                        events.push(crate::delta_event(text));
                    }
                }
                if let Some(reason) = choice.get("finish_reason").filter(|v| !v.is_null()) {
                    if reason.as_str() != Some("stop") {
                        return Err("provider generation was interrupted or truncated");
                    }
                    self.finished = true;
                }
            }
        }
        if let Some(usage) = value.get("usage").filter(|v| !v.is_null()) {
            events.push(usage_event(usage)?);
        }
        Ok(events)
    }
}

fn counter(usage: &Value, name: &str) -> Result<Option<u32>, &'static str> {
    match usage.get(name).filter(|v| !v.is_null()) {
        None => Ok(None),
        Some(value) => value
            .as_u64()
            .and_then(|v| u32::try_from(v).ok())
            .map(Some)
            .ok_or("provider usage counter is invalid"),
    }
}

fn usage_event(usage: &Value) -> Result<String, &'static str> {
    if !usage.is_object() {
        return Err("provider usage is invalid");
    }
    let prompt = counter(usage, "prompt_tokens")?;
    let completion = counter(usage, "completion_tokens")?;
    let total = counter(usage, "total_tokens")?;
    let cached = counter(&usage["prompt_tokens_details"], "cached_tokens")?;
    let reasoning = counter(&usage["completion_tokens_details"], "reasoning_tokens")?;
    let cost = match usage.get("cost").filter(|v| !v.is_null()) {
        None => None,
        Some(value) => {
            let number = value.as_number().ok_or("provider cost is invalid")?;
            if !number.as_f64().is_some_and(|v| v.is_finite() && v >= 0.0) {
                return Err("provider cost is invalid");
            }
            Some(decimal_string(&number.to_string())?)
        }
    };
    // Extra accounting fits the existing typed chat-event without changing its
    // WIT shape. Decimal cost stays a string in the canonical value domain.
    let extra = json!({
        "total_tokens": total, "cached_input_tokens": cached,
        "reasoning_tokens": reasoning, "cost_usd": cost,
    });
    Ok(json!({"kind":"usage", "text":extra.to_string(),
        "prompt-tokens":prompt, "completion-tokens":completion})
    .to_string())
}

fn decimal_string(value: &str) -> Result<String, &'static str> {
    let Some((mantissa, exponent)) = value.split_once(['e', 'E']) else {
        return Ok(value.to_owned());
    };
    let exponent: i32 = exponent
        .parse()
        .map_err(|_| "provider cost exponent is invalid")?;
    let point = mantissa.find('.').unwrap_or(mantissa.len()) as i32 + exponent;
    let digits = mantissa.replace('.', "");
    if !digits.bytes().all(|byte| byte.is_ascii_digit()) || exponent.abs() > 324 {
        return Err("provider cost is invalid");
    }
    Ok(if point <= 0 {
        format!("0.{}{}", "0".repeat((-point) as usize), digits)
    } else if point as usize >= digits.len() {
        format!("{}{}", digits, "0".repeat(point as usize - digits.len()))
    } else {
        format!(
            "{}.{}",
            &digits[..point as usize],
            &digits[point as usize..]
        )
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn provider_default_has_no_completion_cap_or_unsupported_parallel_flag() {
        let request = request_body(
            "xiaomi/mimo-v2.6-flash",
            vec![],
            0,
            Some(&["xiaomi".to_owned()]),
        );
        assert!(request.get("max_tokens").is_none());
        assert!(request.get("max_completion_tokens").is_none());
        assert!(request.get("parallel_tool_calls").is_none());
        assert_eq!(request["provider"]["require_parameters"], true);
    }

    #[test]
    fn explicit_cap_is_preserved_without_a_hidden_512_token_clamp() {
        assert_eq!(request_body("m", vec![], 8192, None)["max_tokens"], 8192);
        assert!(request_body("m", vec![], 0, None).get("provider").is_none());
    }

    #[test]
    fn successful_completion_preserves_accounting_and_excludes_private_reasoning() {
        let mut stream = ChatStream::default();
        let first = stream
            .accept(
                &json!({"choices":[{"delta":{
            "content":"391", "reasoning":"private-test-secret"}}]})
                .to_string(),
            )
            .unwrap();
        let final_events = stream
            .accept(
                &json!({"choices":[{"delta":{},"finish_reason":"stop"}],
            "usage":{"prompt_tokens":21,"completion_tokens":9,"total_tokens":30,
                "prompt_tokens_details":{"cached_tokens":8},
                "completion_tokens_details":{"reasoning_tokens":4},"cost":0.0001}})
                .to_string(),
            )
            .unwrap();
        let done = stream.accept("[DONE]").unwrap();
        assert!(stream.is_done());
        assert!(done[0].contains("391"));
        assert!(!format!("{first:?}{final_events:?}{done:?}").contains("private-test-secret"));
        let event: Value = serde_json::from_str(&final_events[0]).unwrap();
        let extra: Value = serde_json::from_str(event["text"].as_str().unwrap()).unwrap();
        assert_eq!(extra["cached_input_tokens"], 8);
        assert_eq!(extra["reasoning_tokens"], 4);
        assert_eq!(extra["cost_usd"], "0.0001");
    }

    #[test]
    fn missing_finish_truncation_and_unrequested_tools_never_complete() {
        assert!(ChatStream::default().accept("[DONE]").is_err());
        for ending in ["length", "error", "tool_calls", "content_filter"] {
            let frame = json!({"choices":[{"finish_reason":ending}]}).to_string();
            assert!(ChatStream::default().accept(&frame).is_err());
        }
        assert!(
            ChatStream::default()
                .accept(r#"{"choices":[{"delta":{"tool_calls":[{}]}}]}"#)
                .is_err()
        );
    }

    #[test]
    fn malformed_or_unknown_usage_is_not_fabricated_as_zero() {
        for usage in [
            json!({"prompt_tokens":-1}),
            json!({"cost":"secret"}),
            json!({"completion_tokens":4294967296_u64}),
        ] {
            assert!(usage_event(&usage).is_err());
        }
        let event: Value = serde_json::from_str(&usage_event(&json!({})).unwrap()).unwrap();
        assert!(event["prompt-tokens"].is_null());
        assert!(event["completion-tokens"].is_null());
    }

    #[test]
    fn scientific_cost_receipts_are_normalized_without_rounding_or_precision_loss() {
        assert_eq!(decimal_string("3.458e-5").unwrap(), "0.00003458");
        assert_eq!(decimal_string("1e-9").unwrap(), "0.000000001");
        assert_eq!(decimal_string("1.25e2").unwrap(), "125");
        assert_eq!(decimal_string("1e3").unwrap(), "1000");
        assert_eq!(decimal_string("0").unwrap(), "0");
    }
}
