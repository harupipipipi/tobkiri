use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use ed25519_dalek::SigningKey;
use std::collections::BTreeMap;
use tobkiri_canon::{CanonValue, Digest, ids::*};
use tobkiri_gate::PermissionSet;
use tobkiri_manifest::*;

fn manifest(id: &str, files: &BTreeMap<String, Vec<u8>>) -> PackManifest {
    PackManifest {
        format: "tobkiri.pack/1".into(),
        id: PackId::parse(id).unwrap(),
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
        files: files
            .iter()
            .map(|(path, b)| FileEntry {
                path: path.clone(),
                sha256: Digest::hash(b),
                size: b.len() as u64,
            })
            .collect(),
    }
}
fn verify(m: &PackManifest, f: &BTreeMap<String, Vec<u8>>) -> Result<VerifiedPack, ManifestError> {
    verify_tpk(&build_tpk(m, f, None)?, &BTreeMap::new())
}
fn shape(minor: u16, params: WitType, result: WitType) -> ShapeDef {
    ShapeDef {
        reference: ShapeRef::parse(&format!("local.contract.query@1.{minor}")).unwrap(),
        kind: ShapeKind::Port,
        cardinality: Cardinality::One,
        functions: BTreeMap::from([(
            OpName::parse("query").unwrap(),
            WitFunction {
                params: vec![("input".into(), params)],
                result: Some(result),
            },
        )]),
        types: BTreeMap::new(),
        streams: BTreeMap::new(),
        callbacks: BTreeMap::new(),
        wit: String::new(),
    }
}
fn record(extra: bool) -> WitType {
    let mut fields = vec![WitField {
        name: "name".into(),
        ty: WitType::String,
    }];
    if extra {
        fields.push(WitField {
            name: "extra".into(),
            ty: WitType::Option(Box::new(WitType::String)),
        });
    }
    WitType::Record(fields)
}

