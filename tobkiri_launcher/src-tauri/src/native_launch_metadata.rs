//! Original Launch metadata, collected with the RuntimeOwner used during Setup.

use std::{
    io,
    panic::{catch_unwind, AssertUnwindSafe},
    sync::{atomic::Ordering, mpsc},
    time::{Duration, Instant},
};

use anyhow::{anyhow, bail, ensure, Context, Result};
use tauri::{AppHandle, Manager};
use tobkiri_core::{
    ids::{OpName, ShapeRef},
    pack::{RuntimeSpec, ShapeKind, VerifiedPack},
    Digest, NativeArtifactInspection, NativeSetupInvocation,
};
use tobkiri_gate::{ControlKind, CoreCancellation, PermissionSet};
use tobkiri_harness_native::{
    select_original_saved_turn_settlement, OriginalActivationInspection, OriginalActiveProfile,
    OriginalConversationConfirmationChoice, OriginalConversationReadSelection,
    OriginalModelResolveRole, OriginalModelResolveSelection, OriginalModelSaveSelection,
    OriginalModelSettingsSelection, OriginalProviderPrepareSelection, OriginalRoutePin,
    OriginalSavedReceiptRole, OriginalSavedReceiptSelection,
};
use tobkiri_wasm::{
    verify_legacy_conversation_confirmation_declaration,
    verify_legacy_conversation_read_declaration, verify_legacy_conversation_receipt_declaration,
    verify_legacy_model_resolve_declaration, verify_legacy_model_save_declaration,
};

use super::{
    authority, caught, selected_setup_component, JobTicket, NativeRuntimeState,
    OriginalBootstrapInputs, WorkerExit,
};
use crate::{
    defaultspack_authority::{
        require_current_launch_identity, NativeApplicationSources, NativeSourceRoot,
        SignedApplicationResolver,
    },
    dock_registration::{
        assemble_defaultspack_desktop_metadata, DefaultspackDesktopMetadata, LaunchMetadataUse,
    },
    host_contract::ExecutionProfileIdentity,
    native_declared_artifacts::collect_for_native_launch,
    preparation_budget, registry_pack_receipt, AppConfig, ShutdownState,
};

/// The signed Shell-to-Turn grant authorizing this settlement caller.
pub(crate) use tobkiri_harness_native::OriginalSavedTurnSettlementRole as SettlementCaller;

const REGISTRY_RECEIPT_SHA256: &str = env!("TOBKIRI_REGISTRY_PACK_RECEIPT_SHA256");
// Allow the bounded receipt decoder and streaming SHA-256 work to run only
// when their fixed working set fits the real Launch job's control ledger.
pub(super) const REGISTRY_VERIFICATION_BYTES: u64 = 256 * 1024;

/// Detached launch evidence for the verified legacy routes and their bundled Wasm Packs.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct VerifiedRegistryLaunch {
    pub(crate) identity: ExecutionProfileIdentity,
    pub(crate) security_epoch: u64,
    pub(crate) catalog_revision: String,
    pub(crate) provider_prepare: Option<VerifiedProviderPrepareSource>,
    pub(crate) model_settings: Option<VerifiedModelSettingsSource>,
    pub(crate) model_save: Option<VerifiedModelSaveSource>,
    model_resolve: Option<VerifiedModelResolveSource>,
    conversation: Option<VerifiedConversationSource>,
    turn_settlement: Option<VerifiedTurnSettlementSource>,
}

/// The caller of the original conversation-owner read grant.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum ConversationReadCaller {
    /// The selected Shell calling the owner directly.
    Shell,
    /// The original Saved Turn function calling the owner.
    SavedTurn,
    /// The original Saved Orchestration function calling the owner.
    SavedOrchestration,
}

impl VerifiedRegistryLaunch {
    /// Require the Gateway role's own signed grant against the verified Model Pack.
    pub(crate) fn permits_model_resolve(&self, role: OriginalModelResolveRole) -> bool {
        self.model_resolve
            .as_ref()
            .is_some_and(|source| source.permits(role))
    }

    /// Require the caller's own legacy read edge against the verified replacement.
    pub(crate) fn permits_conversation_read(&self, caller: ConversationReadCaller) -> bool {
        self.conversation
            .as_ref()
            .is_some_and(|source| source.permits_read(caller))
    }

    /// Require the caller's original signed Turn grant and verified settlement Pack.
    pub(crate) fn permits_turn_settlement(&self, role: SettlementCaller) -> bool {
        self.turn_settlement
            .as_ref()
            .is_some_and(|source| source.permits(role))
    }

    /// Require the receipt caller's own legacy edge and verified receipt declaration.
    pub(crate) fn permits_saved_receipt(&self, role: OriginalSavedReceiptRole) -> bool {
        self.conversation
            .as_ref()
            .is_some_and(|source| source.permits_saved_receipt(role))
    }

    /// Require the exact Shell, Turn, and Conversation grants for this caller.
    pub(crate) fn permits_conversation_confirmation(&self, role: OriginalSavedReceiptRole) -> bool {
        self.conversation
            .as_ref()
            .is_some_and(|source| source.permits_confirmation(role))
    }
}

/// Signed legacy Provider route pins and the receipt identity of its Wasm Pack.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct VerifiedProviderPrepareSource {
    pub(crate) coordinator: VerifiedLegacyRoutePin,
    pub(crate) provider: VerifiedLegacyRoutePin,
    pub(crate) archive_sha256: String,
    pub(crate) archive_bytes: u64,
    pub(crate) manifest_sha256: String,
}

/// Signed legacy Model list pin and the receipt identity of its Wasm Pack.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct VerifiedModelSettingsSource {
    pub(crate) model_list: VerifiedLegacyRoutePin,
    pub(crate) archive_sha256: String,
    pub(crate) archive_bytes: u64,
    pub(crate) manifest_sha256: String,
}

/// The two signed Model Save edges and the receipt identity of their Wasm replacement.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct VerifiedModelSaveSource {
    pub(crate) model_manage: VerifiedLegacyRoutePin,
    pub(crate) provider_read: VerifiedLegacyRoutePin,
    pub(crate) archive_sha256: String,
    pub(crate) archive_bytes: u64,
    pub(crate) manifest_sha256: String,
}

/// Independent Gateway role pins and the exact Model Pack receipt they share.
#[derive(Clone, Debug, PartialEq, Eq)]
struct VerifiedModelResolveSource {
    roles: [Option<(OriginalModelResolveRole, VerifiedLegacyRoutePin)>; 4],
    archive_sha256: String,
    archive_bytes: u64,
    manifest_sha256: String,
}

impl VerifiedModelResolveSource {
    fn permits(&self, role: OriginalModelResolveRole) -> bool {
        self.roles
            .iter()
            .flatten()
            .any(|(granted, _)| *granted == role)
    }
}

/// Separate signed conversation-owner edges and the receipt identity they share.
#[derive(Clone, Debug, PartialEq, Eq)]
struct VerifiedConversationSource {
    shell: Option<VerifiedLegacyRoutePin>,
    saved_turn: Option<VerifiedLegacyRoutePin>,
    saved_orchestration: Option<VerifiedLegacyRoutePin>,
    saved_receipt: [Option<(OriginalSavedReceiptRole, VerifiedLegacyRoutePin)>; 2],
    confirmation: VerifiedConversationConfirmationRoles,
    archive_sha256: String,
    archive_bytes: u64,
    manifest_sha256: String,
}

type VerifiedConversationConfirmationRoles =
    [Option<(OriginalSavedReceiptRole, [VerifiedLegacyRoutePin; 3])>; 2];

impl VerifiedConversationSource {
    fn permits_read(&self, caller: ConversationReadCaller) -> bool {
        match caller {
            ConversationReadCaller::Shell => self.shell.is_some(),
            ConversationReadCaller::SavedTurn => self.saved_turn.is_some(),
            ConversationReadCaller::SavedOrchestration => self.saved_orchestration.is_some(),
        }
    }

