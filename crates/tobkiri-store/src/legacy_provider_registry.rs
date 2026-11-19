//! Native access to original Python Profile stores.

use crate::{DataRoot, StoreError};
use std::path::Path;
use tobkiri_canon::ids::ProfileId;

/// Outcomes of attempting a legacy Registry publication.
#[derive(Debug, thiserror::Error)]
pub enum LegacyRegistryPublishError<E> {
    /// The Registry was not replaced by this attempt.
    #[error("Registry publication failed before replacement")]
    BeforePublish(#[source] StoreError),
    /// The caller's final authority check rejected publication.
    #[error("Registry publication authority changed")]
    Guard(E),
    /// Replacement may have happened. Only this transaction can check it.
    #[error("Registry publication outcome is uncertain")]
    Uncertain(#[source] StoreError),
}

/// A native-only lock on one Profile's original JSON store.
/// The transaction is tied to the original physical lock and store directory.
pub struct LegacyRegistryTransaction {
    #[cfg(unix)]
    inner: unix::Transaction,
}

impl std::fmt::Debug for LegacyRegistryTransaction {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("LegacyRegistryTransaction")
            .finish_non_exhaustive()
    }
}

impl DataRoot {
    /// Open the original Host's Registry root. On macOS the system /var and
    /// /tmp links are recognized after checking their ownership and target.
    pub fn open_legacy_registry(path: &Path) -> Result<Self, StoreError> {
        #[cfg(target_os = "macos")]
        let physical = macos_platform_root(path);
        #[cfg(target_os = "macos")]
        let path = physical.as_ref();
        Self::open_existing(path)
    }

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
            unix::read_legacy_store(self, profile, max_bytes, &unix::PROVIDER.data)
        }
        #[cfg(not(unix))]
        {
            let _ = (profile, max_bytes);
            Err(unavailable("Provider Registry"))
        }
    }

    /// Read one bounded observation of the Profile's original Model Registry.
    /// Stable absence returns `None`. This never creates a directory or lock.
    pub fn read_legacy_model_registry(
        &self,
        profile: &ProfileId,
        max_bytes: usize,
    ) -> Result<Option<Vec<u8>>, StoreError> {
        #[cfg(unix)]
        {
            unix::read_legacy_store(self, profile, max_bytes, &unix::MODEL.data)
        }
        #[cfg(not(unix))]
        {
            let _ = (profile, max_bytes);
            Err(unavailable("Model Registry"))
        }
    }

    /// Read the original conversation owner document for this Profile.
    /// Stable absence returns `None`. The read does not create state or lock the store.
    pub fn read_legacy_conversation_store(
        &self,
        profile: &ProfileId,
        max_bytes: usize,
    ) -> Result<Option<Vec<u8>>, StoreError> {
        #[cfg(unix)]
        {
            unix::read_legacy_store(self, profile, max_bytes, &unix::CONVERSATIONS.data)
        }
        #[cfg(not(unix))]
        {
            let _ = (profile, max_bytes);
            Err(unavailable("Conversation Store"))
        }
    }

    /// Acquire the original Python Provider Registry lock without waiting or
    /// breaking stale locks.
    /// An existing owned regular lock file returns `Io(AlreadyExists)`.
    pub fn lock_legacy_provider_registry(
        &self,
        profile: &ProfileId,
    ) -> Result<LegacyRegistryTransaction, StoreError> {
        #[cfg(unix)]
        {
            Ok(LegacyRegistryTransaction {
                inner: unix::Transaction::lock(self, profile, &unix::PROVIDER)?,
            })
        }
        #[cfg(not(unix))]
        {
            let _ = profile;
            Err(unavailable("Provider Registry"))
        }
    }

    /// Acquire the original Python Model Registry lock without waiting or
    /// breaking stale locks.
    /// An existing owned regular lock file returns `Io(AlreadyExists)`.
    pub fn lock_legacy_model_registry(
        &self,
        profile: &ProfileId,
    ) -> Result<LegacyRegistryTransaction, StoreError> {
        #[cfg(unix)]
        {
            Ok(LegacyRegistryTransaction {
                inner: unix::Transaction::lock(self, profile, &unix::MODEL)?,
            })
        }
        #[cfg(not(unix))]
        {
            let _ = profile;
            Err(unavailable("Model Registry"))
        }
    }

    /// Acquire the original Python Conversation Store lock without waiting or
    /// breaking stale locks. The store and its Profile directory must exist.
    /// An existing owned regular lock file returns `Io(AlreadyExists)`.
    pub fn lock_legacy_conversation_store(
        &self,
        profile: &ProfileId,
    ) -> Result<LegacyRegistryTransaction, StoreError> {
        #[cfg(unix)]
        {
            Ok(LegacyRegistryTransaction {
                inner: unix::Transaction::lock(self, profile, &unix::CONVERSATIONS)?,
            })
        }
        #[cfg(not(unix))]
        {
            let _ = profile;
            Err(unavailable("Conversation Store"))
        }
    }
}

