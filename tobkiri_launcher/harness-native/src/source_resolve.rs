//! Descriptor-backed translation of CPython 3.13.16's non-strict realpath.
//!
//! The caller has already selected an original module and admitted THIS
//! operation. Only its actual SDK root and internal symlinks are supported.
//! External source relations need their own capability; they are not followed
//! and are not reported as original OSError/missing-path outcomes.
use std::{
    ffi::CStr,
    fs::File,
    io,
    os::{
        fd::{AsRawFd, FromRawFd},
        unix::{ffi::OsStrExt, fs::MetadataExt},
    },
    path::Path,
    sync::Arc,
};
use tobkiri_canon::{AllocationBudget, ControlHandle, CoreBuffer, MAX_BYTES};
use tobkiri_gate::{
    CapacityKind, ControlKind, ControlReservation, NativeSetupInvocation, NativeSetupReservation,
};
use tobkiri_wasm::{NativeDataIoError, NativeInspectionOutput};

#[cfg(all(test, target_arch = "aarch64"))]
#[path = "source_resolve_tests.rs"]
mod tests;

type Result<T> = std::result::Result<T, NativeDataIoError>;

// These are native bookkeeping guards, never source permissions. The Vec is
// deallocated before its original payload reservation is released.
struct FundedVec<T> {
    values: Vec<T>,
    funding: NativeSetupReservation,
}
impl<T> FundedVec<T> {
    fn new(invocation: &NativeSetupInvocation) -> Result<Self> {
        Ok(Self {
            values: Vec::new(),
            funding: invocation
                .reserve(CapacityKind::VfsBuffer, 0)
                .map_err(|_| NativeDataIoError::Capacity)?,
        })
    }
    fn push(&mut self, value: T, output: &mut NativeInspectionOutput<'_>) -> Result<()> {
        if self.values.len() == self.values.capacity() {
            let capacity = self
                .values
                .len()
                .checked_add(1)
                .and_then(usize::checked_next_power_of_two)
                .ok_or(NativeDataIoError::Capacity)?;
            let bytes = capacity
                .checked_mul(size_of::<T>())
                .ok_or(NativeDataIoError::Capacity)?;
            // Both old and replacement storage coexist during growth. Reserve
            // the replacement separately, then free the old Vec BEFORE its
            // original funding, rather than undercounting realloc's peak.
            let replacement_funding = output
                .invocation()
                .reserve(CapacityKind::VfsBuffer, bytes as u64)
                .map_err(|_| NativeDataIoError::Capacity)?;
            let mut replacement = Vec::new();
            replacement
                .try_reserve_exact(capacity)
                .map_err(|_| NativeDataIoError::Capacity)?;
            if replacement.capacity() != capacity {
                return Err(output.reject_execution(NativeDataIoError::CompletionUnknown));
            }
            replacement.append(&mut self.values);
            let previous = std::mem::replace(&mut self.values, replacement);
            drop(previous);
            let previous_funding = std::mem::replace(&mut self.funding, replacement_funding);
            drop(previous_funding);
        }
        self.values.push(value);
        Ok(())
    }
}

// Actual descriptors close before their original native/control reservations.
struct Directory {
    file: File,
    parent: Option<MetadataFd>,
    name: Option<CoreBuffer>,
}
struct DirectoryFunding {
    _native: NativeSetupReservation,
    _control: ControlReservation,
}
#[derive(Clone)]
struct MetadataFd(ControlHandle<Directory>);
impl MetadataFd {
    fn file(&self) -> &File {
        &self.0.get().file
    }

