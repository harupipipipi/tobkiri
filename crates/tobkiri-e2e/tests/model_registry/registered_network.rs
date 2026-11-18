//! Authenticated streaming uses the saved route at each invocation.

use super::*;
use std::{
    collections::BTreeMap,
    fs,
    io::{BufRead, BufReader, Read, Write},
    net::TcpListener,
    thread,
    time::{Duration, Instant},
};
use tobkiri_core::UiAttachment;
use tobkiri_manifest::{
    PackManifest, PortDeclaration, Ports, RuntimeConfig, RuntimeSpec, ShapeDefinition, ShapeKind,
    UiDefinition,
};

const LLM: &str = "local.harness-chat.llm-provider@1.0";
const MODEL_ROUTE: &str = "local.model-registry.routing@1.0";
const PROVIDER_ROUTE: &str = "local.provider-registry.routing@1.0";
const UI: &str = "local.harness-chat";

fn ports(shape: &str) -> Result<PortDeclaration> {
    let value = tobkiri_canon::ids::ShapeRef::parse(shape)?;
    Ok(PortDeclaration {
        shape: value.name,
        version: value.version,
        optional: false,
    })
}

fn manifest(id: &str, runtime: RuntimeSpec) -> Result<PackManifest> {
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
        permissions: registry::store_permissions(),
        ui: None,
        files: vec![],
    })
}

fn streaming_packs(root: &Path, origins: &[String]) -> Result<[RegistryPack; 2]> {
    let artifact = std::env::var_os("TOBKIRI_TEST_REGISTERED_OPENAI_COMPONENT")
        .context("source-built registered OpenAI component required")?;
    let repository = support::repo();
    let chat_wit = fs::read(repository.join("packs/shapes/harness-chat.wit"))?;
    let core = fs::read(repository.join("crates/tobkiri-wit/wit/core/core.wit"))?;
    let mut ui = manifest(UI, RuntimeSpec::Data)?;
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

async fn stream(session: &Session, ui: &UiAttachment, reference: &str) -> Result<()> {
    let arguments = canon(&json!({"prompt":{
        "model":reference, "max-tokens":512, "messages":[{"role":"user","content":"Hello"}]
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
    .await?;
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
        .await?;
        let batch: Vec<Value> = serde_json::from_slice(chunk.as_bytes())?;
        if batch.is_empty() {
            break;
        }
        output.extend(batch);
    }
    ensure!(
        output
            .iter()
            .any(|row| row["kind"] == "delta" && row["text"] == "local reply")
    );
    ensure!(output.iter().any(|row| row["kind"] == "done"));
    Ok(())
}

#[tokio::test]
#[ignore = "requires source-built registered OpenAI and Registry Wasm components"]
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
    let [model, provider] = packs(root.path())?;
    let [ui, openai] = streaming_packs(root.path(), &origins)?;
    let mut profile = registry::profile(
        "prof_registered_network",
        &[model, provider, ui, openai],
        &[SETTINGS, PROVIDER_MANAGE],
    )?;
    profile.startup.ui = Some(UI.parse()?);
    profile
        .ceiling
        .network
        .origins
        .extend(origins.iter().cloned());
    let host = Arc::new(Host::default());
    *host.ui_enabled.lock().unwrap() = true;
    host.utility_ports.lock().unwrap().extend([
        (SETTINGS.into(), "save-model".into()),
        (PROVIDER_MANAGE.into(), "apply".into()),
    ]);
    host.approve_literal_user(&profile)?;
    let core = Core::open(
        CoreConfig::for_test_data_root(root.path().join("data")),
        host,
    )?;
    core.save_profile(profile.clone(), None).await?;
    let session = core.start(&profile.id).await?;
    let checked = async {
        for (revision, (origin, (route, cap))) in origins.iter().zip([("one",128),("two",256)]).enumerate() {
            call(&session, PROVIDER_MANAGE, "apply", json!({"payload":{
                "operation":"save", "profile_id":profile.id.as_str(), "expected_revision":revision,
                "record":{"provider_instance_id":route,"adapter_id":"local-openai-compatible","display_name":route,
                    "endpoint":format!("{origin}/v1"),"credential_handle":null,"enabled":true}
            }})).await?;
            save_model(&session, &json!({
                "model_profile_id":format!("registered-{route}"),"model_id":format!("llama/{route}"),
                "provider_instance_id":route,"display_name":route,"expected_revision":revision,
                "provider_registry_revision":revision+1,"max_output_tokens":cap
            })).await?;
        }
        session.bind_ui_generation(7)?;
        let attachments = session.ui_attachments()?;
        let [ui] = attachments.as_slice() else { anyhow::bail!("expected one admitted chat UI") };
        ensure!(stream(&session, ui, "unregistered").await.is_err(), "unregistered model sent a request");
        stream(&session, ui, "registered-one").await?;
        stream(&session, ui, "registered-two").await?;
        Ok::<_, anyhow::Error>(())
    }.await;
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
