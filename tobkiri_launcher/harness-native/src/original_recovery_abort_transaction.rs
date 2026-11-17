//! The original AuthorityStore's recovery-only transition to "aborted".
use super::incomplete_reservations::{EPOCH_SQL, PENDING_SQL, fetch_one, wrap_sqlite};
use super::*;
use rusqlite::params;

const STAGE: OriginalAuthorityStage = OriginalAuthorityStage::ActivationTransition;
const FIELDS: [&str; 8] = [
    "reservation_id",
    "activation_id",
    "profile_id",
    "plan_digest",
    "profile_authority_digest",
    "security_epoch",
    "fencing_token",
    "state",
];
const EVENT_PREFIX: &str = "activation-aborted-";

/// Abort a single reservation using the Store and locks held by recovery.
/// The state fence and authoritative audit append share one SQLite transaction.
pub(super) fn abort<S: OriginalAuthorityOwner>(
    store: &Store<'_, '_, S>,
    reservation_id: &str,
    expected_state: &str,
) -> Result<()> {
    store.check_process(STAGE)?;
    if !matches!(
        expected_state,
        "prepared" | "ready_without_authority" | "committing"
    ) {
        return Err(raw(
            "builtins.ValueError",
            "invalid activation state transition",
        ));
    }

    let op = store.op;
    let db = store
        .connection(STAGE)
        .map_err(|e| wrap_sqlite(e, "authority database is unavailable"))?;
    let result = (|| {
        db.execute("BEGIN IMMEDIATE", [])?;
        let lost_fence = || denied("activation reservation lost its state fence");
        let mut row = fetch_one(op, &db, STAGE, PENDING_SQL, Some(reservation_id), FIELDS)?
            .ok_or_else(lost_fence)?;
        let [payload @ .., state] = &mut row.value;
        if value_text(state.as_ref().ok_or_else(missing_column)?) != Some(expected_state) {
            return Err(lost_fence());
        }

        // CPython still fetches the epoch row (including SQLite conversion)
        // before aborting. It skips the existence and integer/epoch checks.
        let _ = fetch_one(op, &db, STAGE, EPOCH_SQL, None, [])?;
        {
            let binding_bytes = reservation_id
                .len()
                .checked_add(expected_state.len() + "aborted".len())
                .ok_or_else(|| OriginalAuthorityError::capacity(STAGE))?;
            let _binding = op.reserve(STAGE, binding_bytes)?;
            let updated = db.execute(
                "UPDATE activation_reservations SET state=?, updated_at=? WHERE reservation_id=? AND state=?",
                params!["aborted", store.clock(), reservation_id, expected_state],
            )?;
            if updated != 1 {
                return Err(denied("activation transition lost a race"));
            }
        }

        let id_bytes = EVENT_PREFIX
            .len()
            .checked_add(reservation_id.len())
            .ok_or_else(|| OriginalAuthorityError::capacity(STAGE))?;
        // Moved strings retain their row's reservation. Reserve the event
        // ID and the fixed canonical object's keys and map storage here.
        let _payload_capacity = op.reserve(
            STAGE,
            id_bytes
                .checked_add(2048)
                .ok_or_else(|| OriginalAuthorityError::capacity(STAGE))?,
        )?;
        let mut entries = BTreeMap::new();
        for (name, field) in FIELDS.into_iter().zip(payload) {
            entries.insert(
                name.into(),
                canonical(field.take().ok_or_else(missing_column)?)?,
            );
        }
        let mut event_id = String::with_capacity(id_bytes);
        event_id.push_str(EVENT_PREFIX);
        event_id.push_str(reservation_id);
        schema::append_audit(
            store,
            &db,
            schema::AuditKind::Activation,
            &event_id,
            "aborted",
            CanonValue::Obj(entries),
        )?;
        db.commit()
    })();
    db.complete(result)
        .map_err(|e| wrap_sqlite(e, "activation transition failed"))
}

fn canonical(value: Value) -> Result<CanonValue> {
    Ok(match value {
        Value::Null => CanonValue::Null,
        Value::Integer(number) => CanonValue::Int(number),
        Value::Real(number) if number.is_finite() => CanonValue::Float(number),
        Value::Real(_) => {
            return Err(raw(
                "builtins.ValueError",
                "Out of range float values are not JSON compliant",
            ));
        }
        Value::Text(text) => CanonValue::Str(text),
        Value::Blob(_) => {
            return Err(raw(
                "builtins.TypeError",
                "Object of type bytes is not JSON serializable",
            ));
        }
    })
}

fn missing_column() -> OriginalAuthorityError {
    raw("builtins.IndexError", "No item with that key")
}

