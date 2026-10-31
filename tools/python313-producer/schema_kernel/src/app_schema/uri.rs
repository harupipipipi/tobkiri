//! App-owned URI DATA operations, independent of the shared urllib helpers.
//!
//! Derived from CPython 3.13.16 Lib/urllib/parse.py (PSF-2.0; see
//! crate-root COPYING.python). This is its join/defrag behavior, not a general
//! URI normalizer. Unsupported inputs choose the original sequence before any
//! native URI operation, and failures never cause a second execution.
use crate::keywords::module_member;
use pyo3::{
    exceptions::PyUnicodeEncodeError,
    prelude::*,
    types::{PyDict, PyModule, PyString},
};
use std::{
    borrow::Cow,
    sync::atomic::{AtomicUsize, Ordering},
};

static JOIN_DEFRAGS: AtomicUsize = AtomicUsize::new(0);
static JOIN_ONLY: AtomicUsize = AtomicUsize::new(0);
static RESOLVE_BOUNDARIES: AtomicUsize = AtomicUsize::new(0);
static JOIN_BOUNDARIES: AtomicUsize = AtomicUsize::new(0);

/// Return borrowed exact ASCII arguments only. This admission scan performs no
/// URI parse or foreign protocol operation. Bracketed/Unicode authorities keep
/// their original validators; scheme-less bases keep original parser-cache and
/// singleton-path identity behavior. The empty-base raw return is supported.
fn admitted<'a, 'py>(
    base: &'a Bound<'py, PyAny>,
    reference: &'a Bound<'py, PyAny>,
) -> PyResult<Option<(&'a str, &'a str)>> {
    if !base.is_exact_instance_of::<PyString>() || !reference.is_exact_instance_of::<PyString>() {
        return Ok(None);
    }
    let text = |value: &'a Bound<'py, PyAny>| -> PyResult<Option<&'a str>> {
        match value.cast::<PyString>()?.to_str() {
            Ok(text) => Ok(text.is_ascii().then_some(text)),
            Err(error) if error.is_instance_of::<PyUnicodeEncodeError>(value.py()) => Ok(None),
            Err(error) => Err(error),
        }
    };
    let (Some(base), Some(reference)) = (text(base)?, text(reference)?) else {
        return Ok(None);
    };
    if base.contains(['[', ']']) || reference.contains(['[', ']']) {
        return Ok(None);
    }
    if !base.is_empty() && scheme_end(base).is_none() {
        return Ok(None);
    }
    Ok(Some((base, reference)))
}

fn scheme_end(value: &str) -> Option<usize> {
    let end = value.find(':')?;
    let scheme = &value.as_bytes()[..end];
    if !scheme.first()?.is_ascii_alphabetic()
        || !scheme
            .iter()
            .all(|value| value.is_ascii_alphanumeric() || matches!(*value, b'+' | b'-' | b'.'))
    {
        return None;
    }
    Some(end)
}

fn uses_relative(scheme: &str) -> bool {
    matches!(
        scheme,
        "" | "ftp"
            | "http"
            | "gopher"
            | "nntp"
            | "imap"
            | "wais"
            | "file"
            | "https"
            | "shttp"
            | "mms"
            | "prospero"
            | "rtsp"
            | "rtsps"
            | "rtspu"
            | "sftp"
            | "svn"
            | "svn+ssh"
            | "ws"
            | "wss"
    )
}

/// Unlike join-only, defrag exposes the shared urlsplit cache's fragment field
/// and may return its path object directly for a raw scheme-less reference.
/// Keep all potentially fragmented sequences on the original path. This
/// decision precedes native URI work; direct fragment-only slicing is separate.
fn resolve_admitted(base: &str, reference: &str) -> bool {
    if reference.starts_with('#') {
        return true;
    }
    if base.contains('#') || reference.contains('#') {
        return false;
    }
    if base.is_empty() {
        return true;
    }
    let end = scheme_end(base).expect("raw scheme established by argument admission");
    uses_relative(&base[..end].to_ascii_lowercase())
}

fn uses_netloc(scheme: &str) -> bool {
    matches!(
        scheme,
        "" | "ftp"
            | "http"
            | "gopher"
            | "nntp"
            | "telnet"
            | "imap"
            | "wais"
            | "file"
            | "mms"
            | "https"
            | "shttp"
            | "snews"
            | "prospero"
            | "rtsp"
            | "rtsps"
            | "rtspu"
            | "rsync"
            | "svn"
            | "svn+ssh"
            | "sftp"
            | "nfs"
            | "git"
            | "git+ssh"
            | "ws"
            | "wss"
            | "itms-services"
    )
}

