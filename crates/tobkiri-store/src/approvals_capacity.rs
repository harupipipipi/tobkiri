//! Durable future-growth escrow for the private approvals database.
//!
//! Complete receipts occupy actual SQLite pages forever. Only unfinished
//! lifecycles retain unspent escrow. Ordinary writes cannot debit that escrow.
use super::{
    FAULT_METADATA_PAGES, MAX_TERMINAL_BYTES, MAX_TERMINAL_ENTRY_BYTES, TERMINAL_METADATA_PAGES,
    persistence,
};
use rusqlite::{Connection, OptionalExtension, Transaction, params};
use tobkiri_gate::{GateError, IntegrityFaultCode};

const LIMIT: u64 = MAX_TERMINAL_BYTES as u64;
const ACTIVE: &str = "active";
const TERMINAL: &str = "terminal";
const CLOSED: &str = "closed";

fn integrity() -> GateError {
    GateError::Integrity(IntegrityFaultCode::AuthorityDatabase)
}
fn exhausted() -> GateError {
    GateError::Limit("live reservation quota".into())
}
fn number(bytes: &[u8]) -> Result<u64, GateError> {
    Ok(u64::from_be_bytes(
        bytes.try_into().map_err(|_| integrity())?,
    ))
}
fn page_size(conn: &Connection) -> Result<u64, GateError> {
    let size: i64 = conn
        .query_row("PRAGMA page_size", [], |row| row.get(0))
        .map_err(persistence)?;
    let size = u64::try_from(size).map_err(|_| integrity())?;
    if !(512..=65536).contains(&size) || !size.is_power_of_two() {
        return Err(integrity());
    }
    Ok(size)
}
fn database_bytes(conn: &Connection) -> Result<u64, GateError> {
    let pages: i64 = conn
        .query_row("PRAGMA page_count", [], |row| row.get(0))
        .map_err(persistence)?;
    u64::try_from(pages)
        .map_err(|_| integrity())?
        .checked_mul(page_size(conn)?)
        .ok_or_else(exhausted)
}
fn slot(size: u64) -> Result<u64, GateError> {
    (TERMINAL_METADATA_PAGES as u64)
        .checked_mul(size)
        .and_then(|bytes| bytes.checked_add(MAX_TERMINAL_ENTRY_BYTES as u64 * 2))
        .ok_or_else(exhausted)
}

/// Main-page safety cap, including transient uncommitted ordinary writes. This
/// does not replace future-growth escrow or promise journal/media allocation.
pub(super) fn physical_limit(conn: &Connection) -> Result<(), GateError> {
    let size = page_size(conn)?;
    if database_bytes(conn)? > LIMIT {
        return Err(exhausted());
    }
    let pages = LIMIT / size;
    let actual: u64 = conn
        .query_row(&format!("PRAGMA max_page_count={pages}"), [], |row| {
            crate::sql_u64(row, 0)
        })
        .map_err(persistence)?;
    if actual != pages {
        return Err(integrity());
    }
    Ok(())
}

#[derive(Clone, Copy)]
pub(super) struct Snapshot {
    bytes: u64,
    total: u64,
    ceiling: u64,
    debt_closed: bool,
    fault: u64,
}

fn remaining(conn: &Connection, maximum: u64) -> Result<u64, GateError> {
    let mut query = conn
        .prepare(
            "SELECT remaining FROM terminal_capacity_escrow WHERE state IN('active','terminal')",
        )
        .map_err(persistence)?;
    let mut rows = query.query([]).map_err(persistence)?;
    let mut total = 0u64;
    while let Some(row) = rows.next().map_err(persistence)? {
        let bytes = row.get_ref(0).map_err(persistence)?;
        let amount = number(bytes.as_blob().map_err(persistence)?)?;
        if amount > maximum {
            return Err(integrity());
        }
        total = total.checked_add(amount).ok_or_else(exhausted)?;
    }
    Ok(total)
}

