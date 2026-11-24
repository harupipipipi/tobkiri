//! One consumed Flow permit and its original durable terminal handoff.

use std::{
    sync::Arc,
    time::{Duration, Instant},
};
use tobkiri_gate::{
    CallCx, CleanupSnapshotAttempt, DispatchPermit, Gate, GateEngine, GateError, Outcome,
};

/// Original close episode budget; no retry or later observer restarts it.
pub(crate) const NATIVE_CLEANUP_GRACE: Duration = Duration::from_secs(2);

/// Sole owner of one already-consumed FlowRun or virtual-ingress permit while
/// Core prepares/executes the exact entry. A frame may borrow the permit only
/// synchronously during prepared verification; terminalization consumes this
/// wrapper so the same grant cannot acquire a second outcome owner.
pub(crate) struct ConsumedEntryTerminal {
    permit: DispatchPermit,
    gate: Arc<GateEngine>,
    proposal: Option<Outcome>,
    finished: bool,
}
impl ConsumedEntryTerminal {
    pub(crate) fn new(permit: DispatchPermit, gate: Arc<GateEngine>) -> Self {
        Self {
            permit,
            gate,
            proposal: None,
            finished: false,
        }
    }

    /// Borrow the exact consumed permit for Core's synchronous frame verifier.
    pub(crate) fn permit(&self) -> &DispatchPermit {
        &self.permit
    }

    /// Supervise the original lease before execution or any other fallible
    /// post-consume work. The owner retains this wrapper if supervision fails
    /// and must still submit one terminal proposal.
    pub(crate) fn watch(&self, cx: &CallCx) -> Result<(), GateError> {
        if self.finished {
            return Err(GateError::Consumed);
        }
        self.gate.watch_lease(cx, &self.permit)
    }

    /// Submit the immutable terminal proposal. Once the original deadline is
    /// due, Gate settles the deadline family from this exact permit instead of
    /// accepting a caller-selected cancellation or success outcome.
    pub(crate) async fn finish(&mut self, outcome: Outcome) -> Result<(), GateError> {
        if let Some(proposal) = &self.proposal {
            if proposal != &outcome {
                return Err(GateError::Denied(
                    "consumed Flow terminal proposal is immutable".into(),
                ));
            }
        } else {
            self.proposal = Some(outcome.clone());
        }
        if self.finished {
            return Ok(());
        }
        if std::time::Instant::now() >= self.permit.deadline() {
            self.gate.settle_deadline(&self.permit).await.map(|_| ())
        } else {
            self.gate
                .record_outcome(self.permit.id().clone(), outcome)
                .await
        }?;
        self.finished = true;
        Ok(())
    }

    /// Read-only transfer to the existing durable terminal outbox/recovery
    /// owner. Exclusive custody of this sealed permit is retained throughout;
    /// this selects no native cause, performs no notification and creates no
    /// reservation, effect or terminal. Pending audit ACK is allowed.
    pub(crate) fn try_durable_handoff(
        &mut self,
        deadline: Instant,
    ) -> Result<Option<Outcome>, GateError> {
        if Instant::now() >= deadline {
            return Err(GateError::Deadline);
        }
        let mut winner = None;
        let attempt = self.gate.try_cleanup_snapshot(&self.permit, &mut |snapshot| {
            if snapshot.observed_at >= deadline {
                return Err(GateError::Deadline);
            }
            let terminal = snapshot.authority.terminal.ok_or(GateError::Unavailable)?;
            if matches!(&terminal.decision.outcome, Outcome::Failed { code } if code.len() > 128) {
                return Err(GateError::Unavailable);
            }
            // Gate has validated the exact original consumed reservation,
            // effect, args, revision, epoch and retained terminal identity.
            winner = Some(terminal.decision.outcome.clone());
            Ok(())
        });
        match attempt {
            CleanupSnapshotAttempt::RetryableBusyBeforeCallback => return Ok(None),
            CleanupSnapshotAttempt::Uncertain(error) => return Err(error),
            CleanupSnapshotAttempt::Selected => {}
        }
        if Instant::now() >= deadline {
            return Err(GateError::Deadline);
        }
        let winner = winner.ok_or(GateError::Unavailable)?;
        self.proposal = Some(winner.clone());
        Ok(Some(winner))
    }
}

/// Retry only authenticated pre-callback Store contention. This borrows the
/// original terminal slot; no source close, terminal write, task or authority
/// is created. Every guard is released before sleeping. The deadline belongs
/// to the first close intent, including all physical cleanup before this call.
pub(crate) async fn observe_durable_handoff(
    slot: &tokio::sync::Mutex<Option<ConsumedEntryTerminal>>,
    deadline: Instant,
) -> Result<Outcome, GateError> {
    let mut backoff = Duration::from_millis(2);
    #[cfg(debug_assertions)]
    let mut busy_reported = false;
    loop {
        if Instant::now() >= deadline {
            return Err(GateError::Deadline);
        }
        let attempt = {
            let mut terminal = tokio::time::timeout_at(deadline.into(), slot.lock())
                .await
                .map_err(|_| GateError::Deadline)?;
            terminal
                .as_mut()
                .ok_or(GateError::Unavailable)?
                .try_durable_handoff(deadline)
        };
        match attempt? {
            Some(winner) => return Ok(winner),
            None => {
                #[cfg(debug_assertions)]
                if !busy_reported {
                    eprintln!("FLOW_TERMINAL_HANDOFF_BUSY stage=before-callback");
                    busy_reported = true;
                }
                let remaining = deadline.saturating_duration_since(Instant::now());
                if remaining.is_zero() {
                    return Err(GateError::Deadline);
                }
                tokio::time::sleep(backoff.min(remaining)).await;
                backoff = (backoff * 2).min(Duration::from_millis(32));
            }
        }
    }
}

#[cfg(test)]
#[path = "consumed_entry_terminal_tests.rs"]
mod tests;
