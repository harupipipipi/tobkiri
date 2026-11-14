//! Bridge the existing Tauri main window to its AppKit-owned review.

use super::slot::{
    eligible_owner, measure, ConfirmationSender, ReviewAnchor, SetupReviewPresentation,
};
use super::{SetupReviewConfirmation, SetupReviewListing};
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
    url: String,
    anchor: ReviewAnchor,
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

    /// Capture the actual Setup anchor before any worker receives a request.
    pub(crate) async fn begin(
        &self,
        window: tauri::WebviewWindow,
        port: u16,
    ) -> Result<SetupReviewOwner, SetupReviewOwnerError> {
        use SetupReviewOwnerError::{Retired, Schedule, TimedOut, Unavailable};

        if window.label() != "main" {
            return Err(Unavailable);
        }
        let state = self.clone();
        let deadline = Instant::now() + UI_HANDOFF_TIMEOUT;
        let (reply, wait) = tokio::sync::oneshot::channel();
        window
            .with_webview(move |webview| {
                let Some(mtm) = MainThreadMarker::new() else {
                    let _ = reply.send(Err(Unavailable));
                    return;
                };
                if reply.is_closed() || Instant::now() >= deadline {
                    return;
                }
                // Tauri borrows the current platform view for this callback.
                let Some(parent) =
                    (unsafe { Retained::<WKWebView>::retain(webview.inner().cast()) })
                else {
                    let _ = reply.send(Err(Unavailable));
                    return;
                };
                let Some(url) = eligible_owner(&parent, port) else {
                    let _ = reply.send(Err(Unavailable));
                    return;
                };
                let Some(generation) = state.retire(mtm) else {
                    let _ = reply.send(Err(Retired));
                    return;
                };
                let current = objc2::rc::Weak::new(&*parent);
                measure(&parent, move |sample| {
                    let admit = || {
                        if reply.is_closed() || Instant::now() >= deadline {
                            return Err(TimedOut);
                        }
                        if state.0.get(mtm).borrow().generation != generation {
                            return Err(Retired);
                        }
                        let parent = current.load().ok_or(Unavailable)?;
                        if eligible_owner(&parent, port).as_ref() != Some(&url) {
                            return Err(Retired);
                        }
                        let anchor = sample.ok_or(Unavailable)?.anchor();
                        Ok((generation, url, anchor))
                    };
                    let result = admit();
                    let _ = reply.send(result);
                });
            })
            .map_err(Schedule)?;
        let (generation, url, anchor) =
            tokio::time::timeout(deadline.saturating_duration_since(Instant::now()), wait)
                .await
                .map_err(|_| TimedOut)?
                .map_err(|_| Unavailable)??;
        Ok(SetupReviewOwner {
            window,
            port,
            generation,
            url,
            anchor,
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
    /// Mount the review and await a gesture for the still-visible candidate.
    /// The caller owns the wait lifetime. Dropping it closes the gesture path.
    pub(crate) async fn confirm(
        &self,
        listing: SetupReviewListing,
    ) -> Result<SetupReviewConfirmation, SetupReviewOwnerError> {
        let (reply, wait) = tokio::sync::oneshot::channel();
        self.present(listing, reply).await?;
        wait.await.map_err(|_| SetupReviewOwnerError::Retired)
    }

    async fn present(
        &self,
        listing: SetupReviewListing,
        confirm: ConfirmationSender,
    ) -> Result<(), SetupReviewOwnerError> {
        use SetupReviewOwnerError::{Retired, Schedule, TimedOut, Unavailable};

        let confirmation = super::listing_confirmation(listing.text()).ok_or(Unavailable)?;
        if confirmation != self.anchor.confirmation() {
            return Err(Retired);
        }
        let state = self.window_state.0.clone();
        let port = self.port;
        let generation = self.generation;
        let url = self.url.clone();
        let anchor = self.anchor.clone();
        let deadline = Instant::now() + UI_HANDOFF_TIMEOUT;
        let (reply, wait) = tokio::sync::oneshot::channel();
        self.window
            .with_webview(move |webview| {
                let Some(mtm) = MainThreadMarker::new() else {
                    let _ = reply.send(Err(Unavailable));
                    return;
                };
                let admit = || {
                    if reply.is_closed() || Instant::now() >= deadline {
                        return Err(TimedOut);
                    }
                    let mut state = state.get(mtm).try_borrow_mut().map_err(|_| Unavailable)?;
                    if state.generation != generation {
                        return Err(Retired);
                    }
                    // Tauri's platform handle borrows the live WKWebView on this thread.
                    let parent = unsafe { Retained::<WKWebView>::retain(webview.inner().cast()) }
                        .ok_or(Unavailable)?;
                    if eligible_owner(&parent, port).as_ref() != Some(&url) {
                        return Err(Retired);
                    }
                    // Wry installs the primary WKWebView under the content view.
                    let owner = parent
                        .window()
                        .and_then(|window| window.contentView())
                        .ok_or(Unavailable)?;
                    let presentation = SetupReviewPresentation::with_confirmation(
                        &parent,
                        &owner,
                        port,
                        anchor,
                        listing,
                        Some(confirm),
                    )
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
                if result.is_err() {
                    let _ = reply.send(result);
                    return;
                }
                if reply.send(result).is_err() {
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
