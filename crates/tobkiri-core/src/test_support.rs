use std::{
    collections::BTreeMap,
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
    },
};
use tobkiri_canon::{
    AllocationBudget, AllocationError, AllocationRequest, CanonValue, CoreValue, Digest,
    ids::{OpName, ShapeRef},
};

/// Pure source tests have no guest invocation. Their fixture values still carry
/// a finite allocation owner, including every retained array and event graph.
#[derive(Default)]
struct FixtureBudget(AtomicU64);
impl AllocationBudget for FixtureBudget {
    fn reserve(&self, request: AllocationRequest) -> Result<(), AllocationError> {
        if request.depth > 64 || request.elements > 65_536 || request.work > 1_048_576 {
            return Err(AllocationError::Limit);
        }
        self.0
            .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |used| {
                used.checked_add(request.bytes)
                    .filter(|next| *next <= 4 * 1024 * 1024)
            })
            .map(|_| ())
            .map_err(|_| AllocationError::Limit)
    }
    fn release(&self, bytes: u64) {
        self.0.fetch_sub(bytes, Ordering::SeqCst);
    }
}
pub(crate) fn value_budget() -> Arc<dyn AllocationBudget> {
    Arc::new(FixtureBudget::default())
}
pub(crate) fn value(value: &CanonValue) -> CoreValue {
    CoreValue::try_from_borrowed(value, &value_budget()).unwrap()
}
pub(crate) fn batch(values: impl ExactSizeIterator<Item = CoreValue>) -> CoreValue {
    CoreValue::array_shared(values.map(Ok), &value_budget()).unwrap()
}
use tobkiri_gate::{AuditEvent, GateError};

/// Per-fixture timing only: no authority, source receipt or completion proof.
#[derive(Default)]
pub(crate) struct AuditTiming {
    calls: AtomicU64,
    total_micros: AtomicU64,
    max_micros: AtomicU64,
}

impl AuditTiming {
    fn measure<T>(&self, append: impl FnOnce() -> T) -> T {
        let start = std::time::Instant::now();
        let result = append();
        let micros = u64::try_from(start.elapsed().as_micros()).unwrap_or(u64::MAX);
        self.calls.fetch_add(1, Ordering::Relaxed);
        self.total_micros.fetch_add(micros, Ordering::Relaxed);
        self.max_micros.fetch_max(micros, Ordering::Relaxed);
        result
    }

    /// Observe the unchanged real close. The heartbeat is stopped and joined
    /// before returning its original result; dropping this observer aborts the
    /// heartbeat via JoinSet. No source/physical receipt is polled separately.
    pub(crate) async fn observe_close(
        &self,
        close: impl std::future::Future<Output = Result<(), crate::CoreError>>,
    ) -> Result<(), crate::CoreError> {
        let (started, ready) = tokio::sync::oneshot::channel();
        let (stop, mut stopped) = tokio::sync::oneshot::channel();
        let mut heartbeat = tokio::task::JoinSet::new();
        heartbeat.spawn(async move {
            let mut interval = tokio::time::interval(std::time::Duration::from_millis(10));
            interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
            let mut last = std::time::Instant::now();
            let mut maximum = std::time::Duration::ZERO;
            let _ = started.send(());
            loop {
                tokio::select! {
                    biased;
                    _ = &mut stopped => return maximum.max(last.elapsed()),
                    _ = interval.tick() => {
                        let now = std::time::Instant::now();
                        maximum = maximum.max(now.duration_since(last));
                        last = now;
                    }
                }
            }
        });
        let heartbeat_started = ready.await.is_ok();
        let before_calls = self.calls.load(Ordering::Relaxed);
        let before_total = self.total_micros.load(Ordering::Relaxed);
        self.max_micros.store(0, Ordering::Relaxed);
        let start = std::time::Instant::now();
        let result = close.await;
        let elapsed = start.elapsed();
        let calls = self
            .calls
            .load(Ordering::Relaxed)
            .saturating_sub(before_calls);
        let total = self
            .total_micros
            .load(Ordering::Relaxed)
            .saturating_sub(before_total);
        let maximum = self.max_micros.load(Ordering::Relaxed);
        let _ = stop.send(());
        let poll_gap = heartbeat.join_next().await.and_then(Result::ok);
        // One bounded aggregate line, with no IDs, payloads or error text.
        // Timing correlates scheduler pressure; it does not prove cleanup.
        let _ = std::io::Write::write_fmt(
            &mut std::io::stderr(),
            format_args!(
                "CLEANUP_TIMING close_ok={} close_us={} audit_calls={} audit_total_us={} audit_max_us={} heartbeat_ok={} max_poll_gap_us={}\n",
                result.is_ok(),
                elapsed.as_micros(),
                calls,
                total,
                maximum,
                heartbeat_started && poll_gap.is_some(),
                poll_gap.map_or(0, |gap| gap.as_micros()),
            ),
        );
        result
    }
}

