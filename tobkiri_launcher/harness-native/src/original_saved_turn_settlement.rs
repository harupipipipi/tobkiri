//! Indexed read and settlement in the original Turn owner's database.

use rusqlite::{
    Connection, ErrorCode, PrepFlags, Row, Transaction, TransactionBehavior,
    hooks::{AuthAction, AuthContext, Authorization},
    params,
};
use std::{
    panic::{AssertUnwindSafe, catch_unwind},
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tobkiri_canon::ids::ProfileId;
use tobkiri_canon::{AllocationBudget, AllocationKind, AllocationRequest, CoreBuffer, Digest};
pub use tobkiri_pack_saved_turn_runtime_rust::{
    LegacySavedTurnSettlementError, LegacySavedTurnSettlementRequest, LegacySavedTurnStartRequest,
};
use tobkiri_pack_saved_turn_runtime_rust::{RECORD_LIMIT, Result, Transition, settle};
use tobkiri_store::{DataRoot, StoreError};
use tobkiri_wasm::NativeReadOnlyPort;
use wasmtime::component::{BudgetedBox, BudgetedString, Val};

mod start;
mod start_port;
pub use start::{
    OriginalSavedTurnStartInput, OriginalSavedTurnStartOutcome, OriginalSavedTurnStartStep,
    begin_legacy_saved_turn_from_admitted_port, claim_legacy_saved_turn_from_admitted_port,
};
pub use start_port::{
    invoke_original_saved_turn_begin_port, invoke_original_saved_turn_claim_port,
};

#[cfg(all(test, unix))]
mod start_tests;
#[cfg(test)]
mod tests;

const SQLITE_WORK_BYTES: u64 = 5 * 1024 * 1024;
const TRANSITION_WORK_BYTES: u64 = 24 * 1024 * 1024;
const SQLITE_LOCK_TIMEOUT: Duration = Duration::from_secs(5);
const SQLITE_BUSY_SLICE: Duration = Duration::from_millis(25);
const TURNS_OWNER: &str = "packs/rumi_turn_runtime_pack/profiles";
const SELECT_TURN: &str = "SELECT id, request_id, body FROM turns WHERE id = ?1";

type Error = LegacySavedTurnSettlementError;

/// Authorization stage for a single original SQLite settlement.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OriginalSavedTurnCheck {
    /// Check the original caller's continuing eligibility without offering a write.
    Live,
    /// Check eligibility and obtain permission while SQLite holds its commit lock.
    Publish,
}

/// Original indexed row borrowed from the current SQLite statement.
pub struct OriginalSavedTurnRow<'a> {
    /// The already admitted Profile for this operation.
    pub profile_id: &'a str,
    /// Exact indexed Turn and request identifiers.
    pub turn_id: &'a str,
    /// Exact indexed saved request identifier.
    pub request_id: &'a str,
    /// Original compact UTF-8 row JSON.
    pub body: &'a str,
}

/// A bounded decision from the verified Pack, funded by the active owner.
pub enum OriginalSavedTurnDecision {
    /// Return the original record without changing it.
    Replay,
    /// Update the same indexed record under the current transaction.
    Replace {
        /// The timestamp also written to the original index.
        updated_at: i64,
        /// Fully materialized owner record.
        body: CoreBuffer,
    },
}

/// Validate the original row with the admitted read-only Wasm port.
///
/// A valid result preserves the exact original body bytes for the caller.
pub async fn invoke_original_saved_turn_read_port<T: Send + 'static>(
    port: NativeReadOnlyPort<'_, T>,
    row: OriginalSavedTurnRow<'_>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreBuffer> {
    let owner = port.allocation_owners().component();
    let text = |value: &str| {
        BudgetedString::copy_from(value, owner, 1)
            .map(Val::String)
            .map_err(|_| Error::Capacity)
    };
    let parameters = [text(row.turn_id)?, text(row.request_id)?, text(row.body)?];
    let output = port.invoke(&parameters).await.map_err(|_| Error::Storage)?;
    let [Val::Result(Ok(Some(outcome)))] = output.as_slice() else {
        return Err(Error::Storage);
    };
    let Val::Enum(tag) = &**outcome else {
        return Err(Error::Storage);
    };
    match tag.as_str() {
        "valid" => CoreBuffer::copy_from(row.body.as_bytes(), budget).map_err(|_| Error::Capacity),
        "invalid-record" => Err(Error::InvalidRecord),
        "requires-legacy-codec" => Err(Error::RequiresLegacyCodec),
        "capacity" => Err(Error::Capacity),
        _ => Err(Error::Storage),
    }
}

