#![cfg(not(all(target_os = "macos", target_arch = "aarch64")))]

use tobkiri_store::{DataRoot, StoreError};

#[test]
fn unproven_allocation_owner_platform_is_unavailable() {
    let dir = tempfile::tempdir().unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(dir.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
    }
    let root = DataRoot::open(dir.path().canonicalize().unwrap());
    #[cfg(unix)]
    {
        let root = root.expect("opening the private physical DATA directory must succeed");
        let profile = tobkiri_canon::ids::ProfileId::parse("prof_owner").unwrap();
        let pack = tobkiri_canon::ids::PackId::parse("local.owner").unwrap();
        for result in [
            root.owner_lease(&profile, &pack),
            root.user_owner_lease(&profile),
        ] {
            let error = result.expect_err("an unverified platform cannot grant an owner lease");
            assert!(
                matches!(
                    &error,
                    StoreError::Unavailable(reason)
                        if reason == "allocation owner OS/filesystem proof unavailable"
                ),
                "the owner platform guard must be the reason for denial: {error:?}"
            );
        }
    }
    #[cfg(not(unix))]
    {
        let error = root.expect_err("descriptor-relative DATA roots are unsupported here");
        assert!(
            matches!(
                &error,
                StoreError::Unavailable(reason)
                    if reason == "descriptor-relative nofollow storage is unsupported on this platform"
            ),
            "the unsupported root contract must be the reason for denial: {error:?}"
        );
    }
}
