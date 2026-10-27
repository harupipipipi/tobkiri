//! Hostile WAT tests exercise rejection/custody, never product compatibility.
use super::*;
use crate::{BootstrapModule, BootstrapRoots};
use std::{
    fs::File,
    future::Future,
    path::PathBuf,
    sync::{
        Mutex,
        atomic::{AtomicBool, AtomicUsize},
        mpsc,
    },
    task::Poll,
};
use tobkiri_core::RuntimeOwner;

struct SelectedFixture {
    root: PathBuf,
    _directory: File,
    process: u32,
    metadata: bool,
    barrier: Option<Arc<RootBarrier>>,
}
impl BootstrapInputOwner for SelectedFixture {
    fn original_module_metadata_policy(&self) -> Option<&'static crate::ModuleMetadataPolicy> {
        self.metadata
            .then_some(&crate::ORIGINAL_PROFILE_SOURCE_METADATA)
    }

    fn with_roots<R>(
        &self,
        use_roots: impl for<'a> FnOnce(BootstrapRoots<'a>) -> R,
    ) -> std::result::Result<R, NativeDataIoError> {
        self.check_live()?;
        if let Some(barrier) = &self.barrier {
            barrier.enter()?;
        }
        let result = use_roots(BootstrapRoots {
            runtime: &self.root,
            working_directory: &self.root,
            development_workspace: None,
        });
        self.check_live()?;
        Ok(result)
    }
    fn check_live(&self) -> std::result::Result<(), NativeDataIoError> {
        if self.process == std::process::id() {
            Ok(())
        } else {
            Err(NativeDataIoError::Retired)
        }
    }
}
static INDEX: BootstrapModuleIndex = BootstrapModuleIndex {
    sha256: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    modules: &[BootstrapModule {
        module: "fixture",
        relative_file: "fixture.py",
    }],
};

static PROFILE_INDEX: BootstrapModuleIndex = BootstrapModuleIndex {
    sha256: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    modules: &[BootstrapModule {
        module: "ecosystem.defaultspack.defaultspack.profile_runtime_composition",
        relative_file: "ecosystem/defaultspack/defaultspack/profile_runtime_composition.py",
    }],
};

fn selected(root: PathBuf) -> Result<SelectedFixture> {
    Ok(SelectedFixture {
        _directory: File::open(&root)?,
        root,
        process: std::process::id(),
        metadata: false,
        barrier: None,
    })
}

enum InitAction {
    Unit,
    InspectThenUnit,
    InspectTwiceThenUnit,
    InspectThenTrap,
}

