use async_trait::async_trait;
use std::{
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};
use tobkiri_canon::{
    AllocationBudget, AllocationError, AllocationRequest, CanonValue, CoreRef, CoreValue, Digest,
    ids::*,
};
use tobkiri_gate::*;
use tobkiri_store::*;
struct NoPrompt;
#[async_trait]
impl TrustedPrompt for NoPrompt {
    async fn prompt_approval(&self, _: ApprovalPrompt) -> ApprovalAnswer {
        ApprovalAnswer::Deny
    }
}
struct TestBudget(AtomicU64);
impl AllocationBudget for TestBudget {
    fn reserve(&self, request: AllocationRequest) -> Result<(), AllocationError> {
        self.0
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |used| {
                used.checked_add(request.bytes)
                    .filter(|next| *next <= 4 * 1024 * 1024)
            })
            .map(|_| ())
            .map_err(|_| AllocationError::Limit)
    }
    fn release(&self, bytes: u64) {
        self.0.fetch_sub(bytes, Ordering::AcqRel);
    }
}
fn typed_statement(
    statement: &str,
    params: Vec<CanonValue>,
    budget: &Arc<dyn AllocationBudget>,
) -> OwnedSqlStatement {
    let value = CanonValue::Obj(std::collections::BTreeMap::from([
        ("statement".into(), CanonValue::Str(statement.into())),
        ("params".into(), CanonValue::Arr(params)),
        ("max-rows".into(), CanonValue::Int(10)),
    ]));
    let value = CoreValue::try_from_borrowed(&value, budget).unwrap();
    OwnedSqlStatement::from_value(value).unwrap()
}
fn text_cell(value: &str) -> CanonValue {
    CanonValue::Obj(std::collections::BTreeMap::from([
        ("type".into(), CanonValue::Str("text".into())),
        ("value".into(), CanonValue::Str(value.into())),
    ]))
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
    fn new() -> Self {
        let dir = tempfile::tempdir().unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(dir.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
        }
        let root = DataRoot::open(dir.path().canonicalize().unwrap()).unwrap();
        let profile = ProfileId::parse("prof_terminal").unwrap();
        let approvals = Arc::new(Approvals::open(&root, &profile).unwrap());
        let audit = Arc::new(AuditLog::open(&root, &profile, AuditQuota::default()).unwrap());
        let gate = Arc::new(GateEngine::new(
            approvals.clone(),
            audit.clone(),
            Arc::new(NoPrompt),
            MediatorChain::default(),
        ));
        let pack = PackId::parse("local.terminal").unwrap();
        let hash = PackHash::new(Digest::hash(b"pack"));
        let limits = ResourceLimits {
            fuel_per_call: 100000,
            memory_mib: 64,
            wall_ms: 30000,
            table_elements: 4096,
            output_bytes: 1024,
        };
        let policy = PermissionSet {
            audit_read: true,
            wasm: limits.clone(),
            ..Default::default()
        };
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
            RequestId::parse("req_terminal").unwrap(),
            CancellationToken::new(),
            Instant::now() + Duration::from_secs(30),
            limits,
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
    async fn consumed(&self) -> (GrantId, u64, DispatchPermit) {
        let args = Digest::hash(b"read");
        let grant = self
            .gate
            .authorize(&self.cx, EffectRequest::AuditRead, args.clone())
            .await
            .unwrap();
        let id = grant.id().clone();
        let expiry = grant.expires_unix_ms();
        let permit = grant
            .consume(&self.cx, &EffectRequest::AuditRead, &args)
            .unwrap();
        (id, expiry, permit)
    }
    fn raw(&self) -> rusqlite::Connection {
        rusqlite::Connection::open(
            self.root
                .path()
                .join(format!("profiles/{}/approvals.sqlite", self.profile)),
        )
        .unwrap()
    }
    fn audit_raw(&self) -> rusqlite::Connection {
        rusqlite::Connection::open(
            self.root
                .path()
                .join(format!("profiles/{}/audit.sqlite", self.profile)),
        )
        .unwrap()
    }
    fn fresh_gate(&self) -> GateEngine {
        GateEngine::new(
            Arc::new(Approvals::open(&self.root, &self.profile).unwrap()),
            self.audit.clone(),
            Arc::new(NoPrompt),
            MediatorChain::default(),
        )
    }
    fn outcome_count(&self) -> i64 {
        self.audit_raw()
            .query_row(
                "SELECT COUNT(*) FROM audit WHERE event_id IS NOT NULL",
                [],
                |r| r.get(0),
            )
            .unwrap()
    }
}
#[tokio::test]
async fn failure_before_terminal_commit_rolls_back_the_entire_decision_and_outbox() {
    let f = Fixture::new();
    let (id, _, _) = f.consumed().await;
    let raw = f.raw();
    raw.execute_batch("CREATE TRIGGER terminal_test_failure BEFORE INSERT ON terminal_outbox BEGIN SELECT RAISE(ABORT,'precommit fault'); END;").unwrap();
    assert!(
        f.gate
            .record_outcome(id.clone(), Outcome::Succeeded)
            .await
            .is_err()
    );
    assert_eq!(
        f.approvals.reservation_state(&id).unwrap().as_deref(),
        Some("consumed")
    );
    assert!(f.approvals.terminal_delivery(&id).unwrap().is_none());
    assert_eq!(f.outcome_count(), 0);
    raw.execute_batch("DROP TRIGGER terminal_test_failure;")
        .unwrap();
    f.gate.record_outcome(id, Outcome::Succeeded).await.unwrap();
    assert_eq!(f.outcome_count(), 1);
}
#[tokio::test]
async fn commit_before_sink_recovers_and_retries_same_decision_stably_after_ack() {
    let f = Fixture::new();
    let (id, _, _) = f.consumed().await;
    let first = f
        .approvals
        .finish_terminal(&id, &Outcome::Succeeded)
        .unwrap();
    assert!(!first.acknowledged);
    assert_eq!(f.outcome_count(), 0);
    let fresh = f.fresh_gate();
    let watermark = fresh.outbox_watermark(&f.profile).unwrap();
    assert_eq!(
        fresh
            .flush_outbox_through(&f.profile, watermark)
            .await
            .unwrap(),
        1
    );
    fresh
        .record_outcome(id.clone(), Outcome::Succeeded)
        .await
        .unwrap();
    fresh
        .record_outcome(id.clone(), Outcome::Succeeded)
        .await
        .unwrap();
    assert_eq!(f.outcome_count(), 1);
    assert!(
        f.approvals
            .terminal_delivery(&id)
            .unwrap()
            .unwrap()
            .acknowledged
    );
    assert!(
        fresh
            .record_outcome(id.clone(), Outcome::Cancelled)
            .await
            .is_err()
    );
    assert_eq!(
        f.approvals
            .terminal_delivery(&id)
            .unwrap()
            .unwrap()
            .decision
            .outcome,
        Outcome::Succeeded
    );
    f.audit.verify().unwrap();
}
#[tokio::test]
async fn delivered_receipts_survive_pruning_and_reused_ids_and_conflicting_codes_fail_closed() {
    let f = Fixture::new();
    let (id, expiry, _) = f.consumed().await;
    for code in ["plaintext secret contents", &"x".repeat(129)] {
        assert!(matches!(
            f.approvals
                .finish_terminal(&id, &Outcome::Failed { code: code.into() }),
            Err(GateError::Invalid(_))
        ));
    }
    assert_eq!(
        f.approvals.reservation_state(&id).unwrap().as_deref(),
        Some("consumed")
    );
    f.gate
        .record_outcome(id.clone(), Outcome::Succeeded)
        .await
        .unwrap();
    let original = f.approvals.terminal_delivery(&id).unwrap().unwrap();
    f.approvals
        .sweep_expired(&f.profile, expiry + 2 * 86_400_000)
        .unwrap();
    let reopened = Approvals::open(&f.root, &f.profile).unwrap();
    let retained = reopened.finish_terminal(&id, &Outcome::Succeeded).unwrap();
    assert_eq!(retained.decision.event_id, original.decision.event_id);
    assert!(retained.acknowledged);
    assert!(matches!(
        reopened.reserve(&original.decision.reservation),
        Err(GateError::Consumed)
    ));
    assert!(matches!(
        reopened.finish_terminal(&id, &Outcome::Cancelled),
        Err(GateError::Denied(_))
    ));
    assert_eq!(f.outcome_count(), 1);
}
#[tokio::test]
async fn revoked_epoch_cannot_consume_and_consumed_unknown_expiry_keeps_original_deadline() {
    let f = Fixture::new();
    let args = Digest::hash(b"stale");
    let grant = f
        .gate
        .authorize(&f.cx, EffectRequest::AuditRead, args.clone())
        .await
        .unwrap();
    let stale = grant.id().clone();
    let (consumed, original_expiry, permit) = f.consumed().await;
    // Consumption durably narrows the deadline using the actual monotonic
    // budget, so its millisecond expiry can precede the dispatched grant's.
    let body: Vec<u8> = f
        .raw()
        .query_row(
            "SELECT body FROM reservations WHERE id=?1 AND state='consumed'",
            [consumed.as_str()],
            |row| row.get(0),
        )
        .unwrap();
    let consumed_reservation: Reservation = serde_json::from_slice(&body).unwrap();
    assert_eq!(
        consumed_reservation.original_expires_unix_ms,
        Some(original_expiry)
    );
    let expiry = consumed_reservation.expires_unix_ms;
    assert!(expiry <= original_expiry);
    f.gate.watch_lease(&f.cx, &permit).unwrap();
    f.approvals.advance_security_epoch(&f.profile).unwrap();
    assert!(matches!(
        grant.consume(&f.cx, &EffectRequest::AuditRead, &args),
        Err(GateError::StaleEpoch)
    ));
    assert_eq!(
        f.approvals
            .terminal_delivery(&stale)
            .unwrap()
            .unwrap()
            .decision
            .disposition,
        TerminalDisposition::AbandonedBeforeUse
    );
    assert_eq!(
        f.gate.recover_profile_at(&f.profile, expiry - 1).unwrap(),
        0
    );
    assert!(f.approvals.terminal_delivery(&consumed).unwrap().is_none());
    assert_eq!(f.gate.recover_profile_at(&f.profile, expiry).unwrap(), 1);
    let decision = f
        .approvals
        .terminal_delivery(&consumed)
        .unwrap()
        .unwrap()
        .decision;
    assert_eq!(decision.reservation.expires_unix_ms, expiry);
    assert_eq!(
        decision.reservation.original_expires_unix_ms,
        Some(original_expiry)
    );
    assert_eq!(
        decision.disposition,
        TerminalDisposition::ExpiredCleanupUnknown
    );
    assert_eq!(
        decision.outcome,
        Outcome::Failed {
            code: "deadline-failure-unknown".into()
        }
    );
    assert!(f.cx.cancel().is_cancelled());
}
struct SinkFault {
    inner: Arc<AuditLog>,
    fail: AtomicBool,
    after_insert: bool,
}
impl AuditSink for SinkFault {
    fn append(&self, e: AuditEvent) -> Result<(), GateError> {
        self.inner.append(e)
    }
    fn append_idempotent(&self, id: &Digest, e: AuditEvent) -> Result<(), GateError> {
        if self.after_insert {
            self.inner.append_idempotent(id, e.clone())?;
        }
        if self.fail.swap(false, Ordering::SeqCst) {
            return Err(GateError::Persistence("sink fault".into()));
        }
        self.inner.append_idempotent(id, e)
    }
}
#[tokio::test]
async fn outage_and_sink_insert_before_ack_are_retryable_without_duplicate_audit_or_effect() {
    for after_insert in [false, true] {
        let f = Fixture::new();
        let (id, _, _) = f.consumed().await;
        let gate = GateEngine::new(
            f.approvals.clone(),
            Arc::new(SinkFault {
                inner: f.audit.clone(),
                fail: AtomicBool::new(true),
                after_insert,
            }),
            Arc::new(NoPrompt),
            MediatorChain::default(),
        );
        assert!(
            gate.record_outcome(id.clone(), Outcome::Succeeded)
                .await
                .is_err()
        );
        assert_eq!(
            f.approvals.reservation_state(&id).unwrap().as_deref(),
            Some("finished")
        );
        assert!(
            !f.approvals
                .terminal_delivery(&id)
                .unwrap()
                .unwrap()
                .acknowledged
        );
        assert_eq!(f.outcome_count(), i64::from(after_insert));
        let fresh = f.fresh_gate();
        fresh.flush_outbox(&f.profile).await.unwrap();
        fresh.record_outcome(id, Outcome::Succeeded).await.unwrap();
        assert_eq!(f.outcome_count(), 1);
        f.audit.verify().unwrap();
    }
}
#[tokio::test]
async fn ack_failure_after_sink_insertion_preserves_retryable_complete_payload() {
    let f = Fixture::new();
    let (id, _, _) = f.consumed().await;
    let raw = f.raw();
    raw.execute_batch("CREATE TRIGGER ack_test_failure BEFORE UPDATE OF delivered ON terminal_outbox BEGIN SELECT RAISE(ABORT,'ack fault'); END;").unwrap();
    assert!(
        f.gate
            .record_outcome(id.clone(), Outcome::Succeeded)
            .await
            .is_err()
    );
    assert_eq!(f.outcome_count(), 1);
    assert!(
        !f.approvals
            .terminal_delivery(&id)
            .unwrap()
            .unwrap()
            .acknowledged
    );
    raw.execute_batch("DROP TRIGGER ack_test_failure;").unwrap();
    f.fresh_gate()
        .record_outcome(id.clone(), Outcome::Succeeded)
        .await
        .unwrap();
    assert!(
        f.approvals
            .terminal_delivery(&id)
            .unwrap()
            .unwrap()
            .acknowledged
    );
    assert_eq!(f.outcome_count(), 1);
}
#[test]
fn runtime_loss_then_reopen_expires_consumed_authority_as_unknown_and_signals_cancel() {
    let f = Fixture::new();
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    let (id, expiry, permit) = runtime.block_on(f.consumed());
    f.gate.watch_lease(&f.cx, &permit).unwrap();
    drop(runtime);
    assert_eq!(
        f.gate.recover_profile_at(&f.profile, expiry + 1).unwrap(),
        1
    );
    assert!(f.cx.cancel().is_cancelled());
    let record = f.approvals.terminal_delivery(&id).unwrap().unwrap();
    assert_eq!(
        record.decision.disposition,
        TerminalDisposition::ExpiredCleanupUnknown
    );
    assert_eq!(
        record.decision.outcome,
        Outcome::Failed {
            code: "deadline-failure-unknown".into()
        }
    );
    assert!(!record.acknowledged);
    assert_eq!(
        f.gate.recover_profile_at(&f.profile, expiry + 1).unwrap(),
        0
    );
    let next = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    next.block_on(f.fresh_gate().flush_outbox(&f.profile))
        .unwrap();
    assert_eq!(f.outcome_count(), 1);
}
#[tokio::test]
async fn expiry_and_completion_race_has_one_immutable_winner_across_connections() {
    let f = Fixture::new();
    let (id, expiry, permit) = f.consumed().await;
    f.gate.watch_lease(&f.cx, &permit).unwrap();
    let a = Approvals::open(&f.root, &f.profile).unwrap();
    let b = Approvals::open(&f.root, &f.profile).unwrap();
    let barrier = Arc::new(std::sync::Barrier::new(2));
    let one = barrier.clone();
    let first = id.clone();
    let profile = f.profile.clone();
    let finish = std::thread::spawn(move || {
        one.wait();
        a.finish_terminal(&first, &Outcome::Succeeded)
    });
    let expire = std::thread::spawn(move || {
        barrier.wait();
        b.expire_terminal(&profile, expiry + 1)
    });
    let finished = finish.join().unwrap();
    let expired = expire.join().unwrap().unwrap();
    let decision = f
        .approvals
        .terminal_delivery(&id)
        .unwrap()
        .unwrap()
        .decision;
    if finished.is_ok() {
        assert!(expired.is_empty());
        assert_eq!(decision.outcome, Outcome::Succeeded);
    } else {
        assert_eq!(expired.len(), 1);
        assert_eq!(
            decision.disposition,
            TerminalDisposition::ExpiredCleanupUnknown
        );
    }
    f.gate.recover_profile_at(&f.profile, expiry + 1).unwrap();
    if decision.disposition == TerminalDisposition::ExpiredCleanupUnknown {
        assert!(f.cx.cancel().is_cancelled());
    }
    f.gate.flush_outbox(&f.profile).await.unwrap();
    assert_eq!(f.outcome_count(), 1);
    f.audit.verify().unwrap();
}
#[tokio::test]
async fn live_future_expiry_is_preserved_and_watermark_does_not_swallow_later_delivery() {
    let f = Fixture::new();
    let (first, expiry, _) = f.consumed().await;
    assert_eq!(
        f.gate.recover_profile_at(&f.profile, expiry - 1).unwrap(),
        0
    );
    f.approvals
        .finish_terminal(&first, &Outcome::Succeeded)
        .unwrap();
    let mark = f.gate.outbox_watermark(&f.profile).unwrap();
    let (second, _, _) = f.consumed().await;
    f.approvals
        .finish_terminal(&second, &Outcome::Cancelled)
        .unwrap();
    assert_eq!(
        f.gate.flush_outbox_through(&f.profile, mark).await.unwrap(),
        1
    );
    assert!(
        !f.approvals
            .terminal_delivery(&second)
            .unwrap()
            .unwrap()
            .acknowledged
    );
    assert_eq!(f.gate.flush_outbox(&f.profile).await.unwrap(), 1);
    assert_eq!(f.outcome_count(), 2);
}
#[tokio::test]
async fn sink_dedup_rejects_conflicting_payload_and_mixed_old_new_chain_survives_reopen() {
    let f = Fixture::new();
    f.audit
        .append(AuditEvent {
            profile: f.profile.clone(),
            principal: Principal::User,
            request: RequestId::parse("req_old").unwrap(),
            kind: "legacy-style".into(),
            payload: CanonValue::Null,
        })
        .unwrap();
    let (id, _, _) = f.consumed().await;
    f.gate
        .record_outcome(id.clone(), Outcome::Succeeded)
        .await
        .unwrap();
    let decision = f
        .approvals
        .terminal_delivery(&id)
        .unwrap()
        .unwrap()
        .decision;
    f.audit
        .append_idempotent(&decision.event_id, decision.event.clone())
        .unwrap();
    let mut forged = decision.event;
    forged.payload = CanonValue::Null;
    assert!(
        f.audit
            .append_idempotent(&decision.event_id, forged)
            .is_err()
    );
    assert_eq!(f.outcome_count(), 1);
    AuditLog::open(&f.root, &f.profile, AuditQuota::default())
        .unwrap()
        .verify()
        .unwrap();
}
#[test]
fn actual_legacy_audit_schema_upgrades_without_rewriting_existing_hashes() {
    let dir = tempfile::tempdir().unwrap();
    let root = DataRoot::open(dir.path().canonicalize().unwrap()).unwrap();
    let profile = ProfileId::parse("prof_legacy_audit").unwrap();
    root.write_profile_file_atomic(&profile, "marker", b"")
        .unwrap();
    let event = AuditEvent {
        profile: profile.clone(),
        principal: Principal::User,
        request: RequestId::parse("req_legacy_audit").unwrap(),
        kind: "legacy".into(),
        payload: CanonValue::Null,
    };
    let strict = |v: &serde_json::Value| {
        let value = tobkiri_canon::parse(
            &serde_json::to_vec(v).unwrap(),
            tobkiri_canon::Variant::StrictV1,
        )
        .unwrap();
        tobkiri_canon::encode(&value, tobkiri_canon::Variant::StrictV1).unwrap()
    };
    let prev = Digest::hash(&[]);
    let hash = Digest::hash(&strict(
        &serde_json::json!({"seq":1,"prev_digest":prev,"event":event}),
    ));
    let body = strict(&serde_json::to_value(&event).unwrap());
    let raw =
        rusqlite::Connection::open(root.path().join(format!("profiles/{profile}/audit.sqlite")))
            .unwrap();
    raw.execute_batch("CREATE TABLE audit(seq INTEGER PRIMARY KEY,prev_digest TEXT NOT NULL,digest TEXT NOT NULL,body BLOB NOT NULL,writer TEXT NOT NULL,minute INTEGER NOT NULL,size INTEGER NOT NULL);").unwrap();
    raw.execute(
        "INSERT INTO audit VALUES(1,?1,?2,?3,'user',0,?4)",
        rusqlite::params![prev.as_str(), hash.as_str(), body, body.len() as i64],
    )
    .unwrap();
    let audit = AuditLog::open(&root, &profile, AuditQuota::default()).unwrap();
    audit.verify().unwrap();
    assert_eq!(
        raw.query_row("SELECT digest FROM audit WHERE seq=1", [], |r| r
            .get::<_, String>(0))
            .unwrap(),
        hash.as_str()
    );
    assert!(
        raw.query_row("SELECT event_id FROM audit WHERE seq=1", [], |r| r
            .get::<_, Option<String>>(0))
            .unwrap()
            .is_none()
    );
    audit
        .append_idempotent(&Digest::hash(b"new identified row"), event)
        .unwrap();
    audit.verify().unwrap();
    assert_eq!(
        raw.query_row("SELECT digest FROM audit WHERE seq=1", [], |r| r
            .get::<_, String>(0))
            .unwrap(),
        hash.as_str()
    );
}
#[tokio::test]
async fn profile_data_opens_bounded_native_database_and_runs_real_kv_operations() {
    let f = Fixture::new();
    let mut policy = PermissionSet {
        audit_read: true,
        wasm: f.cx.limits().clone(),
        ..Default::default()
    };
    policy.storage.private = true;
    let (pack, hash) = f.cx.principal().pack().unwrap();
    f.approvals
        .set_permissions(&f.profile, pack, hash, &policy)
        .unwrap();
    let gate = Arc::new(GateEngine::new(
        f.approvals.clone(),
        f.audit.clone(),
        Arc::new(NoPrompt),
        MediatorChain::default(),
    ));
    gate.register_pack(
        &f.profile,
        pack,
        hash,
        PackPolicy {
            declaration: policy.clone(),
            ceiling: policy.clone(),
            pack_limits: policy,
        },
    )
    .unwrap();
    let stores = TieredStore::open(&f.root, &f.profile).unwrap();
    stores
        .register(pack, "data", StoreTier::Private, "local.terminal.data@1.0")
        .unwrap();
    let (req, args) = stores.open_request(pack, "data").unwrap();
    let opened = stores.open_store(
        &f.cx,
        pack,
        "data",
        gate.authorize(&f.cx, req, args).await.unwrap(),
    );
    let seed = tobkiri_canon::CanonValue::Obj(std::collections::BTreeMap::from([(
        "first".into(),
        tobkiri_canon::CanonValue::Str("seeded".into()),
    )]));
    stores.apply_seed(pack, "data", hash, &seed).unwrap();
    let opened = opened.unwrap();
    let get = StoreCall::KvGet {
        key: "first".into(),
    };
    let grant = gate
        .authorize(&f.cx, opened.request(&get).unwrap(), get.digest().unwrap())
        .await
        .unwrap();
    assert!(matches!(
        opened.execute(&f.cx, get, grant).unwrap(),
        StoreResult::Value(Some(value)) if value == br#""seeded""#
    ));
    let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget(AtomicU64::new(0)));
    let fixed_arguments = CanonValue::Obj(std::collections::BTreeMap::from([
        ("op".into(), CanonValue::Str("kv-get".into())),
        ("key".into(), CanonValue::Str("first".into())),
    ]));
    let owned_arguments = CoreValue::try_from_borrowed(&fixed_arguments, &budget).unwrap();
    let fixed_request = FixedDataRequest::from_value(owned_arguments, &budget).unwrap();
    let (fixed_effect, fixed_digest) = opened.fixed_request(&fixed_request).unwrap();
    let fixed_grant = gate
        .authorize(&f.cx, fixed_effect, fixed_digest)
        .await
        .unwrap();
    let fixed_output = opened
        .execute_fixed(&f.cx, fixed_request, fixed_grant, budget.clone())
        .unwrap();
    let CoreRef::Arr(bytes) = fixed_output.inspect() else {
        panic!("owned fixed get must return a byte list");
    };
    let bytes = bytes
        .iter()
        .map(|byte| match byte.inspect() {
            CoreRef::Int(value) => u8::try_from(value).unwrap(),
            _ => panic!("fixed get returned a non-byte"),
        })
        .collect::<Vec<_>>();
    assert_eq!(bytes, br#""seeded""#);
    let gate_api: Arc<dyn Gate> = gate.clone();
    let create = typed_statement(
        "CREATE TABLE native_rows(value TEXT NOT NULL)",
        vec![],
        &budget,
    );
    opened
        .sql_exec_typed(
            &f.cx,
            create,
            SqlResultPolicy::ForbidRows,
            gate_api.clone(),
            budget.clone(),
        )
        .await
        .unwrap();
    let transaction = opened
        .sql_begin(&f.cx, gate_api.clone(), budget.clone())
        .await
        .unwrap();
    for value in ["typed", "transaction"] {
        let insert = typed_statement(
            "INSERT INTO native_rows(value) VALUES(?1)",
            vec![text_cell(value)],
            &budget,
        );
        transaction
            .exec_typed(
                &f.cx,
                insert,
                SqlResultPolicy::ForbidRows,
                gate_api.clone(),
                budget.clone(),
            )
            .await
            .unwrap();
    }
    transaction.commit(&f.cx, gate_api.clone()).await.unwrap();
    let query = typed_statement(
        "SELECT value FROM native_rows ORDER BY value",
        vec![],
        &budget,
    );
    let rows = opened
        .sql_query_typed(&f.cx, query, gate_api, budget)
        .await
        .unwrap();
    let rows = rows.value().get_key("rows").unwrap();
    assert_eq!(rows.array().unwrap().len(), 2);
    assert_eq!(
        rows.get_index(0)
            .unwrap()
            .get_index(0)
            .unwrap()
            .get_key("value")
            .unwrap()
            .as_str(),
        Some("transaction")
    );
    let put = StoreCall::KvPut {
        key: "written".into(),
        value: b"native".to_vec(),
    };
    let grant = gate
        .authorize(&f.cx, opened.request(&put).unwrap(), put.digest().unwrap())
        .await
        .unwrap();
    opened.execute(&f.cx, put, grant).unwrap();
    // Registration and the real bounded database now coexist in the same
    // profile-scoped native path.
    assert_eq!(stores.describe(pack, "data").unwrap().name, "data");
    let directory = f
        .root
        .path()
        .join(format!("profiles/{}/data/{}", f.profile, pack));
    assert!(directory.join("private.sqlite").is_file());
}

#[tokio::test]
async fn fresh_ids_retain_namespace_and_sequence_across_reopen_with_distinct_os_nonces() {
    let f = Fixture::new();
    let (first, _, _) = f.consumed().await;
    f.gate
        .record_outcome(first.clone(), Outcome::Succeeded)
        .await
        .unwrap();
    let a = f
        .approvals
        .terminal_delivery(&first)
        .unwrap()
        .unwrap()
        .decision
        .reservation
        .authority
        .unwrap();
    let reopened = Arc::new(Approvals::open(&f.root, &f.profile).unwrap());
    let gate = GateEngine::new(
        reopened,
        f.audit.clone(),
        Arc::new(NoPrompt),
        MediatorChain::default(),
    );
    let (pack, hash) = f.cx.principal().pack().unwrap();
    let p = PermissionSet {
        audit_read: true,
        wasm: f.cx.limits().clone(),
        ..Default::default()
    };
    gate.register_pack(
        &f.profile,
        pack,
        hash,
        PackPolicy {
            declaration: p.clone(),
            ceiling: p.clone(),
            pack_limits: p,
        },
    )
    .unwrap();
    let args = Digest::hash(b"same request");
    let grant = gate
        .authorize(&f.cx, EffectRequest::AuditRead, args.clone())
        .await
        .unwrap();
    let second = grant.id().clone();
    grant
        .consume(&f.cx, &EffectRequest::AuditRead, &args)
        .unwrap();
    gate.record_outcome(second.clone(), Outcome::Succeeded)
        .await
        .unwrap();
    let b = f
        .approvals
        .terminal_delivery(&second)
        .unwrap()
        .unwrap()
        .decision
        .reservation
        .authority
        .unwrap();
    assert_ne!(first, second);
    assert_eq!(a.namespace, b.namespace);
    assert_eq!(a.generation, b.generation);
    assert_eq!(a.sequence + 1, b.sequence);
    assert_ne!(a.nonce, b.nonce);
}
#[tokio::test]
async fn consumed_deadline_is_durable_reduce_only_and_settlement_honors_the_existing_winner() {
    let f = Fixture::new();
    let (id, original, mut permit) = f.consumed().await;
    let cx = f.cx.clone().narrow_limits(&ResourceLimits {
        wall_ms: 1000,
        ..f.cx.limits().clone()
    });
    f.gate.narrow_lease(&mut permit, &cx).await.unwrap();
    assert!(permit.deadline() <= cx.deadline());
    let body: Vec<u8> = f
        .raw()
        .query_row(
            "SELECT body FROM reservations WHERE id=?1",
            [id.as_str()],
            |r| r.get(0),
        )
        .unwrap();
    let r: Reservation = serde_json::from_slice(&body).unwrap();
    assert_eq!(r.original_expires_unix_ms, Some(original));
    assert!(r.expires_unix_ms < original);
    let unchanged = f.approvals.narrow_consumed(&r, original + 100000).unwrap();
    assert_eq!(unchanged.expires_unix_ms, r.expires_unix_ms);
    f.gate
        .record_outcome(id.clone(), Outcome::Succeeded)
        .await
        .unwrap();
    let winner = f.gate.settle_deadline(&permit).await.unwrap();
    assert_eq!(winner.decision.outcome, Outcome::Succeeded);
    assert!(winner.acknowledged);
    assert_eq!(f.outcome_count(), 1);
}
#[tokio::test]
async fn late_success_terminalizes_unknown_deadline_and_never_reports_success() {
    let f = Fixture::new();
    let (id, _, _) = f.consumed().await;
    let raw = f.raw();
    let body: Vec<u8> = raw
        .query_row(
            "SELECT body FROM reservations WHERE id=?1",
            [id.as_str()],
            |row| row.get(0),
        )
        .unwrap();
    let r: Reservation = serde_json::from_slice(&body).unwrap();
    let due = f.approvals.narrow_consumed(&r, 0).unwrap();
    assert!(matches!(
        f.gate.record_outcome(id.clone(), Outcome::Succeeded).await,
        Err(GateError::Deadline)
    ));
    let saved = f.approvals.terminal_delivery(&id).unwrap().unwrap();
    assert_eq!(
        saved.decision.disposition,
        TerminalDisposition::ExpiredCleanupUnknown
    );
    assert_eq!(
        saved.decision.outcome,
        Outcome::Failed {
            code: "deadline-failure-unknown".into()
        }
    );
    assert_eq!(
        saved.decision.reservation.original_expires_unix_ms,
        due.original_expires_unix_ms
    );
    let winner = f.approvals.settle_deadline(&due).unwrap();
    assert!(winner.acknowledged);
}
#[tokio::test]
async fn permission_effect_and_terminal_outbox_roll_back_together_before_commit() {
    let f = Fixture::new();
    let (pack, old) = f.cx.principal().pack().unwrap();
    let new = PackHash::new(Digest::hash(b"new permission revision"));
    let p = PermissionSet {
        audit_read: true,
        wasm: f.cx.limits().clone(),
        ..Default::default()
    };
    f.gate
        .register_pack(
            &f.profile,
            pack,
            &new,
            PackPolicy {
                declaration: p.clone(),
                ceiling: p.clone(),
                pack_limits: p,
            },
        )
        .unwrap();
    f.raw().execute_batch("CREATE TRIGGER policy_commit_fault BEFORE INSERT ON terminal_outbox BEGIN SELECT RAISE(ABORT,'terminal failure'); END;").unwrap();
    let user = f.cx.child(Principal::User);
    assert!(
        f.gate
            .approve_pack_permissions(&user, pack, Some(old), &new)
            .await
            .is_err()
    );
    assert!(
        f.approvals
            .permissions(&f.profile, pack, &new)
            .unwrap()
            .is_none()
    );
    let effects: i64 = f
        .raw()
        .query_row("SELECT COUNT(*) FROM permission_effects", [], |row| {
            row.get(0)
        })
        .unwrap();
    assert_eq!(effects, 0);
    f.raw()
        .execute_batch("DROP TRIGGER policy_commit_fault")
        .unwrap();
    f.gate
        .approve_pack_permissions(&user, pack, Some(old), &new)
        .await
        .unwrap();
    assert!(
        f.approvals
            .permissions(&f.profile, pack, &new)
            .unwrap()
            .is_some()
    );
    assert_eq!(f.outcome_count(), 1);
}
#[tokio::test]
async fn corrupt_terminal_is_quarantined_without_claiming_ready_or_blocking_independent_delivery() {
    let f = Fixture::new();
    let (first, _, _) = f.consumed().await;
    let (second, _, _) = f.consumed().await;
    f.approvals
        .finish_terminal(&first, &Outcome::Succeeded)
        .unwrap();
    f.approvals
        .finish_terminal(&second, &Outcome::Succeeded)
        .unwrap();
    let raw = f.raw();
    raw.execute_batch("DROP TRIGGER terminal_no_rewrite")
        .unwrap();
    raw.execute(
        "UPDATE terminal_outbox SET body=x'00' WHERE grant_id=?1",
        [first.as_str()],
    )
    .unwrap();
    assert!(matches!(
        f.gate.flush_outbox(&f.profile).await,
        Err(GateError::Integrity(_))
    ));
    assert_eq!(f.outcome_count(), 1);
    assert!(
        f.approvals
            .terminal_delivery(&second)
            .unwrap()
            .unwrap()
            .acknowledged
    );
    assert!(matches!(
        f.approvals.admission_status(&f.profile).unwrap(),
        AdmissionStatus::IntegrityFault { .. }
    ));
    assert!(
        f.gate
            .authorize(&f.cx, EffectRequest::AuditRead, Digest::hash(b"denied"))
            .await
            .is_err()
    );
    let evidence: Vec<u8> = raw
        .query_row(
            "SELECT body FROM terminal_outbox WHERE grant_id=?1",
            [first.as_str()],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(evidence, vec![0]);
}
#[tokio::test]
async fn corrupt_expiry_row_retains_authority_evidence_but_other_due_rows_still_terminalize() {
    let f = Fixture::new();
    let (first, expiry, _) = f.consumed().await;
    let (second, _, _) = f.consumed().await;
    let raw = f.raw();
    raw.execute(
        "UPDATE reservations SET body=x'00' WHERE id=?1",
        [first.as_str()],
    )
    .unwrap();
    let decisions = f
        .approvals
        .expire_terminal(&f.profile, expiry + 60000)
        .unwrap();
    assert_eq!(decisions.len(), 1);
    assert_eq!(decisions[0].reservation.id, second);
    assert_eq!(
        f.approvals.reservation_state(&first).unwrap().as_deref(),
        Some("consumed")
    );
    assert!(matches!(
        f.approvals.admission_status(&f.profile).unwrap(),
        AdmissionStatus::IntegrityFault { .. }
    ));
    assert!(matches!(
        f.gate.flush_outbox(&f.profile).await,
        Err(GateError::Integrity(_))
    ));
    assert_eq!(f.outcome_count(), 1);
}
