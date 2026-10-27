//! Actual synchronous CPython stack + sealed native IO prerequisite on macOS.
//! This test never approves a Profile or proves the complete Setup/GUI path.
use super::*;
use anyhow::ensure;
use serde_json::{Value, json};
use std::{
    fs::{File, OpenOptions},
    future::Future,
    io::{Read, Write},
    os::unix::fs::{DirBuilderExt, OpenOptionsExt},
    path::{Path, PathBuf},
    sync::Arc,
    task::{Context as TaskContext, Poll, Waker},
};
use tobkiri_canon::{CoreValue, Digest};
use tobkiri_gate::{CapacityLedger, ControlKind, CoreControl};

const INTERFACE: &str = "tobkiri-ui:native-input-stack/probe@1.0.0";
const SIZE: usize = 2 * 65536 + 17;

#[path = "native_input_stack_state_probe.rs"]
mod state_probe;

fn stack_bindings() -> [NativeDataBinding<'static>; 2] {
    [
        NativeDataBinding {
            export: NativeDataExport::new(Some(INTERFACE), "init"),
            result: Some(NativeDataResultContract {
                success: NativeDataSuccessType::Unit,
                error: NativeDataErrorContract {
                    code_field: "code",
                    message_field: "message",
                    details_field: Some("details"),
                    cases: &["invalid-input", "already-initialized"],
                },
            }),
        },
        NativeDataBinding {
            export: NativeDataExport::new(Some(INTERFACE), "run"),
            result: None,
        },
    ]
}

fn expect_init_error(
    reply: std::result::Result<NativeDataValue, crate::NativeDataError>,
    code: &str,
    message: &str,
) -> Result<()> {
    let Err(error) = reply else {
        bail!("initialization unexpectedly succeeded");
    };
    ensure!(
        error.code().as_str()? == code
            && error.message().as_str()? == message
            && error
                .details()
                .is_some_and(|value| value.as_bytes().is_empty()),
        "original typed init refusal differs"
    );
    Ok(())
}

fn checked_file(label: &str, bound: u64) -> Result<(PathBuf, Vec<u8>)> {
    let path = PathBuf::from(
        std::env::var_os(format!("TOBKIRI_TEST_INPUT_STACK_{label}"))
            .context("explicit stack-probe input required")?,
    );
    ensure!(
        path.is_absolute() && std::fs::canonicalize(&path)? == path,
        "physical stack-probe input required"
    );
    let before = std::fs::symlink_metadata(&path)?;
    ensure!(
        before.is_file() && before.len() <= bound,
        "stack-probe input exceeds bound"
    );
    let mut bytes = Vec::new();
    File::open(&path)?.take(bound + 1).read_to_end(&mut bytes)?;
    ensure!(
        bytes.len() as u64 == before.len(),
        "stack-probe input changed"
    );
    let expected = std::env::var(format!("TOBKIRI_TEST_INPUT_STACK_{label}_SHA256"))?;
    ensure!(
        Digest::hash(&bytes).as_str() == format!("sha256:{expected}"),
        "stack-probe input identity differs"
    );
    Ok((path, bytes))
}

fn create_output(path: &Path) -> Result<File> {
    Ok(OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)?)
}

fn original_fixture() -> Vec<u8> {
    let mut bytes: Vec<_> = (0..SIZE).map(|index| (index % 251) as u8).collect();
    bytes[..6].copy_from_slice(b"\x7fELF\x02\x01");
    bytes[18..20].copy_from_slice(b"\x3e\x00");
    bytes
}

fn matches_json(actual: &CoreValue, expected: &Value) -> bool {
    match expected {
        Value::Null => matches!(actual.inspect(), tobkiri_canon::CoreRef::Null),
        Value::Bool(value) => actual.as_bool() == Some(*value),
        Value::Number(value) => actual.as_i64() == value.as_i64(),
        Value::String(value) => actual.as_str() == Some(value.as_str()),
        Value::Array(values) => actual.array().is_some_and(|array| {
            array.len() == values.len()
                && array.iter().zip(values).all(|(a, b)| matches_json(&a, b))
        }),
        Value::Object(values) => actual.object().is_some_and(|object| {
            object.len() == values.len()
                && values
                    .iter()
                    .all(|(key, value)| object.get(key).is_some_and(|a| matches_json(&a, value)))
        }),
    }
}

