//! DELETE-journal DATA VFS. Normal SQLite I/O/locking is delegated; logical
//! extents are admitted under a short physical-directory quota lock.
use crate::{StoreDiskPolicy, StoreError};
use rusqlite::ffi::*;
#[cfg(unix)]
use std::os::fd::AsRawFd;
#[cfg(all(test, unix))]
use std::os::unix::fs::MetadataExt;
use std::{
    cell::Cell,
    collections::BTreeMap,
    ffi::{CStr, CString, c_char, c_int, c_void},
    fs::File,
    panic::{AssertUnwindSafe, catch_unwind},
    ptr,
    sync::{
        Arc, Mutex, OnceLock, Weak,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
};

pub(crate) const NAME: &str = "tobkiri-data-quota";
const MAX_DOMAINS: usize = 4096;
type DirectoryKey = (u64, u64);
type GroupKey = (DirectoryKey, String);
struct DirectoryLock {
    file: File,
    process: Mutex<()>,
    quarantined: AtomicBool,
    #[cfg(test)]
    fail_unlock: AtomicBool,
}
/// A short native lock is settled before success is published. On an unknown
/// unlock the existing lock and its bounded registry slot remain live, and all
/// groups in this physical directory stop accepting work.
struct DirectoryUnlock<'a> {
    lock: &'a Arc<DirectoryLock>,
    previous: usize,
    finished: bool,
}
impl DirectoryUnlock<'_> {
    fn finish(&mut self) -> Result<(), c_int> {
        if self.finished {
            return Ok(());
        }
        self.finished = true;
        ACTIVE_DIRECTORY.with(|active| active.set(self.previous));
        #[cfg(test)]
        let injected = self.lock.fail_unlock.load(Ordering::Acquire);
        #[cfg(not(test))]
        let injected = false;
        if injected || crate::quota_fs::unlock(&self.lock.file).is_err() {
            if !self.lock.quarantined.swap(true, Ordering::AcqRel) {
                // Cloning an already admitted Arc allocates no new recovery
                // record. Never close/release an uncertain physical lock.
                std::mem::forget(self.lock.clone());
            }
            return Err(SQLITE_IOERR_UNLOCK);
        }
        Ok(())
    }
}
impl Drop for DirectoryUnlock<'_> {
    fn drop(&mut self) {
        // Panic cleanup follows the same quarantine/retention rule.
        let _ = self.finish();
    }
}
/// The issuing process ends this lease explicitly. Closing only its descriptor
/// can leave flock held by a transient fork/exec copy of the open description.
#[derive(Debug)]
struct Lease {
    file: Option<File>,
    process: u32,
    held: bool,
    #[cfg(test)]
    fail_unlock: bool,
}
impl Lease {
    fn release(&mut self) -> c_int {
        if !self.held {
            return SQLITE_OK;
        }
        if self.process != std::process::id() {
            return SQLITE_IOERR_UNLOCK;
        }
        #[cfg(test)]
        if self.fail_unlock {
            return SQLITE_IOERR_UNLOCK;
        }
        let Some(file) = self.file.as_ref() else {
            return SQLITE_IOERR_UNLOCK;
        };
        if crate::quota_fs::unlock(file).is_err() {
            return SQLITE_IOERR_UNLOCK;
        }
        self.held = false;
        SQLITE_OK
    }
}
impl Drop for Lease {
    fn drop(&mut self) {
        // A fork child may close its inherited descriptor but cannot explicitly
        // unlock the issuing parent's still-live open description.
        if self.process == std::process::id() && self.release() != SQLITE_OK {
            // Prepublication/scope cleanup has no SQLite file custody. Retain
            // its existing descriptor on uncertainty, without allocating a
            // recovery owner. Published FileState checks release before Drop
            // and retains the complete preadmitted custody on any failure.
            if let Some(file) = self.file.take() {
                std::mem::forget(file);
            }
        }
    }
}
#[derive(Default)]
struct Registry {
    directories: BTreeMap<DirectoryKey, Weak<DirectoryLock>>,
    groups: BTreeMap<GroupKey, Weak<QuotaDomain>>,
}
fn registry() -> &'static Mutex<Registry> {
    static REGISTRY: OnceLock<Mutex<Registry>> = OnceLock::new();
    REGISTRY.get_or_init(Default::default)
}
thread_local! { static ACTIVE_DIRECTORY: Cell<usize> = const { Cell::new(0) }; }

#[cfg(test)]
#[derive(Clone, Copy)]
enum BusySite {
    DirectoryMutex,
    DirectoryFlock,
    OpenLease,
    DelegatedOpen,
    DelegatedDelete,
    DelegatedClose,
    DelegatedRead,
    DelegatedWrite,
    DelegatedTruncate,
    DelegatedSync,
    DelegatedSize,
    DelegatedLock,
    DelegatedUnlock,
    DelegatedReserved,
    DelegatedControl,
}
#[cfg(test)]
const BUSY_LABELS: [&str; 15] = [
    "directory-mutex",
    "directory-flock",
    "open-lease",
    "delegated-open",
    "delegated-delete",
    "delegated-close",
    "delegated-read",
    "delegated-write",
    "delegated-truncate",
    "delegated-sync",
    "delegated-size",
    "delegated-lock",
    "delegated-unlock",
    "delegated-reserved",
    "delegated-control",
];

// Diagnostics compile out of production, including their site/detail inputs.
// The delegated operation and its exact result are never retried or changed.
macro_rules! delegated {
    ($domain:expr, $site:ident, $detail:expr, $operation:expr) => {{
        let result = $operation;
        #[cfg(test)]
        $domain.note_busy(BusySite::$site, result, $detail);
        result
    }};
}

