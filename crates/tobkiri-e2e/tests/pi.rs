//! Optional runtime proof for the two pinned, unchanged upstream Pi examples.
//! These tests do not claim the complete Harness tool-loop/FS acceptance.
mod support;
use anyhow::{Context, Result};
use support::*;
use tobkiri_canon::ids::{OpName, ShapeRef};
use tobkiri_core::{Core, Session};
use tobkiri_gate::PermissionSet;

async fn start_example(
    example: &str,
    operations: &[(&str, &str)],
    discovery_wrapper: Option<std::path::PathBuf>,
) -> Result<(Fixture, Core, Session)> {
    let mut fixture = Fixture::new("harness-mock", None)?;
    let discovery = discovery_wrapper.is_some();
    let wrapper = discovery_wrapper.unwrap_or_else(|| repo().join("packs").join(example));
    let provenance: serde_json::Value =
        serde_json::from_slice(&std::fs::read(wrapper.join("upstream.json"))?)?;
    assert_eq!(
        provenance["commit"],
        "b06c1f2cf1dea6af8748ddeee18c23587d2cb7c6"
    );
    let expected = match example {
        "local.pi-example-hello" => {
            "0aa4e9800c2526914d4c1edb00b2cfa9bd9dd6da5218289994cccd5f5bfa4934"
        }
        "local.pi-example-protected-paths" => {
            "9ca66b1f3b1b9a61cc5ddc660f92cca2ea66c4264a1bdb6b00e0da5844399fe2"
        }
        _ => anyhow::bail!("unreviewed Pi example"),
    };
    let original = std::fs::read(wrapper.join(provenance["stored_path"].as_str().unwrap()))?;
    assert_eq!(
        tobkiri_canon::Digest::hash(&original).to_string(),
        format!("sha256:{expected}")
    );
    let paths = [
        repo().join("packs/local.pi-adapter"),
        wrapper,
        repo().join("packs/local.harness-agent"),
    ];
    let verified = paths
        .iter()
        .map(|source| {
            Ok((
                source.clone(),
                tobkiri_manifest::verify_directory(source, &Default::default())?,
            ))
        })
        .collect::<Result<Vec<_>>>()?;
    let mut profile = serde_json::to_value(&fixture.profile)?;
    for (path, pack) in &verified {
        profile["packs"].as_array_mut().unwrap().push(serde_json::json!({"id":pack.manifest.id,"version":pack.manifest.version,"sha256":pack.hash,"source":format!("file:{}",path.display())}));
    }
    let ports = profile["ceiling"]["ports"].as_array_mut().unwrap();
    ports.push(serde_json::json!("local.pi-adapter.pi-extension@1.0"));
    ports.extend(operations.iter().map(|(port, _)| serde_json::json!(port)));
    if discovery {
        ports.push(serde_json::json!("local.pi-adapter.command-inventory@1.1"));
        profile["bindings"]["local.pi-adapter.command-inventory@1"] = serde_json::json!(example);
    }
    profile["bindings"]["local.pi-adapter.pi-extension@1"] = serde_json::json!(example);
    profile["bindings"]["local.harness-agent.tool-provider@1"] =
        serde_json::json!("local.pi-adapter");
    profile["bindings"]["local.harness-agent.tool-call-guard@1"] =
        serde_json::json!("local.pi-adapter");
    profile["bindings"]["local.harness-chat.command-provider@1"] =
        serde_json::json!("local.pi-adapter");
    fixture.profile = serde_json::from_value(profile)?;
    for (_, pack) in &verified {
        fixture.profile.pack_limits.insert(
            pack.manifest.id.clone(),
            pack.manifest
                .permissions
                .intersect(&fixture.profile.ceiling),
        );
    }
    let user = PermissionSet {
        ports: operations
            .iter()
            .map(|(shape, _)| (*shape).into())
            .collect(),
        wasm: fixture.profile.ceiling.wasm.clone(),
        loops: fixture.profile.ceiling.loops.clone(),
        ..Default::default()
    };
    fixture.profile.user_permissions = Some(user);
    fixture.host.utility_ports.lock().unwrap().extend(
        operations
            .iter()
            .map(|(shape, operation)| ((*shape).into(), (*operation).into())),
    );
    fixture.host.approve_literal_user(&fixture.profile)?;
    let (core, session) = fixture.start().await?;
    Ok((fixture, core, session))
}

#[tokio::test]
#[ignore = "requires separately built componentize-js Pi example components"]
async fn original_hello_runs_through_wrapper_adapter_and_gate() -> Result<()> {
    let shape = "local.harness-agent.tool-provider@1.0";
    let (_temporary, _core, session) =
        start_example("local.pi-example-hello", &[(shape, "invoke")], None).await?;
    let args = canon(&serde_json::json!({
        "call":{"id":"hello-1","name":"hello","arguments":{"name":"Tobkiri"}}
    }))?;
    let actual = session
        .call_user_port(ShapeRef::parse(shape)?, OpName::parse("invoke")?, &args)
        .await?;
    assert_eq!(
        parse_output(&actual)?,
        canon(
            &serde_json::json!({"content":[{"type":"text","text":"Hello, Tobkiri!"}],"details":{"greeted":"Tobkiri"}})
        )?
    );
    session.close().await?;
    println!(
        "Pi wasm runtime receipt: example=hello wrapper+adapter=true original_sha_pinned=true"
    );
    Ok(())
}

