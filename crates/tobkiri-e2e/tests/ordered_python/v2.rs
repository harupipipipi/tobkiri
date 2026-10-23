//! Opt-in V2 public-Core acceptance, not the production Gateway replacement.
//!
//! Use the independent TOBKIRI_TEST_ORDERED_PYTHON_V2_{PACK_DIR,PACK_SHA256,
//! COMPONENT_SHA256,RECEIPT_SHA256,EVIDENCE} inputs. The ignored test requires the
//! real pinned component on arm64 macOS, Darwin 24.6.0 and local APFS. Core's
//! production allocation-owner checks remain authoritative and unchanged.
use super::{
    audit_rows, canon, consumer, evidence,
    evidence::Inputs,
    evidence_v2::{self, SHAPE},
    pack_events, parse_output, physical_temp, profile_for, provider_initialized, repo,
    require_runtime, runtime, start_for, terminal_pair, terminal_pair_with_outcome,
};
use anyhow::{Context, Result, ensure};
use consumer::BridgeAbi;
use serde_json::{Value, json};
use std::{collections::BTreeMap, path::Path};
use tobkiri_canon::{
    CanonValue, CoreBuffer, Variant,
    ids::{OpName, ShapeRef},
};
use tobkiri_core::{CoreError, Session};
use tobkiri_gate::{EffectRequest, Outcome};
use tobkiri_manifest::VerifiedPack;
use tobkiri_profile::{PackPin, Profile};

fn args(case: &Value) -> Result<CanonValue> {
    ensure!(
        case["operation"].is_string() && case["input_json"].is_string(),
        "reviewed operation and payload must be strings"
    );
    let value = canon(&json!({"operation":case["operation"], "payload":case["input_json"]}))?;
    ensure!(
        tobkiri_canon::encode(&value, Variant::StrictV1)?.len() <= tobkiri_canon::MAX_BYTES,
        "V2 outer request exceeds native bound"
    );
    Ok(value)
}

fn public_error_matches(classification: &str, code: &str, message: &str) -> bool {
    match classification {
        "original-invalid" => code == "invalid" && message == "provider reported invalid input",
        "excluded-not-parity" => {
            code == "internal" && message == "provider reported an internal error"
        }
        _ => false,
    }
}

fn normalize_outcome(classification: &str, nested: bool) -> Result<Outcome> {
    Ok(match classification {
        "success" => Outcome::Succeeded,
        "original-invalid" | "excluded-not-parity" => Outcome::Failed {
            code: if nested {
                "provider-error"
            } else if classification == "original-invalid" {
                "invalid"
            } else {
                "internal"
            }
            .into(),
        },
        _ => anyhow::bail!("unknown reviewed normalize classification"),
    })
}

fn provider_initialized_successfully(rows: &[Value], provider: &VerifiedPack) -> Result<bool> {
    let effect = EffectRequest::WasmEntry {
        pack: provider.manifest.id.clone(),
        hash: provider.hash.clone(),
        entry: tobkiri_gate::WasmEntryKind::Init,
    };
    Ok(provider_initialized(rows, provider)?
        && terminal_pair_with_outcome(
            rows,
            &json!({"kind":"pack", "pack":provider.manifest.id, "hash":provider.hash}),
            &effect,
            &effect.digest()?,
            &Outcome::Succeeded,
        )?)
}

