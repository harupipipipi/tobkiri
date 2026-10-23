#[cfg(unix)]
use std::collections::BTreeMap;
use tobkiri_canon::Digest;
use tobkiri_gate::PermissionSet;
use tobkiri_manifest::{FileEntry, PackId, PackManifest, Ports, RuntimeSpec, build_tpk};
#[cfg(unix)]
use tobkiri_manifest::{canonical_manifest, verify_directory, verify_tpk};

fn manifest(files: Vec<FileEntry>) -> PackManifest {
    PackManifest {
        format: "tobkiri.pack/1".into(),
        id: PackId::parse("local.directory-bounds").unwrap(),
        version: "1.0.0".into(),
        publisher_key: None,
        runtime: RuntimeSpec::Data,
        shapes: vec![],
        ports: Ports::default(),
        inputs: vec![],
        flows: vec![],
        overlays: vec![],
        stores: vec![],
        permissions: PermissionSet::default(),
        ui: None,
        files,
    }
}

fn entry(index: usize, size: u64) -> FileEntry {
    FileEntry {
        path: format!("missing-{index:05}.bin"),
        sha256: Digest::hash(&[]),
        size,
    }
}

#[cfg(unix)]
#[test]
fn aggregate_declarations_fail_before_reading_any_artifact() {
    let root = tempfile::tempdir().unwrap();
    let document = manifest(
        (0..4)
            .map(|index| entry(index, 128 * 1024 * 1024))
            .collect(),
    );
    std::fs::write(
        root.path().join("pack.json"),
        canonical_manifest(&document).unwrap(),
    )
    .unwrap();
    // No artifact exists. The aggregate error must precede an attempted read.
    let error = verify_directory(root.path(), &BTreeMap::new()).unwrap_err();
    assert!(error.to_string().contains("package exceeds 512 MiB"));
}

#[test]
fn public_build_and_directory_count_the_manifest_entry_before_collecting_artifacts() {
    let document = manifest((0..16_384).map(|index| entry(index, 0)).collect());
    let files = document
        .files
        .iter()
        .map(|entry| (entry.path.clone(), Vec::new()))
        .collect();
    let error = build_tpk(&document, &files, None).unwrap_err();
    assert!(error.to_string().contains("bounded regular ustar entries"));
    #[cfg(unix)]
    {
        let root = tempfile::tempdir().unwrap();
        let raw = canonical_manifest(&document).unwrap();
        assert!(raw.len() < tobkiri_canon::MAX_BYTES);
        std::fs::write(root.path().join("pack.json"), raw).unwrap();
        // Only the manifest exists: no thousands of disk files are created.
        let error = verify_directory(root.path(), &BTreeMap::new()).unwrap_err();
        assert!(error.to_string().contains("bounded regular ustar entries"));
    }
}

#[cfg(unix)]
#[test]
fn declared_member_size_caps_the_pinned_read_before_content_allocation() {
    let root = tempfile::tempdir().unwrap();
    let document = manifest(vec![entry(0, 0)]);
    std::fs::write(
        root.path().join("pack.json"),
        canonical_manifest(&document).unwrap(),
    )
    .unwrap();
    let file = std::fs::File::create(root.path().join(&document.files[0].path)).unwrap();
    file.set_len(64 * 1024).unwrap();
    // A small sparse metadata fixture, not a large allocated artifact.
    let error = verify_directory(root.path(), &BTreeMap::new()).unwrap_err();
    assert!(error.to_string().contains("not a bounded regular file"));
}

#[cfg(unix)]
#[test]
fn good_directory_and_archive_keep_the_original_ustar_bytes_and_pin() {
    let files = BTreeMap::from([("a.txt".into(), b"hello".to_vec())]);
    let document = manifest(vec![FileEntry {
        path: "a.txt".into(),
        sha256: Digest::hash(b"hello"),
        size: 5,
    }]);
    let raw = canonical_manifest(&document).unwrap();
    let root = tempfile::tempdir().unwrap();
    std::fs::write(root.path().join("pack.json"), &raw).unwrap();
    std::fs::write(root.path().join("a.txt"), b"hello").unwrap();

    // Reproduce the unchanged pre-budget writer recipe independently.
    let mut original_entries = files.clone();
    original_entries.insert("pack.json".into(), raw);
    let mut original = tar::Builder::new(Vec::new());
    for (path, content) in original_entries {
        let mut header = tar::Header::new_ustar();
        header.set_path(path).unwrap();
        header.set_size(content.len() as u64);
        header.set_mode(0o644);
        header.set_uid(0);
        header.set_gid(0);
        header.set_mtime(0);
        header.set_cksum();
        original.append(&header, content.as_slice()).unwrap();
    }
    original.finish().unwrap();
    let original = original.into_inner().unwrap();
    let current = build_tpk(&document, &files, None).unwrap();
    assert_eq!(current, original);
    let directory = verify_directory(root.path(), &BTreeMap::new()).unwrap();
    let archive = verify_tpk(&current, &BTreeMap::new()).unwrap();
    assert_eq!(directory.hash, archive.hash);
    assert_eq!(directory.files.materialize_for_pack_build().unwrap(), files);
    assert_eq!(archive.files.materialize_for_pack_build().unwrap(), files);
}
