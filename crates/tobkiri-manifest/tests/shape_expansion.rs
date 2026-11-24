use std::collections::BTreeMap;
use tobkiri_canon::{Digest, ids::*};
use tobkiri_gate::PermissionSet;
use tobkiri_manifest::*;

fn input(wit: &str, sidecar: &str) -> (PackManifest, BTreeMap<String, Vec<u8>>) {
    let files: BTreeMap<String, Vec<u8>> = BTreeMap::from([
        ("wit/check.wit".into(), wit.as_bytes().to_vec()),
        ("wit/check.shape.json".into(), sidecar.as_bytes().to_vec()),
    ]);
    let manifest = PackManifest {
        format: "tobkiri.pack/1".into(),
        id: PackId::parse("local.bounded").unwrap(),
        version: "1.0.0".into(),
        publisher_key: None,
        runtime: RuntimeSpec::Data,
        shapes: vec![ShapeDefinition {
            name: ShapeName::parse("local.bounded.check").unwrap(),
            version: ShapeVersion::new(1, 0),
            kind: ShapeKind::Port,
            wit: "wit/check.wit".into(),
        }],
        ports: Ports::default(),
        inputs: vec![],
        flows: vec![],
        overlays: vec![],
        stores: vec![],
        permissions: PermissionSet::default(),
        ui: None,
        files: files
            .iter()
            .map(|(path, bytes)| FileEntry {
                path: path.clone(),
                sha256: Digest::hash(bytes),
                size: bytes.len() as u64,
            })
            .collect(),
    };
    (manifest, files)
}

fn hostile_archive(manifest: &PackManifest, mut files: BTreeMap<String, Vec<u8>>) -> Vec<u8> {
    files.insert("pack.json".into(), canonical_manifest(manifest).unwrap());
    let mut builder = tar::Builder::new(Vec::new());
    for (name, bytes) in files {
        let mut header = tar::Header::new_ustar();
        header.set_path(name).unwrap();
        header.set_size(bytes.len() as u64);
        header.set_mode(0o644);
        header.set_uid(0);
        header.set_gid(0);
        header.set_mtime(0);
        header.set_cksum();
        builder.append(&header, bytes.as_slice()).unwrap();
    }
    builder.finish().unwrap();
    builder.into_inner().unwrap()
}

fn assert_rejected(wit: &str, sidecar: &str) {
    assert_rejected_with(wit, sidecar, "bounded WIT type");
}

fn assert_rejected_with(wit: &str, sidecar: &str, reason: &str) {
    let (manifest, files) = input(wit, sidecar);
    let error = load_shapes(
        &manifest,
        &tobkiri_manifest::ArtifactFiles::capture_for_verification(
            &files,
            &tobkiri_manifest::source::SourceCatalog::new(&tobkiri_gate::CoreControl::default())
                .unwrap(),
        )
        .unwrap(),
    )
    .unwrap_err();
    assert!(error.to_string().contains(reason), "{error}");
    // Exercise the actual hash-inventory-verified untrusted Pack reader too;
    // the package is tiny and never expands into its advertised million nodes.
    let archive = hostile_archive(&manifest, files);
    assert!(archive.len() < wit.len() + sidecar.len() + 8192);
    let error = verify_tpk(&archive, &BTreeMap::new()).unwrap_err();
    assert!(error.to_string().contains(reason), "{error}");
}

fn aliases(last: usize, branch: bool) -> String {
    let mut text = "type a0 = string;".to_owned();
    for index in 1..=last {
        let previous = index - 1;
        text.push_str(&if branch {
            format!("type a{index} = tuple<a{previous}, a{previous}>;")
        } else {
            format!("type a{index} = a{previous};")
        });
    }
    text
}

const SIDECAR: &str = r#"{"cardinality":"one","streams":{},"callbacks":{}}"#;

#[test]
fn small_alias_bombs_are_rejected_in_arguments_results_and_exposed_types() {
    for signature in [
        "read: func(input: a24) -> string;",
        "read: func() -> a24;",
        // Public type definitions are converted even when not used by a call.
        "read: func() -> string;",
    ] {
        let wit = format!(
            "package local:bounded@1.0.0; interface check {{ {} {signature} }}",
            aliases(24, true),
        );
        assert!(wit.len() < 2048);
        assert_rejected(&wit, SIDECAR);
    }
}

#[test]
fn stream_and_callback_sidecar_types_share_the_same_expansion_budget() {
    let wit = format!(
        "package local:bounded@1.0.0; interface events {{ {} }} interface check {{ read: func() -> string; }}",
        aliases(24, true),
    );
    for sidecar in [
        r#"{"cardinality":"one","streams":{"read":"a24"},"callbacks":{}}"#,
        r#"{"cardinality":"one","streams":{},"callbacks":{"read":"a24"}}"#,
    ] {
        assert_rejected(&wit, sidecar);
    }
}

