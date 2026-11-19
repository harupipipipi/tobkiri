//! Ordinary saved Turn start in the original SQLite owner.

use super::{
    Connection, DataRoot, Error, Instant, LegacySavedTurnStartRequest, OriginalSavedTurnCheck,
    OriginalSavedTurnDecision, OriginalSavedTurnRow, PrepFlags, ProfileId, Result,
    SQLITE_BUSY_SLICE, SQLITE_LOCK_TIMEOUT, SQLITE_WORK_BYTES, StoreError, TRANSITION_WORK_BYTES,
    TemporaryCharge, Transaction, begin_immediate, cap_connection, commit_current, database_path,
    indexed_turn_row, read_sql, sql, storage,
};
use rusqlite::{
    Row,
    hooks::{AuthAction, AuthContext, Authorization},
    params,
};
use std::sync::Arc;
use tobkiri_canon::{AllocationBudget, CoreBuffer};
use tobkiri_pack_saved_turn_runtime_rust::active_turn_in_conversation;

const TURN_CAPACITY: i64 = 10_000;
const SELECT_REQUEST: &str = "SELECT id, request_id, body FROM turns WHERE request_id = ?1";
const SELECT_ID: &str = "SELECT 1 FROM turns WHERE id = ?1";
const INSERT_TURN: &str =
    "INSERT INTO turns (id, request_id, updated_at, body) VALUES (?1, ?2, ?3, ?4)";
const UPDATE_TURN: &str =
    "UPDATE turns SET updated_at = ?1, body = ?2 WHERE id = ?3 AND request_id = ?4";
const COUNT_TABLES: &str =
    "SELECT count(*) FROM main.sqlite_schema WHERE type = 'table' AND name IN
    ('turns', 'saved_inputs', 'message_deliveries', 'message_interrupts', 'calendar_preparations')";
const COUNT_TURNS: &str = "SELECT count(*) FROM turns";
// Sort only bounded index fields. Read each body by its exact id below so the
// SQLite sorter never retains the history's JSON. Noncanonical indexed types
// take the legacy route, which can preserve SQLite's dynamic sort ordering.
const SCAN_TURN_IDS: &str = "SELECT
    CASE WHEN typeof(id) = 'text' AND length(CAST(id AS BLOB)) <= 256 THEN id END AS scan_id,
    CASE WHEN typeof(updated_at) = 'integer' THEN updated_at END AS scan_updated_at
    FROM turns ORDER BY scan_updated_at, scan_id";

/// Physical result of ordinary saved Turn begin or claim.
pub enum OriginalSavedTurnStartOutcome {
    /// The exact original or newly published Turn JSON.
    Turn {
        /// Copy of the JSON funded by the same owner as the transaction.
        body: CoreBuffer,
        /// True only if this call published the queued-to-running claim.
        claimed: bool,
    },
    /// Continue with the original implementation before any Turn write.
    ///
    /// The original owner must initialize its schema or create the missing
    /// preliminary begin row, or handle an unsupported physical history.
    LegacyFallback,
}

/// Evaluation input from the original owner to the admitted Pack.
pub enum OriginalSavedTurnStartStep<'a> {
    /// Generate the first queued record.
    Begin,
    /// Evaluate the latest indexed row under the separate claim write lock.
    Claim(OriginalSavedTurnRow<'a>),
}

