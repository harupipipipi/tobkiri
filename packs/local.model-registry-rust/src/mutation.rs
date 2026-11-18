//! Stage only the Settings create and output-policy save operations.

use std::collections::HashMap;

use ordered_python_compat::{
    CodecError, CoercionError, MAX_INTEGER_DIGITS, PyValue, decimal_digit_count, dumps, pyint,
    pyrepr, pystr, pytruthy,
};
use sha2::{Digest, Sha256};

use crate::{
    SourceError,
    registry::{
        Fields, Registry, copy_dictionary, dictionary_pair, field, identifier, object, string,
        type_name,
    },
};

impl Registry {
    /// Return the owner result and the replacement document, when the source would write it.
    pub(crate) fn apply_settings(
        &self,
        request: &PyValue,
        now: &str,
    ) -> Result<(Option<PyValue>, PyValue), SourceError> {
        let create = match request.get("operation").and_then(PyValue::as_str) {
            Some("create") => true,
            Some("save") => false,
            _ => return Err(value_error("model registry operation payload is invalid")),
        };
        let record = request
            .get("record")
            .filter(|record| record.as_object().is_some())
            .ok_or_else(|| value_error("model configuration fields are invalid"))?;
        let provider = create.then(|| creation_provider(record)).transpose()?;
        let mut normalized = profile_record(record)?;
        let state = self.read()?;
        let expected = integer_or_zero(request.get("expected_revision"))?;
        if integer_or_zero(state.revision)? != expected {
            return Err(conflict("model registry revision is stale"));
        }
        let profile_id = normalized
            .get("model_profile_id")
            .and_then(PyValue::as_str)
            .expect("normalized profile identity")
            .to_owned();
        let current = field(state.profiles, &profile_id);
        if let Some(provider) = provider {
            if let Some(current) = current.filter(|value| !matches!(value, PyValue::Null)) {
                check_created_route(current, &normalized, &provider)?;
                return Ok((None, saved(current.clone(), state.revision()?)));
            }
            if field(state.aliases, &profile_id).is_some() {
                return Err(conflict("model profile ID is already reserved"));
            }
        } else if let Some(current) = current.filter(|current| current.as_object().is_some()) {
            if matches!(request.get("preserve_existing"), Some(PyValue::Bool(true))) {
                if record.get("enabled").is_some()
                    && matches!(normalized.get("enabled"), Some(PyValue::Bool(true)))
                {
                    let connection = provider_connection(&normalized);
                    if connection.is_none()
                        || current.get("model_id") != normalized.get("model_id")
                        || provider_connection(current) != connection
                    {
                        return Err(value_error("model profile route identity changed"));
                    }
                }
                normalized = profile_record(&merge_record(current, record)?)?;
            }
            if normalized
                .as_object()
                .expect("normalized record fields")
                .iter()
                .all(|(key, value)| python_equal(current.get(key).unwrap_or(&PyValue::Null), value))
            {
                return Ok((None, saved(current.clone(), state.revision()?)));
            }
        }

        let current = current.filter(|current| pytruthy(current));
        let created_at = current
            .map(|current| dictionary_get(current, "created_at"))
            .transpose()?
            .flatten()
            .filter(|value| pytruthy(value))
            .map_or_else(|| now.to_owned(), pystr);
        let previous_revision = current.and_then(|current| current.get("record_revision"));
        let record_revision = increment_digits(&integer_or_zero(previous_revision)?);
        let mut normalized = normalized.into_object().expect("normalized record fields");
        normalized.push(("created_at".into(), PyValue::String(created_at)));
        normalized.push(("updated_at".into(), string(now)));
        normalized.push(("record_revision".into(), PyValue::Int(record_revision)));
        let mut normalized = PyValue::Object(normalized);
        let encoded = dumps(&sorted_json(&normalized)).map_err(hash_codec_error)?;
        let hash = format!("sha256:{:x}", Sha256::digest(encoded.as_bytes()));
        set_field(&mut normalized, "record_hash", PyValue::String(hash));
        let revision = increment_store_revision(&state.revision()?)?;

        let mut document = self.document.clone();
        let PyValue::Object(document_fields) = &mut document else {
            unreachable!("read validated the document object")
        };
        let profiles = document_fields
            .iter_mut()
            .find(|(name, _)| name == "profiles")
            .map(|(_, value)| value)
            .expect("read validated profiles");
        set_field(profiles, &profile_id, normalized.clone());
        set_field(&mut document, "revision", revision.clone());
        Ok((Some(document), saved(normalized, revision)))
    }
}

