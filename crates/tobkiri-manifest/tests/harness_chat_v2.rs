//! The standalone structured chat contract is a distinct, projected major.
use std::collections::{BTreeMap, BTreeSet};
use tobkiri_canon::{Digest, Variant};
use tobkiri_gate::PermissionSet;
use tobkiri_manifest::*;

const V1: &str = include_str!("../../../packs/shapes/harness-chat.wit");
const V2: &str = include_str!("../../../packs/shapes/harness-chat-v2/harness-chat.wit");
const PROVIDER_SIDECAR: &str =
    include_str!("../../../packs/shapes/harness-chat-v2/llm-provider.shape.json");
const INPUT_SIDECAR: &str =
    include_str!("../../../packs/shapes/harness-chat-v2/user-input.shape.json");
const STORE_SIDECAR: &str =
    include_str!("../../../packs/shapes/harness-chat-v2/conversation-store.shape.json");

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
    verify_tpk(&bytes, &BTreeMap::new()).unwrap().shapes[0].clone()
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
    assert_eq!(case(part, "reasoning"), Some(&WitType::String));
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
    let response = WitType::Record(vec![
        WitField {
            name: "finish".into(),
            ty: finish.clone(),
        },
        WitField {
            name: "usage".into(),
            ty: WitType::Option(Box::new(usage.clone())),
        },
    ]);
    assert_eq!(
        field(message, "response"),
        &WitType::Option(Box::new(response))
    );
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

#[test]
fn conversation_store_projects_the_same_ordered_messages_for_append_load_and_prompt() {
    let old = shape(1, "conversation-store", V1, STORE_SIDECAR);
    let store = shape(2, "conversation-store", V2, STORE_SIDECAR);
    assert_eq!(compatibility(&old, &store).unwrap(), Compat::NeedsAdapter);
    assert_eq!(store.cardinality, Cardinality::One);
    assert!(store.streams.is_empty());
    assert!(store.callbacks.is_empty());

    let load = OpName::parse("load").unwrap();
    let append = OpName::parse("append").unwrap();
    assert_eq!(
        store.functions.keys().cloned().collect::<BTreeSet<_>>(),
        BTreeSet::from([load.clone(), append.clone()])
    );
    assert_eq!(
        core_error_result_operations(&store).unwrap(),
        BTreeSet::from([load.clone(), append.clone()])
    );

    let provider = shape(2, "llm-provider", V2, PROVIDER_SIDECAR);
    let prompt = &provider.functions[&OpName::parse("chat").unwrap()].params[0].1;
    let messages = field(prompt, "messages");
    let user_input = shape(2, "user-input", V2, INPUT_SIDECAR);
    let input = &user_input.functions[&OpName::parse("submit").unwrap()].params[0].1;
    let error = Some(Box::new(store.types["error"].clone()));
    assert_eq!(
        store.functions[&append],
        WitFunction {
            params: vec![
                ("conversation".into(), WitType::String),
                ("input".into(), input.clone()),
                ("messages".into(), messages.clone()),
            ],
            result: Some(WitType::Result {
                ok: None,
                err: error.clone(),
            }),
        }
    );
    let context = WitType::Record(vec![
        WitField {
            name: "conversation".into(),
            ty: WitType::String,
        },
        WitField {
            name: "messages".into(),
            ty: messages.clone(),
        },
    ]);
    assert_eq!(
        store.functions[&load],
        WitFunction {
            params: vec![("conversation".into(), WitType::String)],
            result: Some(WitType::Result {
                ok: Some(Box::new(context)),
                err: error,
            }),
        }
    );

    let completed_messages = tobkiri_canon::parse(
        br#"[
          {"role":"assistant","parts":[
            {"tag":"reasoning","val":"Need the local time"},
            {"tag":"tool-call","val":{"id":"call-1","name":"clock","arguments":{"zone":"UTC"}}}
          ],"response":{"finish":{"tag":"tool-calls"},"usage":{"prompt-tokens":20,"completion-tokens":0,"cached-tokens":null}}},
          {"role":"tool","parts":[
            {"tag":"tool-result","val":{"call-id":"call-1","content":"12:00 UTC","is-error":false}}
          ],"response":null},
          {"role":"assistant","parts":[{"tag":"text","val":"It is noon in UTC."}],"response":{"finish":{"tag":"stop"},"usage":null}}
        ]"#,
        Variant::StrictV1,
    )
    .unwrap();
    assert_eq!(
        project_json(
            &store.functions[&append].params[2].1,
            messages,
            &completed_messages
        )
        .unwrap(),
        completed_messages
    );
}
