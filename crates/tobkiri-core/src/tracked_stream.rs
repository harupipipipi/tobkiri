use crate::funded_arc::FundedArc;
use crate::{execution_frames::ExecutionFactory, session::ConsumedEntryTerminal};
use async_trait::async_trait;
use std::{
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    task::{Context, Poll, Waker},
};
use tobkiri_canon::{ControlHandle, CoreBuffer, CoreValue, Digest};
use tobkiri_flow::{
    ActiveCodeFrame, CoreStream, RunEvent, RunEventKind, RunTask, RunTaskCompletion,
    RunTaskPhysicalError, RunTaskPhysicalResult, StepError, StreamCloseControl, StreamCloseReason,
    StreamSource, StreamTerminal,
};
use tobkiri_gate::{CallCx, ControlKind, ControlReservation, Gate, GateEngine, GrantId, Outcome};
use tokio::{runtime::Handle, task::JoinHandle};

/// Retains the FlowRun grant until cleanup is completed on its owning runtime.
/// Callers can observe cleanup/audit errors through close or a post-cancel pull.
pub(crate) struct TrackedStream {
    source: Option<CoreStream>,
    gate: Arc<GateEngine>,
    cx: CallCx,
    grant: Option<GrantId>,
    entry_terminal: Option<ConsumedEntryTerminal>,
    terminal: Option<Outcome>,
    runtime: Handle,
    cleanup: Option<JoinHandle<CleanupReceipt>>,
    receipt: Option<CleanupReceipt>,
    cleanup_error: Option<StepError>,
    end_batch: Option<CoreValue>,
    end_buffer: Option<CoreBuffer>,
    closed: bool,
    native_cleanup: Option<NativeCleanup>,
}
/// The terminal proposal is chosen after the single physical cleanup attempt.
/// A delivery retry never chooses a different outcome or runs guest code again.
#[derive(Clone)]
struct CleanupReceipt {
    outcome: Outcome,
    physical_error: Option<StepError>,
    business_error: Option<StepError>,
    delivery_error: Option<StepError>,
}

// This process-lifetime Core owner deliberately lives outside any Session,
// frame, Host or census. An unresolved source may retain those shorter-lived
// objects, but none of them retains this registry. No unknown slot is evicted.
const NATIVE_CLEANUP_SLOTS: usize = 256;
static NATIVE_CLEANUP_OWNER: Mutex<[Option<NativeCleanupEntry>; NATIVE_CLEANUP_SLOTS]> =
    Mutex::new([const { None }; NATIVE_CLEANUP_SLOTS]);

enum NativeCleanupEntry {
    Physical(NativePhysicalEntry),
    Notifications(FundedArc<crate::ui_operation_delivery::UiDeliveryAnchor>),
}
struct NativePhysicalEntry {
    state: Arc<NativeCleanupState>,
    completion: Option<Arc<dyn RunTaskCompletion>>,
    quarantined_unknown: bool,
}
fn vacant_cleanup_slot(
    owner: &[Option<NativeCleanupEntry>; NATIVE_CLEANUP_SLOTS],
) -> Option<usize> {
    owner.iter().position(Option::is_none)
}
/// Only contention of this exact Core-owned registry before mutation is retryable.
pub(crate) enum UiNotificationRegistrationAttempt {
    Registered,
    BusyBeforePublication,
    Rejected(tobkiri_gate::GateError),
}
fn insert_ui_notifications(
    owner: &mut [Option<NativeCleanupEntry>; NATIVE_CLEANUP_SLOTS],
    anchor: &FundedArc<crate::ui_operation_delivery::UiDeliveryAnchor>,
) -> Result<Option<NativeCleanupEntry>, tobkiri_gate::GateError> {
    let index = owner
        .iter()
        .position(|entry| {
            matches!(entry, Some(NativeCleanupEntry::Notifications(value))
            if value.removable())
        })
        // Prefer reclaiming retired funding even when a vacant slot exists;
        // serial Factory replacement must not accumulate old registrations.
        .or_else(|| vacant_cleanup_slot(owner))
        .ok_or_else(|| tobkiri_gate::GateError::Limit("Core cleanup quarantine is full".into()))?;
    // Reuse at most one retired, completely empty notification record. Never
    // displace physical/unknown work or a live/uncertain notification record.
    Ok(owner[index].replace(NativeCleanupEntry::Notifications(anchor.clone())))
}
fn register_ui_notifications_in(
    registry: &Mutex<[Option<NativeCleanupEntry>; NATIVE_CLEANUP_SLOTS]>,
    anchor: &FundedArc<crate::ui_operation_delivery::UiDeliveryAnchor>,
) -> UiNotificationRegistrationAttempt {
    let removed = {
        let mut owner = match registry.try_lock() {
            Ok(owner) => owner,
            Err(std::sync::TryLockError::WouldBlock) => {
                return UiNotificationRegistrationAttempt::BusyBeforePublication;
            }
            Err(std::sync::TryLockError::Poisoned(_)) => {
                return UiNotificationRegistrationAttempt::Rejected(
                    tobkiri_gate::GateError::Unavailable,
                );
            }
        };
        insert_ui_notifications(&mut owner, anchor)
    };
    match removed {
        Ok(removed) => {
            // No payload, funding, callback or notification destruction under
            // the process owner guard, including displaced retired metadata.
            drop(removed);
            UiNotificationRegistrationAttempt::Registered
        }
        Err(error) => UiNotificationRegistrationAttempt::Rejected(error),
    }
}
/// Called during funded, unpublished Factory construction. One nonblocking
/// lock attempt covers the bounded scan and atomic insertion; only its own
/// WouldBlock is retryable, never poison, full capacity or external failures.
pub(crate) fn register_ui_notifications(
    anchor: &FundedArc<crate::ui_operation_delivery::UiDeliveryAnchor>,
) -> UiNotificationRegistrationAttempt {
    register_ui_notifications_in(&NATIVE_CLEANUP_OWNER, anchor)
}
/// Best-effort exact retirement, never physical cleanup proof. Contention or
/// uncertain slot state leaves the original funded entry in the fixed registry;
/// a later observer or registration can reclaim proven retired-empty metadata.
fn remove_retired_ui_notifications_in(
    registry: &Mutex<[Option<NativeCleanupEntry>; NATIVE_CLEANUP_SLOTS]>,
    anchor: &FundedArc<crate::ui_operation_delivery::UiDeliveryAnchor>,
) -> bool {
    let removed = {
        let Ok(mut owner) = registry.try_lock() else {
            return false;
        };
        let index = owner.iter().position(|entry| {
            matches!(entry, Some(NativeCleanupEntry::Notifications(current))
                if FundedArc::ptr_eq(current, anchor))
        });
        match index {
            Some(index) if anchor.removable() => owner[index].take(),
            _ => None,
        }
    };
    let removed_any = removed.is_some();
    // Release storage/funding only after the registry guard has been dropped.
    drop(removed);
    removed_any
}
pub(crate) fn remove_retired_ui_notifications(
    anchor: &FundedArc<crate::ui_operation_delivery::UiDeliveryAnchor>,
) {
    remove_retired_ui_notifications_in(&NATIVE_CLEANUP_OWNER, anchor);
}
fn observe_ui_notifications(
    index: usize,
    anchor: &FundedArc<crate::ui_operation_delivery::UiDeliveryAnchor>,
) {
    if !anchor.removable() {
        return;
    }
    let removed = {
        let mut owner = NATIVE_CLEANUP_OWNER
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        match owner[index].as_ref() {
            Some(NativeCleanupEntry::Notifications(current))
                if FundedArc::ptr_eq(current, anchor) && anchor.removable() =>
            {
                owner[index].take()
            }
            _ => None,
        }
    };
    drop(removed);
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum SourceHandoff {
    Pending,
    Attached,
    NoSource,
}
struct NativeCleanupIntent {
    handoff: SourceHandoff,
    outcome: Option<Outcome>,
    early_close: Option<StreamCloseReason>,
    // Publication and early-close receipt share one boundary. Separate locks
    // permit a closer to observe no source while attach observes no cause.
    source_control: Option<Arc<dyn StreamCloseControl>>,
    abandon_failed: bool,
}
struct NativeCleanupState {
    // These are independently owned BEFORE a supervisor can panic. Neither the
    // unique source nor the terminal is moved into task-local custody at await.
    source: tokio::sync::Mutex<Option<CoreStream>>,
    body: Arc<dyn RunTaskCompletion>,
    terminal: tokio::sync::Mutex<Option<ConsumedEntryTerminal>>,
    factory: Digest,
    intent: Mutex<NativeCleanupIntent>,
    receipt: Mutex<Option<CleanupReceipt>>,
    cleanup_proved: AtomicBool,
    quarantined_unknown: AtomicBool,
    ready: tokio::sync::Notify,
    cx: CallCx,
    #[cfg(test)]
    before_close_receipt: Mutex<Option<Box<dyn FnOnce() + Send>>>,
    _reservation: ControlReservation,
}
impl NativeCleanupState {
    fn request_close(&self, outcome: Outcome) {
        let mut intent = self
            .intent
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        if intent.outcome.is_none() {
            intent.outcome = Some(outcome);
        }
        drop(intent);
        self.ready.notify_one();
    }
    fn no_source(&self, outcome: Outcome, abandon_failed: bool) {
        let mut intent = self
            .intent
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        if intent.handoff == SourceHandoff::Pending {
            intent.handoff = SourceHandoff::NoSource;
        }
        if intent.outcome.is_none() {
            intent.outcome = Some(outcome);
        }
        intent.abandon_failed |= abandon_failed;
        drop(intent);
        self.ready.notify_one();
    }
    fn attach(&self, source: CoreStream) {
        // There is no await between the interpreter returning its actual
        // source and this owner-held installation. The supervisor waits for
        // Attached/NoSource; an early seal cannot race it into the wrong branch.
        let mut slot = self
            .source
            .try_lock()
            .expect("unattached native source slot");
        assert!(slot.is_none(), "native source is attached once");
        *slot = Some(source);
        let control = slot.as_ref().and_then(CoreStream::close_control);
        drop(slot);
        let early_close = {
            let mut intent = self
                .intent
                .lock()
                .unwrap_or_else(|error| error.into_inner());
            assert!(
                intent.handoff == SourceHandoff::Pending,
                "native source handoff is unique"
            );
            intent.source_control = control.clone();
            intent.handoff = SourceHandoff::Attached;
            intent.early_close
        };
        if let (Some(control), Some(reason)) = (control, early_close) {
            let _ = control.record_close(reason);
        }
        self.ready.notify_one();
    }
    fn receipt(&self) -> Option<CleanupReceipt> {
        self.receipt
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .clone()
    }
}
impl StreamCloseControl for NativeCleanupState {
    fn record_close(&self, reason: StreamCloseReason) -> Result<(), StepError> {
        let control = {
            let mut intent = self
                .intent
                .lock()
                .unwrap_or_else(|error| error.into_inner());
            let control = intent.source_control.clone();
            // Deterministic test seam at the former split-lock window. This
            // hook and its storage do not exist in production builds.
            #[cfg(test)]
            if let Some(before_receipt) = self.before_close_receipt.lock().unwrap().take() {
                before_receipt();
            }
            if intent.early_close.is_none() {
                intent.early_close = Some(reason);
            }
            control
        };
        // The actual source selects under its own original guards. Locks are
        // released before callbacks and token notifications; none certify Join.
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            control.map_or(Ok(()), |control| control.record_close(reason))
        }))
        .unwrap_or_else(|_| Err(StepError::new("trap", "Flow close control trapped")));
        if reason == StreamCloseReason::Deadline {
            self.cx.cancel().notify_deadline();
        } else {
            self.cx.cancel().cancel();
        }
        self.request_close(Outcome::Cancelled);
        result
    }
    fn record_failure(&self, error: &StepError) -> Result<(), StepError> {
        let control = self
            .intent
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .source_control
            .clone();
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            control.map_or(Ok(()), |control| control.record_failure(error))
        }))
        .unwrap_or_else(|_| Err(StepError::new("trap", "Flow failure control trapped")));
        self.request_close(Outcome::Failed {
            code: error.audit_code().into(),
        });
        result
    }
}
struct NativeCleanup {
    state: Arc<NativeCleanupState>,
    completion: Arc<dyn RunTaskCompletion>,
    owner_slot: usize,
    observed: bool,
}
struct NativeCleanupRun {
    state: Arc<NativeCleanupState>,
    completed: bool,
}
impl NativeCleanupRun {
    async fn execute(mut self) -> RunTaskPhysicalResult {
        let result = supervise_native_cleanup(self.state.clone()).await;
        self.completed = result.is_ok();
        result
    }
}
impl Drop for NativeCleanupRun {
    fn drop(&mut self) {
        if !self.completed {
            // This guard is captured BEFORE task publication, so even an
            // unpolled task drop/panic marks the independently held slot before
            // its actual monitor can report Unknown. No custody moves here.
            self.state
                .quarantined_unknown
                .store(true, Ordering::Release);
        }
    }
}

