//! Actual Wasm Core storage regression on two fresh Profiles under one data root.
//! Public writes are deliberately sequential. This does not replay a migration.
mod support;

use anyhow::{Context, Result, ensure};
use async_trait::async_trait;
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
};
use support::{Host, parse_output, physical_temp, within_session};
use tobkiri_canon::{
    CanonValue, Digest, Variant,
    ids::{ProfileId, ShapeRef, UiSessionId},
};
use tobkiri_core::{Core, CoreConfig, Session};
use tobkiri_gate::{
    ApprovalAnswer, ApprovalPrompt, EffectRequest, HostError, HostIo, LoopLimits, PermissionSet,
    Principal, ResourceLimits, StoragePermissions, StoreOperation, StoreTier, TrustedPrompt,
    UiEmbed,
};
use tobkiri_manifest::{
    FileEntry, PackManifest, PortDeclaration, Ports, RuntimeSpec, ShapeDefinition, ShapeKind,
    StoreDefinition, VerifiedPack,
};
use tobkiri_profile::{PackPin, Profile, Startup};

const OWNER: &str = "local.storage-tier-owner";
const WRITER: &str = "local.storage-tier-writer";
const SECOND_WRITER: &str = "local.storage-tier-second-writer";
const PRIVATE: &str = "vault";
const READ_ONLY: &str = "catalog";
const PUBLIC: &str = "notes";
const OTHER_PUBLIC: &str = "other-notes";
const READ: &str = "read-marker";
const WRITE: &str = "write-marker";
const OPEN_DENIED: i64 = 256;
const WRITE_DENIED: i64 = 512;

#[derive(Debug, PartialEq, Eq)]
struct StoragePrompt {
    profile: ProfileId,
    principal: Principal,
    effect: EffectRequest,
    args_digest: Digest,
}

#[derive(Default)]
struct StoragePrompts {
    expected: Option<StoragePrompt>,
    // At most two are needed: two already violates every per-call expectation.
    observed: Vec<StoragePrompt>,
}

#[derive(Default)]
struct StorageHost {
    admission: Host,
    storage: Mutex<StoragePrompts>,
}

#[async_trait]
impl TrustedPrompt for StorageHost {
    async fn prompt_approval(&self, prompt: ApprovalPrompt) -> ApprovalAnswer {
        if !matches!(&prompt.effect, EffectRequest::Storage { .. }) {
            // Existing test host accepts only reviewed Pack hashes, literal
            // User declarations and the two explicitly registered operations.
            return self.admission.prompt_approval(prompt).await;
        }
        let mut storage = self.storage.lock().unwrap();
        let observed = StoragePrompt {
            profile: prompt.profile,
            principal: prompt.principal,
            effect: prompt.effect,
            args_digest: prompt.args_digest,
        };
        let allowed = storage.observed.is_empty() && storage.expected.as_ref() == Some(&observed);
        if storage.observed.len() < 2 {
            storage.observed.push(observed);
        }
        if allowed {
            ApprovalAnswer::Approve
        } else {
            ApprovalAnswer::Deny
        }
    }
}

#[async_trait]
impl HostIo for StorageHost {
    fn display(&self, _: &str, _: &CanonValue) {}
    async fn terminal_read_line(&self, _: &str) -> Result<String, HostError> {
        Err(HostError("no terminal in storage fixture".into()))
    }
    fn terminal_write(&self, _: &str) {
        panic!("no terminal in storage fixture");
    }
    fn embed_ui(&self, _: UiEmbed) -> Result<UiSessionId, HostError> {
        Err(HostError("no UI in storage fixture".into()))
    }
    async fn pick_directory(&self, _: &str) -> Option<PathBuf> {
        None
    }
}

impl StorageHost {
    fn expect_storage(&self, expected: Option<StoragePrompt>) -> Result<()> {
        let mut storage = self.storage.lock().unwrap();
        ensure!(
            storage.expected.is_none() && storage.observed.is_empty(),
            "unexpected storage prompts before the guest operation: {:?}",
            storage.observed
        );
        storage.expected = expected;
        Ok(())
    }

    fn verify_storage(&self) -> Result<()> {
        let StoragePrompts { expected, observed } =
            std::mem::take(&mut *self.storage.lock().unwrap());
        ensure!(
            expected.as_slice() == observed.as_slice(),
            "storage prompt expected {expected:?}; observed {observed:?}"
        );
        Ok(())
    }
}

