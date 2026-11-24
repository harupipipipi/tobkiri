//! Real Gate/Store integration for committed native process launches
//! (issue1520 / Astra Q48, Q49, Q53).
//!
//! The physical Proc/PTY backend remains unavailable: nothing here spawns a
//! process, opens a PTY, looks up an executable/cwd path or fabricates an
//! FD/current-frame proof. Fixture `NativeIdentity` payloads are supplied
//! test facts only. Every authorization goes through a real `GateEngine`
//! backed by real durable `Approvals`/`AuditLog` rows, ordinary
//! `register_pack`/`register_revision` and normal `approve_pack_permissions`
//! installation — never hand-written grants, permits, reservations or
//! permission rows.

use async_trait::async_trait;
use std::{
    collections::{BTreeMap, BTreeSet},
    sync::{
        Arc, Mutex,
        atomic::{AtomicUsize, Ordering},
    },
    time::{Duration, Instant},
};
use tobkiri_canon::{CanonValue, Digest, Variant, ids::*};
use tobkiri_gate::*;
use tobkiri_store::*;

const PROGRAM: &str = "tool.build";
const OTHER_PROGRAM: &str = "tool.other";
const SECRET_ARGV: &str = "ARGV-S3CRET-MARKER";
const SECRET_ENV_NAME: &str = "S3CRET_ENV_NAME";
const SECRET_ENV_VALUE: &str = "ENV-S3CRET-MARKER";
const SECRET_EXE_FACT: &[u8] = b"EXE-NATIVE-MARKER";
const SECRET_CWD_FACT: &[u8] = b"CWD-NATIVE-MARKER";

fn secret_markers() -> [&'static str; 5] {
    [
        SECRET_ARGV,
        SECRET_ENV_NAME,
        SECRET_ENV_VALUE,
        std::str::from_utf8(SECRET_EXE_FACT).unwrap(),
        std::str::from_utf8(SECRET_CWD_FACT).unwrap(),
    ]
}

fn assert_no_markers(text: &str) {
    for marker in secret_markers() {
        assert!(!text.contains(marker), "secret marker leaked: {marker}");
    }
}

fn cx_limits() -> ResourceLimits {
    ResourceLimits {
        fuel_per_call: 1_000_000,
        memory_mib: 64,
        wall_ms: 30_000,
        table_elements: 10_000,
        output_bytes: 4 * 1024 * 1024,
    }
}

/// Committed launch limits strictly below the context/frozen budget.
fn launch_limits() -> ResourceLimits {
    ResourceLimits {
        fuel_per_call: 500_000,
        memory_mib: 32,
        wall_ms: 10_000,
        table_elements: 128,
        output_bytes: 1024 * 1024,
    }
}

fn launch_permissions(pty: bool) -> PermissionSet {
    PermissionSet {
        proc: BTreeSet::from([PROGRAM.to_string()]),
        pty,
        wasm: cx_limits(),
        ..Default::default()
    }
}

fn pack_policy(permissions: PermissionSet) -> PackPolicy {
    PackPolicy {
        declaration: permissions.clone(),
        ceiling: permissions.clone(),
        pack_limits: permissions,
    }
}

fn pack_pair(label: &str) -> (PackId, PackHash) {
    (
        PackId::parse(label).unwrap(),
        PackHash::new(Digest::hash(label.as_bytes())),
    )
}

fn launch_request(pty: bool, program: &str) -> EffectRequest {
    if pty {
        EffectRequest::Pty {
            program: program.into(),
        }
    } else {
        EffectRequest::Proc {
            program: program.into(),
        }
    }
}

/// Trusted prompt fixture: records the actual typed prompt for inspection
/// and answers only through the real `obtain_approval` path.
struct LaunchPrompt {
    calls: AtomicUsize,
    mode: Mutex<PromptMode>,
    risks: Mutex<Vec<Risk>>,
    summaries: Mutex<Vec<String>>,
    effects: Mutex<Vec<EffectRequest>>,
    reservations: Mutex<Vec<GrantId>>,
}
#[derive(Clone, Copy)]
enum PromptMode {
    Approve,
    Deny,
}
impl LaunchPrompt {
    fn new() -> Self {
        Self {
            calls: AtomicUsize::new(0),
            mode: Mutex::new(PromptMode::Approve),
            risks: Mutex::new(Vec::new()),
            summaries: Mutex::new(Vec::new()),
            effects: Mutex::new(Vec::new()),
            reservations: Mutex::new(Vec::new()),
        }
    }
    fn calls(&self) -> usize {
        self.calls.load(Ordering::SeqCst)
    }
    fn set_mode(&self, mode: PromptMode) {
        *self.mode.lock().unwrap() = mode;
    }
    fn last_risk(&self) -> Option<Risk> {
        self.risks.lock().unwrap().last().copied()
    }
    fn last_summary(&self) -> String {
        self.summaries
            .lock()
            .unwrap()
            .last()
            .cloned()
            .unwrap_or_default()
    }
    fn last_effect(&self) -> Option<EffectRequest> {
        self.effects.lock().unwrap().last().cloned()
    }
    fn last_reservation(&self) -> Option<GrantId> {
        self.reservations.lock().unwrap().last().cloned()
    }
}
#[async_trait]
impl TrustedPrompt for LaunchPrompt {
    async fn prompt_approval(&self, prompt: ApprovalPrompt) -> ApprovalAnswer {
        self.calls.fetch_add(1, Ordering::SeqCst);
        self.risks.lock().unwrap().push(prompt.risk);
        self.summaries.lock().unwrap().push(prompt.summary.clone());
        self.effects.lock().unwrap().push(prompt.effect);
        self.reservations
            .lock()
            .unwrap()
            .push(prompt.reservation.clone());
        match *self.mode.lock().unwrap() {
            PromptMode::Approve => ApprovalAnswer::Approve,
            PromptMode::Deny => ApprovalAnswer::Deny,
        }
    }
}

