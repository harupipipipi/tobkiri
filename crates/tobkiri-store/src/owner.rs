//! Physical authority domains and permanent allocation-owner locks.
//!
//! Unix `flock` and the Windows directory sharing exclusion follow a retained
//! native file object: cloning a lease retains the same lock, and closing an
//! unrelated open descriptor does not release it. We never use a mutable slot
//! pathname or explicitly unlock the directory inode. The final strong lease drop is
//! the cleanup boundary; it must occur only after all charged storage is gone.
use crate::{DataRoot, StoreError};
use std::{cmp::Ordering, fmt, sync::Arc};
use tobkiri_canon::Digest;
use tobkiri_canon::ids::{PackId, ProfileId};
use tobkiri_gate::{AllocationOwnerLease, CapacityOwner};

/// Opaque physical root identity. Its directory descriptor stays open while any
/// registry key or owner lease retains it, including after a directory rename.
#[derive(Clone)]
pub struct DataRootIdentity {
    device: u64,
    #[cfg(not(windows))]
    inode: u64,
    #[cfg(windows)]
    file_id: [u8; 16],
    #[cfg(any(unix, windows))]
    directory: Arc<std::fs::File>,
}
impl fmt::Debug for DataRootIdentity {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let mut value = f.debug_struct("DataRootIdentity");
        value.field("device", &self.device);
        #[cfg(not(windows))]
        value.field("inode", &self.inode);
        #[cfg(windows)]
        value.field("file_id", &self.file_id);
        value.finish()
    }
}
impl PartialEq for DataRootIdentity {
    fn eq(&self, other: &Self) -> bool {
        self.cmp(other) == Ordering::Equal
    }
}
impl Eq for DataRootIdentity {}
impl PartialOrd for DataRootIdentity {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}
impl Ord for DataRootIdentity {
    fn cmp(&self, other: &Self) -> Ordering {
        #[cfg(not(windows))]
        {
            (self.device, self.inode).cmp(&(other.device, other.inode))
        }
        #[cfg(windows)]
        {
            (self.device, self.file_id).cmp(&(other.device, other.file_id))
        }
    }
}

struct HeldOwner {
    identity: DataRootIdentity,
    owner: CapacityOwner,
    domain: Digest,
    process: u32,
    #[cfg(unix)]
    _root_lock: Arc<unix::HeldRoot>,
    #[cfg(windows)]
    _root_lock: Arc<windows::HeldRoot>,
}
/// Strong proof of a real exclusive OS lock, with no public constructor. Retain
/// it through allocations, outputs, asynchronous I/O and unknown cleanup.
#[derive(Clone)]
pub struct OwnerLease(Arc<HeldOwner>);
impl fmt::Debug for OwnerLease {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("OwnerLease")
            .field("identity", self.identity())
            .field("profile", self.profile())
            .field("pack", &self.pack())
            .finish()
    }
}
impl OwnerLease {
    pub fn identity(&self) -> &DataRootIdentity {
        &self.0.identity
    }
    pub fn profile(&self) -> &ProfileId {
        self.0.owner.profile()
    }
    pub fn pack(&self) -> Option<&PackId> {
        self.0.owner.pack()
    }
    pub fn check_live(&self) -> Result<(), StoreError> {
        if self.0.process != std::process::id() {
            return Err(StoreError::Unavailable(
                "inherited allocation owner proof".into(),
            ));
        }
        Ok(())
    }
}
impl AllocationOwnerLease for OwnerLease {
    fn validate(&self) -> Result<(), tobkiri_gate::GateError> {
        if self.0.process != std::process::id() {
            return Err(tobkiri_gate::GateError::Unavailable);
        }
        Ok(())
    }
    fn owner(&self) -> &CapacityOwner {
        &self.0.owner
    }
    fn allocation_domain(&self) -> Digest {
        self.0.domain.clone()
    }
}

impl DataRoot {
    /// Validate the actual pinned native storage root without acquiring an
    /// allocation-owner lock or granting any storage authority.
    pub(crate) fn validate_native_data_root(&self) -> Result<(), StoreError> {
        #[cfg(unix)]
        {
            unix::validate_root(&self.identity()?)
        }
        #[cfg(windows)]
        {
            windows::validate_root(&self.identity()?)
        }
        #[cfg(not(any(unix, windows)))]
        {
            Err(StoreError::Unavailable(
                "native storage root proof unsupported".into(),
            ))
        }
    }

