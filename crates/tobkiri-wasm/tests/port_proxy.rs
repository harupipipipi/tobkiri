use async_trait::async_trait;
use std::{
    collections::{BTreeMap, BTreeSet},
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
    time::{Duration, Instant},
};
use tobkiri_canon::{CanonValue, Digest, ids::*};
use tobkiri_flow::{
    CoreStream, FlowEngine, FlowRunner, PortInvoker, RunEvent, RunEventKind, RunHandle, RunJournal,
    StepError, StepOutput, StreamSource,
};
use tobkiri_gate::*;
use tobkiri_manifest::*;
use tobkiri_profile::{
    ErrorPolicy, FlowDocument, OverlayAccess, PortCall, ResolvedFlow, Step, StepKind, Trigger,
};
use tobkiri_store::{Approvals, AuditLog, AuditQuota, DataRoot};
use tobkiri_wasm::PackHost;
use tokio_util::sync::CancellationToken;

struct TestHost {
    prompts: AtomicUsize,
    ui_embeds: AtomicUsize,
    block_input: AtomicBool,
    read_started: tokio::sync::Notify,
    read_dropped: AtomicUsize,
}
struct PendingRead<'a>(&'a AtomicUsize);
impl Drop for PendingRead<'_> {
    fn drop(&mut self) {
        self.0.fetch_add(1, Ordering::SeqCst);
    }
}
struct FaultAudit {
    audit: Arc<AuditLog>,
    fail_outcome: AtomicBool,
    failed_outcomes: AtomicUsize,
}
impl AuditSink for FaultAudit {
    fn append(&self, event: AuditEvent) -> Result<(), GateError> {
        if event.kind == "outcome" && self.fail_outcome.load(Ordering::SeqCst) {
            self.failed_outcomes.fetch_add(1, Ordering::SeqCst);
            return Err(GateError::Persistence(
                "fixture outcome sink failure".into(),
            ));
        }
        self.audit.append(event)
    }
    fn append_idempotent(&self, event_id: &Digest, event: AuditEvent) -> Result<(), GateError> {
        if event.kind == "outcome" && self.fail_outcome.load(Ordering::SeqCst) {
            self.failed_outcomes.fetch_add(1, Ordering::SeqCst);
            return Err(GateError::Persistence(
                "fixture outcome sink failure".into(),
            ));
        }
        self.audit.append_idempotent(event_id, event)
    }
}
#[async_trait]
impl TrustedPrompt for TestHost {
    async fn prompt_approval(&self, _: ApprovalPrompt) -> ApprovalAnswer {
        self.prompts.fetch_add(1, Ordering::SeqCst);
        ApprovalAnswer::Approve
    }
}
#[async_trait]
impl HostIo for TestHost {
    fn display(&self, _: &str, _: &CanonValue) {}
    async fn terminal_read_line(&self, _: &str) -> Result<String, HostError> {
        if self.block_input.load(Ordering::SeqCst) {
            let _guard = PendingRead(&self.read_dropped);
            self.read_started.notify_one();
            return std::future::pending().await;
        }
        Err(HostError("unused fixture input".into()))
    }
    fn terminal_write(&self, _: &str) {}
    fn embed_ui(&self, _: UiEmbed) -> Result<UiSessionId, HostError> {
        self.ui_embeds.fetch_add(1, Ordering::SeqCst);
        Ok(UiSessionId::parse("fixture-ui-session").unwrap())
    }
    async fn pick_directory(&self, _: &str) -> Option<std::path::PathBuf> {
        None
    }
}
fn limits() -> ResourceLimits {
    ResourceLimits {
        fuel_per_call: 10_000_000,
        memory_mib: 16,
        wall_ms: 5000,
        table_elements: 10_000,
        output_bytes: 1024 * 1024,
    }
}
fn shape(name: &str) -> ShapeRef {
    ShapeRef::parse(&format!("local.proxy-test.{name}@1.0")).unwrap()
}
fn signature(params: &[(&str, WitType)], result: Option<WitType>) -> WitFunction {
    WitFunction {
        params: params
            .iter()
            .map(|(n, t)| ((*n).into(), t.clone()))
            .collect(),
        result,
    }
}
fn shapes() -> Vec<ShapeDef> {
    let error = WitType::Record(vec![
        WitField {
            name: "code".into(),
            ty: WitType::Enum(
                vec![
                    "denied",
                    "approval-required",
                    "cancelled",
                    "not-found",
                    "invalid",
                    "limit-exceeded",
                    "conflict",
                    "unavailable",
                    "internal",
                ]
                .into_iter()
                .map(String::from)
                .collect(),
            ),
        },
        WitField {
            name: "message".into(),
            ty: WitType::String,
        },
    ]);
    let result = |ok| WitType::Result {
        ok: Some(Box::new(ok)),
        err: Some(Box::new(error.clone())),
    };
    let functions = BTreeMap::from([
        (
            OpName::parse("increment").unwrap(),
            signature(&[("value", WitType::U32)], Some(WitType::U32)),
        ),
        (
            OpName::parse("floats").unwrap(),
            signature(
                &[("single", WitType::F32), ("double", WitType::F64)],
                Some(WitType::Tuple(vec![WitType::F32, WitType::F64])),
            ),
        ),
        (
            OpName::parse("nan").unwrap(),
            signature(&[], Some(WitType::F64)),
        ),
        (OpName::parse("spin").unwrap(), signature(&[], None)),
        (
            OpName::parse("memory-bomb").unwrap(),
            signature(&[], Some(WitType::U32)),
        ),
        (OpName::parse("trap").unwrap(), signature(&[], None)),
        (
            OpName::parse("wait").unwrap(),
            signature(
                &[],
                Some(WitType::Result {
                    ok: None,
                    err: Some(Box::new(error.clone())),
                }),
            ),
        ),
        (
            OpName::parse("request-ui").unwrap(),
            signature(
                &[("slot", WitType::String)],
                Some(WitType::Result {
                    ok: None,
                    err: Some(Box::new(error.clone())),
                }),
            ),
        ),
        (
            OpName::parse("count-stream").unwrap(),
            signature(
                &[("count", WitType::U32)],
                Some(result(WitType::StreamHandle)),
            ),
        ),
        (
            OpName::parse("with-callback").unwrap(),
            signature(
                &[
                    ("callback", WitType::CallbackHandle),
                    ("value", WitType::U32),
                ],
                Some(result(WitType::U32)),
            ),
        ),
    ]);
    let build = |name: &str, functions| ShapeDef {
        reference: shape(name),
        kind: ShapeKind::Port,
        cardinality: Cardinality::One,
        types: BTreeMap::from([("json".into(), WitType::Json)]),
        functions,
        streams: BTreeMap::from([(
            if name == "arithmetic" {
                "count-stream"
            } else {
                "run-flow"
            }
            .into(),
            "json".into(),
        )]),
        callbacks: BTreeMap::new(),
        wit: include_str!("../../test-guests/wit/fixtures.wit").into(),
    };
    vec![
        build("arithmetic", functions),
        build(
            "entry",
            BTreeMap::from([
                (
                    OpName::parse("run").unwrap(),
                    signature(&[("value", WitType::U32)], Some(WitType::U32)),
                ),
                (
                    OpName::parse("run-flow").unwrap(),
                    signature(
                        &[("implements", WitType::String), ("input", WitType::String)],
                        Some(result(WitType::StreamHandle)),
                    ),
                ),
                (
                    OpName::parse("run-callback").unwrap(),
                    signature(&[("value", WitType::U32)], Some(result(WitType::U32))),
                ),
                (
                    OpName::parse("open-callback").unwrap(),
                    signature(&[], Some(result(WitType::CallbackHandle))),
                ),
            ]),
        ),
    ]
}
fn artifact(binary: &str) -> Vec<u8> {
    let target = std::env::var_os("CARGO_TARGET_DIR")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| {
            std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../.work/cargo-target")
        });
    std::fs::read(target.join("wasm32-wasip2/debug").join(binary))
        .expect("build the independently locked Rust test-guests workspace first")
}
fn permission(ports: &[ShapeRef]) -> PermissionSet {
    PermissionSet {
        ports: ports.iter().map(ToString::to_string).collect(),
        audit_append: true,
        audit_read: true,
        host_io: BTreeSet::from(["terminal-read-line".into(), "request-ui-embed".into()]),
        wasm: limits(),
        ..Default::default()
    }
}
fn port(reference: ShapeRef) -> PortDeclaration {
    PortDeclaration {
        shape: reference.name,
        version: reference.version,
        optional: false,
    }
}
fn pack(
    id: &str,
    binary: &str,
    provides: Vec<PortDeclaration>,
    consumes: Vec<PortDeclaration>,
    permissions: PermissionSet,
) -> VerifiedPack {
    let bytes = artifact(binary);
    let manifest = PackManifest {
        format: "tobkiri.pack/1".into(),
        id: PackId::parse(id).unwrap(),
        version: "0.1.0".into(),
        publisher_key: None,
        runtime: RuntimeSpec::Wasm {
            component: "component.wasm".into(),
            toolchain: "rust-1.94.0/wasm32-wasip2".into(),
            config: None,
        },
        shapes: vec![],
        ports: Ports {
            provides,
            consumes,
            untyped: vec![],
        },
        flows: vec![],
        overlays: vec![],
        inputs: vec![],
        stores: vec![],
        permissions,
        ui: None,
        files: vec![FileEntry {
            path: "component.wasm".into(),
            sha256: Digest::hash(&bytes),
            size: bytes.len() as u64,
        }],
    };
    let hash = PackHash::new(Digest::hash(&serde_json::to_vec(&manifest).unwrap()));
    VerifiedPack {
        manifest,
        hash,
        files: BTreeMap::from([
            ("component.wasm".into(), bytes),
            (
                "wit/fixtures.wit".into(),
                include_bytes!("../../test-guests/wit/fixtures.wit").to_vec(),
            ),
        ]),
        shapes: shapes(),
    }
}
struct Fixture {
    _dir: tempfile::TempDir,
    host: PackHost,
    gate: Arc<GateEngine>,
    io: Arc<TestHost>,
    cx: CallCx,
    audit: Arc<AuditLog>,
    fault_audit: Arc<FaultAudit>,
    consumer_hash: PackHash,
}
async fn fixture(substitute_input: bool, consumer_allowed: bool) -> Fixture {
    fixture_mediated(substitute_input, consumer_allowed, MediatorChain::default()).await
}
async fn fixture_mediated(
    substitute_input: bool,
    consumer_allowed: bool,
    mediators: MediatorChain,
) -> Fixture {
    let dir = tempfile::tempdir().unwrap();
    let root = Arc::new(DataRoot::open(dir.path().canonicalize().unwrap()).unwrap());
    let profile = ProfileId::parse("proxy-profile").unwrap();
    let approvals = Arc::new(Approvals::open(&root, &profile).unwrap());
    let audit = Arc::new(AuditLog::open(&root, &profile, AuditQuota::default()).unwrap());
    let fault_audit = Arc::new(FaultAudit {
        audit: audit.clone(),
        fail_outcome: AtomicBool::new(false),
        failed_outcomes: AtomicUsize::new(0),
    });
    let io = Arc::new(TestHost {
        prompts: AtomicUsize::new(0),
        ui_embeds: AtomicUsize::new(0),
        block_input: AtomicBool::new(false),
        read_started: tokio::sync::Notify::new(),
        read_dropped: AtomicUsize::new(0),
    });
    let gate = Arc::new(GateEngine::new(
        approvals.clone(),
        fault_audit.clone(),
        io.clone(),
        mediators,
    ));
    let revision = Digest::hash(b"proxy-fixture-frozen-revision");
    let cx = CallCx::root(
        profile.clone(),
        Principal::User,
        RequestId::parse("proxy-root").unwrap(),
        CancellationToken::new(),
        Instant::now() + Duration::from_secs(20),
        limits(),
    )
    .with_revision(revision.clone());
    let allowed = permission(&[shape("arithmetic"), shape("entry")]);
    gate.register_operation_views(
        &profile,
        &revision,
        shapes()
            .into_iter()
            .map(|shape| (shape.reference, shape.functions.into_keys().collect()))
            .collect(),
    )
    .unwrap();
    let profile_hash = Digest::hash(b"proxy-fixture-explicit-user-policy");
    gate.register_user_policy_for_revision(
        &profile,
        &profile_hash,
        &revision,
        cx.security_epoch(),
        allowed.clone(),
        allowed.clone(),
    )
    .unwrap();
    gate.approve_user_permissions(&cx, &profile_hash, &revision, cx.security_epoch(), &allowed)
        .await
        .unwrap();
    let consumer_ports = if consumer_allowed {
        vec![shape("arithmetic")]
    } else {
        vec![]
    };
    let mut packs = vec![
        pack(
            "local.test-provider",
            "tobkiri_test_provider.wasm",
            vec![port(shape("arithmetic"))],
            vec![],
            permission(&[]),
        ),
        pack(
            "local.test-consumer",
            "tobkiri_test_consumer.wasm",
            vec![port(shape("entry"))],
            vec![port(shape("arithmetic"))],
            permission(&consumer_ports),
        ),
    ];
    if substitute_input {
        packs.push(pack(
            "local.test-substitute",
            "tobkiri_test_consumer.wasm",
            vec![port(shape("entry"))],
            vec![port(shape("arithmetic"))],
            permission(&[shape("arithmetic")]),
        ));
    }
    let mut budget = BTreeMap::new();
    for pack in &packs {
        let declared = pack.manifest.permissions.clone();
        gate.register_pack(
            &profile,
            &pack.manifest.id,
            &pack.hash,
            PackPolicy {
                declaration: declared.clone(),
                ceiling: allowed.clone(),
                pack_limits: declared.clone(),
            },
        )
        .unwrap();
        gate.approve_pack_permissions(&cx, &pack.manifest.id, None, &pack.hash)
            .await
            .unwrap();
        let child = cx.child(Principal::Pack {
            pack: pack.manifest.id.clone(),
            hash: pack.hash.clone(),
        });
        budget.insert(
            (pack.manifest.id.clone(), pack.hash.clone()),
            gate.effective_permissions(&child).unwrap().wasm,
        );
    }
    let selected = if substitute_input {
        "local.test-substitute"
    } else {
        "local.test-consumer"
    };
    let bindings = BTreeMap::from([
        (
            shape("arithmetic"),
            vec![PackId::parse("local.test-provider").unwrap()],
        ),
        (shape("entry"), vec![PackId::parse(selected).unwrap()]),
    ]);
    gate.register_revision(&profile, &revision, budget.clone())
        .unwrap();
    let consumer_hash = packs
        .iter()
        .find(|p| p.manifest.id.as_str() == "local.test-consumer")
        .unwrap()
        .hash
        .clone();
    let host = PackHost::new(
        gate.clone(),
        tobkiri_gate::CapacityLedger::new(),
        io.clone(),
        root,
        bindings,
        shapes()
            .into_iter()
            .map(|shape| (shape.reference.clone(), shape))
            .collect(),
        packs,
        budget,
        &cx,
    )
    .await
    .unwrap();
    Fixture {
        _dir: dir,
        host,
        gate,
        io,
        cx,
        audit,
        fault_audit,
        consumer_hash,
    }
}
async fn call(
    f: &Fixture,
    interface: &str,
    op: &str,
    args: CanonValue,
) -> Result<CanonValue, tobkiri_flow::StepError> {
    match f
        .host
        .call(&f.cx, &shape(interface), &OpName::parse(op).unwrap(), args)
        .await?
    {
        StepOutput::Value(value) => Ok(value),
        StepOutput::Stream(_) => panic!("fixture has no streams"),
    }
}
fn args(entries: &[(&str, CanonValue)]) -> CanonValue {
    CanonValue::Obj(
        entries
            .iter()
            .map(|(k, v)| ((*k).into(), v.clone()))
            .collect(),
    )
}