pub(crate) fn timed_audit_sink(
    audit: Arc<tobkiri_store::AuditLog>,
    timing: Arc<AuditTiming>,
) -> Arc<dyn tobkiri_gate::AuditSink> {
    struct TimedAuditSink {
        audit: Arc<tobkiri_store::AuditLog>,
        timing: Arc<AuditTiming>,
    }
    impl tobkiri_gate::AuditSink for TimedAuditSink {
        fn append(&self, event: AuditEvent) -> Result<(), GateError> {
            self.timing
                .measure(|| tobkiri_gate::AuditSink::append(self.audit.as_ref(), event))
        }
        fn append_idempotent(&self, id: &Digest, event: AuditEvent) -> Result<(), GateError> {
            self.timing.measure(|| {
                tobkiri_gate::AuditSink::append_idempotent(self.audit.as_ref(), id, event)
            })
        }
    }
    Arc::new(TimedAuditSink { audit, timing })
}

pub(crate) fn append_once(
    delivered: &Mutex<BTreeMap<Digest, Vec<u8>>>,
    id: &Digest,
    event: &AuditEvent,
    append: impl FnOnce() -> Result<(), GateError>,
) -> Result<(), GateError> {
    let bytes = serde_json::to_vec(event)
        .map_err(|_| GateError::Persistence("test event encoding failed".into()))?;
    let mut delivered = delivered
        .lock()
        .map_err(|_| GateError::Persistence("test audit lock poisoned".into()))?;
    if let Some(prior) = delivered.get(id) {
        if prior != &bytes {
            return Err(GateError::Denied("event ID content conflict".into()));
        }
        return Ok(());
    }
    append()?;
    delivered.insert(id.clone(), bytes);
    Ok(())
}
pub(crate) fn register_views(
    gate: &tobkiri_gate::GateEngine,
    compiled: &tobkiri_profile::CompiledProfile,
) {
    gate.register_operation_views(
        &compiled.profile.id,
        &compiled.plan_digest,
        compiled
            .views
            .iter()
            .map(|(reference, view)| (reference.clone(), view.functions.keys().cloned().collect()))
            .collect(),
    )
    .unwrap();
}