/// Five exact ABI bindings let each negative case reach its intended policy.
/// WAT success here says nothing about any original Python method or Profile.
fn policy_component(
    engine: &WasmEngine,
    payload: &str,
    init: InitAction,
    root_inspects: bool,
) -> Result<Component> {
    let escaped: String = payload
        .bytes()
        .map(|byte| format!("\\{byte:02x}"))
        .collect();
    let inspect = format!(
        "i32.const 2048 i32.const {} i32.const 64 call $inspect",
        payload.len()
    );
    let initialize = match init {
        InitAction::Unit => String::new(),
        InitAction::InspectThenUnit => inspect.clone(),
        InitAction::InspectTwiceThenUnit => format!("{inspect} {inspect}"),
        InitAction::InspectThenTrap => format!("{inspect} unreachable"),
    };
    let source = r#"(component
      (type $native (instance
        (export "inventory" (func (result string)))
        (export "inspect" (func (param "payload" string) (result string)))))
      (import "tobkiri-native:harness/bindings@1.0.0" (instance $native (type $native)))
      (alias export $native "inspect" (func $inspect))
      (type $code (enum "not-initialized" "already-initialized" "invalid-frame"
        "invalid-reference" "unsupported-value" "method-raised"))
      (type $error (record (field "code" $code) (field "message" string) (field "details" string)))
      (type $unit (result (error $error)))
      (type $text (result string (error $error)))
      (core module $storage
        (memory (export "memory") 1)
        (global $next (mut i32) (i32.const 8192))
        (data (i32.const 2048) "@PAYLOAD@")
        (func (export "realloc") (param i32 i32 i32 i32) (result i32)
          local.get 0 local.get 1 i32.or if unreachable end
          local.get 2 i32.const 1 i32.ne if unreachable end
          global.get $next
          global.get $next local.get 3 i32.add global.set $next))
      (core instance $storage (instantiate $storage))
      (alias core export $storage "memory" (core memory $memory))
      (alias core export $storage "realloc" (core func $realloc))
      (core func $inspect-lowered (canon lower (func $inspect) (memory $memory) (realloc $realloc)))
      (core module $run
        (import "native" "inspect" (func $inspect (param i32 i32 i32)))
        (func (export "init") (param i32 i32) (result i32)
          @INITIALIZE@
          i32.const 128)
        (func (export "text") (param i32 i32) (result i32)
          @ROOT@
          i32.const 128))
      (core instance $run (instantiate $run
        (with "native" (instance (export "inspect" (func $inspect-lowered))))))
      (alias core export $run "init" (core func $init))
      (alias core export $run "text" (core func $text))
      (func $init (param "payload" string) (result $unit)
        (canon lift (core func $init) (memory $memory) (realloc $realloc)))
      (func $text (param "payload" string) (result $text)
        (canon lift (core func $text) (memory $memory) (realloc $realloc)))
      (instance $runtime
        (export "error-code" (type $code)) (export "error" (type $error))
        (export "init" (func $init)) (export "bootstrap-profile-id" (func $text))
        (export "host-resource-root" (func $text))
        (export "bundled-profile-root" (func $text)))
      (instance $transport
        (export "error-code" (type $code)) (export "error" (type $error))
        (export "describe-value" (func $text)))
      (export "tobkiri-ui:harness-native/runtime@1.0.0" (instance $runtime))
      (export "tobkiri-ui:harness-native/transport@1.0.0" (instance $transport)))"#
        .replace("@PAYLOAD@", &escaped)
        .replace("@INITIALIZE@", &initialize)
        .replace("@ROOT@", if root_inspects { &inspect } else { "" });
    Ok(Component::new(engine.engine(), wat::parse_str(source)?)?)
}

fn diagnostic(invocation: &NativeSetupInvocation) -> serde_json::Value {
    let provenance = invocation.provenance();
    serde_json::json!({
        "format": "io.tobkiri.native-init-diagnostic.v1",
        "scope": [format!("{:x}", provenance.account_generation),
                  format!("{:x}", provenance.invocation), "1", "1"],
        "phase": "original-factory",
        "kind": "module-not-found-error",
        "args": ["original missing module"],
        "fields": {"name": "original.module", "path": null},
        "locations": [["original.py", "original_factory", 17]],
        "omitted": false,
    })
}

#[tokio::test(flavor = "current_thread")]
async fn even_an_abi_valid_component_cannot_inspect_during_metadata_bootstrap() -> Result<()> {
    let temporary = tempfile::tempdir()?;
    let root = temporary.path().canonicalize()?;
    let selected = selected(root.clone())?;
    let owner = RuntimeOwner::open(root.join("runtime-data"))?;
    let invocation = owner.begin_native_setup()?;
    let engine = WasmEngine::new_with_control(invocation.control())?;
    let component = policy_component(&engine, "{}", InitAction::InspectThenUnit, false)?;
    let failure =
        match NativeBootstrap::prepare(&engine, &component, invocation, selected, &INDEX).await {
            Err(failure) => failure,
            Ok(_) => bail!("hostile metadata component did not fail execution"),
        };
    let original = format!("{failure:#}");
    assert!(
        original.contains("native inspection is unavailable in the metadata-only bootstrap phase"),
        "failure must come from actual native inspection rejection: {original}"
    );
    Ok(())
}

