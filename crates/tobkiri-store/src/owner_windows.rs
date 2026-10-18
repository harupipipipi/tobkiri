//! Allocation exclusion on the actual private local-NTFS root directory.
//!
//! A separate native directory handle requests FILE_ADD_FILE while denying
//! FILE_SHARE_WRITE. The Windows I/O manager consequently rejects another such
//! handle, including opens through aliases and from other processes. Ordinary
//! read/traverse/attribute handles remain available for authority/CAS work.
//! The lock has no thread ownership, mutable slot, timeout or explicit unlock:
//! the last strong lease closes the held native file object. File handles are
//! non-inheritable. Pinned directory handles also deny DELETE sharing, so root
//! rename/replacement is refused while custody remains live.
use super::*;
use crate::secure::windows::{file_identity, private_directory, reopen_directory_owner};
use std::{
    collections::BTreeMap,
    fs::File,
    sync::{Mutex, OnceLock, Weak},
};

const MAX_LIVE_OWNERS: usize = 4096;
type RootKey = (u32, u64, [u8; 16]);
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

pub(super) fn flush_directory(directory: &File) -> Result<bool, StoreError> {
    let Some(registry) = REGISTRY.get() else {
        return Ok(false);
    };
    let (device, file_id) = file_identity(directory)?;
    let held = registry
        .lock()
        .map_err(|_| StoreError::Unavailable("allocation owner registry poisoned".into()))?
        .roots
        .get(&(std::process::id(), device, file_id))
        .and_then(Weak::upgrade);
    let Some(held) = held else { return Ok(false) };
    // Retain the exact physical owner while flushing. No permission, lease or
    // lock state changes, and a failed OS flush remains an error.
    held._file.sync_all()?;
    Ok(true)
}

pub(super) fn validate_root(identity: &DataRootIdentity) -> Result<(), StoreError> {
    // Recheck the live handle, not a remembered path or a platform name alone.
    private_directory(&identity.directory)?;
    if file_identity(&identity.directory)? != (identity.device, identity.file_id) {
        return Err(StoreError::Conflict(
            "allocation root identity changed".into(),
        ));
    }
    Ok(())
}

pub(super) fn acquire(
    identity: DataRootIdentity,
    owner: CapacityOwner,
) -> Result<OwnerLease, StoreError> {
    validate_root(&identity)?;
    let process = std::process::id();
    let root_key = (process, identity.device, identity.file_id);
    let key = (root_key, owner.clone());
    let mut registry = REGISTRY
        .get_or_init(Default::default)
        .lock()
        .map_err(|_| StoreError::Unavailable("allocation owner registry poisoned".into()))?;
    registry.owners.retain(|_, held| held.strong_count() != 0);
    registry.roots.retain(|_, held| held.strong_count() != 0);
    if let Some(held) = registry.owners.get(&key).and_then(Weak::upgrade) {
        return Ok(OwnerLease(held));
    }
    if registry.owners.len() >= MAX_LIVE_OWNERS || registry.roots.len() >= MAX_LIVE_OWNERS {
        return Err(StoreError::Quota);
    }
    let root_lock = if let Some(held) = registry.roots.get(&root_key).and_then(Weak::upgrade) {
        held
    } else {
        // NtCreateFile uses this exact retained directory as RootDirectory and
        // an empty object name; it never resolves the root's old pathname.
        let file = reopen_directory_owner(&identity.directory).map_err(|error| match error {
            StoreError::Io(error) if error.raw_os_error() == Some(32) => {
                StoreError::Conflict("allocation root is already held".into())
            }
            other => other,
        })?;
        private_directory(&file)?;
        if file_identity(&file)? != (identity.device, identity.file_id) {
            return Err(StoreError::Conflict(
                "allocation root identity changed".into(),
            ));
        }
        let held = Arc::new(HeldRoot { _file: file });
        registry.roots.insert(root_key, Arc::downgrade(&held));
        held
    };
    // Keep the pre-existing Unix domain representation unchanged. Windows
    // includes the complete native 128-bit file ID and a distinct domain tag.
    let mut domain = Vec::from(b"tobkiri.physical-root.windows-ntfs/1".as_slice());
    domain.extend(identity.device.to_be_bytes());
    domain.extend(identity.file_id);
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
