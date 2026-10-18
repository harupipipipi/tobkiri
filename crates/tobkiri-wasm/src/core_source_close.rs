//! One retained close future for one actual Core-owned stream.
//!
//! A joined task is separate from the handle's immutable terminal proposal.
//! Neither a late join nor source absence authorizes an effect replay.
use crate::handles::HandleCleanupError;
use anyhow::{Context, Result};
use futures_util::{
    FutureExt,
    future::{BoxFuture, Shared},
};
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, Ordering},
};
use tobkiri_flow::{CoreStream, StepError};
use tobkiri_gate::{ControlKind, ControlReservation, CoreControl};
use tokio::{
    sync::{MappedMutexGuard, Mutex as AsyncMutex, MutexGuard},
    time::Instant,
};

#[derive(Clone, Copy, Debug)]
pub(crate) struct PhysicalCloseReceipt {
    // A receipt exists only after close returned, the actual source dropped,
    // and the same spawned task was joined. Err remains a cleanup failure.
    pub(crate) result: Result<(), HandleCleanupError>,
}
type Completion = Shared<BoxFuture<'static, Result<PhysicalCloseReceipt, ()>>>;

enum State {
    Open,
    Running {
        deadline: Instant,
        completion: Completion,
    },
    Joined(PhysicalCloseReceipt),
    Unknown,
}

enum JoinTarget {
    Ready(PhysicalCloseReceipt),
    Running {
        deadline: Instant,
        completion: Completion,
    },
}

struct SourceHolder {
    source: AsyncMutex<Option<CoreStream>>,
    // The task retains this exact allocation even if every observer disappears.
    _registration: ControlReservation,
}

pub(crate) struct CoreSourceClose {
    holder: Arc<SourceHolder>,
    state: Mutex<State>,
    started: AtomicBool,
    interrupted: AtomicBool,
    control: CoreControl,
    runtime: tokio::runtime::Handle,
    #[cfg(test)]
    fail_spawn: AtomicBool,
    #[cfg(test)]
    abort: Mutex<Option<tokio::task::AbortHandle>>,
}

// Dropping an observer records uncertainty; it never drops the task's future.
struct Observer<'a> {
    interrupted: &'a AtomicBool,
    finished: bool,
}
impl Drop for Observer<'_> {
    fn drop(&mut self) {
        if !self.finished {
            self.interrupted.store(true, Ordering::Release);
        }
    }
}

impl CoreSourceClose {
    pub(crate) fn new(control: &CoreControl, source: CoreStream) -> Result<Arc<Self>> {
        let registration = control.reserve(ControlKind::Registration, 2048)?;
        let runtime = tokio::runtime::Handle::try_current()
            .context("Core stream cleanup runtime unavailable")?;
        Ok(Arc::new(Self {
            holder: Arc::new(SourceHolder {
                source: AsyncMutex::new(Some(source)),
                _registration: registration,
            }),
            state: Mutex::new(State::Open),
            started: AtomicBool::new(false),
            interrupted: AtomicBool::new(false),
            control: control.clone(),
            runtime,
            #[cfg(test)]
            fail_spawn: AtomicBool::new(false),
            #[cfg(test)]
            abort: Mutex::new(None),
        }))
    }

