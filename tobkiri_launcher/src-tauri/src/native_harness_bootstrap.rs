//! Original-composition input adapter; no replacement window or startup probe.
//!
//! This opt-in constructor is not yet routed from the original production
//! operation. Keep that route unchanged until actual component/flow parity.
use crate::native_harness_host_inputs::{with_original_host_inputs, NativeHostSourceRole};
use crate::native_runtime_jobs::{
    Finalization, JobTicket, Jobs, LifecycleError, SourceCustody, WorkerExit,
};
use crate::native_worker_failure::{Condition, DetachedFailure, Detachment, Outcome, Stage};
use anyhow::{bail, Result};
use std::{
    fs::File,
    future::{poll_fn, Future},
    panic::{catch_unwind, AssertUnwindSafe},
    sync::{atomic::AtomicBool, Arc, Mutex},
    task::Poll,
    time::Instant,
};
use tauri::{AppHandle, Manager};
use tobkiri_core::{Core, Digest, HostIo, NativeSetupInvocation, RuntimeOwner};
use tobkiri_harness_native::{
    BootstrapInputOwner, BootstrapModuleIndex, BootstrapRoots, ModuleMetadataPolicy,
    NativeBootstrap, NativeComponentPreparation, OriginalAuthorityError, OriginalAuthorityMaterial,
    OriginalAuthorityOwner, ORIGINAL_PROFILE_SOURCE_METADATA,
};
use tobkiri_wasm::NativeDataIoError;

#[path = "native_harness_dispatch.rs"]
mod dispatch;
pub(crate) use crate::native_worker_failure::UnboundOperation;
pub(crate) use dispatch::{OriginalGetter, OriginalOperation};
#[path = "native_authority_job.rs"]
mod authority;
pub(crate) use authority::{
    read_original_authority_snapshot, OriginalAuthorityData, OriginalAuthorityJob,
    OriginalAuthorityJobFailure,
};

enum OwnedRuntime {
    Unopened,
    Native(RuntimeOwner),
    Core(Arc<Core>),
    Closed,
}
impl Default for OwnedRuntime {
    fn default() -> Self {
        Self::Unopened
    }
}

/// Cold registration only. Root IO first occurs at the native operation's
/// reachpoint, using the original selected user_data_dir and one fixed child.
#[derive(Clone)]
pub(crate) struct NativeRuntimeState(Arc<RuntimeState>);
struct RuntimeState {
    owner: Mutex<OwnedRuntime>,
    jobs: Jobs,
    finalization: Finalization,
    retained_authority: Mutex<Vec<authority::Retained>>,
}

impl NativeRuntimeState {
    pub(crate) fn new(shutdown: Arc<AtomicBool>) -> Self {
        Self(Arc::new(RuntimeState {
            owner: Mutex::new(OwnedRuntime::Unopened),
            jobs: Jobs::new(shutdown),
            finalization: Finalization::default(),
            retained_authority: Mutex::new(Vec::new()),
        }))
    }

    fn begin(&self, app: &AppHandle, job: &JobTicket) -> Result<NativeSetupInvocation> {
        job.ensure_accepting(&self.0.jobs)?;
        let mut state =
            self.0.owner.lock().map_err(|_| {
                anyhow::Error::new(LifecycleError("native runtime owner is poisoned"))
            })?;
        // Existing owners do not bypass the original flag or admission seal.
        job.ensure_accepting(&self.0.jobs)?;
        if matches!(*state, OwnedRuntime::Unopened) {
            let owner = with_original_host_inputs(app, |inputs| {
                RuntimeOwner::open(inputs.internal_runtime_storage_parent().join("core"))
            })
            .map_err(|_| {
                anyhow::Error::new(LifecycleError(
                    "original native configuration is unavailable",
                ))
            })??;
            *state = OwnedRuntime::Native(owner);
        }
        match &*state {
            OwnedRuntime::Native(owner) => Ok(owner.begin_native_setup()?),
            OwnedRuntime::Core(core) => Ok(core.begin_native_setup()?),
            _ => bail!(LifecycleError("native runtime owner is unavailable")),
        }
    }

