//! Supervise real filesystem work independently of the awaiting component.
//!
//! Dropping a guest future closes the existing delivery channel. It does not
//! cancel the OS worker, release its input/descriptor custody early, or replay
//! a mutation. The supervisor waits for physical completion before terminal
//! delivery. There is no asynchronous Drop action.

use super::{FsCompletion, FsDisposition, FsError, FsOutput, FsWork};
use anyhow::{Context, Result};
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, Ordering},
};
use tobkiri_gate::{CallCx, ControlKind, ControlReservation, GateError, Outcome};
use tokio::sync::oneshot;

struct State {
    completion: Mutex<Option<FsCompletion>>,
    changed: AtomicBool,
    // Every worker/receiver/supervisor retaining this state keeps its control
    // allocation alive, including after the original guest future is dropped.
    _control: ControlReservation,
}

pub(crate) struct FsDelivery {
    state: Arc<State>,
    cx: CallCx,
    converted: Option<oneshot::Sender<bool>>,
    terminal: oneshot::Receiver<std::result::Result<(), GateError>>,
}

impl FsWork {
    pub(crate) async fn dispatch(self) -> Result<FsDelivery> {
        let gate = self.gate.clone();
        let cx = self.context().clone();
        let grant_id = self.permit().id().clone();
        let control = match cx.control().reserve(ControlKind::Task, 64 * 1024) {
            Ok(control) => control,
            Err(error) => {
                gate.record_outcome(
                    grant_id,
                    Outcome::Failed {
                        code: "filesystem-supervision-limit".into(),
                    },
                )
                .await?;
                return Err(error.into());
            }
        };
        // No await separates this sticky evidence from starting real work.
        // All enclosing contexts inherit it, including a parent export whose
        // cancellation wins before this import can return its sealed error.
        if self.is_mutation() {
            cx.note_filesystem_mutation();
        }
        let state = Arc::new(State {
            completion: Mutex::new(None),
            changed: AtomicBool::new(false),
            _control: control,
        });
        let worker_state = state.clone();
        let supervisor_state = state.clone();
        let supervisor_cx = cx.clone();
        let (ready_tx, ready_rx) = oneshot::channel();
        let (converted_tx, converted_rx) = oneshot::channel();
        let (terminal_tx, terminal_rx) = oneshot::channel();

        // There are no awaits between starting real work and installing its
        // supervisor. Both closures retain the pre-admitted state allocation.
        let worker = tokio::task::spawn_blocking(move || {
            let completion = self.execute();
            (completion, worker_state)
        });
        tokio::spawn(async move {
            let (completion, _worker_state) = match worker.await {
                Ok(value) => value,
                Err(_) => {
                    // A panicked worker cannot prove absence of a partial
                    // effect. Its consumed grant never becomes reusable.
                    supervisor_state.changed.store(true, Ordering::Release);
                    let result = gate
                        .record_outcome(
                            grant_id,
                            Outcome::Failed {
                                code: "filesystem-commit-unknown".into(),
                            },
                        )
                        .await;
                    let _ = terminal_tx.send(result);
                    return;
                }
            };
            supervisor_state.changed.store(
                completion.disposition() != FsDisposition::Clean,
                Ordering::Release,
            );
            match supervisor_state.completion.lock() {
                Ok(mut slot) => *slot = Some(completion),
                Err(poisoned) => {
                    // Retain the actual completion even if conversion panicked.
                    *poisoned.into_inner() = Some(completion);
                }
            }
            let waiting = ready_tx.send(()).is_ok();
            let converted = if waiting {
                tokio::select! {
                    value = converted_rx => value.unwrap_or(false),
                    _ = supervisor_cx.cancel().cancelled() => false,
                    _ = tokio::time::sleep_until(supervisor_cx.deadline().into()) => false,
                }
            } else {
                false
            };
            let completion = match supervisor_state.completion.lock() {
                Ok(mut slot) => slot.take(),
                Err(poisoned) => poisoned.into_inner().take(),
            };
            let result = match completion {
                Some(completion) => {
                    // Keep the entire real receipt/permit alive across durable
                    // terminal delivery, not merely its ID or a success flag.
                    if std::time::Instant::now() >= completion.permit().deadline() {
                        gate.settle_deadline(completion.permit()).await.map(|_| ())
                    } else {
                        let outcome = if converted {
                            Outcome::Succeeded
                        } else if completion.disposition() != FsDisposition::Clean {
                            Outcome::Failed {
                                code: "filesystem-commit-unknown".into(),
                            }
                        } else if supervisor_cx.cancel().is_cancelled() {
                            Outcome::Cancelled
                        } else {
                            Outcome::Failed {
                                code: "filesystem-effect".into(),
                            }
                        };
                        gate.record_outcome(grant_id, outcome).await
                    }
                }
                None => Err(GateError::Unavailable),
            };
            let _ = terminal_tx.send(result);
        });

        ready_rx.await.map_err(|_| FsError::CommitUnknown)?;
        Ok(FsDelivery {
            state,
            cx,
            converted: Some(converted_tx),
            terminal: terminal_rx,
        })
    }
}

impl FsDelivery {
    pub(crate) fn convert<T>(&self, encode: impl FnOnce(FsOutput) -> Result<T>) -> Result<T> {
        let result = (|| {
            let mut slot = self
                .state
                .completion
                .lock()
                .map_err(|_| anyhow::Error::new(FsError::Unavailable))?;
            let completion = slot
                .as_mut()
                .context("filesystem delivery has already settled")?;
            completion
                .take_result()
                .map_err(anyhow::Error::new)
                .and_then(encode)
        })();
        self.preserve_uncertainty(result)
    }

    fn preserve_uncertainty<T>(&self, result: Result<T>) -> Result<T> {
        if result.is_err() && self.state.changed.load(Ordering::Acquire) {
            Err(FsError::CommitUnknown.into())
        } else {
            result
        }
    }

    pub(crate) async fn finish<T>(mut self, result: Result<T>) -> Result<T> {
        let result = self.preserve_uncertainty(result.and_then(|value| {
            self.cx.check_active()?;
            Ok(value)
        }));
        let sender = self
            .converted
            .take()
            .context("filesystem conversion already acknowledged")?;
        // A closed receiver means the supervisor has already won cancellation
        // or deadline settlement. Await its immutable result; never retry it.
        let _ = sender.send(result.is_ok());
        let terminal = (&mut self.terminal)
            .await
            .context("filesystem terminal delivery was interrupted")
            .and_then(|result| result.map_err(anyhow::Error::new));
        self.preserve_uncertainty(terminal)?;
        self.preserve_uncertainty(result.and_then(|value| {
            self.cx.check_active()?;
            Ok(value)
        }))
    }
}
