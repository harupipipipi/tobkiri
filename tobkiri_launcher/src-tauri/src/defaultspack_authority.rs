//! Pack v4 authority resolution for a Launcher-owned application instance.
//!
//! The historical module name is retained because the current Launcher still
//! uses the Defaultspack adapter at its composition root.  Authority itself is
//! deliberately product-neutral: the active Profile (or the signed catalog's
//! explicit bootstrap selection) supplies the Base, Shell, Application, and
//! artifact identities.  No product ID is an authority rule.

use std::collections::{BTreeMap, BTreeSet};
use std::ffi::OsString;
use std::fmt;
use std::fs;
use std::path::{Component, Path, PathBuf};

use anyhow::{bail, Context, Result};
use serde::Deserialize;
use serde_json::Value;
use sha2::{Digest, Sha256};

#[cfg(test)]
#[path = "packaging_toolchain.rs"]
mod packaging_toolchain;

use crate::config::AppConfig;

const DEFAULT_PROFILE_API_VERSION: &str = "io.tobkiri.profile.v5";
const EXECUTABLE_CATALOG_API_VERSION: &str = "io.tobkiri.executable-catalog.v4";
const MAX_CANONICAL_JSON_BYTES: usize = 4 * 1024 * 1024;
const MAX_CANONICAL_JSON_DEPTH: usize = 64;
const MAX_SAFE_JSON_INTEGER: u64 = (1_u64 << 53) - 1;
const BUNDLE_SCHEMA: &str = "io.tobkiri.defaultspack-bundle-lock.v1";

// These values are test-fixture coordinates, not production authority.  Keep
// them scoped to the fixture helpers so a future Pack cannot accidentally be
// admitted because it happens to reuse the historical Defaults identities.
#[cfg(test)]
const DEFAULT_PROFILE_ID: &str = "defaults";
#[cfg(test)]
const DEFAULT_BASE_ID: &str = "defaults-basepack";
#[cfg(test)]
const DEFAULT_SHELL_ID: &str = "shell.tauri.default";
#[cfg(test)]
const DEFAULT_RUNTIME_ID: &str = "runtime.tauri.application.default";
#[cfg(test)]
const DEFAULT_PROFILE_SOURCE: &str =
    "tobkiri_runtime/ecosystem/defaultspack/v4/defaults.profile.v4.json";
#[cfg(test)]
const DEFAULT_PROVIDER_PACK_IDS: [&str; 13] = [
    "defaultspack",
    "rumi_ai_gateway_pack",
    "rumi_ai_pipeline_pack",
    "rumi_ai_routing_pack",
    "rumi_ai_stream_pack",
    "rumi_ai_tool_bridge_pack",
    "rumi_ai_usage_pack",
    "rumi_file_inspect_pack",
    "rumi_model_catalog_pack",
    "rumi_model_registry_pack",
    "rumi_provider_adapters_pack",
    "rumi_provider_registry_pack",
    "tobkiri_host_pack_control",
];
#[cfg(test)]
const PROFILE_PATH: &str = "defaults.profile.v4.json";
#[cfg(test)]
const DEFAULTSPACK_PACK_PATH: &str = "packs/defaultspack.pack.v4.json";
#[cfg(test)]
const BASE_PACK_PATH: &str = "packs/defaults-basepack.pack.v4.json";
#[cfg(test)]
const SHELL_PACK_PATH: &str = "packs/shell.tauri.default.pack.v4.json";
#[cfg(test)]
const RUNTIME_PACK_PATH: &str = "packs/runtime.tauri.application.default.pack.v4.json";

/// Exact, immutable authority captured for one application launch.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ApplicationAuthority {
    pub pack_root: PathBuf,
    /// Artifacts captured while resolving this selected Pack root.
    /// Consumers must not re-read the mutable artifact index after resolution.
    pub verified_artifacts: BTreeMap<String, VerifiedPackArtifact>,
    pub materialized_pack_id: String,
    pub launch: ApplicationLaunch,
    pub profile_id: String,
    /// The source Profile bytes digest for bootstrap, or the Profile revision
    /// captured by the active activation.
    pub profile_digest: String,
    pub catalog_revision: String,
    pub profile_revision: Option<String>,
    pub activation_id: Option<String>,
    pub plan_digest: Option<String>,
    pub base_pack_id: String,
    pub shell_provider_id: String,
    pub application_id: String,
    pub launch_contribution: Option<RuntimeLaunchContribution>,
}

/// One artifact declaration accepted by the selected Pack verifier.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct VerifiedPackArtifact {
    pub digest: String,
    pub role: String,
}

/// Canonical Application launch selector carried by the active ResolvedPlan.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct RuntimeLaunchContribution {
    pub provider_id: String,
    pub contract_id: String,
    pub operation_id: String,
    pub platform: String,
    pub architecture: String,
    pub artifact_digest: String,
    pub relative_path: String,
    pub entrypoint: String,
}

/// Recoverable compatibility state for an activation created before the
/// ResolvedPlan launch selector became available.
#[derive(Debug)]
pub(crate) struct ProfileReresolutionRequired;

impl fmt::Display for ProfileReresolutionRequired {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(
            "active ResolvedPlan has no Application launch contribution; profile reactivation or re-resolution is required",
        )
    }
}

impl std::error::Error for ProfileReresolutionRequired {}

impl ProfileReresolutionRequired {
    pub(crate) const CODE: &'static str = "PROFILE_RERESOLUTION_REQUIRED";
    pub(crate) const ACTION: &'static str = "reactivate_or_reresolve_profile";
}

impl ApplicationAuthority {
    /// Return the activation identity that is permitted to execute this
    /// Application.  A signed bootstrap Profile is useful for setup and safe
    /// repair, but it is deliberately not a normal launch authority.
    pub(crate) fn execution_identity(
        &self,
    ) -> Result<crate::host_contract::ExecutionProfileIdentity> {
        let profile_revision = self
            .profile_revision
            .as_deref()
            .context("Application launch requires an active Profile revision")?;
        let activation_id = self
            .activation_id
            .as_deref()
            .context("Application launch requires an active activation")?;
        let plan_digest = self
            .plan_digest
            .as_deref()
            .context("Application launch requires a verified ResolvedPlan")?;
        if self.profile_digest != profile_revision {
            bail!("active Profile revision differs from the selected Profile bytes");
        }
        crate::host_contract::ExecutionProfileIdentity::new(
            &self.profile_id,
            profile_revision,
            activation_id,
            plan_digest,
        )
        .context("Application launch execution identity is invalid")
    }

    pub(crate) fn runtime_launch_contribution(&self) -> Result<&RuntimeLaunchContribution> {
        self.launch_contribution
            .as_ref()
            .context("active ResolvedPlan has no Application launch contribution")
    }
}

/// Compatibility alias for callers that still use the old guardian vocabulary.
pub(crate) type GuardianAuthority = ApplicationAuthority;

/// Verified process materialization for an Application Pack's launch function.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ApplicationLaunch {
    pub entrypoint: PathBuf,
    pub argv: Vec<OsString>,
    pub artifact_id: String,
    pub artifact_digest: String,
    pub entrypoint_digest: String,
    pub function_id: String,
    pub provider_id: String,
    pub contract_namespace: String,
}

/// Compatibility alias for the pre-generic Launcher composition root.
pub(crate) type GuardianLaunch = ApplicationLaunch;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct BundleLock {
    schema: String,
    entries: Vec<BundleEntry>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct BundleEntry {
    path: String,
    kind: BundleEntryKind,
    digest: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
enum BundleEntryKind {
    Pack,
    Base,
    Shell,
    Profile,
    ExecutableCatalog,
}

#[derive(Debug, PartialEq, Eq)]
struct VerifiedBundleLock {
    authority_digests: BTreeMap<String, String>,
    sidecar_digests: BTreeMap<String, String>,
    pack_paths: BTreeMap<String, String>,
    authority_roles: BTreeMap<String, BundleEntryKind>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ExecutableCatalog {
    catalog_api_version: String,
    pack_id: String,
    source_identity: String,
    variants: Vec<ExecutableVariant>,
    materialization_catalog_digest: Option<String>,
    catalog_digest: String,
}

/// Identity facts needed to bind a sidecar catalog to its Pack authority.
///
/// A generated Pack is a source-bound projection.  Its sidecar must preserve
/// the digest of the canonical executable catalog that was used to materialize
/// it.  Canonical and externally admitted Packs use `catalog_digest` directly
/// and must not introduce that alias field.
struct PackCatalogIdentity {
    source_identity: String,
    is_source_bound_projection: bool,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ExecutableVariant {
    variant_id: String,
    function_id: String,
    implementation_path: String,
    implementation_digest: String,
    execution_kind: String,
    platform: String,
    architecture: String,
    runtime_abi: String,
    backend: String,
    materialization_mode: String,
    execution_domain_profile: String,
    operations: Vec<ExecutableOperation>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ExecutableOperation {
    contract_id: String,
    contract_version: String,
    revision_digest: String,
    operation_id: String,
    input_schema: BTreeMap<String, Value>,
    output_schema: BTreeMap<String, Value>,
    error_schema: BTreeMap<String, Value>,
    effect_class: String,
    timeout_default_ms: u64,
    timeout_hard_max_ms: u64,
    idempotency: String,
}

/// Resolve launch metadata from the signed catalog, an active Profile, and a
/// sealed Application Pack.
pub(crate) fn resolve(config: &AppConfig) -> Result<GuardianAuthority> {
    SignedApplicationResolver::resolve(config)
}

/// Return whether the durable active-Profile pointer is present and valid.
///
/// This deliberately validates the complete pointer/snapshot pair rather than
/// treating an old Host-contract file as authority. Callers that receive
/// ``true`` must still resolve the selected Application before launch.
pub(crate) fn has_verified_active_profile(config: &AppConfig) -> Result<bool> {
    Ok(read_active_profile_snapshot(config)?.is_some())
}

/// Generic resolver retained behind the historical module boundary until the
/// Launcher composition root can be renamed without a migration fan-out.
pub(crate) struct SignedApplicationResolver;

impl SignedApplicationResolver {
    /// Resolve one application instance without selecting a product by name.
    pub(crate) fn resolve(config: &AppConfig) -> Result<ApplicationAuthority> {
        let catalog = crate::presentation::load_catalog(config)
            .context("signed presentation catalog authority was rejected")?;
        let app_root = canonical_directory(&config.app_dir, "packaged application root")?;
        let (_, bootstrap_profile_source, _) = catalog.bootstrap_profile_identity()?;
        let bundle_root = packaged_bundle_root(&app_root, bootstrap_profile_source)?;
        let pack_root = canonical_pack_root(&bundle_root)?;
        verify_symlink_free_tree(&pack_root, &pack_root)?;
        let bundle_lock = verify_bundle_lock(&bundle_root)?;
        #[cfg(test)]
        let catalog = fixture_catalog_with_shell_variant(catalog, &bundle_root, &bundle_lock)?;
        let selected = select_profile_authority(config, &catalog, &bundle_root, &bundle_lock)?;

        let selected_variant = validate_profile(&selected.profile, &catalog, &selected)?;
        validate_profile_pack_closure(&selected, &catalog, &bundle_root, &bundle_lock)?;

        let application_path =
            bundle_pack_path(&bundle_root, &bundle_lock, &selected.application_pack_id)?;
        let application_pack = read_json(&application_path, "selected Application Pack v4")?;
        let launch = validate_application_pack(
            &pack_root,
            &pack_root,
            &application_pack,
            selected_variant,
            selected.launch_contribution.as_ref(),
            &selected.application_pack_id,
            selected.application_artifact_digest.as_deref(),
        )?;

        // The process root is still supplied by the current Launcher adapter,
        // but its identity is obtained from the selected closure. This keeps
        // the PackVM/artifact-index reconciliation in place while removing the
        // old Defaultspack-only authority rule.
        let root_pack = read_json(&pack_root.join("pack.v4.json"), "materialized Pack")?;
        let root_pack_id = value_str(&root_pack, "/pack/id")
            .context("materialized Pack is missing its Pack identity")?
            .to_owned();
        ensure_materialized_pack_selected(&root_pack_id, &selected.pack_ids)?;
        let verified_artifacts =
            verify_pack_artifact_index(&pack_root, &bundle_root, &root_pack_id)?;

        let catalog_revision = crate::presentation::catalog_revision(&catalog)?;
        Ok(ApplicationAuthority {
            pack_root,
            verified_artifacts,
            materialized_pack_id: root_pack_id,
            launch,
            profile_id: selected.profile_id,
            profile_digest: selected.profile_digest,
            catalog_revision,
            profile_revision: selected.profile_revision,
            activation_id: selected.activation_id,
            plan_digest: selected.plan_digest,
            base_pack_id: selected.base_pack_id,
            shell_provider_id: selected.shell_provider_id,
            application_id: selected.application_pack_id,
            launch_contribution: selected.launch_contribution,
        })
    }
}

fn ensure_materialized_pack_selected(
    root_pack_id: &str,
    selected_pack_ids: &BTreeSet<String>,
) -> Result<()> {
    if !selected_pack_ids.contains(root_pack_id) {
        bail!("materialized Pack is outside the selected Profile closure");
    }
    Ok(())
}

#[derive(Debug)]
struct SelectedProfileAuthority {
    profile: Value,
    lock: Option<Value>,
    plan: Option<Value>,
    profile_id: String,
    profile_digest: String,
    profile_revision: Option<String>,
    activation_id: Option<String>,
    plan_digest: Option<String>,
    lock_digest: Option<String>,
    base_pack_id: String,
    shell_provider_id: String,
    shell_pack_id: String,
    application_pack_id: String,
    application_artifact_digest: Option<String>,
    launch_contribution: Option<RuntimeLaunchContribution>,
    pack_ids: BTreeSet<String>,
}

#[derive(Debug)]
struct ActiveProfileSnapshot {
    profile: Value,
    lock: Value,
    plan: Value,
    identity: crate::host_contract::ExecutionProfileIdentity,
    profile_revision: String,
    activation_id: String,
    plan_digest: String,
    lock_digest: String,
}

fn read_active_profile_snapshot(config: &AppConfig) -> Result<Option<ActiveProfileSnapshot>> {
    match fs::symlink_metadata(&config.user_data_dir) {
        Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_dir() => {
            bail!("Host state root must be a non-symlink directory");
        }
        Ok(_) => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error).context("failed to inspect Host state root"),
    }
    let user_data_root = canonical_directory(&config.user_data_dir, "Host state root")?;
    let profiles_root = user_data_root.join("profiles");
    let profiles_metadata = match fs::symlink_metadata(&profiles_root) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error).context("failed to inspect Profile authority directory"),
    };
    if profiles_metadata.file_type().is_symlink() || !profiles_metadata.is_dir() {
        bail!("Profile authority directory must be a non-symlink directory");
    }
    let pointer_path = profiles_root.join("active.json");
    let pointer_raw = match fs::symlink_metadata(&pointer_path) {
        Ok(_) => read_regular_file(&pointer_path, "active Profile pointer")?,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error).context("failed to inspect active Profile pointer"),
    };
    let pointer: Value =
        serde_json::from_slice(&pointer_raw).context("active Profile pointer is malformed")?;
    validate_canonical_json(&pointer, 0)?;
    let pointer_object = pointer
        .as_object()
        .context("active Profile pointer must be an object")?;
    let expected_fields = [
        "schema",
        "profile_id",
        "profile_revision",
        "activation_id",
        "plan_digest",
        "lock_digest",
        "activation_snapshot_path",
        "activation_snapshot_digest",
        "catalog_revision",
        "generation",
        "updated_at",
        "pointer_digest",
    ]
    .into_iter()
    .collect::<BTreeSet<_>>();
    if pointer_object
        .keys()
        .map(String::as_str)
        .collect::<BTreeSet<_>>()
        != expected_fields
    {
        bail!("active Profile pointer has unknown or missing fields");
    }
    if value_str(&pointer, "/schema") != Some("io.tobkiri.active-profile-pointer.v1") {
        bail!("active Profile pointer schema is unsupported");
    }
    let pointer_digest = value_str(&pointer, "/pointer_digest")
        .context("active Profile pointer digest is missing")?;
    let mut unsigned_pointer = pointer.clone();
    unsigned_pointer
        .as_object_mut()
        .context("active Profile pointer must be an object")?
        .remove("pointer_digest");
    if canonical_value_digest(&unsigned_pointer)? != pointer_digest {
        bail!("active Profile pointer digest is invalid");
    }

    let profile_id = value_str(&pointer, "/profile_id")
        .context("active Profile pointer profile_id is missing")?;
    let profile_revision = value_str(&pointer, "/profile_revision")
        .context("active Profile pointer profile_revision is missing")?;
    let activation_id = value_str(&pointer, "/activation_id")
        .context("active Profile pointer activation_id is missing")?;
    let plan_digest = value_str(&pointer, "/plan_digest")
        .context("active Profile pointer plan_digest is missing")?;
    let lock_digest = value_str(&pointer, "/lock_digest")
        .context("active Profile pointer lock_digest is missing")?;
    let snapshot_digest = value_str(&pointer, "/activation_snapshot_digest")
        .context("active Profile pointer snapshot digest is missing")?;
    if !valid_identifier(profile_id)
        || !valid_digest(profile_revision)
        || !valid_activation_id(activation_id)
        || !valid_digest(plan_digest)
        || !valid_digest(lock_digest)
        || !valid_digest(snapshot_digest)
    {
        bail!("active Profile pointer identity is invalid");
    }
    if let Some(catalog_revision) = pointer
        .pointer("/catalog_revision")
        .filter(|value| !value.is_null())
        .and_then(Value::as_str)
    {
        if !valid_digest(catalog_revision) {
            bail!("active Profile pointer catalog revision is invalid");
        }
    } else if !pointer
        .pointer("/catalog_revision")
        .is_some_and(Value::is_null)
    {
        bail!("active Profile pointer catalog revision must be a digest or null");
    }
    for pointer_name in ["generation", "updated_at"] {
        if pointer
            .pointer(&format!("/{pointer_name}"))
            .and_then(Value::as_u64)
            .is_none()
        {
            bail!("active Profile pointer {pointer_name} is invalid");
        }
    }
    if pointer.pointer("/generation").and_then(Value::as_u64) == Some(0) {
        bail!("active Profile pointer generation must be positive");
    }
    let snapshot_relative = value_str(&pointer, "/activation_snapshot_path")
        .context("active Profile pointer snapshot path is missing")?;
    let snapshot_relative = safe_active_snapshot_path(snapshot_relative)?;
    let snapshot_path = user_data_root.join(&snapshot_relative);
    let snapshot_raw = read_rooted_regular_file(
        &user_data_root,
        &snapshot_path,
        "active Profile activation snapshot",
    )?;
    let snapshot: Value = serde_json::from_slice(&snapshot_raw)
        .context("active Profile activation snapshot is malformed")?;
    validate_canonical_json(&snapshot, 0)?;
    if canonical_value_digest(&snapshot)? != snapshot_digest {
        bail!("active Profile activation snapshot digest is invalid");
    }
    let envelope = snapshot
        .get("envelope")
        .filter(|value| value.is_object())
        .unwrap_or(&snapshot);
    let profile = envelope
        .get("profile")
        .filter(|value| value.is_object())
        .cloned()
        .context("active Profile snapshot Profile record is missing")?;
    let lock = envelope
        .get("lock")
        .filter(|value| value.is_object())
        .cloned()
        .context("active Profile snapshot ProfileLock record is missing")?;
    let plan = envelope
        .get("plan")
        .filter(|value| value.is_object())
        .cloned()
        .context("active Profile snapshot ResolvedPlan record is missing")?;
    let activation = envelope
        .get("activation")
        .filter(|value| value.is_object())
        .context("active Profile snapshot ActivationRecord is missing")?;
    if value_str(&profile, "/profile_id") != Some(profile_id)
        || value_str(&plan, "/profile_revision") != Some(profile_revision)
        || value_str(&plan, "/plan_digest") != Some(plan_digest)
        || value_str(&lock, "/lock_digest") != Some(lock_digest)
        || value_str(&lock, "/profile_revision") != Some(profile_revision)
        || value_str(&lock, "/plan_digest") != Some(plan_digest)
        || value_str(activation, "/profile_id") != Some(profile_id)
        || value_str(activation, "/profile_revision") != Some(profile_revision)
        || value_str(activation, "/activation_id") != Some(activation_id)
        || value_str(activation, "/plan_digest") != Some(plan_digest)
        || value_str(activation, "/lock_digest") != Some(lock_digest)
        || value_str(activation, "/state") != Some("active")
    {
        bail!("active Profile snapshot identity does not match its pointer");
    }
    if let Some(catalog_revision) = pointer.pointer("/catalog_revision").and_then(Value::as_str) {
        if value_str(&plan, "/catalog_revision") != Some(catalog_revision) {
            bail!("active Profile snapshot catalog revision is stale");
        }
    }
    if canonical_value_digest(&profile)? != profile_revision {
        bail!("active Profile revision digest is stale");
    }
    if canonical_record_digest(&lock, "lock_digest")? != lock_digest {
        bail!("active ProfileLock digest is stale");
    }
    if canonical_record_digest(&plan, "plan_digest")? != plan_digest {
        bail!("active ResolvedPlan digest is stale");
    }
    let identity = crate::host_contract::ExecutionProfileIdentity::new(
        profile_id,
        profile_revision,
        activation_id,
        plan_digest,
    )?;
    Ok(Some(ActiveProfileSnapshot {
        profile,
        lock,
        plan,
        identity,
        profile_revision: profile_revision.to_owned(),
        activation_id: activation_id.to_owned(),
        plan_digest: plan_digest.to_owned(),
        lock_digest: lock_digest.to_owned(),
    }))
}

fn safe_active_snapshot_path(value: &str) -> Result<PathBuf> {
    let path = safe_relative(value)?;
    if path
        .components()
        .next()
        .and_then(|component| component.as_os_str().to_str())
        != Some("workspaces")
    {
        bail!("active Profile snapshot must be below workspaces");
    }
    Ok(path)
}

fn read_rooted_regular_file(root: &Path, path: &Path, label: &str) -> Result<Vec<u8>> {
    let metadata = fs::symlink_metadata(path)
        .with_context(|| format!("{label} is missing at {}", path.display()))?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        bail!("{label} must be a regular non-symlink file");
    }
    if has_multiple_links(path, &metadata)? {
        bail!("{label} must not be multiply linked");
    }
    let canonical = path
        .canonicalize()
        .with_context(|| format!("failed to canonicalize {label}"))?;
    if !canonical.starts_with(root) {
        bail!("{label} escapes the Host state root");
    }
    fs::read(path).with_context(|| format!("failed to read {label} at {}", path.display()))
}

