//! Fixed final-consumer encoders. The dispatcher supplies the actual live
//! provider OutputBuffer budget before terminal/retirement; no callback runs.
use std::sync::Arc;
use tobkiri_canon::{AllocationBudget, AllocationError, CoreBuffer, CoreValue, MAX_SAFE_INTEGER};
use tobkiri_flow::FinalOutputKind;

pub(crate) fn encode_value(
    value: &CoreValue,
    kind: FinalOutputKind,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreBuffer, AllocationError> {
    match kind {
        FinalOutputKind::CanonicalValue => value.encode_guest(budget),
        FinalOutputKind::UiInvokeValue { .. } => {
            let tag = CoreValue::string("value", budget)?;
            let envelope = CoreValue::object_shared(
                [Ok(("kind", tag)), Ok(("value", value.clone()))].into_iter(),
                budget,
            )?;
            envelope.encode_guest(budget)
        }
    }
}

pub(crate) fn encode_stream(
    kind: FinalOutputKind,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreBuffer, AllocationError> {
    let FinalOutputKind::UiInvokeValue { stream_handle } = kind else {
        return Err(AllocationError::Invalid);
    };
    // The Session reserves the unpublished candidate. StrictV1's exact integer
    // range is also the UI's lossless numeric range; do not round or retag it.
    if stream_handle > MAX_SAFE_INTEGER {
        return Err(AllocationError::Invalid);
    }
    let tag = CoreValue::string("stream", budget)?;
    let handle = CoreValue::int(stream_handle as i64, budget)?;
    let envelope = CoreValue::object_shared(
        [Ok(("kind", tag)), Ok(("handle", handle))].into_iter(),
        budget,
    )?;
    envelope.encode_guest(budget)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};
    use tobkiri_canon::AllocationRequest;

    struct Budget {
        live: AtomicU64,
        maximum: u64,
    }
    impl AllocationBudget for Budget {
        fn reserve(&self, request: AllocationRequest) -> Result<(), AllocationError> {
            self.live
                .fetch_update(Ordering::AcqRel, Ordering::Acquire, |live| {
                    live.checked_add(request.bytes)
                        .filter(|next| *next <= self.maximum)
                })
                .map(|_| ())
                .map_err(|_| AllocationError::Limit)
        }
        fn release(&self, bytes: u64) {
            let previous = self.live.fetch_sub(bytes, Ordering::AcqRel);
            assert!(previous >= bytes);
        }
    }
    fn budget(maximum: u64) -> (Arc<Budget>, Arc<dyn AllocationBudget>) {
        let owner = Arc::new(Budget {
            live: AtomicU64::new(0),
            maximum,
        });
        (owner.clone(), owner)
    }

    #[test]
    fn value_envelope_is_closed_and_serialization_retains_its_own_charge() {
        let (source, source_budget) = budget(1024 * 1024);
        let (output, output_budget) = budget(1024 * 1024);
        let value = CoreValue::string("leased", &source_budget).unwrap();
        let raw = encode_value(&value, FinalOutputKind::CanonicalValue, &output_budget).unwrap();
        assert_eq!(raw.as_bytes(), br#""leased""#);
        let envelope = encode_value(
            &value,
            FinalOutputKind::UiInvokeValue { stream_handle: 7 },
            &output_budget,
        )
        .unwrap();
        assert_eq!(envelope.as_bytes(), br#"{"kind":"value","value":"leased"}"#);
        drop(value);
        assert_eq!(source.live.load(Ordering::Acquire), 0);
        assert!(output.live.load(Ordering::Acquire) > 0);
        drop(raw);
        drop(envelope);
        assert_eq!(output.live.load(Ordering::Acquire), 0);
    }

    #[test]
    fn stream_envelope_uses_only_the_reserved_lossless_candidate() {
        let (_, owner) = budget(1024 * 1024);
        let value =
            encode_stream(FinalOutputKind::UiInvokeValue { stream_handle: 17 }, &owner).unwrap();
        assert_eq!(value.as_bytes(), br#"{"handle":17,"kind":"stream"}"#);
        assert!(encode_stream(FinalOutputKind::CanonicalValue, &owner).is_err());
        assert!(
            encode_stream(
                FinalOutputKind::UiInvokeValue {
                    stream_handle: MAX_SAFE_INTEGER + 1
                },
                &owner
            )
            .is_err()
        );
    }

    #[test]
    fn output_limit_failure_preserves_borrowed_source_and_refunds_only_own_storage() {
        let (_, source) = budget(1024 * 1024);
        let (destination, output) = budget(1);
        let value = CoreValue::string("unchanged", &source).unwrap();
        assert!(
            encode_value(
                &value,
                FinalOutputKind::UiInvokeValue { stream_handle: 1 },
                &output
            )
            .is_err()
        );
        assert_eq!(value.as_str(), Some("unchanged"));
        assert_eq!(destination.live.load(Ordering::Acquire), 0);
    }
}
