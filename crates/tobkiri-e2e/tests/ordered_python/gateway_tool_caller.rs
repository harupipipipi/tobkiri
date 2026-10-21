//! Additional real-Python caller -> real-Python bridge native route.
//! Existing WAT consumers are unchanged. This is not the finalizer graph, the
//! complete Gateway, or a substitute for the historical 92f38e3 replay.
use super::{
    EXPECTED_RUNTIME, EXPECTED_UNICODE, audit_rows, canon, evidence,
    evidence_gateway_tool_caller as inputs, pack_events, parse_output, physical_temp, repo,
    require_runtime, runtime, terminal_pair_with_outcome,
};
use anyhow::{Context, Result, ensure};
use inputs::{BRIDGE, BRIDGE_SHAPE, CALLER, CALLER_SHAPE, Graph};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    path::Path,
    sync::Arc,
};
use tobkiri_canon::{
    CanonValue, CoreBuffer, Variant,
    ids::{OpName, ShapeRef},
};
use tobkiri_core::{Core, CoreConfig, CoreError, Session};
use tobkiri_gate::{EffectRequest, Outcome, PermissionSet};
use tobkiri_profile::{PackPin, Profile};

fn arguments(case: &Value) -> Result<CanonValue> {
    ensure!(
        inputs::OPERATIONS.contains(&case["operation"].as_str().context("operation missing")?),
        "unknown caller operation"
    );
    let payload = case["input_json"]
        .as_str()
        .context("opaque input missing")?;
    // Inner DATA includes floats, very large integers and significant ordering.
    // It is never parsed/reencoded through Core's StrictV1 object transport.
    canon(&json!({"payload":payload}))
}

/// Static source expectations, NOT an observation of imported calls.
fn expected_dependency_calls(case: &Value) -> Result<&[Value]> {
    let source = case["source_reference_calls"]
        .as_array()
        .context("source calls missing")?;
    if case["id"] == "events-later-nonfinite" {
        ensure!(
            case["classification"] == "provider-nonfinite" && source.len() == 3,
            "terminal-prefix source changed"
        );
        // Original direct-source continuation is deliberately distinct from
        // the imported provider's terminal finite-DATA error on event two.
        Ok(&source[..2])
    } else {
        Ok(source)
    }
}

fn public_message(code: &str) -> Option<&'static str> {
    match code {
        "denied" => Some("provider denied the operation"),
        "approval-required" => Some("provider reported that approval is required"),
        "cancelled" => Some("provider reported cancellation"),
        "not-found" => Some("provider reported a missing item"),
        "invalid" => Some("provider reported invalid input"),
        "limit-exceeded" => Some("provider reported a resource limit"),
        "conflict" => Some("provider reported a conflict"),
        "unavailable" => Some("provider reported unavailable"),
        "internal" => Some("provider reported an internal error"),
        _ => None,
    }
}

fn result_matches(case: &Value, actual: Result<CoreBuffer, CoreError>) -> Result<Value> {
    let mut record = json!({"id":case["id"],"classification":case["classification"],"observed":true,"exact_result":false,"matched":false});
    let matches = match actual {
        Ok(buffer) => match parse_output(&buffer) {
            Ok(CanonValue::Str(output)) => {
                record["actual"] = json!({"status":"ok","output_sha256":evidence::digest(output.as_bytes()),"output_bytes":output.len()});
                case["classification"] == "success"
                    && case["output_json"].as_str() == Some(output.as_str())
            }
            Ok(_) => {
                record["actual"] = json!({"status":"unexpected-output-type"});
                false
            }
            Err(error) => {
                record["actual"] =
                    json!({"status":"invalid-core-output","message":error.to_string()});
                false
            }
        },
        Err(CoreError::Step(step)) => {
            let expected = case["expected"]["code"].as_str();
            let matched = matches!(
                case["classification"].as_str(),
                Some("provider-invalid" | "provider-nonfinite")
            ) && expected == Some(step.code.as_str())
                && public_message(&step.code) == Some(step.message.as_str());
            record["actual"] = json!({"status":"step-error","code":step.code,"message":step.message,"exact_public_error":matched});
            matched
        }
        Err(error) => {
            record["actual"] =
                json!({"status":"unexpected-core-error","message":error.to_string()});
            false
        }
    };
    record["exact_result"] = json!(matches);
    Ok(record)
}

