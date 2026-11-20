//! Finite transport syntax for the original saved-turn input.

use ordered_python_compat::{CodecError, PyValue, dumps_bounded, pystrip, pytruthy};
use std::collections::BTreeSet;
use tobkiri_canon::{CanonValue, Digest, Variant};

const BINDING_VERSION: &str = "tobkiri.conversation-context-binding.v1";
const MAX_SAVED_INPUT_BYTES: usize = 3 * 1024 * 1024;
const MAX_SAVED_TEXT_BYTES: usize = 60 * 1024;
const MAX_SAVED_IMAGE_BYTES: usize = 1024 * 1024;
const MAX_SAVED_IMAGE_BASE64_CHARS: usize = MAX_SAVED_IMAGE_BYTES.div_ceil(3) * 4;
const MAX_SNAPSHOT_BYTES: usize = 64 * 1024;
const MAX_TASK_CONTEXT_BYTES: usize = 48 * 1024;

/// The saved request and its canonical, complete input.
pub struct ValidatedInput<'a> {
    /// The supplied request after finite shape checks.
    pub request: &'a PyValue,
    /// The whole input normalized to the original owner's canonical domain.
    pub canonical: CanonValue,
}

/// Failures of the original saved input's finite transport contract.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ValidationError {
    /// The input is outside the accepted contract.
    Invalid,
    /// The input exceeds an encoded byte limit.
    Limit,
    /// The bounded ordered codec cannot represent this input.
    Compatibility,
}

/// Validate one complete saved input, including its nested preferences and contexts.
pub fn validate(initial: &PyValue) -> Result<ValidatedInput<'_>, ValidationError> {
    let required = [
        "turn_id",
        "conversation_id",
        "conversation_revision",
        "content",
    ];
    let optional = [
        "tool_selection",
        "strategy_reference",
        "strategy_maximum_cost_microusd",
        "thinking_level",
        "action_approval_mode",
        "task_context",
        "context_binding",
        "chat_references",
        "resolved_chat_references",
    ];
    if !has_fields(initial, &["request"]) {
        return Err(ValidationError::Invalid);
    }
    let request = initial.get("request").ok_or(ValidationError::Invalid)?;
    let fields = request.as_object().ok_or(ValidationError::Invalid)?;
    if required.iter().any(|key| request.get(key).is_none())
        || fields
            .iter()
            .any(|(key, _)| !required.contains(&key.as_str()) && !optional.contains(&key.as_str()))
    {
        return Err(ValidationError::Invalid);
    }
    // The single {"request":...} wrapper adds exactly 12 bytes. Compact
    // ordered JSON uses the same string escaping and integer digits as
    // StrictV1 for values accepted by the strict parser.
    let strict_initial = canonical(initial, MAX_SAVED_INPUT_BYTES + 12)?;
    let turn_id = checked_id(request, "turn_id")?;
    let conversation_id = checked_id(request, "conversation_id")?;
    if integer(request.get("conversation_revision")).is_none_or(|value| value < 1)
        || !request.get("content").is_some_and(user_content)
    {
        return Err(ValidationError::Invalid);
    }
    if let Some(selection) = request.get("tool_selection") {
        validate_tool_selection(selection)?;
    }
    if let Some(context) = request.get("task_context") {
        validate_task_context(context, conversation_id, turn_id)?;
    }
    if let Some(binding) = request.get("context_binding") {
        validate_context_binding(binding)?;
    }
    if request
        .get("strategy_reference")
        .filter(|value| !matches!(value, PyValue::Null))
        .is_some_and(|strategy| {
            !strategy
                .as_str()
                .is_some_and(|value| identifier(value, false))
        })
    {
        return Err(ValidationError::Invalid);
    }
    let cost = request.get("strategy_maximum_cost_microusd");
    if cost.is_some_and(|value| !matches!(value, PyValue::Null))
        && !integer(cost).is_some_and(|value| (1..=1_000_000).contains(&value))
    {
        return Err(ValidationError::Invalid);
    }
    if !optional_string(
        request,
        "thinking_level",
        &["none", "low", "medium", "high", "xhigh"],
    ) || !optional_string(request, "action_approval_mode", &["ask", "agent", "full"])
    {
        return Err(ValidationError::Invalid);
    }
    let references = match request.get("chat_references") {
        Some(references) => validate_chat_references(references)?,
        None => Vec::new(),
    };
    if let Some(snapshot) = request.get("resolved_chat_references") {
        validate_resolved_chat_references(snapshot, &references)?;
    }
    Ok(ValidatedInput {
        request,
        canonical: strict_initial,
    })
}

