//! Primary bindings written by the App construction family, never a map scan.
//! Registry DATA custody is independent of Resource fragment policy/authority.
use super::Object;
use crate::keywords::module_member;
use pyo3::{
    exceptions::PyRuntimeError,
    ffi,
    gc::{PyTraverseError, PyVisit},
    prelude::*,
    sync::PyOnceLock,
    types::{PyDict, PyString, PyTuple},
};
use std::{
    collections::HashMap,
    rc::Rc,
    sync::{
        atomic::{AtomicBool, AtomicUsize, Ordering},
        Arc, Mutex, MutexGuard,
    },
};

struct Storage<T>(Mutex<T>);
impl<T> Storage<T> {
    fn new(value: T) -> Self {
        Self(Mutex::new(value))
    }
    fn borrow(&self) -> MutexGuard<'_, T> {
        self.0.lock().unwrap_or_else(|error| error.into_inner())
    }
    fn borrow_mut(&self) -> MutexGuard<'_, T> {
        self.borrow()
    }
}

static META: PyOnceLock<Py<RegistryMetadata>> = PyOnceLock::new();
static RESOURCE_HITS: AtomicUsize = AtomicUsize::new(0);
static ANCHOR_HITS: AtomicUsize = AtomicUsize::new(0);
static OPAQUE: AtomicUsize = AtomicUsize::new(0);

fn slot(value: &Bound<'_, PyAny>, id: i32) -> usize {
    unsafe {
        if ffi::PyType_Check(value.as_ptr()) == 0 {
            return 0;
        }
        ffi::PyType_GetSlot(value.as_ptr().cast(), id) as usize
    }
}
fn namespace(class: &Bound<'_, PyAny>) -> PyResult<Object> {
    // A real live namespace proxy, obtained through type's fixed C getset.
    // No metaclass user getter can substitute this metadata. No dict scan.
    let py = class.py();
    let type_class =
        unsafe { Bound::<PyAny>::from_borrowed_ptr(py, (&raw mut ffi::PyType_Type).cast()) };
    let descriptor = type_class.getattr("__dict__")?.get_item("__dict__")?;
    Ok(descriptor.call_method1("__get__", (class,))?.unbind())
}
fn raw<'py>(namespace: &Bound<'py, PyAny>, name: &str) -> PyResult<Option<Bound<'py, PyAny>>> {
    let value = namespace.call_method1("get", (name,))?;
    Ok(if value.is_none() { None } else { Some(value) })
}
fn same_raw(class: &Object, name: &str, value: &Object, py: Python<'_>) -> PyResult<bool> {
    Ok(raw(class.bind(py), name)?.is_some_and(|item| item.is(value.bind(py))))
}
fn c_function(value: &Bound<'_, PyAny>) -> usize {
    unsafe {
        if ffi::PyCFunction_Check(value.as_ptr()) == 0 {
            return 0;
        }
        ffi::PyCFunction_GetFunction(value.as_ptr()).map_or(0, |function| function as usize)
    }
}
fn native_method(method: &Bound<'_, PyAny>, receiver: &Bound<'_, PyAny>, function: usize) -> bool {
    function != 0
        && c_function(method) == function
        && unsafe { ffi::PyCFunction_GetSelf(method.as_ptr()) == receiver.as_ptr() }
}

