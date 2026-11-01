//! Real SQLite regressions for prefix evidence, durability and exact retries.

use super::*;
use std::os::unix::fs::PermissionsExt;
use tobkiri_canon::{CanonValue, ids::RequestId};
use tobkiri_gate::{IntegrityFaultCode, Principal};

struct Fixture {
    _dir: tempfile::TempDir,
    root: DataRoot,
    audit: AuditLog,
    profile: ProfileId,
}

impl Fixture {
    fn new() -> Self {
        let dir = tempfile::Builder::new()
            .permissions(std::fs::Permissions::from_mode(0o700))
            .tempdir()
            .unwrap();
        let root = DataRoot::open(dir.path().canonicalize().unwrap()).unwrap();
        let profile = ProfileId::parse("prof_prefix").unwrap();
        let audit = AuditLog::open(&root, &profile, AuditQuota::default()).unwrap();
        let f = Self {
            _dir: dir,
            root,
            audit,
            profile,
        };
        for index in 1..=3 {
            f.audit
                .append_idempotent(&Self::id(index), f.event(index))
                .unwrap();
        }
        assert!(f.has_proof());
        f
    }

    fn id(index: u64) -> Digest {
        Digest::hash(&index.to_be_bytes())
    }

    fn event(&self, index: u64) -> AuditEvent {
        AuditEvent {
            profile: self.profile.clone(),
            principal: Principal::User,
            request: RequestId::parse(&format!("req_prefix_{index}")).unwrap(),
            kind: "prefix-regression".into(),
            payload: CanonValue::Int(index as i64),
        }
    }

    fn raw(&self) -> rusqlite::Connection {
        rusqlite::Connection::open(
            self.root
                .path()
                .join(format!("profiles/{}/audit.sqlite", self.profile)),
        )
        .unwrap()
    }

    fn has_proof(&self) -> bool {
        self.audit.verified_prefix.lock().unwrap().is_some()
    }

    fn count(&self) -> u64 {
        self.raw()
            .query_row("SELECT COUNT(*) FROM audit", [], |row| {
                crate::sql_u64(row, 0)
            })
            .unwrap()
    }

    fn retry(&self) -> Result<(), GateError> {
        self.audit.append_idempotent(&Self::id(3), self.event(3))
    }
}

#[test]
fn old_row_changes_and_sql_type_aliases_cannot_hide_behind_an_unchanged_tail() {
    // All mutations target an OLD row. A valid last event identity and digest
    // must never make an exact retry acknowledge a corrupt prefix.
    for mutation in [
        "UPDATE audit SET body=x'7b7d' WHERE seq=1",
        "UPDATE audit SET body=CAST(body AS TEXT) WHERE seq=1",
        "UPDATE audit SET body=zeroblob(65537) WHERE seq=1",
        "UPDATE audit SET prev_digest=digest WHERE seq=1",
        "UPDATE audit SET prev_digest=CAST(prev_digest AS BLOB) WHERE seq=1",
        "UPDATE audit SET digest=prev_digest WHERE seq=1",
        "UPDATE audit SET digest=CAST(digest AS BLOB) WHERE seq=1",
        "UPDATE audit SET event_id=NULL WHERE seq=1",
        "UPDATE audit SET event_id=CAST(event_id AS BLOB) WHERE seq=1",
        "UPDATE audit SET seq=0 WHERE seq=1",
    ] {
        let f = Fixture::new();
        let raw = f.raw();
        raw.execute_batch("DROP TRIGGER audit_no_update").unwrap();
        raw.execute_batch(mutation).unwrap();
        assert!(
            matches!(
                f.retry(),
                Err(GateError::Integrity(IntegrityFaultCode::AuditChain))
            ),
            "mutation accepted: {mutation}"
        );
        assert!(!f.has_proof());
        assert_eq!(f.count(), 3);
    }
}

#[test]
fn semantically_identical_peer_reencoding_gets_full_verification_without_rewriting_history() {
    let f = Fixture::new();
    let raw = f.raw();
    let pretty = serde_json::to_vec_pretty(&f.event(1)).unwrap();
    assert_ne!(pretty, canonical(&f.event(1)).unwrap());
    raw.execute_batch("DROP TRIGGER audit_no_update").unwrap();
    raw.execute("UPDATE audit SET body=?1 WHERE seq=1", [&pretty])
        .unwrap();
    f.retry().unwrap();
    assert!(f.has_proof());
    f.audit.verify().unwrap();
    let stored: Vec<u8> = raw
        .query_row("SELECT body FROM audit WHERE seq=1", [], |row| row.get(0))
        .unwrap();
    assert_eq!(stored, pretty);
    assert_eq!(f.count(), 3);
}

