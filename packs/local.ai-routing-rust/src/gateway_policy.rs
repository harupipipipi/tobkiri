use std::collections::{BTreeMap, BTreeSet};

use ordered_python_compat::{PyValue, dumps_bounded, pystr, pystrip, pytruthy};

use crate::{
    MAX_OUTPUT_BYTES, compare_integer, number, object,
    pipeline::{dict_or_empty, int_or, load_request},
    text_or,
};

const MAX_DEPTH: usize = 128;
const OUTPUT_LIMIT_KEYS: [&str; 3] = ["max_tokens", "max_completion_tokens", "max_output_tokens"];
type Fields = Vec<(String, PyValue)>;

/// Project already-resolved model profile fields or normalize route requirements.
/// Leave original exceptions and unsupported data to the leased Python Gateway.
pub fn project_gateway_policy(request: &str) -> Option<String> {
    let mut input = load_request(request)?;
    if !within_depth_limit(&input) {
        return None;
    }

    let result = match input.get("phase")?.as_str()? {
        "model-profile" => model_profile(input.take().into_object()?)?,
        "route-requirement" => {
            route_requirement(input.get("requirements").unwrap_or(&PyValue::Null))?
        }
        _ => return None,
    };
    dumps_bounded(&result, MAX_OUTPUT_BYTES).ok()
}

fn within_depth_limit(value: &PyValue) -> bool {
    let mut pending = vec![(value, 0)];
    while let Some((value, depth)) = pending.pop() {
        if depth > MAX_DEPTH {
            return false;
        }
        match value {
            PyValue::Array(items) => pending.extend(items.iter().map(|item| (item, depth + 1))),
            PyValue::Object(fields) => {
                pending.extend(fields.iter().map(|(_, item)| (item, depth + 1)));
            }
            _ => {}
        }
    }
    true
}

fn model_profile(mut input: Fields) -> Option<PyValue> {
    let mut take = |key| {
        input
            .iter_mut()
            .find(|(name, _)| name == key)
            .map_or(PyValue::Null, |(_, value)| value.take())
    };
    let identifier = take("identifier");
    let model_profile_id = text_or(Some(&take("resolved_profile_id")), identifier.as_str()?);
    let model_id = text_or(Some(&take("model_id")), "");
    let requirements = merge_requirements(
        take("profile_requirements"),
        take("request_requirements"),
        model_id,
    )?;
    let parameters = bounded_parameters(
        dict_or_empty(take("profile_parameters"))?,
        dict_or_empty(take("request_parameters"))?,
    )?;
    Some(object([
        ("model_profile_id", PyValue::String(model_profile_id)),
        ("requirements", PyValue::Object(requirements)),
        ("parameters", PyValue::Object(parameters)),
    ]))
}

fn merge_requirements(profile: PyValue, request: PyValue, model_id: String) -> Option<Fields> {
    let mut remaining = MAX_OUTPUT_BYTES;
    let mut overrides: Fields = Vec::new();
    for key in ["modalities", "capabilities"] {
        let mut values = BTreeSet::new();
        collect_strings(profile.get(key), &mut values, &mut remaining)?;
        collect_strings(request.get(key), &mut values, &mut remaining)?;
        if !values.is_empty() {
            overrides.push((key.into(), string_values(values)));
        }
    }

    for key in ["tool_calling", "thinking"] {
        overrides.push((
            key.into(),
            PyValue::Bool(truthy(profile.get(key)) || truthy(request.get(key))),
        ));
    }
    let profile_context = int_or(profile.get("minimum_context"), "0")?;
    let request_context = int_or(request.get("minimum_context"), "0")?;
    let minimum_context = if compare_integer(&request_context, &profile_context).is_gt() {
        request_context
    } else {
        profile_context
    };
    overrides.push(("minimum_context".into(), PyValue::Int(minimum_context)));

    let profile_cost = number(profile.get("maximum_cost")).ok()?;
    let request_cost = number(request.get("maximum_cost")).ok()?;
    // Python min keeps the first value when a later value is unordered or equal.
    let cost = match (profile_cost, request_cost) {
        (Some(left), Some(right)) if right < left => Some(right),
        (Some(left), _) => Some(left),
        (_, right) => right,
    };
    if let Some(cost) = cost {
        overrides.push(("maximum_cost".into(), PyValue::Float(cost)));
    }

    if profile.get("preferred_model_id").is_none() && request.get("preferred_model_id").is_none() {
        overrides.push(("preferred_model_id".into(), PyValue::String(model_id)));
    }
    Some(merge_fields(
        profile
            .into_object()
            .unwrap_or_default()
            .into_iter()
            .chain(request.into_object().unwrap_or_default())
            .chain(overrides),
    ))
}