pub(super) fn snapshot(conn: &Connection) -> Result<Snapshot, GateError> {
    let (version, stored_size, fault, ceiling, closed): (i64, i64, Vec<u8>, Vec<u8>, i64) = conn
        .query_row("SELECT version,page_size,CASE WHEN typeof(fault_remaining)='blob' AND length(fault_remaining)=8 THEN fault_remaining ELSE x'' END,CASE WHEN typeof(legacy_ceiling)='blob' AND length(legacy_ceiling)=8 THEN legacy_ceiling ELSE x'' END,debt_closed FROM terminal_capacity_state WHERE singleton=1", [], |row| Ok((row.get(0)?,row.get(1)?,row.get(2)?,row.get(3)?,row.get(4)?)))
        .optional().map_err(persistence)?.ok_or_else(integrity)?;
    let size = page_size(conn)?;
    let fault = number(&fault)?;
    let ceiling = number(&ceiling)?;
    if version != 1
        || u64::try_from(stored_size).ok() != Some(size)
        || fault > FAULT_METADATA_PAGES as u64 * size
        || ceiling < LIMIT
        || !matches!(closed, 0 | 1)
        || (closed == 0 && ceiling <= LIMIT)
    {
        return Err(integrity());
    }
    let bytes = database_bytes(conn)?;
    let total = bytes
        .checked_add(remaining(conn, slot(size)?)?)
        .and_then(|value| value.checked_add(fault))
        .ok_or_else(exhausted)?;
    Ok(Snapshot {
        bytes,
        total,
        ceiling,
        debt_closed: closed == 1,
        fault,
    })
}

/// Migration-only debt is not a raised quota. New admission and ordinary
/// writes always use LIMIT; only existing escrow settlement may preserve (never
/// increase) inherited conservative over-reservation. Once repaid it cannot
/// reopen, including after a process restart.
fn finish(
    tx: &Transaction<'_>,
    before: Snapshot,
    legacy_settlement: bool,
) -> Result<(), GateError> {
    let after = snapshot(tx)?;
    if after.bytes > LIMIT {
        return Err(exhausted());
    }
    if after.total <= LIMIT {
        if !after.debt_closed {
            tx.execute(
                "UPDATE terminal_capacity_state SET debt_closed=1 WHERE singleton=1",
                [],
            )
            .map_err(persistence)?;
            // Integer 0 and 1 use equally sized SQLite serial forms; nevertheless
            // verify actual page occupancy before committing the transition.
            if database_bytes(tx)? != after.bytes {
                return Err(integrity());
            }
        }
        return Ok(());
    }
    if legacy_settlement
        && !before.debt_closed
        && !after.debt_closed
        && before.total > LIMIT
        && after.ceiling == before.ceiling
        && after.total <= before.total
        && after.total <= after.ceiling
    {
        return Ok(());
    }
    Err(exhausted())
}

pub(super) fn ordinary_start(conn: &Connection) -> Result<Snapshot, GateError> {
    let before = snapshot(conn)?;
    if before.total > LIMIT {
        return Err(exhausted());
    }
    Ok(before)
}
pub(super) fn ordinary_finish(tx: &Transaction<'_>, before: Snapshot) -> Result<(), GateError> {
    finish(tx, before, false)
}

/// Capture already-accounted databases before any base-schema repair writes.
/// Only a genuinely old header with neither table may initialize new escrow.
pub(super) fn reopen_start(conn: &Connection) -> Result<Option<Snapshot>, GateError> {
    let version: i64 = conn
        .query_row("PRAGMA user_version", [], |row| row.get(0))
        .map_err(persistence)?;
    let tables: i64 = conn.query_row("SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN ('terminal_capacity_escrow','terminal_capacity_state')", [], |row| row.get(0)).map_err(persistence)?;
    match (version, tables) {
        (0, 0) => Ok(None),
        (1, 2) => snapshot(conn).map(Some),
        _ => Err(integrity()),
    }
}

pub(super) fn reopen_finish(
    tx: &Transaction<'_>,
    before: Option<Snapshot>,
) -> Result<(), GateError> {
    if let Some(before) = before {
        finish(tx, before, !before.debt_closed)?;
    }
    Ok(())
}

