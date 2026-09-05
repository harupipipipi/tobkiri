//! Presentation-only runtime for a verified Profile-selected Shell artifact.
//!
//! This process deliberately does not start the Launcher Host Broker, Kernel,
//! Application guardian, tray, or any Host command surface. It only consumes a
//! one-shot authenticated runtime-profile handoff and presents that loopback
//! origin in its WebView.

use std::ffi::OsString;
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use anyhow::{anyhow, Context, Result};
use log::{error, warn};
use tauri::{AppHandle, Manager};

use crate::navigation_is_allowed;
use crate::shell_handoff::{
    consume_shell_handoff, handoff_path_from_os_args, handoff_path_from_strings,
    write_shell_handoff_receipt, ShellHandoffReceiptIdentity, ShellHandoffReceiptStatus,
};

const SHELL_ADMISSION_TIMEOUT: Duration = Duration::from_secs(15);

#[derive(Debug, Clone, PartialEq, Eq)]
struct ShellRuntimeBinding {
    runtime_port: u16,
    identity: crate::host_contract::ExecutionProfileIdentity,
    catalog_revision: String,
    artifact: crate::shell_handoff::ShellArtifactIdentity,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
struct ShellNavigationState {
    binding: Option<ShellRuntimeBinding>,
    allowed_runtime_ports: Vec<u16>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ShellHandoffAdmission {
    Initial,
    Exact,
    RotationRequired,
}

impl ShellNavigationState {
    fn admission(
        &self,
        handoff: &crate::shell_handoff::ValidatedShellHandoff,
    ) -> ShellHandoffAdmission {
        let proposed = ShellRuntimeBinding {
            runtime_port: handoff.runtime_port,
            identity: handoff.identity.clone(),
            catalog_revision: handoff.catalog_revision.clone(),
            artifact: handoff.artifact.clone(),
        };
        if let Some(current) = self.binding.as_ref() {
            if current != &proposed {
                return ShellHandoffAdmission::RotationRequired;
            }
            return ShellHandoffAdmission::Exact;
        }

        ShellHandoffAdmission::Initial
    }

