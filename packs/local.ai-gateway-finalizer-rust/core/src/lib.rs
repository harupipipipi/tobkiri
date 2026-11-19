//! Pure Gateway result projection shared with the original finalizer candidate.

use std::collections::BTreeMap;

use ordered_python_compat::{PyValue, pystr, pytruthy};

/// Selected model and provider identifiers copied into the original response.
pub struct CandidateIdentity<'a> {
    pub model_id: &'a str,
    pub provider_instance_id: &'a str,
    pub catalog_provider_instance_id: &'a str,
    pub catalog_revision: &'a str,
}

/// Original finalizer rejections that require the caller's compatibility path.
#[derive(Debug)]
pub enum ProjectionError {
    Terminal,
    Domain { code: String, message: String },
}

/// Project a generate result and indicate whether its usage dict contains any
/// non-string keys. Such a dict cannot be encoded as ordered finite JSON.
pub fn normalize_generate_result(
    value: &PyValue,
    request_id: &str,
    selected: CandidateIdentity<'_>,
) -> Result<(PyValue, bool), ProjectionError> {
    value.as_object().ok_or_else(|| ProjectionError::Domain {
        code: "invalid_response".into(),
        message: "provider result must be an object".into(),
    })?;
    let status = value.get("status");
    if let Some(status) = status {
        if matches!(status, PyValue::Array(_) | PyValue::Object(_)) {
            return Err(ProjectionError::Terminal);
        }
        if !matches!(status, PyValue::Null)
            && !matches!(status, PyValue::String(text) if text == "ok")
        {
            let code = truthy_field(value, "error_code")
                .map(pystr)
                .unwrap_or_else(|| "provider_unavailable".into());
            let message = truthy_field(value, "message")
                .map(pystr)
                .unwrap_or_else(|| "provider failed".into());
            return Err(ProjectionError::Domain { code, message });
        }
    }

    let output = clone_field_or(value, "output", PyValue::Null);
    let tool_intents = iterable_list(
        truthy_field(value, "tool_intents"),
        PyValue::Array(Vec::new()),
    )?;
    let finish_reason = truthy_field(value, "finish_reason")
        .map(pystr)
        .unwrap_or_else(|| "stop".into());
    let (usage, usage_has_non_string_key) =
        python_dict(truthy_field(value, "usage"), PyValue::object([]))?;
    let usage_provenance = truthy_field(value, "usage_provenance")
        .map(pystr)
        .unwrap_or_else(|| "provider_reported".into());

    let mut output_fields = vec![
        ("status".into(), PyValue::String("ok".into())),
        ("request_id".into(), PyValue::String(request_id.into())),
        ("model_id".into(), PyValue::String(selected.model_id.into())),
        (
            "provider_instance_id".into(),
            PyValue::String(selected.provider_instance_id.into()),
        ),
        (
            "catalog_provider_instance_id".into(),
            PyValue::String(selected.catalog_provider_instance_id.into()),
        ),
        (
            "catalog_revision".into(),
            PyValue::String(selected.catalog_revision.into()),
        ),
        (
            "pricing_revision".into(),
            PyValue::String(selected.catalog_revision.into()),
        ),
        ("output".into(), output),
        ("tool_intents".into(), tool_intents),
        ("finish_reason".into(), PyValue::String(finish_reason)),
        ("usage".into(), usage),
        ("usage_provenance".into(), PyValue::String(usage_provenance)),
    ];

    let metadata = value.get("metadata");
    if value.get("task_state").is_some() || matches!(metadata, Some(PyValue::Object(_))) {
        let task_state = message_task_state(
            value.get("finish_reason"),
            value.get("task_state"),
            metadata,
        );
        output_fields.push(("task_state".into(), PyValue::String(task_state.into())));
    }
    Ok((PyValue::Object(output_fields), usage_has_non_string_key))
}

/// Assemble the stream result around events and tool intents already resolved
/// by the caller, with the legacy defaults and field order.
pub fn assemble_stream_result(
    value: &PyValue,
    request_id: &str,
    selected: CandidateIdentity<'_>,
    intents: PyValue,
    events: PyValue,
    attempts: PyValue,
) -> Result<PyValue, ProjectionError> {
    value.as_object().ok_or(ProjectionError::Terminal)?;
    let output = clone_field_or(value, "output", PyValue::String(String::new()));
    let finish_reason = clone_field_or(value, "finish_reason", PyValue::Null);
    let delivery_mode = clone_field_or(value, "delivery_mode", PyValue::String("buffered".into()));
    Ok(PyValue::object([
        ("status".into(), PyValue::String("ok".into())),
        ("output".into(), output),
        ("tool_intents".into(), intents),
        ("finish_reason".into(), finish_reason),
        ("delivery_mode".into(), delivery_mode),
        ("request_id".into(), PyValue::String(request_id.into())),
        ("model_id".into(), PyValue::String(selected.model_id.into())),
        (
            "provider_instance_id".into(),
            PyValue::String(selected.provider_instance_id.into()),
        ),
        ("events".into(), events),
        ("attempts".into(), attempts),
    ]))
}

