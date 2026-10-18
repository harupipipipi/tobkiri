//! Native User calls use the exact prepared User entry and producer encoders.
//! Streams have a separate bounded Session namespace; no UI claim is created.
use super::*;
use std::sync::atomic::AtomicBool;
use tokio::sync::Notify;

const MAX_STREAMS: usize = 64;
const MAX_BATCH: u32 = 32;
const ENTRY_BYTES: u64 = 4096;
const TASK_BYTES: u64 = 8192;

/// Opaque native stream identity. Only its originating Session can use it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct UserStreamHandle {
    session: Digest,
    id: u64,
}

/// The provider-funded result of a trusted native User action.
pub enum UserPortOutput {
    /// Closed StrictV1 `{kind:"value",value:...}` envelope.
    Value(CoreBuffer),
    /// Closed stream envelope and its actual Session-owned stream identity.
    Stream {
        envelope: CoreBuffer,
        handle: UserStreamHandle,
    },
}

pub(super) struct Registry {
    identity: Digest,
    next: AtomicU64,
    entries: Mutex<BTreeMap<u64, Arc<Entry>>>,
}
impl Default for Registry {
    fn default() -> Self {
        Self {
            identity: Digest::hash(ulid::Ulid::new().to_string().as_bytes()),
            next: AtomicU64::new(1),
            entries: Mutex::new(BTreeMap::new()),
        }
    }
}
struct Entry {
    id: u64,
    cx: CallCx,
    external: TokioCancellationToken,
    state: tokio::sync::Mutex<State>,
    // The dispatch worker publishes here synchronously before completing. A
    // stopped supervisor must never drop an unpublished actual stream result.
    completed: Mutex<Option<Result<FinalPortOutput, StepError>>>,
    ready: Notify,
    retained: AtomicBool,
    supervisor_failed: AtomicBool,
    _control: ControlReservation,
}
enum State {
    Preparing,
    Live(Source),
    Finished,
    Failed(StepError),
}
struct Source {
    stream: CoreStream,
    output_bytes: u64,
    cleanup_error: Option<StepError>,
}
impl Entry {
    fn retain(self: &Arc<Self>) {
        // Unknown cleanup retains the original preadmitted owner, not a new
        // unbudgeted recovery allocation. At most MAX_STREAMS entries live.
        if !self.retained.swap(true, Ordering::AcqRel) {
            std::mem::forget(self.clone());
        }
    }
}
impl Registry {
    fn reserve(
        &self,
        cx: CallCx,
        external: TokioCancellationToken,
    ) -> Result<Arc<Entry>, CoreError> {
        let mut entries = self
            .entries
            .lock()
            .map_err(|_| unavailable("User stream registry poisoned"))?;
        if entries.len() >= MAX_STREAMS {
            return Err(unavailable("User stream capacity exceeded"));
        }
        let control = cx
            .control()
            .reserve(ControlKind::Registration, ENTRY_BYTES)?;
        let id = self
            .next
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |id| {
                (id < tobkiri_canon::MAX_SAFE_INTEGER).then_some(id + 1)
            })
            .map_err(|_| unavailable("User stream handle space exhausted"))?;
        let entry = Arc::new(Entry {
            id,
            cx,
            external,
            state: tokio::sync::Mutex::new(State::Preparing),
            completed: Mutex::new(None),
            ready: Notify::new(),
            retained: AtomicBool::new(false),
            supervisor_failed: AtomicBool::new(false),
            _control: control,
        });
        entries.insert(id, entry.clone());
        Ok(entry)
    }
    fn entry(&self, handle: &UserStreamHandle) -> Result<Arc<Entry>, CoreError> {
        self.lookup(handle)?
            .ok_or_else(|| CoreError::Invalid("User stream is closed or unknown".into()))
    }
    fn lookup(&self, handle: &UserStreamHandle) -> Result<Option<Arc<Entry>>, CoreError> {
        if handle.session != self.identity {
            return Err(CoreError::Invalid(
                "User stream belongs to another Session".into(),
            ));
        }
        Ok(self
            .entries
            .lock()
            .map_err(|_| unavailable("User stream registry poisoned"))?
            .get(&handle.id)
            .cloned())
    }
    fn remove(&self, id: u64) {
        if let Ok(mut entries) = self.entries.lock() {
            entries.remove(&id);
        }
    }
    fn snapshot(&self) -> Result<Vec<Arc<Entry>>, CoreError> {
        Ok(self
            .entries
            .lock()
            .map_err(|_| unavailable("User stream registry poisoned"))?
            .values()
            .cloned()
            .collect())
    }
}