fn canonical_record_digest(value: &Value, digest_field: &str) -> Result<String> {
    let mut unsigned = value.clone();
    unsigned
        .as_object_mut()
        .context("digest-bound record must be an object")?
        .remove(digest_field);
    canonical_value_digest(&unsigned)
}

fn packaged_bundle_root(app_root: &Path, source: &str) -> Result<PathBuf> {
    let source_path = safe_relative(source)?;
    let components = source_path
        .components()
        .filter_map(|component| match component {
            Component::Normal(value) => value.to_str(),
            _ => None,
        })
        .collect::<Vec<_>>();
    let ecosystem_index = components
        .iter()
        .position(|component| *component == "ecosystem")
        .context("Profile source does not identify an ecosystem bundle")?;
    if ecosystem_index + 1 >= components.len() {
        bail!("Profile source does not identify a Pack bundle root");
    }
    let bundle_components = &components[ecosystem_index..components.len() - 1];
    if bundle_components.is_empty() {
        bail!("Profile source does not identify a Pack bundle root");
    }
    let relative = bundle_components.iter().collect::<PathBuf>();
    canonical_child_directory(app_root, &relative, "selected Pack v4 root")
}

fn canonical_pack_root(bundle_root: &Path) -> Result<PathBuf> {
    let parent = bundle_root
        .parent()
        .context("selected Pack v4 root has no Pack root parent")?;
    let pack_root = canonical_directory(parent, "selected Pack root")?;
    let bundle_name = bundle_root
        .file_name()
        .context("selected Pack v4 root has no directory name")?;
    let revalidated_bundle_root = pack_root
        .join(bundle_name)
        .canonicalize()
        .context("failed to revalidate selected Pack v4 root from its Pack root")?;
    if revalidated_bundle_root.as_path() != bundle_root {
        bail!("selected Pack v4 root does not remain beneath its Pack root");
    }
    Ok(pack_root)
}

fn select_profile_authority(
    config: &AppConfig,
    catalog: &crate::presentation::PresentationCatalog,
    bundle_root: &Path,
    bundle_lock: &VerifiedBundleLock,
) -> Result<SelectedProfileAuthority> {
    if let Some(active) = read_active_profile_snapshot(config)? {
        let selected = selected_profile_from_documents(
            active.profile,
            Some(active.lock),
            Some(active.plan),
            active.identity.profile_id.clone(),
            active.profile_revision.clone(),
            Some(active.profile_revision),
            Some(active.activation_id),
            Some(active.plan_digest),
            Some(active.lock_digest),
        )?;
        if selected.profile_id != active.identity.profile_id {
            bail!("active Profile identity does not match its activation snapshot");
        }
        validate_plan_identity(&selected, &active.identity)?;
        ensure_profile_selection_is_known(catalog, &selected, false)?;
        return Ok(selected);
    }

    // A signed catalog can explicitly describe a bootstrap candidate. This is
    // a compatibility adapter for a fresh install only; it derives every
    // identity from the catalog/profile bytes and never substitutes a
    // Defaultspack or Tauri identifier.
    let (bootstrap_profile_id, _, bootstrap_profile_digest) =
        catalog.bootstrap_profile_identity()?;
    let profile_path = bundle_lock
        .authority_roles
        .iter()
        .find(|(path, role)| {
            **role == BundleEntryKind::Profile
                && bundle_lock
                    .authority_digests
                    .get(*path)
                    .is_some_and(|digest| digest == bootstrap_profile_digest)
        })
        .map(|(path, _)| path)
        .context("signed catalog Profile is absent from the Pack bundle")?;
    let profile = read_json(
        &bundle_root.join(profile_path),
        "catalog-selected Profile v5",
    )?;
    let selected = selected_profile_from_documents(
        profile,
        None,
        None,
        bootstrap_profile_id.to_owned(),
        bootstrap_profile_digest.to_owned(),
        None,
        None,
        None,
        None,
    )?;
    if selected.base_pack_id != catalog.default_selection.base_pack_id
        || selected.shell_provider_id != catalog.default_selection.shell_provider_id
    {
        bail!("signed catalog selection differs from its selected Profile");
    }
    ensure_profile_selection_is_known(catalog, &selected, true)?;
    Ok(selected)
}

fn selected_profile_from_documents(
    profile: Value,
    lock: Option<Value>,
    plan: Option<Value>,
    expected_profile_id: String,
    profile_digest: String,
    profile_revision: Option<String>,
    activation_id: Option<String>,
    plan_digest: Option<String>,
    lock_digest: Option<String>,
) -> Result<SelectedProfileAuthority> {
    let profile_id = value_str(&profile, "/profile_id")
        .context("selected Profile is missing profile_id")?
        .to_owned();
    if !valid_identifier(&profile_id) || profile_id != expected_profile_id {
        bail!("selected Profile identity is unknown or inconsistent");
    }
    if !valid_digest(&profile_digest) {
        bail!("selected Profile digest is invalid");
    }
    if let Some(revision) = profile_revision.as_deref() {
        if !valid_digest(revision) || revision != profile_digest {
            bail!("selected Profile revision does not match its definition digest");
        }
    }
    if let Some(activation) = activation_id.as_deref() {
        if !valid_activation_id(activation) {
            bail!("selected Profile activation identity is invalid");
        }
    }
    if let Some(digest) = plan_digest.as_deref() {
        if !valid_digest(digest) {
            bail!("selected Profile plan digest is invalid");
        }
    }
    if lock_digest.is_some() && plan_digest.is_none() {
        bail!("selected Profile Lock identity has no ResolvedPlan");
    }
    let base_pack_id = value_str(&profile, "/base/pack_id")
        .context("selected Profile Base identity is missing")?
        .to_owned();
    let shell_provider_id = value_str(&profile, "/shell/provider_id")
        .context("selected Profile Shell provider identity is missing")?
        .to_owned();
    let shell_pack_id = value_str(&profile, "/shell/pack_id")
        .context("selected Profile Shell Pack identity is missing")?
        .to_owned();
    let packs = profile
        .get("packs")
        .and_then(Value::as_array)
        .context("selected Profile packs must be an array")?;
    let application = packs
        .iter()
        .filter(|pack| value_str(pack, "/role") == Some("application"))
        .collect::<Vec<_>>();
    if application.len() != 1 {
        bail!("selected Profile must bind exactly one Application Pack");
    }
    let application_pack_id = value_str(application[0], "/pack_id")
        .context("selected Application Pack identity is missing")?
        .to_owned();
    let application_artifact_digest =
        value_str(application[0], "/artifact_digest").map(str::to_owned);
    for identity in [
        base_pack_id.as_str(),
        shell_provider_id.as_str(),
        shell_pack_id.as_str(),
        application_pack_id.as_str(),
    ] {
        if !valid_identifier(identity) {
            bail!("selected Profile contains an invalid authority identity");
        }
    }
    if let Some(digest) = application_artifact_digest.as_deref() {
        if !valid_digest(digest) {
            bail!("selected Application Pack artifact digest is invalid");
        }
    }

    let pack_ids = packs
        .iter()
        .filter_map(|pack| value_str(pack, "/pack_id"))
        .map(str::to_owned)
        .collect::<BTreeSet<_>>();
    if pack_ids.len() != packs.len() {
        bail!("selected Profile contains duplicate or incomplete Pack identities");
    }
    let launch_contribution = plan
        .as_ref()
        .map(runtime_launch_contribution_from_plan)
        .transpose()?;
    let selected = SelectedProfileAuthority {
        profile,
        lock,
        plan,
        profile_id,
        profile_digest,
        profile_revision,
        activation_id,
        plan_digest,
        base_pack_id,
        shell_provider_id,
        shell_pack_id,
        application_pack_id,
        application_artifact_digest,
        launch_contribution,
        lock_digest,
        pack_ids,
    };
    if let Some(lock) = selected.lock.as_ref() {
        validate_lock_graph(&selected, lock)?;
    }
    if selected.plan.is_some() {
        let plan = selected.plan.as_ref().expect("plan presence was checked");
        validate_plan_graph(&selected, plan)?;
    }
    Ok(selected)
}

fn runtime_launch_contribution_from_plan(plan: &Value) -> Result<RuntimeLaunchContribution> {
    let Some(raw_contribution) = plan.get("launch_contribution") else {
        return Err(ProfileReresolutionRequired.into());
    };
    if raw_contribution.is_null() {
        return Err(ProfileReresolutionRequired.into());
    }
    let contribution = raw_contribution
        .as_object()
        .context("active ResolvedPlan launch_contribution is malformed")?;
    let required = [
        "provider_id",
        "contract_id",
        "operation_id",
        "platform",
        "architecture",
        "artifact_digest",
        "relative_path",
        "entrypoint",
    ];
    if contribution.len() != required.len()
        || required
            .iter()
            .any(|field| !contribution.contains_key(*field))
    {
        bail!("active ResolvedPlan launch_contribution shape is invalid");
    }
    let field = |name: &str| -> Result<String> {
        contribution
            .get(name)
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .map(str::to_owned)
            .with_context(|| format!("launch_contribution {name} is invalid"))
    };
    let selector = RuntimeLaunchContribution {
        provider_id: field("provider_id")?,
        contract_id: field("contract_id")?,
        operation_id: field("operation_id")?,
        platform: field("platform")?,
        architecture: field("architecture")?,
        artifact_digest: field("artifact_digest")?,
        relative_path: field("relative_path")?,
        entrypoint: field("entrypoint")?,
    };
    if !valid_identifier(&selector.provider_id)
        || !valid_contract_id(&selector.contract_id)
        || !valid_identifier(&selector.operation_id)
        || !valid_digest(&selector.artifact_digest)
    {
        bail!("active ResolvedPlan launch_contribution identity is invalid");
    }
    let artifact_path = safe_relative(&selector.relative_path)?;
    let entrypoint_path = safe_relative(&selector.entrypoint)?;
    if !entrypoint_path.starts_with(&artifact_path) {
        bail!("active ResolvedPlan launch entrypoint escapes its artifact");
    }
    Ok(selector)
}

fn ensure_profile_selection_is_known(
    catalog: &crate::presentation::PresentationCatalog,
    selected: &SelectedProfileAuthority,
    bootstrap: bool,
) -> Result<()> {
    let base = catalog
        .base_packs
        .iter()
        .find(|base| base.pack_id == selected.base_pack_id)
        .context("selected Profile Base identity is not in the signed catalog")?;
    let shell = catalog
        .shell_providers
        .iter()
        .find(|shell| shell.provider_id == selected.shell_provider_id)
        .context("selected Profile Shell identity is not in the signed catalog")?;
    if base.backend_provider_ids.is_empty()
        || shell.artifact_variants.is_empty()
        || (bootstrap
            && (selected.base_pack_id != catalog.default_selection.base_pack_id
                || selected.shell_provider_id != catalog.default_selection.shell_provider_id))
    {
        bail!("selected Profile presentation identities are incomplete");
    }
    Ok(())
}

fn validate_plan_identity(
    selected: &SelectedProfileAuthority,
    identity: &crate::host_contract::ExecutionProfileIdentity,
) -> Result<()> {
    if selected.profile_id != identity.profile_id
        || selected.profile_revision.as_deref() != Some(identity.profile_revision.as_str())
        || selected.activation_id.as_deref() != Some(identity.activation_id.as_str())
        || selected.plan_digest.as_deref() != Some(identity.plan_digest.as_str())
    {
        bail!("active Profile identity is not bound to the selected ResolvedPlan");
    }
    Ok(())
}

fn validate_lock_graph(selected: &SelectedProfileAuthority, lock: &Value) -> Result<()> {
    let expected_lock_digest = selected
        .lock_digest
        .as_deref()
        .context("selected ProfileLock digest is missing")?;
    if !valid_digest(expected_lock_digest)
        || canonical_record_digest(lock, "lock_digest")? != expected_lock_digest
    {
        bail!("selected ProfileLock digest is stale");
    }
    if value_str(lock, "/profile_id") != Some(selected.profile_id.as_str())
        || value_str(lock, "/profile_revision") != selected.profile_revision.as_deref()
        || value_str(lock, "/plan_digest") != selected.plan_digest.as_deref()
    {
        bail!("selected ProfileLock identity does not match the active Profile");
    }
    if let Some(plan) = selected.plan.as_ref() {
        for pointer in [
            "/profile_authority_snapshot_digest",
            "/catalog_revision",
            "/bundle_digest",
            "/application",
            "/effective_set",
            "/requested_edges_digest",
            "/constraints_digest",
            "/closure_digest",
            "/provenance_digest",
            "/security_epoch",
        ] {
            if lock.pointer(pointer) != plan.pointer(pointer) {
                bail!("selected ProfileLock and ResolvedPlan bindings diverge");
            }
        }
    }
    Ok(())
}

fn validate_plan_graph(selected: &SelectedProfileAuthority, plan: &Value) -> Result<()> {
    let plan_api =
        value_str(plan, "/plan_api_version").context("ResolvedPlan API version is missing")?;
    if !plan_api.starts_with("io.tobkiri.resolved-plan.v")
        || plan_api.len() <= "io.tobkiri.resolved-plan.v".len()
    {
        bail!("ResolvedPlan API version is unsupported");
    }
    if value_str(plan, "/profile_id") != Some(selected.profile_id.as_str())
        || value_str(plan, "/profile_revision") != selected.profile_revision.as_deref()
        || value_str(plan, "/base/pack_id") != Some(selected.base_pack_id.as_str())
        || value_str(plan, "/shell/provider_id") != Some(selected.shell_provider_id.as_str())
        || value_str(plan, "/shell/pack_id") != Some(selected.shell_pack_id.as_str())
        || value_str(plan, "/application/pack_id") != Some(selected.application_pack_id.as_str())
    {
        bail!("ResolvedPlan graph does not match the selected Profile");
    }
    let plan_digest = value_str(plan, "/plan_digest").context("ResolvedPlan digest is missing")?;
    if !valid_digest(plan_digest) || selected.plan_digest.as_deref() != Some(plan_digest) {
        bail!("ResolvedPlan digest does not match the active Profile identity");
    }
    let mut unsigned = plan.clone();
    unsigned
        .as_object_mut()
        .context("ResolvedPlan must be an object")?
        .remove("plan_digest");
    if canonical_value_digest(&unsigned)? != plan_digest {
        bail!("ResolvedPlan digest is stale");
    }
    let application_artifact_digest = value_str(plan, "/application/artifact_digest")
        .context("ResolvedPlan Application artifact digest is missing")?;
    if !valid_digest(application_artifact_digest)
        || selected.application_artifact_digest.as_deref() != Some(application_artifact_digest)
    {
        bail!("ResolvedPlan Application artifact digest differs from the Profile");
    }
    let shell_artifact_digest = value_str(plan, "/shell/artifact_digest")
        .context("ResolvedPlan Shell digest is missing")?;
    if !valid_digest(shell_artifact_digest)
        || value_str(&selected.profile, "/shell/artifact_digest") != Some(shell_artifact_digest)
    {
        bail!("ResolvedPlan Shell artifact digest differs from the Profile");
    }
    let launch = selected
        .launch_contribution
        .as_ref()
        .context("active ResolvedPlan launch contribution is unavailable")?;
    if value_str(&selected.profile, "/shell/platform") != Some(launch.platform.as_str())
        || value_str(&selected.profile, "/shell/architecture") != Some(launch.architecture.as_str())
    {
        bail!("ResolvedPlan launch contribution targets a different Profile Shell");
    }
    Ok(())
}

fn validate_profile_pack_closure(
    selected: &SelectedProfileAuthority,
    catalog: &crate::presentation::PresentationCatalog,
    bundle_root: &Path,
    bundle_lock: &VerifiedBundleLock,
) -> Result<()> {
    let mut identities = selected.pack_ids.clone();
    identities.insert(selected.base_pack_id.clone());
    identities.insert(selected.shell_pack_id.clone());
    for pack_id in identities {
        let path = bundle_pack_path(bundle_root, bundle_lock, &pack_id)?;
        let pack = read_json(&path, "selected Profile Pack")?;
        if value_str(&pack, "/pack/id") != Some(pack_id.as_str())
            || value_str(&pack, "/pack_api_version") != Some("io.tobkiri.pack.v4")
            || !profile_pack_migration_is_admissible(selected, &pack_id, &pack)
        {
            bail!("selected Profile Pack identity is inconsistent: {pack_id}");
        }
        if let Some(expected) = catalog.source_manifest_digests.get(&pack_id) {
            let relative = path
                .strip_prefix(bundle_root)
                .context("selected Profile Pack escaped its bundle root")?
                .to_string_lossy()
                .replace('\\', "/");
            if bundle_lock.authority_digests.get(&relative) != Some(expected) {
                bail!("selected Profile Pack source digest differs from the signed catalog");
            }
        }
        let expected_artifact_digest = if pack_id == selected.application_pack_id {
            selected.application_artifact_digest.as_deref()
        } else if pack_id == selected.base_pack_id {
            value_str(&selected.profile, "/base/artifact_digest")
        } else if pack_id == selected.shell_pack_id && selected.plan.is_some() {
            // A bootstrap Profile names the selected platform artifact tree
            // here; an active Profile names the Shell Pack aggregate. Only
            // the latter belongs in the Pack closure comparison below.
            value_str(&selected.profile, "/shell/artifact_digest")
        } else {
            selected
                .profile
                .get("packs")
                .and_then(Value::as_array)
                .and_then(|packs| {
                    packs
                        .iter()
                        .find(|item| value_str(item, "/pack_id") == Some(pack_id.as_str()))
                })
                .and_then(|item| value_str(item, "/artifact_digest"))
        };
        if let Some(expected) = expected_artifact_digest {
            if !valid_digest(expected)
                || value_str(&pack, "/pack/artifact_digest") != Some(expected)
            {
                bail!("selected Profile Pack artifact digest differs from its authority");
            }
        }
    }
    Ok(())
}