pub(super) fn initialize(tx: &Transaction<'_>) -> Result<(), GateError> {
    let version: i64 = tx
        .query_row("PRAGMA user_version", [], |row| row.get(0))
        .map_err(persistence)?;
    let tables: i64 = tx.query_row("SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN ('terminal_capacity_escrow','terminal_capacity_state')", [], |row| row.get(0)).map_err(persistence)?;
    if tables == 0 {
        // The header marker survives loss of both accounting tables. Never
        // interpret migrated evidence as permission to remint legacy escrow.
        if version != 0 {
            return Err(integrity());
        }
        tx.execute_batch("PRAGMA user_version=1;")
            .map_err(persistence)?;
        tx.execute_batch("CREATE TABLE terminal_capacity_escrow(grant_id TEXT PRIMARY KEY,remaining BLOB NOT NULL CHECK(typeof(remaining)='blob' AND length(remaining)=8),state TEXT NOT NULL CHECK(state IN('active','terminal','closed')),legacy INTEGER NOT NULL CHECK(legacy IN(0,1)),CHECK(state!='closed' OR remaining=x'0000000000000000')); CREATE INDEX terminal_capacity_live ON terminal_capacity_escrow(state); CREATE TABLE terminal_capacity_state(singleton INTEGER PRIMARY KEY CHECK(singleton=1),version INTEGER NOT NULL CHECK(version=1),page_size INTEGER NOT NULL,fault_remaining BLOB NOT NULL CHECK(typeof(fault_remaining)='blob' AND length(fault_remaining)=8),legacy_ceiling BLOB NOT NULL CHECK(typeof(legacy_ceiling)='blob' AND length(legacy_ceiling)=8),debt_closed INTEGER NOT NULL CHECK(debt_closed IN(0,1)));")
            .map_err(persistence)?;
        tx.execute_batch("CREATE TRIGGER terminal_capacity_no_delete BEFORE DELETE ON terminal_capacity_escrow BEGIN SELECT RAISE(ABORT,'capacity escrow is permanent'); END; CREATE TRIGGER terminal_capacity_no_rebind BEFORE UPDATE OF grant_id,legacy ON terminal_capacity_escrow BEGIN SELECT RAISE(ABORT,'capacity escrow identity is immutable'); END; CREATE TRIGGER terminal_capacity_no_refill BEFORE UPDATE OF remaining ON terminal_capacity_escrow WHEN NEW.remaining>OLD.remaining BEGIN SELECT RAISE(ABORT,'capacity escrow cannot refill'); END; CREATE TRIGGER terminal_capacity_no_reopen BEFORE UPDATE OF state ON terminal_capacity_escrow WHEN OLD.state='closed' OR OLD.state='terminal' AND NEW.state='active' BEGIN SELECT RAISE(ABORT,'capacity lifecycle cannot reopen'); END; CREATE TRIGGER terminal_capacity_state_no_delete BEFORE DELETE ON terminal_capacity_state BEGIN SELECT RAISE(ABORT,'capacity migration marker is permanent'); END; CREATE TRIGGER terminal_capacity_state_no_rebind BEFORE UPDATE OF singleton,version,page_size,legacy_ceiling ON terminal_capacity_state BEGIN SELECT RAISE(ABORT,'capacity migration identity is immutable'); END; CREATE TRIGGER terminal_capacity_fault_no_refill BEFORE UPDATE OF fault_remaining ON terminal_capacity_state WHEN NEW.fault_remaining>OLD.fault_remaining BEGIN SELECT RAISE(ABORT,'fault escrow cannot refill'); END; CREATE TRIGGER terminal_capacity_debt_no_reopen BEFORE UPDATE OF debt_closed ON terminal_capacity_state WHEN NEW.debt_closed<OLD.debt_closed BEGIN SELECT RAISE(ABORT,'migration debt cannot reopen'); END;")
            .map_err(persistence)?;
        let size = page_size(tx)?;
        let full = slot(size)?.to_be_bytes();
        tx.execute("INSERT INTO terminal_capacity_escrow(grant_id,remaining,state,legacy) SELECT id,?1,'active',1 FROM reservations WHERE state!='finished'", [full.as_slice()]).map_err(persistence)?;
        tx.execute("INSERT INTO terminal_capacity_escrow(grant_id,remaining,state,legacy) SELECT grant_id,?1,'terminal',1 FROM terminal_outbox WHERE delivered=0", [full.as_slice()]).map_err(persistence)?;
        let faulted: bool = tx
            .query_row("SELECT EXISTS(SELECT 1 FROM authority_fault)", [], |row| {
                row.get(0)
            })
            .map_err(persistence)?;
        let fault = if faulted {
            0
        } else {
            FAULT_METADATA_PAGES as u64 * size
        };
        // All schema/triggers/legacy escrow rows already occupy their real
        // pages. The one small fixed-width metadata row fits its empty root
        // page even at SQLite's minimum 512-byte page size.
        let bytes = database_bytes(tx)?;
        if bytes > LIMIT {
            return Err(exhausted());
        }
        let total = bytes
            .checked_add(remaining(tx, slot(size)?)?)
            .and_then(|value| value.checked_add(fault))
            .ok_or_else(exhausted)?;
        let ceiling = total.max(LIMIT);
        tx.execute("INSERT INTO terminal_capacity_state(singleton,version,page_size,fault_remaining,legacy_ceiling,debt_closed) VALUES(1,1,?1,?2,?3,?4)", params![i64::try_from(size).map_err(|_| integrity())?, fault.to_be_bytes().as_slice(), ceiling.to_be_bytes().as_slice(), i64::from(total<=LIMIT)]).map_err(persistence)?;
        if database_bytes(tx)? != bytes {
            return Err(integrity());
        }
    } else if tables != 2 || version != 1 {
        return Err(integrity());
    }
    let current = snapshot(tx)?;
    let malformed: bool = tx.query_row("SELECT EXISTS(SELECT 1 FROM terminal_capacity_escrow e WHERE typeof(remaining)!='blob' OR length(remaining)!=8 OR legacy NOT IN(0,1) OR state NOT IN('active','terminal','closed') OR state='closed' AND remaining!=x'0000000000000000' OR state='active' AND (NOT EXISTS(SELECT 1 FROM reservations r WHERE r.id=e.grant_id AND r.state!='finished') OR EXISTS(SELECT 1 FROM terminal_outbox t WHERE t.grant_id=e.grant_id)) OR state='terminal' AND NOT EXISTS(SELECT 1 FROM terminal_outbox t WHERE t.grant_id=e.grant_id AND t.delivered=0) OR state='closed' AND NOT EXISTS(SELECT 1 FROM terminal_outbox t WHERE t.grant_id=e.grant_id AND t.delivered=1)) OR EXISTS(SELECT 1 FROM reservations r WHERE r.state!='finished' AND NOT EXISTS(SELECT 1 FROM terminal_capacity_escrow e WHERE e.grant_id=r.id AND e.state='active')) OR EXISTS(SELECT 1 FROM terminal_outbox t WHERE t.delivered=0 AND NOT EXISTS(SELECT 1 FROM terminal_capacity_escrow e WHERE e.grant_id=t.grant_id AND e.state='terminal'))", [], |row| row.get(0)).map_err(persistence)?;
    if malformed
        || current.bytes > LIMIT
        || (!current.debt_closed && current.total > current.ceiling)
    {
        return Err(integrity());
    }
    // A missing singleton is an integrity error through snapshot(), never an
    // opportunity to remint legacy escrow on reopen.
    finish(tx, current, !current.debt_closed)
}

