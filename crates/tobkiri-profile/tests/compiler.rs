//! Topology fixtures use hash-verified data packages; guest execution is tested
//! separately by the Wasmtime and end-to-end suites.
use std::collections::BTreeMap;
use tobkiri_canon::{CanonValue, Digest, ids::*};
use tobkiri_gate::{LoopLimits, PermissionSet};
use tobkiri_manifest::*;
use tobkiri_profile::*;

fn reference(name: &str) -> ShapeRef {
    ShapeRef::parse(&format!("local.contracts.{name}@1.0")).unwrap()
}
fn pack(
    id: &str,
    files: BTreeMap<String, Vec<u8>>,
    shapes: Vec<ShapeDefinition>,
    provides: Vec<PortDeclaration>,
    consumes: Vec<PortDeclaration>,
    flows: Vec<String>,
    overlays: Vec<String>,
) -> VerifiedPack {
    let mut permissions = PermissionSet::default();
    permissions.ports.extend([
        reference("query").to_string(),
        reference("role").to_string(),
    ]);
    let m = PackManifest {
        format: "tobkiri.pack/1".into(),
        id: PackId::parse(id).unwrap(),
        version: "1.0.0".into(),
        publisher_key: None,
        runtime: RuntimeSpec::Data,
        shapes,
        ports: Ports {
            provides,
            consumes,
            untyped: vec![],
        },
        inputs: vec![],
        flows,
        overlays,
        stores: vec![],
        permissions,
        ui: None,
        files: files
            .iter()
            .map(|(path, b)| FileEntry {
                path: path.clone(),
                sha256: Digest::hash(b),
                size: b.len() as u64,
            })
            .collect(),
    };
    verify_tpk(&build_tpk(&m, &files, None).unwrap(), &BTreeMap::new()).unwrap()
}
fn port(name: &str, optional: bool) -> PortDeclaration {
    let r = reference(name);
    PortDeclaration {
        shape: r.name,
        version: r.version,
        optional,
    }
}
fn contracts() -> VerifiedPack {
    let wit=b"package local:contracts@1.0.0; interface query { query: func(value: string) -> string; } interface role { run: func(); } interface workflow { run: func(); } interface input { read: func() -> string; }".to_vec();
    let mut files = BTreeMap::from([("wit/contract.wit".into(), wit)]);
    let shapes = [
        ("query", ShapeKind::Port),
        ("role", ShapeKind::Role),
        ("workflow", ShapeKind::Flow),
        ("input", ShapeKind::Port),
    ]
    .into_iter()
    .map(|(name, kind)| {
        files.insert(
            format!("wit/{name}.shape.json"),
            br#"{"cardinality":"one","streams":{},"callbacks":{}}"#.to_vec(),
        );
        ShapeDefinition {
            name: reference(name).name,
            version: ShapeVersion::new(1, 0),
            kind,
            wit: "wit/contract.wit".into(),
        }
    })
    .collect();
    pack(
        "local.contracts",
        files,
        shapes,
        vec![],
        vec![],
        vec![],
        vec![],
    )
}
fn profile(packs: &[VerifiedPack]) -> Profile {
    let mut ceiling = PermissionSet {
        loops: LoopLimits {
            max_iterations: 10,
            max_wall_ms: 1000,
            max_cost_units: 100,
        },
        ..PermissionSet::default()
    };
    ceiling.ports.extend([
        reference("query").to_string(),
        reference("role").to_string(),
    ]);
    Profile {
        format: "tobkiri.profile/1".into(),
        id: ProfileId::parse("fixture").unwrap(),
        name: "Fixture".into(),
        packs: packs
            .iter()
            .map(|p| PackPin {
                id: p.manifest.id.clone(),
                version: p.manifest.version.clone(),
                sha256: p.hash.clone(),
                source: format!("fixture:{}", p.manifest.id),
            })
            .collect(),
        publishers: BTreeMap::new(),
        pack_limits: packs
            .iter()
            .map(|pack| {
                (
                    pack.manifest.id.clone(),
                    pack.manifest.permissions.intersect(&ceiling),
                )
            })
            .collect(),
        ceiling,
        user_permissions: None,
        bindings: BTreeMap::new(),
        overlay_order: vec![],
        user_overlays: vec![],
        startup: Startup {
            inputs: vec![],
            ui: None,
        },
    }
}
fn flow(steps: Vec<Step>) -> FlowDocument {
    FlowDocument {
        format: "tobkiri.flow/1".into(),
        id: "turn".into(),
        implements: None,
        trigger: Trigger {
            input: reference("input"),
        },
        loop_limits: "profile-default".into(),
        steps,
        on_error: ErrorPolicy::default(),
    }
}
fn call(id: &str) -> Step {
    let mut s = Step::new(
        id,
        StepKind::Call(PortCall {
            shape: reference("query"),
            op: OpName::parse("query").unwrap(),
            args: CanonValue::Obj(BTreeMap::from([("value".into(), "hello".into())])),
        }),
    );
    s.role = Some(reference("role"));
    s.overlay = OverlayAccess::Open;
    s.out = Some("answer".into());
    s
}
fn flow_pack(doc: &FlowDocument) -> VerifiedPack {
    pack(
        "local.flow",
        BTreeMap::from([("turn.json".into(), serde_json::to_vec(doc).unwrap())]),
        vec![],
        vec![],
        vec![port("query", false)],
        vec!["turn.json".into()],
        vec![],
    )
}
fn provider(id: &str) -> VerifiedPack {
    pack(
        id,
        BTreeMap::new(),
        vec![],
        vec![port("query", false)],
        vec![],
        vec![],
        vec![],
    )
}
fn with_permissions(mut pack: VerifiedPack, permissions: PermissionSet) -> VerifiedPack {
    pack.manifest.permissions = permissions;
    verify_tpk(
        &build_tpk(&pack.manifest, &pack.files, None).unwrap(),
        &BTreeMap::new(),
    )
    .unwrap()
}
fn overlay(id: &str, target: OverlayTarget, body: Vec<Step>) -> OverlayDocument {
    OverlayDocument {
        format: "tobkiri.overlay/1".into(),
        id: id.into(),
        target,
        order: OrderConstraints::default(),
        body,
    }
}
fn has_error(result: Result<CompiledProfile, Vec<Conflict>>, code: &str) {
    let errors = result.unwrap_err();
    assert!(
        errors.iter().any(|e| e.code == code),
        "expected {code}: {errors:?}"
    );
}

