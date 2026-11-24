//! Real native root custody integration; no Profile or execution grant exists.
#![cfg(all(target_os = "macos", target_arch = "aarch64"))]

use super::*;
use std::sync::Arc;
use tobkiri_gate::{CapacityKind, CoreControl, CoreControlLimits, NativeSetupLease};

#[test]
fn retained_setup_output_reopens_same_real_root_budget_without_any_profile() {
    let parent = tempfile::tempdir().unwrap();
    let canonical = std::fs::canonicalize(parent.path()).unwrap();
    let original_path = canonical.join("original");
    let renamed_path = canonical.join("renamed");
    let root = DataRoot::open(&original_path).unwrap();
    let ledger = shared(&root).unwrap();
    let control = CoreControl::default();
    let account = ledger
        .native_setup_account(&control, || {
            Ok(Arc::new(
                root.native_setup_owner_lease(&control)
                    .map_err(|_| tobkiri_gate::GateError::Unavailable)?,
            ) as Arc<dyn NativeSetupLease>)
        })
        .unwrap();
    let generation = account.snapshot().unwrap().account_generation;
    let invocation = account.begin().unwrap();
    // The test proves real-root ledger custody; it does not allocate or certify
    // a 64MiB guest. Actual worker/input/output allocations still need binding.
    let output = invocation
        .reserve(
            CapacityKind::AbiMarshal,
            tobkiri_gate::NATIVE_SETUP_MEMORY_BYTES,
        )
        .unwrap();
    drop(invocation);
    drop(account);
    drop(ledger);
    drop(root);
    std::fs::rename(&original_path, &renamed_path).unwrap();

    let reopened = DataRoot::open(&renamed_path).unwrap();
    let replacement = CoreControl::new(CoreControlLimits {
        max_registrations: 0,
        max_frames: 0,
        ..Default::default()
    })
    .unwrap();
    let same_ledger = shared(&reopened).unwrap();
    let next = same_ledger
        .native_setup_account(&replacement, || {
            Ok(Arc::new(
                reopened
                    .native_setup_owner_lease(&replacement)
                    .map_err(|_| tobkiri_gate::GateError::Unavailable)?,
            ) as Arc<dyn NativeSetupLease>)
        })
        .unwrap();
    let snapshot = next.snapshot().unwrap();
    assert_eq!(snapshot.account_generation, generation);
    assert_eq!(
        snapshot.charged_bytes,
        tobkiri_gate::NATIVE_SETUP_MEMORY_BYTES
    );
    let next_invocation = next.begin().unwrap();
    assert_eq!(replacement.usage().bytes, 0);
    assert!(next_invocation.reserve(CapacityKind::VfsBuffer, 1).is_err());
    drop(output);
    assert_eq!(next.snapshot().unwrap().charged_bytes, 0);
    let now_available = next_invocation
        .reserve(
            CapacityKind::AbiMarshal,
            tobkiri_gate::NATIVE_SETUP_MEMORY_BYTES,
        )
        .unwrap();
    assert_eq!(now_available.provenance().account_generation, generation);
    drop(now_available);
    assert_eq!(next.snapshot().unwrap().charged_bytes, 0);
}
