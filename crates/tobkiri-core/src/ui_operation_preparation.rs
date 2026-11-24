//! Core-owned prepared UI operation owner: one retained close candidate, the
//! single originally minted invocation context and one bounded publication
//! counter shared across every stage. No raw Arc/Weak, CallCx constructor,
//! Gate authority proof or route-selected budget escapes this facade.

use std::sync::Arc;
use std::time::{Duration, Instant};

use tobkiri_canon::ids::UiSessionId;
use tobkiri_flow::{StepError, StreamCloseControl, StreamCloseReason};
use tobkiri_gate::{CallCx, ControlKind, ControlReservation, GateError};
use tokio_util::sync::CancellationToken as TokioCancellationToken;

use crate::execution_frames::{UiOperationCloseHandle, UiOperationPublishAttempt};

/// Post-busy waits indexed by the consumed attempt; attempt eight never waits.
const BUSY_WAIT_MS: [u64; 7] = [1, 2, 4, 8, 16, 16, 16];
const MAX_ATTEMPTS: u8 = 8;

/// Fixed close causes a trusted host may accept under its own guards. These
/// carry no authority; acceptance is atomics plus durable wake duty only.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum UiOperationTermination {
    Manual,
    Deadline,
    Shutdown,
    Retired,
    Unknown,
}
impl UiOperationTermination {
    fn reason(self) -> StreamCloseReason {
        match self {
            Self::Manual => StreamCloseReason::Manual,
            Self::Deadline => StreamCloseReason::Deadline,
            Self::Shutdown => StreamCloseReason::Shutdown,
            Self::Retired => StreamCloseReason::Retired,
            Self::Unknown => StreamCloseReason::Unknown,
        }
    }
    fn accepted(reason: StreamCloseReason) -> Self {
        match reason {
            StreamCloseReason::Manual => Self::Manual,
            StreamCloseReason::Deadline => Self::Deadline,
            StreamCloseReason::Shutdown => Self::Shutdown,
            StreamCloseReason::Retired => Self::Retired,
            StreamCloseReason::Unknown => Self::Unknown,
        }
    }
}

/// Pull-side binding to one registered stream entry. The original context is
/// borrowed; the entry lease itself stays inside the Session registry.
struct PullBinding {
    session: crate::Session,
    ui: UiSessionId,
    handle: u64,
    entry_key: usize,
    generation: Option<u64>,
    source: Option<Arc<dyn StreamCloseControl>>,
}

/// Opaque prepared UI operation owner. Never Clone: its single attempt
/// counter and genuine one-use claim can never be duplicated.
pub struct UiOperationPreparation {
    candidate: UiOperationCloseHandle,
    cx: CallCx,
    generation: Option<u64>,
    pull: Option<PullBinding>,
    attempts: u8,
    claimed: bool,
    _reservation: ControlReservation,
}

fn step(error: GateError) -> StepError {
    match error {
        GateError::Cancelled | GateError::Deadline => {
            StepError::new("cancelled", "prepared UI operation was terminated")
        }
        _ => StepError::new("unavailable", "prepared UI operation is unavailable"),
    }
}

