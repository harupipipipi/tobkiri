//! Nine independent actual catalog operations. CI fixtures are inputs only;
//! their expected outcomes never enter this driver or the actual component.
use super::actual_ci::{SelectedRuntime, selected};
use super::catalog_actual_ci::{lock_is_free, original_diagnostic_in_outer};
use super::denied_actual_ci::ObservedSource;
use super::*;
use crate::{
    BootstrapCatalogSource, BootstrapDenied, BootstrapRecoveryCatch, BootstrapReviewFailure,
    BootstrapReviewOptions, BootstrapReviewReachpoint, NativeErrorKind, OriginalAuthorityError,
    OriginalAuthorityOwner,
};
use anyhow::ensure;
use base64::Engine;
use serde_json::{Value, json};
use std::{
    collections::BTreeSet,
    fmt::Write as _,
    fs::File,
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
};
use tobkiri_canon::Digest;
use tobkiri_core::RuntimeOwner;

const CASES: [&str; 9] = [
    "packaged-valid",
    "source-without-artifact-root",
    "manifest-missing",
    "manifest-invalid-json",
    "manifest-invalid-utf8",
    "manifest-wrong-schema",
    "manifest-crlf",
    "resource-unlisted",
    "development-bundled",
];
// Pinned original Pack class, not a class name taken from the reference's
// expected graph or from an arbitrary Guest field. The actual trusted kind
// below must first be ProfileResolutionDenied. Original profile_resolution.py
// explicitly assigns its owned class's __module__ to this legacy public name.
const DENIED_CLASS: &str =
    "ecosystem.defaultspack.domain.runtime_v4.service.ProfileResolutionDenied";

struct CatalogSource {
    source: ObservedSource,
    user_data: PathBuf,
    environment: String,
    configured_app: Option<PathBuf>,
}
impl BootstrapInputOwner for CatalogSource {
    fn original_module_metadata_policy(&self) -> Option<&'static crate::ModuleMetadataPolicy> {
        self.source.original_module_metadata_policy()
    }
    fn with_roots<R>(
        &self,
        operation: impl for<'a> FnOnce(crate::BootstrapRoots<'a>) -> R,
    ) -> std::result::Result<R, NativeDataIoError> {
        self.source.with_roots(operation)
    }
    fn check_live(&self) -> std::result::Result<(), NativeDataIoError> {
        self.source.check_live()
    }
}
impl OriginalAuthorityOwner for CatalogSource {
    fn with_original_user_data<R>(
        &self,
        operation: impl FnOnce(&Path) -> R,
    ) -> std::result::Result<R, OriginalAuthorityError> {
        self.check_original_owner()?;
        Ok(operation(&self.user_data))
    }
    fn check_original_owner(&self) -> std::result::Result<(), OriginalAuthorityError> {
        self.check_live()
            .map_err(|_| OriginalAuthorityError::unavailable())
    }
}
impl crate::BootstrapReviewOwner for CatalogSource {
    fn with_original_review_user_data<R>(
        &self,
        operation: impl FnOnce(&Path) -> R,
    ) -> std::result::Result<R, OriginalAuthorityError> {
        self.with_original_user_data(operation)
    }
    fn with_original_catalog_source<R>(
        &self,
        operation: impl for<'a> FnOnce(BootstrapCatalogSource<'a>) -> R,
    ) -> std::result::Result<R, OriginalAuthorityError> {
        self.check_original_owner()?;
        // IO still borrows this SAME actual selected source. Environment and
        // configured_app are controlled branch DATA, not a grant or root.
        self.with_roots(|roots| {
            operation(BootstrapCatalogSource {
                runtime: roots.runtime,
                environment: &self.environment,
                configured_app: self.configured_app.as_deref(),
            })
        })
        .map_err(|_| OriginalAuthorityError::unavailable())
    }
}

fn codepoints(value: &Value, maximum: usize) -> Result<String> {
    let length = value["length"]
        .as_u64()
        .context("actual text length required")?;
    let points = value["codepoints"]
        .as_array()
        .context("actual text codepoints required")?;
    ensure!(length <= maximum as u64 && length == points.len() as u64);
    points
        .iter()
        .map(|point| {
            let point = u32::try_from(point.as_u64().context("actual codepoint required")?)?;
            char::from_u32(point).context("surrogate text is outside these nine leaf fixtures")
        })
        .collect()
}

