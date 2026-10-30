//! Private comparison descriptors, never a production validation replacement.

use std::error::Error;

use jsonschema::{
    error::ValidationErrorKind, json, Draft, Registry, Retrieve, Uri, ValidationError,
};
use pyo3::{exceptions::PyValueError, prelude::*};
use serde_json::{json as value, Map, Value};

struct DenyRetrieval;

impl Retrieve for DenyRetrieval {
    fn retrieve(&self, uri: &Uri<String>) -> Result<Value, Box<dyn Error + Send + Sync>> {
        Err(format!("comparison kernel refuses retrieval: {uri}").into())
    }
}

fn descriptor(error: &ValidationError<'_>) -> Value {
    // Preserve the engine's branch grouping and iteration order. These are
    // descriptors of Rust errors, not fabricated Python exception objects.
    let context: Vec<Vec<Value>> = match error.kind() {
        ValidationErrorKind::AnyOf { context }
        | ValidationErrorKind::OneOfNotValid { context }
        | ValidationErrorKind::OneOfMultipleValid { context } => context
            .iter()
            .map(|branch| branch.iter().map(descriptor).collect())
            .collect(),
        _ => Vec::new(),
    };
    let property_names_error = match error.kind() {
        ValidationErrorKind::PropertyNames { error } => Some(descriptor(error)),
        _ => None,
    };
    value!({
        "message": error.to_string(),
        "keyword": error.kind().keyword(),
        "instance_clone": error.instance().as_ref(),
        "instance_path": error.instance_path().as_str(),
        "schema_path": error.schema_path().as_str(),
        "evaluation_path": error.evaluation_path().as_str(),
        "absolute_keyword_location": error.absolute_keyword_location().map(ToString::to_string),
        "context": context,
        "property_names_error": property_names_error,
        "source_message": error.source().map(ToString::to_string),
    })
}

/// Inspect the actual engine's build or validation errors against Python data.
///
/// `resources_json` maps explicit URIs to local schema values. For schema
/// selfchecks, pass the original meta-schema as `schema_json`, its registry as
/// `resources_json`, and the actual Python schema object as `instance`.
#[pyfunction]
#[pyo3(signature = (schema_json, instance, resources_json = "{}", *, validate_formats = true))]
fn inspect<'py>(
    py: Python<'py>,
    schema_json: &str,
    instance: &Bound<'py, PyAny>,
    resources_json: &str,
    validate_formats: bool,
) -> PyResult<Bound<'py, PyAny>> {
    let schema: Value = serde_json::from_str(schema_json)
        .map_err(|error| PyValueError::new_err(format!("schema JSON: {error}")))?;
    let resources: Map<String, Value> = serde_json::from_str(resources_json)
        .map_err(|error| PyValueError::new_err(format!("resource JSON object: {error}")))?;
    let registry = Registry::new()
        .draft(Draft::Draft202012)
        .retriever(DenyRetrieval)
        .extend(resources)
        .and_then(|builder| builder.prepare())
        .map_err(|error| PyValueError::new_err(format!("local registry: {error}")))?;
    let options = jsonschema::options_for::<json::Pyo3>()
        .with_draft(Draft::Draft202012)
        .with_registry(&registry)
        .offline()
        .should_validate_formats(validate_formats)
        .should_ignore_unknown_formats(true);
    let report = match options.build(&schema) {
        Ok(validator) => {
            // inspect tracks errors from the Python representation, including
            // unsupported input types. Keep its scope around error rendering.
            let errors: Vec<Value> = json::inspect(instance.as_borrowed(), true, || {
                validator
                    .iter_errors(instance.as_borrowed())
                    .map(|error| descriptor(&error))
                    .collect()
            })?;
            value!({
                "phase": "instance-validation",
                "compiled": true,
                "valid": errors.is_empty(),
                "errors": errors,
            })
        }
        Err(error) => value!({
            "phase": "schema-build",
            "compiled": false,
            "valid": null,
            "errors": [descriptor(&error)],
        }),
    };
    let serialized = serde_json::to_string(&report)
        .map_err(|error| PyValueError::new_err(format!("descriptor JSON: {error}")))?;
    let result = py.import("json")?.call_method1("loads", (serialized,))?;
    // This is the actual input root, kept separately from serde error clones.
    // It does not establish engine-held references to individual error nodes.
    result.set_item("original_instance_root", instance)?;
    result.set_item("production_enabled", false)?;
    Ok(result)
}

/// Install only the private observation function.
#[pymodule]
fn _tobkiri_schema_kernel(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(inspect, module)?)?;
    module.add("production_enabled", false)?;
    Ok(())
}
