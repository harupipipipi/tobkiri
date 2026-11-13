//! The actual native-only component runner and original source metadata seam.
//!
//! Only initialization, the original first Profile-id method and reviewed DATA
//! description and fixed denied factory are exposed, with the two opaque root getters
//! only when composition explicitly admits their fixed module operation.
//! Initialization permits bounded failure observations, never filesystem IO.
use crate::bootstrap_metadata::{self, BootstrapInputOwner, BootstrapModuleIndex, Scope};
use crate::init_trace::{InitTrace, InitializationRejection, checkpoint_sequence};
use anyhow::{Context, Result, bail};
use std::sync::{
    Arc, OnceLock,
    atomic::{AtomicU8, AtomicU64, Ordering},
};
use tobkiri_canon::{AllocationBudget, ControlHandle, CoreBuffer, CoreValue, MAX_BYTES};
use tobkiri_gate::{
    CapacityKind, ControlKind, ControlReservation, CoreCancellation, NativeSetupInvocation,
    NativeSetupReservation,
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
const REVIEW: u8 = 6;
const DENIED: u8 = 7;
const IDENTITY: u8 = 8;
const REVIEW_HOST_ROOT: u8 = 9;
const REVIEW_BUNDLE_ROOT: u8 = 10;
const CATALOG_LOAD: u8 = 11;
const CATALOG_PARSE: u8 = 12;
const RESOLVER_SOURCE_LOOKUP: u8 = 13;
const RESOLVER_STATIC_EDGES: u8 = 14;
const RESOLVER_DYNAMIC_EDGES: u8 = 15;
const INTERFACE: &str = "tobkiri-native:harness/bindings@1.0.0";
const RUNTIME: &str = "tobkiri-ui:harness-native/runtime@1.0.0";
const TRANSPORT: &str = "tobkiri-ui:harness-native/transport@1.0.0";

#[path = "bootstrap_denied.rs"]
mod denied;
pub use denied::{
    BootstrapDenied, BootstrapDeniedFailure, BootstrapDeniedRaise, BootstrapDeniedStage,
};
#[path = "bootstrap_identity_observation.rs"]
mod identity_observation;
pub use identity_observation::{
    BootstrapBuiltinKind, BootstrapGetterError, BootstrapIdentityFailure, BootstrapIdentityOutcome,
    BootstrapIdentityStage, BootstrapIdentityValue, BootstrapRecoveryCatch,
};
#[path = "bootstrap_catalog.rs"]
mod catalog;
#[path = "bootstrap_resolver_edges.rs"]
mod resolver_edges;
#[path = "bootstrap_review_continuation.rs"]
mod review_continuation;
#[cfg(all(
    test,
    feature = "catalog-diagnostics-ci",
    target_os = "macos",
    target_arch = "aarch64"
))]
#[path = "bootstrap_schema_diagnostic_ci.rs"]
mod schema_diagnostic_ci;

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

/// The original domain DATA remains DATA when physical cleanup also fails.
/// Neither this envelope nor a domain spelling grants any native operation.
#[derive(Debug)]
pub struct BootstrapDomainCleanupFailure {
    pub domain: NativeDataError,
    pub cleanup: anyhow::Error,
}
impl std::fmt::Display for BootstrapDomainCleanupFailure {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("native domain refusal was followed by unverified physical cleanup")
    }
}
impl std::error::Error for BootstrapDomainCleanupFailure {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        Some(self.cleanup.as_ref())
    }
}

fn initialization_domain_after_cleanup(
    domain: NativeDataError,
    cleanup: Result<()>,
) -> Result<NativeDataError> {
    match cleanup {
        Ok(()) => Ok(domain),
        Err(cleanup) => Err(BootstrapDomainCleanupFailure { domain, cleanup }.into()),
    }
}

