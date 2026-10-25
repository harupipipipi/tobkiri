//! Bounded, hash-bound fixture inputs. No producer or Python oracle executes here.
use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
};
use tobkiri_canon::{Digest, Variant, ids::ShapeRef};
use tobkiri_gate::{LoopLimits, PermissionSet, ResourceLimits};
use tobkiri_manifest::{
    FileEntry, PackManifest, PortDeclaration, Ports, RuntimeSpec, ShapeDefinition, ShapeKind,
    VerifiedPack,
};
use tobkiri_profile::PackPin;

pub const PACK: &str = "local.harness-setup-python";
pub const SHAPE: &str = "local.harness-setup-python.setup@1.0";
pub const OPERATIONS: [&str; 6] = [
    "decision",
    "success",
    "failure",
    "retired",
    "bootstrap-confirmation",
    "profile-confirmation",
];
pub const IDS: [&str; 30] = [
    "exact",
    "reordered",
    "stale",
    "false",
    "integer-one",
    "extra-approved",
    "missing-key",
    "renamed-operation",
    "malformed-confirmation",
    "missing-preview",
    "host-preparation",
    "host-preparation-malformed",
    "intermediate-success",
    "failure",
    "retired",
    "retired-route",
    "invalid-shape",
    "duplicate-key",
    "bootstrap-confirmation",
    "bootstrap-definition-predecessor",
    "bootstrap-pin-predecessor",
    "bootstrap-both-predecessors",
    "bootstrap-empty-predecessors",
    "named-profile-confirmation",
    "reordered-profile-confirmation",
    "confirmation-unsafe-integer",
    "confirmation-floating-point",
    "confirmation-missing-plan",
    "confirmation-extra-key",
    "confirmation-invalid-predecessor",
];
const PREFIX: &str = "TOBKIRI_TEST_SETUP";
const SOURCE: &str =
    "tobkiri_runtime/ecosystem/defaultspack/defaultspack/profile_runtime_composition.py";
const PORT: &str = "tobkiri_runtime/core_runtime/profile_runtime_port.py";
const PACK_ROOT: &str = "packs/local.harness-setup-python";
const CODEC_ROOT: &str = "packs/local.ai-tool-bridge-ordered-python";
const CORE_WIT: &str = "crates/tobkiri-wit/wit/core/core.wit";

pub struct Inputs {
    pub cases: Vec<Value>,
    pub component: Vec<u8>,
    pub identity: Value,
    pub evidence: PathBuf,
}

fn path(label: &str) -> Result<PathBuf> {
    let path = PathBuf::from(
        std::env::var_os(format!("{PREFIX}_{label}"))
            .context("explicit native Setup input required; missing inputs do not skip green")?,
    );
    ensure!(
        path.is_absolute() && path.ancestors().all(|p| !p.is_symlink()),
        "physical absolute native Setup path required"
    );
    Ok(path)
}

pub fn read(path: &Path, maximum: usize) -> Result<Vec<u8>> {
    ensure!(
        path.ancestors().all(|p| !p.is_symlink()),
        "linked fixture input"
    );
    Ok(tobkiri_canon::files::read_relative(
        path.parent().context("input parent missing")?,
        path.file_name()
            .and_then(|name| name.to_str())
            .context("UTF-8 input filename required")?,
        maximum as u64,
    )?)
}

fn pinned(label: &str, maximum: usize) -> Result<Vec<u8>> {
    let bytes = read(&path(label)?, maximum)?;
    ensure!(
        Digest::hash(&bytes).hex() == std::env::var(format!("{PREFIX}_{label}_SHA256"))?,
        "native Setup input hash differs"
    );
    Ok(bytes)
}

fn document(bytes: &[u8]) -> Result<Value> {
    // Reject duplicate outer metadata keys before ordinary inspection.
    tobkiri_canon::parse(bytes, Variant::StrictV1)?;
    Ok(serde_json::from_slice(bytes)?)
}

