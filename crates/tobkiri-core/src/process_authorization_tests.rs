//! Real Gate/Approvals/AuditLog consent with real retained catalog descriptors.
//! Fixture files are never executed. No grant, permission row or permit is
//! fabricated, and no test consumes a process grant.

use super::*;
use std::{
    collections::{BTreeMap, BTreeSet},
    fs::File,
    os::unix::fs::PermissionsExt,
    sync::{
        Mutex,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};
use tobkiri_canon::{AllocationBudget, AllocationRequest, Digest, ids::*};
use tobkiri_gate::{
    ApprovalAnswer, ApprovalPrompt, ApprovalStore, AuditEvent, AuditSink, CancellationToken,
    CoreControl, CoreControlLimits, GateEngine, MediatorChain, PackPolicy, PermissionSet,
    Principal, PtyDimensions, ResourceLimits, Risk, TerminalDisposition, TrustedPrompt,
};
use tobkiri_store::{Approvals, AuditLog, AuditQuota, DataRoot};

const PROGRAM: &str = "tool.snapshot";
const EXECUTABLE: &[u8] = b"catalog fixture, never executed";
const ARG: &str = "PRIVATE_ARG_MARKER";
const ENV_NAME: &str = "PRIVATE_ENV_NAME";
const ENV_VALUE: &str = "PRIVATE_ENV_VALUE";

fn context_limits() -> ResourceLimits {
    ResourceLimits {
        fuel_per_call: 1_000_000,
        memory_mib: 64,
        wall_ms: 30_000,
        table_elements: 10_000,
        output_bytes: 4 * 1024 * 1024,
    }
}

fn launch_limits() -> ResourceLimits {
    ResourceLimits {
        fuel_per_call: 1_000,
        memory_mib: 16,
        wall_ms: 1_000,
        table_elements: 128,
        output_bytes: 1_024,
    }
}

#[derive(Clone, Copy)]
enum PromptMode {
    Approve,
    Deny,
    Wait,
}

struct Prompt {
    mode: Mutex<PromptMode>,
    prompts: Mutex<Vec<ApprovalPrompt>>,
    entered: tokio::sync::Notify,
}
impl Prompt {
    fn new() -> Self {
        Self {
            mode: Mutex::new(PromptMode::Approve),
            prompts: Mutex::new(Vec::new()),
            entered: tokio::sync::Notify::new(),
        }
    }
    fn calls(&self) -> usize {
        self.prompts.lock().unwrap().len()
    }
    fn last(&self) -> ApprovalPrompt {
        self.prompts.lock().unwrap().last().unwrap().clone()
    }
    fn set(&self, mode: PromptMode) {
        *self.mode.lock().unwrap() = mode;
    }
}
#[async_trait::async_trait]
impl TrustedPrompt for Prompt {
    async fn prompt_approval(&self, prompt: ApprovalPrompt) -> ApprovalAnswer {
        self.prompts.lock().unwrap().push(prompt);
        let mode = *self.mode.lock().unwrap();
        match mode {
            PromptMode::Approve => ApprovalAnswer::Approve,
            PromptMode::Deny => ApprovalAnswer::Deny,
            PromptMode::Wait => {
                self.entered.notify_one();
                std::future::pending().await
            }
        }
    }
}

#[derive(Default)]
struct PayloadBudget(AtomicU64);
impl PayloadBudget {
    fn used(&self) -> u64 {
        self.0.load(Ordering::SeqCst)
    }
}
impl AllocationBudget for PayloadBudget {
    fn reserve(&self, request: AllocationRequest) -> Result<(), AllocationError> {
        self.0
            .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |used| {
                used.checked_add(request.bytes)
                    .filter(|next| *next <= 1024 * 1024)
            })
            .map(|_| ())
            .map_err(|_| AllocationError::Limit)
    }
    fn release(&self, bytes: u64) {
        assert!(self.0.fetch_sub(bytes, Ordering::SeqCst) >= bytes);
    }
}

