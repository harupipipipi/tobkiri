use crate::CoreError;
use std::{
    collections::BTreeMap,
    sync::{Arc, Mutex, Weak},
    time::{Duration, Instant},
};
use tobkiri_canon::ids::ProfileId;
use tobkiri_gate::{
    AdmissionStatus, CancellationToken, DeliveryFault, FailureClass, Gate, GateEngine, GateError,
    IntegrityFaultCode,
};
use tokio::{runtime::Handle, task::JoinHandle};

/// Shared per-Core startup barriers. These recovery-only Gates have no mutable
/// candidate/runtime policy, so profile preparation cannot widen a live Session.
#[derive(Default)]
pub(crate) struct Admissions(Mutex<BTreeMap<ProfileId, Arc<Barrier>>>);
impl Admissions {
    #[cfg(test)]
    pub fn contains(&self, profile: &ProfileId) -> bool {
        self.0.lock().unwrap().contains_key(profile)
    }
    pub fn install(
        &self,
        profile: ProfileId,
        gate: Arc<GateEngine>,
    ) -> Result<Arc<Barrier>, CoreError> {
        let mut profiles = self
            .0
            .lock()
            .map_err(|_| CoreError::Runtime("admission registry poisoned".into()))?;
        if let Some(barrier) = profiles.get(&profile) {
            return Ok(barrier.clone());
        }
        gate.recover_profile(&profile)?;
        let stop = CancellationToken::new_with_control(gate.control()).map_err(GateError::from)?;
        let barrier = Arc::new(Barrier::new(profile.clone(), gate, stop));
        profiles.insert(profile, barrier.clone());
        Ok(barrier)
    }
}
/// Ownership state of the single metadata supervisor. Its Join is only ever
/// awaited by borrow under the state lock the supervisor itself never
/// touches, so an abandoned ensure_ready wait or a repeated close can never
/// mistake a detached task for a completed supervisor.
enum Supervised {
    Pending(JoinHandle<Result<(), GateError>>),
    /// The actual Join completed and the supervisor returned Ok.
    Joined,
    /// The actual Join failed (panic/abort); the lost outcome is latched and
    /// can never be reconciled into a transient success.
    Lost,
    /// The supervisor returned a typed fault. GateError is not required to
    /// be Clone, so the machine code is retained for repeat closes.
    Reported(&'static str),
}
impl Supervised {
    fn is_running(&self) -> bool {
        matches!(self, Self::Pending(task) if !task.is_finished())
    }
}
struct State {
    ready: bool,
    supervisor: Option<Supervised>,
}
/// Typed classification of a supervisor-observed fault, recorded with the
/// observation itself rather than recovered from its message. Only
/// `Transient` delivery/persistence faults can be resolved by the close-time
/// reconciliation; terminal, integrity and unclassified outcomes (including
/// a lost or timed-out delivery Join) stay latched for the life of the
/// Barrier. Declaration order is severity order.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
enum FaultClass {
    Transient,
    Terminal,
    Integrity,
    Unknown,
}
impl FaultClass {
    fn of(error: &GateError) -> Self {
        match error {
            // An Unavailable without a tagged origin is the
            // unclassified/interrupted bucket; it is never promoted to a
            // resolved transient.
            GateError::Unavailable => Self::Unknown,
            // A compound denial/audit fault is only as recoverable as its
            // least recoverable member: it can never conceal a lost Join, a
            // rights fault, revocation or cancellation behind a transient
            // audit retry.
            GateError::AuditFailure { denial, audit } => {
                match (Self::of(denial), Self::of(audit)) {
                    (Self::Integrity, _) | (_, Self::Integrity) => Self::Integrity,
                    (denial, audit) => denial.max(audit),
                }
            }
            _ => match error.failure_class() {
                FailureClass::Transient => Self::Transient,
                FailureClass::Integrity => Self::Integrity,
                FailureClass::Terminal => Self::Terminal,
            },
        }
    }
    /// The delivery job's typed outcome. Only the tagged sink-boundary
    /// `Unavailable` reported by `GateEngine::supervised_metadata_delivery`
    /// is a known transient metadata-sink fault; every other origin keeps
    /// its own classification.
    fn of_delivery(fault: &DeliveryFault) -> Self {
        match fault {
            DeliveryFault::SinkUnavailable => Self::Transient,
            DeliveryFault::Gate(error) => Self::of(error),
        }
    }
}
#[derive(Clone, Copy)]
struct ObservedFault {
    code: &'static str,
    class: FaultClass,
}
#[derive(Default)]
struct Health {
    // Machine constants only; bounded independently of queue size and retries.
    failure: Option<ObservedFault>,
    lease_failure: Option<ObservedFault>,
    integrity: Option<IntegrityFaultCode>,
    // Checked-bumped on every write so close-time reconciliation clears only
    // faults observed before its verification began.
    generation: u64,
}
impl Health {
    /// A successful observation clears only a transient fault; a new
    /// observation replaces the old one only when it is at least as severe.
    /// Typed terminal, integrity and unknown faults can never be erased by a
    /// later recovery or a milder failure.
    fn record(fault: &mut Option<ObservedFault>, new: Option<ObservedFault>) {
        match new {
            Some(new) => {
                if fault.is_none_or(|old| new.class >= old.class) {
                    *fault = Some(new);
                }
            }
            None => {
                if fault.is_some_and(|old| old.class == FaultClass::Transient) {
                    *fault = None;
                }
            }
        }
    }
    /// Checked so a saturated counter never wraps into the generation a
    /// reconciliation captured; a saturated health is unreconcilable.
    fn bump(&mut self) {
        self.generation = self.generation.saturating_add(1);
    }
    /// A typed transient delivery/lease fault exists that the bounded
    /// close-time reconciliation may resolve.
    fn reconcilable(&self) -> bool {
        self.generation < u64::MAX
            && [self.failure, self.lease_failure]
                .into_iter()
                .flatten()
                .any(|fault| fault.class == FaultClass::Transient)
    }
}
type SharedHealth = Arc<Mutex<Health>>;
pub(crate) struct Barrier {
    profile: ProfileId,
    gate: Arc<GateEngine>,
    stop: CancellationToken,
    state: tokio::sync::Mutex<State>,
    health: SharedHealth,
}
impl Barrier {
    pub fn new(profile: ProfileId, gate: Arc<GateEngine>, stop: CancellationToken) -> Self {
        Self {
            profile,
            gate,
            stop,
            state: tokio::sync::Mutex::new(State {
                ready: false,
                supervisor: None,
            }),
            health: Arc::new(Mutex::new(Health::default())),
        }
    }
    /// A synchronous open can recover persisted deadlines, but authorizing work
    /// requires an active runtime and delivery through the captured watermark.
    pub async fn ensure_ready(&self) -> Result<(), CoreError> {
        let runtime = Handle::try_current().map_err(|_| CoreError::Gate(GateError::Unavailable))?;
        if self.stop.is_cancelled() {
            return Err(CoreError::Gate(GateError::Unavailable));
        }
        let mut state = self.state.lock().await;
        self.check_integrity()?;
        if state.ready
            && self.health_error().is_none()
            && state
                .supervisor
                .as_ref()
                .is_some_and(Supervised::is_running)
        {
            return Ok(());
        }
        state.ready = false;
        // A retained finished Join is observed by borrow before it can be
        // replaced, exactly as `close` observes it: panic/abort latches
        // `Lost`, a typed fault latches `Reported`, and only a clean exit
        // becomes `Joined`. A latched lost/reported outcome is terminal and
        // is never overwritten by a fresh healthy supervisor.
        let pending = match state.supervisor.as_mut() {
            Some(Supervised::Lost) => {
                return Err(CoreError::Gate(GateError::Unavailable));
            }
            Some(Supervised::Reported(code)) => {
                let code = *code;
                return Err(CoreError::Gate(GateError::Persistence(format!(
                    "lease supervision unavailable: {code}"
                ))));
            }
            Some(Supervised::Pending(task)) if task.is_finished() => Some(task),
            _ => None,
        };
        if let Some(task) = pending {
            // Borrowed, never taken: if this waiter is dropped mid-await the
            // actual Join stays owned by the Barrier for the next call.
            match task.await {
                // A cleanly joined supervisor only exits on stop; a stopped
                // session denies new work instead of respawning.
                Ok(Ok(())) => {
                    state.supervisor = Some(Supervised::Joined);
                    return Err(CoreError::Gate(GateError::Unavailable));
                }
                Ok(Err(error)) => {
                    let code = error.code();
                    state.supervisor = Some(Supervised::Reported(code));
                    return Err(error.into());
                }
                Err(_) => {
                    state.supervisor = Some(Supervised::Lost);
                    return Err(CoreError::Gate(GateError::Unavailable));
                }
            }
        }
        self.gate.recover_profile(&self.profile)?;
        let watermark = self.gate.outbox_watermark(&self.profile)?;
        if state
            .supervisor
            .as_ref()
            .is_none_or(|supervised| !supervised.is_running())
        {
            state.supervisor = Some(Supervised::Pending(runtime.spawn(supervise(
                Arc::downgrade(&self.gate),
                self.profile.clone(),
                self.stop.clone(),
                self.health.clone(),
            ))));
        }
        self.gate
            .flush_outbox_through(&self.profile, watermark)
            .await?;
        self.check_integrity()?;
        if self.stop.is_cancelled()
            || state
                .supervisor
                .as_ref()
                .is_none_or(|supervised| !supervised.is_running())
        {
            return Err(CoreError::Gate(GateError::Unavailable));
        }
        state.ready = true;
        Ok(())
    }
    fn health_error(&self) -> Option<GateError> {
        match self.health.lock() {
            Ok(health) if health.integrity.is_some() => Some(GateError::Integrity(
                health.integrity.expect("latched integrity"),
            )),
            Ok(health) => health.lease_failure.or(health.failure).map(|fault| {
                GateError::Persistence(format!("lease supervision unavailable: {}", fault.code))
            }),
            Err(_) => Some(GateError::Unavailable),
        }
    }
    fn check_integrity(&self) -> Result<(), CoreError> {
        // Durable domain status is checked even on the Ready fast path. A
        // successful unrelated delivery cannot clear an integrity fault.
        match self.gate.admission_status(&self.profile)? {
            AdmissionStatus::Allowed => {}
            AdmissionStatus::IntegrityFault { code } => {
                return Err(GateError::Integrity(code).into());
            }
        }
        if let Some(GateError::Integrity(code)) = self.health_error() {
            return Err(GateError::Integrity(code).into());
        }
        Ok(())
    }
    /// Stop new admission immediately; physical supervisor completion is
    /// observed separately by `close`.
    pub(crate) fn request_shutdown(&self) {
        self.stop.cancel();
    }
    /// One bounded same-profile reconciliation, run only after the actual
    /// supervisor Join has succeeded. A latched transient fault can be
    /// stale: the same outbox may already have been durably flushed by
    /// another writer before the final tick ran. This re-observes existing
    /// due-lease cancellation, recovery (including the records it
    /// generates), delivery through the captured watermark, and
    /// admission/integrity state. Only typed transient observations resolved
    /// by those results are cleared, and only while the health generation is
    /// unchanged. Integrity, terminal, revocation and unclassified join-loss
    /// outcomes stay latched, and a failed reconciliation remains a close
    /// failure.
    async fn reconcile_health_after_join(&self) -> Result<(), CoreError> {
        let generation = match self.health.lock() {
            Ok(health) if health.reconcilable() => health.generation,
            _ => return Ok(()),
        };
        self.gate.cancel_due_leases(&self.profile)?;
        self.gate.recover_profile(&self.profile)?;
        let watermark = self.gate.outbox_watermark(&self.profile)?;
        self.gate
            .flush_outbox_through(&self.profile, watermark)
            .await?;
        self.check_integrity()?;
        if let Ok(mut health) = self.health.lock()
            && health.generation == generation
        {
            let mut cleared = false;
            if matches!(health.failure, Some(f) if f.class == FaultClass::Transient) {
                health.failure = None;
                cleared = true;
            }
            if matches!(health.lease_failure, Some(f) if f.class == FaultClass::Transient) {
                health.lease_failure = None;
                cleared = true;
            }
            if cleared {
                health.bump();
            }
        }
        Ok(())
    }
    pub async fn close(&self) -> Result<(), CoreError> {
        self.request_shutdown();
        {
            let mut state = self.state.lock().await;
            let pending = match state.supervisor.as_mut() {
                // A never-started supervisor is not a lost Join; only the
                // retained terminal states below deny reconciliation.
                None | Some(Supervised::Joined) => None,
                Some(Supervised::Lost) => {
                    return Err(CoreError::Gate(GateError::Unavailable));
                }
                Some(Supervised::Reported(code)) => {
                    let code = *code;
                    return Err(CoreError::Gate(GateError::Persistence(format!(
                        "lease supervision unavailable: {code}"
                    ))));
                }
                Some(Supervised::Pending(task)) => Some(task),
            };
            if let Some(task) = pending {
                // The actual Join is awaited strictly by borrow. If this
                // close waiter is cancelled the handle stays owned by the
                // Barrier, so a later close still observes the real task
                // outcome instead of a detached slot.
                match task.await {
                    Ok(Ok(())) => state.supervisor = Some(Supervised::Joined),
                    Ok(Err(error)) => {
                        let code = error.code();
                        state.supervisor = Some(Supervised::Reported(code));
                        return Err(error.into());
                    }
                    Err(_) => {
                        state.supervisor = Some(Supervised::Lost);
                        return Err(CoreError::Gate(GateError::Unavailable));
                    }
                }
            }
        }
        self.check_integrity()?;
        self.reconcile_health_after_join().await?;
        match self.health_error() {
            Some(error) => Err(error.into()),
            None => Ok(()),
        }
    }
}
impl Drop for Barrier {
    fn drop(&mut self) {
        self.stop.cancel();
    }
}
async fn supervise(
    gate: Weak<GateEngine>,
    profile: ProfileId,
    stop: CancellationToken,
    health: SharedHealth,
) -> Result<(), GateError> {
    let mut tick = tokio::time::interval(Duration::from_millis(50));
    tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let runtime = Handle::current();
    let mut delivery: Option<JoinHandle<Result<(), DeliveryFault>>> = None;
    let mut failures = 0u32;
    let mut retry_after = Instant::now();
    loop {
        tokio::select! {
            _=stop.cancelled()=>{
                if let Some(task) = delivery.as_mut() {
                    // No new work is admitted. Allow the single metadata-only
                    // delivery job a bounded interval to report its result.
                    match tokio::time::timeout(Duration::from_secs(2), task).await {
                        Ok(Ok(Ok(()))) => {},
                        Ok(Ok(Err(fault))) => observe(&health, &fault),
                        // Timeout or Join loss: the delivery outcome is
                        // genuinely unknown and stays latched; a later
                        // successful flush can never resolve it.
                        _ => remember(
                            &health,
                            Some(ObservedFault {
                                code: "unavailable",
                                class: FaultClass::Unknown,
                            }),
                        ),
                    }
                }
                return Ok(());
            },
            _=tick.tick()=>{}
        }
        let Some(gate) = gate.upgrade() else {
            return Ok(());
        };
        // This local signal is independent of SQLite/audit progress and retains
        // the original watch deadline. It never asserts a terminal decision.
        let lease_failure = gate
            .cancel_due_leases(&profile)
            .err()
            .map(|error| ObservedFault {
                code: error.code(),
                class: FaultClass::of(&error),
            });
        if let Ok(mut health) = health.lock() {
            Health::record(&mut health.lease_failure, lease_failure);
            health.bump();
        }
        if delivery.as_ref().is_some_and(JoinHandle::is_finished) {
            match delivery.take().expect("finished delivery").await {
                Ok(Ok(())) => {
                    failures = 0;
                    retry_after = Instant::now();
                    remember(&health, None);
                }
                Ok(Err(fault)) => {
                    failures = failures.saturating_add(1);
                    observe(&health, &fault);
                    // At most one metadata-only recovery/delivery job is in
                    // flight. Storage and sink outages never stop lease ticks.
                    let delay = match &fault {
                        // The tagged sink outage retries with the same
                        // bounded transient backoff as a persistence fault.
                        DeliveryFault::SinkUnavailable => {
                            100u64.saturating_mul(1u64 << failures.min(5)).min(2000)
                        }
                        DeliveryFault::Gate(error) => match error.failure_class() {
                            FailureClass::Transient => {
                                100u64.saturating_mul(1u64 << failures.min(5)).min(2000)
                            }
                            // Integrity remains latched. Continue bounded existing
                            // expiry/cleanup maintenance without a busy retry loop.
                            FailureClass::Integrity | FailureClass::Terminal => 2000,
                        },
                    };
                    retry_after = Instant::now() + Duration::from_millis(delay);
                }
                // A lost delivery Join is its own typed outcome: latch
                // Unknown so no later success can report it resolved.
                Err(_) => {
                    failures = failures.saturating_add(1);
                    remember(
                        &health,
                        Some(ObservedFault {
                            code: "unavailable",
                            class: FaultClass::Unknown,
                        }),
                    );
                    retry_after = Instant::now() + Duration::from_millis(2000);
                }
            }
        }
        if delivery.is_none() && Instant::now() >= retry_after {
            let gate = gate.clone();
            let profile = profile.clone();
            let runtime = runtime.clone();
            delivery = Some(tokio::task::spawn_blocking(move || {
                runtime.block_on(async move { gate.supervised_metadata_delivery(&profile).await })
            }));
        }
    }
}
fn remember(health: &SharedHealth, failure: Option<ObservedFault>) {
    if let Ok(mut health) = health.lock() {
        Health::record(&mut health.failure, failure);
        health.bump();
    }
}
fn observe(health: &SharedHealth, fault: &DeliveryFault) {
    fn integrity(error: &GateError) -> Option<IntegrityFaultCode> {
        match error {
            GateError::Integrity(code) => Some(*code),
            GateError::AuditFailure { audit, .. } => integrity(audit),
            _ => None,
        }
    }
    if let Ok(mut health) = health.lock() {
        Health::record(
            &mut health.failure,
            Some(ObservedFault {
                code: fault.code(),
                class: FaultClass::of_delivery(fault),
            }),
        );
        health.bump();
        if let Some(error) = fault.gate_error()
            && let Some(code) = integrity(error)
        {
            health.integrity.get_or_insert(code);
        }
    }
}

#[cfg(test)]
#[path = "admission_tests.rs"]
mod tests;
