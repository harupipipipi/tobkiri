//! Verified fixture Packs with private Registry storage.

use super::repo;
use anyhow::{Context, Result};
use serde_json::json;
use std::{collections::BTreeMap, fs, path::Path};
use tobkiri_canon::{Digest, ids::ShapeRef};
use tobkiri_gate::{LoopLimits, PermissionSet, ResourceLimits, StoragePermissions};
use tobkiri_manifest::{
    FileEntry, PackManifest, PortDeclaration, Ports, RuntimeSpec, ShapeDefinition, ShapeKind,
    StoreDefinition, StoreTier,
};
use tobkiri_profile::{PackPin, Profile};

fn store_permissions() -> PermissionSet {
    PermissionSet {
        storage: StoragePermissions {
            private: true,
            ..Default::default()
        },
        wasm: ResourceLimits {
            fuel_per_call: 100_000_000,
            memory_mib: 32,
            wall_ms: 10_000,
            table_elements: 10_000,
            output_bytes: 1024 * 1024,
        },
        loops: LoopLimits {
            max_iterations: 100,
            max_wall_ms: 10_000,
            max_cost_units: 10_000,
        },
        ..Default::default()
    }
}

pub(crate) fn pack(
    root: &Path,
    component: &Path,
    id: &str,
    interfaces: &[&str],
) -> Result<PackPin> {
    let store = id
        .strip_prefix("local.")
        .context("local Registry fixture ID")?;
    let wit = fs::read(repo().join(format!("packs/{id}-rust/wit/pack.wit")))?;
    let sidecar = br#"{"cardinality":"one","streams":{},"callbacks":{}}"#.to_vec();
    let mut files = BTreeMap::from([
        ("component.wasm".to_owned(), fs::read(component)?),
        ("wit/pack.wit".to_owned(), wit),
        (
            "wit/deps/core.wit".to_owned(),
            include_bytes!("../../../tobkiri-wit/wit/core-v1-2/core.wit").to_vec(),
        ),
    ]);
    let mut shapes = Vec::new();
    let mut provides = Vec::new();
    for (name, kind) in interfaces
        .iter()
        .map(|name| (*name, ShapeKind::Port))
        .chain([("data", ShapeKind::Data)])
    {
        let shape = ShapeRef::parse(&format!("{id}.{name}@1.0"))?;
        files.insert(format!("wit/{name}.shape.json"), sidecar.clone());
        if kind == ShapeKind::Port {
            provides.push(PortDeclaration {
                shape: shape.name.clone(),
                version: shape.version,
                optional: false,
            });
        }
        shapes.push(ShapeDefinition {
            name: shape.name,
            version: shape.version,
            kind,
            wit: "wit/pack.wit".into(),
        });
    }
    let manifest = PackManifest {
        format: "tobkiri.pack/1".into(),
        id: id.parse()?,
        version: "0.1.0".into(),
        publisher_key: None,
        runtime: RuntimeSpec::Wasm {
            core_modules: Default::default(),
            component: "component.wasm".into(),
            toolchain: "rust/1.94.0".into(),
            config: None,
        },
        shapes,
        ports: Ports {
            provides,
            ..Default::default()
        },
        inputs: vec![],
        flows: vec![],
        overlays: vec![],
        stores: vec![StoreDefinition {
            name: store.into(),
            tier: StoreTier::Private,
            data_shape: format!("{id}.data@1.0").parse()?,
            seed: None,
        }],
        permissions: store_permissions(),
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
    let directory = root.join(id);
    fs::create_dir_all(directory.join("wit/deps"))?;
    for (path, content) in files {
        fs::write(directory.join(path), content)?;
    }
    fs::write(
        directory.join("pack.json"),
        tobkiri_manifest::canonical_manifest(&manifest)?,
    )?;
    let verified = tobkiri_manifest::verify_directory(&directory, &BTreeMap::new())?;
    Ok(PackPin {
        id: manifest.id,
        version: manifest.version,
        sha256: verified.hash.clone(),
        source: format!("file:{}", directory.canonicalize()?.display()),
    })
}

pub(crate) fn profile(id: &str, pin: &PackPin, ports: &[&str]) -> Result<Profile> {
    let pack_limits = store_permissions();
    let ceiling = PermissionSet {
        ports: ports.iter().map(|shape| (*shape).into()).collect(),
        ..pack_limits.clone()
    };
    let user = PermissionSet {
        ports: ceiling.ports.clone(),
        wasm: ceiling.wasm.clone(),
        loops: ceiling.loops.clone(),
        ..Default::default()
    };
    Ok(serde_json::from_value(json!({
        "format":"tobkiri.profile/1", "id":id, "name":"Registry persistence fixture",
        "packs":[pin], "publishers":{}, "ceiling":ceiling, "user_permissions":user,
        "pack_limits": {(pin.id.as_str()):pack_limits},
        "bindings":{},
        "overlay_order":[], "user_overlays":[], "startup":{"inputs":[],"ui":null}
    }))?)
}
