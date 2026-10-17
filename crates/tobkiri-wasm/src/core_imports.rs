use anyhow::{Context, Result, bail};
use std::sync::Arc;
use tobkiri_canon::{
    CoreValue,
    ids::{OpName, ShapeRef},
};
use tobkiri_gate::{CallCx, ControlKind, Outcome};
use tobkiri_manifest::{ShapeDef, VerifiedPack, WitFunction};
use wasmtime::StoreContextMut;
use wasmtime::component::{
    Component, Linker, Val,
    types::{ComponentItem, Type},
};

use crate::host::{PackHost, PackState, Shared, interface_name};
use crate::{HandleKind, InvocationState, ProviderFrame};

pub(crate) fn link_core_and_ports(
    linker: &mut Linker<InvocationState<PackState>>,
    shared: &Arc<Shared>,
    pack: &VerifiedPack,
    component: &Component,
    _owners: &crate::owned_values::ValueOwners,
) -> Result<()> {
    for (name, item) in component.component_type().imports(shared.engine.engine()) {
        if name.starts_with("wasi:") {
            continue;
        }
        let ComponentItem::ComponentInstance(import) = item.ty else {
            bail!("non-interface component import")
        };
        let core = name.strip_prefix("tobkiri:core/").and_then(|rest| {
            let (interface, version) = rest.rsplit_once('@')?;
            matches!(version, "1.0.0" | "1.1.0").then_some((interface, version))
        });
        let shape: Option<ShapeDef> = if core.is_none() {
            let port = pack.manifest.ports.consumes.iter().find(|p| {
                interface_name(&tobkiri_canon::ids::ShapeRef::new(
                    p.shape.clone(),
                    p.version,
                )) == name
            });
            if let Some(port) = port {
                Some(
                    shared
                        .views
                        .get(&tobkiri_canon::ids::ShapeRef::new(
                            port.shape.clone(),
                            port.version,
                        ))
                        .context("typed imported shape missing")?
                        .clone(),
                )
            } else {
                if import
                    .exports(shared.engine.engine())
                    .any(|(_, e)| !matches!(e.ty, ComponentItem::Type(_)))
                {
                    bail!("type-only dependency interface imports effects")
                }
                linker.instance(name)?;
                continue;
            }
        } else {
            None
        };
        let mut instance = linker.instance(name)?;
        for (function, item) in import.exports(shared.engine.engine()) {
            match item.ty {
                ComponentItem::ComponentFunc(_) => {
                    let operation = function.to_owned();
                    if let Some(shape) = &shape {
                        let op = OpName::parse(&operation)?;
                        let signature = shape
                            .functions
                            .get(&op)
                            .context("undeclared imported function")?
                            .clone();
                        let shape = shape.reference.clone();
                        instance.func_new_async(function, move |store, _ty, params, results| {
                            let op = op.clone();
                            let shape = shape.clone();
                            let signature = signature.clone();
                            Box::new(async move {
                                let result: Result<()> = async move {
                                    let host = host_from_store(&store)?;
                                    let cx = store.data().cx.clone();
                                    let owners = store.data().data.owners.clone();
                                    let frame = store
                                        .data()
                                        .data
                                        .frame
                                        .as_ref()
                                        .context(
                                            "typed guest call lacks actual provider activation",
                                        )?
                                        .clone();
                                    let admitted = admit_argument_handles(
                                        &host, &cx, params, &signature, &shape, &frame, &owners,
                                    )
                                    .await?;
                                    let values = host
                                        .invoke_values_owned(
                                            &cx,
                                            &shape,
                                            &op,
                                            admitted.as_deref().unwrap_or(params),
                                            &signature,
                                            &owners,
                                            &frame,
                                        )
                                        .await?;
                                    if values.len() != results.len() {
                                        bail!("typed proxy result arity mismatch")
                                    }
                                    results.clone_from_slice(values.as_slice());
                                    Ok(())
                                }
                                .await;
                                result.map_err(wasmtime::Error::from_anyhow)
                            })
                        })?;
                    } else {
                        let (interface, version) = core.context("unexpected core namespace")?;
                        let interface = interface.to_owned();
                        let version = version.to_owned();
                        instance.func_new_async(
                            function,
                            move |mut store, ty, params, results| {
                                let interface = interface.clone();
                                let version = version.clone();
                                let operation = operation.clone();
                                Box::new(async move {
                                    let result: Result<()> = async move {
                                        let output = crate::effects::invoke(
                                            &mut store, &interface, &version, &operation, params,
                                        )
                                        .await;
                                        match output {
                                            Ok(values) => {
                                                if values.len() != results.len() {
                                                    bail!("core import result arity mismatch")
                                                };
                                                results.clone_from_slice(&values);
                                                Ok(())
                                            }
                                            Err(error) => {
                                                if results.len() == 1
                                                    && matches!(
                                                        ty.results().next(),
                                                        Some(Type::Result(_))
                                                    )
                                                {
                                                    results[0] = error_result_or_trap(
                                                        error,
                                                        &store.data().data.owners,
                                                    )?;
                                                    Ok(())
                                                } else {
                                                    Err(error)
                                                }
                                            }
                                        }
                                    }
                                    .await;
                                    result.map_err(wasmtime::Error::from_anyhow)
                                })
                            },
                        )?;
                    }
                }
                ComponentItem::Resource(_) => crate::effects::link_resource(
                    &mut instance,
                    core.context("Pack-defined resources are forbidden")?.0,
                    core.context("Pack-defined resources are forbidden")?.1,
                    function,
                )?,
                ComponentItem::Type(_) => {}
                _ => bail!("unsupported interface export kind"),
            }
        }
    }
    Ok(())
}

pub(crate) fn host_from_store(
    store: &StoreContextMut<'_, InvocationState<PackState>>,
) -> Result<PackHost> {
    Ok(PackHost {
        shared: store
            .data()
            .data
            .shared
            .upgrade()
            .context("component host has shut down")?,
    })
}
/// Closed host failure metadata; never interpolate SQL, parameters or a
/// chained native diagnostic into the guest-visible uncertainty message.
pub(crate) const STORAGE_COMMIT_UNKNOWN_MESSAGE: &str =
    "storage-sql-commit-unknown: operation may have committed; do not replay";
pub(crate) const STORAGE_PENDING_MOVE_MESSAGE: &str =
    "storage-pending-data-move: storage move requires explicit repair; do not replay";

/// Only sealed native effect variants select stable public metadata. Never infer
/// repair/commit status from a guest code, message, path or native diagnostic.
pub(crate) fn storage_failure_metadata(
    error: &anyhow::Error,
) -> Option<(&'static str, &'static str)> {
    if matches!(
        error.downcast_ref::<crate::filesystem::FsError>(),
        Some(crate::filesystem::FsError::CommitUnknown)
    ) {
        return Some((
            "filesystem-commit-unknown",
            "filesystem-commit-unknown: operation may have changed files; do not replay",
        ));
    }
    match error.downcast_ref::<tobkiri_store::StoreError>() {
        Some(tobkiri_store::StoreError::CommitUnknown) => {
            Some(("storage-sql-commit-unknown", STORAGE_COMMIT_UNKNOWN_MESSAGE))
        }
        Some(tobkiri_store::StoreError::PendingDataMove) => {
            Some(("storage-pending-data-move", STORAGE_PENDING_MOVE_MESSAGE))
        }
        _ => None,
    }
}

/// Preserve an existing CoreStream error facade, not native effect authority.
/// Guest result decoding permits only the nine Core enum codes, so an arbitrary
/// guest code/message cannot mint one of these internal StepError labels. Keep
/// this separate from storage_failure_metadata, whose proof is a sealed native
/// variant and is also used at effect/commit boundaries outside stream facades.
fn facaded_no_replay_metadata(error: &anyhow::Error) -> Option<(&'static str, &'static str)> {
    match error
        .downcast_ref::<tobkiri_flow::StepError>()?
        .code
        .as_str()
    {
        "filesystem-commit-unknown" => Some((
            "filesystem-commit-unknown",
            "filesystem-commit-unknown: operation may have changed files; do not replay",
        )),
        "storage-sql-commit-unknown" => {
            Some(("storage-sql-commit-unknown", STORAGE_COMMIT_UNKNOWN_MESSAGE))
        }
        "storage-pending-data-move" => {
            Some(("storage-pending-data-move", STORAGE_PENDING_MOVE_MESSAGE))
        }
        _ => None,
    }
}

/// Terminal/audit failure must not erase an already sealed no-replay result.
/// Both results have already completed; this helper never retries an effect or
/// manufactures an acknowledgement for a failed terminal delivery.
pub(crate) fn finish_effect_delivery<T>(
    cx: &CallCx,
    result: Result<T>,
    delivery: Result<()>,
) -> Result<T> {
    let result = result.map_err(|error| crate::filesystem::preserve_effect_error(cx, error));
    if let Err(error) = delivery {
        if result
            .as_ref()
            .err()
            .is_some_and(|error| storage_failure_metadata(error).is_some())
        {
            return result;
        }
        // Preserve native sealed evidence before facade-only classification.
        // This does not turn a StepError label into a native commit proof.
        if storage_failure_metadata(&error).is_none()
            && result
                .as_ref()
                .err()
                .is_some_and(|error| facaded_no_replay_metadata(error).is_some())
        {
            return result;
        }
        return Err(crate::filesystem::preserve_effect_error(cx, error));
    }
    result
}

