use super::*;
use crate::original_authority::catalog::initial_catalog_fixture;
use crate::original_authority::definitions::tests::{lock_free, operation, with_setup_source};
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
            let resolved = load_document(&witness.join("resolved-records.json"), budget);
            let definition = load_document(&witness.join("reread-source.json"), budget);
            let expected = &intent["candidate"];
            let digest = |key| Digest::parse(expected[key].as_str().unwrap()).unwrap();
            let definition_digest = digest("profile_definition_digest");
            let (candidate, resolved) = BootstrapActivationCandidate::fixture(
                approved.share(),
                definition.share(),
                resolved,
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
        });
    }
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
