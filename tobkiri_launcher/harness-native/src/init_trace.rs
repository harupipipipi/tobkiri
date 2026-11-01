//! Fixed-size native observations; no checkpoint grants authority or success.
use std::sync::atomic::{AtomicU8, AtomicU64, Ordering};
use tobkiri_canon::CoreValue;

pub(super) const STAGES: [&str; 7] = [
    "dispatcher-entry",
    "wire-decoded",
    "scope-decoded",
    "factory-entering",
    "factory-returned",
    "registry-entering",
    "registry-returned",
];

/// Host validation failures are separate from guest-reported progress.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
pub enum InitializationRejection {
    /// No host validation failure was recorded.
    None = 0,
    /// The actual import context belongs to another operation.
    Invocation = 1,
    /// The import is unavailable in the actual native phase.
    Phase = 2,
    /// The request is not an admitted closed initialization observation.
    Frame = 3,
    /// The checkpoint repeated or moved backwards.
    Sequence = 4,
}

/// Bounded observations from this actual Store, including when no PyErr arrived.
#[derive(Clone, Copy, Debug)]
pub struct InitializationTrace {
    /// Actual host phase, independent of all guest checkpoint payloads.
    pub host_phase: &'static str,
    /// Calls reaching the fixed inventory import.
    pub inventory_entries: u64,
    /// Inventory calls passing the owner and native-phase checks.
    pub inventory_validated: u64,
    /// Calls reaching the fixed inspection import.
    pub inspect_entries: u64,
    /// Closed init observations passing native validation.
    pub inspect_validated: u64,
    /// Last admitted checkpoint. This is guest-reported DATA, not proof.
    pub guest_checkpoint: Option<&'static str>,
    /// Last admitted sequence; gaps do not invent missing observations.
    pub checkpoint_sequence: u8,
    /// Total admitted checkpoints, bounded by seven.
    pub checkpoint_count: u8,
    /// Last host validation failure, if any.
    pub last_rejection: InitializationRejection,
}

pub(super) struct InitTrace {
    pub inventory_entries: AtomicU64,
    pub inventory_validated: AtomicU64,
    pub inspect_entries: AtomicU64,
    pub inspect_validated: AtomicU64,
    host_phase: AtomicU8,
    checkpoint: AtomicU8,
    checkpoints: AtomicU8,
    rejection: AtomicU8,
}

impl InitTrace {
    pub fn new() -> Self {
        Self {
            inventory_entries: AtomicU64::new(0),
            inventory_validated: AtomicU64::new(0),
            inspect_entries: AtomicU64::new(0),
            inspect_validated: AtomicU64::new(0),
            host_phase: AtomicU8::new(0),
            checkpoint: AtomicU8::new(0),
            checkpoints: AtomicU8::new(0),
            rejection: AtomicU8::new(0),
        }
    }

    pub fn phase(&self, phase: u8) {
        self.host_phase.store(phase, Ordering::Release);
    }

    pub fn reject(&self, reason: InitializationRejection) {
        self.rejection.store(reason as u8, Ordering::Release);
    }

    pub fn checkpoint(&self, sequence: u8) -> bool {
        // One stored byte and a fixed seven-stage alphabet, never a log Vec.
        let previous = self.checkpoint.load(Ordering::Acquire);
        if sequence <= previous
            || self
                .checkpoint
                .compare_exchange(previous, sequence, Ordering::AcqRel, Ordering::Acquire)
                .is_err()
        {
            self.reject(InitializationRejection::Sequence);
            return false;
        }
        self.checkpoints.fetch_add(1, Ordering::Relaxed);
        true
    }

    pub fn snapshot(&self) -> InitializationTrace {
        let sequence = self.checkpoint.load(Ordering::Acquire);
        InitializationTrace {
            host_phase: match self.host_phase.load(Ordering::Acquire) {
                0 => "store-bindings-preparing",
                1 => "exports-preparing",
                2 => "export-invoking",
                3 => "export-returned",
                4 => "export-trapped",
                _ => "unavailable",
            },
            inventory_entries: self.inventory_entries.load(Ordering::Relaxed),
            inventory_validated: self.inventory_validated.load(Ordering::Relaxed),
            inspect_entries: self.inspect_entries.load(Ordering::Relaxed),
            inspect_validated: self.inspect_validated.load(Ordering::Relaxed),
            guest_checkpoint: sequence
                .checked_sub(1)
                .and_then(|index| STAGES.get(usize::from(index)).copied()),
            checkpoint_sequence: sequence,
            checkpoint_count: self.checkpoints.load(Ordering::Relaxed),
            last_rejection: match self.rejection.load(Ordering::Acquire) {
                0 => InitializationRejection::None,
                1 => InitializationRejection::Invocation,
                2 => InitializationRejection::Phase,
                3 => InitializationRejection::Frame,
                _ => InitializationRejection::Sequence,
            },
        }
    }
}

pub(super) fn checkpoint_sequence(frame: &CoreValue) -> Option<u8> {
    crate::source_metadata::exact(frame, &["format", "sequence", "stage"])?;
    if frame.get_key("format")?.as_str()? != "io.tobkiri.native-init-checkpoint.v1" {
        return None;
    }
    let sequence = u8::try_from(frame.get_key("sequence")?.as_i64()?).ok()?;
    let stage = STAGES.get(usize::from(sequence.checked_sub(1)?))?;
    (frame.get_key("stage")?.as_str()? == *stage).then_some(sequence)
}
