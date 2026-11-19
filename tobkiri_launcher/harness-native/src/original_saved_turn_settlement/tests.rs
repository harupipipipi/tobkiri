#![cfg(unix)]

use super::*;
use serde_json::{Value, json};
use std::{
    fs,
    os::unix::{fs::MetadataExt, process::ExitStatusExt},
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        mpsc,
    },
    thread,
};
use tobkiri_canon::AllocationError;

const PROFILE: &str = "prof_original_turn";
const TURN: &str = "turn-original";
const REQUEST_ID: &str = "saved-turn.original";
const CONVERSATION: &str = "conversation-original";
const SOURCE_DIGEST: &str =
    "sha256:1111111111111111111111111111111111111111111111111111111111111111";
const RUNTIME_BYTES: u64 = 64 * 1024 * 1024;
const WAIT: Duration = Duration::from_secs(10);

const ORIGINAL_TURNS_SCHEMA: &str = "CREATE TABLE turns (
    id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
    updated_at INTEGER NOT NULL, body TEXT NOT NULL
)";

#[derive(Default)]
struct BoundedBudget(AtomicU64);

impl AllocationBudget for BoundedBudget {
    fn reserve(&self, request: AllocationRequest) -> std::result::Result<(), AllocationError> {
        self.0
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |occupied| {
                occupied
                    .checked_add(request.bytes)
                    .filter(|total| *total <= RUNTIME_BYTES)
            })
            .map(|_| ())
            .map_err(|_| AllocationError::Limit)
    }

    fn release(&self, bytes: u64) {
        self.0
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |occupied| {
                occupied.checked_sub(bytes)
            })
            .expect("allocation owner released more capacity than it reserved");
    }
}

struct OriginalStore {
    _temp: tempfile::TempDir,
    root: DataRoot,
    profile: ProfileId,
    database: PathBuf,
}

impl OriginalStore {
    fn new() -> Self {
        let temp = tempfile::tempdir().unwrap();
        let root = DataRoot::open_existing(temp.path().canonicalize().unwrap()).unwrap();
        let profile = ProfileId::parse(PROFILE).unwrap();
        let database = root.path().join(database_path(&profile));
        Self {
            _temp: temp,
            root,
            profile,
            database,
        }
    }

    fn initialize(&self, original: &Value) {
        fs::create_dir_all(self.database.parent().unwrap()).unwrap();
        let connection = Connection::open(&self.database).unwrap();
        connection.execute_batch(ORIGINAL_TURNS_SCHEMA).unwrap();
        connection
            .execute(
                "INSERT INTO turns VALUES (?1, ?2, ?3, ?4)",
                params![
                    TURN,
                    REQUEST_ID,
                    original["updated_at"].as_i64().unwrap(),
                    original.to_string()
                ],
            )
            .unwrap();
        connection
            .execute(
                "INSERT INTO turns VALUES (?1, ?2, ?3, ?4)",
                params!["unrelated", "request-other", 50, "{}"],
            )
            .unwrap();
    }

    fn row(&self) -> (String, i64, String) {
        let connection = Connection::open(&self.database).unwrap();
        connection
            .query_row(
                "SELECT request_id, updated_at, body FROM turns WHERE id = ?1",
                [TURN],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap()
    }

    fn settle(
        &self,
        expected_revision: Option<u64>,
        budget: &Arc<dyn AllocationBudget>,
        guard: impl FnMut() -> Result<()> + Send + 'static,
        clock: impl FnMut() -> Result<i64>,
    ) -> Result<CoreBuffer> {
        let reference = result_reference().to_string();
        settle_legacy_saved_turn_with_clock(
            &self.root,
            &self.profile,
            &request(expected_revision, reference.as_bytes()),
            budget,
            Instant::now() + WAIT,
            guard,
            clock,
        )
    }

    fn identity(&self) -> (u64, u64) {
        let metadata = fs::metadata(&self.database).unwrap();
        (metadata.dev(), metadata.ino())
    }

    fn journal(&self) -> PathBuf {
        self.database.with_file_name("turns.sqlite3-journal")
    }
}

fn request(
    expected_revision: Option<u64>,
    reference: &[u8],
) -> LegacySavedTurnSettlementRequest<'_> {
    LegacySavedTurnSettlementRequest {
        turn_id: TURN,
        input_digest: SOURCE_DIGEST,
        expected_revision,
        result_reference: reference,
    }
}