fn creation_provider(record: &PyValue) -> Result<String, SourceError> {
    if record
        .as_object()
        .expect("settings record object")
        .iter()
        .any(|(key, _)| {
            !matches!(
                key.as_str(),
                "model_profile_id" | "model_id" | "display_name" | "metadata"
            )
        })
    {
        return Err(value_error("model route creation fields are invalid"));
    }
    let Some([(key, provider)]) = record.get("metadata").and_then(PyValue::as_object) else {
        return Err(value_error("model route provider connection is invalid"));
    };
    if key != "provider_connection_id" {
        return Err(value_error("model route provider connection is invalid"));
    }
    Ok(identifier(provider)?.into_owned())
}

fn check_created_route(
    current: &PyValue,
    normalized: &PyValue,
    provider: &str,
) -> Result<(), SourceError> {
    let empty = PyValue::Object(Vec::new());
    let metadata = dictionary_get(current, "metadata")?.unwrap_or(&empty);
    let mut current_provider = dictionary_get(metadata, "provider_connection_id")?;
    if current_provider.is_none_or(|value| !pytruthy(value)) {
        let requirements = dictionary_get(current, "requirements")?.unwrap_or(&empty);
        current_provider = dictionary_get(requirements, "preferred_provider_instance_id")?;
    }
    if !matches!(current.get("enabled"), Some(PyValue::Bool(true)))
        || current.get("model_id") != normalized.get("model_id")
        || current.get("display_name") != normalized.get("display_name")
        || current_provider.and_then(PyValue::as_str) != Some(provider)
    {
        return Err(conflict("model profile ID is already reserved"));
    }
    Ok(())
}

fn provider_connection(record: &PyValue) -> Option<&str> {
    record
        .get("metadata")
        .and_then(|metadata| metadata.get("provider_connection_id"))
        .filter(|connection| pytruthy(connection))
        .or_else(|| {
            record
                .get("requirements")
                .and_then(|requirements| requirements.get("preferred_provider_instance_id"))
        })
        .and_then(PyValue::as_str)
        .filter(|connection| !connection.is_empty())
}

fn profile_record(record: &PyValue) -> Result<PyValue, SourceError> {
    let default_enabled = PyValue::Bool(true);
    let enabled = record.get("enabled").unwrap_or(&default_enabled);
    if !matches!(enabled, PyValue::Bool(_)) {
        return Err(value_error("model profile enabled must be a boolean"));
    }
    let missing = PyValue::Null;
    let profile_id = identifier(
        first_truthy(record, ["model_profile_id", "profile_id", "id"]).unwrap_or(&missing),
    )?;
    let model_id = identifier(first_truthy(record, ["model_id", "model"]).unwrap_or(&missing))?;
    let null = PyValue::Null;
    let credential = record.get("credential_handle").unwrap_or(&null);
    if !matches!(credential, PyValue::Null) {
        let handle = pystr(credential);
        if !handle.starts_with("credential:") && !handle.starts_with("opaque:") {
            return Err(value_error(
                "model profile credential must be an opaque handle",
            ));
        }
    }
    let display_name = first_truthy(record, ["display_name"])
        .map_or_else(|| profile_id.to_string(), pystr)
        .chars()
        .take(200)
        .collect();
    let requirements = select_fields(record.get("requirements"), |key| {
        matches!(
            key,
            "modalities"
                | "capabilities"
                | "tool_calling"
                | "thinking"
                | "minimum_context"
                | "request_surface"
                | "data_residency"
                | "maximum_cost"
                | "preferred_model_id"
                | "preferred_provider_instance_id"
                | "health_max_age"
        )
    });
    let parameters = safe_parameters(record.get("parameters"))?;
    Ok(object([
        ("model_profile_id", string(&profile_id)),
        ("display_name", PyValue::String(display_name)),
        ("model_id", string(&model_id)),
        ("requirements", requirements),
        ("credential_handle", credential.clone()),
        ("parameters", parameters),
        ("enabled", enabled.clone()),
        (
            "metadata",
            select_fields(record.get("metadata"), safe_scalar_key),
        ),
    ]))
}

fn first_truthy<'a, const N: usize>(record: &'a PyValue, keys: [&str; N]) -> Option<&'a PyValue> {
    keys.into_iter()
        .filter_map(|key| record.get(key))
        .find(|value| pytruthy(value))
}

