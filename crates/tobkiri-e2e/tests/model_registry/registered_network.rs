//! Authenticated streaming uses the saved route at each invocation.

use super::*;
use std::{
    collections::BTreeMap,
    fs,
    io::{BufRead, BufReader, Read, Write},
    net::{Ipv4Addr, SocketAddrV4, TcpListener},
    path::Path,
    thread,
    time::{Duration, Instant},
};
use support::registry::{PROVIDER_COMMIT, PROVIDER_CONFIGURATION};
use tobkiri_canon::ids::ProfileId;
use tobkiri_core::{ProfileStartBinding, RuntimeOwner, UiAttachment};
use tobkiri_manifest::{
    PackManifest, PortDeclaration, Ports, RuntimeConfig, RuntimeSpec, ShapeDefinition, ShapeKind,
    UiDefinition,
};

const LLM: &str = "local.harness-chat.llm-provider@1.0";
const MODEL_ROUTE: &str = "local.model-registry.routing@1.0";
const PROVIDER_ROUTE: &str = "local.provider-registry.routing@1.0";
const SAVED_PACK: &str = "local.saved-conversation";
const SAVED_MODEL: &str = "local.saved-conversation.model@1.0";
const SAVED_DATA: &str = "local.saved-conversation.data@1.0";
const CONVERSATION_FIXTURE: &str = include_str!("../../fixtures/saved-conversation-owner.json");
const UI: &str = "local.harness-chat";

fn ports(shape: &str) -> Result<PortDeclaration> {
    let value = tobkiri_canon::ids::ShapeRef::parse(shape)?;
    Ok(PortDeclaration {
        shape: value.name,
        version: value.version,
        optional: false,
    })
}

fn manifest(id: &str, runtime: RuntimeSpec, wall_ms: u64) -> Result<PackManifest> {
    let mut permissions = registry::store_permissions();
    permissions.wasm.wall_ms = wall_ms;
    permissions.loops.max_wall_ms = wall_ms;
    Ok(PackManifest {
        format: "tobkiri.pack/1".into(),
        id: id.parse()?,
        version: "0.1.0".into(),
        publisher_key: None,
        runtime,
        shapes: vec![],
        ports: Ports::default(),
        inputs: vec![],
        flows: vec![],
        overlays: vec![],
        stores: vec![],
        permissions,
        ui: None,
        files: vec![],
    })
}

