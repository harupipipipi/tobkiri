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
