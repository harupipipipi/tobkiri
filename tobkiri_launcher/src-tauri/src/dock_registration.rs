//! Defaultspack launch coordination and legacy Dock command handling.

use std::ffi::OsString;
use std::fs;
#[cfg(unix)]
use std::os::unix::fs::PermissionsExt;
#[cfg(unix)]
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::{Arc, Mutex, OnceLock};
use std::thread;
use std::time::{Duration, Instant};

use anyhow::{anyhow, bail, Context, Result as AnyResult};
use log::{error, info, warn};
use serde_json::json;
use serde_json::Value;
use tauri::{AppHandle, Manager, Url, WebviewUrl, WebviewWindowBuilder};

use crate::config::AppConfig;
use crate::defaultspack_manager::DefaultspackManager;
use crate::kernel_manager::{detect_port_listener, terminate_external_listener, PortListener};
use crate::process_utils;

const DEFAULTSPACK_DEFAULT_PORT: u16 = 8766;
const DEFAULTSPACK_READY_TIMEOUT: Duration = Duration::from_secs(60);
const DEFAULTSPACK_READY_POLL_INTERVAL: Duration = Duration::from_millis(250);
const DEFAULTSPACK_WINDOW_LABEL: &str = "defaultspack-main";
const DEFAULTSPACK_WINDOW_TITLE: &str = "Tobkiri";
static DEFAULTSPACK_LAUNCH_LOCK: OnceLock<Mutex<()>> = OnceLock::new();

fn with_defaultspack_launch_coordination<T>(
    operation: impl FnOnce() -> AnyResult<T>,
) -> AnyResult<T> {
    let lock = DEFAULTSPACK_LAUNCH_LOCK.get_or_init(|| Mutex::new(()));
    let started = Instant::now();
    let _guard = lock
        .lock()
        .map_err(|error| anyhow!("Defaultspack launch coordination lock was poisoned: {error}"))?;
    if started.elapsed().as_millis() > 0 {
        info!(
            "launch_defaultspack_desktop_impl: launch coordination acquired after {} ms",
            started.elapsed().as_millis()
        );
    }
    operation()
}

#[derive(Debug, Clone)]
pub(crate) struct DefaultspackDesktopMetadata {
    entrypoint: PathBuf,
    argv: Vec<OsString>,
    app_working_dir: PathBuf,
    env_vars: Vec<(String, String)>,
    port: u16,
    execution_identity: crate::host_contract::ExecutionProfileIdentity,
    bootstrap_profile_digest: String,
    catalog_revision: String,
    artifact_digest: String,
    function_id: String,
    provider_id: String,
    contract_namespace: String,
    application_id: String,
    base_pack_id: String,
    shell_provider_id: String,
    shell_artifact_id: String,
    shell_artifact_digest: String,
    shell_entrypoint_digest: String,
    host_contract_contributions: crate::host_contract_contributions::HostContractContributionValues,
}

impl DefaultspackDesktopMetadata {
    pub(crate) fn working_dir(&self) -> &Path {
        &self.app_working_dir
    }

    pub(crate) fn port(&self) -> u16 {
        self.port
    }

    pub(crate) fn execution_identity(&self) -> &crate::host_contract::ExecutionProfileIdentity {
        &self.execution_identity
    }

    pub(crate) fn application_id(&self) -> &str {
        &self.application_id
    }

    pub(crate) fn artifact_digest(&self) -> &str {
        &self.artifact_digest
    }

    pub(crate) fn function_id(&self) -> &str {
        &self.function_id
    }

    pub(crate) fn provider_id(&self) -> &str {
        &self.provider_id
    }
}

#[derive(Debug, Clone)]
pub(crate) struct PreparedShellRuntime {
    pub(crate) url: String,
    pub(crate) identity: crate::host_contract::ExecutionProfileIdentity,
    pub(crate) catalog_revision: String,
    pub(crate) base_pack_id: String,
    pub(crate) shell: PreparedShellArtifact,
}

#[derive(Debug, Clone)]
pub(crate) struct PreparedShellArtifact {
    pub(crate) provider_id: String,
    pub(crate) artifact_id: String,
    pub(crate) artifact_digest: String,
    pub(crate) entrypoint_digest: String,
}

/// Read the HMAC key from the plaintext `hmac_keys.json` file.
///
/// Returns the first active key's `key` field. If the file uses Fernet
/// encryption, returns an error (caller should inform the user).
fn read_desktop_api_token(hmac_keys_path: &Path) -> AnyResult<String> {
    let raw = fs::read_to_string(hmac_keys_path)
        .with_context(|| format!("failed to read {}", hmac_keys_path.display()))?;
    let data: Value = serde_json::from_str(&raw)
        .with_context(|| format!("invalid JSON in {}", hmac_keys_path.display()))?;

    // Check for Fernet encryption wrapper
    if data.get("encryption").and_then(|v| v.as_str()) == Some("fernet") {
        bail!("HMAC keys are encrypted. Decrypt them first or set RUMI_SECURITY_MODE=permissive.");
    }

    let keys = data
        .get("keys")
        .and_then(|v| v.as_array())
        .context("hmac_keys.json missing 'keys' array")?;

    for key_entry in keys {
        if key_entry
            .get("is_active")
            .and_then(|v| v.as_bool())
            .is_some_and(|is_active| !is_active)
        {
            continue;
        }
        if let Some(key_str) = key_entry.get("key").and_then(|v| v.as_str()) {
            if !key_str.is_empty() {
                return Ok(key_str.to_string());
            }
        }
    }

    bail!("No active key found in hmac_keys.json")
}

fn venv_bin_dir(venv_dir: &Path) -> PathBuf {
    if cfg!(target_os = "windows") {
        venv_dir.join("Scripts")
    } else {
        venv_dir.join("bin")
    }
}

fn application_origin(port: u16) -> String {
    format!("http://127.0.0.1:{port}")
}

fn encode_url_fragment_value(value: &str) -> String {
    let mut encoded = String::with_capacity(value.len());
    for byte in value.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                encoded.push(byte as char)
            }
            _ => encoded.push_str(&format!("%{byte:02X}")),
        }
    }
    encoded
}

fn defaultspack_window_url_with_local_auth(port: u16, api_token: &str) -> AnyResult<String> {
    let mut url = Url::parse(&application_origin(port))
        .with_context(|| format!("invalid defaultspack window port: {port}"))?;
    url.set_fragment(Some(&format!(
        "rumi_local_auth={}",
        encode_url_fragment_value(api_token)
    )));
    Ok(url.to_string())
}

fn add_defaultspack_bootstrap_code(mut url: Url, code: &str) -> AnyResult<Url> {
    if code.is_empty() {
        bail!("Defaultspack panel bootstrap code must not be empty");
    }
    if url.query_pairs().any(|(key, _)| key == "code") {
        bail!("Defaultspack URL already contains a panel bootstrap code");
    }
    url.set_fragment(None);
    url.query_pairs_mut().append_pair("code", code);
    Ok(url)
}

fn application_url_with_bootstrap_code(port: u16, route: &str, code: &str) -> AnyResult<String> {
    crate::health_check::validate_application_route(route)?;
    let mut url = Url::parse(&application_origin(port))
        .with_context(|| format!("invalid defaultspack window port: {port}"))?;
    url.set_path(route);
    Ok(add_defaultspack_bootstrap_code(url, code)?.to_string())
}

fn defaultspack_window_url_with_path(authenticated_url: &str, path: &str) -> AnyResult<String> {
    let mut url = Url::parse(authenticated_url)
        .with_context(|| format!("invalid authenticated Defaultspack URL: {authenticated_url}"))?;
    let fragment = url.fragment().map(str::to_owned);
    let trimmed = path.trim();
    let path = if trimmed.is_empty() { "/" } else { trimmed };
    if path.contains("://") || path.starts_with("//") || path.contains('\\') {
        bail!("Defaultspack window path must be a same-origin path");
    }
    let path_without_fragment = path.split('#').next().unwrap_or(path);
    let (pathname, query) = match path_without_fragment.split_once('?') {
        Some((pathname, query)) => (pathname, Some(query)),
        None => (path_without_fragment, None),
    };
    if !pathname.starts_with('/') {
        bail!("Defaultspack window path must start with /");
    }
    url.set_path(pathname);
    url.set_query(query);
    url.set_fragment(fragment.as_deref());
    Ok(url.to_string())
}

