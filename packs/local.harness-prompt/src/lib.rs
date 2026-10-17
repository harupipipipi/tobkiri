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
}
thread_local! {static SETTINGS:RefCell<Option<Settings>>=const{RefCell::new(None)};}
struct Pack;
impl lifecycle::Guest for Pack {
    fn init(config: String) -> Result<(), Error> {
        let c: Settings = serde_json::from_str(&config).map_err(|_| Error {
            code: ErrorCode::Invalid,
            message: "prompt settings invalid".into(),
        })?;
        // Zero selects the provider default. An explicit positive cap is preserved.
        if c.model.is_empty() {
            return Err(Error {
                code: ErrorCode::Invalid,
                message: "explicit model required".into(),
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
            messages.extend(ctx.messages);
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
