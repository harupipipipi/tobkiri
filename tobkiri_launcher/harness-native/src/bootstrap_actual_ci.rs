//! Actual full component, genuine physical funding and selected SDK metadata.
//! This CI source-owner fixture does not prove the original Tauri/GUI reachpoint.
use super::*;
use crate::{BootstrapModule, BootstrapRoots};
use anyhow::ensure;
use std::{fs::File, io::Read, path::PathBuf, sync::atomic::AtomicBool};
use tobkiri_canon::{CoreValue, Digest};
use tobkiri_core::RuntimeOwner;

mod selected {
    use super::{BootstrapModule, BootstrapModuleIndex};
    include!(env!("TOBKIRI_HARNESS_NATIVE_MODULE_INDEX_RS"));
}

struct SelectedRuntime {
    root: PathBuf,
    _directory: File,
    retired: AtomicBool,
    process: u32,
}
impl BootstrapInputOwner for SelectedRuntime {
    fn original_module_metadata_policy(&self) -> Option<&'static crate::ModuleMetadataPolicy> {
        // Explicit fixture policy, not authority inferred from its path/index.
        Some(&crate::ORIGINAL_PROFILE_SOURCE_METADATA)
    }
    fn with_roots<R>(
        &self,
        use_roots: impl for<'a> FnOnce(BootstrapRoots<'a>) -> R,
    ) -> std::result::Result<R, NativeDataIoError> {
        self.check_live()?;
        let value = use_roots(BootstrapRoots {
            runtime: &self.root,
            working_directory: &self.root,
            development_workspace: None,
        });
        self.check_live()?;
        Ok(value)
    }
    fn check_live(&self) -> std::result::Result<(), NativeDataIoError> {
        if self.process != std::process::id() || self.retired.load(Ordering::Acquire) {
            Err(NativeDataIoError::Retired)
        } else {
            Ok(())
        }
    }
}