fn running_turn() -> Value {
    json!({
        "id": TURN,
        "request_id": REQUEST_ID,
        "profile_id": PROFILE,
        "conversation_id": CONVERSATION,
        "conversation_revision": 4,
        "status": "running",
        "revision": 2,
        "created_at": 50,
        "updated_at": 51,
        "input_digest": SOURCE_DIGEST,
        "guidance": [],
        "handoff": null,
        "result_reference": null,
        "error": null,
        "events": [
            {"sequence": 0, "name": "turn.queued", "at": 50, "details": {}},
            {"sequence": 1, "name": "turn.running", "at": 51, "details": {}}
        ],
    })
}

fn result_reference() -> Value {
    let message_id = |role| {
        let identity = serde_json::to_vec(&[CONVERSATION, TURN, role]).unwrap();
        let digest = Digest::hash(&identity);
        format!("message:{}", &digest.as_str()["sha256:".len()..])
    };
    json!({
        "conversation_id": CONVERSATION,
        "conversation_revision": 6,
        "user_message_id": message_id("user"),
        "assistant_message_id": message_id("assistant"),
        "outcome_digest": Digest::hash(b"original outcome").as_str(),
    })
}

#[test]
fn settles_the_original_file_and_exact_stale_replay_preserves_its_bytes() {
    let store = OriginalStore::new();
    let mut original = running_turn();
    original["status"] = json!("waiting");
    original["events"].as_array_mut().unwrap().push(json!({
        "sequence": 2, "name": "turn.waiting", "at": 52, "details": {"phase": "reconciliation_required"}
    }));
    original["revision"] = json!(3);
    original["updated_at"] = json!(52);
    store.initialize(&original);
    let identity = store.identity();
    let budget: Arc<dyn AllocationBudget> = Arc::new(BoundedBudget::default());
    let mut ticks = [100, 101].into_iter();
    let output = store
        .settle(Some(3), &budget, || Ok(()), || Ok(ticks.next().unwrap()))
        .unwrap();
    assert_eq!(store.identity(), identity);
    let (request_id, updated_at, body) = store.row();
    assert_eq!(request_id, REQUEST_ID);
    assert_eq!(updated_at, 100);
    assert_eq!(body.as_bytes(), output.as_bytes());
    let settled: Value = serde_json::from_str(&body).unwrap();
    let mut expected = original;
    expected["status"] = json!("completed");
    expected["revision"] = json!(4);
    expected["updated_at"] = json!(100);
    expected["result_reference"] = result_reference();
    expected["events"].as_array_mut().unwrap().push(json!({
        "sequence": 3, "name": "turn.completed", "at": 101,
        "details": {"result_reference": result_reference()},
    }));
    assert_eq!(settled, expected);

    let connection = Connection::open(&store.database).unwrap();
    let schema: Vec<String> = connection
        .prepare("SELECT name FROM sqlite_schema WHERE type = 'table' ORDER BY name")
        .unwrap()
        .query_map([], |row| row.get(0))
        .unwrap()
        .collect::<rusqlite::Result<_>>()
        .unwrap();
    assert_eq!(schema, ["turns"]);
    let unrelated: String = connection
        .query_row("SELECT body FROM turns WHERE id = 'unrelated'", [], |row| {
            row.get(0)
        })
        .unwrap();
    assert_eq!(unrelated, "{}");
    drop(connection);

    let committed = fs::read(&store.database).unwrap();
    let replay = store
        .settle(
            Some(1),
            &budget,
            || Ok(()),
            || panic!("replay must not observe the clock"),
        )
        .unwrap();
    assert_eq!(replay.as_bytes(), output.as_bytes());
    assert_eq!(fs::read(&store.database).unwrap(), committed);
    assert_eq!(store.identity(), identity);
    assert!(!store.journal().exists());
}