// Every decoded PyValue is a JSON type, so the source's recursive _json_safe
// test holds for descendants as well as scalar fields here.
fn select_fields(value: Option<&PyValue>, mut keep: impl FnMut(&str) -> bool) -> PyValue {
    PyValue::Object(
        value
            .and_then(PyValue::as_object)
            .unwrap_or_default()
            .iter()
            .filter(|(key, _)| keep(key))
            .cloned()
            .collect(),
    )
}

fn safe_scalar_key(key: &str) -> bool {
    let lower = key.to_lowercase();
    !["secret", "password", "api_key", "authorization"]
        .iter()
        .any(|marker| lower.contains(marker))
        && !matches!(
            lower.replace(['_', '-'], "").as_str(),
            "token"
                | "accesstoken"
                | "refreshtoken"
                | "idtoken"
                | "authtoken"
                | "apitoken"
                | "bearertoken"
                | "clienttoken"
                | "hftoken"
                | "huggingfacetoken"
        )
}

fn safe_parameters(value: Option<&PyValue>) -> Result<PyValue, SourceError> {
    let mut parameters = select_fields(value, safe_scalar_key);
    if let Some(cap) = value.and_then(|value| value.get("max_tokens")) {
        if !matches!(cap, PyValue::Int(value) if value.parse::<u32>().is_ok_and(|cap| (1..=131_072).contains(&cap)))
        {
            return Err(value_error("model output token limit is invalid"));
        }
        set_field(&mut parameters, "max_tokens", cap.clone());
    }
    Ok(parameters)
}

fn merge_record(current: &PyValue, record: &PyValue) -> Result<PyValue, SourceError> {
    let mut merged = merge_fields(
        current.as_object().expect("mapping current record"),
        record.as_object().expect("mapping request record"),
    );
    let empty = PyValue::Object(Vec::new());
    for key in ["parameters", "metadata"] {
        let saved = current
            .get(key)
            .filter(|value| pytruthy(value))
            .unwrap_or(&empty);
        let incoming = record
            .get(key)
            .filter(|value| pytruthy(value))
            .unwrap_or(&empty);
        set_field(&mut merged, key, merge_namespace(saved, incoming)?);
    }
    Ok(merged)
}

#[derive(Eq, Hash, PartialEq)]
enum DictionaryKey {
    Null,
    Integer(String),
    Float(u64),
    String(String),
}

impl DictionaryKey {
    fn from_scalar(value: &PyValue) -> Self {
        match value {
            PyValue::Null => Self::Null,
            PyValue::String(text) => Self::String(text.clone()),
            PyValue::Float(value) if !value.is_finite() || value.fract() != 0.0 => {
                Self::Float(value.to_bits())
            }
            _ => Self::Integer(pyint(value).expect("integral dictionary key")),
        }
    }
}

fn namespace_entries(value: &PyValue) -> Result<Vec<(PyValue, PyValue)>, SourceError> {
    if let PyValue::Array(rows) = value {
        return rows
            .iter()
            .enumerate()
            .map(|(index, row)| dictionary_pair(row, index))
            .collect();
    }
    Ok(copy_dictionary(value, &mut false)?
        .into_object()
        .expect("dict conversion")
        .into_iter()
        .map(|(key, value)| (PyValue::String(key), value))
        .collect())
}

fn merge_namespace(existing: &PyValue, incoming: &PyValue) -> Result<PyValue, SourceError> {
    if let (Some(existing), Some(incoming)) = (existing.as_object(), incoming.as_object()) {
        return Ok(merge_fields(existing, incoming));
    }
    // Older documents may hold sequences of pairs. dict() and the unpacking
    // merge keep the first key's type and position, then _safe_scalars casts
    // the resulting keys to strings, which can collapse a second set of keys.
    let mut raw: HashMap<_, usize> = HashMap::new();
    let mut entries = Vec::new();
    for (key, value) in namespace_entries(existing)?
        .into_iter()
        .chain(namespace_entries(incoming)?)
    {
        match raw.entry(DictionaryKey::from_scalar(&key)) {
            std::collections::hash_map::Entry::Occupied(position) => {
                let (_, current) = &mut entries[*position.get()];
                *current = value;
            }
            std::collections::hash_map::Entry::Vacant(position) => {
                position.insert(entries.len());
                entries.push((pystr(&key), value));
            }
        }
    }
    let mut positions: HashMap<_, usize> = HashMap::new();
    let mut normalized = Vec::new();
    for (key, value) in entries {
        match positions.entry(key.clone()) {
            std::collections::hash_map::Entry::Occupied(position) => {
                let (_, current) = &mut normalized[*position.get()];
                *current = value;
            }
            std::collections::hash_map::Entry::Vacant(position) => {
                position.insert(normalized.len());
                normalized.push((key, value));
            }
        }
    }
    Ok(PyValue::Object(normalized))
}

