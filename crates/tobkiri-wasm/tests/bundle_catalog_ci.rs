//! Whole original bundle DATA comparison. No product/OS/Profile authority proof.
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
        Digest::hash(b"bundle-data-ci-allocation-only-not-profile-or-os-authority")
    }
}
#[derive(Deserialize)]
struct Original {
    format: String,
    oracle_commit: String,
    expected_sha256: String,
    source_identity_sha256: String,
    schema_store_expected_sha256: String,
    schema_store_expected_bytes: usize,
    schema_store_cold_selfcheck_calls: usize,
    expected_bytes: usize,
    entries: usize,
    read_occurrences: usize,
    reread_mutation: Mutation,
}
#[derive(Deserialize)]
struct Mutation {
    expected_error: String,
}

fn input(label: &str, limit: u64) -> Result<Vec<u8>> {
    let path = PathBuf::from(std::env::var(format!("TOBKIRI_BUNDLE_{label}"))?);
    ensure!(
        path.is_absolute() && path.canonicalize()? == path,
        "physical CI input required"
    );
    let metadata = path.symlink_metadata()?;
    ensure!(
        metadata.is_file() && metadata.len() <= limit,
        "CI input exceeds original bound"
    );
    let mut bytes = Vec::new();
    std::fs::File::open(&path)?
        .take(limit + 1)
        .read_to_end(&mut bytes)?;
    ensure!(bytes.len() as u64 == metadata.len(), "CI input changed");
    ensure!(
        Digest::hash(&bytes).as_str()
            == format!(
                "sha256:{}",
                std::env::var(format!("TOBKIRI_BUNDLE_{label}_SHA256"))?
            ),
        "CI input identity changed"
    );
    Ok(bytes)
}

