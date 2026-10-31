//! Descriptor-relative original persistence closure. No guest pathname or SQL.
use super::*;
use rusqlite::{
    Connection, OpenFlags, Params,
    types::{Value, ValueRef},
};
use std::{
    collections::BTreeMap,
    fs::{self, File},
    io::{Read, Write},
    path::{Component, PathBuf},
    sync::{Arc, Condvar, Mutex, OnceLock},
    time::{Duration, SystemTime, UNIX_EPOCH},
};
#[cfg(unix)]
use std::{
    ffi::CString,
    os::unix::{
        ffi::OsStrExt,
        fs::{MetadataExt, PermissionsExt},
        io::{AsRawFd, FromRawFd},
    },
};
use std::{
    mem::ManuallyDrop,
    ops::{Deref, DerefMut},
    sync::atomic::{AtomicBool, AtomicUsize, Ordering},
};
use tobkiri_gate::{ControlKind, ControlReservation, CoreControl};

#[derive(Default)]
pub(super) struct CleanupState {
    files: AtomicUsize,
    connections: AtomicUsize,
    uncertain: AtomicBool,
    #[cfg(unix)]
    retained: Mutex<Vec<RetainedConnection>>,
    _control: Option<ControlReservation>,
}
impl CleanupState {
    pub fn new(control: ControlReservation) -> Self {
        Self {
            _control: Some(control),
            ..Self::default()
        }
    }
    pub fn receipt(&self) -> OriginalAuthorityCloseReceipt {
        OriginalAuthorityCloseReceipt {
            open_files: self.files.load(Ordering::Acquire),
            open_connections: self.connections.load(Ordering::Acquire),
            uncertain: self.uncertain.load(Ordering::Acquire),
        }
    }
    pub fn verified(&self) -> bool {
        self.receipt().verified()
    }
}
#[cfg(unix)]
struct TrackedFile {
    value: ManuallyDrop<File>,
    state: Arc<CleanupState>,
}
#[cfg(unix)]
impl TrackedFile {
    fn new(value: File, state: Arc<CleanupState>) -> Self {
        state.files.fetch_add(1, Ordering::AcqRel);
        Self {
            value: ManuallyDrop::new(value),
            state,
        }
    }
    fn try_clone(&self) -> std::io::Result<Self> {
        Ok(Self::new(self.value.try_clone()?, self.state.clone()))
    }
}
#[cfg(unix)]
impl Deref for TrackedFile {
    type Target = File;
    fn deref(&self) -> &File {
        &self.value
    }
}
#[cfg(unix)]
impl DerefMut for TrackedFile {
    fn deref_mut(&mut self) -> &mut File {
        &mut self.value
    }
}
#[cfg(unix)]
impl Drop for TrackedFile {
    fn drop(&mut self) {
        // close exactly once. EINTR/other close failure is physically unknown;
        // retrying an fd number can close another thread's newly reused fd.
        if unsafe { libc::close(self.value.as_raw_fd()) } == 0 {
            self.state.files.fetch_sub(1, Ordering::AcqRel);
        } else {
            self.state.uncertain.store(true, Ordering::Release);
        }
    }
}
#[cfg(unix)]
struct RetainedConnection {
    _connection: Connection,
    _busy: Box<Busy>,
    _control: ControlReservation,
}
#[cfg(unix)]
struct TrackedConnection {
    value: Option<Connection>,
    state: Arc<CleanupState>,
    busy: Option<Box<Busy>>,
    control: Option<ControlReservation>,
}
#[cfg(unix)]
impl Deref for TrackedConnection {
    type Target = Connection;
    fn deref(&self) -> &Connection {
        self.value.as_ref().expect("live tracked SQLite")
    }
}
#[cfg(unix)]
impl TrackedConnection {
    fn close_inner(&mut self) -> std::result::Result<(), rusqlite::Error> {
        let Some(connection) = self.value.take() else {
            return Ok(());
        };
        match connection.close() {
            Ok(()) => {
                self.state.connections.fetch_sub(1, Ordering::AcqRel);
                Ok(())
            }
            Err((connection, error)) => {
                self.state.uncertain.store(true, Ordering::Release);
                let retained = RetainedConnection {
                    _connection: connection,
                    _busy: self.busy.take().expect("retained SQLite busy callback"),
                    _control: self
                        .control
                        .take()
                        .expect("retained SQLite control custody"),
                };
                self.state
                    .retained
                    .lock()
                    .unwrap_or_else(|error| error.into_inner())
                    .push(retained);
                Err(error)
            }
        }
    }
    fn close(mut self) -> std::result::Result<(), rusqlite::Error> {
        self.close_inner()
    }
}
#[cfg(unix)]
impl Drop for TrackedConnection {
    fn drop(&mut self) {
        let _ = self.close_inner();
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub(super) struct Identity {
    device: u64,
    inode: u64,
    owner: u32,
    kind: u32,
}
#[cfg(unix)]
fn identity(metadata: &fs::Metadata) -> Identity {
    Identity {
        device: metadata.dev(),
        inode: metadata.ino(),
        owner: metadata.uid(),
        kind: metadata.mode() & 0o170000,
    }
}
#[cfg(unix)]
fn regular(metadata: &fs::Metadata) -> Result<Identity> {
    if !metadata.is_file() {
        return Err(secure("file is not regular"));
    }
    if metadata.nlink() != 1 {
        return Err(secure("file does not have exactly one link"));
    }
    if metadata.uid() != unsafe { libc::getuid() } {
        return Err(secure("file is not owned by the current user"));
    }
    Ok(identity(metadata))
}
fn secure(message: &'static str) -> OriginalAuthorityError {
    OriginalAuthorityError::new(
        OriginalAuthorityStage::Path,
        "authority state path is unsafe",
        Some(OriginalAuthorityCause::SecurePath {
            message,
            context: None,
        }),
    )
}
fn io_error(
    stage: OriginalAuthorityStage,
    message: &'static str,
    error: std::io::Error,
) -> OriginalAuthorityError {
    OriginalAuthorityError::new(
        stage,
        message,
        Some(OriginalAuthorityCause::Io(OriginalAuthorityIoError {
            kind: error.kind(),
            errno: error.raw_os_error(),
            filename: None,
            context: None,
        })),
    )
}
fn state_error(
    mut error: OriginalAuthorityError,
    stage: OriginalAuthorityStage,
    message: &'static str,
) -> OriginalAuthorityError {
    error.stage = stage;
    error.message = message.into();
    error
}
fn value_text(value: &Value) -> Option<&str> {
    if let Value::Text(value) = value {
        Some(value)
    } else {
        None
    }
}
fn trim_key(value: &[u8]) -> &[u8] {
    let first = value
        .iter()
        .position(|byte| !b" \t\n\r\x0b\x0c".contains(byte))
        .unwrap_or(value.len());
    let last = value
        .iter()
        .rposition(|byte| !b" \t\n\r\x0b\x0c".contains(byte))
        .map(|n| n + 1)
        .unwrap_or(first);
    &value[first..last]
}

#[cfg(unix)]
struct Parent {
    path: PathBuf,
    fd: TrackedFile,
    identity: Identity,
}
#[cfg(unix)]
struct FileStat {
    identity: Identity,
    size: u64,
    mode: u32,
}
#[cfg(unix)]
impl FileStat {
    fn len(&self) -> u64 {
        self.size
    }
    fn mode(&self) -> u32 {
        self.mode
    }
}
#[cfg(unix)]
impl Parent {
    fn selected(path: &Path, expected: Identity, cleanup: &Arc<CleanupState>) -> Result<Self> {
        let parent = Self::open(path, cleanup)?;
        if parent.identity != expected {
            return Err(secure("authority parent identity changed"));
        }
        Ok(parent)
    }
    fn open(path: &Path, cleanup: &Arc<CleanupState>) -> Result<Self> {
        let directory = path.parent().ok_or_else(|| secure("ancestor is unsafe"))?;
        let fd = Self::walk(directory, cleanup)?;
        let observed = identity(&fd.metadata().map_err(|e| {
            io_error(
                OriginalAuthorityStage::Path,
                "authority state path is unsafe",
                e,
            )
        })?);
        Ok(Self {
            path: directory.into(),
            fd,
            identity: observed,
        })
    }
    fn walk(path: &Path, cleanup: &Arc<CleanupState>) -> Result<TrackedFile> {
        let flags = libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC;
        let descriptor = unsafe { libc::open(c"/".as_ptr(), flags) };
        if descriptor < 0 {
            let mut error = io_error(
                OriginalAuthorityStage::Path,
                "authority state path is unsafe",
                std::io::Error::last_os_error(),
            );
            if let Some(OriginalAuthorityCause::Io(source)) = error.cause.as_mut() {
                source.filename = Some("/".into());
            }
            return Err(error);
        }
        let mut current =
            TrackedFile::new(unsafe { File::from_raw_fd(descriptor) }, cleanup.clone());
        for part in path.components() {
            let Component::Normal(name) = part else {
                if matches!(part, Component::ParentDir) {
                    return Err(secure("ancestor is unsafe"));
                }
                continue;
            };
            let name = CString::new(name.as_bytes()).map_err(|_| secure("ancestor is unsafe"))?;
            let next = unsafe { libc::openat(current.as_raw_fd(), name.as_ptr(), flags) };
            if next < 0 {
                return Err(io_error(
                    OriginalAuthorityStage::Path,
                    "authority state path is unsafe",
                    std::io::Error::last_os_error(),
                ));
            }
            current = TrackedFile::new(unsafe { File::from_raw_fd(next) }, cleanup.clone());
        }
        Ok(current)
    }
    fn continuity(&self) -> Result<()> {
        let current = Self::walk(&self.path, &self.fd.state)?;
        if identity(&current.metadata().map_err(|e| {
            io_error(
                OriginalAuthorityStage::Path,
                "authority state path is unsafe",
                e,
            )
        })?) != self.identity
        {
            return Err(secure("parent directory identity changed"));
        }
        Ok(())
    }
    fn name(name: &str) -> Result<CString> {
        if name.contains('/') || name == ".." || name == "." {
            return Err(secure("file name is unsafe"));
        }
        CString::new(name).map_err(|_| secure("file name is unsafe"))
    }
    fn stat(&self, name: &str, required: bool) -> Result<Option<FileStat>> {
        // lstat through /dev/fd would follow a new ambient pathname. Use the
        // original directory's fd with AT_SYMLINK_NOFOLLOW instead.
        let name = Self::name(name)?;
        let mut stat = std::mem::MaybeUninit::<libc::stat>::uninit();
        let code = unsafe {
            libc::fstatat(
                self.fd.as_raw_fd(),
                name.as_ptr(),
                stat.as_mut_ptr(),
                libc::AT_SYMLINK_NOFOLLOW,
            )
        };
        if code != 0 {
            let error = std::io::Error::last_os_error();
            if error.kind() == std::io::ErrorKind::NotFound {
                if !required {
                    return Ok(None);
                }
                return Err(secure("required file is unavailable"));
            }
            return Err(io_error(
                OriginalAuthorityStage::Path,
                "authority state path is unsafe",
                error,
            ));
        }
        let stat = unsafe { stat.assume_init() };
        if stat.st_mode & libc::S_IFMT != libc::S_IFREG {
            return Err(secure("file is not regular"));
        }
        if stat.st_nlink != 1 {
            return Err(secure("file does not have exactly one link"));
        }
        if stat.st_uid != unsafe { libc::getuid() } {
            return Err(secure("file is not owned by the current user"));
        }
        Ok(Some(FileStat {
            identity: Identity {
                device: stat.st_dev as u64,
                inode: stat.st_ino as u64,
                owner: stat.st_uid,
                kind: u32::from(stat.st_mode & libc::S_IFMT),
            },
            size: stat.st_size as u64,
            mode: u32::from(stat.st_mode),
        }))
    }
    fn open_file_raw(&self, name: &CString, flags: i32, mode: libc::mode_t) -> Result<TrackedFile> {
        let fd = unsafe {
            libc::openat(
                self.fd.as_raw_fd(),
                name.as_ptr(),
                flags | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                u32::from(mode),
            )
        };
        if fd < 0 {
            let mut error = io_error(
                OriginalAuthorityStage::Path,
                "authority state path is unsafe",
                std::io::Error::last_os_error(),
            );
            if let Some(OriginalAuthorityCause::Io(source)) = error.cause.as_mut() {
                source.filename = name.to_str().ok().map(str::to_owned);
            }
            return Err(error);
        }
        Ok(TrackedFile::new(
            unsafe { File::from_raw_fd(fd) },
            self.fd.state.clone(),
        ))
    }
    fn open_file(&self, name: &str, flags: i32, mode: libc::mode_t) -> Result<TrackedFile> {
        self.open_file_raw(&Self::name(name)?, flags, mode)
    }
    fn validate(
        &self,
        name: &str,
        required: bool,
        expected: Option<Identity>,
    ) -> Result<Option<Identity>> {
        let Some(before) = self.stat(name, required)? else {
            return Ok(None);
        };
        let before = before.identity;
        let opened = regular(
            &self
                .open_file(name, libc::O_RDONLY, 0)?
                .metadata()
                .map_err(|e| {
                    io_error(
                        OriginalAuthorityStage::Path,
                        "authority state path is unsafe",
                        e,
                    )
                })?,
        )?;
        let after = self
            .stat(name, true)?
            .ok_or_else(|| secure("required file is unavailable"))?
            .identity;
        if before != opened || opened != after {
            return Err(secure("file identity changed while opening"));
        }
        if expected.is_some_and(|value| value != opened) {
            return Err(secure("file identity does not match the pinned file"));
        }
        Ok(Some(opened))
    }
    fn create(&self, name: &str) -> Result<Identity> {
        let file = self.open_file(name, libc::O_RDWR | libc::O_CREAT | libc::O_EXCL, 0o600)?;
        let id = regular(&file.metadata().map_err(|e| {
            io_error(
                OriginalAuthorityStage::Path,
                "authority state path is unsafe",
                e,
            )
        })?)?;
        file.sync_all().map_err(|e| {
            io_error(
                OriginalAuthorityStage::Path,
                "authority state path is unsafe",
                e,
            )
        })?;
        drop(file);
        self.continuity()?;
        self.validate(name, true, Some(id))?
            .ok_or_else(|| secure("required file is unavailable"))
    }
    fn private(&self, name: &str) -> Result<()> {
        if self
            .stat(name, true)?
            .ok_or_else(|| secure("required file is unavailable"))?
            .mode()
            & 0o077
            != 0
        {
            return Err(OriginalAuthorityError::new(
                OriginalAuthorityStage::Connect,
                "authority database permissions are too broad",
                None,
            ));
        }
        Ok(())
    }
    fn read<S: OriginalAuthorityOwner>(
        &self,
        op: &Operation<'_, S>,
        name: &str,
        expected: Option<Identity>,
    ) -> Result<(Funded<zeroize::Zeroizing<Vec<u8>>>, Identity)> {
        let before = self
            .stat(name, true)?
            .ok_or_else(|| secure("required file is unavailable"))?;
        let before_id = before.identity;
        let file = self.open_file(name, libc::O_RDONLY, 0)?;
        let opened = regular(&file.metadata().map_err(|e| {
            io_error(
                OriginalAuthorityStage::Key,
                "authority encryption key is invalid",
                e,
            )
        })?)?;
        let mut reader = file.try_clone().map_err(|e| {
            io_error(
                OriginalAuthorityStage::Key,
                "authority encryption key is invalid",
                e,
            )
        })?;
        let size = usize::try_from(before.len())
            .ok()
            .and_then(|n| n.checked_add(1))
            .ok_or_else(|| OriginalAuthorityError::capacity(OriginalAuthorityStage::Key))?;
        let mut capacity = op.reserve(OriginalAuthorityStage::Key, size)?;
        let mut bytes = zeroize::Zeroizing::new(Vec::new());
        bytes
            .try_reserve_exact(size)
            .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Key))?;
        bytes.resize(size, 0);
        let mut position = 0;
        loop {
            op.check(OriginalAuthorityStage::Key)?;
            if position == bytes.len() {
                let next = bytes
                    .len()
                    .checked_mul(2)
                    .ok_or_else(|| OriginalAuthorityError::capacity(OriginalAuthorityStage::Key))?;
                let next_capacity = op.reserve(OriginalAuthorityStage::Key, next)?;
                let additional = next - bytes.len();
                bytes
                    .try_reserve_exact(additional)
                    .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Key))?;
                bytes.resize(next, 0);
                capacity = next_capacity;
            }
            let read = reader.read(&mut bytes[position..]).map_err(|e| {
                io_error(
                    OriginalAuthorityStage::Key,
                    "authority encryption key is invalid",
                    e,
                )
            })?;
            if read == 0 {
                break;
            }
            position += read;
        }
        bytes.truncate(position);
        drop(reader);
        drop(file);
        let after = self
            .stat(name, true)?
            .ok_or_else(|| secure("required file is unavailable"))?
            .identity;
        if before_id != opened || opened != after {
            return Err(secure("file identity changed while reading"));
        }
        if expected.is_some_and(|id| id != opened) {
            return Err(secure("file identity does not match the pinned file"));
        }
        self.continuity()?;
        Ok((
            Funded {
                value: bytes,
                _capacity: capacity,
            },
            opened,
        ))
    }
}

