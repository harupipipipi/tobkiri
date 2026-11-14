#![cfg(unix)]

use serde_json::{Value, json};
use std::{
    fs,
    os::unix::fs::symlink,
    path::Path,
    process::{Command, Output},
};

fn cli(root: &Path, args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_tobkiri"))
        .current_dir(root)
        .arg("pack")
        .args(args)
        .output()
        .unwrap()
}

fn success(output: Output) -> Vec<u8> {
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    output.stdout
}

fn draft(files: Value) -> Value {
    json!({
        "format": "tobkiri.pack/1",
        "id": "local.manifest-example",
        "version": "1.0.0",
        "publisher_key": null,
        "runtime": {"kind": "data"},
        "shapes": [],
        "ports": {"provides": [], "consumes": [], "untyped": []},
        "flows": [],
        "overlays": [],
        "stores": [],
        "permissions": {"storage": {"private": true}},
        "ui": null,
        "files": files
    })
}

fn write_draft(root: &Path, document: &Value) {
    fs::write(
        root.join("draft.json"),
        serde_json::to_vec(document).unwrap(),
    )
    .unwrap();
}

#[test]
fn generated_manifest_packages_and_verifies_only_the_declared_bytes() {
    let work = tempfile::tempdir().unwrap();
    let root = work.path();
    let stage = root.join("stage");
    fs::create_dir_all(stage.join("assets")).unwrap();
    fs::write(stage.join("assets/a.txt"), b"abc").unwrap();
    fs::write(stage.join("z.txt"), b"").unwrap();
    fs::write(stage.join(".env"), b"not a Pack artifact").unwrap();
    write_draft(root, &draft(json!(["z.txt", "assets/a.txt"])));
    let manifest = success(cli(
        root,
        &["manifest", "stage", "--template", "draft.json"],
    ));
    let parsed: Value = serde_json::from_slice(&manifest).unwrap();
    assert_eq!(
        parsed["files"],
        json!([
            {"path": "assets/a.txt", "sha256": "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", "size": 3},
            {"path": "z.txt", "sha256": "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "size": 0}
        ])
    );
    assert_eq!(
        parsed["permissions"],
        json!({
            "network": {"origins": [], "user_origins": null}, "fs": [], "proc": [],
            "pty": false, "secrets": [],
            "storage": {"private": true, "public_read": false, "public_write": false},
            "native": false, "cross_profile_storage": false,
            "audit_read": false, "audit_append": false, "host_io": [], "ports": [],
            "mediator": false,
            "wasm": {"fuel_per_call": 0, "memory_mib": 0, "wall_ms": 0,
                "table_elements": 0, "output_bytes": 0},
            "loops": {"max_iterations": 0, "max_wall_ms": 0, "max_cost_units": 0}
        })
    );

    write_draft(root, &draft(json!(["assets/a.txt", "z.txt"])));
    let create = [
        "manifest",
        "stage",
        "--template",
        "draft.json",
        "--output",
        "stage/pack.json",
    ];
    success(cli(root, &create));
    assert!(!cli(root, &create).status.success());
    assert_eq!(fs::read(stage.join("pack.json")).unwrap(), manifest);
    success(cli(root, &["verify", "stage"]));
    success(cli(root, &["build", "stage", "--output", "example.tpk"]));
    success(cli(root, &["verify", "example.tpk"]));

    fs::write(stage.join("assets/a.txt"), b"ABC").unwrap();
    assert!(!cli(root, &["verify", "stage"]).status.success());
}

#[test]
fn invalid_drafts_and_unsafe_files_leave_no_new_manifest() {
    let work = tempfile::tempdir().unwrap();
    let root = work.path();
    let stage = root.join("stage");
    fs::create_dir_all(stage.join("directory")).unwrap();
    fs::write(stage.join("safe"), b"payload").unwrap();
    fs::write(root.join("outside"), b"external payload").unwrap();
    symlink(root.join("outside"), stage.join("link")).unwrap();
    symlink(root, stage.join("linked-directory")).unwrap();

    let mut cases = vec![
        (draft(json!(["safe", "SAFE"])), "duplicate"),
        (draft(json!(["../outside"])), "unsafe"),
        (draft(json!(["PACK.JSON"])), "reserved"),
        (draft(json!([{"path": "safe", "size": 7}])), "without hash"),
        (draft(json!(["missing"])), "cannot inventory"),
        (draft(json!(["link"])), "cannot inventory"),
        (
            draft(json!(["linked-directory/outside"])),
            "cannot inventory",
        ),
        (draft(json!(["directory"])), "cannot inventory"),
    ];
    let mut missing_permissions = draft(json!(["safe"]));
    missing_permissions
        .as_object_mut()
        .unwrap()
        .remove("permissions");
    cases.push((missing_permissions, "permissions object"));
    for (document, error) in cases {
        write_draft(root, &document);
        let output = cli(
            root,
            &[
                "manifest",
                "stage",
                "--template",
                "draft.json",
                "--output",
                "stage/pack.json",
            ],
        );
        assert!(!output.status.success(), "accepted {document}");
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(stderr.contains(error), "expected {error:?}: {stderr}");
        assert!(!stage.join("pack.json").exists());
    }
}
