//! The actual native-only component runner and original source metadata seam.
//!
//! Only initialization, the original first Profile-id method and reviewed DATA
//! description are exposed, followed by the two original opaque root getters
//! only when composition explicitly admits their fixed module operation.
//! Initialization permits bounded failure observations, never filesystem IO.
use crate::bootstrap_metadata::{self, BootstrapInputOwner, BootstrapModuleIndex, Scope};
use anyhow::{Context, Result, bail};
use std::sync::{
    Arc, OnceLock,
    atomic::{AtomicU8, AtomicU64, Ordering},
};
use tobkiri_canon::{AllocationBudget, ControlHandle, CoreBuffer, CoreValue, MAX_BYTES};
use tobkiri_gate::{
    CapacityKind, ControlKind, ControlReservation, NativeSetupInvocation, NativeSetupReservation,
};
use tobkiri_wasm::{
    NativeDataBinding, NativeDataError, NativeDataErrorContract, NativeDataExport, NativeDataIo,
    NativeDataIoError, NativeDataProgram, NativeDataResultContract, NativeDataSuccessType,
    NativeDataValue, NativeImportBinding, NativeImportContext, NativeImportInput,
    NativeImportOperation, NativeImportState, WasmEngine,
};
use wasmtime::component::Component;

const CONSTRUCTING: u8 = 0;
const INITIALIZING: u8 = 1;
const READY: u8 = 2;
const HOST_ROOT: u8 = 3;
const BUNDLE_ROOT: u8 = 4;
const CLOSED: u8 = 5;
const INTERFACE: &str = "tobkiri-native:harness/bindings@1.0.0";
const RUNTIME: &str = "tobkiri-ui:harness-native/runtime@1.0.0";
const TRANSPORT: &str = "tobkiri-ui:harness-native/transport@1.0.0";

/// Preserve both the original execution failure and failed physical cleanup.
/// This is native diagnostic data, never a replacement guest/domain outcome.
#[derive(Debug)]
pub struct BootstrapCleanupFailure {
    pub original: anyhow::Error,
    pub cleanup: anyhow::Error,
}
impl std::fmt::Display for BootstrapCleanupFailure {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("native execution failed and physical cleanup was not confirmed")
    }
}
impl std::error::Error for BootstrapCleanupFailure {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        Some(self.original.as_ref())
    }
}

struct Work<S> {
    source: S,
    invocation: NativeSetupInvocation,
    inventory: OnceLock<CoreBuffer>,
    phase: AtomicU8,
    inspections: AtomicU64,
    index: &'static BootstrapModuleIndex,
    metadata_policy: Option<&'static crate::ModuleMetadataPolicy>,
    source_occurrence: AtomicU8,
    result_identity: AtomicU64,
    first_host_resolution: OnceLock<CoreBuffer>,
    first_bundle_resolution: OnceLock<CoreBuffer>,
    init_diagnostic: OnceLock<CoreBuffer>,
}
struct Funding {
    _native: NativeSetupReservation,
    _control: ControlReservation,
}
struct State<S> {
    work: ControlHandle<Work<S>>,
}

// Dropping a suspended invocation already destroys NativeDataProgram's active
// Store. Also close this adapter's admission and the SAME physical IO owner;
// restoring READY could re-authorize an old, still-joining worker.
struct RootCallGuard<S> {
    work: ControlHandle<Work<S>>,
    io: NativeDataIo,
    completed: bool,
}
impl<S> Drop for RootCallGuard<S> {
    fn drop(&mut self) {
        if !self.completed {
            self.work.get().phase.store(CLOSED, Ordering::Release);
            self.io.retire();
        }
    }
}
impl<S: BootstrapInputOwner> NativeImportState for State<S> {
    fn native_import_invocation(&self) -> &NativeSetupInvocation {
        &self.work.get().invocation
    }
}
impl<S: BootstrapInputOwner> State<S> {
    const BINDINGS: [NativeImportBinding<Self>; 2] = [
        NativeImportBinding {
            interface: INTERFACE,
            function: "inventory",
            input: NativeImportInput::None,
            prepare: inventory::<S>,
        },
        NativeImportBinding {
            interface: INTERFACE,
            function: "inspect",
            input: NativeImportInput::String,
            prepare: inspect::<S>,
        },
    ];
}