#[tokio::test(flavor = "current_thread")]
async fn init_observation_and_actual_outer_trap_are_retained_separately() -> Result<()> {
    let temporary = tempfile::tempdir()?;
    let root = temporary.path().canonicalize()?;
    let owner = RuntimeOwner::open(root.join("runtime-data"))?;
    let invocation = owner.begin_native_setup()?;
    let engine = WasmEngine::new_with_control(invocation.control())?;
    let payload = serde_json::to_string(&diagnostic(&invocation))?;
    let component = policy_component(&engine, &payload, InitAction::InspectThenTrap, false)?;
    let failure =
        match NativeBootstrap::prepare(&engine, &component, invocation, selected(root)?, &INDEX)
            .await
        {
            Err(failure) => failure,
            Ok(_) => bail!("a real unreachable after observation must remain an outer failure"),
        };
    let recorded = failure
        .downcast_ref::<crate::OriginalInitializationFailure>()
        .context("the actual pre-trap observation was lost")?;
    assert_eq!(
        recorded
            .diagnostic
            .as_ref()
            .context("diagnostic missing")?
            .as_str()?,
        payload
    );
    assert_eq!(recorded.trace.host_phase, "export-trapped");
    assert_eq!(recorded.trace.inspect_entries, 1);
    assert_eq!(recorded.trace.inspect_validated, 1);
    let observation = recorded
        .observation
        .context("same Store observation missing")?;
    assert!(observation.unreachable_trap && observation.store_retired);
    assert!(recorded.original.downcast_ref::<wasmtime::Trap>().is_some());
    assert!(!format!("{:#}", recorded.original).contains("original missing module"));
    assert!(recorded.capacity.is_some());
    Ok(())
}

#[tokio::test(flavor = "current_thread")]
async fn an_init_observation_cannot_be_followed_by_forged_unit_success() -> Result<()> {
    let temporary = tempfile::tempdir()?;
    let root = temporary.path().canonicalize()?;
    let owner = RuntimeOwner::open(root.join("runtime-data"))?;
    let invocation = owner.begin_native_setup()?;
    let engine = WasmEngine::new_with_control(invocation.control())?;
    let payload = serde_json::to_string(&diagnostic(&invocation))?;
    let component = policy_component(&engine, &payload, InitAction::InspectThenUnit, false)?;
    let failure =
        match NativeBootstrap::prepare(&engine, &component, invocation, selected(root)?, &INDEX)
            .await
        {
            Err(failure) => failure,
            Ok(_) => bail!("a reported initialization failure cannot become unit success"),
        };
    let recorded = failure
        .downcast_ref::<crate::OriginalInitializationFailure>()
        .context("the rejected success lost its actual observation")?;
    assert_eq!(
        recorded
            .diagnostic
            .as_ref()
            .context("diagnostic missing")?
            .as_str()?,
        payload
    );
    assert_eq!(
        recorded.original.to_string(),
        "init returned success after reporting an original failure"
    );
    assert!(
        !recorded
            .observation
            .context("same Store observation missing")?
            .unreachable_trap
    );
    Ok(())
}

#[tokio::test(flavor = "current_thread")]
async fn forged_init_diagnostics_never_create_a_preprint_exception_observation() -> Result<()> {
    for forgery in [
        "scope",
        "unknown-key",
        "phase",
        "kind",
        "field",
        "args",
        "integer",
        "scalar",
        "location",
        "unrepresented",
        "string",
        "size",
        "checkpoint-stage",
        "checkpoint-sequence",
        "checkpoint-extra",
    ] {
        let temporary = tempfile::tempdir()?;
        let root = temporary.path().canonicalize()?;
        let owner = RuntimeOwner::open(root.join("runtime-data"))?;
        let invocation = owner.begin_native_setup()?;
        let engine = WasmEngine::new_with_control(invocation.control())?;
        let mut frame = diagnostic(&invocation);
        match forgery {
            "scope" => frame["scope"][3] = serde_json::json!("2"),
            "unknown-key" => frame["authority"] = serde_json::json!(true),
            "phase" => frame["phase"] = serde_json::json!("ready"),
            "kind" => frame["kind"] = serde_json::json!("custom-error"),
            "field" => frame["fields"]["obj"] = serde_json::json!("not admitted"),
            "args" => frame["args"] = serde_json::json!([1, 2, 3, 4, 5]),
            "integer" => frame["args"] = serde_json::json!([9_007_199_254_740_992_u64]),
            "scalar" => frame["args"] = serde_json::json!([{"private": "object"}]),
            "location" => frame["locations"] = serde_json::json!([["a", "b", 0]]),
            "unrepresented" => frame["kind"] = serde_json::json!("unrepresented-exception"),
            "string" => frame["args"] = serde_json::json!(["x".repeat(513)]),
            "size" => frame["args"] = serde_json::json!(["x".repeat(4096)]),
            "checkpoint-stage" | "checkpoint-sequence" | "checkpoint-extra" => {
                frame = serde_json::json!({
                    "format": "io.tobkiri.native-init-checkpoint.v1",
                    "sequence": 1, "stage": "dispatcher-entry",
                });
                match forgery {
                    "checkpoint-stage" => frame["stage"] = serde_json::json!("factory-returned"),
                    "checkpoint-sequence" => frame["sequence"] = serde_json::json!(8),
                    _ => frame["authority"] = serde_json::json!(true),
                }
            }
            _ => unreachable!(),
        }
        let payload = serde_json::to_string(&frame)?;
        let component = policy_component(&engine, &payload, InitAction::InspectThenTrap, false)?;
        let failure = match NativeBootstrap::prepare(
            &engine,
            &component,
            invocation,
            selected(root)?,
            &INDEX,
        )
        .await
        {
            Err(failure) => failure,
            Ok(_) => bail!("forged diagnostic was accepted: {forgery}"),
        };
        let recorded = failure
            .downcast_ref::<crate::OriginalInitializationFailure>()
            .context("actual failed-init observations missing")?;
        assert!(
            recorded.diagnostic.is_none(),
            "forgery created an original observation: {forgery}"
        );
        assert_eq!(recorded.trace.inspect_entries, 1);
        assert_eq!(recorded.trace.inspect_validated, 0);
        assert_eq!(recorded.trace.checkpoint_count, 0);
        if forgery == "integer" {
            // The original DATA parser rejects unsafe integers before policy;
            // preserve that real outer failure rather than normalizing it.
            assert!(
                matches!(
                    recorded
                        .original
                        .downcast_ref::<tobkiri_canon::AllocationError>(),
                    Some(tobkiri_canon::AllocationError::Invalid)
                ),
                "unsafe integer lost its actual codec failure: {failure:#}"
            );
        } else {
            assert!(
                format!("{failure:#}").contains(
                    "native inspection is unavailable in the metadata-only bootstrap phase"
                ),
                "forgery failed before reaching native policy: {forgery}: {failure:#}"
            );
        }
    }
    Ok(())
}

