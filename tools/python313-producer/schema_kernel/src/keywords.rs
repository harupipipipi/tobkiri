//! Direct lazy port of jsonschema 4.26.0's Draft 2020-12 keyword generators.
//!
//! Source: python-jsonschema/jsonschema, tag v4.26.0, jsonschema/_keywords.py
//! (MIT; see ../COPYING.jsonschema). Python owns all values and exceptions.
//! No original keyword handler or traversal function is called by this module.

use pyo3::{
    class::gc::{PyTraverseError, PyVisit},
    exceptions::{
        PyOverflowError, PyRuntimeError, PyStopIteration, PyTypeError, PyUnboundLocalError,
        PyValueError,
    },
    prelude::*,
    pyclass::CompareOp,
    types::{PyBool, PyDict, PyIterator, PyList, PyString, PyTuple},
};

type Object = Py<PyAny>;
type Cursor = Py<PyIterator>;

struct Inputs {
    validator: Object,
    value: Object,
    instance: Object,
    schema: Object,
}

struct Context<'py> {
    validator: Bound<'py, PyAny>,
    value: Bound<'py, PyAny>,
    instance: Bound<'py, PyAny>,
    schema: Bound<'py, PyAny>,
}

impl Inputs {
    fn bind<'py>(&self, py: Python<'py>) -> Context<'py> {
        Context {
            validator: self.validator.bind(py).clone(),
            value: self.value.bind(py).clone(),
            instance: self.instance.bind(py).clone(),
            schema: self.schema.bind(py).clone(),
        }
    }

    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.validator)?;
        visit.call(&self.value)?;
        visit.call(&self.instance)?;
        visit.call(&self.schema)
    }
}

#[derive(Clone, Copy)]
enum ForKind {
    Properties,
    PropertyNames,
    AdditionalProperties,
    Required,
    DependentSchemas,
    AllOf,
    PrefixItems,
    Items,
}

enum State {
    Start,
    Done,
    For {
        cursor: Cursor,
        kind: ForKind,
    },
    Patterns {
        outer: Cursor,
        inner: Option<Cursor>,
        pattern: Option<Object>,
        subschema: Option<Object>,
    },
    Dependencies {
        outer: Cursor,
        inner: Option<Cursor>,
        property: Option<Object>,
    },
    OneOfRemaining {
        cursor: Cursor,
        first_valid: Option<Object>,
    },
}

impl State {
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        match self {
            Self::Start | Self::Done => {}
            Self::For { cursor, .. } => visit.call(cursor)?,
            Self::Patterns {
                outer,
                inner,
                pattern,
                subschema,
            } => {
                visit.call(outer)?;
                if let Some(value) = inner {
                    visit.call(value)?;
                }
                if let Some(value) = pattern {
                    visit.call(value)?;
                }
                if let Some(value) = subschema {
                    visit.call(value)?;
                }
            }
            Self::Dependencies {
                outer,
                inner,
                property,
            } => {
                visit.call(outer)?;
                if let Some(value) = inner {
                    visit.call(value)?;
                }
                if let Some(value) = property {
                    visit.call(value)?;
                }
            }
            Self::OneOfRemaining {
                cursor,
                first_valid,
            } => {
                visit.call(cursor)?;
                if let Some(value) = first_valid {
                    visit.call(value)?;
                }
            }
        }
        Ok(())
    }
}

#[pyclass(name = "_OriginalKeywordIterator", module = "_tobkiri_schema_kernel")]
struct OriginalKeywordIterator {
    keyword: String,
    inputs: Option<Inputs>,
    state: State,
    child: Option<Cursor>,
    // Python generator locals whose context lists remain live at a yield.
    retained: Vec<Object>,
}

#[pymethods]
impl OriginalKeywordIterator {
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    fn __next__(&mut self, py: Python<'_>) -> PyResult<Option<Object>> {
        if self.inputs.is_none() {
            return Ok(None);
        }
        let result = self.step(py).map_err(|error| {
            if error.is_instance_of::<PyStopIteration>(py) {
                // PEP 479: a callback's uncaught StopIteration is not a
                // successful return from the surrounding keyword generator.
                let wrapped = PyRuntimeError::new_err("generator raised StopIteration");
                wrapped.set_context(py, Some(error.clone_ref(py)));
                wrapped.set_cause(py, Some(error));
                wrapped
            } else {
                error
            }
        });
        // A Python generator closes its frame after return or an uncaught
        // exception. Keep references while suspended at an actual yield only.
        if !matches!(&result, Ok(Some(_))) {
            self.close_frame();
        }
        result
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Some(inputs) = &self.inputs {
            inputs.traverse(&visit)?;
        }
        self.state.traverse(&visit)?;
        if let Some(child) = &self.child {
            visit.call(child)?;
        }
        for value in &self.retained {
            visit.call(value)?;
        }
        Ok(())
    }

