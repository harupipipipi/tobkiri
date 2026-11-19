//! Settlement of an already verified outcome in the original Turn owner's database.

use rusqlite::{
    Connection, ErrorCode, Transaction, TransactionBehavior,
    hooks::{AuthContext, Authorization},
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
    LegacySavedTurnSettlementError, LegacySavedTurnSettlementRequest,
};
use tobkiri_pack_saved_turn_runtime_rust::{RECORD_LIMIT, Result, Transition, settle};
use tobkiri_store::{DataRoot, StoreError};

#[cfg(test)]
mod tests;

const SQLITE_WORK_BYTES: u64 = 5 * 1024 * 1024;
const TRANSITION_WORK_BYTES: u64 = 24 * 1024 * 1024;
const SQLITE_LOCK_TIMEOUT: Duration = Duration::from_secs(5);
const SQLITE_BUSY_SLICE: Duration = Duration::from_millis(25);
const TURNS_OWNER: &str = "packs/rumi_turn_runtime_pack/profiles";

type Error = LegacySavedTurnSettlementError;

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
    if Digest::parse(request.input_digest).is_err() || request.expected_revision == Some(0) {
        return Err(Error::InvalidRequest);
    }
    if request.turn_id.len() > 256 * 4 {
        return Err(Error::NotFound);
    }
    let now = Instant::now();
    let mut check = move || {
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
    let _sqlite_funding = TemporaryCharge::new(budget, SQLITE_WORK_BYTES)?;
    let path = database_path(profile);
    root.with_existing_database(&path, |connection| {
        prepare_connection(connection, deadline).map_err(sql)?;
        let lock_deadline = (now + SQLITE_LOCK_TIMEOUT).min(deadline);
        let transaction = begin_immediate(connection, lock_deadline, &mut check)?;
        check()?;

        let (updated_at, output) = {
            let mut select = transaction
                .prepare("SELECT id, request_id, body FROM turns WHERE id = ?1")
                .map_err(sql)?;
            let mut rows = select.query([request.turn_id]).map_err(sql)?;
            let row = rows.next().map_err(sql)?.ok_or(Error::NotFound)?;
            let stored_turn = row
                .get_ref(0)
                .map_err(sql)?
                .as_str()
                .map_err(|_| Error::InvalidRecord)?;
            let stored_request = row
                .get_ref(1)
                .map_err(sql)?
                .as_str()
                .map_err(|_| Error::InvalidRecord)?;
            let body = row
                .get_ref(2)
                .map_err(sql)?
                .as_str()
                .map_err(|_| Error::InvalidRecord)?;
            if body.len() > RECORD_LIMIT || stored_turn != request.turn_id {
                return Err(Error::InvalidRecord);
            }
            let _transition_funding = TemporaryCharge::new(budget, TRANSITION_WORK_BYTES)?;
            let transition = settle(
                profile.as_str(),
                request.turn_id,
                stored_request,
                body.as_bytes(),
                request,
                &mut clock,
            )?;
            match transition {
                Transition::Replay => (
                    None,
                    CoreBuffer::copy_from(body.as_bytes(), budget).map_err(|_| Error::Capacity)?,
                ),
                Transition::Commit { updated_at, body } => (
                    Some(updated_at),
                    CoreBuffer::copy_from(&body, budget).map_err(|_| Error::Capacity)?,
                ),
            }
        };

        match updated_at {
            None => {
                check()?;
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
                commit_current(connection, transaction, deadline, check)?;
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

fn prepare_connection(connection: &Connection, deadline: Instant) -> rusqlite::Result<()> {
    // SQLite measures the full encoded row, including its index fields.
    // The body itself has the original exact limit when it is read and written.
    unsafe {
        rusqlite::ffi::sqlite3_limit(
            connection.handle(),
            rusqlite::ffi::SQLITE_LIMIT_LENGTH,
            RECORD_LIMIT as i32 + 4096,
        );
    }
    connection.execute_batch("PRAGMA cache_size=-512")?;
    connection.progress_handler(1000, Some(move || Instant::now() >= deadline))?;
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