fn unavailable(message: &str) -> CoreError {
    CoreError::Step(StepError::new("unavailable", message))
}
fn join_error() -> CoreError {
    unavailable("owning runtime did not complete the User operation")
}
fn interruption(cx: &CallCx, message: &str) -> StepError {
    if cx.filesystem_mutation_possible() {
        StepError::new(
            "filesystem-commit-unknown",
            "Filesystem mutation may already have applied; inspect retained effects before retrying the interrupted User call",
        )
    } else if Instant::now() >= cx.deadline() {
        StepError::new("deadline", message)
    } else {
        StepError::new("cancelled", message)
    }
}
fn with_cleanup(original: StepError, cleanup: StepError) -> StepError {
    let (mut primary, secondary) = if cleanup.code == "filesystem-commit-unknown"
        && original.code != "filesystem-commit-unknown"
    {
        (cleanup, original)
    } else {
        (original, cleanup)
    };
    primary.message.push_str(&format!(
        "; cleanup also reported {}",
        secondary.audit_code()
    ));
    primary
}
fn preserve_effect_error(cx: &CallCx, error: StepError) -> StepError {
    if cx.filesystem_mutation_possible() && error.code != "filesystem-commit-unknown" {
        with_cleanup(interruption(cx, "User operation stopped"), error)
    } else {
        error
    }
}
fn stopped(entry: &Entry) -> CoreError {
    if entry.cx.filesystem_mutation_possible() {
        CoreError::Step(interruption(&entry.cx, "User supervisor stopped"))
    } else {
        join_error()
    }
}
fn supervisor_failure(entry: &Entry) -> StepError {
    preserve_effect_error(
        &entry.cx,
        StepError::new(
            "unavailable",
            "User supervisor stopped; its retained source cannot be replayed",
        ),
    )
}
pub(super) fn is_filesystem_unknown(error: &CoreError) -> bool {
    match error {
        CoreError::Step(error) => error.code == "filesystem-commit-unknown",
        CoreError::Cleanup { original, cleanup } => {
            is_filesystem_unknown(original) || is_filesystem_unknown(cleanup)
        }
        _ => false,
    }
}
pub(super) fn record_failure(failure: &mut Option<CoreError>, error: CoreError) {
    match failure.take() {
        None => *failure = Some(error),
        Some(original) => {
            let (original, cleanup) =
                if is_filesystem_unknown(&error) && !is_filesystem_unknown(&original) {
                    (error, original)
                } else {
                    (original, error)
                };
            *failure = Some(CoreError::Cleanup {
                original: Box::new(original),
                cleanup: Box::new(cleanup),
            });
        }
    }
}
fn reserve_task(cx: &CallCx) -> Result<ControlReservation, CoreError> {
    Ok(cx.control().reserve(ControlKind::Task, TASK_BYTES)?)
}
fn store_completed(entry: &Entry, result: Result<FinalPortOutput, StepError>) {
    // No await or fallible conversion between receiving the real result and
    // transferring it into its preadmitted owner. Poisoning cannot discard it.
    *entry
        .completed
        .lock()
        .unwrap_or_else(|error| error.into_inner()) = Some(result);
}
async fn publish_completed(
    registry: Arc<Registry>,
    entry: Arc<Entry>,
) -> Result<UserPortOutput, StepError> {
    // Acquire the potentially contended async lock BEFORE taking custody from
    // the synchronous cell. Publication itself contains no suspension point.
    let mut state = entry.state.lock().await;
    if entry.supervisor_failed.load(Ordering::Acquire) {
        entry.retain();
        return Err(supervisor_failure(&entry));
    }
    let result = entry
        .completed
        .lock()
        .unwrap_or_else(|error| error.into_inner())
        .take()
        .unwrap_or_else(|| {
            Err(StepError::new(
                "unavailable",
                "User dispatch completed without a result",
            ))
        });
    let result = result.map_err(|error| preserve_effect_error(&entry.cx, error));
    let output = match result {
        Ok(FinalPortOutput::Value(value)) => {
            *state = State::Finished;
            registry.remove(entry.id);
            Ok(UserPortOutput::Value(value))
        }
        Ok(FinalPortOutput::Stream { envelope, stream }) => {
            *state = State::Live(Source {
                stream,
                output_bytes: 0,
                cleanup_error: None,
            });
            Ok(UserPortOutput::Stream {
                envelope,
                handle: UserStreamHandle {
                    session: registry.identity.clone(),
                    id: entry.id,
                },
            })
        }
        Err(error) => {
            if error.code == "filesystem-commit-unknown" {
                *state = State::Failed(error.clone());
                entry.retain();
            } else {
                *state = State::Finished;
                registry.remove(entry.id);
            }
            Err(error)
        }
    };
    entry.ready.notify_waiters();
    output
}

