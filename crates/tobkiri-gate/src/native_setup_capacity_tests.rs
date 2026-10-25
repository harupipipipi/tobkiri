//! Portable accounting tests. The injected test proof is not actual OS proof.
use super::*;
use crate::CoreControlLimits;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};

struct Proof {
    domain: Digest,
    live: AtomicBool,
    dropped: Arc<AtomicUsize>,
}
impl NativeSetupLease for Proof {
    fn validate(&self) -> Result<(), GateError> {
        if !self.live.load(Ordering::Acquire) {
            return Err(GateError::Unavailable);
        }
        Ok(())
    }
    fn allocation_domain(&self) -> Digest {
        self.domain.clone()
    }
}
impl Drop for Proof {
    fn drop(&mut self) {
        self.dropped.fetch_add(1, Ordering::AcqRel);
    }
}
fn proof(label: &[u8]) -> Arc<Proof> {
    Arc::new(Proof {
        domain: Digest::hash(label),
        live: AtomicBool::new(true),
        dropped: Arc::new(AtomicUsize::new(0)),
    })
}
fn account(
    ledger: &CapacityLedger,
    control: &CoreControl,
    proof: &Arc<Proof>,
) -> NativeSetupAccount {
    ledger
        .native_setup_account(control, || Ok(proof.clone()))
        .unwrap()
}

#[test]
fn same_root_handles_share_one_fixed_payload_budget_and_last_copy_custody() {
    let ledger = CapacityLedger::new();
    let control = CoreControl::default();
    let root = proof(b"root");
    let first = account(&ledger, &control, &root);
    let second = account(&ledger, &control, &root);
    assert_eq!(
        first.snapshot().unwrap().account_generation,
        second.snapshot().unwrap().account_generation
    );
    let invocation = first.begin().unwrap();
    let retained = invocation
        .reserve(CapacityKind::GuestLinear, NATIVE_SETUP_MEMORY_BYTES)
        .unwrap();
    let other = second.begin().unwrap();
    assert!(other.reserve(CapacityKind::AbiMarshal, 1).is_err());
    retained.resize(NATIVE_SETUP_MEMORY_BYTES - 1).unwrap();
    let output = other.reserve(CapacityKind::AbiMarshal, 1).unwrap();
    assert_eq!(
        second.snapshot().unwrap().charged_bytes,
        NATIVE_SETUP_MEMORY_BYTES
    );
    assert!(retained.resize(NATIVE_SETUP_MEMORY_BYTES).is_err());
    assert_eq!(
        retained.allocated_bytes().unwrap(),
        NATIVE_SETUP_MEMORY_BYTES - 1
    );
    let copy = retained.clone();
    drop(retained);
    assert_eq!(first.snapshot().unwrap().reservations, 2);
    drop(copy);
    assert_eq!(first.snapshot().unwrap().charged_bytes, 1);
    drop(output);
    assert_eq!(first.snapshot().unwrap().charged_bytes, 0);
}

#[test]
fn replacement_core_control_does_not_mint_new_invocation_or_payload_funding() {
    let ledger = CapacityLedger::new();
    let original = CoreControl::new(CoreControlLimits {
        max_frames: 2,
        ..Default::default()
    })
    .unwrap();
    let replacement = CoreControl::new(CoreControlLimits {
        max_frames: 0,
        max_registrations: 0,
        ..Default::default()
    })
    .unwrap();
    let root = proof(b"root");
    let first = account(&ledger, &original, &root);
    let second = account(&ledger, &replacement, &root);
    let invocation = first.begin().unwrap();
    let output = invocation.reserve(CapacityKind::AbiMarshal, 17).unwrap();
    assert!(second.begin().is_err());
    assert_eq!(replacement.usage().bytes, 0);
    drop(output);
    let next = second.begin().unwrap();
    assert_eq!(
        next.provenance().account_generation,
        invocation.provenance().account_generation
    );
    assert_ne!(
        next.provenance().invocation,
        invocation.provenance().invocation
    );
    assert_eq!(original.usage().count(ControlKind::Frame), 2);
}

#[test]
fn retained_output_keeps_ledger_root_and_metadata_until_actual_release() {
    let ledger = CapacityLedger::new();
    // This weak Core registry handle exists BEFORE native funding is installed.
    let weak = ledger.downgrade();
    let control = CoreControl::default();
    let root = proof(b"root");
    let dropped = root.dropped.clone();
    let native = account(&ledger, &control, &root);
    let invocation = native.begin().unwrap();
    let output = invocation.reserve(CapacityKind::AbiMarshal, 64).unwrap();
    assert_eq!(output.kind(), CapacityKind::AbiMarshal);
    assert_eq!(output.provenance(), invocation.provenance());
    drop(root);
    drop(invocation);
    drop(native);
    drop(ledger);
    assert!(weak.upgrade().is_some());
    assert_eq!(dropped.load(Ordering::Acquire), 0);
    let last = output.clone();
    drop(output);
    assert!(weak.upgrade().is_some());
    drop(last);
    assert!(weak.upgrade().is_none());
    assert_eq!(dropped.load(Ordering::Acquire), 1);
    assert_eq!(control.usage().count(ControlKind::Frame), 0);
    // Weak allocation/bookkeeping remains physically retained by this handle.
    assert!(control.usage().count(ControlKind::Registration) >= 1);
    drop(weak);
    // The process broker retains a Weak until its next bounded prune. Its
    // permanent carrier tier remains funded even after that prune.
    let next_ledger = CapacityLedger::new();
    let next_control = CoreControl::default();
    let next_root = proof(b"next-root");
    let _next_account = account(&next_ledger, &next_control, &next_root);
    let remaining = control.usage().bytes;
    assert!(remaining == 0 || remaining == MAX_NATIVE_LEDGERS as u64 * CARRIER_BYTES_PER_LEDGER);
}

