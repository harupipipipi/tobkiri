//! The real FlowEngine / RunStream chain, with bounded in-memory providers.
use super::*;

async fn drain_failure(stream: &mut CoreStream) -> (StepError, Option<String>) {
    let mut terminal = None;
    for _ in 0..16 {
        match stream.pull_final(16).await {
            Ok(batch) => {
                assert_ne!(batch.as_bytes(), b"[]", "failed Flow became successful EOF");
                let frames: serde_json::Value = serde_json::from_slice(batch.as_bytes()).unwrap();
                for frame in frames.as_array().unwrap() {
                    match frame
                        .pointer("/kind/kind")
                        .and_then(serde_json::Value::as_str)
                    {
                        Some("failed") => {
                            assert!(terminal.is_none());
                            terminal =
                                Some(frame["kind"]["error"]["code"].as_str().unwrap().into());
                        }
                        Some("cancelled") => {
                            assert!(terminal.is_none());
                            terminal = Some("cancelled".into());
                        }
                        Some("completed") => panic!("failed Flow reported success"),
                        _ => (),
                    }
                }
            }
            Err(error) => return (error, terminal),
        }
    }
    panic!("fixture failed Flow did not terminate within its bounded frames");
}

fn start(
    fake: Arc<Fake>,
    journal: Arc<dyn RunJournal>,
    op: &str,
    source: bool,
) -> (CoreStream, tobkiri_gate::CoreControl) {
    let cx = context();
    let control = cx.control().clone();
    let mut operation = call("operation", op, CanonValue::Null, None);
    if source {
        operation.stream = Some(StreamRoute {
            emit: shape(),
            collect: None,
        });
    }
    let run = FlowEngine::new(fake, journal)
        .run_fixture(
            cx,
            Arc::new(flow(vec![operation])),
            owned(CanonValue::Null),
            limits(),
        )
        .unwrap();
    (run.into_stream(), control)
}

#[tokio::test]
async fn failed_terminal_drain_keeps_unavailable_and_conflict_separate_from_cleanup() {
    for (op, expected) in [("fail", "unavailable"), ("conflict", "conflict")] {
        let fake = Arc::new(Fake::default());
        let (mut stream, control) = start(fake.clone(), Arc::new(Journal::default()), op, false);
        let (error, terminal) = drain_failure(&mut stream).await;
        assert_eq!(terminal.as_deref(), Some(expected));
        assert_eq!(error.code, expected);
        stream.close().await.unwrap();
        stream.close().await.unwrap();
        assert_eq!(control.usage().count(ControlKind::Task), 0);
        assert_eq!(control.usage().count(ControlKind::Frame), 0);
        assert_eq!(fake.calls.lock().unwrap().len(), 1);
    }
}

#[tokio::test]
async fn cancelled_outcome_needs_successful_cleanup_with_or_without_a_source() {
    for (op, source, closes) in [("cancelled", false, 0), ("cancelled-stream", true, 1)] {
        let fake = Arc::new(Fake::default());
        let (mut stream, control) = start(fake.clone(), Arc::new(Journal::default()), op, source);
        let (error, terminal) = drain_failure(&mut stream).await;
        assert_eq!(terminal.as_deref(), Some("cancelled"));
        assert_eq!(error.code, "cancelled");
        stream.close().await.unwrap();
        assert_eq!(fake.stream_closed.load(Ordering::SeqCst), closes);
        assert_eq!(control.usage().count(ControlKind::Task), 0);
    }
}

#[tokio::test]
async fn actual_source_close_failures_remain_unknown_even_after_failed_terminal() {
    for op in [
        "cleanup-unavailable",
        "cleanup-conflict",
        "cleanup-cancelled",
    ] {
        let fake = Arc::new(Fake::default());
        let (mut stream, _) = start(fake.clone(), Arc::new(Journal::default()), op, true);
        let (error, terminal) = drain_failure(&mut stream).await;
        assert!(
            terminal.is_some(),
            "real failure terminal was not delivered"
        );
        assert_eq!(error.code, "internal");
        assert_eq!(stream.close().await.unwrap_err().code, "internal");
        assert_eq!(stream.close().await.unwrap_err().code, "internal");
        assert_eq!(fake.stream_closed.load(Ordering::SeqCst), 1);
    }
}

