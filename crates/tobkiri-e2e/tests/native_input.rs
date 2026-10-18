//! Actual physical reads remain bound to their frozen entry and Session.
mod support;
use anyhow::{Context, Result};
use std::{path::Path, time::Duration};
use support::*;
use tobkiri_canon::{
    Digest,
    ids::{InputSourceId, OpName, ShapeRef},
};
use tobkiri_core::{NativeInputRouteStatus, NativeInputRunHandle, NativeInputStatus};
use tobkiri_flow::{RunEvent, RunEventKind};
use tobkiri_gate::{EffectRequest, Outcome};

fn native_fanout_source(fixture: &mut Fixture) -> Result<()> {
    let pin = fixture
        .profile
        .packs
        .iter_mut()
        .find(|pin| pin.id.as_str() == fixture.input.as_str())
        .context("fanout Input Pack missing")?;
    let source = Path::new(
        pin.source
            .strip_prefix("file:")
            .context("fanout Input fixture is not local")?,
    )
    .canonicalize()?;
    anyhow::ensure!(source.starts_with(fixture.temp.path().canonicalize()?));
    let previous = tobkiri_manifest::verify_directory(&source, &Default::default())?;
    anyhow::ensure!(previous.hash == pin.sha256, "fanout Input pin was stale");
    let mut manifest: serde_json::Value =
        serde_json::from_slice(&std::fs::read(source.join("pack.json"))?)?;
    let consumes = manifest["ports"]["consumes"]
        .as_array_mut()
        .context("fanout Input consumes missing")?;
    let dynamic = consumes
        .iter()
        .enumerate()
        .filter(|(_, port)| {
            port["shape"] == "local.echo-chat.turn-flow" && port["version"] == "1.0"
        })
        .map(|(index, _)| index)
        .collect::<Vec<_>>();
    let [index] = dynamic.as_slice() else {
        anyhow::bail!("fanout fixture requires exactly one dynamic Flow consume");
    };
    // These tests invoke read, then route its receipt through Core's exact
    // frozen Flow keys. They never invoke the guest's shape-only submit path,
    // whose consume must resolve uniquely. Keep its Flow permission ceiling
    // intact so the actual NativeInput routes still need that authorization.
    consumes.remove(*index);
    std::fs::write(
        source.join("pack.json"),
        serde_json::to_vec_pretty(&manifest)?,
    )?;
    let verified = tobkiri_manifest::verify_directory(&source, &Default::default())?;
    assert_eq!(verified.manifest.id, pin.id);
    assert_eq!(verified.manifest.version, pin.version);
    pin.sha256 = verified.hash;
    Ok(())
}

/// Two actual manifest-declared Flows share an existing implements view. The
/// normal verifier/compiler creates both targets; this test mints no witness.
fn fanout_fixture(first_fails: bool) -> Result<Fixture> {
    let mut fixture = Fixture::new("echo", None)?;
    native_fanout_source(&mut fixture)?;
    let pin = fixture
        .profile
        .packs
        .iter()
        .find(|pin| pin.id.as_str() == "local.echo-chat")
        .context("echo Flow Pack missing")?;
    let source = Path::new(
        pin.source
            .strip_prefix("file:")
            .context("Flow fixture is not local")?,
    )
    .canonicalize()?;
    anyhow::ensure!(source.starts_with(fixture.temp.path().canonicalize()?));
    let original: serde_json::Value =
        serde_json::from_slice(&std::fs::read(source.join("flows/turn.flow.json"))?)?;
    if first_fails {
        let mut failing = original.clone();
        // The real prompt-builder rejects ctx.conversation != input.conversation
        // with ErrorCode::Invalid before generation or persistence can start.
        failing["steps"][1]["call"]["args"]["ctx"] = serde_json::json!({
            "conversation":"another-conversation", "messages":[],
        });
        fixture.replace_pack_json("local.echo-chat", "flows/turn.flow.json", &failing)?;
    }
    let mut second = original;
    second["id"] = serde_json::json!("zz_followup");
    let relative = "flows/zz-followup.flow.json";
    let bytes = serde_json::to_vec_pretty(&second)?;
    let mut manifest: serde_json::Value =
        serde_json::from_slice(&std::fs::read(source.join("pack.json"))?)?;
    manifest["flows"]
        .as_array_mut()
        .context("manifest Flows missing")?
        // CompiledInputEntry freezes Flow-key order, independently of manifest
        // declaration order. Put zz first so the assertions distinguish them.
        .insert(0, serde_json::json!(relative));
    manifest["files"]
        .as_array_mut()
        .context("manifest files missing")?
        .push(
            serde_json::json!({"path":relative,"size":bytes.len(),"sha256":Digest::hash(&bytes)}),
        );
    std::fs::write(source.join(relative), bytes)?;
    std::fs::write(
        source.join("pack.json"),
        serde_json::to_vec_pretty(&manifest)?,
    )?;
    let verified = tobkiri_manifest::verify_directory(&source, &Default::default())?;
    let pin = fixture
        .profile
        .packs
        .iter_mut()
        .find(|pin| pin.id.as_str() == "local.echo-chat")
        .context("echo Flow pin missing")?;
    assert_eq!(verified.manifest.id, pin.id);
    assert_eq!(verified.manifest.version, pin.version);
    pin.sha256 = verified.hash;
    Ok(fixture)
}

