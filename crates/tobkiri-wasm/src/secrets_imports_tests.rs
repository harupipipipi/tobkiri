//! Local supervisor custody tests: genuine Gate/Grant/SecretWork and Shared
//! cache, without a fabricated Core frame or native-generation provenance.
//! Hooks exchange metadata only around the ORIGINAL blocking body; no backend,
//! Grant, dispatch permit, completion, root or terminal decision is substituted.
use super::*;
use std::{
    collections::BTreeMap,
    sync::{
        Condvar, Mutex,
        atomic::{AtomicUsize, Ordering},
    },
    time::Duration,
};
use tobkiri_canon::ids::{PackHash, PackId, ProfileId, RequestId};
use tobkiri_gate::{
    ApprovalAnswer, ApprovalPrompt, ApprovalStore, CapacityLedger, CoreCancellation, CoreControl,
    CoreControlLimits, GateEngine, HostError, HostIo, MediatorChain, PackPolicy, PermissionSet,
    Principal, ResourceLimits, SecretPermission, TerminalDisposition, TrustedPrompt, UiEmbed,
};
use tobkiri_store::{Approvals, AuditLog, AuditQuota};
use tokio::{sync::Notify, task::JoinHandle};

const WAITERS: u64 = 8;

#[derive(Clone, Copy)]
enum Pause {
    BeforeConsume,
    Blocking,
}

struct AsyncPause {
    enabled: bool,
    arrived: Notify,
    release: Notify,
}
impl AsyncPause {
    fn new(enabled: bool) -> Self {
        Self {
            enabled,
            arrived: Notify::new(),
            release: Notify::new(),
        }
    }
    async fn wait(&self) {
        if self.enabled {
            self.arrived.notify_one();
            self.release.notified().await;
        }
    }
}
struct BlockingPause {
    enabled: bool,
    arrived: Notify,
    released: Mutex<bool>,
    release: Condvar,
}
impl BlockingPause {
    fn new(enabled: bool) -> Self {
        Self {
            enabled,
            arrived: Notify::new(),
            released: Mutex::new(false),
            release: Condvar::new(),
        }
    }
    fn wait(&self) {
        if !self.enabled {
            return;
        }
        self.arrived.notify_one();
        let mut released = self.released.lock().unwrap_or_else(|e| e.into_inner());
        while !*released {
            released = self
                .release
                .wait(released)
                .unwrap_or_else(|e| e.into_inner());
        }
    }
    fn release(&self) {
        *self.released.lock().unwrap_or_else(|e| e.into_inner()) = true;
        self.release.notify_all();
    }
}

/// Per-custody, pre-consume funded metadata. Production defaults to no hook.
/// This never receives a secret, Gate, grant, work, completion, or output.
pub(super) struct Hooks {
    pre: AsyncPause,
    before: BlockingPause,
    after: BlockingPause,
    entered: AtomicUsize,
    executed: AtomicUsize,
    // Last: independently prepaid observer metadata cannot mask the
    // original Task charge whose retirement these tests measure.
    _funding: ControlReservation,
}
impl Hooks {
    fn new(pause: Pause, funding: ControlReservation) -> Self {
        Self {
            pre: AsyncPause::new(matches!(pause, Pause::BeforeConsume)),
            before: BlockingPause::new(matches!(pause, Pause::Blocking)),
            after: BlockingPause::new(matches!(pause, Pause::Blocking)),
            entered: AtomicUsize::new(0),
            executed: AtomicUsize::new(0),
            _funding: funding,
        }
    }
    pub(super) async fn before_consume(&self) {
        self.pre.wait().await;
    }
    pub(super) fn before_blocking(&self) {
        self.entered.fetch_add(1, Ordering::AcqRel);
        self.before.wait();
    }
    pub(super) fn after_blocking(&self) {
        self.executed.fetch_add(1, Ordering::AcqRel);
        self.after.wait();
    }
    fn release_all(&self) {
        self.pre.release.notify_one();
        self.before.release();
        self.after.release();
    }
}
/// Created immediately after start; releases every guard before a panic or
/// timeout unwinds into Tokio runtime teardown, which must join blocking work.
struct ReleaseOnDrop {
    hooks: Arc<Hooks>,
    cancel: tobkiri_gate::CancellationToken,
}
impl ReleaseOnDrop {
    fn new(hooks: Arc<Hooks>, cx: &CallCx) -> Self {
        Self {
            hooks,
            cancel: cx.cancel().clone(),
        }
    }
}
impl Drop for ReleaseOnDrop {
    fn drop(&mut self) {
        // Even an assertion failure must not release a live body into key IO.
        self.cancel.cancel();
        self.hooks.release_all();
    }
}