/// Failure to allocate an error packet must not replace sealed no-replay
/// state with an ordinary allocation failure. Trap with that original Store
/// unit error; the outer host mapping uses only the fixed safe metadata.
fn error_result_or_trap(
    error: anyhow::Error,
    owners: &crate::owned_values::ValueOwners,
) -> Result<Val> {
    match error_result(&error, owners) {
        Ok(value) => Ok(value),
        Err(_)
            if storage_failure_metadata(&error).is_some()
                || facaded_no_replay_metadata(&error).is_some() =>
        {
            Err(error)
        }
        Err(encoding) => Err(encoding),
    }
}

pub(crate) fn error_result(
    error: &anyhow::Error,
    owners: &crate::owned_values::ValueOwners,
) -> Result<Val> {
    use wasmtime::component::{BudgetedBox, BudgetedString, BudgetedVec};
    let storage_failure = storage_failure_metadata(error);
    let facaded_failure = facaded_no_replay_metadata(error);
    let network_failure = crate::network_diagnostics::failure_metadata(error);
    let code = if storage_failure.is_some() || facaded_failure.is_some() {
        "internal"
    } else if network_failure.is_some() {
        "unavailable"
    } else if let Some(error) = error.downcast_ref::<crate::filesystem::FsError>() {
        use crate::filesystem::FsError;
        match error {
            FsError::Invalid(_) => "invalid",
            FsError::Denied(_) => "denied",
            FsError::Io(error) if error.kind() == std::io::ErrorKind::NotFound => "not-found",
            FsError::Gate(tobkiri_gate::GateError::Cancelled) => "cancelled",
            FsError::Gate(
                tobkiri_gate::GateError::Deadline | tobkiri_gate::GateError::Limit(_),
            ) => "limit-exceeded",
            FsError::Gate(tobkiri_gate::GateError::ApprovalRequired) => "approval-required",
            FsError::Gate(_) => "denied",
            FsError::Allocation(tobkiri_canon::AllocationError::Limit) => "limit-exceeded",
            _ => "unavailable",
        }
    } else {
        match error.downcast_ref::<tobkiri_gate::GateError>() {
            Some(error) => match error {
                tobkiri_gate::GateError::Cancelled => "cancelled",
                tobkiri_gate::GateError::Deadline | tobkiri_gate::GateError::Limit(_) => {
                    "limit-exceeded"
                }
                tobkiri_gate::GateError::ApprovalRequired => "approval-required",
                _ => "denied",
            },
            None => error
                .downcast_ref::<tobkiri_flow::StepError>()
                .map(|error| error.audit_code())
                .unwrap_or("unavailable"),
        }
    };
    let message = storage_failure
        .or(facaded_failure)
        .or(network_failure)
        .map_or("core operation failed", |(_, message)| message);
    let text = |value: &str| BudgetedString::copy_from(value, owners.component(), 0);
    let fields = BudgetedVec::from_iter(
        [
            (text("code")?, Val::Enum(text(code)?)),
            (text("message")?, Val::String(text(message)?)),
        ]
        .into_iter(),
        owners.component(),
        0,
    )?;
    Ok(Val::Result(Err(Some(BudgetedBox::new(
        Val::Record(fields),
        owners.component(),
        0,
    )?))))
}

async fn finish_handle(
    host: &PackHost,
    entry: &crate::HandleEntry,
    outcome: Outcome,
) -> Result<()> {
    let result = async {
        entry.lease.begin_close();
        let mut receipt = entry.lease.close.lock().await;
        if entry
            .lease
            .permit
            .as_ref()
            .is_some_and(|permit| std::time::Instant::now() >= permit.deadline())
        {
            receipt.deadline_settlement = true;
        }
        if receipt.outcome.is_none() {
            receipt.outcome = Some(outcome);
        }
        if !receipt.delivered {
            if let Some(id) = &entry.lease.grant {
                if receipt.deadline_settlement {
                    let permit = entry
                        .lease
                        .permit
                        .as_ref()
                        .context("deadline proof missing")?;
                    let terminal = host.shared.gate.settle_deadline(permit).await?;
                    receipt.outcome = Some(terminal.decision.outcome);
                } else if let Err(error) = host
                    .shared
                    .gate
                    .record_outcome(
                        id.clone(),
                        receipt
                            .outcome
                            .clone()
                            .context("terminal proposal missing")?,
                    )
                    .await
                {
                    if matches!(error, tobkiri_gate::GateError::Deadline) {
                        // Durable wall-time expiry may win between the Instant
                        // check and CAS. Deliver that same winner without changing
                        // its disposition or replaying physical cleanup.
                        receipt.deadline_settlement = true;
                        let permit = entry
                            .lease
                            .permit
                            .as_ref()
                            .context("deadline proof missing")?;
                        let terminal = host.shared.gate.settle_deadline(permit).await?;
                        receipt.outcome = Some(terminal.decision.outcome);
                    } else {
                        return Err(error.into());
                    }
                }
            }
            if let (Some(frame), Some(permit)) = (&entry.source_frame, &entry.lease.permit) {
                let acknowledged = host.shared.gate.acknowledged_terminal(permit)?;
                frame
                    .terminal_acknowledged(&acknowledged)
                    .map_err(anyhow::Error::new)?;
            }
            receipt.delivered = true;
        }
        Ok(())
    }
    .await;
    result.map_err(|error| crate::filesystem::preserve_effect_error(&entry.owner, error))
}

async fn release_stream_once(host: &PackHost, entry: &crate::HandleEntry) -> Result<()> {
    entry.lease.begin_close();
    let mut receipt = entry.lease.close.lock().await;
    if !receipt.cleanup_started {
        receipt.cleanup_started = true;
        let result = release_stream(host, entry).await;
        record_stream_cleanup(&mut receipt, &entry.owner, result);
    }
    stream_cleanup_result(&receipt, &entry.owner)
}

fn record_stream_cleanup(
    receipt: &mut crate::handles::HandleCloseReceipt,
    cx: &CallCx,
    result: Result<()>,
) {
    let result = result.map_err(|error| crate::filesystem::preserve_effect_error(cx, error));
    receipt.cleanup_done = result.is_ok();
    receipt.cleanup_error = result.err().map(|error| {
        use crate::handles::HandleCleanupError;
        match storage_failure_metadata(&error).map(|metadata| metadata.0) {
            Some("filesystem-commit-unknown") => {
                return HandleCleanupError::FilesystemCommitUnknown;
            }
            Some("storage-sql-commit-unknown") => return HandleCleanupError::StorageCommitUnknown,
            Some("storage-pending-data-move") => return HandleCleanupError::StoragePendingDataMove,
            _ => (),
        }
        match facaded_no_replay_metadata(&error).map(|metadata| metadata.0) {
            Some("filesystem-commit-unknown") => HandleCleanupError::FacadedFilesystemCommitUnknown,
            Some("storage-sql-commit-unknown") => HandleCleanupError::FacadedStorageCommitUnknown,
            Some("storage-pending-data-move") => HandleCleanupError::FacadedStoragePendingDataMove,
            _ => HandleCleanupError::CompletionUnknown,
        }
    });
}

fn stream_cleanup_result(receipt: &crate::handles::HandleCloseReceipt, cx: &CallCx) -> Result<()> {
    if cx.filesystem_mutation_possible() {
        return Err(crate::filesystem::FsError::CommitUnknown.into());
    }
    if receipt.cleanup_done {
        return Ok(());
    }
    let error = match receipt.cleanup_error {
        Some(crate::handles::HandleCleanupError::FilesystemCommitUnknown) => {
            crate::filesystem::FsError::CommitUnknown.into()
        }
        Some(crate::handles::HandleCleanupError::StorageCommitUnknown) => {
            tobkiri_store::StoreError::CommitUnknown.into()
        }
        Some(crate::handles::HandleCleanupError::StoragePendingDataMove) => {
            tobkiri_store::StoreError::PendingDataMove.into()
        }
        Some(crate::handles::HandleCleanupError::FacadedFilesystemCommitUnknown) => {
            tobkiri_flow::StepError::new(
                "filesystem-commit-unknown",
                "filesystem-commit-unknown: operation may have changed files; do not replay",
            )
            .into()
        }
        Some(crate::handles::HandleCleanupError::FacadedStorageCommitUnknown) => {
            tobkiri_flow::StepError::new(
                "storage-sql-commit-unknown",
                STORAGE_COMMIT_UNKNOWN_MESSAGE,
            )
            .into()
        }
        Some(crate::handles::HandleCleanupError::FacadedStoragePendingDataMove) => {
            tobkiri_flow::StepError::new("storage-pending-data-move", STORAGE_PENDING_MOVE_MESSAGE)
                .into()
        }
        Some(crate::handles::HandleCleanupError::CompletionUnknown) => {
            anyhow::anyhow!("stream cleanup completion unknown")
        }
        None => anyhow::anyhow!("stream cleanup attempt was interrupted"),
    };
    Err(crate::filesystem::preserve_effect_error(cx, error))
}

pub(crate) async fn finish_stream_eof(
    host: &PackHost,
    cx: &CallCx,
    id: u64,
    entry: &crate::HandleEntry,
) -> Result<()> {
    let result = async {
        release_stream_once(host, entry).await?;
        finish_handle(host, entry, Outcome::Succeeded).await?;
        if entry.lease.close.lock().await.outcome.as_ref() != Some(&Outcome::Succeeded)
            && entry.lease.grant.is_some()
        {
            bail!("stream terminal did not acknowledge success")
        }
        host.shared
            .handles
            .lock()
            .map_err(|_| anyhow::anyhow!("handle table poisoned"))?
            .remove_captured(id, cx, entry)?;
        Ok(())
    }
    .await;
    result.map_err(|error| crate::filesystem::preserve_effect_error(&entry.owner, error))
}

