//! Rust/Wasm translation candidate for the existing Gateway finalizer owner.
//!
//! The WIT world and imported interfaces stay the original Python pack's
//! identities. This candidate is additive and does not claim promotion or a
//! separate canonical caller identity.

wit_bindgen::generate!({
    path: [
        "../../crates/tobkiri-wit/wit/core/core.wit",
        "../local.ai-tool-bridge-ordered-python/wit/pack-v2.wit",
        "../local.ai-usage-cost-python/wit",
        "../local.ai-stream-normalizer-python/wit",
        "../local.ai-gateway-tool-caller-python/wit",
        "../local.ai-gateway-finalizer-python/wit",
    ],
    world: "local:ai-gateway-finalizer-python/pack@1.0.0",
    generate_all,
});

use exports::local::ai_gateway_finalizer_python::finalizer::{
    DomainError, FinalizationOutcome, Guest as Finalizer,
};
use exports::tobkiri::core::lifecycle::Guest as Lifecycle;
use ordered_python_compat::{CodecError, PyValue, dumps, loads, pystr, pytruthy};
use tobkiri::core::types::{Error, ErrorCode};

const USAGE_GENERATE_OPERATION: &str = "rumi_ai_usage_pack.ai-usage-cost.generate";
const USAGE_STREAM_OPERATION: &str = "rumi_ai_usage_pack.ai-usage-cost.stream";
#[cfg(target_arch = "wasm32")]
const STREAM_NORMALIZE_OPERATION: &str = "rumi_ai_stream_pack.ai-stream-normalize";
const PRICING_UNIT: &str = "usd_per_token";

const RUNTIME_INFO: &str = "{\"implementation\":\"rust\",\"candidate\":\"local.ai-gateway-finalizer-rust\",\"source_owner\":\"local.ai-gateway-finalizer-python\",\"promotion\":\"not-promoted\",\"protocol\":\"ordered-finite-json-v2\"}";

const INPUT_ERROR: &str = "invalid Gateway finalization request";
const RESOURCE_ERROR: &str = "Gateway finalization resource limit exceeded";
const TERMINAL_ERROR: &str = "Gateway finalization failed";
#[cfg(not(target_arch = "wasm32"))]
const NATIVE_IMPORT_ERROR: &str = "finalization dependency unavailable in native execution";

const CANDIDATE_FIELDS: [&str; 14] = [
    "model_id",
    "provider_instance_id",
    "catalog_provider_instance_id",
    "catalog_revision",
    "capabilities",
    "modalities",
    "context_length",
    "input_cost",
    "output_cost",
    "priority",
    "available",
    "health",
    "health_observed_at",
    "raw",
];

#[derive(Debug)]
enum Failure {
    Input,
    Resource,
    Terminal,
    Domain(DomainErrorData),
    #[cfg(target_arch = "wasm32")]
    Imported(Error),
    #[cfg(not(target_arch = "wasm32"))]
    NativeUnavailable,
}

impl Failure {
    fn into_wit(self) -> Error {
        match self {
            Self::Input => Error {
                code: ErrorCode::Invalid,
                message: INPUT_ERROR.into(),
            },
            Self::Resource => Error {
                code: ErrorCode::LimitExceeded,
                message: RESOURCE_ERROR.into(),
            },
            Self::Terminal => Error {
                code: ErrorCode::Internal,
                message: TERMINAL_ERROR.into(),
            },
            Self::Domain(_) => unreachable!("domain outcomes are not Core errors"),
            #[cfg(target_arch = "wasm32")]
            Self::Imported(error) => error,
            #[cfg(not(target_arch = "wasm32"))]
            Self::NativeUnavailable => Error {
                code: ErrorCode::Unavailable,
                message: NATIVE_IMPORT_ERROR.into(),
            },
        }
    }
}

enum FinalizeResult {
    Finalized(String),
    DomainRejected { code: String, message: String },
}

struct Request {
    operation: String,
    value: PyValue,
    request_id: String,
    candidate: Candidate,
    attempts: PyValue,
    attempt_number: Option<PyValue>,
}

struct Candidate {
    model_id: String,
    provider_instance_id: String,
    catalog_provider_instance_id: String,
    catalog_revision: String,
    input_cost: PyValue,
    output_cost: PyValue,
    raw: PyValue,
}

