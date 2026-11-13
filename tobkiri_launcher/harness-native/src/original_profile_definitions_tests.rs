//! Real registry/lock mechanics on a controlled portable source. These tests
//! are not actual AppHandle, Guest factory or full Setup qualification.
use super::*;
use serde_json::{Value, json};
use std::os::fd::AsRawFd;
use std::os::unix::fs::PermissionsExt;
use tobkiri_gate::{CapacityLedger, CoreControl, GateError, NativeSetupLease};

struct Portable;
impl NativeSetupLease for Portable {
    fn validate(&self) -> std::result::Result<(), GateError> {
        Ok(())
    }
    fn allocation_domain(&self) -> Digest {
        Digest::hash(b"registry-test-only-not-source-authority")
    }
}
pub(super) struct Source {
    pub(super) root: PathBuf,
}
impl OriginalAuthorityOwner for Source {
    fn with_original_user_data<R>(&self, f: impl FnOnce(&Path) -> R) -> Result<R> {
        Ok(f(&self.root))
    }
    fn check_original_owner(&self) -> Result<()> {
        Ok(())
    }
}
impl crate::BootstrapReviewOwner for Source {
    fn with_original_review_user_data<R>(&self, f: impl FnOnce(&Path) -> R) -> Result<R> {
        Ok(f(&self.root))
    }
}
pub(super) fn with_source(
    run: impl FnOnce(&Source, &mut PublicationRead, &CoreCancellation, &Arc<dyn AllocationBudget>),
) {
    with_setup_source(|source, read, cancellation, budget| {
        run(source, read, cancellation, budget);
        assert!(!source.root.join("authority").exists());
    });
}
pub(super) fn with_setup_source(
    run: impl FnOnce(&Source, &mut PublicationRead, &CoreCancellation, &Arc<dyn AllocationBudget>),
) {
    let temporary = tempfile::tempdir().unwrap();
    let source = Source {
        root: temporary.path().canonicalize().unwrap().join("user-data"),
    };
    let control = CoreControl::default();
    let account = CapacityLedger::new()
        .native_setup_account(&control, || Ok(Arc::new(Portable)))
        .unwrap();
    let invocation = account.begin().unwrap();
    let engine = tobkiri_wasm::WasmEngine::new_with_control(&control).unwrap();
    let store = engine.native_setup_store(invocation.clone(), ()).unwrap();
    let cancellation = store.cancellation().clone();
    let budget = store.allocation_owners().graph().clone();
    let mut read = PublicationRead::new(&invocation).unwrap();
    run(&source, &mut read, &cancellation, &budget);
    assert!(read.receipt().verified());
    // Every owned diagnostic produced by the test body is gone before Read.
    drop((read, budget, store, cancellation, invocation, engine));
    tobkiri_wasm::reap_allocation_owners().unwrap();
    let remaining = account.snapshot().unwrap();
    assert_eq!((remaining.charged_bytes, remaining.reservations), (0, 0));
}
fn operation<'a>(
    read: &'a PublicationRead,
    source: &'a Source,
    cancel: &'a CoreCancellation,
) -> Operation<'a, Source> {
    Operation {
        owner: source,
        invocation: &read.invocation,
        cancellation: cancel,
        deadline: read.invocation.deadline().unwrap(),
        pid: std::process::id(),
        cleanup: read.cleanup.clone(),
        fixture: None,
    }
}
pub(super) fn digest(value: &Value) -> String {
    let data = serde_json::to_vec(value).unwrap();
    let value = tobkiri_canon::parse(&data, tobkiri_canon::Variant::StrictV1).unwrap();
    tobkiri_canon::digest(&value, tobkiri_canon::Variant::StrictV1)
        .unwrap()
        .as_str()
        .to_owned()
}
pub(super) fn entry(id: &str, order: i64) -> Value {
    let profile = json!({"profile_id":id,"display_name":id});
    let revision = digest(&profile);
    json!({"profile_id":id,"order":order,"tombstone":false,"current_revision":revision,
        "revisions":[{"profile_revision":revision,"parent_revision":null,"profile":profile,
            "created_at":10,"updated_at":10,"tombstone":false}],
        "created_at":10,"updated_at":10,"legacy_ids":[]})
}
pub(super) fn state(profiles: Vec<Value>) -> Value {
    json!({"schema":"io.tobkiri.profile-definition-store.v1","generation":1,"updated_at":10,
        "profiles":profiles,"legacy":{},"bootstrap":{"state":"not_required","template_profile_revision":null}})
}
pub(super) fn sealed(mut state: Value) -> Vec<u8> {
    state.as_object_mut().unwrap().remove("store_digest");
    let hash = digest(&state);
    state["store_digest"] = json!(hash);
    serde_json::to_vec(&state).unwrap()
}
pub(super) fn write_index(source: &Source, bytes: &[u8]) {
    let profiles = source.root.join("profiles");
    fs::create_dir_all(&profiles).unwrap();
    fs::write(profiles.join(INDEX), bytes).unwrap();
    fs::set_permissions(profiles.join(INDEX), fs::Permissions::from_mode(0o600)).unwrap();
}
pub(super) fn lock_free(path: &Path) -> bool {
    let file = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(path)
        .unwrap();
    if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
        return false;
    }
    assert_eq!(unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_UN) }, 0);
    true
}

