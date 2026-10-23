//! Stable reads of hash-addressed files beneath an opened source directory.
use crate::CanonError;
use std::{io::Read, path::Path};

/// Read one bounded regular file without following any child symlink.
/// Directory descriptors retain the root even if an ancestor is renamed.
pub fn read_relative(root: &Path, relative: &str, max_bytes: u64) -> Result<Vec<u8>, CanonError> {
    read_relative_with(root, relative, max_bytes, |reader, size| {
        let length = usize::try_from(size).map_err(|_| CanonError::Limit("artifact size"))?;
        let mut content = Vec::new();
        content
            .try_reserve_exact(length)
            .map_err(|_| CanonError::Limit("artifact allocation"))?;
        let mut chunk = [0u8; 8192];
        loop {
            let count = match reader.read(&mut chunk) {
                Ok(count) => count,
                Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(error) => {
                    return Err(CanonError::Invalid(format!("artifact IO: {error}")));
                }
            };
            if count == 0 {
                return Ok(content);
            }
            if count > length - content.len() {
                return Err(CanonError::Invalid(
                    "artifact changed during admission".into(),
                ));
            }
            content.extend_from_slice(&chunk[..count]);
        }
    })
}

#[cfg(unix)]
struct CountedReader<R> {
    inner: R,
    read: u64,
}

#[cfg(unix)]
impl<R: Read> Read for CountedReader<R> {
    fn read(&mut self, target: &mut [u8]) -> std::io::Result<usize> {
        let count = self.inner.read(target)?;
        self.read += count as u64;
        Ok(count)
    }
}

/// Inspect a bounded regular file through its pinned descriptor, before
/// deciding whether owned bytes must be allocated. The trusted native reader
/// cannot seek, reopen, or obtain the descriptor through this interface.
///
/// The result is not admitted until this function returns successfully:
/// remaining input is drained and device/inode/mode/size/timestamps are checked.
/// Callers must not publish or cache unverified results inside the callback.
#[cfg(unix)]
pub fn read_relative_with<T>(
    root: &Path,
    relative: &str,
    max_bytes: u64,
    inspect: impl FnOnce(&mut dyn Read, u64) -> Result<T, CanonError>,
) -> Result<T, CanonError> {
    use std::{
        ffi::CString,
        fs::{File, OpenOptions},
        os::fd::{AsRawFd, FromRawFd},
        os::unix::fs::{MetadataExt, OpenOptionsExt},
    };
    let fail = |e: std::io::Error| CanonError::Invalid(format!("artifact IO: {e}"));
    let parts: Vec<_> = relative.split('/').collect();
    if parts.is_empty()
        || parts
            .iter()
            .any(|p| p.is_empty() || *p == "." || *p == ".." || p.contains(['\\', '\0', ':']))
    {
        return Err(CanonError::Invalid("unsafe relative artifact path".into()));
    }
    let mut options = OpenOptions::new();
    options
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_DIRECTORY | libc::O_CLOEXEC);
    let mut directory = options.open(root).map_err(fail)?;
    for (index, part) in parts.iter().enumerate() {
        let name =
            CString::new(*part).map_err(|_| CanonError::Invalid("invalid artifact path".into()))?;
        let leaf = index + 1 == parts.len();
        let flags = libc::O_RDONLY
            | libc::O_NOFOLLOW
            | libc::O_CLOEXEC
            | libc::O_NONBLOCK
            | if leaf { 0 } else { libc::O_DIRECTORY };
        // SAFETY: the directory descriptor remains owned, and name is NUL terminated.
        let fd = unsafe { libc::openat(directory.as_raw_fd(), name.as_ptr(), flags) };
        if fd < 0 {
            return Err(fail(std::io::Error::last_os_error()));
        }
        // SAFETY: successful openat returns a fresh descriptor, owned exactly once.
        let mut file = unsafe { File::from_raw_fd(fd) };
        if !leaf {
            directory = file;
            continue;
        }
        let before = file.metadata().map_err(fail)?;
        if !before.is_file() || before.len() > max_bytes {
            return Err(CanonError::Invalid(
                "artifact is not a bounded regular file".into(),
            ));
        }
        let (result, bytes_read) = {
            let mut reader = CountedReader {
                inner: (&mut file).take(before.len().saturating_add(1)),
                read: 0,
            };
            let result = inspect(&mut reader, before.len())?;
            // Even a duplicate/early-returning consumer must observe the exact
            // physical length. This fixed-buffer drain cannot allocate a
            // second copy of an artifact just to reach EOF.
            std::io::copy(&mut reader, &mut std::io::sink()).map_err(fail)?;
            (result, reader.read)
        };
        let after = file.metadata().map_err(fail)?;
        let identity = |m: &std::fs::Metadata| {
            (
                m.dev(),
                m.ino(),
                m.mode(),
                m.len(),
                m.mtime(),
                m.mtime_nsec(),
                m.ctime(),
                m.ctime_nsec(),
            )
        };
        if bytes_read != before.len() || identity(&before) != identity(&after) {
            return Err(CanonError::Invalid(
                "artifact changed during admission".into(),
            ));
        }
        return Ok(result);
    }
    Err(CanonError::Invalid("missing artifact leaf".into()))
}

/// No path-based fallback is permitted where descriptor-relative no-follow
/// reads are unavailable. In-memory .tpk verification remains portable.
#[cfg(not(unix))]
pub fn read_relative_with<T>(
    _root: &Path,
    _relative: &str,
    _max_bytes: u64,
    _inspect: impl FnOnce(&mut dyn Read, u64) -> Result<T, CanonError>,
) -> Result<T, CanonError> {
    Err(CanonError::Invalid(
        "secure directory admission is unavailable on this platform; use a verified .tpk".into(),
    ))
}
