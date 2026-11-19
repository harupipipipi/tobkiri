//! Pure CPython-compatible tool intent projection over ordered JSON.

#[cfg(test)]
use ordered_python_compat::dumps;
use ordered_python_compat::{
    CodecError, MAX_NODES, PyValue, PythonJson, dumps_python_json, dumps_python_json_bounded,
    loads, loads_python_json, pystr, pystrip, pytruthy,
};

const PREFIX: &str = "rumi_ai_tool_bridge_pack.ai-tool-intent-normalize";
const MAX_GATEWAY_BYTES: usize = 1024 * 1024;

/// Closed original v2 error categories, selected at the original operation stage.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ProviderFailure {
    /// The request or projection falls outside the standalone adapter's domain.
    Invalid,
    /// The projection contains a nonfinite number.
    NonFinite,
    /// The standalone adapter could not allocate memory.
    Resource,
    #[cfg(test)]
    HistoricalV1Invalid,
}
impl ProviderFailure {
    /// Return the standalone WIT error code.
    pub const fn code(self) -> &'static str {
        match self {
            Self::Invalid => "invalid",
            Self::NonFinite => "internal",
            Self::Resource => "limit-exceeded",
            #[cfg(test)]
            Self::HistoricalV1Invalid => "invalid",
        }
    }
    /// Return the standalone WIT error message.
    pub const fn message(self) -> &'static str {
        match self {
            Self::Invalid => "outside ordered-finite-json-v2 domain",
            Self::NonFinite => "provider output contains a non-finite number",
            Self::Resource => "ordered tool bridge resource limit exceeded",
            #[cfg(test)]
            Self::HistoricalV1Invalid => "outside ordered-finite-json-v1 domain",
        }
    }
}

fn input_error(error: CodecError) -> ProviderFailure {
    match error {
        CodecError::Resource => ProviderFailure::Resource,
        _ => ProviderFailure::Invalid,
    }
}

fn output_error(error: CodecError) -> ProviderFailure {
    match error {
        CodecError::NonFinite => ProviderFailure::NonFinite,
        CodecError::Resource => ProviderFailure::Resource,
        _ => ProviderFailure::Invalid,
    }
}

/// Project one request using the original registered-operation mapping. Nested
/// authority-like fields remain ordinary DATA; this function invokes no Host.
pub fn invoke(operation: &str, payload: &str) -> Result<String, ProviderFailure> {
    if !registered_operation(operation) {
        return Err(ProviderFailure::Invalid);
    }
    let value = loads(payload).map_err(input_error)?;
    let result = project(value)?;
    dumps_python_json(&result).map_err(output_error)
}

