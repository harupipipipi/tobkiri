//! Launcher-owned Base Pack/Shell selection and production launch boundary.
//!
//! The catalog is metadata only. Selecting a Base Pack or Shell never creates a
//! Grant and never executes Pack code. A launch is allowed only after the
//! selected platform artifact has been verified as a prebuilt production
//! artifact. Development commands are deliberately not represented as a
//! launch fallback.

use std::collections::{BTreeMap, BTreeSet};
use std::ffi::OsString;
use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::path::{Component, Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use anyhow::{bail, Context, Result as AnyResult};
use base64::{engine::general_purpose::STANDARD as BASE64, Engine as _};
use ed25519_dalek::{Signature, Verifier, VerifyingKey};
use log::{error, warn};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::artifact_integrity;
use tauri::{AppHandle, State, Url, WebviewWindow};

use crate::config::AppConfig;

const CATALOG_SCHEMA: &str = "io.tobkiri.launcher.presentation-catalog.v1";
const SHELL_CONTRACT_ID: &str = "app.shell.v1";
const SELECTION_DIR: &str = "presentation";
const SELECTION_FILE: &str = "selection.json";
const SELECTION_SCHEMA: &str = "io.tobkiri.launcher.profile-selection.v4";
const RELEASE_SCHEMA: &str = "io.tobkiri.shell.release.v4";
const RELEASE_FILE: &str = "presentation_release.v4.json";
const LAUNCHER_PANEL_PORT: u16 = 8765;
// Both macOS waits stay below the Shell handoff's 60-second validity window.
// A suspended or unresponsive Shell therefore cannot turn an expired handoff
// into a successful Launcher response.
const MACOS_LAUNCH_COMMAND_TIMEOUT: Duration = Duration::from_secs(15);
const MACOS_HANDOFF_ACK_TIMEOUT: Duration = Duration::from_secs(15);
const LAUNCH_POLL_INTERVAL: Duration = Duration::from_millis(25);
const PRESENTATION_CALLER_DENIED: &str =
    "presentation access is unavailable from this Launcher window";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum PresentationCallerDenial {
    WindowLabel,
    ConfiguredPort,
    Scheme,
    Host,
    Port,
    Route,
}

impl PresentationCallerDenial {
    fn audit_code(self) -> &'static str {
        match self {
            Self::WindowLabel => "window_label",
            Self::ConfiguredPort => "configured_port",
            Self::Scheme => "scheme",
            Self::Host => "host",
            Self::Port => "port",
            Self::Route => "route",
        }
    }
}

fn validate_presentation_caller_context(
    window_label: &str,
    url: &Url,
    configured_port: u16,
) -> Result<(), PresentationCallerDenial> {
    if window_label != "main" {
        return Err(PresentationCallerDenial::WindowLabel);
    }
    if configured_port == 0 {
        return Err(PresentationCallerDenial::ConfiguredPort);
    }
    if url.scheme() != "http" {
        return Err(PresentationCallerDenial::Scheme);
    }
    if !matches!(url.host_str(), Some("127.0.0.1") | Some("localhost")) {
        return Err(PresentationCallerDenial::Host);
    }
    if url.port_or_known_default() != Some(configured_port) {
        return Err(PresentationCallerDenial::Port);
    }
    if url.path() != "/panel" && !url.path().starts_with("/panel/") {
        return Err(PresentationCallerDenial::Route);
    }
    Ok(())
}