fn cancellation_outcome(entry: &crate::HandleEntry, reason: ExpiryReason, proven: bool) -> Outcome {
    if !proven && entry.owner.filesystem_mutation_possible() {
        return Outcome::Failed {
            code: "filesystem-commit-unknown".into(),
        };
    }
    if matches!(reason, ExpiryReason::Deadline)
        || std::time::Instant::now() >= entry.owner.deadline()
    {
        Outcome::Failed {
            code: "deadline-failure-unknown".into(),
        }
    } else if matches!(reason, ExpiryReason::UserCancel) && proven {
        Outcome::Cancelled
    } else {
        Outcome::Failed {
            code: "cleanup-unknown".into(),
        }
    }
}
#[derive(Clone, Copy)]
pub(crate) enum ExpiryReason {
    Deadline,
    Revoked,
    UserCancel,
}

pub(crate) async fn pull_stream(
    host: &PackHost,
    cx: &CallCx,
    id: u64,
    max: u32,
    owners: &crate::owned_values::ValueOwners,
) -> Result<CoreValue> {
    let result = async {
    if max == 0 || max > 1024 {
        bail!("stream pull bound must be 1..1024")
    }
    if let Err(error) = host.shared.gate.check_context(cx) {
        expire_handle(host, cx, id, HandleKind::Stream, ExpiryReason::Revoked).await?;
        return Err(error.into());
    }
    let entry = host
        .shared
        .handles
        .lock()
        .map_err(|_| anyhow::anyhow!("handle table poisoned"))?
        .resolve(id, HandleKind::Stream, cx)?;
    if let Err(error) = host.shared.gate.check_context(&entry.owner) {
        expire_handle(host, cx, id, HandleKind::Stream, ExpiryReason::Revoked).await?;
        return Err(error.into());
    }
    if let Err(error) = host.check_handle_generation(&entry) {
        close_retired_handle(host, cx, id, HandleKind::Stream).await?;
        return Err(error);
    }
    let result = async {
        let events = if let Some(source) = &entry.source {
            let mut source = tokio::select! {
                _=cx.cancel().cancelled()=>bail!("stream pull cancelled"),
                _=tokio::time::sleep_until(cx.deadline().into())=>bail!("stream pull deadline elapsed"),
                source=source.lock()=>source,
            };
            source.pull(max).await.map_err(anyhow::Error::new)?
        } else {
            pull_guest_source(host, &entry, max, false).await?.0
        };
        let count = events.array().context("stream source must return an array")?.len();
        if count > max as usize { bail!("provider stream exceeded batch bound") }
        let encoded = events.encode_guest(owners.graph())?;
        if entry.source.is_some() { entry.lease.reserve_output(encoded.len() as u64, entry.owner.limits().output_bytes)?; }
        Ok(events)
    }.await.map_err(|error| crate::filesystem::preserve_effect_error(cx, error));
    let delivery = async {
    match &result {
        Ok(events) if events.array().is_some_and(|events| events.is_empty()) => {
            release_stream_once(host, &entry).await?;
            finish_handle(host, &entry, Outcome::Succeeded).await?;
            host.shared
                .handles
                .lock()
                .map_err(|_| anyhow::anyhow!("handle table poisoned"))?
                .remove(id, HandleKind::Stream, cx)?;
        }
        Err(error) => {
            // Release the provider on a malformed batch or trap, then record
            // exactly one final outcome for the original StreamOpen grant.
            let cleanup = release_stream_once(host, &entry).await;
            let delivered = finish_handle(
                host,
                &entry,
                if let Some((code, _)) = storage_failure_metadata(error)
                    .or_else(|| facaded_no_replay_metadata(error)) {
                    Outcome::Failed { code: code.into() }
                } else if std::time::Instant::now() >= entry.owner.deadline() {
                    cancellation_outcome(&entry, ExpiryReason::Deadline, false)
                } else if cx.cancel().is_cancelled() {
                    cancellation_outcome(&entry, ExpiryReason::Revoked, cleanup.is_ok())
                } else {
                    Outcome::Failed {
                        code: "stream-pull".into(),
                    }
                },
            )
            .await;
            let cleaned = cleanup.is_ok();
            // Join both outcomes before returning either. A terminal write
            // failure cannot hide a stronger cleanup/no-replay uncertainty.
            finish_stream_cleanup(cleanup, delivered)?;
            if cleaned {
                host.shared
                    .handles
                    .lock()
                    .map_err(|_| anyhow::anyhow!("handle table poisoned"))?
                    .remove(id, HandleKind::Stream, cx)?;
            }
        }
        _ => {}
    }
    Ok(())
    }.await;
    finish_effect_delivery(cx, result, delivery)
    }.await;
    result.map_err(|error| crate::filesystem::preserve_effect_error(cx, error))
}

/// A source-open frame remains live until actual cleanup. No per-pull grant is
/// minted and each batch is converted/encoded before publishing it to callers.
pub(crate) async fn pull_guest_source(
    host: &PackHost,
    entry: &crate::HandleEntry,
    max: u32,
    final_output: bool,
) -> Result<(CoreValue, Option<tobkiri_canon::CoreBuffer>)> {
    let result = async {
        if max == 0 || max > 1024 {
            bail!("stream pull bound must be 1..1024")
        }
        host.shared.gate.check_context(&entry.owner)?;
        host.check_handle_generation(entry)?;
        let frame = entry
            .source_frame
            .as_ref()
            .context("guest source activation missing")?;
        let cx = frame.context();
        host.shared.gate.check_context(cx)?;
        let owners = host.allocation_owners_for_frame(
            cx,
            frame
                .allocation_scope(tobkiri_gate::AllocationReason::StreamBatch)
                .map_err(anyhow::Error::new)?,
        )?;
        let (pack, _) = cx
            .principal()
            .pack()
            .context("source owner is not a Pack")?;
        let version = entry.source_version.context("source package missing")?;
        let interface = format!("tobkiri:core/stream-source@{version}");
        let (values, _) = host
            .export_call_owned(
                pack,
                cx,
                &interface,
                "pull",
                &[Val::U64(entry.local_id), Val::U32(max)],
                entry.generation,
                &owners,
                frame.clone(),
                frame
                    .allocation_scope(tobkiri_gate::AllocationReason::RuntimeValue)
                    .map_err(anyhow::Error::new)?,
            )
            .await?;
        let values = decode_guest_stream_events(version, values.as_slice())?;
        if values.len() > max as usize {
            bail!("guest stream exceeded its bounded batch")
        }
        let events = CoreValue::array_from(
            values.iter().map(|value| {
                let Val::String(value) = value else {
                    return Err(tobkiri_canon::AllocationError::Invalid);
                };
                CoreValue::parse_guest(value.as_str().as_bytes(), owners.graph())
            }),
            owners.graph(),
        )?;
        // Size/final encoding happens inside this still-live source activation.
        let output = host.allocation_owners_for_frame(
            cx,
            frame
                .allocation_scope(tobkiri_gate::AllocationReason::OutputBuffer)
                .map_err(anyhow::Error::new)?,
        )?;
        let encoded = events.encode_guest(output.graph())?;
        entry
            .lease
            .reserve_output(encoded.len() as u64, entry.owner.limits().output_bytes)?;
        Ok((events, final_output.then_some(encoded)))
    }
    .await;
    result.map_err(|error| crate::filesystem::preserve_effect_error(&entry.owner, error))
}

/// Only called for the exact Core stream-source export recorded during Pack
/// admission, after verified WIT and the closed Core ABI have both passed.
/// Arbitrary domain Result records never enter this decoder. A returned typed
/// error is distinct from an export trap, malformed value or conversion error.
fn decode_guest_stream_events<'a>(version: &str, results: &'a [Val]) -> Result<&'a [Val]> {
    let invalid_result = || {
        anyhow::Error::new(tobkiri_flow::StepError::new(
            "internal",
            "guest stream source returned an invalid result",
        ))
    };
    if !matches!(version, "1.0.0" | "1.1.0") {
        return Err(invalid_result());
    }
    match results {
        [Val::Result(Ok(Some(value)))] => match &**value {
            Val::List(events) => Ok(events.as_slice()),
            _ => Err(invalid_result()),
        },
        [Val::Result(Err(Some(value)))] => {
            let Val::Record(fields) = &**value else {
                return Err(invalid_result());
            };
            let [(code_name, Val::Enum(code)), (message_name, Val::String(_))] = fields.as_slice()
            else {
                return Err(invalid_result());
            };
            if code_name.as_str() != "code" || message_name.as_str() != "message" {
                return Err(invalid_result());
            }
            // Inspect only the validated enum. Do not clone/format the message.
            Err(crate::dispatch::public_core_error_code(code.as_str())
                .map(anyhow::Error::new)
                .unwrap_or_else(invalid_result))
        }
        _ => Err(invalid_result()),
    }
}

fn confirm_stream_cleanup(cleanup: Result<()>) -> Result<()> {
    cleanup.map_err(|error| {
        if storage_failure_metadata(&error).is_some()
            || facaded_no_replay_metadata(&error).is_some()
        {
            error
        } else {
            tobkiri_flow::StepError::new("internal", "stream cleanup completion unknown").into()
        }
    })
}

