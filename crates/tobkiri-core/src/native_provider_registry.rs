//! Native access to the original Profile Registry during an admitted operation.
use crate::{CoreError, NativeArtifactError};
use std::{io::ErrorKind, path::Path, sync::Arc, thread, time::Duration};
use tobkiri_canon::{AllocationBudget, CoreBuffer, ids::ProfileId};
use tobkiri_gate::{CapacityKind, NativeSetupInvocation, NativeSetupReservation};
use tobkiri_store::{
    DataRoot, LegacyProviderRegistryPublishError, LegacyProviderRegistryTransaction, StoreError,
};
use tobkiri_wasm::NativeDataIoError;

pub(crate) const DOCUMENT_LIMIT: usize = 1024 * 1024;

type Result<T> = std::result::Result<T, NativeArtifactError>;

struct LockedRegistry {
    transaction: LegacyProviderRegistryTransaction,
    observed: Option<CoreBuffer>,
}

/// Bounded observations and optional save custody for one original Profile Registry.
/// Only the native application can obtain this from its live inspection.
pub struct NativeProviderRegistry {
    root: DataRoot,
    profile: ProfileId,
    locked: Option<LockedRegistry>,
    _raw_read_funding: NativeSetupReservation,
}

impl NativeProviderRegistry {
    pub(crate) fn open(
        invocation: &NativeSetupInvocation,
        root: &Path,
        profile_id: &str,
    ) -> Result<Self> {
        let profile = ProfileId::parse(profile_id).map_err(CoreError::from)?;
        let funding = invocation
            .reserve(CapacityKind::VfsBuffer, DOCUMENT_LIMIT as u64)
            .map_err(CoreError::from)?;
        Ok(Self {
            root: open_legacy_registry_root(root)?,
            profile,
            locked: None,
            _raw_read_funding: funding,
        })
    }

    /// Read the original Registry without creating or locking it.
    pub fn observe(&self, budget: &Arc<dyn AllocationBudget>) -> Result<Option<CoreBuffer>> {
        if self.locked.is_some() {
            return Err(NativeArtifactError::Denied);
        }
        let bytes = self
            .root
            .read_legacy_provider_registry(&self.profile, DOCUMENT_LIMIT);
        funded_observation(bytes, budget)
    }

    /// Retain the fourth observation and its cooperative lock until publication.
    /// The caller's checkpoint covers both authorization and a bounded deadline.
    pub fn observe_locked(
        &mut self,
        budget: &Arc<dyn AllocationBudget>,
        checkpoint: impl Fn() -> Result<()>,
    ) -> Result<Option<CoreBuffer>> {
        if self.locked.is_some() {
            return Err(NativeArtifactError::Denied);
        }
        let transaction = loop {
            checkpoint()?;
            match self.root.lock_legacy_provider_registry(&self.profile) {
                Ok(transaction) => break transaction,
                Err(StoreError::Io(error)) if error.kind() == ErrorKind::AlreadyExists => {
                    thread::sleep(Duration::from_millis(10));
                }
                Err(error) => return Err(CoreError::from(error).into()),
            }
        };
        checkpoint()?;
        let observed = funded_observation(transaction.read(DOCUMENT_LIMIT), budget)?;
        checkpoint()?;
        self.locked = Some(LockedRegistry {
            transaction,
            observed: observed.clone(),
        });
        Ok(observed)
    }

    /// Publish one previously staged document, checking the current lease
    /// immediately before rename. The retained lock is never transferred.
    pub fn publish(self, document: &[u8], checkpoint: impl Fn() -> Result<()>) -> Result<()> {
        let locked = self.locked.as_ref().ok_or(NativeArtifactError::Denied)?;
        if document.len() > DOCUMENT_LIMIT {
            return Err(NativeArtifactError::ProviderRegistryLimitBeforePublish);
        }
        checkpoint()?;
        match locked.transaction.publish(
            locked.observed.as_ref().map(CoreBuffer::as_bytes),
            document,
            DOCUMENT_LIMIT,
            &checkpoint,
        ) {
            Ok(()) => Ok(()),
            Err(LegacyProviderRegistryPublishError::BeforePublish(error)) => {
                Err(before_publish(error))
            }
            Err(LegacyProviderRegistryPublishError::Guard(error)) => Err(error),
            Err(LegacyProviderRegistryPublishError::Uncertain(error)) => {
                if locked
                    .transaction
                    .confirm_exact(document, DOCUMENT_LIMIT)
                    .unwrap_or(false)
                {
                    Ok(())
                } else {
                    Err(NativeArtifactError::ProviderRegistryPublicationUnconfirmed(
                        error,
                    ))
                }
            }
        }
    }
}

pub(crate) fn read_legacy_model_document(
    invocation: &NativeSetupInvocation,
    root: &Path,
    profile_id: &str,
    output: &mut [u8],
) -> Result<usize> {
    let profile = ProfileId::parse(profile_id).map_err(CoreError::from)?;
    let _funding = invocation
        .reserve(CapacityKind::VfsBuffer, DOCUMENT_LIMIT as u64)
        .map_err(CoreError::from)?;
    let bytes = open_legacy_registry_root(root)?
        .read_legacy_model_registry(&profile, DOCUMENT_LIMIT)
        .map_err(|error| match error {
            StoreError::Quota => NativeArtifactError::TooLarge,
            error => CoreError::from(error).into(),
        })?;
    match bytes {
        None => {
            output[0] = 0;
            Ok(1)
        }
        Some(bytes) => {
            output[0] = 1;
            output[1..][..bytes.len()].copy_from_slice(&bytes);
            Ok(1 + bytes.len())
        }
    }
}

fn open_legacy_registry_root(root: &Path) -> Result<DataRoot> {
    #[cfg(target_os = "macos")]
    let physical_root = macos_platform_root(root);
    #[cfg(target_os = "macos")]
    let root = physical_root.as_ref();
    DataRoot::open_existing(root).map_err(|error| CoreError::from(error).into())
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

fn funded_observation(
    bytes: std::result::Result<Option<Vec<u8>>, StoreError>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Option<CoreBuffer>> {
    copy_document(bytes.map_err(before_publish)?, budget)
}

fn copy_document(
    bytes: Option<Vec<u8>>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Option<CoreBuffer>> {
    bytes
        .map(|bytes| CoreBuffer::copy_from(&bytes, budget))
        .transpose()
        .map_err(|_| NativeDataIoError::Unavailable.into())
}

fn before_publish(error: StoreError) -> NativeArtifactError {
    match error {
        StoreError::Quota => NativeArtifactError::ProviderRegistryLimitBeforePublish,
        error => CoreError::from(error).into(),
    }
}
