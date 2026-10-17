use crate::engine::{RunCompletionReceipt, completion_result, is_no_replay};
use crate::{
    CoreStream, RunEvent, RunEventKind, RunHandle, StepError, StreamSource, StreamTerminal,
};
use async_trait::async_trait;
use std::sync::Arc;
use tobkiri_canon::{AllocationBudget, AllocationError, CoreBuffer, CoreRef, CoreValue};
use tobkiri_gate::CancellationToken;
use tokio::sync::{mpsc, oneshot};

impl RunHandle {
    /// Convert one real engine run into a funded CoreValue event-frame stream.
    pub fn into_stream(self) -> CoreStream {
        CoreStream::new(RunStream {
            events: self.events,
            result: Some(self.result.into_receiver()),
            completion: None,
            cancel: self.cancel,
            stream_budget: self.stream_budget,
            event_budget: self.event_budget,
            max_batch_bytes: self.max_batch_bytes,
            terminal_seen: false,
            terminal_outcome: None,
            terminal_failure: None,
            terminal_reported: false,
            batch_has_terminal: false,
            ended: false,
            pending_batch: None,
            pending_event: None,
            empty_value: None,
            empty_buffer: None,
        })
    }
}
struct RunStream {
    events: mpsc::Receiver<RunEvent>,
    result: Option<oneshot::Receiver<RunCompletionReceipt>>,
    completion: Option<StreamCompletion>,
    cancel: CancellationToken,
    stream_budget: Arc<dyn AllocationBudget>,
    event_budget: Arc<dyn AllocationBudget>,
    max_batch_bytes: u64,
    terminal_seen: bool,
    terminal_outcome: Option<StreamTerminal>,
    terminal_failure: Option<StepError>,
    terminal_reported: bool,
    batch_has_terminal: bool,
    ended: bool,
    pending_batch: Option<CoreValue>,
    pending_event: Option<RunEvent>,
    empty_value: Option<CoreValue>,
    empty_buffer: Option<CoreBuffer>,
}
struct StreamCompletion {
    outcome: Result<(), StepError>,
    cleanup: Result<(), StepError>,
}
fn allocation(error: AllocationError) -> StepError {
    StepError::new(
        match error {
            AllocationError::Limit | AllocationError::Overflow | AllocationError::Depth => {
                "limit-exceeded"
            }
            AllocationError::Invalid => "invalid",
            _ => "unavailable",
        },
        "run event batch allocation failed",
    )
}
#[async_trait]
impl StreamSource for RunStream {
    async fn pull(&mut self, max: u32) -> Result<CoreValue, StepError> {
        let batch = self.next_batch(max).await?;
        self.report_terminal_batch();
        Ok(batch)
    }

    async fn pull_final(&mut self, max: u32) -> Result<CoreBuffer, StepError> {
        if max == 0 {
            return Err(StepError::new(
                "invalid",
                "stream pull size must be positive",
            ));
        }
        let batch = self.next_batch(max).await?;
        let is_end = matches!(batch.inspect(), CoreRef::Arr(values) if values.is_empty());
        if is_end {
            if let Some(buffer) = &self.empty_buffer {
                return Ok(buffer.share());
            }
        }
        let buffer = match batch.encode_guest(&self.stream_budget) {
            Ok(buffer) => buffer,
            Err(AllocationError::Limit | AllocationError::Overflow | AllocationError::Depth) => {
                self.pending_batch = Some(batch);
                return Err(StepError::new(
                    "batch-too-large",
                    "run event batch is waiting for bounded transport capacity",
                ));
            }
            Err(error) => return Err(allocation(error)),
        };
        if buffer.len() as u64 > self.max_batch_bytes {
            self.pending_batch = Some(batch);
            return Err(StepError::new(
                "batch-too-large",
                "run event batch exceeds the bounded transport size",
            ));
        }
        if is_end {
            self.empty_buffer = Some(buffer.share());
        }
        self.report_terminal_batch();
        Ok(buffer)
    }

    fn cancel(&mut self) {
        if !self.ended && !self.terminal_seen {
            self.cancel.cancel();
        }
        self.events.close();
        self.ended = true;
        self.pending_batch = None;
        self.pending_event = None;
    }

    fn terminal_outcome(&self) -> Option<StreamTerminal> {
        self.terminal_outcome
    }

    async fn close(&mut self) -> Result<(), StepError> {
        self.cancel();
        self.receive_completion().await;
        let completion = self.completion.as_ref().expect("completion was recorded");
        // A receipt is sent only after interpreter custody drops. Its separate
        // sticky cleanup result must also be successful; a delivered Failed
        // frame or a cancelled outcome alone is never physical-cleanup proof.
        match completion_result(completion.outcome.clone(), completion.cleanup.clone()) {
            Err(error) if is_no_replay(&error) => Err(error),
            Err(error) if completion.cleanup.is_err() => Err(error),
            Err(error) if error.code == "cancelled" => Ok(()),
            Err(error)
                if self.terminal_reported && self.terminal_failure.as_ref() == Some(&error) =>
            {
                Ok(())
            }
            result => result,
        }
    }
}

