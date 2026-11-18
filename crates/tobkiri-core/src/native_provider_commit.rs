//! Native custody of a staged write to the original Profile Registry.
use crate::{CoreError, NativeArtifactError};
use std::{io::ErrorKind, path::Path, sync::Arc, thread, time::Duration};
use tobkiri_canon::{AllocationBudget, CoreBuffer, ids::ProfileId};
use tobkiri_gate::{CapacityKind, NativeSetupInvocation, NativeSetupReservation};
use tobkiri_store::{
    DataRoot, LegacyProviderRegistryPublishError, LegacyProviderRegistryTransaction, StoreError,
};
use tobkiri_wasm::NativeDataIoError;

const DOCUMENT_LIMIT: usize = 1024 * 1024;

type Result<T> = std::result::Result<T, NativeArtifactError>;

struct LockedRegistry {
    transaction: LegacyProviderRegistryTransaction,
    observed: Option<CoreBuffer>,
}

/// The fixed Profile's original Registry lock and its bounded observation.
/// Only the native application can obtain this from its live inspection.
pub struct NativeProviderRegistryCommit {
    root: DataRoot,
    profile: ProfileId,
    locked: Option<LockedRegistry>,
    _raw_read_funding: NativeSetupReservation,
}

impl NativeProviderRegistryCommit {
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
            root: DataRoot::open_existing(root).map_err(CoreError::from)?,
            profile,
            locked: None,
            _raw_read_funding: funding,
        })
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
        let observed = transaction
            .read(DOCUMENT_LIMIT)
            .map_err(before_publish)?
            .map(|bytes| CoreBuffer::copy_from(&bytes, budget))
            .transpose()
            .map_err(|_| NativeDataIoError::Unavailable)?;
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

fn before_publish(error: StoreError) -> NativeArtifactError {
    match error {
        StoreError::Quota => NativeArtifactError::ProviderRegistryLimitBeforePublish,
        error => CoreError::from(error).into(),
    }
}
