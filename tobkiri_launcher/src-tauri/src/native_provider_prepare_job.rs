//! Tracked Registry reads, Provider preparation, commit staging and publication.

use std::{
    cell::Cell,
    panic::{catch_unwind, AssertUnwindSafe},
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc, Mutex,
    },
    time::Instant,
};

use anyhow::{anyhow, ensure, Context, Result};
use serde::Deserialize;
use serde_json::value::RawValue;
use tauri::{AppHandle, Manager};
use tobkiri_core::ids::{OpName, ProfileId, ShapeRef};
use tobkiri_core::{
    AllocationBudget, CoreBuffer, CoreError, CoreRef, CoreValue, LegacyRegistryRead,
    NativeArtifactError, NativeArtifactInspection, NativeRegistry, MAX_BYTES,
};
use tobkiri_gate::{CapacityKind, ControlKind, CoreCancellation};
pub(crate) use tobkiri_harness_native::SavedOrchestrationStepError;
use tobkiri_harness_native::{
    begin_legacy_saved_turn_from_admitted_port, claim_legacy_saved_turn_from_admitted_port,
    invoke_original_saved_orchestration_port, invoke_original_saved_turn_begin_port,
    invoke_original_saved_turn_claim_port, invoke_original_saved_turn_port,
    invoke_original_saved_turn_read_port, read_legacy_saved_turn,
    settle_legacy_saved_turn_from_admitted_port, LegacySavedTurnSettlementError,
    LegacySavedTurnSettlementRequest, LegacySavedTurnStartRequest, OriginalSavedTurnCheck,
    OriginalSavedTurnStartInput, OriginalSavedTurnStartOutcome, OriginalSavedTurnStartStep,
};
use tobkiri_store::{DataRoot, StoreError};
use tobkiri_wasm::NativeDataIoError;

use super::{
    caught,
    launch_metadata::{
        reacquire_conversation_append_pack, reacquire_conversation_confirmation_pack,
        reacquire_conversation_read_pack, reacquire_model_resolve_pack, reacquire_model_save_pack,
        reacquire_model_settings_pack, reacquire_provider_prepare_pack,
        reacquire_saved_orchestration_pack, reacquire_saved_receipt_pack, reacquire_turn_read_pack,
        reacquire_turn_settlement_pack, reacquire_turn_start_pack, ConversationReadCaller,
        SettlementCaller, VerifiedRegistryLaunch, REGISTRY_VERIFICATION_BYTES,
    },
    NativeRuntimeState, OriginalModelResolveRole, OriginalSavedReceiptRole,
};
use crate::{
    defaultspack_authority::{require_current_launch_identity, SignedApplicationResolver},
    defaultspack_manager::{ApplicationProcessManager, ProviderPrepareListener},
    native_runtime_jobs::{JobJoin, JobTicket, WorkerExit},
    AppConfig,
};

type ContinueCurrent = Arc<dyn Fn() -> Result<()> + Send + Sync>;
const MODEL_SAVE_REPLY_LIMIT: usize = 1024 * 1024;
/// Maximum encoded CPython Conversation read result accepted by the Host.
pub(crate) const CONVERSATION_READ_REPLY_LIMIT: usize = 1024 * 1024;
/// Maximum terminal evidence and result JSON in the Conversation confirmation exchange.
pub(crate) const CONVERSATION_CONFIRMATION_LIMIT: usize = 16 * 1024;
/// Maximum encoded start and owner result for the Conversation append exchange.
pub(crate) const CONVERSATION_APPEND_LIMIT: usize = 1024 * 1024;
/// Maximum encoded CPython Model Resolve result accepted by the Host.
pub(crate) const MODEL_RESOLVE_REPLY_LIMIT: usize = 1024 * 1024;
/// Maximum encoded saved Turn GET result accepted by the Host.
pub(crate) const TURN_READ_REPLY_LIMIT: usize = 1024 * 1024;
/// Maximum encoded saved Turn start result including its wire envelope.
pub(crate) const TURN_START_REPLY_LIMIT: usize = TURN_READ_REPLY_LIMIT + 128;
/// Maximum original saved-orchestration input and result JSON.
pub(crate) const SAVED_ORCHESTRATION_FRAME_LIMIT: usize = 4 * 1024 * 1024;

/// Fixed operations on the original saved Turn owner.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum SavedTurnStartAction {
    Begin,
    Claim,
}

pub(crate) enum NativeRegistryRequest {
    Provider {
        request: Vec<u8>,
        apply: Option<Vec<u8>>,
    },
    ModelList,
    ModelSave {
        request: Vec<u8>,
        provider: Vec<u8>,
    },
    ConversationGet {
        caller: ConversationReadCaller,
        conversation_id: String,
    },
    ConversationReceipt {
        caller: OriginalSavedReceiptRole,
        turn_id: String,
    },
    ConversationConfirmation {
        caller: OriginalSavedReceiptRole,
        conversation_id: String,
        terminal: Vec<u8>,
    },
    ConversationAppend {
        request: Vec<u8>,
        expected_revision: u64,
    },
    ModelResolve {
        role: OriginalModelResolveRole,
        identifier: String,
    },
    TurnRead {
        caller: OriginalSavedReceiptRole,
        turn_id: String,
    },
    TurnStart {
        action: SavedTurnStartAction,
        turn_id: String,
        conversation_id: String,
        conversation_revision: u64,
        input_digest: String,
        source_digest: String,
    },
    TurnSettlement {
        caller: SettlementCaller,
        turn_id: String,
        input_digest: String,
        expected_revision: Option<u64>,
        result_reference: String,
    },
    SavedOrchestration {
        input: Vec<u8>,
    },
}

impl NativeRegistryRequest {
    pub(crate) fn has_launch_proof(&self, proof: &VerifiedRegistryLaunch) -> bool {
        match self {
            Self::Provider { .. } => proof.provider_prepare.is_some(),
            Self::ModelList => proof.model_settings.is_some(),
            Self::ModelSave { .. } => proof.model_save.is_some(),
            Self::ConversationGet { caller, .. } => proof.permits_conversation_read(*caller),
            Self::ConversationReceipt { caller, .. } => proof.permits_saved_receipt(*caller),
            Self::ConversationConfirmation { caller, .. } => {
                proof.permits_conversation_confirmation(*caller)
            }
            Self::ConversationAppend { .. } => proof.permits_conversation_append(),
            Self::ModelResolve { role, .. } => proof.permits_model_resolve(*role),
            Self::TurnRead { caller, .. } => proof.permits_turn_read(*caller),
            Self::TurnStart { .. } => proof.permits_turn_start(),
            Self::TurnSettlement { caller, .. } => proof.permits_turn_settlement(*caller),
            Self::SavedOrchestration { .. } => proof.permits_saved_orchestration(),
        }
    }

    pub(crate) fn requires_publication_permit(&self) -> bool {
        matches!(
            self,
            Self::ModelSave { .. }
                | Self::ConversationConfirmation { .. }
                | Self::ConversationAppend { .. }
                | Self::TurnStart { .. }
                | Self::TurnSettlement { .. }
        )
    }

    fn input_capacity(&self) -> Result<usize> {
        let (request, extra) = match self {
            Self::Provider { request, apply } => (request, apply.as_ref()),
            Self::ModelList => return Ok(0),
            Self::ModelSave { request, provider } => (request, Some(provider)),
            Self::ConversationConfirmation {
                conversation_id,
                terminal,
                ..
            } => {
                ensure!(
                    (1..=CONVERSATION_CONFIRMATION_LIMIT).contains(&terminal.len()),
                    "native Conversation confirmation terminal exceeds its input envelope"
                );
                return terminal
                    .capacity()
                    .checked_add(conversation_id.capacity())
                    .filter(|capacity| *capacity <= MAX_BYTES)
                    .context("native Conversation confirmation input capacity overflow");
            }
            Self::ConversationAppend { request, .. } => {
                ensure!(
                    !request.is_empty() && request.capacity() <= CONVERSATION_APPEND_LIMIT,
                    "native Conversation append exceeds its input envelope"
                );
                return Ok(request.capacity());
            }
            Self::SavedOrchestration { input } => {
                if input.is_empty() {
                    return Err(SavedOrchestrationStepError::Invalid.into());
                }
                if input.capacity() > SAVED_ORCHESTRATION_FRAME_LIMIT {
                    return Err(SavedOrchestrationStepError::Capacity.into());
                }
                return Ok(input.capacity());
            }
            Self::TurnSettlement {
                turn_id,
                input_digest,
                result_reference,
                ..
            } => return turn_input_capacity([turn_id, input_digest, result_reference]),
            Self::TurnStart {
                turn_id,
                conversation_id,
                input_digest,
                source_digest,
                ..
            } => {
                return turn_input_capacity([turn_id, conversation_id, input_digest, source_digest])
            }
            Self::ConversationGet {
                conversation_id: identifier,
                ..
            }
            | Self::ConversationReceipt {
                turn_id: identifier,
                ..
            }
            | Self::TurnRead {
                turn_id: identifier,
                ..
            }
            | Self::ModelResolve { identifier, .. } => {
                let size = identifier.capacity();
                ensure!(
                    size <= MAX_BYTES,
                    "native Registry request exceeds its input envelope"
                );
                return Ok(size);
            }
        };
        let size = request
            .capacity()
            .checked_add(extra.map_or(0, Vec::capacity))
            .context("native Registry input capacity overflow")?;
        ensure!(
            !request.is_empty() && extra.is_none_or(|bytes| !bytes.is_empty()) && size <= MAX_BYTES,
            "native Registry request exceeds its input envelope"
        );
        Ok(size)
    }
}

