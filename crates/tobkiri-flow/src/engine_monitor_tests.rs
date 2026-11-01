//! The actual native monitor must finish before its public physical receipt.

use super::*;
use std::sync::{Mutex as StdMutex, mpsc as blocking};
use std::task::{Wake, Waker};
use std::time::Duration;

struct Close;
impl StreamCloseControl for Close {
    fn record_close(&self, _: StreamCloseReason) -> Result<(), StepError> {
        Ok(())
    }
}

struct ReleaseWake(Option<blocking::Sender<()>>);
impl ReleaseWake {
    fn release(&mut self) {
        if let Some(sender) = self.0.take() {
            let _ = sender.send(());
        }
    }
}
impl Drop for ReleaseWake {
    fn drop(&mut self) {
        self.release();
    }
}

struct FirstWakeGate {
    entered: StdMutex<Option<oneshot::Sender<()>>>,
    released: StdMutex<blocking::Receiver<()>>,
}
impl Wake for FirstWakeGate {
    fn wake(self: Arc<Self>) {
        self.wake_by_ref();
    }
    fn wake_by_ref(self: &Arc<Self>) {
        let entered = self.entered.lock().unwrap().take();
        if let Some(entered) = entered {
            let _ = entered.send(());
            let _ = self.released.lock().unwrap().recv();
        }
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn original_monitor_join_outlives_ready_channel_and_abandoned_observer() {
    for physical_unknown in [false, true] {
        let control = CoreControl::default();
        let (start, started) = oneshot::channel();
        let (release_body, blocked_body) = oneshot::channel();
        let task = RunTask::new(
            async move {
                let _ = start.send(());
                let _ = blocked_body.await;
                if physical_unknown {
                    Err(RunTaskPhysicalError::CompletionUnknown)
                } else {
                    Ok(())
                }
            },
            &control,
        )
        .unwrap();
        let completion = TokioRunTaskSpawner {
            control: control.clone(),
            runtime: tokio::runtime::Handle::current(),
        }
        .spawn(task, Arc::new(Close))
        .unwrap();
        tokio::time::timeout(Duration::from_secs(1), started)
            .await
            .unwrap()
            .unwrap();
        let (entered, observed) = oneshot::channel();
        let (release_wake, released) = blocking::channel();
        // Drop releases a blocked real monitor even if a test assertion fails.
        let mut release_wake = ReleaseWake(Some(release_wake));
        let gate = Arc::new(FirstWakeGate {
            entered: StdMutex::new(Some(entered)),
            released: StdMutex::new(released),
        });
        let waker = Waker::from(gate);
        let mut first = Box::pin(tokio::task::unconstrained(completion.wait()));
        assert!(
            first
                .as_mut()
                .poll(&mut Context::from_waker(&waker))
                .is_pending()
        );
        release_body.send(()).unwrap();
        tokio::time::timeout(Duration::from_secs(1), observed)
            .await
            .expect("original monitor published its real result")
            .unwrap();
        // Tokio writes the original oneshot result before invoking this
        // registered Wake. The real monitor is now stopped inside send(),
        // still owning its Task reservation, after the actual child Join.
        assert_eq!(control.usage().count(ControlKind::Task), 1);
        assert!(
            first
                .as_mut()
                .poll(&mut Context::from_waker(&waker))
                .is_pending()
        );
        drop(first);
        assert_eq!(control.usage().count(ControlKind::Waiter), 0);
        let mut replacement = Box::pin(tokio::task::unconstrained(completion.wait()));
        assert!(
            replacement
                .as_mut()
                .poll(&mut Context::from_waker(&waker))
                .is_pending()
        );
        assert_eq!(control.usage().count(ControlKind::Task), 1);
        release_wake.release();
        let result = tokio::time::timeout(Duration::from_secs(1), replacement)
            .await
            .expect("same original monitor released its real custody");
        let expected = if physical_unknown {
            Err(task_unknown())
        } else {
            Ok(())
        };
        assert_eq!(result, expected);
        assert_eq!(completion.wait().await, expected);
        assert_eq!(control.usage().count(ControlKind::Task), 0);
        assert_eq!(control.usage().count(ControlKind::Waiter), 0);
        drop(completion);
        assert_eq!(control.usage().bytes, 0);
    }
}

#[test]
fn runtime_drop_releases_original_monitor_and_caches_unknown_without_a_cycle() {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    let control = CoreControl::default();
    let (started, observed) = oneshot::channel();
    let task = RunTask::new(
        async move {
            let _ = started.send(());
            std::future::pending::<()>().await;
            Ok(())
        },
        &control,
    )
    .unwrap();
    let completion = TokioRunTaskSpawner {
        control: control.clone(),
        runtime: runtime.handle().clone(),
    }
    .spawn(task, Arc::new(Close))
    .unwrap();
    runtime.block_on(async {
        tokio::time::timeout(Duration::from_secs(1), observed)
            .await
            .unwrap()
            .unwrap();
    });
    assert_eq!(control.usage().count(ControlKind::Task), 2);
    drop(runtime);
    assert_eq!(control.usage().count(ControlKind::Task), 0);
    // Runtime destruction drops the real monitor's retained completion Arc,
    // breaking self-retention even though no observer ever joined it there.
    assert_eq!(Arc::strong_count(&completion), 1);
    let observer = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    observer.block_on(async {
        for _ in 0..2 {
            assert_eq!(
                tokio::time::timeout(Duration::from_secs(1), completion.wait())
                    .await
                    .unwrap(),
                Err(task_unknown())
            );
        }
    });
    assert_eq!(control.usage().count(ControlKind::Waiter), 0);
    drop(completion);
    assert_eq!(control.usage().bytes, 0);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn successful_channel_cannot_mask_actual_monitor_panic() {
    struct PanicWake(StdMutex<Option<oneshot::Sender<()>>>);
    impl Wake for PanicWake {
        fn wake(self: Arc<Self>) {
            self.wake_by_ref();
        }
        fn wake_by_ref(self: &Arc<Self>) {
            let entered = self.0.lock().unwrap().take();
            if let Some(entered) = entered {
                let _ = entered.send(());
                panic!("fault injected in actual monitor after result publication");
            }
        }
    }
    let control = CoreControl::default();
    let (release, blocked) = oneshot::channel();
    let task = RunTask::new(
        async move {
            let _ = blocked.await;
            Ok(())
        },
        &control,
    )
    .unwrap();
    let completion = TokioRunTaskSpawner {
        control: control.clone(),
        runtime: tokio::runtime::Handle::current(),
    }
    .spawn(task, Arc::new(Close))
    .unwrap();
    let (entered, observed) = oneshot::channel();
    let waker = Waker::from(Arc::new(PanicWake(StdMutex::new(Some(entered)))));
    let mut original = Box::pin(tokio::task::unconstrained(completion.wait()));
    assert!(
        original
            .as_mut()
            .poll(&mut Context::from_waker(&waker))
            .is_pending()
    );
    release.send(()).unwrap();
    tokio::time::timeout(Duration::from_secs(1), observed)
        .await
        .unwrap()
        .unwrap();
    // The child completed normally and the real monitor published Ok, but
    // unwinding from that exact send makes its own original Join unknown.
    assert_eq!(
        tokio::time::timeout(Duration::from_secs(1), original)
            .await
            .unwrap(),
        Err(task_unknown())
    );
    assert_eq!(completion.wait().await, Err(task_unknown()));
    assert_eq!(control.usage().count(ControlKind::Task), 0);
    assert_eq!(control.usage().count(ControlKind::Waiter), 0);
    drop(completion);
    assert_eq!(control.usage().bytes, 0);
}
