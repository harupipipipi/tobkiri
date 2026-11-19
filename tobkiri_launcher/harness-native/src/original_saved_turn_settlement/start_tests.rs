use super::*;
use std::{fs, sync::atomic::AtomicU64};
use tobkiri_canon::AllocationError;

const PROFILE: &str = "prof_original_start";
const TURN: &str = "turn-original-start";
const CONVERSATION: &str = "conversation-original-start";
const INPUT_DIGEST: &str =
    "sha256:1111111111111111111111111111111111111111111111111111111111111111";
const SOURCE_DIGEST: &str =
    "sha256:2222222222222222222222222222222222222222222222222222222222222222";
const LEGACY_SCHEMA: &str = "
    CREATE TABLE turns (
        id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
        updated_at INTEGER NOT NULL, body TEXT NOT NULL
    );
    CREATE TABLE saved_inputs (
        id TEXT PRIMARY KEY, source_digest TEXT NOT NULL,
        accepted_digest TEXT NOT NULL, context TEXT NOT NULL
    );
    CREATE TABLE message_deliveries (
        id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, body TEXT NOT NULL
    );
    CREATE TABLE message_interrupts (id TEXT PRIMARY KEY, body TEXT NOT NULL);
    CREATE TABLE calendar_preparations (id TEXT PRIMARY KEY, body TEXT NOT NULL);
";

#[derive(Default)]
struct Budget(AtomicU64);

impl AllocationBudget for Budget {
    fn reserve(&self, request: AllocationRequest) -> std::result::Result<(), AllocationError> {
        self.0
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |used| {
                used.checked_add(request.bytes)
                    .filter(|total| *total <= 64 * 1024 * 1024)
            })
            .map(drop)
            .map_err(|_| AllocationError::Limit)
    }

    fn release(&self, bytes: u64) {
        let used = self.0.fetch_sub(bytes, Ordering::AcqRel);
        assert!(used >= bytes);
    }
}

struct OriginalStartStore {
    _temp: tempfile::TempDir,
    root: DataRoot,
    profile: ProfileId,
    path: PathBuf,
    budget: Arc<dyn AllocationBudget>,
}

impl OriginalStartStore {
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

    fn initialize(&self) -> Connection {
        fs::create_dir_all(self.path.parent().unwrap()).unwrap();
        let connection = self.connection();
        connection.execute_batch(LEGACY_SCHEMA).unwrap();
        for turn_id in [TURN, "new-turn"] {
            bind_input(&connection, turn_id);
        }
        connection
    }

    fn connection(&self) -> Connection {
        Connection::open(&self.path).unwrap()
    }

