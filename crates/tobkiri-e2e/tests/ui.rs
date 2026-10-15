//! Actual guest-to-Flow UI stream proof through the generic Core Session facade.
//! Browser rendering/native WebView acceptance remains a separate proof.
mod support;
use anyhow::{Context, Result};
use support::*;
use tobkiri_canon::{
    CanonValue, CoreBuffer,
    ids::{OpName, ShapeRef, UiSessionId},
};
use tobkiri_core::CoreError;

fn stream_handle(buffer: &CoreBuffer) -> Result<u64> {
    let opened = parse_output(buffer)?;
    assert_eq!(
        opened.get("kind").and_then(CanonValue::as_str),
        Some("stream")
    );
    Ok(u64::try_from(
        opened
            .get("handle")
            .and_then(CanonValue::as_i64)
            .context("UI stream handle missing")?,
    )?)
}

fn event_kind(frame: &CanonValue) -> Option<&str> {
    frame.get("kind")?.get("kind")?.as_str()
}

fn event_delta(frame: &CanonValue) -> Option<&str> {
    if event_kind(frame)? != "emitted" {
        return None;
    }
    let value = frame.get("kind")?.get("value")?;
    (value.get("kind")?.as_str()? == "delta")
        .then(|| value.get("text")?.as_str())
        .flatten()
}

#[tokio::test]
async fn pack_ui_scoped_stream_reaches_flow_and_verified_persistence() -> Result<()> {
    let fixture = Fixture::new("harness-ui", None)?;
    *fixture.host.ui_enabled.lock().unwrap() = true;
    let (_core, session) = fixture.start().await?;
    assert!(
        session.ui_attachments().is_err(),
        "unbound UI was published"
    );
    let generation = 7;
    session.bind_ui_generation(generation)?;
    assert!(session.bind_ui_generation(generation).is_err());
    let attachments = session.ui_attachments()?;
    assert_eq!(attachments.len(), 1);
    let attachment = &attachments[0];
    assert_eq!(attachment.generation, Some(generation));
    assert_eq!(attachment.host_session, UiSessionId::new("ui_test_host")?);
    let token = &attachment.session;
    let args = canon(
        &serde_json::json!({"input":{"conversation":"e2e","text":"Through the actual UI input Pack"}}),
    )?;
    assert!(
        session
            .ui_invoke_received(
                token,
                &attachment.host_session,
                generation + 1,
                &attachment.receiver,
                fixture.shape.clone(),
                OpName::parse("submit")?,
                &args,
            )
            .await
            .is_err(),
        "another Launcher generation reused the attachment"
    );
    assert!(
        session
            .ui_invoke_received(
                token,
                &attachment.host_session,
                generation,
                &tobkiri_canon::Digest::hash(b"foreign-receiver"),
                fixture.shape.clone(),
                OpName::parse("submit")?,
                &args,
            )
            .await
            .is_err(),
        "another renderer receiver reused the attachment"
    );
    let opened_buffer = session
        .ui_invoke_received(
            token,
            &attachment.host_session,
            generation,
            &attachment.receiver,
            fixture.shape.clone(),
            OpName::parse("submit")?,
            &args,
        )
        .await?;
    let handle = stream_handle(&opened_buffer)?;
    let foreign = UiSessionId::new("ui_foreign_test")?;
    assert!(
        session
            .ui_stream_pull_buffer(&foreign, handle, 1)
            .await
            .is_err(),
        "foreign UI token replayed a stream handle"
    );
    assert!(
        session
            .ui_stream_pull_buffer(token, handle, 33)
            .await
            .is_err(),
        "unbounded UI stream pull succeeded"
    );
    let mut events = Vec::new();
    let mut plan_digest = None;
    loop {
        let buffer = tokio::time::timeout(
            std::time::Duration::from_secs(30),
            session.ui_stream_pull_buffer(token, handle, 8),
        )
        .await??;
        let batch = parse_output(&buffer)?;
        let frames = batch
            .as_array()
            .context("UI stream batch is not an array")?;
        assert!(frames.len() <= 8);
        if frames.is_empty() {
            break;
        }
        for frame in frames {
            let plan = tobkiri_canon::Digest::parse(
                frame
                    .get("plan_digest")
                    .and_then(CanonValue::as_str)
                    .context("UI Flow plan digest missing")?,
            )?;
            if let Some(expected) = &plan_digest {
                assert_eq!(&plan, expected);
            } else {
                plan_digest = Some(plan);
            }
            events.push(frame.clone());
        }
    }
    assert_eq!(
        events.iter().filter_map(event_delta).collect::<String>(),
        "Echo: Through the actual UI input Pack"
    );
    assert!(
        events
            .iter()
            .any(|event| event_kind(event) == Some("completed"))
    );
    assert!(events.iter().any(|event| {
        event_kind(event) == Some("step_value")
            && event
                .get("kind")
                .and_then(|kind| kind.get("step"))
                .and_then(CanonValue::as_str)
                == Some("save")
    }));
    assert!(events.windows(2).all(|pair| {
        pair[0].get("request") == pair[1].get("request")
            && pair[0]
                .get("sequence")
                .and_then(CanonValue::as_i64)
                .and_then(|n| n.checked_add(1))
                == pair[1].get("sequence").and_then(CanonValue::as_i64)
    }));
    assert!(
        session
            .ui_stream_pull_buffer(token, handle, 1)
            .await
            .is_err(),
        "completed UI stream handle was retained"
    );
    let stored_args = canon(&serde_json::json!({"conversation":"e2e"}))?;
    let stored_buffer = session
        .ui_invoke_received(
            token,
            &attachment.host_session,
            generation,
            &attachment.receiver,
            ShapeRef::parse("local.harness-chat.conversation-store@1.0")?,
            OpName::parse("load")?,
            &stored_args,
        )
        .await?;
    let stored = parse_output(&stored_buffer)?;
    assert_eq!(
        stored.get("kind").and_then(CanonValue::as_str),
        Some("value")
    );
    assert_eq!(
        stored
            .get("value")
            .and_then(|value| value.get("messages"))
            .and_then(CanonValue::as_array)
            .map(<[_]>::len),
        Some(2),
    );
    session.detach_ui(token).await?;
    assert!(
        session
            .ui_invoke_received(
                token,
                &attachment.host_session,
                generation,
                &attachment.receiver,
                fixture.shape.clone(),
                OpName::parse("read")?,
                &canon(&serde_json::json!({}))?,
            )
            .await
            .is_err(),
        "detached UI retained authority"
    );
    Ok(())
}

