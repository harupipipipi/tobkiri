use crate::{DataRoot, StoreError, canonical, decode};
use rusqlite::{OptionalExtension, params};
use serde::{Deserialize, Serialize};
use std::{
    sync::Mutex,
    time::{SystemTime, UNIX_EPOCH},
};
use tobkiri_canon::{Digest, ids::ProfileId};
use tobkiri_gate::{AuditEvent, AuditSink, CallCx, EffectRequest, GateError, Grant};

#[path = "audit_image.rs"]
mod image;
use image::VerifiedImage;

#[derive(Clone, Debug)]
pub struct AuditQuota {
    pub records_per_minute: u64,
    pub bytes_per_minute: u64,
    pub total_bytes: u64,
}
impl Default for AuditQuota {
    fn default() -> Self {
        Self {
            records_per_minute: 600,
            bytes_per_minute: 1024 * 1024,
            total_bytes: 100 * 1024 * 1024,
        }
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AuditRow {
    pub seq: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub event_id: Option<Digest>,
    pub prev_digest: Digest,
    pub event: AuditEvent,
    pub digest: Digest,
}
#[derive(Serialize)]
struct UnsignedRow<'a> {
    seq: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    event_id: Option<&'a Digest>,
    prev_digest: &'a Digest,
    event: &'a AuditEvent,
}
impl AuditRow {
    fn digest(
        seq: u64,
        prev: &Digest,
        event: &AuditEvent,
        event_id: Option<&Digest>,
    ) -> Result<Digest, StoreError> {
        Ok(Digest::hash(&canonical(&UnsignedRow {
            seq,
            event_id,
            prev_digest: prev,
            event,
        })?))
    }
}

/// Append-only Profile audit chain. Guest reads require their own dispatch grant.
pub struct AuditLog {
    profile: ProfileId,
    connection: Mutex<AuditConnection>,
    _directory: crate::vfs::DatabaseGuard,
    quota: AuditQuota,
    #[cfg(test)]
    decoded_rows: std::sync::atomic::AtomicU64,
}
struct AuditConnection {
    sql: rusqlite::Connection,
    verified: Option<VerifiedImage>,
    #[cfg(test)]
    lose_commit_ack: bool,
}
impl AuditLog {
    pub fn open(
        root: &DataRoot,
        profile: &ProfileId,
        quota: AuditQuota,
    ) -> Result<Self, StoreError> {
        let (conn, directory) =
            root.database(&DataRoot::profile_relative(profile, "audit.sqlite")?)?;
        conn.execute_batch("CREATE TABLE IF NOT EXISTS audit(seq INTEGER PRIMARY KEY,prev_digest TEXT NOT NULL,digest TEXT NOT NULL,body BLOB NOT NULL,writer TEXT NOT NULL,minute INTEGER NOT NULL,size INTEGER NOT NULL); CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT,'append-only audit'); END; CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT,'append-only audit'); END;")?;
        if conn.prepare("SELECT event_id FROM audit LIMIT 0").is_err() {
            conn.execute_batch("ALTER TABLE audit ADD COLUMN event_id TEXT;")?;
        }
        conn.execute_batch("CREATE UNIQUE INDEX IF NOT EXISTS audit_event_identity ON audit(event_id) WHERE event_id IS NOT NULL;")?;
        let log = Self {
            profile: profile.clone(),
            connection: Mutex::new(AuditConnection {
                sql: conn,
                verified: None,
                #[cfg(test)]
                lose_commit_ack: false,
            }),
            _directory: directory,
            quota,
            #[cfg(test)]
            decoded_rows: std::sync::atomic::AtomicU64::new(0),
        };
        log.verify()?;
        Ok(log)
    }
    pub fn verify(&self) -> Result<(), StoreError> {
        let mut connection = self
            .connection
            .lock()
            .map_err(|_| StoreError::Conflict("audit lock poisoned".into()))?;
        // Explicit verification always performs the complete semantic check.
        connection.verified = None;
        self.verify_connection(&connection.sql)
    }
    fn verify_connection(&self, conn: &rusqlite::Connection) -> Result<(), StoreError> {
        let mut statement =
            conn.prepare("SELECT seq,prev_digest,digest,body,event_id FROM audit ORDER BY seq")?;
        let mut rows = statement.query([])?;
        let mut prev = Digest::hash(&[]);
        let mut sequence = 0;
        while let Some(row) = rows.next()? {
            let seq = crate::sql_u64(row, 0)
                .map_err(|_| StoreError::Integrity(tobkiri_gate::IntegrityFaultCode::AuditChain))?;
            let stored_prev: String = row.get(1)?;
            let stored_digest: String = row.get(2)?;
            #[cfg(test)]
            self.decoded_rows
                .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            let event: AuditEvent = decode(&row.get::<_, Vec<u8>>(3)?)
                .map_err(|_| StoreError::Integrity(tobkiri_gate::IntegrityFaultCode::AuditChain))?;
            sequence += 1;
            if seq != sequence || stored_prev != prev.as_str() || event.profile != self.profile {
                return Err(StoreError::Integrity(
                    tobkiri_gate::IntegrityFaultCode::AuditChain,
                ));
            }
            let event_id = row
                .get::<_, Option<String>>(4)?
                .map(|id| {
                    Digest::parse(&id).map_err(|_| {
                        StoreError::Integrity(tobkiri_gate::IntegrityFaultCode::AuditChain)
                    })
                })
                .transpose()?;
            let digest = AuditRow::digest(seq, &prev, &event, event_id.as_ref())?;
            if digest.as_str() != stored_digest {
                return Err(StoreError::Integrity(
                    tobkiri_gate::IntegrityFaultCode::AuditChain,
                ));
            }
            prev = digest;
        }
        Ok(())
    }
    fn append_event(&self, event: AuditEvent) -> Result<(), StoreError> {
        self.append_identified(event, None)
    }
    fn append_identified(
        &self,
        event: AuditEvent,
        event_id: Option<&Digest>,
    ) -> Result<(), StoreError> {
        if event.profile != self.profile {
            return Err(StoreError::Denied("audit Profile mismatch".into()));
        }
        let body = canonical(&event)?;
        if body.len() > 64 * 1024 {
            return Err(StoreError::Quota);
        }
        let minute = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| StoreError::Invalid("system clock".into()))?
            .as_secs()
            / 60;
        let writer = match event.principal.pack() {
            Some((pack, _)) => format!("pack:{pack}"),
            None => match &event.principal {
                tobkiri_gate::Principal::Input { source } => format!("input:{source}"),
                _ => "user".into(),
            },
        };
        let mut connection = self
            .connection
            .lock()
            .map_err(|_| StoreError::Conflict("audit lock poisoned".into()))?;
        // Taking first makes every transaction/scan/insert/commit failure
        // invalidate reuse. No cache survives an unknown acknowledgement.
        let previous = connection.verified.take();
        let tx = connection
            .sql
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        // Existing outbox decisions may progress through a faulted admission
        // domain only while this independent audit chain is still verified.
        let mut image = VerifiedImage::scan(&tx)?;
        if previous
            .as_ref()
            .is_none_or(|previous| !image.matches(previous))
        {
            self.verify_connection(&tx)?;
        }
        if let Some(event_id) = event_id {
            let existing: Option<Vec<u8>> = tx
                .query_row(
                    "SELECT body FROM audit WHERE event_id=?1",
                    [event_id.as_str()],
                    |r| r.get(0),
                )
                .optional()?;
            if let Some(existing) = existing {
                if existing != body {
                    return Err(StoreError::Integrity(
                        tobkiri_gate::IntegrityFaultCode::AuditIdentityConflict,
                    ));
                }
                tx.commit()?;
                #[cfg(test)]
                if std::mem::take(&mut connection.lose_commit_ack) {
                    return Err(StoreError::Invalid("fixture lost audit commit ack".into()));
                }
                connection.verified = Some(image);
                return Ok(());
            }
        }
        let (count, bytes): (u64, u64) = tx.query_row(
            "SELECT COUNT(*),COALESCE(SUM(size),0) FROM audit WHERE writer=?1 AND minute=?2",
            params![writer, minute as i64],
            |r| Ok((crate::sql_u64(r, 0)?, crate::sql_u64(r, 1)?)),
        )?;
        let total: u64 = tx.query_row("SELECT COALESCE(SUM(size),0) FROM audit", [], |r| {
            crate::sql_u64(r, 0)
        })?;
        if count >= self.quota.records_per_minute
            || bytes.saturating_add(body.len() as u64) > self.quota.bytes_per_minute
            || total.saturating_add(body.len() as u64) > self.quota.total_bytes
        {
            return Err(StoreError::Quota);
        }
        let (seq, prev) = match &image.last {
            Some((n, digest)) => (n + 1, digest.clone()),
            None => (1, Digest::hash(&[])),
        };
        let digest = AuditRow::digest(seq, &prev, &event, event_id)?;
        use rusqlite::types::ValueRef;
        image.append_row([
            ValueRef::Integer(seq as i64),
            ValueRef::Text(prev.as_str().as_bytes()),
            ValueRef::Text(digest.as_str().as_bytes()),
            ValueRef::Blob(&body),
            ValueRef::Text(writer.as_bytes()),
            ValueRef::Integer(minute as i64),
            ValueRef::Integer(body.len() as i64),
            event_id.map_or(ValueRef::Null, |id| ValueRef::Text(id.as_str().as_bytes())),
        ])?;
        image.last = Some((seq, digest.clone()));
        tx.execute("INSERT INTO audit(seq,prev_digest,digest,body,writer,minute,size,event_id) VALUES(?1,?2,?3,?4,?5,?6,?7,?8)",params![seq as i64,prev.as_str(),digest.as_str(),body,writer,minute as i64,body.len()as i64,event_id.map(Digest::as_str)])?;
        tx.commit()?;
        #[cfg(test)]
        if std::mem::take(&mut connection.lose_commit_ack) {
            return Err(StoreError::Invalid("fixture lost audit commit ack".into()));
        }
        // No fallible operation follows confirmed commit before publishing.
        connection.verified = Some(image);
        Ok(())
    }
    pub fn read_digest(after: u64, max: u32) -> Result<Digest, StoreError> {
        Ok(Digest::hash(&canonical(&(after, max))?))
    }
    pub fn read(
        &self,
        cx: &CallCx,
        after: u64,
        max: u32,
        grant: Grant,
    ) -> Result<Vec<AuditRow>, StoreError> {
        if *cx.profile() != self.profile {
            return Err(StoreError::Denied("audit Profile mismatch".into()));
        }
        if max > 1000 {
            return Err(StoreError::Quota);
        }
        grant.consume(
            cx,
            &EffectRequest::AuditRead,
            &Self::read_digest(after, max)?,
        )?;
        let conn = self
            .connection
            .lock()
            .map_err(|_| StoreError::Conflict("audit lock poisoned".into()))?;
        let mut statement = conn.sql.prepare(
            "SELECT seq,prev_digest,digest,body,event_id FROM audit WHERE seq>?1 ORDER BY seq LIMIT ?2",
        )?;
        statement
            .query_map(params![after.min(i64::MAX as u64) as i64, max], |r| {
                Ok((
                    crate::sql_u64(r, 0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, Vec<u8>>(3)?,
                    r.get::<_, Option<String>>(4)?,
                ))
            })?
            .map(|row| {
                let (seq, prev, digest, body, event_id) = row?;
                Ok(AuditRow {
                    seq,
                    event_id: event_id
                        .map(|id| Digest::parse(&id).map_err(|e| StoreError::Canon(e.to_string())))
                        .transpose()?,
                    prev_digest: Digest::parse(&prev)
                        .map_err(|e| StoreError::Canon(e.to_string()))?,
                    digest: Digest::parse(&digest).map_err(|e| StoreError::Canon(e.to_string()))?,
                    event: decode(&body)?,
                })
            })
            .collect()
    }
    pub fn append_guest(
        &self,
        cx: &CallCx,
        kind: String,
        payload: tobkiri_canon::CanonValue,
        grant: Grant,
    ) -> Result<(), StoreError> {
        let args = Digest::hash(&canonical(&(&kind, &payload))?);
        grant.consume(cx, &EffectRequest::AuditAppend, &args)?;
        self.append_event(AuditEvent {
            profile: cx.profile().clone(),
            principal: cx.principal().clone(),
            request: cx.request().clone(),
            kind,
            payload,
        })
    }
}
impl AuditSink for AuditLog {
    fn append_idempotent(&self, event_id: &Digest, event: AuditEvent) -> Result<(), GateError> {
        self.append_identified(event, Some(event_id))
            .map_err(|e| match e {
                StoreError::Integrity(code) => GateError::Integrity(code),
                StoreError::Quota => GateError::Limit("audit quota".into()),
                other => GateError::Persistence(other.to_string()),
            })
    }
    fn append(&self, event: AuditEvent) -> Result<(), GateError> {
        self.append_event(event).map_err(|e| match e {
            StoreError::Integrity(code) => GateError::Integrity(code),
            StoreError::Quota => GateError::Limit("audit quota".into()),
            _ => GateError::Persistence(e.to_string()),
        })
    }
}

#[cfg(test)]
#[path = "audit_image_tests.rs"]
mod image_tests;
