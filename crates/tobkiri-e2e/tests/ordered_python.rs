//! Opt-in real ordered Python component acceptance through the public Core.
//!
//! Materialize with scripts/wasm/materialize_ordered_tool_bridge.py first.
//! Set TOBKIRI_TEST_ORDERED_PYTHON_PACK_DIR and the explicit PACK_SHA256,
//! COMPONENT_SHA256, RECEIPT_SHA256 pins under the same prefix. Set EVIDENCE
//! to a fresh absolute output path outside the checkout. Then run:
//! cargo test -p tobkiri-e2e --test ordered_python
//!   ordered_python_bridge_replays_through_real_core -- --ignored --exact
//!
//! This is not the official 91-case shell-policy replay. Nine known domain
//! exclusions remain feature/error-boundary gaps, never full-unit parity.
#[path = "ordered_python/consumer.rs"]
mod consumer;
#[path = "ordered_python/evidence.rs"]
mod evidence;
mod support;

use anyhow::{Context, Result, ensure};
use evidence::{Inputs, PACK, SHAPE};
use serde_json::{Value, json};
use std::{collections::BTreeMap, path::Path, sync::Arc};
use support::{Host, canon, parse_output, physical_temp, repo};
use tobkiri_canon::{
    CanonValue, Variant,
    ids::{OpName, ShapeRef},
};
use tobkiri_core::{Core, CoreConfig, CoreError, Session};
use tobkiri_gate::PermissionSet;
use tobkiri_manifest::VerifiedPack;
use tobkiri_profile::{PackPin, Profile};

const EXPECTED_RUNTIME: &str = "3.13.16";
const EXPECTED_UNICODE: &str = "15.1.0";

fn profile(
    inputs: &Inputs,
    caller: Option<&(PackPin, VerifiedPack)>,
    allow: bool,
    id: &str,
) -> Result<Profile> {
    let candidate = PackPin {
        id: inputs.pack.manifest.id.clone(),
        version: inputs.pack.manifest.version.clone(),
        sha256: inputs.pack.hash.clone(),
        source: format!("file:{}", inputs.source.display()),
    };
    let mut pins = vec![candidate];
    let mut limits = BTreeMap::from([(
        inputs.pack.manifest.id.clone(),
        inputs.pack.manifest.permissions.clone(),
    )]);
    let mut bindings = BTreeMap::from([(
        "local.ai-tool-bridge-ordered-python.bridge@1".to_owned(),
        PACK.to_owned(),
    )]);
    if let Some((pin, pack)) = caller {
        pins.push(pin.clone());
        let mut permissions = pack.manifest.permissions.clone();
        if !allow {
            permissions.ports.clear();
        }
        limits.insert(pin.id.clone(), permissions);
        bindings.insert(
            "local.ordered-python-consumer.entry@1".to_owned(),
            consumer::CONSUMER.to_owned(),
        );
    }
    let mut ceiling = inputs.pack.manifest.permissions.clone();
    ceiling.ports.insert(SHAPE.to_owned());
    if caller.is_some() {
        ceiling.ports.insert(consumer::ENTRY.to_owned());
    }
    let user = PermissionSet {
        ports: [if caller.is_some() {
            consumer::ENTRY
        } else {
            SHAPE
        }
        .to_owned()]
        .into_iter()
        .collect(),
        wasm: ceiling.wasm.clone(),
        loops: ceiling.loops.clone(),
        ..Default::default()
    };
    Ok(serde_json::from_value(json!({
        "format":"tobkiri.profile/1", "id":id, "name":"Ordered Python native fixture",
        "packs":pins, "publishers":{}, "ceiling":ceiling, "pack_limits":limits,
        "user_permissions":user, "bindings":bindings, "overlay_order":[],
        "user_overlays":[], "startup":{"inputs":[], "ui":null}
    }))?)
}

async fn start(data: &Path, profile: &Profile, consumer: bool) -> Result<(Core, Session)> {
    let host = Arc::new(Host::default());
    for (shape, op) in if consumer {
        vec![(consumer::ENTRY, "run")]
    } else {
        vec![(SHAPE, "runtime-info"), (SHAPE, "normalize")]
    } {
        host.utility_ports
            .lock()
            .unwrap()
            .insert((shape.into(), op.into()));
    }
    host.approve_literal_user(profile)?;
    let core = Core::open(CoreConfig::for_test_data_root(data.to_path_buf()), host)?;
    core.save_profile(profile.clone(), None).await?;
    let session = core.start(&profile.id).await?;
    Ok((core, session))
}

