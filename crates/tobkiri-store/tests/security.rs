use async_trait::async_trait;
use ed25519_dalek::{Signer, SigningKey};
use std::{
    sync::{
        Arc, Mutex,
        atomic::{AtomicUsize, Ordering},
    },
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tobkiri_canon::{
    AllocationBudget, AllocationError, AllocationRequest, CanonValue, CoreValue, Digest, Variant,
    ids::*,
};
use tobkiri_gate::*;
use tobkiri_store::*;

#[path = "support/storage_commit_fence.rs"]
mod storage_commit_fence;

struct Prompt {
    calls: AtomicUsize,
    mode: Mutex<PromptMode>,
    risks: Mutex<Vec<Risk>>,
    snapshots: Mutex<Vec<ApprovalPrompt>>,
}
#[derive(Clone)]
enum PromptMode {
    Approve,
    Deny,
    Signed {
        key: [u8; 32],
        nonce: String,
        forge: bool,
    },
}
impl Prompt {
    fn new() -> Self {
        Self {
            calls: AtomicUsize::new(0),
            mode: Mutex::new(PromptMode::Approve),
            risks: Mutex::new(vec![]),
            snapshots: Mutex::new(vec![]),
        }
    }
}
#[async_trait]
impl TrustedPrompt for Prompt {
    async fn prompt_approval(&self, p: ApprovalPrompt) -> ApprovalAnswer {
        self.calls.fetch_add(1, Ordering::SeqCst);
        self.risks.lock().unwrap().push(p.risk);
        self.snapshots.lock().unwrap().push(p.clone());
        match self.mode.lock().unwrap().clone() {
            PromptMode::Approve => ApprovalAnswer::Approve,
            PromptMode::Deny => ApprovalAnswer::Deny,
            PromptMode::Signed { key, nonce, forge } => {
                let mut att = DeviceAttestation {
                    device: "phone".into(),
                    profile: p.profile,
                    principal: p.principal,
                    reservation: p.reservation,
                    request: p.request,
                    effect: p.effect.digest().unwrap(),
                    args: p.args_digest,
                    epoch: p.security_epoch,
                    expires_unix_ms: (SystemTime::now()
                        .duration_since(UNIX_EPOCH)
                        .unwrap()
                        .as_millis()
                        + 60_000) as u64,
                    nonce,
                    signature: vec![],
                };
                let v = tobkiri_canon::parse(&serde_json::to_vec(&att).unwrap(), Variant::StrictV1)
                    .unwrap();
                let bytes = tobkiri_canon::encode(&v, Variant::StrictV1).unwrap();
                att.signature = SigningKey::from_bytes(&key)
                    .sign(&bytes)
                    .to_bytes()
                    .to_vec();
                if forge {
                    att.signature[0] ^= 1;
                }
                ApprovalAnswer::Attestation(att)
            }
        }
    }
}
struct Fixture {
    _temp: tempfile::TempDir,
    root: DataRoot,
    profile: ProfileId,
    approvals: Arc<Approvals>,
    audit: Arc<AuditLog>,
    prompt: Arc<Prompt>,
    gate: Arc<GateEngine>,
}
impl Fixture {
    fn new() -> Self {
        Self::with_control(None)
    }
    fn with_control(control: Option<CoreControl>) -> Self {
        let temp = tempfile::tempdir().unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(temp.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
        }
        let root = DataRoot::open(temp.path().canonicalize().unwrap()).unwrap();
        let profile = ProfileId::parse("prof_test").unwrap();
        let approvals = Arc::new(Approvals::open(&root, &profile).unwrap());
        let audit = Arc::new(AuditLog::open(&root, &profile, AuditQuota::default()).unwrap());
        let prompt = Arc::new(Prompt::new());
        let gate = Arc::new(match control {
            Some(control) => GateEngine::new_with_control(
                approvals.clone(),
                audit.clone(),
                prompt.clone(),
                MediatorChain::default(),
                control,
            ),
            None => GateEngine::new(
                approvals.clone(),
                audit.clone(),
                prompt.clone(),
                MediatorChain::default(),
            ),
        });
        Self {
            _temp: temp,
            root,
            profile,
            approvals,
            audit,
            prompt,
            gate,
        }
    }
    fn cx(&self, principal: Principal) -> CallCx {
        let control = self.gate.control();
        CallCx::try_root_with_control(
            self.profile.clone(),
            principal,
            RequestId::parse("req_test").unwrap(),
            CancellationToken::new_with_control(control).unwrap(),
            Instant::now() + Duration::from_secs(30),
            limits(),
            control.clone(),
        )
        .unwrap()
        .with_security_epoch(self.gate.epoch(&self.profile).unwrap())
    }
    async fn pack(&self, id: &str, permissions: PermissionSet) -> CallCx {
        let pack = PackId::parse(id).unwrap();
        let hash = PackHash::new(Digest::hash(id.as_bytes()));
        self.gate
            .register_pack(
                &self.profile,
                &pack,
                &hash,
                PackPolicy {
                    declaration: permissions.clone(),
                    ceiling: permissions.clone(),
                    pack_limits: permissions,
                },
            )
            .unwrap();
        self.gate
            .approve_pack_permissions(&self.cx(Principal::User), &pack, None, &hash)
            .await
            .unwrap();
        self.cx(Principal::Pack { pack, hash })
    }
    async fn grant(&self, cx: &CallCx, req: EffectRequest, args: Digest) -> Grant {
        self.gate.authorize(cx, req, args).await.unwrap()
    }
}
fn limits() -> ResourceLimits {
    ResourceLimits {
        fuel_per_call: 1_000_000,
        memory_mib: 64,
        wall_ms: 30_000,
        table_elements: 10_000,
        output_bytes: 4 * 1024 * 1024,
    }
}
fn permissions() -> PermissionSet {
    PermissionSet {
        storage: StoragePermissions {
            private: true,
            public_read: true,
            public_write: true,
        },
        audit_append: true,
        audit_read: true,
        wasm: limits(),
        loops: LoopLimits {
            max_iterations: 16,
            max_wall_ms: 30_000,
            max_cost_units: 1000,
        },
        ..Default::default()
    }
}
fn owner(cx: &CallCx) -> &PackId {
    cx.principal().pack().unwrap().0
}
async fn open(
    f: &Fixture,
    stores: &TieredStore,
    cx: &CallCx,
    owner: &PackId,
    name: &str,
) -> StoreHandle {
    let (req, args) = stores.open_request(owner, name).unwrap();
    let g = f.grant(cx, req, args).await;
    stores.open_store(cx, owner, name, g).unwrap()
}
async fn execute(
    f: &Fixture,
    handle: &StoreHandle,
    cx: &CallCx,
    call: StoreCall,
) -> Result<StoreResult, StoreError> {
    let req = handle.request(&call)?;
    let g = f.gate.authorize(cx, req, call.digest()?).await?;
    handle.execute(cx, call, g)
}

/// A bounded trusted fixture producer. The call still uses the real typed
/// native backend, readonly classification, Gate grants and result custody.
struct SqlTestValues(AtomicUsize);
impl AllocationBudget for SqlTestValues {
    fn reserve(&self, request: AllocationRequest) -> Result<(), AllocationError> {
        let bytes = usize::try_from(request.bytes).map_err(|_| AllocationError::Overflow)?;
        self.0
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |used| {
                used.checked_add(bytes)
                    .filter(|next| *next <= 8 * 1024 * 1024)
            })
            .map(|_| ())
            .map_err(|_| AllocationError::Limit)
    }
    fn release(&self, bytes: u64) {
        self.0.fetch_sub(bytes as usize, Ordering::AcqRel);
    }
}
async fn native_sql(
    f: &Fixture,
    handle: &StoreHandle,
    cx: &CallCx,
    sql: &str,
    params: &[SqlCell],
    query: bool,
) -> Result<OwnedSqlResult, StoreError> {
    let budget: Arc<dyn AllocationBudget> = Arc::new(SqlTestValues(AtomicUsize::new(0)));
    let value = CanonValue::Obj(std::collections::BTreeMap::from([
        ("statement".into(), CanonValue::Str(sql.into())),
        (
            "params".into(),
            CanonValue::Arr(params.iter().map(SqlCell::to_value).collect()),
        ),
        ("max-rows".into(), CanonValue::Int(10)),
    ]));
    let statement = OwnedSqlStatement::from_value(
        CoreValue::try_from_borrowed(&value, &budget).map_err(|_| StoreError::Quota)?,
    )?;
    if query {
        handle
            .sql_query_typed(cx, statement, f.gate.clone(), budget)
            .await
    } else {
        handle
            .sql_exec_typed(
                cx,
                statement,
                SqlResultPolicy::ForbidRows,
                f.gate.clone(),
                budget,
            )
            .await
    }
}