    /// Future effectful Core composition must consume THIS owner; reopening the
    /// default/another root would replace accounting and is not an alternative.
    pub(crate) fn adopt_core(&self, job: &JobTicket, host: Arc<dyn HostIo>) -> Result<Arc<Core>> {
        job.ensure_accepting(&self.0.jobs)?;
        let mut state =
            self.0.owner.lock().map_err(|_| {
                anyhow::Error::new(LifecycleError("native runtime owner is poisoned"))
            })?;
        job.ensure_accepting(&self.0.jobs)?;
        if let OwnedRuntime::Core(core) = &*state {
            return Ok(core.clone());
        }
        let OwnedRuntime::Native(owner) = std::mem::replace(&mut *state, OwnedRuntime::Closed)
        else {
            bail!(LifecycleError(
                "native runtime ownership cannot be transferred"
            ));
        };
        let core = Arc::new(Core::from_runtime_owner(owner, host)?);
        *state = OwnedRuntime::Core(core.clone());
        Ok(core)
    }

    /// All real preparation/IO Joins precede dropping this same owner. There
    /// are no product Sessions here yet; none are fabricated to obtain close.
    pub(crate) fn stop_until(&self, deadline: Instant) -> Result<()> {
        self.0.jobs.drain_until(deadline)?;
        self.0.finalization.run_until(deadline, || {
            let owner = {
                let mut owner = self.0.owner.lock().map_err(|_| {
                    anyhow::Error::new(LifecycleError("native runtime owner is poisoned"))
                })?;
                std::mem::replace(&mut *owner, OwnedRuntime::Closed)
            };
            drop(owner);
            self.0.jobs.mark_closed()
        })
    }
}

/// Actual AppHandle owns the original managed config and shutdown state through
/// worker completion. No serialized roots or cloned configuration replace it.
pub(crate) struct OriginalBootstrapInputs {
    owner: tobkiri_harness_native::OriginalSourceHandle<OriginalBootstrapOwner>,
}
struct OriginalBootstrapOwner {
    app: AppHandle,
    // Field order releases the actual AppHandle before publishing its receipt.
    _custody: SourceCustody,
}
impl Clone for OriginalBootstrapInputs {
    fn clone(&self) -> Self {
        Self {
            owner: self.owner.clone(),
        }
    }
}
impl OriginalBootstrapInputs {
    fn new(
        app: AppHandle,
        custody: SourceCustody,
        invocation: &NativeSetupInvocation,
    ) -> Result<Self> {
        let bytes =
            tobkiri_harness_native::OriginalSourceHandle::<OriginalBootstrapOwner>::storage_bytes::<
                tobkiri_gate::ControlReservation,
            >();
        let funding = invocation
            .control()
            .reserve(tobkiri_gate::ControlKind::Frame, bytes as u64)?;
        Ok(Self {
            owner: tobkiri_harness_native::OriginalSourceHandle::try_new_precharged(
                OriginalBootstrapOwner {
                    app,
                    _custody: custody,
                },
                funding,
            )?,
        })
    }
}
impl BootstrapInputOwner for OriginalBootstrapInputs {
    fn original_module_metadata_policy(&self) -> Option<&'static ModuleMetadataPolicy> {
        // Explicit original-operation policy from the domain nativeKit. The
        // borrowed config roots alone do not grant any filesystem operation.
        Some(&ORIGINAL_PROFILE_SOURCE_METADATA)
    }
    fn with_roots<R>(
        &self,
        use_roots: impl for<'a> FnOnce(BootstrapRoots<'a>) -> R,
    ) -> std::result::Result<R, NativeDataIoError> {
        with_original_host_inputs(&self.owner.get().app, |inputs| {
            use_roots(BootstrapRoots {
                runtime: inputs
                    .root(NativeHostSourceRole::Runtime)
                    .expect("selected runtime"),
                working_directory: inputs
                    .root(NativeHostSourceRole::WorkingDirectory)
                    .expect("selected working directory"),
                development_workspace: inputs.root(NativeHostSourceRole::DevelopmentWorkspace),
            })
        })
        .map_err(|_| NativeDataIoError::Retired)
    }

    fn check_live(&self) -> std::result::Result<(), NativeDataIoError> {
        with_original_host_inputs(&self.owner.get().app, |_| ())
            .map_err(|_| NativeDataIoError::Retired)
    }
}

