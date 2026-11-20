use std::collections::{BTreeMap, BTreeSet, btree_map::Entry};

use serde_json::value::RawValue;

use super::{
    ChatEvent, CompletedResponse, Error, ErrorCode, FinishReason, Message, MessagePart,
    ResponseSummary, ToolCall, Usage, invalid,
};

const MAX_EVENTS: usize = 65_536;
const MAX_RESPONSE_BYTES: usize = 4 * 1024 * 1024;
const MAX_TOOL_CALLS: usize = 256;

enum Part {
    Message(MessagePart),
    ToolCall(u32),
}

#[derive(Default)]
struct PendingCall {
    id: Option<String>,
    name: Option<String>,
    arguments: String,
}

/// Assemble one finished provider response. Tool calls remain untrusted data.
pub(super) fn assemble(mut events: Vec<ChatEvent>) -> Result<CompletedResponse, Error> {
    check_size(&events)?;
    let Some(ChatEvent::Finish(finish)) = events.pop() else {
        return Err(invalid("structured response requires a terminal finish"));
    };
    if matches!(&finish, FinishReason::Other(reason) if reason.trim().is_empty()) {
        return Err(invalid("structured response has an empty finish reason"));
    }

    let mut parts = Vec::new();
    let mut pending_calls = BTreeMap::new();
    let mut usage: Option<Usage> = None;
    for event in events {
        match event {
            ChatEvent::TextDelta(delta) if !delta.is_empty() => match parts.last_mut() {
                Some(Part::Message(MessagePart::Text(previous))) => previous.push_str(&delta),
                _ => parts.push(Part::Message(MessagePart::Text(delta))),
            },
            ChatEvent::ReasoningDelta(delta) if !delta.is_empty() => match parts.last_mut() {
                Some(Part::Message(MessagePart::Reasoning(previous))) => previous.push_str(&delta),
                _ => parts.push(Part::Message(MessagePart::Reasoning(delta))),
            },
            ChatEvent::TextDelta(_) | ChatEvent::ReasoningDelta(_) => {}
            ChatEvent::ToolDelta(delta) => {
                let call_count = pending_calls.len();
                let pending = match pending_calls.entry(delta.index) {
                    Entry::Occupied(entry) => entry.into_mut(),
                    Entry::Vacant(entry) => {
                        if call_count >= MAX_TOOL_CALLS {
                            return Err(limit("structured response has too many tool calls"));
                        }
                        // The first fragment anchors the call in message order.
                        parts.push(Part::ToolCall(delta.index));
                        entry.insert(PendingCall::default())
                    }
                };
                set_metadata(&mut pending.id, delta.id)?;
                set_metadata(&mut pending.name, delta.name)?;
                if let Some(arguments) = delta.arguments {
                    if pending.arguments.is_empty() {
                        pending.arguments = arguments;
                    } else {
                        pending.arguments.push_str(&arguments);
                    }
                }
            }
            ChatEvent::Usage(value) => usage = Some(value),
            ChatEvent::Finish(_) => {
                return Err(invalid("structured response has events after its finish"));
            }
        }
    }

    let has_calls = !pending_calls.is_empty();
    if matches!(&finish, FinishReason::ToolCalls) != has_calls {
        return Err(invalid(
            "structured tool calls do not match the finish reason",
        ));
    }

    let mut calls = Vec::with_capacity(pending_calls.len());
    let mut message_parts = Vec::with_capacity(parts.len());
    for part in parts {
        let part = match part {
            Part::Message(part) => part,
            Part::ToolCall(index) => {
                let pending = pending_calls
                    .remove(&index)
                    .expect("part has a pending call");
                let call = finish_call(pending)?;
                calls.push(call.clone());
                MessagePart::ToolCall(call)
            }
        };
        message_parts.push(part);
    }
    let mut ids = BTreeSet::new();
    if calls.iter().any(|call| !ids.insert(call.id.as_str())) {
        return Err(invalid("structured tool call IDs must be unique"));
    }

    Ok(CompletedResponse {
        message: Message {
            role: "assistant".into(),
            parts: message_parts,
            response: Some(ResponseSummary { finish, usage }),
        },
        calls,
    })
}

