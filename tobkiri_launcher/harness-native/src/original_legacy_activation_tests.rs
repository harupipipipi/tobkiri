use super::*;
use crate::original_authority::definitions::tests::{lock_free, operation, with_setup_source};
use rusqlite::Connection;
use serde_json::{Value as Json, json};
use std::os::unix::fs::PermissionsExt;

fn candidate(budget: &Arc<dyn AllocationBudget>) -> CoreValue {
    let digest = Digest::hash(b"legacy successor");
    CoreValue::parse_guest(&serde_json::to_vec(&json!({
        "profile": {"profile_id":"defaults", "profile_authority_snapshot_digest":digest.as_str()},
        "lock": {"lock_digest":digest.as_str()},
        "plan": {"profile_revision":digest.as_str(), "catalog_revision":digest.as_str(),
            "bundle_digest":digest.as_str(), "plan_digest":digest.as_str(),
            "closure_digest":digest.as_str(), "security_epoch":1}
    })).unwrap(), budget).unwrap()
}

fn read_json(path: &Path) -> Json {
    serde_json::from_slice(&fs::read(path).unwrap()).unwrap()
}

#[tokio::test(flavor = "current_thread")]
async fn legacy_commit_preserves_original_phases_pending_digests_and_cleanup() {
    for failure in [
        None,
        Some("prepared"),
        Some("ready_without_authority"),
        Some("committing"),
        Some("before_authority_commit"),
        Some("after_authority_commit"),
    ] {
        with_setup_source(|source, publication, cancellation, budget| {
            let op = operation(publication, source, cancellation);
            let directory_path = source.root.join("activation");
            let state = Directory::capture(&op, &directory_path, true).unwrap();
            fs::write(directory_path.join("active.json"), b"old-pointer\n").unwrap();
            let lock_name = LockName::workspace("defaults");
            let mut guard = PublicationLock::acquire_named(&op, &state, lock_name).unwrap();
            let resolved = candidate(budget);
            let created_at = string("2026-11-18T01:02:03Z", budget).unwrap();
            let digest = Digest::hash(b"workspace");
            let mut gates = 0;
            let mut phases = Vec::new();
            let result = with_registered_store(&op, &source.root, |store| {
                pending_reservation::abort_incomplete(&op, store, "defaults")?;
                activate_with_fault(
                    &op,
                    store,
                    &state,
                    &digest,
                    budget,
                    Input {
                        profile_id: "defaults",
                        resolved: &resolved,
                        created_at: &created_at,
                        schemas: workspace_active::tests::bound_recovery_schemas(),
                    },
                    (
                        |profile| {
                            assert_eq!(
                                profile.get_key("profile_id").unwrap().as_str(),
                                Some("defaults")
                            );
                            assert!(!lock_free(&directory_path.join(lock_name.text())));
                            gates += 1;
                            Ok(())
                        },
                        |phase| {
                            let pending = read_json(&directory_path.join("pending.json"));
                            let envelope_path = directory_path
                                .join("activations")
                                .join(pending["envelope_path"].as_str().unwrap());
                            let envelope = read_json(&envelope_path);
                            let (state, generation, authority_state) = match phase {
                                "prepared" => ("prepared", 1, "prepared"),
                                "ready_without_authority" => {
                                    ("ready_without_authority", 2, "ready_without_authority")
                                }
                                "committing" => ("committing", 3, "committing"),
                                "before_authority_commit" => ("active", 4, "committing"),
                                "after_authority_commit" => ("active", 4, "active"),
                                _ => panic!("unexpected original phase"),
                            };
                            assert_eq!(envelope["activation"]["state"], state);
                            assert_eq!(envelope["activation"]["state_generation"], generation);
                            assert_eq!(
                                envelope["activation"]["created_at"],
                                "2026-11-18T01:02:03Z"
                            );
                            assert_eq!(
                                envelope["activation"].get("committed_at").is_some(),
                                generation == 4
                            );
                            assert_eq!(envelope["workspace_digest"], digest.as_str());
                            let actual_digest =
                                Digest::hash(&serde_json::to_vec(&envelope).unwrap());
                            assert_eq!(
                                pending["envelope_digest"] == actual_digest.as_str(),
                                phase != "committing"
                            );
                            let db =
                                Connection::open(source.root.join("authority/v4.sqlite3")).unwrap();
                            let actual: String = db.query_row("SELECT state FROM activation_reservations WHERE reservation_id=?", [pending["reservation_id"].as_str().unwrap()], |row| row.get(0)).unwrap();
                            assert_eq!(actual, authority_state);
                            assert!(fs::read(&envelope_path).unwrap().ends_with(b"\n"));
                            assert_eq!(
                                fs::metadata(envelope_path).unwrap().permissions().mode() & 0o777,
                                0o600
                            );
                            phases.push(phase);
                            if failure == Some(phase) {
                                return Err(raw(
                                    "builtins.RuntimeError",
                                    "original activation fault",
                                ));
                            }
                            Ok(())
                        },
                    ),
                )
            });
            let db = Connection::open(source.root.join("authority/v4.sqlite3")).unwrap();
            let authority_state: String = db
                .query_row("SELECT state FROM activation_reservations", [], |row| {
                    row.get(0)
                })
                .unwrap();
            match failure {
                None => {
                    result.unwrap();
                    assert_eq!(gates, 2);
                    assert_eq!(authority_state, "active");
                    assert!(!directory_path.join("pending.json").exists());
                    let pointer = read_json(&directory_path.join("active.json"));
                    let plan_digest = Digest::hash(b"legacy successor");
                    let digest_suffix = &plan_digest.as_str()[7..23];
                    assert_eq!(
                        pointer["activation_id"],
                        format!("activation:defaults-migration-{digest_suffix}")
                    );
                    assert_eq!(pointer["workspace_digest"], digest.as_str());
                    let envelope_path = directory_path
                        .join("activations")
                        .join(pointer["envelope_path"].as_str().unwrap());
                    assert_eq!(
                        pointer["envelope_digest"],
                        Digest::hash(&serde_json::to_vec(&read_json(&envelope_path)).unwrap())
                            .as_str()
                    );
                    assert_eq!(phases.len(), 5);
                }
                Some(phase) => {
                    let error = result.unwrap_err();
                    assert_eq!(error.class, "builtins.RuntimeError");
                    assert_eq!(error.message, "original activation fault");
                    assert_eq!(
                        fs::read(directory_path.join("active.json")).unwrap(),
                        b"old-pointer\n"
                    );
                    if phase == "after_authority_commit" {
                        assert_eq!(authority_state, "active");
                        assert!(directory_path.join("pending.json").exists());
                        assert_eq!(gates, 1);
                    } else {
                        assert_eq!(authority_state, "aborted");
                        assert!(!directory_path.join("pending.json").exists());
                        assert_eq!(
                            fs::read_dir(directory_path.join("activations"))
                                .unwrap()
                                .count(),
                            0
                        );
                        assert_eq!(gates, 0);
                    }
                }
            }
            guard.close().unwrap();
            assert!(lock_free(&directory_path.join(lock_name.text())));
        });
    }
}

