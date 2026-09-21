use serde::Serialize;

use crate::host_broker::HostBrokerRuntime;
use crate::host_broker_types::HostBrokerStatus;

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct DesktopPermissionStatus {
    pub id: String,
    pub label: String,
    pub status: String,
    pub granted: Option<bool>,
    pub detail: String,
    pub settings_hint: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct DesktopHostPermissionStatus {
    pub id: String,
    pub label: String,
    pub status: String,
    pub granted: Option<bool>,
    pub rumi_status: String,
    pub rumi_granted: Option<bool>,
    pub os_status: String,
    pub os_granted: Option<bool>,
    pub risk_level: String,
    pub stream_allowed: Option<bool>,
    pub required_by_functions: Vec<String>,
    pub detail: String,
    pub settings_hint: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct DesktopSystemInfo {
    pub source: String,
    pub reliable: bool,
    pub app_name: String,
    pub display_version: String,
    pub launcher_tauri: bool,
    pub viewer_tauri: bool,
    pub launcher_version: String,
    pub viewer_version: String,
    pub build_channel: String,
    pub platform: String,
    pub platform_release: String,
    pub permission_subject: String,
    pub host_broker: HostBrokerStatus,
    pub host_permissions: Vec<DesktopHostPermissionStatus>,
    pub permissions: Vec<DesktopPermissionStatus>,
}

#[tauri::command]
pub fn get_desktop_system_info(
    host_broker: tauri::State<'_, HostBrokerRuntime>,
) -> DesktopSystemInfo {
    collect_desktop_system_info(host_broker.inner().status_snapshot())
}

#[tauri::command]
pub fn get_host_permission_status(
    host_broker: tauri::State<'_, HostBrokerRuntime>,
) -> Vec<DesktopHostPermissionStatus> {
    collect_desktop_system_info(host_broker.inner().status_snapshot()).host_permissions
}

#[tauri::command]
pub fn open_host_permission_settings(permission_id: String) -> Result<(), String> {
    let url = host_permission_settings_url(&permission_id)
        .ok_or_else(|| format!("No OS Settings pane is available for {permission_id}"))?;
    open::that_detached(url).map_err(|error| format!("failed to open OS Settings: {error}"))
}

pub fn collect_desktop_system_info(host_broker: HostBrokerStatus) -> DesktopSystemInfo {
    let launcher_version = env!("CARGO_PKG_VERSION").to_string();
    let permissions = collect_permissions();
    DesktopSystemInfo {
        source: "launcher_tauri".to_string(),
        reliable: true,
        app_name: "Tobkiri".to_string(),
        display_version: display_version_from_package_version(&launcher_version),
        launcher_tauri: true,
        viewer_tauri: true,
        launcher_version: launcher_version.clone(),
        viewer_version: launcher_version,
        build_channel: "beta".to_string(),
        platform: std::env::consts::OS.to_string(),
        platform_release: platform_release(),
        permission_subject: "Tobkiri Launcher".to_string(),
        host_broker,
        host_permissions: collect_host_permissions(&permissions),
        permissions,
    }
}

fn display_version_from_package_version(version: &str) -> String {
    if let Some((base, pre_release)) = version.split_once('-') {
        if let Some(label) = pre_release
            .split('.')
            .next()
            .filter(|value| !value.is_empty())
        {
            return format!("{label} {base}");
        }
    }
    version.to_string()
}

fn platform_release() -> String {
    #[cfg(target_os = "macos")]
    {
        std::process::Command::new("sw_vers")
            .arg("-productVersion")
            .output()
            .ok()
            .and_then(|output| String::from_utf8(output.stdout).ok())
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| "unknown".to_string())
    }

    #[cfg(not(target_os = "macos"))]
    {
        std::env::consts::ARCH.to_string()
    }
}

pub fn collect_permissions() -> Vec<DesktopPermissionStatus> {
    #[cfg(target_os = "macos")]
    {
        macos_permissions()
    }

    #[cfg(not(target_os = "macos"))]
    {
        vec![DesktopPermissionStatus {
            id: "macos_privacy".to_string(),
            label: "macOS Privacy".to_string(),
            status: "unsupported".to_string(),
            granted: None,
            detail: "macOS permission checks are only available on macOS.".to_string(),
            settings_hint: String::new(),
        }]
    }
}

pub fn collect_host_permissions(
    os_permissions: &[DesktopPermissionStatus],
) -> Vec<DesktopHostPermissionStatus> {
    vec![
        host_permission_row(
            HostPermissionSpec {
                id: "host.microphone.capture",
                label: "Microphone",
                risk_level: "high",
                stream_allowed: Some(true),
                os_aliases: &["microphone"],
                required_by_functions: &["ambient_monitor_start", "ai_transcribe", "recording_capture"],
                detail: "Capture microphone input after Tobkiri approval. Raw audio is not stored by defaultspack.",
                settings_hint: "System Settings > Privacy & Security > Microphone",
            },
            os_permissions,
        ),
        host_permission_row(
            HostPermissionSpec {
                id: "host.camera.capture",
                label: "Camera",
                risk_level: "high",
                stream_allowed: Some(true),
                os_aliases: &["camera"],
                required_by_functions: &["ambient_monitor_start", "recording_capture"],
                detail: "Capture camera frames for gesture tracking. Raw frames are not stored by defaultspack.",
                settings_hint: "System Settings > Privacy & Security > Camera",
            },
            os_permissions,
        ),
        host_permission_row(
            HostPermissionSpec {
                id: "host.screen.capture",
                label: "Screen Capture",
                risk_level: "high",
                stream_allowed: Some(false),
                os_aliases: &["screen_recording"],
                required_by_functions: &["computer_screenshot", "recording_capture"],
                detail: "Capture visible screen content for approved computer-use workflows.",
                settings_hint: "System Settings > Privacy & Security > Screen Recording",
            },
            os_permissions,
        ),
        host_permission_row(
            HostPermissionSpec {
                id: "host.audio.capture",
                label: "System Audio Capture",
                risk_level: "high",
                stream_allowed: Some(true),
                os_aliases: &[],
                required_by_functions: &["recording_capture"],
                detail: "Capture system audio after Tobkiri approval. Raw audio is not stored by defaultspack.",
                settings_hint: "",
            },
            os_permissions,
        ),
        host_permission_row(
            HostPermissionSpec {
                id: "host.input.pointer",
                label: "Pointer Input",
                risk_level: "high",
                stream_allowed: Some(false),
                os_aliases: &["accessibility", "input_monitoring"],
                required_by_functions: &["computer_click"],
                detail: "Move or click the pointer through approved computer-use actions.",
                settings_hint: "System Settings > Privacy & Security > Accessibility",
            },
            os_permissions,
        ),
        host_permission_row(
            HostPermissionSpec {
                id: "host.input.keyboard",
                label: "Keyboard Input",
                risk_level: "high",
                stream_allowed: Some(false),
                os_aliases: &["accessibility", "input_monitoring"],
                required_by_functions: &["computer_type"],
                detail: "Type keystrokes through approved computer-use actions.",
                settings_hint: "System Settings > Privacy & Security > Input Monitoring",
            },
            os_permissions,
        ),
        DesktopHostPermissionStatus {
            id: "host.clipboard.*".to_string(),
            label: "Clipboard".to_string(),
            status: "unknown".to_string(),
            granted: None,
            rumi_status: "unknown".to_string(),
            rumi_granted: None,
            os_status: "unsupported".to_string(),
            os_granted: None,
            risk_level: "high".to_string(),
            stream_allowed: Some(false),
            required_by_functions: vec![
                "media_clipboard_read".to_string(),
                "media_clipboard_write".to_string(),
            ],
            detail: "Clipboard access is mediated by Tobkiri approval; there is no separate OS privacy pane on this platform.".to_string(),
            settings_hint: String::new(),
        },
    ]
}

struct HostPermissionSpec<'a> {
    id: &'a str,
    label: &'a str,
    risk_level: &'a str,
    stream_allowed: Option<bool>,
    os_aliases: &'a [&'a str],
    required_by_functions: &'a [&'a str],
    detail: &'a str,
    settings_hint: &'a str,
}