fn streaming_packs(root: &Path, origins: &[String], wall_ms: u64) -> Result<[RegistryPack; 2]> {
    let artifact = std::env::var_os("TOBKIRI_TEST_REGISTERED_OPENAI_COMPONENT")
        .context("source-built registered OpenAI component required")?;
    let repository = support::repo();
    let chat_wit = fs::read(repository.join("packs/shapes/harness-chat.wit"))?;
    let core = fs::read(repository.join("crates/tobkiri-wit/wit/core/core.wit"))?;
    let mut ui = manifest(UI, RuntimeSpec::Data, wall_ms)?;
    let llm = ports(LLM)?;
    ui.shapes.push(ShapeDefinition {
        name: llm.shape.clone(),
        version: llm.version,
        kind: ShapeKind::Port,
        wit: "wit/harness-chat.wit".into(),
    });
    ui.ports.consumes.push(llm.clone());
    ui.permissions.ports.insert(LLM.into());
    ui.ui = Some(UiDefinition {
        entry: "ui/index.html".into(),
        ports_used: vec![LLM.parse()?],
    });
    let ui = registry::materialize(
        root,
        ui,
        BTreeMap::from([
            ("wit/harness-chat.wit".into(), chat_wit.clone()),
            ("wit/deps/core.wit".into(), core.clone()),
            (
                "wit/llm-provider.shape.json".into(),
                br#"{"cardinality":"one","streams":{"chat":"chat-event"},"callbacks":{}}"#.to_vec(),
            ),
            (
                "ui/index.html".into(),
                b"<!doctype html><title>Registered chat acceptance</title>".to_vec(),
            ),
        ]),
    )?;
    let mut openai = manifest(
        "local.openai-compat-provider",
        RuntimeSpec::Wasm {
            component: "component.wasm".into(),
            toolchain: "rust/1.94.0".into(),
            core_modules: BTreeMap::new(),
            config: Some(RuntimeConfig {
                path: "config.json".into(),
                shape: None,
            }),
        },
        wall_ms,
    )?;
    openai.ports.provides.push(llm);
    openai.ports.consumes = vec![ports(MODEL_ROUTE)?, ports(PROVIDER_ROUTE)?];
    openai
        .permissions
        .ports
        .extend([MODEL_ROUTE.into(), PROVIDER_ROUTE.into()]);
    openai
        .permissions
        .network
        .origins
        .extend(origins.iter().cloned());
    let openai = registry::materialize(
        root,
        openai,
        BTreeMap::from([
            ("component.wasm".into(), fs::read(artifact)?),
            (
                "config.json".into(),
                serde_json::to_vec(&json!({"registered_models":true,"timeout_ms":20000}))?,
            ),
            (
                "wit/pack.wit".into(),
                fs::read(repository.join("packs/local.openai-compat-provider/wit/pack.wit"))?,
            ),
            ("wit/deps/harness-chat.wit".into(), chat_wit),
            ("wit/deps/core.wit".into(), core),
            (
                "wit/deps/core-v1-2.wit".into(),
                fs::read(repository.join("crates/tobkiri-wit/wit/core-v1-2/core.wit"))?,
            ),
            (
                "wit/deps/model-registry.wit".into(),
                fs::read(repository.join("packs/local.model-registry-rust/wit/pack.wit"))?,
            ),
            (
                "wit/deps/provider-registry.wit".into(),
                fs::read(repository.join("packs/local.provider-registry-rust/wit/pack.wit"))?,
            ),
        ]),
    )?;
    Ok([ui, openai])
}

fn api(listener: TcpListener) -> Result<(String, Value)> {
    listener.set_nonblocking(true)?;
    let deadline = Instant::now() + Duration::from_secs(30);
    let mut socket = loop {
        match listener.accept() {
            Ok((socket, _)) => break socket,
            Err(error)
                if error.kind() == std::io::ErrorKind::WouldBlock && Instant::now() < deadline =>
            {
                thread::sleep(Duration::from_millis(10));
                continue;
            }
            Err(error) => return Err(error.into()),
        }
    };
    socket.set_read_timeout(Some(Duration::from_secs(5)))?;
    socket.set_write_timeout(Some(Duration::from_secs(5)))?;
    let mut reader = BufReader::new(&mut socket);
    let mut line = String::new();
    reader.read_line(&mut line)?;
    let path = line
        .split_whitespace()
        .nth(1)
        .context("missing request URI")?
        .to_owned();
    let mut length = None;
    loop {
        line.clear();
        ensure!(reader.read_line(&mut line)? != 0, "unexpected header EOF");
        if line == "\r\n" {
            break;
        }
        if let Some((name, value)) = line.split_once(':') {
            if name.eq_ignore_ascii_case("content-length") {
                length = Some(value.trim().parse::<usize>()?);
            }
            ensure!(
                !name.eq_ignore_ascii_case("authorization"),
                "keyless route sent authorization"
            );
        }
    }
    let length = length.context("missing content length")?;
    ensure!(length <= 64 * 1024);
    let mut body = vec![0; length];
    reader.read_exact(&mut body)?;
    let request = serde_json::from_slice(&body)?;
    let sse = concat!(
        "data: {\"choices\":[{\"delta\":{\"content\":\"local reply\"}}]}\n\n",
        "data: [DONE]\n\n"
    );
    write!(
        socket,
        "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{sse}",
        sse.len()
    )?;
    socket.flush()?;
    Ok((path, request))
}

