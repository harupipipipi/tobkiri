//! The original publication SecureDirectory/lock/read closure.
use super::*;

#[cfg(unix)]
pub(super) fn read_stream<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    file: &mut TrackedFile,
    chunk: usize,
) -> Result<Funded<Zeroizing<Vec<u8>>>> {
    let _scratch_capacity = op.reserve(OriginalAuthorityStage::Source, chunk)?;
    let mut scratch = Zeroizing::new(Vec::new());
    scratch
        .try_reserve_exact(chunk)
        .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source))?;
    if scratch.capacity() != chunk {
        return Err(OriginalAuthorityError::capacity(
            OriginalAuthorityStage::Source,
        ));
    }
    scratch.resize(chunk, 0);
    let capacity = op.reserve(OriginalAuthorityStage::Source, 1)?;
    let mut bytes = Zeroizing::new(Vec::new());
    loop {
        op.check(OriginalAuthorityStage::Source)?;
        let old = bytes.len();
        let length = old
            .checked_add(chunk)
            .ok_or_else(|| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source))?;
        // Prepay the largest possible next result BEFORE IO. Read into the
        // separately prepaid scratch, so EOF cannot cause a needless key copy.
        capacity
            .resize((bytes.capacity() + length) as u64)
            .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source))?;
        let count = file
            .read(&mut scratch)
            .map_err(|error| raw_io(error, None))?;
        if count == 0 {
            capacity
                .resize(bytes.capacity().max(1) as u64)
                .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source))?;
            break;
        }
        let needed = old + count;
        if needed > bytes.capacity() {
            let mut next = Zeroizing::new(Vec::new());
            next.try_reserve_exact(needed)
                .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source))?;
            if next.capacity() != needed {
                return Err(OriginalAuthorityError::capacity(
                    OriginalAuthorityStage::Source,
                ));
            }
            next.extend_from_slice(&bytes);
            next.extend_from_slice(&scratch[..count]);
            // Zeroize the old owned allocation BEFORE freeing it; Vec::reserve
            // would otherwise leave the previous key copy in a freed buffer.
            drop(std::mem::replace(&mut bytes, next));
        } else {
            bytes.extend_from_slice(&scratch[..count]);
        }
        scratch[..count].fill(0);
        capacity
            .resize(bytes.capacity().max(1) as u64)
            .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source))?;
    }
    Ok(Funded {
        value: bytes,
        _capacity: capacity,
    })
}

#[cfg(unix)]
#[derive(Clone, Copy, PartialEq, Eq)]
struct FileIdentity {
    dev: u64,
    ino: u64,
}
#[cfg(unix)]
fn file_identity(value: &fs::Metadata) -> FileIdentity {
    FileIdentity {
        dev: value.dev(),
        ino: value.ino(),
    }
}
#[cfg(unix)]
type Fingerprint = (FileIdentity, u32, u64, u32, u64);
#[cfg(unix)]
type ReadFingerprint = (Fingerprint, i64, i64, i64, i64);
#[cfg(unix)]
fn fingerprint(value: &fs::Metadata) -> Fingerprint {
    (
        file_identity(value),
        value.mode(),
        value.nlink(),
        value.uid(),
        value.len(),
    )
}
#[cfg(unix)]
fn read_fingerprint(value: &fs::Metadata) -> ReadFingerprint {
    (
        fingerprint(value),
        value.mtime(),
        value.mtime_nsec(),
        value.ctime(),
        value.ctime_nsec(),
    )
}
#[cfg(unix)]
struct Stat {
    fingerprint: Fingerprint,
    mtime: i64,
    mtime_nsec: i64,
    ctime: i64,
    ctime_nsec: i64,
}
#[cfg(unix)]
impl Stat {
    fn read_fingerprint(&self) -> ReadFingerprint {
        (
            self.fingerprint,
            self.mtime,
            self.mtime_nsec,
            self.ctime,
            self.ctime_nsec,
        )
    }
}
#[cfg(unix)]
fn owned_regular(value: &fs::Metadata) -> bool {
    value.is_file() && value.nlink() == 1 && value.uid() == unsafe { libc::getuid() }
}
#[cfg(target_os = "linux")]
fn link_bits(value: libc::nlink_t) -> u64 {
    value
}
#[cfg(all(unix, not(target_os = "linux")))]
fn link_bits(value: libc::nlink_t) -> u64 {
    u64::from(value)
}

