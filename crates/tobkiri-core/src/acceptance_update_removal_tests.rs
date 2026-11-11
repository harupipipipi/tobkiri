//! Issue #1520 acceptance items 13, 15 and 16 through the actual Core entry
//! points: Pack update/removal transactions, dangerous permissions, and the
//! published Profile boundary. Every Pack here is a real hash-verified `.tpk`.
use super::*;
use std::path::Path;
use tobkiri_canon::ids::PackHash;
use tobkiri_gate::Risk;
use tobkiri_manifest::{
    FileEntry, PackManifest, PortDeclaration, Ports, RuntimeSpec, ShapeDefinition, ShapeKind,
};

const PORT: &str = "local.fixture.port@1.0";

#[derive(Clone, Copy)]
enum Role {
    Provider,
    Consumer,
    Native,
}
struct Spec<'a> {
    id: &'a str,
    version: &'a str,
    salt: &'a str,
    permissions: PermissionSet,
    role: Role,
}

/// Build and independently verify a Pack artifact on disk.
fn build(dir: &Path, spec: &Spec<'_>) -> (String, PackHash) {
    let shape = ShapeRef::parse(PORT).unwrap();
    let mut files: BTreeMap<String, Vec<u8>> =
        BTreeMap::from([("data/salt.txt".to_string(), spec.salt.as_bytes().to_vec())]);
    let mut manifest = PackManifest {
        format: "tobkiri.pack/1".into(),
        id: PackId::parse(spec.id).unwrap(),
        version: spec.version.into(),
        publisher_key: None,
        runtime: RuntimeSpec::Data,
        shapes: Vec::new(),
        ports: Ports::default(),
        inputs: Vec::new(),
        flows: Vec::new(),
        overlays: Vec::new(),
        stores: Vec::new(),
        permissions: spec.permissions.clone(),
        ui: None,
        files: Vec::new(),
    };
    let declaration = PortDeclaration {
        shape: shape.name.clone(),
        version: shape.version,
        optional: false,
    };
    match spec.role {
        Role::Provider => {
            files.insert(
                "wit/port.wit".into(),
                b"package local:fixture@1.0.0; interface port { read: func() -> string; }".to_vec(),
            );
            files.insert(
                "wit/port.shape.json".into(),
                br#"{"cardinality":"one","streams":{},"callbacks":{}}"#.to_vec(),
            );
            manifest.shapes = vec![ShapeDefinition {
                name: shape.name.clone(),
                version: shape.version,
                kind: ShapeKind::Port,
                wit: "wit/port.wit".into(),
            }];
            manifest.ports.provides = vec![declaration];
        }
        Role::Consumer => manifest.ports.consumes = vec![declaration],
        Role::Native => {
            files.insert("guest".into(), b"not-executed-in-this-test".to_vec());
            manifest.runtime = RuntimeSpec::Native {
                executable: "guest".into(),
                args: Vec::new(),
                config: None,
            };
            manifest.permissions.native = true;
        }
    }
    manifest.files = files
        .iter()
        .map(|(path, bytes)| FileEntry {
            path: path.clone(),
            sha256: Digest::hash(bytes),
            size: bytes.len() as u64,
        })
        .collect();
    let artifact = tobkiri_manifest::build_tpk(&manifest, &files, None).unwrap();
    let verified = tobkiri_manifest::verify_tpk(&artifact, &BTreeMap::new()).unwrap();
    let path = dir.join(format!("{}-{}-{}.tpk", spec.id, spec.version, spec.salt));
    std::fs::write(&path, artifact).unwrap();
    (path.to_str().unwrap().to_string(), verified.hash)
}
fn physical(dir: &tempfile::TempDir) -> PathBuf {
    std::fs::canonicalize(dir.path()).unwrap()
}
fn pin(id: &str, version: &str, source: &str, hash: &PackHash) -> PackPin {
    PackPin {
        id: PackId::parse(id).unwrap(),
        version: version.into(),
        sha256: hash.clone(),
        source: source.into(),
    }
}
fn pack_prompts(host: &Host) -> Vec<ApprovalPrompt> {
    host.prompts
        .lock()
        .unwrap()
        .iter()
        .filter(|prompt| matches!(prompt.effect, EffectRequest::Permissions { .. }))
        .cloned()
        .collect()
}
/// The prompt summary is exactly the permission delta shown to the user.
fn delta(prompt: &ApprovalPrompt) -> PermissionSet {
    serde_json::from_str(&prompt.summary).unwrap()
}
fn only_origins(origins: &[&str]) -> PermissionSet {
    let mut expected = PermissionSet::default();
    expected
        .network
        .origins
        .extend(origins.iter().map(|origin| origin.to_string()));
    expected
}
fn stored_pin(core: &Core, id: &ProfileId, pack: &str) -> (String, PackHash) {
    let (profile, _) = core.profile(id).unwrap();
    let pin = profile
        .packs
        .iter()
        .find(|pin| pin.id.as_str() == pack)
        .unwrap();
    (pin.version.clone(), pin.sha256.clone())
}

