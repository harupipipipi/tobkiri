//! Native access to the original Python Provider Registry store.

use crate::{DataRoot, StoreError};
use tobkiri_canon::ids::ProfileId;

/// Outcomes of attempting a legacy Registry publication.
#[derive(Debug, thiserror::Error)]
pub enum LegacyProviderRegistryPublishError<E> {
    /// The Registry was not replaced by this attempt.
    #[error("Provider Registry publication failed before replacement")]
    BeforePublish(#[source] StoreError),
    /// The caller's final authority check rejected publication.
    #[error("Provider Registry publication authority changed")]
    Guard(E),
    /// Replacement may have happened. Only this transaction can check it.
    #[error("Provider Registry publication outcome is uncertain")]
    Uncertain(#[source] StoreError),
}

/// A native-only lock on one Profile's original Provider Registry.
/// The transaction is tied to the original physical lock and store directory.
pub struct LegacyProviderRegistryTransaction {
    #[cfg(unix)]
    inner: unix::Transaction,
}

impl std::fmt::Debug for LegacyProviderRegistryTransaction {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("LegacyProviderRegistryTransaction")
            .finish_non_exhaustive()
    }
}

impl DataRoot {
    /// Read one bounded observation of the original Profile Registry.
    /// Stable absence returns `None`. This never creates a directory or
    /// acquires the publication lock.
    pub fn read_legacy_provider_registry(
        &self,
        profile: &ProfileId,
        max_bytes: usize,
    ) -> Result<Option<Vec<u8>>, StoreError> {
        #[cfg(unix)]
        {
            unix::read_registry(self, profile, max_bytes)
        }
        #[cfg(not(unix))]
        {
            let _ = (profile, max_bytes);
            Err(unavailable())
        }
    }

    /// Acquire the original Python lock without waiting or breaking stale locks.
    /// An existing owned regular lock file returns `Io(AlreadyExists)`.
    pub fn lock_legacy_provider_registry(
        &self,
        profile: &ProfileId,
    ) -> Result<LegacyProviderRegistryTransaction, StoreError> {
        #[cfg(unix)]
        {
            Ok(LegacyProviderRegistryTransaction {
                inner: unix::Transaction::lock(self, profile)?,
            })
        }
        #[cfg(not(unix))]
        {
            let _ = profile;
            Err(unavailable())
        }
    }
}

impl LegacyProviderRegistryTransaction {
    /// Return bounded owned bytes from the original Registry, or absence.
    pub fn read(&self, max_bytes: usize) -> Result<Option<Vec<u8>>, StoreError> {
        #[cfg(unix)]
        {
            self.inner.read(max_bytes)
        }
        #[cfg(not(unix))]
        {
            let _ = max_bytes;
            Err(unavailable())
        }
    }

    /// Stage and replace the original Registry if its raw content still matches
    /// `prior`. Run `guard` after staging, at the final publication boundary.
    pub fn publish<E>(
        &self,
        prior: Option<&[u8]>,
        next: &[u8],
        max_bytes: usize,
        guard: impl FnOnce() -> Result<(), E>,
    ) -> Result<(), LegacyProviderRegistryPublishError<E>> {
        #[cfg(unix)]
        {
            self.inner
                .publish(prior, next, max_bytes, guard, std::fs::File::sync_all)
        }
        #[cfg(not(unix))]
        {
            let _ = (prior, next, max_bytes, guard);
            Err(LegacyProviderRegistryPublishError::BeforePublish(
                unavailable(),
            ))
        }
    }

