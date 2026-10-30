//! Direct control-flow port of CPython 3.13.16 urllib.parse.urljoin.
//! Source: https://github.com/python/cpython/blob/v3.13.16/Lib/urllib/parse.py
//! Python-2.0; original terms/notices are retained in ../COPYING.python.
//! Coercion, parse results, helpers, configuration and caches remain Python's.

use pyo3::{
    call::PyCallArgs,
    exceptions::{PyBaseException, PyRuntimeError, PyTypeError, PyValueError},
    gc::{PyTraverseError, PyVisit},
    prelude::*,
    pyclass::CompareOp,
    types::{PyDict, PyList, PySlice, PyString, PyTuple, PyType, PyTypeMethods},
};
use std::sync::atomic::{AtomicUsize, Ordering};

static CALLS: AtomicUsize = AtomicUsize::new(0);

struct State {
    globals: Py<PyDict>,
    builtins: Py<PyDict>,
    empty: Py<PyAny>,
    slash: Py<PyAny>,
    dot: Py<PyAny>,
    parent: Py<PyAny>,
    trailing: Py<PyTuple>,
}

#[pyclass(module = "_tobkiri_schema_kernel")]
struct UrlJoinContext {
    state: Option<State>,
}

#[pymethods]
impl UrlJoinContext {
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Some(s) = &self.state {
            visit.call(&s.globals)?;
            visit.call(&s.builtins)?;
            visit.call(&s.empty)?;
            visit.call(&s.slash)?;
            visit.call(&s.dot)?;
            visit.call(&s.parent)?;
            visit.call(&s.trailing)?;
        }
        Ok(())
    }

    fn __clear__(&mut self) {
        self.state = None;
    }
}

#[pyfunction]
fn make_urljoin_context(original: &Bound<'_, PyAny>) -> PyResult<UrlJoinContext> {
    let py = original.py();
    let constants = original
        .getattr(pyo3::intern!(py, "__code__"))?
        .getattr(pyo3::intern!(py, "co_consts"))?
        .cast_into::<PyTuple>()?;
    let constant = |name: &str| -> PyResult<Py<PyAny>> {
        for value in constants.iter() {
            if value.is_exact_instance_of::<PyString>() && value.eq(name)? {
                return Ok(value.unbind());
            }
        }
        Err(PyRuntimeError::new_err(
            "original urljoin literal unavailable",
        ))
    };
    let mut trailing = None;
    for value in constants.iter() {
        if let Ok(tuple) = value.cast::<PyTuple>() {
            if tuple.len() == 2 && tuple.get_item(0)?.eq(".")? && tuple.get_item(1)?.eq("..")? {
                trailing = Some(tuple.clone().unbind());
                break;
            }
        }
    }
    Ok(UrlJoinContext {
        state: Some(State {
            globals: original
                .getattr(pyo3::intern!(py, "__globals__"))?
                .cast_into()?
                .unbind(),
            builtins: original
                .getattr(pyo3::intern!(py, "__builtins__"))?
                .cast_into()?
                .unbind(),
            empty: constant("")?,
            slash: constant("/")?,
            dot: constant(".")?,
            parent: constant("..")?,
            trailing: trailing
                .ok_or_else(|| PyRuntimeError::new_err("original trailing literals unavailable"))?,
        }),
    })
}

fn global<'py>(s: &State, name: &Bound<'py, PyString>) -> PyResult<Bound<'py, PyAny>> {
    let py = name.py();
    if let Some(value) = s.globals.bind(py).get_item(name)? {
        return Ok(value);
    }
    if let Some(value) = s.builtins.bind(py).get_item(name)? {
        return Ok(value);
    }
    let error =
        pyo3::exceptions::PyNameError::new_err(format!("name '{}' is not defined", name.to_str()?));
    error.value(py).setattr(pyo3::intern!(py, "name"), name)?;
    Err(error)
}

fn unpack<'py>(value: &Bound<'py, PyAny>, count: usize) -> PyResult<Vec<Bound<'py, PyAny>>> {
    // CPython's UNPACK_SEQUENCE also handles exact tuple/list inputs without
    // invoking their iteration protocol; subclasses retain the real iterator.
    if value.is_exact_instance_of::<PyTuple>() || value.is_exact_instance_of::<PyList>() {
        let length = value.len()?;
        if length < count {
            return Err(PyValueError::new_err(format!(
                "not enough values to unpack (expected {count}, got {length})"
            )));
        }
        if length > count {
            return Err(PyValueError::new_err(format!(
                "too many values to unpack (expected {count})"
            )));
        }
        return (0..count).map(|index| value.get_item(index)).collect();
    }
    let mut cursor = value.try_iter()?;
    let mut values = Vec::with_capacity(count);
    for index in 0..count {
        values.push(cursor.next().transpose()?.ok_or_else(|| {
            PyValueError::new_err(format!(
                "not enough values to unpack (expected {count}, got {index})"
            ))
        })?);
    }
    if cursor.next().transpose()?.is_some() {
        return Err(PyValueError::new_err(format!(
            "too many values to unpack (expected {count})"
        )));
    }
    Ok(values)
}

