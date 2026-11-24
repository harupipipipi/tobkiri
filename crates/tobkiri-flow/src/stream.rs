use async_trait::async_trait;
use std::sync::Arc;
use tobkiri_canon::{CoreBuffer, CoreRef, CoreValue};

use crate::{RunTaskCompletion, StepError};

/// A cloneable observer of the original physical-work receipt. Its data Arc is
/// private: consumers can wait or clone this lease, but cannot manufacture Weak
/// aliases whose allocation outlives the external control owner. Field order
/// releases the data allocation before its optional external funding.
#[derive(Clone)]
pub struct StreamPhysicalCompletion {
    completion: Arc<dyn RunTaskCompletion>,
    _control: Option<Arc<tobkiri_gate::ControlReservation>>,
}
impl StreamPhysicalCompletion {
    /// Observe an already-owned task receipt without allocating another Arc or
    /// changing its existing task/record custody.
    pub fn from_task(completion: Arc<dyn RunTaskCompletion>) -> Self {
        Self {
            completion,
            _control: None,
        }
    }

    /// Keep external funding through destruction of the original data Arc.
    /// The caller must keep all other data Arc/Weak owners funded separately.
    pub fn with_control(
        completion: Arc<dyn RunTaskCompletion>,
        control: Arc<tobkiri_gate::ControlReservation>,
    ) -> Self {
        Self {
            completion,
            _control: Some(control),
        }
    }

    /// Await the same original receipt; abandoning this wait transfers no work.
    pub async fn wait(&self) -> Result<(), StepError> {
        self.completion.wait().await
    }

    /// Observe original task publication, independently of physical completion.
    pub async fn wait_started(&self) -> Result<(), StepError> {
        self.completion.wait_started().await
    }
}

/// Terminal result observed by a stream producer. The Core wrapper uses this
/// metadata to settle the original invocation after the final event batch is
/// drained, without reparsing or copying encoded guest payloads.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StreamTerminal {
    Succeeded,
    Cancelled,
    Failed(&'static str),
}

/// Delivery custody of the same immutable terminal, independent of its result.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StreamCloseDelivery {
    Acknowledged,
    DurablyHandedOff,
}

/// Bounded native diagnostic metadata from an original completed close receipt.
/// This is not authority or physical proof. Consumers independently retain the
/// exact source's physical receipt; adapters return None unless their original
/// identity, immutable terminal and delivery custody have all been established.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct StreamCloseReport {
    terminal: StreamTerminal,
    business_error: Option<&'static str>,
    audit_error: Option<&'static str>,
    delivery: StreamCloseDelivery,
}
impl StreamCloseReport {
    /// Called only by a trusted native adapter after observing its original
    /// actual cleanup receipt. No guest message or payload is copied here.
    pub fn observed(
        outcome: &tobkiri_gate::Outcome,
        business_error: Option<&StepError>,
        audit_error: Option<&StepError>,
        delivery: StreamCloseDelivery,
    ) -> Option<Self> {
        let terminal = match outcome {
            tobkiri_gate::Outcome::Succeeded => StreamTerminal::Succeeded,
            tobkiri_gate::Outcome::Cancelled => StreamTerminal::Cancelled,
            tobkiri_gate::Outcome::Failed { code } => {
                StreamTerminal::Failed(Self::closed_code(code)?)
            }
        };
        Some(Self {
            terminal,
            business_error: business_error.map(Self::error_code),
            audit_error: audit_error.map(Self::error_code),
            delivery,
        })
    }
    fn closed_code(code: &str) -> Option<&'static str> {
        Some(match code {
            "denied" => "denied",
            "approval-required" => "approval-required",
            "cancelled" => "cancelled",
            "deadline" => "deadline",
            "not-found" => "not-found",
            "invalid" => "invalid",
            "limit-exceeded" => "limit-exceeded",
            "conflict" => "conflict",
            "unavailable" => "unavailable",
            "trap" => "trap",
            "blocked" => "blocked",
            "persistence" => "persistence",
            "internal" => "internal",
            // In particular cleanup-unknown/deadline-failure-unknown are not
            // positive terminal classifications. No string supplies proof.
            _ => return None,
        })
    }
    fn error_code(error: &StepError) -> &'static str {
        Self::closed_code(&error.code).unwrap_or_else(|| error.audit_code())
    }
    pub fn terminal(self) -> StreamTerminal {
        self.terminal
    }
    pub fn business_error(self) -> Option<&'static str> {
        self.business_error
    }
    pub fn audit_error(self) -> Option<&'static str> {
        self.audit_error
    }
    pub fn delivery(self) -> StreamCloseDelivery {
        self.delivery
    }
    pub fn matches_outcome(self, outcome: &tobkiri_gate::Outcome) -> bool {
        match (self.terminal, outcome) {
            (StreamTerminal::Succeeded, tobkiri_gate::Outcome::Succeeded)
            | (StreamTerminal::Cancelled, tobkiri_gate::Outcome::Cancelled) => true,
            (StreamTerminal::Failed(expected), tobkiri_gate::Outcome::Failed { code }) => {
                expected == code
            }
            _ => false,
        }
    }
    pub fn outcome(self) -> tobkiri_gate::Outcome {
        match self.terminal {
            StreamTerminal::Succeeded => tobkiri_gate::Outcome::Succeeded,
            StreamTerminal::Cancelled => tobkiri_gate::Outcome::Cancelled,
            StreamTerminal::Failed(code) => tobkiri_gate::Outcome::Failed { code: code.into() },
        }
    }
    /// A bounded cached error for a repeated observation. The first observer
    /// can move the original error; no arbitrary guest text is cloned to cache.
    pub fn cached_error(self) -> Option<StepError> {
        self.audit_error
            .map(|code| StepError::new(code, "source terminal audit delivery failed"))
            .or_else(|| {
                self.business_error
                    .map(|code| StepError::new(code, "source completed with a business failure"))
            })
    }
}

