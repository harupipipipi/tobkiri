//! The AuthorityStore context at the original nonstale publication caller.
use super::*;
use crate::original_authority::io::incomplete_reservations;
use tobkiri_canon::AllocationBudget;
#[path = "original_recovery_active_reservation.rs"]
mod active_reservation;
#[path = "original_recovery_pending_reservation.rs"]
mod pending_reservation;

/// The result needed by the original recovery caller at its next operation.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum RecoveryAuthorityReached {
    LoadCommittedActivationEnvelope,
    AbortPendingActivationReservation,
    WorkspaceRecovered,
    LoadActiveSnapshot,
    MissingPublishedAuthority,
    ReadIncompleteActivationReservations,
    RecoveredNone,
    MatchedReservation,
}

impl PublicationRead {
    /// Enter Authority only after Bootstrap has verified the replacement
    /// catalog on this runtime. Paths and intent stay in the journal scope.
    pub(crate) fn run_recovery_authority<S: crate::BootstrapReviewOwner>(
        &mut self,
        owner: &S,
        cancellation: &CoreCancellation,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<RecoveryAuthorityReached> {
        self.check_continuation_thread()?;
        let receipt = self.receipt();
        let pending = self.pending.as_mut().expect("held recovery scope");
        if !pending.cancellation.same_node(cancellation)
            || pending.scope.guard.file.is_none()
            || receipt.open_files != 1
            || receipt.open_connections != 0
            || receipt.uncertain
        {
            return Err(OriginalAuthorityError::unsupported(
                OriginalAuthorityStage::Guard,
                "publication recovery owner or cleanup is uncertain",
            ));
        }
        let workspace = pending.workspace.as_ref().ok_or_else(|| {
            OriginalAuthorityError::unsupported(
                OriginalAuthorityStage::Path,
                "publication recovery workspace is not held",
            )
        })?;
        if std::mem::replace(&mut pending.authority_attempted, true) {
            return Err(OriginalAuthorityError::unsupported(
                OriginalAuthorityStage::Guard,
                "publication recovery Authority was already entered",
            ));
        }

        let op = Self::operation(&self.invocation, &self.cleanup, owner, cancellation)?;
        let reached = (|| {
            // Check continuity of the held publication lock before the
            // Authority or activation store may create state.
            op.check(OriginalAuthorityStage::Guard)?;
            pending.scope.validate(&op)?;
            with_registered_store(&op, &pending.root, |store| {
                // Path.exists follows links, treats files as existing too, and
                // suppresses only the original finite set of stat errors.
                let exists = candidate_selection::predecessor_stat(&op, &workspace.path, |path| {
                    fs::metadata(path)
                })?
                .is_some();
                if exists {
                    let profile_id = workspace
                        .path
                        .file_name()
                        .and_then(|part| part.to_str())
                        .expect("held canonical bootstrap Profile ID");
                    let next = recovery_workspace::recover(
                        &op,
                        &workspace.path,
                        workspace.lock_name,
                        |state, journal| {
                            if let Some(journal) = journal {
                                return pending_reservation::recover(
                                    &op, store, state, journal, profile_id, budget,
                                );
                            }
                            let rows = incomplete_reservations::read(&op, store, profile_id)?;
                            Ok(if rows.is_empty() {
                                RecoveryAuthorityReached::WorkspaceRecovered
                            } else {
                                RecoveryAuthorityReached::ReadIncompleteActivationReservations
                            })
                        },
                    )?;
                    if next != RecoveryAuthorityReached::WorkspaceRecovered {
                        return Ok(next);
                    }
                }
                if active_reservation::read(&op, store, &pending.intent)? {
                    return Ok(if exists {
                        RecoveryAuthorityReached::LoadActiveSnapshot
                    } else {
                        RecoveryAuthorityReached::MatchedReservation
                    });
                }
                if exists
                    && published_candidate(
                        pending._current.as_ref(),
                        &pending.intent,
                        pending._expected.as_ref(),
                    )?
                {
                    return Ok(RecoveryAuthorityReached::MissingPublishedAuthority);
                }
                // Original recovery clears the intent in both open contexts,
                // reopening the fixed journal directory under the same guard.
                let _path = op.reserve(
                    OriginalAuthorityStage::Path,
                    pending.root.as_os_str().len().saturating_add(8192),
                )?;
                Directory::capture(&op, &pending.root.join("profiles"), true)?
                    .clear_journal(&op)?;
                Ok(RecoveryAuthorityReached::RecoveredNone)
            })
        })()
        .map_err(recovery_error)?;
        // The activation lock and Authority's SQLite and guard handles have
        // closed here. The publication lock stays in the outer continuation.
        if self.receipt() != receipt {
            return Err(OriginalAuthorityError::unsupported(
                OriginalAuthorityStage::Close,
                "publication recovery Authority cleanup is uncertain",
            ));
        }
        if reached == RecoveryAuthorityReached::RecoveredNone {
            let pending = self.pending.take().expect("completed publication recovery");
            pending
                .scope
                .finish(&op, Ok(Prefix::RecoveredNone))
                .map_err(recovery_error)?;
            self.fresh.record_recovery(cancellation);
        }
        Ok(reached)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::original_authority::definitions::tests::{
        Source, lock_free, operation, with_setup_source,
    };
    use hmac::{Hmac, Mac};
    use rusqlite::params;
    use sha2::Sha256;
    use std::os::unix::fs::symlink;

    const PROFILE_ID: &str = "recovery-profile";
    const ACTIVATION_ID: &str = "activation:recovery";
    const DATABASE: &str = "authority/v4.sqlite3";
    const GUARD: &str = "authority/.v4.sqlite3.lifecycle.lock";
    const LOCK_PATH: &str = "profiles/.bootstrap-publication.lock";

    fn enter(source: &Source, read: &mut PublicationRead, cancellation: &CoreCancellation) {
        enter_candidate(
            source,
            read,
            cancellation,
            r#"{"activation_id": "activation:recovery", "profile_id": "recovery-profile"}"#,
        );
    }

    fn enter_candidate(
        source: &Source,
        read: &mut PublicationRead,
        cancellation: &CoreCancellation,
        candidate: &str,
    ) {
        let profiles = source.root.join("profiles");
        fs::create_dir_all(&profiles).unwrap();
        let key = [b'k'; 32];
        fs::write(profiles.join(KEY), key).unwrap();
        fs::set_permissions(profiles.join(KEY), fs::Permissions::from_mode(0o600)).unwrap();
        let payload = format!(
            r#"{{"candidate": {candidate}, "confirmation_digest": null, "predecessor": null, "schema": "io.tobkiri.profile-publication-intent.v1"}}"#
        );
        let mut mac = Hmac::<Sha256>::new_from_slice(&key).unwrap();
        mac.update(payload.as_bytes());
        let signature = Digest::from_sha256(mac.finalize().into_bytes().into());
        let journal = profiles.join(JOURNAL);
        fs::write(
            &journal,
            format!(
                r#"{{"payload":{},"hmac":"{}"}}"#,
                payload,
                &signature.as_str()[7..],
            ),
        )
        .unwrap();
        fs::set_permissions(journal, fs::Permissions::from_mode(0o600)).unwrap();
        assert_eq!(
            read.enter_recovery(source, cancellation, true).unwrap(),
            Prefix::BootstrapProfileIdNotBound
        );
        read.construct_workspace(source, cancellation, PROFILE_ID)
            .unwrap();
        assert!(!lock_free(&source.root.join(LOCK_PATH)));
    }

    fn initialize(source: &Source, read: &PublicationRead, cancellation: &CoreCancellation) {
        let op = operation(read, source, cancellation);
        with_registered_store(&op, &source.root, |_| Ok(())).unwrap();
        assert!(lock_free(&source.root.join(GUARD)));
    }

    fn insert(db: &Connection, token: i64, activation_id: &str, state: &str) {
        db.execute(
            "INSERT INTO activation_reservations (
                 reservation_id, activation_id, profile_id, plan_digest,
                 profile_authority_digest, security_epoch, fencing_token,
                 state, created_at, updated_at
             ) VALUES (?, ?, 'other-profile', 'plan', 'authority', 2, ?, ?, 1.0, 2.0)",
            params![format!("reservation:{token}"), activation_id, token, state],
        )
        .unwrap();
    }

    fn verify_custody(
        source: &Source,
        read: &mut PublicationRead,
        cancellation: &CoreCancellation,
        cleared: bool,
    ) {
        assert!(lock_free(&source.root.join(GUARD)));
        assert_eq!(lock_free(&source.root.join(LOCK_PATH)), cleared);
        assert_eq!(read.receipt().open_connections, 0);
        assert_eq!(read.receipt().open_files, if cleared { 0 } else { 1 });
        assert!(!read.receipt().uncertain);
        assert_eq!(
            !source.root.join("profiles").join(JOURNAL).exists(),
            cleared
        );
        if cleared {
            assert_eq!(
                read.enter_recovery(source, cancellation, false).unwrap(),
                Prefix::NoJournal
            );
            read.acquire_candidate_root(source, cancellation).unwrap();
        }
        read.close_continuation().unwrap();
        assert!(lock_free(&source.root.join(LOCK_PATH)));
    }

    #[tokio::test(flavor = "current_thread")]
    async fn recovery_authority_initializes_before_branching_on_the_workspace() {
        for (case, expected) in [
            ("missing", RecoveryAuthorityReached::RecoveredNone),
            ("directory", RecoveryAuthorityReached::RecoveredNone),
            ("dangling", RecoveryAuthorityReached::RecoveredNone),
            ("loop", RecoveryAuthorityReached::RecoveredNone),
        ] {
            with_setup_source(|source, read, cancellation, budget| {
                enter(source, read, cancellation);
                let workspace = source.root.join("workspaces").join(PROFILE_ID);
                fs::create_dir(workspace.parent().unwrap()).unwrap();
                match case {
                    "directory" => fs::create_dir(&workspace).unwrap(),
                    "dangling" => symlink("missing", &workspace).unwrap(),
                    "loop" => symlink(PROFILE_ID, &workspace).unwrap(),
                    "missing" => {}
                    _ => unreachable!(),
                }
                assert!(!source.root.join("authority").exists());
                assert_eq!(
                    read.run_recovery_authority(source, cancellation, budget)
                        .unwrap(),
                    expected
                );
                let db = Connection::open(source.root.join(DATABASE)).unwrap();
                assert_eq!(
                    db.query_row(
                        "SELECT value FROM authority_meta WHERE key='schema_version'",
                        [],
                        |row| row.get::<_, String>(0),
                    )
                    .unwrap(),
                    "4"
                );
                db.close().unwrap();
                assert!(source.root.join("authority/v4.key").is_file());
                assert!(
                    read.run_recovery_authority(source, cancellation, budget)
                        .is_err()
                );
                verify_custody(
                    source,
                    read,
                    cancellation,
                    expected == RecoveryAuthorityReached::RecoveredNone,
                );
            });
        }
    }

    #[tokio::test(flavor = "current_thread")]
    async fn recovery_authority_denies_a_missing_pending_row_without_touching_the_journal() {
        with_setup_source(|source, read, cancellation, budget| {
            enter(source, read, cancellation);
            let workspace = source.root.join("workspaces").join(PROFILE_ID);
            let state = workspace.join("activation");
            fs::create_dir_all(&state).unwrap();
            let resolved = workspace.canonicalize().unwrap();
            let digest = Digest::hash(
                &serde_json::to_vec(
                    &serde_json::json!({"workspace_root": resolved.to_str().unwrap()}),
                )
                .unwrap(),
            );
            let data = serde_json::to_vec(&serde_json::json!({
                "schema": "io.tobkiri.defaultspack-pending-activation.v1",
                "reservation_id": "not-in-sqlite",
                "activation_id": ACTIVATION_ID,
                "envelope_path": "not-on-disk.json",
                "envelope_digest": null,
                "workspace_digest": digest.as_str(),
                "fencing_token": 1,
            }))
            .unwrap();
            let pending = state.join("pending.json");
            fs::write(&pending, &data).unwrap();
            let lock = state.join(LockName::workspace(PROFILE_ID).text());

            let error = read
                .run_recovery_authority(source, cancellation, budget)
                .unwrap_err();
            assert_eq!(error.class, recovery_workspace::APP_DENIED);
            assert_eq!(
                error.message,
                "pending authority reservation is unavailable"
            );
            assert_eq!(fs::read(pending).unwrap(), data);
            assert!(lock_free(&lock));
            verify_custody(source, read, cancellation, false);
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn pending_recovery_checks_live_binding_before_branching_or_unlinking() {
        for (state, profile, fence, envelope, expected) in [
            (
                "active",
                PROFILE_ID,
                "+１",
                "pinned.json",
                Ok(RecoveryAuthorityReached::LoadCommittedActivationEnvelope),
            ),
            (
                "committing",
                PROFILE_ID,
                "1",
                "pinned.json",
                Ok(RecoveryAuthorityReached::AbortPendingActivationReservation),
            ),
            (
                "aborted",
                PROFILE_ID,
                "1",
                "pinned.json",
                Ok(RecoveryAuthorityReached::RecoveredNone),
            ),
            (
                "active",
                "another-profile",
                "1",
                "../pinned.json",
                Err("pending activation authority binding changed"),
            ),
            (
                "active",
                PROFILE_ID,
                "2",
                "../pinned.json",
                Err("pending activation authority binding changed"),
            ),
            (
                "active",
                PROFILE_ID,
                "1",
                "../pinned.json",
                Err("pending activation envelope path is invalid"),
            ),
            (
                "retired",
                PROFILE_ID,
                "1",
                "pinned.json",
                Err("pending activation has an invalid authority state"),
            ),
        ] {
            with_setup_source(|source, read, cancellation, budget| {
                enter(source, read, cancellation);
                initialize(source, read, cancellation);
                let workspace = source.root.join("workspaces").join(PROFILE_ID);
                let state_root = workspace.join("activation");
                fs::create_dir_all(state_root.join("activations")).unwrap();
                let stored_envelope = state_root.join("activations").join("pinned.json");
                let other_envelope = state_root.join("activations").join("different.json");
                fs::write(
                    &stored_envelope,
                    b"only this envelope belongs to the pending activation",
                )
                .unwrap();
                fs::write(&other_envelope, b"existing unrelated envelope").unwrap();
                let lock = state_root.join(LockName::workspace(PROFILE_ID).text());
                let digest = tobkiri_canon::digest(
                    &object([(
                        "workspace_root",
                        CanonValue::Str(
                            workspace
                                .canonicalize()
                                .unwrap()
                                .to_str()
                                .unwrap()
                                .to_owned(),
                        ),
                    )]),
                    tobkiri_canon::Variant::StrictV1,
                )
                .unwrap();
                let pending = state_root.join("pending.json");
                let data = serde_json::to_vec(&serde_json::json!({
                    "schema": "io.tobkiri.defaultspack-pending-activation.v1",
                    "reservation_id": "reservation:1",
                    "activation_id": ACTIVATION_ID,
                    "envelope_path": envelope,
                    "envelope_digest": null,
                    "workspace_digest": digest.as_str(),
                    "fencing_token": fence,
                }))
                .unwrap();
                fs::write(&pending, &data).unwrap();
                let db = Connection::open(source.root.join(DATABASE)).unwrap();
                insert(&db, 1, ACTIVATION_ID, state);
                db.execute("UPDATE activation_reservations SET profile_id=?", [profile])
                    .unwrap();
                db.close().unwrap();

                let result = read.run_recovery_authority(source, cancellation, budget);
                let cleared = expected == Ok(RecoveryAuthorityReached::RecoveredNone);
                match expected {
                    Ok(next) => assert_eq!(result.unwrap(), next),
                    Err(message) => {
                        let error = result.unwrap_err();
                        assert_eq!(error.class, recovery_workspace::APP_DENIED);
                        assert_eq!(error.message, message);
                        assert!(error.cause.is_none());
                    }
                }
                assert_eq!(stored_envelope.exists(), !cleared);
                assert_eq!(
                    fs::read(other_envelope).unwrap(),
                    b"existing unrelated envelope"
                );
                if cleared {
                    assert!(!pending.exists());
                } else {
                    assert_eq!(fs::read(pending).unwrap(), data);
                }
                assert!(lock_free(&lock));
                verify_custody(source, read, cancellation, cleared);
            });
        }
    }

    #[tokio::test(flavor = "current_thread")]
    async fn recovery_authority_preserves_constructor_failure_and_closes_the_held_lock() {
        with_setup_source(|source, read, cancellation, budget| {
            enter(source, read, cancellation);
            fs::create_dir_all(source.root.join(DATABASE)).unwrap();
            assert_eq!(
                read.run_recovery_authority(source, cancellation, budget)
                    .unwrap_err()
                    .class,
                "core_runtime.authority.store_contracts.AuthorityStoreError"
            );
            assert!(source.root.join(DATABASE).is_dir());
            assert!(!source.root.join("authority/v4.key").exists());
            assert!(!source.root.join(GUARD).exists());
            assert!(!lock_free(&source.root.join(LOCK_PATH)));
            assert!(source.root.join("profiles").join(JOURNAL).is_file());
            read.close_continuation().unwrap();
            assert!(lock_free(&source.root.join(LOCK_PATH)));
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn existing_workspace_checks_incomplete_reservations_before_active_snapshot() {
        for (state, expected) in [
            ("active", RecoveryAuthorityReached::LoadActiveSnapshot),
            (
                "committing",
                RecoveryAuthorityReached::ReadIncompleteActivationReservations,
            ),
        ] {
            with_setup_source(|source, read, cancellation, budget| {
                enter(source, read, cancellation);
                let workspace = source.root.join("workspaces").join(PROFILE_ID);
                fs::create_dir_all(&workspace).unwrap();
                initialize(source, read, cancellation);
                let db = Connection::open(source.root.join(DATABASE)).unwrap();
                insert(&db, 1, ACTIVATION_ID, state);
                db.execute(
                    "UPDATE activation_reservations SET profile_id=?",
                    [PROFILE_ID],
                )
                .unwrap();
                db.close().unwrap();

                assert_eq!(
                    read.run_recovery_authority(source, cancellation, budget)
                        .unwrap(),
                    expected
                );
                let lock = workspace
                    .join("activation")
                    .join(LockName::workspace(PROFILE_ID).text());
                assert!(lock_free(&lock));
                verify_custody(source, read, cancellation, false);
            });
        }
    }

    #[tokio::test(flavor = "current_thread")]
    async fn recovery_authority_uses_exact_activation_id_and_active_state() {
        for matches in 0..=2 {
            with_setup_source(|source, read, cancellation, budget| {
                enter(source, read, cancellation);
                initialize(source, read, cancellation);
                let db = Connection::open(source.root.join(DATABASE)).unwrap();
                for (index, (id, state)) in [
                    ("activation:RECOVERY", "active"),
                    ("another-activation", "active"),
                    (ACTIVATION_ID, "prepared"),
                    (ACTIVATION_ID, "ready_without_authority"),
                    (ACTIVATION_ID, "committing"),
                    (ACTIVATION_ID, "retired"),
                    (ACTIVATION_ID, "aborted"),
                ]
                .into_iter()
                .enumerate()
                {
                    insert(&db, 1 + index as i64, id, state);
                }
                for i in 0..matches {
                    insert(&db, 20 + i, ACTIVATION_ID, "active");
                }
                db.close().unwrap();
                let result = read.run_recovery_authority(source, cancellation, budget);
                match matches {
                    0 => assert_eq!(result.unwrap(), RecoveryAuthorityReached::RecoveredNone),
                    1 => assert_eq!(
                        result.unwrap(),
                        RecoveryAuthorityReached::MatchedReservation
                    ),
                    _ => {
                        let error = result.unwrap_err();
                        assert_eq!(error.stage, OriginalAuthorityStage::ActiveReservation);
                        assert_eq!(
                            error.class,
                            "core_runtime.authority.store_contracts.AuthorityStoreError"
                        );
                        assert_eq!(
                            error.message,
                            "multiple active authority reservations exist"
                        );
                        assert!(error.cause.is_none());
                    }
                }
                verify_custody(source, read, cancellation, matches == 0);
            });
        }
    }

    #[tokio::test(flavor = "current_thread")]
    async fn recovery_authority_reads_signed_activation_id_after_workspace_stat() {
        use RecoveryAuthorityReached::*;
        for (id, workspace_exists, expected) in [
            ("", true, Err(("builtins.KeyError", "'activation_id'"))),
            ("", false, Err(("builtins.KeyError", "'activation_id'"))),
            (
                r#""activation_id": [], "#,
                false,
                Err((
                    "core_runtime.authority.store_contracts.AuthorityStoreError",
                    "active activation read failed",
                )),
            ),
            (r#""activation_id": 42, "#, false, Ok(MatchedReservation)),
            (r#""activation_id": true, "#, false, Ok(MatchedReservation)),
            (r#""activation_id": null, "#, false, Ok(RecoveredNone)),
        ] {
            with_setup_source(|source, read, cancellation, budget| {
                let candidate = format!(r#"{{{id}"profile_id": "{PROFILE_ID}"}}"#);
                enter_candidate(source, read, cancellation, &candidate);
                initialize(source, read, cancellation);
                let db = Connection::open(source.root.join(DATABASE)).unwrap();
                insert(&db, 1, "1", "active");
                insert(&db, 2, "42", "active");
                db.close().unwrap();
                let workspace = source.root.join("workspaces").join(PROFILE_ID);
                if workspace_exists {
                    fs::create_dir_all(workspace).unwrap();
                }
                let result = read.run_recovery_authority(source, cancellation, budget);
                match expected {
                    Ok(expected) => assert_eq!(result.unwrap(), expected),
                    Err((class, message)) => {
                        let error = result.unwrap_err();
                        assert_eq!(error.class, class);
                        assert_eq!(error.message, message);
                        if message == "active activation read failed" {
                            assert!(matches!(
                                error.cause,
                                Some(OriginalAuthorityCause::Python {
                                    class: "sqlite3.ProgrammingError",
                                    ..
                                })
                            ));
                        }
                    }
                }
                verify_custody(source, read, cancellation, expected == Ok(RecoveredNone));
            });
        }
    }

    #[tokio::test(flavor = "current_thread")]
    async fn recovery_authority_wraps_a_sqlite_reservation_read_failure() {
        with_setup_source(|source, read, cancellation, budget| {
            enter(source, read, cancellation);
            initialize(source, read, cancellation);
            let db = Connection::open(source.root.join(DATABASE)).unwrap();
            // A legacy table with an incomplete shape makes the actual
            // reservation prepare fail after the store constructor.
            db.execute_batch(
                "DROP TABLE activation_reservations;
                 CREATE TABLE activation_reservations (reservation_id TEXT PRIMARY KEY) STRICT;",
            )
            .unwrap();
            db.close().unwrap();
            let error = read
                .run_recovery_authority(source, cancellation, budget)
                .unwrap_err();
            assert_eq!(error.stage, OriginalAuthorityStage::ActiveReservation);
            assert_eq!(
                error.class,
                "core_runtime.authority.store_contracts.AuthorityStoreError"
            );
            assert_eq!(error.message, "active activation read failed");
            assert!(error.suppressed);
            assert!(matches!(
                error.cause,
                Some(OriginalAuthorityCause::Python {
                    class: "sqlite3.OperationalError",
                    ..
                })
            ));
            verify_custody(source, read, cancellation, false);
        });
    }
}
