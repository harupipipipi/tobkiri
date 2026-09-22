//! Owner-only host contract for injecting scoped runtime values.
//!
//! Secrets are passed to the managed Python runtime through this file rather
//! than through a process environment variable.  The file is created below
//! the Launcher-owned user-data root with owner-only permissions and contains
//! only the values for the selected profile.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde_json::{Map, Value};

use crate::config::AppConfig;

pub(crate) const DEFAULT_PROFILE_ID: &str = "default-profile";
pub(crate) const CONTRACT_ENV: &str = "TOBKIRI_HOST_CONTRACT_PATH";

pub(crate) fn contract_path(config: &AppConfig) -> PathBuf {
    config.user_data_dir.join("host_contract.json")
}

/// Write the current host-bound values and return the contract path.
pub(crate) fn write_contract(
    config: &AppConfig,
    profile_id: &str,
    values: impl IntoIterator<Item = (&'static str, String)>,
) -> Result<PathBuf> {
    let normalized_profile = profile_id.trim();
    if normalized_profile.is_empty() {
        anyhow::bail!("host contract profile_id is required");
    }
    let path = contract_path(config);
    let parent = path
        .parent()
        .context("host contract path has no parent directory")?;
    fs::create_dir_all(parent).with_context(|| {
        format!(
            "failed to create host contract directory {}",
            parent.display()
        )
    })?;
    restrict_owner_only(parent)?;

    let existing: Option<Value> = fs::read(&path)
        .ok()
        .and_then(|raw| serde_json::from_slice::<Value>(&raw).ok());
    let mut merged_values = existing_values_for_profile(existing.as_ref(), normalized_profile);
    for (name, value) in values {
        if !value.trim().is_empty() {
            merged_values.insert(name.to_string(), value);
        }
    }
    let mut payload = Map::new();
    payload.insert(
        "schema_version".into(),
        Value::String("tobkiri.host-contract.v1".into()),
    );
    payload.insert(
        "profile_id".into(),
        Value::String(normalized_profile.to_string()),
    );
    payload.insert(
        "values".into(),
        serde_json::to_value(merged_values).context("failed to encode host contract values")?,
    );
    let body = serde_json::to_vec_pretty(&Value::Object(payload))?;
    let temporary = path.with_extension(format!("{}.tmp", std::process::id()));
    fs::write(&temporary, body)
        .with_context(|| format!("failed to write host contract {}", temporary.display()))?;
    restrict_owner_only_file(&temporary)?;
    fs::rename(&temporary, &path).with_context(|| {
        format!(
            "failed to publish host contract {} from {}",
            path.display(),
            temporary.display()
        )
    })?;
    restrict_owner_only_file(&path)?;
    Ok(path)
}

fn existing_values_for_profile(
    existing: Option<&Value>,
    profile_id: &str,
) -> BTreeMap<String, String> {
    existing
        .filter(|payload| payload.get("profile_id").and_then(Value::as_str) == Some(profile_id))
        .and_then(|payload| payload.get("values").cloned())
        .and_then(|raw| serde_json::from_value(raw).ok())
        .unwrap_or_default()
}

/// Read one contract value for Launcher-side verification without exposing the
/// entire document to logs or child process arguments.
pub(crate) fn read_value(config: &AppConfig, name: &str) -> Option<String> {
    let path = contract_path(config);
    let payload: Value = serde_json::from_slice(&fs::read(path).ok()?).ok()?;
    let profile_id = payload.get("profile_id")?.as_str()?.trim();
    if profile_id != DEFAULT_PROFILE_ID {
        return None;
    }
    payload
        .get("values")?
        .get(name)?
        .as_str()
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_owned)
}

#[cfg(unix)]
fn restrict_owner_only(path: &Path) -> Result<()> {
    use std::os::unix::fs::PermissionsExt;

    let metadata = fs::symlink_metadata(path)
        .with_context(|| format!("failed to inspect host contract path {}", path.display()))?;
    if metadata.file_type().is_symlink() {
        anyhow::bail!("refusing symlinked host contract path {}", path.display());
    }
    fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    Ok(())
}

#[cfg(unix)]
fn restrict_owner_only_file(path: &Path) -> Result<()> {
    use std::os::unix::fs::PermissionsExt;

    let metadata = fs::symlink_metadata(path)
        .with_context(|| format!("failed to inspect host contract file {}", path.display()))?;
    if metadata.file_type().is_symlink() {
        anyhow::bail!("refusing symlinked host contract file {}", path.display());
    }
    fs::set_permissions(path, fs::Permissions::from_mode(0o600))?;
    Ok(())
}

#[cfg(not(unix))]
fn restrict_owner_only_file(_path: &Path) -> Result<()> {
    Ok(())
}

#[cfg(not(unix))]
fn restrict_owner_only(_path: &Path) -> Result<()> {
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::existing_values_for_profile;
    use serde_json::json;

    #[test]
    fn profile_switch_does_not_reuse_existing_secret_values() {
        let existing = json!({
            "profile_id": "profile-a",
            "values": {"desktop_api_token": "profile-a-secret"}
        });

        assert_eq!(
            existing_values_for_profile(Some(&existing), "profile-a")
                .get("desktop_api_token")
                .map(String::as_str),
            Some("profile-a-secret")
        );
        assert!(existing_values_for_profile(Some(&existing), "profile-b").is_empty());
    }
}
