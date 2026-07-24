//! Dock registration: generate a macOS .app bundle for defaultspack.

use std::ffi::OsString;
use std::fs;
#[cfg(unix)]
use std::os::unix::fs::PermissionsExt;
#[cfg(unix)]
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Stdio};
use std::sync::{Arc, Mutex, OnceLock};
use std::thread;
use std::time::{Duration, Instant};

use anyhow::{anyhow, bail, Context, Result as AnyResult};
use log::{error, info, warn};
use serde_json::Value;
use tauri::{AppHandle, Manager, Url, WebviewUrl, WebviewWindowBuilder};

use crate::config::AppConfig;
use crate::defaultspack_manager::DefaultspackManager;
use crate::kernel_manager::{
    detect_port_listener, python_path_with_runtime, terminate_external_listener, PortListener,
};
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
    command: String,
    app_working_dir: PathBuf,
    env_vars: Vec<(String, String)>,
    port: u16,
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

/// Read the `desktop_app.command` from the defaultspack ecosystem.json.
fn read_desktop_app_command(ecosystem_path: &Path) -> AnyResult<(String, Value)> {
    let raw = fs::read_to_string(ecosystem_path)
        .with_context(|| format!("failed to read {}", ecosystem_path.display()))?;
    let data: Value = serde_json::from_str(&raw)
        .with_context(|| format!("invalid JSON in {}", ecosystem_path.display()))?;

    let desktop_app = data
        .get("desktop_app")
        .context("ecosystem.json missing 'desktop_app' section")?;

    let command = desktop_app
        .get("command")
        .and_then(|v| v.as_str())
        .context("desktop_app.command is missing")?
        .to_string();

    Ok((command, desktop_app.clone()))
}

fn resolve_desktop_app_working_dir(desktop_app: &Value, pack_root: &Path) -> PathBuf {
    let working_dir = desktop_app
        .get("working_dir")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim();
    if working_dir.is_empty() {
        return pack_root.to_path_buf();
    }

    let path = PathBuf::from(working_dir);
    if path.is_absolute() {
        path
    } else {
        pack_root.join(path)
    }
}

fn is_valid_env_key(key: &str) -> bool {
    let mut chars = key.chars();
    match chars.next() {
        Some(first) if first == '_' || first.is_ascii_alphabetic() => {}
        _ => return false,
    }
    chars.all(|ch| ch == '_' || ch.is_ascii_alphanumeric())
}

fn read_desktop_app_env(desktop_app: &Value) -> AnyResult<Vec<(String, String)>> {
    let Some(env) = desktop_app.get("env") else {
        return Ok(Vec::new());
    };
    let env = env
        .as_object()
        .context("desktop_app.env must be an object")?;
    let mut entries = Vec::with_capacity(env.len());
    for (key, value) in env {
        if !is_valid_env_key(key) {
            bail!("desktop_app.env contains invalid shell variable name: {key}");
        }
        let value = value
            .as_str()
            .with_context(|| format!("desktop_app.env.{key} must be a string"))?;
        entries.push((key.clone(), value.to_string()));
    }
    entries.sort_by(|left, right| left.0.cmp(&right.0));
    Ok(entries)
}

fn shell_quote(value: &str) -> String {
    if value.is_empty() {
        return "''".to_string();
    }
    format!("'{}'", value.replace('\'', "'\\''"))
}

fn shell_quote_path(path: &Path) -> String {
    shell_quote(&path.to_string_lossy())
}

fn venv_bin_dir(venv_dir: &Path) -> PathBuf {
    if cfg!(target_os = "windows") {
        venv_dir.join("Scripts")
    } else {
        venv_dir.join("bin")
    }
}

fn kernel_command_for_python(python: &Path) -> String {
    format!("{} -m app", shell_quote_path(python))
}

fn defaultspack_window_url(port: u16) -> String {
    format!("http://127.0.0.1:{port}/chat")
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
    let mut url = Url::parse(&defaultspack_window_url(port))
        .with_context(|| format!("invalid defaultspack window port: {port}"))?;
    url.set_fragment(Some(&format!(
        "rumi_local_auth={}",
        encode_url_fragment_value(api_token)
    )));
    Ok(url.to_string())
}

fn defaultspack_window_url_with_path(authenticated_url: &str, path: &str) -> AnyResult<String> {
    let mut url = Url::parse(authenticated_url)
        .with_context(|| format!("invalid authenticated Defaultspack URL: {authenticated_url}"))?;
    let fragment = url.fragment().map(str::to_owned);
    let trimmed = path.trim();
    let path = if trimmed.is_empty() { "/chat" } else { trimmed };
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
            parsed.set_fragment(None);
            parsed.to_string()
        }
        Err(_) => "<invalid defaultspack url>".to_string(),
    }
}

fn defaultspack_health_url(port: u16) -> String {
    format!("http://127.0.0.1:{port}/api/health")
}

fn defaultspack_auth_probe_url(port: u16) -> String {
    format!("http://127.0.0.1:{port}/api/integrations/secrets")
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

fn check_defaultspack_health_ready(client: &reqwest::blocking::Client, port: u16) -> bool {
    client
        .get(defaultspack_health_url(port))
        .send()
        .is_ok_and(|response| response.status().is_success())
}

fn check_defaultspack_auth_ready(
    client: &reqwest::blocking::Client,
    port: u16,
    api_token: &str,
) -> bool {
    client
        .get(defaultspack_auth_probe_url(port))
        .bearer_auth(api_token)
        .send()
        .is_ok_and(|response| response.status().is_success())
}

fn check_defaultspack_http_ready(
    client: &reqwest::blocking::Client,
    port: u16,
    api_token: &str,
) -> bool {
    check_defaultspack_health_ready(client, port)
        && check_defaultspack_auth_ready(client, port, api_token)
}

fn defaultspack_health_client() -> AnyResult<reqwest::blocking::Client> {
    reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(1))
        .build()
        .context("failed to build defaultspack health client")
}

