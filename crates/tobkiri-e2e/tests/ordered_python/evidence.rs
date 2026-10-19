//! Independently pinned inputs, never a fabricated build or runtime receipt.
use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs::{File, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
};
use tobkiri_canon::{Digest, Variant, ids::ShapeRef};
use tobkiri_manifest::VerifiedPack;

pub const PACK: &str = "local.ai-tool-bridge-ordered-python";
pub const SHAPE: &str = "local.ai-tool-bridge-ordered-python.bridge@1.0";
pub const INTERFACE: &str = "local:ai-tool-bridge-ordered-python/bridge@1.0.0";
pub const ORACLE_SHA: &str = "c571795b64aa470b12adf719f7946baa11129ed7f70556feb0f1fd3b272dd2fa";
const PLAN_SHA: &str = "0375ca57eb858a1ec70323eda9170090d7622c1c5bde25af15b520b368dca591";
const SOURCES_SHA: &str = "6f25971071bed0441534d0377166d71b284ee10caa65eea9645cac3ade96a955";
const EVIDENCE_LIMIT: usize = 4 * 1024 * 1024;

pub struct Inputs {
    pub source: PathBuf,
    pub pack: VerifiedPack,
    pub oracle: Value,
    pub identity: Value,
}

pub fn digest(bytes: &[u8]) -> String {
    Digest::hash(bytes)
        .to_string()
        .trim_start_matches("sha256:")
        .to_owned()
}

fn no_links(path: &Path) -> Result<()> {
    for part in path.ancestors() {
        ensure!(!part.is_symlink(), "evidence path contains a symlink");
    }
    Ok(())
}

fn bounded_read(path: &Path, limit: usize) -> Result<Vec<u8>> {
    no_links(path)?;
    let file = File::open(path)?;
    ensure!(
        file.metadata()?.is_file(),
        "evidence must be a regular file"
    );
    ensure!(
        file.metadata()?.len() <= limit as u64,
        "evidence exceeds size bound"
    );
    let mut bytes = Vec::new();
    file.take(limit as u64 + 1).read_to_end(&mut bytes)?;
    ensure!(bytes.len() <= limit, "evidence grew beyond size bound");
    Ok(bytes)
}

fn strict_json(bytes: &[u8]) -> Result<Value> {
    // All evidence is finite JSON metadata; ordered request/response strings
    // remain opaque. This also rejects duplicate metadata keys.
    tobkiri_canon::parse(bytes, Variant::StrictV1)?;
    Ok(serde_json::from_slice(bytes)?)
}

fn environment_hash(name: &str) -> Result<String> {
    let value = std::env::var(name).with_context(|| format!("explicit {name} pin required"))?;
    ensure!(
        value.len() == 64
            && value
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)),
        "invalid explicit SHA-256 pin"
    );
    Ok(value)
}

/// Bind every staged source to the reviewed source set and the current original
/// files. No host Python, producer, guest, or expected output is executed.
pub fn reviewed_sources(repo: &Path) -> Result<Value> {
    let candidate = repo.join("packs").join(PACK);
    let mut sources = BTreeMap::new();
    for name in [
        "app.py",
        "caller_adapter.py",
        "inventory.json",
        "original_bridge.py",
        "provenance.json",
    ] {
        sources.insert(
            name,
            bounded_read(&candidate.join("python").join(name), EVIDENCE_LIMIT)?,
        );
    }
    sources.insert(
        "ordered_json.py",
        bounded_read(&candidate.join("ordered_json.py"), EVIDENCE_LIMIT)?,
    );
    sources.insert(
        "reviewed-inputs.json",
        bounded_read(&candidate.join("oracle.freeze.json"), EVIDENCE_LIMIT)?,
    );
    sources.insert(
        "wit/pack.wit",
        bounded_read(&candidate.join("wit/pack.wit"), EVIDENCE_LIMIT)?,
    );
    sources.insert(
        "wit/deps/core.wit",
        bounded_read(
            &repo.join("crates/tobkiri-wit/wit/core/core.wit"),
            EVIDENCE_LIMIT,
        )?,
    );
    let manifest = Value::Array(
        sources
            .iter()
            .map(|(name, bytes)| {
                json!({
                    "source":name, "destination":name, "sha256":digest(bytes), "bytes":bytes.len()
                })
            })
            .collect(),
    );
    ensure!(
        digest(&serde_json::to_vec(&manifest)?) == SOURCES_SHA,
        "reviewed staged source set changed"
    );
    for name in ["provenance.json", "inventory.json"] {
        let document = strict_json(&sources[name])?;
        for record in document["sources"]
            .as_array()
            .context("source provenance missing")?
        {
            let relative = record["path"].as_str().context("source path missing")?;
            ensure!(
                Path::new(relative)
                    .components()
                    .all(|part| matches!(part, std::path::Component::Normal(_))),
                "non-relative source provenance"
            );
            let bytes = bounded_read(&repo.join(relative), 16 * 1024 * 1024)?;
            ensure!(
                record["sha256"] == digest(&bytes) && record["bytes"] == bytes.len(),
                "original source changed since extraction"
            );
        }
    }
    let frozen = strict_json(&sources["reviewed-inputs.json"])?;
    ensure!(
        frozen["oracle_sha256"] == ORACLE_SHA && frozen["case_count"] == 33,
        "reviewed oracle binding changed"
    );
    let freeze_script = bounded_read(
        &repo.join("scripts/wasm/freeze_ordered_tool_bridge.py"),
        EVIDENCE_LIMIT,
    )?;
    ensure!(
        frozen["freeze_script_sha256"] == digest(&freeze_script),
        "original freeze script changed"
    );
    Ok(manifest)
}

