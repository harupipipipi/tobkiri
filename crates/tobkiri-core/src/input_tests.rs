use super::*;
use crate::test_support::within_session;
use std::{
    sync::Arc,
    time::{Duration, Instant},
};
use tobkiri_canon::{
    CanonValue,
    ids::{PackHash, RequestId},
};
use tobkiri_gate::{
    ApprovalStore, CallCx, CoreControl, EffectRequest, FlowEndpointRegistration, Gate, GateEngine,
    MediatorChain, Principal,
};
use tobkiri_profile::CompiledProfile;
use tobkiri_store::{Approvals, AuditLog, AuditQuota, DataRoot};

struct Fixture {
    compiled: CompiledProfile,
    packs: Vec<VerifiedPack>,
    gate: Arc<GateEngine>,
    cx: CallCx,
    source: InputSourceId,
    other_source: InputSourceId,
    input: ShapeRef,
    flow: ShapeRef,
    other_flow: ShapeRef,
    approvals: Arc<Approvals>,
    audit_path: std::path::PathBuf,
    _endpoints: FlowEndpointRegistration,
    _directory: tempfile::TempDir,
}
struct StaticRejection {
    conflicts: Vec<tobkiri_profile::Conflict>,
    directory: tempfile::TempDir,
}
fn shape(namespace: &str, name: &str) -> ShapeRef {
    ShapeRef::parse(&format!("local.{namespace}.{name}@1.0")).unwrap()
}
fn fixture(missing: Option<&str>) -> Fixture {
    fixture_checked(missing)
        .unwrap_or_else(|rejected| panic!("unexpected static rejection: {:?}", rejected.conflicts))
}
fn fixture_checked(missing: Option<&str>) -> Result<Fixture, StaticRejection> {
    let directory = tempfile::tempdir().unwrap();
    let physical = directory.path().canonicalize().unwrap();
    let permission = crate::test_support::permissions(&["fixture", "other"]);
    let input = shape("fixture", "input");
    let flow = shape("fixture", "turn");
    let other_flow = shape("other", "turn");
    let mut declaration = permission.clone();
    if missing == Some("declaration") {
        declaration.ports.remove(&flow.to_string());
    }
    let packs = vec![
        crate::test_support::runtime_pack(&physical, "fixture", declaration, false),
        crate::test_support::runtime_pack(&physical, "other", permission.clone(), false),
    ];
    let mut profile =
        crate::test_support::runtime_profile(&physical, &packs, permission.clone(), false);
    match missing {
        Some("ceiling") => {
            profile.ceiling.ports.remove(&flow.to_string());
            // A legal explicit limit must remain a subset of the ceiling.
            for limits in profile.pack_limits.values_mut() {
                *limits = limits.intersect(&profile.ceiling);
            }
        }
        Some("pack-limit") => {
            profile
                .pack_limits
                .get_mut(&packs[0].manifest.id)
                .unwrap()
                .ports
                .remove(&flow.to_string());
        }
        Some("absent-pack-limit") => {
            profile.pack_limits.remove(&packs[0].manifest.id);
        }
        _ => {}
    }
    let compiled = match tobkiri_profile::compile(&profile, &packs) {
        Ok(compiled) => compiled,
        Err(conflicts) => {
            // A static denial precedes creation of runtime authority, audit
            // storage or an invoker. Preserve the directory for that assertion.
            return Err(StaticRejection {
                conflicts,
                directory,
            });
        }
    };
    let root = DataRoot::open(physical.join("data")).unwrap();
    let audit_path = root
        .path()
        .join(DataRoot::profile_relative(&profile.id, "audit.sqlite").unwrap());
    let approvals = Arc::new(Approvals::open(&root, &profile.id).unwrap());
    let audit = Arc::new(AuditLog::open(&root, &profile.id, AuditQuota::default()).unwrap());
    let control = CoreControl::default();
    let gate = Arc::new(GateEngine::new_with_control(
        approvals.clone(),
        audit,
        Arc::new(crate::test_support::RuntimeHost),
        MediatorChain::default(),
        control.clone(),
    ));
    for (index, pack) in packs.iter().enumerate() {
        let mut approved = permission.clone();
        if index == 0 && missing == Some("approval") {
            approved.ports.remove(&flow.to_string());
        }
        approvals
            .set_permissions(&profile.id, &pack.manifest.id, &pack.hash, &approved)
            .unwrap();
    }
    crate::core::register_policies(&gate, &profile, &packs).unwrap();
    gate.register_revision(
        &profile.id,
        &compiled.plan_digest,
        packs
            .iter()
            .map(|pack| {
                (
                    (pack.manifest.id.clone(), pack.hash.clone()),
                    profile
                        .pack_limits
                        .get(&pack.manifest.id)
                        .map(|limits| limits.wasm.clone())
                        .unwrap_or_default(),
                )
            })
            .collect(),
    )
    .unwrap();
    crate::test_support::register_views(&gate, &compiled);
    let bindings = compiled
        .bindings
        .iter()
        .map(|(view, providers)| {
            (
                view.clone(),
                providers
                    .iter()
                    .map(|provider| {
                        let pack = packs
                            .iter()
                            .find(|pack| &pack.manifest.id == provider)
                            .unwrap();
                        let declaration = pack
                            .manifest
                            .ports
                            .provides
                            .iter()
                            .find(|port| port.shape == view.name && port.version == view.version)
                            .unwrap();
                        tobkiri_gate::FrozenPortTarget {
                            pack: provider.clone(),
                            hash: pack.hash.clone(),
                            provided: ShapeRef::new(declaration.shape.clone(), declaration.version),
                        }
                    })
                    .collect(),
            )
        })
        .collect();
    gate.register_port_bindings(&profile.id, &compiled.plan_digest, bindings)
        .unwrap();
    let document = Digest::hash(&crate::profiles::strict_bytes(&profile).unwrap());
    let epoch = gate.epoch(&profile.id).unwrap();
    let empty_user = tobkiri_gate::PermissionSet::default();
    gate.register_user_policy_for_revision(
        &profile.id,
        &document,
        &compiled.plan_digest,
        epoch,
        empty_user.clone(),
        profile.ceiling.clone(),
    )
    .unwrap();
    approvals
        .set_user_permissions(
            &profile.id,
            &document,
            &compiled.plan_digest,
            epoch,
            &empty_user,
        )
        .unwrap();
    let endpoints = crate::execution_frames::register_flow_endpoints(&compiled, &gate).unwrap();
    let cx = CallCx::try_root_with_control(
        profile.id.clone(),
        Principal::User,
        RequestId::parse("req_projection").unwrap(),
        tobkiri_gate::CancellationToken::new_with_control(&control).unwrap(),
        Instant::now() + Duration::from_secs(1),
        permission.wasm.clone(),
        control,
    )
    .unwrap()
    .with_security_epoch(epoch)
    .with_revision(compiled.plan_digest.clone());
    Ok(Fixture {
        source: InputSourceId::parse(packs[0].manifest.id.as_str()).unwrap(),
        other_source: InputSourceId::parse(packs[1].manifest.id.as_str()).unwrap(),
        compiled,
        packs,
        gate,
        cx,
        input,
        flow,
        other_flow,
        approvals,
        audit_path,
        _endpoints: endpoints,
        _directory: directory,
    })
}
impl Fixture {
    fn audit_events(&self) -> Vec<tobkiri_gate::AuditEvent> {
        let connection = rusqlite::Connection::open_with_flags(
            &self.audit_path,
            rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
        )
        .unwrap();
        let mut statement = connection
            .prepare("SELECT body FROM audit ORDER BY seq")
            .unwrap();
        statement
            .query_map([], |row| row.get::<_, Vec<u8>>(0))
            .unwrap()
            .map(|body| serde_json::from_slice(&body.unwrap()).unwrap())
            .collect()
    }
    fn source_cx(&self, source: InputSourceId) -> CallCx {
        self.cx.child(Principal::Input { source })
    }
    fn flow_request(&self, other: bool) -> EffectRequest {
        let (implements, key) = if other {
            (&self.other_flow, "local.other/main")
        } else {
            (&self.flow, "local.fixture/main")
        };
        let target = self
            .gate
            .resolve_frozen_flow_target(&self.cx, implements, Some(key))
            .unwrap();
        EffectRequest::FlowRun {
            implements: implements.clone(),
            flow_owner: (target.owner.clone(), target.owner_hash.clone()),
            flow_key: target.flow_key.clone(),
            target_digest: target.digest().unwrap(),
        }
    }
}

