//! Issue #1520 acceptance items 6, 7, 8 and 10 through the actual Core: real
//! verified Packs (a WAT-built Wasm component plus hash-verified data Packs),
//! the production Profile compiler, the production Flow engine and the real
//! Gate with a recording trusted Host. Nothing here fabricates a frame, an
//! approval or an audit record.
use super::*;
use crate::test_support::{
    capture_run_failure, native_run_diagnostic, permissions, runtime_pack_with_step,
    runtime_profile, within_session,
};
use crate::{Core, CoreConfig};
use async_trait::async_trait;
use std::{path::Path, path::PathBuf, sync::atomic::AtomicBool};
use tobkiri_canon::ids::ProfileId;
use tobkiri_gate::{
    ApprovalAnswer, ApprovalPrompt, ApprovalStore, AuditEvent, HostError, HostIo, PermissionSet,
    TrustedPrompt, UiEmbed,
};
use tobkiri_manifest::{
    FileEntry, PackManifest, PortDeclaration, Ports, RuntimeSpec, ShapeDefinition, ShapeKind,
    VerifiedPack,
};
use tobkiri_profile::{
    Next, OrderConstraints, OverlayAccess, OverlayDocument, OverlayOrder, OverlayTarget, PackPin,
    PortCall, Profile, Step, StepKind,
};
use tobkiri_store::{Approvals, AuditLog, AuditQuota, DataRoot};

const INPUT: &str = "local.base.input@1.0";
const STEP: &str = "local.base.step@1.0";

type Hook = Box<dyn Fn() + Send + Sync>;

/// A trusted Host that records every prompt. It approves unless told to deny,
/// and can run one hook at the moment a User-principal port call is prompted:
/// the point at which an overlay's own effect is between approval and dispatch.
#[derive(Default)]
struct TestHost {
    prompts: Mutex<Vec<ApprovalPrompt>>,
    deny_user_calls: AtomicBool,
    deny_all: AtomicBool,
    on_user_call: Mutex<Option<Hook>>,
}
fn is_user_call(prompt: &ApprovalPrompt) -> bool {
    matches!(prompt.principal, Principal::User)
        && matches!(prompt.effect, EffectRequest::PortCall { .. })
}
impl TestHost {
    fn recorded(&self) -> Vec<ApprovalPrompt> {
        self.prompts.lock().unwrap().clone()
    }
    fn user_calls(&self) -> Vec<ApprovalPrompt> {
        self.recorded().into_iter().filter(is_user_call).collect()
    }
}
#[async_trait]
impl TrustedPrompt for TestHost {
    async fn prompt_approval(&self, prompt: ApprovalPrompt) -> ApprovalAnswer {
        let user_call = is_user_call(&prompt);
        let hook = if user_call {
            self.on_user_call.lock().unwrap().take()
        } else {
            None
        };
        if let Some(hook) = hook {
            hook();
        }
        let denied = self.deny_all.load(Ordering::SeqCst)
            || (user_call && self.deny_user_calls.load(Ordering::SeqCst));
        self.prompts.lock().unwrap().push(prompt);
        if denied {
            ApprovalAnswer::Deny
        } else {
            ApprovalAnswer::Approve
        }
    }
}
#[async_trait]
impl HostIo for TestHost {
    fn display(&self, _: &str, _: &CanonValue) {}
    async fn terminal_read_line(&self, _: &str) -> Result<String, HostError> {
        Err(HostError("unavailable".into()))
    }
    fn terminal_write(&self, _: &str) {}
    fn embed_ui(&self, _: UiEmbed) -> Result<UiSessionId, HostError> {
        Err(HostError("unavailable".into()))
    }
    async fn pick_directory(&self, _: &str) -> Option<PathBuf> {
        None
    }
}

fn input_shape() -> ShapeRef {
    ShapeRef::parse(INPUT).unwrap()
}
fn document(id: &str, target: OverlayTarget, body: Vec<Step>) -> OverlayDocument {
    OverlayDocument {
        format: "tobkiri.overlay/1".into(),
        id: id.into(),
        target,
        order: OrderConstraints::default(),
        body,
    }
}
/// Addressed by the Flow step's shape name (its Role).
fn by_role() -> OverlayTarget {
    OverlayTarget::Role {
        role: ShapeRef::parse(STEP).unwrap(),
    }
}
/// Addressed by Flow ID and step ID.
fn by_step() -> OverlayTarget {
    OverlayTarget::Step {
        flow: "local.base/main".into(),
        step: "echo".into(),
    }
}
fn value_args(value: i64) -> CanonValue {
    CanonValue::Obj(BTreeMap::from([("value".into(), CanonValue::Int(value))]))
}
fn next(args: CanonValue) -> Step {
    Step::new("next", StepKind::Next(Next { args }))
}
/// Wrap: call `next` with changed arguments.
fn wrap(value: i64) -> Vec<Step> {
    vec![next(value_args(value))]
}
/// Insert: an extra effect of the overlay's own, then the original step.
fn insert() -> Vec<Step> {
    let mut call = Step::new(
        "extra",
        StepKind::Call(PortCall {
            shape: input_shape(),
            op: OpName::parse("echo").unwrap(),
            args: value_args(5),
        }),
    );
    call.out = Some("seen".into());
    vec![call, next(CanonValue::Str("$input".into()))]
}
/// Replace: never call `next`, return another value.
fn replace(value: i64) -> Vec<Step> {
    vec![Step::new(
        "answer",
        StepKind::Return(CanonValue::Obj(BTreeMap::from([(
            "ok".into(),
            CanonValue::Int(value),
        )]))),
    )]
}
/// Delete: do nothing at all.
fn delete() -> Vec<Step> {
    Vec::new()
}

