//! Real authority/SQLite tests. Metadata exclusion is not product GUI proof.
use super::*;
use crate::storage_test_fixture::{Fixture, sql};
use std::time::{Duration, Instant};
use tobkiri_gate::{DispatchPermit, EffectRequest, Gate, StoreOperation, StoreTier};

impl Fixture {
    async fn consumed(&self) -> (DispatchPermit, Reservation) {
        let _actual_store = self.store().await;
        let request = EffectRequest::Storage {
            profile: self.profile.clone(),
            owner: self.pack.clone(),
            store: "records".into(),
            tier: StoreTier::Private,
            operation: StoreOperation::SqlWrite,
        };
        let args = Digest::hash(b"metadata exclusion fixture only");
        let grant = self
            .gate
            .authorize(&self.cx, request.clone(), args.clone())
            .await
            .unwrap();
        let permit = grant.consume(&self.cx, &request, &args).unwrap();
        let connection = self.approvals.connection.lock().unwrap();
        let body: Vec<u8> = connection
            .query_row(
                "SELECT body FROM reservations WHERE id=?1 AND state='consumed'",
                [permit.id().as_str()],
                |r| r.get(0),
            )
            .unwrap();
        let expected = validated_reservation(&self.profile, permit.id().as_str(), &body).unwrap();
        (permit, expected)
    }
}

#[tokio::test]
async fn actual_writer_contention_refuses_before_callback_and_restores_busy_policy() {
    let f = Fixture::new().await;
    let (_permit, expected) = f.consumed().await;
    let (path, before): (String, u64) = {
        let conn = f.approvals.connection.lock().unwrap();
        (
            conn.pragma_query_value(None, "database_list", |r| r.get(2))
                .unwrap(),
            conn.pragma_query_value(None, "busy_timeout", |r| crate::sql_u64(r, 0))
                .unwrap(),
        )
    };
    let mut writer = rusqlite::Connection::open(path).unwrap();
    writer.busy_timeout(Duration::ZERO).unwrap();
    let writer_tx = writer
        .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
        .unwrap();
    let mut called = false;
    assert!(
        f.approvals
            .with_storage_commit_fence(&expected, &mut || {
                called = true;
                Ok(())
            })
            .is_err()
    );
    assert!(!called);
    assert!(!f.approvals.connection_fault.load(Ordering::Acquire));
    let after: u64 = f
        .approvals
        .connection
        .lock()
        .unwrap()
        .pragma_query_value(None, "busy_timeout", |r| crate::sql_u64(r, 0))
        .unwrap();
    assert_eq!(before, after);
    writer_tx.rollback().unwrap();
    f.approvals
        .with_storage_commit_fence(&expected, &mut || {
            called = true;
            Ok(())
        })
        .unwrap();
    assert!(called);
}

