//! Core's bounded read-only action at a reached native resolution operation.
//!
//! A trusted native adapter moves its actual resolver result here. Its original
//! allocations are retained, not rebuilt from client paths, pins or booleans.
//! Core selects a member of that result's declaration and owns the operation's
//! active state, invocation, jailed open, verification and physical cleanup.
//! This is separate from ordinary Profile permissions and early Setup reads.

use crate::{
    Core, CoreError,
    native_provider_registry::{LegacyReadDocument, LegacyRegistryKind},
};
use sha2::{Digest as _, Sha256};
use std::{
    collections::BTreeMap,
    io::{self, Read},
    path::Path,
    sync::{
        Arc, Mutex,
        atomic::{AtomicU8, Ordering},
    },
    time::Instant,
};
use tobkiri_canon::{
    AllocationBudget, ControlHandle, CoreBuffer, CoreValue, Digest, MAX_BYTES,
    ids::{OpName, ProfileId, ShapeRef},
};
use tobkiri_gate::{
    CapacityKind, ControlKind, ControlReservation, CoreCancellation, NativeSetupInvocation,
    NativeSetupReservation,
};
use tobkiri_manifest::{VerifiedPack, source::SourceCatalog, verify_tpk_reader_in_catalog};
use tobkiri_wasm::{
    LegacyRegistryError, NativeDataIo, NativeDataIoError, NativeInspectionOutput,
    NativeReadOnlyPort, NativeSetupStore,
};

pub use tobkiri_wasm::{LegacyProviderRegistryRead, LegacyRegistryRead};

#[path = "native_artifact_open_unix.rs"]
mod opening;
use opening::{OpenFailure, OpenedArtifact};

/// Borrowed declaration facts from the actual native resolver result.
/// A digest alone never authorizes access to a path supplied independently.
pub trait NativeArtifactDeclaration {
    /// Borrow the canonical SHA-256 identity recorded by the native resolver.
    fn digest(&self) -> &str;
}

/// Trusted native adapter over an already reached, owned resolver result.
///
/// Implementations must expose that same result's root and declaration map;
/// they must not reconstruct a selection from guest/client input. Construction
/// and invocation belong to the native composition, never a guest import.
pub trait NativeDeclaredArtifactSource: Send + Sync + 'static {
    /// The actual resolver's declaration record, without a copied catalog.
    type Declaration: NativeArtifactDeclaration;
    /// Borrow the selected root; its concrete FD binding is established at open.
    fn root(&self) -> &Path;
    /// Borrow the same resolved owner's map from declared names to identities.
    fn declarations(&self) -> &BTreeMap<String, Self::Declaration>;
}

