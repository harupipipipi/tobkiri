//! Source binding and projection for the original Saved Turn input capture.

use crate::{
    Error, ObjectFields, RECORD_LIMIT, Result, check_structure_bound, equals_python_integer,
    load_record,
};
use ordered_python_compat::{CodecError, PyValue, loads, loads_python_json, pytruthy};
use std::collections::BTreeMap;
use tobkiri_canon::{CanonError, CanonValue, Digest, Variant};
use tobkiri_saved_input::{ValidatedInput, ValidationError, validate};

const PROJECTION_KIND: &str = "tobkiri.saved.input-projection.v1";
const PROJECTED_FIELDS: [&str; 2] = ["task_context", "resolved_chat_references"];
const TARGET_SETTING: &str = "tool_selection";

/// Index and immutable identity of the original validated saved input.
#[derive(Debug, Eq, PartialEq)]
pub struct SavedInputSource {
    /// Physical saved input row key.
    pub turn_id: String,
    /// Canonical SHA-256 of the full original input.
    pub source_digest: String,
}

/// Prepared capture for the original owner's transaction.
#[derive(Debug, Eq, PartialEq)]
pub struct SavedInputCapture {
    /// Physical saved input row key.
    pub turn_id: String,
    /// Canonical SHA-256 of the full original input.
    pub source_digest: String,
    /// Canonical SHA-256 of the full accepted input.
    pub accepted_digest: String,
    /// Compact owner projection to persist with both digests.
    pub context: Vec<u8>,
    /// Full validated accepted input in canonical JSON.
    pub accepted: Vec<u8>,
}

/// Identify the original input before looking up its owner row.
pub fn saved_input_source(source: &[u8]) -> Result<SavedInputSource> {
    let source = load_source(source)?;
    let validated = validate_initial(&source, Error::InvalidRequest)?;
    let turn_id = turn_id(&validated)?;
    let body = encode(&validated.canonical, Error::InvalidRequest)?;
    Ok(SavedInputSource {
        turn_id,
        source_digest: Digest::hash(&body).to_string(),
    })
}

/// Project owner-captured context without changing the immutable user input.
pub fn prepare_saved_input(source: &[u8], accepted: &[u8]) -> Result<SavedInputCapture> {
    let source = load_source(source)?;
    let accepted = load_source(accepted)?;
    let initial = validate_initial(&source, Error::InvalidRequest)?;
    let captured = validate_initial(&accepted, Error::InvalidRequest)?;
    let turn_id = turn_id(&initial)?;
    if initial.request.get("resolved_chat_references").is_some() {
        return Err(Error::Conflict);
    }

    let source_digest = Digest::hash(&encode(&initial.canonical, Error::InvalidRequest)?);
    let accepted = encode(&captured.canonical, Error::InvalidRequest)?;
    let accepted_digest = Digest::hash(&accepted);
    let mut original_request = into_request(initial.canonical)?;
    let mut accepted_request = into_request(captured.canonical)?;
    original_request.remove("task_context");
    let [task_context, chat_references] =
        PROJECTED_FIELDS.map(|field| accepted_request.remove(field).unwrap_or(CanonValue::Null));
    // Both objects have already passed strict canonical validation. Equality
    // here is the same comparison as Python's canonical request digests.
    if original_request != accepted_request {
        return Err(Error::Conflict);
    }

    let settings = original_request
        .remove_entry(TARGET_SETTING)
        .into_iter()
        .collect();
    let context = encode_projection(task_context, chat_references, CanonValue::Obj(settings))?;

    Ok(SavedInputCapture {
        turn_id,
        source_digest: source_digest.to_string(),
        accepted_digest: accepted_digest.to_string(),
        context,
        accepted,
    })
}