fn file_entries(files: &BTreeMap<String, Vec<u8>>) -> Vec<FileEntry> {
    files
        .iter()
        .map(|(path, bytes)| FileEntry {
            path: path.clone(),
            sha256: Digest::hash(bytes),
            size: bytes.len() as u64,
        })
        .collect()
}
fn overlay_pack(
    directory: &Path,
    suffix: &str,
    overlay: &OverlayDocument,
    calls_input: bool,
    declared: &PermissionSet,
) -> VerifiedPack {
    let files = BTreeMap::from([(
        "overlays/main.json".to_string(),
        serde_json::to_vec(overlay).unwrap(),
    )]);
    let shape = input_shape();
    let manifest = PackManifest {
        format: "tobkiri.pack/1".into(),
        id: PackId::parse(&format!("local.{suffix}")).unwrap(),
        version: "1.0.0".into(),
        publisher_key: None,
        runtime: RuntimeSpec::Data,
        shapes: Vec::new(),
        ports: Ports {
            provides: Vec::new(),
            consumes: if calls_input {
                vec![PortDeclaration {
                    shape: shape.name.clone(),
                    version: shape.version,
                    optional: false,
                }]
            } else {
                Vec::new()
            },
            untyped: Vec::new(),
        },
        inputs: Vec::new(),
        flows: Vec::new(),
        overlays: vec!["overlays/main.json".into()],
        stores: Vec::new(),
        permissions: declared.clone(),
        ui: None,
        files: file_entries(&files),
    };
    let artifact = tobkiri_manifest::build_tpk(&manifest, &files, None).unwrap();
    let verified = tobkiri_manifest::verify_tpk(&artifact, &BTreeMap::new()).unwrap();
    std::fs::write(directory.join(format!("{suffix}.tpk")), artifact).unwrap();
    verified
}

fn is_dispatch_of(event: &AuditEvent, prompt: &ApprovalPrompt) -> bool {
    let effect = CanonValue::Str(prompt.effect.digest().unwrap().to_string());
    let args = CanonValue::Str(prompt.args_digest.to_string());
    event.kind == "dispatch"
        && matches!(&event.payload, CanonValue::Obj(fields)
            if fields.get("effect_digest") == Some(&effect)
                && fields.get("args_digest") == Some(&args))
}
fn dispatches_by(events: &[AuditEvent], pack: &str) -> usize {
    events
        .iter()
        .filter(|event| {
            event.kind == "dispatch"
                && matches!(&event.principal, Principal::Pack { pack: id, .. } if id.as_str() == pack)
        })
        .count()
}

struct Outcome {
    completion: NativeInputCompletion,
    failures: Vec<String>,
}
impl Outcome {
    fn succeeded(&self) -> bool {
        self.completion.status == NativeInputStatus::Succeeded
    }
    fn diagnostic(&self) -> String {
        native_run_diagnostic(&self.completion, &self.failures)
    }
    fn result(&self) -> serde_json::Value {
        let envelope: serde_json::Value =
            serde_json::from_slice(self.completion.buffer.as_bytes()).unwrap();
        envelope["routes"][0]["result"].clone()
    }
    #[track_caller]
    fn assert_result(&self, expected: serde_json::Value) {
        assert!(self.succeeded(), "{}", self.diagnostic());
        assert_eq!(self.result(), expected, "{}", self.diagnostic());
    }
}
async fn drive_raw(session: &Session) -> Result<Outcome, StepError> {
    let entry = session.input_entries()[0].clone();
    let input = within_session(
        session,
        "overlay Input read",
        session.read_input(
            InputSourceId::parse(entry.source_pack().as_str()).unwrap(),
            entry.exact_input_view().clone(),
            entry.operation().clone(),
        ),
    )
    .await
    .unwrap();
    let mut run = within_session(
        session,
        "overlay NativeInput admission",
        session.submit_native_input(input),
    )
    .await
    .unwrap();
    let mut failures = Vec::new();
    let completion = within_session(session, "overlay Flow completion", async {
        while let Some(event) = run.events.recv().await {
            capture_run_failure(&event.event, &mut failures);
        }
        run.result.await
    })
    .await?;
    Ok(Outcome {
        completion,
        failures,
    })
}
async fn drive(session: &Session) -> Outcome {
    drive_raw(session).await.unwrap()
}

type Snapshot = (Digest, Vec<u8>, usize, crate::ProfileSavePrecondition);
fn snapshot(core: &Core, host: &TestHost, id: &ProfileId) -> Snapshot {
    let (stored, head) = core.profile(id).unwrap();
    (
        head,
        crate::profiles::strict_bytes(&stored).unwrap(),
        host.recorded().len(),
        core.profile_save_precondition(id).unwrap(),
    )
}
fn conflict_codes(result: Result<Digest, CoreError>) -> Vec<String> {
    match result {
        Err(CoreError::Conflicts(conflicts)) => conflicts
            .into_iter()
            .map(|conflict| conflict.code)
            .collect(),
        other => panic!("expected a structural conflict, got {other:?}"),
    }
}