fn case_outcome(case: &Value, nested: bool) -> Result<Outcome> {
    Ok(match case["classification"].as_str() {
        Some("success") => Outcome::Succeeded,
        Some("provider-invalid" | "provider-nonfinite") => Outcome::Failed {
            code: if nested {
                "provider-error".into()
            } else {
                case["expected"]["code"]
                    .as_str()
                    .context("expected code missing")?
                    .into()
            },
        },
        _ => anyhow::bail!("unknown classification"),
    })
}

fn profile(graph: &Graph, allow: bool, runtime_probe: bool, id: &str) -> Result<Profile> {
    let mut pins = Vec::new();
    let mut limits = BTreeMap::new();
    for (name, pack) in [("bridge", &graph.bridge), ("caller", &graph.caller)] {
        pins.push(PackPin {
            id: pack.manifest.id.clone(),
            version: pack.manifest.version.clone(),
            sha256: pack.hash.clone(),
            source: format!("file:{}", graph.root.join(name).display()),
        });
        let mut permissions = pack.manifest.permissions.clone();
        if name == "caller" && !allow {
            permissions.ports.remove(BRIDGE_SHAPE);
        }
        limits.insert(pack.manifest.id.clone(), permissions);
    }
    let mut ceiling = graph.bridge.manifest.permissions.clone();
    ceiling.ports = [CALLER_SHAPE.to_owned(), BRIDGE_SHAPE.to_owned()]
        .into_iter()
        .collect();
    let user = PermissionSet {
        ports: if runtime_probe {
            [CALLER_SHAPE.to_owned(), BRIDGE_SHAPE.to_owned()]
                .into_iter()
                .collect()
        } else {
            [CALLER_SHAPE.to_owned()].into_iter().collect()
        },
        wasm: ceiling.wasm.clone(),
        loops: ceiling.loops.clone(),
        ..Default::default()
    };
    Ok(serde_json::from_value(
        json!({"format":"tobkiri.profile/1","id":id,"name":"Original Python tool caller native fixture",
        "packs":pins,"publishers":{},"ceiling":ceiling,"pack_limits":limits,"user_permissions":user,
        "bindings":{"local.ai-gateway-tool-caller-python.caller@1":CALLER,"local.ai-tool-bridge-ordered-python.bridge@2":BRIDGE},
        "overlay_order":[],"user_overlays":[],"startup":{"inputs":[],"ui":null}}),
    )?)
}

fn validate_denial_delta(allowed: &Profile, denied: &Profile) -> Result<()> {
    let mut expected = serde_json::to_value(allowed)?;
    let denied = serde_json::to_value(denied)?;
    expected["id"] = denied["id"].clone();
    expected["pack_limits"][CALLER]["ports"] = json!([]);
    ensure!(
        expected == denied,
        "denial altered more than one exact caller limit and profile id"
    );
    Ok(())
}

/// Linux-safe Profile compiler proof from the two already verified real Packs.
pub(super) fn validate_profiles(graph: &Graph) -> Result<()> {
    let allowed = profile(graph, true, false, "prof_caller_static_allow")?;
    let denied = profile(graph, false, false, "prof_caller_static_deny")?;
    validate_denial_delta(&allowed, &denied)?;
    for policy in [
        &allowed,
        &denied,
        &profile(graph, true, true, "prof_caller_static_runtime")?,
    ] {
        tobkiri_profile::compile(policy, &[graph.bridge.clone(), graph.caller.clone()])
            .map_err(|errors| anyhow::anyhow!("verified graph profile conflicts: {errors:?}"))?;
    }
    let mut crossed = denied.clone();
    crossed.ceiling.ports.clear();
    ensure!(
        validate_denial_delta(&allowed, &crossed).is_err(),
        "crossed denial policy accepted"
    );
    Ok(())
}