    /// Verify each actual retained parent/name edge. A rename or replacement
    /// cannot redirect an already inspected path through a stale pathname.
    fn validate(&self, output: &mut NativeInspectionOutput<'_>) -> Result<()> {
        let mut current = self;
        while let Some(parent) = &current.0.get().parent {
            let name = current
                .0
                .get()
                .name
                .as_ref()
                .ok_or(NativeDataIoError::CompletionUnknown)?;
            let actual = stat_at(parent, as_cstr(name)?, output)?
                .map_err(|_| NativeDataIoError::CompletionUnknown)?;
            output.charge_work(1)?;
            let held = current
                .file()
                .metadata()
                .map_err(|_| NativeDataIoError::CompletionUnknown)?;
            output.charge_work(0)?;
            if actual.st_dev as u64 != held.dev()
                || actual.st_ino != held.ino()
                || actual.st_mode & libc::S_IFMT != libc::S_IFDIR
            {
                return Err(output.reject_execution(NativeDataIoError::CompletionUnknown));
            }
            current = parent;
        }
        Ok(())
    }
    fn open(
        invocation: &NativeSetupInvocation,
        parent: Option<&Self>,
        name: &CStr,
        nofollow: bool,
        output: &mut NativeInspectionOutput<'_>,
    ) -> Result<io::Result<Self>> {
        let bytes = ControlHandle::<Directory>::storage_bytes::<DirectoryFunding>();
        let native = invocation
            .reserve(CapacityKind::VfsBuffer, bytes as u64)
            .map_err(|_| NativeDataIoError::Capacity)?;
        let control = invocation
            .control()
            .reserve(ControlKind::Frame, bytes as u64)
            .map_err(|_| NativeDataIoError::Capacity)?;
        let held_name = if let Some(parent) = parent {
            parent.validate(output)?;
            Some(copy(
                output.allocation_owners().graph(),
                name.to_bytes_with_nul(),
            )?)
        } else {
            None
        };
        let flags = libc::O_EVTONLY
            | libc::O_DIRECTORY
            | libc::O_CLOEXEC
            | if nofollow { libc::O_NOFOLLOW } else { 0 };
        loop {
            output.charge_work(1)?;
            let fd = unsafe {
                libc::openat(
                    parent.map_or(libc::AT_FDCWD, |p| p.file().as_raw_fd()),
                    name.as_ptr(),
                    flags,
                )
            };
            let error = (fd < 0).then(io::Error::last_os_error);
            // Own an actually opened descriptor before a cancellation can fail.
            let file = (fd >= 0).then(|| unsafe { File::from_raw_fd(fd) });
            output.charge_work(0)?;
            match (file, error) {
                (Some(file), _) => {
                    return ControlHandle::try_new_precharged(
                        Directory {
                            file,
                            parent: parent.cloned(),
                            name: held_name,
                        },
                        DirectoryFunding {
                            _native: native,
                            _control: control,
                        },
                    )
                    .map(Self)
                    .map(Ok)
                    .map_err(|_| NativeDataIoError::Capacity);
                }
                (_, Some(error)) if error.kind() == io::ErrorKind::Interrupted => (),
                (_, Some(error)) => return Ok(Err(error)),
                _ => return Err(NativeDataIoError::CompletionUnknown),
            }
        }
    }
}

struct Snapshot {
    path: CoreBuffer,
    directory: MetadataFd,
    missing: usize,
    inside: bool,
}
struct Seen {
    path: CoreBuffer,
    resolved: Option<Snapshot>,
}
enum Part {
    Name(CoreBuffer, usize, usize),
    Complete(usize),
    EnterRoot,
}

