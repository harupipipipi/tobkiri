//! Exercise the actual Gate/Store path, including faults after SQL mutation.
use super::*;
use std::{
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
    time::{Duration, Instant},
};
use tobkiri_canon::ids::RequestId;
use tobkiri_gate::{
    ApprovalAnswer, ApprovalPrompt, CallCx, CoreCancellation, CoreControl, GateEngine,
    MediatorChain, PreparedPermissionApproval, Principal, ResourceLimits, TrustedPrompt,
};

struct Allow;
#[async_trait::async_trait]
impl TrustedPrompt for Allow {
    async fn prompt_approval(&self, _: ApprovalPrompt) -> ApprovalAnswer {
        ApprovalAnswer::Approve
    }
}

#[tokio::test]
async fn permission_batch_rolls_back_after_in_transaction_cancel_or_rejected_commit() {
    for cancel_during_mutation in [true, false] {
        let temp = tempfile::tempdir().unwrap();
        let root = DataRoot::open(temp.path().canonicalize().unwrap()).unwrap();
        let profile = ProfileId::parse("prof_permission_batch").unwrap();
        let approvals = Arc::new(Approvals::open(&root, &profile).unwrap());
        let audit =
            Arc::new(crate::AuditLog::open(&root, &profile, crate::AuditQuota::default()).unwrap());
        let control = CoreControl::default();
        let gate = GateEngine::new_with_control(
            approvals.clone(),
            audit,
            Arc::new(Allow),
            MediatorChain::default(),
            control.clone(),
        );
        let revision = Digest::hash(b"batch-plan");
        let cx = CallCx::try_root_with_control(
            profile.clone(),
            Principal::User,
            RequestId::parse("req_permission_batch").unwrap(),
            CoreCancellation::new_with_control(&control).unwrap(),
            Instant::now() + Duration::from_secs(30),
            ResourceLimits::default(),
            control.clone(),
        )
        .unwrap()
        .with_revision(revision.clone());
        let first_hash = Digest::hash(b"first-profile");
        let second_hash = Digest::hash(b"second-profile");
        let permissions = PermissionSet {
            audit_read: true,
            ..Default::default()
        };
        let before = control.usage().bytes;
        let first = gate
            .prepare_user_permissions(&cx, &first_hash, &revision, 1, &permissions)
            .await
            .unwrap();
        let second = gate
            .prepare_user_permissions(&cx, &second_hash, &revision, 1, &permissions)
            .await
            .unwrap();
        let mutations = Arc::new(AtomicUsize::new(0));
        let reject = Arc::new(AtomicBool::new(false));
        {
            let conn = approvals.connection.lock().unwrap();
            let observed = mutations.clone();
            let reject_after_mutation = reject.clone();
            let cancellation = cx.cancellation().clone();
            conn.update_hook(Some(move |_, _: &str, table: &str, _| {
                if table == "user_approvals" {
                    observed.fetch_add(1, Ordering::SeqCst);
                    if cancel_during_mutation {
                        cancellation.cancel();
                    } else {
                        reject_after_mutation.store(true, Ordering::SeqCst);
                    }
                }
            }))
            .unwrap();
            // Dispatch transactions precede mutation and must still succeed.
            // Reject precisely the later batch COMMIT, then allow ordinary
            // abandonment to retain terminal evidence after its rollback.
            conn.commit_hook(Some(move || reject.swap(false, Ordering::SeqCst)))
                .unwrap();
        }
        let error = PreparedPermissionApproval::commit_all(vec![first, second]).unwrap_err();
        assert!(matches!(
            (cancel_during_mutation, error),
            (true, GateError::Cancelled) | (false, GateError::Persistence(_))
        ));
        assert_eq!(
            mutations.load(Ordering::SeqCst),
            if cancel_during_mutation { 1 } else { 2 }
        );
        let conn = approvals.connection.lock().unwrap();
        for table in ["user_approvals", "permission_effects"] {
            let count: i64 = conn
                .query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |row| {
                    row.get(0)
                })
                .unwrap();
            assert_eq!(count, 0);
        }
        let mut statement = conn.prepare("SELECT body FROM terminal_outbox").unwrap();
        let mut rows = statement.query([]).unwrap();
        let mut terminals = 0;
        while let Some(row) = rows.next().unwrap() {
            let body: Vec<u8> = row.get(0).unwrap();
            let terminal: TerminalDecision = decode(&body).unwrap();
            assert_ne!(terminal.outcome, Outcome::Succeeded);
            terminals += 1;
        }
        assert_eq!(terminals, 2);
        assert_eq!(control.usage().bytes, before);
    }
}
