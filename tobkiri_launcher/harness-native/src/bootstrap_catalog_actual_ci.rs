//! Actual component catalog reachpoint; CI resource fixtures, not GUI adoption.
use super::actual_ci::{SelectedRuntime, selected};
use super::denied_actual_ci::ObservedSource;
use super::*;
use crate::{
    BootstrapCatalogSource, BootstrapReviewFailure, BootstrapReviewOptions,
    BootstrapReviewReachpoint, OriginalAuthorityError, OriginalAuthorityOwner,
};
use anyhow::ensure;
use serde_json::{Value, json};
use std::{
    fs::{File, OpenOptions},
    os::fd::AsRawFd,
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
};
use tobkiri_canon::Digest;
use tobkiri_core::RuntimeOwner;

struct CatalogSource {
    source: ObservedSource,
    user_data: PathBuf,
}

impl BootstrapInputOwner for CatalogSource {
    fn original_module_metadata_policy(&self) -> Option<&'static crate::ModuleMetadataPolicy> {
        self.source.original_module_metadata_policy()
    }
    fn with_roots<R>(
        &self,
        f: impl for<'a> FnOnce(crate::BootstrapRoots<'a>) -> R,
    ) -> std::result::Result<R, NativeDataIoError> {
        self.source.with_roots(f)
    }
    fn check_live(&self) -> std::result::Result<(), NativeDataIoError> {
        self.source.check_live()
    }
}

impl OriginalAuthorityOwner for CatalogSource {
    fn with_original_user_data<R>(
        &self,
        f: impl FnOnce(&Path) -> R,
    ) -> std::result::Result<R, OriginalAuthorityError> {
        self.check_original_owner()?;
        Ok(f(&self.user_data))
    }
    fn check_original_owner(&self) -> std::result::Result<(), OriginalAuthorityError> {
        self.check_live()
            .map_err(|_| OriginalAuthorityError::unavailable())
    }
}

impl crate::BootstrapReviewOwner for CatalogSource {
    fn with_original_review_user_data<R>(
        &self,
        f: impl FnOnce(&Path) -> R,
    ) -> std::result::Result<R, OriginalAuthorityError> {
        self.with_original_user_data(f)
    }
    fn with_original_catalog_source<R>(
        &self,
        f: impl for<'a> FnOnce(BootstrapCatalogSource<'a>) -> R,
    ) -> std::result::Result<R, OriginalAuthorityError> {
        self.check_original_owner()?;
        self.with_roots(|roots| {
            f(BootstrapCatalogSource {
                runtime: roots.runtime,
                environment: "production",
                configured_app: None,
            })
        })
        .map_err(|_| OriginalAuthorityError::unavailable())
    }
}

fn lock_is_free(root: &Path) -> Result<bool> {
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .open(root.join("profiles/.bootstrap-publication.lock"))?;
    let result = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
    if result == 0 {
        ensure!(unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_UN) } == 0);
        return Ok(true);
    }
    let code = std::io::Error::last_os_error().raw_os_error();
    ensure!(code == Some(libc::EWOULDBLOCK) || code == Some(libc::EAGAIN));
    Ok(false)
}

fn diagnostic_text(value: &CoreBuffer) -> Value {
    let text = String::from_utf8_lossy(value.as_bytes());
    json!({"text":text.chars().take(4096).collect::<String>(),
        "source_bytes":value.len(),"truncated":text.chars().count()>4096})
}