/// Evaluate the exact admitted Wasm port for this indexed row.
pub async fn invoke_original_saved_turn_port<T: Send + 'static>(
    port: NativeReadOnlyPort<'_, T>,
    row: OriginalSavedTurnRow<'_>,
    request: &LegacySavedTurnSettlementRequest<'_>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<OriginalSavedTurnDecision> {
    let owner = port.allocation_owners().component().clone();
    let text = |value: &str| -> Result<Val> {
        BudgetedString::copy_from(value, &owner, 1)
            .map(Val::String)
            .map_err(|_| Error::Capacity)
    };
    let revision = request
        .expected_revision
        .map(|value| BudgetedBox::new(Val::U64(value), &owner, 0).map_err(|_| Error::Capacity))
        .transpose()?;
    let reference =
        std::str::from_utf8(request.result_reference).map_err(|_| Error::InvalidReference)?;
    let parameters = [
        text(row.turn_id)?,
        text(row.request_id)?,
        text(row.body)?,
        text(request.input_digest)?,
        Val::Option(revision),
        text(reference)?,
    ];
    let output = port.invoke(&parameters).await.map_err(|_| Error::Storage)?;
    decode_port_decision(output.as_slice(), budget)
}

fn decode_port_decision(
    output: &[Val],
    budget: &Arc<dyn AllocationBudget>,
) -> Result<OriginalSavedTurnDecision> {
    let [Val::Result(Ok(Some(outcome)))] = output else {
        return Err(Error::Storage);
    };
    let Val::Variant(tag, payload) = &**outcome else {
        return Err(Error::Storage);
    };
    match (tag.as_str(), payload.as_deref()) {
        ("replay", None) => Ok(OriginalSavedTurnDecision::Replay),
        ("update", Some(Val::Record(record))) => {
            let [
                (timestamp, Val::S64(updated_at)),
                (document, Val::String(body)),
            ] = record.as_slice()
            else {
                return Err(Error::Storage);
            };
            let body = body.as_str();
            if timestamp.as_str() != "updated-at" || document.as_str() != "body" {
                return Err(Error::Storage);
            }
            if body.len() > RECORD_LIMIT || !body.starts_with('{') || !body.ends_with('}') {
                return Err(Error::InvalidRecord);
            }
            Ok(OriginalSavedTurnDecision::Replace {
                updated_at: *updated_at,
                body: CoreBuffer::copy_from(body.as_bytes(), budget)
                    .map_err(|_| Error::Capacity)?,
            })
        }
        ("rejected", Some(Val::Enum(code))) => Err(match code.as_str() {
            "invalid-request" => Error::InvalidRequest,
            "invalid-record" => Error::InvalidRecord,
            "invalid-reference" => Error::InvalidReference,
            "not-found" => Error::NotFound,
            "conflict" => Error::Conflict,
            "requires-legacy-codec" => Error::RequiresLegacyCodec,
            "capacity" => Error::Capacity,
            "revoked" => Error::Revoked,
            _ => Error::Storage,
        }),
        _ => Err(Error::Storage),
    }
}