impl UiOperationPreparation {
    /// Precharge owner metadata and both simultaneous Sleep layouts from the
    /// original context's own Core account before construction.
    fn reserve(cx: &CallCx, copied: usize) -> Result<ControlReservation, GateError> {
        let bytes = std::mem::size_of::<Self>()
            .checked_add(std::mem::size_of::<PullBinding>())
            .and_then(|bytes| bytes.checked_add(2 * std::mem::size_of::<tokio::time::Sleep>()))
            .and_then(|bytes| bytes.checked_add(copied))
            .and_then(|bytes| bytes.checked_add(256))
            .ok_or(GateError::Unavailable)?;
        cx.control()
            .reserve(
                ControlKind::Frame,
                u64::try_from(bytes).map_err(|_| GateError::Unavailable)?,
            )
            .map_err(GateError::from)
    }
    /// Prepared owner for a received invocation. The context was minted once
    /// by Session and is retained unchanged through every retry.
    pub(crate) fn for_invoke(
        candidate: UiOperationCloseHandle,
        cx: CallCx,
        generation: u64,
    ) -> Result<Self, GateError> {
        let _reservation = Self::reserve(&cx, 0)?;
        Ok(Self {
            candidate,
            cx,
            generation: Some(generation),
            pull: None,
            attempts: 0,
            claimed: false,
            _reservation,
        })
    }
    /// Prepared owner borrowing a registered entry's original context,
    /// identity and optional close source. Nothing is reminted or reallocated.
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn for_pull(
        candidate: UiOperationCloseHandle,
        cx: CallCx,
        session: crate::Session,
        ui: &UiSessionId,
        handle: u64,
        entry_key: usize,
        generation: Option<u64>,
        source: Option<Arc<dyn StreamCloseControl>>,
    ) -> Result<Self, GateError> {
        // Charge the real copied UiSessionId heap layout to the original
        // entry.cx account before any owned metadata is cloned; Session,
        // CallCx and Arc moves are allocation-free old-owner retention.
        let _reservation = Self::reserve(&cx, ui.clone_storage_bytes())?;
        Ok(Self {
            candidate,
            cx,
            generation,
            pull: Some(PullBinding {
                session,
                ui: ui.clone(),
                handle,
                entry_key,
                generation,
                source,
            }),
            attempts: 0,
            claimed: false,
            _reservation,
        })
    }
    /// The retained close candidate; a clone shares the original funding.
    pub fn close_handle(&self) -> UiOperationCloseHandle {
        self.candidate.clone()
    }
    /// The original minted absolute deadline; retries never refresh it.
    pub fn deadline(&self) -> Instant {
        self.cx.deadline()
    }
    /// The dedicated external cancellation token supplied at construction.
    pub fn external_token(&self) -> TokioCancellationToken {
        self.candidate.external_token()
    }
    /// The first accepted cause, if any. Observation only, never authority.
    pub fn accepted(&self) -> Option<UiOperationTermination> {
        self.candidate
            .accepted()
            .0
            .map(UiOperationTermination::accepted)
    }
    /// Guard-safe fixed-cause acceptance: finite atomics plus durable
    /// in-record wake duty only. No callback, lock wait, allocation, token
    /// wake or Drop callback happens here.
    pub fn accept(&self, cause: UiOperationTermination) {
        self.candidate.record(cause.reason());
    }
    /// Outside-all-guards delivery of every owed source callback and the
    /// original token wake; failure leaves duty in the same funded owner.
    pub fn flush(&self) -> Result<(), StepError> {
        self.candidate.flush()
    }
    /// Original context/deadline and external cancellation liveness; for pull
    /// also that the same registered entry still exists unreplaced.
    pub fn is_live(&self) -> bool {
        self.check().is_ok()
    }
    /// Synchronous final validation and the genuine one-use Running claim on
    /// the retained candidate: atomics only, safe under a final install lock.
    /// The original context's immutable deadline/cancel and the bounded
    /// budget deadline are rechecked without ever taking the ledger mutex;
    /// the full budget check still runs outside guards and at dispatch.
    pub fn claim(&mut self) -> Result<(), GateError> {
        let generation = self.generation.ok_or(GateError::Unavailable)?;
        self.check_guarded()?;
        self.candidate.claim_registered(generation)?;
        self.claimed = true;
        Ok(())
    }
    /// Guard-safe nonblocking recheck derived only from immutable original
    /// context fields, preserving the original deadline/cancel precedence;
    /// registry uncertainty fails closed. No ledger mutex, wake or wait.
    fn check_guarded(&self) -> Result<(), GateError> {
        if Instant::now() >= self.cx.deadline() {
            return Err(GateError::Deadline);
        }
        if self.cx.cancel().is_cancelled() || self.candidate.external_token().is_cancelled() {
            return Err(GateError::Cancelled);
        }
        if self
            .cx
            .budget()
            .is_some_and(|budget| Instant::now() >= budget.deadline())
        {
            return Err(GateError::Deadline);
        }
        if let Some(pull) = &self.pull
            && !pull.session.ui_stream_entry_live(
                &pull.ui,
                pull.handle,
                pull.entry_key,
                pull.generation,
            )
        {
            return Err(GateError::Unavailable);
        }
        Ok(())
    }
    /// Bounded publication of the SAME retained candidate, then the outside-
    /// guard pull source bind. Only this factory's own Busy retries; Uncertain
    /// never waits. No retry creates a new candidate, registration, deadline
    /// or allocation, and the counter never resets between stages.
    pub async fn publish(&mut self) -> Result<(), StepError> {
        loop {
            let consumed = self.consume()?;
            match self.candidate.publish() {
                UiOperationPublishAttempt::Published => break,
                UiOperationPublishAttempt::Uncertain(error) => return Err(step(error)),
                UiOperationPublishAttempt::Busy => {
                    self.post_busy_wait(consumed).await?;
                    self.check().map_err(step)?;
                    if self.attempts >= MAX_ATTEMPTS {
                        return Err(StepError::new(
                            "unavailable",
                            "prepared UI publication stayed busy",
                        ));
                    }
                }
            }
        }
        if let Some(source) = self.pull.as_ref().and_then(|pull| pull.source.as_ref()) {
            self.candidate.bind(source)?.flush()?;
        }
        self.check().map_err(step)
    }
    /// Original context/deadline plus dedicated external cancellation and,
    /// for pull, continued existence of the same registered entry.
    fn check(&self) -> Result<(), GateError> {
        self.cx.check_active()?;
        if self.candidate.external_token().is_cancelled() {
            return Err(GateError::Cancelled);
        }
        if let Some(pull) = &self.pull
            && !pull.session.ui_stream_entry_live(
                &pull.ui,
                pull.handle,
                pull.entry_key,
                pull.generation,
            )
        {
            return Err(GateError::Unavailable);
        }
        Ok(())
    }
    /// Consume one combined attempt BEFORE the call; successes consume too.
    fn consume(&mut self) -> Result<u8, StepError> {
        self.check().map_err(step)?;
        if self.attempts >= MAX_ATTEMPTS {
            return Err(StepError::new(
                "unavailable",
                "prepared UI publication attempts exhausted",
            ));
        }
        self.attempts += 1;
        Ok(self.attempts)
    }
    /// One fixed post-busy wait racing the original cancellation, the
    /// dedicated external channel and the original absolute deadline. Holds
    /// no Core/Host guard; attempt eight never waits.
    async fn post_busy_wait(&mut self, consumed: u8) -> Result<(), StepError> {
        let Some(wait) = usize::from(consumed)
            .checked_sub(1)
            .and_then(|index| BUSY_WAIT_MS.get(index))
        else {
            return Ok(());
        };
        let external = self.external_token();
        tokio::select! {
            _ = self.cx.cancel().cancelled() => Err(step(GateError::Cancelled)),
            _ = external.cancelled() => Err(step(GateError::Cancelled)),
            _ = tokio::time::sleep_until(self.cx.deadline().into()) => {
                Err(step(GateError::Deadline))
            }
            _ = tokio::time::sleep(Duration::from_millis(*wait)) => Ok(()),
        }
    }
    /// Consuming invoke handoff: the original context, the retained candidate
    /// and whether the one-use claim already ran. Nothing is refreshed and a
    /// consumed owner cannot dispatch twice.
    pub(crate) fn into_invoke(self) -> (CallCx, UiOperationCloseHandle, bool) {
        let claimed = self.claimed;
        (self.cx, self.candidate, claimed)
    }
    /// Consuming pull validation: this owner must still name the same live
    /// registered entry before the existing bounded pull path may run.
    pub(crate) fn validate_pull(&self, ui: &UiSessionId, handle: u64) -> bool {
        self.pull
            .as_ref()
            .is_some_and(|pull| pull.ui == *ui && pull.handle == handle)
            && self.check().is_ok()
    }
    #[cfg(test)]
    pub(crate) fn attempts(&self) -> u8 {
        self.attempts
    }
    /// Test-only tightening of the original context deadline; the capped
    /// clone keeps the same request, ledger and cancellation lineage.
    #[cfg(test)]
    pub(crate) fn tighten_deadline_for_test(&mut self, deadline: Instant) {
        self.cx = self.cx.clone().cap_deadline(deadline);
    }
    /// Whether this owner already ran its genuine one-use claim.
    pub(crate) fn is_claimed(&self) -> bool {
        self.claimed
    }
    /// Whether this owner binds a registered pull entry; such an owner can
    /// never dispatch an invocation.
    pub(crate) fn is_pull(&self) -> bool {
        self.pull.is_some()
    }
    /// The registered entry key and minted generation for a pull owner.
    pub(crate) fn pull_identity(&self) -> Option<(usize, Option<u64>)> {
        self.pull
            .as_ref()
            .map(|pull| (pull.entry_key, pull.generation))
    }
    /// Test-only borrow of the real prepared context retained by this owner.
    #[cfg(test)]
    pub(crate) fn context(&self) -> &CallCx {
        &self.cx
    }
}

/// Guard-safe termination acceptance and explicit outside-all-guards
/// delivery on the opaque funded close owner itself, so a host may retain
/// the SAME owner before publication completes. Storage/notification
/// metadata only: never a grant, ACK or physical proof.
impl UiOperationCloseHandle {
    /// Fixed-cause acceptance through the existing record path: finite
    /// atomics plus durable in-record wake duty, no callbacks under guards.
    pub fn accept(&self, cause: UiOperationTermination) {
        self.record(cause.reason());
    }
    /// Outside-all-guards delivery delegate; failure leaves every owed duty
    /// inside the same funded owner for the process owner to flush later.
    pub fn deliver(&self) -> Result<(), StepError> {
        self.flush()
    }
}
