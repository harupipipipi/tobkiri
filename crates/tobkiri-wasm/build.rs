//! Optionally embed an explicitly supplied, checksum-pinned private producer result.
//! No Cargo build downloads tools, compiles a native SQL fallback, or accepts a
//! runtime/guest-provided replacement module.
use sha2::{Digest, Sha256};
use std::{env, error::Error, fs, path::Path};

fn hash(path: &Path) -> Result<String, Box<dyn Error>> {
    Ok(format!("{:x}", Sha256::digest(fs::read(path)?)))
}
fn verify_equal(actual: &str, expected: &str, field: &str) -> Result<(), Box<dyn Error>> {
    if actual != expected {
        return Err(format!(
            "private SQLite producer {field} mismatch; rebuild the pinned producer"
        )
        .into());
    }
    Ok(())
}
fn main() -> Result<(), Box<dyn Error>> {
    if env::var_os("CARGO_FEATURE_PRIVATE_SQLITE_EXPERIMENT").is_none() {
        return Ok(());
    }
    println!("cargo:rerun-if-env-changed=TOBKIRI_SQL_PRODUCER_DIR");
    let source = Path::new(&env::var("CARGO_MANIFEST_DIR")?).join("../../tools/wasm-sqlite");
    let pin_path = source.join("locked-producer.json");
    println!("cargo:rerun-if-changed={}", pin_path.display());
    let pin: serde_json::Value = serde_json::from_slice(&fs::read(&pin_path)?)?;
    let directory = env::var_os("TOBKIRI_SQL_PRODUCER_DIR").ok_or(
        "private SQLite producer unavailable: set TOBKIRI_SQL_PRODUCER_DIR to the verified producer output directory; Cargo never downloads/builds a fallback",
    )?;
    let directory = Path::new(&directory);
    let module = directory.join("sqlite-core.wasm");
    let receipt_path = directory.join("provenance.json");
    println!("cargo:rerun-if-changed={}", module.display());
    println!("cargo:rerun-if-changed={}", receipt_path.display());
    let receipt: serde_json::Value = serde_json::from_slice(&fs::read(&receipt_path)?)?;
    // The original executed arm64 receipt remains byte-for-byte valid. Linux
    // may use only this separately pinned host profile and must still produce
    // the SAME approved module bytes. Its compiler execution is a CI check,
    // never something Cargo performs or a runtime guest can select.
    let tool_pin = match receipt.get("host_toolchain_profile") {
        None => pin.clone(),
        Some(profile) if profile.as_str() == Some("sdk34-x86_64-linux-v1") => {
            let profile_path = source.join("host-toolchain-sdk34-x86_64-linux.json");
            println!("cargo:rerun-if-changed={}", profile_path.display());
            let host: serde_json::Value = serde_json::from_slice(&fs::read(&profile_path)?)?;
            if receipt
                .get("host_sdk_tree_verified")
                .and_then(|value| value.as_bool())
                != Some(true)
            {
                return Err("Linux SDK tree provenance unavailable".into());
            }
            verify_equal(
                receipt["host_pin_sha256"]
                    .as_str()
                    .ok_or("missing Linux pin digest")?,
                &hash(&profile_path)?,
                "host_pin_sha256",
            )?;
            let entry = source.join("build-linux.py");
            println!("cargo:rerun-if-changed={}", entry.display());
            let entry_hash = hash(&entry)?;
            verify_equal(
                &entry_hash,
                host["producer_script_sha256"]
                    .as_str()
                    .ok_or("missing Linux entry pin")?,
                "Linux entry source",
            )?;
            verify_equal(
                &entry_hash,
                receipt["host_entry_sha256"]
                    .as_str()
                    .ok_or("missing Linux entry receipt")?,
                "Linux entry receipt",
            )?;
            let mut fields = host.clone();
            fields["sdk_archive_sha256"] = host["archive_sha256"].clone();
            fields
        }
        Some(_) => return Err("unknown private SQLite host toolchain profile".into()),
    };
    for field in [
        "sdk_archive_sha256",
        "compiler_sha256",
        "linker_sha256",
        "libc_sha256",
    ] {
        if tool_pin.get(field).is_none() || tool_pin.get(field) != receipt.get(field) {
            return Err(format!("private SQLite host toolchain mismatch: {field}").into());
        }
    }
    for field in [
        "sqlite_version",
        "sqlite_source_sha256",
        "sdk_version",
        "wrapper_sha256",
        "producer_sha256",
        "module_sha256",
        "module_bytes",
        "abi_version",
        "imports",
        "exports",
        "linear_memories",
        "tables",
        "stack_bytes",
        "sqlite_heap_capacity_bytes",
        "flags",
    ] {
        if pin.get(field).is_none() || pin.get(field) != receipt.get(field) {
            return Err(format!("private SQLite locked provenance mismatch: {field}").into());
        }
    }
    for (name, field) in [
        ("vendor/sqlite3.c", "sqlite_source_sha256"),
        ("private_sql.c", "wrapper_sha256"),
        ("build.py", "producer_sha256"),
    ] {
        let path = source.join(name);
        println!("cargo:rerun-if-changed={}", path.display());
        verify_equal(
            &hash(&path)?,
            pin[field].as_str().ok_or("invalid producer pin")?,
            field,
        )?;
    }
    let bytes = fs::read(&module)?;
    if bytes.len() as u64
        != pin["module_bytes"]
            .as_u64()
            .ok_or("invalid module size pin")?
    {
        return Err("private SQLite module size mismatch".into());
    }
    verify_equal(
        &format!("{:x}", Sha256::digest(&bytes)),
        pin["module_sha256"]
            .as_str()
            .ok_or("invalid module digest pin")?,
        "module_sha256",
    )?;
    if bytes.get(..8) != Some(b"\0asm\x01\0\0\0") {
        return Err("private SQLite producer is not a core Wasm1 module".into());
    }
    fs::write(
        Path::new(&env::var("OUT_DIR")?).join("sqlite-core.wasm"),
        bytes,
    )?;
    // Producer closure is proven separately. Embedding does not authorize the
    // runtime to instantiate an engine with incomplete VFS/capacity coverage.
    Ok(())
}
