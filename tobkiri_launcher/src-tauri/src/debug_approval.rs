use std::collections::{HashMap, HashSet};
use std::fs::OpenOptions;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use hmac::{Hmac, Mac};
use rand::{distributions::Alphanumeric, Rng, RngCore};
use serde::{Deserialize, Serialize};
use serde_json::json;
use sha2::{Digest, Sha256};

use crate::host_contract::ExecutionProfileIdentity;

type HmacSha256 = Hmac<Sha256>;

const REQUEST_TTL: Duration = Duration::from_secs(60 * 60);
const OPERATOR_TTL_SECONDS: u64 = 120;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum DebugApprovalDuration {
    OneHour,
    OneDay,
    OneWeek,
    OneMonth,
    Permanent,
}

impl DebugApprovalDuration {
    fn parse(value: &str) -> Result<Self, String> {
        match value {
            "1h" => Ok(Self::OneHour),
            "1d" => Ok(Self::OneDay),
            "1w" => Ok(Self::OneWeek),
            "1mo" => Ok(Self::OneMonth),
            "permanent" => Ok(Self::Permanent),
            _ => Err("invalid debug approval duration".into()),
        }
    }

    fn key(self) -> &'static str {
        match self {
            Self::OneHour => "1h",
            Self::OneDay => "1d",
            Self::OneWeek => "1w",
            Self::OneMonth => "1mo",
            Self::Permanent => "permanent",
        }
    }

    fn seconds(self) -> Option<u64> {
        match self {
            Self::OneHour => Some(60 * 60),
            Self::OneDay => Some(24 * 60 * 60),
            Self::OneWeek => Some(7 * 24 * 60 * 60),
            Self::OneMonth => Some(30 * 24 * 60 * 60),
            Self::Permanent => None,
        }
    }
}

#[derive(Debug, Clone)]
enum LeaseState {
    Disabled { reason: String },
    Pending(PendingSession),
    Armed(PendingSession),
    Active(ActiveLease),
}

#[derive(Debug, Clone)]
struct PendingSession {
    session_id: String,
    run_id: String,
    workspace: PathBuf,
    workspace_digest: String,
    pack_id: String,
    profile_id: String,
    profile_revision: String,
    activation_id: String,
    plan_digest: String,
    process_id: u32,
    process_fingerprint: String,
    claim_secret_hash: String,
    approved_duration: Option<DebugApprovalDuration>,
    expires_at: u64,
    deadline: Instant,
}