fn bounded_parameters(saved: Fields, requested: Fields) -> Option<Fields> {
    let mut ceiling = match field(&saved, "max_tokens") {
        None => None,
        Some(PyValue::Int(value))
            if compare_integer(value, "1").is_ge() && compare_integer(value, "131072").is_le() =>
        {
            Some(value.clone())
        }
        _ => return None,
    };

    for key in ["n", "best_of"] {
        if let Some(value) = field(&requested, key).or_else(|| field(&saved, key))
            && !matches!(value, PyValue::Int(value) if value == "1")
        {
            return None;
        }
    }

    for key in OUTPUT_LIMIT_KEYS {
        if let Some(value) = field(&requested, key) {
            let PyValue::Int(value) = value else {
                return None;
            };
            if compare_integer(value, "0").is_le() {
                return None;
            }
            if ceiling
                .as_ref()
                .is_none_or(|current| compare_integer(value, current).is_lt())
            {
                ceiling = Some(value.clone());
            }
        }
    }

    let fields = saved
        .into_iter()
        .chain(requested)
        .filter(|(key, _)| !OUTPUT_LIMIT_KEYS.contains(&key.as_str()));
    let normalized_limit = ceiling.map(|value| ("max_tokens".into(), PyValue::Int(value)));
    Some(merge_fields(fields.chain(normalized_limit)))
}

fn merge_fields(entries: impl IntoIterator<Item = (String, PyValue)>) -> Fields {
    let mut positions = BTreeMap::new();
    let mut result: Fields = Vec::new();
    for (key, value) in entries {
        let position = *positions.entry(key.clone()).or_insert(result.len());
        if position == result.len() {
            result.push((key, value));
        } else {
            result[position].1 = value;
        }
    }
    result
}

fn route_requirement(requirement: &PyValue) -> Option<PyValue> {
    let mut remaining = MAX_OUTPUT_BYTES;
    let mut modalities = BTreeSet::new();
    collect_strings(
        requirement.get("modalities"),
        &mut modalities,
        &mut remaining,
    )?;
    if modalities.is_empty() {
        modalities.insert("text".into());
    }
    let mut capabilities = BTreeSet::new();
    collect_strings(
        requirement.get("capabilities"),
        &mut capabilities,
        &mut remaining,
    )?;
    let minimum_context = int_or(requirement.get("minimum_context"), "0")?;
    let minimum_context = if compare_integer(&minimum_context, "0").is_lt() {
        "0".into()
    } else {
        minimum_context
    };
    let maximum_cost = number(requirement.get("maximum_cost")).ok()?;
    let health_max_age = number(requirement.get("health_max_age"))
        .ok()?
        .filter(|value| *value != 0.0)
        .unwrap_or(60.0);
    // Python max(0.0, age) returns 0.0 for NaN.
    let health_max_age = if health_max_age > 0.0 {
        health_max_age
    } else {
        0.0
    };

    Some(object([
        ("modalities", string_values(modalities)),
        ("capabilities", string_values(capabilities)),
        (
            "tool_calling",
            PyValue::Bool(truthy(requirement.get("tool_calling"))),
        ),
        (
            "thinking",
            PyValue::Bool(truthy(requirement.get("thinking"))),
        ),
        ("minimum_context", PyValue::Int(minimum_context)),
        (
            "request_surface",
            PyValue::String(text_or(requirement.get("request_surface"), "chat")),
        ),
        (
            "data_residency",
            optional_text(requirement.get("data_residency")),
        ),
        ("maximum_cost", crate::float_or_null(maximum_cost)),
        (
            "preferred_model_id",
            optional_text(requirement.get("preferred_model_id")),
        ),
        (
            "preferred_provider_id",
            optional_text(requirement.get("preferred_provider_id")),
        ),
        (
            "preferred_provider_instance_id",
            optional_text(requirement.get("preferred_provider_instance_id")),
        ),
        ("health_max_age", PyValue::Float(health_max_age)),
    ]))
}

fn collect_strings(
    value: Option<&PyValue>,
    output: &mut BTreeSet<String>,
    remaining: &mut usize,
) -> Option<()> {
    if let Some(values) = value.and_then(PyValue::as_array) {
        for value in values {
            let rendered = pystr(value);
            let trimmed = pystrip(&rendered);
            if !trimmed.is_empty() && !output.contains(trimmed) {
                *remaining = remaining.checked_sub(trimmed.len())?;
                output.insert(trimmed.into());
            }
        }
    }
    Some(())
}

fn string_values(values: BTreeSet<String>) -> PyValue {
    PyValue::Array(values.into_iter().map(PyValue::String).collect())
}

fn truthy(value: Option<&PyValue>) -> bool {
    value.is_some_and(pytruthy)
}

fn optional_text(value: Option<&PyValue>) -> PyValue {
    value
        .filter(|value| pytruthy(value))
        .map_or(PyValue::Null, |value| PyValue::String(pystr(value)))
}

fn field<'a>(fields: &'a [(String, PyValue)], key: &str) -> Option<&'a PyValue> {
    fields
        .iter()
        .find(|(name, _)| name == key)
        .map(|(_, value)| value)
}

#[cfg(test)]
mod tests;
