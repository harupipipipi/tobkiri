//! Portable tests for Host persistence after the fresh catalog caller.
use super::super::candidate_selection::RegisteredActivationScope;
use super::{
    tests::{
        Source, digest, entry, lock_free, sealed, state, with_setup_source, with_source,
        write_index,
    },
    *,
};
use rusqlite::{Connection, OpenFlags};
use serde_json::json;
use std::io::Read;
use std::os::unix::fs::{PermissionsExt, symlink};
use std::sync::atomic::{AtomicUsize, Ordering};

struct ChangingOwner<'a> {
    first: &'a Path,
    next: &'a Path,
    calls: AtomicUsize,
}
impl OriginalAuthorityOwner for ChangingOwner<'_> {
    fn with_original_user_data<R>(&self, operation: impl FnOnce(&Path) -> R) -> Result<R> {
        Ok(operation(self.next))
    }
    fn check_original_owner(&self) -> Result<()> {
        Ok(())
    }
}
impl crate::BootstrapReviewOwner for ChangingOwner<'_> {
    fn with_original_review_user_data<R>(&self, operation: impl FnOnce(&Path) -> R) -> Result<R> {
        let calls = self.calls.fetch_add(1, Ordering::Relaxed);
        Ok(operation(if calls < 3 { self.first } else { self.next }))
    }
}

#[tokio::test(flavor = "current_thread")]
async fn confirmed_publication_checks_handover_before_lock_and_holds_it_without_intent() {
    use super::super::{JOURNAL, LOCK, with_confirmed_predecessor};
    use super::tests::operation;

    for case in [
        "no-handover",
        "missing-journal",
        "incomplete-handover",
        "completed-handover",
        "bad-intent",
    ] {
        with_source(|source, read, cancel, budget| {
            let root = &source.root;
            let lock = root.join("profiles").join(LOCK);
            let intent = root.join("profiles").join(JOURNAL);
            let key = [7; 32];
            if matches!(
                case,
                "missing-journal" | "incomplete-handover" | "completed-handover"
            ) {
                fs::create_dir(root).unwrap();
                fs::write(root.join(development_handover::KEY), key).unwrap();
                if case != "missing-journal" {
                    let stage = if case == "completed-handover" {
                        "completed"
                    } else {
                        "staging"
                    };
                    let mut payload = json!({
                        "schema": "io.tobkiri.development-host-handover-journal.v1",
                        "stage": stage,
                    });
                    let data = serde_json::to_vec(&payload).unwrap();
                    let variant = tobkiri_canon::Variant::StrictV1;
                    let canonical = tobkiri_canon::parse(&data, variant).unwrap();
                    payload["authentication"] =
                        json!(tobkiri_canon::seal(&canonical, variant, &key).unwrap());
                    fs::write(
                        root.join(development_handover::JOURNAL),
                        serde_json::to_vec(&payload).unwrap(),
                    )
                    .unwrap();
                }
            }
            if case == "bad-intent" {
                fs::create_dir_all(root.join("profiles")).unwrap();
                fs::write(&intent, b"invalid intent").unwrap();
            }
            let op = operation(read, source, cancel);
            let result = with_confirmed_predecessor(&op, root, budget, |current| {
                assert!(matches!(case, "no-handover" | "completed-handover"));
                assert!(current.is_none());
                assert!(!lock_free(&lock));
                assert!(!intent.exists());
                Ok(())
            });
            match case {
                "no-handover" | "completed-handover" => {
                    result.unwrap();
                    assert!(lock_free(&lock));
                    assert!(!intent.exists());
                }
                "missing-journal" | "incomplete-handover" => {
                    let error = result.unwrap_err();
                    assert!(
                        error
                            .message
                            .starts_with("Development Host handover requires")
                    );
                    assert!(!root.join("profiles").exists());
                }
                "bad-intent" => {
                    assert_eq!(
                        result.unwrap_err().message,
                        "Profile publication recovery failed closed"
                    );
                    assert!(lock_free(&lock));
                    assert_eq!(fs::read(&intent).unwrap(), b"invalid intent");
                }
                _ => unreachable!(),
            }
        });
    }
}

