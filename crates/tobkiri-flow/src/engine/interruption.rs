use std::{future::Future, time::Duration};

use tobkiri_gate::CallCx;

use crate::StepError;

use super::{CleanupFailure, CleanupReceipt, is_no_replay};

const CLEANUP_GRACE: Duration = Duration::from_secs(2);

#[derive(Clone, Copy)]
enum Stopped {
    Cancelled,
    Deadline,
}

impl Stopped {
    fn observe(cx: &CallCx) -> Option<Self> {
        if std::time::Instant::now() >= cx.deadline() {
            Some(Self::Deadline)
        } else if cx.cancel().is_cancelled() {
            Some(Self::Cancelled)
        } else {
            None
        }
    }

    fn signal(self, cx: &CallCx) {
        match self {
            Self::Cancelled => cx.cancel().cancel(),
            Self::Deadline => cx.cancel().notify_deadline(),
        }
    }

    fn error(self) -> StepError {
        StepError::new(
            "cancelled",
            match self {
                Self::Cancelled => "run cancelled by its input source",
                Self::Deadline => "original run deadline expired",
            },
        )
    }
}

/// Fixed stack custody for an already observed facade across an awaited close
/// or failure journal write. It carries no guest message or native authority.
/// Only abandonment while the original context is inactive makes it sticky;
/// a completed await leaves the normal result/ErrorPolicy path in charge.
pub(super) struct ObservedError<'a> {
    receipt: &'a CleanupReceipt,
    context: &'a CallCx,
    failure: Option<CleanupFailure>,
    finished: bool,
}

impl<'a> ObservedError<'a> {
    pub(super) fn new(receipt: &'a CleanupReceipt, context: &'a CallCx) -> Self {
        Self {
            receipt,
            context,
            failure: None,
            finished: false,
        }
    }

    pub(super) fn record(&mut self, error: &StepError) {
        if is_no_replay(error) {
            self.failure = Some(CleanupFailure::from_error(error));
        }
    }

    pub(super) fn complete(&mut self) {
        self.finished = true;
    }
}

impl Drop for ObservedError<'_> {
    fn drop(&mut self) {
        if !self.finished && Stopped::observe(self.context).is_some() {
            if let Some(failure) = self.failure {
                self.receipt.record(failure);
            }
        }
    }
}

/// Keep the exact interpreter future and its stream cleanup guards alive while
/// it responds to the original cancellation/deadline. This does not create a
/// new context, authorize another dispatch, or extend its execution deadline.
/// If cleanup cannot finish within the grace, dropping this same future leaves
/// the existing sticky incomplete-cleanup receipt in charge of the result.
pub(super) async fn execute_with_cleanup<F, T>(
    cx: &CallCx,
    cleanup: &CleanupReceipt,
    execute: F,
) -> Result<T, StepError>
where
    F: Future<Output = Result<T, StepError>>,
{
    tokio::pin!(execute);
    let stopped = tokio::select! {
        biased;
        _ = tokio::time::sleep_until(cx.deadline().into()) => Stopped::Deadline,
        _ = cx.cancel().cancelled() => Stopped::Cancelled,
        result = &mut execute => {
            // A ready result can cross the stop boundary during its own poll.
            // Preserve failures, including no-replay evidence, but never turn
            // a late successful result into logical success.
            let stopped = Stopped::observe(cx);
            if let Some(stopped) = stopped {
                stopped.signal(cx);
            }
            return match result {
                Err(error) => Err(error),
                Ok(value) => match stopped {
                    Some(stopped) => {
                        drop(value);
                        Err(stopped.error())
                    }
                    None => Ok(value),
                },
            };
        }
    };
    stopped.signal(cx);
    // Poll the already-started work, not a replacement execution. Ordinary
    // dispatch and journal writes still see the inactive original CallCx.
    match tokio::time::timeout(CLEANUP_GRACE, &mut execute).await {
        Ok(Err(error)) => Err(error),
        Ok(Ok(_)) => Err(stopped.error()),
        Err(_) => {
            // Work may still be suspended before it returns a CoreStream and
            // installs that source's guard. Abandonment of the whole exact
            // execution still cannot certify cleanup. A stronger existing
            // no-replay receipt cannot be overwritten by this fixed marker.
            cleanup.record(CleanupFailure::Incomplete);
            Err(stopped.error())
        }
    }
}