#[tokio::test]
async fn public_write_approval_is_once_per_writer_and_store_and_reads_do_not_prompt() {
    let f = Fixture::new();
    let owner_cx = f.pack("local.owner", permissions()).await;
    let writer = f.pack("local.writer", permissions()).await;
    let writer2 = f.pack("local.writer-two", permissions()).await;
    let stores = TieredStore::open(&f.root, &f.profile).unwrap();
    stores
        .register(
            owner(&owner_cx),
            "notes",
            StoreTier::Public,
            "local.owner.notes@1.0",
        )
        .unwrap();
    let h = open(&f, &stores, &writer, owner(&owner_cx), "notes").await;
    let baseline = f.prompt.calls.load(Ordering::SeqCst);
    assert!(matches!(
        execute(&f, &h, &writer, StoreCall::KvGet { key: "one".into() })
            .await
            .unwrap(),
        StoreResult::Value(None)
    ));
    assert_eq!(f.prompt.calls.load(Ordering::SeqCst), baseline);
    execute(
        &f,
        &h,
        &writer,
        StoreCall::KvPut {
            key: "one".into(),
            value: b"saved".to_vec(),
        },
    )
    .await
    .unwrap();
    assert_eq!(f.prompt.calls.load(Ordering::SeqCst), baseline + 1);
    execute(
        &f,
        &h,
        &writer,
        StoreCall::KvPut {
            key: "two".into(),
            value: b"saved again".to_vec(),
        },
    )
    .await
    .unwrap();
    assert_eq!(f.prompt.calls.load(Ordering::SeqCst), baseline + 1);
    let h2 = open(&f, &stores, &writer2, owner(&owner_cx), "notes").await;
    execute(
        &f,
        &h2,
        &writer2,
        StoreCall::KvPut {
            key: "three".into(),
            value: vec![3],
        },
    )
    .await
    .unwrap();
    assert_eq!(f.prompt.calls.load(Ordering::SeqCst), baseline + 2);
    f.audit.verify().unwrap();
}

#[tokio::test]
async fn stale_handle_cannot_write_after_owner_marks_store_read_only() {
    let f = Fixture::new();
    let owner_cx = f.pack("local.owner", permissions()).await;
    let writer = f.pack("local.writer", permissions()).await;
    let stores = TieredStore::open(&f.root, &f.profile).unwrap();
    stores
        .register(
            owner(&owner_cx),
            "notes",
            StoreTier::Public,
            "local.owner.notes@1.0",
        )
        .unwrap();
    let h = open(&f, &stores, &writer, owner(&owner_cx), "notes").await;
    let call = StoreCall::KvPut {
        key: "bad".into(),
        value: vec![1],
    };
    let g = f
        .grant(&writer, h.request(&call).unwrap(), call.digest().unwrap())
        .await;
    let meta = stores.describe(owner(&owner_cx), "notes").unwrap();
    let req = EffectRequest::Storage {
        profile: f.profile.clone(),
        owner: owner(&owner_cx).clone(),
        store: "notes".into(),
        tier: meta.tier,
        operation: StoreOperation::SetReadOnly,
    };
    let data = serde_json::to_vec(&(owner(&owner_cx), "notes", "read-only")).unwrap();
    let args = Digest::hash(
        &tobkiri_canon::encode(
            &tobkiri_canon::parse(&data, Variant::StrictV1).unwrap(),
            Variant::StrictV1,
        )
        .unwrap(),
    );
    let rg = f.grant(&owner_cx, req, args).await;
    stores
        .set_read_only(&owner_cx, owner(&owner_cx), "notes", rg)
        .unwrap();
    assert!(h.execute(&writer, call, g).is_err());
    assert!(
        execute(
            &f,
            &h,
            &writer,
            StoreCall::KvPut {
                key: "bad".into(),
                value: vec![1]
            }
        )
        .await
        .is_err()
    );
}

#[tokio::test]
async fn exact_grant_binding_and_atomic_consumption_reject_replay() {
    let f = Fixture::new();
    let cx = f.pack("local.owner", permissions()).await;
    let req = EffectRequest::AuditRead;
    let args = AuditLog::read_digest(0, 10).unwrap();
    let g = f.grant(&cx, req.clone(), args.clone()).await;
    // Race the exact fresh record persisted by Gate, including its sealed
    // authority identity; reconstructing a legacy record is not equivalent.
    let raw = rusqlite::Connection::open(
        f.root
            .path()
            .join(format!("profiles/{}/approvals.sqlite", f.profile)),
    )
    .unwrap();
    let body: Vec<u8> = raw
        .query_row(
            "SELECT body FROM reservations WHERE id=?1",
            [g.id().as_str()],
            |row| row.get(0),
        )
        .unwrap();
    let r: Reservation = serde_json::from_slice(&body).unwrap();
    assert!(r.authority.is_some());
    let a = f.approvals.clone();
    let b = f.approvals.clone();
    let ra = r.clone();
    let rb = r.clone();
    let ta = std::thread::spawn(move || a.consume(&ra));
    let tb = std::thread::spawn(move || b.consume(&rb));
    let results = [ta.join().unwrap(), tb.join().unwrap()];
    assert_eq!(results.iter().filter(|r| r.is_ok()).count(), 1);
    assert!(g.consume(&cx, &req, &args).is_err());
    let g = f.grant(&cx, req.clone(), args.clone()).await;
    assert!(g.consume(&cx, &req, &Digest::hash(b"changed")).is_err());
    let g = f.grant(&cx, req.clone(), args.clone()).await;
    let other = cx.child(cx.principal().clone());
    assert!(g.consume(&other, &req, &args).is_err());
}

#[tokio::test]
async fn security_epoch_revokes_outstanding_grants_across_database_connections() {
    let f = Fixture::new();
    let cx = f.pack("local.owner", permissions()).await;
    let args = AuditLog::read_digest(0, 10).unwrap();
    let g = f.grant(&cx, EffectRequest::AuditRead, args.clone()).await;
    let another = Approvals::open(&f.root, &f.profile).unwrap();
    another.advance_security_epoch(&f.profile).unwrap();
    assert!(matches!(
        g.consume(&cx, &EffectRequest::AuditRead, &args),
        Err(GateError::StaleEpoch)
    ));
    assert!(matches!(
        f.gate.authorize(&cx, EffectRequest::AuditRead, args).await,
        Err(GateError::StaleEpoch)
    ));
}

fn cleanup_snapshot_connection(f: &Fixture) -> rusqlite::Connection {
    rusqlite::Connection::open(
        f.root
            .path()
            .join(format!("profiles/{}/approvals.sqlite", f.profile)),
    )
    .unwrap()
}

fn cleanup_snapshot_fixture() -> Fixture {
    // Saturation and live-custody observations must not share the process-wide
    // compatibility account with other concurrently executing security tests.
    Fixture::with_control(Some(CoreControl::default()))
}

