//! Original handle terminal work, funded before source admission. Physical
//! source cleanup remains separate: it must finish before proposing metadata.
use anyhow::{Context, Result, bail};
use std::sync::{
    Arc, Mutex, OnceLock, Weak,
    atomic::{AtomicBool, Ordering},
};
use tobkiri_flow::StepError;
use tobkiri_gate::{ControlKind, ControlReservation, CoreControl};

const MAX_SETTLEMENTS: usize = 1024;
const MAX_METADATA_FUTURE: usize = 64 * 1024;
const METADATA_TASK_BYTES: u64 = (MAX_METADATA_FUTURE + 4096) as u64;
const MAX_REAPER_FUTURE: usize = 256 * 1024;
pub(crate) type QueuedMetadata =
    std::pin::Pin<Box<dyn std::future::Future<Output = Result<()>> + Send + 'static>>;

const RESULT_CELLS: usize = 2;
const DIAGNOSTIC_BYTES: usize = 512;
const STEP_CODE_BYTES: usize = 64;
const CAUSE_NODES: usize = 7;
const CAUSE_DEPTH: usize = 3;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum FailureStage {
    RecordOutcome,
    SettleDeadline,
    ReadAcknowledged,
    FrameAcknowledged,
    Invariant,
    Legacy,
    Join,
    Admission,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum FailureCategory {
    Denied,
    ApprovalRequired,
    Cancelled,
    Deadline,
    Invalid,
    StaleEpoch,
    Consumed,
    Limit,
    Persistence,
    Integrity,
    AuditFailure,
    Unavailable,
    Step,
    Invariant,
    Unknown,
    JoinCancelled,
    JoinPanicked,
    SpawnUnknown,
    PublicationRejected,
}

/// Inline UTF-8 prefix. The flag/marker distinguish bounded diagnostics from
/// a complete original message. No arbitrary Display/source chain is formatted.
#[derive(Debug)]
struct Diagnostic<const N: usize> {
    bytes: [u8; N],
    len: usize,
    truncated: bool,
}
impl<const N: usize> Diagnostic<N> {
    fn new() -> Self {
        Self {
            bytes: [0; N],
            len: 0,
            truncated: false,
        }
    }
    fn push(&mut self, value: &str) -> bool {
        let remaining = N - self.len;
        let mut take = value.len().min(remaining);
        while !value.is_char_boundary(take) {
            take -= 1;
        }
        self.bytes[self.len..self.len + take].copy_from_slice(&value.as_bytes()[..take]);
        self.len += take;
        if take != value.len() {
            self.truncated = true;
            false
        } else {
            true
        }
    }
    fn finish(&mut self) {
        if !self.truncated {
            return;
        }
        const MARKER: &str = " [truncated]";
        let mut end = self.len.min(N - MARKER.len());
        while !self.as_str().is_char_boundary(end) {
            end -= 1;
        }
        self.len = end;
        self.bytes[end..end + MARKER.len()].copy_from_slice(MARKER.as_bytes());
        self.len += MARKER.len();
    }
    fn as_str(&self) -> &str {
        std::str::from_utf8(&self.bytes[..self.len]).expect("bounded UTF-8 diagnostic")
    }
}

#[derive(Clone, Copy, Debug)]
struct FailureCause {
    _category: FailureCategory,
    _integrity: Option<tobkiri_gate::IntegrityFaultCode>,
    _depth: usize,
}
#[derive(Debug)]
pub(crate) struct MetadataFailure {
    pub(crate) _stage: FailureStage,
    pub(crate) category: FailureCategory,
    diagnostic: Diagnostic<DIAGNOSTIC_BYTES>,
    step_code: Diagnostic<STEP_CODE_BYTES>,
    audit_code: &'static str,
    wit_code: &'static str,
    // Only the length of an already-existing String, never a full formatted
    // arbitrary error. Recursive causes have a separate fixed depth/node cap.
    pub(crate) _original_length: Option<usize>,
    pub(crate) _code_original_length: Option<usize>,
    pub(crate) _code_truncated: bool,
    pub(crate) _diagnostic_unavailable: bool,
    _causes: [FailureCause; CAUSE_NODES],
    cause_len: usize,
    pub(crate) _causes_truncated: bool,
    retryable: bool,
}
impl MetadataFailure {
    fn empty(
        stage: FailureStage,
        category: FailureCategory,
        code: &'static str,
        retryable: bool,
    ) -> Self {
        let mut step_code = Diagnostic::new();
        step_code.push(code);
        Self {
            _stage: stage,
            category,
            diagnostic: Diagnostic::new(),
            step_code,
            audit_code: if code == "component" {
                "internal"
            } else {
                code
            },
            wit_code: if code == "component" {
                "unavailable"
            } else {
                code
            },
            _original_length: None,
            _code_original_length: None,
            _code_truncated: false,
            _diagnostic_unavailable: false,
            _causes: [FailureCause {
                _category: FailureCategory::Unknown,
                _integrity: None,
                _depth: 0,
            }; CAUSE_NODES],
            cause_len: 0,
            _causes_truncated: false,
            retryable,
        }
    }
    fn gate(stage: FailureStage, error: &tobkiri_gate::GateError) -> Self {
        let code = crate::host::gate_audit_code(error);
        let mut failure = Self::empty(stage, gate_category(error), code, true);
        failure._original_length = match error {
            tobkiri_gate::GateError::Denied(value)
            | tobkiri_gate::GateError::Invalid(value)
            | tobkiri_gate::GateError::Limit(value)
            | tobkiri_gate::GateError::Persistence(value) => Some(value.len()),
            _ => None,
        };
        failure.write_gate(error, 0);
        failure.diagnostic.finish();
        failure
    }
    fn write_gate(&mut self, error: &tobkiri_gate::GateError, depth: usize) {
        use tobkiri_gate::GateError as E;
        if depth > CAUSE_DEPTH || self.cause_len == CAUSE_NODES {
            self._causes_truncated = true;
            self.diagnostic.truncated = true;
            return;
        }
        self._causes[self.cause_len] = FailureCause {
            _category: gate_category(error),
            _integrity: if let tobkiri_gate::GateError::Integrity(code) = error {
                Some(*code)
            } else {
                None
            },
            _depth: depth,
        };
        self.cause_len += 1;
        let (prefix, value) = match error {
            E::Denied(value) => ("permission denied: ", Some(value.as_str())),
            E::Invalid(value) => ("invalid request: ", Some(value.as_str())),
            E::Limit(value) => ("limit exceeded: ", Some(value.as_str())),
            E::Persistence(value) => ("authority persistence unavailable: ", Some(value.as_str())),
            E::ApprovalRequired => ("trusted approval required", None),
            E::Cancelled => ("request cancelled", None),
            E::Deadline => ("original request deadline elapsed", None),
            E::StaleEpoch => ("security policy changed", None),
            E::Consumed => ("grant has already been consumed", None),
            E::Unavailable => ("trusted approval surface unavailable", None),
            E::Integrity(code) => {
                self.diagnostic.push("authority integrity fault: ");
                self.diagnostic.push(match code {
                    tobkiri_gate::IntegrityFaultCode::TerminalRecord => "TerminalRecord",
                    tobkiri_gate::IntegrityFaultCode::ReservationRecord => "ReservationRecord",
                    tobkiri_gate::IntegrityFaultCode::AuditIdentityConflict => {
                        "AuditIdentityConflict"
                    }
                    tobkiri_gate::IntegrityFaultCode::AuditChain => "AuditChain",
                    tobkiri_gate::IntegrityFaultCode::AuthorityDatabase => "AuthorityDatabase",
                });
                return;
            }
            E::AuditFailure { denial, audit } => {
                if !self.diagnostic.push("request denied (") {
                    self._causes_truncated = true;
                    return;
                }
                self.write_gate(denial, depth + 1);
                if self.diagnostic.truncated || !self.diagnostic.push("); denial audit failed (") {
                    self._causes_truncated = true;
                    return;
                }
                self.write_gate(audit, depth + 1);
                if !self.diagnostic.truncated {
                    self.diagnostic.push(")");
                }
                return;
            }
        };
        if self.diagnostic.push(prefix)
            && let Some(value) = value
        {
            self.diagnostic.push(value);
        }
    }
    fn step(stage: FailureStage, error: &StepError) -> Self {
        let mut failure = Self::empty(stage, FailureCategory::Step, "component", true);
        failure.audit_code = error.audit_code();
        failure.wit_code = error.audit_code();
        failure.step_code = Diagnostic::new();
        failure.step_code.push(&error.code);
        failure._code_original_length = Some(error.code.len());
        if failure.step_code.truncated {
            // An oversized nonstandard code cannot become a known category.
            failure.step_code = Diagnostic::new();
            failure.step_code.push("component");
            failure._code_truncated = true;
        }
        failure._original_length = Some(error.message.len());
        failure.diagnostic.push(&error.message);
        if failure._code_truncated {
            failure.diagnostic.truncated = true;
        }
        failure.diagnostic.finish();
        failure
    }
    fn unknown(category: FailureCategory) -> Self {
        let stage = match category {
            FailureCategory::PublicationRejected => FailureStage::Admission,
            FailureCategory::Unknown => FailureStage::Legacy,
            _ => FailureStage::Join,
        };
        let mut failure = Self::empty(stage, category, "unavailable", false);
        failure
            .diagnostic
            .push("handle metadata task did not finish");
        failure
    }
    fn capture(error: &anyhow::Error) -> Self {
        if let Some(error) = error.downcast_ref::<DeliveryError>() {
            return match error {
                DeliveryError::Gate(stage, source) => Self::gate(*stage, source),
                DeliveryError::Frame(source) => Self::step(FailureStage::FrameAcknowledged, source),
                DeliveryError::Invariant(message) => {
                    let mut failure = Self::empty(
                        FailureStage::Invariant,
                        FailureCategory::Invariant,
                        "component",
                        true,
                    );
                    failure.diagnostic.push(message);
                    failure
                }
            };
        }
        // Legacy unit futures exercise the same receipt with concrete native
        // errors. Unknown errors are not classified from Display/message text.
        if let Some(error) = error.downcast_ref::<tobkiri_gate::GateError>() {
            return Self::gate(FailureStage::Legacy, error);
        }
        if let Some(error) = error.downcast_ref::<StepError>() {
            return Self::step(FailureStage::Legacy, error);
        }
        // A normal returned Err has a known execution outcome even when its
        // diagnostic type is opaque. Do not inspect arbitrary Display/source
        // chains or misclassify its cause; a later explicit retry is allowed.
        let mut failure = Self::empty(
            FailureStage::Legacy,
            FailureCategory::Unknown,
            "component",
            true,
        );
        failure._diagnostic_unavailable = true;
        failure
            .diagnostic
            .push("handle metadata returned an unclassified error (diagnostic unavailable)");
        failure
    }
    #[cfg(test)]
    pub(crate) fn message(&self) -> &str {
        self.diagnostic.as_str()
    }
    #[cfg(test)]
    pub(crate) fn truncated(&self) -> bool {
        self.diagnostic.truncated
    }
    #[cfg(test)]
    pub(crate) fn cause_count(&self) -> usize {
        self.cause_len
    }
    pub(crate) fn audit_code(&self) -> &'static str {
        self.audit_code
    }
    pub(crate) fn wit_code(&self) -> &'static str {
        self.wit_code
    }
    pub(crate) fn step_error(&self) -> StepError {
        StepError::new(self.step_code.as_str(), self.diagnostic.as_str())
    }
}
fn gate_category(error: &tobkiri_gate::GateError) -> FailureCategory {
    use tobkiri_gate::GateError as E;
    match error {
        E::Denied(_) => FailureCategory::Denied,
        E::ApprovalRequired => FailureCategory::ApprovalRequired,
        E::Cancelled => FailureCategory::Cancelled,
        E::Deadline => FailureCategory::Deadline,
        E::Invalid(_) => FailureCategory::Invalid,
        E::StaleEpoch => FailureCategory::StaleEpoch,
        E::Consumed => FailureCategory::Consumed,
        E::Limit(_) => FailureCategory::Limit,
        E::Persistence(_) => FailureCategory::Persistence,
        E::Integrity(_) => FailureCategory::Integrity,
        E::AuditFailure { .. } => FailureCategory::AuditFailure,
        E::Unavailable => FailureCategory::Unavailable,
    }
}

