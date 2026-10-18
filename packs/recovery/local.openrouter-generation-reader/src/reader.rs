//! Pure, bounded normalization. Unknown/private fields are deserialized with
//! Serde's ignored-field path and are never included in the observation.

use serde::{Deserialize, Serialize};
use serde_json::value::RawValue;

#[path = "catalog.rs"]
pub mod catalog;

pub const MAX_OUTPUT_BYTES: usize = 4 * 1024 * 1024;
const MAX_METADATA_BYTES: usize = 64 * 1024;
const MAX_CONTENT_BYTES: usize = 4 * 1024 * 1024;
const BASE: &str = "https://openrouter.ai/api/v1/generation";

pub struct Selection {
    generation_id: String,
    expected_model: String,
    expected_provider: Option<String>,
    include_public_content: bool,
}

impl Selection {
    pub fn new(
        generation_id: String,
        expected_model: String,
        expected_provider: Option<String>,
        include_public_content: bool,
    ) -> Result<Self, ()> {
        if generation_id.len() <= 4
            || generation_id.len() > 128
            || !generation_id.starts_with("gen-")
            || !generation_id
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'-')
            || !identity_text(&expected_model)
            || expected_provider
                .as_deref()
                .is_some_and(|text| !identity_text(text))
        {
            return Err(());
        }
        Ok(Self {
            generation_id,
            expected_model,
            expected_provider,
            include_public_content,
        })
    }
}