fn finish_stream_cleanup(cleanup: Result<()>, delivery: Result<()>) -> Result<()> {
    match (cleanup, delivery) {
        (Err(error), _) if storage_failure_metadata(&error).is_some() => Err(error),
        (_, Err(error)) if storage_failure_metadata(&error).is_some() => Err(error),
        (Err(error), _) if facaded_no_replay_metadata(&error).is_some() => Err(error),
        (_, Err(error)) if facaded_no_replay_metadata(&error).is_some() => Err(error),
        (Err(error), _) => confirm_stream_cleanup(Err(error)),
        (Ok(()), delivery) => delivery,
    }
}

/// These StepErrors have already crossed the host's closed native/typed error
/// facade. Preserve existing no-replay metadata; never derive it from a guest
/// message or classify an unsuccessful cleanup as ordinary unavailability.
pub(crate) fn stream_error_after_cleanup(
    error: tobkiri_flow::StepError,
    cleanup: Result<(), tobkiri_flow::StepError>,
) -> tobkiri_flow::StepError {
    let no_replay = |error: &tobkiri_flow::StepError| {
        matches!(
            error.code.as_str(),
            "filesystem-commit-unknown"
                | "storage-sql-commit-unknown"
                | "storage-pending-data-move"
        )
    };
    match cleanup {
        Ok(()) => error,
        Err(_) if no_replay(&error) => error,
        Err(cleanup) if no_replay(&cleanup) => cleanup,
        Err(_) => tobkiri_flow::StepError::new("internal", "stream cleanup completion unknown"),
    }
}

async fn release_stream(host: &PackHost, entry: &crate::HandleEntry) -> Result<()> {
    let result = async {
        if let Some(source) = &entry.source {
            match tokio::time::timeout(std::time::Duration::from_secs(2), async {
                source.lock().await.close().await
            })
            .await
            {
                Ok(result) => result.map_err(anyhow::Error::new)?,
                Err(_) => {
                    // A pull holds this mutex while awaiting the run. Revoke its
                    // original request after the grace bound so cleanup cannot wait
                    // indefinitely for its own producer.
                    entry.owner.cancel().cancel();
                    bail!("core stream cleanup exceeded its cancellation grace")
                }
            }
            return Ok(());
        }
        host.check_handle_generation(entry)?;
        let (owner, _) = entry
            .owner
            .principal()
            .pack()
            .context("stream owner is not a Pack")?;
        let mut cancel_result = Err(anyhow::anyhow!("guest stream cancellation was not entered"));
        if host.shared.gate.check_context(&entry.owner).is_ok() {
            if let (Some(frame), Some(version)) = (&entry.source_frame, entry.source_version) {
                let call = async {
                    let cx = frame.context();
                    let owners = host.allocation_owners_for_frame(
                        cx,
                        frame
                            .allocation_scope(tobkiri_gate::AllocationReason::RuntimeValue)
                            .map_err(anyhow::Error::new)?,
                    )?;
                    host.export_call_owned(
                        owner,
                        cx,
                        &format!("tobkiri:core/stream-source@{version}"),
                        "cancel",
                        &[Val::U64(entry.local_id)],
                        entry.generation,
                        &owners,
                        frame.clone(),
                        frame
                            .allocation_scope(tobkiri_gate::AllocationReason::RuntimeValue)
                            .map_err(anyhow::Error::new)?,
                    )
                    .await
                };
                cancel_result =
                    match tokio::time::timeout(std::time::Duration::from_secs(2), call).await {
                        Ok(Ok((values, _))) if void_source_cancel_ack(values.as_slice()) => Ok(()),
                        Ok(Ok(_)) => Err(anyhow::anyhow!(
                            "guest stream cancel acknowledgement invalid"
                        )),
                        Ok(Err(error)) => Err(error),
                        Err(_) => Err(anyhow::anyhow!("guest stream cancellation grace elapsed")),
                    };
            }
        }
        // The frozen source cancel operation returns void. Its ACK is not proof
        // that an untrusted guest dropped every native response/resource it owns.
        // Until per-stream native resource custody exists, ALWAYS await destruction
        // of this Store, including after a valid ACK. Revoked/expired contexts never
        // execute fresh guest cleanup. Native tasks retain their own actual leases.
        let retirement = host
            .discard_instance(owner)
            .await
            .context(if cancel_result.is_ok() {
                "physical stream cleanup incomplete after void cancel acknowledgement"
            } else {
                "physical stream cleanup incomplete without cancel acknowledgement"
            });
        guest_stream_cleanup_result(&entry.owner, cancel_result, retirement)
    }
    .await;
    unjoined_cleanup_result(&entry.owner, result)
}

fn unjoined_cleanup_result(cx: &CallCx, result: Result<()>) -> Result<()> {
    // The sticky bit intentionally includes already completed imports. Until
    // this boundary owns a native-work census and join receipt, neither a
    // void ACK nor Store destruction proves physical filesystem cleanup.
    // Conservatively retain uncertainty even if every actual worker finished.
    if cx.filesystem_mutation_possible() {
        Err(crate::filesystem::FsError::CommitUnknown.into())
    } else {
        result
    }
}

fn guest_stream_cleanup_result(
    cx: &CallCx,
    cancellation: Result<()>,
    retirement: Result<()>,
) -> Result<()> {
    let cancellation =
        cancellation.map_err(|error| crate::filesystem::preserve_effect_error(cx, error));
    // Destruction is still attempted and awaited even after failed guest
    // cleanup. It only proves Store release, not completion of detached FS
    // workers. An uncertain mutation therefore takes precedence over either
    // a successful retirement or a second retirement error.
    if let Err(error) = cancellation {
        if matches!(
            error.downcast_ref::<crate::filesystem::FsError>(),
            Some(crate::filesystem::FsError::CommitUnknown)
        ) {
            return Err(error);
        }
    }
    unjoined_cleanup_result(cx, retirement)
}
fn void_source_cancel_ack(values: &[Val]) -> bool {
    values.is_empty()
}
pub(crate) async fn cancel_stream(host: &PackHost, cx: &CallCx, id: u64) -> Result<()> {
    cancel_stream_reason(host, cx, id, ExpiryReason::UserCancel).await
}
async fn cancel_stream_reason(
    host: &PackHost,
    cx: &CallCx,
    id: u64,
    reason: ExpiryReason,
) -> Result<()> {
    let result = async {
        let entry = host
            .shared
            .handles
            .lock()
            .map_err(|_| anyhow::anyhow!("handle table poisoned"))?
            .resolve_cleanup(id, HandleKind::Stream, cx)?;
        cancel_captured_stream_reason(host, cx, id, &entry, reason).await
    }
    .await;
    result.map_err(|error| crate::filesystem::preserve_effect_error(cx, error))
}
pub(crate) async fn cancel_captured_stream(
    host: &PackHost,
    cx: &CallCx,
    id: u64,
    entry: &crate::HandleEntry,
) -> Result<()> {
    cancel_captured_stream_reason(host, cx, id, entry, ExpiryReason::UserCancel).await
}
async fn cancel_captured_stream_reason(
    host: &PackHost,
    cx: &CallCx,
    id: u64,
    entry: &crate::HandleEntry,
    reason: ExpiryReason,
) -> Result<()> {
    let result = async {
        if entry.kind != HandleKind::Stream || !entry.recipient_matches(cx) {
            bail!("captured stream outside the recipient scope")
        }
        {
            let receipt = entry.lease.close.lock().await;
            if receipt.cleanup_done && receipt.delivered {
                return stream_cleanup_result(&receipt, &entry.owner);
            }
        }
        let result = release_stream_once(host, &entry).await;
        let delivery = async {
            finish_handle(
                host,
                &entry,
                cancellation_outcome(&entry, reason, result.is_ok()),
            )
            .await?;
            if result.is_ok() {
                host.shared
                    .handles
                    .lock()
                    .map_err(|_| anyhow::anyhow!("handle table poisoned"))?
                    .remove_captured(id, cx, entry)?;
            }
            Ok(())
        }
        .await;
        finish_effect_delivery(&entry.owner, result, delivery)
    }
    .await;
    result.map_err(|error| crate::filesystem::preserve_effect_error(&entry.owner, error))
}
pub(crate) async fn expire_handle(
    host: &PackHost,
    cx: &CallCx,
    id: u64,
    kind: HandleKind,
    reason: ExpiryReason,
) -> Result<()> {
    let result = async {
        if kind == HandleKind::Stream {
            return cancel_stream_reason(host, cx, id, reason).await;
        }
        let entry = host
            .shared
            .handles
            .lock()
            .map_err(|_| anyhow::anyhow!("handle table poisoned"))?
            .resolve_cleanup(id, kind, cx)?;
        entry.lease.begin_close();
        let cleanup = match entry.owner.principal().pack() {
            Some((pack, _)) => host.discard_instance(pack).await,
            None => Err(anyhow::anyhow!("callback physical owner missing")),
        };
        // Callback retirement has no guest completion acknowledgement. Store
        // destruction alone cannot prove that dispatched native FS work stopped.
        let cleanup = guest_stream_cleanup_result(
            &entry.owner,
            Err(anyhow::anyhow!(
                "callback native work completion unacknowledged"
            )),
            cleanup,
        );
        finish_handle(
            host,
            &entry,
            cancellation_outcome(&entry, reason, cleanup.is_ok()),
        )
        .await?;
        if cleanup.is_ok() {
            host.shared
                .handles
                .lock()
                .map_err(|_| anyhow::anyhow!("handle table poisoned"))?
                .remove(id, kind, cx)?;
        }
        cleanup
    }
    .await;
    result.map_err(|error| crate::filesystem::preserve_effect_error(cx, error))
}
async fn close_retired_handle(
    host: &PackHost,
    cx: &CallCx,
    id: u64,
    kind: HandleKind,
) -> Result<()> {
    let result = async {
        let entry = host
            .shared
            .handles
            .lock()
            .map_err(|_| anyhow::anyhow!("handle table poisoned"))?
            .resolve_cleanup(id, kind, cx)?;
        finish_handle(
            host,
            &entry,
            Outcome::Failed {
                code: "source-retired".into(),
            },
        )
        .await?;
        // A retired generation is no proof that its asynchronous Drop completed.
        // Keep the closed lease quarantined rather than advertising slot reuse.
        bail!("retired component cleanup completion unknown")
    }
    .await;
    result.map_err(|error| crate::filesystem::preserve_effect_error(cx, error))
}

