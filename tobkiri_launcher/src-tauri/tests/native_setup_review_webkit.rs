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
        SetupReviewListing, SetupReviewView,
    };
    use objc2::{rc::autoreleasepool, runtime::AnyObject, MainThreadMarker, MainThreadOnly};
    use objc2_app_kit::{
        NSApplication, NSBackingStoreType, NSEvent, NSEventModifierFlags, NSEventType, NSView,
        NSWindow, NSWindowStyleMask,
    };
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

    fn press_space(window: &NSWindow, view: &WKWebView, selector: &str, deadline: Instant) {
        assert!(window.makeFirstResponder(Some(view)));
        let selector = serde_json::to_string(selector).unwrap();
        let script = format!(
            "(() => {{
                const el = document.querySelector({selector});
                el.focus();
                return String(document.activeElement === el);
            }})()"
        );
        assert!(evaluate_boolean(view, &script, deadline));
        let space = NSString::from_str(" ");
        let key = |event_type| {
            NSEvent::keyEventWithType_location_modifierFlags_timestamp_windowNumber_context_characters_charactersIgnoringModifiers_isARepeat_keyCode(
                event_type, NSPoint::new(0.0, 0.0), NSEventModifierFlags::empty(), 0.0,
                window.windowNumber(), None, &space, &space, false, 49,
            ).expect("AppKit space key event")
        };
        view.keyDown(&key(NSEventType::KeyDown));
        pump();
        view.keyUp(&key(NSEventType::KeyUp));
        pump();
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

        let accepted = Rc::new(Cell::new(0));
        let observed = accepted.clone();
        let expected = fixture["recommended_default_profile"]["confirmation"]
            ["confirmation_digest"]
            .as_str()
            .unwrap()
            .to_owned();
        let interactive =
            SetupReviewView::mount_with_confirmation(mtm, &slot, listing, move |result| {
                assert_eq!(result.confirmation_digest(), expected);
                observed.set(observed.get() + 1);
            })
            .unwrap();
        let webview = interactive.webview();
        window.makeKeyAndOrderFront(None);
        window.orderFrontRegardless();
        let deadline = Instant::now() + Duration::from_secs(20);
        while !evaluate_boolean(
            webview,
            "String(document.querySelector('input[data-native-setup-reviewed]')?.disabled === false &&
             document.querySelector('button[data-native-setup-confirm]')?.disabled === true &&
             !window.webkit?.messageHandlers?.tobkiriSetupConfirmation)",
            deadline,
        ) {
            assert!(Instant::now() < deadline, "interactive review did not render");
            pump();
        }
        assert!(evaluate_boolean(
            webview,
            "document.querySelector('input[data-native-setup-reviewed]').click();
             document.querySelector('button[data-native-setup-confirm]').click(); String(true)",
            deadline,
        ));
        assert_eq!(accepted.get(), 0);
        assert!(evaluate_boolean(
            webview,
            "if (document.querySelector('input[data-native-setup-reviewed]').checked)
               document.querySelector('input[data-native-setup-reviewed]').click(); String(true)",
            deadline,
        ));
        press_space(
            &window,
            webview,
            "input[data-native-setup-reviewed]",
            deadline,
        );
        while !evaluate_boolean(
            webview,
            "String(document.querySelector('button[data-native-setup-confirm]')?.disabled === false)",
            deadline,
        ) {
            assert!(Instant::now() < deadline, "AppKit review click was not delivered");
            pump();
        }
        press_space(
            &window,
            webview,
            "button[data-native-setup-confirm]",
            deadline,
        );
        while accepted.get() == 0 {
            assert!(
                Instant::now() < deadline,
                "AppKit confirmation click was not delivered"
            );
            pump();
        }
        assert_eq!(accepted.get(), 1);
        drop(interactive);
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
            "<!doctype html><style>body{{margin:0}}section{{width:700px;height:900px}}</style>
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
            "(() => {
                const marker = document.querySelector('section span');
                marker.replaceWith(marker.cloneNode());
                return String(true);
             })()",
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
        let (confirm, mut confirmed) = tokio::sync::oneshot::channel();
        let presentation = SetupReviewPresentation::with_confirmation(
            &parent,
            &slot,
            80,
            replacement,
            SetupReviewListing::new(listing.to_owned()),
            Some(confirm),
        )
        .unwrap();
        presentation.refresh();
        presentation.refresh();
        while slot.subviews().len() == 2 {
            assert!(Instant::now() < deadline, "Parent review was not mounted");
            pump();
        }
        assert_eq!(slot.subviews().len(), 3);
        let child = presentation.review_webview().unwrap();
        let deadline = Instant::now() + Duration::from_secs(20);
        while !evaluate_boolean(
            &child,
            "String(!!document.querySelector('input[data-native-setup-reviewed]'))",
            deadline,
        ) {
            assert!(
                Instant::now() < deadline,
                "Parent review controls did not render"
            );
            pump();
        }
        press_space(
            &window,
            &child,
            "input[data-native-setup-reviewed]",
            deadline,
        );
        assert!(evaluate_boolean(
            &child,
            "String(document.querySelector('button[data-native-setup-confirm]')?.disabled === false)",
            deadline,
        ));
        // Replace the parent candidate without refreshing the overlay first.
        // The real gesture in the old child must re-sample the parent itself.
        assert!(evaluate_boolean(
            &parent,
            "(() => {
                const marker = document.querySelector('section span');
                marker.replaceWith(marker.cloneNode());
                return String(true);
             })()",
            deadline,
        ));
        press_space(
            &window,
            &child,
            "button[data-native-setup-confirm]",
            deadline,
        );
        loop {
            match confirmed.try_recv() {
                Ok(_) => panic!("replaced Setup candidate admitted a gesture"),
                Err(tokio::sync::oneshot::error::TryRecvError::Closed) => break,
                Err(tokio::sync::oneshot::error::TryRecvError::Empty) => {
                    assert!(Instant::now() < deadline, "stale gesture was not retired");
                    pump();
                }
            }
        }
        window.orderOut(None);
        presentation.refresh();
        assert_eq!(slot.subviews().len(), 2);
        println!("Actual WebKit rendered the pinned review, blocked navigation, clipped scrolling and detached");
    });
}

#[cfg(not(target_os = "macos"))]
fn main() {}
