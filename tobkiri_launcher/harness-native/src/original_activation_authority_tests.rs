use super::*;
use tobkiri_gate::{CapacityLedger, GateError, NativeSetupLease};

struct Portable;
impl NativeSetupLease for Portable {
    fn validate(&self) -> std::result::Result<(), GateError> {
        Ok(())
    }
    fn allocation_domain(&self) -> Digest {
        Digest::hash(b"activation-authority-test-allocation")
    }
}
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
    let control = CoreControl::default();
    let account = CapacityLedger::new()
        .native_setup_account(&control, || Ok(Arc::new(Portable)))
        .unwrap();
    let invocation = account.begin().unwrap();
    let cancellation = CoreCancellation::new_with_control(&control).unwrap();
    let fixture = super::super::super::tests::Fixture {
        clock: 123.25,
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
    let remaining = account.snapshot().unwrap();
    assert_eq!((remaining.charged_bytes, remaining.reservations), (0, 0));
}
fn with_db<T>(store: &Store<'_, '_, Source>, body: impl FnOnce(&Connection) -> T) -> T {
    let db = Connection::open(&store.path).unwrap();
    let outcome = body(&db);
    db.close().unwrap();
    outcome
}
fn reserve_id(store: &Store<'_, '_, Source>, id: &str) -> Result<Funded<Reservation>> {
    reserve(
        store,
        ReservationInput {
            activation_id: id,
            profile_id: "profile:test",
            plan_digest: "plan:test",
            profile_authority_digest: "authority:test",
            security_epoch: 1,
        },
    )
}
fn activate(store: &Store<'_, '_, Source>, reservation_id: &str) {
    for (before, after) in [
        ("prepared", "ready_without_authority"),
        ("ready_without_authority", "committing"),
        ("committing", "active"),
    ] {
        transition(store, reservation_id, before, after).unwrap();
    }
}
fn state(db: &Connection, id: &str) -> String {
    db.query_row(
        "SELECT state FROM activation_reservations WHERE reservation_id=?",
        [id],
        |row| row.get(0),
    )
    .unwrap()
}
fn token(db: &Connection) -> String {
    db.query_row(
        "SELECT value FROM authority_meta WHERE key='activation_fencing_token'",
        [],
        |row| row.get(0),
    )
    .unwrap()
}
fn audit_count(db: &Connection) -> i64 {
    db.query_row("SELECT count(*) FROM authority_audit", [], |row| row.get(0))
        .unwrap()
}
fn assert_error(error: OriginalAuthorityError, class: &str, message: &str) {
    assert_eq!(error.stage, STAGE);
    assert_eq!(error.class, class);
    assert_eq!(error.message, message);
}