struct Pack;

impl Lifecycle for Pack {
    fn init(_config: String) -> Result<(), Error> {
        Ok(())
    }

    fn shutdown() {}
}

impl Finalizer for Pack {
    fn runtime_info() -> String {
        RUNTIME_INFO.to_owned()
    }

    fn generate(payload: String) -> Result<FinalizationOutcome, Error> {
        invoke("generate", &payload).map_err(Failure::into_wit)
    }

    fn stream(payload: String) -> Result<FinalizationOutcome, Error> {
        invoke("stream", &payload).map_err(Failure::into_wit)
    }
}

#[cfg(target_arch = "wasm32")]
export!(Pack);

fn invoke(operation: &str, payload: &str) -> Result<FinalizationOutcome, Failure> {
    match finalize(operation, payload)? {
        FinalizeResult::Finalized(value) => Ok(FinalizationOutcome::Finalized(value)),
        FinalizeResult::DomainRejected { code, message } => {
            Ok(FinalizationOutcome::DomainRejected(DomainError {
                code,
                message,
            }))
        }
    }
}

fn finalize(operation: &str, payload: &str) -> Result<FinalizeResult, Failure> {
    let request = parse_request(operation, payload)?;
    let projected = if request.operation == "generate" {
        finalize_generate(&request)
    } else {
        finalize_stream(&request)
    };
    let projected = match projected {
        Ok(projected) => projected,
        Err(Failure::Domain(domain)) => {
            // The original adapter validates this exact evidence record in
            // ordered DATA before returning its typed domain outcome.
            dumps(&PyValue::object([
                ("code".into(), PyValue::String(domain.code.clone())),
                ("message".into(), PyValue::String(domain.message.clone())),
            ]))
            .map_err(map_projection_codec)?;
            return Ok(FinalizeResult::DomainRejected {
                code: domain.code,
                message: domain.message,
            });
        }
        Err(error) => return Err(error),
    };
    match dumps(&projected) {
        Ok(encoded) => Ok(FinalizeResult::Finalized(encoded)),
        Err(CodecError::Resource) => Err(Failure::Resource),
        Err(_) => Err(Failure::Terminal),
    }
}

fn parse_request(operation: &str, payload: &str) -> Result<Request, Failure> {
    if !matches!(operation, "generate" | "stream") {
        return Err(Failure::Input);
    }
    let parsed = loads(payload).map_err(map_input_codec)?;
    let mut fields = parsed.into_object().ok_or(Failure::Input)?;
    let expected: &[&str] = if operation == "stream" {
        &[
            "value",
            "request_id",
            "candidate",
            "attempts",
            "packvm_response",
            "attempt_number",
        ]
    } else {
        &[
            "value",
            "request_id",
            "candidate",
            "attempts",
            "packvm_response",
        ]
    };
    if !exact_keys(&fields, expected) {
        return Err(Failure::Input);
    }

    let value = take_field(&mut fields, "value").ok_or(Failure::Input)?;
    let request_id = take_field(&mut fields, "request_id")
        .and_then(PyValue::into_string)
        .ok_or(Failure::Input)?;
    let candidate = parse_candidate(take_field(&mut fields, "candidate").ok_or(Failure::Input)?)?;
    let attempts = take_field(&mut fields, "attempts").ok_or(Failure::Input)?;
    if !matches!(&attempts, PyValue::Array(values) if values.iter().all(|item| item.as_object().is_some()))
    {
        return Err(Failure::Input);
    }
    if !matches!(
        take_field(&mut fields, "packvm_response"),
        Some(PyValue::Bool(false))
    ) {
        return Err(Failure::Input);
    }
    let attempt_number = if operation == "stream" {
        let value = take_field(&mut fields, "attempt_number").ok_or(Failure::Input)?;
        if !matches!(&value, PyValue::Int(digits) if positive_integer(digits)) {
            return Err(Failure::Input);
        }
        Some(value)
    } else {
        None
    };
    Ok(Request {
        operation: operation.to_owned(),
        value,
        request_id,
        candidate,
        attempts,
        attempt_number,
    })
}