pub(crate) struct QuotaDomain {
    identity: GroupKey,
    directory: Arc<File>,
    lock: Arc<DirectoryLock>,
    names: [CString; 4],
    leases: [CString; 4],
    policy: Mutex<StoreDiskPolicy>,
    quarantined: AtomicBool,
    // Unknown delegated results retain their admitted extent until this domain
    // is destroyed. A subsequent process measures every exact named file again.
    retained_reservation: AtomicU64,
    process: u32,
    #[cfg(test)]
    busy_sites: AtomicU64,
    #[cfg(test)]
    last_busy: AtomicU64,
}
impl QuotaDomain {
    #[cfg(test)]
    fn note_busy(&self, site: BusySite, result: c_int, detail: c_int) {
        if result & 0xff == SQLITE_BUSY {
            self.busy_sites
                .fetch_or(1 << site as u64, Ordering::Relaxed);
            self.last_busy.store(
                ((detail as u32 as u64) << 32) | (site as u64 + 1),
                Ordering::Relaxed,
            );
        }
    }
    #[cfg(test)]
    fn reset_busy(&self) {
        self.busy_sites.store(0, Ordering::Relaxed);
        self.last_busy.store(0, Ordering::Relaxed);
    }
    #[cfg(test)]
    fn busy_diagnostic(&self) -> (Vec<&'static str>, Option<(&'static str, c_int)>) {
        let sites = self.busy_sites.load(Ordering::Relaxed);
        let last = self.last_busy.load(Ordering::Relaxed);
        (
            BUSY_LABELS
                .iter()
                .enumerate()
                .filter_map(|(index, label)| (sites & (1 << index) != 0).then_some(*label))
                .collect(),
            (last != 0).then(|| {
                (
                    BUSY_LABELS[((last & 0xffff_ffff) - 1) as usize],
                    (last >> 32) as u32 as c_int,
                )
            }),
        )
    }
    /// A SQL/close result with unknown physical state blocks this exact group,
    /// including other routes and later opens against its actual DB identity.
    pub(crate) fn quarantine_connection(&self) {
        self.quarantined.store(true, Ordering::Release);
    }
    pub(crate) fn is_quarantined(&self) -> bool {
        self.quarantined.load(Ordering::Acquire) || self.lock.quarantined.load(Ordering::Acquire)
    }
    pub(crate) fn acquire(
        directory: Arc<File>,
        leaf: &str,
        policy: StoreDiskPolicy,
    ) -> Result<Arc<Self>, StoreError> {
        crate::secure::name(leaf)?;
        #[cfg(windows)]
        crate::secure::windows::validate_storage_component(leaf)?;
        let directory_key = crate::quota_fs::identity(&directory)?;
        let group_key = (directory_key, leaf.to_owned());
        let mut registry = registry()
            .lock()
            .map_err(|_| StoreError::Unavailable("DATA quota registry poisoned".into()))?;
        registry.groups.retain(|_, weak| weak.strong_count() != 0);
        registry
            .directories
            .retain(|_, weak| weak.strong_count() != 0);
        if let Some(domain) = registry.groups.get(&group_key).and_then(Weak::upgrade) {
            drop(registry);
            domain
                .locked(|| {
                    let current = *domain.policy.lock().map_err(|_| SQLITE_IOERR)?;
                    let narrower = current.narrow(policy);
                    if current != narrower {
                        // Do not change the recovery ceiling underneath a live
                        // SQLite handle/transaction. A failed narrowing must leave
                        // the previous handles' rollback capacity intact.
                        let _exclusive = domain.lease(0, true)?;
                        *domain.policy.lock().map_err(|_| SQLITE_IOERR)? = narrower;
                        if let Err(error) = domain.snapshot() {
                            *domain.policy.lock().map_err(|_| SQLITE_IOERR)? = current;
                            return Err(error);
                        }
                    } else {
                        domain.snapshot()?;
                    }
                    Ok(())
                })
                .map_err(store_error)?;
            return Ok(domain);
        }
        if registry.groups.len() >= MAX_DOMAINS || registry.directories.len() >= MAX_DOMAINS {
            return Err(StoreError::Quota);
        }
        let lock = match registry
            .directories
            .get(&directory_key)
            .and_then(Weak::upgrade)
        {
            Some(lock) => lock,
            None => {
                let lock = Arc::new(DirectoryLock {
                    file: crate::quota_fs::directory_lock(&directory)?,
                    process: Mutex::new(()),
                    quarantined: AtomicBool::new(false),
                    #[cfg(test)]
                    fail_unlock: AtomicBool::new(false),
                });
                registry
                    .directories
                    .insert(directory_key, Arc::downgrade(&lock));
                lock
            }
        };
        let names = [
            leaf.to_owned(),
            format!("{leaf}-journal"),
            format!("{leaf}-wal"),
            format!("{leaf}-shm"),
        ]
        .map(|name| CString::new(name).map_err(|_| StoreError::Invalid("NUL in DATA leaf".into())));
        let [main, journal, wal, shm] = names;
        let digest = tobkiri_canon::Digest::hash(leaf.as_bytes());
        let digest = digest.hex();
        let leases = [0, 1, 2, 3].map(|kind| {
            CString::new(format!(".tobkiri-quota-{digest}-{kind}.lease"))
                .expect("fixed ASCII lease name")
        });
        let domain = Arc::new(Self {
            identity: group_key.clone(),
            directory,
            lock,
            names: [main?, journal?, wal?, shm?],
            leases,
            policy: Mutex::new(policy),
            quarantined: AtomicBool::new(false),
            retained_reservation: AtomicU64::new(0),
            process: std::process::id(),
            #[cfg(test)]
            busy_sites: AtomicU64::new(0),
            #[cfg(test)]
            last_busy: AtomicU64::new(0),
        });
        domain
            .locked(|| domain.snapshot().map(|_| ()))
            .map_err(store_error)?;
        registry.groups.insert(group_key, Arc::downgrade(&domain));
        Ok(domain)
    }
    fn locked<T>(&self, operation: impl FnOnce() -> Result<T, c_int>) -> Result<T, c_int> {
        if self.process != std::process::id() || self.is_quarantined() {
            return Err(SQLITE_IOERR);
        }
        let identity = Arc::as_ptr(&self.lock) as usize;
        if ACTIVE_DIRECTORY.with(|active| active.get() == identity) {
            return operation();
        }
        let _process = self.lock.process.try_lock().map_err(|error| match error {
            std::sync::TryLockError::WouldBlock => {
                #[cfg(test)]
                self.note_busy(BusySite::DirectoryMutex, SQLITE_BUSY, 0);
                SQLITE_BUSY
            }
            std::sync::TryLockError::Poisoned(_) => {
                self.quarantined.store(true, Ordering::Release);
                SQLITE_IOERR
            }
        })?;
        // Another group can have quarantined this directory between the first
        // check and acquisition of the process mutex.
        if self.is_quarantined() {
            return Err(SQLITE_IOERR);
        }
        if let Err(error) = crate::quota_fs::lock(&self.lock.file, true) {
            return if error.kind() == std::io::ErrorKind::WouldBlock {
                #[cfg(test)]
                self.note_busy(BusySite::DirectoryFlock, SQLITE_BUSY, 0);
                Err(SQLITE_BUSY)
            } else {
                self.quarantined.store(true, Ordering::Release);
                Err(SQLITE_IOERR_LOCK)
            };
        }
        let previous = ACTIVE_DIRECTORY.with(|active| active.replace(identity));
        let mut unlock = DirectoryUnlock {
            lock: &self.lock,
            previous,
            finished: false,
        };
        let result = operation();
        unlock.finish()?;
        result
    }
    fn stat(&self, kind: usize) -> Result<Option<crate::quota_fs::Stat>, c_int> {
        crate::quota_fs::stat(&self.directory, &self.names[kind])
    }
    fn snapshot(&self) -> Result<[u64; 4], c_int> {
        let mut extents = [0; 4];
        for (kind, extent) in extents.iter_mut().enumerate() {
            if let Some(stat) = self.stat(kind)? {
                if kind >= 2 {
                    return Err(SQLITE_CANTOPEN);
                } // WAL/SHM are not admitted by this backend.
                *extent = stat.st_size as u64;
            }
        }
        self.check(extents)?;
        Ok(extents)
    }
    fn check(&self, extents: [u64; 4]) -> Result<(), c_int> {
        let policy = *self.policy.lock().map_err(|_| SQLITE_IOERR)?;
        let charge = extents[0]
            .checked_add(extents[1].max(policy.journal_bytes()))
            .ok_or(SQLITE_FULL)?;
        if extents[0] > policy.database_bytes()
            || extents[1] > policy.journal_bytes()
            || charge > policy.aggregate_bytes()
        {
            return Err(SQLITE_FULL);
        }
        Ok(())
    }
    fn lease(&self, kind: usize, exclusive: bool) -> Result<Lease, c_int> {
        let file = crate::quota_fs::open_lease(&self.directory, &self.leases[kind])
            .map_err(|_| SQLITE_CANTOPEN)?;
        if crate::quota_fs::lock(&file, exclusive).is_err() {
            #[cfg(test)]
            self.note_busy(
                BusySite::OpenLease,
                SQLITE_BUSY,
                (kind as c_int)
                    | ((exclusive as c_int) << 8)
                    | (std::io::Error::last_os_error().raw_os_error().unwrap_or(0) << 16),
            );
            return Err(SQLITE_BUSY);
        }
        Ok(Lease {
            file: Some(file),
            process: std::process::id(),
            held: true,
            #[cfg(test)]
            fail_unlock: false,
        })
    }
    pub(crate) fn unlink(&self, name: &CStr) -> Result<c_int, c_int> {
        let kind = self.kind(name).ok_or(SQLITE_CANTOPEN)?;
        self.locked(|| {
            let before = self.snapshot()?;
            let _exclusive = self.lease(kind, true)?;
            let result = crate::quota_fs::unlink(&self.directory, name);
            match self.stat(kind) {
                Ok(None) => Ok(result),
                Ok(Some(stat)) if result != 0 && stat.st_size as u64 == before[kind] => Ok(result),
                _ => {
                    self.quarantine(before[kind]);
                    Err(SQLITE_IOERR_DELETE)
                }
            }
        })
    }
    fn kind(&self, name: &CStr) -> Option<usize> {
        self.names
            .iter()
            .position(|candidate| candidate.as_c_str() == name)
    }
    fn quarantine(&self, extent: u64) {
        self.retained_reservation
            .fetch_max(extent, Ordering::AcqRel);
        self.quarantined.store(true, Ordering::Release);
    }
    pub(crate) fn with_exclusive_transfer<T>(
        &self,
        target: &Self,
        run: impl FnOnce(&mut TransferFiles<'_>) -> Result<T, StoreError>,
    ) -> Result<T, StoreError> {
        if self.identity == target.identity {
            return Err(StoreError::Conflict(
                "DATA transfer source equals target".into(),
            ));
        }
        let (first, second) = if self.identity < target.identity {
            (self, target)
        } else {
            (target, self)
        };
        first
            .locked(|| {
                second.locked(|| {
                    // Lease inodes are distinct from database inodes. Exclusivity
                    // proves that no SQLite handle or unknown xClose retains this group.
                    let _source_leases = [
                        self.lease(0, true)?,
                        self.lease(1, true)?,
                        self.lease(2, true)?,
                        self.lease(3, true)?,
                    ];
                    let _target_leases = [
                        target.lease(0, true)?,
                        target.lease(1, true)?,
                        target.lease(2, true)?,
                        target.lease(3, true)?,
                    ];
                    self.snapshot()?;
                    target.snapshot()?;
                    for kind in 0..4 {
                        if target.stat(kind)?.is_some() {
                            return Err(SQLITE_BUSY);
                        }
                    }
                    let original = [
                        self.stat(0)?.map(FileIdentity::from),
                        self.stat(1)?.map(FileIdentity::from),
                    ];
                    if original[0].is_none() {
                        return Err(SQLITE_CANTOPEN);
                    }
                    let mut files = TransferFiles {
                        source: self,
                        target,
                        original,
                        moved: [false; 2],
                        settled: false,
                    };
                    Ok(run(&mut files))
                })
            })
            .map_err(store_error)?
    }
    fn mutate(
        &self,
        state: &FileState,
        target: u64,
        truncate: bool,
        delegate: impl FnOnce() -> c_int,
    ) -> c_int {
        self.locked(|| {
            let mut extents = self.snapshot()?;
            let before = unsafe { file_size(state.inner) }?;
            if before != extents[state.kind] {
                self.quarantine(target);
                return Err(SQLITE_IOERR_FSTAT);
            }
            extents[state.kind] = if truncate { target } else { before.max(target) };
            self.check(extents)?; // Reservation held by the physical lock through delegated I/O.
            let result = delegate();
            #[cfg(test)]
            self.note_busy(
                if truncate {
                    BusySite::DelegatedTruncate
                } else {
                    BusySite::DelegatedWrite
                },
                result,
                state.kind as c_int,
            );
            let after = match unsafe { file_size(state.inner) } {
                Ok(after) => after,
                Err(error) => {
                    self.quarantine(extents[state.kind]);
                    return Err(error);
                }
            };
            extents[state.kind] = after;
            if self.check(extents).is_err() {
                self.quarantine(after);
                return Err(SQLITE_IOERR);
            }
            // A failed/partial operation is charged at its actual extent.
            Ok(result)
        })
        .unwrap_or_else(|error| error)
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
struct FileIdentity {
    device: u64,
    inode: u64,
}
impl From<crate::quota_fs::Stat> for FileIdentity {
    fn from(stat: crate::quota_fs::Stat) -> Self {
        Self {
            device: stat.st_dev as u64,
            inode: stat.st_ino as u64,
        }
    }
}
/// The two admitted DELETE-mode files, under both physical locks and all four
/// kinds' exclusive leases. No database descriptor is opened by this move.
pub(crate) struct TransferFiles<'a> {
    source: &'a QuotaDomain,
    target: &'a QuotaDomain,
    original: [Option<FileIdentity>; 2],
    moved: [bool; 2],
    settled: bool,
}
impl TransferFiles<'_> {
    pub(crate) fn file_identities(&self) -> [Option<(u64, u64)>; 2] {
        self.original
            .map(|identity| identity.map(|identity| (identity.device, identity.inode)))
    }
    fn matches(&self, kind: usize, moved: bool) -> Result<bool, StoreError> {
        let source = self
            .source
            .stat(kind)
            .map_err(store_error)?
            .map(FileIdentity::from);
        let target = self
            .target
            .stat(kind)
            .map_err(store_error)?
            .map(FileIdentity::from);
        Ok(if moved {
            source.is_none() && target == self.original[kind]
        } else {
            source == self.original[kind] && target.is_none()
        })
    }
    fn rename(&self, kind: usize, forward: bool) -> Result<(), StoreError> {
        let (from, to) = if forward {
            (self.source, self.target)
        } else {
            (self.target, self.source)
        };
        #[cfg(target_os = "macos")]
        let result = unsafe {
            libc::renameatx_np(
                from.directory.as_raw_fd(),
                from.names[kind].as_ptr(),
                to.directory.as_raw_fd(),
                to.names[kind].as_ptr(),
                libc::RENAME_EXCL,
            )
        };
        #[cfg(target_os = "linux")]
        let result = unsafe {
            libc::renameat2(
                from.directory.as_raw_fd(),
                from.names[kind].as_ptr(),
                to.directory.as_raw_fd(),
                to.names[kind].as_ptr(),
                libc::RENAME_NOREPLACE,
            )
        };
        #[cfg(not(any(target_os = "macos", target_os = "linux")))]
        let result = -1;
        if result != 0 {
            return Err(std::io::Error::last_os_error().into());
        }
        if forward {
            crate::data_moves::checkpoint(if kind == 0 {
                "rename-main"
            } else {
                "rename-journal"
            });
        }
        from.directory.sync_all()?;
        to.directory.sync_all()?;
        Ok(())
    }
    pub(crate) fn quarantine(&self) {
        self.source.quarantine_connection();
        self.target.quarantine_connection();
    }
    fn unknown(&self) -> StoreError {
        self.quarantine();
        StoreError::CommitUnknown
    }
    pub(crate) fn move_to_target(&mut self) -> Result<(), StoreError> {
        for kind in 0..2 {
            if self.original[kind].is_none() {
                continue;
            }
            let result = self.rename(kind, true);
            if self.matches(kind, true).unwrap_or(false) {
                self.moved[kind] = true;
            } else if !self.matches(kind, false).unwrap_or(false) {
                return Err(self.unknown());
            }
            if let Err(error) = result {
                self.rollback_to_source()?;
                return Err(error);
            }
            if !self.moved[kind] {
                return Err(self.unknown());
            }
        }
        crate::data_moves::checkpoint("dir-synced");
        Ok(())
    }
    pub(crate) fn rollback_to_source(&mut self) -> Result<(), StoreError> {
        for kind in [1, 0] {
            if self.moved[kind] {
                let result = self.rename(kind, false);
                if !self.matches(kind, false).unwrap_or(false) || result.is_err() {
                    return Err(self.unknown());
                }
                self.moved[kind] = false;
            }
        }
        if (0..2).any(|kind| !self.matches(kind, false).unwrap_or(false)) {
            return Err(self.unknown());
        }
        self.settled = true;
        Ok(())
    }
    pub(crate) fn accept_target(&mut self) -> Result<(), StoreError> {
        for kind in 0..2 {
            if !self
                .matches(kind, self.original[kind].is_some())
                .unwrap_or(false)
            {
                return Err(self.unknown());
            }
        }
        self.settled = true;
        Ok(())
    }
}
impl Drop for TransferFiles<'_> {
    fn drop(&mut self) {
        if !self.settled && self.moved.iter().any(|moved| *moved) {
            self.quarantine();
        }
    }
}
fn store_error(code: c_int) -> StoreError {
    match code {
        SQLITE_FULL => StoreError::Quota,
        SQLITE_BUSY => StoreError::Conflict("DATA quota domain is busy".into()),
        _ => StoreError::Unavailable("DATA quota group is unsafe or unsupported".into()),
    }
}
fn callback(operation: impl FnOnce() -> c_int) -> c_int {
    catch_unwind(AssertUnwindSafe(operation)).unwrap_or(SQLITE_IOERR)
}
#[repr(C)]
struct Header {
    file: sqlite3_file,
    state: *mut FileState,
}
struct FileState {
    inner: *mut sqlite3_file,
    domain: Arc<QuotaDomain>,
    kind: usize,
    _lease: Lease,
    _custody: Arc<crate::data_custody::DataCustody>,
}
unsafe fn state<'a>(file: *mut sqlite3_file) -> &'a FileState {
    unsafe { &*(*(file.cast::<Header>())).state }
}
unsafe fn methods<'a>(inner: *mut sqlite3_file) -> &'a sqlite3_io_methods {
    unsafe { &*(*inner).pMethods }
}
unsafe fn file_size(inner: *mut sqlite3_file) -> Result<u64, c_int> {
    let mut size = -1;
    let result = unsafe { methods(inner).xFileSize.ok_or(SQLITE_IOERR_FSTAT)?(inner, &mut size) };
    if result != SQLITE_OK || size < 0 {
        return Err(SQLITE_IOERR_FSTAT);
    }
    Ok(size as u64)
}
unsafe fn parent(vfs: *mut sqlite3_vfs) -> *mut sqlite3_vfs {
    unsafe { (*vfs).pAppData.cast() }
}

