//! Actual original Setup Wasm through ordinary Core/Gate/Profile/Session.
//! Explicit hash-bound CI inputs required; fixture approvals are not GUI proof.
#[path = "harness_setup/inputs.rs"]
mod inputs;
mod support;

use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};
use std::{collections::BTreeMap, fs, path::Path, sync::Arc, time::Duration};
use support::{Host, canon, parse_output, physical_temp, repo};
use tobkiri_canon::{
    CanonValue, Digest, Variant,
    ids::{OpName, ShapeRef},
};
use tobkiri_core::{Core, CoreConfig, CoreError, Session};
use tobkiri_gate::{ApprovalStore, EffectRequest, Outcome, PermissionSet};
use tobkiri_profile::{PackPin, Profile};
use tobkiri_store::{Approvals, DataRoot};

fn profile(pin: PackPin) -> Result<Profile> {
    let mut ceiling = inputs::limits();
    ceiling.ports.insert(inputs::SHAPE.to_owned());
    let user = PermissionSet {
        ports: ceiling.ports.clone(),
        wasm: ceiling.wasm.clone(),
        loops: ceiling.loops.clone(),
        ..Default::default()
    };
    Ok(serde_json::from_value(
        json!({"format":"tobkiri.profile/1",
        "id":"prof_harness_setup_core","name":"Original Setup native Core fixture",
        "packs":[pin],"publishers":{},"ceiling":ceiling,
        "pack_limits":{(inputs::PACK):inputs::limits()},"user_permissions":user,
        "bindings":{"local.harness-setup-python.setup@1":inputs::PACK},
        "overlay_order":[],"user_overlays":[],"startup":{"inputs":[],"ui":null}}),
    )?)
}

fn authority(
    root: &DataRoot,
    profile: &Profile,
    pack: &tobkiri_manifest::VerifiedPack,
    plan: &Digest,
) -> Result<Value> {
    let (head, epoch) = root.profile_head_snapshot(&profile.id)?;
    let approvals = Approvals::open(root, &profile.id)?;
    let document =
        tobkiri_canon::digest(&canon(&serde_json::to_value(profile)?)?, Variant::StrictV1)?;
    let pack_policy = approvals.permissions(&profile.id, &pack.manifest.id, &pack.hash)?;
    let user_policy = approvals.user_permissions(&profile.id, &document, plan, epoch)?;
    ensure!(
        pack_policy.as_ref() == Some(&pack.manifest.permissions)
            && user_policy == profile.user_permissions,
        "fixture policies unavailable or changed"
    );
    Ok(
        json!({"head":head,"epoch":epoch,"pack_permissions":pack_policy,
        "user_permissions":user_policy,"profiles":root.list_profiles()?}),
    )
}

fn audit(data: &Path, profile: &Profile) -> Result<Vec<Value>> {
    // Trusted native read-only inspection; no guest/User audit-read grant.
    let relative = DataRoot::profile_relative(&profile.id, "audit.sqlite")?;
    let connection = rusqlite::Connection::open_with_flags(
        data.join(relative),
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    )?;
    let mut statement = connection.prepare("SELECT body FROM audit ORDER BY seq LIMIT 4097")?;
    let mut rows = statement.query([])?;
    let mut events = Vec::new();
    let profile_id = serde_json::to_value(&profile.id)?;
    while let Some(row) = rows.next()? {
        ensure!(events.len() < 4096, "fixture audit bound exceeded");
        let bytes: Vec<u8> = row.get(0)?;
        let event: Value = serde_json::from_slice(&bytes)?;
        ensure!(
            event["profile"] == profile_id,
            "audit event belongs to another Profile"
        );
        events.push(event);
    }
    Ok(events)
}

fn terminal(
    rows: &[Value],
    effect: &EffectRequest,
    arguments: &CanonValue,
    outcome: Outcome,
) -> Result<bool> {
    let effect = effect.digest()?.to_string();
    let args = tobkiri_canon::digest(arguments, Variant::StrictV1)?.to_string();
    let principal = json!({"kind":"user"});
    let dispatches = rows
        .iter()
        .filter(|row| {
            row["kind"] == "dispatch"
                && row["principal"] == principal
                && row["payload"]["effect_digest"] == effect
                && row["payload"]["args_digest"] == args
        })
        .collect::<Vec<_>>();
    let [dispatch] = dispatches.as_slice() else {
        return Ok(false);
    };
    let outcome = serde_json::to_value(outcome)?;
    let terminals = rows
        .iter()
        .filter(|row| {
            row["kind"] == "outcome"
                && row["principal"] == principal
                && row["request"] == dispatch["request"]
                && row["payload"]["grant"] == dispatch["payload"]["grant"]
                && row["payload"]["effect_digest"] == effect
                && row["payload"]["args_digest"] == args
        })
        .collect::<Vec<_>>();
    let [terminal] = terminals.as_slice() else {
        return Ok(false);
    };
    Ok(dispatch["payload"]["grant"].is_string() && terminal["payload"]["outcome"] == outcome)
}