    /// Confirm an uncertain publication only if the currently named file is
    /// this attempt's staged inode and contains the exact intended bytes.
    pub fn confirm_exact(&self, next: &[u8], max_bytes: usize) -> Result<bool, StoreError> {
        #[cfg(unix)]
        {
            self.inner.confirm_exact(next, max_bytes)
        }
        #[cfg(not(unix))]
        {
            let _ = (next, max_bytes);
            Err(unavailable())
        }
    }
}

#[cfg(not(unix))]
fn unavailable() -> StoreError {
    StoreError::Unavailable("nofollow legacy Provider Registry storage unavailable".into())
}

#[cfg(unix)]
mod unix {
    use super::*;
    use crate::secure::{
        OwnedFileSnapshot, checked_file_identity, open_dir, open_file,
        read_existing_owned_at_with_allocation_error, stat_entry, temporary_name,
    };
    use std::{
        cell::RefCell,
        ffi::{CStr, CString},
        fs::{File, Permissions},
        io::{Error, ErrorKind, Write},
        os::{
            fd::AsRawFd,
            unix::fs::{MetadataExt, PermissionsExt},
        },
        sync::Arc,
        time::{SystemTime, UNIX_EPOCH},
    };
    use tobkiri_canon::Digest;

    const REGISTRY_DIRS: [&str; 3] = ["packs", "rumi_provider_registry_pack", "profiles"];
    const STORE: &CStr = c"provider_registry.store.json";
    const LOCK: &CStr = c"provider-registry.lock";

    // Keeping the staged descriptor open prevents inode reuse during confirmation.
    struct PendingPublication {
        file: OwnedFileSnapshot,
        digest: Digest,
    }

    enum Publication {
        Ready,
        Staging,
        Uncertain(Box<PendingPublication>),
        Published,
    }

    pub(super) struct Transaction {
        root: DataRoot,
        profile: ProfileId,
        directory: Arc<File>,
        lock: OwnedEntry,
        lock_snapshot: OwnedFileSnapshot,
        publication: RefCell<Publication>,
    }

    impl Transaction {
        pub(super) fn lock(root: &DataRoot, profile: &ProfileId) -> Result<Self, StoreError> {
            verify_root(root)?;
            let directory = Arc::new(open_registry(root, profile, true)?);
            let lock_directory = Arc::new(owned_directory(&directory, "locks", true)?);
            let pid = std::process::id();
            let acquired_at = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map_err(|_| {
                    StoreError::Unavailable("Provider Registry lock clock invalid".into())
                })?
                .as_secs_f64();
            let payload = serde_json::to_vec(&serde_json::json!({
                "acquired_at": acquired_at,
                "owner": format!("tobkiri-launcher:pid:{pid}:{}", temporary_name()?),
                "pid": pid,
                "stale_after_seconds": null,
            }))
            .map_err(|_| StoreError::Invalid("Provider Registry lock payload invalid".into()))?;
            let mut lock = match OwnedEntry::create(lock_directory.clone(), LOCK.to_owned()) {
                Ok(lock) => lock,
                Err(StoreError::Io(busy)) if busy.kind() == ErrorKind::AlreadyExists => {
                    match stat_entry(&lock_directory, LOCK) {
                        Ok(entry) if !is_owned_regular(&entry) => {
                            return Err(StoreError::Denied(
                                "unsafe Provider Registry lock entry".into(),
                            ));
                        }
                        Ok(_) => return Err(busy.into()),
                        // The cooperative holder may have just released it.
                        Err(StoreError::Io(error)) if error.kind() == ErrorKind::NotFound => {
                            return Err(busy.into());
                        }
                        Err(error) => return Err(error),
                    }
                }
                Err(error) => return Err(error),
            };
            lock.file.write_all(&payload)?;
            let lock_snapshot = OwnedFileSnapshot::capture(lock.file.try_clone()?)?;
            let transaction = Self {
                root: root.clone(),
                profile: profile.clone(),
                directory,
                lock,
                lock_snapshot,
                publication: RefCell::new(Publication::Ready),
            };
            transaction.verify_binding()?;
            Ok(transaction)
        }

