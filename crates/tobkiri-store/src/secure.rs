use crate::StoreError;
#[cfg(unix)]
use std::{
    ffi::CString,
    os::fd::{AsRawFd, FromRawFd},
};
use std::{
    fs::File,
    io::{Read, Write},
    path::{Component, Path, PathBuf},
    sync::Arc,
};
use tobkiri_canon::{Digest, ids::ProfileId};
use tobkiri_gate::ApprovalStore;

#[cfg(all(test, unix))]
#[path = "profile_inventory_tests.rs"]
mod profile_inventory_tests;

/// Native stat fields differ in width and signedness across supported Unix
/// platforms. Reject an unrepresentable field instead of truncating identity.
#[cfg(unix)]
pub(crate) fn checked_file_identity(stat: &libc::stat) -> Result<(u64, u64), StoreError> {
    fn field<T: TryInto<u64>>(value: T) -> Result<u64, StoreError> {
        value
            .try_into()
            .map_err(|_| StoreError::Invalid("file identity outside supported range".into()))
    }
    Ok((field(stat.st_dev)?, field(stat.st_ino)?))
}

/// Reject traversal, absolute paths, NULs and platform separator ambiguities.
pub fn validate_relative(path: &Path) -> Result<Vec<String>, StoreError> {
    let mut parts = Vec::new();
    for c in path.components() {
        match c {
            Component::Normal(s) => {
                let s = s
                    .to_str()
                    .ok_or_else(|| StoreError::Invalid("non-UTF8 path".into()))?;
                if s.is_empty() || s.contains(['\\', '\0']) || s == "." || s == ".." {
                    return Err(StoreError::Invalid("unsafe path component".into()));
                }
                parts.push(s.into())
            }
            _ => {
                return Err(StoreError::Invalid(
                    "path must be relative without traversal".into(),
                ));
            }
        }
    }
    if parts.is_empty() {
        return Err(StoreError::Invalid("empty path".into()));
    }
    Ok(parts)
}
pub(crate) fn name(s: &str) -> Result<(), StoreError> {
    if s.is_empty()
        || s.len() > 240
        || s == "."
        || s == ".."
        || !s
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"._-".contains(&c))
    {
        return Err(StoreError::Invalid("invalid store/name identifier".into()));
    }
    Ok(())
}

