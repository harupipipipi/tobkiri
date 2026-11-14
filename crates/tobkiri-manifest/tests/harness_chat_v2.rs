//! The standalone structured chat contract is a distinct, projected major.
use std::collections::BTreeMap;
use tobkiri_canon::Digest;
use tobkiri_gate::PermissionSet;
use tobkiri_manifest::*;

const V1: &str = include_str!("../../../packs/shapes/harness-chat.wit");
const V2: &str = include_str!("../../../packs/shapes/harness-chat-v2/harness-chat.wit");
const PROVIDER_SIDECAR: &str =
    include_str!("../../../packs/shapes/harness-chat-v2/llm-provider.shape.json");
const INPUT_SIDECAR: &str =
    include_str!("../../../packs/shapes/harness-chat-v2/user-input.shape.json");

fn shape(major: u16, interface: &str, wit: &str, sidecar: &str) -> ShapeDef {
    let files = BTreeMap::from([
        ("wit/contract.wit".to_owned(), wit.as_bytes().to_vec()),
        (
            format!("wit/{interface}.shape.json"),
            sidecar.as_bytes().to_vec(),
        ),
    ]);
    let manifest = PackManifest {
        format: "tobkiri.pack/1".into(),
        id: PackId::parse("local.harness-chat").unwrap(),
        version: "1.0.0".into(),
        publisher_key: None,
        runtime: RuntimeSpec::Data,
        shapes: vec![ShapeDefinition {
            name: ShapeName::parse(&format!("local.harness-chat.{interface}")).unwrap(),
            version: ShapeVersion::new(major, 0),
            kind: ShapeKind::Port,
            wit: "wit/contract.wit".into(),
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
    let bytes = build_tpk(&manifest, &files, None).unwrap();
    verify_tpk(&bytes, &BTreeMap::new())
        .unwrap()
        .shapes
        .remove(0)
}

fn field<'a>(ty: &'a WitType, name: &str) -> &'a WitType {
    let WitType::Record(fields) = ty else {
        panic!("expected record")
    };
    &fields
        .iter()
        .find(|field| field.name == name)
        .expect("field")
        .ty
}
fn list(ty: &WitType) -> &WitType {
    let WitType::List(item) = ty else {
        panic!("expected list")
    };
    item
}
fn case<'a>(ty: &'a WitType, name: &str) -> Option<&'a WitType> {
    let WitType::Variant(cases) = ty else {
        panic!("expected variant")
    };
    cases
        .iter()
        .find(|case| case.name == name)
        .expect("case")
        .ty
        .as_ref()
}

#[test]
fn structured_provider_requires_a_major_adapter_and_projects_all_data() {
    let old = shape(1, "llm-provider", V1, PROVIDER_SIDECAR);
    let new = shape(2, "llm-provider", V2, PROVIDER_SIDECAR);
    assert_eq!(compatibility(&old, &new).unwrap(), Compat::NeedsAdapter);
    assert_eq!(
        new.streams.get("chat").map(String::as_str),
        Some("chat-event")
    );
    let prompt = &new.functions[&OpName::parse("chat").unwrap()].params[0].1;
    let tool = list(field(prompt, "tools"));
    assert_eq!(field(tool, "parameters"), &WitType::Json);
    let message = list(field(prompt, "messages"));
    let part = list(field(message, "parts"));
    assert_eq!(case(part, "text"), Some(&WitType::String));
    let call = case(part, "tool-call").unwrap();
    assert_eq!(field(call, "id"), &WitType::String);
    assert_eq!(field(call, "arguments"), &WitType::Json);
    let result = case(part, "tool-result").unwrap();
    assert_eq!(field(result, "call-id"), &WitType::String);
    assert_eq!(field(result, "is-error"), &WitType::Bool);
    let event = &new.types["chat-event"];
    assert_eq!(case(event, "text-delta"), Some(&WitType::String));
    assert_eq!(case(event, "reasoning-delta"), Some(&WitType::String));
    let delta = case(event, "tool-delta").unwrap();
    assert_eq!(field(delta, "index"), &WitType::U32);
    assert_eq!(
        field(delta, "id"),
        &WitType::Option(Box::new(WitType::String))
    );
    assert_eq!(
        field(delta, "arguments"),
        &WitType::Option(Box::new(WitType::String))
    );
    let usage = case(event, "usage").unwrap();
    for name in ["prompt-tokens", "completion-tokens", "cached-tokens"] {
        assert_eq!(field(usage, name), &WitType::Option(Box::new(WitType::U64)));
    }
    let finish = case(event, "finish").unwrap();
    for name in ["stop", "length", "tool-calls", "content-filter"] {
        assert!(case(finish, name).is_none());
    }
    assert_eq!(case(finish, "other"), Some(&WitType::String));
}

#[test]
fn input_submit_keeps_the_flow_frame_and_accepts_tool_metadata() {
    let old = shape(1, "user-input", V1, INPUT_SIDECAR);
    let new = shape(2, "user-input", V2, INPUT_SIDECAR);
    assert_eq!(compatibility(&old, &new).unwrap(), Compat::NeedsAdapter);
    assert_eq!(
        new.streams.get("submit").map(String::as_str),
        Some("flow-frame")
    );
    assert_eq!(new.types["flow-frame"], WitType::Json);
    let input = &new.functions[&OpName::parse("submit").unwrap()].params[0].1;
    assert_eq!(
        field(list(field(input, "tools")), "parameters"),
        &WitType::Json
    );
    assert!(matches!(list(field(input, "parts")), WitType::Variant(_)));
}
