//! Independent actual App/Rust Wasm observation, never generic64 or GUI acceptance.
use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};
use std::{fs::OpenOptions, io::Read, path::PathBuf, sync::Arc, time::Instant};
use tobkiri_canon::{CoreBuffer, Digest};
use tobkiri_gate::{CapacityLedger, CoreControl, GateError, NativeSetupLease};
use tobkiri_wasm::{
    NativeDataBinding, NativeDataErrorContract, NativeDataExport, NativeDataResultContract,
    NativeDataSuccessType, NativeDataValue, WasmEngine,
};
use wasmtime::component::Component;

#[path = "bundle_catalog_kernel_ci/schema_progress.rs"]
mod schema_progress;

struct AllocationOnlyFixture;
impl NativeSetupLease for AllocationOnlyFixture {
    fn validate(&self) -> std::result::Result<(), GateError> {
        Ok(())
    }
    fn allocation_domain(&self) -> Digest {
        Digest::hash(b"rust-bundle-catalog-data-only-not-profile-or-os-authority")
    }
}
fn input(label: &str, limit: u64) -> Result<Vec<u8>> {
    let path = PathBuf::from(std::env::var(format!("TOBKIRI_BUNDLE_{label}"))?);
    ensure!(
        path.is_absolute() && path.canonicalize()? == path,
        "physical input required"
    );
    let metadata = path.symlink_metadata()?;
    ensure!(
        metadata.is_file() && metadata.len() <= limit,
        "bounded input required"
    );
    let mut bytes = Vec::new();
    std::fs::File::open(&path)?
        .take(limit + 1)
        .read_to_end(&mut bytes)?;
    ensure!(bytes.len() as u64 == metadata.len(), "input changed");
    ensure!(
        Digest::hash(&bytes).as_str()
            == format!(
                "sha256:{}",
                std::env::var(format!("TOBKIRI_BUNDLE_{label}_SHA256"))?
            ),
        "input identity changed"
    );
    Ok(bytes)
}

#[tokio::test]
#[ignore = "actual source-built App/Rust component; Linux CI only"]
async fn actual_rust_bundle_catalog_one_shot() -> Result<()> {
    compare_one_shot(false).await
}

#[tokio::test]
#[ignore = "independent cold schema operation using the SAME actual component; Linux CI only"]
async fn actual_app_frame_cold_schema_store_one_shot() -> Result<()> {
    compare_one_shot(true).await
}

