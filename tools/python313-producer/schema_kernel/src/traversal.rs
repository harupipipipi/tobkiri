//! Lazy traversal port of jsonschema 4.26.0 (see ../COPYING.jsonschema).
//! Python Validator instances still own their schema, registry and resolver.

use pyo3::{
    exceptions::PyRuntimeError,
    gc::{PyTraverseError, PyVisit},
    prelude::*,
    types::{PyDict, PyIterator, PyList, PyTuple},
};

struct ContextState {
    applicable: Py<PyAny>,
    specification: Py<PyAny>,
    delegate: Py<PyAny>,
    evolve_fields: Py<PyAny>,
    evolve_globals: Option<Py<PyDict>>,
    evolve_builtins: Option<Py<PyDict>>,
    handlers: Vec<(String, Py<PyAny>)>,
}

/// Captured original closure objects and builtin callable identities, not schemas.
#[pyclass(module = "_tobkiri_schema_kernel")]
struct KernelContext {
    state: Option<ContextState>,
}

#[pymethods]
impl KernelContext {
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Some(state) = &self.state {
            visit.call(&state.applicable)?;
            visit.call(&state.specification)?;
            visit.call(&state.delegate)?;
            visit.call(&state.evolve_fields)?;
            if let Some(value) = &state.evolve_globals {
                visit.call(value)?;
            }
            if let Some(value) = &state.evolve_builtins {
                visit.call(value)?;
            }
            for (_, callable) in &state.handlers {
                visit.call(callable)?;
            }
        }
        Ok(())
    }

    fn __clear__(&mut self) {
        self.state = None;
    }
}

#[pyfunction]
fn make_context(
    applicable: &Bound<'_, PyAny>,
    specification: &Bound<'_, PyAny>,
    handlers: &Bound<'_, PyDict>,
    delegate: &Bound<'_, PyAny>,
    evolve_fields: &Bound<'_, PyAny>,
    evolve_globals: &Bound<'_, PyAny>,
    evolve_builtins: &Bound<'_, PyAny>,
) -> PyResult<KernelContext> {
    let mut aliases = Vec::with_capacity(handlers.len());
    for (keyword, callable) in handlers {
        aliases.push((keyword.extract::<String>()?, callable.unbind()));
    }
    Ok(KernelContext {
        state: Some(ContextState {
            applicable: applicable.clone().unbind(),
            specification: specification.clone().unbind(),
            delegate: delegate.clone().unbind(),
            evolve_fields: evolve_fields.clone().unbind(),
            evolve_globals: if evolve_globals.is_none() {
                None
            } else {
                Some(evolve_globals.cast::<PyDict>()?.clone().unbind())
            },
            evolve_builtins: if evolve_builtins.is_none() {
                None
            } else {
                Some(evolve_builtins.cast::<PyDict>()?.clone().unbind())
            },
            handlers: aliases,
        }),
    })
}

fn global_name<'py>(
    state: &ContextState,
    py: Python<'py>,
    name: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let globals = state
        .evolve_globals
        .as_ref()
        .ok_or_else(|| PyRuntimeError::new_err("native evolve context is unavailable"))?;
    let builtins = state
        .evolve_builtins
        .as_ref()
        .ok_or_else(|| PyRuntimeError::new_err("native evolve builtins are unavailable"))?;
    lookup_name(globals.bind(py), builtins.bind(py), name)
}

fn lookup_name<'py>(
    globals: &Bound<'py, PyDict>,
    builtins: &Bound<'py, PyDict>,
    name: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let py = globals.py();
    if let Some(value) = globals.get_item(name)? {
        return Ok(value);
    }
    if let Some(value) = builtins.get_item(name)? {
        return Ok(value);
    }
    let error = pyo3::exceptions::PyNameError::new_err(format!("name '{name}' is not defined"));
    error.value(py).setattr("name", name)?;
    Err(error)
}

/// Original attrs post-init body; the genuine generated constructor still runs.
// Keep original Python locals alive through subsequent callbacks. Struct fields
// drop in declaration order, matching the original fast-local declaration order.
struct PostInitLocals<'py> {
    registry: Option<Bound<'py, PyAny>>,
    resource: Option<Bound<'py, PyAny>>,
    push_scope: Option<Bound<'py, PyAny>>,
    id: Option<Bound<'py, PyAny>>,
}

