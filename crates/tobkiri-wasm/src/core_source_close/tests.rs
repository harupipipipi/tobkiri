use super::*;
use async_trait::async_trait;
use std::sync::atomic::AtomicUsize;
use tobkiri_canon::CoreValue;
use tobkiri_flow::StreamSource;
use tobkiri_gate::CoreControlLimits;
use tokio::sync::oneshot;

#[derive(Default)]
struct Counts {
    close: AtomicUsize,
    cancel: AtomicUsize,
    drop: AtomicUsize,
}
struct Source {
    counts: Arc<Counts>,
    entered: Option<oneshot::Sender<()>>,
    release: Option<oneshot::Receiver<()>>,
    error: Option<&'static str>,
}
#[async_trait]
impl StreamSource for Source {
    async fn pull(&mut self, _: u32) -> Result<CoreValue, StepError> {
        Err(StepError::new("unavailable", "no test pull"))
    }
    fn cancel(&mut self) {
        self.counts.cancel.fetch_add(1, Ordering::SeqCst);
    }
    async fn close(&mut self) -> Result<(), StepError> {
        self.counts.close.fetch_add(1, Ordering::SeqCst);
        if let Some(entered) = self.entered.take() {
            let _ = entered.send(());
        }
        if let Some(release) = self.release.take() {
            release
                .await
                .map_err(|_| StepError::new("internal", "test channel lost"))?;
        }
        match self.error {
            Some(code) => Err(StepError::new(code, "PRIVATE test message")),
            None => Ok(()),
        }
    }
}
impl Drop for Source {
    fn drop(&mut self) {
        self.counts.drop.fetch_add(1, Ordering::SeqCst);
    }
}

fn source(
    error: Option<&'static str>,
) -> (
    CoreStream,
    Arc<Counts>,
    oneshot::Receiver<()>,
    oneshot::Sender<()>,
) {
    let counts = Arc::new(Counts::default());
    let (entered_tx, entered_rx) = oneshot::channel();
    let (release_tx, release_rx) = oneshot::channel();
    (
        CoreStream::new(Source {
            counts: counts.clone(),
            entered: Some(entered_tx),
            release: Some(release_rx),
            error,
        }),
        counts,
        entered_rx,
        release_tx,
    )
}

fn completion(source: &CoreSourceClose) -> Completion {
    let state = source.state.lock().unwrap();
    let State::Running { completion, .. } = &*state else {
        panic!("no retained close task")
    };
    completion.clone()
}
fn expire(source: &CoreSourceClose) -> Instant {
    let mut state = source.state.lock().unwrap();
    let State::Running { deadline, .. } = &mut *state else {
        panic!("no retained close task")
    };
    *deadline = Instant::now() - std::time::Duration::from_millis(1);
    *deadline
}
fn start(source: &CoreSourceClose) {
    assert!(matches!(
        source.start_or_join(),
        Ok(JoinTarget::Running { .. })
    ));
}

#[tokio::test]
async fn actual_source_closes_and_drops_once_before_a_successful_join() {
    let control = CoreControl::default();
    let (value, counts, entered, release) = source(None);
    let source = CoreSourceClose::new(&control, value).unwrap();
    let mut close = Box::pin(source.close());
    assert!(futures_util::poll!(&mut close).is_pending());
    entered.await.unwrap();
    assert!(source.physical_receipt().is_none());
    assert_eq!(counts.drop.load(Ordering::SeqCst), 0);
    release.send(()).unwrap();
    close.await.unwrap();
    assert_eq!(counts.close.load(Ordering::SeqCst), 1);
    assert_eq!(counts.drop.load(Ordering::SeqCst), 1);
    assert_eq!(source.physical_receipt().unwrap().result, Ok(()));
    assert_eq!(source.close().await, Ok(()));
    assert_eq!(counts.close.load(Ordering::SeqCst), 1);
    assert!(source.lock_open().await.is_err());
    assert_eq!(control.usage().count(ControlKind::Task), 0);
    assert_eq!(control.usage().count(ControlKind::Waiter), 0);
}

