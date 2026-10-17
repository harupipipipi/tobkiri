use crate::{DataRoot, StoreError, canonical, decode};
use rusqlite::{OptionalExtension, params};
use std::sync::Mutex;
use tobkiri_canon::{
    Digest,
    ids::{PackHash, PackId, ProfileId},
};
use tobkiri_gate::{
    AdmissionStatus, ApprovalStore, AuditEvent, CleanupAuthoritySnapshot, GateError, GrantId,
    IntegrityFaultCode, Outcome, PermissionEffect, PermissionSet, PublicWriteApproval, Reservation,
    ReservationRequest, TerminalDecision, TerminalDelivery, TerminalDisposition,
};

const MAX_TERMINAL_BYTES: i64 = 100 * 1024 * 1024;
const MAX_TERMINAL_ENTRY_BYTES: i64 = 4096;
const MAX_UNDELIVERED_AND_RESERVED: i64 = 4096;
// Each entry reserves full body, reservation/receipt/nonce rows and bounded
// B-tree/index/ack metadata. Accounting is deliberately conservative; it is not
// a promise that the filesystem cannot fail after this logical reservation.
const TERMINAL_METADATA_PAGES: i64 = 64;
const FAULT_METADATA_PAGES: i64 = 16;

/// Profile-bound approvals and transactional dispatch reservations; never exported.
pub struct Approvals {
    profile: ProfileId,
    connection: Mutex<rusqlite::Connection>,
    _directory: crate::vfs::DatabaseGuard,
}
impl Approvals {
    pub fn open(root: &DataRoot, profile: &ProfileId) -> Result<Self, StoreError> {
        Self::open_database(
            root,
            profile,
            &DataRoot::profile_relative(profile, "approvals.sqlite")?,
        )
    }
    #[cfg(any(unix, windows))]
    pub(crate) fn open_pinned(root: &DataRoot, profile: &ProfileId) -> Result<Self, StoreError> {
        Self::open_database(root, profile, std::path::Path::new("approvals.sqlite"))
    }
    fn open_database(
        root: &DataRoot,
        profile: &ProfileId,
        path: &std::path::Path,
    ) -> Result<Self, StoreError> {
        let (mut connection, directory) = root.database(path)?;
        let initialization =
            connection.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        initialization.execute_batch("CREATE TABLE IF NOT EXISTS security_epoch(singleton INTEGER PRIMARY KEY CHECK(singleton=1),epoch INTEGER NOT NULL); INSERT OR IGNORE INTO security_epoch(singleton,epoch) VALUES(1,0); CREATE TABLE IF NOT EXISTS permissions(pack TEXT NOT NULL, hash TEXT NOT NULL, body BLOB NOT NULL, PRIMARY KEY(pack,hash)); CREATE TABLE IF NOT EXISTS reservations(id TEXT PRIMARY KEY, body BLOB NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','dispatched','consumed','finished')), outcome BLOB,expires INTEGER NOT NULL DEFAULT 0); CREATE TABLE IF NOT EXISTS public_writers(writer TEXT NOT NULL, owner TEXT NOT NULL, store TEXT NOT NULL, PRIMARY KEY(writer,owner,store)); CREATE TABLE IF NOT EXISTS paired_devices(device TEXT PRIMARY KEY,key BLOB NOT NULL); CREATE TABLE IF NOT EXISTS attestation_nonces(device TEXT NOT NULL,nonce TEXT NOT NULL,expires INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(device,nonce));")?;
        if initialization
            .prepare("SELECT expires FROM reservations LIMIT 0")
            .is_err()
        {
            initialization.execute_batch(
                "ALTER TABLE reservations ADD COLUMN expires INTEGER NOT NULL DEFAULT 0;",
            )?;
        }
        if initialization
            .prepare("SELECT expires FROM attestation_nonces LIMIT 0")
            .is_err()
        {
            initialization.execute_batch(
                "ALTER TABLE attestation_nonces ADD COLUMN expires INTEGER NOT NULL DEFAULT 0;",
            )?;
        }
        initialization.execute_batch("CREATE INDEX IF NOT EXISTS live_reservations ON reservations(state,expires); CREATE INDEX IF NOT EXISTS nonce_expiry ON attestation_nonces(expires);")?;
        initialization.execute_batch("CREATE TABLE IF NOT EXISTS terminal_outbox(grant_id TEXT PRIMARY KEY,event_id TEXT NOT NULL UNIQUE,body BLOB NOT NULL,delivered INTEGER NOT NULL DEFAULT 0 CHECK(delivered IN(0,1)),size INTEGER NOT NULL); CREATE INDEX IF NOT EXISTS pending_terminal_delivery ON terminal_outbox(delivered);")?;
        initialization.execute_batch("CREATE TRIGGER IF NOT EXISTS terminal_no_delete BEFORE DELETE ON terminal_outbox BEGIN SELECT RAISE(ABORT,'immutable terminal decision'); END; CREATE TRIGGER IF NOT EXISTS terminal_no_rewrite BEFORE UPDATE OF grant_id,event_id,body,size ON terminal_outbox BEGIN SELECT RAISE(ABORT,'immutable terminal decision'); END; CREATE TABLE IF NOT EXISTS user_approvals(profile_hash TEXT NOT NULL,revision TEXT NOT NULL,epoch INTEGER NOT NULL,body BLOB NOT NULL,PRIMARY KEY(profile_hash,revision,epoch));")?;
        if initialization
            .prepare("SELECT quarantined FROM terminal_outbox LIMIT 0")
            .is_err()
        {
            initialization.execute_batch("ALTER TABLE terminal_outbox ADD COLUMN quarantined INTEGER NOT NULL DEFAULT 0 CHECK(quarantined IN(0,1));")?;
        }
        initialization.execute_batch("CREATE TABLE IF NOT EXISTS reservation_quarantine(id TEXT PRIMARY KEY,code BLOB NOT NULL CHECK(length(code)<=128)); CREATE TRIGGER IF NOT EXISTS reservation_quarantine_no_delete BEFORE DELETE ON reservation_quarantine BEGIN SELECT RAISE(ABORT,'quarantined evidence is permanent'); END; CREATE TRIGGER IF NOT EXISTS reservation_quarantine_no_update BEFORE UPDATE ON reservation_quarantine BEGIN SELECT RAISE(ABORT,'quarantined evidence is immutable'); END;")?;
        initialization.execute_batch("CREATE TABLE IF NOT EXISTS permission_effects(grant_id TEXT PRIMARY KEY,digest TEXT NOT NULL); CREATE TRIGGER IF NOT EXISTS permission_effect_no_delete BEFORE DELETE ON permission_effects BEGIN SELECT RAISE(ABORT,'permission effect is permanent'); END; CREATE TRIGGER IF NOT EXISTS permission_effect_no_update BEFORE UPDATE ON permission_effects BEGIN SELECT RAISE(ABORT,'permission effect is immutable'); END;")?;
        initialization.execute_batch("CREATE TABLE IF NOT EXISTS authority_fault(singleton INTEGER PRIMARY KEY CHECK(singleton=1),code BLOB NOT NULL CHECK(length(code)<=128)); CREATE TRIGGER IF NOT EXISTS authority_fault_no_delete BEFORE DELETE ON authority_fault BEGIN SELECT RAISE(ABORT,'authority fault requires verified repair'); END; CREATE TRIGGER IF NOT EXISTS authority_fault_no_update BEFORE UPDATE ON authority_fault BEGIN SELECT RAISE(ABORT,'immutable authority fault'); END;")?;
        initialization.execute_batch("CREATE TABLE IF NOT EXISTS authority_sequence(singleton INTEGER PRIMARY KEY CHECK(singleton=1),namespace TEXT NOT NULL,generation INTEGER NOT NULL CHECK(generation>=0),sequence INTEGER NOT NULL CHECK(sequence>=0)); CREATE TABLE IF NOT EXISTS reservation_identities(nonce TEXT PRIMARY KEY,grant_id TEXT NOT NULL UNIQUE,generation INTEGER NOT NULL,sequence INTEGER NOT NULL,UNIQUE(generation,sequence)); CREATE TRIGGER IF NOT EXISTS authority_sequence_no_delete BEFORE DELETE ON authority_sequence BEGIN SELECT RAISE(ABORT,'authority namespace is permanent'); END; CREATE TRIGGER IF NOT EXISTS authority_sequence_no_reset BEFORE UPDATE ON authority_sequence WHEN NEW.namespace!=OLD.namespace OR NEW.generation!=OLD.generation OR NEW.sequence<OLD.sequence BEGIN SELECT RAISE(ABORT,'authority sequence cannot reset'); END; CREATE TRIGGER IF NOT EXISTS reservation_identity_no_delete BEFORE DELETE ON reservation_identities BEGIN SELECT RAISE(ABORT,'reservation identity is permanent'); END; CREATE TRIGGER IF NOT EXISTS reservation_identity_no_update BEFORE UPDATE ON reservation_identities BEGIN SELECT RAISE(ABORT,'reservation identity is immutable'); END;")?;
        let present: bool = initialization.query_row(
            "SELECT EXISTS(SELECT 1 FROM authority_sequence WHERE singleton=1)",
            [],
            |row| row.get(0),
        )?;
        if !present {
            let identities: bool = initialization.query_row(
                "SELECT EXISTS(SELECT 1 FROM reservation_identities)",
                [],
                |row| row.get(0),
            )?;
            if identities {
                return Err(StoreError::Integrity(IntegrityFaultCode::AuthorityDatabase));
            }
            let mut nonce = [0u8; 32];
            getrandom::fill(&mut nonce).map_err(|_| {
                StoreError::Unavailable("authority namespace randomness unavailable".into())
            })?;
            initialization.execute("INSERT OR IGNORE INTO authority_sequence(singleton,namespace,generation,sequence) VALUES(1,?1,0,0)", [Digest::hash(&nonce).as_str()])?;
        }
        let (namespace, generation, sequence): (String, u64, u64) = initialization.query_row(
            "SELECT namespace,generation,sequence FROM authority_sequence WHERE singleton=1",
            [],
            |row| {
                Ok((
                    row.get(0)?,
                    crate::sql_u64(row, 1)?,
                    crate::sql_u64(row, 2)?,
                ))
            },
        )?;
        Digest::parse(&namespace)
            .map_err(|_| StoreError::Integrity(IntegrityFaultCode::AuthorityDatabase))?;
        let broken:bool=initialization.query_row("SELECT EXISTS(SELECT 1 FROM reservation_identities WHERE generation!=?1 OR sequence>?2 OR NOT EXISTS(SELECT 1 FROM reservations WHERE id=grant_id) AND NOT EXISTS(SELECT 1 FROM terminal_outbox WHERE terminal_outbox.grant_id=reservation_identities.grant_id))",params![generation as i64,sequence as i64],|row|row.get(0))?;
        if broken {
            return Err(StoreError::Integrity(IntegrityFaultCode::AuthorityDatabase));
        }
        initialization.commit()?;
        Ok(Self {
            profile: profile.clone(),
            connection: Mutex::new(connection),
            _directory: directory,
        })
    }
    fn local(&self, profile: &ProfileId) -> Result<(), GateError> {
        if *profile == self.profile {
            Ok(())
        } else {
            Err(GateError::Denied("approval store Profile mismatch".into()))
        }
    }
    /// Core user operation after explicit device pairing, never a guest import.
    pub fn pair_device(
        &self,
        profile: &ProfileId,
        device: &str,
        key: [u8; 32],
    ) -> Result<(), StoreError> {
        self.local(profile)?;
        crate::secure::name(device)?;
        let conn = self
            .connection
            .lock()
            .map_err(|_| StoreError::Conflict("approval lock poisoned".into()))?;
        conn.execute("INSERT INTO paired_devices(device,key) VALUES(?1,?2) ON CONFLICT(device) DO UPDATE SET key=excluded.key",params![device,key.as_slice()])?;
        Ok(())
    }
    pub fn pending(&self) -> Result<Vec<Reservation>, StoreError> {
        let conn = self
            .connection
            .lock()
            .map_err(|_| StoreError::Conflict("approval lock poisoned".into()))?;
        let mut statement = conn.prepare(
            "SELECT body FROM reservations WHERE state='pending' ORDER BY rowid LIMIT 1000",
        )?;
        statement
            .query_map([], |r| r.get::<_, Vec<u8>>(0))?
            .map(|r| decode(&r?))
            .collect()
    }
    pub fn reservation_state(&self, id: &GrantId) -> Result<Option<String>, StoreError> {
        let conn = self
            .connection
            .lock()
            .map_err(|_| StoreError::Conflict("approval lock poisoned".into()))?;
        Ok(conn
            .query_row(
                "SELECT state FROM reservations WHERE id=?1",
                [id.as_str()],
                |r| r.get(0),
            )
            .optional()?)
    }
    fn dispatch_transaction(
        &self,
        id: &GrantId,
        expected: Option<&Reservation>,
        public: Option<&PublicWriteApproval>,
    ) -> Result<(), GateError> {
        let mut conn = self.connection.lock().map_err(persistence)?;
        let tx = conn
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
            .map_err(persistence)?;
        deny_faulted(&tx)?;
        let body: Vec<u8> = tx
            .query_row(
                "SELECT body FROM reservations WHERE id=?1 AND state='pending'",
                [id.as_str()],
                |r| r.get(0),
            )
            .optional()
            .map_err(persistence)?
            .ok_or(GateError::Consumed)?;
        let reservation: Reservation = decode(&body).map_err(persistence)?;
        self.local(&reservation.profile)?;
        if let Some(expected) = expected
            && canonical(expected).map_err(persistence)? != body
        {
            return Err(GateError::Denied("reservation binding mismatch".into()));
        }
        let epoch = tx
            .query_row(
                "SELECT epoch FROM security_epoch WHERE singleton=1",
                [],
                |r| crate::sql_u64(r, 0),
            )
            .map_err(persistence)?;
        if epoch != reservation.epoch {
            return Err(GateError::StaleEpoch);
        }
        if reservation.expires_unix_ms <= now_ms().map_err(persistence)? {
            return Err(GateError::Deadline);
        }
        tx.execute(
            "UPDATE reservations SET state='dispatched' WHERE id=?1 AND state='pending'",
            [id.as_str()],
        )
        .map_err(persistence)?;
        if let Some(public) = public {
            self.local(&public.profile)?;
            if public.profile != reservation.profile
                || reservation
                    .principal
                    .pack()
                    .is_none_or(|(p, _)| p != &public.writer)
            {
                return Err(GateError::Denied("public writer binding mismatch".into()));
            }
            tx.execute(
                "INSERT OR IGNORE INTO public_writers(writer,owner,store) VALUES(?1,?2,?3)",
                params![public.writer.as_str(), public.owner.as_str(), public.store],
            )
            .map_err(persistence)?;
        }
        tx.commit().map_err(persistence)?;
        Ok(())
    }
    fn terminalize(
        tx: &rusqlite::Transaction<'_>,
        reservation: Reservation,
        outcome: Outcome,
        disposition: TerminalDisposition,
    ) -> Result<TerminalDecision, GateError> {
        if let Outcome::Failed { code } = &outcome {
            if code.len() > 128
                || !code
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
            {
                return Err(GateError::Invalid(
                    "terminal outcome requires a bounded machine code".into(),
                ));
            }
        }
        let decision = TerminalDecision::new(reservation, outcome, disposition)?;
        let body = canonical(&decision).map_err(persistence)?;
        if body.len() as i64 > MAX_TERMINAL_ENTRY_BYTES {
            return Err(GateError::Limit("terminal payload capacity".into()));
        }
        // Admission already reserved this entry's complete terminal slot.
        // Terminalization must remain possible after the admission fuse closes.
        let changed = tx.execute("UPDATE reservations SET state='finished',outcome=?2 WHERE id=?1 AND state IN('pending','dispatched','consumed')", params![decision.reservation.id.as_str(), canonical(&decision.outcome).map_err(persistence)?]).map_err(persistence)?;
        if changed != 1 {
            return Err(GateError::Consumed);
        }
        tx.execute(
            "INSERT INTO terminal_outbox(grant_id,event_id,body,size) VALUES(?1,?2,?3,?4)",
            params![
                decision.reservation.id.as_str(),
                decision.event_id.as_str(),
                &body,
                body.len() as i64
            ],
        )
        .map_err(persistence)?;
        Ok(decision)
    }
    fn insert_reservation(
        tx: &rusqlite::Transaction<'_>,
        r: &Reservation,
    ) -> Result<(), GateError> {
        let mut future = r.clone();
        future.original_expires_unix_ms = Some(
            future
                .original_expires_unix_ms
                .unwrap_or(future.expires_unix_ms),
        );
        let worst = TerminalDecision::new(
            future,
            Outcome::Failed {
                code: "x".repeat(128),
            },
            TerminalDisposition::ExpiredCleanupUnknown,
        )?;
        if canonical(&worst).map_err(persistence)?.len() as i64 > MAX_TERMINAL_ENTRY_BYTES {
            return Err(GateError::Limit(
                "future terminal payload cannot be retained".into(),
            ));
        }
        deny_faulted(tx)?;
        let (active, pending, retained): (i64,i64,i64) = tx.query_row("SELECT (SELECT COUNT(*) FROM reservations WHERE state!='finished'),COALESCE(SUM(CASE WHEN delivered=0 THEN 1 ELSE 0 END),0),COUNT(*) FROM terminal_outbox", [], |row|Ok((row.get(0)?,row.get(1)?,row.get(2)?))).map_err(persistence)?;
        let page_size: i64 = tx
            .query_row("PRAGMA page_size", [], |row| row.get(0))
            .map_err(persistence)?;
        if !(512..=65536).contains(&page_size) || !(page_size as u64).is_power_of_two() {
            return Err(GateError::Integrity(IntegrityFaultCode::AuthorityDatabase));
        }
        let slot = TERMINAL_METADATA_PAGES
            .checked_mul(page_size)
            .and_then(|n| n.checked_add(MAX_TERMINAL_ENTRY_BYTES * 2))
            .ok_or_else(|| GateError::Limit("terminal accounting range".into()))?;
        let capacity = retained
            .checked_add(active)
            .and_then(|n| n.checked_add(1))
            .and_then(|n| n.checked_mul(slot))
            .and_then(|n| n.checked_add(FAULT_METADATA_PAGES * page_size))
            .ok_or_else(|| GateError::Limit("terminal accounting range".into()))?;
        if active.saturating_add(pending) >= MAX_UNDELIVERED_AND_RESERVED
            || capacity > MAX_TERMINAL_BYTES
        {
            return Err(GateError::Limit("live reservation quota".into()));
        }
        let reused:bool = tx.query_row("SELECT EXISTS(SELECT 1 FROM terminal_outbox WHERE grant_id=?1) OR EXISTS(SELECT 1 FROM reservations WHERE id=?1)",[r.id.as_str()],|row|row.get(0)).map_err(persistence)?;
        if reused {
            return Err(GateError::Consumed);
        }
        tx.execute(
            "INSERT INTO reservations(id,body,state,expires) VALUES(?1,?2,'pending',?3)",
            params![
                r.id.as_str(),
                canonical(r).map_err(persistence)?,
                i64::try_from(r.expires_unix_ms).map_err(persistence)?
            ],
        )
        .map_err(persistence)?;
        Ok(())
    }
    fn checked_integrity<T>(&self, result: Result<T, GateError>) -> Result<T, GateError> {
        if let Err(GateError::Integrity(code)) = &result {
            self.latch_integrity(&self.profile, *code)?;
        }
        result
    }
    fn terminal(
        tx: &rusqlite::Transaction<'_>,
        profile: &ProfileId,
        id: &GrantId,
    ) -> Result<Option<TerminalDelivery>, GateError> {
        let row: Option<(String, Vec<u8>, bool)> = tx
            .query_row(
                "SELECT CASE WHEN length(event_id)<=128 THEN event_id ELSE '' END,CASE WHEN length(body)<=4096 THEN body ELSE x'' END,delivered FROM terminal_outbox WHERE grant_id=?1",
                [id.as_str()],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .optional()
            .map_err(persistence)?;
        row.map(|(event_id, body, acknowledged)| {
            Ok(TerminalDelivery {
                decision: validated_terminal(profile, id.as_str(), &event_id, &body)?,
                acknowledged,
            })
        })
        .transpose()
    }
}
/// New terminal records have one explicit versioned hash/event construction.
/// Legacy audit rows remain separate and are never rewritten by this validator.
fn validated_terminal(
    profile: &ProfileId,
    grant: &str,
    event_id: &str,
    body: &[u8],
) -> Result<TerminalDecision, GateError> {
    if body.len() > MAX_TERMINAL_ENTRY_BYTES as usize {
        return Err(GateError::Integrity(IntegrityFaultCode::TerminalRecord));
    }
    let decision: TerminalDecision =
        decode(body).map_err(|_| GateError::Integrity(IntegrityFaultCode::TerminalRecord))?;
    validate_reservation_identity(&decision.reservation)?;
    if decision.reservation.profile != *profile
        || decision.reservation.id.as_str() != grant
        || decision.event_id.as_str() != event_id
    {
        return Err(GateError::Integrity(IntegrityFaultCode::TerminalRecord));
    }
    let expected = TerminalDecision::new(
        decision.reservation.clone(),
        decision.outcome.clone(),
        decision.disposition.clone(),
    )?;
    if canonical(&expected).map_err(persistence)? != body {
        return Err(GateError::Integrity(IntegrityFaultCode::TerminalRecord));
    }
    Ok(decision)
}
fn validate_reservation_identity(r: &Reservation) -> Result<(), GateError> {
    if r.original_expires_unix_ms
        .is_some_and(|original| r.expires_unix_ms > original)
    {
        return Err(GateError::Integrity(IntegrityFaultCode::ReservationRecord));
    }
    if let Some(authority) = &r.authority {
        if authority.sequence == 0 || r.original_expires_unix_ms.is_none() {
            return Err(GateError::Integrity(IntegrityFaultCode::ReservationRecord));
        }
        let raw = canonical(&("tobkiri.grant/2", &r.profile, authority)).map_err(persistence)?;
        if r.id.as_str() != format!("grant_{}", Digest::hash(&raw).hex()) {
            return Err(GateError::Integrity(IntegrityFaultCode::ReservationRecord));
        }
    }
    Ok(())
}
fn validated_reservation(
    profile: &ProfileId,
    id: &str,
    body: &[u8],
) -> Result<Reservation, GateError> {
    if body.len() > MAX_TERMINAL_ENTRY_BYTES as usize {
        return Err(GateError::Integrity(IntegrityFaultCode::ReservationRecord));
    }
    let r: Reservation =
        decode(body).map_err(|_| GateError::Integrity(IntegrityFaultCode::ReservationRecord))?;
    validate_reservation_identity(&r)?;
    if r.profile != *profile || r.id.as_str() != id || canonical(&r).map_err(persistence)? != body {
        return Err(GateError::Integrity(IntegrityFaultCode::ReservationRecord));
    }
    Ok(r)
}
fn same_reservation_authority(a: &Reservation, b: &Reservation) -> Result<bool, GateError> {
    let mut a = a.clone();
    let mut b = b.clone();
    a.expires_unix_ms = a.original_expires_unix_ms.unwrap_or(a.expires_unix_ms);
    b.expires_unix_ms = b.original_expires_unix_ms.unwrap_or(b.expires_unix_ms);
    a.original_expires_unix_ms = None;
    b.original_expires_unix_ms = None;
    Ok(canonical(&a).map_err(persistence)? == canonical(&b).map_err(persistence)?)
}
fn cleanup_binding(current: &Reservation, expected: &Reservation) -> Result<(), GateError> {
    if current.original_expires_unix_ms.is_none()
        || current.original_expires_unix_ms != expected.original_expires_unix_ms
        || current.expires_unix_ms > expected.expires_unix_ms
        || !same_reservation_authority(current, expected)?
    {
        return Err(GateError::Denied("cleanup lease binding mismatch".into()));
    }
    Ok(())
}
/// Restore this private connection's previous busy policy on every exit. The
/// connection mutex prevents another operation from seeing the temporary zero.
struct CleanupBusyTimeout<'a> {
    connection: &'a rusqlite::Connection,
    previous: std::time::Duration,
    restored: bool,
}
impl CleanupBusyTimeout<'_> {
    fn restore(mut self) -> Result<(), GateError> {
        self.connection
            .busy_timeout(self.previous)
            .map_err(persistence)?;
        self.restored = true;
        Ok(())
    }
}
impl Drop for CleanupBusyTimeout<'_> {
    fn drop(&mut self) {
        if !self.restored {
            let _ = self.connection.busy_timeout(self.previous);
        }
    }
}
fn now_ms() -> Result<u64, StoreError> {
    u64::try_from(
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_err(|_| StoreError::Invalid("system clock".into()))?
            .as_millis(),
    )
    .map_err(|_| StoreError::Invalid("system clock range".into()))
}
fn persistence(error: impl std::fmt::Display) -> GateError {
    GateError::Persistence(error.to_string())
}
fn status(conn: &rusqlite::Connection) -> Result<AdmissionStatus, GateError> {
    let code: Option<Vec<u8>> = conn
        .query_row(
            "SELECT code FROM authority_fault WHERE singleton=1",
            [],
            |r| r.get(0),
        )
        .optional()
        .map_err(persistence)?;
    code.map_or(Ok(AdmissionStatus::Allowed), |bytes| {
        Ok(AdmissionStatus::IntegrityFault {
            code: decode(&bytes)
                .map_err(|_| GateError::Integrity(IntegrityFaultCode::AuthorityDatabase))?,
        })
    })
}
fn latch(conn: &rusqlite::Connection, code: IntegrityFaultCode) -> Result<(), GateError> {
    conn.execute(
        "INSERT OR IGNORE INTO authority_fault(singleton,code) VALUES(1,?1)",
        [canonical(&code).map_err(persistence)?],
    )
    .map_err(persistence)?;
    Ok(())
}
fn deny_faulted(conn: &rusqlite::Connection) -> Result<(), GateError> {
    match status(conn)? {
        AdmissionStatus::Allowed => Ok(()),
        AdmissionStatus::IntegrityFault { code } => Err(GateError::Integrity(code)),
    }
}
impl ApprovalStore for Approvals {
    fn admission_status(&self, profile: &ProfileId) -> Result<AdmissionStatus, GateError> {
        self.local(profile)?;
        let conn = self.connection.lock().map_err(persistence)?;
        status(&conn)
    }
    fn latch_integrity(
        &self,
        profile: &ProfileId,
        code: IntegrityFaultCode,
    ) -> Result<(), GateError> {
        self.local(profile)?;
        let conn = self.connection.lock().map_err(persistence)?;
        latch(&conn, code)
    }
    fn security_epoch(&self, profile: &ProfileId) -> Result<u64, GateError> {
        self.local(profile)?;
        let conn = self.connection.lock().map_err(persistence)?;
        conn.query_row(
            "SELECT epoch FROM security_epoch WHERE singleton=1",
            [],
            |r| crate::sql_u64(r, 0),
        )
        .map_err(persistence)
    }
    fn advance_security_epoch(&self, profile: &ProfileId) -> Result<u64, GateError> {
        self.local(profile)?;
        let mut conn = self.connection.lock().map_err(persistence)?;
        let tx = conn
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
            .map_err(persistence)?;
        let epoch: u64 = tx
            .query_row(
                "SELECT epoch FROM security_epoch WHERE singleton=1",
                [],
                |r| crate::sql_u64(r, 0),
            )
            .map_err(persistence)?;
        let next = epoch
            .checked_add(1)
            .filter(|v| *v <= i64::MAX as u64)
            .ok_or_else(|| GateError::Limit("epoch exhausted".into()))?;
        tx.execute(
            "UPDATE security_epoch SET epoch=?1 WHERE singleton=1",
            [next as i64],
        )
        .map_err(persistence)?;
        tx.commit().map_err(persistence)?;
        Ok(next)
    }
    fn permissions(
        &self,
        profile: &ProfileId,
        pack: &PackId,
        hash: &PackHash,
    ) -> Result<Option<PermissionSet>, GateError> {
        self.local(profile)?;
        let conn = self.connection.lock().map_err(persistence)?;
        let body: Option<Vec<u8>> = conn
            .query_row(
                "SELECT body FROM permissions WHERE pack=?1 AND hash=?2",
                params![pack.as_str(), hash.as_str()],
                |r| r.get(0),
            )
            .optional()
            .map_err(persistence)?;
        body.map(|b| decode(&b).map_err(persistence)).transpose()
    }
    fn set_permissions(
        &self,
        profile: &ProfileId,
        pack: &PackId,
        hash: &PackHash,
        p: &PermissionSet,
    ) -> Result<(), GateError> {
        self.local(profile)?;
        let body = canonical(p).map_err(persistence)?;
        let conn = self.connection.lock().map_err(persistence)?;
        conn.execute("INSERT INTO permissions(pack,hash,body) VALUES(?1,?2,?3) ON CONFLICT(pack,hash) DO UPDATE SET body=excluded.body",params![pack.as_str(),hash.as_str(),body]).map_err(persistence)?;
        Ok(())
    }
    fn user_permissions(
        &self,
        profile: &ProfileId,
        profile_hash: &Digest,
        revision: &Digest,
        epoch: u64,
    ) -> Result<Option<PermissionSet>, GateError> {
        self.local(profile)?;
        let conn = self.connection.lock().map_err(persistence)?;
        let body: Option<Vec<u8>> = conn.query_row("SELECT body FROM user_approvals WHERE profile_hash=?1 AND revision=?2 AND epoch=?3", params![profile_hash.as_str(),revision.as_str(),i64::try_from(epoch).map_err(persistence)?], |r|r.get(0)).optional().map_err(persistence)?;
        body.map(|bytes| decode(&bytes).map_err(persistence))
            .transpose()
    }
    fn set_user_permissions(
        &self,
        profile: &ProfileId,
        profile_hash: &Digest,
        revision: &Digest,
        epoch: u64,
        permissions: &PermissionSet,
    ) -> Result<(), GateError> {
        self.local(profile)?;
        let conn = self.connection.lock().map_err(persistence)?;
        conn.execute("INSERT INTO user_approvals(profile_hash,revision,epoch,body) VALUES(?1,?2,?3,?4) ON CONFLICT(profile_hash,revision,epoch) DO UPDATE SET body=excluded.body",params![profile_hash.as_str(),revision.as_str(),i64::try_from(epoch).map_err(persistence)?,canonical(permissions).map_err(persistence)?]).map_err(persistence)?;
        Ok(())
    }
    fn reserve(&self, r: &Reservation) -> Result<(), GateError> {
        self.local(&r.profile)?;
        // Legacy trusted fixtures use this API. Production Gate always uses
        // reserve_fresh, whose sequence and identity are minted atomically.
        if r.authority.is_some() {
            let conn = self.connection.lock().map_err(persistence)?;
            let reused:bool=conn.query_row("SELECT EXISTS(SELECT 1 FROM reservations WHERE id=?1) OR EXISTS(SELECT 1 FROM terminal_outbox WHERE grant_id=?1)",[r.id.as_str()],|row|row.get(0)).map_err(persistence)?;
            if reused {
                return Err(GateError::Consumed);
            }
            return Err(GateError::Denied(
                "new authority must use fresh reservation transaction".into(),
            ));
        }
        let result = (|| {
            let mut conn = self.connection.lock().map_err(persistence)?;
            let tx = conn
                .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
                .map_err(persistence)?;
            Self::insert_reservation(&tx, r)?;
            tx.commit().map_err(persistence)?;
            Ok(())
        })();
        self.checked_integrity(result)
    }
    fn reserve_fresh(&self, request: &ReservationRequest) -> Result<Reservation, GateError> {
        self.local(request.profile())?;
        let result = (|| {
            let mut conn = self.connection.lock().map_err(persistence)?;
            let tx = conn
                .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
                .map_err(persistence)?;
            deny_faulted(&tx)?;
            let existing: Option<String> = tx
                .query_row(
                    "SELECT grant_id FROM reservation_identities WHERE nonce=?1",
                    [request.nonce().as_str()],
                    |row| row.get(0),
                )
                .optional()
                .map_err(persistence)?;
            if let Some(id) = existing {
                let body: Option<Vec<u8>> = tx
                    .query_row("SELECT body FROM reservations WHERE id=?1", [&id], |row| {
                        row.get(0)
                    })
                    .optional()
                    .map_err(persistence)?;
                let mut reservation: Reservation = if let Some(body) = body {
                    decode(&body)
                        .map_err(|_| GateError::Integrity(IntegrityFaultCode::ReservationRecord))?
                } else {
                    let (event, body): (String, Vec<u8>) = tx
                        .query_row(
                            "SELECT event_id,body FROM terminal_outbox WHERE grant_id=?1",
                            [&id],
                            |row| Ok((row.get(0)?, row.get(1)?)),
                        )
                        .map_err(persistence)?;
                    validated_terminal(&self.profile, &id, &event, &body)?.reservation
                };
                let actual = reservation.clone();
                if reservation.id.as_str() != id {
                    return Err(GateError::Integrity(IntegrityFaultCode::ReservationRecord));
                }
                let authority = reservation
                    .authority
                    .as_ref()
                    .ok_or(GateError::Integrity(IntegrityFaultCode::ReservationRecord))?;
                let proposed = request.bind(
                    authority.namespace.clone(),
                    authority.generation,
                    authority.sequence,
                )?;
                reservation.expires_unix_ms = reservation
                    .original_expires_unix_ms
                    .ok_or(GateError::Integrity(IntegrityFaultCode::ReservationRecord))?;
                if canonical(&proposed).map_err(persistence)?
                    != canonical(&reservation).map_err(persistence)?
                {
                    return Err(GateError::Denied("fresh reservation retry differs".into()));
                }
                tx.commit().map_err(persistence)?;
                return Ok(actual);
            }
            let (namespace,generation,sequence):(String,u64,u64)=tx.query_row("SELECT namespace,generation,sequence FROM authority_sequence WHERE singleton=1",[],|row|Ok((row.get(0)?,crate::sql_u64(row,1)?,crate::sql_u64(row,2)?))).map_err(persistence)?;
            let sequence = sequence
                .checked_add(1)
                .filter(|value| *value <= i64::MAX as u64)
                .ok_or_else(|| GateError::Limit("reservation sequence exhausted".into()))?;
            let namespace = Digest::parse(&namespace)
                .map_err(|_| GateError::Integrity(IntegrityFaultCode::AuthorityDatabase))?;
            let reservation = request.bind(namespace, generation, sequence)?;
            Self::insert_reservation(&tx, &reservation)?;
            tx.execute("INSERT INTO reservation_identities(nonce,grant_id,generation,sequence) VALUES(?1,?2,?3,?4)",params![request.nonce().as_str(),reservation.id.as_str(),generation as i64,sequence as i64]).map_err(persistence)?;
            tx.execute(
                "UPDATE authority_sequence SET sequence=?1 WHERE singleton=1",
                [sequence as i64],
            )
            .map_err(persistence)?;
            tx.commit().map_err(persistence)?;
            Ok(reservation)
        })();
        self.checked_integrity(result)
    }
    fn dispatch(&self, id: &GrantId) -> Result<(), GateError> {
        self.dispatch_transaction(id, None, None)
    }
    fn dispatch_public_write(
        &self,
        r: &Reservation,
        p: &PublicWriteApproval,
    ) -> Result<(), GateError> {
        self.dispatch_transaction(&r.id, Some(r), Some(p))
    }
    fn abandon(&self, id: &GrantId) -> Result<Option<Reservation>, GateError> {
        Ok(self.abandon_terminal(id)?.map(|v| v.decision.reservation))
    }
    fn sweep_expired(&self, profile: &ProfileId, now: u64) -> Result<u64, GateError> {
        let changed = self.expire_terminal(profile, now)?.len() as u64;
        let conn = self.connection.lock().map_err(persistence)?;
        conn.execute(
            "DELETE FROM reservations WHERE state='finished' AND expires<?1",
            [now.saturating_sub(86400000).min(i64::MAX as u64) as i64],
        )
        .map_err(persistence)?;
        conn.execute(
            "DELETE FROM attestation_nonces WHERE expires<=?1",
            [now.min(i64::MAX as u64) as i64],
        )
        .map_err(persistence)?;
        Ok(changed)
    }
    fn consume(&self, r: &Reservation) -> Result<(), GateError> {
        self.consume_with_deadline(r, r.expires_unix_ms).map(|_| ())
    }
    fn consume_with_deadline(
        &self,
        r: &Reservation,
        deadline: u64,
    ) -> Result<Reservation, GateError> {
        self.local(&r.profile)?;
        validate_reservation_identity(r)?;
        let result = (|| {
            let mut conn = self.connection.lock().map_err(persistence)?;
            let tx = conn
                .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
                .map_err(persistence)?;
            deny_faulted(&tx)?;
            let epoch: u64 = tx
                .query_row(
                    "SELECT epoch FROM security_epoch WHERE singleton=1",
                    [],
                    |row| crate::sql_u64(row, 0),
                )
                .map_err(persistence)?;
            if epoch != r.epoch {
                return Err(GateError::StaleEpoch);
            }
            let now = now_ms().map_err(persistence)?;
            let mut consumed = r.clone();
            consumed.original_expires_unix_ms =
                Some(r.original_expires_unix_ms.unwrap_or(r.expires_unix_ms));
            consumed.expires_unix_ms = deadline.min(r.expires_unix_ms);
            if consumed.expires_unix_ms <= now {
                return Err(GateError::Deadline);
            }
            let changed=tx.execute("UPDATE reservations SET state='consumed',body=?3,expires=?4 WHERE id=?1 AND body=?2 AND state='dispatched'",params![r.id.as_str(),canonical(r).map_err(persistence)?,canonical(&consumed).map_err(persistence)?,i64::try_from(consumed.expires_unix_ms).map_err(persistence)?]).map_err(persistence)?;
            if changed != 1 {
                return Err(GateError::Consumed);
            }
            tx.commit().map_err(persistence)?;
            Ok(consumed)
        })();
        self.checked_integrity(result)
    }
    fn narrow_consumed(&self, r: &Reservation, deadline: u64) -> Result<Reservation, GateError> {
        self.local(&r.profile)?;
        let result = (|| {
            let mut conn = self.connection.lock().map_err(persistence)?;
            let tx = conn
                .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
                .map_err(persistence)?;
            if Self::terminal(&tx, &self.profile, &r.id)?.is_some() {
                return Err(GateError::Consumed);
            }
            let body: Vec<u8> = tx
                .query_row(
                    "SELECT body FROM reservations WHERE id=?1 AND state='consumed'",
                    [r.id.as_str()],
                    |row| row.get(0),
                )
                .optional()
                .map_err(persistence)?
                .ok_or(GateError::Consumed)?;
            let mut current = validated_reservation(&self.profile, r.id.as_str(), &body)?;
            if !same_reservation_authority(&current, r)? {
                return Err(GateError::Denied("lease binding mismatch".into()));
            }
            current.expires_unix_ms = current.expires_unix_ms.min(deadline);
            tx.execute(
                "UPDATE reservations SET body=?2,expires=?3 WHERE id=?1 AND state='consumed'",
                params![
                    r.id.as_str(),
                    canonical(&current).map_err(persistence)?,
                    i64::try_from(current.expires_unix_ms).map_err(persistence)?
                ],
            )
            .map_err(persistence)?;
            tx.commit().map_err(persistence)?;
            Ok(current)
        })();
        self.checked_integrity(result)
    }
    fn with_cleanup_snapshot(
        &self,
        expected: &Reservation,
        inspect: &mut dyn FnMut(CleanupAuthoritySnapshot<'_>) -> Result<(), GateError>,
    ) -> Result<(), GateError> {
        self.local(&expected.profile)?;
        validate_reservation_identity(expected)?;
        if expected.original_expires_unix_ms.is_none() {
            return Err(GateError::Denied(
                "cleanup requires consumed lease evidence".into(),
            ));
        }
        // Callers may hold native lifecycle guards. Never wait for this
        // connection, or for a different SQLite writer, while selecting cause.
        let conn = self.connection.try_lock().map_err(|error| match error {
            std::sync::TryLockError::WouldBlock => GateError::Unavailable,
            std::sync::TryLockError::Poisoned(_) => persistence("approval lock poisoned"),
        })?;
        let previous_ms = conn
            .pragma_query_value(None, "busy_timeout", |row| crate::sql_u64(row, 0))
            .map_err(persistence)?;
        conn.busy_timeout(std::time::Duration::ZERO)
            .map_err(persistence)?;
        let busy_timeout = CleanupBusyTimeout {
            connection: &conn,
            previous: std::time::Duration::from_millis(previous_ms),
            restored: false,
        };
        let result = (|| {
            // The enclosing mutex excludes nested use of this connection.
            // IMMEDIATE also excludes writers through other connections while
            // the synchronous native callback selects its one-time record.
            let tx = rusqlite::Transaction::new_unchecked(
                &conn,
                rusqlite::TransactionBehavior::Immediate,
            )
            .map_err(persistence)?;
            deny_faulted(&tx)?;
            let current_epoch = tx
                .query_row(
                    "SELECT epoch FROM security_epoch WHERE singleton=1",
                    [],
                    |row| crate::sql_u64(row, 0),
                )
                .map_err(persistence)?;
            let terminal = Self::terminal(&tx, &self.profile, &expected.id)?;
            let consumed = if terminal.is_none() {
                let (body, expires): (Vec<u8>, u64) = tx
                    .query_row(
                        "SELECT CASE WHEN length(body)<=4096 THEN body ELSE x'' END,expires FROM reservations WHERE id=?1 AND state='consumed'",
                        [expected.id.as_str()],
                        |row| Ok((row.get(0)?, crate::sql_u64(row, 1)?)),
                    )
                    .optional()
                    .map_err(persistence)?
                    .ok_or(GateError::Consumed)?;
                let current = validated_reservation(&self.profile, expected.id.as_str(), &body)?;
                if current.expires_unix_ms != expires {
                    return Err(GateError::Integrity(IntegrityFaultCode::ReservationRecord));
                }
                Some(current)
            } else {
                None
            };
            let current = match &terminal {
                Some(terminal) => {
                    if matches!(
                        terminal.decision.disposition,
                        TerminalDisposition::AbandonedBeforeUse
                            | TerminalDisposition::ExpiredBeforeUse
                    ) {
                        return Err(GateError::Consumed);
                    }
                    &terminal.decision.reservation
                }
                None => consumed.as_ref().ok_or(GateError::Consumed)?,
            };
            cleanup_binding(current, expected)?;
            // No ordinary admission check: changed epochs and due deadlines
            // are the facts being observed, not grounds for hiding the lease.
            let inspected = inspect(CleanupAuthoritySnapshot {
                current_epoch,
                reservation: current,
                now_unix_ms: now_ms().map_err(persistence)?,
                terminal: terminal.as_ref(),
            });
            // There were no SQL writes. Explicit teardown still reports loss
            // of certainty, without rolling back an already-selected native
            // cause or rerunning the callback.
            let teardown = tx.rollback().map_err(persistence);
            inspected.and(teardown)
        })();
        let restored = busy_timeout.restore();
        // This path performs no durable writes, including integrity latching.
        result.and(restored)
    }
    fn settle_deadline(&self, r: &Reservation) -> Result<TerminalDelivery, GateError> {
        self.local(&r.profile)?;
        let result = (|| {
            let mut conn = self.connection.lock().map_err(persistence)?;
            let tx = conn
                .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
                .map_err(persistence)?;
            if let Some(existing) = Self::terminal(&tx, &self.profile, &r.id)? {
                if !same_reservation_authority(&existing.decision.reservation, r)? {
                    return Err(GateError::Denied("lease binding mismatch".into()));
                }
                tx.commit().map_err(persistence)?;
                return Ok(existing);
            }
            let body: Vec<u8> = tx
                .query_row(
                    "SELECT body FROM reservations WHERE id=?1 AND state='consumed'",
                    [r.id.as_str()],
                    |row| row.get(0),
                )
                .optional()
                .map_err(persistence)?
                .ok_or(GateError::Consumed)?;
            let current = validated_reservation(&self.profile, r.id.as_str(), &body)?;
            if !same_reservation_authority(&current, r)? {
                return Err(GateError::Denied("lease binding mismatch".into()));
            }
            if current.expires_unix_ms > now_ms().map_err(persistence)? {
                return Err(GateError::Denied("lease deadline is not due".into()));
            }
            let decision = Self::terminalize(
                &tx,
                current,
                Outcome::Failed {
                    code: "deadline-failure-unknown".into(),
                },
                TerminalDisposition::ExpiredCleanupUnknown,
            )?;
            tx.commit().map_err(persistence)?;
            Ok(TerminalDelivery {
                decision,
                acknowledged: false,
            })
        })();
        self.checked_integrity(result)
    }
    fn commit_permission_effect(
        &self,
        r: &Reservation,
        deadline: u64,
        effect: PermissionEffect<'_>,
    ) -> Result<TerminalDelivery, GateError> {
        self.local(&r.profile)?;
        if effect.request().digest()? != r.effect {
            return Err(GateError::Denied(
                "permission effect binding mismatch".into(),
            ));
        }
        let digest = Digest::hash(&canonical(&effect).map_err(persistence)?);
        let result = (|| {
            let mut conn = self.connection.lock().map_err(persistence)?;
            let tx = conn
                .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
                .map_err(persistence)?;
            if let Some(existing) = Self::terminal(&tx, &self.profile, &r.id)? {
                if !same_reservation_authority(&existing.decision.reservation, r)? {
                    return Err(GateError::Denied(
                        "permission reservation binding mismatch".into(),
                    ));
                }
                if existing.decision.outcome == Outcome::Succeeded {
                    let retained: String = tx
                        .query_row(
                            "SELECT digest FROM permission_effects WHERE grant_id=?1",
                            [r.id.as_str()],
                            |row| row.get(0),
                        )
                        .map_err(persistence)?;
                    if retained != digest.as_str() {
                        return Err(GateError::Denied("permission effect retry differs".into()));
                    }
                }
                tx.commit().map_err(persistence)?;
                return Ok(existing); // Never replay the effect or replace an expiry winner.
            }
            deny_faulted(&tx)?;
            let body: Vec<u8> = tx
                .query_row(
                    "SELECT body FROM reservations WHERE id=?1 AND state='dispatched'",
                    [r.id.as_str()],
                    |row| row.get(0),
                )
                .optional()
                .map_err(persistence)?
                .ok_or(GateError::Consumed)?;
            let mut current = validated_reservation(&self.profile, r.id.as_str(), &body)?;
            if canonical(&current).map_err(persistence)? != canonical(r).map_err(persistence)? {
                return Err(GateError::Denied(
                    "permission reservation binding mismatch".into(),
                ));
            }
            let epoch: u64 = tx
                .query_row(
                    "SELECT epoch FROM security_epoch WHERE singleton=1",
                    [],
                    |row| crate::sql_u64(row, 0),
                )
                .map_err(persistence)?;
            if epoch != current.epoch {
                return Err(GateError::StaleEpoch);
            }
            current.original_expires_unix_ms = Some(
                current
                    .original_expires_unix_ms
                    .unwrap_or(current.expires_unix_ms),
            );
            current.expires_unix_ms = current.expires_unix_ms.min(deadline);
            if current.expires_unix_ms <= now_ms().map_err(persistence)? {
                let decision = Self::terminalize(
                    &tx,
                    current,
                    Outcome::Failed {
                        code: "deadline-expired-before-use".into(),
                    },
                    TerminalDisposition::ExpiredBeforeUse,
                )?;
                tx.commit().map_err(persistence)?;
                return Ok(TerminalDelivery {
                    decision,
                    acknowledged: false,
                });
            }
            tx.execute("UPDATE reservations SET body=?2,expires=?3,state='consumed' WHERE id=?1 AND state='dispatched'",params![r.id.as_str(),canonical(&current).map_err(persistence)?,i64::try_from(current.expires_unix_ms).map_err(persistence)?]).map_err(persistence)?;
            // Candidate approvals do not advance the active revision epoch; the
            // committed Profile CAS performs that advancement under its lock.
            tx.execute_batch("SAVEPOINT permission_mutation")
                .map_err(persistence)?;
            match effect {
                PermissionEffect::Pack {
                    pack,
                    hash,
                    permissions,
                } => {
                    tx.execute("INSERT INTO permissions(pack,hash,body) VALUES(?1,?2,?3) ON CONFLICT(pack,hash) DO UPDATE SET body=excluded.body",params![pack.as_str(),hash.as_str(),canonical(permissions).map_err(persistence)?]).map_err(persistence)?;
                }
                PermissionEffect::User {
                    profile_hash,
                    revision,
                    epoch,
                    permissions,
                } => {
                    tx.execute("INSERT INTO user_approvals(profile_hash,revision,epoch,body) VALUES(?1,?2,?3,?4) ON CONFLICT(profile_hash,revision,epoch) DO UPDATE SET body=excluded.body",params![profile_hash.as_str(),revision.as_str(),i64::try_from(epoch).map_err(persistence)?,canonical(permissions).map_err(persistence)?]).map_err(persistence)?;
                }
            }
            let due = current.expires_unix_ms <= now_ms().map_err(persistence)?;
            if due {
                tx.execute_batch("ROLLBACK TO permission_mutation; RELEASE permission_mutation")
                    .map_err(persistence)?;
            } else {
                tx.execute(
                    "INSERT INTO permission_effects(grant_id,digest) VALUES(?1,?2)",
                    params![r.id.as_str(), digest.as_str()],
                )
                .map_err(persistence)?;
                tx.execute_batch("RELEASE permission_mutation")
                    .map_err(persistence)?;
            }
            let decision = Self::terminalize(
                &tx,
                current,
                if due {
                    Outcome::Failed {
                        code: "deadline-expired-before-use".into(),
                    }
                } else {
                    Outcome::Succeeded
                },
                if due {
                    TerminalDisposition::ExpiredBeforeUse
                } else {
                    TerminalDisposition::Completed
                },
            )?;
            tx.commit().map_err(persistence)?;
            Ok(TerminalDelivery {
                decision,
                acknowledged: false,
            })
        })();
        self.checked_integrity(result)
    }
    fn finish(&self, id: &GrantId, outcome: &Outcome) -> Result<Reservation, GateError> {
        Ok(self.finish_terminal(id, outcome)?.decision.reservation)
    }
    fn terminal_delivery(&self, id: &GrantId) -> Result<Option<TerminalDelivery>, GateError> {
        let result = (|| {
            let mut conn = self.connection.lock().map_err(persistence)?;
            let tx = conn.transaction().map_err(persistence)?;
            let record = Self::terminal(&tx, &self.profile, id)?;
            tx.commit().map_err(persistence)?;
            Ok(record)
        })();
        self.checked_integrity(result)
    }
    fn finish_terminal(
        &self,
        id: &GrantId,
        outcome: &Outcome,
    ) -> Result<TerminalDelivery, GateError> {
        let result = (|| {
            let mut conn = self.connection.lock().map_err(persistence)?;
            let tx = conn
                .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
                .map_err(persistence)?;
            if let Some(existing) = Self::terminal(&tx, &self.profile, id)? {
                if existing.decision.outcome != *outcome {
                    return Err(GateError::Denied(
                        "terminal proposal conflicts with immutable decision".into(),
                    ));
                }
                tx.commit().map_err(persistence)?;
                return Ok(existing);
            }
            let body: Option<Vec<u8>> = tx
                .query_row(
                    "SELECT body FROM reservations WHERE id=?1 AND state='consumed'",
                    [id.as_str()],
                    |r| r.get(0),
                )
                .optional()
                .map_err(persistence)?;
            let reservation = validated_reservation(
                &self.profile,
                id.as_str(),
                &body.ok_or(GateError::Consumed)?,
            )?;
            let due = reservation.expires_unix_ms <= now_ms().map_err(persistence)?;
            let decision = Self::terminalize(
                &tx,
                reservation,
                if due {
                    Outcome::Failed {
                        code: "deadline-failure-unknown".into(),
                    }
                } else {
                    outcome.clone()
                },
                if due {
                    TerminalDisposition::ExpiredCleanupUnknown
                } else {
                    TerminalDisposition::Completed
                },
            )?;
            tx.commit().map_err(persistence)?;
            Ok(TerminalDelivery {
                decision,
                acknowledged: false,
            })
        })();
        self.checked_integrity(result)
    }
    fn abandon_terminal(&self, id: &GrantId) -> Result<Option<TerminalDelivery>, GateError> {
        let result = (|| {
            let mut conn = self.connection.lock().map_err(persistence)?;
            let tx = conn
                .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
                .map_err(persistence)?;
            if let Some(existing) = Self::terminal(&tx, &self.profile, id)? {
                tx.commit().map_err(persistence)?;
                return Ok(Some(existing));
            }
            let body: Option<Vec<u8>> = tx
            .query_row(
                "SELECT body FROM reservations WHERE id=?1 AND state IN('pending','dispatched')",
                [id.as_str()],
                |r| r.get(0),
            )
            .optional()
            .map_err(persistence)?;
            let Some(body) = body else {
                return Ok(None);
            };
            let reservation = validated_reservation(&self.profile, id.as_str(), &body)?;
            let due = reservation.expires_unix_ms <= now_ms().map_err(persistence)?;
            let decision = Self::terminalize(
                &tx,
                reservation,
                if due {
                    Outcome::Failed {
                        code: "deadline-expired-before-use".into(),
                    }
                } else {
                    Outcome::Cancelled
                },
                if due {
                    TerminalDisposition::ExpiredBeforeUse
                } else {
                    TerminalDisposition::AbandonedBeforeUse
                },
            )?;
            tx.commit().map_err(persistence)?;
            Ok(Some(TerminalDelivery {
                decision,
                acknowledged: false,
            }))
        })();
        self.checked_integrity(result)
    }
    fn expire_terminal(
        &self,
        profile: &ProfileId,
        now: u64,
    ) -> Result<Vec<TerminalDecision>, GateError> {
        self.local(profile)?;
        let result = (|| {
            let mut conn = self.connection.lock().map_err(persistence)?;
            let tx = conn
                .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
                .map_err(persistence)?;
            let expired = {
                let mut query=tx.prepare("SELECT id,CASE WHEN length(body)<=4096 THEN body ELSE x'' END,state,expires FROM reservations WHERE state IN('pending','dispatched','consumed') AND expires<=?1 AND NOT EXISTS(SELECT 1 FROM reservation_quarantine WHERE reservation_quarantine.id=reservations.id) ORDER BY rowid LIMIT 4096").map_err(persistence)?;
                query
                    .query_map([now.min(i64::MAX as u64) as i64], |row| {
                        Ok((
                            row.get::<_, String>(0)?,
                            row.get::<_, Vec<u8>>(1)?,
                            row.get::<_, String>(2)?,
                            crate::sql_u64(row, 3)?,
                        ))
                    })
                    .map_err(persistence)?
                    .collect::<Result<Vec<_>, _>>()
                    .map_err(persistence)?
            };
            let mut decisions = Vec::with_capacity(expired.len());
            for (id, body, state, expires) in expired {
                let reservation = validated_reservation(profile, &id, &body).and_then(|r| {
                    if r.expires_unix_ms == expires {
                        Ok(r)
                    } else {
                        Err(GateError::Integrity(IntegrityFaultCode::ReservationRecord))
                    }
                });
                let reservation = match reservation {
                    Ok(r) => r,
                    Err(GateError::Integrity(code)) => {
                        latch(&tx, code)?;
                        tx.execute(
                            "INSERT OR IGNORE INTO reservation_quarantine(id,code) VALUES(?1,?2)",
                            params![id, canonical(&code).map_err(persistence)?],
                        )
                        .map_err(persistence)?;
                        continue; // Keep corrupt evidence and its reservation; no guessed outcome.
                    }
                    Err(error) => return Err(error),
                };
                let unknown = state == "consumed";
                decisions.push(Self::terminalize(
                    &tx,
                    reservation,
                    Outcome::Failed {
                        code: if unknown {
                            "deadline-failure-unknown"
                        } else {
                            "deadline-expired-before-use"
                        }
                        .into(),
                    },
                    if unknown {
                        TerminalDisposition::ExpiredCleanupUnknown
                    } else {
                        TerminalDisposition::ExpiredBeforeUse
                    },
                )?);
            }
            tx.commit().map_err(persistence)?;
            Ok(decisions)
        })();
        self.checked_integrity(result)
    }
    fn pending_terminal(
        &self,
        profile: &ProfileId,
        max: u32,
    ) -> Result<Vec<TerminalDecision>, GateError> {
        self.pending_terminal_through(profile, max, i64::MAX as u64)
    }
    fn terminal_watermark(&self, profile: &ProfileId) -> Result<u64, GateError> {
        self.local(profile)?;
        let conn = self.connection.lock().map_err(persistence)?;
        conn.query_row(
            "SELECT COALESCE(MAX(rowid),0) FROM terminal_outbox",
            [],
            |r| crate::sql_u64(r, 0),
        )
        .map_err(persistence)
    }
    fn pending_terminal_through(
        &self,
        profile: &ProfileId,
        max: u32,
        watermark: u64,
    ) -> Result<Vec<TerminalDecision>, GateError> {
        self.local(profile)?;
        if max > 4096 {
            return Err(GateError::Limit("terminal delivery batch".into()));
        }
        let result = (|| {
            let mut conn = self.connection.lock().map_err(persistence)?;
            let tx = conn
                .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
                .map_err(persistence)?;
            let rows = {
                let mut query=tx.prepare("SELECT grant_id,event_id,CASE WHEN length(body)<=4096 THEN body ELSE x'' END FROM terminal_outbox WHERE delivered=0 AND quarantined=0 AND rowid<=?2 ORDER BY rowid LIMIT ?1").map_err(persistence)?;
                query
                    .query_map(params![max, watermark.min(i64::MAX as u64) as i64], |row| {
                        Ok((
                            row.get::<_, String>(0)?,
                            row.get::<_, String>(1)?,
                            row.get::<_, Vec<u8>>(2)?,
                        ))
                    })
                    .map_err(persistence)?
                    .collect::<Result<Vec<_>, _>>()
                    .map_err(persistence)?
            };
            let mut decisions = Vec::with_capacity(rows.len());
            for (grant, event, body) in rows {
                match validated_terminal(profile, &grant, &event, &body) {
                    Ok(decision) => decisions.push(decision),
                    Err(GateError::Integrity(code)) => {
                        latch(&tx, code)?;
                        tx.execute(
                            "UPDATE terminal_outbox SET quarantined=1 WHERE grant_id=?1",
                            [&grant],
                        )
                        .map_err(persistence)?;
                    }
                    Err(error) => return Err(error),
                }
            }
            tx.commit().map_err(persistence)?;
            Ok(decisions)
        })();
        self.checked_integrity(result)
    }
    fn acknowledge_terminal(&self, event_id: &Digest, event: &AuditEvent) -> Result<(), GateError> {
        let result = (|| {
            let mut conn = self.connection.lock().map_err(persistence)?;
            let tx = conn
                .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
                .map_err(persistence)?;
            let (grant, body): (String, Vec<u8>) = tx
                .query_row(
                    "SELECT grant_id,body FROM terminal_outbox WHERE event_id=?1",
                    [event_id.as_str()],
                    |r| Ok((r.get(0)?, r.get(1)?)),
                )
                .map_err(persistence)?;
            let decision = validated_terminal(&self.profile, &grant, event_id.as_str(), &body)?;
            if canonical(&decision.event).map_err(persistence)?
                != canonical(event).map_err(persistence)?
            {
                return Err(GateError::Denied(
                    "terminal acknowledgement payload conflict".into(),
                ));
            }
            tx.execute(
                "UPDATE terminal_outbox SET delivered=1 WHERE event_id=?1",
                [event_id.as_str()],
            )
            .map_err(persistence)?;
            tx.commit().map_err(persistence)?;
            Ok(())
        })();
        self.checked_integrity(result)
    }
    fn public_write_approved(
        &self,
        profile: &ProfileId,
        writer: &PackId,
        owner: &PackId,
        store: &str,
    ) -> Result<bool, GateError> {
        self.local(profile)?;
        let conn = self.connection.lock().map_err(persistence)?;
        conn.query_row(
            "SELECT EXISTS(SELECT 1 FROM public_writers WHERE writer=?1 AND owner=?2 AND store=?3)",
            params![writer.as_str(), owner.as_str(), store],
            |r| r.get(0),
        )
        .map_err(persistence)
    }
    fn approve_public_write(
        &self,
        profile: &ProfileId,
        writer: &PackId,
        owner: &PackId,
        store: &str,
    ) -> Result<(), GateError> {
        self.local(profile)?;
        let conn = self.connection.lock().map_err(persistence)?;
        conn.execute(
            "INSERT OR IGNORE INTO public_writers(writer,owner,store) VALUES(?1,?2,?3)",
            params![writer.as_str(), owner.as_str(), store],
        )
        .map_err(persistence)?;
        Ok(())
    }
    fn paired_device_key(
        &self,
        profile: &ProfileId,
        device: &str,
    ) -> Result<Option<[u8; 32]>, GateError> {
        self.local(profile)?;
        let conn = self.connection.lock().map_err(persistence)?;
        let key: Option<Vec<u8>> = conn
            .query_row(
                "SELECT key FROM paired_devices WHERE device=?1",
                [device],
                |r| r.get(0),
            )
            .optional()
            .map_err(persistence)?;
        key.map(|bytes| {
            bytes
                .try_into()
                .map_err(|_| GateError::Persistence("invalid paired key length".into()))
        })
        .transpose()
    }
    fn claim_attestation(
        &self,
        profile: &ProfileId,
        device: &str,
        nonce: &str,
        expires_unix_ms: u64,
    ) -> Result<bool, GateError> {
        self.local(profile)?;
        let mut conn = self.connection.lock().map_err(persistence)?;
        let tx = conn
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
            .map_err(persistence)?;
        let now = now_ms().map_err(persistence)?;
        if expires_unix_ms <= now || expires_unix_ms > now.saturating_add(300000) {
            return Err(GateError::Denied("attestation expiry invalid".into()));
        }
        tx.execute(
            "DELETE FROM attestation_nonces WHERE expires<=?1",
            [now as i64],
        )
        .map_err(persistence)?;
        let count: i64 = tx
            .query_row("SELECT COUNT(*) FROM attestation_nonces", [], |r| r.get(0))
            .map_err(persistence)?;
        if count >= 4096 {
            return Err(GateError::Limit("attestation nonce quota".into()));
        }
        let changed = tx
            .execute(
                "INSERT OR IGNORE INTO attestation_nonces(device,nonce,expires) VALUES(?1,?2,?3)",
                params![device, nonce, expires_unix_ms as i64],
            )
            .map_err(persistence)?;
        tx.commit().map_err(persistence)?;
        Ok(changed == 1)
    }
}

#[cfg(test)]
mod cleanup_snapshot_tests {
    use super::*;
    use rusqlite::hooks::{AuthAction, AuthContext, Authorization, TransactionOperation};
    use tobkiri_canon::ids::RequestId;
    use tobkiri_gate::{EffectRequest, Principal};

