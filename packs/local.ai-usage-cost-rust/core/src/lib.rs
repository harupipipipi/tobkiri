//! Pure usage cost projection over ordered finite JSON.

use ordered_python_compat::{
    CodecError, CoercionError, PyValue, dumps_bounded, loads, pyfloat, pystr, pytruthy,
};

/// Stable terminal categories from the existing Python adapter/app wrapper.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ProviderFailure {
    /// The operation or request JSON is invalid.
    Input,
    /// Provider validation failed.
    Validation,
    /// Numeric coercion overflowed or arithmetic produced a nonfinite result.
    Arithmetic,
    /// The projection cannot be encoded in ordered finite JSON.
    Output,
    /// The codec reached a resource limit.
    Resource,
}

impl ProviderFailure {
    /// Existing Core WIT enum spelling, also used by the independent oracle.
    pub const fn code(self) -> &'static str {
        match self {
            Self::Input => "invalid",
            Self::Validation | Self::Arithmetic | Self::Output => "internal",
            Self::Resource => "limit-exceeded",
        }
    }

    /// Closed original app.py projection; no private payload/error text escapes.
    pub const fn message(self) -> &'static str {
        match self {
            Self::Input => "invalid usage cost request",
            Self::Validation => "usage cost provider validation failed",
            Self::Arithmetic => "usage cost provider arithmetic failed",
            Self::Output => "usage cost provider output failed",
            Self::Resource => "usage cost resource limit exceeded",
        }
    }
}

/// Apply the original usage cost operation with a bounded ordered JSON output.
pub fn invoke(
    operation: &str,
    payload: &str,
    maximum_output_bytes: usize,
) -> Result<String, ProviderFailure> {
    if !matches!(
        operation,
        "calculate"
            | "normalize"
            | "rumi_ai_usage_pack.ai-usage-cost"
            | "rumi_ai_usage_pack.ai-usage-cost.generate"
            | "rumi_ai_usage_pack.ai-usage-cost.stream"
    ) {
        return Err(ProviderFailure::Input);
    }
    let payload = loads(payload).map_err(|error| match error {
        CodecError::Resource => ProviderFailure::Resource,
        _ => ProviderFailure::Input,
    })?;
    if payload.as_object().is_none() {
        return Err(ProviderFailure::Input);
    }
    let usage = payload
        .get("usage")
        .filter(|value| value.as_object().is_some());
    let pricing = payload
        .get("pricing")
        .filter(|value| value.as_object().is_some());
    // get() retains key presence: an explicit null primary must not select the
    // fallback. These four conversions run even when an earlier one is unknown.
    let input_tokens = number(usage.and_then(|value| {
        value
            .get("input_tokens")
            .or_else(|| value.get("prompt_tokens"))
    }))?;
    let output_tokens = number(usage.and_then(|value| {
        value
            .get("output_tokens")
            .or_else(|| value.get("completion_tokens"))
    }))?;
    let input_rate = number(pricing.and_then(|value| value.get("input")))?;
    let output_rate = number(pricing.and_then(|value| value.get("output")))?;
    let known = input_tokens.is_some()
        && output_tokens.is_some()
        && input_rate.is_some()
        && output_rate.is_some();
    let total_tokens = match (input_tokens, output_tokens) {
        (Some(input), Some(output)) => PyValue::Float(input + output),
        _ => PyValue::Null,
    };
    let cost = match (input_tokens, input_rate, output_tokens, output_rate) {
        (Some(input), Some(input_rate), Some(output), Some(output_rate)) => {
            // Preserve CPython's two multiplies followed by one addition.
            let input_cost = input * input_rate;
            let output_cost = output * output_rate;
            PyValue::Float(input_cost + output_cost)
        }
        _ => PyValue::Null,
    };
    let currency = if known {
        text_or_default(pricing.and_then(|value| value.get("currency")), "USD")
    } else {
        PyValue::Null
    };
    let usage_provenance = text_or_default(payload.get("usage_provenance"), "provider_reported");
    // This is the last request access. Retain the original revision subtree by
    // move; cloning a deeply nested JSON value would add recursive native work.
    let pricing_revision = payload
        .into_object()
        .expect("request object checked above")
        .into_iter()
        .find(|(key, _)| key == "pricing_revision")
        .map(|(_, value)| value)
        .unwrap_or(PyValue::Null);
    // Field order is the original dict-literal order. Nonfinite arithmetic is
    // rejected by the same shared output encoder, never changed into unknown.
    let result = PyValue::object([
        ("input_tokens".to_owned(), optional_number(input_tokens)),
        ("output_tokens".to_owned(), optional_number(output_tokens)),
        ("total_tokens".to_owned(), total_tokens),
        ("cost".to_owned(), cost),
        ("currency".to_owned(), currency),
        ("known".to_owned(), PyValue::Bool(known)),
        ("usage_provenance".to_owned(), usage_provenance),
        ("pricing_revision".to_owned(), pricing_revision),
    ]);
    dumps_bounded(&result, maximum_output_bytes).map_err(|error| match error {
        CodecError::NonFinite => ProviderFailure::Arithmetic,
        CodecError::Resource => ProviderFailure::Resource,
        _ => ProviderFailure::Output,
    })
}

fn number(value: Option<&PyValue>) -> Result<Option<f64>, ProviderFailure> {
    let Some(value) = value else { return Ok(None) };
    match pyfloat(value) {
        Ok(value) => Ok((value.is_finite() && value >= 0.0).then_some(value)),
        Err(CoercionError::Type | CoercionError::Value) => Ok(None),
        // Python _number intentionally does not catch OverflowError. A huge
        // integer conversion is an adapter Arithmetic failure, unlike "inf".
        Err(CoercionError::Overflow) => Err(ProviderFailure::Arithmetic),
    }
}

fn optional_number(value: Option<f64>) -> PyValue {
    value.map(PyValue::Float).unwrap_or(PyValue::Null)
}

fn text_or_default(value: Option<&PyValue>, fallback: &str) -> PyValue {
    PyValue::String(
        value
            .filter(|value| pytruthy(value))
            .map(pystr)
            .unwrap_or_else(|| fallback.to_owned()),
    )
}
