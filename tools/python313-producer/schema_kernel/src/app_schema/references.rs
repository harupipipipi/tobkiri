//! App-owned reference state and explicit Python resolution boundaries.
//! Derived from referencing 0.37.0, referencing/_core.py and jsonschema.py
//! (MIT; see crate-root COPYING.jsonschema). Builtin ownership is metadata, not authority.
use super::{Environment, Object};
use crate::keywords::{builtin, module_member};
use pyo3::{
    exceptions::{PyLookupError, PyUnicodeEncodeError},
    gc::{PyTraverseError, PyVisit},
    prelude::*,
    sync::PyOnceLock,
    types::{PyDict, PyInt, PyList, PyModule, PyString},
};
use std::{
    cell::RefCell,
    rc::Rc,
    sync::atomic::{AtomicUsize, Ordering},
};

static CANONICAL: PyOnceLock<ResourcePolicy> = PyOnceLock::new();
static POINTERS: AtomicUsize = AtomicUsize::new(0);
static STATIC_ANCHORS: AtomicUsize = AtomicUsize::new(0);
static DYNAMIC_ANCHORS: AtomicUsize = AtomicUsize::new(0);
static ADAPTERS: AtomicUsize = AtomicUsize::new(0);

/// Actual already-crawled builtin metadata objects, not a URI/result cache.
/// Capture once at the trusted provider's first library load, never after each
/// operation's patches. External retrieval never adds ownership to these lists.
pub(super) struct ResourcePolicy {
    specification: Object,
    resolver_type: Object,
    resource_type: Object,
    pointer_function: Object,
    static_type: Object,
    static_function: Object,
    dynamic_type: Object,
    dynamic_function: Object,
    maybe_function: Object,
    method_type: Object,
    resources: Vec<Object>,
    anchors: Vec<Object>,
}
impl ResourcePolicy {
    fn capture(py: Python<'_>) -> PyResult<Self> {
        let specification = module_member(py, "referencing.jsonschema", "DRAFT202012")?;
        let resource_type = module_member(py, "referencing._core", "Resource")?;
        let static_type = module_member(py, "referencing._core", "Anchor")?;
        let dynamic_type = module_member(py, "referencing.jsonschema", "DynamicAnchor")?;
        let registry = module_member(py, "jsonschema.validators", "SPECIFICATIONS")?;
        let values = |mapping: Bound<'_, PyAny>| -> PyResult<Vec<Object>> {
            mapping
                .call_method0("items")?
                .try_iter()?
                .map(|pair| Ok(pair?.get_item(1)?.unbind()))
                .collect()
        };
        let mut resources = values(registry.getattr("_resources")?)?;
        let anchors = values(registry.getattr("_anchors")?)?;
        for anchor in &anchors {
            // Anchor factories created distinct real Resource objects during
            // the original builtin registry's own crawl. Retain those too.
            let resource = anchor.bind(py).getattr("resource")?;
            if !resources.iter().any(|owned| owned.bind(py).is(&resource)) {
                resources.push(resource.unbind());
            }
        }
        Ok(Self {
            pointer_function: resource_type.getattr("pointer")?.unbind(),
            static_function: static_type.getattr("resolve")?.unbind(),
            dynamic_function: dynamic_type.getattr("resolve")?.unbind(),
            maybe_function: specification.getattr("maybe_in_subresource")?.unbind(),
            specification: specification.unbind(),
            resolver_type: module_member(py, "referencing._core", "Resolver")?.unbind(),
            resource_type: resource_type.unbind(),
            static_type: static_type.unbind(),
            dynamic_type: dynamic_type.unbind(),
            method_type: module_member(py, "types", "MethodType")?.unbind(),
            resources,
            anchors,
        })
    }
    pub(super) fn clone_ref(&self, py: Python<'_>) -> Self {
        Self {
            specification: self.specification.clone_ref(py),
            resolver_type: self.resolver_type.clone_ref(py),
            resource_type: self.resource_type.clone_ref(py),
            pointer_function: self.pointer_function.clone_ref(py),
            static_type: self.static_type.clone_ref(py),
            static_function: self.static_function.clone_ref(py),
            dynamic_type: self.dynamic_type.clone_ref(py),
            dynamic_function: self.dynamic_function.clone_ref(py),
            maybe_function: self.maybe_function.clone_ref(py),
            method_type: self.method_type.clone_ref(py),
            resources: self.resources.iter().map(|v| v.clone_ref(py)).collect(),
            anchors: self.anchors.iter().map(|v| v.clone_ref(py)).collect(),
        }
    }
    fn visit(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        for value in [
            &self.specification,
            &self.resolver_type,
            &self.resource_type,
            &self.pointer_function,
            &self.static_type,
            &self.static_function,
            &self.dynamic_type,
            &self.dynamic_function,
            &self.maybe_function,
            &self.method_type,
        ]
        .into_iter()
        .chain(self.resources.iter())
        .chain(self.anchors.iter())
        {
            visit.call(value)?;
        }
        Ok(())
    }
    fn resource(&self, value: &Bound<'_, PyAny>) -> PyResult<bool> {
        let py = value.py();
        Ok(self.resources.iter().any(|owned| owned.bind(py).is(value))
            && value.get_type().is(self.resource_type.bind(py))
            && value
                .getattr("_specification")?
                .is(self.specification.bind(py)))
    }
    fn method(
        &self,
        value: &Bound<'_, PyAny>,
        function: &Object,
        receiver: &Bound<'_, PyAny>,
    ) -> PyResult<bool> {
        let py = value.py();
        // Exact MethodType metadata cannot execute an arbitrary callable's
        // __getattribute__. The actual descriptor lookup already happened once.
        Ok(value.get_type().is(self.method_type.bind(py))
            && value.getattr("__func__")?.is(function.bind(py))
            && value.getattr("__self__")?.is(receiver))
    }
}

