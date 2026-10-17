//! Unconnected source prototype; BUILD-02 remains closed in original build.rs.
//! Never starts Cargo, compiles CPython, or downloads.
//! All staging is completed sequentially before the outer Cargo invocation.
use anyhow::{Context, Result, bail, ensure};
use sha2::{Digest, Sha256};
use std::{
    env, fs,
    io::Read,
    path::{Path, PathBuf},
    process::Command,
};

fn digest(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn bounded(path: &Path, maximum: usize) -> Result<Vec<u8>> {
    // Physical locators are operator-only and are not hashed into the semantic
    // recipe/receipt or passed to the guest. No symlink leaf is admitted.
    ensure!(
        path.is_absolute() && !fs::symlink_metadata(path)?.file_type().is_symlink(),
        "explicit physical stage file required"
    );
    let mut file = fs::File::open(path)?;
    let metadata = file.metadata()?;
    ensure!(
        metadata.is_file() && metadata.len() <= maximum as u64,
        "stage file type/size"
    );
    let mut bytes = Vec::new();
    (&mut file)
        .take(maximum as u64 + 1)
        .read_to_end(&mut bytes)?;
    ensure!(
        bytes.len() <= maximum && file.metadata()?.len() == metadata.len(),
        "stage file changed"
    );
    Ok(bytes)
}

pub fn install(output: &Path) -> Result<()> {
    let recipe_bytes = include_bytes!("producer-recipe-v2.json");
    let recipe: serde_json::Value = serde_json::from_slice(recipe_bytes)?;
    // In this source checkpoint full source/lock/OS-native-closure pins are
    // deliberately incomplete. It cannot launch even the native build tool.
    ensure!(
        recipe["status"] == "verified" && recipe["system_native_closure_verified"] == true,
        "Unavailable: original producer full staging pins/proofs pending (BUILD-01)"
    );
    let receipt = PathBuf::from(
        env::var_os("TOBKIRI_PRODUCER_STAGE_RECEIPT").context("explicit stage receipt required")?,
    );
    let expected = env::var("TOBKIRI_PRODUCER_STAGE_SHA256")
        .context("explicit semantic receipt digest required")?;
    let receipt_bytes = bounded(&receipt, 4 * 1024 * 1024)?;
    ensure!(
        digest(&receipt_bytes) == expected,
        "actual stage receipt digest differs"
    );
    let locators = PathBuf::from(
        env::var_os("TOBKIRI_PRODUCER_LOCATORS")
            .context("explicit private role locators required")?,
    );
    let locator_bytes = bounded(&locators, 64 * 1024)?;
    let map: serde_json::Value = serde_json::from_slice(&locator_bytes)?;
    let native = PathBuf::from(
        map["native-cpython"]
            .as_str()
            .context("native CPython build tool locator required")?,
    );
    let executable = native.join(
        recipe["native_python"]["path"]
            .as_str()
            .context("native tool path pin required")?,
    );
    let tool = bounded(&executable, 128 * 1024 * 1024)?;
    ensure!(
        digest(&tool)
            == recipe["native_python"]["sha256"]
                .as_str()
                .context("native tool digest pin required")?,
        "native build tool differs"
    );
    let producer =
        PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").context("producer source root required")?);
    let script = producer.join("tools/stage_verify.py");
    ensure!(
        digest(&bounded(&script, 256 * 1024)?)
            == recipe["stage_verifier_sha256"]
                .as_str()
                .context("stage verifier pin required")?,
        "stage verifier source differs"
    );
    let recipe_path = producer.join("producer-recipe-v2.json");
    ensure!(
        bounded(&recipe_path, 256 * 1024)? == recipe_bytes,
        "recipe source differs from compiled recipe"
    );
    let status = Command::new(executable)
        .args(["-E", "-s", "-B", "-S"])
        .arg(script)
        .arg("--recipe")
        .arg(recipe_path)
        .arg("--receipt")
        .arg(&receipt)
        .arg("--receipt-sha256")
        .arg(expected)
        .arg("--locators")
        .arg(&locators)
        .arg("--output")
        .arg(output)
        .env_clear()
        .env("PYTHONHASHSEED", "0")
        .env("TZ", "UTC")
        .env("LANG", "C.UTF-8")
        .env("LC_ALL", "C.UTF-8")
        .status()
        .context("original native CPython stage verifier failed")?;
    if !status.success() {
        bail!("stage verification failed; no compiler/runtime fallback");
    }
    Ok(())
}
