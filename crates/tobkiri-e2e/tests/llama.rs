//! Opt-in real Gemma 3 1B acceptance. Never substitutes a standalone HTTP chat.
mod support;
use anyhow::{Context, Result};
use std::{path::PathBuf, time::Duration};
use support::*;
use tobkiri_core::{NativeInputRouteStatus, NativeInputStatus};
use tobkiri_flow::RunEventKind;

fn key_file() -> Result<Vec<u8>> {
    let path = std::env::var_os("TOBKIRI_TEST_LLAMA_KEY_FILE")
        .map(PathBuf::from)
        .unwrap_or_else(|| repo().parent().unwrap().join(".work/llama/api-key.txt"));
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        anyhow::ensure!(
            std::fs::metadata(&path)?.permissions().mode() & 0o777 == 0o600,
            "llama token file must have mode 0600"
        );
    }
    let mut bytes = std::fs::read(path)?;
    while bytes.last().is_some_and(|byte| byte.is_ascii_whitespace()) {
        bytes.pop();
    }
    anyhow::ensure!(!bytes.is_empty(), "llama token file is empty");
    Ok(bytes)
}

/// Read-only receipt inspection from the trusted test, outside guest authority.
/// The secret's name/digest is public; no secret bytes are read from the audit.
fn assert_secret_dispatch_receipts(fixture: &Fixture, fetches: usize) -> Result<()> {
    let relative = tobkiri_store::DataRoot::profile_relative(&fixture.profile.id, "audit.sqlite")?;
    let connection = rusqlite::Connection::open_with_flags(
        fixture.data_root().join(relative),
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    )?;
    let mut statement = connection.prepare("SELECT body FROM audit ORDER BY seq")?;
    let rows = statement
        .query_map([], |row| row.get::<_, Vec<u8>>(0))?
        .map(|row| Ok(serde_json::from_slice::<serde_json::Value>(&row?)?))
        .collect::<Result<Vec<_>>>()?;
    let digest = tobkiri_gate::EffectRequest::Secret {
        name: "api-key".into(),
        reveal: false,
    }
    .digest()?
    .to_string();
    let dispatches = rows
        .iter()
        .filter(|row| row["kind"] == "dispatch" && row["payload"]["effect_digest"] == digest)
        .collect::<Vec<_>>();
    assert_eq!(
        dispatches.len(),
        fetches * 2,
        "each fetch must grant exactly guest get + one fresh host get"
    );
    for dispatch in dispatches {
        let grant = dispatch["payload"]["grant"]
            .as_str()
            .context("secret dispatch has no grant")?;
        let outcomes = rows
            .iter()
            .filter(|row| row["kind"] == "outcome" && row["payload"]["grant"] == grant)
            .collect::<Vec<_>>();
        assert_eq!(
            outcomes.len(),
            1,
            "secret grant must have exactly one terminal outcome"
        );
        assert_eq!(
            outcomes[0]["payload"]["outcome"],
            serde_json::to_value(tobkiri_gate::Outcome::Succeeded)?,
            "each dispatched secret fetch must succeed"
        );
    }
    Ok(())
}