async fn start(data: &Path, profile: &Profile, runtime_probe: bool) -> Result<(Core, Session)> {
    let host = Arc::new(super::support::Host::default());
    for op in [
        "runtime-info",
        "generate-intents",
        "stream-intents",
        "stream-event-intents",
    ] {
        host.utility_ports
            .lock()
            .unwrap()
            .insert((CALLER_SHAPE.into(), op.into()));
    }
    if runtime_probe {
        host.utility_ports
            .lock()
            .unwrap()
            .insert((BRIDGE_SHAPE.into(), "runtime-info".into()));
    }
    host.approve_literal_user(profile)?;
    let core = Core::open(CoreConfig::for_test_data_root(data.to_path_buf()), host)?;
    core.save_profile(profile.clone(), None).await?;
    let session = core.start(&profile.id).await?;
    Ok((core, session))
}

fn finish(result: Result<()>, close: Result<(), CoreError>, report: &mut Value) -> Result<()> {
    report["session_close_awaited"] = json!(true);
    report["session_closed"] = json!(close.is_ok());
    report["guest_lifecycle_shutdown_claimed"] = json!(false);
    if let Err(error) = &result {
        report["first_error"] = json!(format!("{error:#}"));
    }
    if let Err(error) = &close {
        report["cleanup_error"] = json!(error.to_string());
    }
    match (result, close) {
        (Err(first), Err(close)) => {
            anyhow::bail!("{first:#}; additionally Session::close failed: {close}")
        }
        (Err(first), _) => Err(first),
        (Ok(()), Err(close)) => Err(close.into()),
        (Ok(()), Ok(())) => Ok(()),
    }
}

fn principal(pack: &tobkiri_manifest::VerifiedPack) -> Value {
    json!({"kind":"pack","pack":pack.manifest.id,"hash":pack.hash})
}
fn effect(shape: &str, op: &str) -> Result<EffectRequest> {
    Ok(EffectRequest::PortCall {
        shape: ShapeRef::parse(shape)?,
        op: OpName::parse(op)?,
    })
}
fn dispatch_count(rows: &[Value], effect: &EffectRequest) -> Result<usize> {
    let d = effect.digest()?.to_string();
    Ok(rows
        .iter()
        .filter(|r| r["kind"] == "dispatch" && r["payload"]["effect_digest"] == d)
        .count())
}
fn denied_count(rows: &[Value], owner: &Value) -> usize {
    rows.iter()
        .filter(|r| {
            r["kind"] == "denied"
                && r["principal"] == *owner
                && r["payload"][0] == "port-call"
                && r["payload"][1] == "denied"
        })
        .count()
}

fn initialized(rows: &[Value], pack: &tobkiri_manifest::VerifiedPack) -> Result<bool> {
    let effect = EffectRequest::WasmEntry {
        pack: pack.manifest.id.clone(),
        hash: pack.hash.clone(),
        entry: tobkiri_gate::WasmEntryKind::Init,
    };
    terminal_pair_with_outcome(
        rows,
        &principal(pack),
        &effect,
        &effect.digest()?,
        &Outcome::Succeeded,
    )
}

