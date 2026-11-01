//! Producer-only immutable input capture. No native source is reopened by WASI.
//! Unix capture uses no-follow, descriptor-relative traversal at every component.
use anyhow::{Result, bail, ensure};
use bytes::Bytes;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    path::{Path, PathBuf},
};

pub const MAX_ENTRIES: usize = 32_768;
pub const MAX_TOTAL_BYTES: usize = 128 * 1024 * 1024;
pub const MAX_FILE_BYTES: usize = 8 * 1024 * 1024;
pub const MAX_METADATA_BYTES: usize = 4 * 1024 * 1024;
pub const MAX_PATH_BYTES: usize = 4096;
pub const MAX_READ_BYTES: usize = 1024 * 1024;

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum Kind {
    File,
    Directory,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Entry {
    pub guest_path: String,
    pub kind: Kind,
    pub size: u64,
    /// Files: exact content digest; directories: sha256 of empty content.
    pub sha256: String,
}

pub struct Mount {
    pub guest_path: String,
    pub source: PathBuf,
}
pub struct Node {
    pub entry: Entry,
    pub bytes: Bytes,
    pub inode: u64,
}
pub struct CapturedInputs {
    pub nodes: BTreeMap<String, Node>,
    pub roots: Vec<String>,
    pub epoch: u64,
}

pub fn canonical_path(path: &str) -> Result<()> {
    ensure!(
        !path.is_empty()
            && path.len() <= MAX_PATH_BYTES
            && !path.contains(['\\', '\0'])
            && !path.starts_with('/'),
        "invalid captured guest path"
    );
    ensure!(
        path.split('/')
            .all(|part| !part.is_empty() && part != "." && part != ".."),
        "noncanonical captured path"
    );
    ensure!(path.split('/').count() <= 32, "captured path depth");
    Ok(())
}

impl CapturedInputs {
    pub fn capture(expected: &[Entry], mounts: &[Mount], epoch: u64) -> Result<Self> {
        ensure!(
            epoch < 1u64 << 63 && !mounts.is_empty() && mounts.len() <= 128,
            "capture limits"
        );
        ensure!(
            expected.len() <= MAX_ENTRIES
                && serde_json::to_vec(expected)?.len() <= MAX_METADATA_BYTES,
            "capture metadata limit"
        );
        let mut declaration = BTreeMap::new();
        for entry in expected {
            canonical_path(&entry.guest_path)?;
            ensure!(
                entry.sha256.len() == 64
                    && entry
                        .sha256
                        .bytes()
                        .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)),
                "capture digest spelling"
            );
            ensure!(entry.size <= MAX_FILE_BYTES as u64, "capture file limit");
            ensure!(
                entry.kind != Kind::Directory
                    || (entry.size == 0 && entry.sha256 == hex_digest(&[])),
                "directory declaration"
            );
            ensure!(
                declaration
                    .insert(entry.guest_path.clone(), entry.clone())
                    .is_none(),
                "duplicate captured path"
            );
        }
        ensure!(
            expected
                .windows(2)
                .all(|pair| pair[0].guest_path < pair[1].guest_path),
            "manifest order"
        );
        let mut roots = BTreeSet::new();
        for mount in mounts {
            canonical_path(&mount.guest_path)?;
            ensure!(roots.insert(mount.guest_path.clone()), "duplicate mount");
        }
        let mut nodes = BTreeMap::new();
        let mut total = 0usize;
        for mount in mounts {
            capture_root(mount, &roots, &declaration, &mut nodes, &mut total)?;
        }
        ensure!(
            nodes.len() == declaration.len() && nodes.keys().eq(declaration.keys()),
            "missing/undeclared capture entry"
        );
        let mut seen_inodes = BTreeSet::new();
        for (index, (path, node)) in nodes.iter_mut().enumerate() {
            if let Some((parent, _)) = path.rsplit_once('/') {
                ensure!(
                    declaration
                        .get(parent)
                        .is_some_and(|entry| entry.kind == Kind::Directory),
                    "missing captured parent"
                );
            }
            node.inode = u64::try_from(index)?
                .checked_add(1)
                .ok_or_else(|| anyhow::anyhow!("inode exhausted"))?;
            ensure!(seen_inodes.insert(node.inode), "captured inode collision");
        }
        Ok(Self {
            nodes,
            roots: roots.into_iter().collect(),
            epoch,
        })
    }

    pub fn child_path(&self, base: &str, relative: &str) -> Result<String> {
        // WASI open-at commonly uses "."; it names exactly the current captured
        // directory. Parent traversal and absolute paths are always rejected.
        if relative == "." {
            return Ok(base.to_owned());
        }
        canonical_path(relative)?;
        let path = format!("{base}/{relative}");
        canonical_path(&path)?;
        ensure!(self.nodes.contains_key(&path), "undeclared captured path");
        Ok(path)
    }
}

