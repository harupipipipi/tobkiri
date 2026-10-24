//! Private fixture/evidence helpers; no producer, Python or expected-answer code runs.
use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};
use sha2::{Digest as _, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs::{self, OpenOptions},
    io::Write as _,
    path::{Component, Path, PathBuf},
};
use tobkiri_canon::{Digest, Variant, ids::ShapeRef};
use tobkiri_gate::{LoopLimits, PermissionSet, ResourceLimits};
use tobkiri_manifest::{
    FileEntry, PackManifest, PortDeclaration, Ports, RuntimeSpec, ShapeDefinition, ShapeKind,
    VerifiedPack,
};
use tobkiri_profile::PackPin;

pub const PREFIX: &str = "TOBKIRI_TEST_RUST_PROVIDERS";
pub const MAX_DATA: usize = 4 * 1024 * 1024;
const CORE_WIT: &str = "crates/tobkiri-wit/wit/core/core.wit";

pub struct Role {
    pub label: &'static str,
    pub id: &'static str,
    pub runtime_id: &'static str,
    pub shape: &'static str,
    pub function: &'static str,
    wit: &'static str,
    pub dependencies: &'static [usize],
    pub rust: bool,
}

pub const ROLES: [Role; 5] = [
    Role {
        label: "USAGE",
        id: "local.ai-usage-cost-python",
        runtime_id: "local.ai-usage-cost-rust",
        shape: "local.ai-usage-cost-python.usage-cost@1.0",
        function: "calculate",
        wit: "packs/local.ai-usage-cost-python/wit/pack.wit",
        dependencies: &[],
        rust: true,
    },
    Role {
        label: "STREAM",
        id: "local.ai-stream-normalizer-python",
        runtime_id: "local.ai-stream-normalizer-rust",
        shape: "local.ai-stream-normalizer-python.stream-normalizer@1.0",
        function: "normalize",
        wit: "packs/local.ai-stream-normalizer-python/wit/pack.wit",
        dependencies: &[],
        rust: true,
    },
    Role {
        label: "BRIDGE",
        id: "local.ai-tool-bridge-ordered-python",
        runtime_id: "local.ai-tool-bridge-ordered-python",
        shape: "local.ai-tool-bridge-ordered-python.bridge@2.0",
        function: "normalize",
        wit: "packs/local.ai-tool-bridge-ordered-python/wit/pack-v2.wit",
        dependencies: &[],
        rust: false,
    },
    Role {
        label: "CALLER",
        id: "local.ai-gateway-tool-caller-python",
        runtime_id: "local.ai-gateway-tool-caller-python",
        shape: "local.ai-gateway-tool-caller-python.caller@1.0",
        function: "generate-intents",
        wit: "packs/local.ai-gateway-tool-caller-python/wit/pack.wit",
        dependencies: &[2],
        rust: false,
    },
    Role {
        label: "FINALIZER",
        id: "local.ai-gateway-finalizer-python",
        runtime_id: "local.ai-gateway-finalizer-python",
        shape: "local.ai-gateway-finalizer-python.finalizer@1.0",
        function: "generate",
        wit: "packs/local.ai-gateway-finalizer-python/wit/pack.wit",
        dependencies: &[3, 0, 1],
        rust: false,
    },
];

pub struct Pack {
    pub role: usize,
    pub rust: bool,
    pub runtime_id: &'static str,
    pub pin: PackPin,
    pub verified: VerifiedPack,
    pub identity: Value,
}

pub struct Oracle {
    pub document: Value,
    pub directory: PathBuf,
    pub identity: Value,
}

pub fn digest(bytes: &[u8]) -> String {
    hex::encode(Sha256::digest(bytes))
}

pub fn text<'a>(value: &'a Value, key: &str) -> Result<&'a str> {
    value[key]
        .as_str()
        .with_context(|| format!("missing evidence field {key}"))
}

fn no_links(path: &Path) -> Result<()> {
    for ancestor in path.ancestors() {
        ensure!(
            !ancestor.is_symlink(),
            "fixture/evidence path contains a symlink"
        );
    }
    Ok(())
}

pub fn read(root: &Path, relative: &str, maximum: usize) -> Result<Vec<u8>> {
    no_links(root)?;
    Ok(tobkiri_canon::files::read_relative(
        root,
        relative,
        maximum as u64,
    )?)
}