#[tokio::test]
async fn explicit_four_way_input_flow_authority_dispatches_and_records_outcome() {
    let fixture = crate::test_support::RuntimeFixture::new(&["fixture"], false).await;
    let session = fixture.session().await;
    let entry = session.input_entries()[0].clone();
    let input = within_session(
        &session,
        "physical Input read",
        session.read_input(
            InputSourceId::parse(entry.source_pack().as_str()).unwrap(),
            entry.exact_input_view().clone(),
            entry.operation().clone(),
        ),
    )
    .await
    .unwrap();
    let mut run = within_session(
        &session,
        "NativeInput admission",
        session.submit_native_input(input),
    )
    .await
    .unwrap();
    assert_eq!(run.routes().len(), 1);
    let route = &run.routes()[0];
    let request = EffectRequest::FlowRun {
        implements: route.implements.clone(),
        flow_owner: (route.target.owner.clone(), route.target.owner_hash.clone()),
        flow_key: route.target.flow_key.clone(),
        target_digest: route.target.digest().unwrap(),
    };
    let expected = CanonValue::Str(request.digest().unwrap().to_string());
    let (result, completed, failures) =
        within_session(&session, "NativeInput progress and completion", async {
            let mut completed = false;
            let mut failures = Vec::new();
            while let Some(event) = run.events.recv().await {
                assert_eq!(event.route_index, 0);
                completed |= matches!(
                    event.event.kind,
                    tobkiri_flow::RunEventKind::Completed { .. }
                );
                crate::test_support::capture_run_failure(&event.event, &mut failures);
            }
            (run.result.await.unwrap(), completed, failures)
        })
        .await;
    assert_eq!(
        result.status,
        crate::NativeInputStatus::Succeeded,
        "{}",
        crate::test_support::native_run_diagnostic(&result, &failures)
    );
    assert_eq!(
        result.routes[0].status,
        crate::NativeInputRouteStatus::Succeeded
    );
    assert!(completed);
    assert_eq!(result.buffer.as_bytes(), br#"{"routes":[{"error":null,"index":0,"result":42,"status":"succeeded"}],"status":"succeeded"}"#);
    let effects = fixture.audit_events();
    // Distinguish the Flow admission from source initialization/read and the
    // native routing terminal using its exact durable request digest.
    assert!(effects.iter().any(|event| event.kind == "dispatch" && matches!(&event.payload, CanonValue::Obj(fields) if fields.get("effect_digest") == Some(&expected))));
    assert!(effects.iter().any(|event| event.kind == "outcome" && matches!(&event.payload, CanonValue::Obj(fields) if fields.get("effect_digest") == Some(&expected))));
    AuditLog::open(&fixture.root, &fixture.profile.id, AuditQuota::default())
        .unwrap()
        .verify()
        .unwrap();
    assert_eq!(
        session
            .gate
            .flush_outbox(&fixture.profile.id)
            .await
            .unwrap(),
        0
    );
    within_session(&session, "Session cleanup", session.close())
        .await
        .unwrap();
}

#[tokio::test]
async fn removing_any_original_authority_boundary_denies_before_flow_dispatch() {
    for missing in [
        "declaration",
        "ceiling",
        "pack-limit",
        "approval",
        "absent-pack-limit",
    ] {
        let built = fixture_checked(Some(missing));
        if missing == "absent-pack-limit" {
            let rejected = match built {
                Err(rejected) => rejected,
                Ok(_) => panic!("missing explicit Pack limit must fail before runtime setup"),
            };
            // The actual frozen Flow calls this source's input port. Without
            // any explicit Pack limit the compiler correctly denies that call;
            // this case must not pretend to reach runtime ingress admission.
            assert_eq!(rejected.conflicts.len(), 1);
            let conflict = &rejected.conflicts[0];
            assert_eq!(conflict.code, "port-permission");
            assert_eq!(conflict.packs, vec!["local.fixture".to_owned()]);
            assert!(conflict.message.contains("local.fixture.input@1.0"));
            assert!(!rejected.directory.path().join("data").exists());
            continue;
        }
        let fixture = built.unwrap_or_else(|rejected| {
            panic!(
                "{missing} must reach runtime admission: {:?}",
                rejected.conflicts
            )
        });
        let ingress = register_inputs(
            &fixture.gate,
            &fixture.compiled,
            &fixture.packs,
            &fixture.cx,
        );
        let _ingress = ingress.unwrap();
        let allowed = fixture
            .gate
            .effective_permissions(&fixture.source_cx(fixture.source.clone()))
            .unwrap();
        assert!(
            !allowed.ports.contains(&fixture.flow.to_string()),
            "{missing}"
        );
        assert!(
            allowed.ports.contains(&fixture.input.to_string()),
            "{missing}"
        );
        assert!(
            fixture
                .audit_events()
                .iter()
                .all(|event| event.kind != "dispatch")
        );
        let source_cx = fixture.source_cx(fixture.source.clone());
        assert!(
            fixture
                .gate
                .authorize(
                    &source_cx,
                    fixture.flow_request(false),
                    Digest::hash(b"forged-payload")
                )
                .await
                .is_err(),
            "{missing}"
        );
        let events = fixture.audit_events();
        assert!(
            events.iter().all(|event| event.kind != "dispatch"),
            "{missing}"
        );
        assert!(
            events.iter().any(|event| {
                event.kind == "denied"
                    && &event.principal == source_cx.principal()
                    && &event.request == source_cx.request()
                    && matches!(&event.payload, CanonValue::Arr(parts)
                    if parts.first().and_then(CanonValue::as_str) == Some("flow-run"))
            }),
            "{missing}"
        );
    }
}

#[tokio::test]
async fn source_projection_does_not_inherit_other_trigger_or_payload_authority() {
    let fixture = fixture(None);
    let _ingress = register_inputs(
        &fixture.gate,
        &fixture.compiled,
        &fixture.packs,
        &fixture.cx,
    )
    .unwrap();
    for (source, other) in [
        (fixture.source.clone(), true),
        (fixture.other_source.clone(), false),
    ] {
        assert!(
            fixture
                .gate
                .authorize(
                    &fixture.source_cx(source),
                    fixture.flow_request(other),
                    Digest::hash(b"approved:true,source:forged")
                )
                .await
                .is_err()
        );
    }
    let real = crate::test_support::RuntimeFixture::new(&["fixture", "other"], false).await;
    let session = real.session().await;
    assert!(
        session
            .read_input(
                fixture.other_source.clone(),
                fixture.input.clone(),
                OpName::parse("read").unwrap()
            )
            .await
            .is_err()
    );
    assert!(
        session
            .submit(
                fixture.source.clone(),
                fixture.input.clone(),
                crate::test_support::value(&CanonValue::Obj(BTreeMap::from([(
                    "approved".into(),
                    CanonValue::Bool(true)
                )])))
            )
            .await
            .is_err()
    );
    session.close().await.unwrap();
}

#[test]
fn source_hash_pin_and_revision_cannot_be_substituted() {
    let mut fixture = fixture(None);
    let old = fixture.packs[0].hash.clone();
    let new = PackHash::new(Digest::hash(b"new-source"));
    fixture.packs[0].hash = new.clone();
    assert!(
        register_inputs(
            &fixture.gate,
            &fixture.compiled,
            &fixture.packs,
            &fixture.cx
        )
        .is_err()
    );
    fixture.compiled.profile.packs[0].sha256 = new;
    assert!(
        fixture
            .approvals
            .permissions(
                &fixture.compiled.profile.id,
                &fixture.packs[0].manifest.id,
                &old
            )
            .unwrap()
            .is_some()
    );
    assert!(
        register_inputs(
            &fixture.gate,
            &fixture.compiled,
            &fixture.packs,
            &fixture.cx
        )
        .is_err()
    );
    let control = fixture.cx.control().clone();
    let wrong = CallCx::try_root_with_control(
        fixture.compiled.profile.id.clone(),
        Principal::User,
        RequestId::parse("req_wrong").unwrap(),
        tobkiri_gate::CancellationToken::new_with_control(&control).unwrap(),
        Instant::now() + Duration::from_secs(1),
        fixture.compiled.profile.ceiling.wasm.clone(),
        control,
    )
    .unwrap()
    .with_revision(Digest::hash(b"different"));
    assert!(register_inputs(&fixture.gate, &fixture.compiled, &fixture.packs, &wrong).is_err());
}

#[tokio::test]
async fn projected_flow_target_cannot_become_a_new_input_trigger() {
    let mut fixture = fixture(None);
    fixture
        .compiled
        .flows
        .get_mut("local.other/main")
        .unwrap()
        .document
        .trigger
        .input = fixture.flow.clone();
    assert!(
        register_inputs(
            &fixture.gate,
            &fixture.compiled,
            &fixture.packs,
            &fixture.cx
        )
        .is_err()
    );
}
