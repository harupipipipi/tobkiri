//! POSIX path operations reached by the fixed bundled-catalog member reader.
use super::*;
use std::{ffi::OsStr, os::unix::ffi::OsStrExt};

fn lexical_parts(relative: &str) -> impl Iterator<Item = &str> {
    let anchor = if relative.starts_with("//") && !relative.starts_with("///") {
        Some("//")
    } else if relative.starts_with('/') {
        Some("/")
    } else {
        None
    };
    anchor.into_iter().chain(
        relative
            .split('/')
            .filter(|part| !matches!(*part, "" | ".")),
    )
}

/// Join the bounded lock spelling with the original PurePosixPath rules.
pub(super) fn lexical_member(root: &Path, relative: &str) -> PathBuf {
    let mut path = root.to_path_buf();
    for part in lexical_parts(relative) {
        path.push(part);
    }
    path
}

/// Check the lexical prefixes after the original strict containment check.
pub(super) fn member_has_symlink<S: crate::BootstrapReviewOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    root: &Path,
    relative: &str,
) -> Result<bool> {
    let mut path = root.to_path_buf();
    for part in lexical_parts(relative) {
        op.check(OriginalAuthorityStage::Source)?;
        charge_work(budget, (path.as_os_str().len() + part.len() + 1) as u64)?;
        path.push(part);
        // Path.is_symlink treats the path converter's ValueError as false.
        if path.as_os_str().as_bytes().contains(&0) {
            continue;
        }
        let metadata = fs::symlink_metadata(&path);
        op.check(OriginalAuthorityStage::Source)?;
        match metadata {
            Ok(value) if value.is_symlink() => return Ok(true),
            Ok(_) => (),
            Err(error)
                if error
                    .raw_os_error()
                    .is_some_and(pathlib_suppressed_stat_errno) => {}
            Err(error) => return Err(prefix_io(error, &path)),
        }
    }
    Ok(false)
}

struct Pending {
    text: CoreBuffer,
    offset: Option<usize>,
    link: Option<usize>,
}

impl Pending {
    fn next(&mut self) -> Option<&[u8]> {
        let start = self.offset?;
        let remaining = &self.text.as_bytes()[start..];
        let end = remaining.iter().position(|&byte| byte == b'/');
        self.offset = end.map(|end| start + end + 1);
        Some(&remaining[..end.unwrap_or(remaining.len())])
    }
}

struct Link {
    path: CoreBuffer,
    resolved: Option<CoreBuffer>,
}

struct FundedVec<T> {
    values: Vec<T>,
    capacity: NativeSetupReservation,
}

impl<T> FundedVec<T> {
    fn new<S: crate::BootstrapReviewOwner>(op: &Operation<'_, S>) -> Result<Self> {
        Ok(Self {
            values: Vec::new(),
            capacity: op.reserve(OriginalAuthorityStage::Source, 1)?,
        })
    }

    fn push(&mut self, value: T, budget: &Arc<dyn AllocationBudget>) -> Result<()> {
        if self.values.len() == self.values.capacity() {
            let old = self.values.capacity();
            let next = old.max(1).checked_mul(2).ok_or_else(capacity_error)?;
            let bytes = next
                .checked_mul(size_of::<T>())
                .ok_or_else(capacity_error)?;
            let peak = (old * size_of::<T>())
                .checked_add(bytes)
                .ok_or_else(capacity_error)?;
            charge_work(budget, peak as u64)?;
            self.capacity
                .resize(peak as u64)
                .map_err(|_| capacity_error())?;
            self.values
                .try_reserve_exact(next - self.values.len())
                .map_err(|_| capacity_error())?;
            if self.values.capacity() != next {
                return Err(capacity_error());
            }
            self.capacity
                .resize(bytes as u64)
                .map_err(|_| capacity_error())?;
        }
        self.values.push(value);
        Ok(())
    }
}

fn capacity_error() -> OriginalAuthorityError {
    OriginalAuthorityError::capacity(OriginalAuthorityStage::Source)
}

fn copy(budget: &Arc<dyn AllocationBudget>, bytes: &[u8]) -> Result<CoreBuffer> {
    CoreBuffer::copy_from(bytes, budget).map_err(|_| capacity_error())
}

fn prefix_io(error: std::io::Error, path: &Path) -> OriginalAuthorityError {
    match path.to_str() {
        Some(path) => raw_io(error, Some(path)),
        None => OriginalAuthorityError::unsupported(
            OriginalAuthorityStage::Source,
            "catalog OSError filename is not UTF-8",
        ),
    }
}