fn read_absolute(path: &Path, maximum: usize) -> Result<Vec<u8>> {
    ensure!(
        path.is_absolute(),
        "explicit absolute fixture path required"
    );
    no_links(path)?;
    read(
        path.parent().context("file parent missing")?,
        path.file_name()
            .and_then(|v| v.to_str())
            .context("UTF-8 fixture filename required")?,
        maximum,
    )
}

fn environment(name: &str) -> Result<PathBuf> {
    Ok(PathBuf::from(std::env::var_os(name).with_context(
        || format!("required {name} is absent; no skipped-green fixture"),
    )?))
}

fn pin(name: &str, bytes: &[u8]) -> Result<String> {
    let expected = std::env::var(name).with_context(|| format!("explicit {name} required"))?;
    ensure!(
        expected.len() == 64
            && expected
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)),
        "invalid explicit SHA-256 pin"
    );
    ensure!(
        digest(bytes) == expected,
        "fixture bytes differ from explicit {name}"
    );
    Ok(expected)
}

pub fn checked(root: &Path, record: &Value) -> Result<Vec<u8>> {
    let path = text(record, "path")?;
    let bytes = read(root, path, 8 * 1024 * 1024)?;
    ensure!(
        record["bytes"].as_u64() == Some(bytes.len() as u64)
            && text(record, "sha256")? == digest(&bytes),
        "bound source/sidecar changed: {path}"
    );
    Ok(bytes)
}

fn source_manifest(repo: &Path, records: &Value) -> Result<String> {
    let mut rows = BTreeMap::new();
    for record in records.as_array().context("source bindings missing")? {
        checked(repo, record)?;
        let path = text(record, "path")?;
        ensure!(
            path.is_ascii(),
            "source manifest expects canonical ASCII paths"
        );
        let row = format!(
            "{{\"bytes\":{},\"path\":{},\"sha256\":{}}}",
            record["bytes"]
                .as_u64()
                .context("source byte count missing")?,
            serde_json::to_string(path)?,
            serde_json::to_string(text(record, "sha256")?)?
        );
        ensure!(rows.insert(path, row).is_none(), "duplicate source binding");
    }
    Ok(digest(
        format!(
            "[{}]",
            rows.values()
                .map(String::as_str)
                .collect::<Vec<_>>()
                .join(",")
        )
        .as_bytes(),
    ))
}

pub fn oracle(repo: &Path, composition: bool) -> Result<Oracle> {
    let name = if composition {
        "FINALIZER_ORACLE"
    } else {
        "ORACLE"
    };
    let path = environment(&format!("{PREFIX}_{name}"))?;
    let bytes = read_absolute(&path, 4 * 1024 * 1024)?;
    let hash = pin(&format!("{PREFIX}_{name}_SHA256"), &bytes)?;
    tobkiri_canon::parse(&bytes, Variant::StrictV1)?;
    let document: Value = serde_json::from_slice(&bytes)?;
    let (source_key, digest_key) = if composition {
        ("source_bindings", "source_bindings_sha256")
    } else {
        ("sources", "source_manifest_sha256")
    };
    ensure!(
        text(&document, digest_key)? == source_manifest(repo, &document[source_key])?,
        "source aggregate changed"
    );
    let cases = document["cases"]
        .as_array()
        .context("oracle cases missing")?;
    if composition {
        ensure!(
            document["format"] == "tobkiri.gateway-finalizer-source-observations/1"
                && document["protocol"] == "ordered-finite-json-v2"
                && cases.len() == 10,
            "original ten-case finalizer receipt required"
        );
        let catalog = read(
            repo,
            "packs/local.ai-gateway-finalizer-python/source-cases.json",
            1024 * 1024,
        )?;
        ensure!(
            digest(&catalog) == "dbacb05a9e238b425c0910ca239f09aba3dc8c1ac63a32b2c2a6da734f2d9706",
            "original ten-case catalog changed"
        );
        let catalog: Value = serde_json::from_slice(&catalog)?;
        for (case, original) in cases.iter().zip(
            catalog["cases"]
                .as_array()
                .context("catalog cases missing")?,
        ) {
            ensure!(
                case["id"] == original["id"]
                    && case["operation"] == original["operation"]
                    && case["expected_kind"] == original["expected_kind"]
                    && case["expected_dependency_call_count"]
                        == original["expected_dependency_call_count"],
                "finalizer case order/identity changed"
            );
        }
    } else {
        ensure!(
            document["schema"] == "io.tobkiri.python-provider-compat.v1" && cases.len() == 94,
            "original 94-case provider receipt required"
        );
        ensure!(
            document["runtime"]["implementation"] == "cpython"
                && document["runtime"]["python"]["major"] == 3
                && document["runtime"]["python"]["minor"] == 13
                && document["runtime"]["unicode_version"] == "15.1.0"
                && document["runtime"]["int_max_str_digits"] == 4300,
            "original CPython 3.13/Unicode15.1/4300 receipt required"
        );
        for (provider, count) in [("usage-cost", 41), ("stream-normalizer", 53)] {
            ensure!(
                cases
                    .iter()
                    .filter(|case| case["provider"] == provider)
                    .count()
                    == count,
                "provider corpus count changed"
            );
        }
        let mut ids = BTreeSet::new();
        for case in cases {
            ensure!(
                case["source_manifest_sha256"] == document["source_manifest_sha256"],
                "case source binding changed"
            );
            ensure!(
                ids.insert((text(case, "provider")?, text(case, "id")?)),
                "duplicate provider case"
            );
            let operation = text(case, "operation")?;
            ensure!(
                case["operation_utf8"]["bytes"].as_u64() == Some(operation.len() as u64)
                    && text(&case["operation_utf8"], "sha256")? == digest(operation.as_bytes()),
                "operation bytes changed"
            );
        }
    }
    Ok(Oracle {
        directory: path
            .parent()
            .context("oracle directory missing")?
            .to_path_buf(),
        identity: json!({"path":path,"sha256":hash,"bytes":bytes.len(),"source_manifest_sha256":document[digest_key],"runtime":document["runtime"]}),
        document,
    })
}