#[tokio::test(flavor = "current_thread")]
async fn both_recoveries_precede_selection_and_absent_root_stays_absent() {
    for additions in [false, true] {
        with_source(|source, read, cancel, budget| {
            assert!(read.acquire_candidate_root(source, cancel).is_err());
            assert_eq!(
                read.enter_recovery(source, cancel, true).unwrap(),
                Prefix::NoJournal
            );
            assert!(read.acquire_candidate_root(source, cancel).is_err());
            assert_eq!(
                read.enter_recovery(source, cancel, false).unwrap(),
                Prefix::NoJournal
            );
            assert!(read.check_continuation_thread().is_err());
            read.acquire_candidate_root(source, cancel).unwrap();
            let other = cancel.child().unwrap();
            assert!(read.check_candidate_root(source, &other).is_err());
            read.check_candidate_root(source, cancel).unwrap();
            assert!(
                read.with_registered_activation_authority::<_, ()>(source, cancel, |_| {
                    panic!("Authority entered before a registered candidate was selected")
                })
                .is_err()
            );
            let selected = read
                .candidate_selection(source, cancel, "defaults", additions, budget)
                .unwrap();
            if additions {
                assert!(matches!(
                    selected,
                    CandidateSelection::Denied("source update requires an active Profile")
                ));
            } else {
                assert!(matches!(selected, CandidateSelection::BundledCatalog));
            }
            assert!(
                read.with_registered_activation_authority::<_, ()>(source, cancel, |_| {
                    panic!("Authority entered for a fallback candidate")
                })
                .is_err()
            );
            read.check_candidate_root(source, cancel).unwrap();
            assert!(
                read.candidate_selection(source, cancel, "defaults", additions, budget)
                    .is_err()
            );
            assert!(!source.root.exists());
            read.close_continuation().unwrap();
            assert!(read.check_candidate_root(source, cancel).is_err());
        });
    }
}

