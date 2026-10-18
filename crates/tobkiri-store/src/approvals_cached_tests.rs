//! Real disposable Approvals connections: statements may be cached, values may not.
#![cfg(any(
    windows,
    all(target_os = "macos", target_arch = "aarch64"),
    all(target_os = "linux", target_arch = "x86_64", target_env = "gnu")
))]

use super::*;
use rusqlite::hooks::{AuthAction, AuthContext, Authorization};

struct Fixture {
    reader: Approvals,
    writer: Approvals,
    _root: DataRoot,
    profile: ProfileId,
    // Drop both actual SQLite connections and the root before deleting files.
    _temp: tempfile::TempDir,
}

impl Fixture {
    fn new() -> Self {
        let temp = tempfile::tempdir().unwrap();
        let root = DataRoot::open(temp.path().canonicalize().unwrap().join("data")).unwrap();
        let profile = ProfileId::parse("prof_cached_selects").unwrap();
        let reader = Approvals::open(&root, &profile).unwrap();
        let writer = Approvals::open(&root, &profile).unwrap();
        Self {
            reader,
            writer,
            _root: root,
            profile,
            _temp: temp,
        }
    }
}

fn permission(memory_mib: u64) -> PermissionSet {
    let mut value = PermissionSet::default();
    value.wasm.memory_mib = memory_mib;
    value
}

#[test]
fn cached_fault_and_epoch_reads_see_other_connection_commits() {
    let f = Fixture::new();
    for _ in 0..2 {
        assert_eq!(
            f.reader.admission_status(&f.profile).unwrap(),
            AdmissionStatus::Allowed
        );
        assert_eq!(f.reader.security_epoch(&f.profile).unwrap(), 0);
    }
    assert_eq!(f.writer.advance_security_epoch(&f.profile).unwrap(), 1);
    assert_eq!(f.reader.security_epoch(&f.profile).unwrap(), 1);
    f.writer
        .latch_integrity(&f.profile, IntegrityFaultCode::AuditChain)
        .unwrap();
    assert_eq!(
        f.reader.admission_status(&f.profile).unwrap(),
        AdmissionStatus::IntegrityFault {
            code: IntegrityFaultCode::AuditChain
        }
    );
}

#[test]
fn cached_pack_reads_observe_none_insert_reduce_and_every_key() {
    let f = Fixture::new();
    let first = PackId::parse("local.cache-first").unwrap();
    let second = PackId::parse("local.cache-second").unwrap();
    let hash_a = PackHash::new(Digest::hash(b"cache-a"));
    let hash_b = PackHash::new(Digest::hash(b"cache-b"));
    for _ in 0..2 {
        assert_eq!(
            f.reader.permissions(&f.profile, &first, &hash_a).unwrap(),
            None
        );
    }
    f.writer
        .set_permissions(&f.profile, &first, &hash_a, &permission(16))
        .unwrap();
    assert_eq!(
        f.reader.permissions(&f.profile, &first, &hash_a).unwrap(),
        Some(permission(16))
    );
    f.writer
        .set_permissions(&f.profile, &first, &hash_a, &permission(8))
        .unwrap();
    assert_eq!(
        f.reader.permissions(&f.profile, &first, &hash_a).unwrap(),
        Some(permission(8))
    );
    assert_eq!(
        f.reader.permissions(&f.profile, &second, &hash_a).unwrap(),
        None
    );
    assert_eq!(
        f.reader.permissions(&f.profile, &first, &hash_b).unwrap(),
        None
    );
    f.writer
        .set_permissions(&f.profile, &second, &hash_a, &permission(32))
        .unwrap();
    f.writer
        .set_permissions(&f.profile, &first, &hash_b, &permission(4))
        .unwrap();
    for (pack, hash, expected) in [
        (&second, &hash_a, 32),
        (&first, &hash_b, 4),
        (&first, &hash_a, 8),
        (&second, &hash_b, 0),
    ] {
        assert_eq!(
            f.reader.permissions(&f.profile, pack, hash).unwrap(),
            if expected == 0 {
                None
            } else {
                Some(permission(expected))
            }
        );
    }
}