async fn registered_core(
    root: &Path,
    origins: &[String],
    wall_ms: u64,
) -> Result<(Core, ProfileStartBinding)> {
    let [model, provider] = packs()?;
    let [ui, openai] = streaming_packs(root, origins, wall_ms)?;
    let saved = registry::pack(SAVED_PACK)?;
    let owner = saved.pin.clone();
    let mut profile = registry::profile(
        "prof_registered_conversation",
        &[model, provider, ui, openai, saved],
        &[
            SETTINGS,
            PROVIDER_CONFIGURATION,
            PROVIDER_COMMIT,
            SAVED_MODEL,
        ],
    )?;
    profile.startup.ui = Some(UI.parse()?);
    profile
        .ceiling
        .network
        .origins
        .extend(origins.iter().cloned());
    profile.ceiling.wasm.wall_ms = wall_ms;
    profile.ceiling.loops.max_wall_ms = wall_ms;
    let user = profile
        .user_permissions
        .as_mut()
        .context("missing User permissions")?;
    user.wasm.wall_ms = wall_ms;
    user.loops.max_wall_ms = wall_ms;
    let host = Arc::new(Host::default());
    *host.ui_enabled.lock().unwrap() = true;
    host.utility_ports.lock().unwrap().extend([
        (SETTINGS.into(), "list-models".into()),
        (SETTINGS.into(), "save-model".into()),
        (PROVIDER_CONFIGURATION.into(), "prepare-local".into()),
        (PROVIDER_COMMIT.into(), "execute-local".into()),
        (SAVED_MODEL.into(), "select".into()),
    ]);
    host.approve_literal_user(&profile)?;
    let data = root.join("data");
    let runtime_owner = RuntimeOwner::open(&data)?;
    let core = Core::from_runtime_owner(runtime_owner, host)?;
    let id = profile.id.clone();
    let head = core.save_profile(profile, None).await?;
    let start = core.profile_start_binding(&id, &head)?;
    let stores = TieredStore::open(&DataRoot::open(data)?, &id)?;
    // The file is the output of the original Python ConversationStore; the
    // test import keeps its bytes and does not manufacture a current model.
    stores.register(
        &owner.id,
        "saved-conversation",
        StoreTier::Private,
        SAVED_DATA,
    )?;
    let seed = canon(&json!({"document":CONVERSATION_FIXTURE}))?;
    stores.apply_seed(&owner.id, "saved-conversation", &owner.sha256, &seed)?;
    Ok((core, start))
}

async fn select_saved_model(
    session: &Session,
    conversation: &str,
    revision: u64,
) -> Result<String> {
    let selected = call(
        session,
        SAVED_MODEL,
        "select",
        json!({
            "conversation-id":conversation, "conversation-revision":revision,
        }),
    )
    .await?;
    Ok(selected
        .as_str()
        .context("conversation owner returned no model")?
        .to_owned())
}

fn admitted_ui(session: &Session) -> Result<UiAttachment> {
    session.bind_ui_generation(7)?;
    let [ui] = session
        .ui_attachments()?
        .try_into()
        .ok()
        .context("expected one admitted chat UI")?;
    Ok(ui)
}

async fn save_connection(
    session: &Session,
    profile: &ProfileId,
    name: &str,
    display_name: &str,
    origin: &str,
    revision: u64,
) -> Result<u64> {
    let request = json!({
        "connection-name":name,
        "endpoint":format!("{origin}/v1"),
        "display-name":display_name,
        "catalog-provider-id":null,
        "capabilities":null
    });
    let plan = call(
        session,
        PROVIDER_CONFIGURATION,
        "prepare-local",
        json!({"request":request}),
    )
    .await?;
    ensure!(plan["profile_id"] == profile.as_str() && plan["expected_revision"] == revision);
    let digest = tobkiri_canon::digest(&canon(&plan)?, tobkiri_canon::Variant::StrictV1)?;
    let connection = call(
        session,
        PROVIDER_COMMIT,
        "execute-local",
        json!({"request":request,"plan-digest":digest.to_string()}),
    )
    .await?;
    ensure!(
        connection
            == json!({
                "configured":true, "provider_instance_id":format!("provider.{name}")
            })
    );
    // save_model independently checks this revision against the Provider Registry.
    Ok(revision + 1)
}

