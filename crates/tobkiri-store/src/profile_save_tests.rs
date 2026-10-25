//! Real SQLite/descriptor failures; no synthetic success or guest authority.
use super::*;
use std::{
    sync::Arc,
    time::{Duration, Instant},
};
use tobkiri_canon::ids::RequestId;
use tobkiri_gate::{
    ApprovalAnswer, ApprovalPrompt, CallCx, CoreCancellation, GateEngine, MediatorChain,
    PackPolicy, PreparedPermissionApproval, Principal, ResourceLimits, TrustedPrompt,
};

struct Allow;
#[async_trait::async_trait]
impl TrustedPrompt for Allow {
    async fn prompt_approval(&self, _: ApprovalPrompt) -> ApprovalAnswer {
        ApprovalAnswer::Approve
    }
}
struct Fixture {
    _temp: tempfile::TempDir,
    root: DataRoot,
    profile: ProfileId,
    approvals: Arc<Approvals>,
    gate: GateEngine,
    cx: CallCx,
    control: CoreControl,
    revision: Digest,
    plan: Digest,
}
impl Fixture {
    fn register_pack(&self, pack: &PackId, hash: &PackHash) {
        let permissions = PermissionSet {
            audit_append: true,
            ..Default::default()
        };
        self.gate
            .register_pack(
                &self.profile,
                pack,
                hash,
                PackPolicy {
                    declaration: permissions.clone(),
                    ceiling: permissions.clone(),
                    pack_limits: permissions,
                },
            )
            .unwrap();
    }
    fn new() -> Self {
        let temp = tempfile::tempdir().unwrap();
        let root = DataRoot::open(temp.path().canonicalize().unwrap()).unwrap();
        let profile = ProfileId::parse("prof_coordinated_save").unwrap();
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
        let plan = Digest::hash(b"reviewed-plan");
        let cx = CallCx::try_root_with_control(
            profile.clone(),
            Principal::User,
            RequestId::parse("req_coordinated_save").unwrap(),
            CoreCancellation::new_with_control(&control).unwrap(),
            Instant::now() + Duration::from_secs(30),
            ResourceLimits::default(),
            control.clone(),
        )
        .unwrap()
        .with_revision(plan.clone());
        let bytes = b"immutable reviewed native candidate";
        let revision = Digest::hash(bytes);
        root.write_profile_file_atomic(
            &profile,
            &format!("revisions/{}.json", revision.hex()),
            bytes,
        )
        .unwrap();
        Self {
            _temp: temp,
            root,
            profile,
            approvals,
            gate,
            cx,
            control,
            revision,
            plan,
        }
    }
    async fn prepare(&self) -> PreparedPermissionApproval<'_> {
        self.gate
            .prepare_user_permissions(
                &self.cx,
                &self.revision,
                &self.plan,
                1,
                &PermissionSet {
                    audit_read: true,
                    ..Default::default()
                },
            )
            .await
            .unwrap()
    }
    fn counts(&self) -> (i64, i64) {
        let conn = self.approvals.connection.lock().unwrap();
        (
            conn.query_row("SELECT COUNT(*) FROM permission_effects", [], |row| {
                row.get(0)
            })
            .unwrap(),
            conn.query_row("SELECT COUNT(*) FROM profile_save_decisions", [], |row| {
                row.get(0)
            })
            .unwrap(),
        )
    }
}

