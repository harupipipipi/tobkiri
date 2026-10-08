//! Own and retire only the native panel created for one directory request.
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

// Leave time for the attested reply before the Host's 300-second deadline.
const PICKER_TIMEOUT: Duration = Duration::from_secs(240);

#[cfg(target_os = "macos")]
pub(crate) fn pick(
    app: &tauri::AppHandle,
    cancelled: Arc<AtomicBool>,
) -> Result<Option<Vec<PathBuf>>, String> {
    use dispatch2::{DispatchQueue, MainThreadBound};
    use objc2::MainThreadMarker;
    use objc2_app_kit::{NSModalResponseOK, NSOpenPanel};
    use objc2_foundation::NSString;

    let (result_tx, result_rx) = std::sync::mpsc::channel();
    let deadline = Instant::now() + PICKER_TIMEOUT;
    let work_cancelled = cancelled.clone();
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
        panel.setTitle(Some(&NSString::from_str("Choose Tobkiri project folders")));
        panel.setCanChooseFiles(false);
        panel.setCanChooseDirectories(true);
        panel.setAllowsMultipleSelection(true);
        panel.setCanCreateDirectories(false);
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
pub(crate) fn pick(
    app: &tauri::AppHandle,
    cancelled: Arc<AtomicBool>,
) -> Result<Option<Vec<PathBuf>>, String> {
    use tauri_plugin_dialog::DialogExt;
    let selected = app
        .dialog()
        .file()
        .set_title("Choose Tobkiri project folders")
        .blocking_pick_folders();
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
