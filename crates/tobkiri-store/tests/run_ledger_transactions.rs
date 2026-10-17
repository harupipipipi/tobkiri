//! Real native Store/Gate transaction prerequisites for a Pack-owned Run ledger.
//!
//! This fixture does not implement RunRepository or claim to authenticate a
//! production guest frame. Its public inputs have a bounded trusted producer;
//! every tested SQL operation consumes a real Gate grant and uses the real DATA
//! SQLite/VFS, owner lease, transaction resource and terminal bookkeeping.
//! No provider is installed or called. Guest claim/dispatch proof belongs to
//! the actual repository and ordinary Wasm integration tests.
#![cfg(any(
    windows,
    all(target_os = "macos", target_arch = "aarch64"),
    all(target_os = "linux", target_arch = "x86_64", target_env = "gnu")
))]

use async_trait::async_trait;
use std::{
    collections::BTreeMap,
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};
use tobkiri_canon::{
    AllocationBudget, AllocationError, AllocationRequest, CanonValue, CoreValue, Digest, ids::*,
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

#[derive(Default)]
struct PublicValues(AtomicU64);
impl AllocationBudget for PublicValues {
    fn reserve(&self, request: AllocationRequest) -> Result<(), AllocationError> {
        self.0
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |used| {
                used.checked_add(request.bytes)
                    .filter(|next| *next <= 8 * 1024 * 1024)
            })
            .map(|_| ())
            .map_err(|_| AllocationError::Limit)
    }
    fn release(&self, bytes: u64) {
        self.0.fetch_sub(bytes, Ordering::AcqRel);
    }
}

struct Fixture {
    gate: Arc<GateEngine>,
    cx: CallCx,
    profile: ProfileId,
    pack: PackId,
    root: DataRoot,
    values: Arc<dyn AllocationBudget>,
    // Drop every native handle and physical owner before removing the fixture.
    _temp: tempfile::TempDir,
}