#[tokio::test(flavor = "current_thread")]
async fn activation_reserves_and_supersedes_with_authenticated_ordered_audit() {
    with_store(|store| {
        let old = reserve_id(store, "activation:old").unwrap();
        assert!(old.reservation_id.starts_with(ID_PREFIX));
        assert_eq!(old.reservation_id.len(), ID_PREFIX.len() + 32);
        assert_eq!(old.fencing_token, 1);
        activate(store, &old.reservation_id);
        let new = reserve_id(store, "activation:new").unwrap();
        assert_eq!(new.fencing_token, 2);
        activate(store, &new.reservation_id);
        with_db(store, |db| {
            assert_eq!(state(db, &old.reservation_id), "retired");
            assert_eq!(state(db, &new.reservation_id), "active");
            assert_eq!(token(db), "2");
            assert_eq!(audit_count(db), 9);
            let mut query = db.prepare("SELECT event_id, event_state, previous_digest, event_digest, encrypted_payload FROM authority_audit ORDER BY sequence").unwrap();
            let mut rows = query.query([]).unwrap();
            let mut previous = format!("sha256:{}", "0".repeat(64));
            let mut count = 0;
            while let Some(row) = rows.next().unwrap() {
                let event_id: String = row.get(0).unwrap();
                let event_state: String = row.get(1).unwrap();
                let actual_previous: String = row.get(2).unwrap();
                let actual_digest: String = row.get(3).unwrap();
                let encrypted: Vec<u8> = row.get(4).unwrap();
                let plaintext = store.keys.decrypt(store.op, &encrypted).unwrap();
                let payload: serde_json::Value = serde_json::from_slice(&plaintext).unwrap();
                assert_eq!(actual_previous, previous);
                if count == 7 {
                    assert_eq!(
                        event_id,
                        format!("activation-retired-{}", old.reservation_id)
                    );
                    assert_eq!(event_state, "retired");
                    assert_eq!(
                        payload,
                        serde_json::json!({
                            "reservation_id": old.reservation_id,
                            "activation_id": "activation:old",
                            "superseded_by": new.reservation_id,
                        })
                    );
                } else {
                    assert_eq!(payload["profile_id"], "profile:test");
                    assert_eq!(payload["security_epoch"], 1);
                    assert_eq!(payload["fencing_token"], if count < 4 { 1 } else { 2 });
                }
                let mut event = serde_json::Map::new();
                event.insert("created_at".into(), serde_json::json!(123.25));
                event.insert("event_id".into(), serde_json::json!(event_id));
                event.insert("event_state".into(), serde_json::json!(event_state));
                event.insert("event_type".into(), serde_json::json!("activation"));
                event.insert("payload".into(), payload);
                event.insert("previous_digest".into(), serde_json::json!(previous));
                assert_eq!(
                    Digest::hash(&serde_json::to_vec(&event).unwrap()).as_str(),
                    actual_digest
                );
                previous = actual_digest;
                count += 1;
            }
            assert_eq!(count, 9);
        });
    });
}

#[tokio::test(flavor = "current_thread")]
async fn aborted_identity_can_be_reserved_again_without_reusing_the_fence() {
    with_store(|store| {
        let first = reserve_id(store, "activation:same").unwrap();
        assert_error(
            reserve_id(store, "activation:same").err().unwrap(),
            "core_runtime.authority.v4_models.AuthorityDenied",
            "activation identity is already reserved",
        );
        with_db(store, |db| {
            assert_eq!(token(db), "1");
            assert_eq!(audit_count(db), 1);
        });
        transition(store, &first.reservation_id, "prepared", "aborted").unwrap();
        let second = reserve_id(store, "activation:same").unwrap();
        assert_ne!(first.reservation_id, second.reservation_id);
        assert_eq!(second.fencing_token, 2);
        with_db(store, |db| assert_eq!(audit_count(db), 3));
    });
}

#[tokio::test(flavor = "current_thread")]
async fn stale_epoch_denies_reservation_and_transition_but_allows_abort() {
    with_store(|store| {
        let first = reserve_id(store, "activation:old").unwrap();
        with_db(store, |db| {
            db.execute(
                "UPDATE authority_meta SET value='2' WHERE key='security_epoch'",
                [],
            )
            .unwrap();
        });
        assert_error(
            reserve_id(store, "activation:new").err().unwrap(),
            "core_runtime.authority.v4_models.AuthorityDenied",
            "activation has a stale SecurityEpoch",
        );
        assert_error(
            transition(
                store,
                &first.reservation_id,
                "prepared",
                "ready_without_authority",
            )
            .unwrap_err(),
            "core_runtime.authority.v4_models.AuthorityDenied",
            "activation reservation has a stale SecurityEpoch",
        );
        transition(store, &first.reservation_id, "prepared", "aborted").unwrap();
        with_db(store, |db| {
            assert_eq!(state(db, &first.reservation_id), "aborted");
            assert_eq!(token(db), "1");
            assert_eq!(audit_count(db), 2);
        });
    });
}

