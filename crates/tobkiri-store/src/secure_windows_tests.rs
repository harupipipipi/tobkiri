use super::*;
use crate::DataRoot;
use std::{fs, os::windows::fs::OpenOptionsExt};

fn fixture() -> (tempfile::TempDir, DataRoot) {
    let temp = tempfile::tempdir().unwrap();
    let root = DataRoot::open(temp.path().join("data")).unwrap();
    (temp, root)
}

fn set_acl(file: &File, sddl: &str) {
    let descriptor = descriptor(sddl).unwrap();
    let mut dacl = null_mut();
    let mut present = 0;
    let mut defaulted = 0;
    assert_ne!(
        unsafe { GetSecurityDescriptorDacl(descriptor.0, &mut present, &mut dacl, &mut defaulted) },
        0
    );
    assert_ne!(present, 0);
    assert_eq!(
        unsafe {
            SetSecurityInfo(
                file.as_raw_handle(),
                SE_FILE_OBJECT,
                DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
                null_mut(),
                null_mut(),
                dacl,
                null(),
            )
        },
        0
    );
}

// Mount-point reparse points (junctions) do not require symlink privilege. Build
// one with the documented FSCTL so this negative test cannot silently skip.
fn junction(path: &Path, target: &Path) {
    use windows_sys::Win32::{
        System::IO::DeviceIoControl, System::SystemServices::IO_REPARSE_TAG_MOUNT_POINT,
    };
    fs::create_dir(path).unwrap();
    let file = fs::OpenOptions::new()
        .access_mode(FILE_GENERIC_WRITE)
        .share_mode(0)
        .custom_flags(FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT)
        .open(path)
        .unwrap();
    let target = target.to_str().unwrap();
    let native = format!("\\??\\{target}").encode_utf16().collect::<Vec<_>>();
    let print = target.encode_utf16().collect::<Vec<_>>();
    let mut payload = Vec::new();
    payload.extend(IO_REPARSE_TAG_MOUNT_POINT.to_le_bytes());
    payload.extend(((8 + (native.len() + print.len() + 2) * 2) as u16).to_le_bytes());
    payload.extend(0u16.to_le_bytes());
    payload.extend(0u16.to_le_bytes());
    payload.extend(((native.len() * 2) as u16).to_le_bytes());
    payload.extend((((native.len() + 1) * 2) as u16).to_le_bytes());
    payload.extend(((print.len() * 2) as u16).to_le_bytes());
    for value in native.into_iter().chain([0]).chain(print).chain([0]) {
        payload.extend(value.to_le_bytes());
    }
    let mut returned = 0;
    assert_ne!(
        unsafe {
            DeviceIoControl(
                file.as_raw_handle(),
                0x000900a4,
                payload.as_ptr().cast(),
                payload.len() as u32,
                null_mut(),
                0,
                &mut returned,
                null_mut(),
            )
        },
        0,
        "junction creation failed: {}",
        std::io::Error::last_os_error()
    );
}

#[test]
fn windows_private_root_round_trips_bounded_atomic_data_and_identity() {
    let (_temp, root) = fixture();
    let identity = root.physical_identity_key().unwrap();
    root.write_atomic("nested/value", b"safe data").unwrap();
    assert_eq!(root.read("nested/value", 9).unwrap(), b"safe data");
    assert!(matches!(
        root.read("nested/value", 8),
        Err(StoreError::Quota)
    ));
    assert_eq!(root.file_len("nested/value").unwrap(), Some(9));
    assert_eq!(root.file_len("missing/value").unwrap(), None);
    assert_eq!(
        DataRoot::open(root.path())
            .unwrap()
            .physical_identity_key()
            .unwrap(),
        identity
    );
}

#[test]
fn windows_relative_names_reject_streams_devices_aliases_and_traversal() {
    let (_temp, root) = fixture();
    for name in [
        "../escape",
        "file:secret",
        "NUL",
        "con.txt",
        "COM¹",
        "value.",
        "value ",
        "dir/file:stream",
        "C:\\escape",
    ] {
        assert!(
            root.write_atomic(name, b"forbidden").is_err(),
            "accepted {name}"
        );
    }
}