#[derive(Debug)]
pub(crate) enum DeliveryError {
    Gate(FailureStage, tobkiri_gate::GateError),
    Frame(StepError),
    Invariant(&'static str),
}
impl DeliveryError {
    pub(crate) fn into_original(self) -> anyhow::Error {
        match self {
            Self::Gate(_, error) => error.into(),
            Self::Frame(error) => error.into(),
            Self::Invariant(message) => anyhow::anyhow!(message),
        }
    }
}
impl std::fmt::Display for DeliveryError {
    fn fmt(&self, output: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Gate(_, error) => std::fmt::Display::fmt(error, output),
            Self::Frame(error) => std::fmt::Display::fmt(error, output),
            Self::Invariant(message) => output.write_str(message),
        }
    }
}
impl std::error::Error for DeliveryError {}

type AttemptResult = std::result::Result<(), MetadataFailure>;
pub(crate) struct MetadataAttempt {
    generation: u64,
    join: tokio::sync::Mutex<Option<tokio::task::JoinHandle<AttemptResult>>>,
    result: OnceLock<AttemptResult>,
    ready: tokio::sync::Notify,
    _receipt_funding: Arc<ControlReservation>,
}
impl MetadataAttempt {
    fn new(generation: u64, receipt_funding: Arc<ControlReservation>) -> Arc<Self> {
        Arc::new(Self {
            generation,
            join: tokio::sync::Mutex::new(None),
            result: OnceLock::new(),
            ready: tokio::sync::Notify::new(),
            _receipt_funding: receipt_funding,
        })
    }
}
struct MetadataState {
    current: Option<Arc<MetadataAttempt>>,
    slots: [Weak<MetadataAttempt>; RESULT_CELLS],
    generation: u64,
}
#[derive(Debug)]
struct SharedMetadataFailure(Arc<MetadataAttempt>);
impl std::fmt::Debug for MetadataAttempt {
    fn fmt(&self, output: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        output
            .debug_struct("MetadataAttempt")
            .field("generation", &self.generation)
            .finish_non_exhaustive()
    }
}
impl std::fmt::Display for SharedMetadataFailure {
    fn fmt(&self, output: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self.0.result.get() {
            Some(Err(failure)) if failure.category == FailureCategory::Step => {
                write!(
                    output,
                    "{}: {}",
                    failure.step_code.as_str(),
                    failure.diagnostic.as_str()
                )
            }
            Some(Err(failure)) => output.write_str(failure.diagnostic.as_str()),
            _ => output.write_str("handle metadata task did not finish"),
        }
    }
}
impl std::error::Error for SharedMetadataFailure {}
pub(crate) fn metadata_failure(error: &anyhow::Error) -> Option<&MetadataFailure> {
    let shared = error.downcast_ref::<SharedMetadataFailure>()?;
    shared.0.result.get()?.as_ref().err()
}

