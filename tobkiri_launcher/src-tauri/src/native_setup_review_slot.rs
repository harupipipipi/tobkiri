//! Place the review beside the parent WebKit view, in AppKit points.

use super::{
    valid_bounds, SetupReviewConfirmation, SetupReviewListing, SetupReviewView, MAX_LISTING_BYTES,
};
use block2::RcBlock;
use objc2::{
    rc::{Retained, Weak},
    runtime::AnyObject,
    MainThreadMarker, MainThreadOnly, Message,
};
use objc2_app_kit::{NSView, NSWindowOrderingMode};
use objc2_foundation::{NSError, NSPoint, NSRect, NSSize, NSString};
use objc2_web_kit::{WKContentWorld, WKWebView};
use serde::Deserialize;
use std::cell::{Cell, RefCell};
use std::rc::Rc;
use std::time::{Duration, Instant};

const MAX_DIMENSION: f64 = 16_384.0;
const VIEWPORT_TOLERANCE: f64 = 1.0;
const MEASURE: &str = include_str!("../../frontend/src/lib/measureSetupReviewSlot.js");
const MEASUREMENT_TIMEOUT: Duration = Duration::from_secs(1);

enum Display {
    Waiting(PendingReview),
    Mounted(SetupReviewSlot),
}

pub(super) type ConfirmationSender = tokio::sync::oneshot::Sender<SetupReviewConfirmation>;

struct PendingReview {
    listing: SetupReviewListing,
    confirmation: Option<ConfirmationSender>,
}

/// One native candidate and its exact parent page, confined to the UI thread.
#[allow(dead_code)] // The Launcher owner will supply the admitted worker result.
pub(crate) struct SetupReviewPresentation {
    parent: Retained<WKWebView>,
    owner: Retained<NSView>,
    url: String,
    port: u16,
    anchor: ReviewAnchor,
    state: Rc<RefCell<PresentationState>>,
}

struct PresentationState {
    display: Option<Display>,
    measurement_pending: bool,
}

#[allow(dead_code)]
impl SetupReviewPresentation {
    /// Capture the exact URL of the already validated Setup owner and its listing.
    pub(crate) fn new(
        parent: &WKWebView,
        owner: &NSView,
        port: u16,
        anchor: ReviewAnchor,
        listing: String,
    ) -> Result<Self, &'static str> {
        Self::with_confirmation(
            parent,
            owner,
            port,
            anchor,
            SetupReviewListing::new(listing),
            None,
        )
    }

    pub(super) fn with_confirmation(
        parent: &WKWebView,
        owner: &NSView,
        port: u16,
        anchor: ReviewAnchor,
        listing: SetupReviewListing,
        confirmation: Option<ConfirmationSender>,
    ) -> Result<Self, &'static str> {
        if listing.text().is_empty() || listing.text().len() > MAX_LISTING_BYTES {
            return Err("Setup review listing exceeds its display limit");
        }
        if !contains_view(owner, parent) {
            return Err("Setup review owner is unavailable");
        }
        Ok(Self {
            parent: parent.retain(),
            owner: owner.retain(),
            url: eligible_owner(parent, port).ok_or("Setup review owner is unavailable")?,
            port,
            anchor,
            state: Rc::new(RefCell::new(PresentationState {
                display: Some(Display::Waiting(PendingReview {
                    listing,
                    confirmation,
                })),
                measurement_pending: false,
            })),
        })
    }

    /// Coalesce refresh requests and fail closed while geometry is unavailable.
    pub(crate) fn refresh(&self) {
        let mut state = self.state.borrow_mut();
        if !contains_view(&self.owner, &self.parent)
            || eligible_owner(&self.parent, self.port).as_ref() != Some(&self.url)
        {
            state.display = None;
            return;
        }
        if state.measurement_pending || state.display.is_none() {
            return;
        }
        state.measurement_pending = true;
        drop(state);

        let parent = Weak::new(&*self.parent);
        let owner = Weak::new(&*self.owner);
        let url = self.url.clone();
        let port = self.port;
        let anchor = self.anchor.clone();
        let presentation = Rc::downgrade(&self.state);
        let deadline = Instant::now() + MEASUREMENT_TIMEOUT;
        measure(&self.parent, move |sample| {
            let Some(presentation) = presentation.upgrade() else {
                return;
            };
            let mut state = presentation.borrow_mut();
            state.measurement_pending = false;
            let (Some(parent), Some(owner)) = (parent.load(), owner.load()) else {
                state.display = None;
                return;
            };
            if !contains_view(&owner, &parent)
                || eligible_owner(&parent, port).as_ref() != Some(&url)
            {
                state.display = None;
                return;
            }
            if Instant::now() >= deadline {
                if let Some(Display::Mounted(slot)) = state.display.as_ref() {
                    slot.update(None);
                }
                return;
            }
            let Some(sample) = sample.filter(|sample| sample.matches(&anchor)) else {
                state.display = None;
                return;
            };
            let geometry = sample.geometry();
            match state.display.as_mut() {
                Some(Display::Waiting(pending)) => {
                    if let (Some(geometry), Some(mtm)) = (geometry, MainThreadMarker::new()) {
                        let slot = if let Some(reply) = pending.confirmation.take() {
                            let confirm = ParentConfirmation {
                                parent: Weak::new(&*parent),
                                url,
                                port,
                                anchor,
                                reply,
                            };
                            SetupReviewSlot::mount_review(
                                mtm,
                                &parent,
                                &owner,
                                geometry,
                                pending.listing.text(),
                                Some(confirm),
                            )
                        } else {
                            SetupReviewSlot::mount(
                                mtm,
                                &parent,
                                &owner,
                                geometry,
                                pending.listing.text(),
                            )
                        };
                        if let Ok(slot) = slot {
                            state.display = Some(Display::Mounted(slot));
                        } else {
                            state.display = None;
                        }
                    }
                }
                Some(Display::Mounted(slot)) => {
                    slot.update(geometry);
                }
                None => {}
            }
        });
    }

    /// Retire this candidate on replacement, navigation or owner hide.
    pub(crate) fn retire(&self) {
        self.state.borrow_mut().display = None;
    }
}

