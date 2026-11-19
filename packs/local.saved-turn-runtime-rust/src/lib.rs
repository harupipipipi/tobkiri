//! Receipt validation and lifecycle transition for the original Turn owner.

use ordered_python_compat::{CodecError, PyValue, dumps, dumps_bounded, loads, pystrip};
use std::collections::BTreeSet;
use tobkiri_canon::Digest;

#[cfg(feature = "component")]
mod component;

/// Maximum body size accepted by the original owner.
pub const RECORD_LIMIT: usize = 1024 * 1024;

/// Maximum size of a content-free receipt reference for this port.
pub const REFERENCE_LIMIT: usize = 2 * 1024;

const MAX_DEPTH: usize = 96;
const MAX_STRUCTURAL_MARKS: usize = 48 * 1024;

/// Content-free result identity supplied by the captured saved-turn owner.
pub struct LegacySavedTurnSettlementRequest<'a> {
    /// Existing turn identifier in the captured Profile.
    pub turn_id: &'a str,
    /// Exact digest of the admitted input.
    pub input_digest: &'a str,
    /// Snapshot revision for reconciliation, absent for a direct completion.
    pub expected_revision: Option<u64>,
    /// Original JSON for the five-field result reference.
    pub result_reference: &'a [u8],
}

/// Typed result of the private native settlement operation.
#[derive(Clone, Copy, Debug, Eq, PartialEq, thiserror::Error)]
pub enum LegacySavedTurnSettlementError {
    /// The request could not identify an input and a valid revision.
    #[error("saved receipt settlement request is invalid")]
    InvalidRequest,
    /// The original database or turn is absent.
    #[error("turn is unknown")]
    NotFound,
    /// The immutable indexed identity or state is invalid.
    #[error("persisted turn record is invalid")]
    InvalidRecord,
    /// The supplied reference does not identify this turn's owner output.
    #[error("saved receipt result identity is invalid")]
    InvalidReference,
    /// The saved receipt cannot settle the current revision or lifecycle.
    #[error("saved receipt cannot settle current turn")]
    Conflict,
    /// The existing codec is needed for this row. No update was issued.
    #[error("saved receipt requires the legacy record codec")]
    RequiresLegacyCodec,
    /// The row or its temporary work exceeds the admitted capacity.
    #[error("saved receipt settlement capacity is unavailable")]
    Capacity,
    /// The current operation lost its authority or deadline.
    #[error("saved receipt settlement authority is no longer current")]
    Revoked,
    /// The physical store could not perform the operation before commit.
    #[error("saved receipt settlement storage is unavailable")]
    Storage,
    /// Commit was attempted and its durable result needs to be observed.
    #[error("saved receipt settlement commit is unconfirmed")]
    CommitUnconfirmed,
}

type Error = LegacySavedTurnSettlementError;

/// Result of a saved-turn receipt transition.
pub type Result<T> = std::result::Result<T, Error>;

const RESULT_FIELDS: &[&str] = &[
    "conversation_id",
    "conversation_revision",
    "user_message_id",
    "assistant_message_id",
    "outcome_digest",
];
const WORKFLOW_FIELDS: &[&str] = &[
    "profile_id",
    "turn_id",
    "conversation_id",
    "input_digest",
    "run_id",
    "definition_id",
    "revision_digest",
    "policy_digest",
    "plan_digest",
    "activation_id",
    "activation_digest",
    "security_epoch",
    "workflow_principal_ids",
    "owner_principal_id",
    "owner_session_id",
    "admission_api_version",
];

/// Decision for the observed row, to apply under the owner's write lock.
#[derive(Debug)]
pub enum Transition {
    /// The exact result was already persisted.
    Replay,
    /// Replace the row body and its timestamp together.
    Commit {
        /// New indexed timestamp sampled by the owner.
        updated_at: i64,
        /// Complete replacement JSON for the observed row.
        body: Vec<u8>,
    },
}

