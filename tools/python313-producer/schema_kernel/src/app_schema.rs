//! Experimental App-owned schema-definition checks, not the generic Validator API.
//!
//! Validation rules are derived from jsonschema 4.26.0 (MIT; COPYING.jsonschema).
//! Rust owns the continuation stack. Children are frames, never Python Validator
//! instances/generators. Python values, actual reference scopes and errors retain
//! their identities. No validation results or schema decisions are cached.
use crate::keywords::{self, builtin, format, module_member, validation_error};
use pyo3::{
    exceptions::{PyMemoryError, PyNotImplementedError, PyRuntimeError},
    prelude::*,
    types::{PyBool, PyDict, PyList, PyString},
};
use std::{
    rc::Rc,
    sync::atomic::{AtomicUsize, Ordering},
};

type Object = Py<PyAny>;
static ENTRIES: AtomicUsize = AtomicUsize::new(0);
static FRAMES: AtomicUsize = AtomicUsize::new(0);

mod cursor;
mod references;
mod value;
use cursor::{Cursor, Entry, Keyword as Selected, Keywords, Pair};
use references::{DefaultReferencePolicy, ReferenceState, ResourcePolicy};
use value::{DefaultValuePolicy, Node, TypePolicy};

struct Environment {
    specification: Object,
    resolver_type: Object,
    checker: Object,
    value_policy: TypePolicy,
    urljoin: Object,
    urldefrag: Object,
    resource_policy: ResourcePolicy,
}

#[derive(Clone, Copy)]
enum ChildKind {
    Properties,
    Additional,
    PropertyNames,
    Items,
    PrefixItems,
    AllOf,
    DependentSchemas,
}

struct Children {
    kind: ChildKind,
    cursor: Cursor,
    live: bool,
}

struct Alternatives {
    cursor: Cursor,
    errors: Vec<Object>,
    branch_errors: usize,
    live: bool,
}

enum Handler {
    Children(Children),
    Reference {
        pending: Option<Box<Frame>>,
        live: bool,
    },
    AnyOf(Alternatives),
}

enum Signal {
    Error(Object),
    Done,
}
enum Action {
    Push(Box<Frame>),
    Resume,
    Yield(Object),
    Done,
}

struct Frame {
    schema: Node,
    instance: Node,
    references: Rc<ReferenceState>,
    // Pending ordinary descents enter their actual subresource lazily. A lookup
    // supplies its own resolver and must not enter it a second time.
    enter_resource: bool,
    root: bool,
    started: bool,
    cursor: Option<Keywords>,
    false_yielded: bool,
    selected: Option<Selected>,
    handler: Option<Handler>,
    path: Option<Object>,
    schema_path: Option<Object>,
}

impl Frame {
    fn new(schema: Object, instance: Object, references: Rc<ReferenceState>, root: bool) -> Self {
        Self {
            schema: Node::new(schema),
            instance: Node::new(instance),
            references,
            enter_resource: !root,
            root,
            started: false,
            cursor: None,
            false_yielded: false,
            selected: None,
            handler: None,
            path: None,
            schema_path: None,
        }
    }

    fn start(&mut self, py: Python<'_>, env: &Environment) -> PyResult<()> {
        FRAMES.fetch_add(1, Ordering::Relaxed);
        self.started = true;
        let schema = self.schema.bind(py);
        if schema.is_instance_of::<PyBool>() {
            return Ok(());
        }
        if self.enter_resource {
            self.references = self.references.descend(py, env, schema)?;
        }
        self.cursor = Some(Keywords::new(schema, self.root)?);
        Ok(())
    }

    fn child(
        &self,
        py: Python<'_>,
        schema: Bound<'_, PyAny>,
        instance: Bound<'_, PyAny>,
        path: Option<Bound<'_, PyAny>>,
        schema_path: Option<Bound<'_, PyAny>>,
    ) -> Box<Frame> {
        let mut frame = Self::new(
            schema.unbind(),
            instance.unbind(),
            self.references.clone(),
            false,
        );
        frame.path = path.map(Bound::unbind);
        frame.schema_path = schema_path.map(Bound::unbind);
        Box::new(frame)
    }

