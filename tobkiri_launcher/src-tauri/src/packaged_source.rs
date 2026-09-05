//! Trusted verifier and snapshotter for the packaged Defaults Python source closure.
//!
//! No Python code is imported until every declared source has been copied from
//! a digest-verified file handle into a private, link-free snapshot.

use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::path::{Component, Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::Value;
use sha2::{Digest, Sha256};

const SCHEMA: &str = "io.tobkiri.packaged-defaultspack-source.v1";
const ROOTS: &[&str] = &[
    "scripts",
    "tobkiri_protocol",
    "ecosystem/defaultspack/domain/runtime_v4",
    "ecosystem/defaultspack/v4",
    "ecosystem/defaultspack/runtime",
    "ecosystem/defaultspack/defaultspack",
];
const FILES: &[&str] = &[
    "ecosystem/defaultspack/pack.v4.json",
    "ecosystem/defaultspack/contracts.v4.json",
    "ecosystem/defaultspack/artifact-index.v4.json",
    "ecosystem/defaultspack/executables.v4.json",
    "ecosystem/defaultspack/host_contract_contributions.v1.json",
    "ecosystem/defaultspack/domain/runtime_surface_v4.py",
    "ecosystem/defaultspack/update_metadata.v1.json",
];
const MAX_MANIFEST_BYTES: u64 = 4 * 1024 * 1024;
const MAX_SOURCE_BYTES: u64 = 256 * 1024 * 1024;
const PROVENANCE_FILENAME: &str = "packaging-source-provenance.v1.json";
const MAX_PROVENANCE_BYTES: usize = 64 * 1024;

#[cfg(unix)]
struct ProvisionalFileGuard<'a> {
    root: &'a File,
    file: File,
    name: &'static str,
    armed: bool,
}

#[cfg(unix)]
impl ProvisionalFileGuard<'_> {
    fn cleanup_inner(&mut self) -> io::Result<()> {
        use std::os::unix::fs::PermissionsExt;
        self.root
            .set_permissions(fs::Permissions::from_mode(0o700))?;
        let unlink = (|| {
            let current =
                openat_nofollow(self.root, std::ffi::OsStr::new(self.name), libc::O_RDONLY)?;
            if !same_object(&current.metadata()?, identity(&self.file.metadata()?)) {
                return Err(invalid("refusing to unlink replaced provisional file"));
            }
            unlinkat_name(self.root, std::ffi::OsStr::new(self.name), 0)
        })();
        let reseal = self.root.set_permissions(fs::Permissions::from_mode(0o500));
        match (unlink, reseal) {
            (Ok(()), Ok(())) => {
                self.armed = false;
                Ok(())
            }
            (Err(error), Ok(())) | (Ok(()), Err(error)) => Err(error),
            (Err(unlink), Err(reseal)) => Err(invalid(format!(
                "provisional unlink failed: {unlink}; reseal also failed: {reseal}"
            ))),
        }
    }
}

#[cfg(unix)]
impl Drop for ProvisionalFileGuard<'_> {
    fn drop(&mut self) {
        if self.armed {
            let _ = self.cleanup_inner();
        }
    }
}

fn invalid(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message.into())
}

#[derive(Debug, Clone)]
struct ExpectedFile {
    size: u64,
    sha256: String,
    executable: bool,
}

/// A verified source tree. Its directory is removed when the value is dropped.
#[derive(Debug)]
pub struct VerifiedSourceSnapshot {
    owner: PathBuf,
    root: PathBuf,
    owner_identity: (u64, u64, u64, i64, i64),
    root_identity: (u64, u64, u64, i64, i64),
    root_handle: File,
    #[cfg(unix)]
    directory_identities: BTreeMap<String, (u64, u64, u64, i64, i64)>,
    #[cfg(unix)]
    parent_handle: File,
    #[cfg(unix)]
    owner_handle: File,
    #[cfg(unix)]
    owner_name: std::ffi::OsString,
    trusted_manifest: Vec<u8>,
    provenance: Option<Vec<u8>>,
    cleanup_attempted: bool,
}

impl VerifiedSourceSnapshot {
    pub fn root(&self) -> &Path {
        &self.root
    }

    pub fn bind_command_cwd(
        &self,
        command: &mut super::packaging_toolchain::VerifiedCommand<'_>,
    ) -> io::Result<()> {
        #[cfg(unix)]
        {
            command.current_dir_handle(&self.root_handle)?;
        }
        #[cfg(not(unix))]
        {
            command.current_dir(&self.root);
        }
        Ok(())
    }
}

impl Drop for VerifiedSourceSnapshot {
    fn drop(&mut self) {
        if !self.cleanup_attempted {
            let _ = self.cleanup_inner();
        }
    }
}

impl VerifiedSourceSnapshot {
    pub fn verify_unchanged(&self) -> io::Result<()> {
        if identity(&self.root_handle.metadata()?) != self.root_identity
            || identity(&fs::symlink_metadata(&self.root)?) != self.root_identity
            || identity(&fs::symlink_metadata(&self.owner)?) != self.owner_identity
        {
            return Err(invalid("verified source snapshot root identity changed"));
        }
        #[cfg(unix)]
        verify_snapshot_at(
            &self.root_handle,
            &self.trusted_manifest,
            self.provenance.as_deref(),
            &self.directory_identities,
        )?;
        #[cfg(not(unix))]
        verify_snapshot(&self.root, &self.trusted_manifest)?;
        if let Some(expected) = &self.provenance {
            let actual = read_manifest(&self.root.join(PROVENANCE_FILENAME))?;
            if &actual != expected {
                return Err(invalid("verified source provenance changed"));
            }
        }
        Ok(())
    }

    pub fn bind_provenance(&mut self, bytes: &[u8]) -> io::Result<PathBuf> {
        if self.provenance.is_some() || bytes.is_empty() || bytes.len() > MAX_PROVENANCE_BYTES {
            return Err(invalid("source provenance binding is invalid"));
        }
        #[cfg(unix)]
        let result = (|| -> io::Result<ProvisionalFileGuard<'_>> {
            use std::os::unix::fs::PermissionsExt;
            self.root_handle
                .set_permissions(fs::Permissions::from_mode(0o700))?;
            let file = create_root_file(
                &self.root_handle,
                std::ffi::OsStr::new(PROVENANCE_FILENAME),
                0o400,
            )?;
            let guard = ProvisionalFileGuard {
                root: &self.root_handle,
                file,
                name: PROVENANCE_FILENAME,
                armed: true,
            };
            Ok(guard)
        })();
        let path = self.root.join(PROVENANCE_FILENAME);
        #[cfg(unix)]
        let mut provisional = match result {
            Ok(value) => value,
            Err(primary) => {
                use std::os::unix::fs::PermissionsExt;
                return match self
                    .root_handle
                    .set_permissions(fs::Permissions::from_mode(0o500))
                {
                    Ok(()) => Err(primary),
                    Err(cleanup) => Err(invalid(format!(
                        "{primary}; provenance root reseal also failed: {cleanup}"
                    ))),
                };
            }
        };
        let result = (|| -> io::Result<()> {
            #[cfg(not(unix))]
            let mut output = OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&path)?;
            #[cfg(unix)]
            let output = &mut provisional.file;
            output.write_all(bytes)?;
            output.sync_all()?;
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                provisional
                    .file
                    .set_permissions(fs::Permissions::from_mode(0o400))?;
                self.root_handle
                    .set_permissions(fs::Permissions::from_mode(0o500))?;
            }
            Ok::<_, io::Error>(())
        })();
        if let Err(primary) = result {
            #[cfg(unix)]
            let cleanup = provisional.cleanup_inner();
            #[cfg(not(unix))]
            let cleanup = fs::remove_file(&path);
            return match cleanup {
                Ok(()) => {
                    #[cfg(unix)]
                    {
                        self.root_identity = identity(&self.root_handle.metadata()?);
                        self.directory_identities
                            .insert(String::new(), self.root_identity);
                    }
                    Err(primary)
                }
                Err(cleanup) => Err(invalid(format!(
                    "{primary}; provisional provenance cleanup also failed: {cleanup}"
                ))),
            };
        }
        #[cfg(unix)]
        let finalized = (|| -> io::Result<(
            (u64, u64, u64, i64, i64),
            BTreeMap<String, (u64, u64, u64, i64, i64)>,
        )> {
            let new_root_identity = identity(&self.root_handle.metadata()?);
            let provenance = openat_nofollow(
                &self.root_handle,
                std::ffi::OsStr::new(PROVENANCE_FILENAME),
                libc::O_RDONLY,
            )?;
            let provenance_identity = identity(&provenance.metadata()?);
            let mut candidate_inventory = self.directory_identities.clone();
            candidate_inventory.insert(String::new(), new_root_identity);
            candidate_inventory.insert(PROVENANCE_FILENAME.to_owned(), provenance_identity);
            verify_snapshot_at(
                &self.root_handle,
                &self.trusted_manifest,
                Some(bytes),
                &candidate_inventory,
            )?;
            Ok((new_root_identity, candidate_inventory))
        })();
        #[cfg(unix)]
        let (new_root_identity, candidate_inventory) = match finalized {
            Ok(value) => value,
            Err(primary) => {
                let cleanup = provisional.cleanup_inner();
                return match cleanup {
                    Ok(()) => {
                        let restored_identity = identity(&self.root_handle.metadata()?);
                        self.root_identity = restored_identity;
                        self.directory_identities
                            .insert(String::new(), restored_identity);
                        Err(primary)
                    }
                    Err(cleanup) => Err(invalid(format!(
                        "{primary}; finalized provenance rollback also failed: {cleanup}"
                    ))),
                };
            }
        };
        #[cfg(not(unix))]
        let new_root_identity = identity(&self.root_handle.metadata()?);
        #[cfg(unix)]
        {
            self.directory_identities = candidate_inventory;
        }
        self.root_identity = new_root_identity;
        self.provenance = Some(bytes.to_vec());
        #[cfg(unix)]
        {
            provisional.armed = false;
        }
        Ok(path)
    }

    pub fn cleanup(mut self) -> io::Result<()> {
        self.cleanup_attempted = true;
        self.cleanup_inner()
    }

    fn cleanup_inner(&mut self) -> io::Result<()> {
        if identity(&self.root_handle.metadata()?) != self.root_identity
            || identity(&fs::symlink_metadata(&self.root)?) != self.root_identity
            || identity(&fs::symlink_metadata(&self.owner)?) != self.owner_identity
        {
            return Err(invalid(
                "refusing to clean a replaced verified source snapshot",
            ));
        }
        #[cfg(unix)]
        remove_snapshot_at(&self.root_handle, &self.directory_identities)?;
        #[cfg(not(unix))]
        {
            make_tree_owner_writable(&self.root)?;
            fs::remove_dir_all(&self.root)?;
        }
        #[cfg(unix)]
        {
            verify_named_identity(
                &self.owner_handle,
                std::ffi::OsStr::new("source"),
                self.root_identity,
            )?;
            unlinkat_name(
                &self.owner_handle,
                std::ffi::OsStr::new("source"),
                libc::AT_REMOVEDIR,
            )?;
            verify_named_identity(&self.parent_handle, &self.owner_name, self.owner_identity)?;
            unlinkat_name(&self.parent_handle, &self.owner_name, libc::AT_REMOVEDIR)?;
        }
        #[cfg(not(unix))]
        fs::remove_dir(&self.owner)?;
        Ok(())
    }
}

