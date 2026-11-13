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
            match case {
                "corrupt-pointer" => {
                    let error = result.err().expect("actual malformed active pointer");
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
                    let error = result
                        .err()
                        .expect("definition read must fail before pointer read");
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
