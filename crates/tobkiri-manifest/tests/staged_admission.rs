#![cfg(unix)]

use std::{collections::BTreeMap, fs};
use tobkiri_canon::Digest;
use tobkiri_gate::{CoreControl, CoreControlLimits, PermissionSet};
use tobkiri_manifest::{
    FileEntry, PackId, PackManifest, Ports, RuntimeSpec, canonical_manifest, source::SourceCatalog,
    verify_directory_in_catalog,
};

#[test]
fn admission_retains_a_large_payload_on_disk_with_one_funded_immutable_catalog() {
    let root = tempfile::tempdir().unwrap();
    let payload = vec![42; 1024 * 1024];
    fs::write(root.path().join("payload.bin"), &payload).unwrap();
    let manifest = PackManifest {
        format: "tobkiri.pack/1".into(),
        id: PackId::parse("local.staged-source").unwrap(),
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
        files: vec![FileEntry {
            path: "payload.bin".into(),
            sha256: Digest::hash(&payload),
            size: payload.len() as u64,
        }],
    };
    drop(payload);
    fs::write(
        root.path().join("pack.json"),
        canonical_manifest(&manifest).unwrap(),
    )
    .unwrap();
    let control = CoreControl::new(CoreControlLimits {
        total_bytes: 512 * 1024,
        ..Default::default()
    })
    .unwrap();
    let catalog = SourceCatalog::new(&control).unwrap();
    let pack = verify_directory_in_catalog(root.path(), &BTreeMap::new(), &catalog).unwrap();
    let used = control.usage();
    let alias = pack.clone();
    assert_eq!(control.usage(), used);
    assert!(
        pack.files["payload.bin"].read().is_err(),
        "the whole payload cannot bypass the same memory account"
    );
    assert_eq!(control.usage(), used);
    fs::write(root.path().join("payload.bin"), b"replaced source").unwrap();
    alias.files["payload.bin"]
        .inspect(|reader| {
            let mut prefix = [0; 8];
            reader.read_exact(&mut prefix)?;
            assert_eq!(prefix, [42; 8]);
            Ok(())
        })
        .unwrap();
    assert_eq!(control.usage(), used);
    drop(pack);
    drop(alias);
    drop(catalog);
    assert_eq!(control.usage().bytes, 0);
}
