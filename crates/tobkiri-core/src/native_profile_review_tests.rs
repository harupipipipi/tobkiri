//! Real Core/Store native review seams; mock input is not GUI provenance proof.
use super::*;
use tobkiri_gate::{
    ApprovalReviewBatch, ApprovalReviewItemId, DeviceAttestation, GateError,
    PendingPermissionReview,
};

#[derive(Clone, Copy)]
enum Reply {
    Approve,
    DenyLast,
    MissingLast,
    Duplicate,
    Foreign,
    WrongAttestation,
    Wait,
}

struct ReviewHost {
    inner: Arc<Host>,
    reply: Mutex<Reply>,
    calls: std::sync::atomic::AtomicUsize,
    item_counts: Mutex<Vec<usize>>,
    contents: Mutex<Vec<serde_json::Value>>,
    previous: Mutex<Option<ApprovalReviewItemId>>,
    hook: Mutex<Option<Box<dyn Fn() + Send + Sync>>>,
    entered: tokio::sync::Notify,
}
#[async_trait]
impl TrustedPrompt for ReviewHost {
    async fn prompt_approval(&self, prompt: ApprovalPrompt) -> ApprovalAnswer {
        self.inner.prompt_approval(prompt).await
    }
    async fn prompt_approval_review(
        &self,
        review: &mut ApprovalReviewBatch<'_>,
    ) -> Result<(), GateError> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        self.contents
            .lock()
            .unwrap()
            .push(serde_json::from_slice(review.content()).unwrap());
        let count = review.items().len();
        self.item_counts.lock().unwrap().push(count);
        let previous = self.previous.lock().unwrap().clone();
        let first = review.item(0).map(|item| item.prompt().clone());
        if let Some(item) = review.item(0) {
            *self.previous.lock().unwrap() = Some(item.id().clone());
        }
        let reply = *self.reply.lock().unwrap();
        self.entered.notify_one();
        if matches!(reply, Reply::Wait) {
            std::future::pending::<()>().await;
        }
        for index in 0..count {
            let id = review.item(index).unwrap().id().clone();
            if matches!(reply, Reply::MissingLast) && index + 1 == count {
                continue;
            }
            if matches!(reply, Reply::Foreign) && index == 0 {
                // Ignoring the failure must not let a later valid reply clear it.
                let _ = review.answer(previous.as_ref().unwrap(), ApprovalAnswer::Approve);
            }
            let answer = if matches!(reply, Reply::DenyLast) && index + 1 == count {
                ApprovalAnswer::Deny
            } else if matches!(reply, Reply::WrongAttestation) && index + 1 == count {
                let first = first.as_ref().unwrap();
                ApprovalAnswer::Attestation(DeviceAttestation {
                    device: "fixture-device".into(),
                    profile: first.profile.clone(),
                    principal: first.principal.clone(),
                    reservation: first.reservation.clone(),
                    request: first.request.clone(),
                    effect: first.effect.digest()?,
                    args: first.args_digest.clone(),
                    epoch: first.security_epoch,
                    expires_unix_ms: SystemTime::now()
                        .duration_since(UNIX_EPOCH)
                        .unwrap()
                        .as_millis() as u64
                        + 60_000,
                    nonce: "fixture-nonce".into(),
                    signature: vec![0; 64],
                })
            } else {
                ApprovalAnswer::Approve
            };
            let _ = review.answer(&id, answer);
            if matches!(reply, Reply::Duplicate) && index == 0 {
                let _ = review.answer(&id, ApprovalAnswer::Approve);
            }
        }
        let hook = self.hook.lock().unwrap().take();
        if let Some(hook) = hook {
            hook();
        }
        Ok(())
    }
}
#[async_trait]
impl HostIo for ReviewHost {
    fn display(&self, _: &str, _: &CanonValue) {}
    async fn terminal_read_line(&self, _: &str) -> Result<String, HostError> {
        Err(HostError("unavailable".into()))
    }
    fn terminal_write(&self, _: &str) {}
    fn embed_ui(&self, _: UiEmbed) -> Result<UiSessionId, HostError> {
        Err(HostError("unavailable".into()))
    }
    async fn pick_directory(&self, _: &str) -> Option<PathBuf> {
        None
    }
}

