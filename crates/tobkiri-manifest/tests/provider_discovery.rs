//! Standalone discovery shapes must remain compatible with the published 1.0 pins.
use std::collections::BTreeMap;
use tobkiri_canon::Digest;
use tobkiri_gate::PermissionSet;
use tobkiri_manifest::*;

fn verified_shape(id: &str, interface: &str, minor: u16, wit: &str, sidecar: &str) -> ShapeDef {
    let wit_path = "wit/contract.wit".to_string();
    let files = BTreeMap::from([
        (wit_path.clone(), wit.as_bytes().to_vec()),
        (
            format!("wit/{interface}.shape.json"),
            sidecar.as_bytes().to_vec(),
        ),
    ]);
    let manifest = PackManifest {
        format: "tobkiri.pack/1".into(),
        id: PackId::parse(id).unwrap(),
        version: "1.0.0".into(),
        publisher_key: None,
        runtime: RuntimeSpec::Data,
        shapes: vec![ShapeDefinition {
            name: ShapeName::parse(&format!("{id}.{interface}")).unwrap(),
            version: ShapeVersion::new(1, minor),
            kind: ShapeKind::Port,
            wit: wit_path,
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
    let tpk = build_tpk(&manifest, &files, None).unwrap();
    verify_tpk(&tpk, &BTreeMap::new()).unwrap().shapes.remove(0)
}

fn assert_discovery_contract(
    old: ShapeDef,
    new: ShapeDef,
    operation: &str,
    fields: &[(&str, WitType)],
) {
    assert_eq!(compatibility(&old, &new).unwrap(), Compat::MinorCompatible);
    let invoke = OpName::parse("invoke").unwrap();
    assert_eq!(old.functions[&invoke], new.functions[&invoke]);
    assert_eq!(new.functions.len(), old.functions.len() + 1);
    let list = &new.functions[&OpName::parse(operation).unwrap()];
    assert!(list.params.is_empty());
    let Some(WitType::Result {
        ok: Some(ok),
        err: Some(_),
    }) = &list.result
    else {
        panic!("inventory must return a result with an error");
    };
    let WitType::List(item) = ok.as_ref() else {
        panic!("inventory must return a list")
    };
    let WitType::Record(actual) = item.as_ref() else {
        panic!("inventory entries must be records")
    };
    assert_eq!(
        actual,
        &fields
            .iter()
            .map(|(name, ty)| WitField {
                name: (*name).into(),
                ty: ty.clone(),
            })
            .collect::<Vec<_>>()
    );
}

#[test]
fn tool_inventory_is_a_compatible_minor_and_carries_the_input_schema() {
    let sidecar =
        include_str!("../../../packs/shapes/provider-discovery-v1-1/tool-provider.shape.json");
    let old = verified_shape(
        "local.harness-agent",
        "tool-provider",
        0,
        include_str!("../../../packs/shapes/harness-agent.wit"),
        sidecar,
    );
    let new = verified_shape(
        "local.harness-agent",
        "tool-provider",
        1,
        include_str!("../../../packs/shapes/provider-discovery-v1-1/harness-agent.wit"),
        sidecar,
    );
    assert_discovery_contract(
        old,
        new,
        "list-tools",
        &[
            ("name", WitType::String),
            ("label", WitType::String),
            ("description", WitType::String),
            ("parameters", WitType::Json),
        ],
    );
}

#[test]
fn command_inventory_is_a_compatible_minor_and_carries_descriptions() {
    let sidecar =
        include_str!("../../../packs/shapes/provider-discovery-v1-1/command-provider.shape.json");
    let old = verified_shape(
        "local.harness-chat",
        "command-provider",
        0,
        include_str!("../../../packs/shapes/harness-chat.wit"),
        sidecar,
    );
    let new = verified_shape(
        "local.harness-chat",
        "command-provider",
        1,
        include_str!("../../../packs/shapes/provider-discovery-v1-1/harness-chat.wit"),
        sidecar,
    );
    assert_discovery_contract(
        old,
        new,
        "list-commands",
        &[("name", WitType::String), ("description", WitType::String)],
    );
}