struct Prompt;
#[async_trait::async_trait]
impl TrustedPrompt for Prompt {
    async fn prompt_approval(&self, prompt: ApprovalPrompt) -> ApprovalAnswer {
        if prompt.profile.as_str() != "prof_secret_custody"
            || prompt.request.as_str() != "req_secret_custody"
        {
            return ApprovalAnswer::Deny;
        }
        match (&prompt.principal, &prompt.effect) {
            (Principal::User, EffectRequest::Permissions { pack, hash })
                if pack.as_str() == "local.secret-custody"
                    && hash.digest() == &Digest::hash(b"secret-custody-test") =>
            {
                ApprovalAnswer::Approve
            }
            (Principal::Pack { pack, hash }, EffectRequest::Secret { name, .. })
                if pack.as_str() == "local.secret-custody"
                    && hash.digest() == &Digest::hash(b"secret-custody-test")
                    && name == "api-key" =>
            {
                ApprovalAnswer::Approve
            }
            _ => ApprovalAnswer::Deny,
        }
    }
}
#[async_trait::async_trait]
impl HostIo for Prompt {
    fn display(&self, _: &str, _: &tobkiri_canon::CanonValue) {}
    async fn terminal_read_line(&self, _: &str) -> std::result::Result<String, HostError> {
        Err(HostError("unavailable".into()))
    }
    fn terminal_write(&self, _: &str) {}
    fn embed_ui(
        &self,
        _: UiEmbed,
    ) -> std::result::Result<tobkiri_canon::ids::UiSessionId, HostError> {
        Err(HostError("unavailable".into()))
    }
    async fn pick_directory(&self, _: &str) -> Option<std::path::PathBuf> {
        None
    }
}

