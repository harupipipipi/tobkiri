//! Plan the original Conversation owner's separate completion publication.

use super::legacy_read::{
    MAX_BYTES, ReadError, decode_document, field, normalize, set_field, validate_identifier,
    validate_profile,
};
use ordered_python_compat::{PyValue, dumps_bounded, dumps_spaced_document, loads, pystrip};
use tobkiri_canon::{CanonValue, Digest, Variant};

type Fields = Vec<(String, PyValue)>;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum ConfirmationError {
    Read(ReadError),
    Terminal,
    Receipt,
    Compatibility,
    Limit,
}

impl From<ReadError> for ConfirmationError {
    fn from(error: ReadError) -> Self {
        Self::Read(error)
    }
}

/// Evaluate the previous owner document and the independently read terminal
/// Turn. Only the trusted host can acquire the lock and publish the proposal.
pub(super) fn confirm(
    profile: &str,
    document: &str,
    turn: &str,
) -> Result<String, ConfirmationError> {
    validate_profile(profile)?;
    let turn = loads(turn).map_err(|_| ConfirmationError::Compatibility)?;
    let turn_id = identifier(&turn, "id")?;
    let conversation_id = identifier(&turn, "conversation_id")?;
    let request = object([("profile_id", text(profile)), ("turn_id", text(turn_id))]);
    let expected_request = format!("saved-turn.{}", canonical_digest(&request)?.hex());
    if string(&turn, "profile_id") != Some(profile)
        || string(&turn, "status") != Some("completed")
        || string(&turn, "request_id") != Some(expected_request.as_str())
        || !matches!(turn.get("revision"), Some(PyValue::Int(_)))
    {
        return Err(ConfirmationError::Terminal);
    }
    if integer(turn.get("revision"))? < 3 {
        return Err(ConfirmationError::Terminal);
    }
    let mut state = decode_document(document, profile, None)?;
    let (result, promote) = plan(&mut state, &turn, turn_id, conversation_id)?;
    let next = if promote {
        let Some(PyValue::Object(conversations)) = field(&mut state, "conversations") else {
            return Err(ReadError::InvalidDocument.into());
        };
        for (_, row) in conversations {
            if let PyValue::Object(row) = row {
                normalize(row);
            }
        }
        let mut document = PyValue::Object(state);
        sort_keys(&mut document);
        Some(dumps_spaced_document(&document, MAX_BYTES).map_err(|_| ConfirmationError::Limit)?)
    } else {
        None
    };
    let owner_result = dumps_bounded(&result, MAX_BYTES).map_err(|_| ConfirmationError::Limit)?;
    dumps_bounded(
        &object([
            ("owner_result", PyValue::String(owner_result)),
            ("next_document", next.map_or(PyValue::Null, PyValue::String)),
        ]),
        MAX_BYTES,
    )
    .map_err(|_| ConfirmationError::Limit)
}

fn plan(
    state: &mut Fields,
    turn: &PyValue,
    turn_id: &str,
    conversation_id: &str,
) -> Result<(PyValue, bool), ConfirmationError> {
    let (reference, receipt) = checked_receipt(state, turn, turn_id, conversation_id)?;
    let assistant_id = required_string(&receipt, "assistant_message_id")?;
    let user_id = required_string(&receipt, "user_message_id")?;
    let confirmed = object([
        ("turn_id", text(turn_id)),
        ("input_digest", required(&receipt, "input_digest")?.clone()),
        ("result_reference", reference.clone()),
    ]);
    let revision = integer(field(state, "revision").as_deref())?;
    let Some(PyValue::Object(conversations)) = field(state, "conversations") else {
        return Err(ReadError::InvalidDocument.into());
    };
    let Some(PyValue::Object(current)) = field(conversations, conversation_id) else {
        let append_revision = required(reference, "conversation_revision")?;
        return Ok((
            result(
                false,
                text(conversation_id),
                append_revision.clone(),
                revision,
            ),
            false,
        ));
    };
    normalize(current);
    let id = stored_field(current, "id")?.clone();
    let current_revision = stored_field(current, "conversation_revision")?.clone();
    let reply =
        |confirmed, revision| result(confirmed, id.clone(), current_revision.clone(), revision);
    let Some(PyValue::Object(lifecycle)) = field(current, "lifecycle") else {
        return Ok((reply(false, revision), false));
    };
    let lifecycle_state = lookup(lifecycle, "state").and_then(PyValue::as_str);
    if lifecycle_state == Some("completed")
        && lookup(lifecycle, "completion_message_id").and_then(PyValue::as_str)
            == Some(assistant_id)
        && same_evidence(lookup(lifecycle, "confirmed_completion"), &confirmed)?
    {
        return Ok((reply(true, revision), false));
    }
    if lifecycle_state != Some("running")
        || lookup(lifecycle, "active_user_message_id").and_then(PyValue::as_str) != Some(user_id)
    {
        return Ok((reply(false, revision), false));
    }
    let Some(candidate) = lookup(lifecycle, "completion_candidate") else {
        return Ok((reply(false, revision), false));
    };
    let Some(completed_at @ PyValue::Int(_)) = candidate.get("completed_at_ms") else {
        return Ok((reply(false, revision), false));
    };
    if !same_evidence(candidate.get("receipt"), &receipt)? {
        return Ok((reply(false, revision), false));
    }
    let completed_at = completed_at.clone();
    if lookup(current, "current_node_id").and_then(PyValue::as_str) != Some(assistant_id) {
        return Ok((reply(false, revision), false));
    }
    let Some(assistant) = find_assistant(lookup(current, "messages"), assistant_id)? else {
        return Ok((reply(false, revision), false));
    };
    if !assistant_completed(assistant)? {
        return Ok((reply(false, revision), false));
    }
    let outcome = object([
        ("status", text("ok")),
        ("turn_id", text(turn_id)),
        ("conversation_id", text(conversation_id)),
        (
            "conversation_revision",
            required(reference, "conversation_revision")?.clone(),
        ),
        ("user_message_id", text(user_id)),
        ("message", assistant.clone()),
    ]);
    if Some(canonical_digest(&outcome)?.as_str()) != string(reference, "outcome_digest") {
        return Ok((reply(false, revision), false));
    }
    let next_revision = revision
        .checked_add(1)
        .ok_or(ConfirmationError::Compatibility)?;
    let Some(PyValue::Object(lifecycle)) = field(current, "lifecycle") else {
        return Err(ConfirmationError::Compatibility);
    };
    for (key, value) in [
        ("state", text("completed")),
        ("completed_at_ms", completed_at),
        ("completion_message_id", text(assistant_id)),
        ("confirmed_completion", confirmed),
        ("completion_candidate", PyValue::Null),
    ] {
        set_field(lifecycle, key, value);
    }
    set_field(state, "revision", PyValue::Int(next_revision.to_string()));
    Ok((reply(true, next_revision), true))
}

