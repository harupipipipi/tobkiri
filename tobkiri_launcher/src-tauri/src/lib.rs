//! Rumi Viewer — Tauri application library.
//!
//! V2: Full implementation with setup hook, commands, tray menu, and navigation guard.

mod app_data_migration;
mod config;
mod defaultspack_manager;
mod desktop_system_info;
mod health_check;
mod host_audit;
mod host_broker;
mod host_broker_types;
mod kernel_manager;
mod process_utils;
mod python_env;
mod tray;
mod updater;

use std::io::Write;
use std::net::TcpListener;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use std::{fs, io};

use anyhow::{anyhow, bail, Context, Result as AnyResult};
use hmac::{Hmac, Mac};
use log::{error, info, warn};
use rand::{distributions::Alphanumeric, Rng};
use serde::{Deserialize, Serialize};
use sha2::Sha256;
use tauri::{AppHandle, Emitter, Manager, Url};

use config::AppConfig;
use defaultspack_manager::DefaultspackManager;
use host_broker::HostBrokerRuntime;
use kernel_manager::KernelManager;

mod dock_registration;

/// Wrapper around a shared progress string, managed as Tauri State.
pub struct SetupProgress(pub Arc<Mutex<String>>);
pub struct ShutdownState(pub Arc<AtomicBool>);
pub struct AllowedNavigationPorts(pub Arc<Mutex<Vec<u16>>>);

const PRIMARY_WINDOW_LABELS: [&str; 2] = ["panel", "main"];
const DEFAULTSPACK_RESERVED_PORT: u16 = 8766;
const AUTHORITY_APPROVAL_WINDOW_LABEL: &str = "authority-approval";
const AUTHORITY_APPROVAL_WINDOW_TITLE: &str = "Rumiの許可";
const AMBIENT_TRIGGER_WINDOW_LABEL: &str = "ambient-trigger";
const AMBIENT_TRIGGER_WINDOW_TITLE: &str = "合図待ち";
const FINGER_RECORDING_WINDOW_LABEL: &str = "finger-recording";
const FINGER_RECORDING_WINDOW_TITLE: &str = "指で録音";
const DEFAULTS_CONSOLE_WINDOW_LABEL: &str = "defaults-console";
const DEFAULTS_CONSOLE_WINDOW_TITLE: &str = "詳細ログ";
const HOST_PERMISSIONS_WINDOW_LABEL: &str = "host-permissions";
const HOST_PERMISSIONS_WINDOW_TITLE: &str = "Rumi Host Permissions";
const AUTHORITY_UI_OPERATOR_TTL_SECONDS: u64 = 180;

type HmacSha256 = Hmac<Sha256>;

fn bundled_resource_dir_fallback() -> Option<PathBuf> {
    let executable = std::env::current_exe().ok()?;
    let resources = executable.parent()?.parent()?.join("Resources");
    resources.is_dir().then_some(resources)
}

#[derive(Debug, Deserialize)]
struct PanelBootstrapPayload {
    code: String,
}

#[derive(Debug, Deserialize)]
struct ApiEnvelope<T> {
    success: bool,
    data: Option<T>,
    error: Option<String>,
}

#[derive(Debug, Deserialize)]
struct TauriConfigEnv {
    build: Option<TauriBuildConfigEnv>,
}