fn uses_params(scheme: &str) -> bool {
    matches!(
        scheme,
        "" | "ftp"
            | "hdl"
            | "prospero"
            | "http"
            | "imap"
            | "https"
            | "shttp"
            | "rtsp"
            | "rtsps"
            | "rtspu"
            | "sip"
            | "sips"
            | "mms"
            | "sftp"
            | "tel"
    )
}

/// urlsplit strips leading C0/space, preserves trailing spaces, and removes
/// tabs/CR/LF everywhere. Percent escapes and all remaining bytes stay literal.
fn clean(value: &str) -> Cow<'_, str> {
    let value = value.trim_start_matches(|value: char| value <= ' ');
    if value.contains(['\t', '\r', '\n']) {
        Cow::Owned(
            value
                .chars()
                .filter(|value| !matches!(*value, '\t' | '\r' | '\n'))
                .collect(),
        )
    } else {
        Cow::Borrowed(value)
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

/// Inputs were admitted before reaching this parser. In particular neither
/// bracket/IP validation nor Unicode NFKC validation is replaced here.
fn parse<'a>(mut value: &'a str, default_scheme: &'a str) -> Parts<'a> {
    let scheme = if let Some(end) = scheme_end(value) {
        let scheme = Cow::Owned(value[..end].to_ascii_lowercase());
        value = &value[end + 1..];
        scheme
    } else {
        Cow::Borrowed(default_scheme)
    };
    let netloc = if let Some(rest) = value.strip_prefix("//") {
        let end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
        value = &rest[end..];
        &rest[..end]
    } else {
        ""
    };
    let (value, fragment) = value.split_once('#').unwrap_or((value, ""));
    let (path, query) = value.split_once('?').unwrap_or((value, ""));
    let (path, params) = if uses_params(&scheme) {
        let start = path.rfind('/').unwrap_or(0);
        if let Some(end) = path[start..].find(';').map(|offset| start + offset) {
            (&path[..end], &path[end + 1..])
        } else {
            (path, "")
        }
    } else {
        (path, "")
    };
    Parts {
        scheme,
        netloc,
        path,
        params,
        query,
        fragment,
    }
}

fn unparse(
    scheme: &str,
    netloc: &str,
    path: &str,
    params: &str,
    query: &str,
    fragment: &str,
) -> String {
    let path = if params.is_empty() {
        Cow::Borrowed(path)
    } else {
        Cow::Owned(format!("{path};{params}"))
    };
    let mut value = String::new();
    if !scheme.is_empty() {
        value.push_str(scheme);
        value.push(':');
    }
    if !netloc.is_empty() {
        value.push_str("//");
        value.push_str(netloc);
        if !path.is_empty() && !path.starts_with('/') {
            value.push('/');
        }
    } else if path.starts_with("//")
        || (!scheme.is_empty() && uses_netloc(scheme) && (path.is_empty() || path.starts_with('/')))
    {
        value.push_str("//");
    }
    value.push_str(&path);
    if !query.is_empty() {
        value.push('?');
        value.push_str(query);
    }
    if !fragment.is_empty() {
        value.push('#');
        value.push_str(fragment);
    }
    value
}

enum Joined {
    Base,
    Reference,
    Built(String),
}
impl Joined {
    fn object<'py>(
        self,
        py: Python<'py>,
        base: &Bound<'py, PyAny>,
        reference: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        match self {
            Self::Base => Ok(base.clone()),
            Self::Reference => Ok(reference.clone()),
            Self::Built(value) => string(py, &value),
        }
    }
}

fn string<'py>(py: Python<'py>, value: &str) -> PyResult<Bound<'py, PyAny>> {
    // Preserve a real allocation error instead of PyString::new's panic.
    PyString::from_bytes(py, value.as_bytes()).map(Bound::into_any)
}