fn canonical(path: &Path) -> Result<PathBuf> {
    let absolute = if path.is_absolute() {
        path.to_owned()
    } else {
        std::env::current_dir()
            .map_err(|e| {
                io_error(
                    OriginalAuthorityStage::Path,
                    "authority state path is unsafe",
                    e,
                )
            })?
            .join(path)
    };
    #[cfg(target_os = "macos")]
    for (alias, real) in [("/var", "/private/var"), ("/tmp", "/private/tmp")] {
        if let Ok(suffix) = absolute.strip_prefix(alias) {
            if let (Ok(meta), Ok(target)) = (fs::symlink_metadata(alias), fs::read_link(alias)) {
                if meta.file_type().is_symlink()
                    && meta.uid() == 0
                    && (target == Path::new(real)
                        || target == Path::new(real.trim_start_matches('/')))
                {
                    return Ok(Path::new(real).join(suffix));
                }
            }
        }
    }
    Ok(absolute)
}

pub(super) fn read_selected<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    root: &Path,
) -> Result<OriginalAuthorityMaterial> {
    let path = root.join("authority/v4.sqlite3");
    // Path.is_file and Path.exists are separate original operations. A dangling
    // final symlink is absent here; existing symlinks are refused by the ctor.
    if !path.is_file() {
        if path.exists() {
            return Err(OriginalAuthorityError {
                stage: OriginalAuthorityStage::Source,
                class: "core_runtime.bootstrap.profile_capture.ProfileResolutionDenied",
                message: "Authority store path is not a regular file".into(),
                cause: None,
                context: None,
                suppressed: false,
            });
        }
        return Ok(genesis());
    }
    #[cfg(unix)]
    {
        let mut store = Store::open(op, canonical(&path)?)?;
        let result = store.material();
        let close = store.close();
        // __exit__ close is later than the active read failure, and replaces
        // it if close itself raises. Preserve the displaced error as context.
        match close {
            Ok(()) => result,
            Err(error) => Err(error),
        }
    }
    #[cfg(not(unix))]
    Err(OriginalAuthorityError::unsupported(
        OriginalAuthorityStage::Process,
        "Windows AuthorityStore persistence",
    ))
}