/// One Core with the real Wasm Flow owner `local.base` (one overlayable step
/// `echo`, which returns the 42 its Input reads) and any number of data Packs
/// carrying overlays.
struct World {
    core: Core,
    host: Arc<TestHost>,
    root: DataRoot,
    physical: PathBuf,
    perms: PermissionSet,
    base: VerifiedPack,
    extra: Vec<VerifiedPack>,
    profile: Profile,
    head: Digest,
    _directory: tempfile::TempDir,
}
impl World {
    async fn new(access: OverlayAccess) -> Self {
        let directory = tempfile::tempdir().unwrap();
        let physical = directory.path().canonicalize().unwrap();
        let mut perms = permissions(&["base"]);
        perms.ports.insert(STEP.into());
        let base = runtime_pack_with_step(&physical, "base", perms.clone(), false, Some(access));
        let host = Arc::new(TestHost::default());
        let core = Core::open(
            CoreConfig::for_test_data_root(physical.join("data")),
            host.clone(),
        )
        .unwrap();
        let profile = runtime_profile(&physical, std::slice::from_ref(&base), perms.clone(), false);
        let head = core.save_profile(profile.clone(), None).await.unwrap();
        let root = DataRoot::open(physical.join("data")).unwrap();
        let world = Self {
            core,
            host,
            root,
            physical,
            perms,
            base,
            extra: Vec::new(),
            profile,
            head,
            _directory: directory,
        };
        world.approve();
        world
    }
    fn packs(&self) -> Vec<VerifiedPack> {
        std::iter::once(self.base.clone())
            .chain(self.extra.iter().cloned())
            .collect()
    }
    fn approve(&self) {
        let approvals = Approvals::open(&self.root, &self.profile.id).unwrap();
        for pack in self.packs() {
            approvals
                .set_permissions(&self.profile.id, &pack.manifest.id, &pack.hash, &self.perms)
                .unwrap();
        }
    }
    fn add_overlay_pack(&mut self, suffix: &str, overlay: &OverlayDocument, calls_input: bool) {
        let declared = self.perms.clone();
        self.add_overlay_pack_declaring(suffix, overlay, calls_input, &declared);
    }
    fn add_overlay_pack_declaring(
        &mut self,
        suffix: &str,
        overlay: &OverlayDocument,
        calls_input: bool,
        declared: &PermissionSet,
    ) {
        let pack = overlay_pack(&self.physical, suffix, overlay, calls_input, declared);
        self.extra.push(pack);
    }
    /// A fresh candidate over all Packs known so far, with no User policy and
    /// no User overlay; the caller adds those explicitly.
    fn candidate(&self) -> Profile {
        runtime_profile(&self.physical, &self.packs(), self.perms.clone(), false)
    }
    fn candidate_with_user_overlays(&self, overlays: Vec<OverlayDocument>) -> Profile {
        let mut candidate = self.candidate();
        candidate.user_permissions = Some(self.perms.clone());
        candidate.user_overlays = overlays;
        candidate
    }
    async fn save(&mut self, candidate: Profile) -> Result<Digest, CoreError> {
        let head = self
            .core
            .save_profile(candidate.clone(), Some(self.head.clone()))
            .await?;
        self.profile = candidate;
        self.head = head.clone();
        self.approve();
        Ok(head)
    }
    fn snapshot(&self) -> Snapshot {
        snapshot(&self.core, &self.host, &self.profile.id)
    }
    async fn start(&self) -> Session {
        self.core.start(&self.profile.id).await.unwrap()
    }
    async fn run(&self) -> Outcome {
        let session = self.start().await;
        let outcome = drive(&session).await;
        within_session(&session, "overlay Session close", session.close())
            .await
            .unwrap();
        outcome
    }
    fn audit(&self) -> Vec<AuditEvent> {
        let path = self.root.path().join(
            tobkiri_store::DataRoot::profile_relative(&self.profile.id, "audit.sqlite").unwrap(),
        );
        let connection =
            rusqlite::Connection::open_with_flags(path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)
                .unwrap();
        let mut statement = connection
            .prepare("SELECT body FROM audit ORDER BY seq")
            .unwrap();
        statement
            .query_map([], |row| row.get::<_, Vec<u8>>(0))
            .unwrap()
            .map(|bytes| serde_json::from_slice(&bytes.unwrap()).unwrap())
            .collect()
    }
    fn verify_audit(&self) {
        AuditLog::open(&self.root, &self.profile.id, AuditQuota::default())
            .unwrap()
            .verify()
            .unwrap();
    }
    /// The candidate must fail with `code`, leaving the stored Profile, its
    /// authority epoch and the approval record exactly as they were, and the
    /// previous configuration must still run.
    async fn assert_rejected(&mut self, candidate: Profile, code: &str) {
        let before = self.snapshot();
        let codes = conflict_codes(self.save(candidate).await);
        assert!(codes.iter().any(|found| found == code), "{codes:?}");
        assert_eq!(self.snapshot(), before);
        self.run().await.assert_result(serde_json::json!(42));
    }
}