/// Trusted, bounded constructor for a fresh delivery attempt. Implementations
/// retain authority objects but never a Host or its inventory. Constructing a
/// future only boxes captures; it cannot execute delivery or acquire Gate locks.
pub(crate) trait RetainedMetadata: Send + Sync + 'static {
    fn future(self: Arc<Self>) -> QueuedMetadata;
    fn future_bytes(&self) -> usize;
    fn owned_heap_bytes(&self) -> usize;
}

/// The exact original Gate/lease/frame and first proposal, without Entry's
/// source/control/Host edges. Outcome heap moves into the original receipt on
/// the first attempt; retries cannot propose another winner.
pub(crate) struct HandleDelivery {
    gate: Arc<dyn tobkiri_gate::Gate>,
    lease: Arc<crate::handles::HandleLease>,
    source_frame: Option<Arc<dyn crate::ProviderFrame>>,
    proposal: Mutex<Option<tobkiri_gate::Outcome>>,
}
impl HandleDelivery {
    pub(crate) fn new(
        gate: Arc<dyn tobkiri_gate::Gate>,
        entry: &crate::HandleEntry,
        outcome: tobkiri_gate::Outcome,
    ) -> Self {
        Self {
            gate,
            lease: entry.lease.clone(),
            source_frame: entry.source_frame.clone(),
            proposal: Mutex::new(Some(outcome)),
        }
    }

    pub(crate) async fn deliver(&self) -> std::result::Result<(), DeliveryError> {
        let mut receipt = self.lease.close.lock().await;
        if receipt.outcome.is_none() {
            if self
                .lease
                .permit
                .as_ref()
                .is_some_and(|permit| std::time::Instant::now() >= permit.deadline())
            {
                receipt.deadline_settlement = true;
            }
            receipt.outcome = self
                .proposal
                .lock()
                .map_err(|_| DeliveryError::Invariant("metadata proposal mutex poisoned"))?
                .take();
            if receipt.outcome.is_none() {
                return Err(DeliveryError::Invariant("terminal proposal missing"));
            }
        }
        if !receipt.delivered {
            if let Some(id) = &self.lease.grant {
                if receipt.deadline_settlement {
                    let permit = self
                        .lease
                        .permit
                        .as_ref()
                        .ok_or(DeliveryError::Invariant("deadline proof missing"))?;
                    let terminal = self.gate.settle_deadline(permit).await.map_err(|error| {
                        DeliveryError::Gate(FailureStage::SettleDeadline, error)
                    })?;
                    receipt.outcome = Some(terminal.decision.outcome);
                } else if let Err(error) = self
                    .gate
                    .record_outcome(
                        id.clone(),
                        receipt
                            .outcome
                            .clone()
                            .ok_or(DeliveryError::Invariant("terminal proposal missing"))?,
                    )
                    .await
                {
                    if matches!(error, tobkiri_gate::GateError::Deadline) {
                        // Durable wall-time expiry may win between the Instant
                        // check and CAS. Deliver that same winner without changing
                        // its disposition or replaying physical cleanup.
                        receipt.deadline_settlement = true;
                        let permit = self
                            .lease
                            .permit
                            .as_ref()
                            .ok_or(DeliveryError::Invariant("deadline proof missing"))?;
                        let terminal =
                            self.gate.settle_deadline(permit).await.map_err(|error| {
                                DeliveryError::Gate(FailureStage::SettleDeadline, error)
                            })?;
                        receipt.outcome = Some(terminal.decision.outcome);
                    } else {
                        return Err(DeliveryError::Gate(FailureStage::RecordOutcome, error));
                    }
                }
            }
            if let (Some(frame), Some(permit)) = (&self.source_frame, &self.lease.permit) {
                let acknowledged = self
                    .gate
                    .acknowledged_terminal(permit)
                    .map_err(|error| DeliveryError::Gate(FailureStage::ReadAcknowledged, error))?;
                frame
                    .terminal_acknowledged(&acknowledged)
                    .map_err(DeliveryError::Frame)?;
                receipt.outcome = Some(acknowledged.outcome().clone());
            }
            receipt.delivered = true;
        }
        Ok(())
    }
}
async fn deliver_retained(delivery: Arc<HandleDelivery>) -> Result<()> {
    delivery.deliver().await.map_err(anyhow::Error::new)
}
impl RetainedMetadata for HandleDelivery {
    fn future(self: Arc<Self>) -> QueuedMetadata {
        Box::pin(deliver_retained(self))
    }
    fn future_bytes(&self) -> usize {
        fn size<F: std::future::Future>(_: fn(Arc<HandleDelivery>) -> F) -> usize {
            std::mem::size_of::<F>()
        }
        size(deliver_retained)
    }
    fn owned_heap_bytes(&self) -> usize {
        match self
            .proposal
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .as_ref()
        {
            // The first attempt moves this heap to the receipt; record_outcome
            // may hold one ordinary clone while it is live. Count both.
            Some(tobkiri_gate::Outcome::Failed { code }) => code.capacity().saturating_mul(2),
            _ => 0,
        }
    }
}