unsafe extern "C" fn open(
    vfs: *mut sqlite3_vfs,
    name: *const c_char,
    file: *mut sqlite3_file,
    flags: c_int,
    out_flags: *mut c_int,
) -> c_int {
    callback(|| unsafe {
        (*file).pMethods = ptr::null();
        if name.is_null()
            || flags & SQLITE_OPEN_DELETEONCLOSE != 0
            || flags & (SQLITE_OPEN_MAIN_DB | SQLITE_OPEN_MAIN_JOURNAL) == 0
        {
            return SQLITE_CANTOPEN;
        }
        let Some((domain, leaf, custody)) = crate::vfs::guest_route(name) else {
            return SQLITE_CANTOPEN;
        };
        let Some(kind) = domain.kind(&leaf) else {
            return SQLITE_CANTOPEN;
        };
        if kind > 1 {
            return SQLITE_CANTOPEN;
        }
        domain
            .locked(|| {
                domain.snapshot()?;
                // A separate zero-byte lease inode avoids closing an extra database
                // descriptor, which would release Unix's process-wide POSIX locks.
                let lease = domain.lease(kind, false)?;
                let inner = file
                    .cast::<u8>()
                    .add(std::mem::size_of::<Header>())
                    .cast::<sqlite3_file>();
                (*inner).pMethods = ptr::null();
                let parent = parent(vfs);
                let result = delegated!(
                    domain,
                    DelegatedOpen,
                    flags,
                    (*parent).xOpen.ok_or(SQLITE_CANTOPEN)?(parent, name, inner, flags, out_flags)
                );
                if result != SQLITE_OK {
                    if !(*inner).pMethods.is_null() {
                        close_inner_or_retain(Box::new(FileState {
                            inner,
                            domain: domain.clone(),
                            kind,
                            _lease: lease,
                            _custody: custody.clone(),
                        }));
                    }
                    return Err(result);
                }
                let checked = domain.snapshot().and_then(|extents| {
                    if file_size(inner)? != extents[kind] {
                        return Err(SQLITE_IOERR_FSTAT);
                    }
                    if kind == 0 && extents[kind] != 0 {
                        if extents[kind] < 100 {
                            return Err(SQLITE_CANTOPEN);
                        }
                        let mut header = [0u8; 100];
                        let result = methods(inner).xRead.ok_or(SQLITE_IOERR_READ)?(
                            inner,
                            header.as_mut_ptr().cast(),
                            100,
                            0,
                        );
                        if result != SQLITE_OK
                            || &header[..16] != b"SQLite format 3\0"
                            || (header[18], header[19]) != (1, 1)
                        {
                            return Err(SQLITE_CANTOPEN);
                        }
                    }
                    Ok(())
                });
                if let Err(error) = checked {
                    close_inner_or_retain(Box::new(FileState {
                        inner,
                        domain: domain.clone(),
                        kind,
                        _lease: lease,
                        _custody: custody.clone(),
                    }));
                    return Err(error);
                }
                let state = Box::new(FileState {
                    inner,
                    _custody: custody,
                    domain: domain.clone(),
                    kind,
                    _lease: lease,
                });
                ptr::write(
                    file.cast::<Header>(),
                    Header {
                        file: sqlite3_file { pMethods: &METHODS },
                        state: Box::into_raw(state),
                    },
                );
                Ok(SQLITE_OK)
            })
            .unwrap_or_else(|error| error)
    })
}
unsafe extern "C" fn delete(vfs: *mut sqlite3_vfs, name: *const c_char, sync: c_int) -> c_int {
    callback(|| unsafe {
        let Some((domain, _, _)) = crate::vfs::guest_route(name) else {
            return SQLITE_CANTOPEN;
        };
        domain
            .locked(|| {
                domain.snapshot()?;
                let parent = parent(vfs);
                Ok(delegated!(
                    domain,
                    DelegatedDelete,
                    sync,
                    (*parent).xDelete.ok_or(SQLITE_IOERR_DELETE)?(parent, name, sync)
                ))
            })
            .unwrap_or_else(|error| error)
    })
}
/// Close exactly once. Unknown native destruction retains the actual custody
/// and open-file lease; no later drop may refund their Core/owner resources.
unsafe fn close_inner_or_retain(mut state: Box<FileState>) -> c_int {
    let mut result = delegated!(state.domain, DelegatedClose, state.kind as c_int, unsafe {
        methods(state.inner)
            .xClose
            .map(|close| close(state.inner))
            .unwrap_or(SQLITE_IOERR_CLOSE)
    });
    if result == SQLITE_OK {
        // Only confirmed physical SQLite close may end its lease. An unknown
        // unlock retains the original complete state/control/owner custody.
        result = state._lease.release();
    }
    if result == SQLITE_OK {
        drop(state);
    } else {
        state.domain.quarantine(0);
        let _ = Box::into_raw(state);
    }
    result
}
unsafe extern "C" fn close(file: *mut sqlite3_file) -> c_int {
    callback(|| unsafe {
        let header = &mut *file.cast::<Header>();
        let state = Box::from_raw(header.state);
        header.state = ptr::null_mut();
        header.file.pMethods = ptr::null();
        close_inner_or_retain(state)
    })
}
unsafe extern "C" fn read(
    file: *mut sqlite3_file,
    data: *mut c_void,
    amount: c_int,
    offset: i64,
) -> c_int {
    callback(|| unsafe {
        let s = state(file);
        delegated!(
            s.domain,
            DelegatedRead,
            s.kind as c_int,
            methods(s.inner)
                .xRead
                .map(|read| read(s.inner, data, amount, offset))
                .unwrap_or(SQLITE_IOERR_READ)
        )
    })
}
unsafe extern "C" fn write(
    file: *mut sqlite3_file,
    data: *const c_void,
    amount: c_int,
    offset: i64,
) -> c_int {
    callback(|| unsafe {
        let s = state(file);
        let Some(target) = u64::try_from(offset).ok().and_then(|offset| {
            u64::try_from(amount)
                .ok()
                .and_then(|amount| offset.checked_add(amount))
        }) else {
            return SQLITE_FULL;
        };
        s.domain.mutate(s, target, false, || {
            methods(s.inner)
                .xWrite
                .map(|write| write(s.inner, data, amount, offset))
                .unwrap_or(SQLITE_IOERR_WRITE)
        })
    })
}
unsafe extern "C" fn truncate(file: *mut sqlite3_file, size: i64) -> c_int {
    callback(|| unsafe {
        let s = state(file);
        let Ok(target) = u64::try_from(size) else {
            return SQLITE_FULL;
        };
        s.domain.mutate(s, target, true, || {
            methods(s.inner)
                .xTruncate
                .map(|truncate| truncate(s.inner, size))
                .unwrap_or(SQLITE_IOERR_TRUNCATE)
        })
    })
}
unsafe extern "C" fn sync(file: *mut sqlite3_file, flags: c_int) -> c_int {
    callback(|| unsafe {
        let s = state(file);
        delegated!(
            s.domain,
            DelegatedSync,
            flags,
            methods(s.inner)
                .xSync
                .map(|sync| sync(s.inner, flags))
                .unwrap_or(SQLITE_IOERR_FSYNC)
        )
    })
}
unsafe extern "C" fn size(file: *mut sqlite3_file, out: *mut i64) -> c_int {
    callback(|| unsafe {
        let s = state(file);
        delegated!(
            s.domain,
            DelegatedSize,
            s.kind as c_int,
            methods(s.inner)
                .xFileSize
                .map(|size| size(s.inner, out))
                .unwrap_or(SQLITE_IOERR_FSTAT)
        )
    })
}
unsafe extern "C" fn lock(file: *mut sqlite3_file, flags: c_int) -> c_int {
    callback(|| unsafe {
        let s = state(file);
        delegated!(
            s.domain,
            DelegatedLock,
            flags,
            methods(s.inner)
                .xLock
                .map(|lock| lock(s.inner, flags))
                .unwrap_or(SQLITE_IOERR_LOCK)
        )
    })
}
unsafe extern "C" fn unlock(file: *mut sqlite3_file, flags: c_int) -> c_int {
    callback(|| unsafe {
        let s = state(file);
        delegated!(
            s.domain,
            DelegatedUnlock,
            flags,
            methods(s.inner)
                .xUnlock
                .map(|unlock| unlock(s.inner, flags))
                .unwrap_or(SQLITE_IOERR_UNLOCK)
        )
    })
}
unsafe extern "C" fn reserved(file: *mut sqlite3_file, out: *mut c_int) -> c_int {
    callback(|| unsafe {
        let s = state(file);
        delegated!(
            s.domain,
            DelegatedReserved,
            s.kind as c_int,
            methods(s.inner)
                .xCheckReservedLock
                .map(|check| check(s.inner, out))
                .unwrap_or(SQLITE_IOERR_CHECKRESERVEDLOCK)
        )
    })
}
unsafe extern "C" fn control(
    file: *mut sqlite3_file,
    operation: c_int,
    argument: *mut c_void,
) -> c_int {
    callback(|| unsafe {
        let s = state(file);
        match operation {
            // Hints must not cause delegated fallocate/ftruncate or chunk growth.
            SQLITE_FCNTL_SIZE_HINT | SQLITE_FCNTL_CHUNK_SIZE => SQLITE_OK,
            SQLITE_FCNTL_MMAP_SIZE => {
                if !argument.is_null() {
                    *argument.cast::<i64>() = 0;
                }
                SQLITE_OK
            }
            SQLITE_FCNTL_LOCKSTATE
            | SQLITE_FCNTL_LAST_ERRNO
            | SQLITE_FCNTL_VFSNAME
            | SQLITE_FCNTL_HAS_MOVED
            | SQLITE_FCNTL_POWERSAFE_OVERWRITE => delegated!(
                s.domain,
                DelegatedControl,
                operation,
                methods(s.inner)
                    .xFileControl
                    .map(|control| control(s.inner, operation, argument))
                    .unwrap_or(SQLITE_NOTFOUND)
            ),
            _ => SQLITE_NOTFOUND,
        }
    })
}
unsafe extern "C" fn sector(file: *mut sqlite3_file) -> c_int {
    callback(|| unsafe {
        let s = state(file);
        methods(s.inner)
            .xSectorSize
            .map(|sector| sector(s.inner))
            .unwrap_or(4096)
    })
}
unsafe extern "C" fn characteristics(_: *mut sqlite3_file) -> c_int {
    0
}
static METHODS: sqlite3_io_methods = sqlite3_io_methods {
    iVersion: 1,
    xClose: Some(close),
    xRead: Some(read),
    xWrite: Some(write),
    xTruncate: Some(truncate),
    xSync: Some(sync),
    xFileSize: Some(size),
    xLock: Some(lock),
    xUnlock: Some(unlock),
    xCheckReservedLock: Some(reserved),
    xFileControl: Some(control),
    xSectorSize: Some(sector),
    xDeviceCharacteristics: Some(characteristics),
    xShmMap: None,
    xShmLock: None,
    xShmBarrier: None,
    xShmUnmap: None,
    xFetch: None,
    xUnfetch: None,
};

