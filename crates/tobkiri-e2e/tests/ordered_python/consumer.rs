//! A real test-only typed caller; the ordered Python provider is never replaced.

use anyhow::{Context, Result};
use std::{collections::BTreeMap, path::Path};
use tobkiri_canon::{Digest, ids::ShapeRef};
use tobkiri_gate::PermissionSet;
use tobkiri_manifest::{
    FileEntry, PackManifest, PortDeclaration, Ports, RuntimeSpec, ShapeDefinition, ShapeKind,
    VerifiedPack,
};
use tobkiri_profile::PackPin;

pub const CONSUMER: &str = "local.ordered-python-consumer";
pub const ENTRY: &str = "local.ordered-python-consumer.entry@1.0";
const PROVIDER: &str = "local.ai-tool-bridge-ordered-python";
const BRIDGE: &str = "local.ai-tool-bridge-ordered-python.bridge@1.0";

/// The two reviewed bridge ABIs; no arbitrary interface substitution is allowed.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum BridgeAbi {
    V1,
    V2,
}

impl BridgeAbi {
    /// Exact Pack-owned shape identity, including its minor version.
    pub fn shape(self) -> &'static str {
        match self {
            Self::V1 => BRIDGE,
            Self::V2 => "local.ai-tool-bridge-ordered-python.bridge@2.0",
        }
    }

    /// Exact component-model interface identity.
    pub fn interface(self) -> &'static str {
        match self {
            Self::V1 => "local:ai-tool-bridge-ordered-python/bridge@1.0.0",
            Self::V2 => "local:ai-tool-bridge-ordered-python/bridge@2.0.0",
        }
    }

    /// Exact major-version profile binding.
    pub fn binding(self) -> &'static str {
        match self {
            Self::V1 => "local.ai-tool-bridge-ordered-python.bridge@1",
            Self::V2 => "local.ai-tool-bridge-ordered-python.bridge@2",
        }
    }

    /// Verified provider member declaring this bridge ABI.
    pub fn wit_member(self) -> &'static str {
        match self {
            Self::V1 => "wit/pack.wit",
            Self::V2 => "wit/pack-v2.wit",
        }
    }
}

const WIT: &str = r#"package local:ordered-python-consumer@1.0.0;

interface entry {
    use tobkiri:core/types@1.0.0.{error};
    run: func() -> string;
    normalize: func(operation: string, payload: string) -> result<string, error>;
}
"#;

// Retain this original template byte-for-byte. The published document combines
// its body with WIT and one nested dependency, because production parses each
// document before same-package union.
const WORLD: &str = r#"package local:ordered-python-consumer@1.0.0;
world pack {
    include tobkiri:core/pack-minimal@1.0.0;
    import @BRIDGE_INTERFACE@;
    export entry;
}
"#;

// A deliberately bounded string-only arena, not a production allocator. Every
// canonical allocation in these signatures is fresh (old pointer/length zero)
// and byte-aligned. Separate input and result strings cannot alias. Core calls
// post-return only after copying the complete result out of guest memory.
// Result records live below 1024; the bump state is a separate Wasm global.
const STORAGE: &str = r#"
        (memory (export "memory") 1 1)
        (global $next (mut i32) (i32.const 1024))
        (func (export "realloc") (param i32 i32 i32 i32) (result i32)
            local.get 0 local.get 1 i32.or
            if unreachable end
            local.get 2 i32.const 1 i32.ne
            if unreachable end
            local.get 3 i32.const 65536 global.get $next i32.sub i32.gt_u
            if unreachable end
            global.get $next
            global.get $next local.get 3 i32.add global.set $next)
        (func (export "post-return") (param i32)
            i32.const 1024 global.set $next)
"#;

