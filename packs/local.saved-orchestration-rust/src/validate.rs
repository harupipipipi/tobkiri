//! Transport validation for the saved-conversation request and retained state.

mod references;

use base64::{
    Engine, alphabet, engine::GeneralPurpose, engine::general_purpose::GeneralPurposeConfig,
};
use serde_json::Value;
use std::collections::BTreeSet;

use crate::{Error, Result, check_canonical_size};

const MAX_INPUT_BYTES: usize = 3 * 1024 * 1024;
const MAX_TEXT_BYTES: usize = 60 * 1024;
const MAX_IMAGE_BYTES: usize = 1024 * 1024;
const MAX_IMAGE_BASE64_CHARS: usize = MAX_IMAGE_BYTES.div_ceil(3) * 4;
const MAX_TASK_CONTEXT_BYTES: usize = 48 * 1024;
const MAX_SAFE_INTEGER: i64 = (1 << 53) - 1;

// Python's strict decoder permits nonzero unused bits in the final sextet.
const IMAGE_BASE64: GeneralPurpose = GeneralPurpose::new(
    &alphabet::STANDARD,
    GeneralPurposeConfig::new().with_decode_allow_trailing_bits(true),
);

const REQUIRED: &[&str] = &["turn_id", "conversation_id", "conversation_revision"];
const OPTIONAL: &[&str] = &[
    "tool_selection",
    "strategy_reference",
    "thinking_level",
    "action_approval_mode",
    "task_context",
    "context_binding",
    "chat_references",
    "resolved_chat_references",
];

fn has_fields(value: &Value, fields: &[&str]) -> bool {
    value.as_object().is_some_and(|object| {
        object.len() == fields.len() && fields.iter().all(|key| object.contains_key(*key))
    })
}

fn matches_identifier(value: &str, punctuation: &[u8]) -> bool {
    let bytes = value.as_bytes();
    (1..=256).contains(&bytes.len())
        && bytes[0].is_ascii_alphanumeric()
        && bytes[1..]
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || punctuation.contains(byte))
}

/// Validate one saved conversation identifier without changing it.
pub(crate) fn identifier(value: &Value) -> Result<&str> {
    value
        .as_str()
        .filter(|value| matches_identifier(value, b"._:-"))
        .ok_or(Error::Invalid("saved conversation identity is invalid"))
}

/// Validate one positive saved conversation revision.
pub(crate) fn revision(value: &Value) -> Result<i64> {
    value
        .as_i64()
        .filter(|revision| *revision >= 1)
        .ok_or(Error::Invalid("saved conversation revision is invalid"))
}

/// Check for a prefixed lowercase SHA-256 digest.
pub(crate) fn is_digest(value: &str) -> bool {
    value.strip_prefix("sha256:").is_some_and(|hex| {
        hex.len() == 64
            && hex
                .bytes()
                .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
    })
}

/// Match Python's `bool(text.strip())`, including its C0 separator whitespace.
pub(crate) fn py_nonblank(value: &str) -> bool {
    value
        .chars()
        .any(|character| !character.is_whitespace() && !matches!(character, '\u{1c}'..='\u{1f}'))
}

fn saved_text(value: &Value) -> bool {
    value
        .as_str()
        .is_some_and(|text| text.len() <= MAX_TEXT_BYTES && py_nonblank(text))
}