/// Reconstruct and verify the accepted input from an existing owner row.
pub fn replay_saved_input(
    source: &[u8],
    source_digest: &str,
    accepted_digest: &str,
    context: &[u8],
) -> Result<Vec<u8>> {
    let mut source = load_source(source)?;
    let validated = validate_initial(&source, Error::InvalidRequest)?;
    let source_body = encode(&validated.canonical, Error::InvalidRequest)?;
    if Digest::hash(&source_body).as_str() != source_digest {
        return Err(Error::Conflict);
    }
    if Digest::parse(accepted_digest).is_err() {
        return Err(Error::InvalidRecord);
    }

    let context = load_projection(context)?;
    let PyValue::Object(wrapper) = &mut source else {
        return Err(Error::InvalidRequest);
    };
    let Some((_, request @ PyValue::Object(_))) = wrapper.first_mut() else {
        return Err(Error::InvalidRequest);
    };
    apply_projection(request, context)?;
    let captured = validate_initial(&source, Error::InvalidRecord)?;
    let accepted = encode(&captured.canonical, Error::InvalidRecord)?;
    if Digest::hash(&accepted).as_str() != accepted_digest {
        return Err(Error::InvalidRecord);
    }
    Ok(accepted)
}

/// Check an existing original turn against a proposed capture under its owner lock.
///
/// Queued delivery and Calendar rows with the original source digest require
/// the legacy codec to rebind the turn and capture its context together.
pub fn validate_saved_input_capture_turn(
    profile: &str,
    turn_id: &str,
    request_id: &str,
    body: &[u8],
    source_digest: &Digest,
    accepted_digest: &Digest,
) -> Result<()> {
    let record = load_record(profile, turn_id, request_id, body).map_err(|error| match error {
        // The general row codec does not distinguish opaque legacy data
        // from malformed JSON. Neither can prove a queued context rebind.
        Error::RequiresLegacyCodec => Error::InvalidRecord,
        error => error,
    })?;
    let digest = record.string("input_digest");
    if digest == Some(accepted_digest.as_str()) {
        return Ok(());
    }
    if digest == Some(source_digest.as_str())
        && record.string("status") == Some("queued")
        && ["delivery_provenance", "calendar_preparation"]
            .iter()
            .any(|field| record.get(field).is_some_and(pytruthy))
    {
        return Err(Error::RequiresLegacyCodec);
    }
    Err(Error::Conflict)
}

fn load_source(body: &[u8]) -> Result<PyValue> {
    if body.len() > tobkiri_canon::MAX_BYTES {
        return Err(Error::Capacity);
    }
    check_structure_bound(body)?;
    let text = std::str::from_utf8(body).map_err(|_| Error::InvalidRequest)?;
    loads(text).map_err(|error| match error {
        CodecError::ByteLimit | CodecError::Resource => Error::Capacity,
        CodecError::NodeLimit => Error::RequiresLegacyCodec,
        _ => Error::InvalidRequest,
    })
}

fn load_projection(body: &[u8]) -> Result<PyValue> {
    if body.len() > RECORD_LIMIT {
        return Err(Error::Capacity);
    }
    check_structure_bound(body)?;
    let text = std::str::from_utf8(body).map_err(|_| Error::InvalidRecord)?;
    loads(text).map_err(|error| match error {
        CodecError::Resource | CodecError::ByteLimit => Error::Capacity,
        CodecError::NodeLimit => Error::RequiresLegacyCodec,
        // The original database decoder recognizes additional historical
        // JSON syntax, including duplicate keys and escaped lone surrogates.
        _ if loads_python_json(text).is_ok() => Error::RequiresLegacyCodec,
        _ => Error::InvalidRecord,
    })
}

fn validate_initial(input: &PyValue, invalid: Error) -> Result<ValidatedInput<'_>> {
    validate(input).map_err(|error| match error {
        ValidationError::Invalid => invalid,
        ValidationError::Limit => Error::Capacity,
        ValidationError::Compatibility => Error::RequiresLegacyCodec,
    })
}

fn turn_id(input: &ValidatedInput<'_>) -> Result<String> {
    input
        .request
        .string("turn_id")
        .map(str::to_owned)
        .ok_or(Error::InvalidRequest)
}