#[tokio::test]
async fn rust_consumer_calls_rust_provider_through_real_gate_and_audit() {
    let f = fixture(false, true).await;
    assert_eq!(
        call(&f, "entry", "run", args(&[("value", CanonValue::Int(40))]))
            .await
            .unwrap(),
        CanonValue::Int(82)
    );
    let req = EffectRequest::AuditRead;
    let hash = AuditLog::read_digest(0, 1000).unwrap();
    let grant = f.gate.authorize(&f.cx, req, hash).await.unwrap();
    let id = grant.id().clone();
    let rows = f.audit.read(&f.cx, 0, 1000, grant).unwrap();
    f.gate.record_outcome(id, Outcome::Succeeded).await.unwrap();
    assert!(rows.iter().any(|row| {
        row.event.kind == "dispatch"
            && row
                .event
                .principal
                .pack()
                .is_some_and(|(id, _)| id.as_str() == "local.test-consumer")
    }));
    assert!(rows.iter().any(|row| {
        row.event.kind == "outcome"
            && row
                .event
                .principal
                .pack()
                .is_some_and(|(id, _)| id.as_str() == "local.test-consumer")
    }));
}

#[tokio::test]
async fn declared_typed_import_still_cannot_call_without_effective_port_permission() {
    let f = fixture(false, false).await;
    let result = call(&f, "entry", "run", args(&[("value", CanonValue::Int(40))])).await;
    assert!(result.is_err());
}