/// Trusted, bounded, read-only inspection, following e2e/llama.rs. This grants
/// neither the User fixture nor either guest an audit-read capability.
fn audit_rows(data: &Path, profile: &Profile) -> Result<Vec<Value>> {
    let relative = tobkiri_store::DataRoot::profile_relative(&profile.id, "audit.sqlite")?;
    let connection = rusqlite::Connection::open_with_flags(
        data.join(relative),
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    )?;
    let mut statement = connection.prepare("SELECT body FROM audit ORDER BY seq LIMIT 1001")?;
    let mut rows = statement.query([])?;
    let mut events = Vec::new();
    while let Some(row) = rows.next()? {
        ensure!(
            events.len() < 1000,
            "fixture audit inventory exceeded bound"
        );
        let bytes: Vec<u8> = row.get(0)?;
        ensure!(bytes.len() <= 64 * 1024, "fixture audit row exceeded bound");
        events.push(serde_json::from_slice(&bytes)?);
    }
    Ok(events)
}

fn pack_events(rows: &[Value], kind: &str, pack: &VerifiedPack) -> usize {
    rows.iter()
        .filter(|row| {
            row["kind"] == kind
                && row["principal"]["kind"] == "pack"
                && row["principal"]["pack"] == pack.manifest.id.as_str()
                && row["principal"]["hash"] == pack.hash.to_string()
        })
        .count()
}

fn terminal_pair(
    rows: &[Value],
    principal: &Value,
    effect: &tobkiri_gate::EffectRequest,
    args: &tobkiri_canon::Digest,
) -> Result<bool> {
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
    if dispatches.len() != 1 {
        return Ok(false);
    }
    let dispatch = dispatches[0];
    Ok(dispatch["payload"]["grant"].is_string()
        && rows
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
            == 1)
}

fn provider_initialized(rows: &[Value], provider: &VerifiedPack) -> Result<bool> {
    let effect = tobkiri_gate::EffectRequest::WasmEntry {
        pack: provider.manifest.id.clone(),
        hash: provider.hash.clone(),
        entry: tobkiri_gate::WasmEntryKind::Init,
    };
    terminal_pair(
        rows,
        &json!({"kind":"pack", "pack":provider.manifest.id, "hash":provider.hash}),
        &effect,
        &effect.digest()?,
    )
}

fn runtime(value: CanonValue) -> Result<Value> {
    let CanonValue::Str(raw) = value else {
        anyhow::bail!("runtime-info did not return a WIT string");
    };
    tobkiri_canon::parse(raw.as_bytes(), Variant::StrictV1)?;
    Ok(serde_json::from_str(&raw)?)
}

fn require_runtime(observed: &Value) -> Result<()> {
    ensure!(
        *observed == json!({"python":EXPECTED_RUNTIME, "unicode":EXPECTED_UNICODE}),
        "actual guest runtime differs from reviewed producer"
    );
    Ok(())
}

async fn replay_cases(
    session: &Session,
    inputs: &Inputs,
    data: &Path,
    profile: &Profile,
    report: &mut Value,
) -> Result<()> {
    let buffer = session
        .call_user_port(
            ShapeRef::parse(SHAPE)?,
            OpName::parse("runtime-info")?,
            &CanonValue::Obj(BTreeMap::new()),
        )
        .await?;
    report["guest_runtime"] = runtime(parse_output(&buffer)?)?;
    drop(buffer);
    require_runtime(&report["guest_runtime"])?;
    let initialized = provider_initialized(&audit_rows(data, profile)?, &inputs.pack)?;
    report["physical_provider_init_receipt"] = json!(initialized);
    ensure!(
        initialized,
        "actual Python provider initialization receipt missing"
    );
    let cases = inputs.oracle["cases"]
        .as_array()
        .context("frozen cases missing")?;
    ensure!(cases.len() == 33, "wrong frozen case count");
    for (status, count) in [("ok", 12), ("invalid", 12), ("excluded", 9)] {
        ensure!(
            cases
                .iter()
                .filter(|case| case["transport"]["status"] == status)
                .count()
                == count,
            "wrong frozen case classification"
        );
    }
    report["cases"] = Value::Array(
        cases
            .iter()
            .map(|case| {
                json!({
                    "id":case["id"], "classification":case["transport"]["status"],
                    "observed":false, "matched":false,
                    "profile_exclusion_not_parity":case["transport"]["status"] == "excluded"
                })
            })
            .collect(),
    );
    for (index, case) in cases.iter().enumerate() {
        let before = audit_rows(data, profile)?.len();
        // Only the outer WIT argument record is canonicalized. The original
        // input JSON is copied byte-for-byte into its opaque payload string.
        let args = canon(&json!({"operation":case["operation"], "payload":case["input_json"]}))?;
        ensure!(
            tobkiri_canon::encode(&args, Variant::StrictV1)?.len() <= tobkiri_canon::MAX_BYTES,
            "request outer envelope exceeds native bound"
        );
        let actual = session
            .call_user_port(ShapeRef::parse(SHAPE)?, OpName::parse("normalize")?, &args)
            .await;
        let record = &mut report["cases"][index];
        record["observed"] = json!(true);
        let expected = case["transport"]["status"]
            .as_str()
            .context("case status missing")?;
        let matched = match actual {
            Ok(buffer) => {
                if let CanonValue::Str(output) = parse_output(&buffer)? {
                    record["actual"] = json!({"status":"ok", "output_sha256":evidence::digest(output.as_bytes()), "output_bytes":output.len()});
                    expected == "ok"
                        && case["transport"]["output_json"].as_str() == Some(output.as_str())
                } else {
                    record["actual"] = json!({"status":"unexpected-output-type"});
                    false
                }
            }
            Err(CoreError::Step(step)) => {
                let public_invalid =
                    step.code == "invalid" && step.message == "provider reported invalid input";
                record["actual"] = json!({"status":"step-error", "code":step.code, "exact_public_invalid":public_invalid});
                matches!(expected, "invalid" | "excluded") && public_invalid
            }
            Err(_) => {
                record["actual"] = json!({"status":"unexpected-core-error"});
                false
            }
        };
        let rows = audit_rows(data, profile)?;
        let interval = rows.get(before..).context("audit sequence shrank")?;
        // PortCall authority belongs to the original User, not the provider.
        // Its frozen Profile binding selects the exact verified provider; the
        // separate Init receipt above authenticates that physical Pack/hash.
        let terminal = terminal_pair(
            interval,
            &json!({"kind":"user"}),
            &tobkiri_gate::EffectRequest::PortCall {
                shape: ShapeRef::parse(SHAPE)?,
                op: OpName::parse("normalize")?,
            },
            &tobkiri_canon::digest(&args, Variant::StrictV1)?,
        )?;
        record["exact_port_dispatch_outcome"] = json!(terminal);
        record["matched"] = json!(matched && terminal);
        record["admitted_parity"] = json!(matched && terminal && expected != "excluded");
        record["exclusion_enforced"] = json!(matched && terminal && expected == "excluded");
        ensure!(
            matched && terminal,
            "ordered native case {index} failed exact result/audit checks"
        );
    }
    Ok(())
}