#[derive(Debug, Clone)]
struct ActiveLease {
    session_id: String,
    run_id: String,
    workspace: PathBuf,
    workspace_digest: String,
    pack_id: String,
    profile_id: String,
    profile_revision: String,
    activation_id: String,
    plan_digest: String,
    process_id: u32,
    process_fingerprint: String,
    session_secret_hash: String,
    lease_epoch: u64,
    expires_at: u64,
    deadline: Option<Instant>,
    duration: DebugApprovalDuration,
    lease_hash: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum OperatorState {
    Issued,
    Settling,
    Settled,
    ResumeFailed,
    ExecutionConsumed,
}

#[derive(Debug, Clone)]
struct OperatorRecord {
    operator: DebugCliOperator,
    state: OperatorState,
    execution_jti: Option<String>,
}

#[derive(Debug)]
struct DebugApprovalState {
    lease: LeaseState,
    next_lease_epoch: u64,
    operators: HashMap<String, OperatorRecord>,
    consumed_execution_jtis: HashSet<String>,
    guardians: HashMap<String, GuardianRecord>,
    audit_degraded: bool,
}

#[derive(Debug, Clone)]
struct GuardianRecord {
    process_id: u32,
    process_fingerprint: String,
    executable_identity: String,
    workspace: PathBuf,
    http_port: u16,
    api_token_file: PathBuf,
    execution_identity: ExecutionProfileIdentity,
    #[cfg(windows)]
    _process_handle: std::sync::Arc<std::os::windows::io::OwnedHandle>,
}

#[derive(Debug)]
pub struct DebugApprovalManager {
    state: Mutex<DebugApprovalState>,
    instance_nonce: String,
    signing_key: [u8; 32],
    audit_path: PathBuf,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct DebugApprovalStatus {
    pub state: String,
    pub reason: Option<String>,
    pub armed_remaining_seconds: Option<u64>,
    pub session_id: Option<String>,
    pub run_id: Option<String>,
    pub workspace: Option<String>,
    pub workspace_digest: Option<String>,
    pub pack_id: Option<String>,
    pub profile_id: Option<String>,
    pub profile_revision: Option<String>,
    pub activation_id: Option<String>,
    pub plan_digest: Option<String>,
    pub guardian_owned: bool,
    pub lease_epoch: Option<u64>,
    pub expires_at: Option<u64>,
    pub duration: Option<String>,
    pub instance_nonce: String,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct DebugGuardianStatus {
    pub run_id: String,
    pub workspace: String,
    pub pack_id: String,
    pub profile_id: String,
    pub profile_revision: String,
    pub activation_id: String,
    pub plan_digest: String,
    pub guardian_owned: bool,
    pub http_port: u16,
    pub api_token_file: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct DebugSessionStartRequest {
    pub session_id: String,
    pub run_id: String,
    pub workspace: String,
    pub pack_id: String,
    pub profile_id: String,
    pub profile_revision: String,
    pub activation_id: String,
    pub plan_digest: String,
    pub claim_secret: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct DebugSessionStartResponse {
    pub status: DebugApprovalStatus,
    pub session_secret: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct DebugSessionStopRequest {
    pub session_id: String,
    pub run_id: String,
    pub session_secret: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct DebugOperatorRequest {
    pub session_id: String,
    pub run_id: String,
    pub workspace_digest: String,
    pub pack_id: String,
    pub profile_id: String,
    pub profile_revision: String,
    pub activation_id: String,
    pub plan_digest: String,
    pub lease_epoch: u64,
    pub session_secret: String,
    pub request_id: String,
    pub permission_id: String,
    pub tool: String,
    pub action: String,
    pub operation: String,
    pub decision: String,
    pub canonical_arguments_digest: String,
    #[serde(default)]
    pub target_digest: Option<String>,
    pub conversation_id: String,
    pub operation_owner: String,
    pub request_expires_at: u64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct DebugOperatorVerifyRequest {
    pub debug_cli_operator: DebugCliOperator,
    pub expected_decision: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct DebugOperatorSettleRequest {
    pub debug_cli_operator: DebugCliOperator,
    pub outcome: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct DebugExecutionConsumeRequest {
    pub request_id: String,
    pub lease_epoch: u64,
    pub execution_jti: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct DebugCliOperator {
    pub kind: String,
    pub version: u8,
    pub origin: String,
    pub scope: String,
    pub session_id: String,
    pub run_id: String,
    pub workspace_digest: String,
    pub pack_id: String,
    pub profile_id: String,
    pub profile_revision: String,
    pub activation_id: String,
    pub plan_digest: String,
    pub lease_epoch: u64,
    pub request_id: String,
    pub permission_id: String,
    pub tool: String,
    pub action: String,
    pub operation: String,
    pub decision: String,
    pub canonical_arguments_digest: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub target_digest: Option<String>,
    pub conversation_id: String,
    pub operation_owner: String,
    pub issued_at: u64,
    pub expires_at: u64,
    pub nonce: String,
    pub signature: String,
}

impl DebugApprovalManager {
    pub fn new(audit_path: PathBuf) -> Self {
        let mut signing_key = [0_u8; 32];
        rand::thread_rng().fill_bytes(&mut signing_key);
        Self {
            state: Mutex::new(DebugApprovalState {
                lease: LeaseState::Disabled {
                    reason: "launcher_started".to_string(),
                },
                next_lease_epoch: 1,
                operators: HashMap::new(),
                consumed_execution_jtis: HashSet::new(),
                guardians: HashMap::new(),
                audit_degraded: false,
            }),
            instance_nonce: random_identifier("launcher"),
            signing_key,
            audit_path,
        }
    }

    pub fn status(&self) -> DebugApprovalStatus {
        let now_epoch = now_epoch_seconds();
        let now = Instant::now();
        let mut state = self.state.lock().unwrap_or_else(|error| error.into_inner());
        self.expire_if_needed(&mut state, now);
        status_from_state(&state.lease, &self.instance_nonce, now_epoch, now)
    }

    pub(crate) fn register_guardian(
        &self,
        run_id: String,
        process_id: u32,
        executable_identity: String,
        workspace: PathBuf,
        http_port: u16,
        api_token_file: PathBuf,
        execution_identity: ExecutionProfileIdentity,
    ) -> Result<(), String> {
        validate_identifier(&run_id, "run_id")?;
        validate_identifier(&executable_identity, "executable_identity")?;
        execution_identity
            .validate()
            .map_err(|_| "Launcher-owned guardian execution identity is invalid")?;
        if http_port == 0 {
            return Err("Launcher-owned guardian HTTP port is invalid".into());
        }
        let process_fingerprint = process_fingerprint(process_id)?;
        let workspace = workspace
            .canonicalize()
            .map_err(|_| "Launcher-owned guardian workspace is unavailable")?;
        let api_token_file = api_token_file
            .canonicalize()
            .map_err(|_| "Launcher-owned guardian API token is unavailable")?;
        if !api_token_file.is_file() {
            return Err("Launcher-owned guardian API token is unavailable".into());
        }
        #[cfg(windows)]
        let process_handle = retain_process_handle(process_id)?;
        let mut state = self
            .state
            .lock()
            .map_err(|_| "debug approval state unavailable")?;
        if state.guardians.contains_key(&run_id) {
            return Err("a Launcher-owned guardian already uses this run id".into());
        }
        state.guardians.insert(
            run_id,
            GuardianRecord {
                process_id,
                process_fingerprint,
                executable_identity,
                workspace,
                http_port,
                api_token_file,
                execution_identity,
                #[cfg(windows)]
                _process_handle: std::sync::Arc::new(process_handle),
            },
        );
        Ok(())
    }

    pub(crate) fn unregister_guardian(&self, run_id: &str) {
        if let Ok(mut state) = self.state.lock() {
            state.guardians.remove(run_id);
            self.expire_if_needed(&mut state, Instant::now());
        }
    }

    pub(crate) fn current_guardian(&self) -> Result<DebugGuardianStatus, String> {
        let state = self
            .state
            .lock()
            .map_err(|_| "debug approval state unavailable")?;
        let mut live = state.guardians.iter().filter(|(_, guardian)| {
            process_fingerprint(guardian.process_id)
                .is_ok_and(|fingerprint| fingerprint == guardian.process_fingerprint)
        });
        let Some((run_id, guardian)) = live.next() else {
            return Err("no live Launcher-owned Defaultspack child".into());
        };
        if live.next().is_some() {
            return Err("multiple Launcher-owned Defaultspack children are active".into());
        }
        Ok(DebugGuardianStatus {
            run_id: run_id.clone(),
            workspace: guardian.workspace.to_string_lossy().into_owned(),
            pack_id: "defaultspack".into(),
            profile_id: guardian.execution_identity.profile_id.clone(),
            profile_revision: guardian.execution_identity.profile_revision.clone(),
            activation_id: guardian.execution_identity.activation_id.clone(),
            plan_digest: guardian.execution_identity.plan_digest.clone(),
            guardian_owned: true,
            http_port: guardian.http_port,
            api_token_file: guardian.api_token_file.to_string_lossy().into_owned(),
        })
    }

    pub fn register_session(
        &self,
        request: DebugSessionStartRequest,
    ) -> Result<DebugApprovalStatus, String> {
        let now_epoch = now_epoch_seconds();
        let now = Instant::now();
        let mut state = self
            .state
            .lock()
            .map_err(|_| "debug approval state unavailable")?;
        self.expire_if_needed(&mut state, now);
        if state.audit_degraded {
            return Err("debug approval audit is degraded; new sessions are blocked".into());
        }
        let pending = match pending_from_request(&request, &state.guardians) {
            Ok(pending) => pending,
            Err(error) => {
                let reason = if error.contains("workspace") {
                    "workspace_mismatch"
                } else if error.contains("run id") {
                    "run_mismatch"
                } else {
                    "internal_invariant_failure"
                };
                if self
                    .audit(
                        "session_rejected",
                        reason,
                        None,
                        Some(&request.run_id),
                        None,
                    )
                    .is_err()
                {
                    state.audit_degraded = true;
                }
                return Err(error);
            }
        };
        match &state.lease {
            LeaseState::Disabled { .. } => {}
            LeaseState::Pending(existing)
                if existing.session_id == pending.session_id
                    && existing.claim_secret_hash == pending.claim_secret_hash =>
            {
                return Ok(status_from_state(
                    &state.lease,
                    &self.instance_nonce,
                    now_epoch,
                    now,
                ));
            }
            _ => return Err("another debug session request is already pending or active".into()),
        }
        self.audit(
            "session_requested",
            "pending_human_confirmation",
            None,
            Some(&pending.run_id),
            None,
        )?;
        state.lease = LeaseState::Pending(pending);
        state.operators.clear();
        Ok(status_from_state(
            &state.lease,
            &self.instance_nonce,
            now_epoch,
            now,
        ))
    }

    /// Called only after the Tauri command has validated the dedicated Launcher
    /// window and completed a native operating-system confirmation dialog.
    pub fn arm(&self, duration: &str) -> Result<DebugApprovalStatus, String> {
        let duration = DebugApprovalDuration::parse(duration)?;
        let now_epoch = now_epoch_seconds();
        let now = Instant::now();
        let mut state = self
            .state
            .lock()
            .map_err(|_| "debug approval state unavailable")?;
        self.expire_if_needed(&mut state, now);
        if state.audit_degraded {
            return Err("debug approval audit is degraded; enabling is blocked".into());
        }
        let mut pending = match &state.lease {
            LeaseState::Pending(pending) => pending.clone(),
            LeaseState::Armed(existing) if existing.approved_duration == Some(duration) => {
                return Ok(status_from_state(
                    &state.lease,
                    &self.instance_nonce,
                    now_epoch,
                    now,
                ));
            }
            LeaseState::Active(active) if active.duration == duration => {
                return Ok(status_from_state(
                    &state.lease,
                    &self.instance_nonce,
                    now_epoch,
                    now,
                ));
            }
            LeaseState::Armed(_) | LeaseState::Active(_) => {
                return Err(
                    "revoke the current debug approval before changing its duration".into(),
                );
            }
            LeaseState::Disabled { .. } => {
                return Err("a CLI debug session must be requested before enabling".into());
            }
        };
        pending.approved_duration = Some(duration);
        self.audit(
            "enable",
            &format!("armed_exact_session:{}", duration.key()),
            None,
            Some(&pending.run_id),
            None,
        )?;
        state.lease = LeaseState::Armed(pending);
        Ok(status_from_state(
            &state.lease,
            &self.instance_nonce,
            now_epoch,
            now,
        ))
    }

    pub fn revoke(&self, reason: &str) -> Result<DebugApprovalStatus, String> {
        let now_epoch = now_epoch_seconds();
        let now = Instant::now();
        let mut state = self
            .state
            .lock()
            .map_err(|_| "debug approval state unavailable")?;
        let (lease_hash, run_id, lease_epoch) = match active_lease(&state.lease) {
            Some(active) => (
                Some(active.lease_hash.clone()),
                Some(active.run_id.clone()),
                Some(active.lease_epoch),
            ),
            None => (None, None, None),
        };
        self.audit(
            "revoke",
            reason,
            lease_hash.as_deref(),
            run_id.as_deref(),
            lease_epoch,
        )?;
        state.lease = LeaseState::Disabled {
            reason: reason.to_string(),
        };
        state.operators.clear();
        state.consumed_execution_jtis.clear();
        Ok(status_from_state(
            &state.lease,
            &self.instance_nonce,
            now_epoch,
            now,
        ))
    }

    pub fn start_session(
        &self,
        request: DebugSessionStartRequest,
    ) -> Result<DebugSessionStartResponse, String> {
        let now_epoch = now_epoch_seconds();
        let now = Instant::now();
        let mut state = self
            .state
            .lock()
            .map_err(|_| "debug approval state unavailable")?;
        self.expire_if_needed(&mut state, now);
        if state.audit_degraded {
            return Err("debug approval audit is degraded; claiming is blocked".into());
        }
        let candidate = pending_from_request(&request, &state.guardians)?;
        let approved = match &state.lease {
            LeaseState::Armed(pending) => pending.clone(),
            LeaseState::Active(_) => return Err("debug approval is already active".into()),
            _ => return Err("exact debug session has not been confirmed in Launcher".into()),
        };
        if !pending_matches(&approved, &candidate) {
            return Err("debug session claim does not match the Launcher-confirmed request".into());
        }
        let current_fingerprint = process_fingerprint(candidate.process_id)?;
        if current_fingerprint != approved.process_fingerprint {
            return Err("debug guardian process identity changed before claim".into());
        }
        let duration = approved
            .approved_duration
            .ok_or_else(|| "debug approval duration was not confirmed".to_string())?;
        let session_secret = random_identifier("debug-session-secret");
        let lease_epoch = state.next_lease_epoch;
        state.next_lease_epoch = state.next_lease_epoch.saturating_add(1);
        let lease_material = format!(
            "{}\n{}\n{}\n{}\n{}\n{}\n{}\n{}\n{}\n{}\n{}",
            self.instance_nonce,
            candidate.session_id,
            candidate.run_id,
            candidate.workspace_digest,
            candidate.pack_id,
            candidate.profile_id,
            candidate.profile_revision,
            candidate.activation_id,
            candidate.plan_digest,
            lease_epoch,
            duration.key(),
        );
        let duration_seconds = duration.seconds();
        let lease = ActiveLease {
            session_id: candidate.session_id,
            run_id: candidate.run_id,
            workspace: candidate.workspace,
            workspace_digest: candidate.workspace_digest,
            pack_id: candidate.pack_id,
            profile_id: candidate.profile_id,
            profile_revision: candidate.profile_revision,
            activation_id: candidate.activation_id,
            plan_digest: candidate.plan_digest,
            process_id: candidate.process_id,
            process_fingerprint: current_fingerprint,
            session_secret_hash: sha256_text(&session_secret),
            lease_epoch,
            expires_at: duration_seconds
                .map(|seconds| now_epoch.saturating_add(seconds))
                .unwrap_or(u64::MAX),
            deadline: duration_seconds.map(|seconds| now + Duration::from_secs(seconds)),
            duration,
            lease_hash: sha256_text(lease_material),
        };
        self.audit(
            "claim",
            "active",
            Some(&lease.lease_hash),
            Some(&lease.run_id),
            Some(lease.lease_epoch),
        )?;
        state.lease = LeaseState::Active(lease);
        state.operators.clear();
        let status = status_from_state(&state.lease, &self.instance_nonce, now_epoch, now);
        Ok(DebugSessionStartResponse {
            status,
            session_secret,
        })
    }

    pub fn stop_session(
        &self,
        request: DebugSessionStopRequest,
    ) -> Result<DebugApprovalStatus, String> {
        self.require_active_secret(
            &request.session_id,
            &request.run_id,
            &request.session_secret,
        )?;
        self.revoke("session_stopped")
    }

    pub fn sign_operator(&self, request: DebugOperatorRequest) -> Result<DebugCliOperator, String> {
        validate_operator_request(&request)?;
        let now_epoch = now_epoch_seconds();
        let now = Instant::now();
        let mut state = self
            .state
            .lock()
            .map_err(|_| "debug approval state unavailable")?;
        self.expire_if_needed(&mut state, now);
        let active = active_lease(&state.lease)
            .ok_or_else(|| "no active debug approval session".to_string())?
            .clone();
        require_secret(&active, &request.session_secret)?;
        if active.session_id != request.session_id
            || active.run_id != request.run_id
            || active.workspace_digest != request.workspace_digest
            || active.pack_id != request.pack_id
            || active.profile_id != request.profile_id
            || active.profile_revision != request.profile_revision
            || active.activation_id != request.activation_id
            || active.plan_digest != request.plan_digest
            || active.lease_epoch != request.lease_epoch
        {
            return Err("debug request does not match the active session binding".into());
        }
        if request.request_expires_at <= now_epoch {
            return Err("approval request has expired".into());
        }
        if let Some(existing) = state.operators.get(&request.request_id) {
            let proposed = operator_from_request(
                &request,
                now_epoch,
                existing.operator.issued_at,
                existing.operator.expires_at,
                existing.operator.nonce.clone(),
            );
            if canonical_operator_payload(&proposed)?
                == canonical_operator_payload(&existing.operator)?
            {
                return Ok(existing.operator.clone());
            }
            return Err("request already has a differently-bound debug operator".into());
        }
        let expires_at = request
            .request_expires_at
            .min(active.expires_at)
            .min(now_epoch + OPERATOR_TTL_SECONDS);
        let mut operator = operator_from_request(
            &request,
            now_epoch,
            now_epoch,
            expires_at,
            random_identifier("approval"),
        );
        operator.signature = self.sign(&operator)?;
        self.audit(
            "operator_issued",
            &operator.decision,
            Some(&active.lease_hash),
            Some(&active.run_id),
            Some(active.lease_epoch),
        )?;
        state.operators.insert(
            operator.request_id.clone(),
            OperatorRecord {
                operator: operator.clone(),
                state: OperatorState::Issued,
                execution_jti: None,
            },
        );
        Ok(operator)
    }

    pub fn verify_operator(
        &self,
        request: DebugOperatorVerifyRequest,
    ) -> Result<DebugCliOperator, String> {
        let operator = request.debug_cli_operator;
        validate_decision(&request.expected_decision)?;
        if operator.decision != request.expected_decision {
            return Err("debug operator decision does not match this endpoint".into());
        }
        self.verify_signature_and_active_binding(&operator)?;
        let mut state = self
            .state
            .lock()
            .map_err(|_| "debug approval state unavailable")?;
        let active = active_lease(&state.lease)
            .ok_or_else(|| "no active debug approval session".to_string())?
            .clone();
        let record = state
            .operators
            .get_mut(&operator.request_id)
            .ok_or_else(|| "debug operator was not issued by this Launcher".to_string())?;
        if record.operator != operator {
            return Err("debug operator differs from the issued operator".into());
        }
        match record.state {
            OperatorState::Issued | OperatorState::Settling | OperatorState::ResumeFailed => {
                self.audit(
                    "operator_verified",
                    "settling_idempotent",
                    Some(&active.lease_hash),
                    Some(&active.run_id),
                    Some(active.lease_epoch),
                )?;
                record.state = OperatorState::Settling;
                Ok(operator)
            }
            OperatorState::Settled => Ok(operator),
            OperatorState::ExecutionConsumed => Err("debug operator execution was consumed".into()),
        }
    }

    pub fn settle_operator(
        &self,
        request: DebugOperatorSettleRequest,
    ) -> Result<DebugCliOperator, String> {
        self.verify_signature_and_active_binding(&request.debug_cli_operator)?;
        let next = match request.outcome.as_str() {
            "settled" => OperatorState::Settled,
            "resume_failed" => OperatorState::ResumeFailed,
            _ => return Err("invalid debug operator settlement outcome".into()),
        };
        let mut state = self
            .state
            .lock()
            .map_err(|_| "debug approval state unavailable")?;
        let active = active_lease(&state.lease)
            .ok_or_else(|| "no active debug approval session".to_string())?
            .clone();
        let record = state
            .operators
            .get_mut(&request.debug_cli_operator.request_id)
            .ok_or_else(|| "debug operator was not issued by this Launcher".to_string())?;
        if record.operator != request.debug_cli_operator {
            return Err("debug operator differs from the issued operator".into());
        }
        match (record.state, next) {
            (OperatorState::Settling, _) => {}
            // The CLI settles an approved operator before executing the
            // one-shot token because consume_execution() accepts only that
            // state. If deterministic replay then fails before consumption,
            // preserve the failure outcome without reopening execution.
            (OperatorState::Settled, OperatorState::ResumeFailed) => {}
            (OperatorState::Settled, OperatorState::Settled)
            | (OperatorState::ResumeFailed, OperatorState::ResumeFailed) => {
                return Ok(record.operator.clone());
            }
            (OperatorState::ExecutionConsumed, _) => {
                return Err("debug operator execution was already consumed".into());
            }
            _ => return Err("debug operator is not ready for settlement".into()),
        }
        self.audit(
            "operator_settlement",
            request.outcome.as_str(),
            Some(&active.lease_hash),
            Some(&active.run_id),
            Some(active.lease_epoch),
        )?;
        record.state = next;
        Ok(record.operator.clone())
    }

    pub fn consume_execution(&self, request: DebugExecutionConsumeRequest) -> Result<(), String> {
        validate_identifier(&request.execution_jti, "execution_jti")?;
        let mut state = self
            .state
            .lock()
            .map_err(|_| "debug approval state unavailable")?;
        self.expire_if_needed(&mut state, Instant::now());
        let active = active_lease(&state.lease)
            .ok_or_else(|| "debug approval was revoked or expired".to_string())?
            .clone();
        if active.lease_epoch != request.lease_epoch {
            return Err("debug execution lease was revoked".into());
        }
        if state
            .consumed_execution_jtis
            .contains(&request.execution_jti)
        {
            let record = state
                .operators
                .get(&request.request_id)
                .ok_or_else(|| "debug execution has no active operator".to_string())?;
            if record.execution_jti.as_deref() == Some(request.execution_jti.as_str())
                && record.state == OperatorState::ExecutionConsumed
            {
                return Ok(());
            }
            return Err("debug execution token has already been consumed".into());
        }
        let record = state
            .operators
            .get_mut(&request.request_id)
            .ok_or_else(|| "debug execution has no active operator".to_string())?;
        if record.operator.lease_epoch != request.lease_epoch
            || record.operator.decision != "approve"
        {
            return Err("debug execution binding mismatch".into());
        }
        if record.state != OperatorState::Settled {
            return Err("debug approval has not settled".into());
        }
        self.audit(
            "execution_consumed",
            "once",
            Some(&active.lease_hash),
            Some(&active.run_id),
            Some(active.lease_epoch),
        )?;
        record.state = OperatorState::ExecutionConsumed;
        record.execution_jti = Some(request.execution_jti.clone());
        state.consumed_execution_jtis.insert(request.execution_jti);
        Ok(())
    }

    fn require_active_secret(
        &self,
        session_id: &str,
        run_id: &str,
        secret: &str,
    ) -> Result<(), String> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| "debug approval state unavailable")?;
        self.expire_if_needed(&mut state, Instant::now());
        let active = active_lease(&state.lease)
            .ok_or_else(|| "no active debug approval session".to_string())?;
        if active.session_id != session_id || active.run_id != run_id {
            return Err("debug session binding mismatch".into());
        }
        require_secret(active, secret)
    }

    fn verify_signature_and_active_binding(
        &self,
        operator: &DebugCliOperator,
    ) -> Result<(), String> {
        validate_operator(operator)?;
        let signature =
            hex::decode(&operator.signature).map_err(|_| "debug operator signature is invalid")?;
        let unsigned = canonical_operator_payload(operator)?;
        let mut mac = HmacSha256::new_from_slice(&self.signing_key)
            .map_err(|_| "debug signing key unavailable")?;
        mac.update(unsigned.as_bytes());
        mac.verify_slice(&signature)
            .map_err(|_| "debug operator signature is invalid".to_string())?;
        let now_epoch = now_epoch_seconds();
        let mut state = self
            .state
            .lock()
            .map_err(|_| "debug approval state unavailable")?;
        self.expire_if_needed(&mut state, Instant::now());
        let active = active_lease(&state.lease)
            .ok_or_else(|| "debug approval was revoked or expired".to_string())?;
        if operator.session_id != active.session_id
            || operator.run_id != active.run_id
            || operator.workspace_digest != active.workspace_digest
            || operator.pack_id != active.pack_id
            || operator.profile_id != active.profile_id
            || operator.profile_revision != active.profile_revision
            || operator.activation_id != active.activation_id
            || operator.plan_digest != active.plan_digest
            || operator.lease_epoch != active.lease_epoch
        {
            return Err("debug operator active lease binding mismatch".into());
        }
        if operator.issued_at > now_epoch
            || operator.expires_at <= now_epoch
            || operator.expires_at > active.expires_at
        {
            return Err("debug operator expired or has invalid timestamps".into());
        }
        Ok(())
    }

    fn sign(&self, operator: &DebugCliOperator) -> Result<String, String> {
        let unsigned = canonical_operator_payload(operator)?;
        let mut mac = HmacSha256::new_from_slice(&self.signing_key)
            .map_err(|_| "debug signing key unavailable")?;
        mac.update(unsigned.as_bytes());
        Ok(hex::encode(mac.finalize().into_bytes()))
    }

    fn expire_if_needed(&self, state: &mut DebugApprovalState, now: Instant) {
        if state.audit_degraded
            && self
                .audit("audit_recovery_probe", "recovered", None, None, None)
                .is_ok()
        {
            state.audit_degraded = false;
        }

        let expiration = match &state.lease {
            LeaseState::Pending(pending) | LeaseState::Armed(pending) => {
                if pending.deadline <= now {
                    Some(("expired", None, Some(pending.run_id.clone()), None))
                } else if !state.guardians.contains_key(&pending.run_id) {
                    Some(("guardian_missing", None, Some(pending.run_id.clone()), None))
                } else {
                    None
                }
            }
            LeaseState::Active(active) => {
                if active.deadline.is_some_and(|deadline| deadline <= now) {
                    Some((
                        "expired",
                        Some(active.lease_hash.clone()),
                        Some(active.run_id.clone()),
                        Some(active.lease_epoch),
                    ))
                } else {
                    match state.guardians.get(&active.run_id) {
                        None => Some((
                            "guardian_missing",
                            Some(active.lease_hash.clone()),
                            Some(active.run_id.clone()),
                            Some(active.lease_epoch),
                        )),
                        Some(guardian) if guardian.process_id != active.process_id => Some((
                            "internal_invariant_failure",
                            Some(active.lease_hash.clone()),
                            Some(active.run_id.clone()),
                            Some(active.lease_epoch),
                        )),
                        Some(guardian)
                            if guardian.execution_identity.profile_id != active.profile_id
                                || guardian.execution_identity.profile_revision
                                    != active.profile_revision
                                || guardian.execution_identity.activation_id
                                    != active.activation_id
                                || guardian.execution_identity.plan_digest
                                    != active.plan_digest =>
                        {
                            Some((
                                "internal_invariant_failure",
                                Some(active.lease_hash.clone()),
                                Some(active.run_id.clone()),
                                Some(active.lease_epoch),
                            ))
                        }
                        Some(guardian) => match process_fingerprint(active.process_id) {
                            Err(_) => Some((
                                "guardian_missing",
                                Some(active.lease_hash.clone()),
                                Some(active.run_id.clone()),
                                Some(active.lease_epoch),
                            )),
                            Ok(fingerprint)
                                if fingerprint != active.process_fingerprint
                                    || fingerprint != guardian.process_fingerprint =>
                            {
                                Some((
                                    "guardian_changed",
                                    Some(active.lease_hash.clone()),
                                    Some(active.run_id.clone()),
                                    Some(active.lease_epoch),
                                ))
                            }
                            Ok(_) => None,
                        },
                    }
                }
            }
            LeaseState::Disabled { .. } => None,
        };

        let Some((reason, lease_hash, run_id, lease_epoch)) = expiration else {
            return;
        };
        // Fail closed before touching the durable log.  A broken audit sink
        // must never extend the authority lifetime.
        state.lease = LeaseState::Disabled {
            reason: reason.to_string(),
        };
        state.operators.clear();
        state.consumed_execution_jtis.clear();
        if self
            .audit(
                "automatic_revoke",
                reason,
                lease_hash.as_deref(),
                run_id.as_deref(),
                lease_epoch,
            )
            .is_err()
        {
            state.audit_degraded = true;
            state.lease = LeaseState::Disabled {
                reason: "audit_degraded".into(),
            };
        }
    }

    fn audit(
        &self,
        event: &str,
        result: &str,
        lease_hash: Option<&str>,
        run_id: Option<&str>,
        lease_epoch: Option<u64>,
    ) -> Result<(), String> {
        let payload = json!({
            "ts": now_epoch_seconds(),
            "event": event,
            "result": result,
            "decision_source": "delegated_debug_cli",
            "instance_nonce_hash": sha256_text(&self.instance_nonce),
            "lease_hash": lease_hash,
            "lease_epoch": lease_epoch,
            "run_id": run_id,
        });
        if let Some(parent) = self.audit_path.parent() {
            std::fs::create_dir_all(parent)
                .map_err(|error| format!("debug approval audit directory unavailable: {error}"))?;
        }
        let mut options = OpenOptions::new();
        options.create(true).append(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let mut file = options
            .open(&self.audit_path)
            .map_err(|error| format!("debug approval audit unavailable: {error}"))?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            file.set_permissions(std::fs::Permissions::from_mode(0o600))
                .map_err(|error| format!("debug approval audit permissions failed: {error}"))?;
        }
        writeln!(file, "{payload}")
            .map_err(|error| format!("debug approval audit write failed: {error}"))?;
        file.sync_all()
            .map_err(|error| format!("debug approval audit fsync failed: {error}"))
    }
}

fn request_execution_identity(
    request: &DebugSessionStartRequest,
) -> Result<ExecutionProfileIdentity, String> {
    ExecutionProfileIdentity::new(
        request.profile_id.clone(),
        request.profile_revision.clone(),
        request.activation_id.clone(),
        request.plan_digest.clone(),
    )
    .map_err(|_| "debug session execution Profile identity is invalid".into())
}

fn pending_from_request(
    request: &DebugSessionStartRequest,
    guardians: &HashMap<String, GuardianRecord>,
) -> Result<PendingSession, String> {
    for (value, name) in [
        (&request.session_id, "session_id"),
        (&request.run_id, "run_id"),
        (&request.pack_id, "pack_id"),
        (&request.profile_id, "profile_id"),
    ] {
        validate_identifier(value, name)?;
    }
    if request.claim_secret.len() < 32 {
        return Err("debug session claim secret is invalid".into());
    }
    let requested_identity = request_execution_identity(request)?;
    let workspace = canonical_workspace(&request.workspace)?;
    let guardian = guardians
        .get(&request.run_id)
        .ok_or_else(|| "run id is not a live Launcher-owned Defaultspack child".to_string())?;
    if guardian.workspace != workspace {
        return Err("workspace does not match the Launcher-owned Defaultspack child".into());
    }
    if !guardian.execution_identity.matches(&requested_identity) {
        return Err("execution Profile identity does not match the Launcher-owned guardian".into());
    }
    let process_fingerprint = process_fingerprint(guardian.process_id)?;
    if process_fingerprint != guardian.process_fingerprint {
        return Err("Launcher-owned guardian identity changed".into());
    }
    if guardian.executable_identity.trim().is_empty() {
        return Err("Launcher-owned guardian executable identity is unavailable".into());
    }
    let now = Instant::now();
    Ok(PendingSession {
        session_id: request.session_id.clone(),
        run_id: request.run_id.clone(),
        workspace_digest: sha256_text(workspace.to_string_lossy().as_bytes()),
        workspace,
        pack_id: request.pack_id.clone(),
        profile_id: request.profile_id.clone(),
        profile_revision: request.profile_revision.clone(),
        activation_id: request.activation_id.clone(),
        plan_digest: request.plan_digest.clone(),
        process_id: guardian.process_id,
        process_fingerprint,
        claim_secret_hash: sha256_text(&request.claim_secret),
        approved_duration: None,
        expires_at: now_epoch_seconds() + REQUEST_TTL.as_secs(),
        deadline: now + REQUEST_TTL,
    })
}

fn pending_matches(left: &PendingSession, right: &PendingSession) -> bool {
    left.session_id == right.session_id
        && left.run_id == right.run_id
        && left.workspace == right.workspace
        && left.workspace_digest == right.workspace_digest
        && left.pack_id == right.pack_id
        && left.profile_id == right.profile_id
        && left.profile_revision == right.profile_revision
        && left.activation_id == right.activation_id
        && left.plan_digest == right.plan_digest
        && left.process_id == right.process_id
        && left.process_fingerprint == right.process_fingerprint
        && left.claim_secret_hash == right.claim_secret_hash
}

fn operator_from_request(
    request: &DebugOperatorRequest,
    _now: u64,
    issued_at: u64,
    expires_at: u64,
    nonce: String,
) -> DebugCliOperator {
    DebugCliOperator {
        kind: "debug_cli_operator".into(),
        version: 2,
        origin: "launcher_debug_cli".into(),
        scope: "once".into(),
        session_id: request.session_id.clone(),
        run_id: request.run_id.clone(),
        workspace_digest: request.workspace_digest.clone(),
        pack_id: request.pack_id.clone(),
        profile_id: request.profile_id.clone(),
        profile_revision: request.profile_revision.clone(),
        activation_id: request.activation_id.clone(),
        plan_digest: request.plan_digest.clone(),
        lease_epoch: request.lease_epoch,
        request_id: request.request_id.clone(),
        permission_id: request.permission_id.clone(),
        tool: request.tool.clone(),
        action: request.action.clone(),
        operation: request.operation.clone(),
        decision: request.decision.clone(),
        canonical_arguments_digest: request.canonical_arguments_digest.clone(),
        target_digest: request.target_digest.clone(),
        conversation_id: request.conversation_id.clone(),
        operation_owner: request.operation_owner.clone(),
        issued_at,
        expires_at,
        nonce,
        signature: String::new(),
    }
}

fn canonical_operator_payload(operator: &DebugCliOperator) -> Result<String, String> {
    serde_json::to_string(&json!({
        "kind": operator.kind,
        "version": operator.version,
        "origin": operator.origin,
        "scope": operator.scope,
        "session_id": operator.session_id,
        "run_id": operator.run_id,
        "workspace_digest": operator.workspace_digest,
        "pack_id": operator.pack_id,
        "profile_id": operator.profile_id,
        "profile_revision": operator.profile_revision,
        "activation_id": operator.activation_id,
        "plan_digest": operator.plan_digest,
        "lease_epoch": operator.lease_epoch,
        "request_id": operator.request_id,
        "permission_id": operator.permission_id,
        "tool": operator.tool,
        "action": operator.action,
        "operation": operator.operation,
        "decision": operator.decision,
        "canonical_arguments_digest": operator.canonical_arguments_digest,
        "target_digest": operator.target_digest,
        "conversation_id": operator.conversation_id,
        "operation_owner": operator.operation_owner,
        "issued_at": operator.issued_at,
        "expires_at": operator.expires_at,
        "nonce": operator.nonce,
    }))
    .map_err(|error| format!("failed to encode debug operator: {error}"))
}

fn status_from_state(
    state: &LeaseState,
    instance_nonce: &str,
    now_epoch: u64,
    now: Instant,
) -> DebugApprovalStatus {
    let base = |state: &str, reason: Option<String>| DebugApprovalStatus {
        state: state.to_string(),
        reason,
        armed_remaining_seconds: None,
        session_id: None,
        run_id: None,
        workspace: None,
        workspace_digest: None,
        pack_id: None,
        profile_id: None,
        profile_revision: None,
        activation_id: None,
        plan_digest: None,
        guardian_owned: false,
        lease_epoch: None,
        expires_at: None,
        duration: None,
        instance_nonce: instance_nonce.to_string(),
    };
    match state {
        LeaseState::Disabled { reason } => base("disabled", Some(reason.clone())),
        LeaseState::Pending(pending) | LeaseState::Armed(pending) => {
            let mut status = base(
                if matches!(state, LeaseState::Armed(_)) {
                    "armed"
                } else {
                    "pending"
                },
                None,
            );
            status.armed_remaining_seconds =
                Some(pending.deadline.saturating_duration_since(now).as_secs());
            status.session_id = Some(pending.session_id.clone());
            status.run_id = Some(pending.run_id.clone());
            status.workspace = Some(pending.workspace.to_string_lossy().into_owned());
            status.workspace_digest = Some(pending.workspace_digest.clone());
            status.pack_id = Some(pending.pack_id.clone());
            status.profile_id = Some(pending.profile_id.clone());
            status.profile_revision = Some(pending.profile_revision.clone());
            status.activation_id = Some(pending.activation_id.clone());
            status.plan_digest = Some(pending.plan_digest.clone());
            status.guardian_owned = true;
            status.expires_at = Some(pending.expires_at.max(now_epoch));
            status.duration = pending
                .approved_duration
                .map(|duration| duration.key().to_string());
            status
        }
        LeaseState::Active(active) => {
            let mut status = base("active", None);
            status.session_id = Some(active.session_id.clone());
            status.run_id = Some(active.run_id.clone());
            status.workspace = Some(active.workspace.to_string_lossy().into_owned());
            status.workspace_digest = Some(active.workspace_digest.clone());
            status.pack_id = Some(active.pack_id.clone());
            status.profile_id = Some(active.profile_id.clone());
            status.profile_revision = Some(active.profile_revision.clone());
            status.activation_id = Some(active.activation_id.clone());
            status.plan_digest = Some(active.plan_digest.clone());
            status.guardian_owned = true;
            status.lease_epoch = Some(active.lease_epoch);
            status.expires_at = active.duration.seconds().map(|_| active.expires_at);
            status.duration = Some(active.duration.key().to_string());
            status
        }
    }
}

fn active_lease(state: &LeaseState) -> Option<&ActiveLease> {
    match state {
        LeaseState::Active(active) => Some(active),
        _ => None,
    }
}

fn require_secret(active: &ActiveLease, supplied: &str) -> Result<(), String> {
    let supplied_hash = sha256_text(supplied);
    if supplied.len() < 32 || !constant_time_equal(&supplied_hash, &active.session_secret_hash) {
        return Err("debug session secret is invalid".into());
    }
    Ok(())
}

fn validate_operator_request(request: &DebugOperatorRequest) -> Result<(), String> {
    operator_request_execution_identity(request)?;
    validate_digest(&request.workspace_digest, "workspace_digest")?;
    validate_digest(
        &request.canonical_arguments_digest,
        "canonical_arguments_digest",
    )?;
    if let Some(target_digest) = request.target_digest.as_deref() {
        validate_digest(target_digest, "target_digest")?;
    }
    validate_decision(&request.decision)?;
    for (value, name) in [
        (&request.session_id, "session_id"),
        (&request.run_id, "run_id"),
        (&request.pack_id, "pack_id"),
        (&request.profile_id, "profile_id"),
        (&request.request_id, "request_id"),
        (&request.permission_id, "permission_id"),
        (&request.tool, "tool"),
        (&request.action, "action"),
        (&request.operation, "operation"),
        (&request.conversation_id, "conversation_id"),
        (&request.operation_owner, "operation_owner"),
    ] {
        validate_identifier(value, name)?;
    }
    Ok(())
}

fn validate_operator(operator: &DebugCliOperator) -> Result<(), String> {
    if operator.kind != "debug_cli_operator"
        || operator.version != 2
        || operator.origin != "launcher_debug_cli"
        || operator.scope != "once"
    {
        return Err("debug operator provenance is invalid".into());
    }
    operator_execution_identity(operator)?;
    validate_digest(&operator.workspace_digest, "workspace_digest")?;
    validate_digest(
        &operator.canonical_arguments_digest,
        "canonical_arguments_digest",
    )?;
    if let Some(target_digest) = operator.target_digest.as_deref() {
        validate_digest(target_digest, "target_digest")?;
    }
    validate_decision(&operator.decision)
}

fn operator_request_execution_identity(
    request: &DebugOperatorRequest,
) -> Result<ExecutionProfileIdentity, String> {
    ExecutionProfileIdentity::new(
        request.profile_id.clone(),
        request.profile_revision.clone(),
        request.activation_id.clone(),
        request.plan_digest.clone(),
    )
    .map_err(|_| "debug request execution Profile identity is invalid".into())
}

fn operator_execution_identity(
    operator: &DebugCliOperator,
) -> Result<ExecutionProfileIdentity, String> {
    ExecutionProfileIdentity::new(
        operator.profile_id.clone(),
        operator.profile_revision.clone(),
        operator.activation_id.clone(),
        operator.plan_digest.clone(),
    )
    .map_err(|_| "debug operator execution Profile identity is invalid".into())
}

fn validate_decision(decision: &str) -> Result<(), String> {
    if matches!(decision, "approve" | "deny") {
        Ok(())
    } else {
        Err("debug decision must be approve or deny".into())
    }
}

#[cfg(windows)]
fn retain_process_handle(process_id: u32) -> Result<std::os::windows::io::OwnedHandle, String> {
    use std::os::windows::io::{FromRawHandle, OwnedHandle};
    use windows_sys::Win32::System::Threading::{OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION};

    // SYNCHRONIZE is a standard access right, but windows-sys 0.61 does not
    // expose it for process handles.
    const PROCESS_SYNCHRONIZE_ACCESS: u32 = 0x0010_0000;

    // Keeping this handle open ties the guardian record to the concrete
    // process object even if Windows later reuses its numeric PID.
    let raw = unsafe {
        OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE_ACCESS,
            0,
            process_id,
        )
    };
    if raw.is_null() {
        return Err("failed to retain Launcher-owned guardian process handle".into());
    }
    Ok(unsafe { OwnedHandle::from_raw_handle(raw.cast()) })
}

fn process_fingerprint(process_id: u32) -> Result<String, String> {
    if process_id == 0 {
        return Err("debug guardian process id is invalid".into());
    }
    #[cfg(unix)]
    {
        // PID + owner + process birth time identifies the guardian without
        // depending on `comm`, which a process may legitimately rename.
        let output = std::process::Command::new("/bin/ps")
            .args(["-p", &process_id.to_string(), "-o", "uid=", "-o", "lstart="])
            .output()
            .map_err(|_| "failed to inspect debug guardian process")?;
        if !output.status.success() {
            return Err("debug guardian process is not running".into());
        }
        let facts = String::from_utf8_lossy(&output.stdout).trim().to_string();
        if facts.is_empty() {
            return Err("debug guardian process identity is unavailable".into());
        }
        Ok(sha256_text(facts))
    }
    #[cfg(not(unix))]
    {
        // CIM can fail transiently when several registrations query it at
        // once. Serialize identity snapshots so every guardian is checked
        // against one complete, fail-closed process record.
        static PROCESS_INSPECTION_LOCK: std::sync::OnceLock<std::sync::Mutex<()>> =
            std::sync::OnceLock::new();
        let _inspection_guard = PROCESS_INSPECTION_LOCK
            .get_or_init(|| std::sync::Mutex::new(()))
            .lock()
            .map_err(|_| "debug guardian process inspection is unavailable")?;
        let script = format!(
            "$p=Get-CimInstance -ClassName Win32_Process -Filter 'ProcessId = {process_id}';\
             if($null -eq $p){{exit 3}};\
             $o=Invoke-CimMethod -InputObject $p -MethodName GetOwnerSid;\
             $parentFilter='ProcessId = '+[string]$p.ParentProcessId;\
             $pp=Get-CimInstance -ClassName Win32_Process -Filter $parentFilter;\
             [ordered]@{{pid=[uint32]$p.ProcessId;creation=[string]$p.CreationDate;\
             owner_sid=[string]$o.Sid;executable=[string]$p.ExecutablePath;\
             parent=[uint32]$p.ParentProcessId;parent_creation=[string]$pp.CreationDate;\
             parent_executable=[string]$pp.ExecutablePath}}|ConvertTo-Json -Compress"
        );
        let output = crate::process_utils::command("powershell.exe")
            .args(["-NoProfile", "-NonInteractive", "-Command", &script])
            .output()
            .map_err(|_| "failed to inspect debug guardian process")?;
        let facts = String::from_utf8_lossy(&output.stdout).trim().to_string();
        if !output.status.success() || facts.is_empty() {
            return Err("debug guardian process is not running".into());
        }
        let parsed: serde_json::Value = serde_json::from_str(&facts)
            .map_err(|_| "debug guardian process identity is unavailable")?;
        windows_process_fingerprint(&parsed)
    }
}

#[cfg(any(windows, test))]
fn windows_process_fingerprint(facts: &serde_json::Value) -> Result<String, String> {
    let pid = facts
        .get("pid")
        .and_then(serde_json::Value::as_u64)
        .unwrap_or(0);
    let parent = facts
        .get("parent")
        .and_then(serde_json::Value::as_u64)
        .unwrap_or(0);
    let creation = facts
        .get("creation")
        .and_then(serde_json::Value::as_str)
        .unwrap_or("");
    let owner_sid = facts
        .get("owner_sid")
        .and_then(serde_json::Value::as_str)
        .unwrap_or("");
    let executable = facts
        .get("executable")
        .and_then(serde_json::Value::as_str)
        .unwrap_or("");
    let parent_creation = facts
        .get("parent_creation")
        .and_then(serde_json::Value::as_str)
        .unwrap_or("");
    let parent_executable = facts
        .get("parent_executable")
        .and_then(serde_json::Value::as_str)
        .unwrap_or("");
    if pid == 0
        || parent == 0
        || creation.is_empty()
        || owner_sid.is_empty()
        || executable.is_empty()
        || parent_creation.is_empty()
        || parent_executable.is_empty()
    {
        return Err("debug guardian process identity is unavailable".into());
    }
    Ok(sha256_text(format!(
        "{pid}\n{creation}\n{owner_sid}\n{executable}\n{parent}\n{parent_creation}\n{parent_executable}"
    )))
}

fn canonical_workspace(value: &str) -> Result<PathBuf, String> {
    let raw = Path::new(value);
    if !raw.is_absolute() {
        return Err("workspace must be an absolute path".into());
    }
    let canonical = raw
        .canonicalize()
        .map_err(|_| "workspace must exist and be canonicalizable".to_string())?;
    if !canonical.is_dir() {
        return Err("workspace must be a directory".into());
    }
    Ok(canonical)
}

fn validate_identifier(value: &str, name: &str) -> Result<(), String> {
    let trimmed = value.trim();
    if !(1..=512).contains(&trimmed.len()) || trimmed.chars().any(char::is_control) {
        return Err(format!("{name} is invalid"));
    }
    Ok(())
}

fn validate_digest(value: &str, name: &str) -> Result<(), String> {
    if value.len() != 64 || !value.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err(format!("{name} must be a sha256 digest"));
    }
    Ok(())
}

fn random_identifier(prefix: &str) -> String {
    let suffix: String = rand::thread_rng()
        .sample_iter(&Alphanumeric)
        .take(32)
        .map(char::from)
        .collect();
    format!("{prefix}-{suffix}")
}

fn sha256_text(value: impl AsRef<[u8]>) -> String {
    hex::encode(Sha256::digest(value.as_ref()))
}

fn constant_time_equal(left: &str, right: &str) -> bool {
    left.len() == right.len()
        && left
            .bytes()
            .zip(right.bytes())
            .fold(0_u8, |difference, (a, b)| difference | (a ^ b))
            == 0
}

fn now_epoch_seconds() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;

    fn manager() -> DebugApprovalManager {
        let manager = DebugApprovalManager::new(std::env::temp_dir().join(format!(
            "tobkiri-debug-approval-test-{}.jsonl",
            random_identifier("audit")
        )));
        manager
            .register_guardian(
                "run-12345678".into(),
                std::process::id(),
                "test-defaultspack".into(),
                std::env::temp_dir(),
                8766,
                {
                    let path = std::env::temp_dir().join(format!(
                        "tobkiri-debug-api-token-{}",
                        random_identifier("test")
                    ));
                    std::fs::write(&path, "test-token").unwrap();
                    path
                },
                test_execution_identity(),
            )
            .unwrap();
        manager
    }

    fn request(workspace: &Path) -> DebugSessionStartRequest {
        DebugSessionStartRequest {
            session_id: "session-12345678".into(),
            run_id: "run-12345678".into(),
            workspace: workspace.to_string_lossy().into_owned(),
            pack_id: "defaultspack".into(),
            profile_id: "defaults".into(),
            profile_revision: format!("sha256:{}", "a".repeat(64)),
            activation_id: "activation:defaults-test".into(),
            plan_digest: format!("sha256:{}", "b".repeat(64)),
            claim_secret: "claim-secret-which-is-at-least-thirty-two-bytes".into(),
        }
    }

    fn test_execution_identity() -> ExecutionProfileIdentity {
        ExecutionProfileIdentity::new(
            "defaults",
            format!("sha256:{}", "a".repeat(64)),
            "activation:defaults-test",
            format!("sha256:{}", "b".repeat(64)),
        )
        .unwrap()
    }

    fn active(manager: &DebugApprovalManager) -> (DebugApprovalStatus, String) {
        let request = request(&std::env::temp_dir());
        manager.register_session(request.clone()).unwrap();
        manager.arm("1h").unwrap();
        let result = manager.start_session(request).unwrap();
        (result.status, result.session_secret)
    }

    fn operator_request(
        status: &DebugApprovalStatus,
        session_secret: &str,
        decision: &str,
    ) -> DebugOperatorRequest {
        DebugOperatorRequest {
            session_id: status.session_id.clone().unwrap(),
            run_id: status.run_id.clone().unwrap(),
            workspace_digest: status.workspace_digest.clone().unwrap(),
            pack_id: status.pack_id.clone().unwrap(),
            profile_id: status.profile_id.clone().unwrap(),
            profile_revision: status.profile_revision.clone().unwrap(),
            activation_id: status.activation_id.clone().unwrap(),
            plan_digest: status.plan_digest.clone().unwrap(),
            lease_epoch: status.lease_epoch.unwrap(),
            session_secret: session_secret.into(),
            request_id: "apr-12345678".into(),
            permission_id: "computer.control".into(),
            tool: "computer_use".into(),
            action: "computer.type".into(),
            operation: "computer.type".into(),
            decision: decision.into(),
            canonical_arguments_digest: "a".repeat(64),
            target_digest: Some("b".repeat(64)),
            conversation_id: "conversation-1234".into(),
            operation_owner: "defaultspack".into(),
            request_expires_at: now_epoch_seconds() + 60,
        }
    }

    #[test]
    fn requires_registered_exact_session_before_native_arm() {
        let manager = manager();
        assert_eq!(manager.status().state, "disabled");
        assert!(manager.arm("1h").is_err());
        let request = request(&std::env::temp_dir());
        assert_eq!(manager.register_session(request).unwrap().state, "pending");
    }

    #[test]
    fn rejects_cli_run_id_that_is_not_launcher_owned() {
        let manager = manager();
        let mut forged = request(&std::env::temp_dir());
        forged.run_id = "unrelated-long-lived-process".into();
        assert!(manager
            .register_session(forged)
            .unwrap_err()
            .contains("Launcher-owned"));
    }

    #[test]
    fn rejects_debug_session_for_a_different_execution_profile_identity() {
        let manager = manager();
        let mut forged = request(&std::env::temp_dir());
        forged.activation_id = "activation:other-profile-test".into();
        let error = manager.register_session(forged).unwrap_err();
        assert!(error.contains("execution Profile identity"));
    }

    #[test]
    fn selected_duration_is_bound_to_the_active_lease() {
        for (key, seconds) in [
            ("1h", 60 * 60),
            ("1d", 24 * 60 * 60),
            ("1w", 7 * 24 * 60 * 60),
            ("1mo", 30 * 24 * 60 * 60),
        ] {
            let manager = manager();
            let request = request(&std::env::temp_dir());
            manager.register_session(request.clone()).unwrap();
            let armed = manager.arm(key).unwrap();
            assert_eq!(armed.duration.as_deref(), Some(key));
            let before = now_epoch_seconds();
            let active = manager.start_session(request).unwrap().status;
            assert_eq!(active.duration.as_deref(), Some(key));
            assert!(active.expires_at.unwrap() >= before + seconds);
            assert_eq!(manager.arm(key).unwrap().state, "active");
            assert!(manager.arm("1h").is_err() || key == "1h");
        }
    }

    #[test]
    fn permanent_duration_has_no_wall_clock_expiry() {
        let manager = manager();
        let request = request(&std::env::temp_dir());
        manager.register_session(request.clone()).unwrap();
        manager.arm("permanent").unwrap();
        let active = manager.start_session(request).unwrap().status;
        assert_eq!(active.state, "active");
        assert_eq!(active.duration.as_deref(), Some("permanent"));
        assert_eq!(active.expires_at, None);
    }

    #[test]
    fn guardian_removal_revokes_and_durably_audits_reason() {
        let manager = manager();
        let (status, _) = active(&manager);
        manager.unregister_guardian(status.run_id.as_deref().unwrap());

        let disabled = manager.status();
        assert_eq!(disabled.state, "disabled");
        assert_eq!(disabled.reason.as_deref(), Some("guardian_missing"));
        let audit = std::fs::read_to_string(&manager.audit_path).unwrap();
        assert!(audit.contains("\"event\":\"automatic_revoke\""));
        assert!(audit.contains("\"result\":\"guardian_missing\""));
    }

    #[test]
    fn automatic_expiry_is_durable() {
        let manager = manager();
        active(&manager);
        {
            let mut state = manager.state.lock().unwrap();
            let LeaseState::Active(active) = &mut state.lease else {
                panic!("expected active lease");
            };
            active.deadline = Some(Instant::now() - Duration::from_secs(1));
        }

        let disabled = manager.status();
        assert_eq!(disabled.reason.as_deref(), Some("expired"));
        let audit = std::fs::read_to_string(&manager.audit_path).unwrap();
        assert!(audit.contains("\"result\":\"expired\""));
    }

    #[test]
    fn windows_fingerprint_ignores_memory_but_binds_creation_time() {
        let first = json!({
            "pid": 42,
            "creation": "20260730010203.000000+000",
            "owner_sid": "S-1-5-21-test",
            "executable": "C:\\\\Program Files\\\\Tobkiri\\\\pack-shell.exe",
            "parent": 7,
            "parent_creation": "20260730010000.000000+000",
            "parent_executable": "C:\\\\Program Files\\\\Tobkiri\\\\Tobkiri Launcher.exe",
            "memory": 1000,
        });
        let memory_changed = json!({
            "pid": 42,
            "creation": "20260730010203.000000+000",
            "owner_sid": "S-1-5-21-test",
            "executable": "C:\\\\Program Files\\\\Tobkiri\\\\pack-shell.exe",
            "parent": 7,
            "parent_creation": "20260730010000.000000+000",
            "parent_executable": "C:\\\\Program Files\\\\Tobkiri\\\\Tobkiri Launcher.exe",
            "memory": 9000,
        });
        let replaced = json!({
            "pid": 42,
            "creation": "20260730020203.000000+000",
            "owner_sid": "S-1-5-21-test",
            "executable": "C:\\\\Program Files\\\\Tobkiri\\\\pack-shell.exe",
            "parent": 7,
            "parent_creation": "20260730010000.000000+000",
            "parent_executable": "C:\\\\Program Files\\\\Tobkiri\\\\Tobkiri Launcher.exe",
            "memory": 1000,
        });
        assert_eq!(
            windows_process_fingerprint(&first).unwrap(),
            windows_process_fingerprint(&memory_changed).unwrap()
        );
        assert_ne!(
            windows_process_fingerprint(&first).unwrap(),
            windows_process_fingerprint(&replaced).unwrap()
        );
    }

    #[test]
    fn general_broker_credential_cannot_claim_or_sign() {
        let first_manager = manager();
        let mut request = request(&std::env::temp_dir());
        first_manager.register_session(request.clone()).unwrap();
        first_manager.arm("1h").unwrap();
        request.claim_secret = "wrong-claim-secret-that-is-at-least-thirty-two".into();
        assert!(first_manager.start_session(request).is_err());
        let second_manager = manager();
        let (status, secret) = active(&second_manager);
        let mut operator = operator_request(&status, &secret, "approve");
        operator.session_secret = "not-the-session-secret-at-all-xxxxxxxx".into();
        assert!(second_manager.sign_operator(operator).is_err());
    }

    #[test]
    fn operator_is_bound_to_decision_and_full_lease() {
        let manager = manager();
        let (status, secret) = active(&manager);
        let request = operator_request(&status, &secret, "approve");
        let operator = manager.sign_operator(request.clone()).unwrap();
        assert_eq!(operator.version, 2);
        assert_eq!(operator.decision, "approve");
        assert_eq!(operator.pack_id, "defaultspack");
        assert!(manager
            .verify_operator(DebugOperatorVerifyRequest {
                debug_cli_operator: operator.clone(),
                expected_decision: "deny".into(),
            })
            .is_err());
        assert!(manager
            .verify_operator(DebugOperatorVerifyRequest {
                debug_cli_operator: operator.clone(),
                expected_decision: "approve".into(),
            })
            .is_ok());
        assert_eq!(manager.sign_operator(request).unwrap(), operator);
    }

    #[test]
    fn revoke_invalidates_settled_but_unconsumed_execution() {
        let manager = manager();
        let (status, secret) = active(&manager);
        let operator = manager
            .sign_operator(operator_request(&status, &secret, "approve"))
            .unwrap();
        manager
            .verify_operator(DebugOperatorVerifyRequest {
                debug_cli_operator: operator.clone(),
                expected_decision: "approve".into(),
            })
            .unwrap();
        manager
            .settle_operator(DebugOperatorSettleRequest {
                debug_cli_operator: operator.clone(),
                outcome: "settled".into(),
            })
            .unwrap();
        manager.revoke("user_revoked").unwrap();
        assert!(manager
            .consume_execution(DebugExecutionConsumeRequest {
                request_id: operator.request_id,
                lease_epoch: operator.lease_epoch,
                execution_jti: "tok-12345678".into(),
            })
            .is_err());
    }

    #[test]
    fn settlement_cannot_reopen_consumed_operator() {
        let manager = manager();
        let (status, secret) = active(&manager);
        let operator = manager
            .sign_operator(operator_request(&status, &secret, "approve"))
            .unwrap();
        manager
            .verify_operator(DebugOperatorVerifyRequest {
                debug_cli_operator: operator.clone(),
                expected_decision: "approve".into(),
            })
            .unwrap();
        manager
            .settle_operator(DebugOperatorSettleRequest {
                debug_cli_operator: operator.clone(),
                outcome: "settled".into(),
            })
            .unwrap();
        manager
            .consume_execution(DebugExecutionConsumeRequest {
                request_id: operator.request_id.clone(),
                lease_epoch: operator.lease_epoch,
                execution_jti: "tok-first-execution".into(),
            })
            .unwrap();

        assert!(manager
            .settle_operator(DebugOperatorSettleRequest {
                debug_cli_operator: operator.clone(),
                outcome: "resume_failed".into(),
            })
            .is_err());
        assert!(manager
            .consume_execution(DebugExecutionConsumeRequest {
                request_id: operator.request_id,
                lease_epoch: operator.lease_epoch,
                execution_jti: "tok-second-execution".into(),
            })
            .is_err());
    }

    #[test]
    fn settled_operator_can_record_resume_failure_before_execution() {
        let manager = manager();
        let (status, secret) = active(&manager);
        let operator = manager
            .sign_operator(operator_request(&status, &secret, "approve"))
            .unwrap();
        manager
            .verify_operator(DebugOperatorVerifyRequest {
                debug_cli_operator: operator.clone(),
                expected_decision: "approve".into(),
            })
            .unwrap();
        manager
            .settle_operator(DebugOperatorSettleRequest {
                debug_cli_operator: operator.clone(),
                outcome: "settled".into(),
            })
            .unwrap();
        manager
            .settle_operator(DebugOperatorSettleRequest {
                debug_cli_operator: operator.clone(),
                outcome: "resume_failed".into(),
            })
            .unwrap();
        assert!(manager
            .consume_execution(DebugExecutionConsumeRequest {
                request_id: operator.request_id,
                lease_epoch: operator.lease_epoch,
                execution_jti: "tok-after-failed-resume".into(),
            })
            .is_err());
    }

    #[test]
    fn concurrent_execution_tokens_have_exactly_one_winner() {
        let manager = Arc::new(manager());
        let (status, secret) = active(&manager);
        let operator = manager
            .sign_operator(operator_request(&status, &secret, "approve"))
            .unwrap();
        manager
            .verify_operator(DebugOperatorVerifyRequest {
                debug_cli_operator: operator.clone(),
                expected_decision: "approve".into(),
            })
            .unwrap();
        manager
            .settle_operator(DebugOperatorSettleRequest {
                debug_cli_operator: operator.clone(),
                outcome: "settled".into(),
            })
            .unwrap();

        let attempts = ["tok-concurrent-one", "tok-concurrent-two"]
            .into_iter()
            .map(|execution_jti| {
                let manager = Arc::clone(&manager);
                let request_id = operator.request_id.clone();
                let lease_epoch = operator.lease_epoch;
                std::thread::spawn(move || {
                    manager.consume_execution(DebugExecutionConsumeRequest {
                        request_id,
                        lease_epoch,
                        execution_jti: execution_jti.into(),
                    })
                })
            })
            .collect::<Vec<_>>();
        let successes = attempts
            .into_iter()
            .map(|attempt| attempt.join().unwrap())
            .filter(Result::is_ok)
            .count();
        assert_eq!(successes, 1);
    }

    #[test]
    fn audit_failure_blocks_authority_transition() {
        let manager = DebugApprovalManager::new(PathBuf::from("/dev/null/audit.jsonl"));
        let request = request(&std::env::temp_dir());
        assert!(manager.register_session(request).is_err());
        assert_eq!(manager.status().state, "disabled");
    }
}