#[tokio::test(flavor = "current_thread")]
async fn identity_gates_preserve_precommit_abort_and_postcommit_pending() {
    for failing_gate in [1, 2] {
        with_setup_source(|source, publication, cancellation, budget| {
            let op = operation(publication, source, cancellation);
            let path = source.root.join("activation");
            let state = Directory::capture(&op, &path, true).unwrap();
            fs::write(path.join("active.json"), b"old\n").unwrap();
            let mut guard =
                PublicationLock::acquire_named(&op, &state, LockName::workspace("defaults"))
                    .unwrap();
            let resolved = candidate(budget);
            let created_at = string("2026-11-18T01:02:03Z", budget).unwrap();
            let mut gates = 0;
            let error = with_registered_store(&op, &source.root, |store| {
                activate(
                    &op,
                    store,
                    &state,
                    &Digest::hash(b"workspace"),
                    budget,
                    Input {
                        profile_id: "defaults",
                        resolved: &resolved,
                        created_at: &created_at,
                        schemas: workspace_active::tests::bound_recovery_schemas(),
                    },
                    |_| {
                        gates += 1;
                        if gates == failing_gate {
                            return Err(raw(recovery_workspace::APP_DENIED, "artifact changed"));
                        }
                        Ok(())
                    },
                )
            })
            .unwrap_err();
            assert_eq!(error.message, "artifact changed");
            assert_eq!(gates, failing_gate);
            assert_eq!(fs::read(path.join("active.json")).unwrap(), b"old\n");
            let db = Connection::open(source.root.join("authority/v4.sqlite3")).unwrap();
            let state: String = db
                .query_row("SELECT state FROM activation_reservations", [], |row| {
                    row.get(0)
                })
                .unwrap();
            assert_eq!(
                state,
                if failing_gate == 1 {
                    "aborted"
                } else {
                    "active"
                }
            );
            assert_eq!(path.join("pending.json").exists(), failing_gate == 2);
            guard.close().unwrap();
        });
    }
}

