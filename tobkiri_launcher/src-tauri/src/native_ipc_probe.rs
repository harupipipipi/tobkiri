//! Opt-in debug observation of the real native IPC boundary, never authority.
//! It adds no commands, capabilities, allowed origins or approval decisions.
use std::ffi::OsStr;
use tauri::utils::config::{Config, WindowConfig};
use tobkiri_launcher_core_host::DebugScope;

const FLAG: &str = "TOBKIRI_CORE_IPC_PROBE";

pub(crate) struct ProbeSetup {
    window: WindowConfig,
}

fn requested(
    flag: Option<&OsStr>,
    debug: bool,
    macos: bool,
    scoped: bool,
) -> Result<bool, &'static str> {
    match flag {
        None => Ok(false),
        Some(value) if value == "1" && debug && macos && scoped => Ok(true),
        _ => Err("invalid native IPC probe scope"),
    }
}

/// Validate before plugins or Core can open any data. Keep the real window
/// configuration, changing only when this debug observation creates it.
pub(crate) fn prepare(
    config: &mut Config,
    scope: Option<&DebugScope>,
) -> Result<Option<ProbeSetup>, &'static str> {
    if !requested(
        std::env::var_os(FLAG).as_deref(),
        cfg!(debug_assertions),
        cfg!(target_os = "macos"),
        scope.is_some(),
    )? {
        return Ok(None);
    }
    let window = config
        .app
        .windows
        .iter_mut()
        .find(|window| window.label == "core-host")
        .ok_or("native IPC probe window unavailable")?;
    if !window.create {
        return Err("native IPC probe requires the configured native window");
    }
    let original = window.clone();
    window.create = false;
    Ok(Some(ProbeSetup { window: original }))
}

pub(crate) fn install(
    app: &mut tauri::App,
    setup: Option<ProbeSetup>,
) -> Result<(), Box<dyn std::error::Error>> {
    let Some(setup) = setup else {
        return Ok(());
    };
    #[cfg(debug_assertions)]
    return enabled::install(app, setup);
    #[cfg(not(debug_assertions))]
    {
        let _ = (app, setup);
        Err("native IPC probe is unavailable in release builds".into())
    }
}

/// Count only calls that passed the same real native caller guard. No count
/// is used to authorize, suppress or redirect a request.
pub(crate) fn admitted(window: &tauri::WebviewWindow) {
    #[cfg(debug_assertions)]
    enabled::admitted(window);
    #[cfg(not(debug_assertions))]
    let _ = window;
}

#[cfg(debug_assertions)]
mod enabled {
    use super::ProbeSetup;
    use serde::{Deserialize, Serialize};
    use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
    use tauri::{Manager, WebviewWindow, WebviewWindowBuilder};
    use tobkiri_launcher_core_host::{trusted_host_origin, CORE_HOST_COMMANDS};

    const PREFIX: &str = "TOBKIRI_NATIVE_IPC_PROBE:";
    const MAX_TITLE_BYTES: usize = 4096;
    const SCRIPT: &str = include_str!("../../../tools/native-ipc-probe/root.js");

    #[derive(Default)]
    struct Observation {
        admitted: AtomicU64,
        started: AtomicBool,
        reported: AtomicBool,
    }

    #[derive(Clone, Copy, Deserialize, Serialize)]
    #[serde(rename_all = "kebab-case")]
    enum Failure {
        None,
        RootStart,
        ChildReport,
        Stop,
        Timeout,
        Schema,
    }

    #[derive(Deserialize, Serialize)]
    #[serde(deny_unknown_fields)]
    struct RootReport {
        version: u32,
        root_start: bool,
        iframe_loaded: bool,
        root_stop: bool,
        parent_dom_blocked: bool,
        parent_internals_blocked: bool,
        storage_blocked: bool,
        sdk_exposed: bool,
        sdk_attempts: u32,
        sdk_rejected: u32,
        raw_surfaces: u32,
        raw_sent: u32,
        command_count: u32,
        failure: Failure,
    }

    impl RootReport {
        fn complete(&self) -> bool {
            let commands = CORE_HOST_COMMANDS.len() as u32;
            self.version == 1
                && matches!(self.failure, Failure::None)
                && self.root_start
                && self.iframe_loaded
                && self.root_stop
                && self.parent_dom_blocked
                && self.parent_internals_blocked
                && self.storage_blocked
                && self.command_count == commands
                && self.sdk_attempts == if self.sdk_exposed { commands } else { 0 }
                && self.sdk_rejected == self.sdk_attempts
                && (1..=2).contains(&self.raw_surfaces)
                && self.raw_sent == self.raw_surfaces * commands
        }
    }

