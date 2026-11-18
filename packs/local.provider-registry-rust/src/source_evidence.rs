//! Resolve bytes for the fixed Python Registry observations.

use anyhow::{Context, Result, ensure};
use sha2::{Digest as _, Sha256};
use std::{path::Path, process::Command};

const CAPTURE_COMMIT: &str = "98bb10ab67480d69fee041a08d1516cafb544ff3";

pub(super) fn verify_captured_bytes(
    repository: &Path,
    path: &str,
    current: &[u8],
    size: Option<u64>,
    digest: &str,
) -> Result<()> {
    let matches_capture = |bytes: &[u8]| {
        size.is_none_or(|size| size == bytes.len() as u64)
            && format!("{:x}", Sha256::digest(bytes)) == digest
    };
    if matches_capture(current) {
        return Ok(());
    }

    // Later migration edits may change the checkout. Retrieve the exact blob
    // from the capture commit and still verify its size and digest.
    let file = format!("{CAPTURE_COMMIT}:{path}");
    let output = Command::new("git")
        .current_dir(repository)
        .args(["--no-replace-objects", "cat-file", "blob", &file])
        .output()
        .with_context(|| format!("read captured Registry source {file}"))?;
    ensure!(
        output.status.success(),
        "captured Registry source is missing: {file}"
    );
    ensure!(
        matches_capture(&output.stdout),
        "captured Registry source differs: {file}"
    );
    Ok(())
}