fn turn_input_capacity<'a>(values: impl IntoIterator<Item = &'a String>) -> Result<usize> {
    let capacity = values.into_iter().try_fold(0usize, |total, value| {
        total
            .checked_add(value.capacity())
            .context("native Turn input capacity overflow")
    })?;
    ensure!(
        capacity <= MAX_BYTES,
        "native Turn request exceeds its input envelope"
    );
    Ok(capacity)
}

#[derive(Debug)]
pub(crate) struct ModelRegistryRequiresLegacyRead;

impl std::fmt::Display for ModelRegistryRequiresLegacyRead {
    fn fmt(&self, output: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        output.write_str("Model Registry exceeds the native compatibility envelope")
    }
}

impl std::error::Error for ModelRegistryRequiresLegacyRead {}

/// A fixed Conversation read exceeded its compatibility envelope.
#[derive(Debug)]
pub(crate) struct ConversationRegistryRequiresLegacyRead;

impl std::fmt::Display for ConversationRegistryRequiresLegacyRead {
    fn fmt(&self, output: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        output.write_str("Conversation Registry exceeds the native compatibility envelope")
    }
}

impl std::error::Error for ConversationRegistryRequiresLegacyRead {}

/// The Conversation write needs the original owner before native publication.
#[derive(Debug)]
pub(crate) struct ConversationWriteRequiresLegacyRead;

impl std::fmt::Display for ConversationWriteRequiresLegacyRead {
    fn fmt(&self, output: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        output.write_str("Conversation write requires the original owner")
    }
}

impl std::error::Error for ConversationWriteRequiresLegacyRead {}

/// The verified Turn port requires the original JSON decoder before any write.
#[derive(Debug)]
pub(crate) struct TurnSettlementRequiresLegacyCodec;

impl std::fmt::Display for TurnSettlementRequiresLegacyCodec {
    fn fmt(&self, output: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        output.write_str("Turn settlement requires the original JSON codec")
    }
}

impl std::error::Error for TurnSettlementRequiresLegacyCodec {}

/// The verified Turn owner needs its original implementation before any write.
#[derive(Debug)]
pub(crate) struct TurnStartRequiresLegacyCodec;

impl std::fmt::Display for TurnStartRequiresLegacyCodec {
    fn fmt(&self, output: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        output.write_str("Turn start requires the original owner")
    }
}

impl std::error::Error for TurnStartRequiresLegacyCodec {}

/// A running Provider can narrow its Host deadline when the Core begins.
pub(crate) struct ProviderPrepareDeadline(Mutex<Instant>);

impl ProviderPrepareDeadline {
    pub(crate) fn new(deadline: Instant) -> Self {
        Self(Mutex::new(deadline))
    }

    pub(crate) fn current(&self) -> Instant {
        *self.0.lock().unwrap_or_else(|error| error.into_inner())
    }

    fn restrict(&self, core_deadline: Instant) -> Instant {
        let mut deadline = self.0.lock().unwrap_or_else(|error| error.into_inner());
        *deadline = (*deadline).min(core_deadline);
        *deadline
    }
}

/// Single-result handle for an admitted Provider worker and its physical Join.
pub(crate) struct PendingProviderPrepare {
    join: JobJoin,
    receive: mpsc::Receiver<Result<Prepared>>,
    deadline: Arc<ProviderPrepareDeadline>,
    continue_current: ContinueCurrent,
    cancelled: AtomicBool,
}

impl PendingProviderPrepare {
    /// Cancel the fixed preparation and its original attached invocation.
    pub(crate) fn cancel(&self) -> Result<()> {
        self.cancelled.store(true, Ordering::Release);
        self.join.cancel()
    }

    /// Wait for physical worker and source cleanup independently of result receipt.
    pub(crate) fn join_until(&self, deadline: Instant) -> Result<()> {
        self.join.join_until(deadline)
    }

    /// Poll for verified physical cleanup without blocking peer handling.
    pub(crate) fn try_join(&self) -> Result<bool> {
        self.join.try_join()
    }

    /// Take one terminal result after the actual worker and its sources have ended.
    /// A planner rejection remains downcastable to `NativeArtifactError`.
    pub(crate) fn finish_until(&self, deadline: Instant) -> Result<CoreBuffer> {
        self.join_until(deadline)?;
        self.check_delivery_live()?;
        (self.continue_current)()?;
        let prepared = self
            .receive
            .try_recv()
            .context("native Provider preparation produced no terminal result")??;
        prepared
            .manager
            .require_provider_prepare_admission(&prepared.listener)?;
        self.check_delivery_live()?;
        prepared.result
    }

    fn check_delivery_live(&self) -> Result<()> {
        if self.cancelled.load(Ordering::Acquire) {
            return Err(NativeArtifactError::from(NativeDataIoError::Cancelled).into());
        }
        check_live(self.deadline.current(), None)
    }
}

impl Drop for PendingProviderPrepare {
    fn drop(&mut self) {
        let _ = self.cancel();
    }
}

struct Request {
    listener: Arc<ProviderPrepareListener>,
    operation: NativeRegistryRequest,
    input_capacity: u64,
    deadline: Arc<ProviderPrepareDeadline>,
    continue_current: ContinueCurrent,
    publish_permit: Option<ContinueCurrent>,
}

struct Prepared {
    manager: Arc<ApplicationProcessManager>,
    listener: Arc<ProviderPrepareListener>,
    result: Result<CoreBuffer>,
}

struct PreparedReply {
    wire: CoreBuffer,
    document: Option<CoreBuffer>,
}

/// Run one verified request on the managed Registry worker.
///
/// The Host adapter checks continuing eligibility. The worker owns the fixed
/// Registry and supplies bounded observations to the guest.
pub(crate) fn start_provider_prepare(
    app: &AppHandle,
    listener: Arc<ProviderPrepareListener>,
    operation: NativeRegistryRequest,
    deadline: Arc<ProviderPrepareDeadline>,
    continue_current: ContinueCurrent,
    publish_permit: Option<ContinueCurrent>,
) -> Result<PendingProviderPrepare> {
    check_live(deadline.current(), None)?;
    ensure!(
        operation.has_launch_proof(listener.verified_source()),
        "native Registry operation has no matching Launch proof"
    );
    ensure!(
        operation.requires_publication_permit() == publish_permit.is_some(),
        "native Registry publication permit differs from the operation"
    );
    let input_capacity = operation.input_capacity()?;
    listener.verified_source().identity.validate()?;
    let runtime = app
        .try_state::<NativeRuntimeState>()
        .context("native Provider preparation runtime owner is unavailable")?
        .inner()
        .clone();
    let jobs = runtime.0.jobs.clone();
    let request = Request {
        listener,
        operation,
        input_capacity: u64::try_from(input_capacity)?,
        deadline: Arc::clone(&deadline),
        continue_current: Arc::clone(&continue_current),
        publish_permit,
    };
    let app = app.clone();
    let (send, receive) = mpsc::sync_channel(1);
    let join = jobs.spawn_joinable(move |job| {
        let (result, cleanup_verified) = catch_unwind(AssertUnwindSafe(|| {
            tauri::async_runtime::block_on(run(app, runtime, job, request))
        }))
        .unwrap_or_else(|_| {
            (
                Err(anyhow!("native Provider preparation worker panicked")),
                false,
            )
        });
        let _ = send.send(result);
        WorkerExit { cleanup_verified }
    })?;
    Ok(PendingProviderPrepare {
        join,
        receive,
        deadline,
        continue_current,
        cancelled: AtomicBool::new(false),
    })
}