    fn permits_saved_receipt(&self, role: OriginalSavedReceiptRole) -> bool {
        self.saved_receipt
            .iter()
            .flatten()
            .any(|(granted, _)| *granted == role)
    }

    fn permits_confirmation(&self, role: OriginalSavedReceiptRole) -> bool {
        self.confirmation
            .iter()
            .flatten()
            .any(|(granted, _)| *granted == role)
    }
}

/// Separate signed Shell-to-Turn grants and the receipt pin for the settlement Pack.
#[derive(Clone, Debug, PartialEq, Eq)]
struct VerifiedTurnSettlementSource {
    roles: [Option<(SettlementCaller, VerifiedLegacyRoutePin)>; 2],
    archive_sha256: String,
    archive_bytes: u64,
    manifest_sha256: String,
}

impl VerifiedTurnSettlementSource {
    fn permits(&self, role: SettlementCaller) -> bool {
        self.roles
            .iter()
            .flatten()
            .any(|(granted, _)| *granted == role)
    }
}

/// Exact legacy binding digests confirmed against the activation and signed Bundle.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct VerifiedLegacyRoutePin {
    pub(crate) artifact_digest: String,
    pub(crate) executable_catalog_digest: String,
    pub(crate) contract_revision_digest: String,
    pub(crate) function_implementation_digest: String,
    pub(crate) authority_reference: String,
    pub(crate) requested_scope_digest: String,
}

impl From<&OriginalRoutePin> for VerifiedLegacyRoutePin {
    fn from(pin: &OriginalRoutePin) -> Self {
        Self {
            artifact_digest: pin.artifact_digest().to_owned(),
            executable_catalog_digest: pin.executable_catalog_digest().to_owned(),
            contract_revision_digest: pin.contract_revision_digest().to_owned(),
            function_implementation_digest: pin.function_implementation_digest().to_owned(),
            authority_reference: pin.authority_reference().to_owned(),
            requested_scope_digest: pin.requested_scope_digest().to_owned(),
        }
    }
}

#[cfg(test)]
impl VerifiedRegistryLaunch {
    pub(crate) fn test_proof(identity: ExecutionProfileIdentity, catalog_revision: &str) -> Self {
        Self {
            identity,
            security_epoch: 7,
            catalog_revision: catalog_revision.to_owned(),
            provider_prepare: Some(VerifiedProviderPrepareSource {
                coordinator: VerifiedLegacyRoutePin::fixture(),
                provider: VerifiedLegacyRoutePin::fixture(),
                archive_sha256: "sha256:archive".into(),
                archive_bytes: 512,
                manifest_sha256: "sha256:manifest".into(),
            }),
            model_settings: None,
            model_save: None,
            model_resolve: None,
            conversation: None,
            turn_settlement: None,
        }
    }

    pub(crate) fn test_model_proof(
        identity: ExecutionProfileIdentity,
        catalog_revision: &str,
    ) -> Self {
        Self {
            identity,
            security_epoch: 7,
            catalog_revision: catalog_revision.to_owned(),
            provider_prepare: None,
            model_settings: Some(VerifiedModelSettingsSource {
                model_list: VerifiedLegacyRoutePin::fixture(),
                archive_sha256: "sha256:archive".into(),
                archive_bytes: 512,
                manifest_sha256: "sha256:manifest".into(),
            }),
            model_save: None,
            model_resolve: None,
            conversation: None,
            turn_settlement: None,
        }
    }

    pub(crate) fn test_model_save_proof(
        identity: ExecutionProfileIdentity,
        catalog_revision: &str,
    ) -> Self {
        Self {
            identity,
            security_epoch: 7,
            catalog_revision: catalog_revision.to_owned(),
            provider_prepare: None,
            model_settings: None,
            model_save: Some(VerifiedModelSaveSource {
                model_manage: VerifiedLegacyRoutePin::fixture(),
                provider_read: VerifiedLegacyRoutePin::fixture(),
                archive_sha256: "sha256:archive".into(),
                archive_bytes: 512,
                manifest_sha256: "sha256:manifest".into(),
            }),
            model_resolve: None,
            conversation: None,
            turn_settlement: None,
        }
    }

    pub(crate) fn test_model_resolve_proof(
        identity: ExecutionProfileIdentity,
        catalog_revision: &str,
        role: OriginalModelResolveRole,
    ) -> Self {
        Self {
            identity,
            security_epoch: 7,
            catalog_revision: catalog_revision.to_owned(),
            provider_prepare: None,
            model_settings: None,
            model_save: None,
            model_resolve: Some(VerifiedModelResolveSource {
                roles: [
                    Some((role, VerifiedLegacyRoutePin::fixture())),
                    None,
                    None,
                    None,
                ],
                archive_sha256: "sha256:archive".into(),
                archive_bytes: 512,
                manifest_sha256: "sha256:manifest".into(),
            }),
            conversation: None,
            turn_settlement: None,
        }
    }

    pub(crate) fn test_saved_receipt_proof(
        identity: ExecutionProfileIdentity,
        catalog_revision: &str,
        role: OriginalSavedReceiptRole,
    ) -> Self {
        Self {
            identity,
            security_epoch: 7,
            catalog_revision: catalog_revision.to_owned(),
            provider_prepare: None,
            model_settings: None,
            model_save: None,
            model_resolve: None,
            conversation: Some(VerifiedConversationSource {
                shell: None,
                saved_turn: None,
                saved_orchestration: None,
                saved_receipt: [Some((role, VerifiedLegacyRoutePin::fixture())), None],
                confirmation: [None, None],
                archive_sha256: "sha256:archive".into(),
                archive_bytes: 512,
                manifest_sha256: "sha256:manifest".into(),
            }),
            turn_settlement: None,
        }
    }

    pub(crate) fn test_conversation_confirmation_proof(
        identity: ExecutionProfileIdentity,
        catalog_revision: &str,
        role: OriginalSavedReceiptRole,
    ) -> Self {
        let mut proof = Self::test_saved_receipt_proof(identity, catalog_revision, role);
        let source = proof.conversation.as_mut().unwrap();
        source.saved_receipt = [None, None];
        source.confirmation = [
            Some((
                role,
                std::array::from_fn(|_| VerifiedLegacyRoutePin::fixture()),
            )),
            None,
        ];
        proof
    }

    pub(crate) fn test_turn_settlement_proof(
        identity: ExecutionProfileIdentity,
        catalog_revision: &str,
        role: SettlementCaller,
    ) -> Self {
        Self {
            identity,
            security_epoch: 7,
            catalog_revision: catalog_revision.to_owned(),
            provider_prepare: None,
            model_settings: None,
            model_save: None,
            model_resolve: None,
            conversation: None,
            turn_settlement: Some(VerifiedTurnSettlementSource {
                roles: [Some((role, VerifiedLegacyRoutePin::fixture())), None],
                archive_sha256: "sha256:archive".into(),
                archive_bytes: 512,
                manifest_sha256: "sha256:manifest".into(),
            }),
        }
    }
}

#[cfg(test)]
impl VerifiedLegacyRoutePin {
    fn fixture() -> Self {
        Self {
            artifact_digest: "sha256:artifact".into(),
            executable_catalog_digest: "sha256:catalog".into(),
            contract_revision_digest: "sha256:contract".into(),
            function_implementation_digest: "sha256:implementation".into(),
            authority_reference: "authority-ref:fixture".into(),
            requested_scope_digest: "sha256:scope".into(),
        }
    }
}

pub(crate) fn registry_pack_receipt_bound() -> bool {
    !REGISTRY_RECEIPT_SHA256.is_empty()
}

