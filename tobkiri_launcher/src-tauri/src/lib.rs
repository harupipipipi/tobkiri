//! Rumi Viewer — Tauri application library.
//!
//! V2: Full implementation with setup hook, commands, tray menu, and navigation guard.

mod artifact_integrity;
mod ci_e2e_app_data;
mod config;
mod debug_approval;
mod defaultspack_authority;
mod defaultspack_manager;
mod desktop_system_info;
mod health_check;
mod host_audit;
mod host_broker;
mod host_broker_types;
mod host_contract;
mod host_contract_contributions;
mod kernel_manager;
mod presentation;
mod process_utils;
mod python_env;
mod runtime_resource_integrity;
mod runtime_resource_paths;
mod sealed_python;
#[allow(dead_code)]
mod sealed_python_protocol;
mod shell_handoff;
mod shell_runtime;
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
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};

use config::AppConfig;
use debug_approval::{DebugApprovalManager, DebugApprovalStatus};
use defaultspack_manager::DefaultspackManager;
#[cfg(any(debug_assertions, test))]
use host_broker::DEFAULT_PORT as DEFAULT_HOST_BROKER_PORT;
use host_broker::{BrokerAttestationIdentity, HostBrokerRuntime};
use kernel_manager::KernelManager;

mod dock_registration;

/// Wrapper around a shared progress string, managed as Tauri State.
pub struct SetupProgress(pub Arc<Mutex<String>>);
pub struct ShutdownState(pub Arc<AtomicBool>);
pub struct AllowedNavigationPorts(pub Arc<Mutex<Vec<u16>>>);

const PRIMARY_WINDOW_LABELS: [&str; 2] = ["panel", "main"];
const DEFAULTSPACK_RESERVED_PORT: u16 = 8766;
const DEFAULTSPACK_MAIN_WINDOW_LABEL: &str = "defaultspack-main";
const AUTHORITY_APPROVAL_WINDOW_LABEL: &str = "authority-approval";
const AUTHORITY_APPROVAL_WINDOW_TITLE: &str = "Tobkiriの許可";
const AMBIENT_TRIGGER_WINDOW_LABEL: &str = "ambient-trigger";
const AMBIENT_TRIGGER_WINDOW_TITLE: &str = "合図待ち";
const AMBIENT_AUTHORITY_REQUEST_ID: &str = "rumi_ambient_trigger_pack";
const FINGER_RECORDING_WINDOW_LABEL: &str = "finger-recording";
const FINGER_RECORDING_WINDOW_TITLE: &str = "指で録音";
const DEFAULTS_CONSOLE_WINDOW_LABEL: &str = "defaults-console";
const DEFAULTS_CONSOLE_WINDOW_TITLE: &str = "詳細ログ";
const HOST_PERMISSIONS_WINDOW_LABEL: &str = "host-permissions";
const HOST_PERMISSIONS_WINDOW_TITLE: &str = "Tobkiri Launcher Host Permissions";
const AUTHORITY_UI_OPERATOR_TTL_SECONDS: u64 = 180;
const PANEL_SESSION_CALLER_DENIED: &str =
    "panel session renewal is unavailable from this Launcher window";
#[cfg(any(debug_assertions, test))]
const DEBUG_INSTANCE_ID_ENV: &str = "RUMI_VIEWER_DEBUG_INSTANCE_ID";
#[cfg(any(debug_assertions, test))]
const DEBUG_USER_DATA_ROOT_ENV: &str = "RUMI_VIEWER_DEBUG_USER_DATA_ROOT";
#[cfg(any(debug_assertions, test))]
const HOST_BROKER_CONNECTION_ENV: &str = "RUMI_VIEWER_HOST_BROKER_CONNECTION";
#[cfg(any(debug_assertions, test))]
const HOST_BROKER_PORT_ENV: &str = "RUMI_VIEWER_BROKER_PORT";
#[cfg(any(debug_assertions, test))]
const HOST_BROKER_INSTANCE_NONCE_ENV: &str = "RUMI_VIEWER_BROKER_INSTANCE_NONCE";
#[cfg(any(debug_assertions, test))]
const DEFAULTSPACK_DEBUG_ISOLATION_ENV: &str = "RUMI_DEFAULTSPACK_DEBUG_ISOLATION";
#[cfg(any(debug_assertions, test))]
const DEFAULTSPACK_DEBUG_RUN_ID_ENV: &str = "RUMI_DEFAULTSPACK_RUN_ID";
#[cfg(any(debug_assertions, test))]
const DEFAULTSPACK_DEBUG_LAUNCH_NONCE_ENV: &str = "RUMI_DEFAULTSPACK_LAUNCH_NONCE";
#[cfg(any(debug_assertions, test))]
const DEFAULTSPACK_DEBUG_STATE_ROOT_ENV: &str = "RUMI_DEFAULTSPACK_DEBUG_STATE_ROOT";
#[cfg(any(debug_assertions, test))]
const DEFAULTSPACK_DEBUG_HTTP_PORT_ENV: &str = "RUMI_DEFAULTSPACK_DEBUG_HTTP_PORT";
#[cfg(any(debug_assertions, test))]
const DEFAULTSPACK_DEBUG_KERNEL_PORT_ENV: &str = "RUMI_DEFAULTSPACK_DEBUG_KERNEL_PORT";
#[cfg(any(debug_assertions, test))]
const DEBUG_CACHE_ENVIRONMENTS: [(&str, &str); 6] = [
    ("PYTHONPYCACHEPREFIX", "python_bytecode"),
    ("PYTHONUSERBASE", "python_user_base"),
    ("PIP_CACHE_DIR", "pip_cache"),
    ("XDG_CACHE_HOME", "xdg_cache"),
    ("UV_CACHE_DIR", "uv_cache"),
    ("CARGO_TARGET_DIR", "cargo_target"),
];

/// Debug-only, non-authorizing identity for an isolated Viewer run.
///
/// This is deliberately not a credential. It only allows a debug build to skip
/// Tauri's process-global single-instance plugin after every other isolation
/// precondition has been checked. Release builds always return `None`.
#[cfg(any(debug_assertions, test))]
#[derive(Debug, Clone, PartialEq, Eq)]
struct DebugParallelInstancePolicy {
    supervisor_root: PathBuf,
    user_data_root: PathBuf,
    defaultspack_state_root: PathBuf,
    broker_port: u16,
    defaultspack_http_port: u16,
    kernel_port: u16,
}

#[cfg(any(debug_assertions, test))]
fn create_secure_debug_subdirectory(
    supervisor_root: &std::path::Path,
    name: &str,
) -> AnyResult<PathBuf> {
    let path = supervisor_root.join(name);
    match fs::symlink_metadata(&path) {
        Ok(_) => {
            if !secure_debug_directory(&path, Some(name))
                || !debug_directories_have_same_owner(&[supervisor_root, &path])
            {
                bail!("debug cache directory failed native ownership checks");
            }
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            let mut builder = fs::DirBuilder::new();
            #[cfg(unix)]
            {
                use std::os::unix::fs::DirBuilderExt;

                builder.mode(0o700);
            }
            builder
                .create(&path)
                .context("failed to create isolated debug cache directory")?;
            if !secure_debug_directory(&path, Some(name))
                || !debug_directories_have_same_owner(&[supervisor_root, &path])
            {
                bail!("created debug cache directory failed native ownership checks");
            }
        }
        Err(error) => return Err(error).context("failed to inspect debug cache directory"),
    }
    Ok(path)
}