/// Inventory holds this sidecar strongly; metadata holds only Weak. In
/// particular, capsule -> original lease -> metadata never returns to capsule.
struct MetadataCustody {
    metadata: Arc<HandleMetadata>,
    delivery: Mutex<Option<Arc<dyn RetainedMetadata>>>,
}

enum CleanupPublication {
    Pending,
    NoPublishedHandle,
    Unknown,
    Submitted(Arc<tokio::sync::Mutex<CleanupJoin>>),
}
struct CleanupJoin {
    task: Option<tokio::task::JoinHandle<Result<Option<crate::core_imports::SourceCleanupReport>>>>,
    result: Option<Result<crate::core_imports::SourceCleanupReport, StepError>>,
}
/// The source reaper's actual Result is independent of terminal delivery.
/// Its own future may await metadata.run; only shutdown joins this receipt.
struct SourceCleanupOwner {
    publication: Mutex<CleanupPublication>,
    ready: tokio::sync::Notify,
    settled: AtomicBool,
    future_bytes: usize,
    task_funding: Mutex<Option<Arc<ControlReservation>>>,
    _receipt_funding: ControlReservation,
}
impl SourceCleanupOwner {
    fn prepare(control: &CoreControl, future_bytes: usize) -> Result<Arc<Self>> {
        if future_bytes > MAX_REAPER_FUTURE {
            bail!("source cleanup exceeds its fixed future bound")
        }
        let receipt_funding = control.reserve(ControlKind::Frame, 4096)?;
        let task_funding =
            control.reserve(ControlKind::Task, u64::try_from(future_bytes)? + 4096)?;
        Ok(Arc::new(Self {
            publication: Mutex::new(CleanupPublication::Pending),
            ready: tokio::sync::Notify::new(),
            settled: AtomicBool::new(false),
            future_bytes,
            task_funding: Mutex::new(Some(Arc::new(task_funding))),
            _receipt_funding: receipt_funding,
        }))
    }

    fn finish_unpublished(&self, published: bool) {
        let no_source = {
            let mut publication = self
                .publication
                .lock()
                .unwrap_or_else(|error| error.into_inner());
            if !matches!(*publication, CleanupPublication::Pending) {
                return;
            }
            *publication = if published {
                CleanupPublication::Unknown
            } else {
                CleanupPublication::NoPublishedHandle
            };
            !published
        };
        if no_source {
            let funding = self
                .task_funding
                .lock()
                .unwrap_or_else(|error| error.into_inner())
                .take();
            drop(funding);
            self.settled.store(true, Ordering::Release);
        }
        self.ready.notify_waiters();
    }

    fn spawn(
        &self,
        runtime: &tokio::runtime::Handle,
        future: impl std::future::Future<
            Output = Result<Option<crate::core_imports::SourceCleanupReport>>,
        > + Send
        + 'static,
    ) -> Result<()> {
        if std::mem::size_of_val(&future) > self.future_bytes {
            self.finish_unpublished(true);
            bail!("source cleanup future exceeds its prepaid bound")
        }
        let funding = self
            .task_funding
            .lock()
            .map_err(|_| cleanup_unknown())?
            .as_ref()
            .context("source cleanup task funding missing")?
            .clone();
        let mut future = Some(future);
        let submitted = {
            let mut publication = self.publication.lock().map_err(|_| cleanup_unknown())?;
            if !matches!(*publication, CleanupPublication::Pending) {
                Err(anyhow::Error::new(cleanup_unknown()))
            } else {
                let future = future.take().expect("source cleanup taken twice");
                match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    runtime.spawn(async move {
                        let _funding = funding;
                        future.await
                    })
                })) {
                    Ok(task) => {
                        *publication = CleanupPublication::Submitted(Arc::new(
                            tokio::sync::Mutex::new(CleanupJoin {
                                task: Some(task),
                                result: None,
                            }),
                        ));
                        Ok(())
                    }
                    Err(_) => {
                        *publication = CleanupPublication::Unknown;
                        Err(anyhow::Error::new(cleanup_unknown()))
                    }
                }
            }
        };
        drop(future);
        self.ready.notify_waiters();
        submitted
    }

    async fn wait(&self) -> Result<Option<crate::core_imports::SourceCleanupReport>, StepError> {
        loop {
            let ready = self.ready.notified();
            tokio::pin!(ready);
            ready.as_mut().enable();
            let submitted = {
                let publication = self.publication.lock().map_err(|_| cleanup_unknown())?;
                match &*publication {
                    CleanupPublication::Pending => None,
                    CleanupPublication::NoPublishedHandle => return Ok(None),
                    CleanupPublication::Unknown => return Err(cleanup_unknown()),
                    CleanupPublication::Submitted(join) => Some(join.clone()),
                }
            };
            let Some(submitted) = submitted else {
                ready.await;
                continue;
            };
            let mut joined = submitted.lock().await;
            if let Some(result) = &joined.result {
                return result.clone().map(Some);
            }
            // Borrow the original Join across await. Dropping close drops
            // only this observer's guard, never the source reaper or receipt.
            let result = match joined.task.as_mut().ok_or_else(cleanup_unknown)?.await {
                Ok(Ok(Some(report))) => Ok(report),
                Ok(Ok(None)) | Ok(Err(_)) | Err(_) => Err(cleanup_unknown()),
            };
            drop(joined.task.take());
            joined.result = Some(result.clone());
            if result.is_ok() {
                let funding = self
                    .task_funding
                    .lock()
                    .map_err(|_| cleanup_unknown())?
                    .take();
                drop(funding);
                self.settled.store(true, Ordering::Release);
            }
            return result.map(Some);
        }
    }
}

fn cleanup_unknown() -> StepError {
    StepError::new(
        "unavailable",
        "source cleanup reaper did not complete successfully",
    )
}

