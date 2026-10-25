//! Native request staging is not a decision, grant, or activation receipt.
use super::*;
use tobkiri_gate::{GateError, Outcome};

#[tokio::test]
async fn pending_pack_and_user_reviews_do_not_prompt_or_install_authority_and_drop_originals() {
    let (core, host, profile, _dir) = fixture();
    let (gate, cx, packs) = permission_preparation_fixture(&core, &profile);
    let approvals = Approvals::open(&core.profiles.root, &profile.id).unwrap();
    let before = core.control.usage();
    let candidate = Digest::hash(b"recorded-candidate-not-a-saved-revision");
    let pack = gate
        .review_pack_permissions(&cx, &packs[0].manifest.id, None, &packs[0].hash)
        .await
        .unwrap();
    let user = gate
        .review_user_permissions(
            &cx,
            &candidate,
            cx.revision().unwrap(),
            cx.security_epoch() + 1,
            &profile.ceiling,
        )
        .await
        .unwrap();
    let ids = [
        pack.prompt().unwrap().reservation.clone(),
        user.prompt().unwrap().reservation.clone(),
    ];
    assert_ne!(ids[0], ids[1]);
    assert!(host.prompts.lock().unwrap().is_empty());
    assert!(
        approvals
            .permissions(&profile.id, &packs[0].manifest.id, &packs[0].hash)
            .unwrap()
            .is_none()
    );
    assert!(
        approvals
            .user_permissions(&profile.id, &candidate, cx.revision().unwrap(), 1)
            .unwrap()
            .is_none()
    );
    assert_eq!(
        core.control.usage().count(tobkiri_gate::ControlKind::Queue),
        before.count(tobkiri_gate::ControlKind::Queue) + 2
    );
    assert!(
        core.control.usage().bytes
            >= before.bytes
                + 8192
                + pack.prompt().unwrap().summary.capacity() as u64
                + user.prompt().unwrap().summary.capacity() as u64
    );
    for id in &ids {
        assert_eq!(
            approvals.reservation_state(id).unwrap().as_deref(),
            Some("pending")
        );
        assert!(approvals.terminal_delivery(id).unwrap().is_none());
    }
    drop(pack);
    drop(user);
    for id in ids {
        assert_eq!(
            approvals
                .terminal_delivery(&id)
                .unwrap()
                .unwrap()
                .decision
                .outcome,
            Outcome::Cancelled
        );
    }
    assert_eq!(core.control.usage().bytes, before.bytes);
    assert_eq!(approvals.security_epoch(&profile.id).unwrap(), 0);
    assert!(core.profiles.head(&profile.id).unwrap().is_none());
}

#[tokio::test]
async fn pending_permission_review_requires_the_original_native_decision() {
    for deny in [false, true] {
        let (core, host, profile, _dir) = fixture();
        let (gate, cx, packs) = permission_preparation_fixture(&core, &profile);
        let approvals = Approvals::open(&core.profiles.root, &profile.id).unwrap();
        let before = core.control.usage().bytes;
        let pending = gate
            .review_pack_permissions(&cx, &packs[0].manifest.id, None, &packs[0].hash)
            .await
            .unwrap();
        let exact = serde_json::to_value(pending.prompt().unwrap()).unwrap();
        assert!(host.prompts.lock().unwrap().is_empty());
        host.deny.store(deny, Ordering::SeqCst);
        match pending.request_approval().await {
            Ok(prepared) => {
                assert!(!deny);
                assert!(
                    approvals
                        .permissions(&profile.id, &packs[0].manifest.id, &packs[0].hash)
                        .unwrap()
                        .is_none()
                );
                prepared.commit().unwrap();
            }
            Err(error) => assert!(deny && matches!(error, GateError::Denied(_))),
        }
        let prompts = host.prompts.lock().unwrap();
        assert_eq!(prompts.len(), 1);
        assert_eq!(serde_json::to_value(&prompts[0]).unwrap(), exact);
        assert_eq!(
            approvals
                .permissions(&profile.id, &packs[0].manifest.id, &packs[0].hash)
                .unwrap()
                .is_some(),
            !deny
        );
        assert_eq!(core.control.usage().bytes, before);
        assert_eq!(approvals.security_epoch(&profile.id).unwrap(), 0);
        assert!(core.profiles.head(&profile.id).unwrap().is_none());
    }
}

#[tokio::test]
async fn pending_reviews_reject_cancel_or_revocation_before_showing_the_native_prompt() {
    for revoke in [false, true] {
        let (core, host, profile, _dir) = fixture();
        let (gate, cx, packs) = permission_preparation_fixture(&core, &profile);
        let approvals = Approvals::open(&core.profiles.root, &profile.id).unwrap();
        let before = core.control.usage().bytes;
        let pack = gate
            .review_pack_permissions(&cx, &packs[0].manifest.id, None, &packs[0].hash)
            .await
            .unwrap();
        let user = gate
            .review_user_permissions(
                &cx,
                &Digest::hash(b"candidate"),
                cx.revision().unwrap(),
                cx.security_epoch() + 1,
                &profile.ceiling,
            )
            .await
            .unwrap();
        if revoke {
            approvals.advance_security_epoch(&profile.id).unwrap();
        } else {
            cx.cancellation().cancel();
        }
        for result in [pack.request_approval().await, user.request_approval().await] {
            assert!(matches!(
                (revoke, result),
                (true, Err(GateError::StaleEpoch)) | (false, Err(GateError::Cancelled))
            ));
        }
        assert!(host.prompts.lock().unwrap().is_empty());
        assert!(
            approvals
                .permissions(&profile.id, &packs[0].manifest.id, &packs[0].hash)
                .unwrap()
                .is_none()
        );
        assert_eq!(core.control.usage().bytes, before);
    }
}

#[tokio::test]
async fn pending_review_keeps_its_original_deadline_before_requesting_consent() {
    let (core, host, profile, _dir) = fixture();
    let (gate, cx, packs) = permission_preparation_fixture(&core, &profile);
    let approvals = Approvals::open(&core.profiles.root, &profile.id).unwrap();
    let before = core.control.usage().bytes;
    let pending = gate
        .review_pack_permissions(&cx, &packs[0].manifest.id, None, &packs[0].hash)
        .await
        .unwrap();
    let id = pending.prompt().unwrap().reservation.clone();
    tokio::time::sleep_until(cx.deadline().into()).await;
    assert!(matches!(
        pending.request_approval().await,
        Err(GateError::Deadline)
    ));
    assert!(host.prompts.lock().unwrap().is_empty());
    assert!(matches!(
        approvals.terminal_delivery(&id).unwrap().unwrap().decision.outcome,
        Outcome::Failed { code } if code == "deadline-expired-before-use"
    ));
    assert_eq!(core.control.usage().bytes, before);
}