fn host_permission_row(
    spec: HostPermissionSpec<'_>,
    os_permissions: &[DesktopPermissionStatus],
) -> DesktopHostPermissionStatus {
    let (os_status, os_granted) = combined_os_status(spec.os_aliases, os_permissions);
    DesktopHostPermissionStatus {
        id: spec.id.to_string(),
        label: spec.label.to_string(),
        status: "unknown".to_string(),
        granted: None,
        rumi_status: "unknown".to_string(),
        rumi_granted: None,
        os_status,
        os_granted,
        risk_level: spec.risk_level.to_string(),
        stream_allowed: spec.stream_allowed,
        required_by_functions: spec
            .required_by_functions
            .iter()
            .map(|value| (*value).to_string())
            .collect(),
        detail: spec.detail.to_string(),
        settings_hint: spec.settings_hint.to_string(),
    }
}

fn combined_os_status(
    aliases: &[&str],
    os_permissions: &[DesktopPermissionStatus],
) -> (String, Option<bool>) {
    let matches: Vec<&DesktopPermissionStatus> = os_permissions
        .iter()
        .filter(|row| aliases.iter().any(|alias| row.id == *alias))
        .collect();
    if matches.is_empty() {
        return ("unknown".to_string(), None);
    }
    if matches.iter().any(|row| row.granted == Some(false)) {
        return ("missing".to_string(), Some(false));
    }
    if matches.iter().all(|row| row.granted == Some(true)) {
        return ("granted".to_string(), Some(true));
    }
    if matches.iter().any(|row| row.status == "unsupported") {
        return ("unsupported".to_string(), None);
    }
    ("not_checked".to_string(), None)
}