pub fn load(repo: &Path) -> Result<Inputs> {
    let component = pinned("COMPONENT", 32 * 1024 * 1024)?;
    ensure!(
        component.starts_with(b"\0asm\x0d\0\x01\0"),
        "actual component required"
    );
    let receipt_bytes = pinned("RECEIPT", 256 * 1024)?;
    let oracle_bytes = pinned("ORACLE", 1024 * 1024)?;
    let receipt = document(&receipt_bytes)?;
    let oracle = document(&oracle_bytes)?;
    ensure!(
        oracle["format"] == "tobkiri.harness-setup-native-inputs/1"
            && oracle["native_core_replay"] == false
            && oracle["full_setup_gui_tested"] == false
            && oracle["production_enabled"] == false,
        "inactive source oracle required"
    );
    let mut sources = BTreeMap::new();
    for item in oracle["source_bindings"]
        .as_array()
        .context("source bindings missing")?
    {
        let relative = item["path"].as_str().context("source path missing")?;
        let bytes = tobkiri_canon::files::read_relative(repo, relative, 8 * 1024 * 1024)?;
        ensure!(
            item["sha256"] == Digest::hash(&bytes).hex() && item["bytes"] == bytes.len(),
            "current original/transport source differs"
        );
        ensure!(
            sources.insert(relative.to_owned(), bytes).is_none(),
            "duplicate source binding"
        );
    }
    let mut mappings = BTreeMap::from([
        ("python/app.py", format!("{PACK_ROOT}/python/app.py")),
        (
            "python/setup_adapter.py",
            format!("{PACK_ROOT}/python/setup_adapter.py"),
        ),
        (
            "python/original_setup.py",
            format!("{PACK_ROOT}/python/original_setup.py"),
        ),
        (
            "python/ordered_json.py",
            format!("{CODEC_ROOT}/ordered_json.py"),
        ),
        (
            "python/ordered_json_v2.py",
            format!("{CODEC_ROOT}/ordered_json_v2.py"),
        ),
        ("wit/pack.wit", format!("{PACK_ROOT}/wit/pack.wit")),
        ("wit/deps/tobkiri-core/core.wit", CORE_WIT.to_owned()),
    ])
    .into_iter()
    .map(|(staged, relative)| (staged.to_owned(), relative))
    .collect::<BTreeMap<_, _>>();
    for name in ["__init__", "canonical", "errors", "ids"] {
        mappings.insert(
            format!("python/tobkiri_protocol/{name}.py"),
            format!("tobkiri_runtime/tobkiri_protocol/{name}.py"),
        );
    }
    let mut required = mappings.values().cloned().collect::<Vec<_>>();
    required.extend([
        SOURCE.to_owned(),
        PORT.to_owned(),
        format!("{PACK_ROOT}/python/provenance.json"),
        "scripts/wasm/prepare_harness_setup.py".into(),
        "scripts/wasm/replay_harness_setup.py".into(),
    ]);
    required.sort();
    ensure!(
        sources.keys().cloned().collect::<Vec<_>>() == required,
        "complete source closure required"
    );
    let plan_bytes = oracle["plan_json"]
        .as_str()
        .context("exact source plan missing")?
        .as_bytes();
    let plan = document(plan_bytes)?;
    let plan_sources = mappings
        .iter()
        .map(|(staged, relative)| {
            json!({"source":staged,"destination":staged.strip_prefix("python/").unwrap_or(staged),
            "sha256":Digest::hash(&sources[relative]).hex()})
        })
        .collect::<Vec<_>>();
    let manifest = mappings
        .iter()
        .map(|(staged, relative)| {
            json!({"source":staged,"destination":staged.strip_prefix("python/").unwrap_or(staged),
            "sha256":Digest::hash(&sources[relative]).hex(),"bytes":sources[relative].len()})
        })
        .collect::<Vec<_>>();
    let exports = json!([
        "tobkiri:core/lifecycle@1.0.0",
        "local:harness-setup-python/setup@1.0.0"
    ]);
    let imports = json!([
        "tobkiri:core/types@1.0.0",
        "tobkiri:core/flow@1.0.0",
        "tobkiri:core/clock@1.0.0",
        "tobkiri:core/random@1.0.0",
        "tobkiri:core/audit@1.0.0"
    ]);
    ensure!(
        plan == json!({"format":"tobkiri.python-build/3","stub_wasi":true,
        "wit":"wit/pack.wit","world":"pack","entrypoint":"app", "sources":plan_sources,
        "outer_exports":exports,"allowed_imports":imports}),
        "complete source plan differs"
    );
    ensure!(
        receipt["format"] == "tobkiri.component-build/2"
            && receipt["language"] == "python"
            && receipt["production_enabled"] == false
            && receipt["artifact_sha256"] == Digest::hash(&component).hex()
            && receipt["artifact_bytes"] == component.len()
            && receipt["build_plan_sha256"] == Digest::hash(plan_bytes).hex()
            && receipt["source_manifest"] == json!(manifest)
            && receipt["imports"]
                == json!([
                    "tobkiri:core/audit@1.0.0",
                    "tobkiri:core/clock@1.0.0",
                    "tobkiri:core/flow@1.0.0",
                    "tobkiri:core/random@1.0.0",
                    "tobkiri:core/types@1.0.0"
                ])
            && receipt["exports"]
                == json!([
                    "local:harness-setup-python/setup@1.0.0",
                    "tobkiri:core/lifecycle@1.0.0"
                ])
            && oracle["component_sha256"] == receipt["artifact_sha256"]
            && oracle["build_receipt_sha256"] == Digest::hash(&receipt_bytes).hex()
            && oracle["build_plan_sha256"] == receipt["build_plan_sha256"],
        "source/build receipt differs"
    );
    let cases = oracle["cases"]
        .as_array()
        .context("source cases missing")?
        .clone();
    ensure!(
        cases
            .iter()
            .map(|case| case["id"].as_str())
            .collect::<Vec<_>>()
            == IDS.map(Some).to_vec(),
        "complete original case order required"
    );
    let evidence = path("EVIDENCE")?;
    ensure!(
        !evidence.exists() && evidence.parent().is_some_and(Path::is_dir),
        "fresh evidence required"
    );
    fs::create_dir(&evidence)?;
    let identity = json!({"component_sha256":Digest::hash(&component).hex(),"component_bytes":component.len(),
        "build_receipt_sha256":Digest::hash(&receipt_bytes).hex(),"source_oracle_sha256":Digest::hash(&oracle_bytes).hex(),
        "source_bindings":oracle["source_bindings"],"build_plan_sha256":receipt["build_plan_sha256"]});
    Ok(Inputs {
        cases,
        component,
        identity,
        evidence,
    })
}