// The source capability is the SAME retained original App owner used above.
// Metadata inspection policy does not authorize this fixed persistence action.
impl OriginalAuthorityOwner for OriginalBootstrapInputs {
    fn with_original_user_data<R>(
        &self,
        operation: impl FnOnce(&std::path::Path) -> R,
    ) -> std::result::Result<R, OriginalAuthorityError> {
        crate::native_harness_host_inputs::with_original_authority_source(
            &self.owner.get().app,
            |inputs| operation(inputs.internal_runtime_storage_parent()),
        )
        .map_err(|_| OriginalAuthorityError::unavailable())
    }
    fn check_original_owner(&self) -> std::result::Result<(), OriginalAuthorityError> {
        with_original_host_inputs(&self.owner.get().app, |_| ())
            .map_err(|_| OriginalAuthorityError::unavailable())
    }
}
impl tobkiri_harness_native::BootstrapReviewOwner for OriginalBootstrapInputs {
    // with_roots above and this catalog borrow use the SAME immutable
    // AppConfig managed by Launcher setup (legacy.rs). Both reach app_dir via
    // NativeHostSourceInputs. Work retains this actual AppHandle owner through
    // IO cleanup; a module's resolved DATA does not establish that custody.
    fn with_original_catalog_source<R>(
        &self,
        operation: impl for<'host> FnOnce(tobkiri_harness_native::BootstrapCatalogSource<'host>) -> R,
    ) -> std::result::Result<R, OriginalAuthorityError> {
        crate::native_harness_host_inputs::with_original_review_source(
            &self.owner.get().app,
            |inputs| operation(inputs.catalog_source()),
        )
        .map_err(|_| OriginalAuthorityError::unavailable())
    }
    fn with_original_review_user_data<R>(
        &self,
        use_root: impl FnOnce(&std::path::Path) -> R,
    ) -> std::result::Result<R, OriginalAuthorityError> {
        crate::native_harness_host_inputs::with_original_review_source(
            &self.owner.get().app,
            |inputs| use_root(inputs.internal_runtime_storage_parent()),
        )
        .map_err(|_| OriginalAuthorityError::unavailable())
    }
}

/// Only detached DATA leaves the owning worker. Dropping this waiter never
/// unregisters the worker, cancels its Join, or transfers its Bootstrap/Store.
pub(crate) struct OriginalComponentJob {
    result: tokio::sync::oneshot::Receiver<Outcome>,
}
impl OriginalComponentJob {
    pub(crate) async fn result(self) -> Outcome {
        self.result.await.unwrap_or_else(|_| {
            Err(DetachedFailure::condition(
                Stage::Admission,
                Condition::WorkerDisconnected,
            ))
        })
    }
}

/// A review result can be consumed as display data only after the existing
/// worker completes its authority/Store cleanup and admits publication.
#[cfg(all(target_os = "macos", feature = "native-setup-review"))]
pub(crate) struct SetupReviewJob(OriginalComponentJob);

#[cfg(all(target_os = "macos", feature = "native-setup-review"))]
impl SetupReviewJob {
    pub(crate) async fn display(
        self,
        owner: crate::native_setup_review::owner::SetupReviewOwner,
    ) -> Result<(), SetupReviewDisplayError> {
        let listing =
            setup_listing(self.0.result().await).map_err(SetupReviewDisplayError::Worker)?;
        owner
            .display(listing)
            .await
            .map_err(SetupReviewDisplayError::Window)
    }
}

