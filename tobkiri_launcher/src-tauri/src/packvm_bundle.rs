//! Build-pinned portable PackVM inventory, shared by packaging and startup.
//! This verifies content; the launched application remains the caller trust root.

use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, OpenOptions};
use std::io::{self, Read};
use std::path::{Component, Path};

use serde::Deserialize;
use sha2::{Digest, Sha256};

pub const DIRECTORY: &str = "packvm-qemu";
pub const MANIFEST: &str = "packvm-qemu-provisioning.v1.json";
pub const SOURCE_ENV: &str = "TOBKIRI_PACKVM_BUNDLE_ROOT";
pub const DIGEST_ENV: &str = "TOBKIRI_PACKVM_MANIFEST_SHA256";

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Asset {
    pub path: String,
    pub sha256: String,
    pub size_bytes: u64,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Inventory {
    schema: String,
    architecture: String,
    accelerator: String,
    pub files: BTreeMap<String, Asset>,
    image_source: String,
    pub qemu_dependencies: Vec<Asset>,
}

fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

pub fn valid_digest(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn safe_relative(value: &str) -> bool {
    !value.is_empty()
        && value.is_ascii()
        && !value
            .chars()
            .any(|character| matches!(character, '\\' | ':' | '\0'))
        && !value
            .split('/')
            .any(|part| part.is_empty() || part == "." || part == "..")
        && Path::new(value)
            .components()
            .all(|part| matches!(part, Component::Normal(_)))
}

fn checked_metadata(path: &Path) -> io::Result<fs::Metadata> {
    let metadata = fs::symlink_metadata(path)?;
    if metadata.file_type().is_symlink() {
        return Err(invalid("PackVM assets must not contain symlinks"));
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        if metadata.file_attributes() & 0x400 != 0 {
            return Err(invalid("PackVM assets must not contain reparse points"));
        }
    }
    Ok(metadata)
}

pub fn hash_regular(path: &Path) -> io::Result<(String, u64)> {
    let metadata = checked_metadata(path)?;
    if !metadata.is_file() {
        return Err(invalid("PackVM asset is not a regular file"));
    }
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC);
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        // Share read only: exclude concurrent writers and replacement while hashing.
        options.share_mode(1).custom_flags(0x00200000);
    }
    let mut file = options.open(path)?;
    let before = file.metadata()?;
    if !before.is_file() {
        return Err(invalid("PackVM opened asset is not a regular file"));
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if before.nlink() != 1 || before.dev() != metadata.dev() || before.ino() != metadata.ino() {
            return Err(invalid("PackVM asset identity or link count changed"));
        }
    }
    #[cfg(windows)]
    {
        use std::mem::MaybeUninit;
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::Storage::FileSystem::{
            GetFileInformationByHandle, BY_HANDLE_FILE_INFORMATION,
        };
        let mut information = MaybeUninit::<BY_HANDLE_FILE_INFORMATION>::zeroed();
        if unsafe { GetFileInformationByHandle(file.as_raw_handle(), information.as_mut_ptr()) }
            == 0
        {
            return Err(io::Error::last_os_error());
        }
        let information = unsafe { information.assume_init() };
        if information.nNumberOfLinks != 1 || information.dwFileAttributes & 0x400 != 0 {
            return Err(invalid("PackVM asset link or reparse metadata rejected"));
        }
    }
    let mut digest = Sha256::new();
    let mut size = 0_u64;
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let count = file.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        size = size
            .checked_add(count as u64)
            .ok_or_else(|| invalid("asset size overflow"))?;
        if size > before.len() {
            return Err(invalid("PackVM asset grew during verification"));
        }
        digest.update(&buffer[..count]);
    }
    let after = file.metadata()?;
    if size != before.len()
        || before.len() != after.len()
        || before.modified()? != after.modified()?
    {
        return Err(invalid("PackVM asset changed during verification"));
    }
    Ok((format!("{:x}", digest.finalize()), size))
}

