//! Hash-verified WIT/data fixtures test compilation, not physical guest execution.
use std::collections::BTreeMap;
use tobkiri_canon::{CanonValue, Digest, ids::*};
use tobkiri_gate::{LoopLimits, PermissionSet};
use tobkiri_manifest::*;
use tobkiri_profile::*;

fn view() -> ShapeRef {
    ShapeRef::parse("local.physical-input.input@1.0").unwrap()
}
fn selection(operation: &str) -> InputEntry {
    InputEntry {
        source_pack: PackId::parse("local.physical-input").unwrap(),
        exact_input_view: view(),
        operation: OpName::parse(operation).unwrap(),
    }
}
fn source(functions: &str, operations: &[&str]) -> VerifiedPack {
    let files: BTreeMap<String, Vec<u8>> = BTreeMap::from([
        (
            "wit/input.wit".into(),
            format!("package local:physical-input@1.0.0; interface input {{ {functions} }}")
                .into_bytes(),
        ),
        (
            "wit/input.shape.json".into(),
            br#"{"cardinality":"one","streams":{},"callbacks":{}}"#.to_vec(),
        ),
    ]);
    let manifest = PackManifest {
        format: "tobkiri.pack/1".into(),
        id: selection("receive").source_pack,
        version: "1.0.0".into(),
        publisher_key: None,
        runtime: RuntimeSpec::Data,
        shapes: vec![ShapeDefinition {
            name: view().name,
            version: view().version,
            kind: ShapeKind::Port,
            wit: "wit/input.wit".into(),
        }],
        ports: Ports {
            provides: vec![PortDeclaration {
                shape: view().name,
                version: view().version,
                optional: false,
            }],
            ..Default::default()
        },
        inputs: operations
            .iter()
            .map(|operation| InputDeclaration {
                exact_input_view: view(),
                operation: OpName::parse(operation).unwrap(),
            })
            .collect(),
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
    verify_tpk(
        &build_tpk(&manifest, &files, None).unwrap(),
        &BTreeMap::new(),
    )
    .unwrap()
}
fn profile(pack: &VerifiedPack, entry: InputEntry) -> Profile {
    let ceiling = PermissionSet {
        loops: LoopLimits {
            max_iterations: 8,
            max_wall_ms: 1000,
            max_cost_units: 20,
        },
        ..Default::default()
    };
    Profile {
        format: "tobkiri.profile/1".into(),
        id: ProfileId::parse("exact_input").unwrap(),
        name: "Exact input".into(),
        packs: vec![PackPin {
            id: pack.manifest.id.clone(),
            version: pack.manifest.version.clone(),
            sha256: pack.hash.clone(),
            source: "fixture:input".into(),
        }],
        publishers: BTreeMap::new(),
        pack_limits: BTreeMap::from([(
            pack.manifest.id.clone(),
            pack.manifest.permissions.intersect(&ceiling),
        )]),
        ceiling,
        user_permissions: None,
        bindings: BTreeMap::new(),
        overlay_order: vec![],
        user_overlays: vec![],
        startup: Startup {
            inputs: vec![entry],
            ui: None,
        },
    }
}
fn has_error(profile: &Profile, pack: &VerifiedPack, code: &str) {
    let errors = compile(profile, std::slice::from_ref(pack)).unwrap_err();
    assert!(errors.iter().any(|error| error.code == code), "{errors:?}");
}

#[test]
fn exact_entry_freezes_operation_hash_function_and_revision() {
    let pack = source(
        "receive: func() -> string; other: func() -> u32;",
        &["receive", "other"],
    );
    let doc = profile(&pack, selection("receive"));
    let compiled = compile(&doc, std::slice::from_ref(&pack)).unwrap();
    let entry = compiled
        .input_entry(
            &pack.manifest.id,
            &view(),
            &OpName::parse("receive").unwrap(),
        )
        .unwrap();
    assert_eq!(entry.selection(), &doc.startup.inputs[0]);
    assert_eq!(entry.source_hash(), &pack.hash);
    assert_eq!(entry.profile_revision(), &compiled.plan_digest);
    assert_eq!(entry.function().result, Some(WitType::String));
    assert_eq!(entry.arguments(), &CanonValue::Obj(BTreeMap::new()));
    assert!(
        compiled
            .input_entry(&pack.manifest.id, &view(), &OpName::parse("other").unwrap())
            .is_none()
    );
    let mut other = doc.clone();
    other.startup.inputs[0].operation = OpName::parse("other").unwrap();
    let other = compile(&other, std::slice::from_ref(&pack)).unwrap();
    assert_ne!(compiled.plan_digest, other.plan_digest);
    assert_eq!(
        other.input_entries()[0].function().result,
        Some(WitType::U32)
    );
    let mut stale = compiled.clone();
    stale.plan_digest = Digest::hash(b"different revision");
    assert!(
        stale
            .input_entry(
                &pack.manifest.id,
                &view(),
                &OpName::parse("receive").unwrap()
            )
            .is_none()
    );
}

#[test]
fn ordinary_provided_port_is_not_a_formal_input_and_other_ops_are_not_inferred() {
    let pack = source("receive: func() -> string; other: func() -> string;", &[]);
    has_error(
        &profile(&pack, selection("receive")),
        &pack,
        "startup-input-declaration",
    );
    let pack = source(
        "receive: func() -> string; other: func() -> string;",
        &["receive"],
    );
    has_error(
        &profile(&pack, selection("other")),
        &pack,
        "startup-input-declaration",
    );
}

#[test]
fn exact_minor_view_is_required_without_provider_alias_projection() {
    let pack = source("receive: func() -> string;", &["receive"]);
    let mut doc = profile(&pack, selection("receive"));
    doc.startup.inputs[0].exact_input_view =
        ShapeRef::parse("local.physical-input.input@1.1").unwrap();
    has_error(&doc, &pack, "startup-input-view");
}

#[test]
fn required_arguments_are_rejected_even_when_another_operation_is_usable() {
    let pack = source(
        "receive: func() -> string; other: func(text: string) -> string;",
        &["receive", "other"],
    );
    has_error(
        &profile(&pack, selection("other")),
        &pack,
        "startup-input-signature",
    );
}

#[test]
fn only_explicit_empty_record_values_are_constructed() {
    let pack = source(
        "record empty {} receive: func(options: empty) -> string;",
        &["receive"],
    );
    let compiled = compile(&profile(&pack, selection("receive")), &[pack]).unwrap();
    assert_eq!(
        compiled.input_entries()[0].arguments(),
        &CanonValue::Obj(BTreeMap::from([(
            "options".into(),
            CanonValue::Obj(BTreeMap::new())
        )]))
    );
}

#[test]
fn duplicate_selection_and_changed_pack_hash_are_rejected() {
    let pack = source("receive: func() -> string;", &["receive"]);
    let mut doc = profile(&pack, selection("receive"));
    doc.startup.inputs.push(selection("receive"));
    has_error(&doc, &pack, "startup-input-duplicate");
    let mut doc = profile(&pack, selection("receive"));
    doc.packs[0].sha256 = PackHash::new(Digest::hash(b"different artifact"));
    has_error(&doc, &pack, "missing-artifact");
}

#[test]
fn frozen_routes_match_only_exact_trigger_views() {
    let mut pack = source("receive: func() -> string;", &["receive"]);
    let flow = FlowDocument {
        format: "tobkiri.flow/1".into(),
        id: "on-input".into(),
        implements: None,
        trigger: Trigger { input: view() },
        loop_limits: "profile-default".into(),
        steps: vec![],
        on_error: ErrorPolicy::default(),
    };
    let bytes = serde_json::to_vec(&flow).unwrap();
    pack.manifest.flows.push("flow.json".into());
    pack.manifest.files.push(FileEntry {
        path: "flow.json".into(),
        sha256: Digest::hash(&bytes),
        size: bytes.len() as u64,
    });
    pack.files.insert("flow.json".into(), bytes);
    let pack = verify_tpk(
        &build_tpk(&pack.manifest, &pack.files, None).unwrap(),
        &BTreeMap::new(),
    )
    .unwrap();
    let doc = profile(&pack, selection("receive"));
    let compiled = compile(&doc, std::slice::from_ref(&pack)).unwrap();
    assert_eq!(
        compiled.input_entries()[0].flow_routes(),
        &["local.physical-input/on-input".to_string()]
    );
    let mut altered = pack.clone();
    let mut wrong_flow = flow;
    wrong_flow.trigger.input = ShapeRef::parse("local.physical-input.input@1.1").unwrap();
    let wrong_bytes = serde_json::to_vec(&wrong_flow).unwrap();
    for file in &mut altered.manifest.files {
        if file.path == "flow.json" {
            file.sha256 = Digest::hash(&wrong_bytes);
            file.size = wrong_bytes.len() as u64;
        }
    }
    altered.files.insert("flow.json".into(), wrong_bytes);
    let altered = verify_tpk(
        &build_tpk(&altered.manifest, &altered.files, None).unwrap(),
        &BTreeMap::new(),
    )
    .unwrap();
    has_error(
        &profile(&altered, selection("receive")),
        &altered,
        "flow-trigger-view",
    );
}

#[test]
fn multiple_empty_record_parameters_resolve_aliases_and_preserve_each_name() {
    let pack = source(
        "record empty {} type first-alias = empty; type second-alias = first-alias; receive: func(first: first-alias, second: second-alias, third: empty);",
        &["receive"],
    );
    let compiled = compile(&profile(&pack, selection("receive")), &[pack]).unwrap();
    let entry = &compiled.input_entries()[0];
    assert_eq!(entry.function().result, None);
    assert_eq!(entry.function().params.len(), 3);
    for (_, ty) in &entry.function().params {
        assert_eq!(ty, &WitType::Record(vec![]));
    }
    assert_eq!(
        entry.arguments(),
        &CanonValue::Obj(
            ["first", "second", "third"]
                .into_iter()
                .map(|name| (name.to_string(), CanonValue::Obj(BTreeMap::new())))
                .collect()
        )
    );
}

#[test]
fn alias_to_nonempty_record_does_not_supply_even_optional_fields() {
    let pack = source(
        "record options { text: option<string> } type options-alias = options; receive: func(options: options-alias);",
        &["receive"],
    );
    has_error(
        &profile(&pack, selection("receive")),
        &pack,
        "startup-input-signature",
    );
}

#[test]
fn zero_result_empty_record_and_payloadless_result_keep_exact_signatures() {
    let cases = [
        ("receive: func();", None),
        (
            "record empty {} receive: func() -> empty;",
            Some(WitType::Record(vec![])),
        ),
        (
            "receive: func() -> result<_, string>;",
            Some(WitType::Result {
                ok: None,
                err: Some(Box::new(WitType::String)),
            }),
        ),
        (
            "record empty {} receive: func() -> result<empty, string>;",
            Some(WitType::Result {
                ok: Some(Box::new(WitType::Record(vec![]))),
                err: Some(Box::new(WitType::String)),
            }),
        ),
        ("receive: func() -> string;", Some(WitType::String)),
    ];
    let mut signatures = Vec::new();
    for (wit, expected) in cases {
        let pack = source(wit, &["receive"]);
        let compiled = compile(&profile(&pack, selection("receive")), &[pack]).unwrap();
        let actual = compiled.input_entries()[0].function().result.clone();
        assert_eq!(actual, expected);
        signatures.push(actual);
    }
    for (index, signature) in signatures.iter().enumerate() {
        assert!(!signatures[..index].contains(signature));
    }
    // These are compile-time signature proofs only. No synthetic ingress
    // payload is constructed here; shared runtime codec acceptance is pending.
}

#[test]
fn stream_and_callback_delivery_is_explicitly_unimplemented_not_parity() {
    for handle in ["stream-handle", "callback-handle"] {
        let pack = source(
            &format!("use tobkiri:core/types@1.0.0.{{{handle}}}; receive: func() -> {handle};"),
            &["receive"],
        );
        let errors = compile(&profile(&pack, selection("receive")), &[pack]).unwrap_err();
        assert!(errors.iter().any(|error| {
            error.code == "startup-input-signature"
                && error
                    .message
                    .contains("lifetime forwarding is unimplemented")
        }));
    }
}