pub(crate) fn read_launch_metadata(
    app: &AppHandle,
    deadline: Instant,
    purpose: LaunchMetadataUse,
) -> Result<DefaultspackDesktopMetadata> {
    preparation_budget::check(deadline)?;
    let runtime = app
        .try_state::<NativeRuntimeState>()
        .context("native Launch runtime owner is unavailable")?
        .inner()
        .clone();
    let shutdown = app
        .try_state::<ShutdownState>()
        .context("native Launch shutdown state is unavailable")?;
    let jobs = runtime.0.jobs.clone();
    let app = app.clone();
    let (send, receive) = mpsc::sync_channel(1);
    jobs.spawn(move |job| {
        let (result, cleanup_verified) = catch_unwind(AssertUnwindSafe(|| {
            tauri::async_runtime::block_on(collect_on_worker(
                &app, &runtime, &job, deadline, purpose,
            ))
        }))
        .unwrap_or_else(|_| (Err(anyhow!("native Launch metadata worker failed")), false));
        if !cleanup_verified {
            runtime.0.jobs.cleanup_failed();
        }
        let _ = send.send(result);
        WorkerExit { cleanup_verified }
    })?;
    loop {
        if shutdown.inner().0.load(Ordering::Acquire) {
            bail!("native Launch metadata observation stopped during shutdown");
        }
        let timeout = preparation_budget::remaining(deadline)?.min(Duration::from_millis(100));
        match receive.recv_timeout(timeout) {
            Ok(result) => {
                preparation_budget::check(deadline)?;
                return result;
            }
            Err(mpsc::RecvTimeoutError::Timeout) => {}
            Err(error) => {
                return Err(error).context("native Launch metadata worker is unavailable")
            }
        }
    }
}

async fn collect_on_worker(
    app: &AppHandle,
    runtime: &NativeRuntimeState,
    job: &JobTicket,
    deadline: Instant,
    purpose: LaunchMetadataUse,
) -> (Result<DefaultspackDesktopMetadata>, bool) {
    let admitted = (|| {
        preparation_budget::check(deadline)?;
        let config = app
            .try_state::<AppConfig>()
            .context("native Launch configuration is unavailable")?;
        let sources = SignedApplicationResolver::resolve_sources(&config)?;
        let identity = sources.authority().execution_identity()?;
        preparation_budget::check(deadline)?;
        let invocation = runtime.begin(app, job)?;
        let cancellation = job.attach_invocation(&invocation)?;
        Ok::<_, anyhow::Error>((config, sources, identity, invocation, cancellation))
    })();
    let (config, sources, identity, invocation, cancellation) = match admitted {
        Ok(admitted) => admitted,
        Err(error) => return (Err(error), true),
    };
    let observe_activation =
        matches!(purpose, LaunchMetadataUse::Shell) && registry_pack_receipt_bound();
    let (result, cleanup_verified) = {
        let observer = LaunchActivationObserver {
            app,
            runtime,
            job,
            invocation: &invocation,
            cancellation: &cancellation,
            identity: &identity,
            deadline,
        };
        let collection = collect_for_native_launch(
            sources,
            invocation.clone(),
            &cancellation,
            observe_activation,
            async |inspection| observer.read(inspection).await,
        );
        tokio::pin!(collection);
        tokio::select! {
            result = &mut collection => result,
            () = tokio::time::sleep_until(deadline.into()) => {
                cancellation.cancel();
                // Keep the original worker and descriptors until physical cleanup.
                collection.await
            }
        }
    };
    let metadata = result.and_then(|(sources, contributions, proof)| {
        preparation_budget::check(deadline)?;
        require_current_launch_identity(&config, &identity)?;
        preparation_budget::check(deadline)?;
        job.ensure_accepting(&runtime.0.jobs)?;
        let mut metadata = assemble_defaultspack_desktop_metadata(
            &config,
            sources.into_authority(),
            identity,
            contributions,
        )?;
        if cleanup_verified && proof.is_some() {
            preparation_budget::check(deadline)?;
            job.ensure_accepting(&runtime.0.jobs)?;
            metadata.native_provider_prepare = proof;
        }
        Ok(metadata)
    });
    (metadata, cleanup_verified)
}

struct LaunchActivationObserver<'a> {
    app: &'a AppHandle,
    runtime: &'a NativeRuntimeState,
    job: &'a JobTicket,
    invocation: &'a NativeSetupInvocation,
    cancellation: &'a CoreCancellation,
    identity: &'a ExecutionProfileIdentity,
    deadline: Instant,
}