fn reviewed_fixture(reply: Reply) -> (Core, Arc<ReviewHost>, Profile, tempfile::TempDir) {
    let (mut core, inner, profile, dir) = fixture();
    let host = Arc::new(ReviewHost {
        inner,
        reply: Mutex::new(reply),
        calls: Default::default(),
        item_counts: Default::default(),
        contents: Default::default(),
        previous: Default::default(),
        hook: Default::default(),
        entered: Default::default(),
    });
    core.host = host.clone();
    (core, host, profile, dir)
}

fn no_authority(core: &Core, profile: &Profile) {
    let approvals = Approvals::open(&core.profiles.root, &profile.id).unwrap();
    assert!(
        approvals
            .permissions(&profile.id, &profile.packs[0].id, &profile.packs[0].sha256)
            .unwrap()
            .is_none()
    );
    assert!(approvals.pending().unwrap().is_empty());
    assert!(core.profiles.head(&profile.id).unwrap().is_none());
    let path = core
        .profiles
        .root
        .path()
        .join(DataRoot::profile_relative(&profile.id, "approvals.sqlite").unwrap());
    let raw = rusqlite::Connection::open(path).unwrap();
    for table in ["user_approvals", "permission_effects"] {
        let count: i64 = raw
            .query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(count, 0);
    }
}

#[tokio::test]
async fn fresh_native_creation_reviews_once_without_reusing_the_source_identity() {
    let (core, host, profile, _dir) = reviewed_fixture(Reply::Approve);
    let old_head = core.save_profile(profile.clone(), None).await.unwrap();
    host.inner.prompts.lock().unwrap().clear();
    let id = core
        .create_profile_with_native_review(
            &serde_json::to_vec(&profile).unwrap(),
            "Fresh Harness".into(),
        )
        .await
        .unwrap();
    assert_ne!(id, profile.id);
    assert_eq!(host.calls.load(Ordering::SeqCst), 1);
    assert!(host.inner.prompts.lock().unwrap().is_empty());
    assert_eq!(core.profiles.head(&profile.id).unwrap(), Some(old_head));
    let contents = host.contents.lock().unwrap();
    let shown = &contents[0];
    assert_eq!(shown["definition"]["id"], id.as_str());
    assert_eq!(shown["definition"]["name"], "Fresh Harness");
    assert_eq!(shown["precondition"]["head"], serde_json::Value::Null);
    let (head, epoch) = core.profiles.root.profile_head_snapshot(&id).unwrap();
    assert_eq!(
        head.as_str(),
        shown["structural"]["candidate"]["revision"]
            .as_str()
            .unwrap()
    );
    let plan = Digest::parse(shown["plan_digest"].as_str().unwrap()).unwrap();
    let approvals = Approvals::open(&core.profiles.root, &id).unwrap();
    assert_eq!(
        approvals
            .user_permissions(&id, &head, &plan, epoch)
            .unwrap(),
        profile.user_permissions
    );
}

#[tokio::test]
async fn fresh_native_creation_denial_saves_no_revision_or_authority() {
    let (core, host, profile, _dir) = reviewed_fixture(Reply::DenyLast);
    assert!(
        core.create_profile_with_native_review(
            &serde_json::to_vec(&profile).unwrap(),
            "Denied Harness".into(),
        )
        .await
        .is_err()
    );
    assert_eq!(host.calls.load(Ordering::SeqCst), 1);
    assert!(host.inner.prompts.lock().unwrap().is_empty());
    let mut candidate = profile.clone();
    candidate.id = ProfileId::new(
        host.contents.lock().unwrap()[0]["definition"]["id"]
            .as_str()
            .unwrap(),
    )
    .unwrap();
    assert_ne!(candidate.id, profile.id);
    no_authority(&core, &candidate);
    assert!(core.profiles.head(&profile.id).unwrap().is_none());
}