fn cleanup_snapshot_reservation(raw: &rusqlite::Connection, id: &GrantId) -> Reservation {
    let body: Vec<u8> = raw
        .query_row(
            "SELECT body FROM reservations WHERE id=?1",
            [id.as_str()],
            |row| row.get(0),
        )
        .unwrap();
    serde_json::from_slice(&body).unwrap()
}

fn cleanup_snapshot_data_version(raw: &rusqlite::Connection) -> i64 {
    raw.pragma_query_value(None, "data_version", |row| row.get(0))
        .unwrap()
}

#[tokio::test]
async fn cleanup_snapshot_reads_cancelled_consumed_permit_once_without_mutation() {
    let f = cleanup_snapshot_fixture();
    let cx = f.pack("local.owner", permissions()).await;
    let args = AuditLog::read_digest(0, 10).unwrap();
    let permit = f
        .grant(&cx, EffectRequest::AuditRead, args.clone())
        .await
        .consume(&cx, &EffectRequest::AuditRead, &args)
        .unwrap();
    let raw = cleanup_snapshot_connection(&f);
    let before = cleanup_snapshot_data_version(&raw);
    let control = f.gate.control().clone();
    let control_before = control.usage();
    cx.cancel().cancel();
    let mut calls = 0;
    f.gate
        .with_cleanup_snapshot(&permit, &mut |snapshot| {
            calls += 1;
            assert_eq!(snapshot.authority.reservation.id, *permit.id());
            assert_eq!(snapshot.authority.current_epoch, cx.security_epoch());
            assert_eq!(snapshot.original_deadline, permit.deadline());
            assert!(snapshot.observed_at < snapshot.original_deadline);
            assert_eq!(snapshot.limits.fuel_per_call, permit.limits().fuel_per_call);
            assert!(snapshot.authority.terminal.is_none());
            let live = control.usage();
            assert_eq!(
                live.count(ControlKind::Frame),
                control_before.count(ControlKind::Frame) + 1
            );
            assert!(live.bytes > control_before.bytes);
            Ok(())
        })
        .unwrap();
    assert_eq!(calls, 1);
    // These are live custody counters. This check makes no refund claim about
    // monotonic digest/admission work already charged to an execution budget.
    let control_after = control.usage();
    assert_eq!(
        control_after.count(ControlKind::Frame),
        control_before.count(ControlKind::Frame)
    );
    assert_eq!(control_after.bytes, control_before.bytes);
    assert_eq!(cleanup_snapshot_data_version(&raw), before);
    assert_eq!(
        f.approvals
            .reservation_state(permit.id())
            .unwrap()
            .as_deref(),
        Some("consumed")
    );
    assert!(
        f.approvals
            .terminal_delivery(permit.id())
            .unwrap()
            .is_none()
    );
}

#[tokio::test]
async fn cleanup_snapshot_observes_cross_connection_epoch_and_due_narrowed_lease() {
    let f = cleanup_snapshot_fixture();
    let cx = f.pack("local.owner", permissions()).await;
    let args = AuditLog::read_digest(0, 10).unwrap();
    let permit = f
        .grant(&cx, EffectRequest::AuditRead, args.clone())
        .await
        .consume(&cx, &EffectRequest::AuditRead, &args)
        .unwrap();
    let raw = cleanup_snapshot_connection(&f);
    let original = cleanup_snapshot_reservation(&raw, permit.id());
    let another = Approvals::open(&f.root, &f.profile).unwrap();
    another.narrow_consumed(&original, 0).unwrap();
    let current_epoch = another.advance_security_epoch(&f.profile).unwrap();
    let before = cleanup_snapshot_data_version(&raw);
    let mut calls = 0;
    f.gate
        .with_cleanup_snapshot(&permit, &mut |snapshot| {
            calls += 1;
            assert_eq!(snapshot.authority.current_epoch, current_epoch);
            assert_ne!(snapshot.authority.current_epoch, cx.security_epoch());
            assert_eq!(snapshot.authority.reservation.expires_unix_ms, 0);
            assert!(
                snapshot.authority.reservation.expires_unix_ms <= snapshot.authority.now_unix_ms
            );
            assert_eq!(snapshot.original_deadline, permit.deadline());
            assert!(snapshot.authority.terminal.is_none());
            Ok(())
        })
        .unwrap();
    assert_eq!(calls, 1);
    assert_eq!(cleanup_snapshot_data_version(&raw), before);
    assert!(another.terminal_delivery(permit.id()).unwrap().is_none());
}

#[tokio::test]
async fn cleanup_snapshot_rejects_stale_binding_without_callback_or_writes() {
    let f = cleanup_snapshot_fixture();
    let cx = f.pack("local.owner", permissions()).await;
    let args = AuditLog::read_digest(0, 10).unwrap();
    let permit = f
        .grant(&cx, EffectRequest::AuditRead, args.clone())
        .await
        .consume(&cx, &EffectRequest::AuditRead, &args)
        .unwrap();
    let raw = cleanup_snapshot_connection(&f);
    let original = cleanup_snapshot_reservation(&raw, permit.id());
    let pending = f
        .grant(
            &cx,
            EffectRequest::AuditRead,
            Digest::hash(b"pending cleanup"),
        )
        .await;
    let pending_record = cleanup_snapshot_reservation(&raw, pending.id());
    let before = cleanup_snapshot_data_version(&raw);
    let mut altered = Vec::new();
    let mut changed = original.clone();
    changed.args = Digest::hash(b"other arguments");
    altered.push(changed);
    let mut changed = original.clone();
    changed.request = RequestId::parse("req_other").unwrap();
    altered.push(changed);
    let mut changed = original.clone();
    changed.epoch += 1;
    altered.push(changed);
    let mut changed = original.clone();
    changed.expires_unix_ms -= 1;
    altered.push(changed);
    let mut changed = original.clone();
    changed.authority.as_mut().unwrap().nonce = Digest::hash(b"other authority");
    altered.push(changed);
    for expected in altered {
        let mut calls = 0;
        assert!(
            f.approvals
                .with_cleanup_snapshot(&expected, &mut |_| {
                    calls += 1;
                    Ok(())
                })
                .is_err()
        );
        assert_eq!(calls, 0);
    }
    // Supplying a consumed-looking copy cannot turn an actually dispatched
    // row into consumed authority. The real pending Grant stays alive here.
    let mut pending_as_consumed = pending_record;
    pending_as_consumed.original_expires_unix_ms = Some(pending_as_consumed.expires_unix_ms);
    let mut calls = 0;
    assert!(matches!(
        f.approvals
            .with_cleanup_snapshot(&pending_as_consumed, &mut |_| {
                calls += 1;
                Ok(())
            }),
        Err(GateError::Consumed)
    ));
    assert_eq!(calls, 0);
    assert_eq!(cleanup_snapshot_data_version(&raw), before);
    assert_eq!(
        f.approvals.admission_status(&f.profile).unwrap(),
        AdmissionStatus::Allowed
    );
}

#[tokio::test]
async fn cleanup_snapshot_reads_existing_terminal_and_keeps_failed_callback_selection() {
    let f = cleanup_snapshot_fixture();
    let cx = f.pack("local.owner", permissions()).await;
    let args = AuditLog::read_digest(0, 10).unwrap();
    let permit = f
        .grant(&cx, EffectRequest::AuditRead, args.clone())
        .await
        .consume(&cx, &EffectRequest::AuditRead, &args)
        .unwrap();
    let terminal = f
        .approvals
        .finish_terminal(permit.id(), &Outcome::Cancelled)
        .unwrap();
    let raw = cleanup_snapshot_connection(&f);
    let before = cleanup_snapshot_data_version(&raw);
    let mut calls = 0;
    let mut native_selected = false;
    let result = f.gate.with_cleanup_snapshot(&permit, &mut |snapshot| {
        calls += 1;
        let observed = snapshot.authority.terminal.expect("existing terminal");
        assert_eq!(observed.decision.event_id, terminal.decision.event_id);
        assert_eq!(observed.decision.outcome, Outcome::Cancelled);
        assert!(!observed.acknowledged);
        native_selected = true;
        Err(GateError::Unavailable)
    });
    assert!(matches!(result, Err(GateError::Unavailable)));
    assert_eq!(calls, 1);
    assert!(native_selected);
    assert_eq!(cleanup_snapshot_data_version(&raw), before);
    let retained = f.approvals.terminal_delivery(permit.id()).unwrap().unwrap();
    assert_eq!(retained.decision.event_id, terminal.decision.event_id);
    assert_eq!(retained.decision.outcome, Outcome::Cancelled);
    assert!(!retained.acknowledged);
}

