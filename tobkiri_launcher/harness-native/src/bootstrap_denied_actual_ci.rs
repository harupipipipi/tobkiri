//! Independent actual-component factory comparison, not Setup/GUI adoption.
//! Expected results are never supplied to the component or its factory.
use super::actual_ci::{SelectedRuntime, selected};
use super::*;
use crate::{BootstrapDeniedRaise, NativeComponentPreparation};
use anyhow::ensure;
use serde_json::{Value, json};
use std::{collections::BTreeSet, fs::File, path::PathBuf, sync::atomic::AtomicBool};
use tobkiri_canon::Digest;
use tobkiri_core::RuntimeOwner;

#[tokio::test(flavor = "current_thread")]
#[ignore = "requires this execution's fresh original factory fixtures and actual component"]
async fn original_reference_driver() -> Result<()> {
    ensure!(
        std::env::var("GITHUB_ACTIONS").as_deref() == Ok("true")
            && std::env::var("TOBKIRI_TEST_NATIVE_HARNESS_CI").as_deref() == Ok("1")
    );
    let manifest = PathBuf::from(
        std::env::var_os("TOBKIRI_ORIGINAL_DENIED_FACTORY_FIXTURES")
            .context("fresh original factory fixtures required")?,
    );
    let bytes = std::fs::read(&manifest)?;
    ensure!(bytes.len() <= 2 * 1024 * 1024);
    let fixtures: Value = serde_json::from_slice(&bytes)?;
    ensure!(fixtures["format"] == "io.tobkiri.original-denied-factory-fixtures.v2");
    ensure!(fixtures["original_head"] == "1cf025f1eeab31a3daa2861b1f26a025c6954b6e");
    ensure!(fixtures["python"] == "3.13.16" && fixtures["reference_checks_passed"] == true);
    let cases = fixtures["cases"]
        .as_array()
        .context("closed factory inputs required")?;
    let names = [
        "empty",
        "publication-identity",
        "bootstrap-canonical",
        "japanese",
        "nonbmp",
        "quotes-controls",
        "whitespace",
        "same-message-again",
    ];
    ensure!(cases.len() == names.len());
    ensure!(
        cases
            .iter()
            .filter_map(|case| case["name"].as_str())
            .collect::<BTreeSet<_>>()
            == names.into_iter().collect::<BTreeSet<_>>()
    );

    let root = PathBuf::from(
        std::env::var_os("TOBKIRI_TEST_NATIVE_HARNESS_RUNTIME_ROOT")
            .context("actual selected runtime required")?,
    );
    ensure!(root.is_absolute() && root.canonicalize()? == root);
    let source = SelectedRuntime {
        _directory: File::open(&root)?,
        root,
        retired: AtomicBool::new(false),
        process: std::process::id(),
    };
    let scratch = tempfile::tempdir()?;
    let owner_path = scratch.path().canonicalize()?.join("native-factory");
    let owner = RuntimeOwner::open(&owner_path)?;
    // The original absolute clock includes actual compilation/preparation.
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
    let prepared = preparation.compile(File::open(&component)?, &digest)?;
    ensure!(prepared.observation().deadline == deadline && invocation.deadline()? == deadline);
    let mut runtime = match prepared.initialize(&selected::INDEX).await? {
        Ok(runtime) => runtime,
        Err(error) => bail!(
            "actual factory initialization refused: {}",
            error.code().as_str()?
        ),
    };
    let initial_fuel = runtime.fuel_remaining()?;
    let initial_inspections = runtime.inspection_attempts();
    let mut previous_fuel = initial_fuel;
    let mut retained = Vec::new();
    let mut retained_failures = Vec::new();
    let mut observed = Vec::new();
    let mut references = BTreeSet::new();
    let mut scope = None;
    let execution: Result<()> = async {
        for case in cases {
            let name = case["name"].as_str().context("factory name required")?;
            let message = case["message"]
                .as_str()
                .context("factory message required")?;
            let actual = match runtime.denied(message, BootstrapDeniedRaise::Plain).await? {
                Ok(value) => value,
                Err(error) => {
                    retained_failures.push(error);
                    let error = retained_failures.last().expect("retained factory failure");
                    let stage = error.stage();
                    let code = error.error().code().as_str()?.to_owned();
                    // The source failure's CoreBuffers must survive finish,
                    // even though this successful-factory corpus will fail.
                    bail!("actual factory refused at {stage:?}: {code}");
                }
            };
            // Retain before any fallible observation: codec/comparison errors
            // must not destroy the funded factory result before Store finish.
            retained.push(actual);
            let actual = retained.last().expect("retained actual factory result");
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
                .context("real issued factory ref required")?;
            ensure!(
                references.insert(object.to_owned()),
                "factory reused a previous returned object"
            );
            ensure!(result["owned"] == json!([object]));
            let nodes = view["nodes"]
                .as_array()
                .context("actual factory view required")?;
            ensure!(nodes.len() == 1);
            let node = &nodes[0];
            ensure!(node["ref"] == result["root"]);
            ensure!(node["kind"] == "profile-resolution-denied");
            ensure!(node["fields"] == json!([]) && node["opaque_native_binding"] == false);
            ensure!(
                actual.cause().is_none()
                    && actual.context().is_none()
                    && !actual.suppress_context()
            );
            let row = json!({"name":name,"outcome":"factory-returned", "kind":node["kind"],
                "factory_denied":node["factory_denied"]});
            observed.push(row);
            let remaining = runtime.fuel_remaining()?;
            ensure!(
                remaining < previous_fuel
                    && invocation.deadline()? == deadline
                    && runtime.inspection_attempts() == initial_inspections,
                "factory call did not consume the original same-Store budget"
            );
            previous_fuel = remaining;
            let bytes = serde_json::to_vec_pretty(&json!({
                "format":"io.tobkiri.original-denied-factory-native-observations.v2",
                "native_comparison":true,"qualification":false,"production_enabled":false,
                "same_store":true,"budget_resets":0,"original_deadline_preserved":true,
                "cases":observed,
            }))?;
            ensure!(bytes.len() <= 2 * 1024 * 1024);
            std::fs::write(manifest.with_file_name("native-observations.json"), bytes)?;
        }
        Ok(())
    }
    .await;
    let cleanup = runtime.finish().await;
    drop(retained);
    drop(retained_failures);
    cleanup?;
    execution?;
    let capacity = invocation.capacity_snapshot()?;
    ensure!(capacity.charged_bytes == 0 && capacity.reservations == 0);
    ensure!(references.len() == cases.len() && previous_fuel < initial_fuel);
    drop(invocation);
    drop(owner);
    drop(RuntimeOwner::open(owner_path)?);
    Ok(())
}