/// Optional additional original bridge corpus for the five-Pack replay. A
/// partial configuration is an error, never a silently omitted corpus.
pub fn bridge_oracle(repo: &Path) -> Result<Option<Oracle>> {
    let name = format!("{PREFIX}_BRIDGE_ORACLE");
    let hash_name = format!("{name}_SHA256");
    if std::env::var_os(&name).is_none() && std::env::var_os(&hash_name).is_none() {
        return Ok(None);
    }
    let path = environment(&name)?;
    let bytes = read_absolute(&path, 4 * 1024 * 1024)?;
    let hash = pin(&hash_name, &bytes)?;
    tobkiri_canon::parse(&bytes, Variant::StrictV1)?;
    let document: Value = serde_json::from_slice(&bytes)?;
    ensure!(
        document["schema"] == "io.tobkiri.python-provider-compat.v1"
            && document["protocol"] == "ordered-finite-json-v2"
            && document["provider"] == "tool-bridge"
            && document["runtime"]["implementation"] == "cpython"
            && document["runtime"]["python"]["major"] == 3
            && document["runtime"]["python"]["minor"] == 13
            && document["runtime"]["unicode_version"] == "15.1.0"
            && document["runtime"]["int_max_str_digits"] == 4300,
        "original CPython 3.13/Unicode15.1/4300 bridge v2 receipt required"
    );
    let manifest = text(&document, "source_manifest_sha256")?;
    ensure!(
        manifest == source_manifest(repo, &document["sources"])?,
        "bridge source aggregate changed"
    );
    let sources = document["sources"]
        .as_array()
        .context("bridge sources missing")?;
    for required in [
        CORE_WIT,
        "packs/local.ai-tool-bridge-ordered-python/python/original_bridge.py",
        "packs/local.ai-tool-bridge-ordered-python/python/caller_adapter.py",
        "packs/local.ai-tool-bridge-ordered-python/python/caller_adapter_v2.py",
        "packs/local.ai-tool-bridge-ordered-python/python/app_v2.py",
        "packs/local.ai-tool-bridge-ordered-python/python/provenance.json",
        "packs/local.ai-tool-bridge-ordered-python/ordered_json.py",
        "packs/local.ai-tool-bridge-ordered-python/ordered_json_v2.py",
        "packs/local.ai-tool-bridge-ordered-python/wit/pack-v2.wit",
        "tobkiri_runtime/ecosystem/rumi_ai_tool_bridge_pack/runtime/bridge.py",
    ] {
        ensure!(
            sources.iter().any(|source| source["path"] == required),
            "missing original bridge source {required}"
        );
    }
    let cases = document["cases"]
        .as_array()
        .context("bridge cases missing")?;
    ensure!(
        document["limits"]["static_case_count"] == 81 && cases.len() == 81,
        "complete original 81-case bridge receipt required"
    );
    let mut ids = BTreeSet::new();
    for case in cases {
        ensure!(
            case["provider"] == "tool-bridge"
                && case["source_manifest_sha256"] == manifest
                && ids.insert(text(case, "id")?),
            "bridge case identity or source binding changed"
        );
        let operation = text(case, "operation")?;
        ensure!(
            case["operation_utf8"]["bytes"].as_u64() == Some(operation.len() as u64)
                && text(&case["operation_utf8"], "sha256")? == digest(operation.as_bytes()),
            "bridge operation bytes changed"
        );
    }
    Ok(Some(Oracle {
        directory: path
            .parent()
            .context("bridge oracle directory missing")?
            .to_path_buf(),
        identity: json!({"path":path,"sha256":hash,"bytes":bytes.len(),"source_manifest_sha256":manifest,"runtime":document["runtime"]}),
        document,
    }))
}