// All VFS-level delegates receive the original VFS and its original pAppData.
unsafe extern "C" fn access(
    vfs: *mut sqlite3_vfs,
    name: *const c_char,
    flags: c_int,
    out: *mut c_int,
) -> c_int {
    callback(|| unsafe {
        let p = parent(vfs);
        (*p).xAccess
            .map(|f| f(p, name, flags, out))
            .unwrap_or(SQLITE_IOERR_ACCESS)
    })
}
unsafe extern "C" fn fullpath(
    vfs: *mut sqlite3_vfs,
    name: *const c_char,
    size: c_int,
    out: *mut c_char,
) -> c_int {
    callback(|| unsafe {
        let p = parent(vfs);
        (*p).xFullPathname
            .map(|f| f(p, name, size, out))
            .unwrap_or(SQLITE_CANTOPEN)
    })
}
unsafe extern "C" fn randomness(vfs: *mut sqlite3_vfs, size: c_int, out: *mut c_char) -> c_int {
    callback(|| unsafe {
        let p = parent(vfs);
        (*p).xRandomness.map(|f| f(p, size, out)).unwrap_or(0)
    })
}
unsafe extern "C" fn sleep(vfs: *mut sqlite3_vfs, micros: c_int) -> c_int {
    callback(|| unsafe {
        let p = parent(vfs);
        (*p).xSleep.map(|f| f(p, micros)).unwrap_or(0)
    })
}
unsafe extern "C" fn time(vfs: *mut sqlite3_vfs, out: *mut f64) -> c_int {
    callback(|| unsafe {
        let p = parent(vfs);
        (*p).xCurrentTime.map(|f| f(p, out)).unwrap_or(SQLITE_IOERR)
    })
}
unsafe extern "C" fn time64(vfs: *mut sqlite3_vfs, out: *mut i64) -> c_int {
    callback(|| unsafe {
        let p = parent(vfs);
        (*p).xCurrentTimeInt64
            .map(|f| f(p, out))
            .unwrap_or(SQLITE_IOERR)
    })
}
unsafe extern "C" fn last_error(vfs: *mut sqlite3_vfs, size: c_int, out: *mut c_char) -> c_int {
    callback(|| unsafe {
        let p = parent(vfs);
        (*p).xGetLastError.map(|f| f(p, size, out)).unwrap_or(0)
    })
}

