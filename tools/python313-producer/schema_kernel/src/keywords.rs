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
    ffi,
    prelude::*,
    pyclass::CompareOp,
    types::{PyBool, PyDict, PyIterator, PyList, PyString, PyTuple},
};

use std::sync::atomic::{AtomicUsize, Ordering};

static KEYWORD_ENTRIES: AtomicUsize = AtomicUsize::new(0);

type Object = Py<PyAny>;
type Cursor = Py<PyIterator>;

struct Inputs {
    validator: Object,
    value: Object,
    instance: Object,
    schema: Object,
    descend_names: Py<KeywordNames>,
}

struct Context<'py> {
    validator: Bound<'py, PyAny>,
    value: Bound<'py, PyAny>,
    instance: Bound<'py, PyAny>,
    schema: Bound<'py, PyAny>,
    descend_names: Bound<'py, KeywordNames>,
}

impl Inputs {
    fn bind<'py>(&self, py: Python<'py>) -> Context<'py> {
        Context {
            validator: self.validator.bind(py).clone(),
            value: self.value.bind(py).clone(),
            instance: self.instance.bind(py).clone(),
            schema: self.schema.bind(py).clone(),
            descend_names: self.descend_names.bind(py).clone(),
        }
    }

    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.validator)?;
        visit.call(&self.value)?;
        visit.call(&self.instance)?;
        visit.call(&self.schema)?;
        visit.call(&self.descend_names)
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
                                    &context,
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
                    inner = Some(
                        context
                            .instance
                            .call_method0(pyo3::intern!(py, "items"))?
                            .try_iter()?
                            .unbind(),
                    );
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
                        context,
                        &instance.get_item(&property)?,
                        &subschema,
                        Some(&property),
                        Some(&property),
                        false,
                    )?)?;
                }
            }
            ForKind::PropertyNames => {
                self.delegate(descend(py, context, &item, value, None, None, true)?)?;
            }
            ForKind::AdditionalProperties => {
                self.delegate(descend(
                    py,
                    context,
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
                        context,
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
                    context,
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
                    context,
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
                    context,
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
                        outer: value
                            .call_method0(pyo3::intern!(py, "items"))?
                            .try_iter()?
                            .unbind(),
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
                        "properties" => (
                            value.call_method0(pyo3::intern!(py, "items"))?,
                            ForKind::Properties,
                        ),
                        "required" => (value.clone(), ForKind::Required),
                        _ => (
                            value.call_method0(pyo3::intern!(py, "items"))?,
                            ForKind::DependentSchemas,
                        ),
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
                        outer: value
                            .call_method0(pyo3::intern!(py, "items"))?
                            .try_iter()?
                            .unbind(),
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
                    .call_method1(pyo3::intern!(py, "get"), ("prefixItems", PyList::empty(py)))?
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
                let method = validator.getattr(pyo3::intern!(py, "_validate_reference"))?;
                self.delegate(call_keywords(py, context, &method, &[value, instance], 8)?)?;
            }
            "if" => {
                let selected = if evolved_is_valid(py, context, value, instance)? {
                    "then"
                } else {
                    "else"
                };
                if schema.contains(selected)? {
                    self.delegate(descend(
                        py,
                        context,
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
                    let branch =
                        descend(py, context, instance, &subschema, None, Some(&index), false)?;
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
                    errors.call_method1(pyo3::intern!(py, "extend"), (found,))?;
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
                kwargs.set_item(pyo3::intern!(py, "context"), &errors)?;
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

pub(crate) fn module_member<'py>(
    py: Python<'py>,
    module: &str,
    name: &str,
) -> PyResult<Bound<'py, PyAny>> {
    py.import(module)?.getattr(name)
}

pub(crate) fn builtin<'py>(py: Python<'py>, name: &str) -> PyResult<Bound<'py, PyAny>> {
    module_member(py, "builtins", name)
}

pub(crate) fn utility<'py>(py: Python<'py>, name: &str) -> PyResult<Bound<'py, PyAny>> {
    module_member(py, "jsonschema._utils", name)
}

pub(crate) fn integer(py: Python<'_>, value: usize) -> Bound<'_, PyAny> {
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
    let py = validator.py();
    let literal = type_literal(py, kind);
    validator
        .call_method1(pyo3::intern!(py, "is_type"), (instance, literal))?
        .is_truthy()
}

fn type_literal<'py>(py: Python<'py>, kind: &str) -> Bound<'py, PyString> {
    // Fixed builtin handler literals retain their original interned identity.
    match kind {
        "object" => pyo3::intern!(py, "object").clone(),
        "array" => pyo3::intern!(py, "array").clone(),
        "number" => pyo3::intern!(py, "number").clone(),
        "string" => pyo3::intern!(py, "string").clone(),
        _ => PyString::new(py, kind),
    }
}

