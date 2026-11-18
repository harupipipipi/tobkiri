//! Stage the three Registry Pack archives from a pinned CI producer receipt.

use std::ffi::OsString;
use std::io;
use std::path::PathBuf;

use super::valid_raw_sha256;

pub(super) const SOURCE_ENV: &str = "TOBKIRI_REGISTRY_PACK_SOURCE_DIR";
pub(super) const DIGEST_ENV: &str = "TOBKIRI_REGISTRY_PACK_RECEIPT_SHA256";
pub(super) const DIRECTORY: &str = "native-registry-packs";

#[cfg_attr(not(unix), allow(dead_code))]
pub(super) struct Input {
    source: PathBuf,
    receipt_sha256: String,
}

impl Input {
    pub(super) fn configure(
        source: Option<OsString>,
        digest: Option<OsString>,
        allowed: bool,
    ) -> io::Result<Option<Self>> {
        let (source, digest) = match (source, digest) {
            (None, None) => return Ok(None),
            (Some(source), Some(digest)) => (PathBuf::from(source), digest),
            _ => {
                return Err(invalid(format!(
                    "{SOURCE_ENV} and {DIGEST_ENV} must be paired"
                )))
            }
        };
        if !allowed {
            return Err(invalid(
                "Registry Packs require a macOS native-first-setup CI/E2E release build",
            ));
        }
        let receipt_sha256 = digest
            .into_string()
            .ok()
            .filter(|digest| valid_raw_sha256(digest))
            .ok_or_else(|| invalid(format!("{DIGEST_ENV} must be a lowercase SHA-256")))?;
        if !source.is_absolute()
            || !source
                .to_str()
                .is_some_and(|path| !path.contains(['\n', '\r']))
        {
            return Err(invalid(format!(
                "{SOURCE_ENV} must be an absolute UTF-8 path"
            )));
        }
        Ok(Some(Self {
            source,
            receipt_sha256,
        }))
    }

    #[cfg(not(unix))]
    pub(super) fn stage(&self, _stage_root: &std::path::Path) -> io::Result<()> {
        Err(invalid("Registry Pack staging requires a Unix build host"))
    }
}

fn invalid(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message.into())
}

#[cfg(unix)]
mod unix {
    use super::{invalid, Input, DIRECTORY};
    use serde::Deserialize;
    use sha2::{Digest, Sha256};
    use std::ffi::{CString, OsString};
    use std::fs::{self, File, OpenOptions};
    use std::io::{self, Read, Write};
    use std::os::fd::{AsRawFd, FromRawFd};
    use std::os::unix::fs::MetadataExt;
    use std::path::Path;

    use super::super::{
        core_directory_entries, open_staged_directory, raw_byte_digest, valid_raw_sha256,
    };

    const RECEIPT: &str = "receipt.json";
    const RECEIPT_FORMAT: &str = "tobkiri.registry-packs/1";
    const RECEIPT_LIMIT: u64 = 16 * 1024;
    // The Pack authoring CLI's .tpk size ceiling.
    const ARCHIVE_LIMIT: u64 = 512 * 1024 * 1024;
    const PACK_IDS: [&str; 3] = [
        "local.provider-registry",
        "local.model-registry",
        "local.saved-conversation",
    ];

    #[derive(Deserialize)]
    #[serde(deny_unknown_fields)]
    struct Receipt {
        format: String,
        packs: [Pack; PACK_IDS.len()],
    }

    #[derive(Deserialize)]
    #[serde(deny_unknown_fields)]
    struct Pack {
        id: String,
        version: String,
        archive: String,
        archive_bytes: u64,
        archive_sha256: String,
        manifest_sha256: String,
    }

    fn tagged_sha256(value: &str) -> bool {
        value.strip_prefix("sha256:").is_some_and(valid_raw_sha256)
    }