fn profile_pack_migration_is_admissible(
    selected: &SelectedProfileAuthority,
    pack_id: &str,
    pack: &Value,
) -> bool {
    // `read_only` describes the retained legacy compatibility projection; it
    // is not an execution trust level. The v4 Pack remains authoritative and
    // reaches this check only after its bytes and bundle role are digest-locked.
    // Keep the exception narrower than execution admission: only an explicitly
    // selected Host Extension provider may retain that projection metadata.
    match value_str(pack, "/migration/compatibility") {
        Some("none") => true,
        Some("read_only") => {
            selected_profile_pack_role(selected, pack_id) == Some("provider")
                && value_str(pack, "/pack/kind") == Some("host_extension")
        }
        _ => false,
    }
}

fn selected_profile_pack_role<'a>(
    selected: &'a SelectedProfileAuthority,
    pack_id: &str,
) -> Option<&'a str> {
    selected
        .profile
        .get("packs")?
        .as_array()?
        .iter()
        .find(|item| value_str(item, "/pack_id") == Some(pack_id))
        .and_then(|item| value_str(item, "/role"))
}

fn bundle_pack_path(
    bundle_root: &Path,
    bundle_lock: &VerifiedBundleLock,
    pack_id: &str,
) -> Result<PathBuf> {
    let relative = bundle_lock
        .pack_paths
        .get(pack_id)
        .with_context(|| format!("selected Profile Pack is not in the signed bundle: {pack_id}"))?;
    Ok(bundle_root.join(safe_relative(relative)?))
}

fn validate_application_pack(
    application_pack_root: &Path,
    contract_map_root: &Path,
    pack: &Value,
    selected_variant: &crate::presentation::ArtifactVariant,
    launch_contribution: Option<&RuntimeLaunchContribution>,
    expected_application_id: &str,
    expected_artifact_digest: Option<&str>,
) -> Result<ApplicationLaunch> {
    let selected_platform = format!(
        "{}-{}",
        selected_variant.platform, selected_variant.architecture
    );
    let functions = pack
        .get("functions")
        .and_then(Value::as_array)
        .context("application Pack functions must be an array")?;
    let providers = pack
        .get("provider_catalog")
        .and_then(Value::as_array)
        .context("application Pack providers must be an array")?;
    let operations = pack
        .get("operation_catalog")
        .and_then(Value::as_array)
        .context("application Pack operations must be an array")?;
    let artifacts = pack
        .get("artifacts")
        .and_then(Value::as_array)
        .context("application Pack artifacts must be an array")?;
    if value_str(pack, "/pack_api_version") != Some("io.tobkiri.pack.v4")
        || value_str(pack, "/pack/id") != Some(expected_application_id)
        || value_str(pack, "/pack/kind") != Some("application")
        || value_str(pack, "/migration/compatibility") != Some("none")
        || functions.len() != 1
        || providers.len() != 1
        || operations.len() != 1
        || artifacts.len() != 2
        || value_str(&functions[0], "/id") != Some(expected_application_id)
        || value_str(&functions[0], "/isolation") != Some("dedicated_process")
        || !contains_string(&functions[0], "/operations", "launch")
        || value_str(&providers[0], "/provider_id") != Some(expected_application_id)
        || value_str(&providers[0], "/owner") != value_str(&providers[0], "/provider_id")
        || !valid_contract_id(value_str(&providers[0], "/contract_reference").unwrap_or_default())
        || !contains_string(&providers[0], "/operations", "launch")
        || value_str(&operations[0], "/operation_id") != Some("launch")
        || value_str(&operations[0], "/owner") != value_str(&providers[0], "/provider_id")
        || value_str(&operations[0], "/provider_id") != value_str(&providers[0], "/provider_id")
        || value_str(&operations[0], "/contract_reference")
            != value_str(&providers[0], "/contract_reference")
    {
        bail!("application Pack launch identity is invalid");
    }
    if let Some(selector) = launch_contribution {
        if selector.provider_id != value_str(&providers[0], "/provider_id").unwrap_or_default()
            || selector.contract_id
                != value_str(&providers[0], "/contract_reference").unwrap_or_default()
            || selector.operation_id
                != value_str(&operations[0], "/operation_id").unwrap_or_default()
            || selector.platform != selected_variant.platform
            || selector.architecture != selected_variant.architecture
        {
            bail!("Application Pack differs from the active launch contribution");
        }
    }

    let executable_index = artifacts
        .iter()
        .enumerate()
        .find(|(_, artifact)| {
            value_str(artifact, "/kind") == Some("executable")
                && value_str(artifact, "/platform") == Some(selected_platform.as_str())
        })
        .map(|(index, _)| index)
        .context("application Pack does not contain the selected executable artifact")?;
    let executable = &artifacts[executable_index];
    let asset_index = artifacts
        .iter()
        .enumerate()
        .find(|(_, artifact)| value_str(artifact, "/kind") == Some("asset"))
        .map(|(index, _)| index)
        .context("application Pack frontend contract asset is missing")?;
    if executable_index == asset_index
        || artifacts.iter().enumerate().any(|(index, artifact)| {
            index != executable_index
                && index != asset_index
                && value_str(artifact, "/kind") != Some("asset")
        })
    {
        bail!("application Pack artifact set contains an unsupported artifact kind");
    }
    let artifact_digest =
        value_str(executable, "/digest").context("application Pack artifact digest is missing")?;
    let entrypoint_digest = value_str(executable, "/entrypoint_digest")
        .context("application Pack entrypoint digest is missing")?;
    if !valid_digest(artifact_digest)
        || !valid_digest(entrypoint_digest)
        || expected_artifact_digest
            .is_some_and(|expected| value_str(pack, "/pack/artifact_digest") != Some(expected))
    {
        bail!("application Pack artifact identity is invalid");
    }
    if let Some(selector) = launch_contribution {
        if selector.artifact_digest != artifact_digest {
            bail!("Application artifact digest differs from the active launch contribution");
        }
    }
    #[cfg(not(test))]
    if selected_variant
        .sha256
        .as_deref()
        .is_some_and(|digest| digest != artifact_digest)
        || selected_variant
            .entrypoint_sha256
            .as_deref()
            .is_some_and(|digest| digest != entrypoint_digest)
        || (!cfg!(debug_assertions)
            && (selected_variant.sha256.is_none()
                || selected_variant.entrypoint_sha256.is_none()))
    {
        bail!("application Pack differs from its signed release artifact");
    }
    #[cfg(test)]
    if selected_variant
        .sha256
        .as_deref()
        .is_some_and(|digest| digest != artifact_digest)
        || selected_variant
            .entrypoint_sha256
            .as_deref()
            .is_some_and(|digest| digest != entrypoint_digest)
    {
        bail!("application Pack differs from its test release artifact");
    }
    if value_str(&functions[0], "/implementation_digest") != Some(entrypoint_digest)
        || value_str(pack, "/pack/artifact_digest")
            != value_str(pack, "/integrity/artifact_set_digest")
        || sha256(&serde_json::to_vec(artifacts)?)
            != value_str(pack, "/integrity/artifact_set_digest").unwrap_or_default()
    {
        bail!("application Pack artifact identity is inconsistent");
    }

    let artifact_path =
        value_str(executable, "/path").context("application Pack artifact path is missing")?;
    let entrypoint =
        value_str(executable, "/entrypoint").context("application Pack entrypoint is missing")?;
    if artifact_path != selected_variant.artifact_ref || entrypoint != selected_variant.entrypoint {
        bail!("application Pack does not identify the selected Shell artifact");
    }
    if let Some(selector) = launch_contribution {
        if selector.relative_path != artifact_path || selector.entrypoint != entrypoint {
            bail!("Application path differs from the active launch contribution");
        }
    }
    let argv = executable
        .get("argv")
        .and_then(Value::as_array)
        .context("application Pack argv must be an array")?;
    if !argv.is_empty() {
        bail!("application Pack launch argv must not contain positional arguments");
    }

    let artifact_relative = safe_relative(artifact_path)?;
    let relative = safe_relative(entrypoint)?;
    if !relative.starts_with(&artifact_relative) {
        bail!("application Pack entrypoint escapes its selected artifact");
    }
    let artifact_root = application_pack_root.join("platform-artifacts");
    let artifact_candidate = artifact_root.join(&artifact_relative);
    let candidate = artifact_root.join(relative);
    let bytes = read_regular_file(&candidate, "application Pack entrypoint")?;
    let canonical = candidate
        .canonicalize()
        .context("failed to canonicalize application Pack entrypoint")?;
    if !canonical.starts_with(&artifact_root)
        || artifact_tree_digest(&artifact_candidate)? != artifact_digest
        || sha256(&bytes) != entrypoint_digest
    {
        bail!("application Pack entrypoint escaped or failed artifact verification");
    }

    let contract_asset = &artifacts[asset_index];
    if value_str(contract_asset, "/platform") != Some("host")
        || contract_asset.get("entrypoint").is_some()
        || contract_asset.get("argv").is_some()
    {
        bail!("application Pack frontend contract asset metadata is invalid");
    }
    let contract_map_path = value_str(contract_asset, "/path")
        .context("application Pack frontend contract map path is missing")?;
    let contract_map_digest = value_str(contract_asset, "/digest")
        .context("application Pack frontend contract map digest is missing")?;
    let contract_map_candidate = contract_map_root.join(safe_relative(contract_map_path)?);
    let contract_map_bytes = read_regular_file(
        &contract_map_candidate,
        "application Pack frontend contract map",
    )?;
    let contract_map_canonical = contract_map_candidate
        .canonicalize()
        .context("failed to canonicalize application Pack frontend contract map")?;
    if !contract_map_canonical.starts_with(contract_map_root)
        || sha256(&contract_map_bytes) != contract_map_digest
    {
        bail!("application Pack frontend contract map escaped or failed artifact verification");
    }
    let contract_map: Value = serde_json::from_slice(&contract_map_bytes)
        .context("application Pack frontend contract map is malformed")?;
    let contract_map_pack_id = application_contract_namespace(
        &contract_map,
        expected_application_id,
        contract_map_path,
        contract_map_digest,
    )?;

    Ok(ApplicationLaunch {
        entrypoint: canonical,
        argv: Vec::new(),
        artifact_id: selected_variant.artifact_id.clone(),
        artifact_digest: artifact_digest.to_string(),
        entrypoint_digest: entrypoint_digest.to_string(),
        function_id: value_str(&functions[0], "/id")
            .expect("application function identity was checked")
            .to_owned(),
        provider_id: value_str(&providers[0], "/provider_id")
            .expect("application provider identity was checked")
            .to_owned(),
        contract_namespace: contract_map_pack_id.to_owned(),
    })
}

fn application_contract_namespace<'a>(
    contract_map: &'a Value,
    expected_application_id: &str,
    artifact_path: &str,
    artifact_digest: &str,
) -> Result<&'a str> {
    let pack_id = value_str(contract_map, "/pack_id")
        .context("application Pack frontend route namespace is missing")?;
    if value_str(contract_map, "/schema") != Some("io.tobkiri.frontend-contract-map.v4")
        || !valid_identifier(pack_id)
        || contract_map
            .get("routes")
            .and_then(Value::as_array)
            .is_none()
    {
        bail!("application Pack frontend contract map identity is invalid");
    }
    if let Some(declared_path) = value_str(contract_map, "/artifact_path") {
        if safe_relative(declared_path)? != safe_relative(artifact_path)? {
            bail!("application Pack frontend contract map artifact path is stale");
        }
    } else if contract_map.get("artifact_path").is_some() {
        bail!("application Pack frontend contract map artifact path is invalid");
    }
    if let Some(declared_digest) = value_str(contract_map, "/artifact_digest") {
        if !valid_digest(declared_digest) || declared_digest != artifact_digest {
            bail!("application Pack frontend contract map artifact digest is stale");
        }
    } else if contract_map.get("artifact_digest").is_some() {
        bail!("application Pack frontend contract map artifact digest is invalid");
    }

    let owner_present = contract_map.get("owner").is_some();
    let application_present = contract_map.get("application_id").is_some();
    if owner_present || application_present {
        let owner = value_str(contract_map, "/owner")
            .filter(|value| valid_identifier(value))
            .context("application Pack frontend contract map owner is invalid")?;
        let application_id = value_str(contract_map, "/application_id")
            .filter(|value| valid_identifier(value))
            .context("application Pack frontend contract map Application is invalid")?;
        if pack_id != expected_application_id
            || owner != expected_application_id
            || application_id != expected_application_id
        {
            bail!("application Pack frontend contract map belongs to another Application");
        }
        return Ok(pack_id);
    }

    let artifact = safe_relative(artifact_path)?;
    let path_namespace = if artifact.components().count() > 1 {
        artifact
            .components()
            .next()
            .and_then(|component| match component {
                Component::Normal(value) => value.to_str(),
                _ => None,
            })
            .context("application Pack frontend contract map path has no namespace")?
    } else {
        expected_application_id
    };
    if pack_id != expected_application_id && pack_id != path_namespace {
        bail!("application Pack frontend contract map belongs to another Application");
    }
    Ok(pack_id)
}

fn contains_string(value: &Value, pointer: &str, expected: &str) -> bool {
    value
        .pointer(pointer)
        .and_then(Value::as_array)
        .is_some_and(|items| items.iter().any(|item| item.as_str() == Some(expected)))
}

fn canonical_directory(path: &Path, label: &str) -> Result<PathBuf> {
    let metadata = fs::symlink_metadata(path)
        .with_context(|| format!("{label} is missing at {}", path.display()))?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        bail!("{label} must be a non-symlink directory");
    }
    path.canonicalize()
        .with_context(|| format!("failed to canonicalize {label}"))
}

fn canonical_child_directory(root: &Path, relative: &Path, label: &str) -> Result<PathBuf> {
    if relative.is_absolute()
        || relative
            .components()
            .any(|component| !matches!(component, Component::Normal(_)))
    {
        bail!("{label} has an unsafe path");
    }
    let child = canonical_directory(&root.join(relative), label)?;
    if !child.starts_with(root) {
        bail!("{label} escapes the packaged application root");
    }
    Ok(child)
}

fn verify_symlink_free_tree(root: &Path, current: &Path) -> Result<()> {
    for entry in fs::read_dir(current)
        .with_context(|| format!("failed to inspect packaged tree at {}", current.display()))?
    {
        let entry = entry.context("failed to inspect packaged tree entry")?;
        let path = entry.path();
        let metadata = fs::symlink_metadata(&path).with_context(|| {
            format!(
                "failed to inspect packaged tree entry at {}",
                path.display()
            )
        })?;
        if metadata.file_type().is_symlink() {
            bail!(
                "packaged tree contains a symlink: {}",
                path.strip_prefix(root).unwrap_or(&path).display()
            );
        }
        if metadata.is_dir() {
            verify_symlink_free_tree(root, &path)?;
        } else if !metadata.is_file() {
            bail!(
                "packaged tree contains an unsupported entry: {}",
                path.strip_prefix(root).unwrap_or(&path).display()
            );
        } else if has_multiple_links(&path, &metadata)? {
            bail!(
                "packaged tree contains a multiply-linked file: {}",
                path.strip_prefix(root).unwrap_or(&path).display()
            );
        }
    }
    Ok(())
}

#[cfg(unix)]
fn has_multiple_links(_path: &Path, metadata: &fs::Metadata) -> Result<bool> {
    use std::os::unix::fs::MetadataExt;

    Ok(metadata.nlink() != 1)
}

#[cfg(windows)]
fn has_multiple_links(path: &Path, _metadata: &fs::Metadata) -> Result<bool> {
    use std::mem::MaybeUninit;
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::{
        GetFileInformationByHandle, BY_HANDLE_FILE_INFORMATION,
    };

    let file = fs::File::open(path).with_context(|| {
        format!(
            "failed to inspect packaged file links at {}",
            path.display()
        )
    })?;
    let mut information = MaybeUninit::<BY_HANDLE_FILE_INFORMATION>::zeroed();
    if unsafe { GetFileInformationByHandle(file.as_raw_handle(), information.as_mut_ptr()) } == 0 {
        return Err(std::io::Error::last_os_error()).with_context(|| {
            format!(
                "failed to inspect packaged file links at {}",
                path.display()
            )
        });
    }
    let information = unsafe { information.assume_init() };
    Ok(information.nNumberOfLinks != 1)
}

fn read_regular_file(path: &Path, label: &str) -> Result<Vec<u8>> {
    let metadata = fs::symlink_metadata(path)
        .with_context(|| format!("{label} is missing at {}", path.display()))?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        bail!("{label} must be a regular non-symlink file");
    }
    fs::read(path).with_context(|| format!("failed to read {label} at {}", path.display()))
}

fn read_json(path: &Path, label: &str) -> Result<Value> {
    serde_json::from_slice(&read_regular_file(path, label)?)
        .with_context(|| format!("{label} is malformed"))
}

fn sha256(bytes: &[u8]) -> String {
    format!("sha256:{:x}", Sha256::digest(bytes))
}

fn canonical_value_digest(value: &Value) -> Result<String> {
    validate_canonical_json(value, 0)?;
    Ok(sha256(&serde_json::to_vec(value)?))
}

fn artifact_tree_digest(path: &Path) -> Result<String> {
    fn visit(root: &Path, path: &Path, hasher: &mut Sha256) -> Result<()> {
        let metadata = fs::symlink_metadata(path)
            .with_context(|| format!("packaged artifact is missing at {}", path.display()))?;
        if metadata.file_type().is_symlink() {
            bail!(
                "packaged artifact may not contain a symlink: {}",
                path.display()
            );
        }
        if metadata.is_file() {
            let relative = path
                .strip_prefix(root)
                .unwrap_or(Path::new(""))
                .to_string_lossy()
                .replace('\\', "/");
            hasher.update(relative.as_bytes());
            hasher.update([0]);
            hasher.update(read_regular_file(path, "packaged artifact file")?);
            return Ok(());
        }
        if !metadata.is_dir() {
            bail!(
                "packaged artifact contains an unsupported entry: {}",
                path.display()
            );
        }
        let mut children = fs::read_dir(path)?.collect::<std::io::Result<Vec<_>>>()?;
        children.sort_by_key(|entry| entry.file_name());
        for child in children {
            visit(root, &child.path(), hasher)?;
        }
        Ok(())
    }

    let mut hasher = Sha256::new();
    visit(path, path, &mut hasher)?;
    Ok(format!("sha256:{:x}", hasher.finalize()))
}

fn safe_relative(value: &str) -> Result<PathBuf> {
    let path = PathBuf::from(value);
    if value.is_empty()
        || value.contains('\\')
        || path.is_absolute()
        || path
            .components()
            .any(|component| !matches!(component, Component::Normal(_)))
        || path.to_string_lossy().replace('\\', "/") != value
    {
        bail!("Pack v4 lock contains an unsafe path: {value:?}");
    }
    Ok(path)
}

fn collect_bundle_files(root: &Path, current: &Path, files: &mut BTreeSet<String>) -> Result<()> {
    for entry in fs::read_dir(current)
        .with_context(|| format!("failed to enumerate Pack v4 root at {}", current.display()))?
    {
        let entry = entry?;
        let path = entry.path();
        let metadata = fs::symlink_metadata(&path)?;
        if metadata.file_type().is_symlink() {
            bail!("Pack v4 authority may not be a symlink: {}", path.display());
        }
        if metadata.is_dir() {
            collect_bundle_files(root, &path, files)?;
        } else if metadata.is_file()
            && path.file_name().and_then(|name| name.to_str()) != Some("bundle.lock.json")
        {
            files.insert(
                path.strip_prefix(root)?
                    .to_string_lossy()
                    .replace('\\', "/"),
            );
        }
    }
    Ok(())
}