#[test]
fn windows_reparse_ancestor_and_leaf_never_escape_the_pinned_root() {
    let (temp, root) = fixture();
    let outside = temp.path().join("outside");
    fs::create_dir(&outside).unwrap();
    fs::write(outside.join("proof"), b"unchanged").unwrap();
    junction(&root.path().join("escape"), &outside);
    assert!(root.read("escape/proof", 100).is_err());
    assert!(root.write_atomic("escape/proof", b"bad").is_err());
    assert!(DataRoot::open(root.path().join("escape")).is_err());
    assert_eq!(fs::read(outside.join("proof")).unwrap(), b"unchanged");
}

#[test]
fn windows_hardlinks_are_rejected_before_read_write_metadata_or_rename() {
    let (temp, root) = fixture();
    root.write_atomic("value", b"original").unwrap();
    fs::hard_link(root.path().join("value"), temp.path().join("outside-alias")).unwrap();
    assert!(root.read("value", 100).is_err());
    assert!(root.file_len("value").is_err());
    assert!(root.write_atomic("value", b"bad").is_err());
    assert!(root.rename("value", "moved").is_err());
    assert_eq!(
        fs::read(temp.path().join("outside-alias")).unwrap(),
        b"original"
    );
}

#[test]
fn windows_private_acl_is_verified_on_existing_root_and_each_operation() {
    let (_temp, root) = fixture();
    let sid = user_sid().unwrap();
    let control = open_relative(
        &root.directory_capability(),
        "",
        READ_CONTROL | WRITE_DAC | SYNCHRONIZE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        FILE_OPEN,
        FILE_DIRECTORY_FILE,
        false,
    )
    .unwrap();
    set_acl(&control, &format!("D:P(A;;FA;;;{sid})(A;;FA;;;WD)"));
    assert!(private_directory(&root.directory_capability()).is_err());
    assert!(root.write_atomic("blocked", b"bad").is_err());
    assert!(DataRoot::open(root.path()).is_err());
    // Restore only our test object's ACL, through the retained exact handle.
    set_acl(
        &control,
        &format!("D:P(A;OICI;FA;;;{sid})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"),
    );
}

#[test]
fn windows_root_capability_is_read_only_and_prevents_namespace_replacement() {
    let (temp, root) = fixture();
    let handle = root.directory_capability();
    let mut info: FILE_ACCESS_INFORMATION = unsafe { zeroed() };
    let mut block = unsafe { zeroed() };
    let status = unsafe {
        NtQueryInformationFile(
            handle.as_raw_handle(),
            &mut block,
            (&mut info as *mut FILE_ACCESS_INFORMATION).cast(),
            size_of::<FILE_ACCESS_INFORMATION>() as u32,
            FileAccessInformation,
        )
    };
    nt_result(status, &block).unwrap();
    assert_eq!(
        info.AccessFlags
            & (FILE_ADD_FILE | FILE_ADD_SUBDIRECTORY | DELETE | WRITE_DAC | WRITE_OWNER),
        0
    );
    assert!(fs::rename(root.path(), temp.path().join("replaced")).is_err());
    root.write_atomic("still-pinned", b"ok").unwrap();
}

#[test]
fn windows_atomic_rename_never_replaces_an_existing_target() {
    let (_temp, root) = fixture();
    root.write_atomic("a/source", b"first").unwrap();
    root.write_atomic("b/target", b"second").unwrap();
    assert!(root.rename("a/source", "b/target").is_err());
    assert_eq!(root.read("a/source", 100).unwrap(), b"first");
    assert_eq!(root.read("b/target", 100).unwrap(), b"second");
    root.rename("a/source", "b/moved").unwrap();
    assert_eq!(root.read("b/moved", 100).unwrap(), b"first");
}

#[test]
fn windows_profile_inventory_and_shared_lock_pin_the_same_directory() {
    let (_temp, root) = fixture();
    let profile = tobkiri_canon::ids::ProfileId::parse("windows-proof").unwrap();
    let lock = root.profile_lock(&profile, ".revision.lock").unwrap();
    assert_eq!(root.list_profiles().unwrap(), vec![profile.clone()]);
    let second = open_file(
        &lock.directory.directory_capability(),
        ".profile.lock",
        true,
        false,
    )
    .unwrap();
    let mut overlap = unsafe { zeroed() };
    assert_eq!(
        unsafe {
            LockFileEx(
                second.as_raw_handle(),
                LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY,
                0,
                1,
                0,
                &mut overlap,
            )
        },
        0
    );
    assert!(
        fs::rename(
            lock.directory.path().join(".profile.lock"),
            lock.directory.path().join("replacement")
        )
        .is_err()
    );
    drop(second);
    drop(lock);
    root.profile_lock(&profile, ".vault.lock").unwrap();
}

