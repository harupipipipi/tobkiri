//! Native consumption custody, separate from permission and physical cleanup.
//!
//! The store call is synchronous. Its caller owns no effect until it receives
//! an executable permit. A returned store error (or an unwind through the call)
//! leaves the original attempt in a bounded process-owned slot. Only an exact,
//! acknowledged terminal from that original store can retire an unresolved slot.

use crate::*;
use std::{
    fmt,
    sync::{
        Mutex,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tobkiri_canon::{AllocationError, Digest, LeasedShared};

const QUARANTINE_SLOTS: usize = 256;
// Includes bounded terminal reconstruction/verification scratch. Dynamic
// identity and effect copies are additionally inventoried below, before use.
const TERMINAL_SCRATCH_BYTES: usize = 16 * 1024;
const MAX_FAILURE_CODE_BYTES: usize = 256;

type Record = LeasedShared<AttemptState, ControlReservation>;
struct AttemptState {
    original: Grant,
    cx: CallCx,
    requested_expiry: u64,
    inflight: AtomicBool,
    progress: Mutex<Progress>,
}
#[derive(Default)]
struct Progress {
    returned: Option<Reservation>,
    prepared: Option<PreparedDispatch>,
    handed_off: bool,
    reconciled: bool,
}
struct Slot {
    serial: u64,
    record: Option<Record>,
}
struct Registry {
    next: u64,
    slots: [Option<Slot>; QUARANTINE_SLOTS],
}
impl Registry {
    fn claim(&mut self) -> Result<(usize, u64), GateError> {
        let index = self
            .slots
            .iter()
            .position(Option::is_none)
            .ok_or_else(|| GateError::Limit("consumption recovery capacity".into()))?;
        let serial = self.next;
        self.next = serial
            .checked_add(1)
            .ok_or_else(|| GateError::Limit("consumption recovery sequence".into()))?;
        self.slots[index] = Some(Slot {
            serial,
            record: None,
        });
        Ok((index, serial))
    }
}
static REGISTRY: Mutex<Registry> = Mutex::new(Registry {
    next: 1,
    slots: [const { None }; QUARANTINE_SLOTS],
});

/// A reservation for one fixed process slot. Before installation it owns no
/// payload, so releasing a failed preparation cannot run a payload destructor
/// under the registry lock.
struct SlotClaim {
    index: usize,
    serial: u64,
    installed: bool,
}
impl SlotClaim {
    fn reserve() -> Result<Self, GateError> {
        let mut registry = REGISTRY.lock().map_err(|_| GateError::Unavailable)?;
        let (index, serial) = registry.claim()?;
        Ok(Self {
            index,
            serial,
            installed: false,
        })
    }
    fn install(mut self, record: &Record) -> Result<Custody, GateError> {
        {
            let mut registry = REGISTRY.lock().map_err(|_| GateError::Unavailable)?;
            let slot = registry.slots[self.index]
                .as_mut()
                .filter(|slot| slot.serial == self.serial && slot.record.is_none())
                .ok_or(GateError::Unavailable)?;
            slot.record = Some(record.clone());
            self.installed = true;
        }
        Ok(Custody {
            index: self.index,
            serial: self.serial,
            record: record.clone(),
        })
    }
}
impl Drop for SlotClaim {
    fn drop(&mut self) {
        if self.installed {
            return;
        }
        let removed = {
            let mut registry = REGISTRY.lock().unwrap_or_else(|e| e.into_inner());
            if registry.slots[self.index]
                .as_ref()
                .is_some_and(|s| s.serial == self.serial)
            {
                registry.slots[self.index].take()
            } else {
                None
            }
        };
        // Even this currently-empty placeholder is destroyed outside the lock.
        drop(removed);
    }
}

struct Custody {
    index: usize,
    serial: u64,
    record: Record,
}
impl Custody {
    fn state(&self) -> &AttemptState {
        self.record.get()
    }
    fn remove_slot(&self) {
        let removed = {
            let mut registry = REGISTRY.lock().unwrap_or_else(|e| e.into_inner());
            if registry.slots[self.index]
                .as_ref()
                .is_some_and(|s| s.serial == self.serial)
            {
                registry.slots[self.index].take()
            } else {
                None
            }
        };
        // The caller retains the same record. No last-reference payload Drop
        // can occur under either the registry or progress mutex.
        drop(removed);
    }
    fn reconcile(&self) -> Result<bool, GateError> {
        let state = self.state();
        if state.inflight.load(Ordering::Acquire) {
            return Ok(false);
        }
        {
            let progress = state.progress.lock().unwrap_or_else(|e| e.into_inner());
            if progress.reconciled {
                return Ok(true);
            }
            if progress.handed_off {
                return Ok(false);
            }
        }
        // No registry/progress guard crosses a store call or terminal build.
        let Some(delivery) = state
            .original
            .approvals
            .terminal_delivery(&state.original.reservation.id)?
        else {
            return Ok(false);
        };
        if !delivery.acknowledged {
            return Ok(false);
        }
        verify_terminal(state, &delivery)?;
        {
            let mut progress = state.progress.lock().unwrap_or_else(|e| e.into_inner());
            if progress.handed_off {
                return Ok(false);
            }
            progress.reconciled = true;
        }
        self.remove_slot();
        Ok(true)
    }
}
impl fmt::Debug for Custody {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ConsumptionCustody")
            .field("id", &self.state().original.id())
            .finish_non_exhaustive()
    }
}

/// Exactly one native attempt. Only `BeforeConsumed` proves that the durable
/// consume operation was never invoked. Neither other variant authorizes work.
#[must_use]
#[derive(Debug)]
pub enum ConsumptionAttempt {
    BeforeConsumed(GateError),
    Consumed(ConsumedAttempt),
    CommitUnknown(UnknownConsumption),
}
impl ConsumptionAttempt {
    pub(crate) fn into_legacy_result(self) -> Result<DispatchPermit, GateError> {
        match self {
            Self::BeforeConsumed(error) => Err(error),
            Self::CommitUnknown(unknown) => Err(unknown.error),
            Self::Consumed(attempt) => attempt.into_dispatch().map_err(|cleanup| cleanup.error),
        }
    }
}

struct PreparedDispatch {
    reservation: Reservation,
    request: EffectRequest,
    limits: ResourceLimits,
    deadline: Instant,
}
/// Store-confirmed consumption, with either a complete executable preparation
/// or a sealed cleanup-only failure. Dropping it cannot abandon the original.
#[must_use]
pub struct ConsumedAttempt {
    custody: Custody,
    preparation_error: Option<GateError>,
}
impl fmt::Debug for ConsumedAttempt {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ConsumedAttempt")
            .field("custody", &self.custody)
            .field("executable", &self.preparation_error.is_none())
            .finish()
    }
}
impl ConsumedAttempt {
    pub fn id(&self) -> &GrantId {
        self.custody.state().original.id()
    }
    pub fn context(&self) -> &CallCx {
        &self.custody.state().cx
    }
    pub fn preparation_error(&self) -> Option<&GateError> {
        self.preparation_error.as_ref()
    }
    /// The only executable conversion. All fallible construction precedes
    /// slot removal. The complete original owner then moves into the permit;
    /// only moves, unlocks and non-last reference drops follow that transfer.
    pub fn into_dispatch(self) -> Result<DispatchPermit, CleanupOnlyConsumption> {
        let Self {
            custody,
            preparation_error,
        } = self;
        if let Some(error) = preparation_error {
            return Err(CleanupOnlyConsumption { custody, error });
        }
        let prepared = {
            let mut progress = custody
                .state()
                .progress
                .lock()
                .unwrap_or_else(|e| e.into_inner());
            if progress.reconciled || progress.handed_off || progress.prepared.is_none() {
                return Err(CleanupOnlyConsumption {
                    custody: Custody {
                        index: custody.index,
                        serial: custody.serial,
                        record: custody.record.clone(),
                    },
                    error: GateError::Consumed,
                });
            }
            progress.handed_off = true;
            // Move from the existing funded block, without allocating or
            // dropping a payload while the progress mutex is held.
            progress.prepared.take().expect("checked prepared dispatch")
        };
        let permit = DispatchPermit {
            reservation: prepared.reservation,
            request: prepared.request,
            limits: prepared.limits,
            deadline: prepared.deadline,
            _consumption_owner: SuccessfulConsumption {
                _record: custody.record.clone(),
            },
        };
        custody.remove_slot();
        // Q45: the permit already owns the complete funded state. These are
        // non-last drops; original Store/Audit/context destruction cannot run.
        drop(custody);
        Ok(permit)
    }
}

