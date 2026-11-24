//! Rust/Wasm implementation candidate for the existing Gateway caller helper.
//!
//! The exported WIT owner and the typed bridge@2 import are unchanged. This
//! helper does not implement the original Gateway providers or normalize data
//! locally when its real bridge dependency is unavailable.

wit_bindgen::generate!({
    path: [
        "../../crates/tobkiri-wit/wit/core/core.wit",
        "../local.ai-tool-bridge-ordered-python/wit/pack-v2.wit",
        "../local.ai-gateway-tool-caller-python/wit",
    ],
    world: "local:ai-gateway-tool-caller-python/pack@1.0.0",
    generate_all,
});

use exports::local::ai_gateway_tool_caller_python::caller::Guest as Caller;
use exports::tobkiri::core::lifecycle::Guest as Lifecycle;
use ordered_python_compat::{CodecError, PyValue, dumps, loads};
use tobkiri::core::types::{Error, ErrorCode};

const GENERATE_OPERATION: &str = "rumi_ai_tool_bridge_pack.ai-tool-intent-normalize.generate";
const STREAM_OPERATION: &str = "rumi_ai_tool_bridge_pack.ai-tool-intent-normalize.stream";
const BRIDGE_CONTRACT: &str = "tobkiri.service.ai.tool_intent.normalize.v1";

const RUNTIME_INFO: &str = "{\"implementation\":\"rust\",\"candidate\":\"local.ai-gateway-tool-caller-rust\",\"source_owner\":\"local.ai-gateway-tool-caller-python\",\"promotion\":\"not-promoted\",\"protocol\":\"ordered-finite-json-v2\"}";

const INPUT_ERROR: &str = "invalid Gateway tool caller request";
const RESOURCE_ERROR: &str = "Gateway tool caller resource limit exceeded";
const TERMINAL_ERROR: &str = "Gateway tool bridge failed";
#[cfg(not(target_arch = "wasm32"))]
const NATIVE_IMPORT_ERROR: &str = "Gateway tool bridge unavailable in native execution";

#[derive(Debug)]
enum Failure {
    Input,
    Resource,
    Terminal,
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

struct Request {
    request_id: String,
    collection: PyValue,
}

struct Pack;

impl Lifecycle for Pack {
    fn init(_config: String) -> Result<(), Error> {
        Ok(())
    }

    fn shutdown() {}
}

impl Caller for Pack {
    fn runtime_info() -> String {
        RUNTIME_INFO.to_owned()
    }

    fn generate_intents(payload: String) -> Result<String, Error> {
        invoke("generate-intents", &payload).map_err(Failure::into_wit)
    }

    fn stream_intents(payload: String) -> Result<String, Error> {
        invoke("stream-intents", &payload).map_err(Failure::into_wit)
    }

