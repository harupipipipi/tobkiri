//! Content-free evidence for the original Conversation owner's saved append.

use super::legacy_message_append::AppendError;
use ordered_python_compat::{CodecError, PyValue, dumps_bounded, pytruthy};
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use tobkiri_canon::{CanonValue, Digest, Variant};
use tobkiri_saved_input::ValidationError;

const MAX_TOOL_CALLS: usize = 8;
const MAX_TOOL_TRACE_BYTES: usize = 40 * 1024;
const MAX_TOOL_ARGUMENT_BYTES: usize = 16 * 1024;
const MAX_TOOL_TRACE_DEPTH: usize = 12;

/// Bind one owner-normalized append to the complete immutable saved input.
pub(super) fn append(
    previous: Option<&PyValue>,
    initial: &PyValue,
    conversation_id: &str,
    expected_revision: i64,
    message: &PyValue,
) -> Result<PyValue, AppendError> {
    let validated = tobkiri_saved_input::validate(initial).map_err(|error| match error {
        ValidationError::Compatibility => AppendError::Compatibility,
        ValidationError::Invalid | ValidationError::Limit => AppendError::Invalid,
    })?;
    let request = validated.request;
    let input_digest = tobkiri_canon::digest(&validated.canonical, Variant::StrictV1)
        .map_err(|_| AppendError::Invalid)?;
    let turn_id = string(request, "turn_id").ok_or(AppendError::Invalid)?;
    let initial_revision =
        integer(request.get("conversation_revision")).ok_or(AppendError::Invalid)?;
    let user_id = message_id(conversation_id, turn_id, "user")?;
    let assistant_id = message_id(conversation_id, turn_id, "assistant")?;
    let role = string(message, "role");
    let empty_metadata = PyValue::Null;
    let message_metadata = message.get("metadata").unwrap_or(&empty_metadata);
    if pytruthy(message_metadata) && message_metadata.as_object().is_none() {
        return Err(AppendError::Invalid);
    }
    let mut metadata = vec![("turn_id", text(turn_id))];
    if let Some(references) = request
        .get("resolved_chat_references")
        .and_then(|snapshot| snapshot.get("references"))
    {
        metadata.push(("chat_references", references.clone()));
    }
    if let Some(mode) = request.get("action_approval_mode") {
        metadata.push(("action_approval_mode", mode.clone()));
    }
    let empty_trace = PyValue::Array(Vec::new());
    let trace = message_metadata
        .get("saved_tool_messages")
        .unwrap_or(&empty_trace);
    let logs = trace_logs(trace)?;
    if pytruthy(trace) {
        if role != Some("assistant")
            || request
                .get("tool_selection")
                .and_then(|selection| string(selection, "mode"))
                .unwrap_or("none")
                == "none"
        {
            return Err(AppendError::Invalid);
        }
        metadata.push(("saved_tool_messages", trace.clone()));
    }
    let logs_match = match message.get("tool_logs") {
        None | Some(PyValue::Null) => logs.is_empty(),
        Some(actual) => canonical(actual, tobkiri_canon::MAX_BYTES)? == CanonValue::Arr(logs),
    };
    let expected_id = match role {
        Some("user") => user_id.as_str(),
        Some("assistant") => assistant_id.as_str(),
        _ => return Err(AppendError::Invalid),
    };
    if !logs_match
        || string(request, "conversation_id") != Some(conversation_id)
        || string(message, "id") != Some(expected_id)
        || string(message, "status") != Some("complete")
        || canonical(message_metadata, tobkiri_canon::MAX_BYTES)?
            != canonical(&object(metadata), tobkiri_canon::MAX_BYTES)?
    {
        return Err(AppendError::Invalid);
    }

    let identity = [
        ("turn_id", text(turn_id)),
        ("conversation_id", text(conversation_id)),
        ("input_digest", text(input_digest.as_str())),
        ("initial_revision", number(initial_revision)),
        ("user_message_id", text(&user_id)),
        ("assistant_message_id", text(&assistant_id)),
    ];
    let previous = previous.filter(|value| !matches!(value, PyValue::Null));
    let revision = expected_revision
        .checked_add(1)
        .ok_or(AppendError::Compatibility)?;
    if role == Some("user") {
        let content = message.get("content").ok_or(AppendError::Invalid)?;
        let initial_content = request.get("content").ok_or(AppendError::Invalid)?;
        if previous.is_some()
            || expected_revision != initial_revision
            || (content != initial_content
                && canonical(content, tobkiri_canon::MAX_BYTES)?
                    != canonical(initial_content, tobkiri_canon::MAX_BYTES)?)
        {
            return Err(AppendError::Invalid);
        }
        return Ok(object(
            identity
                .into_iter()
                .chain([("user_revision", number(revision))]),
        ));
    }

    let previous = previous.ok_or(AppendError::Invalid)?;
    if previous.as_object().map(<[_]>::len) != Some(identity.len() + 1)
        || integer(previous.get("user_revision")) != Some(expected_revision)
        || string(message, "parent_id") != Some(user_id.as_str())
        || !message
            .get("content")
            .is_some_and(|content| pytruthy(content) && text_content(content))
    {
        return Err(AppendError::Invalid);
    }
    for (key, value) in &identity {
        let stored = previous.get(key).ok_or(AppendError::Invalid)?;
        if *key == "initial_revision" && matches!(stored, PyValue::Bool(_) | PyValue::Float(_)) {
            // Python compares the identity fields by dict equality, where
            // booleans and floats may equal integers. Defer these legacy
            // receipts before publishing with a different scalar policy.
            return Err(AppendError::Compatibility);
        }
        if stored != value {
            return Err(AppendError::Invalid);
        }
    }
    let mut next = previous.clone().into_object().ok_or(AppendError::Invalid)?;
    let outcome = object([
        ("status", text("ok")),
        ("turn_id", text(turn_id)),
        ("conversation_id", text(conversation_id)),
        ("conversation_revision", number(revision)),
        ("user_message_id", text(&user_id)),
        ("message", message.clone()),
    ]);
    next.push((
        "result_reference".into(),
        object([
            ("conversation_id", text(conversation_id)),
            ("conversation_revision", number(revision)),
            ("user_message_id", text(&user_id)),
            ("assistant_message_id", text(&assistant_id)),
            ("outcome_digest", text(digest(&outcome)?.as_str())),
        ]),
    ));
    Ok(PyValue::Object(next))
}

