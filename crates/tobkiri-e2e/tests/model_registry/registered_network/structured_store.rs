//! The legacy and structured Wasm components share one history across Profile revisions.

use super::*;
use tobkiri_manifest::{StoreDefinition, StoreTier as ManifestStoreTier};
use tobkiri_profile::Profile;

const STORE_PACK: &str = "local.harness-conversation-store";
const STORE_NAME: &str = "conversations";
const WALL_MS: u64 = 10_000;
const FIRST: &str = "first";
const SECOND: &str = "second";
const LEGACY_INPUT: &str = "以前の質問 🗻";
const LEGACY_REPLY: &str = "以前の回答\n二行目";
const STRUCTURED_INPUT: &str = "文字を変換して";
const STRUCTURED_REPLY: &str = "変換できました 🍒\n完了";
const DOWNGRADE_INPUT: &str = "旧版で追記";
const DOWNGRADE_REPLY: &str = "同じ履歴を継続";
const OTHER_INPUT: &str = "別の会話";
const OTHER_REPLY: &str = "別の回答";

pub(super) fn stage_conversation_store(
    root: &Path,
    wall_ms: u64,
    structured: bool,
) -> Result<RegistryPack> {
    let repository = support::repo();
    let source = repository.join("packs/local.harness-conversation-store");
    let (variable, world) = if structured {
        (
            "TOBKIRI_TEST_HARNESS_STRUCTURED_CONVERSATION_STORE_COMPONENT",
            "structured-wit/pack.wit",
        )
    } else {
        (
            "TOBKIRI_TEST_HARNESS_LEGACY_CONVERSATION_STORE_COMPONENT",
            "wit/pack.wit",
        )
    };
    let component = std::env::var_os(variable)
        .with_context(|| format!("set {variable} to the source-built component"))?;
    let mut pack = manifest(STORE_PACK, wasm(Some("config.json")), wall_ms)?;
    pack.ports.provides.push(ports(CHAT_STORE)?);
    pack.stores.push(StoreDefinition {
        name: STORE_NAME.into(),
        tier: ManifestStoreTier::Private,
        data_shape: CHAT_MESSAGE_DATA.parse()?,
        seed: None,
    });
    let mut files = BTreeMap::from([
        ("component.wasm".into(), fs::read(component)?),
        ("config.json".into(), fs::read(source.join("config.json"))?),
        ("wit/pack.wit".into(), fs::read(source.join(world))?),
        (
            "wit/deps/core.wit".into(),
            fs::read(repository.join("crates/tobkiri-wit/wit/core/core.wit"))?,
        ),
        (
            "wit/deps/harness-chat.wit".into(),
            fs::read(repository.join("packs/local.harness-chat/wit/harness-chat.wit"))?,
        ),
    ]);
    if structured {
        pack.ports.provides.push(ports(STRUCTURED_CHAT_STORE)?);
        files.extend([
            (
                "wit/deps/core-v1-1.wit".into(),
                fs::read(repository.join("crates/tobkiri-wit/wit/core-v1-1/core.wit"))?,
            ),
            (
                "wit/deps/harness-chat-v2.wit".into(),
                fs::read(repository.join("packs/shapes/harness-chat-v2/harness-chat.wit"))?,
            ),
        ]);
    }
    registry::materialize(root, pack, files)
}

