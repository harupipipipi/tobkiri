//! Opt-in, debug-only native observations. No guest data or authority changes.

use std::time::Instant;

#[derive(Clone, Copy)]
pub(super) enum AuditPhase {
    Invoked,
    Prepared,
    Authorized,
    Submitted,
    Scheduled,
    Consumed,
    Executed,
    OutputReceived,
    Converted,
    ConversionObserved,
    CanonicalReady,
    Recorded,
    AckInspected,
    CompletionSent,
    WaitEnded,
}
impl AuditPhase {
    fn label(self) -> &'static str {
        match self {
            Self::Invoked => "invoked",
            Self::Prepared => "prepared",
            Self::Authorized => "authorized",
            Self::Submitted => "submitted",
            Self::Scheduled => "scheduled",
            Self::Consumed => "consumed",
            Self::Executed => "executed",
            Self::OutputReceived => "output-received",
            Self::Converted => "converted",
            Self::ConversionObserved => "conversion-observed",
            Self::CanonicalReady => "canonical-ready",
            Self::Recorded => "recorded",
            Self::AckInspected => "ack-inspected",
            Self::CompletionSent => "completion-sent",
            Self::WaitEnded => "wait-ended",
        }
    }
}

/// Inline metadata, included in the actual supervised future's admission.
/// No retained rows, identities, payloads, errors, callback or new task.
#[derive(Clone, Copy)]
pub(super) struct AuditTiming {
    started: Option<Instant>,
    deadline: Instant,
    operation: &'static str,
}
impl AuditTiming {
    pub(super) fn new(operation: &str, deadline: Instant) -> Self {
        let operation = match operation {
            "append" => "append",
            "read" => "read",
            _ => "invalid",
        };
        Self {
            started: (operation != "invalid"
                && std::env::var_os("TOBKIRI_TEST_AUDIT_PHASE_TIMING")
                    .is_some_and(|value| value == "1"))
            .then(Instant::now),
            deadline,
            operation,
        }
    }

    /// Call only outside SQL/transaction, snapshot and lifecycle-selection guards.
    pub(super) fn mark(&self, phase: AuditPhase) {
        if let Some(started) = self.started {
            let now = Instant::now();
            eprintln!(
                "AUDIT_IMPORT_PHASE operation={} stage={} elapsed_us={} wall_remaining_ms={}",
                self.operation,
                phase.label(),
                now.saturating_duration_since(started).as_micros(),
                self.deadline.saturating_duration_since(now).as_millis(),
            );
        }
    }
}