fn parse_candidate(value: PyValue) -> Result<Candidate, Failure> {
    let fields = value.into_object().ok_or(Failure::Input)?;
    if !exact_keys(&fields, &CANDIDATE_FIELDS) {
        return Err(Failure::Input);
    }
    for name in [
        "model_id",
        "provider_instance_id",
        "catalog_provider_instance_id",
        "catalog_revision",
        "health",
    ] {
        if get_field(&fields, name).and_then(PyValue::as_str).is_none() {
            return Err(Failure::Input);
        }
    }
    for name in ["capabilities", "modalities"] {
        if !matches!(get_field(&fields, name), Some(PyValue::Array(values)) if values.iter().all(|item| item.as_str().is_some()))
        {
            return Err(Failure::Input);
        }
    }
    for name in ["context_length", "priority"] {
        if !matches!(get_field(&fields, name), Some(PyValue::Int(_))) {
            return Err(Failure::Input);
        }
    }
    for name in ["input_cost", "output_cost", "health_observed_at"] {
        if !matches!(
            get_field(&fields, name),
            Some(PyValue::Null | PyValue::Int(_) | PyValue::Float(_))
        ) {
            return Err(Failure::Input);
        }
    }
    if !matches!(get_field(&fields, "available"), Some(PyValue::Bool(_)))
        || !matches!(get_field(&fields, "raw"), Some(PyValue::Object(_)))
    {
        return Err(Failure::Input);
    }
    Ok(Candidate {
        model_id: string_field(&fields, "model_id")?,
        provider_instance_id: string_field(&fields, "provider_instance_id")?,
        catalog_provider_instance_id: string_field(&fields, "catalog_provider_instance_id")?,
        catalog_revision: string_field(&fields, "catalog_revision")?,
        input_cost: clone_field(&fields, "input_cost")?,
        output_cost: clone_field(&fields, "output_cost")?,
        raw: clone_field(&fields, "raw")?,
    })
}

fn finalize_generate(request: &Request) -> Result<PyValue, Failure> {
    let (mut result, usage_has_non_string_key) =
        normalize_generate_result(&request.value, &request.request_id, &request.candidate)?;
    let intents = clone_field(result.as_object().ok_or(Failure::Terminal)?, "tool_intents")?;
    let normalized = tool_intents(&intents, &request.request_id, false)?;
    set_field(&mut result, "tool_intents", normalized)?;

    // Python dict() accepts hashable non-string keys. Ordered dependency DATA
    // rejects them only after the original tool-caller has completed. Such a
    // key cannot be replaced by an equal string key, so this private marker
    // can never conceal a usable final usage object or escape into output.
    if usage_has_non_string_key {
        return Err(Failure::Terminal);
    }
    let usage = clone_field(result.as_object().ok_or(Failure::Terminal)?, "usage")?;
    let provenance = clone_field(
        result.as_object().ok_or(Failure::Terminal)?,
        "usage_provenance",
    )?;
    let cost = usage_cost(&usage, &request.candidate, &provenance, false)?;
    set_field(&mut result, "usage_cost", cost)?;
    set_field(&mut result, "attempts", request.attempts.clone())?;
    Ok(result)
}