/// The unchanged thirty-case source observation used by the independent
/// Python caller Core replay, supplied explicitly for this composition run.
pub fn caller_oracle(repo: &Path) -> Result<Option<Oracle>> {
    const ORIGINAL_SHA: &str = "854fa03de651c12cb7126d8f81e4f0c62e340a5a85f71eb50784e539e207bced";
    let name = format!("{PREFIX}_CALLER_ORACLE");
    let hash_name = format!("{name}_SHA256");
    if std::env::var_os(&name).is_none() && std::env::var_os(&hash_name).is_none() {
        return Ok(None);
    }
    let path = environment(&name)?;
    let bytes = read_absolute(&path, 1024 * 1024)?;
    let hash = pin(&hash_name, &bytes)?;
    ensure!(
        bytes.len() == 94_513 && hash == ORIGINAL_SHA,
        "unchanged original thirty-case caller source observation required"
    );
    tobkiri_canon::parse(&bytes, Variant::StrictV1)?;
    let document: Value = serde_json::from_slice(&bytes)?;
    ensure!(
        document["format"] == "tobkiri.gateway-tool-caller-source-observations/1"
            && document["protocol"] == "ordered-finite-json-v2"
            && document["counts"]
                == json!({"success":16,"provider-invalid":7,"provider-nonfinite":7})
            && document["runtime"]
                == json!({"implementation":"CPython","python":"3.13.5","unicode":"15.1.0","int_max_str_digits":4300}),
        "original caller source identity differs"
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
            document[flag] == false,
            "source observation claims execution: {flag}"
        );
    }
    ensure!(
        document["source_bindings"]
            .as_array()
            .is_some_and(|sources| sources.len() == 37)
            && text(&document, "source_bindings_sha256")?
                == source_manifest(repo, &document["source_bindings"])?,
        "original caller source closure differs"
    );
    let cases = document["cases"]
        .as_array()
        .context("caller cases missing")?;
    ensure!(
        cases.len() == 30,
        "complete thirty-case caller corpus required"
    );
    let mut ids = BTreeSet::new();
    for case in cases {
        ensure!(
            ids.insert(text(case, "id")?)
                && ["generate-intents", "stream-intents", "stream-event-intents"]
                    .contains(&text(case, "operation")?)
                && case["input_json"].is_string()
                && case["source_call_counts_are_not_component_observations"] == true,
            "caller case identity differs"
        );
        let calls = case["source_reference_calls"]
            .as_array()
            .context("caller source calls missing")?;
        ensure!(
            case["source_reference_call_count"].as_u64() == Some(calls.len() as u64),
            "caller source call count differs"
        );
        for call in calls {
            ensure!(
                call["contract"] == "tobkiri.service.ai.tool_intent.normalize.v1"
                    && call["operation"].is_string()
                    && call["payload_json"].is_string(),
                "caller source dependency identity differs"
            );
        }
    }
    Ok(Some(Oracle {
        directory: path
            .parent()
            .context("caller oracle directory missing")?
            .to_path_buf(),
        identity: json!({"path":path,"sha256":hash,"bytes":bytes.len(),"source_manifest_sha256":document["source_bindings_sha256"],"runtime":document["runtime"],"source_counts_are_not_runtime_evidence":true}),
        document,
    }))
}

