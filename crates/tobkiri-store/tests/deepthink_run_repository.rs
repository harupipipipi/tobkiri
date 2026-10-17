//! Product RunRepository over real native DATA/typed SQL resources and Gate.
//! No raw SQLite, network, model or fake SQL database is used. Only the public
//! value producer and counted provider sink are test fixtures; this does not
//! replace the separate production guest-frame/User/Flow acceptance test.
#![cfg(any(
    windows,
    all(target_os = "macos", target_arch = "aarch64"),
    all(target_os = "linux", target_arch = "x86_64", target_env = "gnu")
))]

#[allow(dead_code)]
#[path = "../../../packs/local.deepthink/src/run_repository.rs"]
mod run_repository;

use async_trait::async_trait;
use run_repository as repository;
use std::{
    collections::BTreeMap,
    sync::{
        Arc, Barrier,
        atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering},
    },
    time::{Duration, Instant},
};
use tobkiri_canon::{
    AllocationBudget, AllocationError, AllocationRequest, CanonValue, CoreRef, CoreValue,
    Digest as CoreDigest, ids::*,
};
use tobkiri_gate::*;
use tobkiri_store::*;

type Repo = repository::RunRepository<StoreBridge>;
type RepoError = repository::RepositoryError;
type RepoCell = repository::SqlCell;
type AdapterError = repository::SqlError;
type AdapterKind = repository::SqlErrorKind;

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
                    .filter(|next| *next <= 64 * 1024 * 1024)
            })
            .map(|_| ())
            .map_err(|_| AllocationError::Limit)
    }
    fn release(&self, bytes: u64) {
        self.0.fetch_sub(bytes, Ordering::AcqRel);
    }
}

struct Fixture {
    runtime: Arc<tokio::runtime::Runtime>,
    gate: Arc<GateEngine>,
    cx: CallCx,
    profile: ProfileId,
    pack: PackId,
    root: DataRoot,
    budget: Arc<dyn AllocationBudget>,
    lose_next_commit_ack: Arc<AtomicBool>,
    provider_calls: Arc<AtomicUsize>,
    // Real DATA/owner custody must be dropped before temporary directory removal.
    _temp: tempfile::TempDir,
}

impl Fixture {
    fn new() -> Self {
        let temp = tempfile::tempdir().unwrap();
        let root = DataRoot::open(temp.path().canonicalize().unwrap().join("data")).unwrap();
        let profile = ProfileId::parse("prof_repository_native").unwrap();
        let pack = PackId::parse("local.repository-native-fixture").unwrap();
        let hash = PackHash::new(CoreDigest::hash(b"repository-native-fixture"));
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
            memory_mib: 128,
            wall_ms: 300_000,
            table_elements: 4096,
            output_bytes: 4 * 1024 * 1024,
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
        // Trusted, isolated policy installation, identical in role to the
        // existing native Store fixtures. No real User approval is fabricated.
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
            RequestId::parse("req_repository_native").unwrap(),
            CancellationToken::new_with_control(control).unwrap(),
            Instant::now() + Duration::from_secs(300),
            limits,
            control.clone(),
        )
        .unwrap()
        .with_security_epoch(gate.epoch(&profile).unwrap());
        Self {
            runtime: Arc::new(
                tokio::runtime::Builder::new_multi_thread()
                    .worker_threads(2)
                    .enable_all()
                    .build()
                    .unwrap(),
            ),
            gate,
            cx,
            profile,
            pack,
            root,
            budget: Arc::new(PublicValues::default()),
            lose_next_commit_ack: Arc::new(AtomicBool::new(false)),
            provider_calls: Arc::new(AtomicUsize::new(0)),
            _temp: temp,
        }
    }

    fn bridge(&self) -> StoreBridge {
        self.bridge_with_context(self.cx.clone())
    }

    fn bridge_with_context(&self, cx: CallCx) -> StoreBridge {
        let catalog = TieredStore::open(&self.root, &self.profile).unwrap();
        catalog
            .register(
                &self.pack,
                "workflows",
                StoreTier::Private,
                "local.repository-native-fixture.records@1.0",
            )
            .unwrap();
        let (effect, args) = catalog.open_request(&self.pack, "workflows").unwrap();
        let grant = self
            .runtime
            .block_on(self.gate.authorize(&cx, effect, args))
            .unwrap();
        // Each open constructs a new native connection; no StoreHandle clone
        // is passed off as an independent transaction/SQLite connection.
        let store = catalog
            .open_store(&cx, &self.pack, "workflows", grant)
            .unwrap();
        StoreBridge {
            runtime: self.runtime.clone(),
            gate: self.gate.clone(),
            cx,
            store,
            budget: self.budget.clone(),
            lose_next_commit_ack: self.lose_next_commit_ack.clone(),
        }
    }

    fn repo(&self) -> Repo {
        Repo::open(self.bridge()).unwrap()
    }

    fn dispatch(&self, permit: repository::DispatchPermit, claim: &repository::GenerationClaim) {
        counted_dispatch(&self.provider_calls, permit, claim).unwrap();
    }
}

