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
use exports::local::harness_chat::model_response::{self, CompletedResponse, TurnPrompt};
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
    fn start(
        ctx: Context,
        input: Input,
        model: String,
        max_tokens: u32,
        max_history_bytes: u32,
    ) -> Result<TurnPrompt, Error> {
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
        let start = history_start(&ctx.messages, max_history_bytes as usize);
        let mut messages = ctx.messages;
        messages.drain(..start);
        let prior_messages = u32::try_from(messages.len())
            .map_err(|_| invalid("conversation history is too long"))?;
        messages.push(Message {
            role: "user".into(),
            parts: input.parts,
            response: None,
        });
        Ok(TurnPrompt {
            prompt: Prompt {
                messages,
                tools: input.tools,
                model,
                max_tokens,
            },
            prior_messages,
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
        mut previous: TurnPrompt,
        assistant: Message,
        results: Vec<ToolResult>,
    ) -> Result<TurnPrompt, Error> {
        previous.prompt = follow_up(previous.prompt, assistant, results)?;
        Ok(previous)
    }

    fn finish_turn(turn: TurnPrompt, assistant: Message) -> Result<Vec<Message>, Error> {
        finish_turn(turn, assistant)
    }
}

fn history_start(history: &[Message], mut remaining: usize) -> usize {
    if remaining == 0 {
        return history.len();
    }
    // The boundary moves only at a user message, keeping each saved tool
    // call with its results in the next model request.
    let mut start = history.len();
    for (index, message) in history.iter().enumerate().rev() {
        let bytes = message
            .parts
            .iter()
            .fold(message.role.len(), |bytes, part| {
                let part_bytes = match part {
                    MessagePart::Text(text) | MessagePart::Reasoning(text) => text.len(),
                    MessagePart::ToolCall(call) => call
                        .id
                        .len()
                        .saturating_add(call.name.len())
                        .saturating_add(call.arguments.len()),
                    MessagePart::ToolResult(result) => {
                        result.call_id.len().saturating_add(result.content.len())
                    }
                };
                bytes.saturating_add(part_bytes)
            });
        let Some(next) = remaining.checked_sub(bytes) else {
            return start;
        };
        remaining = next;
        if message.role == "user" {
            start = index;
        }
    }
    0
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

fn finish_turn(mut turn: TurnPrompt, assistant: Message) -> Result<Vec<Message>, Error> {
    if assistant.role != "assistant"
        || !assistant
            .response
            .as_ref()
            .is_some_and(|summary| !matches!(summary.finish, FinishReason::ToolCalls))
    {
        return Err(invalid("turn requires a completed final assistant message"));
    }

    let prior = turn.prior_messages as usize;
    if !turn
        .prompt
        .messages
        .get(prior)
        .is_some_and(|message| message.role == "user")
    {
        return Err(invalid("turn prompt is missing its user message"));
    }
    let mut completed = turn.prompt.messages.split_off(prior + 1);
    completed.push(assistant);
    Ok(completed)
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

    #[test]
    fn finished_turn_contributes_only_new_assistant_and_tool_messages() {
        let context = Context {
            conversation: "one".into(),
            messages: prompt().messages,
        };
        let input = Input {
            conversation: "one".into(),
            parts: vec![MessagePart::Text("next user".into())],
            tools: vec![],
        };
        let turn = <Pack as model_response::Guest>::start(context, input, "model".into(), 32, 2048)
            .unwrap();
        let turn = <Pack as model_response::Guest>::follow_up(
            turn,
            assistant(&["a"]),
            vec![result("a", false)],
        )
        .unwrap();
        let final_reply = Message {
            role: "assistant".into(),
            parts: vec![MessagePart::Text("finished".into())],
            response: Some(ResponseSummary {
                finish: FinishReason::Stop,
                usage: None,
            }),
        };
        let saved = finish_turn(turn, final_reply).unwrap();
        let roles = saved
            .iter()
            .map(|message| message.role.as_str())
            .collect::<Vec<_>>();
        assert_eq!(roles, ["assistant", "tool", "assistant"]);
        assert!(
            matches!(saved[2].parts.as_slice(), [MessagePart::Text(text)] if text == "finished")
        );
    }

    #[test]
    fn history_budget_keeps_whole_tool_turns_and_counts_utf8() {
        let text = |role: &str, text: &str| Message {
            role: role.into(),
            parts: vec![MessagePart::Text(text.into())],
            response: None,
        };
        let history = [
            text("user", "古い質問"),
            text("assistant", "古い回答"),
            text("user", "猫"),
            assistant(&["a"]),
            Message {
                role: "tool".into(),
                parts: vec![MessagePart::ToolResult(result("a", false))],
                response: None,
            },
            text("assistant", "完了"),
        ];
        // Role and part lengths: 7 user, 19 calling assistant, 11 result, 15 reply.
        assert_eq!(history_start(&history, 52), 2);
        assert_eq!(history_start(&history, 51), history.len());
        assert_eq!(history_start(&history, 0), history.len());
        assert_eq!(history_start(&history, 2048), 0);
    }
}
