//! Current two-Pack source/byte/type evidence, never a replay or authority grant.
//! Historical 92f38e3 caller replay and the current five-component replay remain
//! separate evidence. Neither establishes this additional native Core route.
use super::{evidence, evidence_v2};
use anyhow::{Context, Result, ensure};
use evidence::{bounded_read, digest, environment_hash, no_links, strict_json};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    path::{Path, PathBuf},
};
use tobkiri_canon::ids::ShapeRef;
use tobkiri_gate::PermissionSet;
use tobkiri_manifest::{VerifiedPack, WitBindingDirection};

pub const CALLER: &str = "local.ai-gateway-tool-caller-python";
pub const CALLER_SHAPE: &str = "local.ai-gateway-tool-caller-python.caller@1.0";
pub const CALLER_INTERFACE: &str = "local:ai-gateway-tool-caller-python/caller@1.0.0";
pub const BRIDGE: &str = "local.ai-tool-bridge-ordered-python";
pub const BRIDGE_SHAPE: &str = "local.ai-tool-bridge-ordered-python.bridge@2.0";
pub const BRIDGE_INTERFACE: &str = "local:ai-tool-bridge-ordered-python/bridge@2.0.0";
pub const ORACLE_PATH: &str = "tests/fixtures/issue1520/gateway-tool-caller.current-source.json";
pub const ORACLE_SHA: &str = "854fa03de651c12cb7126d8f81e4f0c62e340a5a85f71eb50784e539e207bced";
pub const SOURCE_BYTES: usize = 35_798_851;
pub const CATALOG_LIMIT: usize = 64 * 1024 * 1024;
pub const OPERATIONS: [&str; 3] = ["generate-intents", "stream-intents", "stream-event-intents"];
const PREFIX: &str = "packs/local.ai-gateway-tool-caller-python";
const ARCHIVE: &str = "tools/python313-producer/trials/2026-10-20-finalization/builds";
const CALLER_WIT_SHA: &str = "34cad3a098d41f9103386b0dab1988624c0de7f81721807b4da1e5c0f64bdd41";
const SHAPE_SHA: &str = "689a23a2835b690b16496198786fa450596631440cce7041545a7d07c768ae05";
const WORLD_SHA: &str = "36a12119a23bd931929b084533a88ca1fe8e6dc61575e531e0c678462156c5ab";
const BRIDGE_WIT_SHA: &str = "513454d839ac468205bc5b4ed93032625ed4f5d3064aa17f643f907e80bfeb78";
const BUNDLE_SHA: &str = "687f47f6c592ee1bc195d481d02efd0c051f664729e965cc0271850735ffaa04";

pub struct Graph {
    pub root: PathBuf,
    pub bridge: VerifiedPack,
    pub caller: VerifiedPack,
    pub oracle: Value,
    pub identity: Value,
}

#[derive(Clone, Copy)]
struct Pin {
    name: &'static str,
    archive: &'static str,
    id: &'static str,
    version: &'static str,
    shape: &'static str,
    interface: &'static str,
    artifact: &'static str,
    pack: &'static str,
    bytes: usize,
    receipt: &'static str,
    plan: &'static str,
    sources: &'static str,
}
const PINS: [Pin; 2] = [
    Pin {
        name: "bridge",
        archive: "bridge-v2",
        id: BRIDGE,
        version: "2.0.0",
        shape: BRIDGE_SHAPE,
        interface: BRIDGE_INTERFACE,
        artifact: "30103a20b3694622a62e50cd57cb52af791cf0936657889811562db06ad3e092",
        pack: "7ff540650f7677c8530ef6f5cdba8d1e2fe436a57f99133118e9d377c467cb31",
        bytes: 17_910_172,
        receipt: "01f8d3b20b5bcd46d16d1fae1265b16155237fef3ce2163c0cfa47e32c23a14d",
        plan: "4028c00dd67e63c49137440f338fe1995247f9fdbb267a8d0dab7d5cb47b347e",
        sources: "243e0040c4731b3c4cd898fca4112b3e6f861a7815656d793f354bde8772fc72",
    },
    Pin {
        name: "caller",
        archive: "caller",
        id: CALLER,
        version: "1.0.0",
        shape: CALLER_SHAPE,
        interface: CALLER_INTERFACE,
        artifact: "cb561cb9b9b85aaf1de9cb74a57ffece061bd4b8100d6030b5aabc79b2cb7fc1",
        pack: "a8c507beb99d21e6f776121c899c9407b3aa0f7673c16376c4d1e1867ba69114",
        bytes: 17_888_679,
        receipt: "18d900c56e25e2a5ab9d746a00f06066ebbae32cba8f0a681e6009826224f477",
        plan: "bc03deb9d3b214f083258785f2cac52a399aaf3381b1913dea221a21fd4e69ec",
        sources: "47e0ab2a526e81f192e6fdcde6ac083d28a8f561fd54585091d06ededb657761",
    },
];

