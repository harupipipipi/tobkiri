//! SQLite's Unix VFS keeps its normal journal/locking implementation, while its
//! pathname syscalls resolve a unique virtual database name against an open dir.
//! Renaming/replacing a parent path cannot redirect database or journal effects.
use crate::StoreError;
use std::os::fd::AsRawFd;
use std::{
    collections::BTreeMap,
    ffi::{CStr, CString},
    fs::File,
    path::PathBuf,
    sync::{
        Arc, Mutex, OnceLock,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
};

const PREFIX: &str = "/tobkiri-db-";
#[derive(Clone)]
struct GuestRegistration {
    quota: Arc<crate::quota_vfs::QuotaDomain>,
    custody: Arc<crate::data_custody::DataCustody>,
    // Admitted with this DATA route, before SQLite opens. Only its unique
    // DatabaseGuard can install the exact connection; failure allocates no
    // recovery record and never refunds physical owner/control custody.
    retained_connection: Arc<Mutex<Option<rusqlite::Connection>>>,
}
struct Entry {
    directory: Arc<File>,
    filename: String,
    quota: Option<GuestRegistration>,
    owner: OwnerAccess,
}

enum OwnerAccess {
    Standard,
    Migration,
    ReadOnly(Arc<AtomicBool>),
}
fn registry() -> &'static Mutex<BTreeMap<String, Entry>> {
    static REGISTRY: OnceLock<Mutex<BTreeMap<String, Entry>>> = OnceLock::new();
    REGISTRY.get_or_init(|| Mutex::new(BTreeMap::new()))
}
/// Lifetime guard; held after the SQLite connection so SQLite closes first.
pub(crate) struct DatabaseGuard {
    key: String,
    _directory: Arc<File>,
    guest: Option<GuestRegistration>,
}
impl DatabaseGuard {
    pub(crate) fn quarantine(&self) {
        if let Some(guest) = &self.guest {
            guest.quota.quarantine_connection();
        }
    }
    pub(crate) fn is_quarantined(&self) -> bool {
        self.guest
            .as_ref()
            .is_some_and(|guest| guest.quota.is_quarantined())
    }
    pub(crate) fn with_exclusive_data_transfer<T>(
        &self,
        target: &Self,
        run: impl FnOnce(&mut crate::quota_vfs::TransferFiles<'_>) -> Result<T, StoreError>,
    ) -> Result<T, StoreError> {
        let source = self
            .guest
            .as_ref()
            .ok_or_else(|| StoreError::Denied("transfer requires DATA custody".into()))?;
        let target = target
            .guest
            .as_ref()
            .ok_or_else(|| StoreError::Denied("transfer requires DATA custody".into()))?;
        source.quota.with_exclusive_transfer(&target.quota, run)
    }
    #[cfg(test)]
    pub(crate) fn retained_connection_probe(
        &self,
    ) -> std::sync::Weak<Mutex<Option<rusqlite::Connection>>> {
        Arc::downgrade(&self.guest.as_ref().expect("DATA route").retained_connection)
    }
    /// Consume the unique route guard, retaining its exact connection in the
    /// slot reserved before open. Poison is recovered only to preserve custody.
    pub(crate) fn retain_connection(self, connection: rusqlite::Connection) {
        let guest = self
            .guest
            .as_ref()
            .expect("only DATA connections are retained");
        guest.quota.quarantine_connection();
        let mut retained = guest
            .retained_connection
            .lock()
            .unwrap_or_else(|poison| poison.into_inner());
        // A DatabaseGuard is noncloneable and this method consumes it. No other
        // path writes this slot, so replacement of a live connection is impossible.
        *retained = Some(connection);
    }
}
impl Drop for DatabaseGuard {
    fn drop(&mut self) {
        if self.is_quarantined() {
            return;
        }
        if let Ok(mut map) = registry().lock() {
            map.remove(&self.key);
        }
    }
}
pub(crate) fn register(
    directory: File,
    filename: &str,
) -> Result<(PathBuf, DatabaseGuard), StoreError> {
    register_mode(directory, filename, None, OwnerAccess::Standard)
}

pub(crate) fn register_migration(
    directory: File,
    filename: &str,
) -> Result<(PathBuf, DatabaseGuard), StoreError> {
    register_mode(directory, filename, None, OwnerAccess::Migration)
}

pub(crate) fn register_read_only(
    directory: File,
    filename: &str,
) -> Result<(PathBuf, DatabaseGuard, Arc<AtomicBool>), StoreError> {
    let refused_sidecar = Arc::new(AtomicBool::new(false));
    let (path, guard) = register_mode(
        directory,
        filename,
        None,
        OwnerAccess::ReadOnly(refused_sidecar.clone()),
    )?;
    Ok((path, guard, refused_sidecar))
}

pub(crate) fn register_guest_data(
    directory: File,
    filename: &str,
    policy: crate::StoreDiskPolicy,
    custody: Arc<crate::data_custody::DataCustody>,
) -> Result<(PathBuf, DatabaseGuard), StoreError> {
    register_mode(
        directory,
        filename,
        Some((policy, custody)),
        OwnerAccess::Standard,
    )
}

fn register_mode(
    directory: File,
    filename: &str,
    policy: Option<(
        crate::StoreDiskPolicy,
        Arc<crate::data_custody::DataCustody>,
    )>,
    owner: OwnerAccess,
) -> Result<(PathBuf, DatabaseGuard), StoreError> {
    install()?;
    static NEXT: AtomicU64 = AtomicU64::new(1);
    let key = format!(
        "{}{}-{}",
        PREFIX,
        std::process::id(),
        NEXT.fetch_add(1, Ordering::Relaxed)
    );
    let directory = Arc::new(directory);
    let quota = policy
        .map(|(policy, custody)| {
            crate::quota_vfs::QuotaDomain::acquire(directory.clone(), filename, policy).map(
                |quota| GuestRegistration {
                    quota,
                    custody,
                    retained_connection: Arc::new(Mutex::new(None)),
                },
            )
        })
        .transpose()?;
    registry()
        .lock()
        .map_err(|_| StoreError::Conflict("SQLite capability registry poisoned".into()))?
        .insert(
            key.clone(),
            Entry {
                directory: directory.clone(),
                filename: filename.into(),
                quota: quota.clone(),
                owner,
            },
        );
    let path = PathBuf::from(&key).join(filename);
    Ok((
        path,
        DatabaseGuard {
            key,
            _directory: directory,
            guest: quota,
        },
    ))
}

enum Access {
    Owner,
    Migration,
    ReadOnly { sidecar: Option<Arc<AtomicBool>> },
    Guest(GuestRegistration),
}

enum Route {
    Plain,
    Denied,
    Cap(Arc<File>, Option<CString>, Access),
}
unsafe fn route(path: *const libc::c_char) -> Route {
    if path.is_null() {
        return Route::Denied;
    }
    let raw = unsafe { CStr::from_ptr(path) }.to_bytes();
    if !raw.starts_with(PREFIX.as_bytes()) {
        return Route::Plain;
    }
    let Ok(path) = std::str::from_utf8(raw) else {
        return Route::Denied;
    };
    let Ok(map) = registry().lock() else {
        return Route::Denied;
    };
    for (key, entry) in map.range(..=path.to_owned()).rev() {
        if path == key {
            return Route::Cap(entry.directory.clone(), None, Access::Owner);
        }
        if let Some(name) = path.strip_prefix(&format!("{key}/")) {
            let wal = name == format!("{}-wal", entry.filename);
            let shm = name == format!("{}-shm", entry.filename);
            if entry.quota.is_some() && (wal || shm) {
                return Route::Denied;
            }
            let valid = name == entry.filename
                || name == format!("{}-journal", entry.filename)
                || wal
                || shm;
            if !valid || name.contains('/') || name.contains('\\') {
                return Route::Denied;
            }
            return match CString::new(name) {
                Ok(name) => {
                    let access = match (&entry.quota, &entry.owner) {
                        (Some(guest), _) => Access::Guest(guest.clone()),
                        (None, OwnerAccess::ReadOnly(refused_sidecar)) => Access::ReadOnly {
                            sidecar: if name.as_bytes() == entry.filename.as_bytes() {
                                None
                            } else {
                                Some(refused_sidecar.clone())
                            },
                        },
                        (None, OwnerAccess::Migration) => Access::Migration,
                        (None, OwnerAccess::Standard) => Access::Owner,
                    };
                    Route::Cap(entry.directory.clone(), Some(name), access)
                }
                Err(_) => Route::Denied,
            };
        }
    }
    Route::Denied
}
/// Resolve only explicitly registered guest DATA; trusted catalogs and audit
/// routes never receive the quota VFS or an alias through it.
pub(crate) unsafe fn guest_route(
    path: *const libc::c_char,
) -> Option<(
    Arc<crate::quota_vfs::QuotaDomain>,
    CString,
    Arc<crate::data_custody::DataCustody>,
)> {
    match unsafe { route(path) } {
        Route::Cap(_, Some(name), Access::Guest(guest)) => Some((guest.quota, name, guest.custody)),
        _ => None,
    }
}
#[cfg(target_os = "macos")]
fn denied() -> libc::c_int {
    unsafe { *libc::__error() = libc::EACCES };
    -1
}
#[cfg(target_os = "linux")]
fn denied() -> libc::c_int {
    unsafe { *libc::__errno_location() = libc::EACCES };
    -1
}
#[cfg(not(any(target_os = "macos", target_os = "linux")))]
fn denied() -> libc::c_int {
    -1
}

unsafe fn cap_open_impl(
    path: *const libc::c_char,
    flags: libc::c_int,
    mode: libc::c_int,
) -> libc::c_int {
    match unsafe { route(path) } {
        Route::Plain => unsafe { libc::open(path, flags, mode) },
        Route::Denied => denied(),
        Route::Cap(directory, None, _) => {
            if flags & (libc::O_WRONLY | libc::O_RDWR | libc::O_CREAT | libc::O_TRUNC) != 0 {
                denied()
            } else {
                unsafe { libc::fcntl(directory.as_raw_fd(), libc::F_DUPFD_CLOEXEC, 0) }
            }
        }
        Route::Cap(directory, Some(name), access) => {
            match &access {
                // SQLite can write SHM and change sidecar ownership even through
                // a read-only connection. Keep their existence visible but
                // leave their actual opens to the database owner.
                Access::ReadOnly {
                    sidecar: Some(refused),
                } => {
                    if crate::secure::stat_entry(&directory, &name).is_ok_and(|stat| {
                        stat.st_mode & libc::S_IFMT == libc::S_IFREG && stat.st_nlink == 1
                    }) {
                        refused.store(true, Ordering::Relaxed);
                    }
                    return denied();
                }
                Access::ReadOnly { .. }
                    if flags & (libc::O_ACCMODE | libc::O_CREAT | libc::O_TRUNC) != 0 =>
                {
                    return denied();
                }
                // DATA extensions must go through the admitted xWrite/xTruncate.
                Access::Guest(_) if flags & libc::O_TRUNC != 0 => return denied(),
                _ => {}
            }
            let nonblocking = matches!(access, Access::ReadOnly { .. } | Access::Migration);
            let open_at = |flags| unsafe {
                libc::openat(
                    directory.as_raw_fd(),
                    name.as_ptr(),
                    flags
                        | libc::O_NOFOLLOW
                        | libc::O_CLOEXEC
                        | if nonblocking { libc::O_NONBLOCK } else { 0 },
                    0o600,
                )
            };
            let fd = open_at(flags);
            // Darwin can report ENOENT when two nofollow O_CREAT opens race.
            // Try the winning inode without creation before SQLite retries read-only.
            #[cfg(target_os = "macos")]
            let fd = if fd < 0
                && matches!(access, Access::Migration)
                && flags & (libc::O_CREAT | libc::O_EXCL) == libc::O_CREAT
                && std::io::Error::last_os_error().raw_os_error() == Some(libc::ENOENT)
            {
                open_at(flags & !libc::O_CREAT)
            } else {
                fd
            };
            if fd < 0 || matches!(access, Access::Owner) {
                return fd;
            }
            let mut metadata = std::mem::MaybeUninit::<libc::stat>::uninit();
            if unsafe { libc::fstat(fd, metadata.as_mut_ptr()) } < 0 {
                unsafe {
                    libc::close(fd);
                }
                return denied();
            }
            let metadata = unsafe { metadata.assume_init() };
            if metadata.st_nlink != 1 || metadata.st_mode & libc::S_IFMT != libc::S_IFREG {
                unsafe {
                    libc::close(fd);
                }
                return denied();
            }
            fd
        }
    }
}
unsafe fn cap_stat_impl(path: *const libc::c_char, buf: *mut libc::stat) -> libc::c_int {
    match unsafe { route(path) } {
        Route::Plain => unsafe { libc::stat(path, buf) },
        Route::Denied => denied(),
        Route::Cap(directory, None, _) => unsafe { libc::fstat(directory.as_raw_fd(), buf) },
        Route::Cap(directory, Some(name), _) => unsafe {
            libc::fstatat(
                directory.as_raw_fd(),
                name.as_ptr(),
                buf,
                libc::AT_SYMLINK_NOFOLLOW,
            )
        },
    }
}
unsafe fn cap_lstat_impl(path: *const libc::c_char, buf: *mut libc::stat) -> libc::c_int {
    match unsafe { route(path) } {
        Route::Plain => unsafe { libc::lstat(path, buf) },
        Route::Denied => denied(),
        Route::Cap(directory, None, _) => unsafe { libc::fstat(directory.as_raw_fd(), buf) },
        Route::Cap(directory, Some(name), _) => unsafe {
            libc::fstatat(
                directory.as_raw_fd(),
                name.as_ptr(),
                buf,
                libc::AT_SYMLINK_NOFOLLOW,
            )
        },
    }
}
unsafe fn cap_access_impl(path: *const libc::c_char, mode: libc::c_int) -> libc::c_int {
    match unsafe { route(path) } {
        Route::Plain => unsafe { libc::access(path, mode) },
        Route::Denied => denied(),
        Route::Cap(directory, None, _) => {
            let mut stat = std::mem::MaybeUninit::uninit();
            if unsafe { libc::fstat(directory.as_raw_fd(), stat.as_mut_ptr()) } < 0 {
                return -1;
            }
            let stat = unsafe { stat.assume_init() };
            if mode == libc::F_OK || mode & libc::W_OK == 0 || stat.st_mode & 0o200 != 0 {
                0
            } else {
                denied()
            }
        }
        Route::Cap(directory, Some(name), _) => {
            let mut stat = std::mem::MaybeUninit::uninit();
            if unsafe {
                libc::fstatat(
                    directory.as_raw_fd(),
                    name.as_ptr(),
                    stat.as_mut_ptr(),
                    libc::AT_SYMLINK_NOFOLLOW,
                )
            } < 0
            {
                return -1;
            }
            let stat = unsafe { stat.assume_init() };
            if stat.st_mode & libc::S_IFMT == libc::S_IFLNK {
                return denied();
            }
            let access = unsafe { libc::faccessat(directory.as_raw_fd(), name.as_ptr(), mode, 0) };
            let mut after = std::mem::MaybeUninit::<libc::stat>::uninit();
            if unsafe {
                libc::fstatat(
                    directory.as_raw_fd(),
                    name.as_ptr(),
                    after.as_mut_ptr(),
                    libc::AT_SYMLINK_NOFOLLOW,
                )
            } < 0
            {
                return -1;
            }
            let after = unsafe { after.assume_init() };
            if after.st_mode & libc::S_IFMT == libc::S_IFLNK
                || (after.st_dev, after.st_ino) != (stat.st_dev, stat.st_ino)
            {
                return denied();
            }
            access
        }
    }
}
unsafe fn cap_unlink_impl(path: *const libc::c_char) -> libc::c_int {
    match unsafe { route(path) } {
        Route::Plain => unsafe { libc::unlink(path) },
        Route::Cap(_, Some(name), Access::Guest(guest)) => match guest.quota.unlink(&name) {
            Ok(result) => result,
            Err(_) => denied(),
        },
        Route::Cap(directory, Some(name), Access::Owner | Access::Migration) => unsafe {
            libc::unlinkat(directory.as_raw_fd(), name.as_ptr(), 0)
        },
        _ => denied(),
    }
}
unsafe fn cap_open_directory_impl(path: *const libc::c_char, out: *mut libc::c_int) -> libc::c_int {
    if path.is_null() || out.is_null() {
        return rusqlite::ffi::SQLITE_CANTOPEN;
    }
    let bytes = unsafe { CStr::from_ptr(path) }.to_bytes();
    let Some(index) = bytes.iter().rposition(|b| *b == b'/') else {
        return rusqlite::ffi::SQLITE_CANTOPEN;
    };
    let parent = match CString::new(&bytes[..index]) {
        Ok(v) => v,
        Err(_) => return rusqlite::ffi::SQLITE_CANTOPEN,
    };
    let fd = unsafe {
        cap_open(
            parent.as_ptr(),
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_CLOEXEC,
            0,
        )
    };
    unsafe { *out = fd };
    if fd < 0 {
        rusqlite::ffi::SQLITE_CANTOPEN
    } else {
        rusqlite::ffi::SQLITE_OK
    }
}
// Syscall hooks are C entry points too; no Rust panic may unwind into SQLite.
unsafe extern "C" fn cap_open(
    path: *const libc::c_char,
    flags: libc::c_int,
    mode: libc::c_int,
) -> libc::c_int {
    std::panic::catch_unwind(|| unsafe { cap_open_impl(path, flags, mode) })
        .unwrap_or_else(|_| denied())
}
unsafe extern "C" fn cap_stat(path: *const libc::c_char, out: *mut libc::stat) -> libc::c_int {
    std::panic::catch_unwind(|| unsafe { cap_stat_impl(path, out) }).unwrap_or_else(|_| denied())
}
unsafe extern "C" fn cap_lstat(path: *const libc::c_char, out: *mut libc::stat) -> libc::c_int {
    std::panic::catch_unwind(|| unsafe { cap_lstat_impl(path, out) }).unwrap_or_else(|_| denied())
}
unsafe extern "C" fn cap_access(path: *const libc::c_char, mode: libc::c_int) -> libc::c_int {
    std::panic::catch_unwind(|| unsafe { cap_access_impl(path, mode) }).unwrap_or_else(|_| denied())
}
unsafe extern "C" fn cap_unlink(path: *const libc::c_char) -> libc::c_int {
    std::panic::catch_unwind(|| unsafe { cap_unlink_impl(path) }).unwrap_or_else(|_| denied())
}
unsafe extern "C" fn cap_open_directory(
    path: *const libc::c_char,
    out: *mut libc::c_int,
) -> libc::c_int {
    std::panic::catch_unwind(|| unsafe { cap_open_directory_impl(path, out) })
        .unwrap_or(rusqlite::ffi::SQLITE_CANTOPEN)
}
fn install() -> Result<(), StoreError> {
    static INSTALLED: OnceLock<Result<(), String>> = OnceLock::new();
    let result = INSTALLED.get_or_init(|| unsafe {
        if rusqlite::ffi::sqlite3_initialize() != rusqlite::ffi::SQLITE_OK {
            return Err("SQLite initialization failed".into());
        }
        let vfs = rusqlite::ffi::sqlite3_vfs_find(std::ptr::null());
        if vfs.is_null() {
            return Err("SQLite VFS unavailable".into());
        }
        let Some(set) = (*vfs).xSetSystemCall else {
            return Err("SQLite VFS lacks nofollow syscall hooks".into());
        };
        let hooks: [(&CStr, unsafe extern "C" fn()); 6] = [
            (
                c"open",
                std::mem::transmute::<
                    unsafe extern "C" fn(
                        *const libc::c_char,
                        libc::c_int,
                        libc::c_int,
                    ) -> libc::c_int,
                    unsafe extern "C" fn(),
                >(cap_open),
            ),
            (
                c"stat",
                std::mem::transmute::<
                    unsafe extern "C" fn(*const libc::c_char, *mut libc::stat) -> libc::c_int,
                    unsafe extern "C" fn(),
                >(cap_stat),
            ),
            (
                c"lstat",
                std::mem::transmute::<
                    unsafe extern "C" fn(*const libc::c_char, *mut libc::stat) -> libc::c_int,
                    unsafe extern "C" fn(),
                >(cap_lstat),
            ),
            (
                c"access",
                std::mem::transmute::<
                    unsafe extern "C" fn(*const libc::c_char, libc::c_int) -> libc::c_int,
                    unsafe extern "C" fn(),
                >(cap_access),
            ),
            (
                c"unlink",
                std::mem::transmute::<
                    unsafe extern "C" fn(*const libc::c_char) -> libc::c_int,
                    unsafe extern "C" fn(),
                >(cap_unlink),
            ),
            (
                c"openDirectory",
                std::mem::transmute::<
                    unsafe extern "C" fn(*const libc::c_char, *mut libc::c_int) -> libc::c_int,
                    unsafe extern "C" fn(),
                >(cap_open_directory),
            ),
        ];
        for (name, hook) in hooks {
            if set(vfs, name.as_ptr(), Some(hook)) != rusqlite::ffi::SQLITE_OK {
                return Err("SQLite capability VFS hook unavailable".into());
            }
        }
        crate::quota_vfs::install(vfs)?;
        Ok(())
    });
    result.clone().map_err(StoreError::Unavailable)
}