#[tokio::test]
#[ignore = "actual full original Python component, CI only; no product authority proof"]
async fn native_bundle_catalog_comparison() -> Result<()> {
    ensure!(
        std::env::var("GITHUB_ACTIONS").as_deref() == Ok("true"),
        "CI only"
    );
    let bytes = input("COMPONENT", 32 * 1024 * 1024)?;
    let negative_bytes = input("NEGATIVE_COMPONENT", 32 * 1024 * 1024)?;
    ensure!(
        Digest::hash(&bytes) != Digest::hash(&negative_bytes),
        "diagnostic artifact must have its own actual identity"
    );
    let original: Original = serde_json::from_slice(&input("CASES", 4 * 1024 * 1024)?)?;
    ensure!(
        original.format == "tobkiri.bundle-catalog-comparison/1"
            && original.oracle_commit == "54e7da008997a3eb45876cbe18459dba9902cc9d"
            && original.entries == 305
            && original.read_occurrences == 453
            && original.schema_store_cold_selfcheck_calls == 42
            && original.schema_store_expected_bytes <= 4 * 1024 * 1024
            && original.expected_bytes <= 4 * 1024 * 1024,
        "full pinned original comparison differs"
    );
    let cases = [
        (
            "embedded-source-identity",
            "tobkiri.embedded-bundle-identity/1",
            None,
            false,
        ),
        (
            "cold-original-schema-store",
            "tobkiri.cold-original-schema-store/1",
            None,
            false,
        ),
        (
            "full-original-bundle",
            "tobkiri.embedded-bundle-read/1",
            None,
            false,
        ),
        (
            "different-executable-reread",
            "tobkiri.embedded-bundle-read/1",
            Some(original.reread_mutation.expected_error.as_str()),
            true,
        ),
        (
            "unknown-comparison-command",
            "tobkiri.unknown-bundle-command/1",
            Some("unknown bundle comparison command"),
            false,
        ),
    ];
    let control = CoreControl::default();
    let ledger = CapacityLedger::new();
    let account = ledger.native_setup_account(&control, || Ok(Arc::new(AllocationOnlyFixture)))?;
    let engine = WasmEngine::new_with_control(&control)?;
    let component = Component::new(engine.engine(), &bytes)?;
    let negative_component = Component::new(engine.engine(), &negative_bytes)?;
    let positive_sha256 = Digest::hash(&bytes);
    let negative_sha256 = Digest::hash(&negative_bytes);
    drop((bytes, negative_bytes));
    let mut observed = Vec::new();
    // Each independent input has a new actual invocation. No invocation's
    // deadline/fuel is replenished, including during Python initialization.
    for (name, payload, expected_error, diagnostic) in cases {
        eprintln!("bundle comparison: begin {name}");
        ensure!(
            payload.len() <= 4 * 1024 * 1024,
            "carrier exceeds original envelope"
        );
        let invocation = account.begin()?;
        let deadline = invocation.deadline()?;
        let store = engine.native_setup_store(invocation, ())?;
        let owners = store.allocation_owners().clone();
        let mut program = store
            .prepare_data_program(
                if diagnostic {
                    &negative_component
                } else {
                    &component
                },
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
        let prepared_fuel = program.fuel_remaining()?;
        eprintln!("bundle comparison: prepared {name}; fuel_remaining={prepared_fuel}");
        let input = CoreBuffer::copy_from(payload.as_bytes(), owners.graph())?;
        let input_fuel = program.fuel_remaining()?;
        eprintln!("bundle comparison: input copied {name}; fuel_remaining={input_fuel}");
        let reply = program
            .invoke(0, Some(&input))
            .await
            .with_context(|| format!("actual whole-bundle invocation failed: {name}"))?;
        match (reply, expected_error) {
            (Ok(NativeDataValue::String(actual)), None) => {
                let source_identity = name == "embedded-source-identity";
                let schema_store = name == "cold-original-schema-store";
                let expected = if source_identity {
                    &original.source_identity_sha256
                } else if schema_store {
                    &original.schema_store_expected_sha256
                } else {
                    &original.expected_sha256
                };
                let expected_bytes = if schema_store {
                    original.schema_store_expected_bytes
                } else {
                    original.expected_bytes
                };
                ensure!(
                    (source_identity || actual.as_bytes().len() == expected_bytes)
                        && Digest::hash(actual.as_bytes()).as_str() == format!("sha256:{expected}"),
                    "original source/catalog differs: {name}"
                );
                observed.push(json!({"name":name,"all_original_collections_equal":!source_identity && !schema_store,"original_schema_store_and_cold_cache_equal":schema_store,"embedded_source_bytes_equal":source_identity,"output_bytes":actual.as_bytes().len(),"output_sha256":expected}));
            }
            (Err(error), Some(message)) => {
                ensure!(
                    error.code().as_str()? == "invalid-request"
                        && error.message().as_str()? == message,
                    "original refusal differs: {name}"
                );
                observed.push(json!({"name":name,"original_error_equal":true}));
            }
            _ => anyhow::bail!("actual typed result differs: {name}"),
        }
        ensure!(program.original_deadline() == deadline, "deadline renewed");
        ensure!(
            program.observation().post_return && !program.observation().store_retired,
            "post-return or Store lifetime differs"
        );
        let fuel = program.fuel_remaining()?;
        ensure!(fuel <= 2_000_000_000, "fuel raised");
        ensure!(
            prepared_fuel >= input_fuel && input_fuel >= fuel,
            "fuel increased between phases"
        );
        observed.last_mut().unwrap()["fuel_after_prepare"] = json!(prepared_fuel);
        observed.last_mut().unwrap()["fuel_after_input_copy"] = json!(input_fuel);
        observed.last_mut().unwrap()["fuel_remaining"] = json!(fuel);
        observed.last_mut().unwrap()["artifact_sha256"] = json!(if diagnostic {
            negative_sha256.as_str()
        } else {
            positive_sha256.as_str()
        });
        drop((input, program, owners));
        tobkiri_wasm::reap_allocation_owners()?;
        ensure!(
            account.snapshot()?.charged_bytes == 0,
            "guest payload remains charged"
        );
        eprintln!("bundle comparison: completed {name}; fuel_remaining={fuel}; retained_payload=0");
    }
    let file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(PathBuf::from(std::env::var("TOBKIRI_BUNDLE_REPORT")?))?;
    serde_json::to_writer_pretty(
        file,
        &json!({
            "format":"tobkiri.native-bundle-catalog-comparison/1", "cases":observed,
            "oracle_commit":original.oracle_commit,"original_entries":original.entries,
            "original_read_occurrences":original.read_occurrences,
            "memory_limit_bytes":64*1024*1024,"payload_after_physical_drop":0,
            "original_absolute_deadline_preserved":true,"allocation_only_portable_fixture":true,
            "installed_artifact_or_gui_proof":false,"ordinary_pack_connected":false,"production_enabled":false,
        }),
    )?;
    Ok(())
}