/// Directory capability pinned to an open descriptor. All descendants use openat.
#[derive(Clone)]
pub struct DataRoot {
    path: PathBuf,
    #[cfg(unix)]
    directory: Arc<File>,
}
#[cfg(unix)]
pub(crate) struct ProfileLock {
    pub(crate) directory: DataRoot,
    _lock: File,
}
impl std::fmt::Debug for DataRoot {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("DataRoot")
            .field("path", &self.path)
            .finish()
    }
}
impl DataRoot {
    /// Open/create an absolute root, refusing a symlink in every path component.
    pub fn open(path: impl AsRef<Path>) -> Result<Self, StoreError> {
        let path = path.as_ref();
        if !path.is_absolute() {
            return Err(StoreError::Invalid("data root must be absolute".into()));
        }
        #[cfg(unix)]
        {
            let fd = unsafe {
                libc::open(
                    c"/".as_ptr(),
                    libc::O_RDONLY | libc::O_DIRECTORY | libc::O_CLOEXEC,
                )
            };
            if fd < 0 {
                return Err(std::io::Error::last_os_error().into());
            }
            let mut dir = unsafe { File::from_raw_fd(fd) };
            for part in path.components() {
                match part {
                    Component::RootDir => {}
                    Component::Normal(s) => {
                        let s = s
                            .to_str()
                            .ok_or_else(|| StoreError::Invalid("non-UTF8 data root".into()))?;
                        dir = open_dir(&dir, s, true)?
                    }
                    _ => return Err(StoreError::Invalid("unsafe data root".into())),
                }
            }
            Ok(Self {
                path: path.to_owned(),
                directory: Arc::new(dir),
            })
        }
        #[cfg(not(unix))]
        {
            let _ = path;
            Err(StoreError::Unavailable(
                "descriptor-relative nofollow storage is unsupported on this platform".into(),
            ))
        }
    }
    /// Path for diagnostics only; effect paths use the pinned directory capability.
    pub fn path(&self) -> &Path {
        &self.path
    }
    /// Duplicate the original pinned directory for trusted Core filesystem
    /// protection. This is not a guest root or a pathname-based reopen.
    pub fn filesystem_protection_handle(&self) -> Result<File, StoreError> {
        #[cfg(unix)]
        {
            Ok(self.directory.try_clone()?)
        }
        #[cfg(not(unix))]
        {
            Err(StoreError::Unavailable(
                "descriptor-relative filesystem protection is unsupported".into(),
            ))
        }
    }
    /// Stable identity tuple for bounded registries keyed to this pinned root.
    /// It carries no descriptor and does not grant filesystem access.
    pub(crate) fn physical_identity_key(&self) -> Result<(u64, u64), StoreError> {
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            let metadata = self.directory.metadata()?;
            if !metadata.is_dir() {
                return Err(StoreError::Denied(
                    "root capability is not a directory".into(),
                ));
            }
            Ok((metadata.dev(), metadata.ino()))
        }
        #[cfg(not(unix))]
        {
            Err(StoreError::Unavailable(
                "physical root identity unsupported".into(),
            ))
        }
    }
    #[cfg(unix)]
    pub(crate) fn directory_capability(&self) -> Arc<File> {
        self.directory.clone()
    }
    /// Inventory only descriptor-bound Profile directories; never follow a symlink.
    pub fn list_profiles(&self) -> Result<Vec<ProfileId>, StoreError> {
        self.list_profiles_bounded(usize::MAX)
    }

    /// Enumerate at most `maximum` descriptor-bound Profile directory identities.
    /// A larger inventory fails with `Quota` before retaining another entry;
    /// callers never receive a partial list. This does not validate Profile
    /// heads or establish saved, approved, active, or ready state.
    pub fn list_profiles_bounded(&self, maximum: usize) -> Result<Vec<ProfileId>, StoreError> {
        #[cfg(unix)]
        {
            let directory = match open_dir(&self.directory, "profiles", false) {
                Ok(dir) => dir,
                Err(StoreError::Io(error)) if error.kind() == std::io::ErrorKind::NotFound => {
                    return Ok(Vec::new());
                }
                Err(error) => return Err(error),
            };
            use std::ffi::CStr;
            let fd = unsafe { libc::fcntl(directory.as_raw_fd(), libc::F_DUPFD_CLOEXEC, 0) };
            if fd < 0 {
                return Err(std::io::Error::last_os_error().into());
            }
            let pointer = unsafe { libc::fdopendir(fd) };
            if pointer.is_null() {
                let error = std::io::Error::last_os_error();
                unsafe {
                    libc::close(fd);
                }
                return Err(error.into());
            }
            struct Stream(*mut libc::DIR);
            impl Drop for Stream {
                fn drop(&mut self) {
                    unsafe {
                        libc::closedir(self.0);
                    }
                }
            }
            let stream = Stream(pointer);
            let mut profiles = Vec::new();
            loop {
                // readdir uses the same null result for EOF and failure.
                // Neither a stale errno nor a failed scan proves completeness.
                errno::set_errno(errno::Errno(0));
                let entry = unsafe { libc::readdir(stream.0) };
                if entry.is_null() {
                    let error = errno::errno();
                    if error.0 != 0 {
                        return Err(std::io::Error::from_raw_os_error(error.0).into());
                    }
                    break;
                }
                let bytes = unsafe { CStr::from_ptr((*entry).d_name.as_ptr()) };
                let value = bytes
                    .to_str()
                    .map_err(|_| StoreError::Invalid("non-UTF8 Profile directory".into()))?;
                if value == "." || value == ".." {
                    continue;
                }
                let profile =
                    ProfileId::parse(value).map_err(|e| StoreError::Invalid(e.to_string()))?;
                let _pinned = open_dir(&directory, value, false)?;
                if profiles.len() == maximum {
                    return Err(StoreError::Quota);
                }
                profiles.push(profile);
            }
            profiles.sort();
            Ok(profiles)
        }
        #[cfg(not(unix))]
        {
            let _ = maximum;
            Err(StoreError::Unavailable(
                "descriptor-bound Profile inventory unavailable".into(),
            ))
        }
    }
    pub fn profile_relative(profile: &ProfileId, file: &str) -> Result<PathBuf, StoreError> {
        name(profile.as_str())?;
        validate_relative(Path::new(file))?;
        Ok(PathBuf::from("profiles").join(profile.as_str()).join(file))
    }
    pub fn read_profile_file(
        &self,
        profile: &ProfileId,
        file: &str,
        max: usize,
    ) -> Result<Vec<u8>, StoreError> {
        self.read(Self::profile_relative(profile, file)?, max)
    }
    pub fn write_profile_file_atomic(
        &self,
        profile: &ProfileId,
        file: &str,
        bytes: &[u8],
    ) -> Result<(), StoreError> {
        self.write_atomic(Self::profile_relative(profile, file)?, bytes)
    }
    #[cfg(unix)]
    pub(crate) fn profile_lock(
        &self,
        profile: &ProfileId,
        file: &str,
    ) -> Result<ProfileLock, StoreError> {
        if !matches!(file, ".revision.lock" | ".vault.lock" | ".vault-key.lock") {
            return Err(StoreError::Invalid("unknown Profile lock domain".into()));
        }
        name(profile.as_str())?;
        let profiles = open_dir(&self.directory, "profiles", true)?;
        let dir = open_dir(&profiles, profile.as_str(), true)?;
        // Lock the pinned Profile inode itself; unlink/replacement of an old
        // conventional .revision.lock file cannot create a second lock domain.
        let lock = open_dir(&dir, ".", false)?;
        if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX) } != 0 {
            return Err(std::io::Error::last_os_error().into());
        }
        Ok(ProfileLock {
            directory: DataRoot {
                path: self.path.join("profiles").join(profile.as_str()),
                directory: Arc::new(dir),
            },
            _lock: lock,
        })
    }
    /// Cross-process compare-and-swap commit of a compiled Profile revision.
    pub fn compare_and_swap_profile_head(
        &self,
        profile: &ProfileId,
        expected: Option<&Digest>,
        new: &Digest,
    ) -> Result<(), StoreError> {
        self.compare_and_swap_profile_head_inner(profile, expected, None, new, File::sync_all)
            .map(|_| ())
    }
    /// Commit only if both the head and approval epoch still match the reviewed
    /// state. Returns the new head and the epoch acquired by this commit.
    ///
    /// The Profile lock orders head writers and snapshots; the epoch comparison
    /// and advancement share one SQLite transaction. A later revocation may
    /// advance the epoch before this call returns, so activation still requires
    /// an exact current snapshot. An I/O error may follow epoch advancement or
    /// head replacement and does not establish that this commit had no effect.
    /// Such an error carries `ProfileCommitProgress`; it is never a receipt or
    /// proof that the old head/epoch were restored.
    pub fn compare_and_swap_profile_head_at_epoch(
        &self,
        profile: &ProfileId,
        expected: Option<&Digest>,
        expected_epoch: u64,
        new: &Digest,
    ) -> Result<(Digest, u64), StoreError> {
        self.compare_and_swap_profile_head_inner(
            profile,
            expected,
            Some(expected_epoch),
            new,
            File::sync_all,
        )
    }
    fn compare_and_swap_profile_head_inner(
        &self,
        profile: &ProfileId,
        expected: Option<&Digest>,
        expected_epoch: Option<u64>,
        new: &Digest,
        sync_directory: impl FnOnce(&File) -> std::io::Result<()>,
    ) -> Result<(Digest, u64), StoreError> {
        #[cfg(unix)]
        {
            let lock = self.profile_lock(profile, ".revision.lock")?;
            let current = match lock.directory.read("head", 256) {
                Ok(raw) => Some(
                    Digest::parse(
                        std::str::from_utf8(&raw)
                            .map_err(|_| StoreError::Invalid("invalid Profile head".into()))?,
                    )
                    .map_err(|e| StoreError::Canon(e.to_string()))?,
                ),
                Err(StoreError::Io(e)) if e.kind() == std::io::ErrorKind::NotFound => None,
                Err(e) => return Err(e),
            };
            if current.as_ref() != expected {
                return Err(StoreError::Conflict("Profile head changed".into()));
            }
            // The revision lock orders this commit with head writers/snapshots.
            // On I/O failure an advanced epoch still fails closed for old grants.
            let approvals = crate::Approvals::open_pinned(&lock.directory, profile)?;
            let epoch = approvals.advance_profile_security_epoch(profile, expected_epoch)?;
            let mut head_replaced = false;
            let written = lock.directory.write_atomic_with_directory_sync(
                Path::new("head"),
                new.as_str().as_bytes(),
                |directory| {
                    // The writer calls this only after the actual rename.
                    head_replaced = true;
                    sync_directory(directory)
                },
            );
            written.map_err(|source| StoreError::ProfileCommitUncertain {
                progress: if head_replaced {
                    crate::ProfileCommitProgress::HeadReplaced
                } else {
                    crate::ProfileCommitProgress::EpochAdvanced
                },
                source: Box::new(source),
            })?;
            Ok((new.clone(), epoch))
        }
        #[cfg(not(unix))]
        {
            let _ = (profile, expected, expected_epoch, new, sync_directory);
            Err(StoreError::Unavailable(
                "Profile CAS locking unavailable".into(),
            ))
        }
    }
    /// Snapshot immutable head and security epoch under the same process-shared lock.
    pub fn profile_head_snapshot(&self, profile: &ProfileId) -> Result<(Digest, u64), StoreError> {
        #[cfg(unix)]
        let lock = self.profile_lock(profile, ".revision.lock")?;
        #[cfg(unix)]
        let raw = lock.directory.read("head", 256)?;
        #[cfg(not(unix))]
        let raw = self.read_profile_file(profile, "head", 256)?;
        let head = Digest::parse(
            std::str::from_utf8(&raw)
                .map_err(|_| StoreError::Invalid("invalid Profile head".into()))?,
        )
        .map_err(|e| StoreError::Canon(e.to_string()))?;
        #[cfg(unix)]
        let epoch =
            crate::Approvals::open_pinned(&lock.directory, profile)?.security_epoch(profile)?;
        #[cfg(not(unix))]
        let epoch = crate::Approvals::open(self, profile)?.security_epoch(profile)?;
        Ok((head, epoch))
    }
    pub fn read(&self, path: impl AsRef<Path>, max: usize) -> Result<Vec<u8>, StoreError> {
        #[cfg(unix)]
        {
            let (dir, last) = self.parent(path.as_ref(), false)?;
            let file = open_file(&dir, &last, false, false)?;
            let metadata = file.metadata()?;
            if !metadata.is_file() || metadata.len() > max as u64 {
                return Err(StoreError::Quota);
            }
            let mut bytes = Vec::new();
            file.take(max as u64 + 1).read_to_end(&mut bytes)?;
            if bytes.len() > max {
                return Err(StoreError::Quota);
            }
            Ok(bytes)
        }
        #[cfg(not(unix))]
        {
            let _ = (path, max);
            Err(StoreError::Unavailable(
                "nofollow file read unavailable".into(),
            ))
        }
    }
    /// Return the size of a descriptor-relative regular file without reading
    /// its contents. Missing files stay distinguishable from unsafe objects so
    /// callers can inspect SQLite sidecars without following symlinks.
    pub(crate) fn file_len(&self, path: impl AsRef<Path>) -> Result<Option<u64>, StoreError> {
        #[cfg(unix)]
        {
            let (dir, last) = match self.parent(path.as_ref(), false) {
                Ok(parent) => parent,
                Err(StoreError::Io(error)) if error.kind() == std::io::ErrorKind::NotFound => {
                    return Ok(None);
                }
                Err(error) => return Err(error),
            };
            let name = cstring(&last)?;
            let mut stat = std::mem::MaybeUninit::<libc::stat>::uninit();
            if unsafe {
                libc::fstatat(
                    dir.as_raw_fd(),
                    name.as_ptr(),
                    stat.as_mut_ptr(),
                    libc::AT_SYMLINK_NOFOLLOW,
                )
            } != 0
            {
                let error = std::io::Error::last_os_error();
                if error.kind() == std::io::ErrorKind::NotFound {
                    return Ok(None);
                }
                return Err(error.into());
            }
            let stat = unsafe { stat.assume_init() };
            if stat.st_mode & libc::S_IFMT != libc::S_IFREG
                || stat.st_nlink != 1
                || stat.st_size < 0
            {
                return Err(StoreError::Denied(
                    "DATA inode must be a regular single-link file".into(),
                ));
            }
            Ok(Some(stat.st_size as u64))
        }
        #[cfg(not(unix))]
        {
            let _ = path;
            Err(StoreError::Unavailable(
                "nofollow file metadata unavailable".into(),
            ))
        }
    }
    /// Open an existing or newly created guest DATA database through the
    /// descriptor-pinned VFS without changing its journal mode. Authority,
    /// catalog and audit databases continue to use `database` and its own
    /// trusted setup.
    pub(crate) fn data_database_with_disk_policy(
        &self,
        path: &Path,
        create: bool,
        disk_policy: crate::StoreDiskPolicy,
        custody: Arc<crate::data_custody::DataCustody>,
    ) -> Result<crate::data_custody::DataConnection, StoreError> {
        #[cfg(unix)]
        {
            let (dir, last) = self.parent(path, create)?;
            // The quota VFS performs the first create/open and nofollow inode
            // validation under its physical quota lock. An extra database FD
            // opened here would disrupt SQLite's process-wide POSIX locks when
            // closed while another connection retains a transaction.
            let (virtual_path, guard) =
                crate::vfs::register_guest_data(dir, &last, disk_policy, custody.clone())?;
            let mut flags = rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE
                | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX
                | rusqlite::OpenFlags::SQLITE_OPEN_URI;
            if create {
                flags |= rusqlite::OpenFlags::SQLITE_OPEN_CREATE;
            }
            let connection = rusqlite::Connection::open_with_flags_and_vfs(
                virtual_path,
                flags,
                crate::quota_vfs::NAME,
            )?;
            Ok(crate::data_custody::DataConnection {
                connection,
                directory: guard,
                custody,
            })
        }
        #[cfg(not(unix))]
        {
            let _ = (path, create, disk_policy, custody);
            Err(StoreError::Unavailable(
                "nofollow SQLite DATA unavailable".into(),
            ))
        }
    }
    /// Reserve a descriptor-pinned DATA route for a takeover. This creates no
    /// SQLite/database descriptor; its existing VFS slot retains failed moves.
    pub(crate) fn data_transfer_registration(
        &self,
        path: &Path,
        create_parent: bool,
        policy: crate::StoreDiskPolicy,
        custody: Arc<crate::data_custody::DataCustody>,
    ) -> Result<crate::vfs::DatabaseGuard, StoreError> {
        #[cfg(unix)]
        {
            let (directory, leaf) = self.parent(path, create_parent)?;
            let (_, guard) = crate::vfs::register_guest_data(directory, &leaf, policy, custody)?;
            Ok(guard)
        }
        #[cfg(not(unix))]
        {
            let _ = (path, create_parent, policy, custody);
            Err(StoreError::Unavailable(
                "native DATA takeover unavailable".into(),
            ))
        }
    }
    /// Identity-only nofollow inspection never opens a database inode.
    pub(crate) fn move_path_identity(&self, path: &Path) -> Result<Option<(u64, u64)>, StoreError> {
        #[cfg(unix)]
        {
            let (directory, name) = match self.parent(path, false) {
                Ok(parent) => parent,
                Err(StoreError::Io(error)) if error.kind() == std::io::ErrorKind::NotFound => {
                    return Ok(None);
                }
                Err(error) => return Err(error),
            };
            let name = cstring(&name)?;
            let mut stat = std::mem::MaybeUninit::<libc::stat>::uninit();
            if unsafe {
                libc::fstatat(
                    directory.as_raw_fd(),
                    name.as_ptr(),
                    stat.as_mut_ptr(),
                    libc::AT_SYMLINK_NOFOLLOW,
                )
            } != 0
            {
                let error = std::io::Error::last_os_error();
                return if error.kind() == std::io::ErrorKind::NotFound {
                    Ok(None)
                } else {
                    Err(error.into())
                };
            }
            let stat = unsafe { stat.assume_init() };
            if !matches!(stat.st_mode & libc::S_IFMT, libc::S_IFDIR | libc::S_IFREG) {
                return Err(StoreError::Denied(
                    "move source is not a regular file or directory".into(),
                ));
            }
            Ok(Some(checked_file_identity(&stat)?))
        }
        #[cfg(not(unix))]
        {
            let _ = path;
            Err(StoreError::Unavailable(
                "native DATA move identity unavailable".into(),
            ))
        }
    }
    /// Narrow an already-open physical DATA group without opening another
    /// database descriptor (closing one would disturb SQLite's POSIX locks).
    pub(crate) fn validate_data_disk_policy(
        &self,
        path: &Path,
        disk_policy: crate::StoreDiskPolicy,
    ) -> Result<(), StoreError> {
        #[cfg(unix)]
        {
            let (directory, leaf) = self.parent(path, false)?;
            crate::quota_vfs::QuotaDomain::acquire(Arc::new(directory), &leaf, disk_policy)?;
            Ok(())
        }
        #[cfg(not(unix))]
        {
            let _ = (path, disk_policy);
            Err(StoreError::Unavailable(
                "nofollow SQLite DATA unavailable".into(),
            ))
        }
    }
    /// Atomic same-directory replace, private file mode, fsynced before rename.
    pub fn write_atomic(&self, path: impl AsRef<Path>, bytes: &[u8]) -> Result<(), StoreError> {
        self.write_atomic_with_directory_sync(path.as_ref(), bytes, File::sync_all)
    }
    fn write_atomic_with_directory_sync(
        &self,
        path: &Path,
        bytes: &[u8],
        sync_directory: impl FnOnce(&File) -> std::io::Result<()>,
    ) -> Result<(), StoreError> {
        #[cfg(unix)]
        {
            let (dir, last) = self.parent(path, true)?;
            let tmp = temporary_name()?;
            let mut file = open_file(&dir, &tmp, true, true)?;
            file.write_all(bytes)?;
            file.sync_all()?;
            let source = cstring(&tmp)?;
            let target = cstring(&last)?;
            let status = unsafe {
                libc::renameat(
                    dir.as_raw_fd(),
                    source.as_ptr(),
                    dir.as_raw_fd(),
                    target.as_ptr(),
                )
            };
            if status != 0 {
                let _ = unsafe { libc::unlinkat(dir.as_raw_fd(), source.as_ptr(), 0) };
                return Err(std::io::Error::last_os_error().into());
            }
            sync_directory(&dir)?;
            Ok(())
        }
        #[cfg(not(unix))]
        {
            let _ = (path, bytes, sync_directory);
            Err(StoreError::Unavailable(
                "atomic nofollow write unavailable".into(),
            ))
        }
    }
    pub fn rename(
        &self,
        source: impl AsRef<Path>,
        target: impl AsRef<Path>,
    ) -> Result<(), StoreError> {
        #[cfg(unix)]
        {
            let (a, an) = self.parent(source.as_ref(), false)?;
            let (b, bn) = self.parent(target.as_ref(), true)?;
            let an = cstring(&an)?;
            let bn = cstring(&bn)?;
            #[cfg(target_os = "macos")]
            let status = unsafe {
                libc::renameatx_np(
                    a.as_raw_fd(),
                    an.as_ptr(),
                    b.as_raw_fd(),
                    bn.as_ptr(),
                    libc::RENAME_EXCL,
                )
            };
            #[cfg(target_os = "linux")]
            let status = unsafe {
                libc::renameat2(
                    a.as_raw_fd(),
                    an.as_ptr(),
                    b.as_raw_fd(),
                    bn.as_ptr(),
                    libc::RENAME_NOREPLACE,
                )
            };
            #[cfg(not(any(target_os = "macos", target_os = "linux")))]
            return Err(StoreError::Unavailable(
                "atomic no-replace rename unavailable".into(),
            ));
            if status != 0 {
                return Err(std::io::Error::last_os_error().into());
            }
            a.sync_all()?;
            b.sync_all()?;
            Ok(())
        }
        #[cfg(not(unix))]
        {
            let _ = (source, target);
            Err(StoreError::Unavailable(
                "nofollow rename unavailable".into(),
            ))
        }
    }
    #[cfg(unix)]
    pub(crate) fn parent(&self, path: &Path, create: bool) -> Result<(File, String), StoreError> {
        let mut parts = validate_relative(path)?;
        let last = parts.pop().unwrap();
        let mut dir = self.directory.try_clone()?;
        for part in parts {
            dir = open_dir(&dir, &part, create)?
        }
        Ok((dir, last))
    }
    pub(crate) fn database(
        &self,
        path: &Path,
    ) -> Result<(rusqlite::Connection, crate::vfs::DatabaseGuard), StoreError> {
        self.database_mode(path, true)
    }
    fn database_mode(
        &self,
        path: &Path,
        create: bool,
    ) -> Result<(rusqlite::Connection, crate::vfs::DatabaseGuard), StoreError> {
        #[cfg(unix)]
        {
            let (dir, last) = self.parent(path, create)?;
            // Inspect the pathname without opening a second database descriptor:
            // closing any descriptor for an inode drops this process's POSIX
            // locks, including locks retained by another SQLite connection.
            let name = cstring(&last)?;
            let mut stat = std::mem::MaybeUninit::<libc::stat>::uninit();
            if unsafe {
                libc::fstatat(
                    dir.as_raw_fd(),
                    name.as_ptr(),
                    stat.as_mut_ptr(),
                    libc::AT_SYMLINK_NOFOLLOW,
                )
            } == 0
            {
                let stat = unsafe { stat.assume_init() };
                if stat.st_mode & libc::S_IFMT != libc::S_IFREG {
                    return Err(StoreError::Denied("database is not a regular file".into()));
                }
            } else {
                let error = std::io::Error::last_os_error();
                if !create || error.kind() != std::io::ErrorKind::NotFound {
                    return Err(error.into());
                }
            }
            let (virtual_path, guard) = crate::vfs::register(dir, &last)?;
            let mut flags = rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE
                | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX
                | rusqlite::OpenFlags::from_bits_retain(0x01000000);
            if create {
                flags |= rusqlite::OpenFlags::SQLITE_OPEN_CREATE;
            }
            let conn = rusqlite::Connection::open_with_flags(virtual_path, flags)?;
            conn.busy_timeout(std::time::Duration::from_secs(2))?;
            if create {
                conn.execute_batch("PRAGMA journal_mode=DELETE;")?;
            }
            conn.execute_batch(
                "PRAGMA trusted_schema=OFF; PRAGMA foreign_keys=ON; PRAGMA temp_store=MEMORY;",
            )?;
            Ok((conn, guard))
        }
        #[cfg(not(unix))]
        {
            let _ = (path, create);
            Err(StoreError::Unavailable(
                "nofollow SQLite unavailable".into(),
            ))
        }
    }
}
#[cfg(unix)]
fn cstring(s: &str) -> Result<CString, StoreError> {
    CString::new(s).map_err(|_| StoreError::Invalid("NUL in path".into()))
}
#[cfg(unix)]
fn open_dir(parent: &File, s: &str, create: bool) -> Result<File, StoreError> {
    let s = cstring(s)?;
    if create {
        let result = unsafe { libc::mkdirat(parent.as_raw_fd(), s.as_ptr(), 0o700) };
        if result != 0
            && std::io::Error::last_os_error().kind() != std::io::ErrorKind::AlreadyExists
        {
            return Err(std::io::Error::last_os_error().into());
        }
    }
    let fd = unsafe {
        libc::openat(
            parent.as_raw_fd(),
            s.as_ptr(),
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
        )
    };
    if fd < 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    Ok(unsafe { File::from_raw_fd(fd) })
}
#[cfg(unix)]
fn open_file(parent: &File, s: &str, create: bool, exclusive: bool) -> Result<File, StoreError> {
    let s = cstring(s)?;
    let mut flags = libc::O_NOFOLLOW
        | libc::O_CLOEXEC
        | if create {
            libc::O_RDWR | libc::O_CREAT
        } else {
            libc::O_RDONLY
        };
    if exclusive {
        flags |= libc::O_EXCL
    }
    let fd = unsafe { libc::openat(parent.as_raw_fd(), s.as_ptr(), flags, 0o600) };
    if fd < 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    let file = unsafe { File::from_raw_fd(fd) };
    if !file.metadata()?.is_file() {
        return Err(StoreError::Denied("not a regular file".into()));
    }
    Ok(file)
}
fn temporary_name() -> Result<String, StoreError> {
    let mut random = [0; 16];
    getrandom::fill(&mut random).map_err(|e| StoreError::Unavailable(e.to_string()))?;
    Ok(format!(".tmp-{}", hex::encode(random)))
}