#[tokio::test]
async fn oversized_real_flow_event_closes_the_received_ui_stream() -> Result<()> {
    let fixture = Fixture::new("harness-ui", None)?;
    *fixture.host.ui_enabled.lock().unwrap() = true;
    let (_core, session) = fixture.start().await?;
    let generation = 11;
    session.bind_ui_generation(generation)?;
    let attachments = session.ui_attachments()?;
    let [attachment] = attachments.as_slice() else {
        anyhow::bail!("oversized UI fixture requires one actual attachment");
    };
    // Each real UI append stays below the 1 MiB argument bound. Their combined
    // persisted context makes the actual store Pack's load StepValue exceed
    // the UI transport cap, without exceeding the 256 KiB Pack config bound.
    for index in 0..2 {
        let args = canon(&serde_json::json!({
            "conversation":"e2e",
            "input":{"conversation":"e2e","text":format!("Stored turn {index}")},
            "events":[{
                "kind":"done",
                "text":"x".repeat(600 * 1024),
                "prompt-tokens":null,
                "completion-tokens":null,
            }],
        }))?;
        let saved = session
            .ui_invoke_received(
                &attachment.session,
                &attachment.host_session,
                generation,
                &attachment.receiver,
                ShapeRef::parse("local.harness-chat.conversation-store@1.0")?,
                OpName::parse("append")?,
                &args,
            )
            .await?;
        let saved = parse_output(&saved)?;
        // append returns result<_, error>: the public typed facade preserves
        // the payloadless success tag instead of treating it as no result.
        assert_eq!(
            saved,
            canon(&serde_json::json!({"kind":"value","value":{"tag":"ok"}}))?
        );
    }
    let args = canon(&serde_json::json!({
        "input":{"conversation":"e2e","text":"Load the actual persisted context"}
    }))?;
    let opened = session
        .ui_invoke_received(
            &attachment.session,
            &attachment.host_session,
            generation,
            &attachment.receiver,
            fixture.shape.clone(),
            OpName::parse("submit")?,
            &args,
        )
        .await?;
    let handle = stream_handle(&opened)?;
    let error = loop {
        match tokio::time::timeout(
            std::time::Duration::from_secs(30),
            session.ui_stream_pull_buffer(&attachment.session, handle, 1),
        )
        .await?
        {
            Ok(buffer) => {
                let batch = parse_output(&buffer)?;
                let frames = batch
                    .as_array()
                    .context("UI stream batch is not an array")?;
                anyhow::ensure!(
                    !frames.is_empty(),
                    "oversized event never reached the UI transport"
                );
                anyhow::ensure!(
                    frames
                        .iter()
                        .all(|frame| event_kind(frame) != Some("completed")),
                    "oversized event was silently delivered"
                );
            }
            Err(error) => break error,
        }
    };
    assert!(
        matches!(&error, CoreError::Step(step) if step.code == "batch-too-large"),
        "expected the real oversized transport failure: {error}"
    );
    let repeated = session
        .ui_stream_pull_buffer(&attachment.session, handle, 1)
        .await
        .expect_err("oversized stream remained retryable");
    assert!(
        matches!(repeated, CoreError::Invalid(ref message) if message.contains("unknown UI stream handle")),
        "oversized stream retained its native handle: {repeated}"
    );
    tokio::time::timeout(std::time::Duration::from_secs(30), session.close()).await??;
    Ok(())
}