fn parser_observation(denied: &BootstrapDenied, scope: &Value) -> Result<(Value, Value)> {
    let cause = denied
        .opaque_cause()
        .context("actual parser cause required")?;
    let context = denied
        .opaque_context()
        .context("actual parser context required")?;
    ensure!(
        std::ptr::eq(cause, context)
            && denied.suppress_context()
            && denied.cause().is_none()
            && denied.context().is_none()
            && cause.origin() == "manifest"
            && cause.recovery_catch() == BootstrapRecoveryCatch::ValueError
    );
    let view: Value = serde_json::from_slice(cause.view().as_bytes())?;
    ensure!(
        view["format"] == "io.tobkiri.native-bootstrap-identity-view.v1"
            && view["origin"] == "manifest-raised"
            && view["registered_builtin_kind"] == "record"
            && view["scope"] == *scope
    );
    let exception = &view["exception"];
    // This comparator closes only the actual leaf parser cases in Original9.
    // Intrinsic deep edges stay unqualified; they are never silently dropped.
    ensure!(
        exception["cause"].is_null()
            && exception["context"].is_null()
            && exception["suppress_context"] == false
            && exception["os_fields"].is_null()
            && view["owned"] == json!([])
    );
    let (class, message, intrinsic) = match view["kind"].as_str() {
        Some("json-decode-error") => {
            let args = exception["args"]
                .as_array()
                .context("actual JSON args required")?;
            ensure!(args.len() == 1 && args[0]["kind"] == "str");
            let fields = &exception["json_fields"];
            (
                "json.decoder.JSONDecodeError",
                codepoints(&args[0]["value"], 4096)?,
                json!({"kind":"json-decode", "msg":codepoints(&fields["msg"],4096)?,
                    "doc":codepoints(&fields["doc"],MAX_BYTES)?,
                    "pos":fields["pos"],"lineno":fields["lineno"],"colno":fields["colno"]}),
            )
        }
        Some("unicode-decode-error") => {
            let fields = &exception["unicode_fields"];
            ensure!(fields["object"]["kind"] == "bytes");
            let bytes = base64::engine::general_purpose::STANDARD.decode(
                fields["object"]["value"]
                    .as_str()
                    .context("actual decoder bytes required")?,
            )?;
            let mut object_hex = String::with_capacity(bytes.len() * 2);
            for byte in bytes {
                write!(object_hex, "{byte:02x}")?;
            }
            (
                "builtins.UnicodeDecodeError",
                codepoints(&fields["formatted_message"], 4096)?,
                json!({"kind":"unicode-decode", "encoding":codepoints(&fields["encoding"],4096)?,
                    "object_hex":object_hex,"start":fields["start"],"end":fields["end"],
                    "reason":codepoints(&fields["reason"],4096)?}),
            )
        }
        _ => bail!("actual parser class is outside the nine-case leaf contract"),
    };
    Ok((
        json!({"id":1,"type":class,"message":message,"suppressed":false,
            "cause":null,"context":null}),
        intrinsic,
    ))
}