/// Every observer borrows this SAME original Join. Cancelling its wait leaves
/// the Join in the lease/host inventory, rather than detaching it from close.
pub(crate) struct HandleMetadata {
    runtime: tokio::runtime::Handle,
    state: Mutex<MetadataState>,
    ready: tokio::sync::Notify,
    settled: AtomicBool,
    unregistered: AtomicBool,
    queued: Mutex<Option<QueuedMetadata>>,
    task_funding: Mutex<Option<Arc<ControlReservation>>>,
    source_cleanup: Mutex<Option<Arc<SourceCleanupOwner>>>,
    custody: OnceLock<Weak<MetadataCustody>>,
    rejected: AtomicBool,
    retiring: AtomicBool,
    // Receipt storage outlives the actual task and is independently prepaid.
    receipt_funding: Arc<ControlReservation>,
}
impl HandleMetadata {
    pub(crate) fn prepare(control: &CoreControl) -> Result<Arc<Self>> {
        let runtime = tokio::runtime::Handle::try_current()
            .context("handle metadata runtime is unavailable before admission")?;
        // One original Frame funds metadata/custody, the shared reservation,
        // and at most TWO actual immutable result cells including Arc headers.
        let receipt_bytes = std::mem::size_of::<Self>()
            + std::mem::size_of::<MetadataCustody>()
            + RESULT_CELLS * std::mem::size_of::<MetadataAttempt>()
            + std::mem::size_of::<ControlReservation>()
            + (6 + 2 * RESULT_CELLS) * std::mem::size_of::<usize>();
        if receipt_bytes > 4096 {
            bail!("handle metadata receipt exceeds its prepaid bound")
        }
        let receipt_funding = Arc::new(control.reserve(ControlKind::Frame, 4096)?);
        let task_funding = control.reserve(ControlKind::Task, METADATA_TASK_BYTES)?;
        let initial = MetadataAttempt::new(1, receipt_funding.clone());
        let slots = [Arc::downgrade(&initial), Weak::new()];
        Ok(Arc::new(Self {
            runtime,
            state: Mutex::new(MetadataState {
                current: Some(initial),
                slots,
                generation: 1,
            }),
            ready: tokio::sync::Notify::new(),
            settled: AtomicBool::new(false),
            unregistered: AtomicBool::new(false),
            queued: Mutex::new(None),
            task_funding: Mutex::new(Some(Arc::new(task_funding))),
            source_cleanup: Mutex::new(None),
            custody: OnceLock::new(),
            rejected: AtomicBool::new(false),
            retiring: AtomicBool::new(false),
            receipt_funding,
        }))
    }

    fn prepare_source_cleanup(&self, control: &CoreControl, future_bytes: usize) -> Result<()> {
        let cleanup = SourceCleanupOwner::prepare(control, future_bytes)?;
        let mut current = self.source_cleanup.lock().map_err(|_| cleanup_unknown())?;
        if current.is_some() {
            bail!("source cleanup already prepared")
        }
        *current = Some(cleanup);
        Ok(())
    }

    fn source_cleanup(&self) -> Option<Arc<SourceCleanupOwner>> {
        self.source_cleanup
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .clone()
    }

    pub(crate) fn spawn_reported_source_cleanup(
        &self,
        future: impl std::future::Future<
            Output = Result<Option<crate::core_imports::SourceCleanupReport>>,
        > + Send
        + 'static,
    ) -> Result<()> {
        self.source_cleanup()
            .context("source cleanup was not prepaid")?
            .spawn(&self.runtime, future)
    }

    #[cfg(test)]
    pub(crate) fn spawn_source_cleanup(
        &self,
        future: impl std::future::Future<Output = Result<()>> + Send + 'static,
    ) -> Result<()> {
        self.spawn_reported_source_cleanup(async move {
            future
                .await
                .map(|()| Some(crate::core_imports::SourceCleanupReport::joined_fixture_work()))
        })
    }

    fn finish_unpublished_source(&self, published: bool) {
        if let Some(cleanup) = self.source_cleanup() {
            cleanup.finish_unpublished(published);
        }
    }

    pub(crate) async fn wait_source_cleanup(
        &self,
    ) -> Result<Option<crate::core_imports::SourceCleanupReport>, StepError> {
        match self.source_cleanup() {
            Some(cleanup) => cleanup.wait().await,
            None => Ok(None),
        }
    }

    fn fully_settled(&self) -> bool {
        self.settled.load(Ordering::Acquire)
            && self
                .source_cleanup()
                .is_none_or(|cleanup| cleanup.settled.load(Ordering::Acquire))
    }

    fn poll_source_cleanup(&self) {
        // Admission maintenance borrows only existing receipts, outside the
        // inventory guard. A pending poll never takes an owning Join away.
        let future = self.wait_source_cleanup();
        tokio::pin!(future);
        let mut context = std::task::Context::from_waker(std::task::Waker::noop());
        let _ = std::future::Future::poll(future.as_mut(), &mut context);
    }

    fn retain<T: RetainedMetadata>(&self, delivery: T) -> Result<()> {
        // Admission may already have pruned the successful sidecar while an
        // old lease still owns metadata. Repeated finish remains idempotent;
        // it must not require reconstructing custody or a replacement task.
        if self.retiring.load(Ordering::Acquire) || self.settled.load(Ordering::Acquire) {
            drop(delivery);
            return Ok(());
        }
        let custody = match self.custody.get().and_then(Weak::upgrade) {
            Some(custody) => custody,
            None if self.retiring.load(Ordering::Acquire)
                || self.settled.load(Ordering::Acquire) =>
            {
                // Success/prune may race the entry check above. Recheck its
                // release-published receipt before interpreting inventory loss.
                drop(delivery);
                return Ok(());
            }
            None => bail!("handle delivery inventory is unavailable"),
        };
        let mut retained = custody.delivery.lock().map_err(|_| unavailable())?;
        if self.rejected.load(Ordering::Acquire) {
            drop(retained);
            bail!("handle delivery publication is permanently unknown")
        }
        if retained.is_some()
            || self.retiring.load(Ordering::Acquire)
            || self.settled.load(Ordering::Acquire)
        {
            // Never drop losing authority captures while a sidecar is locked.
            drop(retained);
            drop(delivery);
            return Ok(());
        }
        // The original Task allowance covers the capsule, its Arc header,
        // owned heap (including a Failed code's capacity), and ONE live boxed
        // future together. The existing extra 4096 remains runtime overhead.
        let bytes = std::mem::size_of::<T>()
            .checked_add(2 * std::mem::size_of::<usize>())
            .and_then(|bytes| bytes.checked_add(delivery.owned_heap_bytes()))
            .and_then(|bytes| bytes.checked_add(delivery.future_bytes()))
            .and_then(|bytes| bytes.checked_add(std::mem::size_of::<AttemptResult>()));
        if bytes.is_none_or(|bytes| bytes > MAX_METADATA_FUTURE) {
            // Latch under the SAME slot guard that admits a capsule. A later
            // valid proposal cannot race this rejection into a fresh start.
            self.rejected.store(true, Ordering::Release);
            drop(retained);
            bail!("handle delivery exceeds its prepaid bound")
        }
        *retained = Some(Arc::new(delivery));
        Ok(())
    }

    fn retained_future(&self) -> Option<QueuedMetadata> {
        let custody = self.custody.get().and_then(Weak::upgrade)?;
        let delivery = custody
            .delivery
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .clone()?;
        let bytes = delivery.future_bytes();
        let future = delivery.future();
        // Check the actual boxed concrete future against the preadmission
        // accounting too. No second task/future may overlap the original Join.
        assert!(std::mem::size_of_val(future.as_ref().get_ref()) <= bytes);
        Some(future)
    }