#[tokio::test]
async fn native_profile_review_commits_exact_candidate_in_one_interaction_without_single_prompts() {
    let (core, host, profile, _dir) = reviewed_fixture(Reply::Approve);
    let precondition = core.profile_save_precondition(&profile.id).unwrap();
    let saved = core
        .save_profile_with_native_review(profile.clone(), precondition.clone())
        .await
        .unwrap();
    assert_eq!(host.calls.load(Ordering::SeqCst), 1);
    assert!(host.inner.prompts.lock().unwrap().is_empty());
    assert_eq!(*host.item_counts.lock().unwrap(), [2]);
    let contents = host.contents.lock().unwrap();
    let content = &contents[0];
    assert_eq!(
        content["definition"],
        serde_json::to_value(&profile).unwrap()
    );
    assert_eq!(
        content["precondition"],
        serde_json::to_value(precondition).unwrap()
    );
    assert_eq!(
        content["structural"]["candidate"]["revision"],
        saved.as_str()
    );
    assert_eq!(
        content["structural"]["packs"][0]["artifact_hash"],
        profile.packs[0].sha256.as_str()
    );
    let plan = Digest::parse(content["plan_digest"].as_str().unwrap()).unwrap();
    let approvals = Approvals::open(&core.profiles.root, &profile.id).unwrap();
    assert_eq!(
        approvals
            .user_permissions(&profile.id, &saved, &plan, 1)
            .unwrap(),
        profile.user_permissions
    );
    assert_eq!(
        core.profiles
            .root
            .profile_head_snapshot(&profile.id)
            .unwrap(),
        (saved, 1)
    );
}

#[tokio::test]
async fn unsupported_group_surface_never_falls_back_to_single_prompt_approval() {
    let (core, host, profile, _dir) = fixture();
    let precondition = core.profile_save_precondition(&profile.id).unwrap();
    assert!(matches!(
        core.save_profile_with_native_review(profile.clone(), precondition)
            .await,
        Err(CoreError::Gate(GateError::Unavailable))
    ));
    assert!(host.prompts.lock().unwrap().is_empty());
    no_authority(&core, &profile);
}

#[tokio::test]
async fn native_review_denial_missing_duplicate_or_wrong_attestation_commits_no_member() {
    for reply in [
        Reply::DenyLast,
        Reply::MissingLast,
        Reply::Duplicate,
        Reply::WrongAttestation,
    ] {
        let (core, host, profile, _dir) = reviewed_fixture(reply);
        let precondition = core.profile_save_precondition(&profile.id).unwrap();
        assert!(matches!(
            core.save_profile_with_native_review(profile.clone(), precondition)
                .await,
            Err(CoreError::Gate(GateError::Denied(_)))
        ));
        assert_eq!(host.calls.load(Ordering::SeqCst), 1);
        assert!(host.inner.prompts.lock().unwrap().is_empty());
        no_authority(&core, &profile);
        assert_eq!(
            Approvals::open(&core.profiles.root, &profile.id)
                .unwrap()
                .security_epoch(&profile.id)
                .unwrap(),
            0
        );
    }
}