/// Check dynamic Core receipts, never treating reference traces as runtime proof.
fn dependency_audit(rows: &[Value], owner: &Value, case: &Value) -> Result<Value> {
    let expected = expected_dependency_calls(case)?;
    let target = effect(BRIDGE_SHAPE, "normalize")?;
    let count = dispatch_count(rows, &target)?;
    let mut records = Vec::new();
    let effect_digest = target.digest()?.to_string();
    let dispatched: Vec<_> = rows
        .iter()
        .enumerate()
        .filter(|(_, row)| {
            row["kind"] == "dispatch" && row["payload"]["effect_digest"] == effect_digest
        })
        .collect();
    for (index, call) in expected.iter().enumerate() {
        let args = canon(&json!({"operation":call["operation"],"payload":call["payload_json"]}))?;
        let digest = tobkiri_canon::digest(&args, Variant::StrictV1)?;
        let terminal = if index + 1 == expected.len() {
            case_outcome(case, true)?
        } else {
            Outcome::Succeeded
        };
        let terminal_matches =
            terminal_pair_with_outcome(rows, owner, &target, &digest, &terminal)?;
        let terminal_value = serde_json::to_value(&terminal)?;
        let ordered = dispatched.get(index).is_some_and(|(position, row)| {
            row["principal"] == *owner
                && row["payload"]["args_digest"] == digest.to_string()
                && rows.iter().enumerate().any(|(terminal_position, outcome)| {
                    outcome["kind"] == "outcome"
                        && outcome["principal"] == *owner
                        && outcome["request"] == row["request"]
                        && outcome["payload"]["grant"] == row["payload"]["grant"]
                        && outcome["payload"]["effect_digest"] == effect_digest
                        && outcome["payload"]["args_digest"] == digest.to_string()
                        && outcome["payload"]["outcome"] == terminal_value
                        && terminal_position > *position
                        && dispatched
                            .get(index + 1)
                            .is_none_or(|(next, _)| terminal_position < *next)
                })
        });
        records.push(json!({"args_digest":digest,"effect_digest":target.digest()?,"expected_outcome":terminal,"exact_terminal":terminal_matches,"ordered_dispatch_and_terminal":ordered}));
    }
    let matched = count == expected.len()
        && records
            .iter()
            .all(|r| r["exact_terminal"] == true && r["ordered_dispatch_and_terminal"] == true)
        && denied_count(rows, owner) == 0;
    Ok(
        json!({"matched":matched,"observed_bridge_dispatch_count":count,"expected_terminal_prefix_count":expected.len(),
        "original_source_reference_call_count":case["source_reference_call_count"],"source_count_is_not_runtime_evidence":true,"edges":records}),
    )
}

async fn runtime_probe(root: &Path, graph: &Graph, report: &mut Value) -> Result<()> {
    let profile = profile(graph, true, true, "prof_python_caller_runtime")?;
    let data = root.join("runtime/data");
    let (_core, session) = start(&data, &profile, true).await?;
    let result: Result<()> = async {
        for (name, shape, pack) in [
            ("caller", CALLER_SHAPE, &graph.caller),
            ("bridge", BRIDGE_SHAPE, &graph.bridge),
        ] {
            let args = CanonValue::Obj(BTreeMap::new());
            let before = audit_rows(&data, &profile)?.len();
            let value = session
                .call_user_port(
                    ShapeRef::parse(shape)?,
                    OpName::parse("runtime-info")?,
                    &args,
                )
                .await?;
            let observed = runtime(parse_output(&value)?)?;
            drop(value);
            // Preserve the observed identity even when it fails the reviewed
            // guest-version requirement below.
            report[name] = json!({"runtime":observed.clone(),"guest_runtime":observed});
            require_runtime(&report[name]["guest_runtime"])?;
            let rows = audit_rows(&data, &profile)?;
            let effect = effect(shape, "runtime-info")?;
            let terminal = terminal_pair_with_outcome(
                &rows[before..],
                &json!({"kind":"user"}),
                &effect,
                &tobkiri_canon::digest(&args, Variant::StrictV1)?,
                &Outcome::Succeeded,
            )?;
            let init = initialized(&rows, pack)?;
            report[name]["exact_terminal"] = json!(terminal);
            report[name]["provider_initialized"] = json!(init);
            ensure!(terminal && init, "actual runtime/init receipt missing");
        }
        Ok(())
    }
    .await;
    let close = session.close().await;
    finish(result, close, report)
}

