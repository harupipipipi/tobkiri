//! CPython 3.13.16 urllib.parse._coerce_args body (see COPYING.python).
//! Values, decoding and result coercion remain actual Python objects/callables.

use pyo3::{
    exceptions::{PyNameError, PyRuntimeError},
    gc::{PyTraverseError, PyVisit},
    prelude::*,
    pyclass::CompareOp,
    types::{PyDict, PyString, PyTuple},
};
use std::sync::atomic::{AtomicUsize, Ordering};

static COERCE_CALLS: AtomicUsize = AtomicUsize::new(0);

struct Context {
    original: Py<PyAny>,
    globals: Py<PyDict>,
    builtins: Py<PyDict>,
}

#[pyclass(module = "_tobkiri_schema_kernel")]
struct CoerceArgs {
    context: Option<Context>,
}

impl Context {
    fn lookup<'py>(&self, py: Python<'py>, name: &str) -> PyResult<Bound<'py, PyAny>> {
        let name = match name {
            "isinstance" => pyo3::intern!(py, "isinstance").clone(),
            "str" => pyo3::intern!(py, "str").clone(),
            "_noop" => pyo3::intern!(py, "_noop").clone(),
            "_decode_args" => pyo3::intern!(py, "_decode_args").clone(),
            "_encode_result" => pyo3::intern!(py, "_encode_result").clone(),
            "_tobkiri_raise_coerce_mixed" => {
                pyo3::intern!(py, "_tobkiri_raise_coerce_mixed").clone()
            }
            _ => PyString::new(py, name),
        };
        if let Some(value) = self.globals.bind(py).get_item(&name)? {
            return Ok(value);
        }
        if let Some(value) = self.builtins.bind(py).get_item(&name)? {
            return Ok(value);
        }
        let error = PyNameError::new_err(format!("name '{name}' is not defined"));
        error.value(py).setattr(pyo3::intern!(py, "name"), name)?;
        Err(error)
    }

    fn is_str<'py>(
        &self,
        py: Python<'py>,
        value: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        // LOAD_GLOBAL callable precedes the class argument; no helper value is
        // cached across a callback which may change its actual namespace.
        let callable = self.lookup(py, "isinstance")?;
        let class = self.lookup(py, "str")?;
        callable.call1((value, class))
    }
}

#[pymethods]
impl CoerceArgs {
    #[pyo3(signature = (*args, **kwargs))]
    fn __call__<'py>(
        &self,
        py: Python<'py>,
        args: &Bound<'py, PyTuple>,
        kwargs: Option<&Bound<'py, PyDict>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let context = self
            .context
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("coercion metadata was cleared"))?;
        if args.len() == 0 || kwargs.is_some_and(|value| !value.is_empty()) {
            // Explicit binding/error-shape dispatch BEFORE native evaluation.
            // The actual original function preserves its no-argument indexing
            // and keyword-rejection diagnostics. Never retry a native failure.
            return context.original.bind(py).call(args, kwargs);
        }
        COERCE_CALLS.fetch_add(1, Ordering::Relaxed);
        let str_input = context.is_str(py, &args.get_item(0)?)?;
        for arg in args.iter().skip(1) {
            if arg.is_truthy()?
                && context
                    .is_str(py, &arg)?
                    .rich_compare(&str_input, CompareOp::Ne)?
                    .is_truthy()?
            {
                // The exact original raise statement runs in Python, retaining
                // handled-exception context and live TypeError resolution.
                context.lookup(py, "_tobkiri_raise_coerce_mixed")?.call0()?;
                return Err(PyRuntimeError::new_err("coercion error helper returned"));
            }
        }
        if str_input.is_truthy()? {
            let suffix = PyTuple::new(py, [context.lookup(py, "_noop")?])?;
            return args.as_any().add(suffix);
        }
        let decoded = context.lookup(py, "_decode_args")?.call1((args,))?;
        // Lookup follows the actual decoder invocation, which can rebind it.
        let suffix = PyTuple::new(py, [context.lookup(py, "_encode_result")?])?;
        decoded.add(suffix)
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Some(context) = &self.context {
            visit.call(&context.original)?;
            visit.call(&context.globals)?;
            visit.call(&context.builtins)?;
        }
        Ok(())
    }

    fn __clear__(&mut self) {
        self.context = None;
    }
}

#[pyfunction]
fn make_coerce_args(
    original: &Bound<'_, PyAny>,
    globals: &Bound<'_, PyDict>,
    builtins: &Bound<'_, PyDict>,
) -> CoerceArgs {
    CoerceArgs {
        context: Some(Context {
            original: original.clone().unbind(),
            globals: globals.clone().unbind(),
            builtins: builtins.clone().unbind(),
        }),
    }
}

#[pyfunction]
fn coerce_args_calls() -> usize {
    COERCE_CALLS.load(Ordering::Relaxed)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<CoerceArgs>()?;
    module.add_function(wrap_pyfunction!(make_coerce_args, module)?)?;
    module.add_function(wrap_pyfunction!(coerce_args_calls, module)?)
}