fn is_defaultspack_http_ready(port: u16, api_token: &str) -> bool {
    defaultspack_health_client()
        .map(|client| check_defaultspack_http_ready(&client, port, api_token))
        .unwrap_or(false)
}

fn wait_for_defaultspack_http_ready(
    port: u16,
    api_token: &str,
    manager: &DefaultspackManager,
) -> AnyResult<()> {
    let client = defaultspack_health_client()?;
    let deadline = Instant::now() + DEFAULTSPACK_READY_TIMEOUT;
    let mut poll_count: u32 = 0;

    loop {
        poll_count += 1;
        if check_defaultspack_http_ready(&client, port, api_token) {
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

        if poll_count % 20 == 0 {
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

fn xml_escape(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&apos;")
}

fn build_launch_script(
    pack_shell: &Path,
    token_file: &Path,
    panel_bootstrap_secret_file: &Path,
    rumi_home: &Path,
    app_dir: &Path,
    user_data_dir: &Path,
    log_dir: &Path,
    venv_dir: &Path,
    kernel_port: u16,
    app_working_dir: &Path,
    command: &str,
    env_vars: &[(String, String)],
) -> String {
    let env_exports = env_vars
        .iter()
        .map(|(key, value)| format!("export {key}={}", shell_quote(value)))
        .collect::<Vec<_>>()
        .join("\n");

    let env_exports = if env_exports.is_empty() {
        String::new()
    } else {
        format!("\n# Environment declared by defaultspack desktop_app metadata.\n{env_exports}\n")
    };

    let kernel_command = kernel_command_for_python(&venv_bin_dir(venv_dir).join("python3"));

    format!(
        r#"#!/bin/bash
set -euo pipefail

RUMI_HOME={rumi_home}
RUMI_APP_DIR={app_dir}
RUMI_USER_DATA={user_data_dir}
RUMI_DEFAULTSPACK_SECRETS_DIR={defaultspack_secrets_dir}
RUMI_DEFAULTSPACK_FRONTEND_SETTINGS_PATH={defaultspack_frontend_settings_path}
RUMI_LOG_DIR={log_dir}
VENV_DIR={venv_dir}
PACK_SHELL={pack_shell}
TOKEN_FILE={token_file}
PANEL_BOOTSTRAP_SECRET_FILE={panel_bootstrap_secret_file}
APP_WORKING_DIR={app_working_dir}
DESKTOP_COMMAND={command}
KERNEL_COMMAND={kernel_command}

export PATH="$VENV_DIR/bin:$PATH"
export PYTHONPATH="$RUMI_APP_DIR${{PYTHONPATH:+:$PYTHONPATH}}"
export RUMI_HOME
export RUMI_APP_DIR
export RUMI_USER_DATA
export RUMI_DEFAULTSPACK_SECRETS_DIR
export RUMI_DEFAULTSPACK_FRONTEND_SETTINGS_PATH
export RUMI_LOG_DIR
{env_exports}
# Desktop metadata is pack-owned and must not be able to re-enable bytecode
# writes inside the signed Launcher bundle.
export PYTHONDONTWRITEBYTECODE=1

RUMI_API_TOKEN=$(cat "$TOKEN_FILE" 2>/dev/null | tr -d '\n')
export RUMI_API_TOKEN
RUMI_DEFAULTSPACK_LOCAL_TOKEN="$RUMI_API_TOKEN"
export RUMI_DEFAULTSPACK_LOCAL_TOKEN
RUMI_PANEL_BOOTSTRAP_SECRET=$(cat "$PANEL_BOOTSTRAP_SECRET_FILE" 2>/dev/null | tr -d '\n')
export RUMI_PANEL_BOOTSTRAP_SECRET

exec "$PACK_SHELL" run "defaultspack" \
  --command "$DESKTOP_COMMAND" \
  --port {kernel_port} \
  --kernel-cmd "$KERNEL_COMMAND" \
  --working-dir "$APP_WORKING_DIR" \
  --timeout 120
"#,
        rumi_home = shell_quote_path(rumi_home),
        app_dir = shell_quote_path(app_dir),
        user_data_dir = shell_quote_path(user_data_dir),
        defaultspack_secrets_dir = shell_quote_path(&user_data_dir.join("secrets")),
        defaultspack_frontend_settings_path = shell_quote_path(
            &user_data_dir
                .join("defaultspack")
                .join("shared")
                .join("frontend_settings.json"),
        ),
        log_dir = shell_quote_path(log_dir),
        venv_dir = shell_quote_path(venv_dir),
        pack_shell = shell_quote_path(pack_shell),
        token_file = shell_quote_path(token_file),
        panel_bootstrap_secret_file = shell_quote_path(panel_bootstrap_secret_file),
        app_working_dir = shell_quote_path(app_working_dir),
        command = shell_quote(command),
        kernel_command = shell_quote(&kernel_command),
        kernel_port = kernel_port,
        env_exports = env_exports,
    )
}

fn is_legacy_defaultspack_app_bundle(app_dir: &Path) -> bool {
    if app_dir.file_name().and_then(|name| name.to_str()) != Some("Rumi_Defaultspack.app") {
        return false;
    }
    let plist = fs::read_to_string(app_dir.join("Contents").join("Info.plist")).unwrap_or_default();
    let launch = fs::read_to_string(app_dir.join("Contents").join("MacOS").join("launch"))
        .unwrap_or_default();
    let has_generated_legacy_plist = [
        "<key>CFBundleExecutable</key>",
        "<string>launch</string>",
        "<key>CFBundleIdentifier</key>",
        "<string>ai.rumi.pack.defaultspack</string>",
        "<key>CFBundleName</key>",
        "<string>Rumi Defaultspack</string>",
        "<key>CFBundlePackageType</key>",
        "<string>APPL</string>",
    ]
    .iter()
    .all(|marker| plist.contains(marker));
    let has_generated_legacy_launch = [
        "set -euo pipefail",
        "RUMI_HOME=",
        "RUMI_APP_DIR=",
        "RUMI_USER_DATA=",
        "VENV_DIR=",
        "PACK_SHELL=",
        "TOKEN_FILE=",
        "APP_WORKING_DIR=",
        "DESKTOP_COMMAND=",
        "KERNEL_COMMAND=",
        "export RUMI_API_TOKEN",
        "exec \"$PACK_SHELL\" run \"defaultspack\"",
        "--command \"$DESKTOP_COMMAND\"",
        "--port ",
        "--kernel-cmd \"$KERNEL_COMMAND\"",
        "--working-dir \"$APP_WORKING_DIR\"",
        "--timeout 120",
    ]
    .iter()
    .all(|marker| launch.contains(marker));
    let is_pre_diagnostic_generated_launch =
        !launch.contains("RUMI_LOG_DIR") && !launch.contains("RUMI_PANEL_BOOTSTRAP_SECRET");
    has_generated_legacy_plist && has_generated_legacy_launch && is_pre_diagnostic_generated_launch
}

fn cleanup_legacy_defaultspack_app_bundles(apps_base: &Path, current_bundle_dir: &Path) {
    let legacy_dir = apps_base.join("Rumi_Defaultspack.app");
    if legacy_dir == current_bundle_dir || !legacy_dir.exists() {
        return;
    }
    if !is_legacy_defaultspack_app_bundle(&legacy_dir) {
        warn!(
            "Skipping legacy Defaultspack bundle cleanup because {} does not look like a generated Rumi app",
            legacy_dir.display()
        );
        return;
    }
    match fs::remove_dir_all(&legacy_dir) {
        Ok(()) => warn!(
            "Removed legacy Defaultspack app bundle {}. The current bundle is {}",
            legacy_dir.display(),
            current_bundle_dir.display()
        ),
        Err(error) => warn!(
            "Failed to remove legacy Defaultspack app bundle {}: {error}",
            legacy_dir.display()
        ),
    }
}

/// Generate a macOS .app bundle at `~/Applications/Rumi Defaultspack.app`.
///
/// The generated .app launches defaultspack directly as a dedicated UI/launch
/// surface. macOS Computer Use permissions are hosted by Rumi Viewer.
fn create_macos_app_bundle(
    app_name: &str,
    pack_shell: &Path,
    token_file: &Path,
    panel_bootstrap_secret_file: &Path,
    rumi_home: &Path,
    app_dir: &Path,
    user_data_dir: &Path,
    log_dir: &Path,
    venv_dir: &Path,
    kernel_port: u16,
    app_working_dir: &Path,
    command: &str,
    env_vars: &[(String, String)],
) -> AnyResult<PathBuf> {
    let safe_name = app_name.replace('/', "_");
    let apps_base = dirs_home().join("Applications");
    fs::create_dir_all(&apps_base)
        .with_context(|| format!("failed to create {}", apps_base.display()))?;

    let bundle_dir = apps_base.join(format!("{safe_name}.app"));
    cleanup_legacy_defaultspack_app_bundles(&apps_base, &bundle_dir);
    let contents_dir = bundle_dir.join("Contents");
    let macos_dir = contents_dir.join("MacOS");
    fs::create_dir_all(&macos_dir)
        .with_context(|| format!("failed to create {}", macos_dir.display()))?;

    // Info.plist
    let bundle_id = "dev.rumiai.defaultspack";
    let plist_path = contents_dir.join("Info.plist");
    let escaped_app_name = xml_escape(app_name);
    let plist_content = format!(
        r#"<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>launch</string>
    <key>CFBundleIdentifier</key>
    <string>{bundle_id}</string>
    <key>CFBundleName</key>
    <string>{escaped_app_name}</string>
    <key>CFBundleDisplayName</key>
    <string>{escaped_app_name}</string>
    <key>CFBundleVersion</key>
    <string>1.0.0</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
</dict>
</plist>"#
    );
    fs::write(&plist_path, &plist_content)
        .with_context(|| format!("failed to write {}", plist_path.display()))?;

    // Launch script – runs pack-shell/defaultspack directly under this bundle.
    let launch_path = macos_dir.join("launch");
    let launch_script = build_launch_script(
        pack_shell,
        token_file,
        panel_bootstrap_secret_file,
        rumi_home,
        app_dir,
        user_data_dir,
        log_dir,
        venv_dir,
        kernel_port,
        app_working_dir,
        command,
        env_vars,
    );
    fs::write(&launch_path, &launch_script)
        .with_context(|| format!("failed to write {}", launch_path.display()))?;

    // Make executable on Unix platforms. Windows still compiles this module,
    // but does not support POSIX mode bits.
    #[cfg(unix)]
    {
        let mut perms = fs::metadata(&launch_path)?.permissions();
        perms.set_mode(0o755);
        fs::set_permissions(&launch_path, perms)?;
    }

    Ok(bundle_dir)
}

fn dirs_home() -> PathBuf {
    dirs::home_dir().unwrap_or_else(|| PathBuf::from("/tmp"))
}

/// Ad-hoc code-sign the .app bundle so macOS TCC can identify it.
fn codesign_app_bundle(app_dir: &Path) -> AnyResult<()> {
    let status = std::process::Command::new("/usr/bin/codesign")
        .args(["--force", "--deep", "-s", "-"])
        .arg(app_dir)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .with_context(|| "failed to run codesign")?;

    if status.success() {
        info!("Ad-hoc code-signed {}", app_dir.display());
    } else {
        info!("codesign exited with {} (non-fatal)", status);
    }
    Ok(())
}

/// Register the .app bundle with Launch Services so it appears in
/// Launchpad, Spotlight, and System Settings > Privacy & Security.
fn register_with_launch_services(app_dir: &Path) -> AnyResult<()> {
    let lsregister = PathBuf::from(
        "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister",
    );
    if !lsregister.exists() {
        info!("lsregister not found, skipping Launch Services registration");
        return Ok(());
    }

    let status = std::process::Command::new(&lsregister)
        .args(["-f", "-R"])
        .arg(app_dir)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .with_context(|| "failed to run lsregister")?;

    if status.success() {
        info!("Registered {} with Launch Services", app_dir.display());
    } else {
        info!("lsregister exited with {} (non-fatal)", status);
    }
    Ok(())
}

/// Tauri command: register defaultspack to the macOS Dock.
///
/// 1. Resolve pack-shell binary
/// 2. Read ecosystem.json → desktop_app.command
/// 3. Read HMAC key (plaintext only) → save to .desktop_api_token
/// 4. Generate ~/Applications/Rumi Defaultspack.app
#[tauri::command]
pub fn register_defaultspack_dock(config: tauri::State<'_, AppConfig>) -> Result<String, String> {
    register_defaultspack_dock_impl(&config).map_err(|e| {
        error!("register_defaultspack_dock failed: {e:#}");
        format!("{e:#}")
    })
}

pub(crate) fn register_defaultspack_dock_impl(config: &AppConfig) -> AnyResult<String> {
    let app_dir = ensure_defaultspack_app_bundle(config)?;

    info!("Dock registration complete: {}", app_dir.display());
    Ok(format!(
        "Registered 'Tobkiri' to Dock at {}",
        app_dir.display()
    ))
}

#[tauri::command]
pub fn launch_defaultspack_desktop(
    app: AppHandle,
    config: tauri::State<'_, AppConfig>,
) -> Result<String, String> {
    launch_defaultspack_desktop_window_impl(&app, config.inner()).map_err(|e| {
        error!("launch_defaultspack_desktop failed: {e:#}");
        format!("{e:#}")
    })
}

pub(crate) fn launch_defaultspack_desktop_window_impl(
    app: &AppHandle,
    config: &AppConfig,
) -> AnyResult<String> {
    with_defaultspack_launch_coordination(|| {
        info!("launch_defaultspack_desktop_impl: starting");
        let url = ensure_defaultspack_desktop_ready(app, config)?;
        open_defaultspack_tauri_window(app, &url)?;
        info!(
            "launch_defaultspack_desktop_impl: opened Tauri window {}",
            defaultspack_window_url_for_log(&url)
        );
        Ok("Tobkiriを開きました".into())
    })
}

pub(crate) fn open_defaultspack_desktop_window_path_impl(
    app: &AppHandle,
    config: &AppConfig,
    path: &str,
) -> AnyResult<String> {
    with_defaultspack_launch_coordination(|| {
        info!("open_defaultspack_desktop_window_path_impl: starting");
        let authenticated_url = ensure_defaultspack_desktop_ready(app, config)?;
        let url = defaultspack_window_url_with_path(&authenticated_url, path)?;
        open_defaultspack_tauri_window(app, &url)?;
        info!(
            "open_defaultspack_desktop_window_path_impl: opened Tauri window {}",
            defaultspack_window_url_for_log(&url)
        );
        Ok("Tobkiriを開きました".into())
    })
}

#[allow(dead_code)]
pub(crate) fn launch_defaultspack_desktop_impl(
    app: &AppHandle,
    config: &AppConfig,
) -> AnyResult<String> {
    let lock = DEFAULTSPACK_LAUNCH_LOCK.get_or_init(|| Mutex::new(()));
    let _guard = lock
        .lock()
        .map_err(|error| anyhow!("Defaultspack launch coordination lock was poisoned: {error}"))?;
    info!("launch_defaultspack_desktop_impl: starting legacy external launch");
    let allow_browser_debug = std::env::var("RUMI_DEFAULTSPACK_ALLOW_BROWSER_DEBUG")
        .ok()
        .as_deref()
        == Some("1");
    let requested_browser_surface = std::env::var("RUMI_DEFAULTSPACK_SURFACE")
        .ok()
        .is_some_and(|surface| surface.trim().eq_ignore_ascii_case("browser"));
    if !(allow_browser_debug && requested_browser_surface) {
        bail!(
            "external browser Defaultspack launch is disabled; use the Rumi Viewer window or set RUMI_DEFAULTSPACK_ALLOW_BROWSER_DEBUG=1 with RUMI_DEFAULTSPACK_SURFACE=browser for debug"
        );
    }
    let url = ensure_defaultspack_desktop_ready(app, config)?;
    open::that_detached(&url)
        .with_context(|| format!("failed to open {}", defaultspack_window_url_for_log(&url)))?;
    info!(
        "launch_defaultspack_desktop_impl: opened legacy external URL {}",
        defaultspack_window_url_for_log(&url)
    );
    Ok(format!(
        "Opening Tobkiri in debug browser at {}",
        defaultspack_window_url_for_log(&url)
    ))
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

fn recover_authenticated_stale_defaultspack_listener(
    metadata: &DefaultspackDesktopMetadata,
) -> AnyResult<bool> {
    let Some(listener) = detect_port_listener(metadata.port)? else {
        return Ok(false);
    };
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

fn ensure_defaultspack_desktop_ready(app: &AppHandle, config: &AppConfig) -> AnyResult<String> {
    let manager = app.state::<Arc<DefaultspackManager>>();
    let metadata = match read_defaultspack_desktop_metadata(config) {
        Ok(m) => {
            info!("launch_defaultspack_desktop_impl: metadata loaded (port={}, command={}, working_dir={})",
                m.port, m.command, m.app_working_dir.display());
            m
        }
        Err(e) => {
            error!("launch_defaultspack_desktop_impl: failed to read defaultspack metadata: {e:#}");
            info!(
                "launch_defaultspack_desktop_impl: ecosystem_json path={}",
                config.defaultspack_ecosystem_json().display()
            );
            return Err(e);
        }
    };
    let base_url = defaultspack_window_url(metadata.port);
    info!("launch_defaultspack_desktop_impl: Defaultspack window URL will be {base_url}");
    let api_token = read_desktop_api_token_from_config(config)
        .context("failed to read Viewer local auth token for Defaultspack launch")?;

    let managed_process = manager
        .has_managed_process()
        .context("failed to inspect managed Defaultspack process")?;
    let mut server_ready = is_defaultspack_http_ready(metadata.port, &api_token);
    if server_ready && recover_authenticated_stale_defaultspack_listener(&metadata)? {
        server_ready = false;
    }

    if server_ready {
        info!(
            "launch_defaultspack_desktop_impl: health and local auth checks passed, server already ready at {base_url}"
        );
    } else {
        if !managed_process
            && defaultspack_health_client()
                .map(|client| check_defaultspack_health_ready(&client, metadata.port))
                .unwrap_or(false)
        {
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
        match wait_for_defaultspack_http_ready(metadata.port, &api_token, manager.inner()) {
            Ok(()) => info!("launch_defaultspack_desktop_impl: server became ready at {base_url}"),
            Err(e) => {
                error!("launch_defaultspack_desktop_impl: wait_for_ready failed: {e:#}");
                return Err(e);
            }
        }
    }

    defaultspack_window_url_with_local_auth(metadata.port, &api_token)
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
    let ecosystem_path = config.defaultspack_ecosystem_json();
    if !ecosystem_path.exists() {
        bail!(
            "defaultspack ecosystem.json not found at {}",
            ecosystem_path.display()
        );
    }
    let (command, desktop_app) = read_desktop_app_command(&ecosystem_path)?;
    let pack_root = ecosystem_path
        .parent()
        .context("defaultspack ecosystem.json has no parent directory")?;
    let app_working_dir = resolve_desktop_app_working_dir(&desktop_app, pack_root);
    let env_vars = read_desktop_app_env(&desktop_app)?;
    let port = read_defaultspack_port(&env_vars)?;

    Ok(DefaultspackDesktopMetadata {
        command,
        app_working_dir,
        env_vars,
        port,
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
) -> AnyResult<Child> {
    let pack_shell = config
        .ensure_pack_shell_path()
        .context("pack-shell binary is required to launch Defaultspack")?;
    let api_token = read_desktop_api_token_from_config(config)?;
    let panel_bootstrap_secret = read_panel_bootstrap_secret_from_config(config)?;
    let kernel_command = kernel_command_for_python(&config.venv_python());
    let path = append_path_prefix(&venv_bin_dir(&config.venv_dir), std::env::var_os("PATH"))?;
    let python_path = python_path_with_runtime(&config.app_dir, std::env::var_os("PYTHONPATH"))?;

    info!(
        "spawn_defaultspack_local_server: pack_shell={}, port={}, kernel_cmd={}, working_dir={}",
        pack_shell.display(),
        config.kernel_port,
        kernel_command,
        metadata.app_working_dir.display(),
    );

    let mut command = process_utils::command(&pack_shell);
    command
        .arg("run")
        .arg("defaultspack")
        .arg("--command")
        .arg(&metadata.command)
        .arg("--port")
        .arg(config.kernel_port.to_string())
        .arg("--kernel-cmd")
        .arg(&kernel_command)
        .arg("--working-dir")
        .arg(&metadata.app_working_dir)
        .arg("--timeout")
        .arg("120")
        .env("PATH", path)
        .env("PYTHONPATH", python_path)
        .env("RUMI_HOME", &config.rumi_home)
        .env("RUMI_APP_DIR", &config.app_dir)
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
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .env("RUMI_API_TOKEN", &api_token)
        .env("RUMI_DEFAULTSPACK_LOCAL_TOKEN", &api_token)
        .env("RUMI_PANEL_BOOTSTRAP_SECRET", &panel_bootstrap_secret)
        .current_dir(&metadata.app_working_dir)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    for (key, value) in &metadata.env_vars {
        command.env(key, value);
    }
    command
        .env("RUMI_DEFAULTSPACK_OPEN_BROWSER", "0")
        .env("PYTHONDONTWRITEBYTECODE", "1");

    #[cfg(unix)]
    configure_defaultspack_process_group(&mut command);

    command
        .spawn()
        .with_context(|| format!("failed to spawn {}", pack_shell.display()))
}

fn ensure_defaultspack_app_bundle(config: &AppConfig) -> AnyResult<PathBuf> {
    if !cfg!(target_os = "macos") {
        bail!("Defaultspack dock registration is only supported on macOS");
    }

    let pack_shell = config
        .ensure_pack_shell_path()
        .context("pack-shell binary is required to register Defaultspack")?;

    let metadata = read_defaultspack_desktop_metadata(config)?;

    let api_token = read_desktop_api_token_from_config(config)?;
    let token_path = persist_desktop_api_token(config, &api_token)?;
    let panel_bootstrap_secret_path = config.panel_bootstrap_secret_path();

    let app_name = "Tobkiri";
    let app_dir = create_macos_app_bundle(
        app_name,
        &pack_shell,
        &token_path,
        &panel_bootstrap_secret_path,
        &config.rumi_home,
        &config.app_dir,
        &config.user_data_dir,
        &config.log_dir,
        &config.venv_dir,
        config.kernel_port,
        &metadata.app_working_dir,
        &metadata.command,
        &metadata.env_vars,
    )?;

    codesign_app_bundle(&app_dir)?;
    register_with_launch_services(&app_dir)?;

    Ok(app_dir)
}

#[cfg(test)]
mod tests {
    use super::*;

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

    fn write_generated_legacy_defaultspack_bundle(app_dir: &Path) {
        let macos_dir = app_dir.join("Contents").join("MacOS");
        fs::create_dir_all(&macos_dir).unwrap();
        fs::write(
            app_dir.join("Contents").join("Info.plist"),
            r#"<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>launch</string>
    <key>CFBundleIdentifier</key>
    <string>ai.rumi.pack.defaultspack</string>
    <key>CFBundleName</key>
    <string>Rumi Defaultspack</string>
    <key>CFBundleVersion</key>
    <string>1.0.0</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
</dict>
</plist>"#,
        )
        .unwrap();
        fs::write(
            macos_dir.join("launch"),
            r#"#!/bin/bash
set -euo pipefail

RUMI_HOME='/tmp/rumi-home'
RUMI_APP_DIR='/tmp/app-dir'
RUMI_USER_DATA='/tmp/user-data'
VENV_DIR='/tmp/venv'
PACK_SHELL='/Applications/Rumi AI.app/Contents/Resources/app/bundled/pack-shell'
TOKEN_FILE='/tmp/token'
APP_WORKING_DIR='/tmp/defaultspack'
DESKTOP_COMMAND='python -m defaultspack.desktop_app'
KERNEL_COMMAND='python3 -m app'

export PATH="$VENV_DIR/bin:$PATH"
export RUMI_HOME
export RUMI_APP_DIR
export RUMI_USER_DATA

RUMI_API_TOKEN=$(cat "$TOKEN_FILE" 2>/dev/null | tr -d '\n')
export RUMI_API_TOKEN

exec "$PACK_SHELL" run "defaultspack" \
  --command "$DESKTOP_COMMAND" \
  --port 8765 \
  --kernel-cmd "$KERNEL_COMMAND" \
  --working-dir "$APP_WORKING_DIR" \
  --timeout 120
"#,
        )
        .unwrap();
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
            command: "python -m ecosystem.defaultspack.desktop_app".into(),
            app_working_dir: PathBuf::from("/tmp/rumi/defaultspack"),
            env_vars: vec![],
            port: DEFAULTSPACK_DEFAULT_PORT,
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

        assert!(identify_defaultspack_listener(&owned, &metadata));
        assert!(!identify_defaultspack_listener(&foreign, &metadata));
    }

    #[test]
    fn authenticated_prior_bundle_listener_is_stale_but_foreign_server_is_not() {
        let metadata = DefaultspackDesktopMetadata {
            command: "python defaultspack/desktop_app.py".into(),
            app_working_dir: PathBuf::from(
                "/Applications/Tobkiri Launcher.app/Contents/Resources/app/ecosystem/defaultspack",
            ),
            env_vars: vec![],
            port: DEFAULTSPACK_DEFAULT_PORT,
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
    #[cfg(unix)]
    fn launch_script_sets_rumi_app_dir_and_user_data() {
        let script = build_launch_script(
            Path::new("/tmp/Rumi's bin/pack-shell"),
            Path::new("/tmp/token file"),
            Path::new("/tmp/panel secret file"),
            Path::new("/tmp/rumi home"),
            Path::new("/tmp/app dir"),
            Path::new("/tmp/user data"),
            Path::new("/tmp/log dir"),
            Path::new("/tmp/venv dir"),
            8767,
            Path::new("/tmp/work $(bad)"),
            "python -c \"print('hello')\"",
            &[("RUMI_DEFAULTSPACK_SURFACE".into(), "webview".into())],
        );

        assert!(script.contains("PACK_SHELL='/tmp/Rumi'\\''s bin/pack-shell'"));
        assert!(script.contains("RUMI_APP_DIR='/tmp/app dir'"));
        assert!(script.contains("export PYTHONPATH=\"$RUMI_APP_DIR${PYTHONPATH:+:$PYTHONPATH}\""));
        assert!(script.contains("RUMI_USER_DATA='/tmp/user data'"));
        assert!(script.contains("RUMI_LOG_DIR='/tmp/log dir'"));
        assert!(script.contains("TOKEN_FILE='/tmp/token file'"));
        assert!(script.contains("PANEL_BOOTSTRAP_SECRET_FILE='/tmp/panel secret file'"));
        assert!(script.contains("APP_WORKING_DIR='/tmp/work $(bad)'"));
        assert!(script.contains("DESKTOP_COMMAND='python -c \"print('\\''hello'\\'')\"'"));
        assert!(script.contains("KERNEL_COMMAND=''\\''/tmp/venv dir/bin/python3'\\'' -m app'"));
        assert!(script.contains("exec \"$PACK_SHELL\" run \"defaultspack\""));
        assert!(!script.contains("--api-token"));
        assert!(script.contains("export RUMI_DEFAULTSPACK_SURFACE='webview'"));
        assert!(
            script.rfind("export PYTHONDONTWRITEBYTECODE=1").unwrap()
                > script
                    .rfind("export RUMI_DEFAULTSPACK_SURFACE='webview'")
                    .unwrap()
        );
        assert!(script.contains("export RUMI_DEFAULTSPACK_LOCAL_TOKEN"));
        assert!(!script.contains(".defaultspack_launch_request"));
        assert!(!script.contains("open -a \"Rumi AI\""));
    }

    #[test]
    #[cfg(unix)]
    fn launch_script_includes_env_exports() {
        let script = build_launch_script(
            Path::new("/tmp/pack-shell"),
            Path::new("/tmp/token"),
            Path::new("/tmp/panel-secret"),
            Path::new("/tmp/rumi-home"),
            Path::new("/tmp/app-dir"),
            Path::new("/tmp/user-data"),
            Path::new("/tmp/log-dir"),
            Path::new("/tmp/venv"),
            8765,
            Path::new("/tmp/defaultspack"),
            "python -m defaultspack.desktop_app",
            &[
                ("RUMI_DEFAULTSPACK_SURFACE".into(), "webview".into()),
                ("DEFAULTS_HTTP_PORT".into(), "8766".into()),
            ],
        );

        assert!(script.contains("export RUMI_DEFAULTSPACK_SURFACE='webview'"));
        assert!(script.contains("export DEFAULTS_HTTP_PORT='8766'"));
        assert!(script.contains("export RUMI_DEFAULTSPACK_LOCAL_TOKEN"));
        assert!(script.contains("export RUMI_PANEL_BOOTSTRAP_SECRET"));
    }

    #[test]
    #[cfg(unix)]
    fn launch_script_uses_direct_defaultspack_identity_only() {
        let script = build_launch_script(
            Path::new("/tmp/pack-shell"),
            Path::new("/tmp/token"),
            Path::new("/tmp/panel-secret"),
            Path::new("/tmp/rumi-home"),
            Path::new("/tmp/app-dir"),
            Path::new("/tmp/user-data"),
            Path::new("/tmp/log-dir"),
            Path::new("/tmp/venv"),
            8765,
            Path::new("/tmp/defaultspack"),
            "python -m defaultspack.desktop_app",
            &[],
        );

        assert!(script.contains("exec \"$PACK_SHELL\" run \"defaultspack\""));
        assert!(script.contains("--working-dir \"$APP_WORKING_DIR\""));
        assert!(script.contains("--kernel-cmd \"$KERNEL_COMMAND\""));
        assert!(!script.contains("--api-token"));

        assert!(!script.contains("SIGNAL_FILE"));
        assert!(!script.contains("Rumi AI"));
        assert!(!script.contains("defaultspack_launch_request"));
    }

    #[test]
    fn xml_escape_escapes_plist_values() {
        assert_eq!(
            xml_escape("Rumi & <Default> \"Pack\""),
            "Rumi &amp; &lt;Default&gt; &quot;Pack&quot;"
        );
    }

    #[test]
    fn legacy_defaultspack_bundle_detection_requires_generated_app_shape() {
        let dir = std::env::temp_dir().join("rumi_dock_test_legacy_detection");
        let app_dir = dir.join("Rumi_Defaultspack.app");
        fs::remove_dir_all(&dir).ok();
        write_generated_legacy_defaultspack_bundle(&app_dir);

        assert!(is_legacy_defaultspack_app_bundle(&app_dir));
        assert!(!is_legacy_defaultspack_app_bundle(&dir.join("Other.app")));
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn legacy_defaultspack_bundle_detection_rejects_manual_same_name_app() {
        let dir = std::env::temp_dir().join("rumi_dock_test_manual_legacy_reject");
        let app_dir = dir.join("Rumi_Defaultspack.app");
        let macos_dir = app_dir.join("Contents").join("MacOS");
        fs::remove_dir_all(&dir).ok();
        fs::create_dir_all(&macos_dir).unwrap();
        fs::write(
            app_dir.join("Contents").join("Info.plist"),
            "<string>Manual Defaultspack Launcher</string>",
        )
        .unwrap();
        fs::write(
            macos_dir.join("launch"),
            r#"#!/bin/bash
echo "manual defaultspack helper"
"#,
        )
        .unwrap();

        assert!(!is_legacy_defaultspack_app_bundle(&app_dir));
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn legacy_defaultspack_bundle_detection_rejects_old_id_without_generated_shape() {
        let dir = std::env::temp_dir().join("rumi_dock_test_legacy_partial_reject");
        let app_dir = dir.join("Rumi_Defaultspack.app");
        let macos_dir = app_dir.join("Contents").join("MacOS");
        fs::remove_dir_all(&dir).ok();
        fs::create_dir_all(&macos_dir).unwrap();
        fs::write(
            app_dir.join("Contents").join("Info.plist"),
            r#"<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>launch</string>
    <key>CFBundleIdentifier</key>
    <string>ai.rumi.pack.defaultspack</string>
    <key>CFBundleName</key>
    <string>Manual Defaultspack Launcher</string>
</dict>
</plist>"#,
        )
        .unwrap();
        fs::write(
            macos_dir.join("launch"),
            r#"#!/bin/bash
exec "/Applications/Rumi AI.app/Contents/Resources/app/bundled/pack-shell" run "defaultspack"
"#,
        )
        .unwrap();

        assert!(!is_legacy_defaultspack_app_bundle(&app_dir));
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn cleanup_legacy_defaultspack_bundle_removes_only_old_underscore_bundle() {
        let dir = std::env::temp_dir().join("rumi_dock_test_legacy_cleanup");
        let legacy_dir = dir.join("Rumi_Defaultspack.app");
        let current_dir = dir.join("Rumi Defaultspack.app");
        fs::remove_dir_all(&dir).ok();
        fs::create_dir_all(&current_dir).unwrap();
        write_generated_legacy_defaultspack_bundle(&legacy_dir);

        cleanup_legacy_defaultspack_app_bundles(&dir, &current_dir);

        assert!(!legacy_dir.exists());
        assert!(current_dir.exists());
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn cleanup_legacy_defaultspack_bundle_keeps_manual_same_name_app() {
        let dir = std::env::temp_dir().join("rumi_dock_test_manual_legacy_cleanup");
        let legacy_dir = dir.join("Rumi_Defaultspack.app");
        let current_dir = dir.join("Rumi Defaultspack.app");
        let macos_dir = legacy_dir.join("Contents").join("MacOS");
        fs::remove_dir_all(&dir).ok();
        fs::create_dir_all(&macos_dir).unwrap();
        fs::create_dir_all(&current_dir).unwrap();
        fs::write(
            legacy_dir.join("Contents").join("Info.plist"),
            "<string>Manual Defaultspack Launcher</string>",
        )
        .unwrap();
        fs::write(
            macos_dir.join("launch"),
            r#"#!/bin/bash
echo "manual defaultspack helper"
"#,
        )
        .unwrap();

        cleanup_legacy_defaultspack_app_bundles(&dir, &current_dir);

        assert!(legacy_dir.exists());
        assert!(current_dir.exists());
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn read_desktop_app_command_parses_ecosystem() {
        let dir = std::env::temp_dir().join("rumi_dock_test_eco");
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("ecosystem.json");
        fs::write(
            &path,
            r#"{"pack_id":"defaultspack","desktop_app":{"command":"python app.py"}}"#,
        )
        .unwrap();
        let (cmd, _) = read_desktop_app_command(&path).unwrap();
        assert_eq!(cmd, "python app.py");
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn read_desktop_app_env_sorts_and_validates_env_vars() {
        let desktop_app: Value = serde_json::from_str(
            r#"{"env":{"RUMI_DEFAULTSPACK_SURFACE":"webview","DEFAULTS_HTTP_PORT":"8766"}}"#,
        )
        .unwrap();

        let env_vars = read_desktop_app_env(&desktop_app).unwrap();
        assert_eq!(
            env_vars,
            vec![
                ("DEFAULTS_HTTP_PORT".into(), "8766".into()),
                ("RUMI_DEFAULTSPACK_SURFACE".into(), "webview".into()),
            ]
        );
    }

    #[test]
    fn read_desktop_app_env_rejects_invalid_shell_names() {
        let desktop_app: Value = serde_json::from_str(r#"{"env":{"BAD;NAME":"oops"}}"#).unwrap();
        let err = read_desktop_app_env(&desktop_app).unwrap_err();
        assert!(err.to_string().contains("invalid shell variable name"));
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
    fn defaultspack_window_url_targets_loopback_chat_route() {
        assert_eq!(
            defaultspack_window_url(DEFAULTSPACK_DEFAULT_PORT),
            "http://127.0.0.1:8766/chat"
        );
    }

    #[test]
    fn defaultspack_window_url_with_local_auth_uses_fragment() {
        assert_eq!(
            defaultspack_window_url_with_local_auth(DEFAULTSPACK_DEFAULT_PORT, "local+token/1=")
                .unwrap(),
            "http://127.0.0.1:8766/chat#rumi_local_auth=local%2Btoken%2F1%3D"
        );
    }

    #[test]
    fn defaultspack_window_url_with_path_preserves_local_auth_fragment() {
        assert_eq!(
            defaultspack_window_url_with_path(
                "http://127.0.0.1:8766/chat#rumi_local_auth=local-token",
                "/chat?chat=abc-123"
            )
            .unwrap(),
            "http://127.0.0.1:8766/chat?chat=abc-123#rumi_local_auth=local-token"
        );
    }

    #[test]
    fn defaultspack_window_url_with_path_rejects_external_url() {
        let err = defaultspack_window_url_with_path(
            "http://127.0.0.1:8766/chat#rumi_local_auth=local-token",
            "https://example.com/chat",
        )
        .unwrap_err();
        assert!(err
            .to_string()
            .contains("Defaultspack window path must be a same-origin path"));
    }

    #[test]
    fn defaultspack_window_url_for_log_strips_local_auth_fragment() {
        assert_eq!(
            defaultspack_window_url_for_log(
                "http://127.0.0.1:8766/chat#rumi_local_auth=local%2Btoken%2F1%3D"
            ),
            "http://127.0.0.1:8766/chat"
        );
    }

    #[test]
    fn defaultspack_auth_probe_url_targets_sensitive_read_route() {
        assert_eq!(
            defaultspack_auth_probe_url(DEFAULTSPACK_DEFAULT_PORT),
            "http://127.0.0.1:8766/api/integrations/secrets"
        );
    }

    #[test]
    fn resolve_desktop_app_working_dir_defaults_to_pack_root() {
        let pack_root = PathBuf::from("/tmp/defaultspack");
        let desktop_app: Value = serde_json::from_str(r#"{"working_dir":""}"#).unwrap();
        assert_eq!(
            resolve_desktop_app_working_dir(&desktop_app, &pack_root),
            pack_root
        );
    }

    #[test]
    fn resolve_desktop_app_working_dir_joins_relative_path() {
        let pack_root = PathBuf::from("/tmp/defaultspack");
        let desktop_app: Value = serde_json::from_str(r#"{"working_dir":"apps"}"#).unwrap();
        assert_eq!(
            resolve_desktop_app_working_dir(&desktop_app, &pack_root),
            pack_root.join("apps")
        );
    }
}