/// Begin one ordinary saved Turn in the original owner's initialized database.
///
/// The evaluator receives only the new-record path under the writer lock.
/// Indexed replay returns the exact retained bytes and leaves SQLite unchanged.
pub fn begin_legacy_saved_turn_from_admitted_port(
    root: &DataRoot,
    profile: &ProfileId,
    request: &LegacySavedTurnStartRequest<'_>,
    budget: &Arc<dyn AllocationBudget>,
    deadline: Instant,
    guard: impl FnMut(OriginalSavedTurnCheck) -> Result<()> + Send + 'static,
    evaluate: impl FnOnce(OriginalSavedTurnStartStep<'_>) -> Result<OriginalSavedTurnDecision>,
) -> Result<OriginalSavedTurnStartOutcome> {
    start_in_original_store(
        root, profile, request, budget, deadline, guard, evaluate, false,
    )
}

/// Claim a previously begun ordinary saved Turn under a second writer lock.
///
/// The first transaction replays the original request index. If it would have
/// to create a begin row, immediately return a legacy fallback. This keeps the
/// original implementation responsible for the two separate publications.
pub fn claim_legacy_saved_turn_from_admitted_port(
    root: &DataRoot,
    profile: &ProfileId,
    request: &LegacySavedTurnStartRequest<'_>,
    budget: &Arc<dyn AllocationBudget>,
    deadline: Instant,
    guard: impl FnMut(OriginalSavedTurnCheck) -> Result<()> + Send + 'static,
    evaluate: impl FnOnce(OriginalSavedTurnStartStep<'_>) -> Result<OriginalSavedTurnDecision>,
) -> Result<OriginalSavedTurnStartOutcome> {
    start_in_original_store(
        root, profile, request, budget, deadline, guard, evaluate, true,
    )
}

#[allow(clippy::too_many_arguments)]
fn start_in_original_store(
    root: &DataRoot,
    profile: &ProfileId,
    request: &LegacySavedTurnStartRequest<'_>,
    budget: &Arc<dyn AllocationBudget>,
    deadline: Instant,
    mut guard: impl FnMut(OriginalSavedTurnCheck) -> Result<()> + Send + 'static,
    evaluate: impl FnOnce(OriginalSavedTurnStartStep<'_>) -> Result<OriginalSavedTurnDecision>,
    claiming: bool,
) -> Result<OriginalSavedTurnStartOutcome> {
    let now = Instant::now();
    let mut check = move |phase| {
        if Instant::now() >= deadline {
            return Err(Error::Revoked);
        }
        guard(phase)?;
        if Instant::now() >= deadline {
            return Err(Error::Revoked);
        }
        Ok(())
    };
    check(OriginalSavedTurnCheck::Live)?;
    let _sqlite_funding = TemporaryCharge::new(budget, SQLITE_WORK_BYTES)?;
    let request_id = request.request_id(profile.as_str())?;
    let path = database_path(profile);
    match root.with_existing_database(&path, |connection| {
        let lock_deadline = (now + SQLITE_LOCK_TIMEOUT).min(deadline);
        let mut live = || check(OriginalSavedTurnCheck::Live);
        prepare_start_connection(connection, deadline, lock_deadline, &mut live)?;
        let transaction = begin_immediate(connection, lock_deadline, &mut live)?;
        live()?;
        if !schema_present(&transaction)? {
            live()?;
            transaction.rollback().map_err(sql)?;
            return Ok(OriginalSavedTurnStartOutcome::LegacyFallback);
        }
        let replay = replay_begin(
            &transaction,
            profile,
            request,
            &request_id,
            budget,
            &mut live,
            !claiming,
        )?;
        let (transaction, decision) = match replay {
            BeginReplay::Missing if claiming => {
                live()?;
                transaction.rollback().map_err(sql)?;
                return Ok(OriginalSavedTurnStartOutcome::LegacyFallback);
            }
            BeginReplay::Missing => {
                require_absent_turn(&transaction, request.turn_id)?;
                let count = require_idle(&transaction, profile, request, budget, &mut live)?;
                if count >= TURN_CAPACITY {
                    return Err(Error::Capacity);
                }
                live()?;
                let decision = evaluate(OriginalSavedTurnStartStep::Begin)?;
                (transaction, decision)
            }
            BeginReplay::Return(body) => {
                live()?;
                transaction.rollback().map_err(sql)?;
                return Ok(OriginalSavedTurnStartOutcome::Turn {
                    body,
                    claimed: false,
                });
            }
            BeginReplay::Validated => {
                transaction.rollback().map_err(sql)?;
                // ClaimSaved's begin and claim use separate transactions. Reload
                // the primary index after dropping the first writer lock so any
                // intervening guidance or cancellation is seen by the claim.
                let lock_deadline = (Instant::now() + SQLITE_LOCK_TIMEOUT).min(deadline);
                let claim_transaction = begin_immediate(connection, lock_deadline, &mut live)?;
                live()?;
                match evaluate_claim(
                    &claim_transaction,
                    profile,
                    request,
                    &request_id,
                    budget,
                    &mut live,
                    evaluate,
                )? {
                    ClaimDecision::Replay(body) => {
                        live()?;
                        claim_transaction.rollback().map_err(sql)?;
                        return Ok(OriginalSavedTurnStartOutcome::Turn {
                            body,
                            claimed: false,
                        });
                    }
                    ClaimDecision::Publish(decision) => (claim_transaction, decision),
                }
            }
        };
        live()?;
        let OriginalSavedTurnDecision::Replace { updated_at, body } = decision else {
            return Err(Error::Storage);
        };
        {
            let _funding = TemporaryCharge::new(budget, TRANSITION_WORK_BYTES)?;
            request.validate_publication(
                profile.as_str(),
                &request_id,
                body.as_bytes(),
                updated_at,
                claiming,
            )?;
        }
        let body_text = std::str::from_utf8(body.as_bytes()).map_err(|_| Error::InvalidRecord)?;
        live()?;
        let changed = if claiming {
            prepare(&transaction, UPDATE_TURN)?.execute(params![
                updated_at,
                body_text,
                request.turn_id,
                request_id,
            ])
        } else {
            prepare(&transaction, INSERT_TURN)?.execute(params![
                request.turn_id,
                request_id,
                updated_at,
                body_text,
            ])
        }
        .map_err(read_sql)?;
        if changed != 1 {
            return Err(Error::Storage);
        }
        commit_current(connection, transaction, deadline, move || {
            check(OriginalSavedTurnCheck::Live)?;
            check(OriginalSavedTurnCheck::Publish)
        })?;
        Ok(OriginalSavedTurnStartOutcome::Turn {
            body,
            claimed: claiming,
        })
    }) {
        Ok(result) => match result {
            // The codec is consulted before a write. A failed or ambiguous
            // commit uses its own error and never takes the legacy fallback.
            Err(Error::RequiresLegacyCodec) => Ok(OriginalSavedTurnStartOutcome::LegacyFallback),
            other => other,
        },
        Err(StoreError::NotFound) => Ok(OriginalSavedTurnStartOutcome::LegacyFallback),
        Err(error) => Err(storage(error)),
    }
}

enum BeginReplay {
    Missing,
    Validated,
    Return(CoreBuffer),
}

fn replay_begin(
    transaction: &Transaction<'_>,
    profile: &ProfileId,
    request: &LegacySavedTurnStartRequest<'_>,
    request_id: &str,
    budget: &Arc<dyn AllocationBudget>,
    live: &mut impl FnMut() -> Result<()>,
    return_body: bool,
) -> Result<BeginReplay> {
    let mut select = prepare(transaction, SELECT_REQUEST)?;
    let mut rows = select.query([request_id]).map_err(read_sql)?;
    let Some(row) = rows.next().map_err(read_sql)? else {
        return Ok(BeginReplay::Missing);
    };
    live()?;
    let row = indexed_turn_row(row, profile, indexed_id(row)?)?;
    if row.request_id != request_id {
        return Err(Error::InvalidRecord);
    }
    {
        let _funding = TemporaryCharge::new(budget, TRANSITION_WORK_BYTES)?;
        request.validate_replay(
            row.profile_id,
            row.turn_id,
            row.request_id,
            row.body.as_bytes(),
        )?;
    }
    if return_body {
        let body =
            CoreBuffer::copy_from(row.body.as_bytes(), budget).map_err(|_| Error::Capacity)?;
        Ok(BeginReplay::Return(body))
    } else {
        Ok(BeginReplay::Validated)
    }
}

enum ClaimDecision {
    Replay(CoreBuffer),
    Publish(OriginalSavedTurnDecision),
}

fn evaluate_claim(
    transaction: &Transaction<'_>,
    profile: &ProfileId,
    request: &LegacySavedTurnStartRequest<'_>,
    request_id: &str,
    budget: &Arc<dyn AllocationBudget>,
    live: &mut impl FnMut() -> Result<()>,
    evaluate: impl FnOnce(OriginalSavedTurnStartStep<'_>) -> Result<OriginalSavedTurnDecision>,
) -> Result<ClaimDecision> {
    let mut select = prepare(transaction, super::SELECT_TURN)?;
    let mut rows = select.query([request.turn_id]).map_err(read_sql)?;
    let row = rows.next().map_err(read_sql)?.ok_or(Error::Conflict)?;
    let row = indexed_turn_row(row, profile, request.turn_id)?;
    if row.request_id != request_id {
        return Err(Error::Conflict);
    }
    let claimable = {
        let _funding = TemporaryCharge::new(budget, TRANSITION_WORK_BYTES)?;
        request.claimable(
            row.profile_id,
            row.turn_id,
            row.request_id,
            row.body.as_bytes(),
        )?
    };
    if claimable {
        require_idle(transaction, profile, request, budget, live)?;
    }
    live()?;
    let body = row.body;
    match (claimable, evaluate(OriginalSavedTurnStartStep::Claim(row))?) {
        (false, OriginalSavedTurnDecision::Replay) => {
            let body =
                CoreBuffer::copy_from(body.as_bytes(), budget).map_err(|_| Error::Capacity)?;
            Ok(ClaimDecision::Replay(body))
        }
        (true, decision @ OriginalSavedTurnDecision::Replace { .. }) => {
            Ok(ClaimDecision::Publish(decision))
        }
        _ => Err(Error::Storage),
    }
}

fn require_idle(
    transaction: &Transaction<'_>,
    profile: &ProfileId,
    request: &LegacySavedTurnStartRequest<'_>,
    budget: &Arc<dyn AllocationBudget>,
    live: &mut impl FnMut() -> Result<()>,
) -> Result<i64> {
    let count: i64 = prepare(transaction, COUNT_TURNS)?
        .query_row([], |row| row.get(0))
        .map_err(read_sql)?;
    if count > TURN_CAPACITY {
        return Err(Error::RequiresLegacyCodec);
    }
    let _funding = TemporaryCharge::new(budget, TRANSITION_WORK_BYTES)?;
    let mut scan = prepare(transaction, SCAN_TURN_IDS)?;
    let mut ids = scan.query([]).map_err(read_sql)?;
    let mut select = prepare(transaction, super::SELECT_TURN)?;
    let mut active = false;
    while let Some(id) = ids.next().map_err(read_sql)? {
        live()?;
        let turn_id = indexed_id(id)?;
        if id.get::<_, Option<i64>>(1).map_err(read_sql)?.is_none() {
            return Err(Error::RequiresLegacyCodec);
        }
        let mut rows = select.query([turn_id]).map_err(read_sql)?;
        let row = rows.next().map_err(read_sql)?.ok_or(Error::Storage)?;
        let row = indexed_turn_row(row, profile, turn_id)?;
        // The original owner validates the entire ordered scan before deciding
        // whether the conversation is idle, even after another active row.
        active |= active_turn_in_conversation(
            row.profile_id,
            row.turn_id,
            row.request_id,
            row.body.as_bytes(),
            request.conversation_id,
            request.turn_id,
        )?;
    }
    if active {
        return Err(Error::Conflict);
    }
    Ok(count)
}

fn indexed_id<'row>(row: &'row Row<'_>) -> Result<&'row str> {
    row.get_ref(0)
        .map_err(read_sql)?
        .as_str()
        .map_err(|_| Error::InvalidRecord)
}

fn require_absent_turn(transaction: &Transaction<'_>, turn_id: &str) -> Result<()> {
    if prepare(transaction, SELECT_ID)?
        .exists([turn_id])
        .map_err(read_sql)?
    {
        return Err(Error::Conflict);
    }
    Ok(())
}

fn schema_present(transaction: &Transaction<'_>) -> Result<bool> {
    prepare(transaction, COUNT_TABLES)?
        .query_row([], |row| row.get::<_, i64>(0))
        .map(|tables| tables == 5)
        .map_err(read_sql)
}

fn prepare<'db>(connection: &'db Connection, statement: &str) -> Result<rusqlite::Statement<'db>> {
    connection
        .prepare_with_flags(statement, PrepFlags::SQLITE_PREPARE_NO_VTAB)
        .map_err(read_sql)
}

fn prepare_start_connection(
    connection: &Connection,
    deadline: Instant,
    lock_deadline: Instant,
    live: &mut impl FnMut() -> Result<()>,
) -> Result<()> {
    loop {
        live()?;
        connection
            .busy_timeout(
                lock_deadline
                    .saturating_duration_since(Instant::now())
                    .min(SQLITE_BUSY_SLICE),
            )
            .map_err(sql)?;
        match cap_connection(connection, deadline) {
            Ok(()) => break,
            Err(error)
                if error.sqlite_error_code() == Some(rusqlite::ErrorCode::DatabaseBusy)
                    && Instant::now() < lock_deadline => {}
            Err(error) => return Err(read_sql(error)),
        }
    }
    connection
        .authorizer(Some(|context: AuthContext<'_>| {
            if context.accessor.is_some() {
                return Authorization::Deny;
            }
            match (context.database_name, context.action) {
                (
                    _,
                    AuthAction::Select
                    | AuthAction::Transaction { .. }
                    | AuthAction::Function {
                        function_name: "count" | "length" | "typeof",
                    },
                )
                | (
                    None,
                    AuthAction::Read {
                        table_name: "turns",
                        column_name: "",
                    },
                )
                | (
                    Some("main"),
                    AuthAction::Read {
                        table_name: "turns",
                        column_name: "" | "id" | "request_id" | "updated_at" | "body",
                    }
                    | AuthAction::Read {
                        table_name: "sqlite_master" | "sqlite_schema",
                        column_name: "type" | "name",
                    }
                    | AuthAction::Insert {
                        table_name: "turns",
                    }
                    | AuthAction::Update {
                        table_name: "turns",
                        column_name: "updated_at" | "body",
                    },
                ) => Authorization::Allow,
                _ => Authorization::Deny,
            }
        }))
        .map_err(read_sql)
}
