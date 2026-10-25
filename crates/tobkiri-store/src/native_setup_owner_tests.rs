//! Actual supported-platform custody tests; no Profile is fabricated for Setup.
use super::*;
use std::process::Command;
use tobkiri_gate::{ControlKind, CoreControlLimits};

const CHILD_ROOT: &str = "TOBKIRI_NATIVE_SETUP_LOCK_TEST_ROOT";
const CHILD_EXPECT_BLOCKED: &str = "TOBKIRI_NATIVE_SETUP_LOCK_TEST_BLOCKED";

fn restricted_control() -> CoreControl {
    CoreControl::new(CoreControlLimits {
        max_registrations: 0,
        ..Default::default()
    })
    .unwrap()
}

#[test]
fn retained_setup_lease_reuses_original_funding_after_root_rename() {
    let parent = tempfile::tempdir().unwrap();
    let canonical = std::fs::canonicalize(parent.path()).unwrap();
    let original = canonical.join("original");
    let renamed = canonical.join("renamed");
    let root = DataRoot::open(&original).unwrap();
    let control = CoreControl::default();
    let first = root.native_setup_owner_lease(&control).unwrap();
    let retained = first.clone();
    let identity = first.identity().clone();
    let domain = first.allocation_domain().clone();
    let held_slots = control.usage().count(ControlKind::Registration);
    assert!((1..=2).contains(&held_slots));
    drop(first);
    drop(root);
    std::fs::rename(&original, &renamed).unwrap();

    let replacement_control = restricted_control();
    let reopened = DataRoot::open(&renamed).unwrap();
    let next = reopened
        .native_setup_owner_lease(&replacement_control)
        .unwrap();
    assert!(Arc::ptr_eq(&retained.0, &next.0));
    assert_eq!(next.identity(), &identity);
    assert_eq!(next.allocation_domain(), &domain);
    assert_eq!(
        replacement_control.usage().count(ControlKind::Registration),
        0
    );
    assert_eq!(control.usage().count(ControlKind::Registration), held_slots);
    next.check_live().unwrap();
    drop(retained);
    assert_eq!(control.usage().count(ControlKind::Registration), held_slots);
    drop(next);
    assert_eq!(
        control.usage().count(ControlKind::Registration),
        held_slots - 1
    );
}

#[test]
fn exhausted_metadata_funding_denies_before_native_owner_construction() {
    let parent = tempfile::tempdir().unwrap();
    let canonical = std::fs::canonicalize(parent.path()).unwrap();
    let root = DataRoot::open(&canonical.join("root")).unwrap();
    let exhausted = restricted_control();
    assert!(matches!(
        root.native_setup_owner_lease(&exhausted),
        Err(StoreError::Quota)
    ));
    assert_eq!(exhausted.usage().count(ControlKind::Registration), 0);
    let funded = CoreControl::default();
    let lease = root.native_setup_owner_lease(&funded).unwrap();
    let held_slots = funded.usage().count(ControlKind::Registration);
    assert!((1..=2).contains(&held_slots));
    drop(lease);
    assert_eq!(
        funded.usage().count(ControlKind::Registration),
        held_slots - 1
    );
}

fn assert_exec_lock(root: &std::path::Path, blocked: bool) {
    let output = Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "owner::native_setup_owner_tests::exec_process_observes_the_real_root_lock",
            "--nocapture",
        ])
        .env(CHILD_ROOT, root)
        .env(CHILD_EXPECT_BLOCKED, if blocked { "yes" } else { "no" })
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "exec child failed: {} {}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(String::from_utf8_lossy(&output.stdout).contains("1 passed"));
}

#[test]
fn setup_and_pack_share_one_real_root_lock_until_actual_last_lease_drop() {
    let parent = tempfile::tempdir().unwrap();
    let canonical = std::fs::canonicalize(parent.path()).unwrap();
    let path = canonical.join("root");
    let root = DataRoot::open(&path).unwrap();
    let control = CoreControl::default();
    let native = root.native_setup_owner_lease(&control).unwrap();
    let held_slots = control.usage().count(ControlKind::Registration);
    // Ordinary fixture owner only; never a Setup principal or execution grant.
    let profile = ProfileId::parse("prof_native_root_custody").unwrap();
    let pack = PackId::parse("local.native-root-custody").unwrap();
    let ordinary = root.owner_lease(&profile, &pack).unwrap();
    assert_eq!(native.allocation_domain(), &ordinary.allocation_domain());
    assert!(Arc::ptr_eq(&native.0._root_lock, &ordinary.0._root_lock));
    drop(ordinary);
    drop(root);
    assert_exec_lock(&path, true);
    drop(native);
    assert_eq!(
        control.usage().count(ControlKind::Registration),
        held_slots - 1
    );
    assert_exec_lock(&path, false);
}

#[test]
fn exec_process_observes_the_real_root_lock() {
    let Some(path) = std::env::var_os(CHILD_ROOT) else {
        return;
    };
    let root = DataRoot::open(&std::path::PathBuf::from(path)).unwrap();
    let control = CoreControl::default();
    let result = root.native_setup_owner_lease(&control);
    if std::env::var(CHILD_EXPECT_BLOCKED).unwrap() == "yes" {
        assert!(matches!(result, Err(StoreError::Conflict(_))));
        assert_eq!(control.usage().count(ControlKind::Registration), 0);
    } else {
        result.unwrap().check_live().unwrap();
    }
}