fn observe_native_cleanup(
    index: usize,
    state: &Arc<NativeCleanupState>,
    result: &Result<(), StepError>,
) {
    let removed = {
        let mut owner = NATIVE_CLEANUP_OWNER
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        match owner[index].as_mut() {
            Some(NativeCleanupEntry::Physical(entry)) if Arc::ptr_eq(&entry.state, state) => {
                if result.is_ok() && state.cleanup_proved.load(Ordering::Acquire) {
                    owner[index].take()
                } else {
                    entry.quarantined_unknown = true;
                    state.quarantined_unknown.store(true, Ordering::Release);
                    None
                }
            }
            _ => None,
        }
    };
    // Source/terminal/control release is never performed under the owner lock.
    drop(removed);
}

fn poll_native_cleanup_owner() {
    // Called outside all caller guards. Each variant retains its own original
    // custody and proof; notification ACK never certifies physical cleanup.
    for index in 0..NATIVE_CLEANUP_SLOTS {
        let (physical, notifications) = {
            let owner = NATIVE_CLEANUP_OWNER
                .lock()
                .unwrap_or_else(|error| error.into_inner());
            match owner[index].as_ref() {
                Some(NativeCleanupEntry::Physical(entry)) if !entry.quarantined_unknown => (
                    entry
                        .completion
                        .as_ref()
                        .map(|completion| (entry.state.clone(), completion.clone())),
                    None,
                ),
                Some(NativeCleanupEntry::Notifications(anchor)) => (None, Some(anchor.clone())),
                _ => (None, None),
            }
        };
        if let Some(anchor) = notifications {
            let _ = anchor.flush();
            observe_ui_notifications(index, &anchor);
        }
        if let Some((state, completion)) = physical {
            let Ok(_waiter) = state.cx.control().reserve(ControlKind::Waiter, 512) else {
                continue;
            };
            let mut future = completion.wait();
            let mut context = Context::from_waker(Waker::noop());
            if let Poll::Ready(result) = future.as_mut().poll(&mut context) {
                observe_native_cleanup(index, &state, &result);
            }
        }
    }
}

/// The cleanup task and its real Join are published before the interpreter.
/// Dropping this plan before source attachment delivers the exact terminal to
/// that already-owned task; no late post-seal task needs to be created.
pub(crate) struct PreparedNativeCleanup {
    native: Option<NativeCleanup>,
    body_spawner: Option<Arc<dyn tobkiri_flow::RunTaskSpawner>>,
    armed: bool,
}
impl PreparedNativeCleanup {
    fn no_source(&mut self, outcome: Outcome) {
        let abandoned = self.body_spawner.as_ref().map_or(Ok(()), |spawner| {
            std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| spawner.abandon_unstarted()))
                .unwrap_or_else(|_| Err(StepError::new("trap", "Flow mailbox abandonment trapped")))
        });
        if let Some(native) = &self.native {
            // Only notification: never supply an effect, cause or physical
            // assertion from a task-start failure or an abandoned caller.
            native.state.cx.cancel().cancel();
            native.state.no_source(outcome, abandoned.is_err());
        }
        self.body_spawner.take();
    }
    pub(crate) async fn wait_started(&self) -> Result<(), StepError> {
        self.native
            .as_ref()
            .expect("prepared native cleanup")
            .completion
            .wait_started()
            .await
    }
    pub(crate) fn fail(mut self, outcome: Outcome) {
        self.no_source(outcome);
        self.armed = false;
    }
    pub(crate) fn attach(
        mut self,
        source: CoreStream,
        gate: Arc<GateEngine>,
        cx: CallCx,
        runtime: Handle,
    ) -> TrackedStream {
        // Install in the independent owner before constructing the wrapper.
        // Neither boxing nor an abandoned caller future can drop sole custody.
        self.native
            .as_ref()
            .expect("prepared native cleanup")
            .state
            .attach(source);
        let native = self.native.take().expect("prepared native cleanup");
        self.body_spawner.take();
        let tracked = TrackedStream {
            source: None,
            gate,
            cx,
            grant: None,
            entry_terminal: None,
            terminal: None,
            runtime,
            cleanup: None,
            receipt: None,
            cleanup_error: None,
            end_batch: None,
            end_buffer: None,
            closed: false,
            native_cleanup: Some(native),
        };
        self.armed = false;
        tracked
    }
}
impl Drop for PreparedNativeCleanup {
    fn drop(&mut self) {
        if self.armed {
            self.no_source(Outcome::Cancelled);
        }
    }
}

