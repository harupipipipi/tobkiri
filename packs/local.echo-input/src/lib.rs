//! Terminal input is a Pack; its payload enters the Profile's Flow trigger.
wit_bindgen::generate!({path:"wit",world:"pack",generate_all});
use exports::local::echo_chat::user_input::{Guest, Input};
use exports::tobkiri::core::lifecycle;
use std::cell::RefCell;
use tobkiri::core::{
    flow, host_io,
    types::{Error, ErrorCode, StreamHandle},
};
#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct Settings {
    conversation: String,
    prompt: String,
    flow: String,
}
thread_local! {static SETTINGS:RefCell<Option<Settings>>=const{RefCell::new(None)};}
struct Pack;
impl lifecycle::Guest for Pack {
    fn init(config: String) -> Result<(), Error> {
        let c: Settings = serde_json::from_str(&config).map_err(|_| Error {
            code: ErrorCode::Invalid,
            message: "input settings invalid".into(),
        })?;
        if c.conversation.is_empty() {
            return Err(Error {
                code: ErrorCode::Invalid,
                message: "conversation required".into(),
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
    fn read() -> Result<Input, Error> {
        SETTINGS.with_borrow(|s| {
            let c = s.as_ref().ok_or_else(|| Error {
                code: ErrorCode::Invalid,
                message: "input not initialized".into(),
            })?;
            let text = host_io::terminal_read_line(&c.prompt)?;
            Ok(Input {
                conversation: c.conversation.clone(),
                text,
            })
        })
    }
    fn submit(input: Input) -> Result<StreamHandle, Error> {
        SETTINGS.with_borrow(|s| {
            let c = s.as_ref().ok_or_else(|| Error {
                code: ErrorCode::Invalid,
                message: "input not initialized".into(),
            })?;
            let payload = serde_json::json!({"conversation":input.conversation,"text":input.text})
                .to_string();
            flow::run_flow(&c.flow, &payload)
        })
    }
    fn pull(
        stream: StreamHandle,
        max: u32,
    ) -> Result<Vec<exports::local::echo_chat::user_input::ChatEvent>, Error> {
        if max == 0 || max > 64 {
            return Err(Error {
                code: ErrorCode::Invalid,
                message: "input pull bound must be 1..64".into(),
            });
        }
        let events = flow::pull(stream, max)?;
        let mut output = Vec::with_capacity(events.len());
        for event in events {
            let envelope: serde_json::Value = serde_json::from_str(&event).map_err(|_| Error {
                code: ErrorCode::Invalid,
                message: "invalid Flow event".into(),
            })?;
            let frame = envelope
                .get("kind")
                .and_then(|v| v.as_object())
                .ok_or_else(|| Error {
                    code: ErrorCode::Invalid,
                    message: "missing Flow event kind".into(),
                })?;
            let kind = frame
                .get("kind")
                .and_then(|v| v.as_str())
                .ok_or_else(|| Error {
                    code: ErrorCode::Invalid,
                    message: "invalid Flow event kind".into(),
                })?;
            let value = match kind {
                "emitted" => frame
                    .get("value")
                    .ok_or_else(|| Error {
                        code: ErrorCode::Invalid,
                        message: "missing emitted value".into(),
                    })?
                    .clone(),
                "completed" => serde_json::json!({"kind":"completed"}),
                "failed" => {
                    return Err(Error {
                        code: ErrorCode::Internal,
                        message: "Flow failed; conversation was not confirmed saved".into(),
                    });
                }
                "cancelled" => {
                    return Err(Error {
                        code: ErrorCode::Cancelled,
                        message: "Flow cancelled; conversation was not confirmed saved".into(),
                    });
                }
                "started" | "step_started" | "step_value" | "step_failed" => {
                    serde_json::json!({"kind":"progress"})
                }
                _ => {
                    return Err(Error {
                        code: ErrorCode::Invalid,
                        message: "unknown Flow event kind".into(),
                    });
                }
            };
            let kind = value
                .get("kind")
                .and_then(|v| v.as_str())
                .ok_or_else(|| Error {
                    code: ErrorCode::Invalid,
                    message: "invalid chat event".into(),
                })?;
            output.push(exports::local::echo_chat::user_input::ChatEvent {
                kind: kind.into(),
                text: value.get("text").and_then(|v| v.as_str()).map(String::from),
                prompt_tokens: value
                    .get("prompt-tokens")
                    .and_then(|v| v.as_u64())
                    .and_then(|v| u32::try_from(v).ok()),
                completion_tokens: value
                    .get("completion-tokens")
                    .and_then(|v| v.as_u64())
                    .and_then(|v| u32::try_from(v).ok()),
            });
        }
        Ok(output)
    }
    fn cancel(stream: StreamHandle) {
        flow::cancel_stream(stream);
    }
}
export!(Pack);