/// Mediator fixture: records every reviewed prompt verbatim and returns a
/// programmable verdict. A mediator is never native consent.
struct RecordingMediator {
    calls: AtomicUsize,
    prompts: Mutex<Vec<ApprovalPrompt>>,
    verdict: Mutex<Verdict>,
}
impl RecordingMediator {
    fn new(verdict: Verdict) -> Self {
        Self {
            calls: AtomicUsize::new(0),
            prompts: Mutex::new(Vec::new()),
            verdict: Mutex::new(verdict),
        }
    }
    fn calls(&self) -> usize {
        self.calls.load(Ordering::SeqCst)
    }
    fn set_verdict(&self, verdict: Verdict) {
        *self.verdict.lock().unwrap() = verdict;
    }
    fn last_prompt(&self) -> ApprovalPrompt {
        self.prompts.lock().unwrap().last().cloned().unwrap()
    }
}
#[async_trait]
impl Mediator for RecordingMediator {
    async fn review(&self, pending: &ApprovalPrompt) -> Verdict {
        self.calls.fetch_add(1, Ordering::SeqCst);
        self.prompts.lock().unwrap().push(pending.clone());
        self.verdict.lock().unwrap().clone()
    }
}

/// Counting sink delegating every append to the real durable `AuditLog`;
/// recorded events are the same bytes that reached durable storage.
struct CountingAudit {
    inner: Arc<AuditLog>,
    events: Mutex<Vec<AuditEvent>>,
}
impl CountingAudit {
    fn events(&self) -> Vec<AuditEvent> {
        self.events.lock().unwrap().clone()
    }
    /// Encoded durable view of everything this profile audited.
    fn encoded(&self) -> Vec<u8> {
        let mut bytes = Vec::new();
        for event in self.events() {
            bytes.extend_from_slice(event.kind.as_bytes());
            bytes.extend(tobkiri_canon::encode(&event.payload, Variant::StrictV1).unwrap());
        }
        bytes
    }
}
impl AuditSink for CountingAudit {
    fn append(&self, event: AuditEvent) -> Result<(), GateError> {
        self.inner.append(event.clone())?;
        self.events.lock().unwrap().push(event);
        Ok(())
    }
    fn append_idempotent(&self, event_id: &Digest, event: AuditEvent) -> Result<(), GateError> {
        self.inner.append_idempotent(event_id, event.clone())?;
        self.events.lock().unwrap().push(event);
        Ok(())
    }
}

fn payload_field(event: &AuditEvent, key: &str) -> Option<String> {
    let CanonValue::Obj(fields) = &event.payload else {
        return None;
    };
    match fields.get(key) {
        Some(CanonValue::Str(value)) => Some(value.clone()),
        _ => None,
    }
}

/// Owned fixture inputs behind one borrowing `ProcessLaunchSnapshot`. Secret
/// argv/env and the supplied native-identity facts are test data only.
struct LaunchCase {
    program: String,
    executable_digest: Digest,
    executable: NativeIdentity,
    argv: Vec<String>,
    env: Vec<(String, String)>,
    cwd: NativeIdentity,
    pty: Option<PtyDimensions>,
    limits: ResourceLimits,
    catalog_label: String,
    cwd_label: String,
}
impl LaunchCase {
    fn new(pty: Option<PtyDimensions>) -> Self {
        Self {
            program: PROGRAM.into(),
            executable_digest: Digest::hash(b"committed-executable"),
            executable: NativeIdentity::new("unix-fs", 1, SECRET_EXE_FACT).unwrap(),
            argv: vec!["tool".into(), SECRET_ARGV.into()],
            env: vec![(SECRET_ENV_NAME.into(), SECRET_ENV_VALUE.into())],
            cwd: NativeIdentity::new("unix-fs", 1, SECRET_CWD_FACT).unwrap(),
            pty,
            limits: launch_limits(),
            catalog_label: "Build tool".into(),
            cwd_label: "workspace".into(),
        }
    }
    fn snapshot(&self) -> ProcessLaunchSnapshot<'_> {
        ProcessLaunchSnapshot {
            program_id: &self.program,
            executable_digest: &self.executable_digest,
            executable_identity: &self.executable,
            argv: &self.argv,
            env: &self.env,
            cwd_identity: &self.cwd,
            pty: self.pty,
            limits: &self.limits,
            catalog_label: &self.catalog_label,
            cwd_label: &self.cwd_label,
        }
    }
    fn commit(&self, key: &ProcessLaunchKey) -> ProcessLaunchBinding {
        ProcessLaunchBinding::commit(key, &self.snapshot()).unwrap()
    }
}