/// Cleanup custody with no execution conversion. The original returned row
/// remains owned even when clock mapping or preparation could not succeed.
#[must_use]
#[derive(Debug)]
pub struct CleanupOnlyConsumption {
    custody: Custody,
    error: GateError,
}
impl CleanupOnlyConsumption {
    pub fn id(&self) -> &GrantId {
        self.custody.state().original.id()
    }
    pub fn context(&self) -> &CallCx {
        &self.custody.state().cx
    }
    pub fn error(&self) -> &GateError {
        &self.error
    }
    pub fn reconcile(&self) -> Result<bool, GateError> {
        self.custody.reconcile()
    }
    /// Propose one fixed failure through existing terminal CAS. A wrong Gate,
    /// deadline winner or audit failure is never a release proof: retirement
    /// separately requires the original store's exact acknowledged outbox.
    pub async fn settle_failure(&self, gate: &dyn Gate) -> Result<bool, GateError> {
        let id = clone_id(self.id())?;
        let result = gate
            .record_outcome(
                id,
                Outcome::Failed {
                    code: "consumption-preparation-failed".into(),
                },
            )
            .await;
        let reconciled = self.reconcile()?;
        result?;
        Ok(reconciled)
    }
}
/// The store may have committed; no permit or cleanup proposal can be obtained
/// from this native owner. Only original-state terminal reconciliation retires
/// its slot. Dropping the observer leaves the process owner intact.
#[must_use]
#[derive(Debug)]
pub struct UnknownConsumption {
    custody: Custody,
    error: GateError,
}
impl UnknownConsumption {
    pub fn id(&self) -> &GrantId {
        self.custody.state().original.id()
    }
    pub fn context(&self) -> &CallCx {
        &self.custody.state().cx
    }
    pub fn error(&self) -> &GateError {
        &self.error
    }
    pub fn reconcile(&self) -> Result<bool, GateError> {
        self.custody.reconcile()
    }
}

/// Private success carrier, not another authority or process quarantine slot.
/// The shared allocator drops all payload and physically deallocates its block
/// BEFORE releasing the external native-control reservation.
#[derive(Debug)]
pub(crate) struct SuccessfulConsumption {
    _record: Record,
}

/// Explicit bounded recovery over the original process owners. It never
/// consumes, fabricates a terminal, flushes generically, or infers Weak death.
/// An error preserves every unresolved owner and remains visible to the caller.
pub fn reconcile_consumption_quarantine() -> Result<usize, GateError> {
    let mut released = 0;
    let mut first_error = None;
    for index in 0..QUARANTINE_SLOTS {
        let custody = {
            let registry = REGISTRY.lock().map_err(|_| GateError::Unavailable)?;
            registry.slots[index].as_ref().and_then(|slot| {
                slot.record.as_ref().map(|record| Custody {
                    index,
                    serial: slot.serial,
                    record: record.clone(),
                })
            })
        };
        if let Some(custody) = custody {
            match custody.reconcile() {
                Ok(reconciled) => released += usize::from(reconciled),
                Err(error) if first_error.is_none() => first_error = Some(error),
                Err(_) => {}
            }
        }
    }
    first_error.map_or(Ok(released), Err)
}

struct Inflight<'a>(&'a AtomicBool);
impl Drop for Inflight<'_> {
    fn drop(&mut self) {
        self.0.store(false, Ordering::Release);
    }
}

