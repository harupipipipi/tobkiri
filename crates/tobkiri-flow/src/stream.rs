use async_trait::async_trait;
use tobkiri_canon::{CoreBuffer, CoreRef, CoreValue};

use crate::StepError;

/// Terminal result observed by a stream producer. The Core wrapper uses this
/// metadata to settle the original invocation after the final event batch is
/// drained, without reparsing or copying encoded guest payloads.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StreamTerminal {
    Succeeded,
    Cancelled,
    Failed(&'static str),
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

    async fn close_after_error(&mut self, error: StepError) -> StepError {
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
        self.0.cancel();
    }
}

impl Drop for CoreStream {
    fn drop(&mut self) {
        // A provider trap while releasing its handle cannot unwind through Core.
        let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| self.0.cancel()));
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
}