#[cfg(unix)]
pub(super) struct Directory {
    root: PathBuf,
    chain: Vec<FileIdentity>,
    _capacity: NativeSetupReservation,
}
#[cfg(unix)]
impl Directory {
    pub(super) fn capture<S: OriginalAuthorityOwner>(
        op: &Operation<'_, S>,
        path: &Path,
        create: bool,
    ) -> Result<Self> {
        // The original platform helper only permits the OS-owned /var and /tmp
        // aliases. Reuse the Authority source's already audited normalization.
        let path = canonical(path)?;
        let count = path.components().count();
        let capacity = op.reserve(
            OriginalAuthorityStage::Path,
            path.as_os_str()
                .len()
                .saturating_mul(8)
                .saturating_add(count.saturating_mul(128))
                .saturating_add(8192),
        )?;
        let mut files = Vec::new();
        let mut chain = Vec::new();
        files
            .try_reserve_exact(count)
            .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Path))?;
        chain
            .try_reserve_exact(count)
            .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Path))?;
        let result = (|| {
            for component in path.components() {
                op.check(OriginalAuthorityStage::Path)?;
                let name = match component {
                    Component::RootDir => c"/".to_owned(),
                    Component::Normal(name) => CString::new(name.as_bytes())
                        .map_err(|_| OriginalAuthorityError::unavailable())?,
                    _ => return Err(secure_error("persistence ancestor is unsafe")),
                };
                let parent = files
                    .last()
                    .map_or(libc::AT_FDCWD, |file: &TrackedFile| file.as_raw_fd());
                let flags = libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC;
                let mut descriptor = unsafe { libc::openat(parent, name.as_ptr(), flags) };
                if descriptor < 0
                    && create
                    && std::io::Error::last_os_error().kind() == std::io::ErrorKind::NotFound
                {
                    if unsafe { libc::mkdirat(parent, name.as_ptr(), 0o700) } != 0
                        && std::io::Error::last_os_error().kind()
                            != std::io::ErrorKind::AlreadyExists
                    {
                        return Err(caused(
                            secure_error("persistence ancestor is unsafe"),
                            raw_io(std::io::Error::last_os_error(), name.to_str().ok()),
                        ));
                    }
                    descriptor = unsafe { libc::openat(parent, name.as_ptr(), flags) };
                }
                if descriptor < 0 {
                    return Err(caused(
                        secure_error("persistence ancestor is unsafe"),
                        raw_io(std::io::Error::last_os_error(), name.to_str().ok()),
                    ));
                }
                let file =
                    TrackedFile::new(unsafe { File::from_raw_fd(descriptor) }, op.cleanup.clone());
                let metadata = file.metadata().map_err(|error| {
                    caused(
                        secure_error("persistence ancestor is unsafe"),
                        raw_io(error, None),
                    )
                })?;
                chain.push(file_identity(&metadata));
                files.push(file);
            }
            let file = files
                .last()
                .ok_or_else(|| secure_error("persistence root is unavailable"))?;
            let metadata = file.metadata().map_err(|error| {
                caused(
                    secure_error("persistence ancestor is unsafe"),
                    raw_io(error, None),
                )
            })?;
            if !metadata.is_dir() || metadata.uid() != unsafe { libc::getuid() } {
                return Err(secure_error("persistence root is unsafe"));
            }
            if create && unsafe { libc::fchmod(file.as_raw_fd(), 0o700) } != 0 {
                return Err(caused(
                    secure_error("persistence ancestor is unsafe"),
                    raw_io(std::io::Error::last_os_error(), None),
                ));
            }
            Ok(())
        })();
        close_files(files, result)?;
        Ok(Self {
            root: path,
            chain,
            _capacity: capacity,
        })
    }
    fn open<S: OriginalAuthorityOwner>(&self, op: &Operation<'_, S>) -> Result<Vec<TrackedFile>> {
        let mut files = Vec::new();
        files
            .try_reserve_exact(self.chain.len())
            .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Path))?;
        let result = (|| {
            for (index, component) in self.root.components().enumerate() {
                op.check(OriginalAuthorityStage::Path)?;
                let name = match component {
                    Component::RootDir => c"/".to_owned(),
                    Component::Normal(name) => CString::new(name.as_bytes())
                        .map_err(|_| OriginalAuthorityError::unavailable())?,
                    _ => return Err(secure_error("persistence ancestor is unsafe")),
                };
                let parent = files
                    .last()
                    .map_or(libc::AT_FDCWD, |file: &TrackedFile| file.as_raw_fd());
                let descriptor = unsafe {
                    libc::openat(
                        parent,
                        name.as_ptr(),
                        libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                    )
                };
                if descriptor < 0 {
                    return Err(caused(
                        secure_error("persistence ancestor is unsafe"),
                        raw_io(std::io::Error::last_os_error(), name.to_str().ok()),
                    ));
                }
                let file =
                    TrackedFile::new(unsafe { File::from_raw_fd(descriptor) }, op.cleanup.clone());
                let metadata = file.metadata().map_err(|error| raw_io(error, None))?;
                files.push(file);
                if self.chain.get(index) != Some(&file_identity(&metadata)) {
                    return Err(secure_error("persistence ancestor identity changed"));
                }
            }
            Ok(())
        })();
        if let Err(error) = result {
            return close_files(files, Err(error));
        }
        Ok(files)
    }
    fn stat(parent: &TrackedFile, name: &str, required: bool) -> Result<Option<Stat>> {
        let text = CString::new(name).map_err(|_| OriginalAuthorityError::unavailable())?;
        // Original _stat_entry does lstat only: do not advance a file open/read.
        let mut stat = std::mem::MaybeUninit::<libc::stat>::uninit();
        if unsafe {
            libc::fstatat(
                parent.as_raw_fd(),
                text.as_ptr(),
                stat.as_mut_ptr(),
                libc::AT_SYMLINK_NOFOLLOW,
            )
        } != 0
        {
            let error = std::io::Error::last_os_error();
            if error.kind() == std::io::ErrorKind::NotFound {
                if !required {
                    return Ok(None);
                }
                // Original _stat_entry raises FileNotFoundError(name) from
                // None: errno/filename are None, while the actual stat error
                // remains its suppressed context. Do not flatten the two.
                let mut missing = raw("builtins.FileNotFoundError", name);
                missing.context = Some(Box::new(raw_io(error, Some(name))));
                missing.suppressed = true;
                return Err(missing);
            }
            return Err(raw_io(error, Some(name)));
        }
        let stat = unsafe { stat.assume_init() };
        if stat.st_mode & libc::S_IFMT == libc::S_IFLNK {
            return Err(secure_error("persistence entry is symlinked"));
        }
        if stat.st_mode & libc::S_IFMT != libc::S_IFREG
            || stat.st_nlink != 1
            || stat.st_uid != unsafe { libc::getuid() }
        {
            return Err(secure_error("persistence entry identity is unsafe"));
        }
        Ok(Some(Stat {
            fingerprint: (
                FileIdentity {
                    dev: device_bits(stat.st_dev),
                    ino: stat.st_ino,
                },
                mode_bits(stat.st_mode),
                link_bits(stat.st_nlink),
                stat.st_uid,
                stat.st_size as u64,
            ),
            mtime: stat.st_mtime,
            mtime_nsec: stat.st_mtime_nsec,
            ctime: stat.st_ctime,
            ctime_nsec: stat.st_ctime_nsec,
        }))
    }
    pub(super) fn exists<S: OriginalAuthorityOwner>(
        &self,
        op: &Operation<'_, S>,
        name: &str,
    ) -> Result<bool> {
        let files = self.open(op)?;
        let result = Self::stat(files.last().expect("directory chain"), name, false)
            .map(|value| value.is_some());
        close_files(files, result)
    }
    pub(super) fn read<S: OriginalAuthorityOwner>(
        &self,
        op: &Operation<'_, S>,
        name: &str,
    ) -> Result<Funded<Zeroizing<Vec<u8>>>> {
        self.read_relative(op, Path::new(name))
    }
    pub(super) fn read_relative<S: OriginalAuthorityOwner>(
        &self,
        op: &Operation<'_, S>,
        relative: &Path,
    ) -> Result<Funded<Zeroizing<Vec<u8>>>> {
        let parent = self.parent(op, relative)?;
        let result = (|| {
            let name = parent
                .name
                .to_str()
                .map_err(|_| OriginalAuthorityError::unavailable())?;
            let directory = parent.files.last().expect("directory chain");
            let before = Self::stat(directory, name, true)?.expect("required file");
            let text = CString::new(name).map_err(|_| OriginalAuthorityError::unavailable())?;
            let descriptor = unsafe {
                libc::openat(
                    directory.as_raw_fd(),
                    text.as_ptr(),
                    libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC,
                )
            };
            if descriptor < 0 {
                return Err(raw_io(std::io::Error::last_os_error(), Some(name)));
            }
            let mut file =
                TrackedFile::new(unsafe { File::from_raw_fd(descriptor) }, op.cleanup.clone());
            let read = (|| {
                let opened = file.metadata().map_err(|error| raw_io(error, None))?;
                if !owned_regular(&opened) || read_fingerprint(&opened) != before.read_fingerprint()
                {
                    return Err(secure_error("persistence entry changed before read"));
                }
                let bytes = read_stream(op, &mut file, 1024 * 1024)?;
                let after = file.metadata().map_err(|error| raw_io(error, None))?;
                let named = Self::stat(directory, name, true)?.expect("required file");
                if read_fingerprint(&after) != read_fingerprint(&opened)
                    || named.read_fingerprint() != read_fingerprint(&opened)
                {
                    return Err(secure_error("persistence entry changed during read"));
                }
                Ok(bytes)
            })();
            finish_scope(read, file.close().map_err(|error| raw_io(error, None)))
        })();
        parent.finish(result)
    }
    fn parent<S: OriginalAuthorityOwner>(
        &self,
        op: &Operation<'_, S>,
        relative: &Path,
    ) -> Result<Parent> {
        let capacity = op.reserve(
            OriginalAuthorityStage::Path,
            relative
                .as_os_str()
                .len()
                .saturating_mul(8)
                .saturating_add(
                    (self.chain.len() + relative.components().count()).saturating_mul(128),
                )
                .saturating_add(2048),
        )?;
        if relative.is_absolute() || relative.as_os_str().is_empty() {
            return Err(secure_error("persistence path must be relative"));
        }
        let parts = relative
            .components()
            .map(|part| match part {
                Component::Normal(name) => Ok(name),
                _ => Err(secure_error("persistence path is unsafe")),
            })
            .collect::<Result<Vec<_>>>()?;
        let Some((name, directories)) = parts.split_last() else {
            return Err(secure_error("persistence path must be relative"));
        };
        let name =
            CString::new(name.as_bytes()).map_err(|_| OriginalAuthorityError::unavailable())?;
        let mut files = self.open(op)?;
        if files.try_reserve_exact(directories.len()).is_err() {
            return close_files(
                files,
                Err(OriginalAuthorityError::capacity(
                    OriginalAuthorityStage::Path,
                )),
            );
        }
        let result = (|| {
            for component in directories {
                op.check(OriginalAuthorityStage::Path)?;
                let component = CString::new(component.as_bytes())
                    .map_err(|_| OriginalAuthorityError::unavailable())?;
                let descriptor = unsafe {
                    libc::openat(
                        files.last().expect("parent").as_raw_fd(),
                        component.as_ptr(),
                        libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                    )
                };
                if descriptor < 0 {
                    return Err(caused(
                        secure_error("persistence child directory is unsafe"),
                        raw_io(std::io::Error::last_os_error(), component.to_str().ok()),
                    ));
                }
                let file =
                    TrackedFile::new(unsafe { File::from_raw_fd(descriptor) }, op.cleanup.clone());
                let checked = file
                    .metadata()
                    .map_err(|error| {
                        caused(
                            secure_error("persistence child directory is unsafe"),
                            raw_io(error, None),
                        )
                    })
                    .and_then(|metadata| {
                        if metadata.is_dir() && metadata.uid() == unsafe { libc::getuid() } {
                            Ok(())
                        } else {
                            Err(secure_error("persistence child directory is unsafe"))
                        }
                    });
                if let Err(error) = checked {
                    return finish_scope(
                        Err(error),
                        file.close().map_err(|error| raw_io(error, None)),
                    );
                }
                files.push(file);
            }
            Ok(())
        })();
        if let Err(error) = result {
            return close_files(files, Err(error));
        }
        Ok(Parent {
            files,
            name,
            _capacity: capacity,
        })
    }
    pub(super) fn clear_journal<S: OriginalAuthorityOwner>(
        &self,
        op: &Operation<'_, S>,
    ) -> Result<()> {
        let parent = self.parent(op, Path::new(JOURNAL))?;
        let result = (|| {
            let directory = parent.files.last().expect("journal parent");
            let Some(before) = Self::stat(directory, JOURNAL, false)? else {
                return Ok(());
            };
            let current = Self::stat(directory, JOURNAL, true)?.expect("required journal");
            if current.fingerprint != before.fingerprint {
                return Err(secure_error("persistence entry changed before unlink"));
            }
            op.check(OriginalAuthorityStage::Source)?;
            if unsafe { libc::unlinkat(directory.as_raw_fd(), parent.name.as_ptr(), 0) } != 0 {
                return Err(raw_io(std::io::Error::last_os_error(), Some(JOURNAL)));
            }
            // A failed fsync follows an actual unlink. It is neither rolled
            // back nor reported as a read-only/ordinary successful recovery.
            if unsafe { libc::fsync(directory.as_raw_fd()) } != 0 {
                return Err(raw_io(std::io::Error::last_os_error(), None));
            }
            Ok(())
        })();
        // Original missing_ok catches around the complete parent context,
        // including its finally/close, rather than around unlink alone.
        match parent.finish(result) {
            Err(error)
                if error.class == "builtins.FileNotFoundError"
                    || (error.class == RAW_IO
                        && matches!(&error.cause, Some(OriginalAuthorityCause::Io(error)) if error.kind == std::io::ErrorKind::NotFound)) =>
            {
                Ok(())
            }
            other => other,
        }
    }
    pub(super) fn validate_file<S: OriginalAuthorityOwner>(
        &self,
        op: &Operation<'_, S>,
        name: &str,
        file: &TrackedFile,
    ) -> Result<()> {
        let opened = file.metadata().map_err(|error| raw_io(error, None))?;
        if !owned_regular(&opened) {
            return Err(secure_error("persistence open file identity is unsafe"));
        }
        let files = self.open(op)?;
        let result =
            Self::stat(files.last().expect("directory chain"), name, true).and_then(|named| {
                if named.is_some_and(|named| named.fingerprint == fingerprint(&opened)) {
                    Ok(())
                } else {
                    Err(secure_error("persistence open file identity changed"))
                }
            });
        close_files(files, result)
    }
}

