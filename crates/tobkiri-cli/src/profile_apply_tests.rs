use super::*;
use serde_json::{Value, json};

fn document() -> Value {
    json!({
        "format": "tobkiri.profile/1", "id": "prof_existing", "name": "Existing name",
        "packs": [], "publishers": {}, "ceiling": {"loops": {
            "max_iterations": 1, "max_wall_ms": 1000, "max_cost_units": 10,
        }},
        "user_permissions": {"ports": ["local.fixture.port@1.0"]},
        "pack_limits": {}, "bindings": {}, "overlay_order": [], "user_overlays": [],
        "startup": {"inputs": [], "ui": null},
    })
}

fn write_document(path: &Path, value: &impl Serialize) {
    std::fs::write(path, serde_json::to_vec(value).unwrap()).unwrap();
}

#[tokio::test]
async fn foreign_identity_is_rejected_before_core_opens_or_creates_data() {
    let dir = tempfile::tempdir().unwrap();
    let file = dir.path().join("foreign.json");
    let mut foreign = document();
    foreign["id"] = json!("prof_other");
    write_document(&file, &foreign);
    let data = dir.path().join("must-not-exist");
    let error = crate::commands::run(crate::args::Args {
        non_interactive: true,
        data_root: Some(data.clone()),
        command: crate::args::Command::Profile {
            command: crate::args::ProfileCommand::Apply {
                profile: ProfileId::parse("prof_existing").unwrap(),
                definition: file,
                expected_head: Digest::hash(b"expected"),
            },
        },
    })
    .await
    .unwrap_err();
    assert!(error.to_string().contains("cannot change identity"));
    assert!(!data.exists());
}

#[test]
fn publication_keeps_current_identity_name_and_structural_policy() {
    let dir = tempfile::tempdir().unwrap();
    let file = dir.path().join("publication.json");
    let current = parse_profile(&serde_json::to_vec(&document()).unwrap()).unwrap();
    let published = current.published();
    write_document(&file, &published);
    let proposed = load(&current.id, &file)
        .unwrap()
        .materialize(&current)
        .unwrap();
    assert_eq!(proposed.id, current.id);
    assert_eq!(proposed.name, current.name);
    assert_eq!(
        serde_json::to_value(proposed.published()).unwrap(),
        serde_json::to_value(published).unwrap()
    );
    let mut injected = serde_json::to_value(current.published()).unwrap();
    injected["id"] = json!("prof_other");
    write_document(&file, &injected);
    assert!(load(&current.id, &file).is_err());
}

#[test]
fn same_identity_preserves_policy_and_freezes_relative_pack_sources() {
    let dir = tempfile::tempdir().unwrap();
    let origin = dir.path().join("profiles");
    std::fs::create_dir(&origin).unwrap();
    let pack = dir.path().join("fixture.tpk");
    std::fs::write(&pack, b"verification is still Core's responsibility").unwrap();
    let file = origin.join("profile.json");
    let mut value = document();
    value["packs"] = json!([{
        "id": "local.fixture", "version": "1.0.0",
        "sha256": Digest::hash(b"declared pin"), "source": "file:../fixture.tpk",
    }]);
    write_document(&file, &value);
    let current = parse_profile(&serde_json::to_vec(&value).unwrap()).unwrap();
    let proposed = load(&current.id, &file)
        .unwrap()
        .materialize(&current)
        .unwrap();
    assert_eq!(proposed.id, current.id);
    assert_eq!(proposed.packs[0].sha256, current.packs[0].sha256);
    assert_eq!(
        proposed.packs[0].source,
        format!("file:{}", std::fs::canonicalize(pack).unwrap().display())
    );
    assert_eq!(
        serde_json::to_value(&proposed.user_permissions).unwrap(),
        serde_json::to_value(&current.user_permissions).unwrap()
    );
    value["approved"] = json!(true);
    write_document(&file, &value);
    assert!(load(&current.id, &file).is_err());
}

#[test]
fn committed_receipt_does_not_mislabel_a_later_revision_epoch() {
    let id = ProfileId::parse("prof_existing").unwrap();
    let applied = Digest::hash(b"applied");
    let newer = Digest::hash(b"concurrent newer revision");
    let receipt = observe_committed(&id, applied.clone(), Ok((newer.clone(), 8))).unwrap();
    assert_eq!(receipt.applied_head, applied);
    assert_eq!(receipt.observed_head, newer);
    assert_eq!(receipt.observed_epoch, 8);
    assert!(!receipt.applied_revision_is_current);
    let error = observe_committed(
        &id,
        applied.clone(),
        Err(CoreError::Invalid("fixture snapshot failure".into())),
    )
    .unwrap_err();
    let message = format!("{error:#}");
    assert!(message.contains("update committed"));
    assert!(message.contains(applied.as_str()));
    assert!(message.contains("fixture snapshot failure"));
}

