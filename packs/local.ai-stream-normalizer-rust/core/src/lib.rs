//! Pure Gateway stream event projection over ordered finite JSON.

use ordered_python_compat::{
    CodecError, CoercionError, MAX_BYTES, MAX_NODES, PyValue, dumps_bounded, loads, pyint, pystr,
    pystrip, pytruthy,
};

const ALLOWED_TYPES: [&str; 6] = [
    "text_delta",
    "thinking_delta",
    "tool_intent_delta",
    "usage",
    "finish",
    "error",
];

const OPERATIONS: [&str; 3] = [
    "normalize",
    "validate",
    "rumi_ai_stream_pack.ai-stream-normalize",
];

/// Failure categories from the standalone stream normalizer adapter.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Failure {
    /// The operation or the input JSON envelope is invalid.
    Input,
    /// Provider stream validation or argument coercion failed.
    Validation,
    /// A numeric operation overflowed or produced a nonfinite value.
    Arithmetic,
    /// The projected output falls outside the transport domain.
    Output,
    /// The codec or allocator reached a resource limit.
    Limit,
}

/// Normalize the original request envelope and encode the projected events within the byte limit.
pub fn normalize(
    operation: &str,
    payload: &str,
    maximum_output_bytes: usize,
) -> Result<String, Failure> {
    if !OPERATIONS.contains(&operation) {
        return Err(Failure::Input);
    }
    let maximum_output_bytes = maximum_output_bytes.min(MAX_BYTES);

    let parsed = loads(payload).map_err(map_input_error)?;
    let mut request = parsed.into_object().ok_or(Failure::Input)?;

    let request_id_value = take_field(&mut request, "request_id");
    let request_id = match request_id_value.as_ref().filter(|value| pytruthy(value)) {
        Some(value) => pystr(value),
        None => String::new(),
    };
    let request_id = pystrip(&request_id).to_owned();

    let attempt_value = take_field(&mut request, "provider_attempt");
    let attempt_value = match attempt_value.filter(pytruthy) {
        Some(value) => value,
        None => PyValue::Int("1".into()),
    };
    let provider_attempt = pyint(&attempt_value).map_err(map_int_error)?;
    if request_id.is_empty() {
        return Err(Failure::Validation);
    }

    let value = take_field(&mut request, "value").unwrap_or(PyValue::Null);
    let events = if matches!(&value, PyValue::Array(_)) {
        value.into_array().ok_or(Failure::Validation)?
    } else if matches!(&value, PyValue::Object(_)) {
        let mut fields = value.into_object().ok_or(Failure::Validation)?;
        take_field(&mut fields, "events")
            .and_then(PyValue::into_array)
            .ok_or(Failure::Validation)?
    } else {
        return Err(Failure::Validation);
    };

    // The output always has a three-node envelope plus 19 nodes per projected
    // event. Also bound repeated copies of caller-controlled request_id before
    // materializing a larger tree. We still validate every event before
    // returning an output-limit error, matching the Python adapter's order.
    let node_limit_exceeded = events.len() > (MAX_NODES - 3) / 19;
    let repeated_scalar_bytes = request_id
        .len()
        .checked_add(provider_attempt.len())
        .and_then(|bytes| bytes.checked_mul(events.len()));
    let output_limit_exceeded = node_limit_exceeded
        || repeated_scalar_bytes.is_none_or(|bytes| bytes > maximum_output_bytes);

    let mut normalized = Vec::new();
    if !output_limit_exceeded {
        normalized
            .try_reserve_exact(events.len())
            .map_err(|_| Failure::Limit)?;
    }
    let event_count = events.len();
    let mut finished = false;

    for (sequence, event) in events.into_iter().enumerate() {
        let mut fields = event.into_object().ok_or(Failure::Validation)?;

        let type_value = take_field(&mut fields, "type").unwrap_or(PyValue::Null);
        let type_value = if pytruthy(&type_value) {
            type_value
        } else {
            PyValue::String(String::new())
        };
        let event_type = pystr(&type_value);
        if !ALLOWED_TYPES.contains(&event_type.as_str()) || finished {
            return Err(Failure::Validation);
        }

        if !output_limit_exceeded {
            let mut projected = Vec::new();
            projected.try_reserve_exact(9).map_err(|_| Failure::Limit)?;
            projected.push(("request_id".into(), PyValue::String(request_id.clone())));
            projected.push(("sequence".into(), PyValue::Int(sequence.to_string())));
            projected.push(("type".into(), PyValue::String(event_type.clone())));
            projected.push((
                "delta".into(),
                take_field(&mut fields, "delta").unwrap_or(PyValue::Null),
            ));
            projected.push((
                "tool_intent".into(),
                take_field(&mut fields, "tool_intent").unwrap_or(PyValue::Null),
            ));
            projected.push((
                "usage".into(),
                take_field(&mut fields, "usage").unwrap_or(PyValue::Null),
            ));
            projected.push((
                "finish_reason".into(),
                take_field(&mut fields, "finish_reason").unwrap_or(PyValue::Null),
            ));
            projected.push((
                "error_code".into(),
                take_field(&mut fields, "error_code").unwrap_or(PyValue::Null),
            ));
            projected.push((
                "provider_attempt".into(),
                PyValue::Int(provider_attempt.clone()),
            ));
            normalized.push(PyValue::Object(projected));
        }
        finished = event_type == "finish" || event_type == "error";
    }

    if event_count == 0 || !finished {
        return Err(Failure::Validation);
    }
    if output_limit_exceeded {
        return Err(Failure::Output);
    }

    dumps_bounded(
        &PyValue::object([("events".into(), PyValue::Array(normalized))]),
        maximum_output_bytes,
    )
    .map_err(map_output_error)
}

fn take_field(fields: &mut Vec<(String, PyValue)>, name: &str) -> Option<PyValue> {
    let index = fields.iter().position(|(key, _)| key == name)?;
    Some(fields.swap_remove(index).1)
}

fn map_int_error(error: CoercionError) -> Failure {
    match error {
        CoercionError::Overflow => Failure::Arithmetic,
        CoercionError::Type | CoercionError::Value => Failure::Validation,
    }
}

fn map_input_error(error: CodecError) -> Failure {
    match error {
        CodecError::Resource => Failure::Limit,
        _ => Failure::Input,
    }
}

fn map_output_error(error: CodecError) -> Failure {
    match error {
        CodecError::NonFinite => Failure::Arithmetic,
        CodecError::Resource => Failure::Limit,
        CodecError::Invalid
        | CodecError::ByteLimit
        | CodecError::NodeLimit
        | CodecError::IntegerLimit => Failure::Output,
    }
}

#[cfg(test)]
mod tests {
    use super::{CodecError, Failure, map_input_error, map_output_error};

    #[test]
    fn output_codec_failures_follow_the_original_adapter_categories() {
        for error in [
            CodecError::Invalid,
            CodecError::ByteLimit,
            CodecError::NodeLimit,
            CodecError::IntegerLimit,
        ] {
            assert_eq!(map_output_error(error), Failure::Output);
        }
        assert_eq!(map_output_error(CodecError::NonFinite), Failure::Arithmetic);
        assert_eq!(map_output_error(CodecError::Resource), Failure::Limit);
        assert_eq!(map_input_error(CodecError::Resource), Failure::Limit);
        assert_eq!(map_input_error(CodecError::ByteLimit), Failure::Input);
    }
}