pub(crate) unsafe fn install(parent: *mut sqlite3_vfs) -> Result<(), String> {
    let header =
        c_int::try_from(std::mem::size_of::<Header>()).map_err(|_| "DATA VFS header overflow")?;
    let size = unsafe { (*parent).szOsFile }
        .checked_add(header)
        .ok_or("DATA VFS file overflow")?;
    let mut vfs = Box::new(unsafe { *parent });
    vfs.szOsFile = size;
    vfs.zName = c"tobkiri-data-quota".as_ptr();
    vfs.pNext = ptr::null_mut();
    vfs.pAppData = parent.cast();
    vfs.xOpen = Some(open);
    vfs.xDelete = Some(delete);
    vfs.xAccess = Some(access);
    vfs.xFullPathname = Some(fullpath);
    vfs.xDlOpen = None;
    vfs.xDlError = None;
    vfs.xDlSym = None;
    vfs.xDlClose = None;
    vfs.xRandomness = Some(randomness);
    vfs.xSleep = Some(sleep);
    vfs.xCurrentTime = Some(time);
    vfs.xGetLastError = Some(last_error);
    vfs.xCurrentTimeInt64 = Some(time64);
    vfs.xSetSystemCall = None;
    vfs.xGetSystemCall = None;
    vfs.xNextSystemCall = None;
    if unsafe { sqlite3_vfs_register(&mut *vfs, 0) } != SQLITE_OK {
        return Err("DATA quota VFS registration failed".into());
    }
    // SQLite retains the registered pointer for process lifetime, like its
    // built-in VFS. No raw-pointer Send/Sync assertion is introduced.
    let _ = Box::into_raw(vfs);
    Ok(())
}

#[cfg(test)]
mod directory_lock_tests {
    use super::*;