    fn begin(
        &self,
        request: &LegacySavedTurnStartRequest<'_>,
        guard: impl FnMut(OriginalSavedTurnCheck) -> Result<()> + Send + 'static,
        evaluate: impl FnOnce(OriginalSavedTurnStartStep<'_>) -> Result<OriginalSavedTurnDecision>,
    ) -> Result<OriginalSavedTurnStartOutcome> {
        begin_legacy_saved_turn_from_admitted_port(
            &self.root,
            &self.profile,
            &input(request),
            &self.budget,
            Instant::now() + Duration::from_secs(15),
            guard,
            evaluate,
        )
    }

    fn claim(
        &self,
        request: &LegacySavedTurnStartRequest<'_>,
        guard: impl FnMut(OriginalSavedTurnCheck) -> Result<()> + Send + 'static,
        evaluate: impl FnOnce(OriginalSavedTurnStartStep<'_>) -> Result<OriginalSavedTurnDecision>,
    ) -> Result<OriginalSavedTurnStartOutcome> {
        claim_legacy_saved_turn_from_admitted_port(
            &self.root,
            &self.profile,
            &input(request),
            &self.budget,
            Instant::now() + Duration::from_secs(15),
            guard,
            evaluate,
        )
    }

    fn evaluate(
        &self,
        request: &LegacySavedTurnStartRequest<'_>,
        step: OriginalSavedTurnStartStep<'_>,
        time: i64,
    ) -> Result<OriginalSavedTurnDecision> {
        let mut clock = || Ok(time);
        let transition = match step {
            OriginalSavedTurnStartStep::Begin => request.begin(PROFILE, &mut clock)?,
            OriginalSavedTurnStartStep::Claim(row) => request.claim(
                row.profile_id,
                row.turn_id,
                row.request_id,
                row.body.as_bytes(),
                &mut clock,
            )?,
        };
        match transition {
            Transition::Replay => Ok(OriginalSavedTurnDecision::Replay),
            Transition::Commit { updated_at, body } => Ok(OriginalSavedTurnDecision::Replace {
                updated_at,
                body: CoreBuffer::copy_from(&body, &self.budget).map_err(|_| Error::Capacity)?,
            }),
        }
    }

    fn row(&self, turn_id: &str) -> (String, i64, String) {
        self.connection()
            .query_row(
                "SELECT request_id, updated_at, body FROM turns WHERE id = ?",
                [turn_id],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap()
    }

    fn seed(&self, request: &LegacySavedTurnStartRequest<'_>, time: i64) {
        let Transition::Commit { updated_at, body } =
            request.begin(PROFILE, &mut || Ok(time)).unwrap()
        else {
            panic!("seed did not create a queued record");
        };
        self.connection()
            .execute(
                "INSERT INTO turns VALUES (?1, ?2, ?3, ?4)",
                params![
                    request.turn_id,
                    request.request_id(PROFILE).unwrap(),
                    updated_at,
                    std::str::from_utf8(&body).unwrap(),
                ],
            )
            .unwrap();
    }
}

fn bind_input(connection: &Connection, turn_id: &str) {
    connection
        .execute(
            "INSERT INTO saved_inputs VALUES (?1, ?2, ?3, '{}')",
            params![turn_id, SOURCE_DIGEST, INPUT_DIGEST],
        )
        .unwrap();
}

fn input<'a>(request: &LegacySavedTurnStartRequest<'a>) -> OriginalSavedTurnStartInput<'a> {
    OriginalSavedTurnStartInput {
        request: *request,
        source_digest: SOURCE_DIGEST,
    }
}

fn request(turn_id: &str) -> LegacySavedTurnStartRequest<'_> {
    LegacySavedTurnStartRequest {
        turn_id,
        conversation_id: CONVERSATION,
        conversation_revision: 20,
        input_digest: INPUT_DIGEST,
    }
}

fn never_evaluate(_: OriginalSavedTurnStartStep<'_>) -> Result<OriginalSavedTurnDecision> {
    panic!("the original owner's checks must finish before evaluating the Pack");
}

fn no_publication(phase: OriginalSavedTurnCheck) -> Result<()> {
    assert_eq!(phase, OriginalSavedTurnCheck::Live);
    Ok(())
}

fn outcome(result: Result<OriginalSavedTurnStartOutcome>, expected_claimed: bool) -> CoreBuffer {
    let OriginalSavedTurnStartOutcome::Turn { body, claimed } = result.unwrap() else {
        panic!("the original store returned a fallback");
    };
    assert_eq!(claimed, expected_claimed);
    body
}

#[test]
fn original_start_inserts_claims_and_replays_exact_indexed_body() {
    let store = OriginalStartStore::new();
    drop(store.initialize());
    let request = request(TURN);
    let publications = Arc::new(AtomicU64::new(0));
    let authority = || {
        let publications = publications.clone();
        move |phase| {
            if phase == OriginalSavedTurnCheck::Publish {
                publications.fetch_add(1, Ordering::AcqRel);
            }
            Ok(())
        }
    };
    let queued = outcome(
        store.begin(&request, authority(), |step| {
            store.evaluate(&request, step, 50)
        }),
        false,
    );
    let (request_id, updated_at, body) = store.row(TURN);
    assert_eq!(request_id, request.request_id(PROFILE).unwrap());
    assert_eq!(updated_at, 50);
    assert_eq!(queued.as_bytes(), body.as_bytes());
    let bytes_before_replay = fs::read(&store.path).unwrap();
    let replay = outcome(store.begin(&request, no_publication, never_evaluate), false);
    assert_eq!(replay.as_bytes(), queued.as_bytes());
    assert_eq!(fs::read(&store.path).unwrap(), bytes_before_replay);

    let running = outcome(
        store.claim(&request, authority(), |step| {
            store.evaluate(&request, step, 60)
        }),
        true,
    );
    let (claimed_request_id, claimed_at, claimed_body) = store.row(TURN);
    assert_eq!(claimed_request_id, request_id);
    assert_eq!(claimed_at, 60);
    assert_eq!(running.as_bytes(), claimed_body.as_bytes());
    let record: serde_json::Value = serde_json::from_str(&claimed_body).unwrap();
    assert_eq!(record["revision"], 2);
    assert_eq!(record["status"], "running");
    assert_eq!(
        record["events"][1]["details"]["phase"],
        "saved_execution_claimed"
    );
    let bytes_before_retry = fs::read(&store.path).unwrap();
    let retry = outcome(
        store.claim(&request, no_publication, |step| {
            store.evaluate(&request, step, 70)
        }),
        false,
    );
    assert_eq!(retry.as_bytes(), running.as_bytes());
    assert_eq!(fs::read(&store.path).unwrap(), bytes_before_retry);
    assert_eq!(publications.load(Ordering::Acquire), 2);
}