struct Observation {
    outcome: Outcome,
    base_dispatches: usize,
    overlay_dispatches: usize,
}
/// Run the Flow once with at most one foreign Pack overlay on the Open step.
async fn foreign(mode: Option<(Vec<Step>, bool)>) -> Observation {
    let mut world = World::new(OverlayAccess::Open).await;
    if let Some((body, calls_input)) = mode {
        let overlay = document("overlay", by_role(), body);
        world.add_overlay_pack("overlayer", &overlay, calls_input);
        let candidate = world.candidate();
        world.save(candidate).await.unwrap();
    }
    let outcome = world.run().await;
    let events = world.audit();
    world.verify_audit();
    Observation {
        outcome,
        base_dispatches: dispatches_by(&events, "local.base"),
        overlay_dispatches: dispatches_by(&events, "local.overlayer"),
    }
}

// ---------------------------------------------------------------- item 6

#[tokio::test]
async fn foreign_pack_inserts_wraps_replaces_and_deletes_a_role_addressed_step_through_the_real_flow()
 {
    let baseline = foreign(None).await;
    baseline.outcome.assert_result(serde_json::json!(42));
    assert_eq!(baseline.overlay_dispatches, 0);

    // Wrap: `next` with changed arguments. The original runs once.
    let wrapped = foreign(Some((wrap(7), false))).await;
    wrapped.outcome.assert_result(serde_json::json!(7));
    assert_eq!(wrapped.base_dispatches, baseline.base_dispatches);

    // Insert: an extra effect of the overlay Pack's own, then the original.
    let inserted = foreign(Some((insert(), true))).await;
    inserted.outcome.assert_result(serde_json::json!(42));
    assert_eq!(inserted.overlay_dispatches, 1);
    assert_eq!(inserted.base_dispatches, baseline.base_dispatches);

    // Replace: no `next`. The original step is never dispatched.
    let replaced = foreign(Some((replace(99), false))).await;
    replaced.outcome.assert_result(serde_json::json!(99));
    assert_eq!(replaced.base_dispatches + 1, baseline.base_dispatches);

    // Delete: an empty body. The original step is never dispatched.
    let deleted = foreign(Some((delete(), false))).await;
    deleted.outcome.assert_result(serde_json::Value::Null);
    assert_eq!(deleted.base_dispatches + 1, baseline.base_dispatches);
}

#[tokio::test]
async fn foreign_pack_cannot_overlay_a_closed_step_or_address_an_open_step_by_id() {
    // The owner never opened the step: the role address is refused.
    let mut closed = World::new(OverlayAccess::Closed).await;
    closed.add_overlay_pack("overlayer", &document("overlay", by_role(), wrap(7)), false);
    let candidate = closed.candidate();
    closed.assert_rejected(candidate, "overlay-closed").await;

    // The step is open, but another Pack may only use the shape name.
    let mut open = World::new(OverlayAccess::Open).await;
    open.add_overlay_pack("overlayer", &document("overlay", by_step(), wrap(7)), false);
    let candidate = open.candidate();
    open.assert_rejected(candidate, "overlay-closed").await;
}

#[tokio::test]
async fn foreign_pack_overlay_also_needs_profile_and_pack_permission() {
    // Owner permission alone is not enough: the Profile ceiling must allow the
    // role (R4.11).
    let mut world = World::new(OverlayAccess::Open).await;
    world.add_overlay_pack("overlayer", &document("overlay", by_role(), wrap(7)), false);
    let mut candidate = world.candidate();
    candidate.ceiling.ports.remove(STEP);
    world.assert_rejected(candidate, "overlay-permission").await;

    // The Pack itself must declare the role in its manifest.
    let mut undeclared = world.perms.clone();
    undeclared.ports.remove(STEP);
    let mut world = World::new(OverlayAccess::Open).await;
    world.add_overlay_pack_declaring(
        "overlayer",
        &document("overlay", by_role(), wrap(7)),
        false,
        &undeclared,
    );
    let candidate = world.candidate();
    world.assert_rejected(candidate, "overlay-permission").await;

    // An overlay's own call needs the overlay Pack's declaration too.
    let mut no_input = world.perms.clone();
    no_input.ports.remove(INPUT);
    let mut world = World::new(OverlayAccess::Open).await;
    world.add_overlay_pack_declaring(
        "overlayer",
        &document("overlay", by_role(), insert()),
        false,
        &no_input,
    );
    let candidate = world.candidate();
    world.assert_rejected(candidate, "port-permission").await;
}

// ---------------------------------------------------------------- item 7

#[tokio::test]
async fn user_overlay_can_wrap_or_replace_a_closed_step_by_shape_name_or_by_flow_and_step_id() {
    for (target, body, expected) in [
        (by_role(), wrap(7), 7),
        (by_step(), replace(99), 99),
        (by_step(), wrap(7), 7),
        (by_role(), replace(99), 99),
    ] {
        let mut world = World::new(OverlayAccess::Closed).await;
        let before = world.head.clone();
        let candidate = world.candidate_with_user_overlays(vec![document("mine", target, body)]);
        let head = world.save(candidate).await.unwrap();
        assert_ne!(head, before);
        // Recorded in the Profile, not in any Pack.
        let (stored, stored_head) = world.core.profile(&world.profile.id).unwrap();
        assert_eq!(stored_head, head);
        assert_eq!(stored.user_overlays.len(), 1);
        assert_eq!(stored.user_overlays[0].id, "mine");
        assert!(
            stored
                .packs
                .iter()
                .all(|pin| pin.id == world.base.manifest.id)
        );
        world.run().await.assert_result(serde_json::json!(expected));
    }
}

