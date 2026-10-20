//! Independently pinned V2 evidence. No Python source or guest is executed here.
use super::{
    consumer::BridgeAbi,
    evidence::{self, Inputs, bounded_read, digest, environment_hash, no_links, strict_json},
};
use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    path::{Component, Path, PathBuf},
};
use tobkiri_canon::ids::ShapeRef;
use tobkiri_manifest::VerifiedPack;

pub const PACK: &str = evidence::PACK;
pub const SHAPE: &str = "local.ai-tool-bridge-ordered-python.bridge@2.0";
pub const COMPONENT_SHA: &str = "30103a20b3694622a62e50cd57cb52af791cf0936657889811562db06ad3e092";
pub const RECEIPT_SHA: &str = "01f8d3b20b5bcd46d16d1fae1265b16155237fef3ce2163c0cfa47e32c23a14d";
const PLAN_SHA: &str = "4028c00dd67e63c49137440f338fe1995247f9fdbb267a8d0dab7d5cb47b347e";
const SOURCES_SHA: &str = "243e0040c4731b3c4cd898fca4112b3e6f861a7815656d793f354bde8772fc72";
pub const OBSERVATIONS_SHA: &str =
    "438dd64bc9ec1f849739556e2ab25336751022afa24a59fa19ff7e539c22daa8";
const FREEZE_SHA: &str = "be98b1b5cc6a20dda6b098c8d7e2f378497f80bcaae8ad96a6f18b18d8e40553";
const PREFIX: &str = "packs/local.ai-tool-bridge-ordered-python";
const STAGER: &str = "scripts/wasm/stage_ordered_tool_bridge_v2.py";

pub fn counts() -> Value {
    json!({"success":18, "original-invalid":12, "excluded-not-parity":3})
}

fn source_record(path: &str, bytes: &[u8]) -> Value {
    json!({"path":path, "bytes":bytes.len(), "sha256":digest(bytes)})
}

fn read_relative(repo: &Path, path: &str) -> Result<Vec<u8>> {
    ensure!(
        !path.is_empty()
            && Path::new(path)
                .components()
                .all(|part| matches!(part, Component::Normal(_))),
        "source binding is not a confined relative path"
    );
    bounded_read(repo, path, 16 * 1024 * 1024)
}

fn capture_bindings(
    repo: &Path,
    records: &Value,
    captured: &mut BTreeMap<String, Vec<u8>>,
) -> Result<()> {
    let mut seen = BTreeSet::new();
    for record in records.as_array().context("source bindings missing")? {
        let relative = record["path"].as_str().context("source path missing")?;
        ensure!(seen.insert(relative), "duplicate source binding");
        let bytes = read_relative(repo, relative)?;
        ensure!(
            *record == source_record(relative, &bytes),
            "reviewed source binding changed: {relative}"
        );
        if let Some(previous) = captured.insert(relative.to_owned(), bytes.clone()) {
            ensure!(previous == bytes, "source changed during evidence capture");
        }
    }
    Ok(())
}

/// Reproduce Python's ASCII-only canonical metadata encoding, without running
/// the producer or converting any opaque inner JSON number.
fn ascii_json(value: &Value) -> Result<Vec<u8>> {
    let raw = serde_json::to_string(value)?;
    let mut output = String::new();
    for character in raw.chars() {
        if character.is_ascii() {
            output.push(character);
        } else {
            use std::fmt::Write;
            for unit in character.encode_utf16(&mut [0; 2]) {
                write!(&mut output, "\\u{unit:04x}")?;
            }
        }
    }
    Ok(output.into_bytes())
}