#[tokio::test]
#[ignore = "requires TOBKIRI_TEST_LLAMA=1 and the declared external Gemma 3 1B server"]
async fn real_1b_input_flow_wasm_stream_persist_and_cancel() -> Result<()> {
    anyhow::ensure!(
        std::env::var("TOBKIRI_TEST_LLAMA").as_deref() == Ok("1"),
        "real llama test was not explicitly enabled"
    );
    let mut fixture = Fixture::new("harness-live", None)?;
    // A new private fixture must not reuse another binary's Keychain ACL.
    // This remains the real OS-backed encrypted Vault; no key-provider substitute.
    let identity = tobkiri_canon::Digest::hash(fixture.temp.path().to_string_lossy().as_bytes());
    fixture.profile.id =
        tobkiri_canon::ids::ProfileId::new(format!("prof_live_{}", &identity.hex()[..16]))?;
    if let Some(path) = std::env::var_os("TOBKIRI_TEST_PRIVATE_RECEIPT") {
        use std::io::Write;
        let mut options = std::fs::OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let mut file = options.open(path)?;
        file.write_all(&serde_json::to_vec(&serde_json::json!({
            "format":"tobkiri.private-e2e/1", "owned_profile":fixture.profile.id
        }))?)?;
    }
    assert_eq!(
        fixture
            .profile
            .ceiling
            .network
            .origins
            .iter()
            .collect::<Vec<_>>(),
        vec!["http://127.0.0.1:18080"]
    );
    let secret = key_file()?;
    let core = fixture.core()?;
    core.save_profile(fixture.profile.clone(), None)
        .await
        .context("approve live profile permissions")?;
    // The generic Core user operation is the sole import route for the file bytes.
    core.set_secret(&fixture.profile.id, "api-key", &secret)
        .await
        .context("import opaque live credential")?;
    let session = core
        .start(&fixture.profile.id)
        .await
        .context("instantiate live components")?;
    let events = fixture
        .turn(
            &session,
            "Write one sentence of at least twelve words about a quiet forest.",
        )
        .await?;
    let text = deltas(&events);
    assert!(
        text.len() >= 5,
        "real provider did not emit five text deltas"
    );
    assert!(!text.concat().trim().is_empty());
    assert!(
        events
            .windows(2)
            .all(|pair| pair[0].sequence + 1 == pair[1].sequence)
    );
    assert!(
        events
            .iter()
            .any(|event| matches!(&event.kind,RunEventKind::StepValue{step,..} if step=="save"))
    );
    assert_secret_dispatch_receipts(&fixture, 1)?;
    let published = core.export_profile(&fixture.profile.id)?;
    anyhow::ensure!(
        !published
            .windows(secret.len())
            .any(|window| window == secret),
        "secret leaked into publication"
    );
    assert_no_plaintext(&fixture.data_root(), &secret)?;

    fixture
        .host
        .lines
        .lock()
        .unwrap()
        .push_back("Write a long detailed story with many paragraphs about a quiet forest.".into());
    let input = session
        .read_input(
            fixture.input.clone(),
            fixture.shape.clone(),
            fixture.input_operation.clone(),
        )
        .await?;
    let mut run = session.submit_native_input(input).await?;
    assert_eq!(run.routes().len(), 1);
    let mut first_delta = false;
    let mut cancelled_events = Vec::new();
    while let Some(tagged) =
        tokio::time::timeout(Duration::from_secs(30), run.events.recv()).await?
    {
        assert_eq!(tagged.route_index, 0);
        let event = tagged.event;
        if delta_text(&event).is_some() {
            first_delta = true;
            run.cancel.cancel();
            cancelled_events.push(event);
            break;
        }
        if let RunEventKind::Failed { error } = &event.kind {
            anyhow::bail!("real Flow failed before cancellation: {}", error.code);
        }
        cancelled_events.push(event);
    }
    anyhow::ensure!(first_delta, "real Flow ended before a cancellation delta");
    assert_eq!(
        loaded_message_count(&cancelled_events),
        Some(2),
        "real first turn did not persist into the next Flow's context"
    );
    while let Some(tagged) = tokio::time::timeout(Duration::from_secs(5), run.events.recv()).await?
    {
        assert_eq!(tagged.route_index, 0);
        cancelled_events.push(tagged.event);
    }
    // A cancelled bounded send may drop a progress frame; the actual settled
    // completion and host cleanup below determine cancellation success.
    let completion = run.result.await?;
    assert_native_completion(
        &completion,
        NativeInputStatus::Cancelled,
        &[NativeInputRouteStatus::Cancelled],
    )?;
    assert!(
        !cancelled_events
            .iter()
            .any(|event| matches!(&event.kind,RunEventKind::StepValue{step,..} if step=="save")),
        "cancelled turn reached the save step"
    );
    let mut authorization = reqwest::header::HeaderValue::from_str(&format!(
        "Bearer {}",
        std::str::from_utf8(&secret).context("token must be UTF-8")?
    ))?;
    authorization.set_sensitive(true);
    let client = reqwest::Client::new();
    let deadline = tokio::time::Instant::now() + Duration::from_secs(3);
    loop {
        let slots: serde_json::Value = client
            .get("http://127.0.0.1:18080/slots")
            .header("authorization", authorization.clone())
            .send()
            .await?
            .error_for_status()?
            .json()
            .await?;
        if slots
            .as_array()
            .is_some_and(|slots| slots.iter().all(|slot| slot["is_processing"] == false))
        {
            break;
        }
        anyhow::ensure!(
            tokio::time::Instant::now() < deadline,
            "llama server still processes after cancellation"
        );
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    assert_no_plaintext(&fixture.data_root(), &secret)?;
    assert_secret_dispatch_receipts(&fixture, 2)?;
    // Receipts contain only model identity and counts, never request credentials.
    println!(
        "real-1b receipt: model=gemma3-1b-local plan_digest={} streamed_deltas={} cancelled=true persisted=true secret_dispatches=4 secret_outcomes=4",
        events[0].plan_digest,
        text.len()
    );
    Ok(())
}