#[tokio::test]
async fn user_overlay_keeps_the_separate_user_policy_for_its_own_calls() {
    let mut world = World::new(OverlayAccess::Closed).await;
    // No User policy at all.
    let mut candidate = world.candidate();
    candidate.user_overlays = vec![document("mine", by_step(), insert())];
    world.assert_rejected(candidate, "port-permission").await;
    // A User policy that does not name the port.
    let mut candidate =
        world.candidate_with_user_overlays(vec![document("mine", by_step(), insert())]);
    candidate
        .user_permissions
        .as_mut()
        .unwrap()
        .ports
        .remove(INPUT);
    world.assert_rejected(candidate, "port-permission").await;
    // A policy that names it works, and only then.
    let candidate = world.candidate_with_user_overlays(vec![document("mine", by_step(), insert())]);
    world.save(candidate).await.unwrap();
    world.run().await.assert_result(serde_json::json!(42));
}

// ---------------------------------------------------------------- item 8

async fn user_insert_world() -> World {
    let mut world = World::new(OverlayAccess::Closed).await;
    let candidate = world.candidate_with_user_overlays(vec![document("mine", by_step(), insert())]);
    world.save(candidate).await.unwrap();
    world
}
fn assert_never_dispatched(world: &World) {
    let prompts = world.host.user_calls();
    assert!(!prompts.is_empty(), "the overlay's call was never prompted");
    let events = world.audit();
    for prompt in &prompts {
        assert!(
            !events.iter().any(|event| is_dispatch_of(event, prompt)),
            "an unapproved, stale or cancelled overlay effect was dispatched"
        );
    }
}

#[tokio::test]
async fn user_overlay_effect_reaches_the_trusted_prompt_and_the_audit_log_while_pack_calls_do_not()
{
    let world = user_insert_world().await;
    world.run().await.assert_result(serde_json::json!(42));
    let prompts = world.host.user_calls();
    assert!(!prompts.is_empty(), "a User overlay call was not prompted");
    let events = world.audit();
    for prompt in &prompts {
        assert_eq!(
            events
                .iter()
                .filter(|event| is_dispatch_of(event, prompt))
                .count(),
            1,
            "each approved User call is dispatched exactly once"
        );
    }
    // The Flow owner's own calls are checked by the Gate without a per-call
    // prompt; only the User principal is always asked.
    assert!(world.host.recorded().iter().all(|prompt| !matches!(
        (&prompt.principal, &prompt.effect),
        (Principal::Pack { .. }, EffectRequest::PortCall { .. })
    )));
    world.verify_audit();
}

#[tokio::test]
async fn user_overlay_denied_at_the_trusted_prompt_never_dispatches() {
    let world = user_insert_world().await;
    world.host.deny_user_calls.store(true, Ordering::SeqCst);
    let outcome = world.run().await;
    assert!(!outcome.succeeded(), "{}", outcome.diagnostic());
    assert_never_dispatched(&world);
    world.verify_audit();
}

#[tokio::test]
async fn authority_epoch_change_after_approval_blocks_the_overlay_effect() {
    let world = user_insert_world().await;
    let session = world.start().await;
    let hook_session = session.clone();
    let id = world.profile.id.clone();
    *world.host.on_user_call.lock().unwrap() = Some(Box::new(move || {
        hook_session.gate.revoke_epoch(&id).unwrap();
    }));
    let outcome = drive(&session).await;
    assert!(!outcome.succeeded(), "{}", outcome.diagnostic());
    assert_never_dispatched(&world);
    let _ = session.close().await;
}

#[tokio::test]
async fn cancellation_while_the_overlay_effect_awaits_approval_never_dispatches() {
    let world = user_insert_world().await;
    let session = world.start().await;
    let hook_session = session.clone();
    *world.host.on_user_call.lock().unwrap() = Some(Box::new(move || {
        hook_session.request_shutdown();
    }));
    if let Ok(outcome) = drive_raw(&session).await {
        assert!(!outcome.succeeded(), "{}", outcome.diagnostic());
    }
    assert_never_dispatched(&world);
    let _ = session.close().await;
}

#[tokio::test]
async fn approval_revoked_while_a_user_overlay_runs_blocks_the_original_step() {
    // Control: the same overlay without any change completes.
    let control = user_insert_world().await;
    control.run().await.assert_result(serde_json::json!(42));
    let control_base = dispatches_by(&control.audit(), "local.base");

    let world = user_insert_world().await;
    let session = world.start().await;
    let root = world.root.clone();
    let id = world.profile.id.clone();
    let base_id = world.base.manifest.id.clone();
    let base_hash = world.base.hash.clone();
    *world.host.on_user_call.lock().unwrap() = Some(Box::new(move || {
        Approvals::open(&root, &id)
            .unwrap()
            .set_permissions(&id, &base_id, &base_hash, &PermissionSet::default())
            .unwrap();
    }));
    let outcome = drive(&session).await;
    assert!(!outcome.succeeded(), "{}", outcome.diagnostic());
    assert!(
        dispatches_by(&world.audit(), "local.base") < control_base,
        "the original step ran after its Pack approval was revoked"
    );
    world.verify_audit();
    let _ = session.close().await;
}