fn message_id(conversation_id: &str, turn_id: &str, role: &str) -> Result<String, AppendError> {
    let value = PyValue::Array(vec![text(conversation_id), text(turn_id), text(role)]);
    Ok(format!("message:{}", digest(&value)?.hex()))
}

fn trace_logs(trace: &PyValue) -> Result<Vec<CanonValue>, AppendError> {
    let trace_items = trace.as_array().ok_or(AppendError::Invalid)?;
    if trace_items.len() > 2 * MAX_TOOL_CALLS {
        return Err(AppendError::Invalid);
    }
    canonical(trace, MAX_TOOL_TRACE_BYTES)?;
    if !within_depth(trace, MAX_TOOL_TRACE_DEPTH) {
        return Err(AppendError::Invalid);
    }
    let mut calls = VecDeque::new();
    let mut seen = BTreeSet::new();
    let mut logs = Vec::new();
    for message in trace_items {
        match string(message, "role") {
            Some("assistant") => {
                if !calls.is_empty()
                    || !has_fields(message, &["role", "content", "tool_calls"])
                    || !message.get("content").is_some_and(text_content)
                {
                    return Err(AppendError::Invalid);
                }
                let items = message
                    .get("tool_calls")
                    .and_then(PyValue::as_array)
                    .filter(|items| !items.is_empty() && items.len() <= MAX_TOOL_CALLS - seen.len())
                    .ok_or(AppendError::Invalid)?;
                for item in items {
                    if !has_fields(item, &["id", "type", "function"])
                        || string(item, "type") != Some("function")
                    {
                        return Err(AppendError::Invalid);
                    }
                    let id = string(item, "id")
                        .filter(|id| identifier(id, true) && seen.insert(*id))
                        .ok_or(AppendError::Invalid)?;
                    let function = item.get("function").ok_or(AppendError::Invalid)?;
                    if !has_fields(function, &["name", "arguments"]) {
                        return Err(AppendError::Invalid);
                    }
                    let name = string(function, "name")
                        .filter(|name| identifier(name, true))
                        .ok_or(AppendError::Invalid)?;
                    let arguments = string(function, "arguments")
                        .filter(|args| args.len() <= MAX_TOOL_ARGUMENT_BYTES)
                        .ok_or(AppendError::Invalid)?;
                    let arguments = tobkiri_canon::parse(arguments.as_bytes(), Variant::StrictV1)
                        .map_err(|_| AppendError::Invalid)?;
                    if !matches!(arguments, CanonValue::Obj(_)) {
                        return Err(AppendError::Invalid);
                    }
                    calls.push_back((id, name, arguments));
                }
            }
            Some("tool") if has_fields(message, &["role", "tool_call_id", "content"]) => {
                let (id, name, arguments) = calls.pop_front().ok_or(AppendError::Invalid)?;
                let content = string(message, "content").ok_or(AppendError::Invalid)?;
                if string(message, "tool_call_id") != Some(id) {
                    return Err(AppendError::Invalid);
                }
                logs.push(CanonValue::Obj(BTreeMap::from([
                    ("tool_name".into(), CanonValue::Str(name.into())),
                    ("tool_call_id".into(), CanonValue::Str(id.into())),
                    ("arguments".into(), arguments),
                    ("result".into(), CanonValue::Str(content.into())),
                ])));
            }
            _ => return Err(AppendError::Invalid),
        }
    }
    if !calls.is_empty() {
        return Err(AppendError::Invalid);
    }
    Ok(logs)
}