pub(crate) fn consume(
    grant: Grant,
    cx: &CallCx,
    request: &EffectRequest,
    args: &Digest,
) -> ConsumptionAttempt {
    consume_with_clock(grant, cx, request, args, persisted_instant)
}
fn consume_with_clock(
    grant: Grant,
    cx: &CallCx,
    request: &EffectRequest,
    args: &Digest,
    clock: fn(u64, Instant) -> Result<Instant, GateError>,
) -> ConsumptionAttempt {
    let prepare = || -> Result<(Instant, u64, ControlReservation, SlotClaim), GateError> {
        cx.check_active()?;
        if grant.reservation.profile != *cx.profile()
            || grant.reservation.principal != *cx.principal()
            || grant.reservation.request != *cx.request()
            || grant.reservation.revision.as_ref() != cx.revision()
            || grant.request != *request
            || grant.reservation.effect != request.digest()?
            || grant.reservation.args != *args
        {
            return Err(GateError::Denied("grant binding mismatch".into()));
        }
        let epoch = grant.approvals.security_epoch(cx.profile())?;
        if epoch != grant.reservation.epoch || epoch != cx.security_epoch() {
            return Err(GateError::StaleEpoch);
        }
        let deadline = cx.deadline_for_limits(&cx.limits().intersect(&grant.limits));
        let requested = deadline_unix(deadline)?;
        let bytes = consumption_bytes(&grant)?;
        let funding = cx.control().reserve(ControlKind::Queue, bytes)?;
        let slot = SlotClaim::reserve()?;
        Ok((deadline, requested, funding, slot))
    };
    let (deadline, requested_expiry, funding, slot) = match prepare() {
        Ok(value) => value,
        Err(error) => return ConsumptionAttempt::BeforeConsumed(error),
    };
    let record = match Record::try_new_precharged(
        AttemptState {
            original: grant,
            cx: cx.clone(),
            requested_expiry,
            inflight: AtomicBool::new(false),
            progress: Mutex::new(Progress::default()),
        },
        funding,
    ) {
        Ok(record) => record,
        Err(_) => return ConsumptionAttempt::BeforeConsumed(GateError::Unavailable),
    };
    let custody = match slot.install(&record) {
        Ok(custody) => custody,
        Err(error) => return ConsumptionAttempt::BeforeConsumed(error),
    };
    let state = record.get();
    // The independent process owner is installed BEFORE Store entry. Both
    // returned Err and unwinding now suppress Grant's pre-use abandonment.
    state
        .original
        .consumption_attempted
        .store(true, Ordering::Release);
    state.inflight.store(true, Ordering::Release);
    let inflight = Inflight(&state.inflight);
    let result = state
        .original
        .approvals
        .consume_with_deadline(&state.original.reservation, requested_expiry);
    let returned = match result {
        Ok(row) => {
            // No clock, allocation, validation or other fallible work precedes
            // this latch after the actual StoreOk.
            state.original.consumed.store(true, Ordering::Release);
            row
        }
        Err(error) => {
            drop(inflight);
            return ConsumptionAttempt::CommitUnknown(UnknownConsumption { custody, error });
        }
    };
    drop(inflight);
    if !consumed_row_matches(&state.original.reservation, &returned, requested_expiry) {
        // An unauthenticated returned row is not usable cleanup/execution
        // evidence. The sealed original remains owned for reconciliation.
        return ConsumptionAttempt::CommitUnknown(UnknownConsumption {
            custody,
            error: GateError::Invalid("consumption returned an unbound reservation".into()),
        });
    }
    let prepared = {
        let mut progress = state.progress.lock().unwrap_or_else(|e| e.into_inner());
        progress.returned = Some(returned);
        let returned = progress
            .returned
            .as_ref()
            .expect("just installed returned reservation");
        (|| {
            Ok(PreparedDispatch {
                deadline: clock(returned.expires_unix_ms, deadline)?,
                reservation: clone_reservation(returned)?,
                request: clone_effect(&state.original.request)?,
                limits: state.original.limits.clone(),
            })
        })()
    };
    let preparation_error = match prepared {
        Ok(prepared) => {
            // This is the same already precharged state block, not a new box
            // added merely to reduce the public enum's stack representation.
            state
                .progress
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .prepared = Some(prepared);
            None
        }
        Err(error) => Some(error),
    };
    ConsumptionAttempt::Consumed(ConsumedAttempt {
        custody,
        preparation_error,
    })
}

fn binding_matches(a: &Reservation, b: &Reservation) -> bool {
    a.id == b.id
        && a.profile == b.profile
        && a.principal == b.principal
        && a.request == b.request
        && a.effect == b.effect
        && a.args == b.args
        && a.epoch == b.epoch
        && a.revision == b.revision
        && a.authority == b.authority
}
fn consumed_row_matches(original: &Reservation, actual: &Reservation, requested: u64) -> bool {
    binding_matches(original, actual)
        && actual.expires_unix_ms > 0
        && actual.expires_unix_ms <= requested.min(original.expires_unix_ms)
        && actual.original_expires_unix_ms
            == Some(
                original
                    .original_expires_unix_ms
                    .unwrap_or(original.expires_unix_ms),
            )
}
fn verify_terminal(state: &AttemptState, delivery: &TerminalDelivery) -> Result<(), GateError> {
    let row = &delivery.decision.reservation;
    let original = &state.original.reservation;
    let returned = {
        let progress = state.progress.lock().unwrap_or_else(|e| e.into_inner());
        progress
            .returned
            .as_ref()
            .map(|r| (r.expires_unix_ms, r.original_expires_unix_ms))
    };
    let expiry_matches = match delivery.decision.disposition {
        TerminalDisposition::Completed | TerminalDisposition::ExpiredCleanupUnknown => {
            consumed_row_matches(original, row, state.requested_expiry)
                && returned.is_none_or(|(expiry, provenance)| {
                    row.expires_unix_ms <= expiry && row.original_expires_unix_ms == provenance
                })
        }
        TerminalDisposition::AbandonedBeforeUse | TerminalDisposition::ExpiredBeforeUse => {
            !state.original.consumed.load(Ordering::Acquire)
                && returned.is_none()
                && binding_matches(original, row)
                && original.expires_unix_ms == row.expires_unix_ms
                && original.original_expires_unix_ms == row.original_expires_unix_ms
        }
    };
    if !expiry_matches {
        return Err(GateError::Invalid(
            "consumption terminal binding mismatch".into(),
        ));
    }
    if matches!(&delivery.decision.outcome, Outcome::Failed { code } if code.len() > MAX_FAILURE_CODE_BYTES)
    {
        return Err(GateError::Invalid("consumption terminal code bound".into()));
    }
    let expected = TerminalDecision::new(
        clone_reservation(row)?,
        delivery.decision.outcome.clone(),
        delivery.decision.disposition.clone(),
    )?;
    let actual = &delivery.decision;
    if actual.event_id != expected.event_id
        || actual.event.profile != expected.event.profile
        || actual.event.principal != expected.event.principal
        || actual.event.request != expected.event.request
        || actual.event.kind != expected.event.kind
        || actual.event.payload != expected.event.payload
    {
        return Err(GateError::Invalid(
            "consumption terminal outbox mismatch".into(),
        ));
    }
    Ok(())
}

