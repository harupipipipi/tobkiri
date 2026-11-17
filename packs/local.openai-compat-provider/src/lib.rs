//! OpenAI-compatible provider guest. All network and secret access is core-gated.

wit_bindgen::generate!({ path: "wit", world: "pack",generate_all });

use exports::local::harness_chat::llm_provider::{Guest as Provider, Prompt};
use exports::tobkiri::core::{lifecycle, stream_source};
use std::{cell::RefCell, collections::BTreeMap};
use tobkiri::core::{
    flow, network, secrets,
    types::{Error, ErrorCode, StreamHandle},
};
use tobkiri_guest_sdk::{done_event, sse::Decoder};

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct Settings {
    base_url: String,
    model: String,
    secret_name: Option<String>,
    timeout_ms: u32,
    #[serde(default, deserialize_with = "deserialize_temperature")]
    temperature: f64,
}

fn deserialize_temperature<'de, D>(deserializer: D) -> Result<f64, D::Error>
where
    D: serde::Deserializer<'de>,
{
    #[derive(serde::Deserialize)]
    #[serde(untagged)]
    enum Value {
        Number(f64),
        Decimal(String),
    }
    let value = match <Value as serde::Deserialize>::deserialize(deserializer)? {
        Value::Number(value) => value,
        Value::Decimal(value) => value.parse::<f64>().map_err(serde::de::Error::custom)?,
    };
    if !value.is_finite() || !(0.0..=2.0).contains(&value) {
        return Err(serde::de::Error::custom(
            "temperature must be finite and in 0..2",
        ));
    }
    Ok(value)
}

impl Settings {
    fn validate(&mut self) -> Result<(), Error> {
        self.base_url = normalize_base_url(&self.base_url)
            .map_err(|message| error(ErrorCode::Invalid, message))?;
        if self.model.trim().is_empty()
            || self.timeout_ms == 0
            || self.timeout_ms > 300_000
            || !self.temperature.is_finite()
            || !(0.0..=2.0).contains(&self.temperature)
        {
            return Err(error(
                ErrorCode::Invalid,
                "provider requires a model, bounded timeout and finite temperature in 0..2",
            ));
        }
        Ok(())
    }
}

struct ActiveStream {
    response: network::Response,
    decoder: Decoder,
    text: String,
    pending_usage: Option<String>,
    pending_failure: Option<PendingFailure>,
    done: bool,
}

// Share the notification-then-failure order for HTTP and SSE API errors.
// The next failing pull is cleaned up by the ordinary stream cancellation path.
struct PendingFailure {
    failure: Option<Error>,
    notified: bool,
}

impl PendingFailure {
    fn new(failure: Error) -> Self {
        Self {
            failure: Some(failure),
            notified: false,
        }
    }

    fn next(&mut self) -> Result<String, Error> {
        if self.notified {
            return Err(self.failure.take().unwrap_or_else(|| {
                error(ErrorCode::Invalid, "provider failure already consumed")
            }));
        }
        let failure = self
            .failure
            .as_ref()
            .ok_or_else(|| error(ErrorCode::Invalid, "provider failure missing"))?;
        self.notified = true;
        Ok(serde_json::json!({"kind": "error", "text": failure.message,
            "prompt-tokens": null, "completion-tokens": null})
        .to_string())
    }
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

fn normalize_base_url(value: &str) -> Result<String, &'static str> {
    const INVALID: &str =
        "provider requires an absolute HTTP base URL without credentials, query or fragment";
    // WHATWG parsers repair whitespace, backslashes and missing slashes. A
    // settings typo must not silently choose another network target.
    if !(value.starts_with("https://") || value.starts_with("http://"))
        || value.chars().any(|c| c.is_whitespace() || c.is_control())
        || value.contains(['\\', '?', '#'])
    {
        return Err(INVALID);
    }
    let authority = value
        .split_once("://")
        .ok_or(INVALID)?
        .1
        .split('/')
        .next()
        .ok_or(INVALID)?;
    if authority.is_empty() || authority.contains('@') {
        return Err(INVALID);
    }
    let mut url = url::Url::parse(value).map_err(|_| INVALID)?;
    if !matches!(url.scheme(), "http" | "https")
        || url.host_str().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
    {
        return Err(INVALID);
    }
    let origin = url.origin();
    let path = url.path().trim_end_matches('/');
    let path = if path.is_empty() {
        "/v1".to_owned()
    } else {
        path.to_owned()
    };
    url.set_path(&path);
    if url.origin() != origin {
        return Err(INVALID);
    }
    Ok(url.to_string())
}