struct ParentConfirmation {
    parent: Weak<WKWebView>,
    url: String,
    port: u16,
    anchor: ReviewAnchor,
    reply: ConfirmationSender,
}

impl ParentConfirmation {
    fn complete(self, gesture: SetupReviewConfirmation, shown: Rc<Cell<Option<ReviewGeometry>>>) {
        if self.reply.is_closed() || gesture.confirmation_digest() != self.anchor.confirmation() {
            return;
        }
        let Some(parent) = self.parent.load() else {
            return;
        };
        let Some(clicked_geometry) = shown.get() else {
            return;
        };
        if eligible_owner(&parent, self.port).as_ref() != Some(&self.url) {
            return;
        }
        let deadline = Instant::now() + MEASUREMENT_TIMEOUT;
        let (reply, wait) = tokio::sync::oneshot::channel();
        let ParentConfirmation {
            parent: current,
            url,
            port,
            anchor,
            reply: action,
        } = self;
        measure(&parent, move |sample| {
            if Instant::now() < deadline
                && shown.get() == Some(clicked_geometry)
                && current
                    .load()
                    .is_some_and(|parent| eligible_owner(&parent, port).as_ref() == Some(&url))
                && sample.is_some_and(|sample| {
                    sample.matches(&anchor) && sample.geometry() == Some(clicked_geometry)
                })
            {
                let _ = reply.send(gesture);
            }
        });
        // Timeout releases the caller even if WebKit never replies. The late
        // callback holds no sender for the original action.
        tauri::async_runtime::spawn(async move {
            if let Ok(Ok(gesture)) =
                tokio::time::timeout(deadline.saturating_duration_since(Instant::now()), wait).await
            {
                let _ = action.send(gesture);
            }
        });
    }
}