struct Fixture {
    _temp: tempfile::TempDir,
    _root: DataRoot,
    profile: ProfileId,
    control: CoreControl,
    approvals: Arc<Approvals>,
    audit: Arc<CountingAudit>,
    prompt: Arc<LaunchPrompt>,
    gate: Arc<GateEngine>,
}
impl Fixture {
    /// One fresh private DataRoot and one fresh CoreControl shared by the
    /// GateEngine, every CallCx and every CancellationToken.
    fn new(mediators: MediatorChain) -> Self {
        let temp = tempfile::tempdir().unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(temp.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
        }
        let root = DataRoot::open(temp.path().canonicalize().unwrap()).unwrap();
        let profile = ProfileId::parse("prof_launch").unwrap();
        let approvals = Arc::new(Approvals::open(&root, &profile).unwrap());
        let audit = Arc::new(CountingAudit {
            inner: Arc::new(AuditLog::open(&root, &profile, AuditQuota::default()).unwrap()),
            events: Mutex::new(Vec::new()),
        });
        let prompt = Arc::new(LaunchPrompt::new());
        let control = CoreControl::default();
        let gate = Arc::new(GateEngine::new_with_control(
            approvals.clone(),
            audit.clone(),
            prompt.clone(),
            mediators,
            control.clone(),
        ));
        Self {
            _temp: temp,
            _root: root,
            profile,
            control,
            approvals,
            audit,
            prompt,
            gate,
        }
    }
    fn cx(&self, principal: Principal, limits: ResourceLimits) -> CallCx {
        CallCx::try_root_with_control(
            self.profile.clone(),
            principal,
            RequestId::parse("req_launch").unwrap(),
            CancellationToken::new_with_control(&self.control).unwrap(),
            Instant::now() + Duration::from_secs(60),
            limits,
            self.control.clone(),
        )
        .unwrap()
        .with_security_epoch(self.gate.epoch(&self.profile).unwrap())
    }
    fn pack_cx(&self, pack: &PackId, hash: &PackHash, revision: &Digest) -> CallCx {
        self.cx(
            Principal::Pack {
                pack: pack.clone(),
                hash: hash.clone(),
            },
            cx_limits(),
        )
        .with_revision(revision.clone())
    }
    /// Ordinary registration plus a real approval prompt install; no
    /// permission rows or grants are ever written by hand.
    async fn install(&self, pack: &PackId, hash: &PackHash, revision: &Digest, policy: PackPolicy) {
        self.gate
            .register_pack(&self.profile, pack, hash, policy)
            .unwrap();
        self.gate
            .register_revision(
                &self.profile,
                revision,
                BTreeMap::from([((pack.clone(), hash.clone()), cx_limits())]),
            )
            .unwrap();
        let user = self.cx(Principal::User, cx_limits());
        self.gate
            .approve_pack_permissions(&user, pack, None, hash)
            .await
            .unwrap();
    }
    fn watermark(&self) -> u64 {
        self.approvals.terminal_watermark(&self.profile).unwrap()
    }
}

