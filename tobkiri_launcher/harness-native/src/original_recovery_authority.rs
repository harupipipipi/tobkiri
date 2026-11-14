//! The AuthorityStore context at the original nonstale publication caller.
use super::*;
#[path = "original_recovery_active_reservation.rs"]
mod active_reservation;

/// The result needed by the original recovery caller at its next operation.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum RecoveryAuthorityReached {
    RecoverActivation,
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
            // Check continuity of the held lock before the constructor is
            // allowed to create state. Never acquire a replacement lock/root.
            op.check(OriginalAuthorityStage::Guard)?;
            pending.scope.validate(&op)?;
            with_registered_store(&op, &pending.root, |store| {
                // Path.exists follows links, treats files as existing too, and
                // suppresses only the original finite set of stat errors.
                let exists = candidate_selection::predecessor_stat(&op, workspace, |path| {
                    fs::metadata(path)
                })?
                .is_some();
                if exists {
                    recovery_workspace::construct(&op, workspace)?;
                    return Ok(RecoveryAuthorityReached::RecoverActivation);
                }
                if active_reservation::read(&op, store, &pending.intent)? {
                    return Ok(RecoveryAuthorityReached::MatchedReservation);
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
        // The constructor's SQLite/guard handles and Authority context must
        // have closed before this diagnostic leaves the host. The publication
        // lock remains in the outer continuation for its own cleanup.
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
            ("directory", RecoveryAuthorityReached::RecoverActivation),
            ("dangling", RecoveryAuthorityReached::RecoveredNone),
            ("loop", RecoveryAuthorityReached::RecoveredNone),
        ] {
            with_setup_source(|source, read, cancellation, _| {
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
                    read.run_recovery_authority(source, cancellation).unwrap(),
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
                assert!(read.run_recovery_authority(source, cancellation).is_err());
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
    async fn recovery_authority_preserves_constructor_failure_and_closes_the_held_lock() {
        with_setup_source(|source, read, cancellation, _| {
            enter(source, read, cancellation);
            fs::create_dir_all(source.root.join(DATABASE)).unwrap();
            assert_eq!(
                read.run_recovery_authority(source, cancellation)
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
    async fn recovery_authority_uses_exact_activation_id_and_active_state() {
        for matches in 0..=2 {
            with_setup_source(|source, read, cancellation, _| {
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
                let result = read.run_recovery_authority(source, cancellation);
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
            ("", true, Ok(RecoverActivation)),
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
            with_setup_source(|source, read, cancellation, _| {
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
                let result = read.run_recovery_authority(source, cancellation);
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
        with_setup_source(|source, read, cancellation, _| {
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
                .run_recovery_authority(source, cancellation)
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
