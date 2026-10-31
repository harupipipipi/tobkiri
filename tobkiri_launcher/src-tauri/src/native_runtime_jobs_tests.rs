//! Real owner/preparation custody tests; none claims artifact or GUI admission.
use super::*;
use std::{
    fs::{self, File},
    io::Write,
    path::{Path, PathBuf},
    sync::{atomic::AtomicUsize, mpsc},
};
use tobkiri_core::{Digest, RuntimeOwner};
use tobkiri_harness_native::{NativeComponentOwner, NativeComponentPreparation};
use tobkiri_wasm::NativeDataIoError;

struct Root(PathBuf);
impl Root {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!(
            "tobkiri-native-jobs-{}-{}",
            std::process::id(),
            rand::random::<u64>()
        ));
        fs::create_dir(&path).unwrap();
        Self(path.canonicalize().unwrap())
    }
}
impl Drop for Root {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}
fn receive<T>(receiver: &mpsc::Receiver<T>) -> T {
    receiver.recv_timeout(Duration::from_secs(20)).unwrap()
}
fn jobs() -> Jobs {
    Jobs::new(Arc::new(AtomicBool::new(false)))
}
fn host_runtime(worker: impl std::future::Future<Output = WorkerExit>) -> WorkerExit {
    tauri::async_runtime::block_on(worker)
}

struct Source {
    _directory: File,
    _custody: SourceCustody,
    checks: Arc<AtomicUsize>,
    pause: Option<(mpsc::Sender<()>, Mutex<mpsc::Receiver<()>>)>,
    panic_after_compile: bool,
}
impl NativeComponentOwner for Source {
    fn check_component_live(&self) -> std::result::Result<(), NativeDataIoError> {
        let count = self.checks.fetch_add(1, Ordering::SeqCst) + 1;
        // Actual compile has completed at this check, but compile() has not
        // returned or published its component. No fake compiler is substituted.
        if count == 5 {
            if let Some((entered, resume)) = &self.pause {
                entered.send(()).unwrap();
                resume
                    .lock()
                    .unwrap()
                    .recv_timeout(Duration::from_secs(20))
                    .unwrap();
            }
            assert!(!self.panic_after_compile, "supplied source callback panic");
        }
        Ok(())
    }
}
fn source(job: &JobTicket, root: &Path) -> Source {
    Source {
        _directory: File::open(root).unwrap(),
        _custody: job.source_custody().unwrap(),
        checks: Arc::new(AtomicUsize::new(0)),
        pause: None,
        panic_after_compile: false,
    }
}
fn artifact(root: &Path) -> (File, Digest) {
    // The real binary encoding of an empty component. Compile-only fixture:
    // no Runtime.init export, Profile, authority or fabricated startup result.
    let bytes = b"\0asm\x0d\0\x01\0";
    let path = root.join("compile-only.wasm");
    File::create(&path).unwrap().write_all(bytes).unwrap();
    (File::open(path).unwrap(), Digest::hash(bytes))
}

#[test]
fn one_unfunded_owner_opening_is_tracked_and_stop_prevents_engine_admission() {
    let root = Root::new();
    let jobs = jobs();
    let (entered, opened) = mpsc::channel();
    let (resume, wait) = mpsc::channel();
    let worker_jobs = jobs.clone();
    let path = root.0.clone();
    jobs.spawn(move |job| {
        let owner = RuntimeOwner::open(path.join("data")).unwrap();
        entered.send(()).unwrap();
        receive(&wait);
        assert!(job.ensure_accepting(&worker_jobs).is_err());
        assert!(job
            .attach_invocation(&owner.begin_native_setup().unwrap())
            .is_err());
        WorkerExit {
            cleanup_verified: true,
        }
    })
    .unwrap();
    receive(&opened);
    for _ in 0..8 {
        assert!(jobs
            .spawn(|_| panic!("unfunded fanout started a thread"))
            .is_err());
    }
    assert_eq!(jobs.0.state.lock().unwrap().jobs.len(), 1);
    jobs.request_shutdown();
    assert!(jobs.drain_until(Instant::now()).is_err());
    assert_eq!(jobs.0.state.lock().unwrap().phase, Phase::Stopping);
    resume.send(()).unwrap();
    jobs.drain_until(Instant::now() + Duration::from_secs(20))
        .unwrap();
    jobs.mark_closed().unwrap();
    jobs.mark_closed().unwrap();
    assert!(jobs
        .spawn(|_| panic!("closed owner admitted work"))
        .is_err());
}