/// Project the Gateway's tool intent envelope within its 1 MiB ABI.
/// The caller uses the original Host provider when the projection is unavailable.
pub fn normalize_gateway_tool_intents(payload: &str) -> Option<String> {
    bounded_gateway_structure(payload)?;
    let (request_id, values) = take_request(loads(payload).ok()?).ok()?;
    let request_id = pystrip(&request_id);
    let mut output = String::from(r#"{"intents":["#);
    let closing = "]}";
    for (index, value) in values.into_iter().enumerate() {
        let separator = if index == 0 { "" } else { "," };
        let remaining =
            MAX_GATEWAY_BYTES.checked_sub(output.len() + separator.len() + closing.len())?;
        if request_id.len() > remaining {
            return None;
        }
        let projected = normalize(value.into_object()?, request_id, index, |text| {
            bounded_gateway_structure(text).ok_or(ProviderFailure::Resource)?;
            loads_python_json(text).map_err(input_error)
        })
        .ok()?;
        let encoded = dumps_python_json_bounded(&projected, remaining).ok()?;
        output
            .try_reserve(separator.len() + encoded.len() + closing.len())
            .ok()?;
        output.push_str(separator);
        output.push_str(&encoded);
    }
    output.push_str(closing);
    Some(output)
}

fn bounded_gateway_structure(text: &str) -> Option<()> {
    // Leave room for the callers around CPython's recursive JSON decoder and repr.
    // This scan also bounds allocations in the standalone opaque parser.
    const MAX_DEPTH: usize = 128;
    if text.len() > MAX_GATEWAY_BYTES {
        return None;
    }
    let mut depth = 0usize;
    let mut structures = 1;
    let mut quoted = false;
    let mut escaped = false;
    for byte in text.bytes() {
        if quoted {
            if escaped {
                escaped = false;
            } else if byte == b'\\' {
                escaped = true;
            } else if byte == b'"' {
                quoted = false;
            }
            continue;
        }
        match byte {
            b'"' => quoted = true,
            b'[' | b'{' => {
                depth += 1;
                structures += 1;
            }
            b']' | b'}' => depth = depth.checked_sub(1)?,
            b',' | b':' => structures += 1,
            _ => {}
        }
        if depth > MAX_DEPTH || structures > MAX_NODES {
            return None;
        }
    }
    Some(())
}

fn registered_operation(operation: &str) -> bool {
    operation == PREFIX
        || operation == "rumi_ai_tool_bridge_pack.ai-tool-intent-normalize.generate"
        || operation == "rumi_ai_tool_bridge_pack.ai-tool-intent-normalize.stream"
}

fn take_field(fields: &mut Vec<(String, PyValue)>, key: &str) -> Option<PyValue> {
    let index = fields.iter().position(|(name, _)| name == key)?;
    Some(fields.swap_remove(index).1)
}

fn copy_text(value: &str) -> Result<String, ProviderFailure> {
    let mut result = String::new();
    result
        .try_reserve_exact(value.len())
        .map_err(|_| ProviderFailure::Resource)?;
    result.push_str(value);
    Ok(result)
}

fn take_request(request: PyValue) -> Result<(String, Vec<PyValue>), ProviderFailure> {
    let mut request = request.into_object().ok_or(ProviderFailure::Invalid)?;
    // The original treats every non-list intents value as an empty list.
    let values = take_field(&mut request, "intents")
        .and_then(PyValue::into_array)
        .unwrap_or_default();
    let request_id = take_field(&mut request, "request_id")
        .filter(pytruthy)
        .map(|value| pystr(&value))
        .unwrap_or_default();
    Ok((request_id, values))
}

fn project(request: PyValue) -> Result<PythonJson, ProviderFailure> {
    let (request_id, values) = take_request(request)?;
    let request_id = pystrip(&request_id);
    let original_count = values.len();
    let mut normalized = Vec::new();
    normalized
        .try_reserve(original_count)
        .map_err(|_| ProviderFailure::Resource)?;
    for (index, value) in values.into_iter().enumerate() {
        // The Python comprehension skips non-mappings but still normalizes
        // later mappings before it performs its final count check.
        let Some(fields) = value.into_object() else {
            continue;
        };
        normalized.push(normalize(fields, request_id, index, |text| {
            loads_python_json(text).map_err(input_error)
        })?);
    }
    if normalized.len() != original_count {
        return Err(ProviderFailure::Invalid);
    }
    Ok(PythonJson::object([(
        "intents".into(),
        PythonJson::array(normalized),
    )]))
}

fn normalize(
    mut value: Vec<(String, PyValue)>,
    request_id: &str,
    index: usize,
    parse_arguments: impl FnOnce(&str) -> Result<PythonJson, ProviderFailure>,
) -> Result<PythonJson, ProviderFailure> {
    let field = |key: &str| {
        value
            .iter()
            .find(|(name, _)| name == key)
            .map(|(_, value)| value)
    };
    let function = field("function").filter(|value| value.as_object().is_some());
    let name = function
        .and_then(|function| function.get("name"))
        .filter(|value| pytruthy(value))
        .or_else(|| field("name").filter(|value| pytruthy(value)))
        .map(pystr)
        .unwrap_or_default();
    let name = pystrip(&name);
    if !valid_tool_name(name) {
        return Err(ProviderFailure::Invalid);
    }
    let name = copy_text(name)?;
    // Presence controls arguments fallback; an explicit null does not select
    // the outer arguments, even though names use Python truthiness above.
    let function = take_field(&mut value, "function").and_then(PyValue::into_object);
    let arguments = function
        .and_then(|mut fields| take_field(&mut fields, "arguments"))
        .or_else(|| take_field(&mut value, "arguments"))
        .unwrap_or_else(|| PyValue::Object(Vec::new()));
    let arguments = match arguments.as_str() {
        Some(text) => parse_arguments(text)?,
        None => arguments.into(),
    };
    if !arguments.is_object() {
        return Err(ProviderFailure::Invalid);
    }
    let intent_id = match take_field(&mut value, "id").filter(pytruthy) {
        Some(value) => pystr(&value),
        None => {
            let suffix = format!(":tool:{index}");
            let mut value = copy_text(request_id)?;
            value
                .try_reserve(suffix.len())
                .map_err(|_| ProviderFailure::Resource)?;
            value.push_str(&suffix);
            value
        }
    };
    Ok(PythonJson::object([
        ("intent_id".into(), PyValue::String(intent_id).into()),
        (
            "request_id".into(),
            PyValue::String(copy_text(request_id)?).into(),
        ),
        ("operation".into(), PyValue::String(name).into()),
        ("arguments".into(), arguments),
        ("authority_granted".into(), PyValue::Bool(false).into()),
        ("approved".into(), PyValue::Bool(false).into()),
        (
            "approval_status".into(),
            PyValue::String("unrequested".into()).into(),
        ),
        ("executes".into(), PyValue::Bool(false).into()),
    ]))
}

fn valid_tool_name(value: &str) -> bool {
    let bytes = value.as_bytes();
    (1..=200).contains(&bytes.len())
        && bytes[0].is_ascii_alphanumeric()
        && bytes
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'))
}

#[cfg(test)]
mod historical_tests;
