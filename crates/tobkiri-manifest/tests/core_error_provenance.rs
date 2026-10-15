use std::collections::{BTreeMap, BTreeSet};
use tobkiri_canon::{
    Digest,
    ids::{OpName, PackId, ShapeName, ShapeVersion},
};
use tobkiri_gate::PermissionSet;
use tobkiri_manifest::{
    FileEntry, PackManifest, PortDeclaration, Ports, RuntimeSpec, ShapeDef, ShapeDefinition,
    ShapeKind, VerifiedPack, WitBindingDirection, WitType, allowed_imports_for_pack, build_tpk,
    core_error_result_operations, inspect_pack_wit, load_shapes, verify_tpk,
};

fn verified_shape(version: &str) -> ShapeDef {
    let wit = format!(
        r#"package local:error-contract@1.0.0;
interface check {{
  use tobkiri:core/types@{version}.{{error, error-code}};
  type first-alias = error;
  type second-alias = first-alias;
  type aliased-result = result<string, second-alias>;
  record lookalike {{ code: error-code, message: string }}
  direct: func() -> result<string, error>;
  aliases: func() -> aliased-result;
  copied: func() -> result<string, lookalike>;
  nested: func() -> result<string, option<error>>;
  listed: func() -> result<string, list<error>>;
  ordinary: func() -> error;
}}"#
    );
    let files = BTreeMap::from([
        ("wit/check.wit".into(), wit.into_bytes()),
        (
            "wit/check.shape.json".into(),
            br#"{"cardinality":"one","streams":{},"callbacks":{}}"#.to_vec(),
        ),
    ]);
    let manifest = PackManifest {
        format: "tobkiri.pack/1".into(),
        id: PackId::parse("local.error-contract").unwrap(),
        version: "1.0.0".into(),
        publisher_key: None,
        runtime: RuntimeSpec::Data,
        shapes: vec![ShapeDefinition {
            name: ShapeName::parse("local.error-contract.check").unwrap(),
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
        files: vec![],
    };
    load_shapes(&manifest, &files).unwrap().remove(0)
}

#[test]
fn only_outer_result_type_aliases_to_exact_core_error_receive_provenance() {
    for version in ["1.0.0", "1.1.0"] {
        let shape = verified_shape(version);
        assert_eq!(
            core_error_result_operations(&shape).unwrap(),
            BTreeSet::from([
                OpName::parse("direct").unwrap(),
                OpName::parse("aliases").unwrap(),
            ]),
        );
        // These structural normalized signatures are deliberately identical.
        // Public projection must use resolved provenance, not this equality.
        assert_eq!(
            shape.functions[&OpName::parse("direct").unwrap()],
            shape.functions[&OpName::parse("copied").unwrap()],
        );
    }
}

#[test]
fn core_package_spoofs_unknown_versions_and_changed_result_metadata_are_rejected() {
    let shape = verified_shape("1.0.0");
    let mut changed = shape.clone();
    changed.wit = tobkiri_wit::CORE_WIT_SOURCE.into();
    assert!(core_error_result_operations(&changed).is_err());
    changed.wit = shape.wit.replace("types@1.0.0", "types@1.2.0");
    assert!(core_error_result_operations(&changed).is_err());
    changed = shape.clone();
    changed
        .functions
        .get_mut(&OpName::parse("direct").unwrap())
        .unwrap()
        .result = Some(WitType::String);
    assert!(core_error_result_operations(&changed).is_err());
    changed = shape;
    changed.wit = " ".repeat(1024 * 1024 + 1);
    assert!(core_error_result_operations(&changed).is_err());
}

#[test]
fn repeated_alias_expansion_is_bounded_before_normalized_result_construction() {
    let mut shape = verified_shape("1.0.0");
    shape.functions.retain(|op, _| op.as_str() == "direct");
    shape.functions.values_mut().next().unwrap().result = Some(WitType::String);
    let mut wit =
        String::from("package local:error-contract@1.0.0; interface check { type a0 = string;");
    for index in 1..=24 {
        let previous = index - 1;
        wit.push_str(&format!("type a{index} = tuple<a{previous}, a{previous}>;"));
    }
    wit.push_str("direct: func() -> a24; }");
    assert!(wit.len() < 2048);
    shape.wit = wit;
    let error = core_error_result_operations(&shape).unwrap_err();
    assert!(error.to_string().contains("bounded WIT type"));
    assert!(!error.to_string().contains("result differs"));
}

fn runtime_artifact(
    shape: &ShapeDef,
    files: BTreeMap<String, Vec<u8>>,
    direction: WitBindingDirection,
) -> VerifiedPack {
    let port = PortDeclaration {
        shape: shape.reference.name.clone(),
        version: shape.reference.version,
        optional: false,
    };
    let manifest = PackManifest {
        format: "tobkiri.pack/1".into(),
        id: PackId::parse("local.actual-runtime").unwrap(),
        version: "1.0.0".into(),
        publisher_key: None,
        runtime: RuntimeSpec::Data,
        // The canonical ShapeDef belongs to a different Pack. This artifact
        // carries its own independently hash-verified dependency declaration.
        shapes: vec![],
        ports: match direction {
            WitBindingDirection::Import => Ports {
                consumes: vec![port],
                ..Ports::default()
            },
            WitBindingDirection::Export => Ports {
                provides: vec![port],
                ..Ports::default()
            },
        },
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
    let bytes = build_tpk(&manifest, &files, None).unwrap();
    verify_tpk(&bytes, &BTreeMap::new()).unwrap()
}

#[test]
fn actual_artifact_binding_is_independent_of_same_ref_canonical_nominal_proof() {
    let shape = verified_shape("1.0.0");
    let views = BTreeMap::from([(shape.reference.clone(), shape.clone())]);
    let direct = OpName::parse("direct").unwrap();
    assert!(
        core_error_result_operations(&shape)
            .unwrap()
            .contains(&direct)
    );
    for direction in [WitBindingDirection::Import, WitBindingDirection::Export] {
        let nominal = runtime_artifact(
            &shape,
            BTreeMap::from([("wit/runtime.wit".into(), shape.wit.as_bytes().to_vec())]),
            direction,
        );
        let nominal_inspection = inspect_pack_wit(&nominal, &views).unwrap();
        assert_eq!(
            nominal_inspection.allowed_imports(),
            &allowed_imports_for_pack(&nominal).unwrap()
        );
        assert_eq!(nominal_inspection.core_errors().hash(), &nominal.hash);
        assert!(nominal_inspection.core_errors().has_core_error_result(
            direction,
            &shape.reference,
            &direct
        ));
        let opposite = match direction {
            WitBindingDirection::Import => WitBindingDirection::Export,
            WitBindingDirection::Export => WitBindingDirection::Import,
        };
        assert!(!nominal_inspection.core_errors().has_core_error_result(
            opposite,
            &shape.reference,
            &direct
        ));
        let local_wit = shape
            .wit
            .replace(
                "direct: func() -> result<string, error>;",
                "direct: func() -> result<string, lookalike>;",
            )
            .replace(
                "aliases: func() -> aliased-result;",
                "aliases: func() -> result<string, lookalike>;",
            );
        let local = runtime_artifact(
            &shape,
            BTreeMap::from([("wit/runtime.wit".into(), local_wit.into_bytes())]),
            direction,
        );
        assert_ne!(nominal.hash, local.hash);
        // Loading/structural compatibility remains possible. Only the actual
        // runtime binding's nominal projection eligibility is absent.
        let local_inspection = inspect_pack_wit(&local, &views).unwrap();
        assert!(!local_inspection.core_errors().has_core_error_result(
            direction,
            &shape.reference,
            &direct
        ));
        assert!(local.shapes.is_empty());
    }
}

#[test]
fn artifact_aliases_resolve_across_verified_dependency_documents_once() {
    let shape = verified_shape("1.1.0");
    let views = BTreeMap::from([(shape.reference.clone(), shape.clone())]);
    let wit = shape.wit.replace(
        "type first-alias = error;",
        "use local:aliases/error-types@1.0.0.{alias as third-alias}; type first-alias = third-alias;",
    );
    let pack = runtime_artifact(
        &shape,
        BTreeMap::from([
            ("wit/a-runtime.wit".into(), wit.into_bytes()),
            (
                "wit/z-dependency.wit".into(),
                br#"package local:aliases@1.0.0;
interface error-types {
  use tobkiri:core/types@1.1.0.{error};
  type alias = error;
}"#
                .to_vec(),
            ),
        ]),
        WitBindingDirection::Export,
    );
    let inspection = inspect_pack_wit(&pack, &views).unwrap();
    assert!(
        inspection
            .allowed_imports()
            .contains("local:aliases/error-types@1.0.0")
    );
    assert!(inspection.core_errors().has_core_error_result(
        WitBindingDirection::Export,
        &shape.reference,
        &OpName::parse("aliases").unwrap(),
    ));
    assert!(!inspection.core_errors().has_core_error_result(
        WitBindingDirection::Export,
        &shape.reference,
        &OpName::parse("copied").unwrap(),
    ));
}

#[test]
fn actual_package_can_span_distinct_verified_interface_documents() {
    let shape = verified_shape("1.0.0");
    let views = BTreeMap::from([(shape.reference.clone(), shape.clone())]);
    let pack = runtime_artifact(
        &shape,
        BTreeMap::from([
            ("wit/check.wit".into(), shape.wit.as_bytes().to_vec()),
            (
                "wit/settings.wit".into(),
                b"package local:error-contract@1.0.0; interface settings { run: func(); }".to_vec(),
            ),
        ]),
        WitBindingDirection::Export,
    );
    // This artifact has no global definitions of its own. Its actual package
    // dependency still has the exact canonical export's nominal error type.
    assert!(pack.shapes.is_empty());
    let inspection = inspect_pack_wit(&pack, &views).unwrap();
    assert!(inspection.core_errors().has_core_error_result(
        WitBindingDirection::Export,
        &shape.reference,
        &OpName::parse("direct").unwrap(),
    ));
    assert_eq!(
        inspection.allowed_imports(),
        &allowed_imports_for_pack(&pack).unwrap(),
    );
}