fn assert_exact_routes(run: &NativeInputRunHandle) -> Result<Vec<Digest>> {
    let routes = run.routes();
    assert_eq!(routes.len(), 2);
    assert_eq!(routes[0].target.flow_key, "local.echo-chat/turn");
    assert_eq!(routes[1].target.flow_key, "local.echo-chat/zz_followup");
    assert_eq!(routes[0].implements, routes[1].implements);
    assert_eq!(routes[0].target.owner, routes[1].target.owner);
    assert_eq!(routes[0].target.owner_hash, routes[1].target.owner_hash);
    assert_ne!(routes[0].target.digest()?, routes[1].target.digest()?);
    routes
        .iter()
        .map(|route| {
            Ok(EffectRequest::FlowRun {
                implements: route.implements.clone(),
                flow_owner: (route.target.owner.clone(), route.target.owner_hash.clone()),
                flow_key: route.target.flow_key.clone(),
                target_digest: route.target.digest()?,
            }
            .digest()?)
        })
        .collect()
}

fn assert_flow_run_receipts(
    fixture: &Fixture,
    effects: &[Digest],
    outcomes: &[Option<Outcome>],
) -> Result<()> {
    assert_eq!(effects.len(), outcomes.len());
    let rows = audit_rows(fixture)?;
    for (effect, expected) in effects.iter().zip(outcomes) {
        let dispatches = rows
            .iter()
            .filter(|row| {
                row["kind"] == "dispatch"
                    && row["payload"]["effect_digest"].as_str() == Some(effect.as_str())
            })
            .collect::<Vec<_>>();
        assert_eq!(
            dispatches.len(),
            usize::from(expected.is_some()),
            "each exact Flow target needs its own dispatch"
        );
        for dispatch in dispatches {
            let grant = dispatch["payload"]["grant"]
                .as_str()
                .context("Flow grant missing")?;
            let terminal = rows
                .iter()
                .filter(|row| {
                    row["kind"] == "outcome" && row["payload"]["grant"].as_str() == Some(grant)
                })
                .collect::<Vec<_>>();
            assert_eq!(
                terminal.len(),
                1,
                "a FlowRun grant must have exactly one durable outcome"
            );
            assert_eq!(
                terminal[0]["payload"]["outcome"],
                serde_json::to_value(expected)?
            );
        }
    }
    Ok(())
}

fn audit_rows(fixture: &Fixture) -> Result<Vec<serde_json::Value>> {
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
    Ok(rows)
}