        fn verify_binding(&self) -> Result<(), StoreError> {
            verify_root(&self.root)?;
            let directory = open_registry(&self.root, &self.profile, false)?;
            let locks = owned_directory(&directory, "locks", false)?;
            if file_identity(&directory)? != file_identity(&self.directory)?
                || file_identity(&locks)? != file_identity(&self.lock.directory)?
            {
                return Err(binding_changed());
            }
            self.lock_snapshot.verify_current(&locks, LOCK)
        }

        pub(super) fn read(&self, max_bytes: usize) -> Result<Option<Vec<u8>>, StoreError> {
            self.verify_binding()?;
            let snapshot = self.read_store(max_bytes)?;
            self.verify_binding()?;
            verify_store_entry(&self.directory, snapshot.as_ref().map(|(_, file)| file))?;
            Ok(snapshot.map(|(bytes, _)| bytes))
        }

        pub(super) fn publish<E>(
            &self,
            prior: Option<&[u8]>,
            next: &[u8],
            max_bytes: usize,
            guard: impl FnOnce() -> Result<(), E>,
            sync_directory: impl FnOnce(&File) -> std::io::Result<()>,
        ) -> Result<(), LegacyProviderRegistryPublishError<E>> {
            use LegacyProviderRegistryPublishError::{BeforePublish, Guard, Uncertain};
            if !matches!(*self.publication.borrow(), Publication::Ready) {
                return Err(BeforePublish(StoreError::Conflict(
                    "Provider Registry transaction has already attempted publication".into(),
                )));
            }
            if next.len() > max_bytes || prior.is_some_and(|bytes| bytes.len() > max_bytes) {
                return Err(BeforePublish(StoreError::Quota));
            }
            self.verify_binding().map_err(BeforePublish)?;
            let name = CString::new(temporary_name().map_err(BeforePublish)?)
                .expect("generated temporary name is NUL-free");
            let mut staged =
                OwnedEntry::create(self.directory.clone(), name).map_err(BeforePublish)?;
            staged
                .file
                .write_all(next)
                .map_err(|error| BeforePublish(error.into()))?;
            staged
                .file
                .sync_all()
                .map_err(|error| BeforePublish(error.into()))?;
            let pending = Box::new(PendingPublication {
                file: OwnedFileSnapshot::capture(
                    staged
                        .file
                        .try_clone()
                        .map_err(|error| BeforePublish(error.into()))?,
                )
                .map_err(BeforePublish)?,
                digest: Digest::hash(next),
            });

            let observed = self.read_store(max_bytes).map_err(BeforePublish)?;
            if observed.as_ref().map(|(bytes, _)| bytes.as_slice()) != prior {
                return Err(BeforePublish(StoreError::Conflict(
                    "Provider Registry bytes changed".into(),
                )));
            }
            let prior = observed.as_ref().map(|(_, file)| file);
            self.verify_binding().map_err(BeforePublish)?;
            *self.publication.borrow_mut() = Publication::Staging;
            guard().map_err(Guard)?;

            // The guard may cross a process boundary. Check only metadata
            // and pathname identity here, then replace within the pinned dir.
            self.verify_binding().map_err(BeforePublish)?;
            verify_store_entry(&self.directory, prior).map_err(BeforePublish)?;
            pending
                .file
                .verify_current(&self.directory, &staged.name)
                .map_err(BeforePublish)?;
            let fd = self.directory.as_raw_fd();
            let status = unsafe { libc::renameat(fd, staged.name.as_ptr(), fd, STORE.as_ptr()) };
            if status != 0 {
                let error = Error::last_os_error();
                if matches!(
                    error.raw_os_error(),
                    Some(libc::EIO | libc::EINTR | libc::ESTALE | libc::ETIMEDOUT)
                ) {
                    *self.publication.borrow_mut() = Publication::Uncertain(pending);
                    return Err(Uncertain(error.into()));
                }
                return Err(BeforePublish(error.into()));
            }
            let synced = sync_directory(&self.directory)
                .map_err(StoreError::from)
                .and_then(|()| self.verify_binding())
                .and_then(|()| verify_owned_link(&self.directory, STORE, &staged.file));
            if let Err(error) = synced {
                *self.publication.borrow_mut() = Publication::Uncertain(pending);
                return Err(Uncertain(error));
            }
            *self.publication.borrow_mut() = Publication::Published;
            Ok(())
        }