fn checked_receipt<'a>(
    state: &[(String, PyValue)],
    turn: &'a PyValue,
    turn_id: &str,
    conversation_id: &str,
) -> Result<(&'a PyValue, PyValue), ConfirmationError> {
    let inventory = match lookup(state, "saved_receipts") {
        None => return Err(ConfirmationError::Receipt),
        Some(inventory @ PyValue::Object(_)) => inventory,
        Some(_) => return Err(ConfirmationError::Compatibility),
    };
    let receipt = inventory.get(turn_id).ok_or(ConfirmationError::Receipt)?;
    let reference = turn
        .get("result_reference")
        .filter(|reference| reference.as_object().is_some())
        .ok_or(ConfirmationError::Receipt)?;
    if string(receipt, "conversation_id") != Some(conversation_id)
        || string(receipt, "input_digest") != string(turn, "input_digest")
        || integer(receipt.get("initial_revision"))? != integer(turn.get("conversation_revision"))?
        || !same_evidence(receipt.get("result_reference"), reference)?
    {
        return Err(ConfirmationError::Receipt);
    }
    Ok((reference, receipt.clone()))
}

fn find_assistant<'a>(
    messages: Option<&'a PyValue>,
    assistant_id: &str,
) -> Result<Option<&'a PyValue>, ConfirmationError> {
    let Some(messages) = messages else {
        return Ok(None);
    };
    if !ordered_python_compat::pytruthy(messages) {
        return Ok(None);
    }
    let messages = messages
        .as_array()
        .ok_or(ConfirmationError::Compatibility)?;
    for message in messages {
        if message.as_object().is_none() {
            return Err(ConfirmationError::Compatibility);
        }
        if string(message, "id") == Some(assistant_id) {
            return Ok(Some(message));
        }
    }
    Ok(None)
}

fn assistant_completed(message: &PyValue) -> Result<bool, ConfirmationError> {
    if string(message, "role") != Some("assistant") {
        return Ok(false);
    }
    let empty = PyValue::Null;
    let metadata = message.get("metadata").unwrap_or(&empty);
    let explicit = [
        metadata.get("task_state"),
        metadata.get("thinking").and_then(|v| v.get("state")),
    ];
    let status = message.get("status");
    let finish = string(message, "finish_reason")
        .unwrap_or_default()
        .to_ascii_lowercase();
    let success = ["stop", "end_turn", "completed", "complete"];
    if !success.contains(&finish.as_str()) {
        return Ok(false);
    }
    // With an explicit successful finish, every other nonempty task or
    // thinking state blocks promotion, whether waiting, failed or unknown.
    if explicit.into_iter().flatten().any(|state| {
        !matches!(state, PyValue::Null)
            && state.as_str() != Some("")
            && !state
                .as_str()
                .is_some_and(|s| success.contains(&s.to_ascii_lowercase().as_str()))
    }) || [
        "cancelled",
        "approval_required",
        "waiting_for_user",
        "draft",
        "streaming",
    ]
    .iter()
    .any(|flag| metadata.get(flag) == Some(&PyValue::Bool(true)))
    {
        return Ok(false);
    }
    match status {
        None | Some(PyValue::Null) => Ok(true),
        Some(PyValue::String(value)) => {
            Ok(["", "complete", "completed", "success", "succeeded"].contains(&value.as_str()))
        }
        Some(PyValue::Object(_) | PyValue::Array(_)) => Err(ConfirmationError::Compatibility),
        _ => Ok(false),
    }
}