struct Function {
    value: Object,
    code: Object,
}
impl Function {
    fn clone_ref(&self, py: Python<'_>) -> Self {
        Self {
            value: self.value.clone_ref(py),
            code: self.code.clone_ref(py),
        }
    }
    fn capture(value: Bound<'_, PyAny>) -> PyResult<Self> {
        Ok(Self {
            code: value.getattr("__code__")?.unbind(),
            value: value.unbind(),
        })
    }
    fn same(&self, value: &Bound<'_, PyAny>) -> PyResult<bool> {
        Ok(value.is(self.value.bind(value.py()))
            && value.getattr("__code__")?.is(self.code.bind(value.py())))
    }
    fn visit(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.value)?;
        visit.call(&self.code)
    }
}
struct Factory {
    class: Object,
    namespace: Object,
    init: Function,
    new_slot: usize,
    init_slot: usize,
    call_slot: usize,
    defaults: Object,
    globals: Vec<(String, Object)>,
}
impl Factory {
    fn clone_ref(&self, py: Python<'_>) -> Self {
        Self {
            class: self.class.clone_ref(py),
            namespace: self.namespace.clone_ref(py),
            init: self.init.clone_ref(py),
            new_slot: self.new_slot,
            init_slot: self.init_slot,
            call_slot: self.call_slot,
            defaults: self.defaults.clone_ref(py),
            globals: self
                .globals
                .iter()
                .map(|(name, value)| (name.clone(), value.clone_ref(py)))
                .collect(),
        }
    }
    fn capture(class: Bound<'_, PyAny>) -> PyResult<Self> {
        let init = Function::capture(class.getattr("__init__")?)?;
        let function = init.value.bind(class.py());
        let globals = function.getattr("__globals__")?;
        let dictionary = globals.cast::<PyDict>()?;
        let mut used = Vec::new();
        for name in init
            .code
            .bind(class.py())
            .getattr("co_names")?
            .cast::<PyTuple>()?
            .iter()
        {
            if let Some(value) = dictionary.get_item(&name)? {
                used.push((name.extract()?, value.unbind()));
            }
        }
        Ok(Self {
            namespace: namespace(&class)?,
            new_slot: slot(&class, ffi::Py_tp_new),
            init_slot: slot(&class, ffi::Py_tp_init),
            call_slot: slot(class.get_type().as_any(), ffi::Py_tp_call),
            defaults: function.getattr("__defaults__")?.unbind(),
            class: class.unbind(),
            init,
            globals: used,
        })
    }
    fn same(&self, class: &Bound<'_, PyAny>) -> PyResult<bool> {
        let py = class.py();
        if !class.is(self.class.bind(py))
            || slot(class, ffi::Py_tp_new) != self.new_slot
            || slot(class, ffi::Py_tp_init) != self.init_slot
            || slot(class.get_type().as_any(), ffi::Py_tp_call) != self.call_slot
        {
            return Ok(false);
        }
        let Some(init) = raw(self.namespace.bind(py), "__init__")? else {
            return Ok(false);
        };
        if !self.init.same(&init)?
            || !init.getattr("__defaults__")?.is(self.defaults.bind(py))
            || !init.getattr("__kwdefaults__")?.is_none()
        {
            return Ok(false);
        }
        let globals = init.getattr("__globals__")?;
        let globals = globals.cast::<PyDict>()?;
        for (name, before) in &self.globals {
            if !globals
                .get_item(name)?
                .is_some_and(|after| after.is(before.bind(py)))
            {
                return Ok(false);
            }
        }
        Ok(true)
    }
    fn visit(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.class)?;
        visit.call(&self.namespace)?;
        self.init.visit(visit)?;
        visit.call(&self.defaults)?;
        for (_, value) in &self.globals {
            visit.call(value)?;
        }
        Ok(())
    }
}