fn hex_digest(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

#[cfg(any(target_os = "macos", target_os = "linux"))]
pub(crate) fn physical_directory(path: &Path) -> Result<std::os::fd::OwnedFd> {
    use std::{
        ffi::CString,
        os::{
            fd::{AsRawFd, FromRawFd, OwnedFd},
            unix::ffi::OsStrExt,
        },
        path::Component,
    };
    ensure!(
        path.is_absolute(),
        "capture source must be an explicit physical absolute path"
    );
    let root = unsafe {
        libc::open(
            c"/".as_ptr(),
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_CLOEXEC | libc::O_NOFOLLOW,
        )
    };
    ensure!(root >= 0, "capture root open failed");
    let mut fd = unsafe { OwnedFd::from_raw_fd(root) };
    for component in path.components() {
        let part = match component {
            Component::RootDir => continue,
            Component::Normal(part) => part,
            _ => bail!("noncanonical capture source"),
        };
        let name = CString::new(part.as_bytes())?;
        let next = unsafe {
            libc::openat(
                fd.as_raw_fd(),
                name.as_ptr(),
                libc::O_RDONLY | libc::O_DIRECTORY | libc::O_CLOEXEC | libc::O_NOFOLLOW,
            )
        };
        ensure!(next >= 0, "capture source component denied");
        fd = unsafe { OwnedFd::from_raw_fd(next) };
    }
    Ok(fd)
}

#[cfg(any(target_os = "macos", target_os = "linux"))]
fn capture_root(
    mount: &Mount,
    roots: &BTreeSet<String>,
    expected: &BTreeMap<String, Entry>,
    nodes: &mut BTreeMap<String, Node>,
    total: &mut usize,
) -> Result<()> {
    walk(
        physical_directory(&mount.source)?,
        &mount.guest_path,
        &mount.guest_path,
        roots,
        expected,
        nodes,
        total,
    )
}

#[cfg(not(any(target_os = "macos", target_os = "linux")))]
fn capture_root(
    _: &Mount,
    _: &BTreeSet<String>,
    _: &BTreeMap<String, Entry>,
    _: &mut BTreeMap<String, Node>,
    _: &mut usize,
) -> Result<()> {
    bail!(
        "Unavailable: producer no-follow capture requires audited Unix handles (CAPTURE-WINDOWS-01)"
    )
}

#[cfg(any(target_os = "macos", target_os = "linux"))]
pub(crate) fn read_bounded(path: &Path, bound: usize) -> Result<Vec<u8>> {
    use std::{
        ffi::CString,
        io::Read,
        os::{
            fd::{AsRawFd, FromRawFd, OwnedFd},
            unix::ffi::OsStrExt,
        },
    };
    let absolute = std::path::absolute(path)?;
    let parent = physical_directory(
        absolute
            .parent()
            .ok_or_else(|| anyhow::anyhow!("file parent required"))?,
    )?;
    let name = CString::new(
        absolute
            .file_name()
            .ok_or_else(|| anyhow::anyhow!("file name required"))?
            .as_bytes(),
    )?;
    let descriptor = unsafe {
        libc::openat(
            parent.as_raw_fd(),
            name.as_ptr(),
            libc::O_RDONLY | libc::O_CLOEXEC | libc::O_NOFOLLOW | libc::O_NONBLOCK,
        )
    };
    ensure!(descriptor >= 0, "bounded input open denied");
    let mut file = std::fs::File::from(unsafe { OwnedFd::from_raw_fd(descriptor) });
    let before = file.metadata()?;
    ensure!(
        before.is_file() && before.len() <= bound as u64,
        "bounded input type/size"
    );
    let mut bytes = Vec::new();
    (&mut file).take(bound as u64 + 1).read_to_end(&mut bytes)?;
    let after = file.metadata()?;
    ensure!(
        bytes.len() <= bound
            && after.len() == before.len()
            && before.modified()? == after.modified()?,
        "bounded input changed"
    );
    Ok(bytes)
}

#[cfg(not(any(target_os = "macos", target_os = "linux")))]
pub(crate) fn read_bounded(_: &Path, _: usize) -> Result<Vec<u8>> {
    bail!("Unavailable: audited no-follow input reader requires macOS/Linux")
}

#[cfg(any(target_os = "macos", target_os = "linux"))]
fn walk(
    fd: std::os::fd::OwnedFd,
    guest: &str,
    own_root: &str,
    roots: &BTreeSet<String>,
    expected: &BTreeMap<String, Entry>,
    nodes: &mut BTreeMap<String, Node>,
    total: &mut usize,
) -> Result<()> {
    use std::{
        ffi::CString,
        io::Read,
        os::fd::{AsRawFd, FromRawFd, OwnedFd},
    };
    let declaration = expected
        .get(guest)
        .ok_or_else(|| anyhow::anyhow!("undeclared captured directory"))?;
    ensure!(declaration.kind == Kind::Directory, "captured type differs");
    ensure!(
        nodes
            .insert(
                guest.to_owned(),
                Node {
                    entry: declaration.clone(),
                    bytes: Bytes::new(),
                    inode: 0
                }
            )
            .is_none(),
        "capture path collision"
    );
    let before = directory_names(fd.as_raw_fd())?;
    for name in &before {
        let path = format!("{guest}/{name}");
        canonical_path(&path)?;
        // Generated nested mounts replace their whole declared subtree before
        // capture. They are visited exactly once by their own mount, not merged
        // with mutable pregenerated bindings in a parent source directory.
        if path != own_root && roots.contains(&path) {
            continue;
        }
        let declaration = expected
            .get(&path)
            .ok_or_else(|| anyhow::anyhow!("undeclared capture entry"))?;
        let name = CString::new(name.as_bytes())?;
        let flags = libc::O_RDONLY
            | libc::O_NOFOLLOW
            | libc::O_CLOEXEC
            | libc::O_NONBLOCK
            | if declaration.kind == Kind::Directory {
                libc::O_DIRECTORY
            } else {
                0
            };
        let child = unsafe { libc::openat(fd.as_raw_fd(), name.as_ptr(), flags) };
        ensure!(child >= 0, "capture entry denied");
        let child = unsafe { OwnedFd::from_raw_fd(child) };
        if declaration.kind == Kind::Directory {
            walk(child, &path, own_root, roots, expected, nodes, total)?;
            continue;
        }
        let mut file = std::fs::File::from(child);
        let before = file.metadata()?;
        ensure!(
            before.is_file() && before.len() == declaration.size,
            "capture file type/size differs"
        );
        let size = usize::try_from(declaration.size)?;
        *total = total
            .checked_add(size)
            .ok_or_else(|| anyhow::anyhow!("capture total overflow"))?;
        ensure!(*total <= MAX_TOTAL_BYTES, "capture total limit");
        let mut bytes = Vec::with_capacity(size);
        (&mut file)
            .take(u64::try_from(size)?.checked_add(1).unwrap())
            .read_to_end(&mut bytes)?;
        let after = file.metadata()?;
        ensure!(
            bytes.len() == size
                && after.len() == before.len()
                && after.modified()? == before.modified()?
                && hex_digest(&bytes) == declaration.sha256,
            "capture content changed or differs"
        );
        ensure!(
            nodes
                .insert(
                    path,
                    Node {
                        entry: declaration.clone(),
                        bytes: Bytes::from(bytes),
                        inode: 0
                    }
                )
                .is_none(),
            "capture path collision"
        );
    }
    ensure!(
        directory_names(fd.as_raw_fd())? == before,
        "capture directory changed"
    );
    Ok(())
}

#[cfg(any(target_os = "macos", target_os = "linux"))]
fn directory_names(fd: std::os::fd::RawFd) -> Result<Vec<String>> {
    use std::ffi::CStr;
    let duplicate = unsafe { libc::dup(fd) };
    ensure!(duplicate >= 0, "capture directory duplicate failed");
    let pointer = unsafe { libc::fdopendir(duplicate) };
    if pointer.is_null() {
        unsafe { libc::close(duplicate) };
        bail!("capture directory iterator failed");
    }
    struct Directory(*mut libc::DIR);
    impl Drop for Directory {
        fn drop(&mut self) {
            unsafe { libc::closedir(self.0) };
        }
    }
    let directory = Directory(pointer);
    unsafe { libc::rewinddir(directory.0) };
    let mut names = Vec::new();
    loop {
        #[cfg(target_os = "macos")]
        unsafe {
            *libc::__error() = 0
        };
        #[cfg(target_os = "linux")]
        unsafe {
            *libc::__errno_location() = 0
        };
        let entry = unsafe { libc::readdir(directory.0) };
        if entry.is_null() {
            #[cfg(target_os = "macos")]
            ensure!(
                unsafe { *libc::__error() } == 0,
                "capture directory read failed"
            );
            #[cfg(target_os = "linux")]
            ensure!(
                unsafe { *libc::__errno_location() } == 0,
                "capture directory read failed"
            );
            break;
        }
        let name = unsafe { CStr::from_ptr((*entry).d_name.as_ptr()) }.to_str()?;
        if name == "." || name == ".." {
            continue;
        }
        ensure!(
            !name.is_empty() && !name.contains('/'),
            "capture entry name"
        );
        names.push(name.to_owned());
        ensure!(names.len() <= MAX_ENTRIES, "capture listing limit");
    }
    names.sort();
    ensure!(
        names.windows(2).all(|pair| pair[0] != pair[1]),
        "duplicate directory entry"
    );
    Ok(names)
}