/// Check the owner result against the indexed observation and prepare settlement.
///
/// The caller supplies the original row under an active write transaction and
/// commits the decision, including checking live authority before publication.
pub fn settle(
    profile_id: &str,
    turn_id: &str,
    request_id: &str,
    body: &[u8],
    request: &LegacySavedTurnSettlementRequest<'_>,
    clock: &mut impl FnMut() -> Result<i64>,
) -> Result<Transition> {
    if request.turn_id != turn_id
        || Digest::parse(request.input_digest).is_err()
        || request.expected_revision == Some(0)
    {
        return Err(Error::InvalidRequest);
    }
    if body.len() > RECORD_LIMIT {
        return Err(Error::Capacity);
    }
    let mut turn = parse(body)?;
    validate_record(&turn, profile_id, turn_id, request_id)?;

    let conversation_id = turn.string("conversation_id").ok_or(Error::InvalidRecord)?;
    let initial_revision = turn
        .get("conversation_revision")
        .ok_or(Error::InvalidRecord)?;
    let final_revision = exact_u64(initial_revision)
        .and_then(|value| value.checked_add(2))
        .ok_or(Error::RequiresLegacyCodec)?;
    if request.result_reference.len() > REFERENCE_LIMIT {
        return Err(Error::RequiresLegacyCodec);
    }
    let reference = parse(request.result_reference)?;
    validate_reference(&reference, conversation_id, turn_id, final_revision)?;

    let saved = turn.string("input_digest") == Some(request.input_digest)
        && request_id.starts_with("saved-turn.");
    if saved
        && turn.string("status") == Some("completed")
        && turn
            .get("result_reference")
            .is_some_and(|previous| same_reference(previous, &reference, final_revision))
    {
        return Ok(Transition::Replay);
    }
    if !saved || !matches!(turn.string("status"), Some("running" | "waiting")) {
        return Err(Error::Conflict);
    }
    let revision = turn
        .get("revision")
        .and_then(exact_u64)
        .ok_or(Error::RequiresLegacyCodec)?;
    if request
        .expected_revision
        .is_some_and(|expected| revision != expected)
    {
        return Err(Error::Conflict);
    }
    let stopped = cancellation_requested(turn.array("events").ok_or(Error::InvalidRecord)?)?;
    let mut queued = Vec::new();
    if stopped {
        let guidance = turn.array("guidance").ok_or(Error::InvalidRecord)?;
        for (index, item) in guidance.iter().enumerate() {
            if item.string("status") == Some("queued")
                && item.get("value").and_then(|value| value.get("auto_send"))
                    == Some(&PyValue::Bool(true))
            {
                let id = item.get("id").ok_or(Error::RequiresLegacyCodec)?;
                queued.push((index, id.clone()));
            }
        }
    }
    let final_turn_revision = revision
        .checked_add(1 + u64::from(!queued.is_empty()))
        .ok_or(Error::RequiresLegacyCodec)?;

    if !queued.is_empty() {
        let changed_at = clock()?;
        let mut ids = Vec::with_capacity(queued.len());
        let guidance = turn.array_mut("guidance").ok_or(Error::InvalidRecord)?;
        for (index, id) in queued {
            let item = &mut guidance[index];
            item.set("status", string("failed"))?;
            item.set("updated_at", integer(changed_at))?;
            item.set("failure_reason", string("cancelled_before_dispatch"))?;
            ids.push(id);
        }
        append_event(
            &mut turn,
            "turn.guidance_cancelled",
            changed_at,
            object([("guidance_ids", PyValue::Array(ids))]),
        )?;
    }

    let updated_at = clock()?;
    let event_at = clock()?;
    turn.set("status", string("completed"))?;
    turn.set("revision", integer(final_turn_revision))?;
    turn.set("updated_at", integer(updated_at))?;
    turn.set("result_reference", reference.clone())?;
    append_event(
        &mut turn,
        "turn.completed",
        event_at,
        object([("result_reference", reference)]),
    )?;

    let body = dumps_bounded(&turn, RECORD_LIMIT)
        .map_err(|error| match error {
            CodecError::ByteLimit | CodecError::Resource => Error::Capacity,
            _ => Error::InvalidRecord,
        })?
        .into_bytes();
    Ok(Transition::Commit { updated_at, body })
}

fn validate_record(turn: &PyValue, profile: &str, id: &str, request_id: &str) -> Result<()> {
    if turn.string("profile_id") != Some(profile)
        || turn.string("id") != Some(id)
        || turn.string("request_id") != Some(request_id)
        || ["id", "request_id", "conversation_id"]
            .iter()
            .any(|field| !turn.string(field).is_some_and(identifier))
        || [
            "revision",
            "conversation_revision",
            "created_at",
            "updated_at",
        ]
        .iter()
        .any(|field| !turn.get(field).is_some_and(positive_integer))
        || !matches!(
            turn.string("status"),
            Some("queued" | "running" | "waiting" | "completed" | "failed" | "cancelled")
        )
        || ["guidance", "events"].iter().any(|field| {
            !turn
                .array(field)
                .is_some_and(|items| items.iter().all(|item| item.as_object().is_some()))
        })
        || turn.get("input_digest").is_some_and(|value| {
            value
                .as_str()
                .is_none_or(|digest| Digest::parse(digest).is_err())
        })
    {
        return Err(Error::InvalidRecord);
    }
    if let Some(workflow) = turn.get("saved_workflow") {
        validate_workflow(turn, workflow)?;
    }
    Ok(())
}