/// Trusted native intent. A token wake alone supplies none of these causes.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StreamCloseReason {
    Manual,
    Deadline,
    Shutdown,
    Retired,
    Unknown,
}

/// Exact source companion that can record intent without acquiring pull state.
/// Implementations select under their original authority/lifecycle guards and
/// must release every guard before their caller wakes cancellation.
pub trait StreamCloseControl: Send + Sync {
    /// Accept the original close intent. Success acknowledges notification
    /// only; it does not establish cause selection or physical completion.
    fn record_close(&self, reason: StreamCloseReason) -> Result<(), StepError>;
    /// Preserve an actual producer failure separately from cleanup uncertainty.
    /// Implementations may retain only the existing closed audit category.
    fn record_failure(&self, _error: &StepError) -> Result<(), StepError> {
        self.record_close(StreamCloseReason::Unknown)
    }
}

/// Pull-based provider stream. An empty batch is the only end-of-stream marker.
#[async_trait]
pub trait StreamSource: Send {
    /// Pull no more than `max` values. Providers must reject a zero-sized pull.
    async fn pull(&mut self, max: u32) -> Result<CoreValue, StepError>;
    /// Pull and encode one bounded StrictV1 array batch while the actual
    /// producer activation is live. Implementations must not call `pull`, then
    /// encode after that activation has retired; the batch is consumed at most
    /// once even if encoding fails.
    async fn pull_final(&mut self, _max: u32) -> Result<CoreBuffer, StepError> {
        Err(StepError::new(
            "unavailable",
            "producer-funded final stream encoding is unavailable",
        ))
    }
    /// Terminal metadata for event streams whose wire form is opaque to Core.
    fn terminal_outcome(&self) -> Option<StreamTerminal> {
        None
    }
    /// Clone the exact native cleanup companion without borrowing pull state.
    fn close_control(&self) -> Option<Arc<dyn StreamCloseControl>> {
        None
    }
    /// Independent actual-work completion, not a business result or terminal
    /// ACK. Retain the same receipt before close; abandoning a waiter must not
    /// abandon its work. Absence supplies no positive physical proof.
    fn physical_completion(&self) -> Option<StreamPhysicalCompletion> {
        None
    }
    /// Positive classification only from retained original native receipts.
    /// None never permits a caller to downgrade an uncertain close failure.
    fn close_report(&self) -> Option<StreamCloseReport> {
        None
    }
    /// Release outstanding effect requests and provider resources.
    fn cancel(&mut self);
    /// Complete cleanup and surface any failure. This default is for pure
    /// sources; grant-owning sources must await and report their cleanup.
    async fn close(&mut self) -> Result<(), StepError> {
        self.cancel();
        Ok(())
    }
}

/// An owned stream; dropping it always cancels the provider source.
pub struct CoreStream(Box<dyn StreamSource>);

impl CoreStream {
    /// Wrap a provider's source after it has crossed the gated dispatch boundary.
    pub fn new(source: impl StreamSource + 'static) -> Self {
        Self(Box::new(source))
    }

