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
                                            crate::dispatch::TypedProxyInput {
                                                params: admitted.as_deref().unwrap_or(params),
                                                function: &signature,
                                            },
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
    } else if let Some(failure) = crate::handle_metadata::metadata_failure(error) {
        failure.wit_code()
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
    finish_handle_mode(host, entry, outcome, true).await
}
async fn finish_handle_mode(
    host: &PackHost,
    entry: &crate::HandleEntry,
    outcome: Outcome,
    retry_metadata: bool,
) -> Result<()> {
    entry.lease.begin_close();
    if entry.lease.grant.is_none() {
        return finish_handle_metadata(host, entry, outcome).await;
    }
    // Gate's terminal outbox, audit verification and acknowledgement perform
    // synchronous persistence even though the Gate API is async. A shutdown
    // can wake every source reaper at once; doing this work on the async
    // executor can prevent an unrelated source's cleanup Join/timer from being
    // polled for its entire grace. Retain the exact original lease/frame and
    // await the actual preadmitted metadata job without moving physical
    // cleanup. Its original Join is also retained by Session's terminal
    // inventory, so cancellation cannot move terminal work past final drain.
    let metadata = entry
        .lease
        .metadata
        .as_ref()
        .context("admitted handle metadata owner is missing")?
        .clone();
    metadata
        .run_delivery_mode(
            crate::handle_metadata::HandleDelivery::new(host.shared.gate.clone(), entry, outcome),
            retry_metadata,
        )
        .await
}

pub(crate) async fn finish_handle_metadata(
    host: &PackHost,
    entry: &crate::HandleEntry,
    outcome: Outcome,
) -> Result<()> {
    crate::handle_metadata::HandleDelivery::new(host.shared.gate.clone(), entry, outcome)
        .deliver()
        .await
        .map_err(crate::handle_metadata::DeliveryError::into_original)
}

#[cfg(test)]
#[path = "handle_metadata_tests.rs"]
mod handle_metadata_tests;

struct StreamRelease {
    source_report: Option<tobkiri_flow::StreamCloseReport>,
    // The first observer moves the original error. Only bounded closed codes
    // remain cached in HandleCloseReceipt for an abandoned/repeated observer.
    source_error: Option<tobkiri_flow::StepError>,
}
impl StreamRelease {
    fn complete() -> Self {
        Self {
            source_report: None,
            source_error: None,
        }
    }
    fn result(self) -> Result<()> {
        match self.source_error {
            Some(error) => Err(error.into()),
            None => Ok(()),
        }
    }
}
async fn release_stream_once(host: &PackHost, entry: &crate::HandleEntry) -> Result<StreamRelease> {
    entry.lease.begin_close();
    let mut receipt = entry.lease.close.lock().await;
    if !receipt.cleanup_started {
        receipt.cleanup_started = true;
        match release_stream(host, entry).await {
            Ok(released) => {
                receipt.cleanup_done = true;
                receipt.source_report = released.source_report;
                return Ok(released);
            }
            Err(_) => {
                receipt.cleanup_error = Some("stream cleanup completion unknown");
            }
        }
    }
    if !receipt.cleanup_done {
        bail!(
            receipt
                .cleanup_error
                .unwrap_or("stream cleanup attempt was interrupted")
        )
    }
    Ok(StreamRelease {
        source_report: receipt.source_report,
        source_error: receipt
            .source_report
            .and_then(|report| report.cached_error()),
    })
}

