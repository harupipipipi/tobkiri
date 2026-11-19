//! Saved Conversation computation, independent of the executor of its intents.

mod intent;
mod transition;
mod validate;

#[cfg(feature = "component")]
mod component;

use std::io::{self, Write};

use serde_json::Value;
use tobkiri_canon::{CanonValue, MAX_SAFE_INTEGER, Variant};

pub(crate) const STATE_LIMIT: usize = 4 * 1024 * 1024;
const MAX_STEP_DEPTH: usize = 12;

/// Failure of the computation envelope before it can yield an outcome.
#[derive(Debug, PartialEq, Eq, thiserror::Error)]
pub enum Error {
    /// Malformed or unsupported data at the saved computation boundary.
    #[error("{0}")]
    Invalid(&'static str),
    /// The encoded step exceeds the original guest boundary.
    #[error("saved conversation exceeds continuation budget")]
    Capacity,
}

/// Result of the isolated saved-turn computation.
pub type Result<T> = std::result::Result<T, Error>;

/// Execute one bounded saved-conversation step.
///
/// The argument is the original saved guest ABI document. Only its owner may
/// pass retained state and an authenticated Host outcome to this computation.
/// Returned intents are DATA. The continuation owner still validates each hop
/// before using an existing signed Broker edge to dispatch it.
pub fn advance(input: &str) -> Result<String> {
    if input.len() > STATE_LIMIT {
        return Err(Error::Capacity);
    }
    let document = tobkiri_canon::parse(input.as_bytes(), Variant::StrictV1)
        .map_err(|_| Error::Invalid("invalid saved conversation ABI payload"))?;
    let Value::Object(mut fields) = from_canonical(document)? else {
        return Err(Error::Invalid("invalid saved conversation ABI payload"));
    };
    let output = match fields.len() {
        1 => match fields.remove("request") {
            Some(request) => transition::start(request)?,
            None => return Err(Error::Invalid("invalid saved conversation ABI payload")),
        },
        2 => match (fields.remove("state"), fields.remove("outcome")) {
            (Some(state), Some(outcome)) => transition::resume(state, outcome)?,
            _ => return Err(Error::Invalid("invalid saved conversation ABI payload")),
        },
        _ => return Err(Error::Invalid("invalid saved conversation ABI payload")),
    };
    let output = serialize_bounded(&output, STATE_LIMIT)?;
    String::from_utf8(output).map_err(|_| Error::Invalid("invalid saved conversation JSON output"))
}

fn from_canonical(value: CanonValue) -> Result<Value> {
    Ok(match value {
        CanonValue::Null => Value::Null,
        CanonValue::Bool(value) => Value::Bool(value),
        CanonValue::Int(value) => Value::from(value),
        CanonValue::Str(value) => Value::String(value),
        CanonValue::Arr(values) => Value::Array(
            values
                .into_iter()
                .map(from_canonical)
                .collect::<Result<_>>()?,
        ),
        CanonValue::Obj(values) => Value::Object(
            values
                .into_iter()
                .map(|(key, value)| from_canonical(value).map(|value| (key, value)))
                .collect::<Result<_>>()?,
        ),
        CanonValue::BigInt(_) | CanonValue::Float(_) => {
            return Err(Error::Invalid(
                "saved conversation value is not strict JSON",
            ));
        }
    })
}

pub(crate) fn canonical_bytes(value: &Value, limit: usize) -> Result<Vec<u8>> {
    validate_step_json(value, 0)?;
    serialize_bounded(value, limit)
}

pub(crate) fn check_canonical_size(value: &Value, limit: usize) -> Result<()> {
    validate_step_json(value, 0)?;
    write_bounded(io::sink(), value, limit).map(drop)
}

fn validate_step_json(value: &Value, depth: usize) -> Result<()> {
    if depth > MAX_STEP_DEPTH {
        return Err(Error::Invalid("saved conversation nesting exceeds limit"));
    }
    match value {
        Value::Array(items) => {
            for item in items {
                validate_step_json(item, depth + 1)?;
            }
        }
        Value::Object(fields) => {
            for value in fields.values() {
                validate_step_json(value, depth + 1)?;
            }
        }
        Value::Number(number)
            if number
                .as_i64()
                .is_none_or(|integer| integer.unsigned_abs() > MAX_SAFE_INTEGER) =>
        {
            return Err(Error::Invalid(
                "saved conversation value is not strict JSON",
            ));
        }
        _ => {}
    }
    Ok(())
}

struct BoundedJson<W> {
    writer: W,
    remaining: usize,
}

impl<W: Write> Write for BoundedJson<W> {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if bytes.len() > self.remaining {
            return Err(io::Error::other("saved conversation JSON exceeds limit"));
        }
        self.writer.write_all(bytes)?;
        self.remaining -= bytes.len();
        Ok(bytes.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        self.writer.flush()
    }
}

fn serialize_bounded(value: &Value, limit: usize) -> Result<Vec<u8>> {
    write_bounded(Vec::new(), value, limit)
}

fn write_bounded<W: Write>(writer: W, value: &Value, limit: usize) -> Result<W> {
    // serde_json's default object map orders keys by UTF-8, which preserves
    // the Unicode code-point order of the owner's sort_keys=True JSON.
    let mut json = BoundedJson {
        writer,
        remaining: limit,
    };
    serde_json::to_writer(&mut json, value).map_err(|error| {
        if error.is_io() {
            Error::Capacity
        } else {
            Error::Invalid("invalid saved conversation JSON")
        }
    })?;
    Ok(json.writer)
}

pub(crate) fn py_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_i64() != Some(0),
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
    }
}

#[cfg(test)]
mod tests;