/// Read one original Turn through the admitted Profile's existing database.
///
/// The evaluator runs only for an exact indexed row, and can borrow its text
/// until the SQLite statement closes. It must return before the deadline
/// and avoid reentering this database. Missing files and rows return `None`.
pub fn read_legacy_saved_turn<T>(
    root: &DataRoot,
    profile: &ProfileId,
    turn_id: &str,
    budget: &Arc<dyn AllocationBudget>,
    deadline: Instant,
    mut guard: impl FnMut() -> Result<()>,
    evaluate: impl FnOnce(OriginalSavedTurnRow<'_>) -> Result<T>,
) -> Result<Option<T>> {
    let mut check = || {
        if Instant::now() >= deadline {
            return Err(Error::Revoked);
        }
        guard()?;
        if Instant::now() >= deadline {
            return Err(Error::Revoked);
        }
        Ok(())
    };
    check()?;
    if turn_id.len() > 256 * 4 {
        return Err(Error::InvalidRequest);
    }
    let _sqlite_funding = TemporaryCharge::new(budget, SQLITE_WORK_BYTES)?;
    let lock_deadline = (Instant::now() + SQLITE_LOCK_TIMEOUT).min(deadline);
    let database_result =
        root.with_existing_read_only_database(&database_path(profile), |connection| {
            let mut prepared = false;
            let mut evaluate = Some(evaluate);
            loop {
                check()?;
                connection
                    .busy_timeout(
                        lock_deadline
                            .saturating_duration_since(Instant::now())
                            .min(SQLITE_BUSY_SLICE),
                    )
                    .map_err(read_sql)?;
                let attempt: rusqlite::Result<Result<Option<T>>> = (|| {
                    // The cache PRAGMA can acquire a read lock while loading
                    // the schema, so configure it inside the same busy loop.
                    if !prepared {
                        prepare_read_connection(connection, deadline)?;
                        prepared = true;
                    }
                    let mut statement = connection
                        .prepare_with_flags(SELECT_TURN, PrepFlags::SQLITE_PREPARE_NO_VTAB)?;
                    let mut rows = statement.query([turn_id])?;
                    Ok(rows
                        .next()?
                        .map(|row| {
                            check()?;
                            let row = indexed_turn_row(row, profile, turn_id)?;
                            evaluate.take().ok_or(Error::Storage)?(row)
                        })
                        .transpose())
                })();
                match attempt {
                    Err(error)
                        if error.sqlite_error_code() == Some(ErrorCode::DatabaseBusy)
                            && Instant::now() < lock_deadline => {}
                    Err(error) => return Err(read_sql(error)),
                    Ok(result) => return result,
                }
            }
        });
    check()?;
    match database_result {
        Ok(result) => result,
        Err(StoreError::NotFound) => Ok(None),
        Err(StoreError::Sql(error)) => Err(read_sql(error)),
        Err(_) => Err(Error::Storage),
    }
}

/// Settle an owner-verified receipt using the original SQLite row and write lock.
///
/// The trusted caller supplies its live resource owner and authority check.
/// The operation cannot initialize a turn, publish Conversation data, or accept
/// a different Profile from the request. Replaying the exact completed result
/// does not update the row. The commit check runs under SQLite's writer lock
/// and cannot reenter this database. A caller must reconcile an unconfirmed
/// commit.
pub fn settle_legacy_saved_turn(
    root: &DataRoot,
    profile: &ProfileId,
    request: &LegacySavedTurnSettlementRequest<'_>,
    budget: &Arc<dyn AllocationBudget>,
    deadline: Instant,
    guard: impl FnMut() -> Result<()> + Send + 'static,
) -> Result<CoreBuffer> {
    settle_legacy_saved_turn_with_clock(
        root,
        profile,
        request,
        budget,
        deadline,
        guard,
        system_millis,
    )
}

fn settle_legacy_saved_turn_with_clock(
    root: &DataRoot,
    profile: &ProfileId,
    request: &LegacySavedTurnSettlementRequest<'_>,
    budget: &Arc<dyn AllocationBudget>,
    deadline: Instant,
    mut guard: impl FnMut() -> Result<()> + Send + 'static,
    mut clock: impl FnMut() -> Result<i64>,
) -> Result<CoreBuffer> {
    settle_legacy_saved_turn_from_admitted_port(
        root,
        profile,
        request,
        budget,
        deadline,
        move |phase| match phase {
            OriginalSavedTurnCheck::Live => guard(),
            OriginalSavedTurnCheck::Publish => Ok(()),
        },
        |row| {
            let _funding = TemporaryCharge::new(budget, TRANSITION_WORK_BYTES)?;
            match settle(
                row.profile_id,
                row.turn_id,
                row.request_id,
                row.body.as_bytes(),
                request,
                &mut clock,
            )? {
                Transition::Replay => Ok(OriginalSavedTurnDecision::Replay),
                Transition::Commit { updated_at, body } => Ok(OriginalSavedTurnDecision::Replace {
                    updated_at,
                    body: CoreBuffer::copy_from(&body, budget).map_err(|_| Error::Capacity)?,
                }),
            }
        },
    )
}

/// Apply the bounded decision of the already admitted Pack to the original row.
///
/// The evaluator borrows the indexed row while the writer lock is held. It
/// must return before the original deadline and must never reenter the turn
/// database. The publication check runs only for an actual update, after
/// SQLite has acquired the lock required for commit.
pub fn settle_legacy_saved_turn_from_admitted_port(
    root: &DataRoot,
    profile: &ProfileId,
    request: &LegacySavedTurnSettlementRequest<'_>,
    budget: &Arc<dyn AllocationBudget>,
    deadline: Instant,
    mut guard: impl FnMut(OriginalSavedTurnCheck) -> Result<()> + Send + 'static,
    evaluate: impl FnOnce(OriginalSavedTurnRow<'_>) -> Result<OriginalSavedTurnDecision>,
) -> Result<CoreBuffer> {
    if Digest::parse(request.input_digest).is_err() || request.expected_revision == Some(0) {
        return Err(Error::InvalidRequest);
    }
    if request.turn_id.len() > 256 * 4 {
        return Err(Error::NotFound);
    }
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
    let path = database_path(profile);
    root.with_existing_database(&path, |connection| {
        prepare_connection(connection, deadline).map_err(sql)?;
        let lock_deadline = (now + SQLITE_LOCK_TIMEOUT).min(deadline);
        let transaction = begin_immediate(connection, lock_deadline, &mut || {
            check(OriginalSavedTurnCheck::Live)
        })?;
        check(OriginalSavedTurnCheck::Live)?;

        let (updated_at, output) = {
            let mut select = transaction.prepare(SELECT_TURN).map_err(sql)?;
            let mut rows = select.query([request.turn_id]).map_err(sql)?;
            let row = rows.next().map_err(sql)?.ok_or(Error::NotFound)?;
            let row = indexed_turn_row(row, profile, request.turn_id)?;
            let body = row.body;
            match evaluate(row)? {
                OriginalSavedTurnDecision::Replay => (
                    None,
                    CoreBuffer::copy_from(body.as_bytes(), budget).map_err(|_| Error::Capacity)?,
                ),
                OriginalSavedTurnDecision::Replace { updated_at, body } => {
                    if updated_at <= 0
                        || body.len() > RECORD_LIMIT
                        || !body.as_bytes().starts_with(b"{")
                    {
                        return Err(Error::InvalidRecord);
                    }
                    (Some(updated_at), body)
                }
            }
        };

        match updated_at {
            None => {
                check(OriginalSavedTurnCheck::Live)?;
                transaction.rollback().map_err(sql)?;
            }
            Some(updated_at) => {
                let body =
                    std::str::from_utf8(output.as_bytes()).map_err(|_| Error::InvalidRecord)?;
                let changed = transaction
                    .execute(
                        "UPDATE turns SET updated_at = ?1, body = ?2 WHERE id = ?3",
                        params![updated_at, body, request.turn_id],
                    )
                    .map_err(sql)?;
                if changed != 1 {
                    return Err(Error::Storage);
                }
                commit_current(connection, transaction, deadline, move || {
                    check(OriginalSavedTurnCheck::Live)?;
                    check(OriginalSavedTurnCheck::Publish)
                })?;
            }
        }
        Ok(output)
    })
    .map_err(storage)?
}

fn database_path(profile: &ProfileId) -> PathBuf {
    Path::new(TURNS_OWNER)
        .join(profile.as_str())
        .join("turns.sqlite3")
}

fn indexed_turn_row<'a>(
    row: &'a Row<'_>,
    profile: &'a ProfileId,
    turn_id: &str,
) -> Result<OriginalSavedTurnRow<'a>> {
    let text = |column| {
        row.get_ref(column)
            .map_err(sql)?
            .as_str()
            .map_err(|_| Error::InvalidRecord)
    };
    let stored_turn = text(0)?;
    let stored_request = text(1)?;
    let body = text(2)?;
    if body.len() > RECORD_LIMIT || stored_turn != turn_id {
        return Err(Error::InvalidRecord);
    }
    Ok(OriginalSavedTurnRow {
        profile_id: profile.as_str(),
        turn_id: stored_turn,
        request_id: stored_request,
        body,
    })
}