#[derive(Debug, Deserialize)]
struct TauriBuildConfigEnv {
    #[serde(rename = "devUrl")]
    dev_url: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
struct WindowRuntimeSnapshot {
    label: String,
    visible: bool,
    minimized: bool,
    focused: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
struct BackgroundControlStatus {
    enabled: bool,
    app_visible: bool,
    foreground_window: Option<String>,
    kernel_running: bool,
    shutdown_requested: bool,
    windows: Vec<WindowRuntimeSnapshot>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
struct AuthorityUiOperator {
    version: u8,
    kind: String,
    origin: String,
    window_label: String,
    request_id: String,
    issued_at: u64,
    expires_at: u64,
    nonce: String,
    signature: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
struct AuthorityApprovalContext {
    request_id: String,
    ui_operator: AuthorityUiOperator,
}

/// Returns the current setup progress message.
#[tauri::command]
fn get_setup_progress(state: tauri::State<'_, SetupProgress>) -> String {
    match state.0.lock() {
        Ok(progress) => progress.clone(),
        Err(error) => {
            error!("Setup progress lock poisoned: {error}");
            "Setup status unavailable".to_string()
        }
    }
}

/// Restart the Kernel process.
#[tauri::command]
fn restart_kernel(state: tauri::State<'_, Arc<Mutex<KernelManager>>>) -> Result<String, String> {
    let mut km = state.lock().map_err(|e| format!("lock error: {e}"))?;
    km.restart().map_err(|e| format!("restart error: {e}"))?;
    Ok("Kernel restarted".into())
}

#[tauri::command]
fn reauthorize_panel_session(
    config: tauri::State<'_, AppConfig>,
    km: tauri::State<'_, Arc<Mutex<KernelManager>>>,
) -> Result<String, String> {
    request_fresh_panel_session_code(&config, km.inner())
        .map_err(|error| format!("panel reauthorization failed: {error}"))
}

#[tauri::command]
fn open_external_url(url: String) -> Result<(), String> {
    if !(url.starts_with("https://") || url.starts_with("http://")) {
        return Err("only http(s) URLs can be opened externally".into());
    }

    open::that_detached(url).map_err(|error| format!("failed to open external url: {error}"))
}

#[tauri::command]
fn close_current_window(window: tauri::WebviewWindow) -> Result<(), String> {
    if should_send_to_background_on_close(window.label()) {
        return Err("primary windows are sent to the background instead of closed".into());
    }
    window
        .close()
        .map_err(|error| format!("failed to close current window: {error}"))
}

fn valid_authority_request_id(request_id: &str) -> bool {
    let trimmed = request_id.trim();
    !trimmed.is_empty()
        && trimmed.len() <= 160
        && trimmed
            .chars()
            .all(|character| character.is_ascii_alphanumeric() || matches!(character, '_' | '-'))
}

fn authority_approval_url(request_id: &str) -> Result<Url, String> {
    if !valid_authority_request_id(request_id) {
        return Err("invalid authority request id".into());
    }
    Url::parse_with_params(
        &format!("http://127.0.0.1:{DEFAULTSPACK_RESERVED_PORT}/approval"),
        &[("request_id", request_id.trim())],
    )
    .map_err(|error| format!("failed to build approval window URL: {error}"))
}

fn ambient_trigger_url() -> Result<Url, String> {
    Url::parse(&format!(
        "http://127.0.0.1:{DEFAULTSPACK_RESERVED_PORT}/ambient"
    ))
    .map_err(|error| format!("failed to build ambient trigger window URL: {error}"))
}

fn finger_recording_url() -> Result<Url, String> {
    Url::parse(&format!(
        "http://127.0.0.1:{DEFAULTSPACK_RESERVED_PORT}/finger-recording"
    ))
    .map_err(|error| format!("failed to build finger recording window URL: {error}"))
}

fn defaults_console_url() -> Result<Url, String> {
    Url::parse(&format!(
        "http://127.0.0.1:{DEFAULTSPACK_RESERVED_PORT}/console"
    ))
    .map_err(|error| format!("failed to build defaults console window URL: {error}"))
}

fn host_permissions_url() -> Result<Url, String> {
    Url::parse(&format!(
        "http://127.0.0.1:{DEFAULTSPACK_RESERVED_PORT}/host-permissions"
    ))
    .map_err(|error| format!("failed to build host permissions window URL: {error}"))
}

fn authenticated_defaultspack_window_url(
    config: &AppConfig,
    url: Result<Url, String>,
) -> Result<Url, String> {
    let url = url?;
    dock_registration::add_defaultspack_local_auth(config, url)
        .map_err(|error| format!("failed to authenticate Defaultspack window URL: {error:#}"))
}

fn focus_authority_approval_window(window: &tauri::WebviewWindow) -> Result<(), String> {
    window
        .unminimize()
        .map_err(|error| format!("failed to unminimize approval window: {error}"))?;
    window
        .show()
        .map_err(|error| format!("failed to show approval window: {error}"))?;
    window
        .set_always_on_top(true)
        .map_err(|error| format!("failed to bring approval window forward: {error}"))?;
    window
        .set_focus()
        .map_err(|error| format!("failed to focus approval window: {error}"))
}

fn open_authority_approval_window_for_app(
    app: &AppHandle,
    config: &AppConfig,
    request_id: &str,
) -> Result<(), String> {
    let request_id = request_id.trim().to_string();
    let approval_url =
        authenticated_defaultspack_window_url(config, authority_approval_url(&request_id))?;
    if let Some(window) = app.get_webview_window(AUTHORITY_APPROVAL_WINDOW_LABEL) {
        window
            .navigate(approval_url)
            .map_err(|error| format!("failed to navigate approval window: {error}"))?;
        return focus_authority_approval_window(&window);
    }

    let window = tauri::WebviewWindowBuilder::new(
        app,
        AUTHORITY_APPROVAL_WINDOW_LABEL,
        tauri::WebviewUrl::External(approval_url),
    )
    .title(AUTHORITY_APPROVAL_WINDOW_TITLE)
    .inner_size(520.0, 620.0)
    .min_inner_size(480.0, 560.0)
    .resizable(true)
    .focused(true)
    .visible(true)
    .always_on_top(true)
    .build()
    .map_err(|error| format!("failed to open approval window: {error}"))?;
    focus_authority_approval_window(&window)
}

#[tauri::command]
async fn open_authority_approval_window(
    app: AppHandle,
    config: tauri::State<'_, AppConfig>,
    request_id: String,
) -> Result<(), String> {
    open_authority_approval_window_for_app(&app, config.inner(), &request_id)
}

fn focus_ambient_trigger_window(window: &tauri::WebviewWindow) -> Result<(), String> {
    window
        .unminimize()
        .map_err(|error| format!("failed to unminimize ambient trigger window: {error}"))?;
    window
        .show()
        .map_err(|error| format!("failed to show ambient trigger window: {error}"))?;
    window
        .set_always_on_top(true)
        .map_err(|error| format!("failed to float ambient trigger window: {error}"))?;
    window
        .set_focus()
        .map_err(|error| format!("failed to focus ambient trigger window: {error}"))
}

fn open_ambient_trigger_window_for_app(app: &AppHandle, config: &AppConfig) -> Result<(), String> {
    let ambient_url = authenticated_defaultspack_window_url(config, ambient_trigger_url())?;
    if let Some(window) = app.get_webview_window(AMBIENT_TRIGGER_WINDOW_LABEL) {
        window
            .navigate(ambient_url)
            .map_err(|error| format!("failed to navigate ambient trigger window: {error}"))?;
        return focus_ambient_trigger_window(&window);
    }

    let window = tauri::WebviewWindowBuilder::new(
        app,
        AMBIENT_TRIGGER_WINDOW_LABEL,
        tauri::WebviewUrl::External(ambient_url),
    )
    .title(AMBIENT_TRIGGER_WINDOW_TITLE)
    .inner_size(360.0, 240.0)
    .min_inner_size(320.0, 180.0)
    .resizable(true)
    .focused(true)
    .visible(true)
    .always_on_top(true)
    .build()
    .map_err(|error| format!("failed to open ambient trigger window: {error}"))?;
    focus_ambient_trigger_window(&window)
}

#[tauri::command]
async fn open_ambient_trigger_window(
    app: AppHandle,
    config: tauri::State<'_, AppConfig>,
) -> Result<(), String> {
    open_ambient_trigger_window_for_app(&app, config.inner())
}

fn focus_floating_window(window: &tauri::WebviewWindow, label: &str) -> Result<(), String> {
    window
        .unminimize()
        .map_err(|error| format!("failed to unminimize {label} window: {error}"))?;
    window
        .show()
        .map_err(|error| format!("failed to show {label} window: {error}"))?;
    window
        .set_always_on_top(true)
        .map_err(|error| format!("failed to float {label} window: {error}"))?;
    window
        .set_focus()
        .map_err(|error| format!("failed to focus {label} window: {error}"))
}

fn open_small_defaultspack_window_for_app(
    app: &AppHandle,
    label: &'static str,
    title: &str,
    url: Url,
    width: f64,
    height: f64,
) -> Result<(), String> {
    if let Some(window) = app.get_webview_window(label) {
        window
            .navigate(url)
            .map_err(|error| format!("failed to navigate {label} window: {error}"))?;
        return focus_floating_window(&window, label);
    }

    let window = tauri::WebviewWindowBuilder::new(app, label, tauri::WebviewUrl::External(url))
        .title(title)
        .inner_size(width, height)
        .min_inner_size(360.0, 420.0)
        .resizable(true)
        .focused(true)
        .visible(true)
        .always_on_top(true)
        .build()
        .map_err(|error| format!("failed to open {label} window: {error}"))?;
    focus_floating_window(&window, label)
}

fn open_finger_recording_window_for_app(app: &AppHandle, config: &AppConfig) -> Result<(), String> {
    open_small_defaultspack_window_for_app(
        app,
        FINGER_RECORDING_WINDOW_LABEL,
        FINGER_RECORDING_WINDOW_TITLE,
        authenticated_defaultspack_window_url(config, finger_recording_url())?,
        380.0,
        460.0,
    )
}

#[tauri::command]
async fn open_finger_recording_window(
    app: AppHandle,
    config: tauri::State<'_, AppConfig>,
) -> Result<(), String> {
    open_finger_recording_window_for_app(&app, config.inner())
}

#[tauri::command]
async fn open_defaultspack_main_window(
    app: AppHandle,
    config: tauri::State<'_, AppConfig>,
    path: Option<String>,
) -> Result<(), String> {
    dock_registration::open_defaultspack_desktop_window_path_impl(
        &app,
        config.inner(),
        path.as_deref().unwrap_or("/chat"),
    )
    .map(|_| ())
    .map_err(|error| format!("{error:#}"))
}

fn open_defaults_console_window_for_app(app: &AppHandle, config: &AppConfig) -> Result<(), String> {
    open_small_defaultspack_window_for_app(
        app,
        DEFAULTS_CONSOLE_WINDOW_LABEL,
        DEFAULTS_CONSOLE_WINDOW_TITLE,
        authenticated_defaultspack_window_url(config, defaults_console_url())?,
        760.0,
        520.0,
    )
}

#[tauri::command]
async fn open_defaults_console_window(
    app: AppHandle,
    config: tauri::State<'_, AppConfig>,
) -> Result<(), String> {
    open_defaults_console_window_for_app(&app, config.inner())
}

fn focus_host_permissions_window(window: &tauri::WebviewWindow) -> Result<(), String> {
    window
        .unminimize()
        .map_err(|error| format!("failed to unminimize host permissions window: {error}"))?;
    window
        .show()
        .map_err(|error| format!("failed to show host permissions window: {error}"))?;
    window
        .set_focus()
        .map_err(|error| format!("failed to focus host permissions window: {error}"))
}

fn open_host_permissions_window_for_app(app: &AppHandle, config: &AppConfig) -> Result<(), String> {
    let host_permissions_url =
        authenticated_defaultspack_window_url(config, host_permissions_url())?;
    if let Some(window) = app.get_webview_window(HOST_PERMISSIONS_WINDOW_LABEL) {
        window
            .navigate(host_permissions_url)
            .map_err(|error| format!("failed to navigate host permissions window: {error}"))?;
        return focus_host_permissions_window(&window);
    }

    let window = tauri::WebviewWindowBuilder::new(
        app,
        HOST_PERMISSIONS_WINDOW_LABEL,
        tauri::WebviewUrl::External(host_permissions_url),
    )
    .title(HOST_PERMISSIONS_WINDOW_TITLE)
    .inner_size(900.0, 680.0)
    .min_inner_size(620.0, 480.0)
    .resizable(true)
    .focused(true)
    .visible(true)
    .build()
    .map_err(|error| format!("failed to open host permissions window: {error}"))?;
    focus_host_permissions_window(&window)
}

#[tauri::command]
async fn open_host_permissions_window(
    app: AppHandle,
    config: tauri::State<'_, AppConfig>,
) -> Result<(), String> {
    open_host_permissions_window_for_app(&app, config.inner())
}

#[cfg(debug_assertions)]
#[derive(Debug, Deserialize)]
struct AuthorityTestResponse {
    status: String,
    data: Option<AuthorityTestData>,
}

#[cfg(debug_assertions)]
#[derive(Debug, Deserialize)]
struct AuthorityTestData {
    request_id: Option<String>,
}

#[cfg(debug_assertions)]
fn truthy_env_flag(name: &str) -> bool {
    matches!(
        std::env::var(name)
            .unwrap_or_default()
            .trim()
            .to_ascii_lowercase()
            .as_str(),
        "1" | "true" | "yes" | "on"
    )
}

#[cfg(debug_assertions)]
fn maybe_spawn_authority_approval_smoke_window(app: AppHandle) {
    if !truthy_env_flag("RUMI_AUTHORITY_TEST_AUTORUN") {
        return;
    }

    thread::spawn(move || {
        let client = match reqwest::blocking::Client::builder()
            .timeout(Duration::from_secs(3))
            .build()
        {
            Ok(client) => client,
            Err(error) => {
                warn!("authority smoke test client unavailable: {error}");
                return;
            }
        };
        let base_url = format!("http://127.0.0.1:{DEFAULTSPACK_RESERVED_PORT}");
        let health_url = format!("{base_url}/api/health");
        let deadline = SystemTime::now() + Duration::from_secs(60);
        while SystemTime::now() < deadline {
            if client
                .get(&health_url)
                .send()
                .map(|response| response.status().is_success())
                .unwrap_or(false)
            {
                break;
            }
            thread::sleep(Duration::from_millis(300));
        }

        let request_url = format!("{base_url}/api/authority/test/request");
        let response = match client
            .post(&request_url)
            .json(&serde_json::json!({
                "provider_id": "opencode-go",
                "api_id": "legacy",
                "model_id": "deepseek-v4-pro",
                "model_ref": "opencode-go/deepseek-v4-pro",
                "pack_id": "defaultspack",
                "app_display_name": "defaultspack v2",
                "provider_display_name": "OpenCode Go",
                "model_display_name": "DeepSeek V4 Pro via OpenCode Go",
                "credential_label": "OpenCode Go API key",
                "endpoint_url": "https://opencode.ai/zen/go/v1/chat/completions",
                "endpoint_path": "/chat/completions",
                "domain": "opencode.ai",
                "transport": "https",
                "provider_transport": "openai_chat_completions",
                "provider_kind": "cloud",
                "port": 443,
                "reason": "defaultspack v2: OpenCode Go provider を DeepSeek V4 Pro との通信に使います。"
            }))
            .send()
        {
            Ok(response) => response,
            Err(error) => {
                warn!("authority smoke test request failed: {error}");
                return;
            }
        };

        let payload = match response.json::<AuthorityTestResponse>() {
            Ok(payload) => payload,
            Err(error) => {
                warn!("authority smoke test response was not JSON: {error}");
                return;
            }
        };
        if payload.status != "ok" {
            warn!(
                "authority smoke test endpoint returned status={}",
                payload.status
            );
            return;
        }
        let request_id = payload
            .data
            .and_then(|data| data.request_id)
            .unwrap_or_default();
        if !valid_authority_request_id(&request_id) {
            warn!("authority smoke test returned invalid request id");
            return;
        }

        thread::sleep(Duration::from_secs(2));
        let app_for_open = app.clone();
        let config_for_open = app.state::<AppConfig>().inner().clone();
        let request_id_for_open = request_id.clone();
        if let Err(error) = app.run_on_main_thread(move || {
            match open_authority_approval_window_for_app(
                &app_for_open,
                &config_for_open,
                &request_id_for_open,
            ) {
                Ok(()) => info!(
                    "authority smoke approval window opened on main thread for request {request_id_for_open}"
                ),
                Err(error) => {
                    warn!("authority smoke approval window failed: {error}");
                }
            }
        }) {
            warn!("authority smoke test could not schedule approval window: {error}");
        }
    });
}

fn unix_now_seconds() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_else(|_| Duration::from_secs(0))
        .as_secs()
}

fn authority_operator_message(operator: &AuthorityUiOperator) -> String {
    [
        format!("v{}", operator.version),
        operator.origin.clone(),
        operator.window_label.clone(),
        operator.request_id.clone(),
        operator.issued_at.to_string(),
        operator.expires_at.to_string(),
        operator.nonce.clone(),
    ]
    .join("\n")
}

fn sign_authority_ui_operator(
    request_id: &str,
    bootstrap_secret: &str,
    now: u64,
    nonce: String,
) -> Result<AuthorityUiOperator, String> {
    if !valid_authority_request_id(request_id) {
        return Err("invalid authority request id".into());
    }
    if bootstrap_secret.trim().is_empty() {
        return Err("approval signing secret is unavailable".into());
    }
    let mut operator = AuthorityUiOperator {
        version: 1,
        kind: "ui_operator".into(),
        origin: "tauri_webview_window".into(),
        window_label: AUTHORITY_APPROVAL_WINDOW_LABEL.into(),
        request_id: request_id.trim().into(),
        issued_at: now,
        expires_at: now + AUTHORITY_UI_OPERATOR_TTL_SECONDS,
        nonce,
        signature: String::new(),
    };
    let mut mac = HmacSha256::new_from_slice(bootstrap_secret.as_bytes())
        .map_err(|error| format!("failed to prepare approval signature: {error}"))?;
    mac.update(authority_operator_message(&operator).as_bytes());
    operator.signature = hex::encode(mac.finalize().into_bytes());
    Ok(operator)
}

#[tauri::command]
fn authority_approval_context(
    window: tauri::WebviewWindow,
    config: tauri::State<'_, AppConfig>,
    request_id: String,
) -> Result<AuthorityApprovalContext, String> {
    if window.label() != AUTHORITY_APPROVAL_WINDOW_LABEL {
        return Err("approval context is only available in the approval window".into());
    }
    let current_url = window
        .url()
        .map_err(|error| format!("failed to inspect approval window URL: {error}"))?;
    if current_url.path() != "/approval" {
        return Err("approval context is only available on the approval route".into());
    }
    let request_id = request_id.trim().to_string();
    let url_request_id = current_url
        .query_pairs()
        .find_map(|(key, value)| (key == "request_id").then(|| value.into_owned()))
        .unwrap_or_default();
    if request_id != url_request_id {
        return Err("approval context request id does not match the approval window URL".into());
    }
    let bootstrap_secret = load_or_create_panel_bootstrap_secret(&config)
        .map_err(|error| format!("failed to load approval signing secret: {error}"))?;
    let nonce: String = rand::thread_rng()
        .sample_iter(&Alphanumeric)
        .take(32)
        .map(char::from)
        .collect();
    let operator =
        sign_authority_ui_operator(&request_id, &bootstrap_secret, unix_now_seconds(), nonce)?;
    Ok(AuthorityApprovalContext {
        request_id,
        ui_operator: operator,
    })
}

#[tauri::command]
fn send_to_background(app: AppHandle) -> Result<(), String> {
    send_app_to_background(&app)
}

#[tauri::command]
fn show_app_window(app: AppHandle) -> Result<(), String> {
    // Renderer-invoked window restore must not mint or inject fresh panel auth material.
    restore_primary_window(&app, false)
}

#[tauri::command]
fn get_background_control_status(
    app: AppHandle,
    km: tauri::State<'_, Arc<Mutex<KernelManager>>>,
    shutdown: tauri::State<'_, ShutdownState>,
) -> Result<BackgroundControlStatus, String> {
    let kernel_running = {
        let mut kernel = km.lock().map_err(|error| format!("lock error: {error}"))?;
        kernel.is_running()
    };
    let shutdown_requested = shutdown.0.load(Ordering::SeqCst);
    Ok(summarize_background_control_status(
        collect_primary_window_states(&app),
        kernel_running,
        shutdown_requested,
    ))
}

fn generate_panel_bootstrap_secret() -> String {
    rand::thread_rng()
        .sample_iter(&Alphanumeric)
        .take(64)
        .map(char::from)
        .collect()
}

fn load_or_create_panel_bootstrap_secret(config: &AppConfig) -> AnyResult<String> {
    let path = config.panel_bootstrap_secret_path();
    match fs::read_to_string(&path) {
        Ok(existing) => {
            let trimmed = existing.trim();
            if !trimmed.is_empty() {
                restrict_panel_bootstrap_secret_permissions(&path)?;
                return Ok(trimmed.to_string());
            }
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) => {
            warn!(
                "Failed to read persisted panel bootstrap secret from {}: {error}",
                path.display()
            );
        }
    }

    let secret = generate_panel_bootstrap_secret();
    write_panel_bootstrap_secret(&path, &secret)?;
    Ok(secret)
}

#[cfg(unix)]
fn restrict_panel_bootstrap_secret_permissions(path: &std::path::Path) -> AnyResult<()> {
    use std::os::unix::fs::PermissionsExt;

    let metadata = fs::symlink_metadata(path).with_context(|| {
        format!(
            "failed to inspect persisted panel bootstrap secret at {}",
            path.display()
        )
    })?;
    if metadata.file_type().is_symlink() {
        bail!(
            "refusing to use symlinked panel bootstrap secret at {}",
            path.display()
        );
    }

    let mode = metadata.permissions().mode();
    if mode & 0o077 != 0 {
        fs::set_permissions(path, fs::Permissions::from_mode(0o600)).with_context(|| {
            format!(
                "failed to restrict panel bootstrap secret permissions at {}",
                path.display()
            )
        })?;
    }
    Ok(())
}

#[cfg(not(unix))]
fn restrict_panel_bootstrap_secret_permissions(_path: &std::path::Path) -> AnyResult<()> {
    Ok(())
}

fn write_panel_bootstrap_secret(path: &std::path::Path, secret: &str) -> AnyResult<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).with_context(|| {
            format!(
                "failed to create parent directory for bootstrap secret at {}",
                path.display()
            )
        })?;
    }

