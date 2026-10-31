//! Closed failure DATA for the tracked native worker, never execution authority.
//! No arbitrary error chain, Debug text, VM handle or original lease is returned.
use crate::native_runtime_jobs::LifecycleError;
use std::{error::Error as StdError, io, time::Instant};
use tobkiri_core::{CoreError, NativeSetupInvocation};
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

/// Operation adapters must explicitly select one supported source variant.
/// There is deliberately no From<anyhow::Error> or unknown formatting fallback.
pub(crate) enum SourceError {
    NativeIo(NativeDataIoError),
    Gate(GateError),
    Control(CoreControlError),
    Core(CoreError),
    Io(io::Error),
    Condition(Condition),
}
pub(crate) enum WorkerFailure {
    Domain(NativeDataError),
    Execution { stage: Stage, source: SourceError },
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
                })
            }
            CoreError::Conflicts(_) => {
                return Ok(SourceRecord::Unsupported(Unsupported::CoreConflicts))
            }
            CoreError::Step(_) => return Ok(SourceRecord::Unsupported(Unsupported::CoreStep)),
            CoreError::Json(_) => return Ok(SourceRecord::Unsupported(Unsupported::CoreJson)),
            CoreError::Canon(_) => return Ok(SourceRecord::Unsupported(Unsupported::CoreCanon)),
            CoreError::Store(_) => return Ok(SourceRecord::Unsupported(Unsupported::CoreStore)),
            CoreError::Manifest(_) => {
                return Ok(SourceRecord::Unsupported(Unsupported::CoreManifest))
            }
            CoreError::Profile(_) => {
                return Ok(SourceRecord::Unsupported(Unsupported::CoreProfile))
            }
        };
        Ok(SourceRecord::Core {
            variant,
            text,
            source,
        })
    }
    pub(crate) fn worker(&mut self, error: &WorkerFailure) -> DetachedFailure {
        let stage = match error {
            WorkerFailure::Domain(_) => Stage::Operation,
            WorkerFailure::Execution { stage, .. } => *stage,
        };
        let result = match error {
            WorkerFailure::Domain(value) => self.domain(value),
            WorkerFailure::Execution { stage, source } => {
                let source = match source {
                    SourceError::NativeIo(value) => Ok(SourceRecord::NativeIo(*value)),
                    SourceError::Gate(value) => self.gate(value, 0),
                    SourceError::Control(value) => Ok(SourceRecord::Control(*value)),
                    SourceError::Core(value) => self.core(value, 0),
                    SourceError::Io(value) => Ok(Self::io(value)),
                    SourceError::Condition(value) => Ok(SourceRecord::Condition(*value)),
                };
                source.map(|source| DetachedFailure::Execution {
                    stage: *stage,
                    source,
                })
            }
        };
        result.unwrap_or_else(|issue| DetachedFailure::Detachment { stage, issue })
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
        if matches!(stage, Stage::Initialize | Stage::Finish) {
            if let Some(value) = outer.downcast_ref::<BootstrapCleanupFailure>() {
                let original = self.native_inner(&value.original, Stage::Initialize, depth + 1)?;
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