async fn consumer_authority(root: &Path, inputs: &Inputs, report: &mut Value) -> Result<()> {
    let caller = consumer::materialize(root, &inputs.pack)?;
    for allowed in [true, false] {
        let label = if allowed { "allowed" } else { "denied" };
        let profile = profile(
            inputs,
            Some(&caller),
            allowed,
            &format!("prof_ordered_{label}"),
        )?;
        let data = root.join(label).join("data");
        let (_core, session) = start(&data, &profile, true).await?;
        let result: Result<()> = async {
            let before = audit_rows(&data, &profile)?.len();
            let actual = session
                .call_user_port(
                    ShapeRef::parse(consumer::ENTRY)?,
                    OpName::parse("run")?,
                    &CanonValue::Obj(BTreeMap::new()),
                )
                .await;
            let rows = audit_rows(&data, &profile)?;
            let interval = rows.get(before..).context("audit sequence shrank")?;
            let provider_dispatches = pack_events(interval, "dispatch", &inputs.pack);
            let caller_denials = interval
                .iter()
                .filter(|row| {
                    row["kind"] == "denied"
                        && row["principal"]["kind"] == "pack"
                        && row["principal"]["pack"] == consumer::CONSUMER
                        && row["principal"]["hash"] == caller.1.hash.to_string()
                        && row["payload"][0] == "port-call"
                        && row["payload"][1] == "denied"
                })
                .count();
            let bridge_effect = tobkiri_gate::EffectRequest::PortCall {
                shape: ShapeRef::parse(SHAPE)?,
                op: OpName::parse("runtime-info")?,
            }
            .digest()?
            .to_string();
            let bridge_dispatches = interval
                .iter()
                .filter(|row| {
                    row["kind"] == "dispatch" && row["payload"]["effect_digest"] == bridge_effect
                })
                .count();
            report["consumer_authority"][label] = json!({
                "observed":true, "returned_error":actual.is_err(),
                "provider_dispatches":provider_dispatches, "caller_denials":caller_denials,
                "bridge_port_dispatches":bridge_dispatches,
                "caller_pack_sha256":caller.1.hash.to_string()
            });
            if allowed {
                let buffer = actual?;
                report["consumer_authority"][label]["guest_runtime"] =
                    runtime(parse_output(&buffer)?)?;
                require_runtime(&report["consumer_authority"][label]["guest_runtime"])?;
                let effect = tobkiri_gate::EffectRequest::PortCall {
                    shape: ShapeRef::parse(SHAPE)?,
                    op: OpName::parse("runtime-info")?,
                };
                let terminal = terminal_pair(
                    interval,
                    &json!({"kind":"pack", "pack":caller.1.manifest.id, "hash":caller.1.hash}),
                    &effect,
                    &tobkiri_canon::digest(&CanonValue::Obj(BTreeMap::new()), Variant::StrictV1)?,
                )?;
                let initialized = provider_initialized(&rows, &inputs.pack)?;
                report["consumer_authority"][label]["exact_port_dispatch_outcome"] =
                    json!(terminal);
                report["consumer_authority"][label]["physical_provider_init_receipt"] =
                    json!(initialized);
                ensure!(
                    bridge_dispatches > 0 && terminal && initialized,
                    "typed caller did not dispatch to actual Python provider"
                );
            } else {
                ensure!(
                    actual.is_err()
                        && caller_denials > 0
                        && provider_dispatches == 0
                        && bridge_dispatches == 0,
                    "declared typed import bypassed effective port permission"
                );
            }
            Ok(())
        }
        .await;
        let close = session.close().await;
        report["consumer_authority"][label]["session_closed"] = json!(close.is_ok());
        result?;
        close?;
    }
    Ok(())
}