struct FailedTerminalJournal;
#[async_trait]
impl RunJournal for FailedTerminalJournal {
    async fn append(
        &self,
        _: &CallCx,
        event: &RunEvent,
        _: &Arc<dyn AllocationBudget>,
    ) -> Result<(), StepError> {
        if matches!(
            event.kind,
            RunEventKind::Completed { .. }
                | RunEventKind::Failed { .. }
                | RunEventKind::Cancelled { .. }
        ) {
            Err(StepError::new(
                "unavailable",
                "fixture terminal journal failed",
            ))
        } else {
            Ok(())
        }
    }
}

#[tokio::test]
async fn terminal_journal_failure_is_not_hidden_by_failed_or_cancelled_execution() {
    for op in ["fail", "cancelled", "echo"] {
        let (mut stream, _) = start(
            Arc::new(Fake::default()),
            Arc::new(FailedTerminalJournal),
            op,
            false,
        );
        let (error, terminal) = drain_failure(&mut stream).await;
        assert!(terminal.is_none());
        assert_eq!(error.code, "internal");
        assert_eq!(stream.close().await.unwrap_err().code, "internal");
    }
}

#[tokio::test]
async fn no_replay_outcomes_and_cleanup_survive_separate_quiescence_receipts() {
    for (op, source, code) in [
        (
            "filesystem-commit-unknown",
            false,
            "filesystem-commit-unknown",
        ),
        (
            "storage-sql-commit-unknown",
            false,
            "storage-sql-commit-unknown",
        ),
        (
            "storage-pending-data-move",
            false,
            "storage-pending-data-move",
        ),
        ("cleanup-sql-unknown", true, "storage-sql-commit-unknown"),
    ] {
        let (mut stream, _) = start(
            Arc::new(Fake::default()),
            Arc::new(Journal::default()),
            op,
            source,
        );
        let (error, terminal) = drain_failure(&mut stream).await;
        // Public lifecycle frames deliberately use the existing closed audit
        // code set. The original no-replay facade survives the result/cleanup
        // path; this test does not mint a sealed native Store/Fs error.
        assert_eq!(terminal.as_deref(), Some("internal"));
        assert_eq!(error.code, code);
        assert_eq!(stream.close().await.unwrap_err().code, code);
    }
    let (mut stream, _) = start(
        Arc::new(Fake::default()),
        Arc::new(FailedTerminalJournal),
        "storage-sql-commit-unknown",
        false,
    );
    assert_eq!(
        drain_failure(&mut stream).await.0.code,
        "storage-sql-commit-unknown"
    );
    assert_eq!(
        stream.close().await.unwrap_err().code,
        "storage-sql-commit-unknown"
    );
}

#[tokio::test]
async fn dropping_event_receiver_does_not_turn_execution_failure_into_success() {
    let run = FlowEngine::new(Arc::new(Fake::default()), Arc::new(Journal::default()))
        .run_fixture(
            context(),
            Arc::new(flow(vec![call(
                "failure",
                "conflict",
                CanonValue::Null,
                None,
            )])),
            owned(CanonValue::Null),
            limits(),
        )
        .unwrap();
    drop(run.events);
    assert_eq!(run.result.await.unwrap_err().code, "conflict");
}

#[tokio::test]
async fn later_successful_close_cannot_clear_an_earlier_cleanup_failure() {
    let fake = Arc::new(Fake::default());
    let mut failing = call("failing", "cleanup-conflict", CanonValue::Null, None);
    failing.stream = Some(StreamRoute {
        emit: shape(),
        collect: None,
    });
    let mut succeeding = call("succeeding", "stream", CanonValue::Null, None);
    succeeding.stream = failing.stream.clone();
    let mut plan = flow(vec![failing, succeeding]);
    plan.document
        .on_error
        .steps
        .insert("failing".into(), ErrorAction::Continue);
    let run = FlowEngine::new(fake.clone(), Arc::new(Journal::default()))
        .run_fixture(context(), Arc::new(plan), owned(CanonValue::Null), limits())
        .unwrap();
    let (result, events) = finish(Ok(run)).await;
    // ErrorPolicy may continue execution, but it cannot erase the independent
    // physical-cleanup receipt from the first acquired source.
    assert!(
        !events
            .iter()
            .any(|event| matches!(event.kind, RunEventKind::Completed { .. }))
    );
    assert!(events.iter().any(
        |event| matches!(&event.kind, RunEventKind::Failed { error } if error.code == "internal")
    ));
    assert_eq!(fake.stream_closed.load(Ordering::SeqCst), 2);
    assert_eq!(result.unwrap_err().code, "internal");
}

