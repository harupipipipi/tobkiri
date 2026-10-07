//! One monotonic budget for a Shell launch, before its separate receipt window.

use std::time::{Duration, Instant};

use anyhow::{bail, Result};

pub(crate) const TIMEOUT: Duration = Duration::from_secs(600);

pub(crate) fn remaining(deadline: Instant) -> Result<Duration> {
    remaining_at(deadline, Instant::now())
}

fn remaining_at(deadline: Instant, now: Instant) -> Result<Duration> {
    let remaining = deadline.saturating_duration_since(now);
    if remaining.is_zero() {
        bail!("Shell preparation deadline expired");
    }
    Ok(remaining)
}

pub(crate) fn check(deadline: Instant) -> Result<()> {
    remaining(deadline).map(|_| ())
}

pub(crate) fn request_timeout(deadline: Option<Instant>, cap: Duration) -> Result<Duration> {
    match deadline {
        Some(deadline) => Ok(remaining(deadline)?.min(cap)),
        None => Ok(cap),
    }
}

pub(crate) fn check_optional(deadline: Option<Instant>) -> Result<()> {
    match deadline {
        Some(deadline) => check(deadline),
        None => Ok(()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cold_readiness_after_old_cutoff_keeps_the_same_total_budget() {
        let start = Instant::now();
        let deadline = start + TIMEOUT;
        assert_eq!(
            remaining_at(deadline, start + Duration::from_secs(181)).unwrap(),
            Duration::from_secs(419),
        );
        assert!(remaining_at(deadline, deadline).is_err());
        assert!(remaining_at(deadline, deadline + Duration::from_secs(1)).is_err());
    }

    #[test]
    fn expired_budget_never_starts_another_request() {
        assert!(request_timeout(Some(Instant::now()), Duration::from_secs(30)).is_err());
        assert_eq!(
            request_timeout(None, Duration::from_secs(30)).unwrap(),
            Duration::from_secs(30)
        );
        let remaining = request_timeout(
            Some(Instant::now() + Duration::from_secs(2)),
            Duration::from_secs(30),
        )
        .unwrap();
        assert!(remaining <= Duration::from_secs(2));
        assert!(!remaining.is_zero());
    }
}