/// Committed launches through the dedicated entry reach the real Dangerous
/// per-effect prompt, keep the full binding HMAC args digest and return the
/// exact committed limits; consumption and the terminal outcome flow through
/// the ordinary durable one-use path.
async fn committed_launch(pty: bool) {
    let fixture = Fixture::new(MediatorChain::default());
    let revision = Digest::hash(if pty {
        b"launch-revision-pty".as_slice()
    } else {
        b"launch-revision-proc".as_slice()
    });
    let (pack, hash) = pack_pair(if pty {
        "fixture.launch-pty"
    } else {
        "fixture.launch-proc"
    });
    fixture
        .install(
            &pack,
            &hash,
            &revision,
            pack_policy(launch_permissions(pty)),
        )
        .await;
    let baseline_watermark = fixture.watermark();
    let baseline_prompts = fixture.prompt.calls();
    assert_eq!(baseline_prompts, 1);

    let key = ProcessLaunchKey::generate().unwrap();
    let case = LaunchCase::new(pty.then(|| PtyDimensions::new(80, 24).unwrap()));
    let binding = case.commit(&key);
    let cx = fixture.pack_cx(&pack, &hash, &revision);
    let request = launch_request(pty, PROGRAM);

    let grant = fixture
        .gate
        .authorize_process_launch(&cx, request.clone(), &binding)
        .await
        .unwrap();
    assert_eq!(grant.effect(), &request);
    assert_eq!(grant.args_digest(), binding.args_digest());
    assert_eq!(grant.limits(), binding.limits());
    assert_ne!(grant.limits(), cx.limits());

    // The real Dangerous per-effect prompt ran exactly once with a bounded
    // redacted summary and the authoritative effect.
    assert_eq!(fixture.prompt.calls(), baseline_prompts + 1);
    assert_eq!(fixture.prompt.last_risk(), Some(Risk::Dangerous));
    let summary = fixture.prompt.last_summary();
    for expected in [
        PROGRAM,
        "Build tool",
        "workspace",
        "argv=2",
        "env=1",
        "DANGEROUS",
        "unsandboxed",
    ] {
        assert!(
            summary.contains(expected),
            "summary lacks {expected}: {summary}"
        );
    }
    if pty {
        assert!(summary.contains("pty=80x24"), "{summary}");
    } else {
        assert!(summary.contains("pty=none"), "{summary}");
    }
    let digest_text = binding.args_digest().hex().to_string();
    assert!(summary.contains(&digest_text), "{summary}");
    assert_no_markers(&summary);
    assert_eq!(fixture.prompt.last_effect().as_ref(), Some(&request));

    // The dispatch audit binds the regular effect digest and the full
    // binding args digest, both durable in the real Store rows.
    let dispatch = fixture
        .audit
        .events()
        .into_iter()
        .rev()
        .find(|event| event.kind == "dispatch")
        .unwrap();
    assert_eq!(
        payload_field(&dispatch, "args_digest").as_deref(),
        Some(binding.args_digest().to_string().as_str())
    );
    assert_eq!(
        payload_field(&dispatch, "effect_digest").as_deref(),
        Some(request.digest().unwrap().to_string().as_str())
    );

    // Ordinary owned consumption produces the real sealed permit.
    let permit = match grant.consume_owned(&cx, &request, binding.args_digest()) {
        ConsumptionAttempt::Consumed(attempt) => attempt.into_dispatch().unwrap(),
        other => panic!("launch grant was not consumed: {other:?}"),
    };
    assert_eq!(permit.effect(), &request);
    assert_eq!(permit.args_digest(), binding.args_digest());
    assert_eq!(permit.limits(), binding.limits());
    assert!(permit.matches_context(&cx));

    fixture
        .gate
        .record_outcome(permit.id().clone(), Outcome::Succeeded)
        .await
        .unwrap();
    let terminal = fixture
        .approvals
        .terminal_delivery(permit.id())
        .unwrap()
        .unwrap();
    assert!(terminal.acknowledged);
    assert_eq!(terminal.decision.outcome, Outcome::Succeeded);
    assert_eq!(
        terminal.decision.disposition,
        TerminalDisposition::Completed
    );
    assert_eq!(terminal.decision.reservation.args, *binding.args_digest());
    assert_eq!(
        terminal.decision.reservation.effect,
        request.digest().unwrap()
    );
    assert_eq!(fixture.watermark(), baseline_watermark + 1);

    let acknowledged = fixture.gate.acknowledged_terminal(&permit).unwrap();
    assert_eq!(acknowledged.outcome(), &Outcome::Succeeded);

    // The real one-use consumption is visible through the genuine read-only
    // snapshot keyed to this exact permit — no synthesized sealed permits.
    let mut inspected = 0usize;
    fixture
        .gate
        .with_cleanup_snapshot(&permit, &mut |snapshot| {
            inspected += 1;
            assert_eq!(&snapshot.authority.reservation.id, permit.id());
            assert_eq!(snapshot.authority.reservation.args, *binding.args_digest());
            assert_eq!(
                snapshot.authority.reservation.effect,
                request.digest().unwrap()
            );
            let terminal = snapshot.authority.terminal.unwrap();
            assert_eq!(terminal.decision.outcome, Outcome::Succeeded);
            Ok(())
        })
        .unwrap();
    assert_eq!(inspected, 1);

    let outcome = fixture
        .audit
        .events()
        .into_iter()
        .rev()
        .find(|event| event.kind == "outcome")
        .unwrap();
    assert_eq!(
        payload_field(&outcome, "args_digest").as_deref(),
        Some(binding.args_digest().as_str())
    );

    // No fixture secret reached any durable audit row.
    let encoded = fixture.audit.encoded();
    for marker in secret_markers() {
        assert!(
            !encoded
                .windows(marker.len())
                .any(|window| window == marker.as_bytes()),
            "audit leaked {marker}"
        );
    }
}

#[tokio::test]
async fn committed_proc_launch_reaches_dangerous_consent_and_consumes_once() {
    committed_launch(false).await;
}

#[tokio::test]
async fn committed_pty_launch_reaches_dangerous_consent_and_consumes_once() {
    committed_launch(true).await;
}