fn within_depth(value: &PyValue, remaining: usize) -> bool {
    match value {
        PyValue::Array(items) => items.iter().all(|item| {
            remaining
                .checked_sub(1)
                .is_some_and(|depth| within_depth(item, depth))
        }),
        PyValue::Object(items) => items.iter().all(|(_, item)| {
            remaining
                .checked_sub(1)
                .is_some_and(|depth| within_depth(item, depth))
        }),
        _ => true,
    }
}

fn text_content(content: &PyValue) -> bool {
    content.as_str().is_some()
        || content
            .as_array()
            .is_some_and(|parts| parts.iter().all(|part| text_part(part).is_some()))
}

fn text_part(part: &PyValue) -> Option<&str> {
    if !has_fields(part, &["type", "text"]) || string(part, "type") != Some("text") {
        return None;
    }
    string(part, "text")
}

fn canonical(value: &PyValue, maximum: usize) -> Result<CanonValue, AppendError> {
    let encoded = dumps_bounded(value, maximum).map_err(|error| match error {
        CodecError::NodeLimit | CodecError::Resource => AppendError::Compatibility,
        _ => AppendError::Invalid,
    })?;
    tobkiri_canon::parse(encoded.as_bytes(), Variant::StrictV1).map_err(|_| AppendError::Invalid)
}

fn digest(value: &PyValue) -> Result<Digest, AppendError> {
    tobkiri_canon::digest(
        &canonical(value, tobkiri_canon::MAX_BYTES)?,
        Variant::StrictV1,
    )
    .map_err(|_| AppendError::Invalid)
}

fn identifier(value: &str, allow_slash: bool) -> bool {
    let bytes = value.as_bytes();
    bytes.len() <= 256
        && bytes.first().is_some_and(u8::is_ascii_alphanumeric)
        && bytes.iter().all(|byte| {
            byte.is_ascii_alphanumeric() || b"._:-".contains(byte) || (allow_slash && *byte == b'/')
        })
}

fn has_fields(value: &PyValue, keys: &[&str]) -> bool {
    value.as_object().map(<[_]>::len) == Some(keys.len())
        && keys.iter().all(|key| value.get(key).is_some())
}

fn string<'a>(value: &'a PyValue, key: &str) -> Option<&'a str> {
    value.get(key).and_then(PyValue::as_str)
}

fn integer(value: Option<&PyValue>) -> Option<i64> {
    match value {
        Some(PyValue::Int(value)) => value.parse().ok(),
        _ => None,
    }
}

fn object<'a>(fields: impl IntoIterator<Item = (&'a str, PyValue)>) -> PyValue {
    PyValue::object(fields.into_iter().map(|(key, value)| (key.into(), value)))
}

fn text(value: &str) -> PyValue {
    PyValue::String(value.into())
}

fn number(value: i64) -> PyValue {
    PyValue::Int(value.to_string())
}

#[cfg(test)]
mod tests;