pub fn counts() -> Value {
    json!({"success":16,"provider-invalid":7,"provider-nonfinite":7})
}

fn record(path: &str, bytes: &[u8]) -> Value {
    json!({"path":path,"bytes":bytes.len(),"sha256":digest(bytes)})
}

pub fn wit_closure() -> Value {
    json!({"caller_source_destination":"wit/pack.wit","caller_source_sha256":CALLER_WIT_SHA,
        "caller_source_bytes":731,"bridge_source_destination":"wit/deps/bridge-v2.wit",
        "bridge_source_sha256":BRIDGE_WIT_SHA,"bridge_source_bytes":364,
        "bridge_package_semicolon_offset":49,"member":"wit/pack.wit",
        "member_sha256":BUNDLE_SHA,"member_bytes":1099})
}

/// Historical failed partition retained only for a parser regression test.
fn split_wit(original: &[u8]) -> Result<(Vec<u8>, Vec<u8>)> {
    ensure!(
        original.len() == 731 && digest(original) == CALLER_WIT_SHA,
        "caller WIT source changed"
    );
    let shape = original[..597].to_vec();
    let world = [&original[..51], &original[597..]].concat();
    ensure!(
        digest(&shape) == SHAPE_SHA && digest(&world) == WORLD_SHA,
        "caller WIT partition changed"
    );
    ensure!(
        [shape.as_slice(), &world[51..]].concat() == original,
        "WIT partition loses original bytes"
    );
    Ok((shape, world))
}

/// Fixed reversible packaging, not an interface/world or ABI rewrite.
fn bundled_wit(caller: &[u8], bridge: &[u8]) -> Result<Vec<u8>> {
    ensure!(
        caller.len() == 731
            && digest(caller) == CALLER_WIT_SHA
            && bridge.len() == 364
            && digest(bridge) == BRIDGE_WIT_SHA
            && bridge[49] == b';',
        "canonical WIT inputs changed"
    );
    let combined = [caller, b"\n", &bridge[..49], b" {", &bridge[50..], b"}\n"].concat();
    ensure!(
        combined.len() == 1099 && digest(&combined) == BUNDLE_SHA,
        "packaged WIT changed"
    );
    ensure!(
        &combined[..731] == caller
            && [&combined[732..781], b";", &combined[783..1097]].concat() == bridge,
        "packaging loses original WIT bytes"
    );
    Ok(combined)
}

pub fn outer_budget(oracle: &Value) -> Result<Value> {
    let mut requests = 0;
    let mut responses = 0;
    for case in oracle["cases"].as_array().context("cases missing")? {
        let input = case["input_json"]
            .as_str()
            .context("opaque input missing")?;
        requests = requests.max(
            tobkiri_canon::encode(
                &super::canon(&json!({"payload":input}))?,
                tobkiri_canon::Variant::StrictV1,
            )?
            .len(),
        );
        if case["classification"] == "success" {
            let output = case["output_json"]
                .as_str()
                .context("opaque output missing")?;
            responses = responses.max(
                tobkiri_canon::encode(
                    &super::canon(&json!(output))?,
                    tobkiri_canon::Variant::StrictV1,
                )?
                .len(),
            );
        }
    }
    ensure!(
        requests <= tobkiri_canon::MAX_BYTES && responses <= tobkiri_canon::MAX_BYTES,
        "outer DATA envelope exceeds Core bound"
    );
    Ok(
        json!({"core_strict_v1_max_bytes":4194304,"max_request_bytes":requests,"max_success_response_bytes":responses}),
    )
}

pub fn validate_oracle(bytes: &[u8]) -> Result<Value> {
    ensure!(
        bytes.len() == 94513 && digest(bytes) == ORACLE_SHA,
        "current source-only oracle changed"
    );
    let oracle = strict_json(bytes)?;
    ensure!(
        oracle["format"] == "tobkiri.gateway-tool-caller-source-observations/1"
            && oracle["protocol"] == "ordered-finite-json-v2"
            && oracle["counts"] == counts()
            && oracle["runtime"]
                == json!({"implementation":"CPython","python":"3.13.5","unicode":"15.1.0","int_max_str_digits":4300}),
        "wrong source oracle identity"
    );
    for flag in [
        "build_performed",
        "wasm_execution_performed",
        "native_core_acceptance",
        "core_runtime_acceptance",
        "production_enabled",
        "all_typed_error_arms_executed",
        "retry_parity_claimed",
    ] {
        ensure!(
            oracle[flag] == false,
            "source observations claim execution: {flag}"
        );
    }
    let cases = oracle["cases"].as_array().context("cases missing")?;
    ensure!(cases.len() == 30, "incomplete source corpus");
    let mut ids = BTreeSet::new();
    for case in cases {
        ensure!(
            ids.insert(case["id"].as_str().context("case id missing")?)
                && OPERATIONS.contains(&case["operation"].as_str().context("operation missing")?),
            "invalid case identity"
        );
        ensure!(
            case["input_json"].is_string()
                && case["source_call_counts_are_not_component_observations"] == true,
            "source call scope differs"
        );
        let calls = case["source_reference_calls"]
            .as_array()
            .context("source calls missing")?;
        ensure!(
            case["source_reference_call_count"] == calls.len(),
            "source call count differs"
        );
        for call in calls {
            ensure!(
                call["contract"] == "tobkiri.service.ai.tool_intent.normalize.v1"
                    && call["operation"].is_string()
                    && call["payload_json"].is_string(),
                "source dependency identity differs"
            );
        }
    }
    outer_budget(&oracle)?;
    Ok(oracle)
}