/// Bare requests through the generic entry, a nonlaunch request through the
/// dedicated entry, a program mismatch and both PTY-presence mismatches all
/// fail closed before any fresh reservation, mediator or prompt — and each
/// denial still walks the normal trusted-context audit path.
#[tokio::test]
async fn bare_and_mismatched_launches_fail_closed_before_the_pipeline() {
    let fixture = Fixture::new(MediatorChain::default());
    let revision = Digest::hash(b"launch-revision-bare");
    let (pack, hash) = pack_pair("fixture.bare");
    fixture
        .install(
            &pack,
            &hash,
            &revision,
            pack_policy(launch_permissions(true)),
        )
        .await;
    let key = ProcessLaunchKey::generate().unwrap();
    let proc_binding = LaunchCase::new(None).commit(&key);
    let pty_binding = LaunchCase::new(Some(PtyDimensions::new(80, 24).unwrap())).commit(&key);
    let cx = fixture.pack_cx(&pack, &hash, &revision);
    let baseline_watermark = fixture.watermark();
    let baseline_prompts = fixture.prompt.calls();
    let baseline_audit = fixture.audit.events().len();

    let mut errors = Vec::new();
    // A bare request through the generic entry can never mint a launch.
    for request in [
        launch_request(false, PROGRAM),
        launch_request(true, PROGRAM),
    ] {
        errors.push(
            fixture
                .gate
                .authorize(&cx, request, Digest::hash(b"uncommitted-args"))
                .await
                .unwrap_err(),
        );
    }
    // The dedicated entry accepts only the committed Proc/Pty effect.
    errors.push(
        fixture
            .gate
            .authorize_process_launch(&cx, EffectRequest::Clock, &proc_binding)
            .await
            .unwrap_err(),
    );
    errors.push(
        fixture
            .gate
            .authorize_process_launch(&cx, launch_request(false, OTHER_PROGRAM), &proc_binding)
            .await
            .unwrap_err(),
    );
    // PTY presence must match the committed binding in both directions.
    errors.push(
        fixture
            .gate
            .authorize_process_launch(&cx, launch_request(true, PROGRAM), &proc_binding)
            .await
            .unwrap_err(),
    );
    errors.push(
        fixture
            .gate
            .authorize_process_launch(&cx, launch_request(false, PROGRAM), &pty_binding)
            .await
            .unwrap_err(),
    );
    for error in &errors {
        assert!(matches!(error, GateError::Denied(_)), "{error}");
        assert_no_markers(&error.to_string());
    }

    // Every denial produced the normal trusted-context denial audit and
    // nothing else: no reservation, prompt, dispatch or terminal row.
    let events = fixture.audit.events();
    let new_events = &events[baseline_audit..];
    assert_eq!(new_events.len(), errors.len());
    assert!(new_events.iter().all(|event| event.kind == "denied"));
    assert_eq!(fixture.prompt.calls(), baseline_prompts);
    assert_eq!(fixture.watermark(), baseline_watermark);
    assert!(
        fixture
            .approvals
            .pending_terminal(&fixture.profile, 256)
            .unwrap()
            .is_empty()
    );
}

/// Missing proc membership denies; an explicit Pack-limits layer narrower
/// than declaration/ceiling can revoke the PTY permission and also denies —
/// all through the ordinary permission check, before consent.
#[tokio::test]
async fn missing_membership_and_narrowed_policy_layers_deny_normally() {
    let fixture = Fixture::new(MediatorChain::default());
    let revision = Digest::hash(b"launch-revision-noproc");
    let (pack, hash) = pack_pair("fixture.noproc");
    let mut permissions = launch_permissions(false);
    permissions.proc.clear();
    fixture
        .install(&pack, &hash, &revision, pack_policy(permissions))
        .await;
    let key = ProcessLaunchKey::generate().unwrap();
    let binding = LaunchCase::new(None).commit(&key);
    let cx = fixture.pack_cx(&pack, &hash, &revision);
    let baseline_watermark = fixture.watermark();
    let baseline_prompts = fixture.prompt.calls();
    let error = fixture
        .gate
        .authorize_process_launch(&cx, launch_request(false, PROGRAM), &binding)
        .await
        .unwrap_err();
    assert!(matches!(error, GateError::Denied(_)));
    assert_eq!(fixture.prompt.calls(), baseline_prompts);
    assert_eq!(fixture.watermark(), baseline_watermark);

    let fixture = Fixture::new(MediatorChain::default());
    let revision = Digest::hash(b"launch-revision-nopty");
    let (pack, hash) = pack_pair("fixture.nopty");
    let mut pack_limits = launch_permissions(true);
    pack_limits.pty = false;
    fixture
        .install(
            &pack,
            &hash,
            &revision,
            PackPolicy {
                declaration: launch_permissions(true),
                ceiling: launch_permissions(true),
                pack_limits,
            },
        )
        .await;
    let binding = LaunchCase::new(Some(PtyDimensions::new(80, 24).unwrap())).commit(&key);
    let cx = fixture.pack_cx(&pack, &hash, &revision);
    let baseline_prompts = fixture.prompt.calls();
    let error = fixture
        .gate
        .authorize_process_launch(&cx, launch_request(true, PROGRAM), &binding)
        .await
        .unwrap_err();
    assert!(matches!(error, GateError::Denied(_)));
    assert_eq!(fixture.prompt.calls(), baseline_prompts);
}