#[tokio::test]
async fn pack_overlay_effect_is_checked_with_the_overlay_packs_own_approval() {
    let mut world = World::new(OverlayAccess::Open).await;
    world.add_overlay_pack("overlayer", &document("overlay", by_role(), insert()), true);
    let candidate = world.candidate();
    world.save(candidate).await.unwrap();
    let session = world.start().await;
    // The Pack's approval is withdrawn after the Profile was accepted.
    let overlayer = world.extra[0].clone();
    Approvals::open(&world.root, &world.profile.id)
        .unwrap()
        .set_permissions(
            &world.profile.id,
            &overlayer.manifest.id,
            &overlayer.hash,
            &PermissionSet::default(),
        )
        .unwrap();
    let outcome = drive(&session).await;
    assert!(!outcome.succeeded(), "{}", outcome.diagnostic());
    assert_eq!(dispatches_by(&world.audit(), "local.overlayer"), 0);
    world.verify_audit();
    let _ = session.close().await;
}

// --------------------------------------------------------------- item 10

fn ordered_pair(first: i64, second: i64) -> (OverlayDocument, OverlayDocument) {
    (
        document("a", by_role(), wrap(first)),
        document("b", by_role(), wrap(second)),
    )
}
fn explicit_order(labels: [&str; 2]) -> OverlayOrder {
    OverlayOrder {
        target: by_role(),
        order: labels.iter().map(ToString::to_string).collect(),
    }
}

#[tokio::test]
async fn unordered_pack_overlays_fail_keep_the_head_and_accept_a_bound_explicit_order() {
    let mut world = World::new(OverlayAccess::Open).await;
    let (first, second) = ordered_pair(7, 8);
    world.add_overlay_pack("first", &first, false);
    world.add_overlay_pack("second", &second, false);
    let candidate = world.candidate();
    let rejected = candidate.clone();
    world.assert_rejected(candidate, "overlay-order").await;

    let offer = world
        .core
        .offer_repair(&world.profile.id, &rejected)
        .unwrap()
        .expect("a conflicting change must offer a repair");
    assert!(offer.report.repair_pack_available);
    assert!(
        offer
            .report
            .conflicts
            .iter()
            .any(|conflict| conflict.code == "overlay-order")
    );
    assert_eq!(offer.binding.head, world.head);
    assert_eq!(offer.binding.packs.len(), 1);
    assert_eq!(offer.binding.packs[0].sha256, world.base.hash);

    let mut repaired = rejected;
    repaired
        .overlay_order
        .push(explicit_order(["local.first", "local.second"]));
    let before = world.head.clone();
    let head = world
        .core
        .apply_repair(&offer.binding, repaired.clone())
        .await
        .unwrap();
    assert_ne!(head, before);
    world.profile = repaired;
    world.head = head;
    world.approve();
    // The listed first overlay is outermost; the last one is innermost.
    world.run().await.assert_result(serde_json::json!(8));
}

#[tokio::test]
async fn unordered_user_overlays_fail_keep_the_head_and_accept_a_user_order() {
    let mut world = World::new(OverlayAccess::Closed).await;
    let (first, second) = ordered_pair(7, 8);
    let candidate = world.candidate_with_user_overlays(vec![first, second]);
    let rejected = candidate.clone();
    world.assert_rejected(candidate, "overlay-order").await;

    let mut ordered = rejected;
    ordered
        .overlay_order
        .push(explicit_order(["user:b", "user:a"]));
    world.save(ordered).await.unwrap();
    world.run().await.assert_result(serde_json::json!(7));
}

const SLOT: &str = "local.contract.port@1.0";
const SLOT_BINDING: &str = "local.contract.port@1";

fn slot_permissions() -> PermissionSet {
    let mut declared = permissions(&[]);
    declared.ports.insert(SLOT.into());
    declared
}
/// A hash-verified data Pack. `defines` publishes the single-cardinality port
/// shape; `provides` provides it. The salt makes distinct artifact bytes.
fn slot_pack(
    directory: &Path,
    id: &str,
    salt: &str,
    defines: bool,
    provides: bool,
) -> (VerifiedPack, String) {
    let shape = ShapeRef::parse(SLOT).unwrap();
    let mut files: BTreeMap<String, Vec<u8>> =
        BTreeMap::from([("data/salt.txt".to_string(), salt.as_bytes().to_vec())]);
    let mut shapes = Vec::new();
    if defines {
        files.insert(
            "wit/port.wit".into(),
            b"package local:contract@1.0.0; interface port { read: func() -> string; }".to_vec(),
        );
        files.insert(
            "wit/port.shape.json".into(),
            br#"{"cardinality":"one","streams":{},"callbacks":{}}"#.to_vec(),
        );
        shapes.push(ShapeDefinition {
            name: shape.name.clone(),
            version: shape.version,
            kind: ShapeKind::Port,
            wit: "wit/port.wit".into(),
        });
    }
    let provided = if provides {
        vec![PortDeclaration {
            shape: shape.name.clone(),
            version: shape.version,
            optional: false,
        }]
    } else {
        Vec::new()
    };
    let manifest = PackManifest {
        format: "tobkiri.pack/1".into(),
        id: PackId::parse(id).unwrap(),
        version: "1.0.0".into(),
        publisher_key: None,
        runtime: RuntimeSpec::Data,
        shapes,
        ports: Ports {
            provides: provided,
            consumes: Vec::new(),
            untyped: Vec::new(),
        },
        inputs: Vec::new(),
        flows: Vec::new(),
        overlays: Vec::new(),
        stores: Vec::new(),
        permissions: slot_permissions(),
        ui: None,
        files: file_entries(&files),
    };
    let artifact = tobkiri_manifest::build_tpk(&manifest, &files, None).unwrap();
    let verified = tobkiri_manifest::verify_tpk(&artifact, &BTreeMap::new()).unwrap();
    let path = directory.join(format!("{id}-{salt}.tpk"));
    std::fs::write(&path, artifact).unwrap();
    (verified, path.to_str().unwrap().to_string())
}
fn pin(pack: &VerifiedPack, source: &str) -> PackPin {
    PackPin {
        id: pack.manifest.id.clone(),
        version: pack.manifest.version.clone(),
        sha256: pack.hash.clone(),
        source: source.into(),
    }
}

