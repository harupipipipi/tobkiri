//! Independent actual-component factory comparison, not Setup/GUI adoption.
//! Expected results are never supplied to the component or its factory.
use super::actual_ci::{SelectedRuntime, selected};
use super::*;
use crate::{BootstrapDeniedRaise, NativeComponentPreparation};
use anyhow::ensure;
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
use tobkiri_canon::Digest;
use tobkiri_core::RuntimeOwner;

// Test-only witness after the real selected source's fields are destroyed.
// It observes destructor reachability, not an OS close-fault receipt.
struct SourceDropped(Arc<AtomicBool>);
impl Drop for SourceDropped {
    fn drop(&mut self) {
        self.0.store(true, Ordering::Release);
    }
}
pub(super) struct ObservedSource {
    source: SelectedRuntime,
    _dropped: SourceDropped,
}
impl ObservedSource {
    pub(super) fn new(source: SelectedRuntime, dropped: Arc<AtomicBool>) -> Self {
        Self {
            source,
            _dropped: SourceDropped(dropped),
        }
    }
}
impl BootstrapInputOwner for ObservedSource {
    fn original_module_metadata_policy(&self) -> Option<&'static crate::ModuleMetadataPolicy> {
        self.source.original_module_metadata_policy()
    }
    fn with_roots<R>(
        &self,
        roots: impl for<'a> FnOnce(crate::BootstrapRoots<'a>) -> R,
    ) -> std::result::Result<R, NativeDataIoError> {
        self.source.with_roots(roots)
    }
    fn check_live(&self) -> std::result::Result<(), NativeDataIoError> {
        self.source.check_live()
    }
}

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
    let selected_source = SelectedRuntime {
        _directory: File::open(&root)?,
        root,
        retired: AtomicBool::new(false),
        process: std::process::id(),
    };
    let source_dropped = Arc::new(AtomicBool::new(false));
    let source = ObservedSource::new(selected_source, source_dropped.clone());
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
    let capacity_observation = || -> Result<Value> {
        let capacity = invocation.capacity_snapshot()?;
        Ok(json!({"charged_bytes":capacity.charged_bytes,
            "reservations":capacity.reservations,
            "source_destructor_reached":source_dropped.load(Ordering::Acquire)}))
    };
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
                "cleanup_verified":false,
                "same_store":true,"budget_resets":0,"original_deadline_preserved":true,
                "cases":observed,
            }))?;
            ensure!(bytes.len() <= 2 * 1024 * 1024);
            std::fs::write(manifest.with_file_name("native-observations.json"), bytes)?;
        }
        Ok(())
    }
    .await;
    let before_finish = capacity_observation()?;
    let cleanup = runtime.finish().await;
    let after_finish = capacity_observation()?;
    drop(retained);
    drop(retained_failures);
    let after_carriers = capacity_observation()?;
    // Existing allocator custody deliberately retains its zero-byte record
    // until explicit reap proves there are no remaining Arc or Weak users.
    // Collect the real record after DATA drops; do not waive reservations=0.
    let allocator_records_reaped = tobkiri_wasm::reap_allocation_owners()?;
    let after_reap = capacity_observation()?;
    eprintln!(
        "CI_FACTORY_CAPACITY before_finish={before_finish} after_finish={after_finish} after_carriers={after_carriers} allocator_records_reaped={allocator_records_reaped} after_reap={after_reap}"
    );
    let observations_path = manifest.with_file_name("native-observations.json");
    if observations_path.is_file() {
        let bytes = std::fs::read(&observations_path)?;
        ensure!(bytes.len() <= 2 * 1024 * 1024);
        let mut output: Value = serde_json::from_slice(&bytes)?;
        output["cleanup"] = json!({"before_finish":before_finish,
            "after_finish":after_finish,"after_carriers":after_carriers,
            "allocator_records_reaped":allocator_records_reaped,"after_reap":after_reap});
        output["cleanup_verified"] = json!(
            cleanup.is_ok()
                && after_reap["charged_bytes"] == 0
                && after_reap["reservations"] == 0
                && source_dropped.load(Ordering::Acquire)
        );
        let bytes = serde_json::to_vec_pretty(&output)?;
        ensure!(bytes.len() <= 2 * 1024 * 1024);
        std::fs::write(observations_path, bytes)?;
    }
    cleanup?;
    execution?;
    let capacity = invocation.capacity_snapshot()?;
    ensure!(
        capacity.charged_bytes == 0 && capacity.reservations == 0,
        "factory allocations survived finish, carrier drop and actual custody reap: bytes={} reservations={}",
        capacity.charged_bytes,
        capacity.reservations
    );
    ensure!(
        source_dropped.load(Ordering::Acquire),
        "actual selected source did not drop"
    );
    ensure!(references.len() == cases.len() && previous_fuel < initial_fuel);
    drop(invocation);
    drop(owner);
    drop(RuntimeOwner::open(owner_path)?);
    Ok(())
}