#[test]
fn cached_user_reads_observe_none_insert_reduce_and_full_document_key() {
    let f = Fixture::new();
    let document_a = Digest::hash(b"document-a");
    let document_b = Digest::hash(b"document-b");
    let revision_a = Digest::hash(b"revision-a");
    let revision_b = Digest::hash(b"revision-b");
    for _ in 0..2 {
        assert_eq!(
            f.reader
                .user_permissions(&f.profile, &document_a, &revision_a, 0)
                .unwrap(),
            None
        );
    }
    f.writer
        .set_user_permissions(&f.profile, &document_a, &revision_a, 0, &permission(16))
        .unwrap();
    assert_eq!(
        f.reader
            .user_permissions(&f.profile, &document_a, &revision_a, 0)
            .unwrap(),
        Some(permission(16))
    );
    f.writer
        .set_user_permissions(&f.profile, &document_a, &revision_a, 0, &permission(8))
        .unwrap();
    assert_eq!(
        f.reader
            .user_permissions(&f.profile, &document_a, &revision_a, 0)
            .unwrap(),
        Some(permission(8))
    );
    for (document, revision, epoch, expected) in [
        (&document_b, &revision_a, 0, 2),
        (&document_a, &revision_b, 0, 4),
        (&document_a, &revision_a, 1, 32),
    ] {
        assert_eq!(
            f.reader
                .user_permissions(&f.profile, document, revision, epoch)
                .unwrap(),
            None
        );
        f.writer
            .set_user_permissions(&f.profile, document, revision, epoch, &permission(expected))
            .unwrap();
        assert_eq!(
            f.reader
                .user_permissions(&f.profile, document, revision, epoch)
                .unwrap(),
            Some(permission(expected))
        );
    }
    assert_eq!(
        f.reader
            .user_permissions(&f.profile, &document_a, &revision_a, 0)
            .unwrap(),
        Some(permission(8))
    );
}

#[test]
fn cached_permission_reads_still_strictly_decode_after_a_valid_read() {
    let f = Fixture::new();
    let pack = PackId::parse("local.cache-body").unwrap();
    let hash = PackHash::new(Digest::hash(b"body-hash"));
    let document = Digest::hash(b"body-document");
    let revision = Digest::hash(b"body-revision");
    f.writer
        .set_permissions(&f.profile, &pack, &hash, &permission(16))
        .unwrap();
    f.writer
        .set_user_permissions(&f.profile, &document, &revision, 0, &permission(8))
        .unwrap();
    assert_eq!(
        f.reader.permissions(&f.profile, &pack, &hash).unwrap(),
        Some(permission(16))
    );
    assert_eq!(
        f.reader
            .user_permissions(&f.profile, &document, &revision, 0)
            .unwrap(),
        Some(permission(8))
    );
    {
        // Corruption injection is confined to this disposable unit fixture.
        let connection = f.writer.connection.lock().unwrap();
        connection
            .execute(
                "UPDATE permissions SET body=?1 WHERE pack=?2 AND hash=?3",
                params![b"{\"wasm\":".as_slice(), pack.as_str(), hash.as_str()],
            )
            .unwrap();
        connection.execute(
            "UPDATE user_approvals SET body=?1 WHERE profile_hash=?2 AND revision=?3 AND epoch=0",
            params![b"{\"wasm\":0,\"wasm\":0}".as_slice(), document.as_str(), revision.as_str()],
        ).unwrap();
    }
    assert!(matches!(
        f.reader.permissions(&f.profile, &pack, &hash),
        Err(GateError::Persistence(_))
    ));
    assert!(matches!(
        f.reader
            .user_permissions(&f.profile, &document, &revision, 0),
        Err(GateError::Persistence(_))
    ));
}

#[test]
fn cached_epoch_read_keeps_invalid_integer_and_missing_row_fail_closed() {
    let f = Fixture::new();
    assert_eq!(f.reader.security_epoch(&f.profile).unwrap(), 0);
    {
        let connection = f.writer.connection.lock().unwrap();
        connection
            .execute("UPDATE security_epoch SET epoch=-1 WHERE singleton=1", [])
            .unwrap();
    }
    assert!(matches!(
        f.reader.security_epoch(&f.profile),
        Err(GateError::Persistence(_))
    ));
    {
        let connection = f.writer.connection.lock().unwrap();
        connection
            .execute("DELETE FROM security_epoch WHERE singleton=1", [])
            .unwrap();
    }
    assert!(matches!(
        f.reader.security_epoch(&f.profile),
        Err(GateError::Persistence(_))
    ));
}