#[tokio::test(flavor = "current_thread")]
async fn registry_constructor_and_first_list_create_only_original_directories_and_lock() {
    with_source(|source, read, cancel, budget| {
        assert_eq!(
            run(read, source, cancel, budget).unwrap(),
            DefinitionPrefix::MigrateLegacySuccessorsNotBound
        );
        let profiles = source.root.join("profiles");
        assert_eq!(
            fs::metadata(&profiles).unwrap().permissions().mode() & 0o777,
            0o700
        );
        assert_eq!(
            fs::metadata(profiles.join(".profile-definition.lock"))
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o600
        );
        assert!(!profiles.join(INDEX).exists());
        assert!(lock_free(&profiles.join(".profile-definition.lock")));
        fs::create_dir(source.root.join("settings")).unwrap();
        fs::write(
            source.root.join("settings/startup_profiles.json"),
            b"unread at this boundary",
        )
        .unwrap();
        assert_eq!(
            run(read, source, cancel, budget).unwrap(),
            DefinitionPrefix::ImportLegacyNotBound
        );
    });
}

#[tokio::test(flavor = "current_thread")]
async fn registry_definition_lock_is_distinct_inside_the_actual_publication_lock() {
    with_source(|source, read, cancel, budget| {
        let op = operation(read, source, cancel);
        let profiles = source.root.join("profiles");
        let directory = Directory::capture(&op, &profiles, true).unwrap();
        let mut publication = PublicationLock::acquire(&op, &directory).unwrap();
        assert!(!lock_free(&profiles.join(LOCK)));
        assert_eq!(
            run(read, source, cancel, budget).unwrap(),
            DefinitionPrefix::MigrateLegacySuccessorsNotBound
        );
        assert!(lock_free(&profiles.join(".profile-definition.lock")));
        assert!(!lock_free(&profiles.join(LOCK)));
        publication.close().unwrap();
        assert!(lock_free(&profiles.join(LOCK)));
    });
}

#[tokio::test(flavor = "current_thread")]
async fn registry_recovery_rereads_successors_with_the_publication_lock_held() {
    with_source(|source, read, cancel, budget| {
        let op = operation(read, source, cancel);
        let profiles = source.root.join("profiles");
        let directory = Directory::capture(&op, &profiles, true).unwrap();
        let mut publication = PublicationLock::acquire(&op, &directory).unwrap();
        let definition_lock = profiles.join(".profile-definition.lock");

        let store = first_list(&op, &source.root, budget).unwrap().unwrap();
        assert!(lock_free(&definition_lock));
        assert!(matches!(
            store.select_successor(&op, budget).unwrap(),
            DefinitionRecovery::RepairLegacyDisplayNames
        ));

        // Another definition writer can publish after the first list releases
        // its lock. The migration must see this later index on the same Store.
        let mut profile = entry("opaque", 0);
        profile["legacy_ids"] = json!(["original"]);
        let mut value = state(vec![profile]);
        value["legacy"] = json!({
            "source_document": {"profiles": {"original": {"legacy_only": true}}},
            "source_path": " exact original "
        });
        let bytes = sealed(value);
        write_index(source, &bytes);
        let DefinitionRecovery::SuccessorBuilder(candidate) =
            store.select_successor(&op, budget).unwrap()
        else {
            panic!("the later index must reach the successor builder");
        };
        assert!(
            candidate
                ._state
                .doc
                .string_is(candidate._profile_id, "opaque")
        );
        assert!(
            candidate
                ._state
                .doc
                .get(candidate._item, "legacy_only")
                .is_some()
        );
        assert_eq!(
            candidate._item_origin,
            definitions_model::SuccessorItemOrigin::LegacySource
        );
        assert_eq!(candidate._source_path.as_str(), " exact original ");
        assert_eq!(fs::read(profiles.join(INDEX)).unwrap(), bytes);
        assert!(lock_free(&definition_lock));
        assert!(!lock_free(&profiles.join(LOCK)));
        drop(candidate);
        publication.close().unwrap();
        assert!(lock_free(&profiles.join(LOCK)));
    });
}