/// Cancels on abandonment; actual dispatch and close run in owning tasks.
struct RequestGuard {
    cx: CallCx,
    cleanup: Option<(Arc<Registry>, Arc<Entry>, tokio::runtime::Handle)>,
    armed: bool,
}
/// Also protects tasks dropped by a stopped runtime before their first poll.
/// A cancellation request or discarded JoinHandle is never cleanup evidence.
struct RetainOnAbandon {
    entry: Arc<Entry>,
    finished: bool,
}
impl Drop for RetainOnAbandon {
    fn drop(&mut self) {
        if self.finished {
            return;
        }
        self.entry.supervisor_failed.store(true, Ordering::Release);
        self.entry.cx.cancel().cancel();
        if let Ok(mut state) = self.entry.state.try_lock() {
            let error = supervisor_failure(&self.entry);
            match &mut *state {
                State::Preparing => *state = State::Failed(error),
                State::Live(source) => {
                    source.cleanup_error.get_or_insert(error);
                }
                _ => (),
            }
        }
        // A close waiter may still hold State::Preparing when its supervisor
        // disappears. The atomic failure is authoritative even if try_lock
        // fails; notify that waiter before it parks without a future publisher.
        self.entry.ready.notify_waiters();
        self.entry.retain();
    }
}
impl Drop for RequestGuard {
    fn drop(&mut self) {
        if !self.armed {
            return;
        }
        self.cx.cancel().cancel();
        if let Some((registry, entry, runtime)) = self.cleanup.take() {
            if schedule_close(registry, entry.clone(), &runtime).is_err() {
                entry.retain();
            }
        }
    }
}

