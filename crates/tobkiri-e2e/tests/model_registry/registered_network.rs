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

#[cfg(unix)]
struct LiveConversationFixture<'a> {
    core: &'a Arc<Core>,
    root: &'a Path,
    data: PathBuf,
    pin: tobkiri_profile::PackPin,
    profile: &'a ProfileId,
}

#[cfg(unix)]
enum OriginalConversationLookup {
    Conversation,
    LinkedContext,
    LinkedParentRead,
    SavedReceipt,
}

#[cfg(unix)]
impl<'a> LiveConversationFixture<'a> {
    async fn import(core: &'a Arc<Core>, root: &'a Path, profile: &'a ProfileId) -> Result<Self> {
        let data = root.join("original-conversation-data");
        let store = DataRoot::open(&data)?;
        let fixture = Self {
            core,
            root,
            data,
            pin: registry::pack(SAVED_PACK)?.pin,
            profile,
        };
        let missing = fixture
            .get("one")
            .await
            .expect_err("physically absent conversation");
        ensure!(matches!(missing.downcast_ref::<NativeArtifactError>(),
                Some(NativeArtifactError::ConversationRegistryRejected(error))
                    if error.code() == "not-found"));
        ensure!(!fixture.data.join("packs").exists());
        store.write_atomic(fixture.relative_path(), CONVERSATION_FIXTURE.as_bytes())?;
        Ok(fixture)
    }

    fn relative_path(&self) -> String {
        format!(
            "packs/rumi_conversation_store_pack/profiles/{}/conversations.json",
            self.profile,
        )
    }

    async fn get(&self, id: &str) -> Result<Value> {
        let wire = self
            .lookup(id, OriginalConversationLookup::Conversation)
            .await?;
        Ok(serde_json::from_str(&wire)?)
    }

    fn archive(&self) -> Result<(fs::File, u64)> {
        let path = self
            .pin
            .source
            .strip_prefix("file:")
            .context("staged Conversation archive")?;
        let file = fs::File::open(path)?;
        let length = file.metadata()?.len();
        Ok((file, length))
    }

    async fn lookup(&self, id: &str, lookup: OriginalConversationLookup) -> Result<String> {
        let mut inspection = self
            .core
            .inspect_declared_artifacts(Source::new(self.root))?;
        let (mut archive, length) = self.archive()?;
        let pack = inspection.verify_pack_archive(&mut archive, length)?;
        ensure!(pack.hash == self.pin.sha256);
        let outcome: Result<String> = async {
            let profile_id = self.profile.as_str();
            let result_key = match lookup {
                OriginalConversationLookup::LinkedParentRead => "parent_read",
                _ => "owner_result",
            };
            let result = match lookup {
                OriginalConversationLookup::Conversation => {
                    tobkiri_wasm::verify_legacy_conversation_read_declaration(&pack)?;
                    inspection
                        .get_legacy_conversation(&pack, &self.data, profile_id, id)
                        .await?
                }
                OriginalConversationLookup::LinkedContext
                | OriginalConversationLookup::LinkedParentRead => {
                    tobkiri_wasm::verify_legacy_conversation_linked_context_declaration(&pack)?;
                    inspection
                        .get_legacy_linked_conversation_context(&pack, &self.data, profile_id, id)
                        .await?
                }
                OriginalConversationLookup::SavedReceipt => {
                    tobkiri_wasm::verify_legacy_conversation_receipt_declaration(&pack)?;
                    inspection
                        .get_legacy_saved_receipt(&pack, &self.data, profile_id, id)
                        .await?
                }
            };
            let wire = result
                .get_key(result_key)
                .context("missing Conversation owner result")?;
            let wire = wire
                .as_str()
                .context("Conversation owner result was not text")?;
            Ok(wire.to_owned())
        }
        .await;
        inspection.finish().await?;
        outcome
    }
}