fn host_permission_settings_url(permission_id: &str) -> Option<&'static str> {
    let normalized = permission_id.trim().to_ascii_lowercase();

    #[cfg(target_os = "macos")]
    {
        match normalized.as_str() {
            "host.microphone.capture" | "microphone" | "microphone.capture" => {
                Some("x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone")
            }
            "host.camera.capture" | "camera" | "camera.capture" => {
                Some("x-apple.systempreferences:com.apple.preference.security?Privacy_Camera")
            }
            "host.screen.capture" | "screen_recording" | "screen.capture" => Some(
                "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
            ),
            "host.accessibility.read"
            | "host.accessibility.mutate"
            | "host.input.pointer"
            | "accessibility" => Some(
                "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
            ),
            "host.input.keyboard" | "input_monitoring" => {
                Some("x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent")
            }
            _ => None,
        }
    }

    #[cfg(target_os = "windows")]
    {
        match normalized.as_str() {
            "host.microphone.capture" | "microphone" | "microphone.capture" => {
                Some("ms-settings:privacy-microphone")
            }
            "host.camera.capture" | "camera" | "camera.capture" => {
                Some("ms-settings:privacy-webcam")
            }
            "host.screen.capture" | "screen_recording" | "screen.capture" => {
                Some("ms-settings:privacy-graphicsCaptureProgrammatic")
            }
            _ => None,
        }
    }

    #[cfg(not(any(target_os = "macos", target_os = "windows")))]
    {
        let _ = normalized;
        None
    }
}

#[cfg(target_os = "macos")]
fn macos_permissions() -> Vec<DesktopPermissionStatus> {
    vec![
        DesktopPermissionStatus {
            id: "microphone".to_string(),
            label: "Microphone".to_string(),
            status: "not_checked".to_string(),
            granted: None,
            detail: "macOS may prompt the first time Tobkiri requests microphone capture. Verify it manually if capture fails.".to_string(),
            settings_hint: "System Settings > Privacy & Security > Microphone".to_string(),
        },
        DesktopPermissionStatus {
            id: "camera".to_string(),
            label: "Camera".to_string(),
            status: "not_checked".to_string(),
            granted: None,
            detail: "macOS may prompt the first time Tobkiri requests camera capture. Verify it manually if gesture tracking fails.".to_string(),
            settings_hint: "System Settings > Privacy & Security > Camera".to_string(),
        },
        permission_row(
            "accessibility",
            "Accessibility",
            Some(macos::accessibility_granted()),
            "Allows Tobkiri to inspect UI elements and send clicks/keyboard actions for Computer Use.",
            "System Settings > Privacy & Security > Accessibility",
        ),
        permission_row(
            "screen_recording",
            "Screen Recording",
            Some(macos::screen_recording_granted()),
            "Allows Tobkiri to capture the screen for Computer Use vision.",
            "System Settings > Privacy & Security > Screen Recording",
        ),
        DesktopPermissionStatus {
            id: "input_monitoring".to_string(),
            label: "Input Monitoring".to_string(),
            status: "not_checked".to_string(),
            granted: None,
            detail: "macOS does not provide a stable non-prompting preflight API for this permission. If key input fails, verify it manually.".to_string(),
            settings_hint: "System Settings > Privacy & Security > Input Monitoring".to_string(),
        },
    ]
}