fn denied_observation(denied: &BootstrapDenied, parser: bool) -> Result<Value> {
    ensure!(denied.kind() == NativeErrorKind::ProfileResolutionDenied);
    let result: Value = serde_json::from_slice(denied.result().as_bytes())?;
    let view: Value = serde_json::from_slice(denied.view().as_bytes())?;
    ensure!(
        result["format"] == "io.tobkiri.native-value-result.v1"
            && view["format"] == "io.tobkiri.native-value-view.v1"
            && result["scope"] == view["scope"]
            && result["root"] == view["root"]
    );
    let nodes = view["nodes"]
        .as_array()
        .context("actual factory nodes required")?;
    ensure!(nodes.len() == 1);
    let node = &nodes[0];
    let object = result["root"]["object"]
        .as_str()
        .context("actual factory reference required")?;
    ensure!(
        node["ref"] == result["root"]
            && result["owned"] == json!([object])
            && node["kind"] == "profile-resolution-denied"
            && node["fields"] == json!([])
            && node["opaque_native_binding"] == false
    );
    let intrinsic_factory = &node["factory_denied"];
    let args = intrinsic_factory["args"]
        .as_array()
        .context("actual factory args required")?;
    ensure!(
        args.len() == 1
            && intrinsic_factory["cause"].is_null()
            && intrinsic_factory["context"].is_null()
            && intrinsic_factory["suppress_context"] == false
    );
    let message = args[0]
        .as_str()
        .context("actual factory message required")?;
    ensure!(denied.message().as_str()? == message);
    let (cause, context, intrinsic) = if parser {
        let (leaf, intrinsic) = parser_observation(denied, &result["scope"])?;
        (leaf, json!({"ref":1}), json!([intrinsic]))
    } else {
        ensure!(
            denied.cause().is_none()
                && denied.context().is_none()
                && denied.opaque_cause().is_none()
                && denied.opaque_context().is_none()
                && !denied.suppress_context()
        );
        (Value::Null, Value::Null, json!([]))
    };
    Ok(
        json!({"outcome":"error", "error":{"id":0,"type":DENIED_CLASS,
        "message":message,"suppressed":denied.suppress_context(),
        "cause":cause,"context":context}, "intrinsic_parse_errors":intrinsic}),
    )
}

fn boundary_case(name: &str) -> bool {
    matches!(
        name,
        "packaged-valid" | "source-without-artifact-root" | "manifest-crlf" | "development-bundled"
    )
}

