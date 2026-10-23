//! Opt-in actual provider components through Core/Gate/Profile/Session/typed dispatch.
//!
//! Required TOBKIRI_TEST_RUST_PROVIDERS_{USAGE,STREAM}_{COMPONENT,SHA256},
//! {ORACLE,ORACLE_SHA256,EVIDENCE_DIR}. Paths are absolute; evidence is a NEW
//! private directory. Run only the exact ignored test named below.
//! cargo test -p tobkiri-e2e --test rust_python_providers
//!   rust_wasm_providers_replay_all_original_cases_through_core -- --ignored --exact
//! Optional USAGE_IMPLEMENTATION and STREAM_IMPLEMENTATION accept rust|python
//! and default to rust. For an independent original-Python baseline, set both
//! to python and supply the actual pinned Python artifacts through the same
//! COMPONENT/SHA256 variables and a NEW EVIDENCE_DIR. The 94 expectations,
//! attempted admissions and exact mismatch failures remain identical.
//!
//! The second ignored test additionally requires {BRIDGE,CALLER,FINALIZER}_
//! {COMPONENT,SHA256}, FINALIZER_ORACLE, FINALIZER_ORACLE_SHA256 and a separate
//! COMPOSITION_EVIDENCE_DIR. FINALIZER_IMPLEMENTATION=rust|python is required;
//! optional BRIDGE_IMPLEMENTATION and CALLER_IMPLEMENTATION use the same values
//! and default to python. Actual runtime-info must agree with every selection.
//! Optional BRIDGE_ORACLE and BRIDGE_ORACLE_SHA256 add the complete original
//! 81-case bridge v2 corpus. It runs alongside the same 94 cases and unchanged
//! ten-case finalizer observations in one real five-Pack Session. Optional
//! CALLER_ORACLE and CALLER_ORACLE_SHA256 add the unchanged thirty-case caller
//! source corpus, checking actual nested bridge dispatches and their outcomes.
//! Source nonfinite continuations remain distinct from imported error prefixes.
//! Missing artifacts/oracles fail; no test executes a producer or creates expectations.
//! Native fixture approval is the real scripted Host path, not GUI acceptance.
//! Pack/WIT logical IDs remain stable compatibility identities; runtime-info
//! and captured artifact/source identities report the selected implementation.
#[path = "rust_python_providers/admission.rs"]
mod admission;
#[path = "rust_python_providers/inputs.rs"]
mod inputs;
mod support;

use anyhow::{Context, Result, ensure};
use inputs::{Evidence, Oracle, Pack, ROLES, text};
use serde_json::{Value, json};
use std::{collections::BTreeMap, path::Path, sync::Arc, time::Duration};
use support::{Host, parse_output, physical_temp, repo};
use tobkiri_canon::{
    CanonValue, Variant,
    ids::{OpName, ShapeRef},
};
use tobkiri_core::{Core, CoreConfig, CoreError, Session};
use tobkiri_gate::{EffectRequest, Outcome, PermissionSet};
use tobkiri_profile::Profile;

fn arguments(operation: Option<&str>, payload: &str) -> CanonValue {
    // Only the outer record is CoreStrictV1. Inner DATA is never decoded or
    // reordered by Core/test JSON. Construct directly so oversized negative
    // vectors still reach call_user_port and observe its real admission error.
    let mut fields = BTreeMap::from([("payload".to_owned(), CanonValue::Str(payload.to_owned()))]);
    if let Some(operation) = operation {
        fields.insert(
            "operation".to_owned(),
            CanonValue::Str(operation.to_owned()),
        );
    }
    CanonValue::Obj(fields)
}

fn public_message(code: &str) -> Option<&'static str> {
    // Existing closed projection: tobkiri-wasm/src/dispatch.rs public_provider_error.
    match code {
        "invalid" => Some("provider reported invalid input"),
        "internal" => Some("provider reported an internal error"),
        "limit-exceeded" => Some("provider reported a resource limit"),
        _ => None,
    }
}

fn profile(packs: &[Pack], composition: bool) -> Result<Profile> {
    let mut ceiling = inputs::limits();
    ceiling.ports = packs
        .iter()
        .map(|pack| ROLES[pack.role].shape.to_owned())
        .collect();
    let user = PermissionSet {
        ports: ceiling.ports.clone(),
        wasm: ceiling.wasm.clone(),
        loops: ceiling.loops.clone(),
        ..Default::default()
    };
    let limits: BTreeMap<_, _> = packs
        .iter()
        .map(|pack| {
            (
                pack.pin.id.clone(),
                pack.verified.manifest.permissions.clone(),
            )
        })
        .collect();
    let bindings = packs
        .iter()
        .map(|pack| {
            let shape = ShapeRef::parse(ROLES[pack.role].shape)?;
            Ok((
                format!("{}@{}", shape.name, shape.version.major),
                ROLES[pack.role].id.to_owned(),
            ))
        })
        .collect::<Result<BTreeMap<_, _>>>()?;
    Ok(serde_json::from_value(json!({
        "format":"tobkiri.profile/1","id":if composition {"prof_rust_provider_composition"} else {"prof_rust_provider_replay"},
        "name":"Hash-pinned actual Rust provider fixture", "packs":packs.iter().map(|pack| pack.pin.clone()).collect::<Vec<_>>(),
        "publishers":{},"ceiling":ceiling,"pack_limits":limits,"user_permissions":user,"bindings":bindings,
        "overlay_order":[],"user_overlays":[],"startup":{"inputs":[],"ui":null}
    }))?)
}