fn merge_fields(existing: &Fields, incoming: &Fields) -> PyValue {
    let mut fields = PyValue::Object(existing.to_vec());
    for (key, value) in incoming {
        set_field(&mut fields, key, value.clone());
    }
    fields
}

fn set_field(object: &mut PyValue, key: &str, value: PyValue) {
    let PyValue::Object(fields) = object else {
        unreachable!("can only add fields to an object")
    };
    if let Some((_, current)) = fields.iter_mut().find(|(candidate, _)| candidate == key) {
        *current = value;
    } else {
        fields.push((key.to_owned(), value));
    }
}

fn dictionary_get<'a>(value: &'a PyValue, key: &str) -> Result<Option<&'a PyValue>, SourceError> {
    if value.as_object().is_none() {
        return Err(SourceError::new(
            "AttributeError",
            format!("'{}' object has no attribute 'get'", type_name(value)),
        ));
    }
    Ok(value.get(key))
}

fn python_equal(left: &PyValue, right: &PyValue) -> bool {
    let mut pending = vec![(left, right)];
    while let Some((left, right)) = pending.pop() {
        match (left, right) {
            (PyValue::Object(left), PyValue::Object(right)) if left.len() == right.len() => {
                if left
                    .iter()
                    .zip(right)
                    .all(|((left, _), (right, _))| left == right)
                {
                    pending.extend(
                        left.iter()
                            .zip(right)
                            .map(|((_, left), (_, right))| (left, right)),
                    );
                } else {
                    let right = right
                        .iter()
                        .map(|(key, value)| (key, value))
                        .collect::<HashMap<_, _>>();
                    for (key, left) in left {
                        let Some(&right) = right.get(key) else {
                            return false;
                        };
                        pending.push((left, right));
                    }
                }
            }
            (PyValue::Array(left), PyValue::Array(right)) if left.len() == right.len() => {
                pending.extend(left.iter().zip(right));
            }
            (PyValue::Bool(left), PyValue::Int(right))
            | (PyValue::Int(right), PyValue::Bool(left))
                if right == if *left { "1" } else { "0" } => {}
            (PyValue::Bool(left), PyValue::Float(right))
            | (PyValue::Float(right), PyValue::Bool(left))
                if *right == if *left { 1.0 } else { 0.0 } => {}
            (PyValue::Int(left), PyValue::Float(right))
            | (PyValue::Float(right), PyValue::Int(left))
                if right.is_finite()
                    && right.fract() == 0.0
                    && pyint(&PyValue::Float(*right)).is_ok_and(|right| *left == right) => {}
            (PyValue::Null, PyValue::Null)
            | (PyValue::Bool(_), PyValue::Bool(_))
            | (PyValue::Int(_), PyValue::Int(_))
            | (PyValue::Float(_), PyValue::Float(_))
            | (PyValue::String(_), PyValue::String(_))
                if left == right => {}
            _ => return false,
        }
    }
    true
}

fn integer_or_zero(value: Option<&PyValue>) -> Result<String, SourceError> {
    let Some(value) = value.filter(|value| pytruthy(value)) else {
        return Ok("0".into());
    };
    pyint(value).map_err(|error| match (error, value) {
        (CoercionError::Type, _) => SourceError::new(
            "TypeError",
            format!(
                "int() argument must be a string, a bytes-like object or a real number, not '{}'",
                type_name(value)
            ),
        ),
        (CoercionError::Value, PyValue::Float(_)) => {
            value_error("cannot convert float NaN to integer")
        }
        (CoercionError::Overflow, PyValue::Float(_)) => {
            SourceError::new("OverflowError", "cannot convert float infinity to integer")
        }
        (_, PyValue::String(text)) => {
            if let Some(digits) = decimal_digit_count(text).filter(|digits| *digits > MAX_INTEGER_DIGITS) {
                value_error(format!(
                    "Exceeds the limit ({MAX_INTEGER_DIGITS} digits) for integer string conversion: value has {digits} digits; use sys.set_int_max_str_digits() to increase the limit"
                ))
            } else {
                invalid_integer(value)
            }
        }
        _ => invalid_integer(value),
    })
}