#[tokio::test(flavor = "current_thread")]
async fn bundled_resolver_reselects_user_data_and_does_not_create_genesis_state() {
    for authority_directory in [false, true] {
        with_source(|source, read, cancel, budget| {
            let later = source.root.with_file_name("later-user-data");
            if authority_directory {
                fs::create_dir_all(later.join("authority/v4.sqlite3")).unwrap();
            }
            let owner = ChangingOwner {
                first: &source.root,
                next: &later,
                calls: AtomicUsize::new(0),
            };
            read.enter_recovery(&owner, cancel, true).unwrap();
            read.enter_recovery(&owner, cancel, false).unwrap();
            read.acquire_candidate_root(&owner, cancel).unwrap();
            assert!(read.acquire_resolver_root(&owner, cancel).is_err());
            assert!(matches!(
                read.candidate_selection(&owner, cancel, "defaults", false, budget)
                    .unwrap(),
                CandidateSelection::BundledCatalog
            ));
            read.acquire_resolver_root(&owner, cancel).unwrap();
            assert_eq!(owner.calls.load(Ordering::Relaxed), 4);
            let bundle = Digest::hash(b"second physical lock");
            let actual = read
                .resolver_authority_snapshot(&owner, cancel, bundle.clone())
                .unwrap();
            if authority_directory {
                assert!(actual.is_none());
            } else {
                let actual = actual.unwrap();
                let (material, snapshot_digest, bundle_lock_digest) = actual.observation();
                assert!(
                    matches!(material, OriginalAuthorityMaterial::Genesis { epoch, .. } if epoch.decimal() == "1")
                );
                assert_eq!(*bundle_lock_digest, bundle);
                let expected = json!({
                    "schema":"io.tobkiri.profile-authority-snapshot.v1",
                    "security_epoch":1,
                    "security_epoch_reason_digest":Digest::hash(br#"{"reason":"genesis"}"#).as_str(),
                    "bundle_lock_digest":bundle.as_str(),
                    "grant_import":"none",
                });
                assert_eq!(snapshot_digest.as_str(), digest(&expected));
                assert!(!later.exists());
            }
            assert!(!source.root.exists());
            assert!(read.receipt().verified());
            assert!(
                read.resolver_authority_snapshot(&owner, cancel, bundle)
                    .is_err()
            );
            read.close_continuation().unwrap();
            assert!(read.check_resolver_root(&owner, cancel).is_err());
        });
    }
}

#[tokio::test(flavor = "current_thread")]
async fn dangling_pointer_exposes_raw_read_error_before_registry_io() {
    with_source(|source, read, cancel, budget| {
        let profiles = source.root.join("profiles");
        fs::create_dir_all(&profiles).unwrap();
        symlink("missing", profiles.join("active.json")).unwrap();
        read.enter_recovery(source, cancel, true).unwrap();
        read.enter_recovery(source, cancel, false).unwrap();
        read.acquire_candidate_root(source, cancel).unwrap();
        let error = read
            .candidate_selection(source, cancel, "defaults", false, budget)
            .err()
            .expect("dangling pointer error");
        assert_eq!(
            error.class,
            "core_runtime.active_profile_store_v4.ActiveProfileStoreIntegrityError"
        );
        assert_eq!(error.message, "active Profile pointer is unavailable");
        assert!(error.suppressed);
        assert!(error.context.is_none());
        assert!(matches!(
            error.cause,
            Some(OriginalAuthorityCause::Python {
                class: "tobkiri_protocol.secure_persistence.SecurePersistenceError",
                message,
                cause: None,
                context: None,
                suppressed: false,
            }) if message == "persistence entry is symlinked"
        ));
        assert!(profiles.join("active.json").is_symlink());
        assert!(!profiles.join(".profile-definition.lock").exists());
        assert!(!profiles.join(LOCK).exists());
    });
}

#[tokio::test(flavor = "current_thread")]
async fn verified_pointer_gates_the_definition_snapshot_and_active_authority_boundary() {
    for (requested, registered) in [("other", false), ("defaults", false), ("defaults", true)] {
        with_source(|source, read, cancel, budget| {
            let profile_id = "defaults";
            seed_candidate(source, registered);
            read.enter_recovery(source, cancel, true).unwrap();
            read.enter_recovery(source, cancel, false).unwrap();
            read.acquire_candidate_root(source, cancel).unwrap();
            let selected = read
                .candidate_selection(source, cancel, requested, true, budget)
                .unwrap();
            let definition_lock = source.root.join("profiles/.profile-definition.lock");
            if requested != profile_id {
                assert!(matches!(
                    selected,
                    CandidateSelection::Denied("source update requires the active Profile")
                ));
                assert!(!definition_lock.exists());
            } else if registered {
                assert!(matches!(selected, CandidateSelection::ActiveAuthority));
                assert!(lock_free(&definition_lock));
            } else {
                assert!(matches!(
                    selected,
                    CandidateSelection::Denied("source update requires a registered Profile")
                ));
                assert!(lock_free(&definition_lock));
                assert!(!source.root.join("profiles/index.json").exists());
            }
            assert!(!source.root.join("profiles").join(LOCK).exists());
        });
    }
}

fn seed_candidate(source: &Source, registered: bool) {
    let profile_id = "defaults";
    let definition = entry(profile_id, 0);
    let revision = &definition["current_revision"];
    let plan = digest(&json!("plan"));
    let lock = digest(&json!("lock"));
    let activation_id = "activation:defaults-selection";
    let relative = "workspaces/defaults/activation/activations/defaults-selection.json";
    let snapshot = json!({
        "profile": {"profile_id": profile_id},
        "lock": {"lock_digest": lock},
        "plan": {"profile_revision": revision, "plan_digest": plan},
        "activation": {"profile_id": profile_id, "profile_revision": revision,
            "activation_id": activation_id, "plan_digest": plan, "lock_digest": lock,
            "state": "active"},
    });
    let mut pointer = json!({
        "schema": "io.tobkiri.active-profile-pointer.v1", "profile_id": profile_id,
        "profile_revision": revision, "activation_id": activation_id,
        "plan_digest": plan, "lock_digest": lock, "activation_snapshot_path": relative,
        "activation_snapshot_digest": digest(&snapshot), "catalog_revision": null,
        "generation": 1, "updated_at": 10,
    });
    let pointer_digest = digest(&pointer);
    pointer["pointer_digest"] = json!(pointer_digest);
    for (path, value) in [(relative, &snapshot), ("profiles/active.json", &pointer)] {
        let path = source.root.join(path);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(&path, serde_json::to_vec(value).unwrap()).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
    }
    if registered {
        write_index(source, &sealed(state(vec![definition])));
    }
}

#[tokio::test(flavor = "current_thread")]
async fn predecessor_rechecks_registry_and_pointer_in_the_resolver_scope() {
    let reference =
        std::env::var_os("TOBKIRI_ORIGINAL_BOOTSTRAP_PREDECESSOR_REFERENCE").map(|path| {
            let file = std::fs::File::open(path).unwrap();
            let mut bytes = Vec::new();
            file.take(512 * 1024 + 1).read_to_end(&mut bytes).unwrap();
            assert!(bytes.len() <= 512 * 1024);
            let reference: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
            assert_eq!(
                reference["format"],
                "io.tobkiri.original-setup-shell-reference.v1"
            );
            assert_eq!(
                reference["original_head"],
                "1cf025f1eeab31a3daa2861b1f26a025c6954b6e"
            );
            reference
        });
    for case in ["fresh", "corrupt-pointer", "corrupt-registry", "registered"] {
        with_source(|source, read, cancel, budget| {
            read.enter_recovery(source, cancel, true).unwrap();
            read.enter_recovery(source, cancel, false).unwrap();
            read.acquire_candidate_root(source, cancel).unwrap();
            assert!(matches!(
                read.candidate_selection(source, cancel, "defaults", false, budget)
                    .unwrap(),
                CandidateSelection::BundledCatalog
            ));
            read.acquire_resolver_root(source, cancel).unwrap();
            read.resolver_authority_snapshot(source, cancel, Digest::hash(b"bundle lock"))
                .unwrap()
                .unwrap();
            let profiles = source.root.join("profiles");
            if case == "registered" {
                seed_candidate(source, true);
            } else if case != "fresh" {
                fs::create_dir_all(&profiles).unwrap();
                let pointer = profiles.join("active.json");
                fs::write(&pointer, b"{\n").unwrap();
                fs::set_permissions(&pointer, fs::Permissions::from_mode(0o600)).unwrap();
                if case == "corrupt-registry" {
                    write_index(source, b"{\n");
                }
            }
            let result = read.bootstrap_predecessor_inputs(source, cancel, "defaults", budget);
            if let (Some(reference), Some(name)) = (
                reference.as_ref(),
                match case {
                    "fresh" => Some("fresh-pack-pin-predecessor"),
                    "corrupt-pointer" => Some("corrupt-pack-pin-predecessor"),
                    _ => None,
                },
            ) {
                let rows = reference["predecessor_cases"].as_array().unwrap();
                assert_eq!(rows.len(), 2);
                let row = rows.iter().find(|row| row["case"] == name).unwrap();
                let actual = match &result {
                    Ok(inputs) => {
                        assert!(!inputs.needs_source_digest("defaults"));
                        json!({"outcome": "no-predecessor"})
                    }
                    Err(error) => super::reference_ci::original_error(error),
                };
                let mut files: Vec<_> = fs::read_dir(&profiles)
                    .unwrap()
                    .map(|entry| {
                        let entry = entry.unwrap();
                        assert!(entry.file_type().unwrap().is_file());
                        format!("profiles/{}", entry.file_name().into_string().unwrap())
                    })
                    .collect();
                files.sort();
                assert_eq!(row["expected"], actual);
                assert_eq!(row["files_after"], json!(files));
            }
            match case {
                "corrupt-pointer" => {
                    let Err(error) = result else {
                        panic!("the malformed active pointer was accepted");
                    };
                    assert_eq!(
                        error.class,
                        "core_runtime.active_profile_store_v4.ActiveProfileStoreIntegrityError"
                    );
                    assert_eq!(error.message, "active Profile pointer is unavailable");
                    assert!(error.suppressed);
                    assert!(
                        matches!(error.cause, Some(OriginalAuthorityCause::Python { class: "tobkiri_protocol.errors.CanonicalizationError", cause: Some(cause), .. }) if matches!(*cause, OriginalAuthorityCause::Python { class: "json.decoder.JSONDecodeError", .. }))
                    );
                }
                "corrupt-registry" => {
                    let Err(error) = result else {
                        panic!("the malformed definition was accepted");
                    };
                    assert_eq!(
                        error.class,
                        "core_runtime.profile_definition_store_v4.ProfileDefinitionStoreIntegrityError"
                    );
                }
                _ => {
                    let inputs = result.unwrap();
                    let source_digest =
                        Digest::parse(entry("defaults", 0)["current_revision"].as_str().unwrap())
                            .unwrap();
                    assert_eq!(inputs.needs_source_digest("defaults"), case == "registered");
                    assert!(!inputs.needs_source_digest("other"));
                    assert_eq!(
                        inputs.matches_source_digest(&source_digest),
                        case == "registered"
                    );
                    assert!(!inputs.matches_source_digest(&Digest::hash(b"different definition")));
                    if case == "fresh" {
                        assert!(
                            !read
                                .inactive_predecessor_has_registry(source, cancel, "defaults")
                                .unwrap()
                        );
                        assert!(
                            read.inactive_predecessor_has_registry(source, cancel, "defaults")
                                .is_err()
                        );
                    }
                }
            }
            assert!(lock_free(&profiles.join(".profile-definition.lock")));
            if matches!(case, "fresh" | "corrupt-pointer") {
                assert!(!profiles.join("index.json").exists());
            }
            assert!(!profiles.join(LOCK).exists());
            assert!(
                read.bootstrap_predecessor_inputs(source, cancel, "defaults", budget)
                    .is_err()
            );
            assert!(read.receipt().verified());
            read.close_continuation().unwrap();
        });
    }
}

#[tokio::test(flavor = "current_thread")]
async fn inactive_predecessor_treats_dangling_active_and_index_paths_as_present() {
    for (active, index, expected) in [(true, true, false), (false, true, true)] {
        with_source(|source, read, cancel, budget| {
            for discard in [true, false] {
                read.enter_recovery(source, cancel, discard).unwrap();
            }
            read.acquire_candidate_root(source, cancel).unwrap();
            assert!(matches!(
                read.candidate_selection(source, cancel, "defaults", false, budget)
                    .unwrap(),
                CandidateSelection::BundledCatalog
            ));
            read.acquire_resolver_root(source, cancel).unwrap();
            read.resolver_authority_snapshot(source, cancel, Digest::hash(b"bundle lock"))
                .unwrap()
                .unwrap();
            read.bootstrap_predecessor_inputs(source, cancel, "defaults", budget)
                .unwrap();
            if active {
                let directory = source.root.join("workspaces/defaults/activation");
                fs::create_dir_all(&directory).unwrap();
                symlink("missing", directory.join("active.json")).unwrap();
            }
            if index {
                symlink("missing", source.root.join("profiles/index.json")).unwrap();
            }
            assert_eq!(
                read.inactive_predecessor_has_registry(source, cancel, "defaults")
                    .unwrap(),
                expected
            );
            assert!(read.receipt().verified());
            read.close_continuation().unwrap();
        });
    }
}

#[tokio::test(flavor = "current_thread")]
async fn inactive_predecessor_reads_the_original_registry_and_checks_the_fence() {
    const JOURNAL: &str = "development-host-handover.json";
    const KEY: &str = "development-host-handover.key";
    let reference = std::env::var_os("TOBKIRI_ORIGINAL_INACTIVE_REGISTRY_REFERENCE")
        .map(PathBuf::from)
        .map(|path| {
            let observation: serde_json::Value =
                serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
            assert_eq!(
                observation["format"],
                "io.tobkiri.original-inactive-registry-reference.v1"
            );
            assert_eq!(
                observation["original_head"],
                "1cf025f1eeab31a3daa2861b1f26a025c6954b6e"
            );
            let inputs = path.with_file_name("inactive-registry-inputs");
            let definition: serde_json::Value =
                serde_json::from_slice(&fs::read(inputs.join("source-profile.json")).unwrap())
                    .unwrap();
            (observation, inputs, definition)
        });
    for case in [
        "missing-row",
        "exact-mapping-equal",
        "tombstone",
        "different-row-no-handover",
        "authenticated-handover",
        "witness-without-journal",
        "handover-too-large",
        "key-too-large",
        "handover-bad-authentication",
        "handover-stale-snapshot",
        "handover-non-inert",
        "handover-became-active",
    ] {
        with_source(|source, read, cancel, budget| {
            for discard in [true, false] {
                read.enter_recovery(source, cancel, discard).unwrap();
            }
            read.acquire_candidate_root(source, cancel).unwrap();
            assert!(matches!(
                read.candidate_selection(source, cancel, "defaults", false, budget)
                    .unwrap(),
                CandidateSelection::BundledCatalog
            ));
            read.acquire_resolver_root(source, cancel).unwrap();
            read.resolver_authority_snapshot(source, cancel, Digest::hash(b"bundle lock"))
                .unwrap()
                .unwrap();
            read.bootstrap_predecessor_inputs(source, cancel, "defaults", budget)
                .unwrap();
            let profiles = source.root.join("profiles");
            let mut index = entry("defaults", 0);
            let mut packaged = index["revisions"][0]["profile"].clone();
            let original = reference
                .as_ref()
                .and_then(|(observation, inputs, definition)| {
                    observation["cases"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .find(|row| row["case"] == case)
                        .map(|row| (row, inputs.join(case).join("user-data"), definition))
                });
            let mut files = vec!["profiles/.profile-definition.lock", "profiles/index.json"];
            if let Some((row, input, definition)) = original.as_ref() {
                packaged = (**definition).clone();
                files = row["files_after"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|name| name.as_str().unwrap())
                    .collect();
                for file in &files {
                    // Use this invocation's lock file created by the preceding read.
                    if *file != "profiles/.profile-definition.lock" {
                        fs::copy(input.join(file), source.root.join(file)).unwrap();
                    }
                }
            } else {
                if case == "missing-row" {
                    index = entry("unrelated", 0);
                }
                if case == "tombstone" {
                    let prior = index["current_revision"].clone();
                    let revision = digest(&json!({
                        "schema":"io.tobkiri.profile-tombstone.v1",
                        "profile_id":"defaults","parent_revision":prior
                    }));
                    index["tombstone"] = json!(true);
                    index["current_revision"] = json!(revision);
                    index["revisions"].as_array_mut().unwrap().push(json!({
                        "profile_revision":revision,"parent_revision":prior,"profile":null,
                        "created_at":10,"updated_at":10,"tombstone":true
                    }));
                }
                if !matches!(case, "missing-row" | "exact-mapping-equal" | "tombstone") {
                    packaged["display_name"] = json!("packaged version differs");
                }
                if matches!(case, "authenticated-handover" | "handover-became-active") {
                    let profile = &mut index["revisions"][0]["profile"];
                    profile["state"] = json!("needs_resolution");
                    profile["authority_references"] = json!([]);
                    profile["requested_edges"] = json!([]);
                    profile["mode"] = json!("interactive");
                    profile["packs"] =
                        json!([{"pack_id": "fixture.second"}, {"pack_id": "fixture.first"}]);
                    profile["base"] = json!({"pack_id": "fixture.base"});
                    profile["shell"] = json!({
                        "pack_id": "fixture.shell", "provider_id": "fixture.provider",
                        "artifact_digest": null, "executable_artifact_digest": null,
                        "definition_revision": null,
                    });
                    let revision = digest(profile);
                    index["current_revision"] = json!(revision);
                    index["revisions"][0]["profile_revision"] = json!(revision);
                }
                let snapshot = sealed(state(vec![index]));
                write_index(source, &snapshot);
                if case == "witness-without-journal" {
                    fs::write(source.root.join(KEY), b"test witness").unwrap();
                    files.push(KEY);
                } else if case == "handover-too-large" {
                    fs::write(source.root.join(JOURNAL), vec![b' '; 128 * 1024 + 1]).unwrap();
                    files.push(JOURNAL);
                } else if matches!(
                    case,
                    "authenticated-handover"
                        | "key-too-large"
                        | "handover-bad-authentication"
                        | "handover-stale-snapshot"
                        | "handover-non-inert"
                        | "handover-became-active"
                ) {
                    let snapshot_digest = if case == "handover-stale-snapshot" {
                        Digest::hash(b"stale snapshot").as_str().to_owned()
                    } else {
                        digest(&serde_json::from_slice(&snapshot).unwrap())
                    };
                    let key = [7; 32];
                    let mut payload = json!({
                        "schema":"io.tobkiri.development-host-handover-journal.v1",
                        "stage":"completed","destination_profile_generation":1,
                        "destination_profile_snapshot_digest":snapshot_digest
                    });
                    let canonical = tobkiri_canon::parse(
                        &serde_json::to_vec(&payload).unwrap(),
                        tobkiri_canon::Variant::StrictV1,
                    )
                    .unwrap();
                    let signature =
                        tobkiri_canon::seal(&canonical, tobkiri_canon::Variant::StrictV1, &key)
                            .unwrap();
                    payload["authentication"] = if case == "handover-bad-authentication" {
                        json!("0".repeat(64))
                    } else {
                        json!(signature)
                    };
                    let stored_key = if case == "key-too-large" {
                        vec![7; 33]
                    } else {
                        key.to_vec()
                    };
                    fs::write(source.root.join(KEY), stored_key).unwrap();
                    fs::write(
                        source.root.join(JOURNAL),
                        serde_json::to_vec(&payload).unwrap(),
                    )
                    .unwrap();
                    files.extend([KEY, JOURNAL]);
                }
            }
            let read_files = || {
                files
                    .iter()
                    .map(|file| fs::read(source.root.join(file)).unwrap())
                    .collect::<Vec<_>>()
            };
            let before = read_files();
            let result = read.inactive_predecessor_registry(source, cancel, "defaults", budget);
            let expected = json!({"outcome":"returned", "predecessor":null});
            let actual = match case {
                "missing-row" => {
                    assert!(result.unwrap().is_none());
                    expected
                }
                "tombstone" => {
                    let Err(error) = result else {
                        panic!("inactive tombstone was admitted");
                    };
                    assert_eq!(error.message, "imported bootstrap Profile is tombstoned");
                    super::reference_ci::original_error(&error)
                }
                _ => {
                    let mut registry = result.unwrap().unwrap();
                    let selected: serde_json::Value =
                        serde_json::from_slice(registry.definition()).unwrap();
                    registry.release_serialized_definition();
                    // The fixed guest comparison is covered separately, including
                    // Python bool/int equality. The pinned fixture uses exact types.
                    assert_eq!(selected == packaged, case == "exact-mapping-equal");
                    if case == "exact-mapping-equal" {
                        expected
                    } else {
                        let active = profiles.join("active.json");
                        if case == "handover-became-active" {
                            symlink("missing", &active).unwrap();
                        }
                        let result = read.check_inactive_predecessor_handover(
                            source,
                            cancel,
                            "defaults",
                            &mut registry,
                            budget,
                        );
                        assert!(
                            read.check_inactive_predecessor_handover(
                                source,
                                cancel,
                                "defaults",
                                &mut registry,
                                budget,
                            )
                            .is_err()
                        );
                        match case {
                            "different-row-no-handover" => {
                                assert!(result.unwrap().is_none());
                                expected
                            }
                            "authenticated-handover" => {
                                let predecessor =
                                    result.unwrap().unwrap().encode_guest(budget).unwrap();
                                let predecessor: serde_json::Value =
                                    serde_json::from_slice(predecessor.as_bytes()).unwrap();
                                if original.is_none() {
                                    assert_eq!(
                                        predecessor["pack_ids"],
                                        json!(["fixture.second", "fixture.first"])
                                    );
                                    assert!(predecessor["shell_definition_revision"].is_null());
                                }
                                json!({"outcome": "returned", "predecessor": predecessor})
                            }
                            _ => {
                                let message = match case {
                                    "witness-without-journal" => {
                                        "Development Host handover requires its authenticated recovery journal"
                                    }
                                    "handover-too-large" | "key-too-large" => {
                                        "persistence entry exceeds read limit"
                                    }
                                    "handover-bad-authentication" => {
                                        "Development Host handover journal authentication failed"
                                    }
                                    "handover-stale-snapshot" => {
                                        "imported bootstrap registry changed after completed handover"
                                    }
                                    "handover-non-inert" => {
                                        "bootstrap import is not inert definition data"
                                    }
                                    "handover-became-active" => {
                                        assert_eq!(
                                            fs::read_link(active).unwrap(),
                                            Path::new("missing")
                                        );
                                        "inactive bootstrap predecessor has an active Profile"
                                    }
                                    _ => unreachable!(),
                                };
                                let error = result.unwrap_err();
                                assert_eq!(error.message, message);
                                super::reference_ci::original_error(&error)
                            }
                        }
                    }
                }
            };
            if let Some((row, _, _)) = original.as_ref() {
                assert!(row["files_unchanged"].as_bool().unwrap());
                assert_eq!(row["expected"], actual);
            }
            assert_eq!(read_files(), before);
            assert!(lock_free(&profiles.join(".profile-definition.lock")));
            assert!(
                read.inactive_predecessor_registry(source, cancel, "defaults", budget)
                    .is_err()
            );
            read.close_continuation().unwrap();
        });
    }
}

const DATABASE: &str = "authority/v4.sqlite3";
const GUARD: &str = "authority/.v4.sqlite3.lifecycle.lock";
const KEY: &str = "authority/v4.key";

fn selected_setup(
    read: &mut PublicationRead,
    source: &impl crate::BootstrapReviewOwner,
    cancel: &CoreCancellation,
    additions: bool,
    budget: &Arc<dyn AllocationBudget>,
) {
    for discard in [true, false] {
        assert_eq!(
            read.enter_recovery(source, cancel, discard).unwrap(),
            Prefix::NoJournal
        );
    }
    read.acquire_candidate_root(source, cancel).unwrap();
    assert!(matches!(
        read.candidate_selection(source, cancel, "defaults", additions, budget)
            .unwrap(),
        CandidateSelection::ActiveAuthority
    ));
}

fn database(root: &Path) -> Connection {
    Connection::open_with_flags(root.join(DATABASE), OpenFlags::SQLITE_OPEN_READ_WRITE).unwrap()
}

fn meta(db: &Connection, key: &str) -> String {
    db.query_row(
        "SELECT value FROM authority_meta WHERE key=?1",
        [key],
        |row| row.get(0),
    )
    .unwrap()
}

fn seed_authority(source: &Source, read: &PublicationRead, cancel: &CoreCancellation) {
    let op = Operation {
        owner: source,
        invocation: &read.invocation,
        cancellation: cancel,
        deadline: read.invocation.deadline().unwrap(),
        pid: std::process::id(),
        cleanup: read.cleanup.clone(),
        fixture: None,
    };
    super::super::super::with_registered_store(&op, &source.root, |_| Ok(())).unwrap();
}

#[tokio::test(flavor = "current_thread")]
async fn registered_authority_initializes_genesis_on_the_held_root_before_the_body() {
    for additions in [false, true] {
        with_setup_source(|source, read, cancel, budget| {
            seed_candidate(source, true);
            let other = source.root.with_file_name("other-user-data");
            let owner = ChangingOwner {
                first: &source.root,
                next: &other,
                calls: AtomicUsize::new(0),
            };
            selected_setup(read, &owner, cancel, additions, budget);
            assert!(!source.root.join("authority").exists());
            assert!(read.receipt().verified());
            let reached = read
                .with_registered_activation_authority(&owner, cancel, |scope| {
                    assert_eq!(scope.workspace, source.root.join("workspaces/defaults"));
                    assert_eq!(scope.activation_root, scope.workspace.join("activation"));
                    assert_eq!(scope.profile_id, "defaults");
                    assert_eq!(
                        scope.authority_observation(),
                        (source.root.join(DATABASE).as_path(), false)
                    );
                    let db = database(&source.root);
                    assert_eq!(meta(&db, "schema_version"), "4");
                    assert_eq!(meta(&db, "security_epoch"), "1");
                    assert_eq!(meta(&db, "activation_fencing_token"), "0");
                    assert!(
                        meta(&db, "security_epoch_advanced_at")
                            .parse::<f64>()
                            .unwrap()
                            .is_finite()
                    );
                    assert_eq!(
                        meta(&db, "security_epoch_reason_digest"),
                        Digest::hash(br#"{"reason":"genesis"}"#).as_str()
                    );
                    assert_eq!(
                        db.query_row(
                            "SELECT count(*) FROM host_extension_registrations",
                            [],
                            |row| row.get::<_, i64>(0)
                        )
                        .unwrap(),
                        0
                    );
                    db.close().unwrap();
                    Ok(7)
                })
                .unwrap();
            assert_eq!(reached, 7);
            assert_eq!(owner.calls.load(Ordering::Relaxed), 3);
            assert!(!other.exists());
            assert!(
                read.with_registered_activation_authority::<_, ()>(&owner, cancel, |_| {
                    panic!("registered Authority was entered twice")
                })
                .is_err()
            );
            assert!(read.acquire_resolver_root(&owner, cancel).is_err());
            assert!(read.receipt().verified());
            assert!(lock_free(&source.root.join(GUARD)));
            for (path, mode) in [
                ("authority", 0o700),
                (DATABASE, 0o600),
                (GUARD, 0o600),
                (KEY, 0o600),
            ] {
                assert_eq!(
                    fs::metadata(source.root.join(path))
                        .unwrap()
                        .permissions()
                        .mode()
                        & 0o777,
                    mode
                );
            }
            assert_eq!(
                fs::read_dir(source.root.join("authority")).unwrap().count(),
                3
            );
            read.close_continuation().unwrap();
        });
    }
}

#[tokio::test(flavor = "current_thread")]
async fn registered_authority_keeps_bad_epoch_metadata_and_propagates_body_error() {
    for close_fault in [false, true] {
        with_setup_source(|source, read, cancel, budget| {
            seed_candidate(source, true);
            // Seed via the established constructor, then set epoch 2 with
            // the malformed reason that a snapshot reader would validate.
            seed_authority(source, read, cancel);
            let db = database(&source.root);
            db.execute(
                "UPDATE authority_meta SET value='2' WHERE key='security_epoch'",
                [],
            )
            .unwrap();
            db.execute(
                "UPDATE authority_meta SET value='invalid' WHERE key='security_epoch_reason_digest'",
                [],
            )
            .unwrap();
            db.close().unwrap();
            let key = fs::read(source.root.join(KEY)).unwrap();
            selected_setup(read, source, cancel, false, budget);
            fn body(scope: RegisteredActivationScope<'_, '_, '_, Source>) -> Result<()> {
                assert!(!scope.authority_observation().1);
                Err(OriginalAuthorityError::new(
                    OriginalAuthorityStage::Source,
                    "test activation factory error",
                    None,
                ))
            }
            let error = if close_fault {
                let close = read
                    .registered_activation_authority_close_fault::<_, ()>(source, cancel, body)
                    .unwrap_err();
                assert_eq!(close.stage, OriginalAuthorityStage::Close);
                assert_eq!(close.class, "builtins.RuntimeError");
                assert!(!close.suppressed);
                assert!(close.cause.is_none());
                *close.context.expect("actual body error displaced by close")
            } else {
                read.with_registered_activation_authority::<_, ()>(source, cancel, body)
                    .unwrap_err()
            };
            assert_eq!(error.message, "test activation factory error");
            assert!(error.context.is_none());
            assert!(read.receipt().verified());
            assert!(lock_free(&source.root.join(GUARD)));
            assert_eq!(fs::read(source.root.join(KEY)).unwrap(), key);
            let db = database(&source.root);
            assert_eq!(meta(&db, "security_epoch"), "2");
            assert_eq!(meta(&db, "security_epoch_reason_digest"), "invalid");
            db.close().unwrap();
            read.close_continuation().unwrap();
        });
    }
}

#[tokio::test(flavor = "current_thread")]
async fn registered_authority_constructor_errors_stop_before_the_body() {
    for case in ["directory", "dangling", "missing-key"] {
        with_setup_source(|source, read, cancel, budget| {
            seed_candidate(source, true);
            let path = source.root.join(DATABASE);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            let mut existing = None;
            match case {
                "directory" => fs::create_dir(&path).unwrap(),
                "dangling" => symlink("missing.sqlite3", &path).unwrap(),
                "missing-key" => {
                    seed_authority(source, read, cancel);
                    existing = Some(Digest::hash(&fs::read(&path).unwrap()));
                    let missing_key = source.root.with_file_name("prior-authority-key");
                    fs::rename(source.root.join(KEY), missing_key).unwrap();
                }
                _ => unreachable!(),
            }
            selected_setup(read, source, cancel, true, budget);
            let error = read
                .with_registered_activation_authority::<_, ()>(source, cancel, |_| {
                    panic!("constructor error reached activation factory body")
                })
                .unwrap_err();
            assert_eq!(
                error.class,
                "core_runtime.authority.store_contracts.AuthorityStoreError"
            );
            if case == "missing-key" {
                assert_eq!(error.stage, OriginalAuthorityStage::Key);
                assert_eq!(
                    error.message,
                    "authority encryption key is missing for an existing database"
                );
                assert!(error.cause.is_none());
                assert!(!error.suppressed);
                assert_eq!(existing, Some(Digest::hash(&fs::read(&path).unwrap())));
                assert!(lock_free(&source.root.join(GUARD)));
            } else {
                assert_eq!(error.stage, OriginalAuthorityStage::Path);
                assert_eq!(error.message, "authority state path is unsafe");
                assert!(matches!(
                    error.cause,
                    Some(OriginalAuthorityCause::SecurePath {
                        message: "file is not regular",
                        context: None
                    })
                ));
                assert!(error.suppressed);
                assert_eq!(path.is_symlink(), case == "dangling");
                assert_eq!(path.is_dir(), case == "directory");
                assert!(!source.root.join(GUARD).exists());
            }
            assert!(error.context.is_none());
            assert!(!source.root.join(KEY).exists());
            assert!(read.receipt().verified());
            assert!(
                read.with_registered_activation_authority::<_, ()>(source, cancel, |_| {
                    panic!("constructor failure permitted another attempt")
                })
                .is_err()
            );
            read.close_continuation().unwrap();
        });
    }
}
