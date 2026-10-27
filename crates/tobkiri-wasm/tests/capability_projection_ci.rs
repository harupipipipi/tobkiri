//! External CI DATA comparison only. This is not OS/Profile/approval authority.
use anyhow::{Context, Result, ensure};
use serde::Deserialize;
use serde_json::json;
use std::{fs::OpenOptions, io::Read, path::PathBuf, sync::Arc};
use tobkiri_canon::{CoreBuffer, Digest};
use tobkiri_gate::{CapacityLedger, CoreControl, GateError, NativeSetupLease};
use tobkiri_wasm::{
    NativeDataBinding, NativeDataErrorContract, NativeDataExport, NativeDataResultContract,
    NativeDataSuccessType, NativeDataValue, WasmEngine,
};
use wasmtime::component::Component;

struct AllocationOnlyFixture;
impl NativeSetupLease for AllocationOnlyFixture {
    fn validate(&self) -> std::result::Result<(), GateError> {
        Ok(())
    }
    fn allocation_domain(&self) -> Digest {
        Digest::hash(b"capability-data-ci-allocation-only-not-profile-or-os-authority")
    }
}

#[derive(Deserialize)]
struct Cases {
    format: String,
    oracle_commit: String,
    cases: Vec<Case>,
}
#[derive(Deserialize)]
struct Case {
    name: String,
    payload: String,
    expected: Option<String>,
    expected_error: Option<String>,
}

fn checked_input(label: &str, limit: u64) -> Result<Vec<u8>> {
    let path = PathBuf::from(std::env::var(format!("TOBKIRI_PROJECTION_{label}"))?);
    ensure!(
        path.is_absolute() && path.canonicalize()? == path,
        "physical CI input required"
    );
    let metadata = path.symlink_metadata()?;
    ensure!(
        metadata.is_file() && metadata.len() <= limit,
        "CI input exceeds its bound"
    );
    let mut bytes = Vec::new();
    std::fs::File::open(&path)?
        .take(limit + 1)
        .read_to_end(&mut bytes)?;
    ensure!(bytes.len() as u64 == metadata.len(), "CI input changed");
    let expected = std::env::var(format!("TOBKIRI_PROJECTION_{label}_SHA256"))?;
    ensure!(
        Digest::hash(&bytes).as_str() == format!("sha256:{expected}"),
        "CI input identity changed"
    );
    Ok(bytes)
}

#[tokio::test]
#[ignore = "actual external Python component, CI only; no product authority proof"]
async fn native_capability_projection_comparison() -> Result<()> {
    ensure!(
        std::env::var("GITHUB_ACTIONS").as_deref() == Ok("true"),
        "CI only"
    );
    let bytes = checked_input("COMPONENT", 32 * 1024 * 1024)?;
    let cases: Cases = serde_json::from_slice(&checked_input("CASES", 4 * 1024 * 1024)?)?;
    ensure!(
        cases.format == "tobkiri.capability-projection-comparison/1"
            && cases.oracle_commit == "bf8cf14664580b7ca226b56d7bd5a311a19f44b4"
            && !cases.cases.is_empty()
            && cases.cases.len() <= 64,
        "original comparison identity differs"
    );
    let control = CoreControl::default();
    let ledger = CapacityLedger::new();
    let account = ledger.native_setup_account(&control, || Ok(Arc::new(AllocationOnlyFixture)))?;
    let engine = WasmEngine::new_with_control(&control)?;
    let component = Component::new(engine.engine(), &bytes)?;
    drop(bytes);
    let invocation = account.begin()?;
    let deadline = invocation.deadline()?;
    let store = engine.native_setup_store(invocation, ())?;
    let owners = store.allocation_owners().clone();
    let mut program = store
        .prepare_data_program(
            &component,
            &[NativeDataBinding {
                export: NativeDataExport::new(
                    Some("tobkiri-ui:capability-projection/projection@1.0.0"),
                    "project",
                ),
                result: Some(NativeDataResultContract {
                    success: NativeDataSuccessType::String,
                    error: NativeDataErrorContract {
                        code_field: "code",
                        message_field: "message",
                        details_field: None,
                        cases: &["invalid-request", "projection-failed"],
                    },
                }),
            }],
        )
        .await?;
    let mut observed = Vec::new();
    for case in cases.cases {
        ensure!(
            case.payload.len() <= 4 * 1024 * 1024,
            "case exceeds the original envelope"
        );
        let input = CoreBuffer::copy_from(case.payload.as_bytes(), owners.graph())?;
        let reply = program
            .invoke(0, Some(&input))
            .await
            .with_context(|| format!("actual guest invocation failed: {}", case.name))?;
        match (reply, case.expected, case.expected_error) {
            (Ok(NativeDataValue::String(actual)), Some(expected), None) => {
                ensure!(
                    actual.as_str()? == expected,
                    "original projection differs: {}",
                    case.name
                );
                observed.push(json!({"name":case.name,"equal":true,"output_sha256":Digest::hash(actual.as_bytes()).as_str()}));
            }
            (Err(error), None, Some(code)) => {
                ensure!(
                    error.code().as_str()? == code,
                    "typed boundary error differs: {}",
                    case.name
                );
                observed.push(json!({"name":case.name,"boundary_error":code}));
            }
            _ => anyhow::bail!("actual typed result differs: {}", case.name),
        }
        ensure!(
            program.original_deadline() == deadline,
            "deadline was renewed"
        );
        ensure!(
            program.observation().post_return && !program.observation().store_retired,
            "original post-return or Store lifetime differs"
        );
    }
    let fuel_remaining = program.fuel_remaining()?;
    ensure!(fuel_remaining <= 2_000_000_000, "fuel was raised");
    drop((program, owners));
    tobkiri_wasm::reap_allocation_owners()?;
    ensure!(
        account.snapshot()?.charged_bytes == 0,
        "physical guest payload remains charged"
    );
    let output = PathBuf::from(std::env::var("TOBKIRI_PROJECTION_REPORT")?);
    let file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(output)?;
    serde_json::to_writer_pretty(
        file,
        &json!({
            "format":"tobkiri.native-capability-projection-comparison/1", "cases":observed,
            "oracle_commit":cases.oracle_commit, "original_absolute_deadline_preserved":true,
            "fuel_remaining":fuel_remaining, "memory_limit_bytes":64*1024*1024,
            "payload_after_physical_drop":0, "allocation_only_portable_fixture":true,
            "actual_os_profile_or_approval_proof":false, "ordinary_pack_connected":false,
            "production_enabled":false,
        }),
    )?;
    Ok(())
}