#[tokio::test(flavor = "current_thread")]
async fn reservation_audit_failure_rolls_back_insert_and_fencing_sequence() {
    with_store(|store| {
        with_db(store, |db| {
            db.execute_batch("CREATE TRIGGER reject_prepared BEFORE INSERT ON authority_audit WHEN NEW.event_state='prepared' BEGIN SELECT RAISE(ABORT, 'reject prepared audit'); END;").unwrap()
        });
        let error = reserve_id(store, "activation:test").err().unwrap();
        assert_eq!(
            error.class,
            "core_runtime.authority.store_contracts.AuditUnavailable"
        );
        assert_eq!(error.message, "authoritative audit append failed");
        assert!(
            matches!(error.cause, Some(OriginalAuthorityCause::Python { class: "sqlite3.IntegrityError", message, .. }) if message == "reject prepared audit")
        );
        with_db(store, |db| {
            assert_eq!(token(db), "0");
            assert_eq!(audit_count(db), 0);
            let rows: i64 = db
                .query_row("SELECT count(*) FROM activation_reservations", [], |row| {
                    row.get(0)
                })
                .unwrap();
            assert_eq!(rows, 0);
            db.execute("DROP TRIGGER reject_prepared", []).unwrap();
        });
        assert_eq!(
            reserve_id(store, "activation:test").unwrap().fencing_token,
            1
        );
    });
}

#[tokio::test(flavor = "current_thread")]
async fn changed_authority_key_identity_rolls_back_the_prepared_reservation() {
    with_store(|store| {
        let key = store.path.with_extension("key");
        let old_key = store.path.with_extension("old-key");
        fs::rename(&key, &old_key).unwrap();
        fs::copy(&old_key, &key).unwrap();
        let error = reserve_id(store, "activation:test").err().unwrap();
        assert_eq!(error.stage, OriginalAuthorityStage::Key);
        assert_eq!(error.message, "authority encryption key path is unsafe");
        with_db(store, |db| {
            assert_eq!(token(db), "0");
            assert_eq!(audit_count(db), 0);
            assert_eq!(
                db.query_row("SELECT count(*) FROM activation_reservations", [], |row| {
                    row.get::<_, i64>(0)
                })
                .unwrap(),
                0
            );
        });
    });
}

#[tokio::test(flavor = "current_thread")]
async fn fencing_constraint_failure_retains_the_original_integrity_cause() {
    with_store(|store| {
        let first = reserve_id(store, "activation:first").unwrap();
        with_db(store, |db| {
            db.execute(
                "UPDATE authority_meta SET value='0' WHERE key='activation_fencing_token'",
                [],
            )
            .unwrap()
        });
        let error = reserve_id(store, "activation:second").err().unwrap();
        assert_eq!(
            error.class,
            "core_runtime.authority.v4_models.AuthorityDenied"
        );
        assert_eq!(error.message, "activation identity was already reserved");
        assert!(error.suppressed);
        assert!(
            matches!(error.cause, Some(OriginalAuthorityCause::Python { class: "sqlite3.IntegrityError", message, .. }) if message == "UNIQUE constraint failed: activation_reservations.fencing_token")
        );
        with_db(store, |db| {
            assert_eq!(state(db, &first.reservation_id), "prepared");
            assert_eq!(token(db), "0");
            assert_eq!(audit_count(db), 1);
        });
    });
}

#[tokio::test(flavor = "current_thread")]
async fn active_audit_failure_rolls_back_supersession_and_all_appended_audits() {
    with_store(|store| {
        let old = reserve_id(store, "activation:old").unwrap();
        activate(store, &old.reservation_id);
        let new = reserve_id(store, "activation:new").unwrap();
        transition(
            store,
            &new.reservation_id,
            "prepared",
            "ready_without_authority",
        )
        .unwrap();
        transition(
            store,
            &new.reservation_id,
            "ready_without_authority",
            "committing",
        )
        .unwrap();
        with_db(store, |db| {
            db.execute_batch("CREATE TRIGGER reject_active BEFORE INSERT ON authority_audit WHEN NEW.event_state='active' BEGIN SELECT RAISE(ABORT, 'reject active audit'); END;").unwrap()
        });
        let error = transition(store, &new.reservation_id, "committing", "active").unwrap_err();
        assert_eq!(
            error.class,
            "core_runtime.authority.store_contracts.AuditUnavailable"
        );
        with_db(store, |db| {
            assert_eq!(state(db, &old.reservation_id), "active");
            assert_eq!(state(db, &new.reservation_id), "committing");
            assert_eq!(audit_count(db), 7);
            db.execute("DROP TRIGGER reject_active", []).unwrap();
        });
        transition(store, &new.reservation_id, "committing", "active").unwrap();
        with_db(store, |db| assert_eq!(audit_count(db), 9));
    });
}