#[cfg(unix)]
pub(super) struct Store<'a, 'host, S> {
    pub op: &'a Operation<'host, S>,
    pub path: PathBuf,
    parent: Identity,
    key_parent: Identity,
    db: Identity,
    key: Identity,
    guard: Identity,
    pub keys: crypto::Keys,
    pub closed: bool,
    _control: ControlReservation,
}
#[cfg(unix)]
impl<'a, 'host, S: OriginalAuthorityOwner> Store<'a, 'host, S> {
    fn open(op: &'a Operation<'host, S>, path: PathBuf) -> Result<Self> {
        op.check(OriginalAuthorityStage::Process)?;
        verify_process(&op.cleanup)?;
        let control = op
            .invocation
            .control()
            .reserve(ControlKind::Context, 8192)
            .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source))?;
        // The original constructor reaches parent creation twice, in db/key
        // order, even though their selected parents happen to be the same.
        let key_path = path.with_extension("key");
        let mut parent_id = None;
        let mut key_parent_id = None;
        let mut database_id = None;
        let mut key_id = None;
        for target in [&path, &key_path] {
            let directory = target
                .parent()
                .ok_or_else(|| secure("ancestor is unsafe"))?;
            let existed = directory.exists();
            fs::create_dir_all(directory).map_err(|e| {
                io_error(
                    OriginalAuthorityStage::Path,
                    "authority state path is unsafe",
                    e,
                )
            })?;
            if !existed {
                fs::set_permissions(directory, fs::Permissions::from_mode(0o700)).map_err(|e| {
                    io_error(
                        OriginalAuthorityStage::Path,
                        "authority state path is unsafe",
                        e,
                    )
                })?;
            }
            let parent = Parent::open(target, &op.cleanup)?;
            if parent.identity.owner != unsafe { libc::getuid() } {
                return Err(secure("parent is not owned by the current user"));
            }
            let name = target
                .file_name()
                .and_then(|name| name.to_str())
                .ok_or_else(|| secure("file name is unsafe"))?;
            let id = parent.validate(name, false, None)?;
            if target == &path {
                parent_id = Some(parent.identity);
                database_id = id;
            } else {
                key_parent_id = Some(parent.identity);
                key_id = id;
            }
            parent.continuity()?;
        }
        let parent_id = parent_id.ok_or_else(|| secure("ancestor is unsafe"))?;
        let key_parent_id = key_parent_id.ok_or_else(|| secure("ancestor is unsafe"))?;
        let parent = Parent::selected(&key_path, key_parent_id, &op.cleanup)?;
        let key_name = "v4.key";
        let key_result = parent.read(op, key_name, key_id);
        drop(parent);
        let (keys, key) = match key_result {
            Ok((bytes, id)) => {
                let encoded = trim_key(&bytes);
                let keys = crypto::Keys::parse(op, encoded)?;
                let parent = Parent::selected(&key_path, key_parent_id, &op.cleanup)?;
                if parent
                    .stat(key_name, true)?
                    .ok_or_else(|| secure("required file is unavailable"))?
                    .mode()
                    & 0o077
                    != 0
                {
                    return Err(OriginalAuthorityError::new(
                        OriginalAuthorityStage::Key,
                        "authority encryption key permissions are too broad",
                        None,
                    ));
                }
                (keys, id)
            }
            Err(error)
                if matches!(error.cause, Some(OriginalAuthorityCause::SecurePath { .. }))
                    || matches!(&error.cause, Some(OriginalAuthorityCause::Io(e)) if e.kind() == std::io::ErrorKind::NotFound) =>
            {
                if key_id.is_some() {
                    return Err(OriginalAuthorityError::new(
                        OriginalAuthorityStage::Key,
                        "authority encryption key path is unsafe",
                        None,
                    ));
                }
                let parent = Parent::selected(&path, parent_id, &op.cleanup)?;
                if parent
                    .stat("v4.sqlite3", true)?
                    .is_some_and(|metadata| metadata.len() > 0)
                {
                    return Err(OriginalAuthorityError::new(
                        OriginalAuthorityStage::Key,
                        "authority encryption key is missing for an existing database",
                        None,
                    ));
                }
                parent.continuity()?;
                drop(parent);
                let keys = crypto::Keys::generate(op)?;
                let mut random = [0; 8];
                crypto::random(&mut random, OriginalAuthorityStage::Key)?;
                let suffix: String = random.iter().map(|byte| format!("{byte:02x}")).collect();
                let temporary = format!(".v4.key.{suffix}.tmp");
                let parent = Parent::selected(&key_path, key_parent_id, &op.cleanup)?;
                let write = (|| {
                    let mut file = parent.open_file(
                        &temporary,
                        libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL,
                        0o600,
                    )?;
                    file.write_all(&keys.encoded)
                        .and_then(|()| file.write_all(b"\n"))
                        .and_then(|()| file.sync_all())
                        .map_err(|e| {
                            io_error(
                                OriginalAuthorityStage::Key,
                                "authority encryption key is invalid",
                                e,
                            )
                        })?;
                    drop(file);
                    parent.continuity()?;
                    let from = Parent::name(&temporary)?;
                    let to = Parent::name(key_name)?;
                    if unsafe {
                        libc::linkat(
                            parent.fd.as_raw_fd(),
                            from.as_ptr(),
                            parent.fd.as_raw_fd(),
                            to.as_ptr(),
                            0,
                        )
                    } != 0
                    {
                        let e = std::io::Error::last_os_error();
                        if e.kind() != std::io::ErrorKind::AlreadyExists {
                            return Err(io_error(
                                OriginalAuthorityStage::Key,
                                "authority encryption key is invalid",
                                e,
                            ));
                        }
                        let (bytes, id) = parent.read(op, key_name, None)?;
                        return Ok((crypto::Keys::parse(op, trim_key(&bytes))?, id));
                    }
                    unsafe {
                        libc::unlinkat(parent.fd.as_raw_fd(), from.as_ptr(), 0);
                    }
                    parent.fd.sync_all().map_err(|e| {
                        io_error(
                            OriginalAuthorityStage::Key,
                            "authority encryption key is invalid",
                            e,
                        )
                    })?;
                    let (bytes, id) = parent.read(op, key_name, None)?;
                    if trim_key(&bytes) != keys.encoded.as_slice() {
                        return Err(OriginalAuthorityError::new(
                            OriginalAuthorityStage::Key,
                            "authority encryption key changed during creation",
                            None,
                        ));
                    }
                    Ok((keys, id))
                })();
                let temp = Parent::name(&temporary)?;
                unsafe {
                    libc::unlinkat(parent.fd.as_raw_fd(), temp.as_ptr(), 0);
                }
                write?
            }
            Err(error) => {
                return Err(state_error(
                    error,
                    OriginalAuthorityStage::Key,
                    "authority encryption key is invalid",
                ));
            }
        };
        let parent = Parent::selected(&path, parent_id, &op.cleanup)?;
        let db = match database_id {
            Some(id) => id,
            None => parent.create("v4.sqlite3")?,
        };
        parent.continuity()?;
        drop(parent);
        let guard_result: Result<Identity> = (|| {
            let parent = Parent::selected(&path, parent_id, &op.cleanup)?;
            let guard_name = ".v4.sqlite3.lifecycle.lock";
            let guard = match parent.create(guard_name) {
                Ok(id) => id,
                Err(error) if matches!(&error.cause, Some(OriginalAuthorityCause::Io(e)) if e.kind() == std::io::ErrorKind::AlreadyExists) =>
                {
                    let previous = error.cause.expect("actual FileExistsError");
                    parent
                        .validate(guard_name, true, None)
                        .map_err(|mut error| {
                            if let Some(OriginalAuthorityCause::SecurePath { context, .. }) =
                                error.cause.as_mut()
                            {
                                *context = Some(Box::new(previous));
                            }
                            error
                        })?
                        .ok_or_else(|| secure("required file is unavailable"))?
                }
                Err(error) => {
                    return Err(state_error(
                        error,
                        OriginalAuthorityStage::Guard,
                        "authority lifecycle guard path is unsafe",
                    ));
                }
            };
            parent.private(guard_name)?;
            let mut guard_file = parent.open_file(guard_name, libc::O_RDWR, 0)?;
            if regular(&guard_file.metadata().map_err(|e| {
                io_error(
                    OriginalAuthorityStage::Guard,
                    "authority lifecycle guard path is unsafe",
                    e,
                )
            })?)?
                != guard
            {
                return Err(secure("lifecycle guard identity changed"));
            }
            if guard_file
                .metadata()
                .map_err(|e| {
                    io_error(
                        OriginalAuthorityStage::Guard,
                        "authority lifecycle guard path is unsafe",
                        e,
                    )
                })?
                .len()
                == 0
            {
                guard_file
                    .write_all(b"\0")
                    .and_then(|()| guard_file.sync_all())
                    .map_err(|e| {
                        io_error(
                            OriginalAuthorityStage::Guard,
                            "authority lifecycle guard path is unsafe",
                            e,
                        )
                    })?;
            }
            drop(guard_file);
            parent.validate(guard_name, true, Some(guard))?;
            parent.continuity()?;
            Ok(guard)
        })();
        let guard = guard_result.map_err(|error| {
            if matches!(
                error.cause,
                Some(OriginalAuthorityCause::Io(_) | OriginalAuthorityCause::SecurePath { .. })
            ) {
                state_error(
                    error,
                    OriginalAuthorityStage::Guard,
                    "authority lifecycle guard path is unsafe",
                )
            } else {
                error
            }
        })?;
        let mut store = Self {
            op,
            path,
            parent: parent_id,
            key_parent: key_parent_id,
            db,
            key,
            guard,
            keys,
            closed: false,
            _control: control,
        };
        schema::initialize(&mut store)?;
        let parent = store.parent()?;
        parent.validate("v4.sqlite3", true, Some(db))?;
        let file = parent.open_file("v4.sqlite3", libc::O_RDONLY, 0)?;
        if identity(&file.metadata().map_err(|e| {
            io_error(
                OriginalAuthorityStage::Path,
                "authority state path is unsafe",
                e,
            )
        })?) != db
        {
            return Err(secure("file changed before chmod"));
        }
        file.set_permissions(fs::Permissions::from_mode(0o600))
            .map_err(|e| {
                io_error(
                    OriginalAuthorityStage::Path,
                    "authority state path is unsafe",
                    e,
                )
            })?;
        drop(file);
        parent.validate("v4.sqlite3", true, Some(db))?;
        parent.continuity()?;
        Ok(store)
    }
    fn parent(&self) -> Result<Parent> {
        let parent = Parent::open(&self.path, &self.op.cleanup)?;
        if parent.identity != self.parent {
            return Err(secure("authority parent identity changed"));
        }
        Ok(parent)
    }
    pub fn assert_crypto(&self) -> Result<()> {
        self.op.check(OriginalAuthorityStage::Key)?;
        let parent = Parent::selected(
            &self.path.with_extension("key"),
            self.key_parent,
            &self.op.cleanup,
        )?;
        parent
            .validate("v4.key", true, Some(self.key))
            .map_err(|e| {
                state_error(
                    e,
                    OriginalAuthorityStage::Key,
                    "authority encryption key path is unsafe",
                )
            })?;
        parent.continuity()?;
        if self.keys.encoded.is_empty() || self.keys.mac.is_empty() {
            return Err(OriginalAuthorityError::new(
                OriginalAuthorityStage::Key,
                "authority cryptographic material is unavailable",
                None,
            ));
        }
        Ok(())
    }
    fn storage(&self) -> Result<()> {
        let parent = self.parent()?;
        parent.validate("v4.sqlite3", true, Some(self.db))?;
        parent.validate("v4.sqlite3-wal", false, None)?;
        parent.validate("v4.sqlite3-shm", false, None)?;
        parent.continuity()
    }
    pub fn identity(&self) -> Identity {
        self.db
    }
    pub fn clock(&self) -> f64 {
        #[cfg(test)]
        if let Some(fixture) = self.op.fixture {
            return fixture.clock;
        }
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|time| time.as_secs_f64())
            .unwrap_or(0.0)
    }
    pub fn connection(&self, stage: OriginalAuthorityStage) -> Result<Database<'_, 'a, 'host, S>> {
        self.op.check(stage)?;
        if self.closed {
            return Err(OriginalAuthorityError::new(
                stage,
                "authority store is closed",
                None,
            ));
        }
        let control = self
            .op
            .invocation
            .control()
            .reserve(ControlKind::Context, 2 * 1024 * 1024 + 128 * 1024)
            .map_err(|_| OriginalAuthorityError::capacity(stage))?;
        let parent = self.parent()?;
        let guard = acquire_guard(self, &parent)?;
        self.storage()?;
        let before = descriptors(&self.op.cleanup, self.op.invocation.control())?;
        let busy = Box::new(Busy {
            cancellation: self.op.cancellation.clone(),
            deadline: self.op.deadline,
        });
        let raw_connection = Connection::open_with_flags(
            &self.path,
            OpenFlags::SQLITE_OPEN_READ_WRITE
                | OpenFlags::SQLITE_OPEN_CREATE
                | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )
        .map_err(|e| OriginalAuthorityError::sql(stage, "authority database is unavailable", e))?;
        self.op.cleanup.connections.fetch_add(1, Ordering::AcqRel);
        let connection = TrackedConnection {
            value: Some(raw_connection),
            state: self.op.cleanup.clone(),
            busy: Some(busy),
            control: Some(control),
        };
        // SQLite's stock 30-second busy sleep can outlive this invocation's
        // already-running absolute deadline. This trusted native busy handler
        // has no guest configuration and owns no SQL or source capability.
        let code = unsafe {
            rusqlite::ffi::sqlite3_busy_handler(
                connection.handle(),
                Some(busy_wait),
                std::ptr::from_ref(&**connection.busy.as_ref().expect("live busy callback"))
                    .cast_mut()
                    .cast(),
            )
        };
        if code != rusqlite::ffi::SQLITE_OK {
            return Err(OriginalAuthorityError::unsupported(
                stage,
                "SQLite busy handler installation",
            ));
        }
        unsafe {
            rusqlite::ffi::sqlite3_limit(
                connection.handle(),
                rusqlite::ffi::SQLITE_LIMIT_LENGTH,
                64 * 1024 * 1024,
            );
            rusqlite::ffi::sqlite3_limit(
                connection.handle(),
                rusqlite::ffi::SQLITE_LIMIT_SQL_LENGTH,
                1024 * 1024,
            );
        }
        let cancel = self.op.cancellation.clone();
        let deadline = self.op.deadline;
        connection
            .progress_handler(
                1000,
                Some(move || cancel.is_cancelled() || Instant::now() >= deadline),
            )
            .map_err(|e| {
                OriginalAuthorityError::sql(stage, "authority database is unavailable", e)
            })?;
        let mut db = Database {
            store: self,
            connection: Some(connection),
            pinned: Vec::new(),
            guard: Some(guard),
            stage,
        };
        db.pin(&before, &[""])?;
        for sql in [
            "PRAGMA foreign_keys=ON",
            "PRAGMA journal_mode=WAL",
            "PRAGMA synchronous=FULL",
            "PRAGMA trusted_schema=OFF",
            "SELECT count(*) FROM sqlite_schema",
        ] {
            db.raw(sql)?;
        }
        db.pin(&BTreeMap::new(), &["-wal", "-shm"])?;
        self.storage()?;
        db.validate()?;
        // Existing native Host control safety setting, not an extra guest
        // budget or a reinterpretation of disk bytes as trusted metadata.
        db.raw("PRAGMA cache_size=-2048")?;
        db.validate()?; // IdentityBoundConnection.__enter__
        Ok(db)
    }
    fn material(&mut self) -> Result<OriginalAuthorityMaterial> {
        let db = self.connection(OriginalAuthorityStage::Epoch)?;
        let rows = db.query(
            "SELECT value FROM authority_meta WHERE key='security_epoch'",
            [],
        );
        let rows = db.complete(rows)?;
        let value = rows
            .rows
            .first()
            .and_then(|row| row.first())
            .and_then(value_text)
            .ok_or_else(|| {
                OriginalAuthorityError::new(
                    OriginalAuthorityStage::Epoch,
                    "security epoch is unavailable",
                    None,
                )
            })?;
        let epoch = integer(value, OriginalAuthorityStage::Epoch)?;
        drop(rows);
        #[cfg(test)]
        if let Some(fixture) = self.op.fixture {
            fixture.after_epoch(&self.path)?;
        }
        let db = self.connection(OriginalAuthorityStage::EpochRecord)?;
        let rows = db.query("SELECT key, value FROM authority_meta WHERE key IN ('security_epoch', 'security_epoch_advanced_at', 'security_epoch_reason_digest')", []);
        let rows = db.complete(rows)?;
        let get = |key: &str| {
            rows.rows
                .iter()
                .find(|row| row.first().and_then(value_text) == Some(key))
                .and_then(|row| row.get(1))
                .and_then(value_text)
                .ok_or_else(|| {
                    OriginalAuthorityError::new(
                        OriginalAuthorityStage::EpochRecord,
                        "security epoch metadata is invalid",
                        Some(python_error("builtins.KeyError", python_repr(key))),
                    )
                })
        };
        let record_epoch = integer(get("security_epoch")?, OriginalAuthorityStage::EpochRecord)?;
        let advanced = get("security_epoch_advanced_at")?;
        let _advanced_copy = self.op.reserve(
            OriginalAuthorityStage::EpochRecord,
            advanced.len().checked_mul(2).ok_or_else(|| {
                OriginalAuthorityError::capacity(OriginalAuthorityStage::EpochRecord)
            })?,
        )?;
        let advanced_at: f64 = python_float(advanced).map_err(|_| {
            OriginalAuthorityError::new(
                OriginalAuthorityStage::EpochRecord,
                "security epoch metadata is invalid",
                Some(python_error(
                    "builtins.ValueError",
                    format!(
                        "could not convert string to float: {}",
                        python_repr(advanced)
                    ),
                )),
            )
        })?;
        let reason = get("security_epoch_reason_digest")?;
        if record_epoch.0 == "0" || record_epoch.0.starts_with('-') {
            return Err(OriginalAuthorityError::new(
                OriginalAuthorityStage::EpochRecord,
                "security epoch metadata is invalid",
                Some(OriginalAuthorityCause::Validation(
                    "SecurityEpoch must be a positive integer",
                )),
            ));
        }
        if !advanced_at.is_finite() || advanced_at < 0.0 {
            return Err(OriginalAuthorityError::new(
                OriginalAuthorityStage::EpochRecord,
                "security epoch metadata is invalid",
                Some(OriginalAuthorityCause::Validation(
                    "advanced_at must be a finite timestamp",
                )),
            ));
        }
        if !schema::valid_digest(reason) {
            return Err(OriginalAuthorityError::new(
                OriginalAuthorityStage::EpochRecord,
                "security epoch metadata is invalid",
                Some(OriginalAuthorityCause::Validation(
                    "reason_digest must be an exact SHA-256 digest",
                )),
            ));
        }
        Ok(OriginalAuthorityMaterial::Existing {
            epoch,
            record_epoch,
            advanced_at,
            reason_digest: reason.into(),
        })
    }
    fn close(&mut self) -> Result<()> {
        // Source close is process-owned, not a new cancellation boundary.
        if std::process::id() != self.op.pid {
            return Err(OriginalAuthorityError::new(
                OriginalAuthorityStage::Close,
                "authority store cannot be used after fork",
                None,
            ));
        }
        self.closed = true;
        Ok(())
    }
}