async fn replay(root: &Path, graph: &Graph, report: &mut Value) -> Result<()> {
    let profile = profile(graph, true, false, "prof_python_caller_replay")?;
    let data = root.join("replay/data");
    let (_core, session) = start(&data, &profile, false).await?;
    let cases = graph.oracle["cases"].as_array().context("cases missing")?;
    report["cases"]=cases.iter().map(|c|json!({"id":c["id"],"classification":c["classification"],"observed":false,"matched":false})).collect();
    let result: Result<()> = async {
        for (index, case) in cases.iter().enumerate() {
            let args = arguments(case)?;
            let before = audit_rows(&data, &profile)?.len();
            let actual = session
                .call_user_port(
                    ShapeRef::parse(CALLER_SHAPE)?,
                    OpName::parse(case["operation"].as_str().unwrap())?,
                    &args,
                )
                .await;
            let mut record = result_matches(case, actual)?;
            // Preserve the returned observation even if a later audit check fails.
            report["cases"][index] = record.clone();
            let rows = audit_rows(&data, &profile)?;
            let interval = rows.get(before..).context("audit sequence shrank")?;
            let target = effect(CALLER_SHAPE, case["operation"].as_str().unwrap())?;
            let digest = tobkiri_canon::digest(&args, Variant::StrictV1)?;
            let terminal = terminal_pair_with_outcome(
                interval,
                &json!({"kind":"user"}),
                &target,
                &digest,
                &case_outcome(case, false)?,
            )?;
            let nested = dependency_audit(interval, &principal(&graph.caller), case)?;
            let matched = record["exact_result"] == true && terminal && nested["matched"] == true;
            record["exact_user_terminal"] = json!(terminal);
            record["args_digest"] = json!(digest);
            record["dependency_audit"] = nested;
            record["matched"] = json!(matched);
            record["admitted_parity"] =
                json!(matched && case["classification"] != "provider-nonfinite");
            record["nonfinite_boundary_not_original_error_parity"] =
                json!(matched && case["classification"] == "provider-nonfinite");
            report["cases"][index] = record;
            ensure!(
                matched,
                "caller case{index} result or authenticated dependency receipt differs"
            );
        }
        ensure!(
            initialized(&audit_rows(&data, &profile)?, &graph.caller)?
                && initialized(&audit_rows(&data, &profile)?, &graph.bridge)?,
            "separate real Pack initializations missing"
        );
        Ok(())
    }
    .await;
    let close = session.close().await;
    finish(result, close, report)
}

async fn denial(root: &Path, graph: &Graph, report: &mut Value) -> Result<()> {
    let allowed = profile(graph, true, false, "prof_python_caller_allow_comparison")?;
    let denied = profile(graph, false, false, "prof_python_caller_denied")?;
    validate_denial_delta(&allowed, &denied)?;
    let data = root.join("denied/data");
    let (_core, session) = start(&data, &denied, false).await?;
    let result:Result<()>=async {
        let case=&graph.oracle["cases"][0];let args=arguments(case)?;
        let actual=session.call_user_port(ShapeRef::parse(CALLER_SHAPE)?,OpName::parse("generate-intents")?,&args).await;
        let rows=audit_rows(&data,&denied)?;let blocked=effect(BRIDGE_SHAPE,"normalize")?;
        let denied_count=denied_count(&rows,&principal(&graph.caller));let dispatches=dispatch_count(&rows,&blocked)?;
        let provider=pack_events(&rows,"dispatch",&graph.bridge);let caller_init=initialized(&rows,&graph.caller)?;
        let public_denied=matches!(&actual,Err(CoreError::Step(step)) if step.code=="denied");
        let actual_error=match &actual {Err(CoreError::Step(step))=>json!({"code":step.code,"message":step.message}),Err(error)=>json!({"unexpected":error.to_string()}),Ok(_)=>json!(null)};
        let user_terminal=terminal_pair_with_outcome(&rows,&json!({"kind":"user"}),
            &effect(CALLER_SHAPE,"generate-intents")?,&tobkiri_canon::digest(&args,Variant::StrictV1)?,
            &Outcome::Failed {code:"denied".into()})?;
        let matched=public_denied&&user_terminal&&denied_count==1&&dispatches==0&&provider==0&&caller_init;
        *report=json!({"matched":matched,"returned_error":actual.is_err(),"public_denied_code":public_denied,"actual_error":actual_error,
            "exact_user_denied_terminal":user_terminal,"public_message_scope":"Gate trap diagnostic, not a fabricated nominal guest error message","exact_caller_denials":denied_count,
            "blocked_bridge_dispatches":dispatches,"provider_dispatches_entire_session":provider,
            "caller_initialized":caller_init,"only_exact_caller_permission_removed":true,
            "denial_row_scope":"effect kind/code/epoch; path established by single-edge policy delta and absence of target dispatch"});
        ensure!(matched,"missing real caller denial before provider entry");Ok(())
    }.await;
    let close = session.close().await;
    finish(result, close, report)
}