struct StoreBridge {
    runtime: Arc<tokio::runtime::Runtime>,
    gate: Arc<GateEngine>,
    cx: CallCx,
    store: StoreHandle,
    budget: Arc<dyn AllocationBudget>,
    lose_next_commit_ack: Arc<AtomicBool>,
}

struct StoreTransaction {
    runtime: Arc<tokio::runtime::Runtime>,
    gate: Arc<GateEngine>,
    cx: CallCx,
    native: Arc<NativeSqlTransaction>,
    budget: Arc<dyn AllocationBudget>,
    lose_next_commit_ack: Arc<AtomicBool>,
}

impl repository::SqlDatabase for StoreBridge {
    type Transaction = StoreTransaction;
    fn begin(&mut self) -> Result<Self::Transaction, AdapterError> {
        let native = self
            .runtime
            .block_on(
                self.store
                    .sql_begin(&self.cx, self.gate.clone(), self.budget.clone()),
            )
            .map_err(classify)?;
        Ok(StoreTransaction {
            runtime: self.runtime.clone(),
            gate: self.gate.clone(),
            cx: self.cx.clone(),
            native,
            budget: self.budget.clone(),
            lose_next_commit_ack: self.lose_next_commit_ack.clone(),
        })
    }
}

impl repository::SqlTransaction for StoreTransaction {
    fn exec(&mut self, sql: &str, params: &[RepoCell]) -> Result<u64, AdapterError> {
        let input = statement(sql, params, 0, &self.budget)?;
        let output = self
            .runtime
            .block_on(self.native.exec_typed(
                &self.cx,
                input,
                SqlResultPolicy::ForbidRows,
                self.gate.clone(),
                self.budget.clone(),
            ))
            .map_err(classify)?;
        let metadata = output
            .value()
            .get_key("affected-rows")
            .ok_or_else(invalid)?;
        let value = metadata.as_str().ok_or_else(invalid)?;
        let count = value.parse::<u64>().map_err(|_| invalid())?;
        if count.to_string() != value {
            return Err(invalid());
        }
        Ok(count)
    }

    fn query(
        &mut self,
        sql: &str,
        params: &[RepoCell],
        max_rows: u32,
    ) -> Result<repository::SqlRows, AdapterError> {
        let input = statement(sql, params, max_rows, &self.budget)?;
        let output = self
            .runtime
            .block_on(self.native.query_typed(
                &self.cx,
                input,
                self.gate.clone(),
                self.budget.clone(),
            ))
            .map_err(classify)?;
        decode_rows(output.value())
    }

    fn commit(self) -> Result<(), AdapterError> {
        // Arm only the actual COMMIT acknowledgement, not BEGIN or an earlier
        // statement's outcome. The native row and real terminal path run first.
        let gate: Arc<dyn Gate> = if self.lose_next_commit_ack.swap(false, Ordering::AcqRel) {
            Arc::new(LostAcknowledgement {
                real: self.gate.clone(),
                armed: AtomicBool::new(true),
            })
        } else {
            self.gate.clone()
        };
        self.runtime
            .block_on(self.native.commit(&self.cx, gate))
            .map_err(classify)
    }