fn unix_ms() -> Result<u64, GateError> {
    u64::try_from(
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| GateError::Invalid("system clock before Unix epoch".into()))?
            .as_millis(),
    )
    .map_err(|_| GateError::Limit("system clock outside supported range".into()))
}
fn deadline_unix(deadline: Instant) -> Result<u64, GateError> {
    let remaining = u64::try_from(
        deadline
            .saturating_duration_since(Instant::now())
            .as_millis(),
    )
    .map_err(|_| GateError::Limit("deadline range".into()))?;
    unix_ms()?
        .checked_add(remaining)
        .filter(|n| *n <= 9_007_199_254_740_991)
        .ok_or_else(|| GateError::Limit("deadline range".into()))
}
fn persisted_instant(expires: u64, ceiling: Instant) -> Result<Instant, GateError> {
    // Sample monotonic time BEFORE wall time, and subtract the wall clock's
    // submillisecond uncertainty. An overflow/expired row is cleanup-only;
    // falling back to the original (later) Instant would widen Store's expiry.
    let monotonic = Instant::now();
    let remaining = expires
        .checked_sub(unix_ms()?)
        .filter(|n| *n > 0)
        .ok_or(GateError::Deadline)?;
    let bound = monotonic
        .checked_add(Duration::from_millis(remaining - 1))
        .ok_or_else(|| GateError::Limit("consumption deadline range".into()))?
        .min(ceiling);
    if bound <= Instant::now() {
        return Err(GateError::Deadline);
    }
    Ok(bound)
}
fn add(a: usize, b: usize) -> Result<usize, GateError> {
    a.checked_add(b)
        .ok_or_else(|| GateError::Limit("consumption metadata range".into()))
}
fn dynamic_reservation(row: &Reservation) -> Result<usize, GateError> {
    let mut bytes = row.id.clone_storage_bytes();
    for part in [
        row.profile.clone_storage_bytes(),
        row.principal.clone_storage_bytes().map_err(allocation)?,
        row.request.clone_storage_bytes(),
    ] {
        bytes = add(bytes, part)?;
    }
    Ok(bytes)
}
fn dynamic_effect(effect: &EffectRequest) -> Result<usize, GateError> {
    use EffectRequest::*;
    let parts: &[usize] = &match effect {
        WasmEntry { pack, .. } | Native { pack } | Permissions { pack, .. } => {
            [pack.clone_storage_bytes(), 0, 0]
        }
        FlowRun {
            implements,
            flow_owner: (pack, _),
            flow_key,
            ..
        } => [
            implements.clone_storage_bytes(),
            pack.clone_storage_bytes(),
            flow_key.len(),
        ],
        PortCall { shape, op } => [shape.clone_storage_bytes(), op.clone_storage_bytes(), 0],
        Network {
            origin,
            method,
            url,
        } => [origin.len(), method.len(), url.len()],
        Fs { root, path, .. } => [root.len(), path.len(), 0],
        Proc { program } | Pty { program } => [program.len(), 0, 0],
        Secret { name, .. } => [name.len(), 0, 0],
        Storage {
            profile,
            owner,
            store,
            ..
        } => [
            profile.clone_storage_bytes(),
            owner.clone_storage_bytes(),
            store.len(),
        ],
        UserGlobalStorage { store, .. } => [store.len(), 0, 0],
        HostIo { operation } => [operation.len(), 0, 0],
        VirtualInputSubmit { .. }
        | UserPermissions { .. }
        | AuditAppend
        | AuditRead
        | Clock
        | Random => [0, 0, 0],
    };
    parts.iter().try_fold(0, |sum, n| add(sum, *n))
}
fn consumption_bytes(grant: &Grant) -> Result<u64, GateError> {
    let identity = dynamic_reservation(&grant.reservation)?;
    let effects = dynamic_effect(&grant.request)?;
    // Original + returned + executable copies, and worst-case escaped terminal
    // reconstruction scratch, all admitted before the Store transaction. The
    // existing Arc targets and CallCx are retained, not copied/relabelled.
    let dynamic = add(identity, effects)?
        .checked_mul(32)
        .ok_or_else(|| GateError::Limit("consumption metadata range".into()))?;
    let fixed = add(Record::storage_bytes(), std::mem::size_of::<Slot>())?;
    u64::try_from(add(add(fixed, dynamic)?, TERMINAL_SCRATCH_BYTES)?)
        .map_err(|_| GateError::Limit("consumption metadata range".into()))
}
fn allocation(_: AllocationError) -> GateError {
    GateError::Unavailable
}
fn clone_id(id: &GrantId) -> Result<GrantId, GateError> {
    id.try_clone_precharged().map_err(allocation)
}
fn clone_string(value: &str) -> Result<String, GateError> {
    let mut output = String::new();
    output
        .try_reserve_exact(value.len())
        .map_err(|_| GateError::Unavailable)?;
    output.push_str(value);
    Ok(output)
}
fn clone_reservation(row: &Reservation) -> Result<Reservation, GateError> {
    Ok(Reservation {
        id: clone_id(&row.id)?,
        profile: row.profile.try_clone_precharged().map_err(allocation)?,
        principal: row.principal.try_clone_precharged().map_err(allocation)?,
        request: row.request.try_clone_precharged().map_err(allocation)?,
        effect: row.effect.clone(),
        args: row.args.clone(),
        epoch: row.epoch,
        revision: row.revision.clone(),
        expires_unix_ms: row.expires_unix_ms,
        original_expires_unix_ms: row.original_expires_unix_ms,
        authority: row.authority.clone(),
    })
}
fn clone_effect(effect: &EffectRequest) -> Result<EffectRequest, GateError> {
    use EffectRequest::*;
    Ok(match effect {
        WasmEntry { pack, hash, entry } => WasmEntry {
            pack: pack.try_clone_precharged().map_err(allocation)?,
            hash: hash.clone(),
            entry: entry.clone(),
        },
        FlowRun {
            implements,
            flow_owner: (pack, hash),
            flow_key,
            target_digest,
        } => FlowRun {
            implements: implements.try_clone_precharged().map_err(allocation)?,
            flow_owner: (
                pack.try_clone_precharged().map_err(allocation)?,
                hash.clone(),
            ),
            flow_key: clone_string(flow_key)?,
            target_digest: target_digest.clone(),
        },
        PortCall { shape, op } => PortCall {
            shape: shape.try_clone_precharged().map_err(allocation)?,
            op: op.try_clone_precharged().map_err(allocation)?,
        },
        VirtualInputSubmit {
            ingress,
            invocation,
            source,
            routes,
        } => VirtualInputSubmit {
            ingress: ingress.clone(),
            invocation: invocation.clone(),
            source: source.clone(),
            routes: routes.clone(),
        },
        Network {
            origin,
            method,
            url,
        } => Network {
            origin: clone_string(origin)?,
            method: clone_string(method)?,
            url: clone_string(url)?,
        },
        Fs {
            root,
            path,
            operation,
        } => Fs {
            root: clone_string(root)?,
            path: clone_string(path)?,
            operation: *operation,
        },
        Proc { program } => Proc {
            program: clone_string(program)?,
        },
        Pty { program } => Pty {
            program: clone_string(program)?,
        },
        Secret { name, reveal } => Secret {
            name: clone_string(name)?,
            reveal: *reveal,
        },
        Storage {
            profile,
            owner,
            store,
            tier,
            operation,
        } => Storage {
            profile: profile.try_clone_precharged().map_err(allocation)?,
            owner: owner.try_clone_precharged().map_err(allocation)?,
            store: clone_string(store)?,
            tier: *tier,
            operation: *operation,
        },
        UserGlobalStorage { store, operation } => UserGlobalStorage {
            store: clone_string(store)?,
            operation: *operation,
        },
        AuditAppend => AuditAppend,
        AuditRead => AuditRead,
        HostIo { operation } => HostIo {
            operation: clone_string(operation)?,
        },
        Native { pack } => Native {
            pack: pack.try_clone_precharged().map_err(allocation)?,
        },
        UserPermissions {
            profile_hash,
            revision,
            epoch,
        } => UserPermissions {
            profile_hash: profile_hash.clone(),
            revision: revision.clone(),
            epoch: *epoch,
        },
        Permissions { pack, hash } => Permissions {
            pack: pack.try_clone_precharged().map_err(allocation)?,
            hash: hash.clone(),
        },
        Clock => Clock,
        Random => Random,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use async_trait::async_trait;
    use std::{
        collections::BTreeMap,
        sync::{
            Arc,
            atomic::{AtomicU8, AtomicU64},
        },
    };
    use tobkiri_canon::ids::{PackHash, PackId, ProfileId, RequestId};

    const NORMAL: u8 = 0;
    const ERROR_AFTER_COMMIT: u8 = 1;
    const PANIC_AFTER_COMMIT: u8 = 2;
    const WRONG_ARGS: u8 = 3;
    const WIDEN_EXPIRY: u8 = 4;
    const WRONG_AUTHORITY: u8 = 5;
    const WRONG_ORIGINAL_EXPIRY: u8 = 6;
    const TIGHTEN_EXPIRY: u8 = 7;
    const WRONG_ID: u8 = 8;
    const WRONG_PRINCIPAL: u8 = 9;
    const WRONG_REQUEST: u8 = 10;
    const WRONG_PROFILE: u8 = 11;
    const WRONG_REVISION: u8 = 12;
    const WRONG_EFFECT: u8 = 13;
    const WRONG_EPOCH: u8 = 14;

    struct Row {
        reservation: Reservation,
        dispatched: bool,
        consumed: bool,
        terminal: Option<TerminalDelivery>,
    }
    #[derive(Default)]
    struct Rows {
        sequence: u64,
        rows: BTreeMap<GrantId, Row>,
    }
    struct NativeStore {
        rows: Mutex<Rows>,
        permissions: PermissionSet,
        fault: AtomicU8,
        calls: AtomicU64,
        abandons: AtomicU64,
    }
    impl NativeStore {
        fn terminal(&self, id: &GrantId, acknowledged: bool) {
            let mut state = self.rows.lock().unwrap();
            let row = state.rows.get_mut(id).unwrap();
            assert!(row.consumed);
            row.terminal = Some(TerminalDelivery {
                decision: TerminalDecision::new(
                    row.reservation.clone(),
                    Outcome::Failed {
                        code: "native-test-completion".into(),
                    },
                    TerminalDisposition::Completed,
                )
                .unwrap(),
                acknowledged,
            });
        }
    }
    impl ApprovalStore for NativeStore {
        fn admission_status(&self, _: &ProfileId) -> Result<AdmissionStatus, GateError> {
            Ok(AdmissionStatus::Allowed)
        }
        fn latch_integrity(&self, _: &ProfileId, _: IntegrityFaultCode) -> Result<(), GateError> {
            Err(GateError::Unavailable)
        }
        fn security_epoch(&self, _: &ProfileId) -> Result<u64, GateError> {
            Ok(0)
        }
        fn advance_security_epoch(&self, _: &ProfileId) -> Result<u64, GateError> {
            Err(GateError::Unavailable)
        }
        fn permissions(
            &self,
            _: &ProfileId,
            _: &PackId,
            _: &PackHash,
        ) -> Result<Option<PermissionSet>, GateError> {
            Ok(Some(self.permissions.clone()))
        }
        fn set_permissions(
            &self,
            _: &ProfileId,
            _: &PackId,
            _: &PackHash,
            _: &PermissionSet,
        ) -> Result<(), GateError> {
            Err(GateError::Unavailable)
        }
        fn user_permissions(
            &self,
            _: &ProfileId,
            _: &Digest,
            _: &Digest,
            _: u64,
        ) -> Result<Option<PermissionSet>, GateError> {
            Err(GateError::Unavailable)
        }
        fn set_user_permissions(
            &self,
            _: &ProfileId,
            _: &Digest,
            _: &Digest,
            _: u64,
            _: &PermissionSet,
        ) -> Result<(), GateError> {
            Err(GateError::Unavailable)
        }
        fn reserve(&self, _: &Reservation) -> Result<(), GateError> {
            Err(GateError::Unavailable)
        }
        fn reserve_fresh(&self, request: &ReservationRequest) -> Result<Reservation, GateError> {
            let mut state = self.rows.lock().unwrap();
            state.sequence += 1;
            let reservation = request.bind(
                Digest::hash(b"owned-consumption-test-authority"),
                1,
                state.sequence,
            )?;
            state.rows.insert(
                reservation.id.clone(),
                Row {
                    reservation: reservation.clone(),
                    dispatched: false,
                    consumed: false,
                    terminal: None,
                },
            );
            Ok(reservation)
        }
        fn dispatch(&self, id: &GrantId) -> Result<(), GateError> {
            let mut state = self.rows.lock().unwrap();
            let row = state.rows.get_mut(id).ok_or(GateError::Unavailable)?;
            if row.dispatched {
                return Err(GateError::Consumed);
            }
            row.dispatched = true;
            Ok(())
        }
        fn dispatch_public_write(
            &self,
            _: &Reservation,
            _: &PublicWriteApproval,
        ) -> Result<(), GateError> {
            Err(GateError::Unavailable)
        }
        fn abandon(&self, _: &GrantId) -> Result<Option<Reservation>, GateError> {
            Err(GateError::Unavailable)
        }
        fn sweep_expired(&self, _: &ProfileId, _: u64) -> Result<u64, GateError> {
            Ok(0)
        }
        fn consume(&self, _: &Reservation) -> Result<(), GateError> {
            Err(GateError::Unavailable)
        }
        fn consume_with_deadline(
            &self,
            expected: &Reservation,
            expires: u64,
        ) -> Result<Reservation, GateError> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            let fault = self.fault.load(Ordering::SeqCst);
            let mut actual = {
                let mut state = self.rows.lock().unwrap();
                let row = state
                    .rows
                    .get_mut(&expected.id)
                    .ok_or(GateError::Unavailable)?;
                if !row.dispatched
                    || row.consumed
                    || crate::protocol::strict_bytes(&row.reservation)?
                        != crate::protocol::strict_bytes(expected)?
                {
                    return Err(GateError::Consumed);
                }
                row.reservation.original_expires_unix_ms = Some(
                    expected
                        .original_expires_unix_ms
                        .unwrap_or(expected.expires_unix_ms),
                );
                row.reservation.expires_unix_ms = expires.min(expected.expires_unix_ms);
                if fault == TIGHTEN_EXPIRY {
                    row.reservation.expires_unix_ms =
                        row.reservation.expires_unix_ms.min(unix_ms()? + 2_000);
                }
                row.consumed = true;
                row.reservation.clone()
            };
            // The actual committed row remains unchanged by returned-value
            // faults. These model lost delivery and a corrupt response, not
            // a fabricated Grant or an invented consumed permit.
            match fault {
                ERROR_AFTER_COMMIT => {
                    return Err(GateError::Persistence("consume delivery failed".into()));
                }
                PANIC_AFTER_COMMIT => panic!("consume unwound after commit"),
                WRONG_ARGS => actual.args = Digest::hash(b"wrong consumed args"),
                WIDEN_EXPIRY => actual.expires_unix_ms = expected.expires_unix_ms + 1,
                WRONG_AUTHORITY => actual.authority.as_mut().unwrap().sequence += 1,
                WRONG_ORIGINAL_EXPIRY => {
                    actual.original_expires_unix_ms = Some(expected.expires_unix_ms - 1)
                }
                WRONG_ID => actual.id = GrantId("wrong-returned-grant".into()),
                WRONG_PRINCIPAL => actual.principal = Principal::User,
                WRONG_REQUEST => actual.request = RequestId::new("wrong-request").unwrap(),
                WRONG_PROFILE => actual.profile = ProfileId::new("wrong-profile").unwrap(),
                WRONG_REVISION => actual.revision = Some(Digest::hash(b"wrong revision")),
                WRONG_EFFECT => actual.effect = Digest::hash(b"wrong effect"),
                WRONG_EPOCH => actual.epoch += 1,
                _ => {}
            }
            Ok(actual)
        }
        fn finish(&self, _: &GrantId, _: &Outcome) -> Result<Reservation, GateError> {
            Err(GateError::Unavailable)
        }
        fn terminal_delivery(&self, id: &GrantId) -> Result<Option<TerminalDelivery>, GateError> {
            Ok(self
                .rows
                .lock()
                .unwrap()
                .rows
                .get(id)
                .and_then(|row| row.terminal.clone()))
        }
        fn finish_terminal(
            &self,
            id: &GrantId,
            outcome: &Outcome,
        ) -> Result<TerminalDelivery, GateError> {
            let mut state = self.rows.lock().unwrap();
            let row = state.rows.get_mut(id).ok_or(GateError::Unavailable)?;
            if !row.consumed {
                return Err(GateError::Consumed);
            }
            if row.terminal.is_none() {
                row.terminal = Some(TerminalDelivery {
                    decision: TerminalDecision::new(
                        row.reservation.clone(),
                        outcome.clone(),
                        TerminalDisposition::Completed,
                    )?,
                    acknowledged: false,
                });
            }
            Ok(row.terminal.as_ref().unwrap().clone())
        }
        fn abandon_terminal(&self, id: &GrantId) -> Result<Option<TerminalDelivery>, GateError> {
            self.abandons.fetch_add(1, Ordering::SeqCst);
            let mut state = self.rows.lock().unwrap();
            let row = state.rows.get_mut(id).ok_or(GateError::Unavailable)?;
            if row.consumed {
                return Err(GateError::Consumed);
            }
            row.terminal = Some(TerminalDelivery {
                decision: TerminalDecision::new(
                    row.reservation.clone(),
                    Outcome::Cancelled,
                    TerminalDisposition::AbandonedBeforeUse,
                )?,
                acknowledged: false,
            });
            Ok(row.terminal.clone())
        }
        fn expire_terminal(
            &self,
            _: &ProfileId,
            _: u64,
        ) -> Result<Vec<TerminalDecision>, GateError> {
            Ok(Vec::new())
        }
        fn pending_terminal(
            &self,
            _: &ProfileId,
            _: u32,
        ) -> Result<Vec<TerminalDecision>, GateError> {
            Ok(Vec::new())
        }
        fn terminal_watermark(&self, _: &ProfileId) -> Result<u64, GateError> {
            Ok(0)
        }
        fn pending_terminal_through(
            &self,
            _: &ProfileId,
            _: u32,
            _: u64,
        ) -> Result<Vec<TerminalDecision>, GateError> {
            Ok(Vec::new())
        }
        fn acknowledge_terminal(&self, id: &Digest, event: &AuditEvent) -> Result<(), GateError> {
            let mut state = self.rows.lock().unwrap();
            let terminal = state
                .rows
                .values_mut()
                .find_map(|row| row.terminal.as_mut().filter(|t| t.decision.event_id == *id))
                .ok_or(GateError::Unavailable)?;
            if crate::protocol::strict_bytes(&terminal.decision.event)?
                != crate::protocol::strict_bytes(event)?
            {
                return Err(GateError::Invalid("test audit mismatch".into()));
            }
            terminal.acknowledged = true;
            Ok(())
        }
        fn public_write_approved(
            &self,
            _: &ProfileId,
            _: &PackId,
            _: &PackId,
            _: &str,
        ) -> Result<bool, GateError> {
            Ok(false)
        }
        fn approve_public_write(
            &self,
            _: &ProfileId,
            _: &PackId,
            _: &PackId,
            _: &str,
        ) -> Result<(), GateError> {
            Err(GateError::Unavailable)
        }
        fn paired_device_key(&self, _: &ProfileId, _: &str) -> Result<Option<[u8; 32]>, GateError> {
            Ok(None)
        }
        fn claim_attestation(
            &self,
            _: &ProfileId,
            _: &str,
            _: &str,
            _: u64,
        ) -> Result<bool, GateError> {
            Ok(false)
        }
    }
    struct Audit;
    impl AuditSink for Audit {
        fn append(&self, _: AuditEvent) -> Result<(), GateError> {
            Ok(())
        }
        fn append_idempotent(&self, _: &Digest, _: AuditEvent) -> Result<(), GateError> {
            Ok(())
        }
    }
    struct NoPrompt;
    #[async_trait]
    impl TrustedPrompt for NoPrompt {
        async fn prompt_approval(&self, _: ApprovalPrompt) -> ApprovalAnswer {
            panic!("Clock requires no prompt")
        }
    }
    struct Fixture {
        gate: GateEngine,
        store: Arc<NativeStore>,
        cx: CallCx,
        args: Digest,
    }
    impl Fixture {
        fn new(max_queue_entries: u64) -> Self {
            let control = CoreControl::new(CoreControlLimits {
                max_queue_entries,
                ..CoreControlLimits::default()
            })
            .unwrap();
            let limits = ResourceLimits {
                fuel_per_call: 1_000,
                memory_mib: 1,
                wall_ms: 60_000,
                table_elements: 16,
                output_bytes: 1_024,
            };
            let permissions = PermissionSet {
                wasm: limits.clone(),
                ..PermissionSet::default()
            };
            let store = Arc::new(NativeStore {
                rows: Mutex::new(Rows::default()),
                permissions: permissions.clone(),
                fault: AtomicU8::new(NORMAL),
                calls: AtomicU64::new(0),
                abandons: AtomicU64::new(0),
            });
            let gate = GateEngine::new_with_control(
                store.clone(),
                Arc::new(Audit),
                Arc::new(NoPrompt),
                MediatorChain::default(),
                control.clone(),
            );
            let profile = ProfileId::new("owned-consumption-tests").unwrap();
            let pack = PackId::new("fixture.consumption").unwrap();
            let hash = PackHash::new(Digest::hash(b"owned-consumption-pack"));
            gate.register_pack(
                &profile,
                &pack,
                &hash,
                PackPolicy {
                    declaration: permissions.clone(),
                    ceiling: permissions.clone(),
                    pack_limits: permissions,
                },
            )
            .unwrap();
            let cx = CallCx::try_root_with_control(
                profile,
                Principal::Pack { pack, hash },
                RequestId::new("consumption-request").unwrap(),
                CancellationToken::new_with_control(&control).unwrap(),
                Instant::now() + Duration::from_secs(60),
                limits,
                control,
            )
            .unwrap();
            Self {
                gate,
                store,
                cx,
                args: Digest::hash(b"owned-consumption-arguments"),
            }
        }
        async fn grant(&self) -> Grant {
            self.gate
                .authorize(&self.cx, EffectRequest::Clock, self.args.clone())
                .await
                .unwrap()
        }
        fn pending(&self, id: &GrantId) -> Custody {
            let registry = REGISTRY.lock().unwrap();
            registry
                .slots
                .iter()
                .enumerate()
                .find_map(|(index, slot)| {
                    let slot = slot.as_ref()?;
                    let record = slot.record.as_ref()?;
                    (record.get().original.id() == id).then(|| Custody {
                        index,
                        serial: slot.serial,
                        record: record.clone(),
                    })
                })
                .expect("original attempt retained in process custody")
        }
    }

    #[tokio::test]
    async fn binding_failure_and_control_pressure_are_before_consumption() {
        let fixture = Fixture::new(1);
        let result =
            fixture
                .grant()
                .await
                .consume_owned(&fixture.cx, &EffectRequest::Random, &fixture.args);
        assert!(matches!(
            result,
            ConsumptionAttempt::BeforeConsumed(GateError::Denied(_))
        ));
        assert_eq!(fixture.store.calls.load(Ordering::SeqCst), 0);
        assert_eq!(fixture.store.abandons.load(Ordering::SeqCst), 1);
        let held = fixture.cx.control().reserve(ControlKind::Queue, 1).unwrap();
        let result =
            fixture
                .grant()
                .await
                .consume_owned(&fixture.cx, &EffectRequest::Clock, &fixture.args);
        assert!(matches!(
            result,
            ConsumptionAttempt::BeforeConsumed(GateError::Limit(_))
        ));
        assert_eq!(fixture.store.calls.load(Ordering::SeqCst), 0);
        assert_eq!(fixture.store.abandons.load(Ordering::SeqCst), 2);
        drop(held);
    }

    #[tokio::test]
    async fn committed_error_retains_original_until_exact_acknowledged_outbox() {
        let fixture = Fixture::new(1);
        fixture
            .store
            .fault
            .store(ERROR_AFTER_COMMIT, Ordering::SeqCst);
        let grant = fixture.grant().await;
        let id = grant.id().clone();
        let ConsumptionAttempt::CommitUnknown(unknown) =
            grant.consume_owned(&fixture.cx, &EffectRequest::Clock, &fixture.args)
        else {
            panic!("ambiguous store error must retain custody")
        };
        assert!(matches!(unknown.error(), GateError::Persistence(_)));
        assert!(!unknown.reconcile().unwrap());
        assert_eq!(fixture.store.abandons.load(Ordering::SeqCst), 0);
        fixture.store.terminal(&id, false);
        assert!(!unknown.reconcile().unwrap());
        fixture.store.terminal(&id, true);
        {
            let mut rows = fixture.store.rows.lock().unwrap();
            rows.rows
                .get_mut(&id)
                .unwrap()
                .terminal
                .as_mut()
                .unwrap()
                .decision
                .reservation
                .args = Digest::hash(b"wrong terminal args");
        }
        assert!(matches!(unknown.reconcile(), Err(GateError::Invalid(_))));
        assert_eq!(fixture.cx.control().usage().count(ControlKind::Queue), 1);
        fixture.store.terminal(&id, true);
        {
            let mut rows = fixture.store.rows.lock().unwrap();
            rows.rows
                .get_mut(&id)
                .unwrap()
                .terminal
                .as_mut()
                .unwrap()
                .decision
                .event
                .kind = "wrong-outbox".into();
        }
        assert!(matches!(unknown.reconcile(), Err(GateError::Invalid(_))));
        fixture.store.terminal(&id, true);
        assert!(unknown.reconcile().unwrap());
        // Observer still owns its actual metadata; removing the process slot
        // alone does not refund that live storage.
        assert_eq!(fixture.cx.control().usage().count(ControlKind::Queue), 1);
        drop(unknown);
        assert_eq!(fixture.cx.control().usage().count(ControlKind::Queue), 0);
    }

    #[tokio::test]
    async fn legacy_committed_error_and_unwind_keep_the_independent_owner() {
        for fault in [ERROR_AFTER_COMMIT, PANIC_AFTER_COMMIT] {
            let fixture = Fixture::new(1);
            fixture.store.fault.store(fault, Ordering::SeqCst);
            let grant = fixture.grant().await;
            let id = grant.id().clone();
            let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                grant.consume(&fixture.cx, &EffectRequest::Clock, &fixture.args)
            }));
            if fault == ERROR_AFTER_COMMIT {
                assert!(matches!(result.unwrap(), Err(GateError::Persistence(_))));
            } else {
                assert!(result.is_err());
            }
            assert_eq!(fixture.store.abandons.load(Ordering::SeqCst), 0);
            assert_eq!(fixture.cx.control().usage().count(ControlKind::Queue), 1);
            fixture.store.terminal(&id, true);
            let owner = fixture.pending(&id);
            assert!(owner.reconcile().unwrap());
            drop(owner);
            assert_eq!(fixture.cx.control().usage().count(ControlKind::Queue), 0);
        }
    }

    fn failed_clock(_: u64, _: Instant) -> Result<Instant, GateError> {
        Err(GateError::Deadline)
    }
    fn panicking_clock(_: u64, _: Instant) -> Result<Instant, GateError> {
        panic!("clock preparation unwound")
    }

    #[test]
    fn process_slot_bound_has_no_257th_or_sequence_wraparound_admission() {
        // Independent bookkeeping keeps this boundary test from denying
        // unrelated concurrently running Gate tests their real process slots.
        let mut registry = Registry {
            next: 1,
            slots: [const { None }; QUARANTINE_SLOTS],
        };
        for expected in 0..256 {
            assert_eq!(registry.claim().unwrap(), (expected, expected as u64 + 1));
        }
        assert!(matches!(registry.claim(), Err(GateError::Limit(_))));
        registry.slots[0] = None;
        registry.next = u64::MAX;
        assert!(matches!(registry.claim(), Err(GateError::Limit(_))));
        assert!(registry.slots[0].is_none());
    }

    #[tokio::test]
    async fn postcommit_clock_failure_is_cleanup_only_and_wrong_gate_cannot_release_it() {
        let fixture = Fixture::new(1);
        let wrong = Fixture::new(1);
        let grant = fixture.grant().await;
        let id = grant.id().clone();
        let ConsumptionAttempt::Consumed(attempt) = consume_with_clock(
            grant,
            &fixture.cx,
            &EffectRequest::Clock,
            &fixture.args,
            failed_clock,
        ) else {
            panic!("StoreOk must remain consumed")
        };
        assert!(matches!(
            attempt.preparation_error(),
            Some(GateError::Deadline)
        ));
        let cleanup = attempt.into_dispatch().unwrap_err();
        assert!(
            fixture
                .store
                .rows
                .lock()
                .unwrap()
                .rows
                .get(&id)
                .unwrap()
                .consumed
        );
        assert!(cleanup.settle_failure(&wrong.gate).await.is_err());
        assert_eq!(fixture.cx.control().usage().count(ControlKind::Queue), 1);
        assert!(!cleanup.reconcile().unwrap());
        assert!(cleanup.settle_failure(&fixture.gate).await.unwrap());
        drop(cleanup);
        assert_eq!(fixture.cx.control().usage().count(ControlKind::Queue), 0);
    }

    #[tokio::test]
    async fn panic_after_store_ok_never_runs_preuse_abandonment() {
        let fixture = Fixture::new(1);
        let grant = fixture.grant().await;
        let id = grant.id().clone();
        assert!(
            std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| consume_with_clock(
                grant,
                &fixture.cx,
                &EffectRequest::Clock,
                &fixture.args,
                panicking_clock
            )))
            .is_err()
        );
        assert_eq!(fixture.store.abandons.load(Ordering::SeqCst), 0);
        fixture.store.terminal(&id, true);
        let owner = fixture.pending(&id);
        assert!(owner.reconcile().unwrap());
        drop(owner);
        assert_eq!(fixture.cx.control().usage().count(ControlKind::Queue), 0);
    }

    #[tokio::test]
    async fn every_returned_binding_and_expiry_must_authenticate_before_execution() {
        let fixture = Fixture::new(1);
        for fault in [
            WRONG_ARGS,
            WIDEN_EXPIRY,
            WRONG_AUTHORITY,
            WRONG_ORIGINAL_EXPIRY,
            WRONG_ID,
            WRONG_PRINCIPAL,
            WRONG_REQUEST,
            WRONG_PROFILE,
            WRONG_REVISION,
            WRONG_EFFECT,
            WRONG_EPOCH,
        ] {
            fixture.store.fault.store(fault, Ordering::SeqCst);
            let grant = fixture.grant().await;
            let id = grant.id().clone();
            let ConsumptionAttempt::CommitUnknown(unknown) =
                grant.consume_owned(&fixture.cx, &EffectRequest::Clock, &fixture.args)
            else {
                panic!("unbound returned reservation must not mint a permit")
            };
            assert!(matches!(unknown.error(), GateError::Invalid(_)));
            fixture.store.terminal(&id, true);
            assert!(unknown.reconcile().unwrap());
            drop(unknown);
            assert_eq!(fixture.cx.control().usage().count(ControlKind::Queue), 0);
        }
    }

    #[tokio::test]
    async fn successful_handoff_reuses_process_slot_for_257_sequential_permits() {
        let fixture = Fixture::new(1);
        for _ in 0..257 {
            let permit = fixture
                .grant()
                .await
                .consume(&fixture.cx, &EffectRequest::Clock, &fixture.args)
                .unwrap();
            assert!(permit.matches_context(&fixture.cx));
            assert_eq!(fixture.cx.control().usage().count(ControlKind::Queue), 1);
            let in_quarantine = {
                let registry = REGISTRY.lock().unwrap();
                registry
                    .slots
                    .iter()
                    .flatten()
                    .filter_map(|slot| slot.record.as_ref())
                    .any(|record| record.get().original.id() == permit.id())
            };
            assert!(
                !in_quarantine,
                "successful delivery must not retain a process slot until terminal ACK"
            );
            drop(permit);
            assert_eq!(fixture.cx.control().usage().count(ControlKind::Queue), 0);
        }
        assert_eq!(fixture.store.calls.load(Ordering::SeqCst), 257);
        assert_eq!(fixture.store.abandons.load(Ordering::SeqCst), 0);
    }

    #[tokio::test]
    async fn executable_deadline_cannot_widen_the_actual_tightened_store_expiry() {
        let fixture = Fixture::new(1);
        fixture.store.fault.store(TIGHTEN_EXPIRY, Ordering::SeqCst);
        let permit = fixture
            .grant()
            .await
            .consume(&fixture.cx, &EffectRequest::Clock, &fixture.args)
            .unwrap();
        assert!(permit.deadline() <= Instant::now() + Duration::from_secs(2));
        assert!(permit.deadline() < fixture.cx.deadline());
        let rows = fixture.store.rows.lock().unwrap();
        let actual = &rows.rows.get(permit.id()).unwrap().reservation;
        assert_eq!(actual.expires_unix_ms, permit.reservation.expires_unix_ms);
        assert!(binding_matches(actual, &permit.reservation));
    }

    struct ReentrantAudit {
        outside_guard: Arc<AtomicBool>,
        control: CoreControl,
    }
    impl AuditSink for ReentrantAudit {
        fn append(&self, _: AuditEvent) -> Result<(), GateError> {
            Ok(())
        }
    }
    impl Drop for ReentrantAudit {
        fn drop(&mut self) {
            let guard = REGISTRY.lock().unwrap();
            assert_eq!(self.control.usage().count(ControlKind::Queue), 1);
            self.outside_guard.store(true, Ordering::SeqCst);
            drop(guard);
        }
    }
    #[tokio::test]
    async fn success_payload_destructor_runs_outside_registry_guard_before_funding_release() {
        let fixture = Fixture::new(1);
        let outside = Arc::new(AtomicBool::new(false));
        let mut grant = fixture.grant().await;
        grant.audit = Arc::new(ReentrantAudit {
            outside_guard: outside.clone(),
            control: fixture.cx.control().clone(),
        });
        let permit = grant
            .consume(&fixture.cx, &EffectRequest::Clock, &fixture.args)
            .unwrap();
        assert!(!outside.load(Ordering::SeqCst));
        drop(permit);
        assert!(outside.load(Ordering::SeqCst));
        assert_eq!(fixture.cx.control().usage().count(ControlKind::Queue), 0);
    }
}