fn permissions() -> PermissionSet {
    PermissionSet {
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

fn storage_permissions() -> StoragePermissions {
    StoragePermissions {
        private: true,
        public_read: true,
        public_write: true,
    }
}

struct Pack {
    pin: PackPin,
    artifact: VerifiedPack,
    probe: ShapeRef,
}

fn pack(root: &Path, id: &str) -> Result<Pack> {
    let id = tobkiri_canon::ids::PackId::parse(id)?;
    let name = id.name();
    let foreign = id.as_str() != OWNER;
    let wat = include_str!("storage_tiers/probe.wat")
        .replace("@PACK@", name)
        .replace("@OWNER@", OWNER)
        .replace("@OWNER_LEN@", &OWNER.len().to_string())
        .replace("@FOREIGN@", if foreign { "1" } else { "0" });
    let probe = ShapeRef::parse(&format!("{id}.probe@1.0"))?;
    let data = ShapeRef::parse(&format!("{id}.data@1.0"))?;
    let data_wit = if foreign {
        ""
    } else {
        "interface data { type data = u8; }"
    };
    let mut files: BTreeMap<String, Vec<u8>> = BTreeMap::from([
        ("component.wasm".into(), wat::parse_str(&wat)?),
        (
            "wit/pack.wit".into(),
            format!(
                "package local:{name}-component@1.0.0; world pack {{\n\
                 include tobkiri:core/pack-minimal@1.2.0;\n\
                 import tobkiri:core/storage@1.2.0;\n\
                 export local:{name}/probe@1.0.0;\n}}"
            )
            .into_bytes(),
        ),
        (
            "wit/probe.wit".into(),
            format!(
                "package local:{name}@1.0.0; interface probe {{\n\
                 {READ}: func(store: string) -> u32;\n\
                 {WRITE}: func(store: string, value: u8) -> u32;\n}}\n{data_wit}"
            )
            .into_bytes(),
        ),
    ]);
    let sidecar = br#"{"cardinality":"one","streams":{},"callbacks":{}}"#;
    files.insert("wit/probe.shape.json".into(), sidecar.to_vec());
    let mut shapes = vec![ShapeDefinition {
        name: probe.name.clone(),
        version: probe.version,
        kind: ShapeKind::Port,
        wit: "wit/probe.wit".into(),
    }];
    let mut stores = Vec::new();
    if !foreign {
        files.insert("wit/data.shape.json".into(), sidecar.to_vec());
        shapes.push(ShapeDefinition {
            name: data.name.clone(),
            version: data.version,
            kind: ShapeKind::Data,
            wit: "wit/probe.wit".into(),
        });
        use tobkiri_manifest::StoreTier::{Private, Public, ReadOnly};
        for (name, tier) in [
            (PRIVATE, Private),
            (READ_ONLY, ReadOnly),
            (PUBLIC, Public),
            (OTHER_PUBLIC, Public),
        ] {
            stores.push(StoreDefinition {
                name: name.into(),
                tier,
                data_shape: data.clone(),
                seed: None,
            });
        }
    }
    let manifest = PackManifest {
        format: "tobkiri.pack/1".into(),
        id: id.clone(),
        version: "0.1.0".into(),
        publisher_key: None,
        runtime: RuntimeSpec::Wasm {
            core_modules: Default::default(),
            component: "component.wasm".into(),
            toolchain: "wat/storage-tier-test".into(),
            config: None,
        },
        shapes,
        ports: Ports {
            provides: vec![PortDeclaration {
                shape: probe.name.clone(),
                version: probe.version,
                optional: false,
            }],
            ..Default::default()
        },
        inputs: vec![],
        flows: vec![],
        overlays: vec![],
        stores,
        // Deliberately equal rights. The foreign denial must come from the
        // real caller's identity and the store's tier, not a missing flag.
        permissions: PermissionSet {
            storage: storage_permissions(),
            ..permissions()
        },
        ui: None,
        files: files
            .iter()
            .map(|(path, bytes)| FileEntry {
                path: path.clone(),
                sha256: Digest::hash(bytes),
                size: bytes.len() as u64,
            })
            .collect(),
    };
    let directory = root.join(id.as_str());
    fs::create_dir_all(directory.join("wit"))?;
    for (path, bytes) in &files {
        fs::write(directory.join(path), bytes)?;
    }
    fs::write(
        directory.join("pack.json"),
        tobkiri_manifest::canonical_manifest(&manifest)?,
    )?;
    let artifact = tobkiri_manifest::verify_directory(&directory, &BTreeMap::new())?;
    let pin = PackPin {
        id,
        version: artifact.manifest.version.clone(),
        sha256: artifact.hash.clone(),
        source: format!("file:{}", directory.canonicalize()?.display()),
    };
    Ok(Pack {
        pin,
        artifact,
        probe,
    })
}

fn profile(id: &str, packs: &[&Pack]) -> Result<Profile> {
    let user = PermissionSet {
        ports: packs.iter().map(|pack| pack.probe.to_string()).collect(),
        ..permissions()
    };
    let ceiling = PermissionSet {
        storage: storage_permissions(),
        ..user.clone()
    };
    Ok(Profile {
        format: "tobkiri.profile/1".into(),
        id: id.parse()?,
        name: "Actual Wasm storage tiers".into(),
        packs: packs.iter().map(|pack| pack.pin.clone()).collect(),
        publishers: BTreeMap::new(),
        ceiling,
        user_permissions: Some(user),
        pack_limits: packs
            .iter()
            .map(|pack| {
                (
                    pack.pin.id.clone(),
                    pack.artifact.manifest.permissions.clone(),
                )
            })
            .collect(),
        bindings: packs
            .iter()
            .map(|pack| (format!("{}@1", pack.probe.name), pack.pin.id.clone()))
            .collect(),
        overlay_order: vec![],
        user_overlays: vec![],
        startup: Startup {
            inputs: vec![],
            ui: None,
        },
    })
}

struct Replay<'a> {
    host: &'a StorageHost,
    session: &'a Session,
    profile: &'a ProfileId,
}

#[derive(Clone, Copy)]
enum Operation {
    Read,
    Write(u8),
    FirstPublicWrite(u8),
}

impl Replay<'_> {
    async fn check(
        &self,
        pack: &Pack,
        store: &str,
        operation: Operation,
        expected: i64,
    ) -> Result<()> {
        let mut args = BTreeMap::from([("store".into(), CanonValue::Str(store.into()))]);
        let (op, prompt) = match operation {
            Operation::Read => (READ, None),
            Operation::Write(value) | Operation::FirstPublicWrite(value) => {
                args.insert("value".into(), CanonValue::Int(value.into()));
                let prompt = if matches!(operation, Operation::FirstPublicWrite(_)) {
                    let payload = CanonValue::Obj(BTreeMap::from([
                        ("op".into(), CanonValue::Str("kv-put".into())),
                        ("key".into(), CanonValue::Str("marker".into())),
                        (
                            "value".into(),
                            CanonValue::Arr(vec![CanonValue::Int(value.into())]),
                        ),
                    ]));
                    Some(StoragePrompt {
                        profile: self.profile.clone(),
                        principal: Principal::Pack {
                            pack: pack.pin.id.clone(),
                            hash: pack.pin.sha256.clone(),
                        },
                        effect: EffectRequest::Storage {
                            profile: self.profile.clone(),
                            owner: OWNER.parse()?,
                            store: store.into(),
                            tier: StoreTier::Public,
                            operation: StoreOperation::Write,
                        },
                        args_digest: tobkiri_canon::digest(&payload, Variant::StrictV1)?,
                    })
                } else {
                    None
                };
                (WRITE, prompt)
            }
        };
        let stage = format!("{}: {} {op} {store}", self.profile, pack.pin.id);
        let op = op.parse()?;
        let args = CanonValue::Obj(args);
        self.host.expect_storage(prompt)?;
        let output = within_session(
            self.session,
            &stage,
            self.session.call_user_port(pack.probe.clone(), op, &args),
        )
        .await;
        self.host.verify_storage().with_context(|| stage.clone())?;
        let output = parse_output(&output.with_context(|| stage.clone())?)?;
        ensure!(
            output == CanonValue::Int(expected),
            "{stage}: expected guest result {expected}, observed {output:?}"
        );
        Ok(())
    }
}

