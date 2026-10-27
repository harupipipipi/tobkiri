//! Shared actual Gate/Store fixture. No product GUI/OS approval proof.
use crate::*;
use std::{
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};
use tobkiri_canon::{
    AllocationBudget, AllocationError, AllocationRequest, CanonValue, CoreValue, Digest, ids::*,
};
use tobkiri_gate::{
    ApprovalAnswer, ApprovalPrompt, CallCx, CancellationToken, Gate, GateEngine, MediatorChain,
    PackPolicy, PermissionSet, Principal, ResourceLimits, StoragePermissions, StoreTier,
    TrustedPrompt,
};

struct Prompt;
#[async_trait::async_trait]
impl TrustedPrompt for Prompt {
    async fn prompt_approval(&self, _: ApprovalPrompt) -> ApprovalAnswer {
        ApprovalAnswer::Approve
    }
}
struct Values(AtomicU64);
impl AllocationBudget for Values {
    fn reserve(&self, request: AllocationRequest) -> Result<(), AllocationError> {
        self.0
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |v| {
                v.checked_add(request.bytes)
                    .filter(|n| *n <= 8 * 1024 * 1024)
            })
            .map(|_| ())
            .map_err(|_| AllocationError::Limit)
    }
    fn release(&self, bytes: u64) {
        self.0
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |v| {
                v.checked_sub(bytes)
            })
            .expect("actual value custody must release once");
    }
}
pub(crate) struct Fixture {
    pub(crate) temp: tempfile::TempDir,
    pub(crate) root: DataRoot,
    pub(crate) profile: ProfileId,
    pub(crate) pack: PackId,
    pub(crate) hash: PackHash,
    pub(crate) permissions: PermissionSet,
    pub(crate) approvals: Arc<Approvals>,
    pub(crate) gate: Arc<GateEngine>,
    pub(crate) cx: CallCx,
}
impl Fixture {
    pub(crate) async fn new() -> Self {
        let temp = tempfile::tempdir().unwrap();
        let root = DataRoot::open(temp.path().canonicalize().unwrap()).unwrap();
        let profile = ProfileId::parse("prof_storage_fence").unwrap();
        let pack = PackId::parse("local.storage-fence-test").unwrap();
        let hash = PackHash::new(Digest::hash(b"storage fence fixture declaration"));
        let limits = ResourceLimits {
            fuel_per_call: 2_000_000_000,
            memory_mib: 64,
            wall_ms: 30_000,
            output_bytes: 4 * 1024 * 1024,
            table_elements: 65536,
        };
        let permissions = PermissionSet {
            storage: StoragePermissions {
                private: true,
                ..Default::default()
            },
            wasm: limits.clone(),
            ..Default::default()
        };
        let approvals = Arc::new(Approvals::open(&root, &profile).unwrap());
        let gate = Arc::new(GateEngine::new(
            approvals.clone(),
            Arc::new(AuditLog::open(&root, &profile, AuditQuota::default()).unwrap()),
            Arc::new(Prompt),
            MediatorChain::default(),
        ));
        gate.register_pack(
            &profile,
            &pack,
            &hash,
            PackPolicy {
                declaration: permissions.clone(),
                ceiling: permissions.clone(),
                pack_limits: permissions.clone(),
            },
        )
        .unwrap();
        let user = CallCx::try_root_with_control(
            profile.clone(),
            Principal::User,
            RequestId::parse("req_storage_fence_review").unwrap(),
            CancellationToken::new_with_control(gate.control()).unwrap(),
            Instant::now() + Duration::from_secs(30),
            limits.clone(),
            gate.control().clone(),
        )
        .unwrap();
        gate.approve_pack_permissions(&user, &pack, None, &hash)
            .await
            .unwrap();
        let cx = CallCx::try_root_with_control(
            profile.clone(),
            Principal::Pack {
                pack: pack.clone(),
                hash: hash.clone(),
            },
            RequestId::parse("req_storage_fence_write").unwrap(),
            CancellationToken::new_with_control(gate.control()).unwrap(),
            Instant::now() + Duration::from_secs(30),
            limits,
            gate.control().clone(),
        )
        .unwrap()
        .with_security_epoch(gate.epoch(&profile).unwrap());
        Self {
            temp,
            root,
            profile,
            pack,
            hash,
            permissions,
            approvals,
            gate,
            cx,
        }
    }
    pub(crate) async fn store(&self) -> StoreHandle {
        let stores = TieredStore::open(&self.root, &self.profile).unwrap();
        stores
            .register(
                &self.pack,
                "records",
                StoreTier::Private,
                "local.storage-fence-test.data@1.0",
            )
            .unwrap();
        let (request, args) = stores.open_request(&self.pack, "records").unwrap();
        let grant = self.gate.authorize(&self.cx, request, args).await.unwrap();
        stores
            .open_store(&self.cx, &self.pack, "records", grant)
            .unwrap()
    }
    pub(crate) fn recovered_gate(&self) -> Arc<GateEngine> {
        let gate = Arc::new(GateEngine::new_with_control(
            Arc::new(Approvals::open(&self.root, &self.profile).unwrap()),
            Arc::new(AuditLog::open(&self.root, &self.profile, AuditQuota::default()).unwrap()),
            Arc::new(Prompt),
            MediatorChain::default(),
            self.gate.control().clone(),
        ));
        gate.register_pack(
            &self.profile,
            &self.pack,
            &self.hash,
            PackPolicy {
                declaration: self.permissions.clone(),
                ceiling: self.permissions.clone(),
                pack_limits: self.permissions.clone(),
            },
        )
        .unwrap();
        gate
    }
}
pub(crate) fn sql_statement(text: &str) -> (OwnedSqlStatement, Arc<dyn AllocationBudget>) {
    let budget: Arc<dyn AllocationBudget> = Arc::new(Values(AtomicU64::new(0)));
    let value = CanonValue::Obj(std::collections::BTreeMap::from([
        ("statement".into(), CanonValue::Str(text.into())),
        ("params".into(), CanonValue::Arr(vec![])),
        ("max-rows".into(), CanonValue::Int(10)),
    ]));
    let statement =
        OwnedSqlStatement::from_value(CoreValue::try_from_borrowed(&value, &budget).unwrap())
            .unwrap();
    (statement, budget)
}
pub(crate) async fn sql(
    handle: &StoreHandle,
    cx: &CallCx,
    gate: Arc<GateEngine>,
    text: &str,
    read: bool,
) -> Result<OwnedSqlResult, StoreError> {
    let (statement, budget) = sql_statement(text);
    if read {
        handle.sql_query_typed(cx, statement, gate, budget).await
    } else {
        handle
            .sql_exec_typed(cx, statement, SqlResultPolicy::ForbidRows, gate, budget)
            .await
    }
}