#[tokio::test(flavor = "current_thread")]
#[ignore = "requires exact CI-built Python component and independent original oracle"]
async fn synchronous_python_stack_uses_actual_native_input_and_original_artifact_checks()
-> Result<()> {
    ensure!(
        std::env::var("GITHUB_ACTIONS").as_deref() == Ok("true")
            && std::env::var("TOBKIRI_TEST_INPUT_STACK_CI").as_deref() == Ok("1"),
        "external native stack observation requires its explicit CI harness"
    );
    ensure!(
        std::env::var_os("TOBKIRI_TEST_INPUT_STACK_IMPORT_DIAGNOSTICS").is_none(),
        "baseline stack proof must not admit diagnostic capabilities"
    );
    execute_stack_probe(false).await
}

#[tokio::test(flavor = "current_thread")]
#[ignore = "separate import diagnostic; every outcome must remain failure"]
async fn import_audit_diagnostic_preserves_the_original_python_stack_failure() -> Result<()> {
    ensure!(
        std::env::var("GITHUB_ACTIONS").as_deref() == Ok("true")
            && std::env::var("TOBKIRI_TEST_INPUT_STACK_CI").as_deref() == Ok("1")
            && std::env::var("TOBKIRI_TEST_INPUT_STACK_IMPORT_DIAGNOSTICS").as_deref() == Ok("1"),
        "diagnostic requires its separate exact CI harness"
    );
    execute_stack_probe(true).await?;
    bail!("diagnostic-only execution cannot produce an acceptance pass")
}

fn stack_inputs(diagnostic: bool) -> Result<(Vec<u8>, Value)> {
    let (_, bytes) = checked_file("COMPONENT", 32 * 1024 * 1024)?;
    let (_, build) = checked_file("RECEIPT", 256 * 1024)?;
    let (_, reference) = checked_file("ORACLE", 1024 * 1024)?;
    let build: Value = serde_json::from_slice(&build)?;
    let oracle: Value = serde_json::from_slice(&reference)?;
    let component_hash = Digest::hash(&bytes);
    let expected_world = if diagnostic {
        "native-input-stack-import-diagnostics"
    } else {
        "native-input-stack"
    };
    let expected_imports = if diagnostic {
        json!([import_trace::INTERFACE, "tobkiri-ui:inputs/captured@1.0.0"])
    } else {
        json!(["tobkiri-ui:inputs/captured@1.0.0"])
    };
    ensure!(
        build["format"] == "tobkiri.component-build/2"
            && build["artifact_sha256"] == component_hash.hex()
            && build["world"] == expected_world
            && build["imports"] == expected_imports
            && build["exports"] == json!([INTERFACE])
            && build["production_enabled"] == false,
        "built stack component provenance differs"
    );
    ensure!(
        oracle["format"] == "tobkiri.native-input-stack-oracle/1"
            && oracle["native_original_and_captured_match"] == true
            && oracle["candidate_logic_used"] == false
            && oracle["wasm_execution_performed"] == false
            && oracle["actual_os_owner_proof"] == false
            && oracle["whole_setup_compatible"] == false
            && oracle["production_enabled"] == false,
        "original reference scope differs"
    );
    for (name, expected) in oracle["original_sources"]
        .as_object()
        .context("original source pins")?
    {
        let suffix = format!("/tobkiri_protocol/{name}");
        let entries: Vec<_> = build["source_manifest"]
            .as_array()
            .context("source manifest")?
            .iter()
            .filter(|item| {
                item["source"]
                    .as_str()
                    .is_some_and(|p| p.ends_with(&suffix))
            })
            .collect();
        ensure!(
            entries.len() == 1 && &entries[0]["sha256"] == expected,
            "component does not contain the exact original reference source"
        );
    }
    Ok((bytes, oracle))
}