/// A Profile that already holds the contract and one provider of the
/// single-cardinality port, plus two more Packs ready to be added.
struct Slots {
    core: Core,
    host: Arc<TestHost>,
    directory: PathBuf,
    id: ProfileId,
    head: Digest,
    one: (VerifiedPack, String),
    two: (VerifiedPack, String),
    _directory: tempfile::TempDir,
}
impl Slots {
    async fn new() -> Self {
        let temporary = tempfile::tempdir().unwrap();
        let directory = temporary.path().canonicalize().unwrap();
        let host = Arc::new(TestHost::default());
        let core = Core::open(
            CoreConfig::for_test_data_root(directory.join("data")),
            host.clone(),
        )
        .unwrap();
        let contract = slot_pack(&directory, "local.contract", "c", true, false);
        let one = slot_pack(&directory, "local.one", "v1", false, true);
        let two = slot_pack(&directory, "local.two", "v1", false, true);
        let id = ProfileId::parse("prof_slots").unwrap();
        let profile = Profile {
            format: "tobkiri.profile/1".into(),
            id: id.clone(),
            name: "Slots".into(),
            packs: vec![pin(&contract.0, &contract.1), pin(&one.0, &one.1)],
            publishers: BTreeMap::new(),
            ceiling: slot_permissions(),
            user_permissions: None,
            pack_limits: [&contract.0, &one.0]
                .iter()
                .map(|pack| (pack.manifest.id.clone(), slot_permissions()))
                .collect(),
            bindings: BTreeMap::new(),
            overlay_order: Vec::new(),
            user_overlays: Vec::new(),
            startup: tobkiri_profile::Startup {
                inputs: Vec::new(),
                ui: None,
            },
        };
        let head = core.save_profile(profile, None).await.unwrap();
        Self {
            core,
            host,
            directory,
            id,
            head,
            one,
            two,
            _directory: temporary,
        }
    }
    fn snapshot(&self) -> Snapshot {
        snapshot(&self.core, &self.host, &self.id)
    }
    /// The stored Profile with one more Pack and the usual Pack limit.
    fn with_pack(&self, pack: &(VerifiedPack, String)) -> Profile {
        let (mut profile, _) = self.core.profile(&self.id).unwrap();
        profile
            .pack_limits
            .insert(pack.0.manifest.id.clone(), slot_permissions());
        profile.packs.push(pin(&pack.0, &pack.1));
        profile
    }
    fn selecting_one(&self, mut profile: Profile) -> Profile {
        profile
            .bindings
            .insert(SLOT_BINDING.into(), self.one.0.manifest.id.clone());
        profile
    }
}

#[tokio::test]
async fn second_provider_without_a_choice_fails_keeps_the_head_and_a_bound_repair_applies() {
    let slots = Slots::new().await;
    let before = slots.snapshot();

    // The second provider of a single-cardinality port is a failed addition.
    let failed = slots
        .core
        .add_pack(&slots.id, slots.two.1.clone(), false)
        .await;
    let codes = conflict_codes(failed);
    assert!(codes.iter().any(|code| code == "provider-collision"));
    assert_eq!(slots.snapshot(), before, "the previous configuration stays");
    assert_eq!(before.0, slots.head);

    // A repair is offered, bound to the exact original revision and hashes.
    let rejected = slots.with_pack(&slots.two);
    let offer = slots
        .core
        .offer_repair(&slots.id, &rejected)
        .unwrap()
        .expect("the conflict must offer a repair");
    assert!(offer.report.repair_pack_available);
    assert!(
        offer
            .report
            .conflicts
            .iter()
            .any(|conflict| conflict.code == "provider-collision")
    );
    assert_eq!(offer.binding.head, slots.head);
    assert!(
        offer
            .binding
            .packs
            .iter()
            .any(|pin| pin.sha256 == slots.one.0.hash)
    );
    // A change without any conflict offers nothing.
    let (stored, _) = slots.core.profile(&slots.id).unwrap();
    assert!(
        slots
            .core
            .offer_repair(&slots.id, &stored)
            .unwrap()
            .is_none()
    );

    // The explicit choice repairs it through the ordinary save.
    let repaired = slots.selecting_one(rejected);
    let head = slots
        .core
        .apply_repair(&offer.binding, repaired)
        .await
        .unwrap();
    assert_ne!(head, slots.head);
    let (stored, stored_head) = slots.core.profile(&slots.id).unwrap();
    assert_eq!(stored_head, head);
    assert_eq!(stored.packs.len(), 3);
    assert!(
        slots
            .core
            .check_profile(&stored)
            .unwrap()
            .conflicts
            .is_empty()
    );
}

