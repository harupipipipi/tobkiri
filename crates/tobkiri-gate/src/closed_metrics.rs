//! Opt-in numeric observations, never authority or a physical cleanup receipt.
//!
//! A request prepays one fixed bank and terminal encoder scratch before either
//! is constructed. Context descendants retain that same bank. TLS is entered
//! only for synchronous calls; it is never held across an async suspension.

use crate::{ControlKind, ControlReservation, CoreControl, CoreControlError};
use std::{
    cell::RefCell,
    marker::PhantomData,
    rc::Rc,
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    time::Instant,
};

pub const SLOT_COUNT: usize = 24;
pub const ENCODER_BYTES: usize = 8192;

/// Closed slot identities. Unimplemented/reserved slots remain unobserved.
#[derive(Clone, Copy, Debug)]
#[repr(usize)]
pub enum Metric {
    RequestWall = 0,
    ApprovalWait = 1,
    AllocationMetered = 2,
    AllocationConversion = 3,
    GuardFaultEpochSelect = 4,
    GuardFaultSelect = 5,
    GuardEpochSelect = 6,
    GuardPackPermissionsSelect = 7,
    GuardUserPermissionsSelect = 8,
    ApprovalDbLockWait = 9,
    AuditDbLockWait = 11,
    AuditImageScan = 12,
}

#[derive(Default)]
struct Slot {
    started: AtomicU64,
    returned: AtomicU64,
    dropped: AtomicU64,
    elapsed_ns: AtomicU64,
    max_ns: AtomicU64,
    units0: AtomicU64,
    units1: AtomicU64,
}

struct Bank {
    slots: [Slot; SLOT_COUNT],
    observed: AtomicU64,
    overflow: AtomicBool,
    control: CoreControl,
    _funding: ControlReservation,
}

/// Numeric-only shared observation; cloning allocates nothing and grants nothing.
#[derive(Clone)]
pub struct Observation(Arc<Bank>);

impl std::fmt::Debug for Observation {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Observation(..)")
    }
}

impl Observation {
    /// Includes the Arc header and one fixed terminal encoding buffer. It is a
    /// conservative control charge, not a claim about process RSS or CPU cost.
    pub const fn reserved_bytes() -> u64 {
        (std::mem::size_of::<Bank>()
            + 2 * std::mem::size_of::<usize>()
            + std::mem::size_of::<Snapshot>()
            + ENCODER_BYTES) as u64
    }

    pub fn try_new(control: &CoreControl) -> Result<Self, CoreControlError> {
        let funding = control.reserve(ControlKind::Context, Self::reserved_bytes())?;
        Ok(Self(Arc::new(Bank {
            slots: std::array::from_fn(|_| Slot::default()),
            observed: AtomicU64::new(0),
            overflow: AtomicBool::new(false),
            control: control.clone(),
            _funding: funding,
        })))
    }

    pub(crate) fn uses_control(&self, control: &CoreControl) -> bool {
        self.0.control.same_account(control)
    }

    pub fn span(&self, metric: Metric) -> Span {
        self.0
            .observed
            .fetch_or(1 << metric as usize, Ordering::Relaxed);
        self.add(&self.0.slots[metric as usize].started, 1);
        Span {
            observation: Some(self.clone()),
            metric,
            start: Instant::now(),
            returned: false,
        }
    }

    fn add(&self, counter: &AtomicU64, n: u64) {
        if counter
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |old| {
                old.checked_add(n)
            })
            .is_err()
        {
            self.0.overflow.store(true, Ordering::Release);
        }
    }

    /// A non-atomic terminal snapshot. Live children may still update the bank;
    /// in-flight counts and dropped spans are not physical quiescence evidence.
    pub fn snapshot(&self) -> Snapshot {
        let slots = std::array::from_fn(|index| {
            let slot = &self.0.slots[index];
            SlotSnapshot {
                started: slot.started.load(Ordering::Acquire),
                returned: slot.returned.load(Ordering::Acquire),
                dropped: slot.dropped.load(Ordering::Acquire),
                elapsed_ns: slot.elapsed_ns.load(Ordering::Acquire),
                max_ns: slot.max_ns.load(Ordering::Acquire),
                units0: slot.units0.load(Ordering::Acquire),
                units1: slot.units1.load(Ordering::Acquire),
            }
        });
        Snapshot {
            observed_mask: self.0.observed.load(Ordering::Acquire),
            overflow: self.0.overflow.load(Ordering::Acquire),
            slots,
        }
    }
}

thread_local! {
    static CURRENT: RefCell<Option<Observation>> = const { RefCell::new(None) };
}

