//! Read-only exact-plan review with reachable, separately owned Native buttons.

use serde_json::Value;

const REVIEW_SECONDS: u64 = 240; // Leave transport time inside the Host's 300s TTL.

fn full_detail(plan: &Value, nonce: &str) -> Result<String, String> {
    let detail = serde_json::to_string_pretty(plan).map_err(|_| "Native plan is invalid")?;
    if detail.len() > 512 * 1024 {
        return Err("Native plan exceeds its display limit".into());
    }
    Ok(format!("Ceremony nonce: {nonce}\n\n{detail}"))
}

fn accessory_size(width: f64, height: f64) -> Result<(f64, f64), String> {
    if !width.is_finite() || !height.is_finite() || width < 480.0 || height < 400.0 {
        return Err("Native review needs a larger visible screen".into());
    }
    Ok((
        (width - 220.0).clamp(260.0, 620.0),
        (height - 360.0).clamp(64.0, 300.0),
    ))
}

#[cfg(target_os = "macos")]
fn build_alert(
    mtm: objc2::MainThreadMarker,
    screen_size: objc2_foundation::NSSize,
    title: &str,
    explanation: &str,
    accept: &str,
    detail: &str,
) -> Result<objc2::rc::Retained<objc2_app_kit::NSAlert>, String> {
    use objc2::MainThreadOnly;
    use objc2_app_kit::{
        NSAlert, NSAlertStyle, NSAutoresizingMaskOptions, NSFont, NSScrollView, NSTextView,
    };
    use objc2_foundation::{NSPoint, NSRect, NSSize, NSString};
    let (mut width, mut height) = accessory_size(screen_size.width, screen_size.height)?;
    let alert = NSAlert::new(mtm);
    alert.setAlertStyle(NSAlertStyle::Warning);
    alert.setMessageText(&NSString::from_str(title));
    alert.setInformativeText(&NSString::from_str(explanation));
    let confirm = alert.addButtonWithTitle(&NSString::from_str(accept));
    // Return must not accidentally approve this privileged operation.
    confirm.setKeyEquivalent(&NSString::from_str(""));
    let cancel = alert.addButtonWithTitle(&NSString::from_str("Cancel"));
    cancel.setKeyEquivalent(&NSString::from_str("\u{1b}"));
    let frame = NSRect::new(NSPoint::new(0.0, 0.0), NSSize::new(width, height));
    let scroll = NSScrollView::initWithFrame(NSScrollView::alloc(mtm), frame);
    scroll.setHasVerticalScroller(true);
    scroll.setHasHorizontalScroller(false);
    scroll.setAutohidesScrollers(false);
    let text = NSTextView::initWithFrame(NSTextView::alloc(mtm), frame);
    text.setEditable(false);
    text.setSelectable(true);
    text.setRichText(false);
    text.setFont(NSFont::userFixedPitchFontOfSize(11.0).as_deref());
    text.setMinSize(NSSize::new(0.0, height));
    text.setMaxSize(NSSize::new(f64::MAX, f64::MAX));
    text.setVerticallyResizable(true);
    text.setHorizontallyResizable(false);
    text.setAutoresizingMask(NSAutoresizingMaskOptions::ViewWidthSizable);
    if let Some(container) = unsafe { text.textContainer() } {
        container.setContainerSize(NSSize::new(width, f64::MAX));
        container.setWidthTracksTextView(true);
    }
    text.setString(&NSString::from_str(detail));
    scroll.setDocumentView(Some(&text));
    alert.setAccessoryView(Some(&scroll));
    // AppKit measures the real title, summary and buttons. Shrink only
    // the scroll area; never clip the privileged buttons or full text.
    for _ in 0..4 {
        alert.layout();
        let actual = alert.window().frame().size;
        if actual.width <= screen_size.width - 32.0 && actual.height <= screen_size.height - 32.0 {
            break;
        }
        width -= (actual.width - screen_size.width + 40.0).max(0.0);
        height -= (actual.height - screen_size.height + 40.0).max(0.0);
        if width < 200.0 || height < 64.0 {
            return Err("Native review cannot fit the visible screen".into());
        }
        scroll.setFrameSize(NSSize::new(width, height));
    }
    alert.layout();
    let actual = alert.window().frame().size;
    if actual.width > screen_size.width - 32.0 || actual.height > screen_size.height - 32.0 {
        return Err("Native review cannot fit the visible screen".into());
    }
    Ok(alert)
}

