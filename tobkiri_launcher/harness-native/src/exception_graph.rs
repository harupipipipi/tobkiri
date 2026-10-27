//! Closed exception DATA for Rust inspection ports and original guest classes.
//!
//! Native ports must supply only reviewed portable message/field provenance.
//! This decoder cannot establish confidentiality by guessing from strings.
//! Host paths, arbitrary native classes, panics and allocation failures are not
//! converted into portable domain errors. Unknown representations are gaps.
use std::{fmt, sync::Arc};
use tobkiri_canon::{
    AllocationBudget, AllocationError, AllocationKind, AllocationRequest, CoreBuffer, CoreRef,
    CoreValue, MAX_BYTES,
};

const FORMAT: &str = "tobkiri.native-exception/1";

/// Explicit original class tags, never an input-driven module/class lookup.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum NativeErrorKind {
    ValueError,
    RuntimeError,
    DefaultProfileV4Error,
    BundleIntegrityError,
    ProfileResolutionDenied,
    ExternalPackCatalogDenied,
    PackBoundaryError,
    ProfileContentProjectionError,
    ProtocolError,
    CanonicalizationError,
    SchemaValidationError,
    InvalidArtifactError,
    JsonDecodeError,
    OsError,
    FileNotFoundError,
    PermissionError,
    NotADirectoryError,
    IsADirectoryError,
    FileExistsError,
    InterruptedError,
    BlockingIoError,
    TimeoutError,
    ConnectionError,
    BrokenPipeError,
    ConnectionAbortedError,
    ConnectionRefusedError,
    ConnectionResetError,
    ProcessLookupError,
    ChildProcessError,
}

impl NativeErrorKind {
    /// Stable frame spelling, distinct from an original Python class's code.
    pub fn tag(self) -> &'static str {
        use NativeErrorKind::*;
        match self {
            ValueError => "value-error",
            RuntimeError => "runtime-error",
            DefaultProfileV4Error => "default-profile-v4-error",
            BundleIntegrityError => "bundle-integrity-error",
            ProfileResolutionDenied => "profile-resolution-denied",
            ExternalPackCatalogDenied => "external-pack-catalog-denied",
            PackBoundaryError => "pack-boundary-error",
            ProfileContentProjectionError => "profile-content-projection-error",
            ProtocolError => "protocol-error",
            CanonicalizationError => "canonicalization-error",
            SchemaValidationError => "schema-validation-error",
            InvalidArtifactError => "invalid-artifact-error",
            JsonDecodeError => "json-decode-error",
            OsError => "os-error",
            FileNotFoundError => "file-not-found-error",
            PermissionError => "permission-error",
            NotADirectoryError => "not-a-directory-error",
            IsADirectoryError => "is-a-directory-error",
            FileExistsError => "file-exists-error",
            InterruptedError => "interrupted-error",
            BlockingIoError => "blocking-io-error",
            TimeoutError => "timeout-error",
            ConnectionError => "connection-error",
            BrokenPipeError => "broken-pipe-error",
            ConnectionAbortedError => "connection-aborted-error",
            ConnectionRefusedError => "connection-refused-error",
            ConnectionResetError => "connection-reset-error",
            ProcessLookupError => "process-lookup-error",
            ChildProcessError => "child-process-error",
        }
    }
    fn parse(tag: &str) -> Result<Self> {
        use NativeErrorKind::*;
        let all = [
            ValueError,
            RuntimeError,
            DefaultProfileV4Error,
            BundleIntegrityError,
            ProfileResolutionDenied,
            ExternalPackCatalogDenied,
            PackBoundaryError,
            ProfileContentProjectionError,
            ProtocolError,
            CanonicalizationError,
            SchemaValidationError,
            InvalidArtifactError,
            JsonDecodeError,
            OsError,
            FileNotFoundError,
            PermissionError,
            NotADirectoryError,
            IsADirectoryError,
            FileExistsError,
            InterruptedError,
            BlockingIoError,
            TimeoutError,
            ConnectionError,
            BrokenPipeError,
            ConnectionAbortedError,
            ConnectionRefusedError,
            ConnectionResetError,
            ProcessLookupError,
            ChildProcessError,
        ];
        all.into_iter()
            .find(|kind| kind.tag() == tag)
            .ok_or(NativeErrorGraphError::Unsupported)
    }
    fn os(self) -> bool {
        matches!(
            self,
            Self::OsError
                | Self::FileNotFoundError
                | Self::PermissionError
                | Self::NotADirectoryError
                | Self::IsADirectoryError
                | Self::FileExistsError
                | Self::InterruptedError
                | Self::BlockingIoError
                | Self::TimeoutError
                | Self::ConnectionError
                | Self::BrokenPipeError
                | Self::ConnectionAbortedError
                | Self::ConnectionRefusedError
                | Self::ConnectionResetError
                | Self::ProcessLookupError
                | Self::ChildProcessError
        )
    }
}

