//! The original Calendar adapter's profile-local source ledger.

use crate::OriginalSavedTurnCheck::{self, Live, Publish};
use anyhow::{Result, anyhow, bail};
use rusqlite::{
    Connection, ErrorCode, OptionalExtension, PrepFlags, Row, Transaction, TransactionBehavior,
    ffi,
    hooks::{AuthAction, AuthContext, Authorization},
    params,
    types::ValueRef,
};
use std::{
    ffi::{c_int, c_void},
    panic::{AssertUnwindSafe, catch_unwind},
    path::{Path, PathBuf},
    ptr,
    sync::Arc,
    time::{Duration, Instant},
};
use tobkiri_canon::{
    AllocationBudget, AllocationError, AllocationKind, AllocationRequest, CoreBuffer, CoreValue,
    Digest, ids::ProfileId,
};
use tobkiri_store::{DataRoot, StoreError};

#[cfg(test)]
#[path = "original_scheduled_source_ledger_tests.rs"]
mod tests;

const INPUT_LIMIT: usize = 256 * 1024;
const RECORD_LIMIT: usize = 128 * 1024;
const OUTPUT_LIMIT: usize = RECORD_LIMIT + 128;
const SQLITE_WORK_BYTES: u64 = 5 * 1024 * 1024;
const LOCK_TIMEOUT: Duration = Duration::from_secs(10);
const BUSY_SLICE: Duration = Duration::from_millis(25);

// SQLite omits IF NOT EXISTS when storing the original Python DDL.
const TABLES: [(&str, &str, &str); 3] = [
    (
        "jobs",
        "CREATE TABLE jobs (job_key TEXT PRIMARY KEY, digest TEXT NOT NULL, receipt TEXT)",
        "sqlite_autoindex_jobs_1",
    ),
    (
        "destinations",
        "CREATE TABLE destinations (job_key TEXT PRIMARY KEY, intent_digest TEXT NOT NULL, target_id TEXT NOT NULL)",
        "sqlite_autoindex_destinations_1",
    ),
    (
        "sources",
        "CREATE TABLE sources (job_key TEXT NOT NULL, turn_id TEXT NOT NULL, source TEXT NOT NULL, PRIMARY KEY(job_key, turn_id))",
        "sqlite_autoindex_sources_1",
    ),
];
const ALL_TABLES: u8 = (1 << TABLES.len()) - 1;

/// One operation on the original persisted Calendar dispatch data.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OriginalScheduledLedgerOperation {
    /// Reserve the original envelope or return its saved receipt.
    Admit,
    /// Read the originally bound source for a turn.
    RecoverSource,
    /// Keep the first source for a turn in an admitted job.
    BindSource,
    /// Test whether a job has any bound source.
    HasSource,
    /// Keep the first destination intent and target for a job.
    BindDestination,
    /// Record an actual receipt for an admitted job.
    Finish,
}

impl OriginalScheduledLedgerOperation {
    /// The exact operation name on the native owner port.
    pub fn operation_id(self) -> &'static str {
        match self {
            Self::Admit => "admit",
            Self::RecoverSource => "recover-source",
            Self::BindSource => "bind-source",
            Self::HasSource => "has-source",
            Self::BindDestination => "bind-destination",
            Self::Finish => "finish",
        }
    }

    /// Whether the operation may need to write to SQLite.
    pub fn permits_write(self) -> bool {
        !matches!(self, Self::RecoverSource | Self::HasSource)
    }
}

/// Expected failures from the original Calendar source ledger.
#[derive(Clone, Copy, Debug, Eq, PartialEq, thiserror::Error)]
pub enum OriginalScheduledLedgerError {
    /// Invalid operation frame or excessive source or receipt size.
    #[error("invalid scheduled source ledger input")]
    Invalid,
    /// A changed binding, unavailable admission, or stale authorization.
    #[error("scheduled source ledger operation denied")]
    Denied,
    /// The active owner could not fund this operation.
    #[error("scheduled source ledger capacity exceeded")]
    Capacity,
}

