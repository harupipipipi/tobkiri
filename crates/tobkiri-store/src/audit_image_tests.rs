//! Disposable native databases exercise reuse without weakening append checks.
use super::*;
use std::sync::atomic::Ordering;
use tobkiri_canon::{CanonValue, ids::RequestId};
use tobkiri_gate::Principal;

fn fixture(quota: AuditQuota) -> (tempfile::TempDir, DataRoot, ProfileId, AuditLog) {
    let temp = tempfile::tempdir().unwrap();
    let root = DataRoot::open(temp.path().canonicalize().unwrap().join("data")).unwrap();
    let profile = ProfileId::parse("prof_audit_image_fixture").unwrap();
    let audit = AuditLog::open(&root, &profile, quota).unwrap();
    (temp, root, profile, audit)
}

fn event(profile: &ProfileId, index: usize) -> AuditEvent {
    AuditEvent {
        profile: profile.clone(),
        principal: Principal::User,
        request: RequestId::parse("req_audit_image_fixture").unwrap(),
        kind: format!("fixture-{index}"),
        payload: CanonValue::Null,
    }
}

fn count(audit: &AuditLog) -> u64 {
    audit
        .connection
        .lock()
        .unwrap()
        .sql
        .query_row("SELECT COUNT(*) FROM audit", [], |r| crate::sql_u64(r, 0))
        .unwrap()
}

fn has_image(audit: &AuditLog) -> bool {
    audit.connection.lock().unwrap().verified.is_some()
}

#[test]
fn unchanged_images_reuse_semantics_but_explicit_verify_always_decodes() {
    let (_temp, _root, profile, audit) = fixture(AuditQuota::default());
    for index in 0..4 {
        audit.append(event(&profile, index)).unwrap();
    }
    assert!(has_image(&audit));
    assert_eq!(audit.decoded_rows.load(Ordering::Relaxed), 0);
    audit.verify().unwrap();
    assert_eq!(audit.decoded_rows.load(Ordering::Relaxed), 4);
    assert!(!has_image(&audit));
    audit.append(event(&profile, 4)).unwrap();
    assert_eq!(audit.decoded_rows.load(Ordering::Relaxed), 8);
    audit.append(event(&profile, 5)).unwrap();
    assert_eq!(audit.decoded_rows.load(Ordering::Relaxed), 8);
    assert_eq!(count(&audit), 6);
}

#[test]
fn same_head_middle_tampering_is_detected_by_append_without_explicit_verify() {
    let (_temp, _root, profile, audit) = fixture(AuditQuota::default());
    for index in 0..3 {
        audit.append(event(&profile, index)).unwrap();
    }
    let before = audit.connection.lock().unwrap().verified.clone().unwrap();
    {
        let connection = audit.connection.lock().unwrap();
        let mut changed = event(&profile, 1);
        changed.kind = "modified-middle".into();
        let body = canonical(&changed).unwrap();
        connection
            .sql
            .execute_batch("DROP TRIGGER audit_no_update")
            .unwrap();
        connection
            .sql
            .execute("UPDATE audit SET body=?1 WHERE seq=2", [body])
            .unwrap();
        let actual = VerifiedImage::scan(&connection.sql).unwrap();
        assert_eq!(actual.last, before.last);
        assert!(!actual.matches(&before));
    }
    assert!(matches!(
        audit.append(event(&profile, 3)),
        Err(GateError::Integrity(
            tobkiri_gate::IntegrityFaultCode::AuditChain
        ))
    ));
    assert!(!has_image(&audit));
    assert_eq!(count(&audit), 3);
}