    fn rollback(self) -> Result<(), AdapterError> {
        self.runtime
            .block_on(self.native.rollback(&self.cx))
            .map_err(classify)
    }
}

fn invalid() -> AdapterError {
    AdapterError::new(AdapterKind::Invalid)
}

fn classify(error: StoreError) -> AdapterError {
    let kind = match error {
        StoreError::CommitUnknown => AdapterKind::CommitUnknown,
        StoreError::Conflict(_) => AdapterKind::Conflict,
        StoreError::Denied(_) => AdapterKind::Denied,
        StoreError::Invalid(_) | StoreError::Canon(_) => AdapterKind::Invalid,
        StoreError::Quota => AdapterKind::Limit,
        StoreError::Unavailable(_) => AdapterKind::Unavailable,
        StoreError::Gate(error) => match error.code() {
            "cancelled" => AdapterKind::Cancelled,
            "stale-epoch" | "denied" | "approval-required" => AdapterKind::Denied,
            "consumed" => AdapterKind::Conflict,
            "limit-exceeded" => AdapterKind::Limit,
            _ => AdapterKind::Unavailable,
        },
        StoreError::Sql(rusqlite::Error::SqliteFailure(error, _))
            if matches!(
                error.code,
                rusqlite::ErrorCode::DatabaseBusy
                    | rusqlite::ErrorCode::DatabaseLocked
                    | rusqlite::ErrorCode::ConstraintViolation
            ) =>
        {
            AdapterKind::Conflict
        }
        _ => AdapterKind::Internal,
    };
    AdapterError::new(kind)
}

fn statement(
    sql: &str,
    params: &[RepoCell],
    max_rows: u32,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<OwnedSqlStatement, AdapterError> {
    let params = params
        .iter()
        .map(|cell| {
            let (kind, value) = match cell {
                RepoCell::Null => ("null", CanonValue::Null),
                RepoCell::Integer(value) => ("integer", CanonValue::Str(value.to_string())),
                RepoCell::Text(value) => ("text", CanonValue::Str(value.clone())),
                RepoCell::Blob(bytes) => (
                    "blob",
                    CanonValue::Arr(
                        bytes
                            .iter()
                            .map(|byte| CanonValue::Int(i64::from(*byte)))
                            .collect(),
                    ),
                ),
            };
            CanonValue::Obj(BTreeMap::from([
                ("type".into(), CanonValue::Str(kind.into())),
                ("value".into(), value),
            ]))
        })
        .collect();
    let input = CanonValue::Obj(BTreeMap::from([
        ("statement".into(), CanonValue::Str(sql.into())),
        ("params".into(), CanonValue::Arr(params)),
        ("max-rows".into(), CanonValue::Int(i64::from(max_rows))),
    ]));
    let value = CoreValue::try_from_borrowed(&input, budget)
        .map_err(|_| AdapterError::new(AdapterKind::Limit))?;
    OwnedSqlStatement::from_value(value).map_err(classify)
}

fn decode_rows(value: &CoreValue) -> Result<repository::SqlRows, AdapterError> {
    let columns = value.get_key("columns").ok_or_else(invalid)?;
    let CoreRef::Arr(columns) = columns.inspect() else {
        return Err(invalid());
    };
    let columns = columns
        .iter()
        .map(|v| v.as_str().map(str::to_owned).ok_or_else(invalid))
        .collect::<Result<Vec<_>, _>>()?;
    let rows = value.get_key("rows").ok_or_else(invalid)?;
    let CoreRef::Arr(rows) = rows.inspect() else {
        return Err(invalid());
    };
    let rows = rows
        .iter()
        .map(|row| {
            let CoreRef::Arr(cells) = row.inspect() else {
                return Err(invalid());
            };
            cells
                .iter()
                .map(|cell| {
                    let kind = cell.get_key("type").ok_or_else(invalid)?;
                    let kind = kind.as_str().ok_or_else(invalid)?;
                    let payload = cell.get_key("value").ok_or_else(invalid)?;
                    match kind {
                        "null" if matches!(payload.inspect(), CoreRef::Null) => Ok(RepoCell::Null),
                        "text" => Ok(RepoCell::Text(
                            payload.as_str().ok_or_else(invalid)?.to_owned(),
                        )),
                        "integer" => {
                            let text = payload.as_str().ok_or_else(invalid)?;
                            let integer = text.parse::<i64>().map_err(|_| invalid())?;
                            if integer.to_string() != text {
                                return Err(invalid());
                            }
                            Ok(RepoCell::Integer(integer))
                        }
                        "blob" => {
                            let CoreRef::Arr(bytes) = payload.inspect() else {
                                return Err(invalid());
                            };
                            let bytes = bytes
                                .iter()
                                .map(|byte| {
                                    let CoreRef::Int(byte) = byte.inspect() else {
                                        return Err(invalid());
                                    };
                                    u8::try_from(byte).map_err(|_| invalid())
                                })
                                .collect::<Result<Vec<_>, _>>()?;
                            Ok(RepoCell::Blob(bytes))
                        }
                        _ => Err(invalid()),
                    }
                })
                .collect::<Result<Vec<_>, _>>()
        })
        .collect::<Result<Vec<_>, _>>()?;
    Ok(repository::SqlRows { columns, rows })
}

struct LostAcknowledgement {
    real: Arc<GateEngine>,
    armed: AtomicBool,
}
#[async_trait]
impl Gate for LostAcknowledgement {
    fn check_context(&self, cx: &CallCx) -> Result<(), GateError> {
        self.real.check_context(cx)
    }
    async fn authorize(
        &self,
        cx: &CallCx,
        effect: EffectRequest,
        args: CoreDigest,
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
            Err(GateError::Persistence(
                "fixture lost COMMIT acknowledgement".into(),
            ))
        } else {
            Ok(())
        }
    }
}