pub(crate) fn format<'py>(
    py: Python<'py>,
    template: &str,
    values: &[Bound<'py, PyAny>],
) -> PyResult<Bound<'py, PyAny>> {
    let arguments = PyTuple::new(py, values.iter())?;
    PyString::new(py, template)
        .getattr(pyo3::intern!(py, "format"))?
        .call(&arguments, None)
}

pub(crate) fn validation_error<'py>(
    py: Python<'py>,
    message: Bound<'py, PyAny>,
    kwargs: Option<&Bound<'py, PyDict>>,
) -> PyResult<Bound<'py, PyAny>> {
    module_member(py, "jsonschema.exceptions", "ValidationError")?.call((message,), kwargs)
}

fn descend<'py>(
    py: Python<'py>,
    context: &Context<'py>,
    instance: &Bound<'py, PyAny>,
    schema: &Bound<'py, PyAny>,
    path: Option<&Bound<'py, PyAny>>,
    schema_path: Option<&Bound<'py, PyAny>>,
    keyword_arguments: bool,
) -> PyResult<Bound<'py, PyAny>> {
    let index = usize::from(keyword_arguments) * 4
        + usize::from(path.is_some()) * 2
        + usize::from(schema_path.is_some());
    let names = context
        .descend_names
        .borrow()
        .rows
        .get(index)
        .ok_or_else(|| PyRuntimeError::new_err("keyword name metadata was cleared"))?
        .clone_ref(py);
    let names = names.bind(py);
    let mut arguments = [std::ptr::null_mut(); 4];
    arguments[0] = instance.as_ptr();
    arguments[1] = schema.as_ptr();
    let mut length = 2;
    // Keep the original kwargs insertion order, including schema_path before
    // path for the all-keyword callers. Omitted arguments remain omitted.
    let optional = if keyword_arguments {
        [schema_path, path]
    } else {
        [path, schema_path]
    };
    for value in optional.into_iter().flatten() {
        arguments[length] = value.as_ptr();
        length += 1;
    }
    let method = context.validator.getattr(pyo3::intern!(py, "descend"))?;
    // PyObject_Vectorcall is Stable ABI since 3.12. Unlike PyVectorcall_Call it
    // supports arbitrary callables and performs their ordinary tp_call dispatch.
    // https://docs.python.org/3.13/c-api/call.html#c.PyObject_Vectorcall
    // SAFETY: the synchronous call borrows all input Bound objects and method;
    // their owners live through this call, including callbacks/GC. The stack
    // holds exactly nargs + len(kwnames) live pointers (at most four). kwnames
    // comes from the first eight private rows of unique exact Unicode names.
    // No offset flag is used; the callee cannot write before this array. NULL
    // is propagated once as the original Python exception, with no retry.
    unsafe {
        Bound::from_owned_ptr_or_err(
            py,
            ffi::PyObject_Vectorcall(
                method.as_ptr(),
                arguments.as_ptr(),
                if keyword_arguments { 0 } else { 2 },
                names.as_ptr(),
            ),
        )
    }
}

fn call_keywords<'py>(
    py: Python<'py>,
    context: &Context<'py>,
    method: &Bound<'py, PyAny>,
    values: &[&Bound<'py, PyAny>],
    row: usize,
) -> PyResult<Bound<'py, PyAny>> {
    let names = context
        .descend_names
        .borrow()
        .rows
        .get(row)
        .ok_or_else(|| PyRuntimeError::new_err("keyword name metadata was cleared"))?
        .clone_ref(py);
    let names = names.bind(py);
    if values.len() > 2 || names.len() != values.len() {
        return Err(PyRuntimeError::new_err(
            "internal keyword call shape differs",
        ));
    }
    let mut arguments = [std::ptr::null_mut(); 2];
    for (slot, value) in arguments.iter_mut().zip(values) {
        *slot = value.as_ptr();
    }
    // SAFETY: all keyword values, the actual selected callable and the private
    // exact-Unicode names tuple remain owned throughout this synchronous call.
    // The fixed array has len(kwnames) live pointers, no positional arguments
    // and no offset flag. Ordinary arbitrary-callable dispatch is retained;
    // NULL propagates the original exception once, without fallback/retry.
    unsafe {
        Bound::from_owned_ptr_or_err(
            py,
            ffi::PyObject_Vectorcall(method.as_ptr(), arguments.as_ptr(), 0, names.as_ptr()),
        )
    }
}