async fn supervise_native_cleanup(state: Arc<NativeCleanupState>) -> RunTaskPhysicalResult {
    let mut cancelled = false;
    let mut deadline_seen = false;
    let (handoff, proposed, abandon_failed) = loop {
        let notified = state.ready.notified();
        let ready = {
            let intent = state
                .intent
                .lock()
                .unwrap_or_else(|error| error.into_inner());
            if intent.handoff != SourceHandoff::Pending {
                intent
                    .outcome
                    .clone()
                    .map(|outcome| (intent.handoff, outcome, intent.abandon_failed))
            } else {
                None
            }
        };
        if let Some(ready) = ready {
            break ready;
        }
        tokio::select! {
            _ = notified => {}
            _ = state.cx.cancel().cancelled(), if !cancelled => {
                cancelled = true;
                // A wake requests physical cleanup; it never infers a cause
                // such as revocation from the token alone.
                state.request_close(Outcome::Cancelled);
            }
            _ = tokio::time::sleep_until(state.cx.deadline().into()), if !deadline_seen => {
                deadline_seen = true;
                let _ = state.record_close(StreamCloseReason::Deadline);
            }
        }
    };
    let (cleanup, physical, producer_terminal) = if handoff == SourceHandoff::Attached {
        let mut slot = state.source.lock().await;
        match slot.as_mut() {
            Some(source) => {
                let physical = source.physical_completion();
                let closed = source.close().await;
                let producer_terminal = source.terminal_outcome().map(|terminal| match terminal {
                    StreamTerminal::Succeeded => Outcome::Succeeded,
                    StreamTerminal::Cancelled => Outcome::Cancelled,
                    StreamTerminal::Failed(code) => Outcome::Failed { code: code.into() },
                });
                let mut physical = match physical {
                    Some(physical) => physical.wait().await,
                    None => Err(StepError::new(
                        "unavailable",
                        "Flow source physical custody is unavailable",
                    )),
                };
                if physical.is_ok() {
                    // Only an actual source/body receipt permits destruction.
                    // A failed destructor is uncertainty; remaining terminal
                    // custody/charges still stay in the independent owner.
                    if std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| drop(slot.take())))
                        .is_err()
                    {
                        physical = Err(StepError::new("trap", "Flow source destruction trapped"));
                    }
                }
                (closed, physical, producer_terminal)
            }
            None => (
                Err(StepError::new("internal", "Flow source custody is missing")),
                Err(StepError::new(
                    "component",
                    "Flow source physical completion is unknown",
                )),
                None,
            ),
        }
    } else {
        // Interpreter start failed or was abandoned. Only the original body
        // receipt can prove its physical status; no terminal substitutes for it.
        let physical = if abandon_failed {
            Err(StepError::new(
                "component",
                "Flow unsubmitted mailbox custody remains unknown",
            ))
        } else {
            state.body.wait().await
        };
        (Ok(()), physical, None)
    };
    let mut outcome = match &cleanup {
        Ok(()) if matches!(&proposed, Outcome::Failed { .. }) => proposed,
        Ok(()) => producer_terminal.unwrap_or(proposed),
        Err(error) => Outcome::Failed {
            code: error.audit_code().into(),
        },
    };
    let mut terminal_slot = state.terminal.lock().await;
    let (delivery_error, handed_off) = match terminal_slot.as_mut() {
        Some(terminal) => {
            let delivery =
                terminal.finish(outcome.clone()).await.err().map(|error| {
                    StepError::new(error.code(), "Flow outcome could not be recorded")
                });
            // An Err alone cannot distinguish pre-commit failure from an
            // already committed pending-ACK outbox record. Inspect exact sealed
            // custody; the existing Gate supervisor/startup recovery owns retry.
            match terminal.durable_handoff() {
                Ok(winner) => {
                    outcome = winner;
                    (delivery, true)
                }
                Err(error) => (
                    delivery.or_else(|| {
                        Some(StepError::new(
                            error.code(),
                            "Flow terminal custody remains unknown",
                        ))
                    }),
                    false,
                ),
            }
        }
        None => (
            Some(StepError::new(
                "internal",
                "Flow terminal custody is missing",
            )),
            false,
        ),
    };
    drop(terminal_slot);
    let physical_proved = physical.is_ok();
    // Only fixed booleans from the retained native receipts, outside source,
    // terminal and receipt guards. Business text is not diagnostic metadata.
    #[cfg(debug_assertions)]
    if !physical_proved || !handed_off {
        eprintln!(
            "FLOW_CLEANUP_UNKNOWN physical_proved={physical_proved} terminal_handoff={handed_off} business_failed={} audit_failed={}",
            cleanup.is_err(),
            delivery_error.is_some()
        );
    }
    *state
        .receipt
        .lock()
        .unwrap_or_else(|error| error.into_inner()) = Some(CleanupReceipt {
        outcome,
        physical_error: physical.err(),
        business_error: cleanup.err(),
        delivery_error,
    });
    if physical_proved && handed_off {
        // This flag alone cannot release custody. The owner separately observes
        // THIS supervisor's original actual Join before removing the entry.
        state.cleanup_proved.store(true, Ordering::Release);
        Ok(())
    } else {
        // The supervisor may end, but original source/terminal/proposal and
        // charges survive in the independently owned quarantine entry.
        Err(RunTaskPhysicalError::CompletionUnknown)
    }
}
impl CleanupReceipt {
    fn result(&self) -> Result<(), StepError> {
        match self
            .physical_error
            .as_ref()
            .or(self.delivery_error.as_ref())
            .or(self.business_error.as_ref())
        {
            Some(error) => Err(error.clone()),
            None => Ok(()),
        }
    }
}
impl TrackedStream {
    /// Drain only this exact factory's already registered cleanup work. The
    /// total wait is bounded and borrows the original receipts; cancellation or
    /// timeout leaves original custody and charges in the process owner.
    pub(crate) async fn check_native_cleanup(factory: &Digest) -> Result<(), StepError> {
        let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(2);
        for index in 0..NATIVE_CLEANUP_SLOTS {
            let (candidate, notifications) = {
                let owner = NATIVE_CLEANUP_OWNER
                    .lock()
                    .unwrap_or_else(|error| error.into_inner());
                match owner[index].as_ref() {
                    Some(NativeCleanupEntry::Physical(entry))
                        if &entry.state.factory == factory =>
                    {
                        (Some((entry.state.clone(), entry.completion.clone())), None)
                    }
                    Some(NativeCleanupEntry::Notifications(anchor))
                        if &anchor.factory == factory =>
                    {
                        (None, Some(anchor.clone()))
                    }
                    _ => (None, None),
                }
            };
            if let Some(anchor) = notifications {
                anchor.retire();
                anchor.flush()?;
                observe_ui_notifications(index, &anchor);
            }
            if let Some((state, completion)) = candidate {
                let _ = state.record_close(StreamCloseReason::Shutdown);
                let completion = completion.ok_or_else(|| {
                    StepError::new("unavailable", "Flow cleanup submission remains unknown")
                })?;
                let _waiter = state
                    .cx
                    .control()
                    .reserve(ControlKind::Waiter, 512)
                    .map_err(|_| {
                        StepError::new("limit-exceeded", "Flow cleanup waiter capacity exhausted")
                    })?;
                let joined = tokio::time::timeout_at(deadline, completion.wait())
                    .await
                    .map_err(|_| {
                        StepError::new("unavailable", "Flow cleanup remains physically unresolved")
                    })?;
                observe_native_cleanup(index, &state, &joined);
                joined?;
            }
        }
        let mut unresolved = false;
        for index in 0..NATIVE_CLEANUP_SLOTS {
            let (physical, notifications) = {
                let owner = NATIVE_CLEANUP_OWNER
                    .lock()
                    .unwrap_or_else(|error| error.into_inner());
                match owner[index].as_ref() {
                    Some(NativeCleanupEntry::Physical(entry)) => {
                        (&entry.state.factory == factory, None)
                    }
                    Some(NativeCleanupEntry::Notifications(anchor))
                        if &anchor.factory == factory =>
                    {
                        (false, Some(anchor.clone()))
                    }
                    _ => (false, None),
                }
            };
            // Do not inspect/drop notification captures under the process
            // owner guard. Settled live metadata stays charged and pinned;
            // removal still requires the original released/drained proof.
            if physical
                || notifications
                    .as_ref()
                    .is_some_and(|anchor| !anchor.notifications_settled())
            {
                unresolved = true;
                break;
            }
        }
        if unresolved {
            Err(StepError::new(
                "unavailable",
                "Flow cleanup quarantine remains charged",
            ))
        } else {
            Ok(())
        }
    }