// These tests exercise actual secure DataRoot/SQLite/owner custody and the real
// Core permission/CAS path on Windows. Other platforms retain their own proof
// requirements; the portable parsing/receipt tests above need no native owner.
#[cfg(windows)]
mod native {
    use super::*;
    use async_trait::async_trait;
    use std::{
        collections::BTreeMap,
        sync::{
            Arc, Mutex,
            atomic::{AtomicBool, Ordering},
        },
    };
    use tobkiri_core::{
        ApprovalAnswer, ApprovalPrompt, CanonValue, CoreConfig, EffectRequest, HostError, HostIo,
        PermissionSet, TrustedPrompt, UiEmbed, ids::UiSessionId,
    };

    #[derive(Default)]
    struct Host {
        prompts: Mutex<Vec<EffectRequest>>,
        deny: AtomicBool,
    }
    #[async_trait]
    impl TrustedPrompt for Host {
        async fn prompt_approval(&self, prompt: ApprovalPrompt) -> ApprovalAnswer {
            self.prompts.lock().unwrap().push(prompt.effect);
            if self.deny.load(Ordering::SeqCst) {
                ApprovalAnswer::Deny
            } else {
                ApprovalAnswer::Approve
            }
        }
    }
    #[async_trait]
    impl HostIo for Host {
        fn display(&self, _: &str, _: &CanonValue) {}
        async fn terminal_read_line(&self, _: &str) -> Result<String, HostError> {
            Err(HostError("fixture has no physical input".into()))
        }
        fn terminal_write(&self, _: &str) {}
        fn embed_ui(&self, _: UiEmbed) -> Result<UiSessionId, HostError> {
            Err(HostError("fixture has no UI".into()))
        }
        async fn pick_directory(&self, _: &str) -> Option<PathBuf> {
            None
        }
    }

    struct Fixture {
        core: Core,
        host: Arc<Host>,
        profile: Profile,
        // Release native directory/database custody before removing its temp root.
        dir: tempfile::TempDir,
    }
    impl Fixture {
        fn new() -> Self {
            let dir = tempfile::tempdir().unwrap();
            let physical = std::fs::canonicalize(dir.path()).unwrap();
            let host = Arc::new(Host::default());
            let core = Core::open(
                CoreConfig::for_test_data_root(physical.join("data")),
                host.clone(),
            )
            .unwrap();
            let mut permissions = PermissionSet::default();
            permissions
                .ports
                .insert("local.apply-fixture.port@1.0".into());
            permissions.wasm.fuel_per_call = 100_000;
            permissions.wasm.memory_mib = 8;
            permissions.wasm.wall_ms = 30_000;
            permissions.wasm.table_elements = 1024;
            permissions.wasm.output_bytes = 1_048_576;
            permissions.loops.max_iterations = 8;
            permissions.loops.max_wall_ms = 30_000;
            permissions.loops.max_cost_units = 100;
            let pin = artifact(&physical, &permissions, "1.0.0");
            let value = json!({
                "format":"tobkiri.profile/1", "id":"prof_apply", "name":"Apply fixture",
                "packs":[pin], "publishers":{}, "ceiling":permissions,
                "user_permissions":permissions, "pack_limits":{"local.apply-fixture":permissions},
                "bindings":{}, "overlay_order":[], "user_overlays":[],
                "startup":{"inputs":[],"ui":null},
            });
            let profile = parse_profile(&serde_json::to_vec(&value).unwrap()).unwrap();
            Self {
                core,
                host,
                profile,
                dir,
            }
        }
        fn definition(&self, profile: &Profile) -> Definition {
            let file = self.dir.path().join("edit.json");
            write_document(&file, profile);
            load(&self.profile.id, &file).unwrap()
        }
    }