pub(crate) fn add_defaultspack_local_auth(config: &AppConfig, mut url: Url) -> AnyResult<Url> {
    let api_token = read_desktop_api_token_from_config(config)
        .context("failed to read Viewer local auth token")?;
    url.set_fragment(Some(&format!(
        "rumi_local_auth={}",
        encode_url_fragment_value(&api_token)
    )));
    Ok(url)
}

fn defaultspack_window_url_for_log(url: &str) -> String {
    match Url::parse(url) {
        Ok(mut parsed) => {
            parsed.set_query(None);
            parsed.set_fragment(None);
            parsed.to_string()
        }
        Err(_) => "<invalid defaultspack url>".to_string(),
    }
}

fn defaultspack_health_url(port: u16) -> String {
    format!("http://127.0.0.1:{port}/health")
}

fn read_defaultspack_port(env_vars: &[(String, String)]) -> AnyResult<u16> {
    for key in ["RUMI_DEFAULTSPACK_PORT", "DEFAULTS_HTTP_PORT"] {
        if let Some((_, value)) = env_vars.iter().find(|(env_key, _)| env_key == key) {
            return value
                .parse::<u16>()
                .with_context(|| format!("{key} must be a TCP port, got {value:?}"));
        }
    }
    Ok(DEFAULTSPACK_DEFAULT_PORT)
}

fn is_defaultspack_http_ready(port: u16, bootstrap_secret: &str) -> bool {
    crate::health_check::check_authenticated_health(port, bootstrap_secret).unwrap_or(false)
}

fn wait_for_defaultspack_http_ready(
    port: u16,
    bootstrap_secret: &str,
    manager: &DefaultspackManager,
) -> AnyResult<()> {
    let deadline = Instant::now() + DEFAULTSPACK_READY_TIMEOUT;
    let mut poll_count: u32 = 0;

    loop {
        poll_count += 1;
        if is_defaultspack_http_ready(port, bootstrap_secret) {
            info!(
                "wait_for_defaultspack_http_ready: ready after {poll_count} polls on port {port}"
            );
            return Ok(());
        }

        if Instant::now() >= deadline {
            warn!(
                "wait_for_defaultspack_http_ready: timed out after {poll_count} polls; stopping managed pack-shell"
            );
            if let Err(error) = manager.stop() {
                warn!("wait_for_defaultspack_http_ready: failed to stop timed out pack-shell: {error:#}");
            }
            bail!(
                "Defaultspack local server did not become ready at {} within {} seconds",
                defaultspack_health_url(port),
                DEFAULTSPACK_READY_TIMEOUT.as_secs()
            );
        }

        if poll_count.is_multiple_of(20) {
            info!("wait_for_defaultspack_http_ready: still waiting (poll #{poll_count}) on port {port}...");
        }

        thread::sleep(DEFAULTSPACK_READY_POLL_INTERVAL);
    }
}

fn append_path_prefix(prefix: &Path, current_path: Option<OsString>) -> AnyResult<OsString> {
    let mut paths = vec![prefix.to_path_buf()];
    if let Some(current_path) = current_path {
        paths.extend(std::env::split_paths(&current_path));
    }
    std::env::join_paths(paths).map_err(|error| anyhow!("failed to build PATH: {error}"))
}

/// Reject the removed legacy Dock wrapper registration path.
#[tauri::command]
pub fn register_defaultspack_dock(config: tauri::State<'_, AppConfig>) -> Result<String, String> {
    register_defaultspack_dock_impl(&config).map_err(|e| {
        error!("register_defaultspack_dock failed: {e:#}");
        format!("{e:#}")
    })
}

pub(crate) fn register_defaultspack_dock_impl(config: &AppConfig) -> AnyResult<String> {
    let _ = config;
    bail!(
        "legacy Defaultspack Dock wrappers are removed; select and launch a verified Shell artifact"
    )
}

#[tauri::command]
pub async fn launch_defaultspack_desktop(
    app: AppHandle,
    config: tauri::State<'_, AppConfig>,
) -> Result<String, String> {
    let app_handle = app.clone();
    let app_config = config.inner().clone();
    let launch_result = tauri::async_runtime::spawn_blocking(move || {
        launch_defaultspack_desktop_window_impl(&app_handle, &app_config)
    })
    .await
    .map_err(|error| {
        error!("launch_defaultspack_desktop task failed: {error}");
        "Defaultspack desktop could not be launched".to_string()
    })?;
    launch_result.map_err(|e| {
        error!("launch_defaultspack_desktop failed: {e:#}");
        format!("{e:#}")
    })
}

pub(crate) fn launch_defaultspack_desktop_window_impl(
    app: &AppHandle,
    config: &AppConfig,
) -> AnyResult<String> {
    let result = crate::presentation::launch_selected_presentation_impl(app, config)?;
    Ok(result.message)
}

pub(crate) fn prepare_defaultspack_shell_runtime_url(
    app: &AppHandle,
    config: &AppConfig,
    launch_route: &str,
) -> AnyResult<PreparedShellRuntime> {
    crate::health_check::validate_application_route(launch_route)?;
    with_defaultspack_launch_coordination(|| {
        let (metadata, bootstrap_secret) = ensure_defaultspack_desktop_ready(app, config)?;
        let code = crate::request_panel_bootstrap_code_with_retry(metadata.port, &bootstrap_secret)
            .context("failed to issue a Defaultspack shell bootstrap code")?;
        Ok(PreparedShellRuntime {
            url: application_url_with_bootstrap_code(metadata.port, launch_route, &code)?,
            identity: metadata.execution_identity.clone(),
            catalog_revision: metadata.catalog_revision.clone(),
            base_pack_id: metadata.base_pack_id.clone(),
            shell: PreparedShellArtifact {
                provider_id: metadata.shell_provider_id.clone(),
                artifact_id: metadata.shell_artifact_id.clone(),
                artifact_digest: metadata.shell_artifact_digest.clone(),
                entrypoint_digest: metadata.shell_entrypoint_digest.clone(),
            },
        })
    })
}

/// Ensure the real Defaultspack listener is Launcher-owned and registered as
/// the debug guardian without opening or focusing its desktop window.
pub(crate) fn prepare_defaultspack_guardian_impl(
    app: &AppHandle,
    config: &AppConfig,
) -> AnyResult<()> {
    with_defaultspack_launch_coordination(|| {
        ensure_defaultspack_desktop_ready(app, config)?;
        Ok(())
    })
}

pub(crate) fn open_defaultspack_desktop_window_path_impl(
    app: &AppHandle,
    config: &AppConfig,
    path: &str,
) -> AnyResult<String> {
    with_defaultspack_launch_coordination(|| {
        info!("open_defaultspack_desktop_window_path_impl: starting");
        let (metadata, _) = ensure_defaultspack_desktop_ready(app, config)?;
        let api_token = read_desktop_api_token_from_config(config)
            .context("failed to read Viewer local auth token for Defaultspack window")?;
        let authenticated_url = defaultspack_window_url_with_local_auth(metadata.port, &api_token)?;
        let url = defaultspack_window_url_with_path(&authenticated_url, path)?;
        open_defaultspack_tauri_window(app, &url)?;
        info!(
            "open_defaultspack_desktop_window_path_impl: opened Tauri window {}",
            defaultspack_window_url_for_log(&url)
        );
        Ok("Tobkiriを開きました".into())
    })
}

fn normalized_process_value(value: &str) -> String {
    value
        .trim()
        .replace('\\', "/")
        .trim_end_matches('/')
        .to_ascii_lowercase()
}