/// Imported port arguments carry the caller's scoped handles as opaque record
/// IDs. A handle the caller already holds forwards unchanged; a caller-local
/// ID opens its actual source lease once under this caller context and its
/// live provider frame, preserving owner provenance so final argument
/// transfer can resolve and alias it for the callee.
async fn admit_argument_handles(
    host: &PackHost,
    cx: &CallCx,
    params: &[Val],
    signature: &WitFunction,
    authority: &ShapeRef,
    frame: &Arc<dyn ProviderFrame>,
    owners: &crate::owned_values::ValueOwners,
) -> Result<Option<Vec<Val>>> {
    if !signature
        .params
        .iter()
        .any(|(_, ty)| crate::dispatch::has_handle(ty))
    {
        return Ok(None);
    }
    const MAX: usize = 1024;
    let _control = host
        .shared
        .control
        .reserve(ControlKind::Frame, (MAX * 32) as u64)?;
    let mut handles = Vec::new();
    handles.try_reserve(MAX)?;
    for ((_, ty), value) in signature.params.iter().zip(params) {
        let probe =
            crate::owned_values::rewrite_handles_owned(value, ty, owners, &mut |kind, id| {
                if !handles.contains(&(kind, id)) {
                    if handles.len() == MAX {
                        bail!("argument handle count exceeded")
                    }
                    handles.push((kind, id));
                }
                Ok(id)
            })?;
        drop(probe);
    }
    if handles.is_empty() {
        return Ok(None);
    }
    let mut mapped = Vec::new();
    mapped.try_reserve(handles.len())?;
    for (kind, id) in handles {
        // Forwarding an already scoped core handle preserves its original
        // source frame and one lease. A caller-local ID opens a new lease
        // once; argument transfer then resolves and aliases it as usual.
        let existing = {
            host.shared
                .handles
                .lock()
                .map_err(|_| anyhow::anyhow!("handle table poisoned"))?
                .resolve(id, kind, cx)
                .ok()
        };
        let mapped_id = if let Some(entry) = existing {
            host.shared.gate.check_context(&entry.owner)?;
            host.check_handle_generation(&entry)?;
            id
        } else {
            host.register_guest_handle(kind, cx, id, cx, authority, None, frame)
                .await?
        };
        mapped.push((kind, id, mapped_id));
    }
    signature
        .params
        .iter()
        .zip(params)
        .map(|((_, ty), value)| {
            crate::owned_values::rewrite_handles_owned(value, ty, owners, &mut |kind, id| {
                mapped
                    .iter()
                    .find(|entry| entry.0 == kind && entry.1 == id)
                    .map(|entry| entry.2)
                    .context("argument handle mapping missing")
            })
        })
        .collect::<Result<Vec<_>>>()
        .map(Some)
}

pub(crate) async fn invoke_callback(
    host: &PackHost,
    cx: &CallCx,
    id: u64,
    args: CoreValue,
    _owners: &crate::owned_values::ValueOwners,
) -> Result<CoreValue> {
    let result = async {
        host.shared.gate.check_context(cx)?;
        let entry = host
            .shared
            .handles
            .lock()
            .map_err(|_| anyhow::anyhow!("handle table poisoned"))?
            .resolve(id, HandleKind::Callback, cx)?;
        host.shared.gate.check_context(&entry.owner)?;
        host.check_handle_generation(&entry)?;
        let frame = entry
            .source_frame
            .as_ref()
            .context("callback source activation missing")?;
        let source_cx = frame.context();
        host.shared.gate.check_context(source_cx)?;
        let owners = host.allocation_owners_for_frame(
            source_cx,
            frame
                .allocation_scope(tobkiri_gate::AllocationReason::RuntimeValue)
                .map_err(anyhow::Error::new)?,
        )?;
        let input = args.encode_guest(owners.graph())?;
        let input = Val::String(wasmtime::component::BudgetedString::copy_from(
            input.as_str()?,
            owners.component(),
            0,
        )?);
        let (pack, _) = source_cx
            .principal()
            .pack()
            .context("callback source owner missing")?;
        let interface = format!(
            "tobkiri:core/callback-sink@{}",
            entry
                .source_version
                .context("callback source package missing")?
        );
        let (values, _) = host
            .export_call_owned(
                pack,
                source_cx,
                &interface,
                "invoke",
                &[Val::U64(entry.local_id), input],
                entry.generation,
                &owners,
                frame.clone(),
                frame
                    .allocation_scope(tobkiri_gate::AllocationReason::RuntimeValue)
                    .map_err(anyhow::Error::new)?,
            )
            .await?;
        let [Val::Result(Ok(Some(value)))] = values.as_slice() else {
            bail!("callback returned error or invalid result")
        };
        let Val::String(value) = &**value else {
            bail!("callback result is not JSON")
        };
        let value = CoreValue::parse_guest(value.as_str().as_bytes(), owners.graph())?;
        let encoded = value.encode_guest(owners.graph())?;
        entry
            .lease
            .reserve_output(encoded.len() as u64, entry.owner.limits().output_bytes)?;
        Ok(value)
    }
    .await;
    result.map_err(|error| crate::filesystem::preserve_effect_error(cx, error))
}

