//! App-only ASCII URI operation derived from CPython 3.13.16 urllib.parse.
//! No URI answers are cached. Unsupported inputs/configuration select the real
//! Python operation before parsing; native failures never retry in Python.
use super::Object;
use crate::keywords::{builtin, module_member};
use pyo3::{
    exceptions::{PyNameError, PyValueError},
    gc::{PyTraverseError, PyVisit},
    prelude::*,
    types::{PyDict, PyList, PyString},
};
use std::{
    borrow::Cow,
    sync::atomic::{AtomicUsize, Ordering},
};

static NATIVE: AtomicUsize = AtomicUsize::new(0);
static BOUNDARY: AtomicUsize = AtomicUsize::new(0);

enum Read {
    Dict,
    Attribute,
    Proxy,
}
struct Check {
    owner: Object,
    key: Object,
    expected: Object,
    read: Read,
}
struct ListCheck {
    key: Object,
    entries: Vec<Object>,
}

pub(super) struct UriPolicy {
    core: Object,
    builtins: Object,
    globals: Object,
    join: Object,
    defrag: Object,
    absent: Object,
    checks: Vec<Check>,
    lists: Vec<ListCheck>,
    relative: Vec<String>,
    netloc: Vec<String>,
    params: Vec<String>,
    scheme_chars: String,
    controls: String,
    unsafe_bytes: Vec<String>,
}
impl UriPolicy {
    pub(super) fn capture(py: Python<'_>) -> PyResult<Self> {
        let core = py.import("referencing._core")?;
        let module = py.import("urllib.parse")?;
        let globals = module.dict();
        let absent = builtin(py, "object")?.call0()?;
        let mut checks = Vec::new();
        let function_type = module_member(py, "types", "FunctionType")?;
        let mut defaults = |value: &Bound<'_, PyAny>| -> PyResult<()> {
            if value.get_type().is(&function_type) {
                checks.push(Check {
                    owner: value.clone().unbind(),
                    key: PyString::new(py, "__defaults__").into_any().unbind(),
                    expected: value.getattr("__defaults__")?.unbind(),
                    read: Read::Attribute,
                });
            }
            Ok(())
        };
        for name in [
            "urlparse",
            "urlsplit",
            "urlunparse",
            "urlunsplit",
            "_coerce_args",
            "_noop",
            "_splitparams",
            "_splitnetloc",
            "_checknetloc",
            "ParseResult",
            "SplitResult",
            "DefragResult",
            "scheme_chars",
            "_WHATWG_C0_CONTROL_OR_SPACE",
        ] {
            let value = module.getattr(name)?;
            defaults(&value)?;
        }
        // Finish the closure borrow before adding other metadata rows.
        drop(defaults);
        for name in [
            "urlparse",
            "urlsplit",
            "urlunparse",
            "urlunsplit",
            "_coerce_args",
            "_noop",
            "_splitparams",
            "_splitnetloc",
            "_checknetloc",
            "ParseResult",
            "SplitResult",
            "DefragResult",
            "scheme_chars",
            "_WHATWG_C0_CONTROL_OR_SPACE",
        ] {
            checks.push(Check {
                owner: globals.clone().into_any().unbind(),
                key: PyString::new(py, name).into_any().unbind(),
                expected: module.getattr(name)?.unbind(),
                read: Read::Dict,
            });
        }
        let split = module.getattr("urlsplit")?;
        let wrapped = split.getattr("__wrapped__")?;
        checks.push(Check {
            owner: split.unbind(),
            key: PyString::new(py, "__wrapped__").into_any().unbind(),
            expected: wrapped.clone().unbind(),
            read: Read::Attribute,
        });
        checks.push(Check {
            owner: wrapped.clone().unbind(),
            key: PyString::new(py, "__defaults__").into_any().unbind(),
            expected: wrapped.getattr("__defaults__")?.unbind(),
            read: Read::Attribute,
        });
        for name in ["urljoin", "urldefrag"] {
            let value = core.getattr(name)?;
            checks.push(Check {
                owner: value.clone().unbind(),
                key: PyString::new(py, "__defaults__").into_any().unbind(),
                expected: value.getattr("__defaults__")?.unbind(),
                read: Read::Attribute,
            });
        }
        // Raw dictionaries inspect constructor/iteration changes without
        // invoking a newly installed descriptor during the compatibility guard.
        for name in ["ParseResult", "SplitResult", "DefragResult"] {
            let class = module.getattr(name)?;
            let constructor = class.getattr("__new__")?;
            if constructor.get_type().is(&function_type) {
                checks.push(Check {
                    owner: constructor.clone().unbind(),
                    key: PyString::new(py, "__defaults__").into_any().unbind(),
                    expected: constructor.getattr("__defaults__")?.unbind(),
                    read: Read::Attribute,
                });
            }
            let mro = class.getattr("__mro__")?;
            checks.push(Check {
                owner: class.unbind(),
                key: PyString::new(py, "__mro__").into_any().unbind(),
                expected: mro.clone().unbind(),
                read: Read::Attribute,
            });
            for base in mro.try_iter()? {
                let dictionary = base?.getattr("__dict__")?;
                for key in ["__new__", "__init__", "__iter__"] {
                    let key = PyString::new(py, key).into_any();
                    let expected = dictionary.call_method1("get", (&key, &absent))?;
                    checks.push(Check {
                        owner: dictionary.clone().unbind(),
                        key: key.unbind(),
                        expected: expected.unbind(),
                        read: Read::Proxy,
                    });
                }
            }
        }
        let mut lists = Vec::new();
        for name in [
            "uses_relative",
            "uses_netloc",
            "uses_params",
            "_UNSAFE_URL_BYTES_TO_REMOVE",
        ] {
            let list = module.getattr(name)?;
            if !list.is_exact_instance_of::<PyList>() {
                return Err(PyValueError::new_err("canonical URI list differs"));
            }
            let entries = list.cast::<PyList>()?.iter().map(|v| v.unbind()).collect();
            lists.push(ListCheck {
                key: PyString::new(py, name).into_any().unbind(),
                entries,
            });
        }
        Ok(Self {
            builtins: core
                .getattr("Resolver")?
                .getattr("lookup")?
                .getattr("__builtins__")?
                .unbind(),
            core: core.dict().into_any().unbind(),
            globals: globals.into_any().unbind(),
            join: core.getattr("urljoin")?.unbind(),
            defrag: core.getattr("urldefrag")?.unbind(),
            relative: module.getattr("uses_relative")?.extract()?,
            netloc: module.getattr("uses_netloc")?.extract()?,
            params: module.getattr("uses_params")?.extract()?,
            scheme_chars: module.getattr("scheme_chars")?.extract()?,
            controls: module.getattr("_WHATWG_C0_CONTROL_OR_SPACE")?.extract()?,
            unsafe_bytes: module.getattr("_UNSAFE_URL_BYTES_TO_REMOVE")?.extract()?,
            absent: absent.unbind(),
            checks,
            lists,
        })
    }
    pub(super) fn clone_ref(&self, py: Python<'_>) -> Self {
        Self {
            core: self.core.clone_ref(py),
            builtins: self.builtins.clone_ref(py),
            globals: self.globals.clone_ref(py),
            join: self.join.clone_ref(py),
            defrag: self.defrag.clone_ref(py),
            absent: self.absent.clone_ref(py),
            checks: self
                .checks
                .iter()
                .map(|c| Check {
                    owner: c.owner.clone_ref(py),
                    key: c.key.clone_ref(py),
                    expected: c.expected.clone_ref(py),
                    read: match c.read {
                        Read::Dict => Read::Dict,
                        Read::Attribute => Read::Attribute,
                        Read::Proxy => Read::Proxy,
                    },
                })
                .collect(),
            lists: self
                .lists
                .iter()
                .map(|c| ListCheck {
                    key: c.key.clone_ref(py),
                    entries: c.entries.iter().map(|v| v.clone_ref(py)).collect(),
                })
                .collect(),
            relative: self.relative.clone(),
            netloc: self.netloc.clone(),
            params: self.params.clone(),
            scheme_chars: self.scheme_chars.clone(),
            controls: self.controls.clone(),
            unsafe_bytes: self.unsafe_bytes.clone(),
        }
    }
    pub(super) fn visit(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        for value in [
            &self.core,
            &self.builtins,
            &self.globals,
            &self.join,
            &self.defrag,
            &self.absent,
        ] {
            visit.call(value)?;
        }
        for c in &self.checks {
            for v in [&c.owner, &c.key, &c.expected] {
                visit.call(v)?;
            }
        }
        for c in &self.lists {
            visit.call(&c.key)?;
            for v in &c.entries {
                visit.call(v)?;
            }
        }
        Ok(())
    }
    fn selected<'py>(&self, py: Python<'py>, name: &str) -> PyResult<Bound<'py, PyAny>> {
        if let Some(value) = self.core.bind(py).cast::<PyDict>()?.get_item(name)? {
            return Ok(value);
        }
        if let Some(value) = self.builtins.bind(py).cast::<PyDict>()?.get_item(name)? {
            return Ok(value);
        }
        Err(PyNameError::new_err(format!(
            "name '{name}' is not defined"
        )))
    }
    pub(super) fn actual_join<'py>(
        &self,
        py: Python<'py>,
        base: &Bound<'_, PyAny>,
        reference: &Bound<'_, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        self.selected(py, "urljoin")?.call1((base, reference))
    }
    fn supported(
        &self,
        py: Python<'_>,
        base: &Bound<'_, PyAny>,
        reference: &Bound<'_, PyAny>,
        join: &Bound<'_, PyAny>,
        defrag: &Bound<'_, PyAny>,
    ) -> PyResult<bool> {
        if !join.is(self.join.bind(py))
            || !defrag.is(self.defrag.bind(py))
            || !base.is_exact_instance_of::<PyString>()
            || !reference.is_exact_instance_of::<PyString>()
        {
            return Ok(false);
        }
        if !base.call_method0("isascii")?.is_truthy()?
            || !reference.call_method0("isascii")?.is_truthy()?
        {
            return Ok(false);
        }
        for v in [base, reference] {
            if v.cast::<PyString>()?.to_str()?.contains(['[', ']']) {
                return Ok(false);
            }
        }
        // A scheme-less base can make CPython's single-element join/unparse
        // return an existing input object. Select the whole Python operation
        // before doing URI work until those additional alias paths are ported.
        let text = base.cast::<PyString>()?.to_str()?;
        if !text.is_empty()
            && !text.split_once(':').is_some_and(|(scheme, _)| {
                scheme
                    .as_bytes()
                    .first()
                    .is_some_and(u8::is_ascii_alphabetic)
                    && scheme
                        .bytes()
                        .all(|b| b.is_ascii_alphanumeric() || b"+.-".contains(&b))
            })
        {
            return Ok(false);
        }
        for c in &self.checks {
            let actual = match c.read {
                Read::Dict => c
                    .owner
                    .bind(py)
                    .cast::<PyDict>()?
                    .get_item(c.key.bind(py))?,
                Read::Attribute => Some(
                    c.owner
                        .bind(py)
                        .getattr(c.key.bind(py).cast::<PyString>()?)?,
                ),
                Read::Proxy => Some(
                    c.owner
                        .bind(py)
                        .call_method1("get", (c.key.bind(py), self.absent.bind(py)))?,
                ),
            };
            if !actual.is_some_and(|v| v.is(c.expected.bind(py))) {
                return Ok(false);
            }
        }
        for c in &self.lists {
            let Some(value) = self
                .globals
                .bind(py)
                .cast::<PyDict>()?
                .get_item(c.key.bind(py))?
            else {
                return Ok(false);
            };
            if !value.is_exact_instance_of::<PyList>() {
                return Ok(false);
            }
            let list = value.cast::<PyList>()?;
            if list.len() != c.entries.len() {
                return Ok(false);
            }
            for (actual, expected) in list.iter().zip(&c.entries) {
                if !actual.is(expected.bind(py)) {
                    return Ok(false);
                }
            }
        }
        Ok(true)
    }
    pub(super) fn resolve(
        &self,
        py: Python<'_>,
        base: &Bound<'_, PyAny>,
        reference: &Bound<'_, PyAny>,
    ) -> PyResult<(Object, Object)> {
        if reference.call_method1("startswith", ("#",))?.is_truthy()? {
            return Ok((
                base.clone().unbind(),
                reference
                    .get_item(builtin(py, "slice")?.call1((1, py.None()))?)?
                    .unbind(),
            ));
        }
        // Match LOAD_GLOBAL/evaluation order: choose the outer callable first.
        let defrag = self.selected(py, "urldefrag")?;
        let join = self.selected(py, "urljoin")?;
        if !self.supported(py, base, reference, &join, &defrag)? {
            BOUNDARY.fetch_add(1, Ordering::Relaxed);
            return unpack_two(defrag.call1((join.call1((base, reference))?,))?);
        }
        NATIVE.fetch_add(1, Ordering::Relaxed);
        let b = base.cast::<PyString>()?.to_str()?;
        let r = reference.cast::<PyString>()?.to_str()?;
        let joined = self.join_ascii(b, r);
        let text = match &joined {
            Joined::Base => b,
            Joined::Reference => r,
            Joined::Owned(v) => v,
        };
        if !text.contains('#') {
            let uri = match joined {
                Joined::Base => base.clone().unbind(),
                Joined::Reference => reference.clone().unbind(),
                Joined::Owned(v) => PyString::new(py, &v).into_any().unbind(),
            };
            return Ok((uri, PyString::new(py, "").into_any().unbind()));
        }
        let cleaned = self.clean(text);
        let mut parsed = self.parse(&cleaned, "");
        let fragment = PyString::new(py, parsed.fragment).into_any().unbind();
        parsed.fragment = "";
        Ok((
            PyString::new(py, &self.unparse(&parsed))
                .into_any()
                .unbind(),
            fragment,
        ))
    }
    fn clean<'a>(&self, text: &'a str) -> Cow<'a, str> {
        let text = text.trim_start_matches(|c| self.controls.contains(c));
        if self.unsafe_bytes.iter().any(|p| text.contains(p)) {
            let mut owned = text.to_owned();
            for p in &self.unsafe_bytes {
                owned = owned.replace(p, "");
            }
            Cow::Owned(owned)
        } else {
            Cow::Borrowed(text)
        }
    }
    fn parse<'a>(&self, mut text: &'a str, default: &'a str) -> Parts<'a> {
        let mut scheme = Cow::Borrowed(default);
        if let Some(i) = text.find(':') {
            if i > 0
                && text.as_bytes()[0].is_ascii_alphabetic()
                && text[..i].chars().all(|c| self.scheme_chars.contains(c))
            {
                let raw = &text[..i];
                scheme = if raw.bytes().any(|b| b.is_ascii_uppercase()) {
                    Cow::Owned(raw.to_ascii_lowercase())
                } else {
                    Cow::Borrowed(raw)
                };
                text = &text[i + 1..];
            }
        }
        let mut netloc = "";
        if text.starts_with("//") {
            let end = text[2..]
                .find(['/', '?', '#'])
                .map_or(text.len(), |i| i + 2);
            netloc = &text[2..end];
            text = &text[end..];
        }
        let (text, fragment) = text.split_once('#').unwrap_or((text, ""));
        let (mut path, query) = text.split_once('?').unwrap_or((text, ""));
        let mut params = "";
        if self.params.iter().any(|s| s == scheme.as_ref()) {
            let start = path.rfind('/').unwrap_or(0);
            if let Some(i) = path[start..].find(';') {
                let i = start + i;
                params = &path[i + 1..];
                path = &path[..i];
            }
        }
        Parts {
            scheme,
            netloc,
            path,
            params,
            query,
            fragment,
        }
    }
    fn unparse(&self, p: &Parts<'_>) -> String {
        let mut path = p.path.to_owned();
        if !p.params.is_empty() {
            path.push(';');
            path.push_str(p.params);
        }
        let mut result = String::new();
        if !p.scheme.is_empty() {
            result.push_str(&p.scheme);
            result.push(':');
        }
        if !p.netloc.is_empty() {
            result.push_str("//");
            result.push_str(p.netloc);
            if !path.is_empty() && !path.starts_with('/') {
                result.push('/');
            }
        } else if path.starts_with("//")
            || (!p.scheme.is_empty()
                && self.netloc.iter().any(|s| s == p.scheme.as_ref())
                && (path.is_empty() || path.starts_with('/')))
        {
            result.push_str("//");
        }
        result.push_str(&path);
        if !p.query.is_empty() {
            result.push('?');
            result.push_str(p.query);
        }
        if !p.fragment.is_empty() {
            result.push('#');
            result.push_str(p.fragment);
        }
        result
    }
    fn join_ascii(&self, base: &str, reference: &str) -> Joined {
        if base.is_empty() {
            return Joined::Reference;
        }
        if reference.is_empty() {
            return Joined::Base;
        }
        let bclean = self.clean(base);
        let rclean = self.clean(reference);
        let b = self.parse(&bclean, "");
        let mut r = self.parse(&rclean, &b.scheme);
        if r.scheme != b.scheme || !self.relative.iter().any(|s| s == r.scheme.as_ref()) {
            return Joined::Reference;
        }
        if self.netloc.iter().any(|s| s == r.scheme.as_ref()) {
            if !r.netloc.is_empty() {
                return Joined::Owned(self.unparse(&r));
            }
            r.netloc = b.netloc;
        }
        if r.path.is_empty() && r.params.is_empty() {
            r.path = b.path;
            r.params = b.params;
            if r.query.is_empty() {
                r.query = b.query;
            }
            return Joined::Owned(self.unparse(&r));
        }
        let mut segments: Vec<&str> = if r.path.starts_with('/') {
            r.path.split('/').collect()
        } else {
            let mut parts: Vec<_> = b.path.split('/').collect();
            if parts.last().is_some_and(|s| !s.is_empty()) {
                parts.pop();
            }
            parts.extend(r.path.split('/'));
            let last = parts.len().saturating_sub(1);
            parts
                .into_iter()
                .enumerate()
                .filter_map(|(i, s)| (i == 0 || i == last || !s.is_empty()).then_some(s))
                .collect()
        };
        let trailing = segments.last().is_some_and(|s| *s == "." || *s == "..");
        let mut resolved = Vec::new();
        for s in segments.drain(..) {
            match s {
                ".." => {
                    resolved.pop();
                }
                "." => {}
                _ => resolved.push(s),
            }
        }
        if trailing {
            resolved.push("");
        }
        let path = resolved.join("/");
        let output = Parts {
            scheme: r.scheme,
            netloc: r.netloc,
            path: if path.is_empty() { "/" } else { &path },
            params: r.params,
            query: r.query,
            fragment: r.fragment,
        };
        Joined::Owned(self.unparse(&output))
    }
}
struct Parts<'a> {
    scheme: Cow<'a, str>,
    netloc: &'a str,
    path: &'a str,
    params: &'a str,
    query: &'a str,
    fragment: &'a str,
}
enum Joined {
    Base,
    Reference,
    Owned(String),
}

fn unpack_two(value: Bound<'_, PyAny>) -> PyResult<(Object, Object)> {
    let mut iter = value.try_iter()?;
    let a = iter
        .next()
        .transpose()?
        .ok_or_else(|| PyValueError::new_err("not enough values to unpack (expected 2, got 0)"))?;
    let b = iter
        .next()
        .transpose()?
        .ok_or_else(|| PyValueError::new_err("not enough values to unpack (expected 2, got 1)"))?;
    if iter.next().transpose()?.is_some() {
        return Err(PyValueError::new_err(
            "too many values to unpack (expected 2)",
        ));
    }
    Ok((a.unbind(), b.unbind()))
}
pub(super) fn counts() -> (usize, usize) {
    (
        NATIVE.load(Ordering::Relaxed),
        BOUNDARY.load(Ordering::Relaxed),
    )
}