#[tokio::test]
async fn repair_goes_stale_when_an_original_pack_changes() {
    let slots = Slots::new().await;
    let rejected = slots.with_pack(&slots.two);
    let offer = slots
        .core
        .offer_repair(&slots.id, &rejected)
        .unwrap()
        .unwrap();
    let repaired = slots.selecting_one(rejected);

    // The original provider is updated to different bytes.
    let rebuilt = slot_pack(&slots.directory, "local.one", "v2", false, true);
    assert_ne!(rebuilt.0.hash, slots.one.0.hash);
    let updated = slots
        .core
        .add_pack(&slots.id, rebuilt.1.clone(), true)
        .await
        .unwrap();
    assert_ne!(updated, slots.head);
    let before = slots.snapshot();

    match slots.core.apply_repair(&offer.binding, repaired).await {
        Err(CoreError::Invalid(message)) => assert!(message.contains("stale"), "{message}"),
        other => panic!("a repair for old hashes must be stale: {other:?}"),
    }
    assert_eq!(slots.snapshot(), before, "a stale repair changes nothing");

    // A repair regenerated against the changed original applies.
    let (current, _) = slots.core.profile(&slots.id).unwrap();
    let fresh = slots
        .core
        .offer_repair(&slots.id, &slots.with_pack(&slots.two))
        .unwrap()
        .unwrap();
    assert_eq!(fresh.binding.head, updated);
    assert!(
        fresh
            .binding
            .packs
            .iter()
            .any(|pin| pin.sha256 == rebuilt.0.hash)
    );
    assert!(current.packs.iter().any(|pin| pin.sha256 == rebuilt.0.hash));
    let repaired = slots.selecting_one(slots.with_pack(&slots.two));
    slots
        .core
        .apply_repair(&fresh.binding, repaired)
        .await
        .unwrap();
}

#[tokio::test]
async fn repair_cannot_widen_permissions_edit_originals_or_approve_itself() {
    let slots = Slots::new().await;
    let rejected = slots.with_pack(&slots.two);
    let offer = slots
        .core
        .offer_repair(&slots.id, &rejected)
        .unwrap()
        .unwrap();
    let before = slots.snapshot();
    let honest = slots.selecting_one(rejected);

    let mut widened_ceiling = honest.clone();
    widened_ceiling
        .ceiling
        .network
        .origins
        .insert("https://wide.example".into());
    let mut widened_user = honest.clone();
    widened_user.user_permissions = Some(slot_permissions());
    let mut widened_limit = honest.clone();
    widened_limit
        .pack_limits
        .get_mut(&slots.two.0.manifest.id)
        .unwrap()
        .network
        .origins
        .insert("https://wide.example".into());
    let mut edited_original = honest.clone();
    let rebuilt = slot_pack(&slots.directory, "local.one", "edited", false, true);
    let original = edited_original
        .packs
        .iter_mut()
        .find(|existing| existing.id == slots.one.0.manifest.id)
        .unwrap();
    *original = pin(&rebuilt.0, &rebuilt.1);
    let mut removed_original = honest.clone();
    removed_original
        .packs
        .retain(|existing| existing.id != slots.one.0.manifest.id);
    for forbidden in [
        widened_ceiling,
        widened_user,
        widened_limit,
        edited_original,
        removed_original,
    ] {
        match slots.core.apply_repair(&offer.binding, forbidden).await {
            Err(CoreError::Invalid(_)) => {}
            other => panic!("an unsafe repair was not refused: {other:?}"),
        }
        assert_eq!(slots.snapshot(), before);
    }

    // The repair Pack is an ordinary Pack: its permissions are asked of the
    // trusted Host, and a denial leaves the previous configuration alone.
    let fix = slot_pack(&slots.directory, "local.fix", "v1", false, false);
    let mut with_fix = honest.clone();
    with_fix
        .pack_limits
        .insert(fix.0.manifest.id.clone(), slot_permissions());
    with_fix.packs.push(pin(&fix.0, &fix.1));
    let prompts = slots.host.recorded().len();
    slots.host.deny_all.store(true, Ordering::SeqCst);
    assert!(
        slots
            .core
            .apply_repair(&offer.binding, with_fix.clone())
            .await
            .is_err()
    );
    assert!(
        slots.host.recorded().len() > prompts,
        "the repair was never put to the trusted Host"
    );
    assert_eq!(slots.snapshot().0, before.0);
    assert_eq!(slots.snapshot().1, before.1);

    slots.host.deny_all.store(false, Ordering::SeqCst);
    slots
        .core
        .apply_repair(&offer.binding, with_fix)
        .await
        .unwrap();
    assert!(
        slots
            .host
            .recorded()
            .iter()
            .any(|prompt| matches!(prompt.effect, EffectRequest::Permissions { .. }))
    );
}