#[tokio::test(flavor = "current_thread")]
async fn init_checkpoint_is_data_and_cannot_authorize_root_inspection() -> Result<()> {
    let temporary = tempfile::tempdir()?;
    let root = temporary.path().canonicalize()?;
    let owner = RuntimeOwner::open(root.join("runtime-data"))?;
    let invocation = owner.begin_native_setup()?;
    let engine = WasmEngine::new_with_control(invocation.control())?;
    let payload = r#"{"format":"io.tobkiri.native-init-checkpoint.v1","sequence":1,"stage":"dispatcher-entry"}"#;
    let component = policy_component(&engine, payload, InitAction::InspectThenUnit, true)?;
    let mut runtime = NativeBootstrap::prepare(
        &engine,
        &component,
        invocation,
        selected(root.clone())?,
        &INDEX,
    )
    .await?
    .map_err(|_| anyhow::anyhow!("checkpoint became a domain error"))?;
    let trace = runtime.initialization_trace();
    assert_eq!(trace.checkpoint_sequence, 1);
    assert_eq!(trace.checkpoint_count, 1);
    assert_eq!(trace.guest_checkpoint, Some("dispatcher-entry"));
    assert_eq!(trace.inspect_entries, 1);
    assert_eq!(trace.inspect_validated, 1);
    assert_eq!(trace.host_phase, "export-returned");
    // The exact same DATA must not acquire a root or source-operation right.
    assert!(runtime.host_resource_root().await.is_err());
    assert_eq!(runtime.initialization_trace().checkpoint_count, 1);
    runtime.finish().await?;
    let invocation = owner.begin_native_setup()?;
    let component = policy_component(&engine, payload, InitAction::InspectTwiceThenUnit, false)?;
    let failure =
        match NativeBootstrap::prepare(&engine, &component, invocation, selected(root)?, &INDEX)
            .await
        {
            Err(failure) => failure,
            Ok(_) => bail!("replayed checkpoint did not fail native validation"),
        };
    let recorded = failure
        .downcast_ref::<crate::OriginalInitializationFailure>()
        .context("replayed checkpoint lost the actual execution failure")?;
    assert!(recorded.diagnostic.is_none());
    assert_eq!(recorded.trace.inspect_entries, 2);
    assert_eq!(recorded.trace.checkpoint_count, 1);
    assert_eq!(
        recorded.trace.last_rejection,
        crate::InitializationRejection::Sequence
    );
    Ok(())
}