fn observe_result(case: &Value, actual: Result<CoreBuffer, CoreError>) -> Result<Value> {
    let classification = case["classification"]
        .as_str()
        .context("classification missing")?;
    let mut record = json!({
        "id":case["id"], "classification":classification, "observed":true, "matched":false,
        "admitted_parity":false, "nonfinite_boundary_verified":false
    });
    let matched = match actual {
        Ok(buffer) => match parse_output(&buffer)? {
            CanonValue::Str(output) => {
                record["actual"] = json!({
                    "status":"ok", "output_sha256":evidence::digest(output.as_bytes()),
                    "output_bytes":output.len()
                });
                classification == "success" && case["output_json"].as_str() == Some(output.as_str())
            }
            _ => {
                record["actual"] = json!({"status":"unexpected-output-type"});
                false
            }
        },
        Err(CoreError::Step(step)) => {
            let exact = public_error_matches(classification, &step.code, &step.message);
            record["actual"] = json!({
                "status":"step-error", "code":step.code, "message":step.message,
                "exact_public_error":exact
            });
            exact
        }
        Err(error) => {
            record["actual"] =
                json!({"status":"unexpected-core-error", "message":error.to_string()});
            false
        }
    };
    record["exact_result"] = json!(matched);
    Ok(record)
}

fn mark_match(record: &mut Value, matched: bool) {
    record["matched"] = json!(matched);
    let boundary = record["classification"] == "excluded-not-parity";
    record["admitted_parity"] = json!(matched && !boundary);
    record["nonfinite_boundary_verified"] = json!(matched && boundary);
}

/// Always retain the first failure and any cleanup failure independently.
/// Public Session::close is not proof that a guest lifecycle shutdown ran.
fn finish_session(
    result: Result<()>,
    close: Result<(), CoreError>,
    report: &mut Value,
) -> Result<()> {
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
        (Err(first), Err(cleanup)) => {
            anyhow::bail!("{first:#}; additionally public Session::close failed: {cleanup}")
        }
        (Err(first), Ok(())) => Err(first),
        (Ok(()), Err(cleanup)) => Err(cleanup.into()),
        (Ok(()), Ok(())) => Ok(()),
    }
}

async fn replay_cases(
    session: &Session,
    inputs: &Inputs,
    data: &Path,
    profile: &Profile,
    report: &mut Value,
) -> Result<()> {
    let empty = CanonValue::Obj(BTreeMap::new());
    let runtime_effect = EffectRequest::PortCall {
        shape: ShapeRef::parse(SHAPE)?,
        op: OpName::parse("runtime-info")?,
    };
    let before = audit_rows(data, profile)?.len();
    let buffer = session
        .call_user_port(
            ShapeRef::parse(SHAPE)?,
            OpName::parse("runtime-info")?,
            &empty,
        )
        .await?;
    report["guest_runtime"] = runtime(parse_output(&buffer)?)?;
    drop(buffer);
    require_runtime(&report["guest_runtime"])?;
    let rows = audit_rows(data, profile)?;
    let initialized = provider_initialized_successfully(&rows, &inputs.pack)?;
    let runtime_terminal = terminal_pair_with_outcome(
        rows.get(before..).context("audit sequence shrank")?,
        &json!({"kind":"user"}),
        &runtime_effect,
        &tobkiri_canon::digest(&empty, Variant::StrictV1)?,
        &Outcome::Succeeded,
    )?;
    report["physical_provider_init_receipt"] = json!(initialized);
    report["runtime_info_exact_dispatch_outcome"] = json!(runtime_terminal);
    ensure!(
        initialized && runtime_terminal,
        "V2 runtime or physical provider receipt missing"
    );
    let cases = inputs.oracle["cases"]
        .as_array()
        .context("V2 reviewed cases missing")?;
    report["cases"] = cases
        .iter()
        .map(|case| {
            json!({
                "id":case["id"], "classification":case["classification"], "observed":false,
                "matched":false, "admitted_parity":false, "nonfinite_boundary_verified":false
            })
        })
        .collect();
    for (index, case) in cases.iter().enumerate() {
        let arguments = args(case)?;
        let before = audit_rows(data, profile)?.len();
        let actual = session
            .call_user_port(
                ShapeRef::parse(SHAPE)?,
                OpName::parse("normalize")?,
                &arguments,
            )
            .await;
        let mut record = observe_result(case, actual)?;
        let rows = audit_rows(data, profile)?;
        let effect = EffectRequest::PortCall {
            shape: ShapeRef::parse(SHAPE)?,
            op: OpName::parse("normalize")?,
        };
        let arguments_digest = tobkiri_canon::digest(&arguments, Variant::StrictV1)?;
        let terminal = terminal_pair(
            rows.get(before..).context("audit sequence shrank")?,
            &json!({"kind":"user"}),
            &effect,
            &arguments_digest,
        )?;
        let expected_outcome = normalize_outcome(
            case["classification"]
                .as_str()
                .context("classification missing")?,
            false,
        )?;
        let outcome = terminal_pair_with_outcome(
            rows.get(before..).context("audit sequence shrank")?,
            &json!({"kind":"user"}),
            &effect,
            &arguments_digest,
            &expected_outcome,
        )?;
        record["args_digest"] = json!(arguments_digest);
        record["effect_digest"] = json!(effect.digest()?);
        record["exact_port_dispatch_outcome"] = json!(terminal);
        record["exact_terminal_outcome"] = json!(outcome);
        record["expected_terminal_outcome"] = json!(expected_outcome);
        let matched = record["exact_result"] == true && terminal && outcome;
        mark_match(&mut record, matched);
        report["cases"][index] = record;
        ensure!(
            matched,
            "V2 native case {index} failed exact public result/audit checks"
        );
    }
    Ok(())
}