fn denied(message: &'static str) -> OriginalAuthorityError {
    raw("core_runtime.authority.v4_models.AuthorityDenied", message)
}

fn raw(class: &'static str, message: &'static str) -> OriginalAuthorityError {
    let mut error = json::raw_error(class, message);
    error.stage = STAGE;
    error
}

#[cfg(test)]
mod tests {
    use super::*;
    use tobkiri_core::RuntimeOwner;

    const CLOCK: f64 = 123.25;
    const BEFORE: f64 = 23.75;

    struct Source(PathBuf);
    impl OriginalAuthorityOwner for Source {
        fn with_original_user_data<R>(&self, use_root: impl FnOnce(&Path) -> R) -> Result<R> {
            Ok(use_root(&self.0))
        }

        fn check_original_owner(&self) -> Result<()> {
            Ok(())
        }
    }

    fn with_store(body: impl FnOnce(&Store<'_, '_, Source>)) {
        let directory = tempfile::tempdir().unwrap();
        let source = Source(directory.path().join("selected"));
        fs::create_dir(&source.0).unwrap();
        let owner = RuntimeOwner::open(directory.path().join("core")).unwrap();
        let invocation = owner.begin_native_setup().unwrap();
        let cancellation = CoreCancellation::new_with_control(invocation.control()).unwrap();
        let fixture = super::super::super::tests::Fixture {
            clock: CLOCK,
            mutation: BTreeMap::new(),
            after_close_fault: false,
        };
        let op = Operation {
            owner: &source,
            invocation: &invocation,
            cancellation: &cancellation,
            deadline: invocation.deadline().unwrap(),
            pid: std::process::id(),
            cleanup: Arc::new(CleanupState::new(
                invocation
                    .control()
                    .reserve(ControlKind::Context, 4096)
                    .unwrap(),
            )),
            fixture: Some(&fixture),
        };
        with_registered_store(&op, &source.0, |store| {
            body(store);
            Ok(())
        })
        .unwrap();
        assert!(op.cleanup.verified());
    }

    fn with_db<T>(store: &Store<'_, '_, Source>, body: impl FnOnce(&Connection) -> T) -> T {
        let db = Connection::open(&store.path).unwrap();
        let outcome = body(&db);
        db.close().unwrap();
        outcome
    }

