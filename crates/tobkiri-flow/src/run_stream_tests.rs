use super::*;
use crate::engine::RunCompletion;
use tobkiri_canon::{AllocationRequest, Digest, ids::RequestId};
use tobkiri_gate::CoreControl;

struct TestBudget;
impl AllocationBudget for TestBudget {
    fn reserve(&self, request: AllocationRequest) -> Result<(), AllocationError> {
        if request.bytes > 1024 * 1024 {
            Err(AllocationError::Limit)
        } else {
            Ok(())
        }
    }
    fn release(&self, _: u64) {}
}

fn source() -> (
    RunStream,
    mpsc::Sender<RunEvent>,
    oneshot::Sender<RunCompletionReceipt>,
) {
    let (events, receiver) = mpsc::channel(4);
    let (completion, result) = oneshot::channel();
    let control = CoreControl::default();
    let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
    (
        RunStream {
            events: receiver,
            result: Some(result),
            completion: None,
            cancel: CancellationToken::new_with_control(&control).unwrap(),
            stream_budget: budget.clone(),
            event_budget: budget,
            max_batch_bytes: 1024 * 1024,
            terminal_seen: false,
            terminal_outcome: None,
            terminal_failure: None,
            terminal_reported: false,
            batch_has_terminal: false,
            ended: false,
            pending_batch: None,
            pending_event: None,
            empty_value: None,
            empty_buffer: None,
        },
        events,
        completion,
    )
}

fn failed(error: StepError) -> RunEvent {
    RunEvent {
        request: RequestId::parse("req_test").unwrap(),
        plan_digest: Digest::hash(b"bounded-fixture"),
        sequence: 1,
        kind: RunEventKind::Failed { error },
    }
}

fn send_completion(
    sender: oneshot::Sender<RunCompletionReceipt>,
    outcome: Result<RunCompletion, StepError>,
    cleanup: Result<(), StepError>,
) {
    assert!(
        sender
            .send(RunCompletionReceipt { outcome, cleanup })
            .is_ok()
    );
}

#[tokio::test]
async fn run_stream_failed_terminal_is_not_reported_until_final_encoding_succeeds() {
    let (mut stream, events, completion) = source();
    let error = StepError::new("conflict", "ordinary execution failure");
    events.send(failed(error.clone())).await.unwrap();
    drop(events);
    send_completion(completion, Err(error.clone()), Ok(()));
    stream.max_batch_bytes = 1;
    assert_eq!(
        stream.pull_final(1).await.unwrap_err().code,
        "batch-too-large"
    );
    assert!(stream.terminal_seen);
    assert!(!stream.terminal_reported);
    // Fixture capacity becomes available; retry the retained exact batch.
    stream.max_batch_bytes = 1024 * 1024;
    assert_ne!(stream.pull_final(1).await.unwrap().as_bytes(), b"[]");
    assert!(stream.terminal_reported);
    assert_eq!(stream.pull_final(1).await.unwrap_err(), error);
    stream.close().await.unwrap();
}

#[tokio::test]
async fn run_stream_unreported_or_different_failure_is_not_acknowledged_by_close() {
    for different in [false, true] {
        let (mut stream, events, completion) = source();
        if different {
            events
                .send(failed(StepError::new("conflict", "different failure")))
                .await
                .unwrap();
            stream.pull_final(1).await.unwrap();
        }
        drop(events);
        let error = StepError::new("unavailable", "actual interpreter failure");
        send_completion(completion, Err(error.clone()), Ok(()));
        assert_eq!(stream.close().await.unwrap_err(), error);
    }
}

#[tokio::test]
async fn run_stream_lost_completion_remains_unknown_after_a_terminal_frame() {
    let (mut stream, events, completion) = source();
    events
        .send(failed(StepError::new("unavailable", "reported failure")))
        .await
        .unwrap();
    stream.pull_final(1).await.unwrap();
    drop(events);
    drop(completion);
    let error = stream.pull_final(1).await.unwrap_err();
    assert_eq!(error.code, "internal");
    assert_eq!(error.message, "Flow completion channel closed");
    assert_eq!(stream.close().await.unwrap_err(), error);
}

#[tokio::test]
async fn run_stream_missing_terminal_is_not_successful_eof_from_a_cleanup_receipt() {
    let (mut stream, events, completion) = source();
    drop(events);
    send_completion(completion, Ok(RunCompletion::Intermediate), Ok(()));
    let error = stream.pull_final(1).await.unwrap_err();
    assert_eq!(error.code, "internal");
    assert_eq!(
        error.message,
        "Flow completion did not deliver a terminal frame"
    );
}

#[tokio::test]
async fn run_stream_observed_cancelled_result_does_not_hide_actual_cleanup_failure() {
    for code in [
        "unavailable",
        "conflict",
        "storage-sql-commit-unknown",
        "filesystem-commit-unknown",
    ] {
        let (mut stream, events, completion) = source();
        drop(events);
        let cleanup = StepError::new(code, "actual cleanup failure");
        send_completion(
            completion,
            Err(StepError::new("cancelled", "execution cancelled")),
            Err(cleanup.clone()),
        );
        assert_eq!(stream.close().await.unwrap_err(), cleanup);
    }
}

#[tokio::test]
async fn run_stream_abandoned_receipt_wait_retains_the_same_completion_channel() {
    let (mut stream, events, completion) = source();
    drop(events);
    {
        let mut close = Box::pin(stream.close());
        assert!(futures::poll!(&mut close).is_pending());
    }
    assert!(stream.result.is_some());
    send_completion(
        completion,
        Err(StepError::new("cancelled", "no acquired source")),
        Ok(()),
    );
    stream.close().await.unwrap();
    assert!(stream.result.is_none());
    stream.close().await.unwrap();
}