    fn stream_event_intents(payload: String) -> Result<String, Error> {
        invoke("stream-event-intents", &payload).map_err(Failure::into_wit)
    }
}

#[cfg(target_arch = "wasm32")]
export!(Pack);

fn invoke(operation: &str, payload: &str) -> Result<String, Failure> {
    let request = parse_request(operation, payload)?;
    let result = if operation == "stream-event-intents" {
        normalize_stream_events(request.collection, &request.request_id)?
    } else {
        let bridge_operation = if operation == "stream-intents" {
            STREAM_OPERATION
        } else {
            GENERATE_OPERATION
        };
        PyValue::Array(normalize_intents(
            request.collection,
            &request.request_id,
            bridge_operation,
        )?)
    };
    dumps(&result).map_err(map_terminal_codec)
}

fn parse_request(operation: &str, payload: &str) -> Result<Request, Failure> {
    if !matches!(
        operation,
        "generate-intents" | "stream-intents" | "stream-event-intents"
    ) {
        return Err(Failure::Input);
    }
    let parsed = loads(payload).map_err(map_input_codec)?;
    let mut fields = parsed.into_object().ok_or(Failure::Input)?;
    let collection_name = if operation == "stream-event-intents" {
        "events"
    } else {
        "intents"
    };
    if fields.len() != 2
        || !fields.iter().any(|(name, _)| name == "request_id")
        || !fields.iter().any(|(name, _)| name == collection_name)
    {
        return Err(Failure::Input);
    }
    let request_id = take_field(&mut fields, "request_id")
        .and_then(PyValue::into_string)
        .ok_or(Failure::Input)?;
    let collection = take_field(&mut fields, collection_name).ok_or(Failure::Input)?;
    if collection_name == "events"
        && !matches!(&collection, PyValue::Array(events) if events.iter().all(|event| event.as_object().is_some()))
    {
        return Err(Failure::Input);
    }
    Ok(Request {
        request_id,
        collection,
    })
}

fn normalize_stream_events(events: PyValue, request_id: &str) -> Result<PyValue, Failure> {
    let mut events = events.into_array().ok_or(Failure::Input)?;
    for event in &mut events {
        let intent = event
            .as_object()
            .filter(|fields| {
                get_field(fields, "type").and_then(PyValue::as_str) == Some("tool_intent_delta")
            })
            .and_then(|fields| get_field(fields, "tool_intent"))
            .filter(|value| value.as_object().is_some())
            .cloned();
        let Some(intent) = intent else {
            continue;
        };
        let normalized =
            normalize_intents(PyValue::Array(vec![intent]), request_id, STREAM_OPERATION)?;
        let replacement = normalized.into_iter().next().ok_or(Failure::Terminal)?;
        if !replace_field(event, "tool_intent", replacement) {
            return Err(Failure::Terminal);
        }
    }
    Ok(PyValue::Array(events))
}

fn normalize_intents(
    intents: PyValue,
    request_id: &str,
    operation: &str,
) -> Result<Vec<PyValue>, Failure> {
    let request = PyValue::object([
        ("request_id".into(), PyValue::String(request_id.into())),
        ("intents".into(), intents),
    ]);
    let response = bridge_call(BRIDGE_CONTRACT, operation, request)?;
    let fields = response.as_object().ok_or(Failure::Terminal)?;
    let values = get_field(fields, "intents")
        .and_then(PyValue::as_array)
        .ok_or(Failure::Terminal)?;
    let mut normalized = Vec::new();
    normalized
        .try_reserve(values.len())
        .map_err(|_| Failure::Resource)?;
    normalized.extend(
        values
            .iter()
            .filter(|value| value.as_object().is_some())
            .cloned(),
    );
    Ok(normalized)
}

fn bridge_call(contract: &str, operation: &str, request: PyValue) -> Result<PyValue, Failure> {
    if contract != BRIDGE_CONTRACT || !matches!(operation, GENERATE_OPERATION | STREAM_OPERATION) {
        return Err(Failure::Input);
    }
    let encoded = dumps(&request).map_err(map_terminal_codec)?;
    #[cfg(target_arch = "wasm32")]
    {
        let response = local::ai_tool_bridge_ordered_python::bridge::normalize(operation, &encoded)
            .map_err(Failure::Imported)?;
        loads(&response).map_err(map_terminal_codec)
    }
    #[cfg(not(target_arch = "wasm32"))]
    {
        let _ = (operation, encoded);
        Err(Failure::NativeUnavailable)
    }
}

fn map_input_codec(error: CodecError) -> Failure {
    match error {
        CodecError::Resource => Failure::Resource,
        _ => Failure::Input,
    }
}

fn map_terminal_codec(error: CodecError) -> Failure {
    match error {
        CodecError::Resource => Failure::Resource,
        _ => Failure::Terminal,
    }
}

fn take_field(fields: &mut Vec<(String, PyValue)>, key: &str) -> Option<PyValue> {
    fields
        .iter()
        .position(|(name, _)| name == key)
        .map(|index| fields.remove(index).1)
}

fn get_field<'a>(fields: &'a [(String, PyValue)], key: &str) -> Option<&'a PyValue> {
    fields
        .iter()
        .find(|(name, _)| name == key)
        .map(|(_, value)| value)
}

fn replace_field(value: &mut PyValue, key: &str, replacement: PyValue) -> bool {
    if let PyValue::Object(fields) = value
        && let Some((_, current)) = fields.iter_mut().find(|(name, _)| name == key)
    {
        *current = replacement;
        return true;
    }
    false
}