fn identify_defaultspack_listener(
    listener: &PortListener,
    metadata: &DefaultspackDesktopMetadata,
) -> bool {
    let command = normalized_process_value(&listener.command);
    let working_dir = normalized_process_value(&metadata.app_working_dir.to_string_lossy());
    let cwd_matches = listener
        .cwd
        .as_deref()
        .is_some_and(|cwd| normalized_process_value(cwd) == working_dir);
    let command_mentions_working_dir = !working_dir.is_empty() && command.contains(&working_dir);
    let command_mentions_defaultspack = command.contains("defaultspack");

    // A generic Python process is never enough. Ownership requires both the
    // Defaultspack identity and the configured pack working directory, either
    // as the process cwd or as an explicit pack-shell argument.
    command_mentions_defaultspack && (cwd_matches || command_mentions_working_dir)
}

fn identify_authenticated_stale_defaultspack_listener(
    listener: &PortListener,
    metadata: &DefaultspackDesktopMetadata,
) -> bool {
    if identify_defaultspack_listener(listener, metadata) {
        return false;
    }
    normalized_process_value(&listener.command).contains("defaultspack")
}

fn listener_matches_launcher_owned_defaultspack(
    listener: &PortListener,
    metadata: &DefaultspackDesktopMetadata,
    retained_manager_process: bool,
) -> bool {
    retained_manager_process || identify_defaultspack_listener(listener, metadata)
}

fn is_launcher_owned_defaultspack_listener(
    manager: &DefaultspackManager,
    listener: &PortListener,
    metadata: &DefaultspackDesktopMetadata,
) -> AnyResult<bool> {
    if !process_is_descendant_of(listener.pid, std::process::id())? {
        return Ok(false);
    }
    let retained_manager_process = match manager.managed_child_pid()? {
        Some(managed_pid) => process_is_descendant_of(listener.pid, managed_pid)?,
        None => false,
    };
    Ok(listener_matches_launcher_owned_defaultspack(
        listener,
        metadata,
        retained_manager_process,
    ))
}

fn recover_authenticated_stale_defaultspack_listener(
    manager: &DefaultspackManager,
    metadata: &DefaultspackDesktopMetadata,
) -> AnyResult<bool> {
    let Some(listener) = detect_port_listener(metadata.port)? else {
        return Ok(false);
    };
    if is_launcher_owned_defaultspack_listener(manager, &listener, metadata)? {
        return Ok(false);
    }
    if !identify_authenticated_stale_defaultspack_listener(&listener, metadata) {
        return Ok(false);
    }
    warn!(
        "Stopping authenticated Defaultspack listener from a prior app bundle on port {}: pid {} ({})",
        metadata.port,
        listener.pid,
        listener.summary()
    );
    terminate_external_listener(listener.pid, metadata.port).with_context(|| {
        format!(
            "failed to stop authenticated stale Defaultspack listener pid {} on port {}",
            listener.pid, metadata.port
        )
    })?;
    Ok(true)
}

fn recover_stale_defaultspack_listener(metadata: &DefaultspackDesktopMetadata) -> AnyResult<()> {
    let listener = detect_port_listener(metadata.port)?.ok_or_else(|| {
        anyhow!(
            "Defaultspack port {} is occupied, but Viewer could not identify the listener. Viewer did not stop it. Close that process or free port {}.",
            metadata.port,
            metadata.port
        )
    })?;
    if !identify_defaultspack_listener(&listener, metadata) {
        bail!(
            "Defaultspack port {} is occupied by pid {} ({}), which is not an owned Defaultspack process. Viewer did not stop it. Close that process or free port {}.",
            metadata.port,
            listener.pid,
            listener.summary(),
            metadata.port
        );
    }
    warn!(
        "Stopping stale owned Defaultspack listener on port {}: pid {} ({})",
        metadata.port,
        listener.pid,
        listener.summary()
    );
    terminate_external_listener(listener.pid, metadata.port).with_context(|| {
        format!(
            "failed to stop stale owned Defaultspack listener pid {} on port {}",
            listener.pid, metadata.port
        )
    })
}

/// Project a verified active Application authority into the Host contract.
///
/// The runtime health challenge proves that the running Kernel is reporting
/// this exact identity.  The separately resolved metadata proves the identity
/// and contribution values originate from the durable Profile authority; a
/// bootstrap Host contract is never treated as execution authority here.
fn publish_active_host_contract(
    config: &AppConfig,
    metadata: &DefaultspackDesktopMetadata,
    panel_bootstrap_secret: &str,
    desktop_api_token: Option<&str>,
) -> AnyResult<()> {
    let active_identity = crate::health_check::authenticated_runtime_identity(
        config.kernel_port,
        panel_bootstrap_secret,
        &metadata.contract_namespace,
    )
    .context("failed to capture the active execution Profile identity")?;
    if !active_identity.matches(metadata.execution_identity()) {
        bail!("authenticated runtime identity differs from the verified active Profile authority");
    }

    let mut values = vec![
        ("panel_bootstrap_secret", panel_bootstrap_secret.to_owned()),
        (
            "system_pack_descriptors",
            metadata
                .host_contract_contributions
                .system_pack_descriptors
                .clone(),
        ),
        (
            "update_target_descriptors",
            metadata
                .host_contract_contributions
                .update_target_descriptors
                .clone(),
        ),
    ];
    if let Some(token) = desktop_api_token {
        values.push(("desktop_api_token", token.to_owned()));
    }
    crate::host_contract::write_contract(config, metadata.execution_identity(), values)
        .context("failed to publish the active Host contract")?;
    Ok(())
}