/// Capture all current reference source identities and both complete staged manifests.
/// This executes no Python, producer, guest, or expected-answer implementation.
pub fn reviewed_sources(repo: &Path) -> Result<(Value, BTreeMap<String, Value>)> {
    let raw = bounded_read(repo, ORACLE_PATH, 1024 * 1024)?;
    let oracle = validate_oracle(&raw)?;
    let bindings = oracle["source_bindings"]
        .as_array()
        .context("source bindings missing")?;
    ensure!(bindings.len() == 37, "incomplete current source closure");
    let mut captured = BTreeMap::new();
    for binding in bindings {
        let path = binding["path"].as_str().context("source path missing")?;
        let bytes = bounded_read(repo, path, 16 * 1024 * 1024)?;
        ensure!(
            *binding == record(path, &bytes) && captured.insert(path.to_owned(), bytes).is_none(),
            "changed or duplicated source: {path}"
        );
    }
    let (bridge_sources, _) = evidence_v2::reviewed_sources(repo)?;
    let mut caller_sources = BTreeMap::new();
    for name in [
        "app.py",
        "gateway_tool_caller_adapter.py",
        "inventory.json",
        "original_gateway_tool_caller.py",
        "provenance.json",
    ] {
        caller_sources.insert(
            name.to_owned(),
            captured[&format!("{PREFIX}/python/{name}")].clone(),
        );
    }
    for name in ["ordered_json.py", "ordered_json_v2.py"] {
        caller_sources.insert(
            name.to_owned(),
            captured[&format!("packs/{BRIDGE}/{name}")].clone(),
        );
    }
    for (destination, source) in [
        ("wit/pack.wit", format!("{PREFIX}/wit/pack.wit")),
        (
            "wit/deps/core.wit",
            "crates/tobkiri-wit/wit/core/core.wit".into(),
        ),
        (
            "wit/deps/bridge-v2.wit",
            format!("packs/{BRIDGE}/wit/pack-v2.wit"),
        ),
    ] {
        caller_sources.insert(destination.to_owned(), captured[&source].clone());
    }
    bundled_wit(
        &caller_sources["wit/pack.wit"],
        &caller_sources["wit/deps/bridge-v2.wit"],
    )?;
    let caller_manifest: Value = caller_sources.iter().map(|(name, bytes)| json!({"source":name,"destination":name,"sha256":digest(bytes),"bytes":bytes.len()})).collect();
    ensure!(
        caller_sources.len() == 10
            && digest(&serde_json::to_vec(&caller_manifest)?) == PINS[1].sources,
        "caller staged source manifest changed"
    );
    let manifests = BTreeMap::from([
        ("bridge".into(), bridge_sources),
        ("caller".into(), caller_manifest),
    ]);
    for pin in PINS {
        let receipt = bounded_read(
            repo,
            &format!("{ARCHIVE}/{}/component.build.json", pin.archive),
            1024 * 1024,
        )?;
        let plan = bounded_read(
            repo,
            &format!("{ARCHIVE}/{}/plan.json", pin.archive),
            1024 * 1024,
        )?;
        ensure!(
            digest(&receipt) == pin.receipt && digest(&plan) == pin.plan,
            "current archive identity changed"
        );
        validate_receipt(pin, &receipt, &manifests[pin.name])?;
    }
    for (path, bytes) in captured {
        ensure!(
            bounded_read(repo, &path, 16 * 1024 * 1024)? == bytes,
            "source changed while captured"
        );
    }
    ensure!(
        bounded_read(repo, ORACLE_PATH, 1024 * 1024)? == raw,
        "oracle changed while captured"
    );
    Ok((oracle, manifests))
}