struct PreparedCall {
    session: Session,
    cx: CallCx,
    prepared: crate::execution_frames::PreparedUserInvocation,
    shape: ShapeRef,
    operation: OpName,
    args: CoreValue,
    digest: Digest,
}
impl PreparedCall {
    async fn execute(
        self,
        output: FinalOutputKind,
        cancel: TokioCancellationToken,
    ) -> Result<FinalPortOutput, StepError> {
        let invocation = FlowUserInvocation {
            invocation: self.prepared.invocation(),
            view: &self.shape,
            op: &self.operation,
            target: self.prepared.target(),
        };
        let preparation = self.prepared.preparation();
        let dispatch = self.session.invoker.call_user_final(
            &self.cx,
            &invocation,
            &preparation,
            &self.digest,
            self.args,
            output,
        );
        tokio::pin!(dispatch);
        let (mut external, mut context, mut deadline) = (true, true, true);
        loop {
            tokio::select! {
                _ = cancel.cancelled(), if external => { external = false; self.cx.cancel().cancel(); }
                _ = self.cx.cancel().cancelled(), if context => { context = false; }
                _ = tokio::time::sleep_until(self.cx.deadline().into()), if deadline => { deadline = false; self.cx.cancel().cancel(); }
                result = &mut dispatch => return result,
            }
        }
    }
}
impl Session {
    async fn prepare_user_call(
        &self,
        shape: ShapeRef,
        operation: OpName,
        args: &CanonValue,
        #[cfg(feature = "closed-request-metrics")] observation: Option<
            &tobkiri_gate::closed_metrics::Observation,
        >,
    ) -> Result<PreparedCall, CoreError> {
        self.ensure_started().await?;
        self.check_snapshot()?;
        let document = self.user_document.as_ref().ok_or_else(|| {
            CoreError::Invalid("User document approval is unavailable for this Session".into())
        })?;
        let deadline = deadline_after(self.compiled.profile.ceiling.wasm.wall_ms)?;
        let budget = BudgetContext::new(self.compiled.profile.ceiling.loops.clone(), deadline)
            .map_err(|error| CoreError::Invalid(error.to_string()))?;
        let cx = CallCx::try_root_with_control(
            self.compiled.profile.id.clone(),
            Principal::User,
            RequestId::new(format!("req_{}", ulid::Ulid::new()))?,
            self.shutdown.child_with_control(self.gate.control())?,
            deadline,
            self.compiled.profile.ceiling.wasm.clone(),
            self.gate.control().clone(),
        )?
        .with_security_epoch(self.epoch)
        .with_revision(self.compiled.plan_digest.clone())
        .with_budget(budget)?;
        #[cfg(feature = "closed-request-metrics")]
        let cx = match observation {
            Some(observation) => cx.with_observation(observation)?,
            None => cx,
        };
        let factory = self.execution_factory.as_ref().ok_or_else(|| {
            unavailable("prepared User invocation is unavailable for this Session")
        })?;
        let prepared = factory
            .prepare_user_invocation(&cx, &shape, &operation, document)
            .await?;
        let scope = prepared.preparation().argument_scope()?;
        let budget = self.invoker.allocation_budget_for_frame(&cx, scope)?;
        let args = CoreValue::try_from_borrowed(args, &budget).map_err(|_| {
            CoreError::Step(StepError::new(
                "limit-exceeded",
                "User arguments exceed their prepared producer scope",
            ))
        })?;
        let digest = args.digest_guest(&budget).map_err(|_| {
            CoreError::Step(StepError::new(
                "limit-exceeded",
                "User argument verification exceeds its prepared scope",
            ))
        })?;
        Ok(PreparedCall {
            session: self.clone(),
            cx,
            prepared,
            shape,
            operation,
            args,
            digest,
        })
    }
    pub(super) async fn call_user_value(
        &self,
        shape: ShapeRef,
        operation: OpName,
        args: &CanonValue,
    ) -> Result<CoreBuffer, CoreError> {
        let prepared = self
            .prepare_user_call(
                shape,
                operation,
                args,
                #[cfg(feature = "closed-request-metrics")]
                None,
            )
            .await?;
        let mut guard = RequestGuard {
            cx: prepared.cx.clone(),
            cleanup: None,
            armed: true,
        };
        let control = reserve_task(&prepared.cx)?;
        let task = self.runtime.spawn(async move {
            let _control = control;
            prepared
                .execute(
                    FinalOutputKind::CanonicalValue,
                    TokioCancellationToken::new(),
                )
                .await
        });
        let output = task.await.map_err(|_| {
            if guard.cx.filesystem_mutation_possible() {
                CoreError::Step(interruption(&guard.cx, "User dispatch stopped"))
            } else {
                join_error()
            }
        })??;
        guard.armed = false;
        match output {
            FinalPortOutput::Value(value) => Ok(value),
            FinalPortOutput::Stream { mut stream, .. } => {
                stream.close().await?;
                Err(CoreError::Invalid(
                    "value-only port invocation returned a stream".into(),
                ))
            }
        }
    }

    /// Invoke a typed port using the approved User document. Values and streams
    /// use provider-funded encoders; no UI attachment or broader policy exists.
    pub async fn invoke_user_port(
        &self,
        shape: ShapeRef,
        operation: OpName,
        args: &CanonValue,
        cancel: TokioCancellationToken,
    ) -> Result<UserPortOutput, CoreError> {
        self.invoke_user_port_inner(
            shape,
            operation,
            args,
            cancel,
            #[cfg(feature = "closed-request-metrics")]
            None,
        )
        .await
    }

    /// Fixed prepaid diagnostics, separate from User/Pack execution authority.
    #[cfg(feature = "closed-request-metrics")]
    pub fn new_request_observation(
        &self,
    ) -> Result<tobkiri_gate::closed_metrics::Observation, CoreError> {
        tobkiri_gate::closed_metrics::Observation::try_new(self.gate.control())
            .map_err(tobkiri_gate::GateError::from)
            .map_err(CoreError::from)
    }