#[pyfunction]
fn post_init<'py>(
    validator: &Bound<'py, PyAny>,
    applicable: &Bound<'py, PyAny>,
    specification: &Bound<'py, PyAny>,
    id_of: &Bound<'py, PyAny>,
    globals: &Bound<'py, PyDict>,
    builtins: &Bound<'py, PyDict>,
) -> PyResult<()> {
    let py = validator.py();
    let mut locals = PostInitLocals {
        registry: None,
        resource: None,
        push_scope: None,
        id: None,
    };
    if validator.getattr("_resolver")?.is_none() {
        locals.registry = Some(validator.getattr("_registry")?);
        if !locals.registry.as_ref().unwrap().is(&lookup_name(
            globals,
            builtins,
            "_REMOTE_WARNING_REGISTRY",
        )?) {
            locals.registry = Some(
                lookup_name(globals, builtins, "SPECIFICATIONS")?
                    .call_method1("combine", (locals.registry.as_ref().unwrap(),))?,
            );
        }
        let create_resource = specification.getattr("create_resource")?;
        locals.resource = Some(create_resource.call1((validator.getattr("schema")?,))?);
        drop(create_resource);
        let resolver = locals
            .registry
            .as_ref()
            .unwrap()
            .call_method1("resolver_with_root", (locals.resource.as_ref().unwrap(),))?;
        validator.setattr("_resolver", resolver)?;
    }
    // Attribute reads and short-circuit order match the original Python body.
    let is_true = validator.getattr("schema")?.is(&true.into_pyobject(py)?);
    let is_false = !is_true && validator.getattr("schema")?.is(&false.into_pyobject(py)?);
    let selected = PyList::empty(py);
    if !is_true && !is_false {
        let pairs = applicable
            .call1((validator.getattr("schema")?,))?
            .try_iter()?;
        for pair in pairs {
            let (keyword, value) = field_pair(&pair?)?;
            if validator.getattr("VALIDATORS")?.contains(&keyword)? {
                let callable = validator.getattr("VALIDATORS")?.get_item(&keyword)?;
                selected.append((callable, keyword, value))?;
            }
        }
    }
    validator.setattr("_validators", selected)?;
    let getattr = lookup_name(globals, builtins, "getattr")?;
    locals.push_scope =
        Some(getattr.call1((validator.getattr("_ref_resolver")?, "push_scope", py.None()))?);
    drop(getattr);
    if !locals.push_scope.as_ref().unwrap().is_none() {
        locals.id = Some(id_of.call1((validator.getattr("schema")?,))?);
        if !locals.id.as_ref().unwrap().is_none() {
            locals
                .push_scope
                .as_ref()
                .unwrap()
                .call1((locals.id.as_ref().unwrap(),))?;
        }
    }
    Ok(())
}

fn field_pair<'py>(field: &Bound<'py, PyAny>) -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyAny>)> {
    if field.is_exact_instance_of::<PyTuple>() && field.len()? == 2 {
        return Ok((field.get_item(0)?, field.get_item(1)?));
    }
    let mut fields = field.try_iter()?;
    let mut next = |count| {
        fields.next().transpose()?.ok_or_else(|| {
            pyo3::exceptions::PyValueError::new_err(format!(
                "not enough values to unpack (expected 2, got {count})"
            ))
        })
    };
    let first = next(0)?;
    let second = next(1)?;
    if fields.next().transpose()?.is_some() {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "too many values to unpack (expected 2)",
        ));
    }
    Ok((first, second))
}

#[pyfunction]
fn evolve<'py>(
    validator: &Bound<'py, PyAny>,
    changes: &Bound<'py, PyDict>,
    context: Py<KernelContext>,
) -> PyResult<Bound<'py, PyAny>> {
    let py = validator.py();
    let context = context.borrow(py);
    let state = context
        .state
        .as_ref()
        .ok_or_else(|| PyRuntimeError::new_err("schema kernel context was cleared"))?;
    // The default argument is evaluated even when schema already exists.
    let previous_schema = validator.getattr("schema")?;
    let schema = changes.call_method1("setdefault", ("schema", previous_schema))?;
    let selector = global_name(state, py, "validator_for")?;
    let options = PyDict::new(py);
    options.set_item("default", validator.getattr("__class__")?)?;
    let selected = selector.call((schema,), Some(&options))?;
    // Iterate the actual closure list live; callbacks can mutate it mid-call.
    for field in state.evolve_fields.bind(py).try_iter()? {
        let (attribute, argument) = field_pair(&field?)?;
        if !changes.contains(&argument)? {
            let value = global_name(state, py, "getattr")?.call1((validator, attribute))?;
            changes.set_item(argument, value)?;
        }
    }
    selected.call((), Some(changes))
}