    fn valid_version(version: &str) -> bool {
        // Match tobkiri-manifest's three-part numeric Pack version grammar.
        let parts: Vec<_> = version.split('.').collect();
        parts.len() == 3
            && parts.iter().all(|part| {
                !part.is_empty()
                    && !(part.len() > 1 && part.starts_with('0'))
                    && part.bytes().all(|byte| byte.is_ascii_digit())
            })
    }

    fn parse_receipt(raw: &[u8]) -> io::Result<Receipt> {
        let document: serde_json::Value = serde_json::from_slice(raw)?;
        // Derived structs also accept positional arrays. Require named objects.
        if !document
            .get("packs")
            .and_then(serde_json::Value::as_array)
            .is_some_and(|packs| packs.iter().all(serde_json::Value::is_object))
        {
            return Err(invalid(
                "Registry Pack receipt must describe named Pack records",
            ));
        }
        let receipt: Receipt = serde_json::from_slice(raw)?;
        if receipt.format != RECEIPT_FORMAT {
            return Err(invalid("Registry Pack receipt format is invalid"));
        }
        for (id, pack) in PACK_IDS.into_iter().zip(&receipt.packs) {
            if pack.id != id
                || pack.archive != format!("{id}.tpk")
                || !valid_version(&pack.version)
                || !(1..=ARCHIVE_LIMIT).contains(&pack.archive_bytes)
                || !tagged_sha256(&pack.archive_sha256)
                || !tagged_sha256(&pack.manifest_sha256)
            {
                return Err(invalid(format!(
                    "Registry Pack receipt record for {id} is invalid"
                )));
            }
        }
        Ok(receipt)
    }

    fn open_input(
        directory: &File,
        name: &str,
        sizes: std::ops::RangeInclusive<u64>,
    ) -> io::Result<File> {
        let name =
            CString::new(name).map_err(|_| invalid("Registry Pack filename contains NUL"))?;
        let fd = unsafe {
            libc::openat(
                directory.as_raw_fd(),
                name.as_ptr(),
                libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC,
            )
        };
        if fd == -1 {
            return Err(io::Error::last_os_error());
        }
        let file = unsafe { File::from_raw_fd(fd) };
        let metadata = file.metadata()?;
        if !metadata.is_file() || metadata.nlink() != 1 || !sizes.contains(&metadata.len()) {
            return Err(invalid(
                "Registry Pack input must be a bounded, singly linked regular file",
            ));
        }
        Ok(file)
    }

    fn create_output(path: &Path) -> io::Result<File> {
        OpenOptions::new().write(true).create_new(true).open(path)
    }

    fn copy_archive(directory: &File, target: &Path, pack: &Pack) -> io::Result<()> {
        let file = open_input(
            directory,
            &pack.archive,
            pack.archive_bytes..=pack.archive_bytes,
        )?;
        let mut input = file.take(pack.archive_bytes + 1);
        let mut output = create_output(&target.join(&pack.archive))?;
        let mut hash = Sha256::new();
        let mut total = 0;
        let mut buffer = [0; 64 * 1024];
        loop {
            let count = input.read(&mut buffer)?;
            if count == 0 {
                break;
            }
            total += count as u64;
            if total > pack.archive_bytes {
                break;
            }
            hash.update(&buffer[..count]);
            output.write_all(&buffer[..count])?;
        }
        if total != pack.archive_bytes
            || format!("sha256:{:x}", hash.finalize()) != pack.archive_sha256
        {
            return Err(invalid(format!(
                "Registry Pack archive differs from receipt: {}",
                pack.id
            )));
        }
        Ok(())
    }