/// No source bytes or host path are disclosed by these error descriptions.
#[derive(Debug, thiserror::Error)]
pub enum NativeArtifactError {
    #[error("native declared-artifact execution: {0}")]
    Execution(#[from] NativeDataIoError),
    #[error("native declared-artifact admission: {0}")]
    Core(#[from] CoreError),
    #[error("native artifact source IO failed")]
    SourceIo(#[source] io::Error),
    #[error("native artifact is not an eligible declared target")]
    Denied,
    #[error("native artifact changed or failed its declaration verification")]
    Integrity,
    #[error("native artifact exceeds the original DATA envelope")]
    TooLarge,
    #[error("native read-only port preparation failed")]
    ReadOnlyPort(#[source] Box<dyn std::error::Error + Send + Sync>),
    #[error("native Provider preparation failed")]
    ProviderPrepare(#[source] Box<dyn std::error::Error + Send + Sync>),
    #[error("native Provider commit validation failed")]
    ProviderCommitValidation(#[source] Box<dyn std::error::Error + Send + Sync>),
    #[error("native Provider commit staging failed")]
    ProviderCommitStaging(#[source] Box<dyn std::error::Error + Send + Sync>),
    #[error("legacy Provider Registry exceeds its native publication envelope")]
    ProviderRegistryLimitBeforePublish,
    #[error("legacy Provider Registry publication is unconfirmed")]
    ProviderRegistryPublicationUnconfirmed(#[source] tobkiri_store::StoreError),
    #[error("{0}")]
    ProviderRejected(#[from] LegacyRegistryError),
    #[error("native Model Registry projection failed")]
    ModelSettings(#[source] Box<dyn std::error::Error + Send + Sync>),
    #[error("native Model Registry resolution failed")]
    ModelResolve(#[source] Box<dyn std::error::Error + Send + Sync>),
    #[error("native Model Registry save staging failed")]
    ModelSaveStaging(#[source] Box<dyn std::error::Error + Send + Sync>),
    #[error("legacy Model Registry exceeds its native publication envelope")]
    ModelRegistryLimitBeforePublish,
    #[error("legacy Model Registry publication is unconfirmed")]
    ModelRegistryPublicationUnconfirmed(#[source] tobkiri_store::StoreError),
    #[error("{0}")]
    ModelRegistryRejected(#[source] LegacyRegistryError),
    #[error("native Conversation read failed")]
    ConversationRegistry(#[source] Box<dyn std::error::Error + Send + Sync>),
    #[error("{0}")]
    ConversationRegistryRejected(#[source] LegacyRegistryError),
    #[error("native Conversation completion staging failed")]
    ConversationConfirmation(#[source] Box<dyn std::error::Error + Send + Sync>),
    #[error("native Conversation append staging failed")]
    ConversationAppend(#[source] Box<dyn std::error::Error + Send + Sync>),
    #[error("legacy Conversation Store exceeds its native publication envelope")]
    ConversationLimitBeforePublish,
    #[error("legacy Conversation Store publication is unconfirmed")]
    ConversationPublicationUnconfirmed(#[source] tobkiri_store::StoreError),
}

const READY: u8 = 0;
const READING: u8 = 1;
const CLOSED: u8 = 2;

struct Owned<S> {
    source: Option<S>,
    files: Option<OpenedArtifact>,
    failure: Option<NativeArtifactError>,
}
struct Work<S> {
    // Actual native source and descriptors outlive an abandoned read future.
    owned: Mutex<Owned<S>>,
    phase: AtomicU8,
    invocation: NativeSetupInvocation,
    _core: Option<Arc<Core>>,
}
struct Funding {
    _native: NativeSetupReservation,
    _control: ControlReservation,
}
type Held<S> = ControlHandle<Work<S>>;

enum LegacyConversationLookup<'a> {
    Conversation(&'a str),
    SavedReceipt(&'a str),
}

/// Move-only native action owned by Core or its trusted native composition.
///
/// One invocation, Store, deadline, 64MiB account and physical IO slot serve all
/// reads. This is not a transferable filesystem capability or an FD registry.
pub struct NativeArtifactInspection<S: NativeDeclaredArtifactSource> {
    work: Held<S>,
    store: Option<NativeSetupStore<()>>,
    io: NativeDataIo,
    cancellation_parent: Option<CoreCancellation>,
}

impl Core {
    /// Admit the actual resolved native source for declared-artifact inspection.
    /// Requires the host's entered async runtime, not a new runtime per read.
    pub fn inspect_declared_artifacts<S: NativeDeclaredArtifactSource>(
        self: &Arc<Self>,
        source: S,
    ) -> Result<NativeArtifactInspection<S>, NativeArtifactError> {
        let invocation = self.native_artifact_invocation()?;
        NativeArtifactInspection::new(source, invocation, Some(self.clone()), None)
    }
}

impl<S: NativeDeclaredArtifactSource> NativeArtifactInspection<S> {
    /// Admit an actual resolved source using a trusted composition's original
    /// invocation and cancellation parent from the same control account.
    ///
    /// The caller keeps its runtime owner and worker alive through `finish`.
    /// Requires the caller's entered async runtime.
    pub fn from_invocation(
        source: S,
        invocation: NativeSetupInvocation,
        cancellation: &CoreCancellation,
    ) -> Result<Self, NativeArtifactError> {
        Self::new(source, invocation, None, Some(cancellation))
    }

    fn new(
        source: S,
        invocation: NativeSetupInvocation,
        core: Option<Arc<Core>>,
        cancellation: Option<&CoreCancellation>,
    ) -> Result<Self, NativeArtifactError> {
        let bytes = u64::try_from(Held::<S>::storage_bytes::<Funding>())
            .ok()
            // std's pinned Darwin Mutex lazily allocates one pthread_mutex_t.
            // Cover that requested storage before either allocation/first lock.
            .and_then(|n| n.checked_add(size_of::<libc::pthread_mutex_t>() as u64))
            .ok_or(NativeDataIoError::Capacity)?;
        let native = invocation
            .reserve(CapacityKind::VfsBuffer, bytes)
            .map_err(|_| NativeDataIoError::Capacity)?;
        let control = invocation
            .control()
            .reserve(ControlKind::Frame, bytes)
            .map_err(|_| NativeDataIoError::Capacity)?;
        let work = Held::try_new_precharged(
            Work {
                owned: Mutex::new(Owned {
                    source: Some(source),
                    files: None,
                    failure: None,
                }),
                phase: AtomicU8::new(READY),
                invocation: invocation.clone(),
                _core: core,
            },
            Funding {
                _native: native,
                _control: control,
            },
        )
        .map_err(|_| NativeDataIoError::Capacity)?;
        let engine = tobkiri_wasm::WasmEngine::new_with_control(invocation.control())
            .map_err(|_| NativeDataIoError::Unavailable)?;
        let store = match cancellation {
            Some(parent) => {
                engine.native_setup_store_with_cancellation_parent(invocation, (), parent)
            }
            None => engine.native_setup_store(invocation, ()),
        };
        let mut store = store.map_err(|_| NativeDataIoError::Unavailable)?;
        let io = store.bind_native_data_io().map_err(|error| {
            error
                .downcast_ref::<NativeDataIoError>()
                .copied()
                .unwrap_or(NativeDataIoError::Unavailable)
        })?;
        Ok(Self {
            work,
            store: Some(store),
            io,
            cancellation_parent: cancellation.cloned(),
        })
    }

    /// Pure borrowed facts for the original native domain validation. No IO or
    /// permission decision is delegated to this nonescaping callback.
    pub fn with_source<T>(
        &self,
        view: impl for<'source> FnOnce(&'source S) -> T,
    ) -> Result<T, NativeArtifactError> {
        self.require_ready()?;
        let owned = self
            .work
            .get()
            .owned
            .lock()
            .map_err(|_| NativeDataIoError::Unavailable)?;
        let source = owned.source.as_ref().ok_or(NativeArtifactError::Denied)?;
        let result = view(source);
        drop(owned);
        self.require_ready()?;
        Ok(result)
    }

    /// Borrow the inspection Store's graph budget for DATA processing in the
    /// same native read-only operation. Clones share the original capacity,
    /// deadline, cancellation, and retirement.
    pub fn allocation_budget(&self) -> Result<&Arc<dyn AllocationBudget>, NativeArtifactError> {
        self.require_ready()?;
        let store = self.store.as_ref().ok_or(NativeDataIoError::Retired)?;
        Ok(store.allocation_owners().graph())
    }

    /// Bind the original Host-selected user-data root to the fixed Registry
    /// operation. The root and Profile come from the Launcher resolver.
    pub fn legacy_provider_registry(
        &self,
        root: &Path,
        profile_id: &str,
    ) -> Result<crate::NativeProviderRegistry, NativeArtifactError> {
        self.legacy_registry(root, profile_id, LegacyRegistryKind::Provider)
    }

    /// Bind the fixed Model Registry and its original Host lock for save staging.
    pub fn legacy_model_registry(
        &self,
        root: &Path,
        profile_id: &str,
    ) -> Result<crate::NativeRegistry, NativeArtifactError> {
        self.legacy_registry(root, profile_id, LegacyRegistryKind::Model)
    }

    /// Bind the trusted Profile's original Conversation Store and lock for
    /// completion staging. The Launcher selects the root and authenticates it.
    pub fn legacy_conversation_store(
        &self,
        root: &Path,
        profile_id: &str,
    ) -> Result<crate::NativeRegistry, NativeArtifactError> {
        self.legacy_registry(root, profile_id, LegacyRegistryKind::Conversation)
    }

    fn legacy_registry(
        &self,
        root: &Path,
        profile_id: &str,
        kind: LegacyRegistryKind,
    ) -> Result<crate::NativeRegistry, NativeArtifactError> {
        self.require_ready()?;
        let registry =
            crate::NativeRegistry::open(&self.work.get().invocation, root, profile_id, kind)?;
        self.require_ready()?;
        Ok(registry)
    }

    /// Verify the trusted native adapter's exact archive stream using this
    /// inspection's control account and the unsigned local publisher policy.
    pub fn verify_pack_archive(
        &self,
        input: &mut dyn Read,
        metadata_bytes: u64,
    ) -> Result<VerifiedPack, NativeArtifactError> {
        self.require_ready()?;
        let verified =
            SourceCatalog::new(self.work.get().invocation.control()).and_then(|catalog| {
                verify_tpk_reader_in_catalog(input, metadata_bytes, &BTreeMap::new(), &catalog)
            });
        self.require_ready()?;
        verified.map_err(|error| NativeArtifactError::Core(error.into()))
    }

    /// Bind a fixed read-only port in this inspection's original Store.
    ///
    /// The trusted native caller supplies its authenticated Pack and active
    /// Profile and retains the authority for any later operation. After the
    /// single port call, finish this inspection to collect physical cleanup.
    pub async fn prepare_declared_read_only_port<'a>(
        &'a mut self,
        pack: &'a VerifiedPack,
        shape: &ShapeRef,
        operation: &OpName,
        profile: &ProfileId,
        deadline: Instant,
        max_wall_clock_calls: u8,
    ) -> Result<NativeReadOnlyPort<'a, ()>, NativeArtifactError> {
        self.claim_execution()?;
        let store = self.store.as_mut().ok_or(NativeDataIoError::Retired)?;
        store
            .prepare_declared_read_only_port(
                pack,
                shape,
                operation,
                profile,
                deadline,
                max_wall_clock_calls,
            )
            .await
            .map_err(|error| NativeArtifactError::ReadOnlyPort(error.into()))
    }

    /// Bind an ordered pair of read-only operations in this inspection's Store.
    /// Both use the authenticated Pack and Profile from the trusted caller and
    /// share the original execution budget. Finish the inspection on every path.
    pub async fn prepare_declared_read_only_pair<'a>(
        &'a mut self,
        pack: &'a VerifiedPack,
        declarations: [(&ShapeRef, &OpName); 2],
        profile: &ProfileId,
        deadline: Instant,
    ) -> Result<NativeReadOnlyPort<'a, ()>, NativeArtifactError> {
        self.claim_execution()?;
        let store = self.store.as_mut().ok_or(NativeDataIoError::Retired)?;
        store
            .prepare_declared_read_only_pair(pack, declarations, profile, deadline)
            .await
            .map_err(|error| NativeArtifactError::ReadOnlyPort(error.into()))
    }

    /// Execute read-only preparation in the same Store, obtaining original
    /// Registry observations in the order required by the fixed Wasm planner.
    /// The caller owns the activated Profile and must finish the inspection,
    /// even on failure. The plan carries no permission to save or execute it.
    pub async fn invoke_live_legacy_provider_prepare(
        &mut self,
        pack: &VerifiedPack,
        profile_id: &str,
        request: &CoreValue,
        read: LegacyProviderRegistryRead,
    ) -> Result<CoreValue, NativeArtifactError> {
        self.claim_execution()?;
        let store = self.store.as_mut().ok_or(NativeDataIoError::Retired)?;
        let plan = store
            .invoke_live_legacy_provider_prepare(pack, profile_id, request, read)
            .await;
        self.require_live()?;
        plan.map_err(|error| match error.downcast::<LegacyRegistryError>() {
            Ok(rejected) => NativeArtifactError::ProviderRejected(rejected),
            Err(error) => NativeArtifactError::ProviderPrepare(error.into()),
        })
    }

    /// Validate the frozen plan and record using the original Registry reader.
    /// The caller owns the approved lease and the later atomic state change.
    /// The validation Store has no write capability, even while it is live.
    pub async fn validate_live_legacy_provider_commit(
        &mut self,
        pack: &VerifiedPack,
        profile_id: &str,
        request: &CoreValue,
        plan: &CoreValue,
        read: LegacyProviderRegistryRead,
    ) -> Result<CoreValue, NativeArtifactError> {
        self.claim_execution()?;
        let store = self.store.as_mut().ok_or(NativeDataIoError::Retired)?;
        let validation = store
            .validate_live_legacy_provider_commit(pack, profile_id, request, plan, read)
            .await;
        self.require_live()?;
        validation.map_err(|error| match error.downcast::<LegacyRegistryError>() {
            Ok(rejected) => NativeArtifactError::ProviderRejected(rejected),
            Err(error) => NativeArtifactError::ProviderCommitValidation(error.into()),
        })
    }

    /// Derive the exact would-be replacement from four ordered observations.
    /// The caller supplies the final observation under the existing Registry
    /// lock and retains sole responsibility for any later mutation.
    pub async fn stage_live_legacy_provider_commit(
        &mut self,
        pack: &VerifiedPack,
        profile_id: &str,
        request: &CoreValue,
        plan: &CoreValue,
        read: LegacyProviderRegistryRead,
    ) -> Result<CoreValue, NativeArtifactError> {
        self.claim_execution()?;
        let store = self.store.as_mut().ok_or(NativeDataIoError::Retired)?;
        let staged = store
            .stage_live_legacy_provider_commit(pack, profile_id, request, plan, read)
            .await;
        self.require_live()?;
        staged.map_err(|error| match error.downcast::<LegacyRegistryError>() {
            Ok(rejected) => NativeArtifactError::ProviderRejected(rejected),
            Err(error) => NativeArtifactError::ProviderCommitStaging(error.into()),
        })
    }

    /// Stage a save against the original Model document while the native reader
    /// retains the lock. The caller supplies the authorized Provider snapshot.
    /// Only native custody may publish the staged document after physical cleanup.
    pub async fn stage_live_legacy_model_save(
        &mut self,
        pack: &VerifiedPack,
        profile_id: &str,
        request: &CoreValue,
        provider: &CoreValue,
        read: LegacyRegistryRead,
    ) -> Result<CoreValue, NativeArtifactError> {
        self.claim_execution()?;
        let store = self.store.as_mut().ok_or(NativeDataIoError::Retired)?;
        let staged = store
            .stage_live_legacy_model_save(pack, profile_id, request, provider, read)
            .await;
        self.require_live()?;
        staged.map_err(|error| match error.downcast::<LegacyRegistryError>() {
            Ok(rejected) => NativeArtifactError::ModelRegistryRejected(rejected),
            Err(error) => NativeArtifactError::ModelSaveStaging(error.into()),
        })
    }

    /// Stage the independent completion of a Host-read terminal Turn while the
    /// native caller retains the original Conversation lock. The guest returns
    /// a proposal, then native custody must check authority before publication.
    pub async fn stage_legacy_conversation_confirmation(
        &mut self,
        pack: &VerifiedPack,
        owner: &crate::NativeRegistry,
        turn: &str,
    ) -> Result<CoreValue, NativeArtifactError> {
        let (profile_id, document) = owner.locked_conversation(&self.work.get().invocation)?;
        self.claim_execution()?;
        let store = self.store.as_mut().ok_or(NativeDataIoError::Retired)?;
        let staged = store
            .stage_legacy_conversation_confirmation(pack, profile_id, document, turn)
            .await;
        self.require_live()?;
        staged.map_err(|error| match error.downcast::<LegacyRegistryError>() {
            Ok(rejected) => NativeArtifactError::ConversationRegistryRejected(rejected),
            Err(error) => NativeArtifactError::ConversationConfirmation(error.into()),
        })
    }

    /// Stage an append using the original document observed under this
    /// invocation's cooperative lock. Publication belongs to the lock owner.
    pub async fn stage_legacy_conversation_append(
        &mut self,
        pack: &VerifiedPack,
        owner: &crate::NativeRegistry,
        request: &str,
    ) -> Result<CoreValue, NativeArtifactError> {
        let (profile_id, document) = owner.locked_conversation(&self.work.get().invocation)?;
        self.claim_execution()?;
        let store = self.store.as_mut().ok_or(NativeDataIoError::Retired)?;
        let staged = store
            .stage_legacy_conversation_append(pack, profile_id, document, request)
            .await;
        self.require_live()?;
        staged.map_err(|error| match error.downcast::<LegacyRegistryError>() {
            Ok(rejected) => NativeArtifactError::ConversationRegistryRejected(rejected),
            Err(error) => NativeArtifactError::ConversationAppend(error.into()),
        })
    }

    /// Read the original Model Registry in place, then run the fixed Settings
    /// projection. The verified Pack and physical data root are selected by the
    /// trusted native caller. No file selection or write is exposed to the guest.
    pub async fn list_legacy_model_settings(
        &mut self,
        pack: &VerifiedPack,
        root: &Path,
        profile_id: &str,
    ) -> Result<CoreValue, NativeArtifactError> {
        self.require_ready()?;
        if pack.manifest.id.as_str() != "local.model-registry" {
            return Err(NativeArtifactError::Denied);
        }
        let framed = self
            .read_original_document(root, profile_id, LegacyReadDocument::Model)
            .await?;
        let document = original_document_text(&framed, |error| {
            NativeArtifactError::ModelSettings(Box::new(error))
        })?;
        self.require_live()?;
        let store = self.store.as_mut().ok_or(NativeDataIoError::Retired)?;
        let settings = store
            .list_legacy_model_settings(pack, profile_id, document)
            .await;
        self.require_live()?;
        settings.map_err(|error| match error.downcast::<LegacyRegistryError>() {
            Ok(rejected) => NativeArtifactError::ModelRegistryRejected(rejected),
            Err(error) => NativeArtifactError::ModelSettings(error.into()),
        })
    }

    /// Resolve a model in the active Profile's original Registry, preserving
    /// private owner metadata for the authorized native caller.
    pub async fn resolve_legacy_model(
        &mut self,
        pack: &VerifiedPack,
        root: &Path,
        profile_id: &str,
        identifier: &str,
    ) -> Result<CoreValue, NativeArtifactError> {
        self.require_ready()?;
        if pack.manifest.id.as_str() != "local.model-registry" {
            return Err(NativeArtifactError::Denied);
        }
        let framed = self
            .read_original_document(root, profile_id, LegacyReadDocument::Model)
            .await?;
        let document = original_document_text(&framed, |error| {
            NativeArtifactError::ModelResolve(Box::new(error))
        })?;
        self.require_live()?;
        let store = self.store.as_mut().ok_or(NativeDataIoError::Retired)?;
        let resolved = store
            .resolve_legacy_model(pack, profile_id, document, identifier)
            .await;
        self.require_live()?;
        resolved.map_err(|error| match error.downcast::<LegacyRegistryError>() {
            Ok(rejected) => NativeArtifactError::ModelRegistryRejected(rejected),
            Err(error) => NativeArtifactError::ModelResolve(error.into()),
        })
    }

    /// Load the active Profile's original Conversation Store in place.
    /// An authorized native caller chooses the Profile and Pack. The guest
    /// receives the original bytes and one conversation ID as data.
    pub async fn get_legacy_conversation(
        &mut self,
        pack: &VerifiedPack,
        root: &Path,
        profile_id: &str,
        conversation_id: &str,
    ) -> Result<CoreValue, NativeArtifactError> {
        self.lookup_legacy_conversation(
            pack,
            root,
            profile_id,
            LegacyConversationLookup::Conversation(conversation_id),
        )
        .await
    }

    /// Read original saved evidence in the active Profile. Settlement and
    /// publication remain with their existing authorized owners.
    pub async fn get_legacy_saved_receipt(
        &mut self,
        pack: &VerifiedPack,
        root: &Path,
        profile_id: &str,
        turn_id: &str,
    ) -> Result<CoreValue, NativeArtifactError> {
        self.lookup_legacy_conversation(
            pack,
            root,
            profile_id,
            LegacyConversationLookup::SavedReceipt(turn_id),
        )
        .await
    }

    async fn lookup_legacy_conversation(
        &mut self,
        pack: &VerifiedPack,
        root: &Path,
        profile_id: &str,
        lookup: LegacyConversationLookup<'_>,
    ) -> Result<CoreValue, NativeArtifactError> {
        self.require_ready()?;
        if pack.manifest.id.as_str() != "local.saved-conversation" {
            return Err(NativeArtifactError::Denied);
        }
        let framed = self
            .read_original_document(root, profile_id, LegacyReadDocument::Conversation)
            .await?;
        let document = original_document_text(&framed, |error| {
            NativeArtifactError::ConversationRegistry(Box::new(error))
        })?;
        self.require_live()?;
        let store = self.store.as_mut().ok_or(NativeDataIoError::Retired)?;
        let result = match lookup {
            LegacyConversationLookup::Conversation(id) => {
                store
                    .get_legacy_conversation(pack, profile_id, document, id)
                    .await
            }
            LegacyConversationLookup::SavedReceipt(id) => {
                store
                    .get_legacy_saved_receipt(pack, profile_id, document, id)
                    .await
            }
        };
        self.require_live()?;
        result.map_err(|error| match error.downcast::<LegacyRegistryError>() {
            Ok(rejected) => NativeArtifactError::ConversationRegistryRejected(rejected),
            Err(error) => NativeArtifactError::ConversationRegistry(error.into()),
        })
    }

    async fn read_original_document(
        &mut self,
        root: &Path,
        profile_id: &str,
        document: LegacyReadDocument,
    ) -> Result<CoreBuffer, NativeArtifactError> {
        use std::os::unix::ffi::OsStrExt;

        let budget = self.allocation_budget()?;
        let directory = CoreBuffer::copy_from(root.as_os_str().as_bytes(), budget)
            .map_err(|_| NativeDataIoError::Capacity)?;
        let profile = CoreBuffer::copy_from(profile_id.as_bytes(), budget)
            .map_err(|_| NativeDataIoError::Capacity)?;
        self.claim_execution()?;
        let held = self.work.clone();
        let framed = self
            .io
            .inspect_retaining(
                held.project(|work| work as &(dyn std::any::Any + Send + Sync)),
                move |output| {
                    inspect_original_document(&held, &directory, &profile, document, output)
                },
            )
            .await?;
        if let Some(error) = self
            .work
            .get()
            .owned
            .lock()
            .map_err(|_| NativeDataIoError::Unavailable)?
            .failure
            .take()
        {
            return Err(error);
        }
        Ok(framed)
    }

    fn claim_execution(&self) -> Result<(), NativeArtifactError> {
        self.require_ready()?;
        self.work
            .get()
            .phase
            .compare_exchange(READY, READING, Ordering::AcqRel, Ordering::Acquire)
            .map_err(|_| NativeDataIoError::Busy)?;
        Ok(())
    }

    fn require_live(&self) -> Result<(), NativeArtifactError> {
        let deadline = self
            .work
            .get()
            .invocation
            .deadline()
            .map_err(|_| NativeDataIoError::Unavailable)?;
        if std::time::Instant::now() >= deadline {
            return Err(NativeDataIoError::Deadline.into());
        }
        if self
            .store
            .as_ref()
            .is_some_and(|store| store.cancellation().is_cancelled())
        {
            return Err(NativeDataIoError::Cancelled.into());
        }
        Ok(())
    }

    fn require_ready(&self) -> Result<(), NativeArtifactError> {
        self.require_live()?;
        match self.work.get().phase.load(Ordering::Acquire) {
            READY => Ok(()),
            READING => Err(NativeDataIoError::Busy.into()),
            _ => Err(NativeDataIoError::Retired.into()),
        }
    }

    /// Read a name selected FROM this actual source's declaration, confined to
    /// its root as opened at this reachpoint. No independent path/digest pair.
    pub async fn read(&mut self, relative: &str) -> Result<CoreBuffer, NativeArtifactError> {
        self.require_ready()?;
        if !self.with_source(|source| source.declarations().contains_key(relative))? {
            return Err(NativeArtifactError::Denied);
        }
        // New queued key storage is prepaid under the original graph owner.
        let store = self.store.as_ref().ok_or(NativeDataIoError::Retired)?;
        let key = CoreBuffer::copy_from(relative.as_bytes(), store.allocation_owners().graph())
            .map_err(|_| NativeDataIoError::Capacity)?;
        self.work
            .get()
            .phase
            .compare_exchange(READY, READING, Ordering::AcqRel, Ordering::Acquire)
            .map_err(|_| NativeDataIoError::Busy)?;
        let held = self.work.clone();
        // On cancellation/error the original worker retains source/files and
        // phase stays non-admitting. finish() must drive physical completion.
        let bytes = self
            .io
            .inspect_retaining(
                held.project(|work| work as &(dyn std::any::Any + Send + Sync)),
                move |output| inspect_selected(&held, &key, output),
            )
            .await?;
        let mut owned = self
            .work
            .get()
            .owned
            .lock()
            .map_err(|_| NativeDataIoError::Unavailable)?;
        // inspect() returns successful DATA only AFTER its physical Join.
        drop(owned.files.take());
        let failure = owned.failure.take();
        self.work.get().phase.store(READY, Ordering::Release);
        match failure {
            Some(error) => Err(error),
            None => Ok(bytes),
        }
    }

    /// Stop admission, await the original physical Joins, then recover the same
    /// moved resolver result. Never recreate it by rereading a mutable source.
    pub async fn finish(mut self) -> Result<S, NativeArtifactError> {
        let deadline = self
            .work
            .get()
            .invocation
            .deadline()
            .map_err(|_| NativeDataIoError::Unavailable);
        let cancelled = self
            .store
            .as_ref()
            .is_some_and(|store| store.cancellation().is_cancelled());
        self.work.get().phase.store(CLOSED, Ordering::Release);
        if let Some(store) = self.store.take() {
            store.retire();
            drop(store);
        }
        self.io.retire_and_wait().await?;
        // Cleanup still waits after expiry/cancellation. Its successful Join
        // does not renew the action's deadline or authorize a late result.
        if cancelled
            || self
                .cancellation_parent
                .as_ref()
                .is_some_and(CoreCancellation::is_cancelled)
        {
            return Err(NativeDataIoError::Cancelled.into());
        }
        if std::time::Instant::now() >= deadline? {
            return Err(NativeDataIoError::Deadline.into());
        }
        let mut owned = self
            .work
            .get()
            .owned
            .lock()
            .map_err(|_| NativeDataIoError::Unavailable)?;
        drop(owned.files.take());
        owned.source.take().ok_or(NativeArtifactError::Denied)
    }
}
impl<S: NativeDeclaredArtifactSource> Drop for NativeArtifactInspection<S> {
    fn drop(&mut self) {
        self.work.get().phase.store(CLOSED, Ordering::Release);
        if let Some(store) = self.store.take() {
            store.retire();
            drop(store);
        }
        self.io.retire();
        // Dropping an observer cannot release a worker's source/file/funding.
        // Its clone of work remains owned by the original execution kernel.
    }
}

fn inspect_selected<S: NativeDeclaredArtifactSource>(
    held: &Held<S>,
    key: &CoreBuffer,
    output: &mut NativeInspectionOutput<'_>,
) -> Result<(), NativeDataIoError> {
    if !held.get().invocation.same_invocation(output.invocation())
        || held.get().phase.load(Ordering::Acquire) != READING
    {
        return Err(output.reject_execution(NativeDataIoError::Retired));
    }
    output.charge_work(1)?;
    let mut owned = held
        .get()
        .owned
        .lock()
        .map_err(|_| output.reject_execution(NativeDataIoError::Unavailable))?;
    let Owned {
        source,
        files,
        failure,
    } = &mut *owned;
    let source = source
        .as_ref()
        .ok_or_else(|| output.reject_execution(NativeDataIoError::Retired))?;
    let relative = key
        .as_str()
        .map_err(|_| output.reject_execution(NativeDataIoError::Unavailable))?;
    let Some((selected, declaration)) = source.declarations().get_key_value(relative) else {
        *failure = Some(NativeArtifactError::Denied);
        return Ok(());
    };
    let digest = declaration.digest();
    if !valid_digest(digest) {
        *failure = Some(NativeArtifactError::Integrity);
        return Ok(());
    }
    use std::os::unix::ffi::OsStrExt;
    let relative = Path::new(selected);
    let maximum = relative
        .components()
        .map(|c| c.as_os_str().as_bytes().len())
        .fold(source.root().as_os_str().as_bytes().len(), usize::max)
        .checked_add(1)
        .ok_or_else(|| output.reject_execution(NativeDataIoError::Capacity))?;
    // Scratch is privately staged and rewound; no pathname bytes are published.
    let opened = output.write_chunk_checked(maximum, |scratch, checkpoint| {
        *files = Some(
            opening::open_selected_file(source.root(), relative, scratch, &checkpoint)
                .map_err(map_open_failure)?,
        );
        Ok::<_, NativeArtifactError>(0)
    })?;
    if let Err(error) = opened {
        return record_failure(output, failure, error);
    }
    let opened = files
        .as_ref()
        .ok_or_else(|| output.reject_execution(NativeDataIoError::CompletionUnknown))?;
    let length = match usize::try_from(opened.before.len()) {
        Ok(length) if length <= MAX_BYTES => length,
        _ => return record_failure(output, failure, NativeArtifactError::TooLarge),
    };
    let read = output.write_chunk_checked(length, |tail, checkpoint| {
        read_verified(opened, tail, digest, &checkpoint)
    })?;
    match read {
        Ok(_) => Ok(()),
        Err(error) => record_failure(output, failure, error),
    }
}

fn original_document_text(
    framed: &CoreBuffer,
    invalid_utf8: impl FnOnce(std::str::Utf8Error) -> NativeArtifactError,
) -> Result<Option<&str>, NativeArtifactError> {
    match framed.as_bytes() {
        [0] => Ok(None),
        [1, raw @ ..] => std::str::from_utf8(raw).map(Some).map_err(invalid_utf8),
        _ => Err(NativeDataIoError::CompletionUnknown.into()),
    }
}

fn inspect_original_document<S: NativeDeclaredArtifactSource>(
    held: &Held<S>,
    directory: &CoreBuffer,
    profile: &CoreBuffer,
    document: LegacyReadDocument,
    output: &mut NativeInspectionOutput<'_>,
) -> Result<(), NativeDataIoError> {
    use crate::native_provider_registry::{DOCUMENT_LIMIT, read_legacy_document};
    use std::{ffi::OsStr, os::unix::ffi::OsStrExt};

    let work = held.get();
    if !work.invocation.same_invocation(output.invocation())
        || work.phase.load(Ordering::Acquire) != READING
    {
        return Err(output.reject_execution(NativeDataIoError::Retired));
    }
    let directory = Path::new(OsStr::from_bytes(directory.as_bytes()));
    let profile = profile
        .as_str()
        .map_err(|_| output.reject_execution(NativeDataIoError::Unavailable))?;
    let result = output.write_chunk_checked(DOCUMENT_LIMIT + 1, |tail, checkpoint| {
        checkpoint.charge_work(1)?;
        let read = read_legacy_document(&work.invocation, directory, profile, document, tail);
        checkpoint.charge_work(0)?;
        read
    })?;
    if let Err(error) = result {
        let mut owned = work
            .owned
            .lock()
            .map_err(|_| output.reject_execution(NativeDataIoError::Unavailable))?;
        return record_failure(output, &mut owned.failure, error);
    }
    Ok(())
}

fn record_failure(
    output: &mut NativeInspectionOutput<'_>,
    failure: &mut Option<NativeArtifactError>,
    error: NativeArtifactError,
) -> Result<(), NativeDataIoError> {
    match error {
        NativeArtifactError::Execution(error) => Err(output.reject_execution(error)),
        error => {
            *failure = Some(error);
            Ok(())
        }
    }
}
fn map_open_failure(error: OpenFailure) -> NativeArtifactError {
    match error {
        OpenFailure::InvalidTarget => NativeArtifactError::Denied,
        OpenFailure::SourceIo(error) => NativeArtifactError::SourceIo(error),
        OpenFailure::Execution(error) => NativeArtifactError::Execution(error),
    }
}
fn valid_digest(digest: &str) -> bool {
    digest.len() == 71
        && digest.starts_with("sha256:")
        && digest.as_bytes()[7..]
            .iter()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(b))
}

fn read_verified(
    opened: &OpenedArtifact,
    tail: &mut [u8],
    declared: &str,
    checkpoint: &tobkiri_wasm::NativeInspectionCheckpoint<'_>,
) -> Result<usize, NativeArtifactError> {
    use std::os::unix::fs::MetadataExt;
    // Retain and use this actual root/File, never a second pathname open.
    let _root_custody = &opened.root;
    let mut file = &opened.file;
    let mut read = 0;
    let mut hash = Sha256::new();
    loop {
        checkpoint.charge_work(1)?;
        // A one-byte stack probe verifies EOF even for an empty or full tail.
        let mut probe = [0_u8; 1];
        let result = if read == tail.len() {
            file.read(&mut probe)
        } else {
            file.read(&mut tail[read..])
        };
        checkpoint.charge_work(0)?;
        let count = match result {
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            result => result.map_err(NativeArtifactError::SourceIo)?,
        };
        if count == 0 {
            break;
        }
        if read == tail.len() {
            return Err(NativeArtifactError::Integrity);
        }
        hash.update(&tail[read..read + count]);
        read += count;
    }
    checkpoint.charge_work(1)?;
    let after = file.metadata();
    checkpoint.charge_work(0)?;
    let after = after.map_err(NativeArtifactError::SourceIo)?;
    let before = &opened.before;
    if !after.is_file()
        || after.dev() != before.dev()
        || after.ino() != before.ino()
        || after.len() != before.len()
        || after.nlink() != before.nlink()
        || after.mtime() != before.mtime()
        || after.mtime_nsec() != before.mtime_nsec()
        || read != tail.len()
        || Digest::from_sha256(hash.finalize().into()).as_str() != declared
    {
        return Err(NativeArtifactError::Integrity);
    }
    Ok(read)
}

#[cfg(all(test, target_os = "macos", target_arch = "aarch64"))]
#[path = "native_declared_artifact_tests.rs"]
mod tests;
