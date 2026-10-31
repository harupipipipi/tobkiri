//! Fixed original-state worker. Neither SQL nor a callback is a product input.
use super::*;
use crate::native_worker_failure::DetachmentIssue;
use tobkiri_gate::CoreCancellation;
use tobkiri_harness_native::{
    OriginalAuthorityAdmission, OriginalAuthorityCause, OriginalAuthorityCleanup,
    OriginalAuthorityCloseReceipt, OriginalAuthorityRead,
};

#[derive(Debug)]
pub(crate) enum OriginalAuthorityData {
    Genesis {
        security_epoch: String,
        reason_digest: String,
    },
    Existing {
        security_epoch: String,
        record_value: String,
        advanced_at: f64,
        reason_digest: String,
    },
}
type AuthorityOutcome = std::result::Result<OriginalAuthorityData, OriginalAuthorityJobFailure>;
#[derive(Debug)]
pub(crate) enum OriginalAuthorityJobFailure {
    Worker(DetachedFailure),
    Admission(OriginalAuthorityAdmission),
    Source(OriginalAuthorityError),
    Cleanup {
        original: Box<AuthorityOutcome>,
        receipt: OriginalAuthorityCloseReceipt,
    },
}
pub(crate) struct OriginalAuthorityJob {
    result: tokio::sync::oneshot::Receiver<AuthorityOutcome>,
}
impl OriginalAuthorityJob {
    pub(crate) async fn result(self) -> AuthorityOutcome {
        self.result.await.unwrap_or_else(|_| {
            Err(OriginalAuthorityJobFailure::Worker(
                DetachedFailure::condition(Stage::Admission, Condition::WorkerDisconnected),
            ))
        })
    }
}
// Physically uncertain IO remains with its actual source and original owner;
// it is not sent as DATA, retried, or recorded as normal Closed. Job failure
// keeps admission Stopping. No pure DATA result is retained here.
pub(super) struct Retained {
    _cleanup: OriginalAuthorityCleanup,
    _source: OriginalBootstrapInputs,
    _invocation: NativeSetupInvocation,
    _cancellation: CoreCancellation,
    _control: tobkiri_gate::ControlReservation,
}