#[test]
fn original_start_leaves_absent_schema_and_preliminary_row_for_legacy_owner() {
    let store = OriginalStartStore::new();
    let request = request(TURN);
    assert!(matches!(
        store.begin(&request, no_publication, never_evaluate),
        Ok(OriginalSavedTurnStartOutcome::LegacyFallback)
    ));
    assert!(!store.path.parent().unwrap().exists());
    let connection = store.initialize();
    connection.execute_batch("DROP TABLE saved_inputs").unwrap();
    drop(connection);
    let bytes = fs::read(&store.path).unwrap();
    assert!(matches!(
        store.begin(&request, no_publication, never_evaluate),
        Ok(OriginalSavedTurnStartOutcome::LegacyFallback)
    ));
    assert_eq!(fs::read(&store.path).unwrap(), bytes);
    store
        .connection()
        .execute_batch(
            "CREATE TABLE saved_inputs (id TEXT PRIMARY KEY, source_digest TEXT NOT NULL,
            accepted_digest TEXT NOT NULL, context TEXT NOT NULL)",
        )
        .unwrap();
    bind_input(&store.connection(), TURN);
    let bytes = fs::read(&store.path).unwrap();
    assert!(matches!(
        store.claim(&request, no_publication, never_evaluate),
        Ok(OriginalSavedTurnStartOutcome::LegacyFallback)
    ));
    assert_eq!(fs::read(&store.path).unwrap(), bytes);
}

#[test]
fn start_requires_the_exact_owners_source_and_accepted_input() {
    let store = OriginalStartStore::new();
    let connection = store.initialize();
    let requested = request(TURN);
    for column in ["source_digest", "accepted_digest"] {
        let value = Digest::hash(column.as_bytes()).to_string();
        connection
            .execute(
                &format!("UPDATE saved_inputs SET {column} = ?1 WHERE id = ?2"),
                params![value, TURN],
            )
            .unwrap();
        let original = fs::read(&store.path).unwrap();
        assert!(matches!(
            store.begin(&requested, no_publication, never_evaluate),
            Err(Error::Conflict),
        ));
        assert!(matches!(
            store.claim(&requested, no_publication, never_evaluate),
            Err(Error::Conflict),
        ));
        assert_eq!(fs::read(&store.path).unwrap(), original);
        connection
            .execute(
                "UPDATE saved_inputs SET source_digest = ?1, accepted_digest = ?2 WHERE id = ?3",
                params![SOURCE_DIGEST, INPUT_DIGEST, TURN],
            )
            .unwrap();
    }
}

#[test]
fn original_start_validates_rows_after_first_active_turn_before_rejecting() {
    let store = OriginalStartStore::new();
    let connection = store.initialize();
    store.seed(&request("active-turn"), 10);
    store.seed(&request(TURN), 15);
    connection
        .execute(
            "INSERT INTO turns VALUES (?1, ?2, ?3, ?4)",
            params!["corrupt-later", "request-other", 99, "{}"],
        )
        .unwrap();
    let bytes = fs::read(&store.path).unwrap();

    assert!(matches!(
        store.begin(&request("new-turn"), no_publication, never_evaluate),
        Err(Error::InvalidRecord)
    ));
    assert!(matches!(
        store.claim(&request(TURN), no_publication, never_evaluate),
        Err(Error::InvalidRecord)
    ));
    assert_eq!(fs::read(&store.path).unwrap(), bytes);

    connection
        .execute("DELETE FROM turns WHERE id = 'corrupt-later'", [])
        .unwrap();
    let bytes = fs::read(&store.path).unwrap();
    assert!(matches!(
        store.begin(&request("new-turn"), no_publication, never_evaluate),
        Err(Error::Conflict)
    ));
    assert!(matches!(
        store.claim(&request(TURN), no_publication, never_evaluate),
        Err(Error::Conflict)
    ));
    assert_eq!(fs::read(&store.path).unwrap(), bytes);
}