fn cap_connection(connection: &Connection, deadline: Instant) -> rusqlite::Result<()> {
    // SQLite measures the full encoded row, including its index fields.
    // The body itself has the original exact limit when it is read and written.
    unsafe {
        rusqlite::ffi::sqlite3_limit(
            connection.handle(),
            rusqlite::ffi::SQLITE_LIMIT_LENGTH,
            RECORD_LIMIT as i32 + 4096,
        );
    }
    connection.progress_handler(1000, Some(move || Instant::now() >= deadline))?;
    connection.execute_batch("PRAGMA cache_size=-512")?;
    Ok(())
}

fn prepare_read_connection(connection: &Connection, deadline: Instant) -> rusqlite::Result<()> {
    cap_connection(connection, deadline)?;
    connection.authorizer(Some(|context: AuthContext<'_>| {
        if context.accessor.is_none()
            && (context.action == AuthAction::Select
                || matches!(
                    context,
                    AuthContext {
                        database_name: Some("main"),
                        action: AuthAction::Read {
                            table_name: "turns",
                            column_name: "id" | "request_id" | "body"
                        },
                        ..
                    }
                ))
        {
            Authorization::Allow
        } else {
            Authorization::Deny
        }
    }))?;
    Ok(())
}

fn prepare_connection(connection: &Connection, deadline: Instant) -> rusqlite::Result<()> {
    cap_connection(connection, deadline)?;
    connection.authorizer(Some(|context: AuthContext<'_>| {
        if context.accessor.is_some() {
            Authorization::Deny
        } else {
            Authorization::Allow
        }
    }))?;
    Ok(())
}

