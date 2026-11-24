//! Actual Pack factory plus controlled native Host causes, not native IO proof.
//! Fresh original observations stay outside every component/factory input.
use super::{MESSAGE, project};
use crate::bootstrap_runtime::actual_ci::{SelectedRuntime, selected};
use crate::bootstrap_runtime::denied_actual_ci::ObservedSource;
use crate::{
    BootstrapDenied, BootstrapDeniedFailure, BootstrapDeniedRaise, NativeComponentPreparation,
    OriginalAuthorityCause, OriginalAuthorityError, OriginalAuthorityIoError,
    OriginalAuthorityStage,
};
use anyhow::{Context, Result, bail, ensure};
use serde_json::{Value, json};
use std::{
    collections::BTreeSet,
    fs::File,
    path::PathBuf,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
};
use tobkiri_canon::{CoreBuffer, Digest};
use tobkiri_core::RuntimeOwner;
use tobkiri_gate::{CapacityKind, NativeSetupInvocation};

const FORMAT: &str = "io.tobkiri.actual-recovery-factory-observations.v1";
const CASES: [&str; 8] = [
    "secure-message",
    "active-base",
    "active-integrity",
    "active-lock-timeout",
    "secure-chained",
    "active-chained",
    "registry-integrity",
    "registry-conflict",
];
const EXCLUDED: [&str; 2] = ["timeout-message", "timeout-errno"];
const OUTPUT_BOUND: usize = 512 * 1024;

fn class(name: &str) -> Result<(&'static str, bool)> {
    Ok(match name {
        "secure-message" | "secure-chained" => (super::SECURE, false),
        "active-base" => (super::ACTIVE, false),
        "active-integrity" | "active-chained" => (super::INTEGRITY, false),
        "active-lock-timeout" => (super::LOCK_TIMEOUT, false),
        "registry-integrity" => (
            "core_runtime.profile_definition_store_v4.ProfileDefinitionStoreIntegrityError",
            true,
        ),
        "registry-conflict" => (
            "core_runtime.profile_definition_store_v4.ProfileDefinitionStoreConflict",
            true,
        ),
        _ => bail!("case is outside the fixed native recovery caller inputs"),
    })
}

fn native_input(name: &str, message: &str) -> Result<OriginalAuthorityError> {
    let (class, raw) = class(name)?;
    if raw {
        return Ok(OriginalAuthorityError {
            stage: OriginalAuthorityStage::Source,
            class,
            message: message.to_owned(),
            cause: None,
            context: None,
            suppressed: false,
        });
    }
    let chained = matches!(name, "secure-chained" | "active-chained");
    let cause = if chained {
        // Controlled native carrier, NOT an actual read failure. The helper
        // obtains its real OS strerror; no expected/Display text is parsed.
        let error = std::io::Error::from_raw_os_error(libc::EACCES);
        Some(Box::new(OriginalAuthorityCause::Io(
            OriginalAuthorityIoError {
                kind: error.kind(),
                errno: error.raw_os_error(),
                filename: Some("fixture-entry".to_owned()),
                context: None,
            },
        )))
    } else {
        None
    };
    Ok(OriginalAuthorityError {
        stage: OriginalAuthorityStage::Source,
        class: crate::bootstrap_review::DENIED,
        message: MESSAGE.to_owned(),
        cause: Some(OriginalAuthorityCause::Python {
            class,
            message: message.to_owned(),
            cause,
            context: None,
            suppressed: chained,
        }),
        context: None,
        suppressed: true,
    })
}

fn capacity(invocation: &NativeSetupInvocation) -> Result<Value> {
    let actual = invocation.capacity_snapshot()?;
    Ok(json!({"bytes":actual.charged_bytes,"reservations":actual.reservations}))
}