/// Evaluate at the original reached metadata operation, under its actual IO
/// scope. No directory or source is examined during inventory construction.
pub(super) fn resolve(
    output: &mut NativeInspectionOutput<'_>,
    root: &Path,
    relative: &str,
) -> Result<CoreBuffer> {
    output.charge_work(0)?;
    if !root.is_absolute()
        || relative.is_empty()
        || relative
            .split('/')
            .any(|p| p.is_empty() || p == "." || p == "..")
    {
        return Err(output.reject_execution(NativeDataIoError::Unavailable));
    }
    let invocation = output.invocation().clone();
    let budget = output.allocation_owners().graph().clone();
    // Pay the native stack frame, including libc stat/path scratch, before use.
    let _frame = invocation
        .reserve(CapacityKind::VfsBuffer, 16 * 1024)
        .map_err(|_| NativeDataIoError::Capacity)?;
    let root_text = root.to_str().ok_or(NativeDataIoError::Unavailable)?;
    let root_name = c_path(&budget, root.as_os_str().as_bytes())?;
    let source = MetadataFd::open(&invocation, None, as_cstr(&root_name)?, false, output)?
        .map_err(|_| NativeDataIoError::Unavailable)?;
    output.charge_work(1)?;
    let source_identity = source
        .file()
        .metadata()
        .map_err(|_| NativeDataIoError::Unavailable)?;
    output.charge_work(0)?;
    let physical_root = descriptor_path(&source, &budget, output)?;
    let system = MetadataFd::open(&invocation, None, c"/", false, output)?
        .map_err(|_| NativeDataIoError::Unavailable)?;
    let mut directory = system.clone();
    let mut path = copy(&budget, b"/")?;
    let mut pending = FundedVec::new(&invocation)?;
    let mut seen: FundedVec<Seen> = FundedVec::new(&invocation)?;
    push_parts(&mut pending, copy(&budget, relative.as_bytes())?, output)?;
    pending.push(Part::EnterRoot, output)?;
    push_parts(
        &mut pending,
        copy(&budget, root.as_os_str().as_bytes())?,
        output,
    )?;
    let mut missing = 0_usize;
    let mut inside = false;

    while let Some(part) = pending.values.pop() {
        output.charge_work(1)?;
        let (data, start, end) = match part {
            Part::EnterRoot => {
                directory.validate(output)?;
                output.charge_work(1)?;
                let actual = directory
                    .file()
                    .metadata()
                    .map_err(|_| NativeDataIoError::Unavailable)?;
                output.charge_work(0)?;
                if missing != 0
                    || actual.dev() != source_identity.dev()
                    || actual.ino() != source_identity.ino()
                {
                    return Err(output.reject_execution(NativeDataIoError::CompletionUnknown));
                }
                inside = true;
                continue;
            }
            Part::Complete(index) => {
                let snapshot = Snapshot {
                    path: path.clone(),
                    directory: directory.clone(),
                    missing,
                    inside,
                };
                seen.values
                    .get_mut(index)
                    .ok_or(NativeDataIoError::CompletionUnknown)?
                    .resolved = Some(snapshot);
                continue;
            }
            Part::Name(data, start, end) => (data, start, end),
        };
        let name = &data.as_bytes()[start..end];
        if name.is_empty() || name == b"." {
            continue;
        }
        if name == b".." {
            if missing > 0 {
                missing -= 1;
            } else {
                output.charge_work(1)?;
                directory.validate(output)?;
                let identity = directory
                    .file()
                    .metadata()
                    .map_err(|_| NativeDataIoError::Unavailable)?;
                output.charge_work(0)?;
                if inside
                    && identity.dev() == source_identity.dev()
                    && identity.ino() == source_identity.ino()
                {
                    return Err(output.reject_execution(NativeDataIoError::Unavailable));
                }
                // The original '..' operation is lexical. Retain its actual
                // already-verified parent, rather than reopening by a string.
                if let Some(parent) = &directory.0.get().parent {
                    directory = parent.clone();
                }
            }
            let parent = path
                .as_bytes()
                .iter()
                .rposition(|&b| b == b'/')
                .unwrap_or(0);
            path = copy(&budget, &path.as_bytes()[..parent.max(1)])?;
            continue;
        }
        let next = join(&budget, path.as_bytes(), name)?;
        // Relative descriptor lookup must not discover a target that the
        // original full-path lstat could not reach. Exact non-strict handling
        // at this OS boundary is still unsupported, not a synthetic OSError.
        if next.len() >= libc::PATH_MAX as usize {
            return Err(output.reject_execution(NativeDataIoError::Unavailable));
        }
        if missing > 0 {
            missing = missing.checked_add(1).ok_or(NativeDataIoError::Capacity)?;
            path = next;
            continue;
        }
        let name_c = c_path(&budget, name)?;
        directory.validate(output)?;
        let metadata = stat_at(&directory, as_cstr(&name_c)?, output)?;
        let metadata = match metadata {
            Ok(metadata) => metadata,
            // CPython strict=False ignores OSError; it does not assert existence
            // or issue read permission for this lexical unresolved suffix.
            Err(_) => {
                missing = 1;
                path = next;
                continue;
            }
        };
        if metadata.st_mode & libc::S_IFMT != libc::S_IFLNK {
            if metadata.st_mode & libc::S_IFMT == libc::S_IFDIR {
                match MetadataFd::open(
                    &invocation,
                    Some(&directory),
                    as_cstr(&name_c)?,
                    true,
                    output,
                )? {
                    Ok(child) => {
                        output.charge_work(1)?;
                        let identity = child
                            .file()
                            .metadata()
                            .map_err(|_| NativeDataIoError::Unavailable)?;
                        output.charge_work(0)?;
                        if identity.dev() != metadata.st_dev as u64
                            || identity.ino() != metadata.st_ino
                        {
                            return Err(
                                output.reject_execution(NativeDataIoError::CompletionUnknown)
                            );
                        }
                        inside |= identity.dev() == source_identity.dev()
                            && identity.ino() == source_identity.ino();
                        directory = child;
                    }
                    // lstat already observed a directory. Failure to retain its
                    // actual descriptor is an execution/admission failure, not
                    // an original missing-path observation.
                    Err(_) => return Err(output.reject_execution(NativeDataIoError::Unavailable)),
                }
            } else {
                missing = 1;
            }
            path = next;
            continue;
        }
        output.charge_work(seen.values.len() as u64 * next.len() as u64)?;
        if let Some(old) = seen
            .values
            .iter()
            .find(|s| s.path.as_bytes() == next.as_bytes())
        {
            if let Some(resolved) = &old.resolved {
                path = resolved.path.clone();
                directory = resolved.directory.clone();
                directory.validate(output)?;
                missing = resolved.missing;
                inside = resolved.inside;
            } else {
                // The original non-strict algorithm leaves a loop unresolved.
                path = next;
                missing = 1;
            }
            continue;
        }
        let target = match read_link(&directory, as_cstr(&name_c)?, &budget, output)? {
            Ok(target) => target,
            Err(_) => {
                path = next;
                missing = 1;
                continue;
            }
        };
        let after = stat_at(&directory, as_cstr(&name_c)?, output)?
            .map_err(|_| NativeDataIoError::CompletionUnknown)?;
        if after.st_dev != metadata.st_dev
            || after.st_ino != metadata.st_ino
            || after.st_mode != metadata.st_mode
            || after.st_size != metadata.st_size
        {
            return Err(output.reject_execution(NativeDataIoError::CompletionUnknown));
        }
        let mut bound_absolute = None;
        if target.as_bytes().starts_with(b"/") {
            output.charge_work(
                target.len() as u64 + physical_root.len() as u64 + root_text.len() as u64,
            )?;
            if inside {
                let prefix = if under(target.as_bytes(), root.as_os_str().as_bytes()) {
                    root_prefix(root.as_os_str().as_bytes())
                } else if under(target.as_bytes(), physical_root.as_bytes()) {
                    root_prefix(physical_root.as_bytes())
                } else {
                    // Not an original OSError: this source relation has no
                    // admitted capability yet. Do not follow it.
                    return Err(output.reject_execution(NativeDataIoError::Unavailable));
                };
                let suffix = &target.as_bytes()[prefix.len()..];
                bound_absolute = Some((copy(&budget, prefix)?, copy(&budget, suffix)?));
            }
            path = copy(&budget, b"/")?;
            directory = system.clone();
            missing = 0;
            inside = false;
        }
        let index = seen.values.len();
        seen.push(
            Seen {
                path: next,
                resolved: None,
            },
            output,
        )?;
        pending.push(Part::Complete(index), output)?;
        if let Some((prefix, suffix)) = bound_absolute {
            push_parts(&mut pending, suffix, output)?;
            pending.push(Part::EnterRoot, output)?;
            push_parts(&mut pending, prefix, output)?;
        } else {
            push_parts(&mut pending, target, output)?;
        }
    }
    directory.validate(output)?;
    if !inside {
        return Err(output.reject_execution(NativeDataIoError::CompletionUnknown));
    }
    output.charge_work(0)?;
    Ok(path)
}