fn normalize_generate_result(
    value: &PyValue,
    request_id: &str,
    selected: &Candidate,
) -> Result<(PyValue, bool), Failure> {
    let fields = value
        .as_object()
        .ok_or_else(|| domain("invalid_response", "provider result must be an object"))?;
    let status = get_field(fields, "status");
    if let Some(status) = status {
        if matches!(status, PyValue::Array(_) | PyValue::Object(_)) {
            return Err(Failure::Terminal);
        }
        if !matches!(status, PyValue::Null)
            && !matches!(status, PyValue::String(text) if text == "ok")
        {
            let code = truthy_field(fields, "error_code")
                .map(pystr)
                .unwrap_or_else(|| "provider_unavailable".into());
            let message = truthy_field(fields, "message")
                .map(pystr)
                .unwrap_or_else(|| "provider failed".into());
            return Err(domain(code, message));
        }
    }

    let output = clone_field_or(fields, "output", PyValue::Null);
    let tool_intents = iterable_list(
        truthy_field(fields, "tool_intents"),
        PyValue::Array(Vec::new()),
    )?;
    let finish_reason = truthy_field(fields, "finish_reason")
        .map(pystr)
        .unwrap_or_else(|| "stop".into());
    let (usage, usage_has_non_string_key) =
        python_dict(truthy_field(fields, "usage"), PyValue::object([]))?;
    let usage_provenance = truthy_field(fields, "usage_provenance")
        .map(pystr)
        .unwrap_or_else(|| "provider_reported".into());

    let mut output_fields = vec![
        ("status".into(), PyValue::String("ok".into())),
        ("request_id".into(), PyValue::String(request_id.into())),
        (
            "model_id".into(),
            PyValue::String(selected.model_id.clone()),
        ),
        (
            "provider_instance_id".into(),
            PyValue::String(selected.provider_instance_id.clone()),
        ),
        (
            "catalog_provider_instance_id".into(),
            PyValue::String(selected.catalog_provider_instance_id.clone()),
        ),
        (
            "catalog_revision".into(),
            PyValue::String(selected.catalog_revision.clone()),
        ),
        (
            "pricing_revision".into(),
            PyValue::String(selected.catalog_revision.clone()),
        ),
        ("output".into(), output),
        ("tool_intents".into(), tool_intents),
        ("finish_reason".into(), PyValue::String(finish_reason)),
        ("usage".into(), usage),
        ("usage_provenance".into(), PyValue::String(usage_provenance)),
    ];

    let metadata = get_field(fields, "metadata");
    if get_field(fields, "task_state").is_some() || matches!(metadata, Some(PyValue::Object(_))) {
        let mut evidence = metadata
            .and_then(PyValue::as_object)
            .map(|entries| {
                PyValue::object(
                    entries
                        .iter()
                        .map(|(key, value)| (key.clone(), value.clone())),
                )
            })
            .unwrap_or_else(|| PyValue::object([]));
        if let Some(task_state) = get_field(fields, "task_state") {
            set_field(&mut evidence, "task_state", task_state.clone())?;
        }
        let task_state = message_task_state(get_field(fields, "finish_reason"), &evidence);
        output_fields.push(("task_state".into(), PyValue::String(task_state.into())));
    }
    Ok((PyValue::Object(output_fields), usage_has_non_string_key))
}

fn finalize_stream(request: &Request) -> Result<PyValue, Failure> {
    let attempt = request
        .attempt_number
        .as_ref()
        .ok_or(Failure::Input)?
        .clone();
    let normalized = stream_normalize(&request.request_id, &attempt, &request.value)?;
    let mut events = normalized
        .get("events")
        .and_then(PyValue::as_array)
        .ok_or(Failure::Terminal)?
        .to_vec();

    // Keep the original two-pass order: all usage calls precede all tool calls.
    for event in &mut events {
        let fields = event.as_object().ok_or(Failure::Terminal)?;
        let is_usage = get_field(fields, "type") == Some(&PyValue::String("usage".into()));
        let Some(usage) = get_field(fields, "usage") else {
            continue;
        };
        if is_usage && usage.as_object().is_some() {
            let cost = usage_cost(
                usage,
                &request.candidate,
                &PyValue::String("provider_reported".into()),
                true,
            )?;
            set_field(event, "usage_cost", cost)?;
        }
    }
    for event in &mut events {
        let fields = event.as_object().ok_or(Failure::Terminal)?;
        let is_tool =
            get_field(fields, "type") == Some(&PyValue::String("tool_intent_delta".into()));
        let Some(intent) = get_field(fields, "tool_intent") else {
            continue;
        };
        if is_tool && intent.as_object().is_some() {
            let normalized = tool_intents(
                &PyValue::Array(vec![intent.clone()]),
                &request.request_id,
                true,
            )?;
            let first = normalized
                .as_array()
                .and_then(|items| items.first())
                .cloned()
                .ok_or(Failure::Terminal)?;
            set_field(event, "tool_intent", first)?;
        }
    }

    let fields = request.value.as_object().ok_or(Failure::Terminal)?;
    let intents = clone_field_or(fields, "tool_intents", PyValue::Array(Vec::new()));
    let intents = tool_intents(&intents, &request.request_id, true)?;
    let output = clone_field_or(fields, "output", PyValue::String(String::new()));
    let finish_reason = clone_field_or(fields, "finish_reason", PyValue::Null);
    let delivery_mode = clone_field_or(fields, "delivery_mode", PyValue::String("buffered".into()));
    Ok(PyValue::object([
        ("status".into(), PyValue::String("ok".into())),
        ("output".into(), output),
        ("tool_intents".into(), intents),
        ("finish_reason".into(), finish_reason),
        ("delivery_mode".into(), delivery_mode),
        (
            "request_id".into(),
            PyValue::String(request.request_id.clone()),
        ),
        (
            "model_id".into(),
            PyValue::String(request.candidate.model_id.clone()),
        ),
        (
            "provider_instance_id".into(),
            PyValue::String(request.candidate.provider_instance_id.clone()),
        ),
        ("events".into(), PyValue::Array(events)),
        ("attempts".into(), request.attempts.clone()),
    ]))
}