fn validate_receipt(pin: Pin, bytes: &[u8], sources: &Value) -> Result<()> {
    ensure!(digest(bytes) == pin.receipt, "wrong build receipt");
    let r = strict_json(bytes)?;
    let mut imports: BTreeSet<String> = ["types", "flow", "clock", "random", "audit"]
        .iter()
        .map(|n| format!("tobkiri:core/{n}@1.0.0"))
        .collect();
    if pin.name == "caller" {
        imports.insert(BRIDGE_INTERFACE.into());
    }
    ensure!(
        r["format"] == "tobkiri.component-build/2"
            && r["language"] == "python"
            && r["world"] == "pack"
            && r["artifact_sha256"] == pin.artifact
            && r["artifact_bytes"] == pin.bytes
            && r["build_plan_sha256"] == pin.plan
            && r["source_manifest_sha256"] == pin.sources
            && r["source_manifest"] == *sources
            && r["imports"] == json!(imports)
            && r["exports"] == json!([pin.interface, "tobkiri:core/lifecycle@1.0.0"])
            && r["production_enabled"] == false
            && r["guest_runtime_identity_verified"] == false,
        "build receipt/source/ABI identity differs"
    );
    Ok(())
}

fn expected_record(pin: Pin, pack: &VerifiedPack) -> Value {
    json!({"directory":pin.name,"pack_id":pin.id,"pack_version":pin.version,"shape":pin.shape,
        "pack_sha256":pack.hash.to_string().trim_start_matches("sha256:"),"artifact_sha256":pin.artifact,
        "artifact_bytes":pin.bytes,"receipt_sha256":pin.receipt,"build_plan_sha256":pin.plan,"source_manifest_sha256":pin.sources})
}

fn validate_manifest(pin: Pin, pack: &VerifiedPack) -> Result<()> {
    let m = &pack.manifest;
    let caller = pin.name == "caller";
    let shape = ShapeRef::parse(pin.shape)?;
    let allowed = PermissionSet {
        ports: if caller {
            [BRIDGE_SHAPE.to_owned()].into_iter().collect()
        } else {
            BTreeSet::new()
        },
        wasm: m.permissions.wasm.clone(),
        loops: m.permissions.loops.clone(),
        ..Default::default()
    };
    ensure!(
        m.id.as_str() == pin.id
            && m.version == pin.version
            && m.publisher_key.is_none()
            && m.permissions == allowed
            && m.permissions.wasm.fuel_per_call == 2_000_000_000
            && m.permissions.wasm.memory_mib == 128
            && m.permissions.wasm.output_bytes == 4194304
            && m.permissions.wasm.table_elements == 65536
            && m.permissions.wasm.wall_ms == 300000
            && m.permissions.loops.max_cost_units == 100000
            && m.permissions.loops.max_iterations == 16
            && m.permissions.loops.max_wall_ms == 300000,
        "Pack identity/computation/port permissions differ"
    );
    let runtime = serde_json::to_value(&m.runtime)?;
    ensure!(
        runtime
            == json!({"kind":"wasm","component":"component.wasm","toolchain":"python/build-receipt-pinned","config":{"path":"config.json","shape":null}})
            && pack.files.get("config.json").is_some_and(|v| v == b"{}"),
        "runtime/config differs"
    );
    ensure!(
        m.inputs.is_empty()
            && m.flows.is_empty()
            && m.overlays.is_empty()
            && m.stores.is_empty()
            && m.ui.is_none()
            && m.ports.untyped.is_empty(),
        "unexpected executable surfaces"
    );
    ensure!(
        m.shapes.len() == 1
            && m.shapes[0].name == shape.name
            && m.shapes[0].version == shape.version
            && m.shapes[0].kind == tobkiri_manifest::ShapeKind::Port
            && m.shapes[0].wit
                == if caller {
                    "wit/pack.wit"
                } else {
                    "wit/pack-v2.wit"
                },
        "authoritative shape differs"
    );
    ensure!(
        m.ports.provides.len() == 1
            && m.ports.provides[0].shape == shape.name
            && m.ports.provides[0].version == shape.version
            && !m.ports.provides[0].optional,
        "provided port differs"
    );
    let consumes = serde_json::to_value(&m.ports.consumes)?;
    ensure!(
        consumes
            == if caller {
                json!([{"shape":"local.ai-tool-bridge-ordered-python.bridge","version":"2.0","optional":false}])
            } else {
                json!([])
            },
        "consumed port differs"
    );
    let expected: BTreeSet<_> = if caller {
        [
            "component.wasm",
            "component.build.json",
            "config.json",
            "wit/caller.shape.json",
            "wit/pack.wit",
            "wit/deps/core.wit",
        ]
        .into_iter()
        .collect()
    } else {
        [
            "component.wasm",
            "component.build.json",
            "config.json",
            "wit/bridge.shape.json",
            "wit/pack-v2.wit",
            "wit/deps/core.wit",
        ]
        .into_iter()
        .collect()
    };
    ensure!(
        pack.files
            .keys()
            .map(String::as_str)
            .collect::<BTreeSet<_>>()
            == expected,
        "Pack member closure differs"
    );
    let sidecar = strict_json(
        &pack.files[if caller {
            "wit/caller.shape.json"
        } else {
            "wit/bridge.shape.json"
        }],
    )?;
    ensure!(
        sidecar == json!({"cardinality":"one","streams":{},"callbacks":{},"conformance":null}),
        "shape sidecar differs"
    );
    ensure!(
        pack.shapes.len() == 1 && pack.shapes[0].reference == shape,
        "loaded shape differs"
    );
    Ok(())
}