#[tokio::test]
async fn wasm_guests_isolate_storage_tiers_and_approve_each_public_writer_store_pair_once()
-> Result<()> {
    use Operation::{FirstPublicWrite, Read, Write};

    let directory = physical_temp()?;
    let owner = pack(directory.path(), OWNER)?;
    let writer = pack(directory.path(), WRITER)?;
    let second_writer = pack(directory.path(), SECOND_WRITER)?;
    let packs = [&owner, &writer, &second_writer];
    let a = profile("prof_storage_tiers_a", &packs)?;
    let b = profile("prof_storage_tiers_b", &packs)?;
    let host = Arc::new(StorageHost::default());
    for pack in packs {
        for op in [READ, WRITE] {
            host.admission
                .utility_ports
                .lock()
                .unwrap()
                .insert((pack.probe.to_string(), op.into()));
        }
    }
    host.admission.approve_literal_user(&a)?;
    host.admission.approve_literal_user(&b)?;
    let core = Core::open(
        CoreConfig::for_test_data_root(directory.path().join("data")),
        host.clone(),
    )?;
    core.save_profile(a.clone(), None).await?;
    core.save_profile(b.clone(), None).await?;
    let sa = core.start(&a.id).await?;
    let sb = match core.start(&b.id).await {
        Ok(session) => session,
        Err(error) => {
            let close = sa.close().await;
            return Err(anyhow::Error::new(error).context(format!(
                "second Profile start failed; first Session close: {close:?}"
            )));
        }
    };
    let left = Replay {
        host: &host,
        session: &sa,
        profile: &a.id,
    };
    let right = Replay {
        host: &host,
        session: &sb,
        profile: &b.id,
    };
    let checked = async {
        for (pack, store, op, expected) in [
            (&owner, PRIVATE, Write(11), 0),
            (&owner, PRIVATE, Read, 11),
            (&writer, PRIVATE, Read, OPEN_DENIED),
            (&writer, PRIVATE, Write(99), OPEN_DENIED),
            (&owner, READ_ONLY, Write(12), 0),
            (&writer, READ_ONLY, Read, 12),
            (&writer, READ_ONLY, Write(99), WRITE_DENIED),
            (&owner, READ_ONLY, Read, 12),
            (&writer, PUBLIC, Read, 0),
            (&writer, PUBLIC, FirstPublicWrite(13), 0),
            (&writer, PUBLIC, Write(14), 0),
            (&owner, PUBLIC, Read, 14),
            (&writer, OTHER_PUBLIC, Read, 0),
            (&writer, OTHER_PUBLIC, FirstPublicWrite(15), 0),
            (&writer, OTHER_PUBLIC, Write(16), 0),
            (&second_writer, PUBLIC, FirstPublicWrite(17), 0),
            (&second_writer, PUBLIC, Write(18), 0),
            (&writer, PUBLIC, Read, 18),
            (&writer, OTHER_PUBLIC, Read, 16),
        ] {
            left.check(pack, store, op, expected).await?;
        }
        // Same Pack IDs, hashes, stores, key and physical Core data root.
        // Profile B sees none of A's values or its public write approval.
        for (pack, store, op, expected) in [
            (&owner, PRIVATE, Read, 0),
            (&writer, READ_ONLY, Read, 0),
            (&writer, PUBLIC, Read, 0),
            (&writer, OTHER_PUBLIC, Read, 0),
            (&owner, PRIVATE, Write(21), 0),
            (&owner, READ_ONLY, Write(22), 0),
            (&writer, PUBLIC, FirstPublicWrite(23), 0),
            (&writer, PUBLIC, Write(24), 0),
            (&owner, PRIVATE, Read, 21),
            (&writer, READ_ONLY, Read, 22),
            (&writer, PUBLIC, Read, 24),
        ] {
            right.check(pack, store, op, expected).await?;
        }
        for (pack, store, expected) in [
            (&owner, PRIVATE, 11),
            (&writer, READ_ONLY, 12),
            (&writer, PUBLIC, 18),
            (&writer, OTHER_PUBLIC, 16),
        ] {
            left.check(pack, store, Read, expected).await?;
        }
        Ok::<(), anyhow::Error>(())
    }
    .await;
    let (ca, cb) = tokio::join!(sa.close(), sb.close());
    ensure!(
        ca.is_ok() && cb.is_ok(),
        "storage checks: {checked:?}; Session closes: {ca:?}; {cb:?}"
    );
    checked?;
    host.verify_storage()
}
