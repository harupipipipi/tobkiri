//! The same prepaid owner and sealed inventory used by actual handle terminal
//! delivery. Core's source tests exercise Grant/lease and acknowledged outcome.
use super::*;
use crate::handle_metadata::{HandleMetadata, HandleMetadataRegistry};
use std::{
    sync::{
        Condvar, Mutex,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
    time::Duration,
};
use tobkiri_flow::StepError;
use tobkiri_gate::{CoreControl, CoreControlLimits};

#[derive(Default)]
struct BlockingMetadata {
    entered: tokio::sync::Notify,
    released: Mutex<bool>,
    release: Condvar,
    completed: AtomicBool,
}
impl BlockingMetadata {
    fn run(&self) -> Result<()> {
        self.entered.notify_one();
        let released = self.released.lock().unwrap();
        let (released, _) = self
            .release
            .wait_timeout_while(released, Duration::from_secs(2), |released| !*released)
            .unwrap();
        if !*released {
            bail!("test metadata barrier was not released")
        }
        self.completed.store(true, Ordering::Release);
        Ok(())
    }
    fn release(&self) {
        *self.released.lock().unwrap() = true;
        self.release.notify_all();
    }
    async fn entered(&self) {
        tokio::time::timeout(Duration::from_millis(250), self.entered.notified())
            .await
            .unwrap();
    }
}

// Unwind releases before the runtime joins blocking workers. This guard owns
// no delivery receipt and cannot make a pending close report success.
struct ReleaseOnDrop(Arc<BlockingMetadata>);
impl Drop for ReleaseOnDrop {
    fn drop(&mut self) {
        self.0.release();
    }
}
struct FutureDropProbe(Arc<AtomicUsize>);
impl Drop for FutureDropProbe {
    fn drop(&mut self) {
        self.0.fetch_add(1, Ordering::AcqRel);
    }
}

#[tokio::test(flavor = "current_thread")]
async fn blocking_metadata_does_not_occupy_the_cleanup_executor() {
    let control = CoreControl::default();
    let metadata = HandleMetadata::prepare(&control).unwrap();
    let blocking = Arc::new(BlockingMetadata::default());
    let _release = ReleaseOnDrop(blocking.clone());
    let work = blocking.clone();
    let async_thread = std::thread::current().id();
    let delivery = metadata.run(async move {
        assert_ne!(std::thread::current().id(), async_thread);
        work.run()
    });
    tokio::pin!(delivery);
    assert!(futures_util::poll!(&mut delivery).is_pending());
    blocking.entered().await;
    assert_eq!(control.usage().count(ControlKind::Task), 1);
    assert!(!blocking.completed.load(Ordering::Acquire));
    let (heartbeat, observed) = tokio::sync::oneshot::channel();
    tokio::spawn(async move { heartbeat.send(()).unwrap() });
    tokio::time::timeout(Duration::from_millis(250), observed)
        .await
        .unwrap()
        .unwrap();
    assert!(futures_util::poll!(&mut delivery).is_pending());
    blocking.release();
    delivery.await.unwrap();
    assert!(blocking.completed.load(Ordering::Acquire));
    assert_eq!(control.usage().count(ControlKind::Task), 0);
}

#[tokio::test(flavor = "current_thread")]
async fn cancelled_terminal_observer_leaves_original_join_and_custody_in_close_inventory() {
    let control = CoreControl::default();
    let registry = HandleMetadataRegistry::new(&control).unwrap();
    let metadata = registry.admit(&control).unwrap().register();
    let blocking = Arc::new(BlockingMetadata::default());
    let _release = ReleaseOnDrop(blocking.clone());
    let dropped = Arc::new(AtomicUsize::new(0));
    let work = blocking.clone();
    let custody = FutureDropProbe(dropped.clone());
    let observer_metadata = metadata.clone();
    let observer = tokio::spawn(async move {
        observer_metadata
            .run(async move {
                let _custody = custody;
                work.run()
            })
            .await
    });
    blocking.entered().await;
    observer.abort();
    assert!(observer.await.unwrap_err().is_cancelled());
    assert_eq!(dropped.load(Ordering::Acquire), 0);
    assert_eq!(control.usage().count(ControlKind::Task), 1);
    {
        let close = registry.close();
        tokio::pin!(close);
        assert!(futures_util::poll!(&mut close).is_pending());
        assert!(!blocking.completed.load(Ordering::Acquire));
        // Cancelling close itself must also leave the same Join in custody.
    }
    let close = registry.close();
    tokio::pin!(close);
    assert!(futures_util::poll!(&mut close).is_pending());
    blocking.release();
    close.await.unwrap();
    assert!(blocking.completed.load(Ordering::Acquire));
    assert_eq!(dropped.load(Ordering::Acquire), 1);
    metadata.wait().await.unwrap();
    assert_eq!(control.usage().count(ControlKind::Task), 0);
}

#[tokio::test(flavor = "current_thread")]
async fn consumed_handoff_drop_publishes_original_work_without_an_async_observer() {
    let control = CoreControl::default();
    let registry = HandleMetadataRegistry::new(&control).unwrap();
    let metadata = registry.admit(&control).unwrap().register();
    let blocking = Arc::new(BlockingMetadata::default());
    let _release = ReleaseOnDrop(blocking.clone());
    let dropped = Arc::new(AtomicUsize::new(0));
    let custody = FutureDropProbe(dropped.clone());
    let work = blocking.clone();
    // This is the synchronous publication used by consumed custody Drop.
    metadata.queue(async move {
        let _custody = custody;
        work.run()
    });
    drop(metadata);
    blocking.entered().await;
    assert_eq!(dropped.load(Ordering::Acquire), 0);
    let close = registry.close();
    tokio::pin!(close);
    assert!(futures_util::poll!(&mut close).is_pending());
    blocking.release();
    close.await.unwrap();
    assert!(blocking.completed.load(Ordering::Acquire));
    assert_eq!(dropped.load(Ordering::Acquire), 1);
    assert_eq!(control.usage().count(ControlKind::Task), 0);
}

#[tokio::test(flavor = "current_thread")]
async fn close_seals_admission_and_joins_a_source_still_preparing_its_terminal_proposal() {
    let control = CoreControl::default();
    let registry = HandleMetadataRegistry::new(&control).unwrap();
    let admission = registry.admit(&control).unwrap();
    let close = registry.close();
    tokio::pin!(close);
    assert!(futures_util::poll!(&mut close).is_pending());
    assert!(registry.admit(&control).is_err());
    let metadata = admission.register();
    let blocking = Arc::new(BlockingMetadata::default());
    let _release = ReleaseOnDrop(blocking.clone());
    let work = blocking.clone();
    let delivery = metadata.run(async move { work.run() });
    tokio::pin!(delivery);
    assert!(futures_util::poll!(&mut delivery).is_pending());
    blocking.entered().await;
    assert!(futures_util::poll!(&mut close).is_pending());
    blocking.release();
    let (closed, delivered) = tokio::join!(close, delivery);
    closed.unwrap();
    delivered.unwrap();
    assert!(blocking.completed.load(Ordering::Acquire));
}

#[tokio::test(flavor = "current_thread")]
async fn admitted_terminal_runs_when_task_capacity_is_already_full() {
    let control = CoreControl::new(CoreControlLimits {
        max_tasks: 1,
        ..Default::default()
    })
    .unwrap();
    let registry = HandleMetadataRegistry::new(&control).unwrap();
    let metadata = registry.admit(&control).unwrap().register();
    assert_eq!(control.usage().count(ControlKind::Task), 1);
    assert!(control.reserve(ControlKind::Task, 1).is_err());
    let delivered = Arc::new(AtomicUsize::new(0));
    let actual_delivery = delivered.clone();
    metadata
        .run(async move {
            actual_delivery.fetch_add(1, Ordering::AcqRel);
            Ok(())
        })
        .await
        .unwrap();
    registry.close().await.unwrap();
    assert_eq!(delivered.load(Ordering::Acquire), 1);
    assert_eq!(control.usage().count(ControlKind::Task), 0);
    drop(metadata);
    drop(registry);
    assert_eq!(control.usage().count(ControlKind::Task), 0);
    assert_eq!(control.usage().bytes, 0);
}

#[tokio::test(flavor = "current_thread")]
async fn abandoned_unregistered_admission_wakes_close_without_minting_a_terminal() {
    let control = CoreControl::default();
    let registry = HandleMetadataRegistry::new(&control).unwrap();
    let admission = registry.admit(&control).unwrap();
    let close = registry.close();
    tokio::pin!(close);
    assert!(futures_util::poll!(&mut close).is_pending());
    drop(admission);
    close.await.unwrap();
}

#[tokio::test(flavor = "current_thread")]
async fn metadata_failure_and_lost_join_cannot_report_delivery_success() {
    let control = CoreControl::default();
    let registry = HandleMetadataRegistry::new(&control).unwrap();
    let metadata = registry.admit(&control).unwrap().register();
    let failure = metadata
        .run(async { Err(anyhow::Error::new(tobkiri_gate::GateError::Unavailable)) })
        .await
        .unwrap_err();
    assert!(matches!(
        failure.downcast_ref::<tobkiri_gate::GateError>(),
        Some(tobkiri_gate::GateError::Unavailable)
    ));
    assert!(registry.close().await.is_err());
    assert_eq!(control.usage().count(ControlKind::Task), 1);
    fn panic_metadata() -> Result<()> {
        panic!("injected metadata worker panic")
    }
    let failure = metadata.run(async { panic_metadata() }).await.unwrap_err();
    assert!(failure.downcast_ref::<StepError>().is_some_and(|error| {
        error.code == "unavailable" && error.message == "handle metadata task did not finish"
    }));
    assert!(registry.close().await.is_err());
    // A delivery retry uses the same admitted owner; it may succeed without
    // admitting another task or substituting physical/source evidence.
    metadata.run(async { Ok(()) }).await.unwrap();
    registry.close().await.unwrap();
    assert_eq!(control.usage().count(ControlKind::Task), 0);
}

#[tokio::test(flavor = "current_thread")]
async fn denied_task_funding_rejects_before_source_registration() {
    let control = CoreControl::new(CoreControlLimits {
        max_tasks: 0,
        ..Default::default()
    })
    .unwrap();
    let registry = HandleMetadataRegistry::new(&control).unwrap();
    assert!(registry.admit(&control).is_err());
    registry.close().await.unwrap();
    assert_eq!(control.usage().count(ControlKind::Task), 0);
}

#[test]
fn missing_runtime_does_not_admit_or_fund_metadata() {
    let control = CoreControl::default();
    assert!(HandleMetadata::prepare(&control).is_err());
    assert_eq!(control.usage().bytes, 0);
}

#[tokio::test(flavor = "current_thread")]
async fn source_cleanup_error_cannot_be_hidden_by_successful_terminal_delivery() {
    let control = CoreControl::default();
    let registry = HandleMetadataRegistry::new(&control).unwrap();
    let admission = registry.admit(&control).unwrap();
    admission
        .prepare_source_cleanup(&control, 16 * 1024)
        .unwrap();
    let metadata = admission.register();
    let original = metadata.clone();
    let delivered = Arc::new(AtomicUsize::new(0));
    let actual_delivery = delivered.clone();
    let dropped = Arc::new(AtomicUsize::new(0));
    let custody = FutureDropProbe(dropped.clone());
    metadata
        .spawn_source_cleanup(async move {
            let _custody = custody;
            // expire_handle can finish terminal delivery before returning its
            // original physical error. Shutdown must observe BOTH receipts.
            original
                .run(async move {
                    actual_delivery.fetch_add(1, Ordering::AcqRel);
                    Ok(())
                })
                .await?;
            Err(anyhow::Error::new(StepError::new(
                "unavailable",
                "injected physical failure",
            )))
        })
        .unwrap();
    let error = registry.close().await.unwrap_err();
    assert_eq!(error.code, "unavailable");
    assert_eq!(
        error.message,
        "source cleanup reaper did not complete successfully"
    );
    metadata.wait().await.unwrap();
    assert_eq!(delivered.load(Ordering::Acquire), 1);
    assert_eq!(dropped.load(Ordering::Acquire), 1);
    // Physical uncertainty remains quarantined/funded after metadata ACK.
    assert_eq!(control.usage().count(ControlKind::Task), 1);
    assert!(registry.close().await.is_err());
}

#[tokio::test(flavor = "current_thread")]
async fn cancelling_close_keeps_the_original_source_reaper_join_and_capture() {
    let control = CoreControl::default();
    let registry = HandleMetadataRegistry::new(&control).unwrap();
    let admission = registry.admit(&control).unwrap();
    admission
        .prepare_source_cleanup(&control, 16 * 1024)
        .unwrap();
    let metadata = admission.register();
    metadata.run(async { Ok(()) }).await.unwrap();
    let (release, released) = tokio::sync::oneshot::channel();
    let dropped = Arc::new(AtomicUsize::new(0));
    let custody = FutureDropProbe(dropped.clone());
    metadata
        .spawn_source_cleanup(async move {
            let _custody = custody;
            released
                .await
                .map_err(|_| anyhow::anyhow!("source release lost"))?;
            Ok(())
        })
        .unwrap();
    {
        let close = registry.close();
        tokio::pin!(close);
        assert!(futures_util::poll!(&mut close).is_pending());
    }
    assert_eq!(dropped.load(Ordering::Acquire), 0);
    assert_eq!(control.usage().count(ControlKind::Task), 1);
    release.send(()).unwrap();
    registry.close().await.unwrap();
    assert_eq!(dropped.load(Ordering::Acquire), 1);
    assert_eq!(control.usage().count(ControlKind::Task), 0);
}

#[tokio::test(flavor = "current_thread")]
async fn prepaid_source_reaper_and_metadata_complete_at_full_task_capacity() {
    let control = CoreControl::new(CoreControlLimits {
        max_tasks: 2,
        ..Default::default()
    })
    .unwrap();
    let registry = HandleMetadataRegistry::new(&control).unwrap();
    let admission = registry.admit(&control).unwrap();
    admission
        .prepare_source_cleanup(&control, 16 * 1024)
        .unwrap();
    let metadata = admission.register();
    assert_eq!(control.usage().count(ControlKind::Task), 2);
    assert!(control.reserve(ControlKind::Task, 1).is_err());
    let original = metadata.clone();
    metadata
        .spawn_source_cleanup(async move { original.run(async { Ok(()) }).await })
        .unwrap();
    registry.close().await.unwrap();
    assert_eq!(control.usage().count(ControlKind::Task), 0);
}

#[tokio::test(flavor = "current_thread")]
async fn unpublished_source_refunds_only_its_prepared_unstarted_reaper() {
    let control = CoreControl::default();
    let registry = HandleMetadataRegistry::new(&control).unwrap();
    let admission = registry.admit(&control).unwrap();
    admission
        .prepare_source_cleanup(&control, 16 * 1024)
        .unwrap();
    assert_eq!(control.usage().count(ControlKind::Task), 2);
    let close = registry.close();
    tokio::pin!(close);
    assert!(futures_util::poll!(&mut close).is_pending());
    drop(admission);
    close.await.unwrap();
    assert_eq!(control.usage().count(ControlKind::Task), 0);
}

#[test]
fn completed_eof_requires_original_physical_cleanup_as_well_as_delivery() {
    let mut receipt = crate::handles::HandleCloseReceipt {
        delivered: true,
        outcome: Some(Outcome::Succeeded),
        ..Default::default()
    };
    assert!(!completed_eof_cleanup(&receipt, false));
    receipt.cleanup_done = true;
    assert!(completed_eof_cleanup(&receipt, false));
    assert!(!completed_eof_cleanup(&receipt, true));
    receipt.deadline_settlement = true;
    assert!(!completed_eof_cleanup(&receipt, false));
    receipt.deadline_settlement = false;
    receipt.delivered = false;
    assert!(!completed_eof_cleanup(&receipt, false));
}

#[test]
fn reaper_report_requires_original_physical_receipt_and_preserves_reported_failure() {
    let mut receipt = crate::handles::HandleCloseReceipt {
        delivered: true,
        outcome: Some(Outcome::Cancelled),
        ..Default::default()
    };
    // A terminal winner/ACK is never physical completion evidence.
    assert!(source_cleanup_report(&receipt, true).is_none());
    receipt.cleanup_done = true;
    assert!(source_cleanup_report(&receipt, true).is_none());
    receipt.cleanup_started = true;
    assert!(
        source_cleanup_report(&receipt, true)
            .unwrap()
            .reported_failure()
    );
    assert_eq!(
        source_cleanup_report(&receipt, true).unwrap().observation(),
        SourceCleanupObservation::SourceReportedFailure,
    );
    assert!(
        !source_cleanup_report(&receipt, false)
            .unwrap()
            .reported_failure()
    );
    receipt.cleanup_error = Some("original physical completion unknown");
    assert!(source_cleanup_report(&receipt, true).is_none());
}

#[tokio::test(flavor = "current_thread")]
async fn repaired_metadata_joins_same_physical_report_without_replaying_cleanup() {
    let control = CoreControl::new(CoreControlLimits {
        max_tasks: 2,
        ..Default::default()
    })
    .unwrap();
    let registry = HandleMetadataRegistry::new(&control).unwrap();
    let admission = registry.admit(&control).unwrap();
    admission
        .prepare_source_cleanup(&control, 16 * 1024)
        .unwrap();
    let metadata = admission.register();
    let original = metadata.clone();
    let cleanup_calls = Arc::new(AtomicUsize::new(0));
    let actual_cleanup_calls = cleanup_calls.clone();
    // Unit-level stage classification only. The existing Core audit-fault
    // regressions exercise this report from the actual source cleanup path.
    let report = source_cleanup_report(
        &crate::handles::HandleCloseReceipt {
            cleanup_started: true,
            cleanup_done: true,
            ..Default::default()
        },
        true,
    )
    .unwrap();
    assert_eq!(
        report.observation(),
        SourceCleanupObservation::TerminalUnacknowledged
    );
    metadata
        .spawn_reported_source_cleanup(async move {
            actual_cleanup_calls.fetch_add(1, Ordering::AcqRel);
            assert!(
                original
                    .run(async { Err(anyhow::Error::new(tobkiri_gate::GateError::Unavailable)) })
                    .await
                    .is_err()
            );
            Ok(Some(report))
        })
        .unwrap();
    assert!(registry.close().await.is_err());
    assert!(
        metadata
            .wait_source_cleanup()
            .await
            .unwrap()
            .unwrap()
            .reported_failure()
    );
    assert_eq!(cleanup_calls.load(Ordering::Acquire), 1);
    assert_eq!(control.usage().count(ControlKind::Task), 1);
    // Only delivery retries, on the same already-funded metadata owner.
    metadata.run(async { Ok(()) }).await.unwrap();
    registry.close().await.unwrap();
    assert_eq!(
        metadata
            .wait_source_cleanup()
            .await
            .unwrap()
            .unwrap()
            .observation(),
        SourceCleanupObservation::TerminalUnacknowledged,
    );
    assert!(
        metadata
            .wait_source_cleanup()
            .await
            .unwrap()
            .unwrap()
            .reported_failure()
    );
    assert_eq!(cleanup_calls.load(Ordering::Acquire), 1);
    assert_eq!(control.usage().count(ControlKind::Task), 0);
}

#[tokio::test(flavor = "current_thread")]
async fn metadata_ack_cannot_repair_a_panicked_source_reaper_join() {
    let control = CoreControl::default();
    let registry = HandleMetadataRegistry::new(&control).unwrap();
    let admission = registry.admit(&control).unwrap();
    admission
        .prepare_source_cleanup(&control, 16 * 1024)
        .unwrap();
    let metadata = admission.register();
    metadata.run(async { Ok(()) }).await.unwrap();
    fn panic_source_cleanup() -> Result<()> {
        panic!("injected physical reaper panic")
    }
    metadata
        .spawn_source_cleanup(async { panic_source_cleanup() })
        .unwrap();
    assert!(registry.close().await.is_err());
    metadata.run(async { Ok(()) }).await.unwrap();
    assert!(registry.close().await.is_err());
    assert!(metadata.wait_source_cleanup().await.is_err());
    assert_eq!(control.usage().count(ControlKind::Task), 1);
}

#[tokio::test(flavor = "current_thread")]
async fn metadata_ack_cannot_replace_a_submitted_reapers_missing_physical_report() {
    let control = CoreControl::default();
    let registry = HandleMetadataRegistry::new(&control).unwrap();
    let admission = registry.admit(&control).unwrap();
    admission
        .prepare_source_cleanup(&control, 16 * 1024)
        .unwrap();
    let metadata = admission.register();
    metadata.run(async { Ok(()) }).await.unwrap();
    metadata
        .spawn_reported_source_cleanup(async { Ok(None) })
        .unwrap();
    assert!(registry.close().await.is_err());
    assert!(metadata.wait_source_cleanup().await.is_err());
    assert!(registry.close().await.is_err());
    assert_eq!(control.usage().count(ControlKind::Task), 1);
}
