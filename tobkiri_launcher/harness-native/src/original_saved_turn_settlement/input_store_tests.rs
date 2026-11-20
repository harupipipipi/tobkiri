use super::super::start_tests::{Budget, LEGACY_SCHEMA};
use super::*;
use serde_json::{Value, json};
use std::{
    fs,
    os::unix::fs::{PermissionsExt, symlink},
    path::PathBuf,
    sync::{
        Barrier,
        atomic::{AtomicUsize, Ordering},
    },
    thread,
    time::Duration,
};
use tobkiri_pack_saved_turn_runtime_rust::{
    prepare_saved_input, replay_saved_input, saved_input_source,
};

const PROFILE: &str = "prof_original_saved_input";
const SOURCE: &[u8] = br#"{"request":{"turn_id":"turn-1","conversation_id":"conv-1","conversation_revision":1,"content":"hello"}}"#;

struct Store {
    _temp: tempfile::TempDir,
    root: DataRoot,
    profile: ProfileId,
    path: PathBuf,
    budget: Arc<dyn AllocationBudget>,
}

impl Store {
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
            budget: Arc::new(Budget::default()),
        }
    }

    fn sqlite(&self) -> Connection {
        Connection::open(&self.path).unwrap()
    }

    fn identity(&self, source: &[u8]) -> OriginalSavedInputIdentity {
        let identity = saved_input_source(source).unwrap();
        OriginalSavedInputIdentity {
            turn_id: self.copy(identity.turn_id.as_bytes()),
            source_digest: Digest::parse(&identity.source_digest).unwrap(),
        }
    }

    fn copy(&self, bytes: &[u8]) -> CoreBuffer {
        CoreBuffer::copy_from(bytes, &self.budget).unwrap()
    }

    fn read(&self, source: &[u8]) -> Result<OriginalSavedInputOutcome> {
        read_legacy_saved_input_from_admitted_port(
            &self.root,
            &self.profile,
            &self.identity(source),
            &self.budget,
            Instant::now() + Duration::from_secs(5),
            || Ok(()),
            |row| self.recover(source, row),
        )
    }

    fn capture(
        &self,
        candidate: &[u8],
        guard: impl FnMut(OriginalSavedTurnCheck) -> Result<()> + Send + 'static,
    ) -> Result<OriginalSavedInputOutcome> {
        capture_legacy_saved_input_from_admitted_port(
            &self.root,
            &self.profile,
            &self.identity(SOURCE),
            &self.budget,
            Instant::now() + Duration::from_secs(5),
            guard,
            || {
                let projection = prepare_saved_input(SOURCE, candidate)?;
                Ok(OriginalSavedInputProjection {
                    identity: OriginalSavedInputIdentity {
                        turn_id: self.copy(projection.turn_id.as_bytes()),
                        source_digest: Digest::parse(&projection.source_digest).unwrap(),
                    },
                    accepted_digest: Digest::parse(&projection.accepted_digest).unwrap(),
                    context: self.copy(&projection.context),
                })
            },
            |row| self.recover(SOURCE, row),
        )
    }

    fn recover(&self, source: &[u8], row: OriginalSavedInputRow<'_>) -> Result<CoreBuffer> {
        replay_saved_input(
            source,
            row.source_digest,
            row.accepted_digest,
            row.context.as_bytes(),
        )
        .map(|accepted| self.copy(&accepted))
    }
}

fn projected_candidate() -> Vec<u8> {
    let mut candidate: Value = serde_json::from_slice(SOURCE).unwrap();
    candidate["request"]["task_context"] = json!({
        "version": "tobkiri.saved-task-context.v1",
        "source_id": "source-1",
        "conversation_id": "conv-1",
        "recipient_id": "conversation:conv-1",
        "input_id": "turn-1",
        "source_revision": 3,
        "generation": 8,
        "items": [{"id": "item-1", "kind": "todo", "body": "Reflect the retained context"}]
    });
    serde_json::to_vec(&candidate).unwrap()
}

fn accepted(result: Result<OriginalSavedInputOutcome>) -> CoreBuffer {
    let OriginalSavedInputOutcome::Accepted(body) = result.unwrap() else {
        panic!("the original database did not supply an accepted input");
    };
    body
}

fn deny_publication(phase: OriginalSavedTurnCheck) -> Result<()> {
    match phase {
        OriginalSavedTurnCheck::Live => Ok(()),
        OriginalSavedTurnCheck::Publish => Err(Error::Revoked),
    }
}

