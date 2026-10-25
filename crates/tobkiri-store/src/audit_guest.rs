//! Owned guest-facing audit adapter for the Store boundary.
//!
//! Flow: `GuestAuditAppend::prepare`/`GuestAuditRead::prepare` produce owned,
//! caller-funded argument data plus the exact authorize/consume args digest.
//! Preparation is data only: no SQL, authority or approval/frame proof. The
//! caller passes `request()`/`args_digest()` to `Gate::authorize` itself, then
//! `consume` spends the real `Grant` exactly once through
//! `Grant::consume_owned`. `BeforeConsumed`, `Cleanup` and `CommitUnknown`
//! attempts keep their real Gate custody for existing reconcile/settle paths
//! and are never executable; only a real `ConsumedAttempt::into_dispatch`
//! permit yields `Ready(GuestAuditDispatch)`.
//!
//! The exact later owner is the phase-B Wasm import supervisor (a
//! `NativeWorkSpawner`-accepted native future): it takes these carriers BY
//! VALUE, calls `GuestAuditDispatch::execute` synchronously inside its own
//! generation, and retains the returned `GuestAuditDelivery` — the same
//! `DispatchPermit`, original reservation custody and truthful committed
//! state — through physical completion, owner-backed component conversion and
//! the normal `record_outcome`/`settle_deadline`/outbox-acknowledgement
//! terminal path. A committed append whose result is then lost (encode,
//! cancel, deadline or terminal-ACK failure) keeps the existing fixed
//! `unavailable` failure through `terminal_outcome`: never success, no-write,
//! replay or refund. Store installs no task factory and performs no async-Drop
//! terminal work.

use super::AuditLog;
use crate::StoreError;
use std::{sync::Arc, time::Instant};
use tobkiri_canon::{
    AllocationBudget, AllocationError, AllocationKind, AllocationRequest, CoreValue, Digest,
    MAX_SAFE_INTEGER,
};
use tobkiri_gate::{
    CallCx, CleanupOnlyConsumption, ConsumptionAttempt, DispatchPermit, EffectRequest, GateError,
    Grant, GrantId, IntegrityFaultCode, Outcome, ResourceLimits, UnknownConsumption,
};

#[path = "audit_guest_owned.rs"]
mod audit_guest_owned;

#[path = "audit_guest_scan.rs"]
mod audit_guest_scan;

fn map_alloc(error: AllocationError) -> StoreError {
    match error {
        AllocationError::Limit | AllocationError::Depth | AllocationError::Overflow => {
            StoreError::Quota
        }
        AllocationError::Unavailable | AllocationError::Allocator => {
            StoreError::Unavailable("host allocation owner".into())
        }
        AllocationError::Invalid => StoreError::Invalid("guest value is not strict JSON".into()),
    }
}
fn integrity() -> StoreError {
    StoreError::Integrity(IntegrityFaultCode::AuditChain)
}
fn outcome_code(error: &StoreError) -> &'static str {
    match error {
        StoreError::Denied(_) => "denied",
        StoreError::Invalid(_) => "invalid",
        StoreError::NotFound => "not-found",
        StoreError::Conflict(_) => "conflict",
        StoreError::Quota => "limit-exceeded",
        StoreError::Unavailable(_)
        | StoreError::Io(_)
        | StoreError::Sql(_)
        | StoreError::CommitUnknown
        | StoreError::PendingDataMove
        | StoreError::ProfileCommitUncertain { .. }
        | StoreError::Canon(_) => "unavailable",
        StoreError::Integrity(_) => "persistence",
        StoreError::Gate(error) => error.code(),
    }
}