#[cfg(unix)]
struct Parent {
    files: Vec<TrackedFile>,
    name: CString,
    _capacity: NativeSetupReservation,
}
#[cfg(unix)]
impl Parent {
    fn finish<T>(self, result: Result<T>) -> Result<T> {
        let Self {
            files,
            name,
            _capacity: capacity,
        } = self;
        let result = close_files(files, result);
        drop(name);
        // Parent names, descriptor vectors and actual close live inside the
        // same prepaid scope; only the finite result outlives this carrier.
        drop(capacity);
        result
    }
}

#[cfg(unix)]
fn close_files<T>(files: Vec<TrackedFile>, mut result: Result<T>) -> Result<T> {
    for file in files.into_iter().rev() {
        result = finish_scope(result, file.close().map_err(|error| raw_io(error, None)));
    }
    result
}

#[cfg(unix)]
struct ProcessLock {
    owner: Mutex<Option<std::thread::ThreadId>>,
    changed: Condvar,
}
#[cfg(unix)]
struct ProcessEntry {
    lock: std::sync::Weak<ProcessLock>,
    _control: ControlReservation,
}
#[cfg(unix)]
struct ProcessGuard {
    lock: Arc<ProcessLock>,
}
#[cfg(unix)]
impl Drop for ProcessGuard {
    fn drop(&mut self) {
        let mut owner = self
            .lock
            .owner
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        *owner = None;
        self.lock.changed.notify_all();
    }
}
#[cfg(unix)]
fn process_lock<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    path: &Path,
) -> Result<ProcessGuard> {
    static LOCKS: OnceLock<Mutex<BTreeMap<PathBuf, ProcessEntry>>> = OnceLock::new();
    // Every retained registry entry owns its bounded original control token.
    let control = op
        .invocation
        .control()
        .reserve(
            ControlKind::Registration,
            (std::mem::size_of::<ProcessEntry>()
                + std::mem::size_of::<ProcessLock>()
                + path.as_os_str().len()
                + 1024) as u64,
        )
        .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Guard))?;
    let local = {
        let mut locks = LOCKS
            .get_or_init(Mutex::default)
            .lock()
            .map_err(|_| OriginalAuthorityError::unavailable())?;
        locks.retain(|_, entry| entry.lock.strong_count() > 0);
        if let Some(lock) = locks.get(path).and_then(|entry| entry.lock.upgrade()) {
            lock
        } else {
            let lock = Arc::new(ProcessLock {
                owner: Mutex::new(None),
                changed: Condvar::new(),
            });
            locks.insert(
                path.to_owned(),
                ProcessEntry {
                    lock: Arc::downgrade(&lock),
                    _control: control,
                },
            );
            lock
        }
    };
    let mut owner = local
        .owner
        .lock()
        .map_err(|_| OriginalAuthorityError::unavailable())?;
    while owner.is_some() {
        op.check(OriginalAuthorityStage::Guard)?;
        if *owner == Some(std::thread::current().id()) {
            return Err(OriginalAuthorityError::unsupported(
                OriginalAuthorityStage::Guard,
                "nested publication recovery continuation",
            ));
        }
        owner = local
            .changed
            .wait_timeout(
                owner,
                Duration::from_millis(10)
                    .min(op.deadline.saturating_duration_since(Instant::now())),
            )
            .map_err(|_| OriginalAuthorityError::unavailable())?
            .0;
    }
    *owner = Some(std::thread::current().id());
    drop(owner);
    Ok(ProcessGuard { lock: local })
}

