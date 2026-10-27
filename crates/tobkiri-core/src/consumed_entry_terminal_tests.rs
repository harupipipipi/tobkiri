use super::*;
use async_trait::async_trait;
use tobkiri_canon::{
    Digest,
    ids::{PackHash, PackId, ProfileId, RequestId},
};
use tobkiri_gate::{
    ApprovalAnswer, ApprovalPrompt, ApprovalStore, CancellationToken, CoreControl, EffectRequest,
    MediatorChain, PackPolicy, PermissionSet, Principal, ResourceLimits, TrustedPrompt,
};
use tobkiri_store::{Approvals, AuditLog, AuditQuota, DataRoot};

struct NoPrompt;
#[async_trait]
impl TrustedPrompt for NoPrompt {
    async fn prompt_approval(&self, _: ApprovalPrompt) -> ApprovalAnswer {
        panic!("the native Clock fixture requires no interactive approval")
    }
}

struct Fixture {
    _dir: tempfile::TempDir,
    approvals_path: std::path::PathBuf,
    audit_path: std::path::PathBuf,
    terminal: ConsumedEntryTerminal,
}
impl Fixture {
    async fn consumed() -> Self {
        let dir = tempfile::tempdir().unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(dir.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
        }
        let root = DataRoot::open(dir.path().canonicalize().unwrap()).unwrap();
        let profile = ProfileId::parse("prof_handoff").unwrap();
        let approvals = Arc::new(Approvals::open(&root, &profile).unwrap());
        let audit = Arc::new(AuditLog::open(&root, &profile, AuditQuota::default()).unwrap());
        let control = CoreControl::default();
        let gate = Arc::new(GateEngine::new_with_control(
            approvals.clone(),
            audit,
            Arc::new(NoPrompt),
            MediatorChain::default(),
            control.clone(),
        ));
        let pack = PackId::parse("local.handoff").unwrap();
        let hash = PackHash::new(Digest::hash(b"handoff-pack"));
        let limits = ResourceLimits {
            fuel_per_call: 100_000,
            memory_mib: 8,
            wall_ms: 60_000,
            table_elements: 1024,
            output_bytes: 1024,
        };
        let permissions = PermissionSet {
            wasm: limits.clone(),
            ..Default::default()
        };
        gate.register_pack(
            &profile,
            &pack,
            &hash,
            PackPolicy {
                declaration: permissions.clone(),
                ceiling: permissions.clone(),
                pack_limits: permissions.clone(),
            },
        )
        .unwrap();
        approvals
            .set_permissions(&profile, &pack, &hash, &permissions)
            .unwrap();
        let cx = CallCx::try_root_with_control(
            profile.clone(),
            Principal::Pack { pack, hash },
            RequestId::parse("req_handoff").unwrap(),
            CancellationToken::new_with_control(&control).unwrap(),
            Instant::now() + Duration::from_secs(60),
            limits,
            control,
        )
        .unwrap();
        let args = Digest::hash(b"handoff-clock");
        let grant = gate
            .authorize(&cx, EffectRequest::Clock, args.clone())
            .await
            .unwrap();
        let permit = grant.consume(&cx, &EffectRequest::Clock, &args).unwrap();
        let terminal = ConsumedEntryTerminal::new(permit, gate);
        assert!(terminal.permit().matches_context(&cx));
        terminal.watch(&cx).unwrap();
        Self {
            _dir: dir,
            approvals_path: root
                .path()
                .join(format!("profiles/{profile}/approvals.sqlite")),
            audit_path: root.path().join(format!("profiles/{profile}/audit.sqlite")),
            terminal,
        }
    }

    fn outcomes(&self) -> i64 {
        rusqlite::Connection::open(&self.audit_path)
            .unwrap()
            .query_row(
                "SELECT COUNT(*) FROM audit WHERE event_id IS NOT NULL",
                [],
                |row| row.get(0),
            )
            .unwrap()
    }
}

