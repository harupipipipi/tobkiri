//! Verified generic FS imports. Root selection is a candidate, never authority.
//! The native supervisor owns the original work and completion through
//! physical completion, guest conversion and immutable terminal handling.

use super::{get_resource, push, resource_rep};
use crate::{
    InvocationState,
    child_census::NativeWorkError,
    effect_values::{EffectValues, text, uint},
    filesystem::{
        FsCall, FsConsumption, FsDirectory, FsDisposition, FsError, FsOutput, FsRegistry,
        PreparedFsCall, PreparedFsOpen,
    },
    host::{NativeWorkSpawner, PackHost, PackState},
    owned_values::ValueOwners,
};
use anyhow::{Context, Result, bail};
use std::sync::Arc;
use tobkiri_canon::{AllocationBudget, AllocationError, CoreValue};
use tobkiri_flow::StepError;
use tobkiri_gate::{
    CallCx, CleanupOnlyConsumption, ControlKind, ControlReservation, Gate, GateError, Grant,
    GrantId, Outcome, UnknownConsumption,
};
use tokio::sync::oneshot;
use wasmtime::{
    StoreContextMut,
    component::{BudgetedVecBuilder, HostAllocationError, Val},
};

const MAX_LIST_ENTRIES: usize = 1024;
const MAX_LIST_BYTES: usize = 64 * 1024;
const MAX_ENTRY_NAME: usize = 4096;

#[derive(Clone, Copy)]
enum DirectoryOutput {
    Bytes,
    Entries,
    Stat,
    Unit,
}

/// Core supplies the original descriptor registry and checks the actual current
/// approved permission intersection before issuing a token for a candidate ID.
/// Implementations must never interpret the candidate as a filesystem path.
pub trait FilesystemAccess: Send + Sync {
    fn registry(&self) -> &FsRegistry;
    fn issue_open_token(&self, cx: &CallCx, candidate: &str) -> Result<String, FsError>;
}

fn failure(error: FsError) -> anyhow::Error {
    match error {
        FsError::Gate(error) => error.into(),
        FsError::Invalid(_) => StepError::new("invalid", "invalid filesystem operation").into(),
        FsError::Denied(_) => StepError::new("denied", "filesystem operation denied").into(),
        FsError::Allocation(_) => {
            StepError::new("limit-exceeded", "filesystem output limit exceeded").into()
        }
        FsError::Io(error) if error.kind() == std::io::ErrorKind::NotFound => {
            StepError::new("not-found", "filesystem object not found").into()
        }
        FsError::CommitUnknown => anyhow::Error::new(FsError::CommitUnknown).context(
            StepError::new("internal", "filesystem outcome unknown; do not replay"),
        ),
        FsError::Io(_) | FsError::Unavailable => {
            StepError::new("unavailable", "filesystem operation unavailable").into()
        }
    }
}

pub(super) async fn pick_directory(
    host: &PackHost,
    cx: &CallCx,
    purpose: &str,
    owners: &ValueOwners,
) -> Result<Vec<Val>> {
    if purpose.len() > 4096 || purpose.contains('\0') {
        bail!("directory picker purpose exceeds its bound")
    }
    if cx.limits().output_bytes < 72 {
        return Err(
            GateError::Limit("filesystem token exceeds the borrowed output limit".into()).into(),
        );
    }
    let access = host
        .shared
        .filesystem
        .get()
        .context("Core filesystem registry is unavailable")?;
    // The picker may return only a registered opaque candidate ID. A pathname,
    // an approval assertion or a root from another Core cannot register a root.
    let candidate = tokio::select! {
        _ = cx.cancel().cancelled() => return Err(GateError::Cancelled.into()),
        _ = tokio::time::sleep_until(cx.deadline().into()) => return Err(GateError::Deadline.into()),
        result = host.shared.io.pick_directory_budgeted(purpose, 72, owners.graph().clone()) => result?,
    }
    .ok_or_else(|| StepError::new("cancelled", "directory selection cancelled"))?;
    if candidate.len() > 72 {
        bail!("directory picker candidate exceeds its bound")
    }
    host.shared.gate.check_context(cx)?;
    let token = access
        .issue_open_token(cx, candidate.as_str()?)
        .map_err(failure)?;
    EffectValues(owners).ok(Some(EffectValues(owners).string(&token)?))
}

pub(super) async fn invoke(
    store: &mut StoreContextMut<'_, InvocationState<PackState>>,
    host: &PackHost,
    cx: &CallCx,
    version: &str,
    operation: &str,
    params: &[Val],
    owners: &ValueOwners,
) -> Result<Vec<Val>> {
    let codec = EffectValues(owners);
    let access = host
        .shared
        .filesystem
        .get()
        .context("Core filesystem registry is unavailable")?;
    match operation {
        "open-grant" => {
            let prepared = access
                .registry()
                .prepare_open(cx, text(&params[0])?)
                .map_err(failure)?;
            let grant = host
                .shared
                .gate
                .authorize(
                    cx,
                    prepared.request().clone(),
                    prepared.args_digest().clone(),
                )
                .await?;
            // Admission belongs to this actual Store generation and active
            // Core frame. The accepted supervisor owns the original grant and
            // prepared open through consumption, authority narrowing, the
            // conversion decision and the same immutable terminal.
            let spawner = host.native_work_spawner(&store.data().data).await?;
            // Fund every post-authorization fallible admission while the
            // grant is still unconsumed; consumption runs inside the accepted
            // supervisor so caller abandonment cannot drop it mid-narrow.
            let preflight = HandoffPreflight::admit(cx, grant.id())?;
            let handoff = FsHandoff {
                cx,
                gate: host.shared.gate.clone(),
                spawner,
                preflight,
            };
            open_grant(store, version, prepared, grant, &codec, handoff).await
        }
        "[method]dir.read" | "[method]dir.list" | "[method]dir.stat" | "[method]dir.write"
        | "[method]dir.rename" | "[method]dir.remove" | "[method]dir.mkdir" => {
            let rep = resource_rep::<FsDirectory>(store, version, &params[0])?;
            let directory = get_resource::<FsDirectory>(store, version, rep)?.clone();
            let (call, output_kind) = match operation {
                "[method]dir.read" => (
                    FsCall::Read {
                        path: text(&params[1])?,
                        offset: uint(&params[2])?,
                        len: u32::try_from(uint(&params[3])?)?,
                    },
                    DirectoryOutput::Bytes,
                ),
                "[method]dir.list" => (
                    FsCall::List {
                        path: text(&params[1])?,
                    },
                    DirectoryOutput::Entries,
                ),
                "[method]dir.stat" => (
                    FsCall::Stat {
                        path: text(&params[1])?,
                    },
                    DirectoryOutput::Stat,
                ),
                "[method]dir.write" => {
                    let Val::Bool(append) = &params[3] else {
                        return Err(failure(FsError::Invalid(
                            "filesystem append flag is not boolean",
                        )));
                    };
                    (
                        FsCall::Write {
                            path: text(&params[1])?,
                            // The original invoking frame funds this owning
                            // input before any Gate grant can be consumed.
                            data: codec.copy_bytes(&params[2])?,
                            append: *append,
                        },
                        DirectoryOutput::Unit,
                    )
                }
                "[method]dir.rename" => (
                    FsCall::Rename {
                        from: text(&params[1])?,
                        to: text(&params[2])?,
                    },
                    DirectoryOutput::Unit,
                ),
                "[method]dir.remove" => (
                    FsCall::Remove {
                        path: text(&params[1])?,
                    },
                    DirectoryOutput::Unit,
                ),
                "[method]dir.mkdir" => (
                    FsCall::Mkdir {
                        path: text(&params[1])?,
                    },
                    DirectoryOutput::Unit,
                ),
                _ => unreachable!("filesystem directory operation was matched"),
            };
            let prepared = directory.prepare(cx, call).map_err(failure)?;
            let grant = host
                .shared
                .gate
                .authorize(
                    cx,
                    prepared.request().clone(),
                    prepared.args_digest().clone(),
                )
                .await?;
            let frame = store
                .data()
                .data
                .frame
                .as_ref()
                .context("filesystem operation lacks its actual provider frame")?;
            let output_cx = cx.clone().narrow_limits(grant.limits());
            let output = host.allocation_owners_for_frame(
                &output_cx,
                frame
                    .allocation_scope(tobkiri_gate::AllocationReason::OutputBuffer)
                    .map_err(anyhow::Error::new)?,
            )?;
            // Admission belongs to this actual Store generation and active
            // Core frame. Its pending mailbox covers the consuming await and
            // remains in that generation's census if handoff is abandoned.
            let spawner = host.native_work_spawner(&store.data().data).await?;
            // Fund every post-authorization fallible admission while the
            // grant is still unconsumed; consumption itself runs inside the
            // accepted supervisor, so abandonment cannot drop it mid-narrow.
            let preflight = HandoffPreflight::admit(cx, grant.id())?;
            let handoff = FsHandoff {
                cx,
                gate: host.shared.gate.clone(),
                spawner,
                preflight,
            };
            directory_call(
                prepared,
                grant,
                &EffectValues(&output),
                output.graph().clone(),
                output_kind,
                handoff,
            )
            .await
        }
        _ => Err(StepError::new(
            "unavailable",
            "this filesystem operation is not connected to the guest adapter",
        )
        .into()),
    }
}