pub(super) fn validate_cases(observations: &Value, original: &Value) -> Result<()> {
    let observed = observations["cases"]
        .as_array()
        .context("V2 cases missing")?;
    let frozen = original["cases"].as_array().context("V1 cases missing")?;
    ensure!(
        observed.len() == 33 && frozen.len() == 33,
        "wrong frozen case count"
    );
    let mut ids = BTreeSet::new();
    for (case, prior) in observed.iter().zip(frozen) {
        let id = prior["id"].as_str().context("frozen case id missing")?;
        ensure!(ids.insert(id), "duplicate frozen case id");
        let expected = if prior["transport"]["status"] == "invalid" {
            "original-invalid"
        } else if id.contains("inner-nonfinite") {
            "excluded-not-parity"
        } else {
            "success"
        };
        ensure!(
            ["id", "operation", "input_json", "original"]
                .iter()
                .all(|key| case[key] == prior[key])
                && case["v1_classification"] == prior["transport"]["status"]
                && case["classification"] == expected
                && case["admitted_source_parity"] == (expected != "excluded-not-parity"),
            "V2 case order, original input, classification, or parity claim differs"
        );
        if expected == "success" {
            let output = case["output_json"]
                .as_str()
                .context("success string missing")?;
            ensure!(
                case["output_sha256"] == digest(output.as_bytes()),
                "success output hash differs"
            );
            if prior["transport"]["status"] == "ok" {
                ensure!(
                    case["output_json"] == prior["transport"]["output_json"],
                    "V1 success changed"
                );
            }
        } else if expected == "excluded-not-parity" {
            ensure!(
                case["intended_core_error"] == "internal"
                    && case["actual_error"] == "provider output contains a non-finite number"
                    && case["source_provider_code"] == "provider_failed"
                    && case["source_retryable"] == false
                    && case["legacy_http_error_parity_claimed"] == false,
                "nonfinite boundary was relabeled as parity"
            );
        }
    }
    for (classification, count) in [
        ("success", 18),
        ("original-invalid", 12),
        ("excluded-not-parity", 3),
    ] {
        ensure!(
            observed
                .iter()
                .filter(|case| case["classification"] == classification)
                .count()
                == count,
            "V2 classification totals differ"
        );
    }
    Ok(())
}