#[tokio::test]
async fn original_terminal_handoff_retries_only_writer_busy_without_holding_its_slot() {
    let mut f = Fixture::consumed().await;
    f.terminal.finish(Outcome::Succeeded).await.unwrap();
    assert_eq!(f.outcomes(), 1);
    let raw = rusqlite::Connection::open(&f.approvals_path).unwrap();
    raw.execute_batch("BEGIN IMMEDIATE").unwrap();
    let deadline = Instant::now() + NATIVE_CLEANUP_GRACE;
    let before = f.terminal.gate.control().usage().bytes;
    assert!(f.terminal.try_durable_handoff(deadline).unwrap().is_none());
    assert_eq!(f.terminal.gate.control().usage().bytes, before);
    let slot = tokio::sync::Mutex::new(Some(f.terminal));
    let (result, ()) = tokio::join!(observe_durable_handoff(&slot, deadline), async {
        tokio::time::sleep(Duration::from_millis(8)).await;
        // The observation releases its terminal, Store transaction and native
        // scratch before sleeping; the same real writer can now commit.
        assert!(slot.try_lock().is_ok());
        raw.execute_batch("ROLLBACK").unwrap();
    });
    assert_eq!(result.unwrap(), Outcome::Succeeded);
    f.terminal = slot.into_inner().unwrap();
    assert_eq!(f.terminal.proposal, Some(Outcome::Succeeded));
    assert_eq!(f.outcomes(), 1);
}

#[tokio::test]
async fn writer_busy_exhausts_original_deadline_without_restarting_or_rewriting() {
    let mut f = Fixture::consumed().await;
    f.terminal.finish(Outcome::Cancelled).await.unwrap();
    let raw = rusqlite::Connection::open(&f.approvals_path).unwrap();
    raw.execute_batch("BEGIN IMMEDIATE").unwrap();
    let deadline = Instant::now() + Duration::from_millis(20);
    let slot = tokio::sync::Mutex::new(Some(f.terminal));
    assert!(matches!(
        observe_durable_handoff(&slot, deadline).await,
        Err(GateError::Deadline)
    ));
    assert!(slot.try_lock().is_ok());
    raw.execute_batch("ROLLBACK").unwrap();
    // A later observer with the SAME deadline cannot turn timeout into proof.
    assert!(matches!(
        observe_durable_handoff(&slot, deadline).await,
        Err(GateError::Deadline)
    ));
    f.terminal = slot.into_inner().unwrap();
    assert_eq!(f.terminal.proposal, Some(Outcome::Cancelled));
    assert!(f.terminal.finished);
    assert_eq!(f.outcomes(), 1);
}

#[tokio::test]
async fn missing_durable_terminal_is_uncertain_without_busy_retry_or_new_write() {
    let f = Fixture::consumed().await;
    let slot = tokio::sync::Mutex::new(Some(f.terminal));
    assert!(matches!(
        observe_durable_handoff(&slot, Instant::now() + NATIVE_CLEANUP_GRACE).await,
        Err(GateError::Unavailable)
    ));
    let terminal = slot.into_inner().unwrap();
    assert!(!terminal.finished);
    assert!(terminal.proposal.is_none());
}

#[tokio::test]
async fn changed_consumed_binding_is_rejected_without_retrying_a_different_identity() {
    let f = Fixture::consumed().await;
    let raw = rusqlite::Connection::open(&f.approvals_path).unwrap();
    let id = f.terminal.permit().id().as_str();
    let body: Vec<u8> = raw
        .query_row("SELECT body FROM reservations WHERE id=?1", [id], |row| {
            row.get(0)
        })
        .unwrap();
    let mut reservation: tobkiri_gate::Reservation = serde_json::from_slice(&body).unwrap();
    reservation.args = Digest::hash(b"different-arguments");
    let value = tobkiri_canon::parse(
        &serde_json::to_vec(&reservation).unwrap(),
        tobkiri_canon::Variant::StrictV1,
    )
    .unwrap();
    let body = tobkiri_canon::encode(&value, tobkiri_canon::Variant::StrictV1).unwrap();
    raw.execute(
        "UPDATE reservations SET body=?1 WHERE id=?2",
        rusqlite::params![body, id],
    )
    .unwrap();
    let slot = tokio::sync::Mutex::new(Some(f.terminal));
    assert!(matches!(
        observe_durable_handoff(&slot, Instant::now() + NATIVE_CLEANUP_GRACE).await,
        Err(GateError::Denied(_))
    ));
    let terminal = slot.into_inner().unwrap();
    assert!(terminal.proposal.is_none());
    assert!(!terminal.finished);
}