#[tokio::test(flavor = "current_thread")]
async fn registry_list_validates_history_sorts_and_keeps_legacy_bootstrap_upgrade_in_memory() {
    with_source(|source, read, cancel, budget| {
        let mut value = state(vec![
            entry("beta", 2),
            entry("alpha", 2),
            entry("earlier", 1),
        ]);
        value.as_object_mut().unwrap().remove("bootstrap");
        let bytes = sealed(value);
        write_index(source, &bytes);
        let op = operation(read, source, cancel);
        let mut model = definitions_model::load(&op, Some(&bytes), budget).unwrap();
        assert!(model.doc.actual_storage_bytes() <= model.doc.storage_bound());
        let rows = definitions_model::list(&op, &mut model, budget).unwrap();
        let ids: Vec<_> = rows
            .value
            .iter()
            .map(|row| match model.doc.node(row.references[0]) {
                Node::String(text) => text.utf8().unwrap(),
                _ => panic!("validated ID"),
            })
            .collect();
        assert_eq!(ids, ["earlier", "alpha", "beta"]);
        assert!(model.doc.actual_storage_bytes() <= model.doc.storage_bound());
        drop(rows);
        drop(model);
        assert_eq!(
            run(read, source, cancel, budget).unwrap(),
            DefinitionPrefix::MigrateLegacySuccessorsNotBound
        );
        assert_eq!(
            fs::read(source.root.join("profiles").join(INDEX)).unwrap(),
            bytes
        );
    });
}

#[tokio::test(flavor = "current_thread")]
async fn registry_rehashed_history_and_unicode_fail_at_the_original_validation_step() {
    with_source(|source, read, cancel, budget| {
        let mut value = state(vec![entry("alpha", 0)]);
        value["profiles"][0]["revisions"][0]["profile"]["display_name"] = json!("tampered");
        write_index(source, &sealed(value));
        let error = run(read, source, cancel, budget).unwrap_err();
        assert_eq!(
            (error.class, error.message.as_str()),
            (INTEGRITY, "Profile revision digest changed")
        );
        drop(error);
        write_index(source, &sealed(state(vec![entry("e\u{301}", 0)])));
        let error = run(read, source, cancel, budget).unwrap_err();
        assert_eq!(error.class, INTEGRITY);
        assert_eq!(error.message, "profile_id is not Unicode-normalized");
        assert!(matches!(
            &error.cause,
            Some(OriginalAuthorityCause::Python {
                class: "tobkiri_protocol.errors.ProtocolError",
                ..
            })
        ));
    });
}