    #[test]
    fn unknown_unlock_retains_lock_and_rejects_every_group() {
        const FIXTURE: &str = "TOBKIRI_QUOTA_UNLOCK_FIXTURE";
        const MODE: &str = "TOBKIRI_QUOTA_UNLOCK_MODE";
        if let Some(path) = std::env::var_os(FIXTURE) {
            let root = crate::DataRoot::open(path).unwrap();
            let directory = root.directory_capability();
            let policy = StoreDiskPolicy::new(64 * 1024, 64 * 1024, 128 * 1024).unwrap();
            let first = QuotaDomain::acquire(directory.clone(), "first.sqlite", policy).unwrap();
            let sibling = QuotaDomain::acquire(directory.clone(), "second.sqlite", policy).unwrap();
            let observer = crate::quota_fs::directory_lock(&directory).unwrap();
            let retained = Arc::downgrade(&first.lock);
            first.lock.fail_unlock.store(true, Ordering::Release);
            if std::env::var(MODE).unwrap() == "panic" {
                assert!(
                    catch_unwind(AssertUnwindSafe(|| {
                        let _ = first.locked::<()>(|| panic!("injected operation panic"));
                    }))
                    .is_err()
                );
            } else {
                let ran = Cell::new(false);
                assert_eq!(
                    first.locked(|| {
                        ran.set(true);
                        Ok(73)
                    }),
                    Err(SQLITE_IOERR_UNLOCK)
                );
                assert!(ran.get());
            }
            assert!(first.is_quarantined() && sibling.is_quarantined());
            let ran = Cell::new(false);
            assert_eq!(
                sibling.locked(|| {
                    ran.set(true);
                    Ok(())
                }),
                Err(SQLITE_IOERR)
            );
            assert!(!ran.get());
            assert!(QuotaDomain::acquire(directory.clone(), "later.sqlite", policy).is_err());
            drop(sibling);
            drop(first);
            assert!(retained.upgrade().is_some());
            assert_eq!(
                crate::quota_fs::lock(&observer, true).unwrap_err().kind(),
                std::io::ErrorKind::WouldBlock
            );
            // The child exits with unknown custody retained. Only OS process
            // destruction may end that lock; the parent then checks release.
            return;
        }
        let temporary = tempfile::Builder::new()
            .prefix(".quota-unlock-")
            .tempdir_in(std::env::current_dir().unwrap())
            .unwrap();
        for mode in ["success", "panic"] {
            let path = temporary.path().join(mode);
            let child = std::process::Command::new(std::env::current_exe().unwrap())
                .args(["--exact", "quota_vfs::directory_lock_tests::unknown_unlock_retains_lock_and_rejects_every_group", "--nocapture"])
                .env(FIXTURE, &path).env(MODE, mode).output().unwrap();
            assert!(
                child.status.success(),
                "{}\n{}",
                String::from_utf8_lossy(&child.stdout),
                String::from_utf8_lossy(&child.stderr)
            );
            let root = crate::DataRoot::open(path).unwrap();
            let lock = crate::quota_fs::directory_lock(&root.directory_capability()).unwrap();
            crate::quota_fs::lock(&lock, true).unwrap();
            crate::quota_fs::unlock(&lock).unwrap();
        }
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::{
        io::{BufRead, Read, Write},
        process::{Command, Stdio},
    };

    fn domain(path: &std::path::Path, policy: StoreDiskPolicy) -> Arc<QuotaDomain> {
        QuotaDomain::acquire(Arc::new(File::open(path).unwrap()), "data.sqlite", policy).unwrap()
    }
    fn small_policy() -> StoreDiskPolicy {
        StoreDiskPolicy::new(64 * 1024, 64 * 1024, 128 * 1024).unwrap()
    }
    fn test_custody() -> Arc<crate::data_custody::DataCustody> {
        let control = tobkiri_gate::CoreControl::default();
        crate::data_custody::DataCustody::registered(
            None,
            control
                .reserve(tobkiri_gate::ControlKind::Registration, 64 * 1024)
                .unwrap(),
        )
    }

    #[test]
    fn directory_lock_contention_is_busy_but_poison_quarantines() {
        let temp = tempfile::tempdir().unwrap();
        let domain = domain(temp.path(), small_policy());
        let guard = domain.lock.process.lock().unwrap();
        assert_eq!(domain.locked(|| Ok(())).unwrap_err(), SQLITE_BUSY);
        assert!(!domain.quarantined.load(Ordering::Acquire));
        drop(guard);
        let _ = catch_unwind(AssertUnwindSafe(|| {
            let _guard = domain.lock.process.lock().unwrap();
            panic!("inject quota lock poison");
        }));
        let called = Cell::new(false);
        assert_eq!(
            domain.locked(|| {
                called.set(true);
                Ok(())
            }),
            Err(SQLITE_IOERR)
        );
        assert!(!called.get());
        assert!(domain.quarantined.load(Ordering::Acquire));
        assert_eq!(domain.locked(|| Ok(())), Err(SQLITE_IOERR));
    }

    #[test]
    fn exact_sidecars_are_counted_and_wal_is_preserved() {
        let temp = tempfile::tempdir().unwrap();
        std::fs::write(temp.path().join("data.sqlite"), [0; 10]).unwrap();
        std::fs::write(temp.path().join("data.sqlite-journal"), [0; 20]).unwrap();
        let domain = domain(temp.path(), small_policy());
        assert_eq!(domain.locked(|| domain.snapshot()).unwrap(), [10, 20, 0, 0]);
        std::fs::write(temp.path().join("data.sqlite-wal"), [7; 40]).unwrap();
        assert_eq!(
            domain.locked(|| domain.snapshot()).unwrap_err(),
            SQLITE_CANTOPEN
        );
        assert_eq!(
            std::fs::read(temp.path().join("data.sqlite-wal")).unwrap(),
            [7; 40]
        );
    }

    #[test]
    fn open_custody_denies_unlink_until_final_close() {
        let temp = tempfile::tempdir().unwrap();
        std::fs::write(temp.path().join("data.sqlite"), [0; 10]).unwrap();
        let domain = domain(temp.path(), small_policy());
        let lease = domain.locked(|| domain.lease(0, false)).unwrap();
        assert_eq!(domain.unlink(c"data.sqlite").unwrap_err(), SQLITE_BUSY);
        assert!(temp.path().join("data.sqlite").exists());
        drop(lease);
        assert_eq!(domain.unlink(c"data.sqlite").unwrap(), 0);
        assert!(!temp.path().join("data.sqlite").exists());
    }

    #[repr(C)]
    struct FakeFile {
        file: sqlite3_file,
        backing: File,
        fail_measure: bool,
        close_result: c_int,
        close_calls: usize,
    }
    unsafe extern "C" fn fake_size(file: *mut sqlite3_file, out: *mut i64) -> c_int {
        let fake = unsafe { &*file.cast::<FakeFile>() };
        if fake.fail_measure {
            return SQLITE_IOERR_FSTAT;
        }
        match fake.backing.metadata() {
            Ok(metadata) => {
                unsafe { *out = metadata.len() as i64 };
                SQLITE_OK
            }
            Err(_) => SQLITE_IOERR_FSTAT,
        }
    }
    unsafe extern "C" fn fake_close(file: *mut sqlite3_file) -> c_int {
        let fake = unsafe { &mut *file.cast::<FakeFile>() };
        fake.close_calls += 1;
        fake.close_result
    }
    static FAKE_METHODS: sqlite3_io_methods = sqlite3_io_methods {
        xClose: Some(fake_close),
        xFileSize: Some(fake_size),
        ..METHODS
    };

    #[test]
    fn partial_failure_uses_actual_size_and_full_write_never_delegates() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("data.sqlite");
        let backing = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .open(&path)
            .unwrap();
        let domain = domain(temp.path(), small_policy());
        let mut fake = FakeFile {
            file: sqlite3_file {
                pMethods: &FAKE_METHODS,
            },
            backing,
            fail_measure: false,
            close_result: SQLITE_OK,
            close_calls: 0,
        };
        let state = FileState {
            inner: &mut fake.file,
            domain: domain.clone(),
            kind: 0,
            _lease: domain.lease(0, false).unwrap(),
            _custody: test_custody(),
        };
        let delegated = Cell::new(false);
        assert_eq!(
            domain.mutate(&state, 65 * 1024, false, || {
                delegated.set(true);
                SQLITE_OK
            }),
            SQLITE_FULL
        );
        assert!(!delegated.get());
        assert_eq!(
            domain.mutate(&state, 60 * 1024, false, || {
                fake.backing.set_len(10 * 1024).unwrap();
                SQLITE_IOERR_WRITE
            }),
            SQLITE_IOERR_WRITE
        );
        assert_eq!(domain.locked(|| domain.snapshot()).unwrap()[0], 10 * 1024);
        assert_eq!(
            domain.mutate(&state, 64 * 1024, false, || {
                fake.backing.set_len(64 * 1024).unwrap();
                SQLITE_OK
            }),
            SQLITE_OK
        );
    }