use OriginalScheduledLedgerError as Error;
use OriginalScheduledLedgerOperation as Operation;

fn database_path(profile: &ProfileId) -> PathBuf {
    Path::new("packs/rumi_turn_runtime_pack/profiles")
        .join(profile.as_str())
        .join("scheduled-chat-jobs.sqlite3")
}

enum Call {
    Admit(Digest),
    RecoverSource(CoreValue),
    BindSource { turn: CoreValue, source: CoreBuffer },
    HasSource,
    BindDestination { digest: Digest, target: CoreValue },
    Finish(CoreBuffer),
}

impl Call {
    fn parse(
        operation: Operation,
        input: &[u8],
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<(CoreValue, Self)> {
        if input.len() > INPUT_LIMIT {
            return Err(Error::Invalid.into());
        }
        let frame = CoreValue::parse_guest(input, budget).map_err(allocation_error)?;
        if frame
            .encode_guest(budget)
            .map_err(allocation_error)?
            .as_bytes()
            != input
        {
            return Err(Error::Invalid.into());
        }
        let fields = frame.object().ok_or(Error::Invalid)?;
        let key = fields.get("key").ok_or(Error::Invalid)?;
        identifier(&key)?;
        let required = match operation {
            Operation::HasSource => 1,
            Operation::BindDestination => 3,
            _ => 2,
        };
        if fields.len() != required {
            return Err(Error::Invalid.into());
        }
        let get = |name| fields.get(name).ok_or(Error::Invalid);
        let call = match operation {
            Operation::Admit => {
                let envelope = get("envelope")?;
                envelope.object().ok_or(Error::Invalid)?;
                Self::Admit(envelope.digest_guest(budget).map_err(allocation_error)?)
            }
            Operation::RecoverSource => {
                let turn = get("turn_id")?;
                identifier(&turn)?;
                Self::RecoverSource(turn)
            }
            Operation::BindSource => {
                let source = get("source")?;
                source.object().ok_or(Error::Invalid)?;
                let request = source.get_key("request").unwrap_or_else(|| source.share());
                let turn = request.get_key("turn_id").ok_or(Error::Invalid)?;
                identifier(&turn)?;
                Self::BindSource {
                    turn,
                    source: encode_record(&source, budget)?,
                }
            }
            Operation::HasSource => Self::HasSource,
            Operation::BindDestination => {
                let intent = get("intent")?;
                intent.object().ok_or(Error::Invalid)?;
                let target = get("target")?;
                identifier(&target)?;
                Self::BindDestination {
                    digest: intent.digest_guest(budget).map_err(allocation_error)?,
                    target,
                }
            }
            Operation::Finish => Self::Finish(encode_record(&get("receipt")?, budget)?),
        };
        Ok((key, call))
    }
}

fn identifier(value: &CoreValue) -> Result<&str> {
    let text = value.as_str().ok_or(Error::Invalid)?;
    let mut bytes = text.bytes();
    if text.len() > 256
        || !bytes
            .next()
            .is_some_and(|byte| byte.is_ascii_alphanumeric())
        || !bytes.all(|byte| {
            byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'/' | b'-')
        })
    {
        return Err(Error::Invalid.into());
    }
    Ok(text)
}

fn allocation_error(error: AllocationError) -> Error {
    match error {
        AllocationError::Invalid | AllocationError::Depth => Error::Invalid,
        _ => Error::Capacity,
    }
}

fn encode_record(value: &CoreValue, budget: &Arc<dyn AllocationBudget>) -> Result<CoreBuffer> {
    value.object().ok_or(Error::Invalid)?;
    let bytes = value.encode_guest(budget).map_err(allocation_error)?;
    if bytes.len() > RECORD_LIMIT {
        return Err(Error::Invalid.into());
    }
    Ok(bytes)
}

