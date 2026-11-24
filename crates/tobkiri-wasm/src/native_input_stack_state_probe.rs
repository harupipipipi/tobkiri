//! Real component exports and native owners; no synthetic Profile or authority.
use super::*;

#[tokio::test(flavor = "current_thread")]
#[ignore = "requires exact CI-built Python component and original native reference"]
async fn actual_python_exports_keep_init_and_isolate_two_native_stores() -> Result<()> {
    ensure!(
        std::env::var("GITHUB_ACTIONS").as_deref() == Ok("true")
            && std::env::var("TOBKIRI_TEST_INPUT_STACK_CI").as_deref() == Ok("1")
            && std::env::var_os("TOBKIRI_TEST_INPUT_STACK_IMPORT_DIAGNOSTICS").is_none(),
        "state proof requires the original non-diagnostic CI harness"
    );
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let _census = crate::child_census::lock_census_fixture().await;
    let (bytes, oracle) = stack_inputs(false)?;
    let raw = original_fixture();
    ensure!(
        oracle["input_bytes"] == SIZE && oracle["input_sha256"] == Digest::hash(&raw).as_str(),
        "independent original state fixture differs"
    );
    let scenarios = oracle["scenarios"]
        .as_array()
        .context("original scenarios")?;
    ensure!(
        scenarios.len() == 2
            && scenarios[0]["name"] == "success"
            && scenarios[1]["name"] == "wrong-digest",
        "state proof requires both unchanged original scenarios"
    );
    let base = PathBuf::from(
        std::env::var_os("TOBKIRI_TEST_INPUT_STACK_EVIDENCE").context("evidence path")?,
    );
    let mut name = base.file_name().context("evidence name")?.to_os_string();
    name.push("-state");
    let evidence = base.with_file_name(name);
    ensure!(
        evidence.is_absolute()
            && !evidence.exists()
            && std::fs::canonicalize(evidence.parent().context("evidence parent")?)?
                == evidence.parent().unwrap(),
        "state evidence requires its own fresh physical directory"
    );
    std::fs::DirBuilder::new().mode(0o700).create(&evidence)?;
    let fixture = tempfile::tempdir()?;
    let control = CoreControl::default();
    let root = tobkiri_store::DataRoot::open(fixture.path().join("data"))?;
    let owner = root.native_setup_owner_lease(&control)?;
    let account =
        CapacityLedger::new().native_setup_account(&control, || Ok(Arc::new(owner.clone())))?;
    let engine = crate::WasmEngine::new_with_control(&control)?;
    let component_hash = Digest::hash(&bytes);
    let component = Component::new(engine.engine(), &bytes)?;
    drop(bytes);
    let baseline_tasks = control.usage().count(ControlKind::Task);
    let mut programs = Vec::new();
    let mut ios = Vec::new();
    let mut snapshots = Vec::new();
    let mut owners = Vec::new();
    let mut deadlines = Vec::new();
    let mut provenances = Vec::new();
    let mut original_fuel = Vec::new();
    let operation: Result<()> = async {
        for (index, scenario) in scenarios.iter().enumerate() {
            let source = format!("input-{index}");
            std::fs::write(fixture.path().join(&source), &raw)?;
            let invocation = account.begin()?;
            provenances.push(invocation.provenance());
            deadlines.push(invocation.deadline()?);
            let mut store = engine.native_setup_store(invocation, ())?;
            owners.push(store.allocation_owners().clone());
            ios.push(store.bind_native_data_io()?);
            let source_hash = Digest::hash(&raw);
            snapshots.push(
                ios[index]
                    .capture_file_snapshot(
                        &owner,
                        tobkiri_store::NativeSnapshotSource {
                            root: fixture.path(),
                            relative: &source,
                            length: raw.len() as u64,
                            sha256: &source_hash,
                        },
                    )
                    .await?,
            );
            drop(store.seal_captured_inputs(&[crate::NativeCapturedFile {
                path: "artifact",
                snapshot: &snapshots[index],
            }])?);
            std::fs::write(
                fixture.path().join(&source),
                b"changed after own APFS snapshot",
            )?;
            programs.push(
                store
                    .prepare_data_program(&component, &stack_bindings())
                    .await?,
            );
            let program = &mut programs[index];
            let invalid = CoreBuffer::copy_from(b"not-json", owners[index].graph())?;
            // Store A has already initialized when B reaches this call. B must
            // still report invalid-input, never A's already-initialized state.
            expect_init_error(
                program.invoke(0, Some(&invalid)).await?,
                "invalid-input",
                "invalid native input probe frame",
            )?;
            let config = CoreBuffer::copy_from(
                scenario["init"].as_str().context("state init")?.as_bytes(),
                owners[index].graph(),
            )?;
            ensure!(
                matches!(
                    program.invoke(0, Some(&config)).await?,
                    Ok(NativeDataValue::Unit)
                ),
                "valid init after invalid init did not succeed independently"
            );
            let other = CoreBuffer::copy_from(
                scenarios[1 - index]["init"]
                    .as_str()
                    .context("replacement init")?
                    .as_bytes(),
                owners[index].graph(),
            )?;
            expect_init_error(
                program.invoke(0, Some(&other)).await?,
                "already-initialized",
                "native input probe is already initialized",
            )?;
            original_fuel.push(program.fuel_remaining()?);
        }
        ensure!(
            provenances[0].account_generation == provenances[1].account_generation
                && provenances[0].invocation != provenances[1].invocation,
            "state proof did not use distinct real invocations of the original account"
        );
        for (ordinal, index) in [1, 0, 1].into_iter().enumerate() {
            let program = &mut programs[index];
            let Ok(NativeDataValue::String(output)) = program.invoke(1, None).await? else {
                bail!("interleaved original verifier did not return its observation");
            };
            let parsed = CoreValue::parse_guest(output.as_bytes(), owners[index].graph())?;
            validate_observation(&parsed, &scenarios[index])?;
            create_output(&evidence.join(format!("store-{index}-run-{ordinal}.json")))?
                .write_all(output.as_bytes())?;
            ensure!(
                program.observation().post_return && !program.observation().store_retired,
                "state observation did not complete real post-return"
            );
            let remaining = program.fuel_remaining()?;
            ensure!(
                remaining > 0
                    && remaining < original_fuel[index]
                    && program.original_deadline() == deadlines[index],
                "interleaving renewed original fuel or deadline"
            );
            original_fuel[index] = remaining;
        }
        Ok(())
    }
    .await;
    // Preserve every original IO Join through physical cleanup on all failures.
    for program in &mut programs {
        program.retire();
    }
    drop(programs);
    let mut cleanup: Result<()> = Ok(());
    for io in &ios {
        if let Err(error) = io.retire_and_wait().await {
            cleanup = Err(error.into());
        }
    }
    drop((ios, snapshots, owners));
    crate::reap_allocation_owners()?;
    ensure!(
        account.snapshot()?.charged_bytes == 0,
        "state proof retained DATA"
    );
    ensure!(
        control.usage().count(ControlKind::Task) == baseline_tasks
            && control.usage().count(ControlKind::Queue) == 0,
        "state proof did not finish original native task/queue cleanup"
    );
    cleanup?;
    operation?;
    serde_json::to_writer(
        create_output(&evidence.join("report.json"))?,
        &json!({"format":"tobkiri.native-input-stack-state-observation/1",
            "component_sha256":component_hash.as_str(),"simultaneous_stores":2,
            "distinct_native_invocations_same_account":true,"distinct_snapshot_sources":true,
            "fixture_bytes_equal":true,"run_store_order":[1,0,1],
            "invalid_init_atomicity":true,"reinit_refused_first_state_preserved":true,
            "original_deadlines_and_fuel_preserved":true,"physical_cleanup_succeeded":true,
            "shared_memory_limit_bytes":tobkiri_gate::NATIVE_SETUP_MEMORY_BYTES,
            "run_before_init_exact_error_proven":false,
            "whole_setup_compatible":false,"production_enabled":false}),
    )?;
    Ok(())
}