#[cfg(all(target_os = "macos", feature = "native-setup-review"))]
pub(crate) enum SetupReviewDisplayError {
    Worker(DetachedFailure),
    Window(crate::native_setup_review::owner::SetupReviewOwnerError),
}

#[cfg(all(target_os = "macos", feature = "native-setup-review"))]
fn setup_listing(outcome: Outcome) -> Result<String, DetachedFailure> {
    if let serde_json::Value::Object(mut fields) = outcome? {
        if fields.len() == 1 {
            if let Some(serde_json::Value::String(listing)) = fields.remove("listing") {
                return Ok(listing);
            }
        }
    }
    Err(DetachedFailure::integration(
        crate::native_worker_failure::IntegrationFailure::InvalidValueResult,
    ))
}

#[cfg(all(test, target_os = "macos", feature = "native-setup-review"))]
#[test]
fn setup_display_requires_a_completed_listing_result() {
    use serde_json::json;
    let listing = "{ \"exact\": \"入力\" }";
    assert_eq!(
        setup_listing(Ok(json!({"listing": listing}))).unwrap(),
        listing
    );
    for other in [
        json!({"observations": []}),
        json!({"listing": listing, "unexpected": true}),
        json!({"listing": {}}),
    ] {
        assert!(setup_listing(Ok(other)).is_err());
    }
    let cleanup_failure = crate::native_worker_failure::complete(
        Ok(json!({"listing": listing})),
        Some(DetachedFailure::condition(
            Stage::Finish,
            Condition::NativePanic,
        )),
        true,
    );
    assert!(matches!(
        setup_listing(cleanup_failure),
        Err(DetachedFailure::Cleanup { .. })
    ));
    let retired =
        crate::native_worker_failure::complete(Ok(json!({"listing": listing})), None, false);
    assert!(matches!(
        setup_listing(retired),
        Err(DetachedFailure::Publication { .. })
    ));
}

#[cfg(all(target_os = "macos", feature = "native-setup-review"))]
#[path = "setup_component_path.rs"]
mod setup_component_path;

#[cfg(all(target_os = "macos", feature = "native-setup-review"))]
mod selected_setup_component {
    use super::{
        setup_component_path, AppHandle, BootstrapModuleIndex, Digest, File, Manager, Result,
    };
    use std::fs::OpenOptions;
    use std::os::unix::fs::OpenOptionsExt;
    use tobkiri_harness_native::BootstrapModule;

    struct SetupComponent {
        sha256: &'static str,
        index: &'static BootstrapModuleIndex,
    }
    include!(concat!(env!("OUT_DIR"), "/setup_component.rs"));

    pub(super) fn open(
        app: &AppHandle,
    ) -> Result<Option<(File, Digest, &'static BootstrapModuleIndex)>> {
        let Some(component) = &COMPONENT else {
            return Ok(None);
        };
        let root = if env!("TOBKIRI_LOCAL_DEV_WORKSPACE") == "1" {
            std::path::PathBuf::from(
                option_env!("TOBKIRI_LOCAL_DEV_AUTHORITY_STAGE").ok_or_else(|| {
                    anyhow::anyhow!("Setup component development stage unavailable")
                })?,
            )
        } else {
            app.path().resource_dir()?.join("app")
        };
        let path = root.join(setup_component_path::RESOURCE);
        anyhow::ensure!(
            path.canonicalize()? == path,
            "Setup component resource is not physical"
        );
        let file = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC)
            .open(path)?;
        let metadata = file.metadata()?;
        anyhow::ensure!(
            metadata.is_file()
                && (1..=setup_component_path::MAX_COMPONENT_BYTES).contains(&metadata.len()),
            "Setup component resource is not a bounded regular file"
        );
        Ok(Some((
            file,
            Digest::parse(component.sha256)?,
            component.index,
        )))
    }
}

