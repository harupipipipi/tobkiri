//! Plan a message and saved receipt as one replacement of the original owner document.

use super::{
    legacy_append_context, legacy_append_receipt,
    legacy_read::{
        MAX_BYTES, ReadError, decode_document, encode_owner_mutation, field, normalize, set_field,
        validate_identifier, validate_profile,
    },
    old_message_lifecycle,
};
use ordered_python_compat::{CodecError, PyValue, loads, pyint, pystr, pystrip, pytruthy};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum AppendError {
    Read(ReadError),
    Invalid,
    Compatibility,
    Conflict,
    NotFound,
    Limit,
}

impl From<ReadError> for AppendError {
    fn from(error: ReadError) -> Self {
        Self::Read(error)
    }
}

/// Evaluate the locked document. The verified native caller alone can publish
/// the result, and rechecks the existing approval at physical replacement.
pub(super) fn append(
    profile: &str,
    document: &str,
    request: &str,
    mut now_ms: impl FnMut() -> Result<i64, AppendError>,
) -> Result<String, AppendError> {
    validate_profile(profile)?;
    if request.len() > MAX_BYTES {
        return Err(AppendError::Limit);
    }
    let values = loads(request).map_err(|error| match error {
        CodecError::ByteLimit | CodecError::NodeLimit => AppendError::Limit,
        _ => AppendError::Compatibility,
    })?;
    let fields = values.as_object().ok_or(AppendError::Invalid)?;
    if fields.len() != 4 {
        return Err(AppendError::Invalid);
    }
    let conversation_id = identifier(required(&values, "conversation_id")?)?;
    let revision = positive_revision(required(&values, "expected_conversation_revision")?)?;
    let initial = required(&values, "saved_input")?;
    let saved_request = required(initial, "request")?;
    let turn_id = required(saved_request, "turn_id")?
        .as_str()
        .ok_or(AppendError::Invalid)?;
    validate_identifier(turn_id)?;
    let mut message = normalize_message(required(&values, "message")?, &mut now_ms)?;
    let mut state = decode_document(document, profile, None)?;
    let store_revision = next_revision(stored_revision(field(&mut state, "revision").as_deref())?)?;
    // Take disjoint owner collections so the ordinary context check can borrow
    // the target and a parent row from this same already normalized snapshot.
    let mut conversations = field(&mut state, "conversations")
        .ok_or(ReadError::InvalidDocument)?
        .take();
    normalize_all(&mut conversations)?;
    let current = conversations
        .get(&conversation_id)
        .filter(|value| value.as_object().is_some())
        .ok_or(AppendError::NotFound)?;
    if stored_revision(current.get("conversation_revision"))? != revision {
        return Err(AppendError::Conflict);
    }
    legacy_append_context::verify(profile, current, saved_request, &conversations)?;
    let PyValue::Object(conversation_fields) = &mut conversations else {
        return Err(ReadError::InvalidDocument.into());
    };
    let Some(PyValue::Object(current)) = field(conversation_fields, &conversation_id) else {
        return Err(AppendError::NotFound);
    };
    let mut messages = take_messages(current)?;
    let id = message
        .get("id")
        .and_then(PyValue::as_str)
        .ok_or(AppendError::Invalid)?
        .to_owned();
    for prior in &messages {
        if prior.as_object().is_none() {
            return Err(AppendError::Compatibility);
        }
        if prior.get("id").and_then(PyValue::as_str) == Some(id.as_str()) {
            return Err(AppendError::Conflict);
        }
    }
    if let Some(parent) = message
        .get("parent_id")
        .filter(|value| !matches!(value, PyValue::Null))
    {
        let parent = identifier(parent)?;
        link_parent(&mut messages, &parent, &id)?;
    }
    let PyValue::Object(message_fields) = &mut message else {
        return Err(AppendError::Invalid);
    };
    let sequence = i64::try_from(messages.len()).map_err(|_| AppendError::Limit)?;
    set_field(message_fields, "sequence", number(sequence));
    set_field(
        message_fields,
        "sequence_number",
        number(next_revision(sequence)?),
    );

    let now = now_ms()?;
    let receipts = receipt_inventory(&mut state)?;
    let previous = receipts
        .iter()
        .find(|(key, _)| key == turn_id)
        .map(|(_, value)| value);
    if previous.is_none() && receipts.len() >= 10_000 {
        return Err(AppendError::Conflict);
    }
    let receipt =
        legacy_append_receipt::append(previous, initial, &conversation_id, revision, &message)?;
    let verified = receipt
        .get("result_reference")
        .is_some()
        .then_some(&receipt);
    old_message_lifecycle::record(current, &message, &messages, now, verified)?;
    set_field(receipts, turn_id, receipt);

    let updated_at = now_ms()?;
    let conversation_revision = next_revision(revision)?;
    let result = PyValue::Object(vec![
        ("action".into(), text("message_appended")),
        ("message".into(), message.clone()),
        (
            "conversation_revision".into(),
            number(conversation_revision),
        ),
        ("store_revision".into(), number(store_revision)),
    ]);
    messages.push(message);
    set_field(current, "messages", PyValue::Array(messages));
    set_field(current, "current_node_id", PyValue::String(id));
    set_field(current, "updated_at", number(updated_at));
    set_field(
        current,
        "conversation_revision",
        number(conversation_revision),
    );
    set_field(&mut state, "conversations", conversations);
    set_field(&mut state, "revision", number(store_revision));
    encode_owner_mutation(&result, Some(PyValue::Object(state))).map_err(|_| AppendError::Limit)
}

fn normalize_all(value: &mut PyValue) -> Result<(), AppendError> {
    let PyValue::Object(rows) = value else {
        return Err(ReadError::InvalidDocument.into());
    };
    for (_, row) in rows {
        if let PyValue::Object(row) = row {
            normalize(row);
        }
    }
    Ok(())
}