#[test]
fn caller_drop_and_cancel_inside_real_compile_keep_join_and_source_until_final_drain() {
    let root = Root::new();
    let jobs = jobs();
    let (entered, compiling) = mpsc::channel();
    let (resume, wait) = mpsc::channel();
    let (observed, observation) = mpsc::channel();
    let (reply, caller) = tokio::sync::oneshot::channel::<String>();
    drop(caller);
    let path = root.0.clone();
    jobs.spawn(move |job| {
        host_runtime(async move {
            let owner = RuntimeOwner::open(path.join("data")).unwrap();
            let invocation = owner.begin_native_setup().unwrap();
            let deadline = invocation.deadline().unwrap();
            let parent = job.attach_invocation(&invocation).unwrap();
            let mut source = source(&job, &path);
            source.pause = Some((entered, Mutex::new(wait)));
            let source_status = source._custody.0.clone();
            assert!(observed
                .send((invocation.clone(), deadline, source_status))
                .is_ok());
            let (file, digest) = artifact(&path);
            let error = NativeComponentPreparation::new_with_cancellation_parent(
                invocation.clone(),
                source,
                &parent,
            )
            .unwrap_or_else(|error| panic!("preparation failed: {error:#}"))
            .compile(file, &digest)
            .err()
            .expect("cancelled compile published a component");
            assert!(format!("{error:#}").contains("cancelled"));
            assert_eq!(invocation.deadline().unwrap(), deadline);
            assert!(reply.send(format!("{error:#}")).is_err());
            WorkerExit {
                cleanup_verified: true,
            }
        })
    })
    .unwrap();
    let (invocation, deadline, source) = receive(&observation);
    receive(&compiling);
    assert!(source.live.load(Ordering::Acquire));
    assert!(invocation.capacity_snapshot().unwrap().peak_charged_bytes >= 8);
    jobs.request_shutdown();
    assert!(jobs.drain_until(Instant::now()).is_err());
    {
        let state = jobs.0.state.lock().unwrap();
        assert_eq!(state.phase, Phase::Stopping);
        assert_eq!(state.jobs.len(), 1);
        assert!(state.jobs[0].thread.is_some() && !state.jobs[0].joined);
    }
    assert!(jobs.mark_closed().is_err());
    resume.send(()).unwrap();
    jobs.drain_until(Instant::now() + Duration::from_secs(20))
        .unwrap();
    assert!(!source.live.load(Ordering::Acquire));
    assert_eq!(invocation.deadline().unwrap(), deadline);
    assert_eq!(invocation.capacity_snapshot().unwrap().charged_bytes, 0);
    jobs.mark_closed().unwrap();
}