#[test]
fn required_provider_removal_blocks_candidate_and_pin_hash_is_exact() {
    let packs = vec![
        contracts(),
        provider("local.provider"),
        flow_pack(&flow(vec![call("query")])),
    ];
    let p = profile(&packs);
    let first = compile(&p, &packs).unwrap();
    assert_eq!(first.flows.len(), 1);
    assert_eq!(first.plan_digest, compile(&p, &packs).unwrap().plan_digest);
    let mut removed = p.clone();
    removed
        .packs
        .retain(|pin| pin.id.as_str() != "local.provider");
    removed
        .pack_limits
        .remove(&PackId::parse("local.provider").unwrap());
    has_error(compile(&removed, &packs), "required-port");
    let mut bad = p;
    bad.packs[0].sha256 = Digest::hash(b"wrong").into();
    has_error(compile(&bad, &packs), "missing-artifact");
}
#[test]
fn single_provider_collision_needs_explicit_choice_and_many_is_automatic() {
    let mut packs = vec![contracts(), provider("local.one"), provider("local.two")];
    let mut p = profile(&packs);
    has_error(compile(&p, &packs), "provider-collision");
    p.bindings.insert(
        "local.contracts.query@1".into(),
        PackId::parse("local.two").unwrap(),
    );
    let compiled = compile(&p, &packs).unwrap();
    assert_eq!(
        compiled.bindings[&reference("query")],
        vec![PackId::parse("local.two").unwrap()]
    );
    packs[0]
        .shapes
        .iter_mut()
        .find(|s| s.reference == reference("query"))
        .unwrap()
        .cardinality = Cardinality::Many;
    p.bindings.clear();
    assert_eq!(
        compile(&p, &packs).unwrap().bindings[&reference("query")].len(),
        2
    );
}
#[test]
fn optional_and_untyped_ports_cannot_satisfy_required_typed_slots() {
    let optional = pack(
        "local.optional",
        BTreeMap::new(),
        vec![],
        vec![],
        vec![port("query", true)],
        vec![],
        vec![],
    );
    let packs = vec![contracts(), optional];
    compile(&profile(&packs), &packs).unwrap();
    let mut required = packs.clone();
    required[1].manifest.ports.consumes[0].optional = false;
    required[1].manifest.ports.untyped.push(UntypedPort {
        name: "local.contracts.query".into(),
        direction: "provides".into(),
    });
    has_error(compile(&profile(&required), &required), "required-port");
}
#[test]
fn default_loop_bounds_and_explicit_error_policy_keys_are_required() {
    let mut doc = flow(vec![call("query")]);
    doc.steps[0].id = None;
    doc.on_error
        .steps
        .insert("anonymous".into(), ErrorAction::Continue);
    let packs = vec![contracts(), provider("local.provider"), flow_pack(&doc)];
    let mut p = profile(&packs);
    has_error(compile(&p, &packs), "error-policy-step");
    p.ceiling.loops.max_iterations = 0;
    has_error(compile(&p, &packs), "profile-limits");
    let bytes = serde_json::to_vec(&p).unwrap();
    assert!(parse_profile(&bytes).is_err());
}
#[test]
fn unknown_fields_and_multiple_step_kinds_fail_before_execution() {
    for bytes in [
        br#"{"call":null,"return":null}"#.as_slice(),
        br#"{"return":null,"next":{"args":null}}"#,
        br#"{"return":null,"unexpected":true}"#,
    ] {
        assert!(serde_json::from_slice::<Step>(bytes).is_err());
    }
    let step: Step = serde_json::from_slice(br#"{"return":null}"#).unwrap();
    assert_eq!(step.overlay, OverlayAccess::Closed);
    assert!(
        serde_json::from_slice::<OverlayTarget>(br#"{"flow":"turn","step":"x","role":null}"#)
            .is_err()
    );
    assert!(
        serde_json::from_slice::<OverlayTarget>(
            br#"{"role":"local.contracts.role@1.0","extra":true}"#
        )
        .is_err()
    );
    let mut doc = serde_json::to_value(flow(vec![call("query")])).unwrap();
    doc["steps"][0]["call"]["args"]["value"] = serde_json::json!(0.5);
    assert!(parse_flow(&serde_json::to_vec(&doc).unwrap()).is_err());
}
#[test]
fn published_allowlist_excludes_local_identity_and_rejects_authority_fields() {
    let p = profile(&[contracts()]);
    let published = p.published();
    let mut value = serde_json::to_value(&published).unwrap();
    for key in ["id", "name", "approvals", "vault", "data", "head"] {
        assert!(value.get(key).is_none());
    }
    let bytes = serde_json::to_vec(&published).unwrap();
    let imported = parse_published(&bytes)
        .unwrap()
        .to_profile(ProfileId::parse("fresh").unwrap(), "Fresh");
    assert_eq!(imported.id.as_str(), "fresh");
    value["approvals"] = serde_json::json!(["test-secret-approval"]);
    assert!(parse_published(&serde_json::to_vec(&value).unwrap()).is_err());
}
#[test]
fn pack_overlay_requires_open_role_and_ceiling_permission_user_overlay_can_override() {
    let mut target = call("query");
    target.overlay = OverlayAccess::Closed;
    let doc = overlay(
        "wrap",
        OverlayTarget::Role {
            role: reference("role"),
        },
        vec![Step::new(
            "next",
            StepKind::Next(Next {
                args: "$args".into(),
            }),
        )],
    );
    let op = pack(
        "local.overlay",
        BTreeMap::from([("wrap.json".into(), serde_json::to_vec(&doc).unwrap())]),
        vec![],
        vec![],
        vec![],
        vec![],
        vec!["wrap.json".into()],
    );
    let packs = vec![
        contracts(),
        provider("local.provider"),
        flow_pack(&flow(vec![target.clone()])),
        op,
    ];
    let p = profile(&packs);
    has_error(compile(&p, &packs), "overlay-closed");
    let mut user = p;
    user.packs.retain(|pin| pin.id.as_str() != "local.overlay");
    user.pack_limits
        .remove(&PackId::parse("local.overlay").unwrap());
    user.user_overlays.push(doc);
    compile(&user, &packs).unwrap();
    target.overlay = OverlayAccess::Open;
    let mut packs = packs;
    packs[2] = flow_pack(&flow(vec![target]));
    let mut p = profile(&packs);
    p.ceiling.ports.remove(&reference("role").to_string());
    has_error(compile(&p, &packs), "overlay-permission");
}
#[test]
fn overlapping_overlays_need_total_order_and_cycles_fail() {
    let packs = vec![
        contracts(),
        provider("local.provider"),
        flow_pack(&flow(vec![call("query")])),
    ];
    let mut p = profile(&packs);
    let target = OverlayTarget::Step {
        flow: "local.flow/turn".into(),
        step: "query".into(),
    };
    for id in ["a", "b"] {
        p.user_overlays.push(overlay(
            id,
            target.clone(),
            vec![Step::new(
                "next",
                StepKind::Next(Next {
                    args: "$args".into(),
                }),
            )],
        ));
    }
    has_error(compile(&p, &packs), "overlay-order");
    p.overlay_order.push(OverlayOrder {
        target,
        order: vec!["user:a".into(), "user:b".into()],
    });
    let compiled = compile(&p, &packs).unwrap();
    assert_eq!(
        compiled.flows["local.flow/turn"].ordered_overlays["query"][0]
            .document
            .id,
        "a"
    );
    p.user_overlays[0].order.after.push("user:b".into());
    has_error(compile(&p, &packs), "overlay-order");
}
#[test]
fn deleting_used_output_and_repeated_next_fail_closed() {
    let mut consumer = call("consume");
    if let StepKind::Call(c) = &mut consumer.kind {
        c.args = CanonValue::Obj(BTreeMap::from([("value".into(), "$answer".into())]));
    }
    let packs = vec![
        contracts(),
        provider("local.provider"),
        flow_pack(&flow(vec![call("query"), consumer])),
    ];
    let mut p = profile(&packs);
    let target = OverlayTarget::Step {
        flow: "local.flow/turn".into(),
        step: "query".into(),
    };
    p.user_overlays
        .push(overlay("delete", target.clone(), vec![]));
    has_error(compile(&p, &packs), "delete-used-output");
    p.user_overlays[0] = overlay(
        "twice",
        target,
        vec![
            Step::new(
                "a",
                StepKind::Next(Next {
                    args: CanonValue::Null,
                }),
            ),
            Step::new(
                "b",
                StepKind::Next(Next {
                    args: CanonValue::Null,
                }),
            ),
        ],
    );
    has_error(compile(&p, &packs), "multiple-next");
}
#[test]
fn subflow_implements_ambiguity_and_major_mismatch_fail() {
    let mut sub = flow(vec![]);
    sub.implements = Some(reference("workflow"));
    let sub_a = flow_pack(&sub);
    let mut sub_b = sub.clone();
    sub_b.id = "other".into();
    let files = BTreeMap::from([("other.json".into(), serde_json::to_vec(&sub_b).unwrap())]);
    let sub_b = pack(
        "local.other",
        files,
        vec![],
        vec![],
        vec![],
        vec!["other.json".into()],
        vec![],
    );
    // The shared Flow fixture also declares the required query port. Keep that
    // ordinary dependency satisfied while testing independent Flow endpoints.
    let packs = vec![contracts(), provider("local.provider"), sub_a, sub_b];
    let compiled = compile(&profile(&packs), &packs)
        .expect("distinct exact Flow endpoints may share an implements view");
    assert_eq!(compiled.flows.len(), 2);
    let mut ambiguous_parent = flow(vec![Step::new(
        "sub",
        StepKind::Flow(SubFlow {
            implements: reference("workflow"),
            args: CanonValue::Null,
        }),
    )]);
    ambiguous_parent.id = "parent".into();
    let mut ambiguous_packs = packs.clone();
    ambiguous_packs.push(pack(
        "local.parent",
        BTreeMap::from([(
            "parent.json".into(),
            serde_json::to_vec(&ambiguous_parent).unwrap(),
        )]),
        vec![],
        vec![],
        vec![],
        vec!["parent.json".into()],
        vec![],
    ));
    has_error(
        compile(&profile(&ambiguous_packs), &ambiguous_packs),
        "subflow-ambiguous",
    );
    let mut request = flow(vec![Step::new(
        "sub",
        StepKind::Flow(SubFlow {
            implements: ShapeRef::parse("local.contracts.workflow@2.0").unwrap(),
            args: CanonValue::Null,
        }),
    )]);
    request.id = "parent".into();
    let packs = vec![
        contracts(),
        provider("local.provider"),
        flow_pack(&sub),
        pack(
            "local.parent",
            BTreeMap::from([("parent.json".into(), serde_json::to_vec(&request).unwrap())]),
            vec![],
            vec![],
            vec![],
            vec!["parent.json".into()],
            vec![],
        ),
    ];
    has_error(compile(&profile(&packs), &packs), "subflow-ambiguous");
}

#[test]
fn overlay_parallel_without_next_compiles_but_parallel_next_is_rejected() {
    let packs = vec![
        contracts(),
        provider("local.provider"),
        flow_pack(&flow(vec![call("query")])),
    ];
    let mut p = profile(&packs);
    let parallel = Step::new(
        "parallel",
        StepKind::Parallel(Parallel {
            for_each: CanonValue::Arr(vec!["one".into(), "two".into()]),
            binding: "item".into(),
            max: 2,
            body: vec![Step::new(
                "pure",
                StepKind::Branch(Branch {
                    condition: CanonValue::Bool(true),
                    then: vec![],
                    otherwise: vec![],
                }),
            )],
        }),
    );
    let next = Step::new(
        "next",
        StepKind::Next(Next {
            args: "$args".into(),
        }),
    );
    p.user_overlays.push(overlay(
        "parallel",
        OverlayTarget::Step {
            flow: "local.flow/turn".into(),
            step: "query".into(),
        },
        vec![parallel, next.clone()],
    ));
    compile(&p, &packs).unwrap();
    if let StepKind::Parallel(parallel) = &mut p.user_overlays[0].body[0].kind {
        parallel.body.push(next);
    }
    has_error(compile(&p, &packs), "multiple-next");
}

#[test]
fn compatible_minors_bind_from_verified_snapshots_unknown_minors_are_rejected() {
    let original = contracts();
    let mut files = original.files.clone();
    files.insert("wit/newer.wit".into(),b"package local:contracts@1.1.0; interface query { query: func(value: string) -> string; added: func(); }".to_vec());
    let mut definitions = original.manifest.shapes.clone();
    definitions.push(ShapeDefinition {
        name: reference("query").name,
        version: ShapeVersion::new(1, 1),
        kind: ShapeKind::Port,
        wit: "wit/newer.wit".into(),
    });
    let contracts = pack(
        "local.contracts",
        files,
        definitions,
        vec![],
        vec![],
        vec![],
        vec![],
    );
    let mut newer = port("query", false);
    newer.version = ShapeVersion::new(1, 1);
    let provider = pack(
        "local.provider",
        BTreeMap::new(),
        vec![],
        vec![newer.clone()],
        vec![],
        vec![],
        vec![],
    );
    let consumer = pack(
        "local.consumer",
        BTreeMap::new(),
        vec![],
        vec![],
        vec![port("query", false)],
        vec![],
        vec![],
    );
    let mut packs = vec![contracts, provider, consumer];
    let compiled = compile(&profile(&packs), &packs).unwrap();
    assert_eq!(
        compiled.bindings[&reference("query")],
        vec![PackId::parse("local.provider").unwrap()]
    );
    let added = OpName::parse("added").unwrap();
    let newer_ref = ShapeRef::new(reference("query").name, ShapeVersion::new(1, 1));
    assert!(
        compiled
            .view_operation(&reference("query"), &added)
            .is_none()
    );
    assert!(compiled.view_operation(&newer_ref, &added).is_some());
    let old_policy = resolve_permission_views(
        &std::collections::BTreeSet::from([reference("query").to_string()]),
        &compiled.views,
    )
    .unwrap();
    assert!(!old_policy[&reference("query")].contains(&added));
    let mut forged = flow(vec![call("added")]);
    if let StepKind::Call(call) = &mut forged.steps[0].kind {
        call.op = added;
    }
    let mut forged_packs = packs.clone();
    forged_packs.push(flow_pack(&forged));
    has_error(
        compile(&profile(&forged_packs), &forged_packs),
        "caller-view-operation",
    );
    newer.version = ShapeVersion::new(1, 99);
    packs[1] = pack(
        "local.provider",
        BTreeMap::new(),
        vec![],
        vec![newer],
        vec![],
        vec![],
        vec![],
    );
    has_error(compile(&profile(&packs), &packs), "untyped-port");
}

#[test]
fn omitted_empty_ports_and_absent_pack_limits_deny_calls_despite_routes() {
    let packs = vec![
        contracts(),
        provider("local.provider"),
        flow_pack(&flow(vec![call("query")])),
    ];
    let p = profile(&packs);
    let owner = PackId::parse("local.flow").unwrap();
    let compiled = compile(&p, &packs).unwrap();
    assert!(compiled.bindings.contains_key(&reference("query")));

    let mut missing_limit = p.clone();
    missing_limit.pack_limits.remove(&owner);
    has_error(compile(&missing_limit, &packs), "port-permission");
    let mut all_limits_missing = p.clone();
    all_limits_missing.pack_limits.clear();
    has_error(compile(&all_limits_missing, &packs), "port-permission");
    let mut json = serde_json::to_value(&p).unwrap();
    json.as_object_mut().unwrap().remove("pack_limits");
    assert!(parse_profile(&serde_json::to_vec(&json).unwrap()).is_err());
    let mut empty_limit = p.clone();
    empty_limit
        .pack_limits
        .insert(owner.clone(), PermissionSet::default());
    has_error(compile(&empty_limit, &packs), "port-permission");

    let mut json = serde_json::to_value(&p).unwrap();
    json["pack_limits"][owner.as_str()]
        .as_object_mut()
        .unwrap()
        .remove("ports");
    let omitted_ports = parse_profile(&serde_json::to_vec(&json).unwrap()).unwrap();
    assert!(omitted_ports.pack_limits[&owner].ports.is_empty());
    has_error(compile(&omitted_ports, &packs), "port-permission");

    let mut empty_ceiling = p.clone();
    empty_ceiling.ceiling.ports.clear();
    for limits in empty_ceiling.pack_limits.values_mut() {
        limits.ports.clear();
    }
    has_error(compile(&empty_ceiling, &packs), "port-permission");
    let mut json = serde_json::to_value(&empty_ceiling).unwrap();
    json["ceiling"].as_object_mut().unwrap().remove("ports");
    let omitted_ceiling = parse_profile(&serde_json::to_vec(&json).unwrap()).unwrap();
    has_error(compile(&omitted_ceiling, &packs), "port-permission");

    let mut undeclared = packs.clone();
    undeclared[2] = with_permissions(undeclared[2].clone(), PermissionSet::default());
    let declaration_denied = profile(&undeclared);
    has_error(compile(&declaration_denied, &undeclared), "port-permission");
}

#[test]
fn binding_and_endpoint_exposure_do_not_create_policy_operands() {
    let packs: Vec<_> = [
        contracts(),
        provider("local.provider"),
        pack(
            "local.consumer",
            BTreeMap::new(),
            vec![],
            vec![],
            vec![port("query", false)],
            vec![],
            vec![],
        ),
    ]
    .into_iter()
    .map(|pack| with_permissions(pack, PermissionSet::default()))
    .collect();
    let mut p = profile(&packs);
    let default_revision = compile(&p, &packs).unwrap().plan_digest;
    let mut declared = p.clone();
    declared.user_permissions = Some(declared.ceiling.clone());
    assert_ne!(
        default_revision,
        compile(&declared, &packs).unwrap().plan_digest
    );
    p.ceiling.ports.clear();
    let automatic = compile(&p, &packs).unwrap();
    p.bindings.insert(
        "local.contracts.query@1".into(),
        PackId::parse("local.provider").unwrap(),
    );
    let explicit = compile(&p, &packs).unwrap();
    assert_eq!(automatic.bindings, explicit.bindings);
    assert!(
        explicit
            .view_operation(&reference("query"), &OpName::parse("query").unwrap())
            .is_some()
    );
    assert!(explicit.profile.ceiling.ports.is_empty());
    assert!(explicit.profile.user_permissions.is_none());
    for limits in explicit.profile.pack_limits.values() {
        assert!(
            resolve_permission_views(&limits.ports, &explicit.views)
                .unwrap()
                .is_empty()
        );
    }
    for pack in &packs {
        assert!(
            resolve_permission_views(&pack.manifest.permissions.ports, &explicit.views)
                .unwrap()
                .is_empty()
        );
    }
}

#[test]
fn user_declaration_is_optional_published_and_intersected_with_ceiling() {
    let packs = vec![
        contracts(),
        provider("local.provider"),
        flow_pack(&flow(vec![call("query")])),
    ];
    let mut p = profile(&packs);
    let omitted = serde_json::to_value(&p).unwrap();
    assert!(omitted.get("user_permissions").is_none());
    assert!(
        parse_profile(&serde_json::to_vec(&omitted).unwrap())
            .unwrap()
            .user_permissions
            .is_none()
    );
    p.user_overlays.push(overlay(
        "effect",
        OverlayTarget::Step {
            flow: "local.flow/turn".into(),
            step: "query".into(),
        },
        vec![
            call("user-call"),
            Step::new(
                "next",
                StepKind::Next(Next {
                    args: "$args".into(),
                }),
            ),
        ],
    ));
    has_error(compile(&p, &packs), "port-permission");
    let mut empty = serde_json::to_value(&p).unwrap();
    empty["user_permissions"] = serde_json::json!({});
    let empty = parse_profile(&serde_json::to_vec(&empty).unwrap()).unwrap();
    assert!(empty.user_permissions.as_ref().unwrap().ports.is_empty());
    has_error(compile(&empty, &packs), "port-permission");

    p.user_permissions = Some(p.ceiling.clone());
    let compiled = compile(&p, &packs).unwrap();
    assert!(
        compiled.flows["local.flow/turn"].ordered_overlays["query"][0]
            .owner
            .is_none()
    );
    assert!(
        compiled.flows["local.flow/turn"].ordered_overlays["query"][0]
            .hash
            .is_none()
    );
    let published = parse_published(&serde_json::to_vec(&p.published()).unwrap()).unwrap();
    let imported = published.to_profile(ProfileId::parse("fresh-user").unwrap(), "Fresh User");
    assert_eq!(
        serde_json::to_value(&imported.user_permissions).unwrap(),
        serde_json::to_value(&p.user_permissions).unwrap()
    );
    assert!(
        serde_json::to_value(&published)
            .unwrap()
            .get("approvals")
            .is_none()
    );
    p.ceiling.ports.remove(&reference("query").to_string());
    for limits in p.pack_limits.values_mut() {
        limits.ports.remove(&reference("query").to_string());
    }
    has_error(compile(&p, &packs), "port-permission");
}

#[test]
fn original_policy_references_require_exact_verified_views_independently() {
    let original = vec![contracts(), provider("local.provider")];
    let missing = "local.contracts.query@1.99".to_owned();
    for operand in ["ceiling", "limit", "manifest", "user"] {
        let mut packs = original.clone();
        if operand == "manifest" {
            let mut declaration = packs[1].manifest.permissions.clone();
            declaration.ports.insert(missing.clone());
            packs[1] = with_permissions(packs[1].clone(), declaration);
        }
        let mut p = profile(&packs);
        match operand {
            "ceiling" => {
                p.ceiling.ports.insert(missing.clone());
            }
            "limit" => {
                p.pack_limits
                    .get_mut(&PackId::parse("local.provider").unwrap())
                    .unwrap()
                    .ports
                    .insert(missing.clone());
            }
            "user" => {
                let mut user = PermissionSet::default();
                user.ports.insert(missing.clone());
                p.user_permissions = Some(user);
            }
            _ => (),
        }
        has_error(compile(&p, &packs), "policy-view");
    }
    let mut malformed = profile(&original);
    let mut declaration = PermissionSet::default();
    declaration.ports.insert("local.contracts.query@1".into());
    malformed.user_permissions = Some(declaration);
    has_error(compile(&malformed, &original), "policy-view");
}

#[test]
fn structural_role_admission_does_not_require_a_callable_operation() {
    let original = contracts();
    let mut files = original.files.clone();
    files.insert("wit/contract.wit".into(),b"package local:contracts@1.0.0; interface query { query: func(value: string) -> string; } interface role {} interface workflow { run: func(); } interface input { read: func() -> string; }".to_vec());
    let contracts = pack(
        "local.contracts",
        files,
        original.manifest.shapes,
        vec![],
        vec![],
        vec![],
        vec![],
    );
    let doc = overlay(
        "wrap",
        OverlayTarget::Role {
            role: reference("role"),
        },
        vec![Step::new(
            "next",
            StepKind::Next(Next {
                args: "$args".into(),
            }),
        )],
    );
    let overlay = pack(
        "local.overlay",
        BTreeMap::from([("wrap.json".into(), serde_json::to_vec(&doc).unwrap())]),
        vec![],
        vec![],
        vec![],
        vec![],
        vec!["wrap.json".into()],
    );
    let packs = vec![
        contracts,
        provider("local.provider"),
        flow_pack(&flow(vec![call("query")])),
        overlay,
    ];
    let compiled = compile(&profile(&packs), &packs).unwrap();
    assert!(compiled.views[&reference("role")].functions.is_empty());
    let role_policy = resolve_permission_views(
        &std::collections::BTreeSet::from([reference("role").to_string()]),
        &compiled.views,
    )
    .unwrap();
    assert!(role_policy[&reference("role")].is_empty());
    assert_eq!(
        compiled.flows["local.flow/turn"].ordered_overlays["query"].len(),
        1
    );
}

#[test]
fn overlay_effects_and_next_keep_separate_original_pack_authority() {
    let doc = overlay(
        "wrap",
        OverlayTarget::Role {
            role: reference("role"),
        },
        vec![
            call("side-effect"),
            Step::new(
                "next",
                StepKind::Next(Next {
                    args: "$args".into(),
                }),
            ),
        ],
    );
    let overlay = pack(
        "local.overlay",
        BTreeMap::from([("wrap.json".into(), serde_json::to_vec(&doc).unwrap())]),
        vec![],
        vec![],
        vec![port("query", false)],
        vec![],
        vec!["wrap.json".into()],
    );
    let packs = vec![
        contracts(),
        provider("local.provider"),
        flow_pack(&flow(vec![call("query")])),
        overlay,
    ];
    let p = profile(&packs);
    let compiled = compile(&p, &packs).unwrap();
    let resolved = &compiled.flows["local.flow/turn"];
    assert_eq!(resolved.owner.as_str(), "local.flow");
    assert_eq!(
        resolved.ordered_overlays["query"][0]
            .owner
            .as_ref()
            .unwrap()
            .as_str(),
        "local.overlay"
    );
    for owner in ["local.flow", "local.overlay"] {
        let mut denied = p.clone();
        denied
            .pack_limits
            .get_mut(&PackId::parse(owner).unwrap())
            .unwrap()
            .ports
            .remove(&reference("query").to_string());
        has_error(compile(&denied, &packs), "port-permission");
    }
}

#[test]
fn subflow_endpoint_requires_explicit_policy_without_consumes_inference() {
    let mut child = flow(vec![]);
    child.implements = Some(reference("workflow"));
    let mut parent = flow(vec![Step::new(
        "child",
        StepKind::Flow(SubFlow {
            implements: reference("workflow"),
            args: CanonValue::Null,
        }),
    )]);
    parent.id = "parent".into();
    let mut caller = pack(
        "local.parent",
        BTreeMap::from([("parent.json".into(), serde_json::to_vec(&parent).unwrap())]),
        vec![],
        vec![],
        vec![],
        vec!["parent.json".into()],
        vec![],
    );
    let mut declaration = caller.manifest.permissions.clone();
    declaration.ports.insert(reference("workflow").to_string());
    caller = with_permissions(caller, declaration);
    let child = pack(
        "local.flow",
        BTreeMap::from([("turn.json".into(), serde_json::to_vec(&child).unwrap())]),
        vec![],
        vec![],
        vec![],
        vec!["turn.json".into()],
        vec![],
    );
    let packs = vec![contracts(), child, caller];
    let mut p = profile(&packs);
    p.ceiling.ports.insert(reference("workflow").to_string());
    let owner = PackId::parse("local.parent").unwrap();
    p.pack_limits
        .get_mut(&owner)
        .unwrap()
        .ports
        .insert(reference("workflow").to_string());
    assert!(packs[2].manifest.ports.consumes.is_empty());
    compile(&p, &packs).unwrap();
    p.pack_limits
        .get_mut(&owner)
        .unwrap()
        .ports
        .remove(&reference("workflow").to_string());
    has_error(compile(&p, &packs), "port-permission");
}

#[test]
fn pinned_literal_flow_call_declares_request_without_consumes_or_new_rights() {
    let doc = flow(vec![call("query")]);
    let owner = pack(
        "local.flow",
        BTreeMap::from([("turn.json".into(), serde_json::to_vec(&doc).unwrap())]),
        vec![],
        vec![],
        vec![],
        vec!["turn.json".into()],
        vec![],
    );
    assert!(owner.manifest.ports.consumes.is_empty());
    let packs = vec![contracts(), provider("local.provider"), owner];
    let mut p = profile(&packs);
    let compiled = compile(&p, &packs).unwrap();
    assert_eq!(compiled.flows["local.flow/turn"].hash, packs[2].hash);
    assert!(
        compiled
            .view_operation(&reference("query"), &OpName::parse("query").unwrap())
            .is_some()
    );
    p.pack_limits
        .get_mut(&PackId::parse("local.flow").unwrap())
        .unwrap()
        .ports
        .clear();
    has_error(compile(&p, &packs), "port-permission");
}

#[test]
fn witnesses_bind_original_ast_owner_revision_node_and_exact_frozen_target() {
    let packs = vec![
        contracts(),
        provider("local.provider"),
        flow_pack(&flow(vec![call("query")])),
    ];
    let p = profile(&packs);
    let compiled = compile(&p, &packs).unwrap();
    let key = CallNodeKey {
        code: CodeLocation::Flow,
        node: "query".into(),
        kind: CallNodeKind::Call,
    };
    let witness = compiled.call_witness("local.flow/turn", &key).unwrap();
    assert_eq!(witness.profile_revision(), &compiled.plan_digest);
    assert_eq!(
        witness.owner(),
        &CodeOwner::Pack {
            id: packs[2].manifest.id.clone(),
            hash: packs[2].hash.clone()
        }
    );
    assert_eq!(witness.shape(), &reference("query"));
    assert_eq!(witness.op().as_str(), "query");
    assert_eq!(
        witness.targets(),
        &[FrozenTarget {
            pack: packs[1].manifest.id.clone(),
            hash: packs[1].hash.clone(),
            provided: reference("query")
        }]
    );
    assert_eq!(
        witness.id(),
        compile(&p, &packs)
            .unwrap()
            .call_witness("local.flow/turn", &key)
            .unwrap()
            .id()
    );
    assert!(
        compiled
            .call_witness(
                "local.flow/turn",
                &CallNodeKey {
                    code: CodeLocation::UserOverlay {
                        id: "user:forged".into()
                    },
                    node: "query".into(),
                    kind: CallNodeKind::Call
                }
            )
            .is_none()
    );
}

#[test]
fn user_overlay_witness_is_user_code_and_next_has_no_forged_call_descriptor() {
    let packs = vec![
        contracts(),
        provider("local.provider"),
        flow_pack(&flow(vec![call("query")])),
    ];
    let mut p = profile(&packs);
    p.user_permissions = Some(p.ceiling.clone());
    p.user_overlays.push(overlay(
        "wrap",
        OverlayTarget::Step {
            flow: "local.flow/turn".into(),
            step: "query".into(),
        },
        vec![
            call("own"),
            Step::new(
                "next",
                StepKind::Next(Next {
                    args: "$args".into(),
                }),
            ),
        ],
    ));
    let compiled = compile(&p, &packs).unwrap();
    let key = CallNodeKey {
        code: CodeLocation::UserOverlay {
            id: "user:wrap".into(),
        },
        node: "own".into(),
        kind: CallNodeKind::Call,
    };
    assert_eq!(
        compiled
            .call_witness("local.flow/turn", &key)
            .unwrap()
            .owner(),
        &CodeOwner::User
    );
    let next = CallNodeKey {
        node: "next".into(),
        ..key
    };
    assert!(compiled.call_witness("local.flow/turn", &next).is_none());
}

#[test]
fn literal_stream_emit_has_its_own_exact_witness_without_consumes() {
    let original = contracts();
    let mut files = original.files.clone();
    files.insert(
        "wit/stream.wit".into(),
        b"package local:contracts@1.0.0; interface sink { emit: func(value: string); }".to_vec(),
    );
    files.insert(
        "wit/sink.shape.json".into(),
        br#"{"cardinality":"one","streams":{},"callbacks":{}}"#.to_vec(),
    );
    let sink = reference("sink");
    let mut definitions = original.manifest.shapes;
    definitions.push(ShapeDefinition {
        name: sink.name.clone(),
        version: sink.version,
        kind: ShapeKind::Port,
        wit: "wit/stream.wit".into(),
    });
    let contract = pack(
        "local.contracts",
        files,
        definitions,
        vec![],
        vec![],
        vec![],
        vec![],
    );
    let provider = pack(
        "local.provider",
        std::collections::BTreeMap::new(),
        vec![],
        vec![port("query", false), port("sink", false)],
        vec![],
        vec![],
        vec![],
    );
    let mut step = call("query");
    step.stream = Some(StreamRoute {
        emit: sink.clone(),
        collect: None,
    });
    let doc = flow(vec![step]);
    let mut caller = pack(
        "local.flow",
        BTreeMap::from([("turn.json".into(), serde_json::to_vec(&doc).unwrap())]),
        vec![],
        vec![],
        vec![],
        vec!["turn.json".into()],
        vec![],
    );
    let mut declaration = caller.manifest.permissions.clone();
    declaration.ports.insert(sink.to_string());
    caller = with_permissions(caller, declaration);
    let packs = vec![contract, provider, caller];
    let mut p = profile(&packs);
    p.ceiling.ports.insert(sink.to_string());
    p.pack_limits
        .get_mut(&PackId::parse("local.flow").unwrap())
        .unwrap()
        .ports
        .insert(sink.to_string());
    let compiled = compile(&p, &packs).unwrap();
    let key = CallNodeKey {
        code: CodeLocation::Flow,
        node: "query".into(),
        kind: CallNodeKind::StreamEmit,
    };
    let witness = compiled.call_witness("local.flow/turn", &key).unwrap();
    assert_eq!(witness.shape(), &sink);
    assert_eq!(witness.op().as_str(), "emit");
}

fn normalized_overlay_digest(document: &OverlayDocument) -> Digest {
    let bytes = serde_json::to_vec(document).unwrap();
    let value = tobkiri_canon::parse(&bytes, tobkiri_canon::Variant::StrictV1).unwrap();
    tobkiri_canon::digest(&value, tobkiri_canon::Variant::StrictV1).unwrap()
}

#[test]
fn witnessless_user_return_and_next_overlays_have_exact_normalized_ast_hashes() {
    let packs = vec![
        contracts(),
        provider("local.provider"),
        flow_pack(&flow(vec![call("query")])),
    ];
    for kind in [
        StepKind::Return(CanonValue::Null),
        StepKind::Next(Next {
            args: "$args".into(),
        }),
    ] {
        let mut node = Step::new("will-be-generated", kind);
        node.id = None;
        let mut p = profile(&packs);
        p.user_overlays.push(overlay(
            "plain",
            OverlayTarget::Step {
                flow: "local.flow/turn".into(),
                step: "query".into(),
            },
            vec![node],
        ));
        let compiled = compile(&p, &packs).unwrap();
        let resolved = &compiled.flows["local.flow/turn"];
        let overlay = &resolved.ordered_overlays["query"][0];
        assert_eq!(
            overlay.document.body[0].id.as_deref(),
            Some("auto-overlay-0")
        );
        assert_eq!(
            overlay.ast_hash(),
            &normalized_overlay_digest(&overlay.document)
        );
        assert_ne!(
            overlay.ast_hash(),
            &normalized_overlay_digest(&p.user_overlays[0])
        );
        assert!(overlay.owner.is_none() && overlay.hash.is_none());
        assert!(
            !resolved
                .call_witnesses
                .values()
                .any(|w| matches!(&w.key().code, CodeLocation::UserOverlay { .. }))
        );
        let repeated = compile(&p, &packs).unwrap();
        assert_eq!(
            overlay.ast_hash(),
            repeated.flows["local.flow/turn"].ordered_overlays["query"][0].ast_hash()
        );
        let mut changed = p;
        changed.user_overlays[0].body =
            vec![Step::new("different", StepKind::Return("changed".into()))];
        let changed = compile(&changed, &packs).unwrap();
        assert_ne!(
            overlay.ast_hash(),
            changed.flows["local.flow/turn"].ordered_overlays["query"][0].ast_hash()
        );
    }
}

#[test]
fn pack_and_user_overlay_witnesses_reuse_their_resolved_ast_hash() {
    for user in [false, true] {
        let target = if user {
            OverlayTarget::Step {
                flow: "local.flow/turn".into(),
                step: "query".into(),
            }
        } else {
            OverlayTarget::Role {
                role: reference("role"),
            }
        };
        let doc = overlay(
            "wrap",
            target,
            vec![
                call("effect"),
                Step::new(
                    "next",
                    StepKind::Next(Next {
                        args: "$args".into(),
                    }),
                ),
            ],
        );
        let mut packs = vec![
            contracts(),
            provider("local.provider"),
            flow_pack(&flow(vec![call("query")])),
        ];
        if !user {
            packs.push(pack(
                "local.overlay",
                BTreeMap::from([("wrap.json".into(), serde_json::to_vec(&doc).unwrap())]),
                vec![],
                vec![],
                vec![port("query", false)],
                vec![],
                vec!["wrap.json".into()],
            ));
        }
        let mut p = profile(&packs);
        if user {
            p.user_permissions = Some(p.ceiling.clone());
            p.user_overlays.push(doc);
        }
        let compiled = compile(&p, &packs).unwrap();
        let flow = &compiled.flows["local.flow/turn"];
        let overlay = &flow.ordered_overlays["query"][0];
        let witness = flow.call_witnesses.values().find(|w| {
            matches!(&w.key().code, CodeLocation::UserOverlay { id } if id == "user:wrap")
                || matches!(&w.key().code, CodeLocation::PackOverlay { id } if id == "local.overlay/wrap")
        }).unwrap();
        assert_eq!(witness.ast_hash(), overlay.ast_hash());
        assert_eq!(
            overlay.ast_hash(),
            &normalized_overlay_digest(&overlay.document)
        );
        if user {
            assert!(overlay.hash.is_none());
            assert_eq!(witness.owner(), &CodeOwner::User);
        } else {
            let source = packs
                .iter()
                .find(|p| p.manifest.id.as_str() == "local.overlay")
                .unwrap();
            assert_eq!(overlay.hash.as_ref(), Some(&source.hash));
            assert_ne!(overlay.ast_hash(), source.hash.digest());
            assert_eq!(
                witness.owner(),
                &CodeOwner::Pack {
                    id: source.manifest.id.clone(),
                    hash: source.hash.clone()
                }
            );
        }
    }
}
