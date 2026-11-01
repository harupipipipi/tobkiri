//! Verify native acceptance source assets without claiming a WebView execution.
use anyhow::Result;
use std::path::Path;
use tobkiri_manifest::RuntimeSpec;

#[test]
fn native_probe_ui_is_a_verified_pack_with_only_its_declared_port() -> Result<()> {
    let repo = Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(2)
        .unwrap();
    let ui = repo.join("packs/native-acceptance/ui");
    let verified = tobkiri_manifest::verify_directory(&ui, &Default::default())?;
    assert_eq!(verified.manifest.id.as_str(), "local.native-acceptance-ui");
    assert!(matches!(verified.manifest.runtime, RuntimeSpec::Data));
    let definition = verified.manifest.ui.as_ref().unwrap();
    assert_eq!(definition.entry, "index.html");
    assert_eq!(
        definition
            .ports_used
            .iter()
            .map(ToString::to_string)
            .collect::<Vec<_>>(),
        ["local.native-acceptance-provider.review@1.0"]
    );

    // This UI must not inherit the callee provider's cross-Profile right, or
    // gain ambient effects merely to run its denial probes.
    let permissions = &verified.manifest.permissions;
    let expected = tobkiri_gate::PermissionSet {
        ports: ["local.native-acceptance-provider.review@1.0".to_string()]
            .into_iter()
            .collect(),
        audit_append: true,
        wasm: permissions.wasm.clone(),
        loops: permissions.loops.clone(),
        ..Default::default()
    };
    assert_eq!(permissions, &expected);
    assert!(verified.manifest.ports.provides.is_empty());
    assert!(verified.manifest.stores.is_empty());
    assert!(verified.manifest.flows.is_empty());

    // The native fixture uses the real SDK transport, including its one-use
    // channel checks, rather than a special test-only bridge.
    for (asset, source) in [
        ("ports.js", "sdk/js/pack-ui/index.js"),
        ("float.js", "sdk/js/pack-ui/float.js"),
    ] {
        assert_eq!(
            verified.files.get(asset).unwrap().read()?,
            &std::fs::read(repo.join(source))?,
            "native fixture SDK copy drifted: {asset}"
        );
    }
    Ok(())
}