    let mut file = secure_panel_bootstrap_secret_file(path)?;
    file.write_all(secret.as_bytes()).with_context(|| {
        format!(
            "failed to persist panel bootstrap secret at {}",
            path.display()
        )
    })?;
    restrict_panel_bootstrap_secret_permissions(path)?;
    Ok(())
}

#[cfg(unix)]
fn secure_panel_bootstrap_secret_file(path: &std::path::Path) -> AnyResult<fs::File> {
    use std::os::unix::fs::OpenOptionsExt;

    match fs::symlink_metadata(path) {
        Ok(metadata) if metadata.file_type().is_symlink() => {
            bail!(
                "refusing to overwrite symlinked panel bootstrap secret at {}",
                path.display()
            );
        }
        Ok(_) => {}
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) => {
            return Err(error).with_context(|| {
                format!(
                    "failed to inspect panel bootstrap secret before writing at {}",
                    path.display()
                )
            });
        }
    }

    fs::OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o600)
        .open(path)
        .with_context(|| {
            format!(
                "failed to open panel bootstrap secret for secure write at {}",
                path.display()
            )
        })
}

#[cfg(not(unix))]
fn secure_panel_bootstrap_secret_file(path: &std::path::Path) -> AnyResult<fs::File> {
    fs::OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .open(path)
        .with_context(|| {
            format!(
                "failed to open panel bootstrap secret for write at {}",
                path.display()
            )
        })
}