#[tokio::test]
async fn pack_update_asks_only_for_permissions_added_since_the_previous_version() {
    let (core, host, mut profile, dir) = fixture();
    let dir = physical(&dir);
    let pack_id = PackId::parse("local.fixture").unwrap();
    let one = "https://one.example";
    let two = "https://two.example";
    // The Profile already allows both origins. The first Pack version does not
    // ask for either, so each later declaration is a genuine permission delta.
    let base = profile.ceiling.clone();
    let mut wide = base.clone();
    wide.network
        .origins
        .extend([one.to_string(), two.to_string()]);
    profile.ceiling = wide.clone();
    profile.pack_limits.insert(pack_id.clone(), wide);
    let head_one = core.save_profile(profile.clone(), None).await.unwrap();
    let (_, original_hash) = stored_pin(&core, &profile.id, "local.fixture");
    let first_prompts = pack_prompts(&host);
    assert_eq!(first_prompts.len(), 1);
    assert!(first_prompts[0].summary.contains("local.fixture.port@1.0"));

    // Same declaration, same version, different bytes: still an update, but no
    // new permission and therefore no new Pack approval.
    let (same_perms_path, same_perms_hash) = build(
        &dir,
        &Spec {
            id: "local.fixture",
            version: "1.0.0",
            salt: "rebuilt",
            permissions: base.clone(),
            role: Role::Provider,
        },
    );
    assert_ne!(same_perms_hash, original_hash);
    assert!(
        core.add_pack(&profile.id, same_perms_path.clone(), false)
            .await
            .is_err(),
        "an installed Pack must be changed with update, not add"
    );
    let head_two = core
        .add_pack(&profile.id, same_perms_path, true)
        .await
        .unwrap();
    assert_ne!(head_two, head_one);
    assert_eq!(
        stored_pin(&core, &profile.id, "local.fixture").1,
        same_perms_hash
    );
    assert_eq!(pack_prompts(&host).len(), 1);

    // A new version adding one origin asks about that origin and nothing else.
    let mut with_one = base.clone();
    with_one.network.origins.insert(one.to_string());
    let (v3_path, v3_hash) = build(
        &dir,
        &Spec {
            id: "local.fixture",
            version: "1.1.0",
            salt: "adds-one",
            permissions: with_one.clone(),
            role: Role::Provider,
        },
    );
    let head_three = core.add_pack(&profile.id, v3_path, true).await.unwrap();
    let prompts = pack_prompts(&host);
    assert_eq!(prompts.len(), 2);
    assert!(matches!(
        &prompts[1].effect,
        EffectRequest::Permissions { pack, hash } if pack == &pack_id && hash == &v3_hash
    ));
    assert_eq!(delta(&prompts[1]), only_origins(&[one]));
    assert_eq!(
        stored_pin(&core, &profile.id, "local.fixture"),
        ("1.1.0".to_string(), v3_hash.clone())
    );

    // A refused addition leaves the head, pin and approvals exactly as before.
    let mut with_both = with_one;
    with_both.network.origins.insert(two.to_string());
    let (v4_path, v4_hash) = build(
        &dir,
        &Spec {
            id: "local.fixture",
            version: "1.2.0",
            salt: "adds-two",
            permissions: with_both,
            role: Role::Provider,
        },
    );
    let before = crate::profiles::strict_bytes(&core.profile(&profile.id).unwrap().0).unwrap();
    let state = core.profile_save_precondition(&profile.id).unwrap();
    host.deny.store(true, Ordering::SeqCst);
    assert!(
        core.add_pack(&profile.id, v4_path.clone(), true)
            .await
            .is_err()
    );
    host.deny.store(false, Ordering::SeqCst);
    let (stored, current_head) = core.profile(&profile.id).unwrap();
    assert_eq!(current_head, head_three);
    assert_eq!(crate::profiles::strict_bytes(&stored).unwrap(), before);
    assert_eq!(core.profile_save_precondition(&profile.id).unwrap(), state);
    let approvals = Approvals::open(&core.profiles.root, &profile.id).unwrap();
    assert!(
        approvals
            .permissions(&profile.id, &pack_id, &v4_hash)
            .unwrap()
            .is_none()
    );

    // After approval the question is only about the delta from the previous
    // version (two), not from the first version (one and two).
    core.add_pack(&profile.id, v4_path, true).await.unwrap();
    let prompts = pack_prompts(&host);
    assert_eq!(prompts.len(), 4);
    assert_eq!(delta(&prompts[3]), only_origins(&[two]));
    assert_eq!(stored_pin(&core, &profile.id, "local.fixture").1, v4_hash);
    // Another Profile has no approval for the same artifact (R6.3).
    let other = ProfileId::parse("prof_other_approval").unwrap();
    let other_approvals = Approvals::open(&core.profiles.root, &other).unwrap();
    assert!(
        other_approvals
            .permissions(&other, &pack_id, &v4_hash)
            .unwrap()
            .is_none()
    );
}

