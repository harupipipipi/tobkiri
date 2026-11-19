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
    NativeArtifactInspection, NativeSetupInvocation,
};
use tobkiri_gate::{ControlKind, CoreCancellation};
use tobkiri_harness_native::{
    OriginalActivationInspection, OriginalActiveProfile, OriginalModelSaveSelection,
    OriginalModelSettingsSelection, OriginalProviderPrepareSelection, OriginalRoutePin,
};
use tobkiri_wasm::verify_legacy_model_save_declaration;

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
        let (app_root, activation) = inspection.with_source(|sources| {
            let catalog_revision = &sources.authority().catalog_revision;
            let activation = active
                .map(|active| {
                    validate_observed_activation(active, self.identity, catalog_revision)?;
                    if provider_selection.is_none()
                        && model_selection.is_none()
                        && model_save_selection.is_none()
                    {
                        return Ok(None);
                    }
                    let security_epoch = active
                        .identity()
                        .get_key("security_epoch")
                        .and_then(|value| value.as_i64())
                        .and_then(|epoch| u64::try_from(epoch).ok())
                        .context("native Authority activation has an invalid security epoch")?;
                    Ok::<_, anyhow::Error>(Some((
                        sources.registry_bundle(
                            provider_selection.as_ref(),
                            model_selection.as_ref(),
                            model_save_selection.as_ref(),
                            budget,
                        )?,
                        security_epoch,
                        catalog_revision.clone(),
                    )))
                })
                .transpose()?
                .flatten();
            Ok::<_, anyhow::Error>((
                sources.root(NativeSourceRoot::Application).to_path_buf(),
                activation,
            ))
        })??;
        let Some((locked, security_epoch, catalog_revision)) = activation else {
            registry_pack_receipt::verify_installed(&app_root, REGISTRY_RECEIPT_SHA256, || {
                checkpoint().map_err(io::Error::other)
            })
            .context("bundled Registry Pack verification failed during Launch")?;
            return Ok(None);
        };
        locked.verify(checkpoint)?;
        let (provider_prepare, (model_settings, model_save)) =
            registry_pack_receipt::verify_installed_with_provider_and_model(
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
                    if model_list.is_none() && model_save.is_none() {
                        return Ok((None, None));
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
                    Ok((list, save))
                },
            )
            .context("bundled Registry Pack verification failed during Launch")?;
        if provider_prepare.is_some() {
            log::info!("verified the selected old Provider prepare and its bundled Wasm replacement for this activation");
        }
        if model_settings.is_some() {
            log::info!("verified the selected old Model list and its bundled Wasm replacement for this activation");
        }
        if model_save.is_some() {
            log::info!("verified the selected old Model save grants and its bundled Wasm replacement for this activation");
        }
        Ok(Some(VerifiedRegistryLaunch {
            identity: self.identity.clone(),
            security_epoch,
            catalog_revision,
            provider_prepare,
            model_settings,
            model_save,
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
    fn model_save_evidence_keeps_the_other_registry_operations_separate() {
        let identity = ExecutionProfileIdentity {
            profile_id: "defaults".into(),
            profile_revision: "revision:fixture".into(),
            activation_id: "activation:fixture".into(),
            plan_digest: "sha256:fixture".into(),
        };
        let mut proof = VerifiedRegistryLaunch::test_model_proof(identity, "catalog:fixture");
        let list = proof.model_settings.take().unwrap();
        proof.model_save = Some(VerifiedModelSaveSource {
            model_manage: VerifiedLegacyRoutePin::fixture(),
            provider_read: VerifiedLegacyRoutePin::fixture(),
            archive_sha256: list.archive_sha256,
            archive_bytes: list.archive_bytes,
            manifest_sha256: list.manifest_sha256,
        });
        assert!(!NativeRegistryRequest::ModelList.has_launch_proof(&proof));
        assert!(!NativeRegistryRequest::Provider {
            request: Vec::new(),
            apply: None,
        }
        .has_launch_proof(&proof));
    }
}