#[test]
fn revocation_after_the_real_update_rolls_back() {
    let store = OriginalStore::new();
    let original = running_turn();
    store.initialize(&original);
    let before = fs::read(&store.database).unwrap();
    let budget: Arc<dyn AllocationBudget> = Arc::new(BoundedBudget::default());
    let observed_update = Arc::new(AtomicBool::new(false));
    let hook_observed_update = Arc::clone(&observed_update);
    let journal = store.journal();
    let error = store
        .settle(
            Some(2),
            &budget,
            move || {
                if journal.exists() {
                    hook_observed_update.store(true, Ordering::Release);
                    return Err(Error::Revoked);
                }
                Ok(())
            },
            || Ok(150),
        )
        .unwrap_err();
    assert!(observed_update.load(Ordering::Acquire));
    assert_eq!(error, Error::Revoked);
    assert!(!store.journal().exists());
    assert_eq!(fs::read(&store.database).unwrap(), before);
    assert_eq!(store.row().2, original.to_string());
}

#[test]
fn missing_database_and_symlink_targets_are_never_created_or_changed() {
    let store = OriginalStore::new();
    let budget: Arc<dyn AllocationBudget> = Arc::new(BoundedBudget::default());
    let settle = || store.settle(None, &budget, || Ok(()), || Ok(150));
    assert_eq!(settle().unwrap_err(), Error::NotFound);
    assert!(!store.database.parent().unwrap().exists());

    let external = OriginalStore::new();
    external.initialize(&running_turn());
    let before = fs::read(&external.database).unwrap();
    fs::create_dir_all(store.database.parent().unwrap()).unwrap();
    std::os::unix::fs::symlink(&external.database, &store.database).unwrap();
    assert_eq!(settle().unwrap_err(), Error::Storage);
    assert_eq!(fs::read(&external.database).unwrap(), before);
    fs::remove_file(&store.database).unwrap();

    store.initialize(&running_turn());
    let before_local = fs::read(&store.database).unwrap();
    std::os::unix::fs::symlink(&external.database, store.journal()).unwrap();
    assert_eq!(settle().unwrap_err(), Error::Storage);
    assert_eq!(fs::read(&external.database).unwrap(), before);
    assert_eq!(fs::read(&store.database).unwrap(), before_local);
}

#[test]
fn unavailable_output_capacity_is_detected_before_writing() {
    let store = OriginalStore::new();
    let original = running_turn();
    store.initialize(&original);
    let before = fs::read(&store.database).unwrap();
    let other_work = RUNTIME_BYTES - SQLITE_WORK_BYTES - TRANSITION_WORK_BYTES;
    let bounded = Arc::new(BoundedBudget(AtomicU64::new(other_work)));
    let budget: Arc<dyn AllocationBudget> = bounded.clone();
    let mut observed_clock = false;
    let error = store
        .settle(
            Some(2),
            &budget,
            || Ok(()),
            || {
                observed_clock = true;
                Ok(150)
            },
        )
        .unwrap_err();
    assert!(
        observed_clock,
        "temporary transition was funded before output failed"
    );
    assert_eq!(error, Error::Capacity);
    assert_eq!(bounded.0.load(Ordering::Acquire), other_work);
    assert_eq!(fs::read(&store.database).unwrap(), before);
    assert!(!store.journal().exists());
}