/// Copy-only CI metadata from the already sampled real call. It is neither
/// a Store/source handle nor an IO Join/journal-close receipt.
#[cfg(all(test, target_os = "macos", target_arch = "aarch64"))]
#[derive(Clone, Copy)]
pub(crate) struct CiRetiredCall {
    pub(crate) fuel_remaining: Option<u64>,
    pub(crate) post_return: bool,
    pub(crate) store_retired: bool,
    pub(crate) deadline_exceeded: bool,
    pub(crate) phase_at_retire_failure: &'static str,
}
#[cfg(all(test, target_os = "macos", target_arch = "aarch64"))]
impl CiRetiredCall {
    fn capture(phase: u8, observation: tobkiri_wasm::NativeDataObservation) -> Self {
        Self {
            fuel_remaining: observation.fuel_remaining,
            post_return: observation.post_return,
            store_retired: observation.store_retired,
            deadline_exceeded: observation.deadline_exceeded,
            phase_at_retire_failure: Self::phase_name(phase),
        }
    }
    fn phase_name(phase: u8) -> &'static str {
        match phase {
            CONSTRUCTING => "constructing",
            INITIALIZING => "initializing",
            READY => "ready",
            HOST_ROOT => "host-root",
            BUNDLE_ROOT => "bundle-root",
            CLOSED => "closed",
            REVIEW => "review",
            DENIED => "denied",
            IDENTITY => "identity",
            REVIEW_HOST_ROOT => "review-host-root",
            REVIEW_BUNDLE_ROOT => "review-bundle-root",
            CATALOG_LOAD => "catalog-load",
            CATALOG_PARSE => "catalog-manifest-parse",
            RESOLVER_SOURCE_LOOKUP => "resolver-source-lookup",
            RESOLVER_STATIC_EDGES => "resolver-static-edges",
            RESOLVER_DYNAMIC_EDGES => "resolver-dynamic-edges",
            _ => "unknown",
        }
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
    init_trace: InitTrace,
    catalog: std::sync::Mutex<Option<crate::original_authority::catalog::CatalogIo>>,
    catalog_dispatch: OnceLock<catalog::Dispatch<S>>,
    review_root: std::sync::Mutex<Option<catalog::RootResolution>>,
    #[cfg(all(test, target_os = "macos", target_arch = "aarch64"))]
    ci_retired_call: OnceLock<CiRetiredCall>,
    #[cfg(all(
        test,
        feature = "catalog-diagnostics-ci",
        target_os = "macos",
        target_arch = "aarch64"
    ))]
    ci_schema_trace: schema_diagnostic_ci::Trace,
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
    let trace = &work.get().init_trace;
    trace.inventory_entries.fetch_add(1, Ordering::Relaxed);
    if !context.same_invocation(&work.get().invocation) {
        trace.reject(InitializationRejection::Invocation);
        bail!("native bootstrap inventory is not active");
    }
    if work.get().phase.load(Ordering::Acquire) != INITIALIZING {
        trace.reject(InitializationRejection::Phase);
        bail!("native bootstrap inventory is not active");
    }
    trace.inventory_validated.fetch_add(1, Ordering::Relaxed);
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
    state
        .init_trace
        .inspect_entries
        .fetch_add(1, Ordering::Relaxed);
    let phase = state.phase.load(Ordering::Acquire);
    if !context.same_invocation(&state.invocation) {
        state.init_trace.reject(InitializationRejection::Invocation);
        bail!("native inspection is unavailable in the metadata-only bootstrap phase");
    }
    if context.same_invocation(&state.invocation) && phase == INITIALIZING {
        let request = context
            .input()
            .context("native init observation is missing")?;
        if request.len() <= 4096 {
            let frame =
                CoreValue::parse_guest(request.as_bytes(), context.allocation_owners().graph())
                    .inspect_err(|_| {
                        state.init_trace.reject(InitializationRejection::Frame);
                    })?;
            if let Some(sequence) = checkpoint_sequence(&frame) {
                state
                    .init_trace
                    .inspect_validated
                    .fetch_add(1, Ordering::Relaxed);
                let custody = work.project(|value| value as &(dyn std::any::Any + Send + Sync));
                return context.inspect_retaining(custody, move |output| {
                    let state = work.get();
                    if !state.invocation.same_invocation(output.invocation())
                        || state.phase.load(Ordering::Acquire) != INITIALIZING
                    {
                        state.init_trace.reject(InitializationRejection::Phase);
                        return Err(NativeDataIoError::Retired);
                    }
                    state.source.check_live()?;
                    if !state.init_trace.checkpoint(sequence) {
                        return Err(NativeDataIoError::Unavailable);
                    }
                    // Fixed progress DATA carries no authority or init success.
                    output.append(b"{}")
                });
            }
            if crate::init_diagnostic::valid(&frame, &state.invocation) {
                state
                    .init_trace
                    .inspect_validated
                    .fetch_add(1, Ordering::Relaxed);
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
    if phase == CATALOG_LOAD {
        #[cfg(all(
            test,
            feature = "catalog-diagnostics-ci",
            target_os = "macos",
            target_arch = "aarch64"
        ))]
        {
            let marker = (|| -> Result<_> {
                let request = context.input().context("catalog request is missing")?;
                let frame = CoreValue::parse_guest(
                    request.as_bytes(),
                    context.allocation_owners().graph(),
                )?;
                context
                    .allocation_owners()
                    .graph()
                    .reserve(tobkiri_canon::AllocationRequest {
                        kind: tobkiri_canon::AllocationKind::Temporary,
                        bytes: 0,
                        elements: 0,
                        depth: 0,
                        work: 128,
                    })?;
                Ok(schema_diagnostic_ci::marker(&frame))
            })()
            .inspect_err(|_| {
                if let Ok(mut log) = state.ci_schema_trace.try_lock() {
                    log.reject(schema_diagnostic_ci::FrameFailure::Invalid);
                }
            })?;
            match marker {
                Ok(Some(marker)) => {
                    return schema_diagnostic_ci::prepare(context, work.clone(), marker);
                }
                Ok(None) => {}
                Err(schema_diagnostic_ci::FrameFailure::Overflow) => {
                    return schema_diagnostic_ci::prepare_overflow(context, work.clone());
                }
                Err(failure) => {
                    state
                        .ci_schema_trace
                        .try_lock()
                        .map_err(|_| anyhow::anyhow!("schema checkpoint trace is unavailable"))?
                        .reject(failure);
                    bail!("schema checkpoint frame was refused");
                }
            }
        }
        return catalog::inspect(context, work.clone());
    }
    if !context.same_invocation(&state.invocation)
        || !matches!(
            phase,
            HOST_ROOT | BUNDLE_ROOT | REVIEW_HOST_ROOT | REVIEW_BUNDLE_ROOT
        )
    {
        state.init_trace.reject(if phase == INITIALIZING {
            InitializationRejection::Frame
        } else {
            InitializationRejection::Phase
        });
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
        if matches!(phase, REVIEW_HOST_ROOT | REVIEW_BUNDLE_ROOT) {
            *state.review_root.lock().unwrap_or_else(|e| e.into_inner()) =
                Some(catalog::RootResolution {
                    resolved: resolved.clone(),
                    binding: CoreBuffer::copy_from(
                        binding.as_bytes(),
                        output.allocation_owners().graph(),
                    )
                    .map_err(|_| NativeDataIoError::Capacity)?,
                });
        }
        let first = if matches!(phase, HOST_ROOT | REVIEW_HOST_ROOT) {
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
    cancellation: CoreCancellation,
    review: Option<crate::bootstrap_review::Review>,
    // CoreBuffer/cause DATA only; no extra source or IO capability. Keep it
    // outside caught/dropped child futures until actual retire/finish.
    denied_pending: Option<denied::DeniedCallCustody>,
    identity_pending: Option<identity_observation::IdentityCallCustody>,
    call_pending: Option<std::result::Result<NativeDataValue, NativeDataError>>,
}

// A private field split, not another Store/owner or a resumable guest token.
// Review and its actual journal remain in the outer runner while this finite
// Pack borrow awaits calls on the same immutable program and funding.
struct ReviewPackBorrow<'a, S: BootstrapInputOwner> {
    program: &'a mut Option<NativeDataProgram<State<S>>>,
    work: &'a ControlHandle<Work<S>>,
    io: &'a NativeDataIo,
    call: &'a CoreBuffer,
    input_budget: &'a Arc<dyn AllocationBudget>,
    cancellation: &'a CoreCancellation,
    denied_pending: &'a mut Option<denied::DeniedCallCustody>,
    identity_pending: &'a mut Option<identity_observation::IdentityCallCustody>,
    call_pending: &'a mut Option<std::result::Result<NativeDataValue, NativeDataError>>,
    defer_pending_drop: bool,
}

