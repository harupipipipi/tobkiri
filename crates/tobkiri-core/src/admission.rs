use crate::CoreError;
use std::{
    collections::BTreeMap,
    sync::{Arc, Mutex, Weak},
    time::{Duration, Instant},
};
use tobkiri_canon::ids::ProfileId;
use tobkiri_gate::{
    AdmissionStatus, CancellationToken, FailureClass, Gate, GateEngine, GateError,
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
struct State {
    ready: bool,
    supervisor: Option<JoinHandle<Result<(), GateError>>>,
}
#[derive(Default)]
struct Health {
    // Machine constants only; bounded independently of queue size and retries.
    failure: Option<&'static str>,
    lease_failure: Option<&'static str>,
    integrity: Option<IntegrityFaultCode>,
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
                .is_some_and(|task| !task.is_finished())
        {
            return Ok(());
        }
        state.ready = false;
        self.gate.recover_profile(&self.profile)?;
        let watermark = self.gate.outbox_watermark(&self.profile)?;
        if state
            .supervisor
            .as_ref()
            .is_none_or(JoinHandle::is_finished)
        {
            state.supervisor = Some(runtime.spawn(supervise(
                Arc::downgrade(&self.gate),
                self.profile.clone(),
                self.stop.clone(),
                self.health.clone(),
            )));
        }
        self.gate
            .flush_outbox_through(&self.profile, watermark)
            .await?;
        self.check_integrity()?;
        if self.stop.is_cancelled()
            || state
                .supervisor
                .as_ref()
                .is_none_or(JoinHandle::is_finished)
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
            Ok(health) => health.lease_failure.or(health.failure).map(|code| {
                GateError::Persistence(format!("lease supervision unavailable: {code}"))
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
    pub async fn close(&self) -> Result<(), CoreError> {
        self.request_shutdown();
        let task = self.state.lock().await.supervisor.take();
        if let Some(task) = task {
            task.await
                .map_err(|_| CoreError::Gate(GateError::Unavailable))??;
        }
        self.check_integrity()?;
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
    let mut delivery: Option<JoinHandle<Result<(), GateError>>> = None;
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
                        Ok(Ok(Err(error))) => observe(&health, &error),
                        _ => remember(&health, Some("unavailable")),
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
            .map(|error| error.code());
        if let Ok(mut health) = health.lock() {
            health.lease_failure = lease_failure;
        }
        if delivery.as_ref().is_some_and(JoinHandle::is_finished) {
            let result = delivery
                .take()
                .expect("finished delivery")
                .await
                .map_err(|_| GateError::Unavailable)
                .and_then(|result| result);
            match result {
                Ok(()) => {
                    failures = 0;
                    retry_after = Instant::now();
                    remember(&health, None);
                }
                Err(error) => {
                    failures = failures.saturating_add(1);
                    observe(&health, &error);
                    // At most one metadata-only recovery/delivery job is in
                    // flight. Storage and sink outages never stop lease ticks.
                    let delay = match error.failure_class() {
                        FailureClass::Transient => {
                            100u64.saturating_mul(1u64 << failures.min(5)).min(2000)
                        }
                        // Integrity remains latched. Continue bounded existing
                        // expiry/cleanup maintenance without a busy retry loop.
                        FailureClass::Integrity | FailureClass::Terminal => 2000,
                    };
                    retry_after = Instant::now() + Duration::from_millis(delay);
                }
            }
        }
        if delivery.is_none() && Instant::now() >= retry_after {
            let gate = gate.clone();
            let profile = profile.clone();
            let runtime = runtime.clone();
            delivery = Some(tokio::task::spawn_blocking(move || {
                runtime.block_on(async move {
                    gate.recover_profile(&profile)?;
                    gate.flush_outbox(&profile).await?;
                    if let AdmissionStatus::IntegrityFault { code } =
                        gate.admission_status(&profile)?
                    {
                        return Err(GateError::Integrity(code));
                    }
                    Ok(())
                })
            }));
        }
    }
}
fn remember(health: &SharedHealth, failure: Option<&'static str>) {
    if let Ok(mut health) = health.lock() {
        health.failure = failure;
    }
}
fn observe(health: &SharedHealth, error: &GateError) {
    fn integrity(error: &GateError) -> Option<IntegrityFaultCode> {
        match error {
            GateError::Integrity(code) => Some(*code),
            GateError::AuditFailure { audit, .. } => integrity(audit),
            _ => None,
        }
    }
    if let Ok(mut health) = health.lock() {
        health.failure = Some(error.code());
        if let Some(code) = integrity(error) {
            health.integrity.get_or_insert(code);
        }
    }
}

#[cfg(test)]
#[path = "admission_tests.rs"]
mod tests;