fn user_content(content: &PyValue) -> bool {
    if let Some(text) = content.as_str() {
        return valid_user_text(text);
    }
    let Some(parts) = content
        .as_array()
        .filter(|parts| (1..=3).contains(&parts.len()))
    else {
        return false;
    };
    text_part(&parts[0]).is_some_and(valid_user_text) && parts[1..].iter().all(image_part)
}

fn valid_user_text(text: &str) -> bool {
    text.len() <= MAX_SAVED_TEXT_BYTES && !pystrip(text).is_empty()
}

fn image_part(part: &PyValue) -> bool {
    if !has_fields(part, &["type", "image_url"]) || string(part, "type") != Some("image_url") {
        return false;
    }
    let Some(value) = part
        .get("image_url")
        .filter(|value| has_fields(value, &["url"]))
    else {
        return false;
    };
    string(value, "url").is_some_and(image_url)
}

fn image_url(url: &str) -> bool {
    let Some((media_type, encoded)) = url
        .strip_prefix("data:")
        .and_then(|value| value.split_once(";base64,"))
    else {
        return false;
    };
    if encoded.is_empty()
        || encoded.len() > MAX_SAVED_IMAGE_BASE64_CHARS
        || !encoded.len().is_multiple_of(4)
    {
        return false;
    }
    let data = encoded.trim_end_matches('=');
    let padding = encoded.len() - data.len();
    if padding > 2 || encoded.len() / 4 * 3 - padding > MAX_SAVED_IMAGE_BYTES {
        return false;
    }

    // The Python validator only inspects the decoded length and magic.
    // Validate every symbol, retaining just the prefix needed for magic.
    let mut magic = [0_u8; 12];
    let mut magic_len = 0;
    let mut accumulator = 0_u16;
    let mut bits = 0;
    for byte in data.bytes() {
        let value = match byte {
            b'A'..=b'Z' => byte - b'A',
            b'a'..=b'z' => byte - b'a' + 26,
            b'0'..=b'9' => byte - b'0' + 52,
            b'+' => 62,
            b'/' => 63,
            _ => return false,
        };
        if magic_len == magic.len() {
            continue;
        }
        accumulator = (accumulator << 6) | u16::from(value);
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            magic[magic_len] = (accumulator >> bits) as u8;
            magic_len += 1;
        }
    }
    let magic = &magic[..magic_len];
    match media_type {
        "image/png" => magic.starts_with(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg" => magic.starts_with(b"\xff\xd8\xff"),
        "image/gif" => magic.starts_with(b"GIF87a") || magic.starts_with(b"GIF89a"),
        "image/webp" => magic.len() == 12 && &magic[..4] == b"RIFF" && &magic[8..] == b"WEBP",
        _ => false,
    }
}

type Reference<'a> = (&'a str, &'a str, &'a str);

fn validate_chat_references(value: &PyValue) -> Result<Vec<Reference<'_>>, ValidationError> {
    let items = value
        .as_array()
        .filter(|rows| rows.len() <= 16)
        .ok_or(ValidationError::Invalid)?;
    let mut seen = BTreeSet::new();
    let mut references = Vec::new();
    for item in items {
        if !has_fields(item, &["kind", "profile_id", "id"]) {
            return Err(ValidationError::Invalid);
        }
        let kind = string(item, "kind")
            .filter(|kind| matches!(*kind, "chat" | "group"))
            .ok_or(ValidationError::Invalid)?;
        let reference = (
            kind,
            checked_id(item, "profile_id")?,
            checked_id(item, "id")?,
        );
        if !seen.insert(reference) {
            return Err(ValidationError::Invalid);
        }
        references.push(reference);
    }
    Ok(references)
}