#[test]
fn settlement_reads_stop_after_the_independent_sqlite_writer_releases_its_lock() {
    let store = OriginalStore::new();
    let mut stopped = running_turn();
    store.initialize(&stopped);
    stopped["revision"] = json!(3);
    stopped["updated_at"] = json!(52);
    stopped["events"].as_array_mut().unwrap().push(json!({
        "sequence": 2, "name": "turn.cancellation_requested", "at": 52,
        "details": {"phase": "nested_cancellation_requested"},
    }));

    let mut stop = Connection::open(&store.database).unwrap();
    let stop_transaction = stop
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .unwrap();
    stop_transaction
        .execute(
            "UPDATE turns SET updated_at = 52, body = ?1 WHERE id = ?2",
            params![stopped.to_string(), TURN],
        )
        .unwrap();
    let (signal, contention) = mpsc::channel();
    thread::scope(|scope| {
        let settlement = scope.spawn(|| {
            let budget: Arc<dyn AllocationBudget> = Arc::new(BoundedBudget::default());
            let mut checks = 0;
            store.settle(
                Some(2),
                &budget,
                move || {
                    checks += 1;
                    if checks == 3 {
                        signal.send(()).unwrap();
                    }
                    Ok(())
                },
                || panic!("a stale capture must never construct an update"),
            )
        });
        contention.recv_timeout(WAIT).unwrap();
        stop_transaction.commit().unwrap();
        assert_eq!(settlement.join().unwrap().unwrap_err(), Error::Conflict);
    });
    drop(stop);
    assert_eq!(store.row().2, stopped.to_string());

    let budget: Arc<dyn AllocationBudget> = Arc::new(BoundedBudget::default());
    let settled = store.settle(None, &budget, || Ok(()), || Ok(150)).unwrap();
    let body: Value = serde_json::from_slice(settled.as_bytes()).unwrap();
    assert_eq!(body["status"], "completed");
    assert_eq!(body["revision"], 4);
    assert_eq!(body["events"][2], stopped["events"][2]);
    assert_eq!(body["events"][3]["name"], "turn.completed");
}

#[test]
fn an_independent_reader_blocks_the_exclusive_commit_without_changing_the_row() {
    let store = OriginalStore::new();
    let original = running_turn();
    store.initialize(&original);
    let before = fs::read(&store.database).unwrap();
    let mut reader = Connection::open(&store.database).unwrap();
    let read_transaction = reader.transaction().unwrap();
    let snapshot: String = read_transaction
        .query_row("SELECT body FROM turns WHERE id = ?1", [TURN], |row| {
            row.get(0)
        })
        .unwrap();
    assert_eq!(snapshot, original.to_string());

    let commit_guard_reached = Arc::new(AtomicBool::new(false));
    let observed_commit = Arc::clone(&commit_guard_reached);
    let journal = store.journal();
    let budget: Arc<dyn AllocationBudget> = Arc::new(BoundedBudget::default());
    let error = store
        .settle(
            Some(2),
            &budget,
            move || {
                if journal.exists() {
                    observed_commit.store(true, Ordering::Release);
                }
                Ok(())
            },
            || Ok(150),
        )
        .unwrap_err();
    assert_eq!(error, Error::CommitUnconfirmed);
    assert!(!commit_guard_reached.load(Ordering::Acquire));
    read_transaction.rollback().unwrap();
    drop(reader);
    assert_eq!(store.row().2, snapshot);
    assert_eq!(fs::read(&store.database).unwrap(), before);
    assert!(!store.journal().exists());
}

