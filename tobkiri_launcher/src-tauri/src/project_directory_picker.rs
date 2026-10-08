//! Own and retire only the native panel created for one directory request.
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

// Leave time for the attested reply before the Host's 300-second deadline.
const PICKER_TIMEOUT: Duration = Duration::from_secs(240);

pub(crate) fn pick(
    app: &tauri::AppHandle,
    cancelled: Arc<AtomicBool>,
) -> Result<Option<Vec<PathBuf>>, String> {
    pick_named(app, cancelled, "Choose Tobkiri project folders", true)
}

#[cfg(target_os = "macos")]
fn configure_panel(panel: &objc2_app_kit::NSOpenPanel, title: &str, multiple: bool) {
    use objc2_foundation::NSString;
    panel.setTitle(Some(&NSString::from_str(title)));
    panel.setMessage(Some(&NSString::from_str(title)));
    // AppKit reuses NSOpenPanel. A previous .app filter must not disable folders.
    #[allow(deprecated)]
    panel.setAllowedFileTypes(None);
    panel.setCanChooseFiles(false);
    panel.setCanChooseDirectories(true);
    panel.setAllowsMultipleSelection(multiple);
    panel.setCanCreateDirectories(false);
}

#[cfg(target_os = "macos")]
pub(crate) fn pick_named(
    app: &tauri::AppHandle,
    cancelled: Arc<AtomicBool>,
    title: &str,
    multiple: bool,
) -> Result<Option<Vec<PathBuf>>, String> {
    use dispatch2::{DispatchQueue, MainThreadBound};
    use objc2::MainThreadMarker;
    use objc2_app_kit::{NSModalResponseOK, NSOpenPanel};

    let (result_tx, result_rx) = std::sync::mpsc::channel();
    let deadline = Instant::now() + PICKER_TIMEOUT;
    let work_cancelled = cancelled.clone();
    let title = title.to_owned();
    app.run_on_main_thread(move || {
        if work_cancelled.load(Ordering::Acquire) || Instant::now() >= deadline {
            let _ = result_tx.send(Ok(None));
            return;
        }
        let Some(mtm) = MainThreadMarker::new() else {
            let _ = result_tx.send(Err("Project folder picker is unavailable".into()));
            return;
        };
        let panel = NSOpenPanel::openPanel(mtm);
        configure_panel(&panel, &title, multiple);
        let main_owner = Arc::new(MainThreadBound::new(panel.clone(), mtm));
        let owned = main_owner.clone();
        let (done_tx, done_rx) = std::sync::mpsc::channel();
        let watcher_cancelled = work_cancelled.clone();
        let watchdog = std::thread::spawn(move || loop {
            match done_rx.recv_timeout(Duration::from_millis(100)) {
                Ok(()) | Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => break,
                Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {}
            }
            if watcher_cancelled.load(Ordering::Acquire) || Instant::now() >= deadline {
                watcher_cancelled.store(true, Ordering::Release);
                DispatchQueue::main().exec_async(move || {
                    if let Some(mtm) = MainThreadMarker::new() {
                        // This retained panel belongs to this request. Never
                        // find/cancel a global panel or an approval window.
                        unsafe { owned.get(mtm).cancel(None) };
                    }
                });
                break;
            }
        });
        let response = panel.runModal();
        let _ = done_tx.send(());
        let _ = watchdog.join();
        drop(main_owner);
        let result = if work_cancelled.load(Ordering::Acquire) || Instant::now() >= deadline {
            Ok(None)
        } else if response == NSModalResponseOK {
            panel
                .URLs()
                .iter()
                .map(|url| {
                    url.path()
                        .map(|path| PathBuf::from(path.to_string()))
                        .ok_or_else(|| "Project folder picker returned an invalid path".to_string())
                })
                .collect::<Result<Vec<_>, _>>()
                .map(Some)
        } else {
            Ok(None)
        };
        let _ = result_tx.send(result);
    })
    .map_err(|_| "Project folder picker could not be scheduled".to_string())?;
    match result_rx.recv_timeout(PICKER_TIMEOUT + Duration::from_secs(5)) {
        Ok(result) => result,
        Err(_) => {
            // Also fences a delayed main-thread task before it can open UI.
            cancelled.store(true, Ordering::Release);
            Err("Project folder picker timed out".into())
        }
    }
}

#[cfg(not(target_os = "macos"))]
pub(crate) fn pick_named(
    app: &tauri::AppHandle,
    cancelled: Arc<AtomicBool>,
    title: &str,
    multiple: bool,
) -> Result<Option<Vec<PathBuf>>, String> {
    use tauri_plugin_dialog::DialogExt;
    let dialog = app.dialog().file().set_title(title);
    let selected = if multiple {
        dialog.blocking_pick_folders()
    } else {
        dialog.blocking_pick_folder().map(|folder| vec![folder])
    };
    if cancelled.load(Ordering::Acquire) {
        return Ok(None);
    }
    selected
        .map(|entries| {
            entries
                .into_iter()
                .map(|entry| {
                    entry
                        .into_path()
                        .map_err(|_| "Project folder picker returned an invalid path".to_string())
                })
                .collect()
        })
        .transpose()
}

/// Verify that an earlier app filter cannot disable the next folder picker.
#[cfg(all(test, target_os = "macos"))]
pub(crate) fn native_filter_reset_smoke() {
    use objc2::MainThreadMarker;
    use objc2_app_kit::NSOpenPanel;
    use objc2_foundation::{NSArray, NSString};
    let mtm = MainThreadMarker::new().expect("Native picker check requires main thread");
    let panel = NSOpenPanel::openPanel(mtm);
    #[allow(deprecated)]
    panel.setAllowedFileTypes(Some(&NSArray::from_retained_slice(&[NSString::from_str(
        "app",
    )])));
    configure_panel(
        &panel,
        "Select the previous developer Host user_data folder",
        false,
    );
    #[allow(deprecated)]
    let types = panel.allowedFileTypes();
    assert!(types.is_none_or(|types| types.is_empty()));
    assert!(panel.canChooseDirectories());
    assert!(!panel.canChooseFiles());
    assert!(!panel.allowsMultipleSelection());
    assert!(!panel.canCreateDirectories());
    configure_panel(&panel, "Choose Tobkiri project folders", true);
    #[allow(deprecated)]
    let types = panel.allowedFileTypes();
    assert!(types.is_none_or(|types| types.is_empty()));
    assert!(panel.allowsMultipleSelection());
    println!("AppKit app-filter→single-folder→project-folders reset PASS");
}
