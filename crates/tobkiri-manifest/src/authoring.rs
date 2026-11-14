use super::{
    PackageBudget, canonical_manifest, check_entry_count, invalid, key, parse_manifest,
    portable_path,
};
use crate::{FileEntry, ManifestError};
use sha2::{Digest as _, Sha256};
use std::{collections::BTreeMap, io::ErrorKind, path::Path};
use tobkiri_canon::{CanonError, CanonValue, Digest, Variant, encode, files, parse};

/// Create canonical `pack.json` bytes from a StrictV1 draft and a staging directory.
///
/// The draft has the ordinary manifest fields, except `files` is an explicit
/// list of relative payload paths. `permissions` must be an object; fields
/// omitted from that object use the normal manifest defaults. The result binds
/// only the listed regular files using the Unix directory reader. It is not
/// signed or verified for admission.
pub fn generate_manifest(root: &Path, draft: &[u8]) -> Result<Vec<u8>, ManifestError> {
    if !cfg!(unix) {
        return Err(invalid(
            "secure Pack directory authoring is unavailable on this platform",
        ));
    }
    let CanonValue::Obj(mut document) = parse(draft, Variant::StrictV1)? else {
        return Err(invalid("Pack manifest draft must be an object"));
    };
    if !matches!(document.get("permissions"), Some(CanonValue::Obj(_))) {
        return Err(invalid("Pack manifest draft requires a permissions object"));
    }
    let Some(CanonValue::Arr(paths)) = document.remove("files") else {
        return Err(invalid(
            "Pack manifest draft requires a files array of paths",
        ));
    };
    check_entry_count(paths.len(), false)?;

    // Feed ordinary typed validation an internal inventory before reading any
    // payload, so path aliases and undeclared metadata references fail early.
    let empty = Digest::hash(&[]);
    let placeholders = paths
        .into_iter()
        .map(|path| {
            let CanonValue::Str(name) = &path else {
                return Err(invalid(
                    "draft files must be paths without hash or size fields",
                ));
            };
            portable_path(name)?;
            let first = name.split('/').next().unwrap_or_default();
            if first.eq_ignore_ascii_case("pack.json")
                || first.eq_ignore_ascii_case("signature.json")
            {
                return Err(invalid("reserved package metadata path"));
            }
            tar::Header::new_ustar().set_path(name)?;
            Ok(CanonValue::Obj(BTreeMap::from([
                ("path".into(), path),
                ("sha256".into(), CanonValue::Str(empty.as_str().into())),
                ("size".into(), CanonValue::Int(0)),
            ])))
        })
        .collect::<Result<_, ManifestError>>()?;
    document.insert("files".into(), CanonValue::Arr(placeholders));
    let mut manifest = parse_manifest(&encode(&CanonValue::Obj(document), Variant::StrictV1)?)?;
    if let Some(publisher) = &manifest.publisher_key {
        key(publisher)?;
    }
    let signed = manifest.publisher_key.is_some();
    check_entry_count(manifest.files.len(), signed)?;
    manifest.files.sort_unstable_by(|a, b| a.path.cmp(&b.path));

    if !std::fs::symlink_metadata(root)?.is_dir() {
        return Err(invalid("Pack staging root must be a real directory"));
    }
    let mut budget = PackageBudget::new();
    if signed {
        // A canonical Ed25519 signature envelope always fits one tar block.
        budget.add(1)?;
    }
    // Reserve the manifest header and a lower bound for its payload up front.
    // The finished size can only grow as zero file sizes are replaced.
    let initial_bytes = canonical_manifest(&manifest)?.len() as u64;
    budget.add(initial_bytes)?;
    for entry in &mut manifest.files {
        fill_entry(root, entry, budget.remaining_member_limit()?)?;
        budget.add(entry.size)?;
    }
    let result = canonical_manifest(&manifest)?;
    let mut final_budget = PackageBudget::new();
    if signed {
        final_budget.add(1)?;
    }
    final_budget.add(result.len() as u64)?;
    for entry in &manifest.files {
        final_budget.add(entry.size)?;
    }
    Ok(result)
}

fn fill_entry(root: &Path, entry: &mut FileEntry, limit: u64) -> Result<(), ManifestError> {
    let (sha256, size) = files::read_relative_with(root, &entry.path, limit, |reader, size| {
        let mut hasher = Sha256::new();
        let mut buffer = [0u8; 8192];
        loop {
            match reader.read(&mut buffer) {
                Ok(0) => return Ok((Digest::from_sha256(hasher.finalize().into()), size)),
                Ok(len) => hasher.update(&buffer[..len]),
                Err(error) if error.kind() == ErrorKind::Interrupted => continue,
                Err(error) => return Err(CanonError::Invalid(format!("artifact IO: {error}"))),
            }
        }
    })
    .map_err(|error| {
        invalid(format!(
            "cannot inventory Pack file {}: {error}",
            entry.path
        ))
    })?;
    entry.sha256 = sha256;
    entry.size = size;
    Ok(())
}
