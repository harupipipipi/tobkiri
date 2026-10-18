//! Portable VM assets within an already build-authenticated development stage.

use anyhow::{bail, Context, Result};
use std::path::{Path, PathBuf};

pub(crate) const ROOT_ENV: &str = "TOBKIRI_DEVELOPMENT_PACKVM_BUNDLE_ROOT";
pub(crate) const DIGEST_ENV: &str = "TOBKIRI_DEVELOPMENT_PACKVM_BUNDLE_SHA256";

/// Verify the VM subtree only after the caller authenticates the enclosing
/// stage against the executable's embedded authority-manifest digest.
pub(crate) fn verify_assets(
    verified_stage: &Path,
    expected: &str,
    accelerator: &str,
) -> Result<Option<(PathBuf, String)>> {
    let assets = verified_stage.join(crate::packvm_bundle::DIRECTORY);
    if expected.is_empty() {
        match std::fs::symlink_metadata(&assets) {
            Ok(_) => bail!("unbound portable VM assets are present in the development stage"),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error).context("unbound VM asset path cannot be inspected"),
        }
    }
    let verification_started = std::time::Instant::now();
    crate::runtime_resource_integrity::verify_subtree(
        verified_stage,
        crate::packvm_bundle::DIRECTORY,
    )
    .context("build-bound development VM subtree seal is invalid")?;
    log::info!(
        "Development VM verification phase=outer-seal elapsed_ms={}",
        verification_started.elapsed().as_millis()
    );
    let inventory_started = std::time::Instant::now();
    crate::packvm_bundle::verify(&assets, expected, accelerator)
        .context("build-bound development VM inventory is invalid")?;
    log::info!(
        "Development VM verification phase=inventory elapsed_ms={} total_elapsed_ms={}",
        inventory_started.elapsed().as_millis(),
        verification_started.elapsed().as_millis()
    );
    Ok(Some((assets, format!("sha256:{expected}"))))
}