#[cfg(unix)]
struct Pinned {
    suffix: &'static str,
    identity: Identity,
    descriptors: Vec<i32>,
}
#[cfg(unix)]
pub(super) struct Database<'s, 'a, 'host, S> {
    store: &'s Store<'a, 'host, S>,
    connection: Option<TrackedConnection>,
    pinned: Vec<Pinned>,
    guard: Option<Guard>,
    stage: OriginalAuthorityStage,
}
#[cfg(unix)]
struct Busy {
    cancellation: CoreCancellation,
    deadline: Instant,
}
#[cfg(unix)]
unsafe extern "C" fn busy_wait(context: *mut std::ffi::c_void, _prior: libc::c_int) -> libc::c_int {
    let busy = unsafe { &*context.cast::<Busy>() };
    if busy.cancellation.is_cancelled() || Instant::now() >= busy.deadline {
        return 0;
    }
    std::thread::park_timeout(
        Duration::from_millis(10).min(busy.deadline.saturating_duration_since(Instant::now())),
    );
    i32::from(!busy.cancellation.is_cancelled() && Instant::now() < busy.deadline)
}
pub(super) struct Rows {
    pub rows: Vec<Vec<Value>>,
    _capacity: Vec<NativeSetupReservation>,
}
#[cfg(unix)]
impl<S: OriginalAuthorityOwner> Database<'_, '_, '_, S> {
    fn connection(&self) -> &Connection {
        self.connection.as_ref().expect("live original connection")
    }
    fn raw(&self, sql: &str) -> Result<()> {
        let mut statement = self.connection().prepare(sql).map_err(|e| self.sql(e))?;
        let mut rows = statement.query([]).map_err(|e| self.sql(e))?;
        while rows.next().map_err(|e| self.sql(e))?.is_some() {}
        Ok(())
    }
    fn sql(&self, error: rusqlite::Error) -> OriginalAuthorityError {
        if !matches!(
            self.stage,
            OriginalAuthorityStage::Epoch | OriginalAuthorityStage::EpochRecord
        ) {
            return OriginalAuthorityError::raw_sql(self.stage, error);
        }
        OriginalAuthorityError::sql(
            self.stage,
            match self.stage {
                OriginalAuthorityStage::Epoch => "security epoch read failed",
                OriginalAuthorityStage::EpochRecord => "security epoch metadata read failed",
                _ => "authority database is unavailable",
            },
            error,
        )
    }
    fn pin(&mut self, before: &BTreeMap<i32, Identity>, suffixes: &[&'static str]) -> Result<()> {
        let actual: String = self
            .connection()
            .query_row("PRAGMA database_list", [], |row| row.get(2))
            .map_err(|e| self.sql(e))?;
        if canonical(Path::new(&actual))? != self.store.path {
            return Err(OriginalAuthorityError::new(
                self.stage,
                "authority database handle identity is unsafe",
                None,
            ));
        }
        let observed = descriptors(&self.store.op.cleanup, self.store.op.invocation.control())?;
        let parent = self.store.parent()?;
        for suffix in suffixes {
            let name = format!("v4.sqlite3{suffix}");
            let id = parent
                .validate(
                    &name,
                    true,
                    if suffix.is_empty() {
                        Some(self.store.db)
                    } else {
                        None
                    },
                )?
                .ok_or_else(|| secure("required file is unavailable"))?;
            parent.private(&name)?;
            let all: Vec<i32> = observed
                .iter()
                .filter(|(_, value)| **value == id)
                .map(|(fd, _)| *fd)
                .collect();
            let new: Vec<i32> = all
                .iter()
                .copied()
                .filter(|fd| before.get(fd) != Some(&id))
                .collect();
            let descriptors = if new.is_empty() { all } else { new };
            if descriptors.is_empty() {
                return Err(secure(
                    "SQLite handle does not match the pinned storage file",
                ));
            }
            self.pinned.push(Pinned {
                suffix,
                identity: id,
                descriptors,
            });
        }
        parent.continuity()
    }
    pub fn validate(&self) -> Result<()> {
        self.store.op.check(self.stage)?;
        self.validate_handles()
    }
    fn validate_handles(&self) -> Result<()> {
        let parent = self.store.parent()?;
        for pinned in &self.pinned {
            let name = format!("v4.sqlite3{}", pinned.suffix);
            parent.validate(&name, true, Some(pinned.identity))?;
            parent.private(&name)?;
            if !pinned
                .descriptors
                .iter()
                .any(|fd| descriptor_identity(*fd) == Some(pinned.identity))
            {
                return Err(secure("SQLite descriptor identity changed"));
            }
        }
        parent.continuity()
    }
    pub fn execute<P: Params>(&self, sql: &str, parameters: P) -> Result<usize> {
        self.validate()?;
        self.connection()
            .execute(sql, parameters)
            .map_err(|e| self.sql(e))
    }
    pub fn script(&self, sql: &str) -> Result<()> {
        self.validate()?;
        self.connection()
            .execute_batch(sql)
            .map_err(|e| self.sql(e))
    }
    pub fn commit(&self) -> Result<()> {
        self.validate()?;
        if !self.connection().is_autocommit() {
            self.connection()
                .execute_batch("COMMIT")
                .map_err(|e| self.sql(e))?;
        }
        Ok(())
    }
    pub fn query<P: Params>(&self, sql: &str, parameters: P) -> Result<Rows> {
        self.validate()?;
        let mut statement = self.connection().prepare(sql).map_err(|e| self.sql(e))?;
        let columns = statement.column_count();
        let mut cursor = statement.query(parameters).map_err(|e| self.sql(e))?;
        let mut output = Rows {
            rows: Vec::new(),
            _capacity: Vec::new(),
        };
        while let Some(row) = cursor.next().map_err(|e| self.sql(e))? {
            self.store.op.check(self.stage)?;
            let mut bytes = columns
                .checked_mul(std::mem::size_of::<Value>())
                .ok_or_else(|| OriginalAuthorityError::capacity(self.stage))?;
            for column in 0..columns {
                bytes = bytes
                    .checked_add(match row.get_ref(column).map_err(|e| self.sql(e))? {
                        ValueRef::Text(v) | ValueRef::Blob(v) => v.len(),
                        _ => 0,
                    })
                    .ok_or_else(|| OriginalAuthorityError::capacity(self.stage))?;
            }
            let permit = self.store.op.reserve(self.stage, bytes)?;
            let mut owned = Vec::with_capacity(columns);
            for column in 0..columns {
                owned.push(match row.get_ref(column).map_err(|e| self.sql(e))? {
                    ValueRef::Null => Value::Null,
                    ValueRef::Integer(v) => Value::Integer(v),
                    ValueRef::Real(v) => Value::Real(v),
                    ValueRef::Text(v) => Value::Text(
                        std::str::from_utf8(v)
                            .map_err(|_| {
                                OriginalAuthorityError::unsupported(
                                    self.stage,
                                    "SQLite text decoding exception",
                                )
                            })?
                            .into(),
                    ),
                    ValueRef::Blob(v) => Value::Blob(v.into()),
                });
            }
            // Admit the new vector storage BEFORE reallocating; the previous
            // record remains prepaid through the actual fetchall lifetime.
            if output.rows.len() == output.rows.capacity() {
                let add = 16 * std::mem::size_of::<Vec<Value>>();
                output
                    ._capacity
                    .push(self.store.op.reserve(self.stage, add)?);
                output
                    .rows
                    .try_reserve_exact(16)
                    .map_err(|_| OriginalAuthorityError::capacity(self.stage))?;
            }
            output.rows.push(owned);
            output._capacity.push(permit);
        }
        Ok(output)
    }
    pub fn complete<T>(self, outcome: Result<T>) -> Result<T> {
        let finish = self.finish(outcome.is_ok());
        match finish {
            Ok(()) => outcome,
            Err(mut error) => {
                if let Err(previous) = outcome {
                    error.context = Some(Box::new(previous));
                }
                Err(error)
            }
        }
    }
    fn finish(mut self, success: bool) -> Result<()> {
        let transaction = if success {
            self.commit()
        } else if !self.connection().is_autocommit() {
            self.connection()
                .execute_batch("ROLLBACK")
                .map_err(|e| self.sql(e))
        } else {
            Ok(())
        };
        let validation = self.validate_handles();
        let connection = self.connection.take().expect("live original connection");
        let closed = connection.close().map_err(|e| self.sql(e));
        let storage = self.store.storage();
        drop(self.guard.take());
        for closed in [storage, closed, validation] {
            closed.map_err(|mut error| {
                error.stage = OriginalAuthorityStage::Close;
                error
            })?;
        }
        transaction
    }
}
#[cfg(unix)]
impl<S> Drop for Database<'_, '_, '_, S> {
    fn drop(&mut self) {
        drop(self.connection.take());
        drop(self.guard.take());
    }
}