struct Metadata {
    registry: Factory,
    resolver: Factory,
    map: Object,
    map_namespace: Object,
    set: Object,
    map_new: usize,
    set_new: usize,
    map_init: usize,
    set_init: usize,
    getattro: usize,
    methods: HashMap<&'static str, Function>,
    members: Vec<(&'static str, Object)>,
    resolver_registry: Object,
    method_type: Object,
    map_get: Object,
    map_insert: usize,
    map_update: usize,
    set_insert: usize,
    set_update: usize,
    internal_slots: Vec<(Object, &'static str, Object)>,
    empty_uncrawled: Object,
    evolve: Function,
}
impl Metadata {
    fn clone_ref(&self, py: Python<'_>) -> Self {
        Self {
            registry: self.registry.clone_ref(py),
            resolver: self.resolver.clone_ref(py),
            map: self.map.clone_ref(py),
            map_namespace: self.map_namespace.clone_ref(py),
            set: self.set.clone_ref(py),
            map_new: self.map_new,
            set_new: self.set_new,
            map_init: self.map_init,
            set_init: self.set_init,
            getattro: self.getattro,
            methods: self
                .methods
                .iter()
                .map(|(name, value)| (*name, value.clone_ref(py)))
                .collect(),
            members: self
                .members
                .iter()
                .map(|(name, value)| (*name, value.clone_ref(py)))
                .collect(),
            resolver_registry: self.resolver_registry.clone_ref(py),
            method_type: self.method_type.clone_ref(py),
            map_get: self.map_get.clone_ref(py),
            map_insert: self.map_insert,
            map_update: self.map_update,
            set_insert: self.set_insert,
            set_update: self.set_update,
            internal_slots: self
                .internal_slots
                .iter()
                .map(|(class, name, descriptor)| {
                    (class.clone_ref(py), *name, descriptor.clone_ref(py))
                })
                .collect(),
            empty_uncrawled: self.empty_uncrawled.clone_ref(py),
            evolve: self.evolve.clone_ref(py),
        }
    }
    fn capture(py: Python<'_>) -> PyResult<Self> {
        let registry = Factory::capture(module_member(py, "referencing._core", "Registry")?)?;
        let resolver = Factory::capture(module_member(py, "referencing._core", "Resolver")?)?;
        let map = module_member(py, "rpds", "HashTrieMap")?;
        let set = module_member(py, "rpds", "HashTrieSet")?;
        let map_namespace = namespace(&map)?;
        // Empty metadata objects only. No definition/input map is inspected.
        let empty = map.call0()?;
        let view = empty.call_method0("items")?;
        let iterator = view.try_iter()?;
        let empty_set = set.call0()?;
        let set_iterator = empty_set.try_iter()?;
        let mut internal_slots = Vec::new();
        for (class, names) in [
            (map.clone(), &["items"][..]),
            (view.get_type().into_any(), &["__iter__", "__len__"][..]),
            (iterator.get_type().into_any(), &["__next__"][..]),
            (set.clone(), &["__iter__"][..]),
            (set_iterator.get_type().into_any(), &["__next__"][..]),
        ] {
            let namesp = namespace(&class)?;
            for &name in names {
                let descriptor = raw(namesp.bind(py), name)?
                    .ok_or_else(|| PyRuntimeError::new_err("missing native registry slot"))?;
                internal_slots.push((namesp.clone_ref(py), name, descriptor.unbind()));
            }
        }
        let mut methods = HashMap::new();
        for name in [
            "with_resources",
            "with_resource",
            "crawl",
            "combine",
            "resolver_with_root",
            "get_or_retrieve",
            "anchor",
        ] {
            methods.insert(
                name,
                Function::capture(registry.class.bind(py).getattr(name)?)?,
            );
        }
        let mut members = Vec::new();
        for name in ["_resources", "_anchors", "_uncrawled", "_retrieve"] {
            members.push((
                name,
                raw(registry.namespace.bind(py), name)?
                    .ok_or_else(|| PyRuntimeError::new_err("missing Registry member"))?
                    .unbind(),
            ));
        }
        Ok(Self {
            map_new: slot(&map, ffi::Py_tp_new),
            map_init: slot(&map, ffi::Py_tp_init),
            set_new: slot(&set, ffi::Py_tp_new),
            set_init: slot(&set, ffi::Py_tp_init),
            getattro: slot(
                &module_member(py, "builtins", "object")?,
                ffi::Py_tp_getattro,
            ),
            map_get: raw(map_namespace.bind(py), "get")?
                .ok_or_else(|| PyRuntimeError::new_err("missing map get"))?
                .unbind(),
            map_insert: c_function(&empty.getattr("insert")?),
            map_update: c_function(&empty.getattr("update")?),
            set_insert: c_function(&empty_set.getattr("insert")?),
            set_update: c_function(&empty_set.getattr("update")?),
            resolver_registry: raw(resolver.namespace.bind(py), "_registry")?
                .ok_or_else(|| PyRuntimeError::new_err("missing Resolver registry"))?
                .unbind(),
            method_type: module_member(py, "types", "MethodType")?.unbind(),
            empty_uncrawled: module_member(py, "referencing._core", "EMPTY_UNCRAWLED")?.unbind(),
            evolve: Function::capture(module_member(py, "referencing._core", "evolve")?)?,
            registry,
            resolver,
            map: map.unbind(),
            map_namespace,
            set: set.unbind(),
            methods,
            members,
            internal_slots,
        })
    }
    fn plain_registry(&self, value: &Bound<'_, PyAny>) -> PyResult<bool> {
        let py = value.py();
        if !value.get_type().is(self.registry.class.bind(py))
            || slot(value.get_type().as_any(), ffi::Py_tp_getattro) != self.getattro
        {
            return Ok(false);
        }
        for (name, descriptor) in &self.members {
            if !same_raw(&self.registry.namespace, name, descriptor, py)? {
                return Ok(false);
            }
        }
        Ok(true)
    }
    fn method(
        &self,
        method: &Bound<'_, PyAny>,
        receiver: &Bound<'_, PyAny>,
        name: &str,
    ) -> PyResult<bool> {
        let py = method.py();
        let Some(expected) = self.methods.get(name) else {
            return Ok(false);
        };
        Ok(method.get_type().is(self.method_type.bind(py))
            && method.getattr("__self__")?.is(receiver)
            && expected.same(&method.getattr("__func__")?)?)
    }
    fn map_plain(&self, value: &Bound<'_, PyAny>) -> bool {
        value.get_type().is(self.map.bind(value.py()))
            && slot(value.get_type().as_any(), ffi::Py_tp_getattro) == self.getattro
    }
    fn bulk_slots(&self, py: Python<'_>, set: bool) -> PyResult<bool> {
        for (index, (class, name, descriptor)) in self.internal_slots.iter().enumerate() {
            if (index >= 4) == set && !same_raw(class, name, descriptor, py)? {
                return Ok(false);
            }
        }
        Ok(true)
    }
    fn visit(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        self.registry.visit(visit)?;
        self.resolver.visit(visit)?;
        self.evolve.visit(visit)?;
        for value in [
            &self.map,
            &self.map_namespace,
            &self.set,
            &self.resolver_registry,
            &self.method_type,
            &self.map_get,
            &self.empty_uncrawled,
        ] {
            visit.call(value)?;
        }
        for value in self.methods.values() {
            value.visit(visit)?;
        }
        for (_, value) in &self.members {
            visit.call(value)?;
        }
        for (class, _, descriptor) in &self.internal_slots {
            visit.call(class)?;
            visit.call(descriptor)?;
        }
        Ok(())
    }
}

#[derive(Hash, Eq, PartialEq, Clone)]
enum Key {
    Resource(String),
    Anchor(String, String),
}
fn text(value: &Bound<'_, PyAny>) -> Option<String> {
    if !value.is_exact_instance_of::<PyString>() {
        return None;
    }
    value
        .cast::<PyString>()
        .ok()?
        .to_str()
        .ok()
        .map(str::to_owned)
}
fn binding_key(value: &Bound<'_, PyAny>, anchor: bool) -> Option<Key> {
    if !anchor {
        return Some(Key::Resource(text(value)?));
    }
    if !value.is_exact_instance_of::<PyTuple>() {
        return None;
    }
    let tuple = value.cast::<PyTuple>().ok()?;
    if tuple.len() != 2 {
        return None;
    }
    Some(Key::Anchor(
        text(&tuple.get_item(0).ok()?)?,
        text(&tuple.get_item(1).ok()?)?,
    ))
}
#[pyclass(weakref, module = "_tobkiri_schema_kernel")]
struct RegistryMetadata {
    value: Option<Arc<Metadata>>,
}
#[pymethods]
impl RegistryMetadata {
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Some(value) = &self.value {
            value.visit(&visit)?;
        }
        Ok(())
    }
    fn __clear__(slf: &Bound<'_, Self>) {
        let old = { slf.borrow_mut().value.take() };
        drop(old);
    }
}
struct MetadataHandle {
    owner: Py<RegistryMetadata>,
    value: Arc<Metadata>,
}
impl MetadataHandle {
    fn new(py: Python<'_>, owner: &Py<RegistryMetadata>) -> PyResult<Self> {
        let value = owner
            .borrow(py)
            .value
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("registry metadata was cleared"))?
            .clone();
        Ok(Self {
            owner: owner.clone_ref(py),
            value,
        })
    }
    fn clone_ref(&self, py: Python<'_>) -> Self {
        Self {
            owner: self.owner.clone_ref(py),
            value: self.value.clone(),
        }
    }
}
impl std::ops::Deref for MetadataHandle {
    type Target = Metadata;
    fn deref(&self) -> &Metadata {
        &self.value
    }
}