fn stored_record(value: ValueRef<'_>, budget: &Arc<dyn AllocationBudget>) -> Result<CoreBuffer> {
    let bytes = value.as_str()?.as_bytes();
    if bytes.len() > RECORD_LIMIT {
        bail!("invalid stored scheduled source ledger record");
    }
    let value = CoreValue::parse_guest(bytes, budget).map_err(allocation_error)?;
    encode_record(&value, budget)
}

fn output(bytes: &[u8], budget: &Arc<dyn AllocationBudget>) -> Result<CoreBuffer> {
    let prefix = br#"{"value":"#;
    let length = prefix.len() + bytes.len() + 1;
    if length > OUTPUT_LIMIT {
        return Err(Error::Invalid.into());
    }
    Ok(CoreBuffer::write_exact(length, budget, |out| {
        out[..prefix.len()].copy_from_slice(prefix);
        out[prefix.len()..length - 1].copy_from_slice(bytes);
        out[length - 1] = b'}';
        Ok(())
    })
    .map_err(allocation_error)?)
}

/// Execute one already authenticated Calendar adapter operation in its original database.
///
/// The guard runs at eligibility checkpoints and inside SQLite's commit hook for
/// a change. It must avoid reentering the database. Opening and reading an
/// absent ledger does not create its directory or file.
pub fn execute_original_scheduled_source_ledger(
    root: &DataRoot,
    profile: &ProfileId,
    operation: OriginalScheduledLedgerOperation,
    input: &[u8],
    budget: &Arc<dyn AllocationBudget>,
    deadline: Instant,
    mut guard: impl FnMut(OriginalSavedTurnCheck) -> Result<()>,
) -> Result<CoreBuffer> {
    let mut check = |phase| {
        if Instant::now() >= deadline {
            return Err(Error::Denied.into());
        }
        guard(phase)?;
        if Instant::now() >= deadline {
            return Err(Error::Denied.into());
        }
        Ok(())
    };
    check(Live)?;
    let (key, call) = Call::parse(operation, input, budget)?;
    let key = identifier(&key)?;
    let _funding = SqliteCharge::new(budget)?;
    let path = database_path(profile);
    let lock_deadline = (Instant::now() + LOCK_TIMEOUT).min(deadline);
    check(Live)?;
    let run = |db: &Connection| {
        let behavior = if operation.permits_write() {
            TransactionBehavior::Immediate
        } else {
            TransactionBehavior::Deferred
        };
        let mut prepared = false;
        let (transaction, schema) = retry_busy(db, lock_deadline, &mut check, || {
            if !prepared {
                limit_connection(db, deadline)?;
                authorize(db, operation, false)?;
                prepared = true;
            }
            let transaction = Transaction::new_unchecked(db, behavior)?;
            let schema = ledger_schema(&transaction)?;
            Ok((transaction, schema))
        })?;
        check(Live)?;
        let schema = schema?;
        let initialized = schema != ALL_TABLES;
        if initialized {
            if operation != Operation::Admit {
                if !operation.permits_write() && schema == 0 {
                    let result = missing_output(operation, budget)?;
                    check(Live)?;
                    transaction.rollback().map_err(sql_error)?;
                    return Ok(result);
                }
                bail!("scheduled source ledger schema incomplete");
            }
            authorize(db, operation, true).map_err(sql_error)?;
            for (index, (_, create, _)) in TABLES.iter().enumerate() {
                if schema & (1 << index) == 0 {
                    transaction.execute_batch(create).map_err(sql_error)?;
                }
            }
            authorize(db, operation, false).map_err(sql_error)?;
        }
        let (result, changed) = execute_call(&transaction, key, call, budget)?;
        if initialized || changed {
            commit_current(db, transaction, lock_deadline, &mut check)?;
        } else {
            check(Live)?;
            transaction.rollback().map_err(sql_error)?;
        }
        Ok(result)
    };
    let opened = match operation {
        Operation::Admit => root.with_creatable_database(&path, |db, _| run(db)),
        Operation::RecoverSource | Operation::HasSource => {
            root.with_existing_read_only_database(&path, run)
        }
        _ => root.with_existing_database(&path, run),
    };
    match opened {
        Ok(result) => result,
        Err(StoreError::NotFound) if !operation.permits_write() => {
            let result = missing_output(operation, budget)?;
            check(Live)?;
            Ok(result)
        }
        Err(StoreError::NotFound) => Err(Error::Denied.into()),
        Err(StoreError::Sql(error)) => Err(sql_error(error)),
        Err(error) => Err(error.into()),
    }
}