fn exact_keys(value: &serde_json::Map<String, Value>, expected: &[&str]) -> bool {
    value.keys().map(String::as_str).collect::<BTreeSet<_>>()
        == expected.iter().copied().collect::<BTreeSet<_>>()
}

fn safe_relative(value: &str) -> bool {
    !value.is_empty()
        && !value.contains(['\\', '\0'])
        && !value.starts_with('~')
        && !Path::new(value).is_absolute()
        && Path::new(value)
            .components()
            .all(|component| matches!(component, Component::Normal(_)))
        && Path::new(value).to_string_lossy().replace('\\', "/") == value
}

fn parse_manifest(bytes: &[u8]) -> io::Result<BTreeMap<String, ExpectedFile>> {
    let value: Value = serde_json::from_slice(bytes)
        .map_err(|error| invalid(format!("source manifest is malformed: {error}")))?;
    let object = value
        .as_object()
        .ok_or_else(|| invalid("source manifest must be an object"))?;
    if !exact_keys(object, &["schema", "roots", "files"]) {
        return Err(invalid("source manifest has unknown or missing fields"));
    }
    if object.get("schema").and_then(Value::as_str) != Some(SCHEMA) {
        return Err(invalid("source manifest schema marker is unknown"));
    }
    let roots = object
        .get("roots")
        .and_then(Value::as_array)
        .ok_or_else(|| invalid("source manifest roots must be an array"))?;
    if roots.len() != ROOTS.len()
        || roots
            .iter()
            .zip(ROOTS)
            .any(|(actual, expected)| actual.as_str() != Some(expected))
    {
        return Err(invalid(
            "source manifest roots differ from the trusted closure",
        ));
    }
    let files = object
        .get("files")
        .and_then(Value::as_array)
        .filter(|files| !files.is_empty())
        .ok_or_else(|| invalid("source manifest files must be a non-empty array"))?;
    let mut expected = BTreeMap::new();
    let mut previous: Option<&str> = None;
    for value in files {
        let entry = value
            .as_object()
            .ok_or_else(|| invalid("source manifest file entry must be an object"))?;
        if !exact_keys(entry, &["path", "type", "size", "sha256", "executable"]) {
            return Err(invalid(
                "source manifest file entry has unknown or missing fields",
            ));
        }
        let path = entry
            .get("path")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid("source manifest path must be a string"))?;
        if !safe_relative(path)
            || !ROOTS
                .iter()
                .any(|root| path.starts_with(&format!("{root}/")))
                && !FILES.contains(&path)
        {
            return Err(invalid(format!(
                "source manifest path is outside the closure: {path:?}"
            )));
        }
        if previous.is_some_and(|value| value >= path) {
            return Err(invalid("source manifest paths must be unique and sorted"));
        }
        previous = Some(path);
        if entry.get("type").and_then(Value::as_str) != Some("regular-file") {
            return Err(invalid(format!(
                "source manifest type is not regular-file: {path}"
            )));
        }
        let size = entry
            .get("size")
            .and_then(Value::as_u64)
            .filter(|size| *size <= MAX_SOURCE_BYTES)
            .ok_or_else(|| invalid(format!("source manifest size is invalid: {path}")))?;
        let sha256 = entry
            .get("sha256")
            .and_then(Value::as_str)
            .filter(|digest| {
                digest.len() == 64
                    && digest
                        .bytes()
                        .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
            })
            .ok_or_else(|| invalid(format!("source manifest SHA-256 is invalid: {path}")))?;
        let executable = entry
            .get("executable")
            .and_then(Value::as_bool)
            .ok_or_else(|| {
                invalid(format!(
                    "source manifest executable flag is invalid: {path}"
                ))
            })?;
        expected.insert(
            path.to_owned(),
            ExpectedFile {
                size,
                sha256: sha256.to_owned(),
                executable,
            },
        );
    }
    Ok(expected)
}

#[cfg(unix)]
fn identity(metadata: &fs::Metadata) -> (u64, u64, u64, i64, i64) {
    use std::os::unix::fs::MetadataExt;
    (
        metadata.dev(),
        metadata.ino(),
        metadata.len(),
        metadata.mtime_nsec(),
        metadata.ctime_nsec(),
    )
}

#[cfg(unix)]
fn same_object(metadata: &fs::Metadata, expected: (u64, u64, u64, i64, i64)) -> bool {
    use std::os::unix::fs::MetadataExt;
    metadata.dev() == expected.0 && metadata.ino() == expected.1
}

#[cfg(not(unix))]
fn identity(metadata: &fs::Metadata) -> (u64, u64, u64, i64, i64) {
    let modified = metadata
        .modified()
        .ok()
        .and_then(|time| time.duration_since(UNIX_EPOCH).ok())
        .map_or(0, |time| time.as_nanos() as i64);
    (0, 0, metadata.len(), modified, 0)
}

#[cfg(unix)]
fn reject_hardlink(metadata: &fs::Metadata, path: &Path) -> io::Result<()> {
    use std::os::unix::fs::MetadataExt;
    if metadata.nlink() != 1 {
        return Err(invalid(format!(
            "source closure contains a hardlink: {}",
            path.display()
        )));
    }
    Ok(())
}

#[cfg(windows)]
fn reject_hardlink(metadata: &fs::Metadata, path: &Path) -> io::Result<()> {
    use std::mem::MaybeUninit;
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::{
        GetFileInformationByHandle, BY_HANDLE_FILE_INFORMATION,
    };

    if !metadata.is_file() {
        return Ok(());
    }
    let file = File::open(path)?;
    let mut information = MaybeUninit::<BY_HANDLE_FILE_INFORMATION>::zeroed();
    if unsafe { GetFileInformationByHandle(file.as_raw_handle(), information.as_mut_ptr()) } == 0 {
        return Err(invalid(format!(
            "failed to inspect source closure links at {}: {}",
            path.display(),
            io::Error::last_os_error()
        )));
    }
    if unsafe { information.assume_init() }.nNumberOfLinks != 1 {
        return Err(invalid(format!(
            "source closure contains a hardlink: {}",
            path.display()
        )));
    }
    Ok(())
}

#[cfg(not(any(unix, windows)))]
fn reject_hardlink(_metadata: &fs::Metadata, _path: &Path) -> io::Result<()> {
    Ok(())
}

#[cfg(unix)]
fn executable(metadata: &fs::Metadata) -> bool {
    use std::os::unix::fs::PermissionsExt;
    metadata.permissions().mode() & 0o111 != 0
}

#[cfg(not(unix))]
fn executable(_metadata: &fs::Metadata) -> bool {
    false
}

fn collect_actual(root: &Path, current: &Path, actual: &mut BTreeSet<String>) -> io::Result<()> {
    let metadata = fs::symlink_metadata(current)?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        return Err(invalid(format!(
            "source closure root is not a real directory: {}",
            current.display()
        )));
    }
    let mut entries = fs::read_dir(current)?.collect::<Result<Vec<_>, _>>()?;
    entries.sort_by_key(fs::DirEntry::file_name);
    for entry in entries {
        let path = entry.path();
        let metadata = fs::symlink_metadata(&path)?;
        if metadata.file_type().is_symlink() {
            return Err(invalid(format!(
                "source closure contains a symlink: {}",
                path.display()
            )));
        }
        if metadata.is_dir() {
            collect_actual(root, &path, actual)?;
        } else if metadata.is_file() {
            reject_hardlink(&metadata, &path)?;
            let relative = path
                .strip_prefix(root)
                .map_err(|_| invalid("source closure path escaped its root"))?
                .to_string_lossy()
                .replace('\\', "/");
            if !actual.insert(relative) {
                return Err(invalid("source closure contains a duplicate path"));
            }
        } else {
            return Err(invalid(format!(
                "source closure contains a special entry: {}",
                path.display()
            )));
        }
    }
    Ok(())
}