#[cfg(unix)]
pub(super) struct PublicationLock {
    pub(super) file: Option<TrackedFile>,
    locked: bool,
    local: Option<ProcessGuard>,
}
#[cfg(unix)]
impl PublicationLock {
    pub(super) fn acquire<S: OriginalAuthorityOwner>(
        op: &Operation<'_, S>,
        directory: &Directory,
    ) -> Result<Self> {
        let local = process_lock(op, &directory.root)?;
        let files = directory.open(op)?;
        let result = (|| {
            let parent = files.last().expect("directory chain");
            let before = Directory::stat(parent, LOCK, false)?;
            let flags = libc::O_RDWR
                | libc::O_NOFOLLOW
                | libc::O_CLOEXEC
                | if before.is_none() {
                    libc::O_CREAT | libc::O_EXCL
                } else {
                    0
                };
            let descriptor = unsafe {
                libc::openat(
                    parent.as_raw_fd(),
                    c".bootstrap-publication.lock".as_ptr(),
                    flags,
                    0o600,
                )
            };
            if descriptor < 0 {
                return Err(raw_io(std::io::Error::last_os_error(), Some(LOCK)));
            }
            let file =
                TrackedFile::new(unsafe { File::from_raw_fd(descriptor) }, op.cleanup.clone());
            let validation = (|| {
                let opened = file.metadata().map_err(|error| raw_io(error, None))?;
                let named = Directory::stat(parent, LOCK, true)?.expect("required lock");
                if !owned_regular(&opened)
                    || fingerprint(&opened) != named.fingerprint
                    || before
                        .as_ref()
                        .is_some_and(|before| before.fingerprint != fingerprint(&opened))
                {
                    return Err(secure_error("persistence lock identity is unsafe"));
                }
                if unsafe { libc::fchmod(file.as_raw_fd(), 0o600) } != 0 {
                    return Err(raw_io(std::io::Error::last_os_error(), None));
                }
                Ok(())
            })();
            if let Err(error) = validation {
                return finish_scope(
                    Err(error),
                    file.close().map_err(|error| raw_io(error, None)),
                );
            }
            Ok(file)
        })();
        let file = close_files(files, result).map_err(|error| {
            caused(
                raw(
                    "core_runtime.active_profile_store_v4.ActiveProfileStoreError",
                    "active Profile lock is unavailable",
                ),
                error,
            )
        })?;
        let mut guard = Self {
            file: Some(file),
            locked: false,
            local: Some(local),
        };
        loop {
            op.check(OriginalAuthorityStage::Guard)?;
            if unsafe {
                libc::flock(
                    guard.file.as_ref().expect("publication lock").as_raw_fd(),
                    libc::LOCK_EX | libc::LOCK_NB,
                )
            } == 0
            {
                guard.locked = true;
                break;
            }
            let error = std::io::Error::last_os_error();
            if !error
                .raw_os_error()
                .is_some_and(|errno| [libc::EACCES, libc::EAGAIN, libc::EDEADLK].contains(&errno))
            {
                return Err(caused(
                    raw(
                        "core_runtime.active_profile_store_v4.ActiveProfileStoreError",
                        "active Profile lock is unavailable",
                    ),
                    raw_io(error, None),
                ));
            }
            std::thread::park_timeout(
                Duration::from_millis(10)
                    .min(op.deadline.saturating_duration_since(Instant::now())),
            );
        }
        directory.validate_file(op, LOCK, guard.file.as_ref().expect("publication lock"))?;
        Ok(guard)
    }
    pub(super) fn close(&mut self) -> Result<()> {
        let Some(file) = self.file.take() else {
            return Ok(());
        };
        let unlocked =
            if self.locked && unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_UN) } != 0 {
                Err(raw_io(std::io::Error::last_os_error(), None))
            } else {
                Ok(())
            };
        self.locked = false;
        let result = finish_scope(unlocked, file.close().map_err(|error| raw_io(error, None)));
        drop(self.local.take());
        result
    }
}
#[cfg(unix)]
impl Drop for PublicationLock {
    fn drop(&mut self) {
        let _ = self.close();
    }
}