/// Inspect actual nominal types without instantiation or trapping host functions.
fn validate_types(
    engine: &wasmtime::Engine,
    component: &wasmtime::component::Component,
    pin: Pin,
) -> Result<()> {
    use wasmtime::component::types::{ComponentItem, Type};
    let ty = component.component_type();
    let (_, core) = ty
        .imports(engine)
        .find(|(name, _)| *name == "tobkiri:core/types@1.0.0")
        .context("Core types missing")?;
    let ComponentItem::ComponentInstance(core) = core.ty else {
        anyhow::bail!("Core is not interface")
    };
    let (_, error) = core
        .exports(engine)
        .find(|(name, _)| *name == "error")
        .context("Core error missing")?;
    let ComponentItem::Type(error) = error.ty else {
        anyhow::bail!("Core error is not type")
    };
    let mut interfaces = vec![
        (false, pin.interface),
        (false, "tobkiri:core/lifecycle@1.0.0"),
    ];
    if pin.name == "caller" {
        interfaces.push((true, BRIDGE_INTERFACE));
    }
    for (imported, name) in interfaces {
        let item = if imported {
            ty.imports(engine)
                .find(|(n, _)| *n == name)
                .map(|(_, i)| i.ty)
        } else {
            ty.exports(engine)
                .find(|(n, _)| *n == name)
                .map(|(_, i)| i.ty)
        }
        .context("required interface missing")?;
        let ComponentItem::ComponentInstance(instance) = item else {
            anyhow::bail!("not interface")
        };
        let expected: BTreeSet<_> = if name == CALLER_INTERFACE {
            [
                "error",
                "runtime-info",
                "generate-intents",
                "stream-intents",
                "stream-event-intents",
            ]
            .into_iter()
            .collect()
        } else if name == BRIDGE_INTERFACE {
            ["error", "runtime-info", "normalize"].into_iter().collect()
        } else {
            ["error", "json", "init", "shutdown"].into_iter().collect()
        };
        ensure!(
            instance
                .exports(engine)
                .map(|(n, _)| n)
                .collect::<BTreeSet<_>>()
                == expected,
            "typed members differ"
        );
        for (op, item) in instance.exports(engine) {
            if op == "error" || op == "json" {
                let ComponentItem::Type(actual) = item.ty else {
                    anyhow::bail!("named type missing")
                };
                ensure!(
                    actual
                        == if op == "error" {
                            error.clone()
                        } else {
                            Type::String
                        },
                    "nominal type differs"
                );
                continue;
            }
            let ComponentItem::ComponentFunc(f) = item.ty else {
                anyhow::bail!("operation not function")
            };
            let params: Vec<_> = f.params().collect();
            let results: Vec<_> = f.results().collect();
            let expected_params = match op {
                "runtime-info" | "shutdown" => vec![],
                "normalize" => vec![("operation", Type::String), ("payload", Type::String)],
                "init" => vec![("config", Type::String)],
                _ => vec![("payload", Type::String)],
            };
            ensure!(params == expected_params, "typed parameters differ");
            match op {
                "runtime-info" => ensure!(results == [Type::String], "runtime-info result differs"),
                "shutdown" => ensure!(results.is_empty(), "shutdown result differs"),
                _ => {
                    let [Type::Result(r)] = results.as_slice() else {
                        anyhow::bail!("result missing")
                    };
                    ensure!(
                        r.ok()
                            == if op == "init" {
                                None
                            } else {
                                Some(Type::String)
                            }
                            && r.err() == Some(error.clone()),
                        "result or exact Core error differs"
                    );
                }
            }
        }
    }
    Ok(())
}