    fn release_delivery(&self) {
        let retired = self
            .custody
            .get()
            .and_then(Weak::upgrade)
            .and_then(|custody| {
                custody
                    .delivery
                    .lock()
                    .unwrap_or_else(|error| error.into_inner())
                    .take()
            });
        drop(retired);
    }

    fn factory_available(&self, custody: Option<&MetadataCustody>) -> bool {
        if self
            .queued
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .is_some()
        {
            return true;
        }
        custody.is_some_and(|custody| {
            custody
                .delivery
                .lock()
                .unwrap_or_else(|error| error.into_inner())
                .is_some()
        })
    }

    fn generation_limit() -> anyhow::Error {
        StepError::new(
            "limit-exceeded",
            "handle metadata result generation capacity unavailable",
        )
        .into()
    }

    /// Participation pins an actual cell before awaiting any Join. A new
    /// explicit observer can request a retry only after a completed ordinary
    /// failure. Passive source-reaper observation never creates a generation.
    fn participate(&self, retry: bool) -> Result<Arc<MetadataAttempt>> {
        self.participate_after_retirement(retry, || {})
    }

    fn participate_after_retirement(
        &self,
        retry: bool,
        after_retirement: impl FnOnce(),
    ) -> Result<Arc<MetadataAttempt>> {
        // An external metadata Arc may outlive its Registry. If the inventory
        // is dropped concurrently, this could be the final custody owner.
        // Declare it BEFORE state so every return unlocks before dropping
        // any retained capsule/authority captures.
        let custody = self.custody.get().and_then(Weak::upgrade);
        let mut state = self.state.lock().map_err(|_| unavailable())?;
        let current = state.current.as_ref().ok_or_else(Self::generation_limit)?;
        let eligible = retry
            && current
                .result
                .get()
                .is_some_and(|result| result.as_ref().err().is_some_and(|error| error.retryable))
            && self.factory_available(custody.as_deref());
        if !eligible {
            return Ok(current.clone());
        }
        let slot = if Arc::strong_count(current) == 1 {
            state
                .slots
                .iter()
                .position(|slot| slot.as_ptr() == Arc::as_ptr(current))
                .expect("current receipt owns one admitted slot")
        } else {
            state
                .slots
                .iter()
                .position(|slot| slot.strong_count() == 0)
                .ok_or_else(Self::generation_limit)?
        };
        let generation = state
            .generation
            .checked_add(1)
            .ok_or_else(Self::generation_limit)?;
        // Replacing a Weak alone does not retire its allocation. Remove the
        // selected backing BEFORE allocating so there are physically at most
        // two cells, including when the sole current cell is reused.
        // These closed cells contain only bounded inline results/Notify and
        // a non-final Frame Arc (self still owns it). No capsule, authority,
        // live future, opaque error or user destructor is dropped here.
        let retired_slot = std::mem::take(&mut state.slots[slot]);
        drop(state.current.take());
        drop(retired_slot);
        // Production passes only an empty closure. The test entry below
        // pauses here to prove other observers cannot see the transition.
        after_retirement();
        let attempt = MetadataAttempt::new(generation, self.receipt_funding.clone());
        state.generation = generation;
        state.slots[slot] = Arc::downgrade(&attempt);
        state.current = Some(attempt.clone());
        // No observer can see an artificial current=None transition.
        drop(state);
        self.ready.notify_waiters();
        Ok(attempt)
    }