/// Every post-reservation denial — mediator narrowing, a declined
/// Dangerous prompt or a non-interactive refusal — retires the armed
/// reservation through the ordinary durable path: one acknowledged
/// terminal record, abandoned before use and never dispatched, still
/// bound to the committed args digest and authoritative effect digest.
fn assert_abandoned_launch_terminal(
    fixture: &Fixture,
    reservation: &GrantId,
    binding: &ProcessLaunchBinding,
    request: &EffectRequest,
) {
    let terminal = fixture
        .approvals
        .terminal_delivery(reservation)
        .unwrap()
        .unwrap();
    assert!(terminal.acknowledged);
    assert_eq!(terminal.decision.reservation.id, *reservation);
    assert_eq!(terminal.decision.reservation.args, *binding.args_digest());
    assert_eq!(
        terminal.decision.reservation.effect,
        request.digest().unwrap()
    );
    assert_eq!(
        terminal.decision.disposition,
        TerminalDisposition::AbandonedBeforeUse
    );
    assert_eq!(terminal.decision.outcome, Outcome::Cancelled);
    let event = &terminal.decision.event;
    assert_eq!(event.kind, "outcome");
    assert_eq!(
        payload_field(event, "disposition").as_deref(),
        Some("abandoned-before-use")
    );
}

/// A mediator sees the actual typed Dangerous launch prompt and may narrow
/// authority, but it can never widen it, never clamp the committed limits
/// and never substitute for the per-effect native consent.
#[tokio::test]
async fn mediator_may_narrow_but_cannot_widen_or_replace_native_consent() {
    let recorder = Arc::new(RecordingMediator::new(Verdict::Pass));
    let mediator: Arc<dyn Mediator> = recorder.clone();
    let fixture = Fixture::new(MediatorChain::new(vec![mediator]));
    let revision = Digest::hash(b"launch-revision-mediator");
    let (pack, hash) = pack_pair("fixture.mediator");
    fixture
        .install(
            &pack,
            &hash,
            &revision,
            pack_policy(launch_permissions(false)),
        )
        .await;
    let key = ProcessLaunchKey::generate().unwrap();
    let binding = LaunchCase::new(None).commit(&key);
    let cx = fixture.pack_cx(&pack, &hash, &revision);
    let request = launch_request(false, PROGRAM);
    let baseline_mediator = recorder.calls();
    let baseline_prompts = fixture.prompt.calls();
    let baseline_watermark = fixture.watermark();

    // Narrowing the program away denies before the trusted prompt; the
    // mediator still observed the real Dangerous prompt with the
    // authoritative effect and the full binding args digest.
    recorder.set_verdict(Verdict::Constrain({
        let mut narrowed = launch_permissions(false);
        narrowed.proc.clear();
        narrowed
    }));
    let watermark_before = fixture.watermark();
    let error = fixture
        .gate
        .authorize_process_launch(&cx, request.clone(), &binding)
        .await
        .unwrap_err();
    assert!(matches!(error, GateError::Denied(_)));
    assert_eq!(recorder.calls(), baseline_mediator + 1);
    assert_eq!(fixture.prompt.calls(), baseline_prompts);
    let seen = recorder.last_prompt();
    assert_eq!(seen.risk, Risk::Dangerous);
    assert_eq!(seen.effect, request);
    assert_eq!(seen.args_digest, *binding.args_digest());
    assert_no_markers(&seen.summary);
    // The denied attempt still retires its armed reservation through the
    // ordinary durable path: one abandoned terminal, never dispatched.
    assert_eq!(fixture.watermark(), watermark_before + 1);
    assert_abandoned_launch_terminal(&fixture, &seen.reservation, &binding, &request);

    // A post-mediator ceiling below any committed limit field denies before
    // consent rather than silently clamping the binding.
    recorder.set_verdict(Verdict::Constrain({
        let mut narrowed = launch_permissions(false);
        narrowed.wasm.memory_mib = binding.limits().memory_mib - 1;
        narrowed
    }));
    let watermark_before = fixture.watermark();
    let error = fixture
        .gate
        .authorize_process_launch(&cx, request.clone(), &binding)
        .await
        .unwrap_err();
    assert!(matches!(error, GateError::Denied(_)));
    assert_eq!(fixture.prompt.calls(), baseline_prompts);
    let seen = recorder.last_prompt();
    assert_eq!(recorder.calls(), baseline_mediator + 2);
    assert_eq!(seen.risk, Risk::Dangerous);
    assert_eq!(fixture.watermark(), watermark_before + 1);
    assert_abandoned_launch_terminal(&fixture, &seen.reservation, &binding, &request);

    // A mediator can never widen past the effective ceiling.
    recorder.set_verdict(Verdict::Constrain({
        let mut widened = launch_permissions(false);
        widened.proc.insert(OTHER_PROGRAM.into());
        widened
    }));
    let watermark_before = fixture.watermark();
    let error = fixture
        .gate
        .authorize_process_launch(&cx, request.clone(), &binding)
        .await
        .unwrap_err();
    assert!(matches!(error, GateError::Denied(_)));
    assert_eq!(fixture.prompt.calls(), baseline_prompts);
    assert_eq!(recorder.calls(), baseline_mediator + 3);
    let seen = recorder.last_prompt();
    assert_eq!(seen.risk, Risk::Dangerous);
    assert_eq!(fixture.watermark(), watermark_before + 1);
    assert_abandoned_launch_terminal(&fixture, &seen.reservation, &binding, &request);
    assert_eq!(fixture.watermark(), baseline_watermark + 3);

    // Mediator Pass is not consent: a declined Dangerous prompt yields no
    // launch grant.
    recorder.set_verdict(Verdict::Pass);
    fixture.prompt.set_mode(PromptMode::Deny);
    let watermark_before = fixture.watermark();
    let error = fixture
        .gate
        .authorize_process_launch(&cx, request.clone(), &binding)
        .await
        .unwrap_err();
    assert!(matches!(error, GateError::Denied(_)));
    assert_eq!(fixture.prompt.calls(), baseline_prompts + 1);
    assert_eq!(fixture.prompt.last_risk(), Some(Risk::Dangerous));
    assert_eq!(recorder.calls(), baseline_mediator + 4);
    // The declined prompt named the same reservation the mediator reviewed.
    let seen = recorder.last_prompt();
    assert_eq!(
        fixture.prompt.last_reservation().as_ref(),
        Some(&seen.reservation)
    );
    assert_eq!(fixture.watermark(), watermark_before + 1);
    assert_abandoned_launch_terminal(&fixture, &seen.reservation, &binding, &request);

    // A non-interactive context cannot obtain per-effect native consent.
    fixture.prompt.set_mode(PromptMode::Approve);
    let cx_non_interactive = fixture.pack_cx(&pack, &hash, &revision).non_interactive();
    let watermark_before = fixture.watermark();
    let error = fixture
        .gate
        .authorize_process_launch(&cx_non_interactive, request.clone(), &binding)
        .await
        .unwrap_err();
    assert!(matches!(error, GateError::ApprovalRequired));
    assert_eq!(fixture.prompt.calls(), baseline_prompts + 1);
    assert_eq!(recorder.calls(), baseline_mediator + 5);
    let seen = recorder.last_prompt();
    assert_eq!(seen.risk, Risk::Dangerous);
    assert_eq!(fixture.watermark(), watermark_before + 1);
    assert_abandoned_launch_terminal(&fixture, &seen.reservation, &binding, &request);
    assert_eq!(fixture.watermark(), baseline_watermark + 5);

    // Every denied launch walked the normal denial audit path and left
    // exactly one delivered abandoned outcome; nothing dispatched and the
    // durable outbox is drained.
    let events = fixture.audit.events();
    assert_eq!(
        events.iter().filter(|event| event.kind == "denied").count(),
        5
    );
    assert!(events.iter().all(|event| event.kind != "dispatch"));
    assert_eq!(
        events
            .iter()
            .filter(|event| {
                event.kind == "outcome"
                    && payload_field(event, "disposition").as_deref()
                        == Some("abandoned-before-use")
            })
            .count(),
        5
    );
    assert!(
        fixture
            .approvals
            .pending_terminal(&fixture.profile, 256)
            .unwrap()
            .is_empty()
    );
}

