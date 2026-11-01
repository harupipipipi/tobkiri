//! Real components prove input -> Flow -> typed provider -> stream -> SQLite.
mod support;
use anyhow::Result;
use support::*;
use tobkiri_core::CoreError;
use tobkiri_gate::GateError;

#[tokio::test]
async fn verified_pack_changes_require_a_fresh_fixture_review_before_core_admission() -> Result<()>
{
    let mut fixture = Fixture::new("echo", None)?;
    let unreviewed = Fixture::new("harness-mock", None)?;
    let core = fixture.core()?;

    // Both candidates are real verified artifacts. Their verification alone
    // must not script the trusted user's answer to a different Pack identity.
    let error = core
        .save_profile(unreviewed.profile.clone(), None)
        .await
        .expect_err("an unreviewed Pack set was approved");
    assert!(matches!(error, CoreError::Gate(GateError::Denied(_))));
    assert!(core.profile(&unreviewed.profile.id).is_err());
    assert!(fixture.host.approvals.lock().unwrap().is_empty());

    let original_hash = fixture
        .profile
        .packs
        .iter()
        .find(|pin| pin.id.as_str() == "local.echo-prompt")
        .unwrap()
        .sha256
        .clone();
    fixture.replace_pack_json(
        "local.echo-prompt",
        "config.json",
        &serde_json::json!({
            "max_tokens":128,
            "model":"mock",
            "system_prompt":"Use the reviewed private fixture revision.",
        }),
    )?;
    assert_ne!(
        fixture
            .profile
            .packs
            .iter()
            .find(|pin| pin.id.as_str() == "local.echo-prompt")
            .unwrap()
            .sha256,
        original_hash,
    );
    let error = core
        .save_profile(fixture.profile.clone(), None)
        .await
        .expect_err("the old reviewed Pack ID approved an unreviewed hash");
    assert!(matches!(error, CoreError::Gate(GateError::Denied(_))));
    assert!(core.profile(&fixture.profile.id).is_err());

    // A new explicit review admits the modified pin, through the ordinary
    // permission path, and the actual component/Flow completes afterward.
    fixture.host.review_pack_permissions(&fixture.profile)?;
    core.save_profile(fixture.profile.clone(), None).await?;
    let session = core.start(&fixture.profile.id).await?;
    let events = fixture.turn(&session, "Fresh reviewed revision").await?;
    assert_eq!(deltas(&events).concat(), "Echo: Fresh reviewed revision");
    session.close().await?;
    Ok(())
}

#[tokio::test]
async fn entirely_different_pack_set_chats_and_persists_after_core_restart() -> Result<()> {
    let fixture = Fixture::new("echo", None)?;
    assert!(
        fixture
            .profile
            .packs
            .iter()
            .all(|pin| !pin.id.as_str().contains("harness"))
    );
    let (_, session) = fixture.start().await?;
    let first = fixture.turn(&session, "First durable message").await?;
    assert_eq!(deltas(&first).concat(), "Echo: First durable message");
    assert_eq!(loaded_message_count(&first), Some(0));
    drop(session);
    let reopened = fixture.core()?;
    let session = reopened.start(&fixture.profile.id).await?;
    let second = fixture.turn(&session, "Second message").await?;
    assert_eq!(loaded_message_count(&second), Some(2));
    let path = fixture
        .data_root()
        .join("profiles")
        .join(fixture.profile.id.as_str())
        .join("data/local.echo-store/private.sqlite");
    assert!(path.is_file());
    assert!(!fixture.host.output.lock().unwrap().is_empty());
    assert_eq!(
        fixture.host.approvals.lock().unwrap().len(),
        fixture.profile.packs.len()
    );
    Ok(())
}

#[tokio::test]
async fn harness_mock_stream_uses_the_flow_in_order() -> Result<()> {
    let fixture = Fixture::new("harness-mock", None)?;
    let (_, session) = fixture.start().await?;
    let events = fixture.turn(&session, "A typed port turn").await?;
    let deltas = deltas(&events);
    assert!(deltas.len() >= 5);
    assert_eq!(deltas.concat(), "Echo: A typed port turn");
    assert!(
        events
            .windows(2)
            .all(|pair| pair[0].sequence + 1 == pair[1].sequence)
    );
    assert!(
        events
            .iter()
            .all(|event| event.plan_digest == events[0].plan_digest)
    );
    Ok(())
}