#[pyclass(weakref, module = "_tobkiri_schema_kernel")]
struct Rows {
    entries: Storage<HashMap<Key, Object>>,
    sealed: AtomicBool,
}
impl Rows {
    fn empty(py: Python<'_>) -> PyResult<Py<Self>> {
        Py::new(
            py,
            Self {
                entries: Storage::new(HashMap::new()),
                sealed: AtomicBool::new(false),
            },
        )
    }
    fn duplicate(&self, py: Python<'_>) -> PyResult<Py<Self>> {
        let entries = self
            .entries
            .borrow()
            .iter()
            .map(|(key, value)| (key.clone(), value.clone_ref(py)))
            .collect();
        Py::new(
            py,
            Self {
                entries: Storage::new(entries),
                sealed: AtomicBool::new(false),
            },
        )
    }
    fn get(&self, py: Python<'_>, key: &Key) -> Option<Object> {
        self.entries
            .borrow()
            .get(key)
            .map(|value| value.clone_ref(py))
    }
    fn merge(&self, py: Python<'_>, target: &Rows) {
        for (key, value) in self.entries.borrow().iter() {
            target
                .entries
                .borrow_mut()
                .insert(key.clone(), value.clone_ref(py));
        }
    }
}
#[pymethods]
impl Rows {
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        for value in self.entries.borrow().values() {
            visit.call(value)?;
        }
        Ok(())
    }
    fn __clear__(slf: &Bound<'_, Self>) {
        let old = {
            let holder = slf.borrow();
            let old = std::mem::take(&mut *holder.entries.borrow_mut());
            old
        };
        drop(old);
    }
}
struct Map {
    actual: Object,
    rows: Py<Rows>,
}
impl Map {
    fn fork(&self, py: Python<'_>) -> Self {
        self.copy(py)
    }
    fn copy(&self, py: Python<'_>) -> Self {
        self.rows.borrow(py).sealed.store(true, Ordering::Release);
        Self {
            actual: self.actual.clone_ref(py),
            rows: self.rows.clone_ref(py),
        }
    }
    fn writable(&mut self, py: Python<'_>) -> PyResult<()> {
        if self.rows.borrow(py).sealed.load(Ordering::Acquire) {
            let rows = self.rows.borrow(py).duplicate(py)?;
            self.rows = rows;
        }
        Ok(())
    }
}
struct State {
    registry: Object,
    resources: Option<Map>,
    anchors: Option<Map>,
    uncrawled: Option<Object>,
    meta: MetadataHandle,
}
impl State {
    fn opaque(&mut self) {
        self.resources = None;
        self.anchors = None;
        self.uncrawled = None;
    }
    fn visit(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.registry)?;
        visit.call(&self.meta.owner)?;
        for map in [&self.resources, &self.anchors].into_iter().flatten() {
            visit.call(&map.actual)?;
            visit.call(&map.rows)?;
        }
        if let Some(value) = &self.uncrawled {
            visit.call(value)?;
        }
        Ok(())
    }
}