#[tokio::test]
async fn input_principal_cannot_select_another_pack_with_the_same_provided_shape() {
    let f = fixture(true, true).await;
    let source = InputSourceId::parse("local.test-consumer").unwrap();
    f.gate
        .register_input(
            f.cx.profile(),
            &source,
            BTreeSet::from([shape("entry").to_string()]),
            limits(),
        )
        .unwrap();
    let cx = f.cx.child(Principal::Input { source });
    let prompts = f.io.prompts.load(Ordering::SeqCst);
    let result = f
        .host
        .call(
            &cx,
            &shape("entry"),
            &OpName::parse("run").unwrap(),
            args(&[("value", CanonValue::Int(40))]),
        )
        .await;
    assert!(result.is_err());
    assert_eq!(f.io.prompts.load(Ordering::SeqCst), prompts);
}

#[tokio::test]
async fn actual_float_guest_preserves_bits_and_nonfinite_output_is_rejected() {
    let f = fixture(false, true).await;
    for value in [-0.0_f64, f64::MAX, f64::MIN_POSITIVE] {
        let result = call(
            &f,
            "arithmetic",
            "floats",
            args(&[
                ("single", encode_f32(-0.0).unwrap()),
                ("double", encode_f64(value).unwrap()),
            ]),
        )
        .await
        .unwrap();
        assert_eq!(
            result,
            CanonValue::Arr(vec![encode_f32(-0.0).unwrap(), encode_f64(value).unwrap()])
        );
    }
    assert!(call(&f, "arithmetic", "nan", args(&[])).await.is_err());
    assert_eq!(
        call(
            &f,
            "arithmetic",
            "increment",
            args(&[("value", CanonValue::Int(6))])
        )
        .await
        .unwrap(),
        CanonValue::Int(7)
    );
}