fn call<'py, A: PyCallArgs<'py>>(
    callable: Bound<'py, PyAny>,
    arguments: A,
) -> PyResult<Bound<'py, PyAny>> {
    // Release the consumed callable before the caller unpacks/coerces its
    // returned object, matching the Python CALL evaluation-stack lifetime.
    let result = callable.call1(arguments);
    drop(callable);
    result
}

// Actual function locals remain alive through later callbacks. Release slots in
// the original co_varnames order, including early returns and exceptional exits.
#[derive(Clone, Copy)]
#[repr(usize)]
enum Slot {
    Base,
    Url,
    Flags,
    Coerce,
    BScheme,
    BNetloc,
    BPath,
    BParams,
    BQuery,
    BFragment,
    Scheme,
    Netloc,
    Path,
    Params,
    Query,
    Fragment,
    BaseParts,
    Segments,
    Resolved,
    Segment,
}
struct Locals<'py>(Vec<Option<Bound<'py, PyAny>>>);
impl<'py> Locals<'py> {
    fn get(&self, slot: Slot) -> &Bound<'py, PyAny> {
        self.0[slot as usize]
            .as_ref()
            .expect("assigned original local")
    }
    fn put(&mut self, slot: Slot, value: Bound<'py, PyAny>) {
        self.0[slot as usize] = Some(value);
    }
    fn assign(&mut self, slots: &[Slot], values: Vec<Bound<'py, PyAny>>) {
        for (slot, value) in slots.iter().zip(values) {
            self.put(*slot, value);
        }
    }
}
impl Drop for Locals<'_> {
    fn drop(&mut self) {
        for slot in &mut self.0 {
            drop(slot.take());
        }
    }
}

fn catch_target_valid(target: &Bound<'_, PyAny>) -> PyResult<bool> {
    if let Ok(tuple) = target.cast::<PyTuple>() {
        for value in tuple.iter() {
            let Ok(class) = value.cast::<PyType>() else {
                return Ok(false);
            };
            if !class.is_subclass_of::<PyBaseException>()? {
                return Ok(false);
            }
        }
        return Ok(true);
    }
    match target.cast::<PyType>() {
        Ok(class) => class.is_subclass_of::<PyBaseException>(),
        Err(_) => Ok(false),
    }
}

fn render<'py>(
    s: &State,
    py: Python<'py>,
    l: &Locals<'py>,
    path: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    // LOAD_FAST coerce_result precedes LOAD_GLOBAL urlunparse in the original.
    let coerce = l.get(Slot::Coerce);
    let unparse = global(s, pyo3::intern!(py, "urlunparse"))?;
    let parts = PyTuple::new(
        py,
        [
            l.get(Slot::Scheme),
            l.get(Slot::Netloc),
            path,
            l.get(Slot::Params),
            l.get(Slot::Query),
            l.get(Slot::Fragment),
        ],
    )?;
    let value = call(unparse, (parts,))?;
    coerce.call1((value,))
}

