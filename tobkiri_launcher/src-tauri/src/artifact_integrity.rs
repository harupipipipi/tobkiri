//! Canonical v1 presentation artifact digest and payload-size implementation.

use std::fs::{self, File};
use std::io::{self, Read};
use std::path::{Path, PathBuf};

use sha2::{Digest, Sha256};

fn invalid(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message.into())
}

fn hash_path(
    path: &Path,
    relative_parts: &[String],
    digest: &mut Sha256,
    size: &mut u64,
) -> io::Result<()> {
    let metadata = fs::symlink_metadata(path)?;
    if metadata.file_type().is_symlink() {
        return Err(invalid(format!(
            "artifact may not contain a symlink: {}",
            path.display()
        )));
    }
    if metadata.is_file() {
        digest.update(relative_parts.join("/").as_bytes());
        digest.update([0]);
        let mut file = File::open(path)?;
        let mut buffer = [0_u8; 64 * 1024];
        loop {
            let read = file.read(&mut buffer)?;
            if read == 0 {
                break;
            }
            digest.update(&buffer[..read]);
            *size = size
                .checked_add(read as u64)
                .ok_or_else(|| invalid("artifact payload size overflow"))?;
        }
        return Ok(());
    }
    if !metadata.is_dir() {
        return Err(invalid(format!(
            "artifact entry is not a file or directory: {}",
            path.display()
        )));
    }

    let mut children = fs::read_dir(path)?
        .collect::<Result<Vec<_>, io::Error>>()?
        .into_iter()
        .map(|entry| {
            let name = entry.file_name().into_string().map_err(|_| {
                invalid(format!(
                    "artifact filename is not valid UTF-8: {}",
                    entry.path().display()
                ))
            })?;
            Ok::<(String, PathBuf), io::Error>((name, entry.path()))
        })
        .collect::<Result<Vec<_>, io::Error>>()?;
    children.sort_by(|left, right| left.0.cmp(&right.0));
    for (name, child) in children {
        let mut child_relative = relative_parts.to_vec();
        child_relative.push(name);
        hash_path(&child, &child_relative, digest, size)?;
    }
    Ok(())
}

pub(crate) fn digest_and_size(path: &Path) -> io::Result<(String, u64)> {
    let mut digest = Sha256::new();
    let mut size = 0_u64;
    hash_path(path, &[], &mut digest, &mut size)?;
    Ok((format!("sha256:{:x}", digest.finalize()), size))
}

#[cfg(test)]
mod tests {
    use super::digest_and_size;
    use serde_json::Value;
    use std::fs;
    use std::path::{Path, PathBuf};
    use std::time::{SystemTime, UNIX_EPOCH};

    const VECTORS: &str = include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../scripts/tests/fixtures/artifact_integrity_vectors.json"
    ));

    fn temporary_root() -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system clock should be after the Unix epoch")
            .as_nanos();
        std::env::temp_dir().join(format!(
            "tobkiri-artifact-integrity-{}-{nonce}",
            std::process::id()
        ))
    }

    fn materialize_fixture(root: &Path, fixture: &Value) -> PathBuf {
        let artifact = root.join(
            fixture["root_name"]
                .as_str()
                .expect("fixture root name should be text"),
        );
        for (relative, contents) in fixture["files"]
            .as_object()
            .expect("fixture files should be an object")
        {
            let path = if relative.is_empty() {
                artifact.clone()
            } else {
                artifact.join(relative)
            };
            if let Some(parent) = path.parent() {
                fs::create_dir_all(parent).expect("fixture parent should be creatable");
            }
            fs::write(
                path,
                contents
                    .as_str()
                    .expect("fixture contents should be text")
                    .as_bytes(),
            )
            .expect("fixture file should be writable");
        }
        artifact
    }

    #[test]
    fn canonical_vectors_cover_linux_file_windows_exe_and_macos_app() {
        let vectors: Value = serde_json::from_str(VECTORS).expect("vectors should be valid JSON");
        for (name, fixture) in vectors.as_object().expect("vectors should be an object") {
            let root = temporary_root();
            fs::create_dir_all(&root).expect("fixture root should be creatable");
            let artifact = materialize_fixture(&root, fixture);
            let (digest, size) = digest_and_size(&artifact).expect("fixture should hash");
            assert_eq!(digest, fixture["sha256"], "digest for {name}");
            assert_eq!(size, fixture["size"].as_u64().unwrap(), "size for {name}");
            fs::remove_dir_all(root).expect("fixture root should be removable");
        }
    }

    #[cfg(unix)]
    #[test]
    fn canonical_integrity_rejects_symlinked_content() {
        use std::os::unix::fs::symlink;

        let root = temporary_root();
        fs::create_dir_all(root.join("artifact")).expect("fixture root should be creatable");
        fs::write(root.join("outside"), b"outside").expect("outside file should be writable");
        symlink(root.join("outside"), root.join("artifact").join("link"))
            .expect("symlink should be creatable");
        assert!(digest_and_size(&root.join("artifact")).is_err());
        fs::remove_dir_all(root).expect("fixture root should be removable");
    }
}