async fn audit_rows(f: &Fixture) -> Vec<tobkiri_store::AuditRow> {
    let request = EffectRequest::AuditRead;
    let args = AuditLog::read_digest(0, 1000).unwrap();
    let grant = f.gate.authorize(&f.cx, request, args).await.unwrap();
    let id = grant.id().clone();
    let rows = f.audit.read(&f.cx, 0, 1000, grant).unwrap();
    f.gate.record_outcome(id, Outcome::Succeeded).await.unwrap();
    rows
}
#[tokio::test]
async fn guest_stream_opens_once_and_finalizes_once_without_per_pull_audit() {
    let f = fixture(false, true).await;
    let StepOutput::Stream(mut stream) = f
        .host
        .call(
            &f.cx,
            &shape("arithmetic"),
            &OpName::parse("count-stream").unwrap(),
            args(&[("count", CanonValue::Int(3))]),
        )
        .await
        .unwrap()
    else {
        panic!("stream metadata ignored")
    };
    let before = audit_rows(&f).await;
    assert_eq!(
        stream.pull(1).await.unwrap(),
        vec![args(&[("index", CanonValue::Int(0))])]
    );
    assert_eq!(stream.pull(2).await.unwrap().len(), 2);
    assert!(stream.pull(2).await.unwrap().is_empty());
    drop(stream);
    let after = audit_rows(&f).await;
    // Only the two audit-read records and StreamOpen's final outcome are new.
    assert_eq!(after.len() - before.len(), 3);
    assert!(after.iter().any(|row| {
        row.event.kind == "outcome"
            && row
                .event
                .principal
                .pack()
                .is_some_and(|(p, _)| p.as_str() == "local.test-provider")
    }));
}
#[tokio::test]
async fn guest_ui_embed_is_unavailable_even_for_startup_slot_and_is_audited() {
    let f = fixture(false, true).await;
    for slot in ["startup", "guest-panel"] {
        let error = call(
            &f,
            "arithmetic",
            "request-ui",
            args(&[("slot", CanonValue::Str(slot.into()))]),
        )
        .await
        .unwrap_err();
        assert_eq!(error.code, "unavailable");
    }
    assert_eq!(f.io.ui_embeds.load(Ordering::SeqCst), 0);
    let effect_digest = CanonValue::Str(
        EffectRequest::HostIo {
            operation: "request-ui-embed".into(),
        }
        .digest()
        .unwrap()
        .to_string(),
    );
    let rows = audit_rows(&f).await;
    let effect_rows: Vec<_> = rows
        .iter()
        .filter(|row| {
            matches!(&row.event.payload, CanonValue::Obj(fields)
                if fields.get("effect_digest") == Some(&effect_digest))
        })
        .collect();
    let dispatches: BTreeSet<_> = effect_rows
        .iter()
        .filter(|row| row.event.kind == "dispatch")
        .map(|row| match &row.event.payload {
            CanonValue::Obj(fields) => match &fields["grant"] {
                CanonValue::Str(grant) => grant.clone(),
                _ => unreachable!(),
            },
            _ => unreachable!(),
        })
        .collect();
    assert_eq!(dispatches.len(), 2);
    let outcomes: Vec<_> = effect_rows
        .iter()
        .filter(|row| row.event.kind == "outcome")
        .collect();
    assert_eq!(outcomes.len(), 2);
    for row in outcomes {
        let CanonValue::Obj(fields) = &row.event.payload else {
            unreachable!()
        };
        let CanonValue::Str(grant) = &fields["grant"] else {
            unreachable!()
        };
        assert!(dispatches.contains(grant));
        assert_eq!(
            fields["outcome"],
            args(&[
                ("status", CanonValue::Str("failed".into())),
                ("code", CanonValue::Str("host-effect".into())),
            ])
        );
    }
    // Failing the final audit write must also be returned through the existing
    // port Result; it cannot restore a successful UI request or host dispatch.
    f.fault_audit.fail_outcome.store(true, Ordering::SeqCst);
    let error = call(
        &f,
        "arithmetic",
        "request-ui",
        args(&[("slot", CanonValue::Str("startup".into()))]),
    )
    .await
    .unwrap_err();
    assert!(error.message.contains("fixture outcome sink failure"));
    assert!(f.fault_audit.failed_outcomes.load(Ordering::SeqCst) > 0);
    assert_eq!(f.io.ui_embeds.load(Ordering::SeqCst), 0);
}
async fn count_stream(f: &Fixture) -> CoreStream {
    let StepOutput::Stream(stream) = f
        .host
        .call(
            &f.cx,
            &shape("arithmetic"),
            &OpName::parse("count-stream").unwrap(),
            args(&[("count", CanonValue::Int(10))]),
        )
        .await
        .unwrap()
    else {
        panic!("stream metadata ignored")
    };
    stream
}
fn cancelled_outcomes(rows: &[tobkiri_store::AuditRow]) -> usize {
    rows.iter()
        .filter(|row| {
            row.event.kind == "outcome"
                && matches!(&row.event.payload, CanonValue::Obj(fields)
                    if fields.get("outcome") == Some(&args(&[("status", CanonValue::Str("cancelled".into()))])))
        })
        .count()
}
#[tokio::test]
async fn explicit_close_and_cancel_then_pull_surface_audit_sink_failure() {
    let f = fixture(false, true).await;
    let mut first = count_stream(&f).await;
    f.fault_audit.fail_outcome.store(true, Ordering::SeqCst);
    let error = first.close().await.unwrap_err();
    assert!(error.message.contains("fixture outcome sink failure"));
    assert_eq!(first.close().await.unwrap_err().message, error.message);
    f.fault_audit.fail_outcome.store(false, Ordering::SeqCst);
    first.close().await.unwrap();
    assert!(first.pull(1).await.unwrap().is_empty());
    assert_eq!(cancelled_outcomes(&audit_rows(&f).await), 1);
    let mut second = count_stream(&f).await;
    f.fault_audit.fail_outcome.store(true, Ordering::SeqCst);
    second.cancel();
    let error = second.pull(1).await.unwrap_err();
    assert!(error.message.contains("fixture outcome sink failure"));
    assert_eq!(second.close().await.unwrap_err().message, error.message);
    assert!(f.fault_audit.failed_outcomes.load(Ordering::SeqCst) >= 2);
    f.fault_audit.fail_outcome.store(false, Ordering::SeqCst);
    second.close().await.unwrap();
    assert_eq!(cancelled_outcomes(&audit_rows(&f).await), 2);
}

