//! Separate transport-boundary evidence; this does not change corpus parity.
//!
//! Required TOBKIRI_TEST_RUST_PROVIDERS_{USAGE,STREAM}_{COMPONENT,SHA256},
//! {ORACLE,ORACLE_SHA256,ADMISSION_EVIDENCE_DIR}. Both implementations must be
//! Rust (their existing defaults). ADMISSION_EVIDENCE_DIR must be a NEW private
//! directory. The oracle is the unchanged original 94-case provider receipt.
//! Run only admission::rust_provider_outer_transport_admission_boundaries:
//! cargo test -p tobkiri-e2e --test rust_python_providers
//!   admission::rust_provider_outer_transport_admission_boundaries -- --ignored --exact
//!
//! Six synthetic vectors measure only the outer CoreStrictV1 boundary. Three
//! separately pinned original oversized inputs measure entrance rejection;
//! their original direct-source expectations are retained without being
//! rewritten or counted as matched guest outcomes. All original replay tests
//! and their mismatch failures remain independent.

use super::inputs::{self, Evidence, Oracle, Pack, ROLES, text};
use super::support::{physical_temp, repo};
use super::{Harness, arguments, audit_rows, profile, runtimes, start, terminal};
use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};
use std::{env, fs, path::Component, path::PathBuf, time::Duration};
use tobkiri_canon::{CanonError, Variant, ids::OpName, ids::ShapeRef};
use tobkiri_gate::{EffectRequest, WasmEntryKind};

const OUTER_MAX: usize = 4_194_304;
const OUTER_OVERHEAD: usize = 42;
const VERIFY_ERROR_PREFIX: &str = "User arguments could not be verified: ";
const EVIDENCE_SUFFIX: &str = "ADMISSION_EVIDENCE_DIR";
const SAMPLE: &str = r#"{"a":"b"}"#;

struct OriginalOversized {
    role: usize,
    provider: &'static str,
    id: &'static str,
    bytes: usize,
    sha256: &'static str,
}

// Input identities from the original independent source observation, not
// replacement expected values for the 94-case replay.
const ORIGINAL_OVERSIZED: [OriginalOversized; 3] = [
    OriginalOversized {
        role: 0,
        provider: "usage-cost",
        id: "codec-byte-limit",
        bytes: 4_194_327,
        sha256: "88cee76d270f8667a1a2d6094d5ba70dae1d6532a137cf3fe76c096d69fabcea",
    },
    OriginalOversized {
        role: 0,
        provider: "usage-cost",
        id: "encoded-output-byte-limit",
        bytes: 4_194_295,
        sha256: "2c6716243701a44bf3cf8b7c8f16e7a3c2e5935785d7ec4f73d56fff975153b9",
    },
    OriginalOversized {
        role: 1,
        provider: "stream-normalizer",
        id: "codec-byte-limit",
        bytes: 4_194_321,
        sha256: "bfff7ea161dde6469f1460858fb37431d966db2e3381e4b28709ec515538b65f",
    },
];

fn new_evidence() -> Result<Evidence> {
    let name = format!("{}_{EVIDENCE_SUFFIX}", inputs::PREFIX);
    let path = PathBuf::from(env::var_os(&name).with_context(|| format!("required {name}"))?);
    ensure!(
        path.is_absolute()
            && path
                .components()
                .all(|part| !matches!(part, Component::ParentDir)),
        "new absolute admission evidence directory required"
    );
    for ancestor in path.ancestors() {
        ensure!(!ancestor.is_symlink(), "evidence path contains a symlink");
    }
    let directory = path
        .parent()
        .context("evidence parent missing")?
        .canonicalize()?
        .join(
            path.file_name()
                .context("evidence directory name missing")?,
        );
    let mut builder = fs::DirBuilder::new();
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt as _;
        builder.mode(0o700);
    }
    builder
        .create(&directory)
        .context("admission evidence directory must be NEW; no overwrite")?;
    Ok(Evidence { directory })
}

