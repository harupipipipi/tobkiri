//! Native preparation admission and physical worker custody, not artifact rights.
use anyhow::{bail, ensure, Result};
use std::{
    panic::{catch_unwind, AssertUnwindSafe},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Condvar, Mutex, Weak,
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};
use tobkiri_core::NativeSetupInvocation;
use tobkiri_gate::{ControlKind, ControlReservation, CoreCancellation};

/// Closed internal conditions at this coordinator's own generating points.
#[derive(Debug)]
pub(crate) struct LifecycleError(pub(crate) &'static str);
impl std::fmt::Display for LifecycleError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(self.0)
    }
}
impl std::error::Error for LifecycleError {}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Phase {
    Accepting,
    Stopping,
    Closed,
}

pub(crate) struct WorkerExit {
    pub(crate) cleanup_verified: bool,
}

#[derive(Default)]
enum FinalPhase {
    #[default]
    Open,
    Dropping,
    Closed,
    Failed,
}

/// Exactly one physical owner destructor. Other stop callers observe that
/// completion, rather than treating an emptied owner slot as a close receipt.
#[derive(Default)]
pub(crate) struct Finalization {
    phase: Mutex<FinalPhase>,
    changed: Condvar,
}
impl Finalization {
    pub(crate) fn run_until(
        &self,
        deadline: Instant,
        finalize: impl FnOnce() -> Result<()>,
    ) -> Result<()> {
        let mut phase = self
            .phase
            .lock()
            .map_err(|_| anyhow::Error::new(LifecycleError("native finalization is poisoned")))?;
        loop {
            match *phase {
                FinalPhase::Closed => return Ok(()),
                FinalPhase::Failed => {
                    bail!(LifecycleError("native owner destruction was not verified"))
                }
                FinalPhase::Open => {
                    *phase = FinalPhase::Dropping;
                    break;
                }
                FinalPhase::Dropping => {
                    let remaining = deadline.saturating_duration_since(Instant::now());
                    ensure!(
                        !remaining.is_zero(),
                        LifecycleError("native owner destruction is still pending")
                    );
                    phase = self
                        .changed
                        .wait_timeout(phase, remaining)
                        .map_err(|_| {
                            anyhow::Error::new(LifecycleError("native finalization is poisoned"))
                        })?
                        .0;
                }
            }
        }
        drop(phase);
        let result = catch_unwind(AssertUnwindSafe(finalize)).unwrap_or_else(|_| {
            Err(anyhow::Error::new(LifecycleError(
                "native owner destructor panicked",
            )))
        });
        let mut phase = self
            .phase
            .lock()
            .map_err(|_| anyhow::Error::new(LifecycleError("native finalization is poisoned")))?;
        *phase = if result.is_ok() {
            FinalPhase::Closed
        } else {
            FinalPhase::Failed
        };
        self.changed.notify_all();
        result
    }
}

struct SourceStatus {
    live: AtomicBool,
    jobs: Weak<Shared>,
}

/// Dropped after the real input owner's AppHandle, including retained IO work.
pub(crate) struct SourceCustody(Arc<SourceStatus>);
impl Drop for SourceCustody {
    fn drop(&mut self) {
        if let Some(jobs) = self.0.jobs.upgrade() {
            let _guard = jobs.state.lock().unwrap_or_else(|error| error.into_inner());
            self.0.live.store(false, Ordering::Release);
            jobs.changed.notify_all();
        } else {
            self.0.live.store(false, Ordering::Release);
        }
    }
}

struct Job {
    id: u64,
    thread: Option<JoinHandle<WorkerExit>>,
    // Actual thread Join, separately from Source/IO custody and cleanup status.
    joined: bool,
    source: Arc<SourceStatus>,
    source_created: bool,
    invocation: Option<NativeSetupInvocation>,
    cancellation: Option<CoreCancellation>,
    _funding: Option<ControlReservation>,
}
struct State {
    phase: Phase,
    next_id: u64,
    jobs: Vec<Job>,
    // Unknown cleanup conservatively retains all records/permits and the
    // owner. Already joined records still say joined; this is not live work.
    failure: Option<String>,
}
struct Shared {
    state: Mutex<State>,
    changed: Condvar,
    shutdown: Arc<AtomicBool>,
}

#[derive(Clone)]
pub(crate) struct Jobs(Arc<Shared>);

/// A private registration, available only inside the actually retained worker.
pub(crate) struct JobTicket {
    jobs: Jobs,
    id: u64,
}

impl Jobs {
    pub(crate) fn new(shutdown: Arc<AtomicBool>) -> Self {
        Self(Arc::new(Shared {
            state: Mutex::new(State {
                phase: Phase::Accepting,
                next_id: 0,
                jobs: Vec::new(),
                failure: None,
            }),
            changed: Condvar::new(),
            shutdown,
        }))
    }

    pub(crate) fn ensure_accepting(&self) -> Result<()> {
        let state = self
            .0
            .state
            .lock()
            .map_err(|_| anyhow::Error::new(LifecycleError("native jobs are poisoned")))?;
        ensure!(
            state.phase == Phase::Accepting && !self.0.shutdown.load(Ordering::Acquire),
            LifecycleError("native preparation admission is closed")
        );
        Ok(())
    }