    fn __clear__(&mut self) {
        self.close_frame();
    }

    fn close(&mut self, py: Python<'_>) -> PyResult<()> {
        // yield-from closes an active delegated iterator even when another
        // Python reference still keeps that child alive.
        let child = self.child.take();
        let result = (|| {
            if let Some(child) = child {
                if let Some(close) = child.bind(py).getattr_opt("close")? {
                    close.call0()?;
                }
            }
            Ok(())
        })();
        self.close_frame();
        result
    }

    fn send(&mut self, py: Python<'_>, value: &Bound<'_, PyAny>) -> PyResult<Object> {
        if self.inputs.is_some() && matches!(self.state, State::Start) && !value.is_none() {
            return Err(PyTypeError::new_err(
                "can't send non-None value to a just-started generator",
            ));
        }
        match self.__next__(py)? {
            Some(value) => Ok(value),
            None => Err(PyStopIteration::new_err(())),
        }
    }
}

impl OriginalKeywordIterator {
    fn close_frame(&mut self) {
        self.child = None;
        self.state = State::Done;
        self.retained.clear();
        self.inputs = None;
    }

    fn step(&mut self, py: Python<'_>) -> PyResult<Option<Object>> {
        let context = self.inputs.as_ref().expect("live keyword frame").bind(py);
        loop {
            if let Some(child) = &self.child {
                if let Some(error) = next(py, child)? {
                    return Ok(Some(error.unbind()));
                }
                self.child = None;
            }
            let state = std::mem::replace(&mut self.state, State::Done);
            match state {
                State::Done => return Ok(None),
                State::Start => {
                    if let Some(error) = self.start(py, &context)? {
                        return Ok(Some(error.unbind()));
                    }
                }
                State::For { cursor, kind } => {
                    let Some(item) = next(py, &cursor)? else {
                        return Ok(None);
                    };
                    self.state = State::For { cursor, kind };
                    if let Some(error) = self.for_item(py, &context, kind, item)? {
                        return Ok(Some(error.unbind()));
                    }
                }
                State::Patterns {
                    outer,
                    mut inner,
                    mut pattern,
                    mut subschema,
                } => {
                    if let Some(cursor) = &inner {
                        if let Some(item) = next(py, cursor)? {
                            let (key, value) = pair(py, &item)?;
                            let selected = pattern.as_ref().expect("active pattern").bind(py);
                            let matched = module_member(py, "re", "search")?
                                .call1((selected, &key))?
                                .is_truthy()?;
                            if matched {
                                let child = descend(
                                    py,
                                    &context.validator,
                                    &value,
                                    subschema.as_ref().expect("active subschema").bind(py),
                                    Some(&key),
                                    Some(selected),
                                    false,
                                )?;
                                self.child = Some(child.try_iter()?.unbind());
                            }
                            self.state = State::Patterns {
                                outer,
                                inner,
                                pattern,
                                subschema,
                            };
                            continue;
                        }
                    }
                    let Some(item) = next(py, &outer)? else {
                        return Ok(None);
                    };
                    let (selected, schema) = pair(py, &item)?;
                    inner = Some(context.instance.call_method0("items")?.try_iter()?.unbind());
                    pattern = Some(selected.unbind());
                    subschema = Some(schema.unbind());
                    self.state = State::Patterns {
                        outer,
                        inner,
                        pattern,
                        subschema,
                    };
                }
                State::Dependencies {
                    outer,
                    mut inner,
                    mut property,
                } => {
                    if let Some(cursor) = &inner {
                        if let Some(each) = next(py, cursor)? {
                            let missing = !context.instance.contains(&each)?;
                            let error = if missing {
                                Some(validation_error(
                                    py,
                                    format(
                                        py,
                                        "{0!r} is a dependency of {1!r}",
                                        &[
                                            each,
                                            property
                                                .as_ref()
                                                .expect("active property")
                                                .bind(py)
                                                .clone(),
                                        ],
                                    )?,
                                    None,
                                )?)
                            } else {
                                None
                            };
                            self.state = State::Dependencies {
                                outer,
                                inner,
                                property,
                            };
                            if let Some(error) = error {
                                return Ok(Some(error.unbind()));
                            }
                            continue;
                        }
                    }
                    let Some(item) = next(py, &outer)? else {
                        return Ok(None);
                    };
                    let (selected, dependency) = pair(py, &item)?;
                    if context.instance.contains(&selected)? {
                        inner = Some(dependency.try_iter()?.unbind());
                        property = Some(selected.unbind());
                    } else {
                        inner = None;
                        property = None;
                    }
                    self.state = State::Dependencies {
                        outer,
                        inner,
                        property,
                    };
                }
                State::OneOfRemaining {
                    cursor,
                    first_valid,
                } => {
                    return one_of_remaining(py, &context, &cursor, first_valid.as_ref())
                        .map(|error| error.map(Bound::unbind));
                }
            }
        }
    }