fn inventory<S: BootstrapInputOwner>(
    context: NativeImportContext<'_, State<S>>,
) -> Result<NativeImportOperation> {
    let work = context.state().work.clone();
    if !context.same_invocation(&work.get().invocation)
        || work.get().phase.load(Ordering::Acquire) != INITIALIZING
    {
        bail!("native bootstrap inventory is not active");
    }
    let custody = work.project(|value| value as &(dyn std::any::Any + Send + Sync));
    context.inspect_retaining(custody, move |output| {
        let state = work.get();
        if !state.invocation.same_invocation(output.invocation())
            || state.phase.load(Ordering::Acquire) != INITIALIZING
        {
            return Err(NativeDataIoError::Retired);
        }
        state.source.check_live()?;
        output.append(
            state
                .inventory
                .get()
                .ok_or(NativeDataIoError::Unavailable)?
                .as_bytes(),
        )?;
        state.source.check_live()?;
        if state.phase.load(Ordering::Acquire) != INITIALIZING {
            return Err(NativeDataIoError::Retired);
        }
        Ok(())
    })
}

fn inspect<S: BootstrapInputOwner>(
    context: NativeImportContext<'_, State<S>>,
) -> Result<NativeImportOperation> {
    let work = context.state().work.clone();
    let state = work.get();
    state.inspections.fetch_add(1, Ordering::Relaxed);
    let phase = state.phase.load(Ordering::Acquire);
    if context.same_invocation(&state.invocation) && phase == INITIALIZING {
        let request = context
            .input()
            .context("native init observation is missing")?;
        if request.len() <= 4096 {
            let frame =
                CoreValue::parse_guest(request.as_bytes(), context.allocation_owners().graph())?;
            if crate::init_diagnostic::valid(&frame, &state.invocation) {
                let request = request.clone();
                let custody = work.project(|value| value as &(dyn std::any::Any + Send + Sync));
                return context.inspect_retaining(custody, move |output| {
                    let state = work.get();
                    if !state.invocation.same_invocation(output.invocation())
                        || state.phase.load(Ordering::Acquire) != INITIALIZING
                    {
                        return Err(NativeDataIoError::Retired);
                    }
                    state.source.check_live()?;
                    state
                        .init_diagnostic
                        .set(request)
                        .map_err(|_| NativeDataIoError::Unavailable)?;
                    // This acknowledgement carries no success or permission.
                    output.append(b"{}")
                });
            }
        }
    }
    if !context.same_invocation(&state.invocation) || !matches!(phase, HOST_ROOT | BUNDLE_ROOT) {
        bail!("native inspection is unavailable in the metadata-only bootstrap phase");
    }
    let policy = state
        .metadata_policy
        .context("original module metadata policy is unavailable")?;
    if state
        .source_occurrence
        .compare_exchange(0, 1, Ordering::AcqRel, Ordering::Acquire)
        .is_err()
    {
        bail!("original getter metadata occurrence was already consumed");
    }
    let request = context
        .input()
        .context("original metadata request is missing")?;
    let frame = CoreValue::parse_guest(request.as_bytes(), context.allocation_owners().graph())?;
    let module = crate::source_metadata::selected(&frame, &state.invocation, state.index, policy)
        .context("metadata request is outside the original source operation")?;
    let custody = work.project(|value| value as &(dyn std::any::Any + Send + Sync));
    context.inspect_retaining(custody, move |output| {
        let state = work.get();
        if !state.invocation.same_invocation(output.invocation())
            || state.phase.load(Ordering::Acquire) != phase
        {
            return Err(NativeDataIoError::Retired);
        }
        state.source.check_live()?;
        let resolved = state
            .source
            .with_roots(|roots| crate::source_metadata::resolve(output, roots.runtime, module))??;
        state.source.check_live()?;
        let identity = state
            .result_identity
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |n| n.checked_add(1))
            .map_err(|_| NativeDataIoError::Capacity)?;
        let mut identifier = [0_u8; 48];
        let mut cursor = std::io::Cursor::new(identifier.as_mut_slice());
        use std::io::Write;
        write!(cursor, "resolved_module_{identity:x}").map_err(|_| NativeDataIoError::Capacity)?;
        let length = cursor.position() as usize;
        let binding = std::str::from_utf8(&identifier[..length])
            .map_err(|_| NativeDataIoError::Unavailable)?;
        let mut response = CoreBuffer::builder(output.allocation_owners().graph(), MAX_BYTES);
        response
            .append(b"{\"format\":\"io.tobkiri.native-inspection-response.v1\",\"scope\":")
            .map_err(|_| NativeDataIoError::Capacity)?;
        Scope::original(&state.invocation)
            .write(&mut response)
            .map_err(|_| NativeDataIoError::Capacity)?;
        response
            .append(b",\"result\":{\"kind\":\"path\",\"binding\":")
            .map_err(|_| NativeDataIoError::Capacity)?;
        bootstrap_metadata::string_parts(&mut response, [binding])
            .map_err(|_| NativeDataIoError::Capacity)?;
        response
            .append(b",\"value\":")
            .map_err(|_| NativeDataIoError::Capacity)?;
        bootstrap_metadata::string_parts(
            &mut response,
            [resolved
                .as_str()
                .map_err(|_| NativeDataIoError::Unavailable)?],
        )
        .map_err(|_| NativeDataIoError::Capacity)?;
        response
            .append(b"}}")
            .map_err(|_| NativeDataIoError::Capacity)?;
        output.append(
            response
                .finish()
                .map_err(|_| NativeDataIoError::Capacity)?
                .as_bytes(),
        )?;
        // Native-only observation of the real module resolution, not a guest
        // path disclosure or a capability for subsequent resource operations.
        let first = if phase == HOST_ROOT {
            &state.first_host_resolution
        } else {
            &state.first_bundle_resolution
        };
        let _ = first.set(resolved);
        if state.phase.load(Ordering::Acquire) != phase {
            return Err(NativeDataIoError::Retired);
        }
        Ok(())
    })
}