struct SnapshotCreation {
    owner: PathBuf,
    root: PathBuf,
    #[cfg(unix)]
    parent_handle: File,
    #[cfg(unix)]
    owner_handle: File,
    #[cfg(unix)]
    root_handle: File,
    #[cfg(unix)]
    owner_name: std::ffi::OsString,
    #[cfg(unix)]
    owner_identity: (u64, u64, u64, i64, i64),
    #[cfg(unix)]
    root_identity: (u64, u64, u64, i64, i64),
    #[cfg(unix)]
    created_entries: BTreeMap<String, (u64, u64, u64, i64, i64)>,
    armed: bool,
}

#[cfg(unix)]
struct OwnerCreationGuard {
    parent: File,
    owner: Option<File>,
    name: std::ffi::OsString,
    identity: Option<(u64, u64, u64, i64, i64)>,
    root: Option<(File, (u64, u64, u64, i64, i64))>,
    root_created: bool,
    armed: bool,
}

#[cfg(unix)]
impl OwnerCreationGuard {
    fn cleanup_inner(&mut self) -> io::Result<()> {
        if let Some((root, root_identity)) = &self.root {
            let owner = self
                .owner
                .as_ref()
                .ok_or_else(|| invalid("snapshot owner handle is unavailable"))?;
            verify_named_identity(owner, std::ffi::OsStr::new("source"), *root_identity)?;
            use std::os::unix::fs::PermissionsExt;
            root.set_permissions(fs::Permissions::from_mode(0o700))?;
            unlinkat_name(owner, std::ffi::OsStr::new("source"), libc::AT_REMOVEDIR)?;
        } else if self.root_created {
            return Err(invalid(
                "snapshot root identity is unavailable; leaving fail-closed residue",
            ));
        }
        if self.identity.is_none() {
            return Err(invalid(
                "snapshot owner identity is unavailable; leaving fail-closed residue",
            ));
        }
        let identity = self
            .identity
            .expect("snapshot owner identity was populated");
        verify_named_identity(&self.parent, &self.name, identity)?;
        unlinkat_name(&self.parent, &self.name, libc::AT_REMOVEDIR)?;
        self.armed = false;
        Ok(())
    }
}

#[cfg(unix)]
struct CreatedDirectoryGuard<'a> {
    parent: &'a File,
    name: &'a std::ffi::OsStr,
    identity: Option<(u64, u64, u64, i64, i64)>,
    armed: bool,
}

#[cfg(unix)]
impl CreatedDirectoryGuard<'_> {
    fn cleanup_inner(&mut self) -> io::Result<()> {
        if let Some(expected) = self.identity {
            verify_named_identity(self.parent, self.name, expected)?;
        } else {
            return Err(invalid(
                "created directory identity is unavailable; leaving fail-closed residue",
            ));
        }
        unlinkat_name(self.parent, self.name, libc::AT_REMOVEDIR)?;
        self.armed = false;
        Ok(())
    }
}

#[cfg(unix)]
impl Drop for CreatedDirectoryGuard<'_> {
    fn drop(&mut self) {
        if self.armed {
            let _ = self.cleanup_inner();
        }
    }
}

#[cfg(not(unix))]
struct OwnerPathGuard {
    owner: PathBuf,
    root_created: bool,
    armed: bool,
}

#[cfg(not(unix))]
impl Drop for OwnerPathGuard {
    fn drop(&mut self) {
        if self.armed {
            if self.root_created {
                let _ = fs::remove_dir(self.owner.join("source"));
            }
            let _ = fs::remove_dir(&self.owner);
        }
    }
}

#[cfg(unix)]
impl Drop for OwnerCreationGuard {
    fn drop(&mut self) {
        if self.armed {
            let _ = self.cleanup_inner();
        }
    }
}

impl SnapshotCreation {
    fn cleanup_inner(&mut self) -> io::Result<()> {
        #[cfg(unix)]
        {
            remove_snapshot_at(&self.root_handle, &self.created_entries)?;
            verify_named_identity(
                &self.owner_handle,
                std::ffi::OsStr::new("source"),
                self.root_identity,
            )?;
            unlinkat_name(
                &self.owner_handle,
                std::ffi::OsStr::new("source"),
                libc::AT_REMOVEDIR,
            )?;
            verify_named_identity(&self.parent_handle, &self.owner_name, self.owner_identity)?;
            unlinkat_name(&self.parent_handle, &self.owner_name, libc::AT_REMOVEDIR)?;
        }
        #[cfg(not(unix))]
        {
            make_tree_owner_writable(&self.root)?;
            fs::remove_dir_all(&self.root)?;
            fs::remove_dir(&self.owner)?;
        }
        self.armed = false;
        Ok(())
    }

    fn cleanup(mut self) -> io::Result<()> {
        self.cleanup_inner()
    }

    fn disarm(&mut self) {
        self.armed = false;
    }
}

impl Drop for SnapshotCreation {
    fn drop(&mut self) {
        if self.armed {
            let _ = self.cleanup_inner();
        }
    }
}

fn create_snapshot(parent: &Path) -> io::Result<SnapshotCreation> {
    fs::create_dir_all(parent)?;
    let parent_metadata = fs::symlink_metadata(parent)?;
    if parent_metadata.file_type().is_symlink() || !parent_metadata.is_dir() {
        return Err(invalid("source snapshot parent is not a real directory"));
    }
    let canonical_parent = parent.canonicalize()?;
    for attempt in 0..128_u32 {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(io::Error::other)?
            .as_nanos();
        let owner = canonical_parent.join(format!(
            "packaged-source-snapshot-{}-{nonce}-{attempt}",
            std::process::id()
        ));
        #[cfg(unix)]
        let (creation, parent_handle, owner_name) = {
            use std::ffi::CString;
            use std::os::fd::AsRawFd;
            use std::os::unix::ffi::OsStrExt;
            let parent_handle = File::open(&canonical_parent)?;
            let owner_name = owner
                .file_name()
                .ok_or_else(|| invalid("snapshot owner has no name"))?
                .to_owned();
            let name = CString::new(owner_name.as_bytes())
                .map_err(|_| invalid("snapshot owner name contains NUL"))?;
            let result = unsafe { libc::mkdirat(parent_handle.as_raw_fd(), name.as_ptr(), 0o700) };
            let creation = if result == -1 {
                Err(io::Error::last_os_error())
            } else {
                Ok(())
            };
            (creation, parent_handle, owner_name)
        };
        #[cfg(not(unix))]
        let creation = fs::create_dir(&owner);
        match creation {
            Ok(()) => {
                #[cfg(unix)]
                let mut owner_guard = OwnerCreationGuard {
                    parent: parent_handle,
                    owner: None,
                    name: owner_name.clone(),
                    identity: None,
                    root: None,
                    root_created: false,
                    armed: true,
                };
                #[cfg(not(unix))]
                let mut owner_guard = OwnerPathGuard {
                    owner: owner.clone(),
                    root_created: false,
                    armed: true,
                };
                #[cfg(unix)]
                {
                    use std::os::unix::fs::MetadataExt;
                    use std::os::unix::fs::PermissionsExt;
                    let initialized = (|| -> io::Result<File> {
                        let owner_handle = openat_nofollow(
                            &owner_guard.parent,
                            &owner_name,
                            libc::O_RDONLY | libc::O_DIRECTORY,
                        )?;
                        owner_handle.set_permissions(fs::Permissions::from_mode(0o700))?;
                        let metadata = owner_handle.metadata()?;
                        if metadata.uid() != unsafe { libc::geteuid() } {
                            return Err(invalid("source snapshot owner has the wrong user"));
                        }
                        owner_guard.identity = Some(identity(&metadata));
                        Ok(owner_handle)
                    })();
                    match initialized {
                        Ok(owner_handle) => owner_guard.owner = Some(owner_handle),
                        Err(primary) => {
                            return match owner_guard.cleanup_inner() {
                                Ok(()) => Err(primary),
                                Err(cleanup) => Err(invalid(format!(
                                    "{primary}; snapshot owner cleanup also failed: {cleanup}"
                                ))),
                            };
                        }
                    }
                }
                let root = owner.join("source");
                #[cfg(unix)]
                let root_result = (|| -> io::Result<(File, File)> {
                    use std::os::fd::AsRawFd;
                    let owner_handle = owner_guard
                        .owner
                        .as_ref()
                        .ok_or_else(|| invalid("snapshot owner handle is unavailable"))?
                        .try_clone()?;
                    let name = b"source\0";
                    if unsafe {
                        libc::mkdirat(owner_handle.as_raw_fd(), name.as_ptr().cast(), 0o700)
                    } == -1
                    {
                        return Err(io::Error::last_os_error());
                    }
                    owner_guard.root_created = true;
                    let root_handle = openat_nofollow(
                        &owner_handle,
                        std::ffi::OsStr::new("source"),
                        libc::O_RDONLY | libc::O_DIRECTORY,
                    )?;
                    owner_guard.root =
                        Some((root_handle.try_clone()?, identity(&root_handle.metadata()?)));
                    Ok((owner_handle, root_handle))
                })();
                #[cfg(unix)]
                let (owner_handle, root_handle) = match root_result {
                    Ok(handles) => handles,
                    Err(primary) => {
                        return match owner_guard.cleanup_inner() {
                            Ok(()) => Err(primary),
                            Err(cleanup) => Err(invalid(format!(
                                "{primary}; snapshot root cleanup also failed: {cleanup}"
                            ))),
                        };
                    }
                };
                #[cfg(not(unix))]
                {
                    fs::create_dir(&root)?;
                    owner_guard.root_created = true;
                }
                #[cfg(unix)]
                let creation_result = (|| -> io::Result<SnapshotCreation> {
                    use std::os::unix::fs::PermissionsExt;
                    root_handle.set_permissions(fs::Permissions::from_mode(0o700))?;
                    let owner_identity = identity(&owner_handle.metadata()?);
                    let root_identity = identity(&root_handle.metadata()?);
                    let parent_handle = owner_guard.parent.try_clone()?;
                    Ok(SnapshotCreation {
                        owner,
                        root,
                        parent_handle,
                        owner_handle,
                        root_handle,
                        owner_name,
                        owner_identity,
                        root_identity,
                        created_entries: BTreeMap::from([(String::new(), root_identity)]),
                        armed: true,
                    })
                })();
                #[cfg(unix)]
                let creation = match creation_result {
                    Ok(creation) => creation,
                    Err(primary) => {
                        return match owner_guard.cleanup_inner() {
                            Ok(()) => Err(primary),
                            Err(cleanup) => Err(invalid(format!(
                                "{primary}; snapshot handoff cleanup also failed: {cleanup}"
                            ))),
                        };
                    }
                };
                #[cfg(not(unix))]
                let creation = SnapshotCreation {
                    owner,
                    root,
                    armed: true,
                };
                #[cfg(unix)]
                {
                    owner_guard.armed = false;
                }
                #[cfg(not(unix))]
                {
                    owner_guard.armed = false;
                }
                return Ok(creation);
            }
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(error),
        }
    }
    Err(invalid("could not create a unique source snapshot"))
}

