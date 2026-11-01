//! Closed failure DATA for the tracked native worker, never execution authority.
//! No arbitrary error chain, Debug text, VM handle or original lease is returned.
use crate::native_runtime_jobs::LifecycleError;
use std::{error::Error as StdError, io, time::Instant};
use tobkiri_core::{AllocationError, CoreBuffer, CoreError, NativeSetupInvocation};
use tobkiri_gate::{
    CapacityKind, CoreControlError, GateError, IntegrityFaultCode, NativeSetupReservation,
    NativeSetupSnapshot,
};
use tobkiri_harness_native::{
    BootstrapCleanupFailure, BootstrapDomainCleanupFailure, InitializationTrace,
    OriginalInitializationFailure,
};
use tobkiri_wasm::{NativeDataError, NativeDataIoError, NativeDataObservation};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Stage {
    Admission,
    Owner,
    EngineCompile,
    Initialize,
    Operation,
    Review,
    Finish,
    Publication,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Condition {
    RuntimeStateMissing,
    AdmissionRetired,
    WorkerDisconnected,
    NativePanic,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Unsupported {
    AnyhowBoundary,
    IoCustom,
    CoreConflicts,
    CoreStep,
    CoreJson,
    CoreCanon,
    CoreStore,
    CoreManifest,
    CoreProfile,
}

/// These exports exist in the guest world, but have no current runner binding.
/// Listing them here supplies a precise integration failure, never dispatch.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum UnboundOperation {
    SetupProfilePreview,
    Confirmation,
    BootstrapConfirmation,
    ProfileConfirmation,
    SetupListing,
    SetupPreview,
    SetupActivationDecision,
    SetupActivationSuccess,
    SetupActivationFailure,
    RetiredSetupResponse,
    LoadCatalog,
    CatalogWithPacks,
    CatalogWithProfiles,
    ResolveProfile,
    DynamicProfileEdges,
    Denied,
    ImportValues,
    CopyDataDictionary,
    ReleaseValues,
    AppendDataItem,
    SetDataItem,
    NativeInputs,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum IntegrationFailure {
    OperationNotBound(UnboundOperation),
    InvalidValueResult,
    Codec(AllocationError),
    ReviewNotBound(tobkiri_harness_native::BootstrapReviewReachpoint),
    ReviewAdmission(tobkiri_harness_native::OriginalAuthorityAdmission),
}

#[derive(Debug)]
pub(crate) enum ReviewSourceRecord {
    Original(tobkiri_harness_native::OriginalAuthorityError),
    ResolutionDenied {
        message: String,
        cause: tobkiri_harness_native::OriginalAuthorityCause,
    },
    ActualDenied {
        view: String,
        message: String,
        host_cause: Option<ReviewHostCause>,
        // One native Host cause is also its context, by construction. Guest
        // intrinsic edges in the finite view have a separate provenance.
        suppress_context: bool,
    },
    ActualFactoryFailure {
        stage: tobkiri_harness_native::BootstrapDeniedStage,
        error: ReviewGuestError,
        host_context: Option<ReviewHostCause>,
    },
    GetterRaised(ReviewGuestError),
    ObservationRefused {
        stage: tobkiri_harness_native::BootstrapIdentityStage,
        error: ReviewGuestError,
        getter: Option<ReviewGuestError>,
    },
}
#[derive(Debug)]
pub(crate) struct ReviewGuestError {
    pub(crate) code: String,
    pub(crate) message: String,
    pub(crate) details: Option<String>,
    pub(crate) view: Option<String>,
}
#[derive(Debug)]
pub(crate) enum ReviewHostCause {
    NativeGraph {
        code: String,
        message: String,
        details: String,
    },
    OpaqueGetter(ReviewGuestError),
}

pub(crate) type Outcome = std::result::Result<serde_json::Value, DetachedFailure>;

#[derive(Debug)]
pub(crate) enum SourceRecord {
    Lifecycle {
        condition: &'static str,
    },
    NativeIo(NativeDataIoError),
    Control(CoreControlError),
    Condition(Condition),
    Integration(IntegrationFailure),
    Review(ReviewSourceRecord),
    ReviewCleanup(tobkiri_harness_native::OriginalAuthorityCloseReceipt),
    Io {
        kind: io::ErrorKind,
        raw_os_error: Option<i32>,
    },
    Gate {
        variant: &'static str,
        text: Option<Vec<u8>>,
        integrity: Option<IntegrityFaultCode>,
        denial: Option<Box<SourceRecord>>,
        audit: Option<Box<SourceRecord>>,
    },
    Core {
        variant: &'static str,
        text: Option<Vec<u8>>,
        source: Option<Box<SourceRecord>>,
    },
    CoreCleanup {
        original: Box<SourceRecord>,
        cleanup: Box<SourceRecord>,
    },
    Unsupported(Unsupported),
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum DetachmentIssue {
    NoInvocation,
    LengthOverflow,
    ReservationRejected { code: &'static str },
    Allocation,
    Depth,
    Encoding,
}
#[derive(Debug)]
pub(crate) enum DetachedFailure {
    Domain {
        code: Vec<u8>,
        message: Vec<u8>,
        details: Option<Vec<u8>>,
    },
    Initialization {
        diagnostic: Option<Vec<u8>>,
        original: Box<DetachedFailure>,
        trace: InitializationTrace,
        observation: Option<NativeDataObservation>,
        capacity: Option<NativeSetupSnapshot>,
        elapsed_micros: u128,
        deadline: Option<Instant>,
    },
    Execution {
        stage: Stage,
        source: SourceRecord,
    },
    Cleanup {
        original: Box<Outcome>,
        cleanup: Box<DetachedFailure>,
    },
    Publication {
        reason: Condition,
        operation_succeeded: bool,
    },
    Detachment {
        stage: Stage,
        issue: DetachmentIssue,
    },
}
impl DetachedFailure {
    pub(crate) fn condition(stage: Stage, condition: Condition) -> Self {
        Self::Execution {
            stage,
            source: SourceRecord::Condition(condition),
        }
    }
    pub(crate) fn integration(failure: IntegrationFailure) -> Self {
        Self::Execution {
            stage: Stage::Operation,
            source: SourceRecord::Integration(failure),
        }
    }
}

pub(crate) fn publication(outcome: Outcome, permitted: bool) -> Outcome {
    match outcome {
        Ok(value) if !permitted => {
            drop(value);
            Err(DetachedFailure::Publication {
                reason: Condition::AdmissionRetired,
                operation_succeeded: true,
            })
        }
        other => other,
    }
}

pub(crate) fn complete(
    original: Outcome,
    cleanup: Option<DetachedFailure>,
    permitted: bool,
) -> Outcome {
    let original = publication(original, permitted);
    match cleanup {
        Some(cleanup) => Err(DetachedFailure::Cleanup {
            original: Box::new(original),
            cleanup: Box::new(cleanup),
        }),
        None => original,
    }
}

/// Copy reservations and the actual invocation die inside the worker before
/// only owned DATA is sent. No replacement budget or lossy decoding is used.
pub(crate) struct Detachment {
    reservations: Vec<NativeSetupReservation>,
    invocation: Option<NativeSetupInvocation>,
}
impl Detachment {
    pub(crate) fn new(invocation: Option<NativeSetupInvocation>) -> Self {
        Self {
            invocation,
            reservations: Vec::new(),
        }
    }
    /// Preserve the original UTF-8 description as DATA, without interpreting
    /// it as a value graph or issuing a new reference. The copy retains the
    /// same invocation reservation until the worker's physical finish.
    pub(crate) fn description(&mut self, input: &CoreBuffer) -> Result<String, DetachedFailure> {
        self.bytes(input.as_bytes())
            .and_then(|bytes| String::from_utf8(bytes).map_err(|_| DetachmentIssue::Encoding))
            .map_err(|issue| DetachedFailure::Detachment {
                stage: Stage::Operation,
                issue,
            })
    }
    /// Copy original-Authority text once under this worker's same invocation.
    /// The read-side carrier and this prepaid copy overlap until detachment;
    /// neither reservation nor source custody is sent to the result consumer.
    pub(crate) fn authority_text(&mut self, input: &str) -> Result<String, DetachedFailure> {
        self.bytes(input.as_bytes())
            .and_then(|bytes| String::from_utf8(bytes).map_err(|_| DetachmentIssue::Encoding))
            .map_err(|issue| DetachedFailure::Detachment {
                stage: Stage::Operation,
                issue,
            })
    }
    fn bytes(&mut self, input: &[u8]) -> Result<Vec<u8>, DetachmentIssue> {
        let invocation = self
            .invocation
            .as_ref()
            .ok_or(DetachmentIssue::NoInvocation)?;
        self.reservations
            .try_reserve(1)
            .map_err(|_| DetachmentIssue::Allocation)?;
        let permit = invocation
            .reserve(
                CapacityKind::VfsBuffer,
                u64::try_from(input.len()).map_err(|_| DetachmentIssue::LengthOverflow)?,
            )
            .map_err(|error| DetachmentIssue::ReservationRejected { code: error.code() })?;
        let mut output = Vec::new();
        output
            .try_reserve_exact(input.len())
            .map_err(|_| DetachmentIssue::Allocation)?;
        if output.capacity() != input.len() {
            return Err(DetachmentIssue::Allocation);
        }
        output.extend_from_slice(input);
        self.reservations.push(permit);
        Ok(output)
    }
    fn depth(depth: usize) -> Result<(), DetachmentIssue> {
        if depth > 8 {
            Err(DetachmentIssue::Depth)
        } else {
            Ok(())
        }
    }
    fn domain(&mut self, error: &NativeDataError) -> Result<DetachedFailure, DetachmentIssue> {
        Ok(DetachedFailure::Domain {
            code: self.bytes(error.code().as_bytes())?,
            message: self.bytes(error.message().as_bytes())?,
            details: error
                .details()
                .map(|value| self.bytes(value.as_bytes()))
                .transpose()?,
        })
    }
    pub(crate) fn domain_data(&mut self, error: &NativeDataError) -> DetachedFailure {
        self.domain(error)
            .unwrap_or_else(|issue| DetachedFailure::Detachment {
                stage: Stage::Operation,
                issue,
            })
    }
    fn review_guest_error(
        &mut self,
        error: &NativeDataError,
        view: Option<&CoreBuffer>,
    ) -> Result<ReviewGuestError, DetachedFailure> {
        Ok(ReviewGuestError {
            code: self.description(error.code())?,
            message: self.description(error.message())?,
            details: error
                .details()
                .map(|value| self.description(value))
                .transpose()?,
            view: view.map(|value| self.description(value)).transpose()?,
        })
    }
    fn review_host_cause(
        &mut self,
        graph: Option<&tobkiri_harness_native::NativeErrorGraph>,
        getter: Option<&tobkiri_harness_native::BootstrapGetterError>,
    ) -> Result<Option<ReviewHostCause>, DetachedFailure> {
        if let Some(graph) = graph {
            return Ok(Some(ReviewHostCause::NativeGraph {
                code: self.description(graph.code())?,
                message: self.description(graph.message())?,
                details: self.description(graph.details())?,
            }));
        }
        getter
            .map(|error| {
                self.review_guest_error(error.error(), Some(error.view()))
                    .map(ReviewHostCause::OpaqueGetter)
            })
            .transpose()
    }
    /// Fixed actual-factory/getter DATA only. Borrow and copy each funded
    /// envelope under the original invocation; never inspect a general graph,
    /// call str/repr or rebuild an exception from these diagnostic strings.
    pub(crate) fn review_actual(
        &mut self,
        failure: &tobkiri_harness_native::BootstrapReviewFailure,
    ) -> Result<ReviewSourceRecord, DetachedFailure> {
        use tobkiri_harness_native::BootstrapReviewFailure::*;
        Ok(match failure {
            ActualDenied { denied, .. } => ReviewSourceRecord::ActualDenied {
                view: self.description(denied.view())?,
                message: self.description(denied.message())?,
                host_cause: self.review_host_cause(denied.cause(), denied.opaque_cause())?,
                suppress_context: denied.suppress_context(),
            },
            ActualFactoryFailure { failure, .. } => ReviewSourceRecord::ActualFactoryFailure {
                stage: failure.stage(),
                error: self.review_guest_error(failure.error(), None)?,
                host_context: self.review_host_cause(
                    failure.host_except_context(),
                    failure.opaque_except_context(),
                )?,
            },
            GetterRaised(error) => ReviewSourceRecord::GetterRaised(
                self.review_guest_error(error.error(), Some(error.view()))?,
            ),
            ObservationRefused(error) => ReviewSourceRecord::ObservationRefused {
                stage: error.stage(),
                error: self.review_guest_error(error.error(), None)?,
                getter: error
                    .getter_error()
                    .map(|error| self.review_guest_error(error, None))
                    .transpose()?,
            },
            _ => {
                return Err(DetachedFailure::integration(
                    IntegrationFailure::InvalidValueResult,
                ))
            }
        })
    }
    fn gate(&mut self, error: &GateError, depth: usize) -> Result<SourceRecord, DetachmentIssue> {
        Self::depth(depth)?;
        let mut record = SourceRecord::Gate {
            variant: error.code(),
            text: None,
            integrity: None,
            denial: None,
            audit: None,
        };
        if let SourceRecord::Gate {
            variant,
            text,
            integrity,
            denial,
            audit,
        } = &mut record
        {
            match error {
                GateError::Denied(value)
                | GateError::Invalid(value)
                | GateError::Limit(value)
                | GateError::Persistence(value) => {
                    *text = Some(self.bytes(value.as_bytes())?);
                }
                GateError::Integrity(value) => {
                    *variant = "integrity";
                    *integrity = Some(*value);
                }
                GateError::AuditFailure {
                    denial: one,
                    audit: two,
                } => {
                    *variant = "audit-failure";
                    *denial = Some(Box::new(self.gate(one, depth + 1)?));
                    *audit = Some(Box::new(self.gate(two, depth + 1)?));
                }
                _ => {}
            }
        }
        Ok(record)
    }
    fn io(error: &io::Error) -> SourceRecord {
        if error.raw_os_error().is_none() && error.get_ref().is_some() {
            SourceRecord::Unsupported(Unsupported::IoCustom)
        } else {
            SourceRecord::Io {
                kind: error.kind(),
                raw_os_error: error.raw_os_error(),
            }
        }
    }
    fn core(&mut self, error: &CoreError, depth: usize) -> Result<SourceRecord, DetachmentIssue> {
        Self::depth(depth)?;
        let (variant, text, source) = match error {
            CoreError::Invalid(value) => ("invalid", Some(self.bytes(value.as_bytes())?), None),
            CoreError::NotFound(value) => ("not-found", Some(self.bytes(value.as_bytes())?), None),
            CoreError::Runtime(value) => ("runtime", Some(self.bytes(value.as_bytes())?), None),
            CoreError::Gate(value) => ("gate", None, Some(Box::new(self.gate(value, depth + 1)?))),
            CoreError::Io(value) => ("io", None, Some(Box::new(Self::io(value)))),
            CoreError::Cleanup { original, cleanup } => {
                return Ok(SourceRecord::CoreCleanup {
                    original: Box::new(self.core(original, depth + 1)?),
                    cleanup: Box::new(self.core(cleanup, depth + 1)?),
                });
            }
            CoreError::Conflicts(_) => {
                return Ok(SourceRecord::Unsupported(Unsupported::CoreConflicts));
            }
            CoreError::Step(_) => return Ok(SourceRecord::Unsupported(Unsupported::CoreStep)),
            CoreError::Json(_) => return Ok(SourceRecord::Unsupported(Unsupported::CoreJson)),
            CoreError::Canon(_) => return Ok(SourceRecord::Unsupported(Unsupported::CoreCanon)),
            CoreError::Store(_) => return Ok(SourceRecord::Unsupported(Unsupported::CoreStore)),
            CoreError::Manifest(_) => {
                return Ok(SourceRecord::Unsupported(Unsupported::CoreManifest));
            }
            CoreError::Profile(_) => {
                return Ok(SourceRecord::Unsupported(Unsupported::CoreProfile));
            }
        };
        Ok(SourceRecord::Core {
            variant,
            text,
            source,
        })
    }
    /// Only this worker's known native capture sites use exact outer concrete
    /// checks. StdError::downcast_ref does not search anyhow context/source chains.
    pub(crate) fn native(&mut self, error: &anyhow::Error, stage: Stage) -> DetachedFailure {
        self.native_inner(error, stage, 0)
            .unwrap_or_else(|issue| DetachedFailure::Detachment { stage, issue })
    }
    fn native_inner(
        &mut self,
        error: &anyhow::Error,
        stage: Stage,
        depth: usize,
    ) -> Result<DetachedFailure, DetachmentIssue> {
        Self::depth(depth)?;
        let outer: &(dyn StdError + 'static) = error.as_ref();
        if let Some(value) =
            outer.downcast_ref::<tobkiri_harness_native::BootstrapReviewCleanupFailure>()
        {
            return Ok(DetachedFailure::Execution {
                stage,
                source: SourceRecord::ReviewCleanup(value.receipt),
            });
        }
        if matches!(stage, Stage::Initialize | Stage::Operation | Stage::Finish) {
            if let Some(value) = outer.downcast_ref::<BootstrapCleanupFailure>() {
                let original_stage = if stage == Stage::Finish {
                    Stage::Finish
                } else if stage == Stage::Operation {
                    Stage::Operation
                } else {
                    Stage::Initialize
                };
                let original = self.native_inner(&value.original, original_stage, depth + 1)?;
                let cleanup = self.native_inner(&value.cleanup, Stage::Finish, depth + 1)?;
                return Ok(DetachedFailure::Cleanup {
                    original: Box::new(Err(original)),
                    cleanup: Box::new(cleanup),
                });
            }
            if let Some(value) = outer.downcast_ref::<BootstrapDomainCleanupFailure>() {
                let original = self.domain(&value.domain)?;
                let cleanup = self.native_inner(&value.cleanup, Stage::Finish, depth + 1)?;
                return Ok(DetachedFailure::Cleanup {
                    original: Box::new(Err(original)),
                    cleanup: Box::new(cleanup),
                });
            }
            if let Some(value) = outer.downcast_ref::<OriginalInitializationFailure>() {
                return Ok(DetachedFailure::Initialization {
                    diagnostic: value
                        .diagnostic
                        .as_ref()
                        .map(|data| self.bytes(data.as_bytes()))
                        .transpose()?,
                    original: Box::new(self.native_inner(
                        &value.original,
                        Stage::Initialize,
                        depth + 1,
                    )?),
                    trace: value.trace,
                    observation: value.observation,
                    capacity: value.capacity,
                    elapsed_micros: value.elapsed_micros,
                    deadline: value.deadline,
                });
            }
        }
        let source = if let Some(value) = outer.downcast_ref::<LifecycleError>() {
            SourceRecord::Lifecycle { condition: value.0 }
        } else if let Some(value) = outer.downcast_ref::<NativeDataIoError>() {
            SourceRecord::NativeIo(*value)
        } else if let Some(value) = outer.downcast_ref::<CoreControlError>() {
            SourceRecord::Control(*value)
        } else if let Some(value) = outer.downcast_ref::<GateError>() {
            self.gate(value, depth + 1)?
        } else if let Some(value) = outer.downcast_ref::<CoreError>() {
            self.core(value, depth + 1)?
        } else if let Some(value) = outer.downcast_ref::<io::Error>() {
            Self::io(value)
        } else {
            SourceRecord::Unsupported(Unsupported::AnyhowBoundary)
        };
        Ok(DetachedFailure::Execution { stage, source })
    }
}

/// This exact producer envelope is emitted after successful cleanup or before
/// any native IO worker. Unknown/context-wrapped errors prove neither property.
pub(crate) fn initialization_cleanup_verified(error: &anyhow::Error) -> bool {
    let outer: &(dyn StdError + 'static) = error.as_ref();
    outer.is::<OriginalInitializationFailure>()
}

#[cfg(all(test, target_os = "macos"))]
#[path = "native_worker_failure_tests.rs"]
mod tests;