/// Small real component used by Core tests, without an external toolchain or
/// checked-in opaque fixture. Its manifest, WIT, Flow and bytes are verified by
/// the same path as a normal Pack before the Profile compiler sees them.
pub(crate) fn runtime_pack(
    directory: &std::path::Path,
    namespace: &str,
    permissions: tobkiri_gate::PermissionSet,
    ui: bool,
) -> tobkiri_manifest::VerifiedPack {
    use tobkiri_canon::ids::PackId;
    use tobkiri_manifest::{
        FileEntry, InputDeclaration, PackManifest, PortDeclaration, Ports, RuntimeSpec,
        ShapeDefinition, ShapeKind, UiDefinition,
    };
    use tobkiri_profile::{ErrorPolicy, FlowDocument, OverlayAccess, Step, StepKind, Trigger};
    let id = PackId::parse(&format!("local.{namespace}")).unwrap();
    let input = ShapeRef::parse(&format!("local.{namespace}.input@1.0")).unwrap();
    let endpoint = ShapeRef::parse(&format!("local.{namespace}.turn@1.0")).unwrap();
    let wit = format!(
        "package local:{namespace}@1.0.0; interface input {{ use tobkiri:core/types@1.0.0.{{json, stream-handle, error}}; read: func() -> u32; echo: func(value: u32) -> u32; wait: func() -> u32; %stream: func() -> result<stream-handle, error>; }} interface turn {{ run: func(); }}"
    );
    let component = wat::parse_str(format!(
        r#"(component
            (import "tobkiri:core/clock@1.0.0" (instance $clock
                (export "now-ms" (func (result u64)))))
            (alias export $clock "now-ms" (func $now-ms))
            (core func $now-ms-lowered (canon lower (func $now-ms)))
            (type $error-code (enum "denied" "approval-required" "cancelled" "not-found" "invalid" "limit-exceeded" "conflict" "unavailable" "internal"))
            (type $error (record (field "code" $error-code) (field "message" string)))
            (type $init-result (result (error $error)))
            (type $stream-handle (record (field "id" u64)))
            (type $stream-result (result $stream-handle (error $error)))
            (type $frames (list string))
            (type $pull-result (result $frames (error $error)))
            (core module $m
                (import "clock" "now-ms" (func $now-ms (result i64)))
                (memory (export "memory") 1 1)
                (global $remaining (mut i32) (i32.const 0))
                (global $next-id (mut i64) (i64.const 0))
                (data (i32.const 256) "null")
                (func (export "realloc") (param i32 i32 i32 i32) (result i32) i32.const 1024)
                (func (export "init") (param i32 i32) (result i32) i32.const 0)
                (func (export "shutdown"))
                (func (export "read") (result i32) i32.const 42)
                (func (export "echo") (param i32) (result i32) local.get 0)
                (func (export "wait") (result i32)
                    call $now-ms drop
                    (loop $again br $again) i32.const 0)
                (func (export "stream") (result i32)
                    i32.const 3 global.set $remaining
                    i32.const 0 i32.const 0 i32.store
                    global.get $next-id i64.const 1 i64.add global.set $next-id
                    i32.const 8 global.get $next-id i64.store i32.const 0)
                (func (export "pull") (param i64) (param $max i32) (result i32)
                    (local $count i32) (local $at i32)
                    global.get $remaining local.get $max i32.lt_u
                    if (result i32) global.get $remaining else local.get $max end
                    local.set $count
                    global.get $remaining local.get $count i32.sub global.set $remaining
                    i32.const 0 i32.const 0 i32.store
                    i32.const 4 i32.const 64 i32.store
                    i32.const 8 local.get $count i32.store
                    (block $done (loop $next
                        local.get $at local.get $count i32.ge_u br_if $done
                        i32.const 64 local.get $at i32.const 8 i32.mul i32.add i32.const 256 i32.store
                        i32.const 68 local.get $at i32.const 8 i32.mul i32.add i32.const 4 i32.store
                        local.get $at i32.const 1 i32.add local.set $at br $next))
                    i32.const 0)
                (func (export "cancel") (param i64) i32.const 0 global.set $remaining))
            (core instance $i (instantiate $m
                (with "clock" (instance (export "now-ms" (func $now-ms-lowered))))))
            (alias core export $i "memory" (core memory $memory))
            (alias core export $i "realloc" (core func $realloc))
            (alias core export $i "init" (core func $init))
            (alias core export $i "shutdown" (core func $shutdown))
            (alias core export $i "read" (core func $read))
            (alias core export $i "echo" (core func $echo))
            (alias core export $i "wait" (core func $wait))
            (alias core export $i "stream" (core func $stream))
            (alias core export $i "pull" (core func $pull))
            (alias core export $i "cancel" (core func $cancel))
            (func $read-value (result u32) (canon lift (core func $read)))
            (func $echo-value (param "value" u32) (result u32) (canon lift (core func $echo)))
            (func $wait-value (result u32) (canon lift (core func $wait)))
            (func $stream-value (result $stream-result) (canon lift (core func $stream) (memory $memory) (realloc $realloc)))
            (func $pull-value (param "local-id" u64) (param "max" u32) (result $pull-result) (canon lift (core func $pull) (memory $memory) (realloc $realloc)))
            (func $cancel-value (param "local-id" u64) (canon lift (core func $cancel)))
            (func $init-value (param "config" string) (result $init-result) (canon lift (core func $init) (memory $memory) (realloc $realloc)))
            (func $shutdown-value (canon lift (core func $shutdown)))
            (instance $types
                (export "error-code" (type $error-code))
                (export "error" (type $error))
                (export "stream-handle" (type $stream-handle)))
            (export "tobkiri:core/types@1.0.0" (instance $types))
            (instance $lifecycle
                (export "init" (func $init-value))
                (export "shutdown" (func $shutdown-value)))
            (export "tobkiri:core/lifecycle@1.0.0" (instance $lifecycle))
            (instance $stream-source
                (export "pull" (func $pull-value))
                (export "cancel" (func $cancel-value)))
            (export "tobkiri:core/stream-source@1.0.0" (instance $stream-source))
            (instance $input
                (export "read" (func $read-value))
                (export "echo" (func $echo-value))
                (export "wait" (func $wait-value))
                (export "stream" (func $stream-value)))
            (export "local:{namespace}/input@1.0.0" (instance $input)))"#
    ))
    .unwrap();
    let flow = FlowDocument {
        format: "tobkiri.flow/1".into(),
        id: "main".into(),
        implements: Some(endpoint.clone()),
        trigger: Trigger {
            input: input.clone(),
        },
        loop_limits: "profile-default".into(),
        steps: vec![Step {
            id: Some("echo".into()),
            role: None,
            overlay: OverlayAccess::Closed,
            out: None,
            stream: None,
            kind: StepKind::Call(tobkiri_profile::PortCall {
                shape: input.clone(),
                op: OpName::parse("echo").unwrap(),
                args: CanonValue::Obj(BTreeMap::from([(
                    "value".into(),
                    CanonValue::Str("$input".into()),
                )])),
            }),
        }],
        on_error: ErrorPolicy::default(),
    };
    let mut files: BTreeMap<String, Vec<u8>> = BTreeMap::from([
        ("component.wasm".into(), component),
        ("wit/fixture.wit".into(), wit.into_bytes()),
        (
            "wit/input.shape.json".into(),
            br#"{"cardinality":"one","streams":{"stream":"json"},"callbacks":{}}"#.to_vec(),
        ),
        (
            "wit/turn.shape.json".into(),
            br#"{"cardinality":"one","streams":{},"callbacks":{}}"#.to_vec(),
        ),
        ("flows/main.json".into(), serde_json::to_vec(&flow).unwrap()),
    ]);
    if ui {
        files.insert("ui/main.html".into(), b"verified".to_vec());
    }
    let manifest = PackManifest {
        format: "tobkiri.pack/1".into(),
        id: id.clone(),
        version: "1.0.0".into(),
        publisher_key: None,
        runtime: RuntimeSpec::Wasm {
            component: "component.wasm".into(),
            toolchain: "wat-test-fixture/1".into(),
            config: None,
        },
        shapes: [
            (input.clone(), ShapeKind::Port),
            (endpoint, ShapeKind::Flow),
        ]
        .into_iter()
        .map(|(reference, kind)| ShapeDefinition {
            name: reference.name,
            version: reference.version,
            kind,
            wit: "wit/fixture.wit".into(),
        })
        .collect(),
        ports: Ports {
            provides: vec![PortDeclaration {
                shape: input.name.clone(),
                version: input.version,
                optional: false,
            }],
            consumes: vec![PortDeclaration {
                shape: input.name.clone(),
                version: input.version,
                optional: false,
            }],
            untyped: Vec::new(),
        },
        inputs: vec![InputDeclaration {
            exact_input_view: input.clone(),
            operation: OpName::parse("read").unwrap(),
        }],
        flows: vec!["flows/main.json".into()],
        overlays: Vec::new(),
        stores: Vec::new(),
        permissions,
        ui: ui.then(|| UiDefinition {
            entry: "ui/main.html".into(),
            ports_used: vec![input],
        }),
        files: files
            .iter()
            .map(|(path, bytes)| FileEntry {
                path: path.clone(),
                sha256: Digest::hash(bytes),
                size: bytes.len() as u64,
            })
            .collect(),
    };
    let artifact = tobkiri_manifest::build_tpk(&manifest, &files, None).unwrap();
    let verified = tobkiri_manifest::verify_tpk(&artifact, &BTreeMap::new()).unwrap();
    std::fs::write(directory.join(format!("{namespace}.tpk")), artifact).unwrap();
    verified
}