    fn start_attempt(
        &self,
        attempt: &Arc<MetadataAttempt>,
        join: &mut Option<tokio::task::JoinHandle<AttemptResult>>,
    ) {
        if attempt.result.get().is_some() || join.is_some() {
            return;
        }
        if self.rejected.load(Ordering::Acquire) {
            assert!(
                attempt
                    .result
                    .set(Err(MetadataFailure::unknown(
                        FailureCategory::PublicationRejected
                    )))
                    .is_ok()
            );
            attempt.ready.notify_waiters();
            return;
        }
        let Some(metadata) = self.take_queued().or_else(|| self.retained_future()) else {
            return;
        };
        let funding = self
            .task_funding
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .as_ref()
            .expect("consumed metadata funding missing")
            .clone();
        let runtime = self.runtime.clone();
        match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            #[expect(
                clippy::result_large_err,
                reason = "The bounded inline diagnostic is prepaid by the original Task; boxing would add an allocation outside the fixed result-cell layout"
            )]
            let work = move || {
                let _funding = funding;
                // The pre-existing producer may allocate a large source error.
                // This change bounds only metadata RETAINED diagnostic heap:
                // no opaque anyhow/context/backtrace escapes the actual task.
                runtime
                    .block_on(metadata)
                    .map_err(|error| MetadataFailure::capture(&error))
            };
            self.runtime.spawn_blocking(work)
        })) {
            Ok(task) => *join = Some(task),
            Err(_) => {
                assert!(
                    attempt
                        .result
                        .set(Err(MetadataFailure::unknown(FailureCategory::SpawnUnknown)))
                        .is_ok()
                );
            }
        }
        attempt.ready.notify_waiters();
    }

    fn notify_current(&self) {
        let attempt = self
            .state
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .current
            .clone();
        if let Some(attempt) = attempt {
            attempt.ready.notify_waiters();
        }
        self.ready.notify_waiters();
    }

    pub(crate) fn queue_delivery<T: RetainedMetadata>(&self, delivery: T) -> Result<()> {
        if let Err(error) = self.retain(delivery) {
            self.rejected.store(true, Ordering::Release);
            self.notify_current();
            return Err(error);
        }
        let attempt = self.participate(false)?;
        if let Ok(mut join) = attempt.join.try_lock() {
            self.start_attempt(&attempt, &mut join);
        }
        self.notify_current();
        Ok(())
    }

    pub(crate) async fn run_delivery<T: RetainedMetadata>(&self, delivery: T) -> Result<()> {
        self.run_delivery_mode(delivery, true).await
    }
    pub(crate) async fn run_delivery_mode<T: RetainedMetadata>(
        &self,
        delivery: T,
        retry: bool,
    ) -> Result<()> {
        // Pin before making the capsule visible to the standing observer.
        // Even if that observer finishes first, this caller owns the same
        // original attempt rather than accidentally requesting a repair.
        let attempt = self.participate(retry)?;
        if let Err(error) = self.retain(delivery) {
            self.rejected.store(true, Ordering::Release);
            self.notify_current();
            return Err(error);
        }
        self.wait_pinned(attempt).await
    }

    #[cfg(test)]
    pub(crate) fn queue(
        &self,
        metadata: impl std::future::Future<Output = Result<()>> + Send + 'static,
    ) {
        assert!(
            std::mem::size_of_val(&metadata) + std::mem::size_of::<AttemptResult>()
                <= MAX_METADATA_FUTURE
        );
        let mut metadata: Option<QueuedMetadata> = Some(Box::pin(metadata));
        {
            let mut queued = self
                .queued
                .lock()
                .unwrap_or_else(|error| error.into_inner());
            if !self.retiring.load(Ordering::Acquire) && queued.is_none() {
                *queued = metadata.take();
            }
        }
        drop(metadata);
        if let Ok(attempt) = self.participate(false)
            && let Ok(mut join) = attempt.join.try_lock()
        {
            self.start_attempt(&attempt, &mut join);
        }
        self.notify_current();
    }
    fn take_queued(&self) -> Option<QueuedMetadata> {
        self.queued
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .take()
    }
    #[cfg(test)]
    pub(crate) async fn run(
        &self,
        metadata: impl std::future::Future<Output = Result<()>> + Send + 'static,
    ) -> Result<()> {
        if std::mem::size_of_val(&metadata) + std::mem::size_of::<AttemptResult>()
            > MAX_METADATA_FUTURE
        {
            bail!("handle metadata payload exceeds its prepaid bound")
        }
        self.queue(metadata);
        let attempt = self.participate(true)?;
        self.wait_pinned(attempt).await
    }
    pub(crate) async fn wait(&self) -> Result<()> {
        let attempt = self.participate(true)?;
        self.wait_pinned(attempt).await
    }
    /// Standing reaper: observe/drive the SAME first submitted work, but do
    /// not request a retry or consume any other waiter's error/cause.
    pub(crate) async fn observe(&self) -> Result<()> {
        let attempt = self.participate(false)?;
        self.wait_pinned(attempt).await
    }
    #[cfg(test)]
    pub(crate) fn pin_generation(&self) -> Arc<MetadataAttempt> {
        self.participate(false)
            .expect("pinned admitted metadata generation")
    }

    #[cfg(test)]
    pub(crate) fn pin_retry_after_retirement(
        &self,
        after_retirement: impl FnOnce(),
    ) -> Result<Arc<MetadataAttempt>> {
        self.participate_after_retirement(true, after_retirement)
    }

    async fn retire_success(&self) -> Result<()> {
        if self
            .retiring
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .is_ok()
        {
            self.release_delivery();
            drop(self.take_queued());
            let funding = self.task_funding.lock().map_err(|_| unavailable())?.take();
            drop(funding);
            self.settled.store(true, Ordering::Release);
            self.ready.notify_waiters();
        }
        loop {
            let ready = self.ready.notified();
            tokio::pin!(ready);
            ready.as_mut().enable();
            if self.settled.load(Ordering::Acquire) {
                return Ok(());
            }
            ready.await;
        }
    }

    pub(crate) async fn wait_pinned(&self, attempt: Arc<MetadataAttempt>) -> Result<()> {
        loop {
            let ready = attempt.ready.notified();
            tokio::pin!(ready);
            ready.as_mut().enable();
            if self.unregistered.load(Ordering::Acquire) {
                return Ok(());
            }
            if let Some(result) = attempt.result.get() {
                return match result {
                    Ok(()) => self.retire_success().await,
                    Err(failure)
                        if matches!(
                            failure.category,
                            FailureCategory::JoinCancelled
                                | FailureCategory::JoinPanicked
                                | FailureCategory::SpawnUnknown
                        ) =>
                    {
                        Err(unavailable().into())
                    }
                    Err(_) => Err(anyhow::Error::new(SharedMetadataFailure(attempt.clone()))),
                };
            }
            let mut join = attempt.join.lock().await;
            if attempt.result.get().is_some() {
                drop(join);
                continue;
            }
            self.start_attempt(&attempt, &mut join);
            if let Some(task) = join.as_mut() {
                // The original Join remains borrowed in the cell on observer
                // cancellation. Publish one immutable bounded result exactly
                // once; every observer pins and reads this SAME generation.
                let (result, join_failure) = match task.await {
                    Ok(result) => (result, None),
                    Err(error) => {
                        let category = if error.is_panic() {
                            FailureCategory::JoinPanicked
                        } else {
                            FailureCategory::JoinCancelled
                        };
                        (Err(MetadataFailure::unknown(category)), Some(error))
                    }
                };
                drop(join.take());
                assert!(attempt.result.set(result).is_ok());
                drop(join);
                // Panic payload destruction can run arbitrary Drop code.
                drop(join_failure);
                attempt.ready.notify_waiters();
                self.ready.notify_waiters();
                continue;
            }
            drop(join);
            if attempt.result.get().is_some() {
                continue;
            }
            ready.await;
        }
    }

    #[cfg(test)]
    pub(crate) fn retirement_guards_available(&self) -> bool {
        self.state.try_lock().is_ok()
            && self.queued.try_lock().is_ok()
            && self.task_funding.try_lock().is_ok()
            && self
                .custody
                .get()
                .and_then(Weak::upgrade)
                .is_none_or(|custody| custody.delivery.try_lock().is_ok())
    }

    fn abandon_unregistered(&self) {
        // The admission guard cannot coexist with a published handle. Close
        // may already be observing Pending, so publish without acquiring its
        // async mutex or attempting an asynchronous Drop.
        self.retiring.store(true, Ordering::Release);
        self.unregistered.store(true, Ordering::Release);
        self.finish_unpublished_source(false);
        self.release_delivery();
        drop(self.take_queued());
        let funding = self
            .task_funding
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .take();
        drop(funding);
        self.settled.store(true, Ordering::Release);
        self.notify_current();
    }
}

fn unavailable() -> StepError {
    StepError::new("unavailable", "handle metadata task did not finish")
}

struct RegistryState {
    closed: bool,
    entries: Vec<Arc<MetadataCustody>>,
}

/// This inventory is owned by the actual Session's PackHost. Admission and
/// sealing use one guard, so close includes every source that can consume a
/// grant, including a source still being registered when shutdown starts.
pub(crate) struct HandleMetadataRegistry {
    state: Mutex<RegistryState>,
    _funding: ControlReservation,
}
impl HandleMetadataRegistry {
    pub(crate) fn new(control: &CoreControl) -> Result<Self> {
        let bytes = MAX_SETTLEMENTS
            .checked_mul(std::mem::size_of::<Arc<MetadataCustody>>())
            .and_then(|bytes| bytes.checked_add(1024))
            .context("handle terminal inventory bound overflow")?;
        let funding = control.reserve(ControlKind::Registration, u64::try_from(bytes)?)?;
        let mut entries = Vec::new();
        entries.try_reserve_exact(MAX_SETTLEMENTS)?;
        if entries.capacity() != MAX_SETTLEMENTS {
            bail!("handle terminal inventory backing exceeds its bound")
        }
        Ok(Self {
            state: Mutex::new(RegistryState {
                closed: false,
                entries,
            }),
            _funding: funding,
        })
    }