fn request_panel_bootstrap_code(port: u16, bootstrap_secret: &str) -> AnyResult<String> {
    let client = reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(10))
        .build()
        .context("failed to build bootstrap HTTP client")?;
    let url = format!("http://127.0.0.1:{port}/api/panel/auth/bootstrap");
    let response = client
        .post(url)
        .header("X-Rumi-Desktop-Bootstrap", bootstrap_secret)
        .send()
        .context("panel bootstrap request failed")?;

    if !response.status().is_success() {
        let status = response.status();
        let body = response.text().unwrap_or_default();
        bail!("panel bootstrap returned {status}: {body}");
    }

    let envelope: ApiEnvelope<PanelBootstrapPayload> = response
        .json()
        .context("failed to decode panel bootstrap response")?;
    if !envelope.success {
        bail!(envelope
            .error
            .unwrap_or_else(|| "panel bootstrap failed".into()));
    }

    let payload = envelope
        .data
        .context("panel bootstrap response missing payload")?;
    if payload.code.is_empty() {
        bail!("panel bootstrap response missing code");
    }
    Ok(payload.code)
}

fn request_panel_bootstrap_code_with_retry(port: u16, bootstrap_secret: &str) -> AnyResult<String> {
    let max_attempts = 10;
    let retry_delay = Duration::from_millis(500);
    let mut last_error = None;

    for attempt in 1..=max_attempts {
        match request_panel_bootstrap_code(port, bootstrap_secret) {
            Ok(code) => return Ok(code),
            Err(error) => {
                last_error = Some(error);
                if attempt < max_attempts {
                    thread::sleep(retry_delay);
                }
            }
        }
    }

    match last_error {
        Some(error) => Err(error),
        None => bail!("panel bootstrap retry finished without making a request"),
    }
}

fn is_loopback_port_available(port: u16) -> bool {
    match TcpListener::bind(("127.0.0.1", port)) {
        Ok(listener) => {
            drop(listener);
            true
        }
        Err(error) if error.kind() == io::ErrorKind::AddrInUse => false,
        Err(error) => {
            warn!("Could not probe loopback port {port}: {error}");
            false
        }
    }
}

fn existing_kernel_accepts_bootstrap(port: u16, bootstrap_secret: &str) -> bool {
    health_check::check_authenticated_health(port, bootstrap_secret).unwrap_or(false)
        && request_panel_bootstrap_code(port, bootstrap_secret).is_ok()
}

fn resolve_available_kernel_port_with_checks<PortAvailable, ExistingKernelReusable>(
    preferred_port: u16,
    mut port_available: PortAvailable,
    mut existing_kernel_reusable: ExistingKernelReusable,
) -> u16
where
    PortAvailable: FnMut(u16) -> bool,
    ExistingKernelReusable: FnMut(u16) -> bool,
{
    if port_available(preferred_port) || existing_kernel_reusable(preferred_port) {
        return preferred_port;
    }

    let last_candidate = preferred_port.saturating_add(128);
    for port in preferred_port.saturating_add(1)..=last_candidate {
        if port == DEFAULTSPACK_RESERVED_PORT {
            continue;
        }
        if port_available(port) {
            return port;
        }
    }

    preferred_port
}

fn resolve_available_kernel_port(config: &AppConfig, bootstrap_secret: &str) -> u16 {
    let preferred_port = config.kernel_port;
    let port = resolve_available_kernel_port_with_checks(
        preferred_port,
        is_loopback_port_available,
        |candidate| existing_kernel_accepts_bootstrap(candidate, bootstrap_secret),
    );

    if port != preferred_port {
        warn!(
            "Kernel port {preferred_port} is already occupied by another local process; using {port} for this Viewer session"
        );
    }

    port
}

fn set_allowed_navigation_ports(state: &Arc<Mutex<Vec<u16>>>, ports: Vec<u16>) {
    let mut deduped = ports;
    deduped.sort_unstable();
    deduped.dedup();
    match state.lock() {
        Ok(mut allowed_ports) => {
            *allowed_ports = deduped;
        }
        Err(error) => {
            error!("Allowed navigation port lock poisoned: {error}");
        }
    }
}

fn dev_server_port_from_tauri_config(raw_config: &str) -> Option<u16> {
    serde_json::from_str::<TauriConfigEnv>(raw_config)
        .ok()
        .and_then(|config| config.build)
        .and_then(|build| build.dev_url)
        .and_then(|dev_url| Url::parse(&dev_url).ok())
        .filter(|url| url.scheme() == "http")
        .filter(|url| matches!(url.host_str(), Some("localhost") | Some("127.0.0.1")))
        .and_then(|url| url.port_or_known_default())
}

#[cfg(debug_assertions)]
fn tauri_dev_server_port() -> Option<u16> {
    option_env!("TAURI_CONFIG").and_then(dev_server_port_from_tauri_config)
}

#[cfg(not(debug_assertions))]
fn tauri_dev_server_port() -> Option<u16> {
    None
}

fn navigation_ports_with_tauri_dev_server(mut ports: Vec<u16>) -> Vec<u16> {
    if let Some(port) = tauri_dev_server_port() {
        ports.push(port);
    }
    ports
}

fn navigation_is_allowed(
    scheme: &str,
    host: &str,
    port: Option<u16>,
    allowed_ports: &[u16],
) -> bool {
    if scheme == "tauri" {
        return true;
    }
    scheme == "http"
        && (host == "localhost" || host == "127.0.0.1")
        && port.is_some_and(|candidate| allowed_ports.contains(&candidate))
}

fn panel_session_url_for_current(
    current: Option<&Url>,
    port: u16,
    panel_code: &str,
) -> Result<Url, tauri::Error> {
    if let Some(current_url) = current {
        let is_current_panel = current_url.scheme() == "http"
            && matches!(
                current_url.host_str(),
                Some("localhost") | Some("127.0.0.1")
            )
            && current_url.port_or_known_default() == Some(port)
            && current_url.path().starts_with("/panel");

        if is_current_panel {
            let mut next = current_url.clone();
            let mut query_pairs = next
                .query_pairs()
                .filter(|(key, _)| key != "code")
                .map(|(key, value)| (key.into_owned(), value.into_owned()))
                .collect::<Vec<_>>();
            query_pairs.push(("code".to_string(), panel_code.to_string()));

            next.set_query(None);
            next.query_pairs_mut().extend_pairs(query_pairs);
            return Ok(next);
        }
    }

    Url::parse_with_params(
        &format!("http://127.0.0.1:{port}/panel/"),
        [("code", panel_code)],
    )
    .map_err(tauri::Error::InvalidUrl)
}

fn ensure_kernel_ready_for_panel_auth(
    config: &AppConfig,
    km: &Arc<Mutex<KernelManager>>,
) -> AnyResult<()> {
    let port = config.kernel_port;
    let kernel_is_running = km
        .lock()
        .map_err(|error| anyhow!("kernel manager lock poisoned: {error}"))?
        .is_running();
    if kernel_is_running && health_check::check_health(port)? {
        return Ok(());
    }

    if kernel_is_running && health_check::wait_for_healthy(port, 5).is_ok() {
        return Ok(());
    }

    let mut kernel = km
        .lock()
        .map_err(|error| anyhow!("kernel manager lock poisoned: {error}"))?;
    if kernel.is_running() {
        kernel.restart()?;
    } else {
        kernel.start()?;
    }
    drop(kernel);

    health_check::wait_for_healthy(port, 60)?;
    Ok(())
}

fn request_fresh_panel_session_code(
    config: &AppConfig,
    km: &Arc<Mutex<KernelManager>>,
) -> AnyResult<String> {
    ensure_kernel_ready_for_panel_auth(config, km)?;
    let bootstrap_secret = load_or_create_panel_bootstrap_secret(config)
        .context("failed to load persisted panel bootstrap secret")?;
    request_panel_bootstrap_code_with_retry(config.kernel_port, &bootstrap_secret)
}

fn navigate_window_to_panel_session(
    window: &tauri::WebviewWindow,
    port: u16,
    panel_code: &str,
) -> Result<(), tauri::Error> {
    // On macOS a WebView can exist before WKWebView has a URL. Calling
    // `window.url()` during that short window panics in Wry, so always use the
    // stable panel entry point for a fresh authenticated session.
    let panel_url = panel_session_url_for_current(None, port, panel_code)?;
    // `WebviewWindow::navigate` can return success on macOS while a WebView
    // booted from the bundled splash page remains on `tauri://`. Changing the
    // active document location reliably completes the same guarded local
    // navigation and avoids leaving the user on a permanent “Ready”.
    let script = format!("window.location.replace({:?});", panel_url.as_str());
    window.eval(&script)
}