    /// Prepare the source's actual cleanup sibling before starting a
    /// generation-bound interpreter. Pure Flow has no native lifetime scope.
    pub(crate) fn prepare_native_cleanup(
        frame: &ControlHandle<dyn ActiveCodeFrame>,
        factory: &ExecutionFactory,
        terminal: &mut Option<ConsumedEntryTerminal>,
    ) -> Result<Option<PreparedNativeCleanup>, StepError> {
        let Some(spawner) = frame.task_spawner() else {
            return Ok(None);
        };
        if terminal.is_none() {
            return Err(StepError::new(
                "internal",
                "Flow terminal custody is missing",
            ));
        }
        let execution = frame.task_execution().ok_or_else(|| {
            StepError::new("unavailable", "Flow task lifetime identity is unavailable")
        })?;
        let body = spawner.completion()?;
        let control = frame.context().control();
        let bytes = (std::mem::size_of::<NativeCleanupState>()
            + std::mem::size_of::<NativeCleanupEntry>()
            + 4096)
            .checked_add(frame.context().clone_storage_bytes().map_err(|_| {
                StepError::new("limit-exceeded", "Flow cleanup context bound overflow")
            })?)
            .and_then(|bytes| u64::try_from(bytes).ok())
            .ok_or_else(|| {
                StepError::new("limit-exceeded", "Flow cleanup custody bound overflow")
            })?;
        poll_native_cleanup_owner();
        let reservation = control.reserve(ControlKind::Queue, bytes).map_err(|_| {
            StepError::new("limit-exceeded", "Flow cleanup custody capacity exhausted")
        })?;
        let cx = frame.context().try_clone_precharged().map_err(|_| {
            StepError::new("limit-exceeded", "Flow cleanup context allocation failed")
        })?;
        let state = Arc::new(NativeCleanupState {
            source: tokio::sync::Mutex::new(None),
            body,
            terminal: tokio::sync::Mutex::new(None),
            factory: factory.native_cleanup_identity().clone(),
            intent: Mutex::new(NativeCleanupIntent {
                handoff: SourceHandoff::Pending,
                outcome: None,
                early_close: None,
                source_control: None,
                abandon_failed: false,
            }),
            receipt: Mutex::new(None),
            cleanup_proved: AtomicBool::new(false),
            quarantined_unknown: AtomicBool::new(false),
            ready: tokio::sync::Notify::new(),
            cx,
            #[cfg(test)]
            before_close_receipt: Mutex::new(None),
            _reservation: reservation,
        });
        let owner_slot = factory.with_native_cleanup_registration(|| {
            let mut owner = NATIVE_CLEANUP_OWNER
                .lock()
                .unwrap_or_else(|error| error.into_inner());
            let index = vacant_cleanup_slot(&owner).ok_or_else(|| {
                StepError::new("limit-exceeded", "Core cleanup quarantine is full")
            })?;
            owner[index] = Some(NativeCleanupEntry::Physical(NativePhysicalEntry {
                state: state.clone(),
                completion: None,
                quarantined_unknown: false,
            }));
            Ok(index)
        })?;
        *state.terminal.try_lock().expect("new native terminal slot") = terminal.take();
        let prepared = (|| {
            let cleanup = spawner.cleanup_child(execution, frame.context().cancel().clone())?;
            let completion = cleanup.completion()?;
            {
                let mut owner = NATIVE_CLEANUP_OWNER
                    .lock()
                    .unwrap_or_else(|error| error.into_inner());
                match owner[owner_slot].as_mut() {
                    Some(NativeCleanupEntry::Physical(entry))
                        if Arc::ptr_eq(&entry.state, &state) =>
                    {
                        entry.completion = Some(completion.clone());
                    }
                    _ => return Err(StepError::new("internal", "Flow cleanup custody mismatch")),
                }
            }
            let run = NativeCleanupRun {
                state: state.clone(),
                completed: false,
            };
            let task = RunTask::cleanup(run.execute(), control)?;
            cleanup.spawn(task, state.clone())?;
            Ok(PreparedNativeCleanup {
                native: Some(NativeCleanup {
                    state: state.clone(),
                    completion,
                    owner_slot,
                    observed: false,
                }),
                body_spawner: Some(spawner.clone()),
                armed: true,
            })
        })();
        if prepared.is_err() {
            // Host guarantees Err means the task payload was never accepted.
            // No source exists and the supervisor cannot pass Pending handoff.
            *terminal = state
                .terminal
                .try_lock()
                .expect("unstarted native terminal slot")
                .take();
            let removed = {
                let mut owner = NATIVE_CLEANUP_OWNER
                    .lock()
                    .unwrap_or_else(|error| error.into_inner());
                match owner[owner_slot].as_ref() {
                    Some(NativeCleanupEntry::Physical(entry))
                        if Arc::ptr_eq(&entry.state, &state) =>
                    {
                        owner[owner_slot].take()
                    }
                    _ => None,
                }
            };
            drop(removed);
        }
        prepared.map(Some)
    }
    #[cfg(test)]
    pub fn new(
        source: CoreStream,
        gate: Arc<GateEngine>,
        cx: CallCx,
        grant: GrantId,
        runtime: Handle,
    ) -> Self {
        Self {
            source: Some(source),
            gate,
            cx,
            grant: Some(grant),
            entry_terminal: None,
            terminal: None,
            runtime,
            cleanup: None,
            receipt: None,
            cleanup_error: None,
            end_batch: None,
            end_buffer: None,
            closed: false,
            native_cleanup: None,
        }
    }
    /// Wrap a nested canonical Flow whose exact consumed permit remains in
    /// Core-local terminal custody. The terminal task retains that custody
    /// until the immutable outcome is durably accepted.
    pub fn new_with_terminal(
        source: CoreStream,
        gate: Arc<GateEngine>,
        cx: CallCx,
        terminal: ConsumedEntryTerminal,
        runtime: Handle,
    ) -> Self {
        Self {
            source: Some(source),
            gate,
            cx,
            grant: None,
            entry_terminal: Some(terminal),
            terminal: None,
            runtime,
            cleanup: None,
            receipt: None,
            cleanup_error: None,
            end_batch: None,
            end_buffer: None,
            closed: false,
            native_cleanup: None,
        }
    }
    fn begin_close(&mut self, outcome: Outcome) {
        if self.closed {
            return;
        }
        self.closed = true;
        if let Some(native) = &self.native_cleanup {
            native.state.request_close(outcome);
            return;
        }
        let mut source = self.source.take().expect("live tracked source");
        // Stop the run immediately; asynchronous guest/audit cleanup follows on
        // the captured runtime even when this call comes from a native thread.
        let cancelled = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| source.cancel()))
            .map_err(|_| StepError::new("trap", "Flow stream cancellation trapped"));
        let gate = self.gate.clone();
        let grant = self.grant.clone();
        let mut entry_terminal = self.entry_terminal.take();
        let task = async move {
            let closed = source.close().await;
            let cleanup = cancelled.and(closed);
            let outcome = match &cleanup {
                Ok(()) => outcome,
                Err(error) => Outcome::Failed {
                    code: error.audit_code().into(),
                },
            };
            let delivery_error = if let Some(terminal) = entry_terminal.as_mut() {
                // Retain this sole terminal owner in the cleanup task until
                // the same immutable proposal is durably accepted. Backoff
                // keeps transient persistence failures from spinning while
                // the durable deadline supervisor remains active.
                loop {
                    match terminal.finish(outcome.clone()).await {
                        Ok(()) => break None,
                        Err(_) => tokio::time::sleep(std::time::Duration::from_millis(50)).await,
                    }
                }
            } else if let Some(grant) = grant {
                gate.record_outcome(grant, outcome.clone())
                    .await
                    .err()
                    .map(|e| StepError::new(e.code(), "Flow outcome could not be recorded"))
            } else {
                None
            };
            CleanupReceipt {
                outcome,
                physical_error: cleanup.err(),
                business_error: None,
                delivery_error,
            }
        };
        match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| self.runtime.spawn(task))) {
            Ok(task) => self.cleanup = Some(task),
            Err(_) => {
                self.cleanup_error = Some(StepError::new(
                    "internal",
                    "owning runtime could not schedule Flow cleanup",
                ));
            }
        }
    }
    async fn await_cleanup(&mut self) -> Result<(), StepError> {
        if let Some(native) = self.native_cleanup.as_mut() {
            if !native.observed {
                let joined = native.completion.wait().await;
                observe_native_cleanup(native.owner_slot, &native.state, &joined);
                self.cleanup_error = joined.as_ref().err().cloned();
                native.observed = true;
                self.receipt = native.state.receipt();
                if let Some(receipt) = &self.receipt {
                    // Preserve the original business/audit error even when an
                    // exact durable handoff lets physical cleanup complete.
                    receipt.result()?;
                }
                joined?;
                return Ok(());
            }
            if self
                .receipt
                .as_ref()
                .is_some_and(|receipt| receipt.delivery_error.is_some())
            {
                let mut receipt = self.receipt.clone().expect("known native cleanup receipt");
                let mut terminal = native.state.terminal.lock().await;
                receipt.delivery_error = match terminal.as_mut() {
                    Some(terminal) => {
                        terminal
                            .finish(receipt.outcome.clone())
                            .await
                            .err()
                            .map(|error| {
                                StepError::new(error.code(), "Flow outcome could not be recorded")
                            })
                    }
                    None => Some(StepError::new(
                        "internal",
                        "Flow terminal custody is missing",
                    )),
                };
                // This only retries the same stored terminal/ACK. It cannot
                // rewrite a cached physical Unknown or rerun any source work.
                self.receipt = Some(receipt);
            }
            self.receipt.as_ref().map_or_else(
                || {
                    Err(StepError::new(
                        "component",
                        "Flow cleanup physical completion is unknown",
                    ))
                },
                CleanupReceipt::result,
            )?;
            return self
                .cleanup_error
                .as_ref()
                .map_or(Ok(()), |error| Err(error.clone()));
        }
        // Retain the task across an abandoned close future. Joining a cancelled
        // owning runtime yields an error; it does not certify physical cleanup.
        if let Some(cleanup) = self.cleanup.as_mut() {
            let result = cleanup.await.map_err(|_| {
                StepError::new("internal", "owning runtime did not complete Flow cleanup")
            });
            self.cleanup.take();
            match result {
                Ok(receipt) => {
                    let result = receipt.result();
                    if receipt.delivery_error.is_none() {
                        self.grant.take();
                    }
                    self.receipt = Some(receipt);
                    // Report the first delivery failure to this caller; a later
                    // explicit close/pull may resend that immutable proposal.
                    return result;
                }
                Err(error) => self.cleanup_error = Some(error),
            }
        }
        if let Some(error) = &self.cleanup_error {
            return Err(error.clone());
        }
        if self
            .receipt
            .as_ref()
            .is_some_and(|r| r.delivery_error.is_some())
        {
            let gate = self.gate.clone();
            let grant = self.grant.clone();
            let mut receipt = self.receipt.take().expect("known cleanup receipt");
            // Only audit metadata is retried. Use the captured runtime, retaining
            // the task so cancelling this observing future cannot lose its result.
            let task = async move {
                receipt.delivery_error = if let Some(grant) = grant {
                    gate.record_outcome(grant, receipt.outcome.clone())
                        .await
                        .err()
                        .map(|e| StepError::new(e.code(), "Flow outcome could not be recorded"))
                } else {
                    None
                };
                receipt
            };
            match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                self.runtime.spawn(task)
            })) {
                Ok(task) => self.cleanup = Some(task),
                Err(_) => {
                    let error =
                        StepError::new("internal", "owning runtime could not retry Flow audit");
                    self.cleanup_error = Some(error.clone());
                    return Err(error);
                }
            }
            // Join the newly retained metadata-only task without recursion.
            let result = self.cleanup.as_mut().expect("audit retry task").await;
            self.cleanup.take();
            match result {
                Ok(receipt) => {
                    let result = receipt.result();
                    if receipt.delivery_error.is_none() {
                        self.grant.take();
                    }
                    self.receipt = Some(receipt);
                    return result;
                }
                Err(_) => {
                    let error =
                        StepError::new("internal", "owning runtime did not complete Flow audit");
                    self.cleanup_error = Some(error.clone());
                    return Err(error);
                }
            }
        }
        self.receipt.as_ref().map_or(Ok(()), CleanupReceipt::result)
    }
    async fn finish(&mut self, outcome: Outcome) -> Result<(), StepError> {
        self.begin_close(outcome);
        self.await_cleanup().await
    }
}
#[async_trait]
impl StreamSource for TrackedStream {
    async fn pull(&mut self, max: u32) -> Result<CoreValue, StepError> {
        if self.closed {
            self.await_cleanup().await?;
            return self.end_batch.clone().ok_or_else(|| {
                StepError::new("unavailable", "closed Flow stream has no owned empty batch")
            });
        }
        if self.cx.check_active().is_err()
            || self
                .gate
                .epoch(self.cx.profile())
                .map_err(|e| StepError::new(e.code(), "epoch unavailable"))?
                != self.cx.security_epoch()
        {
            self.finish(Outcome::Cancelled).await?;
            return Err(StepError::new("cancelled", "Flow stream authority expired"));
        }
        let result = if let Some(native) = &self.native_cleanup {
            let mut source = native.state.source.lock().await;
            if let Some(source) = source.as_mut() {
                tokio::select! {
                    _=self.cx.cancel().cancelled()=>Err(StepError::new("cancelled","Flow stream cancelled")),
                    _=tokio::time::sleep_until(self.cx.deadline().into())=>Err(StepError::new("cancelled","original Flow deadline expired")),
                    result=source.pull(max)=>result,
                }
            } else {
                Err(StepError::new(
                    "cancelled",
                    "Flow source has completed cleanup",
                ))
            }
        } else {
            let source = self.source.as_mut().expect("live tracked source");
            tokio::select! {
                _=self.cx.cancel().cancelled()=>Err(StepError::new("cancelled","Flow stream cancelled")),
                _=tokio::time::sleep_until(self.cx.deadline().into())=>Err(StepError::new("cancelled","original Flow deadline expired")),
                result=source.pull(max)=>result,
            }
        };
        match &result {
            Ok(values) => {
                let Some(values) = values.array() else {
                    self.finish(Outcome::Failed {
                        code: "invalid".into(),
                    })
                    .await?;
                    return Err(StepError::new(
                        "invalid",
                        "Flow stream returned a non-array batch",
                    ));
                };
                for value in values.iter() {
                    if let Ok(event) = RunEvent::from_value(&value) {
                        self.terminal = match event.kind {
                            RunEventKind::Completed { .. } => Some(Outcome::Succeeded),
                            RunEventKind::Failed { error } => Some(Outcome::Failed {
                                code: error.audit_code().into(),
                            }),
                            RunEventKind::Cancelled { .. } => Some(Outcome::Cancelled),
                            _ => self.terminal.take(),
                        };
                    }
                }
                if values.is_empty() {
                    self.end_batch = Some(result.as_ref().expect("successful batch").clone());
                    let Some(outcome) = self.terminal.take() else {
                        self.finish(Outcome::Failed {
                            code: "internal".into(),
                        })
                        .await?;
                        return Err(StepError::new(
                            "internal",
                            "Flow stream ended without a terminal event",
                        ));
                    };
                    self.finish(outcome).await?;
                }
            }
            Err(error) => {
                // Flow's native transport retains the uncommitted batch when
                // a bounded response buffer cannot be produced. Leave the
                // same stream and FlowRun grant live so the host may retry.
                if error.code == "batch-too-large" {
                    return Err(error.clone());
                }
                self.finish(if error.code == "cancelled" {
                    Outcome::Cancelled
                } else {
                    Outcome::Failed {
                        code: error.audit_code().into(),
                    }
                })
                .await?
            }
        }
        result
    }