#[cfg(target_os = "macos")]
fn permission_row(
    id: &str,
    label: &str,
    granted: Option<bool>,
    detail: &str,
    settings_hint: &str,
) -> DesktopPermissionStatus {
    let status = match granted {
        Some(true) => "granted",
        Some(false) => "missing",
        None => "not_checked",
    };
    DesktopPermissionStatus {
        id: id.to_string(),
        label: label.to_string(),
        status: status.to_string(),
        granted,
        detail: detail.to_string(),
        settings_hint: settings_hint.to_string(),
    }
}

#[cfg(target_os = "macos")]
mod macos {
    use std::os::raw::c_uchar;

    #[link(name = "ApplicationServices", kind = "framework")]
    extern "C" {
        fn AXIsProcessTrusted() -> c_uchar;
    }

    #[link(name = "CoreGraphics", kind = "framework")]
    extern "C" {
        fn CGPreflightScreenCaptureAccess() -> bool;
    }

    pub fn accessibility_granted() -> bool {
        unsafe { AXIsProcessTrusted() != 0 }
    }

    pub fn screen_recording_granted() -> bool {
        unsafe { CGPreflightScreenCaptureAccess() }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reports_beta_display_version() {
        let info = collect_desktop_system_info(HostBrokerStatus::disabled("test"));
        assert_eq!(
            info.display_version,
            display_version_from_package_version(&info.viewer_version)
        );
        assert_eq!(
            display_version_from_package_version("1.2.3-beta.4"),
            "beta 1.2.3"
        );
        assert!(!info.viewer_version.is_empty());
        assert_eq!(info.permission_subject, "Tobkiri Launcher");
        assert_eq!(info.source, "launcher_tauri");
        assert!(info.reliable);
        assert!(info
            .host_permissions
            .iter()
            .any(|row| row.id == "host.microphone.capture"));
    }

    #[test]
    fn permission_rows_have_stable_ids() {
        let info = collect_desktop_system_info(HostBrokerStatus::disabled("test"));
        let ids: Vec<&str> = info.permissions.iter().map(|row| row.id.as_str()).collect();
        #[cfg(target_os = "macos")]
        {
            assert!(ids.contains(&"microphone"));
            assert!(ids.contains(&"camera"));
            assert!(ids.contains(&"accessibility"));
            assert!(ids.contains(&"screen_recording"));
        }
        #[cfg(not(target_os = "macos"))]
        {
            assert_eq!(ids, vec!["macos_privacy"]);
        }
    }

    #[test]
    fn desktop_system_info_includes_host_broker_status() {
        let info = collect_desktop_system_info(HostBrokerStatus {
            enabled: true,
            available: true,
            status: "running".to_string(),
            url: Some("http://127.0.0.1:8770".to_string()),
            connection_path: Some("/tmp/connection.json".to_string()),
            recovery: None,
        });
        assert_eq!(info.host_broker.status, "running");
        assert_eq!(
            info.host_broker.url.as_deref(),
            Some("http://127.0.0.1:8770")
        );
    }

    #[test]
    fn host_permission_rows_map_os_permissions_without_claiming_rumi_approval() {
        let rows = collect_host_permissions(&[
            DesktopPermissionStatus {
                id: "microphone".to_string(),
                label: "Microphone".to_string(),
                status: "granted".to_string(),
                granted: Some(true),
                detail: String::new(),
                settings_hint: String::new(),
            },
            DesktopPermissionStatus {
                id: "camera".to_string(),
                label: "Camera".to_string(),
                status: "missing".to_string(),
                granted: Some(false),
                detail: String::new(),
                settings_hint: String::new(),
            },
        ]);

        let mic = rows
            .iter()
            .find(|row| row.id == "host.microphone.capture")
            .expect("microphone host permission row");
        let camera = rows
            .iter()
            .find(|row| row.id == "host.camera.capture")
            .expect("camera host permission row");

        assert_eq!(mic.os_status, "granted");
        assert_eq!(mic.rumi_status, "unknown");
        assert_eq!(camera.os_status, "missing");
        assert_eq!(camera.risk_level, "high");

        let screen = rows
            .iter()
            .find(|row| row.id == "host.screen.capture")
            .expect("screen capture host permission row");
        let audio = rows
            .iter()
            .find(|row| row.id == "host.audio.capture")
            .expect("system audio host permission row");
        assert_eq!(screen.stream_allowed, Some(false));
        assert_eq!(audio.stream_allowed, Some(true));
    }
}