fn begin_immediate<'db>(
    connection: &'db Connection,
    lock_deadline: Instant,
    check: &mut impl FnMut() -> Result<()>,
) -> Result<Transaction<'db>> {
    // Each failed BEGIN leaves no transaction. Recheck authority between
    // SQLite's short busy waits rather than holding up a revoked request.
    loop {
        check()?;
        connection
            .busy_timeout(
                lock_deadline
                    .saturating_duration_since(Instant::now())
                    .min(SQLITE_BUSY_SLICE),
            )
            .map_err(sql)?;
        match Transaction::new_unchecked(connection, TransactionBehavior::Immediate) {
            Ok(transaction) => return Ok(transaction),
            Err(error) if error.sqlite_error_code() == Some(ErrorCode::DatabaseBusy) => {
                if Instant::now() >= lock_deadline {
                    check()?;
                    return Err(Error::Storage);
                }
            }
            Err(error) => return Err(sql(error)),
        }
    }
}

fn commit_current(
    connection: &Connection,
    transaction: Transaction<'_>,
    deadline: Instant,
    mut check: impl FnMut() -> Result<()> + Send + 'static,
) -> Result<()> {
    let refused = Arc::new(AtomicBool::new(false));
    let hook_refused = Arc::clone(&refused);
    connection
        .busy_timeout(
            deadline
                .saturating_duration_since(Instant::now())
                .min(SQLITE_LOCK_TIMEOUT),
        )
        .map_err(sql)?;
    // The hook runs after SQLite acquires the locks required for COMMIT, even
    // when a rollback-journal reader delayed the exclusive lock upgrade.
    connection
        .commit_hook(Some(move || {
            let denied = !matches!(catch_unwind(AssertUnwindSafe(&mut check)), Ok(Ok(())));
            hook_refused.store(denied, Ordering::Relaxed);
            denied
        }))
        .map_err(sql)?;
    match transaction.commit() {
        Ok(()) => Ok(()),
        Err(_) if refused.load(Ordering::Relaxed) => Err(Error::Revoked),
        Err(_) => Err(Error::CommitUnconfirmed),
    }
}

fn storage(error: StoreError) -> Error {
    match error {
        StoreError::NotFound => Error::NotFound,
        StoreError::Io(error) if error.kind() == std::io::ErrorKind::NotFound => Error::NotFound,
        StoreError::Sql(error) => sql(error),
        _ => Error::Storage,
    }
}

fn sql(error: rusqlite::Error) -> Error {
    match error.sqlite_error_code() {
        Some(ErrorCode::OperationInterrupted) => Error::Revoked,
        Some(ErrorCode::TooBig) => Error::InvalidRecord,
        _ => Error::Storage,
    }
}

fn read_sql(error: rusqlite::Error) -> Error {
    match error.sqlite_error_code() {
        Some(
            ErrorCode::DatabaseCorrupt
            | ErrorCode::NotADatabase
            | ErrorCode::AuthorizationForStatementDenied,
        ) => Error::InvalidRecord,
        Some(ErrorCode::OutOfMemory) => Error::Capacity,
        _ => sql(error),
    }
}

fn system_millis() -> Result<i64> {
    let elapsed = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| Error::Storage)?;
    i64::try_from(elapsed.as_millis()).map_err(|_| Error::Storage)
}

struct TemporaryCharge<'a> {
    budget: &'a Arc<dyn AllocationBudget>,
    bytes: u64,
}

impl<'a> TemporaryCharge<'a> {
    fn new(budget: &'a Arc<dyn AllocationBudget>, bytes: u64) -> Result<Self> {
        budget
            .reserve(AllocationRequest {
                kind: AllocationKind::Temporary,
                bytes,
                elements: 0,
                depth: 0,
                work: 0,
            })
            .map_err(|_| Error::Capacity)?;
        Ok(Self { budget, bytes })
    }
}

impl Drop for TemporaryCharge<'_> {
    fn drop(&mut self) {
        self.budget.release(self.bytes);
    }
}