fn ensure_defaultspack_desktop_ready(
    app: &AppHandle,
    config: &AppConfig,
) -> AnyResult<(DefaultspackDesktopMetadata, String)> {
    let manager = app.state::<Arc<DefaultspackManager>>();
    let api_token = read_desktop_api_token_from_config(config)
        .context("failed to read Viewer local auth token for Defaultspack launch")?;
    let panel_bootstrap_secret = read_panel_bootstrap_secret_from_config(config)?;
    let metadata = match read_defaultspack_desktop_metadata(config) {
        Ok(m) => {
            info!("launch_defaultspack_desktop_impl: metadata loaded (port={}, entrypoint={}, argv_count={}, working_dir={})",
                m.port, m.entrypoint.display(), m.argv.len(), m.app_working_dir.display());
            m
        }
        Err(e) => {
            error!("launch_defaultspack_desktop_impl: failed to read defaultspack metadata: {e:#}");
            return Err(e);
        }
    };
    publish_active_host_contract(
        config,
        &metadata,
        &panel_bootstrap_secret,
        Some(api_token.as_str()),
    )?;
    let base_url = application_origin(metadata.port);
    info!("launch_defaultspack_desktop_impl: Defaultspack window URL will be {base_url}");

    let managed_process = manager
        .has_managed_process()
        .context("failed to inspect managed Defaultspack process")?;
    let mut server_ready = is_defaultspack_http_ready(metadata.port, &panel_bootstrap_secret);
    if server_ready {
        let observed_identity = crate::health_check::authenticated_runtime_identity(
            metadata.port,
            &panel_bootstrap_secret,
            &metadata.contract_namespace,
        )
        .context("authenticated Defaultspack identity is unavailable")?;
        if !observed_identity.matches(metadata.execution_identity()) {
            warn!(
                "Authenticated Defaultspack listener identity differs from the active execution Profile; it will not be reused"
            );
            server_ready = false;
        }
    }
    if server_ready && recover_authenticated_stale_defaultspack_listener(&manager, &metadata)? {
        server_ready = false;
    }
    if server_ready {
        let listener = detect_port_listener(metadata.port)?.ok_or_else(|| {
            anyhow!("authenticated Defaultspack listener identity is unavailable")
        })?;
        if !is_launcher_owned_defaultspack_listener(&manager, &listener, &metadata)? {
            warn!(
                "Stopping authenticated Defaultspack listener that is not descended from this Launcher: pid {}",
                listener.pid
            );
            terminate_external_listener(listener.pid, metadata.port)?;
            server_ready = false;
        }
    }

    if server_ready {
        info!(
            "launch_defaultspack_desktop_impl: authenticated health check passed, server already ready at {base_url}"
        );
    } else {
        if !managed_process && detect_port_listener(metadata.port)?.is_some() {
            recover_stale_defaultspack_listener(&metadata)?;
        }
        info!("launch_defaultspack_desktop_impl: health check indicates server not ready; ensuring supervised pack-shell is running...");
        if let Err(error) = manager.start_or_reuse(metadata.clone()) {
            error!("launch_defaultspack_desktop_impl: failed to start supervised pack-shell: {error:#}");
            info!(
                "launch_defaultspack_desktop_impl: pack_shell_path={}",
                config
                    .pack_shell_path()
                    .map(|path| path.display().to_string())
                    .unwrap_or_else(|| "<not found>".to_string())
            );
            return Err(error);
        }
        match wait_for_defaultspack_http_ready(
            metadata.port,
            &panel_bootstrap_secret,
            manager.inner(),
        ) {
            Ok(()) => info!("launch_defaultspack_desktop_impl: server became ready at {base_url}"),
            Err(e) => {
                error!("launch_defaultspack_desktop_impl: wait_for_ready failed: {e:#}");
                return Err(e);
            }
        }
    }

    let mut listener = detect_port_listener(metadata.port)?
        .ok_or_else(|| anyhow!("authenticated Defaultspack listener identity is unavailable"))?;
    if !is_launcher_owned_defaultspack_listener(&manager, &listener, &metadata)? {
        // The Kernel can finish restoring an old startup profile while the
        // supervised pack-shell is starting. Resolve that race once: stop the
        // authenticated but unowned winner, then launch a fresh owned child.
        warn!(
            "Replacing authenticated Defaultspack listener that won the startup race without Launcher ownership: pid {}",
            listener.pid
        );
        terminate_external_listener(listener.pid, metadata.port)?;
        manager.stop()?;
        manager.start_or_reuse(metadata.clone())?;
        wait_for_defaultspack_http_ready(metadata.port, &panel_bootstrap_secret, manager.inner())?;
        listener = detect_port_listener(metadata.port)?
            .ok_or_else(|| anyhow!("replacement Defaultspack listener identity is unavailable"))?;
    }
    if !is_launcher_owned_defaultspack_listener(&manager, &listener, &metadata)? {
        bail!("replacement Defaultspack listener is not owned by this Launcher");
    }
    let observed_identity = crate::health_check::authenticated_runtime_identity(
        metadata.port,
        &panel_bootstrap_secret,
        &metadata.contract_namespace,
    )
    .context("authenticated Defaultspack identity is unavailable after readiness")?;
    if !observed_identity.matches(metadata.execution_identity()) {
        manager
            .stop()
            .context("failed to stop a Defaultspack listener with stale Profile identity")?;
        bail!("Defaultspack listener identity does not match the active execution Profile");
    }
    if let Some(wrapper_pid) = manager.managed_child_pid()? {
        if !process_is_descendant_of(listener.pid, wrapper_pid)? {
            // A Kernel-restored server won the bind race. It is still a
            // Launcher child, but the losing pack-shell monitor must not own
            // (and later unregister) its guardian record.
            manager.stop()?;
        }
    }
    manager.register_launcher_owned_listener(&metadata, listener.pid, listener.command)?;
    if let Err(error) = write_guardian_ready_audit(config, &metadata) {
        manager
            .stop()
            .context("failed to stop an unaudited Defaultspack guardian")?;
        return Err(error);
    }

    Ok((metadata, panel_bootstrap_secret))
}

fn write_guardian_ready_audit(
    config: &AppConfig,
    metadata: &DefaultspackDesktopMetadata,
) -> AnyResult<()> {
    crate::host_audit::write_audit_log(
        &config.host_broker_audit_log_path(),
        &crate::host_audit::HostAuditEntry {
            audit_id: format!(
                "defaultspack-guardian-ready-{}-{}",
                crate::host_audit::now_epoch_seconds(),
                std::process::id()
            ),
            ts: crate::host_audit::now_epoch_seconds(),
            function_id: "launcher.defaultspack.guardian.prepare".to_string(),
            profile_id: Some(metadata.execution_identity().profile_id.clone()),
            pack_id: Some("defaultspack".to_string()),
            conversation_id: None,
            allowed: true,
            result_ok: true,
            approval_token_present: None,
            approval_result: None,
            args_summary: json!({
                "authority": "pack-v4-profile-lock",
                "profile_id": metadata.execution_identity().profile_id,
                "profile_revision": metadata.execution_identity().profile_revision,
                "activation_id": metadata.execution_identity().activation_id,
                "plan_digest": metadata.execution_identity().plan_digest,
                "catalog_revision": metadata.catalog_revision,
                "bootstrap_profile_digest": metadata.bootstrap_profile_digest,
                "artifact_digest": metadata.artifact_digest,
                "function_id": metadata.function_id,
                "provider_id": metadata.provider_id,
                "health": "ready",
                "local_auth": "verified",
                "guardian": "registered",
            }),
        },
    )
    .context("failed to durably audit Defaultspack guardian readiness")
}

#[cfg(unix)]
fn process_is_descendant_of(mut process_id: u32, ancestor_id: u32) -> AnyResult<bool> {
    for _ in 0..64 {
        if process_id == ancestor_id {
            return Ok(true);
        }
        let output = process_utils::command("/bin/ps")
            .args(["-p", &process_id.to_string(), "-o", "ppid="])
            .output()
            .context("failed to inspect Defaultspack process ancestry")?;
        if !output.status.success() {
            return Ok(false);
        }
        let parent = String::from_utf8_lossy(&output.stdout)
            .trim()
            .parse::<u32>()
            .unwrap_or(0);
        if parent == 0 || parent == process_id {
            return Ok(false);
        }
        process_id = parent;
    }
    Ok(false)
}

#[cfg(windows)]
fn process_is_descendant_of(mut process_id: u32, ancestor_id: u32) -> AnyResult<bool> {
    for _ in 0..64 {
        if process_id == ancestor_id {
            return Ok(true);
        }
        let script = format!(
            "$p=Get-CimInstance Win32_Process -Filter \\\"ProcessId = {process_id}\\\";\
             if($null -eq $p){{exit 3}};[Console]::Write($p.ParentProcessId)"
        );
        let output = process_utils::command("powershell.exe")
            .args(["-NoProfile", "-NonInteractive", "-Command", &script])
            .output()
            .context("failed to inspect Defaultspack process ancestry")?;
        if !output.status.success() {
            return Ok(false);
        }
        let parent = String::from_utf8_lossy(&output.stdout)
            .trim()
            .parse::<u32>()
            .unwrap_or(0);
        if parent == 0 || parent == process_id {
            return Ok(false);
        }
        process_id = parent;
    }
    Ok(false)
}

fn focus_defaultspack_window(window: &tauri::WebviewWindow) -> AnyResult<()> {
    window
        .unminimize()
        .context("failed to unminimize defaultspack window")?;
    window
        .show()
        .context("failed to show defaultspack window")?;
    window
        .set_focus()
        .context("failed to focus defaultspack window")
}

pub(crate) fn is_defaultspack_main_window(label: &str) -> bool {
    label == DEFAULTSPACK_WINDOW_LABEL
}

fn focus_defaultspack_workspace(app: &AppHandle, window: &tauri::WebviewWindow) -> AnyResult<()> {
    focus_defaultspack_window(window)?;
    crate::send_app_to_background(app)
        .map_err(|error| anyhow!("failed to hide launcher behind Tobkiri: {error}"))
}