    impl Input {
        pub(in super::super) fn stage(&self, staged_root: &Path) -> io::Result<()> {
            if self.source.canonicalize()? != self.source {
                return Err(invalid(
                    "Registry Pack source directory must be a physical path",
                ));
            }
            let directory = open_staged_directory(&self.source, "Registry Pack source directory")?;
            let mut raw = Vec::new();
            open_input(&directory, RECEIPT, 1..=RECEIPT_LIMIT)?
                .take(RECEIPT_LIMIT + 1)
                .read_to_end(&mut raw)?;
            if raw.len() as u64 > RECEIPT_LIMIT || raw_byte_digest(&raw) != self.receipt_sha256 {
                return Err(invalid(
                    "Registry Pack receipt differs from the expected digest",
                ));
            }
            let receipt = parse_receipt(&raw)?;
            let mut expected = Vec::from([OsString::from(RECEIPT)]);
            expected.extend(
                receipt
                    .packs
                    .iter()
                    .map(|pack| OsString::from(&pack.archive)),
            );
            expected.sort();
            if core_directory_entries(&directory)? != expected {
                return Err(invalid(
                    "Registry Pack source directory has missing or unexpected entries",
                ));
            }
            let target = staged_root.join(DIRECTORY);
            fs::create_dir(&target)?;
            let result = (|| {
                for pack in &receipt.packs {
                    copy_archive(&directory, &target, pack)?;
                }
                create_output(&target.join(RECEIPT))?.write_all(&raw)
            })();
            if let Err(error) = result {
                return match fs::remove_dir_all(&target) {
                    Ok(()) => Err(error),
                    Err(cleanup) => Err(io::Error::new(
                        error.kind(),
                        format!("{error}; Registry Pack staging cleanup failed: {cleanup}"),
                    )),
                };
            }
            println!("cargo:rerun-if-changed={}", self.source.display());
            Ok(())
        }
    }

    #[cfg(test)]
    mod tests {
        use super::super::super::{write_runtime_resource_manifest, RUNTIME_RESOURCE_MANIFEST};
        use super::*;
        use serde_json::{json, Value};
        use std::os::unix::fs::symlink;
        use std::path::PathBuf;

        struct Fixture {
            root: PathBuf,
            source: PathBuf,
            stage: PathBuf,
            document: Value,
        }

        impl Fixture {
            fn new() -> io::Result<Self> {
                let root = std::env::temp_dir().canonicalize()?.join(format!(
                    "tobkiri-registry-pack-{:016x}",
                    rand::random::<u64>()
                ));
                fs::create_dir(&root)?;
                let source = root.join("source");
                let stage = root.join("stage");
                fs::create_dir(&source)?;
                fs::create_dir(&stage)?;
                let mut packs = Vec::new();
                for id in PACK_IDS {
                    let archive = format!("{id}.tpk");
                    fs::write(source.join(&archive), id)?;
                    packs.push(json!({
                        "id": id,
                        "version": "0.1.0",
                        "archive": archive,
                        "archive_bytes": id.len(),
                        "archive_sha256": format!("sha256:{}", raw_byte_digest(id.as_bytes())),
                        "manifest_sha256": format!("sha256:{}", "1".repeat(64)),
                    }));
                }
                let document = json!({"format": RECEIPT_FORMAT, "packs": packs});
                Ok(Self {
                    root,
                    source,
                    stage,
                    document,
                })
            }

            fn write_receipt(&self, document: &Value) -> io::Result<Input> {
                let raw = serde_json::to_vec(document)?;
                fs::write(self.source.join(RECEIPT), &raw)?;
                Ok(Input {
                    source: self.source.clone(),
                    receipt_sha256: raw_byte_digest(&raw),
                })
            }
        }

        impl Drop for Fixture {
            fn drop(&mut self) {
                let _ = fs::remove_dir_all(&self.root);
            }
        }

        #[test]
        fn settings_are_optional_paired_and_restricted() -> io::Result<()> {
            let fixture = Fixture::new()?;
            let source = fixture.source.as_os_str().to_owned();
            let pin = OsString::from("a".repeat(64));
            assert!(Input::configure(None, None, false)?.is_none());
            assert!(Input::configure(Some(source.clone()), None, true).is_err());
            assert!(Input::configure(None, Some(pin.clone()), true).is_err());
            assert!(Input::configure(Some(source.clone()), Some(pin.clone()), false).is_err());
            assert!(
                Input::configure(Some(source.clone()), Some("A".repeat(64).into()), true).is_err()
            );
            assert!(Input::configure(Some(source), Some(pin), true)?.is_some());
            Ok(())
        }