impl<S: BootstrapInputOwner> NativeBootstrap<S> {
    fn pack_borrow(&mut self) -> ReviewPackBorrow<'_, S> {
        ReviewPackBorrow {
            program: &mut self.program,
            work: &self.work,
            io: &self.io,
            call: &self.call,
            input_budget: &self.input_budget,
            cancellation: &self.cancellation,
            denied_pending: &mut self.denied_pending,
            identity_pending: &mut self.identity_pending,
            call_pending: &mut self.call_pending,
            defer_pending_drop: false,
        }
    }
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
        Self::prepare_inner(engine, component, invocation, source, index, None).await
    }

    /// Retain the actual preparation lineage; cancellation conveys no IO rights.
    pub(crate) async fn prepare_with_cancellation_parent(
        engine: &WasmEngine,
        component: &Component,
        invocation: NativeSetupInvocation,
        source: S,
        index: &'static BootstrapModuleIndex,
        parent: &CoreCancellation,
    ) -> Result<std::result::Result<Self, NativeDataError>> {
        Self::prepare_inner(engine, component, invocation, source, index, Some(parent)).await
    }

    async fn prepare_inner(
        engine: &WasmEngine,
        component: &Component,
        invocation: NativeSetupInvocation,
        source: S,
        index: &'static BootstrapModuleIndex,
        parent: Option<&CoreCancellation>,
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
                init_trace: InitTrace::new(),
                catalog: std::sync::Mutex::new(None),
                catalog_dispatch: OnceLock::new(),
                review_root: std::sync::Mutex::new(None),
                #[cfg(all(test, target_os = "macos", target_arch = "aarch64"))]
                ci_retired_call: OnceLock::new(),
                #[cfg(all(
                    test,
                    feature = "catalog-diagnostics-ci",
                    target_os = "macos",
                    target_arch = "aarch64"
                ))]
                ci_schema_trace: schema_diagnostic_ci::Trace::new(),
            },
            Funding {
                _native: native,
                _control: control,
            },
        )?;
        let store = match parent {
            Some(parent) => engine.native_setup_store_with_imports_and_cancellation_parent(
                invocation.clone(),
                State { work: work.clone() },
                &State::<S>::BINDINGS,
                parent,
            ),
            None => engine.native_setup_store_with_imports(
                invocation.clone(),
                State { work: work.clone() },
                &State::<S>::BINDINGS,
            ),
        }
        .map_err(|original| initialization_failure(work.get(), None, original))?;
        let io = store
            .native_data_io()
            .context("native bootstrap IO was not bound")?
            .clone();
        let cancellation = store.cancellation().clone();
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
            NativeDataBinding {
                export: NativeDataExport::new(Some(RUNTIME), "denied"),
                result: Some(contract(NativeDataSuccessType::String)),
            },
            NativeDataBinding {
                export: NativeDataExport::new(Some(TRANSPORT), "import-values"),
                result: Some(contract(NativeDataSuccessType::String)),
            },
            NativeDataBinding {
                export: NativeDataExport::new(Some(RUNTIME), "load-catalog"),
                result: Some(contract(NativeDataSuccessType::String)),
            },
        ];
        work.get().init_trace.phase(1);
        let program = match store.prepare_data_program(component, &bindings).await {
            Ok(program) => program,
            Err(original) => {
                let original = initialization_failure(work.get(), None, original);
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
            cancellation,
            review: None,
            denied_pending: None,
            identity_pending: None,
            call_pending: None,
        };
        runner
            .work
            .get()
            .phase
            .store(INITIALIZING, Ordering::Release);
        runner.work.get().init_trace.phase(2);
        let result = runner
            .program
            .as_mut()
            .context("native program was retired")?
            .invoke(0, Some(&init))
            .await;
        runner
            .work
            .get()
            .init_trace
            .phase(if result.is_err() { 4 } else { 3 });
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
                let domain = initialization_domain_after_cleanup(domain, runner.finish().await)?;
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
        self.pack_borrow()
            .string_call_in_phase(slot, input, phase)
            .await
    }
    async fn retire_failure(&mut self, original: anyhow::Error) -> anyhow::Error {
        self.pack_borrow().retire_failure(original).await
    }

    fn initialization_failure(&self, original: anyhow::Error) -> anyhow::Error {
        initialization_failure(
            self.work.get(),
            self.program.as_ref().map(|p| p.observation()),
            original,
        )
    }

    /// Actual import observations, separate from guest-reported progress DATA.
    pub fn initialization_trace(&self) -> crate::InitializationTrace {
        self.work.get().init_trace.snapshot()
    }

    /// Actual inspection import entries, including fixed init checkpoints and
    /// failure DATA. This count does not imply filesystem IO or grant rights.
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

    /// Already sampled, Copy-only CI metadata; not a live Store query or an
    /// IO/journal cleanup receipt. None means no available program sample.
    #[cfg(all(test, target_os = "macos", target_arch = "aarch64"))]
    pub(crate) fn ci_retired_call(&self) -> Option<CiRetiredCall> {
        self.work.get().ci_retired_call.get().copied()
    }

    /// Owned bounded DATA/report funding; None means no prepaid attempt,
    /// busy or poisoned. Copying uses only work prepaid before retirement.
    /// This never selects a source/schema or supplies a cleanup receipt.
    #[cfg(all(
        test,
        feature = "catalog-diagnostics-ci",
        target_os = "macos",
        target_arch = "aarch64"
    ))]
    pub(crate) fn ci_schema_trace(&self) -> Option<schema_diagnostic_ci::Snapshot> {
        schema_diagnostic_ci::snapshot(&self.work.get().ci_schema_trace)
    }

    #[cfg(all(
        test,
        feature = "catalog-diagnostics-ci",
        target_os = "macos",
        target_arch = "aarch64"
    ))]
    pub(crate) fn ci_schema_status(&self) -> Option<schema_diagnostic_ci::Status> {
        self.work.get().ci_schema_trace.status()
    }

    /// Borrow the original DATA codec account for native frame codecs. Cloning
    /// its handle retains these same limits, deadline and physical accounting.
    pub fn data_budget(&self) -> &Arc<dyn AllocationBudget> {
        &self.input_budget
    }

    /// Enter the actual original review continuation on this same Store. The
    /// current prefix always refuses its next unbound reachpoint explicitly.
    /// No guest inspection import or independent begin/prefetch is involved.
    pub async fn setup_review_listing(
        &mut self,
        options: &crate::BootstrapReviewOptions,
    ) -> Result<std::result::Result<CoreBuffer, crate::BootstrapReviewFailure>>
    where
        S: crate::BootstrapReviewOwner,
    {
        self.setup_review_inner(options, false, false).await
    }

    /// Historical same-guard identity boundary only; not the product candidate.
    #[cfg(test)]
    pub async fn setup_review_identity_prefix(
        &mut self,
        options: &crate::BootstrapReviewOptions,
    ) -> Result<std::result::Result<CoreBuffer, crate::BootstrapReviewFailure>>
    where
        S: crate::BootstrapReviewOwner,
    {
        self.setup_review_inner(options, true, false).await
    }

    /// Historical constructor-entry catalog proof, before registry adoption.
    #[cfg(test)]
    pub async fn setup_review_catalog_prefix(
        &mut self,
        options: &crate::BootstrapReviewOptions,
    ) -> Result<std::result::Result<CoreBuffer, crate::BootstrapReviewFailure>>
    where
        S: crate::BootstrapReviewOwner,
    {
        self.setup_review_inner(options, false, true).await
    }

    async fn setup_review_inner(
        &mut self,
        options: &crate::BootstrapReviewOptions,
        identity_prefix: bool,
        catalog_prefix: bool,
    ) -> Result<std::result::Result<CoreBuffer, crate::BootstrapReviewFailure>>
    where
        S: crate::BootstrapReviewOwner,
    {
        if self.work.get().phase.load(Ordering::Acquire) != READY {
            bail!("native bootstrap review is not initialized");
        }
        if self.review.is_some() {
            return Ok(Err(crate::BootstrapReviewFailure::Admission(
                crate::OriginalAuthorityAdmission::AlreadyAttempted,
            )));
        }
        let diagnostic = options
            .denial_diagnostic
            .as_ref()
            .map(|text| self.copy_input(text.as_bytes()))
            .transpose()?;
        let mut review = match crate::bootstrap_review::Review::new(
            &self.work.get().invocation,
            options,
            diagnostic,
        ) {
            Ok(review) => review,
            Err(error) => return Ok(Err(crate::BootstrapReviewFailure::Admission(error))),
        };
        review.identity_prefix = identity_prefix;
        review.catalog_prefix = catalog_prefix;
        // The carrier remains in self, outside dispatch's caught future, even
        // if a source callback panics during its real persistence operation.
        self.review = Some(review);
        let mut guard = RootCallGuard {
            work: self.work.clone(),
            io: self.io.clone(),
            completed: false,
        };
        self.work.get().phase.store(REVIEW, Ordering::Release);
        let result = {
            // Explicit field split: the actual journal/intent stays outside
            // this child future in runner.review, throughout every Pack await.
            let Self {
                program,
                work,
                io,
                call,
                input_budget,
                cancellation,
                review,
                denied_pending,
                identity_pending,
                call_pending,
            } = &mut *self;
            let mut pack = ReviewPackBorrow {
                program,
                work,
                io,
                call,
                input_budget,
                cancellation,
                denied_pending,
                identity_pending,
                call_pending,
                defer_pending_drop: true,
            };
            match review_continuation::run(review.as_mut().expect("admitted review"), &mut pack)
                .await
            {
                Err(error) => Err(pack.retire_failure(error).await),
                outcome => outcome,
            }
        };
        // The one review attempt is complete, but its exceptional lexical
        // journal scope may still await actual finish. Do not reopen public
        // READY calls outside that private continuation while it is held.
        self.work.get().phase.store(CLOSED, Ordering::Release);
        guard.completed = true;
        result
    }

    /// Only physically uncertain original IO may retain source custody in the
    /// native coordinator. This contains no success DATA or source permission.
    pub fn original_review_uncertain_cleanup(&self) -> Option<crate::OriginalAuthorityCleanup> {
        self.review
            .as_ref()
            .and_then(|review| review.uncertain_cleanup())
    }

    /// Retire the actual Store and await original native physical cleanup.
    pub async fn finish(mut self) -> Result<()> {
        self.work.get().phase.store(CLOSED, Ordering::Release);
        if let Some(mut program) = self.program.take() {
            program.retire();
        }
        let joined = self.io.retire_and_wait().await;
        // The actual IO worker has finished; completed catalog scratch carries
        // no source/FD. Uncertain descriptors remain in the original ledger.
        if joined.is_ok() {
            drop(
                self.work
                    .get()
                    .catalog
                    .lock()
                    .unwrap_or_else(|e| e.into_inner())
                    .take(),
            );
        } else if let Ok(mut current) = self.work.get().catalog.try_lock() {
            // Never wait on an unknown IO body's mutex after a failed Join.
            // An active body keeps the real Work/controller/FD custody itself.
            drop(current.take());
        }
        // Even a failed Join attempt cannot skip the actual native journal
        // close. It remains a failed cleanup; no successful receipt is forged.
        let closed = self
            .review
            .as_mut()
            .is_none_or(crate::bootstrap_review::Review::close_continuation);
        drop(self.denied_pending.take());
        drop(self.identity_pending.take());
        drop(self.call_pending.take());
        if let Some(review) = &mut self.review {
            review.release_actual_data();
        }
        let receipt_failure = self.review.as_ref().and_then(|review| {
            let receipt = review.receipt();
            if !closed || !receipt.verified() {
                Some(crate::BootstrapReviewCleanupFailure { receipt })
            } else {
                None
            }
        });
        match (joined, receipt_failure) {
            (Err(cleanup), Some(original)) => Err(BootstrapCleanupFailure {
                original: original.into(),
                cleanup: cleanup.into(),
            }
            .into()),
            (Err(error), None) => Err(error.into()),
            (Ok(()), Some(error)) => Err(error.into()),
            (Ok(()), None) => Ok(()),
        }
    }
}