    fn delegate(&mut self, child: Bound<'_, PyAny>) -> PyResult<()> {
        self.child = Some(child.try_iter()?.unbind());
        Ok(())
    }

    fn for_item<'py>(
        &mut self,
        py: Python<'py>,
        context: &Context<'py>,
        kind: ForKind,
        item: Bound<'py, PyAny>,
    ) -> PyResult<Option<Bound<'py, PyAny>>> {
        let validator = &context.validator;
        let instance = &context.instance;
        let value = &context.value;
        match kind {
            ForKind::Properties => {
                let (property, subschema) = pair(py, &item)?;
                if instance.contains(&property)? {
                    self.delegate(descend(
                        py,
                        validator,
                        &instance.get_item(&property)?,
                        &subschema,
                        Some(&property),
                        Some(&property),
                        false,
                    )?)?;
                }
            }
            ForKind::PropertyNames => {
                self.delegate(descend(py, validator, &item, value, None, None, true)?)?;
            }
            ForKind::AdditionalProperties => {
                self.delegate(descend(
                    py,
                    validator,
                    &instance.get_item(&item)?,
                    value,
                    Some(&item),
                    None,
                    false,
                )?)?;
            }
            ForKind::Required => {
                if !instance.contains(&item)? {
                    return Ok(Some(validation_error(
                        py,
                        format(py, "{0!r} is a required property", &[item])?,
                        None,
                    )?));
                }
            }
            ForKind::DependentSchemas => {
                let (property, dependency) = pair(py, &item)?;
                if instance.contains(&property)? {
                    self.delegate(descend(
                        py,
                        validator,
                        instance,
                        &dependency,
                        None,
                        Some(&property),
                        false,
                    )?)?;
                }
            }
            ForKind::AllOf => {
                let (index, subschema) = pair(py, &item)?;
                self.delegate(descend(
                    py,
                    validator,
                    instance,
                    &subschema,
                    None,
                    Some(&index),
                    false,
                )?)?;
            }
            ForKind::PrefixItems => {
                let (enumerated, subschema) = pair(py, &item)?;
                let (index, instance) = pair(py, &enumerated)?;
                self.delegate(descend(
                    py,
                    validator,
                    &instance,
                    &subschema,
                    Some(&index),
                    Some(&index),
                    true,
                )?)?;
            }
            ForKind::Items => {
                self.delegate(descend(
                    py,
                    validator,
                    &instance.get_item(&item)?,
                    value,
                    Some(&item),
                    None,
                    true,
                )?)?;
            }
        }
        Ok(None)
    }

    fn start<'py>(
        &mut self,
        py: Python<'py>,
        context: &Context<'py>,
    ) -> PyResult<Option<Bound<'py, PyAny>>> {
        let validator = &context.validator;
        let value = &context.value;
        let instance = &context.instance;
        let schema = &context.schema;
        match self.keyword.as_str() {
            "patternProperties" => {
                if is_type(validator, instance, "object")? {
                    self.state = State::Patterns {
                        outer: value.call_method0("items")?.try_iter()?.unbind(),
                        inner: None,
                        pattern: None,
                        subschema: None,
                    };
                }
            }
            "propertyNames" | "properties" | "required" | "dependentSchemas" => {
                if is_type(validator, instance, "object")? {
                    let (iterable, kind) = match self.keyword.as_str() {
                        "propertyNames" => (instance.clone(), ForKind::PropertyNames),
                        "properties" => (value.call_method0("items")?, ForKind::Properties),
                        "required" => (value.clone(), ForKind::Required),
                        _ => (value.call_method0("items")?, ForKind::DependentSchemas),
                    };
                    self.state = State::For {
                        cursor: iterable.try_iter()?.unbind(),
                        kind,
                    };
                }
            }
            "dependentRequired" => {
                if is_type(validator, instance, "object")? {
                    self.state = State::Dependencies {
                        outer: value.call_method0("items")?.try_iter()?.unbind(),
                        inner: None,
                        property: None,
                    };
                }
            }
            "additionalProperties" => {
                if !is_type(validator, instance, "object")? {
                    return Ok(None);
                }
                let additional =
                    utility(py, "find_additional_properties")?.call1((instance, schema))?;
                let extras = builtin(py, "set")?.call1((additional,))?;
                if is_type(validator, value, "object")? {
                    self.state = State::For {
                        cursor: extras.try_iter()?.unbind(),
                        kind: ForKind::AdditionalProperties,
                    };
                } else if !value.is_truthy()? && extras.is_truthy()? {
                    let message = if schema.contains("patternProperties")? {
                        let verb = if extras.len()? == 1 { "does" } else { "do" };
                        let joined = reprs_joined(py, &sorted(py, &extras, false)?)?;
                        let patterns = reprs_joined(
                            py,
                            &sorted(py, &schema.get_item("patternProperties")?, false)?,
                        )?;
                        format(
                            py,
                            "{0} {1} not match any of the regexes: {2}",
                            &[joined, PyString::new(py, verb).into_any(), patterns],
                        )?
                    } else {
                        extras_message(
                            py,
                            "Additional properties are not allowed (%s %s unexpected)",
                            &sorted(py, &extras, true)?,
                        )?
                    };
                    return Ok(Some(validation_error(py, message, None)?));
                }
            }
            "items" => {
                if !is_type(validator, instance, "array")? {
                    return Ok(None);
                }
                let prefix = schema
                    .call_method1("get", ("prefixItems", PyList::empty(py)))?
                    .len()?;
                let total = instance.len()?;
                if total <= prefix {
                    return Ok(None);
                }
                let extra = total - prefix;
                if is_false(py, value) {
                    let rest = if extra != 1 {
                        instance.get_item(builtin(py, "slice")?.call1((prefix, py.None()))?)?
                    } else {
                        instance.get_item(prefix)?
                    };
                    return Ok(Some(validation_error(
                        py,
                        format(
                            py,
                            "Expected at most {0} {1} but found {2} extra: {3!r}",
                            &[
                                integer(py, prefix),
                                PyString::new(py, if prefix != 1 { "items" } else { "item" })
                                    .into_any(),
                                integer(py, extra),
                                rest,
                            ],
                        )?,
                        None,
                    )?));
                }
                self.state = State::For {
                    cursor: builtin(py, "range")?
                        .call1((prefix, total))?
                        .try_iter()?
                        .unbind(),
                    kind: ForKind::Items,
                };
            }
            "prefixItems" => {
                if is_type(validator, instance, "array")? {
                    let enumerated = builtin(py, "enumerate")?.call1((instance,))?;
                    self.state = State::For {
                        cursor: builtin(py, "zip")?
                            .call1((enumerated, value))?
                            .try_iter()?
                            .unbind(),
                        kind: ForKind::PrefixItems,
                    };
                }
            }
            "allOf" => {
                self.state = State::For {
                    cursor: builtin(py, "enumerate")?
                        .call1((value,))?
                        .try_iter()?
                        .unbind(),
                    kind: ForKind::AllOf,
                };
            }
            "$ref" | "$dynamicRef" => {
                let kwargs = PyDict::new(py);
                kwargs.set_item("ref", value)?;
                kwargs.set_item("instance", instance)?;
                self.delegate(validator.call_method("_validate_reference", (), Some(&kwargs))?)?;
            }
            "if" => {
                let selected = if evolved_is_valid(py, validator, value, instance)? {
                    "then"
                } else {
                    "else"
                };
                if schema.contains(selected)? {
                    self.delegate(descend(
                        py,
                        validator,
                        instance,
                        &schema.get_item(selected)?,
                        None,
                        Some(&PyString::new(py, selected).into_any()),
                        false,
                    )?)?;
                }
            }
            "anyOf" | "oneOf" => {
                let cursor = builtin(py, "enumerate")?
                    .call1((value,))?
                    .try_iter()?
                    .unbind();
                let errors = PyList::empty(py);
                while let Some(item) = next(py, &cursor)? {
                    let (index, subschema) = pair(py, &item)?;
                    let branch = descend(
                        py,
                        validator,
                        instance,
                        &subschema,
                        None,
                        Some(&index),
                        false,
                    )?;
                    // list(...) is deliberate in the original handler.
                    let found = builtin(py, "list")?.call1((branch,))?;
                    if !found.is_truthy()? {
                        if self.keyword == "oneOf" {
                            self.retained.push(errors.clone().into_any().unbind());
                            self.retained.push(found.unbind());
                            self.state = State::OneOfRemaining {
                                cursor,
                                first_valid: Some(subschema.unbind()),
                            };
                        }
                        return Ok(None);
                    }
                    errors.call_method1("extend", (found,))?;
                }
                self.retained.push(errors.clone().into_any().unbind());
                if self.keyword == "oneOf" {
                    // oneOf resumes its remaining-schemas comprehension after
                    // the no-valid-branch yield, just as the original does.
                    self.state = State::OneOfRemaining {
                        cursor,
                        first_valid: None,
                    };
                }
                let kwargs = PyDict::new(py);
                kwargs.set_item("context", &errors)?;
                return Ok(Some(validation_error(
                    py,
                    format(
                        py,
                        "{0!r} is not valid under any of the given schemas",
                        &[instance.clone()],
                    )?,
                    Some(&kwargs),
                )?));
            }
            "contains" => return contains(py, context),
            "unevaluatedItems" => return unevaluated_items(py, context),
            "unevaluatedProperties" => return unevaluated_properties(py, context),
            _ => return scalar(py, &self.keyword, context),
        }
        Ok(None)
    }
}