#[test]
fn a_different_domain_or_invalid_live_proof_cannot_replace_the_original_account() {
    let ledger = CapacityLedger::new();
    let control = CoreControl::default();
    let root = proof(b"root");
    let native = account(&ledger, &control, &root);
    let other = proof(b"other-root");
    assert!(ledger.native_setup_account(&control, || Ok(other)).is_err());
    let invocation = native.begin().unwrap();
    let output = invocation.reserve(CapacityKind::VfsBuffer, 32).unwrap();
    root.live.store(false, Ordering::Release);
    assert!(native.begin().is_err());
    assert!(output.resize(64).is_err());
    // Actual released storage can still retire without obtaining authority.
    drop(output);
    root.live.store(true, Ordering::Release);
    assert_eq!(native.snapshot().unwrap().charged_bytes, 0);
    assert_eq!(native.snapshot().unwrap().reservations, 0);
}

#[test]
fn metadata_exhaustion_refuses_before_account_or_record_publication() {
    let ledger = CapacityLedger::new();
    let exhausted = CoreControl::new(CoreControlLimits {
        max_registrations: 0,
        ..Default::default()
    })
    .unwrap();
    let root = proof(b"root");
    assert!(
        ledger
            .native_setup_account(&exhausted, || Ok(root.clone()))
            .is_err()
    );
    assert!(ledger.0.native_setup.lock().unwrap().is_none());
    assert!(ledger.0.domain.lock().unwrap().is_none());
    assert_eq!(exhausted.usage().bytes, 0);
    let no_frames = CoreControl::new(CoreControlLimits {
        max_frames: 0,
        ..Default::default()
    })
    .unwrap();
    let native = account(&ledger, &no_frames, &root);
    assert!(native.begin().is_err());
    assert_eq!(native.snapshot().unwrap().charged_bytes, 0);
    assert_eq!(native.snapshot().unwrap().reservations, 0);
}

#[test]
fn concurrent_invocations_cannot_each_spend_a_fresh_64_mib_pool() {
    let ledger = CapacityLedger::new();
    let control = CoreControl::default();
    let root = proof(b"root");
    let native = account(&ledger, &control, &root);
    let start = Arc::new(std::sync::Barrier::new(9));
    let mut threads = Vec::new();
    for _ in 0..8 {
        let native = native.clone();
        let start = start.clone();
        threads.push(std::thread::spawn(move || {
            start.wait();
            native
                .begin()
                .unwrap()
                .reserve(CapacityKind::GuestLinear, NATIVE_SETUP_MEMORY_BYTES / 8)
        }));
    }
    start.wait();
    let held: Vec<_> = threads
        .into_iter()
        .map(|thread| thread.join().unwrap().unwrap())
        .collect();
    assert_eq!(
        native.snapshot().unwrap().charged_bytes,
        NATIVE_SETUP_MEMORY_BYTES
    );
    assert_eq!(native.snapshot().unwrap().reservations, 8);
    assert!(
        native
            .begin()
            .unwrap()
            .reserve(CapacityKind::HostStack, 1)
            .is_err()
    );
    drop(held);
    assert_eq!(native.snapshot().unwrap().charged_bytes, 0);
}

#[test]
fn invocation_clones_share_one_irrevocable_store_claim_after_root_validation() {
    let ledger = CapacityLedger::new();
    let control = CoreControl::default();
    let root = proof(b"claim-root");
    let native = account(&ledger, &control, &root);
    let invocation = native.begin().unwrap();
    root.live.store(false, Ordering::Release);
    assert!(invocation.claim_store().is_err());
    root.live.store(true, Ordering::Release);
    let start = Arc::new(std::sync::Barrier::new(9));
    let mut threads = Vec::new();
    for _ in 0..8 {
        let invocation = invocation.clone();
        let start = start.clone();
        threads.push(std::thread::spawn(move || {
            start.wait();
            invocation.claim_store().is_ok()
        }));
    }
    start.wait();
    assert_eq!(
        threads
            .into_iter()
            .map(|t| t.join().unwrap())
            .filter(|claimed| *claimed)
            .count(),
        1
    );
    assert!(invocation.claim_store().is_err());
    // Output custody remains valid after the only Store attempt is claimed.
    let output = invocation.reserve(CapacityKind::AbiMarshal, 1).unwrap();
    assert_eq!(output.allocated_bytes().unwrap(), 1);
}

#[test]
fn exact_invocation_identity_does_not_alias_equal_counters_from_another_ledger() {
    let control = CoreControl::default();
    let root = proof(b"same-physical-domain");
    let first = account(&CapacityLedger::new(), &control, &root)
        .begin()
        .unwrap();
    let other = account(&CapacityLedger::new(), &control, &root)
        .begin()
        .unwrap();
    assert_eq!(first.provenance(), other.provenance());
    assert_eq!(
        first.allocation_domain().unwrap(),
        other.allocation_domain().unwrap()
    );
    assert!(first.same_invocation(&first.clone()));
    assert!(!first.same_invocation(&other));
    assert_eq!(
        first.deadline().unwrap(),
        first.started_at() + NATIVE_SETUP_WALL
    );
}