fn write_trusted_manifest(root: &Path, bytes: &[u8]) -> io::Result<()> {
    let path = root.join("packaged_defaultspack_source_manifest.v1.json");
    let mut output = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&path)?;
    output.write_all(bytes)?;
    output.sync_all()?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o400))?;
    }
    Ok(())
}

#[cfg(unix)]
fn write_trusted_manifest_at(
    root: &File,
    bytes: &[u8],
    created: &mut BTreeMap<String, (u64, u64, u64, i64, i64)>,
) -> io::Result<()> {
    let name = std::ffi::OsStr::new("packaged_defaultspack_source_manifest.v1.json");
    let mut output = create_relative_file(root, Path::new(name), 0o400, created)?;
    output.write_all(bytes)?;
    output.sync_all()
}

fn verify_snapshot(root: &Path, trusted_manifest: &[u8]) -> io::Result<()> {
    let expected = parse_manifest(trusted_manifest)?;
    if read_manifest(&root.join("packaged_defaultspack_source_manifest.v1.json"))?
        != trusted_manifest
    {
        return Err(invalid("verified snapshot manifest changed"));
    }
    let mut actual = BTreeSet::new();
    for relative in ROOTS {
        collect_actual(root, &root.join(relative), &mut actual)?;
    }
    for relative in FILES {
        let path = root.join(relative);
        let metadata = fs::symlink_metadata(&path)?;
        if metadata.file_type().is_symlink() || !metadata.is_file() {
            return Err(invalid("verified snapshot file type changed"));
        }
        actual.insert((*relative).to_owned());
    }
    if actual != expected.keys().cloned().collect() {
        return Err(invalid("verified snapshot inventory changed"));
    }
    for (relative, record) in expected {
        let path = root.join(relative);
        verify_snapshot_file(&path, &record)?;
    }
    Ok(())
}

#[cfg(unix)]
fn directory_entries(directory: &File) -> io::Result<Vec<std::ffi::OsString>> {
    use std::ffi::CStr;
    use std::os::fd::AsRawFd;
    use std::os::unix::ffi::OsStrExt;

    let dot = b".\0";
    let duplicate = unsafe {
        libc::openat(
            directory.as_raw_fd(),
            dot.as_ptr().cast(),
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_CLOEXEC | libc::O_NOFOLLOW,
        )
    };
    if duplicate == -1 {
        return Err(io::Error::last_os_error());
    }
    let stream = unsafe { libc::fdopendir(duplicate) };
    if stream.is_null() {
        unsafe { libc::close(duplicate) };
        return Err(io::Error::last_os_error());
    }
    let mut names = Vec::new();
    loop {
        #[cfg(any(target_os = "macos", target_os = "ios"))]
        unsafe {
            *libc::__error() = 0;
        }
        #[cfg(any(target_os = "linux", target_os = "android"))]
        unsafe {
            *libc::__errno_location() = 0;
        }
        let entry = unsafe { libc::readdir(stream) };
        if entry.is_null() {
            let error = io::Error::last_os_error();
            unsafe { libc::closedir(stream) };
            return if error.raw_os_error() == Some(0) {
                names.sort();
                Ok(names)
            } else {
                Err(error)
            };
        }
        let name = unsafe { CStr::from_ptr((*entry).d_name.as_ptr()) }.to_bytes();
        if name != b"." && name != b".." {
            names.push(std::ffi::OsStr::from_bytes(name).to_owned());
        }
    }
}

#[cfg(unix)]
fn openat_nofollow(directory: &File, name: &std::ffi::OsStr, flags: i32) -> io::Result<File> {
    use std::ffi::CString;
    use std::os::fd::{AsRawFd, FromRawFd};
    use std::os::unix::ffi::OsStrExt;
    let name = CString::new(name.as_bytes()).map_err(|_| invalid("snapshot name contains NUL"))?;
    let fd = unsafe {
        libc::openat(
            directory.as_raw_fd(),
            name.as_ptr(),
            flags | libc::O_CLOEXEC | libc::O_NOFOLLOW,
        )
    };
    if fd == -1 {
        Err(io::Error::last_os_error())
    } else {
        Ok(unsafe { File::from_raw_fd(fd) })
    }
}

#[cfg(unix)]
fn open_relative_directory(root: &File, relative: &str) -> io::Result<File> {
    let mut current = root.try_clone()?;
    for component in relative.split('/') {
        current = openat_nofollow(
            &current,
            std::ffi::OsStr::new(component),
            libc::O_RDONLY | libc::O_DIRECTORY,
        )?;
    }
    Ok(current)
}

#[cfg(unix)]
fn ensure_relative_directory(
    root: &File,
    relative: &Path,
    created: &mut BTreeMap<String, (u64, u64, u64, i64, i64)>,
) -> io::Result<File> {
    use std::ffi::CString;
    use std::os::fd::AsRawFd;
    use std::os::unix::ffi::OsStrExt;
    let mut current = root.try_clone()?;
    let mut current_relative = PathBuf::new();
    for component in relative.components() {
        let Component::Normal(name) = component else {
            return Err(invalid("snapshot directory path is unsafe"));
        };
        current_relative.push(name);
        match openat_nofollow(&current, name, libc::O_RDONLY | libc::O_DIRECTORY) {
            Ok(child) => current = child,
            Err(error) if error.raw_os_error() == Some(libc::ENOENT) => {
                let parent = current.try_clone()?;
                let name_c = CString::new(name.as_bytes())
                    .map_err(|_| invalid("snapshot directory contains NUL"))?;
                if unsafe { libc::mkdirat(parent.as_raw_fd(), name_c.as_ptr(), 0o700) } == -1 {
                    return Err(io::Error::last_os_error());
                }
                let mut guard = CreatedDirectoryGuard {
                    parent: &parent,
                    name,
                    identity: None,
                    armed: true,
                };
                current = match openat_nofollow(&parent, name, libc::O_RDONLY | libc::O_DIRECTORY) {
                    Ok(child) => child,
                    Err(primary) => {
                        let cleanup = guard.cleanup_inner();
                        return match cleanup {
                            Ok(()) => Err(primary),
                            Err(cleanup) => Err(invalid(format!(
                                "{primary}; provisional directory cleanup also failed: {cleanup}"
                            ))),
                        };
                    }
                };
                let directory_identity =
                    match current.metadata().map(|metadata| identity(&metadata)) {
                        Ok(identity) => identity,
                        Err(primary) => {
                            let cleanup = guard.cleanup_inner();
                            return match cleanup {
                                Ok(()) => Err(primary),
                                Err(cleanup) => Err(invalid(format!(
                                "{primary}; provisional directory cleanup also failed: {cleanup}"
                            ))),
                            };
                        }
                    };
                guard.identity = Some(directory_identity);
                created.insert(
                    current_relative.to_string_lossy().replace('\\', "/"),
                    directory_identity,
                );
                guard.armed = false;
            }
            Err(error) => return Err(error),
        }
    }
    Ok(current)
}

#[cfg(unix)]
fn create_relative_file(
    root: &File,
    relative: &Path,
    mode: u32,
    created: &mut BTreeMap<String, (u64, u64, u64, i64, i64)>,
) -> io::Result<File> {
    use std::ffi::CString;
    use std::os::fd::{AsRawFd, FromRawFd};
    use std::os::unix::ffi::OsStrExt;
    let parent = relative.parent().unwrap_or_else(|| Path::new(""));
    let directory = ensure_relative_directory(root, parent, created)?;
    let name = relative
        .file_name()
        .ok_or_else(|| invalid("snapshot file has no name"))?;
    let encoded =
        CString::new(name.as_bytes()).map_err(|_| invalid("snapshot file contains NUL"))?;
    let fd = unsafe {
        libc::openat(
            directory.as_raw_fd(),
            encoded.as_ptr(),
            libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            mode,
        )
    };
    if fd == -1 {
        Err(io::Error::last_os_error())
    } else {
        let file = unsafe { File::from_raw_fd(fd) };
        let file_identity = match file.metadata().map(|metadata| identity(&metadata)) {
            Ok(identity) => identity,
            Err(primary) => {
                let cleanup = unlinkat_name(&directory, name, 0);
                return match cleanup {
                    Ok(()) => Err(primary),
                    Err(cleanup) => Err(invalid(format!(
                        "{primary}; newly-created file cleanup also failed: {cleanup}"
                    ))),
                };
            }
        };
        created.insert(relative.to_string_lossy().replace('\\', "/"), file_identity);
        Ok(file)
    }
}

