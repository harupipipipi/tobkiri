use super::*;
use serde_json::{Value, json};
use std::{
    fs,
    os::unix::fs::{PermissionsExt, symlink},
    sync::{
        Barrier,
        atomic::{AtomicU64, Ordering},
        mpsc,
    },
    thread,
};

const PROFILE: &str = "prof_original_calendar";
const LEGACY_SCHEMA: &str = "
    CREATE TABLE IF NOT EXISTS jobs (job_key TEXT PRIMARY KEY, digest TEXT NOT NULL, receipt TEXT);
    CREATE TABLE IF NOT EXISTS destinations (job_key TEXT PRIMARY KEY, intent_digest TEXT NOT NULL, target_id TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS sources (job_key TEXT NOT NULL, turn_id TEXT NOT NULL, source TEXT NOT NULL, PRIMARY KEY(job_key, turn_id));
";

struct Budget {
    limit: u64,
    used: AtomicU64,
}

impl AllocationBudget for Budget {
    fn reserve(&self, request: AllocationRequest) -> std::result::Result<(), AllocationError> {
        self.used
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |used| {
                used.checked_add(request.bytes)
                    .filter(|used| *used <= self.limit)
            })
            .map(drop)
            .map_err(|_| AllocationError::Limit)
    }

    fn release(&self, bytes: u64) {
        assert!(self.used.fetch_sub(bytes, Ordering::AcqRel) >= bytes);
    }
}

struct Fixture {
    _temp: tempfile::TempDir,
    root: DataRoot,
    profile: ProfileId,
    path: PathBuf,
    budget: Arc<dyn AllocationBudget>,
}

impl Fixture {
    fn new() -> Self {
        let temp = tempfile::tempdir().unwrap();
        let root = DataRoot::open_existing(temp.path().canonicalize().unwrap()).unwrap();
        let profile = ProfileId::parse(PROFILE).unwrap();
        let path = root.path().join(database_path(&profile));
        Self {
            _temp: temp,
            root,
            profile,
            path,
            budget: Arc::new(Budget {
                limit: 64 * 1024 * 1024,
                used: AtomicU64::new(0),
            }),
        }
    }

    fn legacy(&self) -> Connection {
        fs::create_dir_all(self.path.parent().unwrap()).unwrap();
        let db = Connection::open(&self.path).unwrap();
        db.execute_batch(LEGACY_SCHEMA).unwrap();
        db
    }

    fn call(&self, op: Operation, frame: Value) -> Result<Value> {
        self.guarded(op, frame, |_| Ok(()))
    }

    fn guarded(
        &self,
        op: Operation,
        frame: Value,
        guard: impl FnMut(OriginalSavedTurnCheck) -> Result<()>,
    ) -> Result<Value> {
        let frame = serde_json::to_vec(&frame).unwrap();
        let bytes = self.raw(op, &frame, Instant::now() + Duration::from_secs(5), guard)?;
        let value: Value = serde_json::from_slice(bytes.as_bytes()).unwrap();
        assert_eq!(bytes.as_bytes(), serde_json::to_vec(&value).unwrap());
        Ok(value["value"].clone())
    }

    fn raw(
        &self,
        op: Operation,
        frame: &[u8],
        deadline: Instant,
        guard: impl FnMut(OriginalSavedTurnCheck) -> Result<()>,
    ) -> Result<CoreBuffer> {
        execute_original_scheduled_source_ledger(
            &self.root,
            &self.profile,
            op,
            frame,
            &self.budget,
            deadline,
            guard,
        )
    }

    fn admit(&self) {
        assert_eq!(
            self.call(Operation::Admit, json!({"envelope": {}, "key": "job"}))
                .unwrap(),
            Value::Null
        );
    }
}

fn assert_error<T: std::fmt::Debug>(result: Result<T>, expected: Error) {
    assert_eq!(result.unwrap_err().downcast_ref::<Error>(), Some(&expected));
}

