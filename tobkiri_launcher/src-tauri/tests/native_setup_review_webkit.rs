//! A main-thread executable because the Rust test harness runs on workers.

#[cfg(target_os = "macos")]
#[path = "../src/native_setup_review.rs"]
mod native_setup_review;

#[cfg(target_os = "macos")]
fn main() {
    use block2::RcBlock;
    use native_setup_review::{
        listing_confirmation,
        slot::{measure, ReviewSample, SetupReviewPresentation, SetupReviewSlot},
        SetupReviewView,
    };
    use objc2::{rc::autoreleasepool, runtime::AnyObject, MainThreadMarker, MainThreadOnly};
    use objc2_app_kit::{NSApplication, NSBackingStoreType, NSView, NSWindow, NSWindowStyleMask};
    use objc2_foundation::{NSDate, NSError, NSPoint, NSRect, NSRunLoop, NSSize, NSString, NSURL};
    use objc2_web_kit::{WKWebView, WKWebViewConfiguration};
    use std::cell::Cell;
    use std::rc::Rc;
    use std::time::{Duration, Instant};

    fn pump() {
        NSRunLoop::currentRunLoop().runUntilDate(&NSDate::dateWithTimeIntervalSinceNow(0.025));
    }

    fn evaluate_boolean(view: &WKWebView, script: &str, deadline: Instant) -> bool {
        let result = Rc::new(Cell::new(None));
        let observed = result.clone();
        let callback = RcBlock::new(move |value: *mut AnyObject, error: *mut NSError| {
            // WebKit keeps callback arguments valid for the duration of this call.
            let value = unsafe { value.as_ref() }.and_then(AnyObject::downcast_ref::<NSString>);
            observed.set(Some(
                error.is_null() && value.is_some_and(|value| value.to_string() == "true"),
            ));
        });
        unsafe {
            view.evaluateJavaScript_completionHandler(&NSString::from_str(script), Some(&callback))
        };
        loop {
            if let Some(value) = result.get() {
                return value;
            }
            assert!(
                Instant::now() < deadline,
                "WebKit did not evaluate the document"
            );
            pump();
        }
    }

    fn sample(view: &WKWebView, deadline: Instant) -> ReviewSample {
        let observed = Rc::new(Cell::new(None));
        let result = observed.clone();
        measure(view, move |value| result.set(Some(value)));
        loop {
            if let Some(result) = observed.take() {
                return result.expect("WebKit did not identify the Setup anchor");
            }
            assert!(Instant::now() < deadline, "WebKit sample did not complete");
            pump();
        }
    }

    autoreleasepool(|_| {
        let mtm = MainThreadMarker::new().expect("WebKit smoke requires the main thread");
        let _application = NSApplication::sharedApplication(mtm);
        let bounds = NSRect::new(NSPoint::new(0.0, 0.0), NSSize::new(800.0, 600.0));
        let window = unsafe {
            let window = NSWindow::initWithContentRect_styleMask_backing_defer(
                NSWindow::alloc(mtm),
                bounds,
                NSWindowStyleMask::Titled | NSWindowStyleMask::Resizable,
                NSBackingStoreType::Buffered,
                false,
            );
            window.setReleasedWhenClosed(false);
            window
        };
        let slot = window.contentView().unwrap();
        let listing = include_str!(
            "../../../tobkiri_runtime/tobkiri_protocol/fixtures/defaults_setup_v4.canonical.json"
        );
        assert!(SetupReviewView::mount(mtm, &slot, "").is_err());
        assert!(SetupReviewView::mount(mtm, &slot, &" ".repeat(4 * 1024 * 1024 + 1)).is_err());
        let view = SetupReviewView::mount(mtm, &slot, listing).unwrap();
        assert_eq!(slot.subviews().len(), 1);
        let webview = view.webview();
        assert!(!unsafe { webview.configuration().websiteDataStore().isPersistent() });
        let fixture: serde_json::Value = serde_json::from_str(listing).unwrap();
        let digest = fixture["recommended_default_profile"]["confirmation"]["confirmation_digest"]
            .as_str()
            .unwrap();
        assert_eq!(listing_confirmation(listing), Some(digest));
        let stale_state = listing.replace("\"review_required\"", "\"active\"");
        assert!(listing_confirmation(&stale_state).is_none());
        assert!(listing_confirmation(r#"{"state":"review_required"}"#).is_none());
        let digest = serde_json::to_string(digest).unwrap();
        let render = format!(
            "String(document.body?.textContent.includes({digest}) &&
             document.querySelector('input[type=checkbox]')?.disabled &&
             document.querySelector('button:disabled') &&
             !document.getElementById('setup-review-listing') &&
             typeof window.__TAURI_INTERNALS__ === 'undefined' &&
             typeof window.__TAURI__ === 'undefined' &&
             !window.webkit?.messageHandlers?.ipc)"
        );
        let deadline = Instant::now() + Duration::from_secs(20);
        while !evaluate_boolean(webview, &render, deadline) {
            assert!(
                Instant::now() < deadline,
                "Setup confirmation did not render"
            );
            pump();
        }
        assert!(evaluate_boolean(
            webview,
            "String(getComputedStyle(document.body).marginTop === '0px' &&
             getComputedStyle(document.querySelector('section')).paddingTop === '28px')",
            deadline,
        ));
        assert!(evaluate_boolean(
            webview,
            "(() => { const script = document.createElement('script'); script.textContent =
             'document.body.dataset.cspProbe = \"ran\"'; document.body.append(script);
             return String(document.body.dataset.cspProbe === undefined); })()",
            deadline,
        ));
        window.setContentSize(NSSize::new(1000.0, 720.0));
        assert_eq!(webview.frame().size, slot.bounds().size);
        let _ = evaluate_boolean(
            webview,
            "location.assign('about:blank?closed-navigation'); String(true)",
            deadline,
        );
        for _ in 0..10 {
            pump();
        }
        assert!(evaluate_boolean(webview, &render, deadline));
        drop(view);
        assert_eq!(slot.subviews().len(), 0);

        // Simulate the real parent view's inset within its native container.
        let parent = NSView::initWithFrame(
            NSView::alloc(mtm),
            NSRect::new(NSPoint::new(16.0, 24.0), NSSize::new(800.0, 600.0)),
        );
        slot.addSubview(&parent);
        let mut geometry = serde_json::json!({
            "full": {"left": 30, "top": -100, "right": 730, "bottom": 800},
            "clip": {"left": 30, "top": 0, "right": 730, "bottom": 600},
            "viewportWidth": 800, "viewportHeight": 600,
        });
        let placed = SetupReviewSlot::mount(
            mtm,
            &parent,
            &slot,
            serde_json::from_value(geometry.clone()).unwrap(),
            listing,
        )
        .unwrap();
        let clip = slot.subviews().objectAtIndex(1);
        let full = clip.subviews().objectAtIndex(0);
        assert!(clip.clipsToBounds());
        assert_eq!(clip.frame().origin, NSPoint::new(46.0, 24.0));
        assert_eq!(clip.frame().size, NSSize::new(700.0, 600.0));
        assert_eq!(full.frame().origin, NSPoint::new(0.0, -200.0));
        assert_eq!(full.frame().size, NSSize::new(700.0, 900.0));
        assert_eq!(
            full.subviews().objectAtIndex(0).frame().size,
            full.bounds().size
        );

        geometry["full"]["top"] = (-200).into();
        geometry["full"]["bottom"] = 700.into();
        assert!(placed.update(Some(serde_json::from_value(geometry.clone()).unwrap())));
        assert_eq!(full.frame().origin, NSPoint::new(0.0, -100.0));
        assert_eq!(full.frame().size, NSSize::new(700.0, 900.0));
        geometry["viewportHeight"] = 601.5.into();
        assert!(!placed.update(Some(serde_json::from_value(geometry.clone()).unwrap())));
        assert!(clip.isHidden());
        geometry["viewportHeight"] = 600.into();
        geometry["clip"]["bottom"] = 601.into();
        assert!(!placed.update(Some(serde_json::from_value(geometry.clone()).unwrap())));
        geometry["clip"]["bottom"] = 600.into();
        geometry["listing"] = "renderer-controlled data".into();
        assert!(
            serde_json::from_value::<native_setup_review::slot::ReviewGeometry>(geometry).is_err()
        );
        assert!(!placed.update(None));
        drop(placed);
        assert_eq!(slot.subviews().len(), 1);

        // Exercise the same geometry script against actual WebKit on a parent
        // using the Setup pathname. This in-memory document has no resources.
        let parent = unsafe {
            WKWebView::initWithFrame_configuration(
                WKWebView::alloc(mtm),
                parent.frame(),
                &WKWebViewConfiguration::new(mtm),
            )
        };
        slot.addSubview(&parent);
        window.orderFrontRegardless();
        let base =
            NSURL::URLWithString(&NSString::from_str("http://127.0.0.1/panel/setup")).unwrap();
        let body = NSString::from_str(&format!(
            "<!doctype html><style>body{margin:0}section{width:700px;height:900px}</style>
             <section><span hidden data-native-setup-review-anchor={digest}></span></section>",
        ));
        unsafe { parent.loadHTMLString_baseURL(&body, Some(&base)) }.unwrap();
        let deadline = Instant::now() + Duration::from_secs(20);
        while !evaluate_boolean(
            &parent,
            "String(document.visibilityState === 'visible' && !!document.querySelector('section'))",
            deadline,
        ) {
            assert!(
                Instant::now() < deadline,
                "Parent Setup fixture did not render"
            );
            pump();
        }
        let initial = sample(&parent, deadline);
        let anchor = initial.anchor();
        assert!(evaluate_boolean(
            &parent,
            "String(typeof globalThis.tobkiriSetupReviewAnchor === 'undefined')",
            deadline,
        ));
        assert_eq!(sample(&parent, deadline).anchor(), anchor);
        let geometry = initial.geometry().expect("visible Setup geometry");
        let placed = SetupReviewSlot::mount(mtm, &parent, &slot, geometry, listing).unwrap();
        let clip = slot.subviews().objectAtIndex(2);
        assert_eq!(clip.frame().size, NSSize::new(700.0, 600.0));
        assert_eq!(
            clip.subviews().objectAtIndex(0).frame().size,
            NSSize::new(700.0, 900.0)
        );
        drop(placed);

        assert!(SetupReviewPresentation::new(
            &parent,
            &slot,
            8765,
            anchor.clone(),
            listing.to_owned()
        )
        .is_err());
        let cancelled =
            SetupReviewPresentation::new(&parent, &slot, 80, anchor.clone(), listing.to_owned())
                .unwrap();
        cancelled.refresh();
        cancelled.retire();
        for _ in 0..5 {
            pump();
        }
        assert_eq!(slot.subviews().len(), 2);
        let stale =
            SetupReviewPresentation::new(&parent, &slot, 80, anchor.clone(), listing.to_owned())
                .unwrap();
        assert!(evaluate_boolean(
            &parent,
            "const marker = document.querySelector('section span');
             marker.replaceWith(marker.cloneNode()); String(true)",
            deadline,
        ));
        let replacement = sample(&parent, deadline).anchor();
        assert_eq!(replacement.confirmation(), anchor.confirmation());
        assert_ne!(replacement, anchor);
        stale.refresh();
        for _ in 0..5 {
            pump();
        }
        assert_eq!(slot.subviews().len(), 2);
        let presentation =
            SetupReviewPresentation::new(&parent, &slot, 80, replacement, listing.to_owned())
                .unwrap();
        presentation.refresh();
        presentation.refresh();
        while slot.subviews().len() == 2 {
            assert!(Instant::now() < deadline, "Parent review was not mounted");
            pump();
        }
        assert_eq!(slot.subviews().len(), 3);
        window.orderOut(None);
        presentation.refresh();
        assert_eq!(slot.subviews().len(), 2);
        println!("Actual WebKit rendered the pinned review, blocked navigation, clipped scrolling and detached");
    });
}

#[cfg(not(target_os = "macos"))]
fn main() {}