fn contract(success: NativeDataSuccessType) -> NativeDataResultContract<'static> {
    NativeDataResultContract {
        success,
        error: NativeDataErrorContract {
            code_field: "code",
            message_field: "message",
            details_field: Some("details"),
            cases: &[
                "not-initialized",
                "already-initialized",
                "invalid-frame",
                "invalid-reference",
                "unsupported-value",
                "method-raised",
            ],
        },
    }
}

/// One real component/Store and original invocation, initialized exactly once.
///
/// This deliberately cannot execute catalog/FS/activation/effectful methods yet.
/// Their actual source policies and adapters must exist before exposing them.
pub struct NativeBootstrap<S: BootstrapInputOwner> {
    program: Option<NativeDataProgram<State<S>>>,
    work: ControlHandle<Work<S>>,
    io: NativeDataIo,
    call: CoreBuffer,
    input_budget: Arc<dyn AllocationBudget>,
}

impl<S: BootstrapInputOwner> NativeBootstrap<S> {
    /// Bind actual native metadata before instantiation and explicit Runtime.init.
    /// Source ownership and the real invocation come from trusted composition;
    /// a serialized identity or source digest cannot construct either here.
    pub async fn prepare(
        engine: &WasmEngine,
        component: &Component,
        invocation: NativeSetupInvocation,
        source: S,
        index: &'static BootstrapModuleIndex,
    ) -> Result<std::result::Result<Self, NativeDataError>> {
        source.check_live()?;
        let bytes = u64::try_from(ControlHandle::<Work<S>>::storage_bytes::<Funding>())?;
        let native = invocation.reserve(CapacityKind::VfsBuffer, bytes)?;
        let control = invocation.control().reserve(ControlKind::Frame, bytes)?;
        let metadata_policy = source.original_module_metadata_policy();
        let work = ControlHandle::try_new_precharged(
            Work {
                source,
                invocation: invocation.clone(),
                inventory: OnceLock::new(),
                phase: AtomicU8::new(CONSTRUCTING),
                inspections: AtomicU64::new(0),
                index,
                metadata_policy,
                source_occurrence: AtomicU8::new(0),
                result_identity: AtomicU64::new(1),
                first_host_resolution: OnceLock::new(),
                first_bundle_resolution: OnceLock::new(),
                init_diagnostic: OnceLock::new(),
            },
            Funding {
                _native: native,
                _control: control,
            },
        )?;
        let store = engine.native_setup_store_with_imports(
            invocation.clone(),
            State { work: work.clone() },
            &State::<S>::BINDINGS,
        )?;
        let io = store
            .native_data_io()
            .context("native bootstrap IO was not bound")?
            .clone();
        let scope = Scope::original(&invocation);
        let budget = store.allocation_owners().graph().clone();
        let selected = work
            .get()
            .source
            .with_roots(|roots| bootstrap_metadata::inventory(roots, index, scope, &budget))??;
        if work.get().inventory.set(selected).is_err() {
            bail!("native bootstrap inventory was already bound");
        }
        let init = scope.frame(&budget, true)?;
        let call = scope.frame(&budget, false)?;
        let bindings = [
            NativeDataBinding {
                export: NativeDataExport::new(Some(RUNTIME), "init"),
                result: Some(contract(NativeDataSuccessType::Unit)),
            },
            NativeDataBinding {
                export: NativeDataExport::new(Some(RUNTIME), "bootstrap-profile-id"),
                result: Some(contract(NativeDataSuccessType::String)),
            },
            NativeDataBinding {
                export: NativeDataExport::new(Some(TRANSPORT), "describe-value"),
                result: Some(contract(NativeDataSuccessType::String)),
            },
            NativeDataBinding {
                export: NativeDataExport::new(Some(RUNTIME), "host-resource-root"),
                result: Some(contract(NativeDataSuccessType::String)),
            },
            NativeDataBinding {
                export: NativeDataExport::new(Some(RUNTIME), "bundled-profile-root"),
                result: Some(contract(NativeDataSuccessType::String)),
            },
        ];
        let program = match store.prepare_data_program(component, &bindings).await {
            Ok(program) => program,
            Err(original) => {
                work.get().phase.store(CLOSED, Ordering::Release);
                if let Err(cleanup) = io.retire_and_wait().await {
                    return Err(BootstrapCleanupFailure {
                        original,
                        cleanup: cleanup.into(),
                    }
                    .into());
                }
                return Err(original);
            }
        };
        let mut runner = Self {
            program: Some(program),
            work,
            io,
            call,
            input_budget: budget,
        };
        runner
            .work
            .get()
            .phase
            .store(INITIALIZING, Ordering::Release);
        let result = runner
            .program
            .as_mut()
            .context("native program was retired")?
            .invoke(0, Some(&init))
            .await;
        drop(init);
        match result {
            Ok(Ok(NativeDataValue::Unit)) => {
                if runner.work.get().init_diagnostic.get().is_some() {
                    let error = runner.initialization_failure(anyhow::anyhow!(
                        "init returned success after reporting an original failure"
                    ));
                    return Err(runner.retire_failure(error).await);
                }
                if let Err(original) = runner.work.get().source.check_live() {
                    return Err(runner.retire_failure(original.into()).await);
                }
                runner.work.get().phase.store(READY, Ordering::Release);
                Ok(Ok(runner))
            }
            Ok(Err(domain)) => {
                runner.finish().await?;
                Ok(Err(domain))
            }
            Ok(Ok(NativeDataValue::String(_))) => {
                runner.finish().await?;
                bail!("native bootstrap initialization returned a non-unit value")
            }
            Err(original) => {
                let original = runner.initialization_failure(original);
                if let Err(cleanup) = runner.finish().await {
                    return Err(BootstrapCleanupFailure { original, cleanup }.into());
                }
                Err(original)
            }
        }
    }

