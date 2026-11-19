//! Effect-free routing and Gateway data projections.

use std::{
    cmp::Ordering,
    collections::{BTreeMap, BTreeSet},
    rc::Rc,
};

use ordered_python_compat::{
    CoercionError, PyValue, dumps_bounded, loads, pyfloat, pyint, pystr, pystrip, pytruthy,
};

#[cfg(all(feature = "component", target_arch = "wasm32"))]
mod component;
mod gateway_policy;
mod gateway_result;
mod pipeline;

pub use gateway_policy::project_gateway_policy;
pub use gateway_result::project_gateway_result;
pub use pipeline::{decide_failover, prepare_request};

const MAX_OUTPUT_BYTES: usize = 1024 * 1024;

/// Project Gateway tool intent payloads as non-authoritative descriptors.
/// Return `None` for requests that require the original tool bridge provider.
pub fn normalize_tool_intents(request: &str) -> Option<String> {
    tobkiri_ai_tool_bridge_core::normalize_gateway_tool_intents(request)
}

/// Project usage cost from supplied usage and pricing in ordered finite JSON.
/// Return `None` when the request or result requires the original provider.
pub fn calculate_usage_cost(request: &str) -> Option<String> {
    if request.len() > MAX_OUTPUT_BYTES {
        return None;
    }
    tobkiri_ai_usage_cost_core::invoke("calculate", request, MAX_OUTPUT_BYTES).ok()
}

/// Project Gateway stream events while preserving ordered finite JSON.
/// Return `None` for requests that require the original normalizer.
pub fn normalize_stream_result(request: &str) -> Option<String> {
    tobkiri_ai_stream_normalizer_core::normalize("normalize", request, MAX_OUTPUT_BYTES).ok()
}

/// Evaluate the legacy routing payload within the ordered finite JSON ABI.
/// Return `None` for requests that require the original evaluator or codec.
pub fn evaluate(request: &str) -> Option<String> {
    let request = loads(request).ok()?;
    request.as_object()?;
    let decision_time = number(request.get("decision_time")).ok().flatten()?;
    let models = request
        .get("models")
        .and_then(PyValue::as_array)
        .unwrap_or_default();
    let providers = request
        .get("execution_providers")
        .and_then(PyValue::as_array)
        .unwrap_or_default();
    let empty = PyValue::Null;
    let requirements = request.get("requirements").unwrap_or(&empty);
    let health: BTreeMap<&str, &PyValue> = request
        .get("health")
        .and_then(PyValue::as_object)
        .unwrap_or_default()
        .iter()
        .map(|(key, value)| (key.as_str(), value))
        .collect();
    let providers = ProviderIndex::new(providers);
    let mut candidates = Vec::new();
    let mut excluded = Vec::new();
    let mut remaining = MAX_OUTPUT_BYTES;
    for model in models.iter().filter(|model| model.as_object().is_some()) {
        let result = candidate(model, &providers, &health, requirements, decision_time).ok()?;
        let (entries, value) = match result {
            Ok(value) => (&mut candidates, value),
            Err(reason) => (
                &mut excluded,
                object([
                    ("model_id", text_field(model, "model_id", "unknown")),
                    ("provider_id", text_field(model, "provider_id", "")),
                    ("reason", PyValue::String(reason.into())),
                ]),
            ),
        };
        // Bound the accumulated candidate/descriptor copies before keeping them.
        // The final encoding also counts separators, wrapper fields and selected.
        remaining -= dumps_bounded(&value, remaining).ok()?.len();
        entries.push(value);
    }
    if candidates.len() > 1 {
        let preferences = [
            (
                "execution_provider_instance_id",
                text_or(requirements.get("preferred_provider_instance_id"), ""),
            ),
            (
                "provider_id",
                text_or(requirements.get("preferred_provider_id"), ""),
            ),
            (
                "model_id",
                text_or(requirements.get("preferred_model_id"), ""),
            ),
        ];
        candidates.sort_by(|left, right| compare(left, right, &preferences));
    }
    let selected = candidates.first().cloned().unwrap_or(PyValue::Null);
    dumps_bounded(
        &object([
            ("selected", selected),
            ("candidates", PyValue::Array(candidates)),
            ("excluded", PyValue::Array(excluded)),
            ("deterministic", PyValue::Bool(true)),
        ]),
        MAX_OUTPUT_BYTES,
    )
    .ok()
}

type Candidate = Result<PyValue, &'static str>;

