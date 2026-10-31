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
    NativeBootstrap, NativeComponentPreparation, ORIGINAL_PROFILE_SOURCE_METADATA,
};
use tobkiri_wasm::NativeDataIoError;

#[path = "native_harness_dispatch.rs"]
mod dispatch;
pub(crate) use crate::native_worker_failure::UnboundOperation;
pub(crate) use dispatch::{OriginalGetter, OriginalOperation};

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
}

impl NativeRuntimeState {
    pub(crate) fn new(shutdown: Arc<AtomicBool>) -> Self {
        Self(Arc::new(RuntimeState {
            owner: Mutex::new(OwnedRuntime::Unopened),
            jobs: Jobs::new(shutdown),
            finalization: Finalization::default(),
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
    app: AppHandle,
    // Field order releases the actual AppHandle before publishing its receipt.
    _custody: SourceCustody,
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
        with_original_host_inputs(&self.app, |inputs| {
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
        with_original_host_inputs(&self.app, |_| ()).map_err(|_| NativeDataIoError::Retired)
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
    let prepared = catch_unwind(AssertUnwindSafe(|| -> Result<_> {
        let invocation = runtime.begin(&app, &job)?;
        let parent = job.attach_invocation(&invocation)?;
        let source = OriginalBootstrapInputs {
            app,
            _custody: job.source_custody()?,
        };
        stage = Stage::EngineCompile;
        NativeComponentPreparation::new_with_cancellation_parent(invocation, source, &parent)?
            .compile(artifact, &expected)
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
            )
        }
    };
    let output = finish_bound_job(runner, operation, &job, &runtime.0.jobs, &mut detachment).await;
    drop(detachment);
    output
}

/// Same owning worker in production and component tests. This function takes
/// a finite admitted operation, never arbitrary callback code.
async fn finish_bound_job<S: BootstrapInputOwner>(
    mut runner: NativeBootstrap<S>,
    operation: dispatch::AdmittedOperation,
    job: &JobTicket,
    jobs: &Jobs,
    detachment: &mut Detachment,
) -> (Outcome, bool) {
    // Bootstrap stays outside the caught future and is always consumed by
    // actual finish, even when dispatch polling panics or returns an error.
    let result = caught(dispatch::execute(&mut runner, operation))
        .await
        .unwrap_or_else(|_| Err(dispatch::Failed::panic()));
    // Keep actual errors/buffers live during detachment and finish. Only the
    // closed DATA projection can outlive this worker and original account.
    let original = match &result {
        Ok(observations) => observations.detach(detachment),
        Err(error) => Err(error.detach(detachment)),
    };
    let cleanup = caught(runner.finish()).await;
    let cleanup_error = match &cleanup {
        Ok(Ok(())) => None,
        Ok(Err(error)) => Some(detachment.native(error, Stage::Finish)),
        Err(_) => Some(DetachedFailure::condition(
            Stage::Finish,
            Condition::NativePanic,
        )),
    };
    // Actual funded observations/errors are destroyed after physical finish,
    // before the worker publishes the detached result or its Join receipt.
    drop(result);
    drop(cleanup);
    let permitted = original.is_err() || job.ensure_accepting(jobs).is_ok();
    let clean = cleanup_error.is_none();
    let output = crate::native_worker_failure::complete(original, cleanup_error, permitted);
    (output, clean)
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
