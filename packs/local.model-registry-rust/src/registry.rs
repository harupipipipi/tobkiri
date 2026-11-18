//! ModelRegistry reads over one decoded store document.

use std::{borrow::Cow, collections::HashMap};

use ordered_python_compat::{PyValue, pystr, pystrip, pytruthy};

use crate::SourceError;

const STORE_VERSION: &str = "rumi.model-registry.store.v1";

type Fields = [(String, PyValue)];

/// The owner read operations for one Python-compatible Model Registry store.
pub struct Registry {
    profile_id: String,
    document: PyValue,
}

struct ReadState<'a> {
    revision: Option<&'a PyValue>,
    profiles: &'a Fields,
    aliases: &'a Fields,
}

impl ReadState<'_> {
    fn revision(&self) -> Result<PyValue, SourceError> {
        self.revision
            .cloned()
            .ok_or_else(|| SourceError::new("KeyError", "'revision'"))
    }
}

impl Registry {
    /// Bind a profile to a bounded decoded document, using empty state when absent.
    pub fn new(profile_id: &str, document: Option<PyValue>) -> Result<Self, SourceError> {
        let profile_id = pystrip(profile_id);
        if profile_id.is_empty() {
            return Err(SourceError::new(
                "ValueError",
                "profile_id must not be empty",
            ));
        }
        if profile_id.contains(['/', '\\']) {
            return Err(SourceError::new(
                "ValueError",
                "profile_id must not contain path separators",
            ));
        }
        if profile_id.contains("..") {
            return Err(SourceError::new(
                "ValueError",
                "profile_id must not contain path traversal segments",
            ));
        }
        let document = document.unwrap_or_else(|| {
            object([
                ("version", string(STORE_VERSION)),
                ("profile_id", string(profile_id)),
                ("revision", PyValue::Int("0".into())),
                ("profiles", PyValue::Object(Vec::new())),
                ("aliases", PyValue::Object(Vec::new())),
            ])
        });
        Ok(Self {
            profile_id: profile_id.into(),
            document,
        })
    }

    /// Return the raw records and aliases sorted by their saved keys.
    pub fn snapshot(&self) -> Result<PyValue, SourceError> {
        let state = self.read()?;
        let revision = state.revision()?;
        let mut entries = state.profiles.iter().collect::<Vec<_>>();
        entries.sort_unstable_by(|(left, _), (right, _)| left.cmp(right));
        let mut profiles = Vec::with_capacity(entries.len());
        let mut non_string_key = false;
        for (_, value) in entries {
            profiles.push(copy_dictionary(value, &mut non_string_key)?);
        }
        if non_string_key {
            return Err(SourceError::new(
                "ObservationBoundary",
                "non-string Python dictionary keys are outside this observation representation",
            ));
        }
        let mut aliases = state.aliases.to_vec();
        aliases.sort_unstable_by(|(left, _), (right, _)| left.cmp(right));
        Ok(object([
            ("version", string(STORE_VERSION)),
            ("profile_id", string(&self.profile_id)),
            ("revision", revision),
            ("profiles", PyValue::Array(profiles)),
            ("aliases", PyValue::Object(aliases)),
        ]))
    }

    /// Look up an exact profile ID and return its saved dictionary or null.
    pub fn get(&self, model_profile_id: &PyValue) -> Result<PyValue, SourceError> {
        let state = self.read()?;
        let key = identifier(model_profile_id)?;
        Ok(field(state.profiles, &key)
            .filter(|value| value.as_object().is_some())
            .cloned()
            .unwrap_or(PyValue::Null))
    }

    /// Look up an identifier with at most one alias hop, returning null on a miss.
    pub fn resolve(&self, identifier_value: &PyValue) -> Result<PyValue, SourceError> {
        let requested_id = identifier(identifier_value)?;
        let state = self.read()?;
        let target = match field(state.aliases, &requested_id) {
            Some(value) => {
                ensure_hashable(value)?;
                let Some(target) = value.as_str() else {
                    return Ok(PyValue::Null);
                };
                target
            }
            None => requested_id.as_ref(),
        };
        let Some(profile) =
            field(state.profiles, target).filter(|value| value.as_object().is_some())
        else {
            return Ok(PyValue::Null);
        };
        Ok(object([
            ("requested_id", string(&requested_id)),
            ("resolved_profile_id", string(target)),
            ("aliased", PyValue::Bool(target != requested_id)),
            ("profile", profile.clone()),
            ("store_revision", state.revision()?),
        ]))
    }

    fn read(&self) -> Result<ReadState<'_>, SourceError> {
        let document = &self.document;
        if document.get("version").and_then(PyValue::as_str) != Some(STORE_VERSION) {
            return Err(SourceError::new(
                "ValueError",
                "model registry store version is invalid",
            ));
        }
        if document.get("profile_id").and_then(PyValue::as_str) != Some(self.profile_id.as_str()) {
            return Err(SourceError::new(
                "ValueError",
                "model registry profile binding is invalid",
            ));
        }
        let profiles = document.get("profiles").and_then(PyValue::as_object);
        let aliases = document.get("aliases").and_then(PyValue::as_object);
        let (Some(profiles), Some(aliases)) = (profiles, aliases) else {
            return Err(SourceError::new(
                "ValueError",
                "model registry store is invalid",
            ));
        };
        Ok(ReadState {
            revision: document.get("revision"),
            profiles,
            aliases,
        })
    }
}

