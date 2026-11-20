//! Capture and restore accepted input in the original Saved Turn database.

use super::{
    Connection, DataRoot, Digest, Error, Instant, OriginalSavedTurnCheck, PrepFlags, ProfileId,
    RECORD_LIMIT, Result, SQLITE_BUSY_SLICE, SQLITE_LOCK_TIMEOUT, SQLITE_WORK_BYTES, StoreError,
    TRANSITION_WORK_BYTES, TemporaryCharge, begin_immediate, cap_connection, commit_current,
    database_path, indexed_turn_row, read_sql,
};
use rusqlite::{
    ErrorCode, Row, Transaction, TransactionBehavior,
    hooks::{AuthAction, AuthContext, Authorization},
    params,
};
use std::sync::Arc;
use tobkiri_canon::{AllocationBudget, CoreBuffer, MAX_BYTES};
use tobkiri_pack_saved_turn_runtime_rust::validate_saved_input_capture_turn;

#[cfg(all(test, unix))]
#[path = "input_store_tests.rs"]
mod tests;

const SAVED_INPUT_CAPACITY: i64 = 10_000;
const SELECT_SCHEMA: &str = "SELECT type FROM main.sqlite_schema WHERE name = 'saved_inputs'";
const SELECT_INPUT: &str = "SELECT id, source_digest, accepted_digest, context,
    EXISTS(SELECT 1 FROM main.saved_inputs WHERE id = ?1 LIMIT 1 OFFSET 1)
    FROM main.saved_inputs WHERE id = ?1 LIMIT 1";
const SELECT_TURN: &str = "SELECT id, request_id, body FROM main.turns WHERE id = ?1";
const COUNT_INPUTS: &str = "SELECT count(*) FROM main.saved_inputs";
const INSERT_INPUT: &str = "INSERT INTO main.saved_inputs (id, source_digest, accepted_digest, context) VALUES (?1, ?2, ?3, ?4)";

/// Identity of the authenticated source from the admitted Pack.
pub struct OriginalSavedInputIdentity {
    /// Exact Turn key in the original Profile database.
    pub turn_id: CoreBuffer,
    /// Canonical digest of the original saved input.
    pub source_digest: Digest,
}

/// The projection the admitted Pack derived from the proposed accepted input.
pub struct OriginalSavedInputProjection {
    /// Source identity from the same admitted invocation.
    pub identity: OriginalSavedInputIdentity,
    /// Canonical digest of the full accepted input.
    pub accepted_digest: Digest,
    /// Original owner projection JSON.
    pub context: CoreBuffer,
}

/// Input to the admitted Pack's recovery function.
pub struct OriginalSavedInputRow<'a> {
    /// Exact original source digest.
    pub source_digest: &'a str,
    /// Exact original accepted digest.
    pub accepted_digest: &'a str,
    /// Original owner projection JSON.
    pub context: &'a str,
}

/// Physical result of restoring or capturing accepted input.
pub enum OriginalSavedInputOutcome {
    /// Fully recovered canonical accepted input funded by the active owner.
    Accepted(CoreBuffer),
    /// The initialized original database has no row for this exact Turn.
    Missing,
    /// The original writer must initialize the schema or rebind a prepared Turn.
    LegacyFallback,
}

