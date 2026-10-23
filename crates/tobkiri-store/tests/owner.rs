#![cfg(all(target_os = "macos", target_arch = "aarch64"))]
use std::{os::unix::fs::PermissionsExt, process::Command};
use tobkiri_canon::ids::{PackId, ProfileId};
use tobkiri_store::{DataRoot, StoreError};

fn owner() -> (ProfileId, PackId) {
    (
        ProfileId::parse("prof_owner_lock").unwrap(),
        PackId::parse("local.owner-lock").unwrap(),
    )
}
fn child(root: &DataRoot, blocked: bool) {
    let output = Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "owner_lease_child_probe", "--nocapture"])
        .env("TOBKIRI_OWNER_LEASE_TEST_ROOT", root.path())
        .env(
            "TOBKIRI_OWNER_LEASE_TEST_BLOCKED",
            if blocked { "1" } else { "0" },
        )
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}
#[test]
fn owner_lease_child_probe() {
    let Some(path) = std::env::var_os("TOBKIRI_OWNER_LEASE_TEST_ROOT") else {
        return;
    };
    let root = DataRoot::open(path).unwrap();
    let (profile, pack) = owner();
    let result = root.owner_lease(&profile, &pack);
    if std::env::var("TOBKIRI_OWNER_LEASE_TEST_BLOCKED").unwrap() == "1" {
        assert!(matches!(result, Err(StoreError::Conflict(_))));
    } else {
        assert!(result.is_ok());
    }
}
#[test]
fn actual_cross_process_lock_survives_core_root_drop_and_unrelated_descriptor_close() {
    let dir = tempfile::tempdir().unwrap();
    let root = DataRoot::open(dir.path().canonicalize().unwrap().join("data")).unwrap();
    let (profile, pack) = owner();
    let first = root.owner_lease(&profile, &pack).unwrap();
    let same = DataRoot::open(root.path())
        .unwrap()
        .owner_lease(&profile, &pack)
        .unwrap();
    assert_eq!(first.identity(), same.identity());
    child(&root, true);
    // Closing an unrelated descriptor to the same inode must not release flock.
    drop(std::fs::File::open(root.path()).unwrap());
    drop(first);
    child(&root, true);
    let retained = same.clone();
    drop(same);
    drop(root);
    let reopened = DataRoot::open(dir.path().canonicalize().unwrap().join("data")).unwrap();
    child(&reopened, true);
    drop(retained);
    child(&reopened, false);
}
#[test]
fn physical_root_identity_survives_rename_and_distinguishes_replacement_root() {
    let dir = tempfile::tempdir().unwrap();
    let base = dir.path().canonicalize().unwrap();
    let root = DataRoot::open(base.join("root")).unwrap();
    let original = root.identity().unwrap();
    let (profile, pack) = owner();
    let lease = root.owner_lease(&profile, &pack).unwrap();
    std::fs::rename(base.join("root"), base.join("renamed")).unwrap();
    let renamed = DataRoot::open(base.join("renamed")).unwrap();
    assert_eq!(original, renamed.identity().unwrap());
    assert_eq!(
        lease.identity(),
        renamed.owner_lease(&profile, &pack).unwrap().identity()
    );
    let replacement = DataRoot::open(base.join("root")).unwrap();
    assert_ne!(original, replacement.identity().unwrap());
    assert!(replacement.owner_lease(&profile, &pack).is_ok());
    child(&renamed, true);
}
#[test]
fn owner_locks_refuse_nonprivate_root_and_mutable_slot_replacement_cannot_split_domain() {
    let dir = tempfile::tempdir().unwrap();
    let root = DataRoot::open(dir.path().canonicalize().unwrap().join("data")).unwrap();
    let (profile, pack) = owner();
    std::fs::set_permissions(root.path(), std::fs::Permissions::from_mode(0o755)).unwrap();
    assert!(matches!(
        root.owner_lease(&profile, &pack),
        Err(StoreError::Denied(_))
    ));
    std::fs::set_permissions(root.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
    let lease = root.owner_lease(&profile, &pack).unwrap();
    // No mutable lock filename participates in the domain. An external actor
    // cannot split it by creating/replacing the old conventional slot directory.
    std::fs::create_dir(root.path().join(".owner-leases")).unwrap();
    std::fs::write(root.path().join(".owner-leases/slot-0000"), b"replacement").unwrap();
    std::fs::remove_dir_all(root.path().join(".owner-leases")).unwrap();
    child(&root, true);
    drop(lease);
    child(&root, false);
}

fn provenance() -> tobkiri_gate::CapacityProvenance {
    tobkiri_gate::CapacityProvenance {
        hash: Some(tobkiri_canon::ids::PackHash::new(
            tobkiri_canon::Digest::hash(b"owner"),
        )),
        revision: tobkiri_canon::Digest::hash(b"revision"),
        security_epoch: 0,
        request: tobkiri_canon::ids::RequestId::parse("req_owner").unwrap(),
        store: None,
        session: None,
    }
}
#[test]
fn unused_binding_releases_os_lock_and_failed_reservation_retries_with_the_same_guard() {
    use std::sync::Arc;
    use tobkiri_gate::*;
    let dir = tempfile::tempdir().unwrap();
    let root = DataRoot::open(dir.path().canonicalize().unwrap().join("data")).unwrap();
    let (profile, pack) = owner();
    let ledger = CapacityLedger::new();
    let binding = ledger
        .bind_owner(Arc::new(root.owner_lease(&profile, &pack).unwrap()))
        .unwrap();
    child(&root, true);
    drop(binding);
    child(&root, false);
    let binding = ledger
        .bind_owner(Arc::new(root.owner_lease(&profile, &pack).unwrap()))
        .unwrap();
    let key = CapacityOwner::pack_owner(profile, pack);
    assert!(
        ledger
            .reserve(key.clone(), provenance(), CapacityKind::AbiMarshal, 1, 0)
            .is_err()
    );
    child(&root, true);
    let physical = ledger
        .reserve(key, provenance(), CapacityKind::AbiMarshal, 1, 1 << 20)
        .unwrap();
    drop(binding);
    child(&root, true);
    drop(ledger);
    child(&root, true);
    drop(physical);
    child(&root, false);
}
#[test]
fn owned_output_retains_real_os_lease_after_core_handles_and_budget_registry_drop() {
    use std::sync::{Arc, Mutex};
    use tobkiri_canon::{AllocationBudget, AllocationError, AllocationRequest, CoreValue};
    use tobkiri_gate::*;
    struct OutputBudget(Mutex<CapacityReservation>);
    impl AllocationBudget for OutputBudget {
        fn reserve(&self, request: AllocationRequest) -> Result<(), AllocationError> {
            let lease = self.0.lock().map_err(|_| AllocationError::Unavailable)?;
            let size = lease
                .allocated_bytes()
                .map_err(|_| AllocationError::Unavailable)?
                .checked_add(request.bytes)
                .ok_or(AllocationError::Overflow)?;
            lease
                .resize(size, 1 << 20)
                .map_err(|_| AllocationError::Limit)
        }
        fn release(&self, bytes: u64) {
            let lease = self.0.lock().unwrap();
            let size = lease.allocated_bytes().unwrap().checked_sub(bytes).unwrap();
            lease.resize(size, 1 << 20).unwrap();
        }
    }
    let dir = tempfile::tempdir().unwrap();
    let root = DataRoot::open(dir.path().canonicalize().unwrap().join("data")).unwrap();
    let (profile, pack) = owner();
    let ledger = CapacityLedger::new();
    let binding = ledger
        .bind_owner(Arc::new(root.owner_lease(&profile, &pack).unwrap()))
        .unwrap();
    let physical = ledger
        .reserve(
            CapacityOwner::pack_owner(profile, pack),
            provenance(),
            CapacityKind::AbiMarshal,
            0,
            1 << 20,
        )
        .unwrap();
    let budget: Arc<dyn AllocationBudget> = Arc::new(OutputBudget(Mutex::new(physical)));
    let value = CoreValue::string("retained output", &budget).unwrap();
    drop(budget);
    drop(binding);
    drop(ledger);
    drop(root);
    let root = DataRoot::open(dir.path().canonicalize().unwrap().join("data")).unwrap();
    child(&root, true);
    assert_eq!(value.as_str(), Some("retained output"));
    drop(value);
    child(&root, false);
}