    /// Uses the exact ordinary User dispatch path and unchanged approvals.
    #[cfg(feature = "closed-request-metrics")]
    pub async fn invoke_user_port_observed(
        &self,
        shape: ShapeRef,
        operation: OpName,
        args: &CanonValue,
        cancel: TokioCancellationToken,
        observation: &tobkiri_gate::closed_metrics::Observation,
    ) -> Result<UserPortOutput, CoreError> {
        self.invoke_user_port_inner(shape, operation, args, cancel, Some(observation))
            .await
    }

    async fn invoke_user_port_inner(
        &self,
        shape: ShapeRef,
        operation: OpName,
        args: &CanonValue,
        cancel: TokioCancellationToken,
        #[cfg(feature = "closed-request-metrics")] observation: Option<
            &tobkiri_gate::closed_metrics::Observation,
        >,
    ) -> Result<UserPortOutput, CoreError> {
        if cancel.is_cancelled() {
            return Err(CoreError::Step(StepError::new(
                "cancelled",
                "User invocation cancelled before preparation",
            )));
        }
        let prepared = self
            .prepare_user_call(
                shape,
                operation,
                args,
                #[cfg(feature = "closed-request-metrics")]
                observation,
            )
            .await?;
        let supervisor_control = reserve_task(&prepared.cx)?;
        let dispatch_control = reserve_task(&prepared.cx)?;
        let entry = self
            .user_streams
            .reserve(prepared.cx.clone(), cancel.clone())?;
        let registry = self.user_streams.clone();
        let mut guard = RequestGuard {
            cx: prepared.cx.clone(),
            cleanup: Some((registry.clone(), entry.clone(), self.runtime.clone())),
            armed: true,
        };
        let target = entry.clone();
        let worker_registry = registry.clone();
        let retention = RetainOnAbandon {
            entry: entry.clone(),
            finished: false,
        };
        let dispatch_retention = RetainOnAbandon {
            entry: entry.clone(),
            finished: false,
        };
        let task = self.runtime.spawn(async move {
            let _control = supervisor_control;
            let mut retention = retention;
            let dispatch_target = target.clone();
            let result = tokio::spawn(async move {
                let _control = dispatch_control;
                let mut retention = dispatch_retention;
                let result = prepared
                    .execute(
                        FinalOutputKind::UserInvokeValue {
                            stream_handle: dispatch_target.id,
                        },
                        cancel,
                    )
                    .await;
                store_completed(&dispatch_target, result);
                retention.finished = true;
            })
            .await;
            match result {
                Ok(()) => (),
                Err(_) if target.cx.filesystem_mutation_possible() => {
                    store_completed(
                        &target,
                        Err(interruption(&target.cx, "User dispatch trapped")),
                    );
                }
                Err(_) => store_completed(
                    &target,
                    Err(StepError::new(
                        "trap",
                        "User dispatch trapped; inspect durable outcome before retrying",
                    )),
                ),
            }
            let output = publish_completed(worker_registry, target.clone()).await;
            retention.finished = true;
            output
        });
        let result = task.await.map_err(|_| {
            if entry.cx.filesystem_mutation_possible() {
                CoreError::Step(interruption(&entry.cx, "User dispatch stopped"))
            } else {
                join_error()
            }
        })?;
        guard.armed = false;
        match result {
            Ok(UserPortOutput::Value(value))
                if entry.cx.check_active().is_err() || entry.external.is_cancelled() =>
            {
                drop(value);
                Err(CoreError::Step(interruption(
                    &entry.cx,
                    "User invocation cancelled before value delivery",
                )))
            }
            Ok(UserPortOutput::Stream { envelope, handle })
                if entry.cx.check_active().is_err() || entry.external.is_cancelled() =>
            {
                self.user_stream_cancel(&handle).await?;
                drop(envelope);
                Err(CoreError::Step(interruption(
                    &entry.cx,
                    "User invocation cancelled before stream delivery",
                )))
            }
            result => Ok(result?),
        }
    }