#[tokio::test]
async fn sibling_observers_share_one_actual_close_and_one_task() {
    let control = CoreControl::default();
    let (value, counts, entered, release) = source(None);
    let source = CoreSourceClose::new(&control, value).unwrap();
    let alias = source.clone();
    let mut first = Box::pin(source.close());
    let mut second = Box::pin(alias.close());
    assert!(futures_util::poll!(&mut first).is_pending());
    entered.await.unwrap();
    assert!(futures_util::poll!(&mut second).is_pending());
    assert_eq!(control.usage().count(ControlKind::Task), 1);
    release.send(()).unwrap();
    first.await.unwrap();
    second.await.unwrap();
    assert_eq!(counts.close.load(Ordering::SeqCst), 1);
    assert_eq!(counts.drop.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn dropped_observer_keeps_the_same_future_but_not_a_successful_handle_receipt() {
    let control = CoreControl::default();
    let (value, counts, entered, release) = source(None);
    let source = CoreSourceClose::new(&control, value).unwrap();
    let mut observer = Box::pin(source.close());
    assert!(futures_util::poll!(&mut observer).is_pending());
    entered.await.unwrap();
    let same_join = completion(&source);
    drop(observer);
    assert!(source.was_interrupted());
    assert_eq!(control.usage().count(ControlKind::Task), 1);
    assert_eq!(counts.drop.load(Ordering::SeqCst), 0);
    release.send(()).unwrap();
    assert_eq!(same_join.await.unwrap().result, Ok(()));
    assert_eq!(source.physical_receipt().unwrap().result, Ok(()));
    assert_eq!(
        source.close().await,
        Err(HandleCleanupError::CompletionUnknown)
    );
    assert_eq!(counts.close.load(Ordering::SeqCst), 1);
    assert_eq!(counts.drop.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn timeout_retains_resources_and_uses_only_the_original_deadline() {
    let control = CoreControl::default();
    let (value, counts, entered, release) = source(None);
    let source = CoreSourceClose::new(&control, value).unwrap();
    // Exercise the actual two-second observer timeout once. No timing ratio or
    // scheduler-sensitive elapsed assertion is needed; the task stays blocked.
    assert_eq!(
        source.close().await,
        Err(HandleCleanupError::CompletionUnknown)
    );
    entered.await.unwrap();
    let deadline = {
        let state = source.state.lock().unwrap();
        let State::Running { deadline, .. } = &*state else {
            panic!()
        };
        *deadline
    };
    let same_join = completion(&source);
    assert_eq!(
        source.close().await,
        Err(HandleCleanupError::CompletionUnknown)
    );
    {
        let state = source.state.lock().unwrap();
        let State::Running {
            deadline: saved, ..
        } = &*state
        else {
            panic!()
        };
        assert_eq!(*saved, deadline);
    }
    assert_eq!(counts.drop.load(Ordering::SeqCst), 0);
    assert_eq!(control.usage().count(ControlKind::Task), 1);
    assert_eq!(control.usage().count(ControlKind::Registration), 1);
    release.send(()).unwrap();
    same_join.await.unwrap();
    assert!(source.physical_receipt().is_some());
    assert_eq!(
        source.close().await,
        Err(HandleCleanupError::CompletionUnknown)
    );
    assert_eq!(counts.close.load(Ordering::SeqCst), 1);
    assert_eq!(counts.drop.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn slow_source_mutex_does_not_drop_or_duplicate_the_later_close() {
    let control = CoreControl::default();
    let (value, counts, entered, release) = source(None);
    let source = CoreSourceClose::new(&control, value).unwrap();
    let active_pull = source.lock_open().await.unwrap();
    start(&source);
    expire(&source);
    let same_join = completion(&source);
    assert_eq!(
        source.close().await,
        Err(HandleCleanupError::CompletionUnknown)
    );
    assert_eq!(counts.close.load(Ordering::SeqCst), 0);
    assert_eq!(counts.drop.load(Ordering::SeqCst), 0);
    assert_eq!(control.usage().count(ControlKind::Task), 1);
    drop(active_pull);
    entered.await.unwrap();
    assert!(source.lock_open().await.is_err());
    release.send(()).unwrap();
    same_join.await.unwrap();
    assert!(source.physical_receipt().is_some());
    assert_eq!(
        source.close().await,
        Err(HandleCleanupError::CompletionUnknown)
    );
    assert_eq!(counts.close.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn actual_ordinary_close_errors_remain_errors_after_physical_join() {
    for code in ["unavailable", "conflict", "cancelled"] {
        let (value, counts, entered, release) = source(Some(code));
        let source = CoreSourceClose::new(&CoreControl::default(), value).unwrap();
        start(&source);
        entered.await.unwrap();
        release.send(()).unwrap();
        assert_eq!(
            source.close().await,
            Err(HandleCleanupError::CompletionUnknown)
        );
        assert!(source.physical_receipt().is_some());
        assert_eq!(counts.close.load(Ordering::SeqCst), 1);
        assert_eq!(counts.drop.load(Ordering::SeqCst), 1);
    }
}

#[tokio::test]
async fn late_facaded_no_replay_is_stronger_than_an_observer_timeout() {
    for (code, expected) in [
        (
            "filesystem-commit-unknown",
            HandleCleanupError::FacadedFilesystemCommitUnknown,
        ),
        (
            "storage-sql-commit-unknown",
            HandleCleanupError::FacadedStorageCommitUnknown,
        ),
        (
            "storage-pending-data-move",
            HandleCleanupError::FacadedStoragePendingDataMove,
        ),
    ] {
        let (value, counts, entered, release) = source(Some(code));
        let source = CoreSourceClose::new(&CoreControl::default(), value).unwrap();
        start(&source);
        entered.await.unwrap();
        expire(&source);
        let same_join = completion(&source);
        assert_eq!(
            source.close().await,
            Err(HandleCleanupError::CompletionUnknown)
        );
        release.send(()).unwrap();
        same_join.await.unwrap();
        assert_eq!(source.close().await, Err(expected));
        assert_eq!(source.physical_receipt().unwrap().result, Err(expected));
        assert_eq!(counts.close.load(Ordering::SeqCst), 1);
    }
}

#[tokio::test]
async fn missing_actual_source_is_not_a_physical_completion_receipt() {
    let (value, counts, _entered, _release) = source(None);
    let source = CoreSourceClose::new(&CoreControl::default(), value).unwrap();
    let actual = source.holder.source.lock().await.take().unwrap();
    assert_eq!(
        source.close().await,
        Err(HandleCleanupError::CompletionUnknown)
    );
    assert!(source.physical_receipt().is_none());
    assert_eq!(counts.close.load(Ordering::SeqCst), 0);
    assert_eq!(counts.drop.load(Ordering::SeqCst), 0);
    drop(actual);
    assert_eq!(counts.drop.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn registration_and_task_admission_failures_never_enter_close() {
    for kind in [ControlKind::Registration, ControlKind::Task] {
        let limits = match kind {
            ControlKind::Registration => CoreControlLimits {
                max_registrations: 0,
                ..CoreControlLimits::default()
            },
            _ => CoreControlLimits {
                max_tasks: 0,
                ..CoreControlLimits::default()
            },
        };
        let control = CoreControl::new(limits).unwrap();
        let (value, counts, _entered, _release) = source(None);
        let constructed = CoreSourceClose::new(&control, value);
        if matches!(kind, ControlKind::Registration) {
            assert!(constructed.is_err());
        } else {
            let source = constructed.unwrap();
            assert_eq!(
                source.close().await,
                Err(HandleCleanupError::CompletionUnknown)
            );
            assert_eq!(
                source.close().await,
                Err(HandleCleanupError::CompletionUnknown)
            );
            assert!(source.physical_receipt().is_none());
            assert_eq!(counts.drop.load(Ordering::SeqCst), 0);
        }
        assert_eq!(counts.close.load(Ordering::SeqCst), 0);
        assert_eq!(control.usage().count(ControlKind::Task), 0);
    }
}

#[tokio::test]
async fn spawn_failure_is_sticky_and_does_not_retry_source_close() {
    let control = CoreControl::default();
    let (value, counts, _entered, _release) = source(None);
    let source = CoreSourceClose::new(&control, value).unwrap();
    source.fail_spawn.store(true, Ordering::Release);
    assert_eq!(
        source.close().await,
        Err(HandleCleanupError::CompletionUnknown)
    );
    source.fail_spawn.store(false, Ordering::Release);
    assert_eq!(
        source.close().await,
        Err(HandleCleanupError::CompletionUnknown)
    );
    assert!(source.physical_receipt().is_none());
    assert_eq!(counts.close.load(Ordering::SeqCst), 0);
    assert_eq!(control.usage().count(ControlKind::Task), 0);
}

#[tokio::test]
async fn waiter_admission_failure_keeps_the_same_actual_task() {
    let control = CoreControl::new(CoreControlLimits {
        max_waiters: 0,
        ..CoreControlLimits::default()
    })
    .unwrap();
    let (value, counts, entered, release) = source(None);
    let source = CoreSourceClose::new(&control, value).unwrap();
    assert_eq!(
        source.close().await,
        Err(HandleCleanupError::CompletionUnknown)
    );
    entered.await.unwrap();
    let same_join = completion(&source);
    assert_eq!(control.usage().count(ControlKind::Task), 1);
    release.send(()).unwrap();
    same_join.await.unwrap();
    assert!(source.physical_receipt().is_some());
    assert_eq!(
        source.close().await,
        Err(HandleCleanupError::CompletionUnknown)
    );
    assert_eq!(counts.close.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn task_abort_has_no_successful_join_receipt_even_if_source_dropped() {
    let (value, counts, entered, _release) = source(None);
    let source = CoreSourceClose::new(&CoreControl::default(), value).unwrap();
    start(&source);
    entered.await.unwrap();
    source.abort.lock().unwrap().as_ref().unwrap().abort();
    assert_eq!(
        source.close().await,
        Err(HandleCleanupError::CompletionUnknown)
    );
    assert_eq!(counts.close.load(Ordering::SeqCst), 1);
    assert_eq!(counts.drop.load(Ordering::SeqCst), 1);
    assert!(source.physical_receipt().is_none());
    assert_eq!(
        source.close().await,
        Err(HandleCleanupError::CompletionUnknown)
    );
}

#[tokio::test]
async fn losing_all_observers_keeps_prepaid_source_and_task_until_actual_end() {
    let control = CoreControl::default();
    let (value, counts, entered, release) = source(None);
    let source = CoreSourceClose::new(&control, value).unwrap();
    start(&source);
    entered.await.unwrap();
    let same_join = completion(&source);
    drop(source);
    assert_eq!(control.usage().count(ControlKind::Registration), 1);
    assert_eq!(control.usage().count(ControlKind::Task), 1);
    assert_eq!(counts.drop.load(Ordering::SeqCst), 0);
    release.send(()).unwrap();
    same_join.await.unwrap();
    assert_eq!(counts.close.load(Ordering::SeqCst), 1);
    assert_eq!(counts.drop.load(Ordering::SeqCst), 1);
    assert_eq!(control.usage().count(ControlKind::Registration), 0);
    assert_eq!(control.usage().count(ControlKind::Task), 0);
}

#[tokio::test]
async fn prior_generic_corestream_interruption_remains_unknown_without_replay() {
    let (mut value, counts, entered, _release) = source(None);
    let mut old_close = Box::pin(value.close());
    assert!(futures_util::poll!(&mut old_close).is_pending());
    entered.await.unwrap();
    drop(old_close);
    let source = CoreSourceClose::new(&CoreControl::default(), value).unwrap();
    assert_eq!(
        source.close().await,
        Err(HandleCleanupError::CompletionUnknown)
    );
    assert!(source.physical_receipt().is_some());
    assert_eq!(counts.close.load(Ordering::SeqCst), 1);
    assert_eq!(counts.drop.load(Ordering::SeqCst), 1);
}