#[test]
fn initialized_owner_captures_and_restores_exact_first_projection() {
    let store = Store::new();
    fs::create_dir_all(store.path.parent().unwrap()).unwrap();
    let sqlite = store.sqlite();
    sqlite.execute_batch(LEGACY_SCHEMA).unwrap();
    assert!(matches!(
        store.read(SOURCE),
        Ok(OriginalSavedInputOutcome::Missing)
    ));

    let candidate = projected_candidate();
    let prepared = prepare_saved_input(SOURCE, &candidate).unwrap();
    let published = Arc::new(AtomicUsize::new(0));
    let publication_count = published.clone();
    let first = accepted(store.capture(&candidate, move |phase| {
        if phase == OriginalSavedTurnCheck::Publish {
            publication_count.fetch_add(1, Ordering::SeqCst);
        }
        Ok(())
    }));
    assert_eq!(first.as_bytes(), prepared.accepted);
    assert_eq!(published.load(Ordering::SeqCst), 1);

    let stored: (String, String, String, String) = sqlite
        .query_row(
            "SELECT id, source_digest, accepted_digest, context FROM saved_inputs",
            [],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        )
        .unwrap();
    assert_eq!(
        stored,
        (
            prepared.turn_id,
            prepared.source_digest,
            prepared.accepted_digest,
            String::from_utf8(prepared.context).unwrap(),
        )
    );
    assert_eq!(accepted(store.read(SOURCE)).as_bytes(), first.as_bytes());
    assert_eq!(
        accepted(store.capture(SOURCE, deny_publication)).as_bytes(),
        first.as_bytes()
    );

    let mut different: Value = serde_json::from_slice(SOURCE).unwrap();
    different["request"]["content"] = json!("different source, same Turn");
    assert!(matches!(
        store.read(&serde_json::to_vec(&different).unwrap()),
        Err(Error::Conflict)
    ));

    sqlite
        .execute("UPDATE saved_inputs SET context = '{'", [])
        .unwrap();
    assert!(matches!(store.read(SOURCE), Err(Error::InvalidRecord)));
    assert!(matches!(
        store.capture(SOURCE, deny_publication),
        Err(Error::InvalidRecord)
    ));
}

#[test]
fn denied_publication_leaves_no_accepted_input() {
    let store = Store::new();
    fs::create_dir_all(store.path.parent().unwrap()).unwrap();
    let sqlite = store.sqlite();
    sqlite.execute_batch(LEGACY_SCHEMA).unwrap();

    assert!(matches!(
        store.capture(SOURCE, deny_publication),
        Err(Error::Revoked)
    ));
    assert!(matches!(
        store.read(SOURCE),
        Ok(OriginalSavedInputOutcome::Missing)
    ));
    let count: i64 = sqlite
        .query_row("SELECT count(*) FROM saved_inputs", [], |row| row.get(0))
        .unwrap();
    assert_eq!(count, 0);
}

#[test]
fn missing_database_read_creates_nothing_and_first_capture_initializes_owner_schema() {
    let store = Store::new();
    assert!(matches!(
        store.read(SOURCE),
        Ok(OriginalSavedInputOutcome::Missing)
    ));
    assert!(!store.path.parent().unwrap().exists());

    let candidate = projected_candidate();
    let first = accepted(store.capture(&candidate, |_| Ok(())));
    assert_eq!(
        first.as_bytes(),
        prepare_saved_input(SOURCE, &candidate).unwrap().accepted
    );
    assert_eq!(accepted(store.read(SOURCE)).as_bytes(), first.as_bytes());
    assert_eq!(
        fs::metadata(&store.path).unwrap().permissions().mode() & 0o777,
        0o600
    );

    let schema = |connection: &Connection| {
        let mut statement = connection
            .prepare("SELECT type, name, tbl_name, sql FROM main.sqlite_schema ORDER BY name")
            .unwrap();
        statement
            .query_map([], |row| {
                let sql: Option<String> = row.get(3)?;
                let normalized =
                    sql.map(|sql| sql.split_whitespace().collect::<Vec<_>>().join(" "));
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    normalized,
                ))
            })
            .unwrap()
            .collect::<rusqlite::Result<Vec<_>>>()
            .unwrap()
    };
    let original = Connection::open_in_memory().unwrap();
    original.execute_batch(LEGACY_SCHEMA).unwrap();
    assert_eq!(schema(&store.sqlite()), schema(&original));
}

#[test]
fn refused_first_publication_rolls_back_schema_and_allows_a_fresh_retry() {
    let store = Store::new();
    let publish = Arc::new(AtomicUsize::new(0));
    let observed = Arc::clone(&publish);
    assert!(matches!(
        store.capture(SOURCE, move |phase| {
            if phase == OriginalSavedTurnCheck::Publish {
                observed.fetch_add(1, Ordering::SeqCst);
            }
            deny_publication(phase)
        }),
        Err(Error::Revoked)
    ));
    assert_eq!(publish.load(Ordering::SeqCst), 1);
    assert!(matches!(
        store.read(SOURCE),
        Ok(OriginalSavedInputOutcome::Missing)
    ));
    let count: i64 = store
        .sqlite()
        .query_row("SELECT count(*) FROM main.sqlite_schema", [], |row| {
            row.get(0)
        })
        .unwrap();
    assert_eq!(count, 0);
    assert!(!store.path.with_extension("sqlite3-journal").exists());
    assert_eq!(
        accepted(store.capture(SOURCE, |_| Ok(()))).as_bytes(),
        accepted(store.read(SOURCE)).as_bytes()
    );
}