    fn artifact(directory: &Path, permissions: &PermissionSet, version: &str) -> Value {
        let files = BTreeMap::from([
            (
                "wit/port.wit".to_string(),
                b"package local:apply-fixture@1.0.0; interface port { read: func() -> string; }"
                    .to_vec(),
            ),
            (
                "wit/port.shape.json".to_string(),
                br#"{"cardinality":"one","streams":{},"callbacks":{}}"#.to_vec(),
            ),
        ]);
        let value = json!({
            "format":"tobkiri.pack/1", "id":"local.apply-fixture", "version":version,
            "publisher_key":null, "runtime":{"kind":"data"},
            "shapes":[{"name":"local.apply-fixture.port","version":"1.0","kind":"port","wit":"wit/port.wit"}],
            "ports":{"provides":[{"shape":"local.apply-fixture.port","version":"1.0","optional":false}],"consumes":[],"untyped":[]},
            "flows":[],"overlays":[],"stores":[],"permissions":permissions,"ui":null,
            "files":files.iter().map(|(path,bytes)| json!({"path":path,"sha256":Digest::hash(bytes),"size":bytes.len()})).collect::<Vec<_>>(),
        });
        let manifest =
            tobkiri_core::pack::parse_manifest(&serde_json::to_vec(&value).unwrap()).unwrap();
        let bytes = tobkiri_core::pack::build_tpk(&manifest, &files, None).unwrap();
        let verified = tobkiri_core::pack::verify_tpk(&bytes, &BTreeMap::new()).unwrap();
        let file = directory.join(format!("fixture-{version}.tpk"));
        std::fs::write(&file, bytes).unwrap();
        json!({"id":"local.apply-fixture","version":version,"sha256":verified.hash,"source":format!("file:{}",file.display())})
    }

    #[tokio::test]
    async fn same_profile_apply_commits_new_head_epoch_and_keeps_pack_policy() {
        let f = Fixture::new();
        let head = f.core.save_profile(f.profile.clone(), None).await.unwrap();
        assert_eq!(
            f.core.profile_head_snapshot(&f.profile.id).unwrap(),
            (head.clone(), 1)
        );
        let mut edit = f.profile.clone();
        edit.name = "Edited same Profile".into();
        let receipt = apply(&f.core, &f.profile.id, &head, f.definition(&edit))
            .await
            .unwrap();
        assert_eq!(receipt.profile, f.profile.id);
        assert_ne!(receipt.applied_head, head);
        assert_eq!(receipt.observed_head, receipt.applied_head);
        assert_eq!(receipt.observed_epoch, 2);
        assert!(receipt.applied_revision_is_current);
        let (stored, stored_head) = f.core.profile(&f.profile.id).unwrap();
        assert_eq!(stored.id, f.profile.id);
        assert_eq!(stored_head, receipt.applied_head);
        assert_eq!(stored.name, edit.name);
        assert_eq!(
            serde_json::to_value(stored.pack_limits).unwrap(),
            serde_json::to_value(f.profile.pack_limits).unwrap()
        );
        let prompts = f.host.prompts.lock().unwrap();
        assert_eq!(
            prompts
                .iter()
                .filter(|p| matches!(p, EffectRequest::Permissions { .. }))
                .count(),
            1
        );
        assert_eq!(
            prompts
                .iter()
                .filter(|p| matches!(p, EffectRequest::UserPermissions { .. }))
                .count(),
            2
        );
    }

    #[tokio::test]
    async fn published_definition_updates_existing_identity_and_current_name() {
        let f = Fixture::new();
        let head = f.core.save_profile(f.profile.clone(), None).await.unwrap();
        let mut published = f.profile.published();
        published.user_permissions = None;
        let file = f.dir.path().join("published-edit.json");
        write_document(&file, &published);
        let receipt = apply(
            &f.core,
            &f.profile.id,
            &head,
            load(&f.profile.id, &file).unwrap(),
        )
        .await
        .unwrap();
        let (stored, stored_head) = f.core.profile(&f.profile.id).unwrap();
        assert_eq!(stored.id, f.profile.id);
        assert_eq!(stored.name, f.profile.name);
        assert!(stored.user_permissions.is_none());
        assert_ne!(stored_head, head);
        assert_eq!(receipt.applied_head, stored_head);
        assert_eq!(receipt.observed_epoch, 2);
        assert_eq!(
            serde_json::to_value(stored.packs).unwrap(),
            serde_json::to_value(f.profile.packs).unwrap()
        );
    }

