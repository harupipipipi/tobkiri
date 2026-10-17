//! OpenAI-compatible provider guest. All network and secret access is core-gated.

wit_bindgen::generate!({ path: "wit", world: "pack",generate_all });

use exports::local::harness_chat::llm_provider::{Guest as Provider, Prompt};
use exports::tobkiri::core::{lifecycle, stream_source};
use std::{
    cell::RefCell,
    collections::{BTreeMap, VecDeque},
};
use tobkiri::core::{
    flow, network, secrets,
    types::{Error, ErrorCode, StreamHandle},
};
use tobkiri_guest_sdk::{
    openai::{ChatStream, request_body},
    sse::Decoder,
};

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct Settings {
    base_url: String,
    model: String,
    secret_name: Option<String>,
    timeout_ms: u32,
    #[serde(default)]
    provider_order: Option<Vec<String>>,
}

struct ActiveStream {
    response: network::Response,
    decoder: Decoder,
    chat: ChatStream,
    pending: VecDeque<String>,
}

#[derive(Default)]
struct State {
    settings: Option<Settings>,
    next_id: u64,
    streams: BTreeMap<u64, ActiveStream>,
}
thread_local! { static STATE: RefCell<State> = RefCell::new(State::default()); }

struct Pack;
fn error(code: ErrorCode, message: &str) -> Error {
    Error {
        code,
        message: message.into(),
    }
}

impl lifecycle::Guest for Pack {
    fn init(config: String) -> Result<(), Error> {
        let settings: Settings = serde_json::from_str(&config)
            .map_err(|_| error(ErrorCode::Invalid, "provider settings are invalid"))?;
        if !(settings.base_url.starts_with("https://") || settings.base_url.starts_with("http://"))
            || !settings.base_url.ends_with("/v1")
            || settings.base_url.contains('@')
            || settings.base_url.contains('?')
            || settings.base_url.contains('#')
            || settings.model.is_empty()
            || settings.timeout_ms == 0
            || settings.timeout_ms > 300_000
        {
            return Err(error(
                ErrorCode::Invalid,
                "provider requires an explicit HTTP /v1 URL, model and bounded timeout",
            ));
        }
        if settings.provider_order.as_ref().is_some_and(|order| {
            order.is_empty()
                || order.len() > 16
                || order.iter().any(|name| {
                    name.is_empty() || name.len() > 128 || name.chars().any(char::is_control)
                })
        }) {
            return Err(error(
                ErrorCode::Invalid,
                "provider routing must name 1..16 explicit providers",
            ));
        }
        STATE.with_borrow_mut(|state| {
            state.settings = Some(settings);
            state.streams.clear();
        });
        Ok(())
    }
    fn shutdown() {
        STATE.with_borrow_mut(|state| {
            state.streams.clear();
            state.settings = None;
        });
    }
}

impl Provider for Pack {
    fn chat(prompt: Prompt) -> Result<StreamHandle, Error> {
        if flow::is_cancelled() {
            return Err(error(ErrorCode::Cancelled, "run cancelled"));
        }
        STATE.with_borrow_mut(|state| {
            if state.streams.len() >= 64 {
                return Err(error(ErrorCode::LimitExceeded, "too many active streams"));
            }
            let config = state
                .settings
                .as_ref()
                .ok_or_else(|| error(ErrorCode::Invalid, "provider has not been initialized"))?;
            if !prompt.model.is_empty() && prompt.model != config.model {
                return Err(error(
                    ErrorCode::Invalid,
                    "model does not match the approved provider settings",
                ));
            }
            let messages = prompt
                .messages
                .into_iter()
                .map(|message| serde_json::json!({"role":message.role,"content":message.content}))
                .collect::<Vec<_>>();
            let body = request_body(
                &config.model,
                messages,
                prompt.max_tokens,
                config.provider_order.as_deref(),
            )
            .to_string()
            .into_bytes();
            let secret = match &config.secret_name {
                Some(name) => Some(secrets::get(name)?),
                None => None,
            };
            let secret_headers = if secret.is_some() {
                vec![network::HeaderFromSecret {
                    name: "authorization".into(),
                    prefix: "Bearer ".into(),
                    index: 0,
                }]
            } else {
                vec![]
            };
            let request = network::Request {
                method: "POST".into(),
                url: format!("{}/chat/completions", config.base_url),
                headers: vec![
                    ("content-type".into(), "application/json".into()),
                    ("accept".into(), "text/event-stream".into()),
                ],
                secret_headers,
                body: Some(body),
                timeout_ms: Some(config.timeout_ms),
            };
            let borrowed: Vec<&secrets::Secret> = secret.iter().collect();
            let response = network::fetch(&request, &borrowed)?;
            if !(200..300).contains(&response.status()) {
                return Err(error(
                    ErrorCode::Unavailable,
                    "provider rejected the request",
                ));
            }
            let is_sse = response.headers().iter().any(|(name, value)| {
                name.eq_ignore_ascii_case("content-type") && value.starts_with("text/event-stream")
            });
            if !is_sse {
                return Err(error(ErrorCode::Invalid, "provider response is not SSE"));
            }
            state.next_id = state
                .next_id
                .checked_add(1)
                .ok_or_else(|| error(ErrorCode::LimitExceeded, "stream handle exhaustion"))?;
            let id = state.next_id;
            state.streams.insert(
                id,
                ActiveStream {
                    response,
                    decoder: Decoder::default(),
                    chat: ChatStream::default(),
                    pending: VecDeque::new(),
                },
            );
            Ok(StreamHandle { id })
        })
    }
}

impl stream_source::Guest for Pack {
    fn pull(local_id: u64, max: u32) -> Result<Vec<String>, Error> {
        if max == 0 || max > 64 {
            return Err(error(ErrorCode::Invalid, "pull batch must be 1..64"));
        }
        if flow::is_cancelled() {
            Self::cancel(local_id);
            return Err(error(ErrorCode::Cancelled, "run cancelled"));
        }
        let result = STATE.with_borrow_mut(|state| {
            let stream = state
                .streams
                .get_mut(&local_id)
                .ok_or_else(|| error(ErrorCode::NotFound, "stream not found"))?;
            if stream.chat.is_done() && stream.pending.is_empty() {
                state.streams.remove(&local_id);
                return Ok(vec![]);
            }
            let mut events = Vec::new();
            while events.len() < max as usize {
                if flow::is_cancelled() {
                    state.streams.remove(&local_id);
                    return Err(error(ErrorCode::Cancelled, "run cancelled"));
                }
                if let Some(event) = stream.pending.pop_front() {
                    events.push(event);
                    continue;
                }
                if stream.chat.is_done() {
                    break;
                }
                if let Some(frame) = stream.decoder.next_frame() {
                    stream.pending.extend(
                        stream
                            .chat
                            .accept(&frame)
                            .map_err(|message| error(ErrorCode::Invalid, message))?,
                    );
                    continue;
                }
                match stream.response.read(8192)? {
                    Some(chunk) => stream
                        .decoder
                        .feed(&chunk)
                        .map_err(|message| error(ErrorCode::Invalid, message))?,
                    None => {
                        stream
                            .decoder
                            .finish()
                            .map_err(|message| error(ErrorCode::Invalid, message))?;
                        return Err(error(
                            ErrorCode::Invalid,
                            "provider ended before SSE [DONE]",
                        ));
                    }
                }
            }
            Ok(events)
        });
        if result.is_err() {
            Self::cancel(local_id);
        }
        result
    }
    fn cancel(local_id: u64) {
        STATE.with_borrow_mut(|state| {
            state.streams.remove(&local_id);
        });
    }
}

export!(Pack);
