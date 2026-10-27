//! Descriptor-relative opening of a Core-selected declared artifact.
//!
//! The caller has already selected the real source root and declared relative
//! name, decided policy, and prepaid the scratch and file custody. This module
//! grants no authority. The root is bound as opened here, not to an earlier
//! resolver inode: resolving its absolute path can still follow ancestor
//! symlinks. Only the subsequent relative walk is confined by held directory
//! descriptors and `O_NOFOLLOW` on every component.

use std::{
    ffi::CStr,
    fs::File,
    io,
    mem::MaybeUninit,
    os::{
        fd::{AsRawFd, FromRawFd, RawFd},
        unix::{ffi::OsStrExt, fs::MetadataExt},
    },
    path::{Component, Path},
};
use tobkiri_wasm::{NativeDataIoError, NativeInspectionCheckpoint};

/// Actual opened custody retained by the caller through its original Join.
pub(super) struct OpenedArtifact {
    pub(super) root: File,
    pub(super) file: File,
    pub(super) before: std::fs::Metadata,
}

/// Target syntax, original source IO, and original execution stay distinct.
pub(super) enum OpenFailure {
    InvalidTarget,
    SourceIo(io::Error),
    Execution(NativeDataIoError),
}

/// Open a selected root at the actual reachpoint, then walk its declared name.
///
/// `scratch` must already be funded and fit the root and each component plus a
/// terminating NUL. No owned path or C string is constructed. The caller must
/// also prepay custody before calling and retain the returned files and their
/// admission through actual physical completion.
pub(super) fn open_selected_file(
    root: &Path,
    relative: &Path,
    scratch: &mut [u8],
    checkpoint: &NativeInspectionCheckpoint<'_>,
) -> Result<OpenedArtifact, OpenFailure> {
    let root_bytes = root.as_os_str().as_bytes();
    let relative_bytes = relative.as_os_str().as_bytes();
    if !root.is_absolute()
        || root_bytes.contains(&0)
        || relative_bytes.is_empty()
        || relative_bytes
            .iter()
            .any(|&byte| byte == 0 || byte == b'\\')
        || relative
            .components()
            .any(|component| !matches!(component, Component::Normal(_)))
    {
        return Err(OpenFailure::InvalidTarget);
    }
    // Check all scratch bounds before any open, including the terminating NUL.
    if root_bytes.len() >= scratch.len()
        || relative
            .components()
            .any(|component| component.as_os_str().as_bytes().len() >= scratch.len())
    {
        return Err(OpenFailure::Execution(NativeDataIoError::Capacity));
    }

    let root_name = copy_c_path(root_bytes, scratch)?;
    let root = open_checked(
        None,
        root_name,
        libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
        checkpoint,
    )?;
    let mut parent: Option<File> = None;
    // Use the same Normal component interpretation as declaration validation.
    // Walking that name does not replace its actual declaration authority key.
    let mut components = relative.components().peekable();
    while let Some(component) = components.next() {
        let Component::Normal(name) = component else {
            // All components were validated before opening the selected root.
            return Err(OpenFailure::InvalidTarget);
        };
        let final_component = components.peek().is_none();
        let name = copy_c_path(name.as_bytes(), scratch)?;
        let parent_fd = parent.as_ref().unwrap_or(&root).as_raw_fd();
        if final_component {
            let before = stat_regular_checked(parent_fd, name, checkpoint)?;
            // NONBLOCK leaves regular-file reads unchanged and prevents a FIFO
            // replacement between the no-follow stat and open from hanging.
            let file = open_checked(
                Some(parent_fd),
                name,
                libc::O_RDONLY | libc::O_NONBLOCK | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                checkpoint,
            )?;
            checkpoint.charge_work(1).map_err(OpenFailure::Execution)?;
            let metadata = file.metadata();
            checkpoint.charge_work(0).map_err(OpenFailure::Execution)?;
            let metadata = metadata.map_err(OpenFailure::SourceIo)?;
            if !metadata.is_file() || !same_identity(&metadata, &before) {
                return Err(OpenFailure::InvalidTarget);
            }
            return Ok(OpenedArtifact {
                root,
                file,
                before: metadata,
            });
        }
        let flags = libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_DIRECTORY;
        let file = open_checked(Some(parent_fd), name, flags, checkpoint)?;
        // Replacing the intermediate File closes its descriptor exactly once.
        // Cleanup is unconditional even when an activity check has failed.
        parent = Some(file);
    }
    Err(OpenFailure::InvalidTarget)
}