#[derive(Clone, Copy)]
enum Mode {
    Root,
    Descend,
}

struct Current {
    callable: Py<PyAny>,
    keyword: Py<PyAny>,
    value: Py<PyAny>,
}

struct Traversal {
    mode: Mode,
    started: bool,
    false_yielded: bool,
    validator: Py<PyAny>,
    instance: Py<PyAny>,
    schema: Py<PyAny>,
    path: Py<PyAny>,
    schema_path: Py<PyAny>,
    resolver: Py<PyAny>,
    context: Py<KernelContext>,
    evolved: Option<Py<PyAny>>,
    validators: Option<Py<PyIterator>>,
    current: Option<Current>,
    errors_source: Option<Py<PyAny>>,
    errors_iter: Option<Py<PyIterator>>,
    last_error: Option<Py<PyAny>>,
}

fn next_item(py: Python<'_>, iterator: &Py<PyIterator>) -> PyResult<Option<Py<PyAny>>> {
    let mut iterator = iterator.bind(py).clone();
    iterator
        .next()
        .transpose()
        .map(|item| item.map(Bound::unbind))
}

impl Traversal {
    fn start(&mut self, py: Python<'_>) -> PyResult<()> {
        // Exactly the work performed on the first next(), not iterator creation.
        let validator = self.validator.bind(py);
        if matches!(self.mode, Mode::Root) {
            if !self.schema.bind(py).is_none() {
                let kwargs = PyDict::new(py);
                kwargs.set_item("stacklevel", 2)?;
                py.import("warnings")?.getattr("warn")?.call(
                    (
                        "Passing a schema to Validator.iter_errors is deprecated and will be removed in a future release. Call validator.evolve(schema=new_schema).iter_errors(...) instead.",
                        py.import("builtins")?.getattr("DeprecationWarning")?,
                    ),
                    Some(&kwargs),
                )?;
                let context = self.context.borrow(py);
                let context = context
                    .state
                    .as_ref()
                    .ok_or_else(|| PyRuntimeError::new_err("schema kernel context was cleared"))?;
                let entries = context.applicable.bind(py).call1((self.schema.bind(py),))?;
                let selected = PyList::empty(py);
                for entry in entries.try_iter()? {
                    let entry = entry?;
                    let key = entry.get_item(0)?;
                    if validator.getattr("VALIDATORS")?.contains(&key)? {
                        selected.append((
                            validator.getattr("VALIDATORS")?.get_item(&key)?,
                            key,
                            entry.get_item(1)?,
                        ))?;
                    }
                }
                self.validators = Some(selected.try_iter()?.unbind());
            } else {
                self.schema = validator.getattr("schema")?.unbind();
                self.validators = Some(validator.getattr("_validators")?.try_iter()?.unbind());
            }
        } else {
            let schema = self.schema.bind(py);
            if schema.is(&true.into_pyobject(py)?) || schema.is(&false.into_pyobject(py)?) {
                self.started = true;
                return Ok(());
            }
            let kwargs = PyDict::new(py);
            kwargs.set_item("schema", schema)?;
            if validator.getattr("_ref_resolver")?.is_none() {
                if self.resolver.bind(py).is_none() {
                    let context = self.context.borrow(py);
                    let context = context.state.as_ref().ok_or_else(|| {
                        PyRuntimeError::new_err("schema kernel context was cleared")
                    })?;
                    let resource = context
                        .specification
                        .bind(py)
                        .call_method1("create_resource", (schema,))?;
                    self.resolver = validator
                        .getattr("_resolver")?
                        .call_method1("in_subresource", (resource,))?
                        .unbind();
                }
                kwargs.set_item("_resolver", self.resolver.bind(py))?;
            }
            self.evolved = Some(validator.call_method("evolve", (), Some(&kwargs))?.unbind());
            let context = self.context.borrow(py);
            let context = context
                .state
                .as_ref()
                .ok_or_else(|| PyRuntimeError::new_err("schema kernel context was cleared"))?;
            self.validators = Some(
                context
                    .applicable
                    .bind(py)
                    .call1((schema,))?
                    .try_iter()?
                    .unbind(),
            );
        }
        self.started = true;
        Ok(())
    }

