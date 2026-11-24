//! Bound directory request ownership and retirement to the transport lifetime.
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use crate::host_contract::ExecutionProfileIdentity;

const RETIREMENT_LIFETIME: Duration = Duration::from_secs(300);
const MAX_RETIRED_REQUESTS: usize = 64;
type Owner = (String, ExecutionProfileIdentity);

#[derive(Default)]
pub(crate) struct DirectoryRequests {
    active: Option<(Owner, Arc<AtomicBool>)>,
    retired: Vec<(Owner, Instant)>,
    saturated_until: Option<Instant>,
}

impl DirectoryRequests {
    fn prune(&mut self, now: Instant) {
        self.retired.retain(|(_, deadline)| *deadline > now);
        if self.saturated_until.is_some_and(|deadline| deadline <= now) {
            self.saturated_until = None;
        }
    }

    pub(crate) fn begin(
        &mut self,
        nonce: String,
        identity: ExecutionProfileIdentity,
        flag: Arc<AtomicBool>,
        now: Instant,
    ) -> bool {
        self.prune(now);
        let owner = (nonce, identity);
        if self.active.is_some()
            || self.saturated_until.is_some()
            || self.retired.iter().any(|(entry, _)| entry == &owner)
        {
            return false;
        }
        self.active = Some((owner, flag));
        true
    }

    pub(crate) fn retire(
        &mut self,
        nonce: String,
        identity: ExecutionProfileIdentity,
        now: Instant,
    ) {
        self.prune(now);
        let owner = (nonce, identity);
        if let Some((active, flag)) = &self.active {
            if active == &owner {
                flag.store(true, Ordering::Release);
            }
        }
        let deadline = now + RETIREMENT_LIFETIME;
        if let Some((_, expiry)) = self.retired.iter_mut().find(|(entry, _)| entry == &owner) {
            *expiry = deadline;
        } else if self.retired.len() < MAX_RETIRED_REQUESTS {
            self.retired.push((owner, deadline));
        } else {
            // Keep every live fence. Overflow fails new requests closed for
            // the full lifetime of the request that could not be recorded.
            self.saturated_until = Some(deadline);
        }
    }

    pub(crate) fn finish(&mut self, now: Instant) {
        if let Some(((nonce, identity), _)) = self.active.take() {
            self.retire(nonce, identity, now);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn identity() -> ExecutionProfileIdentity {
        ExecutionProfileIdentity::new(
            "defaults",
            format!("sha256:{}", "a".repeat(64)),
            "activation:directory-requests",
            format!("sha256:{}", "b".repeat(64)),
        )
        .unwrap()
    }

    #[test]
    fn another_request_cannot_erase_a_retired_request() {
        let mut state = DirectoryRequests::default();
        let now = Instant::now();
        state.retire("a".into(), identity(), now);
        assert!(state.begin(
            "b".into(),
            identity(),
            Arc::new(AtomicBool::new(false)),
            now
        ));
        state.retire("c".into(), identity(), now);
        state.finish(now);
        for nonce in ["a", "b", "c"] {
            assert!(!state.begin(
                nonce.into(),
                identity(),
                Arc::new(AtomicBool::new(false)),
                now
            ));
        }
    }

    #[test]
    fn retirement_only_cancels_the_exact_active_owner() {
        let mut state = DirectoryRequests::default();
        let flag = Arc::new(AtomicBool::new(false));
        let now = Instant::now();
        assert!(state.begin("a".into(), identity(), flag.clone(), now));
        state.retire("b".into(), identity(), now);
        assert!(!flag.load(Ordering::Acquire));
        state.retire("a".into(), identity(), now);
        assert!(flag.load(Ordering::Acquire));
    }

    #[test]
    fn overflow_preserves_fences_and_expires_after_the_transport_lifetime() {
        let mut state = DirectoryRequests::default();
        let now = Instant::now();
        for number in 0..=MAX_RETIRED_REQUESTS {
            state.retire(number.to_string(), identity(), now);
        }
        assert_eq!(state.retired.len(), MAX_RETIRED_REQUESTS);
        assert!(!state.begin(
            "new".into(),
            identity(),
            Arc::new(AtomicBool::new(false)),
            now
        ));
        assert!(state.begin(
            "new".into(),
            identity(),
            Arc::new(AtomicBool::new(false)),
            now + RETIREMENT_LIFETIME
        ));
    }
}