#[test]
fn windows_owner_reopen_preserves_full_identity_and_excludes_another_owner() {
    let (_temp, root) = fixture();
    let capability = root.directory_capability();
    let owner = reopen_directory_owner(&capability).unwrap();
    assert_eq!(
        file_identity(&owner).unwrap(),
        file_identity(&capability).unwrap()
    );
    assert!(reopen_directory_owner(&capability).is_err());
    drop(owner);
    reopen_directory_owner(&capability).unwrap();
}

#[test]
fn windows_metadata_handles_coexist_with_an_open_writer() {
    let (_temp, root) = fixture();
    root.write_atomic("value", b"safe data").unwrap();
    let writer = open_file_native(
        &root.directory_capability(),
        "value",
        FILE_GENERIC_READ | FILE_GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
    )
    .unwrap();
    assert_eq!(root.file_len("value").unwrap(), Some(9));
    assert_eq!(regular_metadata(&writer).unwrap().len, 9);
}

#[test]
fn windows_native_truncate_checks_hardlinks_before_effect() {
    let (temp, root) = fixture();
    root.write_atomic("value", b"original").unwrap();
    fs::hard_link(root.path().join("value"), temp.path().join("alias")).unwrap();
    for disposition in [CREATE_ALWAYS, TRUNCATE_EXISTING] {
        assert!(
            open_file_native(
                &root.directory_capability(),
                "value",
                FILE_GENERIC_READ | FILE_GENERIC_WRITE,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                disposition,
                FILE_ATTRIBUTE_NORMAL
            )
            .is_err()
        );
        assert_eq!(fs::read(temp.path().join("alias")).unwrap(), b"original");
    }
}

#[test]
fn windows_cas_reuses_identical_content_and_rejects_tampered_publication() {
    let (_temp, root) = fixture();
    let cas = crate::Cas::new(root.clone());
    let hash = cas.put(b"original").unwrap();
    assert_eq!(cas.put(b"original").unwrap(), hash);
    root.write_atomic(Path::new("cas/sha256").join(hash.hex()), b"tampered")
        .unwrap();
    assert!(cas.put(b"original").is_err());
}

#[test]
fn windows_atomic_root_write_flushes_the_actual_held_allocation_owner() {
    let (_temp, root) = fixture();
    let profile = tobkiri_canon::ids::ProfileId::parse("windows-proof").unwrap();
    let owner = root.user_owner_lease(&profile).unwrap();
    root.write_atomic("value", b"while owned").unwrap();
    assert_eq!(root.read("value", 100).unwrap(), b"while owned");
    owner.check_live().unwrap();
}

#[test]
fn windows_storage_keys_reject_case_unicode_and_short_name_aliases_before_effect() {
    let (_temp, root) = fixture();
    root.write_atomic("data/value", b"lowercase owner").unwrap();
    for alias in [
        "Data/value",
        "data/Value",
        "dAta/value",
        "dátá/value",
        "data/value~1",
    ] {
        assert!(root.read(alias, 100).is_err());
        assert!(root.write_atomic(alias, b"overwritten").is_err());
        assert!(root.rename(alias, "moved").is_err());
    }
    assert_eq!(root.read("data/value", 100).unwrap(), b"lowercase owner");
    let profile = tobkiri_canon::ids::ProfileId::parse("ProfileCase").unwrap();
    assert!(DataRoot::profile_relative(&profile, "head").is_err());
    assert!(root.profile_lock(&profile, ".revision.lock").is_err());
    assert!(!root.path().join("profiles").exists());
    // Secret names are encrypted-map keys, not filesystem components.
    super::super::name("API_KEY").unwrap();
}

#[test]
fn windows_absolute_host_root_keeps_its_original_case() {
    let temp = tempfile::tempdir().unwrap();
    let root = DataRoot::open(temp.path().join("CaseSensitiveHostLabel")).unwrap();
    root.write_atomic("lowercase", b"data").unwrap();
    assert_eq!(root.read("lowercase", 100).unwrap(), b"data");
}