fn selected_consumer_cases(observations: &Value) -> Result<Vec<&Value>> {
    let cases = observations["cases"]
        .as_array()
        .context("V2 cases missing")?;
    [
        ("base:ordinary", "success"),
        ("base:inner-bigint-excluded", "success"),
        ("base:inner-depth-excluded", "success"),
        ("base:invalid-name", "original-invalid"),
        ("base:inner-nonfinite-excluded", "excluded-not-parity"),
    ]
    .into_iter()
    .map(|(id, classification)| {
        let matches: Vec<_> = cases.iter().filter(|case| case["id"] == id).collect();
        ensure!(matches.len() == 1, "typed V2 case is missing or duplicated");
        let case = matches[0];
        ensure!(
            case["classification"] == classification,
            "typed V2 classification changed"
        );
        ensure!(
            case["operation"]
                .as_str()
                .context("operation missing")?
                .len()
                <= 1024
                && case["input_json"]
                    .as_str()
                    .context("payload missing")?
                    .len()
                    <= 4096
                && (classification != "success"
                    || case["output_json"]
                        .as_str()
                        .context("success string missing")?
                        .len()
                        <= 4096),
            "typed V2 fixture exceeds the bounded consumer string arena"
        );
        Ok(case)
    })
    .collect()
}

fn denial_changes_only_consumer_limit(allowed: &Profile, denied: &Profile) -> Result<()> {
    let mut expected = serde_json::to_value(allowed)?;
    let denied = serde_json::to_value(denied)?;
    expected["id"] = denied["id"].clone();
    expected["pack_limits"][consumer::CONSUMER]["ports"] = json!([]);
    ensure!(
        expected == denied,
        "denial profile changed more than the consumer's effective port limit"
    );
    Ok(())
}

fn bridge_dispatch_count(rows: &[Value], effect: &EffectRequest) -> Result<usize> {
    let digest = effect.digest()?.to_string();
    Ok(rows
        .iter()
        .filter(|row| row["kind"] == "dispatch" && row["payload"]["effect_digest"] == digest)
        .count())
}

fn consumer_denials(rows: &[Value], principal: &Value) -> usize {
    rows.iter()
        .filter(|row| {
            row["kind"] == "denied"
                && row["principal"] == *principal
                && row["payload"][0] == "port-call"
                && row["payload"][1] == "denied"
        })
        .count()
}