fn check_size(events: &[ChatEvent]) -> Result<(), Error> {
    if events.len() > MAX_EVENTS {
        return Err(limit("structured response has too many events"));
    }

    let mut bytes = 0usize;
    for event in events {
        let event_bytes = match event {
            ChatEvent::TextDelta(delta) | ChatEvent::ReasoningDelta(delta) => delta.len(),
            ChatEvent::ToolDelta(delta) => [&delta.id, &delta.name, &delta.arguments]
                .into_iter()
                .filter_map(Option::as_ref)
                .fold(0usize, |size, value| size.saturating_add(value.len())),
            ChatEvent::Finish(FinishReason::Other(reason)) => reason.len(),
            ChatEvent::Usage(_) | ChatEvent::Finish(_) => 0,
        };
        bytes = bytes.saturating_add(event_bytes);
        if bytes > MAX_RESPONSE_BYTES {
            return Err(limit("structured response exceeds its byte limit"));
        }
    }
    Ok(())
}

fn set_metadata(field: &mut Option<String>, value: Option<String>) -> Result<(), Error> {
    if let Some(value) = value {
        match field {
            Some(previous) if previous.as_str() != value.as_str() => {
                return Err(invalid("structured tool call metadata changed"));
            }
            Some(_) => {}
            None => *field = Some(value),
        }
    }
    Ok(())
}

fn finish_call(pending: PendingCall) -> Result<ToolCall, Error> {
    let id = pending
        .id
        .filter(|id| !id.trim().is_empty())
        .ok_or_else(|| invalid("structured tool call requires a nonempty ID"))?;
    let name = pending
        .name
        .filter(|name| {
            !name.is_empty()
                && name.len() <= 64
                && name
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
        })
        .ok_or_else(|| invalid("structured tool call has an invalid name"))?;

    // Validate syntax without expanding model data into a JSON value tree.
    let arguments = pending.arguments;
    if !arguments.trim_start().starts_with('{')
        || serde_json::from_str::<&RawValue>(&arguments).is_err()
    {
        return Err(invalid(
            "structured tool call arguments must be a JSON object",
        ));
    }
    Ok(ToolCall {
        id,
        name,
        arguments,
    })
}

