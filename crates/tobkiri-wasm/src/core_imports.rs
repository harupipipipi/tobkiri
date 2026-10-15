use anyhow::{Context, Result, bail};
use std::sync::Arc;
use tobkiri_canon::{CoreValue, ids::OpName};
use tobkiri_gate::{CallCx, Outcome};
use tobkiri_manifest::{ShapeDef, VerifiedPack};
use wasmtime::StoreContextMut;
use wasmtime::component::{
    Component, Linker, Val,
    types::{ComponentItem, Type},
};

use crate::host::{PackHost, PackState, Shared, interface_name};
use crate::{HandleKind, InvocationState};

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
                                    let values = host
                                        .invoke_values_owned(
                                            &cx, &shape, &op, params, &signature, &owners, &frame,
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

/// Only sealed Store unit variants select stable public metadata. Never infer
/// repair/commit status from a guest code, message, path or native diagnostic.
pub(crate) fn storage_failure_metadata(
    error: &anyhow::Error,
) -> Option<(&'static str, &'static str)> {
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

/// Failure to allocate an error packet must not replace sealed no-replay
/// state with an ordinary allocation failure. Trap with that original Store
/// unit error; the outer host mapping uses only the fixed safe metadata.
fn error_result_or_trap(
    error: anyhow::Error,
    owners: &crate::owned_values::ValueOwners,
) -> Result<Val> {
    match error_result(&error, owners) {
        Ok(value) => Ok(value),
        Err(_) if storage_failure_metadata(&error).is_some() => Err(error),
        Err(encoding) => Err(encoding),
    }
}

pub(crate) fn error_result(
    error: &anyhow::Error,
    owners: &crate::owned_values::ValueOwners,
) -> Result<Val> {
    use wasmtime::component::{BudgetedBox, BudgetedString, BudgetedVec};
    let storage_failure = storage_failure_metadata(error);
    let code = if storage_failure.is_some() {
        "internal"
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
    let message = storage_failure.map_or("core operation failed", |(_, message)| message);
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

async fn release_stream_once(host: &PackHost, entry: &crate::HandleEntry) -> Result<()> {
    entry.lease.begin_close();
    let mut receipt = entry.lease.close.lock().await;
    if !receipt.cleanup_started {
        receipt.cleanup_started = true;
        let result = release_stream(host, entry).await;
        receipt.cleanup_done = result.is_ok();
        receipt.cleanup_error = result.err().map(|_| "stream cleanup completion unknown");
    }
    if !receipt.cleanup_done {
        bail!(
            receipt
                .cleanup_error
                .unwrap_or("stream cleanup attempt was interrupted")
        )
    }
    Ok(())
}

pub(crate) async fn finish_stream_eof(
    host: &PackHost,
    cx: &CallCx,
    id: u64,
    entry: &crate::HandleEntry,
) -> Result<()> {
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

fn cancellation_outcome(entry: &crate::HandleEntry, reason: ExpiryReason, proven: bool) -> Outcome {
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
    }.await;
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
        Err(_) => {
            // Release the provider on a malformed batch or trap, then record
            // exactly one final outcome for the original StreamOpen grant.
            let cleanup = release_stream_once(host, &entry).await;
            finish_handle(
                host,
                &entry,
                if std::time::Instant::now() >= entry.owner.deadline() {
                    cancellation_outcome(&entry, ExpiryReason::Deadline, false)
                } else if cx.cancel().is_cancelled() {
                    cancellation_outcome(&entry, ExpiryReason::Revoked, cleanup.is_ok())
                } else {
                    Outcome::Failed {
                        code: "stream-pull".into(),
                    }
                },
            )
            .await?;
            if cleanup.is_ok() {
                host.shared
                    .handles
                    .lock()
                    .map_err(|_| anyhow::anyhow!("handle table poisoned"))?
                    .remove(id, HandleKind::Stream, cx)?;
            }
        }
        _ => {}
    }
    result
}

/// A source-open frame remains live until actual cleanup. No per-pull grant is
/// minted and each batch is converted/encoded before publishing it to callers.
pub(crate) async fn pull_guest_source(
    host: &PackHost,
    entry: &crate::HandleEntry,
    max: u32,
    final_output: bool,
) -> Result<(CoreValue, Option<tobkiri_canon::CoreBuffer>)> {
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
    let [Val::Result(Ok(Some(value)))] = values.as_slice() else {
        bail!("guest stream pull returned an error or invalid result")
    };
    let Val::List(values) = &**value else {
        bail!("guest stream pull did not return a list")
    };
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
async fn release_stream(host: &PackHost, entry: &crate::HandleEntry) -> Result<()> {
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
    let mut cancel_acknowledged = false;
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
            cancel_acknowledged = matches!(
                tokio::time::timeout(std::time::Duration::from_secs(2), call).await,
                Ok(Ok((ref values, _))) if void_source_cancel_ack(values.as_slice())
            );
        }
    }
    // The frozen source cancel operation returns void. Its ACK is not proof
    // that an untrusted guest dropped every native response/resource it owns.
    // Until per-stream native resource custody exists, ALWAYS await destruction
    // of this Store, including after a valid ACK. Revoked/expired contexts never
    // execute fresh guest cleanup. Native tasks retain their own actual leases.
    host.discard_instance(owner)
        .await
        .context(if cancel_acknowledged {
            "physical stream cleanup incomplete after void cancel acknowledgement"
        } else {
            "physical stream cleanup incomplete without cancel acknowledgement"
        })?;
    Ok(())
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
    let entry = host
        .shared
        .handles
        .lock()
        .map_err(|_| anyhow::anyhow!("handle table poisoned"))?
        .resolve_cleanup(id, HandleKind::Stream, cx)?;
    cancel_captured_stream_reason(host, cx, id, &entry, reason).await
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
    if entry.kind != HandleKind::Stream || !entry.recipient_matches(cx) {
        bail!("captured stream outside the recipient scope")
    }
    {
        let receipt = entry.lease.close.lock().await;
        if receipt.cleanup_done && receipt.delivered {
            return Ok(());
        }
    }
    let result = release_stream_once(host, &entry).await;
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
    result
}
pub(crate) async fn expire_handle(
    host: &PackHost,
    cx: &CallCx,
    id: u64,
    kind: HandleKind,
    reason: ExpiryReason,
) -> Result<()> {
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
async fn close_retired_handle(
    host: &PackHost,
    cx: &CallCx,
    id: u64,
    kind: HandleKind,
) -> Result<()> {
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

pub(crate) async fn invoke_callback(
    host: &PackHost,
    cx: &CallCx,
    id: u64,
    args: CoreValue,
    _owners: &crate::owned_values::ValueOwners,
) -> Result<CoreValue> {
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

#[cfg(test)]
mod error_tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};
    use tobkiri_canon::{AllocationBudget, AllocationError, AllocationRequest};
    use wasmtime::component::{HostAllocationBudget, HostAllocationError, HostAllocationRequest};
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
}
