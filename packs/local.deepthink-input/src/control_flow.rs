//! Bounded ordinary Flow transport. A Completed frame is only a candidate;
//! successful EOF is required, and every nominal Core pull error is preserved.

use crate::tobkiri::core::types::{Error, ErrorCode};
use crate::{continuation_input::ChatEvent, error};
use serde::Deserialize;
use serde_json::{Value, json};

#[cfg(test)]
#[path = "control_flow_tests.rs"]
mod tests;

const MAX_FRAME_BYTES: usize = 1024 * 1024;
const MAX_TOTAL_BYTES: usize = 8 * 1024 * 1024;
const MAX_FRAMES: usize = 128;

pub(super) trait Source {
    fn pull(&mut self) -> Result<Vec<String>, Error>;
    fn cancel(&mut self);
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Frame {
    request: String,
    plan_digest: String,
    sequence: u64,
    kind: Kind,
}

#[derive(Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
enum Kind {
    Started {},
    StepStarted {
        step: String,
    },
    StepValue {
        step: String,
        #[serde(deserialize_with = "required_value")]
        value: Value,
    },
    Emitted {
        shape: String,
        #[serde(deserialize_with = "required_value")]
        value: Value,
    },
    StepFailed {
        step: String,
        error: FrameError,
        continued: bool,
    },
    Completed {
        #[serde(deserialize_with = "required_value")]
        value: Value,
        cost_units: u64,
    },
    Failed {
        error: FrameError,
    },
    Cancelled {
        reason: String,
    },
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct FrameError {
    code: String,
    message: String,
}

fn required_value<'de, D: serde::Deserializer<'de>>(deserializer: D) -> Result<Value, D::Error> {
    Value::deserialize(deserializer)
}

fn invalid() -> Error {
    error(
        ErrorCode::Invalid,
        "invalid DeepThink control Flow transport",
    )
}
fn limit() -> Error {
    error(
        ErrorCode::LimitExceeded,
        "DeepThink control Flow transport bound exceeded",
    )
}

fn decode(text: &str) -> Result<Frame, Error> {
    if text.len() > MAX_FRAME_BYTES {
        return Err(limit());
    }
    let frame: Frame = serde_json::from_str(text).map_err(|_| invalid())?;
    if frame.request.is_empty()
        || frame.request.len() > 255
        || !frame
            .plan_digest
            .strip_prefix("sha256:")
            .is_some_and(|digest| {
                digest.len() == 64
                    && digest
                        .bytes()
                        .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
            })
    {
        return Err(invalid());
    }
    // Decode the whole closed event variant, including fields we do not expose.
    // These public diagnostics never mint sealed Core error authority.
    match &frame.kind {
        Kind::StepStarted { step } => {
            let _ = step;
        }
        Kind::StepValue { step, value } => {
            let _ = (step, value);
        }
        Kind::StepFailed {
            step,
            error,
            continued,
        } => {
            let _ = (step, &error.code, &error.message, continued);
        }
        Kind::Emitted { shape, .. } => {
            let _ = shape;
        }
        Kind::Completed { cost_units, .. } => {
            let _ = cost_units;
        }
        Kind::Failed { error } => {
            let _ = (&error.code, &error.message);
        }
        Kind::Cancelled { reason } => {
            let _ = reason;
        }
        _ => {}
    }
    Ok(frame)
}

enum Terminal {
    Completed(Value),
    Failed,
    Cancelled,
}

struct Drain {
    identity: Option<(String, String)>,
    next: u64,
    bytes: usize,
    frames: usize,
    terminal: Option<Terminal>,
}

impl Drain {
    fn accept(&mut self, text: &str) -> Result<(), Error> {
        self.frames = self.frames.checked_add(1).ok_or_else(limit)?;
        self.bytes = self.bytes.checked_add(text.len()).ok_or_else(limit)?;
        if self.frames > MAX_FRAMES || self.bytes > MAX_TOTAL_BYTES {
            return Err(limit());
        }
        let frame = decode(text)?;
        if self.terminal.is_some() || frame.sequence != self.next {
            return Err(invalid());
        }
        match &self.identity {
            Some((request, plan)) if request == &frame.request && plan == &frame.plan_digest => {
                if matches!(frame.kind, Kind::Started {}) {
                    return Err(invalid());
                }
            }
            None if matches!(frame.kind, Kind::Started {}) && frame.sequence == 1 => {
                self.identity = Some((frame.request, frame.plan_digest));
            }
            _ => return Err(invalid()),
        }
        self.next = self.next.checked_add(1).ok_or_else(limit)?;
        self.terminal = match frame.kind {
            Kind::Completed { value, .. } => Some(Terminal::Completed(value)),
            Kind::Failed { .. } => Some(Terminal::Failed),
            Kind::Cancelled { .. } => Some(Terminal::Cancelled),
            _ => None,
        };
        Ok(())
    }

