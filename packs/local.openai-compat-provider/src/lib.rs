//! OpenAI-compatible provider guest. All network and secret access is core-gated.

wit_bindgen::generate!({ path: "wit", world: "pack",generate_all });

use exports::local::harness_chat::llm_provider::{Guest as Provider, Prompt};
use exports::local::harness_chat::structured_llm_provider::{
    Guest as StructuredProvider, ResponseFormat,
};
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
    openai::{ChatStream, request_body_with_json_mode, request_body_with_schema},
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
    #[serde(default)]
    json_mode: bool,
}

struct ActiveStream {
    response: network::Response,
    decoder: Decoder,
    chat: ChatStream,
    pending: VecDeque<String>,
    failure: Option<BufferedFailure>,
}

#[derive(Clone, Copy)]
enum BufferedFailure {
    // Decoder::feed may have queued valid frames before a later framing error.
    // Deliver only that queued prefix, then report the fixed error without a read.
    AfterQueuedFrames(&'static str),
    Ready(&'static str),
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
        start_chat(prompt, None)
    }
}

impl StructuredProvider for Pack {
    fn chat(prompt: Prompt, response_format: ResponseFormat) -> Result<StreamHandle, Error> {
        start_chat(prompt, Some(response_format))
    }
}

fn start_chat(
    prompt: Prompt,
    response_format: Option<ResponseFormat>,
) -> Result<StreamHandle, Error> {
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
        let request_body = if let Some(format) = response_format {
            let schema = serde_json::from_str(&format.schema)
                .map_err(|_| error(ErrorCode::Invalid, "structured response schema is not JSON"))?;
            request_body_with_schema(
                &config.model,
                messages,
                prompt.max_tokens,
                config.provider_order.as_deref(),
                &format.name,
                schema,
                format.strict,
            )
            .map_err(|message| error(ErrorCode::Invalid, message))?
        } else {
            request_body_with_json_mode(
                &config.model,
                messages,
                prompt.max_tokens,
                config.provider_order.as_deref(),
                config.json_mode,
            )
        };
        let body = request_body.to_string().into_bytes();
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
        let mut chat = ChatStream::default();
        let mut pending = VecDeque::new();
        // Keep only the public provider receipt. Never forward raw headers.
        for (_, id) in response
            .headers()
            .iter()
            .filter(|(name, _)| name.eq_ignore_ascii_case("x-generation-id"))
        {
            pending.push_back(
                chat.generation_header(id)
                    .map_err(|message| error(ErrorCode::Invalid, message))?,
            );
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
                chat,
                pending,
                failure: None,
            },
        );
        Ok(StreamHandle { id })
    })
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
            pull_events(
                &mut stream.decoder,
                &mut stream.chat,
                &mut stream.pending,
                &mut stream.failure,
                max as usize,
                || {
                    if flow::is_cancelled() {
                        return Err(error(ErrorCode::Cancelled, "run cancelled"));
                    }
                    stream.response.read(8192)
                },
                |message| error(ErrorCode::Invalid, message),
            )
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