#[tokio::test]
async fn expired_stream_and_callback_never_claim_successful_cancellation() {
    let f = fixture(false, true).await;
    // Warm both providers before applying the deliberately short entry bound.
    let mut warm = count_stream(&f).await;
    warm.close().await.unwrap();
    call(&f, "entry", "run", args(&[("value", CanonValue::Int(1))]))
        .await
        .unwrap();
    let before = audit_rows(&f).await.len();
    let mut cap = limits();
    cap.wall_ms = 100;
    let cx = f.cx.clone().narrow_limits(&cap);
    let StepOutput::Stream(_held) = f
        .host
        .call(
            &cx,
            &shape("arithmetic"),
            &OpName::parse("count-stream").unwrap(),
            args(&[("count", CanonValue::Int(10))]),
        )
        .await
        .unwrap()
    else {
        panic!("not a stream")
    };
    f.host
        .call(
            &cx,
            &shape("entry"),
            &OpName::parse("open-callback").unwrap(),
            args(&[]),
        )
        .await
        .unwrap();
    tokio::time::sleep(Duration::from_millis(250)).await;
    let rows = audit_rows(&f).await;
    let expired = rows[before..]
        .iter()
        .filter_map(|row| {
            if row.event.kind != "outcome" {
                return None;
            }
            let CanonValue::Obj(fields) = &row.event.payload else {
                return None;
            };
            let Some(CanonValue::Obj(outcome)) = fields.get("outcome") else {
                return None;
            };
            (outcome.get("code") == Some(&CanonValue::Str("deadline-failure-unknown".into())))
                .then_some(fields)
        })
        .collect::<Vec<_>>();
    assert_eq!(
        expired.len(),
        2,
        "both original auxiliary grants must expire honestly"
    );
    for fields in expired {
        assert_eq!(
            fields.get("disposition"),
            Some(&CanonValue::Str("expired-cleanup-unknown".into()))
        );
    }
}

#[tokio::test]
async fn acknowledged_cleanup_retries_the_same_immutable_cancel_winner_after_deadline() {
    let f = fixture(false, true).await;
    let mut warm = count_stream(&f).await;
    warm.close().await.unwrap();
    let before = audit_rows(&f).await.len();
    let mut limits = limits();
    limits.wall_ms = 200;
    let cx = f.cx.clone().narrow_limits(&limits);
    let StepOutput::Stream(mut stream) = f
        .host
        .call(
            &cx,
            &shape("arithmetic"),
            &OpName::parse("count-stream").unwrap(),
            args(&[("count", CanonValue::Int(10))]),
        )
        .await
        .unwrap()
    else {
        panic!("missing stream")
    };
    f.fault_audit.fail_outcome.store(true, Ordering::SeqCst);
    // Guest cancel really completes. Only the terminal sink fails, so the
    // durable Cancelled winner precedes the original deadline.
    assert!(stream.close().await.is_err());
    tokio::time::sleep(Duration::from_millis(300)).await;
    f.fault_audit.fail_outcome.store(false, Ordering::SeqCst);
    stream.close().await.unwrap();
    stream.close().await.unwrap();
    let rows = audit_rows(&f).await;
    let terminals: Vec<_> = rows[before..]
        .iter()
        .filter(|row| {
            if row.event.kind != "outcome" {
                return false;
            }
            let CanonValue::Obj(fields) = &row.event.payload else {
                return false;
            };
            let Some(CanonValue::Obj(outcome)) = fields.get("outcome") else {
                return false;
            };
            outcome.get("status") == Some(&CanonValue::Str("cancelled".into()))
        })
        .collect();
    assert_eq!(
        terminals.len(),
        1,
        "sink retry must not replay cleanup or replace the winner"
    );
    let CanonValue::Obj(fields) = &terminals[0].event.payload else {
        unreachable!()
    };
    assert_eq!(
        fields.get("disposition"),
        Some(&CanonValue::Str("completed".into()))
    );
}