    pub(crate) fn admit(&self, control: &CoreControl) -> Result<HandleMetadataAdmission> {
        let length = {
            let state = self.state.lock().map_err(|_| unavailable())?;
            if state.closed {
                bail!("handle source registration is closed")
            }
            state.entries.len()
        };
        for index in 0..length {
            let metadata = {
                let state = self.state.lock().map_err(|_| unavailable())?;
                state.entries.get(index).cloned()
            };
            if let Some(metadata) = metadata {
                metadata.metadata.poll_source_cleanup();
            }
        }
        // Release completed original captures/funding outside the registry.
        loop {
            let removed = {
                let mut state = self.state.lock().map_err(|_| unavailable())?;
                if state.closed {
                    bail!("handle source registration is closed")
                }
                state
                    .entries
                    .iter()
                    .position(|entry| entry.metadata.fully_settled())
                    .map(|index| state.entries.swap_remove(index))
            };
            let Some(removed) = removed else { break };
            drop(removed);
        }
        let metadata = HandleMetadata::prepare(control)?;
        let custody = Arc::new(MetadataCustody {
            metadata: metadata.clone(),
            delivery: Mutex::new(None),
        });
        assert!(metadata.custody.set(Arc::downgrade(&custody)).is_ok());
        {
            let mut state = self.state.lock().map_err(|_| unavailable())?;
            if state.closed || state.entries.len() == MAX_SETTLEMENTS {
                bail!("handle source registration is closed or full")
            }
            state.entries.push(custody);
        }
        Ok(HandleMetadataAdmission {
            metadata,
            registered: false,
        })
    }

    #[cfg(test)]
    pub(crate) fn guard_available(&self) -> bool {
        self.state.try_lock().is_ok()
    }

    pub(crate) async fn close(&self) -> Result<(), StepError> {
        let length = {
            let mut state = self.state.lock().map_err(|_| unavailable())?;
            state.closed = true;
            state.entries.len()
        };
        let mut failure = None;
        for index in 0..length {
            let metadata = {
                let state = self.state.lock().map_err(|_| unavailable())?;
                state.entries[index].metadata.clone()
            };
            if metadata.wait().await.is_err() {
                failure.get_or_insert_with(unavailable);
            }
            if let Err(error) = metadata.wait_source_cleanup().await {
                failure.get_or_insert(error);
            }
        }
        failure.map_or(Ok(()), Err)
    }
}

pub(crate) struct HandleMetadataAdmission {
    metadata: Arc<HandleMetadata>,
    registered: bool,
}
impl HandleMetadataAdmission {
    pub(crate) fn prepare_source_cleanup(
        &self,
        control: &CoreControl,
        future_bytes: usize,
    ) -> Result<()> {
        self.metadata.prepare_source_cleanup(control, future_bytes)
    }
    pub(crate) fn register(mut self) -> Arc<HandleMetadata> {
        self.registered = true;
        self.metadata.clone()
    }
}
impl Drop for HandleMetadataAdmission {
    fn drop(&mut self) {
        if !self.registered {
            self.metadata.abandon_unregistered();
        }
    }
}

/// Cancellation-safe original custody immediately after consume, including
/// failure before a public handle can be inserted. Only pre-consume admission
/// may use NoHandle; this guard always delivers the real consumed terminal.
pub(crate) struct ConsumedHandleMetadata {
    pub(crate) permit: Option<tobkiri_gate::DispatchPermit>,
    pub(crate) frame: Option<Arc<dyn crate::ProviderFrame>>,
    pub(crate) failure: &'static str,
    host: crate::PackHost,
    metadata: Arc<HandleMetadata>,
    lease: Option<Arc<crate::handles::HandleLease>>,
    kind: crate::HandleKind,
    owner: tobkiri_gate::CallCx,
    recipient: tobkiri_gate::CallCx,
    authority: tobkiri_canon::ids::ShapeRef,
    local_id: u64,
    generation: u64,
    version: &'static str,
    transferred: bool,
    published: bool,
}
impl ConsumedHandleMetadata {
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn new(
        host: crate::PackHost,
        metadata: HandleMetadataAdmission,
        permit: tobkiri_gate::DispatchPermit,
        kind: crate::HandleKind,
        owner: tobkiri_gate::CallCx,
        recipient: tobkiri_gate::CallCx,
        authority: tobkiri_canon::ids::ShapeRef,
        local_id: u64,
        generation: u64,
        version: &'static str,
    ) -> Self {
        Self {
            permit: Some(permit),
            frame: None,
            failure: "source-entry",
            host,
            metadata: metadata.register(),
            lease: None,
            kind,
            owner,
            recipient,
            authority,
            local_id,
            generation,
            version,
            transferred: false,
            published: false,
        }
    }

    pub(crate) fn lease(&mut self) -> Arc<crate::handles::HandleLease> {
        self.lease
            .get_or_insert_with(|| {
                Arc::new(crate::handles::HandleLease::authorized(
                    self.permit
                        .take()
                        .expect("consumed handle permit taken twice"),
                    self.metadata.clone(),
                ))
            })
            .clone()
    }

    pub(crate) fn entry(&mut self) -> crate::HandleEntry {
        let lease = self.lease();
        crate::HandleEntry::unpublished(
            self.kind,
            self.owner.clone(),
            self.local_id,
            self.recipient.clone(),
            self.authority.clone(),
            lease,
            self.generation,
            self.frame.clone(),
            self.version,
        )
    }

    pub(crate) async fn settle_failure(&mut self) -> Result<()> {
        self.metadata.finish_unpublished_source(self.published);
        let entry = self.entry();
        let delivery = HandleDelivery::new(
            self.host.shared.gate.clone(),
            &entry,
            tobkiri_gate::Outcome::Failed {
                code: self.failure.into(),
            },
        );
        self.metadata.run_delivery(delivery).await?;
        self.transferred = true;
        Ok(())
    }

    pub(crate) fn transfer_to_handle(&mut self) {
        self.transferred = true;
    }

    pub(crate) fn handle_published(&mut self) {
        self.published = true;
    }
}
impl Drop for ConsumedHandleMetadata {
    fn drop(&mut self) {
        if self.transferred {
            return;
        }
        self.metadata.finish_unpublished_source(self.published);
        let entry = self.entry();
        let delivery = HandleDelivery::new(
            self.host.shared.gate.clone(),
            &entry,
            tobkiri_gate::Outcome::Failed {
                code: self.failure.into(),
            },
        );
        // This fixed-size native capsule is bounded before any submission.
        // If admission is unavailable, preserve failure rather than inventing
        // terminal acknowledgement or a detached fallback task.
        if self.metadata.queue_delivery(delivery).is_err() {
            self.metadata.ready.notify_waiters();
        }
    }
}