        pub(super) fn confirm_exact(
            &self,
            next: &[u8],
            max_bytes: usize,
        ) -> Result<bool, StoreError> {
            let publication = self.publication.borrow();
            let Publication::Uncertain(pending) = &*publication else {
                return Ok(false);
            };
            if next.len() > max_bytes || Digest::hash(next) != pending.digest {
                return Ok(false);
            }

            self.verify_binding()?;
            let Some((bytes, snapshot)) = self.read_store(max_bytes)? else {
                return Ok(false);
            };
            self.verify_binding()?;
            snapshot.verify_current(&self.directory, STORE)?;
            Ok(bytes == next && snapshot.identity() == pending.file.identity())
        }

        fn read_store(
            &self,
            max_bytes: usize,
        ) -> Result<Option<(Vec<u8>, OwnedFileSnapshot)>, StoreError> {
            read_store(&self.directory, max_bytes)
        }
    }

    pub(super) fn read_registry(
        root: &DataRoot,
        profile: &ProfileId,
        max_bytes: usize,
    ) -> Result<Option<Vec<u8>>, StoreError> {
        verify_read_root(root)?;
        // Keep every prefix descriptor alive so a removed directory's inode
        // cannot be reused while we verify the observed path or its absence.
        let observed = registry_directory_prefix(root, profile)?;
        let directory = observed
            .last()
            .filter(|_| observed.len() == REGISTRY_DIRS.len() + 1);
        let snapshot = match directory {
            Some(directory) => read_store(directory, max_bytes),
            None => Ok(None),
        };
        let current = registry_directory_prefix(root, profile)?;
        verify_read_root(root)?;
        if observed.len() != current.len() {
            return Err(binding_changed());
        }
        for (before, after) in observed.iter().zip(current.iter()) {
            if file_identity(before)? != file_identity(after)? {
                return Err(binding_changed());
            }
        }

        let snapshot = snapshot?;
        if let Some(directory) = directory {
            verify_store_entry(directory, snapshot.as_ref().map(|(_, file)| file))?;
        }
        Ok(snapshot.map(|(bytes, _)| bytes))
    }

    fn read_store(
        directory: &File,
        max_bytes: usize,
    ) -> Result<Option<(Vec<u8>, OwnedFileSnapshot)>, StoreError> {
        match read_existing_owned_at_with_allocation_error(directory, STORE, max_bytes, || {
            StoreError::Unavailable("could not allocate Provider Registry read buffer".into())
        }) {
            Ok(snapshot) => Ok(Some(snapshot)),
            Err(StoreError::NotFound) => Ok(None),
            Err(error) => Err(error),
        }
    }

    fn verify_store_entry(
        directory: &File,
        prior: Option<&OwnedFileSnapshot>,
    ) -> Result<(), StoreError> {
        if let Some(prior) = prior {
            return prior.verify_current(directory, STORE);
        }
        match stat_entry(directory, STORE) {
            Err(StoreError::Io(error)) if error.kind() == ErrorKind::NotFound => Ok(()),
            Err(error) => Err(error),
            Ok(_) => Err(StoreError::Conflict("Provider Registry appeared".into())),
        }
    }

    fn binding_changed() -> StoreError {
        StoreError::Conflict("Provider Registry filesystem binding changed".into())
    }

    fn file_identity(file: &File) -> Result<(u64, u64), StoreError> {
        let metadata = file.metadata()?;
        Ok((metadata.dev(), metadata.ino()))
    }