impl Fixture {
    fn new() -> Self {
        let temp = tempfile::tempdir().unwrap();
        let root = DataRoot::open(temp.path().canonicalize().unwrap().join("data")).unwrap();
        let profile = ProfileId::parse("prof_run_ledger_sql").unwrap();
        let pack = PackId::parse("local.run-ledger-sql-fixture").unwrap();
        let hash = PackHash::new(Digest::hash(b"public-run-ledger-sql-fixture"));
        let approvals = Arc::new(Approvals::open(&root, &profile).unwrap());
        let audit = Arc::new(AuditLog::open(&root, &profile, AuditQuota::default()).unwrap());
        let gate = Arc::new(GateEngine::new(
            approvals.clone(),
            audit,
            Arc::new(NoPrompt),
            MediatorChain::default(),
        ));
        let limits = ResourceLimits {
            fuel_per_call: 2_000_000,
            memory_mib: 64,
            wall_ms: 60_000,
            table_elements: 4096,
            output_bytes: 64 * 1024,
        };
        let policy = PermissionSet {
            storage: StoragePermissions {
                private: true,
                public_read: false,
                public_write: false,
            },
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
        // The trusted fixture installs only this private SQL policy. Production
        // Profile/User approval and verified guest entry are separate tests.
        approvals
            .set_permissions(&profile, &pack, &hash, &policy)
            .unwrap();
        let control = gate.control();
        let cx = CallCx::try_root_with_control(
            profile.clone(),
            Principal::Pack {
                pack: pack.clone(),
                hash,
            },
            RequestId::parse("req_run_ledger_sql").unwrap(),
            CancellationToken::new_with_control(control).unwrap(),
            Instant::now() + Duration::from_secs(60),
            limits,
            control.clone(),
        )
        .unwrap()
        .with_security_epoch(gate.epoch(&profile).unwrap());
        Self {
            gate,
            cx,
            profile,
            pack,
            root,
            values: Arc::new(PublicValues::default()),
            _temp: temp,
        }
    }

    async fn open_independent(&self) -> StoreHandle {
        // open_store constructs a new NativeDatabase/connection each time.
        // A clone of StoreHandle would instead share active_transaction.
        let catalog = TieredStore::open(&self.root, &self.profile).unwrap();
        catalog
            .register(
                &self.pack,
                "workflows",
                StoreTier::Private,
                "local.run-ledger-sql-fixture.records@1.0",
            )
            .unwrap();
        let (request, args) = catalog.open_request(&self.pack, "workflows").unwrap();
        let grant = self.gate.authorize(&self.cx, request, args).await.unwrap();
        catalog
            .open_store(&self.cx, &self.pack, "workflows", grant)
            .unwrap()
    }

    fn statement(&self, sql: &str, params: Vec<CanonValue>) -> OwnedSqlStatement {
        statement(sql, params, &self.values)
    }

    async fn exec(
        &self,
        handle: &StoreHandle,
        sql: &str,
        params: Vec<CanonValue>,
    ) -> OwnedSqlResult {
        handle
            .sql_exec_typed(
                &self.cx,
                self.statement(sql, params),
                SqlResultPolicy::ForbidRows,
                self.gate.clone(),
                self.values.clone(),
            )
            .await
            .unwrap()
    }

    async fn query(&self, handle: &StoreHandle, sql: &str) -> OwnedSqlResult {
        handle
            .sql_query_typed(
                &self.cx,
                self.statement(sql, vec![]),
                self.gate.clone(),
                self.values.clone(),
            )
            .await
            .unwrap()
    }

    async fn begin(&self, handle: &StoreHandle) -> Arc<NativeSqlTransaction> {
        handle
            .sql_begin(&self.cx, self.gate.clone(), self.values.clone())
            .await
            .unwrap()
    }
}

fn statement(
    sql: &str,
    params: Vec<CanonValue>,
    budget: &Arc<dyn AllocationBudget>,
) -> OwnedSqlStatement {
    let value = CanonValue::Obj(BTreeMap::from([
        ("statement".into(), CanonValue::Str(sql.into())),
        ("params".into(), CanonValue::Arr(params)),
        ("max-rows".into(), CanonValue::Int(10)),
    ]));
    OwnedSqlStatement::from_value(CoreValue::try_from_borrowed(&value, budget).unwrap()).unwrap()
}

fn text(value: &str) -> CanonValue {
    CanonValue::Obj(BTreeMap::from([
        ("type".into(), CanonValue::Str("text".into())),
        ("value".into(), CanonValue::Str(value.into())),
    ]))
}

fn affected(result: &OwnedSqlResult) -> u64 {
    result
        .value()
        .get_key("affected-rows")
        .unwrap()
        .as_str()
        .unwrap()
        .parse()
        .unwrap()
}

fn first_cell(result: &OwnedSqlResult) -> String {
    result
        .value()
        .get_key("rows")
        .unwrap()
        .get_index(0)
        .unwrap()
        .get_index(0)
        .unwrap()
        .get_key("value")
        .unwrap()
        .as_str()
        .unwrap()
        .to_owned()
}

async fn tx_exec(
    f: &Fixture,
    tx: &NativeSqlTransaction,
    sql: &str,
    params: Vec<CanonValue>,
) -> OwnedSqlResult {
    tx.exec_typed(
        &f.cx,
        f.statement(sql, params),
        SqlResultPolicy::ForbidRows,
        f.gate.clone(),
        f.values.clone(),
    )
    .await
    .unwrap()
}

#[tokio::test]
async fn independent_native_handles_have_distinct_transactions_and_one_committed_database() {
    let f = Fixture::new();
    let a = f.open_independent().await;
    let b = f.open_independent().await;
    f.exec(
        &a,
        "CREATE TABLE fixture_heads(run_id TEXT PRIMARY KEY, head TEXT NOT NULL)",
        vec![],
    )
    .await;
    let first = f.begin(&a).await;
    let second = f.begin(&b).await;
    // Both begins succeed while live: these cannot be aliases of one resource.
    second.rollback(&f.cx).await.unwrap();
    tx_exec(
        &f,
        &first,
        "INSERT INTO fixture_heads VALUES('public-run','head-1')",
        vec![],
    )
    .await;
    first.commit(&f.cx, f.gate.clone()).await.unwrap();
    assert_eq!(
        first_cell(&f.query(&b, "SELECT head FROM fixture_heads").await),
        "head-1"
    );
    let rollback = f.begin(&a).await;
    tx_exec(
        &f,
        &rollback,
        "UPDATE fixture_heads SET head='must-rollback'",
        vec![],
    )
    .await;
    rollback.rollback(&f.cx).await.unwrap();
    assert_eq!(
        first_cell(&f.query(&b, "SELECT head FROM fixture_heads").await),
        "head-1"
    );
}

#[tokio::test]
async fn stale_head_cas_rolls_back_its_provisional_snapshot() {
    let f = Fixture::new();
    let a = f.open_independent().await;
    let b = f.open_independent().await;
    f.exec(&a, "CREATE TABLE fixture_heads(run_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, head TEXT NOT NULL)", vec![]).await;
    f.exec(
        &a,
        "CREATE TABLE fixture_snapshots(head TEXT PRIMARY KEY, body TEXT NOT NULL)",
        vec![],
    )
    .await;
    f.exec(
        &a,
        "INSERT INTO fixture_heads VALUES('public-run',0,'head-0')",
        vec![],
    )
    .await;
    let winner = f.begin(&a).await;
    tx_exec(
        &f,
        &winner,
        "INSERT INTO fixture_snapshots VALUES('head-1','public-snapshot-1')",
        vec![],
    )
    .await;
    let updated = tx_exec(&f, &winner,
        "UPDATE fixture_heads SET revision=1,head=?1 WHERE run_id='public-run' AND revision=0 AND head=?2",
        vec![text("head-1"), text("head-0")]).await;
    assert_eq!(affected(&updated), 1);
    winner.commit(&f.cx, f.gate.clone()).await.unwrap();
    let stale = f.begin(&b).await;
    tx_exec(
        &f,
        &stale,
        "INSERT INTO fixture_snapshots VALUES('stale-head','must-rollback')",
        vec![],
    )
    .await;
    let updated = tx_exec(&f, &stale,
        "UPDATE fixture_heads SET revision=2,head='stale-head' WHERE run_id='public-run' AND revision=0 AND head='head-0'",
        vec![]).await;
    assert_eq!(affected(&updated), 0);
    stale.rollback(&f.cx).await.unwrap();
    assert_eq!(
        first_cell(&f.query(&a, "SELECT head FROM fixture_heads").await),
        "head-1"
    );
    assert_eq!(
        first_cell(&f.query(&a, "SELECT count(*) FROM fixture_snapshots").await),
        "1"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn concurrent_unique_claim_inserts_have_one_confirmed_winner() {
    let f = Arc::new(Fixture::new());
    let a = f.open_independent().await;
    let b = f.open_independent().await;
    f.exec(&a, "CREATE TABLE fixture_claims(run_id TEXT NOT NULL, generation_key TEXT NOT NULL, attempt_id TEXT NOT NULL, PRIMARY KEY(run_id,generation_key),UNIQUE(run_id,attempt_id))", vec![]).await;
    let barrier = Arc::new(tokio::sync::Barrier::new(2));
    let attempt = |handle: StoreHandle| {
        let f = f.clone();
        let barrier = barrier.clone();
        async move {
            let tx = f.begin(&handle).await;
            barrier.wait().await;
            let inserted = tx
                .exec_typed(
                    &f.cx,
                    f.statement(
                        "INSERT INTO fixture_claims VALUES('public-run','phase#0','attempt-0')",
                        vec![],
                    ),
                    SqlResultPolicy::ForbidRows,
                    f.gate.clone(),
                    f.values.clone(),
                )
                .await;
            match inserted {
                Ok(result) => {
                    assert_eq!(affected(&result), 1);
                    tx.commit(&f.cx, f.gate.clone()).await.unwrap();
                    true
                }
                Err(error) => {
                    // Busy/constraint is a loser, never an automatic retry.
                    assert!(matches!(
                        error,
                        StoreError::Sql(_) | StoreError::Conflict(_)
                    ));
                    assert_eq!(tx.state(), SqlTransactionState::RollbackOnly);
                    tx.rollback(&f.cx).await.unwrap();
                    false
                }
            }
        }
    };
    let (left, right) = tokio::join!(tokio::spawn(attempt(a)), tokio::spawn(attempt(b)));
    let (left, right) = (left.unwrap(), right.unwrap());
    assert_eq!(u8::from(left) + u8::from(right), 1);
    let inspect = f.open_independent().await;
    assert_eq!(
        first_cell(
            &f.query(&inspect, "SELECT count(*) FROM fixture_claims")
                .await
        ),
        "1"
    );
}

#[tokio::test]
async fn transaction_rejects_rebound_epoch_and_revision_before_mutation() {
    let f = Fixture::new();
    let handle = f.open_independent().await;
    f.exec(
        &handle,
        "CREATE TABLE fixture_rows(value TEXT NOT NULL)",
        vec![],
    )
    .await;
    let tx = f.begin(&handle).await;
    let changed_epoch = f.cx.clone().with_security_epoch(f.cx.security_epoch() + 1);
    let changed_revision =
        f.cx.clone()
            .with_revision(Digest::hash(b"another-revision"));
    for changed in [&changed_epoch, &changed_revision] {
        let result = tx
            .exec_typed(
                changed,
                f.statement("INSERT INTO fixture_rows VALUES('must-not-write')", vec![]),
                SqlResultPolicy::ForbidRows,
                f.gate.clone(),
                f.values.clone(),
            )
            .await;
        assert!(matches!(result, Err(StoreError::Denied(_))));
        assert_eq!(tx.state(), SqlTransactionState::Active);
    }
    tx.rollback(&f.cx).await.unwrap();
    assert_eq!(
        first_cell(&f.query(&handle, "SELECT count(*) FROM fixture_rows").await),
        "0"
    );
}

#[tokio::test]
async fn published_epoch_revokes_commit_but_retains_exact_rollback_cleanup() {
    let f = Fixture::new();
    let handle = f.open_independent().await;
    f.exec(
        &handle,
        "CREATE TABLE fixture_rows(value TEXT NOT NULL)",
        vec![],
    )
    .await;
    let tx = f.begin(&handle).await;
    tx_exec(
        &f,
        &tx,
        "INSERT INTO fixture_rows VALUES('provisional')",
        vec![],
    )
    .await;
    f.gate.revoke_epoch(&f.profile).unwrap();
    assert!(tx.commit(&f.cx, f.gate.clone()).await.is_err());
    // Revocation does not manufacture a replacement transaction context.
    tx.rollback(&f.cx).await.unwrap();
    let fresh =
        f.cx.clone()
            .with_security_epoch(f.gate.epoch(&f.profile).unwrap());
    let rows = handle
        .sql_query_typed(
            &fresh,
            f.statement("SELECT count(*) FROM fixture_rows", vec![]),
            f.gate.clone(),
            f.values.clone(),
        )
        .await
        .unwrap();
    assert_eq!(first_cell(&rows), "0");
}

/// Inject a lost acknowledgement after the genuine terminal path ran. This
/// wrapper never authorizes or consumes a grant itself; all authority and
/// durable outcome work is delegated unchanged to the actual GateEngine.
struct LostCommitAcknowledgement {
    real: Arc<GateEngine>,
    armed: AtomicBool,
}
#[async_trait]
impl Gate for LostCommitAcknowledgement {
    fn check_context(&self, cx: &CallCx) -> Result<(), GateError> {
        self.real.check_context(cx)
    }
    async fn authorize(
        &self,
        cx: &CallCx,
        effect: EffectRequest,
        args: Digest,
    ) -> Result<Grant, GateError> {
        self.real.authorize(cx, effect, args).await
    }
    fn watch_lease(&self, cx: &CallCx, permit: &DispatchPermit) -> Result<(), GateError> {
        self.real.watch_lease(cx, permit)
    }
    async fn record_denial(
        &self,
        cx: &CallCx,
        kind: &'static str,
        code: &'static str,
    ) -> Result<(), GateError> {
        self.real.record_denial(cx, kind, code).await
    }
    async fn record_outcome(&self, grant: GrantId, outcome: Outcome) -> Result<(), GateError> {
        self.real.record_outcome(grant, outcome).await?;
        if self.armed.swap(false, Ordering::AcqRel) {
            return Err(GateError::Persistence(
                "fixture lost terminal acknowledgement".into(),
            ));
        }
        Ok(())
    }
}

#[tokio::test]
async fn committed_claim_with_lost_ack_is_unknown_and_cannot_be_recommitted_or_rolled_back() {
    let f = Fixture::new();
    let writer = f.open_independent().await;
    let reader = f.open_independent().await;
    f.exec(
        &writer,
        "CREATE TABLE fixture_claims(attempt_id TEXT PRIMARY KEY)",
        vec![],
    )
    .await;
    let tx = f.begin(&writer).await;
    tx_exec(
        &f,
        &tx,
        "INSERT INTO fixture_claims VALUES('public-attempt')",
        vec![],
    )
    .await;
    let lost = Arc::new(LostCommitAcknowledgement {
        real: f.gate.clone(),
        armed: AtomicBool::new(true),
    });
    assert!(matches!(
        tx.commit(&f.cx, lost).await,
        Err(StoreError::CommitUnknown)
    ));
    assert_eq!(tx.state(), SqlTransactionState::CommitUnknown);
    assert!(matches!(
        tx.commit(&f.cx, f.gate.clone()).await,
        Err(StoreError::CommitUnknown)
    ));
    assert!(matches!(
        tx.rollback(&f.cx).await,
        Err(StoreError::CommitUnknown)
    ));
    // A later read can inspect the durable row; it cannot turn the original
    // CommitUnknown into a confirmed success or mint a provider dispatch.
    assert_eq!(
        first_cell(
            &f.query(&reader, "SELECT count(*) FROM fixture_claims")
                .await
        ),
        "1"
    );
    assert_eq!(tx.state(), SqlTransactionState::CommitUnknown);
}