/// One funded pre-consume handoff for a supervised filesystem operation.
/// Declared field order is the drop order: the admitted producer ID dies
/// before the shared Task charge is refunded.
struct HandoffPreflight {
    supervisor_id: GrantId,
    // Last: the single original Task reservation funds the handoff metadata
    // and is shared between the importer and supervisor halves.
    funding: Arc<ControlReservation>,
}

/// Already-held caller context and native handoff. Keep prepaid metadata last
/// so rejecting an unpolled caller drops its other captures before funding.
struct FsHandoff<'a> {
    cx: &'a CallCx,
    gate: Arc<dyn Gate>,
    spawner: NativeWorkSpawner,
    preflight: HandoffPreflight,
}

impl HandoffPreflight {
    /// Admit task control and the supervisor producer ID while the grant is
    /// still unconsumed; the accepted supervisor retains this funding when
    /// the importer future drops.
    fn admit(cx: &CallCx, id: &GrantId) -> Result<Self> {
        let reservation = cx.control().reserve(ControlKind::Task, 16 * 1024)?;
        let supervisor_id = id.try_clone_precharged()?;
        Ok(Self {
            supervisor_id,
            funding: Arc::new(reservation),
        })
    }
}

/// Actual native result and its delivery boundary. None is a lost native
/// completion receipt, never evidence that the operation left no changes.
struct IoOutput {
    result: std::result::Result<FsOutput, FsError>,
    output_bytes: u64,
    disposition: Option<FsDisposition>,
}
impl IoOutput {
    fn before_io(error: FsError) -> Self {
        Self {
            result: Err(error),
            output_bytes: 0,
            disposition: Some(FsDisposition::Clean),
        }
    }
}

fn fs_commit_unknown(error: &anyhow::Error) -> bool {
    matches!(
        error.downcast_ref::<FsError>(),
        Some(FsError::CommitUnknown)
    )
}

fn directory_outcome<T>(result: &Result<T>, cx: &CallCx) -> Outcome {
    if result.as_ref().err().is_some_and(fs_commit_unknown) {
        Outcome::Failed {
            code: "fs-commit-unknown".into(),
        }
    } else {
        super::effect_outcome(result, cx)
    }
}

fn io_delivery<T>(
    result: Result<T>,
    disposition: Option<FsDisposition>,
    kind: DirectoryOutput,
) -> Result<T> {
    if disposition == Some(FsDisposition::PartialUnknown)
        || (disposition.is_none() && matches!(kind, DirectoryOutput::Unit))
    {
        return Err(failure(FsError::CommitUnknown));
    }
    result.map_err(|error| {
        if disposition == Some(FsDisposition::Committed) {
            failure(FsError::CommitUnknown)
        } else {
            error
        }
    })
}
/// Supervised output payload for open: the owning directory clone or the
/// business error, never a borrowed completion.
type OpenOutput = std::result::Result<FsDirectory, FsError>;

/// The importer half of one handoff custody, generic over the supervised
/// payload. Declared field order is the drop order: retained funded entries
/// die before the shared reservation charge.
struct CallerCustody<O> {
    output_rx: oneshot::Receiver<O>,
    delivery_tx: oneshot::Sender<Outcome>,
    terminal_rx: oneshot::Receiver<std::result::Result<(), GateError>>,
    // Last: shared original Task funding outlives every entry it paid for.
    funding: Arc<ControlReservation>,
}

/// The accepted I/O supervisor's whole original custody, passed to the
/// future as one owned argument. An unpolled rejection still drops entries
/// in declaration order and refunds the shared funding only after every
/// payload entry is physically gone.
struct IoCustody {
    prepared: PreparedFsCall,
    grant: Grant,
    cx: CallCx,
    gate: Arc<dyn Gate>,
    budget: Arc<dyn AllocationBudget>,
    id: GrantId,
    output_tx: oneshot::Sender<IoOutput>,
    delivery_rx: oneshot::Receiver<Outcome>,
    terminal_tx: oneshot::Sender<std::result::Result<(), GateError>>,
    // Last: shared original Task funding outlives every entry it paid for.
    funding: Arc<ControlReservation>,
}

/// The accepted open supervisor's whole original custody, passed to the
/// native future as one owned argument under the same drop-order contract.
struct OpenCustody {
    prepared: PreparedFsOpen,
    grant: Grant,
    cx: CallCx,
    gate: Arc<dyn Gate>,
    id: GrantId,
    output_tx: oneshot::Sender<OpenOutput>,
    delivery_rx: oneshot::Receiver<Outcome>,
    terminal_tx: oneshot::Sender<std::result::Result<(), GateError>>,
    // Last: shared original Task funding outlives every entry it paid for.
    funding: Arc<ControlReservation>,
}

/// The consume-phase cleanup decision shared by both supervised operations.
enum CleanupOutcome {
    /// Exact settle ack: surface this preserved business error to the guest.
    Settled(FsError),
    /// Consume outcome unresolved: surface this error and stay unknown.
    Unresolved(FsError),
}

/// A consumed-but-not-dispatchable attempt can never execute. Its owned
/// `settle_failure` on the original gate is the only terminal path; only the
/// exact ack proof releases the retained attempt custody.
async fn settle_cleanup(cleanup: &CleanupOnlyConsumption, gate: &dyn Gate) -> CleanupOutcome {
    let business = cleanup_business(cleanup.error());
    match cleanup.settle_failure(gate).await {
        Ok(true) => CleanupOutcome::Settled(business),
        Ok(false) => CleanupOutcome::Unresolved(FsError::Unavailable),
        Err(error) => CleanupOutcome::Unresolved(FsError::Gate(error)),
    }
}

/// Bounded projection of a retained Gate error into the guest business error
/// while the original owner keeps the authority.
fn cleanup_business(error: &GateError) -> FsError {
    match error {
        GateError::Cancelled => FsError::Gate(GateError::Cancelled),
        GateError::Deadline => FsError::Gate(GateError::Deadline),
        GateError::Limit(_) => FsError::Gate(GateError::Limit("filesystem lease limit".into())),
        GateError::Invalid(_) => {
            FsError::Gate(GateError::Invalid("filesystem lease invalid".into()))
        }
        _ => FsError::Unavailable,
    }
}

/// An unresolved consumption can never execute or settle. Reconcile the
/// same-identity ack, then Drop transfers process ownership either way.
/// Returns the surfaced business error and whether the outcome resolved.
fn reconcile_unknown(unknown: &UnknownConsumption) -> (FsError, bool) {
    match unknown.reconcile() {
        Ok(true) => (FsError::Unavailable, true),
        Ok(false) => (FsError::Unavailable, false),
        Err(error) => (FsError::Gate(error), false),
    }
}