async fn observe_case(
    name: &str,
    input_path: &Path,
    component: &Path,
    digest: &Digest,
) -> Result<Value> {
    let input_bytes = std::fs::read(input_path)?;
    ensure!(input_bytes.len() <= 16 * 1024);
    let input: Value = serde_json::from_slice(&input_bytes)?;
    let base = input_path
        .parent()
        .context("native fixture directory required")?;
    let root = PathBuf::from(
        input["runtime"]
            .as_str()
            .context("runtime input required")?,
    );
    let user_data = PathBuf::from(
        input["user_data"]
            .as_str()
            .context("user data input required")?,
    );
    ensure!(
        input["case"] == name
            && input["resource_fixture_only"] == true
            && input["platform_artifact_directory_only"] == true
            && root == base.join("runtime")
            && user_data == base.join("user-data")
            && root.is_absolute()
            && root.canonicalize()? == root
            && user_data.is_absolute()
            && user_data.canonicalize()? == user_data
    );
    let development = name == "development-bundled";
    let environment = input["environment"]["RUMI_ENVIRONMENT"]
        .as_str()
        .context("fixed original environment required")?;
    ensure!(
        environment
            == if development {
                "development"
            } else {
                "production"
            }
    );
    let configured_app = if development {
        let path = PathBuf::from(
            input["environment"]["RUMI_APP_DIR"]
                .as_str()
                .context("fixed development input required")?,
        );
        ensure!(path == root);
        Some(path)
    } else {
        ensure!(input["environment"]["RUMI_APP_DIR"].is_null());
        None
    };
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
        environment: environment.to_owned(),
        configured_app,
    };
    let scratch = tempfile::tempdir()?;
    let owner_path = scratch
        .path()
        .canonicalize()?
        .join("actual-catalog-nine-owner");
    let owner = RuntimeOwner::open(&owner_path)?;
    // Each case is a distinct original operation. Its one clock starts BEFORE
    // preparation/compilation; the case loop never resets this operation.
    let invocation = owner.begin_native_setup()?;
    let deadline = invocation.deadline()?;
    let prepared = crate::NativeComponentPreparation::new(invocation.clone(), source)?
        .compile(File::open(component)?, digest)?;
    ensure!(prepared.observation().deadline == deadline);
    let mut runtime = match prepared.initialize(&selected::INDEX).await? {
        Ok(value) => value,
        Err(_) => bail!("actual case initialization refused"),
    };
    let fuel_before = runtime.fuel_remaining().ok();
    let inspections_before = runtime.inspection_attempts();
    let called = runtime
        .setup_review_catalog_prefix(&BootstrapReviewOptions::default())
        .await;
    // Acquire real cleanup handles BEFORE inspecting/propagating the outer
    // Result. A trap may have retired the Store without returning a typed
    // Review outcome; it cannot bypass actual finish or synthesize a receipt.
    let cleanup_probe = runtime.original_review_uncertain_cleanup();
    let pre_finish_receipt = runtime.review.as_ref().map(|review| review.receipt());
    let retired_call = runtime.ci_retired_call();
    let fuel_after = runtime
        .fuel_remaining()
        .ok()
        .or_else(|| retired_call.and_then(|sample| sample.fuel_remaining));
    let program_observation = runtime
        .program
        .as_ref()
        .map(|program| program.observation());
    let progress = runtime
        .work
        .get()
        .catalog
        .try_lock()
        .ok()
        .and_then(|catalog| catalog.as_ref().map(|io| io.ci_progress()));
    let lock_held = lock_is_free(&user_data).map(|free| !free).unwrap_or(false);
    let same_deadline = invocation
        .deadline()
        .is_ok_and(|current| current == deadline);
    let inspections_after = runtime.inspection_attempts();
    let (host_root, bundle_root, catalog) = runtime
        .review
        .as_ref()
        .map(|review| {
            let actual = &review.actual;
            (
                actual.host_root.is_some(),
                actual.bundle_root.is_some(),
                actual.catalog.is_some(),
            )
        })
        .unwrap_or((false, false, false));
    let mut held_failure = None;
    let mut held_success = None;
    let mut held_outer = None;
    let mut native_diagnostic = None;
    let mut native_outer_dropped = false;
    let mut failure_tag = None;
    match called {
        Err(error) => {
            // Read-funded OriginalAuthorityError Strings can also escape via
            // the outer channel (including the known IO-cleanup wrapper).
            // This branch records only a fixed failure tag, so drop those
            // diagnostics while Read is still funded. Unknown/trap/Guest
            // carriers retain their existing post-finish lifetime instead.
            if original_diagnostic_in_outer(&error) {
                drop(error);
                native_outer_dropped = true;
            } else {
                held_outer = Some(error);
            }
            failure_tag = Some("outer-call-failed");
        }
        Ok(Ok(value)) => {
            held_success = Some(value);
            failure_tag = Some("unexpected-review-success");
        }
        Ok(Err(error)) if error.owns_original_diagnostics() => native_diagnostic = Some(error),
        Ok(Err(error)) => held_failure = Some(error),
    }
    // Guest/cause CoreBuffers are already retained outside this fallible
    // observation. Native owned strings use Read's existing prepaid funding
    // and are dropped BEFORE finish consumes that Read instead.
    let observation = (|| -> Result<Value> {
        let error = held_failure
            .as_ref()
            .or(native_diagnostic.as_ref())
            .context("no typed Review outcome")?;
        if boundary_case(name) {
            ensure!(matches!(
                error,
                BootstrapReviewFailure::NotBound(
                    BootstrapReviewReachpoint::ProfileDefinitionStoreNotBound
                )
            ));
            ensure!(host_root && catalog && bundle_root != development);
            return Ok(json!({"outcome":"integration-boundary",
                "stage":"profile-definition-store-entry"}));
        }
        let BootstrapReviewFailure::ActualDenied { denied, .. } = error else {
            bail!("actual failure is outside the closed factory/parser contract");
        };
        let parser = matches!(name, "manifest-invalid-json" | "manifest-invalid-utf8");
        let observed = denied_observation(denied, parser)?;
        if parser {
            let parser_kind = if name == "manifest-invalid-json" {
                "json-decode"
            } else {
                "unicode-decode"
            };
            ensure!(observed["intrinsic_parse_errors"][0]["kind"] == parser_kind);
        }
        Ok(observed)
    })();
    drop(native_diagnostic);
    let (observation, matched_branch) = match observation {
        Ok(value) => (value, true),
        Err(_) => {
            failure_tag.get_or_insert("actual-observation-contract-refused");
            (
                json!({"outcome":"execution-error","failure_tag":failure_tag}),
                false,
            )
        }
    };
    let before_finish = invocation.capacity_snapshot();
    let cleanup = runtime.finish().await;
    let after_finish = invocation.capacity_snapshot();
    // finish checks the FRESH post-close receipt internally. The early Copy
    // receipt is only a pre-finish observation and cannot change after close.
    let cleanup_verified = cleanup.is_ok()
        && cleanup_probe
            .as_ref()
            .is_some_and(|probe| probe.is_verified());
    let lock_released = lock_is_free(&user_data).unwrap_or(false);
    drop(held_failure);
    drop(held_success);
    drop(held_outer);
    drop(cleanup_probe);
    let after_data = invocation.capacity_snapshot();
    let reaped = tobkiri_wasm::reap_allocation_owners();
    let reaped_ok = reaped.is_ok();
    let after_reap = invocation.capacity_snapshot();
    let capacity = |snapshot: &std::result::Result<
        tobkiri_gate::NativeSetupSnapshot,
        tobkiri_gate::GateError,
    >| match snapshot {
        Ok(snapshot) => {
            json!({"bytes":snapshot.charged_bytes,"reservations":snapshot.reservations})
        }
        Err(_) => Value::Null,
    };
    let empty = after_reap
        .as_ref()
        .is_ok_and(|snapshot| snapshot.charged_bytes == 0 && snapshot.reservations == 0);
    let limit = after_reap
        .as_ref()
        .ok()
        .map(|snapshot| snapshot.limit_bytes);
    let peak = after_reap
        .as_ref()
        .ok()
        .map(|snapshot| snapshot.peak_charged_bytes);
    let source_dropped = dropped.load(Ordering::Acquire);
    drop(invocation);
    drop(owner);
    // Do not block reopening an owner whose actual cleanup/custody remains
    // uncertain. The false result is a failed proof, never a fake reopen.
    let reopened = if cleanup_verified && lock_released && source_dropped && reaped_ok && empty {
        RuntimeOwner::open(owner_path).map(drop).is_ok()
    } else {
        false
    };
    Ok(json!({"name":name,"observation":observation,
        "execution_matched_branch":matched_branch,"failure_tag":failure_tag,
        "cleanup_verified":cleanup_verified,"source_destructor_reached":source_dropped,
        "physical_owner_reopened":reopened,"journal_lock_held_before_finish":lock_held,
        "journal_lock_released_after_finish":lock_released,
        "capacity":{"before_finish":capacity(&before_finish),"after_finish":capacity(&after_finish),
            "after_data":capacity(&after_data),"after_reap":capacity(&after_reap),
            "reaped":reaped.ok(),"limit_bytes":limit,"peak_bytes":peak},
        "measurement":{"same_original_deadline":same_deadline,"budget_resets":0,
            "fuel_before":fuel_before,"fuel_after":fuel_after,
            "actual_host_root_retained":host_root,"actual_bundle_root_retained":bundle_root,
            "actual_catalog_retained":catalog,
            "inspection_attempts_before_review":inspections_before,
            "inspection_attempts_after_review":inspections_after,
            "native_outer_diagnostic_dropped_before_finish":native_outer_dropped,
            "catalog_progress_copy_only":progress,"program_observation":program_observation,
            "pre_finish_receipt_copy_only":pre_finish_receipt.map(|receipt|json!({
                "open_files":receipt.open_files,"open_connections":receipt.open_connections,
                "uncertain":receipt.uncertain})),
            "retired_call":retired_call.map(|sample|json!({"fuel_remaining":sample.fuel_remaining,
                "post_return":sample.post_return,"store_retired":sample.store_retired,
                "deadline_exceeded":sample.deadline_exceeded,
                "phase_at_retire_failure":sample.phase_at_retire_failure}))}}))
}

