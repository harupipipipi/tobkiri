//! Notification custody only. These records never certify physical cleanup,
//! terminal delivery, authority or a cleanup winner.
use crate::funded_arc::{FundedArc, FundedWeak, Funding, new_funding};
use std::{
    alloc::Layout,
    sync::{
        Arc, Mutex, MutexGuard,
        atomic::{AtomicBool, AtomicU8, AtomicUsize, Ordering},
    },
};
use tobkiri_canon::{Digest, ids::UiSessionId};
use tobkiri_flow::{StepError, StreamCloseControl, StreamCloseReason};
use tobkiri_gate::{CancellationToken, ControlKind, CoreControl, GateError};

const MAX_OPERATIONS: usize = 1024;
pub(crate) const MAX_SOURCES: usize = 64;
const REASON_MASK: u8 = 7;
const RETIRED: u8 = 128;
// Durable original-token wake duty inside the same funded record. Published
// before its accepted cause is observable; only DONE completes it.
const WAKE_NONE: u8 = 0;
const WAKE_PENDING: u8 = 1;
const WAKE_INFLIGHT: u8 = 2;
const WAKE_DONE: u8 = 3;
const WAKE_UNKNOWN: u8 = 4;

fn code(reason: StreamCloseReason) -> u8 {
    match reason {
        StreamCloseReason::Manual => 1,
        StreamCloseReason::Deadline => 2,
        StreamCloseReason::Shutdown => 3,
        StreamCloseReason::Retired => 4,
        StreamCloseReason::Unknown => 5,
    }
}
fn reason(word: u8) -> Option<StreamCloseReason> {
    match word & REASON_MASK {
        1 => Some(StreamCloseReason::Manual),
        2 => Some(StreamCloseReason::Deadline),
        3 => Some(StreamCloseReason::Shutdown),
        4 => Some(StreamCloseReason::Retired),
        5 => Some(StreamCloseReason::Unknown),
        _ => None,
    }
}
fn unavailable() -> StepError {
    StepError::new("unavailable", "UI notification custody remains unresolved")
}

// Rust 1.94 ArcInner is repr(C, align(2)), with two AtomicUsize
// counters followed by T. Calculate its actual alignment/padding, rather
// than assuming size_of<T> includes the separately allocated Arc header.
pub(crate) fn arc_owned_bytes<T>() -> Result<usize, GateError> {
    Layout::new::<[AtomicUsize; 2]>()
        .extend(Layout::new::<T>())
        .map(|(layout, _)| layout.pad_to_align().size())
        .map_err(|_| GateError::Unavailable)
}
// Pinned Rust 1.94 std pthread Mutex uses one OnceBox<pal::Mutex>.
// libc 0.2.190 Darwin64 pal layout is c_long(8)+opaque[56], align 8.
// Linux and non-win7 Windows select the futex mutex: SmallFutex is an inline
// Atomic<u32>/Atomic<u8>, respectively. No separately owned pal allocation.
// Other target backends remain closed until their pinned source is verified.
fn mutex_heap_bytes() -> Result<usize, GateError> {
    if cfg!(all(target_os = "macos", target_pointer_width = "64")) {
        return Ok(64);
    }
    if cfg!(all(
        any(target_pointer_width = "32", target_pointer_width = "64"),
        any(
            target_os = "linux",
            all(target_os = "windows", not(target_vendor = "win7"))
        ),
    )) {
        return Ok(0);
    }
    Err(GateError::Unavailable)
}
// Call only with a new, uniquely owned, unpublished mutex, after funding its
// backing layout. This single first try_lock initializes OnceBox before any
// contender exists; subsequent use cannot create racing loser Boxes.
fn warm_new_mutex<T>(mutex: &Mutex<T>) -> Result<(), GateError> {
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let guard = mutex.try_lock().map_err(|_| GateError::Unavailable)?;
        drop(guard);
        Ok(())
    }))
    .unwrap_or(Err(GateError::Unavailable))
}

/// Provenance from the actual Core UI registration. This is neither a
/// per-operation first-reason ordering nor authority/physical/ACK evidence.
pub(crate) struct UiScopeRetirement {
    retired: AtomicBool,
}
impl UiScopeRetirement {
    pub(crate) fn new(control: &CoreControl) -> Result<FundedArc<Self>, GateError> {
        let bytes = u64::try_from(
            arc_owned_bytes::<Self>()?
                .checked_add(Funding::storage_bytes())
                .ok_or(GateError::Unavailable)?
                .checked_add(256)
                .ok_or(GateError::Unavailable)?,
        )
        .map_err(|_| GateError::Unavailable)?;
        let funding = new_funding(control.reserve(ControlKind::Registration, bytes)?)?;
        Ok(FundedArc::new(
            Self {
                retired: AtomicBool::new(false),
            },
            funding,
        ))
    }
    pub(crate) fn retire(&self) {
        self.retired.store(true, Ordering::Release);
    }
    fn retired(&self) -> bool {
        self.retired.load(Ordering::Acquire)
    }
}