/// Restore an existing accepted input with the exact admitted read-only Pack.
///
/// The evaluator borrows the original row until it returns and must avoid
/// reentering this database.
pub fn read_legacy_saved_input_from_admitted_port(
    root: &DataRoot,
    profile: &ProfileId,
    identity: &OriginalSavedInputIdentity,
    budget: &Arc<dyn AllocationBudget>,
    deadline: Instant,
    mut guard: impl FnMut() -> Result<()>,
    recover: impl FnOnce(OriginalSavedInputRow<'_>) -> Result<CoreBuffer>,
) -> Result<OriginalSavedInputOutcome> {
    let mut live = || check_current(deadline, &mut guard);
    live()?;
    let turn_id = turn_id(identity)?;
    let _sqlite_funding = TemporaryCharge::new(budget, SQLITE_WORK_BYTES)?;
    let lock_deadline = (Instant::now() + SQLITE_LOCK_TIMEOUT).min(deadline);
    let path = database_path(profile);
    let opened = root.with_existing_read_only_database(&path, |connection| {
        let mut prepared = false;
        loop {
            live()?;
            set_busy_timeout(connection, lock_deadline).map_err(read_sql)?;
            let snapshot = (|| {
                if !prepared {
                    cap_connection(connection, deadline)?;
                    authorize(connection, false)?;
                    prepared = true;
                }
                let transaction =
                    Transaction::new_unchecked(connection, TransactionBehavior::Deferred)?;
                let schema = input_schema_present(&transaction)?;
                Ok::<_, rusqlite::Error>((transaction, schema))
            })();
            let (transaction, schema) = match snapshot {
                Err(error) if retry_busy(&error, lock_deadline) => continue,
                Err(error) => return Err(read_sql(error)),
                Ok(snapshot) => snapshot,
            };
            if !schema? {
                live()?;
                transaction.rollback().map_err(read_sql)?;
                return Ok(OriginalSavedInputOutcome::LegacyFallback);
            }
            let mut select = prepare(&transaction, SELECT_INPUT)?;
            let mut rows = select.query([turn_id]).map_err(read_sql)?;
            let accepted = match rows.next().map_err(read_sql)? {
                Some(row) => {
                    let persisted = input_row(row, turn_id)?;
                    live()?;
                    OriginalSavedInputOutcome::Accepted(recover_exact(
                        identity, persisted, recover,
                    )?)
                }
                None => OriginalSavedInputOutcome::Missing,
            };
            drop(rows);
            drop(select);
            live()?;
            transaction.rollback().map_err(read_sql)?;
            return Ok(accepted);
        }
    });
    live()?;
    stored_result(opened)
}

/// Retain the first accepted input for this exact source under the writer lock.
///
/// A concurrent capture of the same source replays the retained projection.
/// Evaluators must return within the deadline and avoid reentering the database.
pub fn capture_legacy_saved_input_from_admitted_port(
    root: &DataRoot,
    profile: &ProfileId,
    identity: &OriginalSavedInputIdentity,
    budget: &Arc<dyn AllocationBudget>,
    deadline: Instant,
    mut guard: impl FnMut(OriginalSavedTurnCheck) -> Result<()> + Send + 'static,
    prepare_projection: impl FnOnce() -> Result<OriginalSavedInputProjection>,
    recover: impl FnOnce(OriginalSavedInputRow<'_>) -> Result<CoreBuffer>,
) -> Result<OriginalSavedInputOutcome> {
    let mut check = move |phase| check_current(deadline, &mut || guard(phase));
    check(OriginalSavedTurnCheck::Live)?;
    let turn_id = turn_id(identity)?;
    let _sqlite_funding = TemporaryCharge::new(budget, SQLITE_WORK_BYTES)?;
    let lock_deadline = (Instant::now() + SQLITE_LOCK_TIMEOUT).min(deadline);
    let path = database_path(profile);
    stored_result(root.with_existing_database(&path, |connection| {
        let mut live = || check(OriginalSavedTurnCheck::Live);
        loop {
            live()?;
            set_busy_timeout(connection, lock_deadline).map_err(read_sql)?;
            match cap_connection(connection, deadline) {
                Err(error) if retry_busy(&error, lock_deadline) => continue,
                result => result.map_err(read_sql)?,
            }
            break;
        }
        authorize(connection, true).map_err(read_sql)?;
        let transaction = begin_immediate(connection, lock_deadline, &mut live)?;
        live()?;
        if !input_schema_present(&transaction).map_err(read_sql)??
            || !super::start::schema_present(&transaction)?
        {
            live()?;
            transaction.rollback().map_err(read_sql)?;
            return Ok(OriginalSavedInputOutcome::LegacyFallback);
        }

        let proposed = prepare_projection()?;
        let proposed_row = projected_row(identity, &proposed)?;
        live()?;
        let recover = {
            let mut select = prepare(&transaction, SELECT_INPUT)?;
            let mut rows = select.query([turn_id]).map_err(read_sql)?;
            match rows.next().map_err(read_sql)? {
                Some(row) => {
                    let persisted = input_row(row, turn_id)?;
                    live()?;
                    let accepted = recover_exact(identity, persisted, recover)?;
                    drop(rows);
                    drop(select);
                    live()?;
                    transaction.rollback().map_err(read_sql)?;
                    return Ok(OriginalSavedInputOutcome::Accepted(accepted));
                }
                None => recover,
            }
        };
        live()?;
        match validate_indexed_turn(&transaction, profile, turn_id, identity, &proposed, budget) {
            Err(Error::RequiresLegacyCodec) => {
                live()?;
                transaction.rollback().map_err(read_sql)?;
                return Ok(OriginalSavedInputOutcome::LegacyFallback);
            }
            result => result?,
        }

        let count: i64 = prepare(&transaction, COUNT_INPUTS)?
            .query_row([], |row| row.get(0))
            .map_err(read_sql)?;
        if count >= SAVED_INPUT_CAPACITY {
            return Err(Error::Conflict);
        }
        live()?;
        let context = proposed_row.context;
        let accepted = recover_exact(identity, proposed_row, recover)?;
        live()?;
        let changed = prepare(&transaction, INSERT_INPUT)?
            .execute(params![
                turn_id,
                identity.source_digest.as_str(),
                proposed.accepted_digest.as_str(),
                context,
            ])
            .map_err(read_sql)?;
        if changed != 1 {
            return Err(Error::Storage);
        }
        commit_current(connection, transaction, deadline, move || {
            check(OriginalSavedTurnCheck::Live)?;
            check(OriginalSavedTurnCheck::Publish)
        })?;
        Ok(OriginalSavedInputOutcome::Accepted(accepted))
    }))
}

fn check_current(deadline: Instant, check: &mut impl FnMut() -> Result<()>) -> Result<()> {
    if Instant::now() >= deadline {
        return Err(Error::Revoked);
    }
    check()?;
    if Instant::now() >= deadline {
        return Err(Error::Revoked);
    }
    Ok(())
}

fn turn_id(identity: &OriginalSavedInputIdentity) -> Result<&str> {
    let text =
        std::str::from_utf8(identity.turn_id.as_bytes()).map_err(|_| Error::InvalidRequest)?;
    let mut bytes = text.bytes();
    if text.len() > 256
        || !bytes
            .next()
            .is_some_and(|byte| byte.is_ascii_alphanumeric())
        || !bytes
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'))
    {
        return Err(Error::InvalidRequest);
    }
    Ok(text)
}

fn input_schema_present(connection: &Connection) -> rusqlite::Result<Result<bool>> {
    let mut select =
        connection.prepare_with_flags(SELECT_SCHEMA, PrepFlags::SQLITE_PREPARE_NO_VTAB)?;
    let mut rows = select.query([])?;
    let Some(row) = rows.next()? else {
        return Ok(Ok(false));
    };
    if row.get_ref(0)?.as_str().ok() != Some("table") || rows.next()?.is_some() {
        return Ok(Err(Error::InvalidRecord));
    }
    Ok(Ok(true))
}

fn input_row<'a>(row: &'a Row<'_>, turn_id: &str) -> Result<OriginalSavedInputRow<'a>> {
    let text = |index| {
        row.get_ref(index)
            .map_err(read_sql)?
            .as_str()
            .map_err(|_| Error::InvalidRecord)
    };
    if text(0)? != turn_id || row.get::<_, i64>(4).map_err(read_sql)? != 0 {
        return Err(Error::InvalidRecord);
    }
    Ok(OriginalSavedInputRow {
        source_digest: text(1)?,
        accepted_digest: text(2)?,
        context: text(3)?,
    })
}

fn projected_row<'a>(
    identity: &OriginalSavedInputIdentity,
    projected: &'a OriginalSavedInputProjection,
) -> Result<OriginalSavedInputRow<'a>> {
    if projected.identity.source_digest != identity.source_digest
        || projected.identity.turn_id.as_bytes() != identity.turn_id.as_bytes()
    {
        return Err(Error::Conflict);
    }
    let context =
        std::str::from_utf8(projected.context.as_bytes()).map_err(|_| Error::InvalidRecord)?;
    if context.len() > RECORD_LIMIT || !context.starts_with('{') || !context.ends_with('}') {
        return Err(Error::InvalidRecord);
    }
    Ok(OriginalSavedInputRow {
        source_digest: projected.identity.source_digest.as_str(),
        accepted_digest: projected.accepted_digest.as_str(),
        context,
    })
}