fn join_native(base: &str, reference: &str) -> Joined {
    if base.is_empty() {
        return Joined::Reference;
    }
    if reference.is_empty() {
        return Joined::Base;
    }
    let base_clean = clean(base);
    let base = parse(&base_clean, "");
    let reference_clean = clean(reference);
    let reference = parse(&reference_clean, &base.scheme);
    if reference.scheme != base.scheme || !uses_relative(&reference.scheme) {
        // CPython returns the original argument, including controls/case which
        // were removed only from the temporary parsed view.
        return Joined::Reference;
    }
    let netloc = if uses_netloc(&reference.scheme) {
        if !reference.netloc.is_empty() {
            return Joined::Built(unparse(
                &reference.scheme,
                reference.netloc,
                reference.path,
                reference.params,
                reference.query,
                reference.fragment,
            ));
        }
        base.netloc
    } else {
        reference.netloc
    };
    if reference.path.is_empty() && reference.params.is_empty() {
        return Joined::Built(unparse(
            &reference.scheme,
            netloc,
            base.path,
            base.params,
            if reference.query.is_empty() {
                base.query
            } else {
                reference.query
            },
            reference.fragment,
        ));
    }
    let mut base_parts: Vec<_> = base.path.split('/').collect();
    if base_parts.last() != Some(&"") {
        base_parts.pop();
    }
    let segments: Vec<_> = if reference.path.starts_with('/') {
        reference.path.split('/').collect()
    } else {
        base_parts.extend(reference.path.split('/'));
        let last = base_parts.len() - 1;
        base_parts
            .into_iter()
            .enumerate()
            .filter_map(|(index, value)| {
                (index == 0 || index == last || !value.is_empty()).then_some(value)
            })
            .collect()
    };
    let mut resolved = Vec::with_capacity(segments.len());
    for segment in &segments {
        match *segment {
            ".." => {
                resolved.pop();
            }
            "." => {}
            value => resolved.push(value),
        }
    }
    if segments
        .last()
        .is_some_and(|value| matches!(*value, "." | ".."))
    {
        resolved.push("");
    }
    let path = resolved.join("/");
    Joined::Built(unparse(
        &reference.scheme,
        netloc,
        if path.is_empty() { "/" } else { &path },
        reference.params,
        reference.query,
        reference.fragment,
    ))
}

/// Compute only the owned lookup's URI interval. Registry/Resource algorithms
/// and opaque resolver dispatch remain with ReferenceState and its boundaries.
pub(super) fn resolve<'py>(
    py: Python<'py>,
    base: &Bound<'py, PyAny>,
    reference: &Bound<'py, PyAny>,
) -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyAny>)> {
    let Some((base_text, reference_text)) =
        admitted(base, reference)?.filter(|(base, reference)| resolve_admitted(base, reference))
    else {
        RESOLVE_BOUNDARIES.fetch_add(1, Ordering::Relaxed);
        let pair = module_member(py, "app_schema_model", "uri_resolve_boundary")?
            .call1((base, reference))?;
        // The source-defined helper has already performed the original actual
        // two-item unpack; its result here is its own exact tuple.
        return Ok((pair.get_item(0)?, pair.get_item(1)?));
    };
    if let Some(fragment) = reference_text.strip_prefix('#') {
        // The source bypasses both helpers and preserves the actual base handle.
        return Ok((base.clone(), string(py, fragment)?));
    }
    JOIN_DEFRAGS.fetch_add(1, Ordering::Relaxed);
    let joined = join_native(base_text, reference_text);
    // Admission guarantees neither argument can contribute a literal fragment.
    // Source urldefrag therefore returns the joined object itself and ''.
    Ok((joined.object(py, base, reference)?, string(py, "")?))
}

/// Join only after the actual owned in_resource operation has obtained its ID.
pub(super) fn join<'py>(
    py: Python<'py>,
    base: &Bound<'py, PyAny>,
    reference: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let Some((base_text, reference_text)) = admitted(base, reference)? else {
        JOIN_BOUNDARIES.fetch_add(1, Ordering::Relaxed);
        return module_member(py, "app_schema_model", "uri_join_boundary")?
            .call1((base, reference));
    };
    JOIN_ONLY.fetch_add(1, Ordering::Relaxed);
    join_native(base_text, reference_text).object(py, base, reference)
}

#[pyfunction]
fn app_uri_counts(py: Python<'_>) -> PyResult<Bound<'_, PyDict>> {
    let result = PyDict::new(py);
    result.set_item("native_join_defrags", JOIN_DEFRAGS.load(Ordering::Relaxed))?;
    result.set_item("native_join_only", JOIN_ONLY.load(Ordering::Relaxed))?;
    result.set_item(
        "python_resolve_boundaries",
        RESOLVE_BOUNDARIES.load(Ordering::Relaxed),
    )?;
    result.set_item(
        "python_join_boundaries",
        JOIN_BOUNDARIES.load(Ordering::Relaxed),
    )?;
    Ok(result)
}

pub(super) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(app_uri_counts, module)?)?;
    Ok(())
}
