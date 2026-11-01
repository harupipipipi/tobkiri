//! Build a new pinned test graph from authenticated originals and extracted code.
//! Receipt labels grant no runtime authority; Core re-verifies each new Pack.
use super::support::shared_modules::{INPUT_LIMIT, artifact, transform_pack};
use super::{evidence, evidence_gateway_tool_caller::Graph};
use anyhow::{Context, Result, ensure};
use serde_json::json;
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
};
use tobkiri_canon::Digest;
use tobkiri_manifest::{FileEntry, RuntimeSpec};

pub fn transform(mut graph: Graph, destination: &Path) -> Result<Graph> {
    let bundle = PathBuf::from(
        std::env::var_os("TOBKIRI_TEST_SHARED_MODULES_DIR")
            .context("separately materialized module bundle required")?,
    );
    ensure!(bundle.is_absolute(), "bundle path must be absolute");
    evidence::no_links(&bundle)?;
    let raw = evidence::bounded_read(&bundle, "extraction.json", 4 * 1024 * 1024)?;
    let pin = evidence::environment_hash("TOBKIRI_TEST_SHARED_MODULES_SHA256")?;
    ensure!(
        evidence::digest(&raw) == pin,
        "module extraction receipt pin differs"
    );
    let receipt = evidence::strict_json(&raw)?;
    ensure!(
        receipt["format"] == "tobkiri.shared-core-module-materialization/1"
            && receipt["guest_execution_performed"] == false
            && receipt["native_core_acceptance"] == false
            && receipt["production_enabled"] == false,
        "materialization is not runtime acceptance"
    );
    let records = receipt["components"]
        .as_array()
        .context("component closure missing")?;
    ensure!(
        records.len() == 2,
        "exact original bridge/caller input order required"
    );
    fs::create_dir(destination)?;
    let mut unique = BTreeMap::new();
    let bridge = transform_pack(
        &graph.bridge,
        &bundle,
        0,
        &destination.join("bridge"),
        &records[0],
        &mut unique,
    )?;
    let caller = transform_pack(
        &graph.caller,
        &bundle,
        1,
        &destination.join("caller"),
        &records[1],
        &mut unique,
    )?;
    let roots = bridge.files["component.wasm"].len() + caller.files["component.wasm"].len();
    let source_bytes = roots + unique.values().sum::<u64>();
    ensure!(
        source_bytes <= 64 * 1024 * 1024
            && receipt["catalog_source_bytes"] == source_bytes
            && receipt["unique_modules"] == unique.len(),
        "actual dependency closure accounting differs"
    );
    graph.identity["shared_core_modules"] = json!({
        "extraction_receipt_sha256": pin, "catalog_source_bytes": source_bytes,
        "unique_modules": unique.len(), "original_pack_hashes": {
            "bridge":graph.bridge.hash,"caller":graph.caller.hash},
        "transformed_pack_hashes":{"bridge":bridge.hash,"caller":caller.hash},
        "transformed_manifests":{"bridge":bridge.manifest,"caller":caller.manifest},
        "materialization_is_not_runtime_acceptance":true,
    });
    graph.bridge = bridge;
    graph.caller = caller;
    graph.root = destination.to_path_buf();
    Ok(graph)
}

#[test]
fn extracted_artifact_requires_exact_bytes_encoding_and_fresh_output() -> Result<()> {
    let temporary = tempfile::tempdir()?;
    let root = temporary.path().canonicalize()?;
    let code = b"\0asm\x01\0\0\0";
    fs::write(root.join("input.wasm"), code)?;
    let record = json!({"bytes":code.len(),"sha256":evidence::digest(code)});
    let good = artifact(&root, "input.wasm", &root, "output.wasm", &record, false)?;
    assert_eq!(good.sha256, Digest::hash(code));
    fs::write(root.join("input.wasm"), b"\0asm\x02\0\0\0")?;
    assert!(artifact(&root, "input.wasm", &root, "changed.wasm", &record, false).is_err());
    assert!(!root.join("changed.wasm").exists());
    fs::write(root.join("input.wasm"), code)?;
    assert!(artifact(&root, "input.wasm", &root, "wrong-kind.wasm", &record, true).is_err());
    let mut oversized = record.clone();
    oversized["bytes"] = json!(INPUT_LIMIT + 1);
    assert!(
        artifact(
            &root,
            "input.wasm",
            &root,
            "oversized.wasm",
            &oversized,
            false
        )
        .is_err()
    );
    assert!(artifact(&root, "input.wasm", &root, "output.wasm", &record, false).is_err());
    assert_eq!(fs::read(root.join("output.wasm"))?, code);
    Ok(())
}