fn evolve<'py>(
    py: Python<'py>,
    context: &Context<'py>,
    schema: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let method = context.validator.getattr(pyo3::intern!(py, "evolve"))?;
    call_keywords(py, context, &method, &[schema], 9)
}

fn evolved_is_valid(
    py: Python<'_>,
    context: &Context<'_>,
    schema: &Bound<'_, PyAny>,
    instance: &Bound<'_, PyAny>,
) -> PyResult<bool> {
    evolve(py, context, schema)?
        .call_method1(pyo3::intern!(py, "is_valid"), (instance,))?
        .is_truthy()
}

pub(crate) fn sorted<'py>(
    py: Python<'py>,
    values: &Bound<'py, PyAny>,
    key_str: bool,
) -> PyResult<Bound<'py, PyAny>> {
    let kwargs = PyDict::new(py);
    if key_str {
        kwargs.set_item(pyo3::intern!(py, "key"), builtin(py, "str")?)?;
    }
    builtin(py, "sorted")?.call((values,), Some(&kwargs))
}

pub(crate) fn reprs_joined<'py>(
    py: Python<'py>,
    values: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let reprs = PyList::empty(py);
    for value in values.try_iter()? {
        reprs.append(value?.repr()?)?;
    }
    PyString::new(py, ", ").call_method1(pyo3::intern!(py, "join"), (reprs,))
}

pub(crate) fn extras_message<'py>(
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
    let minimum = context
        .schema
        .call_method1(pyo3::intern!(py, "get"), ("minContains", 1))?;
    let maximum = context.schema.call_method1(
        pyo3::intern!(py, "get"),
        ("maxContains", context.instance.len()?),
    )?;
    let validator = evolve(py, context, &context.value)?;
    for each in context.instance.try_iter()? {
        if validator
            .call_method1(pyo3::intern!(py, "is_valid"), (each?,))?
            .is_truthy()?
        {
            matches = matches.add(1)?;
            if matches.gt(&maximum)? {
                let kwargs = PyDict::new(py);
                kwargs.set_item(pyo3::intern!(py, "validator"), "maxContains")?;
                kwargs.set_item(pyo3::intern!(py, "validator_value"), &maximum)?;
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
        kwargs.set_item(pyo3::intern!(py, "validator"), "minContains")?;
        kwargs.set_item(pyo3::intern!(py, "validator_value"), &minimum)?;
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
        if evolved_is_valid(py, context, &each, &context.instance)? {
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
                context,
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
    scalar_rule(
        py,
        keyword,
        &context.value,
        &context.instance,
        |instance, kind| {
            context
                .validator
                .call_method1(pyo3::intern!(py, "is_type"), (instance, kind))?
                .is_truthy()
        },
        || {
            context
                .validator
                .getattr(pyo3::intern!(py, "format_checker"))
        },
        |schema, instance| evolved_is_valid(py, context, schema, instance),
    )
}

/// Shared validation rules; each caller supplies its OWN traversal/type policy.
/// The generic library caller still executes every original live method lookup.
/// The App context uses Rust frames instead of Python Validator children.
pub(crate) fn scalar_rule<'py>(
    py: Python<'py>,
    keyword: &str,
    value: &Bound<'py, PyAny>,
    instance: &Bound<'py, PyAny>,
    mut test_type: impl FnMut(&Bound<'py, PyAny>, &Bound<'py, PyAny>) -> PyResult<bool>,
    mut format_checker: impl FnMut() -> PyResult<Bound<'py, PyAny>>,
    mut validates: impl FnMut(&Bound<'py, PyAny>, &Bound<'py, PyAny>) -> PyResult<bool>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
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
                if test_type(instance, &kind?)? {
                    return Ok(None);
                }
            }
            let reprs = reprs_joined(py, &types)?;
            format(py, "{0!r} is not of type {1}", &[instance.clone(), reprs])?
        }
        "minimum" | "maximum" | "exclusiveMinimum" | "exclusiveMaximum" => {
            if !test_type(instance, type_literal(py, "number").as_any())? {
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
            if !test_type(instance, type_literal(py, "number").as_any())? {
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
                            .getattr(pyo3::intern!(py, "denominator"))?
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
            if !test_type(instance, type_literal(py, kind).as_any())? {
                return Ok(None);
            }
            // maxProperties repeats this guard in the original source.
            if keyword == "maxProperties"
                && !test_type(instance, type_literal(py, "object").as_any())?
            {
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
                || !test_type(instance, type_literal(py, "array").as_any())?
                || utility(py, "uniq")?.call1((instance,))?.is_truthy()?
            {
                return Ok(None);
            }
            format(py, "{0!r} has non-unique elements", &[instance.clone()])?
        }
        "pattern" => {
            if !test_type(instance, type_literal(py, "string").as_any())?
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
            if format_checker()?.is_none() {
                return Ok(None);
            }
            match format_checker()?.call_method1(pyo3::intern!(py, "check"), (instance, value)) {
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
                    kwargs.set_item(
                        pyo3::intern!(py, "cause"),
                        exception.getattr(pyo3::intern!(py, "cause"))?,
                    )?;
                    return Ok(Some(validation_error(
                        py,
                        exception.getattr(pyo3::intern!(py, "message"))?,
                        Some(&kwargs),
                    )?));
                }
            }
        }
        "not" => {
            if !validates(value, instance)? {
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

/// Retain the four actual inputs and finite keyword-name metadata.
/// No Python lookup, type check, iteration or validator call occurs here.
fn iterator<'py>(
    py: Python<'py>,
    keyword: &str,
    validator: &Bound<'py, PyAny>,
    value: &Bound<'py, PyAny>,
    instance: &Bound<'py, PyAny>,
    schema: &Bound<'py, PyAny>,
    descend_names: &Bound<'py, KeywordNames>,
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
                descend_names: descend_names.clone().unbind(),
            }),
            state: State::Start,
            child: None,
            retained: Vec::new(),
        },
    )?
    .into_bound(py)
    .into_any())
}