        #[test]
        fn seals_only_the_pinned_receipt_and_three_archives() -> io::Result<()> {
            let fixture = Fixture::new()?;
            let input = fixture.write_receipt(&fixture.document)?;
            input.stage(&fixture.stage)?;
            let staged = fixture.stage.join(DIRECTORY);
            assert_eq!(fs::read_dir(&staged)?.count(), PACK_IDS.len() + 1);
            for id in PACK_IDS {
                assert_eq!(fs::read(staged.join(format!("{id}.tpk")))?, id.as_bytes());
            }
            assert_eq!(
                fs::read(staged.join(RECEIPT))?,
                fs::read(fixture.source.join(RECEIPT))?
            );
            write_runtime_resource_manifest(&fixture.stage)?;
            let seal: Value =
                serde_json::from_slice(&fs::read(fixture.stage.join(RUNTIME_RESOURCE_MANIFEST))?)?;
            let entries = seal["entries"].as_array().unwrap();
            assert_eq!(entries.len(), PACK_IDS.len() + 1);
            for entry in entries {
                let filename = entry["path"].as_str().unwrap();
                assert!(filename.starts_with(&format!("{DIRECTORY}/")));
                let bytes = fs::read(fixture.stage.join(filename))?;
                assert_eq!(entry["size"], bytes.len());
                assert_eq!(entry["sha256"], raw_byte_digest(&bytes));
            }
            Ok(())
        }

        #[test]
        fn rejects_receipt_archive_and_filesystem_substitutions() -> io::Result<()> {
            let fixture = Fixture::new()?;
            let document = &fixture.document;
            let input = fixture.write_receipt(document)?;
            let receipt_path = fixture.source.join(RECEIPT);
            let raw = fs::read(&receipt_path)?;
            fs::write(&receipt_path, [&raw[..], b"\n"].concat())?;
            assert!(input.stage(&fixture.stage).is_err());
            for (key, value) in [
                ("archive", json!("../local.provider-registry.tpk")),
                ("archive_bytes", json!(ARCHIVE_LIMIT + 1)),
                ("manifest_sha256", json!("f".repeat(64))),
                ("version", json!("01.0.0")),
            ] {
                let mut malformed = document.clone();
                malformed["packs"][0][key] = value;
                assert!(fixture
                    .write_receipt(&malformed)?
                    .stage(&fixture.stage)
                    .is_err());
            }
            let mut reordered = document.clone();
            reordered["packs"].as_array_mut().unwrap().swap(0, 1);
            assert!(fixture
                .write_receipt(&reordered)?
                .stage(&fixture.stage)
                .is_err());
            fixture.write_receipt(document)?;

            let archive = fixture.source.join(format!("{}.tpk", PACK_IDS[1]));
            let original = fs::read(&archive)?;
            fs::write(&archive, vec![0; original.len()])?;
            assert!(input.stage(&fixture.stage).is_err());
            assert!(!fixture.stage.join(DIRECTORY).exists());
            fs::remove_file(&archive)?;
            let external = fixture.root.join("original.tpk");
            fs::write(&external, &original)?;
            symlink(&external, &archive)?;
            assert!(input.stage(&fixture.stage).is_err());
            fs::remove_file(&archive)?;
            fs::hard_link(&external, &archive)?;
            assert!(input.stage(&fixture.stage).is_err());
            fs::remove_file(&archive)?;
            fs::rename(&external, &archive)?;

            let extra = fixture.source.join("extra");
            fs::write(&extra, b"unexpected")?;
            assert!(input.stage(&fixture.stage).is_err());
            fs::remove_file(extra)?;
            let linked = fixture.root.join("source-link");
            symlink(&fixture.source, &linked)?;
            let linked_input = Input {
                source: linked,
                receipt_sha256: input.receipt_sha256.clone(),
            };
            assert!(linked_input.stage(&fixture.stage).is_err());
            input.stage(&fixture.stage)?;
            Ok(())
        }
    }
}
