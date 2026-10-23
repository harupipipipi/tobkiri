//! Build a new pinned test graph from authenticated originals and extracted code.
//! Receipt labels grant no runtime authority; Core re-verifies each new Pack.
use super::{evidence, evidence_gateway_tool_caller::Graph};
use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
};
use tobkiri_canon::Digest;
use tobkiri_manifest::{FileEntry, RuntimeSpec, VerifiedPack};

const INPUT_LIMIT: u64 = 32 * 1024 * 1024;

fn artifact(
    bundle: &Path,
    relative: &str,
    destination: &Path,
    pack_path: &str,
    record: &Value,
    component: bool,
) -> Result<FileEntry> {
    let size = record["bytes"].as_u64().context("extracted size missing")?;
    ensure!(
        (8..=INPUT_LIMIT).contains(&size),
        "extracted input bound exceeded"
    );
    let hash = Digest::parse(&format!(
        "sha256:{}",
        record["sha256"]
            .as_str()
            .context("extracted digest missing")?
    ))?;
    let bytes = evidence::bounded_read(bundle, relative, size as usize)?;
    ensure!(
        bytes.len() as u64 == size && Digest::hash(&bytes) == hash,
        "extracted bytes differ from pinned receipt"
    );
    let header: &[u8] = if component {
        b"\0asm\x0d\0\x01\0"
    } else {
        b"\0asm\x01\0\0\0"
    };
    ensure!(bytes.starts_with(header), "wrong extracted Wasm encoding");
    let target = destination.join(pack_path);
    fs::create_dir_all(target.parent().context("artifact parent missing")?)?;
    use std::io::Write;
    fs::File::create_new(target)?.write_all(&bytes)?;
    Ok(FileEntry {
        path: pack_path.into(),
        sha256: hash,
        size,
    })
}

fn transform_pack(
    original: &VerifiedPack,
    bundle: &Path,
    index: usize,
    destination: &Path,
    record: &Value,
    unique: &mut BTreeMap<Digest, u64>,
) -> Result<VerifiedPack> {
    let mut manifest = original.manifest.clone();
    let RuntimeSpec::Wasm {
        component,
        core_modules,
        ..
    } = &mut manifest.runtime
    else {
        anyhow::bail!("original must be an ordinary verified Wasm Pack")
    };
    ensure!(
        core_modules.is_empty(),
        "original already has external module bindings"
    );
    let source = original
        .files
        .get(component.as_str())
        .context("original component missing")?;
    ensure!(
        record["source_sha256"] == source.digest().to_string().trim_start_matches("sha256:")
            && record["source_bytes"] == source.len(),
        "transformation is not bound to this original component"
    );
    let modules = record["component"]["modules"]
        .as_array()
        .context("module closure missing")?;
    ensure!(
        !modules.is_empty() && modules.len() <= tobkiri_manifest::MAX_CORE_MODULE_IMPORTS,
        "bounded nonempty shared dependency closure required"
    );
    fs::create_dir(destination)?;
    let root = artifact(
        bundle,
        &format!("{index}/component.wasm"),
        destination,
        component,
        &record["component"],
        true,
    )?;
    let original_root = manifest
        .files
        .iter_mut()
        .find(|file| file.path == *component)
        .context("original root inventory entry missing")?;
    *original_root = root.clone();
    let mut distinct = BTreeMap::new();
    for (module_index, module) in modules.iter().enumerate() {
        let name = format!("unbundled-module{module_index}");
        ensure!(module["import"] == name, "extracted import order changed");
        let path = format!("core-modules/module-{module_index}.wasm");
        ensure!(
            !original.files.contains_key(&path),
            "dependency path collides with original inventory"
        );
        let file = artifact(
            bundle,
            &format!("{index}/module-{module_index}.wasm"),
            destination,
            &path,
            module,
            false,
        )?;
        if let Some(old) = unique.insert(file.sha256.clone(), file.size) {
            ensure!(old == file.size, "inconsistent dependency identity");
        }
        distinct.insert(file.sha256.clone(), file.size);
        core_modules.insert(name, path);
        manifest.files.push(file);
    }
    ensure!(
        root.size + distinct.values().sum::<u64>() <= INPUT_LIMIT,
        "Pack dependency closure exceeds unchanged 32 MiB bound"
    );
    for (path, source) in original.files.iter() {
        if path.as_str() == component.as_str() {
            continue;
        }
        let target = destination.join(path);
        fs::create_dir_all(target.parent().context("original asset parent missing")?)?;
        let mut output = fs::File::create_new(target)?;
        source.inspect(|reader| {
            std::io::copy(reader, &mut output)?;
            Ok(())
        })?;
    }
    fs::write(
        destination.join("pack.json"),
        tobkiri_manifest::canonical_manifest(&manifest)?,
    )?;
    let transformed = tobkiri_manifest::verify_directory(destination, &BTreeMap::new())?;
    ensure!(
        transformed.hash != original.hash,
        "new dependency closure must have a new Profile pin"
    );
    Ok(transformed)
}

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
