//! Runtime custody regression on the original body/monitor execution slots.
//! This portable control probe conveys no source selection or IO authority.
use super::*;
use std::sync::atomic::AtomicU64;
use std::time::Duration;

struct CustodyProbe {
    dropped: Arc<AtomicBool>,
    tasks_at_drop: Arc<AtomicU64>,
    control: CoreControl,
}
impl Drop for CustodyProbe {
    fn drop(&mut self) {
        self.tasks_at_drop.store(
            self.control.usage().count(ControlKind::Task),
            Ordering::Release,
        );
        self.dropped.store(true, Ordering::Release);
    }
}
struct CallbackProbe(Arc<AtomicBool>);
impl Drop for CallbackProbe {
    fn drop(&mut self) {
        self.0.store(true, Ordering::Release);
    }
}
struct RetainedExecution {
    control: CoreControl,
    slot: execution_owner::SlotId,
    entered: oneshot::Receiver<()>,
    release: oneshot::Sender<()>,
    physical: oneshot::Receiver<PhysicalResult>,
    callback_dropped: Arc<AtomicBool>,
    custody_dropped: Arc<AtomicBool>,
    tasks_at_drop: Arc<AtomicU64>,
}

fn retained_execution(unknown: bool) -> RetainedExecution {
    let control = CoreControl::default();
    let custody_dropped = Arc::new(AtomicBool::new(false));
    let tasks_at_drop = Arc::new(AtomicU64::new(u64::MAX));
    let reservation = control
        .reserve(
            ControlKind::Frame,
            ControlHandle::<CustodyProbe>::storage_bytes::<ControlReservation>() as u64,
        )
        .unwrap();
    let custody = ControlHandle::try_new_precharged(
        CustodyProbe {
            dropped: custody_dropped.clone(),
            tasks_at_drop: tasks_at_drop.clone(),
            control: control.clone(),
        },
        reservation,
    )
    .unwrap();
    let erased = custody.project(|probe| probe as &(dyn Any + Send + Sync));
    drop(custody);
    let funding = Funding::try_new_precharged(
        (),
        control
            .reserve(
                ControlKind::Queue,
                execution_owner::metadata_bytes().unwrap()
                    + Funding::storage_bytes() as u64
                    + RECORD_CONTROL,
            )
            .unwrap(),
    )
    .unwrap();
    let (physical_tx, physical) = oneshot::channel();
    let changed = Arc::new(tokio::sync::Notify::new());
    let (admission, mut monitor) = execution_owner::reserve_with_custody(
        funding,
        control.reserve(ControlKind::Task, TASK_CONTROL).unwrap(),
        control.reserve(ControlKind::Task, MONITOR_CONTROL).unwrap(),
        physical_tx,
        changed.clone(),
        Some(erased),
    )
    .unwrap();
    let slot = admission.id();
    let (entered_tx, entered) = oneshot::channel();
    let (release, blocked) = oneshot::channel();
    let callback_dropped = Arc::new(AtomicBool::new(false));
    let callback_probe = CallbackProbe(callback_dropped.clone());
    monitor.commit().unwrap();
    monitor
        .spawn_monitor(|| {
            tokio::spawn(async move {
                let execution_owner::Prepared::Ready(mut body) =
                    execution_owner::prepare(slot, execution_owner::Kind::Body)?
                else {
                    return Err(CensusError::StateUnavailable);
                };
                body.commit()?;
                body.spawn_body(|| {
                    tokio::spawn(async move {
                        let _callback_probe = callback_probe;
                        let _ = entered_tx.send(());
                        let _ = blocked.await;
                        if unknown {
                            Err(NativeWorkError::CompletionUnknown)
                        } else {
                            Ok(())
                        }
                    })
                })?;
                match execution_owner::wait_body(slot, &changed).await {
                    Ok(Ok(())) => Ok(PhysicalCompletion::Joined),
                    _ => Err(CensusError::TaskUnknown),
                }
            })
        })
        .unwrap();
    drop(admission);
    RetainedExecution {
        control,
        slot,
        entered,
        release,
        physical,
        callback_dropped,
        custody_dropped,
        tasks_at_drop,
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 3)]
async fn retained_owner_outlives_callback_and_actual_monitor_trailer() {
    let _fixture = lock_census_fixture().await;
    let RetainedExecution {
        control,
        slot,
        entered,
        release,
        physical,
        callback_dropped,
        custody_dropped,
        tasks_at_drop,
    } = retained_execution(false);
    entered.await.unwrap();
    let pause =
        execution_owner::pause_actual_trailer_wake(slot, execution_owner::Kind::Monitor).unwrap();
    assert!(execution_owner::prime_original_body_pending(slot));
    execution_owner::progress(slot);
    release.send(()).unwrap();
    pause.entered.recv().unwrap();
    pause.monitor_ready.recv().unwrap();
    assert!(callback_dropped.load(Ordering::Acquire));
    // Consume the actual ready Joins while Tokio's real monitor trailer still
    // owns its final Waker. Callback destruction alone cannot release custody.
    execution_owner::progress(slot);
    tokio::time::timeout(Duration::from_secs(2), async {
        while !execution_owner::original_task_resources_gone(slot, execution_owner::Kind::Body) {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("the actual body Join and trailer must finish before the monitor trailer");
    assert!(!custody_dropped.load(Ordering::Acquire));
    assert_eq!(control.usage().count(ControlKind::Task), 2);
    drop(physical);
    pause.resume.send(()).unwrap();
    // This receipt comes from deallocation of the actual trailer's Waker, with
    // no observer or maintenance call rescuing the final custody release.
    pause.deallocated.recv().unwrap();
    assert!(custody_dropped.load(Ordering::Acquire));
    assert_eq!(tasks_at_drop.load(Ordering::Acquire), 2);
    assert_eq!(control.usage().count(ControlKind::Task), 0);
    tokio::time::timeout(Duration::from_secs(2), async {
        while control.usage().bytes != 0 {
            execution_owner::progress(slot);
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("confirmed completion must release its actual funded owner");
}

#[tokio::test]
async fn uncertain_physical_receipt_keeps_retained_owner_quarantined() {
    let _fixture = lock_census_fixture().await;
    let RetainedExecution {
        control,
        slot,
        entered,
        release,
        mut physical,
        callback_dropped,
        custody_dropped,
        ..
    } = retained_execution(true);
    entered.await.unwrap();
    execution_owner::progress(slot);
    release.send(()).unwrap();
    let result = tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            execution_owner::progress(slot);
            match physical.try_recv() {
                Ok(result) => break result,
                Err(oneshot::error::TryRecvError::Empty) => tokio::task::yield_now().await,
                Err(error) => panic!("original physical receipt was lost: {error}"),
            }
        }
    })
    .await
    .unwrap();
    assert!(matches!(result, Err(CensusError::TaskUnknown)));
    assert!(callback_dropped.load(Ordering::Acquire));
    drop(physical);
    execution_owner::progress(slot);
    assert!(!custody_dropped.load(Ordering::Acquire));
    assert!(execution_owner::owner_attached(slot));
    assert!(control.usage().bytes > 0);
    // Preserve this exact Unknown owner; the test never clears its receipt or
    // makes its original funding/slot reusable merely to force a clean ledger.
}