fn missing_output(operation: Operation, budget: &Arc<dyn AllocationBudget>) -> Result<CoreBuffer> {
    let bytes: &[u8] = if operation == Operation::HasSource {
        b"false"
    } else {
        b"null"
    };
    output(bytes, budget)
}

fn execute_call(
    db: &Connection,
    key: &str,
    call: Call,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<(CoreBuffer, bool)> {
    match call {
        Call::Admit(digest) => {
            let old = prepare(db, "SELECT digest, receipt FROM main.jobs WHERE job_key=?1")?
                .query_row([key], |row| {
                    Ok((|| {
                        if row.get_ref(0)?.as_str()? != digest.as_str() {
                            return Err(Error::Denied.into());
                        }
                        let receipt = row.get_ref(1)?;
                        if receipt == ValueRef::Null {
                            return output(b"null", budget);
                        }
                        output(stored_record(receipt, budget)?.as_bytes(), budget)
                    })())
                })
                .optional()
                .map_err(sql_error)?;
            if let Some(receipt) = old {
                return receipt.map(|value| (value, false));
            }
            change(
                db,
                "INSERT INTO main.jobs(job_key,digest) VALUES (?1,?2)",
                params![key, digest.as_str()],
            )?;
            Ok((output(b"null", budget)?, true))
        }
        Call::RecoverSource(turn) => {
            let source = read_source(db, key, identifier(&turn)?, budget)?;
            let bytes = source.as_ref().map_or(&b"null"[..], CoreBuffer::as_bytes);
            Ok((output(bytes, budget)?, false))
        }
        Call::BindSource { turn, source } => {
            let turn = identifier(&turn)?;
            if !prepare(db, "SELECT job_key FROM main.jobs WHERE job_key=?1")?
                .exists([key])
                .map_err(sql_error)?
            {
                return Err(Error::Denied.into());
            }
            if let Some(old) = read_source(db, key, turn, budget)? {
                if old.as_bytes() != source.as_bytes() {
                    return Err(Error::Denied.into());
                }
                return Ok((output(old.as_bytes(), budget)?, false));
            }
            change(
                db,
                "INSERT INTO main.sources VALUES (?1,?2,?3)",
                params![key, turn, source.as_str()?],
            )?;
            Ok((output(source.as_bytes(), budget)?, true))
        }
        Call::HasSource => {
            let found = prepare(
                db,
                "SELECT turn_id FROM main.sources WHERE job_key=?1 LIMIT 1",
            )?
            .exists([key])
            .map_err(sql_error)?;
            let bytes: &[u8] = if found { b"true" } else { b"false" };
            Ok((output(bytes, budget)?, false))
        }
        Call::BindDestination { digest, target } => {
            let target_text = identifier(&target)?;
            let old = prepare(
                db,
                "SELECT intent_digest,target_id FROM main.destinations WHERE job_key=?1",
            )?
            .query_row([key], |row| {
                Ok(
                    row.get_ref(0)? == ValueRef::Text(digest.as_str().as_bytes())
                        && row.get_ref(1)? == ValueRef::Text(target_text.as_bytes()),
                )
            })
            .optional()
            .map_err(sql_error)?;
            let changed = match old {
                Some(true) => false,
                Some(false) => return Err(Error::Denied.into()),
                None => {
                    change(
                        db,
                        "INSERT INTO main.destinations VALUES (?1,?2,?3)",
                        params![key, digest.as_str(), target_text],
                    )?;
                    true
                }
            };
            let bytes = target.encode_guest(budget).map_err(allocation_error)?;
            Ok((output(bytes.as_bytes(), budget)?, changed))
        }
        Call::Finish(receipt) => {
            let previous = prepare(db, "SELECT receipt FROM main.jobs WHERE job_key=?1")?
                .query_row([key], |row| {
                    Ok(row.get_ref(0)? == ValueRef::Text(receipt.as_bytes()))
                })
                .optional()
                .map_err(sql_error)?
                .ok_or(Error::Denied)?;
            if !previous {
                change(
                    db,
                    "UPDATE main.jobs SET receipt=?1 WHERE job_key=?2",
                    params![receipt.as_str()?, key],
                )?;
            }
            Ok((output(receipt.as_bytes(), budget)?, !previous))
        }
    }
}

fn read_source(
    db: &Connection,
    key: &str,
    turn: &str,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Option<CoreBuffer>> {
    prepare(
        db,
        "SELECT source FROM main.sources WHERE job_key=?1 AND turn_id=?2",
    )?
    .query_row(params![key, turn], |row| {
        Ok(stored_record(row.get_ref(0)?, budget))
    })
    .optional()
    .map_err(sql_error)?
    .transpose()
}

fn change(db: &Connection, statement: &str, params: impl rusqlite::Params) -> Result<()> {
    if prepare(db, statement)?.execute(params).map_err(sql_error)? != 1 {
        bail!("scheduled source ledger change did not affect its exact row");
    }
    Ok(())
}

fn prepare<'a>(db: &'a Connection, statement: &str) -> Result<rusqlite::Statement<'a>> {
    db.prepare_with_flags(statement, PrepFlags::SQLITE_PREPARE_NO_VTAB)
        .map_err(sql_error)
}