/// Factory-owned handle, separate from authentication. The process owner pins
/// its anchor before this handle may accept any operation/source/intent.
/// Drop only records retirement; it performs no allocation, callback or wake.
pub(crate) struct UiDeliveryOwner {
    anchor: FundedArc<UiDeliveryAnchor>,
}
impl UiDeliveryOwner {
    pub(crate) fn new(factory: &Digest, control: &CoreControl) -> Result<Self, GateError> {
        Self::prepare(factory, control, warm_new_mutex)
    }
    // The initializer is a private construction/test seam, never a guest or
    // source callback. Production passes only warm_new_mutex above.
    fn prepare(
        factory: &Digest,
        control: &CoreControl,
        mut initialize: impl FnMut(&Mutex<Option<FundedArc<UiDelivery>>>) -> Result<(), GateError>,
    ) -> Result<Self, GateError> {
        let slot_layout = std::mem::size_of::<Mutex<Option<FundedArc<UiDelivery>>>>()
            .checked_add(mutex_heap_bytes()?)
            .ok_or(GateError::Unavailable)?;
        let bytes = arc_owned_bytes::<UiDeliveryAnchor>()?
            .checked_add(Funding::storage_bytes())
            .ok_or(GateError::Unavailable)?
            .checked_add(
                MAX_OPERATIONS
                    .checked_mul(slot_layout)
                    .ok_or(GateError::Unavailable)?,
            )
            .and_then(|bytes| bytes.checked_add(1024))
            .and_then(|bytes| u64::try_from(bytes).ok())
            .ok_or(GateError::Unavailable)?;
        let funding = new_funding(control.reserve(ControlKind::Registration, bytes)?)?;
        let mut slots = Vec::new();
        slots
            .try_reserve_exact(MAX_OPERATIONS)
            .map_err(|_| GateError::Unavailable)?;
        if slots.capacity() != MAX_OPERATIONS {
            return Err(GateError::Unavailable);
        }
        slots.resize_with(MAX_OPERATIONS, || Mutex::new(None));
        // All allocations, including partially initialized pal Boxes, drop
        // before the earlier reservation on every early error or unwind.
        for slot in &slots {
            initialize(slot)?;
        }
        let anchor = FundedArc::new(
            UiDeliveryAnchor {
                factory: factory.clone(),
                retired: AtomicBool::new(false),
                slots,
            },
            funding,
        );
        crate::tracked_stream::register_ui_notifications(anchor.clone())?;
        Ok(Self { anchor })
    }
    pub(crate) fn weak(&self) -> FundedWeak<UiDeliveryAnchor> {
        self.anchor.downgrade()
    }
    pub(crate) fn retire_ui(&self, ui: &UiSessionId) {
        // The actual principal already published its immutable carrier under
        // the UI removal lock. A busy slot does not lose that accepted scope
        // retirement. Do not infer or invent per-operation first-reason order.
        for slot in &self.anchor.slots {
            let delivery = match self.anchor.slot(slot) {
                Ok(slot) => slot.clone(),
                Err(_) => None,
            };
            if let Some(delivery) = delivery
                && &delivery.ui == ui
            {
                let _ = delivery.flush();
            }
        }
    }
    pub(crate) fn retire(&self) {
        self.anchor.retire();
    }
    /// Caller holds no outer guard. This is not physical cleanup or ACK.
    pub(crate) fn close(&self) {
        self.anchor.retire();
        let _ = self.anchor.flush();
    }
}
impl Drop for UiDeliveryOwner {
    fn drop(&mut self) {
        self.anchor.retire();
    }
}

/// No provider/auth/Factory strong edge. Actual source companions may retain
/// provider frames, whose only edge back to this anchor is Weak.
pub(crate) struct UiDeliveryAnchor {
    pub(crate) factory: Digest,
    retired: AtomicBool,
    slots: Vec<Mutex<Option<FundedArc<UiDelivery>>>>,
}
/// Every source-index unlock rechecks retirement. A close that lost its
/// try-lock either precedes this post-unlock check or acquires the unlocked
/// slot itself. Only atomic receipt occurs here; never callback/token wake.
struct SlotGuard<'a> {
    guard: Option<MutexGuard<'a, Option<FundedArc<UiDelivery>>>>,
    retired: &'a AtomicBool,
}
impl std::ops::Deref for SlotGuard<'_> {
    type Target = Option<FundedArc<UiDelivery>>;
    fn deref(&self) -> &Self::Target {
        self.guard.as_ref().expect("live slot")
    }
}
impl std::ops::DerefMut for SlotGuard<'_> {
    fn deref_mut(&mut self) -> &mut Self::Target {
        self.guard.as_mut().expect("live slot")
    }
}
impl Drop for SlotGuard<'_> {
    fn drop(&mut self) {
        let delivery = self
            .guard
            .as_ref()
            .and_then(|guard| guard.as_ref().cloned());
        drop(self.guard.take());
        if self.retired.load(Ordering::Acquire)
            && let Some(delivery) = delivery.as_ref()
        {
            delivery.accept_word(StreamCloseReason::Shutdown);
        }
    }
}
impl UiDeliveryAnchor {
    fn slot<'a>(
        &'a self,
        slot: &'a Mutex<Option<FundedArc<UiDelivery>>>,
    ) -> Result<SlotGuard<'a>, GateError> {
        Ok(SlotGuard {
            guard: Some(slot.try_lock().map_err(|_| GateError::Unavailable)?),
            retired: &self.retired,
        })
    }
    pub(crate) fn retire(&self) {
        self.retired.store(true, Ordering::Release);
        for slot in &self.slots {
            let delivery = match self.slot(slot) {
                Ok(slot) => slot.clone(),
                Err(_) => None,
            };
            if let Some(delivery) = delivery {
                delivery.accept(StreamCloseReason::Shutdown);
            }
        }
    }
    /// The returned owner must outlive all authority guards at the call site.
    pub(crate) fn find(
        &self,
        token: &CancellationToken,
    ) -> Result<Option<FundedArc<UiDelivery>>, GateError> {
        for slot in &self.slots {
            let slot = self.slot(slot)?;
            if let Some(delivery) = slot.as_ref()
                && token.inherits(&delivery.token)
            {
                return Ok(Some(delivery.clone()));
            }
        }
        Ok(None)
    }
    /// Explicit outside-all-guards maintenance. A failed callback is never
    /// retried, and its source/funding stay in the process-owned anchor.
    pub(crate) fn flush(&self) -> Result<(), StepError> {
        let mut unresolved = false;
        for slot in &self.slots {
            let delivery = match self.slot(slot) {
                Ok(slot) => slot.clone(),
                Err(_) => {
                    unresolved = true;
                    continue;
                }
            };
            let Some(delivery) = delivery else { continue };
            if self.retired.load(Ordering::Acquire) {
                delivery.accept(StreamCloseReason::Shutdown);
            }
            if delivery.flush().is_err() {
                unresolved = true;
            }
            if delivery.released.load(Ordering::Acquire) && delivery.drain_sources() {
                let removed = match self.slot(slot) {
                    Ok(mut slot)
                        if slot
                            .as_ref()
                            .is_some_and(|value| FundedArc::ptr_eq(value, &delivery))
                            && delivery.released.load(Ordering::Acquire)
                            && delivery.drained.load(Ordering::Acquire) =>
                    {
                        slot.take()
                    }
                    _ => None,
                };
                // Source/capture destruction is outside all owner/source guards.
                drop(removed);
            }
        }
        if unresolved {
            Err(unavailable())
        } else {
            Ok(())
        }
    }
    /// Notification completion is independent of metadata removal. A live
    /// control may retain a fully settled record and its funding after close.
    /// Pending/in-flight/Unknown or lock uncertainty is never completion.
    /// This grants no physical cleanup or terminal-ACK proof and frees nothing.
    pub(crate) fn notifications_settled(&self) -> bool {
        self.retired.load(Ordering::Acquire)
            && self.slots.iter().all(|slot| {
                self.slot(slot)
                    .is_ok_and(|slot| slot.as_ref().is_none_or(|delivery| delivery.settled()))
            })
    }
    pub(crate) fn removable(&self) -> bool {
        self.retired.load(Ordering::Acquire)
            && self
                .slots
                .iter()
                .all(|slot| self.slot(slot).is_ok_and(|slot| slot.is_none()))
    }
}