async fn start(
    data: &Path,
    profile: &Profile,
    packs: &[Pack],
    caller_replay: bool,
) -> Result<(Core, Session, Arc<Host>)> {
    let host = Arc::new(Host::default());
    for pack in packs {
        let role = &ROLES[pack.role];
        for op in ["runtime-info", role.function] {
            host.utility_ports
                .lock()
                .unwrap()
                .insert((role.shape.into(), op.into()));
        }
        if pack.role == 4 {
            host.utility_ports
                .lock()
                .unwrap()
                .insert((role.shape.into(), "stream".into()));
        }
        if caller_replay && pack.role == 3 {
            for operation in ["stream-intents", "stream-event-intents"] {
                host.utility_ports
                    .lock()
                    .unwrap()
                    .insert((role.shape.into(), operation.into()));
            }
        }
    }
    host.approve_literal_user(profile)?;
    let core = Core::open(
        CoreConfig::for_test_data_root(data.to_path_buf()),
        host.clone(),
    )?;
    core.save_profile(profile.clone(), None).await?;
    let session = core.start(&profile.id).await?;
    Ok((core, session, host))
}

fn audit_rows(data: &Path, profile: &Profile) -> Result<Vec<Value>> {
    let relative = tobkiri_store::DataRoot::profile_relative(&profile.id, "audit.sqlite")?;
    let connection = rusqlite::Connection::open_with_flags(
        data.join(relative),
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    )?;
    let mut statement = connection.prepare("SELECT body FROM audit ORDER BY seq LIMIT 4097")?;
    let mut rows = statement.query([])?;
    let mut events = Vec::new();
    while let Some(row) = rows.next()? {
        ensure!(events.len() < 4096, "fixture audit exceeded its bound");
        let bytes: Vec<u8> = row.get(0)?;
        ensure!(bytes.len() <= 64 * 1024, "fixture audit row exceeded bound");
        events.push(serde_json::from_slice(&bytes)?);
    }
    Ok(events)
}

fn terminal(
    rows: &[Value],
    principal: &Value,
    effect: &EffectRequest,
    args: &tobkiri_canon::Digest,
) -> Result<bool> {
    Ok(terminal_count(rows, principal, effect, args)? == 1)
}

fn terminal_count(
    rows: &[Value],
    principal: &Value,
    effect: &EffectRequest,
    args: &tobkiri_canon::Digest,
) -> Result<usize> {
    let effect = effect.digest()?.to_string();
    let args = args.to_string();
    let dispatches: Vec<_> = rows
        .iter()
        .filter(|row| {
            row["kind"] == "dispatch"
                && &row["principal"] == principal
                && row["payload"]["effect_digest"] == effect
                && row["payload"]["args_digest"] == args
        })
        .collect();
    for dispatch in &dispatches {
        if !dispatch["payload"]["grant"].is_string()
            || rows
                .iter()
                .filter(|row| {
                    row["kind"] == "outcome"
                        && &row["principal"] == principal
                        && row["request"] == dispatch["request"]
                        && row["payload"]["grant"] == dispatch["payload"]["grant"]
                        && row["payload"]["effect_digest"] == effect
                        && row["payload"]["args_digest"] == args
                })
                .count()
                != 1
        {
            return Ok(0);
        }
    }
    Ok(dispatches.len())
}

fn terminal_with_outcome(
    rows: &[Value],
    principal: &Value,
    effect: &EffectRequest,
    args: &tobkiri_canon::Digest,
    expected: &Outcome,
) -> Result<bool> {
    if terminal_count(rows, principal, effect, args)? != 1 {
        return Ok(false);
    }
    let effect = effect.digest()?.to_string();
    let args = args.to_string();
    let dispatch = rows
        .iter()
        .find(|row| {
            row["kind"] == "dispatch"
                && &row["principal"] == principal
                && row["payload"]["effect_digest"] == effect
                && row["payload"]["args_digest"] == args
        })
        .context("checked dispatch missing")?;
    let expected = serde_json::to_value(expected)?;
    Ok(rows.iter().any(|row| {
        row["kind"] == "outcome"
            && &row["principal"] == principal
            && row["request"] == dispatch["request"]
            && row["payload"]["grant"] == dispatch["payload"]["grant"]
            && row["payload"]["effect_digest"] == effect
            && row["payload"]["args_digest"] == args
            && row["payload"]["outcome"] == expected
    }))
}