#[test]
fn six_operations_replay_their_exact_value_in_the_original_file() {
    let ledger = Fixture::new();
    let no_publish = |phase| {
        assert_eq!(phase, Live);
        Ok(())
    };
    let key = json!({"key": "job"});
    let admit = json!({"envelope": {"message": "e\u{301}ﬀ😀"}, "key": "job"});
    let recover = json!({"key": "job", "turn_id": "calendar:one/2"});
    assert_eq!(
        ledger
            .guarded(Operation::HasSource, key.clone(), no_publish)
            .unwrap(),
        false
    );
    assert_eq!(
        ledger
            .guarded(Operation::RecoverSource, recover.clone(), no_publish)
            .unwrap(),
        Value::Null
    );
    assert!(!ledger.root.path().join("packs").exists());
    assert_eq!(
        ledger.call(Operation::Admit, admit.clone()).unwrap(),
        Value::Null
    );
    assert_eq!(
        ledger
            .guarded(Operation::Admit, admit.clone(), no_publish)
            .unwrap(),
        Value::Null
    );
    assert_error(
        ledger.call(
            Operation::Admit,
            json!({"envelope": {"message": "éﬀ😀"}, "key": "job"}),
        ),
        Error::Denied,
    );

    let source = json!({"request": {"turn_id": "calendar:one/2"}, "source_revision": 1});
    let bind = json!({"key": "job", "source": source});
    assert_eq!(
        ledger.call(Operation::BindSource, bind.clone()).unwrap(),
        source
    );
    assert_eq!(
        ledger
            .guarded(Operation::BindSource, bind, no_publish)
            .unwrap(),
        source
    );
    assert_eq!(
        ledger
            .guarded(Operation::HasSource, key, no_publish)
            .unwrap(),
        true
    );
    assert_eq!(
        ledger
            .guarded(Operation::RecoverSource, recover, no_publish)
            .unwrap(),
        source
    );
    assert_error(
        ledger.call(
            Operation::BindSource,
            json!({
                "key": "job",
                "source": {"request": {"turn_id": "calendar:one/2"}, "source_revision": 2}
            }),
        ),
        Error::Denied,
    );
    assert_error(
        ledger.call(
            Operation::BindSource,
            json!({
                "key": "unknown",
                "source": {"turn_id": "calendar:one/2"}
            }),
        ),
        Error::Denied,
    );

    // The original method does not require a jobs row for binding a destination.
    let destination =
        json!({"key": "other", "intent": {"message": "hello"}, "target": "conv:/one"});
    assert_eq!(
        ledger
            .call(Operation::BindDestination, destination.clone())
            .unwrap(),
        "conv:/one"
    );
    assert_eq!(
        ledger
            .guarded(Operation::BindDestination, destination, no_publish)
            .unwrap(),
        "conv:/one"
    );
    assert_error(
        ledger.call(
            Operation::BindDestination,
            json!({
                "key": "other", "intent": {"message": "hello"}, "target": "conv:two"
            }),
        ),
        Error::Denied,
    );

    assert_error(
        ledger.call(Operation::Finish, json!({"key": "unknown", "receipt": {}})),
        Error::Denied,
    );
    let finish = json!({"key": "job", "receipt": {"status": "failed"}});
    assert_eq!(
        ledger.call(Operation::Finish, finish.clone()).unwrap(),
        finish["receipt"]
    );
    assert_eq!(
        ledger
            .guarded(Operation::Finish, finish.clone(), no_publish)
            .unwrap(),
        finish["receipt"]
    );
    assert_eq!(
        ledger
            .guarded(Operation::Admit, admit.clone(), no_publish)
            .unwrap(),
        finish["receipt"]
    );
    let replacement = json!({"key": "job", "receipt": {"status": "cancelled"}});
    assert_eq!(
        ledger.call(Operation::Finish, replacement.clone()).unwrap(),
        replacement["receipt"]
    );
    assert_eq!(
        ledger.guarded(Operation::Admit, admit, no_publish).unwrap(),
        replacement["receipt"]
    );
    assert!(ledger.path.is_file());
    let mode: String = ledger
        .legacy()
        .query_row("PRAGMA journal_mode", [], |row| row.get(0))
        .unwrap();
    assert_eq!(mode, "delete");
}