async fn consumer_session(
    root: &Path,
    inputs: &Inputs,
    caller: &(PackPin, VerifiedPack),
    normalize: bool,
    allowed: bool,
    profile: &Profile,
    report: &mut Value,
) -> Result<()> {
    let data = root.join(profile.id.as_str()).join("data");
    let (_core, session) = start_for(&data, profile, true, BridgeAbi::V2).await?;
    let result: Result<()> = async {
        let principal = json!({"kind":"pack", "pack":caller.1.manifest.id, "hash":caller.1.hash});
        let effect = EffectRequest::PortCall {
            shape: ShapeRef::parse(SHAPE)?,
            op: OpName::parse(if normalize {
                "normalize"
            } else {
                "runtime-info"
            })?,
        };
        let selected = selected_consumer_cases(&inputs.oracle)?;
        let cases: Vec<Option<&Value>> = if normalize {
            selected
                .into_iter()
                .take(if allowed { 5 } else { 1 })
                .map(Some)
                .collect()
        } else {
            vec![None]
        };
        report["caller_pack_sha256"] = json!(caller.1.hash.to_string());
        report["cases"] = json!([]);
        for case in cases {
            let arguments = match case {
                Some(case) => args(case)?,
                None => CanonValue::Obj(BTreeMap::new()),
            };
            let arguments_digest = tobkiri_canon::digest(&arguments, Variant::StrictV1)?;
            let before = audit_rows(&data, profile)?.len();
            let actual = session
                .call_user_port(
                    ShapeRef::parse(consumer::ENTRY)?,
                    OpName::parse(if normalize { "normalize" } else { "run" })?,
                    &arguments,
                )
                .await;
            let rows = audit_rows(&data, profile)?;
            let interval = rows.get(before..).context("audit sequence shrank")?;
            let dispatches = bridge_dispatch_count(interval, &effect)?;
            let provider_dispatches = pack_events(interval, "dispatch", &inputs.pack);
            let denials = consumer_denials(interval, &principal);
            let terminal = terminal_pair(interval, &principal, &effect, &arguments_digest)?;
            // A forwarded typed error arm is always provider-error in this
            // nested audit, including the public Core "internal" boundary.
            let expected_outcome = match case {
                Some(case) => normalize_outcome(
                    case["classification"]
                        .as_str()
                        .context("classification missing")?,
                    true,
                )?,
                None => Outcome::Succeeded,
            };
            let outcome = terminal_pair_with_outcome(
                interval,
                &principal,
                &effect,
                &arguments_digest,
                &expected_outcome,
            )?;
            let mut record = json!({
                "id":case.map_or(json!("runtime-info"), |case| case["id"].clone()),
                "observed":true, "matched":false, "args_digest":arguments_digest,
                "effect_digest":effect.digest()?, "bridge_port_dispatches":dispatches,
                "provider_dispatches":provider_dispatches, "caller_denials":denials,
                "exact_port_dispatch_outcome":terminal, "exact_terminal_outcome":outcome,
                "expected_terminal_outcome":expected_outcome, "returned_error":actual.is_err()
            });
            let matched = if allowed {
                let result_matches = if let Some(case) = case {
                    let observed = observe_result(case, actual)?;
                    let matches = observed["exact_result"] == true;
                    record["result"] = observed;
                    matches
                } else {
                    let buffer = actual?;
                    record["guest_runtime"] = runtime(parse_output(&buffer)?)?;
                    require_runtime(&record["guest_runtime"])?;
                    true
                };
                let initialized = provider_initialized_successfully(&rows, &inputs.pack)?;
                record["physical_provider_init_receipt"] = json!(initialized);
                result_matches
                    && terminal
                    && outcome
                    && dispatches == 1
                    && denials == 0
                    && initialized
            } else {
                // Check the entire fresh session, not just the invocation:
                // provider work before the denied call is not acceptable.
                let all_target_dispatches = bridge_dispatch_count(&rows, &effect)?;
                let all_provider_dispatches = pack_events(&rows, "dispatch", &inputs.pack);
                record["session_target_dispatches"] = json!(all_target_dispatches);
                record["session_provider_dispatches"] = json!(all_provider_dispatches);
                actual.is_err()
                    && denials == 1
                    && dispatches == 0
                    && provider_dispatches == 0
                    && all_target_dispatches == 0
                    && all_provider_dispatches == 0
                    && !terminal
            };
            record["matched"] = json!(matched);
            if let Some(observed) = record.get_mut("result") {
                mark_match(observed, matched);
            }
            report["cases"]
                .as_array_mut()
                .context("consumer report cases missing")?
                .push(record);
            ensure!(
                matched,
                "typed V2 result or exact caller authority/audit check failed"
            );
        }
        Ok(())
    }
    .await;
    let close = session.close().await;
    finish_session(result, close, report)
}