fn core_error_kind(error: &CoreError) -> &'static str {
    match error {
        CoreError::Cleanup { .. } => "cleanup",
        CoreError::Invalid(_) => "invalid-configuration",
        CoreError::Conflicts(_) => "profile-conflicts",
        CoreError::NotFound(_) => "not-found",
        CoreError::Gate(_) => "gate",
        CoreError::Step(_) => "step",
        CoreError::Io(_) => "io",
        CoreError::Json(_) => "json",
        CoreError::Canon(_) => "canon",
        CoreError::Store(_) => "store",
        CoreError::Manifest(_) => "manifest",
        CoreError::Profile(_) => "profile",
        CoreError::Runtime(_) => "runtime",
    }
}

struct Observed {
    record: Value,
    value: Option<CanonValue>,
    error: Option<(String, String)>,
    audit: Vec<Value>,
    gate_terminal: bool,
}
struct Harness<'a> {
    session: &'a Session,
    data: &'a Path,
    profile: &'a Profile,
    evidence: &'a Evidence,
}

impl Harness<'_> {
    async fn call(&self, role: usize, op: &str, args: &CanonValue, stem: &str) -> Result<Observed> {
        let before = audit_rows(self.data, self.profile)?.len();
        let shape = ShapeRef::parse(ROLES[role].shape)?;
        let effect = EffectRequest::PortCall {
            shape: shape.clone(),
            op: OpName::parse(op)?,
        };
        let args_digest = tobkiri_canon::digest(args, Variant::StrictV1).ok();
        let mut future =
            std::pin::pin!(self.session.call_user_port(shape, OpName::parse(op)?, args));
        let mut record = json!({"role":ROLES[role].label,"operation":op,"observed":true,"argument_digest_available":args_digest.is_some()});
        let result = match tokio::time::timeout(Duration::from_secs(30), &mut future).await {
            Ok(actual) => Some(actual),
            Err(_) => {
                // Retain the original pending future while closing this same
                // Session. There is no replacement call or claimed physical cleanup.
                let close =
                    tokio::time::timeout(Duration::from_secs(5), self.session.close()).await;
                record["status"] = json!("fixture-timeout");
                record["same_session_close_succeeded"] = json!(matches!(close, Ok(Ok(()))));
                None
            }
        };
        let mut value = None;
        let mut error = None;
        match result {
            Some(Ok(buffer)) => {
                // Borrow the original charged producer buffer for raw capture
                // and parsing. Its allocation owner stays alive until both finish.
                record["core_envelope"] = self.evidence.save(
                    &format!("{stem}.core.json"),
                    "core-strict-v1",
                    buffer.as_bytes(),
                )?;
                match parse_output(&buffer) {
                    Ok(parsed) => {
                        if let CanonValue::Str(raw) = &parsed {
                            record["status"] = json!("result");
                            record["output"] = self.evidence.save(
                                &format!("{stem}.output"),
                                "utf-8",
                                raw.as_bytes(),
                            )?;
                        } else {
                            record["status"] = json!("value");
                        }
                        value = Some(parsed);
                    }
                    Err(_) => {
                        record["status"] = json!("invalid-core-envelope");
                    }
                }
            }
            Some(Err(CoreError::Step(step))) => {
                record["status"] = json!("step-error");
                record["code"] = json!(step.code);
                record["message"] = json!(step.message);
                error = Some((step.code, step.message));
            }
            Some(Err(other)) => {
                record["status"] = json!("core-error");
                record["kind"] = json!(core_error_kind(&other));
                record["message"] = json!(other.to_string());
            }
            None => {}
        }
        let rows = audit_rows(self.data, self.profile)?;
        let audit = rows
            .get(before..)
            .context("audit sequence shrank")?
            .to_vec();
        let gate_terminal = match args_digest {
            Some(args) => terminal(&audit, &json!({"kind":"user"}), &effect, &args)?,
            None => false,
        };
        record["gate_dispatch_and_terminal"] = json!(gate_terminal);
        record["audit"] = self
            .evidence
            .json(&format!("{stem}.audit.json"), &json!(audit))?;
        if record["status"] != "result" && record["status"] != "value" {
            record["error_sidecar"] = self.evidence.json(&format!("{stem}.error.json"), &record)?;
        }
        Ok(Observed {
            record,
            value,
            error,
            audit,
            gate_terminal,
        })
    }
}

fn runtime_matches(pack: &Pack, raw: &str) -> Result<bool> {
    tobkiri_canon::parse(raw.as_bytes(), Variant::StrictV1)?;
    let value: Value = serde_json::from_str(raw)?;
    Ok(if pack.rust {
        value["implementation"]
            .as_str()
            .is_some_and(|value| value.eq_ignore_ascii_case("rust"))
            && (value["candidate"] == pack.runtime_id || value["pack_id"] == pack.runtime_id)
            && value["protocol"] == "ordered-finite-json-v2"
            && value.get("python").is_none()
    } else {
        value["python"]
            .as_str()
            .is_some_and(|version| version.starts_with("3.13."))
            && value["unicode"] == "15.1.0"
    })
}