/// Immutable SHA-256 content-addressed artifacts shared across Profiles.
#[derive(Clone, Debug)]
pub struct Cas {
    root: DataRoot,
}
impl Cas {
    pub fn new(root: DataRoot) -> Self {
        Self { root }
    }
    pub fn put(&self, bytes: &[u8]) -> Result<Digest, StoreError> {
        let digest = Digest::hash(bytes);
        let path = PathBuf::from("cas/sha256").join(digest.hex());
        #[cfg(unix)]
        {
            let (dir, last) = self.root.parent(&path, true)?;
            let tmp = temporary_name()?;
            let mut file = open_file(&dir, &tmp, true, true)?;
            file.write_all(bytes)?;
            file.sync_all()?;
            let temp = cstring(&tmp)?;
            let target = cstring(&last)?;
            let status = unsafe {
                libc::linkat(
                    dir.as_raw_fd(),
                    temp.as_ptr(),
                    dir.as_raw_fd(),
                    target.as_ptr(),
                    0,
                )
            };
            let err = std::io::Error::last_os_error();
            let _ = unsafe { libc::unlinkat(dir.as_raw_fd(), temp.as_ptr(), 0) };
            if status != 0 && err.kind() != std::io::ErrorKind::AlreadyExists {
                return Err(err.into());
            }
            dir.sync_all()?;
            let existing = self.get(&digest, bytes.len())?;
            if existing != bytes {
                return Err(StoreError::Conflict(
                    "CAS digest collision or tampering".into(),
                ));
            }
            Ok(digest)
        }
        #[cfg(not(unix))]
        {
            let _ = path;
            Err(StoreError::Unavailable("nofollow CAS unavailable".into()))
        }
    }
    pub fn get(&self, digest: &Digest, max: usize) -> Result<Vec<u8>, StoreError> {
        let bytes = self
            .root
            .read(PathBuf::from("cas/sha256").join(digest.hex()), max)?;
        if Digest::hash(&bytes) != *digest {
            return Err(StoreError::Conflict("CAS bytes do not match digest".into()));
        }
        Ok(bytes)
    }
}