fn show_and_focus_window(window: &tauri::WebviewWindow) -> Result<(), tauri::Error> {
    window.unminimize()?;
    window.show()?;
    window.set_focus()
}

fn navigate_and_show_window_to_panel_session(
    window: &tauri::WebviewWindow,
    port: u16,
    panel_code: &str,
) -> Result<(), tauri::Error> {
    navigate_window_to_panel_session(window, port, panel_code)?;
    show_and_focus_window(window)
}

pub(crate) fn refresh_panel_session_for_window(app: &AppHandle, window_label: &str) {
    let config = app.state::<AppConfig>().inner().clone();
    let km = Arc::clone(app.state::<Arc<Mutex<KernelManager>>>().inner());
    let handle = app.clone();
    let label = window_label.to_string();

    std::thread::spawn(
        move || match request_fresh_panel_session_code(&config, &km) {
            Ok(panel_code) => {
                if let Some(win) = handle.get_webview_window(&label) {
                    if let Err(error) =
                        navigate_window_to_panel_session(&win, config.kernel_port, &panel_code)
                    {
                        error!("Failed to refresh panel session for {label}: {error}");
                    }
                }
            }
            Err(error) => {
                warn!("Failed to refresh panel session for {label}: {error}");
            }
        },
    );
}

pub(crate) fn primary_window_label(has_panel: bool, has_main: bool) -> Option<&'static str> {
    if has_panel {
        Some("panel")
    } else if has_main {
        Some("main")
    } else {
        None
    }
}

fn should_send_to_background_on_close(label: &str) -> bool {
    PRIMARY_WINDOW_LABELS.contains(&label)
}

fn should_restore_primary_on_close(label: &str) -> bool {
    dock_registration::is_defaultspack_main_window(label)
}

fn restore_primary_window(app: &AppHandle, refresh_panel_session: bool) -> Result<(), String> {
    let target = primary_window_label(
        app.get_webview_window("panel").is_some(),
        app.get_webview_window("main").is_some(),
    );

    let Some(label) = target else {
        return Err("no Rumi window is available".into());
    };

    if refresh_panel_session {
        refresh_panel_session_for_window(app, label);
    }
    if let Some(window) = app.get_webview_window(label) {
        window
            .unminimize()
            .map_err(|error| format!("failed to unminimize window: {error}"))?;
        window
            .show()
            .map_err(|error| format!("failed to show window: {error}"))?;
        window
            .set_focus()
            .map_err(|error| format!("failed to focus window: {error}"))?;
    }

    Ok(())
}

pub(crate) fn show_primary_window(app: &AppHandle) -> Result<(), String> {
    restore_primary_window(app, true)
}

pub(crate) fn send_app_to_background(app: &AppHandle) -> Result<(), String> {
    let mut found_window = false;
    for label in PRIMARY_WINDOW_LABELS {
        if let Some(window) = app.get_webview_window(label) {
            found_window = true;
            window
                .hide()
                .map_err(|error| format!("failed to hide {label} window: {error}"))?;
        }
    }

    if !found_window {
        warn!("Background request ignored because no Rumi window is available");
    }

    Ok(())
}

fn collect_primary_window_states(app: &AppHandle) -> Vec<WindowRuntimeSnapshot> {
    PRIMARY_WINDOW_LABELS
        .iter()
        .filter_map(|label| {
            app.get_webview_window(label)
                .map(|window| WindowRuntimeSnapshot {
                    label: (*label).to_string(),
                    visible: window.is_visible().unwrap_or(false),
                    minimized: window.is_minimized().unwrap_or(false),
                    focused: window.is_focused().unwrap_or(false),
                })
        })
        .collect()
}

fn summarize_background_control_status(
    windows: Vec<WindowRuntimeSnapshot>,
    kernel_running: bool,
    shutdown_requested: bool,
) -> BackgroundControlStatus {
    let app_visible = windows
        .iter()
        .any(|window| window.visible && !window.minimized);
    let foreground_window = windows
        .iter()
        .find(|window| window.visible && window.focused)
        .or_else(|| {
            windows
                .iter()
                .find(|window| window.visible && !window.minimized)
        })
        .map(|window| window.label.clone());

    BackgroundControlStatus {
        enabled: !shutdown_requested,
        app_visible,
        foreground_window,
        kernel_running,
        shutdown_requested,
        windows,
    }
}

pub(crate) fn request_app_exit(app: &AppHandle) {
    let shutdown_flag = Arc::clone(&app.state::<ShutdownState>().inner().0);
    if shutdown_flag.swap(true, Ordering::SeqCst) {
        return;
    }

    for label in ["panel", "main"] {
        if let Some(window) = app.get_webview_window(label) {
            let _ = window.hide();
        }
    }

    let km = Arc::clone(app.state::<Arc<Mutex<KernelManager>>>().inner());
    let defaultspack = Arc::clone(app.state::<Arc<DefaultspackManager>>().inner());
    let handle = app.clone();

    std::thread::spawn(move || {
        stop_managed_runtimes(&defaultspack, &km);
        handle.exit(0);
    });
}

fn stop_managed_runtimes(
    defaultspack: &DefaultspackManager,
    kernel_manager: &Mutex<KernelManager>,
) {
    if let Err(error) = defaultspack.stop() {
        error!("Failed to stop Defaultspack during shutdown: {error:#}");
    }
    match kernel_manager.lock() {
        Ok(mut kernel) => {
            if let Err(error) = kernel.stop() {
                error!("Failed to stop kernel during shutdown: {error}");
            }
        }
        Err(error) => {
            error!("Failed to lock kernel manager during shutdown: {error}");
        }
    }
}

fn spawn_kernel_exit_monitor(
    app: AppHandle,
    config: AppConfig,
    km: Arc<Mutex<KernelManager>>,
    shutdown_flag: Arc<AtomicBool>,
    panel_bootstrap_secret: String,
) {
    thread::spawn(move || loop {
        if shutdown_flag.load(Ordering::SeqCst) {
            break;
        }

        let mut restarted = false;
        match km.lock() {
            Ok(mut kernel) => {
                if !kernel.is_running() {
                    match kernel.wait_and_handle_restart() {
                        Ok(true) => match kernel.start() {
                            Ok(()) => {
                                restarted = true;
                                info!("Kernel restart handoff completed");
                            }
                            Err(error) => {
                                error!("Failed to restart Kernel after handoff: {error}");
                            }
                        },
                        Ok(false) => {}
                        Err(error) => {
                            warn!("Failed to inspect Kernel exit status: {error}");
                        }
                    }
                }
            }
            Err(error) => {
                error!("Failed to lock kernel manager for exit monitor: {error}");
            }
        }

        if restarted {
            match health_check::wait_for_healthy(config.kernel_port, 60).and_then(|_| {
                request_panel_bootstrap_code_with_retry(config.kernel_port, &panel_bootstrap_secret)
            }) {
                Ok(panel_code) => {
                    if let Some(win) = app.get_webview_window("main") {
                        if let Err(error) =
                            navigate_window_to_panel_session(&win, config.kernel_port, &panel_code)
                        {
                            error!("Failed to refresh panel after Kernel restart: {error}");
                        }
                    }
                }
                Err(error) => {
                    warn!("Kernel restarted, but panel session refresh failed: {error}");
                }
            }
        }

        thread::sleep(Duration::from_millis(500));
    });
}

fn update_setup_progress(app_handle: Option<&AppHandle>, progress: &Arc<Mutex<String>>, msg: &str) {
    match progress.lock() {
        Ok(mut state) => {
            *state = msg.to_string();
        }
        Err(error) => {
            error!("Failed to update setup progress: {error}");
        }
    }
    if let Some(handle) = app_handle {
        let _ = handle.emit("setup-progress", msg);
    }
    info!("{msg}");
}

fn run_delayed_update_check() {
    thread::sleep(Duration::from_secs(5));
    match updater::check_for_update() {
        Ok(Some(info)) => {
            info!(
                "Update available: {} -> {}",
                info.current_version, info.latest_version
            );
        }
        Ok(None) => {
            info!("Rumi AI is up to date.");
        }
        Err(e) => {
            error!("Startup update check failed (non-fatal): {e}");
        }
    }
}