#[tokio::test]
#[ignore = "requires separately built componentize-js Pi example components"]
async fn original_protected_paths_guard_runs_through_wrapper_adapter_and_gate() -> Result<()> {
    let shape = "local.harness-agent.tool-call-guard@1.0";
    let (fixture, _core, session) = start_example(
        "local.pi-example-protected-paths",
        &[(shape, "on-tool-call")],
        None,
    )
    .await?;
    *fixture.host.notifications.lock().unwrap() = Some(Vec::new());
    for (name, path, block) in [
        ("write", ".env", true),
        ("edit", ".git/config", true),
        ("write", "node_modules/test.js", true),
        ("write", "src/readme.md", false),
        ("read", ".env", false),
    ] {
        let call = serde_json::json!({"id":"guard-1","name":name,"arguments":{"path":path}});
        let args = canon(&serde_json::json!({"call":call,"has-ui":true}))?;
        let actual = session
            .call_user_port(
                ShapeRef::parse(shape)?,
                OpName::parse("on-tool-call")?,
                &args,
            )
            .await
            .with_context(|| format!("upstream guard case {name}/{path}"))?;
        assert_eq!(
            parse_output(&actual)?,
            canon(
                &serde_json::json!({"block":block,"reason":if block {format!("Path \"{path}\" is protected")} else {String::new()},"input":call})
            )?,
            "upstream guard case {name}/{path}"
        );
        let notifications = {
            let mut host = fixture.host.notifications.lock().unwrap();
            std::mem::take(host.as_mut().unwrap())
        };
        if block {
            assert_eq!(
                notifications.len(),
                1,
                "one gated warning for {name}/{path}"
            );
            assert_eq!(
                serde_json::from_slice::<serde_json::Value>(notifications[0].as_bytes())?,
                serde_json::json!({
                    "text": format!("Blocked write to protected path: {path}"),
                    "level": "warning",
                })
            );
        } else {
            assert!(
                notifications.is_empty(),
                "unexpected warning for {name}/{path}"
            );
        }
    }
    session.close().await?;
    println!(
        "Pi wasm runtime receipt: example=protected-paths cases=5 wrapper+adapter=true gated-notifications=true"
    );
    Ok(())
}

#[tokio::test]
#[ignore = "requires the opt-in Rust Pi adapter and pinned wrapper with inventory"]
async fn pi_adapter_lists_registrations_through_verified_ports() -> Result<()> {
    let tools = "local.harness-agent.tool-provider@1.1";
    let commands = "local.harness-chat.command-provider@1.1";
    let legacy = "local.harness-agent.tool-provider@1.0";
    let wrapper = std::env::var_os("TOBKIRI_PI_DISCOVERY_WRAPPER")
        .map(std::path::PathBuf::from)
        .context("opt-in pinned wrapper directory required")?;
    let (_fixture, _core, session) = start_example(
        "local.pi-example-hello",
        &[
            (tools, "list-tools"),
            (commands, "list-commands"),
            (legacy, "invoke"),
        ],
        Some(wrapper),
    )
    .await?;
    let arguments = canon(&serde_json::json!({}))?;
    let tool_list = session
        .call_user_port(
            ShapeRef::parse(tools)?,
            OpName::parse("list-tools")?,
            &arguments,
        )
        .await?;
    let tool_list = parse_output(&tool_list)?;
    let tool_list = tool_list
        .as_array()
        .context("tool inventory is not a list")?;
    assert_eq!(tool_list.len(), 1);
    let tool = &tool_list[0];
    assert_eq!(
        tool.get("name").and_then(|value| value.as_str()),
        Some("hello")
    );
    assert_eq!(
        tool.get("description").and_then(|value| value.as_str()),
        Some("A simple greeting tool")
    );
    let name = tool
        .get("parameters")
        .and_then(|schema| schema.get("properties"))
        .and_then(|properties| properties.get("name"));
    assert_eq!(
        name.and_then(|schema| schema.get("type"))
            .and_then(|ty| ty.as_str()),
        Some("string")
    );
    let command_list = session
        .call_user_port(
            ShapeRef::parse(commands)?,
            OpName::parse("list-commands")?,
            &arguments,
        )
        .await?;
    assert_eq!(parse_output(&command_list)?, canon(&serde_json::json!([]))?);

    let request = canon(&serde_json::json!({
        "call":{"id":"hello-1","name":"hello","arguments":{"name":"Tobkiri"}}
    }))?;
    let reply = session
        .call_user_port(ShapeRef::parse(legacy)?, OpName::parse("invoke")?, &request)
        .await?;
    assert_eq!(
        parse_output(&reply)?,
        canon(
            &serde_json::json!({"content":[{"type":"text","text":"Hello, Tobkiri!"}],"details":{"greeted":"Tobkiri"}})
        )?
    );
    session.close().await?;
    Ok(())
}