#[cfg(target_os = "macos")]
pub(crate) fn show(
    window: &tauri::WebviewWindow,
    title: &str,
    explanation: &str,
    accept: &str,
    plan: &Value,
    nonce: &str,
) -> Result<bool, String> {
    use block2::RcBlock;
    use dispatch2::{DispatchQueue, MainThreadBound};
    use objc2::{rc::Retained, MainThreadMarker};
    use objc2_app_kit::{NSAlertFirstButtonReturn, NSModalResponseCancel, NSScreen, NSWindow};
    use std::sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    };
    use std::time::{Duration, Instant};
    use tauri::Manager;

    static REVIEW_OPEN: AtomicBool = AtomicBool::new(false);
    struct ReviewLease;
    impl Drop for ReviewLease {
        fn drop(&mut self) {
            REVIEW_OPEN.store(false, Ordering::Release);
        }
    }
    REVIEW_OPEN
        .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
        .map_err(|_| "A Native Host review is already open")?;
    let _lease = ReviewLease;
    let detail = full_detail(plan, nonce)?;
    let digest = plan
        .get("plan_digest")
        .and_then(Value::as_str)
        .ok_or("Native plan identity is missing")?;
    let explanation = format!("{explanation}\n\nPlan: {digest}\nRead the complete plan below. This review expires in four minutes.");
    let title = title.to_owned();
    let accept = accept.to_owned();
    let window = window.clone();
    let deadline = Instant::now() + Duration::from_secs(REVIEW_SECONDS);
    let expired = Arc::new(AtomicBool::new(false));
    let task_expired = expired.clone();
    let (result_tx, result_rx) = std::sync::mpsc::channel();
    let app = window.app_handle().clone();
    app.run_on_main_thread(move || {
        let setup = || -> Result<(), String> {
            if task_expired.load(Ordering::Acquire) || Instant::now() >= deadline {
                return Err("Native review expired before display".into());
            }
            let mtm = MainThreadMarker::new().ok_or("Native review is unavailable")?;
            let pointer = window
                .ns_window()
                .map_err(|_| "Native review owner is unavailable")?;
            // Tauri supplies the exact still-owned main window on its UI thread.
            let parent = unsafe { Retained::<NSWindow>::retain(pointer.cast()) }
                .ok_or("Native review owner is unavailable")?;
            let screen = parent
                .screen()
                .or_else(|| NSScreen::mainScreen(mtm))
                .ok_or("Native review screen is unavailable")?;
            let visible = screen.visibleFrame();
            if parent.attachedSheet().is_some() {
                return Err("Finish the existing Native sheet before this review".into());
            }
            let alert = build_alert(mtm, visible.size, &title, &explanation, &accept, &detail)?;
            let owned = Arc::new(MainThreadBound::new((parent.clone(), alert.clone()), mtm));
            let (done_tx, done_rx) = std::sync::mpsc::channel();
            let callback_tx = result_tx.clone();
            let callback_expired = task_expired.clone();
            let completion = RcBlock::new(move |response| {
                let accepted = response == NSAlertFirstButtonReturn
                    && !callback_expired.load(Ordering::Acquire)
                    && Instant::now() < deadline;
                let _ = callback_tx.send(Ok(accepted));
                let _ = done_tx.send(());
            });
            alert.beginSheetModalForWindow_completionHandler(&parent, Some(&completion));
            // No global modal cancellation and no main-thread join. The
            // watcher retains this exact alert until its one-shot completion.
            std::thread::spawn(move || {
                let remaining = deadline.saturating_duration_since(Instant::now());
                if done_rx.recv_timeout(remaining).is_err() {
                    task_expired.store(true, Ordering::Release);
                    DispatchQueue::main().exec_async(move || {
                        if let Some(mtm) = MainThreadMarker::new() {
                            let (parent, alert) = owned.get(mtm);
                            if parent.attachedSheet().as_deref() == Some(&*alert.window()) {
                                parent.endSheet_returnCode(&alert.window(), NSModalResponseCancel);
                            }
                        }
                    });
                }
            });
            Ok(())
        };
        if let Err(error) = setup() {
            let _ = result_tx.send(Err(error));
        }
    })
    .map_err(|_| "Native review could not be scheduled".to_string())?;
    match result_rx.recv_timeout(Duration::from_secs(REVIEW_SECONDS + 5)) {
        Ok(result) if !expired.load(Ordering::Acquire) && Instant::now() < deadline => result,
        _ => {
            expired.store(true, Ordering::Release);
            Err("Native review expired; no approval was issued".into())
        }
    }
}