fn next<'py>(py: Python<'py>, cursor: &Cursor) -> PyResult<Option<Bound<'py, PyAny>>> {
    cursor.bind(py).clone().next().transpose()
}

fn pair<'py>(
    py: Python<'py>,
    value: &Bound<'py, PyAny>,
) -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyAny>)> {
    let cursor = value.try_iter()?.unbind();
    let first = next(py, &cursor)?
        .ok_or_else(|| PyValueError::new_err("not enough values to unpack (expected 2, got 0)"))?;
    let second = next(py, &cursor)?
        .ok_or_else(|| PyValueError::new_err("not enough values to unpack (expected 2, got 1)"))?;
    if next(py, &cursor)?.is_some() {
        return Err(PyValueError::new_err(
            "too many values to unpack (expected 2)",
        ));
    }
    Ok((first, second))
}

fn module_member<'py>(py: Python<'py>, module: &str, name: &str) -> PyResult<Bound<'py, PyAny>> {
    py.import(module)?.getattr(name)
}

fn builtin<'py>(py: Python<'py>, name: &str) -> PyResult<Bound<'py, PyAny>> {
    module_member(py, "builtins", name)
}

fn utility<'py>(py: Python<'py>, name: &str) -> PyResult<Bound<'py, PyAny>> {
    module_member(py, "jsonschema._utils", name)
}