fn open_defaultspack_tauri_window(app: &AppHandle, url: &str) -> AnyResult<()> {
    let url = Url::parse(url).with_context(|| format!("invalid defaultspack URL: {url}"))?;
    if let Some(window) = app.get_webview_window(DEFAULTSPACK_WINDOW_LABEL) {
        window
            .navigate(url)
            .context("failed to navigate defaultspack window")?;
        return focus_defaultspack_workspace(app, &window);
    }

    let builder =
        WebviewWindowBuilder::new(app, DEFAULTSPACK_WINDOW_LABEL, WebviewUrl::External(url))
            .title(DEFAULTSPACK_WINDOW_TITLE)
            .inner_size(980.0, 720.0)
            .min_inner_size(860.0, 600.0)
            .resizable(true)
            .focused(true)
            .visible(true);
    #[cfg(target_os = "macos")]
    let builder = builder
        .hidden_title(true)
        .title_bar_style(tauri::TitleBarStyle::Transparent);
    let window = builder
        .build()
        .context("failed to open defaultspack window")?;
    focus_defaultspack_workspace(app, &window)
}

fn read_defaultspack_desktop_metadata(
    config: &AppConfig,
) -> AnyResult<DefaultspackDesktopMetadata> {
    let authority = crate::defaultspack_authority::resolve(config)?;
    let execution_identity = authority.execution_identity()?;
    let host_contract_contributions =
        crate::host_contract_contributions::collect_for_verified_application(&authority)?;
    let app_working_dir = authority.pack_root;
    let mut env_vars = vec![
        (
            "DEFAULTS_HTTP_PORT".into(),
            DEFAULTSPACK_DEFAULT_PORT.to_string(),
        ),
        (
            "RUMI_DEFAULTSPACK_PORT".into(),
            DEFAULTSPACK_DEFAULT_PORT.to_string(),
        ),
        ("RUMI_DEFAULTSPACK_SURFACE".into(), "webview".into()),
    ];
    let mut port = read_defaultspack_port(&env_vars)?;
    if let Some((debug_http_port, debug_kernel_port)) = crate::debug_defaultspack_ports_from_env() {
        if config.kernel_port != debug_kernel_port {
            bail!(
                "debug Defaultspack isolation kernel port did not match the Viewer configuration"
            );
        }
        // Debug isolation is accepted only after lib.rs has validated the
        // complete Viewer/run identity.  Override metadata, not production
        // defaults, so every URL/readiness check and child environment uses
        // the same run-owned loopback port.
        env_vars.retain(|(key, _)| key != "RUMI_DEFAULTSPACK_PORT" && key != "DEFAULTS_HTTP_PORT");
        env_vars.push(("RUMI_DEFAULTSPACK_PORT".into(), debug_http_port.to_string()));
        env_vars.push(("DEFAULTS_HTTP_PORT".into(), debug_http_port.to_string()));
        port = debug_http_port;
    }

    let shell_artifact_digest = authority.launch.artifact_digest.clone();
    Ok(DefaultspackDesktopMetadata {
        entrypoint: authority.launch.entrypoint,
        argv: authority.launch.argv,
        app_working_dir,
        env_vars,
        port,
        execution_identity,
        bootstrap_profile_digest: authority.profile_digest,
        catalog_revision: authority.catalog_revision,
        artifact_digest: authority.launch.artifact_digest,
        shell_artifact_id: authority.launch.artifact_id,
        shell_artifact_digest,
        shell_entrypoint_digest: authority.launch.entrypoint_digest,
        host_contract_contributions,
        function_id: authority.launch.function_id,
        provider_id: authority.launch.provider_id,
        contract_namespace: authority.launch.contract_namespace,
        application_id: authority.application_id,
        base_pack_id: authority.base_pack_id,
        shell_provider_id: authority.shell_provider_id,
    })
}

fn read_desktop_api_token_from_config(config: &AppConfig) -> AnyResult<String> {
    let token_path = config.desktop_api_token_path();
    let candidates = [
        config.user_data_dir.join("hmac_keys.json"),
        config.rumi_home.join("user_data").join("hmac_keys.json"),
    ];
    let mut saw_hmac_store = false;
    let mut encrypted_store_error: Option<anyhow::Error> = None;
    let mut plaintext_store_error: Option<anyhow::Error> = None;

    // The active HMAC store is authoritative. Reading the desktop cache first
    // can reuse a rotated token and launch a server that immediately rejects
    // every Viewer request.
    for hmac_keys_path in &candidates {
        if !hmac_keys_path.exists() {
            continue;
        }
        saw_hmac_store = true;
        info!(
            "read_desktop_api_token_from_config: checking active HMAC store {}",
            hmac_keys_path.display()
        );
        match read_desktop_api_token(hmac_keys_path) {
            Ok(token) => {
                let _ = persist_desktop_api_token(config, &token);
                return Ok(token);
            }
            Err(error) if error.to_string().to_ascii_lowercase().contains("encrypted") => {
                encrypted_store_error = Some(error);
            }
            Err(error) => {
                plaintext_store_error = Some(error);
            }
        }
    }

    if let Some(error) = plaintext_store_error {
        return Err(error).context(
            "active plaintext HMAC store is unreadable; Viewer refused to use a potentially stale cached token",
        );
    }

    if saw_hmac_store && encrypted_store_error.is_some() {
        match read_saved_desktop_api_token(&token_path) {
            Ok(token) => {
                info!(
                    "active HMAC store is encrypted; using the Kernel-managed desktop token cache"
                );
                return Ok(token);
            }
            Err(cache_error) => {
                return Err(cache_error).with_context(|| {
                    format!(
                        "active HMAC store is encrypted and the Kernel-managed desktop token cache is unavailable at {}",
                        token_path.display()
                    )
                });
            }
        }
    }

    if !saw_hmac_store {
        if let Ok(token) = read_saved_desktop_api_token(&token_path) {
            info!(
                "read_desktop_api_token_from_config: no HMAC store yet; using Kernel-managed desktop token cache"
            );
            return Ok(token);
        }
    }

    bail!(
        "local auth token is not configured (checked {}, {}, and {}). Start or restart the Kernel first.",
        candidates[0].display(),
        candidates[1].display(),
        token_path.display()
    )
}

fn read_saved_desktop_api_token(token_path: &Path) -> AnyResult<String> {
    let token = fs::read_to_string(token_path)
        .with_context(|| format!("failed to read {}", token_path.display()))?;
    let token = token.trim().to_string();
    if token.is_empty() {
        bail!("desktop API token is empty at {}", token_path.display());
    }
    Ok(token)
}

fn persist_desktop_api_token(config: &AppConfig, api_token: &str) -> AnyResult<PathBuf> {
    let token_path = config.desktop_api_token_path();
    if let Some(parent) = token_path.parent() {
        fs::create_dir_all(parent)?;
    }
    fs::write(&token_path, api_token)
        .with_context(|| format!("failed to write token to {}", token_path.display()))?;
    #[cfg(unix)]
    {
        let _ = fs::set_permissions(&token_path, fs::Permissions::from_mode(0o600));
    }
    info!("Desktop API token saved to {}", token_path.display());
    Ok(token_path)
}

fn read_panel_bootstrap_secret_from_config(config: &AppConfig) -> AnyResult<String> {
    let path = config.panel_bootstrap_secret_path();
    let secret = fs::read_to_string(&path)
        .with_context(|| format!("failed to read {}", path.display()))?
        .trim()
        .to_string();
    if secret.is_empty() {
        bail!("panel bootstrap secret is empty at {}", path.display());
    }
    Ok(secret)
}

/// Put a Launcher-owned Defaultspack process in its own group so shutdown can
/// terminate the pack-shell wrapper and the desktop-app process together.
#[cfg(unix)]
pub(crate) fn configure_defaultspack_process_group(command: &mut std::process::Command) {
    command.process_group(0);
}