#[tokio::test]
async fn native_review_rejects_changed_epoch_during_display_and_wrong_target_before_display() {
    let (core, host, profile, _dir) = reviewed_fixture(Reply::Approve);
    let mut precondition = core.profile_save_precondition(&profile.id).unwrap();
    precondition.profile_id = ProfileId::parse("prof_different_review_target").unwrap();
    assert!(matches!(
        core.save_profile_with_native_review(profile.clone(), precondition)
            .await,
        Err(CoreError::Invalid(_))
    ));
    assert_eq!(host.calls.load(Ordering::SeqCst), 0);
    let precondition = core.profile_save_precondition(&profile.id).unwrap();
    let approvals = Approvals::open(&core.profiles.root, &profile.id).unwrap();
    let id = profile.id.clone();
    *host.hook.lock().unwrap() = Some(Box::new(move || {
        approvals.advance_security_epoch(&id).unwrap();
    }));
    assert!(matches!(
        core.save_profile_with_native_review(profile.clone(), precondition)
            .await,
        Err(CoreError::Gate(GateError::StaleEpoch))
    ));
    assert_eq!(host.calls.load(Ordering::SeqCst), 1);
    no_authority(&core, &profile);
    assert_eq!(
        Approvals::open(&core.profiles.root, &profile.id)
            .unwrap()
            .security_epoch(&profile.id)
            .unwrap(),
        1
    );
}

#[tokio::test]
async fn dropping_native_save_before_commit_abandons_every_original_request() {
    let (core, host, profile, _dir) = reviewed_fixture(Reply::Wait);
    let precondition = core.profile_save_precondition(&profile.id).unwrap();
    core.ensure_admission(&profile.id).await.unwrap();
    let before = core.control.usage().count(tobkiri_gate::ControlKind::Queue);
    let mut operation =
        Box::pin(core.save_profile_with_native_review(profile.clone(), precondition));
    tokio::select! {
        _ = host.entered.notified() => {},
        _ = &mut operation => panic!("review operation completed before native input"),
    }
    let approvals = Approvals::open(&core.profiles.root, &profile.id).unwrap();
    assert_eq!(approvals.pending().unwrap().len(), 2);
    drop(operation);
    no_authority(&core, &profile);
    assert_eq!(
        core.control.usage().count(tobkiri_gate::ControlKind::Queue),
        before
    );
    assert!(host.inner.prompts.lock().unwrap().is_empty());
}

#[tokio::test]
async fn native_review_rejects_foreign_reply_even_when_native_caller_ignores_the_error() {
    let (core, host, profile, _dir) = reviewed_fixture(Reply::Approve);
    let (gate, cx, packs) = permission_preparation_fixture(&core, &profile);
    let before = core.control.usage().bytes;
    let pending = gate
        .review_pack_permissions(&cx, &packs[0].manifest.id, None, &packs[0].hash)
        .await
        .unwrap();
    let first = PendingPermissionReview::request_all(vec![pending], b"{}")
        .await
        .unwrap();
    drop(first);
    *host.reply.lock().unwrap() = Reply::Foreign;
    let pending = gate
        .review_pack_permissions(&cx, &packs[0].manifest.id, None, &packs[0].hash)
        .await
        .unwrap();
    assert!(matches!(
        PendingPermissionReview::request_all(vec![pending], b"{}").await,
        Err(GateError::Denied(_))
    ));
    assert_eq!(core.control.usage().bytes, before);
    no_authority(&core, &profile);
}

#[tokio::test]
async fn native_review_rejects_mixed_original_contexts_without_showing_a_group() {
    let (core, host, profile, _dir) = reviewed_fixture(Reply::Approve);
    let (gate, cx, packs) = permission_preparation_fixture(&core, &profile);
    let other = core
        .user_context(&profile, &gate)
        .unwrap()
        .with_revision(cx.revision().unwrap().clone());
    let before = core.control.usage().bytes;
    let a = gate
        .review_pack_permissions(&cx, &packs[0].manifest.id, None, &packs[0].hash)
        .await
        .unwrap();
    let b = gate
        .review_pack_permissions(&other, &packs[0].manifest.id, None, &packs[0].hash)
        .await
        .unwrap();
    assert!(matches!(
        PendingPermissionReview::request_all(vec![a, b], b"{}").await,
        Err(GateError::Denied(_))
    ));
    assert_eq!(host.calls.load(Ordering::SeqCst), 0);
    assert_eq!(core.control.usage().bytes, before);
    no_authority(&core, &profile);
}

