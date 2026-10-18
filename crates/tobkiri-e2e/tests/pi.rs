//! Optional runtime proof for the two pinned, unchanged upstream Pi examples.
//! These tests do not claim the complete Harness tool-loop/FS acceptance.
mod support;
use anyhow::Result;
use support::*;
use tobkiri_canon::ids::{OpName, ShapeRef};
use tobkiri_core::{Core, Session};
use tobkiri_gate::PermissionSet;

async fn start_example(
    example: &str,
    shape: &str,
    operation: &str,
) -> Result<(Fixture, Core, Session)> {
    let mut fixture = Fixture::new("harness-mock", None)?;
    let wrapper = repo().join("packs").join(example);
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
    ports.extend(["local.pi-adapter.pi-extension@1.0", shape].map(|port| serde_json::json!(port)));
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
        ports: [shape.to_string()].into_iter().collect(),
        wasm: fixture.profile.ceiling.wasm.clone(),
        loops: fixture.profile.ceiling.loops.clone(),
        ..Default::default()
    };
    fixture.profile.user_permissions = Some(user);
    fixture
        .host
        .utility_ports
        .lock()
        .unwrap()
        .insert((shape.into(), operation.into()));
    fixture.host.approve_literal_user(&fixture.profile)?;
    let (core, session) = fixture.start().await?;
    Ok((fixture, core, session))
}

#[tokio::test]
#[ignore = "requires separately built componentize-js Pi example components"]
async fn original_hello_runs_through_wrapper_adapter_and_gate() -> Result<()> {
    let shape = "local.harness-agent.tool-provider@1.0";
    let (_temporary, _core, session) =
        start_example("local.pi-example-hello", shape, "invoke").await?;
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
    println!(
        "Pi wasm runtime receipt: example=hello wrapper+adapter=true original_sha_pinned=true"
    );
    Ok(())
}

#[tokio::test]
#[ignore = "requires separately built componentize-js Pi example components"]
async fn original_protected_paths_guard_runs_through_wrapper_adapter_and_gate() -> Result<()> {
    let shape = "local.harness-agent.tool-call-guard@1.0";
    let (_temporary, _core, session) =
        start_example("local.pi-example-protected-paths", shape, "on-tool-call").await?;
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
            .await?;
        assert_eq!(
            parse_output(&actual)?,
            canon(
                &serde_json::json!({"block":block,"reason":if block {format!("Path \"{path}\" is protected")} else {String::new()},"input":call})
            )?,
            "upstream guard case {name}/{path}"
        );
    }
    println!(
        "Pi wasm runtime receipt: example=protected-paths cases=5 wrapper+adapter=true scope=guard-decision-only"
    );
    Ok(())
}
