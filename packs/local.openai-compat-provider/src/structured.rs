//! OpenAI Chat Completions wire format for the opt-in structured chat port.

use std::{collections::BTreeSet, num::NonZeroU32};

use serde::Deserialize;
use serde_json::{Value, json};

use super::{
    Error, ErrorCode, Settings, StructuredPart, StructuredPrompt, api_error_summary, error,
};

const MAX_STREAM_FRAMES: usize = 65_536;
const MAX_STREAM_EVENTS: usize = 65_536;
const MAX_STREAM_BYTES: usize = 16 * 1024 * 1024;
const MAX_GENERATED_BYTES: usize = 4 * 1024 * 1024;
const CORE_MAX_SAFE_INTEGER: u64 = 9_007_199_254_740_991;

fn invalid(message: &str) -> Error {
    error(ErrorCode::Invalid, message)
}

fn valid_function_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 64
        && name
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
}

/// Translate structured prompt history and caller-declared tools to Chat Completions.
pub(super) fn request_body(
    config: &Settings,
    prompt: StructuredPrompt,
    saved_max_tokens: Option<NonZeroU32>,
) -> Result<Vec<u8>, Error> {
    let requested = prompt.max_tokens.max(1);
    let max_tokens = saved_max_tokens.map_or(requested, |cap| requested.min(cap.get()));
    let mut messages = Vec::with_capacity(prompt.messages.len());
    for message in prompt.messages {
        let role = message.role;
        if role == "tool" {
            if message.parts.is_empty() {
                return Err(invalid("structured tool message requires a result"));
            }
            for part in message.parts {
                let StructuredPart::ToolResult(result) = part else {
                    return Err(invalid("structured tool message has an unsupported part"));
                };
                if result.call_id.trim().is_empty() {
                    return Err(invalid("structured tool result requires a call ID"));
                }
                // Chat Completions carries both successful and failed results as content.
                messages.push(json!({
                    "role": "tool", "tool_call_id": result.call_id, "content": result.content,
                }));
            }
            continue;
        }
        if !matches!(role.as_str(), "system" | "developer" | "user" | "assistant") {
            return Err(invalid("structured message has an unsupported role"));
        }
        let mut text = String::new();
        let mut reasoning = String::new();
        let mut calls = Vec::new();
        for part in message.parts {
            match part {
                StructuredPart::Text(content) => text.push_str(&content),
                StructuredPart::Reasoning(content) if role == "assistant" => {
                    reasoning.push_str(&content);
                }
                StructuredPart::ToolCall(call) if role == "assistant" => {
                    if call.id.trim().is_empty() || !valid_function_name(&call.name) {
                        return Err(invalid("structured tool call has an invalid ID or name"));
                    }
                    serde_json::from_str::<Value>(&call.arguments)
                        .map_err(|_| invalid("structured tool arguments are invalid JSON"))?;
                    calls.push(json!({
                        "id": call.id, "type": "function",
                        "function": {"name": call.name, "arguments": call.arguments},
                    }));
                }
                _ => {
                    return Err(invalid(
                        "structured message has a part unsupported by its role",
                    ));
                }
            }
        }
        let mut wire = json!({"role": role, "content": text});
        if !reasoning.is_empty() {
            wire["reasoning_content"] = json!(reasoning);
        }
        if !calls.is_empty() {
            wire["tool_calls"] = json!(calls);
        }
        messages.push(wire);
    }

    let mut names = BTreeSet::new();
    let mut tools = Vec::with_capacity(prompt.tools.len());
    for tool in prompt.tools {
        if !valid_function_name(&tool.name) || !names.insert(tool.name.clone()) {
            return Err(invalid("structured tool has an invalid or duplicate name"));
        }
        let parameters = serde_json::from_str::<Value>(&tool.parameters)
            .ok()
            .filter(Value::is_object)
            .ok_or_else(|| invalid("structured tool parameters must be a JSON Schema object"))?;
        tools.push(json!({
            "type": "function", "function": {
                "name": tool.name, "description": tool.description, "parameters": parameters,
            },
        }));
    }

    let mut body = json!({
        "model": config.model, "messages": messages, "stream": true,
        "stream_options": {"include_usage": true}, "max_tokens": max_tokens,
        "temperature": config.temperature,
    });
    if !tools.is_empty() {
        body["tools"] = json!(tools);
    }
    Ok(body.to_string().into_bytes())
}