// The WIT contract already bounds this value to u32. Keep the historical zero
// fallback, but leave the model's supported token budget to the API.
fn requested_max_tokens(value: u32) -> u32 {
    value.max(1)
}

fn request_body(config: &Settings, prompt: Prompt) -> Vec<u8> {
    let messages = prompt
        .messages
        .into_iter()
        .map(|message| serde_json::json!({"role":message.role,"content":message.content}))
        .collect::<Vec<_>>();
    serde_json::json!({"model":config.model,"messages":messages,"stream":true,
        "stream_options":{"include_usage":true},"max_tokens":requested_max_tokens(prompt.max_tokens),
        "temperature":config.temperature}).to_string().into_bytes()
}

const ERROR_BODY_LIMIT: usize = 4096;

// Display text only: never used for authorization or retries. Authentication
// failures omit upstream messages because those commonly echo the rejected key.
fn sanitize_error_text(text: &str) -> String {
    let text: String = text
        .chars()
        .take(1024)
        .map(|c| if c.is_control() { ' ' } else { c })
        .collect();
    let mut redact_next = false;
    let mut words = Vec::new();
    for word in text.split_whitespace() {
        let lower = word.to_ascii_lowercase();
        let sensitive = redact_next
            || lower.contains("sk-")
            || lower.contains("api_key=")
            || lower.contains("api-key=")
            || lower.contains("api_key:")
            || lower.contains("api-key:");
        redact_next = lower.trim_matches(|c: char| !c.is_ascii_alphanumeric()) == "bearer";
        words.push(if sensitive { "[redacted]" } else { word });
    }
    let joined = words.join(" ");
    if joined.chars().count() > 512 {
        format!("{}…", joined.chars().take(511).collect::<String>())
    } else {
        joined
    }
}

fn api_error_summary(value: &serde_json::Value, auth_failure: bool) -> String {
    let details = value.get("error").unwrap_or(value);
    let auth_codes = [
        "invalid_api_key",
        "authentication_error",
        "permission_denied",
        "permission_error",
        "insufficient_permissions",
    ];
    let auth_failure = auth_failure
        || ["code", "type"].iter().any(|field| {
            details
                .get(field)
                .and_then(|value| value.as_str())
                .is_some_and(|code| auth_codes.contains(&code))
        });
    if auth_failure {
        for field in ["code", "type"] {
            if let Some(code) = details.get(field).and_then(|value| value.as_str())
                && auth_codes.contains(&code)
            {
                return code.into();
            }
        }
        return "authentication or permission rejected".into();
    }
    let mut parts = Vec::new();
    for field in ["message", "type", "code", "param"] {
        if let Some(text) = details.get(field).and_then(|value| value.as_str()) {
            let text = sanitize_error_text(text);
            if !text.is_empty() {
                parts.push(format!("{field}: {text}"));
            }
        }
    }
    if parts.is_empty() {
        if let Some(text) = details.as_str() {
            return sanitize_error_text(text);
        }
        return "API error body received".into();
    }
    sanitize_error_text(&parts.join("; "))
}

fn http_error_message(status: u16, body: &[u8]) -> String {
    let auth_failure = matches!(status, 401 | 403);
    let summary = match serde_json::from_slice(body) {
        Ok(value) => api_error_summary(&value, auth_failure),
        Err(_) if auth_failure => "authentication or permission rejected".into(),
        Err(_) => sanitize_error_text(&String::from_utf8_lossy(body)),
    };
    format!(
        "provider HTTP {status}: {}",
        if summary.is_empty() {
            "empty error body"
        } else {
            &summary
        }
    )
}

fn read_error_body(
    mut cancelled: impl FnMut() -> bool,
    mut read: impl FnMut(u32) -> Result<Option<Vec<u8>>, Error>,
) -> Result<Vec<u8>, Error> {
    let mut body = Vec::new();
    while body.len() < ERROR_BODY_LIMIT {
        if cancelled() {
            return Err(error(ErrorCode::Cancelled, "run cancelled"));
        }
        match read((ERROR_BODY_LIMIT - body.len()) as u32) {
            Ok(Some(chunk)) if !chunk.is_empty() => {
                body.extend_from_slice(&chunk[..chunk.len().min(ERROR_BODY_LIMIT - body.len())]);
                if serde_json::from_slice::<serde_json::Value>(&body).is_ok() {
                    break;
                }
            }
            Ok(_) => break,
            Err(failure) if matches!(failure.code, ErrorCode::Cancelled) => return Err(failure),
            Err(_) => break,
        }
    }
    if cancelled() {
        return Err(error(ErrorCode::Cancelled, "run cancelled"));
    }
    Ok(body)
}