    fn next_error(&mut self, py: Python<'_>) -> PyResult<Option<Py<PyAny>>> {
        if !self.started {
            self.start(py)?;
        }
        let schema = self.schema.bind(py);
        if schema.is(&true.into_pyobject(py)?) || self.false_yielded {
            return Ok(None);
        }
        if schema.is(&false.into_pyobject(py)?) {
            let kwargs = PyDict::new(py);
            kwargs.set_item("validator", py.None())?;
            kwargs.set_item("validator_value", py.None())?;
            kwargs.set_item("instance", self.instance.bind(py))?;
            kwargs.set_item("schema", schema)?;
            let message = format!(
                "False schema does not allow {}",
                self.instance.bind(py).repr()?.to_str()?
            );
            let error = py
                .import("jsonschema.exceptions")?
                .getattr("ValidationError")?
                .call((message,), Some(&kwargs))?
                .unbind();
            self.false_yielded = true;
            self.last_error = Some(error.clone_ref(py));
            // The original early false-schema branch does not prepend paths.
            return Ok(Some(error));
        }
        loop {
            if let Some(iterator) = &self.errors_iter {
                if let Some(error) = next_item(py, iterator)? {
                    self.last_error = Some(error.clone_ref(py));
                    let current = self.current.as_ref().expect("active handler");
                    let validator = self.evolved.as_ref().unwrap_or(&self.validator).bind(py);
                    let kwargs = PyDict::new(py);
                    kwargs.set_item("validator", current.keyword.bind(py))?;
                    kwargs.set_item("validator_value", current.value.bind(py))?;
                    kwargs.set_item("instance", self.instance.bind(py))?;
                    kwargs.set_item("schema", schema)?;
                    kwargs.set_item("type_checker", validator.getattr("TYPE_CHECKER")?)?;
                    let bound = error.bind(py);
                    bound.call_method("_set", (), Some(&kwargs))?;
                    let key = current.keyword.bind(py);
                    if !key.eq("if")? && !key.eq("$ref")? {
                        bound
                            .getattr("schema_path")?
                            .call_method1("appendleft", (key,))?;
                    }
                    if matches!(self.mode, Mode::Descend) {
                        if !self.path.bind(py).is_none() {
                            bound
                                .getattr("path")?
                                .call_method1("appendleft", (self.path.bind(py),))?;
                        }
                        if !self.schema_path.bind(py).is_none() {
                            bound
                                .getattr("schema_path")?
                                .call_method1("appendleft", (self.schema_path.bind(py),))?;
                        }
                    }
                    return Ok(Some(error));
                }
                self.errors_iter = None;
            }
            let Some(entry) = next_item(py, self.validators.as_ref().expect("started traversal"))?
            else {
                return Ok(None);
            };
            let entry = entry.bind(py);
            let validator = self.evolved.as_ref().unwrap_or(&self.validator).bind(py);
            let (callable, keyword, value) = match self.mode {
                Mode::Root => (entry.get_item(0)?, entry.get_item(1)?, entry.get_item(2)?),
                Mode::Descend => {
                    let key = entry.get_item(0)?;
                    let callable = validator
                        .getattr("VALIDATORS")?
                        .call_method1("get", (&key,))?;
                    if callable.is_none() {
                        continue;
                    }
                    (callable, key, entry.get_item(1)?)
                }
            };
            self.current = Some(Current {
                callable: callable.unbind(),
                keyword: keyword.unbind(),
                value: value.unbind(),
            });
            let current = self.current.as_ref().expect("selected handler");
            let context = self.context.borrow(py);
            let context = context
                .state
                .as_ref()
                .ok_or_else(|| PyRuntimeError::new_err("schema kernel context was cleared"))?;
            let native = context
                .handlers
                .iter()
                .find(|(_, original)| original.bind(py).is(current.callable.bind(py)));
            let errors = if let Some((keyword, _)) = native {
                let iterator = crate::keywords::iterator(
                    py,
                    keyword,
                    validator,
                    current.value.bind(py),
                    self.instance.bind(py),
                    schema,
                )?;
                context.delegate.bind(py).call1((iterator,))?
            } else {
                // Explicit custom/other-draft callable dispatch, not retrying a
                // failed native builtin or deciding validity in Python first.
                current.callable.bind(py).call1((
                    validator,
                    current.value.bind(py),
                    self.instance.bind(py),
                    schema,
                ))?
            };
            let errors = if errors.is_truthy()? {
                errors
            } else {
                PyTuple::empty(py).into_any()
            };
            self.errors_source = Some(errors.unbind());
            self.errors_iter = Some(
                self.errors_source
                    .as_ref()
                    .expect("error source")
                    .bind(py)
                    .try_iter()?
                    .unbind(),
            );
        }
    }

    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.validator)?;
        visit.call(&self.instance)?;
        visit.call(&self.schema)?;
        visit.call(&self.path)?;
        visit.call(&self.schema_path)?;
        visit.call(&self.resolver)?;
        visit.call(&self.context)?;
        if let Some(value) = &self.evolved {
            visit.call(value)?;
        }
        if let Some(value) = &self.validators {
            visit.call(value)?;
        }
        if let Some(value) = &self.errors_source {
            visit.call(value)?;
        }
        if let Some(value) = &self.errors_iter {
            visit.call(value)?;
        }
        if let Some(value) = &self.last_error {
            visit.call(value)?;
        }
        if let Some(current) = &self.current {
            visit.call(&current.callable)?;
            visit.call(&current.keyword)?;
            visit.call(&current.value)?;
        }
        Ok(())
    }
}