    /// Execute the original method, returning its owned typed value envelope.
    /// The reference belongs to this Store; the envelope is not itself a grant.
    pub async fn bootstrap_profile_id(
        &mut self,
    ) -> Result<std::result::Result<CoreBuffer, NativeDataError>> {
        let input = self.call.clone();
        self.string_call(1, &input).await
    }

    /// Describe only an actually issued DATA reference using the original codec.
    pub async fn describe_value(
        &mut self,
        request: &CoreBuffer,
    ) -> Result<std::result::Result<CoreBuffer, NativeDataError>> {
        self.string_call(2, request).await
    }

    /// Call the original HostPort getter, retaining its actual opaque Path.
    pub async fn host_resource_root(
        &mut self,
    ) -> Result<std::result::Result<CoreBuffer, NativeDataError>> {
        self.root_call(3, HOST_ROOT).await
    }

    /// Call the original packaged-root getter. Production must still preserve
    /// its development branch and binding validation before consuming this ref.
    pub async fn bundled_profile_root(
        &mut self,
    ) -> Result<std::result::Result<CoreBuffer, NativeDataError>> {
        self.root_call(4, BUNDLE_ROOT).await
    }

    async fn root_call(
        &mut self,
        slot: usize,
        phase: u8,
    ) -> Result<std::result::Result<CoreBuffer, NativeDataError>> {
        if self.work.get().phase.load(Ordering::Acquire) != READY
            || self.work.get().metadata_policy.is_none()
        {
            bail!("original source metadata is not admitted");
        }
        self.work.get().source.check_live()?;
        let mut guard = RootCallGuard {
            work: self.work.clone(),
            io: self.io.clone(),
            completed: false,
        };
        self.work
            .get()
            .source_occurrence
            .store(0, Ordering::Release);
        self.work.get().phase.store(phase, Ordering::Release);
        let input = self.call.clone();
        let result = self.string_call_in_phase(slot, &input, phase).await;
        if self.work.get().phase.load(Ordering::Acquire) == phase {
            self.work.get().phase.store(READY, Ordering::Release);
        }
        guard.completed = true;
        result
    }