/// The accepted supervisor is the exclusive owner of the original prepared
/// operation, grant and consumed phase: consumption, authority narrowing,
/// native execution, delivery and the same immutable terminal all happen
/// inside it.
async fn supervise_io(custody: IoCustody) -> std::result::Result<(), NativeWorkError> {
    // Descriptive projection of the original prepared request only. Exact
    // authority, arguments and dispatch still come from its consumed grant.
    let mutating = matches!(
        custody.prepared.request(),
        tobkiri_gate::EffectRequest::Fs { operation, .. } if operation.is_write()
    );
    if let Err(error) = custody.gate.check_context(&custody.cx) {
        let _ = custody
            .output_tx
            .send(IoOutput::before_io(FsError::Gate(error)));
        let _ = custody.terminal_tx.send(Ok(()));
        return Ok(());
    }
    match custody
        .prepared
        .consume_owned(custody.grant, &custody.cx, custody.budget)
    {
        FsConsumption::NotConsumed(error) => {
            let _ = custody.output_tx.send(IoOutput::before_io(error));
            let _ = custody.terminal_tx.send(Ok(()));
            Ok(())
        }
        FsConsumption::Ready(mut dispatched) => {
            // The whole owned consumed state stays retained through every
            // fallible narrowing await and check, and through the same-ID
            // failed terminal whenever a check denies.
            if let Err(error) = dispatched.narrow(custody.gate.as_ref()).await {
                let _ = custody.output_tx.send(IoOutput::before_io(error));
                let result = custody
                    .gate
                    .record_outcome(
                        custody.id,
                        Outcome::Failed {
                            code: "fs-native-work".into(),
                        },
                    )
                    .await;
                let _ = custody.terminal_tx.send(result);
                drop(dispatched);
                return Ok(());
            }
            if let Err(error) = dispatched.check_lease() {
                let _ = custody.output_tx.send(IoOutput::before_io(error));
                let result = custody
                    .gate
                    .record_outcome(
                        custody.id,
                        Outcome::Failed {
                            code: "fs-native-work".into(),
                        },
                    )
                    .await;
                let _ = custody.terminal_tx.send(result);
                drop(dispatched);
                return Ok(());
            }
            let task_cx = dispatched.context().clone();
            let output_bytes = dispatched.context().limits().output_bytes;
            let work = dispatched.into_work(custody.gate.clone());
            let mut completion = match tokio::task::spawn_blocking(move || work.execute()).await {
                Ok(completion) => completion,
                Err(_) => {
                    let _ = custody.output_tx.send(IoOutput {
                        result: Err(FsError::Unavailable),
                        output_bytes: 0,
                        disposition: None,
                    });
                    let result = custody
                        .gate
                        .record_outcome(
                            custody.id,
                            Outcome::Failed {
                                code: if mutating {
                                    "fs-commit-unknown"
                                } else {
                                    "fs-native-work"
                                }
                                .into(),
                            },
                        )
                        .await;
                    let _ = custody.terminal_tx.send(result);
                    // Joining this supervisor is insufficient when the
                    // original native blocking task lost its completion
                    // receipt.
                    return Err(NativeWorkError::CompletionUnknown);
                }
            };
            let disposition = completion.disposition();
            let output = completion.take_result();
            let delivered = custody
                .output_tx
                .send(IoOutput {
                    result: output,
                    output_bytes,
                    disposition: Some(disposition),
                })
                .is_ok();
            let selected = if delivered {
                tokio::select! {
                    result = custody.delivery_rx => result.unwrap_or(Outcome::Cancelled),
                    _ = task_cx.cancel().cancelled() => Outcome::Cancelled,
                    _ = tokio::time::sleep_until(task_cx.deadline().into()) =>
                        Outcome::Failed { code: "deadline-failure-unknown".into() },
                }
            } else {
                Outcome::Cancelled
            };
            let selected =
                if disposition != FsDisposition::Clean && !matches!(selected, Outcome::Succeeded) {
                    Outcome::Failed {
                        code: "fs-commit-unknown".into(),
                    }
                } else {
                    selected
                };
            // The original receipt, FD, budget and input custody remain here
            // until physical completion AND terminal handling.
            let result = custody.gate.record_outcome(custody.id, selected).await;
            let _ = custody.terminal_tx.send(result);
            drop(completion);
            debug_assert!(custody.funding.bytes() == 16 * 1024);
            // Remaining custody entries now drop in declared order: funded
            // payload first, the shared Task charge last.
            // A failed operation/conversion/terminal is distinct from physical
            // uncertainty: the actual native join and custody destruction
            // above have completed. No parent PortCall terminal is consulted.
            Ok(())
        }
        FsConsumption::Cleanup(phase) => {
            let (error, resolved) =
                match settle_cleanup(&phase.attempt, custody.gate.as_ref()).await {
                    CleanupOutcome::Settled(error) => (error, true),
                    CleanupOutcome::Unresolved(error) => (error, false),
                };
            let _ = custody.output_tx.send(IoOutput::before_io(error));
            let _ = custody.terminal_tx.send(Ok(()));
            // Original prepared call, output budget and typed attempt stayed
            // retained through the settle above; they drop only now, in
            // declared field order.
            drop(phase);
            if resolved {
                Ok(())
            } else {
                Err(NativeWorkError::CompletionUnknown)
            }
        }
        FsConsumption::Unknown(phase) => {
            let (error, acked) = reconcile_unknown(&phase.attempt);
            let _ = custody.output_tx.send(IoOutput::before_io(error));
            let _ = custody.terminal_tx.send(Ok(()));
            drop(phase);
            if acked {
                Ok(())
            } else {
                Err(NativeWorkError::CompletionUnknown)
            }
        }
    }
}

/// The accepted open supervisor is the exclusive owner of the original
/// prepared open, grant and consumed phase through consumption, authority
/// narrowing, the conversion decision and the same immutable terminal;
/// resource conversion stays in the caller.
async fn supervise_open(custody: OpenCustody) -> std::result::Result<(), NativeWorkError> {
    if let Err(error) = custody.gate.check_context(&custody.cx) {
        let _ = custody.output_tx.send(Err(FsError::Gate(error)));
        let _ = custody.terminal_tx.send(Ok(()));
        return Ok(());
    }
    match custody.prepared.consume_owned(custody.grant, &custody.cx) {
        FsConsumption::NotConsumed(error) => {
            let _ = custody.output_tx.send(Err(error));
            let _ = custody.terminal_tx.send(Ok(()));
            Ok(())
        }
        FsConsumption::Ready(mut dispatched) => {
            if let Err(error) = dispatched.narrow(custody.gate.as_ref()).await {
                let _ = custody.output_tx.send(Err(error));
                let result = custody
                    .gate
                    .record_outcome(
                        custody.id,
                        Outcome::Failed {
                            code: "fs-native-work".into(),
                        },
                    )
                    .await;
                let _ = custody.terminal_tx.send(result);
                drop(dispatched);
                return Ok(());
            }
            let completion = dispatched.into_completion();
            // The original completion, FD, permit and context stay owned
            // here until the caller's conversion decision and the terminal.
            let task_cx = completion.context().clone();
            let delivered = custody
                .output_tx
                .send(Ok(completion.directory.clone()))
                .is_ok();
            let selected = if delivered {
                tokio::select! {
                    result = custody.delivery_rx => result.unwrap_or(Outcome::Cancelled),
                    _ = task_cx.cancel().cancelled() => Outcome::Cancelled,
                    _ = tokio::time::sleep_until(task_cx.deadline().into()) =>
                        Outcome::Failed { code: "deadline-failure-unknown".into() },
                }
            } else {
                Outcome::Cancelled
            };
            let result = custody.gate.record_outcome(custody.id, selected).await;
            let _ = custody.terminal_tx.send(result);
            drop(completion);
            debug_assert!(custody.funding.bytes() == 16 * 1024);
            Ok(())
        }
        FsConsumption::Cleanup(phase) => {
            let (error, resolved) =
                match settle_cleanup(&phase.attempt, custody.gate.as_ref()).await {
                    CleanupOutcome::Settled(error) => (error, true),
                    CleanupOutcome::Unresolved(error) => (error, false),
                };
            let _ = custody.output_tx.send(Err(error));
            let _ = custody.terminal_tx.send(Ok(()));
            drop(phase);
            if resolved {
                Ok(())
            } else {
                Err(NativeWorkError::CompletionUnknown)
            }
        }
        FsConsumption::Unknown(phase) => {
            let (error, acked) = reconcile_unknown(&phase.attempt);
            let _ = custody.output_tx.send(Err(error));
            let _ = custody.terminal_tx.send(Ok(()));
            drop(phase);
            if acked {
                Ok(())
            } else {
                Err(NativeWorkError::CompletionUnknown)
            }
        }
    }
}