pub async fn load(repo: &Path) -> Result<Graph> {
    let supplied = PathBuf::from(
        std::env::var_os("TOBKIRI_TEST_GATEWAY_TOOL_CALLER_GRAPH_DIR")
            .context("graph directory required")?,
    );
    ensure!(supplied.is_absolute(), "graph path must be absolute");
    no_links(&supplied)?;
    let root = supplied.canonicalize()?;
    let index_bytes = bounded_read(&root, "index.json", 65536)?;
    let index_sha = environment_hash("TOBKIRI_TEST_GATEWAY_TOOL_CALLER_INDEX_SHA256")?;
    ensure!(digest(&index_bytes) == index_sha, "index pin differs");
    let index = strict_json(&index_bytes)?;
    let (oracle, sources) = reviewed_sources(repo)?;
    ensure!(
        bounded_read(&root, "source-observations.json", 1024 * 1024)?
            == bounded_read(repo, ORACLE_PATH, 1024 * 1024)?,
        "materialized source observations differ"
    );
    let mut packs = BTreeMap::new();
    for pin in PINS {
        let source = root.join(pin.name);
        no_links(&source)?;
        // Reject a changed manifest before its declared artifact sizes are read.
        let manifest_bytes = bounded_read(&source, "pack.json", 65536)?;
        let manifest_value =
            tobkiri_canon::parse(&manifest_bytes, tobkiri_canon::Variant::StrictV1)?;
        ensure!(
            tobkiri_canon::digest(&manifest_value, tobkiri_canon::Variant::StrictV1)?.to_string()
                == format!("sha256:{}", pin.pack),
            "reviewed Pack manifest pin changed"
        );
        let pack = tobkiri_manifest::verify_directory(&source, &Default::default())?;
        ensure!(
            pack.hash.to_string() == format!("sha256:{}", pin.pack),
            "verified Pack identity changed"
        );
        ensure!(
            bounded_read(&source, "pack.json", 65536)? == manifest_bytes,
            "manifest changed during verification"
        );
        validate_manifest(pin, &pack)?;
        ensure!(
            index["components"][pin.name] == expected_record(pin, &pack),
            "independent Pack/index pin differs"
        );
        let binary = &pack.files["component.wasm"];
        ensure!(
            binary.len() == pin.bytes
                && binary.len() <= 32 * 1024 * 1024
                && digest(binary) == pin.artifact,
            "component bytes differ"
        );
        validate_receipt(pin, &pack.files["component.build.json"], &sources[pin.name])?;
        let original = bounded_read(repo, &format!("{PREFIX}/wit/pack.wit"), 1024 * 1024)?;
        let bridge_wit = bounded_read(
            repo,
            &format!("packs/{BRIDGE}/wit/pack-v2.wit"),
            1024 * 1024,
        )?;
        if pin.name == "caller" {
            ensure!(
                pack.files["wit/pack.wit"] == bundled_wit(&original, &bridge_wit)?,
                "packaged original WIT differs"
            );
        } else {
            ensure!(
                pack.files["wit/pack-v2.wit"] == bridge_wit,
                "canonical bridge WIT differs"
            );
        }
        ensure!(
            pack.files["wit/deps/core.wit"]
                == bounded_read(repo, "crates/tobkiri-wit/wit/core/core.wit", 1024 * 1024)?,
            "canonical Core WIT differs"
        );
        packs.insert(pin.name, pack);
    }
    let views = packs
        .values()
        .flat_map(|p| p.shapes.iter().cloned())
        .map(|s| (s.reference.clone(), s))
        .collect();
    for pin in PINS {
        let pack = &packs[pin.name];
        let inspection = tobkiri_manifest::inspect_pack_wit(pack, &views)?;
        for declaration in &pack.manifest.ports.provides {
            let view = ShapeRef::new(declaration.shape.clone(), declaration.version);
            for op in pack.shapes[0]
                .functions
                .keys()
                .filter(|op| op.as_str() != "runtime-info")
            {
                ensure!(
                    inspection.core_errors().has_core_error_result(
                        WitBindingDirection::Export,
                        &view,
                        op
                    ),
                    "export lacks nominal Core error proof"
                );
            }
        }
        if pin.name == "caller" {
            ensure!(
                inspection.core_errors().has_core_error_result(
                    WitBindingDirection::Import,
                    &ShapeRef::parse(BRIDGE_SHAPE)?,
                    &"normalize".parse()?
                ),
                "caller import lacks nominal error proof"
            );
        }
    }
    let expected = json!({"format":"tobkiri.gateway-tool-caller-native-inputs/1","protocol":"ordered-finite-json-v2","scope":"two-separate-python-packs-not-finalizer",
        "components":{"bridge":expected_record(PINS[0],&packs["bridge"]),"caller":expected_record(PINS[1],&packs["caller"])},
        "source_observations":{"path":"source-observations.json","sha256":ORACLE_SHA,"bytes":94513},
        "component_source_bytes":SOURCE_BYTES,"core_component_catalog_limit_bytes":CATALOG_LIMIT,
        "caller_wit_closure":wit_closure(),"outer_budget":outer_budget(&oracle)?,
        "build_performed":false,"wasm_execution_performed":false,"native_core_acceptance":false,"production_enabled":false});
    ensure!(
        index == expected && SOURCE_BYTES <= CATALOG_LIMIT,
        "graph index scope/closure differs"
    );
    ensure!(
        bounded_read(&root, "index.json", 65536)? == index_bytes,
        "index changed during review"
    );
    // Complete production source/manifest inspection before the much more
    // expensive actual-component compilation. Neither phase instantiates.
    let engine = tobkiri_wasm::WasmEngine::new()?;
    for pin in PINS {
        let pack = &packs[pin.name];
        let component =
            wasmtime::component::Component::new(engine.engine(), &pack.files["component.wasm"])?;
        let allowed = tobkiri_manifest::allowed_imports_for_pack(pack)?;
        let imports: BTreeSet<_> =
            tobkiri_wasm::validate_imports(engine.engine(), &component, &allowed)?
                .into_iter()
                .collect();
        let receipt = strict_json(&pack.files["component.build.json"])?;
        ensure!(
            json!(imports) == receipt["imports"],
            "actual imports differ"
        );
        let exports: BTreeSet<_> = component
            .component_type()
            .exports(engine.engine())
            .map(|(n, _)| n.to_owned())
            .collect();
        ensure!(
            json!(exports) == receipt["exports"],
            "actual exports differ"
        );
        validate_types(engine.engine(), &component, pin)?;
    }
    Ok(Graph {
        root,
        bridge: packs.remove("bridge").unwrap(),
        caller: packs.remove("caller").unwrap(),
        oracle,
        identity: json!({"index_sha256":index_sha,"inputs":index,"static_type_inspection_only":true}),
    })
}