async fn shutdown_admission(root: &Path, graph: &Graph, report: &mut Value) -> Result<()> {
    let profile = profile(graph, true, false, "prof_python_caller_shutdown")?;
    let data = root.join("shutdown/data");
    let (_core, session) = start(&data, &profile, false).await?;
    let result:Result<()>=async {
        session.request_shutdown();
        let actual=session.call_user_port(ShapeRef::parse(CALLER_SHAPE)?,OpName::parse("generate-intents")?,&arguments(&graph.oracle["cases"][0])?).await;
        let unavailable=matches!(actual,Err(CoreError::Gate(tobkiri_gate::GateError::Unavailable)));
        let rows=audit_rows(&data,&profile)?;
        let guest_dispatches=pack_events(&rows,"dispatch",&graph.caller)+pack_events(&rows,"dispatch",&graph.bridge);
        let user_dispatches=rows.iter().filter(|r|r["kind"]=="dispatch" && r["principal"]==json!({"kind":"user"})).count();
        let matched=unavailable&&guest_dispatches==0&&user_dispatches==0;
        *report=json!({"matched":matched,"exact_pre_entry_unavailable":unavailable,"guest_dispatches":guest_dispatches,"user_dispatches":user_dispatches,
            "scope":"request_shutdown closes admission before this call; not in-flight guest cancellation",
            "in_flight_guest_cancellation_verified":false});ensure!(matched,"shutdown admission did not reject before dispatch");Ok(())
    }.await;
    let close = session.close().await;
    finish(result, close, report)
}

fn complete(report: &Value, oracle: &Value) -> bool {
    let (Some(actual), Some(expected)) = (report.as_array(), oracle.as_array()) else {
        return false;
    };
    actual.len() == 30
        && actual.len() == expected.len()
        && actual.iter().zip(expected).all(|(a, e)| {
            a["id"] == e["id"]
                && a["classification"] == e["classification"]
                && a["observed"] == true
                && a["matched"] == true
        })
}