impl FundedArc<UiDeliveryAnchor> {
    pub(crate) fn admit(
        &self,
        ui: UiSessionId,
        scope: Option<FundedArc<UiScopeRetirement>>,
        token: CancellationToken,
        external: tokio_util::sync::CancellationToken,
        funding: Funding,
    ) -> Result<FundedArc<UiDelivery>, GateError> {
        self.admit_prepared(ui, scope, token, external, funding, warm_new_mutex)
    }
    // The initializer is private construction logic. A new source-list mutex
    // is funded and initialized before its record or any source is published.
    #[allow(clippy::too_many_arguments)]
    fn admit_prepared(
        &self,
        ui: UiSessionId,
        scope: Option<FundedArc<UiScopeRetirement>>,
        token: CancellationToken,
        external: tokio_util::sync::CancellationToken,
        funding: Funding,
        initialize: impl FnOnce(&Mutex<Vec<FundedArc<SourceDelivery>>>) -> Result<(), GateError>,
    ) -> Result<FundedArc<UiDelivery>, GateError> {
        if self.retired.load(Ordering::Acquire) {
            return Err(GateError::Unavailable);
        }
        let mut sources = Vec::new();
        sources
            .try_reserve_exact(MAX_SOURCES)
            .map_err(|_| GateError::Unavailable)?;
        if sources.capacity() != MAX_SOURCES {
            return Err(GateError::Unavailable);
        }
        let sources = Mutex::new(sources);
        initialize(&sources)?;
        let delivery = FundedArc::new(
            UiDelivery {
                anchor: self.downgrade(),
                ui,
                scope,
                token,
                external,
                intent: AtomicU8::new(0),
                wake: AtomicU8::new(WAKE_NONE),
                released: AtomicBool::new(false),
                drained: AtomicBool::new(false),
                flushing: AtomicBool::new(false),
                sources,
            },
            funding,
        );
        for slot in &self.slots {
            let Ok(mut slot) = self.slot(slot) else {
                continue;
            };
            if slot.is_some() {
                continue;
            }
            if self.retired.load(Ordering::Acquire) {
                return Err(GateError::Unavailable);
            }
            *slot = Some(delivery.clone());
            drop(slot);
            // A retiring thread that saw this slot locked delegated receipt
            // to this publisher. A retirement after this check sees it unlocked.
            if self.retired.load(Ordering::Acquire) {
                delivery.accept(StreamCloseReason::Shutdown);
            }
            return Ok(delivery);
        }
        Err(GateError::Unavailable)
    }
}

