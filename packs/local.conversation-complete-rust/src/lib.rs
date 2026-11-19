//! Bounded request preparation and terminal projection for the original
//! single-hop conversation completion.

#[cfg(feature = "component")]
mod component;
mod projection;

use std::collections::BTreeMap;

use ordered_python_compat::pystrip;
use serde::{Deserialize, Deserializer};
use serde_json::value::RawValue;
use tobkiri_canon::{CanonError, CanonValue as Json, Variant};

const MAX_INITIAL_BYTES: usize = 1024 * 1024;
const MAX_REQUEST_BYTES: usize = 64 * 1024;
const MAX_RESULT_BYTES: usize = 512 * 1024;
const MAX_DEPTH: usize = 8;
const MAX_ITEMS: usize = 64;
const MAX_STRING_LENGTH: usize = 16 * 1024;
pub(crate) type Object = BTreeMap<String, Json>;

/// A failure to admit or compute an original conversation DATA document.
#[derive(Debug, PartialEq, Eq, thiserror::Error)]
pub enum Error {
    /// A field has a shape outside the original conversation DATA boundary.
    #[error("{0}")]
    Invalid(&'static str),
    /// The encoded document exceeds its byte budget.
    #[error("conversation completion exceeds its byte budget")]
    Capacity,
}

pub(crate) type Result<T> = std::result::Result<T, Error>;

#[derive(Deserialize)]
struct Initial<'a> {
    #[serde(borrow)]
    messages: &'a RawValue,
    #[serde(borrow, default, deserialize_with = "present")]
    model: Option<&'a RawValue>,
    #[serde(borrow, default, deserialize_with = "present")]
    continuation: Option<&'a RawValue>,
    #[serde(borrow, default, deserialize_with = "present")]
    bridge_result: Option<&'a RawValue>,
}

fn present<'de, D: Deserializer<'de>>(
    deserializer: D,
) -> std::result::Result<Option<&'de RawValue>, D::Error> {
    <&RawValue>::deserialize(deserializer).map(Some)
}

/// Prepare the generation request from a Host-admitted initial payload.
///
/// The retained messages are canonical wire DATA. Only those messages and an
/// optional bounded model reference enter the generation request. The caller
/// owns continuation identity and the decision to dispatch the request.
pub fn prepare(input: &str) -> Result<String> {
    if input.len() > MAX_INITIAL_BYTES {
        return Err(Error::Capacity);
    }
    if !input.trim_start().starts_with('{') {
        return Err(Error::Invalid("invalid initial turn"));
    }
    let initial: Initial<'_> =
        serde_json::from_str(input).map_err(|_| Error::Invalid("invalid initial turn"))?;
    if initial.continuation.is_some() || initial.bridge_result.is_some() {
        return Err(Error::Invalid("initial turn cannot include a continuation"));
    }

    // Host messages are already compact canonical JSON. Skip ignored fields
    // without allocating their trees and fence the retained array before
    // materializing it. The assembled request has its own encoded size check.
    if initial.messages.get().len() > MAX_REQUEST_BYTES {
        return Err(Error::Capacity);
    }
    let messages = parse(initial.messages.get())?;
    let Json::Arr(items) = &messages else {
        return Err(Error::Invalid("messages must be a non-empty array"));
    };
    if items.is_empty() {
        return Err(Error::Invalid("messages must be a non-empty array"));
    }
    if items.iter().any(|item| !matches!(item, Json::Obj(_))) {
        return Err(Error::Invalid("every message must be an object"));
    }
    validate_bounded(&messages, 0)?;
    let requirements = object([(
        "request_surface",
        Json::Str("defaultspack.conversation".to_owned()),
    )]);
    let mut request = Object::from([
        ("messages".to_owned(), messages),
        ("requirements".to_owned(), requirements),
    ]);
    if let Some(raw_model) = initial.model {
        let model: String = serde_json::from_str(raw_model.get())
            .map_err(|_| Error::Invalid("model must be a bounded nonempty reference"))?;
        if model.is_empty()
            || model != pystrip(&model)
            || !within_chars(&model, 256)
            || model.chars().any(|ch| ch.is_ascii_control())
        {
            return Err(Error::Invalid("model must be a bounded nonempty reference"));
        }
        request.insert("model_reference".to_owned(), Json::Str(model));
    }
    encode(&Json::Obj(request), MAX_REQUEST_BYTES)
}