#[cfg(not(target_os = "macos"))]
pub(crate) fn show(
    _window: &tauri::WebviewWindow,
    _title: &str,
    _explanation: &str,
    _accept: &str,
    _plan: &Value,
    _nonce: &str,
) -> Result<bool, String> {
    Err("Native development Host review is available only on macOS".into())
}

/// Exercise actual AppKit layout on its main thread without opening a review.
#[cfg(all(test, target_os = "macos"))]
pub(crate) fn native_layout_smoke() {
    use objc2::{rc::Retained, MainThreadMarker};
    use objc2_app_kit::{NSApplication, NSScrollView, NSTextView};
    use objc2_foundation::NSSize;
    let mtm = MainThreadMarker::new().expect("AppKit layout requires the main thread");
    let _application = NSApplication::sharedApplication(mtm);
    let plan = serde_json::json!({"plan_digest": "sha256:".to_owned() + &"a".repeat(64),
        "maximum_plan": "完整計画".repeat(16_000), "last_field": "end-of-plan"});
    let detail = full_detail(&plan, "native-nonce-exact").unwrap();
    for (width, height) in [(640.0, 480.0), (800.0, 600.0), (1280.0, 800.0)] {
        let alert = build_alert(mtm, NSSize::new(width, height),
            "Review Tobkiri development Host handover",
            "Move verified base VM storage and import Profile, connection, model and text-history data? Connections stay disabled until credentials are registered again. Review and activate the new Profile separately. Previous guest storage, keys and authority stay in the previous Host.\n\nPlan: sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\nRead the complete plan below. This review expires in four minutes.",
            "Move storage and import data", &detail).unwrap();
        let actual = alert.window().frame().size;
        assert!(actual.width <= width - 32.0 && actual.height <= height - 32.0);
        let accessory = alert.accessoryView().unwrap();
        // These views are created with the exact classes in build_alert above.
        let scroll = unsafe { &*(Retained::as_ptr(&accessory) as *const NSScrollView) };
        let document = scroll.documentView().unwrap();
        let text = unsafe { &*(Retained::as_ptr(&document) as *const NSTextView) };
        assert!(!text.isEditable());
        assert!(text.isSelectable());
        assert_eq!(text.string().to_string(), detail);
        assert!(scroll.hasVerticalScroller());
        let buttons = alert.buttons();
        assert_eq!(buttons.len(), 2);
        assert_eq!(buttons.objectAtIndex(0).keyEquivalent().to_string(), "");
        for button in buttons.iter() {
            let frame = button.convertRect_toView(button.bounds(), None);
            println!("button window rect: {frame:?}; alert size: {actual:?}");
            assert!(frame.origin.y >= 0.0 && frame.origin.y + frame.size.height <= actual.height);
        }
        println!(
            "AppKit layout {width}x{height}: {}x{}, full read-only plan, both buttons fit",
            actual.width, actual.height
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn complete_large_plan_and_nonce_remain_readable_without_truncation() {
        let plan = serde_json::json!({"plan_digest": "sha256:".to_owned() + &"a".repeat(64),
            "maximum_plan": "完整計画".repeat(16_000), "last_field": "end-of-plan"});
        let text = full_detail(&plan, "native-nonce-exact").unwrap();
        let (header, json) = text.split_once("\n\n").unwrap();
        assert_eq!(header, "Ceremony nonce: native-nonce-exact");
        assert_eq!(serde_json::from_str::<Value>(json).unwrap(), plan);
    }
    #[test]
    fn compact_and_normal_screens_reserve_space_outside_scrolling_detail() {
        for (width, height) in [
            (640.0, 480.0),
            (800.0, 600.0),
            (1280.0, 800.0),
            (1920.0, 1080.0),
        ] {
            let (detail_width, detail_height) = accessory_size(width, height).unwrap();
            assert!(detail_width + 220.0 <= width);
            assert!(detail_height + 360.0 <= height);
        }
        assert!(accessory_size(400.0, 300.0).is_err());
        assert!(accessory_size(f64::NAN, 800.0).is_err());
    }
}