#[test]
fn shared_bundle_rechecks_its_exact_pin_scope_and_component_count() -> Result<()> {
    use super::support::shared_modules::load;
    let temporary = tempfile::tempdir()?;
    let root = temporary.path().canonicalize()?;
    let value = json!({"format":"tobkiri.shared-core-module-materialization/1",
        "components":[{},{},{},{},{}],"guest_execution_performed":false,
        "native_core_acceptance":false,"production_enabled":false});
    let raw = serde_json::to_vec(&value)?;
    fs::write(root.join("extraction.json"), &raw)?;
    let pin = evidence::digest(&raw);
    assert_eq!(load(&root, &pin, 5)?, value);
    assert!(load(&root, &pin, 2).is_err());
    assert!(load(&root, &"0".repeat(64), 5).is_err());
    for field in [
        "guest_execution_performed",
        "native_core_acceptance",
        "production_enabled",
    ] {
        let mut changed = value.clone();
        changed[field] = json!(true);
        let bytes = serde_json::to_vec(&changed)?;
        fs::write(root.join("extraction.json"), &bytes)?;
        assert!(load(&root, &evidence::digest(&bytes), 5).is_err());
    }
    let duplicate = br#"{"format":"first","format":"second"}"#;
    fs::write(root.join("extraction.json"), duplicate)?;
    assert!(load(&root, &evidence::digest(duplicate), 5).is_err());
    Ok(())
}

#[test]
fn transformed_pack_preserves_original_manifest_fields_and_asset_bytes() -> Result<()> {
    use tobkiri_manifest::{PackManifest, Ports, build_tpk, verify_tpk};
    let files = BTreeMap::from([
        ("component.wasm".into(), b"\0asm\x0d\0\x01\0".to_vec()),
        (
            "assets/original.bin".into(),
            b"original asset\0\xff".to_vec(),
        ),
    ]);
    let manifest = PackManifest {
        format: "tobkiri.pack/1".into(),
        id: "local.transform-fixture".parse()?,
        version: "1.0.0".into(),
        publisher_key: None,
        runtime: RuntimeSpec::Wasm {
            component: "component.wasm".into(),
            toolchain: "file-integrity-test-only".into(),
            core_modules: BTreeMap::new(),
            config: None,
        },
        shapes: vec![],
        ports: Ports::default(),
        inputs: vec![],
        flows: vec![],
        overlays: vec![],
        stores: vec![],
        permissions: Default::default(),
        ui: None,
        files: files
            .iter()
            .map(|(path, bytes): (&String, &Vec<u8>)| FileEntry {
                path: path.clone(),
                sha256: Digest::hash(bytes),
                size: bytes.len() as u64,
            })
            .collect(),
    };
    let original = verify_tpk(&build_tpk(&manifest, &files, None)?, &BTreeMap::new())?;
    let temporary = tempfile::tempdir()?;
    let root = temporary.path().canonicalize()?;
    fs::create_dir(root.join("0"))?;
    fs::write(root.join("0/component.wasm"), &files["component.wasm"])?;
    let code = b"\0asm\x01\0\0\0";
    fs::write(root.join("0/module-0.wasm"), code)?;
    let record = json!({"source_bytes":8,"source_sha256":evidence::digest(&files["component.wasm"]),
        "component":{"bytes":8,"sha256":evidence::digest(&files["component.wasm"]),
        "modules":[{"import":"unbundled-module0","bytes":8,"sha256":evidence::digest(code)}]}});
    let mut unique = BTreeMap::new();
    let transformed = transform_pack(
        &original,
        &root,
        0,
        &root.join("pack"),
        &record,
        &mut unique,
    )?;
    assert_ne!(original.hash, transformed.hash);
    assert_eq!(
        transformed.files["assets/original.bin"].read()?.as_ref(),
        files["assets/original.bin"].as_slice()
    );
    let mut after = serde_json::to_value(&transformed.manifest)?;
    after["runtime"]
        .as_object_mut()
        .unwrap()
        .remove("core_modules");
    after["files"] = serde_json::to_value(&original.manifest.files)?;
    assert_eq!(after, serde_json::to_value(&original.manifest)?);
    let mut wrong_source = record.clone();
    wrong_source["source_sha256"] = json!(evidence::digest(b"other source"));
    assert!(
        transform_pack(
            &original,
            &root,
            0,
            &root.join("wrong"),
            &wrong_source,
            &mut unique
        )
        .is_err()
    );
    assert!(!root.join("wrong").exists());
    // Empty valid binaries test only file/manifest integrity. No execution or
    // Host admission is claimed; actual shared imports use the ignored replay.
    Ok(())
}