#[tokio::test]
async fn pending_decision_fences_new_authority_but_allows_cleanup_and_revocation_then_recovers_exactly()
 {
    let f = Fixture::new();
    let unrelated = PackId::parse("local.independent-approval").unwrap();
    let unrelated_hash = PackHash::from(Digest::hash(b"independent"));
    f.register_pack(&unrelated, &unrelated_hash);
    // Same request/context, but a different ordinary operation. It must not be
    // adopted into this save merely by scanning matching request identities.
    f.gate
        .approve_pack_permissions(&f.cx, &unrelated, None, &unrelated_hash)
        .await
        .unwrap();
    // A real consumed Store reservation predates the save. This exercises
    // metadata cleanup, not physical backend execution or a guest permit.
    let reservation = f
        .approvals
        .reserve_fresh(
            &ReservationRequest::fresh(
                &f.cx,
                Digest::hash(b"cleanup-effect"),
                Digest::hash(b"cleanup-args"),
                now_ms().unwrap() + 30_000,
            )
            .unwrap(),
        )
        .unwrap();
    f.approvals.dispatch(&reservation.id).unwrap();
    let consumed = f
        .approvals
        .consume_with_deadline(&reservation, reservation.expires_unix_ms)
        .unwrap();
    let prepared = f.prepare().await;
    let error: StoreError = PreparedPermissionApproval::commit_all_with(vec![prepared], |batch| {
        f.root.commit_prepared_profile_inner(
            batch,
            None,
            &f.revision,
            || {
                Err(StoreError::Io(std::io::Error::other(
                    "injected after decision",
                )))
            },
            std::fs::File::sync_all,
        )?;
        Ok(())
    })
    .unwrap_err();
    let operation = error.profile_save_operation().unwrap().clone();
    assert_eq!(
        error.profile_commit_progress(),
        Some(crate::ProfileCommitProgress::EpochAdvanced)
    );
    assert_eq!(f.counts(), (2, 1));
    assert_eq!(
        f.approvals
            .connection
            .lock()
            .unwrap()
            .query_row(
                "SELECT COUNT(*) FROM profile_save_members WHERE operation=?1",
                [operation.as_str()],
                |row| row.get::<_, i64>(0)
            )
            .unwrap(),
        1
    );
    assert!(f.approvals.admission_status(&f.profile).is_err());
    assert!(
        f.approvals
            .permissions(
                &f.profile,
                &PackId::parse("local.unrelated").unwrap(),
                &PackHash::from(Digest::hash(b"unrelated"))
            )
            .is_err()
    );
    assert!(
        f.approvals
            .user_permissions(&f.profile, &f.revision, &f.plan, 1)
            .is_err()
    );
    assert!(
        f.approvals
            .set_user_permissions(
                &f.profile,
                &f.revision,
                &f.plan,
                1,
                &PermissionSet::default()
            )
            .is_err()
    );
    assert!(
        f.root
            .compare_and_swap_profile_head(&f.profile, None, &Digest::hash(b"other head"))
            .is_err()
    );
    assert!(f.root.profile_head_snapshot(&f.profile).is_err());
    assert!(
        f.root
            .profile_save_outcome(&f.profile, &operation, &f.control)
            .unwrap()
            .is_none()
    );
    assert_eq!(f.approvals.advance_security_epoch(&f.profile).unwrap(), 2);
    let mut observed = false;
    f.approvals
        .with_cleanup_snapshot(&consumed, &mut |snapshot| {
            assert_eq!(snapshot.current_epoch, 2);
            observed = true;
            Ok(())
        })
        .unwrap();
    assert!(observed);
    let terminal = f
        .approvals
        .finish_terminal(&consumed.id, &Outcome::Cancelled)
        .unwrap();
    assert_eq!(terminal.decision.outcome, Outcome::Cancelled);
    let recovered = f
        .root
        .recover_profile_save(&f.profile, &f.control)
        .unwrap()
        .unwrap();
    assert_eq!(recovered.operation(), &operation);
    assert_eq!(recovered.revision(), &f.revision);
    assert_eq!(recovered.epoch(), 1); // acquired epoch, not today's epoch
    assert_eq!(
        f.root.profile_head_snapshot(&f.profile).unwrap(),
        (f.revision.clone(), 2)
    );
    assert_eq!(f.counts(), (2, 1));
    assert!(
        f.root
            .recover_profile_save(&f.profile, &f.control)
            .unwrap()
            .is_none()
    );
    assert_eq!(
        f.root
            .profile_save_outcome(&f.profile, &operation, &f.control)
            .unwrap()
            .unwrap()
            .operation(),
        &operation
    );
    assert_eq!(
        f.approvals.admission_status(&f.profile).unwrap(),
        AdmissionStatus::Allowed
    );
}

#[tokio::test]
async fn postrename_directory_failure_retains_decision_and_recovery_does_not_replay_effects() {
    let f = Fixture::new();
    let error: StoreError =
        PreparedPermissionApproval::commit_all_with(vec![f.prepare().await], |batch| {
            f.root.commit_prepared_profile_inner(
                batch,
                None,
                &f.revision,
                || Ok(()),
                |_| Err(std::io::Error::other("injected directory durability")),
            )?;
            Ok(())
        })
        .unwrap_err();
    assert_eq!(
        error.profile_commit_progress(),
        Some(crate::ProfileCommitProgress::HeadReplaced)
    );
    let operation = error.profile_save_operation().unwrap().clone();
    assert_eq!(
        f.root.read_profile_file(&f.profile, "head", 128).unwrap(),
        f.revision.as_str().as_bytes()
    );
    assert!(f.approvals.admission_status(&f.profile).is_err());
    let recovered = f
        .root
        .recover_profile_save(&f.profile, &f.control)
        .unwrap()
        .unwrap();
    assert_eq!(recovered.operation(), &operation);
    assert_eq!(f.counts(), (1, 1));
    assert_eq!(
        f.root.profile_head_snapshot(&f.profile).unwrap(),
        (f.revision.clone(), 1)
    );
}