#[test]
fn native_review_content_uses_original_source_custody_and_releases_its_record() {
    let (core, _host, profile, _dir) = fixture();
    let precondition = core.profile_save_precondition(&profile.id).unwrap();
    let packs = load_packs(&profile, &core.control).unwrap();
    let compiled = compile(&profile, &packs).unwrap();
    let document = Digest::hash(&crate::profiles::strict_bytes(&profile).unwrap());
    let before = core.control.usage();
    let content = crate::native_profile_review::capture(
        &profile,
        &packs,
        &document,
        &compiled.plan_digest,
        &precondition,
        &core.control,
    )
    .unwrap();
    assert!(core.control.usage().bytes >= before.bytes + content.as_bytes().len() as u64);
    assert_eq!(
        core.control
            .usage()
            .count(tobkiri_gate::ControlKind::Catalog),
        before.count(tobkiri_gate::ControlKind::Catalog)
    );
    drop(content);
    assert_eq!(core.control.usage().bytes, before.bytes);
    assert!(core.profiles.head(&profile.id).unwrap().is_none());
}

#[tokio::test]
async fn native_review_with_existing_permissions_fabricates_no_prompt_or_attestation() {
    let (core, host, profile, _dir) = reviewed_fixture(Reply::Approve);
    let (gate, cx, packs) = permission_preparation_fixture(&core, &profile);
    let candidate = Digest::hash(b"recorded-existing-user-document");
    gate.prepare_pack_permissions(&cx, &packs[0].manifest.id, None, &packs[0].hash)
        .await
        .unwrap()
        .commit()
        .unwrap();
    gate.prepare_user_permissions(
        &cx,
        &candidate,
        cx.revision().unwrap(),
        cx.security_epoch(),
        &profile.ceiling,
    )
    .await
    .unwrap()
    .commit()
    .unwrap();
    let singles = host.inner.prompts.lock().unwrap().len();
    let before = core.control.usage().bytes;
    let pack = gate
        .review_pack_permissions(
            &cx,
            &packs[0].manifest.id,
            Some(&packs[0].hash),
            &packs[0].hash,
        )
        .await
        .unwrap();
    let user = gate
        .review_user_permissions(
            &cx,
            &candidate,
            cx.revision().unwrap(),
            cx.security_epoch(),
            &profile.ceiling,
        )
        .await
        .unwrap();
    assert!(pack.prompt().is_none() && user.prompt().is_none());
    PendingPermissionReview::request_all(vec![pack, user], b"{}")
        .await
        .unwrap()
        .commit_all()
        .unwrap();
    assert_eq!(host.calls.load(Ordering::SeqCst), 1);
    assert_eq!(*host.item_counts.lock().unwrap(), [0]);
    assert_eq!(host.inner.prompts.lock().unwrap().len(), singles);
    assert_eq!(core.control.usage().bytes, before);
}

#[tokio::test]
async fn native_review_rejects_unbounded_incoming_array_capacity_before_display() {
    let (core, host, profile, _dir) = reviewed_fixture(Reply::Approve);
    let (gate, cx, packs) = permission_preparation_fixture(&core, &profile);
    let before = core.control.usage().bytes;
    let pending = gate
        .review_pack_permissions(&cx, &packs[0].manifest.id, None, &packs[0].hash)
        .await
        .unwrap();
    let mut oversized = Vec::with_capacity(4097);
    oversized.push(pending);
    assert!(matches!(
        PendingPermissionReview::request_all(oversized, b"{}").await,
        Err(GateError::Limit(_))
    ));
    assert_eq!(host.calls.load(Ordering::SeqCst), 0);
    assert_eq!(core.control.usage().bytes, before);
    no_authority(&core, &profile);
}
