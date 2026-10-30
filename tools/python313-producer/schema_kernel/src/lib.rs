//! Experimental original-object schema traversal and keyword port.

mod keywords;
mod traversal;

use pyo3::{
    exceptions::PyValueError,
    prelude::*,
    types::{PyBool, PyDict, PyFloat, PyInt, PyString, PyType},
};
use std::sync::atomic::{AtomicUsize, Ordering};

static FAST_TYPES: AtomicUsize = AtomicUsize::new(0);
static ORIGINAL_TYPES: AtomicUsize = AtomicUsize::new(0);

/// Retain the actual Python objects, without copying or serializing them.
#[pyfunction]
fn retain_objects<'py>(
    py: Python<'py>,
    schema: &Bound<'py, PyAny>,
    instance: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyDict>> {
    let retained = PyDict::new(py);
    retained.set_item("schema", schema)?;
    retained.set_item("instance", instance)?;
    Ok(retained)
}

/// Match jsonschema 4.26.0's Draft 2020-12 checks for the seven JSON types.
#[pyfunction]
fn is_original_json_type(instance: &Bound<'_, PyAny>, kind: &str) -> PyResult<bool> {
    let py = instance.py();
    let builtins = py.import("builtins")?;
    let isinstance = builtins.getattr(pyo3::intern!(py, "isinstance"))?;
    let is_type = |type_name: &str| -> PyResult<bool> {
        isinstance
            .call1((instance, builtins.getattr(type_name)?))?
            .extract()
    };

    match kind {
        "array" => is_type("list"),
        "object" => is_type("dict"),
        "string" => is_type("str"),
        "boolean" => is_type("bool"),
        "null" => Ok(instance.is_none()),
        "integer" => {
            // Draft 6+ permits integral floats as well as non-bool ints.
            if !is_type("bool")? && is_type("int")? {
                Ok(true)
            } else if is_type("float")? {
                instance
                    .call_method0(pyo3::intern!(py, "is_integer"))?
                    .is_truthy()
            } else {
                Ok(false)
            }
        }
        "number" => {
            if is_type("bool")? {
                return Ok(false);
            }
            let number = py.import("numbers")?.getattr(pyo3::intern!(py, "Number"))?;
            isinstance.call1((instance, number))?.extract()
        }
        _ => Err(PyValueError::new_err(format!("unknown JSON type: {kind}"))),
    }
}

/// Dispatch before evaluation; native errors are never retried in Python.
#[pyfunction]
fn fast_is_type<'py>(
    validator: &Bound<'py, PyAny>,
    instance: &Bound<'py, PyAny>,
    kind: &Bound<'py, PyAny>,
    default_class: &Bound<'py, PyAny>,
    default_checker: &Bound<'py, PyAny>,
    default_lookup: &Bound<'py, PyAny>,
    default_method: &Bound<'py, PyAny>,
    original: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let py = validator.py();
    let class = validator.get_type();
    let checker_class = default_checker.get_type();
    let object = py.get_type::<PyAny>();
    if class.as_any().is(default_class)
        && class.get_type().is(&py.get_type::<PyType>())
        && kind.is_exact_instance_of::<PyString>()
        && class.getattr(pyo3::intern!(py, "__base__"))?.is(&object)
        && checker_class.get_type().is(&py.get_type::<PyType>())
        && checker_class
            .getattr(pyo3::intern!(py, "__base__"))?
            .is(&object)
        && !checker_class
            .getattr(pyo3::intern!(py, "__dict__"))?
            .contains(pyo3::intern!(py, "__get__"))?
    {
        let attributes = class.getattr(pyo3::intern!(py, "__dict__"))?;
        let checker_attributes = checker_class.getattr(pyo3::intern!(py, "__dict__"))?;
        let lookup = attributes.call_method1(
            pyo3::intern!(py, "get"),
            (pyo3::intern!(py, "__getattribute__"), default_lookup),
        )?;
        let checker = attributes.call_method1(
            pyo3::intern!(py, "get"),
            (pyo3::intern!(py, "TYPE_CHECKER"),),
        )?;
        let checker_lookup = checker_attributes.call_method1(
            pyo3::intern!(py, "get"),
            (pyo3::intern!(py, "__getattribute__"), default_lookup),
        )?;
        let checker_method = checker_attributes
            .call_method1(pyo3::intern!(py, "get"), (pyo3::intern!(py, "is_type"),))?;
        if !lookup.is(default_lookup)
            || !checker.is(default_checker)
            || !checker_lookup.is(default_lookup)
            || !checker_method.is(default_method)
        {
            ORIGINAL_TYPES.fetch_add(1, Ordering::Relaxed);
            return original.call1((validator, instance, kind));
        }
        // Compare actual Unicode strings rather than UTF-8-converting an
        // unknown name (surrogates must take the original exception path).
        for (name, identifier) in [
            ("object", pyo3::intern!(py, "object")),
            ("string", pyo3::intern!(py, "string")),
            ("array", pyo3::intern!(py, "array")),
            ("boolean", pyo3::intern!(py, "boolean")),
            ("null", pyo3::intern!(py, "null")),
            ("integer", pyo3::intern!(py, "integer")),
            ("number", pyo3::intern!(py, "number")),
        ] {
            if !kind.eq(identifier)? {
                continue;
            }
            // A float subclass may override is_integer and return an arbitrary
            // object. Preserve its original return value/exception by dispatch.
            if name == "integer"
                && !instance.is_exact_instance_of::<PyInt>()
                && !instance.is_exact_instance_of::<PyBool>()
                && !instance.is_exact_instance_of::<PyFloat>()
            {
                break;
            }
            FAST_TYPES.fetch_add(1, Ordering::Relaxed);
            return Ok(is_original_json_type(instance, name)?
                .into_pyobject(py)?
                .to_owned()
                .into_any());
        }
    }
    ORIGINAL_TYPES.fetch_add(1, Ordering::Relaxed);
    original.call1((validator, instance, kind))
}

/// Diagnostics only: execution counts, never cached type/validation results.
#[pyfunction]
fn type_dispatch_counts(py: Python<'_>) -> PyResult<Bound<'_, PyDict>> {
    let counts = PyDict::new(py);
    counts.set_item("native_default", FAST_TYPES.load(Ordering::Relaxed))?;
    counts.set_item("explicit_original", ORIGINAL_TYPES.load(Ordering::Relaxed))?;
    Ok(counts)
}

#[pymodule]
fn _tobkiri_schema_kernel(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(retain_objects, module)?)?;
    module.add_function(wrap_pyfunction!(is_original_json_type, module)?)?;
    module.add_function(wrap_pyfunction!(fast_is_type, module)?)?;
    module.add_function(wrap_pyfunction!(type_dispatch_counts, module)?)?;
    keywords::register(module)?;
    traversal::register(module)?;
    module.add("production_enabled", false)?;
    module.add("implemented_handler_count", 36)?;
    Ok(())
}