/// Return the exact twelve staged sources and authenticated 33-case observations.
/// Bind all nineteen original-source records plus inventory/extraction sources.
pub fn reviewed_sources(repo: &Path) -> Result<(Value, Value)> {
    // Preserve, rather than replace or relax, the complete original V1 proof.
    evidence::reviewed_sources(repo)?;
    let observation_bytes = read_relative(repo, &format!("{PREFIX}/integer-v2.observations.json"))?;
    ensure!(
        digest(&observation_bytes) == OBSERVATIONS_SHA,
        "V2 observations changed"
    );
    let observations = strict_json(&observation_bytes)?;
    ensure!(
        observations["format"] == "tobkiri.ordered-integer-source-observations/1"
            && observations["protocol"] == "ordered-finite-json-v2"
            && observations["runtime"]
                == json!({
                    "implementation":"CPython", "python":"3.13.5", "unicode":"15.1.0",
                    "int_max_str_digits":4300
                })
            && observations["frozen_case_counts"] == counts()
            && observations["admitted_source_parity_count"] == 30
            && [
                "wasm_execution_performed",
                "native_core_acceptance",
                "full_host_replay_performed",
                "production_enabled"
            ]
            .iter()
            .all(|key| observations[key] == false),
        "wrong V2 source observation identity or proof scope"
    );
    let bindings = &observations["source_bindings"];
    ensure!(
        bindings
            .as_array()
            .context("observation bindings missing")?
            .len()
            == 19
            && observations["source_bindings_sha256"] == digest(&ascii_json(bindings)?),
        "V2 observation source bindings differ"
    );
    let mut captured = BTreeMap::new();
    capture_bindings(repo, bindings, &mut captured)?;
    for name in ["inventory.json", "provenance.json"] {
        let document = strict_json(&captured[&format!("{PREFIX}/python/{name}")])?;
        capture_bindings(repo, &document["sources"], &mut captured)?;
    }
    let oracle_bytes = &captured[&format!("{PREFIX}/oracle.json")];
    let freeze_bytes = &captured[&format!("{PREFIX}/oracle.freeze.json")];
    ensure!(
        digest(oracle_bytes) == evidence::ORACLE_SHA && digest(freeze_bytes) == FREEZE_SHA,
        "immutable V1 oracle or freeze changed"
    );
    let oracle = strict_json(oracle_bytes)?;
    let freeze = strict_json(freeze_bytes)?;
    let case_inputs: Value = oracle["cases"]
        .as_array()
        .context("V1 cases missing")?
        .iter()
        .map(|case| {
            json!({
                "id":case["id"], "operation":case["operation"], "input_json":case["input_json"]
            })
        })
        .collect();
    ensure!(
        observations["oracle_sha256"] == evidence::ORACLE_SHA
            && observations["oracle_freeze_sha256"] == FREEZE_SHA
            && observations["case_inputs_sha256"] == freeze["case_inputs_sha256"]
            && freeze["case_inputs_sha256"] == digest(&ascii_json(&case_inputs)?)
            && oracle["source_provenance_sha256"] == freeze["source_provenance_sha256"]
            && oracle["freeze_script_sha256"] == freeze["freeze_script_sha256"],
        "V2 observations do not bind complete frozen V1 inputs"
    );
    validate_cases(&observations, &oracle)?;
    let mut destinations = BTreeMap::new();
    for name in [
        "app_v2.py",
        "caller_adapter.py",
        "caller_adapter_v2.py",
        "inventory.json",
        "original_bridge.py",
        "provenance.json",
    ] {
        destinations.insert(name, format!("{PREFIX}/python/{name}"));
    }
    for name in ["ordered_json.py", "ordered_json_v2.py", "wit/pack-v2.wit"] {
        destinations.insert(name, format!("{PREFIX}/{name}"));
    }
    destinations.insert(
        "wit/deps/core.wit",
        "crates/tobkiri-wit/wit/core/core.wit".to_owned(),
    );
    let mut sources = BTreeMap::new();
    let mut staged_bindings = Vec::new();
    for (destination, relative) in destinations {
        let bytes = read_relative(repo, &relative)?;
        if let Some(prior) = captured.insert(relative.clone(), bytes.clone()) {
            ensure!(prior == bytes, "source changed during V2 source capture");
        }
        let mut binding = source_record(&relative, &bytes);
        binding["destination"] = json!(destination);
        staged_bindings.push(binding);
        sources.insert(destination, bytes);
    }
    let staging_bytes = read_relative(repo, STAGER)?;
    captured.insert(STAGER.to_owned(), staging_bytes.clone());
    let companion = json!({
        "format":"tobkiri.ordered-python-v2-staged-inputs/1", "protocol":"ordered-finite-json-v2",
        "source_observations_scope":"source-adapter-only",
        "source_observations_sha256":OBSERVATIONS_SHA,
        "source_observations_bindings_sha256":observations["source_bindings_sha256"],
        "frozen_case_counts":counts(), "admitted_source_parity_count":30,
        "original_frozen_inputs_sha256":FREEZE_SHA,
        "guest_source_scope":"source-stage-only-not-execution",
        "guest_source_bindings":staged_bindings, "staging_script":source_record(STAGER, &staging_bytes),
        "build_performed":false, "wasm_execution_performed":false, "native_core_acceptance":false,
        "full_host_replay_performed":false, "production_enabled":false
    });
    let mut companion_bytes = ascii_json(&companion)?;
    companion_bytes.push(b'\n');
    sources.insert("reviewed-inputs-v2.json", companion_bytes);
    sources.insert(
        "reviewed-inputs.json",
        captured[&format!("{PREFIX}/oracle.freeze.json")].clone(),
    );
    let provenance = strict_json(&sources["provenance.json"])?;
    ensure!(
        provenance["output"]["sha256"] == digest(&sources["original_bridge.py"])
            && provenance["output"]["bytes"] == sources["original_bridge.py"].len(),
        "extracted original bridge differs from provenance"
    );
    let manifest: Value = sources
        .iter()
        .map(|(name, bytes)| {
            json!({
                "source":name, "destination":name, "sha256":digest(bytes), "bytes":bytes.len()
            })
        })
        .collect();
    ensure!(
        sources.len() == 12 && digest(&serde_json::to_vec(&manifest)?) == SOURCES_SHA,
        "complete reviewed V2 staged source set changed"
    );
    for (relative, bytes) in captured {
        ensure!(
            read_relative(repo, &relative)? == bytes,
            "source changed during V2 review"
        );
    }
    ensure!(
        read_relative(repo, &format!("{PREFIX}/integer-v2.observations.json"))?
            == observation_bytes,
        "observations changed during V2 review"
    );
    Ok((manifest, observations))
}