    #[derive(Serialize)]
    struct Receipt {
        format: &'static str,
        passed: bool,
        native_caller_entries: u64,
        report: Option<RootReport>,
        native_approval_ui_tested: bool,
        configuration_api_tested: bool,
        full_product_gui_tested: bool,
    }

    pub(super) fn admitted(window: &WebviewWindow) {
        if let Some(observation) = window.app_handle().try_state::<Observation>() {
            observation.admitted.fetch_add(1, Ordering::AcqRel);
        }
    }

    fn report(window: WebviewWindow, title: String) {
        if !title.starts_with(PREFIX) {
            return;
        }
        let Some(observation) = window.app_handle().try_state::<Observation>() else {
            return;
        };
        if observation.reported.swap(true, Ordering::AcqRel) {
            return;
        }
        let parsed = (title.len() <= MAX_TITLE_BYTES
            && window.label() == "core-host"
            && window
                .url()
                .is_ok_and(|url| trusted_host_origin(url.as_str())))
        .then(|| serde_json::from_str::<RootReport>(&title[PREFIX.len()..]).ok())
        .flatten();
        let native_caller_entries = observation.admitted.load(Ordering::Acquire);
        let passed =
            parsed.as_ref().is_some_and(RootReport::complete) && native_caller_entries == 2;
        let receipt = Receipt {
            format: "tobkiri.native-ipc-observation/1",
            passed,
            native_caller_entries,
            report: parsed,
            native_approval_ui_tested: false,
            configuration_api_tested: false,
            full_product_gui_tested: false,
        };
        if let Ok(json) = serde_json::to_string(&receipt) {
            println!("TOBKIRI_NATIVE_IPC_RESULT {json}");
        }
        // The ordinary Core shutdown still runs. The controller requires the
        // closed receipt as well as process completion, never an exit code alone.
        window.app_handle().exit(if passed { 0 } else { 1 });
    }

    pub(super) fn install(
        app: &mut tauri::App,
        setup: ProbeSetup,
    ) -> Result<(), Box<dyn std::error::Error>> {
        app.manage(Observation::default());
        WebviewWindowBuilder::from_config(app, &setup.window)?
            .on_document_title_changed(report)
            .on_page_load(|window, payload| {
                if payload.event() != tauri::webview::PageLoadEvent::Finished
                    || !trusted_host_origin(payload.url().as_str())
                {
                    return;
                }
                let Some(observation) = window.app_handle().try_state::<Observation>() else {
                    return;
                };
                if window.label() != "core-host" || observation.started.swap(true, Ordering::AcqRel)
                {
                    return;
                }
                // The script performs the normal start form submission and
                // final stop. It never sends an invoke key to an untrusted frame.
                let _ = window.eval(SCRIPT);
            })
            .build()?;
        Ok(())
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn incomplete_or_unsettled_reports_never_pass() {
            let good = r#"{"version":1,"root_start":true,"iframe_loaded":true,"root_stop":true,"parent_dom_blocked":true,"parent_internals_blocked":true,"storage_blocked":true,"sdk_exposed":false,"sdk_attempts":0,"sdk_rejected":0,"raw_surfaces":1,"raw_sent":11,"command_count":11,"failure":"none"}"#;
            let mut report: RootReport = serde_json::from_str(good).unwrap();
            assert!(report.complete());
            report.sdk_exposed = true;
            report.sdk_attempts = 11;
            assert!(!report.complete());
            report.sdk_rejected = 10;
            assert!(!report.complete());
            report.sdk_rejected = 11;
            assert!(report.complete());
            report.raw_sent = 10;
            assert!(!report.complete());
            assert!(serde_json::from_str::<RootReport>(
                &good.replace(r#""failure":"none""#, r#""failure":"none","approved":true"#)
            )
            .is_err());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_complete_macos_debug_scope_enables_observation() {
        assert_eq!(requested(None, false, false, false), Ok(false));
        assert_eq!(requested(Some(OsStr::new("1")), true, true, true), Ok(true));
        for (debug, macos, scoped) in [
            (false, true, true),
            (true, false, true),
            (true, true, false),
        ] {
            assert!(requested(Some(OsStr::new("1")), debug, macos, scoped).is_err());
        }
        assert!(requested(Some(OsStr::new("yes")), true, true, true).is_err());
    }
}