fn integer(py: Python<'_>, value: usize) -> Bound<'_, PyAny> {
    value
        .into_pyobject(py)
        .expect("infallible usize conversion")
        .into_any()
}

fn is_false(py: Python<'_>, value: &Bound<'_, PyAny>) -> bool {
    value.as_ptr() == PyBool::new(py, false).as_ptr()
}

fn is_type(
    validator: &Bound<'_, PyAny>,
    instance: &Bound<'_, PyAny>,
    kind: &str,
) -> PyResult<bool> {
    validator
        .call_method1("is_type", (instance, kind))?
        .is_truthy()
}

fn format<'py>(
    py: Python<'py>,
    template: &str,
    values: &[Bound<'py, PyAny>],
) -> PyResult<Bound<'py, PyAny>> {
    let arguments = PyTuple::new(py, values.iter())?;
    PyString::new(py, template)
        .getattr("format")?
        .call(&arguments, None)
}

fn validation_error<'py>(
    py: Python<'py>,
    message: Bound<'py, PyAny>,
    kwargs: Option<&Bound<'py, PyDict>>,
) -> PyResult<Bound<'py, PyAny>> {
    module_member(py, "jsonschema.exceptions", "ValidationError")?.call((message,), kwargs)
}

fn descend<'py>(
    py: Python<'py>,
    validator: &Bound<'py, PyAny>,
    instance: &Bound<'py, PyAny>,
    schema: &Bound<'py, PyAny>,
    path: Option<&Bound<'py, PyAny>>,
    schema_path: Option<&Bound<'py, PyAny>>,
    keyword_arguments: bool,
) -> PyResult<Bound<'py, PyAny>> {
    let kwargs = PyDict::new(py);
    if keyword_arguments {
        kwargs.set_item("instance", instance)?;
        kwargs.set_item("schema", schema)?;
        if let Some(path) = schema_path {
            kwargs.set_item("schema_path", path)?;
        }
        if let Some(path) = path {
            kwargs.set_item("path", path)?;
        }
        validator.call_method("descend", (), Some(&kwargs))
    } else {
        if let Some(path) = path {
            kwargs.set_item("path", path)?;
        }
        if let Some(path) = schema_path {
            kwargs.set_item("schema_path", path)?;
        }
        validator.call_method("descend", (instance, schema), Some(&kwargs))
    }
}