pub(crate) fn spawn_defaultspack_local_server(
    config: &AppConfig,
    metadata: &DefaultspackDesktopMetadata,
    broker_attestation: &crate::host_broker::BrokerAttestationIdentity,
    guardian_run_id: &str,
) -> AnyResult<crate::python_env::PythonChild> {
    let api_token = read_desktop_api_token_from_config(config)?;
    let panel_bootstrap_secret = read_panel_bootstrap_secret_from_config(config)?;
    let host_contract_path = crate::host_contract::write_contract(
        config,
        metadata.execution_identity(),
        [
            ("desktop_api_token", api_token.clone()),
            ("panel_bootstrap_secret", panel_bootstrap_secret),
            (
                "system_pack_descriptors",
                metadata
                    .host_contract_contributions
                    .system_pack_descriptors
                    .clone(),
            ),
            (
                "update_target_descriptors",
                metadata
                    .host_contract_contributions
                    .update_target_descriptors
                    .clone(),
            ),
        ],
    )?;
    let path = append_path_prefix(&venv_bin_dir(&config.venv_dir), std::env::var_os("PATH"))?;
    info!(
        "spawn_defaultspack_local_server: python={}, port={}, working_dir={}",
        config.venv_python().display(),
        metadata.port,
        metadata.app_working_dir.display(),
    );

    // Spawn the actual long-lived server as the Launcher child. pack-shell's
    // `run` command delegates to Kernel and exits, which leaves the real
    // listener orphaned and cannot provide a process-lifetime guardian.
    let role_arguments =
        crate::python_env::RoleArguments::defaultspack(metadata.argv.iter().cloned())?;
    crate::python_env::spawn_python_role(
        config,
        crate::python_env::PythonRole::Defaultspack,
        role_arguments,
        |command| {
            if config.is_dev_workspace() {
                command
                    .env("PATH", &path)
                    .env("RUMI_APP_DIR", &config.app_dir);
            }
            command
                .env_remove("PYTHONPATH")
                .env("RUMI_HOME", &config.rumi_home)
                .env("RUMI_USER_DATA", &config.user_data_dir)
                .env(
                    "RUMI_DEFAULTSPACK_SECRETS_DIR",
                    config.user_data_dir.join("secrets"),
                )
                .env(
                    "RUMI_DEFAULTSPACK_FRONTEND_SETTINGS_PATH",
                    config
                        .user_data_dir
                        .join("defaultspack")
                        .join("shared")
                        .join("frontend_settings.json"),
                )
                .env("RUMI_LOG_DIR", &config.log_dir)
                .env(
                    "RUMI_ENVIRONMENT",
                    if cfg!(debug_assertions) || config.is_dev_workspace() {
                        "development"
                    } else {
                        "production"
                    },
                )
                .env("PYTHONDONTWRITEBYTECODE", "1")
                .env(
                    viewer_host_broker_connection_env_key(),
                    viewer_host_broker_connection_env_value(config),
                )
                .env(
                    "RUMI_VIEWER_BROKER_ATTESTATION_PUBLIC_KEY",
                    broker_attestation.public_key_base64(),
                )
                .env(
                    "RUMI_VIEWER_BROKER_INSTANCE_NONCE",
                    broker_attestation.instance_nonce(),
                )
                .env("RUMI_DEFAULTSPACK_GUARDIAN_RUN_ID", guardian_run_id)
                .env(crate::host_contract::CONTRACT_ENV, &host_contract_path)
                .current_dir(&metadata.app_working_dir)
                .stdin(Stdio::null())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped());

            // The metadata map exists for development launch compatibility. A
            // packaged child receives only the typed values below; even a
            // catalog-controlled key cannot become an environment injection
            // path in production.
            apply_defaultspack_metadata_environment(
                command,
                config.is_dev_workspace(),
                &metadata.env_vars,
            );
            // Metadata is deliberately applied first.  These exact values are then
            // asserted for the child so a desktop_app env entry cannot redirect a
            // debug run to production/default ports or an unrelated listener.
            command
                .env(
                    viewer_host_broker_connection_env_key(),
                    viewer_host_broker_connection_env_value(config),
                )
                .env(
                    "RUMI_VIEWER_BROKER_ATTESTATION_PUBLIC_KEY",
                    broker_attestation.public_key_base64(),
                )
                .env(
                    "RUMI_VIEWER_BROKER_INSTANCE_NONCE",
                    broker_attestation.instance_nonce(),
                )
                .env("RUMI_DEFAULTSPACK_GUARDIAN_RUN_ID", guardian_run_id)
                .env("DEFAULTS_HTTP_HOST", "127.0.0.1")
                .env("DEFAULTS_HTTP_PORT", metadata.port.to_string())
                .env("RUMI_DEFAULTSPACK_PORT", metadata.port.to_string())
                .env("RUMI_DEFAULTSPACK_SURFACE", "webview")
                .env("RUMI_PORT", config.kernel_port.to_string());
            if crate::debug_defaultspack_ports_from_env().is_some() {
                command
                    .env("RUMI_DEFAULTSPACK_DEBUG_ISOLATION", "1")
                    .env("RUMI_DEFAULTSPACK_REQUIRE_OWN_BIND", "1");
            }
            command
                .env("RUMI_DEFAULTSPACK_OPEN_BROWSER", "0")
                .env("PYTHONDONTWRITEBYTECODE", "1");

            #[cfg(unix)]
            command.new_process_group();
            Ok(())
        },
    )
    .with_context(|| {
        format!(
            "failed to verify and spawn managed Defaultspack with {}",
            config.venv_python().display()
        )
    })
}

fn apply_defaultspack_metadata_environment(
    command: &mut crate::python_env::RoleCommand<'_>,
    development_workspace: bool,
    environment: &[(String, String)],
) {
    if development_workspace {
        command.envs(environment.iter().map(|(key, value)| (key, value)));
    }
}

fn viewer_host_broker_connection_env_key() -> &'static str {
    "RUMI_VIEWER_HOST_BROKER_CONNECTION"
}

fn viewer_host_broker_connection_env_value(config: &AppConfig) -> PathBuf {
    config.host_broker_connection_path()
}

#[cfg(test)]
mod tests {
    use super::*;
    use hmac::{Hmac, Mac};
    use sha2::Sha256;
    use std::io::{Read, Write};
    use std::net::TcpListener;

    fn test_config(root: &Path) -> AppConfig {
        let user_data_dir = root.join("app-data").join("user_data");
        AppConfig {
            app_dir: root.join("runtime"),
            rumi_home: root.join("runtime"),
            python_dir: root.join("python"),
            uv_path: root.join("uv"),
            venv_dir: root.join("venv"),
            user_data_dir,
            log_dir: root.join("logs"),
            kernel_port: 8765,
            dev_workspace_root: None,
        }
    }

    #[test]
    fn packaged_defaultspack_does_not_apply_generic_metadata_environment() {
        let metadata_environment = vec![
            ("RUMI_HOME".to_owned(), "/catalog-controlled".to_owned()),
            ("PYTHONPATH".to_owned(), "/import-injection".to_owned()),
        ];
        let mut packaged = std::process::Command::new("python");
        apply_defaultspack_metadata_environment(
            &mut crate::python_env::RoleCommand::new(&mut packaged),
            false,
            &metadata_environment,
        );
        assert_eq!(packaged.get_envs().count(), 0);

        let mut development = std::process::Command::new("python");
        apply_defaultspack_metadata_environment(
            &mut crate::python_env::RoleCommand::new(&mut development),
            true,
            &metadata_environment,
        );
        assert_eq!(development.get_envs().count(), 2);
    }

