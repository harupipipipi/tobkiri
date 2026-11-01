//! Actual original component + real publication guard, not product GUI adoption.
use super::actual_ci::{SelectedRuntime, selected};
use super::denied_actual_ci::ObservedSource;
use super::*;
use crate::{
    BootstrapReviewFailure, BootstrapReviewOptions, BootstrapReviewReachpoint,
    OriginalAuthorityError, OriginalAuthorityOwner,
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

struct ReviewSource {
    source: ObservedSource,
    user_data: PathBuf,
}
impl BootstrapInputOwner for ReviewSource {
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
impl OriginalAuthorityOwner for ReviewSource {
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
impl crate::BootstrapReviewOwner for ReviewSource {
    fn with_original_review_user_data<R>(
        &self,
        f: impl FnOnce(&Path) -> R,
    ) -> std::result::Result<R, OriginalAuthorityError> {
        self.with_original_user_data(f)
    }
}

// Independent descriptor, never the implementation's guard/FD or a held flag.
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

fn getter(value: &crate::BootstrapIdentityOutcome) -> Result<Value> {
    let crate::BootstrapIdentityOutcome::Returned(value) = value else {
        bail!("unmodified actual getter did not return a value");
    };
    ensure!(value.builtin_kind() == crate::BootstrapBuiltinKind::Str);
    let result: Value = serde_json::from_slice(value.result().as_bytes())?;
    let view: Value = serde_json::from_slice(value.view().as_bytes())?;
    ensure!(
        view["root"] == result["root"]
            && view["scope"] == result["scope"]
            && view["origin"] == "getter-returned"
            && view["registered_builtin_kind"] == "str"
            && view["string"]["codepoints"] == json!([100, 101, 102, 97, 117, 108, 116, 115])
    );
    Ok(json!({"kind":view["kind"],"string":view["string"],"scope":view["scope"]}))
}

#[tokio::test(flavor = "current_thread")]
#[ignore = "requires a fresh original publication fixture and the actual component"]
async fn original_default_component_driver() -> Result<()> {
    ensure!(
        std::env::var("GITHUB_ACTIONS").as_deref() == Ok("true")
            && std::env::var("TOBKIRI_TEST_NATIVE_HARNESS_CI").as_deref() == Ok("1")
    );
    let path = PathBuf::from(
        std::env::var_os("TOBKIRI_ORIGINAL_PUBLICATION_IDENTITY_FIXTURES")
            .context("fresh original publication fixtures required")?,
    );
    let bytes = std::fs::read(&path)?;
    ensure!(bytes.len() <= 2 * 1024 * 1024);
    let fixtures: Value = serde_json::from_slice(&bytes)?;
    ensure!(
        fixtures["format"] == "io.tobkiri.original-publication-identity-reference.v1"
            && fixtures["original_head"] == "1cf025f1eeab31a3daa2861b1f26a025c6954b6e"
            && fixtures["reference_checks_passed"] == true
    );
    let rows = fixtures["cases"]
        .as_array()
        .context("closed fixture cases required")?;
    ensure!(rows.len() == 147);
    let root = PathBuf::from(
        std::env::var_os("TOBKIRI_TEST_NATIVE_HARNESS_RUNTIME_ROOT")
            .context("actual selected runtime required")?,
    );
    let component = PathBuf::from(
        std::env::var_os("TOBKIRI_TEST_NATIVE_HARNESS_COMPONENT")
            .context("actual component required")?,
    );
    ensure!(
        root.is_absolute()
            && root.canonicalize()? == root
            && component.is_absolute()
            && component.canonicalize()? == component
    );
    let digest = Digest::parse(&format!(
        "sha256:{}",
        std::env::var("TOBKIRI_TEST_NATIVE_HARNESS_COMPONENT_SHA256")?
    ))?;
    let mut observations = Vec::new();
    for name in ["valid", "identity-candidate-mismatch"] {
        let fixture = rows
            .iter()
            .find(|v| v["name"] == name)
            .context("actual default case required")?;
        ensure!(fixture["getter_fixture"]["mode"] == "unmodified-default");
        let user_data = PathBuf::from(
            fixture["user_data_dir"]
                .as_str()
                .context("fixed fixture root required")?,
        );
        ensure!(user_data.is_absolute() && user_data.canonicalize()? == user_data);
        let source_dropped = Arc::new(AtomicBool::new(false));
        let source = ReviewSource {
            source: ObservedSource::new(
                SelectedRuntime {
                    _directory: File::open(&root)?,
                    root: root.clone(),
                    retired: AtomicBool::new(false),
                    process: std::process::id(),
                },
                source_dropped.clone(),
            ),
            user_data: user_data.clone(),
        };
        let scratch = tempfile::tempdir()?;
        let owner_path = scratch
            .path()
            .canonicalize()?
            .join("actual-review-identity");
        let owner = RuntimeOwner::open(&owner_path)?;
        // Each independent original operation has ONE original Store/account.
        // No compiled preparation is moved outside its absolute original clock.
        let invocation = owner.begin_native_setup()?;
        let deadline = invocation.deadline()?;
        let preparation = crate::NativeComponentPreparation::new(invocation.clone(), source)?;
        let prepared = preparation.compile(File::open(&component)?, &digest)?;
        ensure!(prepared.observation().deadline == deadline);
        let mut runtime = match prepared.initialize(&selected::INDEX).await? {
            Ok(value) => value,
            Err(_) => bail!("actual review initialization refused"),
        };
        let fuel = runtime.fuel_remaining()?;
        let inspections = runtime.inspection_attempts();
        let mut held_failure = None;
        let mut held_success = None;
        let mut cleanup_probe = None;
        let mut observed = None;
        let execution:Result<()>=async {
            let failure=match runtime.setup_review_listing(&BootstrapReviewOptions::default()).await? {
                Ok(value)=>{held_success=Some(value);bail!("unbound review unexpectedly returned success");}
                Err(error)=>error,
            };
            // Split old prepaid String diagnostics from actual guest DATA,
            // including when an independent assertion/codec below fails.
            let failure = if failure.owns_original_diagnostics() {
                Some(failure)
            } else {
                held_failure = Some(failure);
                None
            };
            let error=held_failure.as_ref().or(failure.as_ref()).context("actual outcome required")?;
            let (outcome,raw_getter)=match error {
                BootstrapReviewFailure::NotBound(BootstrapReviewReachpoint::HostProfileCatalogNotBound)=>{
                    ensure!(name=="valid");
                    let actual=runtime.review.as_ref().context("same runner review required")?
                        .actual.getter.as_ref().context("actual getter missing")?;
                    (json!({"outcome":"integration-boundary","stage":"recovery-catalog-not-bound"}),getter(actual)?)
                }
                BootstrapReviewFailure::ActualDenied{denied,getter:actual}=>{
                    ensure!(name=="identity-candidate-mismatch");
                    let actual_getter = getter(actual.as_ref().context("actual mismatch getter required")?)?;
                    let view:Value=serde_json::from_slice(denied.view().as_bytes())?;
                    ensure!(view["scope"] == actual_getter["scope"]);
                    let nodes=view["nodes"].as_array().context("actual factory nodes required")?;
                    ensure!(nodes.len()==1&&nodes[0]["kind"]=="profile-resolution-denied"
                        &&denied.cause().is_none()&&denied.context().is_none()
                        &&denied.opaque_cause().is_none()&&denied.opaque_context().is_none()
                        &&!denied.suppress_context());
                    (json!({"outcome":"actual-denied","kind":nodes[0]["kind"],
                        "factory_denied":nodes[0]["factory_denied"],"host_cause":null,
                        "host_context":null,"host_suppress_context":false}),
                        actual_getter)
                }
                _=>bail!("actual continuous review reached an unexpected branch"),
            };
            let remaining=runtime.fuel_remaining()?;
            ensure!(remaining<fuel&&invocation.deadline()?==deadline
                &&runtime.inspection_attempts()==inspections);
            cleanup_probe=runtime.original_review_uncertain_cleanup();
            ensure!(cleanup_probe.is_some(),"actual pending scope was not retained");
            ensure!(!lock_is_free(&user_data)?,"actual journal unlocked before finish");
            observed=Some(json!({"name":name,"outcome":outcome,"getter":raw_getter,
                "fuel_before":fuel,"fuel_after":remaining,"budget_resets":0,
                "same_original_deadline":true,"extra_inspections":0,
                "journal_lock_held_before_finish":true,"uncertain_before_finish":true}));
            drop(failure); // Native-owned diagnostic variant only.
            Ok(())
        }.await;
        // An observation error cannot skip finish or destroy Guest carriers.
        let before = invocation.capacity_snapshot();
        let cleanup = runtime.finish().await;
        let after_finish = invocation.capacity_snapshot();
        let verified = cleanup_probe
            .as_ref()
            .is_some_and(|probe| probe.is_verified());
        let lock_released = lock_is_free(&user_data)?;
        drop(held_failure);
        drop(held_success);
        drop(cleanup_probe);
        let before = before?;
        let after_finish = after_finish?;
        let after_data = invocation.capacity_snapshot()?;
        let reaped = tobkiri_wasm::reap_allocation_owners()?;
        let after_reap = invocation.capacity_snapshot()?;
        observations.push(json!({"name":name,"observation":observed,
            "execution_matched_branch":execution.is_ok(),"cleanup_verified":cleanup.is_ok()&&verified,
            "journal_lock_released_after_finish":lock_released,
            "source_destructor_reached":source_dropped.load(Ordering::Acquire),
            "capacity":{"before_finish":{"bytes":before.charged_bytes,"reservations":before.reservations},
                "after_finish":{"bytes":after_finish.charged_bytes,"reservations":after_finish.reservations},
                "after_data":{"bytes":after_data.charged_bytes,"reservations":after_data.reservations},
                "reaped":reaped,"after_reap":{"bytes":after_reap.charged_bytes,"reservations":after_reap.reservations}}}));
        let output = json!({"format":"io.tobkiri.actual-default-publication-identity-observations.v1",
            "cases":observations,"qualification":false,"production_enabled":false,
            "scope":"two actual default-getter operations only; each one Store/account; no controlled getter, full147, physical close-fault or GUI qualification"});
        std::fs::write(
            path.with_file_name("actual-default-review-observations.json"),
            serde_json::to_vec_pretty(&output)?,
        )?;
        cleanup?;
        execution?;
        ensure!(
            verified
                && lock_released
                && source_dropped.load(Ordering::Acquire)
                && after_reap.charged_bytes == 0
                && after_reap.reservations == 0
        );
        drop(invocation);
        drop(owner);
        drop(RuntimeOwner::open(owner_path)?);
    }
    Ok(())
}