fn saved_image_url(url: &str) -> bool {
    let Some((media_type, encoded)) = url
        .strip_prefix("data:")
        .and_then(|value| value.split_once(";base64,"))
    else {
        return false;
    };
    if encoded.is_empty()
        || encoded.len() > MAX_IMAGE_BASE64_CHARS
        || !encoded.len().is_multiple_of(4)
    {
        return false;
    }
    let Ok(decoded) = IMAGE_BASE64.decode(encoded) else {
        return false;
    };
    if decoded.is_empty() || decoded.len() > MAX_IMAGE_BYTES {
        return false;
    }
    match media_type {
        "image/png" => decoded.starts_with(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg" => decoded.starts_with(b"\xff\xd8\xff"),
        "image/gif" => decoded.starts_with(b"GIF87a") || decoded.starts_with(b"GIF89a"),
        "image/webp" => decoded.starts_with(b"RIFF") && decoded.get(8..12) == Some(b"WEBP"),
        _ => false,
    }
}

/// Recognize one text value followed by up to two bounded inline raster images.
pub(crate) fn saved_user_content(value: &Value) -> bool {
    if saved_text(value) {
        return true;
    }
    let Some(parts) = value
        .as_array()
        .filter(|parts| (1..=3).contains(&parts.len()))
    else {
        return false;
    };
    let text = &parts[0];
    has_fields(text, &["type", "text"])
        && text["type"].as_str() == Some("text")
        && saved_text(&text["text"])
        && parts[1..].iter().all(|part| {
            has_fields(part, &["type", "image_url"])
                && part["type"].as_str() == Some("image_url")
                && has_fields(&part["image_url"], &["url"])
                && part["image_url"]["url"]
                    .as_str()
                    .is_some_and(saved_image_url)
        })
}

fn context_binding(value: &Value) -> Result<()> {
    if !has_fields(
        value,
        &[
            "version",
            "profile_id",
            "parent_conversation_id",
            "parent_revision",
            "context_digest",
        ],
    ) || value["version"].as_str() != Some("tobkiri.conversation-context-binding.v1")
        || !value["context_digest"].as_str().is_some_and(is_digest)
    {
        return Err(Error::Invalid(
            "saved conversation context binding is invalid",
        ));
    }
    identifier(&value["profile_id"])?;
    identifier(&value["parent_conversation_id"])?;
    revision(&value["parent_revision"])?;
    Ok(())
}

fn task_context(value: &Value, conversation_id: &str, turn_id: &str) -> Result<()> {
    check_canonical_size(value, MAX_TASK_CONTEXT_BYTES)?;
    let recipient_id = format!("conversation:{conversation_id}");
    if !has_fields(
        value,
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
    ) {
        return Err(Error::Invalid("saved task context fields are invalid"));
    }
    if value["version"].as_str() != Some("tobkiri.saved-task-context.v1")
        || value["conversation_id"].as_str() != Some(conversation_id)
        || value["recipient_id"].as_str() != Some(recipient_id.as_str())
        || value["input_id"].as_str() != Some(turn_id)
    {
        return Err(Error::Invalid("saved task context scope is invalid"));
    }
    for key in ["source_id", "recipient_id"] {
        identifier(&value[key])
            .map_err(|_| Error::Invalid("saved task context identity is invalid"))?;
    }
    for key in ["source_revision", "generation"] {
        if !value[key]
            .as_i64()
            .is_some_and(|number| (0..=MAX_SAFE_INTEGER).contains(&number))
        {
            return Err(Error::Invalid("saved task context revision is invalid"));
        }
    }
    let items = value["items"]
        .as_array()
        .filter(|items| (1..=100).contains(&items.len()))
        .ok_or(Error::Invalid("saved task context items are invalid"))?;
    let mut seen = BTreeSet::new();
    for item in items {
        if !has_fields(item, &["id", "kind", "body"])
            || !identifier(&item["id"]).is_ok_and(|id| seen.insert(id))
            || !matches!(item["kind"].as_str(), Some("goal" | "todo" | "instruction"))
            || !item["body"]
                .as_str()
                .is_some_and(|body| body.len() <= 32_000 && py_nonblank(body))
        {
            return Err(Error::Invalid("saved task context item is invalid"));
        }
    }
    Ok(())
}

fn tool_selection(value: &Value) -> Result<()> {
    let selection = value
        .as_object()
        .ok_or(Error::Invalid("saved tool selection is invalid"))?;
    if selection
        .keys()
        .any(|key| !["mode", "include", "exclude", "scope", "must_use"].contains(&key.as_str()))
        || !matches!(value["mode"].as_str(), Some("auto" | "manual" | "none"))
        || value
            .get("scope")
            .is_some_and(|scope| !matches!(scope.as_str(), Some("turn" | "conversation")))
        || value
            .get("must_use")
            .is_some_and(|value| !value.is_boolean())
    {
        return Err(Error::Invalid("saved tool selection is invalid"));
    }
    for key in ["include", "exclude"] {
        let Some(targets) = selection.get(key) else {
            continue;
        };
        let targets = targets
            .as_array()
            .filter(|targets| targets.len() <= 256)
            .ok_or(Error::Invalid("saved tool selection is invalid"))?;
        for target in targets {
            let id = if has_fields(target, &["kind", "id"])
                && matches!(target["kind"].as_str(), Some("tool" | "service"))
            {
                &target["id"]
            } else {
                target
            };
            if !id
                .as_str()
                .is_some_and(|id| matches_identifier(id, b"._:/-"))
            {
                return Err(Error::Invalid("saved tool target is invalid"));
            }
        }
    }
    if value["mode"].as_str() == Some("none")
        && (value["include"]
            .as_array()
            .is_some_and(|items| !items.is_empty())
            || value["must_use"].as_bool() == Some(true))
    {
        return Err(Error::Invalid("disabled saved tools cannot be required"));
    }
    Ok(())
}

fn request_fields(value: &Value, initial: bool) -> bool {
    value.as_object().is_some_and(|object| {
        REQUIRED.iter().all(|field| object.contains_key(*field))
            && object.contains_key("content") == initial
            && object.keys().all(|key| {
                (initial && key == "content")
                    || REQUIRED.contains(&key.as_str())
                    || OPTIONAL.contains(&key.as_str())
            })
    })
}

fn request_options(value: &Value) -> Result<()> {
    if let Some(binding) = value.get("context_binding") {
        context_binding(binding)?;
    }
    if let Some(strategy) = value
        .get("strategy_reference")
        .filter(|value| !value.is_null())
    {
        identifier(strategy)?;
    }
    if value
        .get("action_approval_mode")
        .is_some_and(|mode| !matches!(mode.as_str(), Some("ask" | "agent" | "full")))
    {
        return Err(Error::Invalid(
            "saved conversation action approval mode is invalid",
        ));
    }
    if value.get("thinking_level").is_some_and(|level| {
        !matches!(
            level.as_str(),
            Some("none" | "low" | "medium" | "high" | "xhigh")
        )
    }) {
        return Err(Error::Invalid(
            "saved conversation thinking level is invalid",
        ));
    }
    Ok(())
}

/// Check the exact initial request shape, bound scopes, and continuation byte budget.
pub(crate) fn request(value: &Value) -> Result<()> {
    if !request_fields(value, true) {
        return Err(Error::Invalid(
            "saved conversation request fields are invalid",
        ));
    }
    references::validate(value)?;
    request_options(value)?;
    let turn_id = identifier(&value["turn_id"])?;
    let conversation_id = identifier(&value["conversation_id"])?;
    revision(&value["conversation_revision"])?;
    if let Some(context) = value.get("task_context") {
        task_context(context, conversation_id, turn_id)?;
    }
    if !saved_user_content(&value["content"]) {
        return Err(Error::Invalid("saved conversation user content is invalid"));
    }
    if let Some(selection) = value.get("tool_selection") {
        tool_selection(selection)?;
    }
    check_canonical_size(value, MAX_INPUT_BYTES)?;
    Ok(())
}

/// Check the compact request stored in a continuation after separating user content.
pub(crate) fn state_request(value: &Value) -> Result<()> {
    if !request_fields(value, false) {
        return Err(Error::Invalid(
            "saved continuation request fields are invalid",
        ));
    }
    let turn_id = identifier(&value["turn_id"])?;
    let conversation_id = identifier(&value["conversation_id"])?;
    revision(&value["conversation_revision"])?;
    request_options(value)?;

    if [
        "tool_selection",
        "task_context",
        "chat_references",
        "resolved_chat_references",
    ]
    .iter()
    .any(|key| value.get(*key).is_some())
    {
        references::validate(value)?;
        if let Some(context) = value.get("task_context") {
            task_context(context, conversation_id, turn_id)?;
        }
        if let Some(selection) = value.get("tool_selection") {
            tool_selection(selection)?;
        }
        // The reference validator reintroduces `"content":"saved content"`
        // solely for its 3 MiB check. Account for it without cloning the request.
        const CONTENT_OVERHEAD: usize = ",\"content\":\"saved content\"".len();
        check_canonical_size(value, MAX_INPUT_BYTES - CONTENT_OVERHEAD)?;
    }
    Ok(())
}

#[cfg(test)]
mod tests;
