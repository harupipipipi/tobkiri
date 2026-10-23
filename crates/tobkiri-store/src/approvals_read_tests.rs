//! Compiled SQL reuse must never retain an authority result or parameter.
use super::*;
use rusqlite::hooks::{AuthAction, AuthContext, Authorization};

fn fixture() -> (tempfile::TempDir, DataRoot, ProfileId, Approvals) {
    let temp = tempfile::tempdir().unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(temp.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
    }
    let root = DataRoot::open(temp.path().canonicalize().unwrap()).unwrap();
    let profile = ProfileId::parse("prof_fresh_reads").unwrap();
    let approvals = Approvals::open(&root, &profile).unwrap();
    (temp, root, profile, approvals)
}

#[test]
fn repeated_queries_observe_other_connection_revocation_epoch_and_fault() {
    let (_temp, root, profile, first) = fixture();
    let other = Approvals::open(&root, &profile).unwrap();
    let pack = PackId::parse("local.fresh-reads").unwrap();
    let hash = PackHash::from(Digest::hash(b"first revision"));
    let other_hash = PackHash::from(Digest::hash(b"another revision"));
    let allowed = PermissionSet {
        native: true,
        ..Default::default()
    };
    let denied = PermissionSet::default();
    assert_eq!(first.permissions(&profile, &pack, &hash).unwrap(), None);
    other
        .set_permissions(&profile, &pack, &hash, &allowed)
        .unwrap();
    assert_eq!(
        first.permissions(&profile, &pack, &hash).unwrap(),
        Some(allowed.clone())
    );
    assert_eq!(
        first.permissions(&profile, &pack, &other_hash).unwrap(),
        None
    );
    other
        .set_permissions(&profile, &pack, &hash, &denied)
        .unwrap();
    assert_eq!(
        first.permissions(&profile, &pack, &hash).unwrap(),
        Some(denied)
    );
    assert_eq!(first.security_epoch(&profile).unwrap(), 0);
    assert_eq!(other.advance_security_epoch(&profile).unwrap(), 1);
    assert_eq!(first.security_epoch(&profile).unwrap(), 1);
    assert_eq!(
        first.admission_status(&profile).unwrap(),
        AdmissionStatus::Allowed
    );
    other
        .latch_integrity(&profile, IntegrityFaultCode::AuthorityDatabase)
        .unwrap();
    assert_eq!(
        first.admission_status(&profile).unwrap(),
        AdmissionStatus::IntegrityFault {
            code: IntegrityFaultCode::AuthorityDatabase
        }
    );
}

#[test]
fn user_permission_reads_refresh_bytes_and_bind_every_query_parameter() {
    let (_temp, root, profile, first) = fixture();
    let other = Approvals::open(&root, &profile).unwrap();
    let profile_hash = Digest::hash(b"profile");
    let revision = Digest::hash(b"revision");
    let changed_revision = Digest::hash(b"different revision");
    let allowed = PermissionSet {
        audit_read: true,
        ..Default::default()
    };
    let denied = PermissionSet::default();
    other
        .set_user_permissions(&profile, &profile_hash, &revision, 0, &allowed)
        .unwrap();
    assert_eq!(
        first
            .user_permissions(&profile, &profile_hash, &revision, 0)
            .unwrap(),
        Some(allowed)
    );
    assert_eq!(
        first
            .user_permissions(&profile, &profile_hash, &revision, 1)
            .unwrap(),
        None
    );
    assert_eq!(
        first
            .user_permissions(&profile, &profile_hash, &changed_revision, 0)
            .unwrap(),
        None
    );
    assert_eq!(
        first
            .user_permissions(&profile, &Digest::hash(b"other profile"), &revision, 0)
            .unwrap(),
        None
    );
    other
        .set_user_permissions(&profile, &profile_hash, &revision, 0, &denied)
        .unwrap();
    assert_eq!(
        first
            .user_permissions(&profile, &profile_hash, &revision, 0)
            .unwrap(),
        Some(denied)
    );
}

#[test]
fn prepared_reads_cannot_bypass_changed_authorizer_or_malformed_authority() {
    let (_temp, _root, profile, approvals) = fixture();
    let pack = PackId::parse("local.fresh-reads").unwrap();
    let hash = PackHash::from(Digest::hash(b"revision"));
    let permissions = PermissionSet::default();
    approvals
        .set_permissions(&profile, &pack, &hash, &permissions)
        .unwrap();
    assert_eq!(
        approvals.permissions(&profile, &pack, &hash).unwrap(),
        Some(permissions.clone())
    );
    {
        let connection = approvals.connection.lock().unwrap();
        connection.authorizer(Some(|context: AuthContext<'_>| {
            if matches!(
                context.action,
                AuthAction::Read {
                    table_name: "permissions",
                    ..
                }
            ) {
                Authorization::Deny
            } else {
                Authorization::Allow
            }
        }));
    }
    assert!(approvals.permissions(&profile, &pack, &hash).is_err());
    {
        let connection = approvals.connection.lock().unwrap();
        connection.authorizer(None::<fn(AuthContext<'_>) -> Authorization>);
    }
    assert_eq!(
        approvals.permissions(&profile, &pack, &hash).unwrap(),
        Some(permissions)
    );
    assert_eq!(approvals.security_epoch(&profile).unwrap(), 0);
    {
        let connection = approvals.connection.lock().unwrap();
        connection
            .execute("UPDATE permissions SET body=?1", [b"not-json".as_slice()])
            .unwrap();
        connection
            .execute("UPDATE security_epoch SET epoch='broken'", [])
            .unwrap();
    }
    assert!(approvals.permissions(&profile, &pack, &hash).is_err());
    assert!(approvals.security_epoch(&profile).is_err());
    let wrong_profile = ProfileId::parse("prof_unrelated").unwrap();
    assert!(approvals.permissions(&wrong_profile, &pack, &hash).is_err());
    assert!(approvals.security_epoch(&wrong_profile).is_err());
}