fn valid_digest(value: &str) -> bool {
    value.len() == 71
        && value.starts_with("sha256:")
        && value[7..]
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn valid_identifier(value: &str) -> bool {
    if value.len() > 128 {
        return false;
    }
    let mut parts = value.split(['.', '_', '-']);
    let Some(first) = parts.next() else {
        return false;
    };
    !first.is_empty()
        && first.as_bytes()[0].is_ascii_lowercase()
        && first
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit())
        && parts.all(|part| {
            !part.is_empty()
                && part
                    .bytes()
                    .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit())
        })
}

fn valid_activation_id(value: &str) -> bool {
    let Some(suffix) = value.strip_prefix("activation:") else {
        return false;
    };
    (8..=128).contains(&suffix.len())
        && suffix.as_bytes()[0].is_ascii_lowercase()
        && suffix.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || b"._-".contains(&byte)
        })
}

fn valid_contract_id(value: &str) -> bool {
    if value.len() > 128 {
        return false;
    }
    let Some((prefix, version)) = value.rsplit_once(".v") else {
        return false;
    };
    valid_identifier(prefix)
        && !version.is_empty()
        && version.as_bytes()[0] != b'0'
        && version.bytes().all(|byte| byte.is_ascii_digit())
}

fn validate_canonical_json(value: &Value, depth: usize) -> Result<()> {
    if depth > MAX_CANONICAL_JSON_DEPTH {
        bail!("executable catalog JSON exceeds the canonical depth limit");
    }
    match value {
        Value::Null | Value::Bool(_) | Value::String(_) => Ok(()),
        Value::Number(number) => {
            let safe = number
                .as_u64()
                .map(|item| item <= MAX_SAFE_JSON_INTEGER)
                .or_else(|| {
                    number
                        .as_i64()
                        .map(|item| item.unsigned_abs() <= MAX_SAFE_JSON_INTEGER)
                })
                .unwrap_or(false);
            if !safe {
                bail!("executable catalog JSON contains a non-canonical number");
            }
            Ok(())
        }
        Value::Array(items) => {
            for item in items {
                validate_canonical_json(item, depth + 1)?;
            }
            Ok(())
        }
        Value::Object(items) => {
            for item in items.values() {
                validate_canonical_json(item, depth + 1)?;
            }
            Ok(())
        }
    }
}

fn validate_executable_catalog(raw: &[u8], path: &str) -> Result<ExecutableCatalog> {
    if raw.len() > MAX_CANONICAL_JSON_BYTES {
        bail!("executable catalog exceeds the canonical size limit");
    }
    let mut document: Value = serde_json::from_slice(raw)
        .with_context(|| format!("executable catalog is malformed: {path}"))?;
    validate_canonical_json(&document, 0)?;
    let catalog: ExecutableCatalog = serde_json::from_slice(raw)
        .with_context(|| format!("executable catalog violates its strict schema: {path}"))?;
    if catalog.catalog_api_version != EXECUTABLE_CATALOG_API_VERSION
        || !valid_identifier(&catalog.pack_id)
        || !valid_digest(&catalog.source_identity)
        || !valid_digest(&catalog.catalog_digest)
        || catalog
            .materialization_catalog_digest
            .as_deref()
            .is_some_and(|digest| !valid_digest(digest))
    {
        bail!("executable catalog identity is invalid: {path}");
    }
    let expected_path = format!("packs/{}.executables.v4.json", catalog.pack_id);
    if path != expected_path {
        bail!("executable catalog path does not match its Pack identity: {path}");
    }
    let mut variant_ids = BTreeSet::new();
    let mut function_ids = BTreeSet::new();
    for variant in &catalog.variants {
        if !valid_identifier(&variant.variant_id)
            || !variant_ids.insert(variant.variant_id.as_str())
            || !valid_identifier(&variant.function_id)
            || !function_ids.insert(variant.function_id.as_str())
            || safe_relative(&variant.implementation_path).is_err()
            || !valid_digest(&variant.implementation_digest)
            || !matches!(
                variant.execution_kind.as_str(),
                "wasm" | "pack_vm" | "host_extension" | "remote"
            )
            || variant.platform.is_empty()
            || variant.architecture.is_empty()
            || variant.runtime_abi.is_empty()
            || !valid_identifier(&variant.backend)
            || !matches!(
                variant.materialization_mode.as_str(),
                "eager" | "continuous" | "on_demand" | "event_wake"
            )
            || !valid_identifier(&variant.execution_domain_profile)
            || variant.operations.is_empty()
        {
            bail!("executable catalog variant is invalid: {path}");
        }
        let mut operations = BTreeSet::new();
        for operation in &variant.operations {
            if !valid_contract_id(&operation.contract_id)
                || operation.contract_version.is_empty()
                || !valid_digest(&operation.revision_digest)
                || !valid_identifier(&operation.operation_id)
                || !operations.insert((
                    operation.contract_id.as_str(),
                    operation.operation_id.as_str(),
                ))
                || !matches!(
                    operation.effect_class.as_str(),
                    "pure" | "read" | "write" | "external_effect" | "privileged"
                )
                || operation.timeout_default_ms == 0
                || operation.timeout_hard_max_ms == 0
                || !matches!(
                    operation.idempotency.as_str(),
                    "none" | "keyed" | "replayable"
                )
            {
                bail!("executable catalog operation is invalid: {path}");
            }
            for schema in [
                &operation.input_schema,
                &operation.output_schema,
                &operation.error_schema,
            ] {
                validate_canonical_json(&Value::Object(schema.clone().into_iter().collect()), 0)?;
            }
        }
    }
    let object = document
        .as_object_mut()
        .context("executable catalog must be a JSON object")?;
    object.remove("catalog_digest");
    let actual_catalog_digest = sha256(&serde_json::to_vec(&document)?);
    if actual_catalog_digest != catalog.catalog_digest {
        bail!("executable catalog self-digest mismatch: {path}");
    }
    Ok(catalog)
}

fn authority_pack_identity(raw: &[u8], path: &str) -> Result<(String, PackCatalogIdentity)> {
    let document: Value = serde_json::from_slice(raw)
        .with_context(|| format!("Pack authority is malformed: {path}"))?;
    let pack_id =
        value_str(&document, "/pack/id").context("Pack authority is missing its Pack identity")?;
    let source_identity = value_str(&document, "/integrity/source_identity")
        .context("Pack authority is missing its source identity")?;
    if value_str(&document, "/pack_api_version") != Some("io.tobkiri.pack.v4")
        || !valid_identifier(pack_id)
        || !valid_digest(source_identity)
    {
        bail!("bundle entry does not contain a valid Pack authority: {path}");
    }
    let provenance = document.get("provenance").and_then(Value::as_object);
    let is_source_bound_projection = provenance.is_some_and(|provenance| {
        provenance.get("schema").and_then(Value::as_str) == Some("io.tobkiri.provenance.v2")
            && provenance.get("source_kind").and_then(Value::as_str) == Some("generated")
            && provenance.get("source_digest").and_then(Value::as_str) == Some(source_identity)
    });
    Ok((
        pack_id.to_owned(),
        PackCatalogIdentity {
            source_identity: source_identity.to_owned(),
            is_source_bound_projection,
        },
    ))
}

fn validate_authority_role(kind: BundleEntryKind, raw: &[u8], path: &str) -> Result<()> {
    let document: Value = serde_json::from_slice(raw)
        .with_context(|| format!("Pack v4 authority is malformed: {path}"))?;
    let valid = match kind {
        BundleEntryKind::Pack => {
            value_str(&document, "/pack_api_version") == Some("io.tobkiri.pack.v4")
        }
        BundleEntryKind::Base => {
            value_str(&document, "/base_api_version") == Some("io.tobkiri.base.v4")
        }
        BundleEntryKind::Shell => {
            value_str(&document, "/shell_api_version") == Some("io.tobkiri.shell.v5")
        }
        BundleEntryKind::Profile => {
            value_str(&document, "/profile_api_version") == Some(DEFAULT_PROFILE_API_VERSION)
        }
        BundleEntryKind::ExecutableCatalog => false,
    };
    if !valid {
        bail!("bundle entry does not match its declared authority role: {path}");
    }
    Ok(())
}

fn verify_bundle_lock(root: &Path) -> Result<VerifiedBundleLock> {
    let raw = read_regular_file(&root.join("bundle.lock.json"), "Pack v4 bundle lock")?;
    let lock: BundleLock =
        serde_json::from_slice(&raw).context("Pack v4 bundle lock is malformed")?;
    if lock.schema != BUNDLE_SCHEMA || lock.entries.is_empty() {
        bail!("Pack v4 bundle lock schema or entries are invalid");
    }
    let mut authority_digests = BTreeMap::new();
    let mut sidecar_digests = BTreeMap::new();
    let mut pack_paths = BTreeMap::new();
    let mut authority_roles = BTreeMap::new();
    let mut pack_identities = BTreeMap::new();
    let mut executable_catalogs = Vec::new();
    for entry in lock.entries {
        let relative = safe_relative(&entry.path)?;
        let candidate = root.join(&relative);
        let bytes = read_regular_file(&candidate, "Pack v4 locked entry")?;
        if !valid_digest(&entry.digest) || sha256(&bytes) != entry.digest {
            bail!("Pack v4 locked entry digest mismatch: {}", entry.path);
        }
        let canonical = candidate.canonicalize()?;
        if !canonical.starts_with(root) {
            bail!("Pack v4 locked entry escapes its root: {}", entry.path);
        }
        if authority_digests.contains_key(&entry.path) || sidecar_digests.contains_key(&entry.path)
        {
            bail!("Pack v4 bundle lock contains a duplicate path");
        }
        if entry.kind == BundleEntryKind::ExecutableCatalog {
            let catalog = validate_executable_catalog(&bytes, &entry.path)?;
            sidecar_digests.insert(entry.path.clone(), entry.digest);
            executable_catalogs.push((entry.path, catalog));
        } else {
            validate_authority_role(entry.kind, &bytes, &entry.path)?;
            if entry.kind == BundleEntryKind::Pack {
                let (pack_id, source_identity) = authority_pack_identity(&bytes, &entry.path)?;
                if pack_identities.insert(pack_id, source_identity).is_some() {
                    bail!("Pack v4 bundle contains a duplicate Pack identity");
                }
                let document: Value = serde_json::from_slice(&bytes)
                    .context("Pack authority is malformed while indexing the bundle")?;
                let pack_id = value_str(&document, "/pack/id")
                    .expect("Pack identity was validated above")
                    .to_owned();
                pack_paths.insert(pack_id, entry.path.clone());
            }
            authority_roles.insert(entry.path.clone(), entry.kind);
            authority_digests.insert(entry.path, entry.digest);
        }
    }
    let mut sidecar_pack_ids = BTreeSet::new();
    for (path, catalog) in executable_catalogs {
        if !sidecar_pack_ids.insert(catalog.pack_id.clone()) {
            bail!("Pack v4 bundle contains duplicate executable catalogs");
        }
        let pack_identity = pack_identities
            .get(&catalog.pack_id)
            .with_context(|| format!("executable catalog has no Pack authority: {path}"))?;
        if pack_identity.source_identity != catalog.source_identity {
            bail!("executable catalog source identity disagrees with its Pack: {path}");
        }
        if pack_identity.is_source_bound_projection {
            if catalog.materialization_catalog_digest.is_none() {
                bail!(
                    "source-bound projected Pack executable catalog is missing its canonical materialization digest: {path}"
                );
            }
        } else if catalog.materialization_catalog_digest.is_some() {
            bail!(
                "non-projected Pack executable catalog cannot replace its catalog identity: {path}"
            );
        }
    }
    let mut actual = BTreeSet::new();
    collect_bundle_files(root, root, &mut actual)?;
    let locked = authority_digests
        .keys()
        .chain(sidecar_digests.keys())
        .cloned()
        .collect();
    if actual != locked {
        bail!("Pack v4 bundle inventory differs from its lock");
    }
    Ok(VerifiedBundleLock {
        authority_digests,
        sidecar_digests,
        pack_paths,
        authority_roles,
    })
}

fn value_str<'a>(value: &'a Value, pointer: &str) -> Option<&'a str> {
    value.pointer(pointer).and_then(Value::as_str)
}

fn validate_profile<'a>(
    profile: &Value,
    catalog: &'a crate::presentation::PresentationCatalog,
    selected: &SelectedProfileAuthority,
) -> Result<&'a crate::presentation::ArtifactVariant> {
    let profile_id =
        value_str(profile, "/profile_id").context("selected Profile identity is missing")?;
    let state = value_str(profile, "/state").context("selected Profile state is missing")?;
    if value_str(profile, "/profile_api_version") != Some(DEFAULT_PROFILE_API_VERSION)
        || profile_id != selected.profile_id
        || value_str(profile, "/mode") != Some("interactive")
        || (selected.plan.is_some() && state != "resolved")
        || (selected.plan.is_none() && state != "needs_resolution")
        || value_str(profile, "/base/pack_id") != Some(selected.base_pack_id.as_str())
        || value_str(profile, "/shell/provider_id") != Some(selected.shell_provider_id.as_str())
        || value_str(profile, "/shell/pack_id") != Some(selected.shell_pack_id.as_str())
        || value_str(profile, "/shell/contract_id") != Some("app.shell.v1")
    {
        bail!("selected Profile does not bind a valid Base and Shell");
    }
    validate_effective_pack_set(profile)?;
    let shell_platform = value_str(profile, "/shell/platform")
        .context("selected Profile Shell platform is missing")?;
    let shell_architecture = value_str(profile, "/shell/architecture")
        .context("selected Profile Shell architecture is missing")?;
    let shell = catalog
        .shell_providers
        .iter()
        .find(|shell| shell.provider_id == selected.shell_provider_id)
        .context("selected Profile Shell is missing from the signed catalog")?;
    if shell.contract_id != "app.shell.v1" || shell.provider_id != selected.shell_provider_id {
        bail!("selected Profile Shell contract identity is invalid");
    }
    let variants = shell
        .artifact_variants
        .iter()
        .filter(|variant| {
            variant.platform == shell_platform
                && variant.architecture == shell_architecture
                && variant.production
                && variant.prebuilt
                && variant.development_command.is_none()
        })
        .collect::<Vec<_>>();
    if variants.len() != 1 {
        bail!("selected Profile has no unique packaged Shell variant");
    }
    if selected.plan.is_none() {
        let artifact_digest = value_str(profile, "/shell/artifact_digest")
            .context("bootstrap Profile Shell artifact digest is missing")?;
        let entrypoint_digest = value_str(profile, "/shell/executable_artifact_digest")
            .context("bootstrap Profile Shell entrypoint digest is missing")?;
        if !valid_digest(artifact_digest)
            || !valid_digest(entrypoint_digest)
            || !bootstrap_shell_variant_matches(variants[0], artifact_digest, entrypoint_digest)
        {
            bail!("bootstrap Profile Shell differs from its signed artifact variant");
        }
    } else {
        let executable_digest = value_str(profile, "/shell/executable_artifact_digest")
            .context("active Profile Shell executable digest is missing")?;
        let launch = selected
            .launch_contribution
            .as_ref()
            .context("active Profile has no runtime launch contribution")?;
        if !valid_digest(executable_digest)
            || variants[0].entrypoint_sha256.as_deref() != Some(executable_digest)
            || variants[0].sha256.as_deref() != Some(launch.artifact_digest.as_str())
            || variants[0].artifact_ref != launch.relative_path
            || variants[0].entrypoint != launch.entrypoint
        {
            bail!("active Profile Shell differs from its signed executable artifact");
        }
    }
    Ok(variants[0])
}

fn bootstrap_shell_variant_matches(
    variant: &crate::presentation::ArtifactVariant,
    artifact_digest: &str,
    entrypoint_digest: &str,
) -> bool {
    variant.sha256.as_deref() == Some(artifact_digest)
        && variant.entrypoint_sha256.as_deref() == Some(entrypoint_digest)
}

fn validate_effective_pack_set(profile: &Value) -> Result<()> {
    let packs = profile
        .get("packs")
        .and_then(Value::as_array)
        .context("Defaults Profile packs must be an array")?;
    let mut effective = BTreeSet::new();
    let mut application_count = 0;
    for item in packs {
        let pack_id =
            value_str(item, "/pack_id").context("selected Profile Pack is missing pack_id")?;
        let role = value_str(item, "/role").context("selected Profile Pack is missing role")?;
        if !valid_identifier(pack_id)
            || !matches!(
                role,
                "backend" | "contribution" | "provider" | "application"
            )
            || pack_id.starts_with("shell.cli.")
            || pack_id.starts_with("dev.")
            || !effective.insert((pack_id, role))
        {
            bail!("selected Profile effective Pack set is invalid");
        }
        if role == "application" {
            application_count += 1;
        }
    }
    if application_count != 1 {
        bail!("selected Profile effective Pack set must contain one Application");
    }
    Ok(())
}

fn verify_pack_artifact_index(
    pack_root: &Path,
    bundle_root: &Path,
    expected_pack_id: &str,
) -> Result<BTreeMap<String, VerifiedPackArtifact>> {
    let index = read_json(
        &pack_root.join("artifact-index.v4.json"),
        "selected Pack artifact index",
    )?;
    if value_str(&index, "/index_api_version") != Some("io.tobkiri.pack-artifact-index.v4")
        || value_str(&index, "/pack_id") != Some(expected_pack_id)
    {
        bail!("selected Pack artifact index identity is invalid");
    }
    let signed_digest = value_str(&index, "/integrity_seal/signed_digest")
        .context("Defaultspack artifact index seal is missing")?;
    let mut unsigned_index = index.clone();
    unsigned_index
        .as_object_mut()
        .context("selected Pack artifact index must be an object")?
        .remove("integrity_seal");
    if sha256(&serde_json::to_vec(&unsigned_index)?) != signed_digest {
        bail!("selected Pack artifact index integrity seal is stale");
    }
    let entries = index
        .get("artifacts")
        .and_then(Value::as_array)
        .context("selected Pack artifact index entries must be an array")?;
    let mut actual = BTreeMap::new();
    for entry in entries {
        let relative = value_str(entry, "/path").context("artifact index path is missing")?;
        let expected = value_str(entry, "/digest").context("artifact index digest is missing")?;
        let role = value_str(entry, "/role").context("artifact index role is missing")?;
        let bytes = read_regular_file(&pack_root.join(safe_relative(relative)?), "Pack artifact")?;
        if sha256(&bytes) != expected
            || actual
                .insert(
                    relative.to_owned(),
                    VerifiedPackArtifact {
                        digest: expected.to_owned(),
                        role: role.to_owned(),
                    },
                )
                .is_some()
        {
            bail!("selected Pack artifact index contains a duplicate or stale artifact");
        }
    }
    if !actual.contains_key("pack.v4.json") {
        bail!("selected Pack artifact index is missing pack.v4.json");
    }
    let root_pack = read_regular_file(&pack_root.join("pack.v4.json"), "Defaultspack Pack")?;
    let bundle_lock = verify_bundle_lock(bundle_root)?;
    let bundled_pack_path = bundle_pack_path(bundle_root, &bundle_lock, expected_pack_id)?;
    let bundled_pack = read_regular_file(&bundled_pack_path, "locked selected Pack")?;
    verify_materialized_root_pack_binding(&root_pack, &bundled_pack)?;
    let pack: Value =
        serde_json::from_slice(&root_pack).context("Defaultspack Pack v4 is malformed")?;
    let artifact_set_digest = value_str(&index, "/artifact_set_digest");
    if artifact_set_digest != value_str(&pack, "/integrity/artifact_set_digest")
        || artifact_set_digest != value_str(&pack, "/pack/artifact_digest")
        || value_str(&index, "/source_identity") != value_str(&pack, "/integrity/source_identity")
    {
        bail!("selected Pack artifact index is stale for its Pack v4 authority");
    }
    Ok(actual)
}