#[test]
fn original_start_rolls_back_denied_publication_and_never_claims_cancelled_input() {
    let store = OriginalStartStore::new();
    let connection = store.initialize();
    let request = request(TURN);
    let deny_publication = |phase| match phase {
        OriginalSavedTurnCheck::Live => Ok(()),
        OriginalSavedTurnCheck::Publish => Err(Error::Revoked),
    };
    let empty = fs::read(&store.path).unwrap();
    assert!(matches!(
        store.begin(&request, deny_publication, |step| store
            .evaluate(&request, step, 90)),
        Err(Error::Revoked)
    ));
    assert_eq!(fs::read(&store.path).unwrap(), empty);

    store.seed(&request, 30);
    let queued = fs::read(&store.path).unwrap();
    assert!(matches!(
        store.claim(&request, deny_publication, |step| store
            .evaluate(&request, step, 90)),
        Err(Error::Revoked)
    ));
    assert_eq!(fs::read(&store.path).unwrap(), queued);

    let (_, _, body) = store.row(TURN);
    let mut record: serde_json::Value = serde_json::from_str(&body).unwrap();
    record["events"]
        .as_array_mut()
        .unwrap()
        .push(serde_json::json!({
            "sequence": 1, "name": "turn.cancellation_requested", "at": 35, "details": {}
        }));
    record["revision"] = 2.into();
    record["updated_at"] = 35.into();
    connection
        .execute(
            "UPDATE turns SET body = ?, updated_at = 35 WHERE id = ?",
            params![record.to_string(), TURN],
        )
        .unwrap();
    let bytes = fs::read(&store.path).unwrap();
    let replay = outcome(
        store.claim(&request, no_publication, |step| {
            store.evaluate(&request, step, 90)
        }),
        false,
    );
    assert_eq!(replay.as_bytes(), store.row(TURN).2.as_bytes());
    assert_eq!(fs::read(&store.path).unwrap(), bytes);
}

#[test]
fn original_start_rejects_a_foreign_or_mismatched_pack_replacement() {
    let store = OriginalStartStore::new();
    drop(store.initialize());
    let requested = request(TURN);
    let foreign = request("another-turn");
    let original = fs::read(&store.path).unwrap();
    assert!(matches!(
        store.begin(&requested, no_publication, |step| store
            .evaluate(&foreign, step, 50)),
        Err(Error::InvalidRecord)
    ));
    assert!(matches!(
        store.begin(&requested, no_publication, |step| {
            let OriginalSavedTurnDecision::Replace { body, .. } =
                store.evaluate(&requested, step, 50)?
            else {
                panic!("an original begin must produce a new record");
            };
            Ok(OriginalSavedTurnDecision::Replace {
                updated_at: 51,
                body,
            })
        }),
        Err(Error::InvalidRecord)
    ));
    assert_eq!(fs::read(&store.path).unwrap(), original);
}

#[test]
fn original_start_stops_new_inserts_at_existing_capacity() {
    let store = OriginalStartStore::new();
    let mut connection = store.initialize();
    let transaction = connection.transaction().unwrap();
    let mut insert = transaction
        .prepare("INSERT INTO turns VALUES (?1, ?2, ?3, ?4)")
        .unwrap();
    for index in 0..10_000 {
        let turn_id = format!("terminal-{index}");
        let request_id = format!("retained-{index}");
        let body = serde_json::json!({
            "id": turn_id, "request_id": request_id, "profile_id": PROFILE,
            "conversation_id": CONVERSATION, "conversation_revision": 1,
            "status": "cancelled", "revision": 2, "created_at": 10, "updated_at": 20,
            "guidance": [], "events": [],
        })
        .to_string();
        insert
            .execute(params![turn_id, request_id, 20, body])
            .unwrap();
    }
    drop(insert);
    transaction.commit().unwrap();
    let bytes = fs::read(&store.path).unwrap();
    assert!(matches!(
        store.begin(&request(TURN), no_publication, never_evaluate),
        Err(Error::Conflict)
    ));
    assert_eq!(fs::read(&store.path).unwrap(), bytes);
}