fn validate_manifest(pack: &VerifiedPack) -> Result<()> {
    let manifest = serde_json::to_value(&pack.manifest)?;
    let shape = ShapeRef::parse(SHAPE)?;
    ensure!(
        pack.manifest.id.as_str() == PACK
            && pack.manifest.version == "2.0.0"
            && manifest["runtime"]["kind"] == "wasm"
            && manifest["runtime"]["component"] == "component.wasm"
            && manifest["runtime"]["config"]["path"] == "config.json"
            && pack
                .files
                .get("config.json")
                .is_some_and(|bytes| bytes == b"{}"),
        "not the reviewed version-two Python wasm Pack"
    );
    let no_authority = tobkiri_gate::PermissionSet {
        wasm: pack.manifest.permissions.wasm.clone(),
        loops: pack.manifest.permissions.loops.clone(),
        ..Default::default()
    };
    ensure!(
        pack.manifest.permissions == no_authority,
        "V2 requests non-computation authority"
    );
    ensure!(
        pack.manifest.ports.consumes.is_empty()
            && pack.manifest.ports.untyped.is_empty()
            && pack.manifest.inputs.is_empty()
            && pack.manifest.flows.is_empty()
            && pack.manifest.overlays.is_empty()
            && pack.manifest.stores.is_empty()
            && pack.manifest.ui.is_none(),
        "V2 candidate has unexpected execution surfaces"
    );
    ensure!(
        pack.manifest.shapes.len() == 1
            && pack.manifest.shapes[0].wit == BridgeAbi::V2.wit_member()
            && pack.manifest.shapes[0].kind == tobkiri_manifest::ShapeKind::Port
            && pack.shapes.len() == 1
            && pack.shapes[0].reference == shape
            && pack.manifest.ports.provides.len() == 1
            && pack.manifest.ports.provides[0].shape == shape.name
            && pack.manifest.ports.provides[0].version == shape.version
            && !pack.manifest.ports.provides[0].optional
            && pack.shapes[0]
                .functions
                .keys()
                .map(|op| op.as_str())
                .collect::<BTreeSet<_>>()
                == ["normalize", "runtime-info"].into_iter().collect(),
        "V2 exact shape, WIT, or public operation surface differs"
    );
    ensure!(
        pack.files
            .keys()
            .map(String::as_str)
            .collect::<BTreeSet<_>>()
            == [
                "component.wasm",
                "component.build.json",
                "config.json",
                "wit/bridge.shape.json",
                "wit/pack-v2.wit",
                "wit/deps/core.wit"
            ]
            .into_iter()
            .collect(),
        "V2 materialized member set differs"
    );
    Ok(())
}

fn validate_export_types(
    engine: &wasmtime::Engine,
    component: &wasmtime::component::Component,
) -> Result<()> {
    use wasmtime::component::types::{ComponentItem, Type};
    let types = component.component_type();
    let (_, core) = types
        .imports(engine)
        .find(|(name, _)| *name == "tobkiri:core/types@1.0.0")
        .context("actual Core types import missing")?;
    let ComponentItem::ComponentInstance(core) = core.ty else {
        anyhow::bail!("actual Core types import is not an interface");
    };
    let (_, error) = core
        .exports(engine)
        .find(|(name, _)| *name == "error")
        .context("actual Core error identity missing")?;
    let ComponentItem::Type(error) = error.ty else {
        anyhow::bail!("actual Core error is not a type");
    };
    for (interface, operations) in [
        (
            BridgeAbi::V2.interface(),
            vec!["error", "normalize", "runtime-info"],
        ),
        (
            "tobkiri:core/lifecycle@1.0.0",
            vec!["error", "init", "json", "shutdown"],
        ),
    ] {
        let (_, item) = types
            .exports(engine)
            .find(|(name, _)| *name == interface)
            .context("actual required exported interface missing")?;
        let ComponentItem::ComponentInstance(instance) = item.ty else {
            anyhow::bail!("actual export is not a typed interface");
        };
        let members: BTreeMap<_, _> = instance
            .exports(engine)
            .map(|(name, item)| (name.to_owned(), item.ty))
            .collect();
        ensure!(
            members.keys().map(String::as_str).collect::<Vec<_>>() == operations,
            "actual {interface} member set differs: {:?}",
            members.keys().collect::<Vec<_>>()
        );
        for (name, item) in members {
            if name == "error" || name == "json" {
                let ComponentItem::Type(actual) = item else {
                    anyhow::bail!("actual {interface}/{name} is not a named type");
                };
                ensure!(
                    actual
                        == if name == "error" {
                            error.clone()
                        } else {
                            Type::String
                        },
                    "actual {interface}/{name} named type identity differs"
                );
                continue;
            }
            let ComponentItem::ComponentFunc(function) = item else {
                anyhow::bail!("actual interface member is not a function");
            };
            let params: Vec<_> = function.params().collect();
            let results: Vec<_> = function.results().collect();
            match name.as_str() {
                "runtime-info" => ensure!(
                    params.is_empty() && results == [Type::String],
                    "actual runtime-info signature differs"
                ),
                "shutdown" => ensure!(
                    params.is_empty() && results.is_empty(),
                    "actual shutdown signature differs"
                ),
                "normalize" | "init" => {
                    let expected_params = if name == "normalize" {
                        vec![("operation", Type::String), ("payload", Type::String)]
                    } else {
                        vec![("config", Type::String)]
                    };
                    ensure!(params == expected_params, "actual {name} parameters differ");
                    let [Type::Result(result)] = results.as_slice() else {
                        anyhow::bail!("actual {name} does not return a typed result");
                    };
                    ensure!(
                        result.ok()
                            == if name == "normalize" {
                                Some(Type::String)
                            } else {
                                None
                            }
                            && result.err() == Some(error.clone()),
                        "actual {name} result or nominal Core error identity differs"
                    );
                }
                _ => anyhow::bail!("unexpected actual exported operation"),
            }
        }
    }
    Ok(())
}

