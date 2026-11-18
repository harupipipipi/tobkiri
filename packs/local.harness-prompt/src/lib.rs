//! Pack-owned prompt construction. The core knows none of these domain types.
wit_bindgen::generate!({path:"wit",world:"pack",generate_all});
use exports::local::harness_chat::prompt_builder::{Context, Guest, Input, Prompt};
use exports::tobkiri::core::lifecycle;
use local::harness_chat::messages::Message;
use std::cell::RefCell;
use tobkiri::core::types::{Error, ErrorCode};

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct Settings {
    model: String,
    system_prompt: String,
    max_tokens: u32,
    // Counts the UTF-8 role and content bytes of saved messages. Zero omits history.
    #[serde(default = "default_max_history_bytes")]
    max_history_bytes: u32,
}

fn default_max_history_bytes() -> u32 {
    // The packaged Gemma 3 1B run uses a 4,096-token context and 128 output tokens.
    // Use a conservative history byte budget independent of the model's tokenizer.
    2_048
}

fn history_start(history: &[Message], mut remaining_bytes: usize) -> usize {
    if remaining_bytes == 0 {
        return history.len();
    }

    // Find the longest suffix of complete turns. A user row starts a turn.
    let mut start = history.len();
    for (index, message) in history.iter().enumerate().rev() {
        let message_bytes = message.role.len() + message.content.len();
        let Some(remaining) = remaining_bytes.checked_sub(message_bytes) else {
            return start;
        };
        remaining_bytes = remaining;
        if message.role == "user" {
            start = index;
        }
    }

    // A fitting history can also include imported rows before its first user row.
    0
}

thread_local! {static SETTINGS:RefCell<Option<Settings>>=const{RefCell::new(None)};}
struct Pack;
impl lifecycle::Guest for Pack {
    fn init(config: String) -> Result<(), Error> {
        let c: Settings = serde_json::from_str(&config).map_err(|_| Error {
            code: ErrorCode::Invalid,
            message: "prompt settings invalid".into(),
        })?;
        if c.model.is_empty() || c.max_tokens == 0 {
            return Err(Error {
                code: ErrorCode::Invalid,
                message: "explicit model and bounded tokens required".into(),
            });
        }
        SETTINGS.with_borrow_mut(|s| *s = Some(c));
        Ok(())
    }
    fn shutdown() {
        SETTINGS.with_borrow_mut(|s| *s = None);
    }
}
impl Guest for Pack {
    fn build(ctx: Context, input: Input) -> Result<Prompt, Error> {
        SETTINGS.with_borrow(|s| {
            let c = s.as_ref().ok_or_else(|| Error {
                code: ErrorCode::Invalid,
                message: "prompt not initialized".into(),
            })?;
            if ctx.conversation != input.conversation {
                return Err(Error {
                    code: ErrorCode::Invalid,
                    message: "conversation binding mismatch".into(),
                });
            }
            let mut messages = vec![Message {
                role: "system".into(),
                content: c.system_prompt.clone(),
            }];
            let start = history_start(&ctx.messages, c.max_history_bytes as usize);
            messages.extend(ctx.messages.into_iter().skip(start));
            messages.push(Message {
                role: "user".into(),
                content: input.text,
            });
            Ok(Prompt {
                messages,
                model: c.model.clone(),
                max_tokens: c.max_tokens,
            })
        })
    }
}
export!(Pack);

#[cfg(test)]
mod tests {
    use super::*;

    fn message(role: &str, content: &str) -> Message {
        Message {
            role: role.into(),
            content: content.into(),
        }
    }

    #[test]
    fn configured_token_limits_reach_the_prompt_without_truncation() {
        for limit in [1, 512, 513, 4096, u32::MAX] {
            let config = serde_json::json!({
                "model": "local-model",
                "system_prompt": "system",
                "max_tokens": limit,
            });
            <Pack as lifecycle::Guest>::init(config.to_string()).unwrap();
            let prompt = <Pack as Guest>::build(
                Context {
                    conversation: "chat".into(),
                    messages: vec![Message {
                        role: "assistant".into(),
                        content: "previous".into(),
                    }],
                },
                Input {
                    conversation: "chat".into(),
                    text: "next".into(),
                },
            )
            .unwrap();
            assert_eq!(prompt.max_tokens, limit);
            assert_eq!(prompt.model, "local-model");
            assert_eq!(
                prompt
                    .messages
                    .iter()
                    .map(|message| message.content.as_str())
                    .collect::<Vec<_>>(),
                ["system", "previous", "next"],
            );
            <Pack as lifecycle::Guest>::shutdown();
        }
        let zero = serde_json::json!({
            "model": "local-model", "system_prompt": "system", "max_tokens": 0,
        });
        assert!(<Pack as lifecycle::Guest>::init(zero.to_string()).is_err());
    }

    #[test]
    fn history_trimming_counts_utf8_bytes_and_keeps_the_newest_complete_turn() {
        let config = serde_json::json!({
            "model": "local-model",
            "system_prompt": "  system\n",
            "max_tokens": 513,
            "max_history_bytes": 39,
        });
        <Pack as lifecycle::Guest>::init(config.to_string()).unwrap();
        let prompt = <Pack as Guest>::build(
            Context {
                conversation: "chat".into(),
                messages: vec![
                    message("user", "旧"),
                    message("assistant", "古"),
                    message("user", "猫🐈"),
                    message("assistant", "はい"),
                ],
            },
            Input {
                conversation: "chat".into(),
                text: "  新しい入力\n".into(),
            },
        )
        .unwrap();

        assert_eq!(prompt.model, "local-model");
        assert_eq!(prompt.max_tokens, 513);
        assert_eq!(
            prompt
                .messages
                .iter()
                .map(|message| (message.role.as_str(), message.content.as_str()))
                .collect::<Vec<_>>(),
            [
                ("system", "  system\n"),
                ("user", "猫🐈"),
                ("assistant", "はい"),
                ("user", "  新しい入力\n"),
            ],
        );
        <Pack as lifecycle::Guest>::shutdown();
    }

    #[test]
    fn history_at_the_limit_keeps_unpaired_imported_rows() {
        let history = [
            message("assistant", "imported"),
            message("user", "first"),
            message("user", "second"),
            message("assistant", "reply"),
        ];
        assert_eq!(history_start(&history, 50), 0);
        assert_eq!(history_start(&history, 49), 1);
        assert_eq!(history_start(&history, 33), 1);
        assert_eq!(history_start(&history, 32), 2);
        assert_eq!(history_start(&history, 0), history.len());
    }

    #[test]
    fn oversized_newest_turn_drops_all_history() {
        let history = [
            message("user", "a"),
            message("assistant", "b"),
            message("user", "latest"),
            message("assistant", "answer"),
        ];
        assert_eq!(history_start(&history, 25), 2);
        assert_eq!(history_start(&history, 24), history.len());
    }

    #[test]
    fn packaged_config_applies_the_default_history_budget() {
        let config: Settings = serde_json::from_str(include_str!("../config.json")).unwrap();
        let history = [message("user", &"a".repeat(2_045))];
        assert_eq!(
            history_start(&history, config.max_history_bytes as usize),
            history.len(),
        );
    }
}
