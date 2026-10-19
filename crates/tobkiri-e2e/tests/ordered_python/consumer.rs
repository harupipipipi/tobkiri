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

const WIT: &str = r#"package local:ordered-python-consumer@1.0.0;

interface entry {
    run: func() -> string;
}
"#;

// Shape documents resolve independently against Core. Keep the dependency
// import in a separate same-package world, resolved with all verified WIT when
// the actual runtime Pack is inspected.
const WORLD: &str = r#"package local:ordered-python-consumer@1.0.0;
world pack {
    include tobkiri:core/pack-minimal@1.0.0;
    import local:ai-tool-bridge-ordered-python/bridge@1.0.0;
    export entry;
}
"#;

// Import the complete candidate interface, including the exact Core error
// signature on normalize. Only runtime-info is invoked by this permission
// probe. The actual Core linker supplies both functions through its ordinary
// typed proxy; no test host function or unknown-import trap is installed.
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
    (import "local:ai-tool-bridge-ordered-python/bridge@1.0.0"
        (instance $bridge-import (type $bridge)))
    (alias export $bridge-import "runtime-info" (func $runtime-info))

    (core module $storage
        (memory (export "memory") 1 1)
        (func (export "realloc") (param i32 i32 i32 i32) (result i32)
            local.get 3 i32.const 61440 i32.gt_u
            if unreachable end
            i32.const 4096))
    (core instance $storage-instance (instantiate $storage))
    (alias core export $storage-instance "memory" (core memory $memory))
    (alias core export $storage-instance "realloc" (core func $realloc))
    (core func $runtime-info-lowered
        (canon lower (func $runtime-info) (memory $memory) (realloc $realloc)))
    (core module $caller
        (import "host" "memory" (memory 1 1))
        (import "host" "runtime-info" (func $runtime-info (param i32)))
        (func (export "init") (param i32 i32) (result i32)
            i32.const 64 i32.const 0 i32.store i32.const 64)
        (func (export "shutdown"))
        (func (export "run") (result i32)
            i32.const 0 call $runtime-info i32.const 0))
    (core instance $caller-instance (instantiate $caller
        (with "host" (instance
            (export "memory" (memory $memory))
            (export "runtime-info" (func $runtime-info-lowered))))))
    (alias core export $caller-instance "init" (core func $init))
    (alias core export $caller-instance "shutdown" (core func $shutdown))
    (alias core export $caller-instance "run" (core func $run))
    (func $init-value (param "config" string) (result $init-result)
        (canon lift (core func $init) (memory $memory) (realloc $realloc)))
    (func $shutdown-value (canon lift (core func $shutdown)))
    (func $run-value (result string)
        (canon lift (core func $run) (memory $memory) (realloc $realloc)))
    (instance $types
        (export "error-code" (type $code))
        (export "error" (type $error)))
    (export "tobkiri:core/types@1.0.0" (instance $types))
    (instance $lifecycle
        (export "init" (func $init-value))
        (export "shutdown" (func $shutdown-value)))
    (export "tobkiri:core/lifecycle@1.0.0" (instance $lifecycle))
    (instance $entry (export "run" (func $run-value)))
    (export "local:ordered-python-consumer/entry@1.0.0" (instance $entry)))"#;

/// Materialize a verified caller in a trusted private fixture root.
///
/// Both allowed and denied profiles retain this same required typed import and
/// requested permission. The denied profile narrows only its consumer limit.
/// The fixed one-string scratch region is sufficient for this no-argument
/// runtime-info probe; it is not a general-purpose normalize caller allocator.
pub fn materialize(root: &Path, provider: &VerifiedPack) -> Result<(PackPin, VerifiedPack)> {
    anyhow::ensure!(
        provider.manifest.id.as_str() == PROVIDER,
        "typed caller requires the actual ordered Python candidate"
    );
    let bridge = ShapeRef::parse(BRIDGE)?;
    anyhow::ensure!(
        provider.manifest.ports.provides.iter().any(|port| {
            port.shape == bridge.name && port.version == bridge.version && !port.optional
        }),
        "ordered Python candidate does not provide its required exact bridge"
    );
    let entry = ShapeRef::parse(ENTRY)?;
    let files: BTreeMap<String, Vec<u8>> = BTreeMap::from([
        ("component.wasm".into(), wat::parse_str(COMPONENT)?),
        ("wit/consumer.wit".into(), WIT.as_bytes().to_vec()),
        ("wit/world.wit".into(), WORLD.as_bytes().to_vec()),
        (
            "wit/deps/ordered-python.wit".into(),
            provider
                .files
                .get("wit/pack.wit")
                .context("verified candidate bridge WIT is missing")?
                .clone(),
        ),
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
            ports: [BRIDGE.to_owned()].into_iter().collect(),
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
    let bytes = wat::parse_str(COMPONENT)?;
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