fn read_link<S: crate::BootstrapReviewOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    path: &Path,
) -> Result<CoreBuffer> {
    // CPython passes MAXPATHLEN to readlink. The catalog's private path scratch
    // also covers this full-path syscall and its NUL-terminated argument.
    let mut target = [0_u8; libc::PATH_MAX as usize];
    let name = std::ffi::CString::new(path.as_os_str().as_bytes())
        .map_err(|_| OriginalAuthorityError::unavailable())?;
    op.check(OriginalAuthorityStage::Source)?;
    charge_work(budget, name.as_bytes().len() as u64 + 1)?;
    let size = unsafe { libc::readlink(name.as_ptr(), target.as_mut_ptr().cast(), target.len()) };
    let error = (size < 0).then(std::io::Error::last_os_error);
    op.check(OriginalAuthorityStage::Source)?;
    if let Some(error) = error {
        return Err(prefix_io(error, path));
    }
    copy(budget, &target[..size as usize])
}

fn count_parts(bytes: &[u8], budget: &Arc<dyn AllocationBudget>) -> Result<usize> {
    charge_work(budget, bytes.len() as u64)?;
    Ok(1 + bytes.iter().filter(|&&byte| byte == b'/').count())
}

/// Resolve the existing absolute member as pinned CPython's strict realpath.
pub(super) fn resolve_member<S: crate::BootstrapReviewOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    lexical: &Path,
) -> Result<PathBuf> {
    let lexical = lexical.as_os_str().as_bytes();
    if !lexical.starts_with(b"/") {
        return Err(OriginalAuthorityError::unavailable());
    }
    let mut pending = FundedVec::new(op)?;
    let mut seen: FundedVec<Link> = FundedVec::new(op)?;
    let mut remaining = count_parts(lexical, budget)?;
    pending.push(
        Pending {
            text: copy(budget, lexical)?,
            offset: Some(0),
            link: None,
        },
        budget,
    )?;
    let mut path = PathBuf::from("/");

    while remaining != 0 {
        op.check(OriginalAuthorityStage::Source)?;
        let frame = pending.values.last_mut().expect("pending path segment");
        let Some(name) = frame.next() else {
            let index = frame.link.expect("completed symlink target");
            seen.values[index].resolved = Some(copy(budget, path.as_os_str().as_bytes())?);
            pending.values.pop();
            continue;
        };
        remaining -= 1;
        if matches!(name, b"" | b".") {
            continue;
        }
        if name == b".." {
            path.pop();
            continue;
        }
        if name.contains(&0) {
            return Err(json::raw_error(
                "builtins.ValueError",
                "lstat: embedded null character in path",
            ));
        }
        // Each successful syscall prefix fits the OS path bound. The next
        // name comes from the bounded input or from one bounded readlink.
        charge_work(budget, (path.as_os_str().len() + name.len() + 1) as u64)?;
        path.push(OsStr::from_bytes(name));
        let metadata = fs::symlink_metadata(&path);
        op.check(OriginalAuthorityStage::Source)?;
        let metadata = metadata.map_err(|error| prefix_io(error, &path))?;
        if !metadata.is_symlink() {
            if remaining != 0 && !metadata.is_dir() {
                return Err(prefix_io(
                    std::io::Error::from_raw_os_error(libc::ENOTDIR),
                    &path,
                ));
            }
            continue;
        }

        let bytes = path.as_os_str().as_bytes();
        charge_work(
            budget,
            (seen.values.len() as u64).saturating_mul(bytes.len() as u64 + 1),
        )?;
        if let Some(link) = seen
            .values
            .iter()
            .find(|link| link.path.as_bytes() == bytes)
        {
            if let Some(resolved) = &link.resolved {
                charge_work(budget, resolved.len() as u64)?;
                path.clear();
                path.push(OsStr::from_bytes(resolved.as_bytes()));
            } else {
                // Strict realpath follows a pending loop once to obtain the
                // actual error at this prefix. Its stat can also see a rename.
                op.check(OriginalAuthorityStage::Source)?;
                charge_work(budget, bytes.len() as u64 + 1)?;
                let followed = fs::metadata(&path);
                op.check(OriginalAuthorityStage::Source)?;
                followed.map_err(|error| prefix_io(error, &path))?;
            }
            continue;
        }

        let target = read_link(op, budget, &path)?;
        remaining = remaining
            .checked_add(count_parts(target.as_bytes(), budget)?)
            .ok_or_else(capacity_error)?;
        let index = seen.values.len();
        seen.push(
            Link {
                path: copy(budget, bytes)?,
                resolved: None,
            },
            budget,
        )?;
        if target.as_bytes().starts_with(b"/") {
            path.clear();
            path.push("/");
        } else {
            path.pop();
        }
        pending.push(
            Pending {
                text: target,
                offset: Some(0),
                link: Some(index),
            },
            budget,
        )?;
    }
    Ok(path)
}