    /// Pull one StrictV1 array while the real provider scope is live. Dropping
    /// this future cancels and schedules cleanup; it does not abandon a pull.
    pub async fn user_stream_pull_buffer(
        &self,
        handle: &UserStreamHandle,
        maximum: u32,
    ) -> Result<CoreBuffer, CoreError> {
        if maximum == 0 || maximum > MAX_BATCH {
            return Err(CoreError::Invalid(
                "User pull bound must be in 1..=32".into(),
            ));
        }
        let entry = self.user_streams.entry(handle)?;
        if let Err(error) = self
            .ensure_started()
            .await
            .and_then(|()| self.check_snapshot())
            .and_then(|()| entry.cx.check_active().map_err(CoreError::from))
        {
            self.user_stream_cancel(handle).await?;
            return Err(error);
        }
        let mut guard = RequestGuard {
            cx: entry.cx.clone(),
            cleanup: Some((
                self.user_streams.clone(),
                entry.clone(),
                self.runtime.clone(),
            )),
            armed: true,
        };
        let registry = self.user_streams.clone();
        let target = entry.clone();
        let control = reserve_task(&entry.cx)?;
        let retention = RetainOnAbandon {
            entry: entry.clone(),
            finished: false,
        };
        let task = self.runtime.spawn(async move {
            let _control = control;
            let mut retention = retention;
            let result = pull_entry(registry, target, maximum).await;
            retention.finished = true;
            result
        });
        let result = task.await.map_err(|_| {
            if entry.cx.filesystem_mutation_possible() {
                CoreError::Step(interruption(&entry.cx, "User pull stopped"))
            } else {
                join_error()
            }
        })?;
        guard.armed = false;
        Ok(result?)
    }
    /// Await exact-source cleanup. Unknown cleanup retains the original stream
    /// and registration; subsequent attempts report the recorded failure.
    pub async fn user_stream_cancel(&self, handle: &UserStreamHandle) -> Result<(), CoreError> {
        let Some(entry) = self.user_streams.lookup(handle)? else {
            return Ok(());
        };
        let task = schedule_close(self.user_streams.clone(), entry.clone(), &self.runtime)
            .map_err(|error| {
                entry.retain();
                if entry.cx.filesystem_mutation_possible() {
                    stopped(&entry)
                } else {
                    error
                }
            })?;
        match task.await {
            Ok(result) => Ok(result?),
            Err(_) => {
                entry.retain();
                Err(stopped(&entry))
            }
        }
    }
    pub(super) async fn close_user_streams(&self) -> Result<(), CoreError> {
        let mut failure = None;
        for entry in self.user_streams.snapshot()? {
            match schedule_close(self.user_streams.clone(), entry.clone(), &self.runtime) {
                Ok(task) => match task.await {
                    Ok(Ok(())) => (),
                    Ok(Err(error)) => {
                        record_failure(&mut failure, CoreError::from(error));
                    }
                    Err(_) => {
                        entry.retain();
                        record_failure(&mut failure, stopped(&entry));
                    }
                },
                Err(error) => {
                    entry.retain();
                    let error = if entry.cx.filesystem_mutation_possible() {
                        CoreError::Step(interruption(&entry.cx, "User cleanup admission failed"))
                    } else {
                        error
                    };
                    record_failure(&mut failure, error);
                }
            }
        }
        failure.map_or(Ok(()), Err)
    }
}