#[pyclass(module = "_tobkiri_schema_kernel")]
pub(super) struct RegistryPolicy {
    meta: Option<Py<RegistryMetadata>>,
}
#[pymethods]
impl RegistryPolicy {
    fn empty(&self, py: Python<'_>) -> PyResult<RegistryState> {
        self.construct_empty(py, None)
    }
    fn with_retrieve(
        &self,
        py: Python<'_>,
        retrieve: &Bound<'_, PyAny>,
    ) -> PyResult<RegistryState> {
        self.construct_empty(py, Some(retrieve))
    }
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Some(meta) = &self.meta {
            visit.call(meta)?;
        }
        Ok(())
    }
    fn __clear__(slf: &Bound<'_, Self>) {
        let old = { slf.borrow_mut().meta.take() };
        drop(old);
    }
}
impl RegistryPolicy {
    fn construct_empty(
        &self,
        py: Python<'_>,
        retrieve: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<RegistryState> {
        let owner = self
            .meta
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("registry policy was cleared"))?;
        let meta = MetadataHandle::new(py, owner)?;
        let kwargs = PyDict::new(py);
        if let Some(retrieve) = retrieve {
            kwargs.set_item("retrieve", retrieve)?;
        }
        let eligible = meta.registry.same(meta.registry.class.bind(py))?
            && slot(meta.map.bind(py), ffi::Py_tp_new) == meta.map_new
            && slot(meta.map.bind(py), ffi::Py_tp_init) == meta.map_init
            && slot(meta.set.bind(py), ffi::Py_tp_new) == meta.set_new
            && slot(meta.set.bind(py), ffi::Py_tp_init) == meta.set_init;
        let (resources, anchors, uncrawled) = if eligible {
            let resources = meta.map.bind(py).call0()?.unbind();
            let anchors = meta.map.bind(py).call0()?.unbind();
            let uncrawled = meta.set.bind(py).call0()?.unbind();
            kwargs.set_item("resources", resources.bind(py))?;
            kwargs.set_item("anchors", anchors.bind(py))?;
            kwargs.set_item("uncrawled", uncrawled.bind(py))?;
            (
                Some(Map {
                    actual: resources,
                    rows: Rows::empty(py)?,
                }),
                Some(Map {
                    actual: anchors,
                    rows: Rows::empty(py)?,
                }),
                Some(uncrawled),
            )
        } else {
            (None, None, None)
        };
        let registry = meta
            .registry
            .class
            .bind(py)
            .call((), Some(&kwargs))?
            .unbind();
        let mut state = State {
            registry,
            resources,
            anchors,
            uncrawled,
            meta,
        };
        certify(&mut state, py)?;
        Ok(RegistryState {
            state: Storage::new(Some(state)),
        })
    }
}

fn certify(state: &mut State, py: Python<'_>) -> PyResult<()> {
    let registry = state.registry.bind(py);
    let valid = state
        .meta
        .registry
        .same(state.meta.registry.class.bind(py))?
        && state.meta.plain_registry(registry)?
        && state
            .resources
            .as_ref()
            .is_some_and(|map| state.meta.map_plain(map.actual.bind(py)))
        && state
            .anchors
            .as_ref()
            .is_some_and(|map| state.meta.map_plain(map.actual.bind(py)));
    if !valid {
        state.opaque();
        return Ok(());
    }
    let resources = registry.getattr("_resources")?;
    let anchors = registry.getattr("_anchors")?;
    let uncrawled = registry.getattr("_uncrawled")?;
    if !resources.is(state.resources.as_ref().unwrap().actual.bind(py))
        || !anchors.is(state.anchors.as_ref().unwrap().actual.bind(py))
        || !state
            .uncrawled
            .as_ref()
            .is_some_and(|set| uncrawled.is(set.bind(py)))
    {
        state.opaque();
    }
    Ok(())
}