#[cfg(all(test, unix))]
mod database_lock_tests {
    use super::*;
    use std::{process::Command, time::Duration};

    #[test]
    fn child_database_write_probe() {
        let Some(path) = std::env::var_os("TOBKIRI_TRUSTED_DB_LOCK_PROBE") else {
            return;
        };
        let connection = rusqlite::Connection::open(path).unwrap();
        connection.busy_timeout(Duration::ZERO).unwrap();
        let result = connection.execute("INSERT INTO locked VALUES(2)", []);
        if std::env::var_os("TOBKIRI_TRUSTED_DB_EXPECT_BUSY").is_some() {
            assert!(matches!(
                result,
                Err(rusqlite::Error::SqliteFailure(error, _))
                    if error.code == rusqlite::ErrorCode::DatabaseBusy
            ));
        } else {
            result.unwrap();
        }
    }

    #[test]
    fn trusted_path_validation_keeps_an_existing_sqlite_writer_locked() {
        let temp = tempfile::tempdir().unwrap();
        let root = DataRoot::open(temp.path().canonicalize().unwrap()).unwrap();
        let path = Path::new("trusted.sqlite");
        let (connection, _guard) = root.database(path).unwrap();
        connection
            .execute_batch(
                "CREATE TABLE locked(value INTEGER); BEGIN IMMEDIATE; INSERT INTO locked VALUES(1);",
            )
            .unwrap();
        let probe = |busy| {
            let mut command = Command::new(std::env::current_exe().unwrap());
            command
                .args([
                    "--exact",
                    "secure::database_lock_tests::child_database_write_probe",
                    "--nocapture",
                ])
                .env("TOBKIRI_TRUSTED_DB_LOCK_PROBE", root.path().join(path));
            if busy {
                command.env("TOBKIRI_TRUSTED_DB_EXPECT_BUSY", "1");
            }
            let output = command.output().unwrap();
            assert!(
                output.status.success(),
                "child failed: {} {}",
                String::from_utf8_lossy(&output.stdout),
                String::from_utf8_lossy(&output.stderr)
            );
        };
        probe(true);
        // Reopening performs the real trusted-path validation and SQLite setup.
        // Its descriptors are all owned by SQLite's coordinated inode state.
        let reopened = root.database(path).unwrap();
        probe(true);
        drop(reopened);
        probe(true);
        connection.execute_batch("ROLLBACK;").unwrap();
        probe(false);
    }
}

