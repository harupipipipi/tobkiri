//! Assemble a typed assistant response and bind tool results to its call IDs.
wit_bindgen::generate!({
    path: [
        "../../crates/tobkiri-wit/wit/core/core.wit",
        "../shapes/harness-chat-v2/harness-chat.wit",
        "wit",
    ],
    world: "local:harness-turn/pack@1.0.0",
    generate_all,
});

mod response;

use exports::local::harness_chat::assistant_output;
use exports::local::harness_chat::model_response::{self, CompletedResponse};
use exports::tobkiri::core::lifecycle;
use local::harness_chat::messages::{
    ChatEvent, Context, FinishReason, Input, Message, MessagePart, Prompt, ResponseSummary,
    ToolCall, ToolResult, Usage,
};
use std::collections::BTreeMap;
use tobkiri::core::types::{Error, ErrorCode};

struct Pack;

impl lifecycle::Guest for Pack {
    fn init(_config: String) -> Result<(), Error> {
        Ok(())
    }

    fn shutdown() {}
}

// Flow publishes the typed event after reaching this sink. The sink needs no
// additional capability or durable state.
impl assistant_output::Guest for Pack {
    fn emit(_event: ChatEvent) -> Result<(), Error> {
        Ok(())
    }
}

impl model_response::Guest for Pack {
    fn start(ctx: Context, input: Input, model: String, max_tokens: u32) -> Result<Prompt, Error> {
        if ctx.conversation != input.conversation || ctx.conversation.is_empty() {
            return Err(invalid(
                "prompt context belongs to a different conversation",
            ));
        }
        if input.parts.is_empty()
            || input
                .parts
                .iter()
                .any(|part| !matches!(part, MessagePart::Text(_)))
        {
            return Err(invalid("user input requires text parts"));
        }
        let mut messages = ctx.messages;
        messages.push(Message {
            role: "user".into(),
            parts: input.parts,
            response: None,
        });
        Ok(Prompt {
            messages,
            tools: input.tools,
            model,
            max_tokens,
        })
    }

    fn assemble(events: Vec<ChatEvent>) -> Result<CompletedResponse, Error> {
        response::assemble(events)
    }

    fn bind_result(call_id: String, content: String, is_error: bool) -> Result<ToolResult, Error> {
        if call_id.trim().is_empty() {
            return Err(invalid("tool result requires a call ID"));
        }
        Ok(ToolResult {
            call_id,
            content,
            is_error,
        })
    }

    fn follow_up(
        previous: Prompt,
        assistant: Message,
        results: Vec<ToolResult>,
    ) -> Result<Prompt, Error> {
        follow_up(previous, assistant, results)
    }
}

fn follow_up(
    mut previous: Prompt,
    assistant: Message,
    results: Vec<ToolResult>,
) -> Result<Prompt, Error> {
    if assistant.role != "assistant"
        || !assistant
            .response
            .as_ref()
            .is_some_and(|summary| matches!(summary.finish, FinishReason::ToolCalls))
        || results.is_empty()
    {
        return Err(invalid(
            "follow-up requires a completed assistant tool call",
        ));
    }

    let mut results_by_id = BTreeMap::new();
    for result in results {
        if result.call_id.trim().is_empty()
            || results_by_id
                .insert(result.call_id.clone(), result)
                .is_some()
        {
            return Err(invalid(
                "follow-up contains a missing or duplicate result ID",
            ));
        }
    }
    let mut parts = Vec::new();
    for part in &assistant.parts {
        match part {
            MessagePart::ToolCall(call) => {
                let result = results_by_id.remove(&call.id).ok_or_else(|| {
                    invalid("follow-up result does not match the assistant tool call")
                })?;
                parts.push(MessagePart::ToolResult(result));
            }
            MessagePart::ToolResult(_) => {
                return Err(invalid("assistant tool call contains a result"));
            }
            MessagePart::Text(_) | MessagePart::Reasoning(_) => {}
        }
    }
    if parts.is_empty() || !results_by_id.is_empty() {
        return Err(invalid("follow-up contains an unmatched result"));
    }

    previous.messages.extend([
        assistant,
        Message {
            role: "tool".into(),
            parts,
            response: None,
        },
    ]);
    Ok(previous)
}

fn invalid(message: &str) -> Error {
    Error {
        code: ErrorCode::Invalid,
        message: message.into(),
    }
}

export!(Pack);

#[cfg(test)]
mod tests {
    use super::*;

    fn prompt() -> Prompt {
        Prompt {
            messages: vec![Message {
                role: "user".into(),
                parts: vec![MessagePart::Text("hello".into())],
                response: None,
            }],
            tools: vec![],
            model: "saved-model".into(),
            max_tokens: 32,
        }
    }

    fn assistant(ids: &[&str]) -> Message {
        Message {
            role: "assistant".into(),
            parts: ids
                .iter()
                .map(|id| {
                    MessagePart::ToolCall(ToolCall {
                        id: (*id).into(),
                        name: "rewrite".into(),
                        arguments: "{}".into(),
                    })
                })
                .collect(),
            response: Some(ResponseSummary {
                finish: FinishReason::ToolCalls,
                usage: None,
            }),
        }
    }

    fn result(id: &str, is_error: bool) -> ToolResult {
        ToolResult {
            call_id: id.into(),
            content: "result".into(),
            is_error,
        }
    }

    #[test]
    fn binds_parallel_results_in_call_order_and_keeps_model_settings() {
        let next = follow_up(
            prompt(),
            assistant(&["a", "b"]),
            vec![result("b", true), result("a", false)],
        )
        .unwrap();
        assert_eq!(next.model, "saved-model");
        assert_eq!(next.max_tokens, 32);
        assert_eq!(
            next.messages
                .iter()
                .map(|message| message.role.as_str())
                .collect::<Vec<_>>(),
            ["user", "assistant", "tool"]
        );
        assert!(
            matches!(next.messages[2].parts.as_slice(), [MessagePart::ToolResult(a), MessagePart::ToolResult(b)] if a.call_id == "a" && !a.is_error && b.call_id == "b" && b.is_error)
        );
    }

    #[test]
    fn rejects_missing_duplicate_and_unrelated_results() {
        for results in [
            vec![],
            vec![result("b", false)],
            vec![result("a", false), result("a", false)],
            vec![result("a", false), result("b", false)],
        ] {
            assert!(follow_up(prompt(), assistant(&["a"]), results).is_err());
        }
        assert!(follow_up(prompt(), assistant(&["a", "a"]), vec![result("a", false)]).is_err());
    }
}