async fn runtimes(harness: &Harness<'_>, packs: &[Pack]) -> Result<Vec<Value>> {
    let mut records = Vec::new();
    for pack in packs {
        let role = pack.role;
        let mut observed = harness
            .call(
                role,
                "runtime-info",
                &CanonValue::Obj(BTreeMap::new()),
                &format!("runtime-{role}"),
            )
            .await?;
        let matched = match &observed.value {
            Some(CanonValue::Str(raw)) => runtime_matches(pack, raw).unwrap_or(false),
            _ => false,
        } && observed.gate_terminal;
        observed.record["matched"] = json!(matched);
        observed.record["truthful_actual_runtime"] = json!(matched);
        records.push(observed.record);
    }
    Ok(records)
}

async fn provider_case(
    harness: &Harness<'_>,
    oracle: &Oracle,
    case: &Value,
    stem: &str,
    attempted: &mut bool,
) -> Result<Value> {
    let role = match text(case, "provider")? {
        "usage-cost" => 0,
        "stream-normalizer" => 1,
        "tool-bridge" => 2,
        _ => anyhow::bail!("unexpected provider corpus identity"),
    };
    let id = text(case, "id")?;
    ensure!(
        id.len() <= 128
            && id
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-'),
        "invalid static case ID"
    );
    let input = inputs::checked(&oracle.directory, &case["input"])?;
    let operation = text(case, "operation")?;
    let args = arguments(Some(operation), std::str::from_utf8(&input)?);
    *attempted = true;
    let mut observed = harness
        .call(role, ROLES[role].function, &args, stem)
        .await?;
    let expected = &case["expected"];
    let exact = match text(expected, "kind")? {
        "result" => {
            let expected = inputs::checked(&oracle.directory, &expected["output"])?;
            matches!(&observed.value, Some(CanonValue::Str(raw)) if raw.as_bytes() == expected.as_slice())
        }
        "error" => observed.error.as_ref().is_some_and(|(code, message)| {
            Some(code.as_str()) == expected["code"].as_str()
                && public_message(code) == Some(message.as_str())
        }),
        _ => anyhow::bail!("unknown original provider expectation"),
    };
    let matched = exact && observed.gate_terminal;
    observed.record["id"] = json!(id);
    observed.record["provider"] = case["provider"].clone();
    observed.record["original_input"] = case["input"].clone();
    observed.record["original_operation"] = json!(operation);
    observed.record["original_adapter_expectation"] = expected.clone();
    observed.record["exact_core_result_or_closed_error"] = json!(exact);
    observed.record["core_call_attempted"] = json!(true);
    observed.record["matched"] = json!(matched);
    // Core intentionally closes guest error text. The fixed original app
    // message remains evidence, never falsely labelled observed Core text.
    observed.record["raw_guest_error_message_observed"] = json!(false);
    if !matched {
        eprintln!(
            "provider case {id}: mismatch; actual status {}",
            text(&observed.record, "status")?
        );
    }
    Ok(observed.record)
}

async fn provider_cases(
    harness: &Harness<'_>,
    oracle: &Oracle,
    prefix: &str,
) -> Result<Vec<Value>> {
    let mut records = Vec::new();
    for (index, case) in oracle.document["cases"]
        .as_array()
        .context("provider cases missing")?
        .iter()
        .enumerate()
    {
        let stem = format!("{prefix}-{index:03}");
        let mut attempted = false;
        let record = match provider_case(harness, oracle, case, &stem, &mut attempted).await {
            Ok(record) => record,
            Err(error) => {
                // Retain fixture failures and continue the same Session so an
                // admission failure never silently removes a corpus member.
                let mut record = json!({"id":case["id"],"provider":case["provider"],"original_input":case["input"],"original_adapter_expectation":case["expected"],"evidence_stem":stem,"status":"fixture-error","fixture_failure":format!("{error:#}"),"core_call_attempted":attempted,"matched":false,"raw_guest_error_message_observed":false});
                record["error_sidecar"] = harness
                    .evidence
                    .json(&format!("{stem}.fixture-error.json"), &record)?;
                record
            }
        };
        harness
            .evidence
            .json(&format!("{stem}.case.json"), &record)?;
        records.push(record);
    }
    Ok(records)
}

fn caller_outcome(case: &Value, nested: bool) -> Result<Outcome> {
    Ok(match text(case, "classification")? {
        "success" => Outcome::Succeeded,
        "provider-invalid" | "provider-nonfinite" => Outcome::Failed {
            code: if nested {
                "provider-error"
            } else {
                text(&case["expected"], "code")?
            }
            .into(),
        },
        _ => anyhow::bail!("unknown original caller classification"),
    })
}