#[cfg(unix)]
fn descriptor_identity(fd: i32) -> Option<Identity> {
    let mut stat = std::mem::MaybeUninit::<libc::stat>::uninit();
    if unsafe { libc::fstat(fd, stat.as_mut_ptr()) } != 0 {
        return None;
    }
    let stat = unsafe { stat.assume_init() };
    if stat.st_mode & libc::S_IFMT != libc::S_IFREG {
        return None;
    }
    Some(Identity {
        device: stat.st_dev as u64,
        inode: stat.st_ino as u64,
        owner: stat.st_uid,
        kind: u32::from(stat.st_mode & libc::S_IFMT),
    })
}
#[cfg(unix)]
fn descriptors(cleanup: &Arc<CleanupState>, control: &CoreControl) -> Result<DescriptorSnapshot> {
    let root = if Path::new("/proc/self/fd").is_dir() {
        "/proc/self/fd"
    } else {
        "/dev/fd"
    };
    if !Path::new(root).is_dir() {
        return Err(OriginalAuthorityError::new(
            OriginalAuthorityStage::Connect,
            "authority database handle identity is unavailable",
            None,
        ));
    }
    let mut permit = control
        .reserve(ControlKind::Frame, 1024)
        .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Connect))?;
    let root = CString::new(root).map_err(|_| secure("descriptor root is unsafe"))?;
    let pointer = unsafe { libc::opendir(root.as_ptr()) };
    let pointer = std::ptr::NonNull::new(pointer).ok_or_else(|| {
        io_error(
            OriginalAuthorityStage::Connect,
            "authority database handle identity is unavailable",
            std::io::Error::last_os_error(),
        )
    })?;
    cleanup.files.fetch_add(1, Ordering::AcqRel);
    let directory = TrackedDirectory {
        pointer,
        state: cleanup.clone(),
    };
    let mut names = Vec::new();
    loop {
        set_errno_zero();
        let entry = unsafe { libc::readdir(directory.pointer.as_ptr()) };
        if entry.is_null() {
            let error = std::io::Error::last_os_error();
            if error.raw_os_error() != Some(0) {
                return Err(io_error(
                    OriginalAuthorityStage::Connect,
                    "authority database handle identity is unavailable",
                    error,
                ));
            }
            break;
        }
        let name = unsafe { std::ffi::CStr::from_ptr((*entry).d_name.as_ptr()) };
        if let Some(fd) = name
            .to_str()
            .ok()
            .and_then(|value| value.parse::<i32>().ok())
        {
            permit
                .try_grow(256)
                .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Connect))?;
            names
                .try_reserve_exact(1)
                .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Connect))?;
            names.push(fd);
        }
    }
    // Source os.listdir closes its DIR before beginning the fstat loop.
    drop(directory);
    let mut output = BTreeMap::new();
    for fd in names {
        if let Some(id) = descriptor_identity(fd) {
            output.insert(fd, id);
        }
    }
    Ok(DescriptorSnapshot {
        map: output,
        _control: permit,
    })
}
#[cfg(unix)]
struct DescriptorSnapshot {
    map: BTreeMap<i32, Identity>,
    _control: ControlReservation,
}
#[cfg(unix)]
impl Deref for DescriptorSnapshot {
    type Target = BTreeMap<i32, Identity>;
    fn deref(&self) -> &Self::Target {
        &self.map
    }
}
#[cfg(unix)]
struct TrackedDirectory {
    pointer: std::ptr::NonNull<libc::DIR>,
    state: Arc<CleanupState>,
}
#[cfg(unix)]
impl Drop for TrackedDirectory {
    fn drop(&mut self) {
        if unsafe { libc::closedir(self.pointer.as_ptr()) } == 0 {
            self.state.files.fetch_sub(1, Ordering::AcqRel);
        } else {
            self.state.uncertain.store(true, Ordering::Release);
        }
    }
}
#[cfg(target_os = "macos")]
fn set_errno_zero() {
    unsafe {
        *libc::__error() = 0;
    }
}
#[cfg(target_os = "linux")]
fn set_errno_zero() {
    unsafe {
        *libc::__errno_location() = 0;
    }
}
#[cfg(not(any(target_os = "macos", target_os = "linux")))]
fn set_errno_zero() {}

