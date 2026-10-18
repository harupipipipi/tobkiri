//! Native-independent parts of the host contract, shared by actual callbacks.

use std::{
    sync::mpsc::{Receiver, RecvTimeoutError},
    time::Duration,
};
use tobkiri_core::{ApprovalPrompt, HostError as IoError, Risk};

/// Guest UI is unavailable until Core supplies a real attachment lifecycle.
/// This slot guard supplements, and does not prove, caller provenance.
pub fn startup_ui_embedding<T>(slot: &str, mint: impl FnOnce() -> T) -> Result<T, IoError> {
    if slot != "startup" {
        return Err(IoError(
            "UI unavailable: guest attachment lifecycle is not implemented".into(),
        ));
    }
    Ok(mint())
}

/// Retain the live primary webview so tray/single-instance reopen can restore it.
pub fn preserve_primary_close<E>(
    label: &str,
    prevent_close: impl FnOnce(),
    hide: impl FnOnce() -> Result<(), E>,
) -> Option<Result<(), E>> {
    if label != "core-host" {
        return None;
    }
    prevent_close();
    Some(hide())
}

/// Reopen the same live window, including a minimized/backgrounded instance.
pub fn restore_primary<E>(
    unminimize: impl FnOnce() -> Result<(), E>,
    show: impl FnOnce() -> Result<(), E>,
    focus: impl FnOnce() -> Result<(), E>,
) -> Result<(), E> {
    unminimize()?;
    show()?;
    focus()
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ShutdownCompletion {
    Closed,
    CleanupFailed,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ShutdownObservation {
    Closed,
    CleanupFailed,
    Deadline,
    WorkerLost,
}

/// This outer-thread wait remains bounded even if the async worker blocks.
/// Deadline/worker loss do not establish cleanup or audit completion.
/// Closed means only that Core's close API returned successfully; this does
/// not independently prove all asynchronous run journals have drained.
pub fn observe_shutdown(
    receiver: Receiver<ShutdownCompletion>,
    budget: Duration,
) -> ShutdownObservation {
    match receiver.recv_timeout(budget) {
        Ok(ShutdownCompletion::Closed) => ShutdownObservation::Closed,
        Ok(ShutdownCompletion::CleanupFailed) => ShutdownObservation::CleanupFailed,
        Err(RecvTimeoutError::Timeout) => ShutdownObservation::Deadline,
        Err(RecvTimeoutError::Disconnected) => ShutdownObservation::WorkerLost,
    }
}

/// Only Core's typed risk controls the special native warning presentation.
pub struct TrustedReview {
    pub title: &'static str,
    pub explanation: &'static str,
    pub accept: &'static str,
    pub dangerous: bool,
    pub detail: String,
}

pub fn trusted_review(prompt: &ApprovalPrompt) -> Result<TrustedReview, IoError> {
    // Serialize the complete trusted effect/context, not a host summary or an
    // opaque request id standing in for the operation under review.
    let detail = serde_json::to_string_pretty(prompt)
        .map_err(|_| IoError("native review unavailable".into()))?;
    if detail.len() > 512 * 1024 {
        return Err(IoError("native review exceeds its display limit".into()));
    }
    let dangerous = prompt.risk == Risk::Dangerous;
    Ok(TrustedReview {
        title: if dangerous {
            "Dangerous Tobkiri request"
        } else {
            "Tobkiri approval"
        },
        explanation: if dangerous {
            "Special warning: this request can cause serious or lasting changes. Review the complete effect below. Approve only this exact request. This review expires in four minutes."
        } else {
            "Review the complete Core request below. This review expires in four minutes."
        },
        accept: if dangerous {
            "Approve dangerous request"
        } else {
            "Approve this exact request"
        },
        dangerous,
        detail,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{cell::Cell, sync::mpsc, time::Instant};

    #[test]
    fn unavailable_guest_slots_never_mint_a_phantom_frame() {
        let minted = Cell::new(0);
        for slot in ["", "workspace", "secondary", "startup-ui"] {
            assert!(startup_ui_embedding(slot, || minted.set(minted.get() + 1)).is_err());
        }
        assert_eq!(minted.get(), 0);
        assert_eq!(
            startup_ui_embedding("startup", || "frame-reservation").unwrap(),
            "frame-reservation"
        );
    }

    #[test]
    fn close_preserves_the_window_then_tray_and_duplicate_launch_restore_it() {
        let prevented = Cell::new(false);
        let visible = Cell::new(true);
        let live = Cell::new(true);
        let minimized = Cell::new(true);
        let focused = Cell::new(false);
        preserve_primary_close(
            "core-host",
            || prevented.set(true),
            || {
                visible.set(false);
                Ok::<_, ()>(())
            },
        )
        .unwrap()
        .unwrap();
        // Model the actual window system's default destruction after callback.
        if !prevented.get() {
            live.set(false);
        }
        assert!(live.get());
        assert!(!visible.get());
        restore_primary(
            || {
                assert!(live.get());
                minimized.set(false);
                Ok::<_, ()>(())
            },
            || {
                visible.set(true);
                Ok(())
            },
            || {
                focused.set(true);
                Ok(())
            },
        )
        .unwrap();
        assert!(visible.get() && focused.get() && !minimized.get());
        assert!(
            preserve_primary_close::<()>(
                "another-window",
                || panic!("wrong owner"),
                || panic!("wrong owner")
            )
            .is_none()
        );
    }

    #[test]
    fn wedged_shutdown_is_bounded_and_never_reported_as_a_clean_audit_close() {
        let (sender, receiver) = mpsc::channel();
        let started = Instant::now();
        assert_eq!(
            observe_shutdown(receiver, Duration::from_millis(10)),
            ShutdownObservation::Deadline
        );
        assert!(started.elapsed() < Duration::from_secs(1));
        drop(sender);
        let (sender, receiver) = mpsc::channel();
        sender.send(ShutdownCompletion::CleanupFailed).unwrap();
        assert_eq!(
            observe_shutdown(receiver, Duration::from_secs(1)),
            ShutdownObservation::CleanupFailed
        );
        let (sender, receiver) = mpsc::channel();
        sender.send(ShutdownCompletion::Closed).unwrap();
        assert_eq!(
            observe_shutdown(receiver, Duration::from_secs(1)),
            ShutdownObservation::Closed
        );
        let (sender, receiver) = mpsc::channel();
        drop(sender);
        assert_eq!(
            observe_shutdown(receiver, Duration::from_secs(1)),
            ShutdownObservation::WorkerLost
        );
    }

    fn make_prompt(risk: &str) -> ApprovalPrompt {
        serde_json::from_value(serde_json::json!({
            "reservation":"grant_review_test", "profile":"test-profile", "principal":{"kind":"user"},
            "request":"req_review", "effect":{"kind":"host-io","operation":"pick-directory"},
            "args_digest":format!("sha256:{}","a".repeat(64)), "security_epoch":7,
            "risk":risk, "summary":"Core trusted summary"
        })).unwrap()
    }

    #[test]
    fn complete_effect_and_context_survive_without_truncation_and_danger_has_a_special_warning() {
        let mut prompt = make_prompt("dangerous");
        prompt.summary = "完整內容".repeat(10_000);
        let review = trusted_review(&prompt).unwrap();
        let shown: serde_json::Value = serde_json::from_str(&review.detail).unwrap();
        assert_eq!(shown, serde_json::to_value(&prompt).unwrap());
        assert_eq!(
            shown["effect"],
            serde_json::json!({"kind":"host-io","operation":"pick-directory"})
        );
        assert!(review.dangerous);
        assert!(review.title.starts_with("Dangerous"));
        assert!(review.explanation.starts_with("Special warning:"));
        assert_eq!(review.accept, "Approve dangerous request");
        let normal = trusted_review(&make_prompt("normal")).unwrap();
        assert!(!normal.dangerous);
        assert_ne!(normal.title, review.title);
        assert_ne!(normal.explanation, review.explanation);
        prompt.summary = "x".repeat(512 * 1024);
        assert!(trusted_review(&prompt).is_err());
    }
}
