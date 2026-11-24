//! Fixed APP projection of causes produced by the native recovery body.
//! No Guest class/ref is constructed. Only the immediate native definitions
//! failure caller may turn this DATA into the Host's explicit-from edge.
use crate::{NativeErrorGraph, NativeErrorKind, OriginalAuthorityCause, OriginalAuthorityError};
use anyhow::{Context, ensure};
use std::sync::Arc;
use tobkiri_canon::{
    AllocationBudget, AllocationKind, AllocationRequest, CoreBuffer, CoreBufferBuilder, MAX_BYTES,
};
use tobkiri_gate::{CapacityKind, NativeSetupInvocation};

#[cfg(all(test, unix))]
#[path = "bootstrap_recovery_error_tests.rs"]
mod tests;

#[cfg(all(
    test,
    feature = "bootstrap-ci",
    target_os = "macos",
    target_arch = "aarch64"
))]
#[path = "bootstrap_recovery_error_actual_ci.rs"]
mod actual_ci;

const MAX_NODES: usize = 64;
const ERROR_TEXT_BYTES: usize = 4096;
pub(super) const MESSAGE: &str = "Profile publication recovery failed closed";
const SECURE: &str = "tobkiri_protocol.secure_persistence.SecurePersistenceError";
const ACTIVE: &str = "core_runtime.active_profile_store_v4.ActiveProfileStoreError";
const INTEGRITY: &str = "core_runtime.active_profile_store_v4.ActiveProfileStoreIntegrityError";
const LOCK_TIMEOUT: &str = "core_runtime.active_profile_store_v4.ActiveProfileStoreLockTimeout";

#[derive(Clone, Copy)]
struct Planned<'a> {
    source: &'a OriginalAuthorityCause,
    kind: NativeErrorKind,
    cause: Option<usize>,
    context: Option<usize>,
    suppressed: bool,
}

fn work(budget: &Arc<dyn AllocationBudget>, count: usize) -> anyhow::Result<()> {
    budget.reserve(AllocationRequest {
        kind: AllocationKind::Temporary,
        bytes: 0,
        elements: 0,
        depth: 0,
        work: u64::try_from(count)?,
    })?;
    Ok(())
}

// Closed CPython OSError(errno, strerror) classes for the actual descriptor/
// lock operations reached here. An unreviewed errno stays a typed request.
#[cfg(unix)]
fn os_kind(errno: i32) -> Option<NativeErrorKind> {
    use NativeErrorKind::*;
    Some(match errno {
        libc::ENOENT => FileNotFoundError,
        libc::EACCES | libc::EPERM => PermissionError,
        libc::ENOTDIR => NotADirectoryError,
        libc::EISDIR => IsADirectoryError,
        libc::EEXIST => FileExistsError,
        libc::EINTR => InterruptedError,
        libc::EAGAIN => BlockingIoError,
        libc::ETIMEDOUT => TimeoutError,
        libc::EDEADLK
        | libc::EBADF
        | libc::EINVAL
        | libc::EIO
        | libc::ENOSPC
        | libc::ELOOP
        | libc::EMFILE
        | libc::ENFILE => OsError,
        _ => return None,
    })
}
#[cfg(not(unix))]
fn os_kind(_: i32) -> Option<NativeErrorKind> {
    None
}

fn planned<'a>(
    source: &'a OriginalAuthorityCause,
    budget: &Arc<dyn AllocationBudget>,
) -> anyhow::Result<Option<Planned<'a>>> {
    use NativeErrorKind::*;
    let (kind, suppressed) = match source {
        OriginalAuthorityCause::Python {
            class,
            message,
            suppressed,
            ..
        } => {
            work(
                budget,
                class.len().saturating_add(message.len()).saturating_add(1),
            )?;
            let kind = match *class {
                SECURE => SecurePersistenceError,
                ACTIVE => ActiveProfileStoreError,
                INTEGRITY => ActiveProfileStoreIntegrityError,
                LOCK_TIMEOUT => ActiveProfileStoreLockTimeout,
                "builtins.ValueError" => ValueError,
                "tobkiri_protocol.errors.ProtocolError" => ProtocolError,
                "tobkiri_protocol.errors.CanonicalizationError" => CanonicalizationError,
                // Registry RuntimeError subclasses, JSON/Unicode errors whose
                // constructor fields are not in this native carrier, and all
                // unknown classes are NOT collapsed to another class.
                _ => return Ok(None),
            };
            (kind, *suppressed)
        }
        OriginalAuthorityCause::Io(io) => {
            work(
                budget,
                io.filename
                    .as_ref()
                    .map_or(1, |path| path.len().saturating_mul(8).saturating_add(1)),
            )?;
            let Some(errno) = io.errno.filter(|value| *value > 0) else {
                return Ok(None);
            };
            if std::io::Error::from_raw_os_error(errno).kind() != io.kind
                || io.filename.as_ref().is_some_and(|name| !name.is_ascii())
            {
                return Ok(None);
            }
            let Some(kind) = os_kind(errno) else {
                return Ok(None);
            };
            (kind, false)
        }
        _ => return Ok(None),
    };
    Ok(Some(Planned {
        source,
        kind,
        cause: None,
        context: None,
        suppressed,
    }))
}