fn identifier(value: &PyValue) -> Result<Cow<'_, str>, SourceError> {
    let key = match value {
        PyValue::String(value) => Cow::Borrowed(pystrip(value)),
        value if pytruthy(value) => Cow::Owned(pystr(value)),
        _ => Cow::Borrowed(""),
    };
    let mut bytes = key.bytes();
    if key.len() <= 256
        && bytes
            .next()
            .is_some_and(|byte| byte.is_ascii_alphanumeric())
        && bytes.all(|byte| byte.is_ascii_alphanumeric() || b"._:/-".contains(&byte))
    {
        Ok(key)
    } else {
        Err(SourceError::new(
            "ValueError",
            "model registry identifier is invalid",
        ))
    }
}

fn field<'a>(entries: &'a Fields, key: &str) -> Option<&'a PyValue> {
    entries
        .iter()
        .find(|(name, _)| name == key)
        .map(|(_, value)| value)
}

fn object<const N: usize>(entries: [(&str, PyValue); N]) -> PyValue {
    PyValue::Object(
        entries
            .into_iter()
            .map(|(key, value)| (key.into(), value))
            .collect(),
    )
}

fn string(value: &str) -> PyValue {
    PyValue::String(value.into())
}

fn type_name(value: &PyValue) -> &'static str {
    match value {
        PyValue::Null => "NoneType",
        PyValue::Bool(_) => "bool",
        PyValue::Int(_) => "int",
        PyValue::Float(_) => "float",
        PyValue::String(_) => "str",
        PyValue::Array(_) => "list",
        PyValue::Object(_) => "dict",
    }
}

fn ensure_hashable(value: &PyValue) -> Result<(), SourceError> {
    if matches!(value, PyValue::Array(_) | PyValue::Object(_)) {
        return Err(SourceError::new(
            "TypeError",
            format!("unhashable type: '{}'", type_name(value)),
        ));
    }
    Ok(())
}

fn pair_length_error(index: usize, length: usize) -> SourceError {
    SourceError::new(
        "ValueError",
        format!("dictionary update sequence element #{index} has length {length}; 2 is required"),
    )
}

fn dictionary_pair(row: &PyValue, index: usize) -> Result<Option<(String, PyValue)>, SourceError> {
    let pair = match row {
        PyValue::Array(values) => {
            let [key, value] = values.as_slice() else {
                return Err(pair_length_error(index, values.len()));
            };
            ensure_hashable(key)?;
            key.as_str().map(|key| (key.into(), value.clone()))
        }
        PyValue::Object(fields) => {
            let [(key, _), (value, _)] = fields.as_slice() else {
                return Err(pair_length_error(index, fields.len()));
            };
            Some((key.clone(), string(value)))
        }
        PyValue::String(text) => {
            let mut chars = text.chars();
            match (chars.next(), chars.next(), chars.next()) {
                (Some(key), Some(value), None) => {
                    Some((key.to_string(), PyValue::String(value.to_string())))
                }
                _ => return Err(pair_length_error(index, text.chars().count())),
            }
        }
        _ => {
            return Err(SourceError::new(
                "TypeError",
                format!("cannot convert dictionary update sequence element #{index} to a sequence"),
            ));
        }
    };
    Ok(pair)
}

// The source snapshot calls dict(record), even when a saved record is not a dict.
fn copy_dictionary(value: &PyValue, non_string_key: &mut bool) -> Result<PyValue, SourceError> {
    let rows = match value {
        PyValue::Object(_) => return Ok(value.clone()),
        PyValue::Array(rows) => rows,
        PyValue::String(text) if text.is_empty() => return Ok(PyValue::Object(Vec::new())),
        PyValue::String(_) => return Err(pair_length_error(0, 1)),
        _ => {
            return Err(SourceError::new(
                "TypeError",
                format!("'{}' object is not iterable", type_name(value)),
            ));
        }
    };
    let mut fields = HashMap::new();
    for (index, row) in rows.iter().enumerate() {
        if let Some((key, value)) = dictionary_pair(row, index)? {
            let next = fields.len();
            fields.entry(key).or_insert((next, PyValue::Null)).1 = value;
        } else {
            // Later rows and records can still produce a Python dict() error.
            *non_string_key = true;
        }
    }
    let mut fields = fields.into_iter().collect::<Vec<_>>();
    fields.sort_unstable_by_key(|(_, (position, _))| *position);
    Ok(PyValue::Object(
        fields
            .into_iter()
            .map(|(key, (_, value))| (key, value))
            .collect(),
    ))
}

#[cfg(test)]
#[path = "registry_tests.rs"]
mod tests;
