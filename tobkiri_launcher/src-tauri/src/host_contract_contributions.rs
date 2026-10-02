//! Declarative Pack contributions captured into the Launcher-owned contract.
//!
//! A contribution is read only from an ApplicationAuthority root that the
//! signed resolver has already selected and verified.  This module never
//! imports or executes Pack code while assembling host authority.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Component, Path, PathBuf};

use anyhow::{bail, Context, Result};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use crate::defaultspack_authority::{ApplicationAuthority, VerifiedPackArtifact};

const CONTRIBUTION_PATH: &str = "host_contract_contributions.v1.json";
const CONTRIBUTION_SCHEMA: &str = "io.tobkiri.host-contract-contribution.v1";
const SYSTEM_PACK_DESCRIPTOR_SCHEMA: &str = "io.tobkiri.system-pack-trust.v1";
const UPDATE_TARGET_DESCRIPTOR_SCHEMA: &str = "io.tobkiri.update-target.v1";
const UPDATE_METADATA_SCHEMA: &str = "io.tobkiri.pack-update-metadata.v1";
const CONTRIBUTION_ARTIFACT_ROLE: &str = "host_contract_contribution";
const UPDATE_METADATA_ARTIFACT_ROLE: &str = "host_contract_update_metadata";

/// JSON values ready for the Launcher-owned Host contract's `values` map.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct HostContractContributionValues {
    pub(crate) system_pack_descriptors: String,
    pub(crate) update_target_descriptors: String,
}

impl HostContractContributionValues {
    fn empty() -> Self {
        Self {
            system_pack_descriptors: "[]".into(),
            update_target_descriptors: "[]".into(),
        }
    }
}

/// Collect declarations from the root previously verified by the resolver.
pub(crate) fn collect_for_verified_application(
    authority: &ApplicationAuthority,
) -> Result<HostContractContributionValues> {
    collect_from_verified_artifacts(
        &authority.pack_root,
        &authority.materialized_pack_id,
        &authority.verified_artifacts,
    )
}

fn collect_from_verified_artifacts(
    pack_root: &Path,
    pack_id: &str,
    declared_artifacts: &BTreeMap<String, VerifiedPackArtifact>,
) -> Result<HostContractContributionValues> {
    let Some(contribution) = read_declared_json_artifact(
        pack_root,
        declared_artifacts,
        CONTRIBUTION_PATH,
        CONTRIBUTION_ARTIFACT_ROLE,
        "Host contract contribution",
    )?
    else {
        // An absent or undeclared sidecar grants nothing. This is important
        // during the migration before the canonical artifact generator seals
        // contribution declarations into an artifact index.
        return Ok(HostContractContributionValues::empty());
    };
    contribution_values(pack_root, pack_id, declared_artifacts, &contribution)
}

fn contribution_values(
    pack_root: &Path,
    pack_id: &str,
    declared_artifacts: &BTreeMap<String, VerifiedPackArtifact>,
    contribution: &Value,
) -> Result<HostContractContributionValues> {
    let root = exact_object(
        contribution,
        ["schema", "pack_id", "system_pack", "update_target"],
        "Host contract contribution",
    )?;
    if string_field(root, "schema", "Host contract contribution")? != CONTRIBUTION_SCHEMA
        || string_field(root, "pack_id", "Host contract contribution")? != pack_id
    {
        bail!("Host contract contribution identity is invalid");
    }

    let system = exact_object(
        required_value(root, "system_pack", "Host contract contribution")?,
        ["trust_class", "allow_in_process"],
        "system Pack contribution",
    )?;
    if string_field(system, "trust_class", "system Pack contribution")? != "system" {
        bail!("system Pack contribution trust class is invalid");
    }
    let allow_in_process = bool_field(system, "allow_in_process", "system Pack contribution")?;
    let root_text = pack_root
        .to_str()
        .context("verified Pack root is not valid UTF-8")?;
    let system_descriptor = json!({
        "schema": SYSTEM_PACK_DESCRIPTOR_SCHEMA,
        "pack_id": pack_id,
        "root": root_text,
        "trust_class": "system",
        "allow_in_process": allow_in_process,
    });

    let mut update_descriptors = Vec::new();
    if let Some(update_target) = root.get("update_target") {
        if !update_target.is_null() {
            update_descriptors.push(update_target_descriptor(
                pack_root,
                pack_id,
                declared_artifacts,
                update_target,
            )?);
        }
    }

    Ok(HostContractContributionValues {
        system_pack_descriptors: serde_json::to_string(&vec![system_descriptor])
            .context("failed to encode system Pack descriptors")?,
        update_target_descriptors: serde_json::to_string(&update_descriptors)
            .context("failed to encode update target descriptors")?,
    })
}

