//! Native access to the original Profile Registry during an admitted operation.
use crate::{CoreError, NativeArtifactError};
use std::{io::ErrorKind, path::Path, sync::Arc, thread, time::Duration};
use tobkiri_canon::{AllocationBudget, CoreBuffer, ids::ProfileId};
use tobkiri_gate::{CapacityKind, NativeSetupInvocation, NativeSetupReservation};
use tobkiri_store::{DataRoot, LegacyRegistryPublishError, LegacyRegistryTransaction, StoreError};
use tobkiri_wasm::NativeDataIoError;

pub(crate) const DOCUMENT_LIMIT: usize = 1024 * 1024;

type Result<T> = std::result::Result<T, NativeArtifactError>;

struct LockedRegistry {
    transaction: LegacyRegistryTransaction,
    observed: Option<CoreBuffer>,
}

/// Bounded observations and optional save custody for one original Profile Registry.
/// Only the native application can obtain this from its live inspection.
pub struct NativeRegistry {
    invocation: NativeSetupInvocation,
    root: DataRoot,
    profile: ProfileId,
    kind: LegacyRegistryKind,
    locked: Option<LockedRegistry>,
    _raw_read_funding: NativeSetupReservation,
}

pub use NativeRegistry as NativeProviderRegistry;

#[derive(Clone, Copy)]
pub(crate) enum LegacyRegistryKind {
    Provider,
    Model,
    Conversation,
}

impl LegacyRegistryKind {
    fn limit(self) -> NativeArtifactError {
        match self {
            Self::Provider => NativeArtifactError::ProviderRegistryLimitBeforePublish,
            Self::Model => NativeArtifactError::ModelRegistryLimitBeforePublish,
            Self::Conversation => NativeArtifactError::ConversationLimitBeforePublish,
        }
    }

    fn before_publish(self, error: StoreError) -> NativeArtifactError {
        match error {
            StoreError::Quota => self.limit(),
            error => CoreError::from(error).into(),
        }
    }

    fn unconfirmed(self, error: StoreError) -> NativeArtifactError {
        match self {
            Self::Provider => NativeArtifactError::ProviderRegistryPublicationUnconfirmed(error),
            Self::Model => NativeArtifactError::ModelRegistryPublicationUnconfirmed(error),
            Self::Conversation => NativeArtifactError::ConversationPublicationUnconfirmed(error),
        }
    }
}

impl NativeRegistry {
    pub(crate) fn open(
        invocation: &NativeSetupInvocation,
        root: &Path,
        profile_id: &str,
        kind: LegacyRegistryKind,
    ) -> Result<Self> {
        let profile = ProfileId::parse(profile_id).map_err(CoreError::from)?;
        let funding = invocation
            .reserve(CapacityKind::VfsBuffer, DOCUMENT_LIMIT as u64)
            .map_err(CoreError::from)?;
        Ok(Self {
            invocation: invocation.clone(),
            root: open_legacy_registry_root(root)?,
            profile,
            kind,
            locked: None,
            _raw_read_funding: funding,
        })
    }

    /// Read the original Registry without creating or locking it.
    pub fn observe(&self, budget: &Arc<dyn AllocationBudget>) -> Result<Option<CoreBuffer>> {
        if self.locked.is_some() {
            return Err(NativeArtifactError::Denied);
        }
        let bytes = match self.kind {
            LegacyRegistryKind::Provider => self
                .root
                .read_legacy_provider_registry(&self.profile, DOCUMENT_LIMIT),
            LegacyRegistryKind::Model => self
                .root
                .read_legacy_model_registry(&self.profile, DOCUMENT_LIMIT),
            LegacyRegistryKind::Conversation => self
                .root
                .read_legacy_conversation_store(&self.profile, DOCUMENT_LIMIT),
        };
        copy_document(
            bytes.map_err(|error| self.kind.before_publish(error))?,
            budget,
        )
    }