fn wit_closure(repo: &Path, role: usize) -> Result<Vec<u8>> {
    let mut result = read(repo, ROLES[role].wit, 64 * 1024)?;
    let mut pending = ROLES[role].dependencies.to_vec();
    let mut seen = BTreeSet::new();
    while let Some(dependency) = pending.pop() {
        if !seen.insert(dependency) {
            continue;
        }
        pending.extend(ROLES[dependency].dependencies);
        let source = read(repo, ROLES[dependency].wit, 64 * 1024)?;
        let delimiter = source
            .iter()
            .position(|b| *b == b';')
            .context("WIT package delimiter missing")?;
        ensure!(
            source.starts_with(b"package "),
            "canonical WIT package declaration missing"
        );
        // Same reversible nested-package closure as the existing caller fixture.
        // Every interface/world byte is preserved; only the package delimiter
        // becomes a block delimiter for one authoritative shape document.
        result.extend_from_slice(b"\n");
        result.extend_from_slice(&source[..delimiter]);
        result.extend_from_slice(b" {");
        result.extend_from_slice(&source[delimiter + 1..]);
        result.extend_from_slice(b"}\n");
    }
    Ok(result)
}

pub fn limits() -> PermissionSet {
    // Existing materialize_ordered_tool_bridge_v2.py fixture limits, unchanged.
    // These per-guest limits do not enlarge Core's 64 MiB catalog owner.
    PermissionSet {
        wasm: ResourceLimits {
            fuel_per_call: 2_000_000_000,
            memory_mib: 128,
            wall_ms: 300_000,
            table_elements: 65_536,
            output_bytes: MAX_DATA as u64,
        },
        loops: LoopLimits {
            max_iterations: 16,
            max_wall_ms: 300_000,
            max_cost_units: 100_000,
        },
        ..Default::default()
    }
}