fn update_target_descriptor(
    pack_root: &Path,
    pack_id: &str,
    declared_artifacts: &BTreeMap<String, VerifiedPackArtifact>,
    value: &Value,
) -> Result<Value> {
    let target = exact_object(
        value,
        [
            "target",
            "source_root",
            "destination_root",
            "version_path",
            "version_format",
            "protected_paths",
            "runtime_reload_recommended",
        ],
        "update target contribution",
    )?;
    let target_id = string_field(target, "target", "update target contribution")?;
    if target_id != pack_id || !valid_update_target(target_id) {
        bail!("update target contribution identity is invalid");
    }
    let source_root = string_field(target, "source_root", "update target contribution")?;
    let destination_root = string_field(target, "destination_root", "update target contribution")?;
    let version_path = string_field(target, "version_path", "update target contribution")?;
    let version_format = string_field(target, "version_format", "update target contribution")?;
    if !safe_relative(source_root)
        || !safe_relative(destination_root)
        || !safe_relative(version_path)
        || version_format != "json"
    {
        bail!("update target contribution path or format is invalid");
    }
    let protected_paths = target
        .get("protected_paths")
        .and_then(Value::as_array)
        .context("update target contribution protected_paths is invalid")?;
    if protected_paths
        .iter()
        .any(|item| item.as_str().map_or(true, |pattern| !safe_pattern(pattern)))
    {
        bail!("update target contribution protected path is invalid");
    }
    let reload = bool_field(
        target,
        "runtime_reload_recommended",
        "update target contribution",
    )?;

    let metadata = read_declared_json_artifact(
        pack_root,
        declared_artifacts,
        version_path,
        UPDATE_METADATA_ARTIFACT_ROLE,
        "Pack update metadata",
    )?
    .context("update target version metadata is not a declared Pack artifact")?;
    let metadata = exact_object(
        &metadata,
        ["schema", "pack_id", "version"],
        "Pack update metadata",
    )?;
    if string_field(metadata, "schema", "Pack update metadata")? != UPDATE_METADATA_SCHEMA
        || string_field(metadata, "pack_id", "Pack update metadata")? != pack_id
        || string_field(metadata, "version", "Pack update metadata")?.is_empty()
    {
        bail!("Pack update metadata identity is invalid");
    }

    Ok(json!({
        "schema": UPDATE_TARGET_DESCRIPTOR_SCHEMA,
        "target": target_id,
        "source_root": source_root,
        "destination_root": destination_root,
        "version_path": version_path,
        "version_format": version_format,
        "protected_paths": protected_paths,
        "runtime_reload_recommended": reload,
    }))
}

fn read_declared_json_artifact(
    pack_root: &Path,
    declared: &BTreeMap<String, VerifiedPackArtifact>,
    relative: &str,
    expected_role: &str,
    label: &str,
) -> Result<Option<Value>> {
    let Some(artifact) = declared.get(relative) else {
        return Ok(None);
    };
    if artifact.role != expected_role {
        bail!("{label} has an invalid verified artifact role");
    }
    let path = pack_root.join(relative);
    let bytes = read_regular_file(&path, label)?;
    if sha256(&bytes) != artifact.digest {
        bail!("{label} digest does not match the verified Pack artifact index");
    }
    let value = serde_json::from_slice(&bytes).with_context(|| format!("{label} is malformed"))?;
    Ok(Some(value))
}