    fn verify_root(root: &DataRoot) -> Result<(), StoreError> {
        let current = DataRoot::open_existing(root.path())?;
        if current.physical_identity_key()? != root.physical_identity_key()? {
            return Err(binding_changed());
        }
        Ok(())
    }

    fn verify_read_root(root: &DataRoot) -> Result<(), StoreError> {
        match verify_root(root) {
            Err(StoreError::NotFound) => return Err(binding_changed()),
            result => result?,
        }
        if root.directory_capability().metadata()?.uid() != unsafe { libc::geteuid() } {
            return Err(StoreError::Denied("unsafe Provider Registry root".into()));
        }
        Ok(())
    }

    fn registry_directories(profile: &ProfileId) -> impl Iterator<Item = &str> {
        REGISTRY_DIRS.into_iter().chain([profile.as_str()])
    }

    fn registry_directory_prefix(
        root: &DataRoot,
        profile: &ProfileId,
    ) -> Result<Vec<File>, StoreError> {
        let base = root.directory_capability();
        let mut directories = Vec::with_capacity(REGISTRY_DIRS.len() + 1);
        for name in registry_directories(profile) {
            let parent = directories.last().unwrap_or(base.as_ref());
            match owned_directory(parent, name, false) {
                Ok(directory) => directories.push(directory),
                Err(StoreError::Io(error)) if error.kind() == ErrorKind::NotFound => break,
                Err(error) => return Err(error),
            }
        }
        Ok(directories)
    }

    fn open_registry(
        root: &DataRoot,
        profile: &ProfileId,
        create: bool,
    ) -> Result<File, StoreError> {
        let base = root.directory_capability();
        let mut directory = owned_directory(&base, REGISTRY_DIRS[0], create)?;
        for name in registry_directories(profile).skip(1) {
            directory = owned_directory(&directory, name, create)?;
        }
        Ok(directory)
    }

    fn owned_directory(parent: &File, name: &str, create: bool) -> Result<File, StoreError> {
        let directory = open_dir(parent, name, create)?;
        let metadata = directory.metadata()?;
        // Historical Python-created intermediate directories can be 0755.
        if metadata.uid() != unsafe { libc::geteuid() } || metadata.mode() & 0o022 != 0 {
            return Err(StoreError::Denied(
                "unsafe Provider Registry directory".into(),
            ));
        }
        Ok(directory)
    }

    fn is_owned_regular(entry: &libc::stat) -> bool {
        entry.st_mode & libc::S_IFMT == libc::S_IFREG
            && entry.st_uid == unsafe { libc::geteuid() }
            && entry.st_nlink == 1
    }

    fn verify_owned_link(directory: &File, name: &CStr, file: &File) -> Result<(), StoreError> {
        let entry = stat_entry(directory, name)?;
        if !is_owned_regular(&entry) || checked_file_identity(&entry)? != file_identity(file)? {
            return Err(binding_changed());
        }
        Ok(())
    }

    struct OwnedEntry {
        directory: Arc<File>,
        name: CString,
        file: File,
    }

    impl OwnedEntry {
        fn create(directory: Arc<File>, name: CString) -> Result<Self, StoreError> {
            let file = open_file(
                &directory,
                name.to_str().expect("native Registry entry name is UTF-8"),
                true,
                true,
            )?;
            let entry = Self {
                directory,
                name,
                file,
            };
            entry.file.set_permissions(Permissions::from_mode(0o600))?;
            Ok(entry)
        }

        fn verify_link(&self) -> Result<(), StoreError> {
            verify_owned_link(&self.directory, &self.name, &self.file)
        }
    }

    impl Drop for OwnedEntry {
        fn drop(&mut self) {
            if self.verify_link().is_ok() {
                unsafe {
                    libc::unlinkat(self.directory.as_raw_fd(), self.name.as_ptr(), 0);
                }
            }
        }
    }
}

#[cfg(all(test, unix))]
#[path = "legacy_provider_registry_tests.rs"]
mod tests;