/// Never inherit caller-selected development VM authority.
pub(crate) fn configure_environment(
    command: &mut std::process::Command,
    binding: Option<&(PathBuf, String)>,
    macos_root: Option<&Path>,
) {
    command.env_remove(ROOT_ENV).env_remove(DIGEST_ENV);
    if let Some((root, digest)) = binding {
        command.env(ROOT_ENV, root).env(DIGEST_ENV, digest);
    } else if let Some(root) = macos_root {
        command.env(ROOT_ENV, root);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use sha2::{Digest, Sha256};
    use std::ffi::{OsStr, OsString};
    use std::fs;
    use std::process::Command;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::time::{SystemTime, UNIX_EPOCH};

    static FIXTURE_SEQUENCE: AtomicU64 = AtomicU64::new(0);

    struct TestTree(PathBuf);

    impl TestTree {
        fn new(label: &str) -> Self {
            let nonce = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos();
            let sequence = FIXTURE_SEQUENCE.fetch_add(1, Ordering::Relaxed);
            let root = std::env::temp_dir().canonicalize().unwrap().join(format!(
                "tobkiri-development-packvm-{label}-{}-{nonce}-{sequence}",
                std::process::id(),
            ));
            fs::create_dir(&root).unwrap();
            Self(root)
        }
    }

    impl Drop for TestTree {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn write_vm_fixture(stage: &Path, accelerator: &str) -> (PathBuf, String) {
        fs::create_dir(stage).unwrap();
        let root = stage.join(crate::packvm_bundle::DIRECTORY);
        fs::create_dir(&root).unwrap();
        let mut files = serde_json::Map::new();
        for slot in [
            "agent",
            "bubblewrap",
            "bubblewrap_descriptor",
            "config",
            "firmware_code",
            "firmware_vars",
            "image",
            "qemu",
            "service",
        ] {
            let bytes = format!("reviewed fixture {slot}").into_bytes();
            fs::write(root.join(slot), &bytes).unwrap();
            files.insert(
                slot.to_owned(),
                serde_json::json!({
                    "path": slot,
                    "sha256": format!("sha256:{:x}", Sha256::digest(&bytes)),
                    "size_bytes": bytes.len(),
                }),
            );
        }
        let manifest = serde_json::to_vec(&serde_json::json!({
            "schema": "io.tobkiri.packvm-qemu-provisioning.v1",
            "architecture": "amd64",
            "accelerator": accelerator,
            "files": files,
            "image_source": "https://example.invalid/reviewed-fixture.raw",
            "qemu_dependencies": [],
        }))
        .unwrap();
        let digest = format!("{:x}", Sha256::digest(&manifest));
        fs::write(root.join(crate::packvm_bundle::MANIFEST), manifest).unwrap();
        write_vm_outer_seal(stage);
        (root, digest)
    }

    fn write_vm_outer_seal(stage: &Path) {
        let assets = stage.join(crate::packvm_bundle::DIRECTORY);
        let mut paths = fs::read_dir(&assets)
            .unwrap()
            .map(|entry| entry.unwrap().path())
            .collect::<Vec<_>>();
        paths.sort();
        let entries = paths
            .into_iter()
            .map(|path| {
                let (sha256, size) = crate::packvm_bundle::hash_regular(&path).unwrap();
                serde_json::json!({
                    "path": format!(
                        "{}/{}",
                        crate::packvm_bundle::DIRECTORY,
                        path.file_name().unwrap().to_str().unwrap(),
                    ),
                    "size": size,
                    "sha256": sha256,
                })
            })
            .collect::<Vec<_>>();
        fs::write(
            stage.join(crate::runtime_resource_integrity::MANIFEST_NAME),
            serde_json::to_vec(&serde_json::json!({
                "schema": "io.tobkiri.runtime-resource-manifest.v1",
                "entries": entries,
            }))
            .unwrap(),
        )
        .unwrap();
    }

    fn environment_change(command: &Command, key: &str) -> Option<Option<OsString>> {
        command
            .get_envs()
            .find(|(name, _)| *name == OsStr::new(key))
            .map(|(_, value)| value.map(OsStr::to_owned))
    }

    #[test]
    fn absent_pin_and_assets_are_unbound_but_present_unpinned_assets_are_rejected() {
        let tree = TestTree::new("absent");
        let stage = tree.0.join("stage");
        fs::create_dir(&stage).unwrap();
        assert_eq!(verify_assets(&stage, "", "kvm").unwrap(), None);
        fs::create_dir(stage.join(crate::packvm_bundle::DIRECTORY)).unwrap();
        let error = verify_assets(&stage, "", "kvm").unwrap_err();
        assert!(error.to_string().contains("unbound portable VM assets"));
    }

    #[test]
    fn nonempty_pin_never_turns_missing_assets_into_an_unbound_build() {
        let tree = TestTree::new("missing");
        let stage = tree.0.join("stage");
        fs::create_dir(&stage).unwrap();
        assert!(verify_assets(&stage, &"1".repeat(64), "kvm").is_err());
    }

    #[test]
    fn matching_linux_and_windows_assets_return_exact_root_and_prefixed_pin() {
        for accelerator in ["kvm", "whpx"] {
            let tree = TestTree::new(accelerator);
            let stage = tree.0.join("stage");
            let (root, digest) = write_vm_fixture(&stage, accelerator);
            assert_eq!(
                verify_assets(&stage, &digest, accelerator).unwrap(),
                Some((root, format!("sha256:{digest}"))),
            );
        }
    }

    #[test]
    fn wrong_manifest_pin_invalid_digest_and_accelerator_are_rejected() {
        let tree = TestTree::new("wrong-pin-platform");
        let stage = tree.0.join("stage");
        let (_, digest) = write_vm_fixture(&stage, "kvm");
        for (pin, accelerator) in [
            ("0".repeat(64), "kvm"),
            (format!("sha256:{digest}"), "kvm"),
            (digest.clone(), "whpx"),
            (digest.clone(), "hvf"),
        ] {
            let error = verify_assets(&stage, &pin, accelerator).unwrap_err();
            assert!(error.to_string().contains("VM inventory is invalid"));
        }
    }

    #[test]
    fn corrupt_asset_and_manifest_fail_the_outer_subtree_seal() {
        for filename in ["image", crate::packvm_bundle::MANIFEST] {
            let tree = TestTree::new("corrupt");
            let stage = tree.0.join("stage");
            let (root, digest) = write_vm_fixture(&stage, "kvm");
            fs::write(root.join(filename), b"tampered").unwrap();
            let error = verify_assets(&stage, &digest, "kvm").unwrap_err();
            assert!(error.to_string().contains("VM subtree seal is invalid"));
        }
    }

    #[test]
    fn additional_asset_fails_the_outer_subtree_seal() {
        let tree = TestTree::new("unsealed-addition");
        let stage = tree.0.join("stage");
        let (root, digest) = write_vm_fixture(&stage, "kvm");
        fs::write(root.join("rogue.dll"), b"unlisted code").unwrap();
        let error = verify_assets(&stage, &digest, "kvm").unwrap_err();
        assert!(error.to_string().contains("VM subtree seal is invalid"));
    }

    #[test]
    fn outer_seal_cannot_authorize_assets_missing_from_the_pinned_vm_inventory() {
        let tree = TestTree::new("inventory-addition");
        let stage = tree.0.join("stage");
        let (root, digest) = write_vm_fixture(&stage, "kvm");
        fs::write(root.join("rogue.dll"), b"unlisted code").unwrap();
        write_vm_outer_seal(&stage);
        crate::runtime_resource_integrity::verify_subtree(&stage, crate::packvm_bundle::DIRECTORY)
            .unwrap();
        let error = verify_assets(&stage, &digest, "kvm").unwrap_err();
        assert!(error.to_string().contains("VM inventory is invalid"));
    }

    #[test]
    #[cfg(unix)]
    fn symlinked_asset_or_asset_directory_never_becomes_development_authority() {
        let tree = TestTree::new("symlink");
        let stage = tree.0.join("stage");
        let (root, digest) = write_vm_fixture(&stage, "kvm");
        fs::remove_file(root.join("image")).unwrap();
        std::os::unix::fs::symlink(root.join("qemu"), root.join("image")).unwrap();
        assert!(verify_assets(&stage, &digest, "kvm").is_err());
        let unbound_stage = tree.0.join("unbound-stage");
        fs::create_dir(&unbound_stage).unwrap();
        std::os::unix::fs::symlink(&root, unbound_stage.join(crate::packvm_bundle::DIRECTORY))
            .unwrap();
        assert!(verify_assets(&unbound_stage, "", "kvm").is_err());
    }

    #[test]
    fn unbound_environment_explicitly_removes_both_inherited_overrides() {
        let mut command = Command::new("unused-test-program");
        command
            .env(ROOT_ENV, "/untrusted/runtime/bundle")
            .env(DIGEST_ENV, format!("sha256:{}", "f".repeat(64)));
        configure_environment(&mut command, None, None);
        assert_eq!(environment_change(&command, ROOT_ENV), Some(None));
        assert_eq!(environment_change(&command, DIGEST_ENV), Some(None));
    }

    #[test]
    fn portable_binding_overrides_ambient_values_and_takes_priority_over_macos() {
        let tree = TestTree::new("environment-portable");
        let stage = tree.0.join("stage");
        let (root, digest) = write_vm_fixture(&stage, "kvm");
        let binding = verify_assets(&stage, &digest, "kvm").unwrap().unwrap();
        let macos = tree.0.join("Ignored Developer.app");
        let mut command = Command::new("unused-test-program");
        command
            .env(ROOT_ENV, "/untrusted/runtime/bundle")
            .env(DIGEST_ENV, "untrusted digest");
        configure_environment(&mut command, Some(&binding), Some(&macos));
        assert_eq!(
            environment_change(&command, ROOT_ENV),
            Some(Some(root.into_os_string())),
        );
        assert_eq!(
            environment_change(&command, DIGEST_ENV),
            Some(Some(OsString::from(format!("sha256:{digest}")))),
        );
    }

    #[test]
    fn macos_fallback_keeps_only_the_selected_bundle_root() {
        let tree = TestTree::new("environment-macos");
        let macos = tree.0.join("Selected Developer.app");
        let mut command = Command::new("unused-test-program");
        command
            .env(ROOT_ENV, "/untrusted/runtime/bundle")
            .env(DIGEST_ENV, "untrusted digest");
        configure_environment(&mut command, None, Some(&macos));
        assert_eq!(
            environment_change(&command, ROOT_ENV),
            Some(Some(macos.into_os_string())),
        );
        assert_eq!(environment_change(&command, DIGEST_ENV), Some(None));
    }

    #[test]
    fn reconfiguring_an_unbound_command_cannot_retain_a_previous_binding() {
        let mut command = Command::new("unused-test-program");
        let binding = (
            PathBuf::from("/previous-build/packvm-qemu"),
            format!("sha256:{}", "1".repeat(64)),
        );
        configure_environment(&mut command, Some(&binding), None);
        configure_environment(&mut command, None, None);
        assert_eq!(environment_change(&command, ROOT_ENV), Some(None));
        assert_eq!(environment_change(&command, DIGEST_ENV), Some(None));
    }
}
