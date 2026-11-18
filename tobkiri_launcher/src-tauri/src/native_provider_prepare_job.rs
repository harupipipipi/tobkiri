//! Tracked, read-only execution of the Provider PREPARE admitted at Shell launch.

use std::{
    panic::{catch_unwind, AssertUnwindSafe},
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc, Mutex,
    },
    time::Instant,
};

use anyhow::{anyhow, ensure, Context, Result};
use tauri::{AppHandle, Manager};
use tobkiri_core::{
    CoreBuffer, CoreValue, LegacyProviderRegistryRead, NativeArtifactError,
    NativeArtifactInspection, MAX_BYTES,
};
use tobkiri_gate::{CapacityKind, ControlKind, CoreCancellation};
use tobkiri_wasm::NativeDataIoError;

use super::{
    caught,
    launch_metadata::{reacquire_provider_prepare_pack, REGISTRY_VERIFICATION_BYTES},
    NativeRuntimeState,
};
use crate::{
    defaultspack_authority::{require_current_launch_identity, SignedApplicationResolver},
    defaultspack_manager::{ApplicationProcessManager, ProviderPrepareListener},
    native_runtime_jobs::{JobJoin, JobTicket, WorkerExit},
    AppConfig,
};

type ContinueCurrent = Arc<dyn Fn() -> Result<()> + Send + Sync>;

/// A running Provider can narrow its Host deadline when the Core begins.
pub(crate) struct ProviderPrepareDeadline(Mutex<Instant>);

impl ProviderPrepareDeadline {
    pub(crate) fn new(deadline: Instant) -> Self {
        Self(Mutex::new(deadline))
    }

    pub(crate) fn current(&self) -> Instant {
        *self.0.lock().unwrap_or_else(|error| error.into_inner())
    }

    fn restrict(&self, core_deadline: Instant) -> Instant {
        let mut deadline = self.0.lock().unwrap_or_else(|error| error.into_inner());
        *deadline = (*deadline).min(core_deadline);
        *deadline
    }
}

/// Single-result handle for an admitted Provider worker and its physical Join.
pub(crate) struct PendingProviderPrepare {
    join: JobJoin,
    receive: mpsc::Receiver<Result<Prepared>>,
    deadline: Arc<ProviderPrepareDeadline>,
    continue_current: ContinueCurrent,
    cancelled: AtomicBool,
}

impl PendingProviderPrepare {
    /// Cancel the fixed preparation and its original attached invocation.
    pub(crate) fn cancel(&self) -> Result<()> {
        self.cancelled.store(true, Ordering::Release);
        self.join.cancel()
    }

    /// Wait for physical worker and source cleanup independently of result receipt.
    pub(crate) fn join_until(&self, deadline: Instant) -> Result<()> {
        self.join.join_until(deadline)
    }

    /// Poll for verified physical cleanup without blocking peer handling.
    pub(crate) fn try_join(&self) -> Result<bool> {
        self.join.try_join()
    }

    /// Take one terminal result after the actual worker and its sources have ended.
    /// A planner rejection remains downcastable to `NativeArtifactError`.
    pub(crate) fn finish_until(&self, deadline: Instant) -> Result<CoreBuffer> {
        self.join_until(deadline)?;
        self.check_delivery_live()?;
        (self.continue_current)()?;
        let prepared = self
            .receive
            .try_recv()
            .context("native Provider preparation produced no terminal result")??;
        prepared
            .manager
            .require_provider_prepare_admission(&prepared.listener)?;
        self.check_delivery_live()?;
        prepared.result
    }

    fn check_delivery_live(&self) -> Result<()> {
        if self.cancelled.load(Ordering::Acquire) {
            return Err(NativeArtifactError::from(NativeDataIoError::Cancelled).into());
        }
        check_live(self.deadline.current(), None)
    }
}

impl Drop for PendingProviderPrepare {
    fn drop(&mut self) {
        let _ = self.cancel();
    }
}

struct Request {
    listener: Arc<ProviderPrepareListener>,
    bytes: Vec<u8>,
    deadline: Arc<ProviderPrepareDeadline>,
    read: LegacyProviderRegistryRead,
    continue_current: ContinueCurrent,
}

struct Prepared {
    manager: Arc<ApplicationProcessManager>,
    listener: Arc<ProviderPrepareListener>,
    result: Result<CoreBuffer>,
}