pub fn materialize(repo: &Path, root: &Path, role: usize) -> Result<Pack> {
    let spec = &ROLES[role];
    let name = format!("{PREFIX}_{}_IMPLEMENTATION", spec.label);
    let implementation = match std::env::var(&name) {
        Ok(value) => value,
        Err(std::env::VarError::NotPresent) if role != 4 => {
            if spec.rust { "rust" } else { "python" }.into()
        }
        Err(error) => {
            return Err(error).with_context(|| format!("explicit {name}=rust|python required"));
        }
    };
    let rust = match implementation.as_str() {
        "rust" => true,
        "python" => false,
        _ => anyhow::bail!("{name} must be exactly rust or python"),
    };
    let runtime_id = match (role, rust) {
        (2, true) => "local.ai-tool-bridge-rust",
        (3, true) => "local.ai-gateway-tool-caller-rust",
        (4, true) => "local.ai-gateway-finalizer-rust",
        (_, false) => spec.id,
        _ => spec.runtime_id,
    };
    let component_path = environment(&format!("{PREFIX}_{}_COMPONENT", spec.label))?;
    let component = read_absolute(&component_path, 64 * 1024 * 1024)?;
    let hash = pin(&format!("{PREFIX}_{}_SHA256", spec.label), &component)?;
    ensure!(
        component.starts_with(b"\0asm\x0d\0\x01\0"),
        "actual WebAssembly component required"
    );
    let component_bytes = component.len();
    let reference = ShapeRef::parse(spec.shape)?;
    let files = BTreeMap::from([
        ("component.wasm".to_owned(), component),
        ("wit/pack.wit".to_owned(), wit_closure(repo, role)?),
        (
            "wit/deps/core.wit".to_owned(),
            read(repo, CORE_WIT, 64 * 1024)?,
        ),
        (
            format!("wit/{}.shape.json", reference.name.interface()),
            serde_json::to_vec(
                &json!({"cardinality":"one","streams":{},"callbacks":{},"conformance":null}),
            )?,
        ),
    ]);
    let mut permissions = limits();
    permissions.ports = spec
        .dependencies
        .iter()
        .map(|index| ROLES[*index].shape.to_owned())
        .collect();
    let manifest = PackManifest {
        format: "tobkiri.pack/1".into(),
        id: spec.id.parse()?,
        version: if role == 2 { "2.0.0" } else { "1.0.0" }.into(),
        publisher_key: None,
        runtime: RuntimeSpec::Wasm {
            core_modules: Default::default(),
            component: "component.wasm".into(),
            toolchain: if rust {
                "rust-wasip2/hash-pinned-test-fixture"
            } else {
                "external-python/hash-pinned-test-fixture"
            }
            .into(),
            config: None,
        },
        shapes: vec![ShapeDefinition {
            name: reference.name.clone(),
            version: reference.version,
            kind: ShapeKind::Port,
            wit: "wit/pack.wit".into(),
        }],
        ports: Ports {
            provides: vec![PortDeclaration {
                shape: reference.name,
                version: reference.version,
                optional: false,
            }],
            consumes: spec
                .dependencies
                .iter()
                .map(|index| {
                    let shape = ShapeRef::parse(ROLES[*index].shape)?;
                    Ok(PortDeclaration {
                        shape: shape.name,
                        version: shape.version,
                        optional: false,
                    })
                })
                .collect::<Result<_>>()?,
            ..Default::default()
        },
        inputs: vec![],
        flows: vec![],
        overlays: vec![],
        stores: vec![],
        permissions,
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
    let directory = root.join(spec.id);
    fs::create_dir(&directory)?;
    for (path, bytes) in &files {
        let destination = directory.join(path);
        fs::create_dir_all(
            destination
                .parent()
                .context("fixture member parent missing")?,
        )?;
        fs::write(destination, bytes)?;
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
    let source_directory = format!("packs/{runtime_id}");
    let mut source_files = Vec::new();
    if rust {
        for path in [
            format!("{source_directory}/Cargo.toml"),
            format!("{source_directory}/src/lib.rs"),
            "packs/ordered-python-compat/src/lib.rs".into(),
            "packs/ordered-python-compat/src/codec.rs".into(),
            "packs/ordered-python-compat/src/coercion.rs".into(),
            "packs/ordered-python-compat/src/unicode.rs".into(),
        ] {
            let bytes = read(repo, &path, 1024 * 1024)?;
            source_files.push(json!({"path":path,"bytes":bytes.len(),"sha256":digest(&bytes)}));
        }
    }
    let identity = json!({"role":spec.label,"logical_pack_id":spec.id,"runtime_implementation":if rust {"Rust"} else {"Python"},"runtime_identity":runtime_id,"source_directory":source_directory,"current_rust_sources":source_files,"pack_hash":verified.hash,"component_path":component_path,"component_sha256":hash,"component_bytes":component_bytes,"packaged_wit_sha256":digest(&files["wit/pack.wit"]),"fixture_only":true,"source_build_provenance_verified":false});
    Ok(Pack {
        role,
        rust,
        runtime_id,
        pin,
        verified,
        identity,
    })
}

pub struct Evidence {
    pub directory: PathBuf,
}

/// Derive all five original Python Packs without changing authority or limits.
pub fn share_originals(packs: &mut [Pack], destination: &Path) -> Result<Value> {
    use super::support::shared_modules::{load, transform_pack};
    ensure!(
        packs.len() == ROLES.len()
            && packs
                .iter()
                .enumerate()
                .all(|(index, pack)| pack.role == index && !pack.rust),
        "shared original replay requires all five Python implementations in role order"
    );
    let bundle = environment(&format!("{PREFIX}_SHARED_MODULES_DIR"))?;
    let raw = read_absolute(&bundle.join("extraction.json"), 4 * 1024 * 1024)?;
    let hash = pin(&format!("{PREFIX}_SHARED_MODULES_SHA256"), &raw)?;
    let receipt = load(&bundle, &hash, packs.len())?;
    let records = receipt["components"]
        .as_array()
        .context("shared component records missing")?;
    fs::create_dir(destination)?;
    let mut unique = BTreeMap::new();
    let mut roots = 0;
    let mut other_sources = 0;
    for (index, pack) in packs.iter_mut().enumerate() {
        let directory = destination.join(pack.pin.id.to_string());
        let verified = transform_pack(
            &pack.verified,
            &bundle,
            index,
            &directory,
            &records[index],
            &mut unique,
        )?;
        let RuntimeSpec::Wasm {
            component,
            core_modules,
            ..
        } = &verified.manifest.runtime
        else {
            anyhow::bail!("shared original must remain an ordinary Wasm Pack")
        };
        let root = verified
            .files
            .get(component.as_str())
            .context("shared root missing")?;
        roots += root.len();
        let code: BTreeSet<_> = core_modules
            .values()
            .map(String::as_str)
            .chain(std::iter::once(component.as_str()))
            .collect();
        other_sources += verified
            .manifest
            .files
            .iter()
            .filter(|file| !code.contains(file.path.as_str()))
            .map(|file| file.size)
            .sum::<u64>();
        pack.identity["original_pack_hash"] = pack.identity["pack_hash"].clone();
        pack.identity["original_component_sha256"] = pack.identity["component_sha256"].clone();
        pack.identity["original_component_bytes"] = pack.identity["component_bytes"].clone();
        pack.identity["component_path"] = json!(directory.join(component));
        pack.identity["component_sha256"] =
            json!(root.digest().to_string().trim_start_matches("sha256:"));
        pack.identity["component_bytes"] = json!(root.len());
        pack.identity["pack_hash"] = json!(verified.hash);
        pack.identity["shared_core_modules"] = json!({"extraction_receipt_sha256":hash,
            "original_source_sha256":records[index]["source_sha256"],
            "modules":records[index]["component"]["modules"],
            "transformed_manifest":verified.manifest,"materialization_is_not_runtime_acceptance":true});
        pack.pin = PackPin {
            id: verified.manifest.id.clone(),
            version: verified.manifest.version.clone(),
            sha256: verified.hash.clone(),
            source: format!("file:{}", directory.canonicalize()?.display()),
        };
        pack.verified = verified;
    }
    let component_bytes = roots + unique.values().sum::<u64>();
    let all_sources = component_bytes + other_sources;
    ensure!(
        receipt["catalog_source_bytes"] == component_bytes
            && receipt["unique_modules"] == unique.len()
            && all_sources <= 64 * 1024 * 1024,
        "complete original source closure exceeds or differs from the unchanged budget"
    );
    Ok(
        json!({"extraction_receipt_sha256":hash,"catalog_component_bytes":component_bytes,
        "catalog_noncomponent_bytes":other_sources,"catalog_all_source_bytes":all_sources,
        "unique_modules":unique.len(),"pack_count":packs.len(),
        "only_original_python_implementations":true,"native_core_acceptance":false}),
    )
}

impl Evidence {
    pub fn new(composition: bool) -> Result<Self> {
        let suffix = if composition {
            "COMPOSITION_EVIDENCE_DIR"
        } else {
            "EVIDENCE_DIR"
        };
        let path = environment(&format!("{PREFIX}_{suffix}"))?;
        ensure!(
            path.is_absolute()
                && path
                    .components()
                    .all(|part| !matches!(part, Component::ParentDir)),
            "new absolute evidence directory required"
        );
        no_links(&path)?;
        let parent = path
            .parent()
            .context("evidence parent missing")?
            .canonicalize()?;
        let directory = parent.join(
            path.file_name()
                .context("evidence directory name missing")?,
        );
        let mut builder = fs::DirBuilder::new();
        #[cfg(unix)]
        {
            use std::os::unix::fs::DirBuilderExt as _;
            builder.mode(0o700);
        }
        builder
            .create(&directory)
            .context("evidence directory must be NEW; existing evidence is never overwritten")?;
        Ok(Self { directory })
    }
    pub fn save(&self, path: &str, encoding: &str, bytes: &[u8]) -> Result<Value> {
        ensure!(
            Path::new(path)
                .components()
                .all(|part| matches!(part, Component::Normal(_))),
            "internal sidecar path invalid"
        );
        let mut options = OpenOptions::new();
        options.create_new(true).write(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt as _;
            options.mode(0o600);
        }
        let mut file = options.open(self.directory.join(path))?;
        file.write_all(bytes)?;
        file.sync_all()?;
        Ok(json!({"path":path,"encoding":encoding,"bytes":bytes.len(),"sha256":digest(bytes)}))
    }
    pub fn json(&self, path: &str, value: &Value) -> Result<Value> {
        self.save(path, "utf-8-json", &serde_json::to_vec(value)?)
    }
}
