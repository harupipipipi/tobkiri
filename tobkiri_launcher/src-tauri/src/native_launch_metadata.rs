//! Original Launch metadata, collected with the RuntimeOwner used during Setup.

use std::{
    io,
    panic::{catch_unwind, AssertUnwindSafe},
    sync::{atomic::Ordering, mpsc},
    time::{Duration, Instant},
};

use anyhow::{anyhow, bail, ensure, Context, Result};
use tauri::{AppHandle, Manager};
use tobkiri_core::{NativeArtifactInspection, NativeSetupInvocation};
use tobkiri_gate::{ControlKind, CoreCancellation};
use tobkiri_harness_native::{OriginalActivationInspection, OriginalActiveProfile};

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
const REGISTRY_VERIFICATION_BYTES: u64 = 256 * 1024;

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
    let metadata = result.and_then(|(sources, contributions)| {
        preparation_budget::check(deadline)?;
        require_current_launch_identity(&config, &identity)?;
        if observe_activation {
            let invocation = job
                .invocation()
                .context("native Launch job lost its runtime invocation")?;
            let _funding = invocation
                .control()
                .reserve(ControlKind::Frame, REGISTRY_VERIFICATION_BYTES)?;
            registry_pack_receipt::verify_installed(
                sources.root(NativeSourceRoot::Application),
                REGISTRY_RECEIPT_SHA256,
                || {
                    preparation_budget::check(deadline).map_err(io::Error::other)?;
                    job.ensure_accepting(&runtime.0.jobs)
                        .map_err(io::Error::other)
                },
            )
            .context("bundled Registry Pack verification failed during Launch")?;
            require_current_launch_identity(&config, &identity)?;
            log::info!("verified the bundled Registry Packs for the active Shell launch");
        }
        preparation_budget::check(deadline)?;
        job.ensure_accepting(&runtime.0.jobs)?;
        assemble_defaultspack_desktop_metadata(
            &config,
            sources.into_authority(),
            identity,
            contributions,
        )
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
    ) -> (Result<()>, bool) {
        let Self {
            app,
            runtime,
            job,
            invocation,
            cancellation,
            identity,
            deadline,
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
            Ok(Ok(active)) => active,
            Ok(Err(error)) => {
                // The original Host remains responsible for pending recovery.
                // Only a successful observation can supply native activation data.
                log::debug!(
                    "current v5 Authority observation is unavailable at {:?}",
                    error.stage
                );
                return (Ok(()), true);
            }
            Err(error) => return (Err(anyhow!(error)), false),
        };
        let result = (|| {
            preparation_budget::check(deadline)?;
            job.ensure_accepting(&runtime.0.jobs)?;
            inspection.with_source(|sources| {
                validate_observed_activation(
                    &active,
                    identity,
                    &sources.authority().catalog_revision,
                )
            })??;
            log::info!("verified current v5 Profile Authority with the native Shell Launch Store");
            Ok(())
        })();
        (result, true)
    }
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