impl LaunchActivationObserver<'_> {
    async fn read(
        &self,
        inspection: &NativeArtifactInspection<NativeApplicationSources>,
    ) -> (Result<Option<VerifiedRegistryLaunch>>, bool) {
        let Self {
            app,
            runtime,
            job,
            invocation,
            cancellation,
            deadline,
            ..
        } = *self;
        let admitted = (|| {
            preparation_budget::check(deadline)?;
            job.ensure_accepting(&runtime.0.jobs)?;
            let budget = inspection.allocation_budget()?;
            let (schemas, digest) = selected_setup_component::activation_schemas()?;
            let read = OriginalActivationInspection::new(invocation)?;
            let control = invocation.control().reserve(
                ControlKind::Registration,
                (2 * std::mem::size_of::<authority::Retained>() + 1024) as u64,
            )?;
            let source =
                OriginalBootstrapInputs::new(app.clone(), job.source_custody()?, invocation, None)?;
            Ok::<_, anyhow::Error>((source, read, control, budget, schemas, digest))
        })();
        let (source, read, control, budget, schemas, digest) = match admitted {
            Ok(admitted) => admitted,
            Err(error) => return (Err(error), true),
        };

        let observed =
            caught(read.read(&source, cancellation, budget, schemas, &digest, deadline)).await;
        let receipt = read.close_receipt();
        if let Some(cleanup) = read.uncertain_cleanup() {
            runtime
                .0
                .retained_authority
                .lock()
                .unwrap_or_else(|error| error.into_inner())
                .push(authority::Retained::new(
                    cleanup,
                    source,
                    invocation.clone(),
                    cancellation.clone(),
                    control,
                ));
        }
        if !receipt.verified() {
            return (
                Err(anyhow!("native Launch Authority cleanup is uncertain")),
                false,
            );
        }

        let active = match observed {
            Ok(Ok(active)) => Some(active),
            Ok(Err(error)) => {
                // The original Host remains responsible for pending recovery.
                // Only a successful observation can supply native activation data.
                log::debug!(
                    "current v5 Authority observation is unavailable at {:?}",
                    error.stage
                );
                None
            }
            Err(error) => return (Err(anyhow!(error)), false),
        };
        (self.verify_registry(inspection, active.as_ref()), true)
    }

    fn verify_registry(
        &self,
        inspection: &NativeArtifactInspection<NativeApplicationSources>,
        active: Option<&OriginalActiveProfile>,
    ) -> Result<Option<VerifiedRegistryLaunch>> {
        let checkpoint = || {
            preparation_budget::check(self.deadline)?;
            self.job.ensure_accepting(&self.runtime.0.jobs)?;
            inspection.allocation_budget()?;
            Ok(())
        };
        checkpoint()?;
        let budget = inspection.allocation_budget()?;
        let _funding = self
            .invocation
            .control()
            .reserve(ControlKind::Frame, REGISTRY_VERIFICATION_BYTES)?;
        let provider_selection = active
            .map(|active| OriginalProviderPrepareSelection::select(active, budget))
            .transpose()?
            .flatten();
        let model_selection = active
            .map(|active| OriginalModelSettingsSelection::select(active, budget))
            .transpose()?
            .flatten();
        let model_save_selection = active
            .map(|active| OriginalModelSaveSelection::select(active, budget))
            .transpose()?
            .flatten();
        let model_resolve_selection = active
            .map(|active| OriginalModelResolveSelection::select(active, budget))
            .transpose()?
            .flatten();
        let conversation_read_selection = active
            .map(|active| OriginalConversationReadSelection::select(active, budget))
            .transpose()?
            .flatten();
        let saved_receipt_selection = active
            .map(|active| OriginalSavedReceiptSelection::select(active, budget))
            .transpose()?
            .flatten();
        let has_existing_grants = provider_selection.is_some()
            || model_selection.is_some()
            || model_save_selection.is_some()
            || model_resolve_selection.is_some()
            || conversation_read_selection.is_some()
            || saved_receipt_selection.is_some();
        let (app_root, catalog_revision, locked) = inspection.with_source(|sources| {
            let catalog_revision = &sources.authority().catalog_revision;
            if let Some(active) = active {
                validate_observed_activation(active, self.identity, catalog_revision)?;
            }
            let locked = has_existing_grants
                .then(|| {
                    sources.registry_bundle(
                        provider_selection.as_ref(),
                        model_selection.as_ref(),
                        model_save_selection.as_ref(),
                        model_resolve_selection.as_ref(),
                        (
                            conversation_read_selection.as_ref(),
                            saved_receipt_selection.as_ref(),
                        ),
                        budget,
                    )
                })
                .transpose()?;
            Ok::<_, anyhow::Error>((
                sources.root(NativeSourceRoot::Application).to_path_buf(),
                catalog_revision.clone(),
                locked,
            ))
        })??;
        let Some(active) = active else {
            registry_pack_receipt::verify_installed(&app_root, REGISTRY_RECEIPT_SHA256, || {
                checkpoint().map_err(io::Error::other)
            })
            .context("bundled Registry Pack verification failed during Launch")?;
            return Ok(None);
        };
        if let Some(locked) = locked {
            locked.verify(checkpoint)?;
        }
        let verified_registry_pack_evidence =
            registry_pack_receipt::verify_installed_with_settlement_archives(
                &app_root,
                REGISTRY_RECEIPT_SHA256,
                || checkpoint().map_err(io::Error::other),
                |receipt, input| {
                    provider_selection
                        .as_ref()
                        .map(|selection| {
                            verify_provider_prepare_archive(inspection, receipt, input)?;
                            Ok(VerifiedProviderPrepareSource {
                                coordinator: selection.coordinator().into(),
                                provider: selection.provider().into(),
                                archive_sha256: receipt.archive_sha256().to_owned(),
                                archive_bytes: receipt.archive_bytes(),
                                manifest_sha256: receipt.manifest_sha256().to_owned(),
                            })
                        })
                        .transpose()
                },
                |receipt, input| {
                    let model_list = model_selection.as_ref();
                    let model_save = model_save_selection.as_ref();
                    let model_resolve = model_resolve_selection.as_ref();
                    if model_list.is_none() && model_save.is_none() && model_resolve.is_none() {
                        return Ok((None, None, None));
                    }
                    let pack = verify_registry_archive(inspection, receipt, input)?;
                    let list = model_list
                        .map(|selection| {
                            require_model_list_port(&pack)?;
                            Ok::<_, io::Error>(VerifiedModelSettingsSource {
                                model_list: selection.model_list().into(),
                                archive_sha256: receipt.archive_sha256().to_owned(),
                                archive_bytes: receipt.archive_bytes(),
                                manifest_sha256: receipt.manifest_sha256().to_owned(),
                            })
                        })
                        .transpose()?;
                    let save = model_save
                        .map(|selection| {
                            verify_legacy_model_save_declaration(&pack)
                                .map_err(io::Error::other)?;
                            Ok::<_, io::Error>(VerifiedModelSaveSource {
                                model_manage: selection.model_manage().into(),
                                provider_read: selection.provider_read().into(),
                                archive_sha256: receipt.archive_sha256().to_owned(),
                                archive_bytes: receipt.archive_bytes(),
                                manifest_sha256: receipt.manifest_sha256().to_owned(),
                            })
                        })
                        .transpose()?;
                    let resolve = model_resolve
                        .map(|selection| {
                            verify_legacy_model_resolve_declaration(&pack)
                                .map_err(io::Error::other)?;
                            use OriginalModelResolveRole::{
                                Generate, QuoteGenerate, QuoteStream, Stream,
                            };
                            let roles = [QuoteGenerate, QuoteStream, Generate, Stream]
                                .map(|role| selection.pin(role).map(|pin| (role, pin.into())));
                            Ok::<_, io::Error>(VerifiedModelResolveSource {
                                roles,
                                archive_sha256: receipt.archive_sha256().to_owned(),
                                archive_bytes: receipt.archive_bytes(),
                                manifest_sha256: receipt.manifest_sha256().to_owned(),
                            })
                        })
                        .transpose()?;
                    Ok((list, save, resolve))
                },
                |receipt, input| {
                    let read = conversation_read_selection.as_ref();
                    let saved_receipt = saved_receipt_selection.as_ref();
                    let pack = verify_registry_archive(inspection, receipt, input)?;
                    if read.is_some() {
                        verify_legacy_conversation_read_declaration(&pack)
                            .map_err(io::Error::other)?;
                    }
                    if saved_receipt.is_some() {
                        verify_legacy_conversation_receipt_declaration(&pack)
                            .map_err(io::Error::other)?;
                    }
                    let confirmation_declared =
                        observe_conversation_confirmation_declaration(&pack)
                            .map_err(io::Error::other)?;
                    drop(pack);
                    let confirmation = if confirmation_declared {
                        verify_conversation_confirmation_source(inspection, active, checkpoint)
                            .map_err(io::Error::other)?
                    } else {
                        [None, None]
                    };
                    if read.is_none()
                        && saved_receipt.is_none()
                        && confirmation.iter().all(Option::is_none)
                    {
                        return Ok(None);
                    }
                    Ok(Some(VerifiedConversationSource {
                        shell: read.and_then(|selection| selection.shell()).map(Into::into),
                        saved_turn: read
                            .and_then(|selection| selection.saved_turn())
                            .map(Into::into),
                        saved_orchestration: read
                            .and_then(|selection| selection.saved_orchestration())
                            .map(Into::into),
                        saved_receipt: OriginalSavedReceiptRole::ALL.map(|role| {
                            saved_receipt
                                .and_then(|selection| selection.pin(role))
                                .map(|pin| (role, pin.into()))
                        }),
                        confirmation,
                        archive_sha256: receipt.archive_sha256().to_owned(),
                        archive_bytes: receipt.archive_bytes(),
                        manifest_sha256: receipt.manifest_sha256().to_owned(),
                    }))
                },
                |receipt, input| {
                    verify_turn_settlement_source(inspection, active, receipt, input, checkpoint)
                        .map_err(io::Error::other)
                },
            )
            .context("bundled Registry Pack verification failed during Launch")?;
        let (
            provider_prepare,
            (model_settings, model_save, model_resolve),
            conversation,
            settlement,
        ) = verified_registry_pack_evidence;
        let turn_settlement = settlement.flatten();
        if !has_existing_grants && conversation.is_none() && turn_settlement.is_none() {
            return Ok(None);
        }
        let security_epoch = active
            .identity()
            .get_key("security_epoch")
            .and_then(|value| value.as_i64())
            .and_then(|epoch| u64::try_from(epoch).ok())
            .context("native Authority activation has an invalid security epoch")?;
        if provider_prepare.is_some() {
            log::info!("verified the selected old Provider prepare and its bundled Wasm replacement for this activation");
        }
        if model_settings.is_some() {
            log::info!("verified the selected old Model list and its bundled Wasm replacement for this activation");
        }
        if model_save.is_some() {
            log::info!("verified the selected old Model save grants and its bundled Wasm replacement for this activation");
        }
        if model_resolve.is_some() {
            log::info!("verified selected old Model Resolve grants and their bundled Wasm replacement for this activation");
        }
        if conversation_read_selection.is_some() {
            log::info!("verified selected old Conversation read grants and their bundled Wasm replacement for this activation");
        }
        if saved_receipt_selection.is_some() {
            log::info!("verified selected old Conversation saved receipt grants and their bundled Wasm replacement for this activation");
        }
        if turn_settlement.is_some() {
            log::info!("verified selected old Turn settlement grants and their bundled Wasm replacement for this activation");
        }
        Ok(Some(VerifiedRegistryLaunch {
            identity: self.identity.clone(),
            security_epoch,
            catalog_revision,
            provider_prepare,
            model_settings,
            model_save,
            model_resolve,
            conversation,
            turn_settlement,
        }))
    }
}