/// Same terminal-prefix rule as ordered_python/gateway_tool_caller.rs. The
/// original source trace is retained; it never supplies runtime observations.
fn caller_dependency_audit(case: &Value, rows: &[Value], packs: &[Pack]) -> Result<Value> {
    let source = case["source_reference_calls"]
        .as_array()
        .context("caller source calls missing")?;
    let expected = if case["id"] == "events-later-nonfinite" {
        ensure!(
            case["classification"] == "provider-nonfinite" && source.len() == 3,
            "original caller terminal-prefix source changed"
        );
        &source[..2]
    } else {
        source.as_slice()
    };
    let caller = packs
        .iter()
        .find(|pack| pack.role == 3)
        .context("caller Pack missing")?;
    let owner =
        json!({"kind":"pack","pack":caller.verified.manifest.id,"hash":caller.verified.hash});
    let target = EffectRequest::PortCall {
        shape: ShapeRef::parse(ROLES[2].shape)?,
        op: OpName::parse("normalize")?,
    };
    let effect = target.digest()?.to_string();
    let dispatched: Vec<_> = rows
        .iter()
        .enumerate()
        .filter(|(_, row)| row["kind"] == "dispatch" && row["payload"]["effect_digest"] == effect)
        .collect();
    let mut edges = Vec::new();
    for (index, call) in expected.iter().enumerate() {
        let args = arguments(Some(text(call, "operation")?), text(call, "payload_json")?);
        let args = tobkiri_canon::digest(&args, Variant::StrictV1)?;
        let outcome = if index + 1 == expected.len() {
            caller_outcome(case, true)?
        } else {
            Outcome::Succeeded
        };
        let exact = terminal_with_outcome(rows, &owner, &target, &args, &outcome)?;
        let outcome_value = serde_json::to_value(&outcome)?;
        let ordered = dispatched.get(index).is_some_and(|(position, row)| {
            row["principal"] == owner
                && row["payload"]["args_digest"] == args.to_string()
                && rows
                    .iter()
                    .enumerate()
                    .any(|(terminal_position, terminal)| {
                        terminal["kind"] == "outcome"
                            && terminal["principal"] == owner
                            && terminal["request"] == row["request"]
                            && terminal["payload"]["grant"] == row["payload"]["grant"]
                            && terminal["payload"]["effect_digest"] == effect
                            && terminal["payload"]["args_digest"] == args.to_string()
                            && terminal["payload"]["outcome"] == outcome_value
                            && terminal_position > *position
                            && dispatched
                                .get(index + 1)
                                .is_none_or(|(next, _)| terminal_position < *next)
                    })
        });
        edges.push(json!({"args_digest":args,"effect_digest":effect,"expected_outcome":outcome,"exact_terminal":exact,"ordered_dispatch_and_terminal":ordered}));
    }
    let denials = rows
        .iter()
        .filter(|row| {
            row["kind"] == "denied"
                && row["principal"] == owner
                && row["payload"][0] == "port-call"
                && row["payload"][1] == "denied"
        })
        .count();
    // Runtime probes have already initialized all five Packs. This caller's
    // only admitted dependency is bridge@2; additional Pack effects fail.
    let pack_dispatches = rows
        .iter()
        .filter(|row| row["kind"] == "dispatch" && row["principal"]["kind"] == "pack")
        .count();
    let matched = dispatched.len() == expected.len()
        && pack_dispatches == expected.len()
        && denials == 0
        && edges.iter().all(|edge| {
            edge["exact_terminal"] == true && edge["ordered_dispatch_and_terminal"] == true
        });
    Ok(
        json!({"matched":matched,"observed_bridge_dispatch_count":dispatched.len(),"observed_pack_dispatch_count":pack_dispatches,"observed_caller_denials":denials,"expected_terminal_prefix_count":expected.len(),"original_source_reference_call_count":source.len(),"source_count_is_not_runtime_evidence":true,"edges":edges}),
    )
}

async fn caller_case(
    harness: &Harness<'_>,
    oracle: &Oracle,
    case: &Value,
    packs: &[Pack],
    stem: &str,
    attempted: &mut bool,
) -> Result<Value> {
    let payload = text(case, "input_json")?;
    let operation = text(case, "operation")?;
    let args = arguments(None, payload);
    *attempted = true;
    let mut observed = harness.call(3, operation, &args, stem).await?;
    let exact = match text(case, "classification")? {
        "success" => {
            matches!(&observed.value, Some(CanonValue::Str(output)) if Some(output.as_str()) == case["output_json"].as_str())
        }
        "provider-invalid" | "provider-nonfinite" => {
            observed.error.as_ref().is_some_and(|(code, message)| {
                Some(code.as_str()) == case["expected"]["code"].as_str()
                    && public_message(code) == Some(message.as_str())
            })
        }
        _ => false,
    };
    let target = EffectRequest::PortCall {
        shape: ShapeRef::parse(ROLES[3].shape)?,
        op: OpName::parse(operation)?,
    };
    let args_digest = tobkiri_canon::digest(&args, Variant::StrictV1)?;
    let user_terminal = terminal_with_outcome(
        &observed.audit,
        &json!({"kind":"user"}),
        &target,
        &args_digest,
        &caller_outcome(case, false)?,
    )?;
    let nested = caller_dependency_audit(case, &observed.audit, packs)?;
    let matched = exact && user_terminal && nested["matched"] == true;
    observed.record["original_case"] = case.clone();
    observed.record["original_oracle_sha256"] = oracle.identity["sha256"].clone();
    observed.record["id"] = case["id"].clone();
    observed.record["classification"] = case["classification"].clone();
    observed.record["core_call_attempted"] = json!(true);
    observed.record["exact_core_result_or_closed_error"] = json!(exact);
    observed.record["exact_user_terminal"] = json!(user_terminal);
    observed.record["dependency_audit"] = nested;
    observed.record["matched"] = json!(matched);
    observed.record["admitted_parity"] =
        json!(matched && case["classification"] != "provider-nonfinite");
    observed.record["nonfinite_boundary_not_original_error_parity"] =
        json!(matched && case["classification"] == "provider-nonfinite");
    observed.record["raw_guest_error_message_observed"] = json!(false);
    observed.record["partial_stream_mutations_observed"] = json!(false);
    observed.record["internal_guest_error_point_observed"] = json!(false);
    observed.record["retry_parity_claimed"] = json!(false);
    Ok(observed.record)
}