    pub fn identity(&self) -> Result<DataRootIdentity, StoreError> {
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            let directory = self.directory_capability();
            let metadata = directory.metadata()?;
            if !metadata.is_dir() {
                return Err(StoreError::Denied(
                    "root capability is not a directory".into(),
                ));
            }
            Ok(DataRootIdentity {
                device: metadata.dev(),
                inode: metadata.ino(),
                directory,
            })
        }
        #[cfg(windows)]
        {
            let directory = self.directory_capability();
            let (device, file_id) = crate::secure::windows::file_identity(&directory)?;
            crate::secure::windows::private_directory(&directory)?;
            Ok(DataRootIdentity {
                device,
                file_id,
                directory,
            })
        }
        #[cfg(not(any(unix, windows)))]
        {
            Err(StoreError::Unavailable(
                "physical root identity unsupported".into(),
            ))
        }
    }

    /// Acquire/share a stable owner's exclusive allocation domain. This is
    /// separate from short metadata/CAS locks and never uses expiry or PID takeover.
    pub fn owner_lease(
        &self,
        profile: &ProfileId,
        pack: &PackId,
    ) -> Result<OwnerLease, StoreError> {
        #[cfg(unix)]
        {
            unix::acquire(
                self.identity()?,
                CapacityOwner::Pack {
                    profile: profile.clone(),
                    pack: pack.clone(),
                },
            )
        }
        #[cfg(windows)]
        {
            windows::acquire(
                self.identity()?,
                CapacityOwner::Pack {
                    profile: profile.clone(),
                    pack: pack.clone(),
                },
            )
        }
        #[cfg(not(any(unix, windows)))]
        {
            let _ = (profile, pack);
            Err(StoreError::Unavailable(
                "allocation owner locks unsupported".into(),
            ))
        }
    }
    /// Established User account: one stable domain per Profile. This is only an
    /// OS lease; Gate must independently verify explicit current User funding.
    pub fn user_owner_lease(&self, profile: &ProfileId) -> Result<OwnerLease, StoreError> {
        #[cfg(unix)]
        {
            unix::acquire(
                self.identity()?,
                CapacityOwner::User {
                    profile: profile.clone(),
                },
            )
        }
        #[cfg(windows)]
        {
            windows::acquire(
                self.identity()?,
                CapacityOwner::User {
                    profile: profile.clone(),
                },
            )
        }
        #[cfg(not(any(unix, windows)))]
        {
            let _ = profile;
            Err(StoreError::Unavailable(
                "allocation owner locks unsupported".into(),
            ))
        }
    }
}

#[cfg(windows)]
#[path = "owner_windows.rs"]
mod windows;

#[cfg(any(
    test,
    all(target_os = "linux", target_arch = "x86_64", target_env = "gnu")
))]
#[path = "owner_linux.rs"]
mod linux;

/// Flush an already-held Windows allocation root without reopening a competing
/// write handle. `false` means this process has no such native owner handle.
#[cfg(windows)]
pub(crate) fn flush_windows_owner_directory(file: &std::fs::File) -> Result<bool, StoreError> {
    windows::flush_directory(file)
}