#[tokio::test]
#[ignore = "two separately pinned Python Packs; arm64 macOS Darwin24.6.0/local APFS; explicit fresh evidence"]
async fn original_python_tool_caller_replays_through_real_core() -> Result<()> {
    let destination = inputs::receipt_destination(&repo())?;
    let mut report = json!({"format":"tobkiri.gateway-tool-caller-native-core-replay/1","scope":"two-separate-python-packs-not-finalizer",
        "acceptance_scope":"actual separately pinned Python Wasm Packs through Core/Gate using the test Host and test data root; no Launcher GUI approval flow or production Gate wiring",
        "protocol":"ordered-finite-json-v2","host_os":std::env::consts::OS,"host_arch":std::env::consts::ARCH,
        "required_owner_platform":{"os":"macos","arch":"aarch64","darwin":"24.6.0","filesystem":"local APFS"},
        "original_reference_runtime":{"implementation":"CPython","python":"3.13.5","unicode":"15.1.0","int_max_str_digits":4300},
        "expected_guest_runtime":{"implementation":"CPython","python":EXPECTED_RUNTIME,"unicode":EXPECTED_UNICODE},
        "gate_runtime_acceptance_scope":"Core/Gate runtime under the test Host::default() fixture; not production Gate or Launcher acceptance",
        "source_oracle_sha256":inputs::ORACLE_SHA,"source_counts":inputs::counts(),"core_runtime_path_passed":false,
        "native_core_acceptance":false,"gate_runtime_acceptance":false,"production_enabled":false,"full_gateway_migration_claimed":false,
        "five_pack_finalizer_acceptance":false,"all_typed_error_arms_executed":false,"in_flight_guest_cancellation_verified":false,
        "guest_lifecycle_shutdown_claimed":false,"retry_parity_claimed":false,"legacy_http_error_parity_claimed":false,
        "partial_stream_mutations_observed":false,"runtime":{},"replay":{},"denial":{},"shutdown_admission":{}});
    let result: Result<()> = async {
        ensure!(
            cfg!(all(target_os = "macos", target_arch = "aarch64")),
            "native caller acceptance requires arm64 macOS; Linux is source/static-only"
        );
        let graph = inputs::load(&repo()).await?;
        report["original_reference_runtime"] = graph.oracle["runtime"].clone();
        report["identity"] = graph.identity.clone();
        let temp = physical_temp()?;
        // Core retains authoritative OS/filesystem/physical-owner guards.
        runtime_probe(temp.path(), &graph, &mut report["runtime"]).await?;
        replay(temp.path(), &graph, &mut report["replay"]).await?;
        ensure!(
            complete(&report["replay"]["cases"], &graph.oracle["cases"]),
            "complete current30-case native proof missing"
        );
        denial(temp.path(), &graph, &mut report["denial"]).await?;
        shutdown_admission(temp.path(), &graph, &mut report["shutdown_admission"]).await?;
        Ok(())
    }
    .await;
    report["core_runtime_path_passed"] = json!(result.is_ok());
    report["native_core_acceptance"] = json!(result.is_ok());
    report["gate_runtime_acceptance"] = json!(result.is_ok());
    if let Err(error) = &result {
        report["first_error"] = json!(format!("{error:#}"));
    }
    let saved = evidence::write_receipt(&destination, &report);
    match (result, saved) {
        (Err(first), Err(write)) => {
            anyhow::bail!("{first:#}; additionally receipt failed: {write:#}")
        }
        (Err(first), _) => Err(first),
        (Ok(()), Err(write)) => Err(write),
        (Ok(()), Ok(())) => Ok(()),
    }
}

#[test]
fn source_terminal_prefix_is_not_original_source_call_count() -> Result<()> {
    let (oracle, _) = inputs::reviewed_sources(&repo())?;
    for case in oracle["cases"].as_array().unwrap() {
        let calls = expected_dependency_calls(case)?;
        let source = case["source_reference_call_count"].as_u64().unwrap() as usize;
        if case["id"] == "events-later-nonfinite" {
            assert_eq!((calls.len(), source), (2, 3));
        } else {
            assert_eq!(calls.len(), source);
        }
    }
    Ok(())
}
#[test]
fn opaque_caller_data_keeps_bigints_float_order_and_negative_zero() -> Result<()> {
    let (oracle, _) = inputs::reviewed_sources(&repo())?;
    for case in oracle["cases"].as_array().unwrap() {
        let args = arguments(case)?;
        assert_eq!(args.as_object().unwrap().len(), 1);
        assert_eq!(
            args.get("payload").and_then(CanonValue::as_str),
            case["input_json"].as_str()
        );
    }
    Ok(())
}
#[test]
fn nine_public_error_mapping_entries_are_mapping_only_not_import_injections() {
    let codes = [
        "denied",
        "approval-required",
        "cancelled",
        "not-found",
        "invalid",
        "limit-exceeded",
        "conflict",
        "unavailable",
        "internal",
    ];
    let messages: BTreeSet<_> = codes
        .iter()
        .map(|code| public_message(code).unwrap())
        .collect();
    assert_eq!(messages.len(), 9);
    assert_eq!(public_message("UNKNOWN"), None);
    assert_ne!(
        public_message("invalid"),
        Some("outside ordered-finite-json-v2 domain")
    );
}
#[test]
fn cleanup_retains_both_failures_without_lifecycle_or_cancellation_claim() {
    let mut report = json!({});
    let error = finish(
        Err(anyhow::anyhow!("first-operation")),
        Err(CoreError::Invalid("cleanup".into())),
        &mut report,
    )
    .unwrap_err();
    assert!(error.to_string().contains("first-operation"));
    assert!(error.to_string().contains("cleanup"));
    assert_eq!(report["session_close_awaited"], true);
    assert_eq!(report["session_closed"], false);
    assert_eq!(report["guest_lifecycle_shutdown_claimed"], false);
}
#[test]
fn partial_or_reordered_case_reports_do_not_establish_native_acceptance() -> Result<()> {
    let (oracle, _) = inputs::reviewed_sources(&repo())?;
    let mut cases:Value=oracle["cases"].as_array().unwrap().iter().map(|c|json!({"id":c["id"],"classification":c["classification"],"observed":true,"matched":true})).collect();
    assert!(complete(&cases, &oracle["cases"]));
    cases[29]["observed"] = json!(false);
    assert!(!complete(&cases, &oracle["cases"]));
    cases[29]["observed"] = json!(true);
    cases.as_array_mut().unwrap().swap(0, 1);
    assert!(!complete(&cases, &oracle["cases"]));
    Ok(())
}