// Keep the original Host's platform aliases without resolving other symlinks.
#[cfg(target_os = "macos")]
fn macos_platform_root(root: &Path) -> std::borrow::Cow<'_, Path> {
    use std::{borrow::Cow, fs, os::unix::fs::MetadataExt};

    for (alias, physical, relative) in [
        ("/var", "/private/var", "private/var"),
        ("/tmp", "/private/tmp", "private/tmp"),
    ] {
        if let Ok(suffix) = root.strip_prefix(alias)
            && fs::symlink_metadata(alias)
                .is_ok_and(|metadata| metadata.is_symlink() && metadata.uid() == 0)
            && fs::read_link(alias)
                .is_ok_and(|link| link == Path::new(physical) || link == Path::new(relative))
        {
            return Cow::Owned(Path::new(physical).join(suffix));
        }
    }
    Cow::Borrowed(root)
}

impl LegacyRegistryTransaction {
    /// Return bounded owned bytes from the original Registry, or absence.
    pub fn read(&self, max_bytes: usize) -> Result<Option<Vec<u8>>, StoreError> {
        #[cfg(unix)]
        {
            self.inner.read(max_bytes)
        }
        #[cfg(not(unix))]
        {
            let _ = max_bytes;
            Err(unavailable("Registry"))
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
    ) -> Result<(), LegacyRegistryPublishError<E>> {
        #[cfg(unix)]
        {
            self.inner
                .publish(prior, next, max_bytes, guard, std::fs::File::sync_all)
        }
        #[cfg(not(unix))]
        {
            let _ = (prior, next, max_bytes, guard);
            Err(LegacyRegistryPublishError::BeforePublish(unavailable(
                "Registry",
            )))
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
            Err(unavailable("Registry"))
        }
    }
}

#[cfg(not(unix))]
fn unavailable(registry: &str) -> StoreError {
    StoreError::Unavailable(format!("nofollow legacy {registry} storage unavailable"))
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

    pub(super) struct StoreLayout {
        directories: [&'static str; 3],
        store: &'static CStr,
        label: &'static str,
    }

    pub(super) struct RegistryLayout {
        pub(super) data: StoreLayout,
        lock: &'static CStr,
        private_write_directory: bool,
        requires_existing_store: bool,
    }

    pub(super) const PROVIDER: RegistryLayout = RegistryLayout {
        data: StoreLayout {
            directories: ["packs", "rumi_provider_registry_pack", "profiles"],
            store: c"provider_registry.store.json",
            label: "Provider Registry",
        },
        lock: c"provider-registry.lock",
        private_write_directory: false,
        requires_existing_store: false,
    };
    pub(super) const MODEL: RegistryLayout = RegistryLayout {
        data: StoreLayout {
            directories: ["packs", "rumi_model_registry_pack", "profiles"],
            store: c"model_registry.store.json",
            label: "Model Registry",
        },
        lock: c"model-registry.lock",
        private_write_directory: true,
        requires_existing_store: false,
    };
    pub(super) const CONVERSATIONS: RegistryLayout = RegistryLayout {
        data: StoreLayout {
            directories: ["packs", "rumi_conversation_store_pack", "profiles"],
            store: c"conversations.json",
            label: "Conversation Store",
        },
        lock: c"conversations.lock",
        private_write_directory: true,
        requires_existing_store: true,
    };

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
        layout: &'static RegistryLayout,
        directory: Arc<File>,
        lock: OwnedEntry,
        lock_snapshot: OwnedFileSnapshot,
        publication: RefCell<Publication>,
    }

    impl Transaction {
        pub(super) fn lock(
            root: &DataRoot,
            profile: &ProfileId,
            layout: &'static RegistryLayout,
        ) -> Result<Self, StoreError> {
            let directory = if layout.requires_existing_store {
                verify_read_root(root, &layout.data)?;
                let directory =
                    open_registry(root, profile, false, &layout.data).map_err(normalize_absence)?;
                let entry = stat_entry(&directory, layout.data.store).map_err(normalize_absence)?;
                if !is_owned_regular(&entry) {
                    return Err(StoreError::Denied(format!(
                        "unsafe {} entry",
                        layout.data.label
                    )));
                }
                directory
            } else {
                verify_root(root, &layout.data)?;
                open_registry(root, profile, true, &layout.data)?
            };
            let directory = Arc::new(directory);
            let lock_directory =
                Arc::new(owned_directory(&directory, "locks", true, &layout.data)?);
            let pid = std::process::id();
            let acquired_at = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map_err(|_| {
                    StoreError::Unavailable(format!("{} lock clock invalid", layout.data.label))
                })?
                .as_secs_f64();
            let payload = serde_json::to_vec(&serde_json::json!({
                "acquired_at": acquired_at,
                "owner": format!("tobkiri-launcher:pid:{pid}:{}", temporary_name()?),
                "pid": pid,
                "stale_after_seconds": null,
            }))
            .map_err(|_| {
                StoreError::Invalid(format!("{} lock payload invalid", layout.data.label))
            })?;
            let mut lock = match OwnedEntry::create(lock_directory.clone(), layout.lock.to_owned())
            {
                Ok(lock) => lock,
                Err(StoreError::Io(busy)) if busy.kind() == ErrorKind::AlreadyExists => {
                    match stat_entry(&lock_directory, layout.lock) {
                        Ok(entry) if !is_owned_regular(&entry) => {
                            return Err(StoreError::Denied(format!(
                                "unsafe {} lock entry",
                                layout.data.label
                            )));
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
                layout,
                directory,
                lock,
                lock_snapshot,
                publication: RefCell::new(Publication::Ready),
            };
            transaction.verify_binding()?;
            Ok(transaction)
        }

        fn verify_binding(&self) -> Result<(), StoreError> {
            let layout = self.layout;
            verify_root(&self.root, &layout.data)?;
            let directory = open_registry(&self.root, &self.profile, false, &layout.data)?;
            let locks = owned_directory(&directory, "locks", false, &layout.data)?;
            if file_identity(&directory)? != file_identity(&self.directory)?
                || file_identity(&locks)? != file_identity(&self.lock.directory)?
            {
                return Err(binding_changed(&layout.data));
            }
            self.lock_snapshot.verify_current(&locks, layout.lock)
        }

        pub(super) fn read(&self, max_bytes: usize) -> Result<Option<Vec<u8>>, StoreError> {
            self.verify_binding()?;
            let snapshot = self.read_store(max_bytes)?;
            self.verify_binding()?;
            verify_store_entry(
                &self.directory,
                snapshot.as_ref().map(|(_, file)| file),
                &self.layout.data,
            )?;
            Ok(snapshot.map(|(bytes, _)| bytes))
        }

        pub(super) fn publish<E>(
            &self,
            prior: Option<&[u8]>,
            next: &[u8],
            max_bytes: usize,
            guard: impl FnOnce() -> Result<(), E>,
            sync_directory: impl FnOnce(&File) -> std::io::Result<()>,
        ) -> Result<(), LegacyRegistryPublishError<E>> {
            use LegacyRegistryPublishError::{BeforePublish, Guard, Uncertain};
            let layout = self.layout;
            if !matches!(*self.publication.borrow(), Publication::Ready) {
                return Err(BeforePublish(StoreError::Conflict(format!(
                    "{} transaction has already attempted publication",
                    layout.data.label
                ))));
            }
            if next.len() > max_bytes || prior.is_some_and(|bytes| bytes.len() > max_bytes) {
                return Err(BeforePublish(StoreError::Quota));
            }
            if layout.requires_existing_store && prior.is_none() {
                return Err(BeforePublish(StoreError::NotFound));
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
                return Err(BeforePublish(StoreError::Conflict(format!(
                    "{} bytes changed",
                    layout.data.label
                ))));
            }
            let prior = observed.as_ref().map(|(_, file)| file);
            self.verify_binding().map_err(BeforePublish)?;
            *self.publication.borrow_mut() = Publication::Staging;
            guard().map_err(Guard)?;

            // The guard may cross a process boundary. Check only metadata
            // and pathname identity here, then replace within the pinned dir.
            self.verify_binding().map_err(BeforePublish)?;
            verify_store_entry(&self.directory, prior, &layout.data).map_err(BeforePublish)?;
            pending
                .file
                .verify_current(&self.directory, &staged.name)
                .map_err(BeforePublish)?;
            if layout.private_write_directory {
                self.directory
                    .set_permissions(Permissions::from_mode(0o700))
                    .map_err(|error| BeforePublish(error.into()))?;
            }
            let fd = self.directory.as_raw_fd();
            let status =
                unsafe { libc::renameat(fd, staged.name.as_ptr(), fd, layout.data.store.as_ptr()) };
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
                .and_then(|()| {
                    if has_owned_link(&self.directory, layout.data.store, &staged.file)? {
                        Ok(())
                    } else {
                        Err(binding_changed(&layout.data))
                    }
                });
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
            snapshot.verify_current(&self.directory, self.layout.data.store)?;
            Ok(bytes == next && snapshot.identity() == pending.file.identity())
        }

        fn read_store(
            &self,
            max_bytes: usize,
        ) -> Result<Option<(Vec<u8>, OwnedFileSnapshot)>, StoreError> {
            read_store(&self.directory, max_bytes, &self.layout.data)
        }
    }

    pub(super) fn read_legacy_store(
        root: &DataRoot,
        profile: &ProfileId,
        max_bytes: usize,
        layout: &StoreLayout,
    ) -> Result<Option<Vec<u8>>, StoreError> {
        verify_read_root(root, layout)?;
        // Keep every prefix descriptor alive so a removed directory's inode
        // cannot be reused while we verify the observed path or its absence.
        let observed = store_directory_prefix(root, profile, layout)?;
        let directory = observed
            .last()
            .filter(|_| observed.len() == layout.directories.len() + 1);
        let snapshot = match directory {
            Some(directory) => read_store(directory, max_bytes, layout),
            None => Ok(None),
        };
        let current = store_directory_prefix(root, profile, layout)?;
        verify_read_root(root, layout)?;
        if observed.len() != current.len() {
            return Err(binding_changed(layout));
        }
        for (before, after) in observed.iter().zip(current.iter()) {
            if file_identity(before)? != file_identity(after)? {
                return Err(binding_changed(layout));
            }
        }

        let snapshot = snapshot?;
        if let Some(directory) = directory {
            verify_store_entry(directory, snapshot.as_ref().map(|(_, file)| file), layout)?;
        }
        Ok(snapshot.map(|(bytes, _)| bytes))
    }

    fn read_store(
        directory: &File,
        max_bytes: usize,
        layout: &StoreLayout,
    ) -> Result<Option<(Vec<u8>, OwnedFileSnapshot)>, StoreError> {
        match read_existing_owned_at_with_allocation_error(
            directory,
            layout.store,
            max_bytes,
            || StoreError::Unavailable(format!("could not allocate {} read buffer", layout.label)),
        ) {
            Ok(snapshot) => Ok(Some(snapshot)),
            Err(StoreError::NotFound) => Ok(None),
            Err(error) => Err(error),
        }
    }

    fn verify_store_entry(
        directory: &File,
        prior: Option<&OwnedFileSnapshot>,
        layout: &StoreLayout,
    ) -> Result<(), StoreError> {
        if let Some(prior) = prior {
            return prior.verify_current(directory, layout.store);
        }
        match stat_entry(directory, layout.store) {
            Err(StoreError::Io(error)) if error.kind() == ErrorKind::NotFound => Ok(()),
            Err(error) => Err(error),
            Ok(_) => Err(StoreError::Conflict(format!("{} appeared", layout.label))),
        }
    }

    fn binding_changed(layout: &StoreLayout) -> StoreError {
        StoreError::Conflict(format!("{} filesystem binding changed", layout.label))
    }

    fn normalize_absence(error: StoreError) -> StoreError {
        match error {
            StoreError::Io(error) if error.kind() == ErrorKind::NotFound => StoreError::NotFound,
            error => error,
        }
    }

    fn file_identity(file: &File) -> Result<(u64, u64), StoreError> {
        let metadata = file.metadata()?;
        Ok((metadata.dev(), metadata.ino()))
    }

    fn verify_root(root: &DataRoot, layout: &StoreLayout) -> Result<(), StoreError> {
        let current = DataRoot::open_existing(root.path())?;
        if current.physical_identity_key()? != root.physical_identity_key()? {
            return Err(binding_changed(layout));
        }
        Ok(())
    }

    fn verify_read_root(root: &DataRoot, layout: &StoreLayout) -> Result<(), StoreError> {
        match verify_root(root, layout) {
            Err(StoreError::NotFound) => return Err(binding_changed(layout)),
            result => result?,
        }
        if root.directory_capability().metadata()?.uid() != unsafe { libc::geteuid() } {
            return Err(StoreError::Denied(format!("unsafe {} root", layout.label)));
        }
        Ok(())
    }

    fn store_directories<'a>(
        profile: &'a ProfileId,
        layout: &StoreLayout,
    ) -> impl Iterator<Item = &'a str> + use<'a> {
        layout.directories.into_iter().chain([profile.as_str()])
    }

    fn store_directory_prefix(
        root: &DataRoot,
        profile: &ProfileId,
        layout: &StoreLayout,
    ) -> Result<Vec<File>, StoreError> {
        let base = root.directory_capability();
        let mut directories = Vec::with_capacity(layout.directories.len() + 1);
        for name in store_directories(profile, layout) {
            let parent = directories.last().unwrap_or(base.as_ref());
            match owned_directory(parent, name, false, layout) {
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
        layout: &StoreLayout,
    ) -> Result<File, StoreError> {
        let base = root.directory_capability();
        let mut directory = owned_directory(&base, layout.directories[0], create, layout)?;
        for name in store_directories(profile, layout).skip(1) {
            directory = owned_directory(&directory, name, create, layout)?;
        }
        Ok(directory)
    }

    fn owned_directory(
        parent: &File,
        name: &str,
        create: bool,
        layout: &StoreLayout,
    ) -> Result<File, StoreError> {
        let directory = open_dir(parent, name, create)?;
        let metadata = directory.metadata()?;
        // Historical Python-created intermediate directories can be 0755.
        if metadata.uid() != unsafe { libc::geteuid() } || metadata.mode() & 0o022 != 0 {
            return Err(StoreError::Denied(format!(
                "unsafe {} directory",
                layout.label
            )));
        }
        Ok(directory)
    }

    fn is_owned_regular(entry: &libc::stat) -> bool {
        entry.st_mode & libc::S_IFMT == libc::S_IFREG
            && entry.st_uid == unsafe { libc::geteuid() }
            && entry.st_nlink == 1
    }

    fn has_owned_link(directory: &File, name: &CStr, file: &File) -> Result<bool, StoreError> {
        let entry = stat_entry(directory, name)?;
        Ok(is_owned_regular(&entry) && checked_file_identity(&entry)? == file_identity(file)?)
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
    }

    impl Drop for OwnedEntry {
        fn drop(&mut self) {
            if has_owned_link(&self.directory, &self.name, &self.file).unwrap_or(false) {
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