fn source_bindings(evidence: &Evidence) -> Result<Vec<Value>> {
    let repo = repo();
    let mut bindings = Vec::new();
    for (index, path) in [
        "crates/tobkiri-e2e/tests/rust_python_providers/admission.rs",
        "crates/tobkiri-e2e/tests/rust_python_providers.rs",
        "crates/tobkiri-e2e/tests/rust_python_providers/inputs.rs",
        "crates/tobkiri-core/src/session.rs",
        "crates/tobkiri-core/src/execution_frames.rs",
        "crates/tobkiri-e2e/tests/support/mod.rs",
        "crates/tobkiri-canon/src/lib.rs",
        "crates/tobkiri-canon/src/codec.rs",
        "crates/tobkiri-canon/src/ownership.rs",
    ]
    .into_iter()
    .enumerate()
    {
        let bytes = inputs::read(&repo, path, 2 * 1024 * 1024)?;
        bindings.push(json!({
            "path": path,
            "bytes": bytes.len(),
            "sha256": inputs::digest(&bytes),
            "copy": evidence.save(&format!("source-{index:02}.txt"), "utf-8", &bytes)?,
        }));
    }
    Ok(bindings)
}

fn padded_payload(outer_bytes: usize) -> Result<String> {
    let size = outer_bytes
        .checked_sub(OUTER_OVERHEAD)
        .context("outer size cannot contain the fixture envelope")?;
    ensure!(size >= SAMPLE.len(), "payload cannot contain fixture JSON");
    let mut payload = String::with_capacity(size);
    payload.push_str(SAMPLE);
    payload.extend(std::iter::repeat_n(' ', size - SAMPLE.len()));
    Ok(payload)
}

fn independent_outer(operation: &str, payload: &str) -> Result<Vec<u8>> {
    ensure!(
        operation.len() == 9 && operation.is_ascii(),
        "operation must be nine ASCII bytes"
    );
    ensure!(
        payload.is_ascii()
            && payload.bytes().filter(|byte| *byte == b'"').count() == 4
            && !payload.as_bytes().contains(&b'\\')
            && !payload.bytes().any(|byte| byte < 0x20),
        "fixture payload must have four quotes and no other JSON escapes"
    );
    // Independent of the production canonical encoder. Both fixture keys are
    // ASCII and sorted; only the four inner quote bytes require escaping.
    let outer = serde_json::to_vec(&json!({"operation": operation, "payload": payload}))?;
    ensure!(
        outer.len() == payload.len() + OUTER_OVERHEAD,
        "independent outer byte equation changed"
    );
    Ok(outer)
}

