//! Prepare signed-Pack confirmation with an explicit, validated window owner.

use raw_window_handle::{
    DisplayHandle, HandleError, HasDisplayHandle, HasWindowHandle, RawDisplayHandle,
    RawWindowHandle, WindowHandle,
};
use tauri_plugin_dialog::{
    DialogExt, MessageDialogBuilder, MessageDialogButtons, MessageDialogKind,
};

struct CapturedParent {
    window: RawWindowHandle,
    display: RawDisplayHandle,
}

impl HasWindowHandle for CapturedParent {
    fn window_handle(&self) -> Result<WindowHandle<'_>, HandleError> {
        // The real owner's validated handles remain alive in the caller for
        // the entire confirmation. The dialog builder immediately copies them.
        Ok(unsafe { WindowHandle::borrow_raw(self.window) })
    }
}

impl HasDisplayHandle for CapturedParent {
    fn display_handle(&self) -> Result<DisplayHandle<'_>, HandleError> {
        // This is the display handle captured from the same retained owner.
        Ok(unsafe { DisplayHandle::borrow_raw(self.display) })
    }
}

fn require_parent_handles<W, D>(
    window: Result<W, HandleError>,
    display: Result<D, HandleError>,
) -> Result<(W, D), String> {
    let window = window.map_err(|_| "Signed Pack review window is unavailable")?;
    let display = display.map_err(|_| "Signed Pack review display is unavailable")?;
    Ok((window, display))
}

/// Construct the dialog on the UI thread; the caller waits on a worker thread.
pub(crate) fn prepare(
    window: &tauri::WebviewWindow,
    message: String,
) -> Result<MessageDialogBuilder<tauri::Wry>, String> {
    #[cfg(target_os = "macos")]
    let _main_thread =
        objc2::MainThreadMarker::new().ok_or("Signed Pack review requires the UI thread")?;
    crate::validate_launcher_main_window(window, "signed Pack onboarding")?;
    #[cfg(target_os = "macos")]
    if window
        .ns_window()
        .map_err(|_| "Signed Pack review owner is unavailable")?
        .is_null()
    {
        return Err("Signed Pack review owner is unavailable".into());
    }
    let (window_handle, display_handle) =
        require_parent_handles(window.window_handle(), window.display_handle())?;
    let parent = CapturedParent {
        window: window_handle.as_raw(),
        display: display_handle.as_raw(),
    };
    // parent() silently ignores failed handle queries. This captured adapter
    // returns only the successfully validated pair, so no unparented fallback
    // can reach macOS CFUserNotification instead of this Launcher window.
    Ok(window
        .dialog()
        .message(message)
        .title("Review signed Pack")
        .kind(MessageDialogKind::Warning)
        .buttons(MessageDialogButtons::OkCancelCustom(
            "Trust and add".into(),
            "Cancel".into(),
        ))
        .parent(&parent))
}

#[cfg(test)]
mod tests {
    use super::require_parent_handles;
    use raw_window_handle::HandleError;

    #[test]
    fn unavailable_window_rejects_native_review() {
        assert_eq!(
            require_parent_handles::<(), ()>(Err(HandleError::Unavailable), Ok(())),
            Err("Signed Pack review window is unavailable".into())
        );
    }

    #[test]
    fn unavailable_display_rejects_native_review() {
        assert_eq!(
            require_parent_handles::<(), ()>(Ok(()), Err(HandleError::Unavailable)),
            Err("Signed Pack review display is unavailable".into())
        );
    }

    #[test]
    fn valid_parent_pair_is_preserved_for_dialog_binding() {
        assert_eq!(require_parent_handles(Ok(1_u8), Ok(2_u8)), Ok((1, 2)));
    }
}