impl<S: BootstrapInputOwner> ReviewPackBorrow<'_, S> {
    async fn string_call_in_phase(
        &mut self,
        slot: usize,
        input: &CoreBuffer,
        phase: u8,
    ) -> Result<std::result::Result<CoreBuffer, NativeDataError>> {
        // These private frames are constructed by their own Review caller.
        // The ordinary DATA describe API cannot select these operations.
        if slot == 2 {
            match CoreValue::parse_guest(input.as_bytes(), self.input_budget) {
                Ok(frame) => match frame.get_key("format").as_ref().and_then(CoreValue::as_str) {
                    Some("io.tobkiri.native-catalog-manifest-parse.v1")
                        if phase != CATALOG_PARSE =>
                    {
                        bail!("manifest parse is outside the fixed Review reachpoint");
                    }
                    Some("io.tobkiri.native-resolver-source-lookup.v1")
                        if phase != RESOLVER_SOURCE_LOOKUP =>
                    {
                        bail!("source Profile lookup is outside the fixed Review reachpoint");
                    }
                    Some(resolver_edges::STATIC_REQUEST) if phase != RESOLVER_STATIC_EDGES => {
                        bail!("static Profile edges are outside the fixed Review reachpoint");
                    }
                    Some(resolver_edges::DYNAMIC_REQUEST) if phase != RESOLVER_DYNAMIC_EDGES => {
                        bail!("dynamic Profile edges are outside the fixed Review reachpoint");
                    }
                    _ => {}
                },
                Err(tobkiri_canon::AllocationError::Invalid) => {}
                Err(error) => return Err(error.into()),
            }
        }
        if self.work.get().phase.load(Ordering::Acquire) != phase {
            bail!("native bootstrap is not initialized");
        }
        if self.call_pending.is_some() {
            bail!("prior actual call result is still pending cleanup");
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
        match result {
            Ok(result) => *self.call_pending = Some(result),
            Err(original) => return Err(self.retire_failure(original).await),
        }
        if let Err(original) = self.work.get().source.check_live() {
            return Err(self.retire_failure(original.into()).await);
        }
        match self.call_pending.take().expect("actual completed call") {
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
        #[cfg(debug_assertions)]
        let rejected_identity_view = self
            .identity_pending
            .as_ref()
            .is_some_and(identity_observation::IdentityCallCustody::view_rejected);
        #[cfg(debug_assertions)]
        if rejected_identity_view {
            self.identity_pending
                .as_ref()
                .expect("rejected getter custody")
                .assert_rejected_outputs_live();
        }
        #[cfg(debug_assertions)]
        let rejected_factory_view = self
            .denied_pending
            .as_ref()
            .is_some_and(denied::DeniedCallCustody::view_rejected);
        #[cfg(debug_assertions)]
        if rejected_factory_view {
            self.denied_pending
                .as_ref()
                .expect("rejected factory custody")
                .assert_rejected_outputs_live();
        }
        #[cfg(all(test, target_os = "macos", target_arch = "aarch64"))]
        if let Some(program) = self.program.as_ref() {
            // NativeDataEntry has already sampled the real call before its
            // own retirement. Copy that sample before losing this program;
            // never query dead Store fuel or execute another guest call.
            let sample = CiRetiredCall::capture(
                self.work.get().phase.load(Ordering::Acquire),
                program.observation(),
            );
            // Nested error propagation must not replace the first available
            // sample with a closed phase or an invented unavailable/zero fuel.
            let _ = self.work.get().ci_retired_call.set(sample);
        }
        self.work.get().phase.store(CLOSED, Ordering::Release);
        if let Some(mut program) = self.program.take() {
            program.retire();
            #[cfg(debug_assertions)]
            if rejected_factory_view {
                assert!(
                    program.observation().store_retired,
                    "rejected factory Store was not physically retired"
                );
            }
            #[cfg(debug_assertions)]
            if rejected_identity_view {
                assert!(
                    program.observation().store_retired,
                    "rejected getter Store was not physically retired"
                );
            }
        }
        let outcome = match self.io.retire_and_wait().await {
            Ok(()) => original,
            Err(cleanup) => BootstrapCleanupFailure {
                original,
                cleanup: cleanup.into(),
            }
            .into(),
        };
        #[cfg(debug_assertions)]
        if rejected_factory_view {
            self.denied_pending
                .as_ref()
                .expect("rejected factory custody survived IO wait")
                .assert_rejected_outputs_live();
        }
        if !self.defer_pending_drop {
            drop(self.denied_pending.take());
        }
        #[cfg(debug_assertions)]
        if rejected_identity_view {
            self.identity_pending
                .as_ref()
                .expect("rejected getter custody survived IO wait")
                .assert_rejected_outputs_live();
        }
        if !self.defer_pending_drop {
            drop(self.identity_pending.take());
            drop(self.call_pending.take());
        }
        outcome
    }
}

fn initialization_failure<S>(
    work: &Work<S>,
    observation: Option<tobkiri_wasm::NativeDataObservation>,
    original: anyhow::Error,
) -> anyhow::Error {
    crate::OriginalInitializationFailure {
        original,
        diagnostic: work.init_diagnostic.get().cloned(),
        trace: work.init_trace.snapshot(),
        observation,
        capacity: work.invocation.capacity_snapshot().ok(),
        elapsed_micros: work.invocation.started_at().elapsed().as_micros(),
        deadline: work.invocation.deadline().ok(),
    }
    .into()
}

#[cfg(all(
    test,
    feature = "bootstrap-ci",
    target_os = "macos",
    target_arch = "aarch64"
))]
#[path = "bootstrap_actual_ci.rs"]
mod actual_ci;
#[cfg(all(
    test,
    feature = "bootstrap-ci",
    target_os = "macos",
    target_arch = "aarch64"
))]
#[path = "bootstrap_catalog_actual_ci.rs"]
mod catalog_actual_ci;
#[cfg(all(
    test,
    feature = "bootstrap-ci",
    target_os = "macos",
    target_arch = "aarch64"
))]
#[path = "bootstrap_catalog_parity_actual_ci.rs"]
mod catalog_parity_actual_ci;
#[cfg(all(
    test,
    feature = "bootstrap-ci",
    target_os = "macos",
    target_arch = "aarch64"
))]
#[path = "bootstrap_denied_actual_ci.rs"]
mod denied_actual_ci;
#[cfg(all(test, target_os = "macos"))]
#[path = "bootstrap_domain_cleanup_tests.rs"]
mod domain_cleanup_tests;
#[cfg(all(
    test,
    feature = "bootstrap-ci",
    target_os = "macos",
    target_arch = "aarch64"
))]
#[path = "bootstrap_identity_actual_ci.rs"]
mod identity_actual_ci;
#[cfg(all(test, target_os = "macos", target_arch = "aarch64"))]
#[path = "bootstrap_policy_tests.rs"]
mod policy_tests;
#[cfg(all(
    test,
    feature = "bootstrap-ci",
    target_os = "macos",
    target_arch = "aarch64"
))]
#[path = "bootstrap_review_identity_actual_ci.rs"]
mod review_identity_actual_ci;
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