#[tokio::test]
async fn removing_a_provider_of_another_packs_required_port_fails_and_keeps_data() {
    let (core, host, mut profile, dir) = fixture();
    let dir = physical(&dir);
    let provider = PackId::parse("local.fixture").unwrap();
    let consumer = PackId::parse("local.consumer").unwrap();
    let permissions = profile.ceiling.clone();
    let (path, hash) = build(
        &dir,
        &Spec {
            id: "local.consumer",
            version: "1.0.0",
            salt: "consumer",
            permissions: permissions.clone(),
            role: Role::Consumer,
        },
    );
    profile
        .packs
        .push(pin("local.consumer", "1.0.0", &path, &hash));
    profile.pack_limits.insert(consumer.clone(), permissions);
    let head = core.save_profile(profile.clone(), None).await.unwrap();
    // The Pack's private data stays in the Profile after a Pack is removed.
    {
        let stores = tobkiri_store::TieredStore::open(&core.profiles.root, &profile.id).unwrap();
        stores
            .register(
                &provider,
                "notes",
                tobkiri_gate::StoreTier::Private,
                "local.fixture.notes@1.0",
            )
            .unwrap();
    }

    let before = crate::profiles::strict_bytes(&core.profile(&profile.id).unwrap().0).unwrap();
    let state = core.profile_save_precondition(&profile.id).unwrap();
    let prompts = host.prompts.lock().unwrap().len();
    match core.remove_pack(&profile.id, &provider).await {
        Err(CoreError::Conflicts(conflicts)) => {
            assert!(
                conflicts
                    .iter()
                    .any(|conflict| conflict.code == "required-port")
            );
        }
        other => panic!("removal must fail on the required port: {other:?}"),
    }
    let (stored, current_head) = core.profile(&profile.id).unwrap();
    assert_eq!(current_head, head);
    assert_eq!(crate::profiles::strict_bytes(&stored).unwrap(), before);
    assert_eq!(core.profile_save_precondition(&profile.id).unwrap(), state);
    assert_eq!(host.prompts.lock().unwrap().len(), prompts);
    assert!(
        core.remove_pack(&profile.id, &PackId::parse("local.absent").unwrap())
            .await
            .is_err()
    );

    // Removing the consumer is allowed, after which the provider is no longer
    // required by anyone and can go too. Its store metadata remains.
    let after_consumer = core.remove_pack(&profile.id, &consumer).await.unwrap();
    assert_ne!(after_consumer, head);
    let (profile_now, _) = core.profile(&profile.id).unwrap();
    assert_eq!(profile_now.packs.len(), 1);
    assert!(!profile_now.pack_limits.contains_key(&consumer));
    let after_provider = core.remove_pack(&profile.id, &provider).await.unwrap();
    assert_ne!(after_provider, after_consumer);
    assert!(core.profile(&profile.id).unwrap().0.packs.is_empty());
    let stores = tobkiri_store::TieredStore::open(&core.profiles.root, &profile.id).unwrap();
    let descriptor = stores.describe(&provider, "notes").unwrap();
    assert_eq!(descriptor.tier, tobkiri_gate::StoreTier::Private);
}