#[tokio::test]
async fn cleanup_snapshot_immediate_writer_contention_never_invokes_callback() {
    let f = cleanup_snapshot_fixture();
    let cx = f.pack("local.owner", permissions()).await;
    let args = AuditLog::read_digest(0, 10).unwrap();
    let permit = f
        .grant(&cx, EffectRequest::AuditRead, args.clone())
        .await
        .consume(&cx, &EffectRequest::AuditRead, &args)
        .unwrap();
    let mut raw = cleanup_snapshot_connection(&f);
    let writer = raw
        .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
        .unwrap();
    let start = Instant::now();
    let mut calls = 0;
    assert!(matches!(
        f.gate.with_cleanup_snapshot(&permit, &mut |_| {
            calls += 1;
            Ok(())
        }),
        Err(GateError::Persistence(_))
    ));
    assert_eq!(calls, 0);
    assert!(start.elapsed() < Duration::from_secs(1));
    writer.rollback().unwrap();
    f.gate
        .with_cleanup_snapshot(&permit, &mut |_| {
            calls += 1;
            Ok(())
        })
        .unwrap();
    assert_eq!(calls, 1);
}

#[tokio::test]
async fn cleanup_snapshot_native_funding_failure_never_invokes_callback() {
    let f = cleanup_snapshot_fixture();
    let cx = f.pack("local.owner", permissions()).await;
    let args = AuditLog::read_digest(0, 10).unwrap();
    let permit = f
        .grant(&cx, EffectRequest::AuditRead, args.clone())
        .await
        .consume(&cx, &EffectRequest::AuditRead, &args)
        .unwrap();
    let raw = cleanup_snapshot_connection(&f);
    let before = cleanup_snapshot_data_version(&raw);
    let control = f.gate.control().clone();
    let baseline = control.usage();
    let frame_limit = CoreControlLimits::default().max_frames;
    let available = frame_limit - baseline.count(ControlKind::Frame);
    let held: Vec<_> = (0..available)
        .map(|_| control.reserve(ControlKind::Frame, 1).unwrap())
        .collect();
    assert_eq!(control.usage().count(ControlKind::Frame), frame_limit);
    let mut calls = 0;
    assert!(matches!(
        f.gate.with_cleanup_snapshot(&permit, &mut |_| {
            calls += 1;
            Ok(())
        }),
        Err(GateError::Limit(_))
    ));
    assert_eq!(calls, 0);
    assert_eq!(cleanup_snapshot_data_version(&raw), before);
    drop(held);
    assert_eq!(
        control.usage().count(ControlKind::Frame),
        baseline.count(ControlKind::Frame)
    );
    assert_eq!(control.usage().bytes, baseline.bytes);
    f.gate
        .with_cleanup_snapshot(&permit, &mut |_| {
            calls += 1;
            Ok(())
        })
        .unwrap();
    assert_eq!(calls, 1);
    assert_eq!(cleanup_snapshot_data_version(&raw), before);
}

#[tokio::test]
async fn signed_device_attestations_reject_forgery_and_nonce_replay() {
    let f = Fixture::new();
    let mut p = permissions();
    p.native = true;
    let cx = f.pack("local.native", p).await;
    let key = [42; 32];
    f.approvals
        .pair_device(
            &f.profile,
            "phone",
            SigningKey::from_bytes(&key).verifying_key().to_bytes(),
        )
        .unwrap();
    *f.prompt.mode.lock().unwrap() = PromptMode::Signed {
        key,
        nonce: "fresh".into(),
        forge: true,
    };
    let req = EffectRequest::Native {
        pack: owner(&cx).clone(),
    };
    assert!(
        f.gate
            .authorize(&cx, req.clone(), Digest::hash(b"args"))
            .await
            .is_err()
    );
    *f.prompt.mode.lock().unwrap() = PromptMode::Signed {
        key,
        nonce: "once".into(),
        forge: false,
    };
    let g = f.grant(&cx, req.clone(), Digest::hash(b"args")).await;
    let permit = g.consume(&cx, &req, &Digest::hash(b"args")).unwrap();
    let evidence = permit.trusted_approval().unwrap();
    assert_eq!(evidence.method(), TrustedApprovalMethod::DeviceAttestation);
    assert_eq!(
        evidence.snapshot_digest(),
        &f.prompt
            .snapshots
            .lock()
            .unwrap()
            .last()
            .unwrap()
            .snapshot_digest()
            .unwrap()
    );
    assert!(
        f.gate
            .authorize(&cx, req, Digest::hash(b"args"))
            .await
            .is_err()
    );
}

#[tokio::test]
async fn trusted_approval_follows_original_consumed_port_entry_without_leaking_to_other_grants() {
    let f = Fixture::with_control(Some(CoreControl::default()));
    let revision = Digest::hash(b"approval-evidence-frozen-revision");
    let document = Digest::hash(b"approval-evidence-user-policy");
    let shape = ShapeRef::parse("local.fixture.entry@1.0").unwrap();
    let op = OpName::parse("run").unwrap();
    f.gate
        .register_operation_views(
            &f.profile,
            &revision,
            [(shape.clone(), [op.clone()].into())].into(),
        )
        .unwrap();
    let mut policy = permissions();
    policy.ports.insert(shape.to_string());
    let cx = f.cx(Principal::User).with_revision(revision.clone());
    f.gate
        .register_user_policy_for_revision(
            &f.profile,
            &document,
            &revision,
            cx.security_epoch(),
            policy.clone(),
            policy.clone(),
        )
        .unwrap();
    f.approvals
        .set_user_permissions(
            &f.profile,
            &document,
            &revision,
            cx.security_epoch(),
            &policy,
        )
        .unwrap();
    let request = EffectRequest::PortCall { shape, op };
    let args = Digest::hash(b"approved-entry-arguments");
    let grant = f.grant(&cx, request.clone(), args.clone()).await;
    let permit = grant.consume(&cx, &request, &args).unwrap();
    let prompt = f.prompt.snapshots.lock().unwrap().last().unwrap().clone();
    assert_eq!(&prompt.reservation, permit.id());
    assert_eq!(&prompt.args_digest, permit.args_digest());
    let approval = permit.trusted_approval().unwrap();
    assert_eq!(approval.method(), TrustedApprovalMethod::Prompt);
    assert_eq!(approval.risk(), prompt.risk);
    assert_eq!(
        approval.snapshot_digest(),
        &prompt.snapshot_digest().unwrap()
    );
    // The original funding path includes this inline evidence through sizeof;
    // the dynamic copy inventory remains the same authenticated effect/IDs.
    let dynamic = permit.execution_entry_evidence_storage_bytes().unwrap();
    let _funding = f
        .gate
        .control()
        .reserve(
            ControlKind::Frame,
            (std::mem::size_of::<ExecutionEntryEvidence>() + dynamic) as u64,
        )
        .unwrap();
    let entry = permit.try_execution_entry_evidence_precharged().unwrap();
    assert_eq!(entry.trusted_approval(), Some(approval));
    assert_eq!(entry.grant(), permit.id());
    assert!(entry.matches_context(&cx));

    let pack = f.pack("local.unprompted", permissions()).await;
    let calls_after_install = f.prompt.calls.load(Ordering::SeqCst);
    let args = Digest::hash(b"unprompted-clock");
    let grant = f.grant(&pack, EffectRequest::Clock, args.clone()).await;
    let clock = grant.consume(&pack, &EffectRequest::Clock, &args).unwrap();
    assert!(clock.trusted_approval().is_none());
    assert_eq!(f.prompt.calls.load(Ordering::SeqCst), calls_after_install);
}