fn store_profile(root: &Path, structured: bool) -> Result<Profile> {
    let repository = support::repo();
    let legacy_wit = repository.join("packs/local.harness-chat/wit");
    let mut shapes = manifest(UI, RuntimeSpec::Data, WALL_MS)?;
    shapes.permissions.storage.private = false;
    define_shape(&mut shapes, CHAT_STORE, ShapeKind::Port, CHAT_WIT)?;
    define_shape(&mut shapes, CHAT_MESSAGE_DATA, ShapeKind::Data, CHAT_WIT)?;
    let sidecar = fs::read(legacy_wit.join("conversation-store.shape.json"))?;
    let mut files = BTreeMap::from([
        (
            CHAT_WIT.into(),
            fs::read(legacy_wit.join("harness-chat.wit"))?,
        ),
        ("wit/conversation-store.shape.json".into(), sidecar.clone()),
        (
            "wit/messages.shape.json".into(),
            fs::read(legacy_wit.join("messages.shape.json"))?,
        ),
        (
            "wit/deps/core.wit".into(),
            fs::read(repository.join("crates/tobkiri-wit/wit/core/core.wit"))?,
        ),
    ]);
    let mut utility = vec![CHAT_STORE];
    if structured {
        let v2_wit = repository.join("packs/shapes/harness-chat-v2");
        // Both exact interfaces share the same frozen sidecar path in the shape Pack.
        assert_eq!(
            fs::read(v2_wit.join("conversation-store.shape.json"))?,
            sidecar
        );
        define_shape(
            &mut shapes,
            STRUCTURED_CHAT_STORE,
            ShapeKind::Port,
            STRUCTURED_CHAT_WIT,
        )?;
        files.insert(
            STRUCTURED_CHAT_WIT.into(),
            fs::read(v2_wit.join("harness-chat.wit"))?,
        );
        utility.push(STRUCTURED_CHAT_STORE);
    }
    let shapes = registry::materialize(root, shapes, files)?;
    let store = stage_conversation_store(root, WALL_MS, structured)?;
    registry::profile("prof_store_compatibility", &[shapes, store], &utility)
}

async fn save_and_start(core: &Core, host: &Host, profile: &Profile) -> Result<Session> {
    let allowed = &profile
        .user_permissions
        .as_ref()
        .context("missing User permissions")?
        .ports;
    *host.utility_ports.lock().unwrap() = allowed
        .iter()
        .flat_map(|shape| {
            [
                (shape.clone(), "load".into()),
                (shape.clone(), "append".into()),
            ]
        })
        .collect();
    let precondition = core.profile_save_precondition(&profile.id)?;
    let reviewed = host.review_pack_permissions(profile)?;
    let compiled = tobkiri_profile::compile(profile, &reviewed)
        .map_err(|conflicts| anyhow::anyhow!("store Profile conflicts: {conflicts:?}"))?;
    let profile_hash = tobkiri_canon::digest(
        &canon(&serde_json::to_value(profile)?)?,
        tobkiri_canon::Variant::StrictV1,
    )?;
    host.user_approvals.lock().unwrap().insert((
        profile_hash.to_string(),
        compiled.plan_digest.to_string(),
        precondition.security_epoch + 1,
    ));
    let head = core
        .save_profile_at_epoch(
            profile.clone(),
            precondition.head,
            precondition.security_epoch,
        )
        .await?;
    let binding = core.profile_start_binding(&profile.id, &head)?;
    Ok(core.start_bound(&binding).await?)
}

fn legacy_message(role: &str, content: &str) -> Value {
    json!({"role":role,"content":content})
}

fn text_message(role: &str, text: &str) -> Value {
    json!({"role":role,"parts":[{"tag":"text","val":text}],"response":null})
}

async fn append_legacy(
    session: &Session,
    conversation: &str,
    input: &str,
    reply: &str,
) -> Result<()> {
    let appended = call(
        session,
        CHAT_STORE,
        "append",
        json!({
            "conversation":conversation,
            "input":{"conversation":conversation,"text":input},
            "events":[{"kind":"done","text":reply,"prompt-tokens":null,"completion-tokens":null}]
        }),
    )
    .await?;
    assert_eq!(appended, json!({"tag":"ok"}));
    Ok(())
}

fn structured_replies() -> [Value; 3] {
    [
        json!({
            "role":"assistant",
            "parts":[
                {"tag":"reasoning","val":"記号も保持する"},
                {"tag":"tool-call","val":{"id":"call-rewrite-1","name":"rewrite","arguments":{"text":"さくらんぼ"}}}
            ],
            "response":{"finish":{"tag":"tool-calls"},"usage":null}
        }),
        json!({
            "role":"tool",
            "parts":[{"tag":"tool-result","val":{"call-id":"call-rewrite-1","content":"変換結果","is-error":false}}],
            "response":null
        }),
        json!({
            "role":"assistant",
            "parts":[{"tag":"text","val":STRUCTURED_REPLY}],
            "response":{"finish":{"tag":"stop"},"usage":{"prompt-tokens":0,"completion-tokens":7,"cached-tokens":null}}
        }),
    ]
}