fn validate_presentation_caller(window: &WebviewWindow, config: &AppConfig) -> Result<(), String> {
    let url = window.url().map_err(|error| {
        error!("presentation IPC caller inspection failed: {error}");
        PRESENTATION_CALLER_DENIED.to_string()
    })?;
    validate_presentation_caller_context(window.label(), &url, config.kernel_port).map_err(
        |denial| {
            // Do not log the current URL: its query may contain a panel session code.
            warn!(
                "presentation IPC denied: caller_class={} reason={}",
                if window.label() == "main" {
                    "main"
                } else {
                    "non_main"
                },
                denial.audit_code()
            );
            PRESENTATION_CALLER_DENIED.to_string()
        },
    )
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PresentationApproval {
    pub state: String,
    pub provider_trust: String,
    pub grant_state: String,
    pub authority_mode: String,
    pub execution_domain: String,
    pub effect_scope: Vec<String>,
    pub blast_radius: String,
    #[serde(default)]
    pub reason: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BasePackDescriptor {
    pub pack_id: String,
    pub display_name: String,
    pub version: String,
    pub artifact_digest: String,
    pub backend_provider_ids: Vec<String>,
    pub state_owners: Vec<String>,
    pub backend_identity_digest: String,
    pub required_capabilities: Vec<String>,
    pub allowed_families: Vec<String>,
    pub approval: PresentationApproval,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PresentationContribution {
    pub contribution_id: String,
    pub owner_pack_id: String,
    pub contract_id: String,
    pub contract_revision_digest: String,
    pub family: String,
    pub label: String,
    pub artifact_ref: String,
    pub digest: String,
    pub presentation_kind: String,
    pub technology: String,
    pub host_authority: String,
    pub materialization: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ArtifactVariant {
    pub artifact_id: String,
    pub variant: String,
    pub platform: String,
    pub architecture: String,
    pub artifact_ref: String,
    pub entrypoint: String,
    pub artifact_kind: String,
    pub descriptor_digest: String,
    #[serde(default)]
    pub path: Option<String>,
    #[serde(default)]
    pub sha256: Option<String>,
    #[serde(default)]
    pub entrypoint_sha256: Option<String>,
    #[serde(default)]
    pub size: Option<u64>,
    #[serde(default)]
    pub source_identity: Option<String>,
    #[serde(default)]
    pub source_revision: Option<String>,
    pub prebuilt: bool,
    pub production: bool,
    #[serde(default)]
    pub development_command: Option<String>,
    #[serde(default)]
    pub bundle_identifier: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PresentationArtifact {
    pub artifact_id: String,
    pub variant: String,
    pub platform: String,
    pub architecture: String,
    #[serde(default)]
    pub path: Option<String>,
    #[serde(default)]
    pub sha256: Option<String>,
    #[serde(default)]
    pub size: Option<u64>,
    #[serde(default)]
    pub source_identity: Option<String>,
    #[serde(default)]
    pub source_revision: Option<String>,
    pub prebuilt: bool,
    pub production: bool,
    #[serde(default)]
    pub development_command: Option<String>,
    #[serde(default)]
    pub bundle_identifier: Option<String>,
    pub status: String,
    pub status_detail: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ShellProviderDescriptor {
    pub provider_id: String,
    pub display_name: String,
    pub contract_id: String,
    pub contract_revision_digest: String,
    pub experience_role: String,
    pub presentation_kind: String,
    pub presentation_family: String,
    pub technology: String,
    pub capabilities: Vec<String>,
    pub consumes_contracts: Vec<String>,
    pub contributions: Vec<PresentationContribution>,
    pub artifact_variants: Vec<ArtifactVariant>,
    #[serde(default)]
    pub artifact: Option<PresentationArtifact>,
    pub approval: PresentationApproval,
    #[serde(default)]
    pub protocol_revision_digest: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ContractRevisionDescriptor {
    pub contract_id: String,
    pub revision: String,
    pub digest: String,
    pub source_path: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PresentationCatalog {
    pub schema: String,
    pub generator: String,
    pub generator_version: String,
    pub default_profile_id: String,
    pub default_profile_source: String,
    pub default_profile_digest: String,
    pub default_selection: PresentationSelection,
    pub contract_revisions: Vec<ContractRevisionDescriptor>,
    pub source_manifest_digests: BTreeMap<String, String>,
    pub base_packs: Vec<BasePackDescriptor>,
    pub shell_providers: Vec<ShellProviderDescriptor>,
    #[serde(default)]
    pub generated_at: u64,
    #[serde(default)]
    pub release_binding: Option<PresentationReleaseBinding>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PresentationReleaseBinding {
    pub schema: String,
    pub artifact_index_path: String,
    pub artifact_index_sha256: String,
    pub profile_lock_path: String,
    pub profile_lock_sha256: String,
    pub catalog_revision: String,
    pub artifact_id: String,
    pub source_identity: String,
    pub source_revision: String,
    pub platform: String,
    pub architecture: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct PresentationReleaseManifest {
    schema: String,
    catalog_path: String,
    catalog_sha256: String,
    artifact_index_path: String,
    artifact_index_sha256: String,
    profile_lock_path: String,
    profile_lock_sha256: String,
    default_profile_path: String,
    default_profile_sha256: String,
    defaultspack_lock_path: String,
    defaultspack_lock_sha256: String,
    artifact_id: String,
    platform: String,
    architecture: String,
    source_identity: String,
    source_revision: String,
    key_id: String,
    public_key: String,
    signature: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ShellArtifactIndex {
    schema: String,
    artifact_id: String,
    path: String,
    sha256: String,
    entrypoint_sha256: String,
    size: u64,
    platform: String,
    architecture: String,
    source_identity: String,
    source_revision: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ShellProfileLock {
    schema: String,
    catalog_revision: String,
    artifact_index_sha256: String,
    artifact_id: String,
    artifact_sha256: String,
    entrypoint_sha256: String,
    platform: String,
    architecture: String,
    source_identity: String,
    source_revision: String,
    lock_revision: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct PresentationSelection {
    pub base_pack_id: String,
    pub shell_provider_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
struct StoredProfileSelection {
    schema: String,
    catalog_revision: String,
    base_pack_id: String,
    base_artifact_digest: String,
    shell_provider_id: String,
    shell_contract_revision_digest: String,
    shell_artifact_id: String,
    shell_artifact_digest: String,
    platform: String,
    architecture: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PresentationMaterialization {
    pub status: String,
    #[serde(default)]
    pub base_pack_id: Option<String>,
    #[serde(default)]
    pub shell_provider_id: Option<String>,
    pub selected_contributions: Vec<PresentationContribution>,
    #[serde(default)]
    pub artifact: Option<PresentationArtifact>,
    #[serde(default)]
    pub reason: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PresentationState {
    pub catalog: PresentationCatalog,
    #[serde(default)]
    pub selection: Option<PresentationSelection>,
    pub materialization: PresentationMaterialization,
}

#[derive(Debug, Clone, Serialize)]
pub struct PresentationLaunchResponse {
    pub status: String,
    pub provider_id: String,
    pub artifact_id: String,
    pub message: String,
}

#[tauri::command]
pub fn get_presentation_catalog(
    window: WebviewWindow,
    config: State<'_, AppConfig>,
) -> Result<PresentationState, String> {
    validate_presentation_caller(&window, config.inner())?;
    build_state(config.inner()).map_err(|error| {
        error!("presentation catalog could not be loaded: {error:#}");
        "presentation catalog is unavailable".to_string()
    })
}

#[tauri::command]
pub fn select_presentation(
    window: WebviewWindow,
    config: State<'_, AppConfig>,
    selection: PresentationSelection,
) -> Result<PresentationState, String> {
    validate_presentation_caller(&window, config.inner())?;
    select_presentation_impl(config.inner(), selection).map_err(|error| {
        error!("presentation selection could not be saved: {error:#}");
        "presentation selection could not be saved".to_string()
    })
}

#[tauri::command]
pub async fn launch_selected_presentation(
    window: WebviewWindow,
    app: AppHandle,
    config: State<'_, AppConfig>,
) -> Result<PresentationLaunchResponse, String> {
    validate_presentation_caller(&window, config.inner())?;
    let app_handle = app.clone();
    let app_config = config.inner().clone();
    let launch_result = tauri::async_runtime::spawn_blocking(move || {
        launch_selected_presentation_impl(&app_handle, &app_config)
    })
    .await
    .map_err(|error| {
        error!("selected presentation launch task failed: {error}");
        "selected presentation could not be launched".to_string()
    })?;
    launch_result.map_err(|error| {
        error!("selected presentation launch blocked: {error:#}");
        "selected presentation could not be launched".to_string()
    })
}

fn select_presentation_impl(
    config: &AppConfig,
    selection: PresentationSelection,
) -> AnyResult<PresentationState> {
    let catalog = load_catalog(config)?;
    validate_selection(&catalog, &selection)?;
    let state = build_state_from_catalog(config, catalog, Some(selection.clone()))?;
    if state.materialization.status != "materialized" {
        bail!("selection cannot be saved until its exact artifact is verified");
    }
    write_selection(config, &state.catalog, &selection)?;
    Ok(state)
}

pub(crate) fn launch_selected_presentation_impl(
    app: &AppHandle,
    config: &AppConfig,
) -> AnyResult<PresentationLaunchResponse> {
    let state = build_state(config)?;
    let selection = state
        .selection
        .as_ref()
        .context("no Base Pack and Shell selection has been saved")?;
    if state.materialization.status != "materialized" {
        bail!(
            "selected presentation launch is blocked: {}",
            state
                .materialization
                .reason
                .as_deref()
                .unwrap_or("materialization did not complete")
        );
    }

    let shell = state
        .catalog
        .shell_providers
        .iter()
        .find(|candidate| candidate.provider_id == selection.shell_provider_id)
        .context("selected Shell Provider is no longer in the verified catalog")?;
    let artifact = shell
        .artifact
        .as_ref()
        .context("selected Shell Provider has no platform artifact")?;
    validate_production_artifact(artifact)?;
    let artifact_path = artifact_path(config, artifact)?;
    let catalog_revision = catalog_revision(&state.catalog)?;

    // Runtime readiness and local authentication are resolved only after the
    // exact verified Shell artifact has passed pre-admission. The authenticated
    // URL never crosses argv or the environment; only an owner-only one-shot
    // handoff path is passed to the presentation process.
    let runtime_url =
        crate::dock_registration::prepare_defaultspack_shell_runtime_url(app, config)?;
    let handoff_path = crate::shell_handoff::create_shell_handoff(
        config,
        crate::shell_handoff::ShellHandoffBinding {
            profile_id: &state.catalog.default_profile_id,
            profile_digest: &state.catalog.default_profile_digest,
            catalog_revision: &catalog_revision,
            provider_id: &shell.provider_id,
            artifact_id: &artifact.artifact_id,
        },
        &runtime_url,
    )?;

    if let Err(error) = launch_verified_artifact(&artifact_path, &handoff_path) {
        crate::shell_handoff::discard_shell_handoff(&handoff_path);
        return Err(error);
    }

    Ok(PresentationLaunchResponse {
        status: "launched".to_string(),
        provider_id: shell.provider_id.clone(),
        artifact_id: artifact.artifact_id.clone(),
        message: format!(
            "{} launched from its verified production artifact.",
            shell.display_name
        ),
    })
}

#[derive(Debug, PartialEq, Eq)]
struct VerifiedLaunchSpec {
    program: PathBuf,
    args: Vec<OsString>,
}

fn verified_launch_spec(
    platform: &str,
    artifact_path: &Path,
    handoff_path: &Path,
) -> AnyResult<VerifiedLaunchSpec> {
    if !artifact_path.is_absolute() {
        bail!("verified Shell artifact launch path must be absolute");
    }
    if !handoff_path.is_absolute() {
        bail!("verified Shell handoff path must be absolute");
    }
    let handoff_flag = OsString::from(crate::shell_handoff::HANDOFF_ARGUMENT);
    let handoff_value = handoff_path.as_os_str().to_owned();
    match platform {
        // LaunchServices is required to open an application bundle. `-n`
        // guarantees that the handoff path reaches a process; the Shell's own
        // single-instance plugin forwards that path to an existing Shell when
        // needed. The authenticated URL itself never appears in argv.
        "macos" => Ok(VerifiedLaunchSpec {
            program: PathBuf::from("/usr/bin/open"),
            args: vec![
                OsString::from("-n"),
                artifact_path.as_os_str().to_owned(),
                OsString::from("--args"),
                handoff_flag,
                handoff_value,
            ],
        }),
        // Linux AppImages and Windows executables are the verified artifacts,
        // so execute their exact absolute paths without a shell or PATH lookup.
        "linux" | "windows" => Ok(VerifiedLaunchSpec {
            program: artifact_path.to_path_buf(),
            args: vec![handoff_flag, handoff_value],
        }),
        other => bail!("verified Shell artifact launch is unsupported on {other}"),
    }
}

trait LaunchProcess {
    fn try_wait_success(&mut self) -> io::Result<Option<bool>>;

    fn terminate(&mut self) -> io::Result<()>;
}

impl LaunchProcess for Child {
    fn try_wait_success(&mut self) -> io::Result<Option<bool>> {
        self.try_wait()
            .map(|status| status.map(|exit_status| exit_status.success()))
    }

    fn terminate(&mut self) -> io::Result<()> {
        if self.try_wait()?.is_some() {
            return Ok(());
        }
        if let Err(error) = self.kill() {
            if self.try_wait()?.is_none() {
                return Err(error);
            }
        }
        self.wait().map(|_| ())
    }
}

fn wait_for_launch_success<P: LaunchProcess>(process: &mut P) -> AnyResult<()> {
    let started = Instant::now();
    wait_for_launch_success_with(
        process,
        MACOS_LAUNCH_COMMAND_TIMEOUT,
        || started.elapsed(),
        thread::sleep,
    )
}

fn wait_for_launch_success_with<P, C, S>(
    process: &mut P,
    timeout: Duration,
    mut elapsed: C,
    mut sleep: S,
) -> AnyResult<()>
where
    P: LaunchProcess,
    C: FnMut() -> Duration,
    S: FnMut(Duration),
{
    loop {
        if let Some(success) = process
            .try_wait_success()
            .context("failed to inspect verified Shell launch command")?
        {
            if success {
                return Ok(());
            }
            bail!("verified Shell launch command exited unsuccessfully");
        }

        if elapsed() >= timeout {
            process
                .terminate()
                .context("failed to terminate timed-out verified Shell launch command")?;
            bail!(
                "verified Shell launch command timed out after {} ms",
                timeout.as_millis()
            );
        }
        sleep(LAUNCH_POLL_INTERVAL);
    }
}

fn handoff_was_consumed(path: &Path) -> io::Result<bool> {
    match fs::symlink_metadata(path) {
        Ok(_) => Ok(false),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(true),
        Err(error) => Err(error),
    }
}

fn wait_for_handoff_consumed(path: &Path) -> AnyResult<()> {
    let started = Instant::now();
    wait_for_handoff_consumed_with(
        path,
        MACOS_HANDOFF_ACK_TIMEOUT,
        handoff_was_consumed,
        || started.elapsed(),
        thread::sleep,
    )
}

fn wait_for_handoff_consumed_with<A, C, S>(
    path: &Path,
    timeout: Duration,
    mut acknowledged: A,
    mut elapsed: C,
    mut sleep: S,
) -> AnyResult<()>
where
    A: FnMut(&Path) -> io::Result<bool>,
    C: FnMut() -> Duration,
    S: FnMut(Duration),
{
    loop {
        if acknowledged(path).with_context(|| {
            format!(
                "failed to inspect verified Shell handoff acknowledgement at {}",
                path.display()
            )
        })? {
            return Ok(());
        }

        if elapsed() >= timeout {
            bail!(
                "timed out waiting for verified Shell to consume handoff after {} ms",
                timeout.as_millis()
            );
        }
        sleep(LAUNCH_POLL_INTERVAL);
    }
}

fn launch_verified_artifact(artifact_path: &Path, handoff_path: &Path) -> AnyResult<()> {
    let spec = verified_launch_spec(current_platform(), artifact_path, handoff_path)?;
    let mut process = Command::new(&spec.program)
        .args(&spec.args)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .with_context(|| {
            format!(
                "failed to launch verified Shell artifact {}",
                artifact_path.display()
            )
        })?;

    if current_platform() == "macos" {
        // `/usr/bin/open` is only a LaunchServices request. Its spawn result
        // does not say whether the request itself succeeded or whether the
        // Shell ever consumed the authenticated handoff.
        wait_for_launch_success(&mut process)?;
        wait_for_handoff_consumed(handoff_path)?;
    }

    Ok(())
}

fn build_state(config: &AppConfig) -> AnyResult<PresentationState> {
    let catalog = load_catalog(config)?;
    let selection = read_selection(config, &catalog)?;
    build_state_from_catalog(config, catalog, selection)
}

fn build_state_from_catalog(
    config: &AppConfig,
    mut catalog: PresentationCatalog,
    selection: Option<PresentationSelection>,
) -> AnyResult<PresentationState> {
    catalog.generated_at = now_seconds();
    for shell in &mut catalog.shell_providers {
        shell.artifact = Some(resolve_artifact(config, shell)?);
    }

    let materialization = match selection.as_ref() {
        Some(selection) => materialize_selection(&catalog, selection),
        None => PresentationMaterialization {
            status: "not_selected".to_string(),
            base_pack_id: None,
            shell_provider_id: None,
            selected_contributions: Vec::new(),
            artifact: None,
            reason: Some("Choose a Base Pack and a compatible Shell Provider.".to_string()),
        },
    };

    Ok(PresentationState {
        catalog,
        selection,
        materialization,
    })
}

fn presentation_catalog_path(config: &AppConfig) -> PathBuf {
    config
        .app_dir
        .join("bundled")
        .join("presentation_catalog.json")
}

pub(crate) fn load_catalog(config: &AppConfig) -> AnyResult<PresentationCatalog> {
    let path = presentation_catalog_path(config);
    let raw = fs::read_to_string(&path).with_context(|| {
        format!(
            "verified presentation catalog is required at {} (app root {}); no embedded, environment, or path fallback is permitted",
            path.display(),
            config.app_dir.display()
        )
    })?;
    let source = format!("packaged resource {}", path.display());
    let catalog: PresentationCatalog = serde_json::from_str(&raw).with_context(|| {
        format!("manifest-derived presentation catalog from {source} is malformed and was rejected")
    })?;
    if catalog.schema != CATALOG_SCHEMA {
        bail!(
            "unsupported manifest-derived presentation catalog schema from {source}: {}",
            catalog.schema,
        );
    }
    if catalog.base_packs.is_empty() || catalog.shell_providers.is_empty() {
        bail!(
            "manifest-derived presentation catalog from {source} must contain a Base Pack and Shell Provider"
        );
    }
    validate_catalog_integrity(&catalog).with_context(|| {
        format!("manifest-derived presentation catalog from {source} failed integrity validation")
    })?;
    verify_release_binding(config, &path, &raw, &catalog)
        .with_context(|| format!("signed Shell v4 release binding from {source} was rejected"))?;
    Ok(catalog)
}

fn verify_release_binding(
    config: &AppConfig,
    catalog_path: &Path,
    catalog_raw: &str,
    catalog: &PresentationCatalog,
) -> AnyResult<()> {
    let installed = catalog
        .shell_providers
        .iter()
        .flat_map(|shell| &shell.artifact_variants)
        .any(|variant| {
            variant.path.is_some()
                || variant.sha256.is_some()
                || variant.entrypoint_sha256.is_some()
                || variant.size.is_some()
        });
    let Some(binding) = catalog.release_binding.as_ref() else {
        if installed {
            bail!("installed Shell metadata requires a signed release binding");
        }
        return Ok(());
    };
    if binding.schema != RELEASE_SCHEMA {
        bail!(
            "unsupported Shell release binding schema: {}",
            binding.schema
        );
    }
    let manifest_path = config.app_dir.join("bundled").join(RELEASE_FILE);
    let manifest_raw = read_verified_regular_file(&manifest_path, "Shell release manifest")?;
    let manifest: PresentationReleaseManifest =
        serde_json::from_slice(&manifest_raw).context("Shell release manifest is malformed")?;
    if manifest.schema != RELEASE_SCHEMA
        || manifest.catalog_path != "bundled/presentation_catalog.json"
        || manifest.artifact_index_path != binding.artifact_index_path
        || manifest.profile_lock_path != binding.profile_lock_path
        || manifest.default_profile_path != "ecosystem/defaultspack/v4/defaults.profile.v4.json"
        || manifest.defaultspack_lock_path != "ecosystem/defaultspack/v4/bundle.lock.json"
        || manifest.artifact_id != binding.artifact_id
        || manifest.platform != binding.platform
        || manifest.architecture != binding.architecture
        || manifest.source_identity != binding.source_identity
        || manifest.source_revision != binding.source_revision
    {
        bail!("Shell release manifest does not exactly match its catalog binding");
    }
    if byte_digest(catalog_raw.as_bytes()) != manifest.catalog_sha256 {
        bail!("packaged presentation catalog digest does not match the signed release");
    }

    let index_path = safe_fixed_release_path(config, &manifest.artifact_index_path)?;
    let lock_path = safe_fixed_release_path(config, &manifest.profile_lock_path)?;
    let index_raw = read_verified_regular_file(&index_path, "Shell artifact index")?;
    let lock_raw = read_verified_regular_file(&lock_path, "Shell profile lock")?;
    if byte_digest(&index_raw) != manifest.artifact_index_sha256
        || byte_digest(&lock_raw) != manifest.profile_lock_sha256
    {
        bail!("signed Shell release index or lock digest does not match packaged bytes");
    }
    let profile_path = safe_artifact_path(config, &manifest.default_profile_path)?;
    let defaultspack_lock_path = safe_artifact_path(config, &manifest.defaultspack_lock_path)?;
    let profile_raw = read_verified_regular_file(&profile_path, "default Profile")?;
    let defaultspack_lock_raw =
        read_verified_regular_file(&defaultspack_lock_path, "Defaults bundle lock")?;
    if byte_digest(&profile_raw) != manifest.default_profile_sha256
        || byte_digest(&defaultspack_lock_raw) != manifest.defaultspack_lock_sha256
        || catalog.default_profile_digest != manifest.default_profile_sha256
    {
        bail!("packaged Defaults Profile/lock identity differs from the signed release");
    }
    let defaultspack_lock: serde_json::Value = serde_json::from_slice(&defaultspack_lock_raw)
        .context("Defaults bundle lock is malformed")?;
    let entries = defaultspack_lock
        .get("entries")
        .and_then(serde_json::Value::as_array)
        .context("Defaults bundle lock entries are missing")?;
    let profile_bindings = entries
        .iter()
        .filter(|entry| {
            entry.get("path").and_then(serde_json::Value::as_str)
                == Some("defaults.profile.v4.json")
                && entry.get("kind").and_then(serde_json::Value::as_str) == Some("profile")
                && entry.get("digest").and_then(serde_json::Value::as_str)
                    == Some(manifest.default_profile_sha256.as_str())
        })
        .count();
    if profile_bindings != 1 {
        bail!("Defaults bundle lock does not bind the signed default Profile");
    }

    let index_value: serde_json::Value =
        serde_json::from_slice(&index_raw).context("Shell artifact index is malformed")?;
    let lock_value: serde_json::Value =
        serde_json::from_slice(&lock_raw).context("Shell profile lock is malformed")?;
    if canonical_value_digest(&index_value)? != binding.artifact_index_sha256
        || canonical_value_digest(&lock_value)? != binding.profile_lock_sha256
    {
        bail!("Shell catalog binding does not match the exact index/lock content");
    }
    let index: ShellArtifactIndex =
        serde_json::from_value(index_value).context("Shell artifact index fields are invalid")?;
    let lock: ShellProfileLock = serde_json::from_value(lock_value.clone())
        .context("Shell profile lock fields are invalid")?;
    let mut lock_body = lock_value;
    lock_body
        .as_object_mut()
        .context("Shell profile lock must be an object")?
        .remove("lock_revision");
    if index.schema != "io.tobkiri.shell.artifact-index.v4"
        || lock.schema != "io.tobkiri.shell.profile-lock.v4"
        || lock.lock_revision != canonical_value_digest(&lock_body)?
        || lock.catalog_revision != binding.catalog_revision
        || lock.artifact_index_sha256 != binding.artifact_index_sha256
        || index.artifact_id != binding.artifact_id
        || lock.artifact_id != binding.artifact_id
        || index.sha256 != lock.artifact_sha256
        || index.entrypoint_sha256 != lock.entrypoint_sha256
        || index.platform != binding.platform
        || lock.platform != binding.platform
        || index.architecture != binding.architecture
        || lock.architecture != binding.architecture
        || index.source_identity != binding.source_identity
        || lock.source_identity != binding.source_identity
        || index.source_revision != binding.source_revision
        || lock.source_revision != binding.source_revision
    {
        bail!("Shell artifact index/profile lock exact binding is inconsistent");
    }
    validate_release_target(&binding.platform, &binding.architecture)?;
    let selected_shell = catalog
        .shell_providers
        .iter()
        .find(|shell| shell.provider_id == catalog.default_selection.shell_provider_id)
        .context("default Profile Shell is missing from the catalog")?;
    let variant = selected_shell
        .artifact_variants
        .iter()
        .find(|variant| variant.artifact_id == binding.artifact_id)
        .context("signed Shell artifact does not match the default Profile Shell")?;
    if variant.path.as_deref() != Some(index.path.as_str())
        || variant.sha256.as_deref() != Some(index.sha256.as_str())
        || variant.entrypoint_sha256.as_deref() != Some(index.entrypoint_sha256.as_str())
        || variant.size != Some(index.size)
        || variant.source_identity.as_deref() != Some(index.source_identity.as_str())
        || variant.source_revision.as_deref() != Some(index.source_revision.as_str())
    {
        bail!("catalog variant differs from the signed Shell artifact index");
    }

    let embedded_key = option_env!("TOBKIRI_PRESENTATION_TRUST_KEY_B64").unwrap_or("");
    let embedded_key_id = option_env!("TOBKIRI_PRESENTATION_TRUST_KEY_ID").unwrap_or("");
    if embedded_key.is_empty()
        || embedded_key_id.is_empty()
        || manifest.public_key != embedded_key
        || manifest.key_id != embedded_key_id
    {
        bail!("Shell release signer is not the compile-time trusted build signer");
    }
    let key_bytes: [u8; 32] = BASE64
        .decode(&manifest.public_key)
        .context("Shell release public key is not base64")?
        .try_into()
        .map_err(|_| anyhow::anyhow!("Shell release public key must be 32 bytes"))?;
    let signature_bytes: [u8; 64] = BASE64
        .decode(&manifest.signature)
        .context("Shell release signature is not base64")?
        .try_into()
        .map_err(|_| anyhow::anyhow!("Shell release signature must be 64 bytes"))?;
    VerifyingKey::from_bytes(&key_bytes)
        .context("Shell release public key is invalid")?
        .verify(
            &release_signature_message(&manifest),
            &Signature::from_bytes(&signature_bytes),
        )
        .context("Shell release signature verification failed")?;

    // Ensure the caller-provided catalog path is the fixed packaged location.
    if catalog_path != presentation_catalog_path(config) {
        bail!("presentation catalog path differs from the packaged contract");
    }
    Ok(())
}

fn validate_release_target(platform: &str, architecture: &str) -> AnyResult<()> {
    if platform != current_platform() || architecture != current_architecture() {
        bail!("signed Shell release targets the wrong platform or architecture");
    }
    Ok(())
}

fn release_signature_message(manifest: &PresentationReleaseManifest) -> Vec<u8> {
    [
        RELEASE_SCHEMA,
        &manifest.catalog_sha256,
        &manifest.artifact_index_sha256,
        &manifest.profile_lock_sha256,
        &manifest.default_profile_sha256,
        &manifest.defaultspack_lock_sha256,
        &manifest.source_identity,
        &manifest.source_revision,
        &manifest.platform,
        &manifest.architecture,
        &manifest.artifact_id,
        &manifest.key_id,
    ]
    .join("\0")
    .into_bytes()
}

fn safe_fixed_release_path(config: &AppConfig, relative: &str) -> AnyResult<PathBuf> {
    if !matches!(
        relative,
        "bundled/shell_artifact_index.v4.json" | "bundled/shell_profile_lock.v4.json"
    ) {
        bail!("Shell release binding uses a non-canonical path");
    }
    safe_artifact_path(config, relative)
}

fn read_verified_regular_file(path: &Path, label: &str) -> AnyResult<Vec<u8>> {
    let metadata = fs::symlink_metadata(path)
        .with_context(|| format!("{label} is missing at {}", path.display()))?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        bail!("{label} must be a regular non-symlink file");
    }
    fs::read(path).with_context(|| format!("failed to read {label} at {}", path.display()))
}

fn byte_digest(bytes: &[u8]) -> String {
    format!("sha256:{}", hex::encode(Sha256::digest(bytes)))
}

fn canonical_value_digest(value: &serde_json::Value) -> AnyResult<String> {
    let bytes = serde_json::to_vec(value).context("failed to canonicalize release JSON")?;
    Ok(byte_digest(&bytes))
}

fn validate_catalog_integrity(catalog: &PresentationCatalog) -> AnyResult<()> {
    if catalog.generator.trim().is_empty() || catalog.generator_version.trim().is_empty() {
        bail!("presentation catalog generator metadata is missing");
    }
    if catalog.default_profile_id.trim().is_empty()
        || catalog.default_profile_source.trim().is_empty()
        || !is_sha256_digest(&catalog.default_profile_digest)
    {
        bail!("presentation catalog default profile metadata is invalid");
    }
    if catalog.default_selection.base_pack_id.trim().is_empty()
        || catalog
            .default_selection
            .shell_provider_id
            .trim()
            .is_empty()
    {
        bail!("presentation catalog default selection is incomplete");
    }
    if catalog.source_manifest_digests.is_empty()
        || catalog
            .source_manifest_digests
            .values()
            .any(|digest| !is_sha256_digest(digest))
    {
        bail!("presentation catalog source manifest digests are incomplete");
    }

    let mut base_ids = std::collections::HashSet::new();
    for base in &catalog.base_packs {
        if !base_ids.insert(&base.pack_id) {
            bail!("presentation catalog contains duplicate Base Pack IDs");
        }
        if !is_sha256_digest(&base.artifact_digest)
            || !is_sha256_digest(&base.backend_identity_digest)
            || base.backend_provider_ids.is_empty()
            || base.state_owners.is_empty()
        {
            bail!(
                "Base Pack {} has incomplete backend identity metadata",
                base.pack_id
            );
        }
        validate_approval(&base.approval, &base.pack_id)?;
    }

    let mut contract_ids = std::collections::HashSet::new();
    for revision in &catalog.contract_revisions {
        if !contract_ids.insert(&revision.contract_id) {
            bail!("presentation catalog contains duplicate contract revisions");
        }
        if !is_sha256_digest(&revision.digest) || revision.source_path.trim().is_empty() {
            bail!("contract revision {} is incomplete", revision.contract_id);
        }
    }
    let revision_digests = catalog
        .contract_revisions
        .iter()
        .map(|revision| (revision.contract_id.as_str(), revision.digest.as_str()))
        .collect::<BTreeMap<_, _>>();

    let mut shell_ids = std::collections::HashSet::new();
    let mut artifact_ids = std::collections::HashSet::new();
    for shell in &catalog.shell_providers {
        if !shell_ids.insert(&shell.provider_id) {
            bail!("presentation catalog contains duplicate Shell Provider IDs");
        }
        if shell.contract_id != SHELL_CONTRACT_ID
            || !is_sha256_digest(&shell.contract_revision_digest)
            || shell.consumes_contracts.is_empty()
            || shell.artifact_variants.is_empty()
            || revision_digests.get(shell.contract_id.as_str()).copied()
                != Some(shell.contract_revision_digest.as_str())
        {
            bail!(
                "Shell Provider {} has incomplete contract metadata",
                shell.provider_id
            );
        }
        if shell
            .consumes_contracts
            .iter()
            .any(|contract| !revision_digests.contains_key(contract.as_str()))
        {
            bail!(
                "Shell Provider {} consumes an unregistered contract",
                shell.provider_id
            );
        }
        if shell
            .protocol_revision_digest
            .as_deref()
            .is_some_and(|digest| !is_sha256_digest(digest))
        {
            bail!(
                "Shell Provider {} has an invalid protocol revision",
                shell.provider_id
            );
        }
        validate_approval(&shell.approval, &shell.provider_id)?;
        let mut contribution_ids = std::collections::HashSet::new();
        for contribution in &shell.contributions {
            if !contribution_ids.insert(&contribution.contribution_id)
                || contribution.owner_pack_id.trim().is_empty()
                || contribution.artifact_ref.trim().is_empty()
                || !is_sha256_digest(&contribution.digest)
                || !is_sha256_digest(&contribution.contract_revision_digest)
                || revision_digests
                    .get(contribution.contract_id.as_str())
                    .copied()
                    != Some(contribution.contract_revision_digest.as_str())
                || contribution.materialization != "selected_only"
            {
                bail!(
                    "Shell Provider {} has invalid contribution metadata",
                    shell.provider_id
                );
            }
        }
        let mut variants = std::collections::HashSet::new();
        for variant in &shell.artifact_variants {
            if !variants.insert((&variant.platform, &variant.architecture))
                || !artifact_ids.insert(&variant.artifact_id)
                || variant.artifact_id.trim().is_empty()
                || variant.artifact_ref.trim().is_empty()
                || variant.entrypoint.trim().is_empty()
                || !is_sha256_digest(&variant.descriptor_digest)
                || !variant.prebuilt
                || !variant.production
                || variant
                    .development_command
                    .as_deref()
                    .is_some_and(|command| !command.trim().is_empty())
            {
                bail!(
                    "Shell Provider {} has an invalid production variant",
                    shell.provider_id
                );
            }
            if variant
                .sha256
                .as_deref()
                .is_some_and(|digest| !is_sha256_digest(digest))
            {
                bail!(
                    "Shell Provider {} has an invalid installed digest",
                    shell.provider_id
                );
            }
            if variant
                .entrypoint_sha256
                .as_deref()
                .is_some_and(|digest| !is_sha256_digest(digest))
            {
                bail!(
                    "Shell Provider {} has an invalid installed entrypoint digest",
                    shell.provider_id
                );
            }
            match (
                variant.path.as_deref(),
                variant.sha256.as_deref(),
                variant.entrypoint_sha256.as_deref(),
                variant.size,
                variant.source_identity.as_deref(),
                variant.source_revision.as_deref(),
            ) {
                (Some(path), Some(_), Some(_), Some(size), Some(identity), Some(revision))
                    if !Path::new(path).is_absolute()
                        && !Path::new(path)
                            .components()
                            .any(|component| matches!(component, Component::ParentDir))
                        && size > 0
                        && !identity.trim().is_empty()
                        && !revision.trim().is_empty() => {}
                (None, None, None, None, None, None) => {}
                (Some(_), Some(_), Some(_), Some(_), Some(_), Some(_)) => {
                    bail!(
                        "Shell Provider {} has an unsafe installed artifact path",
                        shell.provider_id
                    )
                }
                _ => bail!(
                    "Shell Provider {} has incomplete installed artifact metadata",
                    shell.provider_id
                ),
            }
        }
    }

    if !base_ids.contains(&catalog.default_selection.base_pack_id) {
        bail!("catalog default Base Pack is unavailable");
    }
    if !shell_ids.contains(&catalog.default_selection.shell_provider_id) {
        bail!("catalog default Shell Provider is unavailable");
    }
    validate_selection(catalog, &catalog.default_selection)
}

fn validate_approval(approval: &PresentationApproval, identity: &str) -> AnyResult<()> {
    if !matches!(
        approval.state.as_str(),
        "verified" | "pending" | "blocked" | "not_required"
    ) || !matches!(
        approval.provider_trust.as_str(),
        "verified" | "pending" | "blocked" | "not_required"
    ) || !matches!(
        approval.grant_state.as_str(),
        "not_minted" | "available" | "missing" | "blocked"
    ) || !matches!(
        approval.authority_mode.as_str(),
        "lease_only" | "os_entitlement" | "none"
    ) || approval.execution_domain.trim().is_empty()
        || approval.blast_radius.trim().is_empty()
    {
        bail!("approval metadata is invalid for {identity}");
    }
    Ok(())
}

fn validate_selection(
    catalog: &PresentationCatalog,
    selection: &PresentationSelection,
) -> AnyResult<()> {
    let base_pack = catalog
        .base_packs
        .iter()
        .find(|base_pack| base_pack.pack_id == selection.base_pack_id)
        .context("selected Base Pack is unavailable")?;
    let shell = catalog
        .shell_providers
        .iter()
        .find(|shell| shell.provider_id == selection.shell_provider_id)
        .context("selected Shell Provider is unavailable")?;

    if shell.contract_id != SHELL_CONTRACT_ID {
        bail!(
            "selected Shell Provider implements {}, expected {}",
            shell.contract_id,
            SHELL_CONTRACT_ID
        );
    }
    if !base_pack
        .allowed_families
        .iter()
        .any(|family| family == &shell.presentation_family)
    {
        bail!("selected Shell Provider is not compatible with the Base Pack presentation family");
    }
    let missing = base_pack
        .required_capabilities
        .iter()
        .filter(|required| {
            !shell
                .capabilities
                .iter()
                .any(|provided| provided == *required)
        })
        .cloned()
        .collect::<Vec<_>>();
    if !missing.is_empty() {
        bail!(
            "selected Shell Provider is missing required capabilities: {}",
            missing.join(", ")
        );
    }
    if base_pack.approval.state == "blocked" || shell.approval.state == "blocked" {
        bail!("selected Base Pack or Shell Provider is blocked by approval state");
    }
    if base_pack.approval.state != "verified"
        || base_pack.approval.provider_trust != "verified"
        || shell.approval.state != "verified"
        || shell.approval.provider_trust != "verified"
    {
        bail!("selected Base Pack or Shell Provider is not verified for production use");
    }
    Ok(())
}

fn materialize_selection(
    catalog: &PresentationCatalog,
    selection: &PresentationSelection,
) -> PresentationMaterialization {
    let base_pack = catalog
        .base_packs
        .iter()
        .find(|base_pack| base_pack.pack_id == selection.base_pack_id);
    let shell = catalog
        .shell_providers
        .iter()
        .find(|shell| shell.provider_id == selection.shell_provider_id);
    let Some(base_pack) = base_pack else {
        return blocked_materialization(selection, "selected Base Pack is unavailable");
    };
    let Some(shell) = shell else {
        return blocked_materialization(selection, "selected Shell Provider is unavailable");
    };
    if let Err(error) = validate_selection(catalog, selection) {
        return blocked_materialization(selection, &error.to_string());
    }

    let contributions = shell
        .contributions
        .iter()
        .filter(|contribution| {
            contribution.family == shell.presentation_family
                && shell
                    .consumes_contracts
                    .iter()
                    .any(|contract| contract == &contribution.contract_id)
                && contribution.materialization == "selected_only"
        })
        .cloned()
        .collect::<Vec<_>>();
    let artifact = shell.artifact.clone();
    let Some(artifact) = artifact else {
        return blocked_materialization(
            selection,
            "selected Shell Provider has no platform artifact",
        );
    };
    if artifact.status != "verified" {
        return PresentationMaterialization {
            status: "blocked".to_string(),
            base_pack_id: Some(base_pack.pack_id.clone()),
            shell_provider_id: Some(shell.provider_id.clone()),
            selected_contributions: contributions,
            artifact: Some(artifact.clone()),
            reason: Some(format!(
                "Production launch requires a verified prebuilt artifact: {}",
                artifact.status_detail
            )),
        };
    }

    PresentationMaterialization {
        status: "materialized".to_string(),
        base_pack_id: Some(base_pack.pack_id.clone()),
        shell_provider_id: Some(shell.provider_id.clone()),
        selected_contributions: contributions,
        artifact: Some(artifact),
        reason: None,
    }
}

fn blocked_materialization(
    selection: &PresentationSelection,
    reason: &str,
) -> PresentationMaterialization {
    PresentationMaterialization {
        status: "blocked".to_string(),
        base_pack_id: Some(selection.base_pack_id.clone()),
        shell_provider_id: Some(selection.shell_provider_id.clone()),
        selected_contributions: Vec::new(),
        artifact: None,
        reason: Some(reason.to_string()),
    }
}

fn resolve_artifact(
    config: &AppConfig,
    shell: &ShellProviderDescriptor,
) -> AnyResult<PresentationArtifact> {
    let platform = current_platform();
    let architecture = current_architecture();
    let Some(variant) = shell
        .artifact_variants
        .iter()
        .find(|candidate| candidate.platform == platform && candidate.architecture == architecture)
    else {
        return Ok(PresentationArtifact {
            artifact_id: shell.provider_id.clone(),
            variant: format!("{platform}-{architecture}"),
            platform: platform.to_string(),
            architecture: architecture.to_string(),
            path: None,
            sha256: None,
            size: None,
            source_identity: None,
            source_revision: None,
            prebuilt: false,
            production: false,
            development_command: None,
            bundle_identifier: None,
            status: "unsupported_platform".to_string(),
            status_detail: format!(
                "No exact {}-{} artifact is declared.",
                platform, architecture
            ),
        });
    };

    let mut artifact = PresentationArtifact {
        artifact_id: variant.artifact_id.clone(),
        variant: variant.variant.clone(),
        platform: variant.platform.clone(),
        architecture: variant.architecture.clone(),
        path: variant.path.clone(),
        sha256: variant.sha256.clone(),
        size: variant.size,
        source_identity: variant.source_identity.clone(),
        source_revision: variant.source_revision.clone(),
        prebuilt: variant.prebuilt,
        production: variant.production,
        development_command: variant.development_command.clone(),
        bundle_identifier: variant.bundle_identifier.clone(),
        status: "unverified".to_string(),
        status_detail: "Artifact has not passed production verification.".to_string(),
    };

    if artifact
        .development_command
        .as_deref()
        .is_some_and(|command| !command.trim().is_empty())
    {
        artifact.status = "development_only".to_string();
        artifact.status_detail =
            "Development commands are never a production launch fallback.".to_string();
        return Ok(artifact);
    }
    if !artifact.prebuilt || !artifact.production {
        artifact.status = "development_only".to_string();
        artifact.status_detail =
            "Only completed prebuilt production artifacts may launch.".to_string();
        return Ok(artifact);
    }
    let Some(path) = artifact.path.as_deref() else {
        artifact.status = "missing".to_string();
        artifact.status_detail = "The verified production artifact is not installed.".to_string();
        return Ok(artifact);
    };
    let Some(expected_digest) = artifact.sha256.as_deref() else {
        artifact.status_detail =
            "Artifact digest is missing; verification is required.".to_string();
        return Ok(artifact);
    };
    let path = match safe_artifact_path(config, path) {
        Ok(path) => path,
        Err(error) => {
            artifact.status_detail = format!("Artifact path rejected: {error}");
            return Ok(artifact);
        }
    };
    if !path.exists() {
        artifact.status = "missing".to_string();
        artifact.status_detail = "The verified production artifact is not installed.".to_string();
        return Ok(artifact);
    }
    let (actual_digest, actual_size) = match artifact_integrity::digest_and_size(&path) {
        Ok((digest, size)) => (normalize_digest(&digest), size),
        Err(error) => {
            artifact.status_detail = format!("Artifact could not be hashed or measured: {error}");
            return Ok(artifact);
        }
    };
    if normalize_digest(expected_digest) != actual_digest {
        artifact.status = "digest_mismatch".to_string();
        artifact.status_detail = "Artifact digest does not match the pinned variant.".to_string();
        return Ok(artifact);
    }
    if artifact.size != Some(actual_size) {
        artifact.status = "size_mismatch".to_string();
        artifact.status_detail = "Artifact size does not match the signed index.".to_string();
        return Ok(artifact);
    }

    artifact.status = "verified".to_string();
    artifact.status_detail =
        "Pinned digest, prebuilt status, and production metadata verified.".to_string();
    Ok(artifact)
}

fn validate_production_artifact(artifact: &PresentationArtifact) -> AnyResult<()> {
    if let Some(command) = artifact.development_command.as_deref() {
        reject_development_command(Some(command))?;
    }
    if !artifact.prebuilt || !artifact.production {
        bail!("production launch requires a completed prebuilt artifact");
    }
    if artifact.status != "verified" {
        bail!(
            "production artifact verification status is {}",
            artifact.status
        );
    }
    Ok(())
}

pub(crate) fn reject_development_command(command: Option<&str>) -> AnyResult<()> {
    let Some(command) = command.map(str::trim).filter(|command| !command.is_empty()) else {
        return Ok(());
    };
    let normalized = command.to_ascii_lowercase();
    let development_markers = [
        "cargo tauri dev",
        "npm run dev",
        "pnpm dev",
        "yarn dev",
        "vite",
        "cargo run",
        "npm install",
        "pnpm install",
        "yarn install",
    ];
    if development_markers
        .iter()
        .any(|marker| normalized.contains(marker))
    {
        bail!("development command is not allowed in Production: {command}");
    }
    bail!("arbitrary commands are not allowed in Production launch metadata: {command}");
}

fn artifact_path(config: &AppConfig, artifact: &PresentationArtifact) -> AnyResult<PathBuf> {
    let relative = artifact
        .path
        .as_deref()
        .context("verified artifact has no path")?;
    let path = safe_artifact_path(config, relative)?;
    if !path.exists() {
        bail!(
            "verified artifact is no longer present at {}",
            path.display()
        );
    }
    let digest = sha256_path(&path)?;
    let expected = artifact
        .sha256
        .as_deref()
        .context("verified artifact has no digest")?;
    if normalize_digest(expected) != digest {
        bail!("verified artifact changed after materialization");
    }
    Ok(path)
}

fn safe_artifact_path(config: &AppConfig, relative: &str) -> AnyResult<PathBuf> {
    let relative_path = Path::new(relative);
    if relative_path.is_absolute()
        || relative_path
            .components()
            .any(|component| matches!(component, Component::ParentDir))
    {
        bail!("artifact path must be relative to the bundled application root");
    }
    let root = config.app_dir.canonicalize().with_context(|| {
        format!(
            "bundled application root is unavailable: {}",
            config.app_dir.display()
        )
    })?;
    let candidate = root.join(relative_path);
    let metadata = match fs::symlink_metadata(&candidate) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(candidate),
        Err(error) => {
            return Err(error).with_context(|| {
                format!("failed to inspect artifact path: {}", candidate.display())
            })
        }
    };
    if metadata.file_type().is_symlink() {
        bail!("symlinked artifact entry is not accepted");
    }
    let canonical = candidate
        .canonicalize()
        .with_context(|| format!("artifact path is unavailable: {}", candidate.display()))?;
    if !canonical.starts_with(&root) {
        bail!("artifact path escapes the bundled application root");
    }
    Ok(canonical)
}

fn write_selection(
    config: &AppConfig,
    catalog: &PresentationCatalog,
    selection: &PresentationSelection,
) -> AnyResult<()> {
    let base = catalog
        .base_packs
        .iter()
        .find(|item| item.pack_id == selection.base_pack_id)
        .context("selected Base disappeared before persistence")?;
    let shell = catalog
        .shell_providers
        .iter()
        .find(|item| item.provider_id == selection.shell_provider_id)
        .context("selected Shell disappeared before persistence")?;
    let artifact = shell
        .artifact
        .as_ref()
        .context("selected Shell artifact was not materialized")?;
    validate_production_artifact(artifact)?;
    let stored = StoredProfileSelection {
        schema: SELECTION_SCHEMA.to_string(),
        catalog_revision: catalog_revision(catalog)?,
        base_pack_id: base.pack_id.clone(),
        base_artifact_digest: base.artifact_digest.clone(),
        shell_provider_id: shell.provider_id.clone(),
        shell_contract_revision_digest: shell.contract_revision_digest.clone(),
        shell_artifact_id: artifact.artifact_id.clone(),
        shell_artifact_digest: artifact
            .sha256
            .clone()
            .context("verified Shell artifact has no digest")?,
        platform: artifact.platform.clone(),
        architecture: artifact.architecture.clone(),
    };
    let directory = config.user_data_dir.join(SELECTION_DIR);
    fs::create_dir_all(&directory).with_context(|| {
        format!(
            "failed to create presentation state directory {}",
            directory.display()
        )
    })?;
    let path = directory.join(SELECTION_FILE);
    let temporary = directory.join(format!(".selection-{}.tmp", std::process::id()));
    let bytes =
        serde_json::to_vec_pretty(&stored).context("failed to encode exact profile selection")?;
    let result = (|| -> AnyResult<()> {
        let mut options = OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let mut file = options
            .open(&temporary)
            .with_context(|| format!("failed to create {}", temporary.display()))?;
        file.write_all(&bytes)
            .context("failed to write presentation selection")?;
        file.sync_all()
            .context("failed to sync presentation selection")?;
        replace_file(&temporary, &path).with_context(|| {
            format!(
                "failed to commit presentation selection at {}",
                path.display()
            )
        })?;
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result
}

#[cfg(not(windows))]
fn replace_file(source: &Path, destination: &Path) -> io::Result<()> {
    fs::rename(source, destination)
}

#[cfg(windows)]
fn replace_file(source: &Path, destination: &Path) -> io::Result<()> {
    // Windows does not let std::fs::rename replace an existing file. Remove
    // only the exact launcher-owned selection file; a failed replacement still
    // leaves no partially written selection.
    if destination.exists() {
        fs::remove_file(destination)?;
    }
    fs::rename(source, destination)
}

fn read_selection(
    config: &AppConfig,
    catalog: &PresentationCatalog,
) -> AnyResult<Option<PresentationSelection>> {
    let path = config
        .user_data_dir
        .join(SELECTION_DIR)
        .join(SELECTION_FILE);
    match fs::read_to_string(&path) {
        Ok(raw) => {
            let value: serde_json::Value =
                serde_json::from_str(&raw).context("saved exact profile selection is malformed")?;
            if let Some(object) = value.as_object() {
                let legacy_keys = object.keys().map(String::as_str).collect::<BTreeSet<_>>();
                if legacy_keys == BTreeSet::from(["base_pack_id", "shell_provider_id"])
                    && object
                        .get("base_pack_id")
                        .and_then(serde_json::Value::as_str)
                        .is_some()
                    && object
                        .get("shell_provider_id")
                        .and_then(serde_json::Value::as_str)
                        .is_some()
                {
                    // The pre-v4 Launcher persisted only two mutable identifiers. They carry no
                    // catalog, artifact, platform, or digest binding and therefore grant nothing.
                    // Treat this one exact legacy shape as unselected so the user must explicitly
                    // materialize a verified v4 Shell; all other malformed state remains fatal.
                    return Ok(None);
                }
            }
            let stored: StoredProfileSelection = serde_json::from_value(value)
                .context("saved exact profile selection is malformed")?;
            if stored.schema != SELECTION_SCHEMA {
                bail!("saved selection is not a Profile v4 selection");
            }
            // A previously valid v4 selection is an exact authority binding,
            // not a durable preference. If any authenticated catalog binding
            // changes, drop to an unselected state so the user must explicitly
            // materialize a new verified Shell. This is fail-closed: stale
            // state grants nothing, while malformed or unknown-schema state
            // above remains fatal instead of being normalized away.
            if stored.catalog_revision != catalog_revision(catalog)? {
                return Ok(None);
            }
            let Some(base) = catalog
                .base_packs
                .iter()
                .find(|item| item.pack_id == stored.base_pack_id)
            else {
                return Ok(None);
            };
            if base.artifact_digest != stored.base_artifact_digest {
                return Ok(None);
            }
            let Some(shell) = catalog
                .shell_providers
                .iter()
                .find(|item| item.provider_id == stored.shell_provider_id)
            else {
                return Ok(None);
            };
            if shell.contract_revision_digest != stored.shell_contract_revision_digest {
                return Ok(None);
            }
            let Some(variant) = shell.artifact_variants.iter().find(|item| {
                item.platform == stored.platform
                    && item.architecture == stored.architecture
                    && item.artifact_id == stored.shell_artifact_id
            }) else {
                return Ok(None);
            };
            if variant.sha256.as_deref() != Some(stored.shell_artifact_digest.as_str()) {
                return Ok(None);
            }
            Ok(Some(PresentationSelection {
                base_pack_id: stored.base_pack_id,
                shell_provider_id: stored.shell_provider_id,
            }))
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error).with_context(|| format!("failed to read {}", path.display())),
    }
}

pub(crate) fn catalog_revision(catalog: &PresentationCatalog) -> AnyResult<String> {
    let mut normalized = catalog.clone();
    normalized.generated_at = 0;
    for shell in &mut normalized.shell_providers {
        shell.artifact = None;
    }
    let bytes = serde_json::to_vec(&normalized)
        .context("failed to canonicalize presentation catalog revision")?;
    Ok(format!("sha256:{}", hex::encode(Sha256::digest(bytes))))
}

fn sha256_path(path: &Path) -> AnyResult<String> {
    let (digest, _) = artifact_integrity::digest_and_size(path)?;
    Ok(normalize_digest(&digest))
}

fn normalize_digest(value: &str) -> String {
    value
        .trim()
        .strip_prefix("sha256:")
        .unwrap_or(value.trim())
        .to_ascii_lowercase()
}

fn is_sha256_digest(value: &str) -> bool {
    let normalized = value.trim().strip_prefix("sha256:").unwrap_or(value.trim());
    normalized.len() == 64
        && normalized
            .chars()
            .all(|character| character.is_ascii_hexdigit())
}

fn current_platform() -> &'static str {
    if cfg!(target_os = "macos") {
        "macos"
    } else if cfg!(target_os = "windows") {
        "windows"
    } else if cfg!(target_os = "linux") {
        "linux"
    } else {
        "unsupported"
    }
}

fn current_architecture() -> &'static str {
    if cfg!(target_arch = "aarch64") {
        "arm64"
    } else if cfg!(target_arch = "x86_64") {
        "x86_64"
    } else {
        "unsupported"
    }
}

fn now_seconds() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;

    fn presentation_catalog_capability() -> serde_json::Value {
        serde_json::from_str(include_str!("../capabilities/presentation-catalog.json")).unwrap()
    }

    fn presentation_control_capability() -> serde_json::Value {
        serde_json::from_str(include_str!("../capabilities/presentation-control.json")).unwrap()
    }

    fn capability_allows_origin(
        capability: &serde_json::Value,
        window_label: &str,
        origin: &str,
    ) -> bool {
        let window_allowed = capability["windows"]
            .as_array()
            .unwrap()
            .iter()
            .any(|label| label.as_str() == Some(window_label));
        let origin = Url::parse(origin).unwrap();
        let origin_allowed = capability["remote"]["urls"]
            .as_array()
            .unwrap()
            .iter()
            .map(|pattern| {
                pattern
                    .as_str()
                    .unwrap()
                    .parse::<tauri::utils::acl::RemoteUrlPattern>()
                    .unwrap()
            })
            .any(|pattern| pattern.test(&origin));
        window_allowed && origin_allowed
    }

    fn capability_permissions(value: &serde_json::Value) -> Vec<&str> {
        value["permissions"]
            .as_array()
            .unwrap()
            .iter()
            .map(|permission| permission.as_str().unwrap())
            .collect()
    }

    #[test]
    fn presentation_capabilities_are_split_from_the_default_surface() {
        let default: serde_json::Value =
            serde_json::from_str(include_str!("../capabilities/default.json")).unwrap();
        assert_eq!(capability_permissions(&default), vec!["core:default"]);

        let catalog = presentation_catalog_capability();
        assert_eq!(catalog["local"], false);
        assert_eq!(catalog["windows"], serde_json::json!(["main"]));
        assert_eq!(
            capability_permissions(&catalog),
            vec!["allow-get-presentation-catalog"]
        );

        let control = presentation_control_capability();
        assert_eq!(control["local"], false);
        assert_eq!(control["windows"], serde_json::json!(["main"]));
        assert_eq!(
            capability_permissions(&control),
            vec![
                "allow-select-presentation",
                "allow-launch-selected-presentation"
            ]
        );

        assert_eq!(
            catalog["remote"]["urls"],
            serde_json::json!(["http://127.0.0.1:*/*", "http://localhost:*/*"])
        );
        assert_eq!(
            control["remote"]["urls"],
            serde_json::json!(["http://127.0.0.1:*/*", "http://localhost:*/*"])
        );
    }

    #[test]
    fn presentation_acl_allows_first_start_and_restart_origins() {
        // Tauri authorizes remote IPC with the browser's origin-only Origin
        // header. The path is deliberately enforced below by the live-Webview
        // caller check, not represented as a misleading ACL URL pattern.
        let catalog = presentation_catalog_capability();
        let control = presentation_control_capability();
        for origin in [
            "http://127.0.0.1:8765/",
            "http://localhost:8765/",
            "http://127.0.0.1:8767/",
            "http://127.0.0.1:8767/panel/?code=fallback",
            "http://localhost:18772/",
        ] {
            assert!(capability_allows_origin(&catalog, "main", origin));
            assert!(capability_allows_origin(&control, "main", origin));
        }

        for live_url in [
            "http://127.0.0.1:8765/panel/?code=first-start",
            "http://127.0.0.1:8765/panel/setup",
            "http://localhost:8765/panel/setup?code=restart",
        ] {
            validate_presentation_caller_context(
                "main",
                &Url::parse(live_url).unwrap(),
                LAUNCHER_PANEL_PORT,
            )
            .unwrap();
        }

        for live_url in [
            "http://127.0.0.1:8767/panel/?code=fallback",
            "http://localhost:18772/panel/setup?code=restart",
        ] {
            let url = Url::parse(live_url).unwrap();
            validate_presentation_caller_context(
                "main",
                &url,
                url.port_or_known_default().unwrap(),
            )
            .unwrap();
        }
    }

    #[test]
    fn presentation_acl_rejects_wrong_labels_and_untrusted_origins() {
        for label in [
            "panel",
            "defaultspack-main",
            "authority-approval",
            "defaults-console",
            "host-permissions",
        ] {
            assert!(!capability_allows_origin(
                &presentation_catalog_capability(),
                label,
                "http://127.0.0.1:8765/"
            ));
        }

        for origin in [
            "tauri://localhost/",
            "https://127.0.0.1:8765/",
            "http://example.invalid:8765/",
        ] {
            assert!(!capability_allows_origin(
                &presentation_catalog_capability(),
                "main",
                origin
            ));
            assert!(!capability_allows_origin(
                &presentation_control_capability(),
                "main",
                origin
            ));
        }

        for origin in ["http://127.0.0.1:8764/", "http://localhost:8766/"] {
            assert!(capability_allows_origin(
                &presentation_catalog_capability(),
                "main",
                origin
            ));
            assert!(capability_allows_origin(
                &presentation_control_capability(),
                "main",
                origin
            ));
        }
    }

    #[test]
    fn forged_allowed_origin_cannot_bypass_live_webview_route_check() {
        assert!(capability_allows_origin(
            &presentation_catalog_capability(),
            "main",
            "http://127.0.0.1:8765/"
        ));

        for live_url in [
            "http://127.0.0.1:8765/console",
            "http://127.0.0.1:8765/approval",
            "http://127.0.0.1:8765/panel-legacy",
            "http://example.invalid:8765/panel/setup",
        ] {
            assert!(validate_presentation_caller_context(
                "main",
                &Url::parse(live_url).unwrap(),
                LAUNCHER_PANEL_PORT,
            )
            .is_err());
        }
    }

    #[test]
    fn presentation_caller_accepts_only_the_launcher_panel() {
        for url in [
            "http://127.0.0.1:8765/panel",
            "http://127.0.0.1:8765/panel/",
            "http://localhost:8765/panel/setup?code=secret#section",
            "http://127.0.0.1:8765/panel/packs/example",
        ] {
            validate_presentation_caller_context(
                "main",
                &Url::parse(url).unwrap(),
                LAUNCHER_PANEL_PORT,
            )
            .unwrap();
        }
    }

    #[test]
    fn presentation_caller_rejects_non_launcher_windows_origins_and_navigation() {
        for label in [
            "defaultspack-main",
            "authority-approval",
            "defaults-console",
            "host-permissions",
        ] {
            assert_eq!(
                validate_presentation_caller_context(
                    label,
                    &Url::parse("http://127.0.0.1:8765/panel/").unwrap(),
                    LAUNCHER_PANEL_PORT,
                ),
                Err(PresentationCallerDenial::WindowLabel)
            );
        }

        for (url, denial) in [
            ("tauri://localhost/panel/", PresentationCallerDenial::Scheme),
            (
                "https://127.0.0.1:8765/panel/",
                PresentationCallerDenial::Scheme,
            ),
            (
                "http://example.invalid:8765/panel/",
                PresentationCallerDenial::Host,
            ),
            (
                "http://localhost:8764/panel/",
                PresentationCallerDenial::Port,
            ),
            (
                "http://127.0.0.1:8766/panel/",
                PresentationCallerDenial::Port,
            ),
            (
                "http://127.0.0.1:8765/console",
                PresentationCallerDenial::Route,
            ),
            (
                "http://127.0.0.1:8765/approval",
                PresentationCallerDenial::Route,
            ),
            (
                "http://127.0.0.1:8765/panel-legacy",
                PresentationCallerDenial::Route,
            ),
        ] {
            assert_eq!(
                validate_presentation_caller_context(
                    "main",
                    &Url::parse(url).unwrap(),
                    LAUNCHER_PANEL_PORT,
                ),
                Err(denial),
                "unexpected decision for {url}"
            );
        }

        assert_eq!(
            validate_presentation_caller_context(
                "main",
                &Url::parse("http://127.0.0.1:18772/panel/").unwrap(),
                0,
            ),
            Err(PresentationCallerDenial::ConfiguredPort)
        );

        validate_presentation_caller_context(
            "main",
            &Url::parse("http://127.0.0.1:18772/panel/").unwrap(),
            18772,
        )
        .unwrap();
    }

    fn sample_catalog() -> PresentationCatalog {
        let selection = PresentationSelection {
            base_pack_id: "defaults-basepack".into(),
            shell_provider_id: "shell.tauri.default".into(),
        };
        PresentationCatalog {
            schema: CATALOG_SCHEMA.to_string(),
            generator: "test".into(),
            generator_version: "1.0.0".into(),
            default_profile_id: "defaults-modern".into(),
            default_profile_source: "test.profile.yaml".into(),
            default_profile_digest: "sha256:".to_string() + &"0".repeat(64),
            default_selection: selection.clone(),
            contract_revisions: vec![ContractRevisionDescriptor {
                contract_id: SHELL_CONTRACT_ID.into(),
                revision: "1.0.0".into(),
                digest: "sha256:".to_string() + &"1".repeat(64),
                source_path: "test.schema.json".into(),
            }],
            source_manifest_digests: BTreeMap::from([(
                "defaults-basepack".into(),
                "sha256:".to_string() + &"2".repeat(64),
            )]),
            base_packs: vec![BasePackDescriptor {
                pack_id: "defaults-basepack".into(),
                display_name: "Defaults Base Pack".into(),
                version: "4.0.0".into(),
                artifact_digest: "sha256:".to_string() + &"3".repeat(64),
                backend_provider_ids: vec!["defaultspack".into()],
                state_owners: vec!["defaultspack.state".into()],
                backend_identity_digest: "sha256:".to_string() + &"4".repeat(64),
                required_capabilities: vec!["navigation".into(), "commands".into()],
                allowed_families: vec!["graphical".into(), "terminal".into()],
                approval: sample_approval("none"),
            }],
            shell_providers: vec![ShellProviderDescriptor {
                provider_id: "shell.tauri.default".into(),
                display_name: "Tauri Desktop".into(),
                contract_id: SHELL_CONTRACT_ID.into(),
                contract_revision_digest: "sha256:".to_string() + &"7".repeat(64),
                experience_role: "shell".into(),
                presentation_kind: "packaged_process".into(),
                presentation_family: "graphical".into(),
                technology: "tauri".into(),
                capabilities: vec!["navigation".into(), "commands".into()],
                consumes_contracts: vec!["ui.route.contribution.v1".into()],
                contributions: vec![PresentationContribution {
                    contribution_id: "ui.route.contribution.v1".into(),
                    owner_pack_id: "defaultspack".into(),
                    contract_id: "ui.route.contribution.v1".into(),
                    contract_revision_digest: "sha256:".to_string() + &"5".repeat(64),
                    family: "graphical".into(),
                    label: "Graphical routes".into(),
                    artifact_ref: "contribution.json".into(),
                    digest: "sha256:".to_string() + &"6".repeat(64),
                    presentation_kind: "declarative".into(),
                    technology: "web".into(),
                    host_authority: "none".into(),
                    materialization: "selected_only".into(),
                }],
                artifact_variants: Vec::new(),
                artifact: None,
                approval: sample_approval("lease_only"),
                protocol_revision_digest: None,
            }],
            generated_at: 0,
            release_binding: None,
        }
    }

    fn sample_approval(authority_mode: &str) -> PresentationApproval {
        PresentationApproval {
            state: "verified".into(),
            provider_trust: "verified".into(),
            grant_state: "not_minted".into(),
            authority_mode: authority_mode.into(),
            execution_domain: "test-domain".into(),
            effect_scope: Vec::new(),
            blast_radius: "No ambient Host authority.".into(),
            reason: None,
        }
    }

    fn test_config(root: &Path) -> AppConfig {
        AppConfig {
            app_dir: root.to_path_buf(),
            rumi_home: root.to_path_buf(),
            python_dir: root.join("python"),
            uv_path: root.join("uv"),
            venv_dir: root.join("venv"),
            user_data_dir: root.join("user_data"),
            log_dir: root.join("logs"),
            kernel_port: 8765,
            dev_workspace_root: None,
        }
    }

    fn relocated_release_config(test_name: &str) -> (PathBuf, AppConfig) {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!(
            "tobkiri-relocated-release-{test_name}-{}-{unique}",
            std::process::id()
        ));
        let app_dir = root
            .join("Relocated")
            .join("Tobkiri Launcher.app")
            .join("Contents")
            .join("Resources")
            .join("app");
        fs::create_dir_all(app_dir.join("bundled")).unwrap();
        fs::write(
            app_dir.join("bundled").join("presentation_catalog.json"),
            include_str!("../bundled/presentation_catalog.json"),
        )
        .unwrap();

        let mut config = test_config(&app_dir);
        config.user_data_dir = root.join("Application Support").join("user_data");
        config.log_dir = root.join("Application Support").join("logs");
        (root, config)
    }

    #[test]
    fn production_rejects_known_development_commands() {
        for command in ["cargo tauri dev", "npm run dev", "pnpm dev"] {
            let error = reject_development_command(Some(command)).unwrap_err();
            assert!(error.to_string().contains("not allowed in Production"));
        }
    }

    #[test]
    fn production_rejects_all_arbitrary_commands_even_if_not_known_dev_command() {
        let error = reject_development_command(Some("./launcher --profile default")).unwrap_err();
        assert!(error.to_string().contains("arbitrary commands"));
        assert!(reject_development_command(None).is_ok());
    }

    #[test]
    fn selection_requires_exact_shell_contract_and_capabilities() {
        let mut catalog = sample_catalog();
        let selection = PresentationSelection {
            base_pack_id: "defaults-basepack".into(),
            shell_provider_id: "shell.tauri.default".into(),
        };
        assert!(validate_selection(&catalog, &selection).is_ok());

        catalog.shell_providers[0].contract_id = "wrong.contract.v1".into();
        let error = validate_selection(&catalog, &selection).unwrap_err();
        assert!(error.to_string().contains("expected app.shell.v1"));

        catalog.shell_providers[0].contract_id = SHELL_CONTRACT_ID.into();
        catalog.base_packs[0]
            .required_capabilities
            .push("windows".into());
        let error = validate_selection(&catalog, &selection).unwrap_err();
        assert!(error.to_string().contains("missing required capabilities"));

        catalog.base_packs[0].required_capabilities.pop();
        catalog.shell_providers[0].approval.state = "pending".into();
        let error = validate_selection(&catalog, &selection).unwrap_err();
        assert!(error
            .to_string()
            .contains("not verified for production use"));
    }

    #[test]
    fn materialization_filters_contributions_to_selected_presentation_family() {
        let mut catalog = sample_catalog();
        catalog.shell_providers[0]
            .contributions
            .push(PresentationContribution {
                contribution_id: "cli.command.contribution.v1".into(),
                owner_pack_id: "defaultspack".into(),
                contract_id: "cli.command.contribution.v1".into(),
                contract_revision_digest: "sha256:".to_string() + &"8".repeat(64),
                family: "terminal".into(),
                label: "CLI commands".into(),
                artifact_ref: "contribution.json".into(),
                digest: "sha256:".to_string() + &"9".repeat(64),
                presentation_kind: "terminal_stdio".into(),
                technology: "cli".into(),
                host_authority: "structured_protocol_only".into(),
                materialization: "selected_only".into(),
            });
        catalog.shell_providers[0].artifact = Some(PresentationArtifact {
            artifact_id: "shell-tauri-test".into(),
            variant: "test".into(),
            platform: current_platform().into(),
            architecture: current_architecture().into(),
            path: None,
            sha256: None,
            size: None,
            source_identity: None,
            source_revision: None,
            prebuilt: true,
            production: true,
            development_command: None,
            bundle_identifier: None,
            status: "verified".into(),
            status_detail: "test".into(),
        });
        let selection = PresentationSelection {
            base_pack_id: "defaults-basepack".into(),
            shell_provider_id: "shell.tauri.default".into(),
        };
        let materialization = materialize_selection(&catalog, &selection);
        assert_eq!(materialization.status, "materialized");
        assert_eq!(materialization.selected_contributions.len(), 1);
        assert_eq!(
            materialization.selected_contributions[0].family,
            "graphical"
        );
    }

    #[test]
    fn bundled_catalog_is_manifest_projection_and_uninstalled_variants_block_launch() {
        let catalog: PresentationCatalog =
            serde_json::from_str(include_str!("../bundled/presentation_catalog.json")).unwrap();
        validate_catalog_integrity(&catalog).unwrap();
        assert_eq!(catalog.default_profile_id, "defaults");
        assert_eq!(
            catalog.default_selection.shell_provider_id,
            "shell.tauri.default"
        );
        assert_eq!(catalog.shell_providers.len(), 1);
        assert!(catalog.shell_providers.iter().all(|shell| {
            shell
                .artifact_variants
                .iter()
                .all(|variant| variant.path.is_none() && variant.sha256.is_none())
        }));

        let root = std::env::temp_dir().join(format!(
            "tobkiri-presentation-catalog-test-{}",
            std::process::id()
        ));
        fs::remove_dir_all(&root).ok();
        fs::create_dir_all(&root).unwrap();
        let state = build_state_from_catalog(
            &test_config(&root),
            catalog.clone(),
            Some(catalog.default_selection.clone()),
        )
        .unwrap();
        assert_eq!(state.materialization.status, "blocked");
        assert_eq!(
            state.materialization.artifact.as_ref().unwrap().status,
            "missing"
        );
        fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn relocated_release_resources_load_base_and_compatible_shells() {
        let (root, config) = relocated_release_config("catalog");
        let state = build_state(&config).unwrap();
        let base = state.catalog.base_packs.first().unwrap();
        let profile_identity = (
            state.catalog.default_profile_id.clone(),
            state.catalog.default_profile_digest.clone(),
            base.backend_identity_digest.clone(),
            base.backend_provider_ids.clone(),
            base.state_owners.clone(),
        );

        assert_eq!(state.catalog.base_packs.len(), 1);
        assert_eq!(state.catalog.shell_providers.len(), 1);
        assert_eq!(state.selection, None);
        assert_eq!(state.materialization.status, "not_selected");

        let compatible_shells = state
            .catalog
            .shell_providers
            .iter()
            .map(|shell| PresentationSelection {
                base_pack_id: base.pack_id.clone(),
                shell_provider_id: shell.provider_id.clone(),
            })
            .filter(|selection| validate_selection(&state.catalog, selection).is_ok())
            .collect::<Vec<_>>();
        assert_eq!(compatible_shells.len(), 1);

        for selection in compatible_shells {
            let selected_state =
                build_state_from_catalog(&config, state.catalog.clone(), Some(selection.clone()))
                    .unwrap();
            let selected_base = selected_state.catalog.base_packs.first().unwrap();
            assert_eq!(
                (
                    selected_state.catalog.default_profile_id.clone(),
                    selected_state.catalog.default_profile_digest.clone(),
                    selected_base.backend_identity_digest.clone(),
                    selected_base.backend_provider_ids.clone(),
                    selected_base.state_owners.clone(),
                ),
                profile_identity
            );
            assert_eq!(selected_state.materialization.status, "blocked");
            let artifact = selected_state.materialization.artifact.as_ref().unwrap();
            assert_eq!(artifact.status, "missing");
            assert!(selected_state
                .materialization
                .reason
                .as_deref()
                .unwrap()
                .contains("not installed"));
        }

        let arbitrary_dev_path = root
            .join("Relocated")
            .join("Tobkiri Launcher.app")
            .join("Contents")
            .join("MacOS")
            .join("Tobkiri Launcher")
            .to_string_lossy()
            .into_owned();
        let error = reject_development_command(Some(&arbitrary_dev_path)).unwrap_err();
        assert!(error.to_string().contains("arbitrary commands"));
        fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn relocated_release_catalog_errors_name_the_packaged_resource() {
        let (root, config) = relocated_release_config("error");
        let catalog_path = presentation_catalog_path(&config);
        fs::write(&catalog_path, "{\"schema\": \"broken\"}").unwrap();
        let error = format!("{:#}", load_catalog(&config).unwrap_err());
        assert!(error.contains("packaged resource"));
        assert!(error.contains("is malformed and was rejected"));
        let catalog_path_text = catalog_path.to_string_lossy();
        assert!(error.contains(catalog_path_text.as_ref()));
        fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn legacy_unbound_selection_requires_new_v4_materialization() {
        let (root, mut config) = relocated_release_config("legacy-selection");
        config.user_data_dir = root.join("Application Support").join("user_data");
        let selection_path = config
            .user_data_dir
            .join(SELECTION_DIR)
            .join(SELECTION_FILE);
        fs::create_dir_all(selection_path.parent().unwrap()).unwrap();
        fs::write(
            &selection_path,
            br#"{"base_pack_id":"defaults-basepack","shell_provider_id":"shell.tauri.default"}"#,
        )
        .unwrap();
        let catalog = load_catalog(&config).unwrap();
        assert_eq!(read_selection(&config, &catalog).unwrap(), None);

        fs::write(
            &selection_path,
            br#"{"base_pack_id":"defaults-basepack","shell_provider_id":"shell.tauri.default","unexpected":true}"#,
        )
        .unwrap();
        assert!(read_selection(&config, &catalog).is_err());
        fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn stale_exact_v4_selection_drops_to_unselected_without_reusing_authority() {
        let (root, mut config) = relocated_release_config("stale-v4-selection");
        config.user_data_dir = root.join("Application Support").join("user_data");
        let selection_path = config
            .user_data_dir
            .join(SELECTION_DIR)
            .join(SELECTION_FILE);
        fs::create_dir_all(selection_path.parent().unwrap()).unwrap();

        let mut catalog = load_catalog(&config).unwrap();
        let base = catalog.base_packs.first().unwrap().clone();
        let bound_digest = "sha256:".to_string() + &"c".repeat(64);
        let (
            shell_provider_id,
            shell_contract_revision_digest,
            shell_artifact_id,
            platform,
            architecture,
        ) = {
            let shell = catalog.shell_providers.first_mut().unwrap();
            let variant = shell
                .artifact_variants
                .iter_mut()
                .find(|variant| {
                    variant.platform == current_platform()
                        && variant.architecture == current_architecture()
                })
                .unwrap();
            variant.sha256 = Some(bound_digest.clone());
            (
                shell.provider_id.clone(),
                shell.contract_revision_digest.clone(),
                variant.artifact_id.clone(),
                variant.platform.clone(),
                variant.architecture.clone(),
            )
        };
        let stored = StoredProfileSelection {
            schema: SELECTION_SCHEMA.to_string(),
            catalog_revision: catalog_revision(&catalog).unwrap(),
            base_pack_id: base.pack_id.clone(),
            base_artifact_digest: base.artifact_digest.clone(),
            shell_provider_id,
            shell_contract_revision_digest,
            shell_artifact_id,
            shell_artifact_digest: bound_digest,
            platform,
            architecture,
        };
        fs::write(&selection_path, serde_json::to_vec_pretty(&stored).unwrap()).unwrap();
        assert_eq!(
            read_selection(&config, &catalog).unwrap(),
            Some(PresentationSelection {
                base_pack_id: stored.base_pack_id.clone(),
                shell_provider_id: stored.shell_provider_id.clone(),
            })
        );

        // Any authenticated catalog revision change invalidates the stored
        // authority binding, but must not trap the user in a retry loop. It
        // becomes unselected and therefore cannot launch until re-materialized.
        catalog.generator_version.push_str("-new");
        assert_eq!(read_selection(&config, &catalog).unwrap(), None);
        assert!(selection_path.is_file());

        // The same fail-closed recovery applies when an exact Base/Shell
        // identity disappears from a newer verified catalog.
        let mut missing_base = catalog.clone();
        missing_base.base_packs.clear();
        assert_eq!(read_selection(&config, &missing_base).unwrap(), None);
        fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn verified_installed_variant_materializes_only_selected_contributions() {
        let root = std::env::temp_dir().join(format!(
            "tobkiri-presentation-installed-test-{}",
            std::process::id()
        ));
        fs::remove_dir_all(&root).ok();
        fs::create_dir_all(&root).unwrap();
        let installed = root.join("installed.bin");
        let contents = b"verified production artifact";
        fs::write(&installed, contents).unwrap();
        let mut digest_input = vec![0_u8];
        digest_input.extend_from_slice(contents);
        let digest = format!("sha256:{}", hex::encode(Sha256::digest(digest_input)));

        let mut catalog = sample_catalog();
        catalog.shell_providers[0].artifact_variants = vec![ArtifactVariant {
            artifact_id: "shell.tauri.default.macos-arm64".into(),
            variant: format!("{}-{}", current_platform(), current_architecture()),
            platform: current_platform().into(),
            architecture: current_architecture().into(),
            artifact_ref: "descriptor.json".into(),
            entrypoint: "tobkiri-shell".into(),
            artifact_kind: "signed_prebuilt_binary".into(),
            descriptor_digest: "sha256:".to_string() + &"a".repeat(64),
            path: Some("installed.bin".into()),
            sha256: Some(digest),
            entrypoint_sha256: Some(byte_digest(contents)),
            size: Some(contents.len() as u64),
            source_identity: Some("test:fixture".into()),
            source_revision: Some("test-revision".into()),
            prebuilt: true,
            production: true,
            development_command: None,
            bundle_identifier: None,
        }];

        let config = test_config(&root);
        let selection = catalog.default_selection.clone();
        let state = build_state_from_catalog(&config, catalog, Some(selection)).unwrap();
        assert_eq!(state.materialization.status, "materialized");
        assert_eq!(
            state.materialization.artifact.as_ref().unwrap().status,
            "verified"
        );
        assert_eq!(state.materialization.selected_contributions.len(), 1);
        assert_eq!(
            state.materialization.selected_contributions[0].family,
            "graphical"
        );
        fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn packaged_artifact_selection_persists_and_tampering_blocks_after_restart() {
        let root = std::env::temp_dir().join(format!(
            "tobkiri-presentation-persistence-test-{}",
            std::process::id()
        ));
        fs::remove_dir_all(&root).ok();
        let app_dir = root
            .join("Relocated")
            .join("Tobkiri Launcher.app")
            .join("Contents")
            .join("Resources")
            .join("app");
        fs::create_dir_all(app_dir.join("bundled").join("presentation-artifacts")).unwrap();
        let artifact_path = app_dir
            .join("bundled")
            .join("presentation-artifacts")
            .join(format!(
                "shell.tauri.default.{}-{}",
                current_platform(),
                current_architecture()
            ))
            .join("Tobkiri.app");
        fs::create_dir_all(&artifact_path).unwrap();
        fs::write(artifact_path.join("Contents"), b"verified shell artifact").unwrap();
        let contents = b"verified shell artifact";
        let mut digest_input = b"Contents".to_vec();
        digest_input.push(0);
        digest_input.extend_from_slice(contents);
        let digest = format!("sha256:{}", hex::encode(Sha256::digest(digest_input)));

        let mut catalog: PresentationCatalog =
            serde_json::from_str(include_str!("../bundled/presentation_catalog.json")).unwrap();
        let variant = catalog
            .shell_providers
            .iter_mut()
            .find(|shell| shell.provider_id == "shell.tauri.default")
            .unwrap()
            .artifact_variants
            .iter_mut()
            .find(|variant| {
                variant.platform == current_platform()
                    && variant.architecture == current_architecture()
            })
            .unwrap();
        variant.path = Some(
            artifact_path
                .strip_prefix(&app_dir)
                .unwrap()
                .to_string_lossy()
                .into_owned(),
        );
        variant.sha256 = Some(digest);
        variant.entrypoint_sha256 = Some(byte_digest(contents));
        variant.size = Some(contents.len() as u64);
        variant.source_identity = Some("test:persistence".into());
        variant.source_revision = Some("test-revision".into());

        let mut config = test_config(&app_dir);
        config.user_data_dir = root.join("Application Support").join("user_data");
        let selection = catalog.default_selection.clone();
        let selected =
            build_state_from_catalog(&config, catalog.clone(), Some(selection.clone())).unwrap();
        write_selection(&config, &selected.catalog, &selection).unwrap();
        assert_eq!(selected.materialization.status, "materialized");
        assert_eq!(
            selected.materialization.artifact.as_ref().unwrap().status,
            "verified"
        );

        let restarted_selection = read_selection(&config, &catalog).unwrap();
        let restarted =
            build_state_from_catalog(&config, catalog.clone(), restarted_selection).unwrap();
        assert_eq!(restarted.selection, Some(catalog.default_selection.clone()));
        assert_eq!(restarted.materialization.status, "materialized");

        fs::write(artifact_path.join("Contents"), b"tampered shell artifact").unwrap();
        let tampered_selection = read_selection(&config, &catalog).unwrap();
        let tampered = build_state_from_catalog(&config, catalog, tampered_selection).unwrap();
        assert_eq!(tampered.materialization.status, "blocked");
        assert_eq!(
            tampered.materialization.artifact.as_ref().unwrap().status,
            "digest_mismatch"
        );
        fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn directory_digest_is_deterministic_and_rejects_symlinks() {
        let root =
            std::env::temp_dir().join(format!("tobkiri-presentation-test-{}", std::process::id()));
        fs::remove_dir_all(&root).ok();
        fs::create_dir_all(root.join("bundle")).unwrap();
        fs::write(root.join("bundle").join("b.txt"), "b").unwrap();
        fs::write(root.join("bundle").join("a.txt"), "a").unwrap();
        let first = sha256_path(&root.join("bundle")).unwrap();
        let second = sha256_path(&root.join("bundle")).unwrap();
        assert_eq!(first, second);
        #[cfg(unix)]
        std::os::unix::fs::symlink(root.join("a.txt"), root.join("bundle").join("link")).unwrap();
        #[cfg(unix)]
        assert!(sha256_path(&root.join("bundle")).is_err());
        fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn normalize_digest_accepts_sha256_prefix() {
        let mut values = BTreeMap::new();
        values.insert(normalize_digest("sha256:ABC"), true);
        values.insert(normalize_digest(" abc "), true);
        assert_eq!(values.len(), 1);
    }

    #[test]
    fn signed_release_rejects_wrong_platform_and_architecture() {
        assert!(validate_release_target(current_platform(), current_architecture()).is_ok());
        let error = validate_release_target("wrong-platform", current_architecture()).unwrap_err();
        assert!(error.to_string().contains("wrong platform"));
        let error = validate_release_target(current_platform(), "wrong-architecture").unwrap_err();
        assert!(error.to_string().contains("wrong platform"));
    }

    #[test]
    fn verified_launch_spec_passes_only_the_one_shot_handoff_path() {
        let artifact = if cfg!(windows) {
            Path::new(r"C:\verified\release\tobkiri-shell.exe")
        } else {
            Path::new("/verified/release/Tobkiri Shell.app")
        };
        let handoff = if cfg!(windows) {
            Path::new(r"C:\private\launcher\shell_handoff\handoff-ABC.json")
        } else {
            Path::new("/private/launcher/shell_handoff/handoff-ABC.json")
        };
        let macos = verified_launch_spec("macos", artifact, handoff).unwrap();
        assert_eq!(macos.program, Path::new("/usr/bin/open"));
        assert_eq!(
            macos.args,
            vec![
                OsString::from("-n"),
                artifact.as_os_str().to_owned(),
                OsString::from("--args"),
                OsString::from(crate::shell_handoff::HANDOFF_ARGUMENT),
                handoff.as_os_str().to_owned(),
            ]
        );

        for platform in ["linux", "windows"] {
            let direct = verified_launch_spec(platform, artifact, handoff).unwrap();
            assert_eq!(direct.program, artifact);
            assert_eq!(
                direct.args,
                vec![
                    OsString::from(crate::shell_handoff::HANDOFF_ARGUMENT),
                    handoff.as_os_str().to_owned(),
                ]
            );
        }
        assert!(macos
            .args
            .iter()
            .all(|arg| !arg.to_string_lossy().contains("rumi_local_auth")));
    }

    #[derive(Default)]
    struct FakeLaunchProcess {
        observations: Vec<Option<bool>>,
        terminated: bool,
    }

    impl LaunchProcess for FakeLaunchProcess {
        fn try_wait_success(&mut self) -> io::Result<Option<bool>> {
            Ok(if self.observations.is_empty() {
                None
            } else {
                self.observations.remove(0)
            })
        }

        fn terminate(&mut self) -> io::Result<()> {
            self.terminated = true;
            Ok(())
        }
    }

    #[test]
    fn injected_macos_launch_failure_is_not_reported_as_success() {
        let mut process = FakeLaunchProcess {
            observations: vec![Some(false)],
            ..Default::default()
        };
        let error = wait_for_launch_success_with(
            &mut process,
            Duration::from_secs(1),
            || Duration::ZERO,
            |_| {},
        )
        .unwrap_err();

        assert!(error.to_string().contains("exited unsuccessfully"));
        assert!(!process.terminated);
    }

    #[test]
    fn injected_macos_launch_timeout_terminates_the_child() {
        let mut process = FakeLaunchProcess {
            observations: vec![None],
            ..Default::default()
        };
        let error = wait_for_launch_success_with(
            &mut process,
            Duration::from_secs(1),
            || Duration::from_secs(1),
            |_| {},
        )
        .unwrap_err();

        assert!(error.to_string().contains("timed out"));
        assert!(process.terminated);
    }

    #[test]
    fn injected_macos_handoff_without_ack_fails_closed() {
        let error = wait_for_handoff_consumed_with(
            Path::new("handoff-test.json"),
            Duration::from_secs(1),
            |_| Ok(false),
            || Duration::from_secs(1),
            |_| {},
        )
        .unwrap_err();

        assert!(error.to_string().contains("consume handoff"));
    }

    #[test]
    fn verified_launch_spec_rejects_relative_and_unsupported_targets() {
        let absolute_shell = if cfg!(windows) {
            Path::new(r"C:\verified\tobkiri-shell.exe")
        } else {
            Path::new("/verified/shell")
        };
        let absolute_handoff = if cfg!(windows) {
            Path::new(r"C:\private\launcher\handoff.json")
        } else {
            Path::new("/private/launcher/handoff.json")
        };
        let relative =
            verified_launch_spec("linux", Path::new("Tobkiri.AppImage"), absolute_handoff)
                .unwrap_err()
                .to_string();
        assert!(relative.contains("artifact launch path must be absolute"));

        let relative_handoff =
            verified_launch_spec("linux", absolute_shell, Path::new("handoff.json"))
                .unwrap_err()
                .to_string();
        assert!(relative_handoff.contains("handoff path must be absolute"));

        let unsupported = verified_launch_spec("fixture-os", absolute_shell, absolute_handoff)
            .unwrap_err()
            .to_string();
        assert!(unsupported.contains("unsupported"));
    }

    #[test]
    fn catalog_rejects_duplicate_shell_artifact_identity() {
        let mut catalog: PresentationCatalog =
            serde_json::from_str(include_str!("../bundled/presentation_catalog.json")).unwrap();
        let mut duplicate = catalog.shell_providers[0].artifact_variants[0].clone();
        duplicate.platform = "fixture-platform".into();
        duplicate.architecture = "fixture-architecture".into();
        catalog.shell_providers[0].artifact_variants.push(duplicate);
        let error = validate_catalog_integrity(&catalog).unwrap_err();
        assert!(error.to_string().contains("invalid production variant"));
    }

    #[test]
    fn installed_catalog_without_signed_release_binding_fails_closed() {
        let (root, config) = relocated_release_config("unsigned-installed");
        let mut catalog: PresentationCatalog =
            serde_json::from_str(include_str!("../bundled/presentation_catalog.json")).unwrap();
        let variant = catalog
            .shell_providers
            .iter_mut()
            .flat_map(|shell| &mut shell.artifact_variants)
            .find(|variant| {
                variant.platform == current_platform()
                    && variant.architecture == current_architecture()
            })
            .unwrap();
        variant.path = Some("bundled/presentation-artifacts/unsigned".into());
        variant.sha256 = Some("sha256:".to_string() + &"a".repeat(64));
        variant.entrypoint_sha256 = Some("sha256:".to_string() + &"b".repeat(64));
        variant.size = Some(1);
        variant.source_identity = Some("test:unsigned".into());
        variant.source_revision = Some("test-revision".into());
        fs::write(
            presentation_catalog_path(&config),
            serde_json::to_vec_pretty(&catalog).unwrap(),
        )
        .unwrap();
        let error = format!("{:#}", load_catalog(&config).unwrap_err());
        assert!(
            error.contains("installed Shell metadata requires a signed release binding"),
            "{error}"
        );
        fs::remove_dir_all(root).unwrap();
    }
}