async fn vector(
    harness: &Harness<'_>,
    role: usize,
    payload: &str,
    expected_outer: usize,
    stem: &str,
    original: Option<&Value>,
) -> Result<Value> {
    let operation = ROLES[role].function;
    let outer = independent_outer(operation, payload)?;
    ensure!(outer.len() == expected_outer, "outer byte count differs");
    let args = arguments(Some(operation), payload);
    let canonical_size_check = match tobkiri_canon::encode(&args, Variant::StrictV1) {
        Ok(bytes) => {
            ensure!(
                outer.len() <= OUTER_MAX && bytes == outer,
                "CoreStrictV1 envelope differs"
            );
            "exact-production-canonical-bytes"
        }
        Err(CanonError::Limit("4 MiB")) => {
            ensure!(outer.len() > OUTER_MAX, "canonical rejection below maximum");
            "production-canonical-4mib-rejection"
        }
        Err(error) => return Err(error).context("unexpected canonical sizing outcome"),
    };
    let input = harness
        .evidence
        .save(&format!("{stem}.payload"), "utf-8", payload.as_bytes())?;
    let outer_copy = harness.evidence.save(
        &format!("{stem}.outer.json"),
        "independently-encoded-outer-json",
        &outer,
    )?;
    let before = audit_rows(harness.data, harness.profile)?;
    let before_copy = harness
        .evidence
        .json(&format!("{stem}.before.audit.json"), &json!(before))?;
    let mut observed = harness.call(role, operation, &args, stem).await?;
    let after = audit_rows(harness.data, harness.profile)?;
    let unchanged = before == after;
    let after_copy = harness
        .evidence
        .json(&format!("{stem}.after.audit.json"), &json!(after))?;
    let above_maximum = expected_outer > OUTER_MAX;
    let exact_entrance_error = observed.record["status"] == "step-error"
        && observed.error.as_ref().is_some_and(|(code, message)| {
            code == "limit-exceeded" && message.starts_with(VERIFY_ERROR_PREFIX)
        });
    let matched = if above_maximum {
        exact_entrance_error
            && unchanged
            && observed.audit.is_empty()
            && !observed.gate_terminal
            && observed.record["argument_digest_available"] == false
    } else {
        observed.gate_terminal
            && observed.record["argument_digest_available"] == true
            && observed.record["status"] != "fixture-timeout"
    };
    observed.record["id"] = json!(stem);
    observed.record["core_call_attempted"] = json!(true);
    observed.record["payload"] = input;
    observed.record["independent_outer"] = outer_copy;
    observed.record["payload_bytes"] = json!(payload.len());
    observed.record["outer_bytes"] = json!(outer.len());
    observed.record["outer_maximum"] = json!(OUTER_MAX);
    observed.record["outer_overhead"] = json!(OUTER_OVERHEAD);
    observed.record["canonical_size_check"] = json!(canonical_size_check);
    observed.record["expected_transport_outcome"] = json!(if above_maximum {
        "reject-before-gate-dispatch"
    } else {
        "gate-dispatch-and-same-grant-terminal"
    });
    observed.record["exact_core_limit_code_and_verify_prefix"] = json!(exact_entrance_error);
    observed.record["audit_before"] = before_copy;
    observed.record["audit_after"] = after_copy;
    observed.record["audit_unchanged"] = json!(unchanged);
    observed.record["guest_final_success_observed"] = json!(observed.value.is_some());
    observed.record["guest_result_parity_claimed"] = json!(false);
    observed.record["matched_transport_boundary"] = json!(matched);
    observed.record["original_replay_mismatch_resolved"] = json!(false);
    if let Some(original) = original {
        observed.record["original_source_case"] = original.clone();
        observed.record["original_adapter_expectation_is_not_transport_expectation"] = json!(true);
    }
    if observed.gate_terminal {
        let shape = ShapeRef::parse(ROLES[role].shape)?;
        let effect = EffectRequest::PortCall {
            shape,
            op: OpName::parse(operation)?,
        };
        let effect_digest = effect.digest()?.to_string();
        let args_digest = tobkiri_canon::digest(&args, Variant::StrictV1)?.to_string();
        let dispatch = observed
            .audit
            .iter()
            .find(|row| {
                row["kind"] == "dispatch"
                    && row["principal"] == json!({"kind":"user"})
                    && row["payload"]["effect_digest"] == effect_digest
                    && row["payload"]["args_digest"] == args_digest
            })
            .context("same-grant dispatch missing")?;
        let outcome = observed
            .audit
            .iter()
            .find(|row| {
                row["kind"] == "outcome"
                    && row["principal"] == dispatch["principal"]
                    && row["request"] == dispatch["request"]
                    && row["payload"]["grant"] == dispatch["payload"]["grant"]
                    && row["payload"]["effect_digest"] == effect_digest
                    && row["payload"]["args_digest"] == args_digest
            })
            .context("same-grant outcome missing")?;
        observed.record["gate_receipt"] = json!({"dispatch":dispatch,"outcome":outcome});
    }
    Ok(observed.record)
}

async fn capture_vector(
    harness: &Harness<'_>,
    role: usize,
    payload: Result<String>,
    outer_bytes: usize,
    stem: &str,
    original: Option<&Value>,
) -> Result<Value> {
    let result = match payload {
        Ok(payload) => vector(harness, role, &payload, outer_bytes, stem, original).await,
        Err(error) => Err(error),
    };
    let record = match result {
        Ok(record) => record,
        Err(error) => json!({
            "id": stem,
            "role": ROLES[role].label,
            "operation": ROLES[role].function,
            "outer_bytes": outer_bytes,
            "core_call_attempted": false,
            "call_attempt_unconfirmed": true,
            "matched_transport_boundary": false,
            "fixture_failure": format!("{error:#}"),
            "original_source_case": original,
        }),
    };
    harness
        .evidence
        .json(&format!("{stem}.case.json"), &record)?;
    Ok(record)
}

