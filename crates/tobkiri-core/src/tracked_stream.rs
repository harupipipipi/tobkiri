use crate::session::ConsumedEntryTerminal;
use async_trait::async_trait;
use std::sync::Arc;
use tobkiri_canon::{CoreBuffer, CoreValue};
use tobkiri_flow::{CoreStream, RunEvent, RunEventKind, StepError, StreamSource, StreamTerminal};
use tobkiri_gate::{CallCx, Gate, GateEngine, GrantId, Outcome};
use tokio::{runtime::Handle, task::JoinHandle};

/// Retains the FlowRun grant until cleanup is completed on its owning runtime.
/// Callers can observe cleanup/audit errors through close or a post-cancel pull.
pub(crate) struct TrackedStream {
    source: Option<CoreStream>,
    gate: Arc<GateEngine>,
    cx: CallCx,
    grant: Option<GrantId>,
    entry_terminal: Option<ConsumedEntryTerminal>,
    terminal: Option<Outcome>,
    runtime: Handle,
    cleanup: Option<JoinHandle<CleanupReceipt>>,
    receipt: Option<CleanupReceipt>,
    cleanup_error: Option<StepError>,
    end_batch: Option<CoreValue>,
    end_buffer: Option<CoreBuffer>,
    closed: bool,
}
/// The terminal proposal is chosen after the single physical cleanup attempt.
/// A delivery retry never chooses a different outcome or runs guest code again.
struct CleanupReceipt {
    outcome: Outcome,
    physical_error: Option<StepError>,
    delivery_error: Option<StepError>,
}
impl CleanupReceipt {
    fn result(&self) -> Result<(), StepError> {
        match self
            .physical_error
            .as_ref()
            .or(self.delivery_error.as_ref())
        {
            Some(error) => Err(error.clone()),
            None => Ok(()),
        }
    }
}
impl TrackedStream {
    #[cfg(test)]
    pub fn new(
        source: CoreStream,
        gate: Arc<GateEngine>,
        cx: CallCx,
        grant: GrantId,
        runtime: Handle,
    ) -> Self {
        Self {
            source: Some(source),
            gate,
            cx,
            grant: Some(grant),
            entry_terminal: None,
            terminal: None,
            runtime,
            cleanup: None,
            receipt: None,
            cleanup_error: None,
            end_batch: None,
            end_buffer: None,
            closed: false,
        }
    }
    /// Wrap a nested canonical Flow whose exact consumed permit remains in
    /// Core-local terminal custody. The terminal task retains that custody
    /// until the immutable outcome is durably accepted.
    pub fn new_with_terminal(
        source: CoreStream,
        gate: Arc<GateEngine>,
        cx: CallCx,
        terminal: ConsumedEntryTerminal,
        runtime: Handle,
    ) -> Self {
        Self {
            source: Some(source),
            gate,
            cx,
            grant: None,
            entry_terminal: Some(terminal),
            terminal: None,
            runtime,
            cleanup: None,
            receipt: None,
            cleanup_error: None,
            end_batch: None,
            end_buffer: None,
            closed: false,
        }
    }
    fn begin_close(&mut self, outcome: Outcome) {
        if self.closed {
            return;
        }
        self.closed = true;
        let mut source = self.source.take().expect("live tracked source");
        // Stop the run immediately; asynchronous guest/audit cleanup follows on
        // the captured runtime even when this call comes from a native thread.
        let cancelled = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| source.cancel()))
            .map_err(|_| StepError::new("trap", "Flow stream cancellation trapped"));
        let gate = self.gate.clone();
        let grant = self.grant.clone();
        let mut entry_terminal = self.entry_terminal.take();
        let task = async move {
            let closed = source.close().await;
            let cleanup = cancelled.and(closed);
            let outcome = match &cleanup {
                Ok(()) => outcome,
                Err(error) => Outcome::Failed {
                    code: error.audit_code().into(),
                },
            };
            let delivery_error = if let Some(terminal) = entry_terminal.as_mut() {
                // Retain this sole terminal owner in the cleanup task until
                // the same immutable proposal is durably accepted. Backoff
                // keeps transient persistence failures from spinning while
                // the durable deadline supervisor remains active.
                loop {
                    match terminal.finish(outcome.clone()).await {
                        Ok(()) => break None,
                        Err(_) => tokio::time::sleep(std::time::Duration::from_millis(50)).await,
                    }
                }
            } else if let Some(grant) = grant {
                gate.record_outcome(grant, outcome.clone())
                    .await
                    .err()
                    .map(|e| StepError::new(e.code(), "Flow outcome could not be recorded"))
            } else {
                None
            };
            CleanupReceipt {
                outcome,
                physical_error: cleanup.err(),
                delivery_error,
            }
        };
        match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| self.runtime.spawn(task))) {
            Ok(task) => self.cleanup = Some(task),
            Err(_) => {
                self.cleanup_error = Some(StepError::new(
                    "internal",
                    "owning runtime could not schedule Flow cleanup",
                ));
            }
        }
    }
    async fn await_cleanup(&mut self) -> Result<(), StepError> {
        // Retain the task across an abandoned close future. Joining a cancelled
        // owning runtime yields an error; it does not certify physical cleanup.
        if let Some(cleanup) = self.cleanup.as_mut() {
            let result = cleanup.await.map_err(|_| {
                StepError::new("internal", "owning runtime did not complete Flow cleanup")
            });
            self.cleanup.take();
            match result {
                Ok(receipt) => {
                    let result = receipt.result();
                    if receipt.delivery_error.is_none() {
                        self.grant.take();
                    }
                    self.receipt = Some(receipt);
                    // Report the first delivery failure to this caller; a later
                    // explicit close/pull may resend that immutable proposal.
                    return result;
                }
                Err(error) => self.cleanup_error = Some(error),
            }
        }
        if let Some(error) = &self.cleanup_error {
            return Err(error.clone());
        }
        if self
            .receipt
            .as_ref()
            .is_some_and(|r| r.delivery_error.is_some())
        {
            let gate = self.gate.clone();
            let grant = self.grant.clone();
            let mut receipt = self.receipt.take().expect("known cleanup receipt");
            // Only audit metadata is retried. Use the captured runtime, retaining
            // the task so cancelling this observing future cannot lose its result.
            let task = async move {
                receipt.delivery_error = if let Some(grant) = grant {
                    gate.record_outcome(grant, receipt.outcome.clone())
                        .await
                        .err()
                        .map(|e| StepError::new(e.code(), "Flow outcome could not be recorded"))
                } else {
                    None
                };
                receipt
            };
            match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                self.runtime.spawn(task)
            })) {
                Ok(task) => self.cleanup = Some(task),
                Err(_) => {
                    let error =
                        StepError::new("internal", "owning runtime could not retry Flow audit");
                    self.cleanup_error = Some(error.clone());
                    return Err(error);
                }
            }
            // Join the newly retained metadata-only task without recursion.
            let result = self.cleanup.as_mut().expect("audit retry task").await;
            self.cleanup.take();
            match result {
                Ok(receipt) => {
                    let result = receipt.result();
                    if receipt.delivery_error.is_none() {
                        self.grant.take();
                    }
                    self.receipt = Some(receipt);
                    return result;
                }
                Err(_) => {
                    let error =
                        StepError::new("internal", "owning runtime did not complete Flow audit");
                    self.cleanup_error = Some(error.clone());
                    return Err(error);
                }
            }
        }
        self.receipt.as_ref().map_or(Ok(()), CleanupReceipt::result)
    }
    async fn finish(&mut self, outcome: Outcome) -> Result<(), StepError> {
        self.begin_close(outcome);
        self.await_cleanup().await
    }
}
#[async_trait]
impl StreamSource for TrackedStream {
    async fn pull(&mut self, max: u32) -> Result<CoreValue, StepError> {
        if self.closed {
            self.await_cleanup().await?;
            return self.end_batch.clone().ok_or_else(|| {
                StepError::new("unavailable", "closed Flow stream has no owned empty batch")
            });
        }
        if self.cx.check_active().is_err()
            || self
                .gate
                .epoch(self.cx.profile())
                .map_err(|e| StepError::new(e.code(), "epoch unavailable"))?
                != self.cx.security_epoch()
        {
            self.finish(Outcome::Cancelled).await?;
            return Err(StepError::new("cancelled", "Flow stream authority expired"));
        }
        let source = self.source.as_mut().expect("live tracked source");
        let result = tokio::select! {
            _=self.cx.cancel().cancelled()=>Err(StepError::new("cancelled","Flow stream cancelled")),
            _=tokio::time::sleep_until(self.cx.deadline().into())=>Err(StepError::new("cancelled","original Flow deadline expired")),
            result=source.pull(max)=>result,
        };
        match &result {
            Ok(values) => {
                let Some(values) = values.array() else {
                    self.finish(Outcome::Failed {
                        code: "invalid".into(),
                    })
                    .await?;
                    return Err(StepError::new(
                        "invalid",
                        "Flow stream returned a non-array batch",
                    ));
                };
                for value in values.iter() {
                    if let Ok(event) = RunEvent::from_value(&value) {
                        self.terminal = match event.kind {
                            RunEventKind::Completed { .. } => Some(Outcome::Succeeded),
                            RunEventKind::Failed { error } => Some(Outcome::Failed {
                                code: error.audit_code().into(),
                            }),
                            RunEventKind::Cancelled { .. } => Some(Outcome::Cancelled),
                            _ => self.terminal.take(),
                        };
                    }
                }
                if values.is_empty() {
                    self.end_batch = Some(result.as_ref().expect("successful batch").clone());
                    let Some(outcome) = self.terminal.take() else {
                        self.finish(Outcome::Failed {
                            code: "internal".into(),
                        })
                        .await?;
                        return Err(StepError::new(
                            "internal",
                            "Flow stream ended without a terminal event",
                        ));
                    };
                    self.finish(outcome).await?;
                }
            }
            Err(error) => {
                // Flow's native transport retains the uncommitted batch when
                // a bounded response buffer cannot be produced. Leave the
                // same stream and FlowRun grant live so the host may retry.
                if error.code == "batch-too-large" {
                    return Err(error.clone());
                }
                self.finish(if error.code == "cancelled" {
                    Outcome::Cancelled
                } else {
                    Outcome::Failed {
                        code: error.audit_code().into(),
                    }
                })
                .await?
            }
        }
        result
    }