pub fn load(repo: &Path) -> Result<Inputs> {
    let supplied = PathBuf::from(
        std::env::var_os("TOBKIRI_TEST_ORDERED_PYTHON_V2_PACK_DIR")
            .context("materialized V2 Pack path required")?,
    );
    no_links(&supplied)?;
    let source = supplied.canonicalize()?;
    let pack = tobkiri_manifest::verify_directory(&source, &Default::default())?;
    let pack_pin = environment_hash("TOBKIRI_TEST_ORDERED_PYTHON_V2_PACK_SHA256")?;
    let component_pin = environment_hash("TOBKIRI_TEST_ORDERED_PYTHON_V2_COMPONENT_SHA256")?;
    let receipt_pin = environment_hash("TOBKIRI_TEST_ORDERED_PYTHON_V2_RECEIPT_SHA256")?;
    ensure!(
        pack.hash.to_string() == format!("sha256:{pack_pin}")
            && component_pin == COMPONENT_SHA
            && receipt_pin == RECEIPT_SHA,
        "V2 independently supplied pins differ from reviewed identity"
    );
    validate_manifest(&pack)?;
    let binary = pack
        .files
        .get("component.wasm")
        .context("V2 component missing")?;
    let receipt_bytes = pack
        .files
        .get("component.build.json")
        .context("V2 receipt missing")?;
    ensure!(
        digest(binary) == component_pin
            && binary.len() == 17_910_172
            && digest(receipt_bytes) == receipt_pin,
        "V2 component or receipt bytes differ"
    );
    let (sources, oracle) = reviewed_sources(repo)?;
    let receipt = strict_json(receipt_bytes)?;
    let imports: BTreeSet<String> = ["types", "flow", "clock", "random", "audit"]
        .into_iter()
        .map(|name| format!("tobkiri:core/{name}@1.0.0"))
        .collect();
    let exports: BTreeSet<String> = [
        BridgeAbi::V2.interface().to_owned(),
        "tobkiri:core/lifecycle@1.0.0".to_owned(),
    ]
    .into_iter()
    .collect();
    ensure!(
        receipt["format"] == "tobkiri.component-build/2"
            && receipt["language"] == "python"
            && receipt["world"] == "pack"
            && receipt["artifact_sha256"] == component_pin
            && receipt["artifact_bytes"] == binary.len()
            && receipt["build_plan_sha256"] == PLAN_SHA
            && receipt["source_manifest_sha256"] == SOURCES_SHA
            && receipt["source_manifest"] == sources
            && receipt["imports"] == json!(imports)
            && receipt["exports"] == json!(exports),
        "V2 receipt differs from complete reviewed source plan or ABI"
    );
    for member in [BridgeAbi::V2.wit_member(), "wit/deps/core.wit"] {
        let bytes = pack.files.get(member).context("V2 verified WIT missing")?;
        let record = sources
            .as_array()
            .context("source manifest missing")?
            .iter()
            .find(|record| record["destination"] == member)
            .context("reviewed WIT binding missing")?;
        ensure!(
            record["sha256"] == digest(bytes) && record["bytes"] == bytes.len(),
            "materialized WIT changed"
        );
    }
    // Static component type inspection only. No instantiation or trap imports.
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
        "actual V2 component ABI differs"
    );
    validate_export_types(engine.engine(), &component)?;
    let identity = json!({
        "pack_sha256":pack_pin, "component_sha256":component_pin, "build_receipt_sha256":receipt_pin,
        "observations_sha256":OBSERVATIONS_SHA, "original_oracle_sha256":evidence::ORACLE_SHA,
        "original_frozen_inputs_sha256":FREEZE_SHA, "source_manifest_sha256":SOURCES_SHA,
        "build_plan_sha256":PLAN_SHA, "actual_imports":imports, "actual_exports":exports
    });
    Ok(Inputs {
        source,
        pack,
        oracle,
        identity,
    })
}

