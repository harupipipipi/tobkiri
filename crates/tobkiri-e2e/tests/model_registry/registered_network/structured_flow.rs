//! Model output can select only the tool edge authored in this Flow.

use super::*;
use tobkiri_gate::EffectRequest;

const CALL_ID: &str = "model-call-9";
const INPUT: &str = "Rewrite the word cherry.";
const TEXT: &str = "cherry";
const REPLY: &str = "The rewrite completed.";
const NEXT_INPUT: &str = "Summarize the preceding turn.";
const NEXT_REPLY: &str = "The preceding turn asked for a rewrite.";

fn sse(chunks: &[Value]) -> String {
    let mut wire = String::new();
    for chunk in chunks {
        wire.push_str(&format!("data: {chunk}\n\n"));
    }
    wire.push_str("data: [DONE]\n\n");
    wire
}

async fn run_turn(session: &Session, text: &str) -> Result<Vec<Value>> {
    let ui = admitted_ui(session)?;
    let args = canon(&json!({"input": {"conversation":"one", "text":text}}))?;
    let stream = within_session(
        session,
        "submit structured tool turn",
        session.ui_invoke_received(
            &ui.session,
            &ui.host_session,
            ui.generation.context("missing UI generation")?,
            &ui.receiver,
            USER_INPUT.parse()?,
            "submit".parse()?,
            &args,
        ),
    )
    .await?;
    let stream: Value = serde_json::from_slice(stream.as_bytes())?;
    ensure!(stream["kind"] == "stream");
    let handle = stream["handle"].as_u64().context("missing Flow stream")?;
    let mut frames = Vec::new();
    loop {
        let batch = within_session(
            session,
            "pull structured tool turn",
            session.ui_stream_pull_buffer(&ui.session, handle, 8),
        )
        .await?;
        let batch: Vec<Value> = serde_json::from_slice(batch.as_bytes())?;
        if batch.is_empty() {
            break;
        }
        for frame in batch {
            ensure!(
                frame["kind"]["kind"] != "failed" && frame["kind"]["kind"] != "cancelled",
                "Flow failed: {:?}; prior steps: {:?}",
                frame["kind"]["error"],
                frames
                    .iter()
                    .filter_map(|prior: &Value| Some((
                        prior["kind"]["step"].as_str()?,
                        prior["kind"]["kind"].as_str()?,
                        prior["kind"]["error"]["code"].as_str()
                    )))
                    .collect::<Vec<_>>()
            );
            frames.push(frame);
        }
    }
    Ok(frames)
}

fn audit_rows(root: &Path, profile: &ProfileId) -> Result<Vec<Value>> {
    let path = DataRoot::profile_relative(profile, "audit.sqlite")?;
    let connection = rusqlite::Connection::open_with_flags(
        root.join("data").join(path),
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    )?;
    let mut statement = connection.prepare("SELECT body FROM audit ORDER BY seq")?;
    statement
        .query_map([], |row| row.get::<_, Vec<u8>>(0))?
        .map(|row| Ok(serde_json::from_slice::<Value>(&row?)?))
        .collect::<Result<Vec<_>>>()
}

fn assert_tool_audit(root: &Path, profile: &ProfileId, executed: bool) -> Result<()> {
    let rows = audit_rows(root, profile)?;
    let effect = EffectRequest::PortCall {
        shape: TYPED_TOOL.parse()?,
        op: "rewrite".parse()?,
    }
    .digest()?
    .to_string();
    let arguments = tobkiri_canon::digest(
        &canon(&json!({"text":TEXT}))?,
        tobkiri_canon::Variant::StrictV1,
    )?
    .to_string();
    let dispatches = rows
        .iter()
        .filter(|row| row["kind"] == "dispatch" && row["payload"]["effect_digest"] == effect)
        .collect::<Vec<_>>();
    let tool_executions = rows
        .iter()
        .filter(|row| {
            row["kind"] == "guest-event" && row["payload"]["kind"] == "typed-tool-rewrite"
        })
        .collect::<Vec<_>>();
    assert_eq!(dispatches.len(), usize::from(executed));
    assert_eq!(tool_executions.len(), usize::from(executed));
    if executed {
        assert_eq!(dispatches[0]["principal"]["pack"], UI);
        assert_eq!(dispatches[0]["payload"]["args_digest"], arguments);
        assert_eq!(
            tool_executions[0]["principal"]["pack"],
            "local.acceptance-typed-tool"
        );
    }
    Ok(())
}