    /// Copy DATA into this original codec account without issuing a reference,
    /// source capability, permission or renewed budget.
    pub fn copy_input(&self, bytes: &[u8]) -> Result<CoreBuffer> {
        if self.work.get().phase.load(Ordering::Acquire) != READY || bytes.len() > MAX_BYTES {
            bail!("native bootstrap input is unavailable or exceeds the envelope");
        }
        self.work.get().source.check_live()?;
        Ok(CoreBuffer::copy_from(bytes, &self.input_budget)?)
    }

    async fn string_call(
        &mut self,
        slot: usize,
        input: &CoreBuffer,
    ) -> Result<std::result::Result<CoreBuffer, NativeDataError>> {
        self.string_call_in_phase(slot, input, READY).await
    }

    async fn string_call_in_phase(
        &mut self,
        slot: usize,
        input: &CoreBuffer,
        phase: u8,
    ) -> Result<std::result::Result<CoreBuffer, NativeDataError>> {
        if self.work.get().phase.load(Ordering::Acquire) != phase {
            bail!("native bootstrap is not initialized");
        }
        if let Err(original) = self.work.get().source.check_live() {
            return Err(self.retire_failure(original.into()).await);
        }
        let result = self
            .program
            .as_mut()
            .context("native bootstrap was retired")?
            .invoke(slot, Some(input))
            .await;
        let result = match result {
            Ok(result) => result,
            Err(original) => return Err(self.retire_failure(original).await),
        };
        if let Err(original) = self.work.get().source.check_live() {
            return Err(self.retire_failure(original.into()).await);
        }
        match result {
            Ok(NativeDataValue::String(value)) => Ok(Ok(value)),
            Err(error) => Ok(Err(error)),
            Ok(NativeDataValue::Unit) => Err(self
                .retire_failure(anyhow::anyhow!(
                    "native method returned a non-string envelope"
                ))
                .await),
        }
    }