#[test]
fn deep_alias_chains_fail_before_recursive_conversion_and_small_aliases_still_load() {
    let wit = format!(
        "package local:bounded@1.0.0; interface check {{ {} read: func() -> a70; }}",
        aliases(70, false),
    );
    assert_rejected(&wit, SIDECAR);
    let wit = format!(
        "package local:bounded@1.0.0; interface check {{ {} read: func(input: a5) -> a5; }}",
        aliases(5, true),
    );
    let (manifest, files) = input(&wit, SIDECAR);
    let shapes = load_shapes(
        &manifest,
        &tobkiri_manifest::ArtifactFiles::capture_for_verification(
            &files,
            &tobkiri_manifest::source::SourceCatalog::new(&tobkiri_gate::CoreControl::default())
                .unwrap(),
        )
        .unwrap(),
    )
    .unwrap();
    let function = &shapes[0].functions[&OpName::parse("read").unwrap()];
    assert_eq!(function.params[0].1, function.result.clone().unwrap());
    let bytes = build_tpk(&manifest, &files, None).unwrap();
    assert_eq!(verify_tpk(&bytes, &BTreeMap::new()).unwrap().shapes, shapes,);
}

#[test]
fn recursive_type_and_package_syntax_is_rejected_before_parser_ast_construction() {
    for constructor in ["list", "tuple"] {
        let wit = format!(
            "package local:bounded@1.0.0; interface check {{ read: func() -> {}string{}; }}",
            format!("{constructor}<").repeat(128),
            ">".repeat(128),
        );
        assert!(wit.len() < 2048);
        assert_rejected_with(&wit, SIDECAR, "bounded WIT syntax depth");
    }
    let mut wit = String::from("package local:bounded@1.0.0;");
    for index in 0..128 {
        wit.push_str(&format!("package local:p{index}@1.0.0 {{"));
    }
    wit.push_str("interface check { read: func() -> string; }");
    wit.push_str(&"}".repeat(128));
    assert!(wit.len() < 8192);
    assert_rejected_with(&wit, SIDECAR, "bounded WIT syntax depth");
}

#[test]
fn malformed_delimiters_and_excessive_token_upper_bound_fail_closed() {
    for result in ["list<string}", "string>", "tuple<string)"] {
        let wit = format!(
            "package local:bounded@1.0.0; interface check {{ read: func() -> {result}; }}",
        );
        assert_rejected_with(&wit, SIDECAR, "unbalanced WIT syntax delimiter");
    }
    let wit = format!(
        "package local:bounded@1.0.0; interface check {{ record payload {{ {}: string }} read: func() -> string; }}",
        "a".repeat(262_144),
    );
    assert_rejected_with(&wit, SIDECAR, "bounded WIT syntax work");
}

#[test]
fn comments_and_function_arrows_do_not_consume_syntax_depth() {
    let comment = format!("{} >}}) <{{( \" ' {}", "/*".repeat(96), "*/".repeat(96),);
    let wit = format!(
        "package local:bounded@1.0.0; // {}\ninterface check {{ {comment} read: func(input: tuple<list<string>, option<u32>>) -> result<list<string>, string>; }}",
        "<({>".repeat(200),
    );
    let (manifest, files) = input(&wit, SIDECAR);
    let bytes = build_tpk(&manifest, &files, None).unwrap();
    let pack = verify_tpk(&bytes, &BTreeMap::new()).unwrap();
    assert!(
        pack.shapes[0]
            .functions
            .contains_key(&OpName::parse("read").unwrap())
    );
    assert!(
        core_error_result_operations(&pack.shapes[0])
            .unwrap()
            .is_empty()
    );
    allowed_imports_for_pack(&pack).unwrap();
}

#[test]
fn undeclared_wit_dependencies_are_preflighted_before_import_closure_parsing() {
    let (mut manifest, mut files) = input(
        "package local:bounded@1.0.0; interface check { read: func() -> string; }",
        SIDECAR,
    );
    let hostile = format!(
        "package local:extra@1.0.0; interface extra {{ type value = {}string{}; }}",
        "list<".repeat(128),
        ">".repeat(128),
    )
    .into_bytes();
    manifest.files.push(FileEntry {
        path: "wit/extra.wit".into(),
        sha256: Digest::hash(&hostile),
        size: hostile.len() as u64,
    });
    files.insert("wit/extra.wit".into(), hostile);
    let bytes = build_tpk(&manifest, &files, None).unwrap();
    let pack = verify_tpk(&bytes, &BTreeMap::new()).unwrap();
    let error = allowed_imports_for_pack(&pack).unwrap_err();
    assert!(error.to_string().contains("bounded WIT syntax depth"));
}