/// Start the installed Provider PREPARE for the original managed Launcher.
///
/// The trusted Host adapter owns the route, activation epoch, Registry reader,
/// and continuing eligibility. The guest receives the bounded typed payload.
pub(crate) fn start_provider_prepare(
    app: &AppHandle,
    listener: Arc<ProviderPrepareListener>,
    request: Vec<u8>,
    deadline: Arc<ProviderPrepareDeadline>,
    read: LegacyProviderRegistryRead,
    continue_current: ContinueCurrent,
) -> Result<PendingProviderPrepare> {
    check_live(deadline.current(), None)?;
    ensure!(
        !request.is_empty() && request.capacity() <= MAX_BYTES,
        "native Provider preparation request exceeds its input envelope"
    );
    listener.verified_source().identity.validate()?;
    let runtime = app
        .try_state::<NativeRuntimeState>()
        .context("native Provider preparation runtime owner is unavailable")?
        .inner()
        .clone();
    let jobs = runtime.0.jobs.clone();
    let request = Request {
        listener,
        bytes: request,
        deadline: Arc::clone(&deadline),
        read,
        continue_current: Arc::clone(&continue_current),
    };
    let app = app.clone();
    let (send, receive) = mpsc::sync_channel(1);
    let join = jobs.spawn_joinable(move |job| {
        let (result, cleanup_verified) = catch_unwind(AssertUnwindSafe(|| {
            tauri::async_runtime::block_on(run(app, runtime, job, request))
        }))
        .unwrap_or_else(|_| {
            (
                Err(anyhow!("native Provider preparation worker panicked")),
                false,
            )
        });
        let _ = send.send(result);
        WorkerExit { cleanup_verified }
    })?;
    Ok(PendingProviderPrepare {
        join,
        receive,
        deadline,
        continue_current,
        cancelled: AtomicBool::new(false),
    })
}

async fn run(
    app: AppHandle,
    runtime: NativeRuntimeState,
    job: JobTicket,
    request: Request,
) -> (Result<Prepared>, bool) {
    let Request {
        listener,
        bytes,
        deadline,
        read,
        continue_current,
    } = request;
    let identity = &listener.verified_source().identity;
    let admitted = (|| {
        check_live(deadline.current(), None)?;
        job.ensure_accepting(&runtime.0.jobs)?;
        continue_current()?;
        check_live(deadline.current(), None)?;
        let config = app
            .try_state::<AppConfig>()
            .context("native Provider preparation configuration is unavailable")?;
        let manager = app
            .try_state::<Arc<ApplicationProcessManager>>()
            .context("native Provider preparation Application manager is unavailable")?
            .inner()
            .clone();
        require_current_launch_identity(&config, identity)?;
        manager.require_provider_prepare_admission(&listener)?;
        let invocation = runtime.begin(&app, &job)?;
        let cancellation = job.attach_invocation(&invocation)?;
        let execution_deadline = deadline.restrict(invocation.deadline()?);
        check_live(execution_deadline, Some(&cancellation))?;
        let custody = job.source_custody()?;
        let sources = SignedApplicationResolver::resolve_sources(&config)?;
        check_live(execution_deadline, Some(&cancellation))?;
        let request_bytes = u64::try_from(bytes.capacity())?;
        let input_funding = invocation.reserve(CapacityKind::VfsBuffer, request_bytes)?;
        let control_funding = invocation.control().reserve(
            ControlKind::Frame,
            request_bytes + REGISTRY_VERIFICATION_BYTES,
        )?;
        let inspection =
            NativeArtifactInspection::from_invocation(sources, invocation, &cancellation)?;
        Ok::<_, anyhow::Error>((
            config,
            manager,
            cancellation,
            execution_deadline,
            custody,
            input_funding,
            control_funding,
            inspection,
        ))
    })();
    let (
        config,
        manager,
        cancellation,
        execution_deadline,
        custody,
        _input_funding,
        _control_funding,
        mut inspection,
    ) = match admitted {
        Ok(admitted) => admitted,
        Err(error) => return (Err(error), true),
    };

    let checkpoint = || {
        check_live(execution_deadline, Some(&cancellation))?;
        job.ensure_accepting(&runtime.0.jobs)?;
        manager.require_provider_prepare_admission(&listener)
    };
    let revalidate = || {
        checkpoint()?;
        continue_current()?;
        require_current_launch_identity(&config, identity)?;
        ensure!(
            listener.owns_socket(),
            "native Provider preparation listener lost its socket"
        );
        checkpoint()
    };
    let outcome = {
        let prepare = caught(async {
            let graph = inspection.allocation_budget()?.clone();
            let input = CoreValue::parse_guest(&bytes, &graph)?;
            drop(bytes);
            let pack = reacquire_provider_prepare_pack(
                &inspection,
                listener.verified_source(),
                checkpoint,
            )?;
            revalidate()?;
            let read_manager = Arc::clone(&manager);
            let read_listener = Arc::clone(&listener);
            let continue_read = Arc::clone(&continue_current);
            let output = inspection
                .invoke_live_legacy_provider_prepare(
                    &pack,
                    &identity.profile_id,
                    &input,
                    guarded_registry_read(
                        read,
                        Arc::new(move || {
                            read_manager.require_provider_prepare_admission(&read_listener)?;
                            continue_read()
                        }),
                        cancellation.clone(),
                        execution_deadline,
                    ),
                )
                .await?;
            Ok::<_, anyhow::Error>(output.encode_guest(&graph)?)
        });
        tokio::pin!(prepare);
        tokio::select! {
            result = &mut prepare => result,
            () = tokio::time::sleep_until(execution_deadline.into()) => {
                cancellation.cancel();
                prepare.await
            }
        }
    };
    let finished = inspection.finish().await;
    let execution_returned = outcome.is_ok();
    let (result, cleanup_verified) = match finished {
        Ok(sources) => {
            drop(sources);
            (
                revalidate().and_then(|()| outcome.map_err(|error| anyhow!(error))?),
                execution_returned,
            )
        }
        // These are emitted by finish after the Store has retired and all
        // physical inspection work has joined.
        Err(
            error @ NativeArtifactError::Execution(
                NativeDataIoError::Cancelled | NativeDataIoError::Deadline,
            ),
        ) => (Err(error.into()), execution_returned),
        Err(error) => (Err(error.into()), false),
    };
    drop(continue_current);
    drop(config);
    drop(app);
    drop(custody);
    (
        Ok(Prepared {
            manager,
            listener,
            result,
        }),
        cleanup_verified,
    )
}