async fn stream(
    session: &Session,
    ui: &UiAttachment,
    reference: &str,
    message: &str,
) -> Result<Vec<Value>> {
    let arguments = canon(&json!({"prompt":{
        "model":reference, "max-tokens":512, "messages":[{"role":"user","content":message}]
    }}))?;
    let opened = within_session(
        session,
        "open registered stream",
        session.ui_invoke_received(
            &ui.session,
            &ui.host_session,
            ui.generation.context("missing UI generation")?,
            &ui.receiver,
            LLM.parse()?,
            "chat".parse()?,
            &arguments,
        ),
    )
    .await
    .with_context(|| format!("open registered stream for {reference}"))?;
    let opened: Value = serde_json::from_slice(opened.as_bytes())?;
    ensure!(opened["kind"] == "stream");
    let handle = opened["handle"].as_u64().context("stream handle")?;
    let mut output = Vec::new();
    loop {
        let chunk = within_session(
            session,
            "pull registered stream",
            session.ui_stream_pull_buffer(&ui.session, handle, 8),
        )
        .await
        .with_context(|| format!("pull registered stream for {reference}"))?;
        let batch: Vec<Value> = serde_json::from_slice(chunk.as_bytes())?;
        if batch.is_empty() {
            break;
        }
        output.extend(batch);
    }
    Ok(output)
}

#[tokio::test]
#[ignore = "requires a source-built OpenAI component and staged Registry Pack archives"]
async fn two_saved_models_route_actual_sse_requests() -> Result<()> {
    let listeners = [
        TcpListener::bind("127.0.0.1:0")?,
        TcpListener::bind("127.0.0.1:0")?,
    ];
    let origins = listeners
        .iter()
        .map(|listener| Ok(format!("http://{}", listener.local_addr()?)))
        .collect::<Result<Vec<_>>>()?;
    let servers = listeners.map(|listener| thread::spawn(move || api(listener)));
    let root = physical_temp()?;
    let (core, start) = registered_core(root.path(), &origins, 10_000).await?;
    let profile = &start.profile_id;
    let session = core.start_bound(&start).await?;
    let checked = async {
        for (origin, (route, cap, revision)) in
            origins.iter().zip([("one", 128, 0), ("two", 256, 1)])
        {
            let provider_revision =
                save_connection(&session, profile, route, route, origin, revision).await?;
            let model = json!({
                "model_profile_id": format!("registered-{route}"),
                "model_id": format!("llama/{route}"),
                "provider_instance_id": format!("provider.{route}"),
                "display_name": route,
                "expected_revision": revision,
                "provider_registry_revision": provider_revision,
                "max_output_tokens": cap
            });
            save_model(&session, &model).await?;
        }
        let ui = admitted_ui(&session)?;
        ensure!(
            stream(&session, &ui, "unregistered", "Hello")
                .await
                .is_err(),
            "unregistered model sent a request"
        );
        let stale = select_saved_model(&session, "one", 1)
            .await
            .expect_err("stale model selection");
        rejected(&stale, "conflict")?;
        ensure!(call(&session, SAVED_MODEL, "select", json!({
            "conversation-id":"one", "conversation-revision":2, "model-reference":"registered-two",
        })).await.is_err(), "a request supplied its own model reference");
        for conversation in ["one", "two"] {
            let reference = select_saved_model(&session, conversation, 2).await?;
            let events = stream(&session, &ui, &reference, "Hello").await?;
            ensure!(
                events
                    .iter()
                    .any(|row| row["kind"] == "delta" && row["text"] == "local reply")
            );
            ensure!(events.iter().any(|row| row["kind"] == "done"));
        }
        Ok::<_, anyhow::Error>(())
    }
    .await;
    let closed = session.close().await;
    checked?;
    closed?;
    for (server, (route, cap)) in servers.into_iter().zip([("one", 128), ("two", 256)]) {
        let (path, request) = server
            .join()
            .map_err(|_| anyhow::anyhow!("loopback server failed"))??;
        ensure!(path == "/v1/chat/completions");
        ensure!(request["model"] == format!("llama/{route}"));
        ensure!(request["max_tokens"] == cap);
        ensure!(request["messages"] == json!([{"role":"user","content":"Hello"}]));
    }
    Ok(())
}