    #[test]
    fn unknown_partial_result_retains_reservation_and_quarantines_group() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("data.sqlite");
        let backing = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .open(&path)
            .unwrap();
        let domain = domain(temp.path(), small_policy());
        let mut fake = FakeFile {
            file: sqlite3_file {
                pMethods: &FAKE_METHODS,
            },
            backing,
            fail_measure: false,
            close_result: SQLITE_OK,
            close_calls: 0,
        };
        let state = FileState {
            inner: &mut fake.file,
            domain: domain.clone(),
            kind: 0,
            _lease: domain.lease(0, false).unwrap(),
            _custody: test_custody(),
        };
        assert_eq!(
            domain.mutate(&state, 100, false, || {
                fake.fail_measure = true;
                SQLITE_IOERR_WRITE
            }),
            SQLITE_IOERR_FSTAT
        );
        assert_eq!(domain.retained_reservation.load(Ordering::Acquire), 100);
        assert!(domain.quarantined.load(Ordering::Acquire));
        assert_eq!(domain.unlink(c"data.sqlite").unwrap_err(), SQLITE_IOERR);
    }

    // Invoked in a separate process by the following test. No inherited Rust
    // mutex or flock open description substitutes for the cross-process proof.
    #[test]
    fn child_open_lease() {
        let Some(path) = std::env::var_os("TOBKIRI_QUOTA_LEASE_CHILD") else {
            return;
        };
        let domain = domain(std::path::Path::new(&path), small_policy());
        let _lease = domain.locked(|| domain.lease(0, false)).unwrap();
        println!("QUOTA_LEASE_READY");
        std::io::stdout().flush().unwrap();
        let mut byte = [0];
        std::io::stdin().read_exact(&mut byte).unwrap();
    }
    #[test]
    fn another_process_open_lease_prevents_unlink() {
        let temp = tempfile::tempdir().unwrap();
        std::fs::write(temp.path().join("data.sqlite"), [0; 10]).unwrap();
        let domain = domain(temp.path(), small_policy());
        let mut child = Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "quota_vfs::tests::child_open_lease",
                "--nocapture",
            ])
            .env("TOBKIRI_QUOTA_LEASE_CHILD", temp.path())
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        let mut stdout = std::io::BufReader::new(child.stdout.take().unwrap());
        let mut ready = false;
        loop {
            let mut line = String::new();
            if stdout.read_line(&mut line).unwrap() == 0 {
                break;
            }
            if line.contains("QUOTA_LEASE_READY") {
                ready = true;
                break;
            }
        }
        assert!(
            ready,
            "child did not acquire its independently opened lease"
        );
        assert_eq!(domain.unlink(c"data.sqlite").unwrap_err(), SQLITE_BUSY);
        child.stdin.take().unwrap().write_all(b"x").unwrap();
        assert!(child.wait().unwrap().success());
        assert_eq!(domain.unlink(c"data.sqlite").unwrap(), 0);
    }

    #[test]
    fn ended_journal_lease_releases_an_inherited_open_description() {
        let temp = tempfile::tempdir().unwrap();
        std::fs::write(temp.path().join("data.sqlite-journal"), [0; 10]).unwrap();
        let domain = domain(temp.path(), small_policy());
        let lease = domain.lease(1, false).unwrap();
        // dup shares the exact open description, as a fork/exec copy does.
        let inherited = lease.file.as_ref().unwrap().try_clone().unwrap();
        assert_eq!(
            domain.unlink(c"data.sqlite-journal").unwrap_err(),
            SQLITE_BUSY
        );
        drop(lease);
        assert_eq!(domain.unlink(c"data.sqlite-journal").unwrap(), 0);
        assert!(!temp.path().join("data.sqlite-journal").exists());
        assert_eq!(inherited.metadata().unwrap().len(), 0);
    }

    #[test]
    fn fork_child_cannot_unlock_the_issuing_process_lease() {
        let temp = tempfile::tempdir().unwrap();
        let domain = domain(temp.path(), small_policy());
        let lease = domain.lease(1, false).unwrap();
        let mut ready = [-1; 2];
        assert_eq!(unsafe { libc::pipe(ready.as_mut_ptr()) }, 0);
        let child = unsafe { libc::fork() };
        assert!(child >= 0);
        if child == 0 {
            // This fixture opens no SQLite connection and never calls SQLite
            // in the child. Use only the lease's process guard/descriptor close
            // and async-signal-safe libc calls after fork.
            drop(lease);
            unsafe {
                libc::close(ready[0]);
                libc::write(ready[1], b"1".as_ptr().cast(), 1);
                libc::_exit(0);
            }
        }
        let mut byte = 0u8;
        let mut status = -1;
        unsafe {
            libc::close(ready[1]);
            assert_eq!(libc::read(ready[0], (&mut byte as *mut u8).cast(), 1), 1);
            libc::close(ready[0]);
            assert_eq!(libc::waitpid(child, &mut status, 0), child);
        }
        assert_eq!((byte, status), (b'1', 0));
        assert_eq!(domain.lease(1, true).unwrap_err(), SQLITE_BUSY);
        drop(lease);
        assert!(domain.lease(1, true).is_ok());
    }

    #[test]
    fn close_result_controls_actual_core_reservation_lifetime() {
        use tobkiri_gate::{ControlKind, CoreControl};
        for (close_result, fail_unlock) in [
            (SQLITE_OK, false),
            (SQLITE_IOERR_CLOSE, false),
            (SQLITE_OK, true),
        ] {
            let temp = tempfile::tempdir().unwrap();
            let backing = std::fs::OpenOptions::new()
                .read(true)
                .write(true)
                .create_new(true)
                .open(temp.path().join("data.sqlite"))
                .unwrap();
            let domain = domain(temp.path(), small_policy());
            let control = CoreControl::default();
            let custody = crate::data_custody::DataCustody::registered(
                None,
                control
                    .reserve(ControlKind::Registration, 64 * 1024)
                    .unwrap(),
            );
            let mut fake = Box::new(FakeFile {
                file: sqlite3_file {
                    pMethods: &FAKE_METHODS,
                },
                backing,
                fail_measure: false,
                close_result,
                close_calls: 0,
            });
            let mut lease = domain.lease(0, false).unwrap();
            lease.fail_unlock = fail_unlock;
            let retained = Box::into_raw(Box::new(FileState {
                inner: &mut fake.file,
                domain: domain.clone(),
                kind: 0,
                _lease: lease,
                _custody: custody.clone(),
            }));
            let mut header = Header {
                file: sqlite3_file { pMethods: &METHODS },
                state: retained,
            };
            // Equivalent to dropping the outer connection/registration owners:
            // only the actual VFS file now retains this reservation.
            drop(custody);
            let expected_result = if fail_unlock {
                SQLITE_IOERR_UNLOCK
            } else {
                close_result
            };
            assert_eq!(unsafe { close(&mut header.file) }, expected_result);
            assert_eq!(fake.close_calls, 1);
            assert!(header.file.pMethods.is_null());
            let expected = if expected_result == SQLITE_OK { 0 } else { 1 };
            assert_eq!(control.usage().count(ControlKind::Registration), expected);
            assert_eq!(control.usage().bytes, expected * 64 * 1024);
            if expected_result != SQLITE_OK {
                assert_eq!(domain.unlink(c"data.sqlite").unwrap_err(), SQLITE_IOERR);
                assert_eq!(domain.lease(0, true).unwrap_err(), SQLITE_BUSY);
                // Fault-fixture cleanup only: after explicitly destroying the
                // injected native file, its destruction is known. Production
                // provides no retry/replay or speculative release on error.
                drop(fake);
                unsafe {
                    (*retained)._lease.fail_unlock = false;
                    drop(Box::from_raw(retained));
                }
                assert_eq!(control.usage().count(ControlKind::Registration), 0);
            }
        }
    }

    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    #[test]
    fn child_owner_custody_probe() {
        let Some(path) = std::env::var_os("TOBKIRI_QUOTA_OWNER_CUSTODY_ROOT") else {
            return;
        };
        let root = crate::DataRoot::open(path).unwrap();
        let profile = tobkiri_canon::ids::ProfileId::parse("prof_quota_custody").unwrap();
        let pack = tobkiri_canon::ids::PackId::parse("local.quota-custody").unwrap();
        let acquired = root.owner_lease(&profile, &pack);
        if std::env::var_os("TOBKIRI_QUOTA_OWNER_CUSTODY_BLOCKED").as_deref()
            == Some(std::ffi::OsStr::new("1"))
        {
            assert!(matches!(acquired, Err(StoreError::Conflict(_))));
        } else {
            assert!(acquired.is_ok());
        }
    }
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    #[test]
    fn unknown_close_keeps_actual_owner_os_lock_and_core_slot() {
        use tobkiri_gate::{ControlKind, CoreControl};
        let temp = tempfile::tempdir().unwrap();
        let root = crate::DataRoot::open(temp.path().canonicalize().unwrap().join("root")).unwrap();
        let data_path = root.path().join("guest");
        std::fs::create_dir(&data_path).unwrap();
        let backing = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .open(data_path.join("data.sqlite"))
            .unwrap();
        let domain = domain(&data_path, small_policy());
        let profile = tobkiri_canon::ids::ProfileId::parse("prof_quota_custody").unwrap();
        let pack = tobkiri_canon::ids::PackId::parse("local.quota-custody").unwrap();
        let owner = root.owner_lease(&profile, &pack).unwrap();
        let control = CoreControl::default();
        let custody = crate::data_custody::DataCustody::registered(
            Some(owner),
            control
                .reserve(ControlKind::Registration, 64 * 1024)
                .unwrap(),
        );
        let mut fake = Box::new(FakeFile {
            file: sqlite3_file {
                pMethods: &FAKE_METHODS,
            },
            backing,
            fail_measure: false,
            close_result: SQLITE_IOERR_CLOSE,
            close_calls: 0,
        });
        let retained = Box::into_raw(Box::new(FileState {
            inner: &mut fake.file,
            domain: domain.clone(),
            kind: 0,
            _lease: domain.lease(0, false).unwrap(),
            _custody: custody.clone(),
        }));
        let mut header = Header {
            file: sqlite3_file { pMethods: &METHODS },
            state: retained,
        };
        drop(custody);
        assert_eq!(unsafe { close(&mut header.file) }, SQLITE_IOERR_CLOSE);
        let probe = |blocked| {
            let result = Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "quota_vfs::tests::child_owner_custody_probe",
                    "--nocapture",
                ])
                .env("TOBKIRI_QUOTA_OWNER_CUSTODY_ROOT", root.path())
                .env(
                    "TOBKIRI_QUOTA_OWNER_CUSTODY_BLOCKED",
                    if blocked { "1" } else { "0" },
                )
                .output()
                .unwrap();
            assert!(
                result.status.success(),
                "{}",
                String::from_utf8_lossy(&result.stderr)
            );
        };
        // The original OwnerLease lives only inside the retained VFS state;
        // an independently exec'd process still cannot acquire its OS domain.
        assert_eq!(control.usage().count(ControlKind::Registration), 1);
        probe(true);
        drop(fake); // Fixture establishes physical destruction before cleanup.
        unsafe {
            drop(Box::from_raw(retained));
        }
        assert_eq!(control.usage().count(ControlKind::Registration), 0);
        probe(false);
    }

    #[test]
    fn real_sqlite_quota_rolls_back_and_survives_reopen() {
        fn sqlite_status<T>(result: &rusqlite::Result<T>) -> Option<(rusqlite::ErrorCode, c_int)> {
            match result {
                Err(rusqlite::Error::SqliteFailure(error, _)) => {
                    Some((error.code, error.extended_code))
                }
                _ => None,
            }
        }
        fn checked<T>(result: rusqlite::Result<T>, stage: &'static str, quota: &QuotaDomain) -> T {
            let status = sqlite_status(&result);
            result.unwrap_or_else(|_| {
                panic!(
                    "stage={stage}; sqlite={status:?}; busy={:?}",
                    quota.busy_diagnostic()
                )
            })
        }
        fn opened_domain(root: &crate::DataRoot) -> Arc<QuotaDomain> {
            // Borrow the actual already-open domain without a second acquire,
            // descriptor, quota operation, or policy mutation.
            let metadata = std::fs::metadata(root.path()).unwrap();
            registry()
                .lock()
                .unwrap()
                .groups
                .get(&((metadata.dev(), metadata.ino()), "data.sqlite".into()))
                .unwrap()
                .upgrade()
                .unwrap()
        }
        let temp = tempfile::tempdir().unwrap();
        let root = crate::DataRoot::open(temp.path().canonicalize().unwrap()).unwrap();
        let path = std::path::Path::new("data.sqlite");
        let database = root
            .data_database_with_disk_policy(path, true, small_policy(), test_custody())
            .expect("stage=initial-open");
        let quota = opened_domain(&root);
        let connection = &database.connection;
        for (stage, sql) in [
            ("delete-journal", "PRAGMA journal_mode=DELETE"),
            ("disable-mmap", "PRAGMA mmap_size=0"),
            ("create-table", "CREATE TABLE t(value BLOB)"),
            ("initial-insert", "INSERT INTO t VALUES(x'01')"),
        ] {
            quota.reset_busy();
            checked(connection.execute_batch(sql), stage, &quota);
        }
        quota.reset_busy();
        let result = connection.execute("INSERT INTO t VALUES(zeroblob(100000))", []);
        let sqlite_status = sqlite_status(&result);
        assert!(
            matches!(result, Err(rusqlite::Error::SqliteFailure(error, _)) if error.code == rusqlite::ErrorCode::DiskFull),
            "stage=quota-insert; expected SQLITE_FULL; sqlite={sqlite_status:?}; busy={:?}",
            quota.busy_diagnostic()
        );
        quota.reset_busy();
        assert_eq!(
            checked(
                connection.query_row("SELECT count(*) FROM t", [], |row| row.get::<_, u32>(0)),
                "rollback-count",
                &quota
            ),
            1
        );
        quota.reset_busy();
        let mode: String = checked(
            connection.query_row("PRAGMA journal_mode=WAL", [], |row| row.get(0)),
            "wal-refusal",
            &quota,
        );
        assert_eq!(mode, "delete");
        let closed_domain = Arc::downgrade(&quota);
        drop(quota);
        drop(database);
        assert!(
            closed_domain.upgrade().is_none(),
            "stage=initial-close; actual quota domain retained after connection/route drop"
        );
        let database = root
            .data_database_with_disk_policy(path, false, small_policy(), test_custody())
            .expect("stage=reopen");
        let quota = opened_domain(&root);
        let connection = &database.connection;
        quota.reset_busy();
        assert_eq!(
            checked(
                connection.query_row("PRAGMA integrity_check", [], |row| row.get::<_, String>(0)),
                "reopened-integrity",
                &quota
            ),
            "ok"
        );
        quota.reset_busy();
        assert_eq!(
            checked(
                connection.query_row("SELECT count(*) FROM t", [], |row| row.get::<_, u32>(0)),
                "reopened-count",
                &quota
            ),
            1
        );
        let closed_domain = Arc::downgrade(&quota);
        drop(quota);
        drop(database);
        assert!(
            closed_domain.upgrade().is_none(),
            "stage=reopened-close; actual quota domain retained after connection/route drop"
        );
    }
}
