#![cfg(windows)]
use std::{
    io::{BufRead, BufReader, Write},
    process::{Command, Stdio},
    sync::{Arc, Mutex},
};
use tobkiri_canon::ids::{PackId, ProfileId};
use tobkiri_gate::*;
use tobkiri_store::{DataRoot, StoreError};

fn owner() -> (ProfileId, PackId) {
    (
        ProfileId::parse("prof_windows_owner").unwrap(),
        PackId::parse("local.windows-owner").unwrap(),
    )
}

fn root() -> (tempfile::TempDir, DataRoot) {
    let directory = tempfile::Builder::new()
        .prefix(".windows-owner-")
        .tempdir_in(std::env::current_dir().unwrap())
        .unwrap();
    let root = DataRoot::open(directory.path().canonicalize().unwrap().join("data")).unwrap();
    (directory, root)
}

fn child(root: &DataRoot, blocked: bool) {
    let output = Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "owner_child_probe", "--nocapture"])
        .env("TOBKIRI_WINDOWS_OWNER_ROOT", root.path())
        .env(
            "TOBKIRI_WINDOWS_OWNER_MODE",
            if blocked { "blocked" } else { "free" },
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
fn owner_child_probe() {
    let Some(path) = std::env::var_os("TOBKIRI_WINDOWS_OWNER_ROOT") else {
        return;
    };
    let root = DataRoot::open(path).unwrap();
    let (profile, pack) = owner();
    let result = root.owner_lease(&profile, &pack);
    match std::env::var("TOBKIRI_WINDOWS_OWNER_MODE")
        .unwrap()
        .as_str()
    {
        "blocked" => assert!(matches!(result, Err(StoreError::Conflict(_)))),
        "free" => assert!(result.is_ok()),
        "hold" => {
            let _lease = result.unwrap();
            println!("OWNER_READY");
            std::io::stdout().flush().unwrap();
            // The parent terminates this owned child and verifies the OS has
            // released its native handles. No Rust destructor is involved.
            std::thread::park();
        }
        _ => panic!("unexpected child mode"),
    }
}

#[test]
fn root_exclusion_survives_clone_unrelated_close_and_root_drop() {
    let (directory, root) = root();
    let (profile, pack) = owner();
    let first = root.owner_lease(&profile, &pack).unwrap();
    let same = DataRoot::open(root.path())
        .unwrap()
        .owner_lease(&profile, &pack)
        .unwrap();
    assert_eq!(first.identity(), same.identity());
    child(&root, true);
    drop(DataRoot::open(root.path()).unwrap());
    drop(first);
    child(&root, true);
    let retained = same.clone();
    drop(same);
    drop(root);
    let reopened = DataRoot::open(directory.path().canonicalize().unwrap().join("data")).unwrap();
    child(&reopened, true);
    drop(retained);
    child(&reopened, false);
}

#[test]
fn pinned_root_refuses_rename_and_mutable_slot_cannot_split_owner() {
    let (directory, root) = root();
    let (profile, pack) = owner();
    let lease = root.owner_lease(&profile, &pack).unwrap();
    let target = directory.path().join("renamed");
    assert!(std::fs::rename(root.path(), &target).is_err());
    // No lock-file name participates in the physical owner identity.
    std::fs::create_dir(root.path().join(".owner-leases")).unwrap();
    std::fs::write(root.path().join(".owner-leases/slot-0000"), b"replacement").unwrap();
    std::fs::remove_file(root.path().join(".owner-leases/slot-0000")).unwrap();
    std::fs::remove_dir(root.path().join(".owner-leases")).unwrap();
    child(&root, true);
    let previous_path = root.path().to_owned();
    drop(lease);
    drop(root);
    std::fs::rename(previous_path, &target).unwrap();
    let renamed = DataRoot::open(target).unwrap();
    child(&renamed, false);
}

#[test]
fn process_termination_releases_native_exclusion_without_pid_takeover() {
    let (_directory, root) = root();
    let mut process = Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "owner_child_probe", "--nocapture"])
        .env("TOBKIRI_WINDOWS_OWNER_ROOT", root.path())
        .env("TOBKIRI_WINDOWS_OWNER_MODE", "hold")
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let reader = BufReader::new(process.stdout.take().unwrap());
    let mut ready = false;
    for line in reader.lines() {
        if line.unwrap() == "OWNER_READY" {
            ready = true;
            break;
        }
    }
    assert!(ready, "child exited before acquiring ownership");
    let (profile, pack) = owner();
    assert!(matches!(
        root.owner_lease(&profile, &pack),
        Err(StoreError::Conflict(_))
    ));
    process.kill().unwrap();
    process.wait().unwrap();
    let lease = root.owner_lease(&profile, &pack).unwrap();
    lease.check_live().unwrap();
}

fn provenance() -> CapacityProvenance {
    CapacityProvenance {
        hash: Some(tobkiri_canon::ids::PackHash::new(
            tobkiri_canon::Digest::hash(b"windows owner"),
        )),
        revision: tobkiri_canon::Digest::hash(b"revision"),
        security_epoch: 0,
        request: tobkiri_canon::ids::RequestId::parse("req_windows_owner").unwrap(),
        store: None,
        session: None,
    }
}

#[test]
fn owned_output_retains_physical_lease_after_budget_registry_drop() {
    use tobkiri_canon::{AllocationBudget, AllocationError, AllocationRequest, CoreValue};
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
    let (_directory, root) = root();
    let (profile, pack) = owner();
    let ledger = CapacityLedger::new();
    let binding = ledger
        .bind_owner(Arc::new(root.owner_lease(&profile, &pack).unwrap()))
        .unwrap();
    let reservation = ledger
        .reserve(
            CapacityOwner::pack_owner(profile, pack),
            provenance(),
            CapacityKind::AbiMarshal,
            0,
            1 << 20,
        )
        .unwrap();
    let budget: Arc<dyn AllocationBudget> = Arc::new(OutputBudget(Mutex::new(reservation)));
    let value = CoreValue::string("retained Windows output", &budget).unwrap();
    drop(budget);
    drop(binding);
    drop(ledger);
    child(&root, true);
    assert_eq!(value.as_str(), Some("retained Windows output"));
    drop(value);
    child(&root, false);
}
