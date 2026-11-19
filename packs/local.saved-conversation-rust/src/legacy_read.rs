//! Read-only projections over the original Conversation owner document.

use crate::legacy_append_context::{context_parent_id, inherited_fields};
use crate::saved_model::{STORE_VERSION, valid_conversation_id};
use ordered_python_compat::{
    CodecError, PyValue, dumps_bounded, dumps_spaced_document, loads, loads_python_json, pystrip,
};

pub(super) const MAX_BYTES: usize = 1024 * 1024;
const NATIVE_CONTAINER_DEPTH: usize = 256;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum ReadError {
    InvalidIdentifier,
    InvalidDocument,
    Profile,
    NotFound,
    Limits,
    LegacyCodec,
    LegacyReceiptInventory,
    Internal,
}

/// Return the original owner reply in an opaque JSON string so the Core DATA
/// codec never interprets the row's floats or Python-width integers.
pub(super) fn get(
    profile: &str,
    document: Option<&str>,
    conversation_id: &str,
) -> Result<String, ReadError> {
    let mut conversations = read_conversations(profile, document, conversation_id)?;
    let conversation = take_conversation(&mut conversations, pystrip(conversation_id))?;
    drop(conversations);
    owner_reply("conversation", conversation)
}

/// Project the child's persisted context and prepare the original parent reply
/// for a separately authorized owner read of the same snapshot.
pub(super) fn get_linked(
    profile: &str,
    document: Option<&str>,
    conversation_id: &str,
) -> Result<String, ReadError> {
    let mut conversations = read_conversations(profile, document, conversation_id)?;
    let conversation_id = pystrip(conversation_id);
    let child = take_conversation(&mut conversations, conversation_id)?;
    if child.get("id").and_then(PyValue::as_str) != Some(conversation_id) {
        return Err(ReadError::InvalidDocument);
    }
    let link = context_parent_id(&child).map_err(|_| ReadError::InvalidDocument)?;
    let (parent, parent_read) = if let Some(parent_id) = link {
        let parent = take_conversation(&mut conversations, parent_id)?;
        if parent.get("id").and_then(PyValue::as_str) != Some(parent_id)
            || context_parent_id(&parent)
                .map_err(|_| ReadError::InvalidDocument)?
                .is_some()
        {
            return Err(ReadError::InvalidDocument);
        }
        let metadata = parent
            .get("metadata")
            .and_then(PyValue::as_object)
            .unwrap_or_default();
        let mut projection = inherited_fields(&parent, metadata);
        projection.push(("id".into(), PyValue::String(parent_id.into())));
        projection.push((
            "conversation_revision".into(),
            parent
                .get("conversation_revision")
                .cloned()
                .unwrap_or(PyValue::Null),
        ));
        let parent_read = match dumps_bounded(&owner_fields("conversation", parent), MAX_BYTES) {
            Ok(bytes) => Some(bytes),
            Err(CodecError::ByteLimit | CodecError::NodeLimit) => None,
            Err(error) => return Err(output_error(error)),
        };
        (PyValue::Object(projection), parent_read)
    } else {
        (PyValue::Null, None)
    };
    drop(conversations);
    encode_owner_reply(
        PyValue::Object(vec![
            ("conversation".into(), child),
            ("parent_context".into(), parent),
        ]),
        parent_read,
    )
}

fn read_conversations(
    profile: &str,
    document: Option<&str>,
    conversation_id: &str,
) -> Result<Vec<(String, PyValue)>, ReadError> {
    validate_profile(profile)?;
    // The Host checks for an empty argument before opening the owner store.
    if conversation_id.is_empty() {
        return Err(ReadError::InvalidIdentifier);
    }
    let conversation_id = pystrip(conversation_id);
    let Some(document) = document else {
        validate_identifier(conversation_id)?;
        return Err(ReadError::NotFound);
    };
    let mut state = decode_document(document, profile, Some(conversation_id))?;
    let Some(PyValue::Object(conversations)) = field(&mut state, "conversations") else {
        return Err(ReadError::InvalidDocument);
    };
    Ok(std::mem::take(conversations))
}