    /// Recheck after taking the source mutex: a previously resolved alias may
    /// have raced the beginning of close. A missing holder is never fresh input.
    pub(crate) async fn lock_open(&self) -> Result<MappedMutexGuard<'_, CoreStream>, StepError> {
        let source = self.holder.source.lock().await;
        if self.started.load(Ordering::Acquire) {
            return Err(StepError::new("unavailable", "Core stream is closing"));
        }
        MutexGuard::try_map(source, |source| source.as_mut())
            .map_err(|_| StepError::new("internal", "Core stream source is unavailable"))
    }

    pub(crate) fn was_interrupted(&self) -> bool {
        self.interrupted.load(Ordering::Acquire)
    }

    fn start_or_join(&self) -> Result<JoinTarget, HandleCleanupError> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| HandleCleanupError::CompletionUnknown)?;
        match &*state {
            State::Running {
                deadline,
                completion,
            } => {
                return Ok(JoinTarget::Running {
                    deadline: *deadline,
                    completion: completion.clone(),
                });
            }
            State::Joined(receipt) => return Ok(JoinTarget::Ready(*receipt)),
            State::Unknown => return Err(HandleCleanupError::CompletionUnknown),
            State::Open => (),
        }
        self.started.store(true, Ordering::Release);
        // Every admission/spawn failure is sticky and cannot start a new close.
        *state = State::Unknown;
        let task_control = self
            .control
            .reserve(ControlKind::Task, 4096)
            .map_err(|_| HandleCleanupError::CompletionUnknown)?;
        let holder = self.holder.clone();
        let deadline = Instant::now() + std::time::Duration::from_secs(2);
        let task = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            #[cfg(test)]
            if self.fail_spawn.load(Ordering::Acquire) {
                panic!("test-only Core close spawn failure");
            }
            // This task does not capture CoreSourceClose, its state, or its own
            // shared JoinHandle. Only the actual source and prepaid task live here.
            self.runtime.spawn(async move {
                let _task_control = task_control;
                let mut source = {
                    let mut slot = holder.source.lock().await;
                    slot.take().ok_or(())?
                };
                let result = std::panic::AssertUnwindSafe(source.close())
                    .catch_unwind()
                    .await
                    .unwrap_or_else(|_| {
                        Err(StepError::new("internal", "Core source close trapped"))
                    });
                let result = result.map_err(classify_source_error);
                drop(source);
                drop(holder);
                Ok(PhysicalCloseReceipt { result })
            })
        }))
        .map_err(|_| HandleCleanupError::CompletionUnknown)?;
        #[cfg(test)]
        if let Ok(mut abort) = self.abort.lock() {
            *abort = Some(task.abort_handle());
        }
        let completion = async move { task.await.unwrap_or(Err(())) }
            .boxed()
            .shared();
        *state = State::Running {
            deadline,
            completion: completion.clone(),
        };
        Ok(JoinTarget::Running {
            deadline,
            completion,
        })
    }

    fn record_join(
        &self,
        result: Result<PhysicalCloseReceipt, ()>,
    ) -> Result<PhysicalCloseReceipt, HandleCleanupError> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| HandleCleanupError::CompletionUnknown)?;
        match result {
            Ok(receipt) => {
                *state = State::Joined(receipt);
                Ok(receipt)
            }
            Err(()) => {
                *state = State::Unknown;
                Err(HandleCleanupError::CompletionUnknown)
            }
        }
    }

    /// Nonwaiting observation of the retained actual join, never another close.
    pub(crate) fn physical_receipt(&self) -> Option<PhysicalCloseReceipt> {
        let completion = {
            let state = self.state.lock().ok()?;
            match &*state {
                State::Joined(receipt) => return Some(*receipt),
                State::Running { completion, .. } => completion.clone(),
                State::Open | State::Unknown => return None,
            }
        };
        completion
            .now_or_never()
            .and_then(|result| self.record_join(result).ok())
    }

    /// The observer has the original grace bound. The actual close future does
    /// not: it remains owned by its one prepaid task until completion or runtime
    /// loss. A late physical join cannot clear an earlier observer uncertainty.
    pub(crate) async fn close(&self) -> Result<(), HandleCleanupError> {
        if let Some(receipt) = self.physical_receipt() {
            return self.result_for_handle(receipt);
        }
        let mut observer = Observer {
            interrupted: &self.interrupted,
            finished: false,
        };
        let (deadline, completion) = match self.start_or_join()? {
            JoinTarget::Ready(receipt) => {
                observer.finished = true;
                return self.result_for_handle(receipt);
            }
            JoinTarget::Running {
                deadline,
                completion,
            } => (deadline, completion),
        };
        let _waiter = self
            .control
            .reserve(ControlKind::Waiter, 256)
            .map_err(|_| HandleCleanupError::CompletionUnknown)?;
        // Poll an already completed join even after the original deadline. This
        // observes a fact; it never grants a new wait or changes the old outcome.
        let result = match completion.clone().now_or_never() {
            Some(result) => result,
            None => tokio::time::timeout_at(deadline, completion)
                .await
                .map_err(|_| HandleCleanupError::CompletionUnknown)?,
        };
        let receipt = self.record_join(result)?;
        observer.finished = true;
        self.result_for_handle(receipt)
    }

    fn result_for_handle(&self, receipt: PhysicalCloseReceipt) -> Result<(), HandleCleanupError> {
        match receipt.result {
            // A real observed close failure, including all three facaded
            // no-replay kinds, always outranks an observer timeout.
            Err(error) => Err(error),
            Ok(()) if self.was_interrupted() => Err(HandleCleanupError::CompletionUnknown),
            Ok(()) => Ok(()),
        }
    }
}

fn classify_source_error(error: StepError) -> HandleCleanupError {
    // These are already CoreStream facades, not newly minted native evidence.
    match error.code.as_str() {
        "filesystem-commit-unknown" => HandleCleanupError::FacadedFilesystemCommitUnknown,
        "storage-sql-commit-unknown" => HandleCleanupError::FacadedStorageCommitUnknown,
        "storage-pending-data-move" => HandleCleanupError::FacadedStoragePendingDataMove,
        _ => HandleCleanupError::CompletionUnknown,
    }
}

#[cfg(test)]
mod tests;