    fn finish(self, operation: &str) -> Result<String, Error> {
        let value = match self.terminal {
            Some(Terminal::Completed(value)) => value,
            Some(Terminal::Cancelled) => {
                return Err(error(
                    ErrorCode::Cancelled,
                    "DeepThink control Flow cancelled",
                ));
            }
            Some(Terminal::Failed) => {
                return Err(error(
                    ErrorCode::Internal,
                    "DeepThink control Flow failed without a typed terminal error",
                ));
            }
            None => return Err(invalid()),
        };
        let fields = value.as_object().ok_or_else(invalid)?;
        if fields.len() != 2
            || fields.get("op").and_then(Value::as_str) != Some(operation)
            || !fields.contains_key("result")
        {
            return Err(invalid());
        }
        // Core's WIT Json codec already parsed the worker JSON. Serialize its
        // actual result once, preserving null/scalar/object values from overlays.
        Ok(fields["result"].to_string())
    }
}

pub(super) fn drain(source: &mut impl Source, operation: &str) -> Result<String, Error> {
    let mut state = Drain {
        identity: None,
        next: 1,
        bytes: 0,
        frames: 0,
        terminal: None,
    };
    loop {
        // Even after a Failed/Cancelled/Completed frame, observe the original
        // Core error or successful EOF. Pull transfers cleanup custody to Core.
        let frames = source.pull()?;
        if frames.is_empty() {
            return state.finish(operation);
        }
        // The production read asks for exactly one. A larger foreign batch is
        // a local protocol failure, not EOF or a reason to re-run the Flow.
        if frames.len() != 1 {
            source.cancel();
            return Err(invalid());
        }
        if let Err(local_error) = state.accept(&frames[0]) {
            source.cancel();
            return Err(local_error);
        }
    }
}

pub(super) fn chat_event(text: &str) -> Result<ChatEvent, Error> {
    let frame = decode(text)?;
    let value = match frame.kind {
        Kind::Emitted { value, .. } => value,
        Kind::Completed { .. } => json!({"kind":"completed"}),
        // Surface the event and allow the next pull to preserve the actual
        // nominal Core error. A public failure frame is never a cleanup ACK.
        Kind::Failed { .. } => json!({"kind":"failed"}),
        Kind::Cancelled { .. } => json!({"kind":"cancelled"}),
        _ => json!({"kind":"progress"}),
    };
    let fields = value.as_object().ok_or_else(invalid)?;
    let kind = fields
        .get("kind")
        .and_then(Value::as_str)
        .ok_or_else(invalid)?;
    let text = match fields.get("text") {
        None | Some(Value::Null) => None,
        Some(Value::String(text)) => Some(text.clone()),
        _ => return Err(invalid()),
    };
    fn tokens(value: Option<&Value>) -> Result<Option<u32>, Error> {
        match value {
            None | Some(Value::Null) => Ok(None),
            Some(value) => value
                .as_u64()
                .and_then(|n| u32::try_from(n).ok())
                .map(Some)
                .ok_or_else(invalid),
        }
    }
    Ok(ChatEvent {
        kind: kind.into(),
        text,
        prompt_tokens: tokens(fields.get("prompt-tokens"))?,
        completion_tokens: tokens(fields.get("completion-tokens"))?,
    })
}
