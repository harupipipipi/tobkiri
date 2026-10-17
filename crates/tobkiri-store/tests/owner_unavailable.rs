#![cfg(not(any(windows, all(target_os = "macos", target_arch = "aarch64"))))]
#[test]
fn unproven_allocation_owner_platform_is_unavailable() {
    let dir = tempfile::tempdir().unwrap();
    let root = tobkiri_store::DataRoot::open(dir.path().canonicalize().unwrap());
    if let Ok(root) = root {
        let profile = tobkiri_canon::ids::ProfileId::parse("prof_owner").unwrap();
        let pack = tobkiri_canon::ids::PackId::parse("local.owner").unwrap();
        assert!(matches!(
            root.owner_lease(&profile, &pack),
            Err(tobkiri_store::StoreError::Unavailable(_))
        ));
    }
}