fn identity() -> repository::RunIdentity {
    let digest = repository::Digest::hash(b"public-native-run-request");
    repository::RunIdentity {
        run_id: digest.as_str().into(),
        run_request_digest: digest,
    }
}

fn claim(phase: &str, epoch: u32) -> repository::GenerationClaim {
    let identity = identity();
    repository::GenerationClaim {
        run_id: identity.run_id,
        run_request_digest: identity.run_request_digest,
        phase: phase.into(),
        generation_key: format!("{phase}@0:0#{epoch}"),
        attempt_id: format!("{phase}@0:0#{epoch}:attempt"),
        request_digest: repository::Digest::hash(
            format!("public-request-{phase}-{epoch}").as_bytes(),
        ),
        route_digest: repository::Digest::hash(b"unchanged-public-route"),
    }
}

fn resolution(
    claim: &repository::GenerationClaim,
    disposition: repository::ResolutionDisposition,
    expected_progress: Option<repository::ProgressStamp>,
) -> repository::Resolution {
    repository::Resolution {
        claim: claim.clone(),
        disposition,
        expected_progress,
        receipt: br#"{"version":"native-public-receipt","cost_usd":"0.000001"}"#.to_vec(),
    }
}

fn counted_dispatch(
    counter: &AtomicUsize,
    permit: repository::DispatchPermit,
    claim: &repository::GenerationClaim,
) -> Result<(), RepoError> {
    permit.consume(claim)?;
    counter.fetch_add(1, Ordering::AcqRel);
    Ok(())
}