#[derive(Deserialize)]
struct Chunk {
    #[serde(default)]
    choices: Vec<Choice>,
    usage: Option<Usage>,
    error: Option<Value>,
}

#[derive(Deserialize)]
struct Choice {
    #[serde(default)]
    index: u32,
    delta: Option<Delta>,
    finish_reason: Option<String>,
}

#[derive(Deserialize, Default)]
struct Delta {
    content: Option<String>,
    reasoning_content: Option<String>,
    tool_calls: Option<Vec<ToolDelta>>,
}

#[derive(Deserialize)]
struct ToolDelta {
    index: u32,
    id: Option<String>,
    #[serde(rename = "type")]
    kind: Option<String>,
    function: Option<FunctionDelta>,
}

#[derive(Deserialize, Default)]
struct FunctionDelta {
    name: Option<String>,
    arguments: Option<String>,
}

#[derive(Deserialize)]
struct Usage {
    prompt_tokens: Option<u64>,
    completion_tokens: Option<u64>,
    prompt_tokens_details: Option<PromptTokenDetails>,
}

#[derive(Deserialize)]
struct PromptTokenDetails {
    cached_tokens: Option<u64>,
}

fn token_count(values: Option<u64>) -> Value {
    match values {
        Some(value) if value > CORE_MAX_SAFE_INTEGER => json!(value.to_string()),
        Some(value) => json!(value),
        None => Value::Null,
    }
}

fn finish_reason(reason: &str) -> Value {
    match reason {
        "stop" | "length" => json!({"tag": reason}),
        "tool_calls" => json!({"tag": "tool-calls"}),
        "content_filter" => json!({"tag": "content-filter"}),
        _ => json!({"tag": "other", "val": reason}),
    }
}

/// Decode one OpenAI SSE data frame at a time into Core's WIT variant JSON.
#[derive(Default)]
pub(super) struct StructuredEvents {
    frames: usize,
    wire_bytes: usize,
    event_count: usize,
    generated_bytes: usize,
    finish: Option<Value>,
    done: bool,
}

impl StructuredEvents {
    /// Whether the stream received a valid terminal marker after a finish reason.
    pub(super) fn done(&self) -> bool {
        self.done
    }

    /// Decode one data frame. The finish event is released once `[DONE]` arrives.
    pub(super) fn frame(&mut self, frame: &str) -> Result<Vec<String>, Error> {
        if self.done {
            return Err(invalid("provider sent structured SSE data after [DONE]"));
        }
        self.frames += 1;
        self.wire_bytes = self.wire_bytes.saturating_add(frame.len());
        if self.frames > MAX_STREAM_FRAMES || self.wire_bytes > MAX_STREAM_BYTES {
            return Err(error(
                ErrorCode::LimitExceeded,
                "structured SSE response exceeds its size limit",
            ));
        }

        let mut events = Vec::new();
        if frame == "[DONE]" {
            let finish = self
                .finish
                .take()
                .ok_or_else(|| invalid("provider ended structured SSE without a finish reason"))?;
            self.emit(&mut events, "finish", finish, 0)?;
            self.done = true;
            return Ok(events);
        }

        if !frame.trim_start().starts_with('{') {
            return Err(invalid("provider sent malformed structured SSE data"));
        }
        let chunk: Chunk = serde_json::from_str(frame)
            .map_err(|_| invalid("provider sent malformed structured SSE data"))?;
        if let Some(details) = chunk.error {
            return Err(error(
                ErrorCode::Unavailable,
                &format!(
                    "provider stream failed: {}",
                    api_error_summary(&details, false)
                ),
            ));
        }
        if chunk.choices.len() > 1
            || chunk
                .choices
                .first()
                .is_some_and(|choice| choice.index != 0)
        {
            return Err(invalid(
                "provider sent an unsupported structured choice index",
            ));
        }
        if let Some(choice) = chunk.choices.into_iter().next() {
            self.choice(choice, &mut events)?;
        } else if chunk.usage.is_none() {
            return Err(invalid(
                "provider structured SSE chunk has no choice or usage",
            ));
        }
        if let Some(usage) = chunk.usage {
            self.emit(&mut events, "usage", json!({
                "prompt-tokens": token_count(usage.prompt_tokens),
                "completion-tokens": token_count(usage.completion_tokens),
                "cached-tokens": token_count(usage.prompt_tokens_details.and_then(|details| details.cached_tokens)),
            }), 0)?;
        }
        Ok(events)
    }