// Import the complete candidate interface with the exact Core error identity.
// Both runtime-info and normalize use Core's ordinary typed proxy; no test host
// function, expected-output substitute, or unknown-import trap is installed.
// Canonical return areas: run's string at 0..8, normalize's 16-byte result at
// 16..32 (tag, then ok pointer/length or error code/message pointer/length), and
// init's result at 64..80. None overlaps the string arena beginning at 1024.
const COMPONENT: &str = r#"(component
    (type $core-types (instance
        (type $code-definition (enum
            "denied" "approval-required" "cancelled" "not-found" "invalid"
            "limit-exceeded" "conflict" "unavailable" "internal"))
        (export "error-code" (type $named-code (eq $code-definition)))
        (type $error-definition (record
            (field "code" $named-code) (field "message" string)))
        (export "error" (type (eq $error-definition)))))
    (import "tobkiri:core/types@1.0.0"
        (instance $types-import (type $core-types)))
    (alias export $types-import "error-code" (type $code))
    (alias export $types-import "error" (type $error))
    (type $normalize-result (result string (error $error)))
    (type $init-result (result (error $error)))
    (type $bridge (instance
        (export "runtime-info" (func (result string)))
        (export "normalize" (func
            (param "operation" string) (param "payload" string)
            (result $normalize-result)))))
    (import "@BRIDGE_INTERFACE@"
        (instance $bridge-import (type $bridge)))
    (alias export $bridge-import "runtime-info" (func $runtime-info))
    (alias export $bridge-import "normalize" (func $normalize))

    (core module $storage @STORAGE@)
    (core instance $storage-instance (instantiate $storage))
    (alias core export $storage-instance "memory" (core memory $memory))
    (alias core export $storage-instance "realloc" (core func $realloc))
    (alias core export $storage-instance "post-return" (core func $post-return))
    (core func $runtime-info-lowered
        (canon lower (func $runtime-info) (memory $memory) (realloc $realloc)))
    (core func $normalize-lowered
        (canon lower (func $normalize) (memory $memory) (realloc $realloc)))
    (core module $caller
        (import "host" "memory" (memory 1 1))
        (import "host" "runtime-info" (func $runtime-info (param i32)))
        (import "host" "normalize" (func $normalize (param i32 i32 i32 i32 i32)))
        (func (export "init") (param i32 i32) (result i32)
            i32.const 64 i32.const 0 i32.store i32.const 64)
        (func (export "shutdown"))
        (func (export "run") (result i32)
            i32.const 0 call $runtime-info i32.const 0)
        (func (export "normalize") (param i32 i32 i32 i32) (result i32)
            local.get 0 local.get 1 local.get 2 local.get 3
            i32.const 16 call $normalize i32.const 16))
    (core instance $caller-instance (instantiate $caller
        (with "host" (instance
            (export "memory" (memory $memory))
            (export "runtime-info" (func $runtime-info-lowered))
            (export "normalize" (func $normalize-lowered))))))
    (alias core export $caller-instance "init" (core func $init))
    (alias core export $caller-instance "shutdown" (core func $shutdown))
    (alias core export $caller-instance "run" (core func $run))
    (alias core export $caller-instance "normalize" (core func $forward-normalize))
    (func $init-value (param "config" string) (result $init-result)
        (canon lift (core func $init) (memory $memory) (realloc $realloc)
            (post-return $post-return)))
    (func $shutdown-value (canon lift (core func $shutdown)))
    (func $run-value (result string)
        (canon lift (core func $run) (memory $memory) (realloc $realloc)
            (post-return $post-return)))
    (func $normalize-value
        (param "operation" string) (param "payload" string)
        (result $normalize-result)
        (canon lift (core func $forward-normalize) (memory $memory) (realloc $realloc)
            (post-return $post-return)))
    (instance $types
        (export "error-code" (type $code))
        (export "error" (type $error)))
    (export "tobkiri:core/types@1.0.0" (instance $types))
    (instance $lifecycle
        (export "init" (func $init-value))
        (export "shutdown" (func $shutdown-value)))
    (export "tobkiri:core/lifecycle@1.0.0" (instance $lifecycle))
    (instance $entry
        (export "run" (func $run-value))
        (export "normalize" (func $normalize-value)))
    (export "local:ordered-python-consumer/entry@1.0.0" (instance $entry)))"#;

fn component_bytes() -> Result<Vec<u8>> {
    component_bytes_for(BridgeAbi::V1)
}

fn component_bytes_for(abi: BridgeAbi) -> Result<Vec<u8>> {
    Ok(wat::parse_str(
        COMPONENT
            .replace("@BRIDGE_INTERFACE@", abi.interface())
            .replace("@STORAGE@", STORAGE),
    )?)
}