pub fn load(repo: &Path) -> Result<Inputs> {
    let supplied = PathBuf::from(
        std::env::var_os("TOBKIRI_TEST_ORDERED_PYTHON_PACK_DIR")
            .context("materialized ordered Pack path required")?,
    );
    no_links(&supplied)?;
    let source = supplied.canonicalize()?;
    let pack = tobkiri_manifest::verify_directory(&source, &Default::default())?;
    let pack_pin = environment_hash("TOBKIRI_TEST_ORDERED_PYTHON_PACK_SHA256")?;
    let component_pin = environment_hash("TOBKIRI_TEST_ORDERED_PYTHON_COMPONENT_SHA256")?;
    let receipt_pin = environment_hash("TOBKIRI_TEST_ORDERED_PYTHON_RECEIPT_SHA256")?;
    ensure!(
        pack.hash.to_string() == format!("sha256:{pack_pin}"),
        "verified Pack pin differs"
    );
    let binary = pack
        .files
        .get("component.wasm")
        .context("verified component missing")?;
    let receipt_bytes = pack
        .files
        .get("component.build.json")
        .context("verified build receipt missing")?;
    ensure!(
        digest(binary) == component_pin && digest(receipt_bytes) == receipt_pin,
        "component or build receipt pin differs"
    );
    let receipt = strict_json(receipt_bytes)?;
    let sources = reviewed_sources(repo)?;
    ensure!(
        receipt["format"] == "tobkiri.component-build/2"
            && receipt["language"] == "python"
            && receipt["world"] == "pack",
        "wrong build receipt kind"
    );
    ensure!(
        receipt["artifact_sha256"] == component_pin && receipt["artifact_bytes"] == binary.len(),
        "receipt artifact binding differs"
    );
    ensure!(
        receipt["build_plan_sha256"] == PLAN_SHA
            && receipt["source_manifest_sha256"] == SOURCES_SHA
            && receipt["source_manifest"] == sources,
        "receipt does not bind the complete reviewed source plan"
    );
    let imports: BTreeSet<String> = ["types", "flow", "clock", "random", "audit"]
        .into_iter()
        .map(|name| format!("tobkiri:core/{name}@1.0.0"))
        .collect();
    let exports: BTreeSet<String> = [
        INTERFACE.to_owned(),
        "tobkiri:core/lifecycle@1.0.0".to_owned(),
    ]
    .into_iter()
    .collect();
    ensure!(
        receipt["imports"] == json!(imports) && receipt["exports"] == json!(exports),
        "receipt import/export surface differs"
    );
    // Inspect the actual binary, not just the receipt. Do not instantiate it or
    // supply trap imports. Core::start independently validates and links it.
    {
        let engine = tobkiri_wasm::WasmEngine::new()?;
        let component = wasmtime::component::Component::new(engine.engine(), binary)?;
        let allowed = tobkiri_manifest::allowed_imports_for_pack(&pack)?;
        let actual: BTreeSet<_> =
            tobkiri_wasm::validate_imports(engine.engine(), &component, &allowed)?
                .into_iter()
                .collect();
        let actual_exports: BTreeSet<_> = component
            .component_type()
            .exports(engine.engine())
            .map(|(name, _)| name.to_owned())
            .collect();
        ensure!(
            actual == imports && actual_exports == exports,
            "actual component import/export surface differs"
        );
    }
    let manifest = serde_json::to_value(&pack.manifest)?;
    ensure!(
        pack.manifest.id.as_str() == PACK
            && manifest["runtime"]["kind"] == "wasm"
            && manifest["runtime"]["component"] == "component.wasm",
        "not the ordered Python wasm Pack"
    );
    let no_authority = tobkiri_gate::PermissionSet {
        wasm: pack.manifest.permissions.wasm.clone(),
        loops: pack.manifest.permissions.loops.clone(),
        ..Default::default()
    };
    ensure!(
        pack.manifest.permissions == no_authority,
        "candidate requests authority beyond computation"
    );
    ensure!(
        pack.manifest.ports.consumes.is_empty()
            && pack.manifest.ports.untyped.is_empty()
            && pack.manifest.inputs.is_empty()
            && pack.manifest.flows.is_empty()
            && pack.manifest.overlays.is_empty()
            && pack.manifest.stores.is_empty()
            && pack.manifest.ui.is_none(),
        "candidate has unexpected execution surfaces"
    );
    let shape = ShapeRef::parse(SHAPE)?;
    ensure!(
        pack.shapes.len() == 1 && pack.shapes[0].reference == shape,
        "candidate shape differs"
    );
    ensure!(
        pack.shapes[0]
            .functions
            .keys()
            .map(|op| op.as_str())
            .collect::<BTreeSet<_>>()
            == ["normalize", "runtime-info"].into_iter().collect(),
        "runtime-info or normalize is not a verified public operation"
    );
    let oracle_bytes = bounded_read(
        &repo.join("packs").join(PACK).join("oracle.json"),
        EVIDENCE_LIMIT,
    )?;
    ensure!(
        digest(&oracle_bytes) == ORACLE_SHA,
        "the frozen 33-case oracle changed"
    );
    let oracle = strict_json(&oracle_bytes)?;
    ensure!(
        oracle["format"] == "tobkiri.ordered-python-tool-bridge-oracle/1"
            && oracle["protocol"] == "ordered-finite-json-v1"
            && oracle["python"] == "3.13.5"
            && oracle["unicode"] == "15.1.0",
        "wrong original oracle identity"
    );
    let identity = json!({"pack_sha256":pack_pin, "component_sha256":component_pin, "build_receipt_sha256":receipt_pin, "oracle_sha256":ORACLE_SHA, "source_manifest_sha256":SOURCES_SHA, "build_plan_sha256":PLAN_SHA, "actual_imports":imports, "actual_exports":exports});
    Ok(Inputs {
        source,
        pack,
        oracle,
        identity,
    })
}