#[test]
fn terminal_metadata_comparison_rejects_missing_duplicate_or_conflicting_outcomes() -> Result<()> {
    // Synthetic metadata tests only the evidence comparator; it neither mints
    // a grant nor claims actual guest/Gate execution.
    let effect = EffectRequest::PortCall {
        shape: ShapeRef::parse(inputs::SHAPE)?,
        op: OpName::parse("decision")?,
    };
    let args = CanonValue::Obj(BTreeMap::new());
    let effect_digest = effect.digest()?.to_string();
    let args_digest = tobkiri_canon::digest(&args, Variant::StrictV1)?.to_string();
    let dispatch = json!({"kind":"dispatch","principal":{"kind":"user"},"request":"fixture-request",
        "payload":{"grant":"fixture-grant","effect_digest":effect_digest,"args_digest":args_digest}});
    let mut outcome = dispatch.clone();
    outcome["kind"] = json!("outcome");
    outcome["payload"]["outcome"] = serde_json::to_value(Outcome::Succeeded)?;
    ensure!(
        terminal(
            &[dispatch.clone(), outcome.clone()],
            &effect,
            &args,
            Outcome::Succeeded
        )?,
        "valid terminal rejected"
    );
    ensure!(
        !terminal(
            std::slice::from_ref(&dispatch),
            &effect,
            &args,
            Outcome::Succeeded
        )?,
        "missing terminal accepted"
    );
    ensure!(
        !terminal(
            &[dispatch.clone(), outcome.clone(), outcome.clone()],
            &effect,
            &args,
            Outcome::Succeeded
        )?,
        "duplicate terminal accepted"
    );
    let mut conflicting = outcome.clone();
    conflicting["payload"]["outcome"] = serde_json::to_value(Outcome::Failed {
        code: "invalid".into(),
    })?;
    ensure!(
        !terminal(
            &[dispatch, outcome, conflicting],
            &effect,
            &args,
            Outcome::Succeeded
        )?,
        "conflicting terminal accepted"
    );
    Ok(())
}

async fn replay(
    session: &Session,
    data: &Path,
    profile: &Profile,
    cases: &[Value],
) -> Result<Vec<Value>> {
    let mut observations = Vec::new();
    for case in cases {
        let operation = case["operation"]
            .as_str()
            .context("source operation missing")?;
        ensure!(
            inputs::OPERATIONS.contains(&operation),
            "unknown source operation"
        );
        let payload = case["input"].as_str().context("source input missing")?;
        let args = CanonValue::Obj(BTreeMap::from([(
            "payload".into(),
            CanonValue::Str(payload.into()),
        )]));
        let shape = ShapeRef::parse(inputs::SHAPE)?;
        let op = OpName::parse(operation)?;
        let effect = EffectRequest::PortCall {
            shape: shape.clone(),
            op: op.clone(),
        };
        let before = audit(data, profile)?.len();
        let mut call = std::pin::pin!(session.call_user_port(shape, op, &args));
        let mut record = json!({"id":case["id"],"operation":operation,"passed":false});
        let (matched, outcome) = match tokio::time::timeout(Duration::from_secs(30), &mut call)
            .await
        {
            Ok(Ok(buffer)) => {
                record["returned"] = json!(true);
                let value = parse_output(&buffer)?;
                (
                    matches!(&value,CanonValue::Str(raw) if case["expected"].as_str()==Some(raw.as_str())),
                    Outcome::Succeeded,
                )
            }
            Ok(Err(CoreError::Step(error))) => {
                record["returned"] = json!(true);
                record["error_code"] = json!(error.code);
                // Core deliberately exposes its fixed public message, not the
                // private guest message checked by standalone execution.
                (
                    case["expected_error"]["code"] == error.code
                        && error.code == "invalid"
                        && error.message == "provider reported invalid input",
                    Outcome::Failed { code: error.code },
                )
            }
            Ok(Err(_)) => {
                record["status"] = json!("unexpected-core-error");
                observations.push(record);
                break;
            }
            Err(_) => {
                // Preserve the actual pending call while closing this same
                // Session. Never retry an uncertain operation on a new one.
                let close = tokio::time::timeout(Duration::from_secs(5), session.close()).await;
                record["status"] = json!("fixture-timeout");
                record["same_session_close_succeeded"] = json!(matches!(close, Ok(Ok(()))));
                observations.push(record);
                break;
            }
        };
        let rows = audit(data, profile)?;
        let gate = terminal(
            rows.get(before..).context("audit sequence shrank")?,
            &effect,
            &args,
            outcome,
        )?;
        record["gate_dispatch_and_terminal"] = json!(gate);
        record["matched"] = json!(matched);
        record["passed"] = json!(matched && gate);
        observations.push(record);
    }
    Ok(observations)
}