#[test]
fn a_corrupt_tail_sequence_preserves_the_integrity_fault_classification() {
    for sequence in [-1, 0] {
        let (_temp, _root, profile, audit) = fixture(AuditQuota::default());
        audit.append(event(&profile, 0)).unwrap();
        {
            let connection = audit.connection.lock().unwrap();
            connection
                .sql
                .execute_batch("DROP TRIGGER audit_no_update")
                .unwrap();
            connection
                .sql
                .execute("UPDATE audit SET seq=?1", [sequence])
                .unwrap();
        }
        assert!(matches!(
            audit.append(event(&profile, 1)),
            Err(GateError::Integrity(
                tobkiri_gate::IntegrityFaultCode::AuditChain
            ))
        ));
        assert!(!has_image(&audit));
        assert_eq!(count(&audit), 1);
    }
}

#[test]
fn canonical_equivalent_body_falls_back_but_exact_event_identity_stays_strict() {
    let (_temp, _root, profile, audit) = fixture(AuditQuota::default());
    let original = event(&profile, 0);
    let id = Digest::hash(b"image-idempotent");
    audit.append_idempotent(&id, original.clone()).unwrap();
    {
        let connection = audit.connection.lock().unwrap();
        let mut whitespace = canonical(&original).unwrap();
        whitespace.push(b' ');
        connection
            .sql
            .execute_batch("DROP TRIGGER audit_no_update")
            .unwrap();
        connection
            .sql
            .execute("UPDATE audit SET body=?1 WHERE seq=1", [whitespace])
            .unwrap();
    }
    // The canonical event and chain digest still agree, but the image differs.
    audit.append(event(&profile, 1)).unwrap();
    assert_eq!(audit.decoded_rows.load(Ordering::Relaxed), 1);
    assert!(matches!(
        audit.append_idempotent(&id, original),
        Err(GateError::Integrity(
            tobkiri_gate::IntegrityFaultCode::AuditIdentityConflict
        ))
    ));
    assert_eq!(count(&audit), 2);
    assert!(!has_image(&audit));
}

#[test]
fn a_valid_foreign_connection_append_forces_full_fallback_and_preserves_both() {
    let (_temp, root, profile, audit) = fixture(AuditQuota::default());
    audit.append(event(&profile, 0)).unwrap();
    let second = AuditLog::open(&root, &profile, AuditQuota::default()).unwrap();
    second
        .append_idempotent(&Digest::hash(b"foreign"), event(&profile, 1))
        .unwrap();
    audit.append(event(&profile, 2)).unwrap();
    assert_eq!(audit.decoded_rows.load(Ordering::Relaxed), 2);
    assert_eq!(count(&audit), 3);
    audit.verify().unwrap();
    drop(second);
    drop(audit);
    let reopened = AuditLog::open(&root, &profile, AuditQuota::default()).unwrap();
    assert_eq!(count(&reopened), 3);
    assert!(!has_image(&reopened));
}

#[test]
fn quota_and_identity_failures_clear_reuse_without_weakening_idempotency() {
    let quota = AuditQuota {
        records_per_minute: 1,
        ..AuditQuota::default()
    };
    let (_temp, _root, profile, audit) = fixture(quota);
    let id = Digest::hash(b"quota-image");
    let original = event(&profile, 0);
    audit.append_idempotent(&id, original.clone()).unwrap();
    audit.append_idempotent(&id, original.clone()).unwrap();
    assert_eq!(count(&audit), 1);
    assert!(matches!(
        audit.append_idempotent(&id, event(&profile, 1)),
        Err(GateError::Integrity(
            tobkiri_gate::IntegrityFaultCode::AuditIdentityConflict
        ))
    ));
    assert!(!has_image(&audit));
    assert!(matches!(
        audit.append(event(&profile, 2)),
        Err(GateError::Limit(_))
    ));
    assert!(!has_image(&audit));
    audit.append_idempotent(&id, original).unwrap();
    assert_eq!(count(&audit), 1);
}