#[tokio::test]
#[ignore = "requires source-built Wasm Packs and TOBKIRI_TEST_REGISTERED_LLAMA_ORIGIN"]
async fn real_1b_saved_model_streams_through_admitted_ui() -> Result<()> {
    const MODEL: &str = "gemma3-1b-local";
    const SELECTION: &str = "saved-gemma";
    const PROVIDER_NAME: &str = "gemma-loopback";
    const PROVIDER: &str = "provider.gemma-loopback";
    // Keep this CPU-only probe within the provider fixture's 20-second budget.
    const CAP: u32 = 6;
    let origin = std::env::var("TOBKIRI_TEST_REGISTERED_LLAMA_ORIGIN")
        .context("set TOBKIRI_TEST_REGISTERED_LLAMA_ORIGIN to the keyless llama.cpp origin")?;
    let address: SocketAddrV4 = origin
        .strip_prefix("http://")
        .context("llama.cpp requires an HTTP loopback origin")?
        .parse()
        .context("llama.cpp origin must use a literal IPv4 address and port")?;
    ensure!(
        *address.ip() == Ipv4Addr::LOCALHOST
            && address.port() >= 1024
            && origin == format!("http://{address}"),
        "llama.cpp requires a canonical http://127.0.0.1 origin with an unprivileged port"
    );
    let root = physical_temp()?;
    let (core, start) = registered_core(root.path(), std::slice::from_ref(&origin), 30_000).await?;
    let expected = json!({
        "profiles": [{
            "profile_id": SELECTION,
            "display_name": "Gemma 3 1B",
            "model_id": MODEL,
            "provider_id": PROVIDER,
            "route_configured": true,
            "max_output_tokens": CAP
        }],
        "count": 1,
        "registry_revision": 1
    });
    let session = core.start_bound(&start).await?;
    let saved = async {
        let provider_revision = save_connection(
            &session,
            &start.profile_id,
            PROVIDER_NAME,
            "Local llama.cpp",
            &origin,
            0,
        )
        .await?;
        let model = json!({
            "model_profile_id": SELECTION,
            "model_id": MODEL,
            "provider_instance_id": PROVIDER,
            "display_name": "Gemma 3 1B",
            "expected_revision": 0,
            "provider_registry_revision": provider_revision,
            "max_output_tokens": CAP
        });
        ensure!(
            save_model(&session, &model).await? == expected,
            "saved model differs from the selected route"
        );
        Ok::<_, anyhow::Error>(())
    }
    .await;
    let closed = session.close().await;
    saved?;
    closed?;
    drop(session);

    let session = core.start_bound(&start).await?;
    let streamed = async {
        ensure!(
            list(&session).await? == expected,
            "saved model did not survive Session restart"
        );
        let reference = select_saved_model(&session, "gemma", 2).await?;
        ensure!(reference == SELECTION);
        let ui = admitted_ui(&session)?;
        stream(&session, &ui, &reference, "Name a color.").await
    }
    .await;
    let closed = session.close().await;
    let events = streamed?;
    closed?;

    let (done, deltas) = events
        .split_last()
        .context("registered stream returned no events")?;
    ensure!(
        done["kind"] == "done",
        "registered stream ended without completion"
    );
    let mut text = String::new();
    let mut delta_count = 0;
    for event in deltas {
        match event["kind"].as_str() {
            Some("delta") => {
                let delta = event["text"]
                    .as_str()
                    .filter(|text| !text.is_empty())
                    .context("registered stream returned an empty text delta")?;
                text.push_str(delta);
                delta_count += 1;
            }
            Some("usage") => {}
            _ => anyhow::bail!("unexpected registered stream event kind: {}", event["kind"]),
        }
    }
    ensure!(
        !text.trim().is_empty(),
        "registered model returned no visible text"
    );
    ensure!(
        done["text"] == text,
        "completion text differs from the streamed deltas"
    );
    println!(
        "registered real-1b receipt: selection={SELECTION} model={MODEL} plan_digest={} registry_revision=1 streamed_deltas={delta_count} done=true",
        session.plan_digest()
    );
    Ok(())
}
