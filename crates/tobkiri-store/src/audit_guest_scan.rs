//! Fixed full-history audit-chain scan and owned read projection. Both paths
//! keep one Deferred read snapshot from seq 1 through the verified tail.
//! Append verifies, checks quotas and inserts within ONE Immediate TX.

use super::audit_guest_owned::{OwnedAuditEvent, charge_work, digest_text};
use super::{
    GuestAuditDispatch, GuestAuditOutput, GuestAuditRead, GuestPrepared, decimal_value, integrity,
    map_alloc,
};
use crate::{AuditLog, StoreError};
use rusqlite::{Row, TransactionBehavior, types::ValueRef};
use std::sync::Arc;
use tobkiri_canon::{
    AllocationBudget, AllocationError, CoreValue, Digest, MAX_BYTES, MAX_SAFE_INTEGER,
    ids::ProfileId,
};

/// Descriptive verified head, never authority or physical-completion proof.
pub(crate) struct AuditScanHead {
    pub(crate) last_seq: u64,
    pub(crate) digest: Digest,
}

fn dispatch_budget(dispatch: &GuestAuditDispatch) -> Arc<dyn AllocationBudget> {
    match &dispatch.prepared {
        GuestPrepared::Append(prepared) => prepared.budget.clone(),
        GuestPrepared::Read(prepared) => prepared.budget.clone(),
    }
}

/// The original dispatch and this strong SAME-budget pin outlive every SQL
/// guard, also on error/unwind. Binding/Grant/terminal work stays outside SQL.
#[cfg(test)]
pub(crate) fn verify_history(
    dispatch: &GuestAuditDispatch,
    log: &AuditLog,
) -> Result<AuditScanHead, StoreError> {
    dispatch.check_binding(log)?;
    let budget = dispatch_budget(dispatch);
    let mut conn = log
        .connection
        .lock()
        .map_err(|_| StoreError::Conflict("audit lock poisoned".into()))?;
    let tx = conn.transaction_with_behavior(TransactionBehavior::Deferred)?;
    let (head, _) = scan_history(&tx, &log.profile, &budget, None)?;
    Ok(head)
}

/// Fixed Store read entry: only the original consumed Read selects its
/// cursor, limits and owner. No caller-selected budget, TX or row callback.
pub(super) fn read_history(
    dispatch: &GuestAuditDispatch,
    log: &AuditLog,
) -> Result<GuestAuditOutput, StoreError> {
    dispatch.check_binding(log)?;
    let read = match &dispatch.prepared {
        GuestPrepared::Read(read) => read,
        GuestPrepared::Append(_) => {
            return Err(StoreError::Denied("audit read dispatch mismatch".into()));
        }
    };
    if read.max > 1000 {
        return Err(StoreError::Quota);
    }
    let budget = dispatch_budget(dispatch);
    let mut conn = log
        .connection
        .lock()
        .map_err(|_| StoreError::Conflict("audit lock poisoned".into()))?;
    let tx = conn.transaction_with_behavior(TransactionBehavior::Deferred)?;
    let bound = usize::try_from(dispatch.limits.output_bytes.min(MAX_BYTES as u64))
        .map_err(|_| StoreError::Quota)?;
    let (_, output) = scan_history(&tx, &log.profile, &budget, Some((read, bound)))?;
    Ok(GuestAuditOutput::Rows(output.ok_or_else(integrity)?))
}

