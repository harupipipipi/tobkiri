//! Verified Registry archives and test-only supporting Packs.

use anyhow::{Context, Result, ensure};
use serde_json::json;
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
};
use tobkiri_canon::Digest;
use tobkiri_gate::{LoopLimits, PermissionSet, ResourceLimits, StoragePermissions};
use tobkiri_manifest::{FileEntry, PackManifest};
use tobkiri_profile::{PackPin, Profile};

pub(crate) const PROVIDER_PACK: &str = "local.provider-registry";
pub(crate) const PROVIDER_MANAGE: &str = "local.provider-registry.manage@1.0";
pub(crate) const PROVIDER_CONFIGURATION: &str = "local.provider-registry.configuration@1.0";
pub(crate) const PROVIDER_COMMIT: &str = "local.provider-registry.configuration-commit@1.0";
pub(crate) const PROVIDER_LEGACY_CONFIGURATION: &str =
    "local.provider-registry.legacy-configuration@1.0";

pub(crate) struct RegistryPack {
    pub(crate) pin: PackPin,
    permissions: PermissionSet,
}

pub(crate) fn store_permissions() -> PermissionSet {
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

pub(crate) fn pack(id: &str) -> Result<RegistryPack> {
    let root = std::env::var_os("TOBKIRI_TEST_REGISTRY_PACKS")
        .context("set TOBKIRI_TEST_REGISTRY_PACKS to the staged Registry archives")?;
    let path = PathBuf::from(root).join(format!("{id}.tpk"));
    let verified = tobkiri_manifest::verify_tpk_file(&path, &BTreeMap::new())?;
    ensure!(
        verified.manifest.id.as_str() == id,
        "unexpected Registry Pack identity"
    );
    Ok(RegistryPack {
        pin: PackPin {
            id: verified.manifest.id.clone(),
            version: verified.manifest.version.clone(),
            sha256: verified.hash.clone(),
            source: format!("file:{}", path.canonicalize()?.display()),
        },
        permissions: verified.manifest.permissions.clone(),
    })
}

pub(crate) fn materialize(
    root: &Path,
    mut manifest: PackManifest,
    files: BTreeMap<String, Vec<u8>>,
) -> Result<RegistryPack> {
    manifest.files = files
        .iter()
        .map(|(path, bytes)| FileEntry {
            path: path.clone(),
            sha256: Digest::hash(bytes),
            size: bytes.len() as u64,
        })
        .collect();
    let directory = root.join(manifest.id.as_str());
    fs::create_dir_all(&directory)?;
    for (path, content) in files {
        let destination = directory.join(path);
        fs::create_dir_all(destination.parent().context("fixture file parent")?)?;
        fs::write(destination, content)?;
    }
    fs::write(
        directory.join("pack.json"),
        tobkiri_manifest::canonical_manifest(&manifest)?,
    )?;
    let verified = tobkiri_manifest::verify_directory(&directory, &BTreeMap::new())?;
    Ok(RegistryPack {
        pin: PackPin {
            id: manifest.id,
            version: manifest.version,
            sha256: verified.hash.clone(),
            source: format!("file:{}", directory.canonicalize()?.display()),
        },
        permissions: manifest.permissions,
    })
}

pub(crate) fn profile(id: &str, packs: &[RegistryPack], ports: &[&str]) -> Result<Profile> {
    let user_ports = ports.iter().map(|shape| (*shape).into()).collect();
    let ceiling = PermissionSet {
        ports: packs
            .iter()
            .flat_map(|pack| pack.permissions.ports.iter().cloned())
            .chain(ports.iter().map(|shape| (*shape).into()))
            .collect(),
        ..store_permissions()
    };
    let user = PermissionSet {
        ports: user_ports,
        wasm: ceiling.wasm.clone(),
        loops: ceiling.loops.clone(),
        ..Default::default()
    };
    Ok(serde_json::from_value(json!({
        "format":"tobkiri.profile/1", "id":id, "name":"Registry persistence fixture",
        "packs":packs.iter().map(|pack| &pack.pin).collect::<Vec<_>>(),
        "publishers":{}, "ceiling":ceiling, "user_permissions":user,
        "pack_limits":packs.iter().map(|pack| (pack.pin.id.as_str(), &pack.permissions)).collect::<BTreeMap<_,_>>(),
        "bindings":{},
        "overlay_order":[], "user_overlays":[], "startup":{"inputs":[],"ui":null}
    }))?)
}