/// Closed default-policy metadata, never execution authority. There is no
/// constructor accepting a caller's arbitrary specification or resolver.
#[pyclass(module = "_tobkiri_schema_kernel")]
pub(super) struct DefaultReferencePolicy {
    pub(super) specification: Option<Object>,
    pub(super) resolver_type: Option<Object>,
    pub(super) resources: Option<ResourcePolicy>,
}

#[pymethods]
impl DefaultReferencePolicy {
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Some(value) = &self.specification {
            visit.call(value)?;
        }
        if let Some(value) = &self.resolver_type {
            visit.call(value)?;
        }
        if let Some(value) = &self.resources {
            value.visit(&visit)?;
        }
        Ok(())
    }
    fn __clear__(&mut self) {
        self.specification = None;
        self.resolver_type = None;
        self.resources = None;
    }
}

#[pyfunction]
fn default_reference_policy(py: Python<'_>) -> PyResult<DefaultReferencePolicy> {
    // Select metadata only, before evaluation. No Resource, Resolver, input or
    // validation result is constructed during producer initialization.
    let canonical = CANONICAL.get_or_try_init(py, || ResourcePolicy::capture(py))?;
    Ok(DefaultReferencePolicy {
        specification: Some(canonical.specification.clone_ref(py)),
        resolver_type: Some(canonical.resolver_type.clone_ref(py)),
        resources: Some(canonical.clone_ref(py)),
    })
}

/// The actual URI, registry and dynamic-scope values are owned by Rust frames.
/// Python adapters are materialized only for explicit ID/reference operations.
/// No schema decisions or ID lookups are cached.
pub(super) enum ReferenceState {
    Owned {
        base_uri: Object,
        registry: Object,
        previous: Object,
        // Preserve a real boundary result's identity, and reuse a materialized
        // adapter while this exact state survives an ordinary no-ID descent.
        adapter: RefCell<Option<Object>>,
    },
    Boundary(Object),
}
impl ReferenceState {
    fn received(
        self: &Rc<Self>,
        _py: Python<'_>,
        env: &Environment,
        before: &Bound<'_, PyAny>,
        after: &Bound<'_, PyAny>,
    ) -> PyResult<Rc<Self>> {
        if before.is(after) {
            Ok(self.clone())
        } else {
            Self::from_boundary(after, env)
        }
    }