fn collect(root: &Path, directory: &Path, paths: &mut BTreeSet<String>) -> io::Result<()> {
    if !checked_metadata(directory)?.is_dir() {
        return Err(invalid("PackVM inventory directory is invalid"));
    }
    for entry in fs::read_dir(directory)? {
        let path = entry?.path();
        let metadata = checked_metadata(&path)?;
        if metadata.is_dir() {
            collect(root, &path, paths)?;
        } else if metadata.is_file() {
            let relative = path
                .strip_prefix(root)
                .map_err(|_| invalid("PackVM path escaped"))?;
            let relative = relative
                .to_str()
                .ok_or_else(|| invalid("non-UTF8 PackVM path"))?
                .replace('\\', "/");
            if !safe_relative(&relative) || !paths.insert(relative) {
                return Err(invalid("PackVM inventory path is ambiguous"));
            }
        } else {
            return Err(invalid("unsupported PackVM filesystem entry"));
        }
    }
    Ok(())
}

pub fn verify(root: &Path, expected: &str, accelerator: &str) -> io::Result<Inventory> {
    if !valid_digest(expected) || !matches!(accelerator, "kvm" | "whpx") {
        return Err(invalid("PackVM build binding is missing or invalid"));
    }
    if !root.is_absolute() || root.canonicalize()? != root || !checked_metadata(root)?.is_dir() {
        return Err(invalid("PackVM bundle root is not canonical"));
    }
    let manifest = root.join(MANIFEST);
    let (digest, size) = hash_regular(&manifest)?;
    if digest != expected || size > 2 * 1024 * 1024 {
        return Err(invalid(
            "PackVM manifest differs from the build-pinned identity",
        ));
    }
    let bytes = fs::read(&manifest)?;
    if format!("{:x}", Sha256::digest(&bytes)) != expected {
        return Err(invalid("PackVM manifest changed before parsing"));
    }
    let inventory: Inventory =
        serde_json::from_slice(&bytes).map_err(|_| invalid("malformed PackVM manifest"))?;
    let required = [
        "agent",
        "bubblewrap",
        "bubblewrap_descriptor",
        "config",
        "firmware_code",
        "firmware_vars",
        "image",
        "qemu",
        "service",
    ];
    if inventory.schema != "io.tobkiri.packvm-qemu-provisioning.v1"
        || inventory.architecture != "amd64"
        || inventory.accelerator != accelerator
        || inventory
            .files
            .keys()
            .map(String::as_str)
            .collect::<Vec<_>>()
            != required
        || !inventory.image_source.starts_with("https://")
        || (accelerator == "kvm" && !inventory.qemu_dependencies.is_empty())
    {
        return Err(invalid("unsupported or incomplete PackVM inventory"));
    }
    let mut expected_paths = BTreeSet::from([MANIFEST.to_owned()]);
    let mut ambiguity = BTreeSet::from([MANIFEST.to_ascii_lowercase()]);
    let mut actual_paths = BTreeSet::new();
    collect(root, root, &mut actual_paths)?;
    for asset in inventory.files.values().chain(&inventory.qemu_dependencies) {
        let Some(expected_sha) = asset.sha256.strip_prefix("sha256:") else {
            return Err(invalid("PackVM digest prefix missing"));
        };
        if !safe_relative(&asset.path)
            || !valid_digest(expected_sha)
            || asset.size_bytes == 0
            || !expected_paths.insert(asset.path.clone())
            || !ambiguity.insert(asset.path.to_ascii_lowercase())
        {
            return Err(invalid("PackVM asset record is unsafe or duplicated"));
        }
        let (actual, size) = hash_regular(&root.join(&asset.path))?;
        if actual != expected_sha || size != asset.size_bytes {
            return Err(invalid("PackVM asset integrity failed"));
        }
    }
    if actual_paths != expected_paths {
        return Err(invalid("PackVM bundle contains missing or unlisted files"));
    }
    Ok(inventory)
}