async fn caller_cases(
    harness: &Harness<'_>,
    oracle: &Oracle,
    packs: &[Pack],
) -> Result<Vec<Value>> {
    let mut records = Vec::new();
    for (index, case) in oracle.document["cases"]
        .as_array()
        .context("caller cases missing")?
        .iter()
        .enumerate()
    {
        let stem = format!("caller-{index:03}");
        let mut attempted = false;
        let record = match caller_case(harness, oracle, case, packs, &stem, &mut attempted).await {
            Ok(record) => record,
            Err(error) => {
                let mut record = json!({"id":case["id"],"classification":case["classification"],"original_case":case,"original_oracle_sha256":oracle.identity["sha256"],"evidence_stem":stem,"status":"fixture-error","fixture_failure":format!("{error:#}"),"core_call_attempted":attempted,"matched":false,"admitted_parity":false,"nonfinite_boundary_not_original_error_parity":false,"partial_stream_mutations_observed":false,"internal_guest_error_point_observed":false,"raw_guest_error_message_observed":false,"retry_parity_claimed":false});
                record["error_sidecar"] = harness
                    .evidence
                    .json(&format!("{stem}.fixture-error.json"), &record)?;
                record
            }
        };
        harness
            .evidence
            .json(&format!("{stem}.case.json"), &record)?;
        records.push(record);
    }
    Ok(records)
}

fn dependency_edges(case: &Value, rows: &[Value], packs: &[Pack]) -> Result<bool> {
    struct ExpectedEdge {
        principal: Value,
        effect: EffectRequest,
        args: tobkiri_canon::Digest,
        count: usize,
    }
    let mut edges: Vec<ExpectedEdge> = Vec::new();
    let mut expected_count = 0;
    for (field, principal_role) in [("source_reference_calls", 4), ("source_bridge_calls", 3)] {
        for call in case[field]
            .as_array()
            .context("source dependency calls missing")?
        {
            expected_count += 1;
            let role = match text(call, "provider")? {
                "usage-cost" => 0,
                "stream-normalizer" => 1,
                "bridge" => 2,
                "caller" => 3,
                _ => anyhow::bail!("unknown source dependency"),
            };
            let operation = text(call, "operation")?;
            let args = arguments(
                (role != 3).then_some(operation),
                text(call, "payload_json")?,
            );
            let pack = packs
                .iter()
                .find(|pack| pack.role == principal_role)
                .context("dependency principal missing")?;
            let effect = EffectRequest::PortCall {
                shape: ShapeRef::parse(ROLES[role].shape)?,
                op: OpName::parse(if role == 3 {
                    operation
                } else {
                    ROLES[role].function
                })?,
            };
            let principal =
                json!({"kind":"pack","pack":pack.verified.manifest.id,"hash":pack.verified.hash});
            let args = tobkiri_canon::digest(&args, Variant::StrictV1)?;
            if let Some(edge) = edges.iter_mut().find(|edge| {
                edge.principal == principal && edge.effect == effect && edge.args == args
            }) {
                edge.count += 1;
            } else {
                edges.push(ExpectedEdge {
                    principal,
                    effect,
                    args,
                    count: 1,
                });
            }
        }
    }
    for edge in edges {
        if terminal_count(rows, &edge.principal, &edge.effect, &edge.args)? != edge.count {
            return Ok(false);
        }
    }
    // All five providers were explicitly runtime-probed first, so there are
    // no lazy init dispatches in a finalizer case. Any extra Pack dispatch is
    // an unexpected dependency/effect and must not be hidden by the source list.
    Ok(rows
        .iter()
        .filter(|row| row["kind"] == "dispatch" && row["principal"]["kind"] == "pack")
        .count()
        == expected_count)
}