#[derive(Default)]
struct ThreadLock {
    held: Mutex<Option<std::thread::ThreadId>>,
    changed: Condvar,
}
struct Guard {
    file: Option<TrackedFile>,
    local: Arc<ThreadLock>,
    locked: bool,
    _control: ControlReservation,
}
impl Drop for Guard {
    fn drop(&mut self) {
        #[cfg(unix)]
        if let Some(file) = self.file.take() {
            if self.locked && unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_UN) } != 0 {
                file.state.uncertain.store(true, Ordering::Release);
            }
            drop(file);
        }
        if let Ok(mut held) = self.local.held.lock() {
            *held = None;
            self.local.changed.notify_all();
        }
    }
}
#[cfg(unix)]
fn acquire_guard<S: OriginalAuthorityOwner>(
    store: &Store<'_, '_, S>,
    parent: &Parent,
) -> Result<Guard> {
    static LOCKS: OnceLock<Mutex<BTreeMap<Identity, std::sync::Weak<ThreadLock>>>> =
        OnceLock::new();
    let deadline = (Instant::now() + Duration::from_secs(15)).min(store.op.deadline);
    let file = parent.open_file(".v4.sqlite3.lifecycle.lock", libc::O_RDWR, 0)?;
    parent.validate(".v4.sqlite3.lifecycle.lock", true, Some(store.guard))?;
    if regular(&file.metadata().map_err(|e| {
        io_error(
            OriginalAuthorityStage::Guard,
            "authority database handle identity is unsafe",
            e,
        )
    })?)?
        != store.guard
    {
        return Err(secure("lifecycle guard identity changed"));
    }
    let permit = store
        .op
        .invocation
        .control()
        .reserve(ControlKind::Waiter, 512)
        .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Guard))?;
    let local = {
        let mut locks = LOCKS
            .get_or_init(Mutex::default)
            .lock()
            .map_err(|_| OriginalAuthorityError::unavailable())?;
        locks.retain(|_, value| value.strong_count() > 0);
        match locks.get(&store.guard).and_then(std::sync::Weak::upgrade) {
            Some(lock) => lock,
            None => {
                let lock = Arc::new(ThreadLock::default());
                locks.insert(store.guard, Arc::downgrade(&lock));
                lock
            }
        }
    };
    let mut held = local
        .held
        .lock()
        .map_err(|_| OriginalAuthorityError::unavailable())?;
    while held.is_some() {
        store.op.check(OriginalAuthorityStage::Guard)?;
        if Instant::now() >= deadline {
            return Err(OriginalAuthorityError::new(
                OriginalAuthorityStage::Guard,
                "authority database lifecycle lock deadline exceeded",
                None,
            ));
        }
        held = local
            .changed
            .wait_timeout(held, Duration::from_millis(10))
            .map_err(|_| OriginalAuthorityError::unavailable())?
            .0;
    }
    *held = Some(std::thread::current().id());
    drop(held);
    let mut guard = Guard {
        file: Some(file),
        local,
        locked: false,
        _control: permit,
    };
    loop {
        store.op.check(OriginalAuthorityStage::Guard)?;
        if unsafe {
            libc::flock(
                guard.file.as_ref().expect("live guard").as_raw_fd(),
                libc::LOCK_EX | libc::LOCK_NB,
            )
        } == 0
        {
            guard.locked = true;
            break;
        }
        if Instant::now() >= deadline {
            return Err(OriginalAuthorityError::new(
                OriginalAuthorityStage::Guard,
                "authority database lifecycle lock deadline exceeded",
                None,
            ));
        }
        std::thread::park_timeout(
            Duration::from_millis(10).min(deadline.saturating_duration_since(Instant::now())),
        );
    }
    parent.validate(".v4.sqlite3.lifecycle.lock", true, Some(store.guard))?;
    parent.continuity()?;
    Ok(guard)
}