pub(crate) fn read_original_authority_snapshot(
    app: AppHandle,
) -> std::result::Result<OriginalAuthorityJob, DetachedFailure> {
    let state = app
        .try_state::<NativeRuntimeState>()
        .ok_or_else(|| {
            DetachedFailure::condition(Stage::Admission, Condition::RuntimeStateMissing)
        })?
        .inner()
        .clone();
    let (send, result) = tokio::sync::oneshot::channel();
    let runtime = state.clone();
    state
        .0
        .jobs
        .spawn(move |job| {
            let prepared = catch_unwind(AssertUnwindSafe(|| {
                let invocation = runtime.begin(&app, &job).map_err(|error| {
                    OriginalAuthorityJobFailure::Worker(
                        Detachment::new(job.invocation()).native(&error, Stage::Owner),
                    )
                })?;
                let cancellation = job.attach_invocation(&invocation).map_err(|error| {
                    OriginalAuthorityJobFailure::Worker(
                        Detachment::new(Some(invocation.clone())).native(&error, Stage::Admission),
                    )
                })?;
                let source = OriginalBootstrapInputs {
                    app,
                    _custody: job.source_custody().map_err(|error| {
                        OriginalAuthorityJobFailure::Worker(
                            Detachment::new(Some(invocation.clone()))
                                .native(&error, Stage::Admission),
                        )
                    })?,
                };
                // Reserve possible uncertain-IO ledger custody BEFORE any old DB
                // IO. The same original control account has at most 256 default
                // Registration records (and 256 admitted Tasks); every retained
                // entry keeps this token. This is not an unbounded failure Vec.
                let retention_control = invocation
                    .control()
                    .reserve(
                        tobkiri_gate::ControlKind::Registration,
                        (2 * std::mem::size_of::<Retained>() + 1024) as u64,
                    )
                    .map_err(|_| {
                        OriginalAuthorityJobFailure::Admission(
                            OriginalAuthorityAdmission::Capacity {
                                code: "native-control",
                            },
                        )
                    })?;
                let read = OriginalAuthorityRead::new(&invocation)
                    .map_err(OriginalAuthorityJobFailure::Admission)?;
                Ok::<_, OriginalAuthorityJobFailure>((
                    invocation,
                    cancellation,
                    source,
                    read,
                    retention_control,
                ))
            }));
            let (outcome, cleanup_verified) = match prepared {
                // No AuthorityStore IO has begun at these admission/owner stages.
                Ok(Err(error)) => (Err(error), true),
                Err(_) => (
                    Err(OriginalAuthorityJobFailure::Worker(
                        DetachedFailure::condition(Stage::Owner, Condition::NativePanic),
                    )),
                    false,
                ),
                Ok(Ok((invocation, cancellation, source, mut read, retention_control))) => {
                    // Keep read/source outside the panic boundary so its actual
                    // close receipt and owned IO survive any internal unwind.
                    let actual =
                        catch_unwind(AssertUnwindSafe(|| read.run(&source, &cancellation)));
                    let panicked = actual.is_err();
                    let mut detachment = Detachment::new(Some(invocation.clone()));
                    let mut original = match actual {
                        Ok(Ok(())) => match read
                            .result()
                            .expect("attempted original Authority operation")
                        {
                            Ok(value) => detach_material(&mut detachment, value)
                                .map_err(OriginalAuthorityJobFailure::Worker),
                            Err(error) => match detach_error(&mut detachment, error, 0) {
                                Ok(error) => Err(OriginalAuthorityJobFailure::Source(error)),
                                Err(error) => Err(OriginalAuthorityJobFailure::Worker(error)),
                            },
                        },
                        Ok(Err(error)) => Err(OriginalAuthorityJobFailure::Admission(error)),
                        Err(_) => Err(OriginalAuthorityJobFailure::Worker(
                            DetachedFailure::condition(Stage::Operation, Condition::NativePanic),
                        )),
                    };
                    // Apply publication refusal BEFORE constructing a cleanup
                    // branch, so retired success DATA cannot escape inside it.
                    if original.is_ok() && job.ensure_accepting(&runtime.0.jobs).is_err() {
                        drop(original);
                        original = Err(OriginalAuthorityJobFailure::Worker(
                            DetachedFailure::Publication {
                                reason: Condition::AdmissionRetired,
                                operation_succeeded: true,
                            },
                        ));
                    }
                    let receipt = read.close_receipt();
                    let retention = read.uncertain_cleanup();
                    let clean = receipt.verified() && !panicked;
                    if !receipt.verified() {
                        original = Err(OriginalAuthorityJobFailure::Cleanup {
                            original: Box::new(original),
                            receipt,
                        });
                    }
                    // Old read buffers and both sets of copy reservations die in
                    // the worker, before only bounded Host transport DATA is sent.
                    drop(read);
                    drop(detachment);
                    if let Some(cleanup) = retention {
                        runtime
                            .0
                            .retained_authority
                            .lock()
                            .unwrap_or_else(|error| error.into_inner())
                            .push(Retained {
                                _cleanup: cleanup,
                                _source: source,
                                _invocation: invocation,
                                _cancellation: cancellation,
                                _control: retention_control,
                            });
                    } else {
                        drop(source);
                        drop(cancellation);
                        drop(invocation);
                        drop(retention_control);
                    }
                    (original, clean)
                }
            };
            if !cleanup_verified {
                runtime.0.jobs.cleanup_failed();
            }
            let _ = send.send(outcome);
            WorkerExit { cleanup_verified }
        })
        .map_err(|error| Detachment::new(None).native(&error, Stage::Admission))?;
    Ok(OriginalAuthorityJob { result })
}