    pub(crate) fn spawn(
        &self,
        worker: impl FnOnce(JobTicket) -> WorkerExit + Send + 'static,
    ) -> Result<()> {
        let id = {
            let mut state = self
                .0
                .state
                .lock()
                .map_err(|_| anyhow::Error::new(LifecycleError("native jobs are poisoned")))?;
            ensure!(
                state.phase == Phase::Accepting && !self.0.shutdown.load(Ordering::Acquire),
                LifecycleError("native preparation admission is closed")
            );
            ensure!(
                state.jobs.iter().all(|job| job.invocation.is_some()
                    || job.thread.as_ref().is_some_and(JoinHandle::is_finished)),
                LifecycleError("native owner admission is already pending")
            );
            let id = state.next_id;
            state.next_id = id.checked_add(1).ok_or_else(|| {
                anyhow::Error::new(LifecycleError("native job identity exhausted"))
            })?;
            state.jobs.try_reserve(1)?;
            // The placeholder precedes owner opening/thread start. The actual
            // invocation funds the sustained record before Engine creation.
            state.jobs.push(Job {
                id,
                thread: None,
                joined: false,
                source: Arc::new(SourceStatus {
                    live: AtomicBool::new(false),
                    jobs: Arc::downgrade(&self.0),
                }),
                source_created: false,
                invocation: None,
                cancellation: None,
                _funding: None,
            });
            id
        };
        let (start, ready) = std::sync::mpsc::sync_channel(1);
        let jobs = self.clone();
        let thread = thread::Builder::new()
            .name("tobkiri-native-preparation".into())
            .spawn(move || {
                if ready.recv().is_err() {
                    return WorkerExit {
                        cleanup_verified: true,
                    };
                }
                // Join previous completions on this already tracked worker,
                // never on the GUI/caller thread. One unbound admission slot
                // also bounds this interval if an old destructor stalls.
                if jobs.reap().is_err() || jobs.ensure_accepting().is_err() {
                    return WorkerExit {
                        cleanup_verified: true,
                    };
                }
                let ticket = JobTicket {
                    jobs: jobs.clone(),
                    id,
                };
                let result = worker(ticket);
                if !result.cleanup_verified {
                    jobs.cleanup_failed();
                }
                jobs.0.changed.notify_all();
                result
            });
        match thread {
            Ok(thread) => {
                // A concurrent stop sees the placeholder until the real Join
                // is installed; the worker cannot open its owner beforehand.
                let mut state = self
                    .0
                    .state
                    .lock()
                    .unwrap_or_else(|error| error.into_inner());
                let job = state
                    .jobs
                    .iter_mut()
                    .find(|job| job.id == id)
                    .expect("registered native job");
                job.thread = Some(thread);
                self.0.changed.notify_all();
                drop(state);
                let _ = start.send(());
                Ok(())
            }
            Err(error) => {
                let mut state = self
                    .0
                    .state
                    .lock()
                    .unwrap_or_else(|error| error.into_inner());
                let position = state
                    .jobs
                    .iter()
                    .position(|job| job.id == id)
                    .expect("registered native job");
                let removed = state.jobs.swap_remove(position);
                self.0.changed.notify_all();
                drop(state);
                drop(removed);
                Err(error.into())
            }
        }
    }

    pub(crate) fn request_shutdown(&self) {
        let cancellation = {
            let mut state = self
                .0
                .state
                .lock()
                .unwrap_or_else(|error| error.into_inner());
            if state.phase == Phase::Accepting {
                state.phase = Phase::Stopping;
            }
            state
                .jobs
                .iter()
                .filter_map(|job| job.cancellation.clone())
                .collect::<Vec<_>>()
        };
        // Cancellation wakes Core/ledger work. Never notify under this mutex.
        for cancellation in cancellation {
            cancellation.cancel();
        }
        self.0.changed.notify_all();
    }

    pub(crate) fn cleanup_failed(&self) {
        {
            let mut state = self
                .0
                .state
                .lock()
                .unwrap_or_else(|error| error.into_inner());
            state
                .failure
                .get_or_insert_with(|| "native worker cleanup was not verified".into());
            if state.phase == Phase::Accepting {
                state.phase = Phase::Stopping;
            }
        }
        self.request_shutdown();
    }