fn validate_workflow(turn: &PyValue, workflow: &PyValue) -> Result<()> {
    if !workflow.has_fields(WORKFLOW_FIELDS)
        || workflow.string("admission_api_version")
            != Some("io.tobkiri.saved-workflow-admission.v1")
        || [
            "profile_id",
            "turn_id",
            "conversation_id",
            "run_id",
            "definition_id",
            "activation_id",
        ]
        .iter()
        .any(|field| !workflow.string(field).is_some_and(identifier))
        || [
            "input_digest",
            "revision_digest",
            "policy_digest",
            "plan_digest",
            "activation_digest",
        ]
        .iter()
        .any(|field| {
            workflow
                .string(field)
                .is_none_or(|digest| Digest::parse(digest).is_err())
        })
        || !workflow.get("security_epoch").is_some_and(positive_integer)
        || workflow.get("profile_id") != turn.get("profile_id")
        || workflow.get("turn_id") != turn.get("id")
        || workflow.get("conversation_id") != turn.get("conversation_id")
        || workflow.get("input_digest") != turn.get("input_digest")
        || !["owner_principal_id", "owner_session_id"]
            .iter()
            .all(|field| workflow.string(field).is_some_and(workflow_identity))
    {
        return Err(Error::InvalidRecord);
    }
    let principals = workflow
        .array("workflow_principal_ids")
        .ok_or(Error::InvalidRecord)?;
    let mut unique = BTreeSet::new();
    if !(1..=64).contains(&principals.len())
        || !principals.iter().all(|principal| {
            principal
                .as_str()
                .is_some_and(|value| workflow_identity(value) && unique.insert(value))
        })
    {
        return Err(Error::InvalidRecord);
    }
    Ok(())
}

fn identifier(value: &str) -> bool {
    let mut bytes = value.bytes();
    value.len() <= 256
        && bytes
            .next()
            .is_some_and(|byte| byte.is_ascii_alphanumeric())
        && bytes
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'))
}

fn workflow_identity(value: &str) -> bool {
    value.chars().count() <= 1024 && !pystrip(value).is_empty()
}

fn validate_reference(
    reference: &PyValue,
    conversation: &str,
    turn: &str,
    revision: u64,
) -> Result<()> {
    if !reference.has_fields(RESULT_FIELDS)
        || reference.string("conversation_id") != Some(conversation)
        || reference.get("conversation_revision").and_then(exact_u64) != Some(revision)
        || reference
            .string("outcome_digest")
            .is_none_or(|digest| Digest::parse(digest).is_err())
    {
        return Err(Error::InvalidReference);
    }
    for (field, role) in [
        ("user_message_id", "user"),
        ("assistant_message_id", "assistant"),
    ] {
        let encoded = dumps(&PyValue::Array(vec![
            string(conversation),
            string(turn),
            string(role),
        ]))
        .map_err(|_| Error::InvalidRecord)?;
        let digest = Digest::hash(encoded.as_bytes());
        let expected = format!("message:{}", digest.hex());
        if reference.string(field) != Some(expected.as_str()) {
            return Err(Error::InvalidReference);
        }
    }
    Ok(())
}

fn same_reference(previous: &PyValue, reference: &PyValue, revision: u64) -> bool {
    previous.has_fields(RESULT_FIELDS)
        && RESULT_FIELDS.iter().all(|field| {
            if *field == "conversation_revision" {
                previous
                    .get(field)
                    .is_some_and(|value| equals_python_integer(value, revision))
            } else {
                previous.get(field) == reference.get(field)
            }
        })
}

fn cancellation_requested(events: &[PyValue]) -> Result<bool> {
    let mut requested = false;
    for (index, event) in events.iter().enumerate() {
        match event.string("name") {
            Some("turn.cancelled") => return Err(Error::Conflict),
            Some("turn.cancellation_requested") => {
                let exact = index + 1 == events.len()
                    && event
                        .get("sequence")
                        .is_some_and(|value| equals_python_integer(value, index as u64))
                    && event.get("details").is_some_and(|details| {
                        details.has_fields(&["phase"])
                            && details.string("phase") == Some("nested_cancellation_requested")
                    });
                if !exact {
                    return Err(Error::Conflict);
                }
                requested = true;
            }
            _ => (),
        }
    }
    Ok(requested)
}