pub(crate) fn permissions(namespaces: &[&str]) -> tobkiri_gate::PermissionSet {
    tobkiri_gate::PermissionSet {
        ports: namespaces
            .iter()
            .flat_map(|namespace| {
                [
                    format!("local.{namespace}.input@1.0"),
                    format!("local.{namespace}.turn@1.0"),
                ]
            })
            .collect(),
        wasm: tobkiri_gate::ResourceLimits {
            fuel_per_call: 10_000_000,
            memory_mib: 8,
            wall_ms: 1000,
            table_elements: 1024,
            output_bytes: 1024 * 1024,
        },
        loops: tobkiri_gate::LoopLimits {
            max_iterations: 8,
            max_wall_ms: 1000,
            max_cost_units: 100,
        },
        ..Default::default()
    }
}

pub(crate) fn runtime_profile(
    directory: &std::path::Path,
    packs: &[tobkiri_manifest::VerifiedPack],
    permissions: tobkiri_gate::PermissionSet,
    ui: bool,
) -> tobkiri_profile::Profile {
    use tobkiri_profile::{InputEntry, PackPin, Profile, Startup};
    Profile {
        format: "tobkiri.profile/1".into(),
        id: tobkiri_canon::ids::ProfileId::parse("prof_fixture").unwrap(),
        name: "Core fixture".into(),
        packs: packs
            .iter()
            .map(|pack| PackPin {
                id: pack.manifest.id.clone(),
                version: pack.manifest.version.clone(),
                sha256: pack.hash.clone(),
                source: directory
                    .join(format!(
                        "{}.tpk",
                        pack.manifest.id.as_str().strip_prefix("local.").unwrap()
                    ))
                    .to_str()
                    .unwrap()
                    .into(),
            })
            .collect(),
        publishers: BTreeMap::new(),
        ceiling: permissions.clone(),
        user_permissions: None,
        pack_limits: packs
            .iter()
            .map(|pack| (pack.manifest.id.clone(), permissions.clone()))
            .collect(),
        bindings: BTreeMap::new(),
        overlay_order: Vec::new(),
        user_overlays: Vec::new(),
        startup: Startup {
            inputs: packs
                .iter()
                .flat_map(|pack| {
                    pack.manifest.inputs.iter().map(|entry| InputEntry {
                        source_pack: pack.manifest.id.clone(),
                        exact_input_view: entry.exact_input_view.clone(),
                        operation: entry.operation.clone(),
                    })
                })
                .collect(),
            ui: ui.then(|| packs[0].manifest.id.clone()),
        },
    }
}