pub(crate) struct UiDelivery {
    anchor: FundedWeak<UiDeliveryAnchor>,
    ui: UiSessionId,
    scope: Option<FundedArc<UiScopeRetirement>>,
    pub(crate) token: CancellationToken,
    pub(crate) external: tokio_util::sync::CancellationToken,
    intent: AtomicU8,
    wake: AtomicU8,
    released: AtomicBool,
    drained: AtomicBool,
    flushing: AtomicBool,
    sources: Mutex<Vec<FundedArc<SourceDelivery>>>,
}
struct SourceDelivery {
    source: Arc<dyn StreamCloseControl>,
    delivered: AtomicU8,
    in_flight: AtomicBool,
    unknown: AtomicBool,
}
/// No copied batch. The anchor retains the funded record and all captures;
/// this handoff is only a claim that may flush after every outer guard ends.
pub(crate) struct UiOperationBind {
    pub(crate) delivery: FundedArc<UiDelivery>,
}
impl UiOperationBind {
    pub(crate) fn flush(self) -> Result<(), StepError> {
        self.delivery.flush()
    }
}
impl UiDelivery {
    pub(crate) fn storage_bytes() -> Result<u64, GateError> {
        // The fixed Vec backing overlaps all 64 source Arcs; flush adds only
        // a refcount to an existing Arc, never a second allocated batch.
        // 4096 also covers a full bounded Weak-index split chain (Rust 1.94,
        // B=6, <=1024 keys, <=3 existing internal levels) before old nodes die.
        arc_owned_bytes::<Self>()?
            .checked_add(mutex_heap_bytes()?)
            .ok_or(GateError::Unavailable)?
            .checked_add(
                MAX_SOURCES
                    * (arc_owned_bytes::<SourceDelivery>()?
                        + std::mem::size_of::<FundedArc<SourceDelivery>>()
                        + 64),
            )
            .and_then(|bytes| bytes.checked_add(4096))
            .and_then(|bytes| u64::try_from(bytes).ok())
            .ok_or(GateError::Unavailable)
    }
    fn accept_word(&self, reason: StreamCloseReason) {
        // Durable wake duty publishes BEFORE the cause CAS, so no accepted
        // cause can ever exist without a pending wake in this record.
        let _ = self.wake.compare_exchange(
            WAKE_NONE,
            WAKE_PENDING,
            Ordering::AcqRel,
            Ordering::Acquire,
        );
        let retired = if matches!(
            reason,
            StreamCloseReason::Shutdown | StreamCloseReason::Retired
        ) {
            RETIRED
        } else {
            0
        };
        let mut word = self.intent.load(Ordering::Acquire);
        // Only two successful transitions can exist globally: first reason,
        // then retired bit. Strong CAS can therefore lose at most twice.
        for _ in 0..3 {
            let next = word
                | retired
                | if word & REASON_MASK == 0 {
                    code(reason)
                } else {
                    0
                };
            if next == word {
                return;
            }
            match self
                .intent
                .compare_exchange(word, next, Ordering::AcqRel, Ordering::Acquire)
            {
                Ok(_) => return,
                Err(observed) => word = observed,
            }
        }
    }
    pub(crate) fn accept(&self, reason: StreamCloseReason) {
        if self
            .anchor
            .upgrade()
            .is_some_and(|anchor| anchor.retired.load(Ordering::Acquire))
        {
            self.accept_word(StreamCloseReason::Shutdown);
        }
        self.accept_word(reason);
    }
    pub(crate) fn accepted(&self) -> (Option<StreamCloseReason>, bool) {
        if self
            .anchor
            .upgrade()
            .is_some_and(|anchor| anchor.retired.load(Ordering::Acquire))
        {
            self.accept_word(StreamCloseReason::Shutdown);
        }
        let word = self.intent.load(Ordering::Acquire);
        (
            reason(word),
            word & RETIRED != 0 || self.scope.as_ref().is_some_and(|scope| scope.retired()),
        )
    }
    pub(crate) fn release(&self) {
        self.released.store(true, Ordering::Release);
    }
    /// Claim and deliver the durable original-token wake exactly once. Only
    /// an unattempted pending duty runs; in-flight or Unknown never replay.
    pub(crate) fn deliver_wake(&self) {
        if self
            .wake
            .compare_exchange(
                WAKE_PENDING,
                WAKE_INFLIGHT,
                Ordering::AcqRel,
                Ordering::Acquire,
            )
            .is_err()
        {
            return;
        }
        let delivered = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            self.token.cancel();
            self.external.cancel();
        }))
        .is_ok();
        self.wake.store(
            if delivered { WAKE_DONE } else { WAKE_UNKNOWN },
            Ordering::Release,
        );
    }
    /// Release only notification references. Physical/terminal ownership is
    /// held independently by the original source owners and physical variant.
    /// No capture destruction occurs under an anchor or source-list guard.
    fn drain_sources(&self) -> bool {
        if !self.released.load(Ordering::Acquire) || !self.settled() {
            return false;
        }
        let sources = {
            let Ok(mut sources) = self.sources.try_lock() else {
                return false;
            };
            if self.flushing.load(Ordering::Acquire) {
                return false;
            }
            let word = self.intent.load(Ordering::Acquire);
            if sources.iter().any(|entry| {
                entry.in_flight.load(Ordering::Acquire)
                    || entry.unknown.load(Ordering::Acquire)
                    || (word != 0
                        && entry.delivered.load(Ordering::Acquire) != (word & REASON_MASK))
            }) {
                return false;
            }
            std::mem::take(&mut *sources)
        };
        drop(sources);
        self.drained.store(true, Ordering::Release);
        true
    }
    pub(crate) fn settled(&self) -> bool {
        if self.flushing.load(Ordering::Acquire) {
            return false;
        }
        self.accepted();
        let word = self.intent.load(Ordering::Acquire);
        // An accepted cause owes its original token wake: pending, in-flight
        // or Unknown wake duty is never notification completion.
        if word & REASON_MASK != 0 && self.wake.load(Ordering::Acquire) != WAKE_DONE {
            return false;
        }
        self.sources.try_lock().is_ok_and(|sources| {
            sources.iter().all(|entry| {
                !entry.in_flight.load(Ordering::Acquire)
                    && !entry.unknown.load(Ordering::Acquire)
                    && (word == 0
                        || entry.delivered.load(Ordering::Acquire) == (word & REASON_MASK))
            })
        })
    }
}