async fn run(
    app: AppHandle,
    runtime: NativeRuntimeState,
    job: JobTicket,
    request: Request,
) -> (Result<Prepared>, bool) {
    let Request {
        listener,
        operation,
        input_capacity,
        deadline,
        continue_current,
        publish_permit,
    } = request;
    let identity = &listener.verified_source().identity;
    let admitted = (|| {
        check_live(deadline.current(), None)?;
        job.ensure_accepting(&runtime.0.jobs)?;
        continue_current()?;
        check_live(deadline.current(), None)?;
        let config = app
            .try_state::<AppConfig>()
            .context("native Provider preparation configuration is unavailable")?;
        let manager = app
            .try_state::<Arc<ApplicationProcessManager>>()
            .context("native Provider preparation Application manager is unavailable")?
            .inner()
            .clone();
        require_current_launch_identity(&config, identity)?;
        manager.require_provider_prepare_admission(&listener)?;
        let invocation = runtime.begin(&app, &job)?;
        let cancellation = job.attach_invocation(&invocation)?;
        let execution_deadline = deadline.restrict(invocation.deadline()?);
        check_live(execution_deadline, Some(&cancellation))?;
        let custody = job.source_custody()?;
        let sources = SignedApplicationResolver::resolve_sources(&config)?;
        check_live(execution_deadline, Some(&cancellation))?;
        let input_funding = (input_capacity != 0)
            .then(|| invocation.reserve(CapacityKind::VfsBuffer, input_capacity))
            .transpose()?;
        let control_funding = invocation.control().reserve(
            ControlKind::Frame,
            input_capacity + REGISTRY_VERIFICATION_BYTES,
        )?;
        let inspection =
            NativeArtifactInspection::from_invocation(sources, invocation, &cancellation)?;
        Ok::<_, anyhow::Error>((
            config,
            manager,
            cancellation,
            execution_deadline,
            custody,
            input_funding,
            control_funding,
            inspection,
        ))
    })();
    let (
        config,
        manager,
        cancellation,
        execution_deadline,
        custody,
        mut input_funding,
        _control_funding,
        mut inspection,
    ) = match admitted {
        Ok(admitted) => admitted,
        Err(error) => return (Err(error), true),
    };

    let checkpoint = || {
        check_live(execution_deadline, Some(&cancellation))?;
        job.ensure_accepting(&runtime.0.jobs)?;
        manager.require_provider_prepare_admission(&listener)
    };
    let revalidate = || {
        checkpoint()?;
        continue_current()?;
        require_current_launch_identity(&config, identity)?;
        ensure!(
            listener.owns_socket(),
            "native Provider preparation listener lost its socket"
        );
        checkpoint()
    };
    let publishes = matches!(
        &operation,
        NativeRegistryRequest::Provider { apply: Some(_), .. }
    );
    let model_save = matches!(&operation, NativeRegistryRequest::ModelSave { .. });
    let registry = Arc::new(Mutex::new(None));
    let registry_limit = Arc::new(AtomicBool::new(false));
    let outcome = {
        let prepare = caught(async {
            let graph = inspection.allocation_budget()?.clone();
            let observe_registry = |target, locked_observation| -> Result<LegacyRegistryRead> {
                let read_manager = Arc::clone(&manager);
                let read_listener = Arc::clone(&listener);
                let continue_read = Arc::clone(&continue_current);
                let continue_observing: ContinueCurrent = Arc::new(move || {
                    read_manager.require_provider_prepare_admission(&read_listener)?;
                    continue_read()
                });
                *registry
                    .lock()
                    .map_err(|_| anyhow!("Registry lock unavailable"))? = Some(target);
                let read = registry_read(
                    Arc::clone(&registry),
                    Arc::clone(&registry_limit),
                    Arc::clone(&continue_observing),
                    cancellation.clone(),
                    execution_deadline,
                    locked_observation,
                );
                Ok(guarded_registry_read(
                    read,
                    continue_observing,
                    cancellation.clone(),
                    execution_deadline,
                ))
            };
            let lock_conversation_store = |inspection: &NativeArtifactInspection<_>,
                                           missing: anyhow::Error|
             -> Result<NativeRegistry> {
                let mut owner = inspection
                    .legacy_conversation_store(&config.user_data_dir, &identity.profile_id)?;
                owner
                    .observe_locked(&graph, || {
                        checkpoint()
                            .and_then(|()| continue_current())
                            .and_then(|()| checkpoint())
                            .map_err(publication_checkpoint_error)
                    })?
                    .ok_or(missing)?;
                revalidate()?;
                Ok(owner)
            };
            let (request, apply) = match operation {
                NativeRegistryRequest::Provider { request, apply } => (request, apply),
                NativeRegistryRequest::ModelList => {
                    let pack = reacquire_model_settings_pack(
                        &inspection,
                        listener.verified_source(),
                        checkpoint,
                    )?;
                    revalidate()?;
                    let output = inspection
                        .list_legacy_model_settings(
                            &pack,
                            &config.user_data_dir,
                            &identity.profile_id,
                        )
                        .await
                        .map_err(model_registry_error)?;
                    return Ok(PreparedReply {
                        wire: output.encode_guest(&graph)?,
                        document: None,
                    });
                }
                NativeRegistryRequest::ModelSave { request, provider } => {
                    let input = CoreValue::parse_guest(&request, &graph)?;
                    drop(request);
                    let snapshot = CoreValue::parse_guest(&provider, &graph)?;
                    drop(provider);
                    let pack = reacquire_model_save_pack(
                        &inspection,
                        listener.verified_source(),
                        checkpoint,
                    )?;
                    revalidate()?;
                    let target = inspection
                        .legacy_model_registry(&config.user_data_dir, &identity.profile_id)?;
                    let read = observe_registry(target, Some(1))?;
                    let staged = inspection
                        .stage_live_legacy_model_save(
                            &pack,
                            &identity.profile_id,
                            &input,
                            &snapshot,
                            read,
                        )
                        .await
                        .map_err(model_save_staging_error)?;
                    return decode_model_save_reply(staged, &graph);
                }
                NativeRegistryRequest::ConversationGet {
                    caller,
                    conversation_id,
                } => {
                    let pack = reacquire_conversation_read_pack(
                        &inspection,
                        listener.verified_source(),
                        caller,
                        checkpoint,
                    )?;
                    revalidate()?;
                    let output = inspection
                        .get_legacy_conversation(
                            &pack,
                            &config.user_data_dir,
                            &identity.profile_id,
                            &conversation_id,
                        )
                        .await
                        .map_err(conversation_registry_error)?;
                    return decode_conversation_read_reply(output, &graph);
                }
                NativeRegistryRequest::ConversationReceipt { caller, turn_id } => {
                    let pack = reacquire_saved_receipt_pack(
                        &inspection,
                        listener.verified_source(),
                        caller,
                        checkpoint,
                    )?;
                    revalidate()?;
                    let output = inspection
                        .get_legacy_saved_receipt(
                            &pack,
                            &config.user_data_dir,
                            &identity.profile_id,
                            &turn_id,
                        )
                        .await
                        .map_err(conversation_registry_error)?;
                    return decode_conversation_read_reply(output, &graph);
                }
                NativeRegistryRequest::ConversationConfirmation {
                    caller,
                    conversation_id,
                    terminal,
                } => {
                    let pack = reacquire_conversation_confirmation_pack(
                        &inspection,
                        listener.verified_source(),
                        caller,
                        checkpoint,
                    )?;
                    revalidate()?;
                    let terminal = std::str::from_utf8(&terminal)?;
                    let owner =
                        lock_conversation_store(&inspection, NativeArtifactError::Denied.into())?;
                    let staged = inspection
                        .stage_legacy_conversation_confirmation(&pack, &owner, terminal)
                        .await
                        .map_err(conversation_write_staging_error)?;
                    let reply =
                        decode_conversation_confirmation_reply(staged, &conversation_id, &graph)?;
                    *registry
                        .lock()
                        .map_err(|_| anyhow!("Registry lock unavailable"))? = Some(owner);
                    return Ok(reply);
                }
                NativeRegistryRequest::ConversationAppend {
                    request,
                    expected_revision,
                } => {
                    let pack = reacquire_conversation_append_pack(
                        &inspection,
                        listener.verified_source(),
                        checkpoint,
                    )?;
                    revalidate()?;
                    let owner = lock_conversation_store(
                        &inspection,
                        ConversationWriteRequiresLegacyRead.into(),
                    )
                    .map_err(|error| {
                        match error.downcast_ref::<NativeArtifactError>() {
                            Some(NativeArtifactError::Core(CoreError::Store(
                                StoreError::NotFound,
                            ))) => ConversationWriteRequiresLegacyRead.into(),
                            _ => error,
                        }
                    })?;
                    let staged = inspection
                        .stage_legacy_conversation_append(
                            &pack,
                            &owner,
                            std::str::from_utf8(&request)?,
                        )
                        .await
                        .map_err(conversation_write_staging_error)?;
                    let reply =
                        decode_conversation_append_reply(staged, expected_revision, &graph)?;
                    *registry
                        .lock()
                        .map_err(|_| anyhow!("Registry lock unavailable"))? = Some(owner);
                    return Ok(reply);
                }
                NativeRegistryRequest::ModelResolve { role, identifier } => {
                    let pack = reacquire_model_resolve_pack(
                        &inspection,
                        listener.verified_source(),
                        role,
                        checkpoint,
                    )?;
                    revalidate()?;
                    let output = inspection
                        .resolve_legacy_model(
                            &pack,
                            &config.user_data_dir,
                            &identity.profile_id,
                            &identifier,
                        )
                        .await
                        .map_err(model_registry_error)?;
                    return decode_model_resolve_reply(output, &graph);
                }
                NativeRegistryRequest::TurnRead { caller, turn_id } => {
                    let pack = reacquire_turn_read_pack(
                        &inspection,
                        listener.verified_source(),
                        caller,
                        checkpoint,
                    )?;
                    revalidate()?;
                    let profile = ProfileId::parse(&identity.profile_id)?;
                    let shape = ShapeRef::parse("local.saved-turn-runtime.legacy-read@1.0")?;
                    let operation = OpName::parse("validate")?;
                    let port = inspection
                        .prepare_declared_read_only_port(
                            &pack,
                            &shape,
                            &operation,
                            &profile,
                            execution_deadline,
                            0,
                        )
                        .await?;
                    let data = DataRoot::open_existing(&config.user_data_dir)
                        .map_err(|_| LegacySavedTurnSettlementError::Storage)?;
                    let handle = tokio::runtime::Handle::current();
                    let result = tokio::task::block_in_place(|| {
                        read_legacy_saved_turn(
                            &data,
                            &profile,
                            &turn_id,
                            &graph,
                            execution_deadline,
                            || revalidate().map_err(|_| LegacySavedTurnSettlementError::Revoked),
                            |row| {
                                handle.block_on(invoke_original_saved_turn_read_port(
                                    port, row, &graph,
                                ))
                            },
                        )
                    })?;
                    let wire = match result {
                        Some(record) => record,
                        None => CoreBuffer::copy_from(b"null", &graph)
                            .map_err(|_| LegacySavedTurnSettlementError::Capacity)?,
                    };
                    return Ok(PreparedReply {
                        wire,
                        document: None,
                    });
                }
                NativeRegistryRequest::TurnStart {
                    action,
                    turn_id,
                    conversation_id,
                    conversation_revision,
                    input_digest,
                    source_digest,
                } => {
                    let pack = reacquire_turn_start_pack(
                        &inspection,
                        listener.verified_source(),
                        checkpoint,
                    )?;
                    revalidate()?;
                    let profile = ProfileId::parse(&identity.profile_id)?;
                    let shape = ShapeRef::parse("local.saved-turn-runtime.legacy-start@1.0")?;
                    let operation = OpName::parse(match action {
                        SavedTurnStartAction::Begin => "begin",
                        SavedTurnStartAction::Claim => "claim",
                    })?;
                    let port = inspection
                        .prepare_declared_read_only_port(
                            &pack,
                            &shape,
                            &operation,
                            &profile,
                            execution_deadline,
                            2,
                        )
                        .await?;
                    let data = DataRoot::open_existing(&config.user_data_dir)
                        .map_err(|_| LegacySavedTurnSettlementError::Storage)?;
                    let input = OriginalSavedTurnStartInput {
                        request: LegacySavedTurnStartRequest {
                            turn_id: &turn_id,
                            conversation_id: &conversation_id,
                            conversation_revision,
                            input_digest: &input_digest,
                        },
                        source_digest: &source_digest,
                    };
                    let permit = Arc::clone(
                        publish_permit
                            .as_ref()
                            .context("Turn start permit is missing")?,
                    );
                    let authorize = original_saved_turn_authority(OriginalSavedTurnAuthority {
                        app: app.clone(),
                        manager: Arc::clone(&manager),
                        listener: Arc::clone(&listener),
                        current: Arc::clone(&continue_current),
                        permit,
                        cancellation: cancellation.clone(),
                        deadline: execution_deadline,
                    });
                    let handle = tokio::runtime::Handle::current();
                    let outcome = tokio::task::block_in_place(|| {
                        let evaluate = |step: OriginalSavedTurnStartStep<'_>| match step {
                            OriginalSavedTurnStartStep::Begin => handle.block_on(
                                invoke_original_saved_turn_begin_port(port, &input.request, &graph),
                            ),
                            OriginalSavedTurnStartStep::Claim(row) => {
                                handle.block_on(invoke_original_saved_turn_claim_port(
                                    port,
                                    row,
                                    &input.request,
                                    &graph,
                                ))
                            }
                        };
                        let execute = match action {
                            SavedTurnStartAction::Begin => {
                                begin_legacy_saved_turn_from_admitted_port
                            }
                            SavedTurnStartAction::Claim => {
                                claim_legacy_saved_turn_from_admitted_port
                            }
                        };
                        execute(
                            &data,
                            &profile,
                            &input,
                            &graph,
                            execution_deadline,
                            authorize,
                            evaluate,
                        )
                    })?;
                    let OriginalSavedTurnStartOutcome::Turn { body, claimed } = outcome else {
                        return Err(TurnStartRequiresLegacyCodec.into());
                    };
                    return Ok(PreparedReply {
                        wire: encode_turn_start_reply(&body, claimed, &graph)?,
                        document: None,
                    });
                }
                NativeRegistryRequest::TurnSettlement {
                    caller,
                    turn_id,
                    input_digest,
                    expected_revision,
                    result_reference,
                } => {
                    let pack = reacquire_turn_settlement_pack(
                        &inspection,
                        listener.verified_source(),
                        caller,
                        checkpoint,
                    )?;
                    revalidate()?;
                    let profile = ProfileId::parse(&identity.profile_id)?;
                    let shape = ShapeRef::parse("local.saved-turn-runtime.legacy-settlement@1.0")?;
                    let operation = OpName::parse("prepare")?;
                    let port = inspection
                        .prepare_declared_read_only_port(
                            &pack,
                            &shape,
                            &operation,
                            &profile,
                            execution_deadline,
                            3,
                        )
                        .await?;
                    let data = DataRoot::open_existing(&config.user_data_dir)?;
                    let request = LegacySavedTurnSettlementRequest {
                        turn_id: &turn_id,
                        input_digest: &input_digest,
                        expected_revision,
                        result_reference: result_reference.as_bytes(),
                    };
                    let permit = Arc::clone(
                        publish_permit
                            .as_ref()
                            .context("Turn settlement permit is missing")?,
                    );
                    let authorize = original_saved_turn_authority(OriginalSavedTurnAuthority {
                        app: app.clone(),
                        manager: Arc::clone(&manager),
                        listener: Arc::clone(&listener),
                        current: Arc::clone(&continue_current),
                        permit,
                        cancellation: cancellation.clone(),
                        deadline: execution_deadline,
                    });
                    let handle = tokio::runtime::Handle::current();
                    let wire = tokio::task::block_in_place(|| {
                        settle_legacy_saved_turn_from_admitted_port(
                            &data,
                            &profile,
                            &request,
                            &graph,
                            execution_deadline,
                            authorize,
                            |row| {
                                handle.block_on(invoke_original_saved_turn_port(
                                    port, row, &request, &graph,
                                ))
                            },
                        )
                    })
                    .map_err(|error| match error {
                        LegacySavedTurnSettlementError::RequiresLegacyCodec => {
                            TurnSettlementRequiresLegacyCodec.into()
                        }
                        _ => anyhow::Error::from(error),
                    })?;
                    return Ok(PreparedReply {
                        wire,
                        document: None,
                    });
                }
                NativeRegistryRequest::SavedOrchestration { input } => {
                    let pack = reacquire_saved_orchestration_pack(
                        &inspection,
                        listener.verified_source(),
                        checkpoint,
                    )?;
                    revalidate()?;
                    let profile = ProfileId::parse(&identity.profile_id)?;
                    let shape = ShapeRef::parse("local.saved-orchestration.legacy-step@1.0")?;
                    let operation = OpName::parse("advance")?;
                    let port = inspection
                        .prepare_declared_read_only_port(
                            &pack,
                            &shape,
                            &operation,
                            &profile,
                            execution_deadline,
                            0,
                        )
                        .await?;
                    revalidate()?;
                    let funding = input_funding
                        .take()
                        .context("saved orchestration input funding is missing")?;
                    let output =
                        invoke_original_saved_orchestration_port(port, input, funding).await?;
                    return decode_saved_orchestration_reply(output.as_str(), &graph);
                }
            };
            let input = CoreValue::parse_guest(&request, &graph)?;
            drop(request);
            let input_plan = apply
                .as_ref()
                .map(|plan| CoreValue::parse_guest(plan, &graph))
                .transpose()?;
            drop(apply);
            let pack = reacquire_provider_prepare_pack(
                &inspection,
                listener.verified_source(),
                checkpoint,
            )?;
            revalidate()?;
            let target =
                inspection.legacy_provider_registry(&config.user_data_dir, &identity.profile_id)?;
            let read = observe_registry(target, publishes.then_some(4))?;
            let output = match input_plan.as_ref() {
                Some(plan) => {
                    let staged = inspection
                        .stage_live_legacy_provider_commit(
                            &pack,
                            &identity.profile_id,
                            &input,
                            plan,
                            read,
                        )
                        .await;
                    if let Err(NativeArtifactError::ProviderRejected(error)) = &staged {
                        if error.code() == "limit-exceeded" {
                            registry_limit.store(true, Ordering::Release);
                        }
                    }
                    return decode_staged_reply(staged?, plan, &graph);
                }
                None => {
                    inspection
                        .invoke_live_legacy_provider_prepare(
                            &pack,
                            &identity.profile_id,
                            &input,
                            read,
                        )
                        .await?
                }
            };
            Ok::<_, anyhow::Error>(PreparedReply {
                wire: output.encode_guest(&graph)?,
                document: None,
            })
        });
        tokio::pin!(prepare);
        tokio::select! {
            result = &mut prepare => result,
            () = tokio::time::sleep_until(execution_deadline.into()) => {
                cancellation.cancel();
                prepare.await
            }
        }
    };
    let finished = inspection.finish().await;
    let execution_returned = outcome.is_ok();
    let (result, cleanup_verified) = match finished {
        Ok(sources) => {
            drop(sources);
            (
                revalidate().and_then(|()| {
                    let outcome = outcome.map_err(|error| anyhow!(error))?;
                    if outcome.is_err() && registry_limit.load(Ordering::Acquire) {
                        let error = if model_save {
                            NativeArtifactError::ModelRegistryLimitBeforePublish
                        } else {
                            NativeArtifactError::ProviderRegistryLimitBeforePublish
                        };
                        return Err(error.into());
                    }
                    let reply = outcome?;
                    if let Some(document) = reply.document {
                        let target = registry
                            .lock()
                            .map_err(|_| anyhow!("Registry lock unavailable"))?
                            .take()
                            .context("native staging lost its Registry lock")?;
                        let checkpoint_count = Cell::new(0);
                        target.publish(document.as_bytes(), || {
                            revalidate().map_err(publication_checkpoint_error)?;
                            if let Some(permit) = &publish_permit {
                                // The second checkpoint is the Store's final guard before rename.
                                match checkpoint_count.replace(checkpoint_count.get() + 1) {
                                    0 => (),
                                    1 => permit().map_err(publication_checkpoint_error)?,
                                    _ => return Err(NativeArtifactError::Denied),
                                }
                            }
                            Ok(())
                        })?;
                    }
                    Ok(reply.wire)
                }),
                execution_returned,
            )
        }
        // These are emitted by finish after the Store has retired and all
        // physical inspection work has joined.
        Err(
            error @ NativeArtifactError::Execution(
                NativeDataIoError::Cancelled | NativeDataIoError::Deadline,
            ),
        ) => (Err(error.into()), execution_returned),
        Err(error) => (Err(error.into()), false),
    };
    drop(registry);
    drop(publish_permit);
    drop(continue_current);
    drop(config);
    drop(app);
    drop(custody);
    (
        Ok(Prepared {
            manager,
            listener,
            result,
        }),
        cleanup_verified,
    )
}