async fn directory_call(
    prepared: PreparedFsCall,
    grant: Grant,
    codec: &EffectValues<'_>,
    budget: Arc<dyn AllocationBudget>,
    output_kind: DirectoryOutput,
    handoff: FsHandoff<'_>,
) -> Result<Vec<Val>> {
    let FsHandoff {
        cx,
        gate,
        spawner,
        preflight,
    } = handoff;
    let HandoffPreflight {
        supervisor_id,
        funding,
    } = preflight;
    let (output_tx, output_rx) = oneshot::channel();
    let (delivery_tx, delivery_rx) = oneshot::channel();
    let (terminal_tx, terminal_rx) = oneshot::channel();
    // Split the funded pre-consume custody between the two ownership
    // branches. Both keep a shared Arc to the SAME original Task reservation.
    let caller = CallerCustody {
        output_rx,
        delivery_tx,
        terminal_rx,
        funding,
    };
    let custody = IoCustody {
        prepared,
        grant,
        cx: cx.clone(),
        gate,
        budget,
        id: supervisor_id,
        output_tx,
        delivery_rx,
        terminal_tx,
        funding: caller.funding.clone(),
    };
    // A close remembered during consumption also reaches this exact caller
    // token; the supervised lease shares the same cancellation ancestry.
    // This requests cancellation, never completion.
    spawner.bind_cancel(cx.cancel().clone())?;
    // A rejected submission was never accepted: the rejected future already
    // dropped the still-unconsumed grant outside every guard through the
    // known abandonment path, so no terminal is published here.
    spawner.spawn(supervise_io(custody))?;
    // Accepted: the supervisor is the exclusive producer for consumption,
    // delivery, cancellation and every error until the same-ID terminal.
    let IoOutput {
        result,
        output_bytes,
        disposition,
    } = caller.output_rx.await.unwrap_or(IoOutput {
        result: Err(FsError::Unavailable),
        output_bytes: 0,
        disposition: None,
    });
    let result = result
        .map_err(failure)
        .and_then(|output| {
            convert_output(output, output_kind, codec, output_bytes).map_err(conversion_error)
        })
        .and_then(|value| {
            cx.check_active()?;
            Ok(value)
        });
    let result = io_delivery(result, disposition, output_kind);
    let _ = caller.delivery_tx.send(directory_outcome(&result, cx));
    let terminal = caller
        .terminal_rx
        .await
        .context("filesystem terminal supervisor unavailable")
        .and_then(|result| result.map_err(anyhow::Error::new));
    // Delivery failure never changes/reproposes the supervisor's same-ID
    // immutable winner; a completed mutation instead stays no-replay.
    io_delivery(terminal, disposition, output_kind)?;
    io_delivery(
        result.and_then(|value| {
            cx.check_active()?;
            Ok(value)
        }),
        disposition,
        output_kind,
    )
}

/// The open caller owns Store resource conversion; the accepted supervisor
/// owns consumption, the original completion and the same-ID terminal.
async fn open_grant(
    store: &mut StoreContextMut<'_, InvocationState<PackState>>,
    version: &str,
    prepared: PreparedFsOpen,
    grant: Grant,
    codec: &EffectValues<'_>,
    handoff: FsHandoff<'_>,
) -> Result<Vec<Val>> {
    let FsHandoff {
        cx,
        gate,
        spawner,
        preflight,
    } = handoff;
    let HandoffPreflight {
        supervisor_id,
        funding,
    } = preflight;
    let (output_tx, output_rx) = oneshot::channel();
    let (delivery_tx, delivery_rx) = oneshot::channel();
    let (terminal_tx, terminal_rx) = oneshot::channel();
    let caller = CallerCustody {
        output_rx,
        delivery_tx,
        terminal_rx,
        funding,
    };
    let custody = OpenCustody {
        prepared,
        grant,
        cx: cx.clone(),
        gate,
        id: supervisor_id,
        output_tx,
        delivery_rx,
        terminal_tx,
        funding: caller.funding.clone(),
    };
    // A close remembered during consumption also reaches this exact caller
    // token; the supervised lease shares the same cancellation ancestry.
    spawner.bind_cancel(cx.cancel().clone())?;
    spawner.spawn(supervise_open(custody))?;
    // The original completion stays owned by the supervisor until this
    // Store-side conversion decision reaches the same immutable terminal.
    let result = match caller.output_rx.await {
        Ok(Ok(directory)) => (|| {
            cx.check_active()?;
            codec.ok(Some(push(store, version, directory)?))
        })(),
        Ok(Err(error)) => Err(failure(error)),
        Err(_) => Err(StepError::new("unavailable", "filesystem open unavailable").into()),
    };
    let _ = caller.delivery_tx.send(super::effect_outcome(&result, cx));
    caller
        .terminal_rx
        .await
        .context("filesystem terminal supervisor unavailable")??;
    result
}

fn limit() -> anyhow::Error {
    StepError::new("limit-exceeded", "filesystem output limit exceeded").into()
}
fn malformed() -> anyhow::Error {
    StepError::new("internal", "unexpected filesystem output").into()
}
fn conversion_error(error: anyhow::Error) -> anyhow::Error {
    let limited = matches!(
        error.downcast_ref::<HostAllocationError>(),
        Some(
            HostAllocationError::Limit | HostAllocationError::Overflow | HostAllocationError::Depth
        )
    ) || matches!(
        error.downcast_ref::<AllocationError>(),
        Some(AllocationError::Limit | AllocationError::Overflow | AllocationError::Depth)
    );
    if limited { limit() } else { error }
}

fn checked_name(name: &str, output_bytes: u64) -> Result<usize> {
    let bytes = name.len().checked_add(32).ok_or_else(limit)?;
    if name.len() > MAX_ENTRY_NAME || name.contains('\0') || bytes as u64 > output_bytes {
        return Err(limit());
    }
    Ok(bytes)
}

fn entry_parts(entry: &CoreValue) -> Result<(CoreValue, bool, u64)> {
    if entry.object().is_none_or(|fields| fields.len() != 3) {
        return Err(malformed());
    }
    let name = entry.get_key("name").ok_or_else(malformed)?;
    if name.as_str().is_none() {
        return Err(malformed());
    }
    let is_dir = entry
        .get_key("is-dir")
        .and_then(|value| value.as_bool())
        .ok_or_else(malformed)?;
    let size = entry.get_key("size").ok_or_else(malformed)?;
    let size = size.as_str().ok_or_else(malformed)?;
    if size.is_empty()
        || size.len() > 20
        || !size.bytes().all(|byte| byte.is_ascii_digit())
        || (size.len() > 1 && size.starts_with('0'))
    {
        return Err(malformed());
    }
    let size = size.parse::<u64>().map_err(|_| malformed())?;
    Ok((name, is_dir, size))
}

fn entry_value(codec: &EffectValues<'_>, name: &str, is_dir: bool, size: u64) -> Result<Val> {
    codec.record(
        [
            ("name", codec.string(name)?),
            ("is-dir", Val::Bool(is_dir)),
            ("size", Val::U64(size)),
        ]
        .into_iter(),
    )
}

fn convert_output(
    output: FsOutput,
    kind: DirectoryOutput,
    codec: &EffectValues<'_>,
    output_bytes: u64,
) -> Result<Vec<Val>> {
    match (kind, output) {
        (DirectoryOutput::Bytes, FsOutput::Bytes(bytes)) => {
            codec.ok(Some(codec.bytes(bytes.as_bytes())?))
        }
        (DirectoryOutput::Entries, FsOutput::Entries(entries)) => {
            let entries = entries.array().ok_or_else(malformed)?;
            if entries.len() > MAX_LIST_ENTRIES {
                return Err(limit());
            }
            // Prewalk the complete retained native result before constructing
            // its ABI copy. Aliases are counted per entry, never by identity.
            let mut bytes = 0usize;
            for index in 0..entries.len() {
                let entry = entries.get(index).ok_or_else(malformed)?;
                let (name, _, _) = entry_parts(&entry)?;
                bytes = bytes
                    .checked_add(checked_name(
                        name.as_str().ok_or_else(malformed)?,
                        output_bytes,
                    )?)
                    .ok_or_else(limit)?;
                if bytes > MAX_LIST_BYTES || bytes as u64 > output_bytes {
                    return Err(limit());
                }
            }
            let mut values = BudgetedVecBuilder::new(entries.len(), codec.0.component(), 0)?;
            for index in 0..entries.len() {
                let entry = entries.get(index).ok_or_else(malformed)?;
                let (name, is_dir, size) = entry_parts(&entry)?;
                values.push(entry_value(
                    codec,
                    name.as_str().ok_or_else(malformed)?,
                    is_dir,
                    size,
                )?)?;
            }
            codec.ok(Some(Val::List(values.finish()?)))
        }
        (DirectoryOutput::Stat, FsOutput::Stat(stat)) => {
            let name = stat.name.as_str().ok_or_else(malformed)?;
            checked_name(name, output_bytes)?;
            // The exact frozen WIT entry has these three fields. Native
            // modification time is not part of either Core FS interface.
            codec.ok(Some(entry_value(
                codec,
                name,
                stat.is_directory,
                stat.size,
            )?))
        }
        (DirectoryOutput::Unit, FsOutput::Unit) => codec.ok(None),
        _ => Err(malformed()),
    }
}