    async fn pull_final(&mut self, max: u32) -> Result<CoreBuffer, StepError> {
        if max == 0 {
            return Err(StepError::new(
                "invalid",
                "stream pull size must be positive",
            ));
        }
        if self.closed {
            self.await_cleanup().await?;
            return self.end_buffer.clone().ok_or_else(|| {
                StepError::new(
                    "unavailable",
                    "closed Flow stream has no producer-encoded empty batch",
                )
            });
        }
        if self.cx.check_active().is_err()
            || self
                .gate
                .epoch(self.cx.profile())
                .map_err(|e| StepError::new(e.code(), "epoch unavailable"))?
                != self.cx.security_epoch()
        {
            self.finish(Outcome::Cancelled).await?;
            return Err(StepError::new("cancelled", "Flow stream authority expired"));
        }
        let (result, producer_terminal) = if let Some(native) = &self.native_cleanup {
            let mut source = native.state.source.lock().await;
            if let Some(source) = source.as_mut() {
                let result = tokio::select! {
                    _=self.cx.cancel().cancelled()=>Err(StepError::new("cancelled","Flow stream cancelled")),
                    _=tokio::time::sleep_until(self.cx.deadline().into())=>Err(StepError::new("cancelled","original Flow deadline expired")),
                    result=source.pull_final(max)=>result,
                };
                (result, source.terminal_outcome())
            } else {
                (
                    Err(StepError::new(
                        "cancelled",
                        "Flow source has completed cleanup",
                    )),
                    None,
                )
            }
        } else {
            let source = self.source.as_mut().expect("live tracked source");
            let result = tokio::select! {
                _=self.cx.cancel().cancelled()=>Err(StepError::new("cancelled","Flow stream cancelled")),
                _=tokio::time::sleep_until(self.cx.deadline().into())=>Err(StepError::new("cancelled","original Flow deadline expired")),
                result=source.pull_final(max)=>result,
            };
            (result, source.terminal_outcome())
        };
        match result {
            Ok(buffer) => {
                if let Some(terminal) = producer_terminal {
                    self.terminal = Some(match terminal {
                        StreamTerminal::Succeeded => Outcome::Succeeded,
                        StreamTerminal::Cancelled => Outcome::Cancelled,
                        StreamTerminal::Failed(code) => Outcome::Failed { code: code.into() },
                    });
                }
                if buffer.as_bytes() == b"[]" {
                    self.end_buffer = Some(buffer.clone());
                    let Some(outcome) = self.terminal.take() else {
                        self.finish(Outcome::Failed {
                            code: "internal".into(),
                        })
                        .await?;
                        return Err(StepError::new(
                            "internal",
                            "Flow stream ended without terminal metadata",
                        ));
                    };
                    self.finish(outcome).await?;
                }
                Ok(buffer)
            }
            Err(error) => {
                if error.code == "batch-too-large" {
                    return Err(error);
                }
                self.finish(if error.code == "cancelled" {
                    Outcome::Cancelled
                } else {
                    Outcome::Failed {
                        code: error.audit_code().into(),
                    }
                })
                .await?;
                Err(error)
            }
        }
    }