// Comparing the original DDL also rejects virtual tables, extra indexes, and
// triggers or views that could repurpose statements using this writer.
fn ledger_schema(db: &Connection) -> rusqlite::Result<Result<u8>> {
    let mut statement = db.prepare_with_flags(
        "SELECT type,name,tbl_name,rootpage,sql FROM main.sqlite_schema",
        PrepFlags::SQLITE_PREPARE_NO_VTAB,
    )?;
    let mut rows = statement.query([])?;
    let mut tables = 0;
    let mut indexes = 0;
    while let Some(row) = rows.next()? {
        if let Err(error) = inspect_schema_row(row, &mut tables, &mut indexes) {
            return Ok(Err(error));
        }
    }
    if tables != indexes {
        return Ok(Err(anyhow!("scheduled source ledger index missing")));
    }
    Ok(Ok(tables))
}

fn inspect_schema_row(row: &Row<'_>, tables: &mut u8, indexes: &mut u8) -> Result<()> {
    let text = |column| -> Result<_> { Ok(row.get_ref(column)?.as_str()?) };
    let (kind, name, table) = (text(0)?, text(1)?, text(2)?);
    let (index, &(_, create, autoindex)) = TABLES
        .iter()
        .enumerate()
        .find(|(_, (candidate, _, _))| *candidate == table)
        .ok_or_else(|| anyhow!("unexpected scheduled source ledger schema"))?;
    let bits = match kind {
        "table" if name == table && text(4)? == create => tables,
        "index" if name == autoindex && row.get_ref(4)? == ValueRef::Null => indexes,
        _ => bail!("unexpected scheduled source ledger schema"),
    };
    let bit = 1 << index;
    if *bits & bit != 0 || row.get::<_, i64>(3)? <= 0 {
        bail!("invalid scheduled source ledger schema");
    }
    *bits |= bit;
    Ok(())
}

