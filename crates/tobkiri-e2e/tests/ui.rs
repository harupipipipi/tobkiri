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
    let fixture = Fixture::new("harness-ui", None).context("UI phase=fixture")?;
    *fixture.host.ui_enabled.lock().unwrap() = true;
    let (_core, session) = fixture.start().await.context("UI phase=start")?;
    assert!(
        session.ui_attachments().is_err(),
        "unbound UI was published"
    );
    let generation = 7;
    session
        .bind_ui_generation(generation)
        .context("UI phase=bind-generation")?;
    assert!(session.bind_ui_generation(generation).is_err());
    let attachments = session.ui_attachments().context("UI phase=attachments")?;
    assert_eq!(attachments.len(), 1);
    let attachment = &attachments[0];
    assert_eq!(attachment.generation, Some(generation));
    assert_eq!(attachment.host_session, UiSessionId::new("ui_test_host")?);
    let token = &attachment.session;
    let args = canon(
        &serde_json::json!({"input":{"conversation":"e2e","text":"Through the actual UI input Pack"}}),
    )
    .context("UI phase=submit args")?;
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
        .await
        .context("UI phase=submit")?;
    let handle = stream_handle(&opened_buffer).context("UI phase=submit handle")?;
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
    let mut pulls = 0u64;
    loop {
        pulls += 1;
        let buffer = tokio::time::timeout(
            std::time::Duration::from_secs(30),
            session.ui_stream_pull_buffer(token, handle, 8),
        )
        .await
        .with_context(|| format!("UI phase=stream-pull timeout pull={pulls}"))?
        .with_context(|| format!("UI phase=stream-pull pull={pulls}"))?;
        let batch = parse_output(&buffer)
            .with_context(|| format!("UI phase=stream-pull parse pull={pulls}"))?;
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
            )
            .with_context(|| format!("UI phase=stream-pull plan-digest pull={pulls}"))?;
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
    let stored_args =
        canon(&serde_json::json!({"conversation":"e2e"})).context("UI phase=store-load args")?;
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
        .await
        .context("UI phase=store-load")?;
    let stored = parse_output(&stored_buffer).context("UI phase=store-load parse")?;
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
    session.detach_ui(token).await.context("UI phase=detach")?;
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
    let fixture = Fixture::new("harness-ui", None).context("UI phase=fixture")?;
    *fixture.host.ui_enabled.lock().unwrap() = true;
    let (_core, session) = fixture.start().await.context("UI phase=start")?;
    let generation = 11;
    session
        .bind_ui_generation(generation)
        .context("UI phase=bind-generation")?;
    let attachments = session.ui_attachments().context("UI phase=attachments")?;
    let [attachment] = attachments.as_slice() else {
        anyhow::bail!("oversized UI fixture requires one actual attachment");
    };
    // The journal accepts the 999,820-byte load value. Its enclosing actual
    // RunEvent batch adds 205 bytes, crossing the native transport's existing
    // 1,000,000-element allocation bound (before the separate 1 MiB UI copy cap).
    // The configured prompt adds 160 bytes, so that later value also stays
    // below the journal bound. Both appends retain the original argument/config bounds.
    const SOURCE_VALUE_BYTES: usize = 999_820;
    let fixed_context = canon(&serde_json::json!({
        "conversation":"e2e",
        "messages":[
            {"role":"user","content":"Stored turn 0"},
            {"role":"assistant","content":""},
            {"role":"user","content":"Stored turn 1"},
            {"role":"assistant","content":""},
        ],
    }))?;
    let fixed_bytes =
        tobkiri_canon::encode(&fixed_context, tobkiri_canon::Variant::StrictV1)?.len();
    assert_eq!(fixed_bytes, 187);
    let stored_text_lengths = [499_816, 499_817];
    let source_value_bytes = fixed_bytes + stored_text_lengths.iter().sum::<usize>();
    assert_eq!(source_value_bytes, SOURCE_VALUE_BYTES);
    assert!(source_value_bytes <= 1_000_000);
    for (index, text_bytes) in stored_text_lengths.into_iter().enumerate() {
        let args = canon(&serde_json::json!({
            "conversation":"e2e",
            "input":{"conversation":"e2e","text":format!("Stored turn {index}")},
            "events":[{
                "kind":"done",
                "text":"x".repeat(text_bytes),
                "prompt-tokens":null,
                "completion-tokens":null,
            }],
        }))
        .with_context(|| format!("UI phase=store-append args index={index}"))?;
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
            .await
            .with_context(|| format!("UI phase=store-append index={index}"))?;
        let saved = parse_output(&saved)
            .with_context(|| format!("UI phase=store-append parse index={index}"))?;
        // append returns result<_, error>: the public typed facade preserves
        // the payloadless success tag instead of treating it as no result.
        assert_eq!(
            saved,
            canon(&serde_json::json!({"kind":"value","value":{"tag":"ok"}}))?
        );
    }
    let args = canon(&serde_json::json!({
        "input":{"conversation":"e2e","text":"Load the actual persisted context"}
    }))
    .context("UI phase=submit args")?;
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
        .await
        .context("UI phase=submit")?;
    let handle = stream_handle(&opened).context("UI phase=submit handle")?;
    let mut pulls = 0u64;
    let mut checked_transport_boundary = false;
    let error = loop {
        pulls += 1;
        match tokio::time::timeout(
            std::time::Duration::from_secs(30),
            session.ui_stream_pull_buffer(&attachment.session, handle, 1),
        )
        .await
        .with_context(|| format!("UI phase=stream-pull timeout pull={pulls}"))?
        {
            Ok(buffer) => {
                let batch = parse_output(&buffer)
                    .with_context(|| format!("UI phase=stream-pull parse pull={pulls}"))?;
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
                if !checked_transport_boundary {
                    let first = &frames[0];
                    let request = first
                        .get("request")
                        .and_then(CanonValue::as_str)
                        .context("actual Flow event request missing")?;
                    let plan_digest = first
                        .get("plan_digest")
                        .and_then(CanonValue::as_str)
                        .context("actual Flow event plan digest missing")?;
                    // The guest Flow callee has req_ plus 32 hex entropy digits.
                    assert_eq!(request.len(), 36);
                    assert_eq!(plan_digest.len(), 71);
                    let envelope = canon(&serde_json::json!([{
                        "request":request,
                        "plan_digest":plan_digest,
                        "sequence":3,
                        "kind":{"kind":"step_value","step":"load","value":null},
                    }]))?;
                    let envelope_bytes =
                        tobkiri_canon::encode(&envelope, tobkiri_canon::Variant::StrictV1)?
                            .len()
                            .checked_sub(b"null".len())
                            .context("actual Flow event envelope size underflow")?;
                    assert_eq!(envelope_bytes, 205);
                    assert_eq!(SOURCE_VALUE_BYTES + envelope_bytes, 1_000_025);
                    checked_transport_boundary = true;
                }
            }
            Err(error) => break error,
        }
    };
    assert!(
        checked_transport_boundary,
        "actual Flow transport was never observed"
    );
    let original = match &error {
        CoreError::Cleanup { original, .. } => original.as_ref(),
        error => error,
    };
    assert!(
        matches!(original, CoreError::Step(step) if step.code == "batch-too-large"),
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
    tokio::time::timeout(std::time::Duration::from_secs(30), session.close())
        .await
        .context("UI phase=close timeout")?
        .context("UI phase=close")?;
    Ok(())
}