/// Observe the snapshot's real charge at terminal delivery, while forwarding
/// every actual append and acknowledgement through the normal durable log.
struct BudgetObservingAudit {
    log: AuditLog,
    budget: Arc<PayloadBudget>,
    terminal_payload: AtomicU64,
}
impl AuditSink for BudgetObservingAudit {
    fn append(&self, event: AuditEvent) -> Result<(), GateError> {
        self.log.append(event)
    }
    fn append_idempotent(&self, id: &Digest, event: AuditEvent) -> Result<(), GateError> {
        self.terminal_payload
            .store(self.budget.used(), Ordering::SeqCst);
        self.log.append_idempotent(id, event)
    }
}

struct Fixture {
    catalog: ProcessCatalog,
    gate: Arc<GateEngine>,
    approvals: Arc<Approvals>,
    audit: Arc<BudgetObservingAudit>,
    prompt: Arc<Prompt>,
    cx: CallCx,
    control: CoreControl,
    budget: Arc<PayloadBudget>,
    _root: DataRoot,
    _temp: tempfile::TempDir,
}
impl Fixture {
    async fn new(allowed: bool, limits: CoreControlLimits) -> Self {
        let temp = tempfile::tempdir().unwrap();
        std::fs::set_permissions(temp.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
        let control = CoreControl::new(limits).unwrap();
        let catalog = ProcessCatalog::new(control.clone()).unwrap();
        let executable = temp.path().join("executable");
        std::fs::write(&executable, EXECUTABLE).unwrap();
        std::fs::set_permissions(&executable, std::fs::Permissions::from_mode(0o755)).unwrap();
        let cwd = temp.path().join("cwd");
        std::fs::create_dir(&cwd).unwrap();
        catalog
            .register(
                PROGRAM,
                File::open(executable).unwrap(),
                &Digest::hash(EXECUTABLE),
                "Fixture tool",
                File::open(cwd).unwrap(),
                "Fixture workspace",
            )
            .unwrap();
        let root = DataRoot::open(temp.path().join("data")).unwrap();
        let profile = ProfileId::parse("prof_process_auth").unwrap();
        let approvals = Arc::new(Approvals::open(&root, &profile).unwrap());
        let budget = Arc::new(PayloadBudget::default());
        let audit = Arc::new(BudgetObservingAudit {
            log: AuditLog::open(&root, &profile, AuditQuota::default()).unwrap(),
            budget: budget.clone(),
            terminal_payload: AtomicU64::new(0),
        });
        let prompt = Arc::new(Prompt::new());
        let gate = Arc::new(GateEngine::new_with_control(
            approvals.clone(),
            audit.clone(),
            prompt.clone(),
            MediatorChain::default(),
            control.clone(),
        ));
        let pack = PackId::parse("fixture.process-auth").unwrap();
        let hash = PackHash::new(Digest::hash(b"fixture process authorization pack"));
        let revision = Digest::hash(b"fixture process authorization revision");
        let permissions = PermissionSet {
            proc: if allowed {
                BTreeSet::from([PROGRAM.to_owned()])
            } else {
                BTreeSet::new()
            },
            pty: true,
            wasm: context_limits(),
            ..PermissionSet::default()
        };
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
        gate.register_revision(
            &profile,
            &revision,
            BTreeMap::from([((pack.clone(), hash.clone()), context_limits())]),
        )
        .unwrap();
        let context = |principal, request| {
            CallCx::try_root_with_control(
                profile.clone(),
                principal,
                RequestId::parse(request).unwrap(),
                CancellationToken::new_with_control(&control).unwrap(),
                Instant::now() + Duration::from_secs(60),
                context_limits(),
                control.clone(),
            )
            .unwrap()
            .with_security_epoch(gate.epoch(&profile).unwrap())
        };
        // The ordinary trusted permission approval installs all real policy
        // rows. Subsequent process approval is a separate per-effect prompt.
        gate.approve_pack_permissions(&context(Principal::User, "req_install"), &pack, None, &hash)
            .await
            .unwrap();
        let cx = context(Principal::Pack { pack, hash }, "req_process").with_revision(revision);
        Self {
            catalog,
            gate,
            approvals,
            audit,
            prompt,
            cx,
            control,
            budget,
            _root: root,
            _temp: temp,
        }
    }

    fn snapshot(&self, pty: bool, arg: &str) -> PreparedProcessSnapshot {
        let budget: Arc<dyn AllocationBudget> = self.budget.clone();
        self.catalog
            .prepare_snapshot(
                PROGRAM,
                &[arg.to_owned()],
                &[(ENV_NAME.to_owned(), ENV_VALUE.to_owned())],
                &launch_limits(),
                pty.then(|| PtyDimensions::new(80, 24).unwrap()),
                &budget,
            )
            .unwrap()
    }

    fn assert_abandoned(&self, id: &tobkiri_gate::GrantId, args: &Digest) {
        let terminal = self.approvals.terminal_delivery(id).unwrap().unwrap();
        assert!(terminal.acknowledged);
        assert_eq!(
            terminal.decision.disposition,
            TerminalDisposition::AbandonedBeforeUse
        );
        assert_eq!(&terminal.decision.reservation.args, args);
        assert_eq!(&terminal.decision.reservation.id, id);
    }
}

#[tokio::test]
async fn real_proc_and_pty_approval_keep_exact_unconsumed_custody_until_drop() {
    for pty in [false, true] {
        let fixture = Fixture::new(true, CoreControlLimits::default()).await;
        let baseline = fixture.control.usage();
        let prompts = fixture.prompt.calls();
        let snapshot = fixture.snapshot(pty, ARG);
        let args = snapshot.binding().args_digest().clone();
        let payload_bytes = fixture.budget.used();
        let gate: Arc<dyn Gate> = fixture.gate.clone();
        let capsule = AuthorizedProcessLaunch::authorize(
            &fixture.catalog,
            snapshot,
            fixture.cx.clone(),
            gate.clone(),
        )
        .await
        .unwrap();
        assert!(grant_matches(capsule.snapshot.binding(), &capsule.grant));
        assert!(Arc::ptr_eq(&capsule.gate, &gate));
        assert_eq!(capsule.cx.profile(), fixture.cx.profile());
        assert_eq!(capsule.cx.principal(), fixture.cx.principal());
        assert_eq!(capsule.cx.request(), fixture.cx.request());
        assert_eq!(capsule.cx.revision(), fixture.cx.revision());
        assert_eq!(capsule.cx.security_epoch(), fixture.cx.security_epoch());
        assert_eq!(capsule.cx.deadline(), fixture.cx.deadline());
        assert!(capsule.cx.cancel().inherits(fixture.cx.cancel()));
        assert_eq!(fixture.catalog.verify_snapshot(&capsule.snapshot), Ok(()));
        assert_eq!(fixture.prompt.calls(), prompts + 1);
        let prompt = fixture.prompt.last();
        assert_eq!(prompt.risk, Risk::Dangerous);
        assert_eq!(prompt.args_digest, args);
        assert_eq!(&prompt.effect, capsule.grant.effect());
        assert_eq!(capsule.grant.limits(), &launch_limits());
        let id = capsule.grant.id().clone();
        assert_eq!(
            fixture.approvals.reservation_state(&id).unwrap().as_deref(),
            Some("dispatched")
        );
        assert!(fixture.approvals.terminal_delivery(&id).unwrap().is_none());
        assert_eq!(fixture.budget.used(), payload_bytes);
        let redacted = format!("{capsule:?} {}", prompt.summary);
        for secret in [ARG, ENV_NAME, ENV_VALUE] {
            assert!(!redacted.contains(secret));
        }
        // No consume or replacement authority is introduced by moving custody.
        let moved = capsule;
        assert_eq!(moved.grant.id(), &id);
        assert_eq!(fixture.prompt.calls(), prompts + 1);
        drop(moved);
        fixture.assert_abandoned(&id, &args);
        assert_eq!(
            fixture.audit.terminal_payload.load(Ordering::SeqCst),
            payload_bytes
        );
        assert_eq!(fixture.budget.used(), 0);
        assert_eq!(fixture.control.usage(), baseline);
    }
}

#[tokio::test]
async fn missing_policy_denies_before_process_prompt_and_releases_snapshot() {
    let fixture = Fixture::new(false, CoreControlLimits::default()).await;
    let baseline = fixture.control.usage();
    let prompts = fixture.prompt.calls();
    let watermark = fixture
        .approvals
        .terminal_watermark(fixture.cx.profile())
        .unwrap();
    let result = AuthorizedProcessLaunch::authorize(
        &fixture.catalog,
        fixture.snapshot(false, ARG),
        fixture.cx.clone(),
        fixture.gate.clone(),
    )
    .await;
    assert!(matches!(
        result,
        Err(ProcessAuthorizationError::Gate(GateError::Denied(_)))
    ));
    assert_eq!(fixture.prompt.calls(), prompts);
    assert_eq!(
        fixture
            .approvals
            .terminal_watermark(fixture.cx.profile())
            .unwrap(),
        watermark
    );
    assert_eq!(fixture.budget.used(), 0);
    assert_eq!(fixture.control.usage(), baseline);
}

#[tokio::test]
async fn native_consent_denial_uses_original_abandonment_and_releases_payload() {
    let fixture = Fixture::new(true, CoreControlLimits::default()).await;
    let baseline = fixture.control.usage();
    fixture.prompt.set(PromptMode::Deny);
    let snapshot = fixture.snapshot(false, ARG);
    let args = snapshot.binding().args_digest().clone();
    let result = AuthorizedProcessLaunch::authorize(
        &fixture.catalog,
        snapshot,
        fixture.cx.clone(),
        fixture.gate.clone(),
    )
    .await;
    assert!(matches!(
        result,
        Err(ProcessAuthorizationError::Gate(GateError::Denied(_)))
    ));
    fixture.assert_abandoned(&fixture.prompt.last().reservation, &args);
    assert_eq!(fixture.budget.used(), 0);
    assert_eq!(fixture.control.usage(), baseline);
}

#[tokio::test]
async fn foreign_catalog_verification_fails_before_request_metadata_or_prompt() {
    let fixture = Fixture::new(true, CoreControlLimits::default()).await;
    let foreign = ProcessCatalog::new(fixture.control.clone()).unwrap();
    let baseline = fixture.control.usage();
    let prompts = fixture.prompt.calls();
    let result = AuthorizedProcessLaunch::authorize(
        &foreign,
        fixture.snapshot(false, ARG),
        fixture.cx.clone(),
        fixture.gate.clone(),
    )
    .await;
    assert!(matches!(
        result,
        Err(ProcessAuthorizationError::Snapshot(
            LaunchBindingError::Mismatch
        ))
    ));
    assert_eq!(fixture.prompt.calls(), prompts);
    assert_eq!(fixture.budget.used(), 0);
    assert_eq!(fixture.control.usage(), baseline);
}

#[tokio::test]
async fn request_metadata_capacity_failure_precedes_copy_and_consent() {
    let fixture = Fixture::new(
        true,
        CoreControlLimits {
            max_frames: 0,
            ..CoreControlLimits::default()
        },
    )
    .await;
    let baseline = fixture.control.usage();
    let prompts = fixture.prompt.calls();
    let result = AuthorizedProcessLaunch::authorize(
        &fixture.catalog,
        fixture.snapshot(false, ARG),
        fixture.cx.clone(),
        fixture.gate.clone(),
    )
    .await;
    assert!(matches!(result, Err(ProcessAuthorizationError::Capacity)));
    assert_eq!(fixture.prompt.calls(), prompts);
    assert_eq!(fixture.budget.used(), 0);
    assert_eq!(fixture.control.usage(), baseline);
}

#[tokio::test]
async fn capsule_metadata_failure_after_real_approval_abandons_that_same_grant() {
    let fixture = Fixture::new(
        true,
        CoreControlLimits {
            max_frames: 1,
            ..CoreControlLimits::default()
        },
    )
    .await;
    let baseline = fixture.control.usage();
    let prompts = fixture.prompt.calls();
    let snapshot = fixture.snapshot(false, ARG);
    let args = snapshot.binding().args_digest().clone();
    let result = AuthorizedProcessLaunch::authorize(
        &fixture.catalog,
        snapshot,
        fixture.cx.clone(),
        fixture.gate.clone(),
    )
    .await;
    assert!(matches!(result, Err(ProcessAuthorizationError::Capacity)));
    assert_eq!(fixture.prompt.calls(), prompts + 1);
    fixture.assert_abandoned(&fixture.prompt.last().reservation, &args);
    assert_eq!(fixture.budget.used(), 0);
    assert_eq!(fixture.control.usage(), baseline);
}

#[tokio::test]
async fn private_adoption_rejects_a_genuine_grant_for_different_snapshot_args() {
    let fixture = Fixture::new(true, CoreControlLimits::default()).await;
    let baseline = fixture.control.usage();
    let snapshot = fixture.snapshot(false, ARG);
    let other = fixture.snapshot(false, "OTHER_PRIVATE_ARG");
    let args = other.binding().args_digest().clone();
    let request_metadata = fixture.control.reserve(ControlKind::Frame, 4096).unwrap();
    let request = launch_request(other.binding()).unwrap();
    let grant = fixture
        .gate
        .authorize_process_launch(&fixture.cx, request, other.binding())
        .await
        .unwrap();
    let id = grant.id().clone();
    drop(other);
    let result = AuthorizedProcessLaunch::from_approved(
        snapshot,
        grant,
        fixture.cx.clone(),
        fixture.gate.clone(),
        request_metadata,
    );
    assert!(matches!(
        result,
        Err(ProcessAuthorizationError::GrantMismatch)
    ));
    fixture.assert_abandoned(&id, &args);
    assert_eq!(fixture.budget.used(), 0);
    assert_eq!(fixture.control.usage(), baseline);
}

#[tokio::test]
async fn dropping_pending_authorization_releases_snapshot_and_original_pending_grant() {
    let fixture = Fixture::new(true, CoreControlLimits::default()).await;
    let baseline = fixture.control.usage();
    fixture.prompt.set(PromptMode::Wait);
    let snapshot = fixture.snapshot(false, ARG);
    let args = snapshot.binding().args_digest().clone();
    let mut pending = Box::pin(AuthorizedProcessLaunch::authorize(
        &fixture.catalog,
        snapshot,
        fixture.cx.clone(),
        fixture.gate.clone(),
    ));
    tokio::time::timeout(Duration::from_secs(2), async {
        tokio::select! {
            result = pending.as_mut() => panic!("approval unexpectedly completed: {result:?}"),
            _ = fixture.prompt.entered.notified() => {}
        }
    })
    .await
    .unwrap();
    let id = fixture.prompt.last().reservation;
    assert!(fixture.budget.used() > 0);
    drop(pending);
    fixture.assert_abandoned(&id, &args);
    assert_eq!(fixture.budget.used(), 0);
    assert_eq!(fixture.control.usage(), baseline);
}

#[tokio::test]
async fn cancellation_during_real_prompt_preserves_original_terminal_identity() {
    let fixture = Fixture::new(true, CoreControlLimits::default()).await;
    let baseline = fixture.control.usage();
    fixture.prompt.set(PromptMode::Wait);
    let snapshot = fixture.snapshot(false, ARG);
    let args = snapshot.binding().args_digest().clone();
    let mut pending = Box::pin(AuthorizedProcessLaunch::authorize(
        &fixture.catalog,
        snapshot,
        fixture.cx.clone(),
        fixture.gate.clone(),
    ));
    tokio::time::timeout(Duration::from_secs(2), async {
        tokio::select! {
            result = pending.as_mut() => panic!("approval unexpectedly completed: {result:?}"),
            _ = fixture.prompt.entered.notified() => {}
        }
    })
    .await
    .unwrap();
    let id = fixture.prompt.last().reservation;
    fixture.cx.cancel().cancel();
    let result = tokio::time::timeout(Duration::from_secs(2), pending)
        .await
        .unwrap();
    assert!(matches!(
        result,
        Err(ProcessAuthorizationError::Gate(GateError::Cancelled))
    ));
    fixture.assert_abandoned(&id, &args);
    assert_eq!(fixture.budget.used(), 0);
    assert_eq!(fixture.control.usage(), baseline);
}
