//! End-to-end DATA policy tests for the currently verified owner-lock platform.
//! All database operations use the real GateEngine, fresh grants, and the
//! public native Store APIs. This trusted fixture's bounded value producer is
//! not a substitute for production guest-frame authentication.
#![cfg(any(
    all(target_os = "macos", target_arch = "aarch64"),
    all(target_os = "linux", target_arch = "x86_64", target_env = "gnu")
))]

use async_trait::async_trait;
use std::{
    collections::BTreeMap,
    sync::{
        Arc,
        atomic::{AtomicU64, AtomicUsize, Ordering},
    },
    time::{Duration, Instant},
};
use tobkiri_canon::{
    AllocationBudget, AllocationError, AllocationRequest, CanonValue, CoreValue, Digest, ids::*,
};
use tobkiri_gate::*;
use tobkiri_store::*;

struct Prompt(AtomicUsize);
#[async_trait]
impl TrustedPrompt for Prompt {
    async fn prompt_approval(&self, _: ApprovalPrompt) -> ApprovalAnswer {
        self.0.fetch_add(1, Ordering::AcqRel);
        ApprovalAnswer::Approve
    }
}
struct TestValues(AtomicU64);
impl AllocationBudget for TestValues {
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
struct Caller {
    profile: ProfileId,
    pack: PackId,
    gate: Arc<GateEngine>,
    cx: CallCx,
    prompt: Arc<Prompt>,
}
impl Caller {
    fn new(root: &DataRoot, control: &CoreControl, profile: &str, cross_profile: bool) -> Self {
        let profile = ProfileId::parse(profile).unwrap();
        let pack = PackId::parse("local.native-quota").unwrap();
        let hash = PackHash::new(Digest::hash(b"native-quota-fixture"));
        let approvals = Arc::new(Approvals::open(root, &profile).unwrap());
        let audit = Arc::new(AuditLog::open(root, &profile, AuditQuota::default()).unwrap());
        let prompt = Arc::new(Prompt(AtomicUsize::new(0)));
        let gate = Arc::new(GateEngine::new_with_control(
            approvals.clone(),
            audit,
            prompt.clone(),
            MediatorChain::default(),
            control.clone(),
        ));
        let limits = ResourceLimits {
            fuel_per_call: 1_000_000,
            memory_mib: 64,
            wall_ms: 30_000,
            table_elements: 4096,
            output_bytes: 1024 * 1024,
        };
        let policy = PermissionSet {
            storage: StoragePermissions {
                private: true,
                public_read: true,
                public_write: true,
            },
            cross_profile_storage: cross_profile,
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
        // Trusted fixture installation persists an explicit approved policy;
        // every tested data effect still passes normal Gate authorization.
        approvals
            .set_permissions(&profile, &pack, &hash, &policy)
            .unwrap();
        let cx = CallCx::try_root_with_control(
            profile.clone(),
            Principal::Pack {
                pack: pack.clone(),
                hash,
            },
            RequestId::parse("req_native_quota").unwrap(),
            CancellationToken::new_with_control(control).unwrap(),
            Instant::now() + Duration::from_secs(30),
            limits,
            control.clone(),
        )
        .unwrap()
        .with_security_epoch(gate.epoch(&profile).unwrap());
        Self {
            profile,
            pack,
            gate,
            cx,
            prompt,
        }
    }
    async fn open(&self, stores: &TieredStore, name: &str) -> Result<StoreHandle, StoreError> {
        let (request, digest) = stores.open_request(&self.pack, name)?;
        let grant = self.gate.authorize(&self.cx, request, digest).await?;
        stores.open_store(&self.cx, &self.pack, name, grant)
    }
    async fn global(
        &self,
        root: &DataRoot,
        name: &str,
        policy: StoreDiskPolicy,
    ) -> Result<UserGlobalStore, StoreError> {
        let (request, digest) = UserGlobalStore::open_request(name)?;
        let grant = self.gate.authorize(&self.cx, request, digest).await?;
        UserGlobalStore::open_with_disk_policy(root, &self.cx, name, grant, policy)
    }
}
struct Fixture {
    _temp: tempfile::TempDir,
    root: DataRoot,
    control: CoreControl,
    caller: Caller,
    values: Arc<TestValues>,
}
impl Fixture {
    fn new(control_limits: CoreControlLimits) -> Self {
        let temp = tempfile::tempdir().unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(temp.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
        }
        let root = DataRoot::open(temp.path().canonicalize().unwrap()).unwrap();
        let control = CoreControl::new(control_limits).unwrap();
        let caller = Caller::new(&root, &control, "prof_native_quota", true);
        Self {
            _temp: temp,
            root,
            control,
            caller,
            values: Arc::new(TestValues(AtomicU64::new(0))),
        }
    }
    fn budget(&self) -> Arc<dyn AllocationBudget> {
        self.values.clone()
    }
    fn stores(&self, policy: StoreDiskPolicy, names: &[(&str, StoreTier)]) -> TieredStore {
        let stores =
            TieredStore::open_with_disk_policy(&self.root, &self.caller.profile, policy).unwrap();
        for (name, tier) in names {
            stores
                .register(
                    &self.caller.pack,
                    name,
                    *tier,
                    "local.native-quota.data@1.0",
                )
                .unwrap();
        }
        stores
    }
}
fn statement(sql: &str, budget: &Arc<dyn AllocationBudget>) -> OwnedSqlStatement {
    let record = CanonValue::Obj(BTreeMap::from([
        ("statement".into(), CanonValue::Str(sql.into())),
        ("params".into(), CanonValue::Arr(vec![])),
        ("max-rows".into(), CanonValue::Int(16)),
    ]));
    OwnedSqlStatement::from_value(CoreValue::try_from_borrowed(&record, budget).unwrap()).unwrap()
}
async fn exec(f: &Fixture, handle: &StoreHandle, sql: &str) -> Result<OwnedSqlResult, StoreError> {
    let budget = f.budget();
    handle
        .sql_exec_typed(
            &f.caller.cx,
            statement(sql, &budget),
            SqlResultPolicy::ForbidRows,
            f.caller.gate.clone(),
            budget,
        )
        .await
}
async fn count(f: &Fixture, handle: &StoreHandle) -> u64 {
    let budget = f.budget();
    let result = handle
        .sql_query_typed(
            &f.caller.cx,
            statement("SELECT count(*) FROM payload", &budget),
            f.caller.gate.clone(),
            budget,
        )
        .await
        .unwrap();
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
        .parse()
        .unwrap()
}
fn quota(error: &StoreError) -> bool {
    matches!(
        error,
        StoreError::Quota
            | StoreError::Sql(rusqlite::Error::SqliteFailure(
                rusqlite::ffi::Error {
                    code: rusqlite::ErrorCode::DiskFull,
                    ..
                },
                _
            ))
    )
}

#[tokio::test]
async fn physical_private_database_quota_is_shared_and_failed_growth_rolls_back() {
    let f = Fixture::new(CoreControlLimits::default());
    let policy = StoreDiskPolicy::new(32 * 1024, 32 * 1024, 64 * 1024).unwrap();
    let stores = f.stores(
        policy,
        &[
            ("first", StoreTier::Private),
            ("second", StoreTier::Private),
        ],
    );
    let first = f.caller.open(&stores, "first").await.unwrap();
    let second = f.caller.open(&stores, "second").await.unwrap();
    exec(&f, &first, "CREATE TABLE payload(value BLOB)")
        .await
        .unwrap();
    exec(&f, &first, "INSERT INTO payload VALUES(x'01')")
        .await
        .unwrap();
    // A bounded literal exercises real disk growth without depending on a
    // guest SQL function outside the production function allowlist.
    let growth = format!("INSERT INTO payload VALUES(x'{}')", "07".repeat(24 * 1024));
    let failure = exec(&f, &second, &growth).await.unwrap_err();
    assert!(quota(&failure), "unexpected quota failure: {failure}");
    assert_eq!(count(&f, &first).await, 1);
    assert_eq!(count(&f, &second).await, 1);
    let directory = f.root.path().join(format!(
        "profiles/{}/data/{}",
        f.caller.profile, f.caller.pack
    ));
    assert!(
        std::fs::metadata(directory.join("private.sqlite"))
            .unwrap()
            .len()
            <= policy.database_bytes()
    );
    if let Ok(metadata) = std::fs::metadata(directory.join("private.sqlite-journal")) {
        assert!(metadata.len() <= policy.journal_bytes());
    }
    drop(first);
    drop(second);
    let reopened = f.caller.open(&stores, "first").await.unwrap();
    assert_eq!(count(&f, &reopened).await, 1);
    assert_eq!(f.values.0.load(Ordering::Acquire), 0);
}

#[tokio::test]
async fn database_and_transaction_keep_core_slots_until_their_last_handle_drops() {
    let f = Fixture::new(CoreControlLimits::default());
    let stores = f.stores(StoreDiskPolicy::default(), &[("data", StoreTier::Private)]);
    let baseline = f.control.usage().count(ControlKind::Registration);
    let handle = f.caller.open(&stores, "data").await.unwrap();
    assert_eq!(
        f.control.usage().count(ControlKind::Registration),
        baseline + 1
    );
    let clone = handle.clone();
    drop(handle);
    assert_eq!(
        f.control.usage().count(ControlKind::Registration),
        baseline + 1
    );
    let transaction = clone
        .sql_begin(&f.caller.cx, f.caller.gate.clone(), f.budget())
        .await
        .unwrap();
    drop(clone);
    assert_eq!(
        f.control.usage().count(ControlKind::Registration),
        baseline + 2
    );
    transaction.rollback(&f.caller.cx).await.unwrap();
    assert_eq!(transaction.state(), SqlTransactionState::RolledBack);
    assert_eq!(
        f.control.usage().count(ControlKind::Registration),
        baseline + 2
    );
    drop(transaction);
    assert_eq!(f.control.usage().count(ControlKind::Registration), baseline);
    assert_eq!(f.control.usage().count(ControlKind::Task), 0);
}

#[tokio::test]
async fn core_registration_exhaustion_refuses_a_database_before_file_creation() {
    let f = Fixture::new(CoreControlLimits {
        max_registrations: 16,
        ..Default::default()
    });
    let stores = f.stores(
        StoreDiskPolicy::default(),
        &[("first", StoreTier::Private), ("other", StoreTier::Public)],
    );
    let baseline = f.control.usage().count(ControlKind::Registration);
    let mut retained = Vec::new();
    while f.control.usage().count(ControlKind::Registration) < 15 {
        retained.push(f.control.reserve(ControlKind::Registration, 64).unwrap());
    }
    let first = f.caller.open(&stores, "first").await.unwrap();
    assert_eq!(f.control.usage().count(ControlKind::Registration), 16);
    let refused = f.caller.open(&stores, "other").await;
    assert!(matches!(refused, Err(StoreError::Quota)));
    let other_path = f.root.path().join(format!(
        "profiles/{}/data/{}/public/other.sqlite",
        f.caller.profile, f.caller.pack
    ));
    assert!(!other_path.exists());
    drop(first);
    assert_eq!(f.control.usage().count(ControlKind::Registration), 15);
    let other = f.caller.open(&stores, "other").await.unwrap();
    assert!(other_path.is_file());
    drop(other);
    assert_eq!(f.control.usage().count(ControlKind::Registration), 15);
    drop(retained);
    assert_eq!(f.control.usage().count(ControlKind::Registration), baseline);
}

#[tokio::test]
async fn typed_query_classification_and_file_escapes_are_checked_before_execution() {
    let f = Fixture::new(CoreControlLimits::default());
    let stores = f.stores(StoreDiskPolicy::default(), &[("data", StoreTier::Private)]);
    let handle = f.caller.open(&stores, "data").await.unwrap();
    exec(&f, &handle, "CREATE TABLE payload(value BLOB)")
        .await
        .unwrap();
    exec(&f, &handle, "INSERT INTO payload VALUES(x'01')")
        .await
        .unwrap();
    let escape = f.root.path().join("escaped.sqlite");
    for sql in [
        "DELETE FROM payload RETURNING value".to_owned(),
        "PRAGMA writable_schema=ON".to_owned(),
        "PRAGMA journal_mode=OFF".to_owned(),
        "SELECT load_extension('/tmp/unavailable')".to_owned(),
        "SELECT readfile('/etc/passwd')".to_owned(),
        "SELECT 1; DELETE FROM payload".to_owned(),
        format!("ATTACH DATABASE '{}' AS escaped", escape.display()),
        format!("VACUUM INTO '{}'", escape.display()),
    ] {
        let budget = f.budget();
        let result = handle
            .sql_query_typed(
                &f.caller.cx,
                statement(&sql, &budget),
                f.caller.gate.clone(),
                budget,
            )
            .await;
        assert!(result.is_err(), "query accepted {sql}");
    }
    assert!(
        exec(&f, &handle, "SELECT value FROM payload")
            .await
            .is_err()
    );
    assert_eq!(count(&f, &handle).await, 1);
    assert!(!escape.exists());
    assert_eq!(f.values.0.load(Ordering::Acquire), 0);
}

#[tokio::test]
async fn user_global_physical_connection_is_shared_but_each_profile_keeps_its_own_authority() {
    let f = Fixture::new(CoreControlLimits::default());
    let second = Caller::new(&f.root, &f.control, "prof_native_quota_second", true);
    let denied = Caller::new(&f.root, &f.control, "prof_native_quota_denied", false);
    let policy = StoreDiskPolicy::new(64 * 1024, 64 * 1024, 128 * 1024).unwrap();
    let first_handle = f.caller.global(&f.root, "shared", policy).await.unwrap();
    let second_handle = second.global(&f.root, "shared", policy).await.unwrap();
    assert!(denied.global(&f.root, "shared", policy).await.is_err());
    let budget = f.budget();
    first_handle
        .sql_exec_typed(
            &f.caller.cx,
            statement("CREATE TABLE payload(value BLOB)", &budget),
            SqlResultPolicy::ForbidRows,
            f.caller.gate.clone(),
            budget.clone(),
        )
        .await
        .unwrap();
    let transaction = first_handle
        .sql_begin(&f.caller.cx, f.caller.gate.clone(), budget.clone())
        .await
        .unwrap();
    let blocked = second_handle
        .sql_query_typed(
            &second.cx,
            statement("SELECT count(*) FROM payload", &budget),
            second.gate.clone(),
            budget.clone(),
        )
        .await;
    assert!(
        matches!(blocked, Err(StoreError::Conflict(_))),
        "profiles must share the actual native connection/transaction state"
    );
    transaction.rollback(&f.caller.cx).await.unwrap();
    let attempted = first_handle
        .sql_query_typed(
            &denied.cx,
            statement("SELECT count(*) FROM payload", &budget),
            denied.gate.clone(),
            budget.clone(),
        )
        .await;
    assert!(matches!(
        attempted,
        Err(StoreError::Gate(GateError::Denied(_)))
    ));
    second_handle
        .sql_exec_typed(
            &second.cx,
            statement("INSERT INTO payload VALUES(x'02')", &budget),
            SqlResultPolicy::ForbidRows,
            second.gate.clone(),
            budget.clone(),
        )
        .await
        .unwrap();
    let output = first_handle
        .sql_query_typed(
            &f.caller.cx,
            statement("SELECT count(*) FROM payload", &budget),
            f.caller.gate.clone(),
            budget.clone(),
        )
        .await
        .unwrap();
    assert_eq!(
        output
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
        Some("1")
    );
    assert!(f.caller.prompt.0.load(Ordering::Acquire) > 0);
    assert!(second.prompt.0.load(Ordering::Acquire) > 0);
    assert_eq!(denied.prompt.0.load(Ordering::Acquire), 0);
}