pub fn receipt_destination(repo: &Path) -> Result<PathBuf> {
    let path = PathBuf::from(
        std::env::var_os("TOBKIRI_TEST_GATEWAY_TOOL_CALLER_EVIDENCE")
            .context("fresh evidence path required")?,
    );
    ensure!(
        path.is_absolute() && !path.exists(),
        "evidence must be fresh and absolute"
    );
    no_links(&path)?;
    let parent = path
        .parent()
        .context("evidence parent missing")?
        .canonicalize()?;
    ensure!(
        !parent.starts_with(repo.canonicalize()?),
        "evidence must be outside checkout"
    );
    Ok(parent.join(path.file_name().context("evidence filename missing")?))
}

#[test]
fn current_caller_graph_source_proof_is_not_native_replay() -> Result<()> {
    let (oracle, manifest) = reviewed_sources(&super::repo())?;
    assert_eq!(oracle["counts"], counts());
    assert_eq!(manifest["caller"].as_array().unwrap().len(), 10);
    assert_eq!(manifest["bridge"].as_array().unwrap().len(), 12);
    Ok(())
}
#[test]
fn historical_split_is_byte_lossless_but_not_parser_acceptance() -> Result<()> {
    let original = bounded_read(&super::repo(), &format!("{PREFIX}/wit/pack.wit"), 1024)?;
    let (shape, world) = split_wit(&original)?;
    assert_eq!((shape.len(), world.len()), (597, 185));
    let mut changed = original.clone();
    changed[10] ^= 1;
    assert!(split_wit(&changed).is_err());
    assert!(split_wit(&original[..730]).is_err());
    Ok(())
}
#[test]
fn current_caller_corpus_rejects_changed_and_historical_evidence() -> Result<()> {
    let raw = bounded_read(&super::repo(), ORACLE_PATH, 1024 * 1024)?;
    let mut value = validate_oracle(&raw)?;
    value["native_core_acceptance"] = json!(true);
    assert!(validate_oracle(&serde_json::to_vec(&value)?).is_err());
    let old = bounded_read(
        &super::repo(),
        "tests/fixtures/issue1520/gateway-tool-caller.source.json",
        1024 * 1024,
    )?;
    assert!(validate_oracle(&old).is_err());
    Ok(())
}
#[test]
fn two_actual_components_fit_unchanged_catalog_without_finalizer() {
    assert_eq!(PINS.iter().map(|p| p.bytes).sum::<usize>(), SOURCE_BYTES);
    const { assert!(SOURCE_BYTES < CATALOG_LIMIT) };
    const { assert!(89_513_931 > CATALOG_LIMIT) };
}

/// A DATA-only parser witness, never one of the runtime Pack identities.
fn wit_parser_witness(pin: Pin, mut files: BTreeMap<String, Vec<u8>>) -> Result<VerifiedPack> {
    let reference = ShapeRef::parse(pin.shape)?;
    files.insert(
        format!("wit/{}.shape.json", reference.name.interface()),
        br#"{"cardinality":"one","streams":{},"callbacks":{},"conformance":null}"#.to_vec(),
    );
    let mut permissions = PermissionSet::default();
    if pin.name == "caller" {
        permissions.ports.insert(BRIDGE_SHAPE.to_owned());
    }
    let manifest: tobkiri_manifest::PackManifest = serde_json::from_value(json!({
        "format":"tobkiri.pack/1","id":pin.id,"version":pin.version,
        "publisher_key":null,"runtime":{"kind":"data"},
        "shapes":[{"name":reference.name,"version":reference.version,"kind":"port","wit":"wit/pack.wit"}],
        "ports":{"provides":[{"shape":reference.name,"version":reference.version,"optional":false}],
            "consumes":if pin.name == "caller" {
                json!([{"shape":"local.ai-tool-bridge-ordered-python.bridge","version":"2.0","optional":false}])
            } else { json!([]) },"untyped":[]},
        "inputs":[],"flows":[],"overlays":[],"stores":[],"ui":null,
        "permissions":permissions,
        "files":files.iter().map(|(path,bytes)| json!({"path":path,"size":bytes.len(),"sha256":format!("sha256:{}",digest(bytes))})).collect::<Vec<_>>()
    }))?;
    let archive = tobkiri_manifest::build_tpk(&manifest, &files, None)?;
    let pack = tobkiri_manifest::verify_tpk(&archive, &BTreeMap::new())?;
    // Also call the production shape-loader entry explicitly, independently of
    // the package verifier's current internal implementation.
    let shapes = tobkiri_manifest::load_shapes(&pack.manifest, &pack.files)?;
    ensure!(
        shapes.len() == 1 && shapes[0].reference == reference,
        "parser witness shape identity differs"
    );
    Ok(pack)
}