#[cfg(unix)]
fn create_root_file(root: &File, name: &std::ffi::OsStr, mode: u32) -> io::Result<File> {
    use std::ffi::CString;
    use std::os::fd::{AsRawFd, FromRawFd};
    use std::os::unix::ffi::OsStrExt;
    let encoded =
        CString::new(name.as_bytes()).map_err(|_| invalid("snapshot file contains NUL"))?;
    let fd = unsafe {
        libc::openat(
            root.as_raw_fd(),
            encoded.as_ptr(),
            libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            mode,
        )
    };
    if fd == -1 {
        Err(io::Error::last_os_error())
    } else {
        Ok(unsafe { File::from_raw_fd(fd) })
    }
}

#[cfg(unix)]
fn walk_snapshot_at(
    directory: &File,
    relative: &str,
    files: &mut BTreeMap<String, File>,
    directories: &mut BTreeMap<String, (u64, u64, u64, i64, i64)>,
) -> io::Result<()> {
    directories.insert(relative.to_owned(), identity(&directory.metadata()?));
    for name in directory_entries(directory)? {
        let component = name
            .to_str()
            .ok_or_else(|| invalid("snapshot path is not UTF-8"))?;
        let child_relative = if relative.is_empty() {
            component.to_owned()
        } else {
            format!("{relative}/{component}")
        };
        match openat_nofollow(directory, &name, libc::O_RDONLY | libc::O_DIRECTORY) {
            Ok(child) => walk_snapshot_at(&child, &child_relative, files, directories)?,
            Err(error) if matches!(error.raw_os_error(), Some(libc::ENOTDIR)) => {
                let child = openat_nofollow(directory, &name, libc::O_RDONLY)?;
                if !child.metadata()?.is_file() {
                    return Err(invalid("snapshot contains a special entry"));
                }
                directories.insert(child_relative.clone(), identity(&child.metadata()?));
                files.insert(child_relative, child);
            }
            Err(error) => return Err(error),
        }
    }
    Ok(())
}

#[cfg(unix)]
fn verify_open_file(mut file: &File, expected: &ExpectedFile) -> io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    let metadata = file.metadata()?;
    let expected_mode = if expected.executable { 0o500 } else { 0o400 };
    if !metadata.is_file()
        || metadata.len() != expected.size
        || metadata.permissions().mode() & 0o777 != expected_mode
    {
        return Err(invalid("anchored snapshot metadata changed"));
    }
    let mut digest = Sha256::new();
    let mut size = 0_u64;
    let mut buffer = [0_u8; 1024 * 1024];
    loop {
        let count = file.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        size += count as u64;
        digest.update(&buffer[..count]);
    }
    if size != expected.size || format!("{:x}", digest.finalize()) != expected.sha256 {
        return Err(invalid("anchored snapshot digest changed"));
    }
    Ok(())
}

#[cfg(unix)]
fn verify_snapshot_at(
    root: &File,
    trusted_manifest: &[u8],
    provenance: Option<&[u8]>,
    expected_directories: &BTreeMap<String, (u64, u64, u64, i64, i64)>,
) -> io::Result<()> {
    let expected = parse_manifest(trusted_manifest)?;
    let mut files = BTreeMap::new();
    let mut directories = BTreeMap::new();
    walk_snapshot_at(root, "", &mut files, &mut directories)?;
    if &directories != expected_directories {
        return Err(invalid("anchored snapshot entry identity changed"));
    }
    use std::os::unix::fs::PermissionsExt;
    for relative in directories.keys().filter(|path| !files.contains_key(*path)) {
        let directory = if relative.is_empty() {
            root.try_clone()?
        } else {
            open_relative_directory(root, relative)?
        };
        if directory.metadata()?.permissions().mode() & 0o777 != 0o500 {
            return Err(invalid("anchored snapshot directory mode changed"));
        }
    }
    let manifest_name = "packaged_defaultspack_source_manifest.v1.json";
    let mut manifest = files
        .remove(manifest_name)
        .ok_or_else(|| invalid("anchored snapshot manifest is missing"))?;
    use std::os::unix::fs::MetadataExt;
    let manifest_metadata = manifest.metadata()?;
    if !manifest_metadata.is_file()
        || manifest_metadata.nlink() != 1
        || manifest_metadata.permissions().mode() & 0o777 != 0o400
        || manifest_metadata.len() > MAX_MANIFEST_BYTES
    {
        return Err(invalid("anchored snapshot manifest metadata changed"));
    }
    let mut bytes = Vec::new();
    manifest.read_to_end(&mut bytes)?;
    if bytes != trusted_manifest {
        return Err(invalid("anchored snapshot manifest changed"));
    }
    if let Some(expected_provenance) = provenance {
        let mut file = files
            .remove(PROVENANCE_FILENAME)
            .ok_or_else(|| invalid("anchored snapshot provenance is missing"))?;
        let metadata = file.metadata()?;
        if !metadata.is_file()
            || metadata.nlink() != 1
            || metadata.permissions().mode() & 0o777 != 0o400
            || metadata.len() > MAX_PROVENANCE_BYTES as u64
        {
            return Err(invalid("anchored snapshot provenance metadata changed"));
        }
        let mut actual = Vec::new();
        file.read_to_end(&mut actual)?;
        if actual != expected_provenance {
            return Err(invalid("anchored snapshot provenance changed"));
        }
    }
    if files.keys().cloned().collect::<BTreeSet<_>>() != expected.keys().cloned().collect() {
        return Err(invalid("anchored snapshot inventory changed"));
    }
    for (relative, record) in expected {
        verify_open_file(&files[&relative], &record)?;
    }
    Ok(())
}

#[cfg(unix)]
fn remove_snapshot_at(
    root: &File,
    expected_directories: &BTreeMap<String, (u64, u64, u64, i64, i64)>,
) -> io::Result<()> {
    use std::ffi::CString;
    use std::os::fd::AsRawFd;
    use std::os::unix::ffi::OsStrExt;

    fn recurse(
        directory: &File,
        relative: &str,
        expected: &BTreeMap<String, (u64, u64, u64, i64, i64)>,
    ) -> io::Result<()> {
        use std::os::unix::fs::PermissionsExt;
        let directory_metadata = directory.metadata()?;
        if !expected
            .get(relative)
            .is_some_and(|expected| same_object(&directory_metadata, *expected))
        {
            return Err(invalid("refusing cleanup of replaced snapshot directory"));
        }
        directory.set_permissions(fs::Permissions::from_mode(0o700))?;
        for name in directory_entries(directory)? {
            let component = name.to_string_lossy();
            let child_relative = if relative.is_empty() {
                component.into_owned()
            } else {
                format!("{relative}/{component}")
            };
            let (child, flags) =
                match openat_nofollow(directory, &name, libc::O_RDONLY | libc::O_DIRECTORY) {
                    Ok(child) => (child, libc::AT_REMOVEDIR),
                    Err(error) if error.raw_os_error() == Some(libc::ENOTDIR) => {
                        (openat_nofollow(directory, &name, libc::O_RDONLY)?, 0)
                    }
                    Err(error) => return Err(error),
                };
            let child_metadata = child.metadata()?;
            if !expected
                .get(&child_relative)
                .is_some_and(|expected| same_object(&child_metadata, *expected))
            {
                return Err(invalid(
                    "refusing cleanup of replaced or extra snapshot entry",
                ));
            }
            if flags == libc::AT_REMOVEDIR {
                recurse(&child, &child_relative, expected)?;
            }
            let encoded =
                CString::new(name.as_bytes()).map_err(|_| invalid("cleanup name contains NUL"))?;
            if unsafe { libc::unlinkat(directory.as_raw_fd(), encoded.as_ptr(), flags) } == -1 {
                return Err(io::Error::last_os_error());
            }
        }
        Ok(())
    }
    recurse(root, "", expected_directories)
}

#[cfg(unix)]
fn unlinkat_name(directory: &File, name: &std::ffi::OsStr, flags: i32) -> io::Result<()> {
    use std::ffi::CString;
    use std::os::fd::AsRawFd;
    use std::os::unix::ffi::OsStrExt;
    let encoded =
        CString::new(name.as_bytes()).map_err(|_| invalid("cleanup name contains NUL"))?;
    if unsafe { libc::unlinkat(directory.as_raw_fd(), encoded.as_ptr(), flags) } == -1 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}

#[cfg(unix)]
fn verify_named_identity(
    directory: &File,
    name: &std::ffi::OsStr,
    expected: (u64, u64, u64, i64, i64),
) -> io::Result<()> {
    let child = openat_nofollow(directory, name, libc::O_RDONLY | libc::O_DIRECTORY)?;
    if !same_object(&child.metadata()?, expected) {
        return Err(invalid("refusing cleanup of replaced snapshot name"));
    }
    Ok(())
}

fn verify_snapshot_file(path: &Path, expected: &ExpectedFile) -> io::Result<()> {
    let before = fs::symlink_metadata(path)?;
    if before.file_type().is_symlink()
        || !before.is_file()
        || before.len() != expected.size
        || executable(&before) != expected.executable
    {
        return Err(invalid("verified snapshot metadata changed"));
    }
    let mut input = File::open(path)?;
    if identity(&input.metadata()?) != identity(&before) {
        return Err(invalid("verified snapshot changed while opened"));
    }
    let mut digest = Sha256::new();
    let mut size = 0_u64;
    let mut buffer = [0_u8; 1024 * 1024];
    loop {
        let count = input.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        size = size
            .checked_add(count as u64)
            .ok_or_else(|| invalid("verified snapshot size overflow"))?;
        if size > expected.size {
            return Err(invalid("verified snapshot grew while read"));
        }
        digest.update(&buffer[..count]);
    }
    let after = fs::symlink_metadata(path)?;
    if identity(&after) != identity(&before)
        || size != expected.size
        || format!("{:x}", digest.finalize()) != expected.sha256
    {
        return Err(invalid("verified snapshot digest or identity changed"));
    }
    Ok(())
}