#[cfg(all(test, unix))]
mod consumed_supervisor_tests {
    // These exercise the accepted supervisor's local ownership boundary using
    // the real Gate/Store/descriptor backend. Direct Tokio submission does not
    // stand in for a Host NativeWorkSpawner generation/census admission proof.
    use super::*;
    use crate::filesystem::{FsRights, FsRootIdentity};
    use async_trait::async_trait;
    use std::{
        fs::{self, File},
        future::Future,
        path::PathBuf,
        sync::atomic::{AtomicU64, AtomicUsize, Ordering},
        time::{Duration, Instant},
    };
    use tobkiri_canon::{AllocationRequest, CoreBuffer, Digest, ids::*};
    use tobkiri_gate::{
        AcknowledgedTerminal, ApprovalAnswer, ApprovalPrompt, ApprovalStore, CallCx,
        CleanupSnapshot, CleanupSnapshotAttempt, CoreCancellation, CoreControl, DispatchPermit,
        EffectRequest, FsPermission, GateEngine, MediatorChain, PackPolicy, PermissionSet,
        Principal, ResourceLimits, TerminalDelivery, TrustedPrompt,
    };
    use tobkiri_store::{Approvals, AuditLog, AuditQuota, DataRoot};
    use tokio::{sync::Notify, task::JoinHandle};

    const PAYLOAD: &[u8] = b"actual native read";

    struct Approve {
        root: String,
        write: bool,
    }

    #[async_trait]
    impl TrustedPrompt for Approve {
        async fn prompt_approval(&self, prompt: ApprovalPrompt) -> ApprovalAnswer {
            if prompt.profile.as_str() != "prof_fs_supervisor"
                || prompt.request.as_str() != "req_fs_supervisor"
            {
                return ApprovalAnswer::Deny;
            }
            match (&prompt.principal, &prompt.effect) {
                (Principal::User, EffectRequest::Permissions { pack, hash })
                    if pack.as_str() == "local.fs-supervisor"
                        && hash.digest() == &Digest::hash(b"native-fs-supervisor-test") =>
                {
                    ApprovalAnswer::Approve
                }
                (
                    Principal::Pack { pack, hash },
                    EffectRequest::Fs {
                        root,
                        path,
                        operation: tobkiri_gate::FsOperation::Write,
                    },
                ) if self.write
                    && root == &self.root
                    && path == "file"
                    && pack.as_str() == "local.fs-supervisor"
                    && hash.digest() == &Digest::hash(b"native-fs-supervisor-test") =>
                {
                    // Mutation needs its ordinary per-effect trusted prompt
                    // even after the Pack permissions approval. Accept only
                    // this fixture's real registered root and append target.
                    ApprovalAnswer::Approve
                }
                _ => ApprovalAnswer::Deny,
            }
        }
    }

    #[derive(Default)]
    struct OutputBudget {
        live: AtomicU64,
        reservations: AtomicUsize,
    }

    impl AllocationBudget for OutputBudget {
        fn reserve(&self, request: AllocationRequest) -> Result<(), AllocationError> {
            self.live
                .fetch_update(Ordering::AcqRel, Ordering::Acquire, |live| {
                    live.checked_add(request.bytes)
                        .filter(|next| *next <= 64 * 1024)
                })
                .map_err(|_| AllocationError::Limit)?;
            self.reservations.fetch_add(1, Ordering::AcqRel);
            Ok(())
        }

        fn release(&self, bytes: u64) {
            self.live.fetch_sub(bytes, Ordering::AcqRel);
        }
    }

    struct Fixture {
        _temporary: tempfile::TempDir,
        data: DataRoot,
        workspace: PathBuf,
        profile: ProfileId,
        control: CoreControl,
        approvals: Arc<Approvals>,
        gate: Arc<GateEngine>,
        registry: FsRegistry,
        root: FsRootIdentity,
        cx: CallCx,
        budget: Arc<OutputBudget>,
        write: bool,
    }

    impl Fixture {
        async fn new() -> Self {
            Self::with_write(false).await
        }

        async fn with_write(write: bool) -> Self {
            let temporary = tempfile::tempdir().unwrap();
            let base = temporary.path().canonicalize().unwrap();
            let workspace = base.join("workspace");
            fs::create_dir(&workspace).unwrap();
            fs::write(workspace.join("file"), PAYLOAD).unwrap();
            let data = DataRoot::open(base.join("authority")).unwrap();
            let control = CoreControl::default();
            let registry = FsRegistry::new(control.clone()).unwrap();
            registry
                .protect_root(data.filesystem_protection_handle().unwrap())
                .unwrap();
            let rights = FsRights { read: true, write };
            let root = registry
                .register_root(File::open(&workspace).unwrap(), rights)
                .unwrap();
            let profile = ProfileId::parse("prof_fs_supervisor").unwrap();
            let approvals = Arc::new(Approvals::open(&data, &profile).unwrap());
            let audit = Arc::new(AuditLog::open(&data, &profile, AuditQuota::default()).unwrap());
            let gate = Arc::new(GateEngine::new_with_control(
                approvals.clone(),
                audit,
                Arc::new(Approve {
                    root: root.id().into(),
                    write,
                }),
                MediatorChain::default(),
                control.clone(),
            ));
            let pack = PackId::parse("local.fs-supervisor").unwrap();
            let hash = PackHash::new(Digest::hash(b"native-fs-supervisor-test"));
            let limits = ResourceLimits {
                fuel_per_call: 1_000_000,
                memory_mib: 64,
                wall_ms: 30_000,
                table_elements: 16_384,
                output_bytes: 4_096,
            };
            let permissions = PermissionSet {
                fs: vec![FsPermission {
                    root: root.id().into(),
                    read: true,
                    write,
                }],
                wasm: limits.clone(),
                ..PermissionSet::default()
            };
            gate.register_pack(
                &profile,
                &pack,
                &hash,
                PackPolicy {
                    declaration: permissions.clone(),
                    ceiling: permissions.clone(),
                    pack_limits: permissions,
                },
            )
            .unwrap();
            let revision = Digest::hash(b"native-fs-supervisor-revision");
            gate.register_revision(
                &profile,
                &revision,
                std::collections::BTreeMap::from([((pack.clone(), hash.clone()), limits.clone())]),
            )
            .unwrap();
            let context = |principal| {
                CallCx::try_root_with_control(
                    profile.clone(),
                    principal,
                    RequestId::parse("req_fs_supervisor").unwrap(),
                    CoreCancellation::new_with_control(&control).unwrap(),
                    Instant::now() + Duration::from_secs(30),
                    limits.clone(),
                    control.clone(),
                )
                .unwrap()
                .with_revision(revision.clone())
            };
            gate.approve_pack_permissions(&context(Principal::User), &pack, None, &hash)
                .await
                .unwrap();
            let cx = context(Principal::Pack { pack, hash });
            Self {
                _temporary: temporary,
                data,
                workspace,
                profile,
                control,
                approvals,
                gate,
                registry,
                root,
                cx,
                budget: Arc::new(OutputBudget::default()),
                write,
            }
        }

        async fn prepared_open(&self) -> (PreparedFsOpen, Grant) {
            let token = self
                .registry
                .issue_open_token(
                    &self.cx,
                    &self.root,
                    FsRights {
                        read: true,
                        write: self.write,
                    },
                    Duration::from_secs(5),
                )
                .unwrap();
            let prepared = self.registry.prepare_open(&self.cx, &token).unwrap();
            let grant = self
                .gate
                .authorize(
                    &self.cx,
                    prepared.request().clone(),
                    prepared.args_digest().clone(),
                )
                .await
                .unwrap();
            (prepared, grant)
        }