async fn release_callback_once(host: &PackHost, entry: &crate::HandleEntry) -> Result<()> {
    entry.lease.begin_close();
    let mut receipt = entry.lease.close.lock().await;
    if !receipt.cleanup_started {
        receipt.cleanup_started = true;
        let cleanup = match entry.owner.principal().pack() {
            Some(_) => host.discard_handle_source(entry).await,
            None => Err(anyhow::anyhow!("callback physical owner missing")),
        }
        .and_then(|()| {
            if entry.operation.is_none()
                && entry
                    .native_children
                    .as_ref()
                    .is_some_and(|children| children.load(std::sync::atomic::Ordering::Acquire))
            {
                bail!("callback Store ended but registered native child completion is unknown")
            }
            Ok(())
        });
        receipt.cleanup_done = cleanup.is_ok();
        receipt.cleanup_error = cleanup.err().map(|_| "callback cleanup completion unknown");
    }
    if !receipt.cleanup_done {
        bail!(
            receipt
                .cleanup_error
                .unwrap_or("callback cleanup attempt was interrupted")
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
    let released = release_stream_once(host, entry).await?;
    let outcome = released
        .source_report
        .map_or(Outcome::Succeeded, |report| report.outcome());
    finish_handle(host, entry, outcome).await?;
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
    {
        let mut receipt = entry.lease.close.lock().await;
        receipt.close_report_confirmed = receipt.cleanup_done && receipt.delivered;
    }
    released.result()
}

fn close_reason(reason: ExpiryReason) -> tobkiri_flow::StreamCloseReason {
    match reason {
        ExpiryReason::Deadline => tobkiri_flow::StreamCloseReason::Deadline,
        ExpiryReason::UserCancel => tobkiri_flow::StreamCloseReason::Manual,
        ExpiryReason::Retired => tobkiri_flow::StreamCloseReason::Retired,
        ExpiryReason::Revoked | ExpiryReason::Unknown => tobkiri_flow::StreamCloseReason::Unknown,
    }
}
fn record_cleanup_cause(entry: &crate::HandleEntry, reason: ExpiryReason) -> Result<()> {
    if let Some(control) = &entry.close_control {
        control
            .record_close(close_reason(reason))
            .map_err(anyhow::Error::new)?;
    }
    Ok(())
}
/// Internal observation of the immutable selected-cause slot. `Pending`
/// means the slot was transiently unobservable or an authenticated busy
/// retry is still outstanding — the same cleanup owner must retry rather
/// than treating the slot as a settled "unknown". `Uncertain` marks a
/// poisoned slot: unconfirmed and never retryable as busy.
pub(crate) enum CauseObservation {
    Observed(Option<crate::handles::SelectedCloseCause>),
    Pending,
    Uncertain,
}
pub(crate) fn observe_cause(entry: &crate::HandleEntry) -> CauseObservation {
    match entry.lease.cause.try_lock() {
        Ok(cause) => CauseObservation::Observed(cause.clone()),
        // WouldBlock is a positive transient signal; poison is not.
        Err(std::sync::TryLockError::WouldBlock) => CauseObservation::Pending,
        Err(std::sync::TryLockError::Poisoned(_)) => CauseObservation::Uncertain,
    }
}
/// Bounded re-observation for the original async cleanup owner only. An
/// empty slot is reported absent only when no authenticated busy retry is
/// outstanding; transient contention and outstanding intent both wait inside
/// the lease's original cleanup episode rather than committing "unknown".
/// No cause is ever manufactured here and uncertainty is never cleared.
pub(crate) async fn observe_cause_bounded(entry: &crate::HandleEntry) -> CauseObservation {
    let bound = entry.lease.cleanup_episode_bound(entry.owner.deadline());
    let mut backoff = std::time::Duration::from_millis(1);
    loop {
        match observe_cause(entry) {
            CauseObservation::Observed(None)
                if entry
                    .lease
                    .cause_pending
                    .load(std::sync::atomic::Ordering::Acquire) =>
            {
                // An authenticated busy retry is still outstanding: the
                // empty slot is unconfirmed, not a settled unknown.
            }
            CauseObservation::Pending => {}
            observed => return observed,
        }
        let now = std::time::Instant::now();
        if now >= bound {
            return CauseObservation::Pending;
        }
        tokio::time::sleep(backoff.min(bound.saturating_duration_since(now))).await;
        backoff = (backoff * 2).min(std::time::Duration::from_millis(32));
    }
}
/// The existing async cleanup owner's single bounded selection episode.
/// Authenticated pre-callback busy retries with backoff strictly OUTSIDE all
/// Core/generation/DB/cause guards; the window is the original two-second
/// cleanup episode initialized once per lease and narrowed by the original
/// effect deadline. Exhaustion leaves the cause unselected — it is never
/// committed as a proven cancellation and uncertainty is never cleared.
async fn select_cleanup_cause_bounded(
    entry: &crate::HandleEntry,
    reason: ExpiryReason,
) -> Result<()> {
    use crate::host::CleanupSelection;
    let Some(selector) = entry.close_selector.clone() else {
        return record_cleanup_cause(entry, reason);
    };
    let bound = entry.lease.cleanup_episode_bound(entry.owner.deadline());
    let mut backoff = std::time::Duration::from_millis(4);
    loop {
        // One synchronous attempt each turn; the stored original intent is
        // republished idempotently, never rewritten.
        match selector.select_typed(close_reason(reason), None) {
            CleanupSelection::Selected => return Ok(()),
            CleanupSelection::Uncertain(error) => return Err(error.into()),
            CleanupSelection::Vacant | CleanupSelection::Busy => {
                let now = std::time::Instant::now();
                if now >= bound {
                    return Err(anyhow::anyhow!("cleanup cause observation remained busy"));
                }
                tokio::time::sleep(backoff.min(bound.saturating_duration_since(now))).await;
                backoff = (backoff * 2).min(std::time::Duration::from_millis(64));
            }
        }
    }
}
async fn cancellation_outcome(
    entry: &crate::HandleEntry,
    _reason: ExpiryReason,
    proven: bool,
) -> Outcome {
    use crate::handles::SelectedCloseCause as Cause;
    // A manual intent is not a terminal reservation. A deadline may still win
    // before physical completion, and Gate's CAS rechecks it at settlement.
    // A transiently busy or pending cause slot is never a settled unknown.
    if proven && entry.lease.grant.is_none() {
        let receipt = entry.lease.close.lock().await;
        if let Some(report) = receipt.source_report {
            return report.outcome();
        }
    }
    let selected = observe_cause_bounded(entry).await;
    if let CauseObservation::Observed(Some(Cause::Terminal(outcome))) = &selected {
        return outcome.clone();
    }
    if std::time::Instant::now() >= entry.owner.deadline() {
        return Outcome::Failed {
            code: "deadline-failure-unknown".into(),
        };
    }
    match selected {
        // Cancelled is the shared cancellation disposition. The independently
        // observed Revoked cause remains immutable; it is never relabelled as
        // manual intent and still requires actual physical completion.
        CauseObservation::Observed(Some(Cause::Manual | Cause::Shutdown(_) | Cause::Revoked))
            if proven
                && !entry
                    .lease
                    .cause_uncertain
                    .load(std::sync::atomic::Ordering::Acquire) =>
        {
            Outcome::Cancelled
        }
        CauseObservation::Observed(Some(Cause::ProducerFailure(code))) if proven => {
            Outcome::Failed { code: code.into() }
        }
        CauseObservation::Observed(Some(Cause::Deadline)) => Outcome::Failed {
            code: "deadline-failure-unknown".into(),
        },
        _ => Outcome::Failed {
            code: "cleanup-unknown".into(),
        },
    }
}
fn cleanup_cause_confirmed(
    entry: &crate::HandleEntry,
    receipt: &crate::handles::HandleCloseReceipt,
    observed: CauseObservation,
) -> bool {
    use crate::handles::SelectedCloseCause as Cause;
    if receipt.deadline_settlement
        || matches!(&receipt.outcome, Some(Outcome::Failed { code }) if matches!(code.as_str(), "cleanup-unknown" | "deadline-failure-unknown" | "source-retired"))
    {
        return false;
    }
    if entry
        .lease
        .cause_uncertain
        .load(std::sync::atomic::Ordering::Acquire)
    {
        return false;
    }
    if let Some(report) = receipt.source_report
        && !receipt
            .outcome
            .as_ref()
            .is_some_and(|outcome| report.matches_outcome(outcome))
    {
        return false;
    }
    match observed {
        CauseObservation::Observed(Some(
            Cause::Manual | Cause::Shutdown(_) | Cause::Revoked | Cause::ProducerFailure(_),
        )) => true,
        CauseObservation::Observed(Some(Cause::Terminal(
            Outcome::Succeeded | Outcome::Cancelled,
        ))) => true,
        CauseObservation::Observed(Some(Cause::Terminal(Outcome::Failed { code }))) => !matches!(
            code.as_str(),
            "cleanup-unknown" | "deadline-failure-unknown" | "source-retired"
        ),
        CauseObservation::Observed(None) if entry.lease.grant.is_none() => true,
        _ => false,
    }
}

#[derive(Clone, Copy)]
pub(crate) enum ExpiryReason {
    Deadline,
    Revoked,
    UserCancel,
    Retired,
    Unknown,
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
            let released = release_stream_once(host, &entry).await?;
            let outcome = released
                .source_report
                .map_or(Outcome::Succeeded, |report| report.outcome());
            finish_handle(host, &entry, outcome).await?;
            host.shared
                .handles
                .lock()
                .map_err(|_| anyhow::anyhow!("handle table poisoned"))?
                .remove(id, HandleKind::Stream, cx)?;
            released.result()?;
        }
        Err(_) => {
            // Release the provider on a malformed batch or trap, then record
            // exactly one final outcome for the original StreamOpen grant.
            let cleanup = release_stream_once(host, &entry).await;
            finish_handle(
                host,
                &entry,
                if let Some(report) = cleanup
                    .as_ref()
                    .ok()
                    .and_then(|released| released.source_report)
                    .filter(|_| entry.lease.grant.is_none())
                {
                    report.outcome()
                } else if std::time::Instant::now() >= entry.owner.deadline() {
                    cancellation_outcome(&entry, ExpiryReason::Deadline, false).await
                } else if cx.cancel().is_cancelled() {
                    cancellation_outcome(&entry, ExpiryReason::Revoked, cleanup.is_ok()).await
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
async fn release_stream(host: &PackHost, entry: &crate::HandleEntry) -> Result<StreamRelease> {
    if let Some(source) = &entry.source {
        return match tokio::time::timeout(std::time::Duration::from_secs(2), async {
            let mut source = source.lock().await;
            let physical = source
                .physical_completion()
                .context("core source physical receipt is unavailable")?;
            let closed = source.close().await;
            physical.wait().await.map_err(anyhow::Error::new)?;
            let report = source.close_report();
            if closed.is_err()
                && !report.is_some_and(|report| {
                    report.business_error().is_some() || report.audit_error().is_some()
                })
            {
                // Physical completion alone does not classify an authority or
                // terminal-integrity failure as a harmless business error.
                bail!("core source close classification remains unknown")
            }
            Ok(StreamRelease {
                source_report: report,
                source_error: closed.err(),
            })
        })
        .await
        {
            Ok(result) => result,
            Err(_) => {
                // A pull holds this mutex while awaiting the run. Revoke its
                // original request after the grace bound so cleanup cannot wait
                // indefinitely for its own producer.
                entry.owner.cancel().cancel();
                bail!("core stream cleanup exceeded its cancellation grace")
            }
        };
    }
    if entry
        .store_disposal
        .as_ref()
        .is_some_and(|receipt| receipt.is_complete())
    {
        host.discard_handle_source(entry).await?;
        if entry.operation.is_none()
            && entry
                .native_children
                .as_ref()
                .is_some_and(|children| children.load(std::sync::atomic::Ordering::Acquire))
        {
            bail!("component Store ended but registered native child completion is unknown")
        }
        return Ok(StreamRelease::complete());
    }
    // An already-retiring source may have an owned disposal task. Skip guest
    // cleanup and observe that exact generation instead of a replacement VM.
    let generation_live = host.check_handle_generation(entry).is_ok();
    let (owner, _) = entry
        .owner
        .principal()
        .pack()
        .context("stream owner is not a Pack")?;
    let mut cancel_acknowledged = false;
    if generation_live
        && host.shared.gate.check_context(&entry.owner).is_ok()
        && let (Some(frame), Some(version)) = (&entry.source_frame, entry.source_version)
    {
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
    // The frozen source cancel operation returns void. Its ACK is not proof
    // that an untrusted guest dropped every native response/resource it owns.
    // Until per-stream native resource custody exists, ALWAYS await destruction
    // of this Store, including after a valid ACK. Revoked/expired contexts never
    // execute fresh guest cleanup. Native tasks retain their own actual leases.
    host.discard_handle_source(entry)
        .await
        .context(if cancel_acknowledged {
            "physical stream cleanup incomplete after void cancel acknowledgement"
        } else {
            "physical stream cleanup incomplete without cancel acknowledgement"
        })?;
    if entry.operation.is_none()
        && entry
            .native_children
            .as_ref()
            .is_some_and(|children| children.load(std::sync::atomic::Ordering::Acquire))
    {
        bail!("component Store ended but registered native child completion is unknown")
    }
    Ok(StreamRelease::complete())
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
    cancel_captured_stream_reason_mode(host, cx, id, entry, reason, true).await
}
async fn cancel_captured_stream_reason_mode(
    host: &PackHost,
    cx: &CallCx,
    id: u64,
    entry: &crate::HandleEntry,
    reason: ExpiryReason,
    retry_metadata: bool,
) -> Result<()> {
    if entry.kind != HandleKind::Stream || !entry.recipient_matches(cx) {
        bail!("captured stream outside the recipient scope")
    }
    let selection = select_cleanup_cause_bounded(entry, reason).await;
    let completed = {
        let receipt = entry.lease.close.lock().await;
        receipt.cleanup_done && receipt.delivered
    };
    if completed {
        // A later observer failure cannot revoke the same entry's already
        // completed cleanup and acknowledged terminal. Reobserve its exact
        // immutable cause/receipt — a transiently busy or pending slot is
        // never a settled unknown — so uncertainty, deadline and retirement
        // checks remain authoritative even when this observer lost. No fresh
        // grant or effect is minted on this branch.
        let observed = observe_cause_bounded(entry).await;
        let mut receipt = entry.lease.close.lock().await;
        if !cleanup_cause_confirmed(entry, &receipt, observed) {
            bail!("earlier stream cleanup outcome remains uncertain or failed")
        }
        receipt.close_report_confirmed = true;
        return match receipt
            .source_report
            .and_then(|report| report.cached_error())
        {
            Some(error) => Err(error.into()),
            None => Ok(()),
        };
    }
    let result = release_stream_once(host, entry).await;
    finish_handle_mode(
        host,
        entry,
        cancellation_outcome(entry, reason, result.is_ok() && selection.is_ok()).await,
        retry_metadata,
    )
    .await?;
    if result.is_ok() {
        host.shared
            .handles
            .lock()
            .map_err(|_| anyhow::anyhow!("handle table poisoned"))?
            .remove_captured(id, cx, entry)?;
    }
    let released = result?;
    selection?;
    let observed = observe_cause_bounded(entry).await;
    {
        let mut receipt = entry.lease.close.lock().await;
        if !cleanup_cause_confirmed(entry, &receipt, observed) {
            bail!("stream cleanup outcome is uncertain or failed")
        }
        receipt.close_report_confirmed = true;
    }
    released.result()
}
pub(crate) async fn expire_handle(
    host: &PackHost,
    cx: &CallCx,
    id: u64,
    kind: HandleKind,
    reason: ExpiryReason,
) -> Result<()> {
    let entry = host
        .shared
        .handles
        .lock()
        .map_err(|_| anyhow::anyhow!("handle table poisoned"))?
        .resolve_cleanup(id, kind, cx)?;
    expire_captured_handle(host, cx, id, &entry, reason).await
}

fn completed_eof_cleanup(
    receipt: &crate::handles::HandleCloseReceipt,
    cause_uncertain: bool,
) -> bool {
    receipt.cleanup_done
        && receipt.delivered
        && !receipt.deadline_settlement
        && !cause_uncertain
        && receipt.outcome.as_ref().is_some_and(|outcome| {
            matches!(outcome, Outcome::Succeeded)
                && receipt
                    .source_report
                    .is_none_or(|report| report.matches_outcome(outcome))
        })
}

/// Physical completion and the reaper's reported business/delivery failure
/// are independent. Only this adapter can construct a positive report, from
/// the original lease receipt after its real cleanup path has returned.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct SourceCleanupReport {
    // Retain the original diagnostic in the cached Join receipt, including
    // after delivery repair. Production consumes only its physical proof;
    // test accessors verify that repair never overwrites this observation.
    _observation: SourceCleanupObservation,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum SourceCleanupObservation {
    Complete,
    TerminalUnacknowledged,
    SourceReportedFailure,
}
impl SourceCleanupReport {
    #[cfg(test)]
    pub(crate) fn joined_fixture_work() -> Self {
        // Task-custody unit fixtures contain no Wasm source or consumed Grant.
        // Their completed unit work cannot manufacture a production receipt.
        Self {
            _observation: SourceCleanupObservation::Complete,
        }
    }
    #[cfg(test)]
    pub(crate) fn observation(self) -> SourceCleanupObservation {
        self._observation
    }
    #[cfg(test)]
    pub(crate) fn reported_failure(self) -> bool {
        self._observation != SourceCleanupObservation::Complete
    }
}

fn source_cleanup_report(
    receipt: &crate::handles::HandleCloseReceipt,
    reported_failure: bool,
) -> Option<SourceCleanupReport> {
    (receipt.cleanup_started && receipt.cleanup_done && receipt.cleanup_error.is_none()).then_some(
        SourceCleanupReport {
            _observation: if !receipt.delivered {
                SourceCleanupObservation::TerminalUnacknowledged
            } else if reported_failure {
                SourceCleanupObservation::SourceReportedFailure
            } else {
                SourceCleanupObservation::Complete
            },
        },
    )
}

pub(crate) async fn reap_captured_handle(
    host: &PackHost,
    cx: &CallCx,
    id: u64,
    entry: &crate::HandleEntry,
    reason: ExpiryReason,
) -> Result<SourceCleanupReport> {
    // The owning reaper joins an existing failure; notification must not
    // create another delivery generation. Explicit close drives recovery.
    let result = expire_captured_handle_mode(host, cx, id, entry, reason, false).await;
    let report = source_cleanup_report(&*entry.lease.close.lock().await, result.is_err());
    if let Some(report) = report {
        // Preserve the normal reaper failure as typed diagnostic evidence.
        // Delivery is still independently required by this lease's original
        // metadata Join. Neither a repaired outbox nor an ordinary source
        // business failure can replace a missing physical cleanup receipt.
        return Ok(report);
    }
    result?;
    bail!("source reaper has no original physical cleanup receipt")
}

/// The owning reaper captures this exact entry before exposure completes.
/// Its physical/result receipt remains valid after every table alias is gone.
pub(crate) async fn expire_captured_handle(
    host: &PackHost,
    cx: &CallCx,
    id: u64,
    entry: &crate::HandleEntry,
    reason: ExpiryReason,
) -> Result<()> {
    expire_captured_handle_mode(host, cx, id, entry, reason, true).await
}
async fn expire_captured_handle_mode(
    host: &PackHost,
    cx: &CallCx,
    id: u64,
    entry: &crate::HandleEntry,
    reason: ExpiryReason,
    retry_metadata: bool,
) -> Result<()> {
    if !entry.recipient_matches(cx) {
        bail!("captured cleanup outside the recipient scope")
    }
    // A source that already reached EOF needs no new cancellation cause. This
    // requires its original successful physical cleanup AND delivered winner;
    // metadata acknowledgement alone never takes this branch.
    if completed_eof_cleanup(
        &*entry.lease.close.lock().await,
        entry
            .lease
            .cause_uncertain
            .load(std::sync::atomic::Ordering::Acquire),
    ) {
        return Ok(());
    }
    if entry.kind == HandleKind::Stream {
        return cancel_captured_stream_reason_mode(host, cx, id, entry, reason, retry_metadata)
            .await;
    }
    let selection = select_cleanup_cause_bounded(entry, reason).await;
    let cleanup = release_callback_once(host, entry).await;
    finish_handle_mode(
        host,
        entry,
        cancellation_outcome(entry, reason, cleanup.is_ok() && selection.is_ok()).await,
        retry_metadata,
    )
    .await?;
    if cleanup.is_ok() {
        host.shared
            .handles
            .lock()
            .map_err(|_| anyhow::anyhow!("handle table poisoned"))?
            .remove_captured(id, cx, entry)?;
    }
    cleanup?;
    selection?;
    let observed = observe_cause_bounded(entry).await;
    if !cleanup_cause_confirmed(entry, &*entry.lease.close.lock().await, observed) {
        bail!("callback cleanup outcome is uncertain or failed")
    }
    Ok(())
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
    let _ = record_cleanup_cause(&entry, ExpiryReason::Retired);
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
            host.register_guest_handle(
                kind,
                id,
                None,
                crate::handle_transfer::HandleSourceContext {
                    owner: cx,
                    recipient: cx,
                    authority,
                    provider: frame,
                },
            )
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
