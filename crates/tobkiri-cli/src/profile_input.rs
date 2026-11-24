//! Resolve local Pack locators at the CLI file boundary, before Core computes
//! the approved and stored Profile revision. Stored pins never depend on CWD.

use super::{bounded_read, canonical_json};
use anyhow::{Context, Result, bail};
use std::path::{Path, PathBuf};
use tobkiri_core::profile::{parse_profile, parse_published};

pub(super) fn definition(path: &Path) -> Result<Vec<u8>> {
    let bytes = bounded_read(path, tobkiri_core::MAX_BYTES)?;
    match parse_profile(&bytes) {
        Ok(mut document) => {
            resolve_sources(document.packs.iter_mut().map(|pin| &mut pin.source), path)?;
            canonical_json(&document)
        }
        Err(profile_error) => {
            let mut document = parse_published(&bytes).map_err(|_| {
                anyhow::Error::new(profile_error)
                    .context("parse Profile definition; published Profile fallback also failed")
            })?;
            resolve_sources(document.packs.iter_mut().map(|pin| &mut pin.source), path)?;
            canonical_json(&document)
        }
    }
}

pub(super) fn published(path: &Path) -> Result<Vec<u8>> {
    published_bytes(&bounded_read(path, tobkiri_core::MAX_BYTES)?, path)
}

fn published_bytes(bytes: &[u8], path: &Path) -> Result<Vec<u8>> {
    let mut document = parse_published(bytes)?;
    resolve_sources(document.packs.iter_mut().map(|pin| &mut pin.source), path)?;
    canonical_json(&document)
}

fn resolve_sources<'a>(
    sources: impl Iterator<Item = &'a mut String>,
    definition: &Path,
) -> Result<()> {
    let origin = std::fs::canonicalize(definition)
        .with_context(|| format!("resolve Profile file {}", definition.display()))?;
    let directory = origin
        .parent()
        .context("Profile file has no parent directory")?;
    for source in sources {
        *source = resolve_source(source, directory)?;
    }
    Ok(())
}

fn resolve_source(source: &str, directory: &Path) -> Result<String> {
    // Match Core's local locator contract. These are path prefixes, not URLs:
    // do not decode escapes or turn an unsupported scheme into a local path.
    if source.contains("://") {
        bail!("Pack source must resolve to a verified local directory or .tpk artifact");
    }
    let (prefix, local) = if let Some(local) = source.strip_prefix("file:") {
        ("file:", local)
    } else if let Some(local) = source.strip_prefix("path:") {
        ("path:", local)
    } else {
        ("", source)
    };
    if local.is_empty() || local.starts_with("bundled:") {
        bail!("Pack source must resolve to a verified local directory or .tpk artifact");
    }
    let path = PathBuf::from(local);
    let authority = local
        .as_bytes()
        .get(..2)
        .is_some_and(|prefix| prefix.iter().all(|byte| matches!(*byte, b'/' | b'\\')));
    // Windows canonicalize returns verbatim drive paths such as \\?\C:\pack.
    // Preserve those local drive paths, while rejecting UNC/device authorities.
    #[cfg(windows)]
    let local_drive = local.starts_with(r"\\?\")
        && path.is_absolute()
        && matches!(
            path.components().next(),
            Some(std::path::Component::Prefix(prefix))
                if matches!(prefix.kind(), std::path::Prefix::VerbatimDisk(_))
        );
    #[cfg(not(windows))]
    let local_drive = false;
    if authority && !local_drive {
        bail!("Pack source must resolve to a verified local directory or .tpk artifact");
    }
    let path = if path.is_absolute() {
        path
    } else {
        directory.join(path)
    };
    let resolved = std::fs::canonicalize(&path)
        .with_context(|| format!("resolve local Pack source {}", path.display()))?;
    stored_source(prefix, &resolved)
}

fn stored_source(prefix: &str, resolved: &Path) -> Result<String> {
    if !resolved.is_absolute() {
        bail!("resolved Pack source path must be absolute for a stored locator");
    }
    let resolved = resolved
        .to_str()
        .context("resolved Pack source path must be UTF-8 for an absolute stored locator")?;
    Ok(format!("{prefix}{resolved}"))
}

#[cfg(test)]
#[path = "profile_input_tests.rs"]
mod tests;