#[tokio::test]
async fn decision_commit_rejection_keeps_exact_uncertainty_identity_without_publishing_effects() {
    use std::sync::atomic::{AtomicBool, Ordering};
    let f = Fixture::new();
    let reject = Arc::new(AtomicBool::new(false));
    {
        let conn = f.approvals.connection.lock().unwrap();
        let flag = reject.clone();
        conn.update_hook(Some(move |_, _: &str, table: &str, _| {
            if table == "profile_save_decisions" {
                flag.store(true, Ordering::SeqCst);
            }
        }))
        .unwrap();
        conn.commit_hook(Some(move || reject.swap(false, Ordering::SeqCst)))
            .unwrap();
    }
    let prepared = f.prepare().await;
    let _lock = f.root.profile_lock(&f.profile, ".revision.lock").unwrap();
    let error: StoreError = PreparedPermissionApproval::commit_all_with(vec![prepared], |batch| {
        let _retained = batch.reserve_metadata(BOOKKEEPING_BYTES)?;
        f.approvals.decide_profile_save(batch, None, &f.revision)?;
        Ok(())
    })
    .unwrap_err();
    assert_eq!(
        error.profile_commit_progress(),
        Some(crate::ProfileCommitProgress::EpochCommitAttempted)
    );
    assert!(error.profile_save_operation().is_some());
    assert_eq!(f.counts(), (0, 0));
    assert_eq!(f.approvals.security_epoch(&f.profile).unwrap(), 0);
    assert!(
        f.approvals
            .user_permissions(&f.profile, &f.revision, &f.plan, 1)
            .unwrap()
            .is_none()
    );
    // The injected COMMIT hook proves rejection in this test; the general
    // COMMIT error itself still reports uncertainty, never rollback success.
}

#[tokio::test]
async fn cancellation_during_decision_insertion_rolls_back_epoch_permissions_members_and_outcome() {
    let f = Fixture::new();
    let prepared = f.prepare().await;
    {
        let cancellation = f.cx.cancellation().clone();
        f.approvals
            .connection
            .lock()
            .unwrap()
            .update_hook(Some(move |_, _: &str, table: &str, _| {
                if table == "profile_save_decisions" {
                    cancellation.cancel();
                }
            }))
            .unwrap();
    }
    let _lock = f.root.profile_lock(&f.profile, ".revision.lock").unwrap();
    let error: StoreError = PreparedPermissionApproval::commit_all_with(vec![prepared], |batch| {
        let _retained = batch.reserve_metadata(BOOKKEEPING_BYTES)?;
        f.approvals.decide_profile_save(batch, None, &f.revision)?;
        Ok(())
    })
    .unwrap_err();
    assert!(matches!(error, StoreError::Gate(GateError::Cancelled)));
    assert_eq!(f.counts(), (0, 0));
    assert_eq!(f.approvals.security_epoch(&f.profile).unwrap(), 0);
    assert_eq!(
        f.approvals
            .connection
            .lock()
            .unwrap()
            .query_row("SELECT COUNT(*) FROM profile_save_members", [], |row| row
                .get::<_, i64>(
                0
            ))
            .unwrap(),
        0
    );
}