fn authorize(db: &Connection, operation: Operation, initialize: bool) -> rusqlite::Result<()> {
    db.authorizer(Some(move |context: AuthContext<'_>| {
        if context.accessor.is_some() {
            return Authorization::Deny;
        }
        let allowed = match (context.database_name, context.action) {
            (_, AuthAction::Select | AuthAction::Transaction { .. })
            | (
                Some("main"),
                AuthAction::Read {
                    table_name: "sqlite_schema" | "sqlite_master",
                    column_name: "type" | "name" | "tbl_name" | "rootpage" | "sql",
                },
            ) => true,
            (
                Some("main"),
                AuthAction::Read {
                    table_name: "jobs",
                    column_name: "job_key" | "digest" | "receipt",
                },
            ) => matches!(
                operation,
                Operation::Admit | Operation::BindSource | Operation::Finish
            ),
            (
                Some("main"),
                AuthAction::Read {
                    table_name: "destinations",
                    column_name: "job_key" | "intent_digest" | "target_id",
                },
            ) => operation == Operation::BindDestination || initialize,
            (
                Some("main"),
                AuthAction::Read {
                    table_name: "sources",
                    column_name: "job_key" | "turn_id" | "source",
                },
            ) => {
                matches!(
                    operation,
                    Operation::RecoverSource | Operation::BindSource | Operation::HasSource
                ) || initialize
            }
            (Some("main"), AuthAction::Insert { table_name: "jobs" }) => {
                operation == Operation::Admit
            }
            (
                Some("main"),
                AuthAction::Insert {
                    table_name: "sources",
                },
            ) => operation == Operation::BindSource,
            (
                Some("main"),
                AuthAction::Insert {
                    table_name: "destinations",
                },
            ) => operation == Operation::BindDestination,
            (
                Some("main"),
                AuthAction::Update {
                    table_name: "jobs",
                    column_name: "receipt",
                },
            ) => operation == Operation::Finish,
            (
                Some("main"),
                AuthAction::CreateTable {
                    table_name: "jobs" | "destinations" | "sources",
                }
                | AuthAction::CreateIndex {
                    index_name:
                        "sqlite_autoindex_jobs_1"
                        | "sqlite_autoindex_destinations_1"
                        | "sqlite_autoindex_sources_1",
                    ..
                }
                | AuthAction::Insert {
                    table_name: "sqlite_schema" | "sqlite_master",
                }
                | AuthAction::Update {
                    table_name: "sqlite_schema" | "sqlite_master",
                    column_name: "type" | "name" | "tbl_name" | "rootpage" | "sql",
                }
                | AuthAction::Read {
                    table_name: "sqlite_schema" | "sqlite_master",
                    column_name: "ROWID",
                },
            ) => initialize,
            _ => false,
        };
        if allowed {
            Authorization::Allow
        } else {
            Authorization::Deny
        }
    }))
}

fn limit_connection(db: &Connection, deadline: Instant) -> rusqlite::Result<()> {
    // Include the compound source key in SQLite's row-length limit.
    unsafe {
        ffi::sqlite3_limit(
            db.handle(),
            ffi::SQLITE_LIMIT_LENGTH,
            RECORD_LIMIT as i32 + 4096,
        );
    }
    db.progress_handler(1000, Some(move || Instant::now() >= deadline))?;
    db.execute_batch("PRAGMA cache_size=-512")?;
    Ok(())
}

fn set_busy_timeout(db: &Connection, lock_deadline: Instant) -> Result<()> {
    db.busy_timeout(
        lock_deadline
            .saturating_duration_since(Instant::now())
            .min(BUSY_SLICE),
    )
    .map_err(sql_error)
}

fn is_retryable(error: &rusqlite::Error, lock_deadline: Instant) -> bool {
    error.sqlite_error_code() == Some(ErrorCode::DatabaseBusy) && Instant::now() < lock_deadline
}

fn retry_busy<T>(
    db: &Connection,
    lock_deadline: Instant,
    check: &mut impl FnMut(OriginalSavedTurnCheck) -> Result<()>,
    mut work: impl FnMut() -> rusqlite::Result<T>,
) -> Result<T> {
    loop {
        check(Live)?;
        set_busy_timeout(db, lock_deadline)?;
        match work() {
            Err(error) if is_retryable(&error, lock_deadline) => {}
            result => return result.map_err(sql_error),
        }
    }
}