async fn consumer_authority(root: &Path, inputs: &Inputs, report: &mut Value) -> Result<()> {
    let caller = consumer::materialize_for(root, &inputs.pack, BridgeAbi::V2)?;
    for normalize in [false, true] {
        let mode = if normalize {
            "normalize"
        } else {
            "runtime_info"
        };
        let profiles = [
            profile_for(
                inputs,
                Some(&caller),
                true,
                &format!("prof_ordered_v2_{mode}_allowed"),
                BridgeAbi::V2,
            )?,
            profile_for(
                inputs,
                Some(&caller),
                false,
                &format!("prof_ordered_v2_{mode}_denied"),
                BridgeAbi::V2,
            )?,
        ];
        denial_changes_only_consumer_limit(&profiles[0], &profiles[1])?;
        report[mode] = json!({"only_effective_consumer_limit_narrowed":true});
        for (profile, allowed) in profiles.iter().zip([true, false]) {
            let label = if allowed { "allowed" } else { "denied" };
            report[mode][label] = json!({});
            consumer_session(
                root,
                inputs,
                &caller,
                normalize,
                allowed,
                profile,
                &mut report[mode][label],
            )
            .await?;
        }
    }
    Ok(())
}

fn complete_case_report(cases: &Value, expected: &Value) -> bool {
    let (Some(cases), Some(expected)) = (cases.as_array(), expected.as_array()) else {
        return false;
    };
    cases.len() == 33
        && expected.len() == 33
        && cases.iter().zip(expected).all(|(case, expected)| {
            case["id"] == expected["id"]
                && case["classification"] == expected["classification"]
                && case["observed"] == true
                && case["matched"] == true
                && case["admitted_parity"] == (expected["classification"] != "excluded-not-parity")
                && case["nonfinite_boundary_verified"]
                    == (expected["classification"] == "excluded-not-parity")
        })
}