#[cfg(all(test, unix))]
mod revision_lock_tests {
    use super::*;
    use std::{sync::mpsc, time::Duration};

    fn epoch_cas_fixture() -> (tempfile::TempDir, DataRoot, ProfileId) {
        let temp = tempfile::tempdir().unwrap();
        let root = DataRoot::open(temp.path().canonicalize().unwrap()).unwrap();
        let profile = ProfileId::parse("prof_epoch_cas").unwrap();
        (temp, root, profile)
    }

    #[test]
    fn epoch_bound_cas_returns_the_exact_acquired_epoch_and_commits_once() {
        let (_temp, root, profile) = epoch_cas_fixture();
        let first = Digest::hash(b"first");
        let next = Digest::hash(b"next");
        assert_eq!(
            root.compare_and_swap_profile_head_at_epoch(&profile, None, 0, &first)
                .unwrap(),
            (first.clone(), 1)
        );
        let reviewed = root.profile_head_snapshot(&profile).unwrap();
        let committed = root
            .compare_and_swap_profile_head_at_epoch(&profile, Some(&reviewed.0), reviewed.1, &next)
            .unwrap();
        assert_eq!(committed, (next, reviewed.1 + 1));
        assert_eq!(root.profile_head_snapshot(&profile).unwrap(), committed);
        assert!(matches!(
            root.compare_and_swap_profile_head_at_epoch(
                &profile,
                Some(&reviewed.0),
                reviewed.1,
                &Digest::hash(b"replay"),
            ),
            Err(StoreError::Conflict(_))
        ));
        assert_eq!(root.profile_head_snapshot(&profile).unwrap(), committed);
    }