fn under(path: &[u8], root: &[u8]) -> bool {
    let root = root_prefix(root);
    path == root
        || path
            .strip_prefix(root)
            .is_some_and(|rest| rest.starts_with(b"/"))
}
fn root_prefix(mut root: &[u8]) -> &[u8] {
    while root.ends_with(b"/") {
        root = &root[..root.len() - 1];
    }
    root
}
fn push_parts(
    parts: &mut FundedVec<Part>,
    data: CoreBuffer,
    output: &mut NativeInspectionOutput<'_>,
) -> Result<()> {
    output.charge_work(data.len() as u64)?;
    let mut end = data.len();
    loop {
        let start = data.as_bytes()[..end]
            .iter()
            .rposition(|&b| b == b'/')
            .map_or(0, |i| i + 1);
        parts.push(Part::Name(data.clone(), start, end), output)?;
        if start == 0 {
            break;
        }
        end = start - 1;
    }
    Ok(())
}
fn copy(budget: &Arc<dyn AllocationBudget>, bytes: &[u8]) -> Result<CoreBuffer> {
    CoreBuffer::copy_from(bytes, budget).map_err(|_| NativeDataIoError::Capacity)
}
fn join(budget: &Arc<dyn AllocationBudget>, base: &[u8], name: &[u8]) -> Result<CoreBuffer> {
    let mut out = CoreBuffer::builder(budget, MAX_BYTES);
    out.append(base).map_err(|_| NativeDataIoError::Capacity)?;
    if !base.ends_with(b"/") {
        out.append(b"/").map_err(|_| NativeDataIoError::Capacity)?;
    }
    out.append(name).map_err(|_| NativeDataIoError::Capacity)?;
    out.finish().map_err(|_| NativeDataIoError::Capacity)
}
fn c_path(budget: &Arc<dyn AllocationBudget>, path: &[u8]) -> Result<CoreBuffer> {
    if path.contains(&0) {
        return Err(NativeDataIoError::Unavailable);
    }
    let mut out = CoreBuffer::builder(budget, MAX_BYTES);
    out.append(path).map_err(|_| NativeDataIoError::Capacity)?;
    out.append(&[0]).map_err(|_| NativeDataIoError::Capacity)?;
    out.finish().map_err(|_| NativeDataIoError::Capacity)
}
fn as_cstr(path: &CoreBuffer) -> Result<&CStr> {
    CStr::from_bytes_with_nul(path.as_bytes()).map_err(|_| NativeDataIoError::Unavailable)
}
fn descriptor_path(
    fd: &MetadataFd,
    budget: &Arc<dyn AllocationBudget>,
    output: &mut NativeInspectionOutput<'_>,
) -> Result<CoreBuffer> {
    let mut bytes = [0_u8; libc::PATH_MAX as usize];
    output.charge_work(1)?;
    let result = unsafe { libc::fcntl(fd.file().as_raw_fd(), libc::F_GETPATH, bytes.as_mut_ptr()) };
    output.charge_work(0)?;
    if result < 0 {
        return Err(NativeDataIoError::Unavailable);
    }
    let end = bytes
        .iter()
        .position(|&b| b == 0)
        .ok_or(NativeDataIoError::Unavailable)?;
    copy(budget, &bytes[..end])
}
fn stat_at(
    fd: &MetadataFd,
    name: &CStr,
    output: &mut NativeInspectionOutput<'_>,
) -> Result<io::Result<libc::stat>> {
    loop {
        let mut metadata = std::mem::MaybeUninit::uninit();
        output.charge_work(1)?;
        let result = unsafe {
            libc::fstatat(
                fd.file().as_raw_fd(),
                name.as_ptr(),
                metadata.as_mut_ptr(),
                libc::AT_SYMLINK_NOFOLLOW,
            )
        };
        let error = (result < 0).then(io::Error::last_os_error);
        output.charge_work(0)?;
        match error {
            None => return Ok(Ok(unsafe { metadata.assume_init() })),
            Some(error) if error.kind() == io::ErrorKind::Interrupted => (),
            Some(error) => return Ok(Err(error)),
        }
    }
}
fn read_link(
    fd: &MetadataFd,
    name: &CStr,
    budget: &Arc<dyn AllocationBudget>,
    output: &mut NativeInspectionOutput<'_>,
) -> Result<io::Result<CoreBuffer>> {
    let mut bytes = [0_u8; libc::PATH_MAX as usize + 1];
    loop {
        output.charge_work(1)?;
        let size = unsafe {
            libc::readlinkat(
                fd.file().as_raw_fd(),
                name.as_ptr(),
                bytes.as_mut_ptr().cast(),
                bytes.len(),
            )
        };
        let error = (size < 0).then(io::Error::last_os_error);
        output.charge_work(0)?;
        match error {
            Some(error) if error.kind() == io::ErrorKind::Interrupted => (),
            Some(error) => return Ok(Err(error)),
            None => {
                let size = usize::try_from(size).map_err(|_| NativeDataIoError::Capacity)?;
                if size >= bytes.len() {
                    return Err(NativeDataIoError::Capacity);
                }
                std::str::from_utf8(&bytes[..size]).map_err(|_| NativeDataIoError::Unavailable)?;
                return copy(budget, &bytes[..size]).map(Ok);
            }
        }
    }
}