async fn replay_tool_turn(tool: &str, expected: &str, executed: bool) -> Result<()> {
    let first_sse = sse(&[
        json!({"choices":[{"delta":{
            "content":"I will rewrite it.", "reasoning_content":"A tool can do this.",
            "tool_calls":[{"index":0,"id":CALL_ID,"type":"function","function":{"name":tool,"arguments":"{\"text\":"}}]
        }}]}),
        json!({"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\"cherry\"}"}}]},"finish_reason":"tool_calls"}]}),
        json!({"choices":[],"usage":{"prompt_tokens":21,"completion_tokens":6,"prompt_tokens_details":{"cached_tokens":12}}}),
    ]);
    let second_sse = sse(&[
        json!({"choices":[{"delta":{"content":REPLY},"finish_reason":"stop"}]}),
        json!({"choices":[],"usage":{"prompt_tokens":32,"completion_tokens":7}}),
    ]);
    let next_sse = sse(&[json!({
        "choices":[{"delta":{"content":NEXT_REPLY},"finish_reason":"stop"}]
    })]);
    let listener = TcpListener::bind("127.0.0.1:0")?;
    let origin = format!("http://{}", listener.local_addr()?);
    let root = physical_temp()?;
    let (core, start) =
        structured_flow_core(root.path(), std::slice::from_ref(&origin), 10_000).await?;
    let session = core.start_bound(&start).await?;
    let request = async {
        let revision =
            save_connection(&session, &start.profile_id, "one", "Local test", &origin, 0).await?;
        save_model(
            &session,
            &json!({
                "model_profile_id":"registered-one", "model_id":"llama/one",
                "provider_instance_id":"provider.one", "display_name":"Local test",
                "expected_revision":0, "provider_registry_revision":revision,
                "max_output_tokens":128,
            }),
        )
        .await?;
        let server = thread::spawn(move || -> Result<_> {
            Ok([
                api(&listener, &first_sse)?,
                api(&listener, &second_sse)?,
                api(&listener, &next_sse)?,
            ])
        });
        Ok::<_, anyhow::Error>((run_turn(&session, INPUT).await?, server))
    }
    .await;
    let closed = session.close().await;
    let (frames, server) = request.with_context(|| {
        let rows = audit_rows(root.path(), &start.profile_id).unwrap_or_default();
        let denied = rows
            .iter()
            .filter(|row| row["kind"] == "denied")
            .map(|row| (&row["principal"], &row["payload"]))
            .collect::<Vec<_>>();
        format!("structured tool Flow did not finish; Gate denials {denied:?}")
    })?;
    closed?;
    assert_committed(&frames);
    assert!(frames.iter().any(|frame| {
        frame["kind"]["kind"] == "emitted"
            && frame["kind"]["shape"] == ASSISTANT_OUTPUT
            && frame["kind"]["value"] == json!({"tag":"usage","val":{"prompt-tokens":21,"completion-tokens":6,"cached-tokens":12}})
    }));

    let renewed = core.start_bound(&start).await?;
    let continued = async {
        let arguments = json!({"conversation":"one"});
        let structured = call(&renewed, STRUCTURED_CHAT_STORE, "load", arguments.clone()).await?;
        let legacy = call(&renewed, CHAT_STORE, "load", arguments.clone()).await?;
        let expected_structured = json!([
            {"role":"user","parts":[{"tag":"text","val":INPUT}],"response":null},
            {"role":"assistant","parts":[
                {"tag":"reasoning","val":"A tool can do this."},
                {"tag":"text","val":"I will rewrite it."},
                {"tag":"tool-call","val":{"id":CALL_ID,"name":tool,"arguments":{"text":TEXT}}}
            ],"response":{"finish":{"tag":"tool-calls"},"usage":{"prompt-tokens":21,"completion-tokens":6,"cached-tokens":12}}},
            {"role":"tool","parts":[{"tag":"tool-result","val":{"call-id":CALL_ID,"content":expected,"is-error":!executed}}],"response":null},
            {"role":"assistant","parts":[{"tag":"text","val":REPLY}],"response":{"finish":{"tag":"stop"},"usage":{"prompt-tokens":32,"completion-tokens":7,"cached-tokens":null}}}
        ]);
        assert_eq!(structured, json!({"conversation":"one","messages":expected_structured}));
        assert_eq!(legacy, json!({"conversation":"one","messages":[
            {"role":"user","content":INPUT},{"role":"assistant","content":REPLY}
        ]}));
        let next_frames = run_turn(&renewed, NEXT_INPUT).await?;
        assert_committed(&next_frames);
        let updated = call(&renewed, STRUCTURED_CHAT_STORE, "load", arguments).await?;
        let updated = updated["messages"].as_array().context("updated conversation")?;
        assert_eq!(updated.len(), 6);
        assert_eq!(&updated[..4], expected_structured.as_array().unwrap());
        assert_eq!(updated[4..], [
            json!({"role":"user","parts":[{"tag":"text","val":NEXT_INPUT}],"response":null}),
            json!({"role":"assistant","parts":[{"tag":"text","val":NEXT_REPLY}],"response":{"finish":{"tag":"stop"},"usage":null}}),
        ]);
        Ok::<_, anyhow::Error>(())
    }.await;
    let closed = renewed.close().await;
    continued?;
    closed?;
    assert_tool_audit(root.path(), &start.profile_id, executed)?;

    let [
        (first_path, first),
        (second_path, second),
        (third_path, third),
    ] = server
        .join()
        .map_err(|_| anyhow::anyhow!("structured tool loopback server failed"))??;
    assert_eq!(first_path, "/v1/chat/completions");
    assert_eq!(second_path, first_path);
    assert_eq!(third_path, first_path);
    for request in [&first, &second, &third] {
        assert_eq!(request["model"], "llama/one");
        assert_eq!(request["max_tokens"], 128);
        assert_eq!(
            request["tools"].as_array().context("offered tools")?.len(),
            1
        );
        assert_eq!(request["tools"][0]["function"]["name"], "rewrite");
    }
    assert_eq!(first["messages"], json!([{"role":"user","content":INPUT}]));
    assert_eq!(
        second["messages"],
        json!([
            {"role":"user","content":INPUT},
            {"role":"assistant","content":"I will rewrite it.","reasoning_content":"A tool can do this.",
             "tool_calls":[{"id":CALL_ID,"type":"function","function":{"name":tool,"arguments":"{\"text\":\"cherry\"}"}}]},
            {"role":"tool","tool_call_id":CALL_ID,"content":expected},
        ])
    );
    let mut expected_next = second["messages"]
        .as_array()
        .context("second request messages")?
        .clone();
    expected_next.extend([
        json!({"role":"assistant","content":REPLY}),
        json!({"role":"user","content":NEXT_INPUT}),
    ]);
    assert_eq!(third["messages"], json!(expected_next));
    Ok(())
}

fn assert_committed(frames: &[Value]) {
    assert_eq!(
        frames
            .iter()
            .filter(|frame| frame["kind"]["kind"] == "completed")
            .count(),
        1
    );
    assert!(frames.iter().any(|frame| {
        frame["kind"]["kind"] == "step_value" && frame["kind"]["step"] == "save-turn"
    }));
}

#[tokio::test]
#[ignore = "requires the source-built Harness Flow, typed tool, provider and staged Registry Packs"]
async fn model_calls_bound_tool_and_receives_actual_result() -> Result<()> {
    replay_tool_turn("rewrite", "tool-cherry", true).await
}

#[tokio::test]
#[ignore = "requires the source-built Harness Flow, typed tool, provider and staged Registry Packs"]
async fn unoffered_tool_returns_error_without_dispatch() -> Result<()> {
    replay_tool_turn("unknown_tool", "Tool is unavailable.", false).await
}
