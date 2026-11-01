//! Exact authenticated code transformation shared by native test fixtures.
//! This helper never admits a Profile or executes a guest.
use anyhow::{Context, Result, ensure};
use serde_json::Value;
use std::{collections::BTreeMap, fs, path::Path};
use tobkiri_canon::{Digest, Variant};
use tobkiri_manifest::{FileEntry, RuntimeSpec, VerifiedPack};

fn bounded_read(root: &Path, relative: &str, limit: usize) -> Result<Vec<u8>> {
    for ancestor in root.ancestors() {
        ensure!(
            !ancestor.is_symlink(),
            "shared code path contains a symlink"
        );
    }
    Ok(tobkiri_canon::files::read_relative(
        root,
        relative,
        limit as u64,
    )?)
}

pub(crate) const INPUT_LIMIT: u64 = 32 * 1024 * 1024;

pub(crate) fn artifact(
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
    let bytes = bounded_read(bundle, relative, size as usize)?;
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

pub(crate) fn transform_pack(
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

pub(crate) fn load(bundle: &Path, pin: &str, count: usize) -> Result<Value> {
    ensure!(bundle.is_absolute(), "shared bundle path must be absolute");
    let expected = Digest::parse(&format!("sha256:{pin}"))?;
    let raw = bounded_read(bundle, "extraction.json", 4 * 1024 * 1024)?;
    ensure!(
        Digest::hash(&raw) == expected,
        "shared extraction receipt pin differs"
    );
    tobkiri_canon::parse(&raw, Variant::StrictV1)?;
    let receipt: Value = serde_json::from_slice(&raw)?;
    ensure!(
        receipt["format"] == "tobkiri.shared-core-module-materialization/1"
            && receipt["guest_execution_performed"] == false
            && receipt["native_core_acceptance"] == false
            && receipt["production_enabled"] == false,
        "shared materialization is not native acceptance"
    );
    ensure!(
        receipt["components"]
            .as_array()
            .is_some_and(|rows| rows.len() == count),
        "shared component input order/count differs"
    );
    Ok(receipt)
}