/// Unsupported ABI DATA is not an original domain denial or native IO result.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum NativeErrorGraphError {
    Invalid,
    Unsupported,
    Envelope,
    Allocation(AllocationError),
}
impl fmt::Display for NativeErrorGraphError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Invalid => "native exception graph differs from its closed schema",
            Self::Unsupported => "native exception representation is unsupported",
            Self::Envelope => "native exception exceeds the original envelope",
            Self::Allocation(_) => "original native exception allocation owner refused work",
        })
    }
}
impl std::error::Error for NativeErrorGraphError {}
impl From<AllocationError> for NativeErrorGraphError {
    fn from(value: AllocationError) -> Self {
        Self::Allocation(value)
    }
}
type Result<T> = std::result::Result<T, NativeErrorGraphError>;

/// Validated immutable graph and original envelopes retain their payload owner.
/// Graph IDs are scoped DATA identities; they confer no native operation rights.
pub struct NativeErrorGraph {
    code: CoreBuffer,
    message: CoreBuffer,
    details: CoreBuffer,
    graph: CoreValue,
    id: u32,
    root: u32,
}
impl NativeErrorGraph {
    /// Decode under the SAME original invocation budget as the outer buffers.
    /// Parsing and semantic traversal consume its existing work/memory limits.
    pub fn decode(
        code: CoreBuffer,
        message: CoreBuffer,
        details: CoreBuffer,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Self> {
        let bytes = code
            .len()
            .checked_add(message.len())
            .and_then(|n| n.checked_add(details.len()))
            .ok_or(NativeErrorGraphError::Envelope)?;
        if bytes > MAX_BYTES {
            return Err(NativeErrorGraphError::Envelope);
        }
        // Charge before all subsequent semantic byte/node traversal. No new
        // owner, dynamic native registry or unaccounted node cache is created.
        budget.reserve(AllocationRequest {
            kind: AllocationKind::Temporary,
            bytes: 0,
            elements: 0,
            depth: 0,
            work: (bytes as u64)
                .checked_mul(16)
                .ok_or(NativeErrorGraphError::Envelope)?,
        })?;
        let graph = CoreValue::parse_guest(details.as_bytes(), budget)?;
        exact(&graph, &["format", "graph_id", "root", "nodes"])?;
        if field(&graph, "format")?.as_str() != Some(FORMAT) {
            return Err(NativeErrorGraphError::Unsupported);
        }
        let id = index(&field(&graph, "graph_id")?)?;
        let root = index(&field(&graph, "root")?)?;
        let nodes = field(&graph, "nodes")?;
        let array = nodes.array().ok_or(NativeErrorGraphError::Invalid)?;
        if array.is_empty() || root as usize >= array.len() {
            return Err(NativeErrorGraphError::Invalid);
        }
        for node in array.iter() {
            validate_node(&node, array.len())?;
        }
        let node = array
            .get(root as usize)
            .ok_or(NativeErrorGraphError::Invalid)?;
        let kind = kind(&node)?;
        if code.as_str()? != kind.tag() {
            return Err(NativeErrorGraphError::Invalid);
        }
        validate_message(&node, kind, message.as_str()?)?;
        // The original owner can retire/expire during parsing or validation.
        // Recheck before exposing the completed DATA graph to its caller.
        budget.reserve(AllocationRequest {
            kind: AllocationKind::Temporary,
            bytes: 0,
            elements: 0,
            depth: 0,
            work: 1,
        })?;
        Ok(Self {
            code,
            message,
            details,
            graph,
            id,
            root,
        })
    }
    pub fn graph_id(&self) -> u32 {
        self.id
    }
    pub fn root(&self) -> NativeErrorNode {
        self.node(self.root).expect("validated immutable root")
    }
    pub fn node(&self, index: u32) -> Option<NativeErrorNode> {
        let value = self.graph.get_key("nodes")?.get_index(index as usize)?;
        Some(NativeErrorNode {
            kind: kind(&value).ok()?,
            value,
        })
    }
    pub fn code(&self) -> &CoreBuffer {
        &self.code
    }
    pub fn message(&self) -> &CoreBuffer {
        &self.message
    }
    pub fn details(&self) -> &CoreBuffer {
        &self.details
    }
}

/// Borrow/share only original funded DATA; class objects exist solely in Wasm.
pub struct NativeErrorNode {
    value: CoreValue,
    kind: NativeErrorKind,
}
impl NativeErrorNode {
    pub fn kind(&self) -> NativeErrorKind {
        self.kind
    }
    pub fn args(&self) -> CoreValue {
        self.value
            .get_key("args")
            .expect("validated immutable args")
    }
    pub fn fields(&self) -> CoreValue {
        self.value
            .get_key("fields")
            .expect("validated immutable fields")
    }
    pub fn cause(&self) -> Option<u32> {
        edge(&self.value, "cause", usize::MAX).expect("validated edge")
    }
    pub fn context(&self) -> Option<u32> {
        edge(&self.value, "context", usize::MAX).expect("validated edge")
    }
    pub fn suppress_context(&self) -> bool {
        self.value
            .get_key("suppress_context")
            .and_then(|v| v.as_bool())
            .expect("validated suppression")
    }
}
fn field(value: &CoreValue, key: &str) -> Result<CoreValue> {
    value.get_key(key).ok_or(NativeErrorGraphError::Invalid)
}
fn exact(value: &CoreValue, keys: &[&str]) -> Result<()> {
    let obj = value.object().ok_or(NativeErrorGraphError::Invalid)?;
    if obj.len() != keys.len() || keys.iter().any(|key| obj.get(key).is_none()) {
        return Err(NativeErrorGraphError::Invalid);
    }
    Ok(())
}
fn index(value: &CoreValue) -> Result<u32> {
    value
        .as_i64()
        .and_then(|v| u32::try_from(v).ok())
        .ok_or(NativeErrorGraphError::Invalid)
}
fn edge(node: &CoreValue, key: &str, length: usize) -> Result<Option<u32>> {
    let value = field(node, key)?;
    if matches!(value.inspect(), CoreRef::Null) {
        return Ok(None);
    }
    let i = index(&value)?;
    if i as usize >= length {
        return Err(NativeErrorGraphError::Invalid);
    }
    Ok(Some(i))
}
fn kind(node: &CoreValue) -> Result<NativeErrorKind> {
    NativeErrorKind::parse(
        field(node, "kind")?
            .as_str()
            .ok_or(NativeErrorGraphError::Invalid)?,
    )
}
fn validate_node(node: &CoreValue, length: usize) -> Result<()> {
    exact(
        node,
        &[
            "kind",
            "args",
            "fields",
            "cause",
            "context",
            "suppress_context",
        ],
    )?;
    edge(node, "cause", length)?;
    edge(node, "context", length)?;
    field(node, "suppress_context")?
        .as_bool()
        .ok_or(NativeErrorGraphError::Invalid)?;
    let kind = kind(node)?;
    let args = field(node, "args")?;
    let array = args.array().ok_or(NativeErrorGraphError::Invalid)?;
    let fields = field(node, "fields")?;
    if kind.os() {
        exact(&fields, &["errno", "strerror", "filename", "filename2"])?;
        if array.len() != 2 {
            return Err(NativeErrorGraphError::Unsupported);
        }
        let errno = array.get(0).ok_or(NativeErrorGraphError::Invalid)?;
        let strerror = array.get(1).ok_or(NativeErrorGraphError::Invalid)?;
        let number = errno.as_i64().ok_or(NativeErrorGraphError::Invalid)?;
        let text = strerror.as_str().ok_or(NativeErrorGraphError::Invalid)?;
        if field(&fields, "errno")?.as_i64() != Some(number)
            || field(&fields, "strerror")?.as_str() != Some(text)
            || !matches!(field(&fields, "filename")?.inspect(), CoreRef::Null)
            || !matches!(field(&fields, "filename2")?.inspect(), CoreRef::Null)
        {
            return Err(NativeErrorGraphError::Unsupported);
        }
        return Ok(());
    }
    if array.len() != 1 || array.get(0).and_then(|v| v.as_str().map(|_| ())).is_none() {
        return Err(NativeErrorGraphError::Unsupported);
    }
    match kind {
        NativeErrorKind::SchemaValidationError => {
            exact(&fields, &["diagnostics"])?;
            let diagnostics = field(&fields, "diagnostics")?;
            if diagnostics
                .array()
                .ok_or(NativeErrorGraphError::Invalid)?
                .iter()
                .any(|v| v.as_str().is_none())
            {
                return Err(NativeErrorGraphError::Invalid);
            }
        }
        NativeErrorKind::JsonDecodeError => validate_json(
            &fields,
            &array.get(0).ok_or(NativeErrorGraphError::Invalid)?,
        )?,
        _ => exact(&fields, &[])?,
    }
    Ok(())
}
fn validate_json(fields: &CoreValue, argument: &CoreValue) -> Result<()> {
    exact(fields, &["msg", "doc", "pos", "lineno", "colno"])?;
    let msg = field(fields, "msg")?;
    let doc = field(fields, "doc")?;
    let msg = msg.as_str().ok_or(NativeErrorGraphError::Invalid)?;
    let doc = doc.as_str().ok_or(NativeErrorGraphError::Invalid)?;
    let pos = index(&field(fields, "pos")?)?;
    if pos as usize > doc.chars().count() {
        return Err(NativeErrorGraphError::Unsupported);
    }
    let mut line = 1;
    let mut column = 1;
    for c in doc.chars().take(pos as usize) {
        if c == '\n' {
            line += 1;
            column = 1;
        } else {
            column += 1;
        }
    }
    if field(fields, "lineno")?.as_i64() != Some(line)
        || field(fields, "colno")?.as_i64() != Some(column)
    {
        return Err(NativeErrorGraphError::Invalid);
    }
    let l = Decimal::new(line);
    let c = Decimal::new(column);
    let p = Decimal::new(pos.into());
    if !parts(
        argument.as_str().ok_or(NativeErrorGraphError::Invalid)?,
        &[
            msg,
            ": line ",
            l.as_str(),
            " column ",
            c.as_str(),
            " (char ",
            p.as_str(),
            ")",
        ],
    ) {
        return Err(NativeErrorGraphError::Invalid);
    }
    Ok(())
}
fn validate_message(node: &CoreValue, kind: NativeErrorKind, message: &str) -> Result<()> {
    let args = field(node, "args")?;
    let first = args.get_index(0).ok_or(NativeErrorGraphError::Invalid)?;
    let valid = if kind.os() {
        let errno = Decimal::new(first.as_i64().ok_or(NativeErrorGraphError::Invalid)?);
        let text = args.get_index(1).ok_or(NativeErrorGraphError::Invalid)?;
        parts(
            message,
            &[
                "[Errno ",
                errno.as_str(),
                "] ",
                text.as_str().ok_or(NativeErrorGraphError::Invalid)?,
            ],
        )
    } else {
        first.as_str() == Some(message)
    };
    if valid {
        Ok(())
    } else {
        Err(NativeErrorGraphError::Invalid)
    }
}
fn parts(mut value: &str, parts: &[&str]) -> bool {
    for part in parts {
        let Some(rest) = value.strip_prefix(part) else {
            return false;
        };
        value = rest;
    }
    value.is_empty()
}
struct Decimal {
    bytes: [u8; 21],
    start: usize,
}
impl Decimal {
    fn new(value: i64) -> Self {
        let mut out = Self {
            bytes: [0; 21],
            start: 21,
        };
        let mut n = value.unsigned_abs();
        loop {
            out.start -= 1;
            out.bytes[out.start] = b'0' + (n % 10) as u8;
            n /= 10;
            if n == 0 {
                break;
            }
        }
        if value < 0 {
            out.start -= 1;
            out.bytes[out.start] = b'-';
        }
        out
    }
    fn as_str(&self) -> &str {
        std::str::from_utf8(&self.bytes[self.start..]).expect("decimal ASCII")
    }
}

#[cfg(test)]
mod tests;