    /// Pull a bounded batch from the provider.
    pub async fn pull(&mut self, max: u32) -> Result<CoreValue, StepError> {
        if max == 0 {
            return Err(StepError::new(
                "invalid",
                "stream pull size must be positive",
            ));
        }
        let values = self.0.pull(max).await?;
        let length = match values.inspect() {
            CoreRef::Arr(values) => values.len(),
            _ => {
                return Err(self
                    .close_after_error(StepError::new(
                        "invalid",
                        "stream pull must return an array",
                    ))
                    .await);
            }
        };
        if length > max as usize {
            return Err(self
                .close_after_error(StepError::new(
                    "invalid",
                    "provider exceeded the stream pull bound",
                ))
                .await);
        }
        Ok(values)
    }

    /// Pull one producer-encoded StrictV1 array batch. Sources that cannot
    /// retain the actual producer scope through encoding remain unavailable.
    pub async fn pull_final(&mut self, max: u32) -> Result<CoreBuffer, StepError> {
        if max == 0 {
            return Err(StepError::new(
                "invalid",
                "stream pull size must be positive",
            ));
        }
        match self.0.pull_final(max).await {
            Ok(buffer) => Ok(buffer),
            // RunStream retains the exact uncommitted batch so the caller can
            // retry after bounded native transport capacity is available.
            Err(error) if error.code == "batch-too-large" => Err(error),
            Err(error) => Err(self.close_after_error(error).await),
        }
    }

    /// Read the producer's terminal metadata without decoding its buffer.
    pub fn terminal_outcome(&self) -> Option<StreamTerminal> {
        self.0.terminal_outcome()
    }

    /// Retain cleanup control independently of an in-flight mutable pull.
    pub fn close_control(&self) -> Option<Arc<dyn StreamCloseControl>> {
        self.0.close_control()
    }

    pub fn physical_completion(&self) -> Option<StreamPhysicalCompletion> {
        self.0.physical_completion()
    }

    pub fn close_report(&self) -> Option<StreamCloseReport> {
        self.0.close_report()
    }

    async fn close_after_error(&mut self, error: StepError) -> StepError {
        if let Some(control) = self.0.close_control() {
            let _ = control.record_failure(&error);
        }
        match self.close().await {
            Ok(()) => error,
            Err(cleanup) => StepError::new(
                error.code,
                format!(
                    "{}; stream cleanup failed: {}",
                    error.message,
                    cleanup.audit_code()
                ),
            ),
        }
    }

    /// Await source cleanup, including any grant outcome or audit failure.
    pub async fn close(&mut self) -> Result<(), StepError> {
        use futures::FutureExt;
        std::panic::AssertUnwindSafe(self.0.close())
            .catch_unwind()
            .await
            .map_err(|_| StepError::new("trap", "stream cleanup trapped"))?
    }

    /// Explicitly stop an outstanding source.
    pub fn cancel(&mut self) {
        if let Some(control) = self.0.close_control() {
            let _ = control.record_close(StreamCloseReason::Manual);
        }
        self.0.cancel();
    }
}

