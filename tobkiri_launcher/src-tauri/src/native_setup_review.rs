//! AppKit-owned view for the existing Setup review content.
//!
//! WebKit receives only the embedded document and a bounded listing. The view
//! has no Tauri label, scheme handler, or message controller registration.

#[path = "native_setup_review_owner.rs"]
pub(crate) mod owner;
#[path = "native_setup_review_slot.rs"]
pub(crate) mod slot;

use base64::{engine::general_purpose::STANDARD as BASE64, Engine as _};
use block2::DynBlock;
use objc2::{
    define_class, msg_send, rc::Retained, runtime::NSObjectProtocol, runtime::ProtocolObject,
    DefinedClass, MainThreadMarker, MainThreadOnly,
};
use objc2_app_kit::NSView;
use objc2_foundation::{NSObject, NSRect, NSString};
use objc2_web_kit::{
    WKNavigationAction, WKNavigationActionPolicy, WKNavigationDelegate, WKNavigationType,
    WKWebView, WKWebViewConfiguration, WKWebsiteDataStore,
};
use std::cell::Cell;

const DOCUMENT: &str = include_str!("../../setup-review-ui/native.html");
const LISTING_SLOT: &str = "<template id=\"setup-review-listing\">";
const MAX_LISTING_BYTES: usize = 4 * 1024 * 1024;

pub(crate) fn listing_confirmation(listing: &str) -> Option<&str> {
    use serde::Deserialize;

    #[derive(Deserialize)]
    struct Listing<'a> {
        state: &'a str,
        #[serde(borrow)]
        recommended_default_profile: Profile<'a>,
    }
    #[derive(Deserialize)]
    struct Profile<'a> {
        #[serde(borrow)]
        confirmation: Confirmation<'a>,
    }
    #[derive(Deserialize)]
    struct Confirmation<'a> {
        confirmation_digest: &'a str,
    }

    if listing.is_empty() || listing.len() > MAX_LISTING_BYTES {
        return None;
    }
    let listing: Listing<'_> = serde_json::from_str(listing).ok()?;
    (listing.state == "review_required").then_some(
        listing
            .recommended_default_profile
            .confirmation
            .confirmation_digest,
    )
}

fn review_document(listing: &str) -> Result<String, &'static str> {
    if listing.is_empty() || listing.len() > MAX_LISTING_BYTES {
        return Err("Setup review listing exceeds its display limit");
    }
    let (before, after) = DOCUMENT
        .split_once(LISTING_SLOT)
        .ok_or("Setup review document is missing its listing slot")?;
    if !after.starts_with("</template>") || after.contains(LISTING_SLOT) {
        return Err("Setup review document has an invalid listing slot");
    }
    let mut html = String::with_capacity(DOCUMENT.len() + listing.len().div_ceil(3) * 4);
    html.push_str(before);
    html.push_str(LISTING_SLOT);
    BASE64.encode_string(listing.as_bytes(), &mut html);
    html.push_str(after);
    Ok(html)
}

define_class!(
    // NSObject permits subclassing. This delegate and its state live on the UI thread.
    #[unsafe(super(NSObject))]
    #[thread_kind = MainThreadOnly]
    #[ivars = Cell<bool>]
    struct ReviewNavigation;

    unsafe impl NSObjectProtocol for ReviewNavigation {}

    unsafe impl WKNavigationDelegate for ReviewNavigation {
        #[unsafe(method(webView:decidePolicyForNavigationAction:decisionHandler:))]
        fn decide(
            &self,
            _view: &WKWebView,
            action: &WKNavigationAction,
            decision: &DynBlock<dyn Fn(WKNavigationActionPolicy)>,
        ) {
            // Only the initial loadHTMLString navigation is admitted. Reject
            // links, new frames, reloads and every other destination.
            let allow = unsafe {
                action.navigationType() == WKNavigationType::Other
                    && action.targetFrame().is_some_and(|frame| frame.isMainFrame())
                    && action.request().URL().is_some_and(|url| {
                        url.absoluteString().is_some_and(|url| url.to_string() == "about:blank")
                    })
                    && self.ivars().replace(false)
            };
            decision.call((if allow {
                WKNavigationActionPolicy::Allow
            } else {
                WKNavigationActionPolicy::Cancel
            },));
        }
    }
);

/// Owns the WebKit view and its weak navigation delegate on the AppKit thread.
#[allow(dead_code)] // The Setup route still owns its existing activation path.
pub(crate) struct SetupReviewView {
    webview: Retained<WKWebView>,
    _navigation: Retained<ReviewNavigation>,
}

#[allow(dead_code)]
impl SetupReviewView {
    /// Mount inside a native-owned slot. The owner supplies the listing directly.
    pub(crate) fn mount(
        mtm: MainThreadMarker,
        slot: &NSView,
        listing: &str,
    ) -> Result<Self, &'static str> {
        let html = NSString::from_str(&review_document(listing)?);
        let bounds = slot.bounds();
        if !valid_bounds(bounds) {
            return Err("Setup review slot is unavailable");
        }

        // A new configuration gives the view its own, empty content
        // controller. There is no base URL or access to bundle/file URLs.
        let (webview, navigation) = unsafe {
            let configuration = WKWebViewConfiguration::new(mtm);
            configuration.setWebsiteDataStore(&WKWebsiteDataStore::nonPersistentDataStore(mtm));
            let webview = WKWebView::initWithFrame_configuration(
                WKWebView::alloc(mtm),
                bounds,
                &configuration,
            );
            let this = ReviewNavigation::alloc(mtm).set_ivars(Cell::new(true));
            let navigation: Retained<ReviewNavigation> = msg_send![super(this), init];
            webview.setNavigationDelegate(Some(ProtocolObject::from_ref(&*navigation)));
            webview.setAllowsBackForwardNavigationGestures(false);
            webview.setAllowsLinkPreview(false);
            webview
                .loadHTMLString_baseURL(&html, None)
                .ok_or("Setup review document could not be loaded")?;
            (webview, navigation)
        };
        use objc2_app_kit::NSAutoresizingMaskOptions as Resize;
        webview.setAutoresizingMask(Resize::ViewWidthSizable | Resize::ViewHeightSizable);
        slot.addSubview(&webview);
        Ok(Self {
            webview,
            _navigation: navigation,
        })
    }

    #[cfg(test)]
    pub(crate) fn webview(&self) -> &WKWebView {
        &self.webview
    }
}

impl Drop for SetupReviewView {
    fn drop(&mut self) {
        // The view remains main-thread bound, including during destruction.
        unsafe { self.webview.stopLoading() };
        self.webview.removeFromSuperview();
    }
}

fn valid_bounds(bounds: NSRect) -> bool {
    [
        bounds.origin.x,
        bounds.origin.y,
        bounds.size.width,
        bounds.size.height,
    ]
    .into_iter()
    .all(f64::is_finite)
        && bounds.size.width > 0.0
        && bounds.size.height > 0.0
}