    /// Default resolver in_subresource control, retaining the real Resource.id
    /// operation even for newly created or external resources. No Resource is
    /// admitted to the builtin ownership set here.
    fn in_resource(
        self: &Rc<Self>,
        py: Python<'_>,
        env: &Environment,
        resource: &Bound<'_, PyAny>,
    ) -> PyResult<Rc<Self>> {
        let Self::Owned {
            base_uri,
            registry,
            previous,
            ..
        } = self.as_ref()
        else {
            let before = self.boundary(py, env)?;
            let after = before.call_method1("in_subresource", (resource,))?;
            return self.received(py, env, &before, &after);
        };
        let id = resource.call_method0("id")?;
        if id.is_none() {
            return Ok(self.clone());
        }
        let uri = env.urljoin.bind(py).call1((base_uri.bind(py), id))?;
        Ok(Rc::new(Self::Owned {
            base_uri: uri.unbind(),
            registry: registry.clone_ref(py),
            previous: previous.clone_ref(py),
            adapter: RefCell::new(None),
        }))
    }

    fn pointer(
        self: &Rc<Self>,
        py: Python<'_>,
        env: &Environment,
        resource: &Bound<'_, PyAny>,
        pointer: &Bound<'_, PyAny>,
    ) -> PyResult<(Object, Rc<Self>)> {
        POINTERS.fetch_add(1, Ordering::Relaxed);
        let mut contents = resource.getattr("contents")?;
        if !pointer.is_truthy()? {
            return Ok((contents.unbind(), self.clone()));
        }
        let decoded = module_member(py, "referencing._core", "unquote")?
            .call1((pointer.get_item(builtin(py, "slice")?.call1((1, py.None()))?)?,))?;
        let parts = decoded.call_method1("split", ("/",))?;
        let mut segments = PyList::empty(py);
        let mut state = self.clone();
        for segment in parts.try_iter()? {
            let segment = segment?;
            let token =
                if contents.is_instance(&module_member(py, "referencing._core", "Sequence")?)? {
                    builtin(py, "int")?.call1((segment,))?
                } else {
                    segment
                        .call_method1("replace", ("~1", "/"))?
                        .call_method1("replace", ("~0", "~"))?
                };
            contents = match contents.get_item(&token) {
                Ok(value) => value,
                Err(error) if error.is_instance_of::<PyLookupError>(py) => {
                    let kwargs = PyDict::new(py);
                    kwargs.set_item("ref", pointer)?;
                    kwargs.set_item("resource", resource)?;
                    let value = module_member(py, "referencing.exceptions", "PointerToNowhere")?
                        .call((), Some(&kwargs))?;
                    value.setattr("__context__", error.value(py))?;
                    let converted = PyErr::from_value(value);
                    converted.set_cause(py, Some(error));
                    return Err(converted);
                }
                Err(error) => return Err(error),
            };
            segments.append(token)?;
            // Preserve original function lookup and argument evaluation order.
            // In particular create_resource is ALWAYS the actual live call.
            let specification = resource.getattr("_specification")?;
            let helper = specification.getattr("maybe_in_subresource")?;
            let subresource = resource
                .getattr("_specification")?
                .call_method1("create_resource", (&contents,))?;
            let native = if specification.is(env.specification.bind(py))
                && helper.is(env.resource_policy.maybe_function.bind(py))
            {
                default_subresource_path(py, &segments)?
            } else {
                None
            };
            let next = if let Some(enter) = native {
                if enter {
                    state.in_resource(py, env, &subresource)?
                } else {
                    state.clone()
                }
            } else {
                let before = state.boundary(py, env)?;
                let kwargs = PyDict::new(py);
                kwargs.set_item("segments", &segments)?;
                kwargs.set_item("resolver", &before)?;
                kwargs.set_item("subresource", subresource)?;
                let after = helper.call((), Some(&kwargs))?;
                state.received(py, env, &before, &after)?
            };
            if !Rc::ptr_eq(&state, &next) {
                segments = PyList::empty(py);
            }
            state = next;
        }
        Ok((contents.unbind(), state))
    }

