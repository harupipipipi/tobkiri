use crate::{
    failure::{self, SourceFailure, SourceResult},
    native_filters, obj,
};
use ordered_python_compat::PyValue;

pub const VERSION: &str = "tobkiri.connection-model-access.v1";
const RESERVED: &[&str] = &[
    "model",
    "models",
    "provider",
    "route",
    "extra_body",
    "fallbacks",
    "provider_id",
    "provider_connection_id",
    "endpoint",
    "credential_handle",
];

pub fn identifier(value: &str, maximum: usize, model: bool) -> bool {
    let bytes = value.as_bytes();
    !bytes.is_empty()
        && bytes.len() <= maximum
        && bytes[0].is_ascii_alphanumeric()
        && bytes.iter().all(|byte| {
            byte.is_ascii_alphanumeric()
                || b"._:/-".contains(byte)
                || (model && b"@+".contains(byte))
        })
}

pub fn normalize(value: &PyValue, connection: &PyValue) -> SourceResult<PyValue> {
    let Some(entries) = value.as_object() else {
        return Err(SourceFailure::value(
            "model access policy schema is invalid",
        ));
    };
    if entries
        .iter()
        .any(|(key, _)| !["version", "mode", "model_ids", "native_filters"].contains(&key.as_str()))
        || value.get("version").and_then(PyValue::as_str) != Some(VERSION)
    {
        return Err(SourceFailure::value(
            "model access policy schema is invalid",
        ));
    }
    let mode = value
        .get("mode")
        .and_then(PyValue::as_str)
        .filter(|mode| ["all", "explicit"].contains(mode))
        .ok_or_else(|| SourceFailure::value("model access mode is invalid"))?;
    let empty = PyValue::Array(Vec::new());
    let ids = value
        .get("model_ids")
        .unwrap_or(&empty)
        .as_array()
        .ok_or_else(|| SourceFailure::value("model access IDs are invalid"))?;
    if ids.len() > 4096
        || ids
            .iter()
            .any(|value| value.as_str().is_none_or(|id| !identifier(id, 512, true)))
    {
        return Err(SourceFailure::value("model access IDs are invalid"));
    }
    if mode == "all" && !ids.is_empty() {
        return Err(SourceFailure::value(
            "all model access must not pin catalog IDs",
        ));
    }
    let mut ids: Vec<_> = ids
        .iter()
        .map(|id| id.as_str().unwrap().to_owned())
        .collect();
    ids.sort();
    ids.dedup();
    let mut result = vec![
        ("version".into(), PyValue::String(VERSION.into())),
        ("mode".into(), PyValue::String(mode.into())),
        (
            "model_ids".into(),
            PyValue::Array(ids.into_iter().map(PyValue::String).collect()),
        ),
    ];
    if let Some(native) = value
        .get("native_filters")
        .filter(|value| !matches!(value, PyValue::Null))
    {
        let verified = native_filters::official(connection)?;
        result.push((
            "native_filters".into(),
            native_filters::normalize(native, verified)?,
        ));
    }
    Ok(PyValue::Object(result))
}

pub fn effective(connection: &PyValue) -> SourceResult<PyValue> {
    if failure::contains(connection, "model_access")? {
        return normalize(failure::subscript(connection, "model_access")?, connection);
    }
    let metadata = failure::get(connection, "metadata")?;
    let empty = PyValue::Object(Vec::new());
    let metadata = if metadata.as_object().is_some() {
        metadata
    } else {
        &empty
    };
    for source in [connection, metadata] {
        if failure::contains(source, "allowed_models")? {
            return normalize(
                &obj([
                    ("version", PyValue::String(VERSION.into())),
                    ("mode", PyValue::String("explicit".into())),
                    (
                        "model_ids",
                        failure::subscript(source, "allowed_models")?.clone(),
                    ),
                ]),
                connection,
            );
        }
    }
    Ok(PyValue::Null)
}

pub fn allowed(connection: &PyValue, model_id: &PyValue) -> SourceResult<bool> {
    let policy = effective(connection)?;
    Ok(matches!(policy, PyValue::Null)
        || policy.get("mode").and_then(PyValue::as_str) == Some("all")
        || policy
            .get("model_ids")
            .and_then(PyValue::as_array)
            .is_some_and(|ids| ids.iter().any(|id| id == model_id)))
}

pub fn compile(
    connection: &PyValue,
    model_id: &PyValue,
    parameters: &PyValue,
) -> SourceResult<PyValue> {
    if model_id
        .as_str()
        .is_none_or(|id| !identifier(id, 512, true))
    {
        return Err(SourceFailure::value("provider model ID is invalid"));
    }
    if !allowed(connection, model_id)? {
        return Err(SourceFailure::permission(
            "model is not permitted by this connection",
        ));
    }
    let empty = PyValue::Object(Vec::new());
    let parameters = if matches!(parameters, PyValue::Null) {
        &empty
    } else {
        parameters
    };
    let Some(entries) = parameters.as_object() else {
        return Err(SourceFailure::value(
            "request parameters cannot override the saved model route",
        ));
    };
    if entries
        .iter()
        .any(|(key, _)| RESERVED.contains(&key.as_str()))
    {
        return Err(SourceFailure::value(
            "request parameters cannot override the saved model route",
        ));
    }
    let policy = effective(connection)?;
    if let Some(native) = policy
        .get("native_filters")
        .filter(|value| ordered_python_compat::pytruthy(value))
    {
        return native_filters::apply(native, parameters);
    }
    Ok(parameters.clone())
}