/// Project an authenticated generation outcome to the original completion.
///
/// The caller supplies only the outcome. It must bind and consume the
/// one-shot continuation and verify the active authority before calling.
pub fn finish(input: &str) -> Result<String> {
    if input.len() > MAX_RESULT_BYTES {
        return Err(Error::Capacity);
    }
    let outcome = parse(input)?;
    let Json::Obj(mut fields) = outcome else {
        return Err(Error::Invalid("bridge result must be an object"));
    };
    if fields.len() != 2 {
        return Err(Error::Invalid("bridge result fields are invalid"));
    }
    let status = fields.remove("status");
    let result = match status.as_ref() {
        Some(Json::Str(status)) if status == "ok" => {
            let Some(Json::Obj(value)) = fields.remove("value") else {
                return Err(Error::Invalid("bridge success value must be an object"));
            };
            validate_object(&value, 0)?;
            projection::completion(value)?
        }
        Some(Json::Str(status)) if status == "error" => {
            let Some(Json::Obj(error)) = fields.remove("error") else {
                return Err(Error::Invalid("bridge error fields are invalid"));
            };
            project_error(error)?
        }
        _ => return Err(Error::Invalid("bridge result status is invalid")),
    };
    encode(&result, tobkiri_canon::MAX_BYTES)
}

fn project_error(error: Object) -> Result<Json> {
    if error.len() != 2 {
        return Err(Error::Invalid("bridge error fields are invalid"));
    }
    let Some(Json::Str(code)) = error.get("code") else {
        return Err(Error::Invalid("bridge error code is invalid"));
    };
    let mut code = code.bytes();
    if !code.next().is_some_and(|first| first.is_ascii_uppercase())
        || code.len() > 63
        || !code.all(|byte| byte.is_ascii_uppercase() || byte.is_ascii_digit() || byte == b'_')
    {
        return Err(Error::Invalid("bridge error code is invalid"));
    }
    let Some(Json::Str(message)) = error.get("message") else {
        return Err(Error::Invalid("bridge error message is invalid"));
    };
    if pystrip(message).is_empty() || !within_chars(message, 512) {
        return Err(Error::Invalid("bridge error message is invalid"));
    }
    Ok(object([
        ("content", Json::Arr(Vec::new())),
        ("tool_calls", Json::Arr(Vec::new())),
        ("error", Json::Obj(error)),
    ]))
}

fn validate_bounded(value: &Json, depth: usize) -> Result<()> {
    if depth > MAX_DEPTH {
        return Err(Error::Invalid("bridge JSON nesting exceeds the limit"));
    }
    match value {
        Json::Arr(values) => {
            if values.len() > MAX_ITEMS {
                return Err(Error::Invalid("bridge JSON array exceeds the limit"));
            }
            for value in values {
                validate_bounded(value, depth + 1)?;
            }
        }
        Json::Obj(values) => validate_object(values, depth)?,
        Json::Str(value) if !within_chars(value, MAX_STRING_LENGTH) => {
            return Err(Error::Invalid("bridge JSON string exceeds the limit"));
        }
        _ => {}
    }
    Ok(())
}

fn validate_object(values: &Object, depth: usize) -> Result<()> {
    if values.len() > MAX_ITEMS {
        return Err(Error::Invalid("bridge JSON object exceeds the limit"));
    }
    for (key, value) in values {
        if key.is_empty() || !within_chars(key, 128) {
            return Err(Error::Invalid("bridge JSON object key is invalid"));
        }
        validate_bounded(value, depth + 1)?;
    }
    Ok(())
}

fn within_chars(value: &str, limit: usize) -> bool {
    value.chars().nth(limit).is_none()
}

pub(crate) fn object<const N: usize>(fields: [(&str, Json); N]) -> Json {
    Json::Obj(
        fields
            .into_iter()
            .map(|(key, value)| (key.to_owned(), value))
            .collect(),
    )
}

fn parse(input: &str) -> Result<Json> {
    tobkiri_canon::parse(input.as_bytes(), Variant::StrictV1).map_err(json_error)
}

fn encode(value: &Json, limit: usize) -> Result<String> {
    let bytes = tobkiri_canon::encode(value, Variant::StrictV1).map_err(json_error)?;
    if bytes.len() > limit {
        return Err(Error::Capacity);
    }
    String::from_utf8(bytes).map_err(|_| Error::Invalid("invalid conversation output"))
}

fn json_error(error: CanonError) -> Error {
    match error {
        CanonError::Limit(_) => Error::Capacity,
        _ => Error::Invalid("invalid conversation wire JSON"),
    }
}

#[cfg(test)]
mod tests;