/// Committed limits are returned exactly — never widened to the wider
/// context and never clamped — and any ceiling below them denies.
#[tokio::test]
async fn committed_limits_are_returned_exactly_and_ceiling_checked() {
    let fixture = Fixture::new(MediatorChain::default());
    let revision = Digest::hash(b"launch-revision-limits");
    let (pack, hash) = pack_pair("fixture.limits");
    fixture
        .install(
            &pack,
            &hash,
            &revision,
            pack_policy(launch_permissions(false)),
        )
        .await;
    let key = ProcessLaunchKey::generate().unwrap();
    let request = launch_request(false, PROGRAM);

    // A binding below the wider context stays exact: it is never widened.
    let binding = LaunchCase::new(None).commit(&key);
    let cx = fixture.pack_cx(&pack, &hash, &revision);
    let grant = fixture
        .gate
        .authorize_process_launch(&cx, request.clone(), &binding)
        .await
        .unwrap();
    assert_eq!(grant.limits(), &launch_limits());
    assert_eq!(grant.limits(), binding.limits());
    assert_ne!(grant.limits(), cx.limits());
    let id = grant.id().clone();
    drop(grant);
    // The unused grant abandons through the ordinary durable path.
    assert_eq!(
        fixture
            .approvals
            .terminal_delivery(&id)
            .unwrap()
            .unwrap()
            .decision
            .disposition,
        TerminalDisposition::AbandonedBeforeUse
    );

    // Committed limits above the live context deny before a reservation.
    let baseline_watermark = fixture.watermark();
    let baseline_prompts = fixture.prompt.calls();
    let mut oversized_case = LaunchCase::new(None);
    oversized_case.limits = ResourceLimits {
        memory_mib: cx_limits().memory_mib * 2,
        ..launch_limits()
    };
    let oversized = oversized_case.commit(&key);
    let error = fixture
        .gate
        .authorize_process_launch(&cx, request.clone(), &oversized)
        .await
        .unwrap_err();
    assert!(matches!(error, GateError::Denied(_)));

    // A live context narrower than the committed configuration also denies.
    let tighter = ResourceLimits {
        memory_mib: launch_limits().memory_mib / 2,
        ..cx_limits()
    };
    let cx_tight = fixture
        .cx(
            Principal::Pack {
                pack: pack.clone(),
                hash: hash.clone(),
            },
            tighter,
        )
        .with_revision(revision);
    let error = fixture
        .gate
        .authorize_process_launch(&cx_tight, request, &binding)
        .await
        .unwrap_err();
    assert!(matches!(error, GateError::Denied(_)));
    assert_eq!(fixture.prompt.calls(), baseline_prompts);
    assert_eq!(fixture.watermark(), baseline_watermark);
}