fn original_payload(oracle: &Oracle, pin: &OriginalOversized) -> Result<(String, Value)> {
    let matching = oracle.document["cases"]
        .as_array()
        .context("provider cases missing")?
        .iter()
        .filter(|case| case["provider"] == pin.provider && case["id"] == pin.id)
        .collect::<Vec<_>>();
    ensure!(
        matching.len() == 1,
        "pinned original oversized case missing or duplicated"
    );
    let case = matching[0];
    ensure!(
        text(case, "operation")? == ROLES[pin.role].function,
        "original operation differs"
    );
    ensure!(
        case["input"]["bytes"].as_u64() == Some(pin.bytes as u64)
            && text(&case["input"], "sha256")? == pin.sha256,
        "original oversized input pin differs"
    );
    let input = inputs::checked(&oracle.directory, &case["input"])?;
    ensure!(
        input.len() == pin.bytes && inputs::digest(&input) == pin.sha256,
        "original input changed"
    );
    Ok((String::from_utf8(input)?, case.clone()))
}

async fn execute(evidence: &Evidence, report: &mut Value) -> Result<()> {
    ensure!(
        tobkiri_canon::MAX_BYTES == OUTER_MAX,
        "canonical outer maximum changed"
    );
    report["consumer_sources"] = json!(source_bindings(evidence)?);
    let repo = repo();
    let oracle = inputs::oracle(&repo, false)?;
    report["original_provider_oracle"] = oracle.identity.clone();
    let receipt_path = PathBuf::from(text(&oracle.identity, "path")?);
    let receipt = inputs::read(
        &oracle.directory,
        receipt_path
            .file_name()
            .and_then(|name| name.to_str())
            .context("receipt filename missing")?,
        OUTER_MAX,
    )?;
    ensure!(
        inputs::digest(&receipt) == text(&oracle.identity, "sha256")?,
        "receipt changed after pin verification"
    );
    report["original_provider_receipt"] =
        evidence.save("original-provider.receipt.json", "utf-8-json", &receipt)?;
    let temporary = physical_temp()?;
    let packs = (0..2)
        .map(|role| inputs::materialize(&repo, temporary.path(), role))
        .collect::<Result<Vec<Pack>>>()?;
    ensure!(
        packs.iter().all(|pack| pack.rust),
        "this boundary test requires two real Rust components"
    );
    report["artifacts"] = json!(packs.iter().map(|pack| &pack.identity).collect::<Vec<_>>());
    let profile = profile(&packs, false)?;
    report["profile"] = evidence.json("profile.json", &serde_json::to_value(&profile)?)?;
    let data = temporary.path().join("data");
    let (_core, session, host) = match start(&data, &profile, &packs, false).await {
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
        let mut boundaries = Vec::with_capacity(6);
        for role in 0..2 {
            for (label, outer_bytes) in [("below", OUTER_MAX - 1), ("at", OUTER_MAX), ("above", OUTER_MAX + 1)] {
                let stem = format!("boundary-{role}-{label}");
                boundaries.push(capture_vector(&harness, role, padded_payload(outer_bytes), outer_bytes, &stem, None).await?);
                report["boundary_cases"] = json!(boundaries);
            }
        }
        let mut originals = Vec::with_capacity(3);
        for (index, pin) in ORIGINAL_OVERSIZED.iter().enumerate() {
            let original = original_payload(&oracle, pin);
            let (payload, case) = match original {
                Ok((payload, case)) => (Ok(payload), Some(case)),
                Err(error) => (Err(error), None),
            };
            let stem = format!("original-oversized-{index}");
            originals.push(capture_vector(&harness, pin.role, payload, pin.bytes + OUTER_OVERHEAD, &stem, case.as_ref()).await?);
            report["original_oversized_cases"] = json!(originals);
        }
        let rows = audit_rows(&data, &profile)?;
        let mut initialization = Vec::new();
        for pack in &packs {
            let effect = EffectRequest::WasmEntry {
                pack: pack.verified.manifest.id.clone(), hash: pack.verified.hash.clone(), entry: WasmEntryKind::Init,
            };
            initialization.push(json!({
                "role":ROLES[pack.role].label,
                "gate_init_dispatch_and_terminal":terminal(&rows, &json!({"kind":"pack","pack":pack.verified.manifest.id,"hash":pack.verified.hash}), &effect, &effect.digest()?)?,
            }));
        }
        report["physical_initialization"] = json!(initialization);
        report["audit"] = evidence.json("session.audit.json", &json!(rows))?;
        report["reviewed_pack_permission_approvals"] = json!(*host.approvals.lock().unwrap());
        Ok::<(), anyhow::Error>(())
    }.await;
    let close = tokio::time::timeout(Duration::from_secs(30), session.close()).await;
    report["session_close_awaited"] = json!(true);
    report["session_close_succeeded"] = json!(matches!(close, Ok(Ok(()))));
    if report["session_close_succeeded"] != true {
        report["retained_failed_fixture_directory"] = json!(temporary.keep());
    }
    result?;
    ensure!(
        report["session_close_succeeded"] == true,
        "Session::close did not succeed"
    );
    Ok(())
}

