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
struct Source {
    root: PathBuf,
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
fn with_source(
    run: impl FnOnce(&Source, &PublicationRead, &CoreCancellation, &Arc<dyn AllocationBudget>),
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
    let read = PublicationRead::new(&invocation).unwrap();
    run(&source, &read, &cancellation, &budget);
    assert!(read.receipt().verified());
    assert!(!source.root.join("authority").exists());
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
fn digest(value: &Value) -> String {
    let data = serde_json::to_vec(value).unwrap();
    let value = tobkiri_canon::parse(&data, tobkiri_canon::Variant::StrictV1).unwrap();
    tobkiri_canon::digest(&value, tobkiri_canon::Variant::StrictV1)
        .unwrap()
        .as_str()
        .to_owned()
}
fn entry(id: &str, order: i64) -> Value {
    let profile = json!({"profile_id":id,"display_name":id});
    let revision = digest(&profile);
    json!({"profile_id":id,"order":order,"tombstone":false,"current_revision":revision,
        "revisions":[{"profile_revision":revision,"parent_revision":null,"profile":profile,
            "created_at":10,"updated_at":10,"tombstone":false}],
        "created_at":10,"updated_at":10,"legacy_ids":[]})
}
fn state(profiles: Vec<Value>) -> Value {
    json!({"schema":"io.tobkiri.profile-definition-store.v1","generation":1,"updated_at":10,
        "profiles":profiles,"legacy":{},"bootstrap":{"state":"not_required","template_profile_revision":null}})
}
fn sealed(mut state: Value) -> Vec<u8> {
    state.as_object_mut().unwrap().remove("store_digest");
    let hash = digest(&state);
    state["store_digest"] = json!(hash);
    serde_json::to_vec(&state).unwrap()
}
fn write_index(source: &Source, bytes: &[u8]) {
    let profiles = source.root.join("profiles");
    fs::create_dir_all(&profiles).unwrap();
    fs::write(profiles.join(INDEX), bytes).unwrap();
    fs::set_permissions(profiles.join(INDEX), fs::Permissions::from_mode(0o600)).unwrap();
}
fn lock_free(path: &Path) -> bool {
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
        let bytes = sealed(state(vec![profile.clone()]));
        let op = operation(read, source, cancel);
        let mut model = definitions_model::load(&op, Some(&bytes), budget).unwrap();
        let rows = definitions_model::list(&op, &mut model, budget).unwrap();
        assert_eq!(rows.value.len(), 1);
        assert!(rows.value[0].scalars.1);
        let payload = rows.value[0].references[2];
        assert!(
            model
                .doc
                .string_is(model.doc.get(payload, "display_name").unwrap(), "alpha")
        );
        drop((rows, model));
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