fn read_regular_file(path: &Path, label: &str) -> Result<Vec<u8>> {
    let metadata = fs::symlink_metadata(path)
        .with_context(|| format!("{label} is missing at {}", path.display()))?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        bail!("{label} must be a regular non-symlink file");
    }
    fs::read(path).with_context(|| format!("failed to read {label} at {}", path.display()))
}

fn exact_object<'a, const N: usize>(
    value: &'a Value,
    expected_fields: [&str; N],
    label: &str,
) -> Result<&'a Map<String, Value>> {
    let object = value
        .as_object()
        .with_context(|| format!("{label} must be an object"))?;
    let actual = object.keys().map(String::as_str).collect::<BTreeSet<_>>();
    let expected = expected_fields.into_iter().collect::<BTreeSet<_>>();
    if actual != expected {
        bail!("{label} fields are invalid");
    }
    Ok(object)
}

fn required_value<'a>(object: &'a Map<String, Value>, key: &str, label: &str) -> Result<&'a Value> {
    object
        .get(key)
        .with_context(|| format!("{label} is missing {key}"))
}

fn string_field<'a>(object: &'a Map<String, Value>, key: &str, label: &str) -> Result<&'a str> {
    required_value(object, key, label)?
        .as_str()
        .filter(|value| !value.is_empty())
        .with_context(|| format!("{label} {key} is invalid"))
}

fn bool_field(object: &Map<String, Value>, key: &str, label: &str) -> Result<bool> {
    required_value(object, key, label)?
        .as_bool()
        .with_context(|| format!("{label} {key} is invalid"))
}

fn safe_relative(value: &str) -> bool {
    let path = PathBuf::from(value);
    !value.is_empty()
        && !value.contains('\\')
        && !value.contains('\0')
        && !path.is_absolute()
        && path
            .components()
            .all(|component| matches!(component, Component::Normal(_)))
        && path.to_string_lossy().replace('\\', "/") == value
}

fn safe_pattern(value: &str) -> bool {
    !value.is_empty()
        && !value.contains('\\')
        && !value.contains('\0')
        && !value.starts_with('/')
        && !value.split('/').any(|part| part == ".." || part.is_empty())
}

fn valid_update_target(value: &str) -> bool {
    let bytes = value.as_bytes();
    !bytes.is_empty()
        && bytes.len() <= 128
        && bytes[0].is_ascii_lowercase()
        && bytes.iter().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'_' | b'-')
        })
}