async fn execute_stack_probe(diagnostic: bool) -> Result<()> {
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let _census = crate::child_census::lock_census_fixture().await;
    let (bytes, oracle) = stack_inputs(diagnostic)?;
    let component_hash = Digest::hash(&bytes);
    let raw = original_fixture();
    ensure!(
        oracle["input_bytes"] == SIZE && oracle["input_sha256"] == Digest::hash(&raw).as_str(),
        "independent original input differs"
    );
    let scenarios = oracle["scenarios"]
        .as_array()
        .context("reference scenarios")?;
    ensure!(
        scenarios.len() == 2
            && scenarios[0]["name"] == "success"
            && scenarios[1]["name"] == "wrong-digest",
        "original scenario set differs"
    );
    let evidence = PathBuf::from(
        std::env::var_os("TOBKIRI_TEST_INPUT_STACK_EVIDENCE").context("private evidence path")?,
    );
    ensure!(
        evidence.is_absolute()
            && !evidence.exists()
            && std::fs::canonicalize(evidence.parent().context("evidence parent")?)?
                == evidence.parent().unwrap(),
        "fresh physical evidence path required"
    );
    std::fs::DirBuilder::new().mode(0o700).create(&evidence)?;
    let fixture = tempfile::tempdir()?;
    let control = CoreControl::default();
    let root = tobkiri_store::DataRoot::open(fixture.path().join("data"))?;
    let owner = root.native_setup_owner_lease(&control)?;
    let account =
        CapacityLedger::new().native_setup_account(&control, || Ok(Arc::new(owner.clone())))?;
    let engine = crate::WasmEngine::new_with_control(&control)?;
    let component = Component::new(engine.engine(), &bytes)?;
    drop(bytes);
    let mut receipts = Vec::new();
    for (index, scenario) in scenarios
        .iter()
        .chain(std::iter::once(&scenarios[0]))
        .take(if diagnostic { 1 } else { 3 })
        .enumerate()
    {
        let cancelled = index == 2;
        std::fs::write(fixture.path().join("input"), &raw)?;
        let invocation = account.begin()?;
        let deadline = invocation.deadline()?;
        let started = Instant::now();
        let initial = engine.native_setup_store(invocation, ())?;
        let owners = initial.allocation_owners().clone();
        let mut store = Some(initial);
        let io = store.as_mut().unwrap().bind_native_data_io()?;
        let trace = if diagnostic {
            Some(store.as_mut().unwrap().enable_import_trace()?)
        } else {
            None
        };
        let mut snapshot = None;
        let mut config = None;
        let mut program = None;
        let mut baseline = control.usage().count(ControlKind::Task);
        let mut admitted = false;
        let mut initialized = false;
        let mut observation = NativeDataObservation::default();
        let mut fuel = None;
        let operation: Result<()> = async {
            let source_hash = Digest::hash(&raw);
            snapshot = Some(
                io.capture_file_snapshot(
                    &owner,
                    tobkiri_store::NativeSnapshotSource {
                        root: fixture.path(),
                        relative: "input",
                        length: raw.len() as u64,
                        sha256: &source_hash,
                    },
                )
                .await?,
            );
            drop(
                store
                    .as_mut()
                    .unwrap()
                    .seal_captured_inputs(&[crate::NativeCapturedFile {
                        path: "artifact",
                        snapshot: snapshot.as_ref().unwrap(),
                    }])?,
            );
            std::fs::write(
                fixture.path().join("input"),
                b"changed after actual APFS snapshot",
            )?;
            config = Some(CoreBuffer::copy_from(
                scenario["init"].as_str().context("init frame")?.as_bytes(),
                owners.graph(),
            )?);
            program = Some(
                store
                    .take()
                    .unwrap()
                    .prepare_data_program(&component, &stack_bindings())
                    .await?,
            );
            let program = program.as_mut().unwrap();
            if !diagnostic {
                let invalid = CoreBuffer::copy_from(b"not-json", owners.graph())?;
                expect_init_error(
                    program.invoke(0, Some(&invalid)).await?,
                    "invalid-input",
                    "invalid native input probe frame",
                )?;
                ensure!(
                    program.observation().post_return && !program.observation().store_retired,
                    "invalid init retired the original guest"
                );
            }
            let result = program.invoke(0, config.as_ref()).await?;
            ensure!(
                matches!(result, Ok(NativeDataValue::Unit)),
                "explicit initialization failed: {result:?}"
            );
            initialized = true;
            if !diagnostic {
                let other = &scenarios[(index + 1) % 2];
                let replacement = CoreBuffer::copy_from(
                    other["init"]
                        .as_str()
                        .context("replacement frame")?
                        .as_bytes(),
                    owners.graph(),
                )?;
                expect_init_error(
                    program.invoke(0, Some(&replacement)).await?,
                    "already-initialized",
                    "native input probe is already initialized",
                )?;
                ensure!(
                    program.observation().post_return && !program.observation().store_retired,
                    "repeated init retired the original guest"
                );
            }
            baseline = control.usage().count(ControlKind::Task);
            let mut call = Box::pin(program.invoke(1, None));
            // Poll without yielding this current-thread executor until the real
            // native worker+monitor are admitted; fuel yields are not IO proof.
            loop {
                ensure!(
                    Instant::now() < deadline,
                    "original stack-probe deadline expired before native IO"
                );
                match call
                    .as_mut()
                    .poll(&mut TaskContext::from_waker(Waker::noop()))
                {
                    Poll::Ready(result) => {
                        bail!("Python call completed without an admitted native wait: {result:?}")
                    }
                    Poll::Pending => {}
                }
                if control.usage().count(ControlKind::Task) >= baseline + 2 {
                    admitted = true;
                    break;
                }
            }
            ensure!(
                control.usage().count(ControlKind::Queue) >= 1,
                "native wait did not retain original queue custody"
            );
            if cancelled {
                drop(call);
                ensure!(
                    program.observation().store_retired,
                    "dropped Python/native call kept its Store alive"
                );
                ensure!(
                    program.invoke(1, None).await.is_err(),
                    "retired Python stack was reused"
                );
            } else {
                let result = call.await?;
                let output = match result {
                    Ok(NativeDataValue::String(output)) => output,
                    _ => bail!("stack probe did not return its owned observation"),
                };
                let parsed = CoreValue::parse_guest(output.as_bytes(), owners.graph())?;
                validate_observation(&parsed, scenario)?;
                create_output(&evidence.join(format!("case-{index}.json")))?
                    .write_all(output.as_bytes())?;
                drop(parsed);
                ensure!(
                    program.observation().post_return,
                    "Python call did not complete actual canonical post-return"
                );
                drop(output);
            }
            fuel = program.fuel_remaining().ok();
            if !cancelled {
                ensure!(
                    fuel.is_some_and(|remaining| remaining > 0 && remaining < super::super::FUEL),
                    "actual Python computation did not retain its original fuel accounting"
                );
            }
            ensure!(
                program.original_deadline() == deadline,
                "native/Python call renewed its deadline"
            );
            Ok(())
        }
        .await;
        // Cleanup and funded diagnostic retention run on EVERY error path.
        let deadline_preserved = program
            .as_ref()
            .is_none_or(|program| program.original_deadline() == deadline);
        if let Some(program) = program.as_mut() {
            observation = program.observation();
            program.retire();
        }
        drop(program);
        if let Some(store) = store.as_ref() {
            store.retire();
        }
        drop(store);
        let cleanup = io.retire_and_wait().await;
        if let Some(trace) = trace.as_ref() {
            trace.write_snapshot(create_output(&evidence.join("import-trace.json"))?)?;
        }
        let outcome = if operation.is_ok() {
            "original_succeeded_inconclusive"
        } else if observation.unreachable_trap {
            "unreachable_trap_requires_original_stack_correlation"
        } else {
            "different_or_setup_failure_inconclusive"
        };
        if diagnostic {
            serde_json::to_writer(
                create_output(&evidence.join("diagnostic-outcome.json"))?,
                &json!({
                "format":"tobkiri.native-import-diagnostic-outcome/1","diagnostic_only":true,
                "outcome":outcome,"initialized":initialized,"native_io_admitted":admitted,
                "observation":observation,"physical_cleanup_succeeded":cleanup.is_ok(),
                "accepted_as_parity":false,"production_enabled":false}),
            )?;
        }
        drop((trace, io, snapshot, config, owners));
        crate::reap_allocation_owners()?;
        ensure!(
            account.snapshot()?.charged_bytes == 0,
            "native/Python DATA remained after actual cleanup"
        );
        ensure!(
            control.usage().count(ControlKind::Task) == baseline
                && control.usage().count(ControlKind::Queue) == 0,
            "native task/queue custody did not return after original physical joins"
        );
        cleanup?;
        ensure!(
            deadline_preserved,
            "native/Python call renewed its deadline"
        );
        if diagnostic {
            return match operation {
                Err(error) => Err(error).context(
                    "diagnostic only; observed failure requires comparison with original stack",
                ),
                Ok(()) => Err(anyhow::anyhow!(
                    "diagnostic only; original succeeded, trace is inconclusive"
                )),
            };
        }
        operation?;
        receipts.push(
            json!({"case":index,"cancelled":cancelled,"native_io_suspension_observed":admitted,
            "fuel_remaining":fuel,"elapsed_seconds":started.elapsed().as_secs_f64(),
            "original_deadline_preserved":true,"payload_released_after_cleanup":true}),
        );
    }
    let report = json!({"format":"tobkiri.native-input-stack-observation/1",
        "component_sha256":component_hash.as_str(), "cases":receipts,
        "memory_limit_bytes":tobkiri_gate::NATIVE_SETUP_MEMORY_BYTES,
        "fuel_limit":super::super::FUEL, "wall_seconds":30,
        "actual_os_owner_proof":true, "whole_setup_compatible":false,"production_enabled":false});
    serde_json::to_writer(create_output(&evidence.join("report.json"))?, &report)?;
    Ok(())
}