fn append_event(turn: &mut PyValue, name: &str, at: i64, details: PyValue) -> Result<()> {
    let events = turn.array_mut("events").ok_or(Error::InvalidRecord)?;
    events.push(object([
        ("sequence", integer(events.len())),
        ("name", string(name)),
        ("at", integer(at)),
        ("details", details),
    ]));
    Ok(())
}

fn object<const N: usize>(fields: [(&str, PyValue); N]) -> PyValue {
    PyValue::Object(
        fields
            .into_iter()
            .map(|(key, value)| (key.to_owned(), value))
            .collect(),
    )
}

fn string(value: &str) -> PyValue {
    PyValue::String(value.to_owned())
}

fn integer(value: impl ToString) -> PyValue {
    PyValue::Int(value.to_string())
}

fn positive_integer(value: &PyValue) -> bool {
    matches!(value, PyValue::Int(value) if value != "0" && !value.starts_with('-'))
}

fn exact_u64(value: &PyValue) -> Option<u64> {
    if let PyValue::Int(value) = value {
        value.parse().ok()
    } else {
        None
    }
}

fn equals_python_integer(value: &PyValue, integer: u64) -> bool {
    match value {
        PyValue::Bool(value) => u64::from(*value) == integer,
        PyValue::Float(value) => {
            // Python compares integers and floats by value, without
            // rounding the integer to the nearest representable float.
            (0.0..18446744073709551616.0).contains(value)
                && value.fract() == 0.0
                && *value as u64 == integer
        }
        _ => exact_u64(value) == Some(integer),
    }
}

trait ObjectFields {
    fn string(&self, name: &str) -> Option<&str>;
    fn array(&self, name: &str) -> Option<&[PyValue]>;
    fn array_mut(&mut self, name: &str) -> Option<&mut Vec<PyValue>>;
    fn set(&mut self, name: &str, value: PyValue) -> Result<()>;
    fn has_fields(&self, names: &[&str]) -> bool;
}

impl ObjectFields for PyValue {
    fn string(&self, name: &str) -> Option<&str> {
        self.get(name)?.as_str()
    }

    fn array(&self, name: &str) -> Option<&[PyValue]> {
        self.get(name)?.as_array()
    }

    fn array_mut(&mut self, name: &str) -> Option<&mut Vec<PyValue>> {
        let PyValue::Object(fields) = self else {
            return None;
        };
        match fields.iter_mut().find(|(key, _)| key == name) {
            Some((_, PyValue::Array(items))) => Some(items),
            _ => None,
        }
    }

    fn set(&mut self, name: &str, value: PyValue) -> Result<()> {
        let PyValue::Object(fields) = self else {
            return Err(Error::InvalidRecord);
        };
        if let Some((_, slot)) = fields.iter_mut().find(|(key, _)| key == name) {
            *slot = value;
        } else {
            fields.push((name.to_owned(), value));
        }
        Ok(())
    }

    fn has_fields(&self, names: &[&str]) -> bool {
        self.as_object().is_some_and(|fields| {
            fields.len() == names.len() && names.iter().all(|name| self.get(name).is_some())
        })
    }
}

fn parse(bytes: &[u8]) -> Result<PyValue> {
    check_structure_bound(bytes)?;
    let text = std::str::from_utf8(bytes).map_err(|_| Error::RequiresLegacyCodec)?;
    // The shared finite ordered format handles arbitrary precision integers
    // and preserves insertion order. Python's owner handles additional
    // accepted forms, such as duplicate keys and escaped lone surrogates.
    loads(text).map_err(|error| match error {
        CodecError::Resource => Error::Capacity,
        _ => Error::RequiresLegacyCodec,
    })
}

/// Bound temporary container allocations before the shared decoder allocates.
/// This only counts structural marks outside strings; the decoder validates
/// the document itself.
fn check_structure_bound(bytes: &[u8]) -> Result<()> {
    let mut depth = 0usize;
    let mut marks = 0;
    let mut in_string = false;
    let mut escaped = false;
    for &byte in bytes {
        if in_string {
            if escaped {
                escaped = false;
            } else if byte == b'\\' {
                escaped = true;
            } else if byte == b'"' {
                in_string = false;
            }
            continue;
        }
        match byte {
            b'"' => in_string = true,
            b'{' | b'[' => {
                marks += 1;
                depth += 1;
            }
            b'}' | b']' => depth = depth.saturating_sub(1),
            b':' | b',' => marks += 1,
            _ => (),
        }
        if depth > MAX_DEPTH || marks > MAX_STRUCTURAL_MARKS {
            return Err(Error::RequiresLegacyCodec);
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests;