fn sha256(bytes: &[u8]) -> String {
    format!("sha256:{:x}", Sha256::digest(bytes))
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;
    use std::sync::atomic::{AtomicUsize, Ordering};

    use super::{collect_from_verified_artifacts, sha256, HostContractContributionValues};
    use crate::defaultspack_authority::VerifiedPackArtifact;
    use serde_json::json;

    static NEXT_ROOT: AtomicUsize = AtomicUsize::new(0);

    fn fixture_root(include_contribution: bool) -> std::path::PathBuf {
        let root = std::env::temp_dir().join(format!(
            "tobkiri-host-contract-contributions-{}-{}",
            std::process::id(),
            NEXT_ROOT.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir_all(&root).unwrap();
        let contribution = json!({
            "schema": "io.tobkiri.host-contract-contribution.v1",
            "pack_id": "example-pack",
            "system_pack": {"trust_class": "system", "allow_in_process": true},
            "update_target": {
                "target": "example-pack",
                "source_root": "tobkiri_runtime/ecosystem/example-pack",
                "destination_root": "ecosystem/example-pack",
                "version_path": "update_metadata.v1.json",
                "version_format": "json",
                "protected_paths": ["user_data", "user_data/**"],
                "runtime_reload_recommended": true
            }
        });
        let metadata = json!({
            "schema": "io.tobkiri.pack-update-metadata.v1",
            "pack_id": "example-pack",
            "version": "1.2.3"
        });
        let contribution_bytes = serde_json::to_vec(&contribution).unwrap();
        let metadata_bytes = serde_json::to_vec(&metadata).unwrap();
        std::fs::write(
            root.join("host_contract_contributions.v1.json"),
            &contribution_bytes,
        )
        .unwrap();
        std::fs::write(root.join("update_metadata.v1.json"), &metadata_bytes).unwrap();
        root
    }

    fn replace_declared_contribution(root: &std::path::Path, contribution: serde_json::Value) {
        let bytes = serde_json::to_vec(&contribution).unwrap();
        std::fs::write(root.join("host_contract_contributions.v1.json"), &bytes).unwrap();
    }

    fn fixture_verified_artifacts(
        root: &std::path::Path,
        include_contribution: bool,
    ) -> BTreeMap<String, VerifiedPackArtifact> {
        let mut digests = BTreeMap::from([(
            "update_metadata.v1.json".to_string(),
            VerifiedPackArtifact {
                digest: sha256(&std::fs::read(root.join("update_metadata.v1.json")).unwrap()),
                role: "host_contract_update_metadata".to_string(),
            },
        )]);
        if include_contribution {
            digests.insert(
                "host_contract_contributions.v1.json".to_string(),
                VerifiedPackArtifact {
                    digest: sha256(
                        &std::fs::read(root.join("host_contract_contributions.v1.json")).unwrap(),
                    ),
                    role: "host_contract_contribution".to_string(),
                },
            );
        }
        digests
    }

    #[test]
    fn verified_declared_json_contribution_produces_contract_values() {
        let root = fixture_root(true);
        let values = collect_from_verified_artifacts(
            &root,
            "example-pack",
            &fixture_verified_artifacts(&root, true),
        )
        .unwrap();
        let system: serde_json::Value =
            serde_json::from_str(&values.system_pack_descriptors).unwrap();
        let updates: serde_json::Value =
            serde_json::from_str(&values.update_target_descriptors).unwrap();
        assert_eq!(system[0]["pack_id"], "example-pack");
        assert_eq!(system[0]["allow_in_process"], true);
        assert_eq!(updates[0]["target"], "example-pack");
        assert_eq!(updates[0]["version_path"], "update_metadata.v1.json");
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn undeclared_contribution_grants_nothing() {
        let root = fixture_root(false);
        assert_eq!(
            collect_from_verified_artifacts(
                &root,
                "example-pack",
                &fixture_verified_artifacts(&root, false),
            )
            .unwrap(),
            HostContractContributionValues::empty()
        );
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn tampered_declared_contribution_fails_closed() {
        let root = fixture_root(true);
        let verified_digests = fixture_verified_artifacts(&root, true);
        std::fs::write(root.join("host_contract_contributions.v1.json"), "{}").unwrap();
        assert!(collect_from_verified_artifacts(&root, "example-pack", &verified_digests).is_err());
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn invalid_declared_update_destination_fails_closed() {
        let root = fixture_root(true);
        replace_declared_contribution(
            &root,
            json!({
                "schema": "io.tobkiri.host-contract-contribution.v1",
                "pack_id": "example-pack",
                "system_pack": {"trust_class": "system", "allow_in_process": true},
                "update_target": {
                    "target": "example-pack",
                    "source_root": "tobkiri_runtime/ecosystem/example-pack",
                    "destination_root": "../outside-runtime",
                    "version_path": "update_metadata.v1.json",
                    "version_format": "json",
                    "protected_paths": ["user_data", "user_data/**"],
                    "runtime_reload_recommended": true
                }
            }),
        );

        assert!(collect_from_verified_artifacts(
            &root,
            "example-pack",
            &fixture_verified_artifacts(&root, true),
        )
        .is_err());
        std::fs::remove_dir_all(root).unwrap();
    }
}