pub(super) fn eligible_owner(parent: &WKWebView, port: u16) -> Option<String> {
    let window = parent.window()?;
    if !window.isVisible()
        || window.isMiniaturized()
        || window.attachedSheet().is_some()
        || parent.isHiddenOrHasHiddenAncestor()
    {
        return None;
    }
    let url = unsafe { parent.URL() }?.absoluteString()?.to_string();
    let parsed = tauri::Url::parse(&url).ok()?;
    (parsed.scheme() == "http"
        && matches!(parsed.host_str(), Some("127.0.0.1" | "localhost"))
        && parsed.port_or_known_default() == Some(port)
        && parsed.path() == "/panel/setup"
        && parsed.query().is_none()
        && parsed.fragment().is_none()
        && parsed.username().is_empty()
        && parsed.password().is_none())
    .then_some(url)
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct ReviewAnchor {
    id: String,
    confirmation: String,
}

impl ReviewAnchor {
    pub(crate) fn confirmation(&self) -> &str {
        &self.confirmation
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
pub(crate) struct ReviewSample {
    anchor_id: String,
    confirmation: String,
    geometry: Option<ReviewGeometry>,
}

impl ReviewSample {
    pub(crate) fn anchor(&self) -> ReviewAnchor {
        ReviewAnchor {
            id: self.anchor_id.clone(),
            confirmation: self.confirmation.clone(),
        }
    }

    pub(crate) fn geometry(&self) -> Option<ReviewGeometry> {
        self.geometry
    }

    fn matches(&self, anchor: &ReviewAnchor) -> bool {
        self.anchor_id == anchor.id && self.confirmation == anchor.confirmation
    }

    fn valid(&self) -> bool {
        fn hex(text: &str, len: usize) -> bool {
            text.len() == len
                && text
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        }
        hex(&self.anchor_id, 32)
            && self
                .confirmation
                .strip_prefix("sha256:")
                .is_some_and(|digest| hex(digest, 64))
    }
}

/// Sample the fixed Setup anchor. The callback runs on the AppKit thread.
/// The owner must discard late results after a route or candidate change.
#[allow(dead_code)]
pub(crate) fn measure(parent: &WKWebView, apply: impl FnOnce(Option<ReviewSample>) + 'static) {
    let Some(mtm) = MainThreadMarker::new() else {
        apply(None);
        return;
    };
    let same_scale = unsafe { parent.pageZoom() == 1.0 && parent.magnification() == 1.0 };
    let pending = RefCell::new(Some(apply));
    let callback = RcBlock::new(move |value: *mut AnyObject, error: *mut NSError| {
        let Some(apply) = pending.borrow_mut().take() else {
            return;
        };
        let sample = if error.is_null() {
            // WebKit owns the result until the callback returns.
            unsafe { value.as_ref() }
                .and_then(AnyObject::downcast_ref::<NSString>)
                .filter(|value| value.length() <= 1024)
                .and_then(|value| serde_json::from_str::<ReviewSample>(&value.to_string()).ok())
                .filter(ReviewSample::valid)
                .map(|mut sample| {
                    if !same_scale {
                        sample.geometry = None;
                    }
                    sample
                })
        } else {
            None
        };
        apply(sample);
    });
    unsafe {
        let world = WKContentWorld::defaultClientWorld(mtm);
        parent.evaluateJavaScript_inFrame_inContentWorld_completionHandler(
            &NSString::from_str(MEASURE),
            None,
            &world,
            Some(&callback),
        )
    };
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
struct Edges {
    left: f64,
    top: f64,
    right: f64,
    bottom: f64,
}

impl Edges {
    fn bounded(self) -> bool {
        [self.left, self.top, self.right, self.bottom]
            .into_iter()
            .all(|edge| edge.abs() <= MAX_DIMENSION)
            && (0.0..=MAX_DIMENSION).contains(&(self.right - self.left))
            && (0.0..=MAX_DIMENSION).contains(&(self.bottom - self.top))
            && self.left < self.right
            && self.top < self.bottom
    }

    fn contains(self, other: Self) -> bool {
        self.left <= other.left
            && self.top <= other.top
            && self.right >= other.right
            && self.bottom >= other.bottom
    }

    fn in_view(self, bounds: NSRect, flipped: bool) -> NSRect {
        let y = if flipped {
            self.top
        } else {
            bounds.size.height - self.bottom
        };
        NSRect::new(
            NSPoint::new(bounds.origin.x + self.left, bounds.origin.y + y),
            NSSize::new(self.right - self.left, self.bottom - self.top),
        )
    }
}

/// The parent page supplies presentation coordinates only.
#[allow(dead_code)] // Consumed by the native slot and its actual WebKit test.
#[derive(Clone, Copy, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
pub(crate) struct ReviewGeometry {
    full: Edges,
    clip: Edges,
    viewport_width: f64,
    viewport_height: f64,
}

impl ReviewGeometry {
    fn frames(self, bounds: NSRect, flipped: bool) -> Option<(NSRect, NSRect)> {
        let viewport = Edges {
            left: 0.0,
            top: 0.0,
            right: self.viewport_width,
            bottom: self.viewport_height,
        };
        if !valid_bounds(bounds)
            || !viewport.bounded()
            || !self.full.bounded()
            || !self.clip.bounded()
            || !viewport.contains(self.clip)
            || !self.full.contains(self.clip)
            || (self.viewport_width - bounds.size.width).abs() > VIEWPORT_TOLERANCE
            || (self.viewport_height - bounds.size.height).abs() > VIEWPORT_TOLERANCE
        {
            return None;
        }
        // DOM viewport dimensions round to CSS pixels. Clamp at the native
        // edge even when the two measurements differ by less than one point.
        let clip = Edges {
            right: self.clip.right.min(bounds.size.width),
            bottom: self.clip.bottom.min(bounds.size.height),
            ..self.clip
        };
        clip.bounded().then(|| {
            (
                self.full.in_view(bounds, flipped),
                clip.in_view(bounds, flipped),
            )
        })
    }
}

/// Owns the clipping and full-size views. Moving the clip does not reflow the review.
#[allow(dead_code)] // Activated by the Setup owner after native listing preparation.
pub(crate) struct SetupReviewSlot {
    _review: SetupReviewView,
    parent: Retained<NSView>,
    owner: Retained<NSView>,
    clip: Retained<NSView>,
    full: Retained<NSView>,
    shown: Rc<Cell<Option<ReviewGeometry>>>,
}

#[allow(dead_code)]
impl SetupReviewSlot {
    /// The caller must supply the actual parent page view, on its UI thread.
    pub(crate) fn mount(
        mtm: MainThreadMarker,
        parent: &NSView,
        owner: &NSView,
        geometry: ReviewGeometry,
        listing: &str,
    ) -> Result<Self, &'static str> {
        Self::mount_review(mtm, parent, owner, geometry, listing, None)
    }

    fn mount_review(
        mtm: MainThreadMarker,
        parent: &NSView,
        owner: &NSView,
        geometry: ReviewGeometry,
        listing: &str,
        confirmation: Option<ParentConfirmation>,
    ) -> Result<Self, &'static str> {
        if !contains_view(owner, parent) || parent.isHiddenOrHasHiddenAncestor() {
            return Err("Setup owner view is unavailable");
        }
        let (full, clip) = geometry
            .frames(parent.bounds(), parent.isFlipped())
            .ok_or("Setup review slot geometry is invalid")?;
        let clip_view = NSView::initWithFrame(
            NSView::alloc(mtm),
            owner.convertRect_fromView(clip, Some(parent)),
        );
        clip_view.setClipsToBounds(true);
        clip_view.setAutoresizesSubviews(false);
        let full_view = NSView::initWithFrame(
            NSView::alloc(mtm),
            NSRect::new(NSPoint::new(0.0, 0.0), full.size),
        );
        clip_view.addSubview(&full_view);
        let shown = Rc::new(Cell::new(Some(geometry)));
        let review = match confirmation {
            Some(confirmation) => {
                let shown = shown.clone();
                SetupReviewView::mount_with_confirmation(mtm, &full_view, listing, move |gesture| {
                    confirmation.complete(gesture, shown);
                })
            }
            None => SetupReviewView::mount(mtm, &full_view, listing),
        }?;
        owner.addSubview_positioned_relativeTo(
            &clip_view,
            NSWindowOrderingMode::Above,
            Some(parent),
        );
        full_view.setFrame(clip_view.convertRect_fromView(full, Some(parent)));
        Ok(Self {
            _review: review,
            parent: parent.retain(),
            owner: owner.retain(),
            clip: clip_view,
            full: full_view,
            shown,
        })
    }

    /// Hide while outside the viewport or while the owner is changing layout.
    pub(crate) fn update(&self, geometry: Option<ReviewGeometry>) -> bool {
        let frames = geometry.and_then(|geometry| {
            let parent = &*self.parent;
            if !contains_view(&self.owner, parent)
                || !contains_view(&self.owner, &self.clip)
                || parent.isHiddenOrHasHiddenAncestor()
            {
                return None;
            }
            let (full, clip) = geometry.frames(parent.bounds(), parent.isFlipped())?;
            Some((full, self.owner.convertRect_fromView(clip, Some(parent))))
        });
        let Some((full, clip)) = frames else {
            self.shown.set(None);
            self.clip.setHidden(true);
            return false;
        };
        self.clip.setFrame(clip);
        self.full
            .setFrame(self.clip.convertRect_fromView(full, Some(&self.parent)));
        self.clip.setHidden(false);
        self.shown.set(geometry);
        true
    }
}

fn contains_view(owner: &NSView, view: &NSView) -> bool {
    owner
        .subviews()
        .iter()
        .any(|child| std::ptr::eq(&*child, view))
}

impl Drop for SetupReviewSlot {
    fn drop(&mut self) {
        self.shown.set(None);
        self.clip.removeFromSuperview();
    }
}
