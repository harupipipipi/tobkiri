//! Bounded trusted Core control storage, separate from Pack resource authority.
//!
//! Callers admit control records before constructing them and retain the permit
//! through their actual lifetime. Large guest arguments, SQL text, ABI values
//! and output buffers must retain their own CodeOwner accounting instead.
//! These conservative control charges are not a whole-process RSS guarantee.

use std::fmt;
use std::sync::{
    Arc, OnceLock,
    atomic::{AtomicU64, Ordering},
};

const KINDS: usize = 8;
const MAX_COUNT: u64 = 1_048_576;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(usize)]
pub enum ControlKind {
    Load,
    Catalog,
    Context,
    Frame,
    Task,
    Waiter,
    Registration,
    Queue,
}
impl ControlKind {
    // Exhaustive rather than a discriminant cast: a new variant requires an
    // explicit layout decision instead of silently indexing beyond the array.
    const fn index(self) -> usize {
        match self {
            Self::Load => 0,
            Self::Catalog => 1,
            Self::Context => 2,
            Self::Frame => 3,
            Self::Task => 4,
            Self::Waiter => 5,
            Self::Registration => 6,
            Self::Queue => 7,
        }
    }
}

/// Internal runtime safety limits, never a Profile permission or guest grant.
#[derive(Clone, Copy, Debug)]
pub struct CoreControlLimits {
    pub total_bytes: u64,
    pub max_loads: u64,
    pub max_catalogs: u64,
    pub max_contexts: u64,
    pub max_frames: u64,
    pub max_tasks: u64,
    pub max_waiters: u64,
    pub max_registrations: u64,
    pub max_queue_entries: u64,
}
impl Default for CoreControlLimits {
    fn default() -> Self {
        Self {
            total_bytes: 64 * 1024 * 1024,
            max_loads: 4,
            max_catalogs: 16,
            max_contexts: 4096,
            max_frames: 8192,
            max_tasks: 256,
            max_waiters: 4096,
            max_registrations: 256,
            max_queue_entries: 2048,
        }
    }
}
impl CoreControlLimits {
    fn counts(self) -> [u64; KINDS] {
        [
            self.max_loads,
            self.max_catalogs,
            self.max_contexts,
            self.max_frames,
            self.max_tasks,
            self.max_waiters,
            self.max_registrations,
            self.max_queue_entries,
        ]
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CoreControlError {
    Invalid,
    Limit,
}
impl fmt::Display for CoreControlError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Invalid => "invalid Core control reservation",
            Self::Limit => "Core control capacity exhausted",
        })
    }
}
impl std::error::Error for CoreControlError {}

#[derive(Debug)]
struct ControlState {
    bytes_limit: u64,
    counts_limit: [u64; KINDS],
    bytes: AtomicU64,
    counts: [AtomicU64; KINDS],
}

/// One account is shared by a Core and its retained catalogs/executions.
/// Cloning the account creates no capacity and allocates no control record.
#[derive(Clone, Debug)]
pub struct CoreControl {
    state: Arc<ControlState>,
}
impl Default for CoreControl {
    fn default() -> Self {
        Self::new(CoreControlLimits::default()).expect("valid fixed Core control defaults")
    }
}
impl CoreControl {
    pub fn new(limits: CoreControlLimits) -> Result<Self, CoreControlError> {
        let counts_limit = limits.counts();
        if limits.total_bytes == 0 || counts_limit.iter().any(|n| *n > MAX_COUNT) {
            return Err(CoreControlError::Invalid);
        }
        Ok(Self {
            state: Arc::new(ControlState {
                bytes_limit: limits.total_bytes,
                counts_limit,
                bytes: AtomicU64::new(0),
                counts: std::array::from_fn(|_| AtomicU64::new(0)),
            }),
        })
    }

    /// Reserve bounded metadata and one category slot BEFORE its construction.
    /// Failure rolls back this attempt's provisional category slot. Concurrent
    /// attempts may observe that slot and receive temporary backpressure.
    /// This confers no execution right and promises no admission fairness.
    pub fn reserve(
        &self,
        kind: ControlKind,
        bytes: u64,
    ) -> Result<ControlReservation, CoreControlError> {
        if bytes == 0 {
            return Err(CoreControlError::Invalid);
        }
        let index = kind.index();
        self.state.counts[index]
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |n| {
                n.checked_add(1)
                    .filter(|next| *next <= self.state.counts_limit[index])
            })
            .map_err(|_| CoreControlError::Limit)?;
        if self
            .state
            .bytes
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |n| {
                n.checked_add(bytes)
                    .filter(|next| *next <= self.state.bytes_limit)
            })
            .is_err()
        {
            self.state.counts[index].fetch_sub(1, Ordering::AcqRel);
            return Err(CoreControlError::Limit);
        }
        Ok(ControlReservation {
            state: self.state.clone(),
            kind,
            bytes,
        })
    }

    /// Non-atomic diagnostic snapshot; reserve alone decides capacity.
    pub fn usage(&self) -> CoreControlUsage {
        CoreControlUsage {
            bytes: self.state.bytes.load(Ordering::Acquire),
            counts: std::array::from_fn(|i| self.state.counts[i].load(Ordering::Acquire)),
        }
    }

    /// Compare native funding identity without creating a record or authority.
    pub fn same_account(&self, other: &Self) -> bool {
        Arc::ptr_eq(&self.state, &other.state)
    }
}

