#![cfg(unix)]

use std::{cell::Cell, fs, os::unix::fs::PermissionsExt};
use tobkiri_canon::{
    CanonError,
    files::{read_relative, read_relative_with},
};

fn io(error: std::io::Error) -> CanonError {
    CanonError::Invalid(error.to_string())
}

#[test]
fn callback_can_inspect_without_materializing_the_whole_file() {
    let root = tempfile::tempdir().unwrap();
    fs::write(root.path().join("blob"), b"abcdefgh").unwrap();
    let prefix = read_relative_with(root.path(), "blob", 8, |reader, size| {
        assert_eq!(size, 8);
        let mut prefix = [0u8; 2];
        reader.read_exact(&mut prefix).map_err(io)?;
        Ok(prefix)
    })
    .unwrap();
    assert_eq!(prefix, *b"ab");
    assert_eq!(read_relative(root.path(), "blob", 8).unwrap(), b"abcdefgh");
}

#[test]
fn opened_directory_and_file_survive_source_path_replacement() {
    let parent = tempfile::tempdir().unwrap();
    let root = parent.path().join("source");
    let moved = parent.path().join("moved");
    fs::create_dir(&root).unwrap();
    fs::write(root.join("blob"), b"original").unwrap();
    let bytes = read_relative_with(&root, "blob", 8, |reader, _| {
        fs::rename(&root, &moved).map_err(io)?;
        fs::create_dir(&root).map_err(io)?;
        fs::write(root.join("blob"), b"replaced").map_err(io)?;
        let mut bytes = Vec::new();
        reader.read_to_end(&mut bytes).map_err(io)?;
        Ok(bytes)
    })
    .unwrap();
    assert_eq!(bytes, b"original");
}

#[test]
fn same_inode_changes_are_rejected_even_after_callback_returns_a_result() {
    let root = tempfile::tempdir().unwrap();
    let path = root.path().join("blob");
    fs::write(&path, b"original").unwrap();
    assert!(
        read_relative_with(root.path(), "blob", 8, |_, _| {
            fs::write(&path, b"modified").map_err(io)?;
            Ok(())
        })
        .is_err()
    );
}

#[test]
fn early_return_cannot_skip_growth_or_truncation_detection() {
    for changed in [b"longer content".as_slice(), b"short".as_slice()] {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("blob");
        fs::write(&path, b"original").unwrap();
        assert!(
            read_relative_with(root.path(), "blob", 32, |reader, _| {
                let mut first = [0u8; 1];
                reader.read_exact(&mut first).map_err(io)?;
                fs::write(&path, changed).map_err(io)?;
                Ok(first)
            })
            .is_err()
        );
    }
}

#[test]
fn zero_byte_limit_accepts_only_an_empty_regular_file() {
    let root = tempfile::tempdir().unwrap();
    fs::write(root.path().join("empty"), b"").unwrap();
    assert!(read_relative(root.path(), "empty", 0).unwrap().is_empty());
    assert!(
        read_relative_with(root.path(), "empty", 0, |_, size| {
            assert_eq!(size, 0);
            fs::write(root.path().join("empty"), b"x").map_err(io)?;
            Ok(())
        })
        .is_err()
    );
}

#[test]
fn mode_changes_are_rejected_without_payload_or_length_changes() {
    let root = tempfile::tempdir().unwrap();
    let path = root.path().join("blob");
    fs::write(&path, b"original").unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
    assert!(
        read_relative_with(root.path(), "blob", 8, |_, _| {
            fs::set_permissions(&path, fs::Permissions::from_mode(0o400)).map_err(io)?;
            Ok(())
        })
        .is_err()
    );
}

#[test]
fn length_and_symlink_checks_precede_the_callback() {
    let root = tempfile::tempdir().unwrap();
    fs::write(root.path().join("blob"), b"original").unwrap();
    fs::create_dir(root.path().join("nested")).unwrap();
    std::os::unix::fs::symlink("blob", root.path().join("alias")).unwrap();
    std::os::unix::fs::symlink("nested", root.path().join("directory-alias")).unwrap();
    let called = Cell::new(false);
    for (path, limit) in [
        ("blob", 7),
        ("alias", 8),
        ("directory-alias/blob", 8),
        ("../blob", 8),
    ] {
        assert!(
            read_relative_with(root.path(), path, limit, |_, _| {
                called.set(true);
                Ok(())
            })
            .is_err()
        );
        assert!(!called.get());
    }
}
