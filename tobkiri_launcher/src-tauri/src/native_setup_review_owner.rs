//! Bridge the existing Tauri main window to its AppKit-owned review.

use super::slot::SetupReviewPresentation;
use dispatch2::MainThreadBound;
use objc2::{rc::Retained, MainThreadMarker};
use objc2_web_kit::WKWebView;
use std::{
    cell::RefCell,
    sync::Arc,
    time::{Duration, Instant},
};

const UI_HANDOFF_TIMEOUT: Duration = Duration::from_secs(2);

/// Bind to a native-selected parent once. A worker result cannot name a window
/// or reach the parent page's scripting bridge.
#[allow(dead_code)] // The original worker supplies the admitted listing.
#[derive(Clone)]
pub(crate) struct SetupReviewOwner {
    window: tauri::WebviewWindow,
    port: u16,
    presentation: Arc<MainThreadBound<RefCell<OwnerState>>>,
}

struct OwnerState {
    retired: bool,
    presentation: Option<SetupReviewPresentation>,
}

#[derive(Debug)]
pub(crate) enum SetupReviewOwnerError {
    Schedule(tauri::Error),
    Unavailable,
    Retired,
    TimedOut,
}

#[allow(dead_code)]
impl SetupReviewOwner {
    /// The original Launcher calls this on the UI thread.
    pub(crate) fn bind(
        window: tauri::WebviewWindow,
        mtm: MainThreadMarker,
        port: u16,
    ) -> Result<Self, &'static str> {
        if window.label() != "main" {
            return Err("Setup review requires the Launcher main window");
        }
        Ok(Self {
            window,
            port,
            presentation: Arc::new(MainThreadBound::new(
                RefCell::new(OwnerState {
                    retired: false,
                    presentation: None,
                }),
                mtm,
            )),
        })
    }

    /// Admit the listing on the UI thread and start its first geometry sample.
    pub(crate) async fn display(&self, listing: String) -> Result<(), SetupReviewOwnerError> {
        use SetupReviewOwnerError::{Retired, Schedule, TimedOut, Unavailable};

        let state = self.presentation.clone();
        let port = self.port;
        let deadline = Instant::now() + UI_HANDOFF_TIMEOUT;
        let (reply, wait) = tokio::sync::oneshot::channel();
        self.window
            .with_webview(move |webview| {
                let admit = || {
                    if reply.is_closed() || Instant::now() >= deadline {
                        return Err(TimedOut);
                    }
                    let mtm = MainThreadMarker::new().ok_or(Unavailable)?;
                    let mut state = state.get(mtm).try_borrow_mut().map_err(|_| Unavailable)?;
                    if state.retired {
                        return Err(Retired);
                    }
                    // Tauri's platform handle borrows the live WKWebView on this thread.
                    let parent = unsafe { Retained::<WKWebView>::retain(webview.inner().cast()) }
                        .ok_or(Unavailable)?;
                    // Wry installs the primary WKWebView under the content view.
                    let owner = parent
                        .window()
                        .and_then(|window| window.contentView())
                        .ok_or(Unavailable)?;
                    let presentation = SetupReviewPresentation::new(&parent, &owner, port, listing)
                        .map_err(|_| Unavailable)?;
                    if reply.is_closed() || Instant::now() >= deadline {
                        return Err(TimedOut);
                    }
                    if let Some(previous) = state.presentation.replace(presentation) {
                        previous.retire();
                    }
                    Ok(())
                };
                let result = admit();
                let accepted = result.is_ok();
                let delivered = reply.send(result).is_ok();
                if !accepted {
                    return;
                }
                let Some(mtm) = MainThreadMarker::new() else {
                    return;
                };
                if !delivered {
                    if let Some(presentation) = state.get(mtm).borrow_mut().presentation.take() {
                        presentation.retire();
                    }
                    return;
                }
                let state = state.get(mtm).borrow();
                if let Some(presentation) = &state.presentation {
                    presentation.refresh();
                }
            })
            .map_err(Schedule)?;
        tokio::time::timeout(deadline.saturating_duration_since(Instant::now()), wait)
            .await
            .map_err(|_| TimedOut)?
            .map_err(|_| Unavailable)?
    }

    /// Re-evaluate the original Setup marker after layout changes.
    pub(crate) fn refresh(&self, mtm: MainThreadMarker) {
        if let Some(presentation) = &self.presentation.get(mtm).borrow().presentation {
            presentation.refresh();
        }
    }

    /// Retire this owner before navigation, hide or candidate replacement.
    pub(crate) fn retire(&self, mtm: MainThreadMarker) {
        let mut state = self.presentation.get(mtm).borrow_mut();
        state.retired = true;
        if let Some(presentation) = state.presentation.take() {
            presentation.retire();
        }
    }
}
