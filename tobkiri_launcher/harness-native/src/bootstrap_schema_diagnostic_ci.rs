//! Opt-in CI DATA observations, never schema selection or source authority.
use super::*;
use std::sync::{Mutex, MutexGuard, TryLockResult};
use tobkiri_canon::{AllocationError, AllocationKind, AllocationRequest, CoreRef};

// One start/end per declared tag, plus the single store entry.
pub(crate) const MAX_RECORDS: usize = SCHEMA_TAG_COUNT as usize * 2 + 1;
pub(crate) const SCHEMA_TAG_COUNT: u8 = 69;
pub(crate) const MAX_REPORT_JSON_BYTES: usize = 16 * 1024;
const SNAPSHOT_ATTEMPTS: u8 = 8;
const FORMAT: &str = "io.tobkiri.native-schema-checkpoint.v1";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Stage {
    CheckSchemaStart,
    CheckSchemaEnd,
    SchemaStoreEntry,
}
impl Stage {
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Self::CheckSchemaStart => "check-schema-start",
            Self::CheckSchemaEnd => "check-schema-end",
            Self::SchemaStoreEntry => "schema-store-entry",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) struct Marker {
    sequence: u8,
    stage: Stage,
    schema_tag: Option<u8>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct Record {
    pub(crate) sequence: u8,
    pub(crate) stage: Stage,
    pub(crate) schema_tag: Option<u8>,
    /// The real import-entry sample. None is unavailable, never zero/fallback.
    pub(crate) fuel_remaining: Option<u64>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct SnapshotData {
    /// Import-entry observations, not ACK, validation or physical IO receipts.
    pub(crate) records: [Option<Record>; MAX_RECORDS],
    pub(crate) count: u16,
    pub(crate) maximum_records: u16,
    pub(crate) schema_tag_count: u8,
    pub(crate) overflow: bool,
    /// A sample was unavailable or a diagnostic frame/storage was refused.
    pub(crate) unavailable: bool,
    pub(crate) attempt_limit: u8,
    pub(crate) attempts_remaining: u8,
}

// Charge bytes of both fixed passes (array initialization and row writes),
// two payload return moves, bounded scalar/CAS/loop overhead, and the fixed
// ASCII report's zero-init, encode, file-write and hash passes. These are
// original work debits, not a new
// execution allowance. No unused work is refunded or refilled.
const SNAPSHOT_WORK: u64 =
    (4 * size_of::<SnapshotData>() + MAX_RECORDS * 8 + 256 + MAX_REPORT_JSON_BYTES * 4) as u64;
const SNAPSHOT_BYTES: u64 = (size_of::<SnapshotData>() + MAX_REPORT_JSON_BYTES) as u64;

/// One owned, prepaid report packet. It deliberately has no Clone/Copy API.
/// Its original DATA reservation must survive borrowed reporting and is
/// released only when the caller drops the packet after reporting finishes.
pub(crate) struct Snapshot {
    data: SnapshotData,
    _funding: NativeSetupReservation,
}
impl Snapshot {
    /// Borrow observations while this packet still funds their fixed storage
    /// and at most MAX_REPORT_JSON_BYTES bytes of bounded ASCII report output.
    /// The data and reservation confer no source/currentness/effect authority.
    pub(crate) fn data(&self) -> &SnapshotData {
        &self.data
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum FrameFailure {
    Invalid,
    Overflow,
}

/// Non-checkpoint frames continue to the unchanged original catalog handler.
/// The caller already parsed this graph under its original DATA/work account.
pub(super) fn marker(frame: &CoreValue) -> std::result::Result<Option<Marker>, FrameFailure> {
    if frame
        .get_key("format")
        .and_then(|value| value.as_str().map(|v| v == FORMAT))
        != Some(true)
    {
        return Ok(None);
    }
    let object = frame.object().ok_or(FrameFailure::Invalid)?;
    if object.len() != 4
        || !object
            .iter()
            .all(|(key, _)| matches!(key, "format" | "sequence" | "stage" | "schema_tag"))
    {
        return Err(FrameFailure::Invalid);
    }
    let sequence = frame
        .get_key("sequence")
        .and_then(|value| value.as_i64())
        .ok_or(FrameFailure::Invalid)?;
    if sequence > MAX_RECORDS as i64 + 1 || sequence < 1 {
        return Err(FrameFailure::Invalid);
    }
    let stage = frame.get_key("stage").ok_or(FrameFailure::Invalid)?;
    let stage = match stage.as_str() {
        Some("check-schema-start") => Stage::CheckSchemaStart,
        Some("check-schema-end") => Stage::CheckSchemaEnd,
        Some("schema-store-entry") => Stage::SchemaStoreEntry,
        _ => return Err(FrameFailure::Invalid),
    };
    let tag = frame.get_key("schema_tag").ok_or(FrameFailure::Invalid)?;
    let schema_tag = match tag.inspect() {
        CoreRef::Null if stage == Stage::SchemaStoreEntry => None,
        CoreRef::Int(tag)
            if stage != Stage::SchemaStoreEntry
                && (0..i64::from(SCHEMA_TAG_COUNT)).contains(&tag) =>
        {
            Some(tag as u8)
        }
        _ => return Err(FrameFailure::Invalid),
    };
    // Only the declared, fully validated overflow notification is ACKed.
    if sequence == MAX_RECORDS as i64 + 1 {
        return Err(FrameFailure::Overflow);
    }
    Ok(Some(Marker {
        sequence: sequence as u8,
        stage,
        schema_tag,
    }))
}

// Field order is intentional: the actual Vec is freed before its reservation.
// Exact-capacity allocation follows the existing funded native Vec convention;
// an allocator exposing a different usable capacity is explicitly unsupported.
struct Records {
    values: Vec<Record>,
    _funding: NativeSetupReservation,
}
impl Records {
    fn new(invocation: &NativeSetupInvocation) -> Result<Self, NativeDataIoError> {
        let bytes = MAX_RECORDS
            .checked_mul(size_of::<Record>())
            .and_then(|bytes| u64::try_from(bytes).ok())
            .ok_or(NativeDataIoError::Capacity)?;
        let funding = invocation
            .reserve(CapacityKind::VfsBuffer, bytes)
            .map_err(|_| NativeDataIoError::Capacity)?;
        let mut values = Vec::new();
        values
            .try_reserve_exact(MAX_RECORDS)
            .map_err(|_| NativeDataIoError::Capacity)?;
        if values.capacity() != MAX_RECORDS {
            drop(values);
            return Err(NativeDataIoError::Unavailable);
        }
        Ok(Self {
            values,
            _funding: funding,
        })
    }
}

pub(super) struct Log {
    records: Option<Records>,
    permits: [Option<NativeSetupReservation>; SNAPSHOT_ATTEMPTS as usize],
    prepay_attempted: bool,
    overflow: bool,
    unavailable: bool,
}
impl Log {
    fn new() -> Self {
        Self {
            records: None,
            permits: [const { None }; SNAPSHOT_ATTEMPTS as usize],
            prepay_attempted: false,
            overflow: false,
            unavailable: false,
        }
    }

    pub(super) fn reject(&mut self, failure: FrameFailure) {
        self.overflow |= failure == FrameFailure::Overflow;
        self.unavailable = true;
    }

    fn record(
        &mut self,
        marker: Marker,
        fuel_remaining: Option<u64>,
        invocation: &NativeSetupInvocation,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<(), NativeDataIoError> {
        let result = self.record_inner(marker, fuel_remaining, invocation, budget);
        if result.is_err() {
            self.unavailable = true;
        }
        result
    }

    fn record_inner(
        &mut self,
        marker: Marker,
        fuel_remaining: Option<u64>,
        invocation: &NativeSetupInvocation,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<(), NativeDataIoError> {
        let first = self.records.is_none() && marker.sequence == 1;
        if first {
            if self.prepay_attempted {
                return Err(NativeDataIoError::Capacity);
            }
            // A failed prepayment is terminal even if capacity later returns.
            self.prepay_attempted = true;
        }
        budget
            .reserve(AllocationRequest {
                kind: AllocationKind::Temporary,
                bytes: 0,
                elements: 0,
                depth: 0,
                work: 1
                    + if self.records.is_none() {
                        MAX_RECORDS as u64
                    } else {
                        0
                    }
                    + if first {
                        SNAPSHOT_WORK * u64::from(SNAPSHOT_ATTEMPTS)
                    } else {
                        0
                    },
            })
            .map_err(|_| NativeDataIoError::Capacity)?;
        let count = self
            .records
            .as_ref()
            .map_or(0, |records| records.values.len());
        if count >= MAX_RECORDS {
            self.overflow = true;
            return Err(NativeDataIoError::Unavailable);
        }
        if usize::from(marker.sequence) != count + 1 {
            return Err(NativeDataIoError::Unavailable);
        }
        if self.records.is_none() {
            let mut permits = [const { None }; SNAPSHOT_ATTEMPTS as usize];
            for permit in &mut permits {
                *permit = Some(
                    invocation
                        .reserve(CapacityKind::VfsBuffer, SNAPSHOT_BYTES)
                        .map_err(|_| NativeDataIoError::Capacity)?,
                );
            }
            let records = Records::new(invocation)?;
            self.records = Some(records);
            self.permits = permits;
        }
        let records = self.records.as_mut().expect("funded checkpoint records");
        // The full bound was prepaid and its actual usable capacity checked;
        // no push is permitted to grow or truncate the actual storage.
        if records.values.len() >= records.values.capacity() {
            return Err(NativeDataIoError::Capacity);
        }
        records.values.push(Record {
            sequence: marker.sequence,
            stage: marker.stage,
            schema_tag: marker.schema_tag,
            fuel_remaining,
        });
        self.unavailable |= fuel_remaining.is_none();
        Ok(())
    }

    fn copy_data(&self, attempts_remaining: u8) -> SnapshotData {
        let mut snapshot = SnapshotData {
            records: [None; MAX_RECORDS],
            count: 0,
            maximum_records: MAX_RECORDS as u16,
            schema_tag_count: SCHEMA_TAG_COUNT,
            overflow: self.overflow,
            unavailable: self.unavailable,
            attempt_limit: SNAPSHOT_ATTEMPTS,
            attempts_remaining,
        };
        if let Some(records) = &self.records {
            snapshot.count = records.values.len() as u16;
            for (slot, record) in snapshot.records.iter_mut().zip(&records.values) {
                *slot = Some(*record);
            }
        }
        snapshot
    }
}

/// Fixed scalar failure metadata; no record DATA, fuel, or authority escapes.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct Status {
    pub(crate) overflow: bool,
    pub(crate) unavailable: bool,
    pub(crate) prepay_attempted: bool,
}

/// The attempt gate is outside the log mutex: busy/poisoned attempts also
/// spend one of the eight original prepaid attempts. No counter is refilled.
pub(super) struct Trace {
    attempts_remaining: AtomicU8,
    log: Mutex<Log>,
}
impl Trace {
    pub(super) fn new() -> Self {
        Self {
            attempts_remaining: AtomicU8::new(0),
            log: Mutex::new(Log::new()),
        }
    }

    pub(super) fn try_lock(&self) -> TryLockResult<MutexGuard<'_, Log>> {
        self.log.try_lock()
    }

    pub(super) fn status(&self) -> Option<Status> {
        let log = self.try_lock().ok()?;
        Some(Status {
            overflow: log.overflow,
            unavailable: log.unavailable,
            prepay_attempted: log.prepay_attempted,
        })
    }

    fn record(
        &self,
        marker: Marker,
        fuel_remaining: Option<u64>,
        invocation: &NativeSetupInvocation,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<(), NativeDataIoError> {
        let mut log = self
            .try_lock()
            .map_err(|_| NativeDataIoError::Unavailable)?;
        let publish = log.records.is_none() && !log.prepay_attempted && marker.sequence == 1;
        log.record(marker, fuel_remaining, invocation, budget)?;
        if publish {
            // Publish only after the first actual record and every original
            // work/memory prepayment succeeded; later markers never refill it.
            self.attempts_remaining
                .store(SNAPSHOT_ATTEMPTS, Ordering::Release);
        }
        Ok(())
    }

    fn snapshot(&self) -> Option<Snapshot> {
        let mut current = self.attempts_remaining.load(Ordering::Acquire);
        let mut claimed = None;
        // Strong CAS has no spurious failure. Once published, this counter
        // only decreases eight times, so even contending readers have a
        // fixed retry bound before any mutex attempt or payload copy.
        for _ in 0..=SNAPSHOT_ATTEMPTS {
            if current == 0 {
                return None;
            }
            match self.attempts_remaining.compare_exchange(
                current,
                current - 1,
                Ordering::AcqRel,
                Ordering::Acquire,
            ) {
                Ok(_) => {
                    claimed = Some(current);
                    break;
                }
                Err(actual) => current = actual,
            }
        }
        let before = claimed?;
        let slot = usize::from(SNAPSHOT_ATTEMPTS.checked_sub(before)?);
        let mut log = self.try_lock().ok()?;
        let funding = log.permits.get_mut(slot)?.take()?;
        let data = log.copy_data(before - 1);
        Some(Snapshot {
            data,
            _funding: funding,
        })
    }
}

pub(super) fn snapshot(trace: &Trace) -> Option<Snapshot> {
    trace.snapshot()
}

pub(super) fn prepare<S: BootstrapInputOwner>(
    context: NativeImportContext<'_, State<S>>,
    work: ControlHandle<Work<S>>,
    marker: Marker,
) -> Result<NativeImportOperation> {
    prepare_event(context, work, Some(marker))
}

pub(super) fn prepare_overflow<S: BootstrapInputOwner>(
    context: NativeImportContext<'_, State<S>>,
    work: ControlHandle<Work<S>>,
) -> Result<NativeImportOperation> {
    prepare_event(context, work, None)
}

fn prepare_event<S: BootstrapInputOwner>(
    context: NativeImportContext<'_, State<S>>,
    work: ControlHandle<Work<S>>,
    marker: Option<Marker>,
) -> Result<NativeImportOperation> {
    if !context.same_invocation(&work.get().invocation)
        || work.get().phase.load(Ordering::Acquire) != CATALOG_LOAD
    {
        bail!("schema checkpoint is outside the same catalog operation");
    }
    let fuel_remaining = context.ci_fuel_at_import_entry();
    let custody = work.project(|value| value as &(dyn std::any::Any + Send + Sync));
    context.inspect_retaining(custody, move |output| {
        let state = work.get();
        if !state.invocation.same_invocation(output.invocation())
            || state.phase.load(Ordering::Acquire) != CATALOG_LOAD
        {
            return Err(NativeDataIoError::Retired);
        }
        if let Some(marker) = marker {
            state
                .ci_schema_trace
                .record(
                    marker,
                    fuel_remaining,
                    output.invocation(),
                    output.allocation_owners().graph(),
                )
                .map_err(|error| output.reject_execution(error))?;
        } else {
            state
                .ci_schema_trace
                .try_lock()
                .map_err(|_| NativeDataIoError::Unavailable)?
                .reject(FrameFailure::Overflow);
        }
        output.append(b"{}")
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;
    use std::sync::atomic::{AtomicBool, AtomicUsize};
    use tobkiri_canon::Digest;
    use tobkiri_gate::{
        CapacityLedger, CoreControl, GateError, NATIVE_SETUP_MEMORY_BYTES, NativeSetupAccount,
        NativeSetupLease,
    };

    struct Portable;
    impl NativeSetupLease for Portable {
        fn validate(&self) -> std::result::Result<(), GateError> {
            Ok(())
        }
        fn allocation_domain(&self) -> Digest {
            Digest::hash(b"schema-checkpoint-mechanics-not-selected-source")
        }
    }
    fn funding() -> (NativeSetupAccount, NativeSetupInvocation) {
        let account = CapacityLedger::new()
            .native_setup_account(&CoreControl::default(), || Ok(Arc::new(Portable)))
            .unwrap();
        let invocation = account.begin().unwrap();
        (account, invocation)
    }
    fn with_budget(run: impl FnOnce(&NativeSetupInvocation, &Arc<dyn AllocationBudget>)) {
        let (account, invocation) = funding();
        let engine = WasmEngine::new_with_control(invocation.control()).unwrap();
        let store = engine.native_setup_store(invocation.clone(), ()).unwrap();
        let budget = store.allocation_owners().graph().clone();
        run(&invocation, &budget);
        drop(budget);
        drop(store);
        tobkiri_wasm::reap_allocation_owners().unwrap();
        assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
        assert_eq!(account.snapshot().unwrap().reservations, 0);
    }
    fn decode(
        bytes: &str,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Option<Marker>, FrameFailure> {
        match CoreValue::parse_guest(bytes.as_bytes(), budget) {
            Ok(frame) => marker(&frame),
            Err(AllocationError::Invalid) => Err(FrameFailure::Invalid),
            Err(error) => panic!("bounded frame test lost its real allocation: {error:?}"),
        }
    }

    #[tokio::test(flavor = "current_thread")]
    async fn ci_schema_checkpoint_frames_have_exact_shape_types_and_closed_bounds() {
        with_budget(|_, budget| {
            let valid = decode(
                r#"{"format":"io.tobkiri.native-schema-checkpoint.v1","sequence":139,"stage":"check-schema-end","schema_tag":68}"#,
                budget,
            ).unwrap().unwrap();
            assert_eq!(valid.sequence, MAX_RECORDS as u8);
            assert_eq!(valid.stage.as_str(), "check-schema-end");
            assert_eq!(valid.schema_tag, Some(68));
            let store = decode(
                r#"{"format":"io.tobkiri.native-schema-checkpoint.v1","sequence":1,"stage":"schema-store-entry","schema_tag":null}"#,
                budget,
            ).unwrap().unwrap();
            assert_eq!(store.schema_tag, None);
            assert_eq!(store.stage.as_str(), "schema-store-entry");
            assert!(
                decode(
                    r#"{"format":"io.tobkiri.native-inspection-request.v1"}"#,
                    budget
                )
                .unwrap()
                .is_none()
            );
            for invalid in [
                r#"{"format":"io.tobkiri.native-schema-checkpoint.v1","sequence":true,"stage":"check-schema-start","schema_tag":0}"#,
                r#"{"format":"io.tobkiri.native-schema-checkpoint.v1","sequence":1.0,"stage":"check-schema-start","schema_tag":0}"#,
                r#"{"format":"io.tobkiri.native-schema-checkpoint.v1","sequence":0,"stage":"check-schema-start","schema_tag":0}"#,
                r#"{"format":"io.tobkiri.native-schema-checkpoint.v1","sequence":1,"stage":"unknown","schema_tag":0}"#,
                r#"{"format":"io.tobkiri.native-schema-checkpoint.v1","sequence":1,"stage":"check-schema-start","schema_tag":null}"#,
                r#"{"format":"io.tobkiri.native-schema-checkpoint.v1","sequence":1,"stage":"schema-store-entry","schema_tag":0}"#,
                r#"{"format":"io.tobkiri.native-schema-checkpoint.v1","sequence":1,"stage":"check-schema-end","schema_tag":69}"#,
                r#"{"format":"io.tobkiri.native-schema-checkpoint.v1","sequence":1,"stage":"check-schema-end","schema_tag":-1}"#,
                r#"{"format":"io.tobkiri.native-schema-checkpoint.v1","sequence":1,"stage":"check-schema-end","schema_tag":false}"#,
                r#"{"format":"io.tobkiri.native-schema-checkpoint.v1","sequence":1,"stage":"check-schema-end","schema_tag":0,"fuel":100}"#,
                r#"{"format":"io.tobkiri.native-schema-checkpoint.v1","sequence":1,"stage":"check-schema-end"}"#,
                r#"{"format":"io.tobkiri.native-schema-checkpoint.v1","sequence":140,"stage":"unknown","schema_tag":0}"#,
                r#"{"format":"io.tobkiri.native-schema-checkpoint.v1","sequence":140,"stage":"check-schema-end","schema_tag":69}"#,
                r#"{"format":"io.tobkiri.native-schema-checkpoint.v1","sequence":141,"stage":"check-schema-end","schema_tag":0}"#,
            ] {
                assert_eq!(decode(invalid, budget), Err(FrameFailure::Invalid));
            }
            assert_eq!(
                decode(
                    r#"{"format":"io.tobkiri.native-schema-checkpoint.v1","sequence":140,"stage":"check-schema-end","schema_tag":0}"#,
                    budget,
                ),
                Err(FrameFailure::Overflow)
            );
            assert!(CoreValue::parse_guest(
                br#"{"format":"io.tobkiri.native-schema-checkpoint.v1","sequence":1,"sequence":2,"stage":"check-schema-end","schema_tag":0}"#,
                budget,
            ).is_err());
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn ci_schema_checkpoint_sequence_fuel_availability_and_payload_custody_are_bounded() {
        with_budget(|invocation, budget| {
            let before = invocation.capacity_snapshot().unwrap();
            let trace = Trace::new();
            let event = Marker {
                sequence: 2,
                stage: Stage::CheckSchemaStart,
                schema_tag: Some(0),
            };
            assert!(snapshot(&trace).is_none());
            assert!(trace.record(event, Some(99), invocation, budget).is_err());
            assert!(trace.try_lock().unwrap().records.is_none());
            assert!(snapshot(&trace).is_none());
            assert_eq!(
                invocation.capacity_snapshot().unwrap().charged_bytes,
                before.charged_bytes
            );
            trace
                .record(
                    Marker {
                        sequence: 1,
                        ..event
                    },
                    Some(999),
                    invocation,
                    budget,
                )
                .unwrap();
            let first = snapshot(&trace).unwrap();
            assert_eq!(first.data().count, 1);
            assert_eq!(first.data().attempt_limit, 8);
            assert_eq!(first.data().attempts_remaining, 7);
            for sequence in 2..=MAX_RECORDS {
                let event = Marker {
                    sequence: sequence as u8,
                    ..event
                };
                let fuel = if sequence == 2 {
                    None
                } else {
                    Some(1000 - sequence as u64)
                };
                trace.record(event, fuel, invocation, budget).unwrap();
            }
            let payload = (MAX_RECORDS * size_of::<Record>()) as u64;
            {
                let log = trace.try_lock().unwrap();
                let records = log.records.as_ref().unwrap();
                assert_eq!(records.values.capacity(), MAX_RECORDS);
                assert_eq!(records._funding.allocated_bytes().unwrap(), payload);
                assert_eq!(trace.attempts_remaining.load(Ordering::Acquire), 7);
            }
            assert_eq!(
                invocation.capacity_snapshot().unwrap().charged_bytes,
                before.charged_bytes + payload + u64::from(SNAPSHOT_ATTEMPTS) * SNAPSHOT_BYTES
            );
            let observed = snapshot(&trace).unwrap();
            assert_eq!(observed.data().count, MAX_RECORDS as u16);
            assert_eq!(observed.data().maximum_records, MAX_RECORDS as u16);
            assert_eq!(observed.data().schema_tag_count, 69);
            assert_eq!(observed.data().records[0].unwrap().sequence, 1);
            assert_eq!(
                observed.data().records[0].unwrap().stage.as_str(),
                "check-schema-start"
            );
            assert_eq!(observed.data().records[1].unwrap().fuel_remaining, None);
            assert_eq!(
                observed.data().records[2].unwrap().fuel_remaining,
                Some(997)
            );
            assert!(observed.data().unavailable && !observed.data().overflow);
            assert!(
                trace
                    .record(
                        Marker {
                            sequence: (MAX_RECORDS + 1) as u8,
                            ..event
                        },
                        Some(0),
                        invocation,
                        budget
                    )
                    .is_err()
            );
            let full = snapshot(&trace).unwrap();
            assert!(full.data().overflow && full.data().unavailable);
            assert_eq!(full.data().count, MAX_RECORDS as u16);
            assert_eq!(full.data().records, observed.data().records);
            for remaining in (0..5).rev() {
                let packet = snapshot(&trace).unwrap();
                assert_eq!(packet.data().attempts_remaining, remaining);
                drop(packet);
            }
            assert!(snapshot(&trace).is_none());
            drop(trace);
            assert_eq!(
                invocation.capacity_snapshot().unwrap().charged_bytes,
                before.charged_bytes + 3 * SNAPSHOT_BYTES
            );
            // Owned packets keep their original payload/report slots after
            // the entire log has dropped; borrowed data is still reportable.
            assert_eq!(
                observed.data().records[2].unwrap().fuel_remaining,
                Some(997)
            );
            assert_eq!(full._funding.allocated_bytes().unwrap(), SNAPSHOT_BYTES);
            drop(first);
            drop(observed);
            assert_eq!(
                invocation.capacity_snapshot().unwrap().charged_bytes,
                before.charged_bytes + SNAPSHOT_BYTES
            );
            drop(full);
            assert_eq!(
                invocation.capacity_snapshot().unwrap().charged_bytes,
                before.charged_bytes
            );
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn ci_schema_checkpoint_capacity_refusal_and_busy_snapshot_never_claim_completion() {
        with_budget(|invocation, budget| {
            let before = invocation.capacity_snapshot().unwrap();
            let trace = Trace::new();
            let event = Marker {
                sequence: 1,
                stage: Stage::SchemaStoreEntry,
                schema_tag: None,
            };
            assert!(snapshot(&trace).is_none());
            trace.record(event, Some(10), invocation, budget).unwrap();
            let guard = trace.try_lock().unwrap();
            for remaining in (0..SNAPSHOT_ATTEMPTS).rev() {
                assert!(snapshot(&trace).is_none());
                assert_eq!(trace.attempts_remaining.load(Ordering::Acquire), remaining);
            }
            drop(guard);
            assert!(snapshot(&trace).is_none());
            // A later valid marker cannot refill even busy-consumed attempts.
            trace
                .record(
                    Marker {
                        sequence: 2,
                        ..event
                    },
                    Some(9),
                    invocation,
                    budget,
                )
                .unwrap();
            assert!(snapshot(&trace).is_none());
            drop(trace);
            assert_eq!(
                invocation.capacity_snapshot().unwrap().charged_bytes,
                before.charged_bytes
            );
            let failed = Trace::new();
            let spare = 2 * SNAPSHOT_BYTES + 1;
            let exhausted = invocation
                .reserve(
                    CapacityKind::VfsBuffer,
                    NATIVE_SETUP_MEMORY_BYTES - before.charged_bytes - spare,
                )
                .unwrap();
            assert!(failed.record(event, None, invocation, budget).is_err());
            assert_eq!(
                invocation.capacity_snapshot().unwrap().charged_bytes,
                NATIVE_SETUP_MEMORY_BYTES - spare
            );
            {
                let log = failed.try_lock().unwrap();
                assert!(log.prepay_attempted && log.unavailable);
                assert!(log.records.is_none() && log.permits.iter().all(Option::is_none));
            }
            assert!(snapshot(&failed).is_none());
            assert_eq!(
                failed.status(),
                Some(Status {
                    overflow: false,
                    unavailable: true,
                    prepay_attempted: true,
                })
            );
            drop(exhausted);
            // Partial slot admission was freed, but failed prepayment cannot
            // retry or issue a fresh quota when original capacity returns.
            assert!(failed.record(event, Some(8), invocation, budget).is_err());
            assert!(snapshot(&failed).is_none());
            assert_eq!(
                invocation.capacity_snapshot().unwrap().charged_bytes,
                before.charged_bytes
            );
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn ci_schema_checkpoint_writer_preserves_full_rows_hash_and_funding() {
        use sha2::{Digest as _, Sha256};
        with_budget(|invocation, budget| {
            let trace = Trace::new();
            for sequence in 1..=MAX_RECORDS {
                trace
                    .record(
                        Marker {
                            sequence: sequence as u8,
                            stage: Stage::CheckSchemaStart,
                            schema_tag: Some(SCHEMA_TAG_COUNT - 1),
                        },
                        Some(u64::MAX),
                        invocation,
                        budget,
                    )
                    .unwrap();
            }
            let packet = snapshot(&trace).unwrap();
            let before = invocation.capacity_snapshot().unwrap().charged_bytes;
            let directory = tempfile::tempdir().unwrap();
            let path = directory.path().join("trace.json");
            let metadata = super::super::catalog_actual_ci::write_schema_trace(
                &path,
                Some(packet),
                trace.status(),
            )
            .unwrap();
            assert_eq!(
                invocation.capacity_snapshot().unwrap().charged_bytes,
                before - SNAPSHOT_BYTES
            );
            let bytes = std::fs::read(path).unwrap();
            assert!(bytes.len() <= MAX_REPORT_JSON_BYTES);
            assert_eq!(metadata["bytes"], bytes.len());
            assert_eq!(metadata["sha256"], format!("{:x}", Sha256::digest(&bytes)));
            let report: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
            assert_eq!(report["count"], MAX_RECORDS);
            assert_eq!(report["records"].as_array().unwrap().len(), MAX_RECORDS);
            for (index, row) in report["records"].as_array().unwrap().iter().enumerate() {
                assert_eq!(row["sequence"], index + 1);
                assert_eq!(row["stage"], "check-schema-start");
                assert_eq!(row["schema_tag"], SCHEMA_TAG_COUNT - 1);
                assert_eq!(row["fuel_remaining"], u64::MAX);
            }
            assert_eq!(report["qualification"], false);
            assert_eq!(report["overflow"], false);
            assert_eq!(report["unavailable"], false);
        });
    }

    #[test]
    fn ci_schema_checkpoint_rejection_before_first_record_has_scalar_status() {
        let trace = Trace::new();
        assert_eq!(
            trace.status(),
            Some(Status {
                overflow: false,
                unavailable: false,
                prepay_attempted: false,
            })
        );
        trace.try_lock().unwrap().reject(FrameFailure::Overflow);
        assert!(snapshot(&trace).is_none());
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("unused.json");
        let metadata =
            super::super::catalog_actual_ci::write_schema_trace(&path, None, trace.status())
                .unwrap();
        assert_eq!(metadata["available"], false);
        assert_eq!(metadata["status_available"], true);
        assert_eq!(metadata["unavailable"], true);
        assert_eq!(metadata["overflow"], true);
        assert_eq!(metadata["prepay_attempted"], false);
        assert!(!path.exists());
    }

    struct Source {
        root_reads: Arc<AtomicUsize>,
        dropped: Arc<AtomicBool>,
    }
    impl Drop for Source {
        fn drop(&mut self) {
            self.dropped.store(true, Ordering::Release);
        }
    }
    impl BootstrapInputOwner for Source {
        fn with_roots<R>(
            &self,
            run: impl for<'a> FnOnce(crate::BootstrapRoots<'a>) -> R,
        ) -> Result<R, NativeDataIoError> {
            self.root_reads.fetch_add(1, Ordering::Relaxed);
            Ok(run(crate::BootstrapRoots {
                runtime: Path::new("/fixture-only"),
                working_directory: Path::new("/fixture-only"),
                development_workspace: None,
            }))
        }
        fn check_live(&self) -> Result<(), NativeDataIoError> {
            Ok(())
        }
    }

    // This hostile mechanics fixture shares the real fixed-import/Store path.
    // It does not execute the original catalog or establish selected source.
    fn component(engine: &WasmEngine, payload: &str) -> Component {
        let escaped: String = payload
            .bytes()
            .map(|byte| format!("\\{byte:02x}"))
            .collect();
        let source = r#"(component
          (import "tobkiri-native:harness/bindings@1.0.0" (instance $native
            (export "inventory" (func (result string)))
            (export "inspect" (func (param "payload" string) (result string)))))
          (alias export $native "inspect" (func $inspect))
          (type $code (enum "not-initialized" "already-initialized" "invalid-frame" "invalid-reference" "unsupported-value" "method-raised"))
          (type $error (record (field "code" $code) (field "message" string) (field "details" string)))
          (type $unit (result (error $error))) (type $text (result string (error $error)))
          (core module $memory
            (memory (export "memory") 1)
            (global $next (mut i32) (i32.const 8192))
            (data (i32.const 2048) "@PAYLOAD@")
            (func (export "realloc") (param i32 i32 i32 i32) (result i32)
              local.get 0 local.get 1 i32.or if unreachable end
              local.get 2 i32.const 1 i32.ne if unreachable end
              global.get $next global.get $next local.get 3 i32.add global.set $next))
          (core instance $mi (instantiate $memory))
          (alias core export $mi "memory" (core memory $memory))
          (alias core export $mi "realloc" (core func $realloc))
          (core func $inspect-lowered (canon lower (func $inspect) (memory $memory) (realloc $realloc)))
          (core module $run
            (import "m" "memory" (memory 1))
            (import "m" "inspect" (func $inspect (param i32 i32 i32)))
            (func (export "init") (param i32 i32) (result i32) i32.const 128)
            (func (export "text") (param i32 i32) (result i32)
              i32.const 2048 i32.const @LENGTH@ i32.const 64 call $inspect
              i32.const 132 i32.const 64 i32.load i32.store
              i32.const 136 i32.const 68 i32.load i32.store
              i32.const 128))
          (core instance $ri (instantiate $run (with "m" (instance
            (export "memory" (memory $memory)) (export "inspect" (func $inspect-lowered))))))
          (alias core export $ri "init" (core func $init))
          (alias core export $ri "text" (core func $text))
          (func $init (param "payload" string) (result $unit) (canon lift (core func $init) (memory $memory) (realloc $realloc)))
          (func $text (param "payload" string) (result $text) (canon lift (core func $text) (memory $memory) (realloc $realloc)))
          (instance $runtime
            (export "error-code" (type $code)) (export "error" (type $error))
            (export "init" (func $init)) (export "bootstrap-profile-id" (func $text))
            (export "host-resource-root" (func $text)) (export "bundled-profile-root" (func $text))
            (export "denied" (func $text)) (export "load-catalog" (func $text))
            (export "resolve-profile" (func $text)) (export "bootstrap-confirmation" (func $text))
            (export "catalog-with-profiles" (func $text)) (export "setup-listing" (func $text)))
          (instance $transport
            (export "error-code" (type $code)) (export "error" (type $error))
            (export "describe-value" (func $text)) (export "import-values" (func $text)))
          (export "tobkiri-ui:harness-native/runtime@1.0.0" (instance $runtime))
          (export "tobkiri-ui:harness-native/transport@1.0.0" (instance $transport)))"#
            .replace("@PAYLOAD@", &escaped).replace("@LENGTH@", &payload.len().to_string());
        Component::new(engine.engine(), wat::parse_str(source).unwrap()).unwrap()
    }

    #[tokio::test(flavor = "current_thread")]
    async fn ci_schema_checkpoint_real_overflow_ack_and_parse_failure_status() {
        static INDEX: BootstrapModuleIndex = BootstrapModuleIndex {
            sha256: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            modules: &[crate::BootstrapModule {
                module: "fixture",
                relative_file: "fixture.py",
            }],
        };
        for (payload, overflow) in [
            (
                r#"{"format":"io.tobkiri.native-schema-checkpoint.v1","sequence":140,"stage":"check-schema-start","schema_tag":0}"#,
                true,
            ),
            ("{", false),
        ] {
            let (account, invocation) = funding();
            let engine = WasmEngine::new_with_control(invocation.control()).unwrap();
            let component = component(&engine, payload);
            let source = Source {
                root_reads: Arc::new(AtomicUsize::new(0)),
                dropped: Arc::new(AtomicBool::new(false)),
            };
            let mut runtime =
                NativeBootstrap::prepare(&engine, &component, invocation.clone(), source, &INDEX)
                    .await
                    .unwrap()
                    .unwrap();
            runtime
                .work
                .get()
                .phase
                .store(CATALOG_LOAD, Ordering::Release);
            let input = runtime.call.clone();
            let result = runtime.string_call_in_phase(1, &input, CATALOG_LOAD).await;
            if overflow {
                assert_eq!(result.unwrap().unwrap().as_bytes(), b"{}");
                // Subsequent original work can still use the same live Store.
                assert!(runtime.fuel_remaining().is_ok());
            } else {
                assert!(result.is_err());
                drop(result);
            }
            assert!(runtime.ci_schema_trace().is_none());
            assert_eq!(
                runtime.ci_schema_status(),
                Some(Status {
                    overflow,
                    unavailable: true,
                    prepay_attempted: false,
                })
            );
            drop(input);
            runtime.finish().await.unwrap();
            drop(invocation);
            tobkiri_wasm::reap_allocation_owners().unwrap();
            assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
            assert_eq!(account.snapshot().unwrap().reservations, 0);
        }
    }

    #[tokio::test(flavor = "current_thread")]
    async fn ci_schema_checkpoint_real_import_refuses_replay_wrong_phase_and_nontrace_fallback() {
        static INDEX: BootstrapModuleIndex = BootstrapModuleIndex {
            sha256: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            modules: &[crate::BootstrapModule {
                module: "fixture",
                relative_file: "fixture.py",
            }],
        };
        let valid = r#"{"format":"io.tobkiri.native-schema-checkpoint.v1","sequence":1,"stage":"check-schema-start","schema_tag":68}"#;
        for (payload, phase) in [
            (valid, CATALOG_LOAD),
            (valid, READY),
            (
                r#"{"format":"io.tobkiri.native-inspection-request.v1"}"#,
                CATALOG_LOAD,
            ),
        ] {
            let (account, invocation) = funding();
            let engine = WasmEngine::new_with_control(invocation.control()).unwrap();
            let component = component(&engine, payload);
            let root_reads = Arc::new(AtomicUsize::new(0));
            let dropped = Arc::new(AtomicBool::new(false));
            let source = Source {
                root_reads: root_reads.clone(),
                dropped: dropped.clone(),
            };
            let mut runtime =
                NativeBootstrap::prepare(&engine, &component, invocation.clone(), source, &INDEX)
                    .await
                    .unwrap()
                    .unwrap();
            let reads = root_reads.load(Ordering::Relaxed);
            // The fixture alone installs a phase; no handler advances it or
            // claims that this test has run the real catalog composition.
            runtime.work.get().phase.store(phase, Ordering::Release);
            let before_fuel = runtime.fuel_remaining().unwrap();
            assert!(runtime.ci_schema_trace().is_none());
            let input = runtime.call.clone();
            let called = runtime.string_call_in_phase(1, &input, phase).await;
            let mut held_reply = None;
            let held_outer;
            let held_packets;
            if phase == CATALOG_LOAD && payload == valid {
                let reply = called.unwrap().unwrap();
                assert_eq!(reply.as_bytes(), b"{}");
                let trace = runtime.ci_schema_trace().unwrap();
                assert_eq!(trace.data().count, 1);
                assert!(!trace.data().unavailable && !trace.data().overflow);
                let record = trace.data().records[0].unwrap();
                assert_eq!(record.sequence, 1);
                assert_eq!(record.schema_tag, Some(68));
                assert_eq!(record.stage, Stage::CheckSchemaStart);
                let after_fuel = runtime.fuel_remaining().unwrap();
                assert!(
                    record
                        .fuel_remaining
                        .is_some_and(|fuel| after_fuel <= fuel && fuel < before_fuel)
                );
                held_reply = Some(reply);
                held_outer = runtime
                    .string_call_in_phase(1, &input, phase)
                    .await
                    .err()
                    .expect("checkpoint replay became success");
                // The unchanged live budget rejects the retired Store. The
                // owned getter must use its already debited fixed quota only.
                assert!(
                    runtime
                        .input_budget
                        .reserve(AllocationRequest {
                            kind: AllocationKind::Temporary,
                            bytes: 0,
                            elements: 0,
                            depth: 0,
                            work: 0,
                        })
                        .is_err()
                );
                let refused = runtime.ci_schema_trace().unwrap();
                assert_eq!(refused.data().count, 1);
                assert!(refused.data().unavailable && !refused.data().overflow);
                assert_eq!(refused.data().records, trace.data().records);
                held_packets = (Some(trace), Some(refused));
            } else {
                let error = called
                    .err()
                    .expect("unbound/wrong-phase import became success");
                if phase == CATALOG_LOAD {
                    assert!(format!("{error:#}").contains("catalog source caller is not bound"));
                } else {
                    assert!(format!("{error:#}").contains("native inspection is unavailable"));
                }
                assert!(runtime.ci_schema_trace().is_none());
                held_packets = (None, None);
                held_outer = error;
            }
            assert_eq!(root_reads.load(Ordering::Relaxed), reads);
            assert!(runtime.original_review_uncertain_cleanup().is_none());
            assert!(runtime.fuel_remaining().is_err());
            runtime.finish().await.unwrap();
            drop(held_reply);
            drop(held_outer);
            drop(input);
            drop(invocation);
            tobkiri_wasm::reap_allocation_owners().unwrap();
            assert!(dropped.load(Ordering::Acquire));
            let (first, last) = held_packets;
            if let (Some(first), Some(last)) = (&first, &last) {
                // Work/Source and the real IO are gone; these two original
                // DATA/report permits alone remain until reporting finishes.
                assert_eq!(
                    account.snapshot().unwrap().charged_bytes,
                    2 * SNAPSHOT_BYTES
                );
                assert_eq!(account.snapshot().unwrap().reservations, 2);
                assert_eq!(first.data().maximum_records, MAX_RECORDS as u16);
                assert_eq!(last.data().count, 1);
                assert_eq!(last._funding.allocated_bytes().unwrap(), SNAPSHOT_BYTES);
                assert!(last.data().unavailable);
            }
            drop(first);
            drop(last);
            tobkiri_wasm::reap_allocation_owners().unwrap();
            assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
            assert_eq!(account.snapshot().unwrap().reservations, 0);
        }
    }
}