/// Fixed reversible fixture packaging, not a component or interface rewrite.
fn packaged_wit(abi: BridgeAbi, bridge: &[u8]) -> Result<Vec<u8>> {
    let expected = match abi {
        BridgeAbi::V1 => "625952f31c4caffd4d1de6ba682b15662b22c4e10f11c451592d187acad3cb75",
        BridgeAbi::V2 => "513454d839ac468205bc5b4ed93032625ed4f5d3064aa17f643f907e80bfeb78",
    };
    anyhow::ensure!(
        bridge.len() == 364
            && Digest::hash(bridge).to_string() == format!("sha256:{expected}")
            && bridge[49] == b';',
        "reviewed bridge WIT changed"
    );
    let declaration = b"package local:ordered-python-consumer@1.0.0;\n";
    let world = WORLD.replace("@BRIDGE_INTERFACE@", abi.interface());
    anyhow::ensure!(
        WIT.as_bytes().starts_with(declaration) && world.as_bytes().starts_with(declaration),
        "original consumer package declarations differ"
    );
    let world_body = &world.as_bytes()[declaration.len()..];
    let combined = [
        WIT.as_bytes(),
        b"\n",
        world_body,
        b"\n",
        &bridge[..49],
        b" {",
        &bridge[50..],
        b"}\n",
    ]
    .concat();
    let world_start = WIT.len() + 1;
    let bridge_start = world_start + world_body.len() + 1;
    anyhow::ensure!(
        &combined[..WIT.len()] == WIT.as_bytes()
            && [
                declaration.as_slice(),
                &combined[world_start..bridge_start - 1]
            ]
            .concat()
                == world.as_bytes()
            && [
                &combined[bridge_start..bridge_start + 49],
                b";",
                &combined[bridge_start + 51..combined.len() - 2],
            ]
            .concat()
                == bridge,
        "fixture packaging loses original WIT bytes"
    );
    Ok(combined)
}

/// Materialize a verified caller in a trusted private fixture root.
///
/// Both allowed and denied profiles retain this same required typed import and
/// requested permission. The denied profile narrows only its consumer limit.
/// Its bounded test-only forwarding entry is not the original Gateway caller
/// and does not constitute production caller replacement or full-unit parity.
pub fn materialize(root: &Path, provider: &VerifiedPack) -> Result<(PackPin, VerifiedPack)> {
    materialize_for(root, provider, BridgeAbi::V1)
}

