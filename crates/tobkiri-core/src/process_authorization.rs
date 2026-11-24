//! Private approval custody for an owned process snapshot.
//!
//! This stage retains the original, unconsumed Grant with its exact snapshot,
//! context and Gate. It has no execution, consume, parts-extraction or guest
//! capability API. A future native owner must first accept this entire custody,
//! verify the same snapshot again immediately before consume, and retain every
//! consumed/cleanup/unknown phase through its required ending. Pre-approval
//! verification here is not a substitute for that future verification.
//! Proc/PTY remain unavailable; no physical process or terminal is created.

use std::{fmt, sync::Arc};

use tobkiri_canon::AllocationError;
use tobkiri_gate::{
    CallCx, ControlKind, ControlReservation, EffectRequest, Gate, GateError, Grant,
    LaunchBindingError, ProcessLaunchBinding,
};

use crate::process_catalog::{PreparedProcessSnapshot, ProcessCatalog};

/// Errors carry no argv/env, descriptor or key data. The original Grant's
/// existing abandonment path handles rejection after approval.
#[allow(dead_code)]
#[derive(Debug, thiserror::Error)]
pub(crate) enum ProcessAuthorizationError {
    #[error("process snapshot verification failed: {0}")]
    Snapshot(LaunchBindingError),
    #[error("process authorization metadata capacity exhausted")]
    Capacity,
    #[error("process request metadata allocation failed: {0}")]
    Allocation(AllocationError),
    #[error("process launch authorization failed: {0}")]
    Gate(GateError),
    #[error("approved grant differs from the prepared process binding")]
    GrantMismatch,
}

/// One move-only approval owner, not executable dispatch authority. No raw
/// Grant, FD, secret arguments or caller-selected key can be extracted.
#[allow(dead_code)]
pub(crate) struct AuthorizedProcessLaunch {
    // Drop the original unconsumed Grant first, while its exact snapshot,
    // context, Gate and all metadata charges are still retained.
    grant: Grant,
    snapshot: PreparedProcessSnapshot,
    cx: CallCx,
    gate: Arc<dyn Gate>,
    // Both original reservations drop after every item they funded.
    _request_metadata: ControlReservation,
    _capsule_metadata: ControlReservation,
}

impl fmt::Debug for AuthorizedProcessLaunch {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        // Both existing Debug projections omit all secret snapshot data.
        f.debug_struct("AuthorizedProcessLaunch")
            .field("grant", &self.grant)
            .field("snapshot", &self.snapshot)
            .finish_non_exhaustive()
    }
}

#[allow(dead_code)]
impl AuthorizedProcessLaunch {
    /// Verify with the originating Core catalog, then use the existing normal
    /// policy/mediation/Dangerous-consent path. Ownership of the original
    /// snapshot and context stays in this future across the approval await.
    pub(crate) async fn authorize(
        catalog: &ProcessCatalog,
        snapshot: PreparedProcessSnapshot,
        cx: CallCx,
        gate: Arc<dyn Gate>,
    ) -> Result<Self, ProcessAuthorizationError> {
        catalog
            .verify_snapshot(&snapshot)
            .map_err(ProcessAuthorizationError::Snapshot)?;
        let binding = snapshot.binding();
        let request_bytes = std::mem::size_of::<EffectRequest>()
            .checked_add(binding.program_id().len())
            .and_then(|bytes| u64::try_from(bytes).ok())
            .ok_or(ProcessAuthorizationError::Capacity)?;
        // Admit before the sole program String copy. This charge follows that
        // exact allocation into Gate's unconsumed Grant, without re-copying it.
        let request_metadata = cx
            .control()
            .reserve(ControlKind::Frame, request_bytes)
            .map_err(|_| ProcessAuthorizationError::Capacity)?;
        let request = launch_request(binding).map_err(ProcessAuthorizationError::Allocation)?;
        let grant = gate
            .authorize_process_launch(&cx, request, binding)
            .await
            .map_err(ProcessAuthorizationError::Gate)?;
        Self::from_approved(snapshot, grant, cx, gate, request_metadata)
    }

    /// Private adoption only: the sole production caller already verified the
    /// snapshot with its originating catalog and obtained this original Grant.
    /// Every failure explicitly abandons the Grant before snapshot destruction.
    fn from_approved(
        snapshot: PreparedProcessSnapshot,
        grant: Grant,
        cx: CallCx,
        gate: Arc<dyn Gate>,
        request_metadata: ControlReservation,
    ) -> Result<Self, ProcessAuthorizationError> {
        if !grant_matches(snapshot.binding(), &grant) {
            drop(grant);
            return Err(ProcessAuthorizationError::GrantMismatch);
        }
        // This post-approval admission is still strictly before consume. A
        // failure is pre-use abandonment, never execution or a new terminal.
        let bytes = u64::try_from(std::mem::size_of::<Self>())
            .map_err(|_| ProcessAuthorizationError::Capacity);
        let metadata = bytes.and_then(|bytes| {
            cx.control()
                .reserve(ControlKind::Frame, bytes)
                .map_err(|_| ProcessAuthorizationError::Capacity)
        });
        let capsule_metadata = match metadata {
            Ok(metadata) => metadata,
            Err(error) => {
                drop(grant);
                return Err(error);
            }
        };
        Ok(Self {
            grant,
            snapshot,
            cx,
            gate,
            _request_metadata: request_metadata,
            _capsule_metadata: capsule_metadata,
        })
    }
}

fn launch_request(binding: &ProcessLaunchBinding) -> Result<EffectRequest, AllocationError> {
    let mut program = String::new();
    program
        .try_reserve_exact(binding.program_id().len())
        .map_err(|_| AllocationError::Allocator)?;
    if program.capacity() != binding.program_id().len() {
        return Err(AllocationError::Unavailable);
    }
    program.push_str(binding.program_id());
    Ok(if binding.pty().is_some() {
        EffectRequest::Pty { program }
    } else {
        EffectRequest::Proc { program }
    })
}

fn grant_matches(binding: &ProcessLaunchBinding, grant: &Grant) -> bool {
    let kind_and_program = match grant.effect() {
        EffectRequest::Proc { program } => {
            binding.pty().is_none() && program == binding.program_id()
        }
        EffectRequest::Pty { program } => {
            binding.pty().is_some() && program == binding.program_id()
        }
        _ => false,
    };
    kind_and_program
        && grant.args_digest() == binding.args_digest()
        && grant.limits() == binding.limits()
}

#[cfg(all(test, unix))]
#[path = "process_authorization_tests.rs"]
mod tests;