/// Review the existing Profile using the component fixed by the Launcher build.
#[cfg(all(target_os = "macos", feature = "native-setup-review"))]
pub(crate) async fn prepare_setup_review(
    app: &AppHandle,
    options: tobkiri_harness_native::BootstrapReviewOptions,
) -> Result<(), SetupReviewStartError> {
    use crate::native_setup_review::owner::SetupReviewWindowState;
    let window = app
        .get_webview_window("main")
        .ok_or(SetupReviewStartError::Window)?;
    let state = app
        .try_state::<SetupReviewWindowState>()
        .ok_or(SetupReviewStartError::Window)?;
    let port = app
        .try_state::<crate::AppConfig>()
        .ok_or(SetupReviewStartError::Window)?
        .kernel_port;
    let (artifact, expected, index) = selected_setup_component::open(app)
        .map_err(SetupReviewStartError::Component)?
        .ok_or(SetupReviewStartError::Unbound)?;
    let owner = state
        .begin(window, port)
        .await
        .map_err(|_| SetupReviewStartError::Window)?;
    let job = prepare_original_component(
        app.clone(),
        artifact,
        expected,
        index,
        OriginalOperation::ReviewListing(options),
    )
    .map(SetupReviewJob)
    .map_err(SetupReviewStartError::Worker)?;
    tauri::async_runtime::spawn(async move {
        if job.display(owner).await.is_err() {
            log::warn!("The Launcher could not display the native Setup review");
        }
    });
    Ok(())
}

#[cfg(all(target_os = "macos", feature = "native-setup-review"))]
pub(crate) enum SetupReviewStartError {
    Window,
    Unbound,
    Component(anyhow::Error),
    Worker(DetachedFailure),
}

/// Execute only the current closed bound subset. No caller receives Bootstrap,
/// its DATA budget, a funded buffer, or a function argument borrowing them.
pub(crate) fn prepare_original_component(
    app: AppHandle,
    artifact: File,
    expected: Digest,
    index: &'static BootstrapModuleIndex,
    operation: OriginalOperation,
) -> std::result::Result<OriginalComponentJob, DetachedFailure> {
    // An unbound operation is an integration failure before owner/Engine/IO,
    // not a fabricated domain denial or a partial successful execution.
    let operation = operation.admit()?;
    let state = app
        .try_state::<NativeRuntimeState>()
        .ok_or_else(|| {
            DetachedFailure::condition(Stage::Admission, Condition::RuntimeStateMissing)
        })?
        .inner()
        .clone();
    let (send, result) = tokio::sync::oneshot::channel();
    let runtime = state.clone();
    let lifecycle = state.0.jobs.clone();
    state
        .0
        .jobs
        .spawn(move |job| {
            let outcome = catch_unwind(AssertUnwindSafe(|| {
                tauri::async_runtime::block_on(run_original_job(
                    runtime, app, job, artifact, expected, index, operation,
                ))
            }));
            let (result, cleanup_verified) = match outcome {
                Ok(outcome) => outcome,
                Err(_) => (
                    Err(DetachedFailure::condition(
                        Stage::Operation,
                        Condition::NativePanic,
                    )),
                    false,
                ),
            };
            if !cleanup_verified {
                lifecycle.cleanup_failed();
            }
            if result.is_err() {
                log::error!("Native preparation failed; detached structured failure returned");
            }
            let _ = send.send(result);
            WorkerExit { cleanup_verified }
        })
        .map_err(|error| Detachment::new(None).native(&error, Stage::Admission))?;
    Ok(OriginalComponentJob { result })
}

pub(super) async fn caught<F: Future>(future: F) -> std::result::Result<F::Output, &'static str> {
    let mut future = std::pin::pin!(future);
    poll_fn(
        |context| match catch_unwind(AssertUnwindSafe(|| future.as_mut().poll(context))) {
            Ok(value) => value.map(Ok),
            Err(_) => Poll::Ready(Err("native operation panicked")),
        },
    )
    .await
}