/// Shared compatibility account for legacy/test constructors. Production Core
/// injects its own account into GateEngine and CallCx roots.
pub(crate) fn default_control_account() -> CoreControl {
    static DEFAULT: OnceLock<CoreControl> = OnceLock::new();
    DEFAULT.get_or_init(CoreControl::default).clone()
}

impl From<CoreControlError> for crate::GateError {
    fn from(error: CoreControlError) -> Self {
        match error {
            CoreControlError::Invalid => Self::Invalid(error.to_string()),
            CoreControlError::Limit => Self::Limit(error.to_string()),
        }
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct CoreControlUsage {
    pub bytes: u64,
    counts: [u64; KINDS],
}
impl CoreControlUsage {
    pub fn count(self, kind: ControlKind) -> u64 {
        self.counts[kind.index()]
    }
}

/// Move-only lifetime custody. Cancellation alone is not a reason to drop this
/// permit while native work or a retained record still needs the control slot.
pub struct ControlReservation {
    state: Arc<ControlState>,
    kind: ControlKind,
    bytes: u64,
}
impl ControlReservation {
    pub fn bytes(&self) -> u64 {
        self.bytes
    }
    pub fn kind(&self) -> ControlKind {
        self.kind
    }

    /// Prepay additional storage in the SAME account and category record.
    /// A source catalog can grow its retained index without creating another
    /// Load/Catalog slot or a second byte allowance. Call BEFORE allocating
    /// the additional storage; denial leaves this reservation unchanged.
    ///
    /// There is deliberately no partial refund: all admitted storage must be
    /// destroyed before this move-only reservation is finally dropped.
    pub fn try_grow(&mut self, additional_bytes: u64) -> Result<(), CoreControlError> {
        if additional_bytes == 0 {
            return Err(CoreControlError::Invalid);
        }
        let next = self
            .bytes
            .checked_add(additional_bytes)
            .ok_or(CoreControlError::Limit)?;
        self.state
            .bytes
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |bytes| {
                bytes
                    .checked_add(additional_bytes)
                    .filter(|total| *total <= self.state.bytes_limit)
            })
            .map_err(|_| CoreControlError::Limit)?;
        self.bytes = next;
        Ok(())
    }
}
impl fmt::Debug for ControlReservation {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ControlReservation")
            .field("kind", &self.kind)
            .field("bytes", &self.bytes)
            .finish()
    }
}
impl Drop for ControlReservation {
    fn drop(&mut self) {
        self.state.bytes.fetch_sub(self.bytes, Ordering::AcqRel);
        self.state.counts[self.kind.index()].fetch_sub(1, Ordering::AcqRel);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn growing_a_retained_catalog_uses_the_same_slot_and_byte_account() {
        let control = CoreControl::new(CoreControlLimits {
            total_bytes: 100,
            max_catalogs: 1,
            ..CoreControlLimits::default()
        })
        .unwrap();
        let mut catalog = control.reserve(ControlKind::Catalog, 20).unwrap();
        catalog.try_grow(60).unwrap();
        assert_eq!(catalog.bytes(), 80);
        assert_eq!(catalog.kind(), ControlKind::Catalog);
        assert_eq!(control.usage().bytes, 80);
        assert_eq!(control.usage().count(ControlKind::Catalog), 1);
        assert!(matches!(
            control.reserve(ControlKind::Catalog, 1),
            Err(CoreControlError::Limit)
        ));
        let other = control.reserve(ControlKind::Task, 20).unwrap();
        assert_eq!(catalog.try_grow(1), Err(CoreControlError::Limit));
        assert_eq!(catalog.bytes(), 80);
        assert_eq!(control.usage().bytes, 100);
        assert_eq!(control.usage().count(ControlKind::Catalog), 1);
        drop(other);
        catalog.try_grow(20).unwrap();
        assert_eq!(control.usage().bytes, 100);
        drop(catalog);
        assert_eq!(control.usage().bytes, 0);
        assert_eq!(control.usage().count(ControlKind::Catalog), 0);
    }

    #[test]
    fn invalid_or_overflowing_growth_retains_the_original_charge() {
        let control = CoreControl::new(CoreControlLimits {
            total_bytes: u64::MAX,
            ..CoreControlLimits::default()
        })
        .unwrap();
        let mut load = control.reserve(ControlKind::Load, u64::MAX - 1).unwrap();
        assert_eq!(load.try_grow(0), Err(CoreControlError::Invalid));
        assert_eq!(load.try_grow(2), Err(CoreControlError::Limit));
        assert_eq!(load.bytes(), u64::MAX - 1);
        assert_eq!(control.usage().bytes, u64::MAX - 1);
        load.try_grow(1).unwrap();
        assert_eq!(control.usage().bytes, u64::MAX);
        drop(load);
        assert_eq!(control.usage().bytes, 0);
        assert_eq!(control.usage().count(ControlKind::Load), 0);
    }

    #[test]
    fn concurrent_growth_cannot_create_a_second_byte_allowance() {
        let control = CoreControl::new(CoreControlLimits {
            total_bytes: 100,
            max_catalogs: 2,
            ..CoreControlLimits::default()
        })
        .unwrap();
        let barrier = Arc::new(std::sync::Barrier::new(2));
        let workers = (0..2)
            .map(|_| {
                let mut reservation = control.reserve(ControlKind::Catalog, 20).unwrap();
                let barrier = barrier.clone();
                std::thread::spawn(move || {
                    barrier.wait();
                    let result = reservation.try_grow(40);
                    (reservation, result)
                })
            })
            .collect::<Vec<_>>();
        let records = workers
            .into_iter()
            .map(|worker| worker.join().unwrap())
            .collect::<Vec<_>>();
        assert_eq!(
            records.iter().filter(|(_, result)| result.is_ok()).count(),
            1
        );
        assert_eq!(control.usage().bytes, 80);
        assert_eq!(control.usage().count(ControlKind::Catalog), 2);
        assert_eq!(
            records.iter().map(|(held, _)| held.bytes()).sum::<u64>(),
            80
        );
        drop(records);
        assert_eq!(control.usage().bytes, 0);
        assert_eq!(control.usage().count(ControlKind::Catalog), 0);
    }

    #[test]
    fn admission_denial_rolls_back_category_and_byte_capacity() {
        let control = CoreControl::new(CoreControlLimits {
            total_bytes: 100,
            max_contexts: 1,
            ..CoreControlLimits::default()
        })
        .unwrap();
        let held = control.reserve(ControlKind::Context, 80).unwrap();
        assert!(matches!(
            control.reserve(ControlKind::Context, 1),
            Err(CoreControlError::Limit)
        ));
        assert!(matches!(
            control.reserve(ControlKind::Task, 21),
            Err(CoreControlError::Limit)
        ));
        assert_eq!(control.usage().count(ControlKind::Task), 0);
        assert_eq!(control.usage().bytes, 80);
        let task = control.reserve(ControlKind::Task, 20).unwrap();
        assert_eq!(control.usage().bytes, 100);
        drop(held);
        drop(task);
        assert_eq!(control.usage().bytes, 0);
    }
    #[test]
    fn account_clones_share_capacity_and_retained_records_keep_their_slot() {
        let control = CoreControl::new(CoreControlLimits {
            max_catalogs: 1,
            ..CoreControlLimits::default()
        })
        .unwrap();
        let clone = control.clone();
        let held = control.reserve(ControlKind::Catalog, 128).unwrap();
        drop(control);
        assert!(matches!(
            clone.reserve(ControlKind::Catalog, 1),
            Err(CoreControlError::Limit)
        ));
        drop(held);
        assert!(clone.reserve(ControlKind::Catalog, 1).is_ok());
    }
    #[test]
    fn concurrent_admission_never_exceeds_shared_control_capacity() {
        let control = CoreControl::new(CoreControlLimits {
            total_bytes: 32,
            max_frames: 4,
            ..CoreControlLimits::default()
        })
        .unwrap();
        let acquired = std::sync::Barrier::new(9);
        let release = std::sync::Barrier::new(9);
        std::thread::scope(|scope| {
            for _ in 0..8 {
                let control = &control;
                let acquired = &acquired;
                let release = &release;
                scope.spawn(move || {
                    let lease = control.reserve(ControlKind::Frame, 8).ok();
                    acquired.wait();
                    release.wait();
                    drop(lease);
                });
            }
            acquired.wait();
            let usage = control.usage();
            release.wait();
            assert_eq!(usage.count(ControlKind::Frame), 4);
            assert_eq!(usage.bytes, 32);
        });
        assert_eq!(control.usage().bytes, 0);
        assert_eq!(control.usage().count(ControlKind::Frame), 0);
    }
    #[test]
    fn invalid_sizes_and_disabled_categories_do_not_consume_capacity() {
        assert!(
            CoreControl::new(CoreControlLimits {
                total_bytes: 0,
                ..CoreControlLimits::default()
            })
            .is_err()
        );
        let control = CoreControl::new(CoreControlLimits {
            max_tasks: 0,
            ..CoreControlLimits::default()
        })
        .unwrap();
        assert!(matches!(
            control.reserve(ControlKind::Context, 0),
            Err(CoreControlError::Invalid)
        ));
        assert!(matches!(
            control.reserve(ControlKind::Task, 1),
            Err(CoreControlError::Limit)
        ));
        assert_eq!(control.usage().bytes, 0);
    }
}
