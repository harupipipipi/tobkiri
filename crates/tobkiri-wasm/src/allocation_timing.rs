//! Debug-only observations inside one synchronous native execution segment.
//! Fixed thread-local counters retain no payload, identity, owner or authority.
use std::{cell::Cell, time::Instant};

#[derive(Clone, Copy, Default)]
pub(crate) struct Costs {
    pub(crate) observed: bool,
    pub(crate) reserve_calls: u64,
    pub(crate) zero_byte_calls: u64,
    pub(crate) reserve_us: u64,
    pub(crate) admission_calls: u64,
    pub(crate) admission_us: u64,
}

thread_local! {
    static COSTS: Cell<Option<Costs>> = const { Cell::new(None) };
}

struct Restore(Option<Costs>);
impl Drop for Restore {
    fn drop(&mut self) {
        let _ = COSTS.try_with(|cell| cell.set(self.0));
    }
}

/// No await, logging, allocation, callback installation or task ownership.
/// Restore the previous observation even when the synchronous body unwinds.
pub(crate) fn measure<T>(enabled: bool, work: impl FnOnce() -> T) -> (T, Costs) {
    if !enabled {
        return (work(), Costs::default());
    }
    let Ok(previous) = COSTS.try_with(|cell| {
        cell.replace(Some(Costs {
            observed: true,
            ..Costs::default()
        }))
    }) else {
        return (work(), Costs::default());
    };
    let _restore = Restore(previous);
    let result = work();
    let costs = COSTS.try_with(Cell::get).ok().flatten().unwrap_or_default();
    (result, costs)
}

enum Kind {
    Reserve { zero_bytes: bool },
    Admission,
}

pub(crate) struct Span {
    started: Option<Instant>,
    kind: Kind,
}
impl Span {
    pub(crate) fn reserve(bytes: u64) -> Self {
        Self::new(Kind::Reserve {
            zero_bytes: bytes == 0,
        })
    }
    pub(crate) fn admission() -> Self {
        Self::new(Kind::Admission)
    }
    fn new(kind: Kind) -> Self {
        Self {
            started: COSTS
                .try_with(|cell| cell.get().is_some())
                .unwrap_or(false)
                .then(Instant::now),
            kind,
        }
    }
}
impl Drop for Span {
    fn drop(&mut self) {
        let Some(started) = self.started else {
            return;
        };
        let elapsed = u64::try_from(started.elapsed().as_micros()).unwrap_or(u64::MAX);
        let _ = COSTS.try_with(|cell| {
            let Some(mut costs) = cell.get() else {
                return;
            };
            match self.kind {
                Kind::Reserve { zero_bytes } => {
                    costs.reserve_calls = costs.reserve_calls.saturating_add(1);
                    costs.zero_byte_calls =
                        costs.zero_byte_calls.saturating_add(u64::from(zero_bytes));
                    costs.reserve_us = costs.reserve_us.saturating_add(elapsed);
                }
                Kind::Admission => {
                    costs.admission_calls = costs.admission_calls.saturating_add(1);
                    costs.admission_us = costs.admission_us.saturating_add(elapsed);
                }
            }
            cell.set(Some(costs));
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn nested_synchronous_observations_do_not_mix_callers() {
        let (_, outer) = measure(true, || {
            drop(Span::reserve(0));
            let (_, inner) = measure(true, || {
                drop(Span::reserve(8));
                drop(Span::admission());
            });
            assert_eq!(inner.reserve_calls, 1);
            assert_eq!(inner.zero_byte_calls, 0);
            assert_eq!(inner.admission_calls, 1);
            drop(Span::reserve(0));
        });
        assert_eq!(outer.reserve_calls, 2);
        assert_eq!(outer.zero_byte_calls, 2);
        assert_eq!(outer.admission_calls, 0);
        assert!(COSTS.get().is_none());
    }

    #[test]
    fn unwind_and_disabled_observation_leave_no_thread_state() {
        assert!(std::panic::catch_unwind(|| measure(true, || panic!("fixture unwind"))).is_err());
        assert!(COSTS.get().is_none());
        let (result, costs) = measure(false, || {
            drop(Span::reserve(0));
            7
        });
        assert_eq!(result, 7);
        assert_eq!(costs.reserve_calls, 0);
        assert!(COSTS.get().is_none());
    }
}