#[test]
fn cached_queries_respect_an_authorizer_change_and_parameter_error_order() {
    let f = Fixture::new();
    let pack = PackId::parse("local.cache-authorizer").unwrap();
    let hash = PackHash::new(Digest::hash(b"authorizer-hash"));
    let document = Digest::hash(b"authorizer-document");
    let revision = Digest::hash(b"authorizer-revision");
    // Warm all four fixed statements before installing a denying callback.
    assert_eq!(
        f.reader.admission_status(&f.profile).unwrap(),
        AdmissionStatus::Allowed
    );
    assert_eq!(f.reader.security_epoch(&f.profile).unwrap(), 0);
    assert_eq!(
        f.reader.permissions(&f.profile, &pack, &hash).unwrap(),
        None
    );
    assert_eq!(
        f.reader
            .user_permissions(&f.profile, &document, &revision, 0)
            .unwrap(),
        None
    );
    f.reader
        .connection
        .lock()
        .unwrap()
        .authorizer(Some(|context: AuthContext<'_>| match context.action {
            AuthAction::Read { .. } => Authorization::Deny,
            _ => Authorization::Allow,
        }))
        .unwrap();
    // This conversion preceded prepare before the optimization and still does.
    let invalid_epoch = f
        .reader
        .user_permissions(&f.profile, &document, &revision, u64::MAX);
    assert!(matches!(invalid_epoch, Err(GateError::Persistence(message))
        if message == i64::try_from(u64::MAX).unwrap_err().to_string()));
    assert!(matches!(
        f.reader.admission_status(&f.profile),
        Err(GateError::Persistence(_))
    ));
    assert!(matches!(
        f.reader.security_epoch(&f.profile),
        Err(GateError::Persistence(_))
    ));
    assert!(matches!(
        f.reader.permissions(&f.profile, &pack, &hash),
        Err(GateError::Persistence(_))
    ));
    assert!(matches!(
        f.reader
            .user_permissions(&f.profile, &document, &revision, 0),
        Err(GateError::Persistence(_))
    ));
    f.reader
        .connection
        .lock()
        .unwrap()
        .authorizer(None::<fn(AuthContext<'_>) -> Authorization>)
        .unwrap();
    assert_eq!(
        f.reader.admission_status(&f.profile).unwrap(),
        AdmissionStatus::Allowed
    );
    assert_eq!(f.reader.security_epoch(&f.profile).unwrap(), 0);
    assert_eq!(
        f.reader.permissions(&f.profile, &pack, &hash).unwrap(),
        None
    );
    assert_eq!(
        f.reader
            .user_permissions(&f.profile, &document, &revision, 0)
            .unwrap(),
        None
    );
}

#[test]
fn cached_queries_do_not_reuse_authority_for_another_profile() {
    let f = Fixture::new();
    let other = ProfileId::parse("prof_not_cached_selects").unwrap();
    let pack = PackId::parse("local.cache-profile").unwrap();
    let hash = PackHash::new(Digest::hash(b"profile-hash"));
    let document = Digest::hash(b"profile-document");
    let revision = Digest::hash(b"profile-revision");
    f.reader.admission_status(&f.profile).unwrap();
    f.reader.security_epoch(&f.profile).unwrap();
    f.reader.permissions(&f.profile, &pack, &hash).unwrap();
    f.reader
        .user_permissions(&f.profile, &document, &revision, 0)
        .unwrap();
    assert!(matches!(
        f.reader.admission_status(&other),
        Err(GateError::Denied(_))
    ));
    assert!(matches!(
        f.reader.security_epoch(&other),
        Err(GateError::Denied(_))
    ));
    assert!(matches!(
        f.reader.permissions(&other, &pack, &hash),
        Err(GateError::Denied(_))
    ));
    assert!(matches!(
        f.reader.user_permissions(&other, &document, &revision, 0),
        Err(GateError::Denied(_))
    ));
}