    fn consumed() -> (tempfile::TempDir, Approvals, Reservation) {
        let temp = tempfile::tempdir().unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(temp.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
        }
        // Let DataRoot create its private child with the current user's owner
        // descriptor; a runner's ordinary temp directory can have another owner.
        let root = DataRoot::open(temp.path().canonicalize().unwrap().join("data")).unwrap();
        let profile = ProfileId::parse("prof_cleanup").unwrap();
        let approvals = Approvals::open(&root, &profile).unwrap();
        // Actual legacy Store reservation/dispatch/consume, rather than a mock
        // ApprovalStore or a fabricated sealed Gate permit.
        let reservation = Reservation {
            id: serde_json::from_value(serde_json::Value::String(format!(
                "grant_{}",
                Digest::hash(b"cleanup snapshot fixture").hex()
            )))
            .unwrap(),
            profile,
            principal: Principal::User,
            request: RequestId::parse("req_cleanup").unwrap(),
            effect: EffectRequest::AuditRead.digest().unwrap(),
            args: Digest::hash(b"cleanup arguments"),
            epoch: 0,
            revision: None,
            expires_unix_ms: now_ms().unwrap() + 60_000,
            original_expires_unix_ms: None,
            authority: None,
        };
        approvals.reserve(&reservation).unwrap();
        approvals.dispatch(&reservation.id).unwrap();
        let reservation = approvals
            .consume_with_deadline(&reservation, reservation.expires_unix_ms)
            .unwrap();
        (temp, approvals, reservation)
    }