#[pyclass(module = "_tobkiri_schema_kernel")]
struct ValidationErrors {
    state: Option<Traversal>,
}

#[pymethods]
impl ValidationErrors {
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    fn __next__(&mut self, py: Python<'_>) -> PyResult<Option<Py<PyAny>>> {
        let result = match &mut self.state {
            Some(state) => state.next_error(py),
            None => Ok(None),
        }
        .map_err(|error| {
            if error.is_instance_of::<pyo3::exceptions::PyStopIteration>(py) {
                let wrapped = PyRuntimeError::new_err("generator raised StopIteration");
                if let Err(failure) = wrapped.value(py).setattr("__context__", error.value(py)) {
                    return failure;
                }
                wrapped.set_cause(py, Some(error));
                wrapped
            } else {
                error
            }
        });
        if !matches!(result, Ok(Some(_))) {
            self.state = None;
        }
        result
    }

    fn send(&mut self, py: Python<'_>, _value: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        self.__next__(py)?
            .ok_or_else(|| pyo3::exceptions::PyStopIteration::new_err(()))
    }

    fn close(&mut self) {
        self.state = None;
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Some(state) = &self.state {
            state.traverse(&visit)?;
        }
        Ok(())
    }

    fn __clear__(&mut self) {
        self.state = None;
    }
}

#[pyfunction]
fn iter_errors(
    py: Python<'_>,
    validator: &Bound<'_, PyAny>,
    instance: &Bound<'_, PyAny>,
    schema: &Bound<'_, PyAny>,
    context: Py<KernelContext>,
) -> ValidationErrors {
    ValidationErrors {
        state: Some(Traversal {
            mode: Mode::Root,
            started: false,
            false_yielded: false,
            validator: validator.clone().unbind(),
            instance: instance.clone().unbind(),
            schema: schema.clone().unbind(),
            path: py.None(),
            schema_path: py.None(),
            resolver: py.None(),
            context,
            evolved: None,
            validators: None,
            current: None,
            errors_source: None,
            errors_iter: None,
            last_error: None,
        }),
    }
}

#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn descend(
    validator: &Bound<'_, PyAny>,
    instance: &Bound<'_, PyAny>,
    schema: &Bound<'_, PyAny>,
    path: &Bound<'_, PyAny>,
    schema_path: &Bound<'_, PyAny>,
    resolver: &Bound<'_, PyAny>,
    context: Py<KernelContext>,
) -> ValidationErrors {
    ValidationErrors {
        state: Some(Traversal {
            mode: Mode::Descend,
            started: false,
            false_yielded: false,
            validator: validator.clone().unbind(),
            instance: instance.clone().unbind(),
            schema: schema.clone().unbind(),
            path: path.clone().unbind(),
            schema_path: schema_path.clone().unbind(),
            resolver: resolver.clone().unbind(),
            context,
            evolved: None,
            validators: None,
            current: None,
            errors_source: None,
            errors_iter: None,
            last_error: None,
        }),
    }
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<KernelContext>()?;
    module.add_class::<ValidationErrors>()?;
    module.add_function(wrap_pyfunction!(make_context, module)?)?;
    module.add_function(wrap_pyfunction!(iter_errors, module)?)?;
    module.add_function(wrap_pyfunction!(descend, module)?)?;
    module.add_function(wrap_pyfunction!(evolve, module)?)?;
    module.add_function(wrap_pyfunction!(post_init, module)?)?;
    Ok(())
}