fn schedule_close(
    registry: Arc<Registry>,
    entry: Arc<Entry>,
    runtime: &tokio::runtime::Handle,
) -> Result<tokio::task::JoinHandle<Result<(), StepError>>, CoreError> {
    entry.cx.cancel().cancel();
    let control = reserve_task(&entry.cx)?;
    let retention = RetainOnAbandon {
        entry: entry.clone(),
        finished: false,
    };
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        runtime.spawn(async move {
            let _control = control;
            let mut retention = retention;
            let result = close_entry(registry, entry).await;
            retention.finished = true;
            result
        })
    }))
    .map_err(|_| unavailable("owning runtime could not schedule User cleanup"))
}
async fn close_entry(registry: Arc<Registry>, entry: Arc<Entry>) -> Result<(), StepError> {
    loop {
        let ready = entry.ready.notified();
        let mut state = entry.state.lock().await;
        if entry.supervisor_failed.load(Ordering::Acquire) {
            entry.retain();
            return Err(supervisor_failure(&entry));
        }
        match &mut *state {
            State::Preparing => {
                drop(state);
                ready.await;
                continue;
            }
            State::Finished => {
                registry.remove(entry.id);
                return Ok(());
            }
            State::Failed(error) => {
                entry.retain();
                return Err(error.clone());
            }
            State::Live(source) => {
                if let Some(error) = &source.cleanup_error {
                    entry.retain();
                    return Err(preserve_effect_error(&entry.cx, error.clone()));
                }
                if let Err(error) = source.stream.close().await {
                    let error = preserve_effect_error(&entry.cx, error);
                    source.cleanup_error = Some(error.clone());
                    entry.retain();
                    return Err(error);
                }
                if entry.cx.filesystem_mutation_possible() {
                    let error = interruption(&entry.cx, "User stream cancelled");
                    source.cleanup_error = Some(error.clone());
                    entry.retain();
                    return Err(error);
                }
                *state = State::Finished;
                registry.remove(entry.id);
                return Ok(());
            }
        }
    }
}
async fn pull_entry(
    registry: Arc<Registry>,
    entry: Arc<Entry>,
    maximum: u32,
) -> Result<CoreBuffer, StepError> {
    let mut state = entry.state.lock().await;
    if entry.supervisor_failed.load(Ordering::Acquire) {
        entry.retain();
        return Err(supervisor_failure(&entry));
    }
    let State::Live(source) = &mut *state else {
        return Err(StepError::new("invalid", "User stream is not live"));
    };
    if let Some(error) = &source.cleanup_error {
        return Err(error.clone());
    }
    let result = {
        let pull = source.stream.pull_final(maximum);
        tokio::pin!(pull);
        let (mut external, mut context, mut deadline) = (true, true, true);
        loop {
            tokio::select! {
                _=entry.external.cancelled(), if external => { external = false; entry.cx.cancel().cancel(); }
                _=entry.cx.cancel().cancelled(), if context => { context = false; }
                _=tokio::time::sleep_until(entry.cx.deadline().into()), if deadline => { deadline = false; entry.cx.cancel().cancel(); }
                result=&mut pull => break result,
            }
        }
    };
    if entry.supervisor_failed.load(Ordering::Acquire) {
        let error = supervisor_failure(&entry);
        source.cleanup_error = Some(error.clone());
        entry.retain();
        return Err(error);
    }
    let result = result
        .and_then(|buffer| {
            if entry.cx.check_active().is_err() || entry.external.is_cancelled() {
                return Err(interruption(
                    &entry.cx,
                    "User stream cancelled before batch delivery",
                ));
            }
            let length = u64::try_from(buffer.len())
                .map_err(|_| StepError::new("limit-exceeded", "User stream byte count overflow"))?;
            source.output_bytes = source
                .output_bytes
                .checked_add(length)
                .filter(|total| *total <= entry.cx.limits().output_bytes)
                .ok_or_else(|| {
                    StepError::new("limit-exceeded", "User stream output bound exceeded")
                })?;
            Ok(buffer)
        })
        .map_err(|error| preserve_effect_error(&entry.cx, error));
    if result
        .as_ref()
        .is_ok_and(|buffer| buffer.as_bytes() != b"[]")
    {
        return result;
    }
    // Keep the state/control alive until close actually returns, even if the
    // caller has abandoned this owning task's JoinHandle.
    if let Err(error) = source.stream.close().await {
        let error = match result {
            Err(original) => with_cleanup(original, error),
            Ok(_) => error,
        };
        let error = preserve_effect_error(&entry.cx, error);
        source.cleanup_error = Some(error.clone());
        entry.retain();
        return Err(error);
    }
    if entry.cx.filesystem_mutation_possible() {
        let error = match result {
            Err(error) => preserve_effect_error(&entry.cx, error),
            Ok(_) => interruption(&entry.cx, "User stream completion is unconfirmed"),
        };
        source.cleanup_error = Some(error.clone());
        entry.retain();
        return Err(error);
    }
    if let Err(error) = &result {
        if error.code == "filesystem-commit-unknown" {
            source.cleanup_error = Some(error.clone());
            entry.retain();
            return Err(error.clone());
        }
    }
    *state = State::Finished;
    registry.remove(entry.id);
    result
}
pub(super) fn drop_registry(registry: Arc<Registry>, runtime: &tokio::runtime::Handle) {
    if let Ok(entries) = registry.snapshot() {
        for entry in entries {
            if schedule_close(registry.clone(), entry.clone(), runtime).is_err() {
                entry.retain();
            }
        }
    }
}

#[cfg(test)]
#[path = "user_ports_tests.rs"]
mod tests;