fn take_conversation(
    conversations: &mut [(String, PyValue)],
    conversation_id: &str,
) -> Result<PyValue, ReadError> {
    let Some(PyValue::Object(conversation)) = field(conversations, conversation_id) else {
        return Err(ReadError::NotFound);
    };
    normalize(conversation);
    Ok(PyValue::Object(std::mem::take(conversation)))
}

/// Read the immutable receipt as stored, including user-only append evidence.
pub(super) fn saved_receipt(
    profile: &str,
    document: Option<&str>,
    turn_id: &str,
) -> Result<String, ReadError> {
    validate_profile(profile)?;
    let turn_id = pystrip(turn_id);
    let Some(document) = document else {
        validate_identifier(turn_id)?;
        return owner_reply("receipt", PyValue::Null);
    };
    // The Python owner reads the document and obtains saved_receipts.get
    // before validating the turn ID. A present non-map has no .get method.
    let mut state = decode_document(document, profile, None)?;
    let receipts = match field(&mut state, "saved_receipts") {
        None => None,
        Some(PyValue::Object(receipts)) => Some(receipts),
        Some(_) => return Err(ReadError::LegacyReceiptInventory),
    };
    validate_identifier(turn_id)?;
    let receipt = match receipts.and_then(|receipts| field(receipts, turn_id)) {
        Some(PyValue::Object(receipt)) => PyValue::Object(std::mem::take(receipt)),
        _ => PyValue::Null,
    };
    drop(state);
    owner_reply("receipt", receipt)
}

fn owner_reply(field: &str, value: PyValue) -> Result<String, ReadError> {
    encode_owner_reply(owner_fields(field, value), None)
}

fn owner_fields(field: &str, value: PyValue) -> PyValue {
    PyValue::Object(vec![(field.into(), value)])
}

fn encode_owner_reply(owner: PyValue, parent_read: Option<String>) -> Result<String, ReadError> {
    let owner = dumps_bounded(&owner, MAX_BYTES).map_err(output_error)?;
    let mut fields = vec![("owner_result".into(), PyValue::String(owner))];
    if let Some(parent_read) = parent_read {
        // The fixed envelope is larger than a stand-alone parent GET, so
        // fitting it also proves the parent's escaped output fits that GET.
        fields.push(("parent_read".into(), PyValue::String(parent_read)));
        let reply = PyValue::Object(fields);
        match dumps_bounded(&reply, MAX_BYTES) {
            Ok(bytes) => return Ok(bytes),
            Err(CodecError::ByteLimit | CodecError::NodeLimit) => {
                fields = reply.into_object().ok_or(ReadError::Internal)?;
                fields.pop();
            }
            Err(error) => return Err(output_error(error)),
        }
    }
    dumps_bounded(&PyValue::Object(fields), MAX_BYTES).map_err(output_error)
}