#[test]
fn cancel_after_owner_runtime_shutdown_returns_error_and_durable_expiry_recovers() {
    let runtime = tokio::runtime::Runtime::new().unwrap();
    let (f, mut stream) = runtime.block_on(async {
        let f = fixture(false, true).await;
        let stream = count_stream(&f).await;
        (f, stream)
    });
    drop(runtime);
    stream.cancel();
    let recovery = tokio::runtime::Runtime::new().unwrap();
    let error = recovery.block_on(async {
        tokio::time::timeout(Duration::from_secs(3), stream.close())
            .await
            .expect("dead runtime cleanup hung")
            .unwrap_err()
    });
    assert_eq!(error.code, "unavailable");
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_millis() as u64;
    f.gate
        .recover_profile_at(f.cx.profile(), now + 60_000)
        .unwrap();
    recovery
        .block_on(f.gate.flush_outbox(f.cx.profile()))
        .unwrap();
    let rows = recovery.block_on(audit_rows(&f));
    assert_eq!(cancelled_outcomes(&rows), 0);
    assert!(rows.iter().any(|row|matches!(&row.event.payload,CanonValue::Obj(fields) if fields.get("disposition")==Some(&CanonValue::Str("expired-cleanup-unknown".into())))));
}
#[tokio::test]
async fn cancellation_from_plain_thread_uses_owning_runtime_and_records_outcome() {
    let f = fixture(false, true).await;
    let stream = count_stream(&f).await;
    let mut stream = std::thread::spawn(move || {
        assert!(tokio::runtime::Handle::try_current().is_err());
        let mut stream = stream;
        stream.cancel();
        stream
    })
    .join()
    .unwrap();
    stream.close().await.unwrap();
    assert!(stream.pull(1).await.unwrap().is_empty());
    assert_eq!(cancelled_outcomes(&audit_rows(&f).await), 1);
}
#[tokio::test]
async fn drop_from_plain_thread_completes_cleanup_on_owning_runtime() {
    let f = fixture(false, true).await;
    let stream = count_stream(&f).await;
    std::thread::spawn(move || {
        assert!(tokio::runtime::Handle::try_current().is_err());
        drop(stream);
    })
    .join()
    .unwrap();
    tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            if cancelled_outcomes(&audit_rows(&f).await) == 1 {
                break;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .expect("off-runtime drop left the StreamOpen grant dispatch-only");
}
#[tokio::test]
async fn callback_reentry_returns_conflict_instead_of_waiting_for_ancestor_lock() {
    let f = fixture(false, true).await;
    let started = Instant::now();
    let error = call(
        &f,
        "entry",
        "run-callback",
        args(&[("value", CanonValue::Int(7))]),
    )
    .await
    .unwrap_err();
    assert_eq!(error.code, "conflict");
    assert!(started.elapsed() < Duration::from_secs(2));
    assert_eq!(
        call(&f, "entry", "run", args(&[("value", CanonValue::Int(5))]))
            .await
            .unwrap(),
        CanonValue::Int(12)
    );
}
struct FixtureJournal;
#[async_trait]
impl RunJournal for FixtureJournal {
    async fn append(&self, _: &CallCx, _: &RunEvent) -> Result<(), StepError> {
        Ok(())
    }
}
struct FixtureFlowRunner {
    gate: Arc<GateEngine>,
    engine: FlowEngine,
    flow: Arc<ResolvedFlow>,
    revision: Digest,
}
#[async_trait]
impl FlowRunner for FixtureFlowRunner {
    async fn run_flow(
        &self,
        cx: &CallCx,
        implements: &ShapeRef,
        input: CanonValue,
    ) -> Result<CoreStream, StepError> {
        if cx.revision() != Some(&self.revision)
            || self.flow.document.implements.as_ref() != Some(implements)
        {
            return Err(StepError::new(
                "not-found",
                "Flow not in frozen fixture revision",
            ));
        }
        let request = EffectRequest::FlowRun {
            implements: implements.clone(),
            flow_owner: (self.flow.owner.clone(), self.flow.hash.clone()),
        };
        let args = tobkiri_canon::digest(&input, tobkiri_canon::Variant::StrictV1).unwrap();
        let grant = self
            .gate
            .authorize(cx, request.clone(), args.clone())
            .await
            .map_err(|e| StepError::new(e.code(), "Flow request denied"))?;
        let id = grant.id().clone();
        grant
            .consume(cx, &request, &args)
            .map_err(|e| StepError::new(e.code(), "Flow grant denied"))?;
        let run = self.engine.run(
            cx.clone(),
            self.flow.clone(),
            input,
            LoopLimits {
                max_iterations: 100,
                max_wall_ms: 5000,
                max_cost_units: 10000,
            },
        );
        Ok(CoreStream::new(FixtureFlowStream {
            gate: self.gate.clone(),
            grant: Some(id),
            run,
            delivered: false,
        }))
    }
}
struct FixtureFlowStream {
    gate: Arc<GateEngine>,
    grant: Option<GrantId>,
    run: RunHandle,
    delivered: bool,
}
#[async_trait]
impl StreamSource for FixtureFlowStream {
    async fn pull(&mut self, _: u32) -> Result<Vec<CanonValue>, StepError> {
        if self.delivered {
            return Ok(vec![]);
        }
        while let Some(event) = self.run.events.recv().await {
            match event.kind {
                RunEventKind::Completed { value, .. } => {
                    self.delivered = true;
                    if let Some(id) = self.grant.take() {
                        self.gate
                            .record_outcome(id, Outcome::Succeeded)
                            .await
                            .map_err(|_| StepError::new("internal", "Flow outcome audit failed"))?;
                    }
                    return Ok(vec![value]);
                }
                RunEventKind::Failed { error } => {
                    self.delivered = true;
                    if let Some(id) = self.grant.take() {
                        self.gate
                            .record_outcome(
                                id,
                                Outcome::Failed {
                                    code: error.audit_code().into(),
                                },
                            )
                            .await
                            .map_err(|_| StepError::new("internal", "Flow outcome audit failed"))?;
                    }
                    return Err(error);
                }
                _ => {}
            }
        }
        Err(StepError::new(
            "internal",
            "Flow fixture ended without outcome",
        ))
    }
    fn cancel(&mut self) {
        if let Some(id) = self.grant.take() {
            self.run.cancel.cancel();
            let gate = self.gate.clone();
            tokio::spawn(async move {
                let _ = gate.record_outcome(id, Outcome::Cancelled).await;
            });
        }
    }
}
#[tokio::test]
async fn guest_run_flow_uses_weak_bridge_and_real_flow_engine_typed_port() {
    let f = fixture(false, true).await;
    let flow = Arc::new(ResolvedFlow {
        owner: PackId::parse("local.test-consumer").unwrap(),
        hash: f.consumer_hash.clone(),
        document: FlowDocument {
            format: "tobkiri.flow/1".into(),
            id: "fixture.increment".into(),
            implements: Some(shape("arithmetic")),
            trigger: Trigger {
                input: shape("entry"),
            },
            loop_limits: "profile".into(),
            steps: vec![Step {
                id: Some("increment".into()),
                role: None,
                overlay: OverlayAccess::Closed,
                kind: StepKind::Call(PortCall {
                    shape: shape("arithmetic"),
                    op: OpName::parse("increment").unwrap(),
                    args: args(&[("value", CanonValue::Str("$input.value".into()))]),
                }),
                out: Some("answer".into()),
                stream: None,
            }],
            on_error: ErrorPolicy::default(),
        },
        ordered_overlays: BTreeMap::new(),
        plan_digest: Digest::hash(b"fixture.increment"),
    });
    let runner: Arc<dyn FlowRunner> = Arc::new(FixtureFlowRunner {
        gate: f.gate.clone(),
        engine: FlowEngine::new(Arc::new(f.host.clone()), Arc::new(FixtureJournal)),
        flow,
        revision: f.cx.revision().unwrap().clone(),
    });
    f.host.set_flow_runner(Arc::downgrade(&runner)).unwrap();
    assert!(f.host.set_flow_runner(Arc::downgrade(&runner)).is_err());
    let StepOutput::Stream(mut stream) = f
        .host
        .call(
            &f.cx,
            &shape("entry"),
            &OpName::parse("run-flow").unwrap(),
            args(&[
                (
                    "implements",
                    CanonValue::Str(shape("arithmetic").to_string()),
                ),
                ("input", CanonValue::Str("{\"value\":41}".into())),
            ]),
        )
        .await
        .unwrap()
    else {
        panic!("Flow bridge returned value")
    };
    assert_eq!(stream.pull(1).await.unwrap(), vec![CanonValue::Int(42)]);
    assert!(stream.pull(1).await.unwrap().is_empty());
}

#[tokio::test]
async fn actual_guest_trap_fuel_and_memory_errors_do_not_poison_following_call() {
    let f = fixture(false, true).await;
    for operation in ["trap", "spin", "memory-bomb"] {
        let started = Instant::now();
        assert!(
            call(&f, "arithmetic", operation, args(&[])).await.is_err(),
            "{operation} unexpectedly succeeded"
        );
        assert!(
            started.elapsed() < Duration::from_secs(3),
            "{operation} exceeded its bounded execution"
        );
        assert_eq!(
            call(
                &f,
                "arithmetic",
                "increment",
                args(&[("value", CanonValue::Int(8))])
            )
            .await
            .unwrap(),
            CanonValue::Int(9)
        );
    }
}

#[tokio::test]
async fn missing_hash_bound_execution_budget_is_audited_before_instantiation() {
    let f = fixture(false, true).await;
    let before = audit_rows(&f).await.len();
    let root = Arc::new(DataRoot::open(f._dir.path().canonicalize().unwrap()).unwrap());
    let result = PackHost::new(
        f.gate.clone(),
        tobkiri_gate::CapacityLedger::new(),
        f.io.clone(),
        root,
        BTreeMap::new(),
        shapes()
            .into_iter()
            .map(|shape| (shape.reference.clone(), shape))
            .collect(),
        vec![pack(
            "local.test-provider",
            "tobkiri_test_provider.wasm",
            vec![port(shape("arithmetic"))],
            vec![],
            permission(&[]),
        )],
        BTreeMap::new(),
        &f.cx,
    )
    .await;
    assert!(result.is_err());
    let rows = audit_rows(&f).await;
    assert!(rows[before..].iter().any(|r| r.event.kind == "denied"));
}

#[tokio::test]
async fn core_interface_name_with_forged_function_signature_is_rejected_before_init() {
    let f = fixture(false, true).await;
    let mut malicious = pack(
        "local.test-provider",
        "tobkiri_test_provider.wasm",
        vec![],
        vec![],
        permission(&[]),
    );
    let bytes = wat::parse_str(
        r#"(component
        (type $clock (instance (export "now-ms" (func (param "forged" u32) (result u64)))))
        (import "tobkiri:core/clock@1.0.0" (instance (type $clock))))"#,
    )
    .unwrap();
    malicious
        .files
        .insert("component.wasm".into(), bytes.clone());
    malicious.manifest.files = vec![FileEntry {
        path: "component.wasm".into(),
        sha256: Digest::hash(&bytes),
        size: bytes.len() as u64,
    }];
    malicious.hash = PackHash::new(Digest::hash(
        &serde_json::to_vec(&malicious.manifest).unwrap(),
    ));
    let budget = BTreeMap::from([(
        (malicious.manifest.id.clone(), malicious.hash.clone()),
        limits(),
    )]);
    let prompts = f.io.prompts.load(Ordering::SeqCst);
    let root = Arc::new(DataRoot::open(f._dir.path().canonicalize().unwrap()).unwrap());
    let result = PackHost::new(
        f.gate.clone(),
        tobkiri_gate::CapacityLedger::new(),
        f.io.clone(),
        root,
        BTreeMap::new(),
        shapes()
            .into_iter()
            .map(|shape| (shape.reference.clone(), shape))
            .collect(),
        vec![malicious],
        budget,
        &f.cx,
    )
    .await;
    assert!(result.is_err());
    assert_eq!(f.io.prompts.load(Ordering::SeqCst), prompts);
}