fn seal_directories(path: &Path) -> io::Result<()> {
    for entry in fs::read_dir(path)? {
        let entry = entry?;
        let metadata = fs::symlink_metadata(entry.path())?;
        if metadata.is_dir() && !metadata.file_type().is_symlink() {
            seal_directories(&entry.path())?;
        }
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o500))?;
    }
    Ok(())
}

#[cfg(unix)]
fn seal_directories_at(directory: &File) -> io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    for name in directory_entries(directory)? {
        match openat_nofollow(directory, &name, libc::O_RDONLY | libc::O_DIRECTORY) {
            Ok(child) => seal_directories_at(&child)?,
            Err(error) if error.raw_os_error() == Some(libc::ENOTDIR) => {}
            Err(error) => return Err(error),
        }
    }
    directory.set_permissions(fs::Permissions::from_mode(0o500))
}

fn make_tree_owner_writable(path: &Path) -> io::Result<()> {
    let metadata = fs::symlink_metadata(path)?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        return Err(invalid("snapshot cleanup encountered a replaced directory"));
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    }
    for entry in fs::read_dir(path)? {
        let entry = entry?;
        let metadata = fs::symlink_metadata(entry.path())?;
        if metadata.file_type().is_symlink() {
            return Err(invalid("snapshot cleanup encountered a symlink"));
        }
        if metadata.is_dir() {
            make_tree_owner_writable(&entry.path())?;
        }
    }
    Ok(())
}

fn copy_verified(source: &Path, target: &Path, expected: &ExpectedFile) -> io::Result<()> {
    let before = fs::symlink_metadata(source)?;
    if before.file_type().is_symlink() || !before.is_file() {
        return Err(invalid(format!(
            "source is not a regular file: {}",
            source.display()
        )));
    }
    reject_hardlink(&before, source)?;
    if before.len() != expected.size || executable(&before) != expected.executable {
        return Err(invalid(format!(
            "source metadata differs from manifest: {}",
            source.display()
        )));
    }
    let mut input = File::open(source)?;
    let opened = input.metadata()?;
    if identity(&opened) != identity(&before) {
        return Err(invalid(format!(
            "source changed while opened: {}",
            source.display()
        )));
    }
    if let Some(parent) = target.parent() {
        fs::create_dir_all(parent)?;
    }
    let mut output = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(target)?;
    let mut digest = Sha256::new();
    let mut count = 0_u64;
    let mut buffer = [0_u8; 1024 * 1024];
    loop {
        let read = input.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        count = count
            .checked_add(read as u64)
            .ok_or_else(|| invalid("source size overflow"))?;
        if count > expected.size {
            return Err(invalid(format!(
                "source grew while copied: {}",
                source.display()
            )));
        }
        digest.update(&buffer[..read]);
        output.write_all(&buffer[..read])?;
    }
    output.sync_all()?;
    let after = fs::symlink_metadata(source)?;
    if after.file_type().is_symlink()
        || !after.is_file()
        || identity(&after) != identity(&before)
        || count != expected.size
        || format!("{:x}", digest.finalize()) != expected.sha256
    {
        return Err(invalid(format!(
            "source content changed or mismatched: {}",
            source.display()
        )));
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(
            target,
            fs::Permissions::from_mode(if expected.executable { 0o500 } else { 0o400 }),
        )?;
    }
    Ok(())
}

#[cfg(unix)]
fn copy_verified_at(
    source_root: &File,
    target_root: &File,
    relative: &Path,
    expected: &ExpectedFile,
    created: &mut BTreeMap<String, (u64, u64, u64, i64, i64)>,
) -> io::Result<()> {
    use std::os::unix::fs::MetadataExt;
    let source_parent = open_relative_directory(
        source_root,
        relative
            .parent()
            .unwrap_or_else(|| Path::new(""))
            .to_str()
            .ok_or_else(|| invalid("source path is not UTF-8"))?,
    )?;
    let name = relative
        .file_name()
        .ok_or_else(|| invalid("source has no name"))?;
    let mut input = openat_nofollow(&source_parent, name, libc::O_RDONLY)?;
    let metadata = input.metadata()?;
    if !metadata.is_file()
        || metadata.nlink() != 1
        || metadata.len() != expected.size
        || executable(&metadata) != expected.executable
    {
        return Err(invalid("anchored source metadata differs from manifest"));
    }
    let mut output = create_relative_file(
        target_root,
        relative,
        if expected.executable { 0o500 } else { 0o400 },
        created,
    )?;
    let mut digest = Sha256::new();
    let mut count = 0_u64;
    let mut buffer = [0_u8; 1024 * 1024];
    loop {
        let read = input.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        count += read as u64;
        if count > expected.size {
            return Err(invalid("anchored source grew while copied"));
        }
        digest.update(&buffer[..read]);
        output.write_all(&buffer[..read])?;
    }
    output.sync_all()?;
    if count != expected.size || format!("{:x}", digest.finalize()) != expected.sha256 {
        return Err(invalid("anchored source digest differs from manifest"));
    }
    Ok(())
}