#[tokio::test]
async fn native_pack_requires_the_profile_to_allow_native_and_is_prompted_as_dangerous() {
    let (core, host, mut profile, dir) = fixture();
    let dir = physical(&dir);
    let permissions = profile.ceiling.clone();
    let (path, hash) = build(
        &dir,
        &Spec {
            id: "local.native",
            version: "1.0.0",
            salt: "native",
            permissions: permissions.clone(),
            role: Role::Native,
        },
    );
    let native = PackId::parse("local.native").unwrap();
    profile
        .packs
        .push(pin("local.native", "1.0.0", &path, &hash));
    let mut native_limits = permissions;
    profile
        .pack_limits
        .insert(native.clone(), native_limits.clone());
    assert!(!profile.ceiling.native);

    // Without an explicit ceiling the whole save fails before any approval.
    match core.save_profile(profile.clone(), None).await {
        Err(CoreError::Conflicts(conflicts)) => {
            assert!(
                conflicts
                    .iter()
                    .any(|conflict| conflict.code == "dangerous-native")
            );
        }
        other => panic!("native Pack must be rejected: {other:?}"),
    }
    assert!(core.profiles.head(&profile.id).unwrap().is_none());
    assert!(host.prompts.lock().unwrap().is_empty());

    // The Profile explicitly allows native execution: now the Pack is admitted
    // only through a Dangerous, native-specific approval.
    profile.ceiling.native = true;
    native_limits.native = true;
    profile.pack_limits.insert(native.clone(), native_limits);
    core.save_profile(profile.clone(), None).await.unwrap();
    let prompts = pack_prompts(&host);
    let native_prompt = prompts
        .iter()
        .find(|prompt| matches!(&prompt.effect, EffectRequest::Permissions { pack, .. } if pack == &native))
        .unwrap();
    assert_eq!(native_prompt.risk, Risk::Dangerous);
    assert!(delta(native_prompt).native);
    for prompt in prompts
        .iter()
        .filter(|prompt| !matches!(&prompt.effect, EffectRequest::Permissions { pack, .. } if pack == &native))
    {
        assert_ne!(prompt.risk, Risk::Dangerous);
    }
}

#[tokio::test]
async fn cross_profile_storage_is_only_asked_for_when_the_profile_ceiling_allows_it() {
    let (core, host, mut profile, dir) = fixture();
    let dir = physical(&dir);
    let pack_id = PackId::parse("local.fixture").unwrap();
    let base = profile.ceiling.clone();
    let mut wants_cross = base.clone();
    wants_cross.cross_profile_storage = true;
    let (path, hash) = build(
        &dir,
        &Spec {
            id: "local.fixture",
            version: "1.0.0",
            salt: "cross",
            permissions: wants_cross.clone(),
            role: Role::Provider,
        },
    );
    profile.packs = vec![pin("local.fixture", "1.0.0", &path, &hash)];

    // The Pack declares the dangerous permission but the Profile does not
    // allow it: the declaration is inert and never even reaches a prompt.
    profile.pack_limits.insert(pack_id.clone(), base.clone());
    core.save_profile(profile.clone(), None).await.unwrap();
    let prompts = pack_prompts(&host);
    assert_eq!(prompts.len(), 1);
    assert!(!delta(&prompts[0]).cross_profile_storage);
    assert_ne!(prompts[0].risk, Risk::Dangerous);

    // A different Profile whose ceiling explicitly allows it is asked again,
    // with the dangerous warning and the permission named in the delta.
    let mut allowing = profile.clone();
    allowing.id = ProfileId::parse("prof_cross_allowed").unwrap();
    allowing.ceiling.cross_profile_storage = true;
    allowing.pack_limits.insert(pack_id, wants_cross);
    core.save_profile(allowing, None).await.unwrap();
    let prompts = pack_prompts(&host);
    assert_eq!(prompts.len(), 2);
    assert!(delta(&prompts[1]).cross_profile_storage);
    assert_eq!(prompts[1].risk, Risk::Dangerous);
}

struct FixedKeys;
impl tobkiri_store::VaultKeyProvider for FixedKeys {
    fn key(&self, _: &ProfileId) -> Result<[u8; 32], tobkiri_store::StoreError> {
        Ok([7; 32])
    }
}