async fn run_original_job(
    runtime: NativeRuntimeState,
    app: AppHandle,
    job: JobTicket,
    artifact: File,
    expected: Digest,
    index: &'static BootstrapModuleIndex,
    operation: dispatch::AdmittedOperation,
) -> (Outcome, bool) {
    // No Store/IO exists during these synchronous stages. Owner creation is
    // already represented by the registered worker before reaching this code.
    let mut stage = Stage::Owner;
    let review = matches!(&operation, dispatch::AdmittedOperation::ReviewListing(_));
    let prepared = catch_unwind(AssertUnwindSafe(|| -> Result<_> {
        let invocation = runtime.begin(&app, &job)?;
        let parent = job.attach_invocation(&invocation)?;
        let source = OriginalBootstrapInputs::new(app, job.source_custody()?, &invocation)?;
        // Pre-admit the existing uncertain-IO ledger record before old-state
        // IO. Only actual source/unfinished IO can move into that ledger.
        let retention = if review {
            Some(invocation.control().reserve(
                tobkiri_gate::ControlKind::Registration,
                (2 * std::mem::size_of::<authority::Retained>() + 1024) as u64,
            )?)
        } else {
            None
        };
        stage = Stage::EngineCompile;
        let prepared = NativeComponentPreparation::new_with_cancellation_parent(
            invocation.clone(),
            source.clone(),
            &parent,
        )?
        .compile(artifact, &expected)?;
        Ok((prepared, source, invocation, parent, retention))
    }));
    let mut detachment = Detachment::new(job.invocation());
    let prepared = match prepared {
        Ok(Ok(prepared)) => prepared,
        Ok(Err(error)) => {
            let detached = detachment.native(&error, stage);
            drop(error);
            return (Err(detached), true);
        }
        Err(_) => {
            return (
                Err(DetachedFailure::condition(stage, Condition::NativePanic)),
                true,
            );
        }
    };
    let (prepared, source, invocation, cancellation, retention) = prepared;
    let runner = match caught(prepared.initialize(index)).await {
        Ok(Ok(Ok(runner))) => runner,
        Ok(Ok(Err(domain))) => {
            let detached = detachment.domain_data(&domain);
            drop(domain);
            return (Err(detached), true);
        }
        Ok(Err(error)) => {
            let clean = crate::native_worker_failure::initialization_cleanup_verified(&error);
            let detached = detachment.native(&error, Stage::Initialize);
            drop(error);
            return (Err(detached), clean);
        }
        Err(_) => {
            return (
                Err(DetachedFailure::condition(
                    Stage::Initialize,
                    Condition::NativePanic,
                )),
                false,
            );
        }
    };
    let (outcome, clean, uncertain) = finish_bound_job(
        runner,
        operation,
        &job,
        &runtime.0.jobs,
        &mut detachment,
        #[cfg(test)]
        None,
    )
    .await;
    if let Some(cleanup) = uncertain {
        runtime
            .0
            .retained_authority
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .push(authority::Retained::new(
                cleanup,
                source,
                invocation,
                cancellation,
                retention.expect("review IO retention was pre-admitted"),
            ));
    }
    drop(detachment);
    (outcome, clean)
}

// The result and its optional test signal have a single destruction order.
struct DispatchResult {
    result: std::result::Result<dispatch::DispatchOutput, dispatch::Failed>,
    // Field order makes this signal follow destruction of the actual result,
    // including every owned cause/context, rather than merely marking a branch.
    #[cfg(test)]
    _drop_probe: Option<DispatchResultDropProbe>,
}
#[cfg(test)]
struct DispatchResultDropProbe(Arc<AtomicBool>);
#[cfg(test)]
impl Drop for DispatchResultDropProbe {
    fn drop(&mut self) {
        self.0.store(true, std::sync::atomic::Ordering::Release);
    }
}