#[pyclass(weakref, module = "_tobkiri_schema_kernel")]
pub(super) struct RegistryState {
    state: Storage<Option<State>>,
}
impl RegistryState {
    pub(super) fn require_available(&self) -> PyResult<()> {
        if self.state.borrow().is_none() {
            return Err(PyRuntimeError::new_err("registry state was cleared"));
        }
        Ok(())
    }
    fn take(&self) -> PyResult<State> {
        self.state
            .borrow_mut()
            .take()
            .ok_or_else(|| PyRuntimeError::new_err("registry construction state is unavailable"))
    }
    fn put(&self, state: State) {
        *self.state.borrow_mut() = Some(state);
    }
    pub(super) fn view(&self, py: Python<'_>) -> PyResult<Option<Rc<View>>> {
        let state = self.state.borrow();
        let state = state
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("registry state was cleared"))?;
        let (Some(resources), Some(anchors)) = (&state.resources, &state.anchors) else {
            return Ok(None);
        };
        Ok(Some(Rc::new(View {
            registry: state.registry.clone_ref(py),
            resources: resources.copy(py),
            anchors: anchors.copy(py),
            meta: state.meta.clone_ref(py),
        })))
    }
}
#[pymethods]
impl RegistryState {
    // Bounded observations of this construction's real DATA. These share the
    // production hit implementation; no map/ref can be supplied for admission.
    fn _resource(&self, py: Python<'_>, uri: &Bound<'_, PyAny>) -> PyResult<Object> {
        self.observed_lookup(py, uri, None)
    }
    fn _anchor(
        &self,
        py: Python<'_>,
        uri: &Bound<'_, PyAny>,
        name: &Bound<'_, PyAny>,
    ) -> PyResult<Object> {
        self.observed_lookup(py, uri, Some(name))
    }
    #[getter]
    fn registry(&self, py: Python<'_>) -> PyResult<Object> {
        Ok(self
            .state
            .borrow()
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("registry state was cleared"))?
            .registry
            .clone_ref(py))
    }
    fn fork(&self, py: Python<'_>) -> PyResult<Self> {
        let state = self.state.borrow();
        let state = state
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("registry state was cleared"))?;
        Ok(Self {
            state: Storage::new(Some(State {
                registry: state.registry.clone_ref(py),
                resources: state.resources.as_ref().map(|map| map.fork(py)),
                anchors: state.anchors.as_ref().map(|map| map.fork(py)),
                uncrawled: state.uncrawled.as_ref().map(|value| value.clone_ref(py)),
                meta: state.meta.clone_ref(py),
            })),
        })
    }
    fn matches(&self, registry: &Bound<'_, PyAny>) -> PyResult<bool> {
        Ok(self
            .registry(registry.py())?
            .bind(registry.py())
            .is(registry))
    }
    fn method(&self, method: &Bound<'_, PyAny>, name: &str) -> PyResult<bool> {
        let (meta, receiver) = {
            let state = self.state.borrow();
            let state = state
                .as_ref()
                .ok_or_else(|| PyRuntimeError::new_err("registry state was cleared"))?;
            (
                state.meta.clone_ref(method.py()),
                state.registry.clone_ref(method.py()),
            )
        };
        meta.method(method, receiver.bind(method.py()), name)
    }
    fn opaque(&self, registry: Object) -> PyResult<()> {
        let mut state = self.take()?;
        state.opaque();
        state.registry = registry;
        self.put(state);
        Ok(())
    }
    fn insert(
        &self,
        py: Python<'_>,
        anchor: bool,
        method: &Bound<'_, PyAny>,
        before: &Bound<'_, PyAny>,
        key: &Bound<'_, PyAny>,
        value: &Bound<'_, PyAny>,
    ) -> PyResult<Object> {
        let mut state = self.take()?;
        let result = method.call1((key, value));
        if let Ok(after) = &result {
            let map = if anchor {
                &mut state.anchors
            } else {
                &mut state.resources
            };
            if let (Some(map), Some(key)) = (map.as_mut(), binding_key(key, anchor)) {
                if map.actual.bind(py).is(before)
                    && state.meta.map_plain(before)
                    && native_method(method, before, state.meta.map_insert)
                    && state.meta.map_plain(after)
                {
                    map.writable(py)?;
                    map.rows
                        .borrow(py)
                        .entries
                        .borrow_mut()
                        .insert(key, value.clone().unbind());
                    map.actual = after.clone().unbind();
                } else {
                    state.opaque();
                }
            } else {
                state.opaque();
            }
        }
        self.put(state);
        result.map(Bound::unbind)
    }
    fn bulk(
        &self,
        py: Python<'_>,
        anchor: bool,
        method: &Bound<'_, PyAny>,
        before: &Bound<'_, PyAny>,
        other_map: &Bound<'_, PyAny>,
        other: &Bound<'_, RegistryState>,
    ) -> PyResult<Object> {
        let incoming = {
            let other = other.borrow();
            let state = other.state.borrow();
            state
                .as_ref()
                .and_then(|state| {
                    if anchor {
                        state.anchors.as_ref()
                    } else {
                        state.resources.as_ref()
                    }
                })
                .map(|map| map.copy(py))
        };
        let mut state = self.take()?;
        let eligible = state.meta.map_plain(before)
            && state.meta.map_plain(other_map)
            && native_method(method, before, state.meta.map_update)
            && state.meta.bulk_slots(py, false)?;
        let result = method.call1((other_map,));
        if let Ok(after) = &result {
            let map = if anchor {
                &mut state.anchors
            } else {
                &mut state.resources
            };
            if let (Some(map), Some(incoming)) = (map.as_mut(), incoming) {
                if eligible
                    && map.actual.bind(py).is(before)
                    && incoming.actual.bind(py).is(other_map)
                    && state.meta.map_plain(after)
                {
                    if !incoming.rows.borrow(py).entries.borrow().is_empty() {
                        map.writable(py)?;
                        incoming.rows.borrow(py).merge(py, &map.rows.borrow(py));
                    }
                    map.actual = after.clone().unbind();
                } else {
                    state.opaque();
                }
            } else {
                state.opaque();
            }
        }
        self.put(state);
        result.map(Bound::unbind)
    }
    #[pyo3(signature = (method, before, argument, *, bulk=false, other=None))]
    fn uncrawled(
        &self,
        py: Python<'_>,
        method: &Bound<'_, PyAny>,
        before: &Bound<'_, PyAny>,
        argument: &Bound<'_, PyAny>,
        bulk: bool,
        other: Option<&Bound<'_, RegistryState>>,
    ) -> PyResult<Object> {
        let incoming = if let Some(other) = other {
            let other = other.borrow();
            let state = other.state.borrow();
            state
                .as_ref()
                .and_then(|state| state.uncrawled.as_ref())
                .map(|value| value.clone_ref(py))
        } else {
            None
        };
        let mut state = self.take()?;
        let eligible = before.get_type().is(state.meta.set.bind(py))
            && state
                .uncrawled
                .as_ref()
                .is_some_and(|value| value.bind(py).is(before))
            && native_method(
                method,
                before,
                if bulk {
                    state.meta.set_update
                } else {
                    state.meta.set_insert
                },
            )
            && if bulk {
                state.meta.bulk_slots(py, true)?
                    && argument.get_type().is(state.meta.set.bind(py))
                    && incoming
                        .as_ref()
                        .is_some_and(|value| value.bind(py).is(argument))
            } else {
                text(argument).is_some()
            };
        let result = method.call1((argument,));
        if let Ok(after) = &result {
            if eligible && after.get_type().is(state.meta.set.bind(py)) {
                state.uncrawled = Some(after.clone().unbind());
            } else {
                state.opaque();
            }
        }
        self.put(state);
        result.map(Bound::unbind)
    }
    #[pyo3(signature = (evolve, **changes))]
    fn evolve(
        &self,
        py: Python<'_>,
        evolve: &Bound<'_, PyAny>,
        changes: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<()> {
        let mut state = self.take()?;
        let changes = changes.map(Bound::clone).unwrap_or_else(|| PyDict::new(py));
        let receiver = state.registry.clone_ref(py);
        let registry = receiver.bind(py);
        let eligible = state.meta.evolve.same(evolve)?
            && state.meta.plain_registry(registry)?
            && state
                .meta
                .registry
                .same(state.meta.registry.class.bind(py))?
            && !state
                .meta
                .registry
                .namespace
                .bind(py)
                .contains("__class__")?;
        let result = if eligible {
            // The App owns this fixed four-field definition, not attrs.fields
            // or an arbitrary attrs evolution API/private helper namespace.
            let class = registry.getattr("__class__")?;
            for (name, alias) in [
                ("_resources", "resources"),
                ("_anchors", "anchors"),
                ("_uncrawled", "uncrawled"),
                ("_retrieve", "retrieve"),
            ] {
                if !changes.contains(alias)? {
                    changes.set_item(alias, registry.getattr(name)?)?;
                }
            }
            class.call((), Some(&changes))
        } else {
            state.opaque();
            evolve.call((registry,), Some(&changes))
        };
        if let Ok(registry) = &result {
            state.registry = registry.clone().unbind();
            if let Some(uncrawled) = changes.get_item("uncrawled")? {
                if uncrawled.is(state.meta.empty_uncrawled.bind(py)) {
                    state.uncrawled = Some(uncrawled.unbind());
                }
            }
            certify(&mut state, py)?;
        }
        self.put(state);
        result.map(|_| ())
    }
    fn resolver(
        &self,
        py: Python<'_>,
        constructor: &Bound<'_, PyAny>,
        uri: &Bound<'_, PyAny>,
    ) -> PyResult<(Object, bool)> {
        let mut state = self.take()?;
        let eligible = state.meta.resolver.same(constructor)?;
        let kwargs = PyDict::new(py);
        kwargs.set_item("base_uri", uri)?;
        kwargs.set_item("registry", state.registry.bind(py))?;
        let result = constructor.call((), Some(&kwargs));
        let owned = if let Ok(resolver) = &result {
            eligible
                && state.meta.resolver.same(constructor)?
                && resolver.get_type().is(state.meta.resolver.class.bind(py))
                && slot(resolver.get_type().as_any(), ffi::Py_tp_getattro) == state.meta.getattro
                && same_raw(
                    &state.meta.resolver.namespace,
                    "_registry",
                    &state.meta.resolver_registry,
                    py,
                )?
                && resolver.getattr("_registry")?.is(state.registry.bind(py))
                && state.resources.is_some()
                && state.anchors.is_some()
        } else {
            false
        };
        if !owned {
            state.opaque();
        }
        self.put(state);
        result.map(|result| (result.unbind(), owned))
    }
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Some(state) = self.state.borrow().as_ref() {
            state.visit(&visit)?;
        }
        Ok(())
    }
    fn __clear__(slf: &Bound<'_, Self>) {
        let old = {
            let holder = slf.borrow();
            let old = holder.state.borrow_mut().take();
            old
        };
        drop(old);
    }
}