#[test]
fn ordered_python_reviewed_source_identity_is_current() -> Result<()> {
    evidence::reviewed_sources(&repo())?;
    Ok(())
}

#[test]
fn terminal_pair_rejects_wrong_principal_arguments_and_grant() -> Result<()> {
    let principal = json!({"kind":"user"});
    let effect = tobkiri_gate::EffectRequest::PortCall {
        shape: ShapeRef::parse(SHAPE)?,
        op: OpName::parse("normalize")?,
    };
    let args = tobkiri_canon::Digest::hash(b"test-only-metadata");
    let dispatch = json!({"kind":"dispatch", "principal":principal, "request":"request-one",
        "payload":{"grant":"grant-one", "effect_digest":effect.digest()?.to_string(), "args_digest":args.to_string()}});
    let mut outcome = dispatch.clone();
    outcome["kind"] = json!("outcome");
    let valid = vec![dispatch, outcome];
    assert!(terminal_pair(&valid, &principal, &effect, &args)?);
    for (field, replacement) in [
        (
            "principal",
            json!({"kind":"input", "source":"wrong-source"}),
        ),
        (
            "args_digest",
            json!(tobkiri_canon::Digest::hash(b"other").to_string()),
        ),
        ("grant", json!("grant-other")),
        (
            "effect_digest",
            json!(tobkiri_canon::Digest::hash(b"other-effect").to_string()),
        ),
    ] {
        let mut changed = valid.clone();
        if field == "principal" {
            changed[1][field] = replacement;
        } else {
            changed[1]["payload"][field] = replacement;
        }
        assert!(
            !terminal_pair(&changed, &principal, &effect, &args)?,
            "accepted mismatched {field}"
        );
    }
    assert!(!terminal_pair(&valid[..1], &principal, &effect, &args)?);
    let mut duplicate = valid.clone();
    duplicate.push(valid[0].clone());
    assert!(!terminal_pair(&duplicate, &principal, &effect, &args)?);
    Ok(())
}

#[tokio::test]
#[ignore = "requires the real pinned ordered Python component and external evidence destination"]
async fn ordered_python_bridge_replays_through_real_core() -> Result<()> {
    let destination = evidence::receipt_destination(&repo())?;
    let inputs = evidence::load(&repo())?;
    let temporary = physical_temp()?;
    let mut report = json!({
        "format":"tobkiri.ordered-python-native-core-replay/1",
        "identity":inputs.identity,
        "host_os":std::env::consts::OS, "host_arch":std::env::consts::ARCH,
        "original_reference_runtime":{"python":"3.13.5", "unicode":"15.1.0"},
        "frozen_case_counts":{"cases":33, "success":12, "original_invalid":12, "excluded_not_parity":9},
        "core_runtime_path_passed":false, "full_unit_complete":false,
        "owner_platform_acceptance":false, "production_enabled":false,
        "consumer_authority":{}, "cases":[]
    });
    let result: Result<()> = async {
        let profile = profile(&inputs, None, true, "prof_ordered_python")?;
        let data = temporary.path().join("replay/data");
        let (_core, session) = start(&data, &profile, false).await?;
        let replay = replay_cases(&session, &inputs, &data, &profile, &mut report).await;
        let close = session.close().await;
        report["replay_session_closed"] = json!(close.is_ok());
        replay?;
        close?;
        consumer_authority(temporary.path(), &inputs, &mut report).await?;
        Ok(())
    }
    .await;
    report["core_runtime_path_passed"] = json!(result.is_ok());
    let saved = evidence::write_receipt(&destination, &report);
    // A diagnostic write failure never converts an execution failure to success.
    result?;
    saved?;
    println!(
        "Ordered Python Core receipt: cases=33 success=12 original-invalid=12 excluded-not-parity=9 full-unit-complete=false"
    );
    Ok(())
}