#[tokio::test(flavor = "current_thread")]
#[ignore = "requires fresh independent Original9 fixtures and the actual component"]
async fn original_catalog_nine_driver() -> Result<()> {
    ensure!(
        std::env::var("GITHUB_ACTIONS").as_deref() == Ok("true")
            && std::env::var("TOBKIRI_TEST_NATIVE_HARNESS_CI").as_deref() == Ok("1")
    );
    let fixtures_path = PathBuf::from(
        std::env::var_os("TOBKIRI_ORIGINAL_CATALOG_PREFIX_FIXTURES")
            .context("fresh independent nine-case fixtures required")?,
    );
    let bytes = std::fs::read(&fixtures_path)?;
    ensure!(bytes.len() <= 2 * 1024 * 1024);
    let fixtures: Value = serde_json::from_slice(&bytes)?;
    ensure!(
        fixtures["format"] == "io.tobkiri.original-catalog-prefix-reference.v1"
            && fixtures["original_head"] == "1cf025f1eeab31a3daa2861b1f26a025c6954b6e"
            && fixtures["case_count"] == 9
    );
    let cases = fixtures["cases"]
        .as_array()
        .context("closed cases required")?;
    ensure!(cases.len() == 9);
    ensure!(
        cases
            .iter()
            .filter_map(|case| case["name"].as_str())
            .collect::<BTreeSet<_>>()
            == CASES.into_iter().collect::<BTreeSet<_>>()
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
    let mut observations = Vec::new();
    for name in CASES {
        let case = cases
            .iter()
            .find(|case| case["name"] == name)
            .context("case missing")?;
        let input = PathBuf::from(
            case["native_input"]
                .as_str()
                .context("native input missing")?,
        );
        ensure!(
            input
                == fixtures_path
                    .parent()
                    .context("fixture directory required")?
                    .join(name)
                    .join("native/input.json")
        );
        // Only the separately prepared native_input is consumed. In particular,
        // this driver never reads case.expected, reference graphs or messages.
        let row = match observe_case(name, &input, &component, &digest).await {
            Ok(row) => row,
            Err(_) => json!({"name":name,"observation":{"outcome":"execution-error",
                "failure_tag":"case-preparation-failed"},"execution_matched_branch":false,
                "cleanup_verified":false,"source_destructor_reached":null,
                "physical_owner_reopened":false,"journal_lock_held_before_finish":null,
                "journal_lock_released_after_finish":null,"capacity":null,"measurement":null}),
        };
        observations.push(row);
        let output = serde_json::to_vec_pretty(&json!({
            "format":"io.tobkiri.actual-catalog-prefix-nine-observations.v1",
            "case_count":observations.len(),"cases":observations,
            "qualification":false,"production_enabled":false,
            "scope":"nine independent actual catalog operations; controlled CI resource fixtures and leaf parser errors only; no constructor body, migration, Authority, arbitrary deep error graph, installed executable or GUI"}))?;
        ensure!(output.len() <= 2 * 1024 * 1024);
        std::fs::write(
            fixtures_path.with_file_name("actual-catalog-nine-observations.json"),
            output,
        )?;
    }
    ensure!(
        observations.iter().all(|row| {
            row["execution_matched_branch"] == true
                && row["cleanup_verified"] == true
                && row["source_destructor_reached"] == true
                && row["physical_owner_reopened"] == true
                && row["journal_lock_held_before_finish"] == true
                && row["journal_lock_released_after_finish"] == true
                && row["capacity"]["after_reap"] == json!({"bytes":0,"reservations":0})
                && row["capacity"]["reaped"].as_u64().is_some()
                && row["capacity"]["limit_bytes"] == 64 * 1024 * 1024
                && row["capacity"]["peak_bytes"]
                    .as_u64()
                    .zip(row["capacity"]["limit_bytes"].as_u64())
                    .is_some_and(|(peak, limit)| peak <= limit)
                && row["measurement"]["same_original_deadline"] == true
                && row["measurement"]["fuel_before"]
                    .as_u64()
                    .zip(row["measurement"]["fuel_after"].as_u64())
                    .is_some_and(|(before, after)| after < before && before <= 2_000_000_000)
        }),
        "one or more actual catalog operations remain unqualified; bounded output retained"
    );
    Ok(())
}
