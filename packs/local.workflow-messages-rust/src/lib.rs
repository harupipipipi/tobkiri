//! Build the selected model-message history for the saved-chat Workflow stage.

#[cfg(feature = "component")]
mod component;
mod context;
mod transcript;

use std::collections::{BTreeMap, BTreeSet};

use base64::{
    Engine, alphabet, engine::GeneralPurpose, engine::general_purpose::GeneralPurposeConfig,
};
use ordered_python_compat::pystrip;
use tobkiri_canon::{CanonError, CanonValue as Json, Variant};

const MAX_BYTES: usize = 512 * 1024;
const MAX_DEPTH: usize = 32;
const MAX_SAVED_TEXT_BYTES: usize = 60 * 1024;
const MAX_SAVED_IMAGE_COUNT: usize = 2;
const MAX_SAVED_IMAGE_BYTES: usize = 1024 * 1024;
const MAX_SAVED_IMAGE_BASE64_CHARS: usize = MAX_SAVED_IMAGE_BYTES.div_ceil(3) * 4;
const IMAGE_OMITTED: &str = "[Earlier inline image omitted from this saved-turn context because of the bounded image limit.]";

// Python's validate=True accepts unused bits in the last base64 symbol.
const IMAGE_BASE64: GeneralPurpose = GeneralPurpose::new(
    &alphabet::STANDARD,
    GeneralPurposeConfig::new().with_decode_allow_trailing_bits(true),
);

type Result<T> = std::result::Result<T, Error>;

/// A rejected document or an exceeded byte budget.
#[derive(Debug, PartialEq, Eq, thiserror::Error)]
pub enum Error {
    /// The original stage rejected the supplied message data.
    #[error("{0}")]
    Invalid(&'static str),
    /// The stage rejected an invalid canonical JSON value.
    #[error("{0}")]
    Canonical(String),
    /// The supplied document or projection exceeded a byte limit.
    #[error("{0}")]
    Capacity(&'static str),
}

/// Validate a resolved conversation and assemble its model messages.
///
/// Owner capabilities cannot be reconstructed from the JSON input.
pub fn build(input: &str) -> Result<String> {
    if input.len() > MAX_BYTES {
        return Err(Error::Capacity("json input exceeds size limit"));
    }
    let payload = parse(input)?;
    if !has_fields(&payload, &["conversation", "system_prompt"]) {
        return Err(Error::Invalid("conversation message fields are invalid"));
    }
    if encode(&payload)?.len() > MAX_BYTES {
        return Err(Error::Capacity("json input exceeds size limit"));
    }
    check_depth(&payload, MAX_DEPTH)?;

    let fields = payload.as_object().expect("payload fields");
    let conversation = &fields["conversation"];
    if conversation.as_object().is_none() || !optional_object(conversation.get("metadata")) {
        return Err(Error::Invalid("resolved conversation is required"));
    }
    let prompt = context::validate_prompt(&fields["system_prompt"])?;
    let history = conversation
        .get("messages")
        .and_then(array)
        .filter(|messages| {
            messages.iter().all(|message| {
                string(message, "role").is_some() && optional_object(message.get("metadata"))
            })
        })
        .ok_or(Error::Invalid("resolved conversation messages are invalid"))?;

    let mut messages = context::prefix(conversation, history, prompt)?;
    let selected = selected_history(conversation.get("current_node_id"), history)?;
    for message in selected {
        append_message(&mut messages, message)?;
    }
    limit_inline_images(&mut messages);

    let result = encode(&object([("messages", Json::Arr(messages))]))?;
    if result.len() > MAX_BYTES {
        return Err(Error::Capacity("assembled messages exceed the byte limit"));
    }
    Ok(result)
}

fn selected_history<'a>(current: Option<&Json>, history: &'a [Json]) -> Result<Vec<&'a Json>> {
    if history.is_empty() {
        return if optional_null(current) {
            Ok(Vec::new())
        } else {
            Err(Error::Invalid(
                "saved bridge empty history has a current node",
            ))
        };
    }

    let mut by_id = BTreeMap::new();
    for (index, message) in history.iter().enumerate() {
        let id = string(message, "id")
            .filter(|id| !id.is_empty())
            .ok_or(Error::Invalid(
                "saved bridge owner message identity is invalid",
            ))?;
        if by_id.insert(id, index).is_some() {
            return Err(Error::Invalid(
                "saved bridge owner message identity is invalid",
            ));
        }
    }
    let current = current
        .and_then(Json::as_str)
        .and_then(|id| by_id.get(id))
        .ok_or(Error::Invalid(
            "saved bridge current message is unavailable",
        ))?;
    if history
        .iter()
        .all(|message| optional_null(message.get("parent_id")))
    {
        return Ok(history[..=*current].iter().collect());
    }