#[tokio::test]
async fn epoch_revocation_closes_live_stream_and_next_fresh_context_recovers() {
    let mut f = fixture(false, true).await;
    let StepOutput::Stream(mut stream) = f
        .host
        .call(
            &f.cx,
            &shape("arithmetic"),
            &OpName::parse("count-stream").unwrap(),
            args(&[("count", CanonValue::Int(3))]),
        )
        .await
        .unwrap()
    else {
        panic!("stream metadata ignored")
    };
    assert_eq!(stream.pull(1).await.unwrap().len(), 1);
    let epoch = f.gate.revoke_epoch(f.cx.profile()).unwrap();
    assert!(
        stream.pull(1).await.is_err(),
        "revoked stream kept executing"
    );
    drop(stream);
    f.cx = f.cx.clone().with_security_epoch(epoch);
    assert_eq!(
        call(
            &f,
            "arithmetic",
            "increment",
            args(&[("value", CanonValue::Int(10))])
        )
        .await
        .unwrap(),
        CanonValue::Int(11)
    );
    let rows = audit_rows(&f).await;
    let expected = CanonValue::Obj(BTreeMap::from([(
        "status".into(),
        CanonValue::Str("cancelled".into()),
    )]));
    let cancelled=rows.iter().filter(|row|row.event.kind=="outcome"&&matches!(&row.event.payload,CanonValue::Obj(fields)if fields.get("outcome")==Some(&expected))).count();
    assert_eq!(cancelled, 1);
}

