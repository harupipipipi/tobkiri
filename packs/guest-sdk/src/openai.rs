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

/// Opt into valid JSON output for a Profile whose entire protocol is JSON.
/// Text-oriented Profiles retain the unmodified provider request.
pub fn request_body_with_json_mode(
    model: &str,
    messages: Vec<Value>,
    max_tokens: u32,
    provider_order: Option<&[String]>,
    json_mode: bool,
) -> Value {
    let mut value = request_body(model, messages, max_tokens, provider_order);
    if json_mode {
        value["response_format"] = json!({"type": "json_object"});
    }
    value
}

/// Carry an explicit phase schema without trusting provider-side enforcement.
/// Callers must still validate every public completion against this contract.
pub fn request_body_with_schema(
    model: &str,
    messages: Vec<Value>,
    max_tokens: u32,
    provider_order: Option<&[String]>,
    name: &str,
    schema: Value,
    strict: bool,
) -> Result<Value, &'static str> {
    if name.is_empty()
        || name.len() > 64
        || !name
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"_-".contains(&byte))
        || !schema.is_object()
        || schema.get("type").and_then(Value::as_str) != Some("object")
    {
        return Err("structured generation requires a portable name and object schema");
    }
    let mut value = request_body(model, messages, max_tokens, provider_order);
    value["response_format"] = json!({"type":"json_schema", "json_schema":{
        "name":name,"schema":schema,"strict":strict
    }});
    Ok(value)
}

/// Validate completion evidence and normalize only public stream information.
#[derive(Default)]
pub struct ChatStream {
    text: String,
    finished: bool,
    done: bool,
    metadata: Value,
}

impl ChatStream {
    /// Whether the response reached both a successful finish and SSE DONE.
    pub fn is_done(&self) -> bool {
        self.done
    }

    /// Capture the public receipt before reading the response body. This is a
    /// provider generation id, never the Core's local stream handle.
    pub fn generation_header(&mut self, id: &str) -> Result<String, &'static str> {
        self.merge_identity("generation_id", id)?;
        Ok(self.metadata_event())
    }

    fn merge_identity(&mut self, key: &str, value: &str) -> Result<bool, &'static str> {
        if value.is_empty() || value.len() > 256 || value.chars().any(char::is_control) {
            return Err("provider public identity is invalid");
        }
        if key == "generation_id"
            && (value.len() > 128
                || !value
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b)))
        {
            return Err("provider generation identity is invalid");
        }
        if self.metadata.is_null() {
            self.metadata = json!({});
        }
        if let Some(old) = self.metadata.get(key) {
            if old.as_str() != Some(value) {
                return Err("provider changed its generation identity");
            }
            return Ok(false);
        }
        self.metadata[key] = json!(value);
        Ok(true)
    }

    fn metadata_event(&self) -> String {
        let mut metadata = self.metadata.clone();
        metadata["version"] = json!("tobkiri.chat.metadata.v1");
        json!({"kind":"metadata","text":metadata.to_string(),
            "prompt-tokens":null,"completion-tokens":null})
        .to_string()
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
        let mut changed = false;
        for (source, target) in [
            ("id", "generation_id"),
            ("model", "model"),
            ("provider", "provider"),
        ] {
            if let Some(value) = value.get(source).filter(|v| !v.is_null()) {
                changed |= self.merge_identity(
                    target,
                    value
                        .as_str()
                        .ok_or("provider public identity is invalid")?,
                )?;
            }
        }
        if changed {
            events.push(self.metadata_event());
        }
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
                    if self.merge_identity("finish_reason", "stop")? {
                        events.push(self.metadata_event());
                    }
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
    fn explicit_schema_is_transported_exactly_without_claiming_provider_enforcement() {
        let schema = json!({"type":"object","properties":{"tool_calls":{"type":"array"}},"required":["tool_calls"],"additionalProperties":false});
        let request =
            request_body_with_schema("m", vec![], 0, None, "phase_1", schema.clone(), true)
                .unwrap();
        assert_eq!(request["response_format"]["type"], "json_schema");
        assert_eq!(request["response_format"]["json_schema"]["schema"], schema);
        assert_eq!(request["response_format"]["json_schema"]["strict"], true);
        assert!(request.get("max_tokens").is_none());
        assert!(
            request_body_with_schema(
                "m",
                vec![],
                0,
                None,
                "bad name",
                json!({"type":"object"}),
                true
            )
            .is_err()
        );
        assert!(
            request_body_with_schema("m", vec![], 0, None, "valid", json!({"type":"array"}), true)
                .is_err()
        );
        // HTTP 200 / finish=stop can still carry a malformed/schema-invalid
        // public answer. The domain validator, not this request, gates success.
        let mut stream = ChatStream::default();
        assert!(
            stream
                .accept(r#"{"choices":[{"delta":{"content":"not JSON"},"finish_reason":"stop"}]}"#)
                .is_ok()
        );
        assert!(stream.accept("[DONE]").is_ok());
        assert!(stream.is_done());
    }

    #[test]
    fn json_mode_is_explicit_and_keeps_provider_default_output_budget() {
        let messages = vec![json!({"role":"user","content":"Return JSON."})];
        let text = request_body("model", messages.clone(), 0, None);
        assert_eq!(
            request_body_with_json_mode("model", messages.clone(), 0, None, false),
            text
        );
        let structured = request_body_with_json_mode("model", messages, 0, None, true);
        assert_eq!(structured["response_format"], json!({"type":"json_object"}));
        assert!(structured.get("max_tokens").is_none());
        assert!(text.get("response_format").is_none());
    }

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
        let event: Value = final_events
            .iter()
            .map(|event| serde_json::from_str::<Value>(event).unwrap())
            .find(|event| event["kind"] == "usage")
            .unwrap();
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

    #[test]
    fn generation_header_and_sse_identity_are_early_stable_and_public_only() {
        let mut stream = ChatStream::default();
        let header: Value =
            serde_json::from_str(&stream.generation_header("gen-abc-123").unwrap()).unwrap();
        let receipt: Value = serde_json::from_str(header["text"].as_str().unwrap()).unwrap();
        assert_eq!(receipt["generation_id"], "gen-abc-123");
        assert!(!stream.is_done());
        let events = stream.accept(&json!({"id":"gen-abc-123","model":"mimo","provider":"xiaomi",
            "choices":[{"delta":{"content":"391","reasoning":"private-marker"},"finish_reason":null}]}).to_string()).unwrap();
        assert!(events.iter().any(|event| event.contains("metadata")));
        assert!(!events.join("").contains("private-marker"));
        assert!(stream.accept(r#"{"id":"gen-other","choices":[]}"#).is_err());
        assert!(
            ChatStream::default()
                .generation_header("secret\nheader")
                .is_err()
        );
    }

    #[test]
    fn final_usage_repeating_stop_is_accounting_not_duplicate_completion() {
        let mut stream = ChatStream::default();
        stream.generation_header("gen-test").unwrap();
        stream.accept(r#"{"id":"gen-test","choices":[{"delta":{"content":"391"},"finish_reason":"stop"}]}"#).unwrap();
        let usage = stream.accept(r#"{"id":"gen-test","choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":2,"completion_tokens":1}}"#).unwrap();
        assert_eq!(usage.len(), 1);
        assert_eq!(
            serde_json::from_str::<Value>(&usage[0]).unwrap()["kind"],
            "usage"
        );
        assert_eq!(stream.accept("[DONE]").unwrap().len(), 1);
        assert!(stream.is_done());
    }
}
