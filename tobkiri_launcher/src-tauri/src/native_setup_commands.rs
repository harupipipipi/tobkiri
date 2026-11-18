use std::sync::{Arc, Mutex};

use serde::Serialize;
use tauri::{State, Url, WebviewWindow};

use crate::{kernel_manager::KernelManager, AppConfig};

const CALLER_DENIED: &str = "Tobkiri Setup requires the main Setup window";

fn validate_setup_caller(label: &str, url: &Url, port: u16) -> Result<(), &'static str> {
    crate::validate_panel_session_caller_context(label, url, port).map_err(|_| CALLER_DENIED)?;
    if url.path() != "/panel/setup"
        || !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
    {
        return Err(CALLER_DENIED);
    }
    Ok(())
}

fn validate_window(window: &WebviewWindow, port: u16) -> Result<(), &'static str> {
    let url = window.url().map_err(|_| CALLER_DENIED)?;
    validate_setup_caller(window.label(), &url, port)
}

#[derive(Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum NativeSetupRoute {
    Legacy,
    #[cfg(all(target_os = "macos", feature = "native-first-setup"))]
    Native,
    #[cfg(all(target_os = "macos", feature = "native-first-setup"))]
    Unavailable,
}

#[tauri::command]
pub(crate) async fn native_setup_route(
    window: WebviewWindow,
    config: State<'_, AppConfig>,
    kernel: State<'_, Arc<Mutex<KernelManager>>>,
) -> Result<NativeSetupRoute, &'static str> {
    validate_window(&window, config.kernel_port)?;
    #[cfg(all(target_os = "macos", feature = "native-first-setup"))]
    {
        let config = config.inner().clone();
        let kernel = Arc::clone(kernel.inner());
        tauri::async_runtime::spawn_blocking(move || {
            let generation = {
                let mut manager = kernel.lock().map_err(|_| "Tobkiri Kernel is unavailable")?;
                if manager.requires_native_setup() {
                    return Ok(if manager.native_setup_launch().is_some() {
                        NativeSetupRoute::Native
                    } else {
                        NativeSetupRoute::Unavailable
                    });
                }
                manager.launch_generation()
            };
            if generation == 0
                && crate::kernel_manager::native_first_setup_enabled(&config)
                && !crate::defaultspack_authority::has_verified_active_profile(&config)
                    .map_err(|_| "Tobkiri Setup authority could not be verified")?
            {
                return Ok(NativeSetupRoute::Unavailable);
            }
            Ok(NativeSetupRoute::Legacy)
        })
        .await
        .map_err(|_| "Tobkiri Setup route could not be read")?
    }
    #[cfg(not(all(target_os = "macos", feature = "native-first-setup")))]
    {
        let _ = kernel;
        Ok(NativeSetupRoute::Legacy)
    }
}

#[derive(Serialize)]
#[serde(tag = "state", rename_all = "snake_case")]
pub(crate) enum NativeReviewReply {
    Unavailable,
    #[cfg(all(target_os = "macos", feature = "native-first-setup"))]
    Published,
    #[cfg(all(target_os = "macos", feature = "native-first-setup"))]
    VerificationRequired,
    #[cfg(all(target_os = "macos", feature = "native-first-setup"))]
    Rejected {
        response: String,
    },
}

/// The panel requests a review. The isolated WebKit view accepts the approval.
#[tauri::command]
pub(crate) async fn start_native_setup_review(
    window: WebviewWindow,
    config: State<'_, AppConfig>,
) -> Result<NativeReviewReply, &'static str> {
    validate_window(&window, config.kernel_port)?;
    #[cfg(all(target_os = "macos", feature = "native-first-setup"))]
    {
        use crate::native_harness_bootstrap::{
            prepare_setup_review, SetupReviewCompletion, SetupReviewStartError,
        };
        use tauri::Manager;

        return Ok(match prepare_setup_review(window.app_handle()).await {
            Ok(SetupReviewCompletion::Published) => NativeReviewReply::Published,
            Ok(SetupReviewCompletion::Rejected(response)) => {
                NativeReviewReply::Rejected { response }
            }
            Err(SetupReviewStartError::Activation(_) | SetupReviewStartError::Restart(_)) => {
                NativeReviewReply::VerificationRequired
            }
            Err(_) => NativeReviewReply::Unavailable,
        });
    }
    #[cfg(not(all(target_os = "macos", feature = "native-first-setup")))]
    Ok(NativeReviewReply::Unavailable)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn native_setup_ipc_requires_the_exact_main_loopback_setup_route() {
        let setup = Url::parse("http://127.0.0.1:8765/panel/setup").unwrap();
        assert!(validate_setup_caller("main", &setup, 8765).is_ok());
        assert!(validate_setup_caller("secondary", &setup, 8765).is_err());
        assert!(validate_setup_caller("main", &setup, 8766).is_err());
        for denied in [
            "http://localhost:8765/panel",
            "http://localhost:8765/panel/setup/",
            "http://localhost:8765/panel/setup?x=1",
            "http://localhost:8765/panel/setup#x",
            "http://local@localhost:8765/panel/setup",
            "http://example.invalid:8765/panel/setup",
            "https://localhost:8765/panel/setup",
        ] {
            assert!(validate_setup_caller("main", &Url::parse(denied).unwrap(), 8765).is_err());
        }
    }
}