#[derive(Clone, Copy)]
pub(super) enum Next {
    Keep,
    Terminal,
    Closed,
}
pub(super) struct GrantCharge {
    before: Snapshot,
    id: String,
    remaining: u64,
    state: String,
    legacy: bool,
    admission: bool,
}
pub(super) fn begin_admission(tx: &Transaction<'_>, id: &str) -> Result<GrantCharge, GateError> {
    let before = ordinary_start(tx)?;
    let amount = slot(page_size(tx)?)?;
    if before.total.checked_add(amount).ok_or_else(exhausted)? > LIMIT {
        return Err(exhausted());
    }
    tx.execute("INSERT INTO terminal_capacity_escrow(grant_id,remaining,state,legacy) VALUES(?1,?2,'active',0)", params![id, amount.to_be_bytes().as_slice()]).map_err(persistence)?;
    Ok(GrantCharge {
        before,
        id: id.into(),
        remaining: amount,
        state: ACTIVE.into(),
        legacy: false,
        admission: true,
    })
}
pub(super) fn begin_grant(
    tx: &Transaction<'_>,
    id: &str,
    terminal: bool,
) -> Result<GrantCharge, GateError> {
    let before = snapshot(tx)?;
    let (raw, state, legacy): (Vec<u8>, String, bool) = tx
        .query_row(
            "SELECT CASE WHEN typeof(remaining)='blob' AND length(remaining)=8 THEN remaining ELSE x'' END,CASE WHEN state IN('active','terminal') THEN state ELSE '' END,legacy FROM terminal_capacity_escrow WHERE grant_id=?1",
            [id],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .optional()
        .map_err(persistence)?
        .ok_or_else(integrity)?;
    let amount = number(&raw)?;
    if state != if terminal { TERMINAL } else { ACTIVE } || amount > slot(page_size(tx)?)? {
        return Err(integrity());
    }
    Ok(GrantCharge {
        before,
        id: id.into(),
        remaining: amount,
        state,
        legacy,
        admission: false,
    })
}
pub(super) fn finish_grant(
    tx: &Transaction<'_>,
    charge: GrantCharge,
    next: Next,
) -> Result<(), GateError> {
    match next {
        Next::Keep => {}
        Next::Terminal if charge.state == ACTIVE => {
            tx.execute(
                "UPDATE terminal_capacity_escrow SET state='terminal' WHERE grant_id=?1",
                [&charge.id],
            )
            .map_err(persistence)?;
        }
        Next::Closed if charge.state == TERMINAL => {
            tx.execute("UPDATE terminal_capacity_escrow SET state='closed',remaining=x'0000000000000000' WHERE grant_id=?1", [&charge.id]).map_err(persistence)?;
        }
        _ => return Err(integrity()),
    }
    let after = database_bytes(tx)?;
    let growth = after.saturating_sub(charge.before.bytes);
    let unused = charge.remaining.checked_sub(growth).ok_or_else(exhausted)?;
    if !matches!(next, Next::Closed) {
        tx.execute(
            "UPDATE terminal_capacity_escrow SET remaining=?2 WHERE grant_id=?1",
            params![charge.id, unused.to_be_bytes().as_slice()],
        )
        .map_err(persistence)?;
        // Only an unindexed, fixed-width 8-byte BLOB changes. Never silently
        // waive accounting if SQLite unexpectedly allocates an extra page.
        if database_bytes(tx)? != after {
            return Err(integrity());
        }
    }
    finish(tx, charge.before, charge.legacy && !charge.admission)
}

pub(super) fn latch_fault(tx: &Connection, code: &[u8]) -> Result<(), GateError> {
    let before = snapshot(tx)?;
    tx.execute(
        "INSERT OR IGNORE INTO authority_fault(singleton,code) VALUES(1,?1)",
        [code],
    )
    .map_err(persistence)?;
    tx.execute(
        "UPDATE terminal_capacity_state SET fault_remaining=x'0000000000000000' WHERE singleton=1",
        [],
    )
    .map_err(persistence)?;
    let bytes = database_bytes(tx)?;
    if bytes.saturating_sub(before.bytes) > before.fault {
        return Err(exhausted());
    }
    // Callers hold an IMMEDIATE Transaction; its Connection deref is borrowed
    // here so nested quarantine handling never opens a second transaction.
    let after = snapshot(tx)?;
    if after.bytes > LIMIT
        || after.total > LIMIT
            && (before.debt_closed
                || after.debt_closed
                || after.total > before.total
                || after.total > before.ceiling)
    {
        return Err(exhausted());
    }
    if after.total <= LIMIT && !after.debt_closed {
        tx.execute(
            "UPDATE terminal_capacity_state SET debt_closed=1 WHERE singleton=1",
            [],
        )
        .map_err(persistence)?;
        if database_bytes(tx)? != bytes {
            return Err(integrity());
        }
    }
    Ok(())
}

#[cfg(test)]
#[path = "approvals_capacity_tests.rs"]
mod capacity_accounting_tests;