pub fn limits() -> PermissionSet {
    PermissionSet {
        wasm: ResourceLimits {
            fuel_per_call: 2_000_000_000,
            memory_mib: 128,
            wall_ms: 300_000,
            table_elements: 65_536,
            output_bytes: 4 * 1024 * 1024,
        },
        loops: LoopLimits {
            max_iterations: 16,
            max_wall_ms: 300_000,
            max_cost_units: 100_000,
        },
        ..Default::default()
    }
}

pub fn materialize(
    repo: &Path,
    root: &Path,
    component: Vec<u8>,
) -> Result<(PackPin, VerifiedPack)> {
    let shape = ShapeRef::parse(SHAPE)?;
    let files = BTreeMap::from([
        ("component.wasm".to_owned(), component),
        (
            "wit/pack.wit".to_owned(),
            read(&repo.join(PACK_ROOT).join("wit/pack.wit"), 64 * 1024)?,
        ),
        (
            "wit/deps/core.wit".to_owned(),
            read(&repo.join(CORE_WIT), 64 * 1024)?,
        ),
        (
            "wit/setup.shape.json".to_owned(),
            serde_json::to_vec(&json!({"cardinality":"one",
            "streams":{},"callbacks":{},"conformance":null}))?,
        ),
    ]);
    let manifest = PackManifest {
        format: "tobkiri.pack/1".into(),
        id: PACK.parse()?,
        version: "1.0.0".into(),
        publisher_key: None,
        runtime: RuntimeSpec::Wasm {
            core_modules: Default::default(),
            component: "component.wasm".into(),
            toolchain: "external-python/hash-pinned-test-fixture".into(),
            config: None,
        },
        shapes: vec![ShapeDefinition {
            name: shape.name.clone(),
            version: shape.version,
            kind: ShapeKind::Port,
            wit: "wit/pack.wit".into(),
        }],
        ports: Ports {
            provides: vec![PortDeclaration {
                shape: shape.name,
                version: shape.version,
                optional: false,
            }],
            ..Default::default()
        },
        inputs: vec![],
        flows: vec![],
        overlays: vec![],
        stores: vec![],
        permissions: limits(),
        ui: None,
        files: files
            .iter()
            .map(|(path, bytes)| FileEntry {
                path: path.clone(),
                sha256: Digest::hash(bytes),
                size: bytes.len() as u64,
            })
            .collect(),
    };
    let directory = root.join(PACK);
    fs::create_dir(&directory)?;
    for (name, bytes) in files {
        let path = directory.join(name);
        fs::create_dir_all(path.parent().context("member parent missing")?)?;
        fs::write(path, bytes)?;
    }
    fs::write(
        directory.join("pack.json"),
        tobkiri_manifest::canonical_manifest(&manifest)?,
    )?;
    let verified = tobkiri_manifest::verify_directory(&directory, &BTreeMap::new())?;
    let pin = PackPin {
        id: verified.manifest.id.clone(),
        version: verified.manifest.version.clone(),
        sha256: verified.hash.clone(),
        source: format!("file:{}", directory.canonicalize()?.display()),
    };
    Ok((pin, verified))
}