    #[test]
    fn epoch_only_revocation_rejects_a_stale_review_without_changing_the_head() {
        let (_temp, root, profile) = epoch_cas_fixture();
        let first = Digest::hash(b"first");
        root.compare_and_swap_profile_head(&profile, None, &first)
            .unwrap();
        let reviewed = root.profile_head_snapshot(&profile).unwrap();
        let revoked_epoch = crate::Approvals::open(&root, &profile)
            .unwrap()
            .advance_security_epoch(&profile)
            .unwrap();
        assert!(matches!(
            root.compare_and_swap_profile_head_at_epoch(
                &profile,
                Some(&reviewed.0),
                reviewed.1,
                &Digest::hash(b"unreviewed epoch"),
            ),
            Err(StoreError::Conflict(_))
        ));
        assert_eq!(
            root.profile_head_snapshot(&profile).unwrap(),
            (first, revoked_epoch)
        );
    }

    #[test]
    fn returning_to_the_same_head_does_not_accept_a_stale_epoch() {
        let (_temp, root, profile) = epoch_cas_fixture();
        let first = Digest::hash(b"first");
        let intermediate = Digest::hash(b"intermediate");
        root.compare_and_swap_profile_head(&profile, None, &first)
            .unwrap();
        let reviewed = root.profile_head_snapshot(&profile).unwrap();
        root.compare_and_swap_profile_head(&profile, Some(&first), &intermediate)
            .unwrap();
        root.compare_and_swap_profile_head(&profile, Some(&intermediate), &first)
            .unwrap();
        assert!(matches!(
            root.compare_and_swap_profile_head_at_epoch(
                &profile,
                Some(&reviewed.0),
                reviewed.1,
                &Digest::hash(b"stale edit"),
            ),
            Err(StoreError::Conflict(_))
        ));
        assert_eq!(root.profile_head_snapshot(&profile).unwrap(), (first, 3));
    }