    fn cancel(&mut self) {
        if !self.closed
            && let Some(native) = &self.native_cleanup
        {
            let _ = native.state.record_close(StreamCloseReason::Unknown);
        }
        let outcome = self.terminal.take().unwrap_or(Outcome::Cancelled);
        self.begin_close(outcome);
    }
    fn close_report(&self) -> Option<tobkiri_flow::StreamCloseReport> {
        let native = self.native_cleanup.as_ref()?;
        if !native.observed
            || self.cleanup_error.is_some()
            || !native.state.cleanup_proved.load(Ordering::Acquire)
            || native.state.quarantined_unknown.load(Ordering::Acquire)
        {
            return None;
        }
        let receipt = self.receipt.as_ref()?;
        if receipt.physical_error.is_some() {
            return None;
        }
        // prepare_native_cleanup/attach install only the actual interpreter
        // source from this factory's original frame. cleanup_proved records its
        // real destruction/body receipt plus the same sealed terminal handoff;
        // observed independently certifies this supervisor's original Join.
        tobkiri_flow::StreamCloseReport::observed(
            &receipt.outcome,
            receipt.business_error.as_ref(),
            receipt.delivery_error.as_ref(),
            if receipt.delivery_error.is_some() {
                tobkiri_flow::StreamCloseDelivery::DurablyHandedOff
            } else {
                tobkiri_flow::StreamCloseDelivery::Acknowledged
            },
        )
    }
    fn close_control(&self) -> Option<Arc<dyn StreamCloseControl>> {
        self.native_cleanup
            .as_ref()
            .map(|native| native.state.clone() as Arc<dyn StreamCloseControl>)
    }
    fn physical_completion(&self) -> Option<tobkiri_flow::StreamPhysicalCompletion> {
        self.native_cleanup.as_ref().map(|native| {
            tobkiri_flow::StreamPhysicalCompletion::from_task(native.completion.clone())
        })
    }
    async fn close(&mut self) -> Result<(), StepError> {
        self.cancel();
        self.await_cleanup().await
    }
}

impl Drop for TrackedStream {
    fn drop(&mut self) {
        if self.native_cleanup.is_some() && !self.closed {
            self.cancel();
        }
    }
}

#[cfg(test)]
#[path = "tracked_stream_tests.rs"]
mod tests;

#[cfg(test)]
mod native_close_handoff_tests {
    use super::*;
    use std::{
        sync::{Barrier, Weak},
        time::{Duration, Instant},
    };
    use tobkiri_canon::ids::{ProfileId, RequestId};
    use tobkiri_gate::{CancellationToken, CoreControl, Principal, ResourceLimits};

    struct NoBody;
    #[async_trait]
    impl RunTaskCompletion for NoBody {
        async fn wait(&self) -> Result<(), StepError> {
            panic!("close-intent receipt is not physical completion")
        }
    }

    fn state() -> Arc<NativeCleanupState> {
        let control = CoreControl::default();
        let cx = CallCx::try_root_with_control(
            ProfileId::parse("prof_close_handoff").unwrap(),
            Principal::User,
            RequestId::parse("req_close_handoff").unwrap(),
            CancellationToken::new_with_control(&control).unwrap(),
            Instant::now() + Duration::from_secs(60),
            ResourceLimits {
                fuel_per_call: 100_000,
                memory_mib: 8,
                wall_ms: 60_000,
                table_elements: 1024,
                output_bytes: 1024,
            },
            control.clone(),
        )
        .unwrap();
        Arc::new(NativeCleanupState {
            source: tokio::sync::Mutex::new(None),
            body: Arc::new(NoBody),
            terminal: tokio::sync::Mutex::new(None),
            factory: Digest::hash(b"close-handoff"),
            intent: Mutex::new(NativeCleanupIntent {
                handoff: SourceHandoff::Pending,
                outcome: None,
                early_close: None,
                source_control: None,
                abandon_failed: false,
            }),
            receipt: Mutex::new(None),
            cleanup_proved: AtomicBool::new(false),
            quarantined_unknown: AtomicBool::new(false),
            ready: tokio::sync::Notify::new(),
            cx,
            before_close_receipt: Mutex::new(None),
            _reservation: control
                .reserve(
                    ControlKind::Queue,
                    4096 + size_of::<NativeCleanupState>() as u64,
                )
                .unwrap(),
        })
    }

    struct Recorder {
        state: Weak<NativeCleanupState>,
        reasons: Mutex<Vec<StreamCloseReason>>,
        check_unlocked: bool,
        panic: bool,
    }
    impl StreamCloseControl for Recorder {
        fn record_close(&self, reason: StreamCloseReason) -> Result<(), StepError> {
            let Some(state) = self.state.upgrade() else {
                // CoreStream Drop asks for Unknown once more after its owning
                // state begins destruction; that is outside the handoff probe.
                return Ok(());
            };
            // Actual source callbacks must not run under either custody lock.
            if self.check_unlocked {
                assert!(state.intent.try_lock().is_ok());
                assert!(state.source.try_lock().is_ok());
            }
            self.reasons.lock().unwrap().push(reason);
            assert!(!self.panic, "deterministic source callback panic");
            Ok(())
        }
    }

    struct Source {
        control: Arc<Recorder>,
        attaching: Mutex<Option<(Arc<Barrier>, Arc<Barrier>)>>,
    }
    #[async_trait]
    impl StreamSource for Source {
        async fn pull(&mut self, _: u32) -> Result<CoreValue, StepError> {
            panic!("close-intent receipt cannot execute the source")
        }
        fn close_control(&self) -> Option<Arc<dyn StreamCloseControl>> {
            let attaching = self.attaching.lock().unwrap().take();
            if let Some((entered, resume)) = attaching {
                entered.wait();
                resume.wait();
            }
            Some(self.control.clone())
        }
        fn cancel(&mut self) {}
    }

    fn recorder(
        state: &Arc<NativeCleanupState>,
        check_unlocked: bool,
        panic: bool,
    ) -> Arc<Recorder> {
        Arc::new(Recorder {
            state: Arc::downgrade(state),
            reasons: Mutex::new(Vec::new()),
            check_unlocked,
            panic,
        })
    }

    #[test]
    fn close_during_source_attachment_delivers_first_reason_outside_custody_locks() {
        let state = state();
        let control = recorder(&state, true, false);
        let entered = Arc::new(Barrier::new(2));
        let resume = Arc::new(Barrier::new(2));
        let attaching = {
            let state = state.clone();
            let control = control.clone();
            let entered = entered.clone();
            let resume = resume.clone();
            std::thread::spawn(move || {
                state.attach(CoreStream::new(Source {
                    control,
                    attaching: Mutex::new(Some((entered, resume))),
                }));
            })
        };
        entered.wait();
        state.record_close(StreamCloseReason::Deadline).unwrap();
        state.record_close(StreamCloseReason::Shutdown).unwrap();
        assert!(control.reasons.lock().unwrap().is_empty());
        resume.wait();
        attaching.join().unwrap();
        assert_eq!(
            *control.reasons.lock().unwrap(),
            [StreamCloseReason::Deadline]
        );
        assert!(state.cx.cancel().is_cancelled());
        assert!(!state.cleanup_proved.load(Ordering::Acquire));
        assert!(state.receipt().is_none());
    }