fn stream_normalize(
    request_id: &str,
    attempt: &PyValue,
    value: &PyValue,
) -> Result<PyValue, Failure> {
    let payload = PyValue::object([
        ("request_id".into(), PyValue::String(request_id.into())),
        ("provider_attempt".into(), attempt.clone()),
        ("value".into(), value.clone()),
    ]);
    let encoded = encode_dependency_request(&payload)?;
    #[cfg(target_arch = "wasm32")]
    let response = local::ai_stream_normalizer_python::stream_normalizer::normalize(
        STREAM_NORMALIZE_OPERATION,
        &encoded,
    )
    .map_err(Failure::Imported)?;
    #[cfg(not(target_arch = "wasm32"))]
    {
        let _ = encoded;
        Err(Failure::NativeUnavailable)
    }
    #[cfg(target_arch = "wasm32")]
    {
        let decoded = decode_dependency_response(&response)?;
        let fields = decoded.as_object().ok_or(Failure::Terminal)?;
        if !matches!(get_field(fields, "events"), Some(PyValue::Array(events)) if events.iter().all(|event| event.as_object().is_some()))
        {
            return Err(Failure::Terminal);
        }
        Ok(decoded)
    }
}

fn usage_cost(
    usage: &PyValue,
    candidate: &Candidate,
    provenance: &PyValue,
    streaming: bool,
) -> Result<PyValue, Failure> {
    if usage.as_object().is_none() {
        return Err(Failure::Terminal);
    }
    let currency = candidate
        .raw
        .get("currency")
        .filter(|value| pytruthy(value))
        .map(pystr)
        .unwrap_or_else(|| "USD".into());
    let payload = PyValue::object([
        (
            "usage".into(),
            PyValue::object(
                usage
                    .as_object()
                    .ok_or(Failure::Terminal)?
                    .iter()
                    .map(|(key, value)| (key.clone(), value.clone())),
            ),
        ),
        (
            "usage_provenance".into(),
            PyValue::String(pystr(provenance)),
        ),
        (
            "pricing".into(),
            PyValue::object([
                ("input".into(), candidate.input_cost.clone()),
                ("output".into(), candidate.output_cost.clone()),
                ("currency".into(), PyValue::String(currency)),
                ("unit".into(), PyValue::String(PRICING_UNIT.into())),
            ]),
        ),
        (
            "pricing_revision".into(),
            PyValue::String(candidate.catalog_revision.clone()),
        ),
    ]);
    let encoded = encode_dependency_request(&payload)?;
    let operation = if streaming {
        USAGE_STREAM_OPERATION
    } else {
        USAGE_GENERATE_OPERATION
    };
    #[cfg(target_arch = "wasm32")]
    let response = local::ai_usage_cost_python::usage_cost::calculate(operation, &encoded)
        .map_err(Failure::Imported)?;
    #[cfg(not(target_arch = "wasm32"))]
    {
        let _ = (encoded, operation);
        Err(Failure::NativeUnavailable)
    }
    #[cfg(target_arch = "wasm32")]
    {
        let decoded = decode_dependency_response(&response)?;
        if decoded.as_object().is_none() {
            return Err(Failure::Terminal);
        }
        Ok(decoded)
    }
}