fn detach_material(
    copy: &mut Detachment,
    value: &OriginalAuthorityMaterial,
) -> std::result::Result<OriginalAuthorityData, DetachedFailure> {
    Ok(match value {
        OriginalAuthorityMaterial::Genesis {
            epoch,
            reason_digest,
        } => OriginalAuthorityData::Genesis {
            security_epoch: copy.authority_text(epoch.decimal())?,
            reason_digest: copy.authority_text(reason_digest.as_str())?,
        },
        OriginalAuthorityMaterial::Existing {
            epoch,
            record_epoch,
            advanced_at,
            reason_digest,
        } => OriginalAuthorityData::Existing {
            security_epoch: copy.authority_text(epoch.decimal())?,
            record_value: copy.authority_text(record_epoch.decimal())?,
            advanced_at: *advanced_at,
            reason_digest: copy.authority_text(reason_digest)?,
        },
    })
}
fn depth(value: usize) -> std::result::Result<(), DetachedFailure> {
    if value > 32 {
        Err(DetachedFailure::Detachment {
            stage: Stage::Operation,
            issue: DetachmentIssue::Depth,
        })
    } else {
        Ok(())
    }
}
fn detach_cause(
    copy: &mut Detachment,
    value: &OriginalAuthorityCause,
    level: usize,
) -> std::result::Result<OriginalAuthorityCause, DetachedFailure> {
    depth(level)?;
    Ok(match value {
        OriginalAuthorityCause::Io(value) => {
            OriginalAuthorityCause::Io(tobkiri_harness_native::OriginalAuthorityIoError {
                kind: value.kind,
                errno: value.errno,
                filename: value
                    .filename
                    .as_ref()
                    .map(|value| copy.authority_text(value))
                    .transpose()?,
                context: value
                    .context
                    .as_ref()
                    .map(|value| detach_cause(copy, value, level + 1).map(Box::new))
                    .transpose()?,
            })
        }
        OriginalAuthorityCause::Sqlite { code, message } => OriginalAuthorityCause::Sqlite {
            code: *code,
            message: copy.authority_text(message)?,
        },
        OriginalAuthorityCause::SecurePath { message, context } => {
            OriginalAuthorityCause::SecurePath {
                message,
                context: context
                    .as_ref()
                    .map(|value| detach_cause(copy, value, level + 1).map(Box::new))
                    .transpose()?,
            }
        }
        OriginalAuthorityCause::InvalidToken => OriginalAuthorityCause::InvalidToken,
        OriginalAuthorityCause::InvalidKey => OriginalAuthorityCause::InvalidKey,
        OriginalAuthorityCause::InvalidRecord(value) => {
            OriginalAuthorityCause::InvalidRecord(copy.authority_text(value)?)
        }
        OriginalAuthorityCause::Validation(value) => OriginalAuthorityCause::Validation(value),
        OriginalAuthorityCause::Capacity => OriginalAuthorityCause::Capacity,
        OriginalAuthorityCause::Cancelled => OriginalAuthorityCause::Cancelled,
        OriginalAuthorityCause::Deadline => OriginalAuthorityCause::Deadline,
        OriginalAuthorityCause::SourceUnavailable => OriginalAuthorityCause::SourceUnavailable,
        OriginalAuthorityCause::Unsupported(value) => OriginalAuthorityCause::Unsupported(value),
        OriginalAuthorityCause::Python {
            class,
            message,
            cause,
            context,
            suppressed,
        } => OriginalAuthorityCause::Python {
            class,
            message: copy.authority_text(message)?,
            cause: cause
                .as_ref()
                .map(|value| detach_cause(copy, value, level + 1).map(Box::new))
                .transpose()?,
            context: context
                .as_ref()
                .map(|value| detach_cause(copy, value, level + 1).map(Box::new))
                .transpose()?,
            suppressed: *suppressed,
        },
    })
}
fn detach_error(
    copy: &mut Detachment,
    value: &OriginalAuthorityError,
    level: usize,
) -> std::result::Result<OriginalAuthorityError, DetachedFailure> {
    depth(level)?;
    Ok(OriginalAuthorityError {
        stage: value.stage,
        class: value.class,
        message: copy.authority_text(&value.message)?,
        cause: value
            .cause
            .as_ref()
            .map(|value| detach_cause(copy, value, level + 1))
            .transpose()?,
        context: value
            .context
            .as_ref()
            .map(|value| detach_error(copy, value, level + 1).map(Box::new))
            .transpose()?,
        suppressed: value.suppressed,
    })
}
