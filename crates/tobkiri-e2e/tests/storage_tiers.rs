//! Actual Wasm Core storage and publication regression across fresh Profiles.
//! Public writes are deliberately sequential. This does not replay a migration.
mod support;

use anyhow::{Context, Result, ensure};
use async_trait::async_trait;
use std::{
    collections::{BTreeMap, BTreeSet},
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
    ApprovalAnswer, ApprovalPrompt, ApprovalStore, EffectRequest, HostError, HostIo, LoopLimits,
    PermissionSet, Principal, ResourceLimits, Risk, StoragePermissions, StoreOperation, StoreTier,
    TrustedPrompt, UiEmbed,
};
use tobkiri_manifest::{
    FileEntry, PackManifest, PortDeclaration, Ports, RuntimeSpec, ShapeDefinition, ShapeKind,
    StoreDefinition, VerifiedPack,
};
use tobkiri_profile::{PackPin, Profile, Startup};
use tobkiri_store::{Approvals, DataRoot, UserGlobalStore};

const OWNER: &str = "local.storage-tier-owner";
const WRITER: &str = "local.storage-tier-writer";
const SECOND_WRITER: &str = "local.storage-tier-second-writer";
const PRIVATE: &str = "vault";
const READ_ONLY: &str = "catalog";
const PUBLIC: &str = "notes";
const OTHER_PUBLIC: &str = "other-notes";
const READ: &str = "read-marker";
const WRITE: &str = "write-marker";
const READ_GLOBAL: &str = "read-global-marker";
const WRITE_GLOBAL: &str = "write-global-marker";
const GLOBAL: &str = "shared-notes";
const KEY: &str = "marker";
const OPEN_DENIED: i64 = 256;
const WRITE_DENIED: i64 = 512;

#[derive(Debug, PartialEq, Eq)]
struct StoragePrompt {
    profile: ProfileId,
    principal: Principal,
    effect: EffectRequest,
    args_digest: Digest,
    security_epoch: u64,
    risk: Risk,
}

#[derive(Default)]
struct StoragePrompts {
    expected: Vec<StoragePrompt>,
    observed: Vec<StoragePrompt>,
}

#[derive(Debug, PartialEq, Eq)]
struct FreshUserApproval {
    profile: ProfileId,
    principal: Principal,
    effect: EffectRequest,
    args_digest: Digest,
    security_epoch: u64,
    risk: Risk,
}

#[derive(Default)]
struct ImportApprovals {
    source_profile: Option<ProfileId>,
    expected_user_permissions: Option<PermissionSet>,
    observed_user: Vec<FreshUserApproval>,
}

fn user_approval_digest(
    profile_hash: &Digest,
    revision: &Digest,
    epoch: u64,
    permissions: &PermissionSet,
) -> Result<Digest> {
    let value = serde_json::to_value((profile_hash, revision, epoch, permissions))?;
    Ok(tobkiri_canon::digest(
        &support::canon(&value)?,
        Variant::StrictV1,
    )?)
}

#[derive(Default)]
struct StorageHost {
    admission: Host,
    storage: Mutex<StoragePrompts>,
    imports: Mutex<ImportApprovals>,
}