/// The opt-in receipt must be fresh and outside the tracked source tree.
pub fn receipt_destination(repo: &Path) -> Result<PathBuf> {
    let path = PathBuf::from(
        std::env::var_os("TOBKIRI_TEST_ORDERED_PYTHON_EVIDENCE")
            .context("explicit external evidence output path required")?,
    );
    ensure!(
        path.is_absolute() && !path.exists(),
        "evidence output must be a fresh absolute path"
    );
    no_links(&path)?;
    let parent = path
        .parent()
        .context("evidence output has no parent")?
        .canonicalize()?;
    ensure!(
        !parent.starts_with(repo.canonicalize()?),
        "evidence output must be outside the source tree"
    );
    Ok(parent.join(path.file_name().context("evidence output name missing")?))
}

pub fn write_receipt(path: &Path, report: &Value) -> Result<()> {
    no_links(path)?;
    let bytes = serde_json::to_vec_pretty(report)?;
    ensure!(
        bytes.len() <= EVIDENCE_LIMIT,
        "evidence output exceeded bound"
    );
    let mut file = OpenOptions::new().create_new(true).write(true).open(path)?;
    file.write_all(&bytes)?;
    file.write_all(b"\n")?;
    file.sync_all()?;
    Ok(())
}

#[test]
fn metadata_parser_rejects_duplicate_keys_and_nonfinite_values() {
    assert!(strict_json(br#"{"artifact_sha256":"first","artifact_sha256":"second"}"#).is_err());
    assert!(strict_json(br#"{"artifact_bytes":NaN}"#).is_err());
}

#[test]
fn diagnostic_receipt_never_overwrites_an_existing_result() -> Result<()> {
    let directory = tempfile::tempdir()?;
    let path = directory.path().join("failure.json");
    let failure = json!({"core_runtime_path_passed":false});
    write_receipt(&path, &failure)?;
    ensure!(
        write_receipt(&path, &json!({"core_runtime_path_passed":true})).is_err(),
        "existing result was overwritten"
    );
    ensure!(
        strict_json(&std::fs::read(path)?)? == failure,
        "failed rewrite changed the original result"
    );
    Ok(())
}