#[tokio::test]
async fn authority_teardown_failure_after_actual_data_commit_is_unknown_and_quarantined() {
    use rusqlite::hooks::{AuthAction, AuthContext, Authorization, TransactionOperation};
    let f = Fixture::new().await;
    let handle = f.store().await;
    sql(
        &handle,
        &f.cx,
        f.gate.clone(),
        "CREATE TABLE payload(value INTEGER NOT NULL)",
        false,
    )
    .await
    .unwrap();
    f.approvals
        .connection
        .lock()
        .unwrap()
        .authorizer(Some(|cx: AuthContext<'_>| match cx.action {
            AuthAction::Transaction {
                operation: TransactionOperation::Rollback,
            } => Authorization::Deny,
            _ => Authorization::Allow,
        }))
        .unwrap();
    let result = sql(
        &handle,
        &f.cx,
        f.gate.clone(),
        "INSERT INTO payload(value) VALUES(1)",
        false,
    )
    .await;
    assert!(matches!(result, Err(StoreError::CommitUnknown)));
    assert!(f.approvals.connection_fault.load(Ordering::Acquire));
    assert!(matches!(
        f.approvals.admission_status(&f.profile),
        Err(GateError::Unavailable)
    ));
    // Test-only physical recovery releases the failed authority transaction.
    // It deliberately does not clear the original instance's quarantine.
    {
        let conn = f.approvals.connection.lock().unwrap();
        conn.authorizer(None::<fn(AuthContext<'_>) -> Authorization>)
            .unwrap();
        if !conn.is_autocommit() {
            conn.execute_batch("ROLLBACK").unwrap();
        }
    }
    assert!(f.approvals.security_epoch(&f.profile).is_err());
    let rows = sql(
        &handle,
        &f.cx,
        f.recovered_gate(),
        "SELECT count(*) FROM payload",
        true,
    )
    .await
    .unwrap();
    assert_eq!(
        rows.value()
            .get_key("rows")
            .unwrap()
            .get_index(0)
            .unwrap()
            .get_index(0)
            .unwrap()
            .get_key("value")
            .unwrap()
            .as_str(),
        Some("1"),
        "authority teardown must not erase a confirmed physical DATA write"
    );
}

struct ChildOwner(std::process::Child);
impl Drop for ChildOwner {
    fn drop(&mut self) {
        if !matches!(self.0.try_wait(), Ok(Some(_))) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
}

/// Only a cfg(test) metadata-exclusion harness. Markers are scheduling, never
/// authority evidence. No production callback or test hook is introduced.
#[test]
fn storage_fence_child_epoch_holder() {
    let Some(root) = std::env::var_os("TOBKIRI_STORAGE_FENCE_TEST_ROOT") else {
        return;
    };
    let root = DataRoot::open(std::path::PathBuf::from(root)).unwrap();
    let profile = ProfileId::parse("prof_storage_fence").unwrap();
    let approvals = Approvals::open(&root, &profile).unwrap();
    let id = std::env::var("TOBKIRI_STORAGE_FENCE_TEST_GRANT").unwrap();
    let directory =
        std::path::PathBuf::from(std::env::var_os("TOBKIRI_STORAGE_FENCE_TEST_SIGNALS").unwrap());
    let expected = {
        let connection = approvals.connection.lock().unwrap();
        let body: Vec<u8> = connection
            .query_row(
                "SELECT body FROM reservations WHERE id=?1 AND state='consumed'",
                [&id],
                |r| r.get(0),
            )
            .unwrap();
        validated_reservation(&profile, &id, &body).unwrap()
    };
    approvals
        .with_storage_commit_fence(&expected, &mut || {
            // Test-only bounded authority-fence hold, without a DATA commit.
            // Never used by Store's private production DATA adapter.
            std::fs::write(directory.join("ready"), b"ready").map_err(persistence)?;
            let end = Instant::now() + Duration::from_secs(5);
            while !directory.join("release").exists() {
                if Instant::now() >= end
                    || now_ms().map_err(persistence)? >= expected.expires_unix_ms
                {
                    return Err(GateError::Deadline);
                }
                std::thread::sleep(Duration::from_millis(2));
            }
            Ok(())
        })
        .unwrap();
}

#[tokio::test]
async fn actual_fence_excludes_epoch_writer_in_another_process_until_release() {
    let f = Fixture::new().await;
    let (permit, _expected) = f.consumed().await;
    let signals = tempfile::tempdir().unwrap();
    let mut child = ChildOwner(
        std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "approvals::storage_commit::tests::storage_fence_child_epoch_holder",
                "--nocapture",
                "--test-threads=1",
            ])
            .env(
                "TOBKIRI_STORAGE_FENCE_TEST_ROOT",
                f.temp.path().canonicalize().unwrap(),
            )
            .env("TOBKIRI_STORAGE_FENCE_TEST_GRANT", permit.id().as_str())
            .env("TOBKIRI_STORAGE_FENCE_TEST_SIGNALS", signals.path())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::inherit())
            .spawn()
            .unwrap(),
    );
    let end = f.cx.deadline().min(Instant::now() + Duration::from_secs(5));
    while !signals.path().join("ready").exists() {
        assert!(
            Instant::now() < end,
            "child did not enter the real fence before deadline"
        );
        assert!(
            child.0.try_wait().unwrap().is_none(),
            "child exited before acquiring the fence"
        );
        std::thread::sleep(Duration::from_millis(2));
    }
    let before_busy: u64 = {
        let conn = f.approvals.connection.lock().unwrap();
        let before = conn
            .pragma_query_value(None, "busy_timeout", |r| crate::sql_u64(r, 0))
            .unwrap();
        conn.busy_timeout(Duration::ZERO).unwrap();
        before
    };
    assert_eq!(f.gate.epoch(&f.profile).unwrap(), 0);
    let error = f
        .gate
        .revoke_epoch(&f.profile)
        .expect_err("epoch advanced while another process held the actual fence");
    assert!(
        matches!(error, GateError::Persistence(ref message) if message.contains("locked") || message.contains("busy")),
        "unexpected writer refusal: {error}"
    );
    assert_eq!(f.gate.epoch(&f.profile).unwrap(), 0);
    std::fs::write(signals.path().join("release"), b"release").unwrap();
    let end = f.cx.deadline().min(Instant::now() + Duration::from_secs(5));
    let status = loop {
        if let Some(status) = child.0.try_wait().unwrap() {
            break status;
        }
        assert!(Instant::now() < end, "child did not release the fence");
        std::thread::sleep(Duration::from_millis(2));
    };
    assert!(status.success(), "child fence failed: {status}");
    assert_eq!(f.gate.revoke_epoch(&f.profile).unwrap(), 1);
    f.approvals
        .connection
        .lock()
        .unwrap()
        .busy_timeout(Duration::from_millis(before_busy))
        .unwrap();
    f.gate
        .record_outcome(
            permit.id().clone(),
            tobkiri_gate::Outcome::Failed {
                code: "metadata-exclusion-test-only".into(),
            },
        )
        .await
        .unwrap();
}