fn http_failure(response: &network::Response) -> Error {
    match read_error_body(flow::is_cancelled, |max| response.read(max)) {
        Ok(body) => error(
            ErrorCode::Unavailable,
            &http_error_message(response.status(), &body),
        ),
        Err(failure) => failure,
    }
}

impl lifecycle::Guest for Pack {
    fn init(config: String) -> Result<(), Error> {
        let mut settings: Settings = serde_json::from_str(&config)
            .map_err(|_| error(ErrorCode::Invalid, "provider settings are invalid"))?;
        settings.validate()?;
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
            let body = request_body(config, prompt);
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
            let pending_failure = if !(200..300).contains(&response.status()) {
                let failure = http_failure(&response);
                if matches!(failure.code, ErrorCode::Cancelled) {
                    return Err(failure);
                }
                Some(PendingFailure::new(failure))
            } else {
                None
            };
            if flow::is_cancelled() {
                return Err(error(ErrorCode::Cancelled, "run cancelled"));
            }
            let is_sse = pending_failure.is_some()
                || response.headers().iter().any(|(name, value)| {
                    name.eq_ignore_ascii_case("content-type")
                        && value.starts_with("text/event-stream")
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
                    text: String::new(),
                    pending_usage: None,
                    pending_failure,
                    done: false,
                },
            );
            Ok(StreamHandle { id })
        })
    }
}