#[tokio::test(flavor = "current_thread")]
#[ignore = "requires a fresh original recovery summary and the actual original component"]
async fn actual_recovery_factory_driver() -> Result<()> {
    ensure!(
        std::env::var("GITHUB_ACTIONS").as_deref() == Ok("true")
            && std::env::var("TOBKIRI_TEST_NATIVE_HARNESS_CI").as_deref() == Ok("1")
    );
    let manifest = PathBuf::from(
        std::env::var_os("TOBKIRI_ORIGINAL_RECOVERY_ERROR_FIXTURES")
            .context("fresh original recovery inputs required")?,
    );
    let bytes = std::fs::read(&manifest)?;
    ensure!(bytes.len() <= 2 * 1024 * 1024);
    let fixtures: Value = serde_json::from_slice(&bytes)?;
    ensure!(
        fixtures["format"] == "io.tobkiri.original-recovery-error-reference.v1"
            && fixtures["original_head"] == "1cf025f1eeab31a3daa2861b1f26a025c6954b6e"
            && fixtures["case_count"] == 10
    );
    let cases = fixtures["cases"]
        .as_array()
        .context("fixed inputs required")?;
    ensure!(cases.len() == 10);
    ensure!(
        cases
            .iter()
            .filter_map(|case| case["name"].as_str())
            .collect::<BTreeSet<_>>()
            == CASES.into_iter().chain(EXCLUDED).collect::<BTreeSet<_>>()
    );
    // These fields are operation inputs, independent of expected Host results.
    for name in CASES {
        let case = cases.iter().find(|case| case["name"] == name).unwrap();
        ensure!(case["input"]["class_case"] == name);
        let args = case["input"]["args"]
            .as_array()
            .context("fixed args required")?;
        ensure!(args.len() == 1 && args[0].as_str().is_some_and(|s| s.len() <= 4096));
    }
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
    let component = PathBuf::from(
        std::env::var_os("TOBKIRI_TEST_NATIVE_HARNESS_COMPONENT")
            .context("actual component required")?,
    );
    ensure!(component.is_absolute() && component.canonicalize()? == component);
    let digest = Digest::parse(&format!(
        "sha256:{}",
        std::env::var("TOBKIRI_TEST_NATIVE_HARNESS_COMPONENT_SHA256")?
    ))?;
    let scratch = tempfile::tempdir()?;
    let owner_path = scratch
        .path()
        .canonicalize()?
        .join("actual-recovery-factory");
    let owner = RuntimeOwner::open(&owner_path)?;
    let invocation = owner.begin_native_setup()?;
    let deadline = invocation.deadline()?;
    let preparation = NativeComponentPreparation::new(invocation.clone(), source)?;
    let prepared = preparation.compile(File::open(&component)?, &digest)?;
    ensure!(prepared.observation().deadline == deadline && invocation.deadline()? == deadline);
    let mut runtime = match prepared.initialize(&selected::INDEX).await? {
        Ok(value) => value,
        Err(_) => bail!("actual recovery factory initialization refused"),
    };
    let initial_fuel = runtime.fuel_remaining()?;
    let initial_inspections = runtime.inspection_attempts();
    let mut previous_fuel = initial_fuel;
    let mut factory_calls = 0;
    // Fixed stack holders: receiving a real output cannot need Vec growth.
    // Every result/failure and input graph envelope survives actual finish.
    let mut retained: [Option<BootstrapDenied>; 6] = std::array::from_fn(|_| None);
    let mut failures: [Option<BootstrapDeniedFailure>; 6] = std::array::from_fn(|_| None);
    let mut inputs: [Option<[CoreBuffer; 3]>; 6] = std::array::from_fn(|_| None);
    let mut observed = Vec::new();
    let mut references = BTreeSet::new();
    let mut scope = None;
    let execution: Result<()> = async {
        for name in CASES {
            let case = cases.iter().find(|case| case["name"] == name).unwrap();
            let message = case["input"]["args"][0].as_str().unwrap();
            let fuel_before = runtime.fuel_remaining()?;
            let projected = {
                // Prepay the ONLY native-owned String/Box carrier before
                // constructing it. Two message copies, two cause nodes and
                // fixed filename/header are bounded here. This same account
                // reservation drops after the error, BEFORE any factory await.
                let bound = message
                    .len()
                    .checked_mul(2)
                    .and_then(|n| n.checked_add(MESSAGE.len() + "fixture-entry".len() + 1024))
                    .and_then(|n| n.checked_add(std::mem::size_of::<OriginalAuthorityError>()))
                    .and_then(|n| n.checked_add(std::mem::size_of::<OriginalAuthorityCause>() * 2))
                    .context("controlled native source funding overflow")?;
                let _owned = invocation.reserve(CapacityKind::VfsBuffer, bound as u64)?;
                let native = native_input(name, message)?;
                let graph = project(&native, &invocation, runtime.data_budget())?;
                if class(name)?.1 {
                    ensure!(
                        graph.is_none(),
                        "raw registry error became a factory request"
                    );
                    ensure!(runtime.fuel_remaining()? == fuel_before);
                    observed.push(json!({"name":name,"outcome":"raw-error",
                        "factory_called":false,"raw_error":{"class":native.class,
                        "message":native.message,"args":[native.message]}}));
                } else {
                    ensure!(
                        graph.is_some(),
                        "fixed native recovery cause was not projected"
                    );
                }
                graph
                // native dies before _owned here, including every `?` path.
            };
            let Some(graph) = projected else {
                ensure!(runtime.inspection_attempts() == initial_inspections);
                ensure!(invocation.deadline()? == deadline);
                continue;
            };
            let index = factory_calls;
            inputs[index] = Some([
                graph.code().clone(),
                graph.message().clone(),
                graph.details().clone(),
            ]);
            factory_calls += 1;
            match runtime
                .denied(MESSAGE, BootstrapDeniedRaise::ExplicitFrom(graph))
                .await?
            {
                Ok(value) => retained[index] = Some(value),
                Err(error) => {
                    failures[index] = Some(error);
                    let failure = failures[index].as_ref().unwrap();
                    bail!(
                        "actual factory failed at {:?}: {}",
                        failure.stage(),
                        failure.error().code().as_str()?
                    );
                }
            }
            let actual = retained[index].as_ref().unwrap();
            let cause = actual.cause().context("actual Host cause required")?;
            let context = actual.context().context("actual Host context required")?;
            ensure!(std::ptr::eq(cause, context) && actual.suppress_context());
            ensure!(actual.opaque_cause().is_none() && actual.opaque_context().is_none());
            let input = inputs[index].as_ref().unwrap();
            ensure!(cause.code().as_bytes() == input[0].as_bytes());
            ensure!(cause.message().as_bytes() == input[1].as_bytes());
            ensure!(cause.details().as_bytes() == input[2].as_bytes());
            let result: Value = serde_json::from_slice(actual.result().as_bytes())?;
            let view: Value = serde_json::from_slice(actual.view().as_bytes())?;
            ensure!(result["format"] == "io.tobkiri.native-value-result.v1");
            ensure!(view["format"] == "io.tobkiri.native-value-view.v1");
            let current_scope = result["scope"].clone();
            if let Some(scope) = &scope {
                ensure!(scope == &current_scope);
            } else {
                scope = Some(current_scope.clone());
            }
            ensure!(view["scope"] == current_scope && view["root"] == result["root"]);
            let object = result["root"]["object"]
                .as_str()
                .context("actual issued ref required")?;
            ensure!(references.insert(object.to_owned()));
            ensure!(result["owned"] == json!([object]));
            let nodes = view["nodes"]
                .as_array()
                .context("actual factory view required")?;
            ensure!(nodes.len() == 1);
            let node = &nodes[0];
            ensure!(node["ref"] == result["root"] && node["kind"] == "profile-resolution-denied");
            ensure!(node["fields"] == json!([]) && node["opaque_native_binding"] == false);
            ensure!(
                node["factory_denied"]
                    == json!({"args":[MESSAGE],"cause":null,
                "context":null,"suppress_context":false})
            );
            let details: Value = serde_json::from_slice(cause.details().as_bytes())?;
            observed.push(json!({"name":name,"outcome":"denied","factory_called":true,
                "factory_kind":node["kind"],"factory_denied":node["factory_denied"],
                "native_cause":{"code":cause.code().as_str()?,"message":cause.message().as_str()?,
                    "details":details},"host_cause_context_same":std::ptr::eq(cause,context),
                "host_suppressed":actual.suppress_context()}));
            let remaining = runtime.fuel_remaining()?;
            ensure!(remaining < previous_fuel && remaining < fuel_before);
            ensure!(runtime.inspection_attempts() == initial_inspections);
            ensure!(invocation.deadline()? == deadline);
            previous_fuel = remaining;
        }
        ensure!(observed.len() == 8 && factory_calls == 6 && references.len() == 6);
        Ok(())
    }
    .await;
    // Capture fallible probes without returning before the real Store finish.
    let before_finish = capacity(&invocation);
    let final_fuel = runtime.fuel_remaining();
    let final_inspections = runtime.inspection_attempts();
    let deadline_preserved = invocation.deadline().map(|value| value == deadline);
    let cleanup = runtime.finish().await;
    let after_finish = capacity(&invocation);
    drop(retained);
    drop(failures);
    drop(inputs);
    let after_data = capacity(&invocation);
    let reaped = tobkiri_wasm::reap_allocation_owners();
    let after_reap = capacity(&invocation);
    let before_finish = before_finish?;
    let after_finish = after_finish?;
    let after_data = after_data?;
    let reaped = reaped?;
    let after_reap = after_reap?;
    let cleanup_verified = cleanup.is_ok()
        && after_reap["bytes"] == 0
        && after_reap["reservations"] == 0
        && source_dropped.load(Ordering::Acquire);
    drop(invocation);
    drop(owner);
    let reopened = RuntimeOwner::open(&owner_path)?;
    drop(reopened);
    let output = json!({"format":FORMAT,"case_count":observed.len(),
        "cases":observed,"excluded_cases":EXCLUDED,
        "same_store":true,"budget_resets":0,"original_deadline_preserved":deadline_preserved?,
        "factory_calls":factory_calls,"fresh_factory_refs":references.len(),
        "inspection_attempts":{"before":initial_inspections,"after":final_inspections},
        "fuel":{"before":initial_fuel,"after":final_fuel.as_ref().ok()},
        "execution_matched_branch":execution.is_ok(),"cleanup_verified":cleanup_verified,
        "source_destructor_reached":source_dropped.load(Ordering::Acquire),
        "physical_owner_reopened":true,
        "capacity":{"before_finish":before_finish,"after_finish":after_finish,
            "after_data":after_data,"allocator_records_reaped":reaped,"after_reap":after_reap},
        "qualification":false,"production_enabled":false,"native_io_origin_proven":false,
        "scope":"six actual same-Store Pack factories with controlled native Host causes; two raw registry typed errors; no catalog body, actual read failure, Guest custom cause object, factory throw, journal recovery, migration, GUI or full ten-case qualification"});
    let bytes = serde_json::to_vec_pretty(&output)?;
    ensure!(bytes.len() <= OUTPUT_BOUND);
    std::fs::write(
        manifest.with_file_name("native-recovery-observations.json"),
        bytes,
    )?;
    cleanup?;
    execution?;
    ensure!(cleanup_verified);
    ensure!(final_fuel? < initial_fuel && previous_fuel < initial_fuel);
    ensure!(final_inspections == initial_inspections);
    Ok(())
}
