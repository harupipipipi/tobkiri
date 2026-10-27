//! Typed projection of selected native inputs. None of these paths grants IO.
use std::{path::Path, sync::Arc};
use tobkiri_canon::{
    AllocationBudget, AllocationError, AllocationKind, AllocationRequest, CoreBuffer,
    CoreBufferBuilder, MAX_BYTES,
};
use tobkiri_gate::NativeSetupInvocation;
use tobkiri_wasm::NativeDataIoError;

/// The existing configuration's borrowed values, not a new root selection.
pub struct BootstrapRoots<'a> {
    pub runtime: &'a Path,
    pub working_directory: &'a Path,
    pub development_workspace: Option<&'a Path>,
}

/// Actual application owner retained through execution and physical cleanup.
///
/// Implementations borrow the already selected config and validate its actual
/// shutdown owner before/after the callback. They must not rediscover roots.
/// Root projection alone never authorizes inspection. A separate, explicitly
/// selected original-operation policy and real reached descriptors are needed.
pub trait BootstrapInputOwner: Send + Sync + 'static {
    /// Deliberately select a narrowly defined original-operation policy. The
    /// default inventory-only owner grants no inspection. Paths/index rows do
    /// not issue this policy; actual source/FD custody is still checked at use.
    fn original_module_metadata_policy(&self) -> Option<&'static crate::ModuleMetadataPolicy> {
        None
    }
    fn with_roots<R>(
        &self,
        use_roots: impl for<'a> FnOnce(BootstrapRoots<'a>) -> R,
    ) -> Result<R, NativeDataIoError>;

    /// Check the same original application owner, without path IO or selection.
    fn check_live(&self) -> Result<(), NativeDataIoError>;
}

/// Fixed module source mapping from the authenticated component source index.
pub struct BootstrapModule {
    pub module: &'static str,
    /// Relative to the original selected runtime directory.
    pub relative_file: &'static str,
}

/// Immutable coherence data, never a filesystem capability or signature grant.
pub struct BootstrapModuleIndex {
    pub sha256: &'static str,
    pub modules: &'static [BootstrapModule],
}

#[derive(Clone, Copy)]
pub(super) struct Scope([u64; 4]);
impl Scope {
    pub(super) fn original(invocation: &NativeSetupInvocation) -> Self {
        let original = invocation.provenance();
        // This invocation can claim exactly one Store, with no reset/generation
        // change. Rust imports additionally retain/check actual shared custody.
        Self([original.account_generation, original.invocation, 1, 1])
    }

    pub(super) fn write(self, out: &mut CoreBufferBuilder) -> Result<(), AllocationError> {
        out.append(b"[")?;
        for (index, value) in self.0.into_iter().enumerate() {
            if index != 0 {
                out.append(b",")?;
            }
            out.append(b"\"")?;
            let mut text = [0_u8; 16];
            let mut offset = text.len();
            let mut value = value;
            loop {
                offset -= 1;
                text[offset] = b"0123456789abcdef"[(value & 15) as usize];
                value >>= 4;
                if value == 0 {
                    break;
                }
            }
            out.append(&text[offset..])?;
            out.append(b"\"")?;
        }
        out.append(b"]")
    }

    pub(super) fn frame(
        self,
        budget: &Arc<dyn AllocationBudget>,
        init: bool,
    ) -> Result<CoreBuffer, AllocationError> {
        let mut out = CoreBuffer::builder(budget, MAX_BYTES);
        out.append(if init {
            b"{\"format\":\"io.tobkiri.native-init.v1\",\"scope\":"
        } else {
            b"{\"format\":\"io.tobkiri.native-call.v1\",\"scope\":"
        })?;
        self.write(&mut out)?;
        out.append(if init {
            b"}"
        } else {
            b",\"args\":[],\"kwargs\":[]}"
        })?;
        out.finish()
    }
}

pub(super) fn string_parts<'a>(
    out: &mut CoreBufferBuilder,
    parts: impl IntoIterator<Item = &'a str>,
) -> Result<(), AllocationError> {
    out.append(b"\"")?;
    for text in parts {
        let bytes = text.as_bytes();
        let mut begin = 0;
        for (position, byte) in bytes.iter().copied().enumerate() {
            if byte >= 32 && byte != b'"' && byte != b'\\' {
                continue;
            }
            out.append(&bytes[begin..position])?;
            match byte {
                b'"' => out.append(b"\\\"")?,
                b'\\' => out.append(b"\\\\")?,
                control => {
                    let encoded = [
                        b'\\',
                        b'u',
                        b'0',
                        b'0',
                        b"0123456789abcdef"[(control >> 4) as usize],
                        b"0123456789abcdef"[(control & 15) as usize],
                    ];
                    out.append(&encoded)?;
                }
            }
            begin = position + 1;
        }
        out.append(&bytes[begin..])?;
    }
    out.append(b"\"")
}