impl RegistryState {
    fn observed_lookup(
        &self,
        py: Python<'_>,
        uri: &Bound<'_, PyAny>,
        name: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Object> {
        let registry = self.registry(py)?;
        let registry = registry.bind(py);
        let method = registry.getattr(if name.is_some() {
            "anchor"
        } else {
            "get_or_retrieve"
        })?;
        if let Some(view) = self.view(py)? {
            if let Some(value) = view.retrieved(registry, &method, uri, name)? {
                return Ok(value);
            }
        } else {
            opaque_lookup();
        }
        let value = if let Some(name) = name {
            method.call1((uri, name))?
        } else {
            method.call1((uri,))?
        };
        Ok(value.unbind())
    }
}

pub(super) struct View {
    registry: Object,
    resources: Map,
    anchors: Map,
    meta: MetadataHandle,
}
pub(super) fn opaque_lookup() {
    OPAQUE.fetch_add(1, Ordering::Relaxed);
}
impl View {
    pub(super) fn matches(&self, registry: &Bound<'_, PyAny>) -> bool {
        self.registry.bind(registry.py()).is(registry)
    }
    /// Pure eligibility prefix, then Rust lookup. A miss has performed no
    /// callback; the already-bound original method can be invoked once.
    pub(super) fn retrieved(
        &self,
        registry: &Bound<'_, PyAny>,
        method: &Bound<'_, PyAny>,
        uri: &Bound<'_, PyAny>,
        name: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Option<Object>> {
        let py = registry.py();
        let anchor = name.is_some();
        let operation = if anchor { "anchor" } else { "get_or_retrieve" };
        let map = if anchor {
            &self.anchors
        } else {
            &self.resources
        };
        let member = if anchor { "_anchors" } else { "_resources" };
        // Only the member being skipped is relevant on this hot path.
        let descriptor = self
            .meta
            .members
            .iter()
            .find(|(key, _)| *key == member)
            .unwrap();
        if !self.matches(registry)
            || !registry.get_type().is(self.meta.registry.class.bind(py))
            || slot(registry.get_type().as_any(), ffi::Py_tp_getattro) != self.meta.getattro
            || !same_raw(&self.meta.registry.namespace, member, &descriptor.1, py)?
            || !self.meta.method(method, registry, operation)?
            || !self.meta.map_plain(map.actual.bind(py))
            || !same_raw(&self.meta.map_namespace, "get", &self.meta.map_get, py)?
        {
            OPAQUE.fetch_add(1, Ordering::Relaxed);
            return Ok(None);
        }
        let Some(uri_key) = text(uri) else {
            OPAQUE.fetch_add(1, Ordering::Relaxed);
            return Ok(None);
        };
        let lookup = if let Some(name) = name {
            let Some(name) = text(name) else {
                OPAQUE.fetch_add(1, Ordering::Relaxed);
                return Ok(None);
            };
            Key::Anchor(uri_key, name)
        } else {
            Key::Resource(uri_key)
        };
        if !registry.getattr(member)?.is(map.actual.bind(py)) {
            OPAQUE.fetch_add(1, Ordering::Relaxed);
            return Ok(None);
        }
        let found = { map.rows.borrow(py).get(py, &lookup) };
        let Some(value) = found else {
            return Ok(None);
        };
        if value.bind(py).is_none() {
            return Ok(None);
        }
        if anchor {
            ANCHOR_HITS.fetch_add(1, Ordering::Relaxed);
        } else {
            RESOURCE_HITS.fetch_add(1, Ordering::Relaxed);
        }
        // Preserve the actual source constructor and later public field reads.
        // Its binding comes from this already-selected function's globals.
        let function = method.getattr("__func__")?;
        let globals = function.getattr("__globals__")?;
        let constructor = match globals
            .cast::<PyDict>()?
            .get_item(pyo3::intern!(py, "Retrieved"))?
        {
            Some(constructor) => constructor,
            None => {
                let error =
                    pyo3::exceptions::PyNameError::new_err("name 'Retrieved' is not defined");
                error
                    .value(py)
                    .setattr("name", pyo3::intern!(py, "Retrieved"))?;
                return Err(error);
            }
        };
        let kwargs = PyDict::new(py);
        if anchor {
            kwargs.set_item("value", value.bind(py))?;
            kwargs.set_item("registry", registry)?;
        } else {
            kwargs.set_item("registry", registry)?;
            kwargs.set_item("value", value.bind(py))?;
        }
        let retrieved = constructor.call((), Some(&kwargs))?;
        Ok(Some(retrieved.unbind()))
    }
}

#[pyfunction]
fn app_registry_policy(py: Python<'_>) -> PyResult<RegistryPolicy> {
    let meta = META.get_or_try_init(py, || {
        Py::new(
            py,
            RegistryMetadata {
                value: Some(Arc::new(Metadata::capture(py)?)),
            },
        )
    })?;
    // The canonical holder is a real external Python root. Each independent
    // policy owns a separately refcounted metadata payload, so its holder can
    // be reclaimed without pinning policy/State cycles through that root.
    let value = meta
        .borrow(py)
        .value
        .as_ref()
        .ok_or_else(|| PyRuntimeError::new_err("canonical registry metadata was cleared"))?
        .clone_ref(py);
    Ok(RegistryPolicy {
        meta: Some(Py::new(
            py,
            RegistryMetadata {
                value: Some(Arc::new(value)),
            },
        )?),
    })
}
#[pyfunction]
fn app_registry_counts(py: Python<'_>) -> PyResult<Py<PyDict>> {
    let output = PyDict::new(py);
    output.set_item(
        "primary_resource_hits",
        RESOURCE_HITS.load(Ordering::Relaxed),
    )?;
    output.set_item("primary_anchor_hits", ANCHOR_HITS.load(Ordering::Relaxed))?;
    output.set_item("opaque_lookups", OPAQUE.load(Ordering::Relaxed))?;
    Ok(output.unbind())
}
pub(super) fn register(module: &Bound<'_, pyo3::types::PyModule>) -> PyResult<()> {
    module.add_class::<RegistryPolicy>()?;
    module.add_class::<RegistryState>()?;
    module.add_function(wrap_pyfunction!(app_registry_policy, module)?)?;
    module.add_function(wrap_pyfunction!(app_registry_counts, module)?)?;
    Ok(())
}