fn validate_observation(actual: &CoreValue, scenario: &Value) -> Result<()> {
    ensure!(
        actual.object().is_some_and(|object| object.len() == 4),
        "stack observation has missing or extra fields"
    );
    let runtime = actual.get_key("runtime").context("actual Python runtime")?;
    ensure!(
        matches_json(&runtime, &json!({"python":[3,13,16],"pointer_bits":32})),
        "actual guest runtime differs from the pinned Python/Wasm target"
    );
    ensure!(
        actual
            .get_key("local_marker_same_object")
            .is_some_and(|value| value.as_bool() == Some(true)),
        "synchronous Python locals were not retained through native calls"
    );
    let calls = actual
        .get_key("calls")
        .context("captured read observations")?;
    ensure!(
        matches_json(&calls, &scenario["expected_calls"]),
        "native range call order, repeated reads or returned bytes differ from the original capture"
    );
    ensure!(
        !calls
            .array()
            .context("range observations must be an array")?
            .is_empty(),
        "original computation did not issue actual input reads"
    );
    let outcome = actual
        .get_key("outcome")
        .context("original artifact outcome")?;
    let expected = match scenario["expected"]["outcome"].as_str() {
        Some("ok") => json!({"status":"verified",
            "returned_relative_path":scenario["expected"]["relative_path"]}),
        Some("protocol-error") => json!({"status":"protocol_error",
            "error_type":scenario["expected"]["error_type"],
            "error_args":scenario["expected"]["error_args"]}),
        _ => bail!("unsupported independent original outcome"),
    };
    ensure!(
        matches_json(&outcome, &expected),
        "Wasm original artifact result/exception differs from the independent native source"
    );
    Ok(())
}