#[pyfunction]
fn urljoin<'py>(
    base: &Bound<'py, PyAny>,
    url: &Bound<'py, PyAny>,
    flags: &Bound<'py, PyAny>,
    context: Py<UrlJoinContext>,
) -> PyResult<Bound<'py, PyAny>> {
    use Slot::*;
    let py = base.py();
    CALLS.fetch_add(1, Ordering::Relaxed);
    let context = context.borrow(py);
    let s = context
        .state
        .as_ref()
        .ok_or_else(|| PyRuntimeError::new_err("urljoin context cleared"))?;
    let mut l = Locals(vec![None; 20]);
    l.put(Base, base.clone());
    l.put(Url, url.clone());
    l.put(Flags, flags.clone());
    if !l.get(Base).is_truthy()? {
        return Ok(l.get(Url).clone());
    }
    if !l.get(Url).is_truthy()? {
        return Ok(l.get(Base).clone());
    }
    let values = unpack(
        &call(
            global(s, pyo3::intern!(py, "_coerce_args"))?,
            (l.get(Base), l.get(Url)),
        )?,
        3,
    )?;
    l.assign(&[Base, Url, Coerce], values);
    let values = unpack(
        &call(
            global(s, pyo3::intern!(py, "urlparse"))?,
            (l.get(Base), s.empty.bind(py), l.get(Flags)),
        )?,
        6,
    )?;
    l.assign(
        &[BScheme, BNetloc, BPath, BParams, BQuery, BFragment],
        values,
    );
    let values = unpack(
        &call(
            global(s, pyo3::intern!(py, "urlparse"))?,
            (l.get(Url), l.get(BScheme), l.get(Flags)),
        )?,
        6,
    )?;
    l.assign(&[Scheme, Netloc, Path, Params, Query, Fragment], values);
    if l.get(Scheme)
        .rich_compare(l.get(BScheme), CompareOp::Ne)?
        .is_truthy()?
        || !global(s, pyo3::intern!(py, "uses_relative"))?.contains(l.get(Scheme))?
    {
        return l.get(Coerce).call1((l.get(Url),));
    }
    if global(s, pyo3::intern!(py, "uses_netloc"))?.contains(l.get(Scheme))? {
        if l.get(Netloc).is_truthy()? {
            return render(s, py, &l, l.get(Path));
        }
        l.put(Netloc, l.get(BNetloc).clone());
    }
    if !l.get(Path).is_truthy()? && !l.get(Params).is_truthy()? {
        l.put(Path, l.get(BPath).clone());
        l.put(Params, l.get(BParams).clone());
        if !l.get(Query).is_truthy()? {
            l.put(Query, l.get(BQuery).clone());
        }
        return render(s, py, &l, l.get(Path));
    }
    let base_parts = l
        .get(BPath)
        .call_method1(pyo3::intern!(py, "split"), (s.slash.bind(py),))?;
    l.put(BaseParts, base_parts);
    if l.get(BaseParts)
        .get_item(-1)?
        .rich_compare(s.empty.bind(py), CompareOp::Ne)?
        .is_truthy()?
    {
        l.get(BaseParts).del_item(-1)?;
    }
    if l.get(Path)
        .get_item(py.get_type::<PySlice>().call1((py.None(), 1, py.None()))?)?
        .rich_compare(s.slash.bind(py), CompareOp::Eq)?
        .is_truthy()?
    {
        l.put(
            Segments,
            l.get(Path)
                .call_method1(pyo3::intern!(py, "split"), (s.slash.bind(py),))?,
        );
    } else {
        let parts = l
            .get(Path)
            .call_method1(pyo3::intern!(py, "split"), (s.slash.bind(py),))?;
        l.put(Segments, l.get(BaseParts).add(parts)?);
        let filter = global(s, pyo3::intern!(py, "filter"))?;
        let selected =
            l.get(Segments)
                .get_item(py.get_type::<PySlice>().call1((1, -1, py.None()))?)?;
        let filtered = call(filter, (py.None(), selected))?;
        let destination = py.get_type::<PySlice>().call1((1, -1, py.None()))?;
        l.get(Segments).set_item(destination, filtered)?;
    }
    l.put(Resolved, PyList::empty(py).into_any());
    let segments = l.get(Segments).try_iter()?;
    for segment in segments {
        l.put(Segment, segment?);
        if l.get(Segment)
            .rich_compare(s.parent.bind(py), CompareOp::Eq)?
            .is_truthy()?
        {
            if let Err(error) = l.get(Resolved).call_method0(pyo3::intern!(py, "pop")) {
                let target = global(s, pyo3::intern!(py, "IndexError")).map_err(|failure| {
                    failure.set_context(py, Some(error.clone_ref(py)));
                    failure
                })?;
                if !catch_target_valid(&target)? {
                    let failure = PyTypeError::new_err(
                        "catching classes that do not inherit from BaseException is not allowed",
                    );
                    failure.set_context(py, Some(error));
                    return Err(failure);
                }
                if !error.is_instance(py, &target) {
                    return Err(error);
                }
            }
        } else if l
            .get(Segment)
            .rich_compare(s.dot.bind(py), CompareOp::Eq)?
            .is_truthy()?
        {
            continue;
        } else {
            l.get(Resolved)
                .call_method1(pyo3::intern!(py, "append"), (l.get(Segment),))?;
        }
    }
    if s.trailing
        .bind(py)
        .contains(l.get(Segments).get_item(-1)?)?
    {
        l.get(Resolved)
            .call_method1(pyo3::intern!(py, "append"), (s.empty.bind(py),))?;
    }
    // Evaluate the original urlunparse callable before evaluating join(...).
    let coerce = l.get(Coerce);
    let unparse = global(s, pyo3::intern!(py, "urlunparse"))?;
    let joined = s
        .slash
        .bind(py)
        .call_method1(pyo3::intern!(py, "join"), (l.get(Resolved),))?;
    let path = if joined.is_truthy()? {
        joined
    } else {
        s.slash.bind(py).clone()
    };
    let parts = PyTuple::new(
        py,
        [
            l.get(Scheme),
            l.get(Netloc),
            &path,
            l.get(Params),
            l.get(Query),
            l.get(Fragment),
        ],
    )?;
    let value = call(unparse, (parts,))?;
    coerce.call1((value,))
}

#[pyfunction]
fn urljoin_calls() -> usize {
    CALLS.load(Ordering::Relaxed)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<UrlJoinContext>()?;
    module.add_function(wrap_pyfunction!(make_urljoin_context, module)?)?;
    module.add_function(wrap_pyfunction!(urljoin, module)?)?;
    module.add_function(wrap_pyfunction!(urljoin_calls, module)?)?;
    Ok(())
}