impl Drop for CoreStream {
    fn drop(&mut self) {
        // A provider trap while releasing its handle cannot unwind through Core.
        let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            if let Some(control) = self.0.close_control() {
                let _ = control.record_close(StreamCloseReason::Unknown);
            }
            self.0.cancel();
        }));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    };
    struct Oversized(Arc<AtomicUsize>);
    #[async_trait]
    impl StreamSource for Oversized {
        async fn pull(&mut self, max: u32) -> Result<CoreValue, StepError> {
            let budget: std::sync::Arc<dyn tobkiri_canon::AllocationBudget> =
                std::sync::Arc::new(RejectBudget);
            let count = usize::try_from(max)
                .ok()
                .and_then(|max| max.checked_add(1))
                .ok_or_else(|| StepError::new("limit-exceeded", "test pull bound overflow"))?;
            let values = (0..count).map(|_| {
                tobkiri_canon::CoreValue::null(&budget)
                    .map_err(|_| tobkiri_canon::AllocationError::Unavailable)
            });
            tobkiri_canon::CoreValue::array_from(values, &budget)
                .map_err(|_| StepError::new("unavailable", "test allocation failed"))
        }
        fn cancel(&mut self) {
            self.0.fetch_add(1, Ordering::SeqCst);
        }
    }
    struct RejectBudget;
    impl tobkiri_canon::AllocationBudget for RejectBudget {
        fn reserve(
            &self,
            _: tobkiri_canon::AllocationRequest,
        ) -> Result<(), tobkiri_canon::AllocationError> {
            Ok(())
        }
        fn release(&self, _: u64) {}
    }
    struct FinalOnly(Arc<dyn tobkiri_canon::AllocationBudget>);
    #[async_trait]
    impl StreamSource for FinalOnly {
        async fn pull(&mut self, _max: u32) -> Result<CoreValue, StepError> {
            Err(StepError::new("invalid", "value pull must not be used"))
        }
        async fn pull_final(&mut self, _max: u32) -> Result<CoreBuffer, StepError> {
            CoreBuffer::copy_from(b"[]", &self.0)
                .map_err(|_| StepError::new("unavailable", "test buffer allocation failed"))
        }
        fn cancel(&mut self) {}
    }
    struct RetryFinal(Arc<AtomicUsize>, Arc<AtomicUsize>);
    #[async_trait]
    impl StreamSource for RetryFinal {
        async fn pull(&mut self, _max: u32) -> Result<CoreValue, StepError> {
            Err(StepError::new("invalid", "value pull must not be used"))
        }
        async fn pull_final(&mut self, _max: u32) -> Result<CoreBuffer, StepError> {
            if self.0.fetch_add(1, Ordering::SeqCst) == 0 {
                Err(StepError::new("batch-too-large", "retryable bound"))
            } else {
                let budget: Arc<dyn tobkiri_canon::AllocationBudget> = Arc::new(RejectBudget);
                Ok(CoreBuffer::copy_from(b"[]", &budget).expect("test buffer"))
            }
        }
        fn cancel(&mut self) {
            self.1.fetch_add(1, Ordering::SeqCst);
        }
    }
    #[tokio::test]
    async fn provider_cannot_exceed_pull_size_and_drop_cancels() {
        let cancelled = Arc::new(AtomicUsize::new(0));
        {
            let mut stream = CoreStream::new(Oversized(cancelled.clone()));
            assert_eq!(stream.pull(1).await.unwrap_err().code, "invalid");
            assert_eq!(stream.pull(0).await.unwrap_err().code, "invalid");
        }
        assert!(cancelled.load(Ordering::SeqCst) >= 1);
    }

    #[tokio::test]
    async fn final_pull_does_not_fallback_to_unfunded_value_encoding() {
        let cancelled = Arc::new(AtomicUsize::new(0));
        let mut stream = CoreStream::new(Oversized(cancelled.clone()));

        let error = stream.pull_final(1).await.unwrap_err();

        assert_eq!(error.code, "unavailable");
        assert!(cancelled.load(Ordering::SeqCst) >= 1);
    }

    #[tokio::test]
    async fn final_pull_forwards_the_producer_owned_buffer() {
        let budget: Arc<dyn tobkiri_canon::AllocationBudget> = Arc::new(RejectBudget);
        let mut stream = CoreStream::new(FinalOnly(budget));

        let buffer = stream.pull_final(1).await.unwrap();

        assert_eq!(buffer.as_bytes(), b"[]");
    }

    #[tokio::test]
    async fn retryable_final_batch_error_keeps_the_stream_open() {
        let pulls = Arc::new(AtomicUsize::new(0));
        let cancelled = Arc::new(AtomicUsize::new(0));
        let mut stream = CoreStream::new(RetryFinal(pulls.clone(), cancelled.clone()));

        assert_eq!(
            stream.pull_final(1).await.unwrap_err().code,
            "batch-too-large"
        );
        assert_eq!(cancelled.load(Ordering::SeqCst), 0);
        assert_eq!(stream.pull_final(1).await.unwrap().as_bytes(), b"[]");
        assert_eq!(pulls.load(Ordering::SeqCst), 2);
    }

    #[test]
    fn sources_without_original_native_observation_report_nothing() {
        let source = CoreStream::new(Oversized(Arc::new(AtomicUsize::new(0))));
        assert!(source.close_report().is_none());
    }

    #[test]
    fn closed_report_keeps_delivery_origin_and_rejects_unknown_terminal_family() {
        let business = StepError::new("unavailable", "not copied into native report");
        let audit = StepError::new("persistence", "not copied into native report");
        let winner = tobkiri_gate::Outcome::Failed {
            code: "unavailable".into(),
        };
        let report = StreamCloseReport::observed(
            &winner,
            Some(&business),
            Some(&audit),
            StreamCloseDelivery::DurablyHandedOff,
        )
        .unwrap();
        assert!(report.matches_outcome(&winner));
        assert!(!report.matches_outcome(&tobkiri_gate::Outcome::Cancelled));
        assert_eq!(report.business_error(), Some("unavailable"));
        assert_eq!(report.audit_error(), Some("persistence"));
        assert_eq!(report.delivery(), StreamCloseDelivery::DurablyHandedOff);
        assert_eq!(report.cached_error().unwrap().code, "persistence");
        assert!(
            StreamCloseReport::observed(
                &tobkiri_gate::Outcome::Failed {
                    code: "cleanup-unknown".into()
                },
                None,
                None,
                StreamCloseDelivery::Acknowledged,
            )
            .is_none()
        );
    }
}