impl FundedArc<UiDelivery> {
    pub(crate) fn bind(
        &self,
        source: &Arc<dyn StreamCloseControl>,
    ) -> Result<UiOperationBind, StepError> {
        let mut sources = self.sources.try_lock().map_err(|_| unavailable())?;
        if self.released.load(Ordering::Acquire) || self.drained.load(Ordering::Acquire) {
            return Err(unavailable());
        }
        if !sources
            .iter()
            .any(|entry| Arc::ptr_eq(&entry.source, source))
        {
            if sources.len() >= MAX_SOURCES {
                return Err(unavailable());
            }
            sources.push(FundedArc::new(
                SourceDelivery {
                    source: source.clone(),
                    delivered: AtomicU8::new(0),
                    in_flight: AtomicBool::new(false),
                    unknown: AtomicBool::new(false),
                },
                self.funding(),
            ));
        }
        drop(sources);
        Ok(UiOperationBind {
            delivery: self.clone(),
        })
    }
    /// Holds no caller guard. An unattempted entry remains in its original
    /// owner on contention; each individual success is committed only AFTER
    /// its callback returns. Panic/Err retains Unknown and is never replayed.
    pub(crate) fn flush(&self) -> Result<(), StepError> {
        if self
            .flushing
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .is_err()
        {
            return Err(unavailable());
        }
        struct FlushGuard<'a>(&'a AtomicBool);
        impl Drop for FlushGuard<'_> {
            fn drop(&mut self) {
                self.0.store(false, Ordering::Release);
            }
        }
        let flushing = FlushGuard(&self.flushing);
        let mut failed = false;
        for index in 0..MAX_SOURCES {
            self.accepted();
            let word = self.intent.load(Ordering::Acquire);
            let Some(reason) = reason(word) else { break };
            let entry = {
                let sources = self.sources.try_lock().map_err(|_| unavailable())?;
                sources.get(index).cloned()
            };
            let Some(entry) = entry else { break };
            if entry.unknown.load(Ordering::Acquire) {
                failed = true;
                continue;
            }
            if entry.delivered.load(Ordering::Acquire) == (word & REASON_MASK) {
                continue;
            }
            entry.in_flight.store(true, Ordering::Release);
            match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                entry.source.record_close(reason)
            })) {
                Ok(Ok(())) => entry.delivered.store(word & REASON_MASK, Ordering::Release),
                Ok(Err(_)) | Err(_) => {
                    entry.unknown.store(true, Ordering::Release);
                    failed = true;
                }
            }
            entry.in_flight.store(false, Ordering::Release);
        }
        // Deliver the owed original-token wake inside the same outside-guard
        // duty pass; Unknown stays owned and blocks settled below.
        if self.intent.load(Ordering::Acquire) & REASON_MASK != 0 {
            self.deliver_wake();
        }
        drop(flushing);
        if failed || !self.settled() {
            Err(unavailable())
        } else {
            Ok(())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Barrier, atomic::AtomicUsize};
    use tobkiri_gate::{ControlReservation, CoreCancellation, CoreControlLimits};

    fn owner(control: &CoreControl, name: &[u8]) -> UiDeliveryOwner {
        UiDeliveryOwner::new(&Digest::hash(name), control).unwrap()
    }
    fn delivery(
        owner: &UiDeliveryOwner,
        control: &CoreControl,
        scope: Option<FundedArc<UiScopeRetirement>>,
    ) -> FundedArc<UiDelivery> {
        let reservation = new_funding(
            control
                .reserve(
                    ControlKind::Frame,
                    UiDelivery::storage_bytes().unwrap() + Funding::storage_bytes() as u64 + 1024,
                )
                .unwrap(),
        )
        .unwrap();
        owner
            .anchor
            .admit(
                UiSessionId::parse("ui_delivery").unwrap(),
                scope,
                CoreCancellation::new_with_control(control).unwrap(),
                tokio_util::sync::CancellationToken::new(),
                reservation,
            )
            .unwrap()
    }
    enum Behavior {
        Success,
        Refuse,
        Panic,
    }
    struct Sink {
        calls: AtomicUsize,
        owner: Mutex<Option<FundedWeak<UiDelivery>>>,
        behavior: Behavior,
        barriers: Option<(Arc<Barrier>, Arc<Barrier>)>,
        _reservation: ControlReservation,
    }
    fn sink(
        control: &CoreControl,
        behavior: Behavior,
        barriers: Option<(Arc<Barrier>, Arc<Barrier>)>,
    ) -> Arc<Sink> {
        let reservation = control.reserve(ControlKind::Frame, 1024).unwrap();
        Arc::new(Sink {
            calls: AtomicUsize::new(0),
            owner: Mutex::new(None),
            behavior,
            barriers,
            _reservation: reservation,
        })
    }
    impl StreamCloseControl for Sink {
        fn record_close(&self, _: StreamCloseReason) -> Result<(), StepError> {
            self.calls.fetch_add(1, Ordering::AcqRel);
            if let Some(owner) = self
                .owner
                .lock()
                .unwrap()
                .as_ref()
                .and_then(FundedWeak::upgrade)
            {
                assert!(owner.sources.try_lock().is_ok());
                let anchor = owner.anchor.upgrade().unwrap();
                assert!(anchor.slots.iter().all(|slot| slot.try_lock().is_ok()));
            }
            if let Some((entered, release)) = &self.barriers {
                entered.wait();
                release.wait();
            }
            match self.behavior {
                Behavior::Success => Ok(()),
                Behavior::Refuse => Err(StepError::new("unavailable", "deterministic refusal")),
                Behavior::Panic => panic!("deterministic notification panic"),
            }
        }
    }
    fn bind(owner: &FundedArc<UiDelivery>, sink: &Arc<Sink>) -> UiOperationBind {
        *sink.owner.lock().unwrap() = Some(owner.downgrade());
        owner
            .bind(&(sink.clone() as Arc<dyn StreamCloseControl>))
            .unwrap()
    }

    #[test]
    fn first_reason_is_concurrent_lock_independent_and_retired_is_sticky() {
        let control = CoreControl::default();
        let owner = owner(&control, b"first-reason");
        let record = delivery(&owner, &control, None);
        let source_guard = record.sources.lock().unwrap();
        let barrier = Arc::new(Barrier::new(9));
        let mut workers = Vec::new();
        for index in 0..8 {
            let record = record.clone();
            let barrier = barrier.clone();
            workers.push(std::thread::spawn(move || {
                barrier.wait();
                record.accept(if index % 2 == 0 {
                    StreamCloseReason::Manual
                } else {
                    StreamCloseReason::Retired
                });
                record.accepted().0.unwrap()
            }));
        }
        barrier.wait();
        let observed: Vec<_> = workers
            .into_iter()
            .map(|worker| worker.join().unwrap())
            .collect();
        let (first, retired) = record.accepted();
        assert!(retired);
        assert!(observed.iter().all(|reason| Some(*reason) == first));
        record.accept(StreamCloseReason::Shutdown);
        assert_eq!(record.accepted(), (first, true));
        drop(source_guard);
        record.release();
        owner.close();
        assert!(owner.anchor.removable());
    }

    #[test]
    fn contention_and_unwind_keep_batch_until_explicit_guard_free_delivery() {
        let control = CoreControl::default();
        let owner = owner(&control, b"unwind");
        let record = delivery(&owner, &control, None);
        let source = sink(&control, Behavior::Success, None);
        let handoff = bind(&record, &source);
        let source_guard = record.sources.lock().unwrap();
        record.accept(StreamCloseReason::Manual);
        assert!(record.flush().is_err());
        assert!(
            std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                let _handoff = handoff;
                panic!("deterministic early unwind");
            }))
            .is_err()
        );
        assert_eq!(source.calls.load(Ordering::Acquire), 0);
        drop(source_guard);
        record.flush().unwrap();
        record.flush().unwrap();
        assert_eq!(source.calls.load(Ordering::Acquire), 1);
        record.accept(StreamCloseReason::Retired);
        record.flush().unwrap();
        assert_eq!(record.accepted(), (Some(StreamCloseReason::Manual), true));
        assert_eq!(source.calls.load(Ordering::Acquire), 1);
        record.release();
        owner.close();
        assert!(owner.anchor.removable());
    }

    #[test]
    fn factory_drop_keeps_refused_or_panicked_sources_and_funding_without_replay() {
        for (name, behavior) in [
            (b"refused".as_slice(), Behavior::Refuse),
            (b"panicked".as_slice(), Behavior::Panic),
        ] {
            let control = CoreControl::default();
            let owner = owner(&control, name);
            let record = delivery(&owner, &control, None);
            let source = sink(&control, behavior, None);
            drop(bind(&record, &source));
            record.accept(StreamCloseReason::Manual);
            let weak_record = record.downgrade();
            let weak_source = Arc::downgrade(&source);
            let anchor = owner.weak();
            record.release();
            drop(record);
            drop(source);
            let funded = control.usage().bytes;
            drop(owner); // no callback, even though the last Factory owner left
            assert_eq!(control.usage().bytes, funded);
            let anchor = anchor.upgrade().unwrap();
            assert!(anchor.flush().is_err());
            assert!(anchor.flush().is_err());
            assert!(!anchor.removable());
            assert_eq!(
                weak_source.upgrade().unwrap().calls.load(Ordering::Acquire),
                1
            );
            assert_eq!(
                weak_record.upgrade().unwrap().accepted(),
                (Some(StreamCloseReason::Manual), true)
            );
            assert!(control.usage().bytes >= funded);
        }
    }

    #[test]
    fn inflight_factory_drop_cannot_remove_record_and_success_releases_once() {
        let control = CoreControl::default();
        let owner = owner(&control, b"inflight");
        let record = delivery(&owner, &control, None);
        let entered = Arc::new(Barrier::new(2));
        let release = Arc::new(Barrier::new(2));
        let source = sink(
            &control,
            Behavior::Success,
            Some((entered.clone(), release.clone())),
        );
        drop(bind(&record, &source));
        record.accept(StreamCloseReason::Manual);
        let anchor = owner.weak();
        let run = {
            let record = record.clone();
            std::thread::spawn(move || record.flush())
        };
        entered.wait();
        record.release();
        drop(owner);
        let anchor = anchor.upgrade().unwrap();
        assert!(anchor.flush().is_err());
        assert!(!anchor.removable());
        release.wait();
        run.join().unwrap().unwrap();
        anchor.flush().unwrap();
        assert!(anchor.removable());
        let charged = control.usage().bytes;
        anchor.flush().unwrap();
        assert_eq!(control.usage().bytes, charged);
        assert_eq!(source.calls.load(Ordering::Acquire), 1);
    }

    #[test]
    fn scope_retirement_survives_slot_contention_and_does_not_rewrite_first_reason() {
        let control = CoreControl::default();
        let owner = owner(&control, b"scope");
        let first = UiScopeRetirement::new(&control).unwrap();
        let other = UiScopeRetirement::new(&control).unwrap();
        let record = delivery(&owner, &control, Some(first.clone()));
        let unaffected = delivery(&owner, &control, Some(other.clone()));
        record.accept(StreamCloseReason::Manual);
        record.release();
        let slot = owner.anchor.slot(&owner.anchor.slots[0]).unwrap();
        first.retire();
        owner.retire_ui(&UiSessionId::parse("ui_delivery").unwrap());
        assert_eq!(record.accepted(), (Some(StreamCloseReason::Manual), true));
        assert_eq!(unaffected.accepted(), (None, false));
        let later = UiScopeRetirement::new(&control).unwrap();
        assert!(!later.retired());
        drop(slot);
        unaffected.release();
        owner.close();
        assert!(owner.anchor.removable());
    }

    #[test]
    fn scope_and_source_capacity_fail_before_publication_or_source_receipt() {
        let tight = CoreControl::new(CoreControlLimits {
            total_bytes: 1,
            ..Default::default()
        })
        .unwrap();
        assert!(UiScopeRetirement::new(&tight).is_err());
        assert_eq!(tight.usage().bytes, 0);
        let control = CoreControl::default();
        let owner = owner(&control, b"sources");
        let record = delivery(&owner, &control, None);
        let mut sources = Vec::new();
        for _ in 0..MAX_SOURCES {
            let source = sink(&control, Behavior::Success, None);
            drop(bind(&record, &source));
            sources.push(source);
        }
        let duplicate: Arc<dyn StreamCloseControl> = sources[0].clone();
        drop(record.bind(&duplicate).unwrap());
        let overflow = sink(&control, Behavior::Success, None);
        assert!(
            record
                .bind(&(overflow.clone() as Arc<dyn StreamCloseControl>))
                .is_err()
        );
        record.accept(StreamCloseReason::Manual);
        record.flush().unwrap();
        assert!(
            sources
                .iter()
                .all(|source| source.calls.load(Ordering::Acquire) == 1)
        );
        assert_eq!(overflow.calls.load(Ordering::Acquire), 0);
        record.release();
        owner.close();
        assert!(owner.anchor.removable());
    }

    #[test]
    fn anchor_reserve_failure_initializes_nothing_and_accepts_nothing() {
        // One byte short of the complete layout: the former sizeof-only
        // anchor charge could start construction here (including on futex
        // targets where the omitted Arc header, rather than pal Box, matters).
        let required = arc_owned_bytes::<UiDeliveryAnchor>().unwrap()
            + Funding::storage_bytes()
            + MAX_OPERATIONS
                * (std::mem::size_of::<Mutex<Option<FundedArc<UiDelivery>>>>()
                    + mutex_heap_bytes().unwrap())
            + 1024;
        let control = CoreControl::new(CoreControlLimits {
            total_bytes: u64::try_from(required - 1).unwrap(),
            ..Default::default()
        })
        .unwrap();
        let calls = AtomicUsize::new(0);
        let result =
            UiDeliveryOwner::prepare(&Digest::hash(b"unfunded-anchor"), &control, |slot| {
                calls.fetch_add(1, Ordering::AcqRel);
                warm_new_mutex(slot)
            });
        assert!(result.is_err());
        assert_eq!(calls.load(Ordering::Acquire), 0);
        assert_eq!(control.usage().bytes, 0);
        assert_eq!(control.usage().count(ControlKind::Registration), 0);
    }

    #[test]
    fn partial_anchor_initialization_error_and_unwind_remain_unpublished_and_refund_once() {
        for unwind in [false, true] {
            let control = CoreControl::default();
            let calls = AtomicUsize::new(0);
            let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                UiDeliveryOwner::prepare(&Digest::hash(b"partial-anchor"), &control, |slot| {
                    assert!(control.usage().bytes > 0);
                    assert_eq!(control.usage().count(ControlKind::Registration), 1);
                    warm_new_mutex(slot)?;
                    if calls.fetch_add(1, Ordering::AcqRel) == 7 {
                        if unwind {
                            panic!("deterministic private initialization unwind");
                        }
                        return Err(GateError::Unavailable);
                    }
                    Ok(())
                })
            }));
            match result {
                Ok(result) => {
                    assert!(!unwind);
                    assert!(result.is_err());
                }
                Err(_) => assert!(unwind),
            }
            assert_eq!(calls.load(Ordering::Acquire), 8);
            assert_eq!(control.usage().bytes, 0);
            assert_eq!(control.usage().count(ControlKind::Registration), 0);
        }
    }

    #[test]
    fn all_anchor_mutexes_initialize_once_before_sharing_and_real_contention() {
        let control = CoreControl::default();
        let calls = AtomicUsize::new(0);
        let owner = UiDeliveryOwner::prepare(&Digest::hash(b"warm-anchor"), &control, |slot| {
            assert!(control.usage().bytes > 0);
            let result = warm_new_mutex(slot);
            calls.fetch_add(1, Ordering::AcqRel);
            result
        })
        .unwrap();
        assert_eq!(calls.load(Ordering::Acquire), MAX_OPERATIONS);
        assert!(
            owner
                .anchor
                .slots
                .iter()
                .all(|slot| slot.try_lock().is_ok())
        );
        let held = owner.anchor.slots[0].lock().unwrap();
        let (send, receive) = std::sync::mpsc::channel();
        let resume = Arc::new(Barrier::new(2));
        let worker = {
            let anchor = owner.anchor.clone();
            let resume = resume.clone();
            std::thread::spawn(move || {
                assert!(matches!(
                    anchor.slots[0].try_lock(),
                    Err(std::sync::TryLockError::WouldBlock)
                ));
                send.send(()).unwrap();
                resume.wait();
                assert!(anchor.slots[0].try_lock().is_ok());
            })
        };
        receive.recv().unwrap();
        drop(held);
        resume.wait();
        worker.join().unwrap();
        // The pinned OnceBox source establishes no new backing allocation:
        // its pointer is set before sharing and no path resets that pointer.
        // This counter checks constructor coverage, not an allocator/RSS claim.
        assert_eq!(calls.load(Ordering::Acquire), MAX_OPERATIONS);
        owner.close();
        assert!(owner.anchor.removable());
    }

    #[test]
    fn record_initialization_failure_preserves_funding_until_unpublished_storage_drops() {
        let control = CoreControl::default();
        let owner = owner(&control, b"partial-record");
        let token = CoreCancellation::new_with_control(&control).unwrap();
        let external = tokio_util::sync::CancellationToken::new();
        let baseline = control.usage();
        for unwind in [false, true] {
            let calls = AtomicUsize::new(0);
            let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                let reservation = new_funding(
                    control
                        .reserve(
                            ControlKind::Frame,
                            UiDelivery::storage_bytes().unwrap()
                                + Funding::storage_bytes() as u64
                                + 1024,
                        )
                        .unwrap(),
                )
                .unwrap();
                owner.anchor.admit_prepared(
                    UiSessionId::parse("ui_partial").unwrap(),
                    None,
                    token.clone(),
                    external.clone(),
                    reservation,
                    |sources| {
                        assert!(control.usage().bytes > baseline.bytes);
                        assert_eq!(
                            control.usage().count(ControlKind::Frame),
                            baseline.count(ControlKind::Frame) + 1
                        );
                        warm_new_mutex(sources)?;
                        calls.fetch_add(1, Ordering::AcqRel);
                        assert!(sources.try_lock().is_ok());
                        if unwind {
                            panic!("deterministic private record initialization unwind");
                        }
                        Err(GateError::Unavailable)
                    },
                )
            }));
            match result {
                Ok(result) => {
                    assert!(!unwind);
                    assert!(result.is_err());
                }
                Err(_) => assert!(unwind),
            }
            assert_eq!(calls.load(Ordering::Acquire), 1);
            assert_eq!(control.usage(), baseline);
            assert!(
                owner
                    .anchor
                    .slots
                    .iter()
                    .all(|slot| slot.try_lock().unwrap().is_none())
            );
        }
        owner.close();
        assert!(owner.anchor.removable());
    }

    #[test]
    fn record_mutex_initializes_before_publication_and_shared_contention() {
        let control = CoreControl::default();
        let owner = owner(&control, b"warm-record");
        let calls = AtomicUsize::new(0);
        let reservation = new_funding(
            control
                .reserve(
                    ControlKind::Frame,
                    UiDelivery::storage_bytes().unwrap() + Funding::storage_bytes() as u64 + 1024,
                )
                .unwrap(),
        )
        .unwrap();
        let record = owner
            .anchor
            .admit_prepared(
                UiSessionId::parse("ui_warm").unwrap(),
                None,
                CoreCancellation::new_with_control(&control).unwrap(),
                tokio_util::sync::CancellationToken::new(),
                reservation,
                |sources| {
                    assert!(
                        owner
                            .anchor
                            .slots
                            .iter()
                            .all(|slot| slot.try_lock().unwrap().is_none())
                    );
                    warm_new_mutex(sources)?;
                    calls.fetch_add(1, Ordering::AcqRel);
                    Ok(())
                },
            )
            .unwrap();
        let held = record.sources.lock().unwrap();
        let copy = record.clone();
        std::thread::spawn(move || {
            assert!(matches!(
                copy.sources.try_lock(),
                Err(std::sync::TryLockError::WouldBlock)
            ));
        })
        .join()
        .unwrap();
        drop(held);
        assert!(record.sources.try_lock().is_ok());
        assert_eq!(calls.load(Ordering::Acquire), 1);
        record.release();
        owner.close();
        assert!(owner.anchor.removable());
    }

    #[tokio::test]
    async fn actual_cleanup_rejects_pending_inflight_unknown_and_retains_their_original_custody() {
        // Pending source-list contention: the genuine process-owner close
        // fails without accepting delivery or returning its original funds.
        {
            let control = CoreControl::default();
            let owner = owner(&control, b"check-pending");
            let record = delivery(&owner, &control, None);
            let source = sink(&control, Behavior::Success, None);
            drop(bind(&record, &source));
            record.accept(StreamCloseReason::Manual);
            let (held_send, held_receive) = std::sync::mpsc::channel();
            let (resume_send, resume_receive) = std::sync::mpsc::channel();
            let worker = {
                let record = record.clone();
                std::thread::spawn(move || {
                    let _guard = record.sources.lock().unwrap();
                    held_send.send(()).unwrap();
                    resume_receive.recv().unwrap();
                })
            };
            held_receive.recv().unwrap();
            owner.retire();
            let funded = control.usage();
            assert_eq!(
                crate::tracked_stream::TrackedStream::check_native_cleanup(&owner.anchor.factory)
                    .await
                    .unwrap_err()
                    .code,
                "unavailable"
            );
            assert_eq!(control.usage(), funded);
            assert_eq!(source.calls.load(Ordering::Acquire), 0);
            assert!(!owner.anchor.removable());
            resume_send.send(()).unwrap();
            worker.join().unwrap();
            crate::tracked_stream::TrackedStream::check_native_cleanup(&owner.anchor.factory)
                .await
                .unwrap();
            assert_eq!(control.usage(), funded);
            assert_eq!(source.calls.load(Ordering::Acquire), 1);
            assert!(!owner.anchor.removable());
            record.release();
            owner.close();
            assert!(owner.anchor.removable());
        }
        // Actual callback is in flight on a foreign thread; cleanup cannot
        // classify that receipt as settled or release the source/captures.
        {
            let control = CoreControl::default();
            let owner = owner(&control, b"check-inflight");
            let record = delivery(&owner, &control, None);
            let entered = Arc::new(Barrier::new(2));
            let resume = Arc::new(Barrier::new(2));
            let source = sink(
                &control,
                Behavior::Success,
                Some((entered.clone(), resume.clone())),
            );
            drop(bind(&record, &source));
            record.accept(StreamCloseReason::Manual);
            let worker = {
                let record = record.clone();
                std::thread::spawn(move || record.flush())
            };
            entered.wait();
            owner.retire();
            let funded = control.usage();
            assert_eq!(
                crate::tracked_stream::TrackedStream::check_native_cleanup(&owner.anchor.factory)
                    .await
                    .unwrap_err()
                    .code,
                "unavailable"
            );
            assert_eq!(control.usage(), funded);
            assert!(!owner.anchor.removable());
            resume.wait();
            worker.join().unwrap().unwrap();
            crate::tracked_stream::TrackedStream::check_native_cleanup(&owner.anchor.factory)
                .await
                .unwrap();
            assert_eq!(control.usage(), funded);
            assert_eq!(source.calls.load(Ordering::Acquire), 1);
            record.release();
            owner.close();
            assert!(owner.anchor.removable());
        }
        // Refusal/panic remains Unknown through the real close checker after
        // last control/source/Factory handles drop. No replay or false refund.
        for (name, behavior) in [
            (b"check-refused".as_slice(), Behavior::Refuse),
            (b"check-panicked".as_slice(), Behavior::Panic),
        ] {
            let control = CoreControl::default();
            let owner = owner(&control, name);
            let record = delivery(&owner, &control, None);
            let source = sink(&control, behavior, None);
            drop(bind(&record, &source));
            record.accept(StreamCloseReason::Manual);
            record.release();
            let anchor = owner.weak();
            let retained = Arc::downgrade(&source);
            drop(record);
            drop(source);
            owner.close();
            drop(owner);
            let anchor = anchor.upgrade().unwrap();
            let funded = control.usage();
            assert!(!anchor.notifications_settled());
            for _ in 0..2 {
                assert_eq!(
                    crate::tracked_stream::TrackedStream::check_native_cleanup(&anchor.factory)
                        .await
                        .unwrap_err()
                        .code,
                    "unavailable"
                );
                assert_eq!(control.usage(), funded);
                assert_eq!(retained.upgrade().unwrap().calls.load(Ordering::Acquire), 1);
                assert!(!anchor.removable());
            }
        }
    }

    #[tokio::test]
    async fn last_factory_owner_and_process_slot_drop_keep_anchor_charge_until_last_weak() {
        let control = CoreControl::default();
        let owner = owner(&control, b"weak-anchor-funding");
        let weak = owner.weak();
        owner.close();
        let funded = control.usage();
        crate::tracked_stream::TrackedStream::check_native_cleanup(&owner.anchor.factory)
            .await
            .unwrap();
        assert_eq!(control.usage(), funded);
        drop(owner);
        assert!(weak.upgrade().is_none());
        assert_eq!(control.usage(), funded);
        drop(weak);
        assert_eq!(control.usage().bytes, 0);
        assert_eq!(control.usage().count(ControlKind::Registration), 0);
    }
}