#[tokio::test(flavor = "current_thread")]
async fn postcommit_epoch_change_preserves_committed_pending_and_old_pointer() {
    with_setup_source(|source, publication, cancellation, budget| {
        let op = operation(publication, source, cancellation);
        let path = source.root.join("activation");
        let state = Directory::capture(&op, &path, true).unwrap();
        fs::write(path.join("active.json"), b"old\n").unwrap();
        let mut guard =
            PublicationLock::acquire_named(&op, &state, LockName::workspace("defaults")).unwrap();
        let resolved = candidate(budget);
        let created_at = string("2026-11-18T01:02:03Z", budget).unwrap();
        let mut gates = 0;
        let error = with_registered_store(&op, &source.root, |store| {
            activate_with_fault(
                &op,
                store,
                &state,
                &Digest::hash(b"workspace"),
                budget,
                Input {
                    profile_id: "defaults",
                    resolved: &resolved,
                    created_at: &created_at,
                    schemas: workspace_active::tests::bound_recovery_schemas(),
                },
                (
                    |_| {
                        gates += 1;
                        Ok(())
                    },
                    |phase| {
                        if phase == "after_authority_commit" {
                            let db =
                                Connection::open(source.root.join("authority/v4.sqlite3")).unwrap();
                            db.execute(
                                "UPDATE authority_meta SET value='2' WHERE key='security_epoch'",
                                [],
                            )
                            .unwrap();
                        }
                        Ok(())
                    },
                ),
            )
        })
        .unwrap_err();
        assert_eq!(
            error.message,
            "activation reservation is stale immediately before publish"
        );
        assert_eq!(gates, 1);
        assert!(path.join("pending.json").exists());
        assert_eq!(fs::read(path.join("active.json")).unwrap(), b"old\n");
        guard.close().unwrap();
    });
}

#[tokio::test(flavor = "current_thread")]
async fn activation_record_validates_true_unsafe_fence_before_canonical_rejection() {
    with_setup_source(|source, publication, cancellation, budget| {
        let op = operation(publication, source, cancellation);
        let resolved = candidate(budget);
        let profile = field(&resolved, "profile").unwrap();
        let lock = field(&resolved, "lock").unwrap();
        let plan = field(&resolved, "plan").unwrap();
        let created_at = string("2026-11-18T01:02:03Z", budget).unwrap();
        let error = activation_record(
            &op,
            budget,
            workspace_active::tests::bound_recovery_schemas(),
            [&profile, &lock, &plan],
            RecordState {
                activation_id: "activation:defaults-migration-0123456789abcdef",
                state: "prepared",
                generation: 1,
                fencing_token: 9_007_199_254_740_992,
                created_at: &created_at,
            },
        )
        .unwrap_err();
        assert_eq!(error.class, "tobkiri_protocol.errors.CanonicalizationError");
        assert_eq!(
            error.message,
            "integer exceeds I-JSON safe range; use a canonical decimal string"
        );
    });
}

#[tokio::test(flavor = "current_thread")]
async fn activation_writer_creates_owned_parent_and_refuses_symlink_parent() {
    with_setup_source(|source, publication, cancellation, _| {
        let op = operation(publication, source, cancellation);
        let path = source.root.join("activation");
        let state = Directory::capture(&op, &path, true).unwrap();
        state
            .replace_activation_state(&op, Path::new("activations/first.json"), b"first\n")
            .unwrap();
        assert_eq!(
            fs::metadata(path.join("activations"))
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o700
        );
        state
            .replace_activation_state(&op, Path::new("activations/first.json"), b"second\n")
            .unwrap();
        assert_eq!(
            fs::read(path.join("activations/first.json")).unwrap(),
            b"second\n"
        );
        let outside = source.root.join("outside");
        fs::create_dir(&outside).unwrap();
        std::os::unix::fs::symlink(&outside, path.join("redirected")).unwrap();
        let error = state
            .replace_activation_state(&op, Path::new("redirected/leak.json"), b"blocked")
            .unwrap_err();
        assert_eq!(error.class, SECURE_PERSISTENCE_ERROR);
        assert!(!outside.join("leak.json").exists());
    });
}

#[tokio::test(flavor = "current_thread")]
async fn encode_distinguishes_json_size_from_the_same_owners_capacity_refusal() {
    struct DenyEncode(Arc<dyn AllocationBudget>);
    impl AllocationBudget for DenyEncode {
        fn reserve(&self, request: AllocationRequest) -> std::result::Result<(), AllocationError> {
            if request.kind == AllocationKind::Encode && request.bytes > 0 {
                return Err(AllocationError::Limit);
            }
            self.0.reserve(request)
        }
        fn release(&self, bytes: u64) {
            self.0.release(bytes);
        }
    }
    with_setup_source(|_, _, _, budget| {
        let value = string("small JSON", budget).unwrap();
        let refusing: Arc<dyn AllocationBudget> = Arc::new(DenyEncode(budget.clone()));
        let error = encode(&value, &refusing).unwrap_err();
        assert_eq!(
            error.class,
            "tobkiri.native.OriginalAuthorityIntegrationFailure"
        );
        assert!(matches!(
            error.cause,
            Some(OriginalAuthorityCause::Capacity)
        ));
        let large = string(&"a".repeat(1_500_000), budget).unwrap();
        let unfolded = object_value(
            budget,
            [("a", large.clone()), ("b", large.clone()), ("c", large)],
        )
        .unwrap();
        let error = encode(&unfolded, budget).unwrap_err();
        assert_eq!(error.class, "tobkiri_protocol.errors.CanonicalizationError");
        assert_eq!(error.message, "canonical JSON exceeds size limit");
    });
}