fn take_messages(current: &mut [(String, PyValue)]) -> Result<Vec<PyValue>, AppendError> {
    let Some(value) = field(current, "messages").filter(|value| pytruthy(value)) else {
        return Ok(Vec::new());
    };
    value.take().into_array().ok_or(AppendError::Compatibility)
}

fn normalize_message(
    message: &PyValue,
    now_ms: &mut impl FnMut() -> Result<i64, AppendError>,
) -> Result<PyValue, AppendError> {
    let id = identifier(required(message, "id")?)?;
    for key in ["sequence", "sequence_number"] {
        if let Some(value) = truthy(message, key) {
            pyint(value).map_err(|_| AppendError::Invalid)?;
        }
    }
    let created_at = match truthy(message, "created_at").or_else(|| truthy(message, "timestamp")) {
        Some(value) => PyValue::Int(pyint(value).map_err(|_| AppendError::Invalid)?),
        None => number(now_ms()?),
    };
    let updated_at = truthy(message, "updated_at")
        .map(|value| {
            pyint(value)
                .map(PyValue::Int)
                .map_err(|_| AppendError::Invalid)
        })
        .transpose()?
        .unwrap_or_else(|| created_at.clone());

    let text_or = |name, default| {
        truthy(message, name)
            .map(|value| PyValue::String(pystr(value)))
            .unwrap_or_else(|| text(default))
    };
    let copy_or = |name, default| truthy(message, name).cloned().unwrap_or(default);
    let field_or_null = |name| message.get(name).cloned().unwrap_or(PyValue::Null);
    Ok(PyValue::Object(vec![
        ("id".into(), PyValue::String(id)),
        ("role".into(), text_or("role", "user")),
        ("content".into(), copy_or("content", text(""))),
        ("parts".into(), copy_or("parts", PyValue::Array(Vec::new()))),
        ("status".into(), text_or("status", "complete")),
        ("created_at".into(), created_at),
        ("updated_at".into(), updated_at),
        ("sequence".into(), number(0)),
        (
            "metadata".into(),
            copy_or("metadata", PyValue::Object(Vec::new())),
        ),
        ("parent_id".into(), field_or_null("parent_id")),
        ("children_ids".into(), PyValue::Array(Vec::new())),
        ("sequence_number".into(), number(1)),
        ("raw_text".into(), text_or("raw_text", "")),
        ("finish_reason".into(), field_or_null("finish_reason")),
        ("usage".into(), field_or_null("usage")),
        ("widget".into(), field_or_null("widget")),
        ("events".into(), field_or_null("events")),
        ("tool_logs".into(), field_or_null("tool_logs")),
    ]))
}

fn link_parent(messages: &mut [PyValue], parent: &str, child: &str) -> Result<(), AppendError> {
    let mut parents = messages
        .iter_mut()
        .filter(|candidate| candidate.get("id").and_then(PyValue::as_str) == Some(parent));
    let Some(PyValue::Object(parent)) = parents.next() else {
        return Err(AppendError::NotFound);
    };
    if parents.next().is_some() {
        // Older malformed files can contain duplicate rows. Python clones the
        // first match over every duplicate; preserve that legacy boundary.
        return Err(AppendError::Compatibility);
    }
    let mut children = match field(parent, "children_ids").filter(|value| pytruthy(value)) {
        Some(value) => value
            .take()
            .into_array()
            .ok_or(AppendError::Compatibility)?,
        None => Vec::new(),
    };
    if !children.iter().any(|id| id.as_str() == Some(child)) {
        children.push(text(child));
    }
    set_field(parent, "children_ids", PyValue::Array(children));
    Ok(())
}

fn receipt_inventory(
    state: &mut Vec<(String, PyValue)>,
) -> Result<&mut Vec<(String, PyValue)>, AppendError> {
    if field(state, "saved_receipts").is_none() {
        state.push(("saved_receipts".into(), PyValue::Object(Vec::new())));
    }
    match field(state, "saved_receipts") {
        Some(PyValue::Object(receipts)) => Ok(receipts),
        _ => Err(AppendError::Compatibility),
    }
}

fn truthy<'a>(value: &'a PyValue, name: &str) -> Option<&'a PyValue> {
    value.get(name).filter(|value| pytruthy(value))
}

fn required<'a>(value: &'a PyValue, name: &str) -> Result<&'a PyValue, AppendError> {
    value.get(name).ok_or(AppendError::Invalid)
}

fn identifier(value: &PyValue) -> Result<String, AppendError> {
    let value = pystr(value);
    let value = pystrip(&value);
    validate_identifier(value)?;
    Ok(value.to_owned())
}

fn stored_revision(value: Option<&PyValue>) -> Result<i64, AppendError> {
    match value {
        Some(PyValue::Int(value)) => value.parse().map_err(|_| AppendError::Compatibility),
        _ => Err(AppendError::Compatibility),
    }
}

fn positive_revision(value: &PyValue) -> Result<i64, AppendError> {
    if !matches!(value, PyValue::Int(_)) {
        return Err(AppendError::Invalid);
    }
    let revision = stored_revision(Some(value))?;
    if revision < 1 {
        return Err(AppendError::Invalid);
    }
    Ok(revision)
}

fn next_revision(value: i64) -> Result<i64, AppendError> {
    value.checked_add(1).ok_or(AppendError::Compatibility)
}

fn text(value: &str) -> PyValue {
    PyValue::String(value.to_owned())
}

fn number(value: i64) -> PyValue {
    PyValue::Int(value.to_string())
}

#[cfg(test)]
#[path = "legacy_message_append_tests.rs"]
mod tests;