/// Serialize an old owner document with CPython's recursively sorted keys
/// and default separators. The inner reply preserves its owner field order.
pub(super) fn encode_owner_mutation(
    owner_result: &PyValue,
    mut document: Option<PyValue>,
) -> Result<String, CodecError> {
    let owner_result = dumps_bounded(owner_result, MAX_BYTES)?;
    let next_document = match document.as_mut() {
        Some(document) => {
            sort_keys(document);
            PyValue::String(dumps_spaced_document(document, MAX_BYTES)?)
        }
        None => PyValue::Null,
    };
    drop(document);
    let reply = PyValue::Object(vec![
        ("owner_result".into(), PyValue::String(owner_result)),
        ("next_document".into(), next_document),
    ]);
    dumps_bounded(&reply, MAX_BYTES)
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

pub(super) fn decode_document(
    raw: &str,
    profile: &str,
    conversation_id: Option<&str>,
) -> Result<Vec<(String, PyValue)>, ReadError> {
    if raw.len() > MAX_BYTES {
        return Err(ReadError::Limits);
    }
    let state = match loads(raw) {
        Ok(state) => state,
        Err(CodecError::Invalid | CodecError::NonFinite | CodecError::NodeLimit) => {
            let python = loads_python_json(raw).map_err(input_error)?;
            if !python.string_field_eq("version", STORE_VERSION)
                || !python.has_object_field("conversations")
            {
                return Err(ReadError::InvalidDocument);
            }
            if !python.string_field_eq("profile_id", profile) {
                return Err(match python.decode_field("profile_id") {
                    Ok(Some(PyValue::String(_))) => ReadError::Profile,
                    _ => ReadError::InvalidDocument,
                });
            }
            if let Some(conversation_id) = conversation_id {
                validate_identifier(conversation_id)?;
            }
            return Err(ReadError::LegacyCodec);
        }
        Err(error) => return Err(input_error(error)),
    };
    if state.get("version").and_then(PyValue::as_str) != Some(STORE_VERSION)
        || state
            .get("conversations")
            .and_then(PyValue::as_object)
            .is_none()
    {
        return Err(ReadError::InvalidDocument);
    }
    let Some(owner) = state.get("profile_id").and_then(PyValue::as_str) else {
        return Err(ReadError::InvalidDocument);
    };
    if owner != profile {
        return Err(ReadError::Profile);
    }
    if let Some(conversation_id) = conversation_id {
        validate_identifier(conversation_id)?;
    }
    // Python's decoder and its recursive `_utf8_safe` copy can reject deep
    // documents, including metadata on other rows. Their exact headroom
    // depends on the interpreter and caller, so let the original reader
    // decide those cases after validating the store and request identities.
    if !within_container_depth(&state, NATIVE_CONTAINER_DEPTH) {
        return Err(ReadError::LegacyCodec);
    }
    state.into_object().ok_or(ReadError::InvalidDocument)
}

fn within_container_depth(value: &PyValue, remaining: usize) -> bool {
    match value {
        PyValue::Array(items) => {
            remaining > 0
                && items
                    .iter()
                    .all(|item| within_container_depth(item, remaining - 1))
        }
        PyValue::Object(fields) => {
            remaining > 0
                && fields
                    .iter()
                    .all(|(_, item)| within_container_depth(item, remaining - 1))
        }
        _ => true,
    }
}

pub(super) fn validate_profile(profile: &str) -> Result<(), ReadError> {
    if profile.is_empty()
        || profile != pystrip(profile)
        || profile.contains(['/', '\\', '\0'])
        || profile.contains("..")
    {
        return Err(ReadError::Profile);
    }
    Ok(())
}

pub(super) fn validate_identifier(identifier: &str) -> Result<(), ReadError> {
    if valid_conversation_id(identifier) {
        Ok(())
    } else {
        Err(ReadError::InvalidIdentifier)
    }
}

pub(super) fn normalize(conversation: &mut Vec<(String, PyValue)>) {
    match field(conversation, "metadata") {
        Some(PyValue::Object(metadata)) => metadata.retain(|(name, _)| name != "icon_svg"),
        Some(metadata) => *metadata = PyValue::Object(Vec::new()),
        None => conversation.push(("metadata".into(), PyValue::Object(Vec::new()))),
    }

    if let Some(PyValue::Array(messages)) = field(conversation, "messages")
        && messages.iter().all(|message| message.as_object().is_some())
    {
        for (sequence, message) in messages.iter_mut().enumerate() {
            if let PyValue::Object(fields) = message {
                set_field(fields, "sequence", PyValue::Int(sequence.to_string()));
                set_field(
                    fields,
                    "sequence_number",
                    PyValue::Int((sequence + 1).to_string()),
                );
            }
        }
    }
}

pub(super) fn field<'a>(fields: &'a mut [(String, PyValue)], key: &str) -> Option<&'a mut PyValue> {
    fields
        .iter_mut()
        .find(|(name, _)| name == key)
        .map(|(_, value)| value)
}

pub(super) fn set_field(fields: &mut Vec<(String, PyValue)>, key: &str, value: PyValue) {
    if let Some(current) = field(fields, key) {
        *current = value;
    } else {
        fields.push((key.into(), value));
    }
}

fn input_error(error: CodecError) -> ReadError {
    match error {
        CodecError::Invalid | CodecError::NonFinite | CodecError::IntegerLimit => {
            ReadError::InvalidDocument
        }
        CodecError::ByteLimit | CodecError::NodeLimit => ReadError::Limits,
        CodecError::Resource => ReadError::Internal,
    }
}

fn output_error(error: CodecError) -> ReadError {
    match error {
        CodecError::ByteLimit | CodecError::NodeLimit => ReadError::LegacyCodec,
        _ => ReadError::Internal,
    }
}

#[cfg(test)]
#[path = "legacy_read_tests.rs"]
mod tests;