fn into_request(input: CanonValue) -> Result<BTreeMap<String, CanonValue>> {
    let CanonValue::Obj(mut wrapper) = input else {
        return Err(Error::InvalidRequest);
    };
    match wrapper.remove("request") {
        Some(CanonValue::Obj(request)) => Ok(request),
        _ => Err(Error::InvalidRequest),
    }
}

fn encode(value: &CanonValue, invalid: Error) -> Result<Vec<u8>> {
    tobkiri_canon::encode(value, Variant::StrictV1).map_err(|error| match error {
        CanonError::Limit(_) => Error::Capacity,
        _ => invalid,
    })
}

fn encode_projection(
    task_context: CanonValue,
    chat_references: CanonValue,
    settings: CanonValue,
) -> Result<Vec<u8>> {
    // The original writer emits these four fields in insertion order. The
    // nested values came from the original validator's recursively sorted input.
    let mut body = format!(r#"{{"kind":"{PROJECTION_KIND}""#).into_bytes();
    for (key, value) in [
        ("task_context", task_context),
        ("resolved_chat_references", chat_references),
        ("target_settings", settings),
    ] {
        body.extend_from_slice(format!(",\"{key}\":").as_bytes());
        body.extend(encode(&value, Error::InvalidRequest)?);
        if body.len() >= RECORD_LIMIT {
            return Err(Error::Capacity);
        }
    }
    body.push(b'}');
    Ok(body)
}

fn apply_projection(request: &mut PyValue, context: PyValue) -> Result<()> {
    let PyValue::Object(fields) = request else {
        return Err(Error::InvalidRequest);
    };
    fields.retain(|(key, _)| !PROJECTED_FIELDS.contains(&key.as_str()));

    if context.string("kind") == Some(PROJECTION_KIND) {
        if !context.has_fields(&["kind", "task_context", "resolved_chat_references"])
            && !context.has_fields(&[
                "kind",
                "task_context",
                "resolved_chat_references",
                "target_settings",
            ])
        {
            return Err(Error::InvalidRecord);
        }
        if context
            .get("target_settings")
            .is_some_and(|settings| !same_target_settings(settings, request.get(TARGET_SETTING)))
        {
            return Err(Error::InvalidRecord);
        }
        let projection = context.into_object().ok_or(Error::InvalidRecord)?;
        let PyValue::Object(fields) = request else {
            return Err(Error::InvalidRequest);
        };
        fields.extend(projection.into_iter().filter(|(key, value)| {
            PROJECTED_FIELDS.contains(&key.as_str()) && !matches!(value, PyValue::Null)
        }));
    } else if context != PyValue::Null {
        // Before projections were versioned, the row stored only task context.
        fields.push(("task_context".into(), context));
    }
    Ok(())
}

fn same_target_settings(settings: &PyValue, expected: Option<&PyValue>) -> bool {
    match expected {
        None => settings.has_fields(&[]),
        Some(expected) => {
            settings.has_fields(&[TARGET_SETTING])
                && settings
                    .get(TARGET_SETTING)
                    .is_some_and(|actual| same_python_value(actual, expected))
        }
    }
}

// The expected settings come from a validated source. Python dict equality
// ignores order, and Python numeric equality also applies to must_use.
fn same_python_value(actual: &PyValue, expected: &PyValue) -> bool {
    match expected {
        PyValue::Object(expected) => actual.as_object().is_some_and(|fields| {
            fields.len() == expected.len()
                && expected.iter().all(|(key, value)| {
                    actual
                        .get(key)
                        .is_some_and(|actual| same_python_value(actual, value))
                })
        }),
        PyValue::Array(expected) => actual.as_array().is_some_and(|items| {
            items.len() == expected.len()
                && items
                    .iter()
                    .zip(expected)
                    .all(|(actual, expected)| same_python_value(actual, expected))
        }),
        PyValue::Bool(value) => equals_python_integer(actual, u64::from(*value)),
        _ => actual == expected,
    }
}

#[cfg(test)]
#[path = "input_tests.rs"]
mod tests;
