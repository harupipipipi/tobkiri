//! Capacity-only tests with actual supported-platform root custody.
#![cfg(all(target_os = "macos", target_arch = "aarch64"))]

use crate::DataRoot;
use std::{path::Path, sync::Arc};
use tobkiri_gate::{
    CapacityKind, CapacityLedger, ControlKind, CoreControl, GateError, NATIVE_SETUP_MEMORY_BYTES,
    NativeSetupAccount,
};

fn funding(path: &Path) -> (DataRoot, CoreControl, CapacityLedger, NativeSetupAccount) {
    // Let the unchanged descriptor-relative creator make its own 0700 root;
    // a runner's existing temporary directory need not satisfy that policy.
    let root = DataRoot::open(path.canonicalize().unwrap().join("root")).unwrap();
    let control = CoreControl::default();
    let lease = root.native_setup_owner_lease(&control).unwrap();
    let ledger = CapacityLedger::new();
    let account = ledger
        .native_setup_account(&control, || Ok(Arc::new(lease)))
        .unwrap();
    (root, control, ledger, account)
}

#[test]
fn shared_capacity_binds_two_actual_operations_without_renewing_them() {
    let directory = tempfile::tempdir().unwrap();
    let (_root, _control, _ledger, account) = funding(directory.path());
    let first = account.begin().unwrap();
    let second = account.begin().unwrap();
    assert!(!first.same_invocation(&second));
    let first_deadline = first.deadline().unwrap();
    let second_deadline = second.deadline().unwrap();
    let first_provenance = first.provenance();
    let second_provenance = second.provenance();
    let shared = account
        .reserve_shared_state(CapacityKind::VfsBuffer, 256)
        .unwrap();
    let mut bytes = Vec::<u8>::new();
    bytes.try_reserve_exact(256).unwrap();
    bytes.resize(256, 1);
    assert_eq!(bytes.capacity(), 256);
    shared.check_account_for(&first).unwrap();
    shared.check_account_for(&second).unwrap();
    first.claim_store().unwrap();
    second.claim_store().unwrap();
    shared.resize(512).unwrap();
    bytes.try_reserve_exact(256).unwrap();
    bytes.resize(512, 2);
    assert_eq!(bytes.capacity(), 512);
    assert_eq!(account.snapshot().unwrap().charged_bytes, 512);
    assert_eq!(first.deadline().unwrap(), first_deadline);
    assert_eq!(second.deadline().unwrap(), second_deadline);
    assert_eq!(first.provenance(), first_provenance);
    assert_eq!(second.provenance(), second_provenance);
    assert!(first.claim_store().is_err());
    assert!(second.claim_store().is_err());
    drop(bytes);
    shared.resize(0).unwrap();
    drop(shared);
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
    assert_eq!(account.snapshot().unwrap().reservations, 0);
}

#[test]
fn actual_account_membership_cannot_be_replaced_by_equal_serialized_counters() {
    let first_root = tempfile::tempdir().unwrap();
    let second_root = tempfile::tempdir().unwrap();
    let (_a, _ca, _la, first) = funding(first_root.path());
    let (_b, _cb, _lb, second) = funding(second_root.path());
    let own_operation = first.begin().unwrap();
    let foreign_operation = second.begin().unwrap();
    assert_eq!(own_operation.provenance(), foreign_operation.provenance());
    let state = first
        .reserve_shared_state(CapacityKind::VfsBuffer, 32)
        .unwrap();
    state.check_account_for(&own_operation).unwrap();
    assert!(matches!(
        state.check_account_for(&foreign_operation),
        Err(GateError::Denied(_))
    ));
}

#[test]
fn denied_shared_admission_and_growth_do_not_publish_or_change_existing_records() {
    let directory = tempfile::tempdir().unwrap();
    let (_root, control, _ledger, account) = funding(directory.path());
    let operation = account.begin().unwrap();
    let shared = account
        .reserve_shared_state(CapacityKind::VfsBuffer, 128)
        .unwrap();
    let payload = vec![0_u8; 128];
    // The large reservation is admission-only: no large physical allocation is
    // claimed by this test. It exhausts the SAME account used by real state.
    let remaining = operation
        .reserve(CapacityKind::VfsBuffer, NATIVE_SETUP_MEMORY_BYTES - 128)
        .unwrap();
    let baseline = account.snapshot().unwrap();
    let metadata = control.usage().bytes;
    assert!(matches!(
        account.reserve_shared_state(CapacityKind::VfsBuffer, 1),
        Err(GateError::Limit(_))
    ));
    assert!(matches!(shared.resize(129), Err(GateError::Limit(_))));
    assert_eq!(shared.allocated_bytes().unwrap(), 128);
    assert_eq!(account.snapshot().unwrap(), baseline);
    assert_eq!(control.usage().bytes, metadata);
    drop(payload);
    drop(shared);
    drop(remaining);
    assert_eq!(account.snapshot().unwrap().charged_bytes, 0);
    assert_eq!(account.snapshot().unwrap().reservations, 0);
}

#[test]
fn real_native_work_retains_capacity_after_outer_handles_and_waiter_are_dropped() {
    let directory = tempfile::tempdir().unwrap();
    let (root, control, ledger, account) = funding(directory.path());
    let frames_before = control.usage().count(ControlKind::Frame);
    let shared = account
        .reserve_shared_state(CapacityKind::VfsBuffer, 256)
        .unwrap();
    let work = shared.clone();
    let waiter = shared.clone();
    let (ready_tx, ready_rx) = std::sync::mpsc::channel();
    let (finish_tx, finish_rx) = std::sync::mpsc::channel();
    let worker = std::thread::spawn(move || {
        let payload = vec![0_u8; 256];
        assert_eq!(work.allocated_bytes().unwrap(), payload.len() as u64);
        ready_tx.send(()).unwrap();
        finish_rx
            .recv_timeout(std::time::Duration::from_secs(5))
            .unwrap();
        assert_eq!(work.allocated_bytes().unwrap(), payload.len() as u64);
        drop(payload);
        drop(work);
    });
    ready_rx
        .recv_timeout(std::time::Duration::from_secs(5))
        .unwrap();
    drop(shared);
    drop(account);
    drop(ledger);
    drop(root);
    assert_eq!(waiter.allocated_bytes().unwrap(), 256);
    drop(waiter);
    assert_eq!(control.usage().count(ControlKind::Frame), frames_before + 1);
    assert!(!worker.is_finished());
    finish_tx.send(()).unwrap();
    worker.join().unwrap();
    assert_eq!(control.usage().count(ControlKind::Frame), frames_before);
}