#[cfg(unix)]
#[tokio::test]
#[ignore = "requires the staged Conversation Registry Pack archive"]
async fn original_saved_linked_context_reads_the_physical_owner() -> Result<()> {
    let root = physical_temp()?;
    let profile = ProfileId::parse("defaults")?;
    let core = Arc::new(Core::open(
        CoreConfig::for_test_data_root(root.path().join("core")),
        Arc::new(Host::default()),
    )?);
    let live = LiveConversationFixture::import(&core, root.path(), &profile).await?;
    let owner = DataRoot::open(&live.data)?;
    let relative = live.relative_path();
    let mut document = json!({
        "version": "rumi.conversation-store.v1", "profile_id": "defaults", "revision": 7,
        "conversations": {
            "child": {
                "id": "child", "conversation_revision": 1, "parent_conversation_id": "parent",
                "context_link": {
                    "version": "tobkiri.conversation-context-link.v1",
                    "parent_conversation_id": "parent", "slot": "side-chat",
                    "created_from_parent_revision": 1,
                },
                "messages": [{"id": "child-message"}],
            },
            "parent": {
                "id": "parent", "conversation_revision": 7, "model_reference": "m-local",
                "metadata": {"thinking_level": "low", "unrelated": "private"},
                "messages": [{"id": "parent-message"}],
            },
        },
    });
    for (model, revision) in [("m-local", 7), ("m-updated", 8)] {
        let parent = &mut document["conversations"]["parent"];
        parent["model_reference"] = json!(model);
        parent["conversation_revision"] = json!(revision);
        let bytes = serde_json::to_vec(&document)?;
        owner.write_atomic(&relative, &bytes)?;
        let result: Value = serde_json::from_str(
            &live
                .lookup("child", OriginalConversationLookup::LinkedContext)
                .await?,
        )?;
        ensure!(result["conversation"] == live.get("child").await?["conversation"]);
        ensure!(
            live.lookup("child", OriginalConversationLookup::LinkedParentRead)
                .await?
                == live
                    .lookup("parent", OriginalConversationLookup::Conversation)
                    .await?
        );
        ensure!(
            result["parent_context"]
                == json!({
                    "id": "parent", "conversation_revision": revision, "model_reference": model,
                    "system_prompt_id": null, "agent_id": null, "conversation_kind": null,
                    "group_id": null, "tags": null, "metadata": {"thinking_level": "low"},
                })
        );
        ensure!(fs::read(live.data.join(&relative))? == bytes);
    }
    Ok(())
}