#[tokio::test]
#[ignore = "requires two hash-pinned real Rust provider components, original provider receipt and NEW ADMISSION_EVIDENCE_DIR"]
async fn rust_provider_outer_transport_admission_boundaries() -> Result<()> {
    let evidence = new_evidence()?;
    let mut report = json!({
        "schema":"io.tobkiri.rust-provider-core-transport-admission.v1",
        "host_os":std::env::consts::OS,"host_arch":std::env::consts::ARCH,
        "scope":"actual two-Rust-Pack Core/Gate/User transport boundary with original scripted fixture approval",
        "outer_maximum":OUTER_MAX,"outer_size_equation":"payload_utf8_bytes + 42",
        "limits_changed":false,"original_replay_expectations_changed":false,
        "original_replay_mismatches_resolved":false,"guest_result_parity_claimed":false,
        "small_negative_permission_acceptance_claimed":false,"full_gateway_acceptance_claimed":false,
        "source_build_provenance_verified":false,"guest_lifecycle_shutdown_claimed":false,
        "boundary_cases":[],"original_oversized_cases":[],"runtimes":[],
        "all_cases_attempted":false,"passed":false,
    });
    let result = execute(&evidence, &mut report).await;
    if let Err(error) = &result {
        report["fixture_failure"] = json!(format!("{error:#}"));
    }
    let boundaries = report["boundary_cases"]
        .as_array()
        .context("boundary records missing")?;
    let originals = report["original_oversized_cases"]
        .as_array()
        .context("original records missing")?;
    let counts = (boundaries.len(), originals.len());
    let all_attempted = counts == (6, 3)
        && boundaries
            .iter()
            .chain(originals)
            .all(|case| case["core_call_attempted"] == true);
    let mismatch_count = boundaries
        .iter()
        .chain(originals)
        .filter(|case| case["matched_transport_boundary"] != true)
        .count();
    let runtimes = report["runtimes"].as_array().is_some_and(|rows| {
        rows.len() == 2
            && rows
                .iter()
                .all(|row| row["matched"] == true && row["truthful_actual_runtime"] == true)
    });
    let init = report["physical_initialization"]
        .as_array()
        .is_some_and(|rows| {
            rows.len() == 2
                && rows
                    .iter()
                    .all(|row| row["gate_init_dispatch_and_terminal"] == true)
        });
    let passed = result.is_ok() && all_attempted && mismatch_count == 0 && runtimes && init;
    report["case_counts"] =
        json!({"synthetic_boundary":counts.0,"pinned_original_oversized":counts.1});
    report["all_cases_attempted"] = json!(all_attempted);
    report["transport_mismatch_count"] = json!(mismatch_count);
    report["passed"] = json!(passed);
    evidence.json("summary.json", &report)?;
    ensure!(
        passed,
        "transport admission boundary incomplete or mismatched; inspect private summary/sidecars"
    );
    Ok(())
}