/// Fixed append entry over the SAME original consumed preparation. All
/// caller data and simultaneous body/writer buffers are funded before SQL;
/// the outer owner and strong budget pin outlive TX/statement/row destruction.
pub(super) fn append_history(
    dispatch: &GuestAuditDispatch,
    log: &AuditLog,
) -> Result<GuestAuditOutput, StoreError> {
    dispatch.check_binding(log)?;
    let append = match &dispatch.prepared {
        GuestPrepared::Append(append) => append,
        GuestPrepared::Read(_) => {
            return Err(StoreError::Denied("audit append dispatch mismatch".into()));
        }
    };
    let budget = dispatch_budget(dispatch);
    let event = OwnedAuditEvent::for_append(&append.cx, &append.kind, &append.payload, &budget)?;
    event.check_append_context(&append.cx)?;
    let body = event.event.encode_guest(&budget).map_err(map_alloc)?;
    if body.len() > 64 * 1024 {
        return Err(StoreError::Quota);
    }
    let writer = event.writer_key()?;
    let writer_len = u64::try_from(writer.len()).map_err(|_| StoreError::Quota)?;
    charge_work(
        &budget,
        1,
        writer_len.checked_add(1).ok_or(StoreError::Quota)?,
    )?;
    let writer_text = writer.as_str().map_err(map_alloc)?;
    let minute = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|_| StoreError::Invalid("system clock".into()))?
        .as_secs()
        / 60;
    let minute = i64::try_from(minute).map_err(|_| StoreError::Quota)?;
    let mut conn = log
        .connection
        .lock()
        .map_err(|_| StoreError::Conflict("audit lock poisoned".into()))?;
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let (head, _) = scan_history(&tx, &log.profile, &budget, None)?;
    // Stack aggregates over borrowed columns avoid SUM's type coercion and
    // allocating FromSql conversion errors. This is the SAME Immediate TX.
    let (count, bytes, total) = quota_totals(&tx, writer_text, minute, &budget)?;
    let size = u64::try_from(body.len()).map_err(|_| StoreError::Quota)?;
    if count >= log.quota.records_per_minute
        || bytes.checked_add(size).ok_or(StoreError::Quota)? > log.quota.bytes_per_minute
        || total.checked_add(size).ok_or(StoreError::Quota)? > log.quota.total_bytes
    {
        return Err(StoreError::Quota);
    }
    let seq = head
        .last_seq
        .checked_add(1)
        .filter(|seq| *seq <= MAX_SAFE_INTEGER)
        .ok_or(StoreError::Quota)?;
    let seq_sql = i64::try_from(seq).map_err(|_| StoreError::Quota)?;
    let size_sql = i64::try_from(size).map_err(|_| StoreError::Quota)?;
    let digest = event.append_digest(seq, &head.digest, &budget)?;
    // All owner-dependent fallible work is BEFORE INSERT. Afterwards only
    // native SQL/commit can fail, preserving execute's conservative no-replay.
    charge_work(&budget, 8, 32)?;
    tx.execute(
        "INSERT INTO audit(seq,prev_digest,digest,body,writer,minute,size,event_id) VALUES(?1,?2,?3,?4,?5,?6,?7,NULL)",
        rusqlite::params![seq_sql, head.digest.as_str(), digest.as_str(), body.as_bytes(), writer_text, minute, size_sql],
    )?;
    tx.commit()?;
    Ok(GuestAuditOutput::Appended)
}