fn validate_resolved_chat_references(
    snapshot: &PyValue,
    lean: &[Reference<'_>],
) -> Result<(), ValidationError> {
    if !has_fields(
        snapshot,
        &[
            "kind",
            "profile_id",
            "store_revision",
            "project_revision",
            "references",
            "snapshot_time",
            "expires_at",
            "next_cursor",
            "truncated",
        ],
    ) || string(snapshot, "kind") != Some("tobkiri.chat.reference.snapshot.v1")
        || !matches!(snapshot.get("next_cursor"), Some(PyValue::Null))
        || !matches!(snapshot.get("truncated"), Some(PyValue::Bool(false)))
    {
        return Err(ValidationError::Invalid);
    }
    let profile = checked_id(snapshot, "profile_id")?;
    if lean
        .iter()
        .any(|(_, row_profile, _)| *row_profile != profile)
    {
        return Err(ValidationError::Invalid);
    }
    for key in [
        "store_revision",
        "project_revision",
        "snapshot_time",
        "expires_at",
    ] {
        if integer(snapshot.get(key)).is_none_or(|value| value < 0) {
            return Err(ValidationError::Invalid);
        }
    }
    let time = integer(snapshot.get("snapshot_time")).ok_or(ValidationError::Invalid)?;
    let expires = integer(snapshot.get("expires_at")).ok_or(ValidationError::Invalid)?;
    if time + 600_000 != expires {
        return Err(ValidationError::Invalid);
    }
    let rows = snapshot
        .get("references")
        .and_then(PyValue::as_array)
        .filter(|rows| rows.len() == lean.len())
        .ok_or(ValidationError::Invalid)?;
    for (row, (kind, _, id)) in rows.iter().zip(lean) {
        validate_reference_row(row, kind, id)?;
    }
    canonical(snapshot, MAX_SNAPSHOT_BYTES)?;
    Ok(())
}

fn validate_reference_row(row: &PyValue, kind: &str, id: &str) -> Result<(), ValidationError> {
    if !has_fields(
        row,
        &[
            "kind",
            "id",
            "label",
            "conversation_ids",
            "snapshot_digest",
            "member_count",
            "membership_complete",
        ],
    ) || string(row, "kind") != Some(kind)
        || string(row, "id") != Some(id)
        || !matches!(row.get("membership_complete"), Some(PyValue::Bool(true)))
        || !string(row, "label").is_some_and(|label| {
            !pystrip(label).is_empty() && label.len() <= 1024 && label.chars().count() <= 256
        })
        || string(row, "snapshot_digest").is_none_or(|value| Digest::parse(value).is_err())
    {
        return Err(ValidationError::Invalid);
    }
    let members = row
        .get("conversation_ids")
        .and_then(PyValue::as_array)
        .filter(|members| members.len() <= 256)
        .ok_or(ValidationError::Invalid)?;
    if integer(row.get("member_count")) != Some(members.len() as i64)
        || (kind == "chat" && (members.len() != 1 || members[0].as_str() != Some(id)))
    {
        return Err(ValidationError::Invalid);
    }
    let mut seen = BTreeSet::new();
    for member in members {
        if !member
            .as_str()
            .is_some_and(|id| identifier(id, false) && seen.insert(id))
        {
            return Err(ValidationError::Invalid);
        }
    }
    Ok(())
}

fn validate_task_context(
    context: &PyValue,
    conversation_id: &str,
    turn_id: &str,
) -> Result<(), ValidationError> {
    canonical(context, MAX_TASK_CONTEXT_BYTES)?;
    if !has_fields(
        context,
        &[
            "version",
            "source_id",
            "conversation_id",
            "recipient_id",
            "input_id",
            "source_revision",
            "generation",
            "items",
        ],
    ) || string(context, "version") != Some("tobkiri.saved-task-context.v1")
        || string(context, "conversation_id") != Some(conversation_id)
        || string(context, "input_id") != Some(turn_id)
        || string(context, "recipient_id")
            != Some(format!("conversation:{conversation_id}").as_str())
    {
        return Err(ValidationError::Invalid);
    }
    for key in ["source_id", "conversation_id", "recipient_id", "input_id"] {
        checked_id(context, key)?;
    }
    for key in ["source_revision", "generation"] {
        if integer(context.get(key)).is_none_or(|value| value < 0) {
            return Err(ValidationError::Invalid);
        }
    }
    let items = context
        .get("items")
        .and_then(PyValue::as_array)
        .filter(|items| (1..=100).contains(&items.len()))
        .ok_or(ValidationError::Invalid)?;
    let mut seen = BTreeSet::new();
    for item in items {
        if !has_fields(item, &["id", "kind", "body"])
            || !seen.insert(checked_id(item, "id")?)
            || !matches!(string(item, "kind"), Some("goal" | "todo" | "instruction"))
            || !string(item, "body")
                .is_some_and(|body| body.len() <= 32_000 && !pystrip(body).is_empty())
        {
            return Err(ValidationError::Invalid);
        }
    }
    Ok(())
}

fn optional_string(value: &PyValue, key: &str, allowed: &[&str]) -> bool {
    value
        .get(key)
        .is_none_or(|value| value.as_str().is_some_and(|value| allowed.contains(&value)))
}

fn checked_id<'a>(value: &'a PyValue, key: &str) -> Result<&'a str, ValidationError> {
    string(value, key)
        .filter(|id| identifier(id, false))
        .ok_or(ValidationError::Invalid)
}