/// Bind the materialized Pack source to the Profile-locked Pack authority.
///
/// Older bundles carried the materialized Pack bytes directly.  Current Pack
/// v4 bundles carry a generated, source-bound projection instead, because the
/// projection pins generated sidecars without mutating the canonical Pack in
/// the materialized runtime tree.  Both forms are safe, but a projection must
/// prove that its immutable source digest is the exact materialized bytes.
fn verify_materialized_root_pack_binding(root_pack: &[u8], bundled_pack: &[u8]) -> Result<()> {
    if root_pack == bundled_pack {
        return Ok(());
    }
    let root: Value = serde_json::from_slice(root_pack)
        .context("materialized root Pack is malformed while binding its Profile projection")?;
    let bundled: Value = serde_json::from_slice(bundled_pack)
        .context("locked Profile Pack is malformed while binding its materialized source")?;
    let root_pack_id = value_str(&root, "/pack/id").context(
        "materialized root Pack identity is missing while binding its Profile projection",
    )?;
    let root_digest = sha256(root_pack);
    let projection_is_bound = value_str(&bundled, "/pack/id") == Some(root_pack_id)
        && value_str(&bundled, "/provenance/schema") == Some("io.tobkiri.provenance.v2")
        && value_str(&bundled, "/provenance/source_kind") == Some("generated")
        && bundled
            .pointer("/provenance/normative")
            .and_then(Value::as_bool)
            == Some(true)
        && value_str(&bundled, "/provenance/source_digest") == Some(root_digest.as_str())
        && value_str(&bundled, "/integrity/source_identity") == Some(root_digest.as_str());
    if !projection_is_bound {
        bail!("materialized root Pack differs from the locked Profile Pack");
    }
    Ok(())
}

#[cfg(test)]
fn fixture_catalog_with_shell_variant(
    mut catalog: crate::presentation::PresentationCatalog,
    bundle_root: &Path,
    bundle_lock: &VerifiedBundleLock,
) -> Result<crate::presentation::PresentationCatalog> {
    let profile = read_json(&bundle_root.join(PROFILE_PATH), "fixture bootstrap Profile")?;
    let platform = value_str(&profile, "/shell/platform")
        .context("fixture bootstrap Profile Shell platform is missing")?;
    let architecture = value_str(&profile, "/shell/architecture")
        .context("fixture bootstrap Profile Shell architecture is missing")?;
    let application_pack_ids = profile
        .get("packs")
        .and_then(Value::as_array)
        .context("fixture bootstrap Profile Pack set is missing")?
        .iter()
        .filter(|pack| value_str(pack, "/role") == Some("application"))
        .filter_map(|pack| value_str(pack, "/pack_id"))
        .collect::<Vec<_>>();
    if application_pack_ids.len() != 1 {
        bail!("fixture bootstrap Profile must select one Application Pack");
    }
    let application_pack = read_json(
        &bundle_pack_path(bundle_root, bundle_lock, application_pack_ids[0])?,
        "fixture Application Pack",
    )?;
    let selected_platform = format!("{platform}-{architecture}");
    let executable_artifacts = application_pack
        .get("artifacts")
        .and_then(Value::as_array)
        .context("fixture Application Pack artifacts are missing")?
        .iter()
        .filter(|artifact| {
            value_str(artifact, "/kind") == Some("executable")
                && value_str(artifact, "/platform") == Some(selected_platform.as_str())
        })
        .collect::<Vec<_>>();
    if executable_artifacts.len() != 1 {
        bail!("fixture Application Pack must select one executable artifact");
    }
    let artifact_digest = value_str(executable_artifacts[0], "/digest")
        .filter(|digest| valid_digest(digest))
        .context("fixture Application Pack artifact digest is invalid")?;
    let entrypoint_digest = value_str(executable_artifacts[0], "/entrypoint_digest")
        .filter(|digest| valid_digest(digest))
        .context("fixture Application Pack entrypoint digest is invalid")?;
    let default_shell_provider_id = catalog.default_selection.shell_provider_id.clone();
    let shell = catalog
        .shell_providers
        .iter_mut()
        .find(|shell| shell.provider_id == default_shell_provider_id)
        .context("fixture catalog default Shell is missing")?;
    let variant = shell
        .artifact_variants
        .iter_mut()
        .find(|variant| variant.platform == platform && variant.architecture == architecture)
        .context("fixture catalog has no selected Shell variant")?;
    let metadata_absent = variant.path.is_none()
        && variant.sha256.is_none()
        && variant.entrypoint_sha256.is_none()
        && variant.size.is_none()
        && variant.source_identity.is_none()
        && variant.source_revision.is_none();
    let metadata_complete = variant.path.is_some()
        && variant.sha256.is_some()
        && variant.entrypoint_sha256.is_some()
        && variant.size.is_some()
        && variant.source_identity.is_some()
        && variant.source_revision.is_some();
    if !metadata_absent && !metadata_complete {
        bail!("fixture catalog Shell variant has partial installed artifact metadata");
    }
    if metadata_absent {
        variant.sha256 = Some(artifact_digest.to_owned());
        variant.entrypoint_sha256 = Some(entrypoint_digest.to_owned());
    }
    Ok(catalog)
}

#[cfg(test)]
mod tests {
    use super::packaging_toolchain;
    use super::*;
    use std::process::Command;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[cfg(unix)]
    use std::os::unix::fs::PermissionsExt;

    const RELOCATION_LAYOUTS: &[(&str, &[&str])] = &[
        (
            "macos-bundle",
            &[
                "Relocated",
                "Tobkiri Launcher.app",
                "Contents",
                "Resources",
                "app",
            ],
        ),
        ("linux-prefix", &["opt", "tobkiri", "resources", "app"]),
        (
            "windows-install",
            &["Program Files", "Tobkiri Launcher", "resources", "app"],
        ),
    ];
    const SOURCE_MANIFEST_RELATIVE: &str =
        "tobkiri_runtime/packaged_defaultspack_source_manifest.v1.json";
    const SOURCE_MANIFEST_SCHEMA: &str = "io.tobkiri.packaged-defaultspack-source.v1";
    const ISOLATED_MODULE_CODE: &str = "import runpy,sys;source_root=sys.argv[1];module_name=sys.argv[2];sys.path.insert(0,source_root);sys.argv=[module_name,*sys.argv[3:]];runpy.run_module(module_name,run_name='__main__',alter_sys=True)";
    const ISOLATED_ENVIRONMENT_KEYS: &[&str] = &[
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "SystemRoot",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "WINDIR",
    ];

    fn verified_git() -> PathBuf {
        packaging_toolchain::verified_tool_executable("git")
            .expect("formal packaging Git binding should be available")
    }

    fn verified_python() -> packaging_toolchain::VerifiedTool {
        packaging_toolchain::verified_tool("python")
            .expect("formal packaging Python binding should be available")
    }

    fn rewrite_locked_document(
        config: &AppConfig,
        relative: &str,
        mutate: impl FnOnce(&mut Value),
    ) {
        let bundle = config.app_dir.join("ecosystem/defaultspack/v4");
        let path = bundle.join(relative);
        let mut document: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        mutate(&mut document);
        let raw = serde_json::to_vec(&document).unwrap();
        fs::write(&path, &raw).unwrap();

        let lock_path = bundle.join("bundle.lock.json");
        let mut lock: Value = serde_json::from_slice(&fs::read(&lock_path).unwrap()).unwrap();
        let entry = lock["entries"]
            .as_array_mut()
            .unwrap()
            .iter_mut()
            .find(|entry| entry["path"] == relative)
            .unwrap();
        entry["digest"] = Value::String(sha256(&raw));
        fs::write(lock_path, serde_json::to_vec(&lock).unwrap()).unwrap();

        let catalog_path = config.app_dir.join("bundled/presentation_catalog.json");
        let mut catalog: Value = serde_json::from_slice(&fs::read(&catalog_path).unwrap()).unwrap();
        if relative == RUNTIME_PACK_PATH {
            catalog["source_manifest_digests"][DEFAULT_RUNTIME_ID] = Value::String(sha256(&raw));
        } else if relative == PROFILE_PATH {
            catalog["default_profile_digest"] = Value::String(sha256(&raw));
        }
        fs::write(catalog_path, serde_json::to_vec(&catalog).unwrap()).unwrap();
    }

    fn rewrite_runtime_pack(config: &AppConfig, mutate: impl FnOnce(&mut Value)) {
        rewrite_locked_document(config, RUNTIME_PACK_PATH, mutate);
    }

    fn minimal_executable_catalog(source_identity: &str) -> Value {
        let mut document = serde_json::json!({
            "catalog_api_version": EXECUTABLE_CATALOG_API_VERSION,
            "pack_id": "test_pack",
            "source_identity": source_identity,
            "variants": [{
                "variant_id": "test_pack.provider.python",
                "function_id": "test_pack.provider",
                "implementation_path": "runtime/provider.py",
                "implementation_digest": format!("sha256:{}", "2".repeat(64)),
                "execution_kind": "pack_vm",
                "platform": "any",
                "architecture": "any",
                "runtime_abi": "python3.13",
                "backend": "tobkiri.python-pack-v4",
                "materialization_mode": "on_demand",
                "execution_domain_profile": "sandbox.default.v1",
                "operations": [{
                    "contract_id": "test.contract.v1",
                    "contract_version": "1.0.0",
                    "revision_digest": format!("sha256:{}", "3".repeat(64)),
                    "operation_id": "invoke",
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object"},
                    "error_schema": {"type": "object"},
                    "effect_class": "pure",
                    "timeout_default_ms": 1000,
                    "timeout_hard_max_ms": 2000,
                    "idempotency": "none"
                }]
            }]
        });
        let digest = sha256(&serde_json::to_vec(&document).unwrap());
        document["catalog_digest"] = Value::String(digest);
        document
    }

