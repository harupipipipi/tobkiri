//! First bounded migration slice of the retained `port_proxy.rs` suite.
//!
//! P01 preserves the real Rust consumer/provider result and durable audit
//! assertions. P02 preserves denial despite the consumer's typed import.
//! Dispatch uses Core::open/save_profile/start and Session::call_user_port;
//! this fixture never creates an execution frame, provider bridge or proof.
//! All 22 originals remain in port_proxy.rs until the complete current
//! Core suite, independent P20 mediator case and coverage mapping pass.

use async_trait::async_trait;
use std::{
    collections::{BTreeMap, BTreeSet},
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use tobkiri_canon::{CanonValue, Digest, Variant, ids::*};
use tobkiri_core::{Core, CoreConfig, Session};
use tobkiri_gate::{
    ApprovalAnswer, ApprovalPrompt, ApprovalStore, CallCx, CoreCancellation, EffectRequest, Gate,
    GateEngine, HostError, HostIo, LoopLimits, MediatorChain, Outcome, PermissionSet, Principal,
    ResourceLimits, TrustedPrompt, UiEmbed,
};
use tobkiri_manifest::{
    FileEntry, PackManifest, PortDeclaration, Ports, RuntimeSpec, ShapeDefinition, ShapeKind,
    ShapeSidecar, VerifiedPack,
};
use tobkiri_profile::{PackPin, Profile, Startup};
use tobkiri_store::{Approvals, AuditLog, AuditQuota, AuditRow, DataRoot};

type TestResult<T = ()> = Result<T, Box<dyn std::error::Error + Send + Sync>>;
const PROVIDER: &str = "local.test-provider";
const CONSUMER: &str = "local.test-consumer";

fn shape(interface: &str) -> ShapeRef {
    ShapeRef::parse(&format!("local.proxy-test.{interface}@1.0")).unwrap()
}

fn permissions(interfaces: &[&str]) -> PermissionSet {
    PermissionSet {
        ports: interfaces
            .iter()
            .map(|name| shape(name).to_string())
            .collect(),
        wasm: ResourceLimits {
            fuel_per_call: 10_000_000,
            memory_mib: 16,
            wall_ms: 5000,
            table_elements: 10_000,
            output_bytes: 1024 * 1024,
        },
        loops: LoopLimits {
            max_iterations: 100,
            max_wall_ms: 5000,
            max_cost_units: 10_000,
        },
        ..Default::default()
    }
}

fn canonical(value: &impl serde::Serialize) -> TestResult<Vec<u8>> {
    let value = tobkiri_canon::parse(&serde_json::to_vec(value)?, Variant::StrictV1)?;
    Ok(tobkiri_canon::encode(&value, Variant::StrictV1)?)
}

#[derive(Default)]
struct Host {
    packs: Mutex<BTreeSet<(PackId, PackHash)>>,
    users: Mutex<BTreeSet<(Digest, Digest, u64)>>,
}

#[async_trait]
impl TrustedPrompt for Host {
    async fn prompt_approval(&self, prompt: ApprovalPrompt) -> ApprovalAnswer {
        let allowed = match prompt.effect {
            EffectRequest::Permissions { pack, hash } => {
                self.packs.lock().unwrap().contains(&(pack, hash))
            }
            EffectRequest::UserPermissions {
                profile_hash,
                revision,
                epoch,
            } if matches!(prompt.principal, Principal::User) => self
                .users
                .lock()
                .unwrap()
                .contains(&(profile_hash, revision, epoch)),
            EffectRequest::PortCall { shape: view, op }
                if matches!(prompt.principal, Principal::User) =>
            {
                view == shape("entry") && op.as_str() == "run"
            }
            EffectRequest::AuditRead if matches!(prompt.principal, Principal::User) => true,
            _ => false,
        };
        if allowed {
            ApprovalAnswer::Approve
        } else {
            ApprovalAnswer::Deny
        }
    }
}

#[async_trait]
impl HostIo for Host {
    fn display(&self, _: &str, _: &CanonValue) {}
    async fn terminal_read_line(&self, _: &str) -> Result<String, HostError> {
        Err(HostError(
            "typed proxy fixture has no terminal input".into(),
        ))
    }
    fn terminal_write(&self, _: &str) {}
    fn embed_ui(&self, _: UiEmbed) -> Result<UiSessionId, HostError> {
        Err(HostError("typed proxy fixture has no UI".into()))
    }
    async fn pick_directory(&self, _: &str) -> Option<PathBuf> {
        None
    }
}

fn manifest(id: &str, runtime: RuntimeSpec, permissions: PermissionSet) -> PackManifest {
    PackManifest {
        format: "tobkiri.pack/1".into(),
        id: PackId::parse(id).unwrap(),
        version: "1.0.0".into(),
        publisher_key: None,
        runtime,
        shapes: Vec::new(),
        ports: Ports::default(),
        inputs: Vec::new(),
        flows: Vec::new(),
        overlays: Vec::new(),
        stores: Vec::new(),
        permissions,
        ui: None,
        files: Vec::new(),
    }
}

fn port(interface: &str) -> PortDeclaration {
    let view = shape(interface);
    PortDeclaration {
        shape: view.name,
        version: view.version,
        optional: false,
    }
}

fn write_pack(
    root: &Path,
    mut manifest: PackManifest,
    files: BTreeMap<String, Vec<u8>>,
) -> TestResult<(PackPin, VerifiedPack)> {
    let directory = root.join("packs").join(manifest.id.as_str());
    std::fs::create_dir_all(&directory)?;
    manifest.files = files
        .iter()
        .map(|(path, bytes)| FileEntry {
            path: path.clone(),
            sha256: Digest::hash(bytes),
            size: bytes.len() as u64,
        })
        .collect();
    for (name, bytes) in files {
        let path = directory.join(name);
        std::fs::create_dir_all(path.parent().unwrap())?;
        std::fs::write(path, bytes)?;
    }
    std::fs::write(
        directory.join("pack.json"),
        tobkiri_manifest::canonical_manifest(&manifest)?,
    )?;
    let verified = tobkiri_manifest::verify_directory(&directory, &BTreeMap::new())?;
    let pin = PackPin {
        id: verified.manifest.id.clone(),
        version: verified.manifest.version.clone(),
        sha256: verified.hash.clone(),
        source: format!("file:{}", directory.display()),
    };
    Ok((pin, verified))
}

fn artifact(binary: &str) -> TestResult<Vec<u8>> {
    let configured = std::env::var_os("CARGO_TARGET_DIR")
        .map(PathBuf::from)
        .map(|root| root.join("wasm32-wasip2/debug").join(binary));
    let fallback = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../.work/cargo-target/wasm32-wasip2/debug")
        .join(binary);
    Ok(std::fs::read(
        configured.filter(|path| path.is_file()).unwrap_or(fallback),
    )?)
}

struct Fixture {
    _directory: tempfile::TempDir,
    root: PathBuf,
    host: Arc<Host>,
    core: Core,
    profile: Profile,
    verified: Vec<VerifiedPack>,
}

impl Fixture {
    fn new(consumer_allowed: bool) -> TestResult<Self> {
        let directory = tempfile::tempdir()?;
        let root = directory.path().canonicalize()?;
        let wit = include_bytes!("../../test-guests/wit/fixtures.wit").to_vec();
        let mut contracts = manifest("local.proxy-test", RuntimeSpec::Data, permissions(&[]));
        contracts.shapes = ["arithmetic", "entry"]
            .into_iter()
            .map(|name| {
                let view = shape(name);
                ShapeDefinition {
                    name: view.name,
                    version: view.version,
                    kind: ShapeKind::Port,
                    wit: "wit/fixtures.wit".into(),
                }
            })
            .collect();
        let sidecar = canonical(&ShapeSidecar {
            cardinality: tobkiri_manifest::Cardinality::One,
            streams: BTreeMap::new(),
            callbacks: BTreeMap::new(),
            conformance: None,
        })?;
        let mut packs = vec![write_pack(
            &root,
            contracts,
            BTreeMap::from([
                ("wit/fixtures.wit".into(), wit.clone()),
                ("wit/arithmetic.shape.json".into(), sidecar.clone()),
                ("wit/entry.shape.json".into(), sidecar),
            ]),
        )?];
        for (id, binary, provided, consumed) in [
            (PROVIDER, "tobkiri_test_provider.wasm", "arithmetic", None),
            (
                CONSUMER,
                "tobkiri_test_consumer.wasm",
                "entry",
                Some("arithmetic"),
            ),
        ] {
            let mut declaration = permissions(if id == CONSUMER && consumer_allowed {
                &["arithmetic"]
            } else {
                &[]
            });
            if id == PROVIDER {
                declaration.host_io =
                    BTreeSet::from(["terminal-read-line".into(), "request-ui-embed".into()]);
            }
            let mut runtime = manifest(
                id,
                RuntimeSpec::Wasm {
                    core_modules: Default::default(),
                    component: "component.wasm".into(),
                    toolchain: "rust-1.94.0/wasm32-wasip2".into(),
                    config: None,
                },
                declaration,
            );
            runtime.ports.provides = vec![port(provided)];
            // This declaration remains present in the denied fixture.
            runtime.ports.consumes = consumed.map(port).into_iter().collect();
            packs.push(write_pack(
                &root,
                runtime,
                BTreeMap::from([
                    ("component.wasm".into(), artifact(binary)?),
                    ("wit/fixtures.wit".into(), wit.clone()),
                ]),
            )?);
        }
        let mut user = permissions(&["entry"]);
        user.audit_read = true;
        let mut ceiling = permissions(&["entry", "arithmetic"]);
        ceiling.audit_read = true;
        ceiling.host_io = BTreeSet::from(["terminal-read-line".into(), "request-ui-embed".into()]);
        let profile = Profile {
            format: "tobkiri.profile/1".into(),
            id: ProfileId::parse("prof_wasm_typed_proxy")?,
            name: "Wasm public Core typed proxy".into(),
            packs: packs.iter().map(|(pin, _)| pin.clone()).collect(),
            publishers: BTreeMap::new(),
            ceiling,
            user_permissions: Some(user),
            pack_limits: packs
                .iter()
                .map(|(_, pack)| (pack.manifest.id.clone(), pack.manifest.permissions.clone()))
                .collect(),
            bindings: BTreeMap::new(),
            overlay_order: Vec::new(),
            user_overlays: Vec::new(),
            startup: Startup {
                inputs: Vec::new(),
                ui: None,
            },
        };
        let verified: Vec<_> = packs.into_iter().map(|(_, pack)| pack).collect();
        let compiled = tobkiri_profile::compile(&profile, &verified)
            .map_err(|conflicts| format!("typed proxy fixture conflicts: {conflicts:?}"))?;
        let host = Arc::new(Host::default());
        *host.packs.lock().unwrap() = verified
            .iter()
            .map(|pack| (pack.manifest.id.clone(), pack.hash.clone()))
            .collect();
        host.users.lock().unwrap().insert((
            Digest::hash(&canonical(&profile)?),
            compiled.plan_digest,
            1,
        ));
        let root = root.join("data");
        let core = Core::open(CoreConfig::for_test_data_root(root.clone()), host.clone())?;
        Ok(Self {
            _directory: directory,
            root,
            host,
            core,
            profile,
            verified,
        })
    }

    async fn start(&self) -> TestResult<Session> {
        self.core.save_profile(self.profile.clone(), None).await?;
        Ok(self.core.start(&self.profile.id).await?)
    }

    async fn audit(&self, session: &Session) -> TestResult<Vec<AuditRow>> {
        // Inspection has its own explicitly approved User read grant. This
        // Gate never dispatches a provider or constructs execution authority.
        let root = DataRoot::open(&self.root)?;
        let approvals = Arc::new(Approvals::open(&root, &self.profile.id)?);
        let audit = Arc::new(AuditLog::open(
            &root,
            &self.profile.id,
            AuditQuota::default(),
        )?);
        let gate = GateEngine::new(
            approvals.clone(),
            audit.clone(),
            self.host.clone(),
            MediatorChain::default(),
        );
        let (profile, document) = self.core.profile(&self.profile.id)?;
        let compiled = tobkiri_profile::compile(&profile, &self.verified)
            .map_err(|conflicts| format!("audit fixture conflicts: {conflicts:?}"))?;
        assert_eq!(&compiled.plan_digest, session.plan_digest());
        let revision = session.plan_digest();
        let epoch = approvals.security_epoch(&profile.id)?;
        gate.register_operation_views(
            &profile.id,
            revision,
            compiled
                .views
                .iter()
                .map(|(reference, view)| {
                    (reference.clone(), view.functions.keys().cloned().collect())
                })
                .collect(),
        )?;
        gate.register_revision(&profile.id, revision, BTreeMap::new())?;
        gate.register_user_policy_for_revision(
            &profile.id,
            &document,
            revision,
            epoch,
            profile.user_permissions.clone().unwrap(),
            profile.ceiling.clone(),
        )?;
        let cx = CallCx::root(
            profile.id,
            Principal::User,
            RequestId::parse("wasm-typed-audit")?,
            CoreCancellation::new(),
            Instant::now() + Duration::from_secs(5),
            profile.ceiling.wasm,
        )
        .with_security_epoch(epoch)
        .with_revision(revision.clone());
        let grant = gate
            .authorize(
                &cx,
                EffectRequest::AuditRead,
                AuditLog::read_digest(0, 1000)?,
            )
            .await?;
        let id = grant.id().clone();
        let rows = audit.read(&cx, 0, 1000, grant)?;
        gate.record_outcome(id, Outcome::Succeeded).await?;
        Ok(rows)
    }
}

fn arguments() -> CanonValue {
    CanonValue::Obj(BTreeMap::from([("value".into(), CanonValue::Int(40))]))
}

fn pack_event(rows: &[AuditRow], kind: &str, pack: &str) -> bool {
    rows.iter().any(|row| {
        row.event.kind == kind
            && row
                .event
                .principal
                .pack()
                .is_some_and(|(id, _)| id.as_str() == pack)
    })
}

#[tokio::test]
async fn rust_consumer_calls_rust_provider_through_public_core_and_durable_audit() -> TestResult {
    let fixture = Fixture::new(true)?;
    let session = fixture.start().await?;
    let buffer = session
        .call_user_port(shape("entry"), OpName::parse("run")?, &arguments())
        .await?;
    assert_eq!(
        tobkiri_canon::parse(buffer.as_bytes(), Variant::StrictV1)?,
        CanonValue::Int(82)
    );
    let rows = fixture.audit(&session).await?;
    for pack in [CONSUMER, PROVIDER] {
        assert!(pack_event(&rows, "dispatch", pack));
        assert!(pack_event(&rows, "outcome", pack));
    }
    session.close().await?;
    Ok(())
}

#[tokio::test]
async fn declared_typed_import_still_cannot_call_without_effective_port_permission() -> TestResult {
    let fixture = Fixture::new(false)?;
    let consumer = fixture
        .verified
        .iter()
        .find(|pack| pack.manifest.id.as_str() == CONSUMER)
        .unwrap();
    assert_eq!(consumer.manifest.ports.consumes.len(), 1);
    assert_eq!(
        consumer.manifest.ports.consumes[0].shape,
        shape("arithmetic").name
    );
    assert!(
        !consumer
            .manifest
            .permissions
            .ports
            .contains(&shape("arithmetic").to_string())
    );
    let session = fixture.start().await?;
    assert!(
        session
            .call_user_port(shape("entry"), OpName::parse("run")?, &arguments())
            .await
            .is_err()
    );
    let rows = fixture.audit(&session).await?;
    assert!(pack_event(&rows, "dispatch", CONSUMER));
    assert!(pack_event(&rows, "denied", CONSUMER));
    assert!(!pack_event(&rows, "dispatch", PROVIDER));
    session.close().await?;
    Ok(())
}