/// Check the complete shape of the saved parent assertion.
pub fn validate_context_binding(value: &PyValue) -> Result<(), ValidationError> {
    if !has_fields(
        value,
        &[
            "version",
            "profile_id",
            "parent_conversation_id",
            "parent_revision",
            "context_digest",
        ],
    ) || value.get("version").and_then(PyValue::as_str) != Some(BINDING_VERSION)
        || !positive_integer(value.get("parent_revision"))
        || value
            .get("context_digest")
            .and_then(PyValue::as_str)
            .is_none_or(|digest| Digest::parse(digest).is_err())
    {
        return Err(ValidationError::Invalid);
    }
    checked_id(value, "profile_id")?;
    checked_id(value, "parent_conversation_id")?;
    Ok(())
}

/// Validate the original saved tool selection shape.
pub fn validate_tool_selection(value: &PyValue) -> Result<(), ValidationError> {
    let fields = value.as_object().ok_or(ValidationError::Invalid)?;
    let mode = value.get("mode").and_then(PyValue::as_str);
    if !matches!(mode, Some("auto" | "manual" | "none"))
        || fields.iter().any(|(key, _)| {
            !["mode", "scope", "include", "exclude", "must_use"].contains(&key.as_str())
        })
        || value
            .get("scope")
            .is_some_and(|scope| !matches!(scope.as_str(), Some("turn" | "conversation")))
        || value
            .get("must_use")
            .is_some_and(|required| !matches!(required, PyValue::Bool(_)))
    {
        return Err(ValidationError::Invalid);
    }
    for targets in ["include", "exclude"]
        .into_iter()
        .filter_map(|key| value.get(key))
    {
        let targets = targets.as_array().ok_or(ValidationError::Invalid)?;
        if targets.len() > 256 {
            return Err(ValidationError::Invalid);
        }
        for target in targets {
            let id = if matches!(target, PyValue::String(_)) {
                target.as_str()
            } else if has_fields(target, &["kind", "id"])
                && matches!(
                    target.get("kind").and_then(PyValue::as_str),
                    Some("tool" | "service")
                )
            {
                target.get("id").and_then(PyValue::as_str)
            } else {
                None
            };
            if !id.is_some_and(valid_tool_id) {
                return Err(ValidationError::Invalid);
            }
        }
    }
    if mode == Some("none")
        && (value.get("include").is_some_and(pytruthy)
            || value.get("must_use") == Some(&PyValue::Bool(true)))
    {
        return Err(ValidationError::Invalid);
    }
    Ok(())
}

fn canonical(value: &PyValue, maximum: usize) -> Result<CanonValue, ValidationError> {
    let encoded = dumps_bounded(value, maximum).map_err(|error| match error {
        CodecError::ByteLimit => ValidationError::Limit,
        CodecError::NodeLimit | CodecError::Resource => ValidationError::Compatibility,
        _ => ValidationError::Invalid,
    })?;
    tobkiri_canon::parse(encoded.as_bytes(), Variant::StrictV1)
        .map_err(|_| ValidationError::Invalid)
}

fn text_part(part: &PyValue) -> Option<&str> {
    if !has_fields(part, &["type", "text"]) || string(part, "type") != Some("text") {
        return None;
    }
    string(part, "text")
}

fn identifier(value: &str, allow_slash: bool) -> bool {
    let mut bytes = value.bytes();
    value.len() <= 256
        && bytes
            .next()
            .is_some_and(|byte| byte.is_ascii_alphanumeric())
        && bytes.all(|byte| {
            byte.is_ascii_alphanumeric() || b"._:-".contains(&byte) || (allow_slash && byte == b'/')
        })
}

fn valid_tool_id(value: &str) -> bool {
    identifier(value, true)
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

fn positive_integer(value: Option<&PyValue>) -> bool {
    matches!(value, Some(PyValue::Int(digits)) if digits != "0" && !digits.starts_with('-'))
}