fn evolve<'py>(
    py: Python<'py>,
    validator: &Bound<'py, PyAny>,
    schema: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let kwargs = PyDict::new(py);
    kwargs.set_item("schema", schema)?;
    validator.call_method("evolve", (), Some(&kwargs))
}

fn evolved_is_valid(
    py: Python<'_>,
    validator: &Bound<'_, PyAny>,
    schema: &Bound<'_, PyAny>,
    instance: &Bound<'_, PyAny>,
) -> PyResult<bool> {
    evolve(py, validator, schema)?
        .call_method1("is_valid", (instance,))?
        .is_truthy()
}

fn sorted<'py>(
    py: Python<'py>,
    values: &Bound<'py, PyAny>,
    key_str: bool,
) -> PyResult<Bound<'py, PyAny>> {
    let kwargs = PyDict::new(py);
    if key_str {
        kwargs.set_item("key", builtin(py, "str")?)?;
    }
    builtin(py, "sorted")?.call((values,), Some(&kwargs))
}

fn reprs_joined<'py>(py: Python<'py>, values: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let reprs = PyList::empty(py);
    for value in values.try_iter()? {
        reprs.append(value?.repr()?)?;
    }
    PyString::new(py, ", ").call_method1("join", (reprs,))
}

fn extras_message<'py>(
    py: Python<'py>,
    template: &str,
    values: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let extras = utility(py, "extras_msg")?.call1((values,))?;
    PyString::new(py, template).rem(extras)
}

fn contains<'py>(py: Python<'py>, context: &Context<'py>) -> PyResult<Option<Bound<'py, PyAny>>> {
    if !is_type(&context.validator, &context.instance, "array")? {
        return Ok(None);
    }
    let mut matches = integer(py, 0);
    let minimum = context.schema.call_method1("get", ("minContains", 1))?;
    let maximum = context
        .schema
        .call_method1("get", ("maxContains", context.instance.len()?))?;
    let validator = evolve(py, &context.validator, &context.value)?;
    for each in context.instance.try_iter()? {
        if validator.call_method1("is_valid", (each?,))?.is_truthy()? {
            matches = matches.add(1)?;
            if matches.gt(&maximum)? {
                let kwargs = PyDict::new(py);
                kwargs.set_item("validator", "maxContains")?;
                kwargs.set_item("validator_value", &maximum)?;
                return Ok(Some(validation_error(
                    py,
                    format(
                        py,
                        "Too many items match the given schema (expected at most {0})",
                        &[maximum],
                    )?,
                    Some(&kwargs),
                )?));
            }
        }
    }
    if matches.lt(&minimum)? {
        if !matches.is_truthy()? {
            return Ok(Some(validation_error(
                py,
                format(
                    py,
                    "{0!r} does not contain items matching the given schema",
                    &[context.instance.clone()],
                )?,
                None,
            )?));
        }
        let kwargs = PyDict::new(py);
        kwargs.set_item("validator", "minContains")?;
        kwargs.set_item("validator_value", &minimum)?;
        return Ok(Some(validation_error(
            py,
            format(
                py,
                "Too few items match the given schema (expected at least {0} but only {1} matched)",
                &[minimum, matches],
            )?,
            Some(&kwargs),
        )?));
    }
    Ok(None)
}

fn one_of_remaining<'py>(
    py: Python<'py>,
    context: &Context<'py>,
    cursor: &Cursor,
    first_valid: Option<&Object>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let more_valid = PyList::empty(py);
    while let Some(item) = next(py, cursor)? {
        let (_, each) = pair(py, &item)?;
        if evolved_is_valid(py, &context.validator, &each, &context.instance)? {
            more_valid.append(each)?;
        }
    }
    if !more_valid.is_empty() {
        let first = first_valid.ok_or_else(|| PyUnboundLocalError::new_err("cannot access local variable 'first_valid' where it is not associated with a value"))?;
        more_valid.append(first.bind(py))?;
        let reprs = reprs_joined(py, more_valid.as_any())?;
        return Ok(Some(validation_error(
            py,
            format(
                py,
                "{0!r} is valid under each of {1}",
                &[context.instance.clone(), reprs],
            )?,
            None,
        )?));
    }
    Ok(None)
}