fn same_evidence(left: Option<&PyValue>, right: &PyValue) -> Result<bool, ConfirmationError> {
    let Some(left) = left else {
        return Ok(false);
    };
    // These signed evidence objects are canonical JSON. Compare complete
    // unordered maps, falling back before publication for legacy numeric
    // representations outside the strict domain.
    Ok(python_equal(&canonical(left)?, &canonical(right)?))
}

fn python_equal(left: &CanonValue, right: &CanonValue) -> bool {
    match (left, right) {
        (CanonValue::Int(number), CanonValue::Bool(flag))
        | (CanonValue::Bool(flag), CanonValue::Int(number)) => *number == i64::from(*flag),
        (CanonValue::Arr(left), CanonValue::Arr(right)) => {
            left.len() == right.len() && left.iter().zip(right).all(|(a, b)| python_equal(a, b))
        }
        (CanonValue::Obj(left), CanonValue::Obj(right)) => {
            left.len() == right.len()
                && left
                    .iter()
                    .zip(right)
                    .all(|((key, a), (other, b))| key == other && python_equal(a, b))
        }
        _ => left == right,
    }
}

fn canonical(value: &PyValue) -> Result<CanonValue, ConfirmationError> {
    let text = dumps_bounded(value, MAX_BYTES).map_err(|_| ConfirmationError::Compatibility)?;
    tobkiri_canon::parse(text.as_bytes(), Variant::StrictV1)
        .map_err(|_| ConfirmationError::Compatibility)
}

fn canonical_digest(value: &PyValue) -> Result<Digest, ConfirmationError> {
    tobkiri_canon::digest(&canonical(value)?, Variant::StrictV1)
        .map_err(|_| ConfirmationError::Compatibility)
}

fn sort_keys(value: &mut PyValue) {
    match value {
        PyValue::Object(fields) => {
            fields.sort_unstable_by(|(left, _), (right, _)| left.cmp(right));
            for (_, value) in fields {
                sort_keys(value);
            }
        }
        PyValue::Array(values) => values.iter_mut().for_each(sort_keys),
        _ => {}
    }
}

fn identifier<'a>(value: &'a PyValue, name: &str) -> Result<&'a str, ConfirmationError> {
    let value = pystrip(required_string(value, name)?);
    validate_identifier(value)?;
    Ok(value)
}

fn integer(value: Option<&PyValue>) -> Result<i64, ConfirmationError> {
    match value {
        Some(PyValue::Int(value)) => value.parse().map_err(|_| ConfirmationError::Compatibility),
        _ => Err(ConfirmationError::Compatibility),
    }
}

fn required<'a>(value: &'a PyValue, key: &str) -> Result<&'a PyValue, ConfirmationError> {
    value.get(key).ok_or(ConfirmationError::Compatibility)
}

fn required_string<'a>(value: &'a PyValue, key: &str) -> Result<&'a str, ConfirmationError> {
    string(value, key).ok_or(ConfirmationError::Compatibility)
}

fn string<'a>(value: &'a PyValue, key: &str) -> Option<&'a str> {
    value.get(key).and_then(PyValue::as_str)
}

fn lookup<'a>(fields: &'a [(String, PyValue)], key: &str) -> Option<&'a PyValue> {
    fields
        .iter()
        .find(|(name, _)| name == key)
        .map(|(_, value)| value)
}

fn stored_field<'a>(
    fields: &'a [(String, PyValue)],
    key: &str,
) -> Result<&'a PyValue, ConfirmationError> {
    lookup(fields, key).ok_or(ConfirmationError::Compatibility)
}

fn result(confirmed: bool, id: PyValue, conversation_revision: PyValue, revision: i64) -> PyValue {
    object([
        ("confirmed", PyValue::Bool(confirmed)),
        ("conversation_id", id),
        ("conversation_revision", conversation_revision),
        ("store_revision", PyValue::Int(revision.to_string())),
    ])
}

fn object<const N: usize>(fields: [(&str, PyValue); N]) -> PyValue {
    PyValue::Object(
        fields
            .into_iter()
            .map(|(name, value)| (name.into(), value))
            .collect(),
    )
}

fn text(value: &str) -> PyValue {
    PyValue::String(value.into())
}

#[cfg(test)]
#[path = "legacy_confirmation_tests.rs"]
mod tests;