fn decimal_value(
    value: u64,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreValue, AllocationError> {
    // At most twenty zeroed stack bytes, twenty digit iterations and a
    // twenty-byte UTF8 pass, before CoreValue funds its own string copy.
    budget.reserve(AllocationRequest {
        kind: AllocationKind::String,
        bytes: 0,
        elements: 1,
        depth: 0,
        work: 60,
    })?;
    let mut buf = [0u8; 20];
    let mut at = buf.len();
    let mut value = value;
    loop {
        at -= 1;
        buf[at] = b'0' + (value % 10) as u8;
        value /= 10;
        if value == 0 {
            break;
        }
    }
    let text = std::str::from_utf8(&buf[at..]).map_err(|_| AllocationError::Invalid)?;
    CoreValue::string(text, budget)
}

/// One owned helper for the authorize/consume args digest: the canonical
/// two-element array hashed from exact StrictV1 bytes, matching the legacy
/// tuple recipe for the same inputs.
fn owned_args(
    items: [CoreValue; 2],
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Digest, StoreError> {
    let values: [Result<CoreValue, AllocationError>; 2] =
        [Ok(items[0].share()), Ok(items[1].share())];
    CoreValue::array_shared(values.into_iter(), budget)
        .map_err(map_alloc)?
        .digest_guest(budget)
        .map_err(map_alloc)
}

/// Owned `AuditAppend` preparation. Data only: the caller obtains the normal
/// grant with `request()`/`args_digest()` and hands it to `consume`.
pub struct GuestAuditAppend {
    cx: CallCx,
    kind: CoreValue,
    payload: CoreValue,
    args: Digest,
    budget: Arc<dyn AllocationBudget>,
}
impl GuestAuditAppend {
    /// Keep the owned caller `kind`/`payload` and compute the
    /// legacy-compatible canonical `[kind, payload]` args digest.
    pub fn prepare(
        cx: &CallCx,
        kind: CoreValue,
        payload: CoreValue,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Self, StoreError> {
        if kind.as_str().is_none() {
            return Err(StoreError::Invalid(
                "audit guest kind must be a string".into(),
            ));
        }
        let args = owned_args([kind.share(), payload.share()], budget)?;
        Ok(Self {
            cx: cx.clone(),
            kind,
            payload,
            args,
            budget: budget.clone(),
        })
    }
    pub fn request(&self) -> EffectRequest {
        EffectRequest::AuditAppend
    }
    pub fn args_digest(&self) -> &Digest {
        &self.args
    }
    /// Consume `grant` exactly once; failure carriers stay owned, never execute.
    pub fn consume(self, grant: Grant) -> GuestAuditAttempt {
        let (cx, request, args) = (self.cx.clone(), self.request(), self.args.clone());
        consume_with(grant, &cx, &request, &args, GuestPrepared::Append(self))
    }
}

/// Owned `AuditRead` preparation: the exact original u64 cursor, the row
/// bound and the additive `[normalized_after, max]` args digest (integer at
/// or below the safe-integer boundary, exact unsigned decimal string above).
pub struct GuestAuditRead {
    cx: CallCx,
    after: u64,
    max: u32,
    args: Digest,
    budget: Arc<dyn AllocationBudget>,
}
impl GuestAuditRead {
    pub fn prepare(
        cx: &CallCx,
        after: u64,
        max: u32,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Self, StoreError> {
        if max > 1000 {
            return Err(StoreError::Quota);
        }
        let cursor = if after <= MAX_SAFE_INTEGER {
            CoreValue::int(after as i64, budget).map_err(map_alloc)?
        } else {
            decimal_value(after, budget).map_err(map_alloc)?
        };
        let rows = CoreValue::int(i64::from(max), budget).map_err(map_alloc)?;
        let args = owned_args([cursor, rows], budget)?;
        Ok(Self {
            cx: cx.clone(),
            after,
            max,
            args,
            budget: budget.clone(),
        })
    }
    pub fn request(&self) -> EffectRequest {
        EffectRequest::AuditRead
    }
    pub fn args_digest(&self) -> &Digest {
        &self.args
    }
    /// Consume `grant` exactly once; failure carriers stay owned, never execute.
    pub fn consume(self, grant: Grant) -> GuestAuditAttempt {
        let (cx, request, args) = (self.cx.clone(), self.request(), self.args.clone());
        consume_with(grant, &cx, &request, &args, GuestPrepared::Read(self))
    }
}

enum GuestPrepared {
    Append(GuestAuditAppend),
    Read(GuestAuditRead),
}
impl GuestPrepared {
    fn cx(&self) -> &CallCx {
        match self {
            Self::Append(prepared) => &prepared.cx,
            Self::Read(prepared) => &prepared.cx,
        }
    }
}

/// Real consumption custody. `BeforeConsumed`, `Cleanup` and `CommitUnknown`
/// are non-executable: their Gate carriers remain available for the existing
/// reconcile/settle behavior without pretending resolution.
#[must_use]
#[expect(
    clippy::large_enum_variant,
    reason = "Keep consumed dispatch inline so Grant consumption cannot introduce a new fallible heap allocation"
)]
pub enum GuestAuditAttempt {
    BeforeConsumed(GateError),
    Cleanup(CleanupOnlyConsumption),
    CommitUnknown(UnknownConsumption),
    Ready(GuestAuditDispatch),
}

fn consume_with(
    grant: Grant,
    cx: &CallCx,
    request: &EffectRequest,
    args: &Digest,
    prepared: GuestPrepared,
) -> GuestAuditAttempt {
    match grant.consume_owned(cx, request, args) {
        ConsumptionAttempt::BeforeConsumed(error) => GuestAuditAttempt::BeforeConsumed(error),
        ConsumptionAttempt::CommitUnknown(unknown) => GuestAuditAttempt::CommitUnknown(unknown),
        ConsumptionAttempt::Consumed(attempt) => match attempt.into_dispatch() {
            Ok(permit) => GuestAuditAttempt::Ready(GuestAuditDispatch {
                prepared,
                limits: cx.limits().intersect(permit.limits()),
                deadline: permit.deadline().min(cx.deadline()),
                permit,
            }),
            Err(cleanup) => GuestAuditAttempt::Cleanup(cleanup),
        },
    }
}

/// Executable custody: the actual `DispatchPermit`, prepared owned data and
/// its funded budget, with effective limits/deadline intersected from the
/// real `CallCx` and the permit. No forgeable constructor and no clone.
#[must_use]
pub struct GuestAuditDispatch {
    prepared: GuestPrepared,
    permit: DispatchPermit,
    limits: ResourceLimits,
    deadline: Instant,
}
impl GuestAuditDispatch {
    pub fn id(&self) -> &GrantId {
        self.permit.id()
    }
    pub fn permit(&self) -> &DispatchPermit {
        &self.permit
    }
    pub fn limits(&self) -> &ResourceLimits {
        &self.limits
    }
    pub fn deadline(&self) -> Instant {
        self.deadline
    }
    /// Execute the bounded store operation inside the owning supervisor. A
    /// business error still returns the move-only delivery carrier; the same
    /// permit and original reservation custody are never lost.
    pub fn execute(self, log: &AuditLog) -> GuestAuditDelivery {
        let result = self.check_binding(log).and_then(|()| match &self.prepared {
            GuestPrepared::Append(_) => audit_guest_scan::append_history(&self, log),
            GuestPrepared::Read(_) => audit_guest_scan::read_history(&self, log),
        });
        let appended = matches!(&self.prepared, GuestPrepared::Append(_));
        let committed = appended && result.is_ok();
        let no_replay = committed
            || (appended
                && matches!(
                    &result,
                    Err(StoreError::Sql(_) | StoreError::Io(_) | StoreError::CommitUnknown)
                ));
        let Self {
            permit, deadline, ..
        } = self;
        GuestAuditDelivery {
            permit,
            result,
            committed,
            no_replay,
            deadline,
        }
    }
    fn check_binding(&self, log: &AuditLog) -> Result<(), StoreError> {
        let cx = self.prepared.cx();
        if !self.permit.matches_context(cx) || *cx.profile() != log.profile {
            return Err(StoreError::Denied("audit dispatch binding mismatch".into()));
        }
        cx.check_active()?;
        if Instant::now() >= self.deadline {
            return Err(GateError::Deadline.into());
        }
        Ok(())
    }
}

/// Owned result of one executed store operation. `Rows` is an owned
/// `CoreValue` array of per-row StrictV1 JSON strings (usable as the later
/// WIT `list<json>`), each projecting exactly seq (unsigned canonical decimal
/// string), event_id (digest string or null), prev_digest, event and digest.
pub enum GuestAuditOutput {
    Appended,
    Rows(CoreValue),
}
impl GuestAuditOutput {
    pub fn rows(&self) -> Option<&CoreValue> {
        match self {
            Self::Rows(value) => Some(value),
            _ => None,
        }
    }
}

/// Move-only delivery custody for the external terminal owner: the same
/// consumed permit, actual result/owned output and truthful committed state.
/// It never signals guest-visible success before the normal Gate terminal
/// acknowledgement path.
#[must_use]
pub struct GuestAuditDelivery {
    permit: DispatchPermit,
    result: Result<GuestAuditOutput, StoreError>,
    committed: bool,
    no_replay: bool,
    deadline: Instant,
}
impl GuestAuditDelivery {
    pub fn grant_id(&self) -> &GrantId {
        self.permit.id()
    }
    pub fn permit(&self) -> &DispatchPermit {
        &self.permit
    }
    pub fn deadline(&self) -> Instant {
        self.deadline
    }
    /// The append definitely committed. `false` can still be no-replay when
    /// the store error was post-commit-ambiguous (see `terminal_outcome`).
    pub fn is_committed(&self) -> bool {
        self.committed
    }
    /// Descriptive no-replay state for the original external supervisor.
    /// This conveys no permission to execute, retry, refund or settle.
    pub fn is_no_replay(&self) -> bool {
        self.no_replay
    }
    pub fn result(&self) -> Result<&GuestAuditOutput, &StoreError> {
        self.result.as_ref()
    }
    /// Select the immutable terminal outcome for the external supervisor's
    /// normal `record_outcome`/`settle_deadline` path. `delivered` is true
    /// only when the converted business result reached the guest. A committed
    /// or commit-ambiguous append keeps the existing fixed `unavailable`
    /// failure, never success, no-write, replay or refund.
    pub fn terminal_outcome(&self, delivered: bool) -> Outcome {
        if self.no_replay {
            return if self.committed && delivered && self.result.is_ok() {
                Outcome::Succeeded
            } else {
                Outcome::Failed {
                    code: "unavailable".into(),
                }
            };
        }
        match &self.result {
            Ok(_) if delivered => Outcome::Succeeded,
            Ok(_) => Outcome::Cancelled,
            Err(error) => Outcome::Failed {
                code: outcome_code(error).into(),
            },
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{Approvals, AuditQuota, AuditRow, DataRoot, canonical};
    use async_trait::async_trait;
    use rusqlite::params;
    use std::collections::BTreeMap;
    use std::{
        sync::{
            Mutex,
            atomic::{AtomicBool, AtomicU64, Ordering},
            mpsc,
        },
        time::Duration,
    };
    use tobkiri_canon::ids::{InputSourceId, PackHash, PackId, ProfileId, RequestId, UiSessionId};
    use tobkiri_canon::{CanonValue, Variant};
    use tobkiri_gate::*;

    struct Deny;
    #[async_trait]
    impl TrustedPrompt for Deny {
        async fn prompt_approval(&self, _: ApprovalPrompt) -> ApprovalAnswer {
            ApprovalAnswer::Deny
        }
    }
    struct Track {
        live: AtomicU64,
        cap: u64,
    }
    impl AllocationBudget for Track {
        fn reserve(&self, request: AllocationRequest) -> Result<(), AllocationError> {
            self.live
                .fetch_update(Ordering::AcqRel, Ordering::Acquire, |used| {
                    used.checked_add(request.bytes)
                        .filter(|next| *next <= self.cap)
                })
                .map(|_| ())
                .map_err(|_| AllocationError::Limit)
        }
        fn release(&self, bytes: u64) {
            self.live.fetch_sub(bytes, Ordering::AcqRel);
        }
    }
    fn limits() -> ResourceLimits {
        ResourceLimits {
            fuel_per_call: 100000,
            memory_mib: 64,
            wall_ms: 30000,
            table_elements: 4096,
            output_bytes: 4 * 1024 * 1024,
        }
    }
    struct Fixture {
        _dir: tempfile::TempDir,
        root: DataRoot,
        profile: ProfileId,
        approvals: Arc<Approvals>,
        audit: Arc<AuditLog>,
        gate: Arc<GateEngine>,
        cx: CallCx,
    }
    impl Fixture {
        fn new(policy: PermissionSet) -> Self {
            let dir = tempfile::tempdir().unwrap();
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                std::fs::set_permissions(dir.path(), std::fs::Permissions::from_mode(0o700))
                    .unwrap();
            }
            let root = DataRoot::open(dir.path().canonicalize().unwrap()).unwrap();
            let profile = ProfileId::parse("prof_guest_audit").unwrap();
            let approvals = Arc::new(Approvals::open(&root, &profile).unwrap());
            let audit = Arc::new(AuditLog::open(&root, &profile, AuditQuota::default()).unwrap());
            let gate = Arc::new(GateEngine::new(
                approvals.clone(),
                audit.clone(),
                Arc::new(Deny),
                MediatorChain::default(),
            ));
            let pack = PackId::parse("local.guest-audit").unwrap();
            let hash = PackHash::new(Digest::hash(b"pack"));
            gate.register_pack(
                &profile,
                &pack,
                &hash,
                PackPolicy {
                    declaration: policy.clone(),
                    ceiling: policy.clone(),
                    pack_limits: policy.clone(),
                },
            )
            .unwrap();
            approvals
                .set_permissions(&profile, &pack, &hash, &policy)
                .unwrap();
            let cx = CallCx::root(
                profile.clone(),
                Principal::Pack { pack, hash },
                RequestId::parse("req_guest_audit").unwrap(),
                CancellationToken::new(),
                Instant::now() + Duration::from_secs(30),
                policy.wasm.clone(),
            );
            Self {
                _dir: dir,
                root,
                profile,
                approvals,
                audit,
                gate,
                cx,
            }
        }
        fn audit_raw(&self) -> rusqlite::Connection {
            rusqlite::Connection::open(
                self.root
                    .path()
                    .join(format!("profiles/{}/audit.sqlite", self.profile)),
            )
            .unwrap()
        }
    }
    fn fixture() -> Fixture {
        Fixture::new(PermissionSet {
            audit_read: true,
            audit_append: true,
            wasm: limits(),
            ..Default::default()
        })
    }
    fn budget() -> (Arc<Track>, Arc<dyn AllocationBudget>) {
        let track = Arc::new(Track {
            live: AtomicU64::new(0),
            cap: 4 * 1024 * 1024,
        });
        (track.clone(), track)
    }
    fn event(profile: &ProfileId, principal: Principal, req: &str, kind: &str) -> AuditEvent {
        AuditEvent {
            profile: profile.clone(),
            principal,
            request: RequestId::parse(req).unwrap(),
            kind: kind.into(),
            payload: CanonValue::Null,
        }
    }
    fn ready(attempt: GuestAuditAttempt) -> GuestAuditDispatch {
        match attempt {
            GuestAuditAttempt::Ready(dispatch) => dispatch,
            GuestAuditAttempt::BeforeConsumed(_) => panic!("grant was never consumed"),
            GuestAuditAttempt::Cleanup(_) => panic!("consumed attempt is cleanup-only"),
            GuestAuditAttempt::CommitUnknown(_) => panic!("consumption outcome unknown"),
        }
    }
    async fn run_append(
        f: &Fixture,
        budget: &Arc<dyn AllocationBudget>,
        kind: &str,
        payload: &[u8],
    ) -> (GrantId, GuestAuditDelivery) {
        let kind = CoreValue::string(kind, budget).unwrap();
        let payload = CoreValue::parse_guest(payload, budget).unwrap();
        let prepared = GuestAuditAppend::prepare(&f.cx, kind, payload, budget).unwrap();
        let grant = f
            .gate
            .authorize(&f.cx, prepared.request(), prepared.args_digest().clone())
            .await
            .unwrap();
        let id = grant.id().clone();
        (id, ready(prepared.consume(grant)).execute(&f.audit))
    }
    async fn run_read(
        f: &Fixture,
        cx: &CallCx,
        budget: &Arc<dyn AllocationBudget>,
        after: u64,
        max: u32,
    ) -> (GrantId, GuestAuditDelivery) {
        let prepared = GuestAuditRead::prepare(cx, after, max, budget).unwrap();
        let grant = f
            .gate
            .authorize(cx, prepared.request(), prepared.args_digest().clone())
            .await
            .unwrap();
        let id = grant.id().clone();
        (id, ready(prepared.consume(grant)).execute(&f.audit))
    }
    fn rows(delivery: &GuestAuditDelivery) -> Vec<serde_json::Value> {
        delivery
            .result()
            .unwrap()
            .rows()
            .unwrap()
            .array()
            .unwrap()
            .iter()
            .map(|row| serde_json::from_str(row.as_str().unwrap()).unwrap())
            .collect()
    }

    #[tokio::test]
    async fn owned_append_and_read_preserve_chain_provenance_and_projection() {
        let f = fixture();
        let (_track, budget) = budget();
        f.audit
            .append(event(
                &f.profile,
                Principal::User,
                "req_core_row",
                "core-note",
            ))
            .unwrap();
        let other = Principal::Pack {
            pack: PackId::parse("local.other").unwrap(),
            hash: PackHash::new(Digest::hash(b"other-pack")),
        };
        f.audit
            .append(event(&f.profile, other, "req_other_row", "other-pack"))
            .unwrap();
        let raw = br#"{"kind":"forged","profile":"forged","payload":{"deep":[1,"\u0001\n"]},"principal":"user"}"#;
        let canon = tobkiri_canon::parse(raw, Variant::StrictV1).unwrap();
        let expected = Digest::hash(&crate::canonical(&("guest.note", &canon)).unwrap());
        let kind = CoreValue::string("guest.note", &budget).unwrap();
        let payload = CoreValue::parse_guest(raw, &budget).unwrap();
        let prepared = GuestAuditAppend::prepare(&f.cx, kind, payload, &budget).unwrap();
        assert_eq!(prepared.args_digest(), &expected);
        let grant = f
            .gate
            .authorize(&f.cx, prepared.request(), expected)
            .await
            .unwrap();
        let id = grant.id().clone();
        let delivery = ready(prepared.consume(grant)).execute(&f.audit);
        assert!(delivery.is_committed());
        assert!(matches!(delivery.result(), Ok(GuestAuditOutput::Appended)));
        assert_eq!(delivery.grant_id(), &id);
        f.gate
            .record_outcome(id.clone(), delivery.terminal_outcome(true))
            .await
            .unwrap();
        let saved = f.approvals.terminal_delivery(&id).unwrap().unwrap();
        assert!(saved.acknowledged && saved.decision.outcome == Outcome::Succeeded);
        assert_eq!(saved.decision.reservation.id, id);
        let body: Vec<u8> = f
            .audit_raw()
            .query_row(
                "SELECT body FROM audit WHERE instr(body,'guest-event')>0",
                [],
                |r| r.get(0),
            )
            .unwrap();
        let stored: AuditEvent = crate::decode(&body).unwrap();
        assert_eq!(stored.kind, "guest-event");
        assert_eq!(stored.profile, f.profile);
        assert_eq!(stored.principal, *f.cx.principal());
        assert_eq!(stored.request, *f.cx.request());
        let fields = stored.payload.as_object().unwrap();
        assert_eq!(fields.len(), 2);
        assert_eq!(fields["kind"].as_str(), Some("guest.note"));
        assert_eq!(fields["payload"], canon);
        let grant = f
            .gate
            .authorize(
                &f.cx,
                EffectRequest::AuditRead,
                AuditLog::read_digest(0, 10).unwrap(),
            )
            .await
            .unwrap();
        let legacy = f.audit.read(&f.cx, 0, 10, grant).unwrap();
        assert!(legacy.iter().any(|row| row.event.kind == "core-note"));
        assert!(legacy.iter().any(|row| row.event.kind == "other-pack"));
        let trusted = legacy
            .iter()
            .find(|row| row.event.kind == "guest-event")
            .unwrap();
        assert!(trusted.seq > 2);
        let (_, read) = run_read(&f, &f.cx, &budget, 0, 10).await;
        let rows = rows(&read);
        let seqs: Vec<u64> = rows
            .iter()
            .map(|r| r["seq"].as_str().unwrap().parse().unwrap())
            .collect();
        assert!(seqs.iter().all(|seq| *seq > 0));
        assert!(seqs.windows(2).all(|pair| pair[0] < pair[1]));
        for row in &rows {
            let mut keys: Vec<_> = row
                .as_object()
                .unwrap()
                .keys()
                .map(String::as_str)
                .collect();
            keys.sort();
            assert_eq!(keys, ["digest", "event", "event_id", "prev_digest", "seq"]);
        }
        for trusted in &legacy {
            let seq = trusted.seq.to_string();
            let row = rows
                .iter()
                .find(|r| r["seq"].as_str() == Some(seq.as_str()))
                .unwrap();
            assert_eq!(row["digest"].as_str().unwrap(), trusted.digest.as_str());
            assert_eq!(
                row["prev_digest"].as_str().unwrap(),
                trusted.prev_digest.as_str()
            );
            match &trusted.event_id {
                Some(id) => assert_eq!(row["event_id"].as_str().unwrap(), id.as_str()),
                None => assert!(row["event_id"].is_null()),
            }
            assert_eq!(row["event"], serde_json::to_value(&trusted.event).unwrap());
        }
        let seq = trusted.seq.to_string();
        let guest = rows
            .iter()
            .find(|r| r["seq"].as_str() == Some(seq.as_str()))
            .unwrap();
        assert!(guest["event_id"].is_null());
        assert_eq!(guest["event"]["kind"], "guest-event");
        assert_eq!(guest["event"]["payload"]["kind"], "guest.note");
        assert_eq!(
            guest["event"]["payload"]["payload"]["payload"]["deep"][1],
            "\u{1}\n"
        );
        assert_eq!(guest["digest"].as_str().unwrap(), trusted.digest.as_str());
        f.audit.verify().unwrap();
    }

    #[tokio::test]
    async fn owned_read_digests_cursors_and_denied_binding() {
        let f = fixture();
        let (_track, budget) = budget();
        for (kind, payload) in [("a", "1"), ("b", "2")] {
            let (id, delivery) = run_append(&f, &budget, kind, payload.as_bytes()).await;
            assert!(delivery.is_committed());
            assert!(delivery.result().is_ok());
            f.gate
                .record_outcome(id.clone(), delivery.terminal_outcome(false))
                .await
                .unwrap();
            assert!(
                f.approvals
                    .terminal_delivery(&id)
                    .unwrap()
                    .unwrap()
                    .acknowledged
            );
            drop(delivery);
        }
        let safe = GuestAuditRead::prepare(&f.cx, 5, 10, &budget).unwrap();
        assert_eq!(safe.args_digest(), &AuditLog::read_digest(5, 10).unwrap());
        let edge = GuestAuditRead::prepare(&f.cx, MAX_SAFE_INTEGER, 1, &budget).unwrap();
        assert_eq!(
            edge.args_digest(),
            &AuditLog::read_digest(MAX_SAFE_INTEGER, 1).unwrap()
        );
        assert!(AuditLog::read_digest(MAX_SAFE_INTEGER + 1, 1).is_err());
        for after in [MAX_SAFE_INTEGER + 1, i64::MAX as u64, u64::MAX] {
            let prepared = GuestAuditRead::prepare(&f.cx, after, 7, &budget).unwrap();
            let expected = tobkiri_canon::digest(
                &CanonValue::Arr(vec![CanonValue::Str(after.to_string()), CanonValue::Int(7)]),
                Variant::StrictV1,
            )
            .unwrap();
            assert_eq!(prepared.args_digest(), &expected);
        }
        let (_, read) = run_read(&f, &f.cx, &budget, 2, 10).await;
        let seqs: Vec<u64> = rows(&read)
            .iter()
            .map(|r| r["seq"].as_str().unwrap().parse().unwrap())
            .collect();
        assert!(seqs.iter().all(|s| *s > 2));
        assert!(seqs.windows(2).all(|w| w[0] < w[1]));
        let (_, read) = run_read(&f, &f.cx, &budget, 0, 0).await;
        assert!(rows(&read).is_empty());
        let (_, read) = run_read(&f, &f.cx, &budget, i64::MAX as u64 + 1, 10).await;
        assert!(rows(&read).is_empty());
        assert!(matches!(
            GuestAuditRead::prepare(&f.cx, 0, 1001, &budget),
            Err(StoreError::Quota)
        ));
        let prepared = GuestAuditRead::prepare(&f.cx, 0, 10, &budget).unwrap();
        let grant = f
            .gate
            .authorize(&f.cx, prepared.request(), Digest::hash(b"other-args"))
            .await
            .unwrap();
        let gid = grant.id().clone();
        assert!(matches!(
            prepared.consume(grant),
            GuestAuditAttempt::BeforeConsumed(GateError::Denied(_))
        ));
        assert_eq!(
            f.approvals
                .terminal_delivery(&gid)
                .unwrap()
                .unwrap()
                .decision
                .disposition,
            TerminalDisposition::AbandonedBeforeUse
        );
        let foreign = AuditLog::open(
            &f.root,
            &ProfileId::parse("prof_foreign_audit").unwrap(),
            AuditQuota::default(),
        )
        .unwrap();
        let prepared = GuestAuditRead::prepare(&f.cx, 0, 10, &budget).unwrap();
        let grant = f
            .gate
            .authorize(&f.cx, prepared.request(), prepared.args_digest().clone())
            .await
            .unwrap();
        let delivery = ready(prepared.consume(grant)).execute(&foreign);
        assert!(matches!(delivery.result(), Err(StoreError::Denied(_))));
        assert!(!delivery.is_committed());
        f.gate
            .record_outcome(delivery.grant_id().clone(), delivery.terminal_outcome(true))
            .await
            .unwrap();
        let prepared = GuestAuditRead::prepare(&f.cx, 0, 10, &budget).unwrap();
        let grant = f
            .gate
            .authorize(&f.cx, prepared.request(), prepared.args_digest().clone())
            .await
            .unwrap();
        f.approvals.advance_security_epoch(&f.profile).unwrap();
        assert!(matches!(
            prepared.consume(grant),
            GuestAuditAttempt::BeforeConsumed(GateError::StaleEpoch)
        ));
        let denied = Fixture::new(PermissionSet {
            audit_read: true,
            wasm: limits(),
            ..Default::default()
        });
        assert!(
            denied
                .gate
                .authorize(&denied.cx, EffectRequest::AuditAppend, Digest::hash(b"x"))
                .await
                .is_err()
        );
    }

    #[tokio::test]
    async fn bounded_event_output_and_owner_funding() {
        let f = fixture();
        let (_track, budget) = budget();
        let raw = f.audit_raw();
        let (id, probe_delivery) = run_append(&f, &budget, "m", br#""p""#).await;
        assert!(probe_delivery.is_committed());
        assert!(probe_delivery.result().is_ok());
        f.gate
            .record_outcome(id.clone(), probe_delivery.terminal_outcome(false))
            .await
            .unwrap();
        assert!(
            f.approvals
                .terminal_delivery(&id)
                .unwrap()
                .unwrap()
                .acknowledged
        );
        drop(probe_delivery);
        let probe: i64 = raw
            .query_row(
                "SELECT size FROM audit WHERE instr(body,'\"kind\":\"m\"')>0 ORDER BY seq DESC LIMIT 1",
                [],
                |r| r.get(0),
            )
            .unwrap();
        let overhead = probe as usize - 3;
        let fill = 64 * 1024 - overhead - 2;
        let (_, delivery) = run_append(
            &f,
            &budget,
            "m",
            format!("\"{}\"", "x".repeat(fill)).as_bytes(),
        )
        .await;
        assert!(delivery.is_committed());
        let size: i64 = raw
            .query_row(
                "SELECT size FROM audit WHERE instr(body,'\"kind\":\"m\"')>0 ORDER BY seq DESC LIMIT 1",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(size as usize, 64 * 1024);
        let (_, delivery) = run_append(
            &f,
            &budget,
            "m",
            format!("\"{}\"", "x".repeat(fill + 1)).as_bytes(),
        )
        .await;
        assert!(matches!(delivery.result(), Err(StoreError::Quota)));
        assert!(!delivery.is_committed());
        let (_, read) = run_read(&f, &f.cx, &budget, 0, 1000).await;
        let first = read
            .result()
            .unwrap()
            .rows()
            .unwrap()
            .array()
            .unwrap()
            .get(0)
            .unwrap();
        let cap = serde_json::to_string(first.as_str().unwrap())
            .unwrap()
            .len() as u64
            + 2;
        let narrow = f.cx.clone().narrow_limits(&ResourceLimits {
            output_bytes: cap,
            ..limits()
        });
        let (_, denied) = run_read(&f, &narrow, &budget, 0, 1000).await;
        assert!(matches!(denied.result(), Err(StoreError::Quota)));
        let (_, single) = run_read(&f, &narrow, &budget, 0, 1).await;
        assert_eq!(rows(&single).len(), 1);
        let tiny: Arc<dyn AllocationBudget> = Arc::new(Track {
            live: AtomicU64::new(0),
            cap: 32,
        });
        let kind = CoreValue::string("k", &budget).unwrap();
        let payload = CoreValue::parse_guest(b"1", &budget).unwrap();
        assert!(matches!(
            GuestAuditAppend::prepare(&f.cx, kind, payload, &tiny),
            Err(StoreError::Quota)
        ));
    }

    #[tokio::test]
    async fn corrupted_stored_body_is_integrity_and_owner_charge_releases() {
        let f = fixture();
        let (track, budget) = budget();
        f.audit
            .append(event(&f.profile, Principal::User, "req_seed", "seed"))
            .unwrap();
        let prepared = GuestAuditRead::prepare(&f.cx, 0, 10, &budget).unwrap();
        let grant = f
            .gate
            .authorize(&f.cx, prepared.request(), prepared.args_digest().clone())
            .await
            .unwrap();
        let dispatch = ready(prepared.consume(grant));
        let narrow = f.cx.clone().narrow_limits(&ResourceLimits {
            output_bytes: 8,
            ..limits()
        });
        let prepared = GuestAuditRead::prepare(&narrow, 0, 10, &budget).unwrap();
        let grant = f
            .gate
            .authorize(&narrow, prepared.request(), prepared.args_digest().clone())
            .await
            .unwrap();
        let narrow_dispatch = ready(prepared.consume(grant));
        f.audit_raw()
            .execute(
                "INSERT INTO audit(seq,prev_digest,digest,body,writer,minute,size) VALUES(4,?1,?2,X'7B7D','user',0,2)",
                rusqlite::params![Digest::hash(b"prev").as_str(), Digest::hash(b"row").as_str()],
            )
            .unwrap();
        let delivery = dispatch.execute(&f.audit);
        assert!(matches!(
            delivery.result(),
            Err(StoreError::Integrity(IntegrityFaultCode::AuditChain))
        ));
        assert_eq!(track.live.load(Ordering::Acquire), 0);
        let delivery = narrow_dispatch.execute(&f.audit);
        assert!(matches!(delivery.result(), Err(StoreError::Quota)));
        assert_eq!(track.live.load(Ordering::Acquire), 0);
    }

    #[tokio::test]
    async fn corrupted_stored_metadata_is_integrity() {
        let f = fixture();
        let (_track, budget) = budget();
        f.audit
            .append(event(&f.profile, Principal::User, "req_seed", "seed"))
            .unwrap();
        let prepared = GuestAuditRead::prepare(&f.cx, 0, 10, &budget).unwrap();
        let grant = f
            .gate
            .authorize(&f.cx, prepared.request(), prepared.args_digest().clone())
            .await
            .unwrap();
        let dispatch = ready(prepared.consume(grant));
        let body =
            crate::canonical(&event(&f.profile, Principal::User, "req_bad", "meta")).unwrap();
        let size = body.len() as i64;
        f.audit_raw()
            .execute(
                "INSERT INTO audit(seq,prev_digest,digest,body,writer,minute,size) VALUES(3,?1,'not-a-digest',?2,'user',0,?3)",
                rusqlite::params![Digest::hash(b"prev").as_str(), body, size],
            )
            .unwrap();
        let delivery = dispatch.execute(&f.audit);
        assert!(matches!(
            delivery.result(),
            Err(StoreError::Integrity(IntegrityFaultCode::AuditChain))
        ));
    }

    #[tokio::test]
    async fn committed_append_keeps_sealed_winner_across_terminal_faults() {
        let f = fixture();
        let (_track, budget) = budget();
        let (id, delivery) = run_append(&f, &budget, "once", b"1").await;
        assert!(delivery.is_committed());
        assert_eq!(
            delivery.terminal_outcome(false),
            Outcome::Failed {
                code: "unavailable".into()
            }
        );
        let raw = rusqlite::Connection::open(
            f.root
                .path()
                .join(format!("profiles/{}/approvals.sqlite", f.profile)),
        )
        .unwrap();
        raw.execute_batch("CREATE TRIGGER guest_term_fail BEFORE INSERT ON terminal_outbox BEGIN SELECT RAISE(ABORT,'fault'); END;").unwrap();
        assert!(
            f.gate
                .record_outcome(id.clone(), delivery.terminal_outcome(true))
                .await
                .is_err()
        );
        assert!(f.approvals.terminal_delivery(&id).unwrap().is_none());
        assert_eq!(
            f.approvals.reservation_state(&id).unwrap().as_deref(),
            Some("consumed")
        );
        let appends: i64 = f
            .audit_raw()
            .query_row(
                "SELECT COUNT(*) FROM audit WHERE instr(body,'guest-event')>0",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(appends, 1);
        raw.execute_batch("DROP TRIGGER guest_term_fail").unwrap();
        f.gate
            .record_outcome(id.clone(), Outcome::Succeeded)
            .await
            .unwrap();
        let saved = f.approvals.terminal_delivery(&id).unwrap().unwrap();
        assert!(saved.acknowledged && saved.decision.outcome == Outcome::Succeeded);
        assert!(f.gate.record_outcome(id, Outcome::Cancelled).await.is_err());
        f.audit.verify().unwrap();
    }

    struct Meter {
        live: AtomicU64,
        work: AtomicU64,
        reserves: AtomicU64,
        fail_at: u64,
        error: AllocationError,
        work_cap: u64,
    }
    impl Meter {
        fn new(fail_at: u64, error: AllocationError, work_cap: u64) -> Self {
            Self {
                live: AtomicU64::new(0),
                work: AtomicU64::new(0),
                reserves: AtomicU64::new(0),
                fail_at,
                error,
                work_cap,
            }
        }
        fn live(&self) -> u64 {
            self.live.load(Ordering::Acquire)
        }
    }
    impl AllocationBudget for Meter {
        fn reserve(&self, request: AllocationRequest) -> Result<(), AllocationError> {
            if self.reserves.fetch_add(1, Ordering::AcqRel) + 1 == self.fail_at {
                return Err(self.error);
            }
            let work = request.work.max(request.elements);
            if self.work.fetch_add(work, Ordering::AcqRel) + work > self.work_cap {
                return Err(AllocationError::Limit);
            }
            self.live.fetch_add(request.bytes, Ordering::AcqRel);
            Ok(())
        }
        fn release(&self, bytes: u64) {
            self.live.fetch_sub(bytes, Ordering::AcqRel);
        }
    }

    #[test]
    fn owned_codec_matches_legacy_unsigned_digest() {
        use super::audit_guest_owned::OwnedAuditEvent;
        use crate::AuditRow;
        use tobkiri_canon::ids::{InputSourceId, UiSessionId};
        let f = fixture();
        let (_track, budget) = budget();
        let pack = PackId::parse("local.guest-audit").unwrap();
        let hash = PackHash::new(Digest::hash(b"pack"));
        let prev = Digest::hash(b"prev");
        let event_id = Digest::hash(b"id");
        for (principal, kind, payload) in [
            (
                Principal::User,
                "user.kind",
                CanonValue::Obj(BTreeMap::from([(
                    "deep".into(),
                    CanonValue::Arr(vec![
                        CanonValue::Int(-1),
                        CanonValue::Int(MAX_SAFE_INTEGER as i64),
                        CanonValue::Int(-(MAX_SAFE_INTEGER as i64)),
                        CanonValue::Str("ünï\u{1}\n".into()),
                    ]),
                )])),
            ),
            (
                Principal::Input {
                    source: InputSourceId::parse("src-1").unwrap(),
                },
                "",
                CanonValue::Null,
            ),
            (
                Principal::Pack {
                    pack: pack.clone(),
                    hash: hash.clone(),
                },
                "kind-\u{1f600}\u{7f}",
                CanonValue::Bool(true),
            ),
            (
                Principal::PackUi {
                    pack,
                    hash,
                    ui: UiSessionId::parse("ui.1").unwrap(),
                },
                "k",
                CanonValue::Arr(vec![]),
            ),
        ] {
            let event = AuditEvent {
                profile: f.profile.clone(),
                principal,
                request: RequestId::parse("req_owned").unwrap(),
                kind: kind.into(),
                payload,
            };
            // Canonical bytes, serde field order and pretty whitespace all
            // carry the same legacy meaning.
            for body in [
                crate::canonical(&event).unwrap(),
                serde_json::to_vec(&event).unwrap(),
                serde_json::to_vec_pretty(&event).unwrap(),
            ] {
                let parsed = OwnedAuditEvent::parse(&body, &f.profile, &budget).unwrap();
                assert_eq!(parsed.profile, event.profile);
                assert_eq!(parsed.principal, event.principal);
                assert_eq!(parsed.request, event.request);
                for (seq, id) in [(1, None), (7, Some(&event_id)), (MAX_SAFE_INTEGER, None)] {
                    let expected = AuditRow::digest(seq, &prev, &event, id).unwrap();
                    assert_eq!(
                        parsed.unsigned_digest(seq, &prev, id, &budget).unwrap(),
                        expected
                    );
                    let bytes = parsed
                        .unsigned_row(seq, &prev, id, &budget)
                        .unwrap()
                        .encode_guest(&budget)
                        .unwrap();
                    assert_eq!(Digest::hash(bytes.as_bytes()), expected);
                    let json: serde_json::Value = serde_json::from_slice(bytes.as_bytes()).unwrap();
                    assert_eq!(json["seq"].as_u64().unwrap(), seq);
                    match id {
                        Some(v) => {
                            assert_eq!(json["event_id"].as_str().unwrap(), v.as_str())
                        }
                        None => assert!(json.get("event_id").is_none()),
                    }
                }
            }
        }
    }

    #[test]
    fn owned_codec_rejects_corruption() {
        use super::audit_guest_owned::OwnedAuditEvent;
        let f = fixture();
        let (_track, budget) = budget();
        let p = f.profile.as_str();
        let hd = Digest::hash(b"h");
        let h = hd.as_str();
        let ev = event(&f.profile, Principal::User, "req_owned", "k");
        let body = crate::canonical(&ev).unwrap();
        let parsed = OwnedAuditEvent::parse(&body, &f.profile, &budget).unwrap();
        let prev = Digest::hash(b"prev");
        for seq in [0, MAX_SAFE_INTEGER + 1] {
            assert!(matches!(
                parsed.unsigned_digest(seq, &prev, None, &budget),
                Err(StoreError::Integrity(IntegrityFaultCode::AuditChain))
            ));
        }
        let mut corrupt: Vec<Vec<u8>> = [
            serde_json::json!({"kind":"k","principal":{"kind":"user"},"profile":p,"request":"r"}),
            serde_json::json!({"kind":"k","payload":null,"principal":{"kind":"user"},"profile":p,"request":"r","x":1}),
            serde_json::json!({"kind":1,"payload":null,"principal":{"kind":"user"},"profile":p,"request":"r"}),
            serde_json::json!({"kind":"k","payload":null,"principal":"user","profile":p,"request":"r"}),
            serde_json::json!({"kind":"k","payload":null,"principal":{"kind":"system"},"profile":p,"request":"r"}),
            serde_json::json!({"kind":"k","payload":null,"principal":{"kind":"pack","pack":"local.x","hash":h,"x":0},"profile":p,"request":"r"}),
            serde_json::json!({"kind":"k","payload":null,"principal":{"kind":"pack","pack":"local.x"},"profile":p,"request":"r"}),
            serde_json::json!({"kind":"k","payload":null,"principal":{"kind":"pack","pack":"BAD","hash":h},"profile":p,"request":"r"}),
            serde_json::json!({"kind":"k","payload":null,"principal":{"kind":"pack","pack":"local.x","hash":"bad"},"profile":p,"request":"r"}),
            serde_json::json!({"kind":"k","payload":null,"principal":{"kind":"pack-ui","pack":"local.x","hash":h},"profile":p,"request":"r"}),
            serde_json::json!({"kind":"k","payload":null,"principal":{"kind":"pack-ui","pack":"local.x","hash":h,"ui":"bad..ui"},"profile":p,"request":"r"}),
            serde_json::json!({"kind":"k","payload":null,"principal":{"kind":"input"},"profile":p,"request":"r"}),
            serde_json::json!({"kind":"k","payload":null,"principal":{"kind":"input","source":"..s"},"profile":p,"request":"r"}),
            serde_json::json!({"kind":"k","payload":null,"principal":{"kind":"user","x":1},"profile":p,"request":"r"}),
            serde_json::json!({"kind":"k","payload":null,"principal":{"kind":"user"},"profile":"BAD ID","request":"r"}),
            serde_json::json!({"kind":"k","payload":null,"principal":{"kind":"user"},"profile":"other","request":"r"}),
            serde_json::json!({"kind":"k","payload":null,"principal":{"kind":"user"},"profile":p,"request":"bad..id"}),
        ]
        .iter()
        .map(|case| serde_json::to_vec(case).unwrap())
        .collect();
        corrupt.extend([
            b"{".to_vec(),
            b"{}".to_vec(),
            b"[1]".to_vec(),
            vec![0xff],
            b"{} x".to_vec(),
            format!("{}0{}", "[".repeat(66), "]".repeat(66)).into_bytes(),
            br#"{"kind":"k","payload":9007199254740992,"principal":{"kind":"user"},"profile":"prof_guest_audit","request":"r"}"#.to_vec(),
        ]);
        for body in corrupt {
            assert!(matches!(
                OwnedAuditEvent::parse(&body, &f.profile, &budget),
                Err(StoreError::Integrity(IntegrityFaultCode::AuditChain))
            ));
        }
    }

    #[test]
    fn owned_codec_budget_failures_keep_class_and_release() {
        use super::audit_guest_owned::OwnedAuditEvent;
        use tobkiri_canon::ids::UiSessionId;
        let f = fixture();
        let (track, budget) = budget();
        let ev = AuditEvent {
            profile: f.profile.clone(),
            principal: Principal::PackUi {
                pack: PackId::parse("local.guest-audit").unwrap(),
                hash: PackHash::new(Digest::hash(b"pack")),
                ui: UiSessionId::parse("ui.1").unwrap(),
            },
            request: RequestId::parse("req_owned").unwrap(),
            kind: "k".into(),
            payload: CanonValue::Arr(vec![CanonValue::Int(1), CanonValue::Str("x".into())]),
        };
        let body = crate::canonical(&ev).unwrap();
        let prev = Digest::hash(b"prev");
        let event_id = Digest::hash(b"id");
        {
            let parsed = OwnedAuditEvent::parse(&body, &f.profile, &budget).unwrap();
            assert!(track.live.load(Ordering::Acquire) > 0);
            parsed
                .unsigned_digest(3, &prev, Some(&event_id), &budget)
                .unwrap();
        }
        assert_eq!(track.live.load(Ordering::Acquire), 0);
        for _ in 0..4 {
            drop(OwnedAuditEvent::parse(&body, &f.profile, &budget).unwrap());
            assert_eq!(track.live.load(Ordering::Acquire), 0);
        }
        let probe = Arc::new(Meter::new(u64::MAX, AllocationError::Limit, u64::MAX));
        let probe_budget: Arc<dyn AllocationBudget> = probe.clone();
        let parsed = OwnedAuditEvent::parse(&body, &f.profile, &probe_budget).unwrap();
        let parse_reserves = probe.reserves.load(Ordering::Acquire);
        assert!(parse_reserves > 1);
        let probe2 = Arc::new(Meter::new(u64::MAX, AllocationError::Limit, u64::MAX));
        let probe2_budget: Arc<dyn AllocationBudget> = probe2.clone();
        parsed
            .unsigned_digest(3, &prev, Some(&event_id), &probe2_budget)
            .unwrap();
        let digest_reserves = probe2.reserves.load(Ordering::Acquire);
        assert!(digest_reserves > 1);
        for error in [
            AllocationError::Limit,
            AllocationError::Overflow,
            AllocationError::Unavailable,
            AllocationError::Allocator,
        ] {
            let quota = matches!(error, AllocationError::Limit | AllocationError::Overflow);
            for fail_at in 1..=parse_reserves {
                let meter = Arc::new(Meter::new(fail_at, error, u64::MAX));
                let failing: Arc<dyn AllocationBudget> = meter.clone();
                match OwnedAuditEvent::parse(&body, &f.profile, &failing).map(|_| ()) {
                    Err(StoreError::Quota) => assert!(quota),
                    Err(StoreError::Unavailable(_)) => assert!(!quota),
                    other => panic!("unexpected codec result {other:?}"),
                }
                assert_eq!(meter.live(), 0);
            }
            for fail_at in 1..=digest_reserves {
                let meter = Arc::new(Meter::new(fail_at, error, u64::MAX));
                let failing: Arc<dyn AllocationBudget> = meter.clone();
                match parsed
                    .unsigned_digest(3, &prev, Some(&event_id), &failing)
                    .map(|_| ())
                {
                    Err(StoreError::Quota) => assert!(quota),
                    Err(StoreError::Unavailable(_)) => assert!(!quota),
                    other => panic!("unexpected digest result {other:?}"),
                }
                assert_eq!(meter.live(), 0);
            }
        }
        let meter = Arc::new(Meter::new(u64::MAX, AllocationError::Limit, 1));
        let failing: Arc<dyn AllocationBudget> = meter.clone();
        assert!(matches!(
            OwnedAuditEvent::parse(&body, &f.profile, &failing).map(|_| ()),
            Err(StoreError::Quota)
        ));
        assert!(meter.work.load(Ordering::Acquire) > 0);
        assert_eq!(meter.live(), 0);
        let meter = Arc::new(Meter::new(u64::MAX, AllocationError::Limit, 10));
        let failing: Arc<dyn AllocationBudget> = meter.clone();
        assert!(matches!(
            parsed.unsigned_digest(3, &prev, None, &failing).map(|_| ()),
            Err(StoreError::Quota)
        ));
        drop(parsed);
        assert_eq!(probe.live(), 0);
        assert_eq!(probe2.live(), 0);
    }

    #[test]
    fn integrity_fault_reports_persistence_code() {
        assert_eq!(
            outcome_code(&StoreError::Integrity(IntegrityFaultCode::AuditChain)),
            "persistence"
        );
        assert_eq!(
            outcome_code(&StoreError::Integrity(
                IntegrityFaultCode::AuthorityDatabase
            )),
            "persistence"
        );
        assert_eq!(
            outcome_code(&StoreError::Gate(GateError::Integrity(
                IntegrityFaultCode::AuditChain
            ))),
            "persistence"
        );
        assert_eq!(outcome_code(&StoreError::CommitUnknown), "unavailable");
        for progress in [
            crate::ProfileCommitProgress::EpochCommitAttempted,
            crate::ProfileCommitProgress::EpochAdvanced,
            crate::ProfileCommitProgress::HeadReplaced,
        ] {
            assert_eq!(
                outcome_code(&StoreError::ProfileCommitUncertain {
                    progress,
                    source: Box::new(StoreError::Io(std::io::Error::other(
                        "private native Profile failure",
                    ))),
                }),
                "unavailable"
            );
        }
        assert_eq!(outcome_code(&StoreError::Quota), "limit-exceeded");
        assert_eq!(outcome_code(&StoreError::Denied("x".into())), "denied");
        assert_eq!(outcome_code(&StoreError::NotFound), "not-found");
    }

    use super::audit_guest_owned::OwnedAuditEvent;
    use super::audit_guest_scan::verify_history;

    /// Test-only reserve probe: once armed it signals after the real first
    /// SQLite step inside the scan transaction, then blocks until released.
    /// This is synchronization evidence only, never production lock proof.
    struct SyncBudget {
        inner: Arc<dyn AllocationBudget>,
        armed: AtomicBool,
        stepped: mpsc::Sender<()>,
        proceed: Mutex<mpsc::Receiver<()>>,
    }
    impl AllocationBudget for SyncBudget {
        fn reserve(&self, request: AllocationRequest) -> Result<(), AllocationError> {
            let result = self.inner.reserve(request);
            if result.is_ok() && self.armed.swap(false, Ordering::AcqRel) {
                let _ = self.stepped.send(());
                let _ = self.proceed.lock().unwrap().recv();
            }
            result
        }
        fn release(&self, bytes: u64) {
            self.inner.release(bytes);
        }
    }
    /// Deniable test budget: fails the reservation call whose ordinal equals
    /// `fail_at`; work accrues monotonically and is never refunded.
    struct FailMeter {
        live: AtomicU64,
        work: AtomicU64,
        reserves: AtomicU64,
        fail_at: AtomicU64,
        error: AllocationError,
    }
    impl FailMeter {
        fn new(error: AllocationError) -> Arc<Self> {
            Arc::new(Self {
                live: AtomicU64::new(0),
                work: AtomicU64::new(0),
                reserves: AtomicU64::new(0),
                fail_at: AtomicU64::new(u64::MAX),
                error,
            })
        }
        fn live(&self) -> u64 {
            self.live.load(Ordering::Acquire)
        }
        fn work(&self) -> u64 {
            self.work.load(Ordering::Acquire)
        }
    }
    impl AllocationBudget for FailMeter {
        fn reserve(&self, request: AllocationRequest) -> Result<(), AllocationError> {
            if self.reserves.fetch_add(1, Ordering::AcqRel) + 1
                == self.fail_at.load(Ordering::Acquire)
            {
                return Err(self.error);
            }
            self.work
                .fetch_add(request.work.max(request.elements), Ordering::AcqRel);
            self.live.fetch_add(request.bytes, Ordering::AcqRel);
            Ok(())
        }
        fn release(&self, bytes: u64) {
            self.live.fetch_sub(bytes, Ordering::AcqRel);
        }
    }
    fn vector_pack() -> (PackId, PackHash) {
        (
            PackId::parse("local.guest-audit").unwrap(),
            PackHash::new(Digest::hash(b"pack")),
        )
    }
    fn vector_event(f: &Fixture, principal: Principal) -> AuditEvent {
        AuditEvent {
            profile: f.profile.clone(),
            principal,
            request: RequestId::parse("req_owned").unwrap(),
            kind: String::new(),
            payload: serde_json::from_str(
                r#"{"deep":[-1,9007199254740991,-9007199254740991,"ünï\u0001\n😀\u007f"]}"#,
            )
            .unwrap(),
        }
    }
    fn tail(f: &Fixture) -> (u64, Digest) {
        let (seq, digest): (i64, String) = f
            .audit_raw()
            .query_row(
                "SELECT seq,digest FROM audit ORDER BY seq DESC LIMIT 1",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        (seq as u64, Digest::parse(&digest).unwrap())
    }
    fn raw_insert(f: &Fixture, seq: i64, prev: &str, digest: &str, body: &[u8], id: Option<&str>) {
        f.audit_raw()
            .execute(
                "INSERT INTO audit(seq,prev_digest,digest,body,writer,minute,size,event_id) VALUES(?1,?2,?3,?4,'probe',0,?5,?6)",
                params![seq, prev, digest, body, body.len() as i64, id],
            )
            .unwrap();
    }
    fn raw_delete(f: &Fixture, seq: i64) {
        f.audit_raw()
            .execute("DELETE FROM audit WHERE seq=?1", params![seq])
            .unwrap();
    }
    async fn read_dispatch(
        f: &Fixture,
        budget: &Arc<dyn AllocationBudget>,
        after: u64,
        max: u32,
    ) -> (GrantId, GuestAuditDispatch) {
        let prepared = GuestAuditRead::prepare(&f.cx, after, max, budget).unwrap();
        let grant = f
            .gate
            .authorize(&f.cx, prepared.request(), prepared.args_digest().clone())
            .await
            .unwrap();
        (grant.id().clone(), ready(prepared.consume(grant)))
    }

    #[tokio::test]
    async fn full_history_scan_mixed_chain_and_vectors() {
        let f = fixture();
        let (_track, budget) = budget();
        let (gid, dispatch) = read_dispatch(&f, &budget, 0, 0).await;
        let prev = Digest::parse(
            "sha256:84fd9bac333ad79154348296204fa7f8c537a96e08983e5f73b3f5aca8e8edf7",
        )
        .unwrap();
        let event_id = Digest::parse(
            "sha256:a56145270ce6b3bebd1dd012b73948677dd618d496488bc608a3cb43ce3547dd",
        )
        .unwrap();
        let (pack, hash) = vector_pack();
        let cases: [(Principal, &str, &str); 4] = [
            (
                Principal::User,
                "sha256:e289642446890ed4ec75206c19b33488a7aaeb5102a5fb942c4330c54ad1fb22",
                "sha256:ec078c2ce0ac1b45ff7ea4b8c32ac0d4a148948559064326a4f918d555151dfc",
            ),
            (
                Principal::Input {
                    source: InputSourceId::parse("src-1").unwrap(),
                },
                "sha256:382dc24bcbc25c965c1c89e17947f0e5af1038b98a9fdc1500c5ee5da0c81793",
                "sha256:0df356b9d53134fe8ca96186751734d99da3d4b6e21967bfd2a29186f166770f",
            ),
            (
                Principal::Pack {
                    pack: pack.clone(),
                    hash: hash.clone(),
                },
                "sha256:7f8605bd864f44961bef8d3e4504bf1e2162a4aa4f8637cb0361fca537b78988",
                "sha256:0ba40558e80a7850c545e2f132e615f0ee3aebeddf8da7f3e8c56eae9ef0f167",
            ),
            (
                Principal::PackUi {
                    pack,
                    hash,
                    ui: UiSessionId::parse("ui.1").unwrap(),
                },
                "sha256:ed88dc8643b851f40b81f10e145c20ad2417847c26d452d976836a17bb31cc8e",
                "sha256:1d0a336e504002dcdf9b54ac759c89e342ed590a83492a11793435340b23b7a1",
            ),
        ];
        // Independent Python vectors through BOTH digest paths.
        for (principal, none_expected, some_expected) in &cases {
            let event = vector_event(&f, principal.clone());
            let body = canonical(&event).unwrap();
            let owned = OwnedAuditEvent::parse(&body, &f.profile, &budget).unwrap();
            for (seq, id, expected) in [
                (1u64, None, *none_expected),
                (7u64, Some(&event_id), *some_expected),
            ] {
                assert_eq!(
                    AuditRow::digest(seq, &prev, &event, id).unwrap().as_str(),
                    expected
                );
                assert_eq!(
                    owned
                        .unsigned_digest(seq, &prev, id, &budget)
                        .unwrap()
                        .as_str(),
                    expected
                );
            }
        }
        // Crafted same-profile rows: all four principals, Some/None event_id,
        // and one noncanonical whitespace + reordered-key body.
        let (pack, hash) = vector_pack();
        for (principal, identified, pretty) in [
            (Principal::User, false, false),
            (
                Principal::Input {
                    source: InputSourceId::parse("src-1").unwrap(),
                },
                true,
                false,
            ),
            (
                Principal::Pack {
                    pack: pack.clone(),
                    hash: hash.clone(),
                },
                false,
                true,
            ),
            (
                Principal::PackUi {
                    pack,
                    hash,
                    ui: UiSessionId::parse("ui.1").unwrap(),
                },
                true,
                false,
            ),
        ] {
            let event = vector_event(&f, principal);
            let (seq, prev) = tail(&f);
            let next = seq + 1;
            let id = identified.then(|| Digest::hash(format!("evt-{next}").as_bytes()));
            let digest = AuditRow::digest(next, &prev, &event, id.as_ref()).unwrap();
            let body = if pretty {
                serde_json::to_string_pretty(&event).unwrap().into_bytes()
            } else {
                canonical(&event).unwrap()
            };
            raw_insert(
                &f,
                next as i64,
                prev.as_str(),
                digest.as_str(),
                &body,
                id.as_ref().map(Digest::as_str),
            );
        }
        // Head uses the actual captured chain, never assumed row counts.
        let head = verify_history(&dispatch, &f.audit).unwrap();
        let (seq, digest) = tail(&f);
        assert_eq!((head.last_seq, head.digest), (seq, digest));
        f.gate
            .record_outcome(gid, Outcome::Cancelled)
            .await
            .unwrap();
    }

    #[tokio::test]
    async fn full_history_scan_detects_any_corruption() {
        let f = fixture();
        let (_track, budget) = budget();
        f.audit_raw()
            .execute_batch("DROP TRIGGER audit_no_update; DROP TRIGGER audit_no_delete;")
            .unwrap();
        let (gid, dispatch) = read_dispatch(&f, &budget, 0, 0).await;
        let (gid2, far) = read_dispatch(&f, &budget, i64::MAX as u64 + 1, 10).await;
        let event = vector_event(&f, Principal::User);
        let good = canonical(&event).unwrap();
        let bad = Digest::hash(b"bad");
        // max0 and after-tail dispatches still scan the whole chain.
        let check = |f: &Fixture| {
            for d in [&dispatch, &far] {
                assert!(matches!(
                    verify_history(d, &f.audit),
                    Err(StoreError::Integrity(IntegrityFaultCode::AuditChain))
                ));
            }
        };
        let head = verify_history(&dispatch, &f.audit).unwrap();
        assert_eq!((head.last_seq, head.digest), tail(&f));
        // Gap: well-formed row at tail+3.
        let (seq, prev) = tail(&f);
        let n = seq + 3;
        let d = AuditRow::digest(n, &prev, &event, None).unwrap();
        raw_insert(&f, n as i64, prev.as_str(), d.as_str(), &good, None);
        check(&f);
        raw_delete(&f, n as i64);
        // Nonpositive seq sorts first and breaks the seq==1 invariant.
        for n in [-3i64, 0] {
            raw_insert(&f, n, Digest::hash(&[]).as_str(), bad.as_str(), &good, None);
            check(&f);
            raw_delete(&f, n);
        }
        // Tail corruptions across prev/digest/event_id/body variants.
        let (seq, prev) = tail(&f);
        let n = seq + 1;
        let ok = AuditRow::digest(n, &prev, &event, None).unwrap();
        for (p, d, b, id) in [
            (bad.as_str(), ok.as_str(), good.as_slice(), None),
            ("junk", ok.as_str(), good.as_slice(), None),
            (prev.as_str(), bad.as_str(), good.as_slice(), None),
            (prev.as_str(), "sha256:xyz", good.as_slice(), None),
            (prev.as_str(), ok.as_str(), good.as_slice(), Some("nope")),
            (
                prev.as_str(),
                ok.as_str(),
                good.as_slice(),
                Some(bad.as_str()),
            ),
            (prev.as_str(), bad.as_str(), b"\xff\xfe".as_slice(), None),
            (prev.as_str(), bad.as_str(), b"[1,2]".as_slice(), None),
        ] {
            raw_insert(&f, n as i64, p, d, b, id);
            check(&f);
            raw_delete(&f, n as i64);
        }
        // Schema/profile corruptions inside a typed-valid row shell.
        for value in [
            serde_json::json!({"kind":"k","payload":null,"principal":{"kind":"user"},"profile":"prof_guest_audit"}),
            serde_json::json!({"kind":"k","payload":null,"principal":{"kind":"alien"},"profile":"prof_guest_audit","request":"req_owned"}),
            serde_json::json!({"kind":"k","payload":null,"principal":{"kind":"user"},"profile":"prof_other","request":"req_owned"}),
        ] {
            let body = canonical(&value).unwrap();
            raw_insert(&f, n as i64, prev.as_str(), bad.as_str(), &body, None);
            check(&f);
            raw_delete(&f, n as i64);
        }
        // Wrong stored SQL types are typed Integrity, never leaked FromSql.
        f.audit_raw()
            .execute(
                "INSERT INTO audit(seq,prev_digest,digest,body,writer,minute,size) VALUES(?1,?2,?3,42,'probe',0,2)",
                params![n as i64, prev.as_str(), bad.as_str()],
            )
            .unwrap();
        check(&f);
        raw_delete(&f, n as i64);
        f.audit_raw()
            .execute(
                "INSERT INTO audit(seq,prev_digest,digest,body,writer,minute,size) VALUES(?1,?2,?3,?4,'probe',0,?5)",
                params![n as i64, bad.as_str().as_bytes(), bad.as_str(), good.as_slice(), good.len() as i64],
            )
            .unwrap();
        check(&f);
        raw_delete(&f, n as i64);
        // Real SQL failure keeps its own class.
        let (_gid3, dispatch3) = read_dispatch(&f, &budget, 0, 10).await;
        f.gate
            .record_outcome(gid, Outcome::Cancelled)
            .await
            .unwrap();
        f.gate
            .record_outcome(gid2, Outcome::Cancelled)
            .await
            .unwrap();
        f.audit_raw().execute_batch("DROP TABLE audit").unwrap();
        assert!(matches!(
            verify_history(&dispatch3, &f.audit),
            Err(StoreError::Sql(_))
        ));
        drop(dispatch3);
    }

    #[tokio::test]
    async fn full_history_scan_consistent_snapshot() {
        let f = fixture();
        let (_track, inner) = budget();
        let (started, started_rx) = mpsc::channel::<()>();
        let (release, release_rx) = mpsc::channel::<()>();
        let sync = Arc::new(SyncBudget {
            inner,
            armed: AtomicBool::new(false),
            stepped: started,
            proceed: Mutex::new(release_rx),
        });
        let sync_budget: Arc<dyn AllocationBudget> = sync.clone();
        let (gid, dispatch) = read_dispatch(&f, &sync_budget, 0, 1000).await;
        let (seq0, prev0) = tail(&f);
        let event = vector_event(&f, Principal::User);
        let body = canonical(&event).unwrap();
        let digest = AuditRow::digest(seq0 + 1, &prev0, &event, None).unwrap();
        let writer = f.audit_raw();
        sync.armed.store(true, Ordering::Release);
        let audit = f.audit.clone();
        let head = std::thread::scope(|s| {
            let handle = s.spawn(move || verify_history(&dispatch, &audit));
            // Scan is inside its Deferred read snapshot now (DELETE journal).
            started_rx.recv().unwrap();
            writer.execute_batch("BEGIN IMMEDIATE").unwrap();
            writer
                .execute(
                    "INSERT INTO audit(seq,prev_digest,digest,body,writer,minute,size) VALUES(?1,?2,?3,?4,'w',0,?5)",
                    params![seq0 as i64 + 1, prev0.as_str(), digest.as_str(), body.as_slice(), body.len() as i64],
                )
                .unwrap();
            assert!(matches!(
                writer.execute_batch("COMMIT"),
                Err(rusqlite::Error::SqliteFailure(error, _))
                    if error.code == rusqlite::ErrorCode::DatabaseBusy
            ));
            writer.execute_batch("ROLLBACK").unwrap();
            release.send(()).unwrap();
            handle.join().unwrap()
        });
        let head = head.unwrap();
        assert_eq!((head.last_seq, head.digest), (seq0, prev0.clone()));
        // Committed state only: the same row now lands for a later scan.
        writer.execute_batch("BEGIN IMMEDIATE").unwrap();
        writer
            .execute(
                "INSERT INTO audit(seq,prev_digest,digest,body,writer,minute,size) VALUES(?1,?2,?3,?4,'w',0,?5)",
                params![seq0 as i64 + 1, prev0.as_str(), digest.as_str(), body.as_slice(), body.len() as i64],
            )
            .unwrap();
        writer.execute_batch("COMMIT").unwrap();
        f.gate
            .record_outcome(gid, Outcome::Cancelled)
            .await
            .unwrap();
        let (_track2, budget2) = budget();
        let (gid2, dispatch2) = read_dispatch(&f, &budget2, 0, 1000).await;
        let head2 = verify_history(&dispatch2, &f.audit).unwrap();
        assert_eq!((head2.last_seq, head2.digest), tail(&f));
        assert!(head2.last_seq > seq0);
        f.gate
            .record_outcome(gid2, Outcome::Cancelled)
            .await
            .unwrap();
    }

    #[tokio::test]
    async fn full_history_scan_denials_keep_class_and_release() {
        let f = fixture();
        for (offset, error) in [
            (1u64, AllocationError::Limit),
            (4u64, AllocationError::Unavailable),
            (7u64, AllocationError::Overflow),
            (11u64, AllocationError::Allocator),
        ] {
            let meter = FailMeter::new(error);
            let budget: Arc<dyn AllocationBudget> = meter.clone();
            let (gid, dispatch) = read_dispatch(&f, &budget, 0, 0).await;
            let baseline = meter.live();
            let consumed = meter.work();
            meter.fail_at.store(
                meter.reserves.load(Ordering::Acquire) + offset,
                Ordering::Release,
            );
            match (verify_history(&dispatch, &f.audit), error) {
                (Err(StoreError::Quota), AllocationError::Limit | AllocationError::Overflow)
                | (
                    Err(StoreError::Unavailable(_)),
                    AllocationError::Unavailable | AllocationError::Allocator,
                ) => {}
                _ => panic!("unexpected scan result"),
            }
            // Prepared-data baseline preserved; work never refunded.
            assert_eq!(meter.live(), baseline);
            assert!(meter.work() >= consumed);
            f.gate
                .record_outcome(gid, Outcome::Cancelled)
                .await
                .unwrap();
            drop(dispatch);
            assert_eq!(meter.live(), 0);
        }
        // Full success: no per-row graph is retained past its iteration.
        let meter = FailMeter::new(AllocationError::Limit);
        let budget: Arc<dyn AllocationBudget> = meter.clone();
        let (gid, dispatch) = read_dispatch(&f, &budget, 0, 0).await;
        let baseline = meter.live();
        let consumed = meter.work();
        let head = verify_history(&dispatch, &f.audit).unwrap();
        assert_eq!(head.last_seq, tail(&f).0);
        assert_eq!(meter.live(), baseline);
        assert!(meter.work() > consumed);
        f.gate
            .record_outcome(gid, Outcome::Cancelled)
            .await
            .unwrap();
        drop(dispatch);
        assert_eq!(meter.live(), 0);
    }

    #[tokio::test]
    async fn owned_read_execute_projects_complete_mixed_history() {
        let f = fixture();
        let (track, budget) = budget();
        let (gid, dispatch) = read_dispatch(&f, &budget, 0, 1000).await;
        let (pack, hash) = vector_pack();
        for (index, principal) in [
            Principal::User,
            Principal::Input {
                source: InputSourceId::parse("src-1").unwrap(),
            },
            Principal::Pack {
                pack: pack.clone(),
                hash: hash.clone(),
            },
            Principal::PackUi {
                pack,
                hash,
                ui: UiSessionId::parse("ui.1").unwrap(),
            },
        ]
        .into_iter()
        .enumerate()
        {
            let event = vector_event(&f, principal);
            let (seq, prev) = tail(&f);
            let id =
                (index % 2 == 1).then(|| Digest::hash(format!("owned-read-{index}").as_bytes()));
            let digest = AuditRow::digest(seq + 1, &prev, &event, id.as_ref()).unwrap();
            let body = if index == 2 {
                serde_json::to_string_pretty(&event).unwrap().into_bytes()
            } else {
                canonical(&event).unwrap()
            };
            raw_insert(
                &f,
                seq as i64 + 1,
                prev.as_str(),
                digest.as_str(),
                &body,
                id.as_ref().map(Digest::as_str),
            );
        }
        // A trusted legacy row oracle covers EVERY row, including the real
        // authorize event, in the same committed pre-execute history.
        let expected: Vec<Vec<u8>> = {
            let conn = f.audit_raw();
            let mut statement = conn
                .prepare("SELECT seq,prev_digest,digest,body,event_id FROM audit ORDER BY seq")
                .unwrap();
            let mut rows = statement.query([]).unwrap();
            let mut expected = Vec::new();
            while let Some(row) = rows.next().unwrap() {
                let seq: i64 = row.get(0).unwrap();
                let prev: String = row.get(1).unwrap();
                let digest: String = row.get(2).unwrap();
                let body: Vec<u8> = row.get(3).unwrap();
                let id: Option<String> = row.get(4).unwrap();
                let event: AuditEvent = crate::decode(&body).unwrap();
                let typed_prev = Digest::parse(&prev).unwrap();
                let typed_id = id.as_deref().map(Digest::parse).transpose().unwrap();
                assert_eq!(
                    AuditRow::digest(seq as u64, &typed_prev, &event, typed_id.as_ref())
                        .unwrap()
                        .as_str(),
                    digest
                );
                let projection = serde_json::json!({
                    "seq": seq.to_string(), "prev_digest": prev, "digest": digest,
                    "event_id": id, "event": event,
                });
                expected.push(canonical(&projection).unwrap());
            }
            expected
        };
        let delivery = dispatch.execute(&f.audit);
        {
            let actual = delivery.result().unwrap().rows().unwrap().array().unwrap();
            assert_eq!(actual.len(), expected.len());
            for (value, expected) in actual.iter().zip(&expected) {
                assert_eq!(value.as_str().unwrap().as_bytes(), expected.as_slice());
            }
        }
        assert!(!delivery.is_committed());
        f.gate
            .record_outcome(gid.clone(), delivery.terminal_outcome(true))
            .await
            .unwrap();
        assert!(
            f.approvals
                .terminal_delivery(&gid)
                .unwrap()
                .unwrap()
                .acknowledged
        );
        drop(delivery);
        assert_eq!(track.live.load(Ordering::Acquire), 0);
    }

    #[tokio::test]
    async fn owned_read_execute_rejects_unselected_corruption() {
        for (after, max) in [(u64::MAX, 10), (0, 0), (0, 1)] {
            let f = fixture();
            let (track, budget) = budget();
            f.audit_raw()
                .execute_batch("DROP TRIGGER audit_no_delete")
                .unwrap();
            // Authorize and consume BEFORE corruption; Gate itself audits.
            let (gid, dispatch) = read_dispatch(&f, &budget, after, max).await;
            let (seq, prev) = tail(&f);
            let bad = Digest::hash(b"invalid stored schema");
            raw_insert(&f, seq as i64 + 1, prev.as_str(), bad.as_str(), b"{}", None);
            let delivery = dispatch.execute(&f.audit);
            assert!(matches!(
                delivery.result(),
                Err(StoreError::Integrity(IntegrityFaultCode::AuditChain))
            ));
            assert!(!delivery.is_committed());
            assert_eq!(track.live.load(Ordering::Acquire), 0);
            // Restore only the deliberately injected row before real Gate
            // terminal persistence, which independently verifies its chain.
            raw_delete(&f, seq as i64 + 1);
            let outcome = delivery.terminal_outcome(false);
            assert_eq!(
                outcome,
                Outcome::Failed {
                    code: "persistence".into()
                }
            );
            f.gate.record_outcome(gid.clone(), outcome).await.unwrap();
            let saved = f.approvals.terminal_delivery(&gid).unwrap().unwrap();
            assert!(saved.acknowledged);
            assert_eq!(
                saved.decision.outcome,
                Outcome::Failed {
                    code: "persistence".into()
                }
            );
        }
    }

    #[tokio::test]
    async fn owned_read_execute_retains_snapshot_through_projection() {
        let f = fixture();
        let (track, inner) = budget();
        let (started, started_rx) = mpsc::channel::<()>();
        let (release, release_rx) = mpsc::channel::<()>();
        let sync = Arc::new(SyncBudget {
            inner,
            armed: AtomicBool::new(false),
            stepped: started,
            proceed: Mutex::new(release_rx),
        });
        let sync_budget: Arc<dyn AllocationBudget> = sync.clone();
        let (gid, dispatch) = read_dispatch(&f, &sync_budget, 0, 1000).await;
        let (seq, prev) = tail(&f);
        let event = vector_event(&f, Principal::User);
        let body = canonical(&event).unwrap();
        let digest = AuditRow::digest(seq + 1, &prev, &event, None).unwrap();
        let writer = f.audit_raw();
        sync.armed.store(true, Ordering::Release);
        let audit = f.audit.clone();
        let delivery = std::thread::scope(|scope| {
            let task = scope.spawn(move || dispatch.execute(&audit));
            started_rx.recv().unwrap();
            writer.execute_batch("BEGIN IMMEDIATE").unwrap();
            writer.execute(
                "INSERT INTO audit(seq,prev_digest,digest,body,writer,minute,size) VALUES(?1,?2,?3,?4,'probe',0,?5)",
                params![seq as i64 + 1, prev.as_str(), digest.as_str(), body.as_slice(), body.len() as i64],
            ).unwrap();
            assert!(
                matches!(writer.execute_batch("COMMIT"), Err(rusqlite::Error::SqliteFailure(error, _)) if error.code == rusqlite::ErrorCode::DatabaseBusy)
            );
            writer.execute_batch("ROLLBACK").unwrap();
            release.send(()).unwrap();
            task.join().unwrap()
        });
        let projected = rows(&delivery);
        assert_eq!(projected.len(), seq as usize);
        assert_eq!(projected.last().unwrap()["seq"], seq.to_string());
        assert_eq!(projected.last().unwrap()["digest"], prev.as_str());
        // The exact same valid row can commit only after the read TX/rows
        // and projection have completed. A later real execute must see it.
        writer.execute_batch("BEGIN IMMEDIATE").unwrap();
        writer.execute(
            "INSERT INTO audit(seq,prev_digest,digest,body,writer,minute,size) VALUES(?1,?2,?3,?4,'probe',0,?5)",
            params![seq as i64 + 1, prev.as_str(), digest.as_str(), body.as_slice(), body.len() as i64],
        ).unwrap();
        writer.execute_batch("COMMIT").unwrap();
        f.gate
            .record_outcome(gid, delivery.terminal_outcome(true))
            .await
            .unwrap();
        drop(delivery);
        assert_eq!(track.live.load(Ordering::Acquire), 0);
        let (gid2, later) = run_read(&f, &f.cx, &sync_budget, seq, 1000).await;
        let later_rows = rows(&later);
        assert_eq!(later_rows.first().unwrap()["digest"], digest.as_str());
        f.gate
            .record_outcome(gid2, later.terminal_outcome(true))
            .await
            .unwrap();
        drop(later);
        assert_eq!(track.live.load(Ordering::Acquire), 0);
    }

    #[tokio::test]
    async fn owned_read_execute_denials_release_partial_output() {
        let f = fixture();
        let meter = FailMeter::new(AllocationError::Limit);
        let budget: Arc<dyn AllocationBudget> = meter.clone();
        let (gid, dispatch) = read_dispatch(&f, &budget, 0, 1000).await;
        let before = meter.reserves.load(Ordering::Acquire);
        let delivery = dispatch.execute(&f.audit);
        assert!(delivery.result().is_ok());
        let calls = meter.reserves.load(Ordering::Acquire) - before;
        assert!(calls > 20);
        f.gate
            .record_outcome(gid, delivery.terminal_outcome(true))
            .await
            .unwrap();
        drop(delivery);
        assert_eq!(meter.live(), 0);
        // Early row/metadata, intermediate parse/hash, and the final output
        // reservations all run through the real consumed Read.execute path.
        for error in [
            AllocationError::Limit,
            AllocationError::Unavailable,
            AllocationError::Overflow,
            AllocationError::Allocator,
        ] {
            for offset in [1, 2, 3, 7, calls / 2, calls - 1, calls] {
                let f = fixture();
                let meter = FailMeter::new(error);
                let budget: Arc<dyn AllocationBudget> = meter.clone();
                let (gid, dispatch) = read_dispatch(&f, &budget, 0, 1000).await;
                let baseline = meter.live();
                let work = meter.work();
                meter.fail_at.store(
                    meter.reserves.load(Ordering::Acquire) + offset,
                    Ordering::Release,
                );
                let delivery = dispatch.execute(&f.audit);
                match (delivery.result(), error) {
                    (
                        Err(StoreError::Quota),
                        AllocationError::Limit | AllocationError::Overflow,
                    )
                    | (
                        Err(StoreError::Unavailable(_)),
                        AllocationError::Unavailable | AllocationError::Allocator,
                    ) => {}
                    _ => panic!("actual owned read lost its allocation error class"),
                }
                assert_eq!(meter.live(), baseline);
                assert!(meter.work() >= work);
                assert!(!delivery.is_committed());
                f.gate
                    .record_outcome(gid.clone(), delivery.terminal_outcome(false))
                    .await
                    .unwrap();
                assert!(
                    f.approvals
                        .terminal_delivery(&gid)
                        .unwrap()
                        .unwrap()
                        .acknowledged
                );
                drop(delivery);
                assert_eq!(meter.live(), 0);
            }
        }
    }

    async fn append_dispatch(
        f: &Fixture,
        budget: &Arc<dyn AllocationBudget>,
        payload: CoreValue,
    ) -> (GrantId, GuestAuditDispatch) {
        let kind = CoreValue::string("owned.note", budget).unwrap();
        let prepared = GuestAuditAppend::prepare(&f.cx, kind, payload, budget).unwrap();
        let grant = f
            .gate
            .authorize(&f.cx, prepared.request(), prepared.args_digest().clone())
            .await
            .unwrap();
        (grant.id().clone(), ready(prepared.consume(grant)))
    }

    #[tokio::test]
    async fn owned_append_constructor_matches_typed_legacy_schema() {
        let f = fixture();
        let (track, budget) = budget();
        let (pack, hash) = vector_pack();
        for (principal, writer) in [
            (Principal::User, "user"),
            (
                Principal::Input {
                    source: InputSourceId::parse("src-1").unwrap(),
                },
                "input:src-1",
            ),
            (
                Principal::Pack {
                    pack: pack.clone(),
                    hash: hash.clone(),
                },
                "pack:local.guest-audit",
            ),
            (
                Principal::PackUi {
                    pack,
                    hash,
                    ui: UiSessionId::parse("ui.1").unwrap(),
                },
                "pack:local.guest-audit",
            ),
        ] {
            // Data-schema proof only: no dispatch authority is created or
            // claimed by the constructor. Runtime tests below consume Gate.
            let cx = CallCx::root(
                f.profile.clone(),
                principal,
                RequestId::parse("req_constructor").unwrap(),
                CancellationToken::new(),
                Instant::now() + Duration::from_secs(30),
                limits(),
            );
            let kind = CoreValue::string("", &budget).unwrap();
            let raw = br#"{"kind":"forged","profile":"forged","payload":{"deep":[1,"\u0001\n"]}}"#;
            let payload = CoreValue::parse_guest(raw, &budget).unwrap();
            let owned = OwnedAuditEvent::for_append(&cx, &kind, &payload, &budget).unwrap();
            let expected = AuditEvent {
                profile: cx.profile().clone(),
                principal: cx.principal().clone(),
                request: cx.request().clone(),
                kind: "guest-event".into(),
                payload: CanonValue::Obj(BTreeMap::from([
                    ("kind".into(), CanonValue::Str("".into())),
                    (
                        "payload".into(),
                        tobkiri_canon::parse(raw, Variant::StrictV1).unwrap(),
                    ),
                ])),
            };
            assert_eq!(
                owned.event.encode_guest(&budget).unwrap().as_bytes(),
                canonical(&expected).unwrap().as_slice()
            );
            assert_eq!(owned.writer_key().unwrap().as_str().unwrap(), writer);
            let prev = Digest::hash(b"constructor prev");
            assert_eq!(
                owned.append_digest(7, &prev, &budget).unwrap(),
                AuditRow::digest(7, &prev, &expected, None).unwrap()
            );
        }
        assert_eq!(track.live.load(Ordering::Acquire), 0);
    }

    #[tokio::test]
    async fn owned_append_execute_rejects_corrupt_history_before_write() {
        let f = fixture();
        let (track, budget) = budget();
        f.audit_raw()
            .execute_batch("DROP TRIGGER audit_no_delete")
            .unwrap();
        let payload = CoreValue::parse_guest(b"1", &budget).unwrap();
        let (gid, dispatch) = append_dispatch(&f, &budget, payload).await;
        let (seq, prev) = tail(&f);
        let event = vector_event(&f, Principal::User);
        let bad = Digest::hash(b"wrong chain digest");
        raw_insert(
            &f,
            seq as i64 + 1,
            prev.as_str(),
            bad.as_str(),
            &canonical(&event).unwrap(),
            None,
        );
        let before = tail(&f);
        let delivery = dispatch.execute(&f.audit);
        assert!(matches!(
            delivery.result(),
            Err(StoreError::Integrity(IntegrityFaultCode::AuditChain))
        ));
        assert!(!delivery.is_committed());
        assert_eq!(tail(&f), before);
        assert_eq!(track.live.load(Ordering::Acquire), 0);
        raw_delete(&f, seq as i64 + 1);
        let outcome = delivery.terminal_outcome(false);
        assert_eq!(
            outcome,
            Outcome::Failed {
                code: "persistence".into()
            }
        );
        f.gate.record_outcome(gid.clone(), outcome).await.unwrap();
        assert!(
            f.approvals
                .terminal_delivery(&gid)
                .unwrap()
                .unwrap()
                .acknowledged
        );
    }

    #[tokio::test]
    async fn owned_append_execute_preserves_configured_quotas_and_caller_depth() {
        for quota in [
            AuditQuota {
                records_per_minute: 0,
                ..AuditQuota::default()
            },
            AuditQuota {
                bytes_per_minute: 0,
                ..AuditQuota::default()
            },
            AuditQuota {
                total_bytes: 0,
                ..AuditQuota::default()
            },
        ] {
            let f = fixture();
            // Public trusted Store quota configuration, same real profile/DB;
            // authorization still uses the genuine Gate and original context.
            let limited = AuditLog::open(&f.root, &f.profile, quota).unwrap();
            let (track, budget) = budget();
            let payload = CoreValue::parse_guest(b"null", &budget).unwrap();
            let (gid, dispatch) = append_dispatch(&f, &budget, payload).await;
            let before = tail(&f);
            let delivery = dispatch.execute(&limited);
            assert!(matches!(delivery.result(), Err(StoreError::Quota)));
            assert!(!delivery.is_committed());
            assert_eq!(tail(&f), before);
            assert_eq!(track.live.load(Ordering::Acquire), 0);
            f.gate
                .record_outcome(gid.clone(), delivery.terminal_outcome(false))
                .await
                .unwrap();
            assert!(
                f.approvals
                    .terminal_delivery(&gid)
                    .unwrap()
                    .unwrap()
                    .acknowledged
            );
        }
        let f = fixture();
        let (track, budget) = budget();
        // Argument tuple fits depth64; the additional complete-event envelope
        // cannot. This is a CALLER Quota, never persisted-chain Integrity.
        let raw = format!("{}0{}", "[".repeat(63), "]".repeat(63));
        let payload = CoreValue::parse_guest(raw.as_bytes(), &budget).unwrap();
        let (gid, dispatch) = append_dispatch(&f, &budget, payload).await;
        let before = tail(&f);
        let delivery = dispatch.execute(&f.audit);
        assert!(matches!(delivery.result(), Err(StoreError::Quota)));
        assert!(!delivery.is_committed());
        assert_eq!(tail(&f), before);
        assert_eq!(track.live.load(Ordering::Acquire), 0);
        f.gate
            .record_outcome(gid, delivery.terminal_outcome(false))
            .await
            .unwrap();
    }

    struct AppendSyncBudget {
        inner: Arc<dyn AllocationBudget>,
        armed: AtomicBool,
        stepped: mpsc::Sender<()>,
        proceed: Mutex<mpsc::Receiver<()>>,
    }
    impl AllocationBudget for AppendSyncBudget {
        fn reserve(&self, request: AllocationRequest) -> Result<(), AllocationError> {
            // Observe the existing fixed scan's first borrowed-row boundary;
            // constructor reserves occur before this. No production hook.
            let scan_row = matches!(request.kind, AllocationKind::Parse)
                && request.bytes == 0
                && request.elements == 5
                && request.work == 32;
            let result = self.inner.reserve(request);
            if result.is_ok() && scan_row && self.armed.swap(false, Ordering::AcqRel) {
                self.stepped.send(()).unwrap();
                self.proceed.lock().unwrap().recv().unwrap();
            }
            result
        }
        fn release(&self, bytes: u64) {
            self.inner.release(bytes);
        }
    }

    #[tokio::test]
    async fn owned_append_execute_holds_one_immediate_transaction() {
        let f = fixture();
        let (track, inner) = budget();
        let (stepped, stepped_rx) = mpsc::channel();
        let (release, release_rx) = mpsc::channel();
        let sync = Arc::new(AppendSyncBudget {
            inner,
            armed: AtomicBool::new(false),
            stepped,
            proceed: Mutex::new(release_rx),
        });
        let budget: Arc<dyn AllocationBudget> = sync.clone();
        let payload = CoreValue::parse_guest(b"1", &budget).unwrap();
        let (gid, dispatch) = append_dispatch(&f, &budget, payload).await;
        let before = tail(&f);
        let writer = f.audit_raw();
        let audit = f.audit.clone();
        sync.armed.store(true, Ordering::Release);
        let delivery = std::thread::scope(|scope| {
            let task = scope.spawn(move || dispatch.execute(&audit));
            stepped_rx.recv().unwrap();
            // A competing writer cannot acquire a write transaction while
            // the actual append is verifying its original head and quotas.
            assert!(
                matches!(writer.execute_batch("BEGIN IMMEDIATE"), Err(rusqlite::Error::SqliteFailure(error, _)) if error.code == rusqlite::ErrorCode::DatabaseBusy)
            );
            release.send(()).unwrap();
            task.join().unwrap()
        });
        assert!(delivery.is_committed());
        assert!(matches!(delivery.result(), Ok(GuestAuditOutput::Appended)));
        assert_eq!(tail(&f).0, before.0 + 1);
        writer.execute_batch("BEGIN IMMEDIATE; ROLLBACK;").unwrap();
        f.audit.verify().unwrap();
        assert_eq!(track.live.load(Ordering::Acquire), 0);
        f.gate
            .record_outcome(gid.clone(), delivery.terminal_outcome(true))
            .await
            .unwrap();
        let saved = f.approvals.terminal_delivery(&gid).unwrap().unwrap();
        assert!(saved.acknowledged && saved.decision.outcome == Outcome::Succeeded);
    }

    #[tokio::test]
    async fn owned_append_execute_resource_denials_have_no_insert() {
        let f = fixture();
        let meter = FailMeter::new(AllocationError::Limit);
        let budget: Arc<dyn AllocationBudget> = meter.clone();
        let payload = CoreValue::parse_guest(b"1", &budget).unwrap();
        let (gid, dispatch) = append_dispatch(&f, &budget, payload).await;
        let start = meter.reserves.load(Ordering::Acquire);
        let delivery = dispatch.execute(&f.audit);
        assert!(delivery.is_committed());
        let calls = meter.reserves.load(Ordering::Acquire) - start;
        assert!(calls > 20);
        assert_eq!(meter.live(), 0);
        f.gate
            .record_outcome(gid, delivery.terminal_outcome(true))
            .await
            .unwrap();
        for error in [
            AllocationError::Limit,
            AllocationError::Unavailable,
            AllocationError::Overflow,
            AllocationError::Allocator,
        ] {
            for offset in [1, 2, 7, calls / 2, calls - 1, calls] {
                let f = fixture();
                let meter = FailMeter::new(error);
                let budget: Arc<dyn AllocationBudget> = meter.clone();
                let payload = CoreValue::parse_guest(b"1", &budget).unwrap();
                let (gid, dispatch) = append_dispatch(&f, &budget, payload).await;
                let before = tail(&f);
                let work = meter.work();
                meter.fail_at.store(
                    meter.reserves.load(Ordering::Acquire) + offset,
                    Ordering::Release,
                );
                let delivery = dispatch.execute(&f.audit);
                match (delivery.result(), error) {
                    (
                        Err(StoreError::Quota),
                        AllocationError::Limit | AllocationError::Overflow,
                    )
                    | (
                        Err(StoreError::Unavailable(_)),
                        AllocationError::Unavailable | AllocationError::Allocator,
                    ) => {}
                    _ => panic!("owned append lost its actual allocation error class"),
                }
                assert!(!delivery.is_committed());
                assert_eq!(tail(&f), before);
                assert_eq!(meter.live(), 0);
                assert!(meter.work() >= work);
                f.gate
                    .record_outcome(gid.clone(), delivery.terminal_outcome(false))
                    .await
                    .unwrap();
                assert!(
                    f.approvals
                        .terminal_delivery(&gid)
                        .unwrap()
                        .unwrap()
                        .acknowledged
                );
            }
        }
    }

    #[tokio::test]
    async fn owned_append_execute_rejects_bad_quota_metadata() {
        for fault in [
            "writer-type",
            "writer-utf8",
            "minute",
            "size-negative",
            "size-type",
        ] {
            let f = fixture();
            let (track, budget) = budget();
            f.audit_raw()
                .execute_batch("DROP TRIGGER audit_no_update")
                .unwrap();
            let payload = CoreValue::parse_guest(b"1", &budget).unwrap();
            let (gid, dispatch) = append_dispatch(&f, &budget, payload).await;
            let before = tail(&f);
            let conn = f.audit_raw();
            let (writer, minute, size): (String, i64, i64) = conn
                .query_row(
                    "SELECT writer,minute,size FROM audit WHERE seq=?1",
                    [before.0 as i64],
                    |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
                )
                .unwrap();
            let sql = match fault {
                "writer-type" => "UPDATE audit SET writer=X'FF' WHERE seq=?1",
                "writer-utf8" => "UPDATE audit SET writer=CAST(X'FF' AS TEXT) WHERE seq=?1",
                "minute" => "UPDATE audit SET minute='bad' WHERE seq=?1",
                "size-negative" => "UPDATE audit SET size=-1 WHERE seq=?1",
                _ => "UPDATE audit SET size=0.5 WHERE seq=?1",
            };
            conn.execute(sql, [before.0 as i64]).unwrap();
            let delivery = dispatch.execute(&f.audit);
            assert!(matches!(
                delivery.result(),
                Err(StoreError::Integrity(IntegrityFaultCode::AuditChain))
            ));
            assert!(!delivery.is_committed());
            assert_eq!(tail(&f), before);
            assert_eq!(track.live.load(Ordering::Acquire), 0);
            conn.execute(
                "UPDATE audit SET writer=?1,minute=?2,size=?3 WHERE seq=?4",
                params![writer, minute, size, before.0 as i64],
            )
            .unwrap();
            // No SQLite row/statement/transaction is held across terminal work.
            drop(conn);
            f.gate
                .record_outcome(gid.clone(), delivery.terminal_outcome(false))
                .await
                .unwrap();
            assert!(
                f.approvals
                    .terminal_delivery(&gid)
                    .unwrap()
                    .unwrap()
                    .acknowledged
            );
        }
    }
}