#[test]
fn a_reader_delays_the_final_authority_check_until_exclusive_commit() {
    let store = OriginalStore::new();
    store.initialize(&running_turn());
    let before = fs::read(&store.database).unwrap();
    let mut reader = Connection::open(&store.database).unwrap();
    let read_transaction = reader.transaction().unwrap();
    read_transaction
        .query_row("SELECT body FROM turns WHERE id = ?1", [TURN], |row| {
            row.get::<_, String>(0)
        })
        .unwrap();

    let revoked = Arc::new(AtomicBool::new(false));
    let (published, observed) = mpsc::channel();
    thread::scope(|scope| {
        let settlement = scope.spawn(|| {
            let revoked = Arc::clone(&revoked);
            let budget: Arc<dyn AllocationBudget> = Arc::new(BoundedBudget::default());
            store.settle(
                Some(2),
                &budget,
                move || {
                    if revoked.load(Ordering::Acquire) {
                        published.send(()).unwrap();
                        return Err(Error::Revoked);
                    }
                    Ok(())
                },
                || Ok(150),
            )
        });
        let deadline = Instant::now() + WAIT;
        while !store.journal().exists() {
            assert!(Instant::now() < deadline, "writer did not reach commit");
            thread::sleep(Duration::from_millis(5));
        }
        revoked.store(true, Ordering::Release);
        assert!(observed.try_recv().is_err());
        read_transaction.rollback().unwrap();
        observed.recv_timeout(WAIT).unwrap();
        assert_eq!(settlement.join().unwrap().unwrap_err(), Error::Revoked);
    });
    drop(reader);
    assert_eq!(fs::read(&store.database).unwrap(), before);
    assert!(!store.journal().exists());
}

#[test]
#[ignore = "uses the original Python owner as a differential oracle"]
fn settlement_matches_the_original_python_owner_byte_for_byte() {
    const ORACLE: &str = r#"
import json
import sqlite3
import sys
from pathlib import Path
from ecosystem.rumi_turn_runtime_pack.runtime import durable, turns

root, profile, turn, source_digest, reference = sys.argv[1:]
times = iter((100, 101, 102))
durable._now_ms = turns._now_ms = lambda: next(times)
owner = durable.DurableTurnRuntime(profile, user_data_root=Path(root))
owner.settle_saved_from_receipt(
    turn, input_digest=source_digest, result_reference=json.loads(reference),
)
with sqlite3.connect(owner.path) as connection:
    body = connection.execute("SELECT body FROM turns WHERE id = ?", (turn,)).fetchone()[0]
sys.stdout.buffer.write(body.encode("utf-8"))
"#;
    let runtime = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tobkiri_runtime");
    let python = std::env::var_os("TOBKIRI_TURN_ORACLE_PYTHON").unwrap_or_else(|| "python3".into());

    for state in ["running", "waiting", "stop-requested"] {
        let mut initial = running_turn();
        initial["opaque"] = serde_json::from_str(
            r#"{"large":123456789123456789123456789,"negative_zero":-0.0,"name":"会話"}"#,
        )
        .unwrap();
        if state == "waiting" {
            initial["status"] = json!("waiting");
            initial["revision"] = json!(3);
            initial["events"].as_array_mut().unwrap().push(json!({
                "sequence": 2, "name": "turn.waiting", "at": 52, "details": {}
            }));
        } else if state == "stop-requested" {
            initial["revision"] = json!(3);
            initial["events"].as_array_mut().unwrap().push(json!({
                "sequence": 2, "name": "turn.cancellation_requested", "at": 52,
                "details": {"phase": "nested_cancellation_requested"}
            }));
            initial["guidance"] = json!([
                {"id": "auto", "status": "queued", "value": {"auto_send": true}},
                {"id": "manual", "status": "queued", "value": {"auto_send": false}}
            ]);
        }

        let original = OriginalStore::new();
        let native = OriginalStore::new();
        original.initialize(&initial);
        native.initialize(&initial);
        let oracle = Command::new(&python)
            .args(["-B", "-c", ORACLE])
            .arg(original.root.path())
            .args([
                PROFILE,
                TURN,
                SOURCE_DIGEST,
                &result_reference().to_string(),
            ])
            .env("PYTHONPATH", &runtime)
            .current_dir(&runtime)
            .output()
            .unwrap();
        assert!(
            oracle.status.success(),
            "{state}: {}",
            String::from_utf8_lossy(&oracle.stderr)
        );
        let budget: Arc<dyn AllocationBudget> = Arc::new(BoundedBudget::default());
        let mut ticks = [100, 101, 102].into_iter();
        let output = native
            .settle(None, &budget, || Ok(()), || Ok(ticks.next().unwrap()))
            .unwrap();
        assert_eq!(output.as_bytes(), oracle.stdout, "{state}");
        assert_eq!(native.row(), original.row(), "{state}");
    }
}