pub fn receipt_destination(repo: &Path) -> Result<PathBuf> {
    let path = PathBuf::from(
        std::env::var_os("TOBKIRI_TEST_ORDERED_PYTHON_V2_EVIDENCE")
            .context("explicit external V2 evidence output path required")?,
    );
    ensure!(
        path.is_absolute() && !path.exists(),
        "V2 evidence must be a fresh absolute path"
    );
    no_links(&path)?;
    let parent = path
        .parent()
        .context("V2 evidence parent missing")?
        .canonicalize()?;
    ensure!(
        !parent.starts_with(repo.canonicalize()?),
        "V2 evidence must be outside the checkout"
    );
    Ok(parent.join(path.file_name().context("V2 evidence name missing")?))
}

#[test]
fn ordered_python_v2_reviewed_source_identity_is_current() -> Result<()> {
    reviewed_sources(&super::repo())?;
    Ok(())
}

#[tokio::test]
#[ignore = "requires the independently pinned materialized V2 Pack; static inspection only"]
async fn ordered_python_v2_pinned_pack_has_reviewed_static_abi() -> Result<()> {
    let inputs = load(&super::repo())?;
    println!(
        "Verified V2 Pack {}: static bytes/type inspection only; native_core_acceptance=false",
        inputs.pack.hash
    );
    Ok(())
}

#[test]
fn v2_rejects_reordered_missing_or_reclassified_frozen_cases() -> Result<()> {
    let (_, observations) = reviewed_sources(&super::repo())?;
    let original = strict_json(&read_relative(
        &super::repo(),
        &format!("{PREFIX}/oracle.json"),
    )?)?;
    for mutation in ["order", "missing", "input", "classification", "parity"] {
        let mut changed = observations.clone();
        match mutation {
            "order" => changed["cases"].as_array_mut().unwrap().swap(0, 1),
            "missing" => {
                changed["cases"].as_array_mut().unwrap().pop();
            }
            "input" => changed["cases"][0]["input_json"] = json!("{\"different\":true}"),
            "classification" => changed["cases"][9]["classification"] = json!("success"),
            "parity" => changed["cases"][9]["admitted_source_parity"] = json!(true),
            _ => unreachable!(),
        }
        assert!(
            validate_cases(&changed, &original).is_err(),
            "accepted {mutation}"
        );
    }
    Ok(())
}

#[test]
fn v2_source_bindings_reject_duplicates_path_escape_and_changed_bytes() -> Result<()> {
    let root = tempfile::tempdir()?;
    std::fs::write(root.path().join("source"), b"original")?;
    let record = source_record("source", b"original");
    for records in [
        json!([record, record]),
        json!([source_record("../source", b"original")]),
        json!([source_record("/source", b"original")]),
        json!([source_record("source", b"changed")]),
    ] {
        assert!(capture_bindings(root.path(), &records, &mut BTreeMap::new()).is_err());
    }
    Ok(())
}