fn append_arguments(input: &str, replies: &[Value]) -> Value {
    json!({
        "conversation":FIRST,
        "input":{"conversation":FIRST,"parts":[{"tag":"text","val":input}],"tools":[]},
        "messages":replies
    })
}

async fn append_structured(session: &Session, input: &str, replies: &[Value]) -> Result<()> {
    let appended = call(
        session,
        STRUCTURED_CHAT_STORE,
        "append",
        append_arguments(input, replies),
    )
    .await?;
    assert_eq!(appended, json!({"tag":"ok"}));
    Ok(())
}

async fn assert_history(
    session: &Session,
    shape: &str,
    conversation: &str,
    messages: &[Value],
) -> Result<()> {
    let history = call(session, shape, "load", json!({"conversation":conversation})).await?;
    assert_eq!(
        history,
        json!({"conversation":conversation,"messages":messages})
    );
    Ok(())
}

#[tokio::test]
#[ignore = "requires the source-built legacy and structured Harness conversation-store components"]
async fn structured_store_preserves_history_through_upgrade_and_downgrade() -> Result<()> {
    let root = physical_temp()?;
    let legacy = store_profile(&root.path().join("legacy"), false)?;
    let dual = store_profile(&root.path().join("dual"), true)?;
    let host = Arc::new(Host::default());
    let data = root.path().join("data");
    let core = Core::from_runtime_owner(RuntimeOwner::open(&data)?, host.clone())?;

    let session = save_and_start(&core, &host, &legacy).await?;
    append_legacy(&session, FIRST, LEGACY_INPUT, LEGACY_REPLY).await?;
    append_legacy(&session, SECOND, OTHER_INPUT, OTHER_REPLY).await?;
    session.close().await?;
    drop(session);

    let mut legacy_history = vec![
        legacy_message("user", LEGACY_INPUT),
        legacy_message("assistant", LEGACY_REPLY),
    ];
    let mut structured_history = vec![
        text_message("user", LEGACY_INPUT),
        text_message("assistant", LEGACY_REPLY),
    ];
    let other_legacy = [
        legacy_message("user", OTHER_INPUT),
        legacy_message("assistant", OTHER_REPLY),
    ];
    let other_structured = [
        text_message("user", OTHER_INPUT),
        text_message("assistant", OTHER_REPLY),
    ];
    let session = save_and_start(&core, &host, &dual).await?;
    assert_history(&session, CHAT_STORE, FIRST, &legacy_history).await?;
    assert_history(&session, STRUCTURED_CHAT_STORE, FIRST, &structured_history).await?;
    let replies = structured_replies();
    append_structured(&session, STRUCTURED_INPUT, &replies).await?;
    legacy_history.extend([
        legacy_message("user", STRUCTURED_INPUT),
        legacy_message("assistant", STRUCTURED_REPLY),
    ]);
    structured_history.push(text_message("user", STRUCTURED_INPUT));
    structured_history.extend(replies);
    assert_history(&session, CHAT_STORE, FIRST, &legacy_history).await?;
    assert_history(&session, STRUCTURED_CHAT_STORE, FIRST, &structured_history).await?;
    assert_history(&session, STRUCTURED_CHAT_STORE, SECOND, &other_structured).await?;
    session.close().await?;
    drop(session);

    let session = save_and_start(&core, &host, &legacy).await?;
    assert_history(&session, CHAT_STORE, FIRST, &legacy_history).await?;
    assert_history(&session, CHAT_STORE, SECOND, &other_legacy).await?;
    append_legacy(&session, FIRST, DOWNGRADE_INPUT, DOWNGRADE_REPLY).await?;
    session.close().await?;
    drop(session);

    let session = save_and_start(&core, &host, &dual).await?;
    legacy_history.extend([
        legacy_message("user", DOWNGRADE_INPUT),
        legacy_message("assistant", DOWNGRADE_REPLY),
    ]);
    structured_history.extend([
        text_message("user", DOWNGRADE_INPUT),
        text_message("assistant", DOWNGRADE_REPLY),
    ]);
    assert_history(&session, CHAT_STORE, FIRST, &legacy_history).await?;
    assert_history(&session, STRUCTURED_CHAT_STORE, FIRST, &structured_history).await?;
    assert_history(&session, CHAT_STORE, SECOND, &other_legacy).await?;
    assert_history(&session, STRUCTURED_CHAT_STORE, SECOND, &other_structured).await?;
    session.close().await?;
    drop(session);

    let mut isolated = dual;
    isolated.id = "prof_store_isolated".parse()?;
    let session = save_and_start(&core, &host, &isolated).await?;
    assert_history(&session, CHAT_STORE, FIRST, &[]).await?;
    assert_history(&session, STRUCTURED_CHAT_STORE, FIRST, &[]).await?;
    session.close().await?;
    Ok(())
}