#[test]
fn package_bytes_are_deterministic_and_complete_inventory_is_enforced() {
    let f = BTreeMap::from([("a.txt".into(), b"hello".to_vec())]);
    let m = manifest("local.sample", &f);
    let bytes = build_tpk(&m, &f, None).unwrap();
    assert_eq!(bytes, build_tpk(&m, &f, None).unwrap());
    assert_eq!(
        verify_tpk(&bytes, &BTreeMap::new())
            .unwrap()
            .files
            .materialize_for_pack_build()
            .unwrap(),
        f
    );
    let mut changed = f.clone();
    changed.insert("a.txt".into(), b"HELLO".to_vec());
    assert!(build_tpk(&m, &changed, None).is_err());
    changed.insert("extra".into(), vec![]);
    assert!(build_tpk(&m, &changed, None).is_err());
    let mut corrupted = bytes;
    corrupted[0] ^= 1;
    assert!(verify_tpk(&corrupted, &BTreeMap::new()).is_err());
}
#[test]
fn only_exact_known_core_versions_resolve_aliases_and_permission_imports() {
    let f=BTreeMap::from([
        ("wit/contract.wit".into(),b"package local:contract@1.0.0; interface query { use tobkiri:core/types@1.1.0.{json}; query: func(input: json) -> json; }".to_vec()),
        ("wit/query.shape.json".into(),br#"{"cardinality":"one","streams":{},"callbacks":{}}"#.to_vec()),
    ]);
    let mut m = manifest("local.contract", &f);
    m.shapes.push(ShapeDefinition {
        name: ShapeName::parse("local.contract.query").unwrap(),
        version: ShapeVersion::new(1, 0),
        kind: ShapeKind::Port,
        wit: "wit/contract.wit".into(),
    });
    let p = verify(&m, &f).unwrap();
    assert_eq!(
        p.shapes[0].functions[&OpName::parse("query").unwrap()].params[0].1,
        WitType::Json
    );
    let imports = allowed_imports_for_pack(&p).unwrap();
    assert!(imports.contains("tobkiri:core/types@1.0.0"));
    assert!(imports.contains("tobkiri:core/types@1.1.0"));
    assert!(imports.contains("tobkiri:core/types@1.2.0"));
    assert!(!imports.contains("tobkiri:core/types@1.3.0"));
    assert!(!imports.contains("tobkiri:core/storage@1.1.0"));
    m.permissions.storage.private = true;
    let imports = allowed_imports(&m).unwrap();
    assert!(imports.contains("tobkiri:core/storage@1.0.0"));
    assert!(imports.contains("tobkiri:core/storage@1.1.0"));
    assert!(imports.contains("tobkiri:core/storage@1.2.0"));
    assert!(!imports.contains("tobkiri:core/storage@1.3.0"));
}
#[test]
fn a_shape_document_stays_independent_of_the_worlds_cross_pack_import_closure() {
    let shape = b"package local:profile-probe@1.0.0; interface probe { use tobkiri:core/types@1.2.0.{error}; read: func() -> result<string, error>; }";
    let world = b"package local:profile-probe-component@1.0.0; world pack { include tobkiri:core/pack-minimal@1.2.0; import local:profile-owner/probe@1.0.0; export local:profile-probe/probe@1.0.0; }";
    let f = BTreeMap::from([
        ("wit/probe.wit".into(), shape.to_vec()),
        ("wit/pack.wit".into(), world.to_vec()),
        ("wit/deps/owner.wit".into(), b"package local:profile-owner@1.0.0; interface probe { use tobkiri:core/types@1.2.0.{error}; read: func() -> result<string, error>; }".to_vec()),
        ("wit/probe.shape.json".into(), br#"{"cardinality":"one","streams":{},"callbacks":{}}"#.to_vec()),
    ]);
    let mut m = manifest("local.profile-probe", &f);
    m.shapes.push(ShapeDefinition {
        name: ShapeName::parse("local.profile-probe.probe").unwrap(),
        version: ShapeVersion::new(1, 0),
        kind: ShapeKind::Port,
        wit: "wit/probe.wit".into(),
    });
    m.ports.provides.push(PortDeclaration {
        shape: ShapeName::parse("local.profile-probe.probe").unwrap(),
        version: ShapeVersion::new(1, 0),
        optional: false,
    });
    m.ports.consumes.push(PortDeclaration {
        shape: ShapeName::parse("local.profile-owner.probe").unwrap(),
        version: ShapeVersion::new(1, 0),
        optional: false,
    });
    m.permissions
        .ports
        .insert("local.profile-owner.probe@1.0".into());
    let p = verify(&m, &f).unwrap();
    assert_eq!(p.shapes.len(), 1);
    assert_eq!(p.shapes[0].functions.len(), 1);
    let imports = allowed_imports_for_pack(&p).unwrap();
    assert!(imports.contains("local:profile-owner/probe@1.0.0"));
    assert!(imports.contains("tobkiri:core/types@1.2.0"));
    // A standalone Shape document cannot resolve a world-only foreign Pack.
    // Keeping the world out of that document is part of fixture packaging.
    let mut combined = f;
    combined
        .get_mut("wit/probe.wit")
        .unwrap()
        .extend_from_slice(
            b" world pack { import local:profile-owner/probe@1.0.0; export probe; }",
        );
    assert!(load_shapes(&m, &tobkiri_manifest::ArtifactFiles::capture_for_verification(&combined, &tobkiri_manifest::source::SourceCatalog::new(&tobkiri_gate::CoreControl::default()).unwrap()).unwrap()).is_err());
}

#[test]
fn signed_artifact_requires_real_publisher_binding_and_matching_key() {
    let f = BTreeMap::new();
    let mut m = manifest("acme.sample", &f);
    let signer = SigningKey::from_bytes(&[7; 32]);
    let key = format!(
        "ed25519:{}",
        URL_SAFE_NO_PAD.encode(signer.verifying_key().to_bytes())
    );
    m.publisher_key = Some(key.clone());
    let sig = sign_manifest(&m, &signer).unwrap();
    let bytes = build_tpk(&m, &f, Some(&sig)).unwrap();
    assert!(verify_tpk(&bytes, &BTreeMap::new()).is_err());
    assert!(
        verify_tpk(
            &bytes,
            &BTreeMap::from([("acme".into(), "ed25519:not-a-key".into())])
        )
        .is_err()
    );
    verify_tpk(&bytes, &BTreeMap::from([("acme".into(), key)])).unwrap();
    assert!(sign_manifest(&m, &SigningKey::from_bytes(&[8; 32])).is_err());
}
#[test]
fn unsigned_ids_unknown_fields_duplicates_and_paths_fail_closed() {
    let f = BTreeMap::new();
    assert!(canonical_manifest(&manifest("acme.sample", &f)).is_err());
    let m = manifest("local.sample", &f);
    let mut json = serde_json::to_value(&m).unwrap();
    json["approval"] = true.into();
    assert!(parse_manifest(&serde_json::to_vec(&json).unwrap()).is_err());
    assert!(parse_manifest(br#"{"format":"x","format":"x"}"#).is_err());
    for path in [
        "../escape",
        "/abs",
        "x\\file",
        "C:evil",
        "x/CON.txt",
        "a/",
        "NUL",
    ] {
        let f = BTreeMap::from([(path.into(), vec![])]);
        assert!(
            canonical_manifest(&manifest("local.sample", &f)).is_err(),
            "{path}"
        );
    }
    let f = BTreeMap::from([("A.txt".into(), vec![]), ("a.txt".into(), vec![])]);
    assert!(canonical_manifest(&manifest("local.sample", &f)).is_err());
}
#[test]
fn native_execution_and_initialization_defaults_are_hash_bound() {
    let f = BTreeMap::from([
        ("guest".into(), vec![]),
        ("config.json".into(), br#"{ "z": 1, "a": "hi" }"#.to_vec()),
    ]);
    let mut m = manifest("local.sample", &f);
    m.runtime = RuntimeSpec::Native {
        executable: "guest".into(),
        args: vec![],
        config: Some(RuntimeConfig {
            path: "config.json".into(),
            shape: None,
        }),
    };
    assert!(verify(&m, &f).is_err());
    m.permissions.native = true;
    assert_eq!(
        verify(&m, &f).unwrap().init_config().unwrap(),
        br#"{"a":"hi","z":1}"#
    );
    let mut bad = f;
    bad.insert("config.json".into(), br#"{"number":1.0}"#.to_vec());
    m.files = manifest("local.sample", &bad).files;
    assert!(verify(&m, &bad).is_err());
}
#[test]
fn immutable_store_seed_requires_strict_document_and_declared_hash() {
    let f = BTreeMap::from([("seed.json".into(), b"[]".to_vec())]);
    let mut m = manifest("local.sample", &f);
    m.stores.push(StoreDefinition {
        name: "cache".into(),
        tier: StoreTier::ReadOnly,
        data_shape: ShapeRef::parse("local.sample.data@1.0").unwrap(),
        seed: Some("missing.json".into()),
    });
    assert!(verify(&m, &f).is_err());
    m.stores[0].seed = Some("seed.json".into());
    verify(&m, &f).unwrap();
}
#[test]
fn compatible_trailing_options_project_both_directions() {
    let old = record(false);
    let new = record(true);
    let value = CanonValue::Obj(BTreeMap::from([(
        "name".into(),
        CanonValue::Str("hello".into()),
    )]));
    let projected = project_json(&old, &new, &value).unwrap();
    assert_eq!(projected.get("extra"), Some(&CanonValue::Null));
    assert_eq!(project_json(&new, &old, &projected).unwrap(), value);
    assert_eq!(
        compatibility(
            &shape(0, old.clone(), old.clone()),
            &shape(1, new.clone(), new)
        )
        .unwrap(),
        Compat::MinorCompatible
    );
    let mut changed = shape(1, old.clone(), old);
    changed
        .functions
        .get_mut(&OpName::parse("query").unwrap())
        .unwrap()
        .params[0]
        .1 = WitType::U16;
    assert!(compatibility(&shape(0, WitType::String, WitType::String), &changed).is_err());
}
#[test]
fn enum_minor_extension_is_input_only_and_width_changes_need_adapter() {
    let a = WitType::Enum(vec!["a".into()]);
    let b = WitType::Enum(vec!["a".into(), "b".into()]);
    compatibility(
        &shape(0, a.clone(), WitType::String),
        &shape(1, b.clone(), WitType::String),
    )
    .unwrap();
    assert!(compatibility(&shape(0, WitType::String, a), &shape(1, WitType::String, b)).is_err());
    assert!(project_json(&WitType::F32, &WitType::F64, &encode_f32(1.0).unwrap()).is_err());
    let mut major = shape(0, WitType::U32, WitType::String);
    major.reference.version.major = 2;
    assert_eq!(
        compatibility(&shape(0, WitType::String, WitType::String), &major).unwrap(),
        Compat::NeedsAdapter
    );
}
#[test]
fn typed_json_requires_exact_ranges_keys_and_tagged_payloads() {
    assert!(validate_json(&WitType::U8, &CanonValue::Int(256)).is_err());
    assert!(validate_json(&WitType::U64, &CanonValue::Int(9_007_199_254_740_992)).is_err());
    validate_json(&WitType::U64, &CanonValue::Str(u64::MAX.to_string())).unwrap();
    assert!(validate_json(&WitType::U64, &CanonValue::Str("01".into())).is_err());
    assert!(
        validate_json(
            &record(false),
            &CanonValue::Obj(BTreeMap::from([
                ("name".into(), "x".into()),
                ("extra".into(), CanonValue::Null)
            ]))
        )
        .is_err()
    );
    validate_json(&WitType::Json, &CanonValue::Obj(BTreeMap::new())).unwrap();
    assert!(validate_json(&WitType::Json, &CanonValue::Float(0.5)).is_err());
    let schema = json_schema(&record(true));
    assert_eq!(schema["additionalProperties"], false);
    assert_eq!(schema["required"], serde_json::json!(["name"]));
}
#[test]
fn float_strings_preserve_declared_width_negative_zero_and_extremes() {
    for v in [
        0.0_f32,
        -0.0,
        1.0,
        f32::MAX,
        f32::MIN_POSITIVE,
        f32::from_bits(1),
        f32::from_bits(0xca50890d),
    ] {
        let value = encode_f32(v).unwrap();
        assert_eq!(parse_f32(&value).unwrap().to_bits(), v.to_bits());
    }
    for v in [
        0.0_f64,
        -0.0,
        1.0,
        f64::MAX,
        f64::MIN_POSITIVE,
        f64::from_bits(1),
    ] {
        let value = encode_f64(v).unwrap();
        assert_eq!(parse_f64(&value).unwrap().to_bits(), v.to_bits());
    }
    assert_eq!(encode_f32(-0.0).unwrap().as_str(), Some("-0e0"));
    assert_ne!(
        encode_f32(0.1).unwrap(),
        encode_f64(f64::from(0.1_f32)).unwrap()
    );
    for text in ["1", "1.0e0", "1e+0", "01e0", "NaN", "inf", "-0", "1e999"] {
        assert!(parse_f64(&text.into()).is_err(), "{text}");
    }
    assert!(encode_f32(f32::NAN).is_err());
    assert!(encode_f64(f64::INFINITY).is_err());
    assert!(validate_json(&WitType::F32, &CanonValue::Float(1.0)).is_err());
}
#[test]
fn actual_shape_file_is_resolved_and_type_only_dependency_is_exact() {
    let root =
        std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../packs/local.harness-chat");
    let pack = verify_directory(&root, &BTreeMap::new()).unwrap();
    assert!(pack.shapes.iter().any(|s| s.reference.name.as_str()
        == "local.harness-chat.user-input"
        && s.functions.contains_key(&OpName::parse("pull").unwrap())));
    let mut model = pack.clone();
    model.manifest.ports.consumes.clear();
    model.manifest.ports.provides.push(PortDeclaration {
        shape: ShapeName::parse("local.harness-chat.user-input").unwrap(),
        version: ShapeVersion::new(1, 0),
        optional: false,
    });
    let allowed = allowed_imports_for_pack(&model).unwrap();
    assert!(allowed.contains("local:harness-chat/messages@1.0.0"));
    assert!(!allowed.contains("tobkiri:core/network@1.0.0"));
    assert!(!allowed.contains("local:harness-chat/prompt-builder@1.0.0"));
}
#[cfg(unix)]
#[test]
fn source_admission_rejects_leaf_and_ancestor_symlinks() {
    use std::os::unix::fs::symlink;
    let root = tempfile::tempdir().unwrap();
    let outside = tempfile::tempdir().unwrap();
    std::fs::write(outside.path().join("file"), b"hello").unwrap();
    symlink(outside.path(), root.path().join("child")).unwrap();
    assert!(tobkiri_canon::files::read_relative(root.path(), "child/file", 1024).is_err());
    symlink(outside.path().join("file"), root.path().join("leaf")).unwrap();
    assert!(tobkiri_canon::files::read_relative(root.path(), "leaf", 1024).is_err());
    std::fs::write(root.path().join("real"), b"hello").unwrap();
    assert_eq!(
        tobkiri_canon::files::read_relative(root.path(), "real", 5).unwrap(),
        b"hello"
    );
    assert!(tobkiri_canon::files::read_relative(root.path(), "real", 4).is_err());
}

#[test]
fn hierarchical_names_are_authoritative_and_wit_collisions_fail() {
    let reference = ShapeRef::parse("acme.defaults.ai.provider@1.0").unwrap();
    assert_eq!(reference.name.owner().as_str(), "acme.defaults");
    assert_eq!(reference.name.path(), "ai.provider");
    assert_eq!(
        wit_interface_name(&reference).unwrap(),
        "acme:defaults/ai-provider@1.0.0"
    );
    assert!(ShapeName::parse("acme.defaults.a.b.c.d.e.f.g.h.i").is_err());
    assert!(ShapeName::parse(&format!("acme.defaults.{}", "a".repeat(128))).is_err());
    let mut files = BTreeMap::new();
    for iface in ["ai-provider", "settings"] {
        files.insert(
            format!("wit/{iface}.wit"),
            format!("package local:defaults@1.0.0; interface {iface} {{ run: func(); }}")
                .into_bytes(),
        );
        files.insert(
            format!("wit/{iface}.shape.json"),
            br#"{"cardinality":"one","streams":{},"callbacks":{}}"#.to_vec(),
        );
    }
    let mut m = manifest("local.defaults", &files);
    for (name, iface) in [("ai.provider", "ai-provider"), ("settings", "settings")] {
        m.shapes.push(ShapeDefinition {
            name: ShapeName::parse(&format!("local.defaults.{name}")).unwrap(),
            version: ShapeVersion::new(1, 0),
            kind: ShapeKind::Port,
            wit: format!("wit/{iface}.wit"),
        });
    }
    let admitted = verify(&m, &files).unwrap();
    assert_eq!(admitted.shapes.len(), 2);
    allowed_imports_for_pack(&admitted).unwrap();
    m.shapes.push(ShapeDefinition {
        name: ShapeName::parse("local.defaults.ai-provider").unwrap(),
        version: ShapeVersion::new(2, 0),
        kind: ShapeKind::Port,
        wit: "wit/ai-provider.wit".into(),
    });
    assert!(canonical_manifest(&m).is_err());
}

#[test]
fn all_rust_declared_width_golden_strings_roundtrip_without_numeric_injection() {
    let rows: Vec<serde_json::Value> =
        serde_json::from_str(include_str!("../../../vectors/wit-floats.json")).unwrap();
    assert!(rows.len() >= 1000);
    for row in rows {
        let value = CanonValue::Str(row["expect"].as_str().unwrap().into());
        let bits = u64::from_str_radix(row["bits"].as_str().unwrap(), 16).unwrap();
        if row["width"] == "f32" {
            assert_eq!(encode_f32(f32::from_bits(bits as u32)).unwrap(), value);
            assert_eq!(parse_f32(&value).unwrap().to_bits(), bits as u32);
        } else {
            assert_eq!(encode_f64(f64::from_bits(bits)).unwrap(), value);
            assert_eq!(parse_f64(&value).unwrap().to_bits(), bits);
        }
        tobkiri_canon::encode(&value, tobkiri_canon::Variant::StrictV1).unwrap();
    }
}

#[test]
fn absent_input_metadata_remains_default_deny_without_changing_old_wire_hashes() {
    let files = BTreeMap::new();
    let m = manifest("local.source", &files);
    let bytes = canonical_manifest(&m).unwrap();
    let value: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    assert!(value.get("inputs").is_none());
    assert!(parse_manifest(&bytes).unwrap().inputs.is_empty());
}

#[test]
fn input_metadata_requires_an_exact_provided_view_and_no_duplicate_entries() {
    let mut m = manifest("local.source", &BTreeMap::new());
    let input = InputDeclaration {
        exact_input_view: ShapeRef::parse("local.contract.input@1.1").unwrap(),
        operation: OpName::parse("receive").unwrap(),
    };
    m.inputs = vec![input.clone()];
    m.ports.provides = vec![PortDeclaration {
        shape: input.exact_input_view.name.clone(),
        version: ShapeVersion::new(1, 0),
        optional: false,
    }];
    assert!(canonical_manifest(&m).is_err());
    m.ports.provides[0].version = input.exact_input_view.version;
    assert!(canonical_manifest(&m).is_ok());
    m.inputs.push(input);
    assert!(canonical_manifest(&m).is_err());
}

#[test]
fn input_operation_must_exist_in_the_exact_authoritative_port_view() {
    let mut m = manifest("local.source", &BTreeMap::new());
    let mut view = shape(0, WitType::String, WitType::String);
    let declaration = InputDeclaration {
        exact_input_view: view.reference.clone(),
        operation: OpName::parse("receive").unwrap(),
    };
    m.ports.provides = vec![PortDeclaration {
        shape: view.reference.name.clone(),
        version: view.reference.version,
        optional: false,
    }];
    m.inputs = vec![declaration.clone()];
    assert!(input_function(&m, &declaration, &view).is_err());
    view.functions.insert(
        declaration.operation.clone(),
        WitFunction {
            params: vec![],
            result: Some(WitType::String),
        },
    );
    assert!(input_function(&m, &declaration, &view).is_ok());
    view.reference.version = ShapeVersion::new(1, 1);
    assert!(input_function(&m, &declaration, &view).is_err());
    view.reference = declaration.exact_input_view.clone();
    view.kind = ShapeKind::Data;
    assert!(input_function(&m, &declaration, &view).is_err());
}