fn persisted_message_count(fixture: &Fixture) -> Result<i64> {
    let path = fixture
        .data_root()
        .join("profiles")
        .join(fixture.profile.id.as_str())
        .join("data/local.echo-store/private.sqlite");
    if !path.is_file() {
        return Ok(0);
    }
    let connection =
        rusqlite::Connection::open_with_flags(path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    Ok(connection.query_row("SELECT count(*) FROM messages", [], |row| row.get(0))?)
}

async fn start_fanout(
    fixture: &Fixture,
    text: &str,
) -> Result<(
    tobkiri_core::Core,
    tobkiri_core::Session,
    NativeInputRunHandle,
)> {
    let (core, session) = fixture.start().await?;
    fixture.host.lines.lock().unwrap().push_back(text.into());
    let input = within_session(
        &session,
        "fanout: physical Input read",
        session.read_input(
            fixture.input.clone(),
            fixture.shape.clone(),
            fixture.input_operation.clone(),
        ),
    )
    .await?;
    let run = within_session(
        &session,
        "fanout: NativeInput admission",
        session.submit_native_input(input),
    )
    .await?;
    Ok((core, session, run))
}

#[tokio::test]
async fn distinct_same_view_routes_continue_after_a_real_provider_failure() -> Result<()> {
    let fixture = fanout_fixture(true)?;
    let (_core, session, mut run) = start_fanout(&fixture, "the successful second route").await?;
    let effects = assert_exact_routes(&run)?;
    assert_ne!(effects[0], effects[1]);
    let plans = run
        .routes()
        .iter()
        .map(|route| route.target.plan_digest.clone())
        .collect::<Vec<_>>();
    let mut events: [Vec<RunEvent>; 2] = std::array::from_fn(|_| Vec::new());
    let mut previous_route = 0;
    let deadline = tokio::time::Instant::now() + Duration::from_secs(30);
    while let Some(tagged) =
        next_native_event(&mut run, "fanout failure: Flow progress", deadline).await?
    {
        assert!(tagged.route_index < 2);
        assert!(
            tagged.route_index >= previous_route,
            "sequential fanout reordered its routes"
        );
        previous_route = tagged.route_index;
        assert_eq!(tagged.event.plan_digest, plans[tagged.route_index as usize]);
        events[tagged.route_index as usize].push(tagged.event);
    }
    let completion = native_completion(&mut run, "fanout failure: NativeInput completion")
        .await
        .with_context(|| {
            format!(
                "fanout failure: failure_events={:?}",
                failure_events(events.iter().flatten())
            )
        })?;
    let envelope = assert_native_completion(
        &completion,
        NativeInputStatus::Failed,
        &[
            NativeInputRouteStatus::Failed,
            NativeInputRouteStatus::Succeeded,
        ],
    )
    .with_context(|| {
        format!(
            "fanout failure: failure_events={:?}",
            failure_events(events.iter().flatten())
        )
    })?;
    assert_eq!(completion.routes[0].error_code, Some("invalid"));
    assert_eq!(
        envelope
            .get("routes")
            .and_then(|value| value.as_array())
            .and_then(|routes| routes[0].get("result")),
        Some(&tobkiri_canon::CanonValue::Null)
    );
    assert!(events[0].iter().any(
        |event| matches!(&event.kind, RunEventKind::Failed { error } if error.code == "invalid")
    ));
    assert!(!events[0].iter().any(
        |event| matches!(&event.kind, RunEventKind::StepValue { step, .. } if step == "save")
    ));
    assert!(
        events[1]
            .iter()
            .any(|event| matches!(&event.kind, RunEventKind::Completed { .. }))
    );
    assert_eq!(
        deltas(&events[1]).concat(),
        "Echo: the successful second route"
    );
    assert_eq!(loaded_message_count(&events[1]), Some(0));
    for route in &events {
        assert!(
            route
                .windows(2)
                .all(|pair| pair[0].sequence + 1 == pair[1].sequence)
        );
    }
    assert_flow_run_receipts(
        &fixture,
        &effects,
        &[
            Some(Outcome::Failed {
                code: "invalid".into(),
            }),
            Some(Outcome::Succeeded),
        ],
    )?;
    tokio::time::timeout(Duration::from_secs(30), session.close())
        .await
        .context("fanout failure: Session close timed out")??;
    Ok(())
}

#[tokio::test]
async fn parent_cancellation_preserves_the_unstarted_frozen_route() -> Result<()> {
    let fixture = fanout_fixture(false)?;
    // More events than both real queues can retain prevents the first route
    // from completing before its first delta has been observed and cancelled.
    let (_core, session, mut run) = start_fanout(&fixture, &"c".repeat(256)).await?;
    let effects = assert_exact_routes(&run)?;
    let mut cancelled = false;
    let mut events = Vec::new();
    let deadline = tokio::time::Instant::now() + Duration::from_secs(30);
    while let Some(tagged) = next_native_event(
        &mut run,
        "parent cancellation: wait for first delta",
        deadline,
    )
    .await?
    {
        assert_eq!(
            tagged.route_index, 0,
            "the next route started before cancellation"
        );
        if delta_text(&tagged.event).is_some() {
            run.cancel.cancel();
            cancelled = true;
            events.push(tagged.event);
            break;
        }
        events.push(tagged.event);
    }
    let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    while let Some(tagged) = next_native_event(
        &mut run,
        "parent cancellation: drain original run",
        deadline,
    )
    .await?
    {
        assert_eq!(tagged.route_index, 0, "an unstarted route emitted progress");
        events.push(tagged.event);
    }
    let completion = native_completion(&mut run, "parent cancellation: NativeInput completion")
        .await
        .with_context(|| {
            format!(
                "parent cancellation: failure_events={:?}",
                failure_events(&events)
            )
        })?;
    anyhow::ensure!(
        cancelled,
        "parent cancellation: no cancellable delta; status={:?}; routes={:?}; failure_events={:?}",
        completion.status,
        completion.routes,
        failure_events(&events),
    );
    assert_native_completion(
        &completion,
        NativeInputStatus::Cancelled,
        &[
            NativeInputRouteStatus::Cancelled,
            NativeInputRouteStatus::Unstarted,
        ],
    )
    .with_context(|| {
        format!(
            "parent cancellation: failure_events={:?}",
            failure_events(&events)
        )
    })?;
    // Completion is authoritative even if cancellation wins a blocked event
    // send. Draining actual child cleanup must not depend on delivering a
    // priority terminal event through a full progress queue.
    assert!(!events.iter().any(
        |event| matches!(&event.kind, RunEventKind::StepValue { step, .. } if step == "save")
    ));
    assert_flow_run_receipts(&fixture, &effects, &[Some(Outcome::Cancelled), None])?;
    tokio::time::timeout(Duration::from_secs(30), session.close())
        .await
        .context("parent cancellation: Session close timed out")??;
    Ok(())
}

#[tokio::test]
async fn undeclared_read_cannot_consume_input_and_closed_receipt_cannot_route() -> Result<()> {
    let fixture = Fixture::new("echo", None)?;
    let (core, session) = fixture.start().await?;
    fixture
        .host
        .lines
        .lock()
        .unwrap()
        .push_back("one physical source read".into());
    for (source, view, operation) in [
        (
            fixture.input.clone(),
            fixture.shape.clone(),
            OpName::parse("submit")?,
        ),
        (
            InputSourceId::parse("local.echo-llm")?,
            fixture.shape.clone(),
            fixture.input_operation.clone(),
        ),
        (
            fixture.input.clone(),
            ShapeRef::parse("local.echo-chat.llm-provider@1.0")?,
            fixture.input_operation.clone(),
        ),
    ] {
        assert!(
            within_session(
                &session,
                "undeclared Input: reject source/view/operation",
                session.read_input(source, view, operation),
            )
            .await
            .is_err(),
            "an undeclared source/view/operation was treated as the frozen Input entry"
        );
        assert_eq!(fixture.host.lines.lock().unwrap().len(), 1);
    }
    let input = within_session(
        &session,
        "undeclared Input: declared physical read",
        session.read_input(
            fixture.input.clone(),
            fixture.shape.clone(),
            fixture.input_operation.clone(),
        ),
    )
    .await?;
    assert!(fixture.host.lines.lock().unwrap().is_empty());
    tokio::time::timeout(Duration::from_secs(30), session.close())
        .await
        .context("closed receipt: source Session close timed out")??;
    assert!(
        within_session(
            &session,
            "closed receipt: reject NativeInput admission",
            session.submit_native_input(input),
        )
        .await
        .is_err(),
        "a closed source Session retained routing authority"
    );
    let replacement =
        tokio::time::timeout(Duration::from_secs(30), core.start(&fixture.profile.id))
            .await
            .context("closed receipt: replacement Session start timed out")??;
    let events = fixture.turn(&replacement, "replacement input").await?;
    assert_eq!(deltas(&events).concat(), "Echo: replacement input");
    tokio::time::timeout(Duration::from_secs(30), replacement.close())
        .await
        .context("closed receipt: replacement Session close timed out")??;
    Ok(())
}

#[tokio::test]
async fn another_session_cannot_route_the_original_source_read_receipt() -> Result<()> {
    let fixture = Fixture::new("echo", None)?;
    let (core, source) = fixture.start().await?;
    let other = tokio::time::timeout(Duration::from_secs(30), core.start(&fixture.profile.id))
        .await
        .context("foreign receipt: second Session start timed out")??;
    fixture
        .host
        .lines
        .lock()
        .unwrap()
        .push_back("bound to the reading Session".into());
    let input = within_session(
        &source,
        "foreign receipt: source physical read",
        source.read_input(
            fixture.input.clone(),
            fixture.shape.clone(),
            fixture.input_operation.clone(),
        ),
    )
    .await?;
    let before_audit = audit_rows(&fixture)?.len();
    let before_messages = persisted_message_count(&fixture)?;
    let before_output = fixture.host.output.lock().unwrap().len();
    let routed = within_session(
        &other,
        "foreign receipt: NativeInput admission",
        other.submit_native_input(input),
    )
    .await;
    let mut events = Vec::new();
    let mut flow_effects = Vec::new();
    let (rejected, terminal) = match routed {
        Err(_) => (true, "rejected during admission".into()),
        Ok(mut run) => {
            flow_effects = run
                .routes()
                .iter()
                .map(|route| {
                    Ok(EffectRequest::FlowRun {
                        implements: route.implements.clone(),
                        flow_owner: (route.target.owner.clone(), route.target.owner_hash.clone()),
                        flow_key: route.target.flow_key.clone(),
                        target_digest: route.target.digest()?,
                    }
                    .digest()?)
                })
                .collect::<Result<Vec<Digest>>>()?;
            // A returned handle alone is not proof of successful routing. Let
            // this exact run settle without masking its behavior by cancelling.
            let deadline = tokio::time::Instant::now() + Duration::from_secs(30);
            while let Some(tagged) =
                next_native_event(&mut run, "foreign receipt: Flow progress", deadline).await?
            {
                events.push(tagged.event);
            }
            match native_result(&mut run, "foreign receipt: NativeInput completion").await? {
                Ok(completion) => (
                    completion.status == NativeInputStatus::Failed
                        && !completion.routes.is_empty()
                        && completion.routes.iter().all(|route| {
                            matches!(
                                route.status,
                                NativeInputRouteStatus::Failed | NativeInputRouteStatus::Denied
                            ) && matches!(route.error_code, Some("invalid" | "denied"))
                        }),
                    format!(
                        "status={:?} routes={:?}",
                        completion.status, completion.routes
                    ),
                ),
                Err(error) => (
                    matches!(error.audit_code(), "invalid" | "denied"),
                    format!("error_code={}", error.audit_code()),
                ),
            }
        }
    };
    let rows = audit_rows(&fixture)?;
    let dispatches = rows
        .iter()
        .skip(before_audit)
        .filter(|row| row["kind"] == "dispatch")
        .collect::<Vec<_>>();
    let flow_dispatches = dispatches
        .iter()
        .filter(|row| {
            flow_effects
                .iter()
                .any(|effect| row["payload"]["effect_digest"].as_str() == Some(effect.as_str()))
        })
        .count();
    let provider_dispatches = dispatches
        .iter()
        .filter(|row| matches!(row["principal"]["kind"].as_str(), Some("pack" | "pack-ui")))
        .count();
    let stored_messages = persisted_message_count(&fixture)?;
    let output_buffers = fixture.host.output.lock().unwrap().len();
    anyhow::ensure!(
        rejected
            && events.is_empty()
            && dispatches.is_empty()
            && stored_messages == before_messages
            && output_buffers == before_output,
        "foreign source receipt crossed its Session boundary: {terminal}; event_count={}; failure_events={:?}; new_dispatches={}; flow_dispatches={flow_dispatches}; provider_dispatches={provider_dispatches}; stored_messages_before={before_messages}; stored_messages_after={stored_messages}; output_buffers_before={before_output}; output_buffers_after={output_buffers}",
        events.len(),
        failure_events(&events),
        dispatches.len(),
    );
    let events = fixture.turn(&other, "its own physical read").await?;
    assert_eq!(deltas(&events).concat(), "Echo: its own physical read");
    tokio::time::timeout(Duration::from_secs(30), source.close())
        .await
        .context("foreign receipt: source Session close timed out")??;
    tokio::time::timeout(Duration::from_secs(30), other.close())
        .await
        .context("foreign receipt: second Session close timed out")??;
    Ok(())
}