// libc exposes different native widths/signs on Darwin and Linux. These casts
// deliberately match MetadataExt's u64 representation on each platform.
#[allow(clippy::unnecessary_cast)]
fn same_identity(metadata: &std::fs::Metadata, before: &libc::stat) -> bool {
    metadata.dev() == before.st_dev as u64 && metadata.ino() == before.st_ino as u64
}

fn copy_c_path<'scratch>(
    bytes: &[u8],
    scratch: &'scratch mut [u8],
) -> Result<&'scratch CStr, OpenFailure> {
    let length = bytes
        .len()
        .checked_add(1)
        .ok_or(OpenFailure::Execution(NativeDataIoError::Capacity))?;
    let target = scratch
        .get_mut(..length)
        .ok_or(OpenFailure::Execution(NativeDataIoError::Capacity))?;
    target[..bytes.len()].copy_from_slice(bytes);
    target[bytes.len()] = 0;
    CStr::from_bytes_with_nul(target).map_err(|_| OpenFailure::InvalidTarget)
}

fn stat_regular_checked(
    parent: RawFd,
    name: &CStr,
    checkpoint: &NativeInspectionCheckpoint<'_>,
) -> Result<libc::stat, OpenFailure> {
    loop {
        checkpoint.charge_work(1).map_err(OpenFailure::Execution)?;
        let mut metadata = MaybeUninit::<libc::stat>::uninit();
        // SAFETY: parent is borrowed from a live directory File, name is a
        // valid C string, and metadata is writable stat-sized storage.
        // AT_SYMLINK_NOFOLLOW inspects the final directory entry itself.
        let status = unsafe {
            libc::fstatat(
                parent,
                name.as_ptr(),
                metadata.as_mut_ptr(),
                libc::AT_SYMLINK_NOFOLLOW,
            )
        };
        let metadata = if status < 0 {
            Err(io::Error::last_os_error())
        } else {
            // SAFETY: successful fstatat initialized the stat output.
            Ok(unsafe { metadata.assume_init() })
        };
        checkpoint.charge_work(0).map_err(OpenFailure::Execution)?;
        let metadata = match metadata {
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            result => result.map_err(OpenFailure::SourceIo)?,
        };
        // Reject symlinks, directories, FIFOs, sockets, and devices BEFORE
        // attempting to open the selected final entry.
        if metadata.st_mode & libc::S_IFMT != libc::S_IFREG {
            return Err(OpenFailure::InvalidTarget);
        }
        return Ok(metadata);
    }
}

fn open_checked(
    parent: Option<RawFd>,
    name: &CStr,
    flags: libc::c_int,
    checkpoint: &NativeInspectionCheckpoint<'_>,
) -> Result<File, OpenFailure> {
    loop {
        checkpoint.charge_work(1).map_err(OpenFailure::Execution)?;
        // SAFETY: name is a borrowed, NUL-terminated string in prepaid scratch.
        // Any parent descriptor is borrowed from a live directory File. These
        // flags never create a file, so open/openat need no mode argument.
        let descriptor = unsafe {
            match parent {
                Some(parent) => libc::openat(parent, name.as_ptr(), flags),
                None => libc::open(name.as_ptr(), flags),
            }
        };
        let opened = if descriptor < 0 {
            // Capture errno immediately, before the post-syscall checkpoint.
            Err(io::Error::last_os_error())
        } else {
            // SAFETY: successful open/openat returned a new descriptor owned
            // only here. Wrap it before any fallible post-syscall operation.
            Ok(unsafe { File::from_raw_fd(descriptor) })
        };
        checkpoint.charge_work(0).map_err(OpenFailure::Execution)?;
        match opened {
            // Like File::open, retry only an open which returned EINTR and did
            // not deliver a descriptor. Each retry gets its own checkpoint.
            // File destruction never retries close: descriptor reuse makes a
            // failed close ambiguous on Unix.
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            result => return result.map_err(OpenFailure::SourceIo),
        }
    }
}