async fn composition_cases(
    harness: &Harness<'_>,
    oracle: &Oracle,
    packs: &[Pack],
) -> Result<Vec<Value>> {
    let mut records = Vec::new();
    for (index, case) in oracle.document["cases"]
        .as_array()
        .context("finalizer cases missing")?
        .iter()
        .enumerate()
    {
        let id = text(case, "id")?;
        let mut observed = harness
            .call(
                4,
                text(case, "operation")?,
                &arguments(None, text(case, "input_json")?),
                &format!("composition-{index:03}"),
            )
            .await?;
        let expected = &case["expected"];
        let exact = match text(expected, "status")? {
            "finalized" => {
                if let Some(value) = &observed.value {
                    let output = value.get("val").and_then(CanonValue::as_str);
                    if let Some(output) = output {
                        observed.record["finalized_output"] = harness.evidence.save(
                            &format!("composition-{index:03}.output"),
                            "utf-8",
                            output.as_bytes(),
                        )?;
                    }
                    value.get("tag").and_then(CanonValue::as_str) == Some("finalized")
                        && output == expected["output_json"].as_str()
                } else {
                    false
                }
            }
            "domain-rejected" => observed.value.as_ref().is_some_and(|value| {
                value.get("tag").and_then(CanonValue::as_str) == Some("domain-rejected")
                    && value
                        .get("val")
                        .and_then(|value| value.get("code"))
                        .and_then(CanonValue::as_str)
                        == expected["code"].as_str()
                    && value
                        .get("val")
                        .and_then(|value| value.get("message"))
                        .and_then(CanonValue::as_str)
                        == expected["message"].as_str()
            }),
            "core-error" => observed.error.as_ref().is_some_and(|(code, message)| {
                Some(code.as_str()) == expected["code"].as_str()
                    && public_message(code) == Some(message.as_str())
            }),
            _ => anyhow::bail!("unknown finalizer source expectation"),
        };
        let edges = dependency_edges(case, &observed.audit, packs)?;
        observed.record["id"] = json!(id);
        observed.record["original_expectation"] = expected.clone();
        observed.record["exact_result_or_closed_error"] = json!(exact);
        observed.record["actual_dependency_dispatches_and_terminals"] = json!(edges);
        observed.record["matched"] = json!(exact && edges && observed.gate_terminal);
        if observed.record["matched"] != true {
            eprintln!("composition case {id}: mismatch");
        }
        harness.evidence.json(
            &format!("composition-{index:03}.case.json"),
            &observed.record,
        )?;
        records.push(observed.record);
    }
    Ok(records)
}

async fn execute(evidence: &Evidence, report: &mut Value, composition: bool) -> Result<()> {
    let repo = repo();
    let oracle = inputs::oracle(&repo, false)?;
    report["original_provider_oracle"] = oracle.identity.clone();
    let composition_oracle = if composition {
        Some(inputs::oracle(&repo, true)?)
    } else {
        None
    };
    let bridge_oracle = if composition {
        inputs::bridge_oracle(&repo)?
    } else {
        None
    };
    let caller_oracle = if composition {
        inputs::caller_oracle(&repo)?
    } else {
        None
    };
    if let Some(oracle) = &composition_oracle {
        report["original_finalizer_oracle"] = oracle.identity.clone();
    }
    if let Some(oracle) = &bridge_oracle {
        report["original_bridge_oracle"] = oracle.identity.clone();
    }
    if let Some(oracle) = &caller_oracle {
        report["original_caller_oracle"] = oracle.identity.clone();
    }
    let temporary = physical_temp()?;
    let packs = (0..if composition { 5 } else { 2 })
        .map(|role| inputs::materialize(&repo, temporary.path(), role))
        .collect::<Result<Vec<_>>>()?;
    report["artifacts"] = json!(packs.iter().map(|pack| &pack.identity).collect::<Vec<_>>());
    let profile = profile(&packs, composition)?;
    report["profile"] = evidence.json("profile.json", &serde_json::to_value(&profile)?)?;
    let data = temporary.path().join("data");
    let (_core, session, host) = match start(&data, &profile, &packs, caller_oracle.is_some()).await
    {
        Ok(started) => started,
        Err(error) => {
            report["retained_failed_fixture_directory"] = json!(temporary.keep());
            return Err(error);
        }
    };
    let harness = Harness {
        session: &session,
        data: &data,
        profile: &profile,
        evidence,
    };
    let result = async {
        report["runtimes"] = json!(runtimes(&harness, &packs).await?);
        report["provider_cases"] = json!(provider_cases(&harness, &oracle, "provider").await?);
        if let Some(oracle) = &bridge_oracle { report["bridge_cases"] = json!(provider_cases(&harness, oracle, "bridge").await?); }
        if let Some(oracle) = &caller_oracle { report["caller_cases"] = json!(caller_cases(&harness, oracle, &packs).await?); }
        if let Some(oracle) = &composition_oracle { report["composition_cases"] = json!(composition_cases(&harness, oracle, &packs).await?); }
        let rows = audit_rows(&data, &profile)?;
        let mut initialization = Vec::new();
        for pack in &packs {
            let effect = EffectRequest::WasmEntry { pack: pack.verified.manifest.id.clone(), hash: pack.verified.hash.clone(), entry: tobkiri_gate::WasmEntryKind::Init };
            initialization.push(json!({"role":ROLES[pack.role].label,"gate_init_dispatch_and_terminal":terminal(&rows, &json!({"kind":"pack","pack":pack.verified.manifest.id,"hash":pack.verified.hash}), &effect, &effect.digest()?)?}));
        }
        report["physical_initialization"] = json!(initialization);
        report["audit"] = evidence.json("session.audit.json", &json!(rows))?;
        report["reviewed_pack_permission_approvals"] = json!(*host.approvals.lock().unwrap());
        Ok::<(), anyhow::Error>(())
    }.await;
    let close = tokio::time::timeout(Duration::from_secs(30), session.close()).await;
    report["session_close_awaited"] = json!(true);
    report["session_close_succeeded"] = json!(matches!(close, Ok(Ok(()))));
    report["guest_lifecycle_shutdown_claimed"] = json!(false);
    if report["session_close_succeeded"] != true {
        report["retained_failed_fixture_directory"] = json!(temporary.keep());
    }
    result?;
    ensure!(
        report["session_close_succeeded"] == true,
        "public Session::close did not succeed"
    );
    Ok(())
}