fn recover_exact(
    identity: &OriginalSavedInputIdentity,
    row: OriginalSavedInputRow<'_>,
    recover: impl FnOnce(OriginalSavedInputRow<'_>) -> Result<CoreBuffer>,
) -> Result<CoreBuffer> {
    let source_digest = Digest::parse(row.source_digest).map_err(|_| Error::InvalidRecord)?;
    let accepted_digest = Digest::parse(row.accepted_digest).map_err(|_| Error::InvalidRecord)?;
    if row.context.is_empty() || row.context.len() > RECORD_LIMIT {
        return Err(Error::InvalidRecord);
    }
    if source_digest != identity.source_digest {
        return Err(Error::Conflict);
    }
    let accepted = recover(row)?;
    let output = accepted.as_bytes();
    if output.len() > MAX_BYTES
        || !output.starts_with(b"{")
        || !output.ends_with(b"}")
        || std::str::from_utf8(output).is_err()
        || Digest::hash(output) != accepted_digest
    {
        return Err(Error::InvalidRecord);
    }
    Ok(accepted)
}

fn validate_indexed_turn(
    transaction: &Transaction<'_>,
    profile: &ProfileId,
    turn_id: &str,
    identity: &OriginalSavedInputIdentity,
    proposed: &OriginalSavedInputProjection,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<()> {
    let mut select = prepare(transaction, SELECT_TURN)?;
    let mut rows = select.query([turn_id]).map_err(read_sql)?;
    if let Some(row) = rows.next().map_err(read_sql)? {
        let row = indexed_turn_row(row, profile, turn_id)?;
        let _funding = TemporaryCharge::new(budget, TRANSITION_WORK_BYTES)?;
        let validation = validate_saved_input_capture_turn(
            row.profile_id,
            row.turn_id,
            row.request_id,
            row.body.as_bytes(),
            &identity.source_digest,
            &proposed.accepted_digest,
        );
        if rows.next().map_err(read_sql)?.is_some() {
            return Err(Error::InvalidRecord);
        }
        validation?;
    }
    Ok(())
}

fn stored_result(
    result: std::result::Result<Result<OriginalSavedInputOutcome>, StoreError>,
) -> Result<OriginalSavedInputOutcome> {
    match result {
        Ok(result) => result,
        Err(StoreError::NotFound) => Ok(OriginalSavedInputOutcome::LegacyFallback),
        Err(StoreError::Sql(error)) => Err(read_sql(error)),
        Err(_) => Err(Error::Storage),
    }
}

fn prepare<'db>(connection: &'db Connection, statement: &str) -> Result<rusqlite::Statement<'db>> {
    connection
        .prepare_with_flags(statement, PrepFlags::SQLITE_PREPARE_NO_VTAB)
        .map_err(read_sql)
}