/// Materialize the same caller with only its reviewed provider ABI selected.
pub fn materialize_for(
    root: &Path,
    provider: &VerifiedPack,
    abi: BridgeAbi,
) -> Result<(PackPin, VerifiedPack)> {
    anyhow::ensure!(
        provider.manifest.id.as_str() == PROVIDER,
        "typed caller requires the actual ordered Python candidate"
    );
    let bridge = ShapeRef::parse(abi.shape())?;
    anyhow::ensure!(
        provider.manifest.ports.provides.iter().any(|port| {
            port.shape == bridge.name && port.version == bridge.version && !port.optional
        }),
        "ordered Python candidate does not provide its required exact bridge"
    );
    let entry = ShapeRef::parse(ENTRY)?;
    let provider_wit = provider
        .files
        .get(abi.wit_member())
        .context("verified candidate bridge WIT is missing")?;
    let files: BTreeMap<String, Vec<u8>> = BTreeMap::from([
        ("component.wasm".into(), component_bytes_for(abi)?),
        ("wit/consumer.wit".into(), packaged_wit(abi, provider_wit)?),
        (
            "wit/deps/core.wit".into(),
            provider
                .files
                .get("wit/deps/core.wit")
                .context("verified candidate Core WIT is missing")?
                .clone(),
        ),
        (
            "wit/entry.shape.json".into(),
            serde_json::to_vec(&serde_json::json!({
                "cardinality": "one",
                "streams": {},
                "callbacks": {},
                "conformance": null,
            }))?,
        ),
    ]);
    let manifest = PackManifest {
        format: "tobkiri.pack/1".into(),
        id: CONSUMER.parse()?,
        version: "0.1.0".into(),
        publisher_key: None,
        runtime: RuntimeSpec::Wasm {
            component: "component.wasm".into(),
            toolchain: "wat-test-only-ordered-python-consumer/1".into(),
            config: None,
        },
        shapes: vec![ShapeDefinition {
            name: entry.name.clone(),
            version: entry.version,
            kind: ShapeKind::Port,
            wit: "wit/consumer.wit".into(),
        }],
        ports: Ports {
            provides: vec![PortDeclaration {
                shape: entry.name,
                version: entry.version,
                optional: false,
            }],
            consumes: vec![PortDeclaration {
                shape: bridge.name,
                version: bridge.version,
                optional: false,
            }],
            ..Default::default()
        },
        inputs: Vec::new(),
        flows: Vec::new(),
        overlays: Vec::new(),
        stores: Vec::new(),
        permissions: PermissionSet {
            ports: [abi.shape().to_owned()].into_iter().collect(),
            wasm: provider.manifest.permissions.wasm.clone(),
            loops: provider.manifest.permissions.loops.clone(),
            ..Default::default()
        },
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
    let packs = root.join("packs");
    std::fs::create_dir_all(&packs)?;
    let directory = packs.join(CONSUMER);
    std::fs::create_dir(&directory).context("typed caller destination must be fresh")?;
    for (path, bytes) in &files {
        let target = directory.join(path);
        std::fs::create_dir_all(target.parent().context("caller file has no parent")?)?;
        std::fs::write(target, bytes)?;
    }
    std::fs::write(
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

#[tokio::test]
async fn typed_caller_validates_without_instantiating_or_replacing_python() -> Result<()> {
    let bytes = component_bytes()?;
    let engine = tobkiri_wasm::WasmEngine::new()?;
    let component = wasmtime::component::Component::new(engine.engine(), &bytes)?;
    let imports: std::collections::BTreeSet<_> = component
        .component_type()
        .imports(engine.engine())
        .map(|(name, _)| name.to_owned())
        .collect();
    assert_eq!(
        imports,
        [
            "tobkiri:core/types@1.0.0".to_owned(),
            "local:ai-tool-bridge-ordered-python/bridge@1.0.0".to_owned(),
        ]
        .into_iter()
        .collect()
    );
    // This proves only fixture syntax/type validity, not provider execution,
    // production ABI admission, authority, or native owner acceptance.
    Ok(())
}

#[test]
fn v1_fixture_templates_remain_byte_exact() {
    // Baselines from e98553ae's existing V1 fixture, not provider/build pins.
    // Expand the allocator too, so its full original WAT is covered.
    let world = WORLD.replace("@BRIDGE_INTERFACE@", BridgeAbi::V1.interface());
    let component = COMPONENT
        .replace("@BRIDGE_INTERFACE@", BridgeAbi::V1.interface())
        .replace("@STORAGE@", STORAGE);
    assert_eq!(
        Digest::hash(world.as_bytes()).to_string(),
        "sha256:9ac946582ce8f07d76428fe61fb0e619bc1ebbce7bbed5e3491e24993acf63b1"
    );
    assert_eq!(
        Digest::hash(component.as_bytes()).to_string(),
        "sha256:7e12fdce47fddb0df2a7b839027b43da17989afd4f2faa9db849b2d64df8c1f8"
    );
}

#[test]
fn packaged_consumer_versions_pass_production_wit_inspection_without_guest() -> Result<()> {
    use tobkiri_manifest::WitBindingDirection;

    for abi in [BridgeAbi::V1, BridgeAbi::V2] {
        let bridge = ShapeRef::parse(abi.shape())?;
        let bridge_wit = std::fs::read(
            super::repo()
                .join("packs")
                .join(PROVIDER)
                .join(abi.wit_member()),
        )?;
        let core_wit = std::fs::read(super::repo().join("crates/tobkiri-wit/wit/core/core.wit"))?;
        // This DATA-only source witness is never activated or offered as a
        // runtime provider. It exercises the actual fixture materializer and
        // production package/WIT parsers without a Python binary or guest.
        let files = BTreeMap::from([
            (abi.wit_member().to_owned(), bridge_wit.clone()),
            ("wit/deps/core.wit".into(), core_wit.clone()),
            (
                "wit/bridge.shape.json".into(),
                br#"{"cardinality":"one","streams":{},"callbacks":{},"conformance":null}"#.to_vec(),
            ),
        ]);
        let manifest: PackManifest = serde_json::from_value(serde_json::json!({
            "format":"tobkiri.pack/1","id":PROVIDER,
            "version":if abi == BridgeAbi::V1 {"1.0.0"} else {"2.0.0"},
            "publisher_key":null,"runtime":{"kind":"data"},
            "shapes":[{"name":bridge.name,"version":bridge.version,
                "kind":"port","wit":abi.wit_member()}],
            "ports":{"provides":[{"shape":bridge.name,"version":bridge.version,
                "optional":false}],"consumes":[],"untyped":[]},
            "inputs":[],"flows":[],"overlays":[],"stores":[],"ui":null,
            "permissions":PermissionSet::default(),
            "files":files.iter().map(|(path,bytes)| serde_json::json!({
                "path":path,"size":bytes.len(),"sha256":Digest::hash(bytes)
            })).collect::<Vec<_>>()
        }))?;
        let archive = tobkiri_manifest::build_tpk(&manifest, &files, None)?;
        let provider = tobkiri_manifest::verify_tpk(&archive, &BTreeMap::new())?;
        let temporary = tempfile::tempdir()?;
        let (_, caller) = materialize_for(temporary.path(), &provider, abi)?;
        assert_eq!(
            caller.files.keys().map(String::as_str).collect::<Vec<_>>(),
            [
                "component.wasm",
                "wit/consumer.wit",
                "wit/deps/core.wit",
                "wit/entry.shape.json",
            ]
        );
        assert_eq!(caller.files["component.wasm"], component_bytes_for(abi)?);
        assert_eq!(caller.files["wit/deps/core.wit"], core_wit);
        assert_eq!(
            caller.manifest.permissions.ports,
            [abi.shape().to_owned()].into_iter().collect()
        );
        assert_eq!(caller.manifest.ports.consumes.len(), 1);
        assert!(!caller.manifest.ports.consumes[0].optional);
        let shapes = tobkiri_manifest::load_shapes(&caller.manifest, &caller.files)?;
        assert_eq!(shapes.len(), 1);
        assert_eq!(shapes[0].reference, ShapeRef::parse(ENTRY)?);
        assert_eq!(
            shapes[0]
                .functions
                .keys()
                .map(|name| name.as_str())
                .collect::<Vec<_>>(),
            ["normalize", "run"]
        );
        let views = [&caller, &provider]
            .into_iter()
            .flat_map(|pack| pack.shapes.iter().cloned())
            .map(|shape| (shape.reference.clone(), shape))
            .collect();
        let inspection = tobkiri_manifest::inspect_pack_wit(&caller, &views)?;
        assert!(inspection.allowed_imports().contains(abi.interface()));
        assert!(
            !inspection
                .allowed_imports()
                .contains("local:ordered-python-consumer/entry@1.0.0")
        );
        for (direction, shape) in [
            (WitBindingDirection::Export, ENTRY),
            (WitBindingDirection::Import, abi.shape()),
        ] {
            assert!(inspection.core_errors().has_core_error_result(
                direction,
                &ShapeRef::parse(shape)?,
                &"normalize".parse()?,
            ));
        }
        let reverify = |files: BTreeMap<String, Vec<u8>>| -> Result<VerifiedPack> {
            let mut manifest = caller.manifest.clone();
            manifest.files = files
                .iter()
                .map(|(path, bytes)| FileEntry {
                    path: path.clone(),
                    sha256: Digest::hash(bytes),
                    size: bytes.len() as u64,
                })
                .collect();
            let archive = tobkiri_manifest::build_tpk(&manifest, &files, None)?;
            Ok(tobkiri_manifest::verify_tpk(&archive, &BTreeMap::new())?)
        };
        let mut duplicate = caller.files.clone();
        duplicate.insert("wit/deps/ordered-python.wit".into(), bridge_wit.clone());
        assert!(
            tobkiri_manifest::inspect_pack_wit(&reverify(duplicate)?, &views).is_err(),
            "duplicate dependency must not resolve"
        );
        let world = WORLD.replace("@BRIDGE_INTERFACE@", abi.interface());
        let declaration = b"package local:ordered-python-consumer@1.0.0;\n";
        let mut missing = caller.files.clone();
        missing.insert(
            "wit/consumer.wit".into(),
            [
                WIT.as_bytes(),
                b"\n",
                &world.as_bytes()[declaration.len()..],
            ]
            .concat(),
        );
        assert!(
            reverify(missing).is_err(),
            "missing dependency must not resolve"
        );
        let mut old_split = caller.files.clone();
        old_split.insert("wit/consumer.wit".into(), WIT.as_bytes().to_vec());
        old_split.insert("wit/world.wit".into(), world.into_bytes());
        old_split.insert("wit/deps/ordered-python.wit".into(), bridge_wit.clone());
        assert!(
            tobkiri_manifest::inspect_pack_wit(&reverify(old_split)?, &views)
                .unwrap_err()
                .to_string()
                .contains("interface or world `entry` does not exist"),
            "original split must remain a parser regression, not acceptance evidence"
        );
        let mut changed = bridge_wit.clone();
        changed[0] ^= 1;
        assert!(packaged_wit(abi, &changed).is_err());
        assert!(packaged_wit(abi, &bridge_wit[..363]).is_err());
        let other = if abi == BridgeAbi::V1 {
            BridgeAbi::V2
        } else {
            BridgeAbi::V1
        };
        assert!(packaged_wit(other, &bridge_wit).is_err());
    }
    Ok(())
}

#[tokio::test]
async fn typed_caller_versions_keep_exact_imports_and_core_error_types() -> Result<()> {
    use wasmtime::component::types::{ComponentItem, Type};

    let engine = tobkiri_wasm::WasmEngine::new()?;
    for (abi, shape, interface, binding, wit_member) in [
        (
            BridgeAbi::V1,
            "local.ai-tool-bridge-ordered-python.bridge@1.0",
            "local:ai-tool-bridge-ordered-python/bridge@1.0.0",
            "local.ai-tool-bridge-ordered-python.bridge@1",
            "wit/pack.wit",
        ),
        (
            BridgeAbi::V2,
            "local.ai-tool-bridge-ordered-python.bridge@2.0",
            "local:ai-tool-bridge-ordered-python/bridge@2.0.0",
            "local.ai-tool-bridge-ordered-python.bridge@2",
            "wit/pack-v2.wit",
        ),
    ] {
        assert_eq!(abi.shape(), shape);
        assert_eq!(abi.interface(), interface);
        assert_eq!(abi.binding(), binding);
        assert_eq!(abi.wit_member(), wit_member);
        let world = WORLD.replace("@BRIDGE_INTERFACE@", abi.interface());
        assert!(world.contains(&format!("import {interface};")));
        assert!(world.contains("include tobkiri:core/pack-minimal@1.0.0;"));
        assert_eq!(world.matches("import ").count(), 1);

        let bytes = component_bytes_for(abi)?;
        let component = wasmtime::component::Component::new(engine.engine(), &bytes)?;
        let imports: BTreeMap<_, _> = component
            .component_type()
            .imports(engine.engine())
            .map(|(name, item)| (name.to_owned(), item.ty))
            .collect();
        assert_eq!(
            imports.keys().map(String::as_str).collect::<Vec<_>>(),
            [interface, "tobkiri:core/types@1.0.0"]
        );
        let ComponentItem::ComponentInstance(bridge) = &imports[interface] else {
            anyhow::bail!("bridge import is not a typed interface");
        };
        let members: BTreeMap<_, _> = bridge
            .exports(engine.engine())
            .map(|(name, item)| (name.to_owned(), item.ty))
            .collect();
        assert_eq!(
            members.keys().map(String::as_str).collect::<Vec<_>>(),
            ["normalize", "runtime-info"]
        );
        let ComponentItem::ComponentFunc(runtime) = &members["runtime-info"] else {
            anyhow::bail!("runtime-info is not a function");
        };
        assert_eq!(runtime.params().len(), 0);
        assert_eq!(runtime.results().collect::<Vec<_>>(), [Type::String]);
        let ComponentItem::ComponentFunc(normalize) = &members["normalize"] else {
            anyhow::bail!("normalize is not a function");
        };
        assert_eq!(
            normalize.params().collect::<Vec<_>>(),
            [("operation", Type::String), ("payload", Type::String)]
        );
        let results: Vec<_> = normalize.results().collect();
        let [Type::Result(result)] = results.as_slice() else {
            anyhow::bail!("normalize has the wrong result type");
        };
        assert_eq!(result.ok(), Some(Type::String));
        let ComponentItem::ComponentInstance(core) = &imports["tobkiri:core/types@1.0.0"] else {
            anyhow::bail!("Core types import is not a typed interface");
        };
        let (_, error) = core
            .exports(engine.engine())
            .find(|(name, _)| *name == "error")
            .context("Core error type is missing")?;
        let ComponentItem::Type(error) = error.ty else {
            anyhow::bail!("Core error is not a type");
        };
        assert_eq!(result.err(), Some(error));

        let exports: BTreeMap<_, _> = component
            .component_type()
            .exports(engine.engine())
            .map(|(name, item)| (name.to_owned(), item.ty))
            .collect();
        assert_eq!(
            exports.keys().map(String::as_str).collect::<Vec<_>>(),
            [
                "local:ordered-python-consumer/entry@1.0.0",
                "tobkiri:core/lifecycle@1.0.0",
                "tobkiri:core/types@1.0.0",
            ]
        );
        let ComponentItem::ComponentInstance(entry) =
            &exports["local:ordered-python-consumer/entry@1.0.0"]
        else {
            anyhow::bail!("caller entry is not a typed interface");
        };
        let (_, forward) = entry
            .exports(engine.engine())
            .find(|(name, _)| *name == "normalize")
            .context("caller normalize is missing")?;
        let ComponentItem::ComponentFunc(forward) = forward.ty else {
            anyhow::bail!("caller normalize is not a function");
        };
        assert_eq!(
            forward.params().collect::<Vec<_>>(),
            normalize.params().collect::<Vec<_>>()
        );
        assert_eq!(forward.results().collect::<Vec<_>>(), results);
    }
    // This inspects real compiled callers, never a mock provider or an
    // execution/authority receipt. Core's imported error aliases and the
    // caller's entry@1 package are deliberately unchanged in both versions.
    Ok(())
}

#[test]
fn string_arena_is_disjoint_bounded_and_reset_after_return() -> Result<()> {
    let engine = wasmtime::Engine::default();
    let module = wasmtime::Module::new(&engine, wat::parse_str(format!("(module {STORAGE})"))?)?;
    let mut store = wasmtime::Store::new(&engine, ());
    let instance = wasmtime::Instance::new(&mut store, &module, &[])?;
    let alloc = instance.get_typed_func::<(i32, i32, i32, i32), i32>(&mut store, "realloc")?;
    let reset = instance.get_typed_func::<i32, ()>(&mut store, "post-return")?;
    let memory = instance.get_memory(&mut store, "memory").unwrap();
    for _ in 0..2 {
        let operation = alloc.call(&mut store, (0, 0, 1, 48))?;
        let payload = alloc.call(&mut store, (0, 0, 1, 120))?;
        let output = alloc.call(&mut store, (0, 0, 1, 256))?;
        assert_eq!((operation, payload, output), (1024, 1072, 1192));
        memory.write(&mut store, operation as usize, &[b'o'; 48])?;
        memory.write(&mut store, payload as usize, &[b'p'; 120])?;
        memory.write(&mut store, output as usize, &[b'r'; 256])?;
        let bytes = memory.data(&store);
        assert_eq!(&bytes[1024..1072], &[b'o'; 48]);
        assert_eq!(&bytes[1072..1192], &[b'p'; 120]);
        assert_eq!(&bytes[1192..1448], &[b'r'; 256]);
        assert!(alloc.call(&mut store, (0, 0, 1, 65536)).is_err());
        assert!(alloc.call(&mut store, (0, 0, 1, -1)).is_err());
        assert!(alloc.call(&mut store, (operation, 48, 1, 49)).is_err());
        assert!(alloc.call(&mut store, (0, 0, 4, 4)).is_err());
        assert_eq!(alloc.call(&mut store, (0, 0, 1, 64088))?, 1448);
        assert!(alloc.call(&mut store, (0, 0, 1, 1)).is_err());
        reset.call(&mut store, 16)?;
    }
    // This exercises only the allocator, never a substitute Python provider.
    Ok(())
}