#[tokio::test]
async fn trusted_approval_never_overrides_original_args_or_revoked_epoch() {
    let f = Fixture::with_control(Some(CoreControl::default()));
    let mut policy = permissions();
    policy.native = true;
    let cx = f.pack("local.prompted", policy).await;
    let request = EffectRequest::Native {
        pack: owner(&cx).clone(),
    };
    let args = Digest::hash(b"approved-original");
    let grant = f.grant(&cx, request.clone(), args.clone()).await;
    assert!(
        grant
            .consume(&cx, &request, &Digest::hash(b"changed"))
            .is_err()
    );
    let grant = f.grant(&cx, request.clone(), args.clone()).await;
    f.approvals.advance_security_epoch(&f.profile).unwrap();
    assert!(matches!(
        grant.consume(&cx, &request, &args),
        Err(GateError::StaleEpoch)
    ));
}

#[tokio::test]
async fn client_approved_payload_and_dangerous_declaration_do_not_override_ceiling() {
    let f = Fixture::new();
    let pack = PackId::parse("local.dangerous").unwrap();
    let hash = PackHash::new(Digest::hash(b"dangerous"));
    let mut p = permissions();
    p.native = true;
    p.cross_profile_storage = true;
    let ceiling = permissions();
    f.gate
        .register_pack(
            &f.profile,
            &pack,
            &hash,
            PackPolicy {
                declaration: p.clone(),
                ceiling: ceiling.clone(),
                pack_limits: p.clone(),
            },
        )
        .unwrap();
    f.approvals
        .set_permissions(&f.profile, &pack, &hash, &p)
        .unwrap();
    let cx = f.cx(Principal::Pack {
        pack: pack.clone(),
        hash,
    });
    let payload = Digest::hash(br#"{"approved":true}"#);
    assert!(
        f.gate
            .authorize(&cx, EffectRequest::Native { pack }, payload.clone())
            .await
            .is_err()
    );
    assert!(
        f.gate
            .authorize(
                &cx,
                EffectRequest::UserGlobalStorage {
                    store: "all".into(),
                    operation: StoreOperation::Read
                },
                payload
            )
            .await
            .is_err()
    );
}

#[tokio::test]
async fn restricted_sql_denies_filesystem_escape_extensions_mutating_pragmas_and_query_writes() {
    let f = Fixture::new();
    let cx = f.pack("local.owner", permissions()).await;
    let stores = TieredStore::open(&f.root, &f.profile).unwrap();
    stores
        .register(
            owner(&cx),
            "database",
            StoreTier::Private,
            "local.owner.database@1.0",
        )
        .unwrap();
    let h = open(&f, &stores, &cx, owner(&cx), "database").await;
    native_sql(
        &f,
        &h,
        &cx,
        "CREATE TABLE notes(id INTEGER PRIMARY KEY,value TEXT)",
        &[],
        false,
    )
    .await
    .unwrap();
    native_sql(
        &f,
        &h,
        &cx,
        "INSERT INTO notes(value) VALUES(?1)",
        &[SqlCell::Text("ok".into())],
        false,
    )
    .await
    .unwrap();
    let result = native_sql(&f, &h, &cx, "SELECT value FROM notes", &[], true)
        .await
        .unwrap();
    let cell = result
        .value()
        .get_key("rows")
        .unwrap()
        .get_index(0)
        .unwrap()
        .get_index(0)
        .unwrap();
    assert_eq!(cell.get_key("type").unwrap().as_str(), Some("text"));
    assert_eq!(cell.get_key("value").unwrap().as_str(), Some("ok"));
    for sql in [
        "ATTACH DATABASE '/tmp/escaped.sqlite' AS escaped",
        "DETACH main",
        "PRAGMA writable_schema=ON",
        "PRAGMA journal_mode=OFF",
        "SELECT load_extension('/tmp/inject')",
        "SELECT readfile('/etc/passwd')",
        "SELECT 1; DELETE FROM notes",
        "DELETE FROM notes",
    ] {
        assert!(
            native_sql(&f, &h, &cx, sql, &[], true).await.is_err(),
            "accepted {sql}"
        );
    }
    for sql in [
        "ATTACH DATABASE '/tmp/escaped.sqlite' AS escaped",
        "PRAGMA writable_schema=ON",
        "CREATE TABLE one(id); CREATE TABLE two(id)",
    ] {
        assert!(
            native_sql(&f, &h, &cx, sql, &[], false).await.is_err(),
            "accepted {sql}"
        );
    }
    let retained = native_sql(&f, &h, &cx, "SELECT value FROM notes", &[], true)
        .await
        .unwrap();
    assert_eq!(
        retained
            .value()
            .get_key("rows")
            .unwrap()
            .get_index(0)
            .unwrap()
            .get_index(0)
            .unwrap()
            .get_key("value")
            .unwrap()
            .as_str(),
        Some("ok")
    );
}

struct TestKeys;
impl VaultKeyProvider for TestKeys {
    fn key(&self, _: &ProfileId) -> Result<[u8; 32], StoreError> {
        Ok([7; 32])
    }
}
#[tokio::test]
async fn secrets_are_opaque_encrypted_profile_bound_and_only_revealed_after_trusted_prompt() {
    let f = Fixture::new();
    let mut p = permissions();
    p.secrets.push(SecretPermission {
        name: "api-key".into(),
        reveal: true,
        optional: false,
    });
    p.network.origins.insert("https://api.example".into());
    let cx = f.pack("local.owner", p).await;
    let vault = Vault::open(&f.root, &f.profile, Arc::new(TestKeys)).unwrap();
    let secret = b"highly-sensitive-token-only-in-vault";
    vault.set("api-key", secret).unwrap();
    let sg = f
        .grant(
            &cx,
            EffectRequest::Secret {
                name: "api-key".into(),
                reveal: false,
            },
            Vault::secret_digest("api-key").unwrap(),
        )
        .await;
    let opaque = vault.get(&cx, "api-key", sg).unwrap();
    assert!(!format!("{opaque:?}").contains(std::str::from_utf8(secret).unwrap()));
    let req = EffectRequest::Network {
        origin: "https://api.example".into(),
        method: "POST".into(),
        url: "https://api.example/v1".into(),
    };
    let args = Digest::hash(b"opaque ref request");
    let g = f.grant(&cx, req.clone(), args.clone()).await;
    let permit = g.consume(&cx, &req, &args).unwrap();
    let header_budget: Arc<dyn AllocationBudget> = Arc::new(SqlTestValues(AtomicUsize::new(0)));
    assert_eq!(
        opaque
            .with_network_value(&cx, &permit, "Bearer ", &header_budget, |v| v.to_vec())
            .unwrap(),
        [b"Bearer ".as_slice(), secret].concat()
    );
    let file = f
        .root
        .read_profile_file(&f.profile, "secrets.vault", 4 * 1024 * 1024)
        .unwrap();
    assert!(!file.windows(secret.len()).any(|w| w == secret));
    let audit = f
        .root
        .read_profile_file(&f.profile, "audit.sqlite", 4 * 1024 * 1024)
        .unwrap();
    assert!(!audit.windows(secret.len()).any(|w| w == secret));
    let other = ProfileId::parse("prof_other").unwrap();
    f.root
        .write_profile_file_atomic(&other, "secrets.vault", &file)
        .unwrap();
    assert!(Vault::open(&f.root, &other, Arc::new(TestKeys)).is_err());
    *f.prompt.mode.lock().unwrap() = PromptMode::Deny;
    assert!(
        f.gate
            .authorize(
                &cx,
                EffectRequest::Secret {
                    name: "api-key".into(),
                    reveal: true
                },
                Vault::secret_digest("api-key").unwrap()
            )
            .await
            .is_err()
    );
}

#[test]
fn nofollow_data_root_and_cas_reject_symlinks_and_traversal() {
    let f = Fixture::new();
    assert!(f.root.read("../escape", 10).is_err());
    assert!(f.root.write_atomic("/tmp/escape", b"bad").is_err());
    #[cfg(unix)]
    {
        use std::os::unix::fs::symlink;
        let outside = tempfile::tempdir().unwrap();
        symlink(outside.path(), f.root.path().join("linked")).unwrap();
        assert!(f.root.write_atomic("linked/escape", b"bad").is_err());
        assert!(!outside.path().join("escape").exists());
        assert!(DataRoot::open(f.root.path().join("linked")).is_err());
        let cas = Cas::new(f.root.clone());
        let d = cas.put(b"artifact").unwrap();
        assert_eq!(cas.get(&d, 100).unwrap(), b"artifact");
        std::fs::remove_file(f.root.path().join("cas/sha256").join(d.hex())).unwrap();
        symlink(
            outside.path().join("target"),
            f.root.path().join("cas/sha256").join(d.hex()),
        )
        .unwrap();
        assert!(cas.get(&d, 100).is_err());
        assert!(cas.put(b"artifact").is_err());
    }
}

#[test]
fn profile_head_cas_is_process_shared_and_advances_epoch_with_the_head() {
    let f = Fixture::new();
    let initial = Digest::hash(b"initial");
    f.root
        .compare_and_swap_profile_head(&f.profile, None, &initial)
        .unwrap();
    let a = f.root.clone();
    let b = f.root.clone();
    let pa = f.profile.clone();
    let pb = f.profile.clone();
    let ea = initial.clone();
    let eb = initial.clone();
    let ta = std::thread::spawn(move || {
        a.compare_and_swap_profile_head(&pa, Some(&ea), &Digest::hash(b"one"))
    });
    let tb = std::thread::spawn(move || {
        b.compare_and_swap_profile_head(&pb, Some(&eb), &Digest::hash(b"two"))
    });
    let results = [ta.join().unwrap(), tb.join().unwrap()];
    assert_eq!(results.iter().filter(|r| r.is_ok()).count(), 1);
    let (head, epoch) = f.root.profile_head_snapshot(&f.profile).unwrap();
    assert!(head == Digest::hash(b"one") || head == Digest::hash(b"two"));
    assert_eq!(epoch, 2);
}

#[test]
fn audit_is_append_only_chained_and_quota_limited() {
    let f = Fixture::new();
    let audit = AuditLog::open(
        &f.root,
        &f.profile,
        AuditQuota {
            records_per_minute: 2,
            bytes_per_minute: 10000,
            total_bytes: 100000,
        },
    )
    .unwrap();
    let event = AuditEvent {
        profile: f.profile.clone(),
        principal: Principal::User,
        request: RequestId::parse("req_audit").unwrap(),
        kind: "test".into(),
        payload: CanonValue::Null,
    };
    audit.append(event.clone()).unwrap();
    audit.append(event.clone()).unwrap();
    assert!(matches!(audit.append(event), Err(GateError::Limit(_))));
    audit.verify().unwrap();
    let db = rusqlite::Connection::open(
        f.root
            .path()
            .join(format!("profiles/{}/audit.sqlite", f.profile)),
    )
    .unwrap();
    assert!(db.execute("DELETE FROM audit", []).is_err());
    assert!(db.execute("UPDATE audit SET digest='bad'", []).is_err());
    db.execute_batch("DROP TRIGGER audit_no_update; UPDATE audit SET digest='bad' WHERE seq=1;")
        .unwrap();
    assert!(audit.verify().is_err());
}
#[test]
#[cfg(unix)]
fn sqlite_main_file_and_journal_stay_in_the_pinned_directory_after_parent_replacement() {
    use std::os::unix::fs::symlink;
    let f = Fixture::new();
    let original = f.root.path().join(format!("profiles/{}", f.profile));
    let retained = f.root.path().join("profiles/retained");
    let outside = tempfile::tempdir().unwrap();
    std::fs::rename(&original, &retained).unwrap();
    symlink(outside.path(), &original).unwrap();
    assert_eq!(f.approvals.advance_security_epoch(&f.profile).unwrap(), 1);
    f.audit
        .append(AuditEvent {
            profile: f.profile.clone(),
            principal: Principal::User,
            request: RequestId::parse("req_after_rename").unwrap(),
            kind: "retained".into(),
            payload: CanonValue::Null,
        })
        .unwrap();
    f.audit.verify().unwrap();
    assert_eq!(std::fs::read_dir(outside.path()).unwrap().count(), 0);
    let retained_db = rusqlite::Connection::open(retained.join("approvals.sqlite")).unwrap();
    assert_eq!(
        retained_db
            .query_row("SELECT epoch FROM security_epoch", [], |r| r
                .get::<_, i64>(0))
            .unwrap(),
        1
    );
    assert!(Approvals::open(&f.root, &f.profile).is_err());
}
#[test]
#[cfg(unix)]
fn sqlite_journal_symlink_cannot_overwrite_an_external_file() {
    use std::os::unix::fs::symlink;
    let f = Fixture::new();
    let outside = tempfile::tempdir().unwrap();
    let target = outside.path().join("protected");
    std::fs::write(&target, b"unchanged").unwrap();
    let journal = f
        .root
        .path()
        .join(format!("profiles/{}/approvals.sqlite-journal", f.profile));
    symlink(&target, &journal).unwrap();
    assert!(f.approvals.advance_security_epoch(&f.profile).is_err());
    assert_eq!(std::fs::read(target).unwrap(), b"unchanged");
}

struct RevokeAtDispatch {
    approvals: Arc<Approvals>,
    audit: Arc<AuditLog>,
}
impl AuditSink for RevokeAtDispatch {
    fn append(&self, event: AuditEvent) -> Result<(), GateError> {
        let revoke = event.kind == "dispatch";
        let profile = event.profile.clone();
        self.audit.append(event)?;
        if revoke {
            self.approvals.advance_security_epoch(&profile)?;
        }
        Ok(())
    }
}
#[tokio::test]
async fn public_writer_approval_rolls_back_when_epoch_changes_at_final_dispatch() {
    let f = Fixture::new();
    let cx = f.pack("local.writer", permissions()).await;
    let (writer, hash) = cx.principal().pack().unwrap();
    let gate = GateEngine::new(
        f.approvals.clone(),
        Arc::new(RevokeAtDispatch {
            approvals: f.approvals.clone(),
            audit: f.audit.clone(),
        }),
        f.prompt.clone(),
        MediatorChain::default(),
    );
    gate.register_pack(
        &f.profile,
        writer,
        hash,
        PackPolicy {
            declaration: permissions(),
            ceiling: permissions(),
            pack_limits: permissions(),
        },
    )
    .unwrap();
    let target = PackId::parse("local.owner").unwrap();
    let effect = EffectRequest::Storage {
        profile: f.profile.clone(),
        owner: target.clone(),
        store: "shared".into(),
        tier: StoreTier::Public,
        operation: StoreOperation::Write,
    };
    let before = f.prompt.calls.load(Ordering::SeqCst);
    assert!(matches!(
        gate.authorize(&cx, effect, Digest::hash(b"write")).await,
        Err(GateError::StaleEpoch)
    ));
    assert_eq!(f.prompt.calls.load(Ordering::SeqCst), before + 1);
    assert!(
        !f.approvals
            .public_write_approved(&f.profile, writer, &target, "shared")
            .unwrap()
    );
    assert!(f.approvals.pending().unwrap().is_empty());
}
#[tokio::test]
async fn dangerous_permission_additions_require_a_dangerous_trusted_prompt() {
    for cross_profile in [false, true] {
        let f = Fixture::new();
        let pack = PackId::parse("local.danger").unwrap();
        let hash = PackHash::new(Digest::hash(b"danger"));
        let mut p = permissions();
        p.native = !cross_profile;
        p.cross_profile_storage = cross_profile;
        f.gate
            .register_pack(
                &f.profile,
                &pack,
                &hash,
                PackPolicy {
                    declaration: p.clone(),
                    ceiling: p.clone(),
                    pack_limits: p,
                },
            )
            .unwrap();
        f.gate
            .approve_pack_permissions(&f.cx(Principal::User), &pack, None, &hash)
            .await
            .unwrap();
        assert_eq!(*f.prompt.risks.lock().unwrap(), vec![Risk::Dangerous]);
    }
}
#[tokio::test]
async fn dropped_and_denied_grants_leave_no_live_reservations() {
    let f = Fixture::new();
    let cx = f.pack("local.owner", permissions()).await;
    let grant = f
        .grant(&cx, EffectRequest::AuditRead, Digest::hash(b"read"))
        .await;
    let id = grant.id().clone();
    assert_eq!(
        f.approvals.reservation_state(&id).unwrap().as_deref(),
        Some("dispatched")
    );
    drop(grant);
    assert_eq!(
        f.approvals.reservation_state(&id).unwrap().as_deref(),
        Some("finished")
    );
    *f.prompt.mode.lock().unwrap() = PromptMode::Deny;
    assert!(
        f.gate
            .authorize(
                &cx,
                EffectRequest::Storage {
                    profile: f.profile.clone(),
                    owner: PackId::parse("local.target").unwrap(),
                    store: "shared".into(),
                    tier: StoreTier::Public,
                    operation: StoreOperation::Write
                },
                Digest::hash(b"write")
            )
            .await
            .is_err()
    );
    assert!(f.approvals.pending().unwrap().is_empty());
}
#[tokio::test]
async fn expiry_sweeps_unconsumed_grants_and_attestation_nonces_are_bounded() {
    let f = Fixture::new();
    let cx = f.pack("local.owner", permissions()).await;
    let grant = f
        .grant(&cx, EffectRequest::AuditRead, Digest::hash(b"read"))
        .await;
    let id = grant.id().clone();
    f.approvals
        .sweep_expired(&f.profile, grant.expires_unix_ms() + 1)
        .unwrap();
    assert_eq!(
        f.approvals.reservation_state(&id).unwrap().as_deref(),
        Some("finished")
    );
    assert!(
        grant
            .consume(&cx, &EffectRequest::AuditRead, &Digest::hash(b"read"))
            .is_err()
    );
    let now = || {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_millis() as u64
    };
    let expiry = now() + 60_000;
    assert!(
        f.approvals
            .claim_attestation(&f.profile, "phone", "nonce", expiry)
            .unwrap()
    );
    assert!(
        !f.approvals
            .claim_attestation(&f.profile, "phone", "nonce", expiry)
            .unwrap()
    );
    let mut db = rusqlite::Connection::open(
        f.root
            .path()
            .join(format!("profiles/{}/approvals.sqlite", f.profile)),
    )
    .unwrap();
    let expired = now() - 1;
    // Advance this private fixture's persisted row to an expired state without
    // requiring the host to complete two SQLite transactions within 20ms.
    assert_eq!(
        db.execute(
            "UPDATE attestation_nonces SET expires=?1 WHERE device=?2 AND nonce=?3 AND expires=?4",
            rusqlite::params![expired as i64, "phone", "nonce", expiry as i64],
        )
        .unwrap(),
        1
    );
    let reused_expiry = now() + 60_000;
    assert!(
        f.approvals
            .claim_attestation(&f.profile, "phone", "nonce", reused_expiry)
            .unwrap()
    );
    assert_eq!(
        db.query_row(
            "SELECT expires FROM attestation_nonces WHERE device=?1 AND nonce=?2",
            rusqlite::params!["phone", "nonce"],
            |row| row.get::<_, i64>(0),
        )
        .unwrap(),
        reused_expiry as i64
    );
    assert!(
        f.approvals
            .claim_attestation(&f.profile, "phone", "expired", now() - 1)
            .is_err()
    );
    let tx = db.transaction().unwrap();
    {
        let mut stmt = tx
            .prepare("INSERT INTO attestation_nonces(device,nonce,expires) VALUES('phone',?1,?2)")
            .unwrap();
        for i in 0..4095 {
            stmt.execute(rusqlite::params![format!("n{i}"), (now() + 60_000) as i64])
                .unwrap();
        }
    }
    tx.commit().unwrap();
    assert!(matches!(
        f.approvals
            .claim_attestation(&f.profile, "phone", "over-quota", now() + 60_000),
        Err(GateError::Limit(_))
    ));
}
#[test]
fn ui_session_ids_and_pack_hash_updates_cannot_reset_audit_quota() {
    let f = Fixture::new();
    let audit = AuditLog::open(
        &f.root,
        &f.profile,
        AuditQuota {
            records_per_minute: 2,
            bytes_per_minute: 10_000,
            total_bytes: 100_000,
        },
    )
    .unwrap();
    let pack = PackId::parse("local.owner").unwrap();
    for i in 0..3 {
        let event = AuditEvent {
            profile: f.profile.clone(),
            principal: Principal::PackUi {
                pack: pack.clone(),
                hash: PackHash::new(Digest::hash(format!("h{i}").as_bytes())),
                ui: UiSessionId::parse(&format!("ui_{i}")).unwrap(),
            },
            request: RequestId::parse("req_audit").unwrap(),
            kind: "test".into(),
            payload: CanonValue::Null,
        };
        let result = audit.append(event);
        if i < 2 {
            result.unwrap();
        } else {
            assert!(matches!(result, Err(GateError::Limit(_))));
        }
    }
}
#[tokio::test]
async fn sqlite_allocations_and_preparation_are_bounded_before_row_conversion() {
    let f = Fixture::new();
    let cx = f.pack("local.owner", permissions()).await;
    let stores = TieredStore::open(&f.root, &f.profile).unwrap();
    stores
        .register(
            owner(&cx),
            "bounded",
            StoreTier::Private,
            "local.owner.bounded@1.0",
        )
        .unwrap();
    let handle = open(&f, &stores, &cx, owner(&cx), "bounded").await;
    for sql in [
        "SELECT printf('%5000000s','x')".to_string(),
        format!("SELECT coalesce({})", vec!["1"; 65].join(",")),
        format!("SELECT {}", vec!["1"; 257].join(",")),
        "SELECT ?257".to_string(),
        format!("SELECT 1 {}", " UNION ALL SELECT 1".repeat(64)),
    ] {
        assert!(
            native_sql(&f, &handle, &cx, &sql, &[], true).await.is_err(),
            "accepted {sql}"
        );
    }
    let result = native_sql(&f, &handle, &cx, "SELECT printf('%32s','x')", &[], true)
        .await
        .unwrap();
    assert_eq!(
        result
            .value()
            .get_key("rows")
            .unwrap()
            .array()
            .unwrap()
            .len(),
        1
    );
}

struct NarrowStoreExecution {
    policy: PermissionSet,
    fuel: u64,
    output: u64,
    wall: u64,
}
#[async_trait]
impl Mediator for NarrowStoreExecution {
    async fn review(&self, prompt: &ApprovalPrompt) -> Verdict {
        let read = matches!(
            prompt.effect,
            EffectRequest::Storage {
                operation: StoreOperation::Read | StoreOperation::SqlRead,
                ..
            } | EffectRequest::UserGlobalStorage {
                operation: StoreOperation::Read | StoreOperation::SqlRead,
                ..
            }
        );
        if !read {
            return Verdict::Pass;
        }
        let mut policy = self.policy.clone();
        policy.wasm.fuel_per_call = self.fuel;
        policy.wasm.output_bytes = self.output;
        policy.wasm.wall_ms = self.wall;
        Verdict::Constrain(policy)
    }
}
fn narrow_store_gate(
    f: &Fixture,
    cx: &CallCx,
    policy: PermissionSet,
    fuel: u64,
    output: u64,
    wall: u64,
) -> GateEngine {
    let gate = GateEngine::new(
        f.approvals.clone(),
        f.audit.clone(),
        f.prompt.clone(),
        MediatorChain::new(vec![Arc::new(NarrowStoreExecution {
            policy: policy.clone(),
            fuel,
            output,
            wall,
        })]),
    );
    let (pack, hash) = cx.principal().pack().unwrap();
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
    gate
}
#[tokio::test]
async fn store_execution_enforces_mediator_output_instruction_and_actual_deadline_caps() {
    let f = Fixture::new();
    let cx = f.pack("local.owner", permissions()).await;
    let stores = TieredStore::open(&f.root, &f.profile).unwrap();
    stores
        .register(
            owner(&cx),
            "narrowed",
            StoreTier::Private,
            "local.owner.narrowed@1.0",
        )
        .unwrap();
    let handle = open(&f, &stores, &cx, owner(&cx), "narrowed").await;
    execute(
        &f,
        &handle,
        &cx,
        StoreCall::KvPut {
            key: "value".into(),
            value: b"long stored content".to_vec(),
        },
    )
    .await
    .unwrap();
    for call in [
        StoreCall::KvGet {
            key: "value".into(),
        },
        StoreCall::KvList {
            prefix: "".into(),
            after: None,
            limit: 10,
        },
    ] {
        let gate = narrow_store_gate(
            &f,
            &cx,
            permissions(),
            limits().fuel_per_call,
            4,
            limits().wall_ms,
        );
        let grant = gate
            .authorize(&cx, handle.request(&call).unwrap(), call.digest().unwrap())
            .await
            .unwrap();
        assert!(matches!(
            handle.execute(&cx, call, grant),
            Err(StoreError::Quota)
        ));
    }
    let simple = StoreCall::KvGet {
        key: "value".into(),
    };
    let gate = narrow_store_gate(
        &f,
        &cx,
        permissions(),
        1,
        limits().output_bytes,
        limits().wall_ms,
    );
    let grant = gate
        .authorize(
            &cx,
            handle.request(&simple).unwrap(),
            simple.digest().unwrap(),
        )
        .await
        .unwrap();
    assert!(matches!(
        handle.execute(&cx, simple, grant),
        Err(StoreError::Gate(GateError::Limit(_)))
    ));
    let read = StoreCall::KvGet {
        key: "value".into(),
    };
    let gate = narrow_store_gate(
        &f,
        &cx,
        permissions(),
        limits().fuel_per_call,
        limits().output_bytes,
        0,
    );
    let grant = gate
        .authorize(&cx, handle.request(&read).unwrap(), read.digest().unwrap())
        .await
        .unwrap();
    assert!(matches!(
        handle.execute(&cx, read, grant),
        Err(StoreError::Gate(GateError::Deadline))
    ));
    // A failed narrowed call must not leave its progress hook installed on this handle.
    assert!(
        matches!(execute(&f, &handle, &cx, StoreCall::KvGet { key: "value".into() }).await.unwrap(), StoreResult::Value(Some(v)) if v == b"long stored content")
    );
}
#[tokio::test]
async fn dangerous_global_store_execution_obeys_the_same_narrowed_output_cap() {
    let f = Fixture::new();
    let mut policy = permissions();
    policy.cross_profile_storage = true;
    let cx = f.pack("local.owner", policy.clone()).await;
    let (request, args) = UserGlobalStore::open_request("narrowed-global").unwrap();
    let grant = f.grant(&cx, request, args).await;
    let handle = UserGlobalStore::open(&f.root, &cx, "narrowed-global", grant).unwrap();
    let put = StoreCall::KvPut {
        key: "value".into(),
        value: b"long global content".to_vec(),
    };
    let grant = f
        .grant(&cx, handle.request(&put), put.digest().unwrap())
        .await;
    handle.execute(&cx, put, grant).unwrap();
    let gate = narrow_store_gate(&f, &cx, policy, limits().fuel_per_call, 4, limits().wall_ms);
    let read = StoreCall::KvGet {
        key: "value".into(),
    };
    let grant = gate
        .authorize(&cx, handle.request(&read), read.digest().unwrap())
        .await
        .unwrap();
    assert!(matches!(
        handle.execute(&cx, read, grant),
        Err(StoreError::Quota)
    ));
}
#[tokio::test]
async fn live_reservation_last_slot_is_atomic_across_separate_sqlite_connections() {
    let f = Fixture::new();
    let cx = f.pack("local.owner", permissions()).await;
    let grant = f
        .grant(&cx, EffectRequest::AuditRead, Digest::hash(b"read"))
        .await;
    let reservation = Reservation {
        revision: None,
        authority: None,
        original_expires_unix_ms: None,
        id: grant.id().clone(),
        profile: f.profile.clone(),
        principal: cx.principal().clone(),
        request: cx.request().clone(),
        effect: EffectRequest::AuditRead.digest().unwrap(),
        args: Digest::hash(b"read"),
        epoch: cx.security_epoch(),
        expires_unix_ms: grant.expires_unix_ms(),
    };
    let id_for = |label: &str| {
        serde_json::from_value::<GrantId>(serde_json::Value::String(format!(
            "grant_{}",
            Digest::hash(label.as_bytes()).hex()
        )))
        .unwrap()
    };
    let raw = rusqlite::Connection::open(
        f.root
            .path()
            .join(format!("profiles/{}/approvals.sqlite", f.profile)),
    )
    .unwrap();
    // The actual limit also reserves future terminal metadata and payload
    // bytes. Fill through the real API, then free one trusted fixture slot.
    let mut last_seed = None;
    let mut limit_reached = false;
    for index in 0..4096 {
        let mut seeded = reservation.clone();
        seeded.id = id_for(&format!("seed{index}"));
        match f.approvals.reserve(&seeded) {
            Ok(()) => last_seed = Some(seeded.id),
            Err(GateError::Limit(_)) => {
                limit_reached = true;
                break;
            }
            Err(error) => panic!("unexpected reservation admission error: {error}"),
        }
    }
    assert!(limit_reached);
    let full_count: i64 = raw
        .query_row(
            "SELECT count(*) FROM reservations WHERE state!='finished'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert!((2..=4096).contains(&full_count));
    assert_eq!(
        raw.execute(
            "DELETE FROM reservations WHERE id=?1 AND state='pending'",
            [last_seed.unwrap().as_str()]
        )
        .unwrap(),
        1
    );
    let a = Approvals::open(&f.root, &f.profile).unwrap();
    let b = Approvals::open(&f.root, &f.profile).unwrap();
    let barrier = Arc::new(std::sync::Barrier::new(2));
    let mut first = reservation.clone();
    first.id = id_for("first-final-slot");
    let mut second = reservation;
    second.id = id_for("second-final-slot");
    let first_barrier = barrier.clone();
    let first_thread = std::thread::spawn(move || {
        first_barrier.wait();
        a.reserve(&first)
    });
    let second_thread = std::thread::spawn(move || {
        barrier.wait();
        b.reserve(&second)
    });
    let results = [first_thread.join().unwrap(), second_thread.join().unwrap()];
    assert_eq!(results.iter().filter(|r| r.is_ok()).count(), 1);
    assert_eq!(
        results
            .iter()
            .filter(|r| matches!(r, Err(GateError::Limit(_))))
            .count(),
        1
    );
    assert_eq!(
        raw.query_row(
            "SELECT COUNT(*) FROM reservations WHERE state!='finished'",
            [],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        full_count
    );
}