    fn stage_initial(&mut self, handoff: &crate::shell_handoff::ValidatedShellHandoff) {
        self.allowed_runtime_ports = vec![handoff.runtime_port];
        self.binding = Some(ShellRuntimeBinding {
            runtime_port: handoff.runtime_port,
            identity: handoff.identity.clone(),
            catalog_revision: handoff.catalog_revision.clone(),
            artifact: handoff.artifact.clone(),
        });
    }
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
enum ShellHandoffLifecycle {
    #[default]
    Idle,
    Admitting(ShellHandoffReceiptIdentity),
    RotationPending(ShellHandoffReceiptIdentity),
    Exiting,
}

enum BeginHandoff {
    Apply(ShellHandoffReceiptIdentity),
    Rotate,
}

fn begin_validated_handoff(
    handoff: crate::shell_handoff::ValidatedShellHandoff,
    navigation_state: &Arc<Mutex<ShellNavigationState>>,
    lifecycle: &Arc<Mutex<ShellHandoffLifecycle>>,
) -> Result<(BeginHandoff, tauri::Url)> {
    let mut lifecycle = lifecycle
        .lock()
        .map_err(|error| anyhow!("Shell handoff lifecycle lock is poisoned: {error}"))?;
    if *lifecycle != ShellHandoffLifecycle::Idle {
        return Err(anyhow!("Shell handoff transaction is already active"));
    }
    let mut navigation = navigation_state
        .lock()
        .map_err(|error| anyhow!("Shell navigation state lock is poisoned: {error}"))?;
    let admission = navigation.admission(&handoff);
    if admission == ShellHandoffAdmission::RotationRequired {
        *lifecycle = ShellHandoffLifecycle::RotationPending(handoff.receipt);
        return Ok((BeginHandoff::Rotate, handoff.runtime_url));
    }

    if admission == ShellHandoffAdmission::Initial {
        // The navigation guard must know the admitted port before Tauri
        // evaluates navigation. Failure after this irreversible process-local
        // commit exits the Shell instead of weakening or rebinding it.
        navigation.stage_initial(&handoff);
    }
    let receipt = handoff.receipt;
    let runtime_url = handoff.runtime_url;
    *lifecycle = ShellHandoffLifecycle::Admitting(receipt.clone());
    Ok((BeginHandoff::Apply(receipt), runtime_url))
}

fn publish_binding_admitted<W>(
    lifecycle: &Arc<Mutex<ShellHandoffLifecycle>>,
    receipt: &ShellHandoffReceiptIdentity,
    write_receipt: W,
) -> Result<()>
where
    W: FnOnce(&ShellHandoffReceiptIdentity, ShellHandoffReceiptStatus) -> Result<()>,
{
    let mut lifecycle = lifecycle
        .lock()
        .map_err(|error| anyhow!("Shell handoff lifecycle lock is poisoned: {error}"))?;
    match &*lifecycle {
        ShellHandoffLifecycle::Admitting(active) if active == receipt => {}
        _ => return Err(anyhow!("Shell handoff admission is no longer active")),
    };
    let result = write_receipt(receipt, ShellHandoffReceiptStatus::BindingAdmitted);
    *lifecycle = if result.is_ok() {
        ShellHandoffLifecycle::Idle
    } else {
        ShellHandoffLifecycle::Exiting
    };
    result
}

fn expire_pending_admission(
    lifecycle: &Arc<Mutex<ShellHandoffLifecycle>>,
    receipt: &ShellHandoffReceiptIdentity,
) -> Result<bool> {
    let mut lifecycle = lifecycle
        .lock()
        .map_err(|error| anyhow!("Shell handoff lifecycle lock is poisoned: {error}"))?;
    match &*lifecycle {
        ShellHandoffLifecycle::Admitting(active) if active == receipt => {
            *lifecycle = ShellHandoffLifecycle::Exiting;
            Ok(true)
        }
        _ => return Ok(false),
    }
}

fn pending_admission_is_active(
    lifecycle: &Arc<Mutex<ShellHandoffLifecycle>>,
    receipt: &ShellHandoffReceiptIdentity,
) -> Result<bool> {
    let lifecycle = lifecycle
        .lock()
        .map_err(|error| anyhow!("Shell handoff lifecycle lock is poisoned: {error}"))?;
    Ok(matches!(
        &*lifecycle,
        ShellHandoffLifecycle::Admitting(active) if active == receipt
    ))
}

fn apply_validated_handoff<U, E, T, W>(
    handoff: crate::shell_handoff::ValidatedShellHandoff,
    navigation_state: &Arc<Mutex<ShellNavigationState>>,
    lifecycle: &Arc<Mutex<ShellHandoffLifecycle>>,
    apply_ui: U,
    request_exit: E,
    schedule_timeout: T,
    write_receipt: W,
) -> Result<()>
where
    U: FnOnce(&tauri::Url) -> Result<()>,
    E: Fn(),
    T: FnOnce(ShellHandoffReceiptIdentity),
    W: FnOnce(&ShellHandoffReceiptIdentity, ShellHandoffReceiptStatus) -> Result<()>,
{
    let (begin, runtime_url) = begin_validated_handoff(handoff, navigation_state, lifecycle)?;
    let BeginHandoff::Apply(receipt) = begin else {
        request_exit();
        return Ok(());
    };
    schedule_timeout(receipt.clone());
    if !pending_admission_is_active(lifecycle, &receipt)? {
        request_exit();
        return Err(anyhow!("Shell handoff admission timed out"));
    }
    if let Err(error) = apply_ui(&runtime_url) {
        let _ = expire_pending_admission(lifecycle, &receipt)?;
        request_exit();
        return Err(error);
    }
    if let Err(error) = publish_binding_admitted(lifecycle, &receipt, write_receipt) {
        let _ = expire_pending_admission(lifecycle, &receipt)?;
        request_exit();
        return Err(error);
    }
    Ok(())
}

fn apply_handoff(
    app: &AppHandle,
    path: &Path,
    navigation_state: &Arc<Mutex<ShellNavigationState>>,
    lifecycle: &Arc<Mutex<ShellHandoffLifecycle>>,
) -> Result<()> {
    let lifecycle_for_timeout = Arc::clone(lifecycle);
    let app_for_timeout = app.clone();
    consume_and_apply_handoff(
        path,
        navigation_state,
        lifecycle,
        consume_shell_handoff,
        |runtime_url| {
            let window = app
                .get_webview_window("main")
                .context("Tobkiri Shell main window is unavailable")?;
            window
                .navigate(runtime_url.clone())
                .context("Tobkiri Shell failed to schedule the verified runtime navigation")?;
            window
                .unminimize()
                .context("Tobkiri Shell failed to unminimize its main window")?;
            window
                .show()
                .context("Tobkiri Shell failed to show its main window")?;
            window
                .set_focus()
                .context("Tobkiri Shell failed to focus its main window")
        },
        || app.exit(0),
        move |receipt| {
            let _ = std::thread::spawn(move || {
                std::thread::sleep(SHELL_ADMISSION_TIMEOUT);
                match expire_pending_admission(&lifecycle_for_timeout, &receipt) {
                    Ok(true) => app_for_timeout.exit(1),
                    Ok(false) => {}
                    Err(error) => {
                        warn!("Tobkiri Shell admission timeout failed closed: {error:#}");
                        app_for_timeout.exit(1);
                    }
                }
            });
        },
        write_shell_handoff_receipt,
    )
}

fn consume_and_apply_handoff<C, U, E, T, W>(
    path: &Path,
    navigation_state: &Arc<Mutex<ShellNavigationState>>,
    lifecycle: &Arc<Mutex<ShellHandoffLifecycle>>,
    consume: C,
    apply_ui: U,
    request_exit: E,
    schedule_timeout: T,
    write_receipt: W,
) -> Result<()>
where
    C: FnOnce(&Path) -> Result<crate::shell_handoff::ValidatedShellHandoff>,
    U: FnOnce(&tauri::Url) -> Result<()>,
    E: Fn(),
    T: FnOnce(ShellHandoffReceiptIdentity),
    W: FnOnce(&ShellHandoffReceiptIdentity, ShellHandoffReceiptStatus) -> Result<()>,
{
    let handoff = consume(path)?;
    apply_validated_handoff(
        handoff,
        navigation_state,
        lifecycle,
        apply_ui,
        request_exit,
        schedule_timeout,
        write_receipt,
    )
}

fn reject_initial_handoff<E>(error: &anyhow::Error, request_exit: E)
where
    E: FnOnce(i32),
{
    // Do not propagate a setup-hook error. Tauri 2.10.x executes setup during
    // applicationDidFinishLaunching on macOS; unwinding through that Objective-C
    // callback aborts the process instead of producing a controlled failure.
    // The error is structural only; the authenticated URL is never logged.
    error!("Tobkiri Shell handoff rejected: {error:#}");
    request_exit(1);
}

fn publish_rotation_receipt_on_exit<W>(
    lifecycle: &Arc<Mutex<ShellHandoffLifecycle>>,
    write_receipt: W,
) -> Result<()>
where
    W: FnOnce(&ShellHandoffReceiptIdentity, ShellHandoffReceiptStatus) -> Result<()>,
{
    let mut lifecycle = lifecycle
        .lock()
        .map_err(|error| anyhow!("Shell handoff lifecycle lock is poisoned: {error}"))?;
    let pending = match &*lifecycle {
        ShellHandoffLifecycle::RotationPending(receipt) => Some(receipt.clone()),
        _ => None,
    };
    *lifecycle = ShellHandoffLifecycle::Exiting;
    drop(lifecycle);
    if let Some(receipt) = pending {
        write_receipt(&receipt, ShellHandoffReceiptStatus::RotationRequired)?;
    }
    Ok(())
}

pub(crate) fn run(context: tauri::Context<tauri::Wry>) {
    let navigation_state = Arc::new(Mutex::new(ShellNavigationState::default()));
    let lifecycle = Arc::new(Mutex::new(ShellHandoffLifecycle::default()));
    let state_for_navigation_guard = Arc::clone(&navigation_state);
    let state_for_forwarded_handoff = Arc::clone(&navigation_state);
    let lifecycle_for_forwarded_handoff = Arc::clone(&lifecycle);
    let state_for_initial_handoff = Arc::clone(&navigation_state);
    let lifecycle_for_initial_handoff = Arc::clone(&lifecycle);
    let initial_args = std::env::args_os().collect::<Vec<OsString>>();

    let builder = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(
            move |app, args, _cwd| {
                let result = handoff_path_from_strings(&args).and_then(|path| {
                    apply_handoff(
                        app,
                        &path,
                        &state_for_forwarded_handoff,
                        &lifecycle_for_forwarded_handoff,
                    )
                });
                if let Err(error) = result {
                    warn!("Forwarded Tobkiri Shell handoff rejected: {error:#}");
                }
            },
        ))
        .plugin(
            tauri::plugin::Builder::<tauri::Wry, ()>::new("shell-nav-guard")
                .on_navigation(move |_webview, url| {
                    let allowed_ports = state_for_navigation_guard
                        .lock()
                        .map(|state| state.allowed_runtime_ports.clone())
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
            match handoff_path_from_os_args(initial_args.clone()).and_then(|path| {
                apply_handoff(
                    app.handle(),
                    &path,
                    &state_for_initial_handoff,
                    &lifecycle_for_initial_handoff,
                )
            }) {
                Ok(()) => {}
                Err(error) => reject_initial_handoff(&error, |code| app.handle().exit(code)),
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

    app.run(move |app_handle, event| {
        if matches!(&event, tauri::RunEvent::Exit) {
            // Tauri invokes plugin RunEvent hooks before this callback. The
            // single-instance plugin has therefore released its OS singleton
            // before the Launcher can observe this receipt.
            if let Err(error) =
                publish_rotation_receipt_on_exit(&lifecycle, write_shell_handoff_receipt)
            {
                error!("Tobkiri Shell rotation receipt failed: {error:#}");
            }
        }
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::shell_handoff::{
        ShellArtifactIdentity, ShellHandoffReceiptIdentity, ValidatedShellHandoff,
    };
    use std::cell::{Cell, RefCell};
    use std::path::PathBuf;
    use tauri::Url;

    fn handoff(code: char, receipt_nonce: char) -> ValidatedShellHandoff {
        ValidatedShellHandoff {
            runtime_url: Url::parse(&format!(
                "http://127.0.0.1:8766/?code={}",
                code.to_string().repeat(64)
            ))
            .unwrap(),
            runtime_port: 8766,
            identity: crate::host_contract::ExecutionProfileIdentity::new(
                "profile-a",
                format!("sha256:{}", "a".repeat(64)),
                "activation:profile-a-2026",
                format!("sha256:{}", "b".repeat(64)),
            )
            .unwrap(),
            catalog_revision: format!("sha256:{}", "c".repeat(64)),
            artifact: ShellArtifactIdentity {
                provider_id: "fixture.shell".into(),
                artifact_id: "fixture.shell.macos-arm64".into(),
                artifact_digest: format!("sha256:{}", "d".repeat(64)),
                entrypoint_digest: format!("sha256:{}", "e".repeat(64)),
            },
            receipt: ShellHandoffReceiptIdentity {
                root: PathBuf::from("/private/fixture"),
                handoff_nonce: "H".repeat(40),
                receipt_nonce: receipt_nonce.to_string().repeat(40),
            },
        }
    }

    fn states() -> (
        Arc<Mutex<ShellNavigationState>>,
        Arc<Mutex<ShellHandoffLifecycle>>,
    ) {
        (
            Arc::new(Mutex::new(ShellNavigationState::default())),
            Arc::new(Mutex::new(ShellHandoffLifecycle::Idle)),
        )
    }

    fn bound_states() -> (
        Arc<Mutex<ShellNavigationState>>,
        Arc<Mutex<ShellHandoffLifecycle>>,
    ) {
        let states = states();
        states.0.lock().unwrap().stage_initial(&handoff('a', 'R'));
        states
    }

    #[test]
    fn exact_forwarded_handoff_emits_binding_admitted_after_ui_requests_succeed() {
        let (navigation, lifecycle) = bound_states();
        let before = navigation.lock().unwrap().clone();
        let forwarded = handoff('b', 'S');
        let expected_url = forwarded.runtime_url.clone();
        let ui_applied = Cell::new(false);
        let writes = RefCell::new(Vec::new());
        apply_validated_handoff(
            forwarded,
            &navigation,
            &lifecycle,
            |runtime_url| {
                assert_eq!(runtime_url, &expected_url);
                assert!(matches!(
                    *lifecycle.lock().unwrap(),
                    ShellHandoffLifecycle::Admitting(_)
                ));
                ui_applied.set(true);
                Ok(())
            },
            || panic!("exact handoff requested exit"),
            |_| {},
            |_, status| {
                assert!(ui_applied.get());
                writes.borrow_mut().push(status);
                Ok(())
            },
        )
        .unwrap();
        assert_eq!(
            *writes.borrow(),
            vec![ShellHandoffReceiptStatus::BindingAdmitted]
        );
        assert_eq!(*navigation.lock().unwrap(), before);
        assert_eq!(*lifecycle.lock().unwrap(), ShellHandoffLifecycle::Idle);

        let scheduled = RefCell::new(None);
        apply_validated_handoff(
            handoff('d', 'U'),
            &navigation,
            &lifecycle,
            |_| Ok(()),
            || panic!("next exact handoff requested exit"),
            |receipt| *scheduled.borrow_mut() = Some(receipt),
            |_, _| Ok(()),
        )
        .unwrap();
        assert!(
            !expire_pending_admission(&lifecycle, scheduled.borrow().as_ref().unwrap()).unwrap()
        );
    }

    #[test]
    fn every_forwarded_binding_mismatch_preserves_state_and_requests_rotation() {
        let cases: &[(&str, fn(&mut ValidatedShellHandoff))] = &[
            ("port", |value| value.runtime_port = 9876),
            ("profile", |value| {
                value.identity.profile_id = "profile-b".into()
            }),
            ("profile revision", |value| {
                value.identity.profile_revision = format!("sha256:{}", "f".repeat(64))
            }),
            ("activation", |value| {
                value.identity.activation_id = "activation:profile-a-2027".into()
            }),
            ("plan", |value| {
                value.identity.plan_digest = format!("sha256:{}", "f".repeat(64))
            }),
            ("catalog", |value| {
                value.catalog_revision = format!("sha256:{}", "f".repeat(64))
            }),
            ("provider", |value| {
                value.artifact.provider_id = "other.shell".into()
            }),
            ("artifact", |value| {
                value.artifact.artifact_id = "other.artifact".into()
            }),
            ("artifact digest", |value| {
                value.artifact.artifact_digest = format!("sha256:{}", "f".repeat(64))
            }),
            ("entrypoint digest", |value| {
                value.artifact.entrypoint_digest = format!("sha256:{}", "f".repeat(64))
            }),
        ];
        for (label, mutate) in cases {
            let (navigation, lifecycle) = bound_states();
            let before = navigation.lock().unwrap().clone();
            let mut forwarded = handoff('b', 'S');
            mutate(&mut forwarded);
            let exit_requested = Cell::new(false);
            let receipt_written = Cell::new(false);
            apply_validated_handoff(
                forwarded,
                &navigation,
                &lifecycle,
                |_| panic!("mismatched {label} navigated"),
                || exit_requested.set(true),
                |_| panic!("rotation scheduled admission timeout"),
                |_, _| {
                    receipt_written.set(true);
                    Ok(())
                },
            )
            .unwrap();
            assert_eq!(*navigation.lock().unwrap(), before, "{label}");
            assert!(matches!(
                *lifecycle.lock().unwrap(),
                ShellHandoffLifecycle::RotationPending(_)
            ));
            assert!(exit_requested.get(), "{label}");
            assert!(!receipt_written.get(), "{label}");

            let exact_exit = Cell::new(false);
            assert!(apply_validated_handoff(
                handoff('c', 'T'),
                &navigation,
                &lifecycle,
                |_| panic!("exact handoff navigated while rotation pending"),
                || exact_exit.set(true),
                |_| {},
                |_, _| panic!("exact handoff admitted while rotation pending"),
            )
            .is_err());
            assert!(!exact_exit.get(), "{label}");
            assert!(!receipt_written.get(), "{label}");
        }
    }

    #[test]
    fn ui_failure_requests_exit_without_a_receipt_for_initial_and_exact_handoffs() {
        for initially_bound in [false, true] {
            let (navigation, lifecycle) = if initially_bound {
                bound_states()
            } else {
                states()
            };
            let exit_requested = Cell::new(false);
            let receipt_written = Cell::new(false);
            assert!(apply_validated_handoff(
                handoff('b', 'S'),
                &navigation,
                &lifecycle,
                |_| Err(anyhow!("injected focus failure")),
                || exit_requested.set(true),
                |_| {},
                |_, _| {
                    receipt_written.set(true);
                    Ok(())
                },
            )
            .is_err());
            assert!(exit_requested.get());
            assert!(!receipt_written.get());
            assert_eq!(*lifecycle.lock().unwrap(), ShellHandoffLifecycle::Exiting);
            assert!(navigation.lock().unwrap().binding.is_some());
        }
    }

    #[test]
    fn receipt_publication_failure_requests_exit_and_releases_the_gate() {
        let (navigation, lifecycle) = states();
        let exit_requested = Cell::new(false);
        assert!(apply_validated_handoff(
            handoff('a', 'R'),
            &navigation,
            &lifecycle,
            |_| Ok(()),
            || exit_requested.set(true),
            |_| {},
            |_, status| {
                assert_eq!(status, ShellHandoffReceiptStatus::BindingAdmitted);
                Err(anyhow!("injected receipt publication failure"))
            },
        )
        .is_err());
        assert!(exit_requested.get());
        assert_eq!(*lifecycle.lock().unwrap(), ShellHandoffLifecycle::Exiting);
    }

    #[test]
    fn concurrent_initial_handoffs_are_serialized() {
        let (navigation, lifecycle) = states();
        let first = handoff('a', 'R');
        let second_ui = Cell::new(false);
        let second_exit = Cell::new(false);
        let first_written = Cell::new(false);
        apply_validated_handoff(
            first,
            &navigation,
            &lifecycle,
            |_| {
                assert!(apply_validated_handoff(
                    handoff('b', 'S'),
                    &navigation,
                    &lifecycle,
                    |_| {
                        second_ui.set(true);
                        Ok(())
                    },
                    || second_exit.set(true),
                    |_| panic!("second handoff scheduled a timeout"),
                    |_, _| panic!("second handoff emitted a receipt"),
                )
                .is_err());
                Ok(())
            },
            || panic!("initial handoff requested rotation"),
            |_| {},
            |_, status| {
                assert_eq!(status, ShellHandoffReceiptStatus::BindingAdmitted);
                first_written.set(true);
                Ok(())
            },
        )
        .unwrap();
        assert!(first_written.get());
        assert!(!second_ui.get());
        assert!(!second_exit.get());
        assert_eq!(*lifecycle.lock().unwrap(), ShellHandoffLifecycle::Idle);
    }

    #[test]
    fn admission_timeout_requests_exit_and_cannot_publish_later() {
        let (navigation, lifecycle) = states();
        let exit_requested = Cell::new(false);
        let ui_applied = Cell::new(false);
        let receipt_written = Cell::new(false);
        assert!(apply_validated_handoff(
            handoff('a', 'R'),
            &navigation,
            &lifecycle,
            |_| {
                ui_applied.set(true);
                Ok(())
            },
            || exit_requested.set(true),
            |receipt| assert!(expire_pending_admission(&lifecycle, &receipt).unwrap()),
            |_, _| {
                receipt_written.set(true);
                Ok(())
            },
        )
        .is_err());
        assert!(exit_requested.get());
        assert!(!ui_applied.get());
        assert!(!receipt_written.get());
        assert_eq!(*lifecycle.lock().unwrap(), ShellHandoffLifecycle::Exiting);
        publish_rotation_receipt_on_exit(&lifecycle, |_, _| {
            panic!("timed-out admission emitted a receipt")
        })
        .unwrap();
    }

    #[test]
    fn invalid_forwarded_input_cannot_kill_or_rebind_a_healthy_shell() {
        let (navigation, lifecycle) = bound_states();
        let before = navigation.lock().unwrap().clone();
        let ui_applied = Cell::new(false);
        let exit_requested = Cell::new(false);
        let receipt_written = Cell::new(false);
        let result = consume_and_apply_handoff(
            Path::new("invalid-handoff"),
            &navigation,
            &lifecycle,
            |_| Err(anyhow!("injected invalid handoff")),
            |_| {
                ui_applied.set(true);
                Ok(())
            },
            || exit_requested.set(true),
            |_| {},
            |_, _| {
                receipt_written.set(true);
                Ok(())
            },
        );
        assert!(result.is_err());
        assert!(!ui_applied.get());
        assert!(!exit_requested.get());
        assert!(!receipt_written.get());
        assert_eq!(*navigation.lock().unwrap(), before);
        assert_eq!(*lifecycle.lock().unwrap(), ShellHandoffLifecycle::Idle);
    }

    #[test]
    fn invalid_initial_handoff_requests_process_exit() {
        let exit_code = Cell::new(None);
        reject_initial_handoff(&anyhow!("injected invalid initial handoff"), |code| {
            exit_code.set(Some(code))
        });
        assert_eq!(exit_code.get(), Some(1));
    }

    #[test]
    fn rotation_receipt_is_published_only_from_exit_handler() {
        let (navigation, lifecycle) = bound_states();
        let mut forwarded = handoff('b', 'S');
        forwarded.runtime_port = 9999;
        let writes = RefCell::new(Vec::new());
        apply_validated_handoff(
            forwarded,
            &navigation,
            &lifecycle,
            |_| panic!("rotation navigated"),
            || {},
            |_| {},
            |_, status| {
                writes.borrow_mut().push(status);
                Ok(())
            },
        )
        .unwrap();
        assert!(writes.borrow().is_empty());

        publish_rotation_receipt_on_exit(&lifecycle, |_, status| {
            writes.borrow_mut().push(status);
            Ok(())
        })
        .unwrap();
        assert_eq!(
            *writes.borrow(),
            vec![ShellHandoffReceiptStatus::RotationRequired]
        );
        publish_rotation_receipt_on_exit(&lifecycle, |_, status| {
            writes.borrow_mut().push(status);
            Ok(())
        })
        .unwrap();
        assert_eq!(writes.borrow().len(), 1);
    }
}
