//! The AuthorityStore context at the original nonstale publication caller.
use super::*;

/// The first unreached operation within the original AuthorityStore context.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum RecoveryAuthorityReached {
    ActivationStore,
    ActiveReservation,
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
            with_registered_store(&op, &pending.root, |_| {
                // Path.exists follows links, treats files as existing too, and
                // suppresses only the original finite set of stat errors.
                let exists = candidate_selection::predecessor_stat(&op, workspace, |path| {
                    fs::metadata(path)
                })?
                .is_some();
                Ok(if exists {
                    RecoveryAuthorityReached::ActivationStore
                } else {
                    RecoveryAuthorityReached::ActiveReservation
                })
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
        Ok(reached)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::original_authority::definitions::tests::{Source, lock_free, with_setup_source};
    use hmac::{Hmac, Mac};
    use sha2::Sha256;
    use std::os::unix::fs::symlink;

    const PROFILE_ID: &str = "recovery-profile";
    const DATABASE: &str = "authority/v4.sqlite3";
    const GUARD: &str = "authority/.v4.sqlite3.lifecycle.lock";
    const LOCK_PATH: &str = "profiles/.bootstrap-publication.lock";

    fn enter(source: &Source, read: &mut PublicationRead, cancellation: &CoreCancellation) {
        let profiles = source.root.join("profiles");
        fs::create_dir_all(&profiles).unwrap();
        let key = [b'k'; 32];
        fs::write(profiles.join(KEY), key).unwrap();
        fs::set_permissions(profiles.join(KEY), fs::Permissions::from_mode(0o600)).unwrap();
        let payload = br#"{"candidate": {"profile_id": "recovery-profile"}, "confirmation_digest": null, "predecessor": null, "schema": "io.tobkiri.profile-publication-intent.v1"}"#;
        let mut mac = Hmac::<Sha256>::new_from_slice(&key).unwrap();
        mac.update(payload);
        let signature = Digest::from_sha256(mac.finalize().into_bytes().into());
        let journal = profiles.join(JOURNAL);
        fs::write(
            &journal,
            format!(
                r#"{{"payload":{},"hmac":"{}"}}"#,
                std::str::from_utf8(payload).unwrap(),
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

    #[tokio::test(flavor = "current_thread")]
    async fn recovery_authority_initializes_before_branching_on_the_workspace() {
        for (case, expected) in [
            ("missing", RecoveryAuthorityReached::ActiveReservation),
            ("directory", RecoveryAuthorityReached::ActivationStore),
            ("file", RecoveryAuthorityReached::ActivationStore),
            ("dangling", RecoveryAuthorityReached::ActiveReservation),
            ("loop", RecoveryAuthorityReached::ActiveReservation),
        ] {
            with_setup_source(|source, read, cancellation, _| {
                enter(source, read, cancellation);
                let workspace = source.root.join("workspaces").join(PROFILE_ID);
                fs::create_dir(workspace.parent().unwrap()).unwrap();
                match case {
                    "directory" => fs::create_dir(&workspace).unwrap(),
                    "file" => fs::write(&workspace, "existing").unwrap(),
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
                assert!(lock_free(&source.root.join(GUARD)));
                assert!(!lock_free(&source.root.join(LOCK_PATH)));
                assert_eq!(read.receipt().open_connections, 0);
                assert_eq!(read.receipt().open_files, 1);
                assert!(!read.receipt().uncertain);
                assert!(read.run_recovery_authority(source, cancellation).is_err());
                assert!(source.root.join("profiles").join(JOURNAL).is_file());
                read.close_continuation().unwrap();
                assert!(lock_free(&source.root.join(LOCK_PATH)));
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
}