    #[test]
    fn read_desktop_api_token_rejects_encrypted() {
        let dir = std::env::temp_dir().join("rumi_dock_test_encrypted");
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("hmac_keys.json");
        fs::write(
            &path,
            r#"{"version":"1.0","encryption":"fernet","payload":"abc"}"#,
        )
        .unwrap();
        let result = read_desktop_api_token(&path);
        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("encrypted"));
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn read_desktop_api_token_returns_first_key() {
        let dir = std::env::temp_dir().join("rumi_dock_test_plaintext");
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("hmac_keys.json");
        fs::write(
            &path,
            r#"{"version":"1.0","keys":[{"key":"test-token-123","created_at":1000}]}"#,
        )
        .unwrap();
        let result = read_desktop_api_token(&path).unwrap();
        assert_eq!(result, "test-token-123");
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn read_desktop_api_token_skips_inactive_keys() {
        let dir = std::env::temp_dir().join("rumi_dock_test_active_key");
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("hmac_keys.json");
        fs::write(
            &path,
            r#"{"version":"1.0","keys":[{"key":"old-token","is_active":false},{"key":"active-token","is_active":true}]}"#,
        )
        .unwrap();
        let result = read_desktop_api_token(&path).unwrap();
        assert_eq!(result, "active-token");
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn read_saved_desktop_api_token_trims_file_contents() {
        let dir = std::env::temp_dir().join("rumi_dock_test_saved_token");
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join(".desktop_api_token");
        fs::write(&path, "saved-token\n").unwrap();
        let result = read_saved_desktop_api_token(&path).unwrap();
        assert_eq!(result, "saved-token");
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn read_saved_desktop_api_token_rejects_empty_file() {
        let dir = std::env::temp_dir().join("rumi_dock_test_empty_saved_token");
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join(".desktop_api_token");
        fs::write(&path, "\n").unwrap();
        let result = read_saved_desktop_api_token(&path);
        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("empty"));
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn register_defaultspack_dock_rejects_legacy_wrapper_registration() {
        let root = std::env::temp_dir().join(format!(
            "tobkiri_dock_registration_rejected_{}",
            std::process::id()
        ));
        let config = test_config(&root);

        let error = register_defaultspack_dock_impl(&config).unwrap_err();

        assert!(error
            .to_string()
            .contains("legacy Defaultspack Dock wrappers are removed"));
    }

    #[test]
    fn config_token_prefers_active_hmac_store_over_stale_cache() {
        let thread_name = std::thread::current()
            .name()
            .unwrap_or("test")
            .chars()
            .map(|ch| {
                if ch.is_ascii_alphanumeric() || ch == '-' || ch == '_' {
                    ch
                } else {
                    '_'
                }
            })
            .collect::<String>();
        let root = std::env::temp_dir().join(format!(
            "rumi_dock_token_precedence_{}_{}",
            std::process::id(),
            thread_name
        ));
        let config = test_config(&root);
        fs::create_dir_all(&config.user_data_dir).unwrap();
        fs::write(config.desktop_api_token_path(), "stale-token").unwrap();
        fs::write(
            config.user_data_dir.join("hmac_keys.json"),
            r#"{"version":"1.0","keys":[{"key":"active-token","is_active":true}]}"#,
        )
        .unwrap();

        let token = read_desktop_api_token_from_config(&config).unwrap();

        assert_eq!(token, "active-token");
        assert_eq!(
            fs::read_to_string(config.desktop_api_token_path()).unwrap(),
            "active-token"
        );
        fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn config_token_uses_kernel_cache_only_for_encrypted_hmac_store() {
        let root =
            std::env::temp_dir().join(format!("rumi_dock_encrypted_token_{}", std::process::id()));
        let config = test_config(&root);
        fs::create_dir_all(&config.user_data_dir).unwrap();
        fs::write(config.desktop_api_token_path(), "kernel-cache-token").unwrap();
        fs::write(
            config.user_data_dir.join("hmac_keys.json"),
            r#"{"version":"1.0","encryption":"fernet","payload":"abc"}"#,
        )
        .unwrap();

        let token = read_desktop_api_token_from_config(&config).unwrap();

        assert_eq!(token, "kernel-cache-token");
        fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn stale_listener_identity_requires_defaultspack_and_working_directory() {
        let metadata = DefaultspackDesktopMetadata {
            entrypoint: PathBuf::from("/tmp/rumi/defaultspack/defaultspack/desktop_app.py"),
            argv: Vec::new(),
            app_working_dir: PathBuf::from("/tmp/rumi/defaultspack"),
            env_vars: vec![],
            port: DEFAULTSPACK_DEFAULT_PORT,
            execution_identity: test_execution_identity(),
            bootstrap_profile_digest: "sha256:".to_string() + &"a".repeat(64),
            catalog_revision: "sha256:test".into(),
            artifact_digest: "sha256:test".into(),
            function_id: "runtime.tauri.application.default".into(),
            provider_id: "runtime.tauri.application.default".into(),
            contract_namespace: "fixture.application".into(),
            application_id: "runtime.tauri.application.default".into(),
            base_pack_id: "fixture.base".into(),
            shell_provider_id: "fixture.shell".into(),
            shell_artifact_id: "fixture.shell.macos-arm64".into(),
            shell_artifact_digest: format!("sha256:{}", "b".repeat(64)),
            shell_entrypoint_digest: format!("sha256:{}", "c".repeat(64)),
            host_contract_contributions:
                crate::host_contract_contributions::HostContractContributionValues {
                    system_pack_descriptors: "[]".into(),
                    update_target_descriptors: "[]".into(),
                },
        };
        let owned = PortListener {
            pid: 101,
            command: "pack-shell run defaultspack --working-dir /tmp/rumi/defaultspack".into(),
            cwd: Some("/tmp/rumi/defaultspack".into()),
        };
        let foreign = PortListener {
            pid: 202,
            command: "python -m http.server 8766".into(),
            cwd: Some("/tmp/rumi/defaultspack".into()),
        };
        let managed_sealed = PortListener {
            pid: 303,
            command: "python -m tobkiri_sealed.bootstrap --role defaultspack".into(),
            cwd: Some("/private/tmp/.tobkiri-sealed-python-snapshot".into()),
        };

        assert!(identify_defaultspack_listener(&owned, &metadata));
        assert!(!identify_defaultspack_listener(&foreign, &metadata));
        assert!(!identify_defaultspack_listener(&managed_sealed, &metadata));
        assert!(listener_matches_launcher_owned_defaultspack(
            &managed_sealed,
            &metadata,
            true,
        ));
        assert!(!listener_matches_launcher_owned_defaultspack(
            &managed_sealed,
            &metadata,
            false,
        ));
        assert!(!listener_matches_launcher_owned_defaultspack(
            &foreign, &metadata, false,
        ));
    }

    #[test]
    fn authenticated_prior_bundle_listener_is_stale_but_foreign_server_is_not() {
        let metadata = DefaultspackDesktopMetadata {
            entrypoint: PathBuf::from(
                "/Applications/Tobkiri Launcher.app/Contents/Resources/app/ecosystem/defaultspack/defaultspack/desktop_app.py",
            ),
            argv: Vec::new(),
            app_working_dir: PathBuf::from(
                "/Applications/Tobkiri Launcher.app/Contents/Resources/app/ecosystem/defaultspack",
            ),
            env_vars: vec![],
            port: DEFAULTSPACK_DEFAULT_PORT,
            execution_identity: test_execution_identity(),
            bootstrap_profile_digest: "sha256:".to_string() + &"a".repeat(64),
            catalog_revision: "sha256:test".into(),
            artifact_digest: "sha256:test".into(),
            function_id: "runtime.tauri.application.default".into(),
            provider_id: "runtime.tauri.application.default".into(),
            contract_namespace: "fixture.application".into(),
            application_id: "runtime.tauri.application.default".into(),
            base_pack_id: "fixture.base".into(),
            shell_provider_id: "fixture.shell".into(),
            shell_artifact_id: "fixture.shell.macos-arm64".into(),
            shell_artifact_digest: format!("sha256:{}", "b".repeat(64)),
            shell_entrypoint_digest: format!("sha256:{}", "c".repeat(64)),
            host_contract_contributions:
                crate::host_contract_contributions::HostContractContributionValues {
                    system_pack_descriptors: "[]".into(),
                    update_target_descriptors: "[]".into(),
                },
        };
        let prior_bundle = PortListener {
            pid: 303,
            command: "python defaultspack/desktop_app.py".into(),
            cwd: Some(
                "/private/tmp/Tobkiri Launcher.previous.app/Contents/Resources/app/ecosystem/defaultspack"
                    .into(),
            ),
        };
        let foreign = PortListener {
            pid: 404,
            command: "python -m http.server 8766".into(),
            cwd: Some("/tmp".into()),
        };

        assert!(identify_authenticated_stale_defaultspack_listener(
            &prior_bundle,
            &metadata,
        ));
        assert!(!identify_authenticated_stale_defaultspack_listener(
            &foreign, &metadata,
        ));
    }

    #[test]
    fn viewer_broker_env_points_defaultspack_at_connection_file() {
        let root =
            std::env::temp_dir().join(format!("rumi_dock_broker_env_{}", std::process::id()));
        let config = test_config(&root);

        assert_eq!(
            viewer_host_broker_connection_env_key(),
            "RUMI_VIEWER_HOST_BROKER_CONNECTION"
        );
        assert_eq!(
            viewer_host_broker_connection_env_value(&config),
            root.join("app-data")
                .join("user_data")
                .join("host_broker")
                .join("connection.json")
        );
        fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn read_defaultspack_port_prefers_rumi_specific_port() {
        let port = read_defaultspack_port(&[
            ("DEFAULTS_HTTP_PORT".into(), "8766".into()),
            ("RUMI_DEFAULTSPACK_PORT".into(), "9876".into()),
        ])
        .unwrap();
        assert_eq!(port, 9876);
    }

    #[test]
    fn read_defaultspack_port_defaults_when_env_is_absent() {
        assert_eq!(
            read_defaultspack_port(&[]).unwrap(),
            DEFAULTSPACK_DEFAULT_PORT
        );
    }

    #[test]
    fn read_defaultspack_port_rejects_invalid_port() {
        let err =
            read_defaultspack_port(&[("DEFAULTS_HTTP_PORT".into(), "nope".into())]).unwrap_err();
        assert!(err.to_string().contains("DEFAULTS_HTTP_PORT"));
    }

    #[test]
    fn application_origin_targets_loopback_without_product_route() {
        assert_eq!(
            application_origin(DEFAULTSPACK_DEFAULT_PORT),
            "http://127.0.0.1:8766"
        );
    }

    #[test]
    fn application_url_with_bootstrap_code_uses_explicit_route() {
        assert_eq!(
            application_url_with_bootstrap_code(
                DEFAULTSPACK_DEFAULT_PORT,
                "/workspace",
                "one-time+code/1="
            )
            .unwrap(),
            "http://127.0.0.1:8766/workspace?code=one-time%2Bcode%2F1%3D"
        );
    }

    #[test]
    fn defaultspack_window_url_with_local_auth_keeps_auxiliary_fragment_contract() {
        assert_eq!(
            defaultspack_window_url_with_local_auth(DEFAULTSPACK_DEFAULT_PORT, "local+token/1=")
                .unwrap(),
            "http://127.0.0.1:8766/#rumi_local_auth=local%2Btoken%2F1%3D"
        );
    }

    #[test]
    fn defaultspack_window_url_with_path_preserves_local_auth_fragment() {
        assert_eq!(
            defaultspack_window_url_with_path(
                "http://127.0.0.1:8766/#rumi_local_auth=local-token",
                "/workspace?item=abc-123"
            )
            .unwrap(),
            "http://127.0.0.1:8766/workspace?item=abc-123#rumi_local_auth=local-token"
        );
    }

    #[test]
    fn defaultspack_window_url_with_path_rejects_external_url() {
        let err = defaultspack_window_url_with_path(
            "http://127.0.0.1:8766/#rumi_local_auth=local-token",
            "https://example.com/workspace",
        )
        .unwrap_err();
        assert!(err
            .to_string()
            .contains("Defaultspack window path must be a same-origin path"));
    }

    #[test]
    fn defaultspack_window_url_for_log_strips_query_and_fragment() {
        assert_eq!(
            defaultspack_window_url_for_log(
                "http://127.0.0.1:8766/workspace?item=abc&code=one-time-code#ignored"
            ),
            "http://127.0.0.1:8766/workspace"
        );
    }

    #[test]
    fn defaultspack_health_url_targets_authenticated_health_route() {
        assert_eq!(
            defaultspack_health_url(DEFAULTSPACK_DEFAULT_PORT),
            "http://127.0.0.1:8766/health"
        );
    }

    #[test]
    fn guardian_readiness_requires_authenticated_health_challenge() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0_u8; 4096];
            let length = stream.read(&mut request).unwrap();
            let request = String::from_utf8_lossy(&request[..length]);
            assert!(request.starts_with("GET /health "));
            assert!(!request.to_ascii_lowercase().contains("authorization:"));
            let challenge = request
                .lines()
                .find_map(|line| {
                    let (name, value) = line.split_once(':')?;
                    name.eq_ignore_ascii_case("X-Rumi-Desktop-Health-Challenge")
                        .then(|| value.trim())
                })
                .expect("health challenge header");
            let mut mac = Hmac::<Sha256>::new_from_slice(b"bootstrap-secret").unwrap();
            mac.update(challenge.as_bytes());
            let challenge_response = mac
                .finalize()
                .into_bytes()
                .iter()
                .map(|byte| format!("{byte:02x}"))
                .collect::<String>();
            let body = format!(
                r#"{{"success":true,"data":{{"panel_ready":true,"desktop_challenge_response":"{challenge_response}"}}}}"#
            );
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            );
            stream.write_all(response.as_bytes()).unwrap();
        });

        assert!(is_defaultspack_http_ready(port, "bootstrap-secret"));
        server.join().unwrap();
    }

    #[test]
    fn guardian_ready_audit_is_durable_and_profile_bound() {
        let root = std::env::temp_dir().join(format!(
            "tobkiri-guardian-ready-audit-{}",
            std::process::id()
        ));
        let config = test_config(&root);
        let metadata = DefaultspackDesktopMetadata {
            entrypoint: config
                .app_dir
                .join("ecosystem/defaultspack/defaultspack/desktop_app.py"),
            argv: Vec::new(),
            app_working_dir: config.app_dir.join("ecosystem/defaultspack"),
            env_vars: Vec::new(),
            port: DEFAULTSPACK_DEFAULT_PORT,
            execution_identity: test_execution_identity(),
            bootstrap_profile_digest: format!("sha256:{}", "1".repeat(64)),
            catalog_revision: format!("sha256:{}", "2".repeat(64)),
            artifact_digest: format!("sha256:{}", "3".repeat(64)),
            function_id: "runtime.tauri.application.default".into(),
            provider_id: "runtime.tauri.application.default".into(),
            contract_namespace: "fixture.application".into(),
            application_id: "runtime.tauri.application.default".into(),
            base_pack_id: "fixture.base".into(),
            shell_provider_id: "fixture.shell".into(),
            shell_artifact_id: "fixture.shell.macos-arm64".into(),
            shell_artifact_digest: format!("sha256:{}", "4".repeat(64)),
            shell_entrypoint_digest: format!("sha256:{}", "5".repeat(64)),
            host_contract_contributions:
                crate::host_contract_contributions::HostContractContributionValues {
                    system_pack_descriptors: "[]".into(),
                    update_target_descriptors: "[]".into(),
                },
        };

        write_guardian_ready_audit(&config, &metadata).unwrap();

        let audit = fs::read_to_string(config.host_broker_audit_log_path()).unwrap();
        assert!(audit.contains("launcher.defaultspack.guardian.prepare"));
        assert!(audit.contains("\"health\":\"ready\""));
        assert!(audit.contains("\"local_auth\":\"verified\""));
        assert!(audit.contains("\"guardian\":\"registered\""));
        assert!(audit.contains("\"profile_id\":\"profile-a\""));
        assert!(audit.contains("\"profile_revision\":\"sha256:aaaaaaaa"));
        assert!(audit.contains("\"activation_id\":\"activation:profile-a-test\""));
        assert!(audit.contains("\"plan_digest\":\"sha256:bbbbbbbb"));
        assert!(audit.contains("\"artifact_digest\":\"sha256:3333"));
        assert!(audit.contains("\"function_id\":\"runtime.tauri.application.default\""));
        assert!(audit.contains("\"provider_id\":\"runtime.tauri.application.default\""));
        fs::remove_dir_all(root).unwrap();
    }

    fn test_execution_identity() -> crate::host_contract::ExecutionProfileIdentity {
        crate::host_contract::ExecutionProfileIdentity::new(
            "profile-a",
            format!("sha256:{}", "a".repeat(64)),
            "activation:profile-a-test",
            format!("sha256:{}", "b".repeat(64)),
        )
        .unwrap()
    }
}