    const INVALID_BRANCH: Error = Error::Invalid("saved bridge history is cyclic or incomplete");
    let mut selected = vec![&history[*current]];
    let mut seen = BTreeSet::from([*current]);
    while let Some(parent) = selected
        .last()
        .and_then(|message| message.get("parent_id"))
        .filter(|parent| !matches!(parent, Json::Null))
    {
        let index = parent
            .as_str()
            .and_then(|id| by_id.get(id))
            .filter(|index| seen.insert(**index))
            .ok_or(INVALID_BRANCH)?;
        selected.push(&history[*index]);
    }
    selected.reverse();
    Ok(selected)
}

fn append_message(result: &mut Vec<Json>, message: &Json) -> Result<()> {
    let role = string(message, "role").expect("validated history role");
    let content = message.get("content");
    if !matches!(role, "system" | "user" | "assistant")
        || string(message, "status") != Some("complete")
        || !content.is_some_and(|value| {
            text_content(value, false) || (role == "user" && saved_user_content(value))
        })
        || truthy(message.get("parts"))
        || truthy(message.get("widget"))
    {
        return Err(Error::Invalid(
            "saved bridge additional content resolution is required",
        ));
    }
    let trace = message
        .get("metadata")
        .and_then(|metadata| metadata.get("saved_tool_messages"));
    result.extend(
        transcript::validated_trace(trace, message.get("tool_logs"), role)?
            .iter()
            .cloned(),
    );
    result.push(model_message(
        role,
        content.expect("validated content").clone(),
    ));
    Ok(())
}

fn text_content(value: &Json, empty_blocks_allowed: bool) -> bool {
    value.as_str().is_some()
        || array(value).is_some_and(|parts| {
            (empty_blocks_allowed || !parts.is_empty()) && parts.iter().all(text_part)
        })
}

fn text_part(value: &Json) -> bool {
    has_fields(value, &["type", "text"])
        && string(value, "type") == Some("text")
        && string(value, "text").is_some()
}

// The caller already accepts any text-only content. Only a message with
// non-text blocks needs the stricter saved-user shape and raster checks.
fn saved_user_content(value: &Json) -> bool {
    let Some(parts) =
        array(value).filter(|parts| (1..=1 + MAX_SAVED_IMAGE_COUNT).contains(&parts.len()))
    else {
        return false;
    };
    text_part(&parts[0])
        && string(&parts[0], "text")
            .is_some_and(|text| text.len() <= MAX_SAVED_TEXT_BYTES && !pystrip(text).is_empty())
        && parts[1..].iter().all(|part| {
            has_fields(part, &["type", "image_url"])
                && string(part, "type") == Some("image_url")
                && part.get("image_url").is_some_and(|image| {
                    has_fields(image, &["url"]) && string(image, "url").is_some_and(saved_image_url)
                })
        })
}

fn saved_image_url(url: &str) -> bool {
    let Some((media_type, encoded)) = url
        .strip_prefix("data:")
        .and_then(|data| data.split_once(";base64,"))
    else {
        return false;
    };
    if !matches!(
        media_type,
        "image/png" | "image/jpeg" | "image/gif" | "image/webp"
    ) || encoded.is_empty()
        || encoded.len() > MAX_SAVED_IMAGE_BASE64_CHARS
        || !encoded.len().is_multiple_of(4)
    {
        return false;
    }
    let Ok(decoded) = IMAGE_BASE64.decode(encoded) else {
        return false;
    };
    !decoded.is_empty()
        && decoded.len() <= MAX_SAVED_IMAGE_BYTES
        && match media_type {
            "image/png" => decoded.starts_with(b"\x89PNG\r\n\x1a\n"),
            "image/jpeg" => decoded.starts_with(b"\xff\xd8\xff"),
            "image/gif" => decoded.starts_with(b"GIF87a") || decoded.starts_with(b"GIF89a"),
            "image/webp" => decoded.starts_with(b"RIFF") && decoded.get(8..12) == Some(b"WEBP"),
            _ => false,
        }
}

