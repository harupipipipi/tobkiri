use crate::{CallCx, PermissionSet, Principal};
use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use std::{path::PathBuf, sync::Arc};
use tobkiri_canon::{
    CanonValue, Digest, Variant,
    ids::{OpName, PackHash, PackId, ProfileId, RequestId, ShapeRef, UiSessionId},
};

#[derive(Debug, thiserror::Error)]
pub enum GateError {
    #[error("permission denied: {0}")]
    Denied(String),
    #[error("trusted approval required")]
    ApprovalRequired,
    #[error("request cancelled")]
    Cancelled,
    #[error("original request deadline elapsed")]
    Deadline,
    #[error("invalid request: {0}")]
    Invalid(String),
    #[error("security policy changed")]
    StaleEpoch,
    #[error("grant has already been consumed")]
    Consumed,
    #[error("limit exceeded: {0}")]
    Limit(String),
    #[error("authority persistence unavailable: {0}")]
    Persistence(String),
    #[error("authority integrity fault: {0:?}")]
    Integrity(IntegrityFaultCode),
    #[error("request denied ({denial}); denial audit failed ({audit})")]
    AuditFailure {
        denial: Box<GateError>,
        audit: Box<GateError>,
    },
    #[error("trusted approval surface unavailable")]
    Unavailable,
}
impl GateError {
    pub fn code(&self) -> &'static str {
        match self {
            Self::Denied(_) => "denied",
            Self::ApprovalRequired => "approval-required",
            Self::Cancelled => "cancelled",
            Self::Deadline => "deadline",
            Self::Invalid(_) => "invalid",
            Self::StaleEpoch => "stale-epoch",
            Self::Consumed => "consumed",
            Self::Limit(_) => "limit-exceeded",
            Self::Persistence(_) | Self::Integrity(_) | Self::AuditFailure { .. } => "persistence",
            Self::Unavailable => "unavailable",
        }
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum IntegrityFaultCode {
    TerminalRecord,
    ReservationRecord,
    AuditIdentityConflict,
    AuditChain,
    AuthorityDatabase,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AdmissionStatus {
    Allowed,
    IntegrityFault { code: IntegrityFaultCode },
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FailureClass {
    Transient,
    Integrity,
    Terminal,
}
impl GateError {
    pub fn failure_class(&self) -> FailureClass {
        match self {
            Self::AuditFailure { denial, audit } => {
                if denial.failure_class() == FailureClass::Integrity
                    || audit.failure_class() == FailureClass::Integrity
                {
                    FailureClass::Integrity
                } else {
                    audit.failure_class()
                }
            }
            Self::Integrity(_) => FailureClass::Integrity,
            Self::Persistence(_) => FailureClass::Transient,
            Self::Limit(code) if code == "audit quota" => FailureClass::Transient,
            _ => FailureClass::Terminal,
        }
    }
}

/// Core-private reservation identity. IDs alone convey no authority.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(transparent)]
pub struct GrantId(pub(crate) String);
impl GrantId {
    pub fn as_str(&self) -> &str {
        &self.0
    }
    pub fn clone_storage_bytes(&self) -> usize {
        self.0.len()
    }
    pub fn try_clone_precharged(&self) -> Result<Self, tobkiri_canon::AllocationError> {
        let mut value = String::new();
        value
            .try_reserve_exact(self.0.len())
            .map_err(|_| tobkiri_canon::AllocationError::Allocator)?;
        value.push_str(&self.0);
        Ok(Self(value))
    }
}
impl std::fmt::Display for GrantId {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum StoreTier {
    Private,
    Public,
    ReadOnly,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum StoreOperation {
    Open,
    Read,
    Write,
    Delete,
    SqlRead,
    SqlWrite,
    SetReadOnly,
    Transfer,
}
impl StoreOperation {
    pub fn is_write(self) -> bool {
        matches!(
            self,
            Self::Write | Self::Delete | Self::SqlWrite | Self::SetReadOnly | Self::Transfer
        )
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum FsOperation {
    /// Resolve a scoped root token; later I/O needs its own exact grant.
    Open,
    Read,
    List,
    Stat,
    Write,
    Rename,
    Remove,
    Mkdir,
}
impl FsOperation {
    pub fn is_write(self) -> bool {
        matches!(
            self,
            Self::Write | Self::Rename | Self::Remove | Self::Mkdir
        )
    }
}

/// Exact effect identity; its argument digest is bound separately to a grant.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "kebab-case", deny_unknown_fields)]
pub enum EffectRequest {
    WasmEntry {
        pack: PackId,
        hash: PackHash,
        entry: WasmEntryKind,
    },
    FlowRun {
        implements: ShapeRef,
        flow_owner: (PackId, PackHash),
        /// Exact frozen manifest Flow key selected by Core.
        flow_key: String,
        /// Canonical digest of the immutable revision-scoped Flow endpoint.
        target_digest: Digest,
    },
    PortCall {
        shape: ShapeRef,
        op: OpName,
    },
    /// Internal Core routing identity. Ordinary authorize always denies it;
    /// only the sealed ingress admission supplies its independent evidence.
    VirtualInputSubmit {
        ingress: Digest,
        invocation: Digest,
        source: Digest,
        routes: Digest,
    },
    Network {
        origin: String,
        method: String,
        url: String,
    },
    Fs {
        root: String,
        path: String,
        operation: FsOperation,
    },
    Proc {
        program: String,
    },
    Pty {
        program: String,
    },
    Secret {
        name: String,
        reveal: bool,
    },
    Storage {
        profile: ProfileId,
        owner: PackId,
        store: String,
        tier: StoreTier,
        operation: StoreOperation,
    },
    UserGlobalStorage {
        store: String,
        operation: StoreOperation,
    },
    AuditAppend,
    AuditRead,
    HostIo {
        operation: String,
    },
    Native {
        pack: PackId,
    },
    UserPermissions {
        profile_hash: Digest,
        revision: Digest,
        epoch: u64,
    },
    Permissions {
        pack: PackId,
        hash: PackHash,
    },
    Clock,
    Random,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "kebab-case", deny_unknown_fields)]
pub enum WasmEntryKind {
    Init,
    StreamOpen { handle: u64 },
    CallbackOpen { handle: u64 },
    MediatorReview,
}
impl EffectRequest {
    pub fn digest(&self) -> Result<Digest, GateError> {
        Ok(Digest::hash(&strict_bytes(self)?))
    }
    pub fn risk(&self) -> Risk {
        match self {
            Self::Native { .. } | Self::UserGlobalStorage { .. } => Risk::Dangerous,
            Self::Proc { .. }
            | Self::Pty { .. }
            | Self::Secret { reveal: true, .. }
            | Self::Storage {
                operation: StoreOperation::Transfer,
                ..
            }
            | Self::Fs {
                operation:
                    FsOperation::Write | FsOperation::Rename | FsOperation::Remove | FsOperation::Mkdir,
                ..
            } => Risk::Elevated,
            _ => Risk::Normal,
        }
    }
    pub fn kind(&self) -> &'static str {
        match self {
            Self::WasmEntry { .. } => "wasm-entry",
            Self::FlowRun { .. } => "flow-run",
            Self::PortCall { .. } => "port-call",
            Self::VirtualInputSubmit { .. } => "virtual-input-submit",
            Self::Network { .. } => "network",
            Self::Fs { .. } => "fs",
            Self::Proc { .. } => "proc",
            Self::Pty { .. } => "pty",
            Self::Secret { .. } => "secrets",
            Self::Storage { .. } => "storage",
            Self::UserGlobalStorage { .. } => "user-global-storage",
            Self::AuditAppend => "audit-append",
            Self::AuditRead => "audit-read",
            Self::HostIo { .. } => "host-io",
            Self::Native { .. } => "native",
            Self::UserPermissions { .. } => "user-permissions",
            Self::Permissions { .. } => "permissions",
            Self::Clock => "clock",
            Self::Random => "random",
        }
    }
    pub fn allowed_by(&self, p: &PermissionSet, cx: &CallCx) -> bool {
        match self {
            Self::WasmEntry { pack, hash, entry } => {
                cx.principal()
                    .pack()
                    .is_some_and(|(p, h)| p == pack && h == hash)
                    && (!matches!(entry, WasmEntryKind::MediatorReview) || p.mediator)
            }
            Self::FlowRun { implements, .. } => p.ports.contains(&implements.to_string()),
            Self::PortCall { shape, .. } => p.ports.contains(&shape.to_string()),
            Self::VirtualInputSubmit { .. } => false,
            Self::Network {
                origin,
                url,
                method,
            } => {
                p.network.origins.contains(origin)
                    && method.bytes().all(|b| b.is_ascii_uppercase())
                    && !method.is_empty()
                    && url::Url::parse(url)
                        .ok()
                        .zip(url::Url::parse(origin).ok())
                        .is_some_and(|(target, declared)| {
                            matches!(target.scheme(), "http" | "https")
                                && target.origin() == declared.origin()
                                && target.username().is_empty()
                                && target.password().is_none()
                                && declared.username().is_empty()
                                && declared.password().is_none()
                                && declared.path() == "/"
                                && declared.query().is_none()
                                && declared.fragment().is_none()
                        })
            }
            Self::Fs {
                root, operation, ..
            } => p.fs.iter().any(|r| {
                r.root == *root
                    && if *operation == FsOperation::Open {
                        r.read || r.write
                    } else if operation.is_write() {
                        r.write
                    } else {
                        r.read
                    }
            }),
            Self::Proc { program } => p.proc.contains(program),
            Self::Pty { program } => p.pty && p.proc.contains(program),
            Self::Secret { name, reveal } => p
                .secrets
                .iter()
                .any(|s| s.name == *name && (!reveal || s.reveal)),
            Self::Storage {
                profile,
                owner,
                tier,
                operation,
                ..
            } => {
                let own = cx.principal().pack().is_some_and(|(pack, _)| pack == owner);
                let local = profile == cx.profile();
                (local || p.cross_profile_storage)
                    && match tier {
                        StoreTier::Private => {
                            (own || *operation == StoreOperation::Transfer) && p.storage.private
                        }
                        StoreTier::Public | StoreTier::ReadOnly => {
                            if operation.is_write() {
                                (own || *tier == StoreTier::Public
                                    || *operation == StoreOperation::Transfer)
                                    && p.storage.public_write
                            } else {
                                p.storage.public_read
                            }
                        }
                    }
            }
            Self::UserGlobalStorage { .. } => p.cross_profile_storage,
            Self::AuditAppend => p.audit_append,
            Self::AuditRead => p.audit_read,
            Self::HostIo { operation } => p.host_io.contains(operation),
            Self::Native { .. } => p.native,
            Self::Permissions { .. } | Self::UserPermissions { .. } => false,
            Self::Clock | Self::Random => true,
        }
    }
}

pub(crate) fn strict_bytes<T: Serialize>(value: &T) -> Result<Vec<u8>, GateError> {
    let bytes = serde_json::to_vec(value).map_err(|e| GateError::Invalid(e.to_string()))?;
    let value = tobkiri_canon::parse(&bytes, Variant::StrictV1)
        .map_err(|e| GateError::Invalid(e.to_string()))?;
    tobkiri_canon::encode(&value, Variant::StrictV1).map_err(|e| GateError::Invalid(e.to_string()))
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum Risk {
    Normal,
    Elevated,
    Dangerous,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ApprovalPrompt {
    pub reservation: GrantId,
    pub profile: ProfileId,
    pub principal: Principal,
    pub request: RequestId,
    pub effect: EffectRequest,
    pub args_digest: Digest,
    pub security_epoch: u64,
    pub risk: Risk,
    pub summary: String,
}
#[derive(Clone, Debug)]
// Keep the frozen trusted-prompt API's attestation payload owned by value.
#[allow(clippy::large_enum_variant)]
pub enum ApprovalAnswer {
    Approve,
    Deny,
    Unavailable,
    Attestation(DeviceAttestation),
}
#[async_trait]
pub trait TrustedPrompt: Send + Sync {
    /// Cancellation waiters reserve their native Core slot before polling.
    fn cancellation_waiter(&self, cx: &CallCx) -> Result<crate::CancellationFuture, HostError> {
        Ok(cx.cancel().cancelled())
    }
    async fn prompt_approval(&self, prompt: ApprovalPrompt) -> ApprovalAnswer;

    /// One native review interaction, with Gate-owned exact per-item replies.
    /// Implementations must bind real trusted input and the complete displayed
    /// candidate to this live call. Untrusted renderer flags are not decisions.
    /// No automatic fallback to individual prompts changes the user's workflow.
    async fn prompt_approval_review(
        &self,
        _review: &mut crate::ApprovalReviewBatch<'_>,
    ) -> Result<(), GateError> {
        Err(GateError::Unavailable)
    }
}
#[derive(Debug, thiserror::Error)]
#[error("host I/O unavailable: {0}")]
pub struct HostError(pub String);
#[derive(Clone, Debug)]
pub struct UiEmbed {
    pub profile: ProfileId,
    pub pack: PackId,
    pub hash: PackHash,
    pub slot: String,
}
#[async_trait]
pub trait HostIo: TrustedPrompt + Send + Sync {
    /// Guest output is borrowed from its producer-funded encoded CoreBuffer.
    fn display_budgeted(
        &self,
        _surface: &str,
        _content: &tobkiri_canon::CoreBuffer,
    ) -> Result<(), HostError> {
        Err(HostError("budgeted display unavailable".into()))
    }
    /// Reserve before native input allocation/read. The returned buffer retains
    /// its actual owner lease; implementations may not wrap an old String read.
    async fn terminal_read_line_budgeted(
        &self,
        _prompt: &str,
        _max_bytes: usize,
        _budget: Arc<dyn tobkiri_canon::AllocationBudget>,
    ) -> Result<tobkiri_canon::CoreBuffer, HostError> {
        Err(HostError("budgeted terminal input unavailable".into()))
    }
    fn terminal_write_budgeted(&self, _text: &tobkiri_canon::CoreBuffer) -> Result<(), HostError> {
        Err(HostError("budgeted terminal output unavailable".into()))
    }
    async fn pick_directory_budgeted(
        &self,
        _purpose: &str,
        _max_bytes: usize,
        _budget: Arc<dyn tobkiri_canon::AllocationBudget>,
    ) -> Result<Option<tobkiri_canon::CoreBuffer>, HostError> {
        Err(HostError("budgeted directory picker unavailable".into()))
    }
    /// Trusted metadata only. Guest execution must use the budgeted methods.
    fn display(&self, surface: &str, content: &CanonValue);
    async fn terminal_read_line(&self, prompt: &str) -> Result<String, HostError>;
    fn terminal_write(&self, text: &str);
    fn embed_ui(&self, request: UiEmbed) -> Result<UiSessionId, HostError>;
    async fn pick_directory(&self, purpose: &str) -> Option<PathBuf>;
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Reservation {
    pub id: GrantId,
    pub profile: ProfileId,
    pub principal: Principal,
    pub request: RequestId,
    pub effect: Digest,
    pub args: Digest,
    pub epoch: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub revision: Option<Digest>,
    #[serde(default)]
    pub expires_unix_ms: u64,
    /// Original reservation expiry is retained when consumption narrows it.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub original_expires_unix_ms: Option<u64>,
    /// Absent only on legacy records. New IDs bind the durable authority scope.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub authority: Option<ReservationAuthority>,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReservationAuthority {
    pub namespace: Digest,
    pub generation: u64,
    pub sequence: u64,
    pub nonce: Digest,
}
/// Core-created fresh-reservation input; guests cannot supply the nonce or ID.
pub struct ReservationRequest {
    profile: ProfileId,
    principal: Principal,
    request: RequestId,
    effect: Digest,
    args: Digest,
    epoch: u64,
    revision: Option<Digest>,
    expires_unix_ms: u64,
    nonce: Digest,
}
impl ReservationRequest {
    pub(crate) fn fresh(
        cx: &CallCx,
        effect: Digest,
        args: Digest,
        expires_unix_ms: u64,
    ) -> Result<Self, GateError> {
        let mut nonce = [0u8; 32];
        getrandom::fill(&mut nonce).map_err(|_| GateError::Unavailable)?;
        Ok(Self {
            profile: cx.profile().clone(),
            principal: cx.principal().clone(),
            request: cx.request().clone(),
            effect,
            args,
            epoch: cx.security_epoch(),
            revision: cx.revision().cloned(),
            expires_unix_ms,
            nonce: Digest::hash(&nonce),
        })
    }
    pub fn profile(&self) -> &ProfileId {
        &self.profile
    }
    pub fn nonce(&self) -> &Digest {
        &self.nonce
    }
    /// Used only by the trusted ApprovalStore, inside its sequence transaction.
    pub fn bind(
        &self,
        namespace: Digest,
        generation: u64,
        sequence: u64,
    ) -> Result<Reservation, GateError> {
        if sequence == 0 {
            return Err(GateError::Invalid("zero reservation sequence".into()));
        }
        let authority = ReservationAuthority {
            namespace,
            generation,
            sequence,
            nonce: self.nonce.clone(),
        };
        let identity = Digest::hash(&strict_bytes(&(
            "tobkiri.grant/2",
            &self.profile,
            &authority,
        ))?);
        Ok(Reservation {
            id: GrantId(format!("grant_{}", identity.hex())),
            profile: self.profile.clone(),
            principal: self.principal.clone(),
            request: self.request.clone(),
            effect: self.effect.clone(),
            args: self.args.clone(),
            epoch: self.epoch,
            revision: self.revision.clone(),
            expires_unix_ms: self.expires_unix_ms,
            original_expires_unix_ms: Some(self.expires_unix_ms),
            authority: Some(authority),
        })
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PublicWriteApproval {
    pub profile: ProfileId,
    pub writer: PackId,
    pub owner: PackId,
    pub store: String,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "status", rename_all = "kebab-case", deny_unknown_fields)]
pub enum Outcome {
    Succeeded,
    Failed { code: String },
    Cancelled,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AuditEvent {
    pub profile: ProfileId,
    pub principal: Principal,
    pub request: RequestId,
    pub kind: String,
    pub payload: CanonValue,
}
pub trait AuditSink: Send + Sync {
    fn append(&self, event: AuditEvent) -> Result<(), GateError>;
    /// Identical event retries succeed; the same ID with different bytes must fail.
    fn append_idempotent(&self, _event_id: &Digest, _event: AuditEvent) -> Result<(), GateError> {
        Err(GateError::Unavailable)
    }
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum TerminalDisposition {
    Completed,
    AbandonedBeforeUse,
    ExpiredBeforeUse,
    ExpiredCleanupUnknown,
}
/// Immutable authority bookkeeping; it never asserts pooled resource cleanup.
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TerminalDecision {
    pub reservation: Reservation,
    pub outcome: Outcome,
    pub disposition: TerminalDisposition,
    pub event_id: Digest,
    pub event: AuditEvent,
}
impl TerminalDecision {
    pub fn new(
        reservation: Reservation,
        outcome: Outcome,
        disposition: TerminalDisposition,
    ) -> Result<Self, GateError> {
        let event_id = Digest::hash(&strict_bytes(&(
            "tobkiri.terminal/1",
            &reservation.profile,
            &reservation.id,
        ))?);
        let payload = tobkiri_canon::parse(
            &strict_bytes(&serde_json::json!({
                "grant": reservation.id.as_str(), "effect_digest": reservation.effect.as_str(),
                "args_digest": reservation.args.as_str(), "security_epoch": reservation.epoch,
                "outcome": &outcome, "disposition": &disposition,
            }))?,
            Variant::StrictV1,
        )
        .map_err(|e| GateError::Invalid(e.to_string()))?;
        let event = AuditEvent {
            profile: reservation.profile.clone(),
            principal: reservation.principal.clone(),
            request: reservation.request.clone(),
            kind: "outcome".into(),
            payload,
        };
        Ok(Self {
            reservation,
            outcome,
            disposition,
            event_id,
            event,
        })
    }
}
#[derive(Clone, Debug)]
pub struct TerminalDelivery {
    pub decision: TerminalDecision,
    pub acknowledged: bool,
}
/// Borrowed authority facts read together for one already-consumed lease.
/// This is descriptive evidence, never admission or a physical-cleanup receipt.
pub struct CleanupAuthoritySnapshot<'a> {
    pub current_epoch: u64,
    pub reservation: &'a Reservation,
    pub now_unix_ms: u64,
    pub terminal: Option<&'a TerminalDelivery>,
}
/// Cleanup facts paired with the actual sealed permit's execution bounds.
/// A stale epoch, elapsed deadline or cancelled context remains observable;
/// none is ordinary admission to execute a new effect.
pub struct CleanupSnapshot<'a> {
    pub authority: CleanupAuthoritySnapshot<'a>,
    pub limits: &'a crate::ResourceLimits,
    pub original_deadline: std::time::Instant,
    pub observed_at: std::time::Instant,
}
/// Typed result of one native cleanup-snapshot attempt. Only an
/// implementation that can authenticate its own pre-callback contention may
/// report `RetryableBusyBeforeCallback`; every other failure remains
/// `Uncertain` and retains its original error. Generic `Unavailable` or
/// `Persistence` results are never busy proof, a callback that already ran
/// can never be relabelled retryable, and no guest/WIT/JSON metadata is
/// consulted.
#[derive(Debug)]
pub enum CleanupSnapshotAttempt {
    /// The coherent snapshot ran and `inspect` was invoked exactly once.
    Selected,
    /// Authenticated contention before `inspect` ran; the same caller may
    /// retry within its original bounded cleanup window.
    RetryableBusyBeforeCallback,
    /// Any other failure: validation, lock poison, SQL, integrity, teardown
    /// or restoration faults, or a callback-reported error.
    Uncertain(GateError),
}
/// Opaque proof that Gate read and acknowledged the immutable terminal winner
/// for one actual consumed dispatch permit. This receipt cannot settle, retry,
/// replace or otherwise exercise the permit.
pub struct AcknowledgedTerminal {
    pub(crate) grant_id: GrantId,
    pub(crate) outcome: Outcome,
    pub(crate) disposition: TerminalDisposition,
    pub(crate) _reservation: crate::ControlReservation,
}
impl AcknowledgedTerminal {
    pub fn grant_id(&self) -> &GrantId {
        &self.grant_id
    }
    pub fn outcome(&self) -> &Outcome {
        &self.outcome
    }
    pub fn disposition(&self) -> &TerminalDisposition {
        &self.disposition
    }
}
/// Trusted policy effect; all fields are bound to the reservation before commit.
#[derive(Serialize)]
#[serde(tag = "kind", rename_all = "kebab-case")]
pub enum PermissionEffect<'a> {
    Pack {
        pack: &'a PackId,
        hash: &'a PackHash,
        permissions: &'a PermissionSet,
    },
    User {
        profile_hash: &'a Digest,
        revision: &'a Digest,
        epoch: u64,
        permissions: &'a PermissionSet,
    },
}
impl PermissionEffect<'_> {
    pub fn request(&self) -> EffectRequest {
        match self {
            Self::Pack { pack, hash, .. } => EffectRequest::Permissions {
                pack: (*pack).clone(),
                hash: (*hash).clone(),
            },
            Self::User {
                profile_hash,
                revision,
                epoch,
                ..
            } => EffectRequest::UserPermissions {
                profile_hash: (*profile_hash).clone(),
                revision: (*revision).clone(),
                epoch: *epoch,
            },
        }
    }
}

/// Persistent core-only authority storage. Consumption must be compare-and-swap.
pub trait ApprovalStore: Send + Sync {
    fn admission_status(&self, profile: &ProfileId) -> Result<AdmissionStatus, GateError>;
    /// First fault wins; clearing requires a separately defined verified repair.
    fn latch_integrity(
        &self,
        profile: &ProfileId,
        code: IntegrityFaultCode,
    ) -> Result<(), GateError>;
    fn security_epoch(&self, profile: &ProfileId) -> Result<u64, GateError>;
    fn advance_security_epoch(&self, profile: &ProfileId) -> Result<u64, GateError>;
    fn permissions(
        &self,
        profile: &ProfileId,
        pack: &PackId,
        hash: &PackHash,
    ) -> Result<Option<PermissionSet>, GateError>;
    fn set_permissions(
        &self,
        profile: &ProfileId,
        pack: &PackId,
        hash: &PackHash,
        permissions: &PermissionSet,
    ) -> Result<(), GateError>;
    fn user_permissions(
        &self,
        profile: &ProfileId,
        profile_hash: &Digest,
        revision: &Digest,
        epoch: u64,
    ) -> Result<Option<PermissionSet>, GateError>;
    fn set_user_permissions(
        &self,
        profile: &ProfileId,
        profile_hash: &Digest,
        revision: &Digest,
        epoch: u64,
        permissions: &PermissionSet,
    ) -> Result<(), GateError>;
    fn reserve(&self, reservation: &Reservation) -> Result<(), GateError>;
    fn reserve_fresh(&self, _request: &ReservationRequest) -> Result<Reservation, GateError> {
        Err(GateError::Unavailable)
    }
    fn dispatch(&self, id: &GrantId) -> Result<(), GateError>;
    fn dispatch_public_write(
        &self,
        reservation: &Reservation,
        approval: &PublicWriteApproval,
    ) -> Result<(), GateError>;
    fn abandon(&self, id: &GrantId) -> Result<Option<Reservation>, GateError>;
    fn sweep_expired(&self, profile: &ProfileId, now_unix_ms: u64) -> Result<u64, GateError>;
    fn consume(&self, reservation: &Reservation) -> Result<(), GateError>;
    /// Consume and persist a reduce-only trusted deadline in the same transaction.
    fn consume_with_deadline(
        &self,
        _reservation: &Reservation,
        _expires_unix_ms: u64,
    ) -> Result<Reservation, GateError> {
        Err(GateError::Unavailable)
    }
    fn narrow_consumed(
        &self,
        _reservation: &Reservation,
        _expires_unix_ms: u64,
    ) -> Result<Reservation, GateError> {
        Err(GateError::Unavailable)
    }
    /// Native DATA commit only. Validate this exact still-consumed reservation
    /// and current epoch under durable exclusion of every authority writer.
    /// Invoke `commit` at most once, keeping exclusion through its return.
    /// The callback is fixed synchronous Store work, never guest/Gate reentry.
    /// Unsupported stores fail closed. An error after callback entry is not
    /// evidence that the separate DATA transaction was rolled back.
    fn with_storage_commit_fence(
        &self,
        _expected: &Reservation,
        _commit: &mut dyn FnMut() -> Result<(), GateError>,
    ) -> Result<(), GateError> {
        Err(GateError::Unavailable)
    }
    /// Read exact consumed/terminal binding and current persisted epoch under
    /// one logically read-only IMMEDIATE transaction, with bounded acquisition.
    /// Call `inspect` at most once, only after validation, while that transaction
    /// still excludes authority writers. The callback must perform only its
    /// synchronous native selection: no locks, reentry, notifications or await.
    /// An error after inspection is teardown uncertainty; it must not undo an
    /// already-selected native cause or authorize selection/physical replay.
    fn with_cleanup_snapshot(
        &self,
        _expected: &Reservation,
        _inspect: &mut dyn FnMut(CleanupAuthoritySnapshot<'_>) -> Result<(), GateError>,
    ) -> Result<(), GateError> {
        Err(GateError::Unavailable)
    }
    /// Typed attempt variant of `with_cleanup_snapshot` under the identical
    /// single-coherent-snapshot contract. The default maps the legacy
    /// result: success is `Selected`; every error — including generic
    /// `Unavailable` and `Persistence` or a callback that never ran — is
    /// `Uncertain`, never inferred busy.
    fn try_cleanup_snapshot(
        &self,
        expected: &Reservation,
        inspect: &mut dyn FnMut(CleanupAuthoritySnapshot<'_>) -> Result<(), GateError>,
    ) -> CleanupSnapshotAttempt {
        match self.with_cleanup_snapshot(expected, inspect) {
            Ok(()) => CleanupSnapshotAttempt::Selected,
            Err(error) => CleanupSnapshotAttempt::Uncertain(error),
        }
    }
    fn settle_deadline(&self, _reservation: &Reservation) -> Result<TerminalDelivery, GateError> {
        Err(GateError::Unavailable)
    }
    /// Authority effect and immutable terminal/outbox decision share one transaction.
    fn commit_permission_effect(
        &self,
        _reservation: &Reservation,
        _expires_unix_ms: u64,
        _effect: PermissionEffect<'_>,
    ) -> Result<TerminalDelivery, GateError> {
        Err(GateError::Unavailable)
    }
    /// Commit all freshly prepared permission effects and their immutable
    /// terminals in one transaction, or publish none. The batch can only be
    /// constructed by Gate from its retained native preparations. Unsupported
    /// stores fail closed; a sequential fallback would violate this contract.
    /// This does not commit a Profile head or advance its security epoch.
    fn commit_permission_batch(
        &self,
        _batch: &crate::PermissionApprovalBatch<'_, '_>,
    ) -> Result<(), GateError> {
        Err(GateError::Unavailable)
    }
    fn finish(&self, id: &GrantId, outcome: &Outcome) -> Result<Reservation, GateError>;
    fn terminal_delivery(&self, id: &GrantId) -> Result<Option<TerminalDelivery>, GateError>;
    fn finish_terminal(
        &self,
        id: &GrantId,
        outcome: &Outcome,
    ) -> Result<TerminalDelivery, GateError>;
    fn abandon_terminal(&self, id: &GrantId) -> Result<Option<TerminalDelivery>, GateError>;
    fn expire_terminal(
        &self,
        profile: &ProfileId,
        now_unix_ms: u64,
    ) -> Result<Vec<TerminalDecision>, GateError>;
    fn pending_terminal(
        &self,
        profile: &ProfileId,
        max: u32,
    ) -> Result<Vec<TerminalDecision>, GateError>;
    fn terminal_watermark(&self, profile: &ProfileId) -> Result<u64, GateError>;
    fn pending_terminal_through(
        &self,
        profile: &ProfileId,
        max: u32,
        watermark: u64,
    ) -> Result<Vec<TerminalDecision>, GateError>;
    fn acknowledge_terminal(&self, event_id: &Digest, event: &AuditEvent) -> Result<(), GateError>;
    fn public_write_approved(
        &self,
        profile: &ProfileId,
        writer: &PackId,
        owner: &PackId,
        store: &str,
    ) -> Result<bool, GateError>;
    fn approve_public_write(
        &self,
        profile: &ProfileId,
        writer: &PackId,
        owner: &PackId,
        store: &str,
    ) -> Result<(), GateError>;
    fn paired_device_key(
        &self,
        profile: &ProfileId,
        device: &str,
    ) -> Result<Option<[u8; 32]>, GateError>;
    fn claim_attestation(
        &self,
        profile: &ProfileId,
        device: &str,
        nonce: &str,
        expires_unix_ms: u64,
    ) -> Result<bool, GateError>;
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DeviceAttestation {
    pub device: String,
    pub profile: ProfileId,
    pub principal: Principal,
    pub reservation: GrantId,
    pub request: RequestId,
    pub effect: Digest,
    pub args: Digest,
    pub epoch: u64,
    pub expires_unix_ms: u64,
    pub nonce: String,
    pub signature: Vec<u8>,
}

#[derive(Clone, Debug)]
// Keep the frozen mediator API's complete narrowing policy owned by value.
#[allow(clippy::large_enum_variant)]
pub enum Verdict {
    Pass,
    Deny(String),
    Constrain(PermissionSet),
}
#[async_trait]
pub trait Mediator: Send + Sync {
    async fn review(&self, pending: &ApprovalPrompt) -> Verdict;
}
#[derive(Default, Clone)]
pub struct MediatorChain {
    pub(crate) reviews: Vec<Arc<dyn Mediator>>,
}
impl MediatorChain {
    pub fn new(reviews: Vec<Arc<dyn Mediator>>) -> Self {
        Self { reviews }
    }
    pub fn review_count(&self) -> usize {
        self.reviews.len()
    }
}

#[async_trait]
pub trait Gate: Send + Sync {
    fn check_context(&self, cx: &CallCx) -> Result<(), GateError>;
    /// Read the immutable acknowledged terminal winner for an exact consumed
    /// permit. This is evidence only and cannot settle or consume the permit.
    fn acknowledged_terminal(
        &self,
        _permit: &crate::DispatchPermit,
    ) -> Result<crate::AcknowledgedTerminal, GateError> {
        Err(GateError::Unavailable)
    }
    /// Trusted Core cleanup-only observation of one exact sealed consumed
    /// permit under a single coherent authority snapshot. This is not fresh
    /// authorization, admission or a context/cancellation check. The caller
    /// MUST already hold Core's live/generation/cause guards in the
    /// established order; `inspect` runs at most once, only while a coherent
    /// snapshot is held, and may take no locks, reenter Gate/Core, notify or
    /// await. Borrowed snapshot fields never escape the call. An error
    /// returned after `inspect` ran is teardown uncertainty: it cannot roll
    /// back or reselect an already-made native cause selection, and the
    /// result is not a physical-cleanup success proof. Release all guards
    /// before waking tokens or performing cleanup.
    fn with_cleanup_snapshot(
        &self,
        _permit: &crate::DispatchPermit,
        _inspect: &mut dyn FnMut(CleanupSnapshot<'_>) -> Result<(), GateError>,
    ) -> Result<(), GateError> {
        Err(GateError::Unavailable)
    }
    /// Typed attempt variant of `with_cleanup_snapshot` for trusted Core
    /// cleanup owners. The default maps the legacy result and never infers
    /// busy from generic `Unavailable`/`Persistence` or a missing callback;
    /// busy is only meaningful before `inspect` ran.
    fn try_cleanup_snapshot(
        &self,
        _permit: &crate::DispatchPermit,
        _inspect: &mut dyn FnMut(CleanupSnapshot<'_>) -> Result<(), GateError>,
    ) -> CleanupSnapshotAttempt {
        match self.with_cleanup_snapshot(_permit, _inspect) {
            Ok(()) => CleanupSnapshotAttempt::Selected,
            Err(error) => CleanupSnapshotAttempt::Uncertain(error),
        }
    }
    fn admission_status(&self, _profile: &ProfileId) -> Result<AdmissionStatus, GateError> {
        Err(GateError::Unavailable)
    }
    /// Signal original live lease deadlines even when durable storage is down.
    /// This never claims cleanup or a terminal decision.
    fn cancel_due_leases(&self, _profile: &ProfileId) -> Result<u64, GateError> {
        Err(GateError::Unavailable)
    }
    async fn admit_port(
        &self,
        _cx: &CallCx,
        _shape: ShapeRef,
        _op: OpName,
        _args: Digest,
        _target: crate::FrozenPortTarget,
    ) -> Result<crate::PortAdmission, GateError> {
        Err(GateError::Unavailable)
    }
    async fn admit_compiled(
        &self,
        _cx: &CallCx,
        _activation: crate::CompiledActivation,
        _args: Digest,
    ) -> Result<crate::PortAdmission, GateError> {
        Err(GateError::Unavailable)
    }
    async fn claim_port(
        &self,
        _cx: &CallCx,
        _admission: crate::PortAdmission,
        _shape: &ShapeRef,
        _op: &OpName,
        _args: &Digest,
        _target: &crate::FrozenPortTarget,
    ) -> Result<crate::AdmittedDispatch, GateError> {
        Err(GateError::Unavailable)
    }
    async fn authorize(
        &self,
        cx: &CallCx,
        request: EffectRequest,
        args: Digest,
    ) -> Result<crate::Grant, GateError>;
    /// Native-only committed process-launch authorization. `request` must be
    /// a Proc/Pty effect exactly matching `binding`; the durable args digest
    /// comes only from `binding.args_digest()` — never from caller-supplied
    /// arguments, a caller key, raw argv/env or a caller identity/summary.
    /// The binding is committed data consistency only: it is not a proven FD
    /// capability, a live caller frame or an authority refresh. Implementations
    /// keep the normal context, permission, mediation, trusted per-effect
    /// consent and dispatch/audit pipeline. The default fails closed; there
    /// is no generic `authorize` fallback for launch effects.
    async fn authorize_process_launch(
        &self,
        _cx: &CallCx,
        _request: EffectRequest,
        _binding: &crate::ProcessLaunchBinding,
    ) -> Result<crate::Grant, GateError> {
        Err(GateError::Unavailable)
    }
    /// Fresh authority check for the stable Pack's physical capacity ledger.
    /// Trusted Core inspection only: these limits do not sponsor an allocation
    /// or establish entry. Actual funding still requires authenticated frame
    /// preparation or a live reached-node scope.
    fn code_owner_execution_limits(
        &self,
        _cx: &CallCx,
        _owner: crate::PreparedCodeOwner<'_>,
    ) -> Result<crate::ResourceLimits, GateError> {
        Err(GateError::Unavailable)
    }
    fn capacity_limit(&self, _cx: &CallCx) -> Result<u64, GateError> {
        Err(GateError::Unavailable)
    }
    /// Inspection only. Physical reserve/resize must use with_frame_allocation.
    fn frame_capacity_limit(
        &self,
        _cx: &CallCx,
        _scope: &crate::AllocationFrameScope,
    ) -> Result<u64, GateError> {
        Err(GateError::Unavailable)
    }
    /// Execute one fixed trusted capacity reserve/resize while Core's actual
    /// node admission is synchronized with retirement. No effects or awaits.
    fn with_frame_allocation(
        &self,
        _cx: &CallCx,
        _scope: &crate::AllocationFrameScope,
        _admit: &mut dyn FnMut(u64) -> Result<(), GateError>,
    ) -> Result<(), GateError> {
        Err(GateError::Unavailable)
    }
    /// Borrowed Core-only first reservation before constructing owned frame or
    /// verifier controls. The callback is fixed synchronous capacity work only.
    fn with_frame_preparation(
        &self,
        _cx: &CallCx,
        _prepared: &dyn crate::PreparedFrameVerifier,
        _reason: crate::AllocationReason,
        _admit: &mut dyn FnMut(crate::PreparedFrameFunding<'_>) -> Result<(), GateError>,
    ) -> Result<(), GateError> {
        Err(GateError::Unavailable)
    }
    /// Native Input bootstrap funding only; this mints no entry or port authority.
    fn input_capacity_limit(
        &self,
        _cx: &CallCx,
        _view: &ShapeRef,
        _op: &OpName,
        _target: &crate::FrozenPortTarget,
    ) -> Result<u64, GateError> {
        Err(GateError::Unavailable)
    }
    /// Register cancellation for a genuinely consumed grant, without minting authority.
    fn watch_lease(&self, _cx: &CallCx, _permit: &crate::DispatchPermit) -> Result<(), GateError> {
        Err(GateError::Unavailable)
    }
    async fn record_outcome(&self, grant: GrantId, outcome: Outcome) -> Result<(), GateError>;
    /// Settle a genuinely consumed lease's due deadline and deliver its immutable winner.
    async fn settle_deadline(
        &self,
        _permit: &crate::DispatchPermit,
    ) -> Result<TerminalDelivery, GateError> {
        Err(GateError::Unavailable)
    }
    /// Trusted reduce-only narrowing; original expiry and authority remain unchanged.
    async fn narrow_lease(
        &self,
        _permit: &mut crate::DispatchPermit,
        _cx: &CallCx,
    ) -> Result<(), GateError> {
        Err(GateError::Unavailable)
    }
    /// Record a pre-grant failure using machine constants and trusted context only.
    async fn record_denial(
        &self,
        cx: &CallCx,
        kind: &'static str,
        code: &'static str,
    ) -> Result<(), GateError>;
}