#[test]
fn native_parallel_repositories_mint_one_provider_permit_from_the_same_head() {
    let f = Fixture::new();
    let mut first = f.repo();
    let head = first
        .create_fresh(&identity(), b"fresh-public-snapshot")
        .unwrap();
    let second = f.repo();
    let barrier = Arc::new(Barrier::new(2));
    let attempt = claim("goal_intake", 0);
    let run = |mut repo: Repo| {
        let head = head.clone();
        let attempt = attempt.clone();
        let barrier = barrier.clone();
        let counter = f.provider_calls.clone();
        std::thread::spawn(move || {
            barrier.wait();
            match repo.claim_generation(&head, b"pending-public-snapshot", &attempt) {
                Ok((next, permit)) => {
                    counted_dispatch(&counter, permit, &attempt).unwrap();
                    Some(next)
                }
                Err(error) => {
                    assert!(matches!(
                        error,
                        RepoError::Conflict | RepoError::PendingGeneration
                    ));
                    None
                }
            }
        })
    };
    let left = run(first);
    let right = run(second);
    let heads = [left.join().unwrap(), right.join().unwrap()];
    assert_eq!(heads.iter().filter(|x| x.is_some()).count(), 1);
    assert_eq!(f.provider_calls.load(Ordering::Acquire), 1);
    let loaded = f.repo().load(&identity().run_id).unwrap().unwrap();
    assert_eq!(loaded.head.revision, 1);
    assert_eq!(loaded.bytes, b"pending-public-snapshot");
}

#[test]
fn native_committed_but_unacknowledged_claim_and_readback_never_mint_a_permit() {
    let f = Fixture::new();
    let mut repo = f.repo();
    let initial = repo.create_fresh(&identity(), b"fresh").unwrap();
    let attempt = claim("goal_intake", 0);
    f.lose_next_commit_ack.store(true, Ordering::Release);
    assert!(matches!(
        repo.claim_generation(&initial, b"pending", &attempt),
        Err(RepoError::CommitUnknown)
    ));
    assert_eq!(f.provider_calls.load(Ordering::Acquire), 0);
    let mut independent = f.repo();
    let saved = independent.load(&identity().run_id).unwrap().unwrap();
    assert_eq!(saved.head.revision, 1);
    assert_eq!(saved.bytes, b"pending");
    assert!(matches!(
        independent.claim_generation(&saved.head, b"pending", &attempt),
        Err(RepoError::PendingGeneration)
    ));
    let changed_nonce = claim("goal_intake", 1);
    assert!(matches!(
        independent.claim_generation(&saved.head, b"another", &changed_nonce),
        Err(RepoError::PendingGeneration)
    ));
    assert_eq!(f.provider_calls.load(Ordering::Acquire), 0);
}

#[test]
fn native_stale_repository_head_cannot_publish_a_claim_or_snapshot() {
    let f = Fixture::new();
    let mut first = f.repo();
    let mut second = f.repo();
    let old = first.create_fresh(&identity(), b"old").unwrap();
    let new = first.commit_snapshot(&old, b"new", &[]).unwrap();
    assert!(matches!(
        second.commit_snapshot(&old, b"stale", &[]),
        Err(RepoError::Conflict)
    ));
    assert!(matches!(
        second.claim_generation(&old, b"stale-pending", &claim("goal_intake", 0)),
        Err(RepoError::Conflict)
    ));
    let saved = second.load(&identity().run_id).unwrap().unwrap();
    assert_eq!(saved.head, new);
    assert_eq!(saved.bytes, b"new");
    assert_eq!(f.provider_calls.load(Ordering::Acquire), 0);
}