fn candidate(
    model: &PyValue,
    providers: &ProviderIndex,
    health: &BTreeMap<&str, &PyValue>,
    requirements: &PyValue,
    decision_time: f64,
) -> Result<Candidate, CoercionError> {
    let model_id = text_or(model.get("model_id"), "");
    let model_id = pystrip(&model_id);
    let provider_id = text_or(model.get("provider_id"), "");
    let provider_id = pystrip(&provider_id);
    let hint = text_or(model.get("execution_provider_instance_id"), "");
    let execution = match providers.resolve(provider_id, pystrip(&hint)) {
        Err(reason) => return Ok(Err(reason)),
        Ok(_) if model_id.is_empty() => return Ok(Err("execution_provider_unresolved")),
        Ok(execution) => execution,
    };
    if matches!(model.get("available"), Some(PyValue::Bool(false))) {
        return Ok(Err("model_unavailable"));
    }

    let modalities = strings(model.get("modalities"));
    let capabilities = strings(model.get("capabilities"));
    let required_modalities = strings(requirements.get("modalities"));
    let required_capabilities = strings(requirements.get("capabilities"));
    if (required_modalities.is_empty() && !modalities.contains("text"))
        || !required_modalities.is_subset(&modalities)
    {
        return Ok(Err("modality_mismatch"));
    }
    if requirements.get("tool_calling").is_some_and(pytruthy)
        && !capabilities.contains("tool_calling")
    {
        return Ok(Err("tool_calling_mismatch"));
    }
    if requirements.get("thinking").is_some_and(pytruthy) && !capabilities.contains("thinking") {
        return Ok(Err("thinking_mismatch"));
    }
    if !required_capabilities.is_subset(&capabilities) {
        return Ok(Err("capability_mismatch"));
    }
    let context_length = integer(model.get("context_length"), "0")?;
    let minimum_context = integer(requirements.get("minimum_context"), "0")?;
    if compare_integer(&context_length, &minimum_context).is_lt() {
        return Ok(Err("context_length_mismatch"));
    }
    let surfaces = strings(model.get("request_surfaces"));
    let request_surface = text_or(requirements.get("request_surface"), "chat");
    if !surfaces.is_empty() && !surfaces.contains(&request_surface) {
        return Ok(Err("request_surface_mismatch"));
    }
    let required_residency = text_or(requirements.get("data_residency"), "");
    let model_residencies = strings(model.get("data_residency"));
    if !required_residency.is_empty() && !model_residencies.contains(&required_residency) {
        return Ok(Err("data_residency_mismatch"));
    }
    let input_cost = number(model.get("input_cost"))?;
    let output_cost = number(model.get("output_cost"))?;
    if let Some(maximum_cost) = number(requirements.get("maximum_cost"))? {
        let (Some(input_cost), Some(output_cost)) = (input_cost, output_cost) else {
            return Ok(Err("cost_unknown"));
        };
        if input_cost + output_cost > maximum_cost {
            return Ok(Err("cost_policy_mismatch"));
        }
    }

    let health_id = text_or(model.get("health_provider_instance_id"), execution);
    let evidence = health.get(health_id.as_str());
    let health_status = text_or(evidence.and_then(|value| value.get("status")), "unknown");
    let observed_at = number(evidence.and_then(|value| value.get("observed_at")))?;
    let max_age = number(requirements.get("health_max_age"))?
        .filter(|value| *value != 0.0)
        .unwrap_or(60.0);
    let health_status = if observed_at.is_some_and(|time| decision_time - time > max_age) {
        "unknown"
    } else {
        &health_status
    };
    match health_status {
        "unavailable" => return Ok(Err("health_unavailable")),
        "denied" => return Ok(Err("health_denied")),
        "invalid" => return Ok(Err("health_invalid")),
        _ => {}
    }
    let priority = integer(model.get("priority"), "100")?;

    Ok(Ok(object([
        ("model_id", PyValue::String(model_id.into())),
        (
            "provider_model_id",
            text_field(model, "provider_model_id", model_id),
        ),
        ("provider_id", PyValue::String(provider_id.into())),
        (
            "execution_provider_instance_id",
            PyValue::String(execution.into()),
        ),
        ("health_provider_instance_id", PyValue::String(health_id)),
        (
            "catalog_provider_instance_id",
            text_field(model, "catalog_provider_instance_id", ""),
        ),
        (
            "catalog_revision",
            text_field(model, "catalog_revision", "unknown"),
        ),
        ("capabilities", string_array(capabilities)),
        ("modalities", string_array(modalities)),
        ("context_length", PyValue::Int(context_length)),
        ("input_cost", float_or_null(input_cost)),
        ("output_cost", float_or_null(output_cost)),
        ("priority", PyValue::Int(priority)),
        ("health", PyValue::String(health_status.into())),
        ("health_observed_at", float_or_null(observed_at)),
        ("descriptor", model.clone()),
    ])))
}

fn compare(left: &PyValue, right: &PyValue, preferences: &[(&str, String); 3]) -> Ordering {
    for (field, preferred) in preferences {
        let evaluate =
            |candidate: &PyValue| !preferred.is_empty() && field_str(candidate, field) == preferred;
        let order = evaluate(right).cmp(&evaluate(left));
        if !order.is_eq() {
            return order;
        }
    }

    let health_rank = |candidate: &PyValue| match field_str(candidate, "health") {
        "healthy" | "available" => 0,
        "degraded" => 1,
        "unknown" => 2,
        _ => 3,
    };
    let cost = |candidate: Option<&PyValue>| match candidate {
        Some(PyValue::Float(value)) => *value,
        _ => f64::INFINITY,
    };
    health_rank(left)
        .cmp(&health_rank(right))
        .then_with(|| compare_integer(priority(left), priority(right)))
        .then_with(|| {
            cost(left.get("input_cost"))
                .partial_cmp(&cost(right.get("input_cost")))
                .expect("candidate cost already passed finite JSON encoding")
        })
        .then_with(|| {
            cost(left.get("output_cost"))
                .partial_cmp(&cost(right.get("output_cost")))
                .expect("candidate cost already passed finite JSON encoding")
        })
        .then_with(|| field_str(left, "model_id").cmp(field_str(right, "model_id")))
        .then_with(|| field_str(left, "provider_id").cmp(field_str(right, "provider_id")))
        .then_with(|| field_str(left, "catalog_revision").cmp(field_str(right, "catalog_revision")))
}