#[tokio::test]
#[ignore = "requires the source-built structured Harness conversation-store component"]
async fn structured_store_rolls_back_on_a_late_sql_constraint() -> Result<()> {
    let root = physical_temp()?;
    let profile = store_profile(&root.path().join("dual"), true)?;
    let host = Arc::new(Host::default());
    let data = root.path().join("data");
    let core = Core::from_runtime_owner(RuntimeOwner::open(&data)?, host.clone())?;
    let private_store = data.join(DataRoot::profile_relative(
        &profile.id,
        &format!("data/{STORE_PACK}/private.sqlite"),
    )?);

    let replies = structured_replies();
    let session = save_and_start(&core, &host, &profile).await?;
    append_structured(&session, STRUCTURED_INPUT, &replies).await?;
    session.close().await?;
    drop(session);

    // The fixture already has a final assistant row at ordinal 2. Only a
    // second final assistant row violates the index, after both legacy rows
    // and the earlier structured messages have been staged.
    let connection = rusqlite::Connection::open_with_flags(
        &private_store,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE,
    )?;
    connection.execute_batch(
        "CREATE UNIQUE INDEX test_reject_final_message ON structured_messages (ordinal) WHERE ordinal = 2",
    )?;
    drop(connection);

    let session = core.start(&profile.id).await?;
    let rejected = append_structured(&session, DOWNGRADE_INPUT, &replies).await;
    ensure!(
        rejected.is_err(),
        "late SQLite constraint accepted the turn"
    );
    let mut legacy_history = vec![
        legacy_message("user", STRUCTURED_INPUT),
        legacy_message("assistant", STRUCTURED_REPLY),
    ];
    let mut structured_history = vec![text_message("user", STRUCTURED_INPUT)];
    structured_history.extend(replies.iter().cloned());
    assert_history(&session, CHAT_STORE, FIRST, &legacy_history).await?;
    assert_history(&session, STRUCTURED_CHAT_STORE, FIRST, &structured_history).await?;
    session.close().await?;
    drop(session);

    let connection = rusqlite::Connection::open_with_flags(
        &private_store,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE,
    )?;
    let stored_rows: i64 =
        connection.query_row("SELECT count(*) FROM structured_messages", [], |row| {
            row.get(0)
        })?;
    assert_eq!(stored_rows, 4);
    connection.execute_batch("DROP INDEX test_reject_final_message")?;
    drop(connection);

    let session = core.start(&profile.id).await?;
    append_structured(&session, DOWNGRADE_INPUT, &replies).await?;
    legacy_history.extend([
        legacy_message("user", DOWNGRADE_INPUT),
        legacy_message("assistant", STRUCTURED_REPLY),
    ]);
    structured_history.push(text_message("user", DOWNGRADE_INPUT));
    structured_history.extend(replies);
    assert_history(&session, CHAT_STORE, FIRST, &legacy_history).await?;
    assert_history(&session, STRUCTURED_CHAT_STORE, FIRST, &structured_history).await?;
    session.close().await?;
    Ok(())
}