#[tokio::test]
#[ignore = "requires pinned V2 component, external evidence, arm64 macOS Darwin 24.6.0/local APFS"]
async fn ordered_python_v2_bridge_replays_through_real_core() -> Result<()> {
    // The destination must be valid before any execution. All subsequent
    // preflight/runtime/cleanup errors are captured in one fresh diagnostic.
    let destination = evidence_v2::receipt_destination(&repo())?;
    let mut report = json!({
        "format":"tobkiri.ordered-python-native-core-replay/2", "protocol":"ordered-finite-json-v2",
        "host_os":std::env::consts::OS, "host_arch":std::env::consts::ARCH,
        "required_owner_platform":{"os":"macos", "arch":"aarch64", "darwin":"24.6.0", "filesystem":"local APFS"},
        "original_reference_runtime":{"python":"3.13.5", "unicode":"15.1.0"},
        "frozen_case_counts":evidence_v2::counts(), "core_runtime_path_passed":false,
        "native_core_acceptance":false, "full_unit_complete":false,
        "original_gateway_replaced":false, "production_enabled":false,
        "legacy_http_error_parity_claimed":false, "guest_lifecycle_shutdown_claimed":false,
        "session_close_scope":"public Core session cleanup, not guest lifecycle shutdown",
        "replay":{}, "consumer":{}
    });
    let result: Result<()> = async {
        ensure!(
            cfg!(all(target_os = "macos", target_arch = "aarch64")),
            "V2 native acceptance requires arm64 macOS; Linux is compile/helper-only"
        );
        let inputs = evidence_v2::load(&repo())?;
        report["identity"] = inputs.identity.clone();
        let temporary = physical_temp()?;
        let profile = profile_for(&inputs, None, true, "prof_ordered_python_v2", BridgeAbi::V2)?;
        let data = temporary.path().join("replay/data");
        // Core::open/start enforce Darwin 24.6.0, local APFS and the physical
        // allocation owner. Do not replace that guard with a test backend.
        let (_core, session) = start_for(&data, &profile, false, BridgeAbi::V2).await?;
        let replay = replay_cases(&session, &inputs, &data, &profile, &mut report["replay"]).await;
        let close = session.close().await;
        finish_session(replay, close, &mut report["replay"])?;
        ensure!(
            complete_case_report(&report["replay"]["cases"], &inputs.oracle["cases"]),
            "native V2 complete ordered result set missing"
        );
        consumer_authority(temporary.path(), &inputs, &mut report["consumer"]).await?;
        Ok(())
    }
    .await;
    report["core_runtime_path_passed"] = json!(result.is_ok());
    report["native_core_acceptance"] = json!(result.is_ok());
    if let Err(error) = &result {
        report["first_error"] = json!(format!("{error:#}"));
    }
    let saved = evidence::write_receipt(&destination, &report);
    match (result, saved) {
        (Err(first), Err(write)) => {
            anyhow::bail!("{first:#}; additionally diagnostic receipt failed: {write:#}")
        }
        (Err(first), Ok(())) => Err(first),
        (Ok(()), Err(write)) => Err(write),
        (Ok(()), Ok(())) => {
            println!(
                "Ordered Python V2 Core receipt: cases=33 exact-success=18 original-invalid=12 nonfinite-boundary-not-parity=3 full-unit-complete=false"
            );
            Ok(())
        }
    }
}

#[test]
fn v2_public_errors_reject_crossed_boundaries_and_unsanitized_messages() {
    assert!(public_error_matches(
        "original-invalid",
        "invalid",
        "provider reported invalid input"
    ));
    assert!(public_error_matches(
        "excluded-not-parity",
        "internal",
        "provider reported an internal error"
    ));
    assert!(!public_error_matches(
        "excluded-not-parity",
        "invalid",
        "provider reported invalid input"
    ));
    assert!(!public_error_matches(
        "original-invalid",
        "internal",
        "provider reported an internal error"
    ));
    assert!(!public_error_matches(
        "excluded-not-parity",
        "internal",
        "provider output contains a non-finite number"
    ));
    assert!(!public_error_matches(
        "success",
        "invalid",
        "provider reported invalid input"
    ));
}

#[test]
fn v2_direct_and_nested_audits_reject_crossed_terminal_outcomes() -> Result<()> {
    let effect = EffectRequest::PortCall {
        shape: ShapeRef::parse(SHAPE)?,
        op: OpName::parse("normalize")?,
    };
    let arguments = tobkiri_canon::Digest::hash(b"test-only-v2-original-arguments");
    let alternatives = [
        Outcome::Succeeded,
        Outcome::Failed {
            code: "invalid".into(),
        },
        Outcome::Failed {
            code: "internal".into(),
        },
        Outcome::Failed {
            code: "provider-error".into(),
        },
    ];
    for nested in [false, true] {
        let principal = if nested {
            json!({"kind":"pack", "pack":consumer::CONSUMER,
                "hash":tobkiri_canon::Digest::hash(b"test-only-v2-consumer")})
        } else {
            json!({"kind":"user"})
        };
        for classification in ["success", "original-invalid", "excluded-not-parity"] {
            let expected = normalize_outcome(classification, nested)?;
            let dispatch = json!({
                "kind":"dispatch", "principal":principal, "request":"request-one",
                "payload":{"grant":"grant-one", "effect_digest":effect.digest()?, "args_digest":arguments}
            });
            for actual in &alternatives {
                let mut terminal = dispatch.clone();
                terminal["kind"] = json!("outcome");
                terminal["payload"]["outcome"] = serde_json::to_value(actual)?;
                let events = [dispatch.clone(), terminal];
                assert!(terminal_pair(&events, &principal, &effect, &arguments)?);
                assert_eq!(
                    terminal_pair_with_outcome(
                        &events, &principal, &effect, &arguments, &expected
                    )?,
                    serde_json::to_value(actual)? == serde_json::to_value(&expected)?,
                    "accepted crossed outcome for {classification}, nested={nested}"
                );
            }
        }
    }
    assert!(normalize_outcome("invented-parity", false).is_err());
    Ok(())
}

