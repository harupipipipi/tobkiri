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

pub(super) fn lock_is_free(root: &Path) -> Result<bool> {
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

/// Only these actual review carriers own Read-funded native diagnostics.
/// Unknown/context/Guest carriers retain their existing post-finish custody.
pub(super) fn original_diagnostic_in_outer(mut error: &anyhow::Error) -> bool {
    loop {
        let current: &(dyn std::error::Error + Send + Sync + 'static) = error.as_ref();
        if current.is::<OriginalAuthorityError>() {
            return true;
        }
        let Some(failure) = current.downcast_ref::<BootstrapCleanupFailure>() else {
            return false;
        };
        // The actual review retire_failure adds only this Copy IO cleanup
        // error. Do not early-drop a wrapper with unknown/Guest cleanup DATA.
        let cleanup: &(dyn std::error::Error + Send + Sync + 'static) = failure.cleanup.as_ref();
        if !cleanup.is::<NativeDataIoError>() {
            return false;
        }
        error = &failure.original;
    }
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
        !cfg!(feature = "catalog-diagnostics-ci"),
        "unmeasured catalog gate requires the baseline Core configuration"
    );
    run_original_catalog_component_driver(false, false).await
}

#[tokio::test(flavor = "current_thread")]
#[ignore = "requires actual component and the pinned original recovery seed"]
async fn original_recovery_catalog_component_driver() -> Result<()> {
    ensure!(!cfg!(feature = "catalog-diagnostics-ci"));
    run_original_catalog_component_driver(false, true).await
}

#[cfg(feature = "catalog-diagnostics-ci")]
#[tokio::test(flavor = "current_thread")]
#[ignore = "requires explicitly derived schema-checkpoint artifact and CI Core configuration"]
async fn original_catalog_schema_diagnostic_driver() -> Result<()> {
    ensure!(
        std::env::var("TOBKIRI_TEST_NATIVE_HARNESS_SCHEMA_DIAGNOSTIC").as_deref() == Ok("1")
            && selected::CI_SCHEMA_TAG_COUNT == 69,
        "explicitly bound diagnostic source/index required"
    );
    // Diagnostic observation never turns the original catalog failure into
    // functional qualification, even if the measured call reaches its end.
    let outcome = run_original_catalog_component_driver(true, false).await;
    if let Err(error) = outcome {
        return Err(error.context("schema checkpoint diagnostic remains unqualified"));
    }
    bail!("schema checkpoint diagnostic remains unqualified; inspect bounded evidence")
}

#[cfg(feature = "catalog-diagnostics-ci")]
pub(super) fn write_schema_trace(
    path: &Path,
    snapshot: Option<schema_diagnostic_ci::Snapshot>,
    status: Option<schema_diagnostic_ci::Status>,
) -> Result<Value> {
    use sha2::{Digest as _, Sha256};
    use std::io::{Cursor, Write as _};
    let Some(packet) = snapshot else {
        return Ok(json!({"available":false,"qualification":false,
            "status_available":status.is_some(),
            "overflow":status.map(|s| s.overflow),
            "unavailable":status.map(|s| s.unavailable),
            "prepay_attempted":status.map(|s| s.prepay_attempted)}));
    };
    let data = packet.data();
    // Every row consists only of fixed ASCII keys/stages, u8 tags and a
    // maximum 20-digit u64. 512 header/trailer bytes plus 139 * 112 row bytes
    // fit in the one prepaid 16KiB output. Cursor over a fixed slice cannot
    // grow, even if this bound regresses. The owned packet funds both its
    // borrowed DATA and this buffer until the file write/hash have finished.
    const MAX_ENCODED_BYTES: usize = 512 + schema_diagnostic_ci::MAX_RECORDS * 112;
    ensure!(MAX_ENCODED_BYTES <= schema_diagnostic_ci::MAX_REPORT_JSON_BYTES);
    ensure!(usize::from(data.count) <= schema_diagnostic_ci::MAX_RECORDS);
    let mut bytes = Vec::<u8>::new();
    bytes.try_reserve_exact(schema_diagnostic_ci::MAX_REPORT_JSON_BYTES)?;
    ensure!(bytes.capacity() == schema_diagnostic_ci::MAX_REPORT_JSON_BYTES);
    bytes.resize(schema_diagnostic_ci::MAX_REPORT_JSON_BYTES, 0);
    let length = {
        let mut output = Cursor::new(bytes.as_mut_slice());
        write!(
            output,
            "{{\"format\":\"io.tobkiri.actual-schema-checkpoint-trace.v1\",\"count\":{},\"maximum_records\":{},\"schema_tag_count\":{},\"overflow\":{},\"unavailable\":{},\"attempt_limit\":{},\"attempts_remaining\":{},\"records\":[",
            data.count,
            data.maximum_records,
            data.schema_tag_count,
            data.overflow,
            data.unavailable,
            data.attempt_limit,
            data.attempts_remaining
        )?;
        let mut first = true;
        let mut rows = 0usize;
        for record in data.records.iter().flatten() {
            if !first {
                output.write_all(b",")?;
            }
            first = false;
            rows += 1;
            write!(
                output,
                "{{\"sequence\":{},\"stage\":\"{}\",\"schema_tag\":",
                record.sequence,
                record.stage.as_str()
            )?;
            match record.schema_tag {
                Some(tag) => write!(output, "{tag}")?,
                None => output.write_all(b"null")?,
            }
            output.write_all(b",\"fuel_remaining\":")?;
            match record.fuel_remaining {
                Some(fuel) => write!(output, "{fuel}")?,
                None => output.write_all(b"null")?,
            }
            output.write_all(b"}")?;
        }
        ensure!(rows == usize::from(data.count));
        output.write_all(b"],\"fuel_sample_boundary\":\"actual-import-construction-entry\",\"pure_check_schema_cost_proven\":false,\"qualification\":false}")?;
        usize::try_from(output.position())?
    };
    ensure!(length <= MAX_ENCODED_BYTES);
    std::fs::write(path, &bytes[..length])?;
    let digest = Sha256::digest(&bytes[..length]);
    // Only bounded file metadata escapes the funded packet, never the trace
    // array or an unfunded serde Value/Vec representation of its records.
    let metadata = json!({"available":true,"file":"actual-catalog-schema-trace.json",
        "bytes":length,"sha256":format!("{digest:x}"),"qualification":false});
    drop(bytes);
    drop(packet);
    Ok(metadata)
}

async fn run_original_catalog_component_driver(
    schema_diagnostic: bool,
    recovery: bool,
) -> Result<()> {
    #[cfg(not(feature = "catalog-diagnostics-ci"))]
    let _ = schema_diagnostic;
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
    // Copy-only reachpoint diagnostics. Counts do not establish successful
    // inspection, IO completion, owner admission or physical cleanup.
    let inspections_before_review = runtime.inspection_attempts();
    let mut held_failure = None;
    let mut held_success = None;
    let mut cleanup_probe = None;
    let mut observed = None;
    let mut post_review_copy = None;
    #[cfg(feature = "catalog-diagnostics-ci")]
    let mut schema_trace = None;
    let execution: Result<()> = async {
        let options = BootstrapReviewOptions::default();
        let outcome = if recovery {
            runtime.setup_review_listing(&options).await
        } else {
            runtime.setup_review_catalog_prefix(&options).await
        };
        // Review/Read live outside the caught call future, including a trap.
        // Acquire its actual ledger BEFORE `?` or finish, not only typed DATA.
        cleanup_probe = runtime.original_review_uncertain_cleanup();
        let progress = runtime
            .work
            .get()
            .catalog
            .try_lock()
            .ok()
            .and_then(|current| current.as_ref().map(|io| io.ci_progress()));
        let retired_call = runtime.ci_retired_call();
        let live_fuel = runtime
            .program
            .as_ref()
            .filter(|program| !program.observation().store_retired)
            .and_then(|program| program.fuel_remaining().ok());
        post_review_copy = Some(json!({
            "phase":CiRetiredCall::phase_name(runtime.work.get().phase.load(Ordering::Acquire)),
            "inspection_attempts_before_review":inspections_before_review,
            "inspection_attempts_after_review":runtime.inspection_attempts(),
            "catalog_progress_copy_only":progress,
            "live_store_fuel_available":live_fuel.is_some(),
            "live_store_fuel_remaining":live_fuel,
            "retired_call_sample_available":retired_call.is_some(),
            "retired_call_sample":retired_call.map(|sample| json!({
                "fuel_remaining":sample.fuel_remaining,
                "post_return":sample.post_return,
                "store_retired":sample.store_retired,
                "deadline_exceeded":sample.deadline_exceeded,
                "phase_at_retire_failure":sample.phase_at_retire_failure,
            })),
            "cleanup_probe_present":cleanup_probe.is_some(),
            "cleanup_probe_verified_before_finish":cleanup_probe.as_ref().map(|v| v.is_verified()),
            "qualification":false,
        }));
        #[cfg(feature = "catalog-diagnostics-ci")]
        if schema_diagnostic {
            // The real log is retained by Work; no Guest call or dead-Store
            // fuel query is made by this bounded native observation.
            schema_trace = runtime.ci_schema_trace();
        }
        if outcome.is_err() {
            observed = Some(json!({"outcome":"outer-error", "qualification":false}));
        }
        let error = match outcome? {
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
                "inspection_attempts_before_review":inspections_before_review,
                "inspection_attempts_after_review":runtime.inspection_attempts(),
                "catalog_progress_copy_only":catalog_progress,
                "native_code":diagnostic_text(native.code()),
                "native_message":diagnostic_text(native.message()),
                "native_details":native.details().map(diagnostic_text),
                "qualification":false}));
        }
        let expected = if recovery {
            BootstrapReviewReachpoint::RecoveryAuthorityStoreNotBound
        } else {
            BootstrapReviewReachpoint::ProfileDefinitionStoreNotBound
        };
        ensure!(matches!(error, BootstrapReviewFailure::NotBound(value) if *value == expected),
            "actual catalog did not reach {expected:?}: {error:?}");
        let actual = &runtime
            .review
            .as_ref()
            .context("same runner review required")?
            .actual;
        ensure!(
            actual.catalog_roots.host.is_some()
                && actual.catalog_roots.packaged.is_some()
                && actual.catalog.is_some(),
            "real same-Store roots and loader result must be retained"
        );
        if recovery {
            let observation = std::fs::read(fixtures_path.with_file_name("recovery-authority-reference.json"))?;
            ensure!(observation.len() <= 4096);
            let observation: Value = serde_json::from_slice(&observation)?;
            let profiles = actual._recovery_profile_definitions.as_ref().context("recovered Profiles required")?;
            let rows = profiles.array().context("ordered Profile rows required")?;
            let row = rows.get(0).context("configured Profile required")?;
            let id = row.get_index(0).context("Profile ID required")?;
            ensure!(
                actual._recovery_catalog.is_some()
                    && rows.len() == 1
                    && id.as_str() == Some("recovery-test-profile")
                    && observation["authority_constructor_entries"] == 1
                    && profiles.digest_guest(runtime.data_budget())?.as_str()
                        == observation["profile_rows_digest"].as_str().context("original Profile digest required")?
                    && !user_data.join("authority").exists(),
                "the same Store must retain the recovered catalog before Authority opens",
            );
        }
        let fuel_after = runtime.fuel_remaining()?;
        ensure!(fuel_after < fuel_before && invocation.deadline()? == deadline);
        ensure!(cleanup_probe.is_some() && !lock_is_free(&user_data)?);
        observed = Some(
            json!({"outcome":"integration-boundary",
            "stage":if recovery {"recovery-authority-store-entry"} else {"profile-definition-store-entry"},
            "fuel_before":fuel_before,"fuel_after":fuel_after,"budget_resets":0,
            "inspection_attempts_before_review":inspections_before_review,
            "inspection_attempts_after_review":runtime.inspection_attempts(),
            "same_original_deadline":true,"journal_lock_held_before_finish":true,
            "actual_host_root_retained":true,"actual_bundle_root_retained":true,
            "actual_catalog_retained":true}),
        );
        drop(diagnostic); // Owned native diagnostic only; Guest DATA stays outside.
        Ok(())
    }
    .await;
    let (execution, native_outer_diagnostic_dropped_before_finish) = match execution {
        Err(error) if original_diagnostic_in_outer(&error) => {
            // Preserve failure without cloning Read-funded native strings or
            // retaining their carrier after finish releases that Read.
            drop(error);
            (
                Err(anyhow::anyhow!(
                    "known native outer diagnostic released before Read finish; CI branch remains unqualified"
                )),
                true,
            )
        }
        other => (other, false),
    };
    let before = invocation.capacity_snapshot();
    #[cfg(feature = "catalog-diagnostics-ci")]
    let schema_status = runtime.ci_schema_status();
    let cleanup = runtime.finish().await;
    let after_finish = invocation.capacity_snapshot();
    #[cfg(feature = "catalog-diagnostics-ci")]
    let schema_trace_file = if schema_diagnostic {
        write_schema_trace(
            &fixtures_path.with_file_name("actual-catalog-schema-trace.json"),
            schema_trace.take(),
            schema_status,
        )
    } else {
        Ok(Value::Null)
    };
    let probe_present = cleanup_probe.is_some();
    let verified = cleanup_probe.as_ref().map(|v| v.is_verified());
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
        "post_review_copy_only":post_review_copy,
        "native_outer_diagnostic_dropped_before_finish":native_outer_diagnostic_dropped_before_finish,
        "execution_error":execution.as_ref().err().map(ToString::to_string),
        "finish_ok":cleanup.is_ok(),"cleanup_probe_present":probe_present,
        "cleanup_probe_verified_after_finish":verified,
        "cleanup_verified":cleanup.is_ok() && verified == Some(true),
        "journal_lock_released_after_finish":lock_released,
        "source_destructor_reached":dropped.load(Ordering::Acquire),
        "capacity":{"before_finish":{"bytes":before.charged_bytes,"reservations":before.reservations},
        "after_finish":{"bytes":after_finish.charged_bytes,"reservations":after_finish.reservations},
        "after_data":{"bytes":after_data.charged_bytes,"reservations":after_data.reservations},
        "reaped":reaped,"after_reap":{"bytes":after_reap.charged_bytes,"reservations":after_reap.reservations}},
        "qualification":false,"production_enabled":false,
        "scope":"one actual catalog operation with controlled CI resource owner and empty artifact directory; no constructor, migration, Authority, actual installed executable, GUI or full9 native qualification"});
    #[cfg(feature = "catalog-diagnostics-ci")]
    let output = {
        let mut output = output;
        if schema_diagnostic {
            output["schema_checkpoint_diagnostic"] = json!({
                "enabled":true,"core_feature":"catalog-diagnostics-ci",
                "unmeasured_catalog_gate":false,"observer_costs_in_same_original_limits":true,
                "source_provider_substitution":false,
                "trace_file":schema_trace_file.as_ref().ok(),
                "trace_write_error":schema_trace_file.as_ref().err().map(ToString::to_string)});
        }
        output
    };
    std::fs::write(
        fixtures_path.with_file_name(if recovery {
            "actual-recovery-catalog-observations.json"
        } else {
            "actual-catalog-observations.json"
        }),
        serde_json::to_vec_pretty(&output)?,
    )?;
    cleanup?;
    #[cfg(feature = "catalog-diagnostics-ci")]
    schema_trace_file?;
    execution?;
    ensure!(
        verified == Some(true)
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