fn number(out: &mut CoreBufferBuilder, mut value: usize) -> anyhow::Result<()> {
    let mut digits = [0_u8; 20];
    let mut start = digits.len();
    loop {
        start -= 1;
        digits[start] = b'0' + (value % 10) as u8;
        value /= 10;
        if value == 0 {
            break;
        }
    }
    out.append(&digits[start..])?;
    Ok(())
}
fn edge(out: &mut CoreBufferBuilder, index: Option<usize>) -> anyhow::Result<()> {
    match index {
        Some(index) => number(out, index)?,
        None => out.append(b"null")?,
    }
    Ok(())
}
fn quoted_path(out: &mut CoreBufferBuilder, path: &str) -> anyhow::Result<()> {
    // The actual native pathname is already ASCII-checked, never recovered
    // from an Error Display string. This is Python's ASCII repr spelling.
    let quote = if path.contains('\'') && !path.contains('"') {
        b'"'
    } else {
        b'\''
    };
    out.append(&[quote])?;
    for byte in path.bytes() {
        match byte {
            b'\\' => out.append(b"\\\\")?,
            b'\n' => out.append(b"\\n")?,
            b'\r' => out.append(b"\\r")?,
            b'\t' => out.append(b"\\t")?,
            byte if byte == quote => out.append(&[b'\\', byte])?,
            byte if byte < 32 || byte == 127 => {
                const HEX: &[u8; 16] = b"0123456789abcdef";
                out.append(&[
                    b'\\',
                    b'x',
                    HEX[usize::from(byte >> 4)],
                    HEX[usize::from(byte & 15)],
                ])?;
            }
            byte => out.append(&[byte])?,
        }
    }
    out.append(&[quote])?;
    Ok(())
}
#[cfg(unix)]
fn os_text(errno: i32, buffer: &mut [u8; ERROR_TEXT_BYTES]) -> anyhow::Result<&str> {
    // Same native strerror source used by CPython, never parsed from Display.
    // Caller consumes this Store's work before buffer init/scan/copy/UTF-8.
    let pointer = unsafe { libc::strerror(errno) };
    ensure!(!pointer.is_null(), "native strerror is unavailable");
    let length = unsafe { libc::strnlen(pointer, buffer.len()) };
    ensure!(
        length < buffer.len(),
        "native strerror exceeds its finite envelope"
    );
    // Copy immediately, as CPython does. No borrowed libc buffer or synthetic
    // 'static string survives another strerror call or escapes in the graph.
    unsafe { std::ptr::copy_nonoverlapping(pointer.cast::<u8>(), buffer.as_mut_ptr(), length) };
    Ok(std::str::from_utf8(&buffer[..length])?)
}
#[cfg(not(unix))]
fn os_text(_: i32, _: &mut [u8; ERROR_TEXT_BYTES]) -> anyhow::Result<&str> {
    anyhow::bail!("native recovery OS class is not bound on this platform")
}

fn message(node: &Planned<'_>, budget: &Arc<dyn AllocationBudget>) -> anyhow::Result<CoreBuffer> {
    let mut out = CoreBuffer::builder(budget, MAX_BYTES);
    match node.source {
        OriginalAuthorityCause::Python { message, .. } => out.append(message.as_bytes())?,
        OriginalAuthorityCause::Io(io) => {
            let errno = io.errno.expect("planned actual errno");
            work(budget, ERROR_TEXT_BYTES * 8)?;
            let mut text = [0_u8; ERROR_TEXT_BYTES];
            out.append(b"[Errno ")?;
            number(&mut out, usize::try_from(errno)?)?;
            out.append(b"] ")?;
            out.append(os_text(errno, &mut text)?.as_bytes())?;
            if let Some(filename) = &io.filename {
                out.append(b": ")?;
                quoted_path(&mut out, filename)?;
            }
        }
        _ => unreachable!("closed planned node"),
    }
    Ok(out.finish()?)
}
fn payload(
    out: &mut CoreBufferBuilder,
    node: &Planned<'_>,
    budget: &Arc<dyn AllocationBudget>,
) -> anyhow::Result<()> {
    out.append(b"\"args\":[")?;
    match node.source {
        OriginalAuthorityCause::Python { message, .. } => {
            crate::bootstrap_metadata::string_parts(out, [message.as_str()])?;
            if node.kind == NativeErrorKind::SecurePersistenceError {
                // Original SecurePersistenceError(message): one argument and
                // unset OSError descriptors, not an invented errno tuple.
                out.append(b"],\"fields\":{\"errno\":null,\"strerror\":null,\"filename\":null,\"filename2\":null}")?;
            } else {
                out.append(b"],\"fields\":{}")?;
            }
        }
        OriginalAuthorityCause::Io(io) => {
            let errno = io.errno.expect("planned actual errno");
            work(budget, ERROR_TEXT_BYTES * 8)?;
            let mut text = [0_u8; ERROR_TEXT_BYTES];
            let text = os_text(errno, &mut text)?;
            number(out, usize::try_from(errno)?)?;
            out.append(b",")?;
            crate::bootstrap_metadata::string_parts(out, [text])?;
            out.append(b"],\"fields\":{\"errno\":")?;
            number(out, usize::try_from(errno)?)?;
            out.append(b",\"strerror\":")?;
            crate::bootstrap_metadata::string_parts(out, [text])?;
            out.append(b",\"filename\":")?;
            match &io.filename {
                Some(name) => crate::bootstrap_metadata::string_parts(out, [name.as_str()])?,
                None => out.append(b"null")?,
            }
            out.append(b",\"filename2\":null}")?;
        }
        _ => unreachable!("closed planned node"),
    }
    Ok(())
}