    fn fragment(
        self: &Rc<Self>,
        py: Python<'_>,
        env: &Environment,
        value: &Bound<'_, PyAny>,
        fragment: &Bound<'_, PyAny>,
        pointer: bool,
    ) -> PyResult<(Object, Rc<Self>)> {
        // Get the real descriptor result ONCE. Non-native dispatch uses that
        // same callable, including a callback's class replacement.
        let callable = value.getattr(if pointer { "pointer" } else { "resolve" })?;
        let policy = &env.resource_policy;
        if pointer {
            if policy.resource(value)?
                && policy.method(&callable, &policy.pointer_function, value)?
            {
                return self.pointer(py, env, value, fragment);
            }
        } else if policy.anchors.iter().any(|owned| owned.bind(py).is(value)) {
            let resource = value.getattr("resource")?;
            if policy.resource(&resource)? {
                if value.get_type().is(policy.static_type.bind(py))
                    && policy.method(&callable, &policy.static_function, value)?
                {
                    STATIC_ANCHORS.fetch_add(1, Ordering::Relaxed);
                    return Ok((resource.getattr("contents")?.unbind(), self.clone()));
                }
                if value.get_type().is(policy.dynamic_type.bind(py))
                    && policy.method(&callable, &policy.dynamic_function, value)?
                {
                    // A native lookup supplies owned state. A custom resolver
                    // never enters this method through its opaque lookup path.
                    if let Self::Owned {
                        registry, previous, ..
                    } = self.as_ref()
                    {
                        DYNAMIC_ANCHORS.fetch_add(1, Ordering::Relaxed);
                        let mut last = resource;
                        for uri in previous.bind(py).try_iter()? {
                            let anchor = match registry
                                .bind(py)
                                .call_method1("anchor", (uri?, value.getattr("name")?))
                            {
                                Ok(retrieved) => retrieved.getattr("value")?,
                                Err(error) => {
                                    let missing = module_member(
                                        py,
                                        "referencing.exceptions",
                                        "NoSuchAnchor",
                                    )?;
                                    if error.value(py).is_instance(&missing)? {
                                        continue;
                                    }
                                    return Err(error);
                                }
                            };
                            if anchor.is_instance(policy.dynamic_type.bind(py))? {
                                last = anchor.getattr("resource")?;
                            }
                        }
                        // Original keyword-argument evaluation reads contents
                        // BEFORE invoking in_subresource on the selected last.
                        let contents = last.getattr("contents")?.unbind();
                        return Ok((contents, self.in_resource(py, env, &last)?));
                    }
                }
            }
        }
        let before = self.boundary(py, env)?;
        let kwargs = PyDict::new(py);
        if pointer {
            kwargs.set_item("pointer", fragment)?;
        }
        kwargs.set_item("resolver", &before)?;
        let resolved = callable.call((), Some(&kwargs))?;
        Ok((
            resolved.getattr("contents")?.unbind(),
            self.received(py, env, &before, &resolved.getattr("resolver")?)?,
        ))
    }