fn unevaluated_items<'py>(
    py: Python<'py>,
    context: &Context<'py>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    if !is_type(&context.validator, &context.instance, "array")? {
        return Ok(None);
    }
    let evaluated = utility(py, "find_evaluated_item_indexes_by_schema")?.call1((
        &context.validator,
        &context.instance,
        &context.schema,
    ))?;
    let values = PyList::empty(py);
    let enumerated = builtin(py, "enumerate")?.call1((&context.instance,))?;
    for item in enumerated.try_iter()? {
        let (index, value) = pair(py, &item?)?;
        if !evaluated.contains(index)? {
            values.append(value)?;
        }
    }
    if !values.is_empty() {
        return Ok(Some(validation_error(
            py,
            extras_message(
                py,
                "Unevaluated items are not allowed (%s %s unexpected)",
                values.as_any(),
            )?,
            None,
        )?));
    }
    Ok(None)
}

fn unevaluated_properties<'py>(
    py: Python<'py>,
    context: &Context<'py>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    if !is_type(&context.validator, &context.instance, "object")? {
        return Ok(None);
    }
    let evaluated = utility(py, "find_evaluated_property_keys_by_schema")?.call1((
        &context.validator,
        &context.instance,
        &context.schema,
    ))?;
    let keys = PyList::empty(py);
    for property in context.instance.try_iter()? {
        let property = property?;
        if !evaluated.contains(&property)? {
            let errors = descend(
                py,
                &context.validator,
                &context.instance.get_item(&property)?,
                &context.value,
                Some(&property),
                Some(&property),
                false,
            )?;
            // Original code consumes EVERY error, appending the key once per
            // error (including duplicates), and does not attach that context.
            for error in errors.try_iter()? {
                let _ = error?;
                keys.append(&property)?;
            }
        }
    }
    if !keys.is_empty() {
        let (template, extras) = if is_false(py, &context.value) {
            (
                "Unevaluated properties are not allowed (%s %s unexpected)",
                sorted(py, keys.as_any(), true)?,
            )
        } else {
            ("Unevaluated properties are not valid under the given schema (%s %s unevaluated and invalid)", keys.into_any())
        };
        return Ok(Some(validation_error(
            py,
            extras_message(py, template, &extras)?,
            None,
        )?));
    }
    Ok(None)
}

