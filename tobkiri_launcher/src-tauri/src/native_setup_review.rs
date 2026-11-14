//! AppKit-owned view for the existing Setup review content.
//!
//! WebKit receives only the embedded document and a bounded listing. Native
//! confirmation listens in an isolated world with a single gesture message.

#[path = "native_setup_review_owner.rs"]
pub(crate) mod owner;
#[path = "native_setup_review_slot.rs"]
pub(crate) mod slot;

use base64::{engine::general_purpose::STANDARD as BASE64, Engine as _};
use block2::DynBlock;
use objc2::{
    define_class, msg_send,
    rc::{Retained, Weak},
    runtime::{AnyObject, NSObjectProtocol, ProtocolObject},
    DefinedClass, MainThreadMarker, MainThreadOnly,
};
use objc2_app_kit::NSView;
use objc2_foundation::{NSObject, NSRect, NSString};
use objc2_web_kit::{
    WKContentWorld, WKNavigationAction, WKNavigationActionPolicy, WKNavigationDelegate,
    WKNavigationType, WKScriptMessage, WKScriptMessageHandler, WKUserContentController,
    WKUserScript, WKUserScriptInjectionTime, WKWebView, WKWebViewConfiguration, WKWebsiteDataStore,
};
use std::cell::{Cell, RefCell};

const DOCUMENT: &str = include_str!("../../setup-review-ui/native.html");
const LISTING_SLOT: &str = "<template id=\"setup-review-listing\">";
const INPUT_SLOT: &str = "<template id=\"setup-review-native-input\"></template>";
const CONFIRM_MESSAGE: &str = "tobkiriSetupConfirmation";
const CONFIRM_SCRIPT: &str = include_str!("../../frontend/src/lib/nativeSetupConfirmation.js");
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

fn review_document(listing: &str, interactive: bool) -> Result<String, &'static str> {
    if listing.is_empty() || listing.len() > MAX_LISTING_BYTES {
        return Err("Setup review listing exceeds its display limit");
    }
    let (before, after) = DOCUMENT
        .split_once(LISTING_SLOT)
        .ok_or("Setup review document is missing its listing slot")?;
    let after = after
        .strip_prefix("</template>")
        .ok_or("Setup review document has an invalid listing slot")?;
    if after.contains(LISTING_SLOT) {
        return Err("Setup review document has an invalid listing slot");
    }
    let mut html = String::with_capacity(DOCUMENT.len() + listing.len().div_ceil(3) * 4);
    html.push_str(before);
    html.push_str(LISTING_SLOT);
    BASE64.encode_string(listing.as_bytes(), &mut html);
    html.push_str("</template>");
    if interactive {
        html.push_str(INPUT_SLOT);
    }
    html.push_str(after);
    Ok(html)
}

/// A UI observation for the exact native listing, consumed once by Rust.
/// The activation transaction must still validate the candidate under its locks.
pub(crate) struct SetupReviewConfirmation {
    confirmation_digest: String,
}

impl SetupReviewConfirmation {
    pub(crate) fn confirmation_digest(&self) -> &str {
        &self.confirmation_digest
    }
}

struct ConfirmationListener {
    view: RefCell<Option<Weak<WKWebView>>>,
    world: Retained<WKContentWorld>,
    digest: String,
    armed: RefCell<Option<ConfirmationAction>>,
}

type ConfirmationAction = Box<dyn FnOnce(SetupReviewConfirmation)>;

define_class!(
    #[unsafe(super(NSObject))]
    #[thread_kind = MainThreadOnly]
    #[ivars = ConfirmationListener]
    struct ReviewConfirmation;

    unsafe impl NSObjectProtocol for ReviewConfirmation {}

    unsafe impl WKScriptMessageHandler for ReviewConfirmation {
        #[unsafe(method(userContentController:didReceiveScriptMessage:))]
        fn received(&self, _controller: &WKUserContentController, message: &WKScriptMessage) {
            let state = self.ivars();
            let Some(view) = state.view.borrow().as_ref().and_then(Weak::load) else {
                return;
            };
            let eligible = unsafe {
                message.frameInfo().isMainFrame()
                    && std::ptr::eq(&*message.world(), &*state.world)
                    && message
                        .webView()
                        .is_some_and(|sender| std::ptr::eq(&*sender, &*view))
                    && view.URL().is_some_and(|url| {
                        url.absoluteString()
                            .is_some_and(|text| text.to_string() == "about:blank")
                    })
            };
            if !eligible
                || view.isHiddenOrHasHiddenAncestor()
                || !view.window().is_some_and(|window| {
                    window.isVisible()
                        && !window.isMiniaturized()
                        && window.attachedSheet().is_none()
                })
            {
                return;
            }
            let body = unsafe { message.body() };
            if !AnyObject::downcast_ref::<NSString>(&body)
                .is_some_and(|body| body.length() == 7 && body.to_string() == "confirm")
            {
                return;
            }
            let action = state.armed.borrow_mut().take();
            if let Some(action) = action {
                action(SetupReviewConfirmation {
                    confirmation_digest: state.digest.clone(),
                });
            }
        }
    }
);

