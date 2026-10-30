//! Private Python-object primitives. This module does not validate schemas.

use pyo3::{exceptions::PyValueError, prelude::*, types::PyDict};

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
    let isinstance = builtins.getattr("isinstance")?;
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
                instance.call_method0("is_integer")?.is_truthy()
            } else {
                Ok(false)
            }
        }
        "number" => {
            if is_type("bool")? {
                return Ok(false);
            }
            let number = py.import("numbers")?.getattr("Number")?;
            isinstance.call1((instance, number))?.extract()
        }
        _ => Err(PyValueError::new_err(format!("unknown JSON type: {kind}"))),
    }
}

#[pymodule]
fn _tobkiri_schema_kernel(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(retain_objects, module)?)?;
    module.add_function(wrap_pyfunction!(is_original_json_type, module)?)?;
    module.add("production_enabled", false)?;
    module.add("implemented_handler_count", 0)?;
    Ok(())
}