    fn decorate(&self, py: Python<'_>, env: &Environment, error: Object) -> PyResult<Action> {
        let selected = self.selected.as_ref().expect("selected rule");
        let kwargs = PyDict::new(py);
        kwargs.set_item("validator", selected.keyword.bind(py))?;
        kwargs.set_item("validator_value", selected.value.bind(py))?;
        kwargs.set_item("instance", self.instance.bind(py))?;
        kwargs.set_item("schema", self.schema.bind(py))?;
        kwargs.set_item("type_checker", env.value_policy.current_checker(py)?)?;
        let error_bound = error.bind(py);
        error_bound.call_method("_set", (), Some(&kwargs))?;
        let keyword = selected.keyword.bind(py);
        let has_schema_path = if keyword.is_exact_instance_of::<PyString>() {
            selected.name != "if" && selected.name != "$ref"
        } else {
            module_member(py, "app_schema_model", "keyword_has_schema_path")?
                .call1((keyword,))?
                .is_truthy()?
        };
        if has_schema_path {
            error_bound
                .getattr("schema_path")?
                .call_method1("appendleft", (selected.keyword.bind(py),))?;
        }
        if !self.root {
            if let Some(path) = &self.path {
                error_bound
                    .getattr("path")?
                    .call_method1("appendleft", (path.bind(py),))?;
            }
            if let Some(path) = &self.schema_path {
                error_bound
                    .getattr("schema_path")?
                    .call_method1("appendleft", (path.bind(py),))?;
            }
        }
        Ok(Action::Yield(error))
    }