fn quota_totals(
    conn: &rusqlite::Connection,
    writer: &str,
    minute: i64,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<(u64, u64, u64), StoreError> {
    let mut statement = conn.prepare("SELECT writer,minute,size FROM audit")?;
    let mut rows = statement.query([])?;
    let (mut count, mut bytes, mut total) = (0u64, 0u64, 0u64);
    while let Some(row) = rows.next()? {
        charge_work(budget, 3, 16)?;
        let raw_writer = match row.get_ref(0)? {
            ValueRef::Text(bytes) => bytes,
            _ => return Err(integrity()),
        };
        let len = u64::try_from(raw_writer.len()).map_err(|_| StoreError::Quota)?;
        let target_len = u64::try_from(writer.len()).map_err(|_| StoreError::Quota)?;
        // One UTF8 pass and bounded byte equality, including unequal/empty
        // spellings, are funded before reading borrowed text.
        let work = len
            .checked_mul(2)
            .and_then(|work| work.checked_add(target_len))
            .and_then(|work| work.checked_add(8))
            .ok_or(StoreError::Quota)?;
        charge_work(budget, 1, work)?;
        let stored_writer = std::str::from_utf8(raw_writer).map_err(|_| integrity())?;
        let stored_minute = match row.get_ref(1)? {
            ValueRef::Integer(value) if value >= 0 => value,
            _ => return Err(integrity()),
        };
        let size = match row.get_ref(2)? {
            ValueRef::Integer(value) if value >= 0 => {
                u64::try_from(value).map_err(|_| integrity())?
            }
            _ => return Err(integrity()),
        };
        total = total.checked_add(size).ok_or_else(integrity)?;
        if stored_writer == writer && stored_minute == minute {
            count = count.checked_add(1).ok_or_else(integrity)?;
            bytes = bytes.checked_add(size).ok_or_else(integrity)?;
        }
    }
    Ok((count, bytes, total))
}

/// One row loop serves only the fixed entries above. A successful read must
/// verify ALL rows even after its output count fills. Genuine output denial
/// may stop early, preserving the original output/owner error class.
fn scan_history(
    conn: &rusqlite::Connection,
    profile: &ProfileId,
    budget: &Arc<dyn AllocationBudget>,
    read: Option<(&GuestAuditRead, usize)>,
) -> Result<(AuditScanHead, Option<CoreValue>), StoreError> {
    let mut statement =
        conn.prepare("SELECT seq,prev_digest,digest,body,event_id FROM audit ORDER BY seq")?;
    let mut rows = statement.query([])?;
    let mut prev = Digest::hash(&[]);
    let mut expected: u64 = 0;
    let mut list = None;
    while let Some(row) = rows.next()? {
        // Five borrowed-column reads and fixed sequence/selection steps,
        // including zero-byte/NULL operations, precede all dependent work.
        charge_work(budget, 5, 32)?;
        // Initialize only after SQLite stepped: the read snapshot already
        // exists before any row-dependent owner reservation or projection.
        if let Some((_, bound)) = read
            && list.is_none()
        {
            list = Some(CoreValue::array_builder_incremental(budget, bound).map_err(map_alloc)?);
        }
        let seq = match row.get_ref(0)? {
            ValueRef::Integer(value) if value > 0 => u64::try_from(value)
                .ok()
                .filter(|seq| *seq <= MAX_SAFE_INTEGER)
                .ok_or_else(integrity)?,
            _ => return Err(integrity()),
        };
        expected = expected.checked_add(1).ok_or_else(integrity)?;
        if seq != expected {
            return Err(integrity());
        }
        let stored_prev = digest_column(row, 1, budget)?;
        let stored_digest = digest_column(row, 2, budget)?;
        let body = match row.get_ref(3)? {
            ValueRef::Blob(bytes) => bytes,
            _ => return Err(integrity()),
        };
        let event_id = match row.get_ref(4)? {
            ValueRef::Null => None,
            ValueRef::Text(bytes) => Some(digest_bytes(bytes, budget)?),
            _ => return Err(integrity()),
        };
        // Digest equality may inspect the entire inline 71-byte spelling.
        charge_work(budget, 1, 71)?;
        if stored_prev != prev {
            return Err(integrity());
        }
        let event = OwnedAuditEvent::parse(body, profile, budget)?;
        let computed = event.unsigned_digest(seq, &prev, event_id.as_ref(), budget)?;
        charge_work(budget, 1, 71)?;
        if computed != stored_digest {
            return Err(integrity());
        }
        if let (Some((read, _)), Some(list)) = (read, list.as_mut())
            && seq > read.after
            && list.len() < read.max as usize
        {
            let text = project_row(seq, &prev, event_id.as_ref(), &event, &computed, budget)?;
            list.push(text).map_err(map_alloc)?;
        }
        // Carry only the fixed inline tip; unselected graph/typed IDs die at
        // the iteration boundary before SQLite advances its borrowed row.
        charge_work(budget, 1, 71)?;
        prev = computed;
    }
    let output = match read {
        Some((_, bound)) => {
            let list = match list {
                Some(list) => list,
                None => CoreValue::array_builder_incremental(budget, bound).map_err(map_alloc)?,
            };
            Some(list.finish().map_err(map_alloc)?)
        }
        None => None,
    };
    Ok((
        AuditScanHead {
            last_seq: expected,
            digest: prev,
        },
        output,
    ))
}

fn digest_column(
    row: &Row<'_>,
    index: usize,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Digest, StoreError> {
    match row.get_ref(index)? {
        ValueRef::Text(bytes) => digest_bytes(bytes, budget),
        _ => Err(integrity()),
    }
}

fn digest_bytes(bytes: &[u8], budget: &Arc<dyn AllocationBudget>) -> Result<Digest, StoreError> {
    // Borrowed SQL text must pay its UTF8 walk BEFORE validation. The owned
    // digest helper separately funds spelling/parser/inline-copy passes.
    let len = u64::try_from(bytes.len()).map_err(|_| StoreError::Quota)?;
    charge_work(budget, 1, len.checked_add(1).ok_or(StoreError::Quota)?)?;
    let text = std::str::from_utf8(bytes).map_err(|_| integrity())?;
    digest_text(text, budget)
}

fn project_row(
    seq: u64,
    prev: &Digest,
    event_id: Option<&Digest>,
    event: &OwnedAuditEvent,
    digest: &Digest,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreValue, StoreError> {
    // decimal_value funds its stack formatting/UTF8 walk before CoreValue
    // pays its own string storage; the other APIs fund their own graphs.
    let fields: [Result<(&'static str, CoreValue), AllocationError>; 5] = [
        decimal_value(seq, budget).map(|value| ("seq", value)),
        match event_id {
            Some(id) => CoreValue::string(id.as_str(), budget),
            None => CoreValue::null(budget),
        }
        .map(|value| ("event_id", value)),
        CoreValue::string(prev.as_str(), budget).map(|value| ("prev_digest", value)),
        Ok(("event", event.event.share())),
        CoreValue::string(digest.as_str(), budget).map(|value| ("digest", value)),
    ];
    let projection = CoreValue::object_shared(fields.into_iter(), budget).map_err(map_alloc)?;
    let encoded = projection.encode_guest(budget).map_err(map_alloc)?;
    CoreValue::string_from_utf8(&encoded, budget).map_err(map_alloc)
}