#[test]
fn native_progress_cas_blocks_stale_resolution_and_frozen_attempt_late_writes() {
    let f = Fixture::new();
    let mut a = f.repo();
    let mut b = f.repo();
    let initial = a.create_fresh(&identity(), b"fresh").unwrap();
    let attempt = claim("goal_intake", 0);
    let (head, permit) = a.claim_generation(&initial, b"pending", &attempt).unwrap();
    f.dispatch(permit, &attempt);
    let first = a
        .save_progress(&head, &attempt, 1, b"public-prefix-1")
        .unwrap();
    let latest = a
        .save_progress(&head, &attempt, 2, b"public-prefix-1-and-2")
        .unwrap();
    assert!(matches!(
        b.save_progress(&head, &attempt, 1, b"stale"),
        Err(RepoError::Conflict)
    ));
    let progress = b.load_progress(&head, &attempt).unwrap().unwrap();
    assert_eq!(progress.stamp, latest);
    assert_eq!(progress.bytes, b"public-prefix-1-and-2");
    let stale = resolution(
        &attempt,
        repository::ResolutionDisposition::AcceptedCompletion,
        Some(first),
    );
    assert!(matches!(
        b.commit_snapshot(&head, b"must-not-complete", &[stale]),
        Err(RepoError::Conflict)
    ));
    let absent = resolution(
        &attempt,
        repository::ResolutionDisposition::AcceptedCompletion,
        None,
    );
    assert!(matches!(
        b.commit_snapshot(&head, b"must-not-complete", &[absent]),
        Err(RepoError::Conflict)
    ));
    assert!(
        b.read_resolution(&head.run_id, &attempt.attempt_id)
            .unwrap()
            .is_none()
    );
    assert_eq!(b.load(&head.run_id).unwrap().unwrap().head, head);
    let settled = resolution(
        &attempt,
        repository::ResolutionDisposition::AcceptedCompletion,
        Some(latest),
    );
    let completed = a
        .commit_snapshot(&head, b"completed", &[settled.clone()])
        .unwrap();
    // Use the current head too: the resolution itself, not only stale Run CAS,
    // must freeze this attempt's public progress.
    assert!(matches!(
        b.save_progress(&completed, &attempt, 3, b"late"),
        Err(RepoError::Conflict)
    ));
    assert_eq!(
        b.load_progress(&completed, &attempt)
            .unwrap()
            .unwrap()
            .bytes,
        b"public-prefix-1-and-2"
    );
    assert_eq!(
        b.read_resolution(&head.run_id, &attempt.attempt_id)
            .unwrap()
            .unwrap()
            .resolution,
        settled
    );
    assert_eq!(f.provider_calls.load(Ordering::Acquire), 1);
}

#[test]
fn native_settlement_is_unique_and_next_normal_phase_requires_a_new_claim() {
    let f = Fixture::new();
    let mut repo = f.repo();
    let initial = repo.create_fresh(&identity(), b"fresh").unwrap();
    let first = claim("goal_intake", 0);
    let (pending, permit) = repo
        .claim_generation(&initial, b"pending-first", &first)
        .unwrap();
    f.dispatch(permit, &first);
    let receipt = resolution(
        &first,
        repository::ResolutionDisposition::AcceptedCompletion,
        None,
    );
    let done = repo
        .commit_snapshot(&pending, b"first-completed", &[receipt.clone()])
        .unwrap();
    assert!(
        repo.commit_snapshot(&done, b"duplicate-receipt", &[receipt.clone()])
            .is_err()
    );
    assert_eq!(repo.load(&done.run_id).unwrap().unwrap().head, done);
    assert_eq!(
        repo.read_resolution(&done.run_id, &first.attempt_id)
            .unwrap()
            .unwrap()
            .digest,
        receipt.digest()
    );
    let second = claim("goal_contract", 0);
    let (_, permit) = repo
        .claim_generation(&done, b"pending-second", &second)
        .unwrap();
    f.dispatch(permit, &second);
    assert_eq!(f.provider_calls.load(Ordering::Acquire), 2);
}