/// Opaque metadata: Python callers cannot replace rows or supply forged tuples.
#[pyclass(module = "_tobkiri_schema_kernel")]
struct KeywordNames {
    rows: Vec<Py<PyTuple>>,
}

#[pymethods]
impl KeywordNames {
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        for row in &self.rows {
            visit.call(row)?;
        }
        Ok(())
    }

    fn __clear__(&mut self) {
        self.rows.clear();
    }
}

/// Finite original keyword names, created once when the translated module loads.
#[pyfunction]
fn keyword_names(py: Python<'_>) -> PyResult<KeywordNames> {
    // Fixed keyword spellings/order from the original keyword calls. No caller
    // values, callables or results are retained in this per-Store metadata.
    let names: [&[&str]; 10] = [
        &[],
        &["schema_path"],
        &["path"],
        &["path", "schema_path"],
        &["instance", "schema"],
        &["instance", "schema", "schema_path"],
        &["instance", "schema", "path"],
        &["instance", "schema", "schema_path", "path"],
        &["ref", "instance"],
        &["schema"],
    ];
    let mut keywords = Vec::with_capacity(names.len());
    for names in names {
        let values = names.iter().map(|name| match *name {
            "ref" => pyo3::intern!(py, "ref"),
            "instance" => pyo3::intern!(py, "instance"),
            "schema" => pyo3::intern!(py, "schema"),
            "path" => pyo3::intern!(py, "path"),
            "schema_path" => pyo3::intern!(py, "schema_path"),
            _ => unreachable!("fixed keyword name"),
        });
        keywords.push(PyTuple::new(py, values)?);
    }
    Ok(KeywordNames {
        rows: keywords.into_iter().map(Bound::unbind).collect(),
    })
}

/// Enter the Rust body from the actual public Python generator's first resume.
#[pyfunction]
fn keyword_iter<'py>(
    py: Python<'py>,
    keyword: &str,
    validator: &Bound<'py, PyAny>,
    value: &Bound<'py, PyAny>,
    instance: &Bound<'py, PyAny>,
    schema: &Bound<'py, PyAny>,
    names: &Bound<'py, KeywordNames>,
) -> PyResult<Bound<'py, PyAny>> {
    KEYWORD_ENTRIES.fetch_add(1, Ordering::Relaxed);
    iterator(py, keyword, validator, value, instance, schema, names)
}

/// Entries from public translated definitions; never cached validation results.
#[pyfunction]
fn keyword_entry_count() -> usize {
    KEYWORD_ENTRIES.load(Ordering::Relaxed)
}

/// Register the Rust iterator type without loading the Python support modules.
pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<KeywordNames>()?;
    module.add_function(wrap_pyfunction!(keyword_names, module)?)?;
    module.add_function(wrap_pyfunction!(keyword_entry_count, module)?)?;
    module.add_function(wrap_pyfunction!(keyword_iter, module)?)?;
    module.add_class::<OriginalKeywordIterator>()
}
