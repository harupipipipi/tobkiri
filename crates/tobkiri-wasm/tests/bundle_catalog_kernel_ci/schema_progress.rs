//! Split-call diagnostic only. The original cold/full one-shot gates stay separate.
use super::*;

#[tokio::test]
#[ignore = "separate actual App schema diagnostic artifact; Linux CI only"]
async fn actual_app_schema_cumulative_progress() -> Result<()> {
    ensure!(
        std::env::var("GITHUB_ACTIONS").as_deref() == Ok("true")
            && std::env::var("TOBKIRI_BUNDLE_APP_BACKEND").as_deref() == Ok("1")
            && std::env::var("TOBKIRI_BUNDLE_SCHEMA_PROGRESS").as_deref() == Ok("1"),
        "explicit CI-only App diagnostic required"
    );
    let original: Value = serde_json::from_slice(&input("CASES", 4 * 1024 * 1024)?)?;
    ensure!(
        original["format"] == "tobkiri.bundle-catalog-comparison/1"
            && original["oracle_commit"] == "54e7da008997a3eb45876cbe18459dba9902cc9d",
        "actual pinned original reference required"
    );
    let mut expected_names: Vec<String> = original["schema_resources"]
        .as_object()
        .context("original schema resource inventory missing")?
        .keys()
        .map(|path| {
            path.strip_prefix("tobkiri_protocol/schemas/")
                .filter(|name| !name.contains('/') && name.len() <= 128)
                .map(str::to_owned)
                .context("original schema resource path differs")
        })
        .collect::<Result<_>>()?;
    expected_names.sort();
    ensure!(expected_names.len() == 42, "complete original42 required");
    let bytes = input("COMPONENT", 32 * 1024 * 1024)?;
    let identity = Digest::hash(&bytes);
    let control = CoreControl::default();
    let ledger = CapacityLedger::new();
    let account = ledger.native_setup_account(&control, || Ok(Arc::new(AllocationOnlyFixture)))?;
    let engine = WasmEngine::new_with_control(&control)?;
    let component = Component::new(engine.engine(), &bytes)?;
    drop(bytes);
    let mut report = json!({
        "format":"tobkiri.app-schema-cumulative-progress/1",
        "component_sha256":identity.as_str(), "fuel_limit":2_000_000_000u64,
        "memory_limit_bytes":64*1024*1024, "wall_seconds":30,
        "stores":1,"accounts":1,"budget_resets":0,"validation_results_prepared":false,
        "export_calls_started":0,"export_calls_completed":0,"maximum_export_calls":43,
        "qualification":false,"production_enabled":false,"gui_or_os_owner_proof":false,
        "one_shot_acceptance":false,"function_fuel_attribution":false,
        "perturbation":"split load_schema calls add boundary, listing and observation work",
        "completed_schemas":0,"last_started":null,"checkpoints":[],"stage":"begin",
        "cold_value_counts_verified":false,"native_value_counts_observed":false,
        "last_value_counts":null,"last_value_counts_index":null,
        "last_successful_observation":null
    });
    let started = Instant::now();
    let outcome: Result<()> = async {
        let invocation = account.begin()?;
        let deadline = invocation.deadline()?;
        let store = engine.native_setup_store(invocation, ())?;
        let owners = store.allocation_owners().clone();
        report["stage"] = json!("prepare");
        let mut program = store.prepare_data_program(&component, &[NativeDataBinding {
            export: NativeDataExport::new(
                Some("tobkiri-ui:capability-projection/projection@1.0.0"), "project"),
            result: Some(NativeDataResultContract {
                success: NativeDataSuccessType::String,
                error: NativeDataErrorContract {
                    code_field:"code",message_field:"message",details_field:None,
                    cases:&["invalid-request","projection-failed"],
                },
            }),
        }]).await?;
        let mut remaining = program.fuel_remaining()?;
        ensure!(remaining <= 2_000_000_000, "original cumulative fuel bound differs");
        report["fuel_after_prepare"] = json!(remaining);
        let mut value_counts = [0_u64; 4];
        // The same program/account/Store survives every successful call.
        for index in 0..=42 {
            let name = if index == 0 { None } else { Some(&expected_names[index-1]) };
            let command = name.map_or_else(
                || "tobkiri.app-schema-progress-list/1".to_owned(),
                |name| format!("tobkiri.app-schema-progress-load/1:{name}"),
            );
            report["stage"] = json!("allocate-input");
            let input = CoreBuffer::copy_from(command.as_bytes(), owners.graph())?;
            report["stage"] = json!("invoke");
            report["last_started"] = json!({"index":index,"name":name});
            report["export_calls_started"] = json!(index + 1);
            let reply = program.invoke(0, Some(&input)).await;
            let observation = serde_json::to_value(program.observation())?;
            let same_deadline = program.original_deadline() == deadline;
            report["original_deadline_preserved"] = json!(same_deadline);
            report["last_observation"] = observation.clone();
            ensure!(same_deadline, "diagnostic renewed original deadline");
            // A trap retires the program; never query fuel or resume it afterwards.
            let actual = match reply.context("actual schema diagnostic invocation failed")? {
                Ok(NativeDataValue::String(value)) => value,
                Err(error) => {
                    report["domain_error"] = json!({"code":error.code().as_str()?,
                        "message":error.message().as_str()?.chars().take(1024).collect::<String>()});
                    anyhow::bail!("actual schema diagnostic returned domain refusal");
                }
                _ => anyhow::bail!("actual schema diagnostic result type differs"),
            };
            ensure!(program.observation().post_return && !program.observation().store_retired
                && !program.observation().post_return_failed
                && !program.observation().unreachable_trap
                && !program.observation().deadline_exceeded,
                "successful call did not preserve actual Store lifecycle");
            let after = program.fuel_remaining()?;
            ensure!(after <= remaining, "diagnostic replenished fuel");
            ensure!(actual.as_bytes().len() <= 16*1024, "bounded checkpoint required");
            let receipt: Value = serde_json::from_slice(actual.as_bytes())?;
            ensure!(receipt["format"] == "tobkiri.app-schema-progress-step/1"
                && receipt["captured_resource_owner"] == true
                && receipt["adapter_module"] == "bundle_catalog_adapter"
                && receipt["qualification"] == false && receipt["production_enabled"] == false,
                "actual captured App checkpoint required");
            if index == 0 {
                ensure!(receipt["names"] == json!(expected_names)
                    && receipt["outcome"] == "metadata" && receipt["backend_entries"] == 0
                    && receipt["backend_frames"] == 0,
                    "cold actual original name order/provider differs");
            } else {
                ensure!(receipt["name"] == json!(name) && receipt["outcome"] == "loaded"
                    && receipt["backend_entries"] == index,
                    "actual cold schema loader did not complete exactly once");
            }
            ensure!(receipt["schema_cache"] == json!({"hits":0,"misses":index,"size":index}),
                "actual original cold schema cache differs");
            let counters = receipt["value_counts"].as_object()
                .context("actual value counter object required")?;
            ensure!(counters.len() == 4, "exact actual value counter fields required");
            let mut next_counts = [0_u64; 4];
            for (slot, &field) in ["native_type_checks", "python_type_checks",
                "native_numeric_comparisons", "python_numeric_comparisons"].iter().enumerate() {
                let count = counters.get(field).and_then(Value::as_u64)
                    .context("actual native value counter required")?;
                ensure!(count >= value_counts[slot] && (index > 0 || count == 0),
                    "value counters reset or validation ran during initialization");
                next_counts[slot] = count;
            }
            ensure!(index == 0 || next_counts[0] > value_counts[0],
                "actual schema did not enter native scalar typing");
            value_counts = next_counts;
            if index == 0 {
                report["cold_value_counts_verified"] = json!(true);
            } else {
                report["native_value_counts_observed"] = json!(true);
                report["completed_schemas"] = json!(index);
            }
            report["last_value_counts"] = receipt["value_counts"].clone();
            report["last_value_counts_index"] = json!(index);
            report["last_successful_observation"] = observation.clone();
            report["checkpoints"].as_array_mut().context("checkpoint array")?.push(json!({
                "index":index,"name":name,"fuel_before":remaining,"fuel_remaining":after,
                "observation":observation,"receipt":receipt,
            }));
            remaining = after;
            report["export_calls_completed"] = json!(index + 1);
            drop((actual, input));
        }
        report["stage"] = json!("completed-split-schema-sequence");
        drop((program, owners));
        Ok(())
    }.await;
    tobkiri_wasm::reap_allocation_owners()?;
    let retained = account.snapshot()?.charged_bytes;
    let expected_fuel_stop = outcome
        .as_ref()
        .err()
        .is_some_and(|error| format!("{error:#}").contains("all fuel consumed by WebAssembly"))
        && report["last_observation"]["store_retired"] == true
        && report["last_observation"]["fuel_remaining"] == 0
        && report["last_observation"]["unreachable_trap"] == false
        && report["last_observation"]["deadline_exceeded"] == false
        && report["original_deadline_preserved"] == true
        && report["cold_value_counts_verified"] == true
        && report["native_value_counts_observed"] == true
        && report["last_value_counts_index"] == report["completed_schemas"];
    report["expected_cumulative_fuel_stop"] = json!(expected_fuel_stop);
    report["sequence_completed"] = json!(outcome.is_ok());
    report["diagnostic_observation_valid"] = json!(outcome.is_ok() || expected_fuel_stop);
    report["cleanup_success"] = json!(retained == 0);
    report["payload_after_physical_drop"] = json!(retained);
    report["elapsed_seconds"] = json!(started.elapsed().as_secs_f64());
    if let Err(error) = &outcome {
        report["error"] = json!(format!("{error:#}").chars().take(4096).collect::<String>());
    }
    let file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(PathBuf::from(std::env::var("TOBKIRI_BUNDLE_REPORT")?))?;
    serde_json::to_writer_pretty(file, &report)?;
    ensure!(retained == 0, "retained invocation payload");
    if !expected_fuel_stop {
        outcome?;
    }
    Ok(())
}