#[tokio::test(flavor = "current_thread")]
#[ignore = "requires actual component and independent pinned original CI fixtures"]
async fn original_catalog_component_driver() -> Result<()> {
    ensure!(
        std::env::var("GITHUB_ACTIONS").as_deref() == Ok("true")
            && std::env::var("TOBKIRI_TEST_NATIVE_HARNESS_CI").as_deref() == Ok("1")
    );
    let fixtures_path = PathBuf::from(
        std::env::var_os("TOBKIRI_ORIGINAL_CATALOG_PREFIX_FIXTURES")
            .context("fresh original catalog fixtures required")?,
    );
    let data = std::fs::read(&fixtures_path)?;
    ensure!(data.len() <= 2 * 1024 * 1024);
    let fixtures: Value = serde_json::from_slice(&data)?;
    ensure!(
        fixtures["format"] == "io.tobkiri.original-catalog-prefix-reference.v1"
            && fixtures["original_head"] == "1cf025f1eeab31a3daa2861b1f26a025c6954b6e"
            && fixtures["case_count"] == 9
    );
    let rows = fixtures["cases"]
        .as_array()
        .context("closed original cases required")?;
    ensure!(rows.len() == 9);
    let fixture = rows
        .iter()
        .find(|v| v["name"] == "packaged-valid")
        .context("normal actual catalog fixture required")?;
    let input_path = PathBuf::from(
        fixture["native_input"]
            .as_str()
            .context("native input required")?,
    );
    ensure!(
        input_path
            == fixtures_path
                .parent()
                .context("fixture directory required")?
                .join("packaged-valid/native/input.json")
    );
    let input_bytes = std::fs::read(&input_path)?;
    ensure!(input_bytes.len() <= 16 * 1024);
    let input: Value = serde_json::from_slice(&input_bytes)?;
    let root = PathBuf::from(
        input["runtime"]
            .as_str()
            .context("fixed runtime required")?,
    );
    let user_data = PathBuf::from(
        input["user_data"]
            .as_str()
            .context("fixed user data required")?,
    );
    ensure!(
        input["case"] == "packaged-valid"
            && input["resource_fixture_only"] == true
            && input["platform_artifact_directory_only"] == true
            && input["environment"]["RUMI_ENVIRONMENT"] == "production"
            && input["environment"]["RUMI_APP_DIR"].is_null()
    );
    ensure!(
        root == input_path
            .parent()
            .context("native directory required")?
            .join("runtime")
            && user_data
                == input_path
                    .parent()
                    .context("native directory required")?
                    .join("user-data")
    );
    ensure!(
        root.is_absolute()
            && root.canonicalize()? == root
            && user_data.is_absolute()
            && user_data.canonicalize()? == user_data
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
    let dropped = Arc::new(AtomicBool::new(false));
    let source = CatalogSource {
        source: ObservedSource::new(
            SelectedRuntime {
                _directory: File::open(&root)?,
                root,
                retired: AtomicBool::new(false),
                process: std::process::id(),
            },
            dropped.clone(),
        ),
        user_data: user_data.clone(),
    };
    let scratch = tempfile::tempdir()?;
    let owner_path = scratch.path().canonicalize()?.join("actual-catalog-owner");
    let owner = RuntimeOwner::open(&owner_path)?;
    // Preparation is inside this operation's one original absolute clock.
    let invocation = owner.begin_native_setup()?;
    let deadline = invocation.deadline()?;
    let preparation = crate::NativeComponentPreparation::new(invocation.clone(), source)?;
    let prepared = preparation.compile(File::open(&component)?, &digest)?;
    ensure!(prepared.observation().deadline == deadline);
    let mut runtime = match prepared.initialize(&selected::INDEX).await? {
        Ok(value) => value,
        Err(_) => bail!("actual catalog initialization refused"),
    };
    let fuel_before = runtime.fuel_remaining()?;
    let mut held_failure = None;
    let mut held_success = None;
    let mut cleanup_probe = None;
    let mut observed = None;
    let execution: Result<()> = async {
        let error = match runtime
            // Preserve this constructor-entry proof while normal listing
            // continues into the independently tested definition interval.
            .setup_review_catalog_prefix(&BootstrapReviewOptions::default())
            .await?
        {
            Ok(value) => {
                held_success = Some(value);
                bail!("unbound catalog returned success");
            }
            Err(error) => error,
        };
        let diagnostic = if error.owns_original_diagnostics() {
            Some(error)
        } else {
            held_failure = Some(error);
            None
        };
        let error = held_failure
            .as_ref()
            .or(diagnostic.as_ref())
            .context("actual outcome required")?;
        cleanup_probe = runtime.original_review_uncertain_cleanup();
        if let BootstrapReviewFailure::ObservationRefused(failure) = error {
            let native = failure.error();
            let catalog_progress = runtime
                .work
                .get()
                .catalog
                .try_lock()
                .ok()
                .and_then(|current| current.as_ref().map(|io| io.ci_progress()));
            observed = Some(json!({"outcome":"observation-refused",
                "stage":format!("{:?}",failure.stage()),"origin":failure.origin(),
                "catalog_progress_copy_only":catalog_progress,
                "native_code":diagnostic_text(native.code()),
                "native_message":diagnostic_text(native.message()),
                "native_details":native.details().map(diagnostic_text),
                "qualification":false}));
        }
        ensure!(
            matches!(
                error,
                BootstrapReviewFailure::NotBound(
                    BootstrapReviewReachpoint::ProfileDefinitionStoreNotBound
                )
            ),
            "actual catalog did not reach the original constructor entry: {error:?}"
        );
        let actual = &runtime
            .review
            .as_ref()
            .context("same runner review required")?
            .actual;
        ensure!(
            actual.host_root.is_some() && actual.bundle_root.is_some() && actual.catalog.is_some(),
            "real same-Store roots and loader result must be retained"
        );
        let fuel_after = runtime.fuel_remaining()?;
        ensure!(fuel_after < fuel_before && invocation.deadline()? == deadline);
        ensure!(cleanup_probe.is_some() && !lock_is_free(&user_data)?);
        observed = Some(
            json!({"outcome":"integration-boundary", "stage":"profile-definition-store-entry",
            "fuel_before":fuel_before,"fuel_after":fuel_after,"budget_resets":0,
            "same_original_deadline":true,"journal_lock_held_before_finish":true,
            "actual_host_root_retained":true,"actual_bundle_root_retained":true,
            "actual_catalog_retained":true}),
        );
        drop(diagnostic); // Owned native diagnostic only; Guest DATA stays outside.
        Ok(())
    }
    .await;
    let before = invocation.capacity_snapshot();
    let cleanup = runtime.finish().await;
    let after_finish = invocation.capacity_snapshot();
    let verified = cleanup_probe.as_ref().is_some_and(|v| v.is_verified());
    let lock_released = lock_is_free(&user_data)?;
    drop(held_failure);
    drop(held_success);
    drop(cleanup_probe);
    let before = before?;
    let after_finish = after_finish?;
    let after_data = invocation.capacity_snapshot()?;
    let reaped = tobkiri_wasm::reap_allocation_owners()?;
    let after_reap = invocation.capacity_snapshot()?;
    let output = json!({"format":"io.tobkiri.actual-catalog-prefix-observations.v1",
        "name":"packaged-valid","observation":observed,"execution_matched_branch":execution.is_ok(),
        "execution_error":execution.as_ref().err().map(ToString::to_string),
        "cleanup_verified":cleanup.is_ok() && verified,"journal_lock_released_after_finish":lock_released,
        "source_destructor_reached":dropped.load(Ordering::Acquire),
        "capacity":{"before_finish":{"bytes":before.charged_bytes,"reservations":before.reservations},
        "after_finish":{"bytes":after_finish.charged_bytes,"reservations":after_finish.reservations},
        "after_data":{"bytes":after_data.charged_bytes,"reservations":after_data.reservations},
        "reaped":reaped,"after_reap":{"bytes":after_reap.charged_bytes,"reservations":after_reap.reservations}},
        "qualification":false,"production_enabled":false,
        "scope":"one actual catalog operation with controlled CI resource owner and empty artifact directory; no constructor, migration, Authority, actual installed executable, GUI or full9 native qualification"});
    std::fs::write(
        fixtures_path.with_file_name("actual-catalog-observations.json"),
        serde_json::to_vec_pretty(&output)?,
    )?;
    cleanup?;
    execution?;
    ensure!(
        verified
            && lock_released
            && dropped.load(Ordering::Acquire)
            && after_reap.charged_bytes == 0
            && after_reap.reservations == 0
    );
    drop(invocation);
    drop(owner);
    drop(RuntimeOwner::open(owner_path)?);
    Ok(())
}