    fn minimal_sidecar_bundle(name: &str) -> PathBuf {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!(
            "tobkiri-executable-sidecar-{name}-{}-{unique}",
            std::process::id()
        ));
        let packs = root.join("packs");
        fs::create_dir_all(&packs).unwrap();
        let source_identity = format!("sha256:{}", "1".repeat(64));
        let pack_path = "packs/test_pack.pack.v4.json";
        let sidecar_path = "packs/test_pack.executables.v4.json";
        let pack_raw = serde_json::to_vec(&serde_json::json!({
            "pack_api_version": "io.tobkiri.pack.v4",
            "pack": {"id": "test_pack"},
            "integrity": {"source_identity": source_identity.clone()}
        }))
        .unwrap();
        let sidecar_raw =
            serde_json::to_vec(&minimal_executable_catalog(&source_identity)).unwrap();
        fs::write(root.join(pack_path), &pack_raw).unwrap();
        fs::write(root.join(sidecar_path), &sidecar_raw).unwrap();
        fs::write(
            root.join("bundle.lock.json"),
            serde_json::to_vec(&serde_json::json!({
                "schema": BUNDLE_SCHEMA,
                "entries": [
                    {
                        "path": pack_path,
                        "kind": "pack",
                        "digest": sha256(&pack_raw)
                    },
                    {
                        "path": sidecar_path,
                        "kind": "executable_catalog",
                        "digest": sha256(&sidecar_raw)
                    }
                ]
            }))
            .unwrap(),
        )
        .unwrap();
        root.canonicalize().unwrap()
    }

    fn rewrite_minimal_lock(root: &Path, mutate: impl FnOnce(&mut Value)) {
        let path = root.join("bundle.lock.json");
        let mut lock: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        mutate(&mut lock);
        fs::write(path, serde_json::to_vec(&lock).unwrap()).unwrap();
    }

    fn rewrite_minimal_sidecar(root: &Path, mutate: impl FnOnce(&mut Value)) {
        let relative = "packs/test_pack.executables.v4.json";
        let path = root.join(relative);
        let mut document: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        mutate(&mut document);
        let raw = serde_json::to_vec(&document).unwrap();
        fs::write(path, &raw).unwrap();
        rewrite_minimal_lock(root, |lock| {
            let entry = lock["entries"]
                .as_array_mut()
                .unwrap()
                .iter_mut()
                .find(|entry| entry["path"] == relative)
                .unwrap();
            entry["digest"] = Value::String(sha256(&raw));
        });
    }

    fn rewrite_minimal_pack(root: &Path, mutate: impl FnOnce(&mut Value)) {
        let relative = "packs/test_pack.pack.v4.json";
        let path = root.join(relative);
        let mut document: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        mutate(&mut document);
        let raw = serde_json::to_vec(&document).unwrap();
        fs::write(&path, &raw).unwrap();
        rewrite_minimal_lock(root, |lock| {
            let entry = lock["entries"]
                .as_array_mut()
                .unwrap()
                .iter_mut()
                .find(|entry| entry["path"] == relative)
                .unwrap();
            entry["digest"] = Value::String(sha256(&raw));
        });
    }

    fn minimal_projected_sidecar_bundle(name: &str) -> PathBuf {
        let root = minimal_sidecar_bundle(name);
        let source_identity = format!("sha256:{}", "1".repeat(64));
        rewrite_minimal_pack(&root, |pack| {
            pack["provenance"] = serde_json::json!({
                "schema": "io.tobkiri.provenance.v2",
                "source_kind": "generated",
                "source_digest": source_identity,
            });
        });
        rewrite_minimal_sidecar(&root, |catalog| {
            catalog["materialization_catalog_digest"] =
                Value::String(format!("sha256:{}", "4".repeat(64)));
            let object = catalog.as_object_mut().unwrap();
            object.remove("catalog_digest");
            let digest = sha256(&serde_json::to_vec(&catalog).unwrap());
            catalog["catalog_digest"] = Value::String(digest);
        });
        root
    }

    fn source_manifest_entries(source_checkout: &Path) -> BTreeMap<String, Value> {
        let manifest_path = source_checkout.join(SOURCE_MANIFEST_RELATIVE);
        let manifest = read_json(&manifest_path, "packaged Defaults source manifest").unwrap();
        let object = manifest
            .as_object()
            .expect("source manifest must be an object");
        let actual_fields = object.keys().map(String::as_str).collect::<BTreeSet<_>>();
        let expected_fields = ["schema", "roots", "files"]
            .into_iter()
            .collect::<BTreeSet<_>>();
        assert_eq!(
            actual_fields, expected_fields,
            "source manifest fields drifted"
        );
        assert_eq!(
            object.get("schema").and_then(Value::as_str),
            Some(SOURCE_MANIFEST_SCHEMA),
            "source manifest schema drifted"
        );
        assert_eq!(
            object.get("roots"),
            Some(&serde_json::json!([
                "scripts",
                "tobkiri_protocol",
                "ecosystem/defaultspack/domain/runtime_v4",
                "ecosystem/defaultspack/v4",
                "ecosystem/defaultspack/runtime",
                "ecosystem/defaultspack/defaultspack",
            ])),
            "source manifest roots drifted"
        );
        let files = object
            .get("files")
            .and_then(Value::as_array)
            .expect("source manifest files must be an array");
        let mut entries = BTreeMap::new();
        let mut previous: Option<&str> = None;
        for entry in files {
            let entry_object = entry
                .as_object()
                .expect("source manifest entry must be an object");
            let fields = entry_object
                .keys()
                .map(String::as_str)
                .collect::<BTreeSet<_>>();
            let expected = ["path", "type", "size", "sha256", "executable"]
                .into_iter()
                .collect::<BTreeSet<_>>();
            assert_eq!(fields, expected, "source manifest file fields drifted");
            let relative = entry_object
                .get("path")
                .and_then(Value::as_str)
                .expect("source manifest path must be a string");
            if let Some(value) = previous {
                assert!(value < relative, "source manifest paths must be sorted");
            }
            previous = Some(relative);
            assert!(
                !relative.is_empty()
                    && !relative.contains('\\')
                    && !Path::new(relative).is_absolute()
                    && Path::new(relative)
                        .components()
                        .all(|component| matches!(component, Component::Normal(_))),
                "source manifest path is unsafe: {relative}"
            );
            assert_eq!(
                entry_object.get("type").and_then(Value::as_str),
                Some("regular-file"),
                "source manifest entry type drifted: {relative}"
            );
            let digest = entry_object
                .get("sha256")
                .and_then(Value::as_str)
                .expect("source manifest digest must be a string");
            assert!(
                digest.len() == 64
                    && digest.bytes().all(|character| character.is_ascii_hexdigit()
                        && !character.is_ascii_uppercase()),
                "source manifest digest must be lowercase raw SHA-256: {relative}"
            );
            assert!(
                entries.insert(relative.to_owned(), entry.clone()).is_none(),
                "source manifest contains duplicate path: {relative}"
            );
        }
        assert!(!entries.is_empty(), "source manifest must contain files");
        entries
    }

    fn source_file_digest(path: &Path) -> String {
        format!(
            "{:x}",
            Sha256::digest(&fs::read(path).expect("source file should be readable"))
        )
    }

    #[cfg(unix)]
    fn source_file_executable(metadata: &fs::Metadata) -> bool {
        use std::os::unix::fs::PermissionsExt;

        metadata.permissions().mode() & 0o111 != 0
    }

    #[cfg(not(unix))]
    fn source_file_executable(_metadata: &fs::Metadata) -> bool {
        false
    }

    fn set_fixture_permissions(path: &Path, mode: u32) {
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;

            fs::set_permissions(path, fs::Permissions::from_mode(mode)).unwrap();
        }
        #[cfg(not(unix))]
        {
            let mut permissions = fs::metadata(path).unwrap().permissions();
            permissions.set_readonly(mode & 0o200 == 0);
            fs::set_permissions(path, permissions).unwrap();
        }
    }

    #[cfg(unix)]
    type FixtureSourceOwnerIdentity = (u64, u64);
    #[cfg(not(unix))]
    type FixtureSourceOwnerIdentity = (u64, Option<SystemTime>);

    fn fixture_cleanup_error(path: &Path, reason: &str) -> std::io::Error {
        std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            format!("fixture cleanup refused {reason}: {}", path.display()),
        )
    }

    fn fixture_source_owner_identity(path: &Path) -> std::io::Result<FixtureSourceOwnerIdentity> {
        let metadata = fs::symlink_metadata(path)?;
        if metadata.file_type().is_symlink() {
            return Err(fixture_cleanup_error(path, "a symlink owner"));
        }
        if !metadata.is_dir() || path.canonicalize()? != path {
            return Err(fixture_cleanup_error(path, "a non-canonical owner"));
        }

        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;

            Ok((metadata.dev(), metadata.ino()))
        }
        #[cfg(not(unix))]
        {
            Ok((metadata.len(), metadata.modified().ok()))
        }
    }

    fn set_fixture_permissions_for_cleanup(path: &Path, mode: u32) -> std::io::Result<()> {
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;

            fs::set_permissions(path, fs::Permissions::from_mode(mode))
        }
        #[cfg(not(unix))]
        {
            let mut permissions = fs::symlink_metadata(path)?.permissions();
            permissions.set_readonly(false);
            fs::set_permissions(path, permissions)
        }
    }

    fn verify_fixture_cleanup_entry(
        owner_root: &Path,
        path: &Path,
        metadata: &fs::Metadata,
        owner_identity: &FixtureSourceOwnerIdentity,
    ) -> std::io::Result<()> {
        if metadata.file_type().is_symlink() {
            return Err(fixture_cleanup_error(path, "a symlink"));
        }
        if !path.starts_with(owner_root) {
            return Err(fixture_cleanup_error(path, "an owner escape"));
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;

            if metadata.dev() != owner_identity.0 {
                return Err(fixture_cleanup_error(path, "a filesystem boundary"));
            }
        }
        #[cfg(not(unix))]
        let _ = owner_identity;
        Ok(())
    }

    fn restore_fixture_tree_permissions(
        owner_root: &Path,
        root: &Path,
        owner_identity: &FixtureSourceOwnerIdentity,
    ) -> std::io::Result<()> {
        let metadata = fs::symlink_metadata(root)?;
        verify_fixture_cleanup_entry(owner_root, root, &metadata, owner_identity)?;
        if !metadata.is_dir() {
            return Err(fixture_cleanup_error(root, "a non-directory"));
        }
        set_fixture_permissions_for_cleanup(root, 0o700)?;
        for entry in fs::read_dir(root)? {
            let entry = entry?;
            let path = entry.path();
            let metadata = fs::symlink_metadata(&path)?;
            verify_fixture_cleanup_entry(owner_root, &path, &metadata, owner_identity)?;
            if metadata.is_dir() {
                restore_fixture_tree_permissions(owner_root, &path, owner_identity)?;
            } else if metadata.is_file() {
                if has_multiple_links(&path, &metadata).map_err(|error| {
                    std::io::Error::new(std::io::ErrorKind::InvalidData, error.to_string())
                })? {
                    return Err(fixture_cleanup_error(&path, "a hardlink"));
                }
                set_fixture_permissions_for_cleanup(&path, 0o600)?;
            } else {
                return Err(fixture_cleanup_error(&path, "a special entry"));
            }
        }
        Ok(())
    }

    struct FixtureSourceOwner {
        path: PathBuf,
        identity: FixtureSourceOwnerIdentity,
        cleaned: bool,
    }

    impl FixtureSourceOwner {
        fn new(destination_parent: &Path, owner: PathBuf) -> std::io::Result<Self> {
            let metadata = fs::symlink_metadata(&owner)?;
            if metadata.file_type().is_symlink() {
                return Err(fixture_cleanup_error(&owner, "a symlink owner"));
            }
            let parent = destination_parent.canonicalize()?;
            let canonical_owner = owner.canonicalize()?;
            if canonical_owner.file_name().and_then(|name| name.to_str())
                != Some("sealed-source-owner")
                || canonical_owner.parent() != Some(parent.as_path())
            {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::InvalidInput,
                    format!(
                        "fixture source owner is not the expected direct child of {}: {}",
                        parent.display(),
                        canonical_owner.display()
                    ),
                ));
            }
            let identity = fixture_source_owner_identity(&canonical_owner)?;
            Ok(Self {
                path: canonical_owner,
                identity,
                cleaned: false,
            })
        }

        fn cleanup(&mut self) -> std::io::Result<()> {
            if self.cleaned {
                return Ok(());
            }
            let current_identity = fixture_source_owner_identity(&self.path)?;
            if current_identity != self.identity {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::InvalidInput,
                    format!(
                        "fixture source owner identity changed before cleanup: {}",
                        self.path.display()
                    ),
                ));
            }
            restore_fixture_tree_permissions(&self.path, &self.path, &self.identity)?;
            let restored_identity = fixture_source_owner_identity(&self.path)?;
            if restored_identity != self.identity {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::InvalidInput,
                    format!(
                        "fixture source owner identity changed during cleanup: {}",
                        self.path.display()
                    ),
                ));
            }
            fs::remove_dir_all(&self.path)?;
            self.cleaned = true;
            Ok(())
        }
    }

    impl Drop for FixtureSourceOwner {
        fn drop(&mut self) {
            if !self.cleaned {
                if let Err(error) = self.cleanup() {
                    eprintln!(
                        "fixture source owner teardown failed at {}: {error}",
                        self.path.display()
                    );
                }
            }
        }
    }

    fn seal_fixture_directories(root: &Path) {
        for entry in fs::read_dir(root).unwrap() {
            let entry = entry.unwrap();
            if entry.file_type().unwrap().is_dir() {
                seal_fixture_directories(&entry.path());
            }
        }
        set_fixture_permissions(root, 0o555);
    }

    fn fixture_source_tree(source_checkout: &Path) -> String {
        let output = Command::new(verified_git())
            .args(["rev-parse", "--verify", "HEAD^{tree}"])
            .current_dir(source_checkout)
            .output()
            .expect("authoritative fixture source tree should be readable");
        assert!(
            output.status.success(),
            "authoritative fixture source tree lookup failed"
        );
        let tree = String::from_utf8(output.stdout)
            .expect("authoritative fixture source tree should be UTF-8")
            .trim()
            .to_owned();
        assert!(
            tree.len() == 40
                && tree
                    .bytes()
                    .all(|character| character.is_ascii_hexdigit()
                        && !character.is_ascii_uppercase()),
            "authoritative fixture source tree must be a full lowercase SHA"
        );
        tree
    }

    fn materialize_fixture_source_provenance(
        source_checkout: &Path,
        destination_parent: &Path,
        source_revision: &str,
    ) -> (PathBuf, PathBuf, FixtureSourceOwner) {
        let owner = destination_parent.join("sealed-source-owner");
        fs::create_dir_all(&owner).unwrap();
        set_fixture_permissions(&owner, 0o700);
        let owner_guard = FixtureSourceOwner::new(destination_parent, owner.clone())
            .expect("fixture source owner should be safe to guard");
        let source_root = owner.join("source");
        fs::create_dir_all(&source_root).unwrap();

        let source_runtime = source_checkout.join("tobkiri_runtime");
        let entries = source_manifest_entries(source_checkout);
        for (relative, entry) in entries {
            let source = source_runtime.join(&relative);
            let metadata = fs::symlink_metadata(&source).unwrap();
            assert!(
                metadata.is_file()
                    && !metadata.file_type().is_symlink()
                    && !has_multiple_links(&source, &metadata).unwrap(),
                "fixture source entry is not a regular non-hardlinked file: {}",
                source.display()
            );
            let destination = source_root.join(&relative);
            fs::create_dir_all(destination.parent().unwrap()).unwrap();
            fs::copy(&source, &destination).unwrap();
            let executable = entry
                .get("executable")
                .and_then(Value::as_bool)
                .expect("source manifest executable flag should be boolean");
            set_fixture_permissions(&destination, if executable { 0o555 } else { 0o444 });
        }

        let manifest = source_runtime.join("packaged_defaultspack_source_manifest.v1.json");
        let manifest_bytes = fs::read(&manifest).unwrap();
        let destination_manifest =
            source_root.join("packaged_defaultspack_source_manifest.v1.json");
        fs::write(&destination_manifest, &manifest_bytes).unwrap();
        set_fixture_permissions(&destination_manifest, 0o444);

        let provenance_path = source_root.join("packaging-source-provenance.v1.json");
        let provenance = serde_json::json!({
            "schema": "io.tobkiri.packaging-source-provenance.v1",
            "source_commit": source_revision,
            "source_tree": fixture_source_tree(source_checkout),
            "source_clean": true,
            "source_manifest_sha256": format!("{:x}", Sha256::digest(&manifest_bytes)),
        });
        fs::write(&provenance_path, serde_json::to_vec(&provenance).unwrap()).unwrap();
        set_fixture_permissions(&provenance_path, 0o400);
        seal_fixture_directories(&source_root);
        set_fixture_permissions(&owner, 0o700);

        let canonical_root = source_root.canonicalize().unwrap();
        let canonical_provenance = provenance_path.canonicalize().unwrap();
        assert_eq!(
            canonical_provenance,
            canonical_root.join("packaging-source-provenance.v1.json"),
            "fixture provenance must bind the sealed snapshot root"
        );
        let provenance_metadata = fs::symlink_metadata(&canonical_provenance).unwrap();
        assert!(
            !provenance_metadata.file_type().is_symlink()
                && !has_multiple_links(&canonical_provenance, &provenance_metadata).unwrap(),
            "fixture provenance must be a regular non-hardlinked file"
        );
        #[cfg(unix)]
        assert_eq!(
            provenance_metadata.permissions().mode() & 0o222,
            0,
            "fixture provenance must be read-only"
        );

        (canonical_root, canonical_provenance, owner_guard)
    }

    fn collect_source_files(root: &Path, current: &Path, actual: &mut BTreeMap<String, Value>) {
        let entries = fs::read_dir(current).expect("source closure directory should be readable");
        for entry in entries {
            let entry = entry.expect("source closure entry should be readable");
            let path = entry.path();
            let metadata =
                fs::symlink_metadata(&path).expect("source closure metadata should exist");
            assert!(
                !metadata.file_type().is_symlink(),
                "source closure contains a symlink: {}",
                path.display()
            );
            if metadata.is_dir() {
                collect_source_files(root, &path, actual);
            } else {
                assert!(
                    metadata.is_file(),
                    "source closure contains a special entry: {}",
                    path.display()
                );
                assert!(
                    !has_multiple_links(&path, &metadata)
                        .expect("source links should be inspectable"),
                    "source closure contains a hardlink: {}",
                    path.display()
                );
                let relative = path
                    .strip_prefix(root)
                    .expect("source file should remain under closure root")
                    .to_string_lossy()
                    .replace('\\', "/");
                let record = serde_json::json!({
                    "path": relative,
                    "type": "regular-file",
                    "size": metadata.len(),
                    "sha256": source_file_digest(&path),
                    "executable": source_file_executable(&metadata),
                });
                assert!(
                    actual.insert(relative, record).is_none(),
                    "duplicate source path"
                );
            }
        }
    }

    fn assert_source_manifest_exact(source_checkout: &Path) {
        let expected = source_manifest_entries(source_checkout);
        let runtime_root = source_checkout.join("tobkiri_runtime");
        let manifest = runtime_root.join("packaged_defaultspack_source_manifest.v1.json");
        let mut actual = BTreeMap::new();
        let roots = [
            "scripts",
            "tobkiri_protocol",
            "ecosystem/defaultspack/domain/runtime_v4",
            "ecosystem/defaultspack/v4",
            "ecosystem/defaultspack/runtime",
            "ecosystem/defaultspack/defaultspack",
        ];
        for root in roots {
            collect_source_files(&runtime_root, &runtime_root.join(root), &mut actual);
        }
        for relative in [
            "ecosystem/defaultspack/pack.v4.json",
            "ecosystem/defaultspack/contracts.v4.json",
            "ecosystem/defaultspack/artifact-index.v4.json",
            "ecosystem/defaultspack/executables.v4.json",
            "ecosystem/defaultspack/host_contract_contributions.v1.json",
            "ecosystem/defaultspack/domain/runtime_surface_v4.py",
            "ecosystem/defaultspack/update_metadata.v1.json",
        ] {
            let path = runtime_root.join(relative);
            let metadata = fs::symlink_metadata(&path).expect("source file should exist");
            assert!(!metadata.file_type().is_symlink() && metadata.is_file());
            assert!(!has_multiple_links(&path, &metadata).unwrap());
            actual.insert(
                relative.to_owned(),
                serde_json::json!({
                    "path": relative,
                    "type": "regular-file",
                    "size": metadata.len(),
                    "sha256": source_file_digest(&path),
                    "executable": source_file_executable(&metadata),
                }),
            );
        }
        assert!(!actual.contains_key("packaged_defaultspack_source_manifest.v1.json"));
        assert_eq!(
            actual, expected,
            "source closure differs from shared manifest"
        );
        assert!(
            !manifest.is_symlink(),
            "source manifest itself may not be a symlink"
        );
    }

    fn clone_authoritative_fixture_source(repository: &Path, destination: &Path) -> String {
        let status = Command::new(verified_git())
            .args(["clone", "--quiet", "--shared", "--no-checkout", "--no-tags"])
            .arg(repository)
            .arg(destination)
            .status()
            .expect("authoritative fixture source clone should run");
        assert!(
            status.success(),
            "authoritative fixture source clone failed"
        );
        let manifest = source_manifest_entries(repository);
        let mut sparse_paths = vec!["sparse-checkout".to_owned(), "set".to_owned()];
        for relative in manifest.keys() {
            sparse_paths.push(format!("tobkiri_runtime/{relative}"));
        }
        sparse_paths.push(SOURCE_MANIFEST_RELATIVE.to_owned());
        sparse_paths.push("tobkiri_launcher/src-tauri/bundled".to_owned());
        let status = Command::new(verified_git())
            .args(&sparse_paths)
            .current_dir(destination)
            .status()
            .expect("authoritative fixture sparse checkout should run");
        assert!(
            status.success(),
            "authoritative fixture sparse checkout failed"
        );
        let status = Command::new(verified_git())
            .args(["checkout", "--quiet", "HEAD"])
            .current_dir(destination)
            .status()
            .expect("authoritative fixture checkout should run");
        assert!(status.success(), "authoritative fixture checkout failed");
        let revision = Command::new(verified_git())
            .args(["rev-parse", "--verify", "HEAD^{commit}"])
            .current_dir(destination)
            .output()
            .expect("authoritative fixture revision should be readable");
        assert!(
            revision.status.success(),
            "authoritative fixture revision lookup failed"
        );
        let revision = String::from_utf8(revision.stdout)
            .expect("authoritative fixture revision should be UTF-8")
            .trim()
            .to_owned();
        assert!(
            revision.len() == 40
                && revision
                    .bytes()
                    .all(|character| character.is_ascii_hexdigit()
                        && !character.is_ascii_uppercase()),
            "authoritative fixture revision must be a full lowercase SHA"
        );
        assert_clean_fixture_source(destination);
        revision
    }

    fn assert_clean_fixture_source(source_checkout: &Path) {
        let status = Command::new(verified_git())
            .args(["status", "--porcelain=v1", "--untracked-files=all"])
            .current_dir(source_checkout)
            .output()
            .expect("authoritative fixture status should be readable");
        assert!(
            status.status.success(),
            "authoritative fixture status failed"
        );
        assert!(
            status.stdout.is_empty(),
            "authoritative fixture source must remain clean"
        );
        assert!(
            !source_checkout
                .join(".github/scripts/packaging_cleanup.py")
                .exists(),
            "relocated generator must not retain the repository helper fallback"
        );
        assert_source_manifest_exact(source_checkout);
    }

    fn selected_fixture_pack_digests(
        bundle_root: &Path,
        selected: &serde_json::Map<String, Value>,
    ) -> Result<serde_json::Map<String, Value>> {
        if selected.is_empty() {
            bail!("fixture catalog selected Pack set is empty");
        }
        let canonical_bundle_root = bundle_root
            .canonicalize()
            .context("generated fixture bundle root is unavailable")?;
        let bundle_lock = verify_bundle_lock(&canonical_bundle_root)
            .context("generated fixture bundle lock is invalid")?;
        let mut digests = serde_json::Map::new();
        for pack_id in selected.keys() {
            let relative = bundle_lock.pack_paths.get(pack_id).with_context(|| {
                format!("generated fixture bundle is missing selected Pack: {pack_id}")
            })?;
            let digest = bundle_lock
                .authority_digests
                .get(relative)
                .with_context(|| format!("generated fixture Pack has no digest: {pack_id}"))?;
            if !valid_digest(digest) {
                bail!("generated fixture Pack digest is invalid: {pack_id}");
            }
            if digests
                .insert(pack_id.clone(), Value::String(digest.clone()))
                .is_some()
            {
                bail!("generated fixture bundle contains a duplicate selected Pack");
            }
        }
        if digests.len() != selected.len()
            || selected
                .keys()
                .any(|pack_id| !digests.contains_key(pack_id))
        {
            bail!("generated fixture bundle is missing a selected catalog Pack");
        }
        Ok(digests)
    }

    fn package_fixture_application(
        config: &AppConfig,
        source_checkout: &Path,
        source_revision: &str,
    ) {
        let source = config.app_dir.join("fixture-release/Tobkiri.app");
        let executable = source.join("Contents/MacOS/tobkiri-shell");
        fs::create_dir_all(executable.parent().unwrap()).unwrap();
        fs::write(
            &executable,
            b"\xcf\xfa\xed\xfe\x0c\x00\x00\x01packaged Tauri fixture",
        )
        .unwrap();
        fs::write(
            source.join("Contents/Info.plist"),
            br#"<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict><key>CFBundleIdentifier</key>
<string>io.tobkiri.shell.tauri</string></dict></plist>"#,
        )
        .unwrap();
        fs::create_dir_all(source.join("Contents/Resources")).unwrap();
        fs::write(
            source.join("Contents/Resources/presentation.json"),
            b"sealed presentation fixture",
        )
        .unwrap();

        let python = verified_python();
        let hostile = config.app_dir.join("hostile-generator-input");
        fs::create_dir_all(hostile.join("scripts")).unwrap();
        let marker = hostile.join("executed.marker");
        let marker_literal = format!("{:?}", marker.to_string_lossy());
        fs::write(
            hostile.join("sitecustomize.py"),
            format!(
                "from pathlib import Path; Path({marker_literal}).write_text('sitecustomize')\n"
            ),
        )
        .unwrap();
        fs::write(
            hostile.join("usercustomize.py"),
            format!(
                "from pathlib import Path; Path({marker_literal}).write_text('usercustomize')\n"
            ),
        )
        .unwrap();
        fs::write(hostile.join("scripts/__init__.py"), "\n").unwrap();
        fs::write(
            hostile.join("scripts/generate_packaged_defaultspack_v4_bundle.py"),
            format!("from pathlib import Path; Path({marker_literal}).write_text('fake-module')\n"),
        )
        .unwrap();
        let mut unsafe_command = python.command().unwrap();
        let unsafe_status = unsafe_command
            .args([
                "-B",
                "-m",
                "scripts.generate_packaged_defaultspack_v4_bundle",
                "--help",
            ])
            .env("PYTHONPATH", &hostile)
            .status()
            .unwrap();
        assert!(unsafe_status.success());
        assert!(
            marker.exists(),
            "unsafe fixture launch should execute its marker"
        );
        fs::remove_file(&marker).unwrap();

        let (source_root, provenance_path, mut source_owner) =
            materialize_fixture_source_provenance(
                source_checkout,
                &config.app_dir.join(".fixture-source-snapshot"),
                source_revision,
            );
        let mut isolated = python.command().unwrap();
        isolated
            .env_clear()
            .args(["-I", "-B", "-c", ISOLATED_MODULE_CODE])
            .arg(&source_root)
            .arg("scripts.generate_packaged_defaultspack_v4_bundle")
            .arg("--source-artifact")
            .arg(&source)
            .arg("--bundle-root")
            .arg(config.app_dir.join("ecosystem/defaultspack/v4"))
            .arg("--artifact-root")
            .arg(
                config
                    .app_dir
                    .join("ecosystem/defaultspack/platform-artifacts"),
            )
            .arg("--relative-path")
            .arg("Tobkiri.app")
            .arg("--entrypoint")
            .arg("Tobkiri.app/Contents/MacOS/tobkiri-shell")
            .arg("--platform")
            .arg("macos")
            .arg("--architecture")
            .arg("arm64")
            .arg("--bundle-identity")
            .arg("io.tobkiri.shell.tauri")
            .arg("--source-provenance-file")
            .arg(&provenance_path)
            .env(
                "GIT_CONFIG_GLOBAL",
                if cfg!(windows) { "NUL" } else { "/dev/null" },
            )
            .env("GIT_CONFIG_NOSYSTEM", "1");
        for key in ISOLATED_ENVIRONMENT_KEYS {
            if let Some(value) = std::env::var_os(key) {
                isolated.env(key, value);
            }
        }
        let status = isolated.status().unwrap();
        assert!(
            status.success(),
            "official packaged Profile generator failed"
        );
        assert!(
            !marker.exists(),
            "isolated fixture launch executed hostile input"
        );
        drop(unsafe_command);
        drop(isolated);
        drop(python);
        if let Err(error) = source_owner.cleanup() {
            panic!(
                "fixture source owner teardown failed at {}: {error}",
                source_owner.path.display()
            );
        }
        assert_clean_fixture_source(source_checkout);
        let bundle_root = config.app_dir.join("ecosystem/defaultspack/v4");
        let profile_raw = fs::read(bundle_root.join(PROFILE_PATH)).unwrap();
        let mut catalog: Value = serde_json::from_slice(
            &fs::read(config.app_dir.join("bundled/presentation_catalog.json")).unwrap(),
        )
        .unwrap();
        catalog["default_profile_digest"] = Value::String(sha256(&profile_raw));
        let selected = catalog["source_manifest_digests"]
            .as_object()
            .expect("fixture catalog selected Pack set should be an object")
            .clone();
        let generated_digests = selected_fixture_pack_digests(&bundle_root, &selected)
            .expect("generated fixture must contain the complete selected Pack set");
        assert_eq!(
            generated_digests.keys().collect::<BTreeSet<_>>(),
            selected.keys().collect::<BTreeSet<_>>(),
            "generated fixture digest projection must preserve the exact catalog selection"
        );
        catalog["source_manifest_digests"] = Value::Object(generated_digests);
        fs::write(
            config.app_dir.join("bundled/presentation_catalog.json"),
            serde_json::to_vec(&catalog).unwrap(),
        )
        .unwrap();
        let profile: Value =
            serde_json::from_slice(&fs::read(bundle_root.join(PROFILE_PATH)).unwrap()).unwrap();
        assert_eq!(
            value_str(&profile, "/provenance/schema"),
            Some("io.tobkiri.provenance.v1"),
            "compatibility Profile must retain its v1 provenance schema"
        );
        assert_eq!(
            value_str(&profile, "/provenance/repository_commit"),
            Some("working-tree"),
            "compatibility Profile must remain non-release provenance"
        );
        assert_eq!(
            profile
                .pointer("/provenance/normative")
                .and_then(Value::as_bool),
            Some(false),
            "compatibility Profile provenance must remain non-authoritative"
        );
        let mut compatibility_payload = profile.clone();
        assert!(
            compatibility_payload
                .as_object_mut()
                .unwrap()
                .remove("provenance")
                .is_some(),
            "compatibility Profile must contain provenance"
        );
        let expected_source_digest = canonical_value_digest(&compatibility_payload).unwrap();
        assert_eq!(
            value_str(&profile, "/provenance/source_digest"),
            Some(expected_source_digest.as_str()),
            "compatibility Profile provenance source digest must bind its payload"
        );

        for relative in [
            "shell.tauri.default.shell.v1.json",
            SHELL_PACK_PATH,
            RUNTIME_PACK_PATH,
        ] {
            let document: Value =
                serde_json::from_slice(&fs::read(bundle_root.join(relative)).unwrap()).unwrap();
            assert_eq!(
                value_str(&document, "/provenance/repository_commit"),
                Some(source_revision),
                "packaged fixture must retain its isolated release provenance"
            );
            assert_eq!(
                document
                    .pointer("/provenance/normative")
                    .and_then(Value::as_bool),
                Some(true),
                "normative generated artifact must retain authoritative provenance: {relative}"
            );
        }
    }

    fn copy_tree(source: &Path, destination: &Path) {
        fs::create_dir_all(destination).unwrap();
        for entry in fs::read_dir(source).unwrap() {
            let entry = entry.unwrap();
            let source_path = entry.path();
            let destination_path = destination.join(entry.file_name());
            if entry.file_type().unwrap().is_dir() {
                copy_tree(&source_path, &destination_path);
            } else {
                fs::copy(source_path, destination_path).unwrap();
            }
        }
    }

    fn fixture_at_layout(name: &str, layout: &[&str]) -> (PathBuf, AppConfig) {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!(
            "tobkiri-defaultspack-authority-{name}-{}-{unique}",
            std::process::id()
        ));
        let repository = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
        let source_checkout = root.join("authoritative-source");
        let source_revision = clone_authoritative_fixture_source(&repository, &source_checkout);
        let source_checkout = source_checkout
            .canonicalize()
            .expect("authoritative fixture source should be canonical");
        let app_dir = layout
            .iter()
            .fold(root.clone(), |path, component| path.join(component));
        let source_pack = source_checkout.join("tobkiri_runtime/ecosystem/defaultspack");
        let destination_pack = app_dir.join("ecosystem/defaultspack");
        copy_tree(&source_pack.join("v4"), &destination_pack.join("v4"));
        for relative in [
            "pack.v4.json",
            "contracts.v4.json",
            "artifact-index.v4.json",
            "executables.v4.json",
            "host_contract_contributions.v1.json",
            "update_metadata.v1.json",
            "runtime/conversation.py",
            "defaultspack/desktop_app.py",
            "defaultspack/frontend_contract_map.v4.json",
        ] {
            let source = source_pack.join(relative);
            let destination = destination_pack.join(relative);
            fs::create_dir_all(destination.parent().unwrap()).unwrap();
            fs::copy(source, destination).unwrap();
        }
        fs::create_dir_all(app_dir.join("bundled")).unwrap();
        fs::copy(
            source_checkout.join("tobkiri_launcher/src-tauri/bundled/presentation_catalog.json"),
            app_dir.join("bundled/presentation_catalog.json"),
        )
        .unwrap();
        let config = AppConfig {
            app_dir: app_dir.clone(),
            rumi_home: app_dir,
            python_dir: root.join("Application Support/python"),
            uv_path: root.join("Application Support/uv"),
            venv_dir: root.join("Application Support/venv"),
            user_data_dir: root.join("Application Support/user_data"),
            log_dir: root.join("Application Support/logs"),
            kernel_port: 8765,
            dev_workspace_root: None,
        };
        package_fixture_application(&config, &source_checkout, &source_revision);
        (root, config)
    }

    fn fixture(name: &str) -> (PathBuf, AppConfig) {
        fixture_at_layout(name, RELOCATION_LAYOUTS[0].1)
    }

    #[test]
    fn legacy_profile_marker_never_becomes_execution_authority() {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!(
            "tobkiri-legacy-profile-pointer-{}-{unique}",
            std::process::id()
        ));
        let user_data_dir = root.join("user_data");
        let profiles = user_data_dir.join("profiles");
        fs::create_dir_all(&profiles).unwrap();
        let config = AppConfig {
            app_dir: root.join("app"),
            rumi_home: root.join("app"),
            python_dir: root.join("python"),
            uv_path: root.join("uv"),
            venv_dir: root.join("venv"),
            user_data_dir,
            log_dir: root.join("logs"),
            kernel_port: 8765,
            dev_workspace_root: None,
        };
        let legacy = serde_json::json!({
            "version": 1,
            "active_profile_id": "default-profile"
        });
        fs::write(
            profiles.join("active_profile.json"),
            serde_json::to_vec(&legacy).unwrap(),
        )
        .unwrap();

        assert!(read_active_profile_snapshot(&config).unwrap().is_none());

        fs::write(
            profiles.join("active.json"),
            serde_json::to_vec(&legacy).unwrap(),
        )
        .unwrap();
        let error = read_active_profile_snapshot(&config)
            .unwrap_err()
            .to_string();
        assert!(
            error.contains("unknown or missing fields"),
            "legacy bytes at the execution pointer path must fail closed: {error}"
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn executable_catalog_is_verified_as_non_authority_sidecar() {
        let root = minimal_sidecar_bundle("valid");
        let verified = verify_bundle_lock(&root).unwrap();
        assert_eq!(
            verified
                .authority_digests
                .keys()
                .cloned()
                .collect::<Vec<_>>(),
            ["packs/test_pack.pack.v4.json"]
        );
        assert_eq!(
            verified.sidecar_digests.keys().cloned().collect::<Vec<_>>(),
            ["packs/test_pack.executables.v4.json"]
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn canonical_bundle_executable_catalogs_pass_rust_verifier() {
        let source_bundle = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../tobkiri_runtime/ecosystem/defaultspack/v4");
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!(
            "tobkiri-canonical-packaged-bundle-{}-{unique}",
            std::process::id()
        ));
        let bundle = root.join("v4");
        copy_tree(&source_bundle, &bundle);
        for relative in [
            "defaults.profile.intent.v1.json",
            "defaults.profile.lock.v5.json",
            "defaults.release.provenance.json",
        ] {
            fs::remove_file(bundle.join(relative)).unwrap();
        }
        let canonical_bundle = bundle.canonicalize().unwrap();
        let verified = verify_bundle_lock(&canonical_bundle).unwrap();
        assert_eq!(verified.sidecar_digests.len(), 65);
        assert_eq!(verified.authority_digests.len(), 74);
        assert!(verified
            .sidecar_digests
            .contains_key("packs/defaultspack.executables.v4.json"));
        assert!(!verified
            .authority_digests
            .contains_key("packs/defaultspack.executables.v4.json"));
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn executable_catalog_tamper_fails_locked_raw_digest() {
        let root = minimal_sidecar_bundle("tamper");
        fs::write(
            root.join("packs/test_pack.executables.v4.json"),
            b"{\"tampered\":true}",
        )
        .unwrap();
        let error = verify_bundle_lock(&root).unwrap_err().to_string();
        assert!(error.contains("locked entry digest mismatch"), "{error}");
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn executable_catalog_missing_file_fails_closed() {
        let root = minimal_sidecar_bundle("missing");
        fs::remove_file(root.join("packs/test_pack.executables.v4.json")).unwrap();
        assert!(verify_bundle_lock(&root).is_err());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn executable_catalog_role_mismatch_cannot_enter_authority_graph() {
        let root = minimal_sidecar_bundle("role-mismatch");
        rewrite_minimal_lock(&root, |lock| {
            lock["entries"][1]["kind"] = Value::String("profile".to_owned());
        });
        let error = verify_bundle_lock(&root).unwrap_err().to_string();
        assert!(error.contains("declared authority role"), "{error}");
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn executable_catalog_self_digest_mismatch_fails_closed() {
        let root = minimal_sidecar_bundle("self-digest");
        rewrite_minimal_sidecar(&root, |catalog| {
            catalog["catalog_digest"] = Value::String(format!("sha256:{}", "0".repeat(64)));
        });
        let error = verify_bundle_lock(&root).unwrap_err().to_string();
        assert!(error.contains("self-digest mismatch"), "{error}");
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn executable_catalog_authority_masquerade_fails_closed() {
        let root = minimal_sidecar_bundle("authority-masquerade");
        rewrite_minimal_lock(&root, |lock| {
            lock["entries"][1]["kind"] = Value::String("pack".to_owned());
        });
        let error = verify_bundle_lock(&root).unwrap_err().to_string();
        assert!(error.contains("declared authority role"), "{error}");
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn executable_catalog_source_identity_mismatch_fails_closed() {
        let root = minimal_sidecar_bundle("source-identity");
        rewrite_minimal_sidecar(&root, |catalog| {
            catalog["source_identity"] = Value::String(format!("sha256:{}", "9".repeat(64)));
            let object = catalog.as_object_mut().unwrap();
            object.remove("catalog_digest");
            let digest = sha256(&serde_json::to_vec(&catalog).unwrap());
            catalog["catalog_digest"] = Value::String(digest);
        });
        let error = verify_bundle_lock(&root).unwrap_err().to_string();
        assert!(error.contains("source identity disagrees"), "{error}");
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn projected_executable_catalog_requires_materialization_digest() {
        let root = minimal_projected_sidecar_bundle("projection-pin-required");
        rewrite_minimal_sidecar(&root, |catalog| {
            catalog
                .as_object_mut()
                .unwrap()
                .remove("materialization_catalog_digest");
            let object = catalog.as_object_mut().unwrap();
            object.remove("catalog_digest");
            let digest = sha256(&serde_json::to_vec(&catalog).unwrap());
            catalog["catalog_digest"] = Value::String(digest);
        });
        let error = verify_bundle_lock(&root).unwrap_err().to_string();
        assert!(
            error.contains("missing its canonical materialization digest"),
            "{error}"
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn non_projected_executable_catalog_rejects_materialization_digest_alias() {
        let root = minimal_sidecar_bundle("materialization-alias");
        rewrite_minimal_sidecar(&root, |catalog| {
            catalog["materialization_catalog_digest"] =
                Value::String(format!("sha256:{}", "4".repeat(64)));
            let object = catalog.as_object_mut().unwrap();
            object.remove("catalog_digest");
            let digest = sha256(&serde_json::to_vec(&catalog).unwrap());
            catalog["catalog_digest"] = Value::String(digest);
        });
        let error = verify_bundle_lock(&root).unwrap_err().to_string();
        assert!(
            error.contains("cannot replace its catalog identity"),
            "{error}"
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn projected_executable_catalog_accepts_valid_materialization_digest() {
        let root = minimal_projected_sidecar_bundle("projection-pin-valid");
        assert!(verify_bundle_lock(&root).is_ok());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn materialized_root_pack_must_bind_its_locked_projection_to_exact_bytes() {
        let root_pack = serde_json::to_vec(&serde_json::json!({
            "pack": {"id": "test_pack"},
        }))
        .unwrap();
        let root_digest = sha256(&root_pack);
        let projection = serde_json::to_vec(&serde_json::json!({
            "pack": {"id": "test_pack"},
            "integrity": {"source_identity": root_digest},
            "provenance": {
                "schema": "io.tobkiri.provenance.v2",
                "source_kind": "generated",
                "source_digest": root_digest,
                "normative": true,
            },
        }))
        .unwrap();
        assert!(verify_materialized_root_pack_binding(&root_pack, &projection).is_ok());

        let forged_projection = serde_json::to_vec(&serde_json::json!({
            "pack": {"id": "test_pack"},
            "integrity": {"source_identity": format!("sha256:{}", "0".repeat(64))},
            "provenance": {
                "schema": "io.tobkiri.provenance.v2",
                "source_kind": "generated",
                "source_digest": format!("sha256:{}", "0".repeat(64)),
                "normative": true,
            },
        }))
        .unwrap();
        let error = verify_materialized_root_pack_binding(&root_pack, &forged_projection)
            .unwrap_err()
            .to_string();
        assert!(
            error.contains("materialized root Pack differs from the locked Profile Pack"),
            "{error}"
        );
    }

    #[test]
    fn executable_catalog_unknown_schema_field_fails_closed() {
        let root = minimal_sidecar_bundle("unknown-field");
        rewrite_minimal_sidecar(&root, |catalog| {
            catalog["unknown_authority_hint"] = Value::Bool(true);
            let object = catalog.as_object_mut().unwrap();
            object.remove("catalog_digest");
            let digest = sha256(&serde_json::to_vec(&catalog).unwrap());
            catalog["catalog_digest"] = Value::String(digest);
        });
        let error = verify_bundle_lock(&root).unwrap_err().to_string();
        assert!(error.contains("strict schema"), "{error}");
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn profile_pack_set_is_declared_and_fenced() {
        let repository = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
        let profile_path = repository.join(DEFAULT_PROFILE_SOURCE);
        let profile: Value = serde_json::from_slice(&fs::read(profile_path).unwrap()).unwrap();

        assert_eq!(value_str(&profile, "/base/pack_id"), Some(DEFAULT_BASE_ID));
        assert_eq!(
            value_str(&profile, "/profile_api_version"),
            Some(DEFAULT_PROFILE_API_VERSION)
        );
        assert_eq!(
            value_str(&profile, "/shell/pack_id"),
            Some(DEFAULT_SHELL_ID)
        );
        validate_effective_pack_set(&profile).unwrap();

        let application_id = profile["packs"]
            .as_array()
            .unwrap()
            .iter()
            .find(|pack| value_str(pack, "/role") == Some("application"))
            .and_then(|pack| value_str(pack, "/pack_id"))
            .unwrap()
            .to_owned();
        let mut missing_application = profile.clone();
        missing_application["packs"]
            .as_array_mut()
            .unwrap()
            .retain(|pack| value_str(pack, "/pack_id") != Some(application_id.as_str()));
        assert!(validate_effective_pack_set(&missing_application).is_err());

        let mut duplicate = profile.clone();
        duplicate["packs"]
            .as_array_mut()
            .unwrap()
            .push(serde_json::json!({"pack_id": application_id, "role": "application"}));
        assert!(validate_effective_pack_set(&duplicate).is_err());

        let mut development = profile;
        development["packs"]
            .as_array_mut()
            .unwrap()
            .push(serde_json::json!({"pack_id": "dev.test", "role": "provider"}));
        assert!(validate_effective_pack_set(&development).is_err());
    }

    fn generic_profile(profile_id: &str, application_id: &str) -> Value {
        serde_json::json!({
            "profile_api_version": DEFAULT_PROFILE_API_VERSION,
            "profile_id": profile_id,
            "mode": "interactive",
            "state": "resolved",
            "base": {"pack_id": format!("{profile_id}.base")},
            "shell": {
                "provider_id": format!("{profile_id}.shell"),
                "pack_id": format!("{profile_id}.shell"),
                "contract_id": "app.shell.v1",
                "platform": "linux",
                "architecture": "x86_64"
            },
            "packs": [{"pack_id": application_id, "role": "application"}]
        })
    }

    fn runtime_launch_plan() -> Value {
        serde_json::json!({
            "launch_contribution": {
                "provider_id": "application.fixture",
                "contract_id": "application.fixture.v1",
                "operation_id": "launch",
                "platform": "linux",
                "architecture": "x86_64",
                "artifact_digest": format!("sha256:{}", "a".repeat(64)),
                "relative_path": "Fixture.AppImage",
                "entrypoint": "Fixture.AppImage"
            }
        })
    }

    #[test]
    fn runtime_launch_selector_accepts_only_the_exact_plan_shape() {
        let selector = runtime_launch_contribution_from_plan(&runtime_launch_plan()).unwrap();
        assert_eq!(selector.provider_id, "application.fixture");
        assert_eq!(selector.contract_id, "application.fixture.v1");
        assert_eq!(selector.relative_path, "Fixture.AppImage");

        let mut extra = runtime_launch_plan();
        extra["launch_contribution"]["route"] = Value::String("/forbidden-in-selector".into());
        assert!(runtime_launch_contribution_from_plan(&extra).is_err());

        let mut escaped = runtime_launch_plan();
        escaped["launch_contribution"]["entrypoint"] = Value::String("../outside".into());
        assert!(runtime_launch_contribution_from_plan(&escaped).is_err());

        let absent = runtime_launch_contribution_from_plan(&serde_json::json!({})).unwrap_err();
        assert!(absent
            .downcast_ref::<ProfileReresolutionRequired>()
            .is_some());
        assert!(absent.to_string().contains("reactivation or re-resolution"));

        let null = runtime_launch_contribution_from_plan(&serde_json::json!({
            "launch_contribution": null
        }))
        .unwrap_err();
        assert!(null.downcast_ref::<ProfileReresolutionRequired>().is_some());
        assert_eq!(
            ProfileReresolutionRequired::CODE,
            "PROFILE_RERESOLUTION_REQUIRED"
        );
        assert_eq!(
            ProfileReresolutionRequired::ACTION,
            "reactivate_or_reresolve_profile"
        );

        let malformed = runtime_launch_contribution_from_plan(&serde_json::json!({
            "launch_contribution": "not-an-object"
        }))
        .unwrap_err();
        assert!(malformed
            .downcast_ref::<ProfileReresolutionRequired>()
            .is_none());
        assert!(malformed.to_string().contains("malformed"));
    }

    #[test]
    fn rich_contract_map_identity_is_bound_to_the_selected_application() {
        let digest = format!("sha256:{}", "a".repeat(64));
        let mut map = serde_json::json!({
            "schema": "io.tobkiri.frontend-contract-map.v4",
            "pack_id": "application.fixture",
            "owner": "application.fixture",
            "application_id": "application.fixture",
            "artifact_path": "application.fixture/frontend_contract_map.v4.json",
            "artifact_digest": digest,
            "routes": []
        });
        assert_eq!(
            application_contract_namespace(
                &map,
                "application.fixture",
                "application.fixture/frontend_contract_map.v4.json",
                map["artifact_digest"].as_str().unwrap(),
            )
            .unwrap(),
            "application.fixture"
        );

        map["owner"] = Value::String("application.foreign".into());
        let error = application_contract_namespace(
            &map,
            "application.fixture",
            "application.fixture/frontend_contract_map.v4.json",
            map["artifact_digest"].as_str().unwrap(),
        )
        .unwrap_err();
        assert!(error.to_string().contains("another Application"));
    }

    #[test]
    fn legacy_contract_map_namespace_must_match_its_signed_artifact_path() {
        let digest = format!("sha256:{}", "a".repeat(64));
        let mut map = serde_json::json!({
            "schema": "io.tobkiri.frontend-contract-map.v4",
            "pack_id": "legacy.surface",
            "routes": []
        });
        assert_eq!(
            application_contract_namespace(
                &map,
                "application.fixture",
                "legacy.surface/frontend_contract_map.v4.json",
                &digest,
            )
            .unwrap(),
            "legacy.surface"
        );

        map["pack_id"] = Value::String("forged.surface".into());
        let error = application_contract_namespace(
            &map,
            "application.fixture",
            "legacy.surface/frontend_contract_map.v4.json",
            &digest,
        )
        .unwrap_err();
        assert!(error.to_string().contains("another Application"));
    }

    #[test]
    fn signed_resolver_accepts_multiple_profiles_and_rejects_unknown_identity() {
        let profile_a = generic_profile("profile.alpha", "application.alpha");
        let profile_b = generic_profile("profile.beta", "application.beta");
        let selected_a = selected_profile_from_documents(
            profile_a.clone(),
            None,
            None,
            "profile.alpha".into(),
            canonical_value_digest(&profile_a).unwrap(),
            None,
            None,
            None,
            None,
        )
        .unwrap();
        let selected_b = selected_profile_from_documents(
            profile_b.clone(),
            None,
            None,
            "profile.beta".into(),
            canonical_value_digest(&profile_b).unwrap(),
            None,
            None,
            None,
            None,
        )
        .unwrap();
        assert_eq!(selected_a.profile_id, "profile.alpha");
        assert_eq!(selected_a.application_pack_id, "application.alpha");
        assert_eq!(selected_b.profile_id, "profile.beta");
        assert_eq!(selected_b.application_pack_id, "application.beta");
        assert!(selected_profile_from_documents(
            profile_a.clone(),
            None,
            None,
            "profile.unknown".into(),
            canonical_value_digest(&profile_a).unwrap(),
            None,
            None,
            None,
            None,
        )
        .is_err());
    }

    #[test]
    fn read_only_migration_requires_selected_host_extension_provider() {
        let mut profile = generic_profile("profile.migration", "application.migration");
        profile["packs"]
            .as_array_mut()
            .unwrap()
            .push(serde_json::json!({
                "pack_id": "provider.migration",
                "role": "provider"
            }));
        let selected = selected_profile_from_documents(
            profile.clone(),
            None,
            None,
            "profile.migration".into(),
            canonical_value_digest(&profile).unwrap(),
            None,
            None,
            None,
            None,
        )
        .unwrap();
        let host_extension = serde_json::json!({
            "pack": {"id": "provider.migration", "kind": "host_extension"},
            "migration": {"compatibility": "read_only"}
        });
        assert!(profile_pack_migration_is_admissible(
            &selected,
            "provider.migration",
            &host_extension
        ));

        let mut application_binding = host_extension.clone();
        application_binding["pack"]["id"] = Value::String("application.migration".into());
        assert!(!profile_pack_migration_is_admissible(
            &selected,
            "application.migration",
            &application_binding
        ));

        let mut ordinary_application = host_extension.clone();
        ordinary_application["pack"]["kind"] = Value::String("application".into());
        ordinary_application["functions"] = serde_json::json!([{
            "id": "provider.migration.forged-host-capability",
            "role": "host_capability_provider"
        }]);
        assert!(!profile_pack_migration_is_admissible(
            &selected,
            "provider.migration",
            &ordinary_application
        ));

        let mut ordinary_sandbox = host_extension.clone();
        ordinary_sandbox["pack"]["kind"] = Value::String("normal_sandbox".into());
        assert!(!profile_pack_migration_is_admissible(
            &selected,
            "provider.migration",
            &ordinary_sandbox
        ));

        let mut unknown_compatibility = host_extension;
        unknown_compatibility["migration"]["compatibility"] = Value::String("legacy".into());
        assert!(!profile_pack_migration_is_admissible(
            &selected,
            "provider.migration",
            &unknown_compatibility
        ));
    }

    #[test]
    fn signed_resolver_rejects_resolved_plan_digest_mismatch() {
        let mut profile = generic_profile("profile.digest", "application.digest");
        profile["packs"][0]["artifact_digest"] =
            Value::String(format!("sha256:{}", "1".repeat(64)));
        let profile_revision = canonical_value_digest(&profile).unwrap();
        let mut plan = serde_json::json!({
            "plan_api_version": "io.tobkiri.resolved-plan.v2",
            "profile_id": "profile.digest",
            "profile_revision": profile_revision,
            "base": {"pack_id": "profile.digest.base"},
            "shell": {
                "provider_id": "profile.digest.shell",
                "pack_id": "profile.digest.shell"
            },
            "application": {
                "pack_id": "application.digest",
                "artifact_digest": format!("sha256:{}", "1".repeat(64))
            }
        });
        let plan_digest = canonical_value_digest(&plan).unwrap();
        plan["plan_digest"] = Value::String(plan_digest.clone());
        plan["unexpected_extension"] = Value::String("stale".into());
        assert!(selected_profile_from_documents(
            profile,
            None,
            Some(plan),
            "profile.digest".into(),
            profile_revision.clone(),
            Some(profile_revision),
            Some("activation:digest-test".into()),
            Some(plan_digest),
            None,
        )
        .is_err());
    }

    #[test]
    fn materialized_root_pack_must_be_in_the_selected_profile_closure() {
        let selected =
            BTreeSet::from(["application.alpha".to_owned(), "provider.alpha".to_owned()]);
        assert!(ensure_materialized_pack_selected("application.alpha", &selected).is_ok());
        assert!(ensure_materialized_pack_selected("provider.alpha", &selected).is_ok());
        assert!(ensure_materialized_pack_selected("provider.foreign", &selected).is_err());
    }

    #[test]
    fn relocated_packaged_first_start_and_restart_use_identical_v4_authority() {
        for (layout_name, layout) in RELOCATION_LAYOUTS {
            let (root, config) =
                fixture_at_layout(&format!("relocated-restart-{layout_name}"), layout);
            let retired = config.app_dir.join("ecosystem/defaultspack/ecosystem.json");
            assert!(
                !retired.exists(),
                "retired ecosystem.json must remain absent for {layout_name}"
            );

            let first = resolve(&config).unwrap();
            let restarted = resolve(&config).unwrap();

            assert_eq!(first, restarted);
            assert_eq!(first.profile_id, DEFAULT_PROFILE_ID);
            assert!(first.launch.argv.is_empty());
            assert_eq!(first.launch.function_id, DEFAULT_RUNTIME_ID);
            assert_eq!(first.launch.provider_id, DEFAULT_RUNTIME_ID);
            assert_eq!(first.launch.entrypoint, restarted.launch.entrypoint);
            assert_eq!(
                first.launch.entrypoint,
                first
                    .pack_root
                    .join("platform-artifacts/Tobkiri.app/Contents/MacOS/tobkiri-shell",)
                    .canonicalize()
                    .unwrap()
            );
            assert_eq!(
                first.launch.artifact_digest,
                artifact_tree_digest(&first.pack_root.join("platform-artifacts/Tobkiri.app"))
                    .unwrap()
            );
            assert_eq!(
                first.pack_root,
                config
                    .app_dir
                    .join("ecosystem/defaultspack")
                    .canonicalize()
                    .unwrap()
            );
            assert!(
                !retired.exists(),
                "guardian preparation must not synthesize legacy state for {layout_name}"
            );
            fs::remove_dir_all(root).unwrap();
        }
    }

    #[test]
    fn packaged_bootstrap_shell_tree_digest_is_not_the_shell_pack_aggregate() {
        let (root, config) = fixture("bootstrap-shell-digest-domain");
        let bundle_root = config.app_dir.join("ecosystem/defaultspack/v4");
        let profile: Value =
            serde_json::from_slice(&fs::read(bundle_root.join(PROFILE_PATH)).unwrap()).unwrap();
        let shell_pack: Value =
            serde_json::from_slice(&fs::read(bundle_root.join(SHELL_PACK_PATH)).unwrap()).unwrap();

        let shell_tree_digest = value_str(&profile, "/shell/artifact_digest").unwrap();
        assert_eq!(value_str(&profile, "/state"), Some("needs_resolution"));
        assert!(valid_digest(shell_tree_digest));
        assert_ne!(
            Some(shell_tree_digest),
            value_str(&shell_pack, "/pack/artifact_digest"),
            "bootstrap Profile shell tree and Shell Pack aggregate must remain distinct domains"
        );

        resolve(&config).unwrap();
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn bootstrap_profile_shell_digests_must_match_the_signed_variant() {
        let (root, config) = fixture("bootstrap-shell-signed-variant");
        let bundle_root = config.app_dir.join("ecosystem/defaultspack/v4");
        let original_profile: Value =
            serde_json::from_slice(&fs::read(bundle_root.join(PROFILE_PATH)).unwrap()).unwrap();
        let original_shell = original_profile["shell"].clone();
        let original_tree_digest = value_str(&original_profile, "/shell/artifact_digest")
            .unwrap()
            .to_owned();
        let original_entrypoint_digest =
            value_str(&original_profile, "/shell/executable_artifact_digest")
                .unwrap()
                .to_owned();

        for field in ["artifact_digest", "executable_artifact_digest"] {
            let expected = if field == "artifact_digest" {
                &original_tree_digest
            } else {
                &original_entrypoint_digest
            };
            let replacement = if expected.ends_with(&"0".repeat(64)) {
                format!("sha256:{}", "f".repeat(64))
            } else {
                format!("sha256:{}", "0".repeat(64))
            };
            rewrite_locked_document(&config, PROFILE_PATH, |profile| {
                profile["shell"] = original_shell.clone();
                profile["shell"][field] = Value::String(replacement.clone());
            });

            let profile_raw = fs::read(bundle_root.join(PROFILE_PATH)).unwrap();
            let catalog = crate::presentation::load_catalog(&config).unwrap();
            assert_eq!(catalog.default_profile_digest, sha256(&profile_raw));
            let bundle_root = bundle_root.canonicalize().unwrap();
            let bundle_lock = verify_bundle_lock(&bundle_root).unwrap();
            assert_eq!(
                bundle_lock.authority_digests.get(PROFILE_PATH),
                Some(&catalog.default_profile_digest),
                "the test must preserve the coherent lock/catalog Profile digest"
            );
            let error = resolve(&config).unwrap_err().to_string();
            assert!(
                error.contains("bootstrap Profile Shell differs from its signed artifact variant"),
                "{field} mismatch was accepted or rejected for the wrong reason: {error}"
            );
        }

        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn relocated_entrypoint_tamper_fails_closed_across_packaged_layouts() {
        for (layout_name, layout) in RELOCATION_LAYOUTS {
            let (root, config) =
                fixture_at_layout(&format!("relocated-tamper-{layout_name}"), layout);
            resolve(&config).unwrap();

            fs::write(
                config
                    .app_dir
                    .join("ecosystem/defaultspack/platform-artifacts/Tobkiri.app/Contents/MacOS/tobkiri-shell"),
                b"raise SystemExit(0)\n",
            )
            .unwrap();

            let error = resolve(&config).unwrap_err().to_string();
            assert!(
                error.contains(
                    "application Pack entrypoint escaped or failed artifact verification"
                ),
                "unexpected tamper error for {layout_name}: {error}"
            );
            fs::remove_dir_all(root).unwrap();
        }
    }

    #[test]
    fn macos_non_entrypoint_bundle_tamper_fails_tree_digest() {
        let (root, config) = fixture("macos-resource-tamper");
        resolve(&config).unwrap();
        fs::write(
            config.app_dir.join(
                "ecosystem/defaultspack/platform-artifacts/Tobkiri.app/Contents/Resources/presentation.json",
            ),
            b"tampered presentation fixture",
        )
        .unwrap();
        let error = resolve(&config).unwrap_err().to_string();
        assert!(
            error.contains("failed artifact verification"),
            "unexpected tree tamper error: {error}"
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn duplicate_module_extra_and_wrong_launch_identities_fail_closed() {
        let mutations: &[fn(&mut Value)] = &[
            |pack| {
                pack["artifacts"][0]["argv"] = serde_json::json!(["defaultspack/desktop_app.py"]);
            },
            |pack| {
                pack["artifacts"][0]["argv"] =
                    serde_json::json!(["-m", "ecosystem.defaultspack.desktop_app"]);
            },
            |pack| pack["artifacts"][0]["argv"] = serde_json::json!(["unexpected"]),
            |pack| {
                pack["artifacts"][0]["entrypoint"] =
                    Value::String("runtime/conversation.py".into());
            },
            |pack| {
                pack["artifacts"][0]["digest"] =
                    Value::String(format!("sha256:{}", "0".repeat(64)));
            },
            |pack| pack["functions"][0]["id"] = Value::String("wrong.function".into()),
            |pack| {
                pack["provider_catalog"][0]["provider_id"] = Value::String("wrong.provider".into());
            },
            |pack| {
                let foreign = Value::String("application.foreign".into());
                pack["functions"][0]["id"] = foreign.clone();
                pack["provider_catalog"][0]["provider_id"] = foreign.clone();
                pack["provider_catalog"][0]["owner"] = foreign.clone();
                pack["operation_catalog"][0]["owner"] = foreign.clone();
                pack["operation_catalog"][0]["provider_id"] = foreign;
            },
        ];
        for (index, mutation) in mutations.iter().enumerate() {
            let (root, config) = fixture(&format!("invalid-launch-{index}"));
            rewrite_runtime_pack(&config, *mutation);
            assert!(resolve(&config).is_err(), "mutation {index} was accepted");
            fs::remove_dir_all(root).unwrap();
        }
    }

    #[test]
    fn wrong_profile_path_escape_and_artifact_tamper_fail_closed() {
        let (profile_root, profile_config) = fixture("wrong-profile");
        rewrite_locked_document(&profile_config, PROFILE_PATH, |profile| {
            profile["profile_id"] = Value::String("wrong-profile".into());
        });
        assert!(resolve(&profile_config).is_err());
        fs::remove_dir_all(profile_root).unwrap();

        let (escape_root, escape_config) = fixture("entrypoint-escape");
        rewrite_runtime_pack(&escape_config, |pack| {
            pack["artifacts"][0]["path"] = Value::String("../desktop_app.py".into());
            pack["artifacts"][0]["entrypoint"] = Value::String("../desktop_app.py".into());
        });
        assert!(resolve(&escape_config).is_err());
        fs::remove_dir_all(escape_root).unwrap();

        let (tamper_root, tamper_config) = fixture("entrypoint-tamper");
        fs::write(
            tamper_config
                .app_dir
                .join("ecosystem/defaultspack/platform-artifacts/Tobkiri.app/Contents/MacOS/tobkiri-shell"),
            b"raise SystemExit(0)\n",
        )
        .unwrap();
        assert!(resolve(&tamper_config).is_err());
        fs::remove_dir_all(tamper_root).unwrap();

        let (contract_map_root, contract_map_config) = fixture("frontend-contract-map-tamper");
        fs::write(
            contract_map_config
                .app_dir
                .join("ecosystem/defaultspack/defaultspack/frontend_contract_map.v4.json"),
            b"{}",
        )
        .unwrap();
        assert!(resolve(&contract_map_config).is_err());
        fs::remove_dir_all(contract_map_root).unwrap();
    }

    #[test]
    fn missing_tampered_and_stale_v4_authority_fail_closed() {
        let (missing_root, missing_config) = fixture("missing");
        fs::remove_file(
            missing_config
                .app_dir
                .join("ecosystem/defaultspack/v4/defaults.profile.v4.json"),
        )
        .unwrap();
        assert!(resolve(&missing_config).is_err());
        fs::remove_dir_all(missing_root).unwrap();

        let (tampered_root, tampered_config) = fixture("tampered");
        fs::write(
            tampered_config
                .app_dir
                .join("ecosystem/defaultspack/v4/defaults.profile.v4.json"),
            b"{}",
        )
        .unwrap();
        assert!(resolve(&tampered_config).is_err());
        fs::remove_dir_all(tampered_root).unwrap();

        let (stale_root, stale_config) = fixture("stale");
        let catalog_path = stale_config
            .app_dir
            .join("bundled/presentation_catalog.json");
        let mut catalog: Value = serde_json::from_slice(&fs::read(&catalog_path).unwrap()).unwrap();
        catalog["default_profile_digest"] = Value::String(format!("sha256:{}", "0".repeat(64)));
        fs::write(catalog_path, serde_json::to_vec(&catalog).unwrap()).unwrap();
        assert!(resolve(&stale_config).is_err());
        fs::remove_dir_all(stale_root).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn symlinked_and_escaping_v4_authority_fail_closed() {
        use std::os::unix::fs::symlink;

        let (symlink_root, symlink_config) = fixture("symlink");
        let profile = symlink_config
            .app_dir
            .join("ecosystem/defaultspack/v4/defaults.profile.v4.json");
        let outside = symlink_root.join("outside.profile.json");
        fs::rename(&profile, &outside).unwrap();
        symlink(&outside, &profile).unwrap();
        assert!(resolve(&symlink_config).is_err());
        fs::remove_dir_all(symlink_root).unwrap();

        let (escape_root, escape_config) = fixture("escape");
        let lock_path = escape_config
            .app_dir
            .join("ecosystem/defaultspack/v4/bundle.lock.json");
        let mut lock: Value = serde_json::from_slice(&fs::read(&lock_path).unwrap()).unwrap();
        lock["entries"][0]["path"] = Value::String("../outside.json".to_string());
        fs::write(lock_path, serde_json::to_vec(&lock).unwrap()).unwrap();
        assert!(resolve(&escape_config).is_err());
        fs::remove_dir_all(escape_root).unwrap();

        let (artifact_root, artifact_config) = fixture("artifact-symlink");
        let entrypoint = artifact_config
            .app_dir
            .join("ecosystem/defaultspack/defaultspack/desktop_app.py");
        let outside_entrypoint = artifact_root.join("outside.py");
        fs::rename(&entrypoint, &outside_entrypoint).unwrap();
        symlink(&outside_entrypoint, &entrypoint).unwrap();
        assert!(resolve(&artifact_config).is_err());
        fs::remove_dir_all(artifact_root).unwrap();

        let (contract_map_root, contract_map_config) = fixture("frontend-contract-map-symlink");
        let contract_map = contract_map_config
            .app_dir
            .join("ecosystem/defaultspack/defaultspack/frontend_contract_map.v4.json");
        let outside_contract_map = contract_map_root.join("outside.frontend-contract-map.json");
        fs::rename(&contract_map, &outside_contract_map).unwrap();
        symlink(&outside_contract_map, &contract_map).unwrap();
        assert!(resolve(&contract_map_config).is_err());
        fs::remove_dir_all(contract_map_root).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn unindexed_external_hardlink_in_pack_tree_fails_closed() {
        let (root, config) = fixture("external-hardlink");
        let outside = root.join("outside-runtime.py");
        fs::write(&outside, b"raise SystemExit('outside mutation')\n").unwrap();
        fs::hard_link(
            &outside,
            config
                .app_dir
                .join("ecosystem/defaultspack/unindexed-runtime.py"),
        )
        .unwrap();

        let error = resolve(&config).unwrap_err().to_string();
        assert!(
            error.contains("multiply-linked file"),
            "unexpected hardlink error: {error}"
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[cfg(windows)]
    #[test]
    fn ntfs_hardlink_count_is_detected() {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!(
            "tobkiri-defaultspack-ntfs-hardlink-{}-{unique}",
            std::process::id()
        ));
        fs::create_dir_all(&root).unwrap();
        let source = root.join("source.py");
        let linked = root.join("linked.py");
        fs::write(&source, b"pass\n").unwrap();
        fs::hard_link(&source, &linked).unwrap();

        let metadata = fs::symlink_metadata(&linked).unwrap();
        assert!(has_multiple_links(&linked, &metadata).unwrap());
        fs::remove_dir_all(root).unwrap();
    }
}