#[tokio::test]
#[ignore = "requires exact real Python Setup component, source oracle and native owner platform"]
async fn original_setup_runs_through_core_without_changing_profile_authority() -> Result<()> {
    let inputs = inputs::load(&repo())?;
    let temporary = physical_temp()?;
    let (pin, pack) = inputs::materialize(&repo(), temporary.path(), inputs.component)?;
    let profile = profile(pin)?;
    let compiled = tobkiri_profile::compile(&profile, std::slice::from_ref(&pack))
        .map_err(|_| anyhow::anyhow!("fixture profile conflicts"))?;
    let data = temporary.path().join("data");
    let host = Arc::new(Host::default());
    for operation in inputs::OPERATIONS {
        host.utility_ports
            .lock()
            .unwrap()
            .insert((inputs::SHAPE.into(), operation.into()));
    }
    host.approve_literal_user(&profile)?;
    let core = Core::open(CoreConfig::for_test_data_root(data.clone()), host.clone())?;
    core.save_profile(profile.clone(), None).await?;
    let root = DataRoot::open(&data)?;
    let before = authority(&root, &profile, &pack, &compiled.plan_digest)?;
    let before_reviews = host.approvals.lock().unwrap().clone();
    let session = core.start(&profile.id).await?;
    let attempt = replay(&session, &data, &profile, &inputs.cases).await;
    // Cleanup is attempted even if source parsing, audit inspection or any
    // actual call failed. A later successful close never erases that failure.
    let closed = matches!(
        tokio::time::timeout(Duration::from_secs(30), session.close()).await,
        Ok(Ok(()))
    );
    let after = authority(&root, &profile, &pack, &compiled.plan_digest);
    let unchanged = after.as_ref().is_ok_and(|value| value == &before)
        && *host.approvals.lock().unwrap() == before_reviews;
    let complete = attempt.as_ref().is_ok_and(|cases| {
        cases.len() == inputs::IDS.len()
            && cases
                .iter()
                .zip(inputs::IDS)
                .all(|(case, id)| case["id"] == id && case["passed"] == true)
    });
    let final_audit = audit(&data, &profile);
    let passed = complete && closed && unchanged && final_audit.is_ok();
    let audit_hash = if let Ok(rows) = &final_audit {
        let bytes = serde_json::to_vec_pretty(rows)?;
        fs::write(inputs.evidence.join("audit.json"), &bytes)?;
        Some(Digest::hash(&bytes))
    } else {
        None
    };
    let report = json!({"format":"tobkiri.harness-setup-core-replay/1","passed":passed,
        "source":inputs.identity,"cases":attempt.as_ref().ok(),"case_runner_failed":attempt.is_err(),
        "same_session_closed":closed,"authority_unchanged":unchanged,"authority_before":before,
        "audit_sidecar_sha256":audit_hash,"audit_read_failed":final_audit.is_err(),
        "authority_after":after.as_ref().ok(),"expected_cases":inputs::IDS.len(),
        "native_core_replay":true,"bootstrap_profile_activation":false,
        "native_approval_gui_tested":false,"full_setup_gui_tested":false,"production_enabled":false});
    fs::write(
        inputs.evidence.join("core-replay.json"),
        serde_json::to_vec_pretty(&report)?,
    )?;
    ensure!(
        passed,
        "native Setup comparison, Gate terminal, authority or Session cleanup failed"
    );
    Ok(())
}