fn message_task_state(
    finish_reason: Option<&PyValue>,
    task_state: Option<&PyValue>,
    metadata: Option<&PyValue>,
) -> &'static str {
    let task_state = task_state.or_else(|| metadata.and_then(|value| value.get("task_state")));
    let thinking = metadata.and_then(|value| value.get("thinking"));
    let states = [
        finish_reason
            .filter(|value| pytruthy(value))
            .map(pystr)
            .unwrap_or_default(),
        task_state
            .filter(|value| pytruthy(value))
            .map(pystr)
            .unwrap_or_default(),
        thinking
            .and_then(|value| value.get("state"))
            .filter(|value| pytruthy(value))
            .map(pystr)
            .unwrap_or_default(),
    ]
    .map(|state| state.to_lowercase());
    let contains = |choices: &[&str]| states.iter().any(|state| choices.contains(&state.as_str()));
    if contains(&["cancelled", "canceled", "interrupted"])
        || metadata.and_then(|value| value.get("cancelled")) == Some(&PyValue::Bool(true))
    {
        return "cancelled";
    }
    if contains(&[
        "waiting_approval",
        "awaiting_approval",
        "approval_required",
        "authority_approval_required",
    ]) || metadata.and_then(|value| value.get("approval_required")) == Some(&PyValue::Bool(true))
    {
        return "waiting_approval";
    }
    if contains(&["waiting_user", "waiting_for_user", "awaiting_user"])
        || metadata.and_then(|value| value.get("waiting_for_user")) == Some(&PyValue::Bool(true))
    {
        return "waiting_user";
    }
    if contains(&["error", "failed", "failure"]) {
        return "failed";
    }
    if contains(&["running", "streaming", "pending", "tool_call", "tool_calls"])
        || metadata.and_then(|value| value.get("draft")) == Some(&PyValue::Bool(true))
        || metadata.and_then(|value| value.get("streaming")) == Some(&PyValue::Bool(true))
    {
        return "running";
    }
    let known = [
        "stop",
        "end_turn",
        "completed",
        "complete",
        "waiting_user",
        "waiting_for_user",
        "awaiting_user",
        "waiting_approval",
        "awaiting_approval",
        "approval_required",
        "authority_approval_required",
        "running",
        "streaming",
        "pending",
        "tool_call",
        "tool_calls",
        "cancelled",
        "canceled",
        "interrupted",
    ];
    for value in [task_state, thinking.and_then(|value| value.get("state"))]
        .into_iter()
        .flatten()
    {
        if !matches!(value, PyValue::Null)
            && !matches!(value, PyValue::String(text) if text.is_empty())
            && !known.contains(&pystr(value).to_lowercase().as_str())
        {
            return "unknown";
        }
    }
    let finish = states[0].as_str();
    if ["stop", "end_turn", "completed", "complete"].contains(&finish) {
        return "completed";
    }
    if !finish.is_empty() {
        return "failed";
    }
    "unknown"
}

fn iterable_list(value: Option<&PyValue>, fallback: PyValue) -> Result<PyValue, ProjectionError> {
    let Some(value) = value.filter(|value| pytruthy(value)) else {
        return Ok(fallback);
    };
    let values = match value {
        PyValue::Array(values) => values.clone(),
        PyValue::Object(values) => values
            .iter()
            .map(|(key, _)| PyValue::String(key.clone()))
            .collect(),
        PyValue::String(text) => text
            .chars()
            .map(|character| PyValue::String(character.to_string()))
            .collect(),
        _ => return Err(ProjectionError::Terminal),
    };
    Ok(PyValue::Array(values))
}

fn python_dict(
    value: Option<&PyValue>,
    fallback: PyValue,
) -> Result<(PyValue, bool), ProjectionError> {
    let Some(value) = value.filter(|value| pytruthy(value)) else {
        return Ok((fallback, false));
    };
    match value {
        PyValue::Object(fields) => Ok((
            PyValue::Object(
                fields
                    .iter()
                    .map(|(key, value)| (key.clone(), value.clone()))
                    .collect(),
            ),
            false,
        )),
        PyValue::Array(rows) => {
            let mut fields: Vec<(String, PyValue)> = Vec::with_capacity(rows.len());
            let mut positions = BTreeMap::new();
            let mut non_string_key = false;
            for row in rows {
                // dict(sequence) iterates each row: arrays yield values,
                // objects yield keys, and strings yield Unicode code points.
                let (key, value) = match row {
                    PyValue::Array(pair) if pair.len() == 2 => (pair[0].clone(), pair[1].clone()),
                    PyValue::Object(pair) if pair.len() == 2 => (
                        PyValue::String(pair[0].0.clone()),
                        PyValue::String(pair[1].0.clone()),
                    ),
                    PyValue::String(pair) => {
                        let mut characters = pair.chars();
                        match (characters.next(), characters.next(), characters.next()) {
                            (Some(key), Some(value), None) => (
                                PyValue::String(key.to_string()),
                                PyValue::String(value.to_string()),
                            ),
                            _ => return Err(ProjectionError::Terminal),
                        }
                    }
                    _ => return Err(ProjectionError::Terminal),
                };
                match &key {
                    PyValue::String(key) => {
                        let position = *positions.entry(key.clone()).or_insert(fields.len());
                        if position == fields.len() {
                            fields.push((key.clone(), value));
                        } else {
                            fields[position].1 = value;
                        }
                    }
                    PyValue::Array(_) | PyValue::Object(_) => {
                        return Err(ProjectionError::Terminal);
                    }
                    _ => non_string_key = true,
                }
            }
            // Continue validating all rows even after a hashable non-string
            // key: a later malformed/unhashable row still fails dict() before
            // any dependency call. String-key replacement keeps first order.
            Ok((PyValue::Object(fields), non_string_key))
        }
        _ => Err(ProjectionError::Terminal),
    }
}

fn clone_field_or(value: &PyValue, name: &str, fallback: PyValue) -> PyValue {
    value.get(name).cloned().unwrap_or(fallback)
}

fn truthy_field<'a>(value: &'a PyValue, name: &str) -> Option<&'a PyValue> {
    value.get(name).filter(|value| pytruthy(value))
}