struct ConfirmationRegistration {
    handler: Retained<ReviewConfirmation>,
    controller: Retained<WKUserContentController>,
}

impl ConfirmationRegistration {
    fn install(
        mtm: MainThreadMarker,
        controller: Retained<WKUserContentController>,
        digest: &str,
        action: ConfirmationAction,
    ) -> Self {
        unsafe {
            let world = WKContentWorld::worldWithName(&NSString::from_str(CONFIRM_MESSAGE), mtm);
            let script = WKUserScript::initWithSource_injectionTime_forMainFrameOnly_inContentWorld(
                WKUserScript::alloc(mtm),
                &NSString::from_str(CONFIRM_SCRIPT),
                WKUserScriptInjectionTime::AtDocumentStart,
                true,
                &world,
            );
            let this = ReviewConfirmation::alloc(mtm).set_ivars(ConfirmationListener {
                view: RefCell::new(None),
                world,
                digest: digest.to_owned(),
                armed: RefCell::new(Some(action)),
            });
            let handler: Retained<ReviewConfirmation> = msg_send![super(this), init];
            controller.addScriptMessageHandler_contentWorld_name(
                ProtocolObject::from_ref(&*handler),
                &handler.ivars().world,
                &NSString::from_str(CONFIRM_MESSAGE),
            );
            controller.addUserScript(&script);
            Self {
                handler,
                controller,
            }
        }
    }
}

impl Drop for ConfirmationRegistration {
    fn drop(&mut self) {
        self.handler.ivars().armed.borrow_mut().take();
        unsafe {
            self.controller
                .removeScriptMessageHandlerForName_contentWorld(
                    &NSString::from_str(CONFIRM_MESSAGE),
                    &self.handler.ivars().world,
                );
        }
    }
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
    confirmation: Option<ConfirmationRegistration>,
}

#[allow(dead_code)]
impl SetupReviewView {
    /// Mount inside a native-owned slot. The owner supplies the listing directly.
    pub(crate) fn mount(
        mtm: MainThreadMarker,
        slot: &NSView,
        listing: &str,
    ) -> Result<Self, &'static str> {
        Self::mount_review(mtm, slot, listing, None)
    }

    /// Only a host-owned review can opt in to the single confirmation gesture.
    pub(crate) fn mount_with_confirmation(
        mtm: MainThreadMarker,
        slot: &NSView,
        listing: &str,
        action: impl FnOnce(SetupReviewConfirmation) + 'static,
    ) -> Result<Self, &'static str> {
        let digest = listing_confirmation(listing).ok_or("Setup listing is not confirmable")?;
        Self::mount_review(mtm, slot, listing, Some((digest, Box::new(action))))
    }

    fn mount_review(
        mtm: MainThreadMarker,
        slot: &NSView,
        listing: &str,
        action: Option<(&str, ConfirmationAction)>,
    ) -> Result<Self, &'static str> {
        let html = NSString::from_str(&review_document(listing, action.is_some())?);
        let bounds = slot.bounds();
        if !valid_bounds(bounds) {
            return Err("Setup review slot is unavailable");
        }

        // A new configuration owns the controller and nonpersistent storage.
        // There is no base URL or access to bundle/file URLs.
        let (webview, navigation, confirmation) = unsafe {
            let configuration = WKWebViewConfiguration::new(mtm);
            configuration.setWebsiteDataStore(&WKWebsiteDataStore::nonPersistentDataStore(mtm));
            let confirmation = action.map(|(digest, action)| {
                ConfirmationRegistration::install(
                    mtm,
                    configuration.userContentController(),
                    digest,
                    action,
                )
            });
            let webview = WKWebView::initWithFrame_configuration(
                WKWebView::alloc(mtm),
                bounds,
                &configuration,
            );
            if let Some(registration) = &confirmation {
                *registration.handler.ivars().view.borrow_mut() = Some(Weak::new(&*webview));
            }
            let this = ReviewNavigation::alloc(mtm).set_ivars(Cell::new(true));
            let navigation: Retained<ReviewNavigation> = msg_send![super(this), init];
            webview.setNavigationDelegate(Some(ProtocolObject::from_ref(&*navigation)));
            webview.setAllowsBackForwardNavigationGestures(false);
            webview.setAllowsLinkPreview(false);
            webview
                .loadHTMLString_baseURL(&html, None)
                .ok_or("Setup review document could not be loaded")?;
            (webview, navigation, confirmation)
        };
        use objc2_app_kit::NSAutoresizingMaskOptions as Resize;
        webview.setAutoresizingMask(Resize::ViewWidthSizable | Resize::ViewHeightSizable);
        slot.addSubview(&webview);
        Ok(Self {
            webview,
            _navigation: navigation,
            confirmation,
        })
    }

    #[cfg(test)]
    pub(crate) fn webview(&self) -> &WKWebView {
        &self.webview
    }
}

impl Drop for SetupReviewView {
    fn drop(&mut self) {
        drop(self.confirmation.take());
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