/// This guard cannot move to an async worker. Use only in synchronous functions.
pub struct SyncScope {
    previous: Option<Observation>,
    _same_thread: PhantomData<Rc<()>>,
}

/// `None` deliberately clears any caller's observation during this sync scope.
pub fn enter_sync(observation: Option<&Observation>) -> SyncScope {
    let previous = CURRENT.with(|current| current.replace(observation.cloned()));
    SyncScope {
        previous,
        _same_thread: PhantomData,
    }
}

impl Drop for SyncScope {
    fn drop(&mut self) {
        CURRENT.with(|current| {
            current.replace(self.previous.take());
        });
    }
}

/// Synchronous Store hooks use only the bank explicitly installed by Core/Gate.
pub fn current_span(metric: Metric) -> Span {
    CURRENT.with(|current| span(current.borrow().as_ref(), metric))
}

pub fn span(observation: Option<&Observation>, metric: Metric) -> Span {
    observation.map_or_else(
        || Span {
            observation: None,
            metric,
            start: Instant::now(),
            returned: false,
        },
        |observation| observation.span(metric),
    )
}

/// Fixed stack timer. `finish` means the operation returned, including Err; it
/// never asserts success. Drop without finish is explicitly counted separately.
pub struct Span {
    observation: Option<Observation>,
    metric: Metric,
    start: Instant,
    returned: bool,
}

impl Span {
    pub fn add_units(&self, units0: u64, units1: u64) {
        if let Some(observation) = &self.observation {
            let slot = &observation.0.slots[self.metric as usize];
            observation.add(&slot.units0, units0);
            observation.add(&slot.units1, units1);
        }
    }

    pub fn finish(mut self) {
        self.returned = true;
    }

    /// Wraps a synchronous operation without changing its Result or panic.
    pub fn measure<T>(mut self, f: impl FnOnce() -> T) -> T {
        let result = f();
        self.returned = true;
        result
    }
}

impl Drop for Span {
    fn drop(&mut self) {
        if let Some(observation) = &self.observation {
            let slot = &observation.0.slots[self.metric as usize];
            let elapsed = match u64::try_from(self.start.elapsed().as_nanos()) {
                Ok(elapsed) => elapsed,
                Err(_) => {
                    observation.0.overflow.store(true, Ordering::Release);
                    return;
                }
            };
            observation.add(&slot.elapsed_ns, elapsed);
            slot.max_ns.fetch_max(elapsed, Ordering::Relaxed);
            observation.add(
                if self.returned {
                    &slot.returned
                } else {
                    &slot.dropped
                },
                1,
            );
        }
    }
}

#[derive(Clone, Copy, Debug, Default)]
pub struct SlotSnapshot {
    pub started: u64,
    pub returned: u64,
    pub dropped: u64,
    pub elapsed_ns: u64,
    pub max_ns: u64,
    pub units0: u64,
    pub units1: u64,
}

#[derive(Clone, Copy, Debug)]
pub struct Snapshot {
    pub observed_mask: u64,
    pub overflow: bool,
    pub slots: [SlotSnapshot; SLOT_COUNT],
}

impl Snapshot {
    /// Fixed schema, no dynamic labels, error text, SQL, Pack/request identities
    /// or payloads. Unknown/reserved/overflowed slots serialize as null.
    pub fn write_json(
        &self,
        id: u64,
        request_ok: bool,
        output: &mut impl std::io::Write,
    ) -> std::io::Result<()> {
        write!(
            output,
            "{{\"kind\":\"request_metrics\",\"format\":\"tobkiri.closed-request-metrics/1\",\"id\":{id},\"request_ok\":{request_ok},\"all_audit_attributed\":false,\"terminal_outcome_covered\":false,\"physical_cleanup_proven\":false,\"overflow\":{},\"observed_mask\":{},\"slots\":[",
            self.overflow, self.observed_mask
        )?;
        for (index, slot) in self.slots.iter().enumerate() {
            if index != 0 {
                output.write_all(b",")?;
            }
            if self.overflow || self.observed_mask & (1 << index) == 0 {
                output.write_all(b"null")?;
            } else {
                write!(
                    output,
                    "[{},{},{},{},{},{},{}]",
                    slot.started,
                    slot.returned,
                    slot.dropped,
                    slot.elapsed_ns,
                    slot.max_ns,
                    slot.units0,
                    slot.units1
                )?;
            }
        }
        output.write_all(b"]}\n")
    }
}

#[cfg(test)]
#[path = "closed_metrics_tests.rs"]
mod tests;