#[test]
fn a_peer_writer_and_reopen_reestablish_evidence_from_the_current_full_chain() {
    let f = Fixture::new();
    let peer = AuditLog::open(&f.root, &f.profile, AuditQuota::default()).unwrap();
    assert!(peer.verified_prefix.lock().unwrap().is_none());
    peer.append_idempotent(&Fixture::id(4), f.event(4)).unwrap();
    f.retry().unwrap();
    f.audit.append(f.event(5)).unwrap();
    assert_eq!(f.count(), 5);
    f.audit.verify().unwrap();
    assert!(!f.has_proof());
    let reopened = AuditLog::open(&f.root, &f.profile, AuditQuota::default()).unwrap();
    assert!(reopened.verified_prefix.lock().unwrap().is_none());
    reopened
        .append_idempotent(&Fixture::id(3), f.event(3))
        .unwrap();
    assert_eq!(f.count(), 5);
}

#[test]
fn duplicate_body_conflict_and_quota_errors_clear_evidence_without_duplicate_rows() {
    let mut f = Fixture::new();
    assert!(matches!(
        f.audit.append_idempotent(&Fixture::id(3), f.event(4)),
        Err(GateError::Integrity(
            IntegrityFaultCode::AuditIdentityConflict
        ))
    ));
    assert!(!f.has_proof());
    f.retry().unwrap();
    f.audit.quota.records_per_minute = 0;
    // A durable exact retry remains permitted before quota admission.
    f.retry().unwrap();
    assert!(matches!(
        f.audit.append(f.event(4)),
        Err(GateError::Limit(_))
    ));
    assert!(!f.has_proof());
    f.retry().unwrap();
    assert_eq!(f.count(), 3);
}

#[test]
fn insertion_failure_rolls_back_and_forces_full_verification_on_retry() {
    let f = Fixture::new();
    let raw = f.raw();
    raw.execute_batch("CREATE TRIGGER prefix_insert_fault BEFORE INSERT ON audit BEGIN SELECT RAISE(ABORT,'injected insert fault'); END;").unwrap();
    assert!(matches!(
        f.audit.append(f.event(4)),
        Err(GateError::Persistence(_))
    ));
    assert!(!f.has_proof());
    assert_eq!(f.count(), 3);
    raw.execute_batch("DROP TRIGGER prefix_insert_fault")
        .unwrap();
    f.retry().unwrap();
    assert_eq!(f.count(), 3);
    f.audit.verify().unwrap();
}

#[test]
fn commit_failure_never_publishes_evidence_or_success_and_rolls_back_the_insert() {
    let f = Fixture::new();
    {
        let conn = f.audit.connection.lock().unwrap();
        conn.execute_batch("PRAGMA foreign_keys=ON; CREATE TABLE prefix_parent(id INTEGER PRIMARY KEY); CREATE TABLE prefix_child(id INTEGER REFERENCES prefix_parent(id) DEFERRABLE INITIALLY DEFERRED); CREATE TRIGGER prefix_commit_fault AFTER INSERT ON audit BEGIN INSERT INTO prefix_child VALUES(999); END;").unwrap();
    }
    // INSERT and the post-insert proof succeed; only COMMIT rejects this TX.
    assert!(matches!(
        f.audit.append(f.event(4)),
        Err(GateError::Persistence(_))
    ));
    assert!(!f.has_proof());
    assert_eq!(f.count(), 3);
    let raw = f.raw();
    assert_eq!(
        raw.query_row("SELECT COUNT(*) FROM prefix_child", [], |row| row
            .get::<_, i64>(0))
            .unwrap(),
        0
    );
    raw.execute_batch("DROP TRIGGER prefix_commit_fault")
        .unwrap();
    f.retry().unwrap();
    f.audit.append(f.event(4)).unwrap();
    assert_eq!(f.count(), 4);
    f.audit.verify().unwrap();
}

#[test]
fn unexpected_insert_trigger_changes_are_detected_before_commit_and_acknowledgment() {
    let f = Fixture::new();
    let raw = f.raw();
    raw.execute_batch("DROP TRIGGER audit_no_update; CREATE TRIGGER prefix_rewrite AFTER INSERT ON audit BEGIN UPDATE audit SET body=x'7b7d' WHERE seq=1; END;").unwrap();
    assert!(matches!(
        f.audit.append(f.event(4)),
        Err(GateError::Integrity(IntegrityFaultCode::AuditChain))
    ));
    assert!(!f.has_proof());
    assert_eq!(f.count(), 3);
    raw.execute_batch("DROP TRIGGER prefix_rewrite").unwrap();
    // Both the new row and the trigger's old-row mutation were rolled back.
    f.audit.verify().unwrap();
    f.retry().unwrap();
}
