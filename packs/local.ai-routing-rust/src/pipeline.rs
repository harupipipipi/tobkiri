use std::collections::BTreeMap;

use ordered_python_compat::{MAX_NODES, PyValue, dumps_bounded, loads, pyint, pystrip, pytruthy};

use crate::{MAX_OUTPUT_BYTES, compare_integer, number, object, text_or};

const REQUIREMENT_KEYS: &[&str] = &[
    "modalities",
    "capabilities",
    "tool_calling",
    "thinking",
    "minimum_context",
    "request_surface",
    "data_residency",
    "maximum_cost",
    "preferred_model_id",
    "preferred_provider_id",
    "preferred_provider_instance_id",
    "health_max_age",
];

/// Prepare Gateway request data. Leave unsupported data and original exceptions
/// to the original leased Python operation.
pub fn prepare_request(request: &str) -> Option<String> {
    let mut payload = load_request(request)?.into_object()?;
    let mut take = |key| {
        payload
            .iter_mut()
            .find(|(name, _)| name == key)
            .map_or(PyValue::Null, |(_, value)| value.take())
    };

    let decision_time = number(Some(&take("decision_time"))).ok()??;
    let deadline = number(Some(&take("deadline")))
        .ok()?
        .unwrap_or(decision_time + 60.0);
    if !decision_time.is_finite() || !deadline.is_finite() || deadline <= decision_time {
        return None;
    }

    // A built-in JSON value's str() can begin with either prefix only when
    // the value itself is a string. Keep the reference as opaque data.
    let credential_handle = take("credential_handle");
    if !matches!(&credential_handle, PyValue::Null)
        && !credential_handle
            .as_str()
            .is_some_and(|value| value.starts_with("credential:") || value.starts_with("opaque:"))
    {
        return None;
    }

    let request_id = text_or(Some(&take("request_id")), "");
    let request_id = pystrip(&request_id);
    if request_id.is_empty() {
        return None;
    }
    let requirements = take("requirements")
        .into_object()
        .unwrap_or_default()
        .into_iter()
        .filter(|(key, _)| REQUIREMENT_KEYS.contains(&key.as_str()))
        .collect();

    dumps_bounded(
        &object([
            ("request_id", PyValue::String(request_id.into())),
            ("decision_time", PyValue::Float(decision_time)),
            ("deadline", PyValue::Float(deadline)),
            ("messages", PyValue::Array(list_or_empty(take("messages"))?)),
            ("input", take("input")),
            (
                "parameters",
                PyValue::Object(dict_or_empty(take("parameters"))?),
            ),
            ("tools", PyValue::Array(list_or_empty(take("tools"))?)),
            ("requirements", PyValue::Object(requirements)),
            ("credential_handle", credential_handle),
            ("idempotency_key", take("idempotency_key")),
            (
                "allow_failover",
                PyValue::Bool(pytruthy(&take("allow_failover"))),
            ),
            (
                "policy_revision",
                PyValue::String(text_or(Some(&take("policy_revision")), "")),
            ),
            ("conversation_id", take("conversation_id")),
            ("profile_id", take("profile_id")),
            ("model_profile_id", take("model_profile_id")),
            ("model_reference", take("model_reference")),
        ]),
        MAX_OUTPUT_BYTES,
    )
    .ok()
}