        async fn prepared_read(&self) -> (PreparedFsCall, Grant) {
            self.prepared_call(FsCall::Read {
                path: "file",
                offset: 0,
                len: PAYLOAD.len() as u32,
            })
            .await
        }

        async fn prepared_call(&self, call: FsCall<'_>) -> (PreparedFsCall, Grant) {
            let (open, grant) = self.prepared_open().await;
            let open_id = grant.id().clone();
            let completion = open
                .consume(grant, &self.cx, self.gate.clone())
                .await
                .unwrap();
            let directory = completion.directory.clone();
            self.gate
                .record_outcome(open_id, Outcome::Succeeded)
                .await
                .unwrap();
            drop(completion);
            let prepared = directory.prepare(&self.cx, call).unwrap();
            let grant = self
                .gate
                .authorize(
                    &self.cx,
                    prepared.request().clone(),
                    prepared.args_digest().clone(),
                )
                .await
                .unwrap();
            (prepared, grant)
        }

        fn task_count(&self) -> u64 {
            self.control.usage().count(ControlKind::Task)
        }

        fn assert_consumed_without_terminal(&self, id: &GrantId) {
            assert_eq!(
                self.approvals.reservation_state(id).unwrap().as_deref(),
                Some("consumed")
            );
            assert!(self.approvals.terminal_delivery(id).unwrap().is_none());
        }

        fn assert_terminal_once(&self, id: &GrantId, expected: Outcome) {
            self.assert_terminal_record_once(id, expected);
            assert_eq!(fs::read(self.workspace.join("file")).unwrap(), PAYLOAD);
        }

        fn assert_terminal_record_once(&self, id: &GrantId, expected: Outcome) {
            assert_eq!(
                self.approvals.reservation_state(id).unwrap().as_deref(),
                Some("finished")
            );
            let terminal = self.approvals.terminal_delivery(id).unwrap().unwrap();
            assert!(terminal.acknowledged);
            assert_eq!(&terminal.decision.reservation.id, id);
            assert_eq!(terminal.decision.outcome, expected);
            let approvals = rusqlite::Connection::open_with_flags(
                self.data
                    .path()
                    .join(DataRoot::profile_relative(&self.profile, "approvals.sqlite").unwrap()),
                rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
            )
            .unwrap();
            let decisions: i64 = approvals
                .query_row(
                    "SELECT count(*) FROM terminal_outbox WHERE grant_id=?1",
                    [id.as_str()],
                    |row| row.get(0),
                )
                .unwrap();
            assert_eq!(decisions, 1);
            let audit = rusqlite::Connection::open_with_flags(
                self.data
                    .path()
                    .join(DataRoot::profile_relative(&self.profile, "audit.sqlite").unwrap()),
                rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
            )
            .unwrap();
            let deliveries: i64 = audit
                .query_row(
                    "SELECT count(*) FROM audit WHERE event_id=?1",
                    [terminal.decision.event_id.as_str()],
                    |row| row.get(0),
                )
                .unwrap();
            assert_eq!(deliveries, 1);
        }

        async fn start_io(&self, fail: bool) -> Running<IoOutput> {
            let (prepared, grant) = self.prepared_read().await;
            self.start_prepared_io(prepared, grant, fail, false)
        }

        async fn start_write(&self, fail_terminal: bool) -> Running<IoOutput> {
            let data = CoreBuffer::copy_from(
                b"-appended-once",
                &(self.budget.clone() as Arc<dyn AllocationBudget>),
            )
            .unwrap();
            let (prepared, grant) = self
                .prepared_call(FsCall::Write {
                    path: "file",
                    data,
                    append: true,
                })
                .await;
            self.start_prepared_io(prepared, grant, false, fail_terminal)
        }

        fn start_prepared_io(
            &self,
            prepared: PreparedFsCall,
            grant: Grant,
            fail: bool,
            fail_terminal: bool,
        ) -> Running<IoOutput> {
            let id = grant.id().clone();
            assert_eq!(
                self.approvals.reservation_state(&id).unwrap().as_deref(),
                Some("dispatched")
            );
            let mut gate = NarrowGate::new(self, id.clone(), fail);
            gate.fail_terminal = fail_terminal;
            let gate = Arc::new(gate);
            let preflight = HandoffPreflight::admit(&self.cx, grant.id()).unwrap();
            let (output_tx, output_rx) = oneshot::channel();
            let (delivery_tx, delivery_rx) = oneshot::channel();
            let (terminal_tx, terminal_rx) = oneshot::channel();
            let caller = CallerCustody {
                output_rx,
                delivery_tx,
                terminal_rx,
                funding: preflight.funding,
            };
            let custody = IoCustody {
                prepared,
                grant,
                cx: self.cx.clone(),
                gate: gate.clone(),
                budget: self.budget.clone(),
                id: preflight.supervisor_id,
                output_tx,
                delivery_rx,
                terminal_tx,
                funding: caller.funding.clone(),
            };
            let join = tokio::spawn(supervise_io(custody));
            Running {
                id,
                gate,
                caller,
                join,
            }
        }

        async fn start_open(&self, fail: bool) -> Running<OpenOutput> {
            let (prepared, grant) = self.prepared_open().await;
            let id = grant.id().clone();
            assert_eq!(
                self.approvals.reservation_state(&id).unwrap().as_deref(),
                Some("dispatched")
            );
            let gate = Arc::new(NarrowGate::new(self, id.clone(), fail));
            let preflight = HandoffPreflight::admit(&self.cx, grant.id()).unwrap();
            let (output_tx, output_rx) = oneshot::channel();
            let (delivery_tx, delivery_rx) = oneshot::channel();
            let (terminal_tx, terminal_rx) = oneshot::channel();
            let caller = CallerCustody {
                output_rx,
                delivery_tx,
                terminal_rx,
                funding: preflight.funding,
            };
            let custody = OpenCustody {
                prepared,
                grant,
                cx: self.cx.clone(),
                gate: gate.clone(),
                id: preflight.supervisor_id,
                output_tx,
                delivery_rx,
                terminal_tx,
                funding: caller.funding.clone(),
            };
            let join = tokio::spawn(supervise_open(custody));
            Running {
                id,
                gate,
                caller,
                join,
            }
        }
    }

    struct Running<O> {
        id: GrantId,
        gate: Arc<NarrowGate>,
        caller: CallerCustody<O>,
        join: JoinHandle<std::result::Result<(), NativeWorkError>>,
    }

    struct NarrowGate {
        actual: Arc<GateEngine>,
        approvals: Arc<Approvals>,
        expected: GrantId,
        fail: bool,
        fail_terminal: bool,
        narrowed: Notify,
        release: Notify,
        context_checks: AtomicUsize,
        narrow_calls: AtomicUsize,
        outcomes: AtomicUsize,
    }

    impl NarrowGate {
        fn new(fixture: &Fixture, expected: GrantId, fail: bool) -> Self {
            Self {
                actual: fixture.gate.clone(),
                approvals: fixture.approvals.clone(),
                expected,
                fail,
                fail_terminal: false,
                narrowed: Notify::new(),
                release: Notify::new(),
                context_checks: AtomicUsize::new(0),
                narrow_calls: AtomicUsize::new(0),
                outcomes: AtomicUsize::new(0),
            }
        }
    }

    #[async_trait]
    impl Gate for NarrowGate {
        fn check_context(&self, cx: &CallCx) -> std::result::Result<(), GateError> {
            self.context_checks.fetch_add(1, Ordering::AcqRel);
            self.actual.check_context(cx)
        }

        fn acknowledged_terminal(
            &self,
            permit: &DispatchPermit,
        ) -> std::result::Result<AcknowledgedTerminal, GateError> {
            self.actual.acknowledged_terminal(permit)
        }

