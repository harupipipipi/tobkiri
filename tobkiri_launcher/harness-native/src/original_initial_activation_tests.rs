use super::*;
use crate::original_authority::catalog::initial_catalog_fixture;
use crate::original_authority::definitions::tests::{
    Source, lock_free, operation, with_setup_source,
};
use rusqlite::Connection;
use serde_json::Value;

#[derive(Clone, Copy, PartialEq, Eq)]
enum Outcome {
    Published,
    MissingShell,
    ConflictingDefinition,
}

#[tokio::test(flavor = "current_thread")]
#[ignore = "requires the original approved Profile records and physical bundled Shell"]
async fn commits_confirmed_initial_publication_to_the_host_pointer() {
    let manifest =
        PathBuf::from(std::env::var_os("TOBKIRI_ORIGINAL_INACTIVE_REGISTRY_REFERENCE").unwrap());
    let rows: Value = serde_json::from_slice(&fs::read(&manifest).unwrap()).unwrap();
    let imported = rows["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["case"] == "authenticated-handover")
        .unwrap();
    let checks = imported["publication_checks"].as_array().unwrap();
    let release = manifest.with_file_name("shell-release");
    let fixtures = manifest.with_file_name("inactive-registry-inputs");
    for (case, outcome) in [
        ("matched", Outcome::Published),
        ("matched-with-predecessor", Outcome::Published),
        ("matched-with-predecessor", Outcome::MissingShell),
        ("matched-with-predecessor", Outcome::ConflictingDefinition),
    ] {
        let row = checks.iter().find(|row| row["case"] == case).unwrap();
        let intent = &row["outcome"]["candidate"]["intent"]["payload"];
        let witness = fixtures.join(format!("publication-{case}"));
        let approved = row
            .get("confirmation")
            .unwrap_or(&imported["setup_review"]["confirmation"]);
        assert_eq!(
            approved.get("definition_predecessor").is_some(),
            case == "matched"
        );
        with_setup_source(|owner, read, cancellation, budget| {
            let root = &owner.root;
            seed(
                root,
                &witness.join("user-data"),
                &intent["predecessor"],
                case == "matched" || outcome == Outcome::ConflictingDefinition,
            );
            let approved =
                CoreValue::parse_guest(&serde_json::to_vec(approved).unwrap(), budget).unwrap();
            let original_resolved = load_document(&witness.join("resolved-records.json"), budget);
            let definition = load_document(&witness.join("reread-source.json"), budget);
            let expected = &intent["candidate"];
            let digest = |key| Digest::parse(expected[key].as_str().unwrap()).unwrap();
            let definition_digest = digest("profile_definition_digest");
            let (candidate, resolved) = BootstrapActivationCandidate::fixture(
                approved.share(),
                definition.share(),
                original_resolved.share(),
                Digest::parse(row["reread_bundle_digest"].as_str().unwrap()).unwrap(),
                [
                    definition_digest.clone(),
                    digest("plan_digest"),
                    digest("lock_digest"),
                ],
                budget,
            )
            .unwrap();
            assert_eq!(
                read.begin_confirmed(owner, cancellation, approved, budget)
                    .unwrap(),
                Prefix::NoIntent
            );
            let op = operation(read, owner, cancellation);
            let missing_release = root.join("empty-artifacts");
            let artifacts = if outcome == Outcome::MissingShell {
                fs::create_dir(&missing_release).unwrap();
                &missing_release
            } else {
                &release
            };
            let loader = Mutex::new(Some(
                initial_catalog_fixture::load(&op, budget, &witness, artifacts).unwrap(),
            ));
            let activated = ready_test(read.activate_initial(
                owner,
                cancellation,
                budget,
                InitialPublication {
                    candidate: &candidate,
                    resolved: &resolved,
                    source_profile: &definition,
                    loader: &loader,
                    schemas: workspace_active::tests::bound_recovery_schemas(),
                },
            ));
            let profiles = root.join("profiles");
            let lock = profiles.join(LOCK);
            assert!(!lock_free(&lock));
            if outcome != Outcome::Published {
                let error = activated.unwrap_err();
                let journal = read_intent(&op, &profiles).unwrap().unwrap();
                verify_recovery_confirmation(&journal, candidate.confirmation(), budget).unwrap();
                assert_eq!(
                    fs::read(profiles.join("active.json")).unwrap(),
                    fs::read(witness.join("user-data/profiles/active.json")).unwrap()
                );
                let workspace = root.join("workspaces/defaults/activation");
                assert!(
                    !workspace.join("active.json").exists()
                        && !workspace.join("pending.json").exists()
                );
                if outcome == Outcome::MissingShell {
                    assert_eq!(error.class, recovery_workspace::APP_DENIED);
                    assert_eq!(
                        error.message,
                        "active Profile Shell artifact identity is unavailable"
                    );
                    assert!(workspace.is_dir());
                    let db = Connection::open(root.join("authority/v4.sqlite3")).unwrap();
                    let reserved: i64 = db
                        .query_row("SELECT COUNT(*) FROM activation_reservations", [], |row| {
                            row.get(0)
                        })
                        .unwrap();
                    assert_eq!(reserved, 0);
                } else {
                    assert_eq!(
                        error.class,
                        "core_runtime.profile_definition_store_v4.ProfileDefinitionStoreConflict"
                    );
                    assert_eq!(
                        error.message,
                        "bootstrap Profile conflicts with the existing definition"
                    );
                    assert!(!root.join("authority").exists());
                    assert!(!workspace.exists());
                }
                drop((journal, loader));
                read.close_continuation().unwrap();
                assert!(lock_free(&lock));
                return;
            }
            activated.unwrap_or_else(|error| panic!("{case}: {error:?}"));
            assert!(!root.join("profiles").join(JOURNAL).exists());
            assert!(root.join("profiles").join(KEY).is_file());
            let active = active_profile::load_verified(&op, root, budget)
                .unwrap()
                .unwrap();
            assert_eq!(active.profile_id().utf8().unwrap(), "defaults");
            let activation_id = expected["activation_id"].as_str().unwrap();
            assert_eq!(
                active.pointer.text(&active.doc, "activation_id").unwrap(),
                activation_id
            );
            let generation = intent["predecessor"]["generation"].as_i64().unwrap_or(0) + 1;
            assert_eq!(
                active.pointer.integer(&active.doc, "generation"),
                generation
            );
            let index: Value =
                serde_json::from_slice(&fs::read(root.join("profiles/index.json")).unwrap())
                    .unwrap();
            let registered = index["profiles"]
                .as_array()
                .unwrap()
                .iter()
                .find(|row| row["profile_id"] == "defaults")
                .unwrap();
            assert_eq!(
                registered["current_revision"].as_str(),
                Some(definition_digest.as_str())
            );
            assert!(
                !root
                    .join("workspaces/defaults/activation/pending.json")
                    .exists()
            );
            let db = Connection::open(root.join("authority/v4.sqlite3")).unwrap();
            let state: String = db
                .query_row(
                    "SELECT state FROM activation_reservations WHERE activation_id=?",
                    [activation_id],
                    |row| row.get(0),
                )
                .unwrap();
            assert_eq!(state, "active");
            drop((active, db, loader));
            read.close_continuation().unwrap();
            assert!(lock_free(&lock));

            let host_pointer = fs::read(profiles.join("active.json")).unwrap();
            let workspace = root.join("workspaces/defaults/activation");
            let workspace_pointer = fs::read(workspace.join("active.json")).unwrap();
            let check_snapshot = |witness: &Path, successor_required: bool| {
                let RegisteredSnapshotReached::CatalogPolicy(records) =
                    read_registered(owner, read, cancellation, budget, witness, artifacts).unwrap()
                else {
                    panic!("expected the authenticated catalog policy inputs");
                };
                let assert_record = |actual: &CoreValue, expected: &CoreValue| {
                    assert_eq!(
                        actual.encode_guest(budget).unwrap().as_bytes(),
                        expected.encode_guest(budget).unwrap().as_bytes()
                    );
                };
                assert_record(&records.registered_profile, &definition);
                assert_record(
                    &records.active_profile,
                    &original_resolved.get_key("profile").unwrap(),
                );
                assert_eq!(
                    records.verified_definition_digest.as_str(),
                    Some(definition_digest.as_str())
                );
                if successor_required {
                    assert!(records.active_plan.is_none());
                } else {
                    assert_record(
                        &records.active_plan.unwrap(),
                        &original_resolved.get_key("plan").unwrap(),
                    );
                }
            };
            check_snapshot(&witness, false);
            let bundled_successor = root.join("successor-catalog");
            seed_successor_catalog(&witness, &bundled_successor);
            check_snapshot(&bundled_successor, true);
            assert_eq!(
                fs::read(profiles.join("active.json")).unwrap(),
                host_pointer
            );
            replace_host_selection(root, &host_pointer, budget);
            let predecessor = fs::read(profiles.join("active.json")).unwrap();
            let error = read_registered(owner, read, cancellation, budget, &witness, artifacts)
                .unwrap_err();
            assert_eq!(
                error.class,
                "core_runtime.profile_definition_store_v4.ProfileDefinitionStoreConflict",
            );
            assert_eq!(
                error.message,
                "bootstrap review does not match the verified active definition"
            );
            assert_eq!(fs::read(profiles.join("active.json")).unwrap(), predecessor);
            assert_eq!(
                fs::read(workspace.join("active.json")).unwrap(),
                workspace_pointer
            );
            assert!(!profiles.join(JOURNAL).exists());
            assert!(lock_free(
                &workspace.join(LockName::workspace("defaults").text())
            ));
        });
    }
}

fn seed_successor_catalog(original: &Path, successor: &Path) {
    fs::create_dir(successor).unwrap();
    let revision = Value::from(Digest::hash(b"successor shell definition").to_string());
    // Retain the physical artifact and Pack pin while independently changing
    // the bundled Shell definition revision that triggers reconfirmation.
    for (name, shell_in_profile) in [("reread-source.json", true), ("reread-shell.json", false)] {
        let mut record: Value =
            serde_json::from_slice(&fs::read(original.join(name)).unwrap()).unwrap();
        let shell = if shell_in_profile {
            &mut record["shell"]
        } else {
            &mut record
        };
        shell["definition_revision"] = revision.clone();
        fs::write(successor.join(name), serde_json::to_vec(&record).unwrap()).unwrap();
    }
    fs::copy(
        original.join("reread-pack.json"),
        successor.join("reread-pack.json"),
    )
    .unwrap();
}

fn read_registered(
    owner: &Source,
    read: &PublicationRead,
    cancellation: &CoreCancellation,
    budget: &Arc<dyn AllocationBudget>,
    witness: &Path,
    artifacts: &Path,
) -> Result<RegisteredSnapshotReached> {
    let mut listing = PublicationRead::new(&read.invocation).unwrap();
    for discard in [true, false] {
        assert_eq!(
            listing
                .enter_recovery(owner, cancellation, discard)
                .unwrap(),
            Prefix::NoJournal
        );
    }
    listing.acquire_candidate_root(owner, cancellation).unwrap();
    assert!(matches!(
        listing
            .candidate_selection(owner, cancellation, "defaults", false, budget)
            .unwrap(),
        CandidateSelection::ActiveAuthority
    ));
    let op = operation(&listing, owner, cancellation);
    let loader = Mutex::new(Some(
        initial_catalog_fixture::load(&op, budget, witness, artifacts).unwrap(),
    ));
    let outcome = ready_test(listing.review_registered_activation(
        owner,
        cancellation,
        budget,
        &loader,
        Some(workspace_active::tests::bound_recovery_schemas()),
    ));
    if let Ok(RegisteredSnapshotReached::CatalogPolicy(records)) = &outcome {
        // This publication fixture exports only the Shell Pack manifest.
        // That retained pin cannot substitute for the missing active closure.
        assert_eq!(
            listing
                .review_registered_catalog(owner, cancellation, budget, &loader, records)
                .unwrap(),
            RegisteredCatalogSelection::AdmittedClosure
        );
    }
    assert!(listing.receipt().verified());
    outcome
}

fn replace_host_selection(root: &Path, predecessor: &[u8], budget: &Arc<dyn AllocationBudget>) {
    let mut pointer: Value = serde_json::from_slice(predecessor).unwrap();
    let prior = root.join(pointer["activation_snapshot_path"].as_str().unwrap());
    let mut snapshot: Value = serde_json::from_slice(&fs::read(prior).unwrap()).unwrap();
    let other_id = "activation:defaults-other";
    snapshot["activation"]["activation_id"] = Value::from(other_id);
    let relative = "workspaces/defaults/activation/activations/defaults-other.json";
    let payload = serde_json::to_vec(&snapshot).unwrap();
    let digest = |payload: &[u8]| {
        CoreValue::parse_guest(payload, budget)
            .unwrap()
            .digest_guest(budget)
            .unwrap()
            .to_string()
    };
    let path = root.join(relative);
    fs::write(&path, &payload).unwrap();
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(0o600)).unwrap();
    pointer["activation_id"] = Value::from(other_id);
    pointer["activation_snapshot_path"] = Value::from(relative);
    pointer["activation_snapshot_digest"] = Value::from(digest(&payload));
    pointer.as_object_mut().unwrap().remove("pointer_digest");
    pointer["pointer_digest"] = Value::from(digest(&serde_json::to_vec(&pointer).unwrap()));
    fs::write(
        root.join("profiles/active.json"),
        serde_json::to_vec(&pointer).unwrap(),
    )
    .unwrap();
}

fn load_document(path: &Path, budget: &Arc<dyn AllocationBudget>) -> CoreValue {
    let data = fs::read(path).unwrap();
    assert!(data.len() < tobkiri_canon::MAX_BYTES);
    CoreValue::parse_guest(&data, budget).unwrap()
}

fn seed(root: &Path, source: &Path, predecessor: &Value, definition_exists: bool) {
    let copy = |relative: &Path| {
        assert!(
            relative
                .components()
                .all(|segment| matches!(segment, std::path::Component::Normal(_)))
        );
        let target = root.join(relative);
        fs::create_dir_all(target.parent().unwrap()).unwrap();
        fs::copy(source.join(relative), target).unwrap();
    };
    for name in [
        "development-host-handover.key",
        "development-host-handover.json",
    ] {
        copy(Path::new(name));
    }
    // The original oracle stops before registration. Its prior-pointer case
    // can also exercise ordinary create with no stored definition for defaults.
    if definition_exists {
        copy(Path::new("profiles/index.json"));
    }
    if let Some(record) = predecessor["activation_snapshot_path"].as_str() {
        copy(Path::new("profiles/active.json"));
        assert!(record.starts_with("workspaces/prior-profile/activation/activations/"));
        copy(Path::new(record));
    }
}