struct OriginalSavedTurnAuthority {
    app: AppHandle,
    manager: Arc<ApplicationProcessManager>,
    listener: Arc<ProviderPrepareListener>,
    current: ContinueCurrent,
    permit: ContinueCurrent,
    cancellation: CoreCancellation,
    deadline: Instant,
}

fn original_saved_turn_authority(
    authority: OriginalSavedTurnAuthority,
) -> impl FnMut(OriginalSavedTurnCheck) -> std::result::Result<(), LegacySavedTurnSettlementError> {
    move |phase| {
        const REVOKED: LegacySavedTurnSettlementError = LegacySavedTurnSettlementError::Revoked;
        let check = || {
            check_live(authority.deadline, Some(&authority.cancellation)).map_err(|_| REVOKED)?;
            if !authority.listener.owns_socket() {
                return Err(REVOKED);
            }
            authority
                .manager
                .require_provider_prepare_admission(&authority.listener)
                .map_err(|_| REVOKED)?;
            let config = authority.app.try_state::<AppConfig>().ok_or(REVOKED)?;
            require_current_launch_identity(&config, &authority.listener.verified_source().identity)
                .map_err(|_| REVOKED)
        };
        check()?;
        match phase {
            OriginalSavedTurnCheck::Live => (authority.current)(),
            OriginalSavedTurnCheck::Publish => (authority.permit)(),
        }
        .map_err(|_| REVOKED)?;
        check()
    }
}