/// Reacquire the Provider Pack from the original signed resolver source and receipt pin.
pub(super) fn reacquire_provider_prepare_pack(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &VerifiedRegistryLaunch,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<VerifiedPack> {
    let source = proof
        .provider_prepare
        .as_ref()
        .context("admitted launch has no verified Provider preparation source")?;
    let app_root = registry_pack_root(inspection, proof)?;
    registry_pack_receipt::verify_installed_with_provider(
        &app_root,
        REGISTRY_RECEIPT_SHA256,
        || registry_checkpoint(inspection, &checkpoint),
        |receipt, input| {
            require_archive_receipt(
                receipt,
                &source.archive_sha256,
                source.archive_bytes,
                &source.manifest_sha256,
            )?;
            verify_provider_prepare_archive(inspection, receipt, input)
        },
    )
    .context("bundled Provider prepare archive verification failed")
}

/// Reacquire the Model Pack from the original signed resolver source and receipt pin.
pub(super) fn reacquire_model_settings_pack(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &VerifiedRegistryLaunch,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<VerifiedPack> {
    let source = proof
        .model_settings
        .as_ref()
        .context("admitted launch has no verified Model settings source")?;
    let app_root = registry_pack_root(inspection, proof)?;
    registry_pack_receipt::verify_installed_with_model(
        &app_root,
        REGISTRY_RECEIPT_SHA256,
        || registry_checkpoint(inspection, &checkpoint),
        |receipt, input| {
            require_archive_receipt(
                receipt,
                &source.archive_sha256,
                source.archive_bytes,
                &source.manifest_sha256,
            )?;
            verify_model_settings_archive(inspection, receipt, input)
        },
    )
    .context("bundled Model settings archive verification failed")
}

/// Reacquire the Model Save Pack using its own launch evidence and fixed declaration.
pub(super) fn reacquire_model_save_pack(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &VerifiedRegistryLaunch,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<VerifiedPack> {
    let source = proof
        .model_save
        .as_ref()
        .context("admitted launch has no verified Model Save source")?;
    let app_root = registry_pack_root(inspection, proof)?;
    registry_pack_receipt::verify_installed_with_model(
        &app_root,
        REGISTRY_RECEIPT_SHA256,
        || registry_checkpoint(inspection, &checkpoint),
        |receipt, input| {
            require_archive_receipt(
                receipt,
                &source.archive_sha256,
                source.archive_bytes,
                &source.manifest_sha256,
            )?;
            let pack = verify_registry_archive(inspection, receipt, input)?;
            verify_legacy_model_save_declaration(&pack).map_err(io::Error::other)?;
            Ok(pack)
        },
    )
    .context("bundled Model Save archive verification failed")
}

/// Reacquire the Model Pack using the Gateway role's grant and pinned archive.
pub(super) fn reacquire_model_resolve_pack(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &VerifiedRegistryLaunch,
    role: OriginalModelResolveRole,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<VerifiedPack> {
    let source = proof
        .model_resolve
        .as_ref()
        .filter(|source| source.permits(role))
        .context("admitted launch has no verified Model Resolve source for the Gateway role")?;
    let app_root = registry_pack_root(inspection, proof)?;
    registry_pack_receipt::verify_installed_with_model(
        &app_root,
        REGISTRY_RECEIPT_SHA256,
        || registry_checkpoint(inspection, &checkpoint),
        |receipt, input| {
            require_archive_receipt(
                receipt,
                &source.archive_sha256,
                source.archive_bytes,
                &source.manifest_sha256,
            )?;
            let pack = verify_registry_archive(inspection, receipt, input)?;
            verify_legacy_model_resolve_declaration(&pack).map_err(io::Error::other)?;
            Ok(pack)
        },
    )
    .context("bundled Model Resolve archive verification failed")
}

/// Reacquire the Conversation Pack using the caller's own grant and pinned archive.
pub(crate) fn reacquire_conversation_read_pack(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &VerifiedRegistryLaunch,
    caller: ConversationReadCaller,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<VerifiedPack> {
    let source = proof
        .conversation
        .as_ref()
        .filter(|source| source.permits_read(caller))
        .context("admitted launch has no verified Conversation read source for the caller")?;
    reacquire_conversation_archive(
        inspection,
        proof,
        source,
        checkpoint,
        verify_legacy_conversation_read_declaration,
    )
    .context("bundled Conversation read archive verification failed")
}

/// Reacquire the Conversation Pack using the Turn Runtime caller's receipt grant.
pub(super) fn reacquire_saved_receipt_pack(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &VerifiedRegistryLaunch,
    role: OriginalSavedReceiptRole,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<VerifiedPack> {
    let source = proof
        .conversation
        .as_ref()
        .filter(|source| source.permits_saved_receipt(role))
        .context(
            "admitted launch has no verified Conversation saved receipt source for the caller",
        )?;
    reacquire_conversation_archive(
        inspection,
        proof,
        source,
        checkpoint,
        verify_legacy_conversation_receipt_declaration,
    )
    .context("bundled Conversation saved receipt archive verification failed")
}

/// Reacquire the Conversation Pack using the exact confirmation chain for this role.
pub(super) fn reacquire_conversation_confirmation_pack(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &VerifiedRegistryLaunch,
    role: OriginalSavedReceiptRole,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<VerifiedPack> {
    let source = proof
        .conversation
        .as_ref()
        .filter(|source| source.permits_confirmation(role))
        .context(
            "admitted launch has no verified Conversation confirmation source for the caller",
        )?;
    reacquire_conversation_archive(
        inspection,
        proof,
        source,
        checkpoint,
        verify_legacy_conversation_confirmation_declaration,
    )
    .context("bundled Conversation confirmation archive verification failed")
}

/// Reacquire the Turn settlement Pack using the caller's signed grant and pinned archive.
pub(crate) fn reacquire_turn_settlement_pack(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &VerifiedRegistryLaunch,
    role: SettlementCaller,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<VerifiedPack> {
    let source = proof
        .turn_settlement
        .as_ref()
        .filter(|source| source.permits(role))
        .context("admitted launch has no verified Turn settlement source for the caller")?;
    let app_root = registry_pack_root(inspection, proof)?;
    registry_pack_receipt::verify_installed_with_settlement(
        &app_root,
        REGISTRY_RECEIPT_SHA256,
        || registry_checkpoint(inspection, &checkpoint),
        |receipt, input| {
            require_archive_receipt(
                receipt,
                &source.archive_sha256,
                source.archive_bytes,
                &source.manifest_sha256,
            )?;
            verify_turn_settlement_archive(inspection, receipt, input)
        },
    )
    .context("bundled Turn settlement archive verification failed")?
    .context("bundled Registry receipt has no Turn settlement archive")
}

fn observe_conversation_confirmation_declaration(pack: &VerifiedPack) -> Result<bool> {
    const CONFIRMATION_SHAPE: &str = "local.saved-conversation.legacy-confirmation";
    // Older signed Conversation archives provide only the read and receipt ports.
    let declared = pack
        .manifest
        .ports
        .provides
        .iter()
        .any(|port| port.shape.as_str() == CONFIRMATION_SHAPE)
        || pack
            .shapes
            .iter()
            .any(|shape| shape.reference.name.as_str() == CONFIRMATION_SHAPE);
    if declared {
        verify_legacy_conversation_confirmation_declaration(pack)?;
    }
    Ok(declared)
}

fn reacquire_conversation_archive(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &VerifiedRegistryLaunch,
    source: &VerifiedConversationSource,
    checkpoint: impl Fn() -> Result<()>,
    verify_declaration: fn(&VerifiedPack) -> Result<()>,
) -> Result<VerifiedPack> {
    let app_root = registry_pack_root(inspection, proof)?;
    registry_pack_receipt::verify_installed_with_conversation(
        &app_root,
        REGISTRY_RECEIPT_SHA256,
        || registry_checkpoint(inspection, &checkpoint),
        |receipt, input| {
            require_archive_receipt(
                receipt,
                &source.archive_sha256,
                source.archive_bytes,
                &source.manifest_sha256,
            )?;
            let pack = verify_registry_archive(inspection, receipt, input)?;
            verify_declaration(&pack).map_err(io::Error::other)?;
            Ok(pack)
        },
    )
    .map_err(Into::into)
}

fn registry_pack_root(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    proof: &VerifiedRegistryLaunch,
) -> Result<std::path::PathBuf> {
    inspection.with_source(|sources| {
        let authority = sources.authority();
        ensure!(
            proof.identity.matches(&authority.execution_identity()?)
                && proof.catalog_revision == authority.catalog_revision,
            "signed Registry source differs from the admitted launch"
        );
        Ok(sources.root(NativeSourceRoot::Application).to_path_buf())
    })?
}

fn registry_checkpoint(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    checkpoint: &impl Fn() -> Result<()>,
) -> io::Result<()> {
    checkpoint().map_err(io::Error::other)?;
    inspection.allocation_budget().map_err(io::Error::other)?;
    Ok(())
}

fn require_archive_receipt(
    receipt: &registry_pack_receipt::Pack,
    archive_sha256: &str,
    archive_bytes: u64,
    manifest_sha256: &str,
) -> io::Result<()> {
    if receipt.archive_sha256() != archive_sha256
        || receipt.archive_bytes() != archive_bytes
        || receipt.manifest_sha256() != manifest_sha256
    {
        return Err(io::Error::other(
            "installed Registry receipt differs from the admitted launch",
        ));
    }
    Ok(())
}

fn verify_conversation_confirmation_source(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    active: &OriginalActiveProfile,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<VerifiedConversationConfirmationRoles> {
    let budget = inspection.allocation_budget()?;
    let [saved_turn, saved_reconcile] = OriginalSavedReceiptRole::ALL;
    let choices = [
        OriginalConversationConfirmationChoice::select(active, budget, saved_turn)?,
        OriginalConversationConfirmationChoice::select(active, budget, saved_reconcile)?,
    ];
    if choices.iter().all(Option::is_none) {
        return Ok([None, None]);
    }
    let locked = inspection.with_source(|sources| {
        sources.conversation_confirmation_bundle(choices.each_ref().map(Option::as_ref), budget)
    })??;
    locked.verify(checkpoint)?;
    Ok(std::array::from_fn(|index| {
        let role = OriginalSavedReceiptRole::ALL[index];
        choices[index].as_ref().map(|choice| {
            (
                role,
                [
                    choice.turn_caller().into(),
                    choice.completion().into(),
                    choice.turn_get().into(),
                ],
            )
        })
    }))
}

fn verify_turn_settlement_source(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    active: &OriginalActiveProfile,
    receipt: &registry_pack_receipt::Pack,
    input: &mut dyn io::Read,
    checkpoint: impl Fn() -> Result<()>,
) -> Result<Option<VerifiedTurnSettlementSource>> {
    let budget = inspection.allocation_budget()?;
    let [saved_turn, saved_reconcile] = SettlementCaller::ALL;
    let selections = [
        select_original_saved_turn_settlement(active, budget, saved_turn)?,
        select_original_saved_turn_settlement(active, budget, saved_reconcile)?,
    ];
    if selections.iter().all(Option::is_none) {
        return Ok(None);
    }
    let locked = inspection.with_source(|sources| {
        sources.turn_settlement_bundle(selections.each_ref().map(Option::as_ref), budget)
    })??;
    locked.verify(checkpoint)?;
    verify_turn_settlement_archive(inspection, receipt, input)?;
    Ok(Some(VerifiedTurnSettlementSource {
        roles: selections.map(|choice| choice.map(|choice| (choice.role(), choice.pin().into()))),
        archive_sha256: receipt.archive_sha256().to_owned(),
        archive_bytes: receipt.archive_bytes(),
        manifest_sha256: receipt.manifest_sha256().to_owned(),
    }))
}

fn verify_turn_settlement_archive(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    receipt: &registry_pack_receipt::Pack,
    input: &mut dyn io::Read,
) -> io::Result<VerifiedPack> {
    let pack = verify_registry_archive(inspection, receipt, input)?;
    require_turn_settlement_declaration(&pack).map_err(io::Error::other)?;
    Ok(pack)
}

// The same files are used when staging the Pack. They determine the exact
// parameter and result ABI, Core imports, and settlement port cardinality.
const TURN_SETTLEMENT_WIT_FILES: [(&str, &[u8]); 3] = [
    (
        "wit/pack.wit",
        include_bytes!("../../../packs/local.saved-turn-runtime-rust/wit/pack.wit"),
    ),
    (
        "wit/deps/core.wit",
        include_bytes!("../../../crates/tobkiri-wit/wit/core-v1-2/core.wit"),
    ),
    (
        "wit/legacy-settlement.shape.json",
        include_bytes!(
            "../../../packs/local.saved-turn-runtime-rust/wit/legacy-settlement.shape.json"
        ),
    ),
];

fn require_turn_settlement_declaration(pack: &VerifiedPack) -> Result<()> {
    const SHAPE: &str = "local.saved-turn-runtime.legacy-settlement@1.0";
    let manifest = &pack.manifest;
    let permissions = PermissionSet {
        wasm: manifest.permissions.wasm.clone(),
        loops: manifest.permissions.loops.clone(),
        ..PermissionSet::default()
    };
    ensure!(
        manifest.id.as_str() == registry_pack_receipt::SETTLEMENT_PACK_ID
            && manifest.ports.provides.len() == 1
            && manifest.ports.consumes.is_empty()
            && manifest.ports.untyped.is_empty()
            && manifest.inputs.is_empty()
            && manifest.flows.is_empty()
            && manifest.overlays.is_empty()
            && manifest.stores.is_empty()
            && manifest.ui.is_none()
            && manifest.permissions == permissions,
        "bundled Turn settlement Pack has unexpected capabilities"
    );
    ensure!(
        matches!(&manifest.runtime, RuntimeSpec::Wasm { component, core_modules, config, .. }
            if component == "component.wasm" && core_modules.is_empty() && config.is_none())
            && manifest.shapes.len() == 1
            && manifest.shapes[0].wit == "wit/pack.wit"
            && manifest.files.len() == TURN_SETTLEMENT_WIT_FILES.len() + 1
            && TURN_SETTLEMENT_WIT_FILES.into_iter().all(|(path, wit)| {
                pack.files
                    .get(path)
                    .is_some_and(|file| *file.digest() == Digest::hash(wit))
            }),
        "bundled Turn settlement Pack has an unexpected WIT or runtime declaration"
    );
    require_registry_ports(pack, "prepare", &[SHAPE])?;
    Ok(())
}

fn verify_provider_prepare_archive(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    receipt: &registry_pack_receipt::Pack,
    input: &mut dyn io::Read,
) -> io::Result<VerifiedPack> {
    let pack = verify_registry_archive(inspection, receipt, input)?;
    require_registry_ports(
        &pack,
        "prepare-local",
        &[
            "local.provider-registry.legacy-configuration@1.0",
            "local.provider-registry.legacy-configuration-live@1.0",
        ],
    )?;
    Ok(pack)
}

fn verify_model_settings_archive(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    receipt: &registry_pack_receipt::Pack,
    input: &mut dyn io::Read,
) -> io::Result<VerifiedPack> {
    let pack = verify_registry_archive(inspection, receipt, input)?;
    require_model_list_port(&pack)?;
    Ok(pack)
}

fn require_model_list_port(pack: &VerifiedPack) -> io::Result<()> {
    require_registry_ports(
        pack,
        "list-models",
        &["local.model-registry.legacy-settings@1.0"],
    )
}

fn verify_registry_archive(
    inspection: &NativeArtifactInspection<NativeApplicationSources>,
    receipt: &registry_pack_receipt::Pack,
    input: &mut dyn io::Read,
) -> io::Result<VerifiedPack> {
    (|| {
        let pack = inspection.verify_pack_archive(input, receipt.archive_bytes())?;
        ensure!(
            pack.manifest.id.as_str() == receipt.id()
                && pack.manifest.version == receipt.version()
                && pack.hash.as_str() == receipt.manifest_sha256()
                && matches!(&pack.manifest.runtime, RuntimeSpec::Wasm { .. }),
            "Registry Pack manifest differs from the bundled receipt"
        );
        Ok::<_, anyhow::Error>(pack)
    })()
    .map_err(io::Error::other)
}

fn require_registry_ports(pack: &VerifiedPack, operation: &str, shapes: &[&str]) -> io::Result<()> {
    (|| {
        let operation = OpName::parse(operation)?;
        for name in shapes {
            let shape = ShapeRef::parse(name)?;
            ensure!(
                pack.manifest.ports.provides.iter().any(|port| {
                    port.shape == shape.name && port.version == shape.version && !port.optional
                }) && pack.shapes.iter().any(|definition| {
                    definition.reference == shape
                        && definition.kind == ShapeKind::Port
                        && definition.functions.len() == 1
                        && definition.functions.contains_key(&operation)
                        && definition.streams.is_empty()
                        && definition.callbacks.is_empty()
                }),
                "bundled Registry Pack has no exact legacy {operation} port {name}"
            );
        }
        Ok(())
    })()
    .map_err(io::Error::other)
}

fn validate_observed_activation(
    active: &OriginalActiveProfile,
    identity: &ExecutionProfileIdentity,
    catalog_revision: &str,
) -> Result<()> {
    let (_, _, plan) = active.records();
    ensure!(
        [
            ("profile_id", identity.profile_id.as_str()),
            ("profile_revision", identity.profile_revision.as_str()),
            ("activation_id", identity.activation_id.as_str()),
            ("plan_digest", identity.plan_digest.as_str()),
        ]
        .into_iter()
        .all(|(field, expected)| {
            active
                .identity()
                .get_key(field)
                .is_some_and(|value| value.as_str() == Some(expected))
        }) && plan
            .get_key("catalog_revision")
            .is_some_and(|value| value.as_str() == Some(catalog_revision)),
        "native Authority activation differs from the signed Shell selection"
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::native_harness_bootstrap::NativeRegistryRequest;

    #[test]
    fn model_resolve_evidence_requires_the_exact_gateway_role() {
        use OriginalModelResolveRole::{Generate, QuoteGenerate, QuoteStream, Stream};
        let identity = ExecutionProfileIdentity {
            profile_id: "defaults".into(),
            profile_revision: "revision:fixture".into(),
            activation_id: "activation:fixture".into(),
            plan_digest: "sha256:fixture".into(),
        };
        let catalog_revision = "catalog:fixture";
        let roles = [QuoteGenerate, QuoteStream, Generate, Stream];
        for proof in [
            VerifiedRegistryLaunch::test_proof(identity.clone(), catalog_revision),
            VerifiedRegistryLaunch::test_model_proof(identity.clone(), catalog_revision),
            VerifiedRegistryLaunch::test_model_save_proof(identity.clone(), catalog_revision),
        ] {
            for role in roles {
                assert!(!proof.permits_model_resolve(role));
            }
        }
        for granted in roles {
            let proof = VerifiedRegistryLaunch::test_model_resolve_proof(
                identity.clone(),
                catalog_revision,
                granted,
            );
            for role in roles {
                assert_eq!(proof.permits_model_resolve(role), role == granted);
            }
            assert!(!NativeRegistryRequest::ModelList.has_launch_proof(&proof));
            assert!(!NativeRegistryRequest::ModelSave {
                request: Vec::new(),
                provider: Vec::new(),
            }
            .has_launch_proof(&proof));
        }
    }

    #[test]
    fn model_save_evidence_keeps_the_other_registry_operations_separate() {
        let identity = ExecutionProfileIdentity {
            profile_id: "defaults".into(),
            profile_revision: "revision:fixture".into(),
            activation_id: "activation:fixture".into(),
            plan_digest: "sha256:fixture".into(),
        };
        let save = NativeRegistryRequest::ModelSave {
            request: b"{}".to_vec(),
            provider: b"{}".to_vec(),
        };
        assert!(!save.has_launch_proof(&VerifiedRegistryLaunch::test_proof(
            identity.clone(),
            "catalog:fixture"
        )));
        assert!(
            !save.has_launch_proof(&VerifiedRegistryLaunch::test_model_proof(
                identity.clone(),
                "catalog:fixture"
            ))
        );
        let proof = VerifiedRegistryLaunch::test_model_save_proof(identity, "catalog:fixture");
        assert!(save.has_launch_proof(&proof));
        assert!(!NativeRegistryRequest::ModelList.has_launch_proof(&proof));
        assert!(!NativeRegistryRequest::Provider {
            request: Vec::new(),
            apply: None,
        }
        .has_launch_proof(&proof));
    }

    #[test]
    fn turn_settlement_declaration_pins_wit_and_rejects_extra_permissions() {
        use std::{fs, path::PathBuf};
        use tobkiri_core::pack::{generate_manifest, verify_directory};

        struct Root(PathBuf);
        impl Drop for Root {
            fn drop(&mut self) {
                let _ = fs::remove_dir_all(&self.0);
            }
        }
        let root = Root(std::env::temp_dir().join(format!(
            "tobkiri-settlement-declaration-{}-{}",
            std::process::id(),
            rand::random::<u64>()
        )));
        fs::create_dir_all(root.0.join("wit/deps")).unwrap();
        let path = root.0.canonicalize().unwrap();
        for (relative, wit) in TURN_SETTLEMENT_WIT_FILES {
            fs::write(path.join(relative), wit).unwrap();
        }
        fs::write(path.join("component.wasm"), b"fixture").unwrap();
        let draft = include_bytes!("../../../packs/local.saved-turn-runtime-rust/pack.draft.json");
        let verify = |draft: &[u8]| {
            let manifest = generate_manifest(&path, draft).unwrap();
            fs::write(path.join("pack.json"), manifest).unwrap();
            verify_directory(&path, &Default::default()).unwrap()
        };
        require_turn_settlement_declaration(&verify(draft)).unwrap();

        let mut extra: serde_json::Value = serde_json::from_slice(draft).unwrap();
        extra["permissions"]["storage"] = serde_json::json!({"private": true});
        assert!(
            require_turn_settlement_declaration(&verify(&serde_json::to_vec(&extra).unwrap()))
                .unwrap_err()
                .to_string()
                .contains("unexpected capabilities")
        );
        let old_wit = std::str::from_utf8(TURN_SETTLEMENT_WIT_FILES[0].1).unwrap();
        fs::write(
            path.join("wit/pack.wit"),
            old_wit.replace("option<u64>", "option<u32>"),
        )
        .unwrap();
        assert!(require_turn_settlement_declaration(&verify(draft))
            .unwrap_err()
            .to_string()
            .contains("unexpected WIT"));
    }

    #[test]
    fn conversation_confirmation_declaration_preserves_legacy_read_and_receipt_ports() {
        use std::{fs, path::PathBuf};
        use tobkiri_core::pack::{generate_manifest, verify_directory};

        struct Root(PathBuf);
        impl Drop for Root {
            fn drop(&mut self) {
                let _ = fs::remove_dir_all(&self.0);
            }
        }
        let root = Root(std::env::temp_dir().join(format!(
            "tobkiri-confirmation-declaration-{}-{}",
            std::process::id(),
            rand::random::<u64>()
        )));
        fs::create_dir_all(root.0.join("wit/deps")).unwrap();
        let path = root.0.canonicalize().unwrap();
        macro_rules! copy_wit {
            ($($filename:literal),+) => {$(
                fs::write(
                    path.join(concat!("wit/", $filename)),
                    include_bytes!(concat!("../../../packs/local.saved-conversation-rust/wit/", $filename)),
                )
                .unwrap();
            )+};
        }
        copy_wit!(
            "pack.wit",
            "data.shape.json",
            "legacy-confirmation.shape.json",
            "legacy-read.shape.json",
            "legacy-receipt.shape.json",
            "model.shape.json"
        );
        fs::write(
            path.join("wit/deps/core.wit"),
            include_bytes!("../../../crates/tobkiri-wit/wit/core-v1-2/core.wit"),
        )
        .unwrap();
        fs::write(path.join("component.wasm"), b"fixture").unwrap();
        let draft: serde_json::Value = serde_json::from_slice(include_bytes!(
            "../../../packs/local.saved-conversation-rust/pack.draft.json"
        ))
        .unwrap();
        let verify = |draft: &serde_json::Value| {
            let manifest = generate_manifest(&path, &serde_json::to_vec(draft).unwrap()).unwrap();
            fs::write(path.join("pack.json"), manifest).unwrap();
            verify_directory(&path, &Default::default()).unwrap()
        };
        let current = verify(&draft);
        assert!(observe_conversation_confirmation_declaration(&current).unwrap());

        let confirmation = "local.saved-conversation.legacy-confirmation";
        let mut old = draft.clone();
        old["shapes"]
            .as_array_mut()
            .unwrap()
            .retain(|row| row["name"] != confirmation);
        old["ports"]["provides"]
            .as_array_mut()
            .unwrap()
            .retain(|row| row["shape"] != confirmation);
        let old = verify(&old);
        assert!(!observe_conversation_confirmation_declaration(&old).unwrap());
        verify_legacy_conversation_read_declaration(&old).unwrap();
        verify_legacy_conversation_receipt_declaration(&old).unwrap();

        let mut optional = draft;
        optional["ports"]["provides"]
            .as_array_mut()
            .unwrap()
            .iter_mut()
            .find(|port| port["shape"] == confirmation)
            .unwrap()["optional"] = serde_json::json!(true);
        assert!(observe_conversation_confirmation_declaration(&verify(&optional)).is_err());
    }

    #[test]
    fn conversation_confirmation_evidence_keeps_roles_read_and_receipt_independent() {
        let identity = ExecutionProfileIdentity {
            profile_id: "defaults".into(),
            profile_revision: "revision:fixture".into(),
            activation_id: "activation:fixture".into(),
            plan_digest: "sha256:fixture".into(),
        };
        let revision = "catalog:fixture";
        for granted in OriginalSavedReceiptRole::ALL {
            let receipt = VerifiedRegistryLaunch::test_saved_receipt_proof(
                identity.clone(),
                revision,
                granted,
            );
            let proof = VerifiedRegistryLaunch::test_conversation_confirmation_proof(
                identity.clone(),
                revision,
                granted,
            );
            for role in OriginalSavedReceiptRole::ALL {
                assert!(!receipt.permits_conversation_confirmation(role));
                assert_eq!(
                    proof.permits_conversation_confirmation(role),
                    role == granted
                );
                assert!(!proof.permits_saved_receipt(role));
            }
            for caller in [
                ConversationReadCaller::Shell,
                ConversationReadCaller::SavedTurn,
                ConversationReadCaller::SavedOrchestration,
            ] {
                assert!(!proof.permits_conversation_read(caller));
            }
            for role in SettlementCaller::ALL {
                assert!(!proof.permits_turn_settlement(role));
            }
        }
    }

    #[test]
    fn turn_settlement_evidence_requires_the_original_grant_for_each_role() {
        let identity = ExecutionProfileIdentity {
            profile_id: "defaults".into(),
            profile_revision: "revision:fixture".into(),
            activation_id: "activation:fixture".into(),
            plan_digest: "sha256:fixture".into(),
        };
        let revision = "catalog:fixture";
        for granted in OriginalSavedReceiptRole::ALL {
            let receipt = VerifiedRegistryLaunch::test_saved_receipt_proof(
                identity.clone(),
                revision,
                granted,
            );
            for role in SettlementCaller::ALL {
                assert!(!receipt.permits_turn_settlement(role));
            }
        }
        for granted in SettlementCaller::ALL {
            let proof = VerifiedRegistryLaunch::test_turn_settlement_proof(
                identity.clone(),
                revision,
                granted,
            );
            for role in SettlementCaller::ALL {
                assert_eq!(proof.permits_turn_settlement(role), role == granted);
            }
            for role in OriginalSavedReceiptRole::ALL {
                assert!(!proof.permits_saved_receipt(role));
            }
            assert!(!proof.permits_conversation_read(ConversationReadCaller::SavedTurn));
        }
    }

    #[test]
    fn conversation_read_evidence_is_specific_to_the_selected_caller() {
        let identity = ExecutionProfileIdentity {
            profile_id: "defaults".into(),
            profile_revision: "revision:fixture".into(),
            activation_id: "activation:fixture".into(),
            plan_digest: "sha256:fixture".into(),
        };
        let callers = [
            ConversationReadCaller::Shell,
            ConversationReadCaller::SavedTurn,
            ConversationReadCaller::SavedOrchestration,
        ];
        let request = |caller| NativeRegistryRequest::ConversationGet {
            caller,
            conversation_id: "fixture".into(),
        };
        let provider = VerifiedRegistryLaunch::test_proof(identity.clone(), "catalog:fixture");
        let model = VerifiedRegistryLaunch::test_model_proof(identity.clone(), "catalog:fixture");
        let save =
            VerifiedRegistryLaunch::test_model_save_proof(identity.clone(), "catalog:fixture");
        for caller in callers {
            for proof in [&provider, &model, &save] {
                assert!(!request(caller).has_launch_proof(proof));
            }
        }
        let mut proof = save;
        proof.model_save = None;
        for selected_caller in callers {
            let mut source = VerifiedConversationSource {
                shell: None,
                saved_turn: None,
                saved_orchestration: None,
                saved_receipt: [None, None],
                confirmation: [None, None],
                archive_sha256: "sha256:archive".into(),
                archive_bytes: 512,
                manifest_sha256: "sha256:manifest".into(),
            };
            let role = match selected_caller {
                ConversationReadCaller::Shell => &mut source.shell,
                ConversationReadCaller::SavedTurn => &mut source.saved_turn,
                ConversationReadCaller::SavedOrchestration => &mut source.saved_orchestration,
            };
            *role = Some(VerifiedLegacyRoutePin::fixture());
            proof.conversation = Some(source);
            for caller in callers {
                assert_eq!(
                    request(caller).has_launch_proof(&proof),
                    caller == selected_caller
                );
            }
            for role in OriginalSavedReceiptRole::ALL {
                assert!(!proof.permits_saved_receipt(role));
            }
            assert!(!NativeRegistryRequest::ModelList.has_launch_proof(&proof));
        }
        for granted in OriginalSavedReceiptRole::ALL {
            let proof = VerifiedRegistryLaunch::test_saved_receipt_proof(
                identity.clone(),
                "catalog:fixture",
                granted,
            );
            for role in OriginalSavedReceiptRole::ALL {
                assert_eq!(proof.permits_saved_receipt(role), role == granted);
            }
            for caller in callers {
                assert!(!proof.permits_conversation_read(caller));
            }
        }
    }
}
