//! Actual original getter observation only; controlled fault fixtures are separate.
use super::actual_ci::{SelectedRuntime, selected};
use super::denied_actual_ci::ObservedSource;
use super::*;
use crate::{BootstrapBuiltinKind, BootstrapIdentityOutcome, NativeComponentPreparation};
use anyhow::ensure;
use serde_json::{Value, json};
use std::{
    fs::File,
    path::PathBuf,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
};
use tobkiri_canon::Digest;
use tobkiri_core::RuntimeOwner;

#[tokio::test(flavor = "current_thread")]
#[ignore = "requires a freshly built actual component and independent original identity reference"]
async fn original_getter_driver() -> Result<()> {
    ensure!(
        std::env::var("GITHUB_ACTIONS").as_deref() == Ok("true")
            && std::env::var("TOBKIRI_TEST_NATIVE_HARNESS_CI").as_deref() == Ok("1")
    );
    let manifest = PathBuf::from(
        std::env::var_os("TOBKIRI_ORIGINAL_BOOTSTRAP_IDENTITY_REFERENCE")
            .context("fresh original identity reference required")?,
    );
    let bytes = std::fs::read(&manifest)?;
    ensure!(bytes.len() <= 2 * 1024 * 1024);
    let reference: Value = serde_json::from_slice(&bytes)?;
    ensure!(
        reference["format"] == "io.tobkiri.original-bootstrap-identity-reference.v1"
            && reference["original_head"] == "1cf025f1eeab31a3daa2861b1f26a025c6954b6e"
            && reference["python"] == "3.13.16"
            && reference["reference_checks_passed"] == true
    );
    let expected = reference["unmodified_default_getter_before_and_after"]
        .as_str()
        .context("actual unmodified original getter observation required")?;
    ensure!(expected == "defaults");
    let root = PathBuf::from(
        std::env::var_os("TOBKIRI_TEST_NATIVE_HARNESS_RUNTIME_ROOT")
            .context("actual selected runtime required")?,
    );
    ensure!(root.is_absolute() && root.canonicalize()? == root);
    let source_dropped = Arc::new(AtomicBool::new(false));
    let source = ObservedSource::new(
        SelectedRuntime {
            _directory: File::open(&root)?,
            root,
            retired: AtomicBool::new(false),
            process: std::process::id(),
        },
        source_dropped.clone(),
    );
    let scratch = tempfile::tempdir()?;
    let owner_path = scratch.path().canonicalize()?.join("native-identity");
    let owner = RuntimeOwner::open(&owner_path)?;
    let invocation = owner.begin_native_setup()?;
    let deadline = invocation.deadline()?;
    let preparation = NativeComponentPreparation::new(invocation.clone(), source)?;
    let component = PathBuf::from(
        std::env::var_os("TOBKIRI_TEST_NATIVE_HARNESS_COMPONENT")
            .context("actual component required")?,
    );
    ensure!(component.is_absolute() && component.canonicalize()? == component);
    let digest = Digest::parse(&format!(
        "sha256:{}",
        std::env::var("TOBKIRI_TEST_NATIVE_HARNESS_COMPONENT_SHA256")?
    ))?;
    let prepared = preparation.compile(File::open(component)?, &digest)?;
    ensure!(prepared.observation().deadline == deadline);
    let mut runtime = match prepared.initialize(&selected::INDEX).await? {
        Ok(value) => value,
        Err(error) => bail!("actual initialization refused: {}", error.code().as_str()?),
    };
    let initial_fuel = runtime.fuel_remaining()?;
    let initial_inspections = runtime.inspection_attempts();
    // The outcome must be retained before every fallible codec/observation.
    // No expected result is passed to the guest or substituted for its getter.
    let mut retained = None;
    let mut observation = None;
    let execution: Result<()> = async {
        retained = Some(runtime.observe_bootstrap_profile_id().await?);
        let actual = match retained.as_ref().context("actual outcome required")? {
            BootstrapIdentityOutcome::Returned(value) => value,
            BootstrapIdentityOutcome::GetterRaised(_) => bail!("actual default getter raised"),
            BootstrapIdentityOutcome::Refused(_) => bail!("actual default getter was refused"),
        };
        ensure!(
            actual.builtin_kind() == BootstrapBuiltinKind::Str
                && actual.string_length() == Some(expected.chars().count())
        );
        let result: Value = serde_json::from_slice(actual.result().as_bytes())?;
        let view: Value = serde_json::from_slice(actual.view().as_bytes())?;
        ensure!(
            result["format"] == "io.tobkiri.native-value-result.v1"
                && view["format"] == "io.tobkiri.native-bootstrap-identity-view.v1"
                && result["scope"] == view["scope"]
                && result["root"] == view["root"]
                && view["origin"] == "getter-returned"
                && view["kind"] == "str"
                && view["registered_builtin_kind"] == "str"
                && view["exception"].is_null()
        );
        let expected_codepoints: Vec<u32> = expected.chars().map(u32::from).collect();
        ensure!(view["string"]["codepoints"] == json!(expected_codepoints));
        let remaining_fuel = runtime.fuel_remaining()?;
        ensure!(
            remaining_fuel < initial_fuel
                && invocation.deadline()? == deadline
                && runtime.inspection_attempts() == initial_inspections
        );
        observation = Some(json!({"origin":view["origin"],"kind":view["kind"],
            "string":view["string"],"same_store_scope":result["scope"],
            "initial_fuel":initial_fuel,"remaining_fuel":remaining_fuel,
            "original_deadline_preserved":true,"budget_resets":0,"extra_inspections":0}));
        Ok(())
    }
    .await;
    let before_finish = invocation.capacity_snapshot()?;
    let cleanup = runtime.finish().await;
    let after_finish = invocation.capacity_snapshot()?;
    drop(retained);
    let after_carriers = invocation.capacity_snapshot()?;
    let allocator_records_reaped = tobkiri_wasm::reap_allocation_owners()?;
    let after_reap = invocation.capacity_snapshot()?;
    let output = json!({"format":"io.tobkiri.actual-bootstrap-identity-observation.v1",
        "observation":observation,"getter_executed_and_matched":execution.is_ok(),
        "cleanup_verified":cleanup.is_ok() && after_reap.charged_bytes == 0
            && after_reap.reservations == 0 && source_dropped.load(Ordering::Acquire),
        "cleanup":{"before_finish":{"bytes":before_finish.charged_bytes,"reservations":before_finish.reservations},
            "after_finish":{"bytes":after_finish.charged_bytes,"reservations":after_finish.reservations},
            "after_carriers":{"bytes":after_carriers.charged_bytes,"reservations":after_carriers.reservations},
            "allocator_records_reaped":allocator_records_reaped,
            "after_reap":{"bytes":after_reap.charged_bytes,"reservations":after_reap.reservations},
            "source_destructor_reached":source_dropped.load(Ordering::Acquire)},
        "qualification":false,"production_enabled":false,
        "scope":"actual original default getter only; no controlled fault, canonical validator, recovery or GUI qualification"});
    std::fs::write(
        manifest.with_file_name("actual-getter-observations.json"),
        serde_json::to_vec_pretty(&output)?,
    )?;
    cleanup?;
    execution?;
    ensure!(
        after_reap.charged_bytes == 0
            && after_reap.reservations == 0
            && source_dropped.load(Ordering::Acquire)
    );
    drop(invocation);
    drop(owner);
    drop(RuntimeOwner::open(owner_path)?);
    Ok(())
}