fn limit_inline_images(messages: &mut [Json]) {
    let mut retained = 0;
    for message in messages.iter_mut().rev() {
        let Json::Obj(message) = message else {
            continue;
        };
        let Some(Json::Arr(blocks)) = message.get_mut("content") else {
            continue;
        };
        let mut omitted = false;
        // Reverse the predicate traversal so surviving images are the newest
        // within the message as well as across the selected history.
        blocks.reverse();
        blocks.retain(|block| {
            if string(block, "type") != Some("image_url") {
                return true;
            }
            if retained == MAX_SAVED_IMAGE_COUNT {
                omitted = true;
                return false;
            }
            retained += 1;
            true
        });
        blocks.reverse();
        if omitted {
            blocks.push(object([
                ("type", "text".into()),
                ("text", IMAGE_OMITTED.into()),
            ]));
        }
    }
}

fn parse(input: &str) -> Result<Json> {
    tobkiri_canon::parse(input.as_bytes(), Variant::StrictV1).map_err(canonical_error)
}

fn encode(value: &Json) -> Result<String> {
    let bytes = tobkiri_canon::encode(value, Variant::StrictV1).map_err(canonical_error)?;
    String::from_utf8(bytes).map_err(|_| Error::Invalid("json string contains invalid Unicode"))
}

fn canonical_error(error: CanonError) -> Error {
    match error {
        CanonError::Limit("nesting depth") => Error::Invalid("json nesting exceeds depth limit"),
        CanonError::Limit(_) => Error::Capacity("canonical JSON exceeds size limit"),
        CanonError::Invalid(message) if message == "floating point values are not permitted" => {
            Error::Invalid(
                "floating point values are not permitted; use an exact string or integer",
            )
        }
        CanonError::Invalid(message) if message == "integer exceeds I-JSON safe range" => {
            Error::Invalid("integer exceeds I-JSON safe range; use a canonical decimal string")
        }
        CanonError::Invalid(message)
            if matches!(
                message.as_str(),
                "invalid Unicode string" | "unpaired surrogate"
            ) =>
        {
            Error::Invalid("json string contains invalid Unicode")
        }
        error => Error::Canonical(error.to_string()),
    }
}

// Python counts the root at depth zero and only visits actual descendants.
fn check_depth(value: &Json, remaining: usize) -> Result<()> {
    let next = || {
        remaining
            .checked_sub(1)
            .ok_or(Error::Invalid("json nesting exceeds depth limit"))
    };
    match value {
        Json::Arr(items) => {
            for item in items {
                check_depth(item, next()?)?;
            }
        }
        Json::Obj(fields) => {
            for value in fields.values() {
                check_depth(value, next()?)?;
            }
        }
        _ => {}
    }
    Ok(())
}

fn optional_object(value: Option<&Json>) -> bool {
    matches!(value, None | Some(Json::Null | Json::Obj(_)))
}

fn optional_null(value: Option<&Json>) -> bool {
    matches!(value, None | Some(Json::Null))
}

fn truthy(value: Option<&Json>) -> bool {
    match value {
        None | Some(Json::Null) => false,
        Some(Json::Bool(value)) => *value,
        Some(Json::Int(value)) => *value != 0,
        Some(Json::Str(value)) => !value.is_empty(),
        Some(Json::Arr(value)) => !value.is_empty(),
        Some(Json::Obj(value)) => !value.is_empty(),
        // Strict input rejects all other number types.
        Some(Json::Float(_) | Json::BigInt(_)) => unreachable!(),
    }
}

fn has_fields(value: &Json, fields: &[&str]) -> bool {
    value.as_object().is_some_and(|object| {
        object.len() == fields.len() && fields.iter().all(|key| object.contains_key(*key))
    })
}

fn string<'a>(value: &'a Json, key: &str) -> Option<&'a str> {
    value.get(key).and_then(Json::as_str)
}

fn array(value: &Json) -> Option<&[Json]> {
    match value {
        Json::Arr(items) => Some(items),
        _ => None,
    }
}

fn identifier(value: &str, first_underscore: bool, allowed_punctuation: &[u8]) -> bool {
    let bytes = value.as_bytes();
    (1..=256).contains(&bytes.len())
        && (bytes[0].is_ascii_alphanumeric() || (first_underscore && bytes[0] == b'_'))
        && bytes[1..]
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || allowed_punctuation.contains(byte))
}

fn object<'a>(fields: impl IntoIterator<Item = (&'a str, Json)>) -> Json {
    Json::Obj(
        fields
            .into_iter()
            .map(|(key, value)| (key.into(), value))
            .collect(),
    )
}

fn model_message(role: &str, content: Json) -> Json {
    object([("role", role.into()), ("content", content)])
}