    fn choice(&mut self, choice: Choice, events: &mut Vec<String>) -> Result<(), Error> {
        if self.finish.is_some() {
            return Err(invalid(
                "provider sent a structured choice after its finish reason",
            ));
        }
        if choice.delta.is_none() && choice.finish_reason.is_none() {
            return Err(invalid(
                "provider structured choice has no delta or finish reason",
            ));
        }

        let delta = choice.delta.unwrap_or_default();
        for (tag, content) in [
            ("reasoning-delta", delta.reasoning_content),
            ("text-delta", delta.content),
        ] {
            if let Some(content) = content.filter(|text| !text.is_empty()) {
                let len = content.len();
                self.emit(events, tag, Value::String(content), len)?;
            }
        }
        for call in delta.tool_calls.into_iter().flatten() {
            if call.kind.as_deref().is_some_and(|kind| kind != "function") {
                return Err(invalid(
                    "provider sent an unsupported structured tool call type",
                ));
            }
            let function = call.function.unwrap_or_default();
            let bytes = [&call.id, &function.name, &function.arguments]
                .into_iter()
                .filter_map(Option::as_ref)
                .map(String::len)
                .sum();
            self.emit(
                events,
                "tool-delta",
                json!({
                    "index": call.index, "id": call.id,
                    "name": function.name, "arguments": function.arguments,
                }),
                bytes,
            )?;
        }
        if let Some(reason) = choice.finish_reason {
            if reason.trim().is_empty() {
                return Err(invalid("provider sent an empty structured finish reason"));
            }
            self.finish = Some(finish_reason(&reason));
        }
        Ok(())
    }