    /// Retain the original Registry observation and its cooperative lock until publication.
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
            let locked = match self.kind {
                LegacyRegistryKind::Provider => {
                    self.root.lock_legacy_provider_registry(&self.profile)
                }
                LegacyRegistryKind::Model => self.root.lock_legacy_model_registry(&self.profile),
                LegacyRegistryKind::Conversation => {
                    self.root.lock_legacy_conversation_store(&self.profile)
                }
            };
            match locked {
                Ok(transaction) => break transaction,
                Err(StoreError::Io(error)) if error.kind() == ErrorKind::AlreadyExists => {
                    thread::sleep(Duration::from_millis(10));
                }
                Err(error) => return Err(CoreError::from(error).into()),
            }
        };
        checkpoint()?;
        let bytes = transaction
            .read(DOCUMENT_LIMIT)
            .map_err(|error| self.kind.before_publish(error))?;
        let observed = copy_document(bytes, budget)?;
        checkpoint()?;
        self.locked = Some(LockedRegistry {
            transaction,
            observed: observed.clone(),
        });
        Ok(observed)
    }

    pub(crate) fn locked_conversation(
        &self,
        invocation: &NativeSetupInvocation,
    ) -> Result<(&str, &str)> {
        if !matches!(self.kind, LegacyRegistryKind::Conversation)
            || !self.invocation.same_invocation(invocation)
        {
            return Err(NativeArtifactError::Denied);
        }
        let observed = self
            .locked
            .as_ref()
            .and_then(|locked| locked.observed.as_ref())
            .ok_or(NativeArtifactError::Denied)?;
        let document = std::str::from_utf8(observed.as_bytes())
            .map_err(|error| NativeArtifactError::ConversationConfirmation(error.into()))?;
        Ok((self.profile.as_str(), document))
    }

    /// Publish one previously staged document, checking the current lease
    /// immediately before rename. The retained lock is never transferred.
    pub fn publish(self, document: &[u8], checkpoint: impl Fn() -> Result<()>) -> Result<()> {
        let locked = self.locked.as_ref().ok_or(NativeArtifactError::Denied)?;
        if document.len() > DOCUMENT_LIMIT {
            return Err(self.kind.limit());
        }
        checkpoint()?;
        match locked.transaction.publish(
            locked.observed.as_ref().map(CoreBuffer::as_bytes),
            document,
            DOCUMENT_LIMIT,
            &checkpoint,
        ) {
            Ok(()) => Ok(()),
            Err(LegacyRegistryPublishError::BeforePublish(error)) => {
                Err(self.kind.before_publish(error))
            }
            Err(LegacyRegistryPublishError::Guard(error)) => Err(error),
            Err(LegacyRegistryPublishError::Uncertain(error)) => {
                if locked
                    .transaction
                    .confirm_exact(document, DOCUMENT_LIMIT)
                    .unwrap_or(false)
                {
                    Ok(())
                } else {
                    Err(self.kind.unconfirmed(error))
                }
            }
        }
    }
}

/// The original owner read is fixed by native operation, never by guest input.
#[derive(Clone, Copy)]
pub(crate) enum LegacyReadDocument {
    Model,
    Conversation,
}

pub(crate) fn read_legacy_document(
    invocation: &NativeSetupInvocation,
    root: &Path,
    profile_id: &str,
    document: LegacyReadDocument,
    output: &mut [u8],
) -> Result<usize> {
    let profile = ProfileId::parse(profile_id).map_err(CoreError::from)?;
    let _funding = invocation
        .reserve(CapacityKind::VfsBuffer, DOCUMENT_LIMIT as u64)
        .map_err(CoreError::from)?;
    let root = open_legacy_registry_root(root)?;
    let bytes = match document {
        LegacyReadDocument::Model => root.read_legacy_model_registry(&profile, DOCUMENT_LIMIT),
        LegacyReadDocument::Conversation => {
            root.read_legacy_conversation_store(&profile, DOCUMENT_LIMIT)
        }
    }
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
    DataRoot::open_legacy_registry(root).map_err(|error| CoreError::from(error).into())
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