#[tokio::test(flavor = "current_thread")]
async fn token_overflow_occurs_after_duplicate_check_and_before_persistent_changes() {
    with_store(|store| {
        let first = reserve_id(store, "activation:same").unwrap();
        with_db(store, |db| {
            db.execute("UPDATE authority_meta SET value='9223372036854775807' WHERE key='activation_fencing_token'", []).unwrap()
        });
        assert_error(
            reserve_id(store, "activation:same").err().unwrap(),
            "core_runtime.authority.v4_models.AuthorityDenied",
            "activation identity is already reserved",
        );
        assert_error(
            reserve_id(store, "activation:new").err().unwrap(),
            "builtins.OverflowError",
            "Python int too large to convert to SQLite INTEGER",
        );
        with_db(store, |db| {
            assert_eq!(state(db, &first.reservation_id), "prepared");
            assert_eq!(token(db), "9223372036854775807");
            assert_eq!(audit_count(db), 1);
            db.execute("UPDATE authority_meta SET value='-9223372036854775809' WHERE key='activation_fencing_token'", []).unwrap();
        });
        assert_eq!(
            reserve_id(store, "activation:negative")
                .unwrap()
                .fencing_token,
            i64::MIN
        );
    });
}

#[tokio::test(flavor = "current_thread")]
async fn transition_preserves_state_fence_and_original_invalid_epoch_error() {
    with_store(|store| {
        let first = reserve_id(store, "activation:test").unwrap();
        with_db(store, |db| {
            db.execute(
                "UPDATE authority_meta SET value='not an integer' WHERE key='security_epoch'",
                [],
            )
            .unwrap()
        });
        assert_error(
            transition(store, &first.reservation_id, "committing", "active").unwrap_err(),
            "core_runtime.authority.v4_models.AuthorityDenied",
            "activation reservation lost its state fence",
        );
        assert_error(
            transition(
                store,
                &first.reservation_id,
                "prepared",
                "ready_without_authority",
            )
            .unwrap_err(),
            "builtins.ValueError",
            "invalid literal for int() with base 10: 'not an integer'",
        );
        assert_error(
            transition(store, &first.reservation_id, "prepared", "active").unwrap_err(),
            "builtins.ValueError",
            "invalid activation state transition",
        );
        transition(store, &first.reservation_id, "prepared", "aborted").unwrap();
    });
}

#[test]
fn arbitrary_precision_fence_increment_matches_integer_arithmetic() {
    for (before, expected) in [
        ("0", "1"),
        ("9", "10"),
        ("99", "100"),
        ("-1", "0"),
        ("-10", "-9"),
        ("-1000", "-999"),
        ("-999", "-998"),
        ("9223372036854775807", "9223372036854775808"),
    ] {
        let mut value = before.to_owned();
        increment(&mut value);
        assert_eq!(value, expected);
    }
}

#[tokio::test(flavor = "current_thread")]
async fn invalid_integer_diagnostic_truncates_the_repr_including_its_quote() {
    with_store(|store| {
        for (value, repr) in [
            (
                Value::Text("z".repeat(250)),
                format!("'{}", "z".repeat(199)),
            ),
            (
                Value::Blob(vec![b'z'; 250]),
                format!("b'{}", "z".repeat(198)),
            ),
        ] {
            let error = integer_value(store.op, &value).err().unwrap();
            assert_error(
                error,
                "builtins.ValueError",
                &format!("invalid literal for int() with base 10: {repr}"),
            );
        }
        assert_eq!(
            integer_repr("\u{200b}\u{e000}\u{10ffff}"),
            "'\\u200b\\ue000\\U0010ffff'"
        );
        assert_eq!(integer_repr("é'\""), "'é\\'\"'");
    });
}