    fn reap(&self) -> Result<()> {
        loop {
            let completed = {
                let mut state =
                    self.0.state.lock().map_err(|_| {
                        anyhow::Error::new(LifecycleError("native jobs are poisoned"))
                    })?;
                state.jobs.iter_mut().find_map(|job| {
                    if job.thread.as_ref().is_some_and(JoinHandle::is_finished) {
                        Some((job.id, job.thread.take().expect("finished native Join")))
                    } else {
                        None
                    }
                })
            };
            let Some((id, thread)) = completed else {
                break;
            };
            // Actual Join, including thread destruction, is outside every owner
            // and admission lock. Taking a result channel is never this proof.
            let clean = thread
                .join()
                .map(|exit| exit.cleanup_verified)
                .unwrap_or(false);
            let mut state = self
                .0
                .state
                .lock()
                .map_err(|_| anyhow::Error::new(LifecycleError("native jobs are poisoned")))?;
            state
                .jobs
                .iter_mut()
                .find(|job| job.id == id)
                .expect("retained native Join")
                .joined = true;
            drop(state);
            if !clean {
                self.cleanup_failed();
            }
        }
        let removed = {
            let mut state = self
                .0
                .state
                .lock()
                .map_err(|_| anyhow::Error::new(LifecycleError("native jobs are poisoned")))?;
            let mut removed = Vec::new();
            let mut index = 0;
            while index < state.jobs.len() {
                if state.jobs[index].joined
                    && !state.jobs[index].source.live.load(Ordering::Acquire)
                    && state.failure.is_none()
                {
                    removed.push(state.jobs.swap_remove(index));
                } else {
                    index += 1;
                }
            }
            removed
        };
        // Invocation/cancellation permits can touch their own ledgers on Drop.
        drop(removed);
        Ok(())
    }

    pub(crate) fn drain_until(&self, deadline: Instant) -> Result<()> {
        self.request_shutdown();
        loop {
            self.reap()?;
            let state = self
                .0
                .state
                .lock()
                .map_err(|_| anyhow::Error::new(LifecycleError("native jobs are poisoned")))?;
            if state.jobs.is_empty() {
                return Ok(());
            }
            if state
                .jobs
                .iter()
                .all(|job| job.joined && !job.source.live.load(Ordering::Acquire))
            {
                if state.failure.is_some() {
                    bail!(LifecycleError("native worker cleanup was not verified"));
                }
            }
            let remaining = deadline.saturating_duration_since(Instant::now());
            ensure!(
                !remaining.is_zero(),
                LifecycleError("native preparation drain timed out; owner and Joins retained")
            );
            drop(
                self.0
                    .changed
                    .wait_timeout(state, remaining.min(Duration::from_millis(10)))
                    .map_err(|_| {
                        anyhow::Error::new(LifecycleError("native job drain is poisoned"))
                    })?,
            );
        }
    }

    pub(crate) fn mark_closed(&self) -> Result<()> {
        let mut state = self
            .0
            .state
            .lock()
            .map_err(|_| anyhow::Error::new(LifecycleError("native jobs are poisoned")))?;
        ensure!(
            matches!(state.phase, Phase::Stopping | Phase::Closed)
                && state.jobs.is_empty()
                && state.failure.is_none(),
            LifecycleError("native runtime is not physically drained")
        );
        state.phase = Phase::Closed;
        Ok(())
    }
}

impl JobTicket {
    pub(crate) fn invocation(&self) -> Option<NativeSetupInvocation> {
        self.jobs
            .0
            .state
            .lock()
            .ok()?
            .jobs
            .iter()
            .find(|job| job.id == self.id)?
            .invocation
            .clone()
    }

    pub(crate) fn ensure_accepting(&self, jobs: &Jobs) -> Result<()> {
        ensure!(
            Arc::ptr_eq(&self.jobs.0, &jobs.0),
            LifecycleError("foreign native job owner")
        );
        self.jobs.ensure_accepting()
    }

    pub(crate) fn attach_invocation(
        &self,
        invocation: &NativeSetupInvocation,
    ) -> Result<CoreCancellation> {
        let funding = invocation
            .control()
            .reserve(ControlKind::Task, std::mem::size_of::<Job>() as u64)?;
        let cancellation = CoreCancellation::new_with_control(invocation.control())?;
        let mut state = self
            .jobs
            .0
            .state
            .lock()
            .map_err(|_| anyhow::Error::new(LifecycleError("native jobs are poisoned")))?;
        ensure!(
            state.phase == Phase::Accepting && !self.jobs.0.shutdown.load(Ordering::Acquire),
            LifecycleError("native preparation stopped before Engine creation")
        );
        let job = state
            .jobs
            .iter_mut()
            .find(|job| job.id == self.id)
            .ok_or_else(|| anyhow::Error::new(LifecycleError("native registration retired")))?;
        ensure!(
            job.invocation.is_none(),
            LifecycleError("native job invocation was already attached")
        );
        job.invocation = Some(invocation.clone());
        job.cancellation = Some(cancellation.clone());
        job._funding = Some(funding);
        Ok(cancellation)
    }

    pub(crate) fn source_custody(&self) -> Result<SourceCustody> {
        let mut state = self
            .jobs
            .0
            .state
            .lock()
            .map_err(|_| anyhow::Error::new(LifecycleError("native jobs are poisoned")))?;
        let job = state
            .jobs
            .iter_mut()
            .find(|job| job.id == self.id)
            .ok_or_else(|| anyhow::Error::new(LifecycleError("native registration retired")))?;
        ensure!(
            job.invocation.is_some() && !job.source_created,
            LifecycleError("native source custody was already created or is unfunded")
        );
        job.source_created = true;
        job.source.live.store(true, Ordering::Release);
        Ok(SourceCustody(job.source.clone()))
    }
}

#[cfg(all(test, target_os = "macos"))]
#[path = "native_runtime_jobs_tests.rs"]
mod tests;