impl stream_source::Guest for Pack {
    fn pull(local_id: u64, max: u32) -> Result<Vec<String>, Error> {
        if flow::is_cancelled() {
            Self::cancel(local_id);
            return Err(error(ErrorCode::Cancelled, "run cancelled"));
        }
        if max == 0 || max > 64 {
            return Err(error(ErrorCode::Invalid, "pull batch must be 1..64"));
        }
        let result = STATE.with_borrow_mut(|state| {
            let stream = state.streams.get_mut(&local_id).ok_or_else(|| error(ErrorCode::NotFound,"stream not found"))?;
            if stream.done { state.streams.remove(&local_id); return Ok(vec![]); }
            if let Some(failure) = &mut stream.pending_failure {
                return failure.next().map(|event| vec![event]);
            }
            let max = max as usize;
            let mut events = Vec::new();
            events.extend(stream.pending_usage.take());
            while events.len() < max {
                if flow::is_cancelled() { state.streams.remove(&local_id); return Err(error(ErrorCode::Cancelled,"run cancelled")); }
                if let Some(frame) = stream.decoder.next_frame() {
                    if frame == "[DONE]" {
                        stream.done = true;
                        events.push(done_event(&stream.text));
                        break;
                    }
                    let value: serde_json::Value = serde_json::from_str(&frame).map_err(|_| error(ErrorCode::Invalid,"provider sent invalid SSE JSON"))?;
                    if value.get("error").is_some() {
                        let mut failure = PendingFailure::new(error(ErrorCode::Unavailable, &format!("provider stream failed: {}", api_error_summary(&value, false))));
                        events.push(failure.next()?);
                        stream.pending_failure = Some(failure);
                        break;
                    }
                    if let Some(text) = value.pointer("/choices/0/delta/content").and_then(|value|value.as_str()) {
                        if stream.text.len().saturating_add(text.len()) > 4 * 1024 * 1024 { return Err(error(ErrorCode::LimitExceeded,"generated text exceeds four MiB")); }
                        if !text.is_empty() { stream.text.push_str(text); events.push(tobkiri_guest_sdk::delta_event(text)); }
                    }
                    if let Some(usage) = value.get("usage").filter(|usage|usage.is_object()) {
                        let event = serde_json::json!({
                            "kind": "usage", "text": null,
                            "prompt-tokens": usage.get("prompt_tokens").and_then(|value| value.as_u64()).unwrap_or(0),
                            "completion-tokens": usage.get("completion_tokens").and_then(|value| value.as_u64()).unwrap_or(0),
                        })
                        .to_string();
                        if events.len() == max {
                            stream.pending_usage = Some(event);
                        } else {
                            events.push(event);
                        }
                    }
                    continue;
                }
                // Publish decoded events before waiting for more network data.
                if !events.is_empty() {
                    break;
                }
                match stream.response.read(8192)? {
                    Some(chunk) => stream.decoder.feed(&chunk).map_err(|message|error(ErrorCode::Invalid,message))?,
                    None => { stream.decoder.finish().map_err(|message|error(ErrorCode::Invalid,message))?;
                        return Err(error(ErrorCode::Invalid,"provider ended before SSE [DONE]")); }
                }
            }
            Ok(events)
        });
        let result = if flow::is_cancelled() {
            Err(error(ErrorCode::Cancelled, "run cancelled"))
        } else {
            result
        };
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

#[cfg(test)]
mod tests {
    use super::*;

    fn settings() -> Settings {
        serde_json::from_str(include_str!("../config.json")).unwrap()
    }

    #[test]
    fn approved_endpoint_prefixes_preserve_the_origin() {
        for (input, expected) in [
            ("https://example.test", "https://example.test/v1"),
            ("https://example.test/", "https://example.test/v1"),
            ("http://127.0.0.1:18080/v1", "http://127.0.0.1:18080/v1"),
            (
                "https://example.test/custom/api/",
                "https://example.test/custom/api",
            ),
            ("http://[::1]:8080/proxy/v2", "http://[::1]:8080/proxy/v2"),
        ] {
            let normalized = normalize_base_url(input).unwrap();
            assert_eq!(normalized, expected);
            assert_eq!(
                url::Url::parse(input).unwrap().origin(),
                url::Url::parse(&normalized).unwrap().origin()
            );
        }
    }

    #[test]
    fn malformed_or_credential_endpoints_are_rejected() {
        for input in [
            "https:example.test",
            "https:///example.test",
            "https://",
            "//example.test/v1",
            "file:///v1",
            "https://user:secret@example.test/v1",
            "https://@example.test/v1",
            "https://example.test/v1?key=secret",
            "https://example.test/v1#fragment",
            " https://example.test/v1",
            "https://example.test/\n",
            "https://example.test:99999/v1",
            "https://example.test\\other.test/v1",
        ] {
            assert!(normalize_base_url(input).is_err(), "accepted {input:?}");
        }
    }

    #[test]
    fn existing_settings_and_requested_token_budgets_remain_supported() {
        let mut config = settings();
        config.validate().unwrap();
        assert_eq!(config.temperature, 0.0);
        for max_tokens in [0, 128, 512, 4096, u32::MAX] {
            let body: serde_json::Value = serde_json::from_slice(&request_body(
                &config,
                Prompt {
                    model: config.model.clone(),
                    messages: vec![],
                    max_tokens,
                },
            ))
            .unwrap();
            assert_eq!(body["max_tokens"], max_tokens.max(1));
            assert_eq!(body["temperature"], 0.0);
        }
        for (value, expected) in [
            (serde_json::json!("0.4"), 0.4),
            (serde_json::json!(1), 1.0),
            (serde_json::json!(0.25), 0.25),
            (serde_json::json!("2"), 2.0),
        ] {
            let mut document: serde_json::Value =
                serde_json::from_str(include_str!("../config.json")).unwrap();
            document["temperature"] = value;
            let mut parsed: Settings = serde_json::from_value(document).unwrap();
            parsed.validate().unwrap();
            let body: serde_json::Value = serde_json::from_slice(&request_body(
                &parsed,
                Prompt {
                    model: parsed.model.clone(),
                    messages: vec![],
                    max_tokens: 2048,
                },
            ))
            .unwrap();
            assert!(body["temperature"].is_number());
            assert_eq!(body["temperature"].as_f64(), Some(expected));
        }
        for value in [
            serde_json::json!("bad"),
            serde_json::json!("NaN"),
            serde_json::json!("inf"),
            serde_json::json!("-0.1"),
            serde_json::json!("2.1"),
            serde_json::Value::Null,
        ] {
            let mut document: serde_json::Value =
                serde_json::from_str(include_str!("../config.json")).unwrap();
            document["temperature"] = value;
            assert!(serde_json::from_value::<Settings>(document).is_err());
        }
        config.temperature = 1.25;
        config.validate().unwrap();
        let body: serde_json::Value = serde_json::from_slice(&request_body(
            &config,
            Prompt {
                model: config.model.clone(),
                messages: vec![],
                max_tokens: 2048,
            },
        ))
        .unwrap();
        assert_eq!(body["temperature"], 1.25);
        for temperature in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY, -0.1, 2.01] {
            config.temperature = temperature;
            assert!(config.validate().is_err());
        }
    }

    #[test]
    fn http_errors_preserve_model_details_and_omit_rejected_keys() {
        let message = http_error_message(400, br#"{"error":{"message":"The model 'missing-model' does not exist","type":"invalid_request_error","code":"model_not_found","param":"model"}}"#);
        assert!(message.contains("HTTP 400"));
        assert!(message.contains("missing-model"));
        assert!(message.contains("model_not_found"));
        let message = http_error_message(401, br#"{"error":{"message":"Incorrect API key provided: arbitrary-secret-value","type":"invalid_request_error","code":"invalid_api_key"}}"#);
        assert!(message.contains("HTTP 401"));
        assert!(message.contains("invalid_api_key"));
        assert!(!message.contains("arbitrary-secret-value"));
        let stream_value = serde_json::json!({"error": {"code": "invalid_api_key", "message": "arbitrary-secret-value"}});
        assert!(!api_error_summary(&stream_value, false).contains("arbitrary-secret-value"));
        let message = http_error_message(403, b"Permission denied for key arbitrary-secret-value");
        assert!(!message.contains("arbitrary-secret-value"));
        // Both HTTP failures and SSE error frames use the same one-notification
        // state, then preserve their original failure classification/message.
        for message in [
            http_error_message(400, br#"{"error":{"message":"unknown model"}}"#),
            format!(
                "provider stream failed: {}",
                api_error_summary(
                    &serde_json::json!({"error": {"message": "upstream overloaded"}}),
                    false
                )
            ),
        ] {
            let mut pending = PendingFailure::new(error(ErrorCode::Unavailable, &message));
            let notification: serde_json::Value =
                serde_json::from_str(&pending.next().unwrap()).unwrap();
            assert_eq!(notification["kind"], "error");
            assert_eq!(notification["text"], message);
            assert!(notification["prompt-tokens"].is_null());
            assert!(notification["completion-tokens"].is_null());
            let failure = pending.next().unwrap_err();
            assert!(matches!(failure.code, ErrorCode::Unavailable));
            assert_eq!(failure.message, message);
            assert!(pending.failure.is_none());
        }
    }

    #[test]
    fn error_reads_are_bounded_and_non_json_diagnostics_are_sanitized() {
        let mut reads = 0;
        let body = read_error_body(
            || false,
            |max| {
                reads += 1;
                assert_eq!(max, ERROR_BODY_LIMIT as u32);
                Ok(Some(vec![b'x'; ERROR_BODY_LIMIT * 2]))
            },
        )
        .unwrap();
        assert_eq!(reads, 1);
        assert_eq!(body.len(), ERROR_BODY_LIMIT);
        let mut complete_reads = 0;
        let body = read_error_body(
            || false,
            |_| {
                complete_reads += 1;
                assert_eq!(complete_reads, 1, "waited after complete JSON error");
                Ok(Some(br#"{"error":{"message":"invalid model"}}"#.to_vec()))
            },
        )
        .unwrap();
        assert!(http_error_message(400, &body).contains("invalid model"));
        let body = vec![b'x'; ERROR_BODY_LIMIT];
        assert!(http_error_message(500, &body).len() < 600);
        let message = http_error_message(
            400,
            b"bad upstream response\nBearer opaque-secret sk-example api_key=hidden",
        );
        assert!(message.contains("bad upstream response"));
        for key in ["opaque-secret", "sk-example", "hidden", "\n"] {
            assert!(!message.contains(key));
        }
        assert!(http_error_message(502, b"").contains("empty error body"));
    }

    #[test]
    fn error_reads_preserve_cancellation_before_and_during_read() {
        let result = read_error_body(|| true, |_| panic!("cancelled call read network"));
        assert!(matches!(result.unwrap_err().code, ErrorCode::Cancelled));
        let result = read_error_body(
            || false,
            |_| Err(error(ErrorCode::Cancelled, "network cancelled")),
        );
        assert!(matches!(result.unwrap_err().code, ErrorCode::Cancelled));
        let calls = std::cell::Cell::new(0);
        let result = read_error_body(
            || calls.get() > 0,
            |_| {
                calls.set(calls.get() + 1);
                Ok(Some(b"partial".to_vec()))
            },
        );
        assert!(matches!(result.unwrap_err().code, ErrorCode::Cancelled));
    }
}