#[cfg(test)]
mod error_tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};
    use tobkiri_canon::{AllocationBudget, AllocationError, AllocationRequest};
    use wasmtime::component::{
        BudgetedBox, BudgetedString, BudgetedVec, HostAllocationBudget, HostAllocationError,
        HostAllocationRequest,
    };

    fn source_error_value(code: &str, owners: &crate::AllocationOwners) -> Val {
        let text = |value: &str| BudgetedString::copy_from(value, owners.component(), 0).unwrap();
        let fields = BudgetedVec::from_iter(
            [
                (text("code"), Val::Enum(text(code))),
                (
                    text("message"),
                    Val::String(text("PRIVATE GUEST BODY MUST NOT ESCAPE")),
                ),
            ]
            .into_iter(),
            owners.component(),
            0,
        )
        .unwrap();
        Val::Result(Err(Some(
            BudgetedBox::new(Val::Record(fields), owners.component(), 0).unwrap(),
        )))
    }

    fn packet_code(packet: &Val) -> &str {
        let Val::Result(Err(Some(record))) = packet else {
            panic!("a typed Core error packet is required")
        };
        let Val::Enum(code) = crate::effect_values::field(record, "code").unwrap() else {
            panic!("a closed Core error enum is required")
        };
        code.as_str()
    }

    #[test]
    fn guest_stream_source_preserves_closed_core_codes_and_masks_guest_messages() {
        let meter = Arc::new(Meter(AtomicU64::new(0), 64 * 1024));
        let owners = crate::AllocationOwners::from_budgets(meter.clone(), meter.clone());
        for version in ["1.0.0", "1.1.0"] {
            for code in [
                "denied",
                "approval-required",
                "cancelled",
                "not-found",
                "invalid",
                "limit-exceeded",
                "conflict",
                "unavailable",
                "internal",
            ] {
                let value = source_error_value(code, &owners);
                let error =
                    decode_guest_stream_events(version, std::slice::from_ref(&value)).unwrap_err();
                assert!(storage_failure_metadata(&error).is_none());
                let error = crate::host::step_error(error);
                assert_eq!(error.code, code);
                assert!(!error.message.contains("PRIVATE"));
                let packet = error_result(&error.into(), &owners).unwrap();
                assert_eq!(packet_code(&packet), code);
                let Val::Result(Err(Some(record))) = &packet else {
                    unreachable!()
                };
                let message = crate::effect_values::text(
                    crate::effect_values::field(record, "message").unwrap(),
                )
                .unwrap();
                assert!(!message.contains("PRIVATE"));
            }
        }
        assert_eq!(meter.0.load(Ordering::Acquire), 0);
    }

    #[test]
    fn guest_stream_source_rejects_wrong_version_unknown_code_and_malformed_results() {
        let meter = Arc::new(Meter(AtomicU64::new(0), 64 * 1024));
        let owners = crate::AllocationOwners::from_budgets(meter.clone(), meter.clone());
        for (version, value) in [
            ("1.0.1", source_error_value("unavailable", &owners)),
            ("2.0.0", source_error_value("unavailable", &owners)),
            ("1.0.0", source_error_value("guest-private-code", &owners)),
            (
                "1.0.0",
                source_error_value("filesystem-commit-unknown", &owners),
            ),
            (
                "1.0.0",
                source_error_value("storage-sql-commit-unknown", &owners),
            ),
            (
                "1.0.0",
                source_error_value("storage-pending-data-move", &owners),
            ),
            ("1.0.0", Val::Result(Err(None))),
            ("1.0.0", Val::Result(Ok(None))),
            ("1.0.0", Val::Bool(false)),
            (
                "1.0.0",
                Val::Result(Err(Some(
                    BudgetedBox::new(Val::Bool(false), owners.component(), 0).unwrap(),
                ))),
            ),
            (
                "1.0.0",
                Val::Result(Ok(Some(
                    BudgetedBox::new(Val::U64(7), owners.component(), 0).unwrap(),
                ))),
            ),
        ] {
            let error =
                decode_guest_stream_events(version, std::slice::from_ref(&value)).unwrap_err();
            assert!(storage_failure_metadata(&error).is_none());
            let error = crate::host::step_error(error);
            assert_eq!(error.code, "internal");
            assert!(!error.message.contains("PRIVATE"));
            assert_eq!(
                packet_code(&error_result(&error.into(), &owners).unwrap()),
                "internal",
            );
        }
        assert!(decode_guest_stream_events("1.0.0", &[]).is_err());
        assert!(
            decode_guest_stream_events("1.0.0", &[Val::Bool(false), Val::Bool(false)]).is_err()
        );
        assert_eq!(meter.0.load(Ordering::Acquire), 0);
    }

    #[test]
    fn guest_stream_source_requires_exact_error_fields_and_keeps_successful_eof() {
        let meter = Arc::new(Meter(AtomicU64::new(0), 64 * 1024));
        let owners = crate::AllocationOwners::from_budgets(meter.clone(), meter.clone());
        let text = |value: &str| BudgetedString::copy_from(value, owners.component(), 0).unwrap();
        for fields in [
            vec![(text("code"), Val::Enum(text("unavailable")))],
            vec![
                (text("code"), Val::String(text("unavailable"))),
                (text("message"), Val::String(text("PRIVATE"))),
            ],
            vec![
                (text("message"), Val::String(text("PRIVATE"))),
                (text("code"), Val::Enum(text("unavailable"))),
            ],
            vec![
                (text("code"), Val::Enum(text("unavailable"))),
                (text("message"), Val::Bool(false)),
            ],
            vec![
                (text("code"), Val::Enum(text("unavailable"))),
                (text("message"), Val::String(text("PRIVATE"))),
                (text("extra"), Val::Bool(false)),
            ],
        ] {
            let record = BudgetedVec::from_iter(fields.into_iter(), owners.component(), 0).unwrap();
            let value = Val::Result(Err(Some(
                BudgetedBox::new(Val::Record(record), owners.component(), 0).unwrap(),
            )));
            assert_eq!(
                crate::host::step_error(
                    decode_guest_stream_events("1.0.0", std::slice::from_ref(&value)).unwrap_err(),
                )
                .code,
                "internal",
            );
        }
        for version in ["1.0.0", "1.1.0"] {
            let events = BudgetedVec::from_iter(std::iter::empty(), owners.component(), 0).unwrap();
            let eof = Val::Result(Ok(Some(
                BudgetedBox::new(Val::List(events), owners.component(), 0).unwrap(),
            )));
            assert!(
                decode_guest_stream_events(version, std::slice::from_ref(&eof))
                    .unwrap()
                    .is_empty()
            );
        }
        assert_eq!(meter.0.load(Ordering::Acquire), 0);
    }

    #[test]
    fn guest_stream_source_native_traps_and_conversion_faults_remain_internal() {
        let meter = Arc::new(Meter(AtomicU64::new(0), 64 * 1024));
        let owners = crate::AllocationOwners::from_budgets(meter.clone(), meter.clone());
        for error in [
            anyhow::Error::new(wasmtime::Trap::OutOfFuel),
            anyhow::Error::new(HostAllocationError::Invalid),
            anyhow::anyhow!("PRIVATE malformed result or native body"),
        ] {
            // export_call_owned returns these as Err before the typed-result
            // decoder runs; normal host mapping must not gain a source code.
            let error = crate::host::step_error(error);
            assert_eq!(error.audit_code(), "internal");
            assert_eq!(
                packet_code(&error_result(&error.into(), &owners).unwrap()),
                "internal",
            );
        }
        assert_eq!(meter.0.load(Ordering::Acquire), 0);
    }

    #[test]
    fn guest_stream_source_cleanup_unknown_overrides_unavailable_without_guest_text() {
        use tobkiri_flow::StepError;
        let source = || StepError::new("unavailable", "provider reported unavailable");
        let cleanup = || Err(StepError::new("unavailable", "PRIVATE cleanup body"));
        assert_eq!(
            stream_error_after_cleanup(source(), Ok(())).code,
            "unavailable"
        );
        let unknown = stream_error_after_cleanup(source(), cleanup());
        assert_eq!(unknown.code, "internal");
        assert_eq!(unknown.message, "stream cleanup completion unknown");
        let native =
            confirm_stream_cleanup(Err(anyhow::anyhow!("PRIVATE cleanup body"))).unwrap_err();
        assert_eq!(crate::host::step_error(native), unknown);
        let native = finish_stream_cleanup(
            Err(anyhow::anyhow!("PRIVATE incomplete cleanup")),
            Err(tobkiri_gate::GateError::Denied("PRIVATE terminal failure".into()).into()),
        )
        .unwrap_err();
        assert_eq!(crate::host::step_error(native), unknown);
        for code in [
            "filesystem-commit-unknown",
            "storage-sql-commit-unknown",
            "storage-pending-data-move",
        ] {
            let original = StepError::new(code, "fixed native no-replay metadata");
            assert_eq!(
                stream_error_after_cleanup(original.clone(), cleanup()),
                original
            );
            assert_eq!(
                stream_error_after_cleanup(source(), Err(original.clone())),
                original,
            );
        }
    }

    #[test]
    fn guest_stream_source_cleanup_cannot_replace_sealed_native_uncertainty() {
        let cx = effect_context();
        for error in [
            anyhow::Error::new(crate::filesystem::FsError::CommitUnknown),
            anyhow::Error::new(tobkiri_store::StoreError::CommitUnknown),
            anyhow::Error::new(tobkiri_store::StoreError::PendingDataMove),
        ] {
            let expected = storage_failure_metadata(&error).unwrap().0;
            let cleanup = confirm_stream_cleanup(Err(error)).unwrap_err();
            assert_eq!(storage_failure_metadata(&cleanup).unwrap().0, expected);
            let cleanup = finish_stream_cleanup(
                Err(cleanup),
                Err(anyhow::anyhow!("PRIVATE terminal write failure")),
            )
            .unwrap_err();
            assert_eq!(storage_failure_metadata(&cleanup).unwrap().0, expected);
            let result: Result<()> = finish_effect_delivery(
                &cx,
                Err(cleanup),
                confirm_stream_cleanup(Err(anyhow::anyhow!("PRIVATE second cleanup failure"))),
            );
            assert_eq!(
                storage_failure_metadata(&result.unwrap_err()).unwrap().0,
                expected
            );
        }
    }

    #[test]
    fn facaded_stream_source_no_replay_survives_cleanup_and_terminal_failure() {
        let cx = effect_context();
        let meter = Arc::new(Meter(AtomicU64::new(0), 64 * 1024));
        let owners = crate::AllocationOwners::from_budgets(meter.clone(), meter.clone());
        for code in [
            "filesystem-commit-unknown",
            "storage-sql-commit-unknown",
            "storage-pending-data-move",
        ] {
            let original: anyhow::Error =
                tobkiri_flow::StepError::new(code, "PRIVATE already-facaded CoreStream diagnostic")
                    .into();
            assert!(storage_failure_metadata(&original).is_none());
            let fixed_message = facaded_no_replay_metadata(&original).unwrap().1;
            let cleanup = finish_stream_cleanup(
                Err(original),
                Err(anyhow::anyhow!("ordinary cleanup delivery failed")),
            )
            .unwrap_err();
            let result: Result<()> = finish_effect_delivery(
                &cx,
                Err(cleanup),
                Err(tobkiri_gate::GateError::Persistence("PRIVATE terminal failure".into()).into()),
            );
            let error = result.unwrap_err();
            assert!(storage_failure_metadata(&error).is_none());
            assert_eq!(facaded_no_replay_metadata(&error).unwrap().0, code);
            let packet = error_result(&error, &owners).unwrap();
            assert_eq!(packet_code(&packet), "internal");
            let Val::Result(Err(Some(record))) = &packet else {
                unreachable!()
            };
            assert_eq!(
                crate::effect_values::text(crate::effect_values::field(record, "message").unwrap())
                    .unwrap(),
                fixed_message,
            );
            drop(packet);
            let small = Arc::new(Meter(AtomicU64::new(0), 96));
            let denied = crate::AllocationOwners::from_budgets(small.clone(), small.clone());
            let error = error_result_or_trap(error, &denied).unwrap_err();
            assert!(storage_failure_metadata(&error).is_none());
            assert_eq!(crate::host::step_error(error).code, code);
            assert_eq!(small.0.load(Ordering::Acquire), 0);
        }
        // Neither a message nor an ordinary guest-representable Core enum code
        // supplies a native or already-facaded no-replay classification.
        for imitation in [
            anyhow::anyhow!("storage-sql-commit-unknown"),
            tobkiri_flow::StepError::new("internal", STORAGE_COMMIT_UNKNOWN_MESSAGE).into(),
            tobkiri_flow::StepError::new("unavailable", STORAGE_PENDING_MOVE_MESSAGE).into(),
        ] {
            assert!(storage_failure_metadata(&imitation).is_none());
            assert!(facaded_no_replay_metadata(&imitation).is_none());
            let result: Result<()> = finish_effect_delivery(
                &cx,
                Err(imitation),
                Err(anyhow::anyhow!("ordinary terminal failure")),
            );
            assert!(facaded_no_replay_metadata(&result.unwrap_err()).is_none());
        }
        assert_eq!(meter.0.load(Ordering::Acquire), 0);
    }

    #[test]
    fn facaded_stream_cleanup_no_replay_survives_receipt_without_native_proof() {
        let cx = effect_context();
        for code in [
            "filesystem-commit-unknown",
            "storage-sql-commit-unknown",
            "storage-pending-data-move",
        ] {
            let mut receipt = crate::handles::HandleCloseReceipt::default();
            receipt.cleanup_started = true;
            record_stream_cleanup(
                &mut receipt,
                &cx,
                Err(tobkiri_flow::StepError::new(code, "PRIVATE cleanup body").into()),
            );
            assert!(!receipt.cleanup_done);
            // Repeated observation reuses the exact receipt, never reruns close.
            for _ in 0..2 {
                let cleanup = stream_cleanup_result(&receipt, &cx).unwrap_err();
                assert!(storage_failure_metadata(&cleanup).is_none());
                assert!(!cleanup.to_string().contains("PRIVATE"));
                let cleanup = finish_stream_cleanup(
                    Err(cleanup),
                    Err(anyhow::anyhow!("ordinary terminal failure")),
                )
                .unwrap_err();
                let result: Result<()> = finish_effect_delivery(
                    &cx,
                    Err(
                        tobkiri_flow::StepError::new("unavailable", "ordinary source failure")
                            .into(),
                    ),
                    Err(cleanup),
                );
                let error = result.unwrap_err();
                assert!(storage_failure_metadata(&error).is_none());
                assert_eq!(crate::host::step_error(error).code, code);
            }
            assert!(receipt.cleanup_started && !receipt.cleanup_done);
        }
        // Only an actual native sealed error can create a native receipt kind.
        for native in [
            anyhow::Error::new(crate::filesystem::FsError::CommitUnknown),
            anyhow::Error::new(tobkiri_store::StoreError::CommitUnknown),
            anyhow::Error::new(tobkiri_store::StoreError::PendingDataMove),
        ] {
            let expected = storage_failure_metadata(&native).unwrap().0;
            let mut receipt = crate::handles::HandleCloseReceipt::default();
            record_stream_cleanup(&mut receipt, &cx, Err(native));
            let error = stream_cleanup_result(&receipt, &cx).unwrap_err();
            assert_eq!(storage_failure_metadata(&error).unwrap().0, expected);
            assert!(facaded_no_replay_metadata(&error).is_none());
        }
    }

    fn effect_context() -> CallCx {
        use std::time::{Duration, Instant};
        use tobkiri_canon::{
            Digest,
            ids::{PackHash, PackId, ProfileId, RequestId},
        };
        use tobkiri_gate::{CoreCancellation, Principal, ResourceLimits};
        CallCx::root(
            ProfileId::parse("profile-effect-boundary").unwrap(),
            Principal::Pack {
                pack: PackId::parse("local.effect-boundary").unwrap(),
                hash: PackHash::new(Digest::hash(b"effect-boundary-test")),
            },
            RequestId::parse("effect-boundary-request").unwrap(),
            CoreCancellation::new(),
            Instant::now() + Duration::from_secs(30),
            ResourceLimits::default(),
        )
    }

    fn assert_fs_unknown(error: anyhow::Error) {
        assert!(matches!(
            error.downcast_ref::<crate::filesystem::FsError>(),
            Some(crate::filesystem::FsError::CommitUnknown)
        ));
        assert_eq!(
            storage_failure_metadata(&error).unwrap().0,
            "filesystem-commit-unknown"
        );
    }

    #[test]
    fn terminal_failure_cannot_replace_a_sealed_no_replay_result() {
        let cx = effect_context();
        // The sealed error itself remains authoritative even without a sticky
        // observation in this context. No guest string supplies this evidence.
        assert!(!cx.filesystem_mutation_possible());
        for original in [
            anyhow::Error::new(crate::filesystem::FsError::CommitUnknown),
            anyhow::Error::new(tobkiri_store::StoreError::CommitUnknown),
        ] {
            let code = storage_failure_metadata(&original).unwrap().0;
            let result: Result<()> = finish_effect_delivery(
                &cx,
                Err(original),
                Err(tobkiri_gate::GateError::Persistence("private audit diagnostic".into()).into()),
            );
            let error = result.unwrap_err();
            assert_eq!(storage_failure_metadata(&error).unwrap().0, code);
            assert!(!error.to_string().contains("private audit diagnostic"));
        }
    }

    #[test]
    fn post_mutation_delivery_failures_are_unknown_but_acknowledged_values_survive() {
        let cx = effect_context();
        cx.note_filesystem_mutation();
        let error = finish_effect_delivery(
            &cx,
            Ok(17),
            Err(tobkiri_gate::GateError::Persistence("audit unavailable".into()).into()),
        )
        .unwrap_err();
        assert_fs_unknown(error);
        let result: Result<()> = finish_effect_delivery(
            &cx,
            Err(anyhow::anyhow!("output encoding failed after mutation")),
            Ok(()),
        );
        assert_fs_unknown(result.unwrap_err());
        assert_eq!(finish_effect_delivery(&cx, Ok(17), Ok(())).unwrap(), 17);
    }

    #[test]
    fn store_retirement_and_void_ack_cannot_prove_filesystem_cleanup() {
        let cx = effect_context();
        assert!(guest_stream_cleanup_result(&cx, Ok(()), Ok(())).is_ok());
        assert!(
            guest_stream_cleanup_result(&cx, Err(anyhow::anyhow!("guest cancel trapped")), Ok(()),)
                .is_ok()
        );
        cx.note_filesystem_mutation();
        assert_fs_unknown(guest_stream_cleanup_result(&cx, Ok(()), Ok(())).unwrap_err());
        assert_fs_unknown(
            guest_stream_cleanup_result(
                &cx,
                Err(anyhow::anyhow!("guest cancellation grace elapsed")),
                Ok(()),
            )
            .unwrap_err(),
        );
        assert_fs_unknown(
            guest_stream_cleanup_result(
                &cx,
                Err(anyhow::anyhow!("guest cancellation grace elapsed")),
                Err(anyhow::anyhow!("Store retirement interrupted")),
            )
            .unwrap_err(),
        );
        assert_fs_unknown(unjoined_cleanup_result(&cx, Ok(())).unwrap_err());
    }

    #[test]
    fn repeated_cleanup_receipts_keep_the_native_failure_kind_without_replay() {
        let cx = effect_context();
        let mut receipt = crate::handles::HandleCloseReceipt::default();
        receipt.cleanup_started = true;
        record_stream_cleanup(
            &mut receipt,
            &cx,
            Err(crate::filesystem::FsError::CommitUnknown.into()),
        );
        for _ in 0..3 {
            assert_fs_unknown(stream_cleanup_result(&receipt, &cx).unwrap_err());
            assert!(receipt.cleanup_started);
            assert!(!receipt.cleanup_done);
        }
        let mut interrupted = crate::handles::HandleCloseReceipt::default();
        interrupted.cleanup_started = true;
        cx.note_filesystem_mutation();
        assert_fs_unknown(stream_cleanup_result(&interrupted, &cx).unwrap_err());
        assert!(!interrupted.cleanup_done);
    }

    struct Meter(AtomicU64, u64);
    impl AllocationBudget for Meter {
        fn reserve(&self, request: AllocationRequest) -> Result<(), AllocationError> {
            self.0
                .fetch_update(Ordering::AcqRel, Ordering::Acquire, |live| {
                    live.checked_add(request.bytes)
                        .filter(|next| *next <= self.1)
                })
                .map(|_| ())
                .map_err(|_| AllocationError::Limit)
        }
        fn release(&self, bytes: u64) {
            self.0
                .fetch_update(Ordering::AcqRel, Ordering::Acquire, |live| {
                    live.checked_sub(bytes)
                })
                .expect("release charged error payload only");
        }
    }
    impl HostAllocationBudget for Meter {
        fn reserve(&self, request: HostAllocationRequest) -> Result<(), HostAllocationError> {
            AllocationBudget::reserve(
                self,
                AllocationRequest {
                    kind: tobkiri_canon::AllocationKind::Temporary,
                    bytes: request.bytes,
                    elements: request.elements,
                    depth: request.depth,
                    work: request.work,
                },
            )
            .map_err(|_| HostAllocationError::Limit)
        }
        fn release(&self, bytes: u64) {
            AllocationBudget::release(self, bytes)
        }
    }
    #[test]
    fn source_cancel_ack_matches_both_frozen_void_abis_without_claiming_resource_cleanup() {
        for version in ["1.0.0", "1.1.0"] {
            let abi = tobkiri_wit::core_abi_version(version).unwrap();
            let cancel = &abi["stream-source"].functions["cancel"];
            assert!(cancel.result.is_none());
            assert!(matches!(
                cancel.params.as_slice(),
                [(name, tobkiri_wit::AbiType::Primitive("u64"))] if name == "local-id"
            ));
        }
        assert!(void_source_cancel_ack(&[]));
        assert!(!void_source_cancel_ack(&[Val::Result(Ok(None))]));
    }
    #[test]
    fn uncertain_commit_uses_closed_safe_metadata_without_losing_uncertainty() {
        let meter = Arc::new(Meter(AtomicU64::new(0), 64 * 1024));
        let owners = crate::AllocationOwners::from_budgets(meter.clone(), meter.clone());
        let error = anyhow::Error::new(tobkiri_store::StoreError::CommitUnknown)
            .context("PRIVATE SQL AND PARAMETER DIAGNOSTIC MUST NOT ESCAPE");
        let value = error_result(&error, &owners).unwrap();
        let Val::Result(Err(Some(record))) = &value else {
            panic!("expected typed core failure")
        };
        assert!(
            matches!(crate::effect_values::field(record, "code").unwrap(), Val::Enum(code) if code.as_str() == "internal")
        );
        let message =
            crate::effect_values::text(crate::effect_values::field(record, "message").unwrap())
                .unwrap();
        assert_eq!(message, STORAGE_COMMIT_UNKNOWN_MESSAGE);
        assert!(!message.contains("PRIVATE"));
        let step = crate::host::step_error(error);
        assert_eq!(step.code, "storage-sql-commit-unknown");
        assert_eq!(step.message, STORAGE_COMMIT_UNKNOWN_MESSAGE);
        assert!(meter.0.load(Ordering::Acquire) > 0);
        drop(value);
        assert_eq!(meter.0.load(Ordering::Acquire), 0);
    }

    #[test]
    fn filesystem_unknown_is_preserved_when_error_encoding_cannot_allocate() {
        let meter = Arc::new(Meter(AtomicU64::new(0), 96));
        let owners = crate::AllocationOwners::from_budgets(meter.clone(), meter.clone());
        let error = anyhow::Error::new(crate::filesystem::FsError::CommitUnknown)
            .context("PRIVATE PATH MUST NOT ESCAPE");
        let trapped = error_result_or_trap(error, &owners).unwrap_err();
        assert!(matches!(
            trapped.downcast_ref::<crate::filesystem::FsError>(),
            Some(crate::filesystem::FsError::CommitUnknown)
        ));
        let step = crate::host::step_error(trapped);
        assert_eq!(step.code, "filesystem-commit-unknown");
        assert!(!step.message.contains("PRIVATE"));
        assert_eq!(meter.0.load(Ordering::Acquire), 0);
        let imitation = anyhow::Error::new(crate::filesystem::FsError::Invalid(
            "filesystem-commit-unknown",
        ));
        assert!(storage_failure_metadata(&imitation).is_none());
    }

    #[test]
    fn filesystem_denial_is_a_typed_denial_with_no_native_path() {
        let meter = Arc::new(Meter(AtomicU64::new(0), 64 * 1024));
        let owners = crate::AllocationOwners::from_budgets(meter.clone(), meter.clone());
        let error = anyhow::Error::new(crate::filesystem::FsError::Denied("PRIVATE ROOT"));
        let value = error_result(&error, &owners).unwrap();
        let Val::Result(Err(Some(record))) = &value else {
            panic!("typed error expected")
        };
        assert!(
            matches!(crate::effect_values::field(record, "code").unwrap(),
            Val::Enum(code) if code.as_str() == "denied")
        );
        let message =
            crate::effect_values::text(crate::effect_values::field(record, "message").unwrap())
                .unwrap();
        assert!(!message.contains("PRIVATE"));
        drop(value);
        assert_eq!(meter.0.load(Ordering::Acquire), 0);
    }
    #[test]
    fn uncertain_commit_survives_error_packet_allocation_failure() {
        let meter = Arc::new(Meter(AtomicU64::new(0), 96));
        let owners = crate::AllocationOwners::from_budgets(meter.clone(), meter.clone());
        let error = anyhow::Error::new(tobkiri_store::StoreError::CommitUnknown)
            .context("PRIVATE SQL DIAGNOSTIC");
        let trapped = error_result_or_trap(error, &owners).unwrap_err();
        assert!(matches!(
            trapped.downcast_ref::<tobkiri_store::StoreError>(),
            Some(tobkiri_store::StoreError::CommitUnknown)
        ));
        let step = crate::host::step_error(trapped);
        assert_eq!(step.code, "storage-sql-commit-unknown");
        assert_eq!(step.message, STORAGE_COMMIT_UNKNOWN_MESSAGE);
        assert_eq!(meter.0.load(Ordering::Acquire), 0);
        // Only the closed unit uncertainty cause gets this treatment. A
        // generic native diagnostic cannot leak through the allocation path.
        let ordinary = anyhow::Error::new(tobkiri_store::StoreError::Invalid(
            "PRIVATE SQL DIAGNOSTIC".into(),
        ));
        let trapped = error_result_or_trap(ordinary, &owners).unwrap_err();
        assert!(
            trapped
                .downcast_ref::<tobkiri_store::StoreError>()
                .is_none()
        );
        assert!(!trapped.to_string().contains("PRIVATE"));
        assert_eq!(meter.0.load(Ordering::Acquire), 0);
    }

    #[test]
    fn pending_data_move_preserves_only_fixed_repair_metadata() {
        let meter = Arc::new(Meter(AtomicU64::new(0), 64 * 1024));
        let owners = crate::AllocationOwners::from_budgets(meter.clone(), meter.clone());
        let error = anyhow::Error::new(tobkiri_store::StoreError::PendingDataMove)
            .context("PRIVATE MOVE PATH AND BODY MUST NOT ESCAPE");
        let value = error_result(&error, &owners).unwrap();
        let Val::Result(Err(Some(record))) = &value else {
            panic!("expected typed core failure")
        };
        assert!(
            matches!(crate::effect_values::field(record, "code").unwrap(), Val::Enum(code) if code.as_str() == "internal")
        );
        let message =
            crate::effect_values::text(crate::effect_values::field(record, "message").unwrap())
                .unwrap();
        assert_eq!(message, STORAGE_PENDING_MOVE_MESSAGE);
        assert!(!message.contains("PRIVATE"));
        let step = crate::host::step_error(error);
        assert_eq!(step.code, "storage-pending-data-move");
        assert_eq!(step.message, STORAGE_PENDING_MOVE_MESSAGE);
        drop(value);
        assert_eq!(meter.0.load(Ordering::Acquire), 0);
    }

    #[test]
    fn pending_move_survives_packet_allocation_failure_without_trusting_error_text() {
        let meter = Arc::new(Meter(AtomicU64::new(0), 96));
        let owners = crate::AllocationOwners::from_budgets(meter.clone(), meter.clone());
        let error = anyhow::Error::new(tobkiri_store::StoreError::PendingDataMove)
            .context("PRIVATE MOVE PATH");
        let trapped = error_result_or_trap(error, &owners).unwrap_err();
        assert!(matches!(
            trapped.downcast_ref::<tobkiri_store::StoreError>(),
            Some(tobkiri_store::StoreError::PendingDataMove)
        ));
        let step = crate::host::step_error(trapped);
        assert_eq!(step.code, "storage-pending-data-move");
        assert_eq!(step.message, STORAGE_PENDING_MOVE_MESSAGE);
        let imitation = anyhow::Error::new(tobkiri_store::StoreError::Invalid(
            STORAGE_PENDING_MOVE_MESSAGE.into(),
        ));
        assert!(storage_failure_metadata(&imitation).is_none());
        let trapped = error_result_or_trap(imitation, &owners).unwrap_err();
        assert!(
            trapped
                .downcast_ref::<tobkiri_store::StoreError>()
                .is_none()
        );
        assert!(!trapped.to_string().contains("storage-pending-data-move"));
        assert_eq!(meter.0.load(Ordering::Acquire), 0);
    }

    #[tokio::test]
    async fn native_network_error_packet_preserves_only_closed_metadata_and_owned_storage() {
        let meter = Arc::new(Meter(AtomicU64::new(0), 64 * 1024));
        let owners = crate::AllocationOwners::from_budgets(meter.clone(), meter.clone());
        let error = crate::network_diagnostics::tests::native_body_timeout().await;
        let value = error_result(&error, &owners).unwrap();
        let Val::Result(Err(Some(record))) = &value else {
            panic!("expected typed native failure");
        };
        assert!(
            matches!(crate::effect_values::field(record, "code").unwrap(), Val::Enum(code) if code.as_str() == "unavailable")
        );
        let message =
            crate::effect_values::text(crate::effect_values::field(record, "message").unwrap())
                .unwrap();
        assert_eq!(message, "network-timeout: native transport timed out");
        assert!(!message.contains("PRIVATE"));
        assert!(!message.contains("http"));
        drop(value);
        assert_eq!(meter.0.load(Ordering::Acquire), 0);
        let sealed = error.context(tobkiri_store::StoreError::CommitUnknown);
        let value = error_result(&sealed, &owners).unwrap();
        let Val::Result(Err(Some(record))) = &value else {
            panic!("expected immutable storage uncertainty");
        };
        assert_eq!(
            crate::effect_values::text(crate::effect_values::field(record, "message").unwrap())
                .unwrap(),
            STORAGE_COMMIT_UNKNOWN_MESSAGE
        );
        drop(value);
        assert_eq!(meter.0.load(Ordering::Acquire), 0);
    }
}