fn identity_text(text: &str) -> bool {
    !text.is_empty() && text.len() <= 256 && !text.chars().any(char::is_control)
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Endpoint {
    Metadata,
    PublicContent,
    ModelCatalog,
}
impl Endpoint {
    pub fn maximum_bytes(self) -> usize {
        match self {
            Self::Metadata => MAX_METADATA_BYTES,
            Self::PublicContent => MAX_CONTENT_BYTES,
            Self::ModelCatalog => MAX_METADATA_BYTES,
        }
    }
    fn url(self, id: &str) -> String {
        let mut encoded = String::with_capacity(id.len());
        for byte in id.bytes() {
            if byte.is_ascii_alphanumeric() || b"-._~".contains(&byte) {
                encoded.push(byte as char);
            } else {
                use std::fmt::Write;
                write!(encoded, "%{byte:02X}").expect("writing to String");
            }
        }
        format!(
            "{BASE}{}?id={encoded}",
            if self == Self::PublicContent {
                "/content"
            } else {
                ""
            }
        )
    }
}

pub struct HttpPayload {
    pub status: u16,
    pub body: Vec<u8>,
    pub oversized: bool,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Availability {
    Available,
    NotRequested,
    MetadataUnavailable,
    NotFound,
    Forbidden,
    Unauthorized,
    RateLimited,
    HttpFailure,
    InvalidResponse,
    ResponseTooLarge,
    IdentityMismatch,
    IdentityUnconfirmed,
    NoStoredOutput,
    ProviderError,
}

fn http_state(status: u16) -> Availability {
    match status {
        200 => Availability::Available,
        401 => Availability::Unauthorized,
        403 => Availability::Forbidden,
        404 => Availability::NotFound,
        429 => Availability::RateLimited,
        _ => Availability::HttpFailure,
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum FinishReason {
    Stop,
    Length,
    ContentFilter,
    ToolCalls,
    Error,
    Cancelled,
    Other,
}
fn finish_reason(text: &str) -> FinishReason {
    match text {
        "stop" => FinishReason::Stop,
        "length" => FinishReason::Length,
        "content_filter" => FinishReason::ContentFilter,
        "tool_calls" => FinishReason::ToolCalls,
        "error" => FinishReason::Error,
        "cancelled" => FinishReason::Cancelled,
        _ => FinishReason::Other,
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum TerminalState {
    Unknown,
    CancellationReported,
    NormalStopReported,
    IncompleteFinishReported,
}

#[derive(Serialize)]
pub struct TerminalReceipt {
    pub state: TerminalState,
    pub finish_reason: Option<FinishReason>,
    pub native_finish_reason: Option<FinishReason>,
    pub cancelled: Option<bool>,
}
impl Default for TerminalReceipt {
    fn default() -> Self {
        Self {
            state: TerminalState::Unknown,
            finish_reason: None,
            native_finish_reason: None,
            cancelled: None,
        }
    }
}

#[derive(Default, Serialize)]
pub struct UsageReceipt {
    pub native_input_tokens: Option<u64>,
    pub native_output_tokens: Option<u64>,
    pub native_total_tokens: Option<u64>,
    pub native_cached_input_tokens: Option<u64>,
    pub native_reasoning_tokens: Option<u64>,
    pub normalized_input_tokens: Option<u64>,
    pub normalized_output_tokens: Option<u64>,
    pub cost_usd: Option<String>,
    pub native_tokens_complete: bool,
    pub cost_known: bool,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum IdentityMismatchReason {
    GenerationId,
    Model,
    Provider,
    Multiple,
}

/// Diagnostic evidence only. A rejected identity never authorizes accounting,
/// public-content retrieval, alias acceptance or a workflow state transition.
#[derive(Serialize)]
pub struct IdentityDiagnostics {
    pub mismatch_reason: Option<IdentityMismatchReason>,
    pub generation_id_matches: bool,
    pub model_matches: bool,
    // None means no comparison: expected or observed provider was absent.
    pub provider_matches: Option<bool>,
    pub observed_model: Option<String>,
    pub observed_provider: Option<String>,
}

#[derive(Serialize)]
pub struct MetadataReceipt {
    pub availability: Availability,
    pub http_status: u16,
    pub model: Option<String>,
    pub provider: Option<String>,
    pub identity_diagnostics: Option<IdentityDiagnostics>,
    // Provider-reported diagnostic numbers, never a local causal attribution.
    pub reported_latency: Option<String>,
    pub reported_generation_time: Option<String>,
    pub terminal: TerminalReceipt,
    pub observed_usage: UsageReceipt,
}
impl MetadataReceipt {
    fn unavailable(availability: Availability, http_status: u16) -> Self {
        Self {
            availability,
            http_status,
            model: None,
            provider: None,
            identity_diagnostics: None,
            reported_latency: None,
            reported_generation_time: None,
            terminal: TerminalReceipt::default(),
            observed_usage: UsageReceipt::default(),
        }
    }
}

#[derive(Serialize)]
pub struct ContentReceipt {
    pub availability: Availability,
    pub http_status: Option<u16>,
    // Availability is not a claim that a Goal phase or generation succeeded.
    pub public_completion: Option<String>,
}
impl ContentReceipt {
    fn unavailable(availability: Availability, status: Option<u16>) -> Self {
        Self {
            availability,
            http_status: status,
            public_completion: None,
        }
    }
}

#[derive(Serialize)]
pub struct Observation {
    version: &'static str,
    generation_id: String,
    pub metadata: MetadataReceipt,
    pub content: ContentReceipt,
}

/// One metadata GET and, only on explicit opt-in plus matched metadata, one
/// content GET. No retry, POST, sidecar write or eligibility transition exists.
pub fn lookup_with<E>(
    selection: &Selection,
    mut fetch: impl FnMut(Endpoint, &str) -> Result<HttpPayload, E>,
) -> Result<Observation, E> {
    let payload = fetch(
        Endpoint::Metadata,
        &Endpoint::Metadata.url(&selection.generation_id),
    )?;
    let metadata = normalize_metadata(selection, payload);
    let content = if !selection.include_public_content {
        ContentReceipt::unavailable(Availability::NotRequested, None)
    } else if metadata.availability != Availability::Available {
        ContentReceipt::unavailable(Availability::MetadataUnavailable, None)
    } else {
        let payload = fetch(
            Endpoint::PublicContent,
            &Endpoint::PublicContent.url(&selection.generation_id),
        )?;
        normalize_content(payload)
    };
    Ok(Observation {
        version: "tobkiri.generation-observation.v1",
        generation_id: selection.generation_id.clone(),
        metadata,
        content,
    })
}

#[derive(Deserialize)]
struct MetadataEnvelope {
    data: MetadataData,
}
#[derive(Deserialize)]
struct MetadataData {
    id: String,
    model: String,
    provider_name: Option<String>,
    finish_reason: Option<String>,
    native_finish_reason: Option<String>,
    cancelled: Option<bool>,
    latency: Option<Box<RawValue>>,
    generation_time: Option<Box<RawValue>>,
    native_tokens_prompt: Option<u64>,
    native_tokens_completion: Option<u64>,
    native_tokens_cached: Option<u64>,
    native_tokens_reasoning: Option<u64>,
    tokens_prompt: Option<u64>,
    tokens_completion: Option<u64>,
    total_cost: Option<Box<RawValue>>,
}

fn identity_diagnostics(selection: &Selection, data: &MetadataData) -> IdentityDiagnostics {
    let generation_id_matches = data.id == selection.generation_id;
    let model_matches = data.model == selection.expected_model;
    let provider_matches = selection
        .expected_provider
        .as_ref()
        .zip(data.provider_name.as_ref())
        .map(|(expected, observed)| expected == observed);
    let mismatch_reason = match (
        !generation_id_matches,
        !model_matches,
        provider_matches == Some(false),
    ) {
        (false, false, false) => None,
        (true, false, false) => Some(IdentityMismatchReason::GenerationId),
        (false, true, false) => Some(IdentityMismatchReason::Model),
        (false, false, true) => Some(IdentityMismatchReason::Provider),
        _ => Some(IdentityMismatchReason::Multiple),
    };
    IdentityDiagnostics {
        mismatch_reason,
        generation_id_matches,
        model_matches,
        provider_matches,
        observed_model: identity_text(&data.model).then(|| data.model.clone()),
        observed_provider: data
            .provider_name
            .as_ref()
            .filter(|value| identity_text(value))
            .cloned(),
    }
}

fn normalize_metadata(selection: &Selection, payload: HttpPayload) -> MetadataReceipt {
    let rejected = |state| MetadataReceipt::unavailable(state, payload.status);
    if payload.status != 200 {
        return rejected(http_state(payload.status));
    }
    if payload.oversized || payload.body.len() > MAX_METADATA_BYTES {
        return rejected(Availability::ResponseTooLarge);
    }
    let Ok(envelope) = serde_json::from_slice::<MetadataEnvelope>(&payload.body) else {
        return rejected(Availability::InvalidResponse);
    };
    let data = envelope.data;
    let identity = identity_diagnostics(selection, &data);
    if identity.mismatch_reason.is_some() {
        let mut receipt = rejected(Availability::IdentityMismatch);
        receipt.identity_diagnostics = Some(identity);
        return receipt;
    }
    if !identity_text(&data.model)
        || data
            .provider_name
            .as_deref()
            .is_some_and(|p| !identity_text(p))
    {
        return rejected(Availability::InvalidResponse);
    }
    if selection.expected_provider.is_some() && data.provider_name.is_none() {
        let mut receipt = rejected(Availability::IdentityUnconfirmed);
        receipt.identity_diagnostics = Some(identity);
        return receipt;
    }
    let cost = match data.total_cost {
        Some(raw) => match decimal(raw.get()) {
            Some(cost) => Some(cost),
            None => return rejected(Availability::InvalidResponse),
        },
        None => None,
    };
    let total = match data.native_tokens_prompt.zip(data.native_tokens_completion) {
        Some((input, output)) => match input.checked_add(output) {
            Some(total) => Some(total),
            None => return rejected(Availability::InvalidResponse),
        },
        None => None,
    };
    if data
        .native_tokens_cached
        .zip(data.native_tokens_prompt)
        .is_some_and(|(cached, input)| cached > input)
        || data
            .native_tokens_reasoning
            .zip(data.native_tokens_completion)
            .is_some_and(|(reasoning, output)| reasoning > output)
    {
        return rejected(Availability::InvalidResponse);
    }
    let finish = data.finish_reason.as_deref().map(finish_reason);
    let terminal = match (finish, data.cancelled) {
        (Some(FinishReason::Stop), Some(false)) => TerminalState::NormalStopReported,
        (
            Some(
                FinishReason::Length
                | FinishReason::ContentFilter
                | FinishReason::ToolCalls
                | FinishReason::Error
                | FinishReason::Cancelled,
            ),
            _,
        ) => TerminalState::IncompleteFinishReported,
        (_, Some(true)) => TerminalState::CancellationReported,
        _ => TerminalState::Unknown,
    };
    MetadataReceipt {
        availability: Availability::Available,
        http_status: payload.status,
        model: Some(data.model),
        provider: data.provider_name,
        identity_diagnostics: Some(identity),
        reported_latency: data.latency.as_ref().and_then(|value| decimal(value.get())),
        reported_generation_time: data
            .generation_time
            .as_ref()
            .and_then(|value| decimal(value.get())),
        terminal: TerminalReceipt {
            state: terminal,
            finish_reason: finish,
            native_finish_reason: data.native_finish_reason.as_deref().map(finish_reason),
            cancelled: data.cancelled,
        },
        observed_usage: UsageReceipt {
            native_input_tokens: data.native_tokens_prompt,
            native_output_tokens: data.native_tokens_completion,
            native_total_tokens: total,
            native_cached_input_tokens: data.native_tokens_cached,
            native_reasoning_tokens: data.native_tokens_reasoning,
            normalized_input_tokens: data.tokens_prompt,
            normalized_output_tokens: data.tokens_completion,
            cost_known: cost.is_some(),
            cost_usd: cost,
            native_tokens_complete: total.is_some(),
        },
    }
}

#[derive(Deserialize)]
struct ContentEnvelope {
    data: ContentData,
}
#[derive(Deserialize)]
struct ContentData {
    error: Option<serde::de::IgnoredAny>,
    output: Option<ContentOutput>,
}
#[derive(Deserialize)]
struct ContentOutput {
    completion: Option<String>,
}

fn normalize_content(payload: HttpPayload) -> ContentReceipt {
    let rejected = |state| ContentReceipt::unavailable(state, Some(payload.status));
    if payload.status != 200 {
        return rejected(http_state(payload.status));
    }
    if payload.oversized || payload.body.len() > MAX_CONTENT_BYTES {
        return rejected(Availability::ResponseTooLarge);
    }
    let Ok(envelope) = serde_json::from_slice::<ContentEnvelope>(&payload.body) else {
        return rejected(Availability::InvalidResponse);
    };
    if envelope.data.error.is_some() {
        return rejected(Availability::ProviderError);
    }
    match envelope.data.output.and_then(|output| output.completion) {
        Some(text) => ContentReceipt {
            availability: Availability::Available,
            http_status: Some(200),
            public_completion: Some(text),
        },
        None => rejected(Availability::NoStoredOutput),
    }
}

/// Convert a bounded nonnegative JSON number lexeme without f64 rounding.
/// Scientific notation is expanded exactly; unusually large exponents fail.
fn decimal(raw: &str) -> Option<String> {
    if raw.is_empty() || raw.len() > 128 || raw.starts_with('-') {
        return None;
    }
    let split = raw.find(['e', 'E']);
    let (mantissa, exponent) = match split {
        Some(index) => (&raw[..index], raw[index + 1..].parse::<i32>().ok()?),
        None => (raw, 0),
    };
    if !(-128..=128).contains(&exponent) {
        return None;
    }
    let (integer, fraction) = mantissa.split_once('.').unwrap_or((mantissa, ""));
    if integer.is_empty()
        || !integer.bytes().all(|b| b.is_ascii_digit())
        || (integer.len() > 1 && integer.starts_with('0'))
        || !fraction.bytes().all(|b| b.is_ascii_digit())
        || (mantissa.contains('.') && fraction.is_empty())
    {
        return None;
    }
    let digits = format!("{integer}{fraction}");
    let position = i32::try_from(integer.len()).ok()?.checked_add(exponent)?;
    let expanded = if position <= 0 {
        format!("0.{}{digits}", "0".repeat(usize::try_from(-position).ok()?))
    } else if usize::try_from(position).ok()? >= digits.len() {
        format!(
            "{digits}{}",
            "0".repeat(usize::try_from(position).ok()? - digits.len())
        )
    } else {
        let index = usize::try_from(position).ok()?;
        format!("{}.{}", &digits[..index], &digits[index..])
    };
    let (integer, fraction) = expanded.split_once('.').unwrap_or((&expanded, ""));
    let integer = integer.trim_start_matches('0');
    let integer = if integer.is_empty() { "0" } else { integer };
    let fraction = fraction.trim_end_matches('0');
    let result = if fraction.is_empty() {
        integer.into()
    } else {
        format!("{integer}.{fraction}")
    };
    if result.len() > 256 {
        None
    } else {
        Some(result)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn selection(content: bool) -> Selection {
        Selection::new(
            "gen-123-AbCd".into(),
            "xiaomi/mimo-v2.6-flash".into(),
            Some("Darkbloom".into()),
            content,
        )
        .unwrap()
    }
    fn metadata() -> serde_json::Value {
        json!({"data":{"id":"gen-123-AbCd","model":"xiaomi/mimo-v2.6-flash","provider_name":"Darkbloom","finish_reason":"stop","cancelled":false,"native_tokens_prompt":20,"native_tokens_completion":7,"native_tokens_cached":5,"native_tokens_reasoning":3,"tokens_prompt":25,"tokens_completion":10,"total_cost":0.001}})
    }
    fn payload(value: serde_json::Value) -> HttpPayload {
        HttpPayload {
            status: 200,
            body: serde_json::to_vec(&value).unwrap(),
            oversized: false,
        }
    }
    fn normalize(value: serde_json::Value) -> MetadataReceipt {
        normalize_metadata(&selection(false), payload(value))
    }

    #[test]
    fn metadata_uses_native_counts_and_keeps_terminal_separate() {
        let got = normalize(metadata());
        assert_eq!(got.availability, Availability::Available);
        assert_eq!(got.terminal.state, TerminalState::NormalStopReported);
        assert_eq!(got.observed_usage.native_total_tokens, Some(27));
        assert_eq!(got.observed_usage.normalized_input_tokens, Some(25));
        assert_eq!(got.observed_usage.cost_usd.as_deref(), Some("0.001"));
        let mut value = metadata();
        value["data"]["finish_reason"] = json!(null);
        assert_eq!(normalize(value).terminal.state, TerminalState::Unknown);
    }

    #[test]
    fn optional_fractional_timing_does_not_control_terminal_or_accounting() {
        let mut value = metadata();
        value["data"]["latency"] = json!(1.25);
        value["data"]["generation_time"] = json!("PRIVATE-DIAGNOSTIC");
        let got = normalize(value);
        assert_eq!(got.availability, Availability::Available);
        assert_eq!(got.reported_latency.as_deref(), Some("1.25"));
        assert_eq!(got.reported_generation_time, None);
        assert_eq!(got.terminal.state, TerminalState::NormalStopReported);
        assert!(got.observed_usage.native_tokens_complete);
        assert!(got.observed_usage.cost_known);
        assert!(
            !serde_json::to_string(&got)
                .unwrap()
                .contains("PRIVATE-DIAGNOSTIC")
        );
    }

    #[test]
    fn missing_usage_is_unknown_and_normalized_counts_are_not_a_native_fallback() {
        let mut value = metadata();
        for key in [
            "native_tokens_prompt",
            "native_tokens_completion",
            "native_tokens_cached",
            "native_tokens_reasoning",
            "total_cost",
        ] {
            value["data"].as_object_mut().unwrap().remove(key);
        }
        let got = normalize(value);
        assert_eq!(got.availability, Availability::Available);
        assert!(!got.observed_usage.native_tokens_complete);
        assert!(!got.observed_usage.cost_known);
        assert_eq!(got.observed_usage.native_input_tokens, None);
        assert_eq!(got.observed_usage.normalized_input_tokens, Some(25));
    }

    #[test]
    fn exact_decimal_preserves_small_charges_and_zero() {
        for (raw, expected) in [
            ("0", "0"),
            ("0.000317027200", "0.0003170272"),
            ("1.234567890123456789e-9", "0.000000001234567890123456789"),
            ("1e2", "100"),
            ("0.001e+1", "0.01"),
        ] {
            assert_eq!(decimal(raw).as_deref(), Some(expected));
        }
        for raw in [
            "-1", "-0", "null", "\"0.1\"", "1e999999", "NaN", "01", "1.", "1e", "",
        ] {
            assert_eq!(decimal(raw), None);
        }
        let raw = br#"{"data":{"id":"gen-123-AbCd","model":"xiaomi/mimo-v2.6-flash","provider_name":"Darkbloom","total_cost":1.234567890123456789e-9}}"#;
        let got = normalize_metadata(
            &selection(false),
            HttpPayload {
                status: 200,
                body: raw.to_vec(),
                oversized: false,
            },
        );
        assert_eq!(
            got.observed_usage.cost_usd.as_deref(),
            Some("0.000000001234567890123456789")
        );
    }

    #[test]
    fn identity_mismatch_never_returns_usage_or_content() {
        for (key, reason) in [
            ("id", IdentityMismatchReason::GenerationId),
            ("model", IdentityMismatchReason::Model),
            ("provider_name", IdentityMismatchReason::Provider),
        ] {
            let mut value = metadata();
            value["data"][key] = json!("wrong");
            let got = normalize(value);
            assert_eq!(got.availability, Availability::IdentityMismatch);
            assert_eq!(got.observed_usage.cost_usd, None);
            assert_eq!(got.observed_usage.native_input_tokens, None);
            assert_eq!(got.observed_usage.native_output_tokens, None);
            assert_eq!(got.terminal.state, TerminalState::Unknown);
            assert_eq!(got.model, None);
            assert_eq!(got.provider, None);
            let diagnostic = got.identity_diagnostics.unwrap();
            assert_eq!(diagnostic.mismatch_reason, Some(reason));
            assert_eq!(diagnostic.generation_id_matches, key != "id");
            assert_eq!(diagnostic.model_matches, key != "model");
            assert_eq!(diagnostic.provider_matches, Some(key != "provider_name"));
        }
        let mut value = metadata();
        value["data"]["provider_name"] = json!(null);
        let got = normalize(value);
        assert_eq!(got.availability, Availability::IdentityUnconfirmed);
        let diagnostic = got.identity_diagnostics.unwrap();
        assert_eq!(diagnostic.mismatch_reason, None);
        assert_eq!(diagnostic.provider_matches, None);
        assert_eq!(diagnostic.observed_provider, None);
    }

    #[test]
    fn identity_diagnostics_distinguish_multiple_and_unchecked_provider() {
        let mut value = metadata();
        value["data"]["id"] = json!("gen-different");
        value["data"]["model"] = json!("different/model");
        value["data"]["provider_name"] = json!("DifferentProvider");
        let got = normalize(value.clone());
        let diagnostic = got.identity_diagnostics.as_ref().unwrap();
        assert_eq!(
            diagnostic.mismatch_reason,
            Some(IdentityMismatchReason::Multiple)
        );
        assert!(!diagnostic.generation_id_matches);
        assert!(!diagnostic.model_matches);
        assert_eq!(diagnostic.provider_matches, Some(false));
        assert_eq!(
            diagnostic.observed_model.as_deref(),
            Some("different/model")
        );
        assert_eq!(
            diagnostic.observed_provider.as_deref(),
            Some("DifferentProvider")
        );
        assert!(
            !serde_json::to_string(&got)
                .unwrap()
                .contains("gen-different")
        );

        let mut unchecked = selection(false);
        unchecked.expected_provider = None;
        value["data"]["id"] = json!(unchecked.generation_id);
        let got = normalize_metadata(&unchecked, payload(value));
        assert_eq!(got.availability, Availability::IdentityMismatch);
        assert_eq!(got.observed_usage.cost_usd, None);
        let diagnostic = got.identity_diagnostics.unwrap();
        assert_eq!(
            diagnostic.mismatch_reason,
            Some(IdentityMismatchReason::Model)
        );
        assert!(diagnostic.generation_id_matches);
        assert!(!diagnostic.model_matches);
        assert_eq!(diagnostic.provider_matches, None);
    }

    #[test]
    fn identity_diagnostics_bound_public_names_and_exclude_private_fields() {
        for field in ["model", "provider_name"] {
            for invalid in [String::new(), "x".repeat(257), "line\nSECRET-MARKER".into()] {
                let mut value = metadata();
                value["data"][field] = json!(invalid);
                value["data"]["reasoning"] = json!("PRIVATE-REASONING");
                value["data"]["input"] = json!({"messages":"PRIVATE-INPUT"});
                value["data"]["headers"] = json!({"authorization":"PRIVATE-HEADER"});
                value["data"]["error"] = json!("PRIVATE-ERROR");
                let got = normalize(value);
                assert_eq!(got.availability, Availability::IdentityMismatch);
                let diagnostic = got.identity_diagnostics.as_ref().unwrap();
                if field == "model" {
                    assert_eq!(diagnostic.observed_model, None);
                } else {
                    assert_eq!(diagnostic.observed_provider, None);
                }
                let encoded = serde_json::to_string(&got).unwrap();
                assert!(!encoded.contains("SECRET-MARKER"));
                assert!(!encoded.contains("PRIVATE-"));
                assert!(!encoded.contains(&"x".repeat(257)));
                assert_eq!(got.observed_usage.cost_usd, None);
                assert_eq!(got.terminal.state, TerminalState::Unknown);
            }
        }
        let mut value = metadata();
        value["data"]["model"] = json!("x".repeat(256));
        assert_eq!(
            normalize(value)
                .identity_diagnostics
                .unwrap()
                .observed_model
                .unwrap()
                .len(),
            256
        );
    }

    #[test]
    fn identity_mismatch_diagnostic_cannot_enable_opted_in_content_lookup() {
        let mut calls = 0;
        let got = lookup_with(&selection(true), |endpoint, _| -> Result<_, ()> {
            calls += 1;
            assert_eq!(endpoint, Endpoint::Metadata);
            let mut value = metadata();
            value["data"]["model"] = json!("unexpected/model");
            Ok(payload(value))
        })
        .unwrap();
        assert_eq!(calls, 1);
        assert_eq!(got.metadata.availability, Availability::IdentityMismatch);
        assert_eq!(got.content.availability, Availability::MetadataUnavailable);
        assert_eq!(got.content.public_completion, None);
        assert_eq!(got.metadata.observed_usage.cost_usd, None);
        assert_eq!(got.metadata.terminal.state, TerminalState::Unknown);
    }

    #[test]
    fn malformed_duplicate_oversize_and_invalid_counters_are_rejected() {
        for bytes in [b"not-json".as_slice(), br#"{"data":{"id":"a","id":"b"}}"#] {
            let got = normalize_metadata(
                &selection(false),
                HttpPayload {
                    status: 200,
                    body: bytes.into(),
                    oversized: false,
                },
            );
            assert_eq!(got.availability, Availability::InvalidResponse);
        }
        for (key, value) in [
            ("total_cost", json!(-1)),
            ("native_tokens_prompt", json!(-1)),
            ("native_tokens_cached", json!(21)),
            ("native_tokens_reasoning", json!(8)),
        ] {
            let mut body = metadata();
            body["data"][key] = value;
            assert_eq!(normalize(body).availability, Availability::InvalidResponse);
        }
        let mut body = metadata();
        body["data"]["native_tokens_prompt"] = json!(u64::MAX);
        assert_eq!(normalize(body).availability, Availability::InvalidResponse);
        let got = normalize_metadata(
            &selection(false),
            HttpPayload {
                status: 200,
                body: vec![0; MAX_METADATA_BYTES + 1],
                oversized: false,
            },
        );
        assert_eq!(got.availability, Availability::ResponseTooLarge);
    }

    #[test]
    fn non_normal_finishes_and_cancellation_are_not_success() {
        for reason in [
            "length",
            "content_filter",
            "tool_calls",
            "error",
            "cancelled",
        ] {
            let mut value = metadata();
            value["data"]["finish_reason"] = json!(reason);
            assert_eq!(
                normalize(value).terminal.state,
                TerminalState::IncompleteFinishReported
            );
        }
        let mut value = metadata();
        value["data"]["cancelled"] = json!(true);
        assert_eq!(
            normalize(value).terminal.state,
            TerminalState::CancellationReported
        );
        let mut value = metadata();
        value["data"]["finish_reason"] = json!("PRIVATE-FINISH-MARKER");
        let got = normalize(value);
        assert_eq!(got.terminal.state, TerminalState::Unknown);
        assert!(
            !serde_json::to_string(&got)
                .unwrap()
                .contains("PRIVATE-FINISH-MARKER")
        );
    }

    #[test]
    fn fixed_url_and_id_admission_reject_injection() {
        for id in [
            "gen-",
            "GEN-123",
            "gen-a_b",
            "gen-a?x=1",
            "gen-a/b",
            "gen-a%20",
            "gen-a\n",
        ] {
            assert!(Selection::new(id.into(), "m".into(), None, false).is_err());
        }
        assert!(
            Selection::new(format!("gen-{}", "a".repeat(125)), "m".into(), None, false).is_err()
        );
        assert_eq!(
            Endpoint::Metadata.url("gen-a-b"),
            "https://openrouter.ai/api/v1/generation?id=gen-a-b"
        );
        assert_eq!(
            Endpoint::PublicContent.url("gen-a-b"),
            "https://openrouter.ai/api/v1/generation/content?id=gen-a-b"
        );
    }

    #[test]
    fn default_lookup_never_requests_content() {
        let mut calls = Vec::new();
        let got = lookup_with::<()>(&selection(false), |endpoint, url| {
            calls.push((endpoint, url.to_owned()));
            Ok(payload(metadata()))
        })
        .unwrap();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].0, Endpoint::Metadata);
        assert_eq!(got.content.availability, Availability::NotRequested);
    }

    #[test]
    fn opt_in_gets_only_public_completion_and_drops_private_fields() {
        let mut calls = Vec::new();
        let got = lookup_with::<()>(&selection(true), |endpoint, _| {
            calls.push(endpoint);
            Ok(payload(if endpoint == Endpoint::Metadata { metadata() } else {
                json!({"data":{"error":null,"input":{"messages":[{"content":"PRIVATE-INPUT"}]},"output":{"completion":"public-answer","reasoning":"PRIVATE-REASONING"}}})
            }))
        }).unwrap();
        assert_eq!(calls, vec![Endpoint::Metadata, Endpoint::PublicContent]);
        assert_eq!(
            got.content.public_completion.as_deref(),
            Some("public-answer")
        );
        let encoded = serde_json::to_string(&got).unwrap();
        assert!(!encoded.contains("PRIVATE-"));
        assert!(!encoded.contains("regenerate"));
    }

    #[test]
    fn http_states_do_not_leak_bodies_or_turn_404_into_zero_cost() {
        for (status, expected) in [
            (401, Availability::Unauthorized),
            (403, Availability::Forbidden),
            (404, Availability::NotFound),
            (429, Availability::RateLimited),
            (500, Availability::HttpFailure),
        ] {
            let mut calls = 0;
            let got = lookup_with::<()>(&selection(true), |_, _| {
                calls += 1;
                Ok(HttpPayload {
                    status,
                    body: b"PRIVATE-ERROR".to_vec(),
                    oversized: false,
                })
            })
            .unwrap();
            assert_eq!(calls, 1);
            assert_eq!(got.metadata.availability, expected);
            assert!(!got.metadata.observed_usage.cost_known);
            assert_eq!(got.content.availability, Availability::MetadataUnavailable);
            assert!(
                !serde_json::to_string(&got)
                    .unwrap()
                    .contains("PRIVATE-ERROR")
            );
        }
    }

    #[test]
    fn unavailable_content_preserves_metadata_accounting() {
        let got = lookup_with::<()>(&selection(true), |endpoint, _| {
            Ok(if endpoint == Endpoint::Metadata {
                payload(metadata())
            } else {
                HttpPayload {
                    status: 403,
                    body: vec![],
                    oversized: false,
                }
            })
        })
        .unwrap();
        assert_eq!(got.content.availability, Availability::Forbidden);
        assert!(got.metadata.observed_usage.cost_known);
        assert_eq!(
            got.metadata.terminal.state,
            TerminalState::NormalStopReported
        );
    }

    #[test]
    fn stored_output_is_not_a_claim_of_normal_completion() {
        let mut body = metadata();
        body["data"]["finish_reason"] = json!("length");
        let got = lookup_with::<()>(&selection(true), |endpoint, _| {
            Ok(payload(if endpoint == Endpoint::Metadata {
                body.clone()
            } else {
                json!({"data":{"output":{"completion":"partial"}}})
            }))
        })
        .unwrap();
        assert_eq!(
            got.metadata.terminal.state,
            TerminalState::IncompleteFinishReported
        );
        assert_eq!(got.content.availability, Availability::Available);
        assert_eq!(got.content.public_completion.as_deref(), Some("partial"));
    }

    #[test]
    fn stored_error_and_absent_output_never_publish_text() {
        let got = normalize_content(payload(
            json!({"data":{"error":{"message":"PRIVATE-ERROR"},"output":{"completion":"do-not-publish"}}}),
        ));
        assert_eq!(got.availability, Availability::ProviderError);
        assert_eq!(got.public_completion, None);
        let got = normalize_content(payload(json!({"data":{"output":null}})));
        assert_eq!(got.availability, Availability::NoStoredOutput);
    }

    #[test]
    fn transport_failure_does_not_retry_or_post() {
        let mut calls = 0;
        let result = lookup_with(&selection(true), |_, _| {
            calls += 1;
            Err::<HttpPayload, _>("controlled-core-error")
        });
        assert!(matches!(result, Err("controlled-core-error")));
        assert_eq!(calls, 1);
    }
}