fn field_str<'a>(candidate: &'a PyValue, field: &str) -> &'a str {
    candidate
        .get(field)
        .and_then(PyValue::as_str)
        .expect("candidate sort field is a string")
}

fn priority(candidate: &PyValue) -> &str {
    match candidate.get("priority") {
        Some(PyValue::Int(value)) => value,
        _ => unreachable!("candidate priority is an integer"),
    }
}

#[derive(Default)]
struct ProviderIndex {
    by_id: BTreeSet<Rc<str>>,
    exact: BTreeMap<String, ProviderMatch>,
    wildcard: ProviderMatch,
}

impl ProviderIndex {
    fn new(providers: &[PyValue]) -> Self {
        let mut index = Self::default();
        for provider in providers {
            let id = text_or(provider.get("provider_instance_id"), "");
            if id.is_empty() {
                continue;
            }
            let id: Rc<str> = id.into();
            index.by_id.insert(Rc::clone(&id));
            for key in strings(provider.get("routing_keys")) {
                if key == "*" {
                    index.wildcard.record(&id);
                }
                index.exact.entry(key).or_default().record(&id);
            }
        }
        index
    }

    fn resolve(&self, provider_id: &str, hint: &str) -> Result<&str, &'static str> {
        match self.exact.get(provider_id) {
            Some(ProviderMatch::One(id)) => return Ok(id),
            Some(ProviderMatch::Ambiguous) => return Err("execution_provider_ambiguous"),
            _ => {}
        }
        if let Some(id) = self.by_id.get(hint) {
            return Ok(id);
        }
        match &self.wildcard {
            ProviderMatch::One(id) => Ok(id),
            ProviderMatch::Ambiguous => Err("execution_provider_ambiguous"),
            ProviderMatch::Missing => Err("execution_provider_unresolved"),
        }
    }
}

#[derive(Default)]
enum ProviderMatch {
    #[default]
    Missing,
    One(Rc<str>),
    Ambiguous,
}

impl ProviderMatch {
    fn record(&mut self, id: &Rc<str>) {
        *self = match self {
            Self::Missing => Self::One(Rc::clone(id)),
            _ => Self::Ambiguous,
        };
    }
}

fn strings(value: Option<&PyValue>) -> BTreeSet<String> {
    match value {
        Some(PyValue::String(value)) if !value.is_empty() => BTreeSet::from([value.clone()]),
        Some(PyValue::Array(values)) => values
            .iter()
            .map(pystr)
            .filter(|value| !pystrip(value).is_empty())
            .collect(),
        _ => BTreeSet::new(),
    }
}

fn string_array(values: BTreeSet<String>) -> PyValue {
    PyValue::Array(values.into_iter().map(PyValue::String).collect())
}

fn text_or(value: Option<&PyValue>, default: &str) -> String {
    value
        .filter(|value| pytruthy(value))
        .map_or_else(|| default.to_owned(), pystr)
}

fn text_field(value: &PyValue, key: &str, default: &str) -> PyValue {
    PyValue::String(text_or(value.get(key), default))
}

fn integer(value: Option<&PyValue>, default: &str) -> Result<String, CoercionError> {
    match value.map(pyint) {
        None | Some(Err(CoercionError::Type | CoercionError::Value)) => Ok(default.into()),
        Some(result) => result,
    }
}

fn compare_integer(left: &str, right: &str) -> Ordering {
    let left_negative = left.starts_with('-');
    let right_negative = right.starts_with('-');
    right_negative.cmp(&left_negative).then_with(|| {
        let magnitude = left.len().cmp(&right.len()).then_with(|| left.cmp(right));
        if left_negative {
            magnitude.reverse()
        } else {
            magnitude
        }
    })
}

fn number(value: Option<&PyValue>) -> Result<Option<f64>, CoercionError> {
    match value.map(pyfloat) {
        None | Some(Err(CoercionError::Type | CoercionError::Value)) => Ok(None),
        Some(result) => result.map(Some),
    }
}

fn float_or_null(value: Option<f64>) -> PyValue {
    value.map_or(PyValue::Null, PyValue::Float)
}

fn object<const N: usize>(fields: [(&str, PyValue); N]) -> PyValue {
    PyValue::Object(
        fields
            .into_iter()
            .map(|(key, value)| (key.into(), value))
            .collect(),
    )
}

#[cfg(test)]
mod tests;

#[cfg(test)]
mod tool_intent_tests;