    fn emit(
        &mut self,
        events: &mut Vec<String>,
        tag: &str,
        value: Value,
        bytes: usize,
    ) -> Result<(), Error> {
        self.event_count += 1;
        self.generated_bytes = self.generated_bytes.saturating_add(bytes);
        if self.event_count > MAX_STREAM_EVENTS || self.generated_bytes > MAX_GENERATED_BYTES {
            return Err(error(
                ErrorCode::LimitExceeded,
                "structured SSE output exceeds its size limit",
            ));
        }
        events.push(json!({"tag": tag, "val": value}).to_string());
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::local::harness_chat2_0_0::messages::{
        FinishReason, Message, ResponseSummary, ToolCall, ToolDefinition, ToolResult,
    };

    fn settings() -> Settings {
        Settings {
            base_url: "https://example.test/v1".into(),
            model: "approved-model".into(),
            secret_name: None,
            timeout_ms: 100,
            temperature: 0.4,
        }
    }

    fn message(role: &str, parts: Vec<StructuredPart>) -> Message {
        Message {
            role: role.into(),
            parts,
            response: None,
        }
    }

    fn call(id: &str, arguments: &str) -> StructuredPart {
        StructuredPart::ToolCall(ToolCall {
            id: id.into(),
            name: "lookup".into(),
            arguments: arguments.into(),
        })
    }

    fn result(call_id: &str, content: &str, is_error: bool) -> StructuredPart {
        StructuredPart::ToolResult(ToolResult {
            call_id: call_id.into(),
            content: content.into(),
            is_error,
        })
    }

    fn definition(parameters: &str) -> ToolDefinition {
        ToolDefinition {
            name: "lookup".into(),
            description: "Look up a value".into(),
            parameters: parameters.into(),
        }
    }

    #[test]
    fn encodes_assistant_channels_and_ordered_call_and_result_messages() {
        let mut assistant = message(
            "assistant",
            vec![
                StructuredPart::Text("Before ".into()),
                StructuredPart::Reasoning("Think ".into()),
                call("a", r#"{ "value": 1 }"#),
                StructuredPart::Reasoning("first.".into()),
                StructuredPart::Text("calling.".into()),
                call("b", "{}"),
            ],
        );
        assistant.response = Some(ResponseSummary {
            finish: FinishReason::ToolCalls,
            usage: None,
        });
        let body = request_body(
            &settings(),
            StructuredPrompt {
                model: "route-name".into(),
                max_tokens: 100,
                messages: vec![
                    message("user", vec![StructuredPart::Text("Find it.".into())]),
                    assistant,
                    message(
                        "tool",
                        vec![result("b", "missing", true), result("a", "1", false)],
                    ),
                ],
                tools: vec![definition(
                    r#"{"type":"object","properties":{"value":{"type":"integer"}}}"#,
                )],
            },
            NonZeroU32::new(80),
        )
        .unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(&body).unwrap(),
            json!({
                "model": "approved-model", "max_tokens": 80, "temperature": 0.4,
                "stream": true, "stream_options": {"include_usage": true},
                "messages": [
                    {"role": "user", "content": "Find it."},
                    {"role": "assistant", "content": "Before calling.", "reasoning_content": "Think first.", "tool_calls": [
                        {"id": "a", "type": "function", "function": {"name": "lookup", "arguments": r#"{ "value": 1 }"#}},
                        {"id": "b", "type": "function", "function": {"name": "lookup", "arguments": "{}"}},
                    ]},
                    {"role": "tool", "tool_call_id": "b", "content": "missing"},
                    {"role": "tool", "tool_call_id": "a", "content": "1"},
                ],
                "tools": [{"type": "function", "function": {
                    "name": "lookup", "description": "Look up a value",
                    "parameters": {"type": "object", "properties": {"value": {"type": "integer"}}},
                }}],
            }),
        );
    }

    #[test]
    fn rejects_invalid_schema_arguments_and_message_parts() {
        for (messages, tools) in [
            (vec![], vec![definition("[]")]),
            (vec![], vec![definition("{")]),
            (vec![], vec![definition("{}"), definition("{}")]),
            (vec![message("assistant", vec![call("a", "{")])], vec![]),
            (vec![message("user", vec![call("a", "{}")])], vec![]),
            (
                vec![message("tool", vec![result("", "value", false)])],
                vec![],
            ),
        ] {
            let result = request_body(
                &settings(),
                StructuredPrompt {
                    model: String::new(),
                    max_tokens: 0,
                    messages,
                    tools,
                },
                None,
            );
            assert!(matches!(
                result,
                Err(Error {
                    code: ErrorCode::Invalid,
                    ..
                })
            ));
        }
    }

    fn frame(codec: &mut StructuredEvents, value: Value) -> Vec<Value> {
        codec
            .frame(&value.to_string())
            .unwrap()
            .iter()
            .map(|event| serde_json::from_str(event).unwrap())
            .collect()
    }

    #[test]
    fn forwards_indexed_fragments_and_usage_before_the_terminal_event() {
        let mut codec = StructuredEvents::default();
        assert_eq!(
            frame(
                &mut codec,
                json!({"choices": [{"index": 0, "delta": {
                    "reasoning_content": "think", "content": "answer", "tool_calls": [
                        {"index": 2, "function": {"arguments": "{"}},
                        {"index": 0, "id": "call_0", "type": "function", "function": {"name": "lookup"}},
                    ],
                }}]})
            ),
            vec![
                json!({"tag": "reasoning-delta", "val": "think"}),
                json!({"tag": "text-delta", "val": "answer"}),
                json!({"tag": "tool-delta", "val": {"index": 2, "id": null, "name": null, "arguments": "{"}}),
                json!({"tag": "tool-delta", "val": {"index": 0, "id": "call_0", "name": "lookup", "arguments": null}}),
            ]
        );
        assert_eq!(
            frame(
                &mut codec,
                json!({"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 2, "id": "call_2", "function": {"name": "lookup", "arguments": "}"}},
        ]}, "finish_reason": "tool_calls"}]})
            ),
            vec![
                json!({"tag": "tool-delta", "val": {"index": 2, "id": "call_2", "name": "lookup", "arguments": "}"}}),
            ]
        );
        assert!(!codec.done());
        assert_eq!(
            frame(
                &mut codec,
                json!({"choices": [], "usage": {
                    "prompt_tokens": u64::MAX, "prompt_tokens_details": {"cached_tokens": 3},
                }})
            ),
            vec![json!({"tag": "usage", "val": {
                "prompt-tokens": u64::MAX.to_string(), "completion-tokens": null, "cached-tokens": 3,
            }})]
        );
        assert!(!codec.done());
        let terminal = codec.frame("[DONE]").unwrap();
        assert_eq!(
            terminal,
            vec![json!({"tag": "finish", "val": {"tag": "tool-calls"}}).to_string()]
        );
        assert!(codec.done());
    }

    #[test]
    fn preserves_a_provider_specific_finish_reason_without_inventing_usage() {
        let mut codec = StructuredEvents::default();
        assert!(
            frame(
                &mut codec,
                json!({"choices": [{"delta": {}, "finish_reason": "max_context"}]})
            )
            .is_empty()
        );
        let terminal = codec.frame("[DONE]").unwrap();
        assert_eq!(
            terminal,
            vec![
                json!({"tag": "finish", "val": {"tag": "other", "val": "max_context"}}).to_string()
            ]
        );
    }

    #[test]
    fn rejects_malformed_or_premature_terminal_frames_and_redacts_upstream_errors() {
        let mut codec = StructuredEvents::default();
        for invalid in [
            "[DONE]",
            "{",
            "{}",
            r#"{"choices":[{"index":2,"delta":{}}]}"#,
            r#"{"choices":[{"delta":{"content":7}}]}"#,
            r#"{"choices":[{"delta":{"tool_calls":[{"function":{"name":"tool"}}]}}]}"#,
            r#"{"choices":[{"delta":{"tool_calls":[{"index":0,"type":"other"}]}}]}"#,
            r#"{"usage":{"prompt_tokens":-1}}"#,
        ] {
            assert_eq!(codec.frame(invalid).unwrap_err().code, ErrorCode::Invalid);
        }
        let failure = codec
            .frame(r#"{"error":{"message":"Bearer key sk-secret","code":"invalid_api_key"}}"#)
            .unwrap_err();
        assert_eq!(failure.code, ErrorCode::Unavailable);
        assert_eq!(failure.message, "provider stream failed: invalid_api_key");

        assert!(
            frame(
                &mut codec,
                json!({"choices": [{"delta": {}, "finish_reason": "stop"}]})
            )
            .is_empty()
        );
        assert_eq!(
            codec
                .frame(r#"{"choices":[{"delta":{"content":"extra"}}]}"#)
                .unwrap_err()
                .code,
            ErrorCode::Invalid
        );
    }

    #[test]
    fn bounds_frames_events_and_generated_bytes() {
        let mut frames = StructuredEvents {
            frames: MAX_STREAM_FRAMES,
            ..StructuredEvents::default()
        };
        let mut wire = StructuredEvents {
            wire_bytes: MAX_STREAM_BYTES,
            ..StructuredEvents::default()
        };
        let mut events = StructuredEvents {
            event_count: MAX_STREAM_EVENTS,
            ..StructuredEvents::default()
        };
        let mut generated = StructuredEvents {
            generated_bytes: MAX_GENERATED_BYTES,
            ..StructuredEvents::default()
        };
        for codec in [&mut frames, &mut wire, &mut events, &mut generated] {
            let failure = codec
                .frame(r#"{"choices":[{"delta":{"content":"a"}}]}"#)
                .unwrap_err();
            assert_eq!(failure.code, ErrorCode::LimitExceeded);
        }
    }
}