fn set_busy_timeout(connection: &Connection, lock_deadline: Instant) -> rusqlite::Result<()> {
    connection.busy_timeout(
        lock_deadline
            .saturating_duration_since(Instant::now())
            .min(SQLITE_BUSY_SLICE),
    )
}

fn retry_busy(error: &rusqlite::Error, lock_deadline: Instant) -> bool {
    error.sqlite_error_code() == Some(ErrorCode::DatabaseBusy) && Instant::now() < lock_deadline
}

fn authorize(connection: &Connection, writable: bool) -> rusqlite::Result<()> {
    connection.authorizer(Some(move |context: AuthContext<'_>| {
        if context.accessor.is_some() {
            return Authorization::Deny;
        }
        match (context.database_name, context.action) {
            (_, AuthAction::Select | AuthAction::Transaction { .. })
            | (
                Some("main"),
                AuthAction::Read {
                    table_name: "sqlite_master" | "sqlite_schema",
                    column_name: "type" | "name",
                },
            )
            | (
                Some("main"),
                AuthAction::Read {
                    table_name: "saved_inputs",
                    column_name: "id" | "source_digest" | "accepted_digest" | "context",
                },
            ) => Authorization::Allow,
            (
                _,
                AuthAction::Function {
                    function_name: "count",
                },
            )
            | (
                None | Some("main"),
                AuthAction::Read {
                    table_name: "saved_inputs",
                    column_name: "",
                },
            )
            | (
                Some("main"),
                AuthAction::Read {
                    table_name: "turns",
                    column_name: "id" | "request_id" | "body",
                }
                | AuthAction::Insert {
                    table_name: "saved_inputs",
                },
            ) if writable => Authorization::Allow,
            _ => Authorization::Deny,
        }
    }))
}