fn encode_turn_start_reply(
    body: &CoreBuffer,
    claimed: bool,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreBuffer, LegacySavedTurnSettlementError> {
    if body.len() > TURN_READ_REPLY_LIMIT {
        return Err(LegacySavedTurnSettlementError::Capacity);
    }
    let prefix: &[u8] = if claimed {
        b"{\"claimed\":true,\"turn\":"
    } else {
        b"{\"claimed\":false,\"turn\":"
    };
    let row_end = prefix.len() + body.len();
    CoreBuffer::write_exact(row_end + 1, budget, |output| {
        output[..prefix.len()].copy_from_slice(prefix);
        output[prefix.len()..row_end].copy_from_slice(body.as_bytes());
        output[row_end] = b'}';
        Ok(())
    })
    .map_err(|_| LegacySavedTurnSettlementError::Capacity)
}

fn model_registry_error(error: NativeArtifactError) -> anyhow::Error {
    match &error {
        NativeArtifactError::TooLarge => ModelRegistryRequiresLegacyRead.into(),
        NativeArtifactError::ModelRegistryRejected(error)
            if matches!(error.code(), "unavailable" | "limit-exceeded") =>
        {
            ModelRegistryRequiresLegacyRead.into()
        }
        _ => error.into(),
    }
}

fn decode_saved_orchestration_reply(
    output: &str,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<PreparedReply> {
    if output.len() > SAVED_ORCHESTRATION_FRAME_LIMIT {
        return Err(SavedOrchestrationStepError::Capacity.into());
    }
    let raw: &RawValue =
        serde_json::from_str(output).context("saved orchestration port returned invalid JSON")?;
    ensure!(
        raw.get().starts_with('{'),
        "saved orchestration port returned a non-object result"
    );
    let wire = CoreBuffer::copy_from(output.as_bytes(), budget)
        .map_err(|_| SavedOrchestrationStepError::Capacity)?;
    Ok(PreparedReply {
        wire,
        document: None,
    })
}

fn conversation_registry_error(error: NativeArtifactError) -> anyhow::Error {
    match &error {
        NativeArtifactError::TooLarge => ConversationRegistryRequiresLegacyRead.into(),
        NativeArtifactError::ConversationRegistryRejected(error)
            if matches!(error.code(), "unavailable" | "limit-exceeded") =>
        {
            ConversationRegistryRequiresLegacyRead.into()
        }
        _ => error.into(),
    }
}

fn model_save_staging_error(error: NativeArtifactError) -> anyhow::Error {
    match &error {
        // The fixed Save export reserves these codes for compatibility limits.
        NativeArtifactError::ModelRegistryRejected(error)
            if matches!(error.code(), "unavailable" | "limit-exceeded") =>
        {
            ModelRegistryRequiresLegacyRead.into()
        }
        _ => error.into(),
    }
}

fn conversation_write_staging_error(error: NativeArtifactError) -> anyhow::Error {
    match &error {
        NativeArtifactError::TooLarge => ConversationWriteRequiresLegacyRead.into(),
        NativeArtifactError::ConversationRegistryRejected(error)
            if matches!(error.code(), "unavailable" | "limit-exceeded") =>
        {
            ConversationWriteRequiresLegacyRead.into()
        }
        _ => error.into(),
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ConversationConfirmationReply<'a> {
    confirmed: bool,
    #[serde(borrow)]
    conversation_id: std::borrow::Cow<'a, str>,
    conversation_revision: u64,
    #[serde(rename = "store_revision")]
    _store_revision: u64,
}

fn decode_conversation_confirmation_reply(
    staged: CoreValue,
    conversation_id: &str,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<PreparedReply> {
    let owner_result = staged
        .get_key("owner_result")
        .context("native Conversation confirmation omitted its owner result")?;
    let owner_result = owner_result
        .as_str()
        .context("native Conversation confirmation returned a non-string owner result")?;
    if owner_result.len() > CONVERSATION_CONFIRMATION_LIMIT {
        return Err(ConversationWriteRequiresLegacyRead.into());
    }
    let result: ConversationConfirmationReply<'_> = serde_json::from_str(owner_result)
        .context("native Conversation confirmation returned an invalid owner result")?;
    ensure!(
        result.conversation_id == conversation_id && result.conversation_revision > 0,
        "native Conversation confirmation returned a different owner result"
    );
    let next = staged
        .get_key("next_document")
        .context("native Conversation confirmation omitted its replacement decision")?;
    let document = match next.inspect() {
        CoreRef::Null => None,
        CoreRef::Str(raw) if result.confirmed && raw.starts_with('{') && raw.ends_with('}') => {
            Some(CoreBuffer::copy_from(raw.as_bytes(), budget)?)
        }
        _ => {
            return Err(anyhow!(
                "native Conversation confirmation returned an invalid document"
            ))
        }
    };
    Ok(PreparedReply {
        // Preserve the original owner's exact text, including field order and whitespace.
        wire: CoreBuffer::copy_from(owner_result.as_bytes(), budget)?,
        document,
    })
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ConversationAppendReply<'a> {
    action: std::borrow::Cow<'a, str>,
    #[serde(borrow)]
    message: &'a RawValue,
    conversation_revision: u64,
    store_revision: i64,
}

fn decode_conversation_append_reply(
    staged: CoreValue,
    expected_revision: u64,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<PreparedReply> {
    let owner_result = staged
        .get_key("owner_result")
        .context("native Conversation append omitted its owner result")?;
    let owner_result = owner_result
        .as_str()
        .context("native Conversation append returned a non-string owner result")?;
    if owner_result.len() > CONVERSATION_APPEND_LIMIT {
        return Err(ConversationWriteRequiresLegacyRead.into());
    }
    let result: ConversationAppendReply<'_> = serde_json::from_str(owner_result)
        .context("native Conversation append returned an invalid owner result")?;
    ensure!(
        result.action == "message_appended"
            && result.message.get().starts_with('{')
            && expected_revision.checked_add(1) == Some(result.conversation_revision),
        "native Conversation append returned a different owner result"
    );
    if result.store_revision <= 0 {
        return Err(ConversationWriteRequiresLegacyRead.into());
    }
    let next = staged
        .get_key("next_document")
        .context("native Conversation append omitted its replacement document")?;
    let document = next
        .as_str()
        .filter(|raw| raw.starts_with('{') && raw.ends_with('}'))
        .context("native Conversation append returned an invalid document")?;
    Ok(PreparedReply {
        wire: CoreBuffer::copy_from(owner_result.as_bytes(), budget)?,
        document: Some(CoreBuffer::copy_from(document.as_bytes(), budget)?),
    })
}

fn decode_model_save_reply(
    staged: CoreValue,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<PreparedReply> {
    let owner_result = staged
        .get_key("owner_result")
        .context("native Model staging omitted its owner result")?;
    let owner_result = owner_result
        .as_str()
        .context("native Model staging returned a non-string owner result")?;
    ensure!(
        owner_result.len() <= MODEL_SAVE_REPLY_LIMIT
            && owner_result.starts_with('{')
            && owner_result.ends_with('}'),
        "native Model staging returned an invalid owner result"
    );
    let document = staged
        .get_key("document")
        .context("native Model staging omitted its document")?;
    let document = match document.inspect() {
        CoreRef::Null => None,
        CoreRef::Str(raw) if raw.ends_with('\n') => {
            Some(CoreBuffer::copy_from(raw.as_bytes(), budget)?)
        }
        _ => return Err(anyhow!("native Model staging returned an invalid document")),
    };
    Ok(PreparedReply {
        // CPython owns this JSON. Preserve its spelling and numeric literals.
        wire: CoreBuffer::copy_from(owner_result.as_bytes(), budget)?,
        document,
    })
}

fn decode_conversation_read_reply(
    output: CoreValue,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<PreparedReply> {
    decode_owner_read_reply(
        output,
        budget,
        CONVERSATION_READ_REPLY_LIMIT,
        ConversationRegistryRequiresLegacyRead,
    )
}

fn decode_model_resolve_reply(
    output: CoreValue,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<PreparedReply> {
    decode_owner_read_reply(
        output,
        budget,
        MODEL_RESOLVE_REPLY_LIMIT,
        ModelRegistryRequiresLegacyRead,
    )
}

fn decode_owner_read_reply(
    output: CoreValue,
    budget: &Arc<dyn AllocationBudget>,
    limit: usize,
    oversized: impl Into<anyhow::Error>,
) -> Result<PreparedReply> {
    let owner_result = output
        .get_key("owner_result")
        .context("native Registry read omitted its owner result")?;
    let owner_result = owner_result
        .as_str()
        .context("native Registry read returned a non-string owner result")?;
    ensure!(
        owner_result.starts_with('{') && owner_result.ends_with('}'),
        "native Registry read returned an invalid owner result"
    );
    if owner_result.len() > limit {
        return Err(oversized.into());
    }
    Ok(PreparedReply {
        // CPython owns the result, including its numeric literals and spelling.
        wire: CoreBuffer::copy_from(owner_result.as_bytes(), budget)?,
        document: None,
    })
}

fn decode_staged_reply(
    staged: CoreValue,
    plan: &CoreValue,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<PreparedReply> {
    let document = staged
        .get_key("document")
        .context("native Provider staging omitted its document")?;
    let raw = document
        .as_str()
        .context("native Provider staged document is invalid")?;
    let reply = staged
        .get_key("result")
        .context("native Provider staging omitted its result")?;
    let expected = plan
        .get_key("provider_instance_id")
        .context("native Provider plan omitted its connection")?;
    let configured = reply
        .get_key("provider_instance_id")
        .context("native Provider staging omitted its connection")?;
    ensure!(
        reply
            .get_key("configured")
            .and_then(|value| value.as_bool())
            == Some(true)
            && expected.as_str().is_some()
            && configured.as_str() == expected.as_str()
            && raw.ends_with('\n'),
        "native Provider staging returned a different result"
    );
    Ok(PreparedReply {
        wire: reply.encode_guest(budget)?,
        document: Some(CoreBuffer::copy_from(raw.as_bytes(), budget)?),
    })
}

fn registry_read(
    registry: Arc<Mutex<Option<NativeRegistry>>>,
    registry_limit: Arc<AtomicBool>,
    continue_current: ContinueCurrent,
    cancellation: CoreCancellation,
    deadline: Instant,
    locked_observation: Option<usize>,
) -> LegacyRegistryRead {
    let mut observations = 0;
    Box::new(move |budget| {
        observations += 1;
        let mut registry = registry
            .lock()
            .map_err(|_| anyhow!("Registry lock unavailable"))?;
        let registry = registry
            .as_mut()
            .context("native Registry is unavailable")?;
        let observed = if locked_observation == Some(observations) {
            registry.observe_locked(budget, || {
                check_live(deadline, Some(&cancellation))
                    .and_then(|()| continue_current())
                    .and_then(|()| check_live(deadline, Some(&cancellation)))
                    .map_err(publication_checkpoint_error)
            })
        } else {
            registry.observe(budget)
        };
        if matches!(
            &observed,
            Err(NativeArtifactError::ProviderRegistryLimitBeforePublish
                | NativeArtifactError::ModelRegistryLimitBeforePublish)
        ) {
            registry_limit.store(true, Ordering::Release);
        }
        Ok(observed?)
    })
}

fn publication_checkpoint_error(error: anyhow::Error) -> NativeArtifactError {
    error.downcast().unwrap_or(NativeArtifactError::Denied)
}

fn check_live(deadline: Instant, cancellation: Option<&CoreCancellation>) -> Result<()> {
    if Instant::now() >= deadline {
        return Err(NativeArtifactError::from(NativeDataIoError::Deadline).into());
    }
    if cancellation.is_some_and(CoreCancellation::is_cancelled) {
        return Err(NativeArtifactError::from(NativeDataIoError::Cancelled).into());
    }
    Ok(())
}

fn guarded_registry_read(
    mut read: LegacyRegistryRead,
    continue_current: ContinueCurrent,
    cancellation: CoreCancellation,
    deadline: Instant,
) -> LegacyRegistryRead {
    Box::new(move |budget| {
        let check = || {
            check_live(deadline, Some(&cancellation))?;
            continue_current()?;
            check_live(deadline, Some(&cancellation))
        };
        check()?;
        let observed = read(budget);
        check()?;
        observed
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;
    use tobkiri_core::{AllocationBudget, AllocationError, AllocationRequest};

    struct TestBudget;

    impl AllocationBudget for TestBudget {
        fn reserve(&self, _: AllocationRequest) -> Result<(), AllocationError> {
            Ok(())
        }

        fn release(&self, _: u64) {}
    }

    #[test]
    fn saved_orchestration_uses_its_own_proof_and_funds_transport_capacity() {
        use crate::host_contract::ExecutionProfileIdentity;

        let identity = ExecutionProfileIdentity {
            profile_id: "defaults".into(),
            profile_revision: "revision:fixture".into(),
            activation_id: "activation:fixture".into(),
            plan_digest: "sha256:fixture".into(),
        };
        let proof =
            VerifiedRegistryLaunch::test_saved_orchestration_proof(identity.clone(), "catalog");
        let append = VerifiedRegistryLaunch::test_conversation_append_proof(identity, "catalog");
        let input = |size| {
            let mut input = Vec::with_capacity(size);
            input.extend_from_slice(b"{}");
            NativeRegistryRequest::SavedOrchestration { input }
        };
        let request = input(SAVED_ORCHESTRATION_FRAME_LIMIT);
        assert!(request.has_launch_proof(&proof));
        assert!(!request.has_launch_proof(&append));
        assert!(!request.requires_publication_permit());
        assert_eq!(
            request.input_capacity().unwrap(),
            SAVED_ORCHESTRATION_FRAME_LIMIT
        );
        assert_eq!(
            input(SAVED_ORCHESTRATION_FRAME_LIMIT + 1)
                .input_capacity()
                .unwrap_err()
                .downcast::<SavedOrchestrationStepError>()
                .unwrap(),
            SavedOrchestrationStepError::Capacity
        );
        let empty = NativeRegistryRequest::SavedOrchestration { input: Vec::new() };
        assert_eq!(
            empty
                .input_capacity()
                .unwrap_err()
                .downcast::<SavedOrchestrationStepError>()
                .unwrap(),
            SavedOrchestrationStepError::Invalid
        );
    }

    #[test]
    fn saved_orchestration_returns_only_bounded_original_object_bytes() {
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let output = r#" {"kind":"intent", "sample":"日本語"} "#;
        let reply = decode_saved_orchestration_reply(output, &budget).unwrap();
        assert_eq!(reply.wire.as_bytes(), output.as_bytes());
        assert!(reply.document.is_none());
        for invalid in ["null", "[]", "{broken}", "{} {}"] {
            assert!(decode_saved_orchestration_reply(invalid, &budget).is_err());
        }
        let oversized = format!(
            "{{\"data\":\"{}\"}}",
            "a".repeat(SAVED_ORCHESTRATION_FRAME_LIMIT)
        );
        let Err(error) = decode_saved_orchestration_reply(&oversized, &budget) else {
            panic!("accepted an oversized result");
        };
        assert_eq!(
            error.downcast::<SavedOrchestrationStepError>().unwrap(),
            SavedOrchestrationStepError::Capacity
        );
    }

    #[test]
    fn confirmation_request_only_accepts_its_role_proof_and_accounts_for_terminal() {
        use crate::host_contract::ExecutionProfileIdentity;
        use OriginalSavedReceiptRole::{SavedReconcile, SavedTurn};

        let identity = ExecutionProfileIdentity {
            profile_id: "defaults".into(),
            profile_revision: "revision:fixture".into(),
            activation_id: "activation:fixture".into(),
            plan_digest: "sha256:fixture".into(),
        };
        let make_request = |caller, size| NativeRegistryRequest::ConversationConfirmation {
            caller,
            conversation_id: "conversation".into(),
            terminal: vec![b' '; size],
        };
        for granted in [SavedTurn, SavedReconcile] {
            let confirmation = VerifiedRegistryLaunch::test_conversation_confirmation_proof(
                identity.clone(),
                "catalog:fixture",
                granted,
            );
            let receipt = VerifiedRegistryLaunch::test_saved_receipt_proof(
                identity.clone(),
                "catalog:fixture",
                granted,
            );
            for caller in [SavedTurn, SavedReconcile] {
                let request = make_request(caller, CONVERSATION_CONFIRMATION_LIMIT);
                assert_eq!(request.has_launch_proof(&confirmation), caller == granted);
                assert!(!request.has_launch_proof(&receipt));
                assert!(request.requires_publication_permit());
                assert_eq!(
                    request.input_capacity().unwrap(),
                    CONVERSATION_CONFIRMATION_LIMIT + "conversation".len(),
                );
            }
        }
        assert!(make_request(SavedTurn, 0).input_capacity().is_err());
        assert!(make_request(SavedTurn, CONVERSATION_CONFIRMATION_LIMIT + 1)
            .input_capacity()
            .is_err());
    }

    #[test]
    fn turn_read_requires_its_own_role_proof_and_has_no_publication_permit() {
        use crate::host_contract::ExecutionProfileIdentity;

        let identity = ExecutionProfileIdentity {
            profile_id: "defaults".into(),
            profile_revision: "revision:fixture".into(),
            activation_id: "activation:fixture".into(),
            plan_digest: "sha256:fixture".into(),
        };
        for granted in OriginalSavedReceiptRole::ALL {
            let read = VerifiedRegistryLaunch::test_turn_read_proof(
                identity.clone(),
                "catalog:fixture",
                granted,
            );
            let writer = VerifiedRegistryLaunch::test_conversation_confirmation_proof(
                identity.clone(),
                "catalog:fixture",
                granted,
            );
            let receipt = VerifiedRegistryLaunch::test_saved_receipt_proof(
                identity.clone(),
                "catalog:fixture",
                granted,
            );
            let settlement = VerifiedRegistryLaunch::test_turn_settlement_proof(
                identity.clone(),
                "catalog:fixture",
                match granted {
                    OriginalSavedReceiptRole::SavedTurn => SettlementCaller::SavedTurn,
                    OriginalSavedReceiptRole::SavedReconcile => SettlementCaller::SavedReconcile,
                },
            );
            assert!(!read.permits_conversation_confirmation(granted));
            for caller in OriginalSavedReceiptRole::ALL {
                let request = NativeRegistryRequest::TurnRead {
                    caller,
                    turn_id: "turn".to_owned(),
                };
                assert_eq!(request.has_launch_proof(&read), caller == granted);
                assert!(!request.has_launch_proof(&writer));
                assert!(!request.has_launch_proof(&receipt));
                assert!(!request.has_launch_proof(&settlement));
                assert!(!request.requires_publication_permit());
                assert_eq!(request.input_capacity().unwrap(), "turn".len());
            }
        }
    }

    #[test]
    fn confirmation_preserves_the_four_owner_fields_and_distinguishes_no_op() {
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let owner = r#"{"store_revision":9007199254740993, "confirmed":true,"conversation_id":"conv","conversation_revision":4}"#;
        let replacement = r#"{"conversations": {}, "revision": 9007199254740993}"#;
        let decode = |owner: &str, next_document: Option<&str>| {
            let staged = serde_json::json!({"owner_result": owner, "next_document": next_document});
            let staged =
                CoreValue::parse_guest(&serde_json::to_vec(&staged).unwrap(), &budget).unwrap();
            decode_conversation_confirmation_reply(staged, "conv", &budget)
        };
        let promoted = decode(owner, Some(replacement)).unwrap();
        assert_eq!(promoted.wire.as_bytes(), owner.as_bytes());
        assert_eq!(
            promoted.document.unwrap().as_bytes(),
            replacement.as_bytes()
        );
        let replay = decode(owner, None).unwrap();
        assert_eq!(replay.wire.as_bytes(), owner.as_bytes());
        assert!(replay.document.is_none());

        let declined = owner.replace(":true", ":false");
        let no_op = decode(&declined, None).unwrap();
        assert_eq!(no_op.wire.as_bytes(), declined.as_bytes());
        assert!(no_op.document.is_none());
        assert!(decode(&declined, Some(replacement)).is_err());
        assert!(decode(owner, Some("[]")).is_err());
        assert!(decode(&owner.replace("\"conv\"", "\"another\""), Some(replacement)).is_err());
        assert!(decode(&format!(r#"{{"extra":null,{}"#, &owner[1..]), None).is_err());
        let oversized = format!("{}{}", " ".repeat(CONVERSATION_CONFIRMATION_LIMIT), owner);
        let Err(error) = decode(&oversized, None) else {
            panic!("accepted an oversized owner result");
        };
        assert!(error.is::<ConversationWriteRequiresLegacyRead>());
    }

    #[test]
    fn append_requires_its_own_launch_proof_and_funds_the_raw_request() {
        use crate::host_contract::ExecutionProfileIdentity;

        let identity = ExecutionProfileIdentity {
            profile_id: "defaults".into(),
            profile_revision: "revision:fixture".into(),
            activation_id: "activation:fixture".into(),
            plan_digest: "sha256:fixture".into(),
        };
        let proof = VerifiedRegistryLaunch::test_conversation_append_proof(
            identity.clone(),
            "catalog:fixture",
        );
        let confirmation = VerifiedRegistryLaunch::test_conversation_confirmation_proof(
            identity,
            "catalog:fixture",
            OriginalSavedReceiptRole::SavedTurn,
        );
        let operation = |capacity| NativeRegistryRequest::ConversationAppend {
            request: vec![b' '; capacity],
            expected_revision: 2,
        };
        let append = operation(CONVERSATION_APPEND_LIMIT);
        assert!(append.has_launch_proof(&proof));
        assert!(!append.has_launch_proof(&confirmation));
        assert!(append.requires_publication_permit());
        assert_eq!(append.input_capacity().unwrap(), CONVERSATION_APPEND_LIMIT);
        assert!(operation(0).input_capacity().is_err());
        assert!(operation(CONVERSATION_APPEND_LIMIT + 1)
            .input_capacity()
            .is_err());
    }

    #[test]
    fn append_preserves_the_exact_owner_result_and_requires_replacement() {
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let owner = r#"{"message": {"id":"会話", "precise":9007199254740993}, "action":"message_appended","conversation_revision":3,"store_revision":9007199254740993}"#;
        let replacement = r#"{"conversations": {}, "revision": 9007199254740993}"#;
        let decode = |owner: &str, next_document: Option<&str>| {
            let staged = serde_json::json!({"owner_result": owner, "next_document": next_document});
            let staged =
                CoreValue::parse_guest(&serde_json::to_vec(&staged).unwrap(), &budget).unwrap();
            decode_conversation_append_reply(staged, 2, &budget)
        };
        let appended = decode(owner, Some(replacement)).unwrap();
        assert_eq!(appended.wire.as_bytes(), owner.as_bytes());
        assert_eq!(
            appended.document.unwrap().as_bytes(),
            replacement.as_bytes()
        );
        assert!(decode(owner, None).is_err());
        assert!(decode(owner, Some("[]")).is_err());
        for invalid in [
            owner.replace("message_appended", "message_updated"),
            owner.replace(
                r#""conversation_revision":3"#,
                r#""conversation_revision":4"#,
            ),
            owner.replace(
                r#""store_revision":9007199254740993"#,
                r#""store_revision":0"#,
            ),
            owner.replace(r#"{"id":"会話", "precise":9007199254740993}"#, "null"),
            format!(r#"{{"extra":null,{}"#, &owner[1..]),
        ] {
            assert!(decode(&invalid, Some(replacement)).is_err());
        }
        let oversized = format!("{}{}", " ".repeat(CONVERSATION_APPEND_LIMIT), owner);
        let Err(error) = decode(&oversized, Some(replacement)) else {
            panic!("accepted an oversized owner result");
        };
        assert!(error.is::<ConversationWriteRequiresLegacyRead>());
    }

    #[test]
    fn model_save_accounts_for_both_input_buffers() {
        let mut request = Vec::with_capacity(32);
        request.extend(b"{}");
        let mut provider = Vec::with_capacity(64);
        provider.extend(b"{}");
        let expected = request.capacity() + provider.capacity();
        assert_eq!(
            NativeRegistryRequest::ModelSave { request, provider }
                .input_capacity()
                .unwrap(),
            expected
        );
        assert!(NativeRegistryRequest::ModelSave {
            request: b"{}".to_vec(),
            provider: Vec::new(),
        }
        .input_capacity()
        .is_err());
    }

    #[test]
    fn model_save_sends_the_opaque_owner_result_and_keeps_the_document_local() {
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let owner = r#"{"z": 9007199254740993, "a": NaN}"#;
        let document = "{\"private\":true}\n";
        let staged = serde_json::json!({
            "result": {"public": true},
            "owner_result": owner,
            "document": document,
        });
        let staged =
            CoreValue::parse_guest(&serde_json::to_vec(&staged).unwrap(), &budget).unwrap();
        let reply = decode_model_save_reply(staged, &budget).unwrap();
        assert_eq!(reply.wire.as_bytes(), owner.as_bytes());
        assert_eq!(reply.document.unwrap().as_bytes(), document.as_bytes());
    }

    #[test]
    fn model_save_distinguishes_a_no_op_from_an_invalid_document() {
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let no_op = CoreValue::parse_guest(
            br#"{"result":{},"owner_result":"{}","document":null}"#,
            &budget,
        )
        .unwrap();
        assert!(decode_model_save_reply(no_op, &budget)
            .unwrap()
            .document
            .is_none());
        let invalid = CoreValue::parse_guest(
            br#"{"result":{},"owner_result":"{}","document":"{}"}"#,
            &budget,
        )
        .unwrap();
        assert!(decode_model_save_reply(invalid, &budget).is_err());
    }

    #[test]
    fn conversation_get_keeps_the_original_owner_result_and_has_no_document() {
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let owner =
            r#"{"conversation": {"id": "会話", "precise": 9007199254740993, "number": NaN}}"#;
        let output = serde_json::json!({"owner_result": owner});
        let output =
            CoreValue::parse_guest(&serde_json::to_vec(&output).unwrap(), &budget).unwrap();
        let reply = decode_conversation_read_reply(output, &budget).unwrap();
        assert_eq!(reply.wire.as_bytes(), owner.as_bytes());
        assert!(reply.document.is_none());
    }

    #[test]
    fn saved_receipt_keeps_null_and_opaque_original_evidence_without_a_document() {
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        for owner in [
            r#"{"receipt": null}"#,
            r#"{"receipt": {"message_id": "保存", "precise": 9007199254740993, "cost": -0.0}}"#,
        ] {
            let output = serde_json::json!({"owner_result": owner});
            let output =
                CoreValue::parse_guest(&serde_json::to_vec(&output).unwrap(), &budget).unwrap();
            let reply = decode_conversation_read_reply(output, &budget).unwrap();
            assert_eq!(reply.wire.as_bytes(), owner.as_bytes());
            assert!(reply.document.is_none());
        }
    }

    #[test]
    fn saved_receipt_request_requires_its_own_caller_proof() {
        use crate::host_contract::ExecutionProfileIdentity;
        use OriginalSavedReceiptRole::{SavedReconcile, SavedTurn};

        let identity = ExecutionProfileIdentity {
            profile_id: "defaults".into(),
            profile_revision: "revision:fixture".into(),
            activation_id: "activation:fixture".into(),
            plan_digest: "sha256:fixture".into(),
        };
        let conversation = NativeRegistryRequest::ConversationGet {
            caller: ConversationReadCaller::SavedTurn,
            conversation_id: "stored".into(),
        };
        for granted in [SavedTurn, SavedReconcile] {
            let proof = VerifiedRegistryLaunch::test_saved_receipt_proof(
                identity.clone(),
                "catalog:fixture",
                granted,
            );
            assert!(!conversation.has_launch_proof(&proof));
            for caller in [SavedTurn, SavedReconcile] {
                let request = NativeRegistryRequest::ConversationReceipt {
                    caller,
                    turn_id: "turn".into(),
                };
                assert_eq!(request.has_launch_proof(&proof), caller == granted);
            }
        }
    }

    #[test]
    fn conversation_get_bounds_the_owner_result_and_rejects_invalid_shapes() {
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        for invalid in [
            serde_json::json!({}),
            serde_json::json!({"owner_result": {"conversation": {}}}),
            serde_json::json!({"owner_result": "null"}),
            serde_json::json!({"owner_result": "[{}]"}),
        ] {
            let invalid =
                CoreValue::parse_guest(&serde_json::to_vec(&invalid).unwrap(), &budget).unwrap();
            let Err(error) = decode_conversation_read_reply(invalid, &budget) else {
                panic!("accepted an invalid owner result");
            };
            assert!(!error.is::<ConversationRegistryRequiresLegacyRead>());
        }

        let prefix = r#"{"conversation":{"content":""#;
        let suffix = r#""}}"#;
        for extra in [0, 1] {
            let padding =
                "a".repeat(CONVERSATION_READ_REPLY_LIMIT - prefix.len() - suffix.len() + extra);
            let owner = format!("{prefix}{padding}{suffix}");
            let output = serde_json::json!({"owner_result": owner});
            let output =
                CoreValue::parse_guest(&serde_json::to_vec(&output).unwrap(), &budget).unwrap();
            let result = decode_conversation_read_reply(output, &budget);
            if extra == 0 {
                assert_eq!(result.unwrap().wire.len(), CONVERSATION_READ_REPLY_LIMIT);
            } else {
                let Err(error) = result else {
                    panic!("accepted an oversized owner result");
                };
                assert!(error.is::<ConversationRegistryRequiresLegacyRead>());
            }
        }
    }

    #[test]
    fn conversation_get_distinguishes_capacity_from_native_io_failure() {
        assert!(conversation_registry_error(NativeArtifactError::TooLarge)
            .is::<ConversationRegistryRequiresLegacyRead>());
        let error = NativeArtifactError::Execution(NativeDataIoError::Unavailable);
        assert!(matches!(
            conversation_registry_error(error).downcast_ref::<NativeArtifactError>(),
            Some(NativeArtifactError::Execution(
                NativeDataIoError::Unavailable
            ))
        ));
    }

    #[test]
    fn turn_start_reply_preserves_original_row_bytes_and_enforces_the_row_limit() {
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let prefix = r#"{"text": "日本語","padding":""#;
        let suffix = r#""}"#;
        let padding = "a".repeat(TURN_READ_REPLY_LIMIT - prefix.len() - suffix.len());
        let row = format!("{prefix}{padding}{suffix}");
        let row = CoreBuffer::copy_from(row.as_bytes(), &budget).unwrap();
        for (claimed, prefix) in [
            (false, b"{\"claimed\":false,\"turn\":".as_slice()),
            (true, b"{\"claimed\":true,\"turn\":".as_slice()),
        ] {
            let reply = encode_turn_start_reply(&row, claimed, &budget).unwrap();
            let reply = reply.as_bytes();
            assert!(reply.len() <= TURN_START_REPLY_LIMIT);
            assert_eq!(
                reply.strip_prefix(prefix).unwrap().strip_suffix(b"}"),
                Some(row.as_bytes()),
            );
        }
        let oversized =
            CoreBuffer::copy_from(&vec![b' '; TURN_READ_REPLY_LIMIT + 1], &budget).unwrap();
        assert!(matches!(
            encode_turn_start_reply(&oversized, false, &budget),
            Err(LegacySavedTurnSettlementError::Capacity)
        ));
    }

    #[test]
    fn model_resolve_preserves_bounded_owner_json_without_a_document() {
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let prefix = r#"{"provider_metadata":{"handle":"資格", "precise":9007199254740993,"number":NaN},"padding":""#;
        let suffix = r#""}"#;
        for extra in [0, 1] {
            let padding =
                "a".repeat(MODEL_RESOLVE_REPLY_LIMIT - prefix.len() - suffix.len() + extra);
            let owner = format!("{prefix}{padding}{suffix}");
            let output = serde_json::json!({"owner_result": owner});
            let output =
                CoreValue::parse_guest(&serde_json::to_vec(&output).unwrap(), &budget).unwrap();
            let result = decode_model_resolve_reply(output, &budget);
            if extra == 0 {
                let reply = result.unwrap();
                assert_eq!(reply.wire.as_bytes(), owner.as_bytes());
                assert!(reply.document.is_none());
            } else {
                let Err(error) = result else {
                    panic!("accepted an oversized owner result");
                };
                assert!(error.is::<ModelRegistryRequiresLegacyRead>());
            }
        }
    }

    #[test]
    fn model_resolve_distinguishes_compatibility_limits_from_io_failures() {
        assert!(model_registry_error(NativeArtifactError::TooLarge)
            .is::<ModelRegistryRequiresLegacyRead>());
        let error = NativeArtifactError::Execution(NativeDataIoError::Unavailable);
        assert!(matches!(
            model_registry_error(error).downcast_ref::<NativeArtifactError>(),
            Some(NativeArtifactError::Execution(
                NativeDataIoError::Unavailable
            ))
        ));
    }

    #[test]
    fn polling_does_not_report_cleanup_for_a_running_worker() {
        let jobs = crate::native_runtime_jobs::Jobs::new(Arc::new(AtomicBool::new(false)));
        let (release, wait) = mpsc::sync_channel(0);
        let join = jobs
            .spawn_joinable(move |_| {
                wait.recv().unwrap();
                WorkerExit {
                    cleanup_verified: true,
                }
            })
            .unwrap();
        assert!(!join.try_join().unwrap());
        release.send(()).unwrap();
        join.join_until(Instant::now() + Duration::from_secs(10))
            .unwrap();
        assert!(join.try_join().unwrap());
    }

    #[test]
    fn cancellation_after_physical_join_still_discards_the_buffered_result() {
        let jobs = crate::native_runtime_jobs::Jobs::new(Arc::new(AtomicBool::new(false)));
        let (send, receive) = mpsc::sync_channel(1);
        let join = jobs
            .spawn_joinable(move |_| {
                assert!(send.send(Err(anyhow!("buffered result"))).is_ok());
                WorkerExit {
                    cleanup_verified: true,
                }
            })
            .unwrap();
        let deadline = Instant::now() + Duration::from_secs(10);
        let pending = PendingProviderPrepare {
            join,
            receive,
            deadline: Arc::new(ProviderPrepareDeadline::new(deadline)),
            continue_current: Arc::new(|| Ok(())),
            cancelled: AtomicBool::new(false),
        };
        pending.join_until(deadline).unwrap();
        pending.cancel().unwrap();
        let error = pending.finish_until(deadline).unwrap_err();
        assert!(matches!(
            error.downcast_ref::<NativeArtifactError>(),
            Some(NativeArtifactError::Execution(NativeDataIoError::Cancelled))
        ));
    }

    #[test]
    fn failed_readiness_keeps_registry_reader_unreached() {
        let mut reader = guarded_registry_read(
            Box::new(|_| panic!("Registry read after readiness was lost")),
            Arc::new(|| anyhow::bail!("readiness lost")),
            CoreCancellation::new(),
            Instant::now() + Duration::from_secs(10),
        );
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        assert_eq!(reader(&budget).unwrap_err().to_string(), "readiness lost");
    }

    #[test]
    fn cancelled_registry_read_does_not_return_a_document() {
        let cancel = CoreCancellation::new();
        let reader_cancel = cancel.clone();
        let reached_check = Arc::new(AtomicBool::new(false));
        let reached_read = Arc::clone(&reached_check);
        let mut reader = guarded_registry_read(
            Box::new(move |budget| {
                reached_read.store(true, Ordering::Relaxed);
                reader_cancel.cancel();
                Ok(Some(CoreBuffer::copy_from(b"{}", budget)?))
            }),
            Arc::new(|| Ok(())),
            cancel,
            Instant::now() + Duration::from_secs(10),
        );
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let error = reader(&budget).unwrap_err();
        assert!(reached_check.load(Ordering::Relaxed));
        assert!(matches!(
            error.downcast_ref::<NativeArtifactError>(),
            Some(NativeArtifactError::Execution(NativeDataIoError::Cancelled))
        ));
    }
}
