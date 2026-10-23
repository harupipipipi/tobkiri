//! Actual descriptor-bound directory scans; no Profile activation authority.
use super::*;
use std::os::unix::fs::{DirBuilderExt, symlink};

fn directory(path: &Path) {
    std::fs::DirBuilder::new().mode(0o700).create(path).unwrap();
}

#[test]
fn absent_and_empty_inventory_are_read_only_even_with_stale_errno() {
    let temp = tempfile::tempdir().unwrap();
    let root = DataRoot::open(temp.path().canonicalize().unwrap()).unwrap();
    assert!(root.list_profiles_bounded(0).unwrap().is_empty());
    assert!(!root.path().join("profiles").exists());
    directory(&root.path().join("profiles"));
    errno::set_errno(errno::Errno(libc::EIO));
    assert!(root.list_profiles_bounded(0).unwrap().is_empty());
    assert_eq!(
        std::fs::read_dir(root.path().join("profiles"))
            .unwrap()
            .count(),
        0
    );
}

#[test]
fn bounded_scan_keeps_the_exact_limit_sorted_and_never_returns_a_partial_list() {
    let temp = tempfile::tempdir().unwrap();
    let root = DataRoot::open(temp.path().canonicalize().unwrap()).unwrap();
    directory(&root.path().join("profiles"));
    for id in ["prof_z", "defaults", "prof_a"] {
        directory(&root.path().join("profiles").join(id));
    }
    let listed = root.list_profiles_bounded(3).unwrap();
    assert_eq!(
        listed.iter().map(ProfileId::as_str).collect::<Vec<_>>(),
        ["defaults", "prof_a", "prof_z"]
    );
    assert!(matches!(
        root.list_profiles_bounded(2),
        Err(StoreError::Quota)
    ));
    assert!(matches!(
        root.list_profiles_bounded(0),
        Err(StoreError::Quota)
    ));
    assert_eq!(root.list_profiles().unwrap(), listed);
    for id in listed {
        assert_eq!(
            std::fs::read_dir(root.path().join("profiles").join(id.as_str()))
                .unwrap()
                .count(),
            0
        );
    }
}

#[test]
fn inventory_uses_the_original_root_descriptor_after_path_replacement() {
    let temp = tempfile::tempdir().unwrap();
    let physical = temp.path().canonicalize().unwrap();
    let original = physical.join("data");
    let root = DataRoot::open(&original).unwrap();
    directory(&original.join("profiles"));
    directory(&original.join("profiles/defaults"));
    std::fs::rename(&original, physical.join("moved")).unwrap();
    directory(&original);
    directory(&original.join("profiles"));
    directory(&original.join("profiles/prof_replacement"));
    assert_eq!(
        root.list_profiles_bounded(1).unwrap(),
        [ProfileId::parse("defaults").unwrap()]
    );
}

#[test]
fn profile_symlink_and_non_directory_entries_fail_the_complete_scan() {
    let temp = tempfile::tempdir().unwrap();
    let physical = temp.path().canonicalize().unwrap();
    let root = DataRoot::open(physical.join("data")).unwrap();
    directory(&root.path().join("profiles"));
    directory(&physical.join("outside"));
    let entry = root.path().join("profiles/prof_link");
    symlink(physical.join("outside"), &entry).unwrap();
    assert!(root.list_profiles_bounded(8).is_err());
    std::fs::remove_file(&entry).unwrap();
    std::fs::write(&entry, b"not a Profile directory").unwrap();
    assert!(root.list_profiles_bounded(8).is_err());
}