pub(crate) struct RuntimeFixture {
    pub core: crate::Core,
    pub profile: tobkiri_profile::Profile,
    pub root: tobkiri_store::DataRoot,
    _directory: tempfile::TempDir,
}

pub(crate) struct RuntimeHost;
#[async_trait::async_trait]
impl tobkiri_gate::TrustedPrompt for RuntimeHost {
    async fn prompt_approval(
        &self,
        _: tobkiri_gate::ApprovalPrompt,
    ) -> tobkiri_gate::ApprovalAnswer {
        tobkiri_gate::ApprovalAnswer::Approve
    }
}
#[async_trait::async_trait]
impl tobkiri_gate::HostIo for RuntimeHost {
    fn display(&self, _: &str, _: &CanonValue) {}
    async fn terminal_read_line(&self, _: &str) -> Result<String, tobkiri_gate::HostError> {
        Err(tobkiri_gate::HostError("unavailable".into()))
    }
    fn terminal_write(&self, _: &str) {}
    fn embed_ui(
        &self,
        _: tobkiri_gate::UiEmbed,
    ) -> Result<tobkiri_canon::ids::UiSessionId, tobkiri_gate::HostError> {
        Ok(tobkiri_canon::ids::UiSessionId::parse("host_fixture").unwrap())
    }
    async fn pick_directory(&self, _: &str) -> Option<std::path::PathBuf> {
        None
    }
}
impl RuntimeFixture {
    pub(crate) async fn new(namespaces: &[&str], ui: bool) -> Self {
        Self::with_permissions(namespaces, ui, permissions(namespaces)).await
    }
    pub(crate) async fn with_permissions(
        namespaces: &[&str],
        ui: bool,
        permissions: tobkiri_gate::PermissionSet,
    ) -> Self {
        use tobkiri_gate::ApprovalStore;
        let directory = tempfile::tempdir().unwrap();
        let physical = directory.path().canonicalize().unwrap();
        let packs = namespaces
            .iter()
            .enumerate()
            .map(|(i, namespace)| {
                runtime_pack(&physical, namespace, permissions.clone(), ui && i == 0)
            })
            .collect::<Vec<_>>();
        let profile = runtime_profile(&physical, &packs, permissions.clone(), ui);
        let core = crate::Core::open(
            crate::CoreConfig::for_test_data_root(physical.join("data")),
            Arc::new(RuntimeHost),
        )
        .unwrap();
        core.save_profile(profile.clone(), None).await.unwrap();
        let root = tobkiri_store::DataRoot::open(physical.join("data")).unwrap();
        let approvals = tobkiri_store::Approvals::open(&root, &profile.id).unwrap();
        for pack in &packs {
            approvals
                .set_permissions(&profile.id, &pack.manifest.id, &pack.hash, &permissions)
                .unwrap();
        }
        Self {
            core,
            profile,
            root,
            _directory: directory,
        }
    }
    pub(crate) async fn session(&self) -> crate::Session {
        self.core.start(&self.profile.id).await.unwrap()
    }
    /// Wire the actual internal factory with an explicit test mediator. This
    /// does not invent a frame, admission receipt, provider bridge or value
    /// sponsor; each of those is minted by its production implementation.
    pub(crate) async fn mediated_session(
        &self,
        mediators: tobkiri_gate::MediatorChain,
    ) -> crate::Session {
        use tobkiri_gate::{CallCx, GateEngine, Principal};
        let packs = self
            .profile
            .packs
            .iter()
            .map(|pin| {
                tobkiri_manifest::verify_tpk(&std::fs::read(&pin.source).unwrap(), &BTreeMap::new())
                    .unwrap()
            })
            .collect::<Vec<_>>();
        let compiled = Arc::new(tobkiri_profile::compile(&self.profile, &packs).unwrap());
        let approvals =
            Arc::new(tobkiri_store::Approvals::open(&self.root, &self.profile.id).unwrap());
        let audit = Arc::new(
            tobkiri_store::AuditLog::open(
                &self.root,
                &self.profile.id,
                tobkiri_store::AuditQuota::default(),
            )
            .unwrap(),
        );
        let host = Arc::new(RuntimeHost);
        let gate = Arc::new(GateEngine::new(
            approvals,
            audit.clone(),
            host.clone(),
            mediators,
        ));
        crate::core::register_policies(&gate, &self.profile, &packs).unwrap();
        register_views(&gate, &compiled);
        let bindings = compiled
            .bindings
            .iter()
            .map(|(view, providers)| {
                (
                    view.clone(),
                    providers
                        .iter()
                        .map(|provider| {
                            let pack = packs
                                .iter()
                                .find(|pack| &pack.manifest.id == provider)
                                .unwrap();
                            let declaration = pack
                                .manifest
                                .ports
                                .provides
                                .iter()
                                .find(|declaration| {
                                    declaration.shape == view.name
                                        && declaration.version == view.version
                                })
                                .unwrap();
                            tobkiri_gate::FrozenPortTarget {
                                pack: provider.clone(),
                                hash: pack.hash.clone(),
                                provided: ShapeRef::new(
                                    declaration.shape.clone(),
                                    declaration.version,
                                ),
                            }
                        })
                        .collect(),
                )
            })
            .collect();
        gate.register_port_bindings(&self.profile.id, &compiled.plan_digest, bindings)
            .unwrap();
        let epoch = gate.epoch(&self.profile.id).unwrap();
        let head = self.core.profile(&self.profile.id).unwrap().1;
        gate.register_user_policy_for_revision(
            &self.profile.id,
            &head,
            &compiled.plan_digest,
            epoch,
            self.profile.user_permissions.clone().unwrap_or_default(),
            self.profile.ceiling.clone(),
        )
        .unwrap();
        let cx = CallCx::try_root_with_control(
            self.profile.id.clone(),
            Principal::User,
            tobkiri_canon::ids::RequestId::parse("req_mediated_start").unwrap(),
            tobkiri_gate::CancellationToken::new_with_control(gate.control()).unwrap(),
            std::time::Instant::now()
                + std::time::Duration::from_millis(self.profile.ceiling.wasm.wall_ms),
            self.profile.ceiling.wasm.clone(),
            gate.control().clone(),
        )
        .unwrap()
        .with_security_epoch(epoch)
        .with_revision(compiled.plan_digest.clone());
        let limits = packs
            .iter()
            .map(|pack| {
                let pack_cx = cx
                    .try_child(Principal::Pack {
                        pack: pack.manifest.id.clone(),
                        hash: pack.hash.clone(),
                    })
                    .unwrap();
                (
                    (pack.manifest.id.clone(), pack.hash.clone()),
                    gate.effective_permissions(&pack_cx).unwrap().wasm,
                )
            })
            .collect::<BTreeMap<_, _>>();
        gate.register_revision(&self.profile.id, &compiled.plan_digest, limits.clone())
            .unwrap();
        let _endpoints =
            crate::execution_frames::register_flow_endpoints(&compiled, &gate).unwrap();
        let ingress = crate::inputs::register_inputs(&gate, &compiled, &packs, &cx).unwrap();
        let pack_host = Arc::new(
            tobkiri_wasm::PackHost::new(
                gate.clone(),
                crate::capacity::shared(&self.root).unwrap(),
                host.clone(),
                Arc::new(self.root.clone()),
                compiled.bindings.clone(),
                compiled.views.clone(),
                packs.clone(),
                limits,
                &cx,
            )
            .await
            .unwrap(),
        );
        let invoker: Arc<dyn tobkiri_flow::PortInvoker> = pack_host.clone();
        let factory = crate::execution_frames::ExecutionFactory::new(
            compiled.clone(),
            gate.clone(),
            invoker.clone(),
        )
        .unwrap();
        let bridge = crate::session::provider_entry::CoreProviderEntryBridge::allocate_shared(
            factory.provider_authority(),
            cx.control(),
        )
        .unwrap();
        pack_host.set_provider_entry_bridge(bridge.clone()).unwrap();
        let session = crate::Session::new_with_ingress(
            crate::session::SessionServices {
                compiled,
                gate,
                invoker,
                journal: Arc::new(crate::AuditRunJournal(audit)),
                host,
            },
            epoch,
            head,
            cx.cancel().clone(),
            ingress,
            factory,
            bridge,
        );
        session.ensure_started().await.unwrap();
        pack_host.set_flow_runner(session.weak_runner()).unwrap();
        for pack in packs {
            if let Some(ui) = pack.manifest.ui.as_ref() {
                session
                    .install_ui_material(
                        pack.manifest.id.clone(),
                        pack.hash.clone(),
                        crate::session::UiMaterial {
                            entry: ui.entry.clone(),
                            files: pack.files.clone(),
                        },
                    )
                    .unwrap();
                session
                    .register_ui(
                        tobkiri_canon::ids::UiSessionId::parse("ui_mediated").unwrap(),
                        tobkiri_canon::ids::UiSessionId::parse("host_mediated").unwrap(),
                        pack.manifest.id.clone(),
                        pack.hash.clone(),
                        ui.ports_used.iter().cloned().collect(),
                    )
                    .unwrap();
            }
        }
        session
    }
    pub(crate) fn audit_events(&self) -> Vec<AuditEvent> {
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
}

pub(crate) fn capture_run_failure(event: &tobkiri_flow::RunEvent, failures: &mut Vec<String>) {
    match &event.kind {
        tobkiri_flow::RunEventKind::Failed { error }
        | tobkiri_flow::RunEventKind::StepFailed { error, .. } => {
            failures.push(format!("{}: {}", error.code, error.message))
        }
        _ => {}
    }
}
pub(crate) fn native_run_diagnostic(
    result: &crate::NativeInputCompletion,
    failures: &[String],
) -> String {
    let bytes = result.buffer.as_bytes();
    format!(
        "routes={:?}; failures={failures:?}; envelope={}",
        result.routes,
        String::from_utf8_lossy(&bytes[..bytes.len().min(512)])
    )
}

/// Bound a test driver's wait without abandoning the first in-flight future
/// at the deadline. A failure signals this exact Session, then keeps polling
/// the same operation before reporting the timeout. A returned future alone
/// does not assert that every retained physical cleanup has completed.
pub(crate) async fn within_session<T>(
    session: &crate::Session,
    stage: &str,
    future: impl std::future::Future<Output = T>,
) -> T {
    tokio::pin!(future);
    match tokio::time::timeout(std::time::Duration::from_secs(5), &mut future).await {
        Ok(result) => result,
        Err(_) => {
            session.request_shutdown();
            let cleanup =
                tokio::time::timeout(std::time::Duration::from_secs(2), &mut future).await;
            panic!(
                "{stage} exceeded 5s; same operation {}",
                if cleanup.is_ok() {
                    "returned after shutdown"
                } else {
                    "remained unconfirmed after 2s"
                }
            );
        }
    }
}