#[test]
fn concurrent_first_captures_publish_one_projection() {
    let store = Store::new();
    let start = Barrier::new(2);
    let publish = Arc::new(AtomicUsize::new(0));
    let candidate = projected_candidate();
    let capture = |candidate: &[u8]| {
        let publish = Arc::clone(&publish);
        start.wait();
        accepted(store.capture(candidate, move |phase| {
            if phase == OriginalSavedTurnCheck::Publish {
                publish.fetch_add(1, Ordering::SeqCst);
            }
            Ok(())
        }))
    };
    thread::scope(|scope| {
        let first = scope.spawn(|| capture(SOURCE));
        let second = scope.spawn(|| capture(&candidate));
        assert_eq!(
            first.join().unwrap().as_bytes(),
            second.join().unwrap().as_bytes()
        );
    });
    assert_eq!(publish.load(Ordering::SeqCst), 1);
}

#[test]
fn populated_unsupported_and_initialized_empty_schemas_fall_back_before_evaluating() {
    let store = Store::new();
    fs::create_dir_all(store.path.parent().unwrap()).unwrap();
    let sqlite = store.sqlite();
    sqlite.execute_batch("PRAGMA user_version=0").unwrap();
    assert!(fs::metadata(&store.path).unwrap().len() > 0);
    for schema in ["", "CREATE TABLE unrelated (id TEXT PRIMARY KEY)"] {
        sqlite.execute_batch(schema).unwrap();
        assert!(matches!(
            store.read(SOURCE),
            Ok(OriginalSavedInputOutcome::LegacyFallback)
        ));
        assert!(matches!(
            store.capture(b"must not prepare", deny_publication),
            Ok(OriginalSavedInputOutcome::LegacyFallback)
        ));
    }
    assert!(
        !sqlite
            .prepare("SELECT 1 FROM main.sqlite_schema WHERE name = 'saved_inputs'")
            .unwrap()
            .exists([])
            .unwrap()
    );
}

#[test]
fn capture_preserves_the_original_wal_database_mode() {
    let store = Store::new();
    fs::create_dir_all(store.path.parent().unwrap()).unwrap();
    let sqlite = store.sqlite();
    sqlite.execute_batch("PRAGMA journal_mode=WAL").unwrap();
    sqlite.execute_batch(LEGACY_SCHEMA).unwrap();
    accepted(store.capture(SOURCE, |_| Ok(())));
    let mode: String = sqlite
        .query_row("PRAGMA journal_mode", [], |row| row.get(0))
        .unwrap();
    assert_eq!(mode, "wal");
    let count: i64 = sqlite
        .query_row("SELECT count(*) FROM saved_inputs", [], |row| row.get(0))
        .unwrap();
    assert_eq!(count, 1);
    let files = [
        store.path.clone(),
        store.path.with_extension("sqlite3-wal"),
        store.path.with_extension("sqlite3-shm"),
    ];
    let original = files.each_ref().map(|path| fs::read(path).unwrap());
    assert!(matches!(
        store.read(SOURCE),
        Ok(OriginalSavedInputOutcome::LegacyFallback)
    ));
    assert_eq!(
        files.each_ref().map(|path| fs::read(path).unwrap()),
        original
    );
}

#[test]
fn a_regular_sidecar_does_not_mask_a_corrupt_main_database() {
    let store = Store::new();
    fs::create_dir_all(store.path.parent().unwrap()).unwrap();
    fs::write(&store.path, b"invalid database header").unwrap();
    fs::write(store.path.with_extension("sqlite3-wal"), b"not a WAL").unwrap();
    assert!(matches!(
        store.read(SOURCE),
        Err(Error::InvalidRecord | Error::Storage)
    ));
}

#[test]
fn first_capture_rejects_redirected_or_orphaned_database_paths() {
    let store = Store::new();
    fs::create_dir_all(store.path.parent().unwrap()).unwrap();
    let outside = store.root.path().join("outside");
    fs::write(&outside, []).unwrap();
    symlink(&outside, &store.path).unwrap();
    assert!(matches!(
        store.capture(SOURCE, |_| Ok(())),
        Err(Error::Storage)
    ));
    fs::remove_file(&store.path).unwrap();

    fs::hard_link(&outside, &store.path).unwrap();
    assert!(matches!(
        store.capture(SOURCE, |_| Ok(())),
        Err(Error::Storage)
    ));
    fs::remove_file(&store.path).unwrap();

    let journal = store.path.with_extension("sqlite3-journal");
    fs::write(&journal, b"orphan").unwrap();
    assert!(matches!(
        store.capture(SOURCE, |_| Ok(())),
        Err(Error::Storage)
    ));
    assert!(!store.path.exists());
    assert_eq!(fs::read(&journal).unwrap(), b"orphan");
    assert_eq!(fs::metadata(&outside).unwrap().len(), 0);
}