struct RootBarrier {
    calls: AtomicUsize,
    entered: Mutex<Option<tokio::sync::oneshot::Sender<()>>>,
    release: Mutex<mpsc::Receiver<()>>,
    released: AtomicBool,
}
impl RootBarrier {
    fn enter(&self) -> std::result::Result<(), NativeDataIoError> {
        // The first projection creates actual inventory. Only the reached
        // getter's physical IO callback is suspended by this fixture barrier.
        if self.calls.fetch_add(1, Ordering::AcqRel) == 1 {
            if let Some(entered) = self
                .entered
                .lock()
                .map_err(|_| NativeDataIoError::Io)?
                .take()
            {
                let _ = entered.send(());
            }
            self.release
                .lock()
                .map_err(|_| NativeDataIoError::Io)?
                .recv()
                .map_err(|_| NativeDataIoError::Io)?;
            self.released.store(true, Ordering::Release);
        }
        Ok(())
    }
}
struct ReleaseWorker(Option<mpsc::Sender<()>>);
impl ReleaseWorker {
    fn release(&mut self) {
        if let Some(sender) = self.0.take() {
            let _ = sender.send(());
        }
    }
}
impl Drop for ReleaseWorker {
    fn drop(&mut self) {
        self.release();
    }
}

#[tokio::test(flavor = "current_thread")]
async fn dropping_a_suspended_root_getter_closes_phase_and_the_same_physical_io() -> Result<()> {
    let temporary = tempfile::tempdir()?;
    let root = temporary.path().canonicalize()?;
    let owner = RuntimeOwner::open(root.join("runtime-data"))?;
    let invocation = owner.begin_native_setup()?;
    let provenance = invocation.provenance();
    let payload = serde_json::to_string(&serde_json::json!({
        "format": "io.tobkiri.native-inspection-request.v1",
        "scope": [format!("{:x}", provenance.account_generation),
                  format!("{:x}", provenance.invocation), "1", "1"],
        "target": {"binding": "module_0", "steps": []},
        "operation": "resolve", "args": [false],
    }))?;
    let engine = WasmEngine::new_with_control(invocation.control())?;
    let component = policy_component(&engine, &payload, InitAction::Unit, true)?;
    let (entered_tx, entered_rx) = tokio::sync::oneshot::channel();
    let (release_tx, release_rx) = mpsc::channel();
    let mut release = ReleaseWorker(Some(release_tx));
    let barrier = Arc::new(RootBarrier {
        calls: AtomicUsize::new(0),
        entered: Mutex::new(Some(entered_tx)),
        release: Mutex::new(release_rx),
        released: AtomicBool::new(false),
    });
    let mut source = selected(root)?;
    source.metadata = true;
    source.barrier = Some(barrier.clone());
    let mut runner =
        match NativeBootstrap::prepare(&engine, &component, invocation, source, &PROFILE_INDEX)
            .await?
        {
            Ok(runner) => runner,
            Err(_) => bail!("the mechanism fixture did not finish its unit initialization"),
        };
    let work = runner.work.clone();
    let io = runner.io.clone();
    let mut call = Box::pin(runner.host_resource_root());
    tokio::select! {
        _ = call.as_mut() => bail!("getter completed before its physical worker barrier"),
        entered = entered_rx => entered.context("actual getter worker did not enter")?,
    }
    assert_eq!(work.get().phase.load(Ordering::Acquire), HOST_ROOT);
    drop(call);
    assert_eq!(work.get().phase.load(Ordering::Acquire), CLOSED);
    assert!(matches!(
        io.inspect(|_| Ok(())).await,
        Err(NativeDataIoError::Retired)
    ));
    assert!(runner.copy_input(b"{}").is_err());
    let mut cleanup = Box::pin(io.retire_and_wait());
    let pending =
        std::future::poll_fn(|context| Poll::Ready(cleanup.as_mut().poll(context).is_pending()))
            .await;
    assert!(
        pending,
        "physical cleanup completed before the blocked worker was released"
    );
    assert!(!barrier.released.load(Ordering::Acquire));
    release.release();
    cleanup.await?;
    assert!(barrier.released.load(Ordering::Acquire));
    assert_eq!(work.get().phase.load(Ordering::Acquire), CLOSED);
    assert!(runner.host_resource_root().await.is_err());
    runner.finish().await?;
    Ok(())
}
