//! Existing native review freshness; these preconditions never grant approval.
use super::*;

#[tokio::test]
async fn save_epoch_precondition_rejects_stale_fresh_review_before_prompts_then_accepts_current() {
    let (core, host, profile, _dir) = fixture();
    let before = core.profiles.root.profile_head_snapshot(&profile.id).err();
    assert!(before.is_some());
    assert!(matches!(
        core.save_profile_at_epoch(profile.clone(), None, 1).await,
        Err(CoreError::Invalid(message)) if message == "Profile authority changed; reload before applying the edit"
    ));
    assert!(host.prompts.lock().unwrap().is_empty());
    assert!(core.profiles.head(&profile.id).unwrap().is_none());
    let approvals = Approvals::open(&core.profiles.root, &profile.id).unwrap();
    assert_eq!(approvals.security_epoch(&profile.id).unwrap(), 0);
    let digest = core
        .save_profile_at_epoch(profile.clone(), None, 0)
        .await
        .unwrap();
    assert_eq!(
        core.profiles
            .root
            .profile_head_snapshot(&profile.id)
            .unwrap(),
        (digest, 1)
    );
    assert!(!host.prompts.lock().unwrap().is_empty());
}

#[tokio::test]
async fn save_epoch_precondition_rejects_same_head_aba_without_reprompt_or_new_authority() {
    let (core, host, profile, _dir) = fixture();
    let digest = core
        .save_profile_at_epoch(profile.clone(), None, 0)
        .await
        .unwrap();
    let (_, reviewed_epoch) = core
        .profiles
        .root
        .profile_head_snapshot(&profile.id)
        .unwrap();
    // A later ordinary save advances authority while preserving exact head
    // bytes. A head-only CAS cannot detect this stale review.
    let same = core
        .save_profile(profile.clone(), Some(digest.clone()))
        .await
        .unwrap();
    assert_eq!(same, digest);
    let before = core
        .profiles
        .root
        .profile_head_snapshot(&profile.id)
        .unwrap();
    assert_eq!(before, (digest.clone(), reviewed_epoch + 1));
    let prompt_count = host.prompts.lock().unwrap().len();
    let approvals = Approvals::open(&core.profiles.root, &profile.id).unwrap();
    let pack = &profile.packs[0];
    let policy = approvals
        .permissions(&profile.id, &pack.id, &pack.sha256)
        .unwrap();
    assert!(matches!(
        core.save_profile_at_epoch(profile.clone(), Some(digest), reviewed_epoch).await,
        Err(CoreError::Invalid(message)) if message == "Profile authority changed; reload before applying the edit"
    ));
    assert_eq!(host.prompts.lock().unwrap().len(), prompt_count);
    assert_eq!(
        core.profiles
            .root
            .profile_head_snapshot(&profile.id)
            .unwrap(),
        before
    );
    assert_eq!(
        approvals
            .permissions(&profile.id, &pack.id, &pack.sha256)
            .unwrap(),
        policy
    );
    // The current review is still valid and uses the same ordinary native
    // approval path. The stale precondition does not permanently fence saves.
    core.save_profile_at_epoch(profile.clone(), Some(same), before.1)
        .await
        .unwrap();
    assert_eq!(approvals.security_epoch(&profile.id).unwrap(), before.1 + 1);
}