fn scalar<'py>(
    py: Python<'py>,
    keyword: &str,
    context: &Context<'py>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let validator = &context.validator;
    let value = &context.value;
    let instance = &context.instance;
    let message = match keyword {
        "const" => {
            if utility(py, "equal")?
                .call1((instance, value))?
                .is_truthy()?
            {
                return Ok(None);
            }
            format(py, "{0!r} was expected", &[value.clone()])?
        }
        "enum" => {
            for each in value.try_iter()? {
                if utility(py, "equal")?
                    .call1((each?, instance))?
                    .is_truthy()?
                {
                    return Ok(None);
                }
            }
            format(
                py,
                "{0!r} is not one of {1!r}",
                &[instance.clone(), value.clone()],
            )?
        }
        "type" => {
            let types = utility(py, "ensure_list")?.call1((value,))?;
            for kind in types.try_iter()? {
                if validator
                    .call_method1("is_type", (instance, kind?))?
                    .is_truthy()?
                {
                    return Ok(None);
                }
            }
            let reprs = reprs_joined(py, &types)?;
            format(py, "{0!r} is not of type {1}", &[instance.clone(), reprs])?
        }
        "minimum" | "maximum" | "exclusiveMinimum" | "exclusiveMaximum" => {
            if !is_type(validator, instance, "number")? {
                return Ok(None);
            }
            let (failed, template) = match keyword {
                "minimum" => (
                    instance.lt(value)?,
                    "{0!r} is less than the minimum of {1!r}",
                ),
                "maximum" => (
                    instance.gt(value)?,
                    "{0!r} is greater than the maximum of {1!r}",
                ),
                "exclusiveMinimum" => (
                    instance.le(value)?,
                    "{0!r} is less than or equal to the minimum of {1!r}",
                ),
                _ => (
                    instance.ge(value)?,
                    "{0!r} is greater than or equal to the maximum of {1!r}",
                ),
            };
            if !failed {
                return Ok(None);
            }
            format(py, template, &[instance.clone(), value.clone()])?
        }
        "multipleOf" => {
            if !is_type(validator, instance, "number")? {
                return Ok(None);
            }
            let is_float = builtin(py, "isinstance")?
                .call1((value, builtin(py, "float")?))?
                .is_truthy()?;
            let failed = if is_float {
                let quotient = instance.div(value)?;
                match builtin(py, "int")?
                    .call1((&quotient,))
                    .and_then(|integer| integer.rich_compare(&quotient, CompareOp::Ne))
                {
                    Ok(failed) => failed,
                    Err(error) if error.is_instance_of::<PyOverflowError>(py) => {
                        let fraction = module_member(py, "fractions", "Fraction")?;
                        fraction
                            .call1((instance,))?
                            .div(fraction.call1((value,))?)?
                            .getattr("denominator")?
                            .rich_compare(1, CompareOp::Ne)?
                    }
                    Err(error) => return Err(error),
                }
            } else {
                instance.rem(value)?
            };
            if !failed.is_truthy()? {
                return Ok(None);
            }
            format(
                py,
                "{0!r} is not a multiple of {1}",
                &[instance.clone(), value.clone()],
            )?
        }
        "minItems" | "maxItems" | "minLength" | "maxLength" | "minProperties" | "maxProperties" => {
            let kind = match keyword {
                "minItems" | "maxItems" => "array",
                "minLength" | "maxLength" => "string",
                _ => "object",
            };
            if !is_type(validator, instance, kind)? {
                return Ok(None);
            }
            // maxProperties repeats this guard in the original source.
            if keyword == "maxProperties" && !is_type(validator, instance, "object")? {
                return Ok(None);
            }
            let length = integer(py, instance.len()?);
            let minimum = keyword.starts_with("min");
            let failed = if minimum {
                length.lt(value)?
            } else {
                length.gt(value)?
            };
            if !failed {
                return Ok(None);
            }
            let adjective = if minimum {
                if value.eq(1)? {
                    "should be non-empty"
                } else if keyword == "minProperties" {
                    "does not have enough properties"
                } else {
                    "is too short"
                }
            } else if value.eq(0)? {
                "is expected to be empty"
            } else if keyword == "maxProperties" {
                "has too many properties"
            } else {
                "is too long"
            };
            format(
                py,
                "{0!r} {1}",
                &[instance.clone(), PyString::new(py, adjective).into_any()],
            )?
        }
        "uniqueItems" => {
            if !value.is_truthy()?
                || !is_type(validator, instance, "array")?
                || utility(py, "uniq")?.call1((instance,))?.is_truthy()?
            {
                return Ok(None);
            }
            format(py, "{0!r} has non-unique elements", &[instance.clone()])?
        }
        "pattern" => {
            if !is_type(validator, instance, "string")?
                || module_member(py, "re", "search")?
                    .call1((value, instance))?
                    .is_truthy()?
            {
                return Ok(None);
            }
            format(
                py,
                "{0!r} does not match {1!r}",
                &[instance.clone(), value.clone()],
            )?
        }
        "format" => {
            if validator.getattr("format_checker")?.is_none() {
                return Ok(None);
            }
            match validator
                .getattr("format_checker")?
                .call_method1("check", (instance, value))
            {
                Ok(_) => return Ok(None),
                Err(error) => {
                    let exception = error.value(py);
                    if !exception.is_instance(&module_member(
                        py,
                        "jsonschema.exceptions",
                        "FormatError",
                    )?)? {
                        return Err(error);
                    }
                    let kwargs = PyDict::new(py);
                    kwargs.set_item("cause", exception.getattr("cause")?)?;
                    return Ok(Some(validation_error(
                        py,
                        exception.getattr("message")?,
                        Some(&kwargs),
                    )?));
                }
            }
        }
        "not" => {
            if !evolved_is_valid(py, validator, value, instance)? {
                return Ok(None);
            }
            format(
                py,
                "{0!r} should not be valid under {1!r}",
                &[instance.clone(), value.clone()],
            )?
        }
        _ => {
            return Err(PyValueError::new_err(format!(
                "unknown original keyword: {keyword}"
            )))
        }
    };
    Ok(Some(validation_error(py, message, None)?))
}

/// Construct a suspended iterator by retaining the four actual Python inputs.
/// No Python lookup, type check, iteration or validator call occurs here.
pub(crate) fn iterator<'py>(
    py: Python<'py>,
    keyword: &str,
    validator: &Bound<'py, PyAny>,
    value: &Bound<'py, PyAny>,
    instance: &Bound<'py, PyAny>,
    schema: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    Ok(Py::new(
        py,
        OriginalKeywordIterator {
            keyword: keyword.to_owned(),
            inputs: Some(Inputs {
                validator: validator.clone().unbind(),
                value: value.clone().unbind(),
                instance: instance.clone().unbind(),
                schema: schema.clone().unbind(),
            }),
            state: State::Start,
            child: None,
            retained: Vec::new(),
        },
    )?
    .into_bound(py)
    .into_any())
}

/// Register the Rust iterator type without loading the Python support modules.
pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<OriginalKeywordIterator>()
}
