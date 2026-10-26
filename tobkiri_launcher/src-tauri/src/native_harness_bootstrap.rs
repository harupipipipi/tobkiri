//! Original-composition input adapter; no replacement window or startup probe.
//!
//! This opt-in constructor is not yet routed from the original production
//! operation. Keep that route unchanged until actual component/flow parity.
use crate::native_harness_host_inputs::{with_original_host_inputs, NativeHostSourceRole};
use anyhow::{anyhow, bail, Result};
use std::sync::{Arc, Mutex};
use tauri::{AppHandle, Manager};
use tobkiri_core::{Core, HostIo, NativeSetupInvocation, RuntimeOwner};
use tobkiri_harness_native::{
    BootstrapInputOwner, BootstrapModuleIndex, BootstrapRoots, ModuleMetadataPolicy,
    NativeBootstrap, NativeHarnessComponent, ORIGINAL_PROFILE_SOURCE_METADATA,
};
use tobkiri_wasm::{NativeDataError, NativeDataIoError, WasmEngine};

enum OwnedRuntime {
    Unopened,
    Native(RuntimeOwner),
    Core(Arc<Core>),
    Closed,
}
impl Default for OwnedRuntime {
    fn default() -> Self {
        Self::Unopened
    }
}

/// Cold registration only. Root IO first occurs at the native operation's
/// reachpoint, using the original selected user_data_dir and one fixed child.
#[derive(Default)]
pub(crate) struct NativeRuntimeState(Mutex<OwnedRuntime>);

impl NativeRuntimeState {
    fn begin(&self, app: &AppHandle) -> Result<NativeSetupInvocation> {
        let mut state = self
            .0
            .lock()
            .map_err(|_| anyhow!("native runtime owner is poisoned"))?;
        if matches!(*state, OwnedRuntime::Unopened) {
            let owner = with_original_host_inputs(app, |inputs| {
                RuntimeOwner::open(inputs.internal_runtime_storage_parent().join("core"))
            })
            .map_err(|_| anyhow!("original native configuration is unavailable"))??;
            *state = OwnedRuntime::Native(owner);
        }
        match &*state {
            OwnedRuntime::Native(owner) => Ok(owner.begin_native_setup()?),
            OwnedRuntime::Core(core) => Ok(core.begin_native_setup()?),
            _ => bail!("native runtime owner is unavailable"),
        }
    }

    /// Future effectful Core composition must consume THIS owner; reopening the
    /// default/another root would replace accounting and is not an alternative.
    pub(crate) fn adopt_core(&self, host: Arc<dyn HostIo>) -> Result<Arc<Core>> {
        let mut state = self
            .0
            .lock()
            .map_err(|_| anyhow!("native runtime owner is poisoned"))?;
        if let OwnedRuntime::Core(core) = &*state {
            return Ok(core.clone());
        }
        let OwnedRuntime::Native(owner) = std::mem::replace(&mut *state, OwnedRuntime::Closed)
        else {
            bail!("native runtime ownership cannot be transferred");
        };
        let core = Arc::new(Core::from_runtime_owner(owner, host)?);
        *state = OwnedRuntime::Core(core.clone());
        Ok(core)
    }
}

/// Actual AppHandle owns the original managed config and shutdown state through
/// worker completion. No serialized roots or cloned configuration replace it.
pub(crate) struct OriginalBootstrapInputs {
    app: AppHandle,
}
impl BootstrapInputOwner for OriginalBootstrapInputs {
    fn original_module_metadata_policy(&self) -> Option<&'static ModuleMetadataPolicy> {
        // Explicit original-operation policy from the domain nativeKit. The
        // borrowed config roots alone do not grant any filesystem operation.
        Some(&ORIGINAL_PROFILE_SOURCE_METADATA)
    }
    fn with_roots<R>(
        &self,
        use_roots: impl for<'a> FnOnce(BootstrapRoots<'a>) -> R,
    ) -> std::result::Result<R, NativeDataIoError> {
        with_original_host_inputs(&self.app, |inputs| {
            use_roots(BootstrapRoots {
                runtime: inputs
                    .root(NativeHostSourceRole::Runtime)
                    .expect("selected runtime"),
                working_directory: inputs
                    .root(NativeHostSourceRole::WorkingDirectory)
                    .expect("selected working directory"),
                development_workspace: inputs.root(NativeHostSourceRole::DevelopmentWorkspace),
            })
        })
        .map_err(|_| NativeDataIoError::Retired)
    }

    fn check_live(&self) -> std::result::Result<(), NativeDataIoError> {
        with_original_host_inputs(&self.app, |_| ()).map_err(|_| NativeDataIoError::Retired)
    }
}

/// The native-only constructor for an already admitted component/index. The
/// selected roots remain metadata. Init permits only bounded observations;
/// later original getters require the exact explicit module metadata policy.
/// No frame or path value can enable general FS, approval or effect authority.
pub(crate) struct OriginalComponentPreparation {
    source: OriginalBootstrapInputs,
    invocation: NativeSetupInvocation,
    engine: WasmEngine,
}

pub(crate) fn prepare_original_component(app: AppHandle) -> Result<OriginalComponentPreparation> {
    let state = app
        .try_state::<NativeRuntimeState>()
        .ok_or_else(|| anyhow!("original runtime owner is not registered"))?;
    let invocation = state.begin(&app)?;
    let engine = WasmEngine::new_with_control(invocation.control())?;
    Ok(OriginalComponentPreparation {
        source: OriginalBootstrapInputs { app: app.clone() },
        invocation,
        engine,
    })
}

impl OriginalComponentPreparation {
    /// The actual owner's engine, for loading the native-selected artifact.
    pub(crate) fn engine(&self) -> &WasmEngine {
        &self.engine
    }

    /// Consume the original preparation; no retry creates another invocation.
    pub(crate) async fn initialize(
        self,
        component: &NativeHarnessComponent,
        index: &'static BootstrapModuleIndex,
    ) -> Result<std::result::Result<NativeBootstrap<OriginalBootstrapInputs>, NativeDataError>>
    {
        NativeBootstrap::prepare(&self.engine, component, self.invocation, self.source, index).await
    }
}
