//! Additive observation candidate for the original pure provider compiler.
//! A source-failure observation is never a successful production route.
mod failure;
mod model_access;
mod native_filters;
mod python_url;

use failure::{SourceFailure, SourceResult};
use ordered_python_compat::{CodecError, PyValue, dumps, loads, pytruthy};

/// Pure source helpers shared with the additive Registry translation.
/// This module supplies no component, storage, credentials or authority.
pub mod registry_support {
    pub use crate::failure::{SourceFailure, SourceResult, contains, get, mapping, type_name};
    pub use crate::model_access::{
        effective as effective_model_access, identifier, normalize as normalize_model_access,
    };
    pub use crate::python_url::{Parts as UrlParts, split as split_url};
}

#[cfg(feature = "component")]
wit_bindgen::generate!({
    path: ["../../crates/tobkiri-wit/wit/core/core.wit", "wit"],
    world: "local:provider-model-access-rust/pack@1.0.0",
    generate_all,
});

pub const OPERATIONS: &[&str] = &[
    "normalize_model_access",
    "effective_model_access",
    "model_is_allowed",
    "compile_connection_parameters",
    "is_official_openrouter",
    "normalize_native_filters",
    "discovery_url",
    "apply_native_routing",
];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BoundaryFailure {
    Input,
    Limit,
}
impl BoundaryFailure {
    pub const fn code(self) -> &'static str {
        match self {
            Self::Input => "invalid",
            Self::Limit => "limit-exceeded",
        }
    }
    pub const fn message(self) -> &'static str {
        match self {
            Self::Input => "invalid provider compiler observation request",
            Self::Limit => "provider compiler observation resource limit exceeded",
        }
    }
    #[cfg(feature = "component")]
    fn into_wit(self) -> tobkiri::core::types::Error {
        use tobkiri::core::types::{Error, ErrorCode};
        Error {
            code: match self {
                Self::Input => ErrorCode::Invalid,
                Self::Limit => ErrorCode::LimitExceeded,
            },
            message: self.message().into(),
        }
    }
}
fn codec_error(error: CodecError) -> BoundaryFailure {
    match error {
        CodecError::Invalid | CodecError::NonFinite => BoundaryFailure::Input,
        _ => BoundaryFailure::Limit,
    }
}

pub fn invoke(operation: &str, payload: &str) -> Result<String, BoundaryFailure> {
    if !OPERATIONS.contains(&operation) {
        return Err(BoundaryFailure::Input);
    }
    let arguments = loads(payload).map_err(codec_error)?;
    if arguments.as_object().is_none() {
        return Err(BoundaryFailure::Input);
    }
    let outcome = match evaluate(operation, &arguments) {
        Ok(value) => obj([("status", PyValue::String("ok".into())), ("value", value)]),
        Err(error) if error.exception_class == "ObservationBoundary" => {
            return Err(BoundaryFailure::Input);
        }
        Err(error) => obj([
            ("status", PyValue::String("source-failure".into())),
            (
                "exception_class",
                PyValue::String(error.exception_class.into()),
            ),
            ("message", PyValue::String(error.message)),
        ]),
    };
    dumps(&outcome).map_err(codec_error)
}

/// Evaluate already-decoded finite kwargs. This never stores, authenticates,
/// obtains credentials or performs transport; the caller owns snapshot authority.
fn evaluate(operation: &str, arguments: &PyValue) -> SourceResult<PyValue> {
    let (positional, keyword): (&[&str], &[&str]) = match operation {
        "normalize_model_access" => (&["value"], &["connection"]),
        "effective_model_access" | "is_official_openrouter" => (&["connection"], &[]),
        "model_is_allowed" => (&["connection", "model_id"], &[]),
        "compile_connection_parameters" => (&["connection", "model_id", "parameters"], &[]),
        "normalize_native_filters" => (&["value"], &["verified_openrouter"]),
        "discovery_url" => (&["native_filters"], &[]),
        "apply_native_routing" => (&["native_filters", "parameters"], &[]),
        _ => unreachable!("outer operation checked"),
    };
    for (key, _) in arguments.as_object().unwrap() {
        if !positional.contains(&key.as_str()) && !keyword.contains(&key.as_str()) {
            let names: Vec<_> = positional.iter().chain(keyword.iter()).copied().collect();
            return Err(failure::unexpected_keyword(operation, key, &names));
        }
    }
    for (names, keyword_only) in [(positional, false), (keyword, true)] {
        let missing: Vec<_> = names
            .iter()
            .copied()
            .filter(|name| arguments.get(name).is_none())
            .collect();
        if !missing.is_empty() {
            return Err(failure::missing_arguments(
                operation,
                &missing,
                keyword_only,
            ));
        }
    }
    let argument = |key| arguments.get(key).expect("required argument checked above");
    match operation {
        "normalize_model_access" => {
            model_access::normalize(argument("value"), argument("connection"))
        }
        "effective_model_access" => model_access::effective(argument("connection")),
        "model_is_allowed" => {
            model_access::allowed(argument("connection"), argument("model_id")).map(PyValue::Bool)
        }
        "compile_connection_parameters" => model_access::compile(
            argument("connection"),
            argument("model_id"),
            argument("parameters"),
        ),
        "is_official_openrouter" => {
            native_filters::official(argument("connection")).map(PyValue::Bool)
        }
        "normalize_native_filters" => {
            native_filters::normalize(argument("value"), pytruthy(argument("verified_openrouter")))
        }
        "discovery_url" => native_filters::discovery_url(argument("native_filters")),
        "apply_native_routing" => {
            native_filters::apply(argument("native_filters"), argument("parameters"))
        }
        _ => unreachable!("outer operation checked"),
    }
}