#[test]
fn bundled_original_world_passes_production_parser_without_guest_or_component() -> Result<()> {
    let root = super::repo();
    let caller = bounded_read(&root, &format!("{PREFIX}/wit/pack.wit"), 1024)?;
    let bridge = bounded_read(&root, &format!("packs/{BRIDGE}/wit/pack-v2.wit"), 1024)?;
    let bundle = bundled_wit(&caller, &bridge)?;
    let mut changed_caller = caller.clone();
    changed_caller[10] ^= 1;
    assert!(bundled_wit(&changed_caller, &bridge).is_err());
    let mut changed_bridge = bridge.clone();
    changed_bridge[49] = b'{';
    assert!(bundled_wit(&caller, &changed_bridge).is_err());
    assert!(bundled_wit(&caller, &bridge[..363]).is_err());
    let caller_pack =
        wit_parser_witness(PINS[1], BTreeMap::from([("wit/pack.wit".into(), bundle)]))?;
    let bridge_pack = wit_parser_witness(
        PINS[0],
        BTreeMap::from([("wit/pack.wit".into(), bridge.clone())]),
    )?;
    let views = [&caller_pack, &bridge_pack]
        .into_iter()
        .flat_map(|p| p.shapes.iter().cloned())
        .map(|s| (s.reference.clone(), s))
        .collect();
    let inspection = tobkiri_manifest::inspect_pack_wit(&caller_pack, &views)?;
    assert!(inspection.allowed_imports().contains(BRIDGE_INTERFACE));
    assert!(!inspection.allowed_imports().contains(CALLER_INTERFACE));
    assert_eq!(caller_pack.shapes[0].functions.len(), 4);
    assert_eq!(
        caller_pack.shapes[0]
            .functions
            .keys()
            .map(|op| op.as_str())
            .collect::<BTreeSet<_>>(),
        BTreeSet::from([
            "runtime-info",
            "generate-intents",
            "stream-intents",
            "stream-event-intents"
        ])
    );
    for operation in OPERATIONS {
        assert!(inspection.core_errors().has_core_error_result(
            WitBindingDirection::Export,
            &ShapeRef::parse(CALLER_SHAPE)?,
            &tobkiri_canon::ids::OpName::parse(operation)?,
        ));
    }
    assert!(inspection.core_errors().has_core_error_result(
        WitBindingDirection::Import,
        &ShapeRef::parse(BRIDGE_SHAPE)?,
        &tobkiri_canon::ids::OpName::parse("normalize")?,
    ));
    assert!(
        wit_parser_witness(
            PINS[1],
            BTreeMap::from([("wit/pack.wit".into(), caller.clone())]),
        )
        .is_err(),
        "missing nested dependency must not resolve"
    );
    let duplicate = wit_parser_witness(
        PINS[1],
        BTreeMap::from([
            ("wit/pack.wit".into(), bundled_wit(&caller, &bridge)?),
            ("wit/deps/bridge-v2.wit".into(), bridge.clone()),
        ]),
    )?;
    assert!(
        tobkiri_manifest::inspect_pack_wit(&duplicate, &views).is_err(),
        "a duplicate nested dependency must not be silently merged"
    );
    // The failed split remains an explicit regression, not silently reblessed.
    let (shape, world) = split_wit(&caller)?;
    let split = wit_parser_witness(
        PINS[1],
        BTreeMap::from([
            ("wit/pack.wit".into(), shape),
            ("wit/world.wit".into(), world),
            ("wit/deps/bridge-v2.wit".into(), bridge),
        ]),
    )?;
    assert!(
        tobkiri_manifest::inspect_pack_wit(&split, &views)
            .unwrap_err()
            .to_string()
            .contains("interface or world `caller` does not exist")
    );
    Ok(())
}

#[tokio::test]
#[ignore = "requires independently pinned two-Pack input directory; static compilation/type inspection, no guest"]
async fn current_caller_graph_has_exact_static_abi() -> Result<()> {
    let graph = load(&super::repo()).await?;
    super::gateway_tool_caller::validate_profiles(&graph)?;
    println!(
        "{}",
        serde_json::to_string(
            &json!({"proof":"actual-two-Pack-bytes-WIT-types-and-profile-compilation-only","identity":graph.identity,"native_core_acceptance":false,"wasm_execution_performed":false,"core_session_started":false})
        )?
    );
    Ok(())
}