struct CommitCheck<'a, F> {
    guard: &'a mut F,
    refused: Option<anyhow::Error>,
    published: bool,
}

unsafe extern "C" fn on_commit<F>(pointer: *mut c_void) -> c_int
where
    F: FnMut(OriginalSavedTurnCheck) -> Result<()>,
{
    // SAFETY: The scoped installation passes a live, exclusively borrowed
    // CommitCheck for this connection. SQLite calls it synchronously.
    let state = unsafe { &mut *pointer.cast::<CommitCheck<'_, F>>() };
    match catch_unwind(AssertUnwindSafe(|| {
        (state.guard)(Live)?;
        (state.guard)(Publish)
    })) {
        Ok(Ok(())) => {
            state.published = true;
            0
        }
        Ok(Err(error)) => {
            state.refused = Some(error);
            1
        }
        Err(_) => {
            state.refused = Some(Error::Denied.into());
            1
        }
    }
}

struct RemoveCommitHook<'a>(&'a Connection);

impl Drop for RemoveCommitHook<'_> {
    fn drop(&mut self) {
        // SAFETY: The same private connection remains live while the hook is
        // removed, before its callback state is accessed or dropped.
        unsafe { ffi::sqlite3_commit_hook(self.0.handle(), None, ptr::null_mut()) };
    }
}

fn commit_current(
    db: &Connection,
    transaction: Transaction<'_>,
    lock_deadline: Instant,
    check: &mut impl FnMut(OriginalSavedTurnCheck) -> Result<()>,
) -> Result<()> {
    fn attempt<F>(db: &Connection, state: &mut CommitCheck<'_, F>) -> rusqlite::Result<()>
    where
        F: FnMut(OriginalSavedTurnCheck) -> Result<()>,
    {
        // SAFETY: This private connection's hook is installed only for the
        // synchronous COMMIT and removed on every exit from this scope.
        unsafe {
            ffi::sqlite3_commit_hook(
                db.handle(),
                Some(on_commit::<F>),
                ptr::from_mut(state).cast(),
            );
        }
        let _remove = RemoveCommitHook(db);
        db.execute_batch("COMMIT")
    }

    loop {
        check(Live)?;
        set_busy_timeout(db, lock_deadline)?;
        let mut state = CommitCheck {
            guard: check,
            refused: None,
            published: false,
        };
        // Keep the transaction alive across busy commit attempts; dropping it
        // on error rolls back before returning to the caller.
        debug_assert!(!transaction.is_autocommit());
        let result = attempt(db, &mut state);
        if let Some(error) = state.refused {
            return Err(error);
        }
        match result {
            Ok(()) if state.published => return Ok(()),
            Err(error)
                if !state.published
                    && error.sqlite_error_code() == Some(ErrorCode::DatabaseBusy) =>
            {
                if Instant::now() >= lock_deadline {
                    check(Live)?;
                    return Err(sql_error(error));
                }
            }
            _ => return Err(StoreError::CommitUnknown.into()),
        }
    }
}

fn sql_error(error: rusqlite::Error) -> anyhow::Error {
    match error.sqlite_error_code() {
        Some(ErrorCode::OperationInterrupted) => Error::Denied.into(),
        Some(ErrorCode::OutOfMemory) => Error::Capacity.into(),
        _ => StoreError::Sql(error).into(),
    }
}

struct SqliteCharge<'a>(&'a Arc<dyn AllocationBudget>);

impl<'a> SqliteCharge<'a> {
    fn new(budget: &'a Arc<dyn AllocationBudget>) -> Result<Self> {
        budget
            .reserve(AllocationRequest {
                kind: AllocationKind::Temporary,
                bytes: SQLITE_WORK_BYTES,
                elements: 0,
                depth: 0,
                work: 0,
            })
            .map_err(allocation_error)?;
        Ok(Self(budget))
    }
}

impl Drop for SqliteCharge<'_> {
    fn drop(&mut self) {
        self.0.release(SQLITE_WORK_BYTES);
    }
}
