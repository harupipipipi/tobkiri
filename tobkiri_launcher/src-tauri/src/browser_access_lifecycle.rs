//! Generation-fenced admission cleanup; no UI or transport dependencies.
use std::collections::HashMap;

const PREFIX: &str = "browser-access-approval-";

/// Return the controlled label for one random admission generation.
pub(crate) fn generation_label(nonce: &str) -> String {
    format!("{PREFIX}{nonce}")
}

/// Recognize only labels carrying the complete lowercase 256-bit nonce.
pub(crate) fn controlled_label(label: &str) -> bool {
    label.strip_prefix(PREFIX).is_some_and(|nonce| {
        nonce.len() == 64
            && nonce
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    })
}

/// Remove the record only when its generation still matches, under the caller's lock.
pub(crate) fn take_generation<T>(
    records: &mut HashMap<String, T>,
    request_id: &str,
    nonce: &str,
    nonce_of: impl FnOnce(&T) -> &str,
) -> Option<T> {
    if records
        .get(request_id)
        .is_some_and(|p| nonce_of(p) == nonce)
    {
        records.remove(request_id)
    } else {
        None
    }
}

/// Remove only the record owning this exact controlled window, under one lock.
pub(crate) fn take_window_generation<T>(
    records: &mut HashMap<String, T>,
    label: &str,
    label_of: impl Fn(&T) -> &str,
) -> Option<T> {
    if !controlled_label(label) {
        return None;
    }
    let id = records
        .iter()
        .find(|(_, p)| label_of(p) == label)
        .map(|(id, _)| id.clone())?;
    records.remove(&id)
}

/// Return a conservative whole-second lifetime, rejecting expired/subsecond records.
pub(crate) fn remaining_ttl(deadline: std::time::Instant, now: std::time::Instant) -> Option<u64> {
    let remaining = deadline.saturating_duration_since(now).as_secs();
    (remaining > 0).then_some(remaining)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[derive(Clone)]
    struct Record {
        id: String,
        nonce: String,
        label: String,
    }
    fn record(id: &str, digit: &str) -> Record {
        let nonce = digit.repeat(64);
        Record {
            id: id.into(),
            label: generation_label(&nonce),
            nonce,
        }
    }
    #[derive(Default)]
    struct Effects {
        denied: Vec<String>,
        queued_close: Vec<String>,
        windows: HashMap<String, bool>,
    }
    impl Effects {
        fn cleanup(&mut self, pending: Option<Record>) {
            if let Some(p) = pending {
                self.denied.push(p.id);
                self.queued_close.push(p.label);
            }
        }
        fn flush_close(&mut self) {
            for label in self.queued_close.drain(..) {
                if let Some(open) = self.windows.get_mut(&label) {
                    *open = false;
                }
            }
        }
    }
    #[test]
    fn destroyed_after_a_settlement_never_denies_or_closes_b() {
        for b_id in ["A", "B"] {
            let a = record("A", "a");
            let b = record(b_id, "b");
            let mut records = HashMap::from([(a.id.clone(), a.clone())]);
            let mut effects = Effects::default();
            // Decision completion settles A without denial; its Destroyed arrives later.
            assert!(take_generation(&mut records, &a.id, &a.nonce, |p| &p.nonce).is_some());
            records.insert(b.id.clone(), b.clone());
            effects.windows.insert(b.label.clone(), true);
            effects.cleanup(take_window_generation(&mut records, &a.label, |p| &p.label));
            effects.flush_close();
            assert!(effects.denied.is_empty());
            assert!(effects.windows[&b.label]);
            assert_eq!(records[&b.id].nonce, b.nonce);
            // The exact B close event removes B once and emits B's own cleanup.
            effects.cleanup(take_window_generation(&mut records, &b.label, |p| &p.label));
            assert!(take_window_generation(&mut records, &b.label, |p| &p.label).is_none());
            assert_eq!(effects.denied, vec![b.id]);
        }
    }
    #[test]
    fn delayed_expiry_error_and_decision_completion_leave_replacement_untouched() {
        let a = record("same", "a");
        let b = record("same", "b");
        let mut records = HashMap::from([(a.id.clone(), a.clone())]);
        let mut effects = Effects::default();
        // First expiry removes A and queues its UI close before B is admitted.
        effects.cleanup(take_generation(&mut records, &a.id, &a.nonce, |p| &p.nonce));
        effects.windows.insert(a.label.clone(), true);
        records.insert(b.id.clone(), b.clone());
        effects.windows.insert(b.label.clone(), true);
        // A's transport error and delayed timer attempt cleanup after B admission.
        effects.cleanup(take_generation(&mut records, &a.id, &a.nonce, |p| &p.nonce));
        effects.cleanup(take_generation(&mut records, &a.id, &a.nonce, |p| &p.nonce));
        // A's late success completion removes no B and queues only A's captured label.
        assert!(take_generation(&mut records, &a.id, &a.nonce, |p| &p.nonce).is_none());
        effects.queued_close.push(a.label.clone());
        effects.flush_close();
        assert_eq!(effects.denied, vec![a.id]);
        assert!(!effects.windows[&a.label]);
        assert!(effects.windows[&b.label]);
        assert_eq!(records[&b.id].nonce, b.nonce);
    }
    #[test]
    fn exact_cleanup_is_request_bound_and_labels_are_narrowly_controlled() {
        let a = record("request", "a");
        let mut records = HashMap::from([(a.id.clone(), a.clone())]);
        assert!(take_generation(&mut records, "other", &a.nonce, |p| &p.nonce).is_none());
        for invalid in [
            "browser-access-approval",
            "browser-access-approval-a",
            "main",
        ] {
            assert!(!controlled_label(invalid));
            assert!(take_window_generation(&mut records, invalid, |p| &p.label).is_none());
        }
        assert!(!controlled_label(&generation_label(&"A".repeat(64))));
        assert!(!controlled_label(&(a.label.clone() + "-extra")));
        assert!(take_window_generation(&mut records, &a.label, |p| &p.label).is_some());
    }
    #[test]
    fn ttl_is_floored_and_late_focus_does_not_extend_deadline() {
        use std::time::{Duration, Instant};
        let start = Instant::now();
        let deadline = start + Duration::from_millis(2500);
        assert_eq!(remaining_ttl(deadline, start), Some(2));
        assert_eq!(
            remaining_ttl(deadline, start + Duration::from_millis(1200)),
            Some(1)
        );
        assert_eq!(
            remaining_ttl(deadline, start + Duration::from_millis(2000)),
            None
        );
        assert_eq!(remaining_ttl(deadline, deadline), None);
        assert_eq!(
            remaining_ttl(deadline, deadline + Duration::from_secs(1)),
            None
        );
    }
}