// Batch only already buffered frames after public events. Never perform another
// read with a public batch in hand. Bounded metadata-only work yields a heartbeat.
fn pull_events<E>(
    decoder: &mut Decoder,
    chat: &mut ChatStream,
    pending: &mut VecDeque<String>,
    failure: &mut Option<BufferedFailure>,
    max: usize,
    mut read: impl FnMut() -> Result<Option<Vec<u8>>, E>,
    invalid: impl Fn(&'static str) -> E,
) -> Result<Vec<String>, E> {
    if let Some(BufferedFailure::Ready(message)) = *failure {
        return Err(invalid(message));
    }
    let mut events = Vec::new();
    let mut reads = 0;
    let mut frames = 0;
    let mut received = 0usize;
    loop {
        while events.len() < max {
            let Some(event) = pending.pop_front() else {
                break;
            };
            events.push(event);
        }
        if events.len() == max || chat.is_done() {
            return Ok(events);
        }
        if frames >= 16 {
            break;
        }
        if let Some(frame) = decoder.next_frame() {
            frames += 1;
            match chat.accept(&frame) {
                Ok(accepted) => pending.extend(accepted),
                Err(message) => {
                    // Earlier public evidence must reach its caller's durable
                    // recorder before this later frame failure is delivered.
                    *failure = Some(BufferedFailure::Ready(message));
                    if events.is_empty() {
                        return Err(invalid(message));
                    }
                    return Ok(events);
                }
            }
            continue;
        }
        if let Some(BufferedFailure::AfterQueuedFrames(message)) = *failure {
            *failure = Some(BufferedFailure::Ready(message));
            if events.is_empty() {
                return Err(invalid(message));
            }
            return Ok(events);
        }
        if !events.is_empty() || reads >= 1 {
            break;
        }
        reads += 1;
        match read()? {
            Some(chunk) => {
                received += chunk.len();
                if let Err(message) = decoder.feed(&chunk) {
                    // feed stops at its first invalid frame. Any queued frames
                    // precede that failure; later bytes are never fed again.
                    *failure = Some(BufferedFailure::AfterQueuedFrames(message));
                }
            }
            None => {
                decoder.finish().map_err(&invalid)?;
                return Err(invalid("provider ended before SSE [DONE]"));
            }
        }
    }
    if !events.is_empty() {
        return Ok(events);
    }
    Ok(vec![
        serde_json::json!({"kind":"progress","text":serde_json::json!({
        "version":"tobkiri.chat.transport.v1","received_bytes":received,"processed_frames":frames
    }).to_string(),"prompt-tokens":null,"completion-tokens":null})
        .to_string(),
    ])
}

#[cfg(test)]
mod tests {
    use super::*;

    fn frame(value: serde_json::Value) -> String {
        format!("data: {value}\n\n")
    }

    fn delta(text: &str) -> String {
        frame(serde_json::json!({"choices":[{"delta":{"content":text}}]}))
    }

    fn decoded(events: &[String]) -> Vec<serde_json::Value> {
        events
            .iter()
            .map(|event| serde_json::from_str(event).unwrap())
            .collect()
    }

    #[test]
    fn buffered_public_frames_share_one_batch_without_another_read() {
        let chunk = [
            frame(serde_json::json!({"id":"gen-batch","choices":[{"delta":{"content":"3"}}]})),
            delta("91"),
            frame(
                serde_json::json!({"choices":[{"delta":{},"finish_reason":"stop"}],
                "usage":{"prompt_tokens":2,"completion_tokens":3}}),
            ),
            "data: [DONE]\n\n".into(),
        ]
        .concat()
        .into_bytes();
        let mut decoder = Decoder::default();
        let mut chat = ChatStream::default();
        let mut reads = 0;
        let events = pull_events(
            &mut decoder,
            &mut chat,
            &mut VecDeque::new(),
            &mut None,
            64,
            || {
                reads += 1;
                assert_eq!(reads, 1, "a public batch must not issue another read");
                Ok::<_, &str>(Some(chunk.clone()))
            },
            |error| error,
        )
        .unwrap();
        assert_eq!(reads, 1);
        let events = decoded(&events);
        assert_eq!(
            events
                .iter()
                .map(|event| event["kind"].as_str().unwrap())
                .collect::<Vec<_>>(),
            ["metadata", "delta", "delta", "metadata", "usage", "done"]
        );
        assert_eq!(events[1]["text"], "3");
        assert_eq!(events[2]["text"], "91");
        assert_eq!(events[4]["prompt-tokens"], 2);
        assert_eq!(events[4]["completion-tokens"], 3);
        assert_eq!(events[5]["text"], "391");
        assert!(chat.is_done());
    }

    #[test]
    fn public_prefix_precedes_a_later_buffered_contract_failure() {
        let mut decoder = Decoder::default();
        decoder.feed([
            frame(serde_json::json!({"id":"gen-prefix","choices":[{"delta":{"content":"391"}}]})),
            "data: invalid-json\n\n".into(),
            delta("must-not-be-accepted"),
        ].concat().as_bytes()).unwrap();
        let mut chat = ChatStream::default();
        let mut pending = VecDeque::new();
        let mut failure = None;
        let first = pull_events(
            &mut decoder,
            &mut chat,
            &mut pending,
            &mut failure,
            64,
            || Err("unexpected read"),
            |error| error,
        )
        .unwrap();
        let first = decoded(&first);
        assert_eq!(first.len(), 2);
        assert_eq!(first[0]["kind"], "metadata");
        assert!(first[0]["text"].as_str().unwrap().contains("gen-prefix"));
        assert_eq!(first[1]["text"], "391");
        assert_eq!(
            pull_events(
                &mut decoder,
                &mut chat,
                &mut pending,
                &mut failure,
                64,
                || Err("unexpected read"),
                |error| error,
            )
            .unwrap_err(),
            "provider sent invalid SSE JSON"
        );
        assert!(!chat.is_done());
        assert!(
            decoder
                .next_frame()
                .unwrap()
                .contains("must-not-be-accepted")
        );
    }

    #[test]
    fn queued_public_prefix_survives_a_later_framing_failure() {
        let mut chunk =
            frame(serde_json::json!({"id":"gen-framing","choices":[{"delta":{"content":"391"}}]}))
                .into_bytes();
        chunk.extend_from_slice(b"data: \xff\n\n");
        let mut decoder = Decoder::default();
        let mut chat = ChatStream::default();
        let mut pending = VecDeque::new();
        let mut failure = None;
        let mut reads = 0;
        let first = pull_events(
            &mut decoder,
            &mut chat,
            &mut pending,
            &mut failure,
            64,
            || {
                reads += 1;
                Ok::<_, &str>(Some(chunk.clone()))
            },
            |error| error,
        )
        .unwrap();
        assert_eq!(reads, 1);
        let first = decoded(&first);
        assert_eq!(first.len(), 2);
        assert!(first[0]["text"].as_str().unwrap().contains("gen-framing"));
        assert_eq!(first[1]["text"], "391");
        assert_eq!(
            pull_events(
                &mut decoder,
                &mut chat,
                &mut pending,
                &mut failure,
                64,
                || Err("unexpected read"),
                |error| error,
            )
            .unwrap_err(),
            "invalid SSE UTF-8"
        );
    }

    #[test]
    fn max_keeps_pending_events_and_done_stops_buffered_parsing() {
        let mut decoder = Decoder::default();
        decoder.feed([
            frame(serde_json::json!({"id":"gen-max","choices":[{"delta":{"content":"391"},"finish_reason":"stop"}],
                "usage":{"prompt_tokens":2,"completion_tokens":3}})),
            "data: [DONE]\n\n".into(),
            "data: invalid-after-done\n\n".into(),
        ].concat().as_bytes()).unwrap();
        let mut chat = ChatStream::default();
        let mut pending = VecDeque::new();
        let mut failure = None;
        let mut kinds = Vec::new();
        for _ in 0..6 {
            let events = pull_events(
                &mut decoder,
                &mut chat,
                &mut pending,
                &mut failure,
                1,
                || Err("unexpected read"),
                |error| error,
            )
            .unwrap();
            assert!(events.len() <= 1);
            kinds.extend(
                decoded(&events)
                    .iter()
                    .map(|event| event["kind"].as_str().unwrap().to_owned()),
            );
            if events.is_empty() {
                assert!(chat.is_done());
                break;
            }
        }
        assert_eq!(kinds, ["metadata", "delta", "metadata", "usage", "done"]);
        assert!(failure.is_none());
        assert_eq!(decoder.next_frame().unwrap(), "invalid-after-done");
    }

    #[test]
    fn buffered_public_work_stops_after_sixteen_frames() {
        let mut decoder = Decoder::default();
        decoder.feed(delta("x").repeat(20).as_bytes()).unwrap();
        let mut chat = ChatStream::default();
        let mut pending = VecDeque::new();
        let mut failure = None;
        for expected in [16, 4] {
            let events = pull_events(
                &mut decoder,
                &mut chat,
                &mut pending,
                &mut failure,
                64,
                || Err("unexpected read"),
                |error| error,
            )
            .unwrap();
            assert_eq!(events.len(), expected);
            assert!(
                decoded(&events)
                    .iter()
                    .all(|event| event["kind"] == "delta")
            );
        }
    }

    #[test]
    fn buffered_private_work_yields_a_heartbeat_after_sixteen_frames() {
        let mut decoder = Decoder::default();
        decoder
            .feed(
                frame(serde_json::json!({"choices":[{"delta":{"reasoning":"private-marker"}}]}))
                    .repeat(32)
                    .as_bytes(),
            )
            .unwrap();
        let mut chat = ChatStream::default();
        let mut pending = VecDeque::new();
        let mut failure = None;
        for _ in 0..2 {
            let events = pull_events(
                &mut decoder,
                &mut chat,
                &mut pending,
                &mut failure,
                64,
                || Err("unexpected read"),
                |error| error,
            )
            .unwrap();
            let values = decoded(&events);
            assert_eq!(values.len(), 1);
            assert_eq!(values[0]["kind"], "progress");
            let progress: serde_json::Value =
                serde_json::from_str(values[0]["text"].as_str().unwrap()).unwrap();
            assert_eq!(progress["processed_frames"], 16);
            assert_eq!(progress["received_bytes"], 0);
            assert!(!events[0].contains("private-marker"));
        }
    }

    #[test]
    fn missing_done_remains_an_error_after_delivering_a_public_prefix() {
        let mut decoder = Decoder::default();
        decoder.feed(delta("391").as_bytes()).unwrap();
        let mut chat = ChatStream::default();
        let mut pending = VecDeque::new();
        let mut failure = None;
        let first = pull_events(
            &mut decoder,
            &mut chat,
            &mut pending,
            &mut failure,
            64,
            || Err("unexpected read"),
            |error| error,
        )
        .unwrap();
        assert_eq!(decoded(&first)[0]["text"], "391");
        assert_eq!(
            pull_events(
                &mut decoder,
                &mut chat,
                &mut pending,
                &mut failure,
                64,
                || Ok::<_, &str>(None),
                |error| error,
            )
            .unwrap_err(),
            "provider ended before SSE [DONE]"
        );
        assert!(!chat.is_done());
    }

    #[test]
    fn invalid_first_frame_is_an_immediate_fixed_error() {
        let mut decoder = Decoder::default();
        decoder.feed(b"data: invalid-json\n\n").unwrap();
        assert_eq!(
            pull_events(
                &mut decoder,
                &mut ChatStream::default(),
                &mut VecDeque::new(),
                &mut None,
                64,
                || Err("unexpected read"),
                |error| error,
            )
            .unwrap_err(),
            "provider sent invalid SSE JSON"
        );
    }

    #[test]
    fn public_receipt_and_partial_are_returned_before_another_blocking_read() {
        let mut decoder = Decoder::default();
        let mut chat = ChatStream::default();
        let mut pending = VecDeque::from([chat.generation_header("gen-test").unwrap()]);
        let mut failure = None;
        let mut reads = 0;
        let first = pull_events(
            &mut decoder,
            &mut chat,
            &mut pending,
            &mut failure,
            64,
            || {
                reads += 1;
                Err("must not read")
            },
            |error| error,
        )
        .unwrap();
        assert_eq!(reads, 0);
        assert!(first[0].contains("gen-test"));
        decoder
            .feed(
                b"data: {\"id\":\"gen-test\",\"choices\":[{\"delta\":{\"content\":\"391\"}}]}\n\n",
            )
            .unwrap();
        let partial = pull_events(
            &mut decoder,
            &mut chat,
            &mut pending,
            &mut failure,
            64,
            || {
                reads += 1;
                Err("later read failed")
            },
            |error| error,
        )
        .unwrap();
        assert_eq!(reads, 0);
        assert!(partial[0].contains("391"));
        assert_eq!(
            pull_events(
                &mut decoder,
                &mut chat,
                &mut pending,
                &mut failure,
                64,
                || Err("later read failed"),
                |error| error
            )
            .unwrap_err(),
            "later read failed"
        );
    }

    #[test]
    fn private_only_or_fragmented_transport_yields_bounded_content_free_heartbeat() {
        for chunk in [
            b"data: {\"choices\":[{\"delta\":{\"reasoning\":\"private-marker\"}}]}\n\n".to_vec(),
            b"data: {\"choices\":".to_vec(),
        ] {
            let mut reads = 0;
            let events = pull_events(
                &mut Decoder::default(),
                &mut ChatStream::default(),
                &mut VecDeque::new(),
                &mut None,
                64,
                || {
                    reads += 1;
                    Ok::<_, &str>(Some(chunk.clone()))
                },
                |error| error,
            )
            .unwrap();
            assert_eq!(reads, 1);
            assert_eq!(events.len(), 1);
            assert_eq!(
                serde_json::from_str::<serde_json::Value>(&events[0]).unwrap()["kind"],
                "progress"
            );
            assert!(!events[0].contains("private-marker"));
        }
    }

    #[test]
    fn empty_batch_is_only_returned_after_verified_done() {
        let mut decoder = Decoder::default();
        let mut chat = ChatStream::default();
        let mut pending = VecDeque::new();
        let mut failure = None;
        decoder.feed(b"data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\"}]}\n\ndata: [DONE]\n\n").unwrap();
        let mut kinds = Vec::new();
        for _ in 0..3 {
            let events = pull_events(
                &mut decoder,
                &mut chat,
                &mut pending,
                &mut failure,
                64,
                || Err("unexpected read"),
                |error| error,
            )
            .unwrap();
            kinds.extend(events.iter().map(|event| {
                serde_json::from_str::<serde_json::Value>(event).unwrap()["kind"]
                    .as_str()
                    .unwrap()
                    .to_owned()
            }));
            if events.is_empty() {
                assert!(chat.is_done());
                break;
            }
        }
        assert_eq!(kinds, ["metadata", "done"]);
    }
}

export!(Pack);
