use crate::{CoreError, core::register_policies};
use async_trait::async_trait;
use std::{
    collections::{BTreeMap, BTreeSet},
    sync::Arc,
    time::{Duration, Instant},
};
use tobkiri_canon::{
    Digest,
    ids::{OpName, PackHash, PackId, ProfileId, RequestId, ShapeRef, UiSessionId},
};
use tobkiri_gate::{
    ApprovalAnswer, ApprovalPrompt, ApprovalStore, AuditEvent, AuditSink, CallCx, EffectRequest,
    Gate, GateEngine, GateError, LoopLimits, MediatorChain, PermissionSet, Principal,
    ResourceLimits, TrustedPrompt,
};
use tobkiri_manifest::{PackManifest, PortDeclaration, Ports, RuntimeSpec, VerifiedPack};
use tobkiri_profile::{Profile, Startup};
use tobkiri_store::{Approvals, DataRoot};
struct Prompt;
#[async_trait]
impl TrustedPrompt for Prompt {
    async fn prompt_approval(&self, _: ApprovalPrompt) -> ApprovalAnswer {
        ApprovalAnswer::Approve
    }
}
#[derive(Default)]
struct Audit(std::sync::Mutex<BTreeMap<Digest, Vec<u8>>>);
impl AuditSink for Audit {
    fn append(&self, _: AuditEvent) -> Result<(), GateError> {
        Ok(())
    }
    fn append_idempotent(&self, id: &Digest, event: AuditEvent) -> Result<(), GateError> {
        crate::test_support::append_once(&self.0, id, &event, || Ok(()))
    }
}
struct Fixture {
    gate: GateEngine,
    profile: Profile,
    pack: VerifiedPack,
    approved: PermissionSet,
    _dir: tempfile::TempDir,
    approvals: Arc<Approvals>,
}
fn shape(minor: u32) -> ShapeRef {
    ShapeRef::parse(&format!("local.fixture.port@1.{minor}")).unwrap()
}
fn fixture() -> Fixture {
    let dir = tempfile::tempdir().unwrap();
    let root = DataRoot::open(std::fs::canonicalize(dir.path()).unwrap()).unwrap();
    let id = ProfileId::parse("prof_policy").unwrap();
    let pack = PackId::parse("local.caller").unwrap();
    let permissions = PermissionSet {
        ports: BTreeSet::from([shape(0).to_string()]),
        wasm: ResourceLimits {
            fuel_per_call: 100000,
            memory_mib: 8,
            wall_ms: 1000,
            table_elements: 1024,
            output_bytes: 1048576,
        },
        loops: LoopLimits {
            max_iterations: 8,
            max_wall_ms: 1000,
            max_cost_units: 100,
        },
        ..Default::default()
    };
    let approvals = Arc::new(Approvals::open(&root, &id).unwrap());
    let hash = PackHash::new(Digest::hash(b"caller"));
    approvals
        .set_permissions(&id, &pack, &hash, &permissions)
        .unwrap();
    let gate = GateEngine::new(
        approvals.clone(),
        Arc::new(Audit::default()),
        Arc::new(Prompt),
        MediatorChain::default(),
    );
    let profile = Profile {
        format: "tobkiri.profile/1".into(),
        id,
        name: "Policy fixture".into(),
        packs: Vec::new(),
        publishers: BTreeMap::new(),
        ceiling: permissions.clone(),
        user_permissions: None,
        pack_limits: BTreeMap::from([(pack.clone(), permissions.clone())]),
        bindings: BTreeMap::new(),
        overlay_order: Vec::new(),
        user_overlays: Vec::new(),
        startup: Startup {
            inputs: Vec::new(),
            ui: None,
        },
    };
    let manifest = PackManifest {
        format: "tobkiri.pack/1".into(),
        id: pack,
        version: "1.0.0".into(),
        publisher_key: None,
        runtime: RuntimeSpec::Data,
        shapes: Vec::new(),
        ports: Ports {
            consumes: vec![PortDeclaration {
                shape: shape(1).name,
                version: shape(1).version,
                optional: false,
            }],
            ..Default::default()
        },
        inputs: Vec::new(),
        flows: Vec::new(),
        overlays: Vec::new(),
        stores: Vec::new(),
        permissions: permissions.clone(),
        ui: None,
        files: Vec::new(),
    };
    Fixture {
        gate,
        profile,
        pack: VerifiedPack {
            manifest,
            hash,
            files: BTreeMap::new(),
            shapes: Vec::new(),
        },
        approved: permissions,
        _dir: dir,
        approvals,
    }
}
impl Fixture {
    fn register(&self) -> Result<(), CoreError> {
        register_policies(&self.gate, &self.profile, std::slice::from_ref(&self.pack))?;
        self.gate.register_operation_views(
            &self.profile.id,
            &Digest::hash(b"policy-views"),
            BTreeMap::from([
                (
                    shape(0),
                    [OpName::parse("read").unwrap()].into_iter().collect(),
                ),
                (
                    shape(1),
                    [
                        OpName::parse("read").unwrap(),
                        OpName::parse("added").unwrap(),
                    ]
                    .into_iter()
                    .collect(),
                ),
            ]),
        )?;
        Ok(())
    }
    fn cx(&self, principal: Principal) -> CallCx {
        CallCx::root(
            self.profile.id.clone(),
            principal,
            RequestId::parse("req_policy").unwrap(),
            tobkiri_gate::CancellationToken::new(),
            Instant::now() + Duration::from_secs(1),
            self.profile.ceiling.wasm.clone(),
        )
        .with_revision(Digest::hash(b"policy-views"))
    }
    fn pack_cx(&self) -> CallCx {
        self.cx(Principal::Pack {
            pack: self.pack.manifest.id.clone(),
            hash: self.pack.hash.clone(),
        })
    }
    async fn allowed(&self, reference: ShapeRef) -> bool {
        self.gate
            .authorize(
                &self.pack_cx(),
                EffectRequest::PortCall {
                    shape: reference,
                    op: OpName::parse("read").unwrap(),
                },
                Digest::hash(b"args"),
            )
            .await
            .is_ok()
    }
}
#[tokio::test]
async fn binding_and_consume_declarations_never_add_rights() {
    let mut fixture = fixture();
    fixture.register().unwrap();
    let before = fixture
        .gate
        .effective_permissions(&fixture.pack_cx())
        .unwrap();
    assert!(fixture.allowed(shape(0)).await);
    fixture.profile.bindings.insert(
        shape(1).to_string(),
        PackId::parse("local.new-provider").unwrap(),
    );
    fixture.register().unwrap();
    let after = fixture
        .gate
        .effective_permissions(&fixture.pack_cx())
        .unwrap();
    assert_eq!(before.ports, after.ports);
    assert_eq!(after.ports, fixture.approved.ports);
    assert!(!fixture.allowed(shape(1)).await);
}
#[tokio::test]
async fn original_empty_or_absent_policy_operands_deny_port_calls() {
    for absent in [
        "declaration",
        "ceiling",
        "limit",
        "missing-limit",
        "approval",
    ] {
        let mut fixture = fixture();
        match absent {
            "declaration" => fixture.pack.manifest.permissions.ports.clear(),
            "ceiling" => fixture.profile.ceiling.ports.clear(),
            "limit" => fixture
                .profile
                .pack_limits
                .get_mut(&fixture.pack.manifest.id)
                .unwrap()
                .ports
                .clear(),
            "missing-limit" => {
                fixture
                    .profile
                    .pack_limits
                    .remove(&fixture.pack.manifest.id);
            }
            "approval" => {
                let mut approval = fixture.approved.clone();
                approval.ports.clear();
                fixture
                    .approvals
                    .set_permissions(
                        &fixture.profile.id,
                        &fixture.pack.manifest.id,
                        &fixture.pack.hash,
                        &approval,
                    )
                    .unwrap();
            }
            _ => unreachable!(),
        }
        fixture.register().unwrap();
        assert!(!fixture.allowed(shape(0)).await, "{absent}");
        if absent == "missing-limit" {
            let effective = fixture
                .gate
                .effective_permissions(&fixture.pack_cx())
                .unwrap();
            assert_eq!(effective.wasm.fuel_per_call, 0);
            assert_eq!(effective.wasm.memory_mib, 0);
        }
    }
}
#[tokio::test]
async fn synthetic_user_and_ui_cannot_borrow_profile_or_other_pack_permissions() {
    let fixture = fixture();
    fixture.register().unwrap();
    let user = fixture.cx(Principal::User);
    assert!(fixture.gate.effective_permissions(&user).is_err());
    assert!(
        fixture
            .gate
            .authorize(
                &user,
                EffectRequest::PortCall {
                    shape: shape(0),
                    op: OpName::parse("read").unwrap()
                },
                Digest::hash(b"user")
            )
            .await
            .is_err()
    );
    let ui = fixture.cx(Principal::PackUi {
        pack: PackId::parse("local.ui").unwrap(),
        hash: PackHash::new(Digest::hash(b"ui")),
        ui: UiSessionId::parse("ui_fixture").unwrap(),
    });
    assert!(
        fixture
            .gate
            .authorize(
                &ui,
                EffectRequest::PortCall {
                    shape: shape(0),
                    op: OpName::parse("read").unwrap()
                },
                Digest::hash(b"ui")
            )
            .await
            .is_err()
    );
}

#[tokio::test]
async fn compatible_provider_new_operations_do_not_expand_the_old_caller_view() {
    let fixture = fixture();
    fixture.register().unwrap();
    assert!(fixture.allowed(shape(0)).await);
    assert!(
        fixture
            .gate
            .authorize(
                &fixture.pack_cx(),
                EffectRequest::PortCall {
                    shape: shape(0),
                    op: OpName::parse("added").unwrap()
                },
                Digest::hash(b"added operation")
            )
            .await
            .is_err()
    );
    assert!(!fixture.allowed(shape(1)).await);
}
