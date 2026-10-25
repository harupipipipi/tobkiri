use super::*;
#[path = "permission_review_tests.rs"]
mod permission_review;
#[path = "profile_save_epoch_tests.rs"]
mod profile_save_epoch;
use async_trait::async_trait;
use std::{
    collections::BTreeSet,
    sync::{
        Mutex,
        atomic::{AtomicBool, Ordering},
    },
};
use tobkiri_canon::{
    CanonValue,
    ids::{OpName, ShapeRef, UiSessionId},
};
use tobkiri_gate::{ApprovalStore, EffectRequest, HostError, LoopLimits, ResourceLimits, UiEmbed};
use tobkiri_manifest::{
    FileEntry, PackManifest, PortDeclaration, Ports, RuntimeSpec, ShapeDefinition, ShapeKind,
};
use tobkiri_profile::{PackPin, Startup};

#[derive(Default)]
struct Host {
    prompts: Mutex<Vec<ApprovalPrompt>>,
    deny: AtomicBool,
    deny_user: AtomicBool,
}
#[async_trait]
impl TrustedPrompt for Host {
    async fn prompt_approval(&self, prompt: ApprovalPrompt) -> ApprovalAnswer {
        let denied = self.deny.load(Ordering::SeqCst)
            || (self.deny_user.load(Ordering::SeqCst)
                && matches!(&prompt.effect, EffectRequest::UserPermissions { .. }));
        self.prompts.lock().unwrap().push(prompt);
        if denied {
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
fn fixture() -> (Core, Arc<Host>, Profile, tempfile::TempDir) {
    let dir = tempfile::tempdir().unwrap();
    let physical = std::fs::canonicalize(dir.path()).unwrap();
    let host = Arc::new(Host::default());
    let core = Core::open(
        CoreConfig::for_test_data_root(physical.join("data")),
        host.clone(),
    )
    .unwrap();
    let shape = ShapeRef::parse("local.fixture.port@1.0").unwrap();
    let permissions = PermissionSet {
        ports: BTreeSet::from([shape.to_string()]),
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
    let files: BTreeMap<String, Vec<u8>> = BTreeMap::from([
        (
            "wit/port.wit".into(),
            b"package local:fixture@1.0.0; interface port { read: func() -> string; }".to_vec(),
        ),
        (
            "wit/port.shape.json".into(),
            br#"{"cardinality":"one","streams":{},"callbacks":{}}"#.to_vec(),
        ),
    ]);
    let pack_id = PackId::parse("local.fixture").unwrap();
    let manifest = PackManifest {
        format: "tobkiri.pack/1".into(),
        id: pack_id.clone(),
        version: "1.0.0".into(),
        publisher_key: None,
        runtime: RuntimeSpec::Data,
        shapes: vec![ShapeDefinition {
            name: shape.name.clone(),
            version: shape.version,
            kind: ShapeKind::Port,
            wit: "wit/port.wit".into(),
        }],
        ports: Ports {
            provides: vec![PortDeclaration {
                shape: shape.name,
                version: shape.version,
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
    let path = physical.join("fixture.tpk");
    std::fs::write(&path, artifact).unwrap();
    let profile = Profile {
        format: "tobkiri.profile/1".into(),
        id: ProfileId::parse("prof_core").unwrap(),
        name: "Core fixture".into(),
        packs: vec![PackPin {
            id: pack_id.clone(),
            version: manifest.version,
            sha256: verified.hash.clone(),
            source: path.to_str().unwrap().into(),
        }],
        publishers: BTreeMap::new(),
        ceiling: permissions.clone(),
        user_permissions: Some(permissions.clone()),
        pack_limits: BTreeMap::from([(pack_id, permissions)]),
        bindings: BTreeMap::new(),
        overlay_order: Vec::new(),
        user_overlays: Vec::new(),
        startup: Startup {
            inputs: Vec::new(),
            ui: None,
        },
    };
    (core, host, profile, dir)
}

#[test]
#[cfg(unix)]
fn profile_inventory_preserves_the_existing_recovery_bound_before_opening_authority() {
    use std::os::unix::fs::DirBuilderExt;
    let (core, host, _, temporary) = fixture();
    let data = temporary.path().canonicalize().unwrap().join("data");
    let profiles = data.join("profiles");
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&profiles)
        .unwrap();
    for index in (0..4096).rev() {
        std::fs::DirBuilder::new()
            .mode(0o700)
            .create(profiles.join(format!("prof_{index:04}")))
            .unwrap();
    }
    let ids = core.profile_ids().unwrap();
    assert_eq!(ids.len(), 4096);
    assert_eq!(ids.first().unwrap().as_str(), "prof_0000");
    assert_eq!(ids.last().unwrap().as_str(), "prof_4095");
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(profiles.join("prof_overflow"))
        .unwrap();
    for result in [
        core.profile_ids().map(|_| ()),
        Core::open(CoreConfig::for_test_data_root(data), host.clone()).map(|_| ()),
    ] {
        assert!(
            matches!(result, Err(CoreError::Invalid(message)) if message == "Profile recovery registry is full")
        );
    }
    assert!(host.prompts.lock().unwrap().is_empty());
    for entry in std::fs::read_dir(profiles).unwrap() {
        assert_eq!(std::fs::read_dir(entry.unwrap().path()).unwrap().count(), 0);
    }
}
fn user_prompts(host: &Host) -> usize {
    host.prompts
        .lock()
        .unwrap()
        .iter()
        .filter(|p| matches!(p.effect, EffectRequest::UserPermissions { .. }))
        .count()
}

#[tokio::test]
async fn add_and_update_reject_oversized_tpk_without_changing_profile_or_authority() {
    let (core, host, profile, dir) = fixture();
    let head = core.save_profile(profile.clone(), None).await.unwrap();
    let before = crate::profiles::strict_bytes(&core.profile(&profile.id).unwrap().0).unwrap();
    let head_snapshot = core
        .profiles
        .root
        .profile_head_snapshot(&profile.id)
        .unwrap();
    let prompt_count = host.prompts.lock().unwrap().len();
    // A newly requested permission would be denied. Invalid package capture
    // must finish before even reaching that approval surface.
    host.deny.store(true, Ordering::SeqCst);

    let path = dir.path().join("oversized.tpk");
    let file = std::fs::File::create(&path).unwrap();
    file.set_len(512 * 1024 * 1024 + 1).unwrap();
    let source = format!("file:{}", path.display());
    for update in [false, true] {
        let error = core
            .add_pack(&profile.id, source.clone(), update)
            .await
            .unwrap_err();
        assert!(error.to_string().contains("package exceeds 512 MiB"));
        let (stored, current_head) = core.profile(&profile.id).unwrap();
        assert_eq!(current_head, head);
        assert_eq!(crate::profiles::strict_bytes(&stored).unwrap(), before);
        assert_eq!(
            core.profiles
                .root
                .profile_head_snapshot(&profile.id)
                .unwrap(),
            head_snapshot
        );
        assert_eq!(host.prompts.lock().unwrap().len(), prompt_count);
    }
}

#[tokio::test]
async fn user_approval_is_bound_to_committed_document_revision_and_epoch() {
    let (core, host, mut profile, _dir) = fixture();
    let head = core.save_profile(profile.clone(), None).await.unwrap();
    let session = core.start(&profile.id).await.unwrap();
    let approvals = Approvals::open(&core.profiles.root, &profile.id).unwrap();
    let epoch = session.gate.epoch(&profile.id).unwrap();
    assert_eq!(epoch, 1);
    assert!(
        approvals
            .user_permissions(&profile.id, &head, session.plan_digest(), epoch)
            .unwrap()
            .is_some()
    );
    assert!(
        approvals
            .user_permissions(
                &profile.id,
                &Digest::hash(b"other document"),
                session.plan_digest(),
                epoch
            )
            .unwrap()
            .is_none()
    );
    assert!(
        approvals
            .user_permissions(&profile.id, &head, &Digest::hash(b"other plan"), epoch)
            .unwrap()
            .is_none()
    );
    assert!(
        approvals
            .user_permissions(&profile.id, &head, session.plan_digest(), epoch + 1)
            .unwrap()
            .is_none()
    );
    assert_eq!(user_prompts(&host), 1);
    profile.name = "Edited document".into();
    let next = core
        .save_profile(profile.clone(), Some(head))
        .await
        .unwrap();
    assert_eq!(user_prompts(&host), 2);
    assert!(
        session
            .call_user_port(
                ShapeRef::parse("local.fixture.port@1.0").unwrap(),
                OpName::parse("read").unwrap(),
                &CanonValue::Obj(BTreeMap::new())
            )
            .await
            .is_err()
    );
    let fresh = core.start(&profile.id).await.unwrap();
    let cx = core
        .user_context(&profile, &fresh.gate)
        .unwrap()
        .with_revision(fresh.plan_digest().clone());
    assert!(
        fresh
            .gate
            .effective_permissions(&cx)
            .unwrap()
            .ports
            .contains("local.fixture.port@1.0")
    );
    assert!(
        approvals
            .user_permissions(&profile.id, &next, fresh.plan_digest(), 2)
            .unwrap()
            .is_some()
    );
    session.close().await.unwrap();
    fresh.close().await.unwrap();
}
#[tokio::test]
async fn declining_new_user_scope_leaves_the_profile_head_unchanged() {
    let (core, host, mut profile, _dir) = fixture();
    profile.user_permissions = None;
    let old = core.save_profile(profile.clone(), None).await.unwrap();
    profile.user_permissions = Some(profile.ceiling.clone());
    host.deny.store(true, Ordering::SeqCst);
    assert!(
        core.save_profile(profile.clone(), Some(old.clone()))
            .await
            .is_err()
    );
    assert_eq!(core.profile(&profile.id).unwrap().1, old);
    let session = core.start(&profile.id).await.unwrap();
    let cx = core
        .user_context(&profile, &session.gate)
        .unwrap()
        .with_revision(session.plan_digest().clone());
    assert!(
        session
            .gate
            .effective_permissions(&cx)
            .unwrap()
            .ports
            .is_empty()
    );
    session.close().await.unwrap();
}

#[tokio::test]
async fn user_refusal_does_not_install_an_earlier_pack_approval() {
    let (core, host, profile, _dir) = fixture();
    host.deny_user.store(true, Ordering::SeqCst);
    assert!(core.save_profile(profile.clone(), None).await.is_err());
    let prompts = host.prompts.lock().unwrap();
    assert_eq!(prompts.len(), 2);
    assert!(matches!(
        prompts[0].effect,
        EffectRequest::Permissions { .. }
    ));
    assert!(matches!(
        prompts[1].effect,
        EffectRequest::UserPermissions { .. }
    ));
    let approvals = Approvals::open(&core.profiles.root, &profile.id).unwrap();
    assert!(
        approvals
            .permissions(&profile.id, &profile.packs[0].id, &profile.packs[0].sha256)
            .unwrap()
            .is_none()
    );
    assert_eq!(approvals.security_epoch(&profile.id).unwrap(), 0);
    assert!(core.profiles.head(&profile.id).unwrap().is_none());
}

fn permission_preparation_fixture(
    core: &Core,
    profile: &Profile,
) -> (Arc<GateEngine>, CallCx, Vec<VerifiedPack>) {
    let packs = load_packs(profile, &core.control).unwrap();
    let compiled = compile(profile, &packs).unwrap();
    let (gate, _) = core.authority(profile).unwrap();
    register_policies(&gate, profile, &packs).unwrap();
    register_views(&gate, &compiled).unwrap();
    let cx = core
        .user_context(profile, &gate)
        .unwrap()
        .with_revision(compiled.plan_digest);
    (gate, cx, packs)
}

#[tokio::test]
async fn later_permission_sql_failure_rolls_back_the_entire_batch_without_erasing_other_approvals()
{
    for independent in [false, true] {
        for fail_at_terminal in [false, true] {
            let (core, _host, profile, _dir) = fixture();
            let approvals = Approvals::open(&core.profiles.root, &profile.id).unwrap();
            if independent {
                let (gate, cx, packs) = permission_preparation_fixture(&core, &profile);
                gate.approve_pack_permissions(&cx, &packs[0].manifest.id, None, &packs[0].hash)
                    .await
                    .unwrap();
            }
            let before = approvals
                .permissions(&profile.id, &profile.packs[0].id, &profile.packs[0].sha256)
                .unwrap();
            assert_eq!(before.is_some(), independent);
            let raw = rusqlite::Connection::open_with_flags(
                core.profiles
                    .root
                    .path()
                    .join(DataRoot::profile_relative(&profile.id, "approvals.sqlite").unwrap()),
                rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE,
            )
            .unwrap();
            let effects_before: i64 = raw
                .query_row("SELECT COUNT(*) FROM permission_effects", [], |row| {
                    row.get(0)
                })
                .unwrap();
            if fail_at_terminal {
                // Both policy writes and the first successful terminal have
                // already run inside the transaction when this fault fires.
                raw.execute_batch(
                    "CREATE TRIGGER reject_permission_batch BEFORE INSERT ON terminal_outbox
                     WHEN EXISTS(SELECT 1 FROM user_approvals)
                     BEGIN SELECT RAISE(ABORT,'injected final terminal failure'); END;",
                )
                .unwrap();
            } else {
                raw.execute_batch(
                    "CREATE TRIGGER reject_permission_batch BEFORE INSERT ON user_approvals
                     BEGIN SELECT RAISE(ABORT,'injected later policy failure'); END;",
                )
                .unwrap();
            }
            assert!(core.save_profile(profile.clone(), None).await.is_err());
            assert_eq!(
                approvals
                    .permissions(&profile.id, &profile.packs[0].id, &profile.packs[0].sha256)
                    .unwrap(),
                before
            );
            assert_eq!(
                raw.query_row("SELECT COUNT(*) FROM user_approvals", [], |row| row
                    .get::<_, i64>(0))
                    .unwrap(),
                0
            );
            assert_eq!(
                raw.query_row("SELECT COUNT(*) FROM permission_effects", [], |row| row
                    .get::<_, i64>(0))
                    .unwrap(),
                effects_before
            );
            assert_eq!(approvals.security_epoch(&profile.id).unwrap(), 0);
            assert!(core.profiles.head(&profile.id).unwrap().is_none());
            // Compare actual immutable decisions, not the current head or the
            // presence of failed/abandoned reservation rows.
            let decisions = raw
                .prepare("SELECT body FROM terminal_outbox")
                .unwrap()
                .query_map([], |row| row.get::<_, Vec<u8>>(0))
                .unwrap()
                .map(|body| {
                    serde_json::from_slice::<tobkiri_gate::TerminalDecision>(&body.unwrap())
                        .unwrap()
                })
                .collect::<Vec<_>>();
            assert_eq!(
                decisions
                    .iter()
                    .filter(|decision| decision.outcome == tobkiri_gate::Outcome::Succeeded)
                    .count() as i64,
                effects_before
            );
            raw.execute_batch("DROP TRIGGER reject_permission_batch")
                .unwrap();
            // This deterministic injected ABORT has been observed and removed;
            // a fresh request can succeed. Unknown commits must not auto-replay.
            let committed = core.save_profile(profile.clone(), None).await.unwrap();
            assert_eq!(core.profile(&profile.id).unwrap().1, committed);
        }
    }
}

#[tokio::test]
async fn prepared_permission_batch_rejects_different_original_contexts_before_policy_effects() {
    let (core, _host, profile, _dir) = fixture();
    let (gate, cx, packs) = permission_preparation_fixture(&core, &profile);
    let other = core
        .user_context(&profile, &gate)
        .unwrap()
        .with_revision(cx.revision().unwrap().clone());
    let first = gate
        .prepare_pack_permissions(&cx, &packs[0].manifest.id, None, &packs[0].hash)
        .await
        .unwrap();
    let second = gate
        .prepare_pack_permissions(&other, &packs[0].manifest.id, None, &packs[0].hash)
        .await
        .unwrap();
    let error =
        tobkiri_gate::PreparedPermissionApproval::commit_all(vec![first, second]).unwrap_err();
    assert!(matches!(error, tobkiri_gate::GateError::Denied(_)));
    let approvals = Approvals::open(&core.profiles.root, &profile.id).unwrap();
    assert!(
        approvals
            .permissions(&profile.id, &packs[0].manifest.id, &packs[0].hash)
            .unwrap()
            .is_none()
    );
}

#[tokio::test]
async fn prepared_permissions_are_inert_and_drop_preserves_an_independent_approval() {
    let (core, host, profile, _dir) = fixture();
    let (gate, cx, packs) = permission_preparation_fixture(&core, &profile);
    let approvals = Approvals::open(&core.profiles.root, &profile.id).unwrap();
    let before = core.control.usage();
    let prepared = gate
        .prepare_pack_permissions(&cx, &packs[0].manifest.id, None, &packs[0].hash)
        .await
        .unwrap();
    assert_eq!(host.prompts.lock().unwrap().len(), 1);
    assert!(
        approvals
            .permissions(&profile.id, &packs[0].manifest.id, &packs[0].hash)
            .unwrap()
            .is_none()
    );
    assert_eq!(
        core.control.usage().count(tobkiri_gate::ControlKind::Queue),
        before.count(tobkiri_gate::ControlKind::Queue) + 1
    );
    assert!(core.control.usage().bytes >= before.bytes + 4096);
    // Another ordinary native request may approve the same Pack while the
    // first preparation is still pending. Abandoning the first must not undo it.
    let independent = core
        .user_context(&profile, &gate)
        .unwrap()
        .with_revision(cx.revision().unwrap().clone());
    assert_ne!(independent.request(), cx.request());
    gate.approve_pack_permissions(&independent, &packs[0].manifest.id, None, &packs[0].hash)
        .await
        .unwrap();
    drop(independent);
    assert_eq!(host.prompts.lock().unwrap().len(), 2);
    assert!(
        approvals
            .permissions(&profile.id, &packs[0].manifest.id, &packs[0].hash)
            .unwrap()
            .is_some()
    );
    drop(prepared);
    assert!(
        approvals
            .permissions(&profile.id, &packs[0].manifest.id, &packs[0].hash)
            .unwrap()
            .is_some()
    );
    assert_eq!(core.control.usage().bytes, before.bytes);
    assert_eq!(
        core.control.usage().count(tobkiri_gate::ControlKind::Queue),
        before.count(tobkiri_gate::ControlKind::Queue)
    );
}

#[tokio::test]
async fn prepared_user_approval_retains_the_exact_candidate_after_temporary_bindings_drop() {
    let (core, _host, profile, _dir) = fixture();
    let (gate, cx, _packs) = permission_preparation_fixture(&core, &profile);
    let candidate = Digest::hash(b"temporary-reviewed-candidate");
    let target_epoch = cx.security_epoch() + 1;
    let before = core.control.usage().bytes;
    let prepared = {
        let temporary_candidate = candidate.clone();
        let temporary_plan = cx.revision().unwrap().clone();
        gate.prepare_user_permissions(
            &cx,
            &temporary_candidate,
            &temporary_plan,
            target_epoch,
            &profile.ceiling,
        )
        .await
        .unwrap()
    };
    tobkiri_gate::PreparedPermissionApproval::commit_all(vec![prepared]).unwrap();
    let approvals = Approvals::open(&core.profiles.root, &profile.id).unwrap();
    assert_eq!(
        approvals
            .user_permissions(
                &profile.id,
                &candidate,
                cx.revision().unwrap(),
                target_epoch,
            )
            .unwrap(),
        Some(profile.ceiling.clone())
    );
    assert_eq!(approvals.security_epoch(&profile.id).unwrap(), 0);
    assert!(core.profiles.head(&profile.id).unwrap().is_none());
    assert_eq!(core.control.usage().bytes, before);
}

#[tokio::test]
async fn prepared_permissions_recheck_cancellation_and_epoch_before_effect() {
    for revoke_epoch in [false, true] {
        let (core, _host, profile, _dir) = fixture();
        let (gate, cx, packs) = permission_preparation_fixture(&core, &profile);
        let approvals = Approvals::open(&core.profiles.root, &profile.id).unwrap();
        let before = core.control.usage();
        let prepared = gate
            .prepare_pack_permissions(&cx, &packs[0].manifest.id, None, &packs[0].hash)
            .await
            .unwrap();
        if revoke_epoch {
            ApprovalStore::advance_security_epoch(&approvals, &profile.id).unwrap();
        } else {
            cx.cancellation().cancel();
        }
        let failure = prepared.commit().unwrap_err();
        assert!(matches!(
            (revoke_epoch, failure),
            (true, tobkiri_gate::GateError::StaleEpoch)
                | (false, tobkiri_gate::GateError::Cancelled)
        ));
        assert!(
            approvals
                .permissions(&profile.id, &packs[0].manifest.id, &packs[0].hash)
                .unwrap()
                .is_none()
        );
        assert_eq!(core.control.usage().bytes, before.bytes);
    }
}

#[test]
fn core_open_and_read_only_inspection_do_not_require_tokio() {
    let (core, _host, profile, dir) = fixture();
    let runtime = tokio::runtime::Runtime::new().unwrap();
    runtime
        .block_on(core.save_profile(profile.clone(), None))
        .unwrap();
    let root = std::fs::canonicalize(dir.path()).unwrap().join("data");
    drop(core);
    drop(runtime);
    let host = Arc::new(Host::default());
    let reopened = Core::open(CoreConfig::for_test_data_root(root), host).unwrap();
    assert_eq!(reopened.profile(&profile.id).unwrap().0.name, profile.name);
    let state = reopened.profile_save_precondition(&profile.id).unwrap();
    assert!(state.head.is_some());
    assert_eq!(state.security_epoch, 1);
    assert!(!reopened.export_profile(&profile.id).unwrap().is_empty());
}

#[cfg(unix)]
#[tokio::test]
async fn reopening_core_finishes_the_exact_decided_save_without_reprompting_or_replaying_permissions()
 {
    let (core, host, profile, dir) = fixture();
    let _approvals = Approvals::open(&core.profiles.root, &profile.id).unwrap();
    let raw = rusqlite::Connection::open_with_flags(
        core.profiles
            .root
            .path()
            .join(DataRoot::profile_relative(&profile.id, "approvals.sqlite").unwrap()),
        rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE,
    )
    .unwrap();
    raw.execute_batch(
        "CREATE TRIGGER reject_save_settlement BEFORE UPDATE OF settled ON profile_save_decisions
         BEGIN SELECT RAISE(ABORT,'injected outcome settlement failure'); END;",
    )
    .unwrap();
    let error = core.save_profile(profile.clone(), None).await.unwrap_err();
    let operation = error.profile_save_operation().unwrap().clone();
    assert_eq!(error.profile_save_profile(), Some(&profile.id));
    assert_eq!(
        error.profile_commit_progress(),
        Some(tobkiri_store::ProfileCommitProgress::HeadReplaced)
    );
    let committed = core.profile(&profile.id).unwrap().1;
    assert!(
        core.profile_save_outcome(&profile.id, &operation)
            .unwrap()
            .is_none()
    );
    assert!(core.start(&profile.id).await.is_err());
    assert_eq!(host.prompts.lock().unwrap().len(), 2);
    let count = |raw: &rusqlite::Connection| {
        raw.query_row("SELECT COUNT(*) FROM permission_effects", [], |row| {
            row.get::<_, i64>(0)
        })
        .unwrap()
    };
    assert_eq!(count(&raw), 2);
    // An original legacy uncertainty has no coordinated identity. Do not
    // accidentally attach a cleanup operation's unrelated identity to it.
    let combined = CoreError::Cleanup {
        original: Box::new(
            tobkiri_store::StoreError::ProfileCommitUncertain {
                progress: tobkiri_store::ProfileCommitProgress::EpochCommitAttempted,
                operation: None,
                source: Box::new(tobkiri_store::StoreError::Unavailable("legacy".into())),
            }
            .into(),
        ),
        cleanup: Box::new(error),
    };
    assert!(combined.profile_save_operation().is_none());
    assert!(combined.profile_save_profile().is_none());
    drop(core);
    raw.execute_batch("DROP TRIGGER reject_save_settlement")
        .unwrap();
    let root = std::fs::canonicalize(dir.path()).unwrap().join("data");
    let fresh_host = Arc::new(Host::default());
    let reopened = Core::open(CoreConfig::for_test_data_root(root), fresh_host.clone()).unwrap();
    let receipt = reopened
        .profile_save_outcome(&profile.id, &operation)
        .unwrap()
        .unwrap();
    assert_eq!(receipt.operation(), &operation);
    assert_eq!(receipt.revision(), &committed);
    assert_eq!(receipt.epoch(), 1);
    assert_eq!(reopened.profile(&profile.id).unwrap().1, committed);
    assert!(fresh_host.prompts.lock().unwrap().is_empty());
    assert_eq!(count(&raw), 2);
}

#[cfg(unix)]
#[tokio::test]
async fn audit_delivery_failure_after_save_retains_the_exact_committed_outcome_for_cold_recovery() {
    let (core, host, profile, dir) = fixture();
    let _approvals = Approvals::open(&core.profiles.root, &profile.id).unwrap();
    let _audit = AuditLog::open(&core.profiles.root, &profile.id, AuditQuota::default()).unwrap();
    let raw = rusqlite::Connection::open_with_flags(
        core.profiles
            .root
            .path()
            .join(DataRoot::profile_relative(&profile.id, "audit.sqlite").unwrap()),
        rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE,
    )
    .unwrap();
    raw.execute_batch(
        "CREATE TRIGGER reject_save_delivery BEFORE INSERT ON audit WHEN NEW.event_id IS NOT NULL
         BEGIN SELECT RAISE(ABORT,'injected terminal delivery failure'); END;",
    )
    .unwrap();
    let error = core.save_profile(profile.clone(), None).await.unwrap_err();
    let operation = error.profile_save_operation().unwrap().clone();
    assert_eq!(error.profile_save_profile(), Some(&profile.id));
    assert_eq!(
        error.profile_commit_progress(),
        Some(tobkiri_store::ProfileCommitProgress::HeadReplaced)
    );
    let committed = core.profile(&profile.id).unwrap().1;
    let receipt = core
        .profile_save_outcome(&profile.id, &operation)
        .unwrap()
        .unwrap();
    assert_eq!(receipt.revision(), &committed);
    assert_eq!(host.prompts.lock().unwrap().len(), 2);
    drop(receipt);
    drop(core);
    raw.execute_batch("DROP TRIGGER reject_save_delivery")
        .unwrap();
    let fresh_host = Arc::new(Host::default());
    let reopened = Core::open(
        CoreConfig::for_test_data_root(std::fs::canonicalize(dir.path()).unwrap().join("data")),
        fresh_host.clone(),
    )
    .unwrap();
    assert_eq!(
        reopened
            .profile_save_outcome(&profile.id, &operation)
            .unwrap()
            .unwrap()
            .revision(),
        &committed
    );
    let session = reopened.start(&profile.id).await.unwrap();
    session.close().await.unwrap();
    assert!(fresh_host.prompts.lock().unwrap().is_empty());
    let raw_approvals = rusqlite::Connection::open_with_flags(
        reopened
            .profiles
            .root
            .path()
            .join(DataRoot::profile_relative(&profile.id, "approvals.sqlite").unwrap()),
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    )
    .unwrap();
    assert_eq!(
        raw_approvals
            .query_row("SELECT COUNT(*) FROM permission_effects", [], |row| row
                .get::<_, i64>(0))
            .unwrap(),
        2
    );
}

#[tokio::test]
async fn faulted_profile_remains_inspectable_without_gate_or_ready_barrier() {
    let (core, host, profile, dir) = fixture();
    core.save_profile(profile.clone(), None).await.unwrap();
    let mut healthy = profile.clone();
    healthy.id = ProfileId::parse("prof_healthy").unwrap();
    core.save_profile(healthy.clone(), None).await.unwrap();
    let approvals = Approvals::open(&core.profiles.root, &profile.id).unwrap();
    approvals
        .latch_integrity(&profile.id, tobkiri_gate::IntegrityFaultCode::AuditChain)
        .unwrap();
    let root = std::fs::canonicalize(dir.path()).unwrap().join("data");
    drop(core);
    let reopened = Core::open(CoreConfig::for_test_data_root(root), host).unwrap();
    assert_eq!(
        reopened.profile_recovery_status(&profile.id).unwrap(),
        ProfileRecoveryStatus::IntegrityFault {
            code: tobkiri_gate::IntegrityFaultCode::AuditChain
        }
    );
    assert_eq!(reopened.profile(&profile.id).unwrap().0.name, profile.name);
    assert!(!reopened.export_profile(&profile.id).unwrap().is_empty());
    assert!(!reopened.admissions.contains(&profile.id));
    assert!(reopened.start(&profile.id).await.is_err());
    assert!(!reopened.admissions.contains(&profile.id));
    assert!(reopened.admissions.contains(&healthy.id));
    let session = reopened.start(&healthy.id).await.unwrap();
    session.close().await.unwrap();
}

#[tokio::test]
async fn published_user_policy_import_requires_fresh_profile_scoped_approval() {
    let (core, host, profile, _dir) = fixture();
    core.save_profile(profile.clone(), None).await.unwrap();
    let export = core.export_profile(&profile.id).unwrap();
    let imported = core
        .import_profile(&export, "Imported fixture".into(), None)
        .await
        .unwrap();
    assert_ne!(imported, profile.id);
    assert_eq!(user_prompts(&host), 2);
    let session = core.start(&imported).await.unwrap();
    let (document, hash) = core.profile(&imported).unwrap();
    let approvals = Approvals::open(&core.profiles.root, &imported).unwrap();
    assert!(
        approvals
            .user_permissions(
                &imported,
                &hash,
                session.plan_digest(),
                session.gate.epoch(&imported).unwrap()
            )
            .unwrap()
            .is_some()
    );
    let cx = core
        .user_context(&document, &session.gate)
        .unwrap()
        .with_revision(session.plan_digest().clone());
    assert!(
        session
            .gate
            .effective_permissions(&cx)
            .unwrap()
            .ports
            .contains("local.fixture.port@1.0")
    );
    session.close().await.unwrap();
}