    #[test]
    fn epoch_bound_cas_keeps_head_conflicts_side_effect_free() {
        let (_temp, root, profile) = epoch_cas_fixture();
        let first = Digest::hash(b"first");
        root.compare_and_swap_profile_head(&profile, None, &first)
            .unwrap();
        let before = root.profile_head_snapshot(&profile).unwrap();
        assert!(matches!(
            root.compare_and_swap_profile_head_at_epoch(
                &profile,
                Some(&Digest::hash(b"other head")),
                before.1,
                &Digest::hash(b"next"),
            ),
            Err(StoreError::Conflict(_))
        ));
        assert_eq!(root.profile_head_snapshot(&profile).unwrap(), before);
    }

    #[test]
    fn competing_epoch_bound_commits_acquire_the_target_epoch_only_once() {
        let (_temp, root, profile) = epoch_cas_fixture();
        let first = Digest::hash(b"first");
        root.compare_and_swap_profile_head(&profile, None, &first)
            .unwrap();
        let reviewed = root.profile_head_snapshot(&profile).unwrap();
        let workers: Vec<_> = [Digest::hash(b"one"), Digest::hash(b"two")]
            .into_iter()
            .map(|next| {
                let root = root.clone();
                let profile = profile.clone();
                let reviewed = reviewed.clone();
                std::thread::spawn(move || {
                    root.compare_and_swap_profile_head_at_epoch(
                        &profile,
                        Some(&reviewed.0),
                        reviewed.1,
                        &next,
                    )
                })
            })
            .collect();
        let results: Vec<_> = workers
            .into_iter()
            .map(|worker| worker.join().unwrap())
            .collect();
        assert_eq!(results.iter().filter(|result| result.is_ok()).count(), 1);
        assert_eq!(
            results
                .iter()
                .filter(|result| matches!(result, Err(StoreError::Conflict(_))))
                .count(),
            1
        );
        let acquired = results.into_iter().find_map(Result::ok).unwrap();
        assert_eq!(acquired.1, reviewed.1 + 1);
        assert_eq!(root.profile_head_snapshot(&profile).unwrap(), acquired);
    }