        fn with_cleanup_snapshot(
            &self,
            permit: &DispatchPermit,
            inspect: &mut dyn FnMut(CleanupSnapshot<'_>) -> std::result::Result<(), GateError>,
        ) -> std::result::Result<(), GateError> {
            self.actual.with_cleanup_snapshot(permit, inspect)
        }

        fn try_cleanup_snapshot(
            &self,
            permit: &DispatchPermit,
            inspect: &mut dyn FnMut(CleanupSnapshot<'_>) -> std::result::Result<(), GateError>,
        ) -> CleanupSnapshotAttempt {
            self.actual.try_cleanup_snapshot(permit, inspect)
        }

        async fn authorize(
            &self,
            cx: &CallCx,
            request: EffectRequest,
            args: Digest,
        ) -> std::result::Result<Grant, GateError> {
            self.actual.authorize(cx, request, args).await
        }

        fn watch_lease(
            &self,
            cx: &CallCx,
            permit: &DispatchPermit,
        ) -> std::result::Result<(), GateError> {
            self.actual.watch_lease(cx, permit)
        }

        async fn narrow_lease(
            &self,
            permit: &mut DispatchPermit,
            cx: &CallCx,
        ) -> std::result::Result<(), GateError> {
            self.actual.narrow_lease(permit, cx).await?;
            assert_eq!(permit.id(), &self.expected);
            assert!(permit.matches_context(cx));
            assert_eq!(
                self.approvals
                    .reservation_state(permit.id())
                    .unwrap()
                    .as_deref(),
                Some("consumed")
            );
            assert_eq!(self.narrow_calls.fetch_add(1, Ordering::AcqRel), 0);
            self.narrowed.notify_one();
            self.release.notified().await;
            if self.fail {
                Err(GateError::Unavailable)
            } else {
                Ok(())
            }
        }

        async fn record_outcome(
            &self,
            grant: GrantId,
            outcome: Outcome,
        ) -> std::result::Result<(), GateError> {
            assert_eq!(grant, self.expected);
            assert_eq!(self.outcomes.fetch_add(1, Ordering::AcqRel), 0);
            self.actual.record_outcome(grant, outcome).await?;
            if self.fail_terminal {
                // The exact immutable decision and audit ACK already exist;
                // only the importing caller loses this delivery response.
                Err(GateError::Unavailable)
            } else {
                Ok(())
            }
        }

        async fn settle_deadline(
            &self,
            permit: &DispatchPermit,
        ) -> std::result::Result<TerminalDelivery, GateError> {
            self.actual.settle_deadline(permit).await
        }

        async fn record_denial(
            &self,
            cx: &CallCx,
            kind: &'static str,
            code: &'static str,
        ) -> std::result::Result<(), GateError> {
            self.actual.record_denial(cx, kind, code).await
        }
    }

    async fn bounded<T>(future: impl Future<Output = T>) -> T {
        tokio::time::timeout(Duration::from_secs(5), future)
            .await
            .expect("original filesystem supervisor handshake timed out")
    }

    #[tokio::test]
    async fn io_post_consume_narrow_failure_keeps_same_terminal_and_caller_funding() {
        let fixture = Fixture::new().await;
        let baseline = fixture.task_count();
        let Running {
            id,
            gate,
            mut caller,
            join,
        } = fixture.start_io(true).await;
        bounded(gate.narrowed.notified()).await;
        fixture.assert_consumed_without_terminal(&id);
        // PreparedFsCall retains its own 64KiB Task reservation in addition
        // to the 16KiB handoff shared by this caller and its supervisor.
        assert_eq!(fixture.task_count(), baseline + 2);
        assert_eq!(fixture.budget.reservations.load(Ordering::Acquire), 0);
        gate.release.notify_one();
        let IoOutput {
            result: output,
            output_bytes: bytes,
            disposition,
        } = bounded(&mut caller.output_rx).await.unwrap();
        assert!(matches!(output, Err(FsError::Gate(GateError::Unavailable))));
        assert_eq!(bytes, 0);
        assert_eq!(disposition, Some(FsDisposition::Clean));
        bounded(&mut caller.terminal_rx).await.unwrap().unwrap();
        bounded(join).await.unwrap().unwrap();
        fixture.cx.check_active().unwrap();
        assert_eq!(gate.context_checks.load(Ordering::Acquire), 1);
        assert_eq!(fixture.budget.reservations.load(Ordering::Acquire), 0);
        assert_eq!(fixture.budget.live.load(Ordering::Acquire), 0);
        fixture.assert_terminal_once(
            &id,
            Outcome::Failed {
                code: "fs-native-work".into(),
            },
        );
        // The actual supervisor Join has ended; its original shared handoff
        // charge still belongs to the retained importer half.
        assert_eq!(fixture.task_count(), baseline + 1);
        drop(caller);
        assert_eq!(fixture.task_count(), baseline);
    }

    #[tokio::test]
    async fn open_post_consume_narrow_failure_keeps_same_terminal_and_caller_funding() {
        let fixture = Fixture::new().await;
        let baseline = fixture.task_count();
        let Running {
            id,
            gate,
            mut caller,
            join,
        } = fixture.start_open(true).await;
        bounded(gate.narrowed.notified()).await;
        fixture.assert_consumed_without_terminal(&id);
        assert_eq!(fixture.task_count(), baseline + 1);
        gate.release.notify_one();
        assert!(matches!(
            bounded(&mut caller.output_rx).await.unwrap(),
            Err(FsError::Gate(GateError::Unavailable))
        ));
        bounded(&mut caller.terminal_rx).await.unwrap().unwrap();
        bounded(join).await.unwrap().unwrap();
        fixture.cx.check_active().unwrap();
        assert_eq!(gate.context_checks.load(Ordering::Acquire), 1);
        fixture.assert_terminal_once(
            &id,
            Outcome::Failed {
                code: "fs-native-work".into(),
            },
        );
        assert_eq!(fixture.task_count(), baseline + 1);
        drop(caller);
        assert_eq!(fixture.task_count(), baseline);
    }

    #[tokio::test]
    async fn io_importer_abandonment_during_narrow_keeps_original_work_and_funding() {
        let fixture = Fixture::new().await;
        let baseline = fixture.task_count();
        let Running {
            id,
            gate,
            caller,
            join,
        } = fixture.start_io(false).await;
        bounded(gate.narrowed.notified()).await;
        fixture.assert_consumed_without_terminal(&id);
        assert_eq!(fixture.budget.reservations.load(Ordering::Acquire), 0);
        drop(caller);
        // No caller remains to poll narrow or retain any output/completion.
        // Only this SAME accepted supervisor still owns the original grant.
        fixture.assert_consumed_without_terminal(&id);
        // The supervisor still owns BOTH the prepared call's 64KiB Task and
        // its half of the shared 16KiB handoff after the caller is gone.
        assert_eq!(fixture.task_count(), baseline + 2);
        assert!(!join.is_finished());
        gate.release.notify_one();
        bounded(join).await.unwrap().unwrap();
        assert!(fixture.budget.reservations.load(Ordering::Acquire) > 0);
        assert_eq!(fixture.budget.live.load(Ordering::Acquire), 0);
        fixture.assert_terminal_once(&id, Outcome::Cancelled);
        assert_eq!(gate.narrow_calls.load(Ordering::Acquire), 1);
        assert_eq!(gate.outcomes.load(Ordering::Acquire), 1);
        assert_eq!(fixture.task_count(), baseline);
    }

    #[tokio::test]
    async fn open_importer_abandonment_during_narrow_keeps_original_completion_and_funding() {
        let fixture = Fixture::new().await;
        let baseline = fixture.task_count();
        let Running {
            id,
            gate,
            caller,
            join,
        } = fixture.start_open(false).await;
        bounded(gate.narrowed.notified()).await;
        fixture.assert_consumed_without_terminal(&id);
        drop(caller);
        fixture.assert_consumed_without_terminal(&id);
        assert_eq!(fixture.task_count(), baseline + 1);
        assert!(!join.is_finished());
        gate.release.notify_one();
        bounded(join).await.unwrap().unwrap();
        fixture.assert_terminal_once(&id, Outcome::Cancelled);
        assert_eq!(gate.narrow_calls.load(Ordering::Acquire), 1);
        assert_eq!(gate.outcomes.load(Ordering::Acquire), 1);
        assert_eq!(fixture.task_count(), baseline);
    }

    #[tokio::test]
    async fn committed_append_importer_abandonment_is_no_replay_and_keeps_one_terminal() {
        let fixture = Fixture::with_write(true).await;
        let baseline = fixture.task_count();
        let Running {
            id,
            gate,
            mut caller,
            join,
        } = fixture.start_write(false).await;
        bounded(gate.narrowed.notified()).await;
        fixture.assert_consumed_without_terminal(&id);
        assert_eq!(fixture.task_count(), baseline + 2);
        gate.release.notify_one();
        let output = bounded(&mut caller.output_rx).await.unwrap();
        assert!(matches!(output.result, Ok(FsOutput::Unit)));
        assert_eq!(output.disposition, Some(FsDisposition::Committed));
        fixture.assert_consumed_without_terminal(&id);
        // This is actual native completion, not a cancellation notification:
        // both the original prepared input and shared handoff are still paid.
        assert_eq!(fixture.task_count(), baseline + 2);
        assert!(fixture.budget.live.load(Ordering::Acquire) > 0);
        let cancelled = io_delivery::<()>(
            Err(GateError::Cancelled.into()),
            output.disposition,
            DirectoryOutput::Unit,
        )
        .unwrap_err();
        assert!(fs_commit_unknown(&cancelled));
        // Abandon only the importer. The original supervisor observes its
        // same delivery receiver and proposes its sole terminal; no replay.
        drop(caller);
        bounded(join).await.unwrap().unwrap();
        fixture.assert_terminal_record_once(
            &id,
            Outcome::Failed {
                code: "fs-commit-unknown".into(),
            },
        );
        assert_eq!(gate.narrow_calls.load(Ordering::Acquire), 1);
        assert_eq!(gate.outcomes.load(Ordering::Acquire), 1);
        assert_eq!(
            fs::read(fixture.workspace.join("file")).unwrap(),
            [PAYLOAD, b"-appended-once"].concat()
        );
        assert_eq!(fixture.budget.live.load(Ordering::Acquire), 0);
        assert_eq!(fixture.task_count(), baseline);
    }

    #[tokio::test]
    async fn committed_append_terminal_response_failure_preserves_succeeded_winner() {
        let fixture = Fixture::with_write(true).await;
        let baseline = fixture.task_count();
        let Running {
            id,
            gate,
            mut caller,
            join,
        } = fixture.start_write(true).await;
        bounded(gate.narrowed.notified()).await;
        fixture.assert_consumed_without_terminal(&id);
        gate.release.notify_one();
        let output = bounded(&mut caller.output_rx).await.unwrap();
        assert!(matches!(output.result, Ok(FsOutput::Unit)));
        assert_eq!(output.disposition, Some(FsDisposition::Committed));
        caller.delivery_tx.send(Outcome::Succeeded).unwrap();
        let terminal = bounded(&mut caller.terminal_rx).await.unwrap();
        assert!(matches!(terminal, Err(GateError::Unavailable)));
        let error = io_delivery(
            terminal.map_err(anyhow::Error::new),
            output.disposition,
            DirectoryOutput::Unit,
        )
        .unwrap_err();
        assert!(fs_commit_unknown(&error));
        bounded(join).await.unwrap().unwrap();
        // Business delivery is uncertain after an actual mutation; physical
        // completion is known and the original immutable success stays exact.
        fixture.assert_terminal_record_once(&id, Outcome::Succeeded);
        assert_eq!(gate.outcomes.load(Ordering::Acquire), 1);
        assert_eq!(
            fs::read(fixture.workspace.join("file")).unwrap(),
            [PAYLOAD, b"-appended-once"].concat()
        );
        assert_eq!(fixture.budget.live.load(Ordering::Acquire), 0);
        assert_eq!(fixture.task_count(), baseline + 1);
        drop(caller.output_rx);
        drop(caller.terminal_rx);
        drop(caller.funding);
        assert_eq!(fixture.task_count(), baseline);
    }

    /// End-to-end regression for the forced test-only native post-install
    /// failure seam, run through the ACTUAL supervisor and Gate: the real
    /// renameat installs the overwrite target, then this original prepared
    /// call's armed closed flag returns one fixed Unavailable before
    /// Committed. This is not a real kernel failure, not a syscall
    /// replay-count proof and not a naturally reachable fault: the same
    /// consumed identity, the immutable same-ID terminal and move-only
    /// custody are the authority evidence.
    #[tokio::test]
    async fn forced_test_only_native_post_install_overwrite_failure_is_commit_unknown() {
        let fixture = Fixture::with_write(true).await;
        let baseline = fixture.task_count();
        // Bounded replacement bytes deliberately different from PAYLOAD;
        // only THIS original prepared call is armed before consumption.
        let replacement = b"installed-overwrite-then-forced-failure";
        let data = CoreBuffer::copy_from(
            replacement,
            &(fixture.budget.clone() as Arc<dyn AllocationBudget>),
        )
        .unwrap();
        let (prepared, grant) = fixture
            .prepared_call(FsCall::Write {
                path: "file",
                data,
                append: false,
            })
            .await;
        let prepared = prepared.force_test_only_native_post_install_failure();
        let Running {
            id,
            gate,
            mut caller,
            join,
        } = fixture.start_prepared_io(prepared, grant, false, false);
        bounded(gate.narrowed.notified()).await;
        // Same real Grant is consumed without any terminal, funded by the
        // original prepared-call + handoff Task reservations.
        fixture.assert_consumed_without_terminal(&id);
        assert_eq!(fixture.task_count(), baseline + 2);
        gate.release.notify_one();
        // The physical spawn_blocking native Join ALREADY ended before this
        // receipt: the still-pending `join` below is only the async
        // supervisor parked on its delivery_rx, not native work running.
        let output = bounded(&mut caller.output_rx).await.unwrap();
        assert!(matches!(output.result, Err(FsError::CommitUnknown)));
        assert_eq!(output.disposition, Some(FsDisposition::PartialUnknown));
        // The overwrite target was really installed before the forced
        // failure; `installed` kept ordinary cleanup from unlinking it.
        assert_eq!(
            fs::read(fixture.workspace.join("file")).unwrap().as_slice(),
            replacement.as_slice()
        );
        // Still no terminal for the same consumed ID before caller
        // delivery; original input/task custody remains funded.
        fixture.assert_consumed_without_terminal(&id);
        assert_eq!(fixture.task_count(), baseline + 2);
        assert!(fixture.budget.live.load(Ordering::Acquire) > 0);
        assert!(!join.is_finished());
        // Derive the caller outcome with the actual production
        // failure -> io_delivery -> directory_outcome functions on the
        // returned error/disposition; no hand-written terminal winner.
        let result = io_delivery(
            output.result.map_err(failure),
            output.disposition,
            DirectoryOutput::Unit,
        );
        let outcome = directory_outcome(&result, &fixture.cx);
        assert_eq!(
            outcome,
            Outcome::Failed {
                code: "fs-commit-unknown".into()
            }
        );
        caller.delivery_tx.send(outcome).unwrap();
        let terminal = bounded(&mut caller.terminal_rx).await.unwrap();
        assert!(matches!(terminal, Ok(())));
        bounded(join).await.unwrap().unwrap();
        // Actual Store: same-ID immutable fs-commit-unknown winner, an
        // acknowledged terminal, one outbox row and one audit event.
        fixture.assert_terminal_record_once(
            &id,
            Outcome::Failed {
                code: "fs-commit-unknown".into(),
            },
        );
        assert_eq!(gate.narrow_calls.load(Ordering::Acquire), 1);
        assert_eq!(gate.outcomes.load(Ordering::Acquire), 1);
        // Final bytes are the replacement and no private temp file remains;
        // bytes alone cannot count identical overwrite replays — consumed
        // identity and the same-ID terminal above are the evidence.
        assert_eq!(
            fs::read(fixture.workspace.join("file")).unwrap().as_slice(),
            replacement.as_slice()
        );
        let names: Vec<String> = fs::read_dir(&fixture.workspace)
            .unwrap()
            .map(|entry| entry.unwrap().file_name().into_string().unwrap())
            .collect();
        assert_eq!(names.len(), 1);
        assert_eq!(names[0], "file");
        assert_eq!(fixture.budget.live.load(Ordering::Acquire), 0);
        // Eventual original budget/task baseline only after all Joins and
        // owner Drops; not a claim about all heap deallocation.
        assert_eq!(fixture.task_count(), baseline + 1);
        drop(caller.output_rx);
        drop(caller.terminal_rx);
        drop(caller.funding);
        assert_eq!(fixture.task_count(), baseline);
    }
}