    pub(super) fn from_boundary(
        resolver: &Bound<'_, PyAny>,
        env: &Environment,
    ) -> PyResult<Rc<Self>> {
        // Only the concrete default-policy result can become owned state.
        // Never inspect a subclass/custom resolver's private fields.
        if !resolver
            .get_type()
            .is(env.resolver_type.bind(resolver.py()))
        {
            return Ok(Rc::new(Self::Boundary(resolver.clone().unbind())));
        }
        Ok(Rc::new(Self::Owned {
            base_uri: resolver.getattr("_base_uri")?.unbind(),
            registry: resolver.getattr("_registry")?.unbind(),
            previous: resolver.getattr("_previous")?.unbind(),
            adapter: RefCell::new(Some(resolver.clone().unbind())),
        }))
    }
    pub(super) fn boundary<'py>(
        &self,
        py: Python<'py>,
        env: &Environment,
    ) -> PyResult<Bound<'py, PyAny>> {
        let Self::Owned {
            base_uri,
            registry,
            previous,
            adapter,
        } = self
        else {
            let Self::Boundary(resolver) = self else {
                unreachable!()
            };
            return Ok(resolver.bind(py).clone());
        };
        if let Some(resolver) = adapter.borrow().as_ref() {
            return Ok(resolver.bind(py).clone());
        }
        let kwargs = PyDict::new(py);
        kwargs.set_item("base_uri", base_uri.bind(py))?;
        kwargs.set_item("registry", registry.bind(py))?;
        kwargs.set_item("previous", previous.bind(py))?;
        let resolver = env.resolver_type.bind(py).call((), Some(&kwargs))?;
        ADAPTERS.fetch_add(1, Ordering::Relaxed);
        *adapter.borrow_mut() = Some(resolver.clone().unbind());
        Ok(resolver)
    }
    fn evolved(
        &self,
        py: Python<'_>,
        uri: Bound<'_, PyAny>,
        registry: Bound<'_, PyAny>,
    ) -> PyResult<Rc<Self>> {
        let Self::Owned {
            base_uri, previous, ..
        } = self
        else {
            unreachable!()
        };
        let mut previous = previous.bind(py).clone();
        if base_uri.bind(py).is_truthy()? && (!previous.is_truthy()? || uri.ne(base_uri.bind(py))?)
        {
            previous = previous.call_method1("push_front", (base_uri.bind(py),))?;
        }
        Ok(Rc::new(Self::Owned {
            base_uri: uri.unbind(),
            registry: registry.unbind(),
            previous: previous.unbind(),
            adapter: RefCell::new(None),
        }))
    }
    pub(super) fn lookup(
        &self,
        py: Python<'_>,
        env: &Environment,
        reference: &Bound<'_, PyAny>,
    ) -> PyResult<(Object, Rc<Self>)> {
        let Self::Owned {
            base_uri, registry, ..
        } = self
        else {
            let resolved = self
                .boundary(py, env)?
                .call_method1("lookup", (reference,))?;
            return Ok((
                resolved.getattr("contents")?.unbind(),
                Self::from_boundary(&resolved.getattr("resolver")?, env)?,
            ));
        };
        // Keep the original URI implementation, call order and live arguments.
        let (uri, fragment) = if reference.call_method1("startswith", ("#",))?.is_truthy()? {
            (
                base_uri.bind(py).clone(),
                reference.get_item(builtin(py, "slice")?.call1((1, py.None()))?)?,
            )
        } else {
            let joined = env.urljoin.bind(py).call1((base_uri.bind(py), reference))?;
            let parts = env.urldefrag.bind(py).call1((joined,))?;
            (parts.get_item(0)?, parts.get_item(1)?)
        };
        let retrieved = match registry.bind(py).call_method1("get_or_retrieve", (&uri,)) {
            Ok(value) => value,
            Err(error) => {
                let missing = module_member(py, "referencing.exceptions", "NoSuchResource")?;
                let unavailable = module_member(py, "referencing.exceptions", "Unretrievable")?;
                let suppress = error.value(py).is_instance(&missing)?;
                if !suppress && !error.value(py).is_instance(&unavailable)? {
                    return Err(error);
                }
                let kwargs = PyDict::new(py);
                kwargs.set_item("ref", reference)?;
                let value = module_member(py, "referencing.exceptions", "Unresolvable")?
                    .call((), Some(&kwargs))?;
                value.setattr("__context__", error.value(py))?;
                let converted = PyErr::from_value(value);
                converted.set_cause(py, if suppress { None } else { Some(error) });
                return Err(converted);
            }
        };
        let pointer = fragment.call_method1("startswith", ("/",))?.is_truthy()?;
        if pointer || fragment.is_truthy()? {
            let selected = if pointer {
                retrieved
            } else {
                retrieved
                    .getattr("registry")?
                    .call_method1("anchor", (&uri, &fragment))?
            };
            let state = self.evolved(py, uri, selected.getattr("registry")?)?;
            return state.fragment(py, env, &selected.getattr("value")?, &fragment, pointer);
        }
        let state = self.evolved(py, uri, retrieved.getattr("registry")?)?;
        Ok((
            retrieved.getattr("value")?.getattr("contents")?.unbind(),
            state,
        ))
    }
    pub(super) fn descend(
        self: &Rc<Self>,
        py: Python<'_>,
        env: &Environment,
        schema: &Bound<'_, PyAny>,
    ) -> PyResult<Rc<Self>> {
        if let Self::Boundary(resolver) = self.as_ref() {
            let resource = env
                .specification
                .bind(py)
                .call_method1("create_resource", (schema,))?;
            let next = resolver
                .bind(py)
                .call_method1("in_subresource", (resource,))?;
            return Self::from_boundary(&next, env);
        }
        // This is an owned Draft2020-12 rule, not an identity guess about a
        // received specification. Read the live value on EVERY transition.
        if schema.is_exact_instance_of::<PyDict>() {
            let dictionary = schema.cast::<PyDict>()?;
            if dictionary
                .get_item("$id")?
                .is_none_or(|value| value.is_none())
            {
                return Ok(self.clone());
            }
        }
        // Empty strings and invalid IDs are not absence. Non-builtin mappings
        // also retain their real protocol calls at the resolution boundary.
        let resource = env
            .specification
            .bind(py)
            .call_method1("create_resource", (schema,))?;
        let resolver = self
            .boundary(py, env)?
            .call_method1("in_subresource", (resource,))?;
        Self::from_boundary(&resolver, env)
    }
}