    fn poll(
        &mut self,
        py: Python<'_>,
        env: &Rc<Environment>,
        signal: Option<Signal>,
    ) -> PyResult<Action> {
        if !self.started {
            self.start(py, env)?;
        }
        if self.schema.bind(py).is_instance_of::<PyBool>() {
            if self.false_yielded || self.schema.bind(py).is_truthy()? {
                return Ok(Action::Done);
            }
            self.false_yielded = true;
            let kwargs = PyDict::new(py);
            kwargs.set_item("validator", py.None())?;
            kwargs.set_item("validator_value", py.None())?;
            kwargs.set_item("instance", self.instance.bind(py))?;
            kwargs.set_item("schema", self.schema.bind(py))?;
            return Ok(Action::Yield(
                validation_error(
                    py,
                    format(
                        py,
                        "False schema does not allow {0!r}",
                        &[self.instance.bind(py).clone()],
                    )?,
                    Some(&kwargs),
                )?
                .unbind(),
            ));
        }
        let mut signal = signal;
        loop {
            if let Some(mut handler) = self.handler.take() {
                let outcome = match &mut handler {
                    Handler::Children(children) => {
                        if children.live {
                            match signal.take() {
                                Some(Signal::Error(error)) => {
                                    self.handler = Some(handler);
                                    return self.decorate(py, env, error);
                                }
                                Some(Signal::Done) => children.live = false,
                                None => {
                                    self.handler = Some(handler);
                                    return Ok(Action::Resume);
                                }
                            }
                        }
                        match children.cursor.next(py)? {
                            None => None,
                            Some(item) => {
                                let selected = self.selected.as_ref().expect("selected rule");
                                let value = selected.value.bind(py);
                                let instance = self.instance.bind(py);
                                let child = match (children.kind, item) {
                                    (
                                        ChildKind::Properties,
                                        Entry::Pair(Pair { key, value: schema }),
                                    ) => {
                                        let key = key.bind(py);
                                        if !instance.contains(key)? {
                                            self.handler = Some(handler);
                                            continue;
                                        }
                                        self.child(
                                            py,
                                            schema.bind(py).clone(),
                                            instance.get_item(key)?,
                                            Some(key.clone()),
                                            Some(key.clone()),
                                        )
                                    }
                                    (ChildKind::Additional, Entry::Value(item)) => self.child(
                                        py,
                                        value.clone(),
                                        instance.get_item(item.bind(py))?,
                                        Some(item.into_bound(py)),
                                        None,
                                    ),
                                    (ChildKind::PropertyNames, Entry::Value(item)) => self.child(
                                        py,
                                        value.clone(),
                                        item.into_bound(py),
                                        None,
                                        None,
                                    ),
                                    (ChildKind::Items, Entry::Value(item)) => self.child(
                                        py,
                                        value.clone(),
                                        instance.get_item(item.bind(py))?,
                                        Some(item.into_bound(py)),
                                        None,
                                    ),
                                    (
                                        ChildKind::PrefixItems,
                                        Entry::Prefix {
                                            index,
                                            instance: actual,
                                            schema,
                                        },
                                    ) => {
                                        let index = index.into_bound(py);
                                        self.child(
                                            py,
                                            schema.into_bound(py),
                                            actual.into_bound(py),
                                            Some(index.clone()),
                                            Some(index),
                                        )
                                    }
                                    (
                                        ChildKind::AllOf,
                                        Entry::Indexed {
                                            index,
                                            value: schema,
                                        },
                                    ) => self.child(
                                        py,
                                        schema.into_bound(py),
                                        instance.clone(),
                                        None,
                                        Some(index.into_bound(py)),
                                    ),
                                    (
                                        ChildKind::DependentSchemas,
                                        Entry::Pair(Pair { key, value: schema }),
                                    ) => {
                                        let key = key.into_bound(py);
                                        if !instance.contains(&key)? {
                                            self.handler = Some(handler);
                                            continue;
                                        }
                                        self.child(
                                            py,
                                            schema.into_bound(py),
                                            instance.clone(),
                                            None,
                                            Some(key),
                                        )
                                    }
                                    _ => unreachable!("child cursor shape"),
                                };
                                children.live = true;
                                Some(Action::Push(child))
                            }
                        }
                    }
                    Handler::Reference { pending, live } => {
                        if *live {
                            match signal.take() {
                                Some(Signal::Error(error)) => {
                                    self.handler = Some(handler);
                                    return self.decorate(py, env, error);
                                }
                                Some(Signal::Done) => {
                                    *live = false;
                                    None
                                }
                                None => {
                                    self.handler = Some(handler);
                                    return Ok(Action::Resume);
                                }
                            }
                        } else if let Some(child) = pending.take() {
                            *live = true;
                            Some(Action::Push(child))
                        } else {
                            None
                        }
                    }
                    Handler::AnyOf(alternatives) => {
                        if alternatives.live {
                            match signal.take() {
                                Some(Signal::Error(error)) => {
                                    alternatives.errors.try_reserve(1).map_err(|_| {
                                        PyMemoryError::new_err("native error context allocation")
                                    })?;
                                    alternatives.errors.push(error);
                                    alternatives.branch_errors += 1;
                                    self.handler = Some(handler);
                                    return Ok(Action::Resume);
                                }
                                Some(Signal::Done) => {
                                    alternatives.live = false;
                                    if alternatives.branch_errors == 0 {
                                        // Stop at the first valid branch. No later callback runs.
                                        continue;
                                    }
                                }
                                None => {
                                    self.handler = Some(handler);
                                    return Ok(Action::Resume);
                                }
                            }
                        }
                        if let Some(Entry::Indexed {
                            index,
                            value: schema,
                        }) = alternatives.cursor.next(py)?
                        {
                            alternatives.branch_errors = 0;
                            alternatives.live = true;
                            let child = self.child(
                                py,
                                schema.into_bound(py),
                                self.instance.bind(py).clone(),
                                None,
                                Some(index.into_bound(py)),
                            );
                            Some(Action::Push(child))
                        } else {
                            let errors = PyList::empty(py);
                            for error in &alternatives.errors {
                                errors.append(error.bind(py))?;
                            }
                            let kwargs = PyDict::new(py);
                            kwargs.set_item("context", errors)?;
                            let error = validation_error(
                                py,
                                format(
                                    py,
                                    "{0!r} is not valid under any of the given schemas",
                                    &[self.instance.bind(py).clone()],
                                )?,
                                Some(&kwargs),
                            )?
                            .unbind();
                            return self.decorate(py, env, error);
                        }
                    }
                };
                if let Some(action) = outcome {
                    self.handler = Some(handler);
                    return Ok(action);
                }
            }
            let Some(selected) = self.cursor.as_mut().expect("started META frame").next(py)? else {
                return Ok(Action::Done);
            };
            let name = selected.name.clone();
            let value = selected.value.clone_ref(py).into_bound(py);
            self.selected = Some(selected);
            let instance = self.instance.bind(py);
            let schema = self.schema.bind(py);
            let mut children = |kind, cursor: Cursor| {
                self.handler = Some(Handler::Children(Children {
                    kind,
                    cursor,
                    live: false,
                }));
            };
            match name.as_str() {
                "properties" if json_type(env, instance, "object", schema)? => children(
                    ChildKind::Properties,
                    Cursor::pairs(&value.call_method0("items")?)?,
                ),
                "propertyNames" if json_type(env, instance, "object", schema)? => {
                    children(ChildKind::PropertyNames, Cursor::values(instance)?)
                }
                "dependentSchemas" if json_type(env, instance, "object", schema)? => children(
                    ChildKind::DependentSchemas,
                    Cursor::pairs(&value.call_method0("items")?)?,
                ),
                "additionalProperties" if json_type(env, instance, "object", schema)? => {
                    // Preserve the original extras iterator/set ordering and live
                    // dictionary values; this helper performs no validation.
                    let extras = keywords::utility(py, "find_additional_properties")?
                        .call1((instance, schema))?;
                    let extras = builtin(py, "set")?.call1((extras,))?;
                    if json_type(env, &value, "object", schema)? {
                        children(ChildKind::Additional, Cursor::values(&extras)?);
                    } else if !value.is_truthy()? && extras.is_truthy()? {
                        let message = if schema.contains("patternProperties")? {
                            let verb = if extras.len()? == 1 { "does" } else { "do" };
                            format(
                                py,
                                "{0} {1} not match any of the regexes: {2}",
                                &[
                                    keywords::reprs_joined(
                                        py,
                                        &keywords::sorted(py, &extras, false)?,
                                    )?,
                                    PyString::new(py, verb).into_any(),
                                    keywords::reprs_joined(
                                        py,
                                        &keywords::sorted(
                                            py,
                                            &schema.get_item("patternProperties")?,
                                            false,
                                        )?,
                                    )?,
                                ],
                            )?
                        } else {
                            keywords::extras_message(
                                py,
                                "Additional properties are not allowed (%s %s unexpected)",
                                &keywords::sorted(py, &extras, true)?,
                            )?
                        };
                        let error = validation_error(py, message, None)?.unbind();
                        return self.decorate(py, env, error);
                    }
                }
                "items" if json_type(env, instance, "array", schema)? => {
                    let prefix = schema
                        .call_method1("get", ("prefixItems", PyList::empty(py)))?
                        .len()?;
                    let total = instance.len()?;
                    if total > prefix {
                        if value.is(&false.into_pyobject(py)?) {
                            let extra = total - prefix;
                            let rest = if extra != 1 {
                                instance
                                    .get_item(builtin(py, "slice")?.call1((prefix, py.None()))?)?
                            } else {
                                instance.get_item(prefix)?
                            };
                            let message = format(
                                py,
                                "Expected at most {0} {1} but found {2} extra: {3!r}",
                                &[
                                    keywords::integer(py, prefix),
                                    PyString::new(py, if prefix != 1 { "items" } else { "item" })
                                        .into_any(),
                                    keywords::integer(py, extra),
                                    rest,
                                ],
                            )?;
                            return self.decorate(
                                py,
                                env,
                                validation_error(py, message, None)?.unbind(),
                            );
                        }
                        children(ChildKind::Items, Cursor::range(prefix, total));
                    }
                }
                "prefixItems" if json_type(env, instance, "array", schema)? => {
                    children(ChildKind::PrefixItems, Cursor::prefix(instance, &value)?)
                }
                "allOf" => children(ChildKind::AllOf, Cursor::enumerated(&value)?),
                "anyOf" => {
                    self.handler = Some(Handler::AnyOf(Alternatives {
                        cursor: Cursor::enumerated(&value)?,
                        errors: Vec::new(),
                        branch_errors: 0,
                        live: false,
                    }))
                }
                "$ref" | "$dynamicRef" => {
                    let (contents, references) = match self.references.lookup(py, env, &value) {
                        Ok(resolved) => resolved,
                        Err(error) => {
                            let unresolvable =
                                module_member(py, "referencing.exceptions", "Unresolvable")?;
                            if !error.value(py).is_instance(&unresolvable)? {
                                return Err(error);
                            }
                            let wrapped = module_member(
                                py,
                                "jsonschema.exceptions",
                                "_WrappedReferencingError",
                            )?
                            .call1((error.value(py),))?;
                            wrapped.setattr("__context__", error.value(py))?;
                            let wrapped = PyErr::from_value(wrapped);
                            wrapped.set_cause(py, Some(error));
                            return Err(wrapped);
                        }
                    };
                    let mut child =
                        self.child(py, contents.bind(py).clone(), instance.clone(), None, None);
                    child.references = references;
                    child.enter_resource = false;
                    self.handler = Some(Handler::Reference {
                        pending: Some(child),
                        live: false,
                    });
                }
                "properties"
                | "propertyNames"
                | "dependentSchemas"
                | "additionalProperties"
                | "items"
                | "prefixItems" => {}
                "minimum" | "maximum" | "exclusiveMinimum" | "exclusiveMaximum" => {
                    if let Some(error) = value::numeric_bound(
                        py,
                        &env.value_policy,
                        &name,
                        &value,
                        instance,
                        schema,
                    )? {
                        return self.decorate(py, env, error.unbind());
                    }
                }
                "type" | "const" | "enum" | "multipleOf" | "minItems" | "maxItems"
                | "minLength" | "maxLength" | "minProperties" | "maxProperties" | "uniqueItems"
                | "pattern" | "format" | "not" => {
                    let references = self.references.clone();
                    let error = keywords::scalar_rule(
                        py,
                        &name,
                        &value,
                        instance,
                        |actual, kind| env.value_policy.check(py, actual, kind, schema),
                        || Ok(env.checker.bind(py).clone()),
                        |subschema, actual| {
                            let frame = Frame::new(
                                subschema.clone().unbind(),
                                actual.clone().unbind(),
                                references.clone(),
                                true,
                            );
                            Ok(Execution::new(env.clone(), frame)
                                .first_error(py)?
                                .is_none())
                        },
                    )?;
                    if let Some(error) = error {
                        return self.decorate(py, env, error.unbind());
                    }
                }
                // Annotation keywords are ignored as in the original. Known
                // unimplemented evaluators refuse rather than trust or retry.
                "patternProperties"
                | "required"
                | "dependentRequired"
                | "contains"
                | "oneOf"
                | "if"
                | "unevaluatedItems"
                | "unevaluatedProperties" => {
                    return Err(PyNotImplementedError::new_err(format!(
                        "experimental META rule not yet implemented: {name}"
                    )))
                }
                _ => {}
            }
        }
    }
}