#[test]
fn native_unavailable_resolution_allows_one_explicit_successor_only() {
    let f = Fixture::new();
    let mut repo = f.repo();
    let fresh = repo.create_fresh(&identity(), b"fresh").unwrap();
    let old = claim("goal_intake", 0);
    let (pending, permit) = repo.claim_generation(&fresh, b"pending", &old).unwrap();
    f.dispatch(permit, &old);
    let unavailable = resolution(
        &old,
        repository::ResolutionDisposition::TerminalContentUnavailable,
        None,
    );
    let resolved = repo
        .commit_snapshot(&pending, b"unavailable", &[unavailable.clone()])
        .unwrap();
    let successor = claim("goal_intake", 1);
    assert!(matches!(
        repo.claim_generation(&resolved, b"implicit-retry", &successor),
        Err(RepoError::PendingGeneration)
    ));
    let authorization = repository::SuccessorAuthorization {
        old_attempt_id: old.attempt_id.clone(),
        resolution_digest: unavailable.digest(),
        receipt: b"explicit-fixture-action-1".to_vec(),
    };
    let (new_head, permit) = repo
        .claim_successor(&resolved, b"successor-pending", &authorization, &successor)
        .unwrap();
    f.dispatch(permit, &successor);
    let different_nonce = repository::SuccessorAuthorization {
        receipt: b"different-action-2".to_vec(),
        ..authorization
    };
    let later = claim("goal_intake", 2);
    assert!(
        repo.claim_successor(&new_head, b"duplicate-successor", &different_nonce, &later)
            .is_err()
    );
    let settled = resolution(
        &successor,
        repository::ResolutionDisposition::AcceptedCompletion,
        None,
    );
    let completed = repo
        .commit_snapshot(&new_head, b"successor-done", &[settled])
        .unwrap();
    assert!(
        repo.claim_successor(
            &completed,
            b"duplicate-after-done",
            &different_nonce,
            &later
        )
        .is_err()
    );
    assert_eq!(
        repo.load(&completed.run_id).unwrap().unwrap().head,
        completed
    );
    assert_eq!(f.provider_calls.load(Ordering::Acquire), 2);
}

#[test]
fn native_chunked_snapshot_and_resolution_exceed_one_mib_without_an_oversized_cell() {
    let f = Fixture::new();
    let mut repo = f.repo();
    let bytes = vec![b'a'; 1024 * 1024 + repository::CHUNK_BYTES + 7];
    let head = repo.create_fresh(&identity(), &bytes).unwrap();
    let loaded = repo.load(&head.run_id).unwrap().unwrap();
    assert_eq!(loaded.head, head);
    assert_eq!(loaded.bytes, bytes);
    let attempt = claim("goal_intake", 0);
    let (pending, permit) = repo.claim_generation(&head, b"pending", &attempt).unwrap();
    f.dispatch(permit, &attempt);
    let mut receipt = resolution(
        &attempt,
        repository::ResolutionDisposition::AcceptedCompletion,
        None,
    );
    receipt.receipt = vec![b'b'; 1024 * 1024 + 19];
    let settled = repo
        .commit_snapshot(&pending, b"large-receipt-done", &[receipt.clone()])
        .unwrap();
    let saved = repo
        .read_resolution(&settled.run_id, &attempt.attempt_id)
        .unwrap()
        .unwrap();
    assert_eq!(saved.resolution, receipt);
    assert_eq!(saved.digest, receipt.digest());
    assert_eq!(f.provider_calls.load(Ordering::Acquire), 1);
}

#[test]
fn native_repository_stale_epoch_cannot_claim_and_fresh_context_cannot_remint_unknown_work() {
    let f = Fixture::new();
    let mut old = f.repo();
    let head = old.create_fresh(&identity(), b"fresh").unwrap();
    f.gate.revoke_epoch(&f.profile).unwrap();
    assert!(
        old.claim_generation(&head, b"stale-pending", &claim("goal_intake", 0))
            .is_err()
    );
    assert_eq!(f.provider_calls.load(Ordering::Acquire), 0);
    let fresh_cx =
        f.cx.clone()
            .with_security_epoch(f.gate.epoch(&f.profile).unwrap());
    let mut current = Repo::open(f.bridge_with_context(fresh_cx)).unwrap();
    assert_eq!(current.load(&head.run_id).unwrap().unwrap().head, head);
    let attempt = claim("goal_intake", 0);
    let (pending, permit) = current
        .claim_generation(&head, b"current-pending", &attempt)
        .unwrap();
    f.dispatch(permit, &attempt);
    assert!(matches!(
        current.claim_generation(&pending, b"readback-retry", &attempt),
        Err(RepoError::PendingGeneration)
    ));
    assert_eq!(f.provider_calls.load(Ordering::Acquire), 1);
}