/// An unregistered compiled revision, an already-expired deadline and a
/// stale security epoch all fail through the normal context checks with no
/// executable grant — deterministic state changes, no sleeps.
#[tokio::test]
async fn stale_epoch_unregistered_revision_and_expired_deadline_fail_without_a_grant() {
    let fixture = Fixture::new(MediatorChain::default());
    let revision = Digest::hash(b"launch-revision-context");
    let (pack, hash) = pack_pair("fixture.context");
    fixture
        .install(
            &pack,
            &hash,
            &revision,
            pack_policy(launch_permissions(false)),
        )
        .await;
    let key = ProcessLaunchKey::generate().unwrap();
    let binding = LaunchCase::new(None).commit(&key);
    let request = launch_request(false, PROGRAM);
    let baseline_watermark = fixture.watermark();
    let baseline_prompts = fixture.prompt.calls();

    let cx_unregistered = fixture.pack_cx(&pack, &hash, &Digest::hash(b"unregistered"));
    let error = fixture
        .gate
        .authorize_process_launch(&cx_unregistered, request.clone(), &binding)
        .await
        .unwrap_err();
    assert!(matches!(error, GateError::Denied(_)));

    let cx_expired = CallCx::try_root_with_control(
        fixture.profile.clone(),
        Principal::Pack {
            pack: pack.clone(),
            hash: hash.clone(),
        },
        RequestId::parse("req_expired").unwrap(),
        CancellationToken::new_with_control(&fixture.control).unwrap(),
        Instant::now(),
        cx_limits(),
        fixture.control.clone(),
    )
    .unwrap()
    .with_security_epoch(fixture.gate.epoch(&fixture.profile).unwrap())
    .with_revision(revision.clone());
    assert!(
        fixture
            .gate
            .authorize_process_launch(&cx_expired, request.clone(), &binding)
            .await
            .is_err()
    );

    let cx_stale = fixture.pack_cx(&pack, &hash, &revision);
    fixture.gate.revoke_epoch(&fixture.profile).unwrap();
    assert!(matches!(
        fixture
            .gate
            .authorize_process_launch(&cx_stale, request, &binding)
            .await
            .unwrap_err(),
        GateError::StaleEpoch
    ));
    assert_eq!(fixture.prompt.calls(), baseline_prompts);
    assert_eq!(fixture.watermark(), baseline_watermark);
}

/// A wrong full digest can never consume the genuine launch grant; the
/// untouched grant abandons through the ordinary one-shot durable path and
/// stays bound to the real committed args digest.
#[tokio::test]
async fn wrong_args_digest_cannot_consume_a_genuine_launch_grant() {
    let fixture = Fixture::new(MediatorChain::default());
    let revision = Digest::hash(b"launch-revision-digest");
    let (pack, hash) = pack_pair("fixture.digest");
    fixture
        .install(
            &pack,
            &hash,
            &revision,
            pack_policy(launch_permissions(false)),
        )
        .await;
    let key = ProcessLaunchKey::generate().unwrap();
    let binding = LaunchCase::new(None).commit(&key);
    let cx = fixture.pack_cx(&pack, &hash, &revision);
    let request = launch_request(false, PROGRAM);
    let grant = fixture
        .gate
        .authorize_process_launch(&cx, request.clone(), &binding)
        .await
        .unwrap();
    let id = grant.id().clone();
    // An unkeyed or foreign digest is not the retained binding commitment.
    let result = grant.consume(&cx, &request, &Digest::hash(b"foreign-args"));
    assert!(matches!(result, Err(GateError::Denied(_))));
    let terminal = fixture.approvals.terminal_delivery(&id).unwrap().unwrap();
    assert_eq!(
        terminal.decision.disposition,
        TerminalDisposition::AbandonedBeforeUse
    );
    assert_eq!(terminal.decision.reservation.args, *binding.args_digest());
}
