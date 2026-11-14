//! Stage the ordinary Setup Wasm from a separately pinned build receipt.

use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use std::fs::{self, OpenOptions};
use std::io::{self, Read, Write};
use std::path::Path;

#[path = "src/setup_component_path.rs"]
mod component;

pub const SOURCE_ENV: &str = "TOBKIRI_SETUP_COMPONENT_SOURCE";
pub const RECEIPT_ENV: &str = "TOBKIRI_SETUP_COMPONENT_RECEIPT";
pub const PIN_ENV: &str = "TOBKIRI_SETUP_COMPONENT_RECEIPT_SHA256";
pub const GENERATED: &str = "setup_component.rs";
const RECEIPT_LIMIT: u64 = 64 * 1024;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Receipt {
    format: String,
    component: Component,
    source_index_sha256: String,
    modules: Vec<Module>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Component {
    bytes: u64,
    sha256: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Module {
    module: String,
    relative_file: String,
}

fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn valid_digest(digest: &str) -> bool {
    digest.len() == 64
        && digest
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn physical_input(path: &Path, limit: u64) -> io::Result<impl Read> {
    if !path.is_absolute() || path.canonicalize()? != path {
        return Err(invalid(
            "Setup component input must be a physical absolute path",
        ));
    }
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC);
    }
    let file = options.open(path)?;
    let metadata = file.metadata()?;
    if !metadata.is_file() || !(1..=limit).contains(&metadata.len()) {
        return Err(invalid(
            "Setup component input is not a bounded regular file",
        ));
    }
    Ok(file.take(limit + 1))
}

fn valid_module(row: &Module) -> bool {
    let name = row.module.split('.').all(|part| {
        let mut bytes = part.bytes();
        bytes
            .next()
            .is_some_and(|b| b == b'_' || b.is_ascii_alphabetic())
            && bytes.all(|b| b == b'_' || b.is_ascii_alphanumeric())
    });
    let path = row.relative_file.ends_with(".py")
        && row.relative_file.split('/').all(|part| {
            !part.is_empty()
                && part != "."
                && part != ".."
                && part
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(&b))
        });
    name && path
}

pub fn stage(
    source: &Path,
    receipt: &Path,
    expected_receipt: &str,
    staged_root: &Path,
    generated: &Path,
) -> io::Result<()> {
    let mut raw = Vec::new();
    physical_input(receipt, RECEIPT_LIMIT)?.read_to_end(&mut raw)?;
    if raw.len() as u64 > RECEIPT_LIMIT
        || !valid_digest(expected_receipt)
        || format!("{:x}", Sha256::digest(&raw)) != expected_receipt
    {
        return Err(invalid(
            "Setup component receipt differs from the expected digest",
        ));
    }
    let receipt: Receipt = serde_json::from_slice(&raw).map_err(io::Error::other)?;
    let mut modules = BTreeSet::new();
    if receipt.format != "io.tobkiri.launcher-setup-component.v1"
        || !valid_digest(&receipt.component.sha256)
        || !(1..=component::MAX_COMPONENT_BYTES).contains(&receipt.component.bytes)
        || !valid_digest(&receipt.source_index_sha256)
        || !(1..=128).contains(&receipt.modules.len())
        || receipt
            .modules
            .iter()
            .any(|row| !valid_module(row) || !modules.insert(&row.module))
    {
        return Err(invalid(
            "Setup component receipt has an invalid component or module index",
        ));
    }

    let mut input = physical_input(source, component::MAX_COMPONENT_BYTES)?;
    let mut digest = Sha256::new();
    let mut bytes = 0_u64;
    let mut buffer = [0_u8; 64 * 1024];
    let target = staged_root.join(component::RESOURCE);
    fs::create_dir(target.parent().expect("fixed component subdirectory"))?;
    let mut output = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(target)?;
    loop {
        let read = input.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        bytes += read as u64;
        if bytes > receipt.component.bytes {
            return Err(invalid("Setup Wasm exceeds the verified size"));
        }
        digest.update(&buffer[..read]);
        output.write_all(&buffer[..read])?;
    }
    if bytes != receipt.component.bytes
        || format!("{:x}", digest.finalize()) != receipt.component.sha256
    {
        return Err(invalid("Setup Wasm differs from the verified component"));
    }
    output.flush()?;
    let component_digest = format!("sha256:{}", receipt.component.sha256);
    let mut rust = format!(
        "static COMPONENT: Option<SetupComponent> = Some(SetupComponent {{ sha256: {:?}, index: &BootstrapModuleIndex {{ sha256: {:?}, modules: &[\n",
        component_digest, receipt.source_index_sha256,
    );
    for row in receipt.modules {
        use std::fmt::Write;
        writeln!(
            rust,
            "    BootstrapModule {{ module: {:?}, relative_file: {:?} }},",
            row.module, row.relative_file
        )
        .expect("writing to String");
    }
    rust.push_str("] } });\n");
    fs::write(generated, rust)
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;

    #[test]
    fn verified_bytes_and_index_are_staged_together() -> io::Result<()> {
        let root = std::env::temp_dir().canonicalize()?.join(format!(
            "tobkiri-setup-component-{:016x}",
            rand::random::<u64>(),
        ));
        fs::create_dir(&root)?;
        struct Cleanup<'a>(&'a Path);
        impl Drop for Cleanup<'_> {
            fn drop(&mut self) {
                let _ = fs::remove_dir_all(self.0);
            }
        }
        let _cleanup = Cleanup(&root);
        let source = root.join("source.wasm");
        let receipt = root.join("receipt.json");
        let generated = root.join(GENERATED);
        let wasm = b"opaque bounded fixture component";
        fs::write(&source, wasm)?;
        let mut document = serde_json::json!({
            "format": "io.tobkiri.launcher-setup-component.v1",
            "component": {"sha256": format!("{:x}", Sha256::digest(wasm)), "bytes": wasm.len()},
            "source_index_sha256": "a".repeat(64),
            "modules": [{"module": "a.b", "relative_file": "a/b.py"}],
        });
        let pin = |document: &serde_json::Value| {
            let raw = document.to_string();
            fs::write(&receipt, &raw)?;
            Ok::<_, io::Error>(format!("{:x}", Sha256::digest(raw.as_bytes())))
        };
        let expected = pin(&document)?;
        assert!(stage(&source, &receipt, &"0".repeat(64), &root, &generated).is_err());
        document["modules"][0]["relative_file"] = "../b.py".into();
        assert!(stage(&source, &receipt, &pin(&document)?, &root, &generated).is_err());
        document["modules"][0]["relative_file"] = "a/b.py".into();
        assert_eq!(pin(&document)?, expected);
        let linked = root.join("link.wasm");
        std::os::unix::fs::symlink(&source, &linked)?;
        assert!(stage(&linked, &receipt, &expected, &root, &generated).is_err());
        let changed = root.join("changed.wasm");
        fs::write(&changed, [b'x'; 31])?;
        let rejected = root.join("rejected");
        fs::create_dir(&rejected)?;
        assert!(stage(&changed, &receipt, &expected, &rejected, &generated).is_err());
        assert!(!generated.exists());
        stage(&source, &receipt, &expected, &root, &generated)?;
        assert_eq!(fs::read(root.join(component::RESOURCE))?, wasm);
        let rust = fs::read_to_string(&generated)?;
        assert!(rust.contains(document["component"]["sha256"].as_str().unwrap()));
        assert!(rust.contains(&"a".repeat(64)));
        assert!(rust.contains("module: \"a.b\", relative_file: \"a/b.py\""));
        Ok(())
    }
}