struct Execution {
    env: Rc<Environment>,
    frames: Vec<Box<Frame>>,
}
impl Execution {
    fn new(env: Rc<Environment>, frame: Frame) -> Self {
        Self {
            env,
            frames: vec![Box::new(frame)],
        }
    }
    fn first_error(mut self, py: Python<'_>) -> PyResult<Option<Object>> {
        let mut current = 0;
        let mut signal = None;
        loop {
            let action = self.frames[current].poll(py, &self.env, signal.take())?;
            match action {
                Action::Push(child) => {
                    assert_eq!(current + 1, self.frames.len());
                    self.frames.try_reserve(1).map_err(|_| {
                        PyMemoryError::new_err("native validation frame allocation")
                    })?;
                    self.frames.push(child);
                    current += 1;
                }
                Action::Resume => {
                    current += 1;
                    assert!(current < self.frames.len());
                }
                Action::Yield(error) if current == 0 => return Ok(Some(error)),
                Action::Yield(error) => {
                    current -= 1;
                    signal = Some(Signal::Error(error));
                }
                Action::Done => {
                    assert_eq!(current + 1, self.frames.len());
                    self.frames.pop();
                    if current == 0 {
                        return Ok(None);
                    }
                    current -= 1;
                    signal = Some(Signal::Done);
                }
            }
        }
    }
}