#[test]
fn preexisting_python_layout_and_semantic_json_remain_readable_and_writable() {
    let ledger = Fixture::new();
    let legacy = ledger.legacy();
    // Python canonical_digest({"message": "e\u0301ﬀ😀", "number": 9007199254740991}).
    let digest = "sha256:1d6907d087ea947cc8c7b44161edc61c4e22b1273278ffc31a3427ae6e5faed8";
    legacy
        .execute(
            "INSERT INTO jobs VALUES (?1,?2,?3)",
            params!["job", digest, r#"{ "z": true, "a": "\u0065\u0301" }"#],
        )
        .unwrap();
    legacy
        .execute(
            "INSERT INTO sources VALUES (?1,?2,?3)",
            params![
                "job",
                "a/b",
                r#"{ "turn_id": "a\/b", "data": "\u0065\u0301" }"#
            ],
        )
        .unwrap();
    let admit = json!({"envelope": {"message": "e\u{301}ﬀ😀", "number": 9_007_199_254_740_991_i64}, "key": "job"});
    assert_eq!(
        ledger.call(Operation::Admit, admit).unwrap(),
        json!({"a": "e\u{301}", "z": true})
    );
    let source = json!({"data": "e\u{301}", "turn_id": "a/b"});
    assert_eq!(
        ledger
            .call(
                Operation::BindSource,
                json!({"key": "job", "source": source})
            )
            .unwrap(),
        source
    );
    assert_eq!(
        legacy
            .query_row("SELECT source FROM sources", [], |row| row
                .get::<_, String>(0))
            .unwrap(),
        r#"{ "turn_id": "a\/b", "data": "\u0065\u0301" }"#
    );
    ledger
        .call(
            Operation::Finish,
            json!({"key": "job", "receipt": {"z": true, "a": "e\u{301}"}}),
        )
        .unwrap();
    assert_eq!(
        legacy
            .query_row("SELECT receipt FROM jobs", [], |row| row
                .get::<_, String>(0))
            .unwrap(),
        "{\"a\":\"e\u{301}\",\"z\":true}"
    );
}

#[test]
fn invalid_compact_json_and_bounded_input_never_create_the_ledger() {
    let mut ledger = Fixture::new();
    for frame in [
        &br#"{"key":"job","envelope":{}}"#[..],
        br#"{"envelope":{},"key":"job","key":"job"}"#,
        br#"{"envelope":{"float":1.0},"key":"job"}"#,
        br#"{"envelope":{"unsafe":9007199254740992},"key":"job"}"#,
        r#"{"envelope":{},"key":"é"}"#.as_bytes(),
        br#"{"envelope":{},"extra":0,"key":"job"}"#,
        br#"{"envelope":null,"key":"job"}"#,
    ] {
        assert_error(
            ledger.raw(
                Operation::Admit,
                frame,
                Instant::now() + LOCK_TIMEOUT,
                |_| Ok(()),
            ),
            Error::Invalid,
        );
    }
    assert_error(
        ledger.call(
            Operation::BindSource,
            json!({"key":"job","source":{"turn_id":"ok","request":null}}),
        ),
        Error::Invalid,
    );
    assert_error(
        ledger.call(
            Operation::BindSource,
            json!({"key":"job","source":{"turn_id":"ok","body":"a".repeat(RECORD_LIMIT)}}),
        ),
        Error::Invalid,
    );
    assert_error(
        ledger.call(
            Operation::Finish,
            json!({"key":"job","receipt":{"body":"a".repeat(RECORD_LIMIT)}}),
        ),
        Error::Invalid,
    );
    assert_error(
        ledger.call(
            Operation::Admit,
            json!({"key":"job","envelope":{"body":"a".repeat(INPUT_LIMIT)}}),
        ),
        Error::Invalid,
    );
    ledger.budget = Arc::new(Budget {
        limit: 4096,
        used: AtomicU64::new(0),
    });
    assert_error(
        ledger.call(Operation::Admit, json!({"key":"job","envelope":{}})),
        Error::Capacity,
    );
    assert!(!ledger.root.path().join("packs").exists());
}

#[test]
fn exact_record_limit_and_wire_operations_stay_compatible() {
    assert_eq!(
        [
            Operation::Admit,
            Operation::RecoverSource,
            Operation::BindSource,
            Operation::HasSource,
            Operation::BindDestination,
            Operation::Finish,
        ]
        .map(Operation::operation_id),
        [
            "admit",
            "recover-source",
            "bind-source",
            "has-source",
            "bind-destination",
            "finish"
        ]
    );
    let ledger = Fixture::new();
    ledger.admit();
    let body_size = RECORD_LIMIT - br#"{"body":"","turn_id":"turn"}"#.len();
    let source = json!({"body": "a".repeat(body_size), "turn_id": "turn"});
    assert_eq!(
        ledger
            .call(
                Operation::BindSource,
                json!({"key": "job", "source": source})
            )
            .unwrap(),
        source
    );
    let body_size = RECORD_LIMIT - br#"{"body":""}"#.len();
    let receipt = json!({"body": "a".repeat(body_size)});
    assert_eq!(
        ledger
            .call(Operation::Finish, json!({"key": "job", "receipt": receipt}))
            .unwrap(),
        receipt
    );
    assert_eq!(
        ledger
            .call(Operation::Admit, json!({"envelope": {}, "key": "job"}))
            .unwrap(),
        receipt
    );
}

#[test]
fn rejected_publish_rolls_back_initialization_and_every_changed_row() {
    let ledger = Fixture::new();
    let mut publishes = 0;
    let mut deny_publish = |phase| {
        if phase == Publish {
            publishes += 1;
            return Err(Error::Denied.into());
        }
        Ok(())
    };
    // The descriptor-bound open may leave an empty file. Neither schema nor
    // an admitted job can have escaped the rolled-back transaction.
    assert_error(
        ledger.guarded(
            Operation::Admit,
            json!({"envelope":{},"key":"job"}),
            &mut deny_publish,
        ),
        Error::Denied,
    );
    let db = Connection::open(&ledger.path).unwrap();
    assert_eq!(
        db.query_row("SELECT count(*) FROM sqlite_schema", [], |row| row
            .get::<_, i64>(0))
            .unwrap(),
        0
    );
    ledger.admit();
    for (op, frame) in [
        (
            Operation::BindSource,
            json!({"key":"job","source":{"turn_id":"turn"}}),
        ),
        (
            Operation::BindDestination,
            json!({"intent":{},"key":"job","target":"target"}),
        ),
        (
            Operation::Finish,
            json!({"key":"job","receipt":{"status":"completed"}}),
        ),
    ] {
        assert_error(ledger.guarded(op, frame, &mut deny_publish), Error::Denied);
    }
    assert_eq!(publishes, 4);
    for table in ["sources", "destinations"] {
        let count: i64 = db
            .query_row(&format!("SELECT count(*) FROM {table}"), [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(count, 0);
    }
    assert_eq!(
        db.query_row("SELECT receipt FROM jobs", [], |row| row
            .get::<_, Option<String>>(0))
            .unwrap(),
        None
    );
}

#[test]
fn rollback_journal_readers_hold_publish_until_the_commit_lock() {
    let ledger = Fixture::new();
    ledger.admit();
    let reader = ledger.legacy();
    reader.execute_batch("BEGIN; SELECT * FROM jobs").unwrap();
    let (send, receive) = mpsc::channel();
    thread::scope(|workers| {
        let writer = workers.spawn(|| {
            let mut live_checks = 0;
            ledger.guarded(
                Operation::Finish,
                json!({"key":"job","receipt":{"status":"completed"}}),
                |phase| {
                    match phase {
                        Live => {
                            live_checks += 1;
                            if live_checks == 6 {
                                send.send(Live).unwrap();
                            }
                        }
                        Publish => {
                            send.send(Publish).unwrap();
                            return Err(Error::Denied.into());
                        }
                    }
                    Ok(())
                },
            )
        });
        let before_release = receive.recv_timeout(Duration::from_secs(2));
        reader.execute_batch("ROLLBACK").unwrap();
        assert_eq!(before_release.unwrap(), Live);
        assert_eq!(
            receive.recv_timeout(Duration::from_secs(2)).unwrap(),
            Publish
        );
        assert_error(writer.join().unwrap(), Error::Denied);
    });
    assert_eq!(
        ledger
            .call(Operation::Admit, json!({"envelope":{},"key":"job"}))
            .unwrap(),
        Value::Null
    );
}

#[test]
fn competing_writers_keep_the_first_source_and_replay_identical_attempts() {
    let ledger = Fixture::new();
    ledger.admit();
    let barrier = Barrier::new(2);
    let results = thread::scope(|workers| {
        let first = workers.spawn(|| {
            barrier.wait();
            ledger.call(
                Operation::BindSource,
                json!({"key":"job","source":{"turn_id":"turn","revision":1}}),
            )
        });
        let second = workers.spawn(|| {
            barrier.wait();
            ledger.call(
                Operation::BindSource,
                json!({"key":"job","source":{"turn_id":"turn","revision":2}}),
            )
        });
        [first.join().unwrap(), second.join().unwrap()]
    });
    let mut winners = results.iter().filter_map(|result| result.as_ref().ok());
    let winner = winners.next().unwrap();
    assert_eq!(winners.count(), 0);
    assert_eq!(
        results
            .iter()
            .filter_map(|x| x.as_ref().err())
            .next()
            .unwrap()
            .downcast_ref::<Error>(),
        Some(&Error::Denied)
    );
    assert_eq!(
        &ledger
            .call(
                Operation::RecoverSource,
                json!({"key":"job","turn_id":"turn"})
            )
            .unwrap(),
        winner
    );
    assert_eq!(
        &ledger
            .guarded(
                Operation::BindSource,
                json!({"key":"job","source":winner}),
                |phase| {
                    assert_eq!(phase, Live);
                    Ok(())
                }
            )
            .unwrap(),
        winner
    );
}

#[test]
fn reads_accept_read_only_files_and_reject_mismatched_profile_or_symlink() {
    let ledger = Fixture::new();
    ledger.admit();
    ledger
        .call(
            Operation::BindSource,
            json!({"key":"job","source":{"turn_id":"turn"}}),
        )
        .unwrap();
    let before = fs::read(&ledger.path).unwrap();
    fs::set_permissions(&ledger.path, fs::Permissions::from_mode(0o400)).unwrap();
    assert_eq!(
        ledger
            .call(Operation::HasSource, json!({"key":"job"}))
            .unwrap(),
        true
    );
    assert_eq!(fs::read(&ledger.path).unwrap(), before);
    assert_eq!(
        fs::read_dir(ledger.path.parent().unwrap()).unwrap().count(),
        1
    );

    let other_profile = ProfileId::parse("prof_different_calendar").unwrap();
    let empty = execute_original_scheduled_source_ledger(
        &ledger.root,
        &other_profile,
        Operation::HasSource,
        br#"{"key":"job"}"#,
        &ledger.budget,
        Instant::now() + Duration::from_secs(5),
        |phase| {
            assert_eq!(phase, Live);
            Ok(())
        },
    )
    .unwrap();
    assert_eq!(empty.as_bytes(), br#"{"value":false}"#);
    assert!(
        !ledger
            .root
            .path()
            .join(database_path(&other_profile))
            .exists()
    );

    let other = Fixture::new();
    fs::create_dir_all(other.path.parent().unwrap().parent().unwrap()).unwrap();
    symlink(ledger.path.parent().unwrap(), other.path.parent().unwrap()).unwrap();
    assert!(
        other
            .call(Operation::HasSource, json!({"key":"job"}))
            .is_err()
    );
    assert!(
        other
            .call(Operation::Admit, json!({"envelope":{},"key":"new"}))
            .is_err()
    );
    fs::remove_file(other.path.parent().unwrap()).unwrap();
    fs::create_dir(other.path.parent().unwrap()).unwrap();
    symlink(&ledger.path, &other.path).unwrap();
    assert!(
        other
            .call(Operation::HasSource, json!({"key":"job"}))
            .is_err()
    );
    fs::remove_file(&other.path).unwrap();
    assert_eq!(
        other
            .call(Operation::HasSource, json!({"key":"job"}))
            .unwrap(),
        false
    );
    assert_eq!(fs::read(&ledger.path).unwrap(), before);
}

#[test]
fn rejects_views_triggers_and_incompatible_table_definitions() {
    for extra_schema in [
        "CREATE VIEW fake_view AS SELECT * FROM jobs;",
        "CREATE TRIGGER fake_trigger AFTER INSERT ON jobs BEGIN DELETE FROM jobs; END;",
        "DROP TABLE sources; CREATE TABLE sources (job_key TEXT, turn_id TEXT, source TEXT);",
    ] {
        let ledger = Fixture::new();
        let db = ledger.legacy();
        db.execute_batch(extra_schema).unwrap();
        for (operation, frame) in [
            (Operation::HasSource, json!({"key":"job"})),
            (Operation::Admit, json!({"envelope":{},"key":"job"})),
        ] {
            assert!(
                ledger
                    .guarded(operation, frame, |phase| {
                        assert_eq!(phase, Live);
                        Ok(())
                    })
                    .is_err()
            );
        }
        let count: i64 = db
            .query_row("SELECT count(*) FROM jobs", [], |row| row.get(0))
            .unwrap();
        assert_eq!(count, 0);
    }
}