fn limit(message: &str) -> Error {
    Error {
        code: ErrorCode::LimitExceeded,
        message: message.into(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::local::harness_chat::messages::ToolDelta;

    fn tool(
        index: u32,
        id: Option<&str>,
        name: Option<&str>,
        arguments: Option<&str>,
    ) -> ChatEvent {
        ChatEvent::ToolDelta(ToolDelta {
            index,
            id: id.map(str::to_owned),
            name: name.map(str::to_owned),
            arguments: arguments.map(str::to_owned),
        })
    }

    fn rejected(events: Vec<ChatEvent>) -> Error {
        let Err(error) = assemble(events) else {
            panic!("structured response should be rejected");
        };
        error
    }

    #[test]
    fn interleaved_fragments_keep_first_arrival_order_and_last_usage() {
        const OPAQUE_ID: &str = " provider://呼び出し/α? ";
        let completed = assemble(vec![
            ChatEvent::ReasoningDelta("Need ".into()),
            ChatEvent::ReasoningDelta("both.".into()),
            ChatEvent::TextDelta("First".into()),
            tool(u32::MAX, None, None, Some(" {\"zone\":")),
            ChatEvent::TextDelta("second".into()),
            tool(2, Some("other"), Some("lookup"), Some("{")),
            ChatEvent::ReasoningDelta("Wait.".into()),
            tool(
                u32::MAX,
                Some(OPAQUE_ID),
                Some("get-clock"),
                Some("\"UTC\"}"),
            ),
            ChatEvent::Usage(Usage {
                prompt_tokens: Some(5),
                completion_tokens: Some(8),
                cached_tokens: None,
            }),
            tool(2, Some("other"), None, Some("}")),
            ChatEvent::TextDelta("Done".into()),
            ChatEvent::TextDelta(".".into()),
            ChatEvent::Usage(Usage {
                prompt_tokens: Some(0),
                completion_tokens: None,
                cached_tokens: Some(u64::MAX),
            }),
            ChatEvent::Finish(FinishReason::ToolCalls),
        ])
        .expect("complete response");

        assert_eq!(completed.message.role, "assistant");
        assert!(matches!(completed.message.parts.as_slice(), [
            MessagePart::Reasoning(reason),
            MessagePart::Text(first),
            MessagePart::ToolCall(first_call),
            MessagePart::Text(second),
            MessagePart::ToolCall(second_call),
            MessagePart::Reasoning(rest),
            MessagePart::Text(last),
        ] if reason == "Need both." && first == "First" && first_call.id == OPAQUE_ID
            && second == "second" && second_call.id == "other" && rest == "Wait." && last == "Done."));
        assert!(matches!(completed.calls.as_slice(), [first, second]
            if first.id == OPAQUE_ID && first.name == "get-clock" && first.arguments == " {\"zone\":\"UTC\"}"
                && second.id == "other" && second.name == "lookup" && second.arguments == "{}"));
        assert!(matches!(
            completed.message.response,
            Some(ResponseSummary {
                finish: FinishReason::ToolCalls,
                usage: Some(Usage {
                    prompt_tokens: Some(0),
                    completion_tokens: None,
                    cached_tokens: Some(u64::MAX),
                }),
            })
        ));
    }

    #[test]
    fn rejects_truncated_responses_and_inconsistent_finishes() {
        for events in [
            vec![],
            vec![ChatEvent::TextDelta("partial".into())],
            vec![
                ChatEvent::Finish(FinishReason::Stop),
                ChatEvent::TextDelta("trailing".into()),
            ],
            vec![
                ChatEvent::Finish(FinishReason::Length),
                ChatEvent::Finish(FinishReason::Stop),
            ],
            vec![ChatEvent::Finish(FinishReason::ToolCalls)],
            vec![
                tool(0, Some("call"), Some("lookup"), Some("{}")),
                ChatEvent::Finish(FinishReason::Stop),
            ],
        ] {
            assert!(matches!(rejected(events).code, ErrorCode::Invalid));
        }
    }

    #[test]
    fn rejects_incomplete_invalid_or_conflicting_tool_data() {
        for mut events in [
            vec![tool(0, None, Some("lookup"), Some("{}"))],
            vec![tool(0, Some("  "), Some("lookup"), Some("{}"))],
            vec![tool(0, Some("call"), None, Some("{}"))],
            vec![tool(0, Some("call"), Some("private tool"), Some("{}"))],
            vec![tool(0, Some("call"), Some("lookup"), None)],
            vec![tool(
                0,
                Some("call"),
                Some("lookup"),
                Some("{\"private-tool-data\":"),
            )],
            vec![tool(0, Some("call"), Some("lookup"), Some("[]"))],
            vec![tool(0, Some("call"), Some("lookup"), Some("{}{}"))],
            vec![
                tool(0, Some("call"), Some("lookup"), Some("{}")),
                tool(0, Some("different"), None, None),
            ],
            vec![
                tool(0, Some("call"), Some("lookup"), Some("{}")),
                tool(1, Some("call"), Some("lookup"), Some("{}")),
            ],
        ] {
            events.push(ChatEvent::Finish(FinishReason::ToolCalls));
            let error = rejected(events);
            assert!(matches!(error.code, ErrorCode::Invalid));
            assert!(!error.message.contains("private-tool-data"));
        }
    }

    #[test]
    fn bounds_event_count_byte_size_and_distinct_tool_calls() {
        let mut too_many_events: Vec<_> = (0..MAX_EVENTS)
            .map(|_| ChatEvent::TextDelta(String::new()))
            .collect();
        too_many_events.push(ChatEvent::Finish(FinishReason::Stop));
        assert!(matches!(
            rejected(too_many_events).code,
            ErrorCode::LimitExceeded
        ));

        let too_many_bytes = vec![
            ChatEvent::TextDelta("x".repeat(MAX_RESPONSE_BYTES)),
            ChatEvent::ReasoningDelta("é".into()),
            ChatEvent::Finish(FinishReason::Stop),
        ];
        assert!(matches!(
            rejected(too_many_bytes).code,
            ErrorCode::LimitExceeded
        ));

        let mut too_many_calls: Vec<_> = (0..=MAX_TOOL_CALLS)
            .map(|index| tool(index as u32, None, None, None))
            .collect();
        too_many_calls.push(ChatEvent::Finish(FinishReason::ToolCalls));
        assert!(matches!(
            rejected(too_many_calls).code,
            ErrorCode::LimitExceeded
        ));
    }
}