fn invalid_integer(value: &PyValue) -> SourceError {
    let repr: String = pyrepr(value).chars().take(200).collect();
    value_error(format!("invalid literal for int() with base 10: {repr}"))
}

fn increment_digits(value: &str) -> String {
    if let Some(unsigned) = value.strip_prefix('-') {
        let mut digits = unsigned.as_bytes().to_vec();
        for digit in digits.iter_mut().rev() {
            if *digit != b'0' {
                *digit -= 1;
                break;
            }
            *digit = b'9';
        }
        return match digits.iter().position(|digit| *digit != b'0') {
            Some(first) => format!(
                "-{}",
                std::str::from_utf8(&digits[first..]).expect("decimal digits")
            ),
            None => "0".into(),
        };
    }
    let mut digits = value.as_bytes().to_vec();
    for digit in digits.iter_mut().rev() {
        if *digit != b'9' {
            *digit += 1;
            return String::from_utf8(digits).expect("decimal digits");
        }
        *digit = b'0';
    }
    digits.insert(0, b'1');
    String::from_utf8(digits).expect("decimal digits")
}

fn increment_store_revision(value: &PyValue) -> Result<PyValue, SourceError> {
    match value {
        PyValue::Int(value) => Ok(PyValue::Int(increment_digits(value))),
        PyValue::Bool(value) => Ok(PyValue::Int(if *value { "2" } else { "1" }.into())),
        PyValue::Float(value) => Ok(PyValue::Float(value + 1.0)),
        PyValue::String(_) => Err(SourceError::new(
            "TypeError",
            "can only concatenate str (not \"int\") to str",
        )),
        PyValue::Array(_) => Err(SourceError::new(
            "TypeError",
            "'int' object is not iterable",
        )),
        _ => Err(SourceError::new(
            "TypeError",
            format!(
                "unsupported operand type(s) for +=: '{}' and 'int'",
                type_name(value)
            ),
        )),
    }
}

/// Python sorts all object keys for the digest; the saved document retains their insertion order.
fn sorted_json(value: &PyValue) -> PyValue {
    enum Task<'a> {
        Value(&'a PyValue),
        Array(usize),
        Object(Vec<&'a str>),
    }
    let mut pending = vec![Task::Value(value)];
    let mut results = Vec::new();
    while let Some(task) = pending.pop() {
        match task {
            Task::Value(PyValue::Array(children)) => {
                pending.push(Task::Array(children.len()));
                pending.extend(children.iter().rev().map(Task::Value));
            }
            Task::Value(PyValue::Object(fields)) => {
                let mut fields: Vec<_> = fields.iter().collect();
                fields.sort_unstable_by(|(left, _), (right, _)| left.cmp(right));
                pending.push(Task::Object(
                    fields.iter().map(|(key, _)| key.as_str()).collect(),
                ));
                pending.extend(
                    fields
                        .into_iter()
                        .rev()
                        .map(|(_, value)| Task::Value(value)),
                );
            }
            Task::Value(value) => results.push(value.clone()),
            Task::Array(length) => {
                let children = results.split_off(results.len() - length);
                results.push(PyValue::Array(children));
            }
            Task::Object(keys) => {
                let values = results.split_off(results.len() - keys.len());
                results.push(PyValue::Object(
                    keys.into_iter().map(str::to_owned).zip(values).collect(),
                ));
            }
        }
    }
    results.pop().expect("one sorted JSON root")
}

fn hash_codec_error(error: CodecError) -> SourceError {
    if error == CodecError::IntegerLimit {
        let limit = MAX_INTEGER_DIGITS;
        return value_error(format!(
            "Exceeds the limit ({limit} digits) for integer string conversion; use sys.set_int_max_str_digits() to increase the limit"
        ));
    }
    SourceError::new("ObservationBoundary", error.to_string())
}

fn saved(profile: PyValue, revision: PyValue) -> PyValue {
    object([
        ("action", string("saved")),
        ("profile", profile),
        ("store_revision", revision),
    ])
}

fn value_error(message: impl Into<String>) -> SourceError {
    SourceError::new("ValueError", message)
}

fn conflict(message: &str) -> SourceError {
    SourceError::new("ModelRegistryConflict", message)
}

#[cfg(test)]
#[path = "mutation_tests.rs"]
mod tests;