impl RunStream {
    async fn receive_completion(&mut self) {
        if self.completion.is_some() {
            return;
        }
        // Borrow the receiver until it resolves. An abandoned close/pull
        // future must leave this exact completion proof available for retry.
        let receipt = match self.result.as_mut() {
            Some(result) => result
                .await
                .map_err(|_| StepError::new("internal", "Flow completion channel closed")),
            None => Err(StepError::new("internal", "Flow completion proof missing")),
        };
        self.result.take();
        self.completion = Some(match receipt {
            Ok(receipt) => StreamCompletion {
                outcome: receipt.outcome.map(|_| ()),
                cleanup: receipt.cleanup,
            },
            Err(error) => StreamCompletion {
                outcome: Err(error.clone()),
                cleanup: Err(error),
            },
        });
    }

    async fn await_completion(&mut self) -> Result<(), StepError> {
        self.receive_completion().await;
        let completion = self.completion.as_ref().expect("completion was recorded");
        completion_result(completion.outcome.clone(), completion.cleanup.clone())
    }

    fn report_terminal_batch(&mut self) {
        if self.batch_has_terminal {
            self.terminal_reported = true;
        }
    }

    async fn confirmed_eof(&mut self) -> Result<(), StepError> {
        self.await_completion().await?;
        if !self.terminal_reported {
            return Err(StepError::new(
                "internal",
                "Flow completion did not deliver a terminal frame",
            ));
        }
        Ok(())
    }

    async fn next_batch(&mut self, max: u32) -> Result<CoreValue, StepError> {
        if let Some(batch) = self.pending_batch.take() {
            return Ok(batch);
        }
        self.batch_has_terminal = false;
        if self.ended {
            self.confirmed_eof().await?;
            if let Some(value) = &self.empty_value {
                return Ok(value.share());
            }
            let empty = CoreValue::array_builder(&self.stream_budget, 0)
                .map_err(allocation)?
                .finish()
                .map_err(allocation)?;
            self.empty_value = Some(empty.share());
            return Ok(empty);
        }

        let bound = max.min(32) as usize;
        let mut batch = CoreValue::array_builder(&self.stream_budget, bound).map_err(allocation)?;
        while batch.len() < bound {
            let event = match self.pending_event.take() {
                Some(event) => Some(event),
                None if batch.is_empty() => self.events.recv().await,
                None => self.events.try_recv().ok(),
            };
            let Some(event) = event else {
                if self.events.is_closed() && self.events.is_empty() {
                    self.ended = true;
                    self.confirmed_eof().await?;
                }
                break;
            };

            let value = match event.to_value(&self.event_budget) {
                Ok(value) => value,
                Err(error) => {
                    self.pending_event = Some(event);
                    if !batch.is_empty() {
                        self.pending_batch = Some(batch.finish().map_err(allocation)?);
                    }
                    return Err(if error.code == "limit-exceeded" {
                        StepError::new(
                            "batch-too-large",
                            "run event cannot fit in the bounded transport batch",
                        )
                    } else {
                        error
                    });
                }
            };
            if let Err(error) = batch.push(value) {
                self.pending_event = Some(event);
                if !batch.is_empty() {
                    self.pending_batch = Some(batch.finish().map_err(allocation)?);
                }
                return Err(
                    if matches!(
                        error,
                        AllocationError::Limit | AllocationError::Overflow | AllocationError::Depth
                    ) {
                        StepError::new(
                            "batch-too-large",
                            "run event cannot fit in the bounded transport batch",
                        )
                    } else {
                        allocation(error)
                    },
                );
            }
            // Mark only an accepted event, and report it only when this batch
            // actually leaves pull/pull_final. An encoding failure keeps the
            // terminal pending and cannot turn close into successful cleanup.
            if matches!(
                event.kind,
                RunEventKind::Completed { .. }
                    | RunEventKind::Failed { .. }
                    | RunEventKind::Cancelled { .. }
            ) {
                self.terminal_seen = true;
                self.batch_has_terminal = true;
                self.terminal_outcome = Some(match event.kind {
                    RunEventKind::Completed { .. } => StreamTerminal::Succeeded,
                    RunEventKind::Cancelled { .. } => StreamTerminal::Cancelled,
                    RunEventKind::Failed { error } => {
                        let code = error.audit_code();
                        // Transfer the existing event diagnostic; do not clone
                        // guest text into another per-stream native allocation.
                        self.terminal_failure = Some(error);
                        StreamTerminal::Failed(code)
                    }
                    _ => unreachable!("terminal event kind was matched above"),
                });
            }
            if self.terminal_seen {
                break;
            }
        }
        batch.finish().map_err(allocation)
    }
}

#[cfg(test)]
#[path = "run_stream_tests.rs"]
mod tests;