fn tool_intents(intents: &PyValue, request_id: &str, streaming: bool) -> Result<PyValue, Failure> {
    let payload = PyValue::object([
        ("request_id".into(), PyValue::String(request_id.into())),
        ("intents".into(), intents.clone()),
    ]);
    let encoded = encode_dependency_request(&payload)?;
    #[cfg(target_arch = "wasm32")]
    let response = if streaming {
        local::ai_gateway_tool_caller_python::caller::stream_intents(&encoded)
    } else {
        local::ai_gateway_tool_caller_python::caller::generate_intents(&encoded)
    }
    .map_err(Failure::Imported)?;
    #[cfg(not(target_arch = "wasm32"))]
    {
        let _ = (encoded, streaming);
        Err(Failure::NativeUnavailable)
    }
    #[cfg(target_arch = "wasm32")]
    {
        let decoded = decode_dependency_response(&response)?;
        if !matches!(&decoded, PyValue::Array(values) if values.iter().all(|item| item.as_object().is_some()))
        {
            return Err(Failure::Terminal);
        }
        Ok(decoded)
    }
}

fn encode_dependency_request(value: &PyValue) -> Result<String, Failure> {
    dumps(value).map_err(map_projection_codec)
}

#[cfg(target_arch = "wasm32")]
fn decode_dependency_response(value: &str) -> Result<PyValue, Failure> {
    loads(value).map_err(map_dependency_codec)
}

#[cfg(target_arch = "wasm32")]
fn map_dependency_codec(error: CodecError) -> Failure {
    match error {
        CodecError::Resource => Failure::Resource,
        _ => Failure::Terminal,
    }
}

fn map_input_codec(error: CodecError) -> Failure {
    match error {
        CodecError::Resource => Failure::Resource,
        _ => Failure::Input,
    }
}

fn map_projection_codec(error: CodecError) -> Failure {
    match error {
        CodecError::Resource => Failure::Resource,
        _ => Failure::Terminal,
    }
}

fn domain(code: impl Into<String>, message: impl Into<String>) -> Failure {
    Failure::Domain(DomainErrorData {
        code: code.into(),
        message: message.into(),
    })
}

#[derive(Debug)]
struct DomainErrorData {
    code: String,
    message: String,
}

fn message_task_state(finish_reason: Option<&PyValue>, metadata: &PyValue) -> &'static str {
    let metadata_fields = metadata.as_object().unwrap_or(&[]);
    let thinking = get_field(metadata_fields, "thinking").and_then(PyValue::as_object);
    let status = "complete".to_owned();
    let states = [
        status,
        finish_reason
            .filter(|value| pytruthy(value))
            .map(pystr)
            .unwrap_or_default(),
        get_field(metadata_fields, "task_state")
            .filter(|value| pytruthy(value))
            .map(pystr)
            .unwrap_or_default(),
        thinking
            .and_then(|fields| get_field(fields, "state"))
            .filter(|value| pytruthy(value))
            .map(pystr)
            .unwrap_or_default(),
    ]
    .map(|state| state.to_lowercase());
    let contains = |choices: &[&str]| states.iter().any(|state| choices.contains(&state.as_str()));
    if contains(&["cancelled", "canceled", "interrupted"])
        || get_field(metadata_fields, "cancelled") == Some(&PyValue::Bool(true))
    {
        return "cancelled";
    }
    if contains(&[
        "waiting_approval",
        "awaiting_approval",
        "approval_required",
        "authority_approval_required",
    ]) || get_field(metadata_fields, "approval_required") == Some(&PyValue::Bool(true))
    {
        return "waiting_approval";
    }
    if contains(&["waiting_user", "waiting_for_user", "awaiting_user"])
        || get_field(metadata_fields, "waiting_for_user") == Some(&PyValue::Bool(true))
    {
        return "waiting_user";
    }
    if contains(&["error", "failed", "failure"]) {
        return "failed";
    }
    if contains(&["running", "streaming", "pending", "tool_call", "tool_calls"])
        || get_field(metadata_fields, "draft") == Some(&PyValue::Bool(true))
        || get_field(metadata_fields, "streaming") == Some(&PyValue::Bool(true))
    {
        return "running";
    }
    let known = [
        "stop",
        "end_turn",
        "completed",
        "complete",
        "waiting_user",
        "waiting_for_user",
        "awaiting_user",
        "waiting_approval",
        "awaiting_approval",
        "approval_required",
        "authority_approval_required",
        "running",
        "streaming",
        "pending",
        "tool_call",
        "tool_calls",
        "cancelled",
        "canceled",
        "interrupted",
    ];
    for value in [
        get_field(metadata_fields, "task_state"),
        thinking.and_then(|fields| get_field(fields, "state")),
    ]
    .into_iter()
    .flatten()
    {
        if !matches!(value, PyValue::Null)
            && !matches!(value, PyValue::String(text) if text.is_empty())
            && !known.contains(&pystr(value).to_lowercase().as_str())
        {
            return "unknown";
        }
    }
    let finish = states.get(1).map(String::as_str).unwrap_or_default();
    if ["stop", "end_turn", "completed", "complete"].contains(&finish)
        && ["complete", "completed", "success", "succeeded"].contains(&states[0].as_str())
    {
        return "completed";
    }
    if !finish.is_empty() {
        return "failed";
    }
    "unknown"
}