struct Fixture {
    // Host/physical metadata is destroyed before the temporary root.
    host: PackHost,
    gate: Arc<GateEngine>,
    approvals: Arc<Approvals>,
    cx: CallCx,
    control: CoreControl,
    _temporary: tempfile::TempDir,
}
impl Fixture {
    async fn new() -> Self {
        let temporary = tempfile::tempdir().unwrap();
        let root = Arc::new(DataRoot::open(temporary.path().canonicalize().unwrap()).unwrap());
        let profile = ProfileId::parse("prof_secret_custody").unwrap();
        let control = CoreControl::new(CoreControlLimits {
            max_waiters: WAITERS,
            ..Default::default()
        })
        .unwrap();
        let approvals = Arc::new(Approvals::open(&root, &profile).unwrap());
        let audit = Arc::new(AuditLog::open(&root, &profile, AuditQuota::default()).unwrap());
        let io = Arc::new(Prompt);
        let gate = Arc::new(GateEngine::new_with_control(
            approvals.clone(),
            audit,
            io.clone(),
            MediatorChain::default(),
            control.clone(),
        ));
        let limits = ResourceLimits {
            fuel_per_call: 1_000_000,
            memory_mib: 64,
            wall_ms: 30_000,
            table_elements: 16_384,
            output_bytes: 4096,
        };
        let pack = PackId::parse("local.secret-custody").unwrap();
        let hash = PackHash::new(Digest::hash(b"secret-custody-test"));
        let permissions = PermissionSet {
            secrets: vec![SecretPermission {
                name: "api-key".into(),
                reveal: true,
                optional: false,
            }],
            wasm: limits.clone(),
            ..Default::default()
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
        let revision = Digest::hash(b"secret-custody-revision");
        gate.register_revision(
            &profile,
            &revision,
            BTreeMap::from([((pack.clone(), hash.clone()), limits.clone())]),
        )
        .unwrap();
        let context = |principal| {
            CallCx::try_root_with_control(
                profile.clone(),
                principal,
                RequestId::parse("req_secret_custody").unwrap(),
                CoreCancellation::new_with_control(&control).unwrap(),
                Instant::now() + Duration::from_secs(30),
                limits.clone(),
                control.clone(),
            )
            .unwrap()
            .with_revision(revision.clone())
            .with_security_epoch(gate.epoch(&profile).unwrap())
        };
        gate.approve_pack_permissions(&context(Principal::User), &pack, None, &hash)
            .await
            .unwrap();
        let cx = context(Principal::Pack { pack, hash });
        let init = context(Principal::User);
        let host = PackHost::new(
            gate.clone(),
            CapacityLedger::new(),
            io,
            root,
            BTreeMap::new(),
            BTreeMap::new(),
            Vec::new(),
            BTreeMap::new(),
            &init,
        )
        .await
        .unwrap();
        Self {
            host,
            gate,
            approvals,
            cx,
            control,
            _temporary: temporary,
        }
    }
    async fn grant(&self) -> Grant {
        self.gate
            .authorize(
                &self.cx,
                EffectRequest::Secret {
                    name: "api-key".into(),
                    reveal: false,
                },
                tobkiri_store::Vault::secret_digest("api-key").unwrap(),
            )
            .await
            .unwrap()
    }
    fn start(&self, grant: Grant, cx: CallCx, pause: Pause) -> Running {
        // This tests local custody, not the call() preflight-size formula or
        // Core generation admission. Its one Task charge is admitted before
        // all test hooks/channels/retained metadata are constructed.
        let bytes = metadata_bytes::<(), ()>().unwrap()
            + self
                .host
                .shared
                .root
                .path()
                .as_os_str()
                .as_encoded_bytes()
                .len()
            + grant.id().clone_storage_bytes()
            + 240;
        let funding = Funding::try_new_precharged(
            (),
            self.control
                .reserve(ControlKind::Task, bytes as u64)
                .unwrap(),
        )
        .unwrap();
        // Fund test-only observer storage separately; keeping this Arc in
        // the test must never retain/mask the operation's original Task slot.
        let observer_bytes = std::mem::size_of::<Hooks>()
            + std::mem::size_of::<ReleaseOnDrop>()
            + 2 * std::mem::size_of::<usize>();
        let observer_funding = self
            .control
            .reserve(ControlKind::Frame, observer_bytes as u64)
            .unwrap();
        let hooks = Arc::new(Hooks::new(pause, observer_funding));
        let id = grant.id().try_clone_precharged().unwrap();
        let request = grant.effect().clone();
        let args = grant.args_digest().clone();
        let root = self.host.shared.root.as_ref().clone();
        let (output_tx, output_rx) = oneshot::channel();
        let (delivery_tx, delivery_rx) = oneshot::channel();
        let (terminal_tx, terminal_rx) = oneshot::channel();
        let caller = CallerCustody {
            output_rx,
            delivery_tx,
            terminal_rx,
            convert: (),
            funding,
        };
        let custody = SecretCustody {
            grant,
            request,
            args,
            root,
            shared: self.host.shared.clone(),
            cx: cx.clone(),
            gate: self.gate.clone(),
            id: id.try_clone_precharged().unwrap(),
            output_tx,
            delivery_rx,
            terminal_tx,
            deadline: Box::pin(tokio::time::sleep_until(cx.deadline().into())),
            cancelled: Box::pin(cx.cancel().cancelled()),
            hooks: Some(hooks.clone()),
            funding: caller.funding.clone(),
        };
        let join = tokio::spawn(supervise(custody));
        Running {
            id,
            hooks,
            caller,
            join,
        }
    }
    fn task_count(&self) -> u64 {
        self.control.usage().count(ControlKind::Task)
    }
    fn waiter_count(&self) -> u64 {
        self.control.usage().count(ControlKind::Waiter)
    }
    fn assert_consumed(&self, id: &GrantId) {
        assert_eq!(
            self.approvals.reservation_state(id).unwrap().as_deref(),
            Some("consumed")
        );
        assert!(self.approvals.terminal_delivery(id).unwrap().is_none());
    }
    fn assert_unconsumed_terminal(&self, id: &GrantId) {
        let terminal = self.approvals.terminal_delivery(id).unwrap().unwrap();
        assert!(terminal.acknowledged);
        assert_eq!(&terminal.decision.reservation.id, id);
        assert!(matches!(
            terminal.decision.disposition,
            TerminalDisposition::AbandonedBeforeUse | TerminalDisposition::ExpiredBeforeUse
        ));
        assert!(self.host.shared.vault.lock().unwrap().is_none());
    }
    fn assert_terminal(&self, id: &GrantId, outcome: Outcome) {
        let terminal = self.approvals.terminal_delivery(id).unwrap().unwrap();
        assert!(terminal.acknowledged);
        assert_eq!(&terminal.decision.reservation.id, id);
        assert_eq!(terminal.decision.outcome, outcome);
        assert_eq!(
            self.approvals.reservation_state(id).unwrap().as_deref(),
            Some("finished")
        );
    }
}
struct Running {
    id: GrantId,
    hooks: Arc<Hooks>,
    caller: CallerCustody<()>,
    join: JoinHandle<std::result::Result<(), NativeWorkError>>,
}
async fn bounded<T>(future: impl Future<Output = T>) -> T {
    tokio::time::timeout(Duration::from_secs(10), future)
        .await
        .expect("original secret supervisor handshake timed out")
}

#[tokio::test]
async fn waiter_capacity_refuses_before_consume_and_blocking_body() {
    let f = Fixture::new().await;
    let grant = f.grant().await;
    let tasks = f.task_count();
    let waiters = f.waiter_count();
    let mut held = Vec::new();
    for _ in waiters..WAITERS {
        held.push(f.control.reserve(ControlKind::Waiter, 128).unwrap());
    }
    assert_eq!(f.waiter_count(), WAITERS);
    let Running {
        id,
        hooks,
        caller,
        join,
    } = f.start(grant, f.cx.clone(), Pause::BeforeConsume);
    let release = ReleaseOnDrop::new(hooks.clone(), &f.cx);
    assert!(bounded(join).await.unwrap().is_ok());
    assert_eq!(hooks.entered.load(Ordering::Acquire), 0);
    assert_eq!(hooks.executed.load(Ordering::Acquire), 0);
    assert!(matches!(
        bounded(caller.output_rx).await.unwrap().result,
        Err(SecretFailure::Gate(GateFailure::Unavailable))
    ));
    assert!(bounded(caller.terminal_rx).await.unwrap().is_ok());
    drop(caller.delivery_tx);
    drop(caller.funding);
    f.assert_unconsumed_terminal(&id);
    assert_eq!(
        f.task_count(),
        tasks,
        "observer funding cannot retain the original Task slot"
    );
    assert_eq!(f.waiter_count(), WAITERS);
    drop(held);
    assert_eq!(f.waiter_count(), waiters);
    drop(release);
    drop(hooks);
    assert_eq!(f.task_count(), tasks);
}

#[tokio::test]
async fn pre_cancel_refuses_without_consume_or_vault_io() {
    let f = Fixture::new().await;
    let grant = f.grant().await;
    let tasks = f.task_count();
    let waiters = f.waiter_count();
    f.cx.cancel().cancel();
    let Running {
        id,
        hooks,
        caller,
        join,
    } = f.start(grant, f.cx.clone(), Pause::BeforeConsume);
    let release = ReleaseOnDrop::new(hooks.clone(), &f.cx);
    assert!(bounded(join).await.unwrap().is_ok());
    assert!(matches!(
        bounded(caller.output_rx).await.unwrap().result,
        Err(SecretFailure::Gate(GateFailure::Cancelled))
    ));
    assert!(bounded(caller.terminal_rx).await.unwrap().is_ok());
    assert_eq!(hooks.entered.load(Ordering::Acquire), 0);
    assert_eq!(hooks.executed.load(Ordering::Acquire), 0);
    drop(caller.delivery_tx);
    drop(caller.funding);
    f.assert_unconsumed_terminal(&id);
    assert_eq!(
        f.task_count(),
        tasks,
        "observer funding cannot retain the original Task slot"
    );
    assert_eq!(f.waiter_count(), waiters);
    drop(release);
    drop(hooks);
    assert_eq!(f.task_count(), tasks);
}

#[tokio::test]
async fn pending_probe_then_cancel_revalidates_original_before_consume() {
    let f = Fixture::new().await;
    let grant = f.grant().await;
    let waiters = f.waiter_count();
    let Running {
        id,
        hooks,
        caller,
        join,
    } = f.start(grant, f.cx.clone(), Pause::BeforeConsume);
    let release = ReleaseOnDrop::new(hooks.clone(), &f.cx);
    bounded(hooks.pre.arrived.notified()).await;
    assert_eq!(f.waiter_count(), waiters + 1);
    assert_eq!(
        f.approvals.reservation_state(&id).unwrap().as_deref(),
        Some("dispatched")
    );
    f.cx.cancel().cancel();
    drop(release);
    assert!(bounded(join).await.unwrap().is_ok());
    assert_eq!(hooks.entered.load(Ordering::Acquire), 0);
    assert!(matches!(
        bounded(caller.output_rx).await.unwrap().result,
        Err(SecretFailure::Gate(GateFailure::Cancelled))
    ));
    assert!(bounded(caller.terminal_rx).await.unwrap().is_ok());
    f.assert_unconsumed_terminal(&id);
    assert_eq!(f.waiter_count(), waiters);
}

#[tokio::test]
async fn pending_probe_then_original_deadline_refuses_before_consume() {
    let f = Fixture::new().await;
    let grant = f.grant().await;
    let deadline = Instant::now() + Duration::from_secs(1);
    let cx = f.cx.clone().cap_deadline(deadline);
    let Running {
        id,
        hooks,
        caller,
        join,
    } = f.start(grant, cx, Pause::BeforeConsume);
    let release = ReleaseOnDrop::new(hooks.clone(), &f.cx);
    bounded(hooks.pre.arrived.notified()).await;
    // This waits for the actual immutable test-context deadline, not a sleep
    // used to guess that another task reached a particular scheduling point.
    tokio::time::sleep_until(deadline.into()).await;
    drop(release);
    assert!(bounded(join).await.unwrap().is_ok());
    assert_eq!(hooks.entered.load(Ordering::Acquire), 0);
    assert!(matches!(
        bounded(caller.output_rx).await.unwrap().result,
        Err(SecretFailure::Gate(GateFailure::Deadline))
    ));
    assert!(bounded(caller.terminal_rx).await.unwrap().is_ok());
    f.assert_unconsumed_terminal(&id);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn same_pinned_waiter_cancel_keeps_original_join_and_funding() {
    let f = Fixture::new().await;
    let grant = f.grant().await;
    let tasks = f.task_count();
    let waiters = f.waiter_count();
    let Running {
        id,
        hooks,
        caller,
        join,
    } = f.start(grant, f.cx.clone(), Pause::Blocking);
    let release = ReleaseOnDrop::new(hooks.clone(), &f.cx);
    bounded(hooks.before.arrived.notified()).await;
    f.assert_consumed(&id);
    assert_eq!(f.waiter_count(), waiters + 1);
    assert_eq!(f.task_count(), tasks + 1);
    f.cx.cancel().cancel();
    assert!(!join.is_finished(), "cancel is not physical completion");
    hooks.before.release();
    bounded(hooks.after.arrived.notified()).await;
    assert_eq!(hooks.executed.load(Ordering::Acquire), 1);
    assert!(
        !join.is_finished(),
        "original completion is still in the blocking closure"
    );
    assert_eq!(f.task_count(), tasks + 1);
    hooks.after.release();
    let output = bounded(caller.output_rx).await.unwrap();
    assert!(matches!(
        output.result,
        Err(SecretFailure::Gate(GateFailure::Cancelled))
    ));
    // Leave delivery_tx alive and untriggered. Only the SAME pinned original
    // cancellation future can finish the supervisor's delivery select here.
    assert!(bounded(caller.terminal_rx).await.unwrap().is_ok());
    assert!(bounded(join).await.unwrap().is_ok());
    f.assert_terminal(&id, Outcome::Cancelled);
    assert!(f.host.shared.vault.lock().unwrap().is_none());
    assert_eq!(f.waiter_count(), waiters);
    assert_eq!(
        f.task_count(),
        tasks + 1,
        "caller still owns the original handoff funding"
    );
    drop(caller.delivery_tx);
    drop(caller.funding);
    assert_eq!(
        f.task_count(),
        tasks,
        "observer funding cannot mask Task retirement"
    );
    drop(release);
    drop(hooks);
    assert_eq!(f.task_count(), tasks);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn observer_drop_does_not_retire_original_blocking_capture() {
    let f = Fixture::new().await;
    let grant = f.grant().await;
    let tasks = f.task_count();
    let Running {
        id,
        hooks,
        caller,
        join,
    } = f.start(grant, f.cx.clone(), Pause::Blocking);
    let release = ReleaseOnDrop::new(hooks.clone(), &f.cx);
    bounded(hooks.before.arrived.notified()).await;
    f.assert_consumed(&id);
    drop(caller);
    assert!(
        !join.is_finished(),
        "observer loss is not physical completion"
    );
    assert_eq!(
        f.task_count(),
        tasks + 1,
        "blocking owner retains the only original charge"
    );
    // Cancellation is used only to finish this key-free test after proving
    // observer loss alone did not release the original blocking custody.
    f.cx.cancel().cancel();
    hooks.before.release();
    bounded(hooks.after.arrived.notified()).await;
    assert!(!join.is_finished());
    assert_eq!(f.task_count(), tasks + 1);
    hooks.after.release();
    assert!(bounded(join).await.unwrap().is_ok());
    f.assert_terminal(&id, Outcome::Cancelled);
    assert!(f.host.shared.vault.lock().unwrap().is_none());
    assert_eq!(hooks.entered.load(Ordering::Acquire), 1);
    assert_eq!(hooks.executed.load(Ordering::Acquire), 1);
    assert_eq!(
        f.task_count(),
        tasks,
        "observer funding cannot mask Task retirement"
    );
    drop(release);
    drop(hooks);
    assert_eq!(f.task_count(), tasks);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn original_deadline_does_not_complete_or_forget_blocking_join() {
    let f = Fixture::new().await;
    let grant = f.grant().await;
    let tasks = f.task_count();
    let deadline = Instant::now() + Duration::from_secs(1);
    let cx = f.cx.clone().cap_deadline(deadline);
    let Running {
        id,
        hooks,
        caller,
        join,
    } = f.start(grant, cx, Pause::Blocking);
    let release = ReleaseOnDrop::new(hooks.clone(), &f.cx);
    bounded(hooks.before.arrived.notified()).await;
    f.assert_consumed(&id);
    tokio::time::sleep_until(deadline.into()).await;
    assert!(!join.is_finished(), "deadline is not physical completion");
    assert_eq!(f.task_count(), tasks + 1);
    hooks.before.release();
    bounded(hooks.after.arrived.notified()).await;
    assert!(
        !join.is_finished(),
        "original completion still belongs to the blocking closure"
    );
    assert_eq!(f.task_count(), tasks + 1);
    hooks.after.release();
    assert!(matches!(
        bounded(caller.output_rx).await.unwrap().result,
        Err(SecretFailure::Gate(GateFailure::Deadline))
    ));
    let terminal = bounded(caller.terminal_rx).await.unwrap();
    assert!(matches!(terminal, Ok(()) | Err(GateFailure::Deadline)));
    assert!(bounded(join).await.unwrap().is_ok());
    f.assert_terminal(
        &id,
        Outcome::Failed {
            code: "deadline-failure-unknown".into(),
        },
    );
    let decision = f.approvals.terminal_delivery(&id).unwrap().unwrap();
    assert_eq!(
        decision.decision.disposition,
        TerminalDisposition::ExpiredCleanupUnknown
    );
    assert!(f.host.shared.vault.lock().unwrap().is_none());
    drop(caller.delivery_tx);
    drop(caller.funding);
    assert_eq!(f.task_count(), tasks);
    drop(release);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn actual_cache_error_and_lost_terminal_receiver_keep_same_ack_and_funding() {
    let f = Fixture::new().await;
    // This changes only a cache metadata mutex, never a key provider/backend.
    // The actual SecretWork must take its poisoned-lock error before cold IO.
    let shared = f.host.shared.clone();
    assert!(
        std::thread::spawn(move || {
            let _guard = shared.vault.lock().unwrap();
            panic!("test-only cache mutex poison");
        })
        .join()
        .is_err()
    );
    let grant = f.grant().await;
    let tasks = f.task_count();
    let queues = f.control.usage().count(ControlKind::Queue);
    let Running {
        id,
        hooks,
        caller,
        join,
    } = f.start(grant, f.cx.clone(), Pause::Blocking);
    let release = ReleaseOnDrop::new(hooks.clone(), &f.cx);
    bounded(hooks.before.arrived.notified()).await;
    f.assert_consumed(&id);
    hooks.before.release();
    bounded(hooks.after.arrived.notified()).await;
    assert_eq!(hooks.executed.load(Ordering::Acquire), 1);
    assert!(!join.is_finished());
    assert_eq!(f.task_count(), tasks + 1);
    assert_eq!(
        f.control.usage().count(ControlKind::Queue),
        queues + 1,
        "actual consumed permit/completion is still retained"
    );
    hooks.after.release();
    let output = bounded(caller.output_rx).await.unwrap();
    let result = output.result.map(|_| ());
    assert!(result.is_err());
    let selected = disposition(&result, &f.cx);
    assert_eq!(selected, SecretDisposition::Failed(FailureCode::HostEffect));
    drop(caller.terminal_rx);
    caller.delivery_tx.send(selected).unwrap();
    assert!(bounded(join).await.unwrap().is_ok());
    f.assert_terminal(&id, selected.into_outcome());
    assert_eq!(f.control.usage().count(ControlKind::Queue), queues);
    assert_eq!(
        f.task_count(),
        tasks + 1,
        "caller branch still retains original funding"
    );
    drop(caller.funding);
    assert_eq!(f.task_count(), tasks);
    let cache = f
        .host
        .shared
        .vault
        .lock()
        .unwrap_or_else(|error| error.into_inner());
    assert!(cache.is_none());
    drop(cache);
    drop(release);
}

#[test]
fn handoff_failure_and_disposition_types_are_heap_free() {
    assert!(!std::mem::needs_drop::<GateFailure>());
    assert!(!std::mem::needs_drop::<SecretFailure>());
    assert!(!std::mem::needs_drop::<FailureCode>());
    assert!(!std::mem::needs_drop::<SecretDisposition>());
    assert_eq!(std::mem::size_of::<GateFailure>(), 1);
    assert!(std::mem::size_of::<SecretFailure>() <= 2);
    assert!(std::mem::size_of::<SecretDisposition>() <= 2);
}

#[test]
fn typed_sender_projection_preserves_only_closed_error_kinds() {
    let gate = [
        (GateError::Cancelled, GateFailure::Cancelled),
        (GateError::Deadline, GateFailure::Deadline),
        (GateError::Limit(String::new()), GateFailure::Limit),
        (GateError::Invalid(String::new()), GateFailure::Invalid),
        (GateError::Denied(String::new()), GateFailure::Denied),
        (GateError::ApprovalRequired, GateFailure::ApprovalRequired),
        (
            GateError::Persistence(String::new()),
            GateFailure::Unavailable,
        ),
        (
            GateError::AuditFailure {
                denial: Box::new(GateError::Denied(String::new())),
                audit: Box::new(GateError::Persistence(String::new())),
            },
            GateFailure::Unavailable,
        ),
    ];
    for (source, expected) in gate {
        assert_eq!(GateFailure::from_gate(source), expected);
    }
    let store = [
        (StoreError::NotFound, SecretFailure::NotFound),
        (StoreError::CommitUnknown, SecretFailure::CommitUnknown),
        (StoreError::PendingDataMove, SecretFailure::PendingDataMove),
        (StoreError::Denied(String::new()), SecretFailure::Denied),
        (StoreError::Invalid(String::new()), SecretFailure::Invalid),
        (StoreError::Canon(String::new()), SecretFailure::Invalid),
        (StoreError::Quota, SecretFailure::Quota),
        (
            StoreError::Unavailable(String::new()),
            SecretFailure::Unavailable,
        ),
        (
            StoreError::Gate(GateError::Deadline),
            SecretFailure::Gate(GateFailure::Deadline),
        ),
    ];
    for (source, expected) in store {
        assert_eq!(SecretFailure::from_store(source), expected);
    }
}

#[tokio::test]
async fn closed_delivery_preserves_sealed_failure_and_deadline_priority() {
    let f = Fixture::new().await;
    let expired = f.cx.clone().cap_deadline(Instant::now());
    f.cx.cancel().cancel();
    let ordinary: std::result::Result<(), SecretFailure> = Err(SecretFailure::Unavailable);
    assert_eq!(
        disposition(&ordinary, &expired),
        SecretDisposition::Failed(FailureCode::Deadline)
    );
    let commit: std::result::Result<(), SecretFailure> = Err(SecretFailure::CommitUnknown);
    assert_eq!(
        disposition(&commit, &expired),
        SecretDisposition::Failed(FailureCode::StorageCommitUnknown)
    );
    let pending: std::result::Result<(), SecretFailure> = Err(SecretFailure::PendingDataMove);
    assert_eq!(
        disposition(&pending, &expired),
        SecretDisposition::Failed(FailureCode::StoragePendingMove)
    );
    assert_eq!(disposition(&Ok(()), &expired), SecretDisposition::Succeeded);
}

#[test]
fn conversion_projection_discards_untyped_context_and_preserves_sealed_kinds() {
    assert_eq!(
        SecretFailure::from_conversion(StoreError::CommitUnknown.into()),
        SecretFailure::CommitUnknown
    );
    assert_eq!(
        SecretFailure::from_conversion(StoreError::PendingDataMove.into()),
        SecretFailure::PendingDataMove
    );
    assert_eq!(
        SecretFailure::from_conversion(GateError::Cancelled.into()),
        SecretFailure::Gate(GateFailure::Cancelled)
    );
    // StepError has no typed kind API; neither code nor message is inspected.
    assert_eq!(
        SecretFailure::from_conversion(
            StepError::new("invalid", "secret result mode mismatch").into()
        ),
        SecretFailure::Unavailable
    );
    assert_eq!(
        SecretFailure::from_conversion(anyhow::Error::msg("fixed test-only conversion error")),
        SecretFailure::Unavailable
    );
    assert!(matches!(
        failure(SecretFailure::CommitUnknown).downcast_ref::<StoreError>(),
        Some(StoreError::CommitUnknown)
    ));
    assert!(matches!(
        failure(SecretFailure::PendingDataMove).downcast_ref::<StoreError>(),
        Some(StoreError::PendingDataMove)
    ));
    assert!(matches!(
        failure(SecretFailure::Gate(GateFailure::ApprovalRequired)).downcast_ref::<GateError>(),
        Some(GateError::ApprovalRequired)
    ));
}

#[test]
fn requested_layout_formula_accounts_for_payload_alignment_and_overflow() {
    #[repr(align(64))]
    struct Aligned([u8; 64]);
    let small = channel_bytes::<u8>().unwrap();
    let large = channel_bytes::<[u8; 4096]>().unwrap();
    assert!(large >= small + 4095);
    assert_eq!(Aligned([0; 64]).0.len(), 64);
    let aligned = channel_bytes::<Aligned>().unwrap();
    assert!(aligned >= std::mem::size_of::<Aligned>());
    assert_eq!(aligned % 64, 0);
    assert!(
        metadata_bytes::<[u8; 4096], ()>().unwrap() >= metadata_bytes::<(), ()>().unwrap() + 4095
    );
    let maximum = Layout::from_size_align(isize::MAX as usize, 1).unwrap();
    assert!(c_layout(&[maximum, Layout::new::<u8>()]).is_none());
    assert!(rust_layout_bound(&[maximum, Layout::new::<u8>()]).is_none());
}

#[test]
fn fixed_error_layout_covers_closed_receivers_and_funding_block() {
    let bytes = metadata_bytes::<(), ()>().unwrap();
    assert!(bytes >= Funding::storage_bytes());
    assert!(fixed_error_bytes().unwrap() >= std::mem::size_of::<GateError>());
    assert!(fixed_error_bytes().unwrap() >= std::mem::size_of::<StepError>());
    assert!(fixed_error_bytes().unwrap() >= std::mem::size_of::<StoreError>());
    assert!(blocking_task_bytes(blocking_body).unwrap() >= std::mem::size_of::<SecretCompletion>());
    assert!(supervisor_bytes(supervise) >= std::mem::size_of::<SecretCustody>());
}