#[tokio::test]
async fn cancelling_stream_retires_locked_instance_and_drops_pending_host_future() {
    let f = fixture(false, true).await;
    let StepOutput::Stream(mut stream) = f
        .host
        .call(
            &f.cx,
            &shape("arithmetic"),
            &OpName::parse("count-stream").unwrap(),
            args(&[("count", CanonValue::Int(3))]),
        )
        .await
        .unwrap()
    else {
        panic!("stream metadata ignored")
    };
    f.io.block_input.store(true, Ordering::SeqCst);
    let host = f.host.clone();
    let cx = f.cx.clone();
    let pending = tokio::spawn(async move {
        host.call(
            &cx,
            &shape("arithmetic"),
            &OpName::parse("wait").unwrap(),
            args(&[]),
        )
        .await
    });
    tokio::time::timeout(Duration::from_secs(2), f.io.read_started.notified())
        .await
        .unwrap();
    stream.cancel();
    let result = tokio::time::timeout(Duration::from_millis(2400), pending)
        .await
        .expect("retirement waited beyond cancellation grace")
        .unwrap();
    assert!(result.is_err());
    assert_eq!(f.io.read_dropped.load(Ordering::SeqCst), 1);
    assert!(
        !f.cx.cancel().is_cancelled(),
        "retirement cancelled parent request"
    );
    assert_eq!(
        call(
            &f,
            "arithmetic",
            "increment",
            args(&[("value", CanonValue::Int(11))])
        )
        .await
        .unwrap(),
        CanonValue::Int(12)
    );
}

struct SmallStreamOutput;
#[async_trait]
impl Mediator for SmallStreamOutput {
    async fn review(&self, prompt: &ApprovalPrompt) -> Verdict {
        if matches!(
            prompt.effect,
            EffectRequest::WasmEntry {
                entry: WasmEntryKind::StreamOpen { .. },
                ..
            }
        ) {
            let mut allowed = permission(&[]);
            allowed.wasm.output_bytes = 32;
            Verdict::Constrain(allowed)
        } else {
            Verdict::Pass
        }
    }
}
#[tokio::test]
async fn stream_uses_own_entry_mediator_budget_across_multiple_pulls() {
    let f = fixture_mediated(
        false,
        true,
        MediatorChain::new(vec![Arc::new(SmallStreamOutput)]),
    )
    .await;
    let StepOutput::Stream(mut stream) = f
        .host
        .call(
            &f.cx,
            &shape("arithmetic"),
            &OpName::parse("count-stream").unwrap(),
            args(&[("count", CanonValue::Int(4))]),
        )
        .await
        .unwrap()
    else {
        panic!("stream metadata ignored")
    };
    assert_eq!(stream.pull(2).await.unwrap().len(), 2);
    assert_eq!(stream.pull(1).await.unwrap_err().code, "limit-exceeded");
}
#[tokio::test]
async fn json_ui_cannot_mint_a_guest_local_callback_pointer() {
    let f = fixture(false, true).await;
    let cx = f.cx.child(Principal::PackUi {
        pack: PackId::parse("local.test-consumer").unwrap(),
        hash: f.consumer_hash.clone(),
        ui: UiSessionId::parse("fixture-ui").unwrap(),
    });
    let value = args(&[
        ("callback", args(&[("id", CanonValue::Int(1))])),
        ("value", CanonValue::Int(7)),
    ]);
    assert!(
        f.host
            .call(
                &cx,
                &shape("arithmetic"),
                &OpName::parse("with-callback").unwrap(),
                value
            )
            .await
            .is_err()
    );
}
#[tokio::test]
async fn old_handle_cannot_read_or_cancel_a_recreated_instance_local_id() {
    let f = fixture(false, true).await;
    let get = || async {
        f.host
            .call(
                &f.cx,
                &shape("arithmetic"),
                &OpName::parse("count-stream").unwrap(),
                args(&[("count", CanonValue::Int(3))]),
            )
            .await
    };
    let StepOutput::Stream(mut old) = get().await.unwrap() else {
        panic!("missingstream")
    };
    assert!(call(&f, "arithmetic", "trap", args(&[])).await.is_err());
    let StepOutput::Stream(mut fresh) = get().await.unwrap() else {
        panic!("missingstream")
    };
    assert!(old.pull(1).await.is_err());
    old.cancel();
    assert_eq!(
        fresh.pull(1).await.unwrap(),
        vec![args(&[("index", CanonValue::Int(0))])]
    );
    assert_eq!(fresh.pull(2).await.unwrap().len(), 2);
    assert!(fresh.pull(1).await.unwrap().is_empty());
}