/// Project replay-safety guards and their first failure in the original order.
/// Invalid inputs defer to the original leased Python operation.
pub fn decide_failover(request: &str) -> Option<String> {
    let payload = load_request(request)?;
    let error_code = text_or(payload.get("error_code"), "");
    let attempt = int_or(payload.get("attempt"), "1")?;
    let candidate_count = int_or(payload.get("candidate_count"), "0")?;
    let attempt = if compare_integer(&attempt, "1").is_lt() {
        "1"
    } else {
        &attempt
    };
    // After clamping attempt to at least 1, clamping candidate_count to 0
    // does not change the comparison when candidate_count is negative.
    let deadline = number(payload.get("deadline")).ok()?;
    let decision_time = number(payload.get("decision_time")).ok()?;
    let truthy = |key| payload.get(key).is_some_and(pytruthy);
    let checks = [
        ("explicitly_allowed", truthy("allow_failover")),
        ("idempotency_bound", truthy("idempotency_key")),
        ("tool_free", !truthy("tools")),
        (
            "retryable_error",
            matches!(
                error_code.as_str(),
                "provider_unavailable" | "quota" | "invalid_response"
            ),
        ),
        (
            "candidate_available",
            compare_integer(attempt, &candidate_count).is_lt(),
        ),
        (
            "deadline_remaining",
            decision_time
                .zip(deadline)
                .is_some_and(|(now, end)| now < end),
        ),
    ];
    let failure = checks.iter().find(|(_, passed)| !passed);
    let reason = failure.map_or("replay_safe_failover", |(name, _)| name);
    dumps_bounded(
        &object([
            ("allowed", PyValue::Bool(failure.is_none())),
            (
                "checks",
                object(checks.map(|(key, passed)| (key, PyValue::Bool(passed)))),
            ),
            ("reason", PyValue::String(reason.into())),
        ]),
        MAX_OUTPUT_BYTES,
    )
    .ok()
}

fn load_request(request: &str) -> Option<PyValue> {
    if request.len() > MAX_OUTPUT_BYTES {
        return None;
    }
    let payload = loads(request).ok()?;
    payload.as_object()?;
    Some(payload)
}

fn int_or(value: Option<&PyValue>, default: &str) -> Option<String> {
    match value.filter(|value| pytruthy(value)) {
        Some(value) => pyint(value).ok(),
        None => Some(default.into()),
    }
}

fn list_or_empty(value: PyValue) -> Option<Vec<PyValue>> {
    if !pytruthy(&value) {
        return Some(Vec::new());
    }
    match &value {
        PyValue::Array(_) => value.into_array(),
        PyValue::Object(_) => Some(
            value
                .into_object()?
                .into_iter()
                .map(|(key, _)| PyValue::String(key))
                .collect(),
        ),
        PyValue::String(text) if text.chars().take(MAX_NODES).count() < MAX_NODES => Some(
            text.chars()
                .map(|character| PyValue::String(character.into()))
                .collect(),
        ),
        _ => None,
    }
}

fn dict_or_empty(value: PyValue) -> Option<Vec<(String, PyValue)>> {
    if !pytruthy(&value) {
        return Some(Vec::new());
    }
    if matches!(value, PyValue::Object(_)) {
        return value.into_object();
    }

    // dict() replaces a duplicate's value at its first insertion position.
    let mut positions = BTreeMap::new();
    let mut fields: Vec<(String, PyValue)> = Vec::new();
    for pair in value.into_array()? {
        let (key, value) = dict_pair(pair)?;
        let position = *positions.entry(key.clone()).or_insert(fields.len());
        if position == fields.len() {
            fields.push((key, value));
        } else {
            fields[position].1 = value;
        }
    }
    Some(fields)
}

fn dict_pair(pair: PyValue) -> Option<(String, PyValue)> {
    match &pair {
        PyValue::Array(values) if values.len() == 2 => {
            let mut values = pair.into_array()?.into_iter();
            Some((values.next()?.into_string()?, values.next()?))
        }
        PyValue::Object(entries) if entries.len() == 2 => {
            let mut keys = pair.into_object()?.into_iter().map(|(key, _)| key);
            Some((keys.next()?, PyValue::String(keys.next()?)))
        }
        PyValue::String(text) => {
            let mut chars = text.chars();
            let (key, value) = (chars.next()?, chars.next()?);
            if chars.next().is_some() {
                return None;
            }
            Some((key.into(), PyValue::String(value.into())))
        }
        _ => None,
    }
}

#[cfg(test)]
mod tests;