#[test]
fn begin_scan_and_insert_failures_never_leave_a_verified_cache() {
    for stage in ["begin", "scan", "insert"] {
        let (_temp, _root, profile, audit) = fixture(AuditQuota::default());
        audit.append(event(&profile, 0)).unwrap();
        {
            let connection = audit.connection.lock().unwrap();
            match stage {
                "begin" => connection.sql.execute_batch("BEGIN IMMEDIATE").unwrap(),
                "scan" => connection.sql.execute_batch("ALTER TABLE audit RENAME TO old_audit").unwrap(),
                "insert" => connection.sql.execute_batch("CREATE TRIGGER fixture_deny BEFORE INSERT ON audit BEGIN SELECT RAISE(ABORT,'fixture insert failure'); END").unwrap(),
                _ => unreachable!(),
            }
        }
        assert!(audit.append(event(&profile, 1)).is_err(), "{stage}");
        assert!(!has_image(&audit), "{stage}");
        if stage == "begin" {
            audit
                .connection
                .lock()
                .unwrap()
                .sql
                .execute_batch("ROLLBACK")
                .unwrap();
        }
    }
}

#[test]
fn deferred_commit_failure_rolls_back_and_lost_ack_never_publishes_reuse() {
    let (_temp, _root, profile, audit) = fixture(AuditQuota::default());
    audit.append(event(&profile, 0)).unwrap();
    {
        let connection = audit.connection.lock().unwrap();
        connection.sql.execute_batch("PRAGMA foreign_keys=ON; CREATE TABLE fixture_parent(id INTEGER PRIMARY KEY); CREATE TABLE fixture_child(parent INTEGER REFERENCES fixture_parent(id) DEFERRABLE INITIALLY DEFERRED); CREATE TRIGGER fixture_commit_failure AFTER INSERT ON audit BEGIN INSERT INTO fixture_child VALUES(1); END").unwrap();
    }
    assert!(audit.append(event(&profile, 1)).is_err());
    assert!(!has_image(&audit));
    assert_eq!(count(&audit), 1);
    audit
        .connection
        .lock()
        .unwrap()
        .sql
        .execute_batch("DROP TRIGGER fixture_commit_failure")
        .unwrap();
    let id = Digest::hash(b"lost-ack-image");
    audit.connection.lock().unwrap().lose_commit_ack = true;
    assert!(audit.append_idempotent(&id, event(&profile, 2)).is_err());
    assert_eq!(count(&audit), 2);
    assert!(!has_image(&audit));
    audit.append_idempotent(&id, event(&profile, 2)).unwrap();
    assert_eq!(count(&audit), 2);
    audit.verify().unwrap();
}

#[test]
fn raw_framing_distinguishes_all_columns_storage_classes_and_byte_boundaries() {
    use rusqlite::types::ValueRef;
    let connection = rusqlite::Connection::open_in_memory().unwrap();
    connection.execute_batch("CREATE TABLE audit(seq INTEGER,prev_digest TEXT,digest TEXT,body BLOB,writer TEXT,minute INTEGER,size INTEGER,event_id TEXT)").unwrap();
    let empty = VerifiedImage::scan(&connection).unwrap();
    let baseline = [ValueRef::Null; 8];
    let mut original = empty.clone();
    original.append_row(baseline).unwrap();
    for column in 0..8 {
        for value in [
            ValueRef::Text(b""),
            ValueRef::Blob(b""),
            ValueRef::Integer(0),
            ValueRef::Real(0.0),
        ] {
            let mut fields = baseline;
            fields[column] = value;
            let mut changed = empty.clone();
            changed.append_row(fields).unwrap();
            assert!(!changed.matches(&original), "column {column}");
        }
    }
    let mut first = empty.clone();
    let mut second = empty;
    let mut fields = baseline;
    fields[0] = ValueRef::Text(b"a");
    fields[1] = ValueRef::Text(b"bc");
    first.append_row(fields).unwrap();
    fields[0] = ValueRef::Text(b"ab");
    fields[1] = ValueRef::Text(b"c");
    second.append_row(fields).unwrap();
    assert!(!first.matches(&second));
}