    async fn pull_final(&mut self, max: u32) -> Result<CoreBuffer, StepError> {
        if max == 0 {
            return Err(StepError::new(
                "invalid",
                "stream pull size must be positive",
            ));
        }
        if self.closed {
            self.await_cleanup().await?;
            return self.end_buffer.clone().ok_or_else(|| {
                StepError::new(
                    "unavailable",
                    "closed Flow stream has no producer-encoded empty batch",
                )
            });
        }
        if self.cx.check_active().is_err()
            || self
                .gate
                .epoch(self.cx.profile())
                .map_err(|e| StepError::new(e.code(), "epoch unavailable"))?
                != self.cx.security_epoch()
        {
            self.finish(Outcome::Cancelled).await?;
            return Err(StepError::new("cancelled", "Flow stream authority expired"));
        }
        let source = self.source.as_mut().expect("live tracked source");
        let result = tokio::select! {
            _=self.cx.cancel().cancelled()=>Err(StepError::new("cancelled","Flow stream cancelled")),
            _=tokio::time::sleep_until(self.cx.deadline().into())=>Err(StepError::new("cancelled","original Flow deadline expired")),
            result=source.pull_final(max)=>result,
        };
        let producer_terminal = self.source.as_ref().and_then(CoreStream::terminal_outcome);
        match result {
            Ok(buffer) => {
                if let Some(terminal) = producer_terminal {
                    self.terminal = Some(match terminal {
                        StreamTerminal::Succeeded => Outcome::Succeeded,
                        StreamTerminal::Cancelled => Outcome::Cancelled,
                        StreamTerminal::Failed(code) => Outcome::Failed { code: code.into() },
                    });
                }
                if buffer.as_bytes() == b"[]" {
                    self.end_buffer = Some(buffer.clone());
                    let Some(outcome) = self.terminal.take() else {
                        self.finish(Outcome::Failed {
                            code: "internal".into(),
                        })
                        .await?;
                        return Err(StepError::new(
                            "internal",
                            "Flow stream ended without terminal metadata",
                        ));
                    };
                    self.finish(outcome).await?;
                }
                Ok(buffer)
            }
            Err(error) => {
                if error.code == "batch-too-large" {
                    return Err(error);
                }
                self.finish(if error.code == "cancelled" {
                    Outcome::Cancelled
                } else {
                    Outcome::Failed {
                        code: error.audit_code().into(),
                    }
                })
                .await?;
                Err(error)
            }
        }
    }

    fn cancel(&mut self) {
        let outcome = self.terminal.take().unwrap_or(Outcome::Cancelled);
        self.begin_close(outcome);
    }
    async fn close(&mut self) -> Result<(), StepError> {
        self.cancel();
        self.await_cleanup().await
    }
}

#[cfg(test)]
#[path = "tracked_stream_tests.rs"]
mod tests;