#[cfg(unix)]
fn verify_process(_cleanup: &Arc<CleanupState>) -> Result<()> {
    #[cfg(target_os = "linux")]
    {
        let value =
            fs::read_to_string(format!("/proc/{}/stat", std::process::id())).map_err(|_| {
                OriginalAuthorityError::new(
                    OriginalAuthorityStage::Process,
                    "authority process identity is unavailable",
                    None,
                )
            })?;
        if value
            .rsplit_once(')')
            .and_then(|(_, tail)| tail.split_whitespace().nth(19))
            .is_none()
        {
            return Err(OriginalAuthorityError::new(
                OriginalAuthorityStage::Process,
                "authority process identity is unavailable",
                None,
            ));
        }
    }
    #[cfg(target_os = "macos")]
    {
        #[repr(C)]
        struct BsdInfo {
            words: [u32; 12],
            comm: [u8; 16],
            name: [u8; 32],
            tail: [u32; 6],
            seconds: u64,
            microseconds: u64,
        }
        #[link(name = "proc")]
        unsafe extern "C" {
            fn proc_pidinfo(
                pid: libc::c_int,
                flavor: libc::c_int,
                arg: u64,
                buffer: *mut libc::c_void,
                size: libc::c_int,
            ) -> libc::c_int;
        }
        let mut info = std::mem::MaybeUninit::<BsdInfo>::uninit();
        let expected = std::mem::size_of::<BsdInfo>() as libc::c_int;
        let count = unsafe {
            proc_pidinfo(
                std::process::id() as i32,
                3,
                0,
                info.as_mut_ptr().cast(),
                expected,
            )
        };
        if count != expected {
            return Err(OriginalAuthorityError::new(
                OriginalAuthorityStage::Process,
                "authority process identity is unavailable",
                None,
            ));
        }
        let info = unsafe { info.assume_init() };
        if info.words[3] != std::process::id()
            || info.seconds == 0
            || info.microseconds >= 1_000_000
        {
            return Err(OriginalAuthorityError::new(
                OriginalAuthorityStage::Process,
                "authority process identity is unavailable",
                None,
            ));
        }
    }
    #[cfg(not(any(target_os = "linux", target_os = "macos")))]
    return Err(OriginalAuthorityError::unsupported(
        OriginalAuthorityStage::Process,
        "POSIX process-start identity",
    ));
    Ok(())
}