    #[tokio::test]
    async fn stale_expected_head_rejects_before_sources_or_new_approvals() {
        let f = Fixture::new();
        let initial = f.core.save_profile(f.profile.clone(), None).await.unwrap();
        let mut edit = f.profile.clone();
        edit.name = "First accepted edit".into();
        let first = apply(&f.core, &f.profile.id, &initial, f.definition(&edit))
            .await
            .unwrap();
        let before = f.core.profile_head_snapshot(&f.profile.id).unwrap();
        let prompts = f.host.prompts.lock().unwrap().len();
        edit.packs[0].source = "file:missing-artifact-must-not-be-resolved.tpk".into();
        let error = apply(&f.core, &f.profile.id, &initial, f.definition(&edit))
            .await
            .unwrap_err();
        assert!(error.to_string().contains("head changed"));
        assert_eq!(before, (first.applied_head, 2));
        assert_eq!(f.core.profile_head_snapshot(&f.profile.id).unwrap(), before);
        assert_eq!(f.host.prompts.lock().unwrap().len(), prompts);
    }

    #[tokio::test]
    async fn declining_new_pack_permissions_leaves_same_head_epoch_and_policy() {
        let f = Fixture::new();
        let head = f.core.save_profile(f.profile.clone(), None).await.unwrap();
        let before = f.core.profile_head_snapshot(&f.profile.id).unwrap();
        let mut edit = f.profile.clone();
        edit.ceiling.audit_read = true;
        edit.pack_limits
            .get_mut(&tobkiri_core::ids::PackId::parse("local.apply-fixture").unwrap())
            .unwrap()
            .audit_read = true;
        let pin = artifact(f.dir.path(), &edit.ceiling, "1.0.1");
        edit.packs[0] = serde_json::from_value(pin).unwrap();
        f.host.deny.store(true, Ordering::SeqCst);
        assert!(
            apply(&f.core, &f.profile.id, &head, f.definition(&edit))
                .await
                .is_err()
        );
        assert!(matches!(
            f.host.prompts.lock().unwrap().last(),
            Some(EffectRequest::Permissions { .. })
        ));
        assert_eq!(f.core.profile_head_snapshot(&f.profile.id).unwrap(), before);
        assert_eq!(
            serde_json::to_value(f.core.profile(&f.profile.id).unwrap().0.pack_limits).unwrap(),
            serde_json::to_value(f.profile.pack_limits).unwrap()
        );
    }

    #[tokio::test]
    async fn declining_new_user_permissions_does_not_publish_a_revision() {
        let mut f = Fixture::new();
        f.profile.user_permissions = None;
        let head = f.core.save_profile(f.profile.clone(), None).await.unwrap();
        let before = f.core.profile_head_snapshot(&f.profile.id).unwrap();
        let mut edit = f.profile.clone();
        edit.user_permissions = Some(edit.ceiling.clone());
        f.host.deny.store(true, Ordering::SeqCst);
        assert!(
            apply(&f.core, &f.profile.id, &head, f.definition(&edit))
                .await
                .is_err()
        );
        assert!(matches!(
            f.host.prompts.lock().unwrap().last(),
            Some(EffectRequest::UserPermissions { .. })
        ));
        assert_eq!(f.core.profile_head_snapshot(&f.profile.id).unwrap(), before);
        assert!(
            f.core
                .profile(&f.profile.id)
                .unwrap()
                .0
                .user_permissions
                .is_none()
        );
    }

    #[tokio::test]
    async fn missing_profile_and_invalid_pack_pin_cannot_create_authority() {
        let f = Fixture::new();
        let head = f.core.save_profile(f.profile.clone(), None).await.unwrap();
        let before = f.core.profile_head_snapshot(&f.profile.id).unwrap();
        let prompts = f.host.prompts.lock().unwrap().len();
        let mut invalid = f.profile.clone();
        invalid.packs[0].sha256 =
            tobkiri_core::ids::PackHash::new(Digest::hash(b"wrong artifact pin"));
        assert!(
            apply(&f.core, &f.profile.id, &head, f.definition(&invalid))
                .await
                .is_err()
        );
        assert_eq!(f.core.profile_head_snapshot(&f.profile.id).unwrap(), before);
        assert_eq!(f.host.prompts.lock().unwrap().len(), prompts);
        let missing = ProfileId::parse("prof_missing").unwrap();
        invalid.id = missing.clone();
        let file = f.dir.path().join("missing.json");
        write_document(&file, &invalid);
        assert!(
            apply(&f.core, &missing, &head, load(&missing, &file).unwrap())
                .await
                .is_err()
        );
        assert!(!f.dir.path().join("data/profiles/prof_missing").exists());
        assert_eq!(f.host.prompts.lock().unwrap().len(), prompts);
    }
}