    fn insert(db: &Connection, id: &str, expected: &str, token: i64) {
        db.execute(
            "INSERT INTO activation_reservations(
                 reservation_id, activation_id, profile_id, plan_digest,
                 profile_authority_digest, security_epoch, fencing_token,
                 state, created_at, updated_at)
             VALUES (?, 'activation:test', 'profile', 'plan', 'authority', 37, ?, ?, 1.0, ?)",
            params![id, token, expected, BEFORE],
        )
        .unwrap();
    }

    fn state(db: &Connection, id: &str) -> (String, f64) {
        db.query_row(
            "SELECT state, updated_at FROM activation_reservations WHERE reservation_id=?",
            [id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap()
    }

    fn audit_count(db: &Connection) -> i64 {
        db.query_row("SELECT count(*) FROM authority_audit", [], |row| row.get(0))
            .unwrap()
    }

    #[tokio::test(flavor = "current_thread")]
    async fn abort_chains_encrypted_audit_for_each_recoverable_state() {
        with_store(|store| {
            let mut previous = format!("sha256:{}", "0".repeat(64));
            for (token, expected, epoch_change) in [
                (
                    1,
                    "prepared",
                    "UPDATE authority_meta SET value='2' WHERE key='security_epoch'",
                ),
                (
                    2,
                    "ready_without_authority",
                    "UPDATE authority_meta SET value='not an int' WHERE key='security_epoch'",
                ),
                (
                    3,
                    "committing",
                    "DELETE FROM authority_meta WHERE key='security_epoch'",
                ),
            ] {
                let id = format!("reservation:{token}");
                with_db(store, |db| {
                    insert(db, &id, expected, token);
                    db.execute(epoch_change, []).unwrap();
                });
                abort(store, &id, expected).unwrap();

                let payload = format!(
                    r#"{{"activation_id":"activation:test","fencing_token":{token},"plan_digest":"plan","profile_authority_digest":"authority","profile_id":"profile","reservation_id":"{id}","security_epoch":37}}"#
                );
                let event = format!(
                    r#"{{"created_at":123.25,"event_id":"activation-aborted-{id}","event_state":"aborted","event_type":"activation","payload":{payload},"previous_digest":"{previous}"}}"#
                );
                let digest = Digest::hash(event.as_bytes());
                with_db(store, |db| {
                    assert_eq!(state(db, &id), ("aborted".to_owned(), CLOCK));
                    assert_eq!(audit_count(db), token);
                    db.query_row(
                        "SELECT event_type, event_state, previous_digest, event_digest,
                                encrypted_payload, created_at
                         FROM authority_audit WHERE event_id=?",
                        [format!("activation-aborted-{id}")],
                        |row| {
                            assert_eq!(row.get::<_, String>(0)?, "activation");
                            assert_eq!(row.get::<_, String>(1)?, "aborted");
                            assert_eq!(row.get::<_, String>(2)?, previous);
                            assert_eq!(row.get::<_, String>(3)?, digest.as_str());
                            let encrypted: Vec<u8> = row.get(4)?;
                            let plaintext = store.keys.decrypt(store.op, &encrypted).unwrap();
                            assert_eq!(plaintext.as_slice(), payload.as_bytes());
                            assert_eq!(row.get::<_, f64>(5)?, CLOCK);
                            Ok(())
                        },
                    )
                    .unwrap();
                });
                previous = digest.as_str().into();
            }
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn abort_rolls_back_state_and_timestamp_when_audit_insert_fails() {
        with_store(|store| {
            let id = "reservation:1";
            with_db(store, |db| {
                insert(db, id, "prepared", 1);
                db.execute_batch(
                    "CREATE TRIGGER reject_abort_audit BEFORE INSERT ON authority_audit
                     WHEN NEW.event_id='activation-aborted-reservation:1'
                       AND (SELECT state FROM activation_reservations
                            WHERE reservation_id='reservation:1')='aborted'
                     BEGIN SELECT RAISE(ABORT, 'test rejects the audit insert'); END;",
                )
                .unwrap();
            });
            let error = abort(store, id, "prepared").unwrap_err();
            assert_eq!(
                error.class,
                "core_runtime.authority.store_contracts.AuditUnavailable"
            );
            assert_eq!(error.message, "authoritative audit append failed");
            assert!(error.suppressed);
            assert!(matches!(
                error.cause,
                Some(OriginalAuthorityCause::Python {
                    class: "sqlite3.IntegrityError",
                    message,
                    ..
                }) if message == "test rejects the audit insert"
            ));
            with_db(store, |db| {
                assert_eq!(state(db, id), ("prepared".to_owned(), BEFORE));
                assert_eq!(audit_count(db), 0);
                db.execute("DROP TRIGGER reject_abort_audit", []).unwrap();
            });
            abort(store, id, "prepared").unwrap();
            with_db(store, |db| {
                assert_eq!(state(db, id), ("aborted".to_owned(), CLOCK));
                assert_eq!(audit_count(db), 1);
            });
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn abort_checks_state_fence_before_reading_an_unreadable_epoch() {
        with_store(|store| {
            let id = "reservation:1";
            with_db(store, |db| {
                insert(db, id, "ready_without_authority", 1);
                db.execute(
                    "UPDATE authority_meta SET value=CAST(x'ff' AS TEXT) WHERE key='security_epoch'",
                    [],
                )
                .unwrap();
            });
            let error = abort(store, id, "prepared").unwrap_err();
            assert_eq!(
                error.class,
                "core_runtime.authority.v4_models.AuthorityDenied"
            );
            assert_eq!(error.message, "activation reservation lost its state fence");
            let error = abort(store, id, "ready_without_authority").unwrap_err();
            assert_eq!(
                error.class,
                "core_runtime.authority.store_contracts.AuthorityStoreError"
            );
            assert_eq!(error.message, "activation transition failed");
            assert!(matches!(
                error.cause,
                Some(OriginalAuthorityCause::Python {
                    class: "sqlite3.OperationalError",
                    message,
                    ..
                }) if message.starts_with("Could not decode to UTF-8 column 'value'")
            ));
            with_db(store, |db| {
                assert_eq!(
                    state(db, id),
                    ("ready_without_authority".to_owned(), BEFORE)
                );
                assert_eq!(audit_count(db), 0);
            });
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn abort_rolls_back_if_the_held_authority_key_changes_identity() {
        with_store(|store| {
            let id = "reservation:1";
            with_db(store, |db| insert(db, id, "committing", 1));
            let key = store.path.with_extension("key");
            let old_key = store.path.with_extension("old-key");
            fs::rename(&key, &old_key).unwrap();
            fs::copy(&old_key, &key).unwrap();

            let error = abort(store, id, "committing").unwrap_err();
            assert_eq!(error.stage, OriginalAuthorityStage::Key);
            assert_eq!(error.message, "authority encryption key path is unsafe");
            with_db(store, |db| {
                assert_eq!(state(db, id), ("committing".to_owned(), BEFORE));
                assert_eq!(audit_count(db), 0);
            });
        });
    }
}