// Separate operations, separate original invocations. Their results/fuel cannot
// be combined to claim that the full catalog completed within one budget.
async fn compare_one_shot(schema_store: bool) -> Result<()> {
    ensure!(
        std::env::var("GITHUB_ACTIONS").as_deref() == Ok("true"),
        "CI only"
    );
    let app_backend = match std::env::var("TOBKIRI_BUNDLE_APP_BACKEND").as_deref() {
        Ok("1") => true,
        Ok("0") | Err(std::env::VarError::NotPresent) => false,
        _ => anyhow::bail!("explicit App backend fixture selection required"),
    };
    let original: Value = serde_json::from_slice(&input("CASES", 4 * 1024 * 1024)?)?;
    ensure!(
        original["format"] == "tobkiri.bundle-catalog-comparison/1"
            && original["oracle_commit"] == "54e7da008997a3eb45876cbe18459dba9902cc9d"
            && original["entries"] == 305
            && original["read_occurrences"] == 453,
        "complete original reference required"
    );
    if schema_store {
        ensure!(
            original["schema_store_cold_selfcheck_calls"] == 42
                && original["schema_store_expected_bytes"]
                    .as_u64()
                    .is_some_and(|n| n <= 4 * 1024 * 1024)
                && original["schema_store_expected_sha256"].is_string(),
            "independent original cold schema reference required"
        );
    }
    let bytes = input("COMPONENT", 32 * 1024 * 1024)?;
    let identity = Digest::hash(&bytes);
    let control = CoreControl::default();
    let ledger = CapacityLedger::new();
    let account = ledger.native_setup_account(&control, || Ok(Arc::new(AllocationOnlyFixture)))?;
    let engine = WasmEngine::new_with_control(&control)?;
    let component = Component::new(engine.engine(), &bytes)?;
    drop(bytes);
    let mut report = json!({"format":"tobkiri.rust-bundle-catalog-one-shot/1",
        "component_sha256":identity.as_str(), "fuel_limit":2_000_000_000u64,
        "memory_limit_bytes":64*1024*1024, "wall_seconds":30,
        "qualification":false,"production_enabled":false,"gui_or_os_owner_proof":false,
        "generic_64_parity_claim":false,"invocations":1,"stage":"begin"});
    report["app_schema_backend"] = json!(app_backend);
    report["operation"] = json!(if schema_store {
        "cold-schema-store"
    } else {
        "full-catalog"
    });
    let started = Instant::now();
    let outcome: Result<()> = async {
        let invocation = account.begin()?;
        let deadline = invocation.deadline()?;
        let store = engine.native_setup_store(invocation, ())?;
        let owners = store.allocation_owners().clone();
        report["stage"] = json!("prepare");
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
        report["fuel_after_prepare"] = json!(program.fuel_remaining()?);
        report["stage"] = json!(if schema_store {
            "invoke-cold-schema-store"
        } else {
            "invoke-full-original-catalog"
        });
        let payload = if schema_store {
            b"tobkiri.cold-original-schema-store/1".as_slice()
        } else {
            b"tobkiri.embedded-bundle-read/1".as_slice()
        };
        let input = CoreBuffer::copy_from(payload, owners.graph())?;
        let reply = program.invoke(0, Some(&input)).await;
        report["observation"] = serde_json::to_value(program.observation())?;
        report["original_deadline_preserved"] = json!(program.original_deadline() == deadline);
        ensure!(program.original_deadline() == deadline, "deadline renewed");
        let actual = match reply.with_context(|| {
            if schema_store {
                "actual original cold schema store failed"
            } else {
                "actual full original App/Rust call failed"
            }
        })? {
            Ok(NativeDataValue::String(value)) => value,
            Err(error) => {
                let message = error.message().as_str()?;
                report["domain_error"] = json!({
                    "code":error.code().as_str()?,
                    "message":message.chars().take(4096).collect::<String>(),
                    "message_bytes":message.len(),
                    "message_truncated":message.chars().count() > 4096,
                });
                anyhow::bail!("actual App returned a domain refusal");
            }
            _ => anyhow::bail!("actual typed App/Rust result differs"),
        };
        ensure!(
            program.observation().post_return && !program.observation().store_retired,
            "post-return or Store lifetime differs"
        );
        if schema_store {
            ensure!(
                actual.as_bytes().len() <= 4 * 1024 * 1024
                    && original["schema_store_expected_bytes"].as_u64()
                        == Some(actual.as_bytes().len() as u64)
                    && original["schema_store_expected_sha256"]
                        == Digest::hash(actual.as_bytes())
                            .as_str()
                            .trim_start_matches("sha256:"),
                "actual original cold schema store/cache outcome differs"
            );
            report["stage"] = json!("returned-original-cold-schema-store");
            report["schema_store_output_bytes"] = json!(actual.as_bytes().len());
            report["schema_store_output_sha256"] = original["schema_store_expected_sha256"].clone();
            drop((actual, input, program, owners));
            return Ok(());
        }
        ensure!(
            actual.as_bytes().len() <= 16 * 1024,
            "bounded runtime receipt required"
        );
        let receipt: Value = serde_json::from_slice(actual.as_bytes())?;
        let expected_format = if app_backend {
            "tobkiri.app-frame-bundle-catalog-observation/1"
        } else {
            "tobkiri.rust-bundle-catalog-observation/1"
        };
        let actual_entries = if app_backend {
            receipt["backend_entries_before"] == 0 && receipt["backend_entries_after"] == 45
        } else {
            receipt["keyword_entries_before"] == 0
                && receipt["keyword_entries_after"]
                    .as_u64()
                    .is_some_and(|n| n > 0)
        };
        ensure!(
            receipt["format"] == expected_format
                && receipt["actual_sha256"] == original["expected_sha256"]
                && receipt["actual_bytes"] == original["expected_bytes"]
                && receipt["entry_reads"] == 453
                && receipt["schema_reads"]
                    .as_array()
                    .is_some_and(|rows| rows.len() == 45)
                && receipt["schema_cache"] == json!({"misses":45,"hits":302,"size":45})
                && actual_entries
                && receipt["captured_resource_owner"] == true
                && receipt["keyword_loader"] == "bundle_catalog_loader"
                && receipt["qualification"] == false
                && receipt["production_enabled"] == false,
            "actual catalog/Rust/resource/cache receipt differs"
        );
        report["runtime_receipt"] = receipt;
        report["stage"] = json!("returned-original-catalog");
        drop((actual, input, program, owners));
        Ok(())
    }
    .await;
    // Both success and trap destroy the same invocation; never retry after trap.
    tobkiri_wasm::reap_allocation_owners()?;
    let retained = account.snapshot()?.charged_bytes;
    report["payload_after_physical_drop"] = json!(retained);
    report["elapsed_seconds"] = json!(started.elapsed().as_secs_f64());
    report["passed"] = json!(outcome.is_ok() && retained == 0);
    if let Err(error) = &outcome {
        report["error"] = json!(format!("{error:#}").chars().take(4096).collect::<String>());
    }
    let mut path = PathBuf::from(std::env::var("TOBKIRI_BUNDLE_REPORT")?);
    if schema_store {
        path.set_file_name("app-schema-cold-store-observation.json");
    }
    let file = OpenOptions::new().write(true).create_new(true).open(path)?;
    serde_json::to_writer_pretty(file, &report)?;
    ensure!(retained == 0, "retained invocation payload");
    outcome
}