pub fn stage(
    source: &Path,
    destination: &Path,
    expected: &str,
    accelerator: &str,
) -> io::Result<()> {
    let inventory = verify(source, expected, accelerator)?;
    if destination.exists() {
        return Err(invalid("PackVM stage must be new"));
    }
    fs::create_dir(destination)?;
    for asset in inventory.files.values().chain(&inventory.qemu_dependencies) {
        let path = destination.join(&asset.path);
        fs::create_dir_all(
            path.parent()
                .ok_or_else(|| invalid("PackVM parent missing"))?,
        )?;
        fs::copy(source.join(&asset.path), &path)?;
    }
    fs::copy(source.join(MANIFEST), destination.join(MANIFEST))?;
    verify(destination, expected, accelerator)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::time::{SystemTime, UNIX_EPOCH};

    fn fixture_directory(timestamp: u128) -> std::path::PathBuf {
        static NEXT_FIXTURE: AtomicU64 = AtomicU64::new(0);
        // Wall-clock precision is not a uniqueness guarantee on every host.
        // Reserve a new directory atomically; never adopt an existing fixture.
        for _ in 0..64 {
            let sequence = NEXT_FIXTURE.fetch_add(1, Ordering::Relaxed);
            let root = std::env::temp_dir().join(format!(
                "tobkiri-packvm-inventory-{}-{timestamp}-{sequence}",
                std::process::id(),
            ));
            match fs::create_dir(&root) {
                Ok(()) => return root,
                Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
                Err(error) => panic!("failed to reserve PackVM fixture: {error}"),
            }
        }
        panic!("could not reserve a unique PackVM fixture directory");
    }

    fn fixture() -> (std::path::PathBuf, String) {
        let root = fixture_directory(
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos(),
        );
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
            let content = format!("fixture {slot}");
            fs::write(root.join(slot), content.as_bytes()).unwrap();
            files.insert(
                slot.to_owned(),
                serde_json::json!({"path": slot,
                "sha256": format!("sha256:{:x}", Sha256::digest(content.as_bytes())),
                "size_bytes": content.len()}),
            );
        }
        let value = serde_json::json!({"schema": "io.tobkiri.packvm-qemu-provisioning.v1",
            "architecture": "amd64", "accelerator": "kvm", "files": files,
            "image_source": "https://example.com/pinned.raw", "qemu_dependencies": []});
        let bytes = serde_json::to_vec(&value).unwrap();
        let digest = format!("{:x}", Sha256::digest(&bytes));
        fs::write(root.join(MANIFEST), bytes).unwrap();
        (root.canonicalize().unwrap(), digest)
    }

    #[test]
    fn parallel_fixtures_do_not_depend_on_clock_resolution() {
        let timestamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let workers: Vec<_> = (0..32)
            .map(|_| std::thread::spawn(move || fixture_directory(timestamp)))
            .collect();
        let roots: Vec<_> = workers
            .into_iter()
            .map(|worker| worker.join().unwrap())
            .collect();
        let unique: BTreeSet<_> = roots.iter().collect();
        assert_eq!(unique.len(), roots.len());
        for root in roots {
            fs::remove_dir(root).unwrap();
        }
    }

    #[test]
    fn verifies_and_stages_complete_inventory() {
        let (root, digest) = fixture();
        let destination = root.with_extension("staged");
        verify(&root, &digest, "kvm").unwrap();
        stage(&root, &destination, &digest, "kvm").unwrap();
        verify(&destination, &digest, "kvm").unwrap();
        fs::remove_dir_all(destination).unwrap();
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn rejects_wrong_pin_accelerator_and_tampered_asset() {
        let (root, digest) = fixture();
        assert!(verify(&root, &"0".repeat(64), "kvm").is_err());
        assert!(verify(&root, &digest, "whpx").is_err());
        fs::write(root.join("image"), b"substituted").unwrap();
        assert!(verify(&root, &digest, "kvm").is_err());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn rejects_unlisted_executable_or_library() {
        let (root, digest) = fixture();
        fs::write(root.join("rogue.dll"), b"not pinned").unwrap();
        assert!(verify(&root, &digest, "kvm").is_err());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    #[cfg(unix)]
    fn rejects_symlink_and_multiply_linked_assets() {
        let (root, digest) = fixture();
        let extra = root.with_extension("hardlink");
        fs::hard_link(root.join("qemu"), &extra).unwrap();
        assert!(verify(&root, &digest, "kvm").is_err());
        fs::remove_file(extra).unwrap();
        fs::remove_file(root.join("image")).unwrap();
        std::os::unix::fs::symlink(root.join("qemu"), root.join("image")).unwrap();
        assert!(verify(&root, &digest, "kvm").is_err());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn paths_and_build_identities_are_strict() {
        for path in [
            "../qemu", "/qemu", "a/../b", "a//b", "C:qemu", "a\\b", "./qemu", "a/",
        ] {
            assert!(!safe_relative(path), "{path}");
        }
        assert!(safe_relative("bin/qemu-system-x86_64.exe"));
        assert!(!valid_digest(""));
        assert!(!valid_digest(&"A".repeat(64)));
    }
}
