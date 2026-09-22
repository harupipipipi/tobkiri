//! Presentation-only runtime for the verified `shell.tauri.default` artifact.
//!
//! This process deliberately does not start the Launcher Host Broker, Kernel,
//! Defaultspack guardian, tray, or any Host command surface. It only consumes a
//! one-shot authenticated runtime-profile handoff and presents that loopback
//! origin in its WebView.

use std::ffi::OsString;
use std::path::Path;
use std::sync::{Arc, Mutex};

use anyhow::{anyhow, Context, Result};
use log::{error, warn};
use tauri::{AppHandle, Manager};

use crate::navigation_is_allowed;
use crate::shell_handoff::{
    consume_shell_handoff, handoff_path_from_os_args, handoff_path_from_strings,
};

fn apply_handoff(
    app: &AppHandle,
    path: &Path,
    allowed_runtime_ports: &Arc<Mutex<Vec<u16>>>,
) -> Result<()> {
    let handoff = consume_shell_handoff(path)?;
    {
        let mut ports = allowed_runtime_ports
            .lock()
            .map_err(|error| anyhow!("Shell navigation policy lock is poisoned: {error}"))?;
        ports.clear();
        ports.push(handoff.runtime_port);
    }

    let window = app
        .get_webview_window("main")
        .context("Tobkiri Shell main window is unavailable")?;
    window
        .navigate(handoff.runtime_url)
        .context("Tobkiri Shell failed to navigate to the authenticated runtime")?;
    let _ = window.unminimize();
    window
        .show()
        .context("Tobkiri Shell failed to show its main window")?;
    window
        .set_focus()
        .context("Tobkiri Shell failed to focus its main window")?;
    Ok(())
}

fn reject_initial_handoff(app: &AppHandle, error: &anyhow::Error) {
    // Do not propagate a setup-hook error. Tauri 2.10.x executes setup during
    // applicationDidFinishLaunching on macOS; unwinding through that Objective-C
    // callback aborts the process instead of producing a controlled failure.
    // The error is structural only; the authenticated URL is never logged.
    error!("Tobkiri Shell handoff rejected: {error:#}");
    app.exit(1);
}

pub(crate) fn run(context: tauri::Context<tauri::Wry>) {
    let allowed_runtime_ports = Arc::new(Mutex::new(Vec::<u16>::new()));
    let ports_for_navigation = Arc::clone(&allowed_runtime_ports);
    let ports_for_forwarded_handoff = Arc::clone(&allowed_runtime_ports);
    let initial_args = std::env::args_os().collect::<Vec<OsString>>();

    let builder = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(
            move |app, args, _cwd| {
                let result = handoff_path_from_strings(&args)
                    .and_then(|path| apply_handoff(app, &path, &ports_for_forwarded_handoff));
                if let Err(error) = result {
                    warn!("Forwarded Tobkiri Shell handoff rejected: {error:#}");
                }
            },
        ))
        .plugin(
            tauri::plugin::Builder::<tauri::Wry, ()>::new("shell-nav-guard")
                .on_navigation(move |_webview, url| {
                    let allowed_ports = ports_for_navigation
                        .lock()
                        .map(|ports| ports.clone())
                        .unwrap_or_default();
                    navigation_is_allowed(
                        url.scheme(),
                        url.host_str().unwrap_or(""),
                        url.port_or_known_default(),
                        &allowed_ports,
                    )
                })
                .build(),
        )
        .setup(move |app| {
            match handoff_path_from_os_args(initial_args.clone())
                .and_then(|path| apply_handoff(app.handle(), &path, &allowed_runtime_ports))
            {
                Ok(()) => {}
                Err(error) => reject_initial_handoff(app.handle(), &error),
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            if matches!(event, tauri::WindowEvent::CloseRequested { .. }) {
                // The Shell owns no background work. Exit instead of leaving a
                // hidden process that could retain a stale authenticated URL.
                window.app_handle().exit(0);
            }
        });

    let app = match builder.build(context) {
        Ok(app) => app,
        Err(error) => {
            error!("Tobkiri Shell construction failed: {error:#}");
            std::process::exit(1);
        }
    };

    app.run(|app_handle, event| {
        #[cfg(target_os = "macos")]
        if let tauri::RunEvent::Reopen {
            has_visible_windows: false,
            ..
        } = event
        {
            if let Some(window) = app_handle.get_webview_window("main") {
                let _ = window.show();
                let _ = window.set_focus();
            }
        }
        #[cfg(not(target_os = "macos"))]
        let _ = (app_handle, event);
    });
}
