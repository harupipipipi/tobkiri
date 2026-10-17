//! SQLite's bundled win32 VFS retains its native I/O and byte-lock protocol.
//! Registered virtual paths use pinned directory-relative Windows primitives;
//! no DB or journal open re-resolves a diagnostic filesystem pathname.
use crate::StoreError;

use std::{
    collections::BTreeMap,
    ffi::{CStr, CString},
    fs::File,
    path::PathBuf,
    sync::{
        Arc, Mutex, OnceLock,
        atomic::{AtomicU64, Ordering},
    },
};

const PREFIX: &str = r"C:\tobkiri-db-";
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
    register_mode(directory, filename, None)
}

pub(crate) fn register_guest_data(
    directory: File,
    filename: &str,
    policy: crate::StoreDiskPolicy,
    custody: Arc<crate::data_custody::DataCustody>,
) -> Result<(PathBuf, DatabaseGuard), StoreError> {
    register_mode(directory, filename, Some((policy, custody)))
}

fn register_mode(
    directory: File,
    filename: &str,
    policy: Option<(
        crate::StoreDiskPolicy,
        Arc<crate::data_custody::DataCustody>,
    )>,
) -> Result<(PathBuf, DatabaseGuard), StoreError> {
    crate::secure::name(filename)?;
    crate::secure::windows::validate_storage_component(filename)?;
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

enum Route {
    Plain,
    Denied,
    Cap(Arc<File>, Option<CString>, Option<GuestRegistration>),
    // SQLite probes for an absent WAL/SHM even in DELETE mode. Metadata must
    // describe the actual file; every open, create and delete remains denied.
    SidecarMetadata(Arc<File>, CString),
}
fn route_text(path: &str) -> Route {
    let path = path
        .strip_prefix(r"\\?\")
        .unwrap_or(path)
        .replace('/', "\\");
    if !path.starts_with(PREFIX) {
        return Route::Plain;
    }
    let Ok(map) = registry().lock() else {
        return Route::Denied;
    };
    for (key, entry) in map.range(..=path.clone()).rev() {
        if path == *key {
            return Route::Cap(entry.directory.clone(), None, entry.quota.clone());
        }
        if let Some(name) = path.strip_prefix(&format!("{key}\\")) {
            let wal = name == format!("{}-wal", entry.filename);
            let shm = name == format!("{}-shm", entry.filename);
            let valid = name == entry.filename
                || name == format!("{}-journal", entry.filename)
                || wal
                || shm;
            if !valid || name.contains('/') || name.contains('\\') {
                return Route::Denied;
            }
            return match CString::new(name) {
                Ok(name) if entry.quota.is_some() && (wal || shm) => {
                    Route::SidecarMetadata(entry.directory.clone(), name)
                }
                Ok(name) => Route::Cap(entry.directory.clone(), Some(name), entry.quota.clone()),
                Err(_) => Route::Denied,
            };
        }
    }
    Route::Denied
}
unsafe fn route(path: *const libc::c_char) -> Route {
    if path.is_null() {
        return Route::Denied;
    }
    match unsafe { CStr::from_ptr(path) }.to_str() {
        Ok(path) => route_text(path),
        Err(_) => Route::Denied,
    }
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
        Route::Cap(_, Some(name), Some(guest)) => Some((guest.quota, name, guest.custody)),
        _ => None,
    }
}

use crate::secure::windows as secure;
use std::{
    mem::zeroed,
    os::windows::io::{AsRawHandle, IntoRawHandle},
    panic::{AssertUnwindSafe, catch_unwind},
};
use windows_sys::Win32::{
    Foundation::{HANDLE, INVALID_HANDLE_VALUE, SetLastError},
    Security::SECURITY_ATTRIBUTES,
    Storage::FileSystem::*,
};
use windows_sys::core::BOOL;

fn set_error(error: StoreError) {
    let code = match error {
        StoreError::Io(error) => error.raw_os_error().unwrap_or(5) as u32,
        StoreError::NotFound => 2,
        StoreError::Conflict(_) => 32,
        StoreError::Invalid(_) => 123,
        StoreError::Quota => 112,
        _ => 5,
    };
    unsafe { SetLastError(code) };
}
fn denied() {
    unsafe { SetLastError(5) };
}

unsafe fn route_wide(path: *const u16) -> Route {
    if path.is_null() {
        return Route::Denied;
    }
    // SQLite's Windows VFS already bounds paths. Keep this adapter bounded too.
    for length in 0..32768 {
        if unsafe { *path.add(length) } == 0 {
            return match String::from_utf16(unsafe { std::slice::from_raw_parts(path, length) }) {
                Ok(path) => route_text(&path),
                Err(_) => Route::Denied,
            };
        }
    }
    Route::Denied
}

unsafe extern "system" fn cap_create_file(
    path: *const u16,
    access: u32,
    share: u32,
    attributes: *const SECURITY_ATTRIBUTES,
    disposition: u32,
    flags: u32,
    template: HANDLE,
) -> HANDLE {
    catch_unwind(AssertUnwindSafe(|| unsafe {
        match route_wide(path) {
            Route::Plain => CreateFileW(
                path,
                access,
                share,
                attributes,
                disposition,
                flags,
                template,
            ),
            Route::Cap(directory, Some(name), quota) => {
                // Creation may be requested; truncation must not spend recovery
                // state outside the quota VFS's admitted mutation operation.
                if quota.is_some()
                    && (disposition == CREATE_ALWAYS
                        || disposition == TRUNCATE_EXISTING
                        || flags & FILE_FLAG_DELETE_ON_CLOSE != 0)
                {
                    denied();
                    return INVALID_HANDLE_VALUE;
                }
                let Ok(name) = name.to_str() else {
                    denied();
                    return INVALID_HANDLE_VALUE;
                };
                match secure::open_file_native(&directory, name, access, share, disposition, flags)
                {
                    Ok(file) => file.into_raw_handle(),
                    Err(error) => {
                        set_error(error);
                        INVALID_HANDLE_VALUE
                    }
                }
            }
            _ => {
                denied();
                INVALID_HANDLE_VALUE
            }
        }
    }))
    .unwrap_or_else(|_| {
        denied();
        INVALID_HANDLE_VALUE
    })
}

unsafe extern "system" fn cap_delete_file(path: *const u16) -> BOOL {
    catch_unwind(AssertUnwindSafe(|| unsafe {
        match route_wide(path) {
            Route::Plain => DeleteFileW(path),
            Route::Cap(directory, Some(name), guest) => {
                if let Some(guest) = guest {
                    return match guest.quota.unlink(&name) {
                        Ok(0) => 1,
                        _ => {
                            denied();
                            0
                        }
                    };
                }
                let Ok(name) = name.to_str() else {
                    denied();
                    return 0;
                };
                match secure::unlink_file(&directory, name) {
                    Ok(()) => 1,
                    Err(error) => {
                        set_error(error);
                        0
                    }
                }
            }
            _ => {
                denied();
                0
            }
        }
    }))
    .unwrap_or_else(|_| {
        denied();
        0
    })
}

fn information(
    directory: Arc<File>,
    name: Option<CString>,
) -> Result<BY_HANDLE_FILE_INFORMATION, StoreError> {
    let file = match name {
        None => directory.try_clone()?,
        Some(name) => {
            let name = name
                .to_str()
                .map_err(|_| StoreError::Invalid("invalid SQLite name".into()))?;
            secure::open_file_native(
                &directory,
                name,
                FILE_READ_ATTRIBUTES | READ_CONTROL,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                OPEN_EXISTING,
                FILE_FLAG_OPEN_REPARSE_POINT,
            )?
        }
    };
    let mut information: BY_HANDLE_FILE_INFORMATION = unsafe { zeroed() };
    if unsafe { GetFileInformationByHandle(file.as_raw_handle(), &mut information) } == 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    Ok(information)
}

unsafe extern "system" fn cap_attributes(path: *const u16) -> u32 {
    catch_unwind(AssertUnwindSafe(|| unsafe {
        let result = match route_wide(path) {
            Route::Plain => return GetFileAttributesW(path),
            Route::Cap(directory, name, _) => information(directory, name),
            Route::SidecarMetadata(directory, name) => information(directory, Some(name)),
            Route::Denied => {
                denied();
                return INVALID_FILE_ATTRIBUTES;
            }
        };
        match result {
            Ok(information) => information.dwFileAttributes,
            Err(error) => {
                set_error(error);
                INVALID_FILE_ATTRIBUTES
            }
        }
    }))
    .unwrap_or_else(|_| {
        denied();
        INVALID_FILE_ATTRIBUTES
    })
}

unsafe extern "system" fn cap_attributes_ex(
    path: *const u16,
    level: GET_FILEEX_INFO_LEVELS,
    out: *mut std::ffi::c_void,
) -> BOOL {
    catch_unwind(AssertUnwindSafe(|| unsafe {
        let result = match route_wide(path) {
            Route::Plain => return GetFileAttributesExW(path, level, out),
            Route::Cap(directory, name, _) => information(directory, name),
            Route::SidecarMetadata(directory, name) => information(directory, Some(name)),
            Route::Denied => {
                denied();
                return 0;
            }
        };
        if level != GetFileExInfoStandard || out.is_null() {
            SetLastError(87);
            return 0;
        }
        match result {
            Ok(information) => {
                out.cast::<WIN32_FILE_ATTRIBUTE_DATA>()
                    .write(WIN32_FILE_ATTRIBUTE_DATA {
                        dwFileAttributes: information.dwFileAttributes,
                        ftCreationTime: information.ftCreationTime,
                        ftLastAccessTime: information.ftLastAccessTime,
                        ftLastWriteTime: information.ftLastWriteTime,
                        nFileSizeHigh: information.nFileSizeHigh,
                        nFileSizeLow: information.nFileSizeLow,
                    });
                1
            }
            Err(error) => {
                set_error(error);
                0
            }
        }
    }))
    .unwrap_or_else(|_| {
        denied();
        0
    })
}

fn install() -> Result<(), StoreError> {
    static INSTALLED: OnceLock<Result<(), String>> = OnceLock::new();
    INSTALLED
        .get_or_init(|| unsafe {
            use rusqlite::ffi::*;
            if sqlite3_initialize() != SQLITE_OK {
                return Err("SQLite initialization failed".into());
            }
            let vfs = sqlite3_vfs_find(std::ptr::null());
            if vfs.is_null()
                || (*vfs).iVersion < 3
                || !CStr::from_ptr((*vfs).zName)
                    .to_bytes()
                    .starts_with(b"win32")
            {
                return Err("SQLite native Windows VFS unavailable".into());
            }
            let Some(set) = (*vfs).xSetSystemCall else {
                return Err("SQLite Windows syscall hooks unavailable".into());
            };
            let Some(get) = (*vfs).xGetSystemCall else {
                return Err("SQLite Windows syscall query unavailable".into());
            };
            let hooks = [
                (c"CreateFileW", cap_create_file as *const ()),
                (c"DeleteFileW", cap_delete_file as *const ()),
                (c"GetFileAttributesW", cap_attributes as *const ()),
                (c"GetFileAttributesExW", cap_attributes_ex as *const ()),
            ];
            for (name, _) in &hooks {
                if get(vfs, name.as_ptr()).is_none() {
                    return Err("required SQLite Windows capability hook unavailable".into());
                }
            }
            for (name, hook) in hooks {
                let hook = std::mem::transmute::<*const (), unsafe extern "C" fn()>(hook);
                if set(vfs, name.as_ptr(), Some(hook)) != SQLITE_OK {
                    return Err("SQLite Windows capability hook installation failed".into());
                }
            }
            crate::quota_vfs::install(vfs)?;
            Ok(())
        })
        .clone()
        .map_err(StoreError::Unavailable)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{DataRoot, StoreDiskPolicy};
    use rusqlite::{Connection, ErrorCode, OpenFlags};

    fn root() -> (tempfile::TempDir, DataRoot) {
        let temporary = tempfile::Builder::new()
            .prefix(".windows-sqlite-")
            .tempdir_in(std::env::current_dir().unwrap())
            .unwrap();
        let root = DataRoot::open(temporary.path().join("data")).unwrap();
        (temporary, root)
    }
    fn custody(root: &DataRoot) -> Arc<crate::data_custody::DataCustody> {
        let profile = tobkiri_canon::ids::ProfileId::parse("prof_sqlite_windows").unwrap();
        let pack = tobkiri_canon::ids::PackId::parse("local.sqlite-windows").unwrap();
        let owner = root.owner_lease(&profile, &pack).unwrap();
        let control = tobkiri_gate::CoreControl::default();
        crate::data_custody::DataCustody::registered(
            Some(owner),
            control
                .reserve(tobkiri_gate::ControlKind::Registration, 64 * 1024)
                .unwrap(),
        )
    }
    fn open(path: PathBuf, quota: bool) -> Connection {
        let flags = OpenFlags::SQLITE_OPEN_READ_WRITE
            | OpenFlags::SQLITE_OPEN_CREATE
            | OpenFlags::SQLITE_OPEN_NO_MUTEX;
        let connection = if quota {
            Connection::open_with_flags_and_vfs(path, flags, crate::quota_vfs::NAME)
        } else {
            Connection::open_with_flags(path, flags)
        }
        .unwrap();
        connection
            .execute_batch(
                "PRAGMA journal_mode=DELETE; PRAGMA temp_store=MEMORY; PRAGMA mmap_size=0;",
            )
            .unwrap();
        connection
    }

    #[test]
    fn native_sqlite_persists_through_registered_directory_handle() {
        let (_temporary, root) = root();
        let (path, guard) = register(
            root.directory_capability().try_clone().unwrap(),
            "trusted.sqlite",
        )
        .unwrap();
        let connection = open(path, false);
        connection
            .execute_batch(
                "CREATE TABLE fixture(value TEXT); INSERT INTO fixture VALUES('retained');",
            )
            .unwrap();
        drop(connection);
        drop(guard);
        let (path, guard) = register(
            root.directory_capability().try_clone().unwrap(),
            "trusted.sqlite",
        )
        .unwrap();
        let connection = open(path, false);
        assert_eq!(
            connection
                .query_row("SELECT value FROM fixture", [], |row| row
                    .get::<_, String>(0))
                .unwrap(),
            "retained"
        );
        drop(connection);
        drop(guard);
        assert!(root.file_len("trusted.sqlite").unwrap().unwrap() > 0);
    }

    #[test]
    fn case_aliases_are_rejected_and_distinct_canonical_databases_stay_isolated() {
        let (_temporary, root) = root();
        assert!(
            register(
                root.directory_capability().try_clone().unwrap(),
                "Data.sqlite"
            )
            .is_err()
        );
        let policy = StoreDiskPolicy::new(64 * 1024, 64 * 1024, 128 * 1024).unwrap();
        assert!(
            register_guest_data(
                root.directory_capability().try_clone().unwrap(),
                "Data.sqlite",
                policy,
                custody(&root)
            )
            .is_err()
        );
        let (first, first_guard) = register(
            root.directory_capability().try_clone().unwrap(),
            "data.sqlite",
        )
        .unwrap();
        let first = open(first, false);
        first
            .execute_batch("CREATE TABLE fixture(value TEXT); INSERT INTO fixture VALUES('first');")
            .unwrap();
        let (second, second_guard) = register(
            root.directory_capability().try_clone().unwrap(),
            "other.sqlite",
        )
        .unwrap();
        let second = open(second, false);
        second
            .execute_batch(
                "CREATE TABLE fixture(value TEXT); INSERT INTO fixture VALUES('second');",
            )
            .unwrap();
        assert_eq!(
            first
                .query_row("SELECT value FROM fixture", [], |row| row
                    .get::<_, String>(0))
                .unwrap(),
            "first"
        );
        assert_eq!(
            second
                .query_row("SELECT value FROM fixture", [], |row| row
                    .get::<_, String>(0))
                .unwrap(),
            "second"
        );
        assert!(
            register(
                root.directory_capability().try_clone().unwrap(),
                "DATA.sqlite"
            )
            .is_err()
        );
        assert!(
            crate::quota_vfs::QuotaDomain::acquire(
                root.directory_capability(),
                "DATA.sqlite",
                policy
            )
            .is_err()
        );
        drop(second);
        drop(first);
        drop(second_guard);
        drop(first_guard);
    }

    #[test]
    fn native_sqlite_locks_conflict_across_virtual_aliases() {
        let (_temporary, root) = root();
        let (path_a, guard_a) = register(
            root.directory_capability().try_clone().unwrap(),
            "locking.sqlite",
        )
        .unwrap();
        let a = open(path_a, false);
        a.execute_batch("CREATE TABLE fixture(value INTEGER);")
            .unwrap();
        let (path_b, guard_b) = register(
            root.directory_capability().try_clone().unwrap(),
            "locking.sqlite",
        )
        .unwrap();
        let b = open(path_b, false);
        b.busy_timeout(std::time::Duration::ZERO).unwrap();
        a.execute_batch("BEGIN IMMEDIATE; INSERT INTO fixture VALUES(1);")
            .unwrap();
        assert_eq!(
            b.execute("INSERT INTO fixture VALUES(2)", [])
                .unwrap_err()
                .sqlite_error_code(),
            Some(ErrorCode::DatabaseBusy)
        );
        a.execute_batch("COMMIT;").unwrap();
        b.execute("INSERT INTO fixture VALUES(2)", []).unwrap();
        assert_eq!(
            a.query_row("SELECT COUNT(*) FROM fixture", [], |row| row
                .get::<_, i64>(0))
                .unwrap(),
            2
        );
        drop(b);
        drop(a);
        drop(guard_b);
        drop(guard_a);
    }

    #[test]
    fn quota_refuses_growth_and_keeps_committed_data_recoverable() {
        let (_temporary, root) = root();
        let policy = StoreDiskPolicy::new(64 * 1024, 64 * 1024, 128 * 1024).unwrap();
        let (path, guard) = register_guest_data(
            root.directory_capability().try_clone().unwrap(),
            "limited.sqlite",
            policy,
            custody(&root),
        )
        .unwrap();
        let connection = open(path, true);
        connection
            .execute_batch("CREATE TABLE fixture(value BLOB);")
            .unwrap();
        let mut committed = 0;
        let mut refused = false;
        for _ in 0..20 {
            match connection.execute("INSERT INTO fixture VALUES(?1)", [vec![42u8; 8192]]) {
                Ok(_) => committed += 1,
                Err(error) => {
                    assert_eq!(error.sqlite_error_code(), Some(ErrorCode::DiskFull));
                    refused = true;
                    break;
                }
            }
        }
        assert!(refused && committed > 0);
        assert_eq!(
            connection
                .query_row("SELECT COUNT(*) FROM fixture", [], |row| row
                    .get::<_, i64>(0))
                .unwrap(),
            committed
        );
        assert!(!guard.is_quarantined());
        drop(connection);
        drop(guard);
        assert!(root.file_len("limited.sqlite").unwrap().unwrap() <= policy.database_bytes());
        let (path, guard) = register_guest_data(
            root.directory_capability().try_clone().unwrap(),
            "limited.sqlite",
            policy,
            custody(&root),
        )
        .unwrap();
        let connection = open(path, true);
        assert_eq!(
            connection
                .query_row("SELECT COUNT(*) FROM fixture", [], |row| row
                    .get::<_, i64>(0))
                .unwrap(),
            committed
        );
        drop(connection);
        drop(guard);
    }

    #[test]
    fn wal_and_shm_metadata_are_truthful_while_open_and_delete_stay_denied() {
        use rusqlite::ffi::*;
        let (_temporary, root) = root();
        let (path, guard) = register_guest_data(
            root.directory_capability().try_clone().unwrap(),
            "probe.sqlite",
            StoreDiskPolicy::default(),
            custody(&root),
        )
        .unwrap();
        for suffix in ["wal", "shm"] {
            let virtual_name = format!("{}-{suffix}", path.to_str().unwrap());
            let native_name = format!("probe.sqlite-{suffix}");
            let c_name = CString::new(virtual_name.clone()).unwrap();
            let wide = virtual_name.encode_utf16().chain([0]).collect::<Vec<_>>();
            unsafe {
                let vfs = sqlite3_vfs_find(c"tobkiri-data-quota".as_ptr());
                assert!(!vfs.is_null());
                let mut exists = -1;
                assert_eq!(
                    (*vfs).xAccess.unwrap()(
                        vfs,
                        c_name.as_ptr(),
                        SQLITE_ACCESS_EXISTS,
                        &mut exists
                    ),
                    SQLITE_OK
                );
                assert_eq!(exists, 0);
                root.write_atomic(&native_name, b"retained recovery fixture")
                    .unwrap();
                assert_eq!(
                    (*vfs).xAccess.unwrap()(
                        vfs,
                        c_name.as_ptr(),
                        SQLITE_ACCESS_EXISTS,
                        &mut exists
                    ),
                    SQLITE_OK
                );
                assert_eq!(exists, 1);
                assert_eq!(
                    cap_create_file(
                        wide.as_ptr(),
                        FILE_READ_DATA,
                        FILE_SHARE_READ | FILE_SHARE_WRITE,
                        std::ptr::null(),
                        OPEN_EXISTING,
                        FILE_ATTRIBUTE_NORMAL,
                        std::ptr::null_mut()
                    ),
                    INVALID_HANDLE_VALUE
                );
                assert_eq!(cap_delete_file(wide.as_ptr()), 0);
                assert!(guest_route(c_name.as_ptr()).is_none());
            }
            assert_eq!(
                root.read(&native_name, 128).unwrap(),
                b"retained recovery fixture"
            );
        }
        drop(guard);
    }

    #[test]
    fn existing_wal_and_unsafe_link_alias_are_rejected_before_open() {
        let (_temporary, root) = root();
        root.write_atomic("unsafe.sqlite-wal", b"existing recovery bytes")
            .unwrap();
        assert!(
            register_guest_data(
                root.directory_capability().try_clone().unwrap(),
                "unsafe.sqlite",
                StoreDiskPolicy::default(),
                custody(&root)
            )
            .is_err()
        );
        assert_eq!(
            root.read("unsafe.sqlite-wal", 100).unwrap(),
            b"existing recovery bytes"
        );
        root.write_atomic("original.sqlite", b"original").unwrap();
        std::fs::hard_link(
            root.path().join("original.sqlite"),
            root.path().join("alias.sqlite"),
        )
        .unwrap();
        assert!(
            register_guest_data(
                root.directory_capability().try_clone().unwrap(),
                "alias.sqlite",
                StoreDiskPolicy::default(),
                custody(&root)
            )
            .is_err()
        );
        assert_eq!(
            std::fs::read(root.path().join("original.sqlite")).unwrap(),
            b"original"
        );
    }
}