/// None selects the actual helper BEFORE executing it for non-builtin tokens.
/// The list remains the real, mutable callback argument; no path is cached.
fn default_subresource_path(
    py: Python<'_>,
    segments: &Bound<'_, PyList>,
) -> PyResult<Option<bool>> {
    for token in segments.iter() {
        if !token.is_exact_instance_of::<PyString>() && !token.is_exact_instance_of::<PyInt>() {
            return Ok(None);
        }
    }
    let mut index = 0;
    while index < segments.len() {
        let token = segments.get_item(index)?;
        index += 1;
        if !token.is_exact_instance_of::<PyString>() {
            return Ok(Some(false));
        }
        let text = match token.cast::<PyString>()?.to_str() {
            Ok(text) => text,
            // None of the closed ASCII keywords contains a surrogate. Preserve
            // unknown-token behavior without introducing a UTF-8-only error.
            Err(error) if error.is_instance_of::<PyUnicodeEncodeError>(py) => {
                return Ok(Some(false))
            }
            Err(error) => return Err(error),
        };
        match text {
            "additionalProperties"
            | "contains"
            | "contentSchema"
            | "else"
            | "if"
            | "items"
            | "not"
            | "propertyNames"
            | "then"
            | "unevaluatedItems"
            | "unevaluatedProperties" => {}
            "$defs" | "definitions" | "dependentSchemas" | "patternProperties" | "properties"
            | "allOf" | "anyOf" | "oneOf" | "prefixItems" => {
                if index == segments.len() {
                    return Ok(Some(false));
                }
                index += 1;
            }
            _ => return Ok(Some(false)),
        }
    }
    Ok(Some(true))
}

#[pyfunction]
fn app_reference_counts(py: Python<'_>) -> PyResult<Bound<'_, PyDict>> {
    let result = PyDict::new(py);
    result.set_item("native_pointers", POINTERS.load(Ordering::Relaxed))?;
    result.set_item(
        "native_static_anchors",
        STATIC_ANCHORS.load(Ordering::Relaxed),
    )?;
    result.set_item(
        "native_dynamic_anchors",
        DYNAMIC_ANCHORS.load(Ordering::Relaxed),
    )?;
    result.set_item("adapter_constructions", ADAPTERS.load(Ordering::Relaxed))?;
    Ok(result)
}

pub(super) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<DefaultReferencePolicy>()?;
    module.add_function(wrap_pyfunction!(default_reference_policy, module)?)?;
    module.add_function(wrap_pyfunction!(app_reference_counts, module)?)?;
    Ok(())
}
