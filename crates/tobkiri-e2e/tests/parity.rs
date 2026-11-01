//! Optional frozen legacy fixtures replay against a real Python wasm component.
mod support;
use anyhow::{Context, Result};
use std::{path::PathBuf, sync::Arc};
use support::*;
use tobkiri_canon::ids::{OpName, ShapeRef};
use tobkiri_core::{Core, CoreConfig, CoreError};
use tobkiri_gate::PermissionSet;
use tobkiri_profile::Profile;

#[tokio::test]
#[ignore = "requires the separately built componentize-py shell policy component"]
async fn shell_policy_frozen_inputs_replay_through_the_real_rust_host() -> Result<()> {
    let source = std::env::var_os("TOBKIRI_TEST_PYTHON_PACK_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| repo().join("packs/local.shell-policy-python"))
        .canonicalize()
        .context("reviewed Python Pack fixture directory is absent")?;
    anyhow::ensure!(
        source.join("component.wasm").is_file(),
        "build the reviewed Python component before parity replay"
    );
    let pack = tobkiri_manifest::verify_directory(&source, &Default::default())?;
    let fixture_bytes = std::fs::read(source.join("tests/inspection.json"))?;
    assert_eq!(
        tobkiri_canon::Digest::hash(&fixture_bytes).to_string(),
        "sha256:504e542a6e4cec983e402753db7a3bf93a96f394b69400249d353d1e7442edd5",
        "the original 91-case corpus, including its order, must remain unchanged"
    );
    let fixtures: serde_json::Value = serde_json::from_slice(&fixture_bytes)?;
    assert_eq!(
        fixtures["source_sha256"],
        "4474513154ced3474da4db2d6bf90ece67c0da9406ee3ac392660d7bd9ddcf51"
    );
    let profile: Profile = serde_json::from_value(serde_json::json!({
        "format":"tobkiri.profile/1","id":"prof_python_parity","name":"Python shell policy parity",
        "packs":[{"id":pack.manifest.id,"version":pack.manifest.version,"sha256":pack.hash,"source":format!("file:{}",source.display())}],
        "publishers":{},"ceiling":pack.manifest.permissions,
        "pack_limits":{"local.shell-policy-python":pack.manifest.permissions},
        "bindings":{"local.shell-policy-python.policy@1":"local.shell-policy-python"},
        "overlay_order":[],"user_overlays":[],"startup":{"inputs":[],"ui":null}
    }))?;
    let mut profile = profile;
    profile
        .ceiling
        .ports
        .insert("local.shell-policy-python.policy@1.0".into());
    let user = PermissionSet {
        ports: ["local.shell-policy-python.policy@1.0".to_string()]
            .into_iter()
            .collect(),
        wasm: profile.ceiling.wasm.clone(),
        loops: profile.ceiling.loops.clone(),
        ..Default::default()
    };
    profile.user_permissions = Some(user);
    let temporary = physical_temp()?;
    let host = Arc::new(Host::default());
    host.utility_ports.lock().unwrap().insert((
        "local.shell-policy-python.policy@1.0".into(),
        "inspect".into(),
    ));
    host.approve_literal_user(&profile)?;
    let core = Core::open(
        CoreConfig::for_test_data_root(temporary.path().join("data")),
        host,
    )?;
    core.save_profile(profile.clone(), None).await?;
    let session = core.start(&profile.id).await?;
    let cases = fixtures["cases"]
        .as_array()
        .context("frozen parity cases missing")?;
    assert_eq!(cases.len(), 91);
    assert_eq!(
        cases
            .iter()
            .filter(|case| case["input"]["operation"] == "classify")
            .count(),
        87
    );
    assert_eq!(
        cases
            .iter()
            .filter(|case| case["input"]["operation"] == "tokenize")
            .count(),
        4
    );
    assert_eq!(
        cases
            .iter()
            .filter(|case| case["expect"].get("ok").is_some())
            .count(),
        88
    );
    assert_eq!(
        cases
            .iter()
            .filter(|case| case["expect"]["error_class"] == "ValueError")
            .count(),
        3
    );
    for (index, case) in cases.iter().enumerate() {
        let input = canon(&case["input"])?;
        let actual = session
            .call_user_port(
                ShapeRef::parse("local.shell-policy-python.policy@1.0")?,
                OpName::parse("inspect")?,
                &input,
            )
            .await;
        if let Some(expected) = case["expect"].get("ok") {
            let buffer = actual?;
            assert_eq!(
                parse_output(&buffer)?,
                canon(expected)?,
                "frozen legacy case {index}"
            );
        } else {
            let error = actual.err().ok_or_else(|| {
                anyhow::anyhow!("legacy invalid case {index} unexpectedly succeeded")
            })?;
            let expected_class = case["expect"]["error_class"]
                .as_str()
                .context("frozen error class missing")?;
            let expected_code = fixtures["error_mapping"][expected_class]
                .as_str()
                .context("frozen error mapping missing")?;
            assert!(
                matches!(&error, CoreError::Step(step) if step.code == expected_code),
                "legacy validation error mapping changed for case {index}: {error}"
            );
        }
    }
    println!(
        "Python wasm parity receipt: cases={} component_bytes={} scope=classifier-and-tokenizer",
        cases.len(),
        source.join("component.wasm").metadata()?.len()
    );
    // This is a handler-level parity receipt, not permission to retire a legacy
    // directory or delete any Python runtime. The inventory remains authoritative.
    Ok(())
}