fn iterable_list(value: Option<&PyValue>, fallback: PyValue) -> Result<PyValue, Failure> {
    let Some(value) = value.filter(|value| pytruthy(value)) else {
        return Ok(fallback);
    };
    let values = match value {
        PyValue::Array(values) => values.clone(),
        PyValue::Object(values) => values
            .iter()
            .map(|(key, _)| PyValue::String(key.clone()))
            .collect(),
        PyValue::String(text) => text
            .chars()
            .map(|character| PyValue::String(character.to_string()))
            .collect(),
        _ => return Err(Failure::Terminal),
    };
    Ok(PyValue::Array(values))
}

fn python_dict(value: Option<&PyValue>, fallback: PyValue) -> Result<(PyValue, bool), Failure> {
    let Some(value) = value.filter(|value| pytruthy(value)) else {
        return Ok((fallback, false));
    };
    match value {
        PyValue::Object(fields) => Ok((
            PyValue::Object(
                fields
                    .iter()
                    .map(|(key, value)| (key.clone(), value.clone()))
                    .collect(),
            ),
            false,
        )),
        PyValue::Array(rows) => {
            let mut fields = Vec::with_capacity(rows.len());
            let mut non_string_key = false;
            for row in rows {
                // dict(sequence) iterates each row: arrays yield values,
                // objects yield keys, and strings yield Unicode code points.
                let (key, value) = match row {
                    PyValue::Array(pair) if pair.len() == 2 => (pair[0].clone(), pair[1].clone()),
                    PyValue::Object(pair) if pair.len() == 2 => (
                        PyValue::String(pair[0].0.clone()),
                        PyValue::String(pair[1].0.clone()),
                    ),
                    PyValue::String(pair) => {
                        let mut characters = pair.chars();
                        match (characters.next(), characters.next(), characters.next()) {
                            (Some(key), Some(value), None) => (
                                PyValue::String(key.to_string()),
                                PyValue::String(value.to_string()),
                            ),
                            _ => return Err(Failure::Terminal),
                        }
                    }
                    _ => return Err(Failure::Terminal),
                };
                match &key {
                    PyValue::String(key) => fields.push((key.clone(), value)),
                    PyValue::Array(_) | PyValue::Object(_) => return Err(Failure::Terminal),
                    _ => non_string_key = true,
                }
            }
            // Continue validating all rows even after a hashable non-string
            // key: a later malformed/unhashable row still fails dict() before
            // any dependency call. String-key replacement keeps first order.
            Ok((PyValue::object(fields), non_string_key))
        }
        _ => Err(Failure::Terminal),
    }
}

fn get_field<'a>(fields: &'a [(String, PyValue)], name: &str) -> Option<&'a PyValue> {
    fields
        .iter()
        .find(|(key, _)| key.as_str() == name)
        .map(|(_, value)| value)
}

fn take_field(fields: &mut Vec<(String, PyValue)>, name: &str) -> Option<PyValue> {
    fields
        .iter()
        .position(|(key, _)| key.as_str() == name)
        .map(|index| fields.remove(index).1)
}

fn exact_keys(fields: &[(String, PyValue)], expected: &[&str]) -> bool {
    fields.len() == expected.len()
        && fields
            .iter()
            .all(|(key, _)| expected.contains(&key.as_str()))
}

fn string_field(fields: &[(String, PyValue)], name: &str) -> Result<String, Failure> {
    get_field(fields, name)
        .and_then(PyValue::as_str)
        .map(str::to_owned)
        .ok_or(Failure::Input)
}