#[tokio::test]
async fn recovery_rejects_missing_pack_effect_terminal_or_membership_before_releasing_the_head() {
    for corruption in 0..4 {
        let f = Fixture::new();
        let pack = PackId::parse("local.recorded-member").unwrap();
        let hash = PackHash::from(Digest::hash(b"recorded-pack"));
        f.register_pack(&pack, &hash);
        let pack_approval = f
            .gate
            .prepare_pack_permissions(&f.cx, &pack, None, &hash)
            .await
            .unwrap();
        let error: StoreError = PreparedPermissionApproval::commit_all_with(
            vec![pack_approval, f.prepare().await],
            |batch| {
                f.root.commit_prepared_profile_inner(
                    batch,
                    None,
                    &f.revision,
                    || {
                        Err(StoreError::Io(std::io::Error::other(
                            "injected after decision",
                        )))
                    },
                    std::fs::File::sync_all,
                )?;
                Ok(())
            },
        )
        .unwrap_err();
        let operation = error.profile_save_operation().unwrap().clone();
        {
            // Deliberate corruption only in this disposable native test DB.
            let conn = f.approvals.connection.lock().unwrap();
            let pack_grant: String = conn
                .query_row(
                    "SELECT grant_id FROM profile_save_members WHERE operation=?1 AND ordinal=0",
                    [operation.as_str()],
                    |row| row.get(0),
                )
                .unwrap();
            match corruption {
                0 => {
                    conn.execute_batch("DROP TRIGGER permission_effect_no_delete")
                        .unwrap();
                    conn.execute(
                        "DELETE FROM permission_effects WHERE grant_id=?1",
                        [&pack_grant],
                    )
                    .unwrap();
                }
                1 => {
                    conn.execute_batch("DROP TRIGGER terminal_no_rewrite")
                        .unwrap();
                    conn.execute(
                        "UPDATE terminal_outbox SET body=x'7b7d' WHERE grant_id=?1",
                        [&pack_grant],
                    )
                    .unwrap();
                }
                2 => {
                    conn.execute_batch("DROP TRIGGER profile_save_members_no_delete")
                        .unwrap();
                    conn.execute(
                        "DELETE FROM profile_save_members WHERE operation=?1 AND ordinal=0",
                        [operation.as_str()],
                    )
                    .unwrap();
                }
                _ => {
                    conn.execute(
                        "UPDATE terminal_outbox SET quarantined=1 WHERE grant_id=?1",
                        [&pack_grant],
                    )
                    .unwrap();
                }
            }
        }
        assert!(f.root.recover_profile_save(&f.profile, &f.control).is_err());
        assert!(matches!(
            f.approvals.admission_status(&f.profile).unwrap(),
            AdmissionStatus::IntegrityFault { .. }
        ));
        assert!(
            matches!(f.root.read_profile_file(&f.profile,"head",128),Err(StoreError::Io(error)) if error.kind()==std::io::ErrorKind::NotFound)
        );
        assert_eq!(
            f.approvals
                .connection
                .lock()
                .unwrap()
                .query_row(
                    "SELECT settled FROM profile_save_decisions WHERE operation=?1",
                    [operation.as_str()],
                    |row| row.get::<_, i64>(0)
                )
                .unwrap(),
            0
        );
    }
}

#[tokio::test]
async fn recovery_never_overwrites_an_unrelated_head_or_a_corrupt_candidate() {
    for corrupt_candidate in [false, true] {
        let f = Fixture::new();
        let _: StoreError =
            PreparedPermissionApproval::commit_all_with(vec![f.prepare().await], |batch| {
                f.root.commit_prepared_profile_inner(
                    batch,
                    None,
                    &f.revision,
                    || {
                        Err(StoreError::Io(std::io::Error::other(
                            "injected after decision",
                        )))
                    },
                    std::fs::File::sync_all,
                )?;
                Ok(())
            })
            .unwrap_err();
        let unrelated = Digest::hash(b"unrelated head");
        if corrupt_candidate {
            f.root
                .write_profile_file_atomic(
                    &f.profile,
                    &format!("revisions/{}.json", f.revision.hex()),
                    b"corrupt",
                )
                .unwrap();
        } else {
            // Simulate an out-of-band writer, not an admitted configuration.
            f.root
                .write_profile_file_atomic(&f.profile, "head", unrelated.as_str().as_bytes())
                .unwrap();
        }
        assert!(f.root.recover_profile_save(&f.profile, &f.control).is_err());
        if corrupt_candidate {
            assert!(
                matches!(f.root.read_profile_file(&f.profile,"head",128),Err(StoreError::Io(error)) if error.kind()==std::io::ErrorKind::NotFound)
            );
        } else {
            assert_eq!(
                f.root.read_profile_file(&f.profile, "head", 128).unwrap(),
                unrelated.as_str().as_bytes()
            );
        }
        assert_eq!(
            f.approvals
                .connection
                .lock()
                .unwrap()
                .query_row("SELECT settled FROM profile_save_decisions", [], |row| row
                    .get::<_, i64>(
                    0
                ))
                .unwrap(),
            0
        );
    }
}