#[tokio::test(flavor = "current_thread")]
async fn actual_original_component_initializes_and_returns_original_profile_id() -> Result<()> {
    ensure!(
        std::env::var("GITHUB_ACTIONS").as_deref() == Ok("true")
            && std::env::var("TOBKIRI_TEST_NATIVE_HARNESS_CI").as_deref() == Ok("1"),
        "actual component consumer requires explicit CI inputs"
    );
    let root = PathBuf::from(
        std::env::var_os("TOBKIRI_TEST_NATIVE_HARNESS_RUNTIME_ROOT")
            .context("actual selected runtime required")?,
    );
    ensure!(
        root.is_absolute() && root.canonicalize()? == root,
        "physical SDK required"
    );
    let expected_module = root
        .join("ecosystem/defaultspack/defaultspack/profile_runtime_composition.py")
        .canonicalize()?;
    let source = SelectedRuntime {
        _directory: File::open(&root)?,
        root,
        retired: AtomicBool::new(false),
        process: std::process::id(),
    };
    let scratch = tempfile::tempdir()?;
    let owner = RuntimeOwner::open(scratch.path().canonicalize()?.join("native-data"))?;
    let engine = WasmEngine::new_with_control(owner.control())?;
    let path = PathBuf::from(
        std::env::var_os("TOBKIRI_TEST_NATIVE_HARNESS_COMPONENT")
            .context("actual built component required")?,
    );
    ensure!(
        path.is_absolute() && path.canonicalize()? == path,
        "physical artifact required"
    );
    let mut input = File::open(&path)?;
    let size = usize::try_from(input.metadata()?.len())?;
    ensure!(
        size > 0 && size <= 32 * 1024 * 1024,
        "component exceeds source bound"
    );
    // Code admission precedes the native DATA invocation. Its bounded source
    // buffer still retains the actual runtime's metadata funding until freed.
    let permit = owner
        .control()
        .reserve(ControlKind::Frame, u64::try_from(size)?)?;
    let mut bytes = Vec::new();
    bytes.try_reserve_exact(size)?;
    ensure!(
        bytes.capacity() == size,
        "unsupported source allocator capacity"
    );
    bytes.resize(size, 0);
    input.read_exact(&mut bytes)?;
    let expected = std::env::var("TOBKIRI_TEST_NATIVE_HARNESS_COMPONENT_SHA256")?;
    ensure!(
        Digest::hash(&bytes).as_str().strip_prefix("sha256:") == Some(expected.as_str()),
        "component changed"
    );
    let component = Component::new(engine.engine(), &bytes)?;
    drop(bytes);
    drop(permit);
    drop(input);
    let invocation = owner.begin_native_setup()?;
    let deadline = invocation.deadline()?;
    let mut runtime = match NativeBootstrap::prepare(
        &engine,
        &component,
        invocation.clone(),
        source,
        &selected::INDEX,
    )
    .await?
    {
        Ok(runtime) => runtime,
        Err(error) => bail!(
            "actual original initialization refused: {}",
            error.code().as_str()?
        ),
    };
    ensure!(
        runtime.inspection_attempts() == 0,
        "metadata init attempted native IO"
    );
    let result = match runtime.bootstrap_profile_id().await? {
        Ok(result) => result,
        Err(error) => bail!("actual original method refused: {}", error.code().as_str()?),
    };
    let budget = runtime.data_budget().clone();
    let value = CoreValue::parse_guest(result.as_bytes(), &budget)?;
    let actual_format = value.get_key("format").context("result format missing")?;
    ensure!(
        actual_format.as_str() == Some("io.tobkiri.native-value-result.v1"),
        "wrong original result frame"
    );
    let scope = value.get_key("scope").context("actual scope missing")?;
    let reference = value
        .get_key("root")
        .context("actual original reference missing")?;
    let format = CoreValue::string("io.tobkiri.native-value-describe.v1", &budget)?;
    let describe = CoreValue::object_from(
        [
            Ok(("format", format)),
            Ok(("scope", scope)),
            Ok(("root", reference)),
        ]
        .into_iter(),
        &budget,
    )?
    .encode_guest(&budget)?;
    let described = match runtime.describe_value(&describe).await? {
        Ok(value) => value,
        Err(error) => bail!(
            "actual original value description refused: {}",
            error.code().as_str()?
        ),
    };
    let observed = CoreValue::parse_guest(described.as_bytes(), &budget)?;
    // Original Registry.describe's closed scalar observation, computed AFTER
    // actual method execution; never an expected value supplied to the guest.
    let nodes = observed
        .get_key("nodes")
        .context("original description nodes missing")?;
    let nodes = nodes
        .array()
        .context("original description nodes are not an array")?;
    ensure!(
        nodes.len() == 1,
        "original scalar result became another graph"
    );
    let scalar = nodes.get(0).context("original scalar missing")?;
    let kind = scalar
        .get_key("kind")
        .context("original scalar kind missing")?;
    let text = scalar
        .get_key("value")
        .context("original scalar text missing")?;
    ensure!(
        kind.as_str() == Some("str") && text.as_str() == Some("defaults"),
        "original Profile identity changed"
    );
    ensure!(
        runtime.inspection_attempts() == 0,
        "pure original method attempted native IO"
    );
    // Execute each original getter as a component-level unit operation. This
    // is not the production _bundle_root branch/binding-validation flow.
    for host in [true, false] {
        let result = match if host {
            runtime.host_resource_root().await?
        } else {
            runtime.bundled_profile_root().await?
        } {
            Ok(value) => value,
            Err(error) => bail!(
                "original opaque root getter refused: {}",
                error.code().as_str()?
            ),
        };
        let description = describe_result(&mut runtime, &result).await?;
        let nodes = description.get_key("nodes").context("root nodes missing")?;
        let nodes = nodes.array().context("root nodes are not an array")?;
        ensure!(nodes.len() == 1, "root getter returned another graph");
        let path = nodes.get(0).context("root node missing")?;
        ensure!(
            path.get_key("kind")
                .is_some_and(|v| v.as_str() == Some("native-path"))
                && path
                    .get_key("opaque_native_binding")
                    .and_then(|v| v.as_bool())
                    == Some(true)
                && path
                    .get_key("fields")
                    .is_some_and(|v| v.array().is_some_and(|a| a.is_empty()))
                && path.get_key("value").is_none(),
            "original root reference disclosed native fields or lost its actual proxy type"
        );
    }
    ensure!(
        runtime.inspection_attempts() == 2,
        "original getters did not each resolve their source module exactly once"
    );
    let (host, bundle) = runtime.first_source_resolutions();
    for observation in [host, bundle] {
        ensure!(
            observation
                .context("actual module resolution missing")?
                .as_str()?
                == expected_module
                    .to_str()
                    .context("module path is not UTF-8")?,
            "actual original module resolution changed"
        );
    }
    // The observations prove module resolution, not the opaque parents result
    // or resource IO. Those require the original subsequent production call.
    ensure!(
        invocation.deadline()? == deadline,
        "original deadline was replaced"
    );
    let peak = invocation.capacity_snapshot()?.peak_charged_bytes;
    let fuel_remaining = runtime.fuel_remaining()?;
    let elapsed = invocation.started_at().elapsed();
    ensure!(
        peak <= tobkiri_gate::NATIVE_SETUP_MEMORY_BYTES
            && fuel_remaining < 2_000_000_000
            && elapsed < tobkiri_gate::NATIVE_SETUP_WALL,
        "original shared execution limits were not observed"
    );
    runtime.finish().await?;
    println!(
        "NATIVE_HARNESS_ORIGINAL_INIT init=true profile_id=true pure_inspections=0 \
         opaque_root_getters=2 module_resolutions=2 parents_target_proven=false physical_cleanup=true \
         peak_charged_bytes={peak} fuel_remaining={fuel_remaining} elapsed_us={}",
        elapsed.as_micros()
    );
    Ok(())
}

async fn describe_result(
    runtime: &mut NativeBootstrap<SelectedRuntime>,
    result: &CoreBuffer,
) -> Result<CoreValue> {
    let budget = runtime.data_budget().clone();
    let result = CoreValue::parse_guest(result.as_bytes(), &budget)?;
    ensure!(
        result
            .get_key("format")
            .is_some_and(|v| v.as_str() == Some("io.tobkiri.native-value-result.v1")),
        "wrong root result frame"
    );
    let request = CoreValue::object_from(
        [
            Ok((
                "format",
                CoreValue::string("io.tobkiri.native-value-describe.v1", &budget)?,
            )),
            Ok((
                "scope",
                result.get_key("scope").context("root scope missing")?,
            )),
            Ok((
                "root",
                result.get_key("root").context("root reference missing")?,
            )),
        ]
        .into_iter(),
        &budget,
    )?
    .encode_guest(&budget)?;
    let value = match runtime.describe_value(&request).await? {
        Ok(value) => value,
        Err(error) => bail!(
            "original path description refused: {}",
            error.code().as_str()?
        ),
    };
    Ok(CoreValue::parse_guest(value.as_bytes(), &budget)?)
}