async fn replay(composition: bool) -> Result<()> {
    let evidence = Evidence::new(composition)?;
    let bridge_requested = composition
        && ["BRIDGE_ORACLE", "BRIDGE_ORACLE_SHA256"]
            .iter()
            .any(|suffix| std::env::var_os(format!("{}_{}", inputs::PREFIX, suffix)).is_some());
    let caller_requested = composition
        && ["CALLER_ORACLE", "CALLER_ORACLE_SHA256"]
            .iter()
            .any(|suffix| std::env::var_os(format!("{}_{}", inputs::PREFIX, suffix)).is_some());
    let mut report = json!({"schema":"io.tobkiri.rust-provider-core-replay.v1","host_os":std::env::consts::OS,"host_arch":std::env::consts::ARCH,"scope":"actual components through Core/Gate/Profile/Session with scripted fixture approvals; no GUI or full Gateway acceptance","composition_requested":composition,"bridge_oracle_requested":bridge_requested,"caller_oracle_requested":caller_requested,"core_64mib_catalog_limit_changed":false,"expected_values_generated_by_rust":false,"source_build_provenance_verified":false,"all_cases_attempted":false,"passed":false,"provider_cases":[],"bridge_cases":[],"caller_cases":[],"composition_cases":[],"runtimes":[]});
    let result = execute(&evidence, &mut report, composition).await;
    if let Err(error) = &result {
        report["fixture_failure"] = json!(format!("{error:#}"));
    }
    let provider_count = report["provider_cases"]
        .as_array()
        .context("provider records missing")?
        .len();
    let composition_count = report["composition_cases"]
        .as_array()
        .context("composition records missing")?
        .len();
    let bridge_count = report["bridge_cases"]
        .as_array()
        .context("bridge records missing")?
        .len();
    let caller_count = report["caller_cases"]
        .as_array()
        .context("caller records missing")?
        .len();
    let mismatch_count = [
        "runtimes",
        "provider_cases",
        "bridge_cases",
        "caller_cases",
        "composition_cases",
    ]
    .iter()
    .map(|field| {
        report[*field].as_array().map_or(0, |records| {
            records
                .iter()
                .filter(|record| record["matched"] != true)
                .count()
        })
    })
    .sum::<usize>();
    let provider_attempts = ["provider_cases", "bridge_cases", "caller_cases"]
        .iter()
        .all(|field| {
            report[*field].as_array().is_some_and(|records| {
                records
                    .iter()
                    .all(|record| record["core_call_attempted"] == true)
            })
        });
    let all_attempted = provider_attempts
        && provider_count == 94
        && bridge_count == if bridge_requested { 81 } else { 0 }
        && caller_count == if caller_requested { 30 } else { 0 }
        && composition_count == if composition { 10 } else { 0 };
    let init = report["physical_initialization"]
        .as_array()
        .is_some_and(|records| {
            records.len() == if composition { 5 } else { 2 }
                && records
                    .iter()
                    .all(|record| record["gate_init_dispatch_and_terminal"] == true)
        });
    let passed = result.is_ok() && all_attempted && mismatch_count == 0 && init;
    report["all_cases_attempted"] = json!(all_attempted);
    report["case_counts"] = json!({"provider":provider_count,"bridge":bridge_count,"caller":caller_count,"composition":composition_count});
    report["mismatch_count"] = json!(mismatch_count);
    report["passed"] = json!(passed);
    evidence.json("summary.json", &report)?;
    ensure!(
        passed,
        "Core provider replay incomplete or mismatched; inspect private summary/sidecars"
    );
    Ok(())
}

#[tokio::test]
#[ignore = "requires hash-pinned real provider Wasm components, original 94-case oracle and NEW external evidence"]
async fn rust_wasm_providers_replay_all_original_cases_through_core() -> Result<()> {
    replay(false).await
}

#[tokio::test]
#[ignore = "requires five real artifacts, explicit finalizer implementation, original 94+10 observations, optional complete bridge81/caller30 oracles and NEW composition evidence"]
async fn rust_providers_and_gateway_roles_compose_through_core() -> Result<()> {
    replay(true).await
}