fn path_text(path: &Path) -> Result<&str, AllocationError> {
    path.to_str().ok_or(AllocationError::Invalid)
}

fn path_row(
    out: &mut CoreBufferBuilder,
    name: &str,
    binding: &str,
    path: &Path,
) -> Result<(), AllocationError> {
    out.append(b"{\"name\":")?;
    string_parts(out, [name])?;
    out.append(b",\"binding\":")?;
    string_parts(out, [binding])?;
    out.append(b",\"value\":")?;
    string_parts(out, [path_text(path)?])?;
    out.append(b"}")
}

pub(super) fn inventory(
    roots: BootstrapRoots<'_>,
    index: &'static BootstrapModuleIndex,
    scope: Scope,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreBuffer, AllocationError> {
    if index.sha256.len() != 64
        || !index.sha256.bytes().all(|c| c.is_ascii_hexdigit())
        || index.modules.is_empty()
        || index.modules.len() > 128
    {
        return Err(AllocationError::Invalid);
    }
    let mut out = CoreBuffer::builder(budget, MAX_BYTES);
    out.append(b"{\"format\":\"io.tobkiri.native-input-inventory.v1\",\"scope\":")?;
    scope.write(&mut out)?;
    out.append(b",\"source_index_sha256\":")?;
    string_parts(&mut out, [index.sha256])?;
    #[cfg(unix)]
    {
        out.append(b",\"flavor\":\"posix\",\"os_flags\":{\"O_RDONLY\":0,\"O_NOFOLLOW\":")?;
        // Native O_RDONLY is zero on these actual supported Unix targets.
        // Decimal formatting is bounded stack DATA, not an allocated string.
        let value = libc::O_NOFOLLOW as u32;
        let mut bytes = [0; 10];
        let mut offset = bytes.len();
        let mut value = value;
        loop {
            offset -= 1;
            bytes[offset] = b'0' + (value % 10) as u8;
            value /= 10;
            if value == 0 {
                break;
            }
        }
        out.append(&bytes[offset..])?;
        out.append(b"}")?;
    }
    #[cfg(not(unix))]
    out.append(b",\"flavor\":\"windows\",\"os_flags\":{\"O_RDONLY\":0}")?;
    out.append(b",\"cwd_binding\":null,\"paths\":[")?;
    path_row(&mut out, "runtime-root", "runtime", roots.runtime)?;
    out.append(b",")?;
    path_row(&mut out, "working-root", "working", roots.working_directory)?;
    if let Some(path) = roots.development_workspace {
        out.append(b",")?;
        path_row(&mut out, "development-root", "development", path)?;
    }
    out.append(b"],\"modules\":[")?;
    for (ordinal, module) in index.modules.iter().enumerate() {
        if module.module.is_empty()
            || module.module.len() > 256
            || module.relative_file.is_empty()
            || module.relative_file.len() > 1024
        {
            return Err(AllocationError::Invalid);
        }
        // Validation is native work too: pay before scanning source metadata
        // and the bounded earlier-name set, using this SAME DATA account.
        budget.reserve(AllocationRequest {
            kind: AllocationKind::Temporary,
            bytes: 0,
            elements: 0,
            depth: 0,
            work: (ordinal as u64 + 1) * 256 + module.relative_file.len() as u64,
        })?;
        if !module
            .module
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"._".contains(&c))
            || module.relative_file.contains(['\\', ':', '\0'])
            || module
                .relative_file
                .split('/')
                .any(|c| c.is_empty() || c == "." || c == "..")
            || index.modules[..ordinal]
                .iter()
                .any(|old| old.module == module.module)
        {
            return Err(AllocationError::Invalid);
        }
        if ordinal != 0 {
            out.append(b",")?;
        }
        out.append(b"{\"module\":")?;
        string_parts(&mut out, [module.module])?;
        out.append(b",\"binding\":\"module_")?;
        // At most 128 fixed anchors, never a path/handler selected by a frame.
        let number = [
            (ordinal / 100) as u8 + b'0',
            ((ordinal / 10) % 10) as u8 + b'0',
            (ordinal % 10) as u8 + b'0',
        ];
        let start = if ordinal >= 100 {
            0
        } else if ordinal >= 10 {
            1
        } else {
            2
        };
        out.append(&number[start..])?;
        out.append(b"\",\"value\":")?;
        let root = path_text(roots.runtime)?;
        // No PathBuf, normalization, stat, resolve, enumeration or prefetch.
        let separator = if root.ends_with(std::path::MAIN_SEPARATOR) {
            ""
        } else {
            std::path::MAIN_SEPARATOR_STR
        };
        string_parts(&mut out, [root, separator, module.relative_file])?;
        out.append(b"}")?;
    }
    out.append(b"]}")?;
    out.finish()
}