/// Synthetic rows test the evidence checker only. They are not Gate receipts.
#[test]
fn synthetic_edge_checker_rejects_crossed_and_misordered_custody() -> Result<()> {
    let (oracle, _) = inputs::reviewed_sources(&repo())?;
    let case = &oracle["cases"][0];
    let call = &expected_dependency_calls(case)?[0];
    let owner = json!({"kind":"pack","pack":CALLER,"hash":format!("sha256:{}","0".repeat(64))});
    let target = effect(BRIDGE_SHAPE, "normalize")?;
    let args = canon(&json!({"operation":call["operation"],"payload":call["payload_json"]}))?;
    let argument_digest = tobkiri_canon::digest(&args, Variant::StrictV1)?;
    let payload = json!({"grant":"source-checker-only-grant","effect_digest":target.digest()?,"args_digest":argument_digest});
    let dispatch = json!({"kind":"dispatch","principal":owner,"request":"source-checker-only-request","payload":payload});
    let mut outcome = json!({"kind":"outcome","principal":owner,"request":"source-checker-only-request","payload":payload});
    outcome["payload"]["outcome"] = serde_json::to_value(Outcome::Succeeded)?;
    let rows = vec![dispatch, outcome];
    assert_eq!(dependency_audit(&rows, &owner, case)?["matched"], true);
    for mutation in [
        "request",
        "grant",
        "effect",
        "args",
        "principal",
        "outcome",
        "order",
        "duplicate",
        "wrong-outcome-before-right",
    ] {
        let mut changed = rows.clone();
        match mutation {
            "request" => changed[1]["request"] = json!("other"),
            "grant" => changed[1]["payload"]["grant"] = json!("other"),
            "effect" => changed[1]["payload"]["effect_digest"] = json!("other"),
            "args" => changed[1]["payload"]["args_digest"] = json!("other"),
            "principal" => changed[1]["principal"] = json!({"kind":"user"}),
            "outcome" => {
                changed[1]["payload"]["outcome"] = serde_json::to_value(Outcome::Failed {
                    code: "internal".into(),
                })?
            }
            "order" => changed.swap(0, 1),
            "duplicate" => changed.push(rows[1].clone()),
            "wrong-outcome-before-right" => {
                changed.swap(0, 1);
                let mut wrong = rows[1].clone();
                wrong["payload"]["args_digest"] = json!("other");
                changed.push(wrong);
            }
            _ => unreachable!(),
        }
        assert_eq!(
            dependency_audit(&changed, &owner, case)?["matched"],
            false,
            "accepted synthetic {mutation}"
        );
    }
    Ok(())
}
