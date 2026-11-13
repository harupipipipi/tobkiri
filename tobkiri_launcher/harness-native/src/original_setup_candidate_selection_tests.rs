//! Portable tests for Host persistence after the fresh catalog caller.
use super::{
    tests::{digest, entry, lock_free, sealed, state, with_source, write_index},
    *,
};
use serde_json::json;
use std::os::unix::fs::{PermissionsExt, symlink};

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
                assert!(matches!(selected, CandidateSelection::ActiveAuthority(_)));
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
