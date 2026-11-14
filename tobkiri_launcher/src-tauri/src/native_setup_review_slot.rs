//! Place the review beside the parent WebKit view, in AppKit points.

use super::{valid_bounds, SetupReviewView};
use objc2::{rc::Retained, MainThreadMarker, MainThreadOnly, Message};
use objc2_app_kit::{NSView, NSWindowOrderingMode};
use objc2_foundation::{NSPoint, NSRect, NSSize};
use serde::Deserialize;

const MAX_DIMENSION: f64 = 16_384.0;
const VIEWPORT_TOLERANCE: f64 = 1.0;

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