fn check_live(deadline: Instant, cancellation: Option<&CoreCancellation>) -> Result<()> {
    if Instant::now() >= deadline {
        return Err(NativeArtifactError::from(NativeDataIoError::Deadline).into());
    }
    if cancellation.is_some_and(CoreCancellation::is_cancelled) {
        return Err(NativeArtifactError::from(NativeDataIoError::Cancelled).into());
    }
    Ok(())
}

fn guarded_registry_read(
    mut read: LegacyProviderRegistryRead,
    continue_current: ContinueCurrent,
    cancellation: CoreCancellation,
    deadline: Instant,
) -> LegacyProviderRegistryRead {
    Box::new(move |budget| {
        let check = || {
            check_live(deadline, Some(&cancellation))?;
            continue_current()?;
            check_live(deadline, Some(&cancellation))
        };
        check()?;
        let observed = read(budget);
        check()?;
        observed
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;
    use tobkiri_core::{AllocationBudget, AllocationError, AllocationRequest};

    struct TestBudget;

    impl AllocationBudget for TestBudget {
        fn reserve(&self, _: AllocationRequest) -> Result<(), AllocationError> {
            Ok(())
        }

        fn release(&self, _: u64) {}
    }

    #[test]
    fn polling_does_not_report_cleanup_for_a_running_worker() {
        let jobs = crate::native_runtime_jobs::Jobs::new(Arc::new(AtomicBool::new(false)));
        let (release, wait) = mpsc::sync_channel(0);
        let join = jobs
            .spawn_joinable(move |_| {
                wait.recv().unwrap();
                WorkerExit {
                    cleanup_verified: true,
                }
            })
            .unwrap();
        assert!(!join.try_join().unwrap());
        release.send(()).unwrap();
        join.join_until(Instant::now() + Duration::from_secs(10))
            .unwrap();
        assert!(join.try_join().unwrap());
    }

    #[test]
    fn cancellation_after_physical_join_still_discards_the_buffered_result() {
        let jobs = crate::native_runtime_jobs::Jobs::new(Arc::new(AtomicBool::new(false)));
        let (send, receive) = mpsc::sync_channel(1);
        let join = jobs
            .spawn_joinable(move |_| {
                assert!(send.send(Err(anyhow!("buffered result"))).is_ok());
                WorkerExit {
                    cleanup_verified: true,
                }
            })
            .unwrap();
        let deadline = Instant::now() + Duration::from_secs(10);
        let pending = PendingProviderPrepare {
            join,
            receive,
            deadline: Arc::new(ProviderPrepareDeadline::new(deadline)),
            continue_current: Arc::new(|| Ok(())),
            cancelled: AtomicBool::new(false),
        };
        pending.join_until(deadline).unwrap();
        pending.cancel().unwrap();
        let error = pending.finish_until(deadline).unwrap_err();
        assert!(matches!(
            error.downcast_ref::<NativeArtifactError>(),
            Some(NativeArtifactError::Execution(NativeDataIoError::Cancelled))
        ));
    }

    #[test]
    fn failed_readiness_keeps_registry_reader_unreached() {
        let mut reader = guarded_registry_read(
            Box::new(|_| panic!("Registry read after readiness was lost")),
            Arc::new(|| anyhow::bail!("readiness lost")),
            CoreCancellation::new(),
            Instant::now() + Duration::from_secs(10),
        );
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        assert_eq!(reader(&budget).unwrap_err().to_string(), "readiness lost");
    }

    #[test]
    fn cancelled_registry_read_does_not_return_a_document() {
        let cancel = CoreCancellation::new();
        let reader_cancel = cancel.clone();
        let reached_check = Arc::new(AtomicBool::new(false));
        let reached_read = Arc::clone(&reached_check);
        let mut reader = guarded_registry_read(
            Box::new(move |budget| {
                reached_read.store(true, Ordering::Relaxed);
                reader_cancel.cancel();
                Ok(Some(CoreBuffer::copy_from(b"{}", budget)?))
            }),
            Arc::new(|| Ok(())),
            cancel,
            Instant::now() + Duration::from_secs(10),
        );
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let error = reader(&budget).unwrap_err();
        assert!(reached_check.load(Ordering::Relaxed));
        assert!(matches!(
            error.downcast_ref::<NativeArtifactError>(),
            Some(NativeArtifactError::Execution(NativeDataIoError::Cancelled))
        ));
    }
}