fn clone_field(fields: &[(String, PyValue)], name: &str) -> Result<PyValue, Failure> {
    get_field(fields, name).cloned().ok_or(Failure::Input)
}

fn clone_field_or(fields: &[(String, PyValue)], name: &str, fallback: PyValue) -> PyValue {
    get_field(fields, name).cloned().unwrap_or(fallback)
}

fn truthy_field<'a>(fields: &'a [(String, PyValue)], name: &str) -> Option<&'a PyValue> {
    get_field(fields, name).filter(|value| pytruthy(value))
}

fn set_field(value: &mut PyValue, name: &str, replacement: PyValue) -> Result<(), Failure> {
    let PyValue::Object(fields) = value else {
        return Err(Failure::Terminal);
    };
    if let Some((_, current)) = fields.iter_mut().find(|(key, _)| key.as_str() == name) {
        *current = replacement;
    } else {
        fields.push((name.into(), replacement));
    }
    Ok(())
}

fn positive_integer(value: &str) -> bool {
    value.bytes().all(|byte| byte.is_ascii_digit()) && value.bytes().any(|byte| byte != b'0')
}

#[cfg(all(test, not(target_arch = "wasm32")))]
mod tests {
    use super::{Failure, FinalizationOutcome, invoke};
    use serde_json::Value;

    const SOURCE_CASES: &[u8] =
        include_bytes!("../../../packs/local.ai-gateway-finalizer-python/source-cases.json");
    const SOURCE_ORACLE: &[u8] = include_bytes!(
        "../../../tools/python313-producer/trials/2026-10-20-finalization/source/finalizer.oracle.json"
    );

    #[test]
    fn ten_source_cases_keep_original_python_expectations_and_native_import_limits_explicit() {
        let catalog: Value = serde_json::from_slice(SOURCE_CASES).expect("source case catalog");
        let oracle: Value = serde_json::from_slice(SOURCE_ORACLE).expect("source observation");
        assert_eq!(catalog["cases"].as_array().map(Vec::len), Some(10));
        assert_eq!(oracle["cases"].as_array().map(Vec::len), Some(10));
        assert_eq!(
            oracle["format"],
            "tobkiri.gateway-finalizer-source-observations/1"
        );

        let catalog_cases = catalog["cases"].as_array().expect("catalog cases");
        let oracle_cases = oracle["cases"].as_array().expect("oracle cases");
        let mut finalized = 0;
        let mut domain_rejected = 0;
        let mut core_error = 0;

        for (source, observed) in catalog_cases.iter().zip(oracle_cases) {
            assert_eq!(source["id"], observed["id"]);
            assert_eq!(source["operation"], observed["operation"]);
            assert_eq!(source["expected_kind"], observed["expected_kind"]);
            let operation = observed["operation"].as_str().expect("operation");
            let input = observed["input_json"].as_str().expect("source input");
            match source["expected_kind"].as_str().expect("case kind") {
                "domain-rejected" => {
                    domain_rejected += 1;
                    let expected = &source["expected_error"];
                    match invoke(operation, input).expect("domain result is not a Core error") {
                        FinalizationOutcome::DomainRejected(actual) => {
                            assert_eq!(actual.code, expected["code"].as_str().unwrap());
                            assert_eq!(actual.message, expected["message"].as_str().unwrap());
                        }
                        FinalizationOutcome::Finalized(_) => panic!("domain result finalized"),
                    }
                }
                "finalized" => {
                    finalized += 1;
                    assert!(observed["expected"]["output_json"].is_string());
                    assert!(matches!(
                        invoke(operation, input),
                        Err(Failure::NativeUnavailable)
                    ));
                }
                "core-error" => {
                    core_error += 1;
                    assert!(source["expected_error"].is_object());
                    assert!(matches!(
                        invoke(operation, input),
                        Err(Failure::NativeUnavailable)
                    ));
                }
                kind => panic!("unexpected source case kind {kind}"),
            }
        }
        assert_eq!((finalized, domain_rejected, core_error), (5, 2, 3));
    }
}

#[cfg(all(test, unix, not(target_arch = "wasm32")))]
mod normalize_oracle_tests;