#[cfg(unix)]
#[tokio::test]
#[ignore = "requires the staged Conversation Registry Pack archive"]
async fn original_saved_receipt_reads_physical_store_without_writes() -> Result<()> {
    const DOCUMENT: &str = include_str!(
        "../../../../packs/local.saved-conversation-rust/fixtures/legacy-receipt-document.json"
    );
    const PYTHON_REPLY: &str = include_str!(
        "../../../../packs/local.saved-conversation-rust/fixtures/legacy-receipt-owner-result.json"
    );
    let root = physical_temp()?;
    let data = root.path().join("owner");
    let store = DataRoot::open(&data)?;
    let mut original: Value = serde_json::from_str(DOCUMENT)?;
    let profile = original["profile_id"]
        .as_str()
        .context("original Profile")?;
    let profile = ProfileId::parse(profile)?;
    let core = Arc::new(Core::open(
        CoreConfig::for_test_data_root(root.path().join("core")),
        Arc::new(Host::default()),
    )?);
    let fixture = LiveConversationFixture {
        core: &core,
        root: root.path(),
        data,
        pin: registry::pack(SAVED_PACK)?.pin,
        profile: &profile,
    };
    let receipt = |id| fixture.lookup(id, OriginalConversationLookup::SavedReceipt);
    ensure!(receipt("Turn.1_x-y:z").await? == r#"{"receipt":null}"#);
    ensure!(!fixture.data.join("packs").exists());
    let relative = fixture.relative_path();
    store.write_atomic(&relative, DOCUMENT.as_bytes())?;
    ensure!(receipt(" Turn.1_x-y:z ").await? == PYTHON_REPLY.trim_end());
    ensure!(receipt("empty").await? == r#"{"receipt":{}}"#);
    ensure!(receipt("array").await? == r#"{"receipt":null}"#);
    let file = fixture.data.join(&relative);
    ensure!(fs::read(&file)? == DOCUMENT.as_bytes());
    ensure!(!file.with_file_name("conversations.lock").exists());

    original["saved_receipts"] = Value::Null;
    store.write_atomic(&relative, &serde_json::to_vec(&original)?)?;
    let error = receipt("!")
        .await
        .expect_err("malformed inventory needs legacy handling");
    ensure!(matches!(error.downcast_ref::<NativeArtifactError>(),
            Some(NativeArtifactError::ConversationRegistryRejected(error))
                if error.code() == "unavailable"));
    Ok(())
}

#[cfg(unix)]
#[tokio::test]
#[ignore = "requires the staged Conversation Registry Pack archive"]
async fn original_saved_confirmation_publishes_physical_store() -> Result<()> {
    use std::{cell::Cell, os::unix::fs::MetadataExt};

    const ORACLE: &str = include_str!(
        "../../../../packs/local.saved-conversation-rust/fixtures/legacy-confirmation-oracle.json"
    );
    let sample: Value = serde_json::from_str(ORACLE)?;
    let root = physical_temp()?;
    let data = root.path().join("owner");
    let disk = DataRoot::open(&data)?;
    let profile = ProfileId::parse("defaults")?;
    let core = Arc::new(Core::open(
        CoreConfig::for_test_data_root(root.path().join("core")),
        Arc::new(Host::default()),
    )?);
    let fixture = LiveConversationFixture {
        core: &core,
        root: root.path(),
        data,
        pin: registry::pack(SAVED_PACK)?.pin,
        profile: &profile,
    };
    let document = sample["document"].as_str().context("old owner document")?;
    let expected = sample["promotion"]
        .as_str()
        .context("published owner document")?;
    let terminal = sample["turn"].to_string();
    let path = fixture.data.join(fixture.relative_path());
    let lock = path.parent().unwrap().join("locks/conversations.lock");
    disk.write_atomic(fixture.relative_path(), document.as_bytes())?;
    let original_inode = fs::metadata(&path)?.ino();
    for (deny, wrote) in [(true, true), (false, true), (false, false)] {
        let mut inspection = core.inspect_declared_artifacts(Source::new(root.path()))?;
        let (mut archive, length) = fixture.archive()?;
        let pack = inspection.verify_pack_archive(&mut archive, length)?;
        ensure!(pack.hash == fixture.pin.sha256);
        tobkiri_wasm::verify_legacy_conversation_confirmation_declaration(&pack)?;
        let mut owner = inspection.legacy_conversation_store(&fixture.data, profile.as_str())?;
        let prior = owner
            .observe_locked(inspection.allocation_budget()?, || Ok(()))?
            .context("locked Conversation document")?;
        ensure!(lock.is_file());
        if deny {
            let mut other = core.inspect_declared_artifacts(Source::new(root.path()))?;
            ensure!(matches!(
                other
                    .stage_legacy_conversation_confirmation(&pack, &owner, &terminal)
                    .await,
                Err(NativeArtifactError::Denied)
            ));
            other.finish().await?;
        }
        let staged = inspection
            .stage_legacy_conversation_confirmation(&pack, &owner, &terminal)
            .await?;
        let wire = staged
            .get_key("owner_result")
            .context("Conversation confirmation reply")?;
        let wire = wire
            .as_str()
            .context("Conversation confirmation reply is not text")?;
        ensure!(serde_json::from_str::<Value>(wire)? == sample["reply"]);
        let next = staged
            .get_key("next_document")
            .context("replacement decision")?;
        let next = next.as_str();
        ensure!(next == wrote.then_some(expected));
        ensure!(fs::read(&path)? == prior.as_bytes());
        inspection.finish().await?;
        if let Some(next) = next {
            let checks = Cell::new(0);
            let publication = owner.publish(next.as_bytes(), || {
                checks.set(checks.get() + 1);
                if deny && checks.get() == 2 {
                    return Err(NativeArtifactError::Denied);
                }
                Ok(())
            });
            ensure!(checks.get() == 2);
            if deny {
                ensure!(matches!(publication, Err(NativeArtifactError::Denied)));
                ensure!(fs::read(&path)? == document.as_bytes());
                ensure!(fs::metadata(&path)?.ino() == original_inode);
            } else {
                publication?;
                ensure!(fs::read(&path)? == expected.as_bytes());
                ensure!(fs::metadata(&path)?.ino() != original_inode);
            }
        } else {
            let committed_inode = fs::metadata(&path)?.ino();
            drop(owner);
            ensure!(fs::read(&path)? == expected.as_bytes());
            ensure!(fs::metadata(&path)?.ino() == committed_inode);
        }
        ensure!(!lock.exists());
    }
    Ok(())
}

#[cfg(unix)]
#[tokio::test]
#[ignore = "requires the staged Conversation Registry Pack archive"]
async fn original_saved_append_updates_the_physical_store_and_receipt() -> Result<()> {
    use std::cell::Cell;
    use tobkiri_canon::{Variant, digest, parse};

    const ORACLE: &str = include_str!(
        "../../../../packs/local.saved-conversation-rust/fixtures/legacy-append-oracle.json"
    );
    let oracle: Value = serde_json::from_str(ORACLE)?;
    let root = physical_temp()?;
    let data = root.path().join("owner");
    let disk = DataRoot::open(&data)?;
    let profile = ProfileId::parse("defaults")?;
    let core = Arc::new(Core::open(
        CoreConfig::for_test_data_root(root.path().join("core")),
        Arc::new(Host::default()),
    )?);
    let fixture = LiveConversationFixture {
        core: &core,
        root: root.path(),
        data,
        pin: registry::pack(SAVED_PACK)?.pin,
        profile: &profile,
    };
    let path = fixture.data.join(fixture.relative_path());
    let lock = path
        .parent()
        .context("physical owner parent")?
        .join("locks/conversations.lock");
    let original = oracle["document"].as_str().context("old owner document")?;
    disk.write_atomic(fixture.relative_path(), original.as_bytes())?;

    for (role, deny, replay) in [
        ("user", true, false),
        ("user", false, false),
        ("assistant", false, false),
        ("assistant", false, true),
    ] {
        let case = &oracle[role];
        let request = &case["request"];
        let conversation = request["conversation_id"]
            .as_str()
            .context("oracle conversation")?;
        let turn_id = request["saved_input"]["request"]["turn_id"]
            .as_str()
            .context("oracle turn")?;
        let mut inspection = core.inspect_declared_artifacts(Source::new(root.path()))?;
        let (mut archive, size) = fixture.archive()?;
        let pack = inspection.verify_pack_archive(&mut archive, size)?;
        ensure!(pack.hash == fixture.pin.sha256);
        tobkiri_wasm::verify_legacy_conversation_append_declaration(&pack)?;
        let mut owner = inspection.legacy_conversation_store(&fixture.data, profile.as_str())?;
        let prior = owner
            .observe_locked(inspection.allocation_budget()?, || Ok(()))?
            .context("locked Conversation file")?;
        ensure!(lock.is_file());
        let staged = inspection
            .stage_legacy_conversation_append(&pack, &owner, &request.to_string())
            .await;
        inspection.finish().await?;
        if replay {
            let error = staged.expect_err("append replay must conflict");
            ensure!(
                matches!(error, NativeArtifactError::ConversationRegistryRejected(ref error)
                if error.code() == "conflict")
            );
            let on_disk: Value = serde_json::from_slice(&fs::read(&path)?)?;
            ensure!(on_disk["saved_receipts"][turn_id]["result_reference"].is_object());
            ensure!(fs::read(&path)? == prior.as_bytes());
            drop(owner);
            ensure!(!lock.exists());
            continue;
        }
        let staged = staged?;
        let wire = staged.get_key("owner_result").context("owner result")?;
        let wire: Value = serde_json::from_str(wire.as_str().context("owner result text")?)?;
        let next = staged
            .get_key("next_document")
            .context("replacement decision")?;
        let next = next.as_str().context("replacement text")?;
        let replacement: Value = serde_json::from_str(next)?;
        let mut message = wire["message"].clone();
        ensure!(message["created_at"].as_i64().context("created clock")? > 0);
        ensure!(message["created_at"] == message["updated_at"]);
        message["created_at"] = case["owner_result"]["message"]["created_at"].clone();
        message["updated_at"] = case["owner_result"]["message"]["updated_at"].clone();
        ensure!(message == case["owner_result"]["message"]);
        ensure!(wire["action"] == "message_appended");
        ensure!(wire["conversation_revision"] == case["owner_result"]["conversation_revision"]);
        let record = &replacement["conversations"][conversation];
        ensure!(
            record["messages"].as_array().and_then(|items| items.last()) == Some(&wire["message"])
        );
        let receipt = &replacement["saved_receipts"][turn_id];
        if role == "user" {
            ensure!(*receipt == case["receipt"]);
            ensure!(
                record["lifecycle"]["active_user_received_at_ms"]
                    .as_i64()
                    .is_some()
            );
        } else {
            let reference = &receipt["result_reference"];
            let canonical_outcome = json!({
                "status":"ok", "turn_id":turn_id, "conversation_id":conversation,
                "conversation_revision":wire["conversation_revision"],
                "user_message_id":receipt["user_message_id"], "message":wire["message"],
            });
            let value = parse(&serde_json::to_vec(&canonical_outcome)?, Variant::StrictV1)?;
            ensure!(reference["outcome_digest"] == digest(&value, Variant::StrictV1)?.as_str());
            ensure!(record["lifecycle"]["completion_candidate"]["receipt"] == *receipt);
            ensure!(
                record["lifecycle"]["completion_candidate"]["completed_at_ms"]
                    .as_i64()
                    .is_some()
            );
        }
        let checkpoints = Cell::new(0);
        let written = owner.publish(next.as_bytes(), || {
            checkpoints.set(checkpoints.get() + 1);
            if deny && checkpoints.get() == 2 {
                return Err(NativeArtifactError::Denied);
            }
            Ok(())
        });
        ensure!(checkpoints.get() == 2 && !lock.exists());
        if deny {
            ensure!(matches!(written, Err(NativeArtifactError::Denied)));
            ensure!(fs::read(&path)? == prior.as_bytes());
        } else {
            written?;
            ensure!(fs::read(&path)? == next.as_bytes());
        }
    }
    let data: Value = serde_json::from_slice(&fs::read(&path)?)?;
    let turn = oracle["user"]["request"]["saved_input"]["request"]["turn_id"]
        .as_str()
        .context("oracle turn")?;
    ensure!(data["saved_receipts"][turn]["result_reference"].is_object());

    let stale = &oracle["linked"];
    let prior = stale["stale_parent_document"]
        .as_str()
        .context("changed parent document")?;
    let request = stale["stale_parent"]["request"].to_string();
    disk.write_atomic(fixture.relative_path(), prior.as_bytes())?;
    let mut inspection = core.inspect_declared_artifacts(Source::new(root.path()))?;
    let (mut archive, size) = fixture.archive()?;
    let pack = inspection.verify_pack_archive(&mut archive, size)?;
    ensure!(pack.hash == fixture.pin.sha256);
    let mut owner = inspection.legacy_conversation_store(&fixture.data, profile.as_str())?;
    owner
        .observe_locked(inspection.allocation_budget()?, || Ok(()))?
        .context("locked changed parent document")?;
    let rejected = inspection
        .stage_legacy_conversation_append(&pack, &owner, &request)
        .await;
    ensure!(
        matches!(rejected, Err(NativeArtifactError::ConversationRegistryRejected(ref error))
        if error.code() == "invalid")
    );
    inspection.finish().await?;
    drop(owner);
    ensure!(fs::read(path)? == prior.as_bytes() && !lock.exists());
    Ok(())
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
    let core = Arc::new(core);
    #[cfg(unix)]
    let live = LiveConversationFixture::import(&core, root.path(), profile).await?;
    #[cfg(unix)]
    let original: Value = serde_json::from_str(CONVERSATION_FIXTURE)?;
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
            #[cfg(unix)]
            let reference = {
                let actual = live.get(conversation).await?;
                let owner = &actual["conversation"];
                ensure!(*owner == original["conversations"][conversation]);
                let stored = owner["model_reference"]
                    .as_str()
                    .context("live selected model")?;
                ensure!(stored == reference);
                stored.to_owned()
            };
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
    #[cfg(unix)]
    ensure!(fs::read(live.data.join(live.relative_path()))? == CONVERSATION_FIXTURE.as_bytes());
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
    let core = Arc::new(core);
    #[cfg(unix)]
    let live = LiveConversationFixture::import(&core, root.path(), &start.profile_id).await?;
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
        #[cfg(unix)]
        let reference = {
            let result = live.get("gemma").await?;
            let record = &result["conversation"];
            ensure!(record["conversation_revision"] == 2);
            let original = record["model_reference"]
                .as_str()
                .context("live Gemma model")?;
            ensure!(original == reference);
            original.to_owned()
        };
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