/// Same owning worker in production and component tests. This function takes
/// a finite admitted operation, never arbitrary callback code.
async fn finish_bound_job<S: BootstrapInputOwner + tobkiri_harness_native::BootstrapReviewOwner>(
    mut runner: NativeBootstrap<S>,
    operation: dispatch::AdmittedOperation,
    job: &JobTicket,
    jobs: &Jobs,
    detachment: &mut Detachment,
    #[cfg(test)] result_dropped: Option<Arc<AtomicBool>>,
) -> (
    Outcome,
    bool,
    Option<tobkiri_harness_native::OriginalAuthorityCleanup>,
) {
    // Bootstrap stays outside the caught future and is always consumed by
    // actual finish, even when dispatch polling panics or returns an error.
    let mut result = Some(DispatchResult {
        result: caught(dispatch::execute(&mut runner, operation))
            .await
            .unwrap_or_else(|_| Err(dispatch::Failed::panic())),
        #[cfg(test)]
        _drop_probe: result_dropped.map(DispatchResultDropProbe),
    });
    let actual = &result.as_ref().expect("live dispatch result").result;
    let release_review = matches!(actual, Err(error) if error.owns_review_diagnostics());
    // Detachment owns every projected byte; it leaves no borrowed message,
    // cause or context, including when projection itself fails partway.
    let original = match actual {
        Ok(observations) => observations.detach(detachment),
        Err(error) => Err(error.detach(detachment)),
    };
    if release_review {
        // Review's diagnostic reservation belongs to runner.review. Destroy
        // its owned diagnostics BEFORE finish consumes that reservation. FD,
        // source, close receipt and uncertain cleanup remain solely in runner.
        drop(result.take());
    }
    let uncertain = runner.original_review_uncertain_cleanup();
    let cleanup = caught(runner.finish()).await;
    let cleanup_error = match &cleanup {
        Ok(Ok(())) => None,
        Ok(Err(error)) => Some(detachment.native(error, Stage::Finish)),
        Err(_) => Some(DetachedFailure::condition(
            Stage::Finish,
            Condition::NativePanic,
        )),
    };
    // Getter/CoreBuffer results keep their own funding through physical finish.
    // The owned Review diagnostics have already died under their live Read.
    drop(result);
    drop(cleanup);
    // A whole recovery scope may have been OPEN when this handle was sampled
    // before finish. Retain source/invocation only for the actual remaining
    // uncertain IO after Store/Join/journal close, never for detached DATA.
    let uncertain = uncertain.filter(|cleanup| !cleanup.is_verified());
    let permitted = original.is_err() || job.ensure_accepting(jobs).is_ok();
    let clean = cleanup_error.is_none();
    let output = crate::native_worker_failure::complete(original, cleanup_error, permitted);
    (output, clean, uncertain)
}

#[cfg(all(test, target_os = "macos"))]
mod tests {
    use super::*;
    use std::{sync::atomic::Ordering, time::Duration};

    #[test]
    fn original_shutdown_flag_refuses_new_jobs_with_an_already_open_owner() {
        let root = std::env::temp_dir().join(format!(
            "tobkiri-existing-native-owner-{}-{}",
            std::process::id(),
            rand::random::<u64>()
        ));
        std::fs::create_dir(&root).unwrap();
        let root = root.canonicalize().unwrap();
        let shutdown = Arc::new(AtomicBool::new(false));
        let state = NativeRuntimeState::new(shutdown.clone());
        *state.0.owner.lock().unwrap() =
            OwnedRuntime::Native(RuntimeOwner::open(root.join("core")).unwrap());
        shutdown.store(true, Ordering::SeqCst);
        assert!(state
            .0
            .jobs
            .spawn(|_| panic!("existing owner bypassed original shutdown"))
            .is_err());
        state
            .stop_until(Instant::now() + Duration::from_secs(20))
            .unwrap();
        assert!(matches!(
            *state.0.owner.lock().unwrap(),
            OwnedRuntime::Closed
        ));
        state.stop_until(Instant::now()).unwrap();
        std::fs::remove_dir_all(root).unwrap();
    }
}
