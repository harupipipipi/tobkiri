//! Fail-closed verification for the packaged Python runtime tree.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::Path;

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::runtime_resource_paths::{CanonicalResourcePath, SealedApplicationResourceBinding};

pub(crate) const MANIFEST_NAME: &str = "runtime-resource-manifest.v1.json";
const MANIFEST_SCHEMA: &str = "io.tobkiri.runtime-resource-manifest.v1";

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct VerifiedResourceManifest {
    sha256: String,
    entries: BTreeMap<CanonicalResourcePath, ResourceEntry>,
}

impl VerifiedResourceManifest {
    pub(crate) fn sha256(&self) -> &str {
        &self.sha256
    }

    pub(crate) fn entry(&self, path: &CanonicalResourcePath) -> Option<&ResourceEntry> {
        self.entries.get(path)
    }

    pub(crate) fn bind_sealed_application(
        &self,
        sealed_path: &str,
        size: u64,
        sha256: &str,
    ) -> Result<ResourceEntry> {
        let binding = SealedApplicationResourceBinding::from_sealed_path(sealed_path)
            .map_err(|error| anyhow::anyhow!(error))?;
        let outer_entry = self.entry(&binding.outer).with_context(|| {
            format!(
                "[PYTHON_SEALED_SNAPSHOT_INVALID] outer runtime manifest omits sealed application resource: {}",
                binding.outer.as_str()
            )
        })?;
        if outer_entry.size != size || outer_entry.sha256 != sha256 {
            bail!(
                "[PYTHON_SEALED_SNAPSHOT_INVALID] outer and sealed application bindings differ: {}",
                binding.outer.as_str()
            );
        }
        Ok(ResourceEntry {
            path: binding.application.as_str().to_owned(),
            size: outer_entry.size,
            sha256: outer_entry.sha256.clone(),
        })
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ResourceManifest {
    schema: String,
    entries: Vec<ResourceEntry>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ResourceEntry {
    pub(crate) path: String,
    pub(crate) size: u64,
    pub(crate) sha256: String,
}

#[cfg(unix)]
fn has_one_link(_path: &Path, metadata: &fs::Metadata) -> Result<bool> {
    use std::os::unix::fs::MetadataExt;
    Ok(metadata.nlink() == 1)
}

#[cfg(windows)]
fn has_one_link(path: &Path, _metadata: &fs::Metadata) -> Result<bool> {
    use std::mem::MaybeUninit;
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::{
        GetFileInformationByHandle, BY_HANDLE_FILE_INFORMATION,
    };

    let file = fs::File::open(path)?;
    let mut information = MaybeUninit::<BY_HANDLE_FILE_INFORMATION>::zeroed();
    if unsafe { GetFileInformationByHandle(file.as_raw_handle(), information.as_mut_ptr()) } == 0 {
        return Err(std::io::Error::last_os_error()).context("inspect runtime resource links");
    }
    Ok(unsafe { information.assume_init() }.nNumberOfLinks == 1)
}

#[cfg(not(any(unix, windows)))]
fn has_one_link(_path: &Path, _metadata: &fs::Metadata) -> Result<bool> {
    bail!("runtime resource link-count inspection is unavailable")
}

fn collect_files(
    root: &Path,
    current: &Path,
    files: &mut Vec<CanonicalResourcePath>,
    ambiguity_keys: &mut BTreeSet<String>,
) -> Result<()> {
    for entry in fs::read_dir(current)
        .with_context(|| format!("failed to read runtime directory {}", current.display()))?
    {
        let entry = entry?;
        let path = entry.path();
        let metadata = fs::symlink_metadata(&path)?;
        if metadata.file_type().is_symlink() {
            bail!(
                "packaged runtime resource may not be a symlink: {}",
                path.display()
            );
        }
        if metadata.is_dir() {
            if path.file_name().and_then(|name| name.to_str()) == Some("__pycache__") {
                bail!(
                    "packaged runtime may not contain Python bytecode: {}",
                    path.display()
                );
            }
            collect_files(root, &path, files, ambiguity_keys)?;
        } else if metadata.is_file() && path.strip_prefix(root)? != Path::new(MANIFEST_NAME) {
            if matches!(
                path.extension().and_then(|value| value.to_str()),
                Some("pyc" | "pyo")
            ) {
                bail!(
                    "packaged runtime may not contain Python bytecode: {}",
                    path.display()
                );
            }
            if !has_one_link(&path, &metadata)? {
                bail!(
                    "packaged runtime resource may not be hardlinked: {}",
                    path.display()
                );
            }
            let relative = path
                .strip_prefix(root)?
                .to_str()
                .context("packaged runtime resource path is not Unicode")?
                .replace('\\', "/");
            let canonical =
                CanonicalResourcePath::parse(&relative).map_err(|error| anyhow::anyhow!(error))?;
            if !ambiguity_keys.insert(canonical.ambiguity_key()) {
                bail!("packaged runtime resource paths are ambiguous by ASCII case");
            }
            files.push(canonical);
        } else if !metadata.is_file() {
            bail!(
                "packaged runtime resource may not be special: {}",
                path.display()
            );
        }
    }
    Ok(())
}

pub(crate) fn verify(root: &Path) -> Result<VerifiedResourceManifest> {
    let manifest_path = root.join(MANIFEST_NAME);
    let metadata = fs::symlink_metadata(&manifest_path).with_context(|| {
        format!(
            "packaged runtime manifest is missing: {}",
            manifest_path.display()
        )
    })?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        bail!("packaged runtime manifest is not a regular file");
    }
    if !has_one_link(&manifest_path, &metadata)? {
        bail!("packaged runtime manifest may not be hardlinked");
    }
    let manifest_bytes = fs::read(&manifest_path)?;
    let manifest: ResourceManifest = serde_json::from_slice(&manifest_bytes)
        .context("packaged runtime manifest is malformed")?;
    if manifest.schema != MANIFEST_SCHEMA {
        bail!("packaged runtime manifest schema is unsupported");
    }

    let mut expected = BTreeMap::new();
    let mut verified_entries = BTreeMap::new();
    let mut expected_ambiguity_keys = BTreeSet::new();
    for entry in manifest.entries {
        let relative =
            CanonicalResourcePath::parse(&entry.path).map_err(|error| anyhow::anyhow!(error))?;
        if entry.sha256.len() != 64
            || !entry
                .sha256
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
            || !expected_ambiguity_keys.insert(relative.ambiguity_key())
            || expected
                .insert(relative.clone(), (entry.size, entry.sha256.clone()))
                .is_some()
        {
            bail!("packaged runtime manifest contains an unsafe or duplicate path");
        }
        verified_entries.insert(relative, entry);
    }

    let mut actual_files = Vec::new();
    let mut actual_ambiguity_keys = BTreeSet::new();
    collect_files(root, root, &mut actual_files, &mut actual_ambiguity_keys)?;
    actual_files.sort();
    if actual_files != expected.keys().cloned().collect::<Vec<_>>() {
        bail!("packaged runtime file inventory does not match its manifest");
    }
    for relative in actual_files {
        let payload = fs::read(root.join(relative.as_path()))?;
        let (size, digest) = &expected[&relative];
        if payload.len() as u64 != *size || format!("{:x}", Sha256::digest(&payload)) != *digest {
            bail!(
                "packaged runtime resource failed integrity: {}",
                relative.as_str()
            );
        }
    }
    Ok(VerifiedResourceManifest {
        sha256: format!("{:x}", Sha256::digest(&manifest_bytes)),
        entries: verified_entries,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::time::{SystemTime, UNIX_EPOCH};

    static FIXTURE_SEQUENCE: AtomicU64 = AtomicU64::new(0);

    fn fixture_path(nonce: u128) -> PathBuf {
        let sequence = FIXTURE_SEQUENCE.fetch_add(1, Ordering::Relaxed);
        std::env::temp_dir().join(format!(
            "tobkiri-runtime-integrity-{}-{nonce}-{sequence}",
            std::process::id()
        ))
    }

    fn fixture() -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = fixture_path(nonce);
        fs::create_dir_all(root.join("core_runtime")).unwrap();
        let payload = b"bootstrap\n";
        fs::write(root.join("core_runtime/bootstrap.py"), payload).unwrap();
        let manifest = serde_json::json!({
            "schema": MANIFEST_SCHEMA,
            "entries": [{
                "path": "core_runtime/bootstrap.py",
                "size": payload.len(),
                "sha256": format!("{:x}", Sha256::digest(payload)),
            }],
        });
        fs::write(
            root.join(MANIFEST_NAME),
            serde_json::to_vec(&manifest).unwrap(),
        )
        .unwrap();
        root
    }

    #[test]
    fn fixture_paths_are_unique_when_clock_values_match() {
        assert_ne!(fixture_path(0), fixture_path(0));
    }

    #[test]
    fn accepts_exact_resource_tree() {
        let root = fixture();
        let verification = verify(&root);
        assert!(verification.is_ok(), "{verification:?}");
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn rejects_missing_extra_and_tampered_resources() {
        let missing = fixture();
        fs::remove_file(missing.join("core_runtime/bootstrap.py")).unwrap();
        assert!(verify(&missing).is_err());
        fs::remove_dir_all(missing).unwrap();

        let extra = fixture();
        fs::write(extra.join("unlisted-resource.txt"), b"unlisted\n").unwrap();
        assert!(verify(&extra).is_err());
        fs::remove_dir_all(extra).unwrap();

        let tampered = fixture();
        fs::write(tampered.join("core_runtime/bootstrap.py"), b"tampered").unwrap();
        assert!(verify(&tampered).is_err());
        fs::remove_dir_all(tampered).unwrap();

        let nested_manifest = fixture();
        fs::write(
            nested_manifest.join("core_runtime/runtime-resource-manifest.v1.json"),
            b"{}",
        )
        .unwrap();
        assert!(verify(&nested_manifest).is_err());
        fs::remove_dir_all(nested_manifest).unwrap();
    }

    #[test]
    fn rejects_python_bytecode_even_when_listed() {
        let root = fixture();
        let bytecode = b"bytecode";
        let cache = root.join("core_runtime/__pycache__");
        fs::create_dir_all(&cache).unwrap();
        fs::write(cache.join("bootstrap.pyc"), bytecode).unwrap();
        let manifest_path = root.join(MANIFEST_NAME);
        let mut manifest: serde_json::Value =
            serde_json::from_slice(&fs::read(&manifest_path).unwrap()).unwrap();
        manifest["entries"]
            .as_array_mut()
            .unwrap()
            .push(serde_json::json!({
                "path": "core_runtime/__pycache__/bootstrap.pyc",
                "size": bytecode.len(),
                "sha256": format!("{:x}", Sha256::digest(bytecode)),
            }));
        fs::write(&manifest_path, serde_json::to_vec(&manifest).unwrap()).unwrap();

        assert!(verify(&root).is_err());
        fs::remove_dir_all(root).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn rejects_symlinked_resources() {
        use std::os::unix::fs::symlink;

        let root = fixture();
        let target = root.join("target.py");
        fs::write(&target, b"bootstrap\n").unwrap();
        fs::remove_file(root.join("core_runtime/bootstrap.py")).unwrap();
        symlink(&target, root.join("core_runtime/bootstrap.py")).unwrap();
        assert!(verify(&root).is_err());
        fs::remove_dir_all(root).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn rejects_hardlinked_resources() {
        let root = fixture();
        let target = root.join("outside.py");
        fs::write(&target, b"bootstrap\n").unwrap();
        fs::remove_file(root.join("core_runtime/bootstrap.py")).unwrap();
        fs::hard_link(&target, root.join("core_runtime/bootstrap.py")).unwrap();
        assert!(verify(&root).is_err());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn rejects_noncanonical_and_case_ambiguous_manifest_paths() {
        for paths in [
            vec!["core_runtime/../bootstrap.py"],
            vec!["core_runtime//bootstrap.py"],
            vec!["core_runtime\\bootstrap.py"],
            vec!["core_runtime/é.py"],
            vec!["core_runtime/bootstrap.py", "core_runtime/Bootstrap.py"],
        ] {
            let root = fixture();
            let entries = paths
                .iter()
                .map(|path| {
                    serde_json::json!({
                        "path": path,
                        "size": 10,
                        "sha256": format!("{:x}", Sha256::digest(b"bootstrap\n")),
                    })
                })
                .collect::<Vec<_>>();
            fs::write(
                root.join(MANIFEST_NAME),
                serde_json::to_vec(&serde_json::json!({
                    "schema": MANIFEST_SCHEMA,
                    "entries": entries,
                }))
                .unwrap(),
            )
            .unwrap();
            assert!(verify(&root).is_err(), "{paths:?}");
            fs::remove_dir_all(root).unwrap();
        }
    }

    #[test]
    fn binds_actual_packaged_sealed_application_domain_without_fallback() {
        let root = fixture();
        fs::remove_dir_all(root.join("core_runtime")).unwrap();
        let payload = b"entry\n";
        let outer_path = root.join("python-runtime/app/defaultspack_entry.py");
        fs::create_dir_all(outer_path.parent().unwrap()).unwrap();
        fs::write(&outer_path, payload).unwrap();
        fs::write(
            root.join(MANIFEST_NAME),
            serde_json::to_vec(&serde_json::json!({
                "schema": MANIFEST_SCHEMA,
                "entries": [{
                    "path": "python-runtime/app/defaultspack_entry.py",
                    "size": payload.len(),
                    "sha256": format!("{:x}", Sha256::digest(payload)),
                }],
            }))
            .unwrap(),
        )
        .unwrap();
        let manifest = verify(&root).unwrap();
        let entry = manifest
            .bind_sealed_application(
                "app/defaultspack_entry.py",
                payload.len() as u64,
                &format!("{:x}", Sha256::digest(payload)),
            )
            .unwrap();
        assert_eq!(entry.path, "defaultspack_entry.py");

        let legacy = fixture();
        let legacy_manifest = verify(&legacy).unwrap();
        assert!(legacy_manifest
            .bind_sealed_application(
                "app/core_runtime/bootstrap.py",
                10,
                &format!("{:x}", Sha256::digest(b"bootstrap\n")),
            )
            .is_err());
        fs::remove_dir_all(root).unwrap();
        fs::remove_dir_all(legacy).unwrap();
    }
}