struct KillChild(Child);

impl Drop for KillChild {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

#[test]
fn real_process_death_before_and_after_commit_recovers_in_the_original_file() {
    for stage in ["before", "after"] {
        let store = OriginalStore::new();
        let mut original = running_turn();
        original["legacy_metadata"] = json!("x".repeat(800 * 1024));
        store.initialize(&original);
        let identity = store.identity();
        let ready = store.root.path().join("settlement-ready");
        let mut child = KillChild(
            Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "original_saved_turn_settlement::tests::settlement_crash_child",
                ])
                .env("TOBKIRI_TEST_TURN_CRASH_ROOT", store.root.path())
                .env("TOBKIRI_TEST_TURN_CRASH_STAGE", stage)
                .stdout(Stdio::null())
                .stderr(Stdio::inherit())
                .spawn()
                .unwrap(),
        );
        let deadline = Instant::now() + WAIT;
        while !ready.exists() {
            assert!(
                child.0.try_wait().unwrap().is_none(),
                "child exited before {stage} commit"
            );
            assert!(
                Instant::now() < deadline,
                "child failed to reach {stage} commit"
            );
            thread::sleep(Duration::from_millis(5));
        }
        if stage == "before" {
            assert!(fs::metadata(store.journal()).unwrap().len() > 512);
        }
        child.0.kill().unwrap();
        assert_eq!(child.0.wait().unwrap().signal(), Some(libc::SIGKILL));

        let row = store.row();
        let body: Value = serde_json::from_str(&row.2).unwrap();
        assert_eq!(store.identity(), identity);
        let budget: Arc<dyn AllocationBudget> = Arc::new(BoundedBudget::default());
        if stage == "before" {
            assert_eq!(body, original);
            assert_eq!(row.1, 51);
            store
                .settle(Some(2), &budget, || Ok(()), || Ok(150))
                .unwrap();
        } else {
            assert_eq!(body["status"], "completed");
            assert_eq!(body["revision"], 3);
            assert_eq!(body["result_reference"], result_reference());
            assert_eq!(body["legacy_metadata"], original["legacy_metadata"]);
            let committed = fs::read(&store.database).unwrap();
            let replay = store
                .settle(
                    Some(2),
                    &budget,
                    || Ok(()),
                    || panic!("committed row must replay"),
                )
                .unwrap();
            assert_eq!(replay.as_bytes(), row.2.as_bytes());
            assert_eq!(fs::read(&store.database).unwrap(), committed);
        }
        assert!(!store.journal().exists());
    }
}

#[test]
fn settlement_crash_child() {
    let Some(root) = std::env::var_os("TOBKIRI_TEST_TURN_CRASH_ROOT") else {
        return;
    };
    let root = DataRoot::open_existing(PathBuf::from(root)).unwrap();
    let profile = ProfileId::parse(PROFILE).unwrap();
    let stage = std::env::var("TOBKIRI_TEST_TURN_CRASH_STAGE").unwrap();
    let ready = root.path().join("settlement-ready");
    let journal = root
        .path()
        .join(database_path(&profile))
        .with_file_name("turns.sqlite3-journal");
    let checkpoint = move |ready: &Path| {
        fs::write(ready, b"ready").unwrap();
        thread::sleep(WAIT * 2);
        panic!("parent did not terminate settlement crash fixture")
    };
    let budget: Arc<dyn AllocationBudget> = Arc::new(BoundedBudget::default());
    let reference = result_reference().to_string();
    let precommit = stage == "before";
    let hook_ready = ready.clone();
    settle_legacy_saved_turn(
        &root,
        &profile,
        &request(Some(2), reference.as_bytes()),
        &budget,
        Instant::now() + WAIT,
        move || {
            if precommit && journal.exists() {
                checkpoint(&hook_ready);
            }
            Ok(())
        },
    )
    .unwrap();
    assert_eq!(stage, "after");
    checkpoint(&ready);
}
