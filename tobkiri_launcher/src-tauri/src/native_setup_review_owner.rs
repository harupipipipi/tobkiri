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
    generation: u64,
    window_state: SetupReviewWindowState,
}

struct OwnerState {
    generation: u64,
    presentation: Option<SetupReviewPresentation>,
}

/// The Launcher manages the presentation without retaining its own AppHandle.
#[derive(Clone)]
pub(crate) struct SetupReviewWindowState(Arc<MainThreadBound<RefCell<OwnerState>>>);

#[allow(dead_code)]
impl SetupReviewWindowState {
    pub(crate) fn new(mtm: MainThreadMarker) -> Self {
        Self(Arc::new(MainThreadBound::new(
            RefCell::new(OwnerState {
                generation: 0,
                presentation: None,
            }),
            mtm,
        )))
    }

    /// A new request supersedes all pending results for the previous candidate.
    pub(crate) fn begin(
        &self,
        window: tauri::WebviewWindow,
        mtm: MainThreadMarker,
        port: u16,
    ) -> Result<SetupReviewOwner, &'static str> {
        if window.label() != "main" {
            return Err("Setup review requires the Launcher main window");
        }
        let generation = self
            .retire(mtm)
            .ok_or("Setup review generation exhausted")?;
        Ok(SetupReviewOwner {
            window,
            port,
            generation,
            window_state: self.clone(),
        })
    }

    pub(crate) fn refresh(&self, mtm: MainThreadMarker) {
        if let Some(presentation) = &self.0.get(mtm).borrow().presentation {
            presentation.refresh();
        }
    }

    pub(crate) fn retire(&self, mtm: MainThreadMarker) -> Option<u64> {
        let mut state = self.0.get(mtm).borrow_mut();
        if let Some(presentation) = state.presentation.take() {
            presentation.retire();
        }
        // Reserve the final value for permanent exhaustion. An earlier
        // pending worker can never hold this generation.
        state.generation = state.generation.saturating_add(1);
        (state.generation != u64::MAX).then_some(state.generation)
    }
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
    /// Admit the listing on the UI thread and start its first geometry sample.
    pub(crate) async fn display(&self, listing: String) -> Result<(), SetupReviewOwnerError> {
        use SetupReviewOwnerError::{Retired, Schedule, TimedOut, Unavailable};

        let state = self.window_state.0.clone();
        let port = self.port;
        let generation = self.generation;
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
                    if state.generation != generation {
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
}