#[test]
fn v2_typed_inputs_preserve_original_bigint_depth_and_ordered_strings() -> Result<()> {
    let (_, observations) = evidence_v2::reviewed_sources(&repo())?;
    let selected = selected_consumer_cases(&observations)?;
    ensure!(selected.len() == 5, "wrong typed case count");
    for case in &selected {
        let CanonValue::Obj(fields) = args(case)? else {
            anyhow::bail!("outer object missing")
        };
        assert_eq!(
            fields["operation"],
            CanonValue::Str(case["operation"].as_str().unwrap().to_owned())
        );
        assert_eq!(
            fields["payload"],
            CanonValue::Str(case["input_json"].as_str().unwrap().to_owned())
        );
    }
    assert!(
        selected[0]["input_json"]
            .as_str()
            .unwrap()
            .contains("\"z\":1.0,\"a\":-0.0")
    );
    assert!(
        selected[1]["input_json"]
            .as_str()
            .unwrap()
            .contains("9007199254740992")
    );
    assert!(
        selected[2]["input_json"]
            .as_str()
            .unwrap()
            .contains("[[[[[[[[")
    );
    Ok(())
}

#[test]
fn v2_cleanup_preserves_primary_and_secondary_failures() {
    let mut report = json!({});
    let result = finish_session(
        Err(anyhow::anyhow!("original replay failure")),
        Err(CoreError::Invalid("cleanup failure".into())),
        &mut report,
    );
    let text = format!("{:#}", result.unwrap_err());
    assert!(text.starts_with("original replay failure"));
    assert!(text.contains("cleanup failure"));
    assert_eq!(report["first_error"], "original replay failure");
    assert!(
        report["cleanup_error"]
            .as_str()
            .unwrap()
            .contains("cleanup failure")
    );
    assert_eq!(report["session_close_awaited"], true);
    assert_eq!(report["session_closed"], false);
    assert_eq!(report["guest_lifecycle_shutdown_claimed"], false);
}

#[test]
fn v2_summary_rejects_partial_duplicate_reordered_and_boundary_parity_reports() -> Result<()> {
    let (_, observations) = evidence_v2::reviewed_sources(&repo())?;
    let expected = &observations["cases"];
    let valid: Value = expected
        .as_array()
        .unwrap()
        .iter()
        .map(|case| {
            let mut record =
                json!({"id":case["id"], "classification":case["classification"], "observed":true});
            mark_match(&mut record, true);
            record
        })
        .collect();
    assert!(complete_case_report(&valid, expected));
    for mutation in [
        "partial",
        "duplicate",
        "order",
        "boundary-parity",
        "not-observed",
    ] {
        let mut changed = valid.clone();
        match mutation {
            "partial" => {
                changed.as_array_mut().unwrap().pop();
            }
            "duplicate" => changed[1] = changed[0].clone(),
            "order" => changed.as_array_mut().unwrap().swap(0, 1),
            "boundary-parity" => changed[9]["admitted_parity"] = json!(true),
            "not-observed" => changed[0]["observed"] = json!(false),
            _ => unreachable!(),
        }
        assert!(
            !complete_case_report(&changed, expected),
            "accepted {mutation}"
        );
    }
    Ok(())
}