#[cfg(any(debug_assertions, test))]
fn prepare_debug_cache_environment(
    policy: &DebugParallelInstancePolicy,
) -> AnyResult<Vec<(&'static str, PathBuf)>> {
    DEBUG_CACHE_ENVIRONMENTS
        .iter()
        .map(|(key, name)| {
            create_secure_debug_subdirectory(&policy.supervisor_root, name).map(|path| (*key, path))
        })
        .collect()
}

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
    #[serde(skip_serializing_if = "Option::is_none")]
    decision: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    request_snapshot_digest: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    typed_confirmation_digest: Option<String>,
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

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
struct CodingUiOperator {
    version: u8,
    kind: String,
    origin: String,
    instance_nonce: String,
    window_label: String,
    request_id: String,
    expected_digest: String,
    decision: String,
    issued_at: u64,
    expires_at: u64,
    nonce: String,
    signature: String,
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

#[tauri::command]
fn debug_approval_status(
    state: tauri::State<'_, Arc<DebugApprovalManager>>,
) -> DebugApprovalStatus {
    state.status()
}

fn validate_debug_approval_window(window: &tauri::WebviewWindow) -> Result<(), String> {
    if window.label() != "main" {
        return Err("debug approval can only be changed from the Launcher main window".into());
    }
    let url = window
        .url()
        .map_err(|error| format!("failed to inspect Launcher URL: {error}"))?;
    let local_launcher = matches!(url.scheme(), "tauri" | "http" | "https")
        && matches!(
            url.host_str().unwrap_or(""),
            "localhost" | "127.0.0.1" | "tauri.localhost"
        );
    if !local_launcher || url.path() == "/approval" {
        return Err("debug approval is unavailable from this Launcher route".into());
    }
    Ok(())
}

#[tauri::command]
async fn arm_debug_approval(
    duration: String,
    window: tauri::WebviewWindow,
    state: tauri::State<'_, Arc<DebugApprovalManager>>,
) -> Result<DebugApprovalStatus, String> {
    validate_debug_approval_window(&window)?;
    let pending = state.status();
    if matches!(pending.state.as_str(), "armed" | "active") {
        return state.arm(&duration);
    }
    if pending.state != "pending" {
        return Err("start a CLI debug session request before enabling".into());
    }
    let duration_label = match duration.as_str() {
        "1h" => "1時間",
        "1d" => "1日",
        "1w" => "1週間",
        "1mo" => "1か月",
        "permanent" => "無期限（手動OFF・Launcher終了・guardian終了まで）",
        _ => return Err("invalid debug approval duration".into()),
    };
    let message = format!(
        "この1つのCLIデバッグセッションだけに個別承認を委任します。\n\n利用期間: {}\nWorkspace: {}\nPack / Profile: {} / {}\nRun: {}\nGuardian: Launcher-owned Defaultspack child（検証済み）\n\n承認後も各操作は個別のdigestに束縛されます。",
        duration_label,
        pending.workspace.as_deref().unwrap_or("unknown"),
        pending.pack_id.as_deref().unwrap_or("unknown"),
        pending.profile_id.as_deref().unwrap_or("unknown"),
        pending.run_id.as_deref().unwrap_or("unknown"),
    );
    let confirmed = window
        .dialog()
        .message(message)
        .title("Developer Debug Approvalを有効にしますか？")
        .kind(MessageDialogKind::Warning)
        .buttons(MessageDialogButtons::OkCancelCustom(
            format!("{duration_label}だけ有効化"),
            "キャンセル".into(),
        ))
        .blocking_show();
    if !confirmed {
        return Err("native confirmation was cancelled".into());
    }
    state.arm(&duration)
}

#[tauri::command]
fn revoke_debug_approval(
    window: tauri::WebviewWindow,
    state: tauri::State<'_, Arc<DebugApprovalManager>>,
) -> Result<DebugApprovalStatus, String> {
    validate_debug_approval_window(&window)?;
    state.revoke("user_revoked")
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
    window: tauri::WebviewWindow,
    config: tauri::State<'_, AppConfig>,
    km: tauri::State<'_, Arc<Mutex<KernelManager>>>,
) -> Result<String, String> {
    validate_panel_session_caller(&window, config.inner())?;
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

fn defaultspack_http_port_for_urls(isolated_port: Option<u16>) -> u16 {
    isolated_port.unwrap_or(DEFAULTSPACK_RESERVED_PORT)
}

fn active_defaultspack_http_port() -> u16 {
    defaultspack_http_port_for_urls(
        debug_defaultspack_ports_from_env().map(|(http_port, _)| http_port),
    )
}

fn authority_approval_url(request_id: &str) -> Result<Url, String> {
    if !valid_authority_request_id(request_id) {
        return Err("invalid authority request id".into());
    }
    Url::parse_with_params(
        &format!(
            "http://127.0.0.1:{}/approval",
            active_defaultspack_http_port()
        ),
        &[("request_id", request_id.trim())],
    )
    .map_err(|error| format!("failed to build approval window URL: {error}"))
}

fn ambient_trigger_url() -> Result<Url, String> {
    Url::parse(&format!(
        "http://127.0.0.1:{}/ambient",
        active_defaultspack_http_port()
    ))
    .map_err(|error| format!("failed to build ambient trigger window URL: {error}"))
}

fn finger_recording_url() -> Result<Url, String> {
    Url::parse(&format!(
        "http://127.0.0.1:{}/finger-recording",
        active_defaultspack_http_port()
    ))
    .map_err(|error| format!("failed to build finger recording window URL: {error}"))
}

fn defaults_console_url() -> Result<Url, String> {
    Url::parse(&format!(
        "http://127.0.0.1:{}/console",
        active_defaultspack_http_port()
    ))
    .map_err(|error| format!("failed to build defaults console window URL: {error}"))
}

fn host_permissions_url() -> Result<Url, String> {
    Url::parse(&format!(
        "http://127.0.0.1:{}/host-permissions",
        active_defaultspack_http_port()
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

fn validate_authority_approval_open_caller(
    window_label: &str,
    focused: bool,
    current_url: &Url,
    expected_port: u16,
) -> Result<(), String> {
    if !focused {
        return Err("opening an approval window requires the focused caller window".into());
    }
    if current_url.scheme() != "http"
        || !current_url.username().is_empty()
        || current_url.password().is_some()
        || current_url.host_str() != Some("127.0.0.1")
        || current_url.port_or_known_default() != Some(expected_port)
    {
        return Err("approval window is unavailable from this caller origin".into());
    }
    let path_allowed = match window_label {
        DEFAULTSPACK_MAIN_WINDOW_LABEL => matches!(
            current_url.path(),
            "/" | "/chat"
                | "/defaultspack"
                | "/pack/defaultspack"
                | "/coding"
                | "/calendar"
                | "/kanban"
                | "/desktops"
                | "/subagents"
                | "/canvas"
                | "/tools"
        ),
        AMBIENT_TRIGGER_WINDOW_LABEL => current_url.path() == "/ambient",
        FINGER_RECORDING_WINDOW_LABEL => current_url.path() == "/finger-recording",
        _ => false,
    };
    if !path_allowed {
        return Err("approval window is unavailable from this caller route".into());
    }
    Ok(())
}

#[tauri::command]
async fn open_authority_approval_window(
    app: AppHandle,
    window: tauri::WebviewWindow,
    config: tauri::State<'_, AppConfig>,
    request_id: String,
) -> Result<(), String> {
    let focused = window
        .is_focused()
        .map_err(|error| format!("failed to inspect approval caller focus: {error}"))?;
    let current_url = window
        .url()
        .map_err(|error| format!("failed to inspect approval caller URL: {error}"))?;
    validate_authority_approval_open_caller(
        window.label(),
        focused,
        &current_url,
        active_defaultspack_http_port(),
    )?;
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
const DEBUG_DEFAULTSPACK_CONTRACT_PREFIX: &str = "/api/contracts/defaultspack/";

/// One authenticated, short-lived panel session used only by the debug native
/// approval smoke.  The cookie and CSRF token stay in this thread and are
/// never logged or exposed to the approval window.
#[cfg(debug_assertions)]
struct DebugPanelSession {
    cookie: String,
    csrf_token: String,
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
fn debug_contract_url(base_url: &str, method: &str, api_path: &str) -> AnyResult<String> {
    if !matches!(method, "GET" | "POST")
        || !api_path.starts_with("/api/")
        || api_path.contains(['?', '#', '\\'])
    {
        bail!("debug approval smoke contract route is invalid");
    }
    // The production web client sends an opaque `METHOD /api/...` token.  A
    // slash must be percent-encoded too: the Host rejects a token containing a
    // path separator before it decodes the exact signed route.
    let encoded_path = api_path.replace('/', "%2F");
    Ok(format!(
        "{base_url}{DEBUG_DEFAULTSPACK_CONTRACT_PREFIX}{method}%20{encoded_path}"
    ))
}

#[cfg(debug_assertions)]
fn debug_contract_request_id() -> String {
    let mut bytes = [0_u8; 16];
    rand::thread_rng().fill(&mut bytes);
    // RFC 4122 UUIDv4: PackAPI's replay guard deliberately requires this
    // format rather than accepting arbitrary client correlation IDs.
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    format!(
        "{:02x}{:02x}{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}{:02x}{:02x}{:02x}{:02x}",
        bytes[0],
        bytes[1],
        bytes[2],
        bytes[3],
        bytes[4],
        bytes[5],
        bytes[6],
        bytes[7],
        bytes[8],
        bytes[9],
        bytes[10],
        bytes[11],
        bytes[12],
        bytes[13],
        bytes[14],
        bytes[15],
    )
}

#[cfg(debug_assertions)]
fn debug_panel_session(
    client: &reqwest::blocking::Client,
    base_url: &str,
    bootstrap_secret: &str,
) -> AnyResult<DebugPanelSession> {
    let bootstrap = client
        .post(format!("{base_url}/api/panel/auth/bootstrap"))
        .header("X-Rumi-Desktop-Bootstrap", bootstrap_secret)
        .send()
        .context("debug approval smoke panel bootstrap request failed")?;
    let bootstrap_status = bootstrap.status();
    let bootstrap: ApiEnvelope<PanelBootstrapPayload> = bootstrap
        .json()
        .context("debug approval smoke panel bootstrap response was invalid")?;
    if !bootstrap_status.is_success() || !bootstrap.success {
        bail!("debug approval smoke panel bootstrap was rejected");
    }
    let code = bootstrap
        .data
        .context("debug approval smoke panel bootstrap response had no code")?
        .code;
    if code.is_empty() {
        bail!("debug approval smoke panel bootstrap response had an empty code");
    }

    let exchange = client
        .post(format!("{base_url}/api/panel/auth/exchange"))
        .header(reqwest::header::ORIGIN, base_url)
        .json(&serde_json::json!({ "code": code }))
        .send()
        .context("debug approval smoke panel exchange request failed")?;
    let exchange_status = exchange.status();
    let cookie = exchange
        .headers()
        .get(reqwest::header::SET_COOKIE)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.split(';').next())
        .filter(|value| value.starts_with("rumi_panel_session="))
        .map(str::to_owned)
        .context("debug approval smoke panel exchange had no session cookie")?;
    let exchange: ApiEnvelope<serde_json::Value> = exchange
        .json()
        .context("debug approval smoke panel exchange response was invalid")?;
    if !exchange_status.is_success() || !exchange.success {
        bail!("debug approval smoke panel exchange was rejected");
    }
    let csrf_token = exchange
        .data
        .as_ref()
        .and_then(|data| data.get("csrf_token"))
        .and_then(serde_json::Value::as_str)
        .filter(|value| !value.is_empty())
        .map(str::to_owned)
        .context("debug approval smoke panel exchange had no CSRF token")?;
    Ok(DebugPanelSession { cookie, csrf_token })
}

#[cfg(debug_assertions)]
fn debug_contract_request(
    client: &reqwest::blocking::Client,
    base_url: &str,
    session: &DebugPanelSession,
    method: &str,
    api_path: &str,
    payload: Option<serde_json::Value>,
) -> AnyResult<serde_json::Value> {
    let url = debug_contract_url(base_url, method, api_path)?;
    let mut request = client
        .request(
            reqwest::Method::from_bytes(method.as_bytes())
                .context("debug approval smoke contract method was invalid")?,
            url,
        )
        .header(reqwest::header::ORIGIN, base_url)
        .header(reqwest::header::COOKIE, &session.cookie)
        .header("X-Rumi-CSRF", &session.csrf_token)
        .header("X-Tobkiri-Request-ID", debug_contract_request_id());
    if let Some(payload) = payload {
        request = request.json(&payload);
    }
    let response = request
        .send()
        .context("debug approval smoke contract request failed")?;
    let status = response.status();
    let envelope: ApiEnvelope<serde_json::Value> = response
        .json()
        .context("debug approval smoke contract response was invalid")?;
    if !status.is_success() || !envelope.success {
        bail!("debug approval smoke contract operation was rejected");
    }
    envelope
        .data
        .context("debug approval smoke contract response had no result")
}

#[cfg(debug_assertions)]
fn debug_pending_interactive_request_id(value: &serde_json::Value) -> Option<&str> {
    value
        .get("approval_request_id")
        .and_then(serde_json::Value::as_str)
        .filter(|request_id| valid_authority_request_id(request_id))
}

#[cfg(debug_assertions)]
fn debug_result_state(value: &serde_json::Value) -> Option<&str> {
    value.get("state").and_then(serde_json::Value::as_str)
}

/// Finish one debug-only native approval smoke through the same captured V4
/// client path that prepared it.
///
/// The approval window alone may settle the Host approval record but it cannot
/// dispatch the deferred command.  A real command client resumes its own
/// invocation after that decision.  This monitor mirrors that one client
/// responsibility for the `RUMI_AUTHORITY_TEST_AUTORUN` smoke only; it never
/// provides a UI operator, changes production routes, or retries a resumed
/// effect.  The Host remains the sole authority that decides whether resume
/// can execute the effect.
#[cfg(debug_assertions)]
fn monitor_debug_authority_smoke_settlement(
    client: &reqwest::blocking::Client,
    base_url: &str,
    session: &DebugPanelSession,
    request_id: &str,
    invocation_id: &str,
) {
    let deadline = SystemTime::now() + Duration::from_secs(120);
    while SystemTime::now() < deadline {
        let approval = match debug_contract_request(
            client,
            base_url,
            session,
            "POST",
            "/api/interactive-approval/v1/get",
            Some(serde_json::json!({ "request_id": request_id })),
        ) {
            Ok(approval) => approval,
            Err(error) => {
                warn!("debug approval smoke could not refresh its approval state: {error}");
                return;
            }
        };
        match debug_result_state(&approval) {
            Some("approval_pending" | "pending") => {
                thread::sleep(Duration::from_millis(300));
            }
            Some("approved") => {
                let resumed = match debug_contract_request(
                    client,
                    base_url,
                    session,
                    "POST",
                    "/api/command-protocol/v1/high-risk",
                    Some(serde_json::json!({
                        "phase": "resume",
                        "invocation_id": invocation_id,
                    })),
                ) {
                    Ok(result) => result,
                    Err(error) => {
                        warn!(
                            "debug approval smoke could not resume its approved command: {error}"
                        );
                        return;
                    }
                };
                if debug_result_state(&resumed) != Some("succeeded") {
                    warn!(
                        "debug approval smoke approved command did not settle successfully: {}",
                        debug_result_state(&resumed).unwrap_or("unknown")
                    );
                    return;
                }
                let status = match debug_contract_request(
                    client,
                    base_url,
                    session,
                    "POST",
                    "/api/command-protocol/v1/high-risk",
                    Some(serde_json::json!({
                        "phase": "status",
                        "invocation_id": invocation_id,
                    })),
                ) {
                    Ok(result) => result,
                    Err(error) => {
                        warn!("debug approval smoke could not verify its settled command: {error}");
                        return;
                    }
                };
                if debug_result_state(&status) == Some("succeeded") {
                    info!(
                        "debug approval smoke approved and resumed exactly once for request {request_id}"
                    );
                } else {
                    warn!(
                        "debug approval smoke approved command status was not succeeded: {}",
                        debug_result_state(&status).unwrap_or("unknown")
                    );
                }
                return;
            }
            Some("denied" | "expired") => {
                let cancelled = match debug_contract_request(
                    client,
                    base_url,
                    session,
                    "POST",
                    "/api/command-protocol/v1/high-risk",
                    Some(serde_json::json!({
                        "phase": "cancel",
                        "invocation_id": invocation_id,
                    })),
                ) {
                    Ok(result) => result,
                    Err(error) => {
                        warn!(
                            "debug approval smoke could not cancel its declined command: {error}"
                        );
                        return;
                    }
                };
                match debug_result_state(&cancelled) {
                    Some("cancelled" | "stale") => info!(
                        "debug approval smoke declined request settled without dispatch for request {request_id}"
                    ),
                    state => warn!(
                        "debug approval smoke declined command had an unexpected terminal state: {}",
                        state.unwrap_or("unknown")
                    ),
                }
                return;
            }
            Some(state) => {
                warn!("debug approval smoke saw an unexpected approval state: {state}");
                return;
            }
            None => {
                warn!("debug approval smoke approval state was missing");
                return;
            }
        }
    }
    warn!("debug approval smoke timed out waiting for native approval settlement");
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
        let base_url = format!("http://127.0.0.1:{}", active_defaultspack_http_port());
        let health_url = format!("{base_url}/health");
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

        let config = app.state::<AppConfig>().inner().clone();
        let bootstrap_secret = match load_or_create_panel_bootstrap_secret(&config) {
            Ok(secret) => secret,
            Err(error) => {
                warn!("debug approval smoke could not load panel bootstrap secret: {error}");
                return;
            }
        };
        let session = match debug_panel_session(&client, &base_url, &bootstrap_secret) {
            Ok(session) => session,
            Err(error) => {
                warn!("debug approval smoke could not establish its panel session: {error}");
                return;
            }
        };

        // This is the real signed V4 high-risk path. `true` is a
        // Host-allowlisted no-op when the Host later resumes the effect; it
        // still exercises prepare -> pending approval -> single-use resume
        // without a test-only authority bypass or a retired endpoint.  The
        // relative cwd deliberately asks the Host to use the already selected
        // trusted workspace instead of letting this debug client choose one.
        let invocation_id = format!(
            "debug-native-{}",
            rand::thread_rng()
                .sample_iter(&Alphanumeric)
                .take(24)
                .map(char::from)
                .collect::<String>()
        );
        let prepared = match debug_contract_request(
            &client,
            &base_url,
            &session,
            "POST",
            "/api/command-protocol/v1/high-risk",
            Some(serde_json::json!({
                "phase": "prepare",
                "invocation_id": invocation_id,
                "command_ref": "terminal",
                "arguments": {
                    "command": ["true"],
                    "cwd": ".",
                    "env": {},
                    "timeout": 30
                },
                "presentation": {
                    "title": "Tobkiri native approval smoke (debug)",
                    "summary": "A debug-only no-op command is awaiting one interactive approval."
                }
            })),
        ) {
            Ok(result) => result,
            Err(error) => {
                warn!("debug approval smoke high-risk prepare failed: {error}");
                return;
            }
        };
        let request_id = match debug_pending_interactive_request_id(&prepared) {
            Some(request_id) => request_id.to_string(),
            None => {
                warn!("debug approval smoke high-risk prepare did not return a pending request");
                return;
            }
        };
        if !matches!(
            prepared.get("state").and_then(serde_json::Value::as_str),
            Some("approval_pending" | "pending")
        ) {
            warn!("debug approval smoke high-risk prepare returned a non-pending state");
            return;
        }
        let fetched = match debug_contract_request(
            &client,
            &base_url,
            &session,
            "POST",
            "/api/interactive-approval/v1/get",
            Some(serde_json::json!({ "request_id": request_id })),
        ) {
            Ok(result) => result,
            Err(error) => {
                warn!("debug approval smoke could not read its pending request: {error}");
                return;
            }
        };
        let listed = match debug_contract_request(
            &client,
            &base_url,
            &session,
            "GET",
            "/api/interactive-approval/v1/list",
            None,
        ) {
            Ok(result) => result,
            Err(error) => {
                warn!("debug approval smoke could not list pending requests: {error}");
                return;
            }
        };
        let fetched_is_pending = fetched
            .get("request_id")
            .and_then(serde_json::Value::as_str)
            .is_some_and(|candidate| candidate == request_id)
            && matches!(
                fetched.get("state").and_then(serde_json::Value::as_str),
                Some("approval_pending" | "pending")
            );
        let listed_is_pending = listed
            .get("approvals")
            .and_then(serde_json::Value::as_array)
            .is_some_and(|approvals| {
                approvals.iter().any(|approval| {
                    approval
                        .get("request_id")
                        .and_then(serde_json::Value::as_str)
                        == Some(request_id.as_str())
                        && matches!(
                            approval.get("state").and_then(serde_json::Value::as_str),
                            Some("approval_pending" | "pending")
                        )
                })
            });
        if !fetched_is_pending || !listed_is_pending {
            warn!("debug approval smoke pending request did not survive authoritative get/list");
            return;
        }

        thread::sleep(Duration::from_secs(2));
        let app_for_open = app.clone();
        let config_for_open = config;
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
            return;
        }

        monitor_debug_authority_smoke_settlement(
            &client,
            &base_url,
            &session,
            &request_id,
            &invocation_id,
        );
    });
}

fn unix_now_seconds() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_else(|_| Duration::from_secs(0))
        .as_secs()
}

fn authority_operator_message(operator: &AuthorityUiOperator) -> String {
    let mut fields = vec![
        format!("v{}", operator.version),
        operator.origin.clone(),
        operator.window_label.clone(),
        operator.request_id.clone(),
    ];
    if operator.version == 3 {
        fields.extend([
            operator.decision.clone().unwrap_or_default(),
            operator.request_snapshot_digest.clone().unwrap_or_default(),
            operator
                .typed_confirmation_digest
                .clone()
                .unwrap_or_default(),
        ]);
    }
    fields.extend([
        operator.issued_at.to_string(),
        operator.expires_at.to_string(),
        operator.nonce.clone(),
    ]);
    fields.join("\n")
}

fn coding_operator_message(operator: &CodingUiOperator) -> String {
    [
        format!("v{}", operator.version),
        operator.origin.clone(),
        operator.instance_nonce.clone(),
        operator.window_label.clone(),
        operator.request_id.clone(),
        operator.expected_digest.clone(),
        operator.decision.clone(),
        operator.issued_at.to_string(),
        operator.expires_at.to_string(),
        operator.nonce.clone(),
    ]
    .join("\n")
}

#[tauri::command]
async fn coding_approval_operator(
    window: tauri::WebviewWindow,
    attestation: tauri::State<'_, BrokerAttestationIdentity>,
    request_id: String,
    expected_digest: String,
    decision: String,
) -> Result<CodingUiOperator, String> {
    if window.label() != "defaultspack-main" {
        return Err("coding approval is only available in the Defaultspack Launcher window".into());
    }
    if !window
        .is_focused()
        .map_err(|error| format!("failed to inspect Defaultspack focus: {error}"))?
    {
        return Err("Defaultspack approval window must be focused".into());
    }
    let url = window
        .url()
        .map_err(|error| format!("failed to inspect Defaultspack URL: {error}"))?;
    if !matches!(url.host_str().unwrap_or(""), "127.0.0.1" | "localhost")
        || url.port_or_known_default() != Some(DEFAULTSPACK_RESERVED_PORT)
    {
        return Err("coding approval is unavailable from this window origin".into());
    }
    if !valid_authority_request_id(&request_id)
        || expected_digest.len() != 64
        || !expected_digest.bytes().all(|byte| byte.is_ascii_hexdigit())
        || !matches!(decision.as_str(), "approve" | "deny")
    {
        return Err("coding approval binding is invalid".into());
    }
    let confirmed = window
        .dialog()
        .message(format!(
            "{} request {}\nDigest: {}\n\nこのexact requestだけに適用します。",
            if decision == "approve" {
                "Approve"
            } else {
                "Deny"
            },
            request_id,
            expected_digest,
        ))
        .title("Tobkiri coding approval")
        .kind(MessageDialogKind::Warning)
        .buttons(MessageDialogButtons::OkCancelCustom(
            if decision == "approve" {
                "Approve once".into()
            } else {
                "Deny".into()
            },
            "Cancel".into(),
        ))
        .blocking_show();
    if !confirmed {
        return Err("native coding approval was cancelled".into());
    }
    let issued_at = unix_now_seconds();
    let nonce: String = rand::thread_rng()
        .sample_iter(&Alphanumeric)
        .take(32)
        .map(char::from)
        .collect();
    let mut operator = CodingUiOperator {
        version: 4,
        kind: "coding_ui_operator".into(),
        origin: "tauri_webview_window".into(),
        instance_nonce: attestation.instance_nonce().into(),
        window_label: "defaultspack-main".into(),
        request_id,
        expected_digest,
        decision,
        issued_at,
        expires_at: issued_at + 60,
        nonce,
        signature: String::new(),
    };
    operator.signature =
        attestation.sign_message_base64(coding_operator_message(&operator).as_bytes());
    Ok(operator)
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
        decision: None,
        request_snapshot_digest: None,
        typed_confirmation_digest: None,
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

fn valid_authority_digest(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn sign_interactive_authority_ui_operator(
    request_id: &str,
    decision: &str,
    request_snapshot_digest: &str,
    typed_confirmation_digest: Option<&str>,
    bootstrap_secret: &str,
    now: u64,
    nonce: String,
) -> Result<AuthorityUiOperator, String> {
    if !valid_authority_request_id(request_id) {
        return Err("invalid authority request id".into());
    }
    if !matches!(decision, "approve" | "deny")
        || !valid_authority_digest(request_snapshot_digest)
        || typed_confirmation_digest.is_some_and(|digest| !valid_authority_digest(digest))
        || decision == "deny" && typed_confirmation_digest.is_some()
    {
        return Err("interactive approval binding is invalid".into());
    }
    if bootstrap_secret.trim().is_empty() {
        return Err("approval signing secret is unavailable".into());
    }
    let mut operator = AuthorityUiOperator {
        version: 3,
        kind: "ui_operator".into(),
        origin: "tauri_webview_window".into(),
        window_label: AUTHORITY_APPROVAL_WINDOW_LABEL.into(),
        request_id: request_id.trim().into(),
        decision: Some(decision.into()),
        request_snapshot_digest: Some(request_snapshot_digest.into()),
        typed_confirmation_digest: typed_confirmation_digest.map(str::to_string),
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

fn validate_authority_approval_context_caller(
    window_label: &str,
    focused: bool,
    current_url: &Url,
    request_id: &str,
    expected_port: u16,
) -> Result<(), String> {
    if window_label != AUTHORITY_APPROVAL_WINDOW_LABEL {
        return Err("approval context is only available in the approval window".into());
    }
    if !focused {
        return Err("approval context requires the focused approval window".into());
    }
    if current_url.scheme() != "http"
        || current_url.host_str() != Some("127.0.0.1")
        || current_url.port_or_known_default() != Some(expected_port)
        || current_url.path() != "/approval"
    {
        return Err("approval context is only available on the local approval route".into());
    }
    if !valid_authority_request_id(request_id) {
        return Err("invalid authority request id".into());
    }
    let query_pairs = current_url.query_pairs().collect::<Vec<_>>();
    if query_pairs.len() != 1
        || query_pairs[0].0 != "request_id"
        || request_id.trim() != query_pairs[0].1
    {
        return Err("approval context request id does not match the approval window URL".into());
    }
    Ok(())
}

#[tauri::command]
fn authority_approval_context(
    window: tauri::WebviewWindow,
    config: tauri::State<'_, AppConfig>,
    request_id: String,
    decision: Option<String>,
    request_snapshot_digest: Option<String>,
    typed_confirmation_digest: Option<String>,
) -> Result<AuthorityApprovalContext, String> {
    let focused = window
        .is_focused()
        .map_err(|error| format!("failed to inspect approval window focus: {error}"))?;
    let current_url = window
        .url()
        .map_err(|error| format!("failed to inspect approval window URL: {error}"))?;
    let request_id = request_id.trim().to_string();
    validate_authority_approval_context_caller(
        window.label(),
        focused,
        &current_url,
        &request_id,
        active_defaultspack_http_port(),
    )?;
    let bootstrap_secret = load_or_create_panel_bootstrap_secret(&config)
        .map_err(|error| format!("failed to load approval signing secret: {error}"))?;
    let nonce: String = rand::thread_rng()
        .sample_iter(&Alphanumeric)
        .take(32)
        .map(char::from)
        .collect();
    let operator = match (decision, request_snapshot_digest) {
        (None, None)
            if request_id == AMBIENT_AUTHORITY_REQUEST_ID
                && typed_confirmation_digest.is_none() =>
        {
            sign_authority_ui_operator(&request_id, &bootstrap_secret, unix_now_seconds(), nonce)?
        }
        (Some(decision), Some(request_snapshot_digest))
            if request_id != AMBIENT_AUTHORITY_REQUEST_ID =>
        {
            sign_interactive_authority_ui_operator(
                &request_id,
                &decision,
                &request_snapshot_digest,
                typed_confirmation_digest.as_deref(),
                &bootstrap_secret,
                unix_now_seconds(),
                nonce,
            )?
        }
        _ => return Err("interactive approval binding is incomplete".into()),
    };
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

/// The session-renewal IPC endpoint returns a bootstrap credential.  Tauri's
/// capability is the first gate, while this live caller check prevents another
/// allowed loopback document or a misconfigured capability from minting one.
fn validate_panel_session_caller_context(
    window_label: &str,
    url: &Url,
    configured_port: u16,
) -> Result<(), &'static str> {
    if window_label != "main"
        || configured_port == 0
        || url.scheme() != "http"
        || !matches!(url.host_str(), Some("127.0.0.1") | Some("localhost"))
        || url.port_or_known_default() != Some(configured_port)
        || (url.path() != "/panel" && !url.path().starts_with("/panel/"))
    {
        return Err(PANEL_SESSION_CALLER_DENIED);
    }
    Ok(())
}

fn validate_panel_session_caller(
    window: &tauri::WebviewWindow,
    config: &AppConfig,
) -> Result<(), String> {
    let url = window.url().map_err(|error| {
        // Never include the URL in diagnostics: it can carry a short-lived
        // bootstrap code while the panel is exchanging its session.
        warn!("panel session renewal caller inspection failed: {error}");
        PANEL_SESSION_CALLER_DENIED.to_string()
    })?;
    validate_panel_session_caller_context(window.label(), &url, config.kernel_port).map_err(
        |message| {
            warn!(
                "panel session renewal denied: caller_class={}",
                if window.label() == "main" {
                    "main"
                } else {
                    "non_main"
                }
            );
            message.to_string()
        },
    )
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
    // `/panel/` is the canonical authenticated entry point. The frontend
    // selects setup or the active panel from the authenticated lifecycle
    // state after exchanging this bootstrap code.
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
                        navigate_window_to_panel_session(
                            &win,
                            config.kernel_port,
                            &panel_code,
                        )
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
                        if let Err(error) = navigate_window_to_panel_session(
                            &win,
                            config.kernel_port,
                            &panel_code,
                        )
                        {
                            error!("Failed to refresh panel after Kernel restart: {error}");
                        }
                    }
                    // Exit 42 is a contract-transition handoff.  The new
                    // Kernel has been given a freshly projected authority and
                    // the old WebView cookie is intentionally not reused.
                    // Restart guardian preparation only after the new panel
                    // code was minted from that fresh authenticated process.
                    prepare_defaultspack_guardian_in_background(
                        app.clone(),
                        config.clone(),
                        panel_bootstrap_secret.clone(),
                    );
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
        "Error: {stage} failed — {error:#}. See {}",
        log_path.display()
    )
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum StartupRecoveryStage {
    HealthCheck,
    Bootstrap,
}

fn capture_guardian_kernel_generation(app: &AppHandle) -> Option<u64> {
    let manager = Arc::clone(app.state::<Arc<Mutex<KernelManager>>>().inner());
    let generation = match manager.lock() {
        Ok(kernel) => Some(kernel.launch_generation()),
        Err(error) => {
            error!("Failed to capture Kernel generation for Defaultspack guardian: {error}");
            None
        }
    };
    generation
}

fn guardian_kernel_generation_is_current(app: &AppHandle, generation: u64) -> bool {
    let manager = Arc::clone(app.state::<Arc<Mutex<KernelManager>>>().inner());
    let is_current = match manager.lock() {
        Ok(mut kernel) => kernel.is_current_launch_generation(generation),
        Err(error) => {
            error!("Failed to validate Kernel generation for Defaultspack guardian: {error}");
            false
        }
    };
    is_current
}

fn prepare_defaultspack_guardian_in_background(
    app: AppHandle,
    config: AppConfig,
    panel_bootstrap_secret: String,
) {
    let Some(expected_generation) = capture_guardian_kernel_generation(&app) else {
        return;
    };
    thread::spawn(move || {
        if !guardian_kernel_generation_is_current(&app, expected_generation) {
            info!(
                "Skipping stale Defaultspack guardian task for Kernel generation {expected_generation}"
            );
            return;
        }
        match health_check::check_authenticated_runtime_ready(
            config.kernel_port,
            &panel_bootstrap_secret,
        ) {
            Ok(true) => {}
            Ok(false) => {
                info!("Deferring Launcher-owned Defaultspack guardian until runtime activation");
                return;
            }
            Err(error) => {
                error!(
                    "Failed to verify runtime readiness before preparing Defaultspack guardian: {error:#}"
                );
                return;
            }
        }
        if !guardian_kernel_generation_is_current(&app, expected_generation) {
            info!(
                "Skipping stale Defaultspack guardian task after readiness for Kernel generation {expected_generation}"
            );
            return;
        }
        if let Err(error) = dock_registration::prepare_defaultspack_guardian_impl(&app, &config) {
            error!("Failed to prepare Launcher-owned Defaultspack guardian: {error:#}");
        }
    });
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

fn startup_stage_name(stage: &Arc<Mutex<&'static str>>) -> &'static str {
    stage
        .lock()
        .map(|current| *current)
        .unwrap_or("unavailable")
}

fn record_startup_stage(stage: &Arc<Mutex<&'static str>>, next: &'static str) {
    if let Ok(mut current) = stage.lock() {
        *current = next;
    }
    // Keep this intentionally structural: startup logs must never include
    // broker tokens, approval tokens, connection payloads, or environment
    // values.
    info!("Viewer startup stage={next}");
}

#[cfg(any(debug_assertions, test))]
fn ascii_decimal_port(value: &str) -> Option<u16> {
    if value.is_empty() || !value.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    value.parse::<u16>().ok().filter(|port| *port != 0)
}

#[cfg(any(debug_assertions, test))]
fn is_clean_absolute_path(path: &std::path::Path) -> bool {
    path.is_absolute()
        && path.components().all(|component| {
            matches!(
                component,
                std::path::Component::RootDir | std::path::Component::Normal(_)
            ) || cfg!(windows) && matches!(component, std::path::Component::Prefix(_))
        })
}

#[cfg(any(debug_assertions, test))]
fn secure_debug_directory(path: &std::path::Path, expected_name: Option<&str>) -> bool {
    if !is_clean_absolute_path(path)
        || expected_name
            .is_some_and(|name| path.file_name().and_then(|part| part.to_str()) != Some(name))
    {
        return false;
    }
    let Ok(metadata) = fs::symlink_metadata(path) else {
        return false;
    };
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        return false;
    }
    if path.canonicalize().ok().as_deref() != Some(path) {
        return false;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;

        if metadata.permissions().mode() & 0o777 != 0o700 {
            return false;
        }
    }
    true
}

#[cfg(all(any(debug_assertions, test), unix))]
fn debug_directories_have_same_owner(paths: &[&std::path::Path]) -> bool {
    use std::os::unix::fs::MetadataExt;

    unsafe extern "C" {
        fn geteuid() -> u32;
    }

    let mut owners = paths
        .iter()
        .filter_map(|path| fs::metadata(path).ok().map(|item| item.uid()));
    let Some(owner) = owners.next() else {
        return false;
    };
    // POSIX geteuid has no preconditions and does not dereference memory.
    let effective_user = unsafe { geteuid() };
    owner == effective_user && owners.all(|candidate| candidate == owner)
}

#[cfg(all(any(debug_assertions, test), not(unix)))]
fn debug_directories_have_same_owner(_paths: &[&std::path::Path]) -> bool {
    true
}

#[cfg(any(debug_assertions, test))]
fn valid_debug_instance_id(value: &str) -> bool {
    let Some(suffix) = value.strip_prefix("debug-") else {
        return false;
    };
    (3..=58).contains(&suffix.len())
        && suffix
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
}

#[cfg(any(debug_assertions, test))]
fn valid_launch_nonce(value: &str) -> bool {
    (16..=128).contains(&value.len())
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
}

#[cfg(any(debug_assertions, test))]
struct DebugParallelInstanceEnvironment<'a> {
    debug_build: bool,
    instance_id: Option<&'a str>,
    user_data_root: Option<&'a str>,
    connection_path: Option<&'a str>,
    broker_port: Option<&'a str>,
    nonce: Option<&'a str>,
    defaultspack_isolation: Option<&'a str>,
    defaultspack_run_id: Option<&'a str>,
    defaultspack_nonce: Option<&'a str>,
    defaultspack_state_root: Option<&'a str>,
    defaultspack_http_port: Option<&'a str>,
    defaultspack_kernel_port: Option<&'a str>,
}

#[cfg(any(debug_assertions, test))]
fn debug_parallel_instance_policy_from_values(
    environment: DebugParallelInstanceEnvironment<'_>,
) -> Option<DebugParallelInstancePolicy> {
    if !environment.debug_build {
        return None;
    }

    let instance_id = environment.instance_id?;
    if !valid_debug_instance_id(instance_id) {
        return None;
    }

    let user_data_root = PathBuf::from(environment.user_data_root?);
    if !is_clean_absolute_path(&user_data_root)
        || user_data_root.file_name()?.to_str()? != "viewer_user_data"
    {
        return None;
    }

    let connection_path = PathBuf::from(environment.connection_path?);
    let expected_connection_path = user_data_root.join("host_broker").join("connection.json");
    if !is_clean_absolute_path(&connection_path) || connection_path != expected_connection_path {
        return None;
    }

    let broker_port = ascii_decimal_port(environment.broker_port?)?;
    if broker_port == DEFAULT_HOST_BROKER_PORT {
        return None;
    }

    let nonce = environment.nonce?;
    if !valid_launch_nonce(nonce) {
        return None;
    }

    if environment.defaultspack_isolation? != "1"
        || environment.defaultspack_run_id? != instance_id
        || environment.defaultspack_nonce? != nonce
    {
        return None;
    }
    let supervisor_root = user_data_root.parent()?.to_path_buf();
    let defaultspack_state_root = PathBuf::from(environment.defaultspack_state_root?);
    let expected_state_root = supervisor_root.join("defaultspack_state");
    if !is_clean_absolute_path(&defaultspack_state_root)
        || defaultspack_state_root != expected_state_root
        || !secure_debug_directory(&supervisor_root, None)
        || !secure_debug_directory(&user_data_root, Some("viewer_user_data"))
        || !secure_debug_directory(&defaultspack_state_root, Some("defaultspack_state"))
        || !debug_directories_have_same_owner(&[
            &supervisor_root,
            &user_data_root,
            &defaultspack_state_root,
        ])
    {
        return None;
    }
    let defaultspack_http_port = ascii_decimal_port(environment.defaultspack_http_port?)?;
    let kernel_port = ascii_decimal_port(environment.defaultspack_kernel_port?)?;
    if defaultspack_http_port == DEFAULTSPACK_RESERVED_PORT
        || kernel_port == 8765
        || defaultspack_http_port == kernel_port
        || defaultspack_http_port == broker_port
        || kernel_port == broker_port
    {
        return None;
    }

    Some(DebugParallelInstancePolicy {
        supervisor_root,
        user_data_root,
        defaultspack_state_root,
        broker_port,
        defaultspack_http_port,
        kernel_port,
    })
}

#[cfg(debug_assertions)]
fn debug_parallel_instance_policy_from_env() -> Option<DebugParallelInstancePolicy> {
    debug_parallel_instance_policy_from_values(DebugParallelInstanceEnvironment {
        debug_build: true,
        instance_id: std::env::var(DEBUG_INSTANCE_ID_ENV).ok().as_deref(),
        user_data_root: std::env::var(DEBUG_USER_DATA_ROOT_ENV).ok().as_deref(),
        connection_path: std::env::var(HOST_BROKER_CONNECTION_ENV).ok().as_deref(),
        broker_port: std::env::var(HOST_BROKER_PORT_ENV).ok().as_deref(),
        nonce: std::env::var(HOST_BROKER_INSTANCE_NONCE_ENV)
            .ok()
            .as_deref(),
        defaultspack_isolation: std::env::var(DEFAULTSPACK_DEBUG_ISOLATION_ENV)
            .ok()
            .as_deref(),
        defaultspack_run_id: std::env::var(DEFAULTSPACK_DEBUG_RUN_ID_ENV).ok().as_deref(),
        defaultspack_nonce: std::env::var(DEFAULTSPACK_DEBUG_LAUNCH_NONCE_ENV)
            .ok()
            .as_deref(),
        defaultspack_state_root: std::env::var(DEFAULTSPACK_DEBUG_STATE_ROOT_ENV)
            .ok()
            .as_deref(),
        defaultspack_http_port: std::env::var(DEFAULTSPACK_DEBUG_HTTP_PORT_ENV)
            .ok()
            .as_deref(),
        defaultspack_kernel_port: std::env::var(DEFAULTSPACK_DEBUG_KERNEL_PORT_ENV)
            .ok()
            .as_deref(),
    })
}

#[cfg(debug_assertions)]
pub(crate) fn debug_defaultspack_ports_from_env() -> Option<(u16, u16)> {
    debug_parallel_instance_policy_from_env()
        .map(|policy| (policy.defaultspack_http_port, policy.kernel_port))
}

#[cfg(debug_assertions)]
pub(crate) fn debug_defaultspack_approval_secret_path_from_env() -> Option<PathBuf> {
    debug_parallel_instance_policy_from_env().map(|policy| {
        policy
            .defaultspack_state_root
            .join("approval")
            .join("approval_runtime_secret")
    })
}

#[cfg(not(debug_assertions))]
pub(crate) fn debug_defaultspack_approval_secret_path_from_env() -> Option<PathBuf> {
    None
}

#[cfg(not(debug_assertions))]
pub(crate) fn debug_defaultspack_ports_from_env() -> Option<(u16, u16)> {
    None
}

pub fn run() {
    let context = tauri::generate_context!();
    if context.config().identifier == shell_handoff::SHELL_BUNDLE_IDENTIFIER {
        shell_runtime::run(context);
        return;
    }
    run_launcher(context);
}

fn run_launcher(context: tauri::Context<tauri::Wry>) {
    env_logger::init();
    let app_identifier = context.config().identifier.clone();

    #[cfg(debug_assertions)]
    let debug_parallel_instance = debug_parallel_instance_policy_from_env().and_then(|policy| {
        match prepare_debug_cache_environment(&policy) {
            Ok(environment) => {
                for (key, path) in environment {
                    std::env::set_var(key, path);
                }
                Some(policy)
            }
            Err(error) => {
                error!("Viewer debug parallel-instance cache isolation was rejected: {error:#}");
                None
            }
        }
    });
    #[cfg(debug_assertions)]
    let debug_writable_roots = debug_parallel_instance.as_ref().map(|policy| {
        (
            policy.supervisor_root.clone(),
            policy.user_data_root.clone(),
        )
    });
    #[cfg(not(debug_assertions))]
    let debug_writable_roots: Option<(PathBuf, PathBuf)> = None;
    let startup_stage = Arc::new(Mutex::new("builder"));

    let allowed_navigation_ports =
        Arc::new(Mutex::new(navigation_ports_with_tauri_dev_server(vec![
            8765,
        ])));
    let allowed_navigation_ports_for_plugin = Arc::clone(&allowed_navigation_ports);
    let allowed_navigation_ports_for_setup = Arc::clone(&allowed_navigation_ports);

    #[cfg(debug_assertions)]
    let builder = if let Some(policy) = debug_parallel_instance.as_ref() {
        // The policy is validated above, is debug-only, and carries no
        // authorization. Production/release continues to register the plugin
        // unconditionally.
        info!(
            "Viewer debug parallel-instance mode enabled for isolated ports broker={}, defaultspack={}, kernel={}",
            policy.broker_port, policy.defaultspack_http_port, policy.kernel_port
        );
        tauri::Builder::default()
    } else {
        tauri::Builder::default().plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Err(error) = show_primary_window(app) {
                error!("Failed to focus existing Rumi window after duplicate launch: {error}");
            }
        }))
    };
    #[cfg(not(debug_assertions))]
    let builder =
        tauri::Builder::default().plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Err(error) = show_primary_window(app) {
                error!("Failed to focus existing Rumi window after duplicate launch: {error}");
            }
        }));

    record_startup_stage(&startup_stage, "builder_configured");
    let setup_startup_stage = Arc::clone(&startup_stage);
    let build_startup_stage = Arc::clone(&startup_stage);

    let app = builder
        .plugin(tauri_plugin_dialog::init())
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
            record_startup_stage(&setup_startup_stage, "setup_entered");
            record_startup_stage(&setup_startup_stage, "resolving_app_paths");
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
            let app_data_dir =
                ci_e2e_app_data::resolve_app_data_dir_from_env(&app_identifier, &app_data_dir)?;
            record_startup_stage(&setup_startup_stage, "building_config");
            let mut config = AppConfig::detect_for_tauri(resource_dir, app_data_dir)
                .context("failed to build AppConfig")?;
            if let Some((supervisor_root, user_data_root)) = debug_writable_roots.as_ref() {
                config.isolate_writable_state(supervisor_root, user_data_root.clone());
            }

            record_startup_stage(&setup_startup_stage, "creating_state_directories");
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

            record_startup_stage(&setup_startup_stage, "loading_panel_bootstrap_secret");
            let panel_bootstrap_secret = load_or_create_panel_bootstrap_secret(&config)
                .context("failed to load persisted panel bootstrap secret")?;
            let debug_approval = Arc::new(DebugApprovalManager::new(
                config.log_dir.join("debug-approval-audit.jsonl"),
            ));
            record_startup_stage(&setup_startup_stage, "starting_host_broker");
            let host_broker =
                HostBrokerRuntime::start(&config, Arc::clone(&debug_approval))
                .context("failed to start Viewer host broker")?;
            let broker_attestation = host_broker.attestation_identity();
            record_startup_stage(&setup_startup_stage, "host_broker_running");
            app.manage(host_broker.clone());
            app.manage(broker_attestation.clone());
            app.manage(Arc::clone(&debug_approval));
            #[cfg(debug_assertions)]
            if let Some(policy) = debug_parallel_instance.as_ref() {
                // A complete debug policy binds every run to an exact reserved
                // kernel port.  Do not scan/reuse another Viewer's kernel.
                config.kernel_port = policy.kernel_port;
            } else {
                config.kernel_port = resolve_available_kernel_port(&config, &panel_bootstrap_secret);
            }
            #[cfg(not(debug_assertions))]
            {
                config.kernel_port = resolve_available_kernel_port(&config, &panel_bootstrap_secret);
            }
            set_allowed_navigation_ports(
                &allowed_navigation_ports_for_setup,
                navigation_ports_with_tauri_dev_server(vec![
                    config.kernel_port,
                    #[cfg(debug_assertions)]
                    debug_parallel_instance
                        .as_ref()
                        .map(|policy| policy.defaultspack_http_port)
                        .unwrap_or(DEFAULTSPACK_RESERVED_PORT),
                    #[cfg(not(debug_assertions))]
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
                broker_attestation,
                Arc::clone(&debug_approval),
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
                                    navigate_and_show_window_to_panel_session(
                                        &win,
                                        port,
                                        &panel_code,
                                    )
                                {
                                    error!("Failed to navigate to panel: {e}");
                                }
                            }
                            prepare_defaultspack_guardian_in_background(
                                handle.clone(),
                                config.clone(),
                                panel_bootstrap_secret.clone(),
                            );
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
                if let Err(e) = python_env::ensure_python_env_with_progress(&config, |message| {
                    update_setup_progress(Some(&handle), &progress_arc, message);
                }) {
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
                        let msg = startup_failure_message("Tobkiri Launcher startup", &e, &config);
                        error!("{msg}");
                        update_setup_progress(Some(&handle), &progress_arc, &msg);
                        return;
                    }
                };

                update_setup_progress(Some(&handle), &progress_arc, "Ready");

                if let Some(win) = handle.get_webview_window("main") {
                    if let Err(e) = navigate_and_show_window_to_panel_session(
                        &win,
                        port,
                        &panel_code,
                    ) {
                        error!("Failed to navigate to panel: {e}");
                    }
                }

                prepare_defaultspack_guardian_in_background(
                    handle.clone(),
                    config.clone(),
                    panel_bootstrap_secret.clone(),
                );

                // Delayed background update check.
                run_delayed_update_check();
            });

            record_startup_stage(&setup_startup_stage, "setting_up_tray");
            tray::setup_tray(app)?;
            record_startup_stage(&setup_startup_stage, "setup_complete");

            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                if should_send_to_background_on_close(window.label()) {
                    api.prevent_close();
                    if let Err(error) = send_app_to_background(window.app_handle()) {
                        error!("Failed to send app to background: {error}");
                    }
                } else if should_restore_primary_on_close(window.label()) {
                    if let Err(error) = restore_primary_window(window.app_handle(), true) {
                        error!("Failed to restore launcher after closing Tobkiri: {error}");
                    }
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            get_setup_progress,
            debug_approval_status,
            arm_debug_approval,
            revoke_debug_approval,
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
            coding_approval_operator,
            send_to_background,
            show_app_window,
            get_background_control_status,
            desktop_system_info::get_desktop_system_info,
            desktop_system_info::get_host_permission_status,
            desktop_system_info::open_host_permission_settings,
            dock_registration::register_defaultspack_dock,
            dock_registration::launch_defaultspack_desktop,
            presentation::get_presentation_catalog,
            presentation::select_presentation,
            presentation::launch_selected_presentation
        ])
        .build(context);

    let app = match app {
        Ok(app) => app,
        Err(_) => {
            // Do not convert a setup/build failure into a duplicate-instance
            // success. The single-instance plugin itself retains its documented
            // exit(0) behavior for normal duplicate launches.
            error!(
                "Viewer startup failed at stage={}; exiting nonzero",
                startup_stage_name(&build_startup_stage)
            );
            std::process::exit(1);
        }
    };

    record_startup_stage(&startup_stage, "running");
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
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    use std::sync::Mutex;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn test_config() -> AppConfig {
        AppConfig::detect_for_tauri(
            PathBuf::from("/tmp/test_resource"),
            PathBuf::from("/tmp/test_appdata"),
        )
        .unwrap()
    }

    struct DebugPolicyFixture {
        supervisor: PathBuf,
        user_data: PathBuf,
        connection: PathBuf,
        defaultspack_state: PathBuf,
    }

    impl DebugPolicyFixture {
        fn new() -> Self {
            let unique = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos();
            let supervisor = std::env::temp_dir().canonicalize().unwrap().join(format!(
                "rumi-viewer-policy-{}-{unique}",
                std::process::id()
            ));
            let user_data = supervisor.join("viewer_user_data");
            let connection = user_data.join("host_broker").join("connection.json");
            let defaultspack_state = supervisor.join("defaultspack_state");
            fs::create_dir(&supervisor).unwrap();
            fs::create_dir(&user_data).unwrap();
            fs::create_dir(&defaultspack_state).unwrap();
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;

                for path in [&supervisor, &user_data, &defaultspack_state] {
                    fs::set_permissions(path, fs::Permissions::from_mode(0o700)).unwrap();
                }
            }
            Self {
                supervisor,
                user_data,
                connection,
                defaultspack_state,
            }
        }

        fn path_text(path: &std::path::Path) -> &str {
            path.to_str().unwrap()
        }

        fn viewer_values(&self) -> (&'static str, &str, &str, &'static str, &'static str) {
            (
                "debug-viewer-smoke-12345",
                Self::path_text(&self.user_data),
                Self::path_text(&self.connection),
                "18770",
                "debug_nonce_0123456789",
            )
        }

        fn defaultspack_values(
            &self,
        ) -> (
            &'static str,
            &'static str,
            &'static str,
            &str,
            &'static str,
            &'static str,
        ) {
            (
                "1",
                "debug-viewer-smoke-12345",
                "debug_nonce_0123456789",
                Self::path_text(&self.defaultspack_state),
                "18771",
                "18772",
            )
        }
    }

    impl Drop for DebugPolicyFixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.supervisor);
        }
    }

    fn valid_debug_parallel_policy_values(
        fixture: &DebugPolicyFixture,
    ) -> (&'static str, &str, &str, &'static str, &'static str) {
        fixture.viewer_values()
    }

    fn valid_debug_defaultspack_policy_values(
        fixture: &DebugPolicyFixture,
    ) -> (
        &'static str,
        &'static str,
        &'static str,
        &str,
        &'static str,
        &'static str,
    ) {
        fixture.defaultspack_values()
    }

    macro_rules! debug_policy {
        (
            $debug_build:expr, $instance_id:expr, $user_data_root:expr,
            $connection_path:expr, $broker_port:expr, $nonce:expr,
            $defaultspack_isolation:expr, $defaultspack_run_id:expr,
            $defaultspack_nonce:expr, $defaultspack_state_root:expr,
            $defaultspack_http_port:expr, $defaultspack_kernel_port:expr $(,)?
        ) => {
            debug_parallel_instance_policy_from_values(DebugParallelInstanceEnvironment {
                debug_build: $debug_build,
                instance_id: $instance_id,
                user_data_root: $user_data_root,
                connection_path: $connection_path,
                broker_port: $broker_port,
                nonce: $nonce,
                defaultspack_isolation: $defaultspack_isolation,
                defaultspack_run_id: $defaultspack_run_id,
                defaultspack_nonce: $defaultspack_nonce,
                defaultspack_state_root: $defaultspack_state_root,
                defaultspack_http_port: $defaultspack_http_port,
                defaultspack_kernel_port: $defaultspack_kernel_port,
            })
        };
    }

    #[test]
    fn debug_parallel_instance_requires_every_isolation_precondition() {
        let fixture = DebugPolicyFixture::new();
        let (id, root, connection, port, nonce) = valid_debug_parallel_policy_values(&fixture);
        let (isolation, run_id, defaultspack_nonce, state_root, http_port, kernel_port) =
            valid_debug_defaultspack_policy_values(&fixture);
        let policy = debug_policy!(
            true,
            Some(id),
            Some(root),
            Some(connection),
            Some(port),
            Some(nonce),
            Some(isolation),
            Some(run_id),
            Some(defaultspack_nonce),
            Some(state_root),
            Some(http_port),
            Some(kernel_port),
        )
        .expect("fully isolated debug launch should be eligible");
        assert_eq!(policy.supervisor_root, fixture.supervisor);
        assert_eq!(policy.user_data_root, PathBuf::from(root));
        assert_eq!(policy.broker_port, 18770);
        assert_eq!(policy.defaultspack_http_port, 18771);
        assert_eq!(policy.kernel_port, 18772);

        for missing in 0..11 {
            let mut values = [
                Some(id),
                Some(root),
                Some(connection),
                Some(port),
                Some(nonce),
                Some(isolation),
                Some(run_id),
                Some(defaultspack_nonce),
                Some(state_root),
                Some(http_port),
                Some(kernel_port),
            ];
            values[missing] = None;
            assert!(
                debug_policy!(
                    true, values[0], values[1], values[2], values[3], values[4], values[5],
                    values[6], values[7], values[8], values[9], values[10]
                )
                .is_none(),
                "missing precondition {missing} must retain single-instance"
            );
        }
    }

    #[test]
    fn debug_parallel_instance_rejects_malformed_or_shared_inputs() {
        let fixture = DebugPolicyFixture::new();
        let (id, root, connection, _port, nonce) = valid_debug_parallel_policy_values(&fixture);
        let (isolation, run_id, defaultspack_nonce, state_root, http_port, kernel_port) =
            valid_debug_defaultspack_policy_values(&fixture);
        for bad_id in ["viewer-smoke", "debug-x", "debug-has space"] {
            assert!(debug_policy!(
                true,
                Some(bad_id),
                Some(root),
                Some(connection),
                Some("18770"),
                Some(nonce),
                Some(isolation),
                Some(run_id),
                Some(defaultspack_nonce),
                Some(state_root),
                Some(http_port),
                Some(kernel_port),
            )
            .is_none());
        }
        for bad_port in ["8770", "0", " 18770", "18770 ", "65536"] {
            assert!(
                debug_policy!(
                    true,
                    Some(id),
                    Some(root),
                    Some(connection),
                    Some(bad_port),
                    Some(nonce),
                    Some(isolation),
                    Some(run_id),
                    Some(defaultspack_nonce),
                    Some(state_root),
                    Some(http_port),
                    Some(kernel_port),
                )
                .is_none(),
                "{bad_port:?} must retain single-instance"
            );
        }
        assert!(debug_policy!(
            true,
            Some(id),
            Some(root),
            Some("/tmp/not-the-run/host_broker/connection.json"),
            Some("18770"),
            Some(nonce),
            Some(isolation),
            Some(run_id),
            Some(defaultspack_nonce),
            Some(state_root),
            Some(http_port),
            Some(kernel_port),
        )
        .is_none());
        assert!(debug_policy!(
            true,
            Some(id),
            Some("relative-root"),
            Some(connection),
            Some("18770"),
            Some(nonce),
            Some(isolation),
            Some(run_id),
            Some(defaultspack_nonce),
            Some(state_root),
            Some(http_port),
            Some(kernel_port),
        )
        .is_none());
    }

    #[cfg(unix)]
    #[test]
    fn debug_parallel_instance_rejects_symlinked_or_permissive_supervisor() {
        use std::os::unix::fs::{symlink, PermissionsExt};

        let fixture = DebugPolicyFixture::new();
        let alias = fixture.supervisor.with_file_name(format!(
            "{}-alias",
            fixture.supervisor.file_name().unwrap().to_string_lossy()
        ));
        symlink(&fixture.supervisor, &alias).unwrap();
        let alias_user_data = alias.join("viewer_user_data");
        let alias_connection = alias_user_data.join("host_broker").join("connection.json");
        let alias_state = alias.join("defaultspack_state");
        assert!(debug_policy!(
            true,
            Some("debug-viewer-smoke-12345"),
            alias_user_data.to_str(),
            alias_connection.to_str(),
            Some("18770"),
            Some("debug_nonce_0123456789"),
            Some("1"),
            Some("debug-viewer-smoke-12345"),
            Some("debug_nonce_0123456789"),
            alias_state.to_str(),
            Some("18771"),
            Some("18772"),
        )
        .is_none());
        fs::remove_file(&alias).unwrap();

        fs::set_permissions(&fixture.supervisor, fs::Permissions::from_mode(0o755)).unwrap();
        let (id, root, connection, port, nonce) = fixture.viewer_values();
        let (isolation, run_id, defaultspack_nonce, state_root, http_port, kernel_port) =
            fixture.defaultspack_values();
        assert!(debug_policy!(
            true,
            Some(id),
            Some(root),
            Some(connection),
            Some(port),
            Some(nonce),
            Some(isolation),
            Some(run_id),
            Some(defaultspack_nonce),
            Some(state_root),
            Some(http_port),
            Some(kernel_port),
        )
        .is_none());
    }

    #[test]
    fn debug_parallel_instance_rejects_unexpected_state_basenames() {
        let fixture = DebugPolicyFixture::new();
        let wrong_user_data = fixture.supervisor.join("user_data");
        let wrong_connection = wrong_user_data.join("host_broker").join("connection.json");
        assert!(debug_policy!(
            true,
            Some("debug-viewer-smoke-12345"),
            wrong_user_data.to_str(),
            wrong_connection.to_str(),
            Some("18770"),
            Some("debug_nonce_0123456789"),
            Some("1"),
            Some("debug-viewer-smoke-12345"),
            Some("debug_nonce_0123456789"),
            fixture.defaultspack_state.to_str(),
            Some("18771"),
            Some("18772"),
        )
        .is_none());

        let wrong_state = fixture.supervisor.join("state");
        let (id, root, connection, port, nonce) = fixture.viewer_values();
        assert!(debug_policy!(
            true,
            Some(id),
            Some(root),
            Some(connection),
            Some(port),
            Some(nonce),
            Some("1"),
            Some(id),
            Some(nonce),
            wrong_state.to_str(),
            Some("18771"),
            Some("18772"),
        )
        .is_none());
    }

    #[test]
    fn debug_cache_environment_stays_under_supervisor_without_recreating_state() {
        let fixture = DebugPolicyFixture::new();
        let (id, root, connection, port, nonce) = fixture.viewer_values();
        let (isolation, run_id, defaultspack_nonce, state_root, http_port, kernel_port) =
            fixture.defaultspack_values();
        let policy = debug_policy!(
            true,
            Some(id),
            Some(root),
            Some(connection),
            Some(port),
            Some(nonce),
            Some(isolation),
            Some(run_id),
            Some(defaultspack_nonce),
            Some(state_root),
            Some(http_port),
            Some(kernel_port),
        )
        .unwrap();
        let sentinel = fixture.supervisor.join("do-not-delete.txt");
        fs::write(&sentinel, b"preserve").unwrap();

        let environment = prepare_debug_cache_environment(&policy).unwrap();
        let second = prepare_debug_cache_environment(&policy).unwrap();

        assert_eq!(environment, second);
        assert_eq!(fs::read(&sentinel).unwrap(), b"preserve");
        for (_, path) in environment {
            assert!(path.starts_with(&fixture.supervisor));
            assert!(secure_debug_directory(
                &path,
                path.file_name().and_then(|v| v.to_str())
            ));
        }
    }

    #[cfg(unix)]
    #[test]
    fn debug_cache_environment_rejects_preplanted_symlink() {
        use std::os::unix::fs::{symlink, PermissionsExt};

        let fixture = DebugPolicyFixture::new();
        let (id, root, connection, port, nonce) = fixture.viewer_values();
        let (isolation, run_id, defaultspack_nonce, state_root, http_port, kernel_port) =
            fixture.defaultspack_values();
        let policy = debug_policy!(
            true,
            Some(id),
            Some(root),
            Some(connection),
            Some(port),
            Some(nonce),
            Some(isolation),
            Some(run_id),
            Some(defaultspack_nonce),
            Some(state_root),
            Some(http_port),
            Some(kernel_port),
        )
        .unwrap();
        let external = fixture.supervisor.with_file_name(format!(
            "{}-external-cache",
            fixture.supervisor.file_name().unwrap().to_string_lossy()
        ));
        fs::create_dir(&external).unwrap();
        fs::set_permissions(&external, fs::Permissions::from_mode(0o700)).unwrap();
        symlink(&external, fixture.supervisor.join("cargo_target")).unwrap();

        assert!(prepare_debug_cache_environment(&policy).is_err());
        assert!(external.read_dir().unwrap().next().is_none());

        fs::remove_dir(&external).unwrap();
    }

    #[test]
    fn release_policy_never_bypasses_single_instance() {
        let fixture = DebugPolicyFixture::new();
        let (id, root, connection, port, nonce) = valid_debug_parallel_policy_values(&fixture);
        let (isolation, run_id, defaultspack_nonce, state_root, http_port, kernel_port) =
            valid_debug_defaultspack_policy_values(&fixture);
        assert!(debug_policy!(
            false,
            Some(id),
            Some(root),
            Some(connection),
            Some(port),
            Some(nonce),
            Some(isolation),
            Some(run_id),
            Some(defaultspack_nonce),
            Some(state_root),
            Some(http_port),
            Some(kernel_port),
        )
        .is_none());
    }

    #[test]
    fn defaultspack_auxiliary_urls_keep_production_default_and_accept_isolated_port() {
        assert_eq!(
            defaultspack_http_port_for_urls(None),
            DEFAULTSPACK_RESERVED_PORT
        );
        assert_eq!(defaultspack_http_port_for_urls(Some(18771)), 18771);
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

        assert_eq!(AUTHORITY_APPROVAL_WINDOW_TITLE, "Tobkiriの許可");
        assert_eq!(
            url.as_str(),
            "http://127.0.0.1:8766/approval?request_id=auth_123"
        );
    }

    #[test]
    fn authority_approval_open_requires_focused_exact_launcher_route() {
        for (label, route) in [
            (DEFAULTSPACK_MAIN_WINDOW_LABEL, "/chat?chat=conversation-1"),
            (DEFAULTSPACK_MAIN_WINDOW_LABEL, "/coding"),
            (AMBIENT_TRIGGER_WINDOW_LABEL, "/ambient"),
            (FINGER_RECORDING_WINDOW_LABEL, "/finger-recording"),
        ] {
            validate_authority_approval_open_caller(
                label,
                true,
                &Url::parse(&format!("http://127.0.0.1:18771{route}")).unwrap(),
                18771,
            )
            .unwrap();
        }

        let main_url = Url::parse("http://127.0.0.1:18771/chat").unwrap();
        assert!(validate_authority_approval_open_caller(
            DEFAULTSPACK_MAIN_WINDOW_LABEL,
            false,
            &main_url,
            18771,
        )
        .is_err());
        for (label, rejected) in [
            (DEFAULTSPACK_MAIN_WINDOW_LABEL, "http://127.0.0.1:8766/chat"),
            (
                DEFAULTSPACK_MAIN_WINDOW_LABEL,
                "https://127.0.0.1:18771/chat",
            ),
            (
                DEFAULTSPACK_MAIN_WINDOW_LABEL,
                "http://example.invalid:18771/chat",
            ),
            (
                DEFAULTSPACK_MAIN_WINDOW_LABEL,
                "http://localhost:18771/chat",
            ),
            (
                DEFAULTSPACK_MAIN_WINDOW_LABEL,
                "http://user@127.0.0.1:18771/chat",
            ),
            (
                DEFAULTSPACK_MAIN_WINDOW_LABEL,
                "http://127.0.0.1:18771/approval",
            ),
            (
                AMBIENT_TRIGGER_WINDOW_LABEL,
                "http://127.0.0.1:18771/finger-recording",
            ),
            (
                FINGER_RECORDING_WINDOW_LABEL,
                "http://127.0.0.1:18771/ambient",
            ),
            ("main", "http://127.0.0.1:18771/chat"),
        ] {
            assert!(validate_authority_approval_open_caller(
                label,
                true,
                &Url::parse(rejected).unwrap(),
                18771,
            )
            .is_err());
        }
    }

    #[test]
    fn authority_approval_context_requires_focused_exact_local_route() {
        let url = Url::parse("http://127.0.0.1:18771/approval?request_id=auth_123").unwrap();
        validate_authority_approval_context_caller(
            AUTHORITY_APPROVAL_WINDOW_LABEL,
            true,
            &url,
            "auth_123",
            18771,
        )
        .unwrap();

        assert!(validate_authority_approval_context_caller(
            AUTHORITY_APPROVAL_WINDOW_LABEL,
            false,
            &url,
            "auth_123",
            18771,
        )
        .is_err());
        for rejected in [
            "http://127.0.0.1:8766/approval?request_id=auth_123",
            "https://127.0.0.1:18771/approval?request_id=auth_123",
            "http://example.invalid:18771/approval?request_id=auth_123",
            "http://localhost:18771/approval?request_id=auth_123",
            "http://127.0.0.1:18771/ambient?request_id=auth_123",
            "http://127.0.0.1:18771/approval?request_id=other",
            "http://127.0.0.1:18771/approval?request_id=auth_123&extra=1",
            "http://127.0.0.1:18771/approval?request_id=auth_123&request_id=auth_123",
        ] {
            assert!(validate_authority_approval_context_caller(
                AUTHORITY_APPROVAL_WINDOW_LABEL,
                true,
                &Url::parse(rejected).unwrap(),
                "auth_123",
                18771,
            )
            .is_err());
        }
    }

    #[test]
    fn authority_approval_acl_supports_isolated_local_ports_with_narrow_windows() {
        let open: serde_json::Value =
            serde_json::from_str(include_str!("../capabilities/authority-approval-open.json"))
                .unwrap();
        assert_eq!(
            open["windows"],
            serde_json::json!(["defaultspack-main", "ambient-trigger", "finger-recording"])
        );
        assert_eq!(
            open["remote"]["urls"],
            serde_json::json!(["http://127.0.0.1:*/*"])
        );
        assert_eq!(
            open["permissions"],
            serde_json::json!(["allow-open-authority-approval-window"])
        );

        let context: serde_json::Value = serde_json::from_str(include_str!(
            "../capabilities/authority-approval-context.json"
        ))
        .unwrap();
        assert_eq!(
            context["windows"],
            serde_json::json!(["authority-approval"])
        );
        assert_eq!(
            context["remote"]["urls"],
            serde_json::json!(["http://127.0.0.1:*/*"])
        );
        assert_eq!(
            context["permissions"],
            serde_json::json!([
                "allow-authority-approval-context",
                "allow-close-current-window"
            ])
        );
    }

    #[cfg(debug_assertions)]
    #[test]
    fn debug_authority_smoke_uses_an_opaque_signed_contract_route() {
        assert_eq!(
            debug_contract_url(
                "http://127.0.0.1:18771",
                "POST",
                "/api/command-protocol/v1/high-risk",
            )
            .unwrap(),
            "http://127.0.0.1:18771/api/contracts/defaultspack/POST%20%2Fapi%2Fcommand-protocol%2Fv1%2Fhigh-risk",
        );
        assert!(debug_contract_url(
            "http://127.0.0.1:18771",
            "PUT",
            "/api/command-protocol/v1/high-risk",
        )
        .is_err());
        assert!(debug_contract_url(
            "http://127.0.0.1:18771",
            "POST",
            "/api/command-protocol/v1/high-risk?unsafe=true",
        )
        .is_err());
    }

    #[cfg(debug_assertions)]
    #[test]
    fn debug_authority_smoke_request_ids_are_uuid_v4() {
        let request_id = debug_contract_request_id();
        let bytes = request_id.as_bytes();

        assert_eq!(request_id.len(), 36);
        assert_eq!(bytes[8], b'-');
        assert_eq!(bytes[13], b'-');
        assert_eq!(bytes[14], b'4');
        assert_eq!(bytes[18], b'-');
        assert!(matches!(bytes[19], b'8' | b'9' | b'a' | b'b'));
        assert_eq!(bytes[23], b'-');
    }

    #[cfg(debug_assertions)]
    #[test]
    fn debug_authority_smoke_reads_only_the_authoritative_state_field() {
        assert_eq!(
            debug_result_state(&serde_json::json!({ "state": "approved" })),
            Some("approved")
        );
        assert_eq!(debug_result_state(&serde_json::json!({ "state": 1 })), None);
        assert_eq!(debug_result_state(&serde_json::json!({})), None);
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
        assert_eq!(operator.decision, None);
        assert_eq!(operator.request_snapshot_digest, None);
        assert_eq!(operator.typed_confirmation_digest, None);
        assert_eq!(
            authority_operator_message(&operator),
            "v1\ntauri_webview_window\nauthority-approval\nauth_123\n1700000000\n1700000180\nnonce-1"
        );
        assert!(!operator.signature.is_empty());
    }

    #[test]
    fn interactive_authority_ui_operator_binds_decision_snapshot_and_confirmation() {
        let snapshot = "a".repeat(64);
        let confirmation = "b".repeat(64);
        let approve = sign_interactive_authority_ui_operator(
            "auth_123",
            "approve",
            &snapshot,
            Some(&confirmation),
            "test-bootstrap-secret",
            1_700_000_000,
            "nonce-1".into(),
        )
        .unwrap();
        assert_eq!(approve.version, 3);
        assert_eq!(approve.decision.as_deref(), Some("approve"));
        assert_eq!(
            authority_operator_message(&approve),
            format!(
                "v3\ntauri_webview_window\nauthority-approval\nauth_123\napprove\n{snapshot}\n{confirmation}\n1700000000\n1700000180\nnonce-1"
            )
        );

        let deny = sign_interactive_authority_ui_operator(
            "auth_123",
            "deny",
            &snapshot,
            None,
            "test-bootstrap-secret",
            1_700_000_000,
            "nonce-1".into(),
        )
        .unwrap();
        let changed_snapshot = sign_interactive_authority_ui_operator(
            "auth_123",
            "approve",
            &"c".repeat(64),
            Some(&confirmation),
            "test-bootstrap-secret",
            1_700_000_000,
            "nonce-1".into(),
        )
        .unwrap();
        let changed_confirmation = sign_interactive_authority_ui_operator(
            "auth_123",
            "approve",
            &snapshot,
            Some(&"d".repeat(64)),
            "test-bootstrap-secret",
            1_700_000_000,
            "nonce-1".into(),
        )
        .unwrap();
        assert_ne!(approve.signature, deny.signature);
        assert_ne!(approve.signature, changed_snapshot.signature);
        assert_ne!(approve.signature, changed_confirmation.signature);
        assert!(sign_interactive_authority_ui_operator(
            "auth_123",
            "deny",
            &snapshot,
            Some(&confirmation),
            "test-bootstrap-secret",
            1_700_000_000,
            "nonce-1".into(),
        )
        .is_err());
        assert!(sign_interactive_authority_ui_operator(
            "auth_123",
            "approve",
            &"A".repeat(64),
            None,
            "test-bootstrap-secret",
            1_700_000_000,
            "nonce-1".into(),
        )
        .is_err());
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
    fn macos_development_config_disables_all_packaged_runtime_resources() {
        let config: serde_json::Value =
            serde_json::from_str(include_str!("../tauri.macos.dev.conf.json")).unwrap();

        assert_eq!(
            config["identifier"], "dev.tobkiri.local-launcher",
            "the unbundled mode must be independently identifiable"
        );
        assert!(
            config["bundle"]["resources"].is_null(),
            "a per-entry null does not override Tauri's base resource map; dev must disable the map itself"
        );
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

    #[test]
    fn panel_session_renewal_accepts_only_the_live_launcher_panel() {
        for url in [
            "http://127.0.0.1:8765/panel",
            "http://localhost:8765/panel/",
            "http://127.0.0.1:8765/panel/packs/defaults?code=secret",
        ] {
            validate_panel_session_caller_context("main", &Url::parse(url).unwrap(), 8765)
                .unwrap();
        }
    }

    #[test]
    fn panel_session_renewal_rejects_wrong_window_origin_port_and_route() {
        for (label, url, port) in [
            ("defaultspack-main", "http://127.0.0.1:8765/panel/", 8765),
            ("main", "tauri://localhost/panel/", 8765),
            ("main", "http://example.invalid:8765/panel/", 8765),
            ("main", "http://127.0.0.1:8766/panel/", 8765),
            ("main", "http://127.0.0.1:8765/approval", 8765),
            ("main", "http://127.0.0.1:8765/panel/", 0),
        ] {
            assert_eq!(
                validate_panel_session_caller_context(label, &Url::parse(url).unwrap(), port),
                Err(PANEL_SESSION_CALLER_DENIED)
            );
        }
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