    #[test]
    fn directory_sync_failure_after_head_replace_is_not_a_rollback() {
        let (_temp, root, profile) = epoch_cas_fixture();
        let first = Digest::hash(b"first");
        let next = Digest::hash(b"next");
        root.compare_and_swap_profile_head(&profile, None, &first)
            .unwrap();
        let reviewed = root.profile_head_snapshot(&profile).unwrap();
        let error = root
            .compare_and_swap_profile_head_inner(
                &profile,
                Some(&reviewed.0),
                Some(reviewed.1),
                &next,
                |_| Err(std::io::Error::other("injected directory fsync failure")),
            )
            .unwrap_err();
        assert_eq!(
            error.profile_commit_progress(),
            Some(crate::ProfileCommitProgress::HeadReplaced)
        );
        assert!(matches!(
            &error,
            StoreError::ProfileCommitUncertain { source, .. }
                if matches!(source.as_ref(), StoreError::Io(_))
        ));
        // The real rename preceded the failed directory fsync. Observe state
        // instead of treating the error as evidence that nothing committed.
        let observed = root.profile_head_snapshot(&profile).unwrap();
        assert_eq!(observed, (next.clone(), reviewed.1 + 1));
        assert!(matches!(
            root.compare_and_swap_profile_head_at_epoch(
                &profile,
                Some(&reviewed.0),
                reviewed.1,
                &next,
            ),
            Err(StoreError::Conflict(_))
        ));
        assert_eq!(root.profile_head_snapshot(&profile).unwrap(), observed);
    }

    #[test]
    fn acquired_epoch_receipt_does_not_hide_a_later_revocation() {
        let (_temp, root, profile) = epoch_cas_fixture();
        let first = Digest::hash(b"first");
        let next = Digest::hash(b"next");
        root.compare_and_swap_profile_head(&profile, None, &first)
            .unwrap();
        let reviewed = root.profile_head_snapshot(&profile).unwrap();
        let acquired = root
            .compare_and_swap_profile_head_inner(
                &profile,
                Some(&reviewed.0),
                Some(reviewed.1),
                &next,
                |directory| {
                    directory.sync_all()?;
                    // Revocation has its own SQLite transaction and can occur
                    // after this commit acquired its epoch, even before return.
                    crate::Approvals::open(&root, &profile)
                        .unwrap()
                        .advance_security_epoch(&profile)
                        .unwrap();
                    Ok(())
                },
            )
            .unwrap();
        assert_eq!(acquired, (next.clone(), reviewed.1 + 1));
        assert_eq!(
            root.profile_head_snapshot(&profile).unwrap(),
            (next, reviewed.1 + 2)
        );
    }

    #[test]
    fn replacing_legacy_lock_file_cannot_split_the_profile_directory_cas_domain() {
        let temp = tempfile::tempdir().unwrap();
        let root = DataRoot::open(temp.path().canonicalize().unwrap()).unwrap();
        let profile = ProfileId::parse("prof_pinned_lock").unwrap();
        let first = Digest::hash(b"first");
        let next = Digest::hash(b"next");
        root.compare_and_swap_profile_head(&profile, None, &first)
            .unwrap();
        let lock = root.profile_lock(&profile, ".revision.lock").unwrap();
        root.write_profile_file_atomic(&profile, ".revision.lock", b"replacement")
            .unwrap();
        let worker_root = root.clone();
        let worker_profile = profile.clone();
        let worker_next = next.clone();
        let (started, started_rx) = mpsc::channel();
        let (done, done_rx) = mpsc::channel();
        let worker = std::thread::spawn(move || {
            started.send(()).unwrap();
            done.send(worker_root.compare_and_swap_profile_head(
                &worker_profile,
                Some(&first),
                &worker_next,
            ))
            .unwrap();
        });
        started_rx.recv_timeout(Duration::from_secs(2)).unwrap();
        assert!(matches!(
            done_rx.recv_timeout(Duration::from_millis(50)),
            Err(mpsc::RecvTimeoutError::Timeout)
        ));
        drop(lock);
        done_rx
            .recv_timeout(Duration::from_secs(2))
            .unwrap()
            .unwrap();
        worker.join().unwrap();
        let (head, epoch) = root.profile_head_snapshot(&profile).unwrap();
        assert_eq!(head, next);
        assert_eq!(epoch, 2);
    }

    #[test]
    fn pinned_profile_operations_remain_in_the_locked_inode_after_name_replacement() {
        let temp = tempfile::tempdir().unwrap();
        let root = DataRoot::open(temp.path().canonicalize().unwrap()).unwrap();
        let profile = ProfileId::parse("prof_pinned_rename").unwrap();
        root.write_profile_file_atomic(&profile, "head", b"old")
            .unwrap();
        let lock = root.profile_lock(&profile, ".revision.lock").unwrap();
        let original = root.path().join("profiles").join(profile.as_str());
        let renamed = root.path().join("profiles/renamed");
        std::fs::rename(&original, &renamed).unwrap();
        root.write_profile_file_atomic(&profile, "head", b"replacement")
            .unwrap();
        lock.directory.write_atomic("head", b"pinned").unwrap();
        assert_eq!(std::fs::read(renamed.join("head")).unwrap(), b"pinned");
        assert_eq!(
            root.read_profile_file(&profile, "head", 256).unwrap(),
            b"replacement"
        );
        let approvals = crate::Approvals::open_pinned(&lock.directory, &profile).unwrap();
        approvals.advance_security_epoch(&profile).unwrap();
        assert_eq!(approvals.security_epoch(&profile).unwrap(), 1);
        assert_eq!(
            crate::Approvals::open(&root, &profile)
                .unwrap()
                .security_epoch(&profile)
                .unwrap(),
            0
        );
    }
}
