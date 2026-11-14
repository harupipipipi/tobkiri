//! Place the review beside the parent WebKit view, in AppKit points.

use super::{valid_bounds, SetupReviewView, MAX_LISTING_BYTES};
use block2::RcBlock;
use objc2::{
    rc::{Retained, Weak},
    runtime::AnyObject,
    MainThreadMarker, MainThreadOnly, Message,
};
use objc2_app_kit::{NSView, NSWindowOrderingMode};
use objc2_foundation::{NSError, NSPoint, NSRect, NSSize, NSString};
use objc2_web_kit::WKWebView;
use serde::Deserialize;
use std::cell::RefCell;
use std::rc::Rc;
use std::time::{Duration, Instant};

const MAX_DIMENSION: f64 = 16_384.0;
const VIEWPORT_TOLERANCE: f64 = 1.0;
const MEASURE: &str = include_str!("../../frontend/src/lib/measureSetupReviewSlot.js");
const MEASUREMENT_TIMEOUT: Duration = Duration::from_secs(1);

enum Display {
    Waiting(String),
    Mounted(SetupReviewSlot),
}

/// One native candidate and its exact parent page, confined to the UI thread.
#[allow(dead_code)] // The Launcher owner will supply the admitted worker result.
pub(crate) struct SetupReviewPresentation {
    parent: Retained<WKWebView>,
    url: String,
    port: u16,
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
        port: u16,
        listing: String,
    ) -> Result<Self, &'static str> {
        if listing.is_empty() || listing.len() > MAX_LISTING_BYTES {
            return Err("Setup review listing exceeds its display limit");
        }
        Ok(Self {
            parent: parent.retain(),
            url: eligible_owner(parent, port).ok_or("Setup review owner is unavailable")?,
            port,
            state: Rc::new(RefCell::new(PresentationState {
                display: Some(Display::Waiting(listing)),
                measurement_pending: false,
            })),
        })
    }

    /// Coalesce refresh requests and fail closed while geometry is unavailable.
    pub(crate) fn refresh(&self) {
        let mut state = self.state.borrow_mut();
        if eligible_owner(&self.parent, self.port).as_ref() != Some(&self.url) {
            state.display = None;
            return;
        }
        if state.measurement_pending || state.display.is_none() {
            return;
        }
        state.measurement_pending = true;
        drop(state);

        let parent = Weak::new(&*self.parent);
        let url = self.url.clone();
        let port = self.port;
        let presentation = Rc::downgrade(&self.state);
        let deadline = Instant::now() + MEASUREMENT_TIMEOUT;
        measure(&self.parent, move |geometry| {
            let Some(presentation) = presentation.upgrade() else {
                return;
            };
            let mut state = presentation.borrow_mut();
            state.measurement_pending = false;
            let parent = parent.load();
            if parent
                .as_deref()
                .and_then(|parent| eligible_owner(parent, port))
                .as_ref()
                != Some(&url)
            {
                state.display = None;
                return;
            }
            let geometry = geometry.filter(|_| Instant::now() < deadline);
            match state.display.as_ref() {
                Some(Display::Waiting(listing)) => {
                    if let (Some(geometry), Some(mtm), Some(parent)) =
                        (geometry, MainThreadMarker::new(), parent)
                    {
                        if let Ok(slot) = SetupReviewSlot::mount(mtm, &parent, geometry, listing) {
                            state.display = Some(Display::Mounted(slot));
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

fn eligible_owner(parent: &WKWebView, port: u16) -> Option<String> {
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
        && parsed.path() == "/panel/setup")
        .then_some(url)
}

/// Sample the fixed Setup marker. The callback runs on the AppKit thread.
/// The owner must discard late results after a route or candidate change.
#[allow(dead_code)]
pub(crate) fn measure(parent: &WKWebView, apply: impl FnOnce(Option<ReviewGeometry>) + 'static) {
    // Keep native AppKit and CSS coordinates at the same scale.
    if unsafe { parent.pageZoom() != 1.0 || parent.magnification() != 1.0 } {
        apply(None);
        return;
    }
    let pending = RefCell::new(Some(apply));
    let callback = RcBlock::new(move |value: *mut AnyObject, error: *mut NSError| {
        let Some(apply) = pending.borrow_mut().take() else {
            return;
        };
        let geometry = if error.is_null() {
            // WebKit owns the result until the callback returns.
            unsafe { value.as_ref() }
                .and_then(AnyObject::downcast_ref::<NSString>)
                .filter(|value| value.length() <= 1024)
                .and_then(|value| serde_json::from_str(&value.to_string()).ok())
        } else {
            None
        };
        apply(geometry);
    });
    unsafe {
        parent.evaluateJavaScript_completionHandler(&NSString::from_str(MEASURE), Some(&callback))
    };
}

#[derive(Clone, Copy, Debug, Deserialize)]
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
#[derive(Clone, Copy, Debug, Deserialize)]
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
    clip: Retained<NSView>,
    full: Retained<NSView>,
}

#[allow(dead_code)]
impl SetupReviewSlot {
    /// The caller must supply the actual parent page view, on its UI thread.
    pub(crate) fn mount(
        mtm: MainThreadMarker,
        parent: &NSView,
        geometry: ReviewGeometry,
        listing: &str,
    ) -> Result<Self, &'static str> {
        let owner = parent
            .superview()
            .ok_or("Setup owner view is unavailable")?;
        if parent.isHiddenOrHasHiddenAncestor() {
            return Err("Setup owner view is hidden");
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
        let review = SetupReviewView::mount(mtm, &full_view, listing)?;
        owner.addSubview_positioned_relativeTo(
            &clip_view,
            NSWindowOrderingMode::Above,
            Some(parent),
        );
        full_view.setFrame(clip_view.convertRect_fromView(full, Some(parent)));
        Ok(Self {
            _review: review,
            parent: parent.retain(),
            clip: clip_view,
            full: full_view,
        })
    }

    /// Hide while outside the viewport or while the owner is changing layout.
    pub(crate) fn update(&self, geometry: Option<ReviewGeometry>) -> bool {
        let frames = geometry.and_then(|geometry| {
            let parent = &*self.parent;
            let owner = self.clip.superview()?;
            if parent.superview().as_deref() != Some(&*owner)
                || parent.isHiddenOrHasHiddenAncestor()
            {
                return None;
            }
            let (full, clip) = geometry.frames(parent.bounds(), parent.isFlipped())?;
            Some((full, owner.convertRect_fromView(clip, Some(parent))))
        });
        let Some((full, clip)) = frames else {
            self.clip.setHidden(true);
            return false;
        };
        self.clip.setFrame(clip);
        self.full
            .setFrame(self.clip.convertRect_fromView(full, Some(&self.parent)));
        self.clip.setHidden(false);
        true
    }
}

impl Drop for SetupReviewSlot {
    fn drop(&mut self) {
        self.clip.removeFromSuperview();
    }
}