fn read_manifest(path: &Path) -> io::Result<Vec<u8>> {
    let metadata = fs::symlink_metadata(path)?;
    if metadata.file_type().is_symlink()
        || !metadata.is_file()
        || metadata.len() > MAX_MANIFEST_BYTES
    {
        return Err(invalid(
            "source manifest must be a bounded regular non-symlink file",
        ));
    }
    reject_hardlink(&metadata, path)?;
    let input = File::open(path)?;
    if identity(&input.metadata()?) != identity(&metadata) {
        return Err(invalid("source manifest changed while opened"));
    }
    let mut bytes = Vec::with_capacity(metadata.len() as usize);
    input.take(MAX_MANIFEST_BYTES + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 != metadata.len() {
        return Err(invalid("source manifest size changed while read"));
    }
    let after = fs::symlink_metadata(path)?;
    if identity(&metadata) != identity(&after) {
        return Err(invalid("source manifest changed while read"));
    }
    Ok(bytes)
}

/// Verify the complete source closure and return an immutable-by-construction copy.
#[cfg(test)]
pub fn verify_and_snapshot(
    runtime_root: &Path,
    snapshot_parent: &Path,
) -> io::Result<VerifiedSourceSnapshot> {
    let manifest =
        read_manifest(&runtime_root.join("packaged_defaultspack_source_manifest.v1.json"))?;
    verify_and_snapshot_against_manifest_with_hook(runtime_root, snapshot_parent, &manifest, || {})
}

/// Verify against manifest bytes obtained from a separate trusted authority.
pub fn verify_and_snapshot_against_manifest(
    runtime_root: &Path,
    snapshot_parent: &Path,
    trusted_manifest: &[u8],
) -> io::Result<VerifiedSourceSnapshot> {
    verify_and_snapshot_against_manifest_with_hook(
        runtime_root,
        snapshot_parent,
        trusted_manifest,
        || {},
    )
}

#[cfg(not(unix))]
fn unsupported_nonunix_snapshot() -> io::Error {
    io::Error::new(
        io::ErrorKind::Unsupported,
        "packaged source snapshots are disabled on non-Unix platforms until handle-anchored cleanup is available",
    )
}

#[cfg(not(unix))]
fn verify_and_snapshot_against_manifest_with_hook(
    _runtime_root: &Path,
    _snapshot_parent: &Path,
    _trusted_manifest: &[u8],
    _before_copy: impl FnOnce(),
) -> io::Result<VerifiedSourceSnapshot> {
    Err(unsupported_nonunix_snapshot())
}

#[cfg(unix)]
fn verify_and_snapshot_against_manifest_with_hook(
    runtime_root: &Path,
    snapshot_parent: &Path,
    trusted_manifest: &[u8],
    before_copy: impl FnOnce(),
) -> io::Result<VerifiedSourceSnapshot> {
    let root_metadata = fs::symlink_metadata(runtime_root)?;
    if root_metadata.file_type().is_symlink() || !root_metadata.is_dir() {
        return Err(invalid("packaged source root must be a real directory"));
    }
    if trusted_manifest.len() as u64 > MAX_MANIFEST_BYTES {
        return Err(invalid("trusted source manifest exceeds its size bound"));
    }
    let actual_manifest =
        read_manifest(&runtime_root.join("packaged_defaultspack_source_manifest.v1.json"))?;
    if actual_manifest != trusted_manifest {
        return Err(invalid(
            "working source manifest differs from the trusted authority",
        ));
    }
    let manifest = parse_manifest(trusted_manifest)?;
    #[cfg(unix)]
    let runtime_handle = File::open(runtime_root)?;
    let mut actual = BTreeSet::new();
    for relative in ROOTS {
        collect_actual(runtime_root, &runtime_root.join(relative), &mut actual)?;
    }
    for relative in FILES {
        let path = runtime_root.join(relative);
        let metadata = fs::symlink_metadata(&path)?;
        if metadata.file_type().is_symlink() || !metadata.is_file() {
            return Err(invalid(format!(
                "source closure file is not regular: {relative}"
            )));
        }
        reject_hardlink(&metadata, &path)?;
        actual.insert((*relative).to_owned());
    }
    if actual != manifest.keys().cloned().collect() {
        return Err(invalid(
            "actual source paths differ from the strict manifest",
        ));
    }
    before_copy();
    let mut creation = create_snapshot(snapshot_parent)?;
    let snapshot_owner = creation.owner.clone();
    let snapshot_root = creation.root.clone();
    let result = (|| {
        #[cfg(unix)]
        let root_handle = creation.root_handle.try_clone()?;
        for (relative, expected) in &manifest {
            #[cfg(unix)]
            copy_verified_at(
                &runtime_handle,
                &root_handle,
                Path::new(relative),
                expected,
                &mut creation.created_entries,
            )?;
            #[cfg(not(unix))]
            copy_verified(
                &runtime_root.join(relative),
                &snapshot_root.join(relative),
                expected,
            )?;
        }
        #[cfg(unix)]
        write_trusted_manifest_at(
            &root_handle,
            trusted_manifest,
            &mut creation.created_entries,
        )?;
        #[cfg(not(unix))]
        write_trusted_manifest(&snapshot_root, trusted_manifest)?;
        #[cfg(unix)]
        seal_directories_at(&root_handle)?;
        #[cfg(not(unix))]
        {
            verify_snapshot(&snapshot_root, trusted_manifest)?;
            seal_directories(&snapshot_root)?;
        }
        if identity(&fs::symlink_metadata(runtime_root)?) != identity(&root_metadata) {
            return Err(invalid("packaged source root changed during verification"));
        }
        #[cfg(not(unix))]
        let root_handle = File::open(&snapshot_root)?;
        #[cfg(unix)]
        let parent_handle = creation.parent_handle.try_clone()?;
        #[cfg(unix)]
        let owner_handle = creation.owner_handle.try_clone()?;
        #[cfg(unix)]
        let owner_name = creation.owner_name.clone();
        #[cfg(unix)]
        let directory_identities = {
            // Directory sealing updates directory timestamps.  Capture the
            // anchored identities only after sealing so the stored snapshot
            // describes the immutable state that verification will reopen.
            let mut files = BTreeMap::new();
            let mut directories = BTreeMap::new();
            walk_snapshot_at(&root_handle, "", &mut files, &mut directories)?;
            verify_snapshot_at(&root_handle, trusted_manifest, None, &directories)?;
            directories
        };
        let snapshot = VerifiedSourceSnapshot {
            owner_identity: identity(&fs::symlink_metadata(&snapshot_owner)?),
            root_identity: identity(&root_handle.metadata()?),
            owner: snapshot_owner.clone(),
            root: snapshot_root.clone(),
            root_handle,
            #[cfg(unix)]
            parent_handle,
            #[cfg(unix)]
            owner_handle,
            #[cfg(unix)]
            owner_name,
            #[cfg(unix)]
            directory_identities,
            trusted_manifest: trusted_manifest.to_vec(),
            provenance: None,
            cleanup_attempted: false,
        };
        creation.disarm();
        Ok(snapshot)
    })();
    match result {
        Ok(snapshot) => Ok(snapshot),
        Err(primary) => match creation.cleanup() {
            Ok(()) => Err(primary),
            Err(cleanup) => Err(invalid(format!(
                "{primary}; snapshot construction cleanup also failed: {cleanup}"
            ))),
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn copy_fixture_tree(source: &Path, target: &Path) {
        fs::create_dir_all(target).unwrap();
        for entry in fs::read_dir(source).unwrap() {
            let entry = entry.unwrap();
            let destination = target.join(entry.file_name());
            if entry.file_type().unwrap().is_dir() {
                copy_fixture_tree(&entry.path(), &destination);
            } else {
                fs::copy(entry.path(), destination).unwrap();
            }
        }
    }

    struct Tree(PathBuf);
    impl Tree {
        fn new(label: &str) -> Self {
            let root = std::env::temp_dir().join(format!(
                "tobkiri-source-{label}-{}-{}",
                std::process::id(),
                SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .unwrap()
                    .as_nanos()
            ));
            fs::create_dir_all(&root).unwrap();
            Self(root)
        }
    }
    impl Drop for Tree {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn fixture(tree: &Tree) -> PathBuf {
        let root = tree.0.join("runtime");
        if root.exists() {
            fs::remove_dir_all(&root).unwrap();
        }
        for relative in ROOTS {
            fs::create_dir_all(root.join(relative)).unwrap();
        }
        let mut records = Vec::new();
        let mut paths = ROOTS
            .iter()
            .map(|root| format!("{root}/fixture.py"))
            .collect::<Vec<_>>();
        paths.extend(FILES.iter().map(|path| (*path).to_owned()));
        paths.sort();
        for relative in paths {
            let path = root.join(&relative);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            let payload = format!("safe:{relative}\n");
            fs::write(&path, payload.as_bytes()).unwrap();
            records.push(serde_json::json!({
                "path": relative, "type": "regular-file", "size": payload.len(),
                "sha256": format!("{:x}", Sha256::digest(payload.as_bytes())), "executable": false
            }));
        }
        fs::write(
            root.join("packaged_defaultspack_source_manifest.v1.json"),
            serde_json::to_vec(&serde_json::json!({
                "schema": SCHEMA, "roots": ROOTS, "files": records
            }))
            .unwrap(),
        )
        .unwrap();
        root
    }

    fn mutate_first_manifest_entry(root: &Path, mutate: impl FnOnce(&mut Value)) {
        let path = root.join("packaged_defaultspack_source_manifest.v1.json");
        let mut manifest: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        mutate(&mut manifest["files"][0]);
        fs::write(path, serde_json::to_vec(&manifest).unwrap()).unwrap();
    }

    #[test]
    fn verified_snapshot_contains_only_manifest_bytes() {
        let tree = Tree::new("valid");
        let root = fixture(&tree);
        let snapshot = verify_and_snapshot(&root, &tree.0.join("snapshots")).unwrap();
        assert_eq!(
            fs::read(snapshot.root().join("scripts/fixture.py")).unwrap(),
            b"safe:scripts/fixture.py\n"
        );
        assert_eq!(
            fs::read(
                snapshot
                    .root()
                    .join("packaged_defaultspack_source_manifest.v1.json")
            )
            .unwrap(),
            fs::read(root.join("packaged_defaultspack_source_manifest.v1.json")).unwrap()
        );
    }

    #[test]
    fn repository_manifest_produces_a_verified_snapshot_without_python() {
        let tree = Tree::new("repository");
        let runtime_root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(Path::parent)
            .unwrap()
            .join("tobkiri_runtime");
        let snapshot = verify_and_snapshot(&runtime_root, &tree.0).unwrap();
        assert!(snapshot
            .root()
            .join("scripts/generate_packaged_defaultspack_v4_bundle.py")
            .is_file());
        assert!(snapshot
            .root()
            .join("ecosystem/defaultspack/domain/runtime_surface_v4.py")
            .is_file());
        assert!(!snapshot.root().join("scripts/__pycache__").exists());
    }

    #[test]
    fn actual_isolated_generator_imports_from_verified_snapshot() {
        let tree = Tree::new("generator-integration");
        let runtime_root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(Path::parent)
            .unwrap()
            .join("tobkiri_runtime");
        let mut snapshot = verify_and_snapshot(&runtime_root, &tree.0).unwrap();
        let manifest =
            fs::read(runtime_root.join("packaged_defaultspack_source_manifest.v1.json")).unwrap();
        let provenance = format!(
            "{{\"schema\":\"io.tobkiri.packaging-source-provenance.v1\",\"source_commit\":\"0123456789abcdef0123456789abcdef01234567\",\"source_tree\":\"89abcdef0123456789abcdef0123456789abcdef\",\"source_clean\":true,\"source_manifest_sha256\":\"{:x}\"}}",
            Sha256::digest(&manifest)
        );
        snapshot.bind_provenance(provenance.as_bytes()).unwrap();
        let python = super::super::packaging_toolchain::verified_tool("python").unwrap();
        let fixture_root = tree.0.join("generator-fixture");
        let source_artifact = fixture_root.join("verified-release/Tobkiri.AppImage");
        fs::create_dir_all(source_artifact.parent().unwrap()).unwrap();
        let artifact = [
            0x7f, b'E', b'L', b'F', 2, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0x3e, 0,
        ];
        fs::write(&source_artifact, artifact).unwrap();
        let bundle_root = fixture_root.join("defaultspack/v4");
        copy_fixture_tree(
            &runtime_root.join("ecosystem/defaultspack/v4"),
            &bundle_root,
        );
        let artifact_root = fixture_root.join("defaultspack/platform-artifacts");
        let mut command = super::super::isolated_python_module_command(
            &python,
            &snapshot,
            "scripts.generate_packaged_defaultspack_v4_bundle",
        )
        .unwrap();
        command
            .arg("--source-artifact")
            .arg(&source_artifact)
            .arg("--bundle-root")
            .arg(&bundle_root)
            .arg("--artifact-root")
            .arg(&artifact_root)
            .args(["--relative-path", "Tobkiri.AppImage"])
            .args(["--entrypoint", "Tobkiri.AppImage"])
            .args(["--platform", "linux"])
            .args(["--architecture", "x86_64"])
            .args(["--bundle-identity", "io.tobkiri.shell.tauri"]);
        command
            .arg("--source-provenance-file")
            .arg(PROVENANCE_FILENAME);
        let output = command.output().unwrap();
        assert!(
            output.status.success(),
            "actual generator failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        assert!(bundle_root.join("defaults.profile.v4.json").is_file());
        assert!(bundle_root
            .join("shell.tauri.default.shell.v1.json")
            .is_file());
        assert!(bundle_root.join("bundle.lock.json").is_file());
        assert!(artifact_root.join("Tobkiri.AppImage").is_file());
        snapshot.verify_unchanged().unwrap();
        snapshot.cleanup().unwrap();
    }

    #[test]
    fn tampered_source_fails_closed() {
        let tree = Tree::new("tamper");
        let root = fixture(&tree);
        fs::write(root.join("scripts/fixture.py"), b"tampered\n").unwrap();
        assert!(verify_and_snapshot(&root, &tree.0.join("snapshots")).is_err());
    }

    #[test]
    fn manifest_type_size_digest_and_executable_are_strict() {
        let tree = Tree::new("manifest-metadata");
        for field in ["type", "size", "sha256", "executable"] {
            let root = fixture(&tree);
            mutate_first_manifest_entry(&root, |entry| match field {
                "type" => entry["type"] = Value::String("device".to_owned()),
                "size" => entry["size"] = Value::from(1_000_000_u64),
                "sha256" => entry["sha256"] = Value::String("0".repeat(64)),
                "executable" => entry["executable"] = Value::Bool(true),
                _ => unreachable!(),
            });
            assert!(
                verify_and_snapshot(&root, &tree.0.join(format!("snapshot-{field}"))).is_err(),
                "manifest {field} drift must fail closed"
            );
        }
    }

    #[test]
    fn extra_pyc_fails_closed() {
        let tree = Tree::new("pyc");
        let root = fixture(&tree);
        fs::create_dir_all(root.join("scripts/__pycache__")).unwrap();
        fs::write(root.join("scripts/__pycache__/fixture.pyc"), b"code").unwrap();
        assert!(verify_and_snapshot(&root, &tree.0.join("snapshots")).is_err());
    }

    #[cfg(unix)]
    #[test]
    fn symlink_fails_closed() {
        let tree = Tree::new("symlink");
        let root = fixture(&tree);
        let source = root.join("scripts/fixture.py");
        fs::remove_file(&source).unwrap();
        std::os::unix::fs::symlink("../tobkiri_protocol/fixture.py", &source).unwrap();
        assert!(verify_and_snapshot(&root, &tree.0.join("snapshots")).is_err());
    }

    #[cfg(unix)]
    #[test]
    fn hardlink_fails_closed() {
        let tree = Tree::new("hardlink");
        let root = fixture(&tree);
        let source = root.join("scripts/fixture.py");
        let outside = tree.0.join("outside");
        fs::rename(&source, &outside).unwrap();
        fs::hard_link(&outside, &source).unwrap();
        assert!(verify_and_snapshot(&root, &tree.0.join("snapshots")).is_err());
    }

    #[test]
    fn unknown_manifest_marker_fails_before_source_can_execute() {
        let tree = Tree::new("marker");
        let root = fixture(&tree);
        fs::write(
            root.join("scripts/__init__.py"),
            b"raise SystemExit('EXECUTED')\n",
        )
        .unwrap();
        let manifest_path = root.join("packaged_defaultspack_source_manifest.v1.json");
        let mut manifest: Value =
            serde_json::from_slice(&fs::read(&manifest_path).unwrap()).unwrap();
        manifest["execute_verifier"] = Value::Bool(true);
        fs::write(&manifest_path, serde_json::to_vec(&manifest).unwrap()).unwrap();
        assert!(verify_and_snapshot(&root, &tree.0.join("snapshots")).is_err());
        assert!(!tree.0.join("EXECUTED").exists());
    }

    #[test]
    fn working_manifest_must_match_trusted_git_authority() {
        let tree = Tree::new("manifest-authority");
        let root = fixture(&tree);
        let manifest_path = root.join("packaged_defaultspack_source_manifest.v1.json");
        let trusted = fs::read(&manifest_path).unwrap();
        let mut tampered: Value = serde_json::from_slice(&trusted).unwrap();
        tampered["files"][0]["sha256"] = Value::String("0".repeat(64));
        fs::write(manifest_path, serde_json::to_vec(&tampered).unwrap()).unwrap();
        let error =
            verify_and_snapshot_against_manifest(&root, &tree.0.join("snapshots"), &trusted)
                .unwrap_err();
        assert!(error.to_string().contains("trusted authority"));
    }

    #[test]
    fn file_swap_fails_closed() {
        let tree = Tree::new("file-swap");
        let root = fixture(&tree);
        let swapped = root.join("scripts/fixture.py");
        let manifest =
            read_manifest(&root.join("packaged_defaultspack_source_manifest.v1.json")).unwrap();
        let error = verify_and_snapshot_against_manifest_with_hook(
            &root,
            &tree.0.join("snapshots"),
            &manifest,
            || fs::write(&swapped, b"swapped\n").unwrap(),
        )
        .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
        assert!(fs::read_dir(tree.0.join("snapshots"))
            .unwrap()
            .all(|entry| !entry
                .unwrap()
                .file_name()
                .to_string_lossy()
                .starts_with("packaged-source-snapshot-")));
    }

    #[test]
    fn root_swap_fails_closed() {
        let tree = Tree::new("root-swap");
        let root = fixture(&tree);
        let moved = tree.0.join("moved-runtime");
        let replacement = tree.0.join("runtime");
        let manifest =
            read_manifest(&root.join("packaged_defaultspack_source_manifest.v1.json")).unwrap();
        let error = verify_and_snapshot_against_manifest_with_hook(
            &root,
            &tree.0.join("snapshots"),
            &manifest,
            || {
                fs::rename(&root, &moved).unwrap();
                fs::create_dir(&replacement).unwrap();
            },
        )
        .unwrap_err();
        assert!(error.to_string().contains("source") || error.kind() == io::ErrorKind::NotFound);
    }

    #[cfg(unix)]
    #[test]
    fn umask_zero_still_produces_private_sealed_snapshot() {
        use std::os::unix::fs::PermissionsExt;
        use std::sync::Mutex;

        static UMASK_LOCK: Mutex<()> = Mutex::new(());
        let _lock = UMASK_LOCK.lock().unwrap();
        let previous = unsafe { libc::umask(0) };
        struct Restore(libc::mode_t);
        impl Drop for Restore {
            fn drop(&mut self) {
                unsafe { libc::umask(self.0) };
            }
        }
        let _restore = Restore(previous);
        let tree = Tree::new("umask-zero");
        let root = fixture(&tree);
        let snapshot = verify_and_snapshot(&root, &tree.0).unwrap();
        assert_eq!(
            fs::metadata(&snapshot.owner).unwrap().permissions().mode() & 0o777,
            0o700
        );
        assert_eq!(
            fs::metadata(snapshot.root()).unwrap().permissions().mode() & 0o777,
            0o500
        );
        assert_eq!(
            fs::metadata(snapshot.root().join("scripts"))
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o500
        );
    }

    #[cfg(unix)]
    #[test]
    fn postverify_replace_and_extra_are_detected() {
        use std::os::unix::fs::PermissionsExt;

        for mutation in ["replace", "extra"] {
            let tree = Tree::new(mutation);
            let root = fixture(&tree);
            let snapshot = verify_and_snapshot(&root, &tree.0).unwrap();
            let scripts = snapshot.root().join("scripts");
            fs::set_permissions(&scripts, fs::Permissions::from_mode(0o700)).unwrap();
            if mutation == "replace" {
                fs::remove_file(scripts.join("fixture.py")).unwrap();
                fs::write(scripts.join("fixture.py"), b"replacement\n").unwrap();
            } else {
                fs::write(scripts.join("extra.pyc"), b"extra").unwrap();
            }
            assert!(snapshot.verify_unchanged().is_err());
        }
    }

    #[test]
    fn failed_child_is_followed_by_explicit_snapshot_cleanup() {
        let tree = Tree::new("spawn-cleanup");
        let root = fixture(&tree);
        let snapshot = verify_and_snapshot(&root, &tree.0).unwrap();
        let owner = snapshot.owner.clone();
        let python = super::super::packaging_toolchain::verified_tool("python").unwrap();
        let status = super::super::isolated_python_module_command(
            &python,
            &snapshot,
            "scripts.module_that_does_not_exist",
        )
        .unwrap()
        .status()
        .unwrap();
        assert!(!status.success());
        snapshot.cleanup().unwrap();
        assert!(!owner.exists());
    }

    #[test]
    fn cleanup_refuses_root_swap_and_preserves_replacement() {
        let tree = Tree::new("cleanup-root-swap");
        let root = fixture(&tree);
        let snapshot = verify_and_snapshot(&root, &tree.0).unwrap();
        let snapshot_root = snapshot.root.clone();
        let moved = snapshot.owner.join("original-source");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&snapshot.owner, fs::Permissions::from_mode(0o700)).unwrap();
            fs::set_permissions(&snapshot_root, fs::Permissions::from_mode(0o700)).unwrap();
        }
        fs::rename(&snapshot_root, &moved).unwrap();
        fs::create_dir(&snapshot_root).unwrap();
        fs::write(snapshot_root.join("replacement"), b"preserve").unwrap();
        let error = snapshot.cleanup().unwrap_err();
        assert!(error.to_string().contains("refusing"));
        assert_eq!(
            fs::read(snapshot_root.join("replacement")).unwrap(),
            b"preserve"
        );
    }

    #[cfg(not(unix))]
    #[test]
    fn nonunix_snapshot_creation_is_fail_closed_before_path_mutation() {
        let tree = Tree::new("nonunix-disabled");
        let snapshot_parent = tree.0.join("snapshot-parent-must-not-exist");
        let error = verify_and_snapshot_against_manifest(
            &tree.0.join("runtime-must-not-be-read"),
            &snapshot_parent,
            b"not a manifest",
        )
        .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::Unsupported);
        assert!(!snapshot_parent.exists());
    }
}
