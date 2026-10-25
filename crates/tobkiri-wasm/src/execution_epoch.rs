//! Execution lifetime checks without a Profile, principal or authority API.
use std::time::{Duration, Instant};
use tokio_util::sync::CancellationToken;
use wasmtime::UpdateDeadline;

const CANCEL_GRACE: Duration = Duration::from_secs(2);

/// Read the CURRENT entry lifetime; never cache a reused Pack Store's CallCx.
pub(crate) fn check_epoch(
    deadline: Instant,
    cancel: &CancellationToken,
    retirement: Option<&CancellationToken>,
    cancel_observed: &mut Option<Instant>,
) -> wasmtime::Result<UpdateDeadline> {
    if retirement.is_some_and(|token| token.is_cancelled()) {
        return Err(wasmtime::Error::msg("component instance retired"));
    }
    if Instant::now() >= deadline {
        return Err(wasmtime::Error::msg("original component deadline exceeded"));
    }
    if cancel.is_cancelled() {
        let observed = cancel_observed.get_or_insert_with(Instant::now);
        if observed.elapsed() >= CANCEL_GRACE {
            return Err(wasmtime::Error::msg(
                "component cancelled after grace period",
            ));
        }
    }
    Ok(UpdateDeadline::Yield(1))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn actual_retirement_takes_precedence_over_an_expired_deadline() {
        let cancel = CancellationToken::new();
        let retirement = CancellationToken::new();
        retirement.cancel();
        let mut observed = None;
        let error = check_epoch(Instant::now(), &cancel, Some(&retirement), &mut observed)
            .err()
            .expect("epoch check must reject this lifetime");
        assert_eq!(error.to_string(), "component instance retired");
        assert!(observed.is_none());
    }

    #[test]
    fn cancellation_uses_the_first_observation_without_renewing_the_grace() {
        let cancel = CancellationToken::new();
        cancel.cancel();
        let deadline = Instant::now() + Duration::from_secs(30);
        let mut observed = None;
        assert!(matches!(
            check_epoch(deadline, &cancel, None, &mut observed).unwrap(),
            UpdateDeadline::Yield(1)
        ));
        assert!(observed.is_some());
        let original = Instant::now() - CANCEL_GRACE - Duration::from_millis(1);
        observed = Some(original);
        let error = check_epoch(deadline, &cancel, None, &mut observed)
            .err()
            .expect("epoch check must reject this lifetime");
        assert_eq!(error.to_string(), "component cancelled after grace period");
        assert_eq!(observed, Some(original));
    }

    #[test]
    fn current_entry_deadline_is_rechecked_instead_of_cached_by_the_store() {
        let cancel = CancellationToken::new();
        let mut observed = None;
        assert_eq!(
            check_epoch(Instant::now(), &cancel, None, &mut observed)
                .err()
                .expect("epoch check must reject this lifetime")
                .to_string(),
            "original component deadline exceeded"
        );
        let next_deadline = Instant::now() + Duration::from_secs(30);
        assert!(matches!(
            check_epoch(next_deadline, &cancel, None, &mut observed).unwrap(),
            UpdateDeadline::Yield(1)
        ));
    }
}