    async fn retire_failure(&mut self, original: anyhow::Error) -> anyhow::Error {
        self.work.get().phase.store(CLOSED, Ordering::Release);
        if let Some(mut program) = self.program.take() {
            program.retire();
        }
        match self.io.retire_and_wait().await {
            Ok(()) => original,
            Err(cleanup) => BootstrapCleanupFailure {
                original,
                cleanup: cleanup.into(),
            }
            .into(),
        }
    }

    fn initialization_failure(&self, original: anyhow::Error) -> anyhow::Error {
        match self.work.get().init_diagnostic.get() {
            Some(diagnostic) => crate::OriginalInitializationFailure {
                original,
                diagnostic: diagnostic.clone(),
                observation: self.program.as_ref().map(|p| p.observation()),
                capacity: self.work.get().invocation.capacity_snapshot().ok(),
                elapsed_micros: self
                    .work
                    .get()
                    .invocation
                    .started_at()
                    .elapsed()
                    .as_micros(),
            }
            .into(),
            None => original,
        }
    }

    /// Native import observations, including failed-init diagnostics. Pure
    /// successful initialization and Profile-id execution must not inspect.
    pub fn inspection_attempts(&self) -> u64 {
        self.work.get().inspections.load(Ordering::Acquire)
    }

    /// Actual module metadata observations for trusted native diagnostics. These
    /// do not prove the opaque getter's parents calculation or resource reads.
    pub fn first_source_resolutions(&self) -> (Option<&CoreBuffer>, Option<&CoreBuffer>) {
        (
            self.work.get().first_host_resolution.get(),
            self.work.get().first_bundle_resolution.get(),
        )
    }

    /// Sample fuel from this same Store; no execution allowance is replaced.
    pub fn fuel_remaining(&self) -> Result<u64> {
        self.program
            .as_ref()
            .context("native bootstrap was retired")?
            .fuel_remaining()
    }

    /// Borrow the original DATA codec account for native frame codecs. Cloning
    /// its handle retains these same limits, deadline and physical accounting.
    pub fn data_budget(&self) -> &Arc<dyn AllocationBudget> {
        &self.input_budget
    }

    /// Retire the actual Store and await original native physical cleanup.
    pub async fn finish(mut self) -> Result<()> {
        self.work.get().phase.store(CLOSED, Ordering::Release);
        if let Some(mut program) = self.program.take() {
            program.retire();
        }
        self.io.retire_and_wait().await?;
        Ok(())
    }
}

#[cfg(all(
    test,
    feature = "bootstrap-ci",
    target_os = "macos",
    target_arch = "aarch64"
))]
#[path = "bootstrap_actual_ci.rs"]
mod actual_ci;
#[cfg(all(test, target_os = "macos", target_arch = "aarch64"))]
#[path = "bootstrap_policy_tests.rs"]
mod policy_tests;
impl<S: BootstrapInputOwner> Drop for NativeBootstrap<S> {
    fn drop(&mut self) {
        self.work.get().phase.store(CLOSED, Ordering::Release);
        if let Some(mut program) = self.program.take() {
            program.retire();
        }
        self.io.retire();
        // Any actual worker/monitor retains source and funding via inspect_retaining.
    }
}