fn startup_failure_message(stage: &str, error: &anyhow::Error, config: &AppConfig) -> String {
    let log_path = config.log_dir.join("kernel.log");
    format!(
        "Error: {stage} failed — {error}. See {}",
        log_path.display()
    )
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum StartupRecoveryStage {
    HealthCheck,
    Bootstrap,
}

fn run_startup_sequence<StartKernel, WaitForHealthy, AuthorizePanel, RecoverConflict>(
    app_handle: Option<&AppHandle>,
    progress: &Arc<Mutex<String>>,
    mut start_kernel: StartKernel,
    mut wait_for_healthy: WaitForHealthy,
    mut authorize_panel: AuthorizePanel,
    mut recover_conflict: RecoverConflict,
) -> AnyResult<String>
where
    StartKernel: FnMut() -> AnyResult<()>,
    WaitForHealthy: FnMut() -> AnyResult<()>,
    AuthorizePanel: FnMut() -> AnyResult<String>,
    RecoverConflict: FnMut(StartupRecoveryStage) -> AnyResult<Option<String>>,
{
    let mut recovered_conflict = false;

    loop {
        update_setup_progress(
            app_handle,
            progress,
            if recovered_conflict {
                "Retrying Kernel startup after recovering a stale listener..."
            } else {
                "Starting Kernel..."
            },
        );

        start_kernel()?;

        update_setup_progress(app_handle, progress, "Waiting for Kernel...");
        if let Err(error) = wait_for_healthy() {
            if recovered_conflict {
                return Err(error);
            }

            if let Some(message) = recover_conflict(StartupRecoveryStage::HealthCheck)? {
                warn!("{message}");
                recovered_conflict = true;
                continue;
            }

            return Err(error);
        }

        update_setup_progress(app_handle, progress, "Authorizing panel session...");
        match authorize_panel() {
            Ok(code) => return Ok(code),
            Err(error) => {
                if recovered_conflict {
                    return Err(error);
                }

                if let Some(message) = recover_conflict(StartupRecoveryStage::Bootstrap)? {
                    warn!("{message}");
                    recovered_conflict = true;
                    continue;
                }

                return Err(error);
            }
        }
    }
}

fn start_kernel_and_bootstrap(
    app_handle: &AppHandle,
    km: &Arc<Mutex<KernelManager>>,
    port: u16,
    bootstrap_secret: &str,
    progress: &Arc<Mutex<String>>,
) -> AnyResult<String> {
    run_startup_sequence(
        Some(app_handle),
        progress,
        || {
            let mut kernel = km
                .lock()
                .map_err(|error| anyhow!("kernel manager lock poisoned: {error}"))?;
            kernel.start()?;
            Ok(())
        },
        || {
            health_check::wait_for_healthy(port, 60)?;
            Ok(())
        },
        || request_panel_bootstrap_code_with_retry(port, bootstrap_secret),
        |_| {
            let mut kernel = km
                .lock()
                .map_err(|lock_error| anyhow!("kernel manager lock poisoned: {lock_error}"))?;
            kernel.recover_port_conflict()
        },
    )
}

pub fn run() {
    env_logger::init();

    let allowed_navigation_ports =
        Arc::new(Mutex::new(navigation_ports_with_tauri_dev_server(vec![
            8765,
        ])));
    let allowed_navigation_ports_for_plugin = Arc::clone(&allowed_navigation_ports);
    let allowed_navigation_ports_for_setup = Arc::clone(&allowed_navigation_ports);

    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Err(error) = show_primary_window(app) {
                error!("Failed to focus existing Rumi window after duplicate launch: {error}");
            }
        }))
        .plugin(
            tauri::plugin::Builder::<tauri::Wry, ()>::new("nav-guard")
                .on_navigation(move |_webview, url| {
                    let scheme = url.scheme();
                    let host = url.host_str().unwrap_or("");
                    let port = url.port_or_known_default();
                    let allowed_ports = allowed_navigation_ports_for_plugin
                        .lock()
                        .map(|ports| ports.clone())
                        .unwrap_or_default();
                    let allowed = navigation_is_allowed(scheme, host, port, &allowed_ports);

                    if !allowed {
                        log::warn!("Blocked navigation to: {url}");
                    }
                    allowed
                })
                .build(),
        )
        .setup(move |app| {
            let resource_dir = match app.path().resource_dir() {
                Ok(resource_dir) => resource_dir,
                Err(error) => bundled_resource_dir_fallback().ok_or_else(|| {
                    anyhow!("failed to resolve resource_dir: {error}")
                })?,
            };
            let app_data_dir = app
                .path()
                .app_data_dir()
                .context("failed to resolve app_data_dir")?;
            if app_data_migration::migrate_legacy_app_data(&app_data_dir)? {
                info!("copied legacy Rumi Viewer application data into Tobkiri Launcher storage");
            }

            let mut config = AppConfig::detect_for_tauri(resource_dir, app_data_dir)
                .context("failed to build AppConfig")?;

            std::fs::create_dir_all(&config.log_dir).ok();
            std::fs::create_dir_all(&config.user_data_dir).ok();
            std::fs::create_dir_all(config.host_broker_dir()).ok();

            let progress = SetupProgress(Arc::new(Mutex::new(
                "Initializing...".to_string(),
            )));
            let progress_arc = progress.0.clone();
            app.manage(progress);
            let shutdown_flag = Arc::new(AtomicBool::new(false));
            app.manage(ShutdownState(Arc::clone(&shutdown_flag)));

            let panel_bootstrap_secret = load_or_create_panel_bootstrap_secret(&config)
                .context("failed to load persisted panel bootstrap secret")?;
            let host_broker = HostBrokerRuntime::start(&config)
                .context("failed to start Viewer host broker")?;
            app.manage(host_broker.clone());
            config.kernel_port = resolve_available_kernel_port(&config, &panel_bootstrap_secret);
            set_allowed_navigation_ports(
                &allowed_navigation_ports_for_setup,
                navigation_ports_with_tauri_dev_server(vec![
                    config.kernel_port,
                    DEFAULTSPACK_RESERVED_PORT,
                ]),
            );
            app.manage(AllowedNavigationPorts(Arc::clone(
                &allowed_navigation_ports_for_setup,
            )));
            let km = Arc::new(Mutex::new(KernelManager::new(
                &config,
                panel_bootstrap_secret.clone(),
            )));
            let km_for_thread = km.clone();
            let km_for_monitor = km.clone();
            app.manage(km);

            let defaultspack_manager = Arc::new(DefaultspackManager::new(
                config.clone(),
                Arc::clone(&shutdown_flag),
            ));
            let defaultspack_manager_for_monitor = Arc::clone(&defaultspack_manager);
            app.manage(defaultspack_manager);

            app.manage(config.clone());

            if let Some(win) = app.get_webview_window("main") {
                let _ = win.show();
            }

            let handle = app.handle().clone();
            let monitor_handle = app.handle().clone();
            let port = config.kernel_port;

            #[cfg(debug_assertions)]
            maybe_spawn_authority_approval_smoke_window(app.handle().clone());

            spawn_kernel_exit_monitor(
                monitor_handle,
                config.clone(),
                km_for_monitor,
                Arc::clone(&app.state::<ShutdownState>().inner().0),
                panel_bootstrap_secret.clone(),
            );
            DefaultspackManager::spawn_exit_monitor(defaultspack_manager_for_monitor);

            std::thread::spawn(move || {
                // --- Fast path: existing authenticated kernel ---
                update_setup_progress(
                    Some(&handle),
                    &progress_arc,
                    "Checking for existing session...",
                );
                if let Ok(true) =
                    health_check::check_authenticated_health(port, &panel_bootstrap_secret)
                {
                    info!("Existing authenticated kernel detected on port {port}, attempting fast-path bootstrap...");
                    match request_panel_bootstrap_code_with_retry(port, &panel_bootstrap_secret) {
                        Ok(panel_code) => {
                            update_setup_progress(Some(&handle), &progress_arc, "Ready");
                            if let Some(win) = handle.get_webview_window("main") {
                                if let Err(e) =
                                    navigate_and_show_window_to_panel_session(&win, port, &panel_code)
                                {
                                    error!("Failed to navigate to panel: {e}");
                                }
                            }
                            // Delayed background update check.
                            run_delayed_update_check();
                            return;
                        }
                        Err(e) => {
                            info!("Fast-path bootstrap failed: {e}, falling back to normal startup");
                        }
                    }
                }

                // --- Normal startup sequence ---
                update_setup_progress(Some(&handle), &progress_arc, "Checking Python environment...");
                if let Err(e) = python_env::ensure_python_env(&config) {
                    let msg = startup_failure_message("Python setup", &e, &config);
                    error!("{msg}");
                    update_setup_progress(Some(&handle), &progress_arc, &msg);
                    return;
                }

                let panel_code = match start_kernel_and_bootstrap(
                    &handle,
                    &km_for_thread,
                    port,
                    &panel_bootstrap_secret,
                    &progress_arc,
                ) {
                    Ok(code) => code,
                    Err(e) => {
                        let msg = startup_failure_message("Viewer startup", &e, &config);
                        error!("{msg}");
                        update_setup_progress(Some(&handle), &progress_arc, &msg);
                        return;
                    }
                };

                update_setup_progress(Some(&handle), &progress_arc, "Ready");

                if let Some(win) = handle.get_webview_window("main") {
                    if let Err(e) = navigate_and_show_window_to_panel_session(&win, port, &panel_code) {
                        error!("Failed to navigate to panel: {e}");
                    }
                }

                // Delayed background update check.
                run_delayed_update_check();
            });

            tray::setup_tray(app)?;

            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                if should_send_to_background_on_close(window.label()) {
                    api.prevent_close();
                    if let Err(error) = send_app_to_background(&window.app_handle()) {
                        error!("Failed to send app to background: {error}");
                    }
                } else if should_restore_primary_on_close(window.label()) {
                    if let Err(error) = restore_primary_window(&window.app_handle(), true) {
                        error!("Failed to restore launcher after closing Tobkiri: {error}");
                    }
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            get_setup_progress,
            restart_kernel,
            reauthorize_panel_session,
            open_external_url,
            close_current_window,
            open_authority_approval_window,
            open_ambient_trigger_window,
            open_finger_recording_window,
            open_defaultspack_main_window,
            open_defaults_console_window,
            open_host_permissions_window,
            authority_approval_context,
            send_to_background,
            show_app_window,
            get_background_control_status,
            desktop_system_info::get_desktop_system_info,
            desktop_system_info::get_host_permission_status,
            desktop_system_info::open_host_permission_settings,
            dock_registration::register_defaultspack_dock,
            dock_registration::launch_defaultspack_desktop
        ])
        .build(tauri::generate_context!())
        .map(|app| {
            app.run(|app_handle, event| {
                if let tauri::RunEvent::ExitRequested { api, .. } = &event {
                    let shutdown_requested = app_handle
                        .state::<ShutdownState>()
                        .inner()
                        .0
                        .load(Ordering::SeqCst);
                    if !shutdown_requested {
                        api.prevent_exit();
                        request_app_exit(app_handle);
                    }
                }

                // macOS can deliver a terminal Exit without first allowing the
                // asynchronous ExitRequested shutdown path to finish (for
                // example from the application-menu Quit action). Keep this
                // synchronous fallback so Launcher-owned 8765/8766 processes
                // cannot outlive the app. Re-running it after the normal path
                // is harmless because both managers have idempotent stops.
                if matches!(&event, tauri::RunEvent::Exit) {
                    app_handle
                        .state::<ShutdownState>()
                        .inner()
                        .0
                        .store(true, Ordering::SeqCst);
                    let defaultspack = app_handle.state::<Arc<DefaultspackManager>>();
                    let kernel_manager = app_handle.state::<Arc<Mutex<KernelManager>>>();
                    stop_managed_runtimes(defaultspack.inner(), kernel_manager.inner());
                }

                #[cfg(target_os = "macos")]
                if let tauri::RunEvent::Reopen {
                    has_visible_windows: false,
                    ..
                } = &event
                {
                    if let Err(error) = show_primary_window(app_handle) {
                        warn!("Failed to reopen primary window: {error}");
                    }
                }

                #[cfg(not(target_os = "macos"))]
                {
                    let _ = (app_handle, event);
                }
            });
        })
        .unwrap_or_else(|error| error!("error while running tauri application: {error}"));
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    use std::sync::{Mutex, OnceLock};
    use std::time::{SystemTime, UNIX_EPOCH};

    fn env_lock() -> &'static Mutex<()> {
        static ENV_LOCK: OnceLock<Mutex<()>> = OnceLock::new();
        ENV_LOCK.get_or_init(|| Mutex::new(()))
    }

    fn test_config() -> AppConfig {
        AppConfig::detect_for_tauri(
            PathBuf::from("/tmp/test_resource"),
            PathBuf::from("/tmp/test_appdata"),
        )
        .unwrap()
    }

    #[test]
    fn prefers_panel_window_when_available() {
        assert_eq!(primary_window_label(true, true), Some("panel"));
    }

    #[test]
    fn falls_back_to_main_window_before_panel_exists() {
        assert_eq!(primary_window_label(false, true), Some("main"));
    }

    #[test]
    fn returns_none_when_no_window_exists() {
        assert_eq!(primary_window_label(false, false), None);
    }

    #[test]
    fn authority_approval_url_targets_defaultspack_window_route() {
        let url = authority_approval_url("auth_123").unwrap();

        assert_eq!(
            url.as_str(),
            "http://127.0.0.1:8766/approval?request_id=auth_123"
        );
    }

    #[test]
    fn ambient_trigger_url_targets_defaultspack_window_route() {
        let url = ambient_trigger_url().unwrap();

        assert_eq!(url.as_str(), "http://127.0.0.1:8766/ambient");
    }

    #[test]
    fn finger_recording_url_targets_dedicated_defaultspack_window_route() {
        let url = finger_recording_url().unwrap();

        assert_eq!(url.as_str(), "http://127.0.0.1:8766/finger-recording");
    }

    #[test]
    fn defaults_console_url_targets_defaultspack_console_surface() {
        let url = defaults_console_url().unwrap();

        assert_eq!(url.as_str(), "http://127.0.0.1:8766/console");
    }

    #[test]
    fn authority_ui_operator_signature_is_bound_to_request_and_window() {
        let operator = sign_authority_ui_operator(
            "auth_123",
            "test-bootstrap-secret",
            1_700_000_000,
            "nonce-1".into(),
        )
        .unwrap();

        assert_eq!(operator.window_label, AUTHORITY_APPROVAL_WINDOW_LABEL);
        assert_eq!(operator.request_id, "auth_123");
        assert_eq!(
            authority_operator_message(&operator),
            "v1\ntauri_webview_window\nauthority-approval\nauth_123\n1700000000\n1700000180\nnonce-1"
        );
        assert!(!operator.signature.is_empty());
    }

    #[test]
    fn close_policy_keeps_primary_windows_but_allows_approval_close() {
        assert!(should_send_to_background_on_close("main"));
        assert!(should_send_to_background_on_close("panel"));
        assert!(!should_send_to_background_on_close(
            AUTHORITY_APPROVAL_WINDOW_LABEL
        ));
        assert!(should_restore_primary_on_close("defaultspack-main"));
        assert!(!should_restore_primary_on_close("authority-approval"));
    }

    #[test]
    fn macos_main_window_reserves_a_titlebar_without_showing_a_title() {
        let config: serde_json::Value =
            serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
        let main_window = &config["app"]["windows"][0];

        assert_eq!(main_window["hiddenTitle"], true);
        assert_eq!(main_window["titleBarStyle"], "Transparent");
    }

    #[test]
    fn background_status_reports_visible_foreground_window() {
        let status = summarize_background_control_status(
            vec![
                WindowRuntimeSnapshot {
                    label: "panel".into(),
                    visible: false,
                    minimized: false,
                    focused: false,
                },
                WindowRuntimeSnapshot {
                    label: "main".into(),
                    visible: true,
                    minimized: false,
                    focused: true,
                },
            ],
            true,
            false,
        );

        assert!(status.enabled);
        assert!(status.app_visible);
        assert!(status.kernel_running);
        assert_eq!(status.foreground_window.as_deref(), Some("main"));
    }

    #[test]
    fn background_status_stays_enabled_when_all_windows_are_hidden() {
        let status = summarize_background_control_status(
            vec![WindowRuntimeSnapshot {
                label: "main".into(),
                visible: false,
                minimized: false,
                focused: false,
            }],
            true,
            false,
        );

        assert!(status.enabled);
        assert!(!status.app_visible);
        assert_eq!(status.foreground_window, None);
        assert!(status.kernel_running);
    }

    #[test]
    fn background_status_disables_during_shutdown() {
        let status = summarize_background_control_status(Vec::new(), false, true);

        assert!(!status.enabled);
        assert!(status.shutdown_requested);
        assert!(!status.kernel_running);
    }

    #[test]
    fn resolve_kernel_port_keeps_available_preferred_port() {
        let port = resolve_available_kernel_port_with_checks(
            8765,
            |candidate| candidate == 8765,
            |_| false,
        );

        assert_eq!(port, 8765);
    }

    #[test]
    fn resolve_kernel_port_reuses_existing_kernel_when_bootstrap_matches() {
        let port = resolve_available_kernel_port_with_checks(
            8765,
            |_| false,
            |candidate| candidate == 8765,
        );

        assert_eq!(port, 8765);
    }

    #[test]
    fn resolve_kernel_port_skips_defaultspack_port_when_falling_back() {
        let port = resolve_available_kernel_port_with_checks(
            8765,
            |candidate| candidate == 8767,
            |_| false,
        );

        assert_eq!(port, 8767);
    }

    #[test]
    fn navigation_guard_allows_only_resolved_loopback_ports() {
        let allowed_ports = vec![8767, DEFAULTSPACK_RESERVED_PORT];

        assert!(navigation_is_allowed(
            "http",
            "localhost",
            Some(8767),
            &allowed_ports
        ));
        assert!(navigation_is_allowed(
            "http",
            "127.0.0.1",
            Some(8766),
            &allowed_ports
        ));
        assert!(navigation_is_allowed("tauri", "", None, &allowed_ports));
        assert!(!navigation_is_allowed(
            "http",
            "localhost",
            Some(8765),
            &allowed_ports
        ));
        assert!(!navigation_is_allowed(
            "http",
            "127.0.0.1",
            Some(9999),
            &allowed_ports
        ));
        assert!(!navigation_is_allowed(
            "https",
            "localhost",
            Some(8767),
            &allowed_ports
        ));
    }

    #[test]
    fn detects_tauri_dev_server_port_from_cli_config() {
        assert_eq!(
            dev_server_port_from_tauri_config(r#"{"build":{"devUrl":"http://127.0.0.1:1430"}}"#),
            Some(1430)
        );
        assert_eq!(
            dev_server_port_from_tauri_config(r#"{"build":{"devUrl":"https://127.0.0.1:1430"}}"#),
            None
        );
        assert_eq!(
            dev_server_port_from_tauri_config(r#"{"build":{"devUrl":"http://example.com:1430"}}"#),
            None
        );
    }

    #[test]
    fn panel_navigation_url_starts_at_panel_entrypoint() {
        let url = panel_session_url_for_current(None, 8765, "code with space").unwrap();

        assert_eq!(url.scheme(), "http");
        assert_eq!(url.host_str(), Some("127.0.0.1"));
        assert_eq!(url.port_or_known_default(), Some(8765));
        assert_eq!(url.path(), "/panel/");
        assert_eq!(
            url.query_pairs()
                .find(|(key, _)| key == "code")
                .map(|(_, value)| value.into_owned()),
            Some("code with space".into())
        );
    }

    #[test]
    fn panel_navigation_script_replaces_the_splash_location() {
        let url = panel_session_url_for_current(None, 8765, "fresh-code").unwrap();
        let script = format!("window.location.replace({:?});", url.as_str());

        assert_eq!(
            script,
            "window.location.replace(\"http://127.0.0.1:8765/panel/?code=fresh-code\");"
        );
    }

    #[test]
    fn panel_navigation_url_preserves_existing_panel_route() {
        let current =
            Url::parse("http://localhost:8765/panel/packs?foo=bar&code=old#section").unwrap();
        let url = panel_session_url_for_current(Some(&current), 8765, "new").unwrap();

        assert_eq!(
            url.as_str(),
            "http://localhost:8765/panel/packs?foo=bar&code=new#section"
        );
    }

    #[test]
    fn panel_navigation_url_escapes_blank_or_dev_page() {
        let current = Url::parse("http://127.0.0.1:1430/").unwrap();
        let url = panel_session_url_for_current(Some(&current), 8765, "fresh").unwrap();

        assert_eq!(url.as_str(), "http://127.0.0.1:8765/panel/?code=fresh");
    }

    fn isolated_app_config(prefix: &str) -> (PathBuf, AppConfig) {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("{prefix}_{unique}"));
        let config =
            AppConfig::detect_for_tauri(root.join("resource"), root.join("appdata")).unwrap();
        (root, config)
    }

    #[test]
    fn reuses_persisted_panel_bootstrap_secret() {
        let (root, config) = isolated_app_config("tobkiri_launcher_secret");

        let first = load_or_create_panel_bootstrap_secret(&config).unwrap();
        let second = load_or_create_panel_bootstrap_secret(&config).unwrap();

        assert_eq!(first, second);
        assert_eq!(
            fs::read_to_string(config.panel_bootstrap_secret_path())
                .unwrap()
                .trim(),
            first
        );

        fs::remove_dir_all(root).ok();
    }

    #[cfg(unix)]
    #[test]
    fn creates_panel_bootstrap_secret_with_owner_only_permissions() {
        use std::os::unix::fs::PermissionsExt;

        let (root, config) = isolated_app_config("tobkiri_launcher_secret_mode");

        load_or_create_panel_bootstrap_secret(&config).unwrap();

        let mode = fs::metadata(config.panel_bootstrap_secret_path())
            .unwrap()
            .permissions()
            .mode()
            & 0o777;
        assert_eq!(mode, 0o600);

        fs::remove_dir_all(root).ok();
    }

    #[cfg(unix)]
    #[test]
    fn restricts_existing_panel_bootstrap_secret_permissions() {
        use std::os::unix::fs::PermissionsExt;

        let (root, config) = isolated_app_config("tobkiri_launcher_secret_restrict");
        let path = config.panel_bootstrap_secret_path();
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(&path, "existing-secret").unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).unwrap();

        let loaded = load_or_create_panel_bootstrap_secret(&config).unwrap();

        assert_eq!(loaded, "existing-secret");
        let mode = fs::metadata(path).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o600);

        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn is_running_default_false() {
        let config = test_config();
        let mut km = KernelManager::new(&config, "test-bootstrap".into());
        assert!(!km.is_running());
    }

    #[test]
    fn stop_without_start_is_ok() {
        let config = test_config();
        let mut km = KernelManager::new(&config, "test-bootstrap".into());
        assert!(km.stop().is_ok());
    }

    #[test]
    fn wait_and_handle_restart_no_child() {
        let config = test_config();
        let mut km = KernelManager::new(&config, "test-bootstrap".into());
        let result = km.wait_and_handle_restart().unwrap();
        assert!(!result);
    }

    #[test]
    fn explicit_auto_approve_opt_in_is_required() {
        let _guard = env_lock().lock().unwrap();
        std::env::remove_var("RUMI_AUTO_APPROVE_LOCAL");

        let dev_environment = true;
        let auto_approve_local = dev_environment
            && std::env::var("RUMI_AUTO_APPROVE_LOCAL")
                .map(|value| value.eq_ignore_ascii_case("true"))
                .unwrap_or(false);

        assert!(!auto_approve_local);
    }

    #[test]
    fn explicit_auto_approve_opt_in_only_applies_in_dev() {
        let _guard = env_lock().lock().unwrap();
        std::env::set_var("RUMI_AUTO_APPROVE_LOCAL", "true");

        let production_auto_approve = false
            && std::env::var("RUMI_AUTO_APPROVE_LOCAL")
                .map(|value| value.eq_ignore_ascii_case("true"))
                .unwrap_or(false);
        let development_auto_approve = true
            && std::env::var("RUMI_AUTO_APPROVE_LOCAL")
                .map(|value| value.eq_ignore_ascii_case("true"))
                .unwrap_or(false);

        assert!(!production_auto_approve);
        assert!(development_auto_approve);

        std::env::remove_var("RUMI_AUTO_APPROVE_LOCAL");
    }

    #[test]
    fn retries_startup_after_recovering_stale_listener_during_health_check() {
        let progress = Arc::new(Mutex::new(String::new()));
        let mut start_calls = 0;
        let mut health_calls = 0;
        let mut recover_stages = Vec::new();

        let panel_code = run_startup_sequence(
            None,
            &progress,
            || {
                start_calls += 1;
                Ok(())
            },
            || {
                health_calls += 1;
                if health_calls == 1 {
                    Err(anyhow!("health failed"))
                } else {
                    Ok(())
                }
            },
            || Ok("panel-code".into()),
            |stage| {
                recover_stages.push(stage);
                Ok(Some("Recovered stale listener".into()))
            },
        )
        .unwrap();

        assert_eq!(panel_code, "panel-code");
        assert_eq!(start_calls, 2);
        assert_eq!(health_calls, 2);
        assert_eq!(recover_stages, vec![StartupRecoveryStage::HealthCheck]);
        assert_eq!(
            progress.lock().unwrap().as_str(),
            "Authorizing panel session..."
        );
    }

    #[test]
    fn retries_startup_after_recovering_stale_listener_during_bootstrap() {
        let progress = Arc::new(Mutex::new(String::new()));
        let mut start_calls = 0;
        let mut bootstrap_calls = 0;
        let mut recover_stages = Vec::new();

        let panel_code = run_startup_sequence(
            None,
            &progress,
            || {
                start_calls += 1;
                Ok(())
            },
            || Ok(()),
            || {
                bootstrap_calls += 1;
                if bootstrap_calls == 1 {
                    Err(anyhow!("bootstrap failed"))
                } else {
                    Ok("panel-code".into())
                }
            },
            |stage| {
                recover_stages.push(stage);
                Ok(Some("Recovered stale listener".into()))
            },
        )
        .unwrap();

        assert_eq!(panel_code, "panel-code");
        assert_eq!(start_calls, 2);
        assert_eq!(bootstrap_calls, 2);
        assert_eq!(recover_stages, vec![StartupRecoveryStage::Bootstrap]);
    }

    #[test]
    fn does_not_retry_when_conflict_recovery_rejects_foreign_listener() {
        let progress = Arc::new(Mutex::new(String::new()));
        let mut start_calls = 0;
        let mut recover_calls = 0;

        let error = run_startup_sequence(
            None,
            &progress,
            || {
                start_calls += 1;
                Ok(())
            },
            || Err(anyhow!("health failed")),
            || Ok("panel-code".into()),
            |stage| {
                recover_calls += 1;
                assert_eq!(stage, StartupRecoveryStage::HealthCheck);
                Err(anyhow!(
                    "port 8765 is already in use by pid 999 (foreign process)"
                ))
            },
        )
        .unwrap_err();

        assert_eq!(start_calls, 1);
        assert_eq!(recover_calls, 1);
        assert!(error
            .to_string()
            .contains("port 8765 is already in use by pid 999"));
    }
}