/// Called only on the immediate typed result of native definitions::run.
/// Unknown/uncaught types remain the prior raw error or UNQUALIFIED denied
/// request. This is no general class-name-to-exception or authority API.
pub(super) fn project(
    error: &OriginalAuthorityError,
    invocation: &NativeSetupInvocation,
    budget: &Arc<dyn AllocationBudget>,
) -> anyhow::Result<Option<NativeErrorGraph>> {
    if error.class != crate::bootstrap_review::DENIED
        || error.message != MESSAGE
        || !error.suppressed
        || error.context.is_some()
    {
        return Ok(None);
    }
    let Some(source) = &error.cause else {
        return Ok(None);
    };
    // Fixed node/edge/index bookkeeping, charged before allocation/traversal;
    // variable source text and final codec work use this same owner below.
    work(budget, MAX_NODES * 128)?;
    // Fund actual scratch in this invocation before allocating/traversing it.
    let bytes = MAX_NODES
        .checked_mul(std::mem::size_of::<Planned<'_>>())
        .and_then(|bytes| bytes.checked_add(ERROR_TEXT_BYTES + 128))
        .context("native recovery graph scratch overflow")?;
    let _capacity = invocation.reserve(CapacityKind::VfsBuffer, u64::try_from(bytes)?)?;
    let mut nodes = Vec::new();
    nodes.try_reserve_exact(MAX_NODES)?;
    ensure!(
        nodes.capacity() == MAX_NODES,
        "native recovery graph scratch capacity differs"
    );
    let Some(root) = planned(source, budget)? else {
        return Ok(None);
    };
    nodes.push(root);
    let mut cursor = 0;
    while cursor < nodes.len() {
        work(budget, 1)?;
        let (cause, context) = match nodes[cursor].source {
            OriginalAuthorityCause::Python { cause, context, .. } => {
                (cause.as_deref(), context.as_deref())
            }
            OriginalAuthorityCause::Io(io) => (None, io.context.as_deref()),
            _ => unreachable!("closed planned node"),
        };
        let mut edges = [None, None];
        for (slot, source) in [cause, context].into_iter().enumerate() {
            if let Some(source) = source {
                if nodes.len() == MAX_NODES {
                    return Ok(None);
                }
                let Some(node) = planned(source, budget)? else {
                    return Ok(None);
                };
                edges[slot] = Some(nodes.len());
                nodes.push(node);
            }
        }
        nodes[cursor].cause = edges[0];
        // `raise ... from cause` also has the same ambient context object.
        // The native source carrier omits that redundant implicit edge.
        nodes[cursor].context = edges[1].or(edges[0]);
        cursor += 1;
    }
    let code = CoreBuffer::copy_from(nodes[0].kind.tag().as_bytes(), budget)?;
    let message = message(&nodes[0], budget)?;
    let mut out = CoreBuffer::builder(budget, MAX_BYTES);
    out.append(
        b"{\"format\":\"tobkiri.native-exception/1\",\"graph_id\":1,\"root\":0,\"nodes\":[",
    )?;
    for (index, node) in nodes.iter().enumerate() {
        if index != 0 {
            out.append(b",")?;
        }
        out.append(b"{\"kind\":")?;
        crate::bootstrap_metadata::string_parts(&mut out, [node.kind.tag()])?;
        out.append(b",")?;
        payload(&mut out, node, budget)?;
        out.append(b",\"cause\":")?;
        edge(&mut out, node.cause)?;
        out.append(b",\"context\":")?;
        edge(&mut out, node.context)?;
        out.append(if node.suppressed {
            b",\"suppress_context\":true}"
        } else {
            b",\"suppress_context\":false}"
        })?;
    }
    out.append(b"]}")?;
    Ok(Some(NativeErrorGraph::decode_host_recovery(
        code,
        message,
        out.finish()?,
        budget,
    )?))
}
