//! Original Launch metadata, collected with the RuntimeOwner used during Setup.

use std::{
    panic::{catch_unwind, AssertUnwindSafe},
    sync::mpsc,
    time::Instant,
};

use anyhow::{anyhow, Context, Result};
use tauri::{AppHandle, Manager};

use super::{JobTicket, NativeRuntimeState, WorkerExit};
use crate::{
    defaultspack_authority::SignedApplicationResolver,
    dock_registration::{assemble_defaultspack_desktop_metadata, DefaultspackDesktopMetadata},
    native_declared_artifacts::collect_for_native_launch,
    preparation_budget, AppConfig,
};

pub(crate) fn read_launch_metadata(
    app: &AppHandle,
    deadline: Instant,
) -> Result<DefaultspackDesktopMetadata> {
    preparation_budget::check(deadline)?;
    let runtime = app
        .try_state::<NativeRuntimeState>()
        .context("native Launch runtime owner is unavailable")?
        .inner()
        .clone();
    let jobs = runtime.0.jobs.clone();
    let app = app.clone();
    let (send, receive) = mpsc::sync_channel(1);
    jobs.spawn(move |job| {
        let (result, cleanup_verified) = catch_unwind(AssertUnwindSafe(|| {
            tauri::async_runtime::block_on(collect_on_worker(&app, &runtime, &job, deadline))
        }))
        .unwrap_or_else(|_| (Err(anyhow!("native Launch metadata worker failed")), false));
        let _ = send.send(result);
        WorkerExit { cleanup_verified }
    })?;
    receive
        .recv_timeout(preparation_budget::remaining(deadline)?)
        .context("native Launch metadata is unavailable")?
}

async fn collect_on_worker(
    app: &AppHandle,
    runtime: &NativeRuntimeState,
    job: &JobTicket,
    deadline: Instant,
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
    let collection = collect_for_native_launch(sources, invocation, &cancellation);
    tokio::pin!(collection);
    let (result, cleanup_verified) = tokio::select! {
        result = &mut collection => result,
        () = tokio::time::sleep_until(deadline.into()) => {
            cancellation.cancel();
            // Keep the original worker and descriptors until physical cleanup.
            collection.await
        }
    };
    let metadata = result.and_then(|(sources, contributions)| {
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