pub(crate) fn obj<const N: usize>(entries: [(&str, PyValue); N]) -> PyValue {
    PyValue::Object(
        entries
            .into_iter()
            .map(|(name, value)| (name.into(), value))
            .collect(),
    )
}

// dict(parameters) compatibility for the direct apply helper. Ordinary JSON
// objects are copied in order; list-of-pairs retains first key position.
fn python_dict(value: &PyValue) -> SourceResult<(Vec<(String, PyValue)>, bool)> {
    if let Some(entries) = value.as_object() {
        return Ok((entries.to_vec(), false));
    }
    let string_items;
    let values = if let Some(values) = value.as_array() {
        values
    } else if let Some(text) = value.as_str() {
        string_items = text
            .chars()
            .map(|ch| PyValue::String(ch.to_string()))
            .collect::<Vec<_>>();
        &string_items
    } else {
        return Err(SourceFailure::type_error(format!(
            "'{}' object is not iterable",
            failure::type_name(value)
        )));
    };
    let mut result: Vec<(String, PyValue)> = Vec::new();
    let mut unrepresented_key = false;
    for (index, item) in values.iter().enumerate() {
        let chars;
        let pair = if let Some(pair) = item.as_array() {
            pair
        } else if let Some(text) = item.as_str() {
            chars = text
                .chars()
                .map(|ch| PyValue::String(ch.to_string()))
                .collect::<Vec<_>>();
            &chars
        } else if let Some(entries) = item.as_object() {
            chars = entries
                .iter()
                .map(|(key, _)| PyValue::String(key.clone()))
                .collect::<Vec<_>>();
            &chars
        } else {
            return Err(SourceFailure::type_error(format!(
                "cannot convert dictionary update sequence element #{index} to a sequence"
            )));
        };
        if pair.len() != 2 {
            return Err(SourceFailure::value(format!(
                "dictionary update sequence element #{index} has length {}; 2 is required",
                pair.len()
            )));
        }
        failure::hashable(&pair[0])?;
        // Keep validating every later source row. Representation limits must
        // not mask a later original dict error or native wire conversion error.
        let Some(key) = pair[0].as_str() else {
            unrepresented_key = true;
            continue;
        };
        if let Some((_, value)) = result.iter_mut().find(|(name, _)| name == key) {
            *value = pair[1].clone();
        } else {
            result.push((key.into(), pair[1].clone()));
        }
    }
    Ok((result, unrepresented_key))
}

#[cfg(feature = "component")]
const RUNTIME_INFO: &str = "{\"implementation\":\"rust\",\"candidate\":\"local.provider-model-access-rust\",\"source_owner\":\"tobkiri_protocol.provider_compiler\",\"protocol\":\"ordered-finite-json-v2\",\"promotion\":\"not-promoted\"}";
#[cfg(feature = "component")]
struct Pack;
#[cfg(feature = "component")]
impl exports::tobkiri::core::lifecycle::Guest for Pack {
    fn init(_config: String) -> Result<(), tobkiri::core::types::Error> {
        Ok(())
    }
    fn shutdown() {}
}
#[cfg(feature = "component")]
impl exports::local::provider_model_access_rust::compiler::Guest for Pack {
    fn runtime_info() -> String {
        RUNTIME_INFO.into()
    }
    fn invoke(operation: String, payload: String) -> Result<String, tobkiri::core::types::Error> {
        invoke(&operation, &payload).map_err(BoundaryFailure::into_wit)
    }
}
#[cfg(all(target_arch = "wasm32", feature = "component"))]
export!(Pack);