#[test]
fn invocation_binding_is_single_use_and_foreign_account_parent_is_rejected() {
    let root = Root::new();
    let jobs = jobs();
    let (done, completed) = mpsc::channel();
    let path = root.0.clone();
    jobs.spawn(move |job| {
        let one = RuntimeOwner::open(path.join("one")).unwrap();
        let two = RuntimeOwner::open(path.join("two")).unwrap();
        let invocation = one.begin_native_setup().unwrap();
        let other = two.begin_native_setup().unwrap();
        let parent = job.attach_invocation(&invocation).unwrap();
        assert!(job.attach_invocation(&other).is_err());
        assert!(job.jobs.0.state.lock().unwrap().jobs[0]
            .invocation
            .as_ref()
            .unwrap()
            .same_invocation(&invocation));
        let source = source(&job, &path);
        let error =
            NativeComponentPreparation::new_with_cancellation_parent(other, source, &parent)
                .err()
                .expect("foreign cancellation parent was admitted");
        let outer: &(dyn std::error::Error + 'static) = error.as_ref();
        assert!(matches!(
            outer.downcast_ref::<tobkiri_gate::CoreControlError>(),
            Some(tobkiri_gate::CoreControlError::Invalid)
        ));
        done.send(()).unwrap();
        WorkerExit {
            cleanup_verified: true,
        }
    })
    .unwrap();
    receive(&completed);
    jobs.drain_until(Instant::now() + Duration::from_secs(20))
        .unwrap();
    jobs.mark_closed().unwrap();
}

#[test]
fn synchronous_source_panic_after_actual_compile_unwinds_and_releases_before_join_receipt() {
    let root = Root::new();
    let jobs = jobs();
    let (done, completed) = mpsc::channel();
    let path = root.0.clone();
    jobs.spawn(move |job| {
        host_runtime(async move {
            let owner = RuntimeOwner::open(path.join("data")).unwrap();
            let invocation = owner.begin_native_setup().unwrap();
            let parent = job.attach_invocation(&invocation).unwrap();
            let mut source = source(&job, &path);
            source.panic_after_compile = true;
            let status = source._custody.0.clone();
            let checks = source.checks.clone();
            let (file, digest) = artifact(&path);
            let prepared = NativeComponentPreparation::new_with_cancellation_parent(
                invocation, source, &parent,
            )
            .unwrap();
            assert!(catch_unwind(AssertUnwindSafe(|| prepared.compile(file, &digest))).is_err());
            assert_eq!(checks.load(Ordering::SeqCst), 5);
            assert!(!status.live.load(Ordering::Acquire));
            done.send(()).unwrap();
            WorkerExit {
                cleanup_verified: true,
            }
        })
    })
    .unwrap();
    receive(&completed);
    jobs.drain_until(Instant::now() + Duration::from_secs(20))
        .unwrap();
    jobs.mark_closed().unwrap();
}

#[test]
fn async_panic_retains_outer_owned_preparation_for_explicit_cleanup() {
    let root = Root::new();
    let jobs = jobs();
    let (done, completed) = mpsc::channel();
    let path = root.0.clone();
    jobs.spawn(move |job| {
        host_runtime(async move {
            let owner = RuntimeOwner::open(path.join("data")).unwrap();
            let invocation = owner.begin_native_setup().unwrap();
            let parent = job.attach_invocation(&invocation).unwrap();
            let source = source(&job, &path);
            let status = source._custody.0.clone();
            let (file, digest) = artifact(&path);
            let prepared = NativeComponentPreparation::new_with_cancellation_parent(
                invocation, source, &parent,
            )
            .unwrap()
            .compile(file, &digest)
            .unwrap();
            let result: std::result::Result<(), &str> =
                crate::native_harness_bootstrap::caught(async {
                    std::future::ready(()).await;
                    panic!("actual borrowed operation future panic");
                })
                .await;
            assert!(result.is_err());
            assert!(status.live.load(Ordering::Acquire));
            drop(prepared);
            assert!(!status.live.load(Ordering::Acquire));
            done.send(()).unwrap();
            WorkerExit {
                cleanup_verified: true,
            }
        })
    })
    .unwrap();
    receive(&completed);
    jobs.drain_until(Instant::now() + Duration::from_secs(20))
        .unwrap();
    jobs.mark_closed().unwrap();
}

#[test]
fn unknown_worker_panic_is_joined_but_never_reported_closed() {
    let jobs = jobs();
    let (entered, started) = mpsc::channel();
    jobs.spawn(move |_| {
        entered.send(()).unwrap();
        panic!("unverified worker cleanup");
    })
    .unwrap();
    receive(&started);
    assert!(jobs
        .drain_until(Instant::now() + Duration::from_secs(20))
        .is_err());
    let state = jobs.0.state.lock().unwrap();
    assert_eq!(state.phase, Phase::Stopping);
    assert!(state.jobs[0].joined && state.failure.is_some());
    drop(state);
    assert!(jobs.mark_closed().is_err());
}

#[test]
fn concurrent_finalizer_waits_after_owner_slot_is_empty_until_actual_drop_finishes() {
    let root = Root::new();
    let owner = Arc::new(Mutex::new(Some(
        RuntimeOwner::open(root.0.join("data")).unwrap(),
    )));
    let finalizer = Arc::new(Finalization::default());
    let jobs = jobs();
    jobs.request_shutdown();
    let (entered, dropping) = mpsc::channel();
    let (resume, wait) = mpsc::channel();
    let worker_finalizer = finalizer.clone();
    let worker_owner = owner.clone();
    let worker_jobs = jobs.clone();
    let first = thread::spawn(move || {
        worker_finalizer.run_until(Instant::now() + Duration::from_secs(20), || {
            let owner = worker_owner.lock().unwrap().take().unwrap();
            entered.send(()).unwrap();
            receive(&wait);
            drop(owner);
            worker_jobs.mark_closed()
        })
    });
    receive(&dropping);
    assert!(owner.lock().unwrap().is_none());
    assert!(finalizer
        .run_until(Instant::now(), || panic!("second physical destructor"))
        .is_err());
    assert_eq!(jobs.0.state.lock().unwrap().phase, Phase::Stopping);
    resume.send(()).unwrap();
    first.join().unwrap().unwrap();
    finalizer
        .run_until(Instant::now(), || panic!("closed finalization repeated"))
        .unwrap();
    assert_eq!(jobs.0.state.lock().unwrap().phase, Phase::Closed);
}

#[test]
fn owner_finalization_failure_is_sticky_and_cannot_become_closed() {
    let finalizer = Finalization::default();
    assert!(finalizer
        .run_until(Instant::now(), || panic!("destructor failure"))
        .is_err());
    assert!(finalizer.run_until(Instant::now(), || Ok(())).is_err());
}