fn json_type(
    env: &Environment,
    value: &Bound<'_, PyAny>,
    kind: &str,
    schema: &Bound<'_, PyAny>,
) -> PyResult<bool> {
    let py = value.py();
    match kind {
        "object" => env
            .value_policy
            .check(py, value, pyo3::intern!(py, "object").as_any(), schema),
        "array" => env
            .value_policy
            .check(py, value, pyo3::intern!(py, "array").as_any(), schema),
        _ => unreachable!("structural App type"),
    }
}

/// Inputs are the live selected provider's META, resource registry and callbacks.
/// The caller chooses the provider BEFORE validation. No original Validator is
/// constructed, called or retried here, including on malformed/invalid inputs.
#[pyfunction]
#[pyo3(signature = (schema, meta, registry, policy, type_checker, format_checker, *, value_policy=None, type_owner=None))]
fn app_check_schema(
    py: Python<'_>,
    schema: &Bound<'_, PyAny>,
    meta: &Bound<'_, PyAny>,
    registry: &Bound<'_, PyAny>,
    policy: &Bound<'_, DefaultReferencePolicy>,
    type_checker: &Bound<'_, PyAny>,
    format_checker: &Bound<'_, PyAny>,
    value_policy: Option<&Bound<'_, DefaultValuePolicy>>,
    type_owner: Option<&Bound<'_, PyAny>>,
) -> PyResult<()> {
    let (specification, resolver_type, resource_policy) = {
        let policy = policy.borrow();
        let specification = policy
            .specification
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("default reference policy was cleared"))?
            .clone_ref(py);
        let resolver_type = policy
            .resolver_type
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("default reference policy was cleared"))?
            .clone_ref(py);
        let resources = policy
            .resources
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("default reference policy was cleared"))?
            .clone_ref(py);
        (specification, resolver_type, resources)
    };
    // Extract opaque configuration before callbacks can inspect an input.
    // No policy borrow survives Resource construction, resolver callbacks or
    // a custom type check that reenters this operation.
    let value_policy = TypePolicy::new(py, type_checker, value_policy, type_owner)?;
    ENTRIES.fetch_add(1, Ordering::Relaxed);
    let resource = specification
        .bind(py)
        .call_method1("create_resource", (meta,))?;
    let resolver = registry.call_method1("resolver_with_root", (resource,))?;
    let env = Rc::new(Environment {
        specification,
        resolver_type,
        checker: format_checker.clone().unbind(),
        value_policy,
        urljoin: module_member(py, "referencing._core", "urljoin")?.unbind(),
        urldefrag: module_member(py, "referencing._core", "urldefrag")?.unbind(),
        resource_policy,
    });
    let references = ReferenceState::from_boundary(&resolver, &env)?;
    let frame = Frame::new(
        meta.clone().unbind(),
        schema.clone().unbind(),
        references,
        true,
    );
    if let Some(error) = Execution::new(env, frame).first_error(py)? {
        let error = module_member(py, "jsonschema.exceptions", "SchemaError")?
            .call_method1("create_from", (error.bind(py),))?;
        return Err(PyErr::from_value(error));
    }
    Ok(())
}

#[pyfunction]
fn app_schema_entry_count() -> usize {
    ENTRIES.load(Ordering::Relaxed)
}
#[pyfunction]
fn app_schema_frame_count() -> usize {
    FRAMES.load(Ordering::Relaxed)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    references::register(module)?;
    value::register(module)?;
    module.add_function(wrap_pyfunction!(app_check_schema, module)?)?;
    module.add_function(wrap_pyfunction!(app_schema_entry_count, module)?)?;
    module.add_function(wrap_pyfunction!(app_schema_frame_count, module)?)?;
    Ok(())
}