#[cfg(unix)]
mod unix {
    use super::*;
    use std::{
        collections::BTreeMap,
        fs::File,
        os::{
            fd::{AsRawFd, FromRawFd},
            unix::fs::MetadataExt,
        },
        sync::{Mutex, OnceLock, Weak},
    };
    const MAX_LIVE_OWNERS: usize = 4096;
    type RootKey = (u32, u64, u64);
    type OwnerKey = (RootKey, CapacityOwner);
    pub(super) struct HeldRoot {
        _file: File,
    }
    #[derive(Default)]
    struct Registry {
        roots: BTreeMap<RootKey, Weak<HeldRoot>>,
        owners: BTreeMap<OwnerKey, Weak<HeldOwner>>,
    }
    static REGISTRY: OnceLock<Mutex<Registry>> = OnceLock::new();
    static PROCESS: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(0);
    fn private_directory(metadata: &std::fs::Metadata) -> Result<(), StoreError> {
        if metadata.uid() != unsafe { libc::geteuid() }
            || metadata.mode() & 0o077 != 0
            || !metadata.is_dir()
        {
            return Err(StoreError::Denied(
                "allocation root inode is not private".into(),
            ));
        }
        Ok(())
    }
    /// Admit only the platform/filesystem combinations with native proof.
    /// Other combinations remain unavailable, including unknown Linux mounts.
    fn supported_filesystem(file: &File) -> Result<(), StoreError> {
        #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
        {
            let mut filesystem = std::mem::MaybeUninit::<libc::statfs>::uninit();
            if unsafe { libc::fstatfs(file.as_raw_fd(), filesystem.as_mut_ptr()) } != 0 {
                return Err(std::io::Error::last_os_error().into());
            }
            let filesystem = unsafe { filesystem.assume_init() };
            let name = unsafe { std::ffi::CStr::from_ptr(filesystem.f_fstypename.as_ptr()) };
            let mut system = std::mem::MaybeUninit::<libc::utsname>::uninit();
            if unsafe { libc::uname(system.as_mut_ptr()) } != 0 {
                return Err(std::io::Error::last_os_error().into());
            }
            let system = unsafe { system.assume_init() };
            let release = unsafe { std::ffi::CStr::from_ptr(system.release.as_ptr()) };
            if name.to_bytes() != b"apfs"
                || filesystem.f_flags & libc::MNT_LOCAL as u32 == 0
                || release.to_bytes() != b"24.6.0"
            {
                return Err(StoreError::Unavailable(
                    "allocation owner OS/filesystem proof unavailable".into(),
                ));
            }
            Ok(())
        }
        #[cfg(all(target_os = "linux", target_arch = "x86_64", target_env = "gnu"))]
        {
            super::linux::validate(file)
        }
        #[cfg(not(any(
            all(target_os = "macos", target_arch = "aarch64"),
            all(target_os = "linux", target_arch = "x86_64", target_env = "gnu")
        )))]
        {
            let _ = file;
            Err(StoreError::Unavailable(
                "allocation owner OS/filesystem proof unavailable".into(),
            ))
        }
    }
    pub(super) fn validate_root(identity: &DataRootIdentity) -> Result<(), StoreError> {
        private_directory(&identity.directory.metadata()?)?;
        supported_filesystem(&identity.directory)
    }
    pub(super) fn acquire(
        identity: DataRootIdentity,
        owner: CapacityOwner,
    ) -> Result<OwnerLease, StoreError> {
        let process = std::process::id();
        match PROCESS.compare_exchange(
            0,
            process,
            std::sync::atomic::Ordering::AcqRel,
            std::sync::atomic::Ordering::Acquire,
        ) {
            Ok(_) => (),
            Err(original) if original == process => (),
            Err(_) => {
                return Err(StoreError::Unavailable(
                    "inherited allocation broker requires exec".into(),
                ));
            }
        }
        validate_root(&identity)?;
        let root_key = (std::process::id(), identity.device, identity.inode);
        let key = (root_key, owner.clone());
        let mut registry = REGISTRY
            .get_or_init(Default::default)
            .lock()
            .map_err(|_| StoreError::Unavailable("allocation owner registry poisoned".into()))?;
        registry.owners.retain(|_, lease| lease.strong_count() != 0);
        registry.roots.retain(|_, lease| lease.strong_count() != 0);
        if let Some(held) = registry.owners.get(&key).and_then(Weak::upgrade) {
            return Ok(OwnerLease(held));
        }
        if registry.owners.len() >= MAX_LIVE_OWNERS || registry.roots.len() >= MAX_LIVE_OWNERS {
            return Err(StoreError::Quota);
        }
        let root_lock = if let Some(held) = registry.roots.get(&root_key).and_then(Weak::upgrade) {
            held
        } else {
            // One conservative collision slot is the physical directory inode
            // itself. Mutable slot files cannot provide this replacement proof.
            // Different owners in this process share the lock; every other
            // process's allocation admission for this root is denied while any
            // owner has charged storage. Metadata/CAS operations remain allowed.
            let fd = unsafe {
                libc::openat(
                    identity.directory.as_raw_fd(),
                    c".".as_ptr(),
                    libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                )
            };
            if fd < 0 {
                return Err(std::io::Error::last_os_error().into());
            }
            let file = unsafe { File::from_raw_fd(fd) };
            let metadata = file.metadata()?;
            private_directory(&metadata)?;
            if metadata.dev() != identity.device || metadata.ino() != identity.inode {
                return Err(StoreError::Conflict("allocation root inode changed".into()));
            }
            if unsafe { libc::flock(fd, libc::LOCK_EX | libc::LOCK_NB) } != 0 {
                let error = std::io::Error::last_os_error();
                return if error.kind() == std::io::ErrorKind::WouldBlock {
                    Err(StoreError::Conflict(
                        "allocation root is already held".into(),
                    ))
                } else {
                    Err(StoreError::Unavailable(format!(
                        "physical directory locking unsupported: {error}"
                    )))
                };
            }
            let held = Arc::new(HeldRoot { _file: file });
            registry.roots.insert(root_key, Arc::downgrade(&held));
            held
        };
        let mut domain = Vec::from(b"tobkiri.physical-root/1".as_slice());
        domain.extend(identity.device.to_be_bytes());
        domain.extend(identity.inode.to_be_bytes());
        let held = Arc::new(HeldOwner {
            domain: Digest::hash(&domain),
            identity,
            owner,
            process,
            _root_lock: root_lock,
        });
        registry.owners.insert(key, Arc::downgrade(&held));
        Ok(OwnerLease(held))
    }
}
