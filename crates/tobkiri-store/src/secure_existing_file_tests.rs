use super::*;
use std::{fs, os::unix::fs::symlink};

#[test]
fn absent_source_paths_do_not_create_directories_or_files() {
    let temp = tempfile::tempdir().unwrap();
    let parent = temp.path().canonicalize().unwrap();
    assert!(matches!(
        DataRoot::open_existing(parent.join("missing/nested")),
        Err(StoreError::NotFound)
    ));
    assert!(!parent.join("missing").exists());

    let root = DataRoot::open_existing(&parent).unwrap();
    for source in ["missing", "missing/nested"] {
        assert!(matches!(
            root.read_existing_owned(source, 8),
            Err(StoreError::NotFound)
        ));
    }
    assert_eq!(fs::read_dir(parent).unwrap().count(), 0);
}

#[test]
fn each_read_observes_the_current_file_within_the_pinned_directory() {
    let temp = tempfile::tempdir().unwrap();
    let parent = temp.path().canonicalize().unwrap();
    let original = parent.join("root");
    fs::create_dir(&original).unwrap();
    fs::write(original.join("source"), b"first").unwrap();
    let root = DataRoot::open_existing(&original).unwrap();
    assert_eq!(root.read_existing_owned("source", 5).unwrap(), b"first");
    assert!(matches!(
        root.read_existing_owned("source", 4),
        Err(StoreError::Quota)
    ));

    let pinned = parent.join("pinned");
    fs::rename(&original, &pinned).unwrap();
    fs::create_dir(&original).unwrap();
    fs::write(original.join("source"), b"outside").unwrap();
    fs::write(pinned.join("new"), b"next").unwrap();
    fs::rename(pinned.join("new"), pinned.join("source")).unwrap();
    assert_eq!(root.read_existing_owned("source", 4).unwrap(), b"next");

    fs::write(pinned.join("source"), []).unwrap();
    assert!(root.read_existing_owned("source", 0).unwrap().is_empty());
}

#[test]
fn links_fifos_and_directories_cannot_be_read_as_source_files() {
    let temp = tempfile::tempdir().unwrap();
    let parent = temp.path().canonicalize().unwrap();
    let root = DataRoot::open_existing(&parent).unwrap();
    fs::write(parent.join("source"), b"value").unwrap();
    fs::hard_link(parent.join("source"), parent.join("alias")).unwrap();
    symlink("missing", parent.join("dangling")).unwrap();
    symlink(".", parent.join("directory-link")).unwrap();
    fs::create_dir(parent.join("directory")).unwrap();
    let fifo = cstring(parent.join("pipe").to_str().unwrap()).unwrap();
    assert_eq!(unsafe { libc::mkfifo(fifo.as_ptr(), 0o600) }, 0);

    for source in ["source", "alias", "dangling", "directory", "pipe"] {
        assert!(matches!(
            root.read_existing_owned(source, 8),
            Err(StoreError::Denied(_))
        ));
    }
    assert!(matches!(
        root.read_existing_owned("directory-link/source", 8),
        Err(StoreError::Io(_) | StoreError::Denied(_))
    ));
    assert!(matches!(
        DataRoot::open_existing(parent.join("dangling")),
        Err(StoreError::Io(_) | StoreError::Denied(_))
    ));
}