#[derive(Clone, Copy)]
enum AfterInterruption {
    Error(&'static str),
    ImmediateError(&'static str),
    Eof,
    StuckBeforeSource,
}

#[derive(Clone, Copy)]
enum CloseAfterInterruption {
    Held,
    Ready,
    Stuck,
}

#[derive(Default)]
struct InterruptionState {
    pull_entered: tokio::sync::Notify,
    close_entered: tokio::sync::Notify,
    release_close: tokio::sync::Notify,
    pulls: AtomicUsize,
    closes: AtomicUsize,
    dropped: AtomicUsize,
    inactive_closes: AtomicUsize,
    original_deadline: Mutex<Option<Instant>>,
}

struct InterruptibleSource {
    state: Arc<InterruptionState>,
    cancel: tobkiri_gate::CancellationToken,
    deadline: Instant,
    after: AfterInterruption,
    close: CloseAfterInterruption,
    close_error: Option<&'static str>,
}

#[async_trait]
impl StreamSource for InterruptibleSource {
    async fn pull(&mut self, _: u32) -> Result<CoreValue, StepError> {
        self.state.pulls.fetch_add(1, Ordering::SeqCst);
        self.state.pull_entered.notify_one();
        // This real suspended pull observes the exact original token; the
        // fixture does not return a precomputed cancellation before stopping.
        if !matches!(self.after, AfterInterruption::ImmediateError(_)) {
            let observed = self
                .cancel
                .cancelled()
                .await
                .expect("the original context interruption is observable");
            assert_eq!(self.cancel.cause(), Some(observed));
        }
        match self.after {
            AfterInterruption::Error(code) | AfterInterruption::ImmediateError(code) => Err(
                StepError::new(code, "fixture observed original cancellation"),
            ),
            AfterInterruption::Eof => CoreValue::array_builder(&support::budget(), 0)
                .and_then(|builder| builder.finish())
                .map_err(|_| StepError::new("unavailable", "fixture EOF allocation failed")),
            AfterInterruption::StuckBeforeSource => unreachable!("fixture returns no source"),
        }
    }

    fn cancel(&mut self) {}

    async fn close(&mut self) -> Result<(), StepError> {
        self.state.closes.fetch_add(1, Ordering::SeqCst);
        if self.cancel.is_cancelled() || Instant::now() >= self.deadline {
            self.state.inactive_closes.fetch_add(1, Ordering::SeqCst);
        }
        self.state.close_entered.notify_one();
        match self.close {
            CloseAfterInterruption::Held => self.state.release_close.notified().await,
            CloseAfterInterruption::Ready => (),
            CloseAfterInterruption::Stuck => std::future::pending::<()>().await,
        }
        match self.close_error {
            Some(code) => Err(StepError::new(code, "fixture close remains unconfirmed")),
            None => Ok(()),
        }
    }
}

impl Drop for InterruptibleSource {
    fn drop(&mut self) {
        self.state.dropped.fetch_add(1, Ordering::SeqCst);
    }
}

struct InterruptibleInvoker {
    fake: Fake,
    state: Arc<InterruptionState>,
    after: AfterInterruption,
    close: CloseAfterInterruption,
    close_error: Option<&'static str>,
}

#[async_trait]
impl PortInvoker for InterruptibleInvoker {
    async fn call(
        &self,
        cx: &CallCx,
        shape: &ShapeRef,
        op: &OpName,
        args: CoreValue,
    ) -> Result<StepOutput, StepError> {
        let output = self.fake.call(cx, shape, op, args).await?;
        if op.as_str() != "interruptible" {
            return Ok(output);
        }
        drop(output);
        *self.state.original_deadline.lock().unwrap() = Some(cx.deadline());
        if matches!(self.after, AfterInterruption::StuckBeforeSource) {
            self.state.pull_entered.notify_one();
            std::future::pending::<()>().await;
        }
        Ok(StepOutput::Stream(CoreStream::new(InterruptibleSource {
            state: self.state.clone(),
            cancel: cx.cancel().clone(),
            deadline: cx.deadline(),
            after: self.after,
            close: self.close,
            close_error: self.close_error,
        })))
    }

    async fn emit(&self, cx: &CallCx, shape: &ShapeRef, event: CoreValue) -> Result<(), StepError> {
        self.fake.emit(cx, shape, event).await
    }

    async fn call_reached(
        &self,
        cx: &CallCx,
        frame: &ControlHandle<dyn ActiveCodeFrame>,
        node: &ReachedNode,
        arguments_digest: &Digest,
        args: CoreValue,
    ) -> Result<StepOutput, StepError> {
        let witness = support::check_reached_call(frame, node)?;
        let actual = args
            .digest_guest(&support::budget())
            .map_err(|error| StepError::new("limit-exceeded", error.to_string()))?;
        if &actual != arguments_digest {
            return Err(StepError::new("denied", "fixture argument digest mismatch"));
        }
        self.call(cx, witness.shape(), witness.op(), args).await
    }

    async fn emit_reached(
        &self,
        cx: &CallCx,
        frame: &ControlHandle<dyn ActiveCodeFrame>,
        node: &ReachedNode,
        event_digest: &Digest,
        event: CoreValue,
    ) -> Result<(), StepError> {
        self.fake
            .emit_reached(cx, frame, node, event_digest, event)
            .await
    }
}

struct InterruptedRun {
    handle: tobkiri_flow::RunHandle,
    provider: Arc<InterruptibleInvoker>,
    journal: Arc<Journal>,
    control: tobkiri_gate::CoreControl,
    deadline: Instant,
}

fn interrupted_run(
    after: AfterInterruption,
    close: CloseAfterInterruption,
    close_error: Option<&'static str>,
    timeout: Duration,
) -> InterruptedRun {
    let provider = Arc::new(InterruptibleInvoker {
        fake: Fake::default(),
        state: Arc::new(InterruptionState::default()),
        after,
        close,
        close_error,
    });
    let journal = Arc::new(Journal::default());
    let mut operation = call("operation", "interruptible", CanonValue::Null, None);
    operation.stream = Some(StreamRoute {
        emit: shape(),
        collect: None,
    });
    let plan = flow(vec![
        operation,
        call("must-not-run", "echo", CanonValue::Null, None),
    ]);
    // Compile before starting the measured original request deadline. This
    // uses the existing test frame adapter; it is not real Core/Gate evidence.
    let profile = support::compiled_profile(std::slice::from_ref(&plan));
    let key = support::flow_key(&profile, &plan);
    let cx = support::root_context_with_timeout(limits(), timeout);
    let control = cx.control().clone();
    let deadline = cx.deadline();
    let handle = support::flow_handle(&profile, &key, cx.control());
    let handle = support::run_fixture(
        &FlowEngine::new(provider.clone(), journal.clone()),
        cx,
        &handle,
        owned(CanonValue::Null),
        limits(),
    )
    .unwrap();
    InterruptedRun {
        handle,
        provider,
        journal,
        control,
        deadline,
    }
}

async fn entered_pull(run: &InterruptedRun) {
    tokio::time::timeout(
        Duration::from_secs(2),
        run.provider.state.pull_entered.notified(),
    )
    .await
    .expect("the original source pull started");
}

async fn entered_close(run: &InterruptedRun) {
    tokio::time::timeout(
        Duration::from_secs(2),
        run.provider.state.close_entered.notified(),
    )
    .await
    .expect("the same source reached its real close operation");
}

fn assert_interrupted_once(run: &InterruptedRun) {
    assert_eq!(run.provider.state.pulls.load(Ordering::SeqCst), 1);
    assert_eq!(run.provider.state.closes.load(Ordering::SeqCst), 1);
    assert_eq!(run.provider.state.inactive_closes.load(Ordering::SeqCst), 1);
    assert_eq!(run.provider.fake.calls.lock().unwrap().len(), 1);
    assert_eq!(
        *run.provider.state.original_deadline.lock().unwrap(),
        Some(run.deadline)
    );
    assert!(
        !run.journal
            .0
            .lock()
            .unwrap()
            .iter()
            .any(|event| { matches!(event.kind, RunEventKind::Completed { .. }) })
    );
}

#[tokio::test]
async fn cancelled_execution_retains_same_pull_until_one_close_without_next_dispatch() {
    let mut run = interrupted_run(
        AfterInterruption::Error("cancelled"),
        CloseAfterInterruption::Held,
        None,
        Duration::from_secs(5),
    );
    entered_pull(&run).await;
    run.handle.cancel.cancel();
    entered_close(&run).await;
    assert!(futures::poll!(&mut run.handle.result).is_pending());
    assert_eq!(run.provider.state.dropped.load(Ordering::SeqCst), 0);
    assert_eq!(run.control.usage().count(ControlKind::Task), 1);
    run.provider.state.release_close.notify_one();
    let error = (&mut run.handle.result).await.unwrap_err();
    assert_eq!(error.code, "cancelled");
    assert_interrupted_once(&run);
    assert_eq!(run.provider.state.dropped.load(Ordering::SeqCst), 1);
    assert_eq!(run.control.usage().count(ControlKind::Task), 0);
}

#[tokio::test]
async fn original_deadline_signals_same_source_and_awaits_its_close() {
    let mut run = interrupted_run(
        AfterInterruption::Error("cancelled"),
        CloseAfterInterruption::Held,
        None,
        Duration::from_millis(500),
    );
    entered_pull(&run).await;
    entered_close(&run).await;
    assert!(Instant::now() >= run.deadline);
    assert_eq!(
        run.handle.cancel.cause(),
        Some(tobkiri_gate::CancellationCause::Deadline)
    );
    assert!(futures::poll!(&mut run.handle.result).is_pending());
    assert_eq!(run.provider.state.dropped.load(Ordering::SeqCst), 0);
    run.provider.state.release_close.notify_one();
    assert_eq!(
        (&mut run.handle.result).await.unwrap_err().code,
        "cancelled"
    );
    assert_interrupted_once(&run);
}

#[tokio::test]
async fn stuck_close_after_cancellation_stays_unknown_without_another_dispatch() {
    let mut run = interrupted_run(
        AfterInterruption::Error("cancelled"),
        CloseAfterInterruption::Stuck,
        None,
        Duration::from_secs(5),
    );
    entered_pull(&run).await;
    run.handle.cancel.cancel();
    entered_close(&run).await;
    let error = tokio::time::timeout(Duration::from_secs(5), &mut run.handle.result)
        .await
        .expect("the existing two-second cleanup grace is bounded")
        .unwrap_err();
    assert_eq!(error.code, "internal");
    assert_interrupted_once(&run);
    assert_eq!(run.provider.state.dropped.load(Ordering::SeqCst), 1);
    assert_eq!(run.control.usage().count(ControlKind::Task), 0);
}

#[tokio::test]
async fn late_source_eof_after_cancellation_is_not_a_completed_flow() {
    let mut run = interrupted_run(
        AfterInterruption::Eof,
        CloseAfterInterruption::Ready,
        None,
        Duration::from_secs(5),
    );
    entered_pull(&run).await;
    run.handle.cancel.cancel();
    assert_eq!(
        (&mut run.handle.result).await.unwrap_err().code,
        "cancelled"
    );
    assert_interrupted_once(&run);
}

#[tokio::test]
async fn source_no_replay_error_survives_cancelled_step_journaling() {
    for code in [
        "filesystem-commit-unknown",
        "storage-sql-commit-unknown",
        "storage-pending-data-move",
    ] {
        let mut run = interrupted_run(
            AfterInterruption::Error(code),
            CloseAfterInterruption::Ready,
            None,
            Duration::from_secs(5),
        );
        entered_pull(&run).await;
        run.handle.cancel.cancel();
        assert_eq!((&mut run.handle.result).await.unwrap_err().code, code);
        assert_interrupted_once(&run);
    }
}

#[tokio::test]
async fn cleanup_no_replay_error_survives_cancelled_step_journaling() {
    for code in [
        "filesystem-commit-unknown",
        "storage-sql-commit-unknown",
        "storage-pending-data-move",
    ] {
        let mut run = interrupted_run(
            AfterInterruption::Error("cancelled"),
            CloseAfterInterruption::Ready,
            Some(code),
            Duration::from_secs(5),
        );
        entered_pull(&run).await;
        run.handle.cancel.cancel();
        assert_eq!((&mut run.handle.result).await.unwrap_err().code, code);
        assert_interrupted_once(&run);
    }
}

#[tokio::test]
async fn stuck_dispatch_before_source_is_not_a_successful_cleanup_receipt() {
    let mut run = interrupted_run(
        AfterInterruption::StuckBeforeSource,
        CloseAfterInterruption::Ready,
        None,
        Duration::from_secs(5),
    );
    entered_pull(&run).await;
    run.handle.cancel.cancel();
    assert_eq!(
        tokio::time::timeout(Duration::from_secs(5), &mut run.handle.result)
            .await
            .expect("the original pending dispatch is bounded by cleanup grace")
            .unwrap_err()
            .code,
        "internal"
    );
    assert_eq!(run.provider.fake.calls.lock().unwrap().len(), 1);
    assert_eq!(run.provider.state.pulls.load(Ordering::SeqCst), 0);
    assert_eq!(run.provider.state.closes.load(Ordering::SeqCst), 0);
    assert_eq!(run.provider.state.dropped.load(Ordering::SeqCst), 0);
    assert_eq!(run.control.usage().count(ControlKind::Task), 0);
    assert!(
        !run.journal
            .0
            .lock()
            .unwrap()
            .iter()
            .any(|event| { matches!(event.kind, RunEventKind::Completed { .. }) })
    );
}

#[tokio::test]
async fn observed_source_no_replay_survives_interrupted_stuck_close() {
    for code in [
        "filesystem-commit-unknown",
        "storage-sql-commit-unknown",
        "storage-pending-data-move",
    ] {
        let mut run = interrupted_run(
            AfterInterruption::Error(code),
            CloseAfterInterruption::Stuck,
            None,
            Duration::from_secs(5),
        );
        entered_pull(&run).await;
        run.handle.cancel.cancel();
        entered_close(&run).await;
        let error = tokio::time::timeout(Duration::from_secs(5), &mut run.handle.result)
            .await
            .expect("stuck cleanup keeps the observed no-replay error")
            .unwrap_err();
        assert_eq!(error.code, code);
        assert_interrupted_once(&run);
        assert_eq!(run.provider.state.dropped.load(Ordering::SeqCst), 1);
    }
}

#[tokio::test]
async fn no_replay_observed_before_cancellation_survives_later_close_timeout() {
    let mut run = interrupted_run(
        AfterInterruption::ImmediateError("storage-sql-commit-unknown"),
        CloseAfterInterruption::Stuck,
        None,
        Duration::from_secs(5),
    );
    entered_close(&run).await;
    assert!(!run.handle.cancel.is_cancelled());
    assert_eq!(run.provider.state.inactive_closes.load(Ordering::SeqCst), 0);
    run.handle.cancel.cancel();
    let error = tokio::time::timeout(Duration::from_secs(5), &mut run.handle.result)
        .await
        .expect("interruption cannot discard an earlier observed no-replay error")
        .unwrap_err();
    assert_eq!(error.code, "storage-sql-commit-unknown");
    assert_eq!(run.provider.state.pulls.load(Ordering::SeqCst), 1);
    assert_eq!(run.provider.state.closes.load(Ordering::SeqCst), 1);
    assert_eq!(run.provider.state.dropped.load(Ordering::SeqCst), 1);
    assert_eq!(run.provider.fake.calls.lock().unwrap().len(), 1);
    assert_eq!(run.control.usage().count(ControlKind::Task), 0);
    assert!(
        !run.journal
            .0
            .lock()
            .unwrap()
            .iter()
            .any(|event| { matches!(event.kind, RunEventKind::Completed { .. }) })
    );
}

#[derive(Default)]
struct HeldFailureJournal {
    entered: tokio::sync::Notify,
    completed: AtomicUsize,
}

#[async_trait]
impl RunJournal for HeldFailureJournal {
    async fn append(
        &self,
        _: &CallCx,
        event: &RunEvent,
        _: &Arc<dyn AllocationBudget>,
    ) -> Result<(), StepError> {
        if matches!(event.kind, RunEventKind::StepFailed { .. }) {
            self.entered.notify_one();
            std::future::pending::<()>().await;
        }
        if matches!(event.kind, RunEventKind::Completed { .. }) {
            self.completed.fetch_add(1, Ordering::SeqCst);
        }
        Ok(())
    }
}

#[tokio::test]
async fn observed_no_replay_survives_interruption_during_step_failed_journal() {
    let fake = Arc::new(Fake::default());
    let journal = Arc::new(HeldFailureJournal::default());
    let mut run = FlowEngine::new(fake.clone(), journal.clone())
        .run_fixture(
            context(),
            Arc::new(flow(vec![
                call(
                    "failed",
                    "storage-sql-commit-unknown",
                    CanonValue::Null,
                    None,
                ),
                call("must-not-run", "echo", CanonValue::Null, None),
            ])),
            owned(CanonValue::Null),
            limits(),
        )
        .unwrap();
    tokio::time::timeout(Duration::from_secs(2), journal.entered.notified())
        .await
        .expect("the observed real source error reached the held journal await");
    assert!(!run.cancel.is_cancelled());
    assert!(futures::poll!(&mut run.result).is_pending());
    run.cancel.cancel();
    assert_eq!(
        tokio::time::timeout(Duration::from_secs(5), &mut run.result)
            .await
            .expect("journal interruption is bounded by the same cleanup grace")
            .unwrap_err()
            .code,
        "storage-sql-commit-unknown"
    );
    assert_eq!(fake.calls.lock().unwrap().len(), 1);
    assert_eq!(journal.completed.load(Ordering::SeqCst), 0);
}