#[tokio::test(flavor = "current_thread")]
async fn registry_tombstones_keep_the_prior_document_and_check_parent_before_digest() {
    with_source(|source, read, cancel, budget| {
        let mut profile = entry("alpha", 0);
        let parent = profile["current_revision"].clone();
        let tombstone = digest(&json!({"schema":"io.tobkiri.profile-tombstone.v1",
            "profile_id":"alpha","parent_revision":parent}));
        profile["revisions"].as_array_mut().unwrap().push(json!({
            "profile_revision":tombstone,"parent_revision":parent,"profile":null,
            "created_at":11,"updated_at":11,"tombstone":true}));
        profile["current_revision"] = json!(tombstone);
        profile["tombstone"] = json!(true);
        let beta = entry("beta", 2);
        let bytes = sealed(state(vec![profile.clone(), beta.clone()]));
        let op = operation(read, source, cancel);
        write_index(source, &bytes);
        assert!(
            get_profile(&op, &source.root, &Text::from_str("alpha"), budget)
                .unwrap()
                .is_none()
        );
        let selected = get_profile(&op, &source.root, &Text::from_str("beta"), budget)
            .unwrap()
            .expect("exact current profile");
        let doc = &selected._state.doc;
        let [id, revision, document, _, _] = selected._profile.references;
        assert!(doc.string_is(id, "beta"));
        assert!(doc.string_is(revision, beta["current_revision"].as_str().unwrap()));
        assert!(doc.string_is(doc.get(document, "display_name").unwrap(), "beta"));
        assert!(lock_free(
            &source.root.join("profiles/.profile-definition.lock")
        ));
        drop(selected);

        let mut model = definitions_model::load(&op, Some(&bytes), budget).unwrap();
        let rows = definitions_model::list(&op, &mut model, budget).unwrap();
        assert_eq!(rows.value.len(), 2);
        assert!(rows.value[0].scalars.1);
        let payload = rows.value[0].references[2];
        assert!(
            model
                .doc
                .string_is(model.doc.get(payload, "display_name").unwrap(), "alpha")
        );
        drop((rows, model));
        // A first tombstone has no earlier document: the actual list builds
        // its fixed profile_id-only fallback in the same funded arena.
        let mut orphan = entry("orphan", 0);
        let orphan_digest = digest(&json!({"schema":"io.tobkiri.profile-tombstone.v1",
            "profile_id":"orphan","parent_revision":null}));
        orphan["current_revision"] = json!(orphan_digest);
        orphan["tombstone"] = json!(true);
        orphan["revisions"] = json!([{"profile_revision":orphan["current_revision"],
            "parent_revision":null,"profile":null,"created_at":10,"updated_at":10,
            "tombstone":true}]);
        let orphan_bytes = sealed(state(vec![orphan]));
        let mut orphan_model = definitions_model::load(&op, Some(&orphan_bytes), budget).unwrap();
        let orphan_rows = definitions_model::list(&op, &mut orphan_model, budget).unwrap();
        let fallback = orphan_rows.value[0].references[2];
        assert_eq!(orphan_model.doc.entries(fallback).unwrap().len(), 1);
        assert!(orphan_model.doc.string_is(
            orphan_model.doc.get(fallback, "profile_id").unwrap(),
            "orphan"
        ));
        assert!(orphan_model.doc.actual_storage_bytes() <= orphan_model.doc.storage_bound());
        drop((orphan_rows, orphan_model));
        profile["revisions"][1]["parent_revision"] = Value::Null;
        write_index(source, &sealed(state(vec![profile])));
        let error = run(read, source, cancel, budget).unwrap_err();
        assert_eq!(error.message, "Profile revision successor chain is invalid");
        drop(error);
        write_index(
            source,
            &sealed(state(vec![entry("alpha", 0), entry("alpha", 1)])),
        );
        let error = run(read, source, cancel, budget).unwrap_err();
        assert_eq!(error.message, "Profile store ID is duplicated");
    });
}

#[tokio::test(flavor = "current_thread")]
async fn registry_snapshot_postyield_validation_is_normal_only_and_always_closes() {
    with_source(|source, read, cancel, budget| {
        let op = operation(read, source, cancel);
        let profiles = source.root.join("profiles");
        let directory = Directory::capture(&op, &profiles, true).unwrap();
        for normal in [true, false] {
            let mut lock = publication_io::PublicationLock::acquire_named(
                &op,
                &directory,
                publication_io::LockName::Definition,
            )
            .unwrap();
            fs::rename(
                profiles.join(".profile-definition.lock"),
                profiles.join("held-definition-lock"),
            )
            .unwrap();
            fs::write(
                profiles.join(".profile-definition.lock"),
                b"different inode",
            )
            .unwrap();
            fs::set_permissions(
                profiles.join(".profile-definition.lock"),
                fs::Permissions::from_mode(0o600),
            )
            .unwrap();
            let state = if normal {
                definitions_model::load(&op, None, budget)
            } else {
                Err(integrity("prior body failure"))
            };
            let error = finish_snapshot(&op, &directory, &mut lock, state)
                .err()
                .unwrap();
            if normal {
                assert_eq!(
                    error.class,
                    "tobkiri_protocol.secure_persistence.SecurePersistenceError"
                );
            } else {
                assert_eq!(
                    (error.class, error.message.as_str()),
                    (INTEGRITY, "prior body failure")
                );
            }
            drop(error);
            assert!(read.receipt().verified());
            assert!(lock_free(&profiles.join("held-definition-lock")));
        }
    });
}