    #[test]
    fn source_snapshot_and_close_receipt_are_one_atomic_attachment_boundary() {
        let state = state();
        let control = recorder(&state, false, false);
        let snapshotted = Arc::new(Barrier::new(2));
        let accept = Arc::new(Barrier::new(2));
        *state.before_close_receipt.lock().unwrap() = Some(Box::new({
            let snapshotted = snapshotted.clone();
            let accept = accept.clone();
            move || {
                snapshotted.wait();
                accept.wait();
            }
        }));
        let closing = {
            let state = state.clone();
            std::thread::spawn(move || state.record_close(StreamCloseReason::Retired).unwrap())
        };
        snapshotted.wait();
        // At the exact point after observing no published source but before
        // latching the cause, attachment must still be excluded. The old
        // split-lock implementation allowed publication in this window.
        let publication_excluded = state.intent.try_lock().is_err();
        let attaching = {
            let state = state.clone();
            let control = control.clone();
            std::thread::spawn(move || {
                state.attach(CoreStream::new(Source {
                    control,
                    attaching: Mutex::new(None),
                }));
            })
        };
        accept.wait();
        closing.join().unwrap();
        attaching.join().unwrap();
        assert!(publication_excluded);
        assert_eq!(
            *control.reasons.lock().unwrap(),
            [StreamCloseReason::Retired]
        );
        assert!(state.receipt().is_none());
        assert!(!state.cleanup_proved.load(Ordering::Acquire));
    }

    #[test]
    fn concurrent_source_publication_and_close_never_lose_or_duplicate_the_cause() {
        for reason in [
            StreamCloseReason::Manual,
            StreamCloseReason::Deadline,
            StreamCloseReason::Shutdown,
            StreamCloseReason::Retired,
            StreamCloseReason::Unknown,
        ] {
            for _ in 0..32 {
                let state = state();
                // The other thread may legitimately own a lock while the
                // callback runs; lock-release assertions belong to the
                // separately barrier-ordered tests, not to this race.
                let control = recorder(&state, false, false);
                let start = Arc::new(Barrier::new(3));
                let attaching = {
                    let state = state.clone();
                    let control = control.clone();
                    let start = start.clone();
                    std::thread::spawn(move || {
                        start.wait();
                        state.attach(CoreStream::new(Source {
                            control,
                            attaching: Mutex::new(None),
                        }));
                    })
                };
                let closing = {
                    let state = state.clone();
                    let start = start.clone();
                    std::thread::spawn(move || {
                        start.wait();
                        state.record_close(reason).unwrap();
                    })
                };
                start.wait();
                attaching.join().unwrap();
                closing.join().unwrap();
                assert_eq!(*control.reasons.lock().unwrap(), [reason]);
                assert_eq!(state.intent.lock().unwrap().early_close, Some(reason));
                assert!(!state.cleanup_proved.load(Ordering::Acquire));
            }
        }
    }

    #[test]
    fn attached_source_callback_panic_keeps_close_receipt_without_physical_proof() {
        let state = state();
        let control = recorder(&state, true, true);
        state.attach(CoreStream::new(Source {
            control: control.clone(),
            attaching: Mutex::new(None),
        }));
        let error = state.record_close(StreamCloseReason::Retired).unwrap_err();
        assert_eq!(error.code, "trap");
        assert_eq!(
            *control.reasons.lock().unwrap(),
            [StreamCloseReason::Retired]
        );
        assert_eq!(
            state.intent.lock().unwrap().early_close,
            Some(StreamCloseReason::Retired)
        );
        assert!(state.cx.cancel().is_cancelled());
        assert!(state.source.try_lock().unwrap().is_some());
        assert!(state.receipt().is_none());
        assert!(!state.cleanup_proved.load(Ordering::Acquire));
    }

    #[test]
    fn early_close_callback_panic_retains_attached_source_and_first_cause() {
        let state = state();
        let control = recorder(&state, true, true);
        state.record_close(StreamCloseReason::Shutdown).unwrap();
        let attached = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            state.attach(CoreStream::new(Source {
                control: control.clone(),
                attaching: Mutex::new(None),
            }));
        }));
        assert!(attached.is_err());
        assert_eq!(
            *control.reasons.lock().unwrap(),
            [StreamCloseReason::Shutdown]
        );
        let intent = state.intent.lock().unwrap();
        assert!(intent.handoff == SourceHandoff::Attached);
        assert_eq!(intent.early_close, Some(StreamCloseReason::Shutdown));
        assert!(intent.source_control.is_some());
        drop(intent);
        assert!(state.source.try_lock().unwrap().is_some());
        assert!(state.receipt().is_none());
        assert!(!state.cleanup_proved.load(Ordering::Acquire));
    }
}

#[cfg(test)]
mod cleanup_receipt_classification_tests {
    use super::*;

    #[test]
    fn physical_unknown_dominates_without_discarding_business_or_audit_failure() {
        let mut receipt = CleanupReceipt {
            outcome: Outcome::Failed {
                code: "unavailable".into(),
            },
            physical_error: Some(StepError::new("component", "original physical unknown")),
            business_error: Some(StepError::new("unavailable", "original business error")),
            delivery_error: Some(StepError::new("persistence", "original audit error")),
        };
        assert_eq!(receipt.result().unwrap_err().code, "component");
        assert!(receipt.business_error.is_some());
        assert!(receipt.delivery_error.is_some());
        receipt.physical_error = None;
        assert_eq!(receipt.result().unwrap_err().code, "persistence");
        receipt.delivery_error = None;
        assert_eq!(receipt.result().unwrap_err().code, "unavailable");
    }
}

#[cfg(test)]
mod notification_slot_tests {
    use super::*;
    #[test]
    fn exact_retired_removal_preserves_other_anchor_with_same_factory() {
        let control = tobkiri_gate::CoreControl::default();
        let baseline = control.usage().bytes;
        let identity = Digest::hash(b"same-factory-distinct-anchor");
        let first = crate::ui_operation_delivery::UiDeliveryOwner::unpublished_for_test(
            &identity, &control,
        )
        .unwrap();
        let second = crate::ui_operation_delivery::UiDeliveryOwner::unpublished_for_test(
            &identity, &control,
        )
        .unwrap();
        let a = first.weak().upgrade().unwrap();
        let b = second.weak().upgrade().unwrap();
        let registry = Mutex::new([const { None }; NATIVE_CLEANUP_SLOTS]);
        assert!(matches!(
            register_ui_notifications_in(&registry, &a),
            UiNotificationRegistrationAttempt::Registered
        ));
        assert!(matches!(
            register_ui_notifications_in(&registry, &b),
            UiNotificationRegistrationAttempt::Registered
        ));
        assert!(!remove_retired_ui_notifications_in(&registry, &a));
        drop(first);
        assert!(remove_retired_ui_notifications_in(&registry, &a));
        let slots = registry.lock().unwrap();
        assert_eq!(slots.iter().filter(|slot| slot.is_some()).count(), 1);
        assert!(slots.iter().any(|slot| matches!(slot, Some(NativeCleanupEntry::Notifications(current)) if FundedArc::ptr_eq(current, &b))));
        drop(slots);
        drop((a, second, b, registry));
        assert_eq!(control.usage().bytes, baseline);
    }

    #[test]
    fn contended_failed_start_retirement_is_funded_and_reuses_one_slot() {
        let control = tobkiri_gate::CoreControl::default();
        let baseline = control.usage().bytes;
        let registry = Mutex::new([const { None }; NATIVE_CLEANUP_SLOTS]);
        let mut retained_bytes = None;
        // More failed constructions than registry capacity, with no observer.
        for _ in 0..(NATIVE_CLEANUP_SLOTS + 16) {
            let owner = crate::ui_operation_delivery::UiDeliveryOwner::unpublished_for_test(
                &Digest::hash(b"failed-start"),
                &control,
            )
            .unwrap();
            let anchor = owner.weak().upgrade().unwrap();
            assert!(matches!(
                register_ui_notifications_in(&registry, &anchor),
                UiNotificationRegistrationAttempt::Registered
            ));
            let held = registry.lock().unwrap();
            drop(owner);
            assert!(anchor.removable());
            assert!(!remove_retired_ui_notifications_in(&registry, &anchor));
            assert_eq!(held.iter().filter(|slot| slot.is_some()).count(), 1);
            drop(held);
            drop(anchor);
            let bytes = control.usage().bytes;
            assert!(bytes > baseline);
            assert_eq!(*retained_bytes.get_or_insert(bytes), bytes);
        }
        // A normal exact observation refunds the final bounded retained record.
        let anchor = {
            let slots = registry.lock().unwrap();
            match slots.iter().flatten().next().unwrap() {
                NativeCleanupEntry::Notifications(anchor) => anchor.clone(),
                _ => panic!("notification only"),
            }
        };
        assert!(remove_retired_ui_notifications_in(&registry, &anchor));
        drop(anchor);
        assert_eq!(control.usage().bytes, baseline);
    }