    #[test]
    fn cleanup_snapshot_connection_contention_does_not_wait_or_inspect() {
        let (_temp, approvals, reservation) = consumed();
        let connection = approvals.connection.lock().unwrap();
        let mut calls = 0;
        let start = std::time::Instant::now();
        assert!(matches!(
            approvals.with_cleanup_snapshot(&reservation, &mut |_| {
                calls += 1;
                Ok(())
            }),
            Err(GateError::Unavailable)
        ));
        assert_eq!(calls, 0);
        assert!(start.elapsed() < std::time::Duration::from_secs(1));
        drop(connection);
        approvals
            .with_cleanup_snapshot(&reservation, &mut |_| {
                calls += 1;
                Ok(())
            })
            .unwrap();
        assert_eq!(calls, 1);
    }

    #[test]
    fn cleanup_snapshot_teardown_failure_keeps_native_selection_and_restores_busy_policy() {
        let (_temp, approvals, reservation) = consumed();
        let (busy_before, changes_before) = {
            let connection = approvals.connection.lock().unwrap();
            let busy: u64 = connection
                .pragma_query_value(None, "busy_timeout", |row| crate::sql_u64(row, 0))
                .unwrap();
            connection
                .authorizer(Some(|context: AuthContext<'_>| match context.action {
                    AuthAction::Transaction {
                        operation: TransactionOperation::Rollback,
                    } => Authorization::Deny,
                    _ => Authorization::Allow,
                }))
                .unwrap();
            (busy, connection.total_changes())
        };
        let mut calls = 0;
        let mut native_selected = false;
        let result = approvals.with_cleanup_snapshot(&reservation, &mut |_| {
            calls += 1;
            native_selected = true;
            Ok(())
        });
        assert!(matches!(result, Err(GateError::Persistence(_))));
        assert_eq!(calls, 1);
        assert!(native_selected);
        let connection = approvals.connection.lock().unwrap();
        assert!(!connection.is_autocommit());
        assert_eq!(connection.total_changes(), changes_before);
        let busy_after: u64 = connection
            .pragma_query_value(None, "busy_timeout", |row| crate::sql_u64(row, 0))
            .unwrap();
        assert_eq!(busy_after, busy_before);
        connection
            .authorizer(None::<fn(AuthContext<'_>) -> Authorization>)
            .unwrap();
        connection.execute_batch("ROLLBACK").unwrap();
    }
}