#[async_trait]
impl TrustedPrompt for StorageHost {
    async fn prompt_approval(&self, prompt: ApprovalPrompt) -> ApprovalAnswer {
        if matches!(&prompt.effect, EffectRequest::UserPermissions { .. }) {
            let mut imports = self.imports.lock().unwrap();
            if let (Some(source_profile), Some(expected)) = (
                imports.source_profile.as_ref(),
                imports.expected_user_permissions.as_ref(),
            ) {
                let permissions = serde_json::from_str::<PermissionSet>(&prompt.summary).ok();
                let args_digest = match &prompt.effect {
                    EffectRequest::UserPermissions {
                        profile_hash,
                        revision,
                        epoch,
                    } => user_approval_digest(profile_hash, revision, *epoch, expected).ok(),
                    _ => None,
                };
                let approved = imports.observed_user.is_empty()
                    && &prompt.profile != source_profile
                    && matches!(&prompt.principal, Principal::User)
                    && matches!(
                        &prompt.effect,
                        EffectRequest::UserPermissions { epoch: 1, .. }
                    )
                    && prompt.security_epoch == 0
                    && prompt.risk == Risk::Dangerous
                    && permissions.as_ref() == Some(expected)
                    && args_digest.as_ref() == Some(&prompt.args_digest);
                imports.observed_user.push(FreshUserApproval {
                    profile: prompt.profile,
                    principal: prompt.principal,
                    effect: prompt.effect,
                    args_digest: prompt.args_digest,
                    security_epoch: prompt.security_epoch,
                    risk: prompt.risk,
                });
                return if approved {
                    ApprovalAnswer::Approve
                } else {
                    ApprovalAnswer::Deny
                };
            }
        }
        if !matches!(
            &prompt.effect,
            EffectRequest::Storage { .. } | EffectRequest::UserGlobalStorage { .. }
        ) {
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
            security_epoch: prompt.security_epoch,
            risk: prompt.risk,
        };
        let allowed = storage.expected.get(storage.observed.len()) == Some(&observed);
        if storage.observed.len() <= storage.expected.len() {
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
    fn expect_storage(&self, expected: Vec<StoragePrompt>) -> Result<()> {
        let mut storage = self.storage.lock().unwrap();
        ensure!(
            storage.expected.is_empty() && storage.observed.is_empty(),
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
            expected == observed,
            "storage prompt expected {expected:?}; observed {observed:?}"
        );
        Ok(())
    }

    fn expect_import_approval(
        &self,
        source_profile: &ProfileId,
        user_permissions: PermissionSet,
    ) -> Result<()> {
        let mut imports = self.imports.lock().unwrap();
        ensure!(
            imports.source_profile.is_none() && imports.observed_user.is_empty(),
            "unexpected import approval expectation"
        );
        imports.source_profile = Some(source_profile.clone());
        imports.expected_user_permissions = Some(user_permissions);
        Ok(())
    }

    fn verify_import_approval(&self, imported_profile: &ProfileId) -> Result<()> {
        let ImportApprovals {
            source_profile,
            expected_user_permissions,
            observed_user,
        } = std::mem::take(&mut *self.imports.lock().unwrap());
        let [observed] = observed_user.as_slice() else {
            anyhow::bail!(
                "expected one fresh imported User approval; observed {}",
                observed_user.len()
            );
        };
        ensure!(
            source_profile
                .as_ref()
                .is_some_and(|source| source != imported_profile),
            "imported Profile reused the source identity"
        );
        ensure!(
            &observed.profile == imported_profile
                && matches!(&observed.principal, Principal::User)
                && matches!(
                    &observed.effect,
                    EffectRequest::UserPermissions { epoch: 1, .. }
                )
                && observed.security_epoch == 0
                && observed.risk == Risk::Dangerous
                && matches!(
                    &observed.effect,
                    EffectRequest::UserPermissions {
                        profile_hash,
                        revision,
                        epoch: 1,
                    } if expected_user_permissions.as_ref().is_some_and(|permissions| {
                        user_approval_digest(profile_hash, revision, 1, permissions)
                            .is_ok_and(|digest| digest == observed.args_digest)
                    })
                ),
            "import did not obtain the exact fresh dangerous User approval"
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
                 {WRITE}: func(store: string, value: u8) -> u32;\n\
                 {READ_GLOBAL}: func() -> u32;\n\
                 {WRITE_GLOBAL}: func(value: u8) -> u32;\n}}\n{data_wit}"
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
            cross_profile_storage: true,
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
        cross_profile_storage: true,
        ..permissions()
    };
    let ceiling = PermissionSet {
        storage: storage_permissions(),
        cross_profile_storage: true,
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

fn approve_storage_user(host: &Host, profile: &Profile) -> Result<()> {
    let declared = profile
        .user_permissions
        .as_ref()
        .context("explicit User declaration required")?;
    let expected = PermissionSet {
        ports: host
            .utility_ports
            .lock()
            .unwrap()
            .iter()
            .map(|(shape, _)| shape.clone())
            .collect(),
        wasm: profile.ceiling.wasm.clone(),
        loops: profile.ceiling.loops.clone(),
        cross_profile_storage: true,
        ..Default::default()
    };
    ensure!(
        declared == &expected && declared.is_subset_of(&profile.ceiling),
        "fixture User declaration exceeds its reviewed utility and global-storage policy"
    );
    let packs = host.review_pack_permissions(profile)?;
    let compiled = tobkiri_profile::compile(profile, &packs)
        .map_err(|conflicts| anyhow::anyhow!("fixture profile conflicts: {conflicts:?}"))?;
    let hash = tobkiri_canon::digest(
        &support::canon(&serde_json::to_value(profile)?)?,
        Variant::StrictV1,
    )?;
    host.user_approvals.lock().unwrap().insert((
        hash.to_string(),
        compiled.plan_digest.to_string(),
        1,
    ));
    Ok(())
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
    GlobalRead,
    GlobalWrite(u8),
}

fn global_prompts(
    profile: &ProfileId,
    pack: &Pack,
    operation: StoreOperation,
    value: Option<u8>,
) -> Result<Vec<StoragePrompt>> {
    let (open_effect, open_args) = UserGlobalStore::open_request(GLOBAL)?;
    let call = if let Some(value) = value {
        CanonValue::Obj(BTreeMap::from([
            ("key".into(), CanonValue::Str(KEY.into())),
            ("op".into(), CanonValue::Str("kv-put".into())),
            (
                "value".into(),
                CanonValue::Arr(vec![CanonValue::Int(value.into())]),
            ),
        ]))
    } else {
        CanonValue::Obj(BTreeMap::from([
            ("key".into(), CanonValue::Str(KEY.into())),
            ("op".into(), CanonValue::Str("kv-get".into())),
        ]))
    };
    let call_args = tobkiri_canon::digest(&call, Variant::StrictV1)?;
    let principal = Principal::Pack {
        pack: pack.pin.id.clone(),
        hash: pack.pin.sha256.clone(),
    };
    Ok(vec![
        StoragePrompt {
            profile: profile.clone(),
            principal: principal.clone(),
            risk: open_effect.risk(),
            effect: open_effect,
            args_digest: open_args,
            security_epoch: 1,
        },
        StoragePrompt {
            profile: profile.clone(),
            principal,
            risk: Risk::Dangerous,
            effect: EffectRequest::UserGlobalStorage {
                store: GLOBAL.into(),
                operation,
            },
            args_digest: call_args,
            security_epoch: 1,
        },
    ])
}

fn paired_device_exists(data_path: &Path, profile: &ProfileId, device: &str) -> Result<bool> {
    let path = data_path.join(DataRoot::profile_relative(profile, "approvals.sqlite")?);
    let connection =
        rusqlite::Connection::open_with_flags(path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    Ok(connection.query_row(
        "SELECT EXISTS(SELECT 1 FROM paired_devices WHERE device=?1)",
        [device],
        |row| row.get(0),
    )?)
}

impl Replay<'_> {
    async fn check(
        &self,
        pack: &Pack,
        store: &str,
        operation: Operation,
        expected: i64,
    ) -> Result<()> {
        let (op, args, prompts) = match operation {
            Operation::Read => (
                READ,
                CanonValue::Obj(BTreeMap::from([(
                    "store".into(),
                    CanonValue::Str(store.into()),
                )])),
                Vec::new(),
            ),
            Operation::Write(value) => (
                WRITE,
                CanonValue::Obj(BTreeMap::from([
                    ("store".into(), CanonValue::Str(store.into())),
                    ("value".into(), CanonValue::Int(value.into())),
                ])),
                Vec::new(),
            ),
            Operation::FirstPublicWrite(value) => {
                let args = CanonValue::Obj(BTreeMap::from([
                    ("store".into(), CanonValue::Str(store.into())),
                    ("value".into(), CanonValue::Int(value.into())),
                ]));
                let payload = CanonValue::Obj(BTreeMap::from([
                    ("op".into(), CanonValue::Str("kv-put".into())),
                    ("key".into(), CanonValue::Str(KEY.into())),
                    (
                        "value".into(),
                        CanonValue::Arr(vec![CanonValue::Int(value.into())]),
                    ),
                ]));
                let prompt = StoragePrompt {
                    profile: self.profile.clone(),
                    principal: Principal::Pack {
                        pack: pack.pin.id.clone(),
                        hash: pack.pin.sha256.clone(),
                    },
                    risk: Risk::Normal,
                    effect: EffectRequest::Storage {
                        profile: self.profile.clone(),
                        owner: OWNER.parse()?,
                        store: store.into(),
                        tier: StoreTier::Public,
                        operation: StoreOperation::Write,
                    },
                    args_digest: tobkiri_canon::digest(&payload, Variant::StrictV1)?,
                    security_epoch: 1,
                };
                (WRITE, args, vec![prompt])
            }
            Operation::GlobalRead => (
                READ_GLOBAL,
                CanonValue::Obj(BTreeMap::new()),
                global_prompts(self.profile, pack, StoreOperation::Read, None)?,
            ),
            Operation::GlobalWrite(value) => (
                WRITE_GLOBAL,
                CanonValue::Obj(BTreeMap::from([(
                    "value".into(),
                    CanonValue::Int(value.into()),
                )])),
                global_prompts(self.profile, pack, StoreOperation::Write, Some(value))?,
            ),
        };
        let stage = format!("{}: {} {op} {store}", self.profile, pack.pin.id);
        let op = op.parse()?;
        self.host.expect_storage(prompts)?;
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
async fn wasm_publication_excludes_secret_approvals_and_profile_storage() -> Result<()> {
    use Operation::{FirstPublicWrite, GlobalRead, GlobalWrite, Read, Write};

    let directory = physical_temp()?;
    let data_path = directory.path().join("data");
    let owner = pack(directory.path(), OWNER)?;
    let writer = pack(directory.path(), WRITER)?;
    let second_writer = pack(directory.path(), SECOND_WRITER)?;
    let packs = [&owner, &writer, &second_writer];
    let a = profile("prof_storage_tiers_a", &packs)?;
    let b = profile("prof_storage_tiers_b", &packs)?;
    let host = Arc::new(StorageHost::default());
    for pack in packs {
        for op in [READ, WRITE, READ_GLOBAL, WRITE_GLOBAL] {
            host.admission
                .utility_ports
                .lock()
                .unwrap()
                .insert((pack.probe.to_string(), op.into()));
        }
    }
    approve_storage_user(&host.admission, &a)?;
    approve_storage_user(&host.admission, &b)?;
    let core = Core::open(
        CoreConfig::for_test_data_root(data_path.clone()),
        host.clone(),
    )?;
    core.save_profile(a.clone(), None)
        .await
        .context("save source storage Profile")?;
    core.save_profile(b.clone(), None)
        .await
        .context("save isolated storage Profile")?;
    let data_root = DataRoot::open(&data_path)?;
    const DEVICE: &str = "publication-fixture-device";
    {
        let approvals = Approvals::open(&data_root, &a.id)?;
        approvals.pair_device(&a.id, DEVICE, [0x5a; 32])?;
    }
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
            (&writer, GLOBAL, GlobalWrite(41), 0),
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
    host.verify_storage()?;

    let secret = b"synthetic-publication-secret-value";
    core.set_secret(&a.id, "publication-fixture-secret", secret)
        .await
        .context("write the fixture Secret through the OS vault")?;
    let source_vault = data_root.read_profile_file(&a.id, "secrets.vault", 4 * 1024 * 1024)?;
    ensure!(
        !source_vault
            .windows(secret.len())
            .any(|window| window == secret),
        "source vault contains secret plaintext"
    );
    ensure!(
        paired_device_exists(&data_path, &a.id, DEVICE)?,
        "source approval record was not installed"
    );

    let export = core.export_profile(&a.id)?;
    let text = std::str::from_utf8(&export)?;
    for marker in [
        std::str::from_utf8(secret)?,
        "publication-fixture-secret",
        DEVICE,
        a.id.as_str(),
        "Actual Wasm storage tiers",
        GLOBAL,
    ] {
        ensure!(
            !text.contains(marker),
            "published Profile contains excluded Profile state"
        );
    }
    let published: serde_json::Value = serde_json::from_slice(&export)?;
    let published_keys: BTreeSet<_> = published
        .as_object()
        .context("published Profile is not an object")?
        .keys()
        .map(String::as_str)
        .collect();
    let allowed_keys: BTreeSet<_> = [
        "format",
        "packs",
        "publishers",
        "ceiling",
        "user_permissions",
        "pack_limits",
        "bindings",
        "overlay_order",
        "user_overlays",
        "startup",
    ]
    .into_iter()
    .collect();
    ensure!(
        published_keys == allowed_keys,
        "published Profile contains fields outside its structural allowlist"
    );
    support::assert_no_plaintext(&data_path, secret)?;

    let user_permissions = a
        .user_permissions
        .clone()
        .context("storage fixture User declaration missing")?
        .intersect(&a.ceiling);
    let pack_approvals_before = host.admission.approvals.lock().unwrap().clone();
    host.expect_import_approval(&a.id, user_permissions)?;
    let imported = core
        .import_profile(&export, "Imported storage Profile".into(), None)
        .await
        .context("import the published storage Profile with fresh approvals")?;
    ensure!(
        imported != a.id,
        "import reused the source Profile identity"
    );
    host.verify_import_approval(&imported)?;
    let pack_approvals_after = host.admission.approvals.lock().unwrap().clone();
    let expected_fresh_pack_approvals: Vec<_> =
        packs.iter().map(|pack| pack.pin.id.to_string()).collect();
    ensure!(
        pack_approvals_after.len() == pack_approvals_before.len() + packs.len()
            && pack_approvals_after.get(pack_approvals_before.len()..)
                == Some(expected_fresh_pack_approvals.as_slice()),
        "import did not request the exact fresh Pack approvals"
    );

    ensure!(
        data_root
            .read_profile_file(&imported, "secrets.vault", 4 * 1024 * 1024)
            .is_err(),
        "imported Profile received source vault data"
    );
    ensure!(
        paired_device_exists(&data_path, &a.id, DEVICE)?,
        "publication changed the source approval record"
    );
    ensure!(
        !paired_device_exists(&data_path, &imported, DEVICE)?,
        "imported Profile received a source approval record"
    );

    let imported_session = core.start(&imported).await?;
    let imported_checked = async {
        let (imported_document, imported_head) = core.profile(&imported)?;
        let imported_approvals = Approvals::open(&data_root, &imported)?;
        let imported_epoch = imported_approvals.security_epoch(&imported)?;
        ensure!(
            imported_epoch == 1,
            "import did not start with fresh authority"
        );
        for pack in packs {
            ensure!(
                imported_approvals
                    .permissions(&imported, &pack.pin.id, &pack.pin.sha256)?
                    .is_some(),
                "imported Pack approval was not stored"
            );
        }
        let imported_user = imported_document
            .user_permissions
            .clone()
            .unwrap_or_default()
            .intersect(&imported_document.ceiling);
        ensure!(
            imported_approvals.user_permissions(
                &imported,
                &imported_head,
                imported_session.plan_digest(),
                imported_epoch,
            )? == Some(imported_user),
            "imported Profile lacks its fresh revision-bound User approval"
        );
        let imported_replay = Replay {
            host: &host,
            session: &imported_session,
            profile: &imported,
        };
        for (pack, store) in [
            (&owner, PRIVATE),
            (&writer, READ_ONLY),
            (&writer, PUBLIC),
            (&writer, OTHER_PUBLIC),
        ] {
            imported_replay.check(pack, store, Read, 0).await?;
        }
        // The account-wide user-global record remains shared by design;
        // Profile storage above and the source secret/approval are Profile-bound.
        imported_replay
            .check(&writer, GLOBAL, GlobalRead, 41)
            .await?;
        imported_replay
            .check(&writer, PUBLIC, FirstPublicWrite(31), 0)
            .await?;
        imported_replay.check(&writer, PUBLIC, Read, 31).await?;
        Ok::<(), anyhow::Error>(())
    }
    .await;
    let imported_close = imported_session.close().await;
    ensure!(
        imported_close.is_ok(),
        "imported checks: {imported_checked:?}; Session close: {imported_close:?}"
    );
    imported_checked?;

    let source_session = core.start(&a.id).await?;
    let source_checked = async {
        let (source_document, source_head) = core.profile(&a.id)?;
        let source_approvals = Approvals::open(&data_root, &a.id)?;
        let source_epoch = source_approvals.security_epoch(&a.id)?;
        ensure!(
            source_epoch == 1,
            "publication changed source approval epoch"
        );
        for pack in packs {
            ensure!(
                source_approvals
                    .permissions(&a.id, &pack.pin.id, &pack.pin.sha256)?
                    .is_some(),
                "source Pack approval was not preserved"
            );
        }
        let source_user = source_document
            .user_permissions
            .clone()
            .unwrap_or_default()
            .intersect(&source_document.ceiling);
        ensure!(
            source_approvals.user_permissions(
                &a.id,
                &source_head,
                source_session.plan_digest(),
                source_epoch,
            )? == Some(source_user),
            "publication changed the source revision-bound User approval"
        );
        let source_replay = Replay {
            host: &host,
            session: &source_session,
            profile: &a.id,
        };
        for (pack, store, value) in [
            (&owner, PRIVATE, 11),
            (&writer, READ_ONLY, 12),
            (&writer, PUBLIC, 18),
            (&writer, OTHER_PUBLIC, 16),
            (&second_writer, PUBLIC, 18),
        ] {
            source_replay.check(pack, store, Read, value).await?;
        }
        source_replay.check(&writer, GLOBAL, GlobalRead, 41).await?;
        source_replay.check(&writer, PUBLIC, Write(19), 0).await?;
        source_replay
            .check(&writer, OTHER_PUBLIC, Write(20), 0)
            .await?;
        source_replay
            .check(&second_writer, PUBLIC, Write(21), 0)
            .await?;
        for (pack, store, value) in [
            (&owner, PRIVATE, 11),
            (&writer, READ_ONLY, 12),
            (&writer, PUBLIC, 19),
            (&writer, OTHER_PUBLIC, 20),
            (&second_writer, PUBLIC, 21),
        ] {
            source_replay.check(pack, store, Read, value).await?;
        }
        source_replay.check(&writer, GLOBAL, GlobalRead, 41).await?;
        Ok::<(), anyhow::Error>(())
    }
    .await;
    let source_close = source_session.close().await;
    ensure!(
        source_close.is_ok(),
        "source checks: {source_checked:?}; Session close: {source_close:?}"
    );
    source_checked?;
    ensure!(
        data_root.read_profile_file(&a.id, "secrets.vault", 4 * 1024 * 1024)? == source_vault,
        "publication changed the source vault"
    );
    support::assert_no_plaintext(&data_path, secret)?;
    Ok(())
}