    #[test]
    fn own_registry_contention_is_retryable_before_publication() {
        let control = tobkiri_gate::CoreControl::default();
        let owner = crate::ui_operation_delivery::UiDeliveryOwner::unpublished_for_test(
            &Digest::hash(b"retry-owner"),
            &control,
        )
        .unwrap();
        let anchor = owner.weak().upgrade().unwrap();
        let registry = Mutex::new([const { None }; NATIVE_CLEANUP_SLOTS]);
        let before = anchor.strong_count();
        let held = registry.lock().unwrap();
        std::thread::scope(|scope| {
            scope
                .spawn(|| {
                    assert!(matches!(
                        register_ui_notifications_in(&registry, &anchor),
                        UiNotificationRegistrationAttempt::BusyBeforePublication
                    ));
                })
                .join()
                .unwrap();
        });
        assert!(held.iter().all(Option::is_none));
        assert_eq!(anchor.strong_count(), before);
        drop(held);
        assert!(matches!(
            register_ui_notifications_in(&registry, &anchor),
            UiNotificationRegistrationAttempt::Registered
        ));
        assert_eq!(anchor.strong_count(), before + 1);
        drop(registry);
        assert_eq!(anchor.strong_count(), before);
        owner.close();
    }

    #[test]
    fn concurrent_registrations_publish_once_each_without_slot_loss() {
        const WRITERS: usize = 8;
        let control = tobkiri_gate::CoreControl::default();
        let owner = crate::ui_operation_delivery::UiDeliveryOwner::unpublished_for_test(
            &Digest::hash(b"concurrent-owner"),
            &control,
        )
        .unwrap();
        let anchor = owner.weak().upgrade().unwrap();
        let registry = Mutex::new([const { None }; NATIVE_CLEANUP_SLOTS]);
        let resume = std::sync::Barrier::new(WRITERS + 1);
        let (send, receive) = std::sync::mpsc::channel();
        let before = anchor.strong_count();
        std::thread::scope(|scope| {
            let held = registry.lock().unwrap();
            let mut workers = Vec::new();
            for _ in 0..WRITERS {
                let send = send.clone();
                let registry = &registry;
                let anchor = &anchor;
                let resume = &resume;
                workers.push(scope.spawn(move || {
                    assert!(matches!(
                        register_ui_notifications_in(registry, anchor),
                        UiNotificationRegistrationAttempt::BusyBeforePublication
                    ));
                    send.send(()).unwrap();
                    resume.wait();
                    for attempt in 0..8 {
                        match register_ui_notifications_in(registry, anchor) {
                            UiNotificationRegistrationAttempt::Registered => return,
                            UiNotificationRegistrationAttempt::BusyBeforePublication
                                if attempt < 7 =>
                            {
                                std::thread::sleep(std::time::Duration::from_millis(
                                    1 << attempt.min(4),
                                ));
                            }
                            _ => panic!("isolated bounded publication failed"),
                        }
                    }
                }));
            }
            for _ in 0..WRITERS {
                receive.recv().unwrap();
            }
            assert!(held.iter().all(Option::is_none));
            drop(held);
            resume.wait();
            for worker in workers {
                worker.join().unwrap();
            }
        });
        assert_eq!(
            registry
                .lock()
                .unwrap()
                .iter()
                .filter(|slot| slot.is_some())
                .count(),
            WRITERS
        );
        assert_eq!(anchor.strong_count(), before + WRITERS);
        drop(registry);
        assert_eq!(anchor.strong_count(), before);
        owner.close();
    }

    #[test]
    fn retired_metadata_is_reused_before_consuming_another_vacant_slot() {
        let control = tobkiri_gate::CoreControl::default();
        let retired = crate::ui_operation_delivery::UiDeliveryOwner::unpublished_for_test(
            &Digest::hash(b"retired-partial-owner"),
            &control,
        )
        .unwrap();
        let old = retired.weak().upgrade().unwrap();
        let incoming = crate::ui_operation_delivery::UiDeliveryOwner::unpublished_for_test(
            &Digest::hash(b"new-partial-owner"),
            &control,
        )
        .unwrap();
        let new = incoming.weak().upgrade().unwrap();
        let mut slots = [const { None }; NATIVE_CLEANUP_SLOTS];
        slots[17] = Some(NativeCleanupEntry::Notifications(old.clone()));
        let registry = Mutex::new(slots);
        retired.close();
        assert!(matches!(
            register_ui_notifications_in(&registry, &new),
            UiNotificationRegistrationAttempt::Registered
        ));
        let slots = registry.lock().unwrap();
        assert!(slots[0].is_none());
        assert!(
            matches!(slots[17].as_ref(), Some(NativeCleanupEntry::Notifications(value))
            if FundedArc::ptr_eq(value, &new))
        );
        drop(slots);
        incoming.close();
    }

    #[test]
    fn poisoned_registry_is_not_retryable_and_accepts_nothing() {
        let control = tobkiri_gate::CoreControl::default();
        let owner = crate::ui_operation_delivery::UiDeliveryOwner::unpublished_for_test(
            &Digest::hash(b"poison-owner"),
            &control,
        )
        .unwrap();
        let anchor = owner.weak().upgrade().unwrap();
        let registry = Mutex::new([const { None }; NATIVE_CLEANUP_SLOTS]);
        let before = anchor.strong_count();
        let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let _held = registry.lock().unwrap();
            panic!("poison only the isolated test registry");
        }));
        assert!(matches!(
            register_ui_notifications_in(&registry, &anchor),
            UiNotificationRegistrationAttempt::Rejected(tobkiri_gate::GateError::Unavailable)
        ));
        assert_eq!(anchor.strong_count(), before);
        let poisoned = match registry.lock() {
            Err(error) => error,
            Ok(_) => panic!("registry must remain poisoned"),
        };
        assert!(poisoned.get_ref().iter().all(Option::is_none));
        drop(poisoned);
        owner.close();
    }

    #[test]
    fn only_proven_retired_empty_metadata_can_be_replaced_when_full() {
        let control = tobkiri_gate::CoreControl::default();
        let retired = crate::ui_operation_delivery::UiDeliveryOwner::unpublished_for_test(
            &Digest::hash(b"retired-owner"),
            &control,
        )
        .unwrap();
        let retired_anchor = retired.weak().upgrade().unwrap();
        let incoming = crate::ui_operation_delivery::UiDeliveryOwner::unpublished_for_test(
            &Digest::hash(b"incoming-owner"),
            &control,
        )
        .unwrap();
        let incoming_anchor = incoming.weak().upgrade().unwrap();
        let registry = Mutex::new(std::array::from_fn(|_| {
            Some(NativeCleanupEntry::Notifications(retired_anchor.clone()))
        }));
        assert!(matches!(
            register_ui_notifications_in(&registry, &incoming_anchor),
            UiNotificationRegistrationAttempt::Rejected(tobkiri_gate::GateError::Limit(_))
        ));
        retired.close();
        assert!(retired_anchor.removable());
        let old_count = retired_anchor.strong_count();
        let new_count = incoming_anchor.strong_count();
        assert!(matches!(
            register_ui_notifications_in(&registry, &incoming_anchor),
            UiNotificationRegistrationAttempt::Registered
        ));
        assert_eq!(retired_anchor.strong_count(), old_count - 1);
        assert_eq!(incoming_anchor.strong_count(), new_count + 1);
        assert!(registry.try_lock().is_ok());
        incoming.close();
    }

    #[test]
    fn full_existing_256_slots_reject_before_notification_receipt_without_eviction() {
        let control = tobkiri_gate::CoreControl::default();
        let owner = crate::ui_operation_delivery::UiDeliveryOwner::unpublished_for_test(
            &Digest::hash(b"full-owner"),
            &control,
        )
        .unwrap();
        let anchor = owner.weak().upgrade().unwrap();
        let mut local =
            std::array::from_fn(|_| Some(NativeCleanupEntry::Notifications(anchor.clone())));
        let before = anchor.strong_count();
        assert!(matches!(
            insert_ui_notifications(&mut local, &anchor),
            Err(tobkiri_gate::GateError::Limit(_))
        ));
        assert_eq!(anchor.strong_count(), before);
        assert!(local.iter().all(|entry| matches!(entry,
            Some(NativeCleanupEntry::Notifications(current)) if FundedArc::ptr_eq(current, &anchor))));
        assert!(vacant_cleanup_slot(&local).is_none());
        owner.close();
    }
}