#[tokio::test]
async fn published_profile_excludes_populated_approvals_data_secrets_and_user_global_storage() {
    let (core, host, profile, _dir) = fixture();
    core.save_profile(profile.clone(), None).await.unwrap();
    let secret = b"r8-secret-value-4f9a1c".to_vec();
    let vault =
        tobkiri_store::Vault::open(&core.profiles.root, &profile.id, Arc::new(FixedKeys)).unwrap();
    vault.set("api-key", &secret).unwrap();
    // A paired device is an approval record that only the approval store holds.
    let approvals = Approvals::open(&core.profiles.root, &profile.id).unwrap();
    approvals
        .pair_device(&profile.id, "r8-paired-device-marker", [9; 32])
        .unwrap();
    drop(approvals);
    // One file per tier plus the account-wide user-global storage.
    let data = [
        (
            "data/local.fixture/private.db",
            &b"r8-private-tier-marker"[..],
        ),
        ("data/public/notes.db", &b"r8-public-tier-marker"[..]),
        ("data/read-only/catalog.db", &b"r8-readonly-tier-marker"[..]),
    ];
    for (path, bytes) in data {
        core.profiles
            .root
            .write_profile_file_atomic(&profile.id, path, bytes)
            .unwrap();
    }
    core.profiles
        .root
        .write_atomic("user-global/shared.sqlite", b"r8-user-global-marker")
        .unwrap();

    let export = core.export_profile(&profile.id).unwrap();
    let text = String::from_utf8(export.clone()).unwrap();
    for marker in [
        "r8-secret-value-4f9a1c",
        "r8-paired-device-marker",
        "r8-private-tier-marker",
        "r8-public-tier-marker",
        "r8-readonly-tier-marker",
        "r8-user-global-marker",
        "api-key",
        "prof_core",
        "Core fixture",
    ] {
        assert!(!text.contains(marker), "published Profile leaked {marker}");
    }
    let value: serde_json::Value = serde_json::from_slice(&export).unwrap();
    let keys: std::collections::BTreeSet<_> = value.as_object().unwrap().keys().cloned().collect();
    let allowed: std::collections::BTreeSet<_> = [
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
    .map(String::from)
    .collect();
    assert_eq!(keys, allowed);
    // Publication does not consume or alter the source data.
    for (path, bytes) in data {
        assert_eq!(
            core.profiles
                .root
                .read_profile_file(&profile.id, path, 1024)
                .unwrap(),
            bytes
        );
    }

    // Authority and data fields are refused instead of silently imported.
    let prompts_before = pack_prompts(&host).len();
    for field in [
        "approvals",
        "vault",
        "secrets",
        "data",
        "user_global",
        "head",
    ] {
        let mut forged = value.clone();
        forged[field] = serde_json::json!("forbidden");
        assert!(
            core.import_profile(&serde_json::to_vec(&forged).unwrap(), "Forged".into(), None)
                .await
                .is_err(),
            "published Profile accepted {field}"
        );
    }
    assert_eq!(pack_prompts(&host).len(), prompts_before);

    // A genuine import gets a fresh identity, fresh approval, and none of the
    // source Profile's secrets or data.
    let imported = core
        .import_profile(&export, "Imported".into(), None)
        .await
        .unwrap();
    assert_ne!(imported, profile.id);
    assert_eq!(pack_prompts(&host).len(), prompts_before + 1);
    for (path, _) in data {
        assert!(
            core.profiles
                .root
                .read_profile_file(&imported, path, 1024)
                .is_err()
        );
    }
    let source_vault = core
        .profiles
        .root
        .read_profile_file(&profile.id, "secrets.vault", 4 * 1024 * 1024)
        .unwrap();
    if let Ok(copied) =
        core.profiles
            .root
            .read_profile_file(&imported, "secrets.vault", 4 * 1024 * 1024)
    {
        assert_ne!(copied, source_vault);
    }
    let imported_approvals = Approvals::open(&core.profiles.root, &imported).unwrap();
    assert!(
        imported_approvals
            .permissions(&imported, &profile.packs[0].id, &profile.packs[0].sha256)
            .unwrap()
            .is_some(),
        "the importing Profile must record its own fresh approval"
    );
}
