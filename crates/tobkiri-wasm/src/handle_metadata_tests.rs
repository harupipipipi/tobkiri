//! The same prepaid owner and sealed inventory used by actual handle terminal
//! delivery. Core's source tests exercise Grant/lease and acknowledged outcome.
use super::*;
use crate::handle_metadata::{
    HandleMetadata, HandleMetadataRegistry, QueuedMetadata, RetainedMetadata,
};
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
    // Ordinary returned Err preserves the existing explicit repair behavior.
    metadata.run(async { Ok(()) }).await.unwrap();
    registry.close().await.unwrap();
    assert_eq!(control.usage().count(ControlKind::Task), 0);
}

#[tokio::test(flavor = "current_thread")]
async fn panicked_metadata_join_remains_unknown_even_with_a_new_successful_future() {
    let control = CoreControl::default();
    let registry = HandleMetadataRegistry::new(&control).unwrap();
    let metadata = registry.admit(&control).unwrap().register();
    fn panic_metadata() -> Result<()> {
        panic!("injected metadata worker panic")
    }
    let failure = metadata.run(async { panic_metadata() }).await.unwrap_err();
    assert!(failure.downcast_ref::<StepError>().is_some_and(|error| {
        error.code == "unavailable" && error.message == "handle metadata task did not finish"
    }));
    assert!(registry.close().await.is_err());
    assert!(metadata.run(async { Ok(()) }).await.is_err());
    assert!(registry.close().await.is_err());
    assert_eq!(control.usage().count(ControlKind::Task), 1);
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
                    .run(async {
                        Err(anyhow::Error::new(tobkiri_gate::GateError::Persistence(
                            "injected delivery persistence fault".into(),
                        )))
                    })
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

#[derive(Default)]
struct RetryDeliveryState {
    attempts: AtomicUsize,
    winner: AtomicUsize,
    mode: AtomicUsize,
    outbox_repaired: AtomicBool,
    acknowledged: AtomicBool,
    delivered: AtomicBool,
    blocking: Mutex<Option<Arc<BlockingMetadata>>>,
    retirement: Mutex<Option<RetirementProbe>>,
}
struct RetirementProbe {
    blocking: Arc<BlockingMetadata>,
    registry: std::sync::Weak<HandleMetadataRegistry>,
    guards_free: Arc<AtomicBool>,
}
struct RetryDelivery {
    state: Arc<RetryDeliveryState>,
    proposal: usize,
    // Mirrors the real capsule's original lease -> metadata ownership edge.
    _original_owner: Arc<HandleMetadata>,
    owned: Vec<u8>,
    dropped: Arc<AtomicUsize>,
}
impl Drop for RetryDelivery {
    fn drop(&mut self) {
        let retirement = self.state.retirement.lock().unwrap().take();
        if let Some(retirement) = retirement {
            let guards_free = self._original_owner.retirement_guards_available()
                && retirement
                    .registry
                    .upgrade()
                    .is_some_and(|registry| registry.guard_available());
            retirement.guards_free.store(guards_free, Ordering::Release);
            retirement.blocking.run().unwrap();
        }
        self.dropped.fetch_add(1, Ordering::AcqRel);
    }
}
impl RetainedMetadata for RetryDelivery {
    fn future(self: Arc<Self>) -> QueuedMetadata {
        Box::pin(retry_delivery_attempt(self))
    }
    fn future_bytes(&self) -> usize {
        fn size<F: std::future::Future>(_: fn(Arc<RetryDelivery>) -> F) -> usize {
            std::mem::size_of::<F>()
        }
        size(retry_delivery_attempt)
    }
    fn owned_heap_bytes(&self) -> usize {
        self.owned.capacity()
    }
}
async fn retry_delivery_attempt(delivery: Arc<RetryDelivery>) -> Result<()> {
    let state = &delivery.state;
    state.attempts.fetch_add(1, Ordering::AcqRel);
    let _ =
        state
            .winner
            .compare_exchange(0, delivery.proposal, Ordering::AcqRel, Ordering::Acquire);
    let blocking = state.blocking.lock().unwrap().clone();
    if let Some(blocking) = blocking {
        blocking.run()?;
    }
    match state.mode.load(Ordering::Acquire) {
        0 => {
            return Err(tobkiri_gate::GateError::Persistence(
                "original terminal delivery failed".into(),
            )
            .into());
        }
        2 => return Err(tobkiri_gate::GateError::Unavailable.into()),
        3 => {
            return Err(tobkiri_gate::GateError::Integrity(
                tobkiri_gate::IntegrityFaultCode::AuditChain,
            )
            .into());
        }
        4 => panic!("injected retained delivery panic"),
        5 => {
            return Err(StepError::new(
                "allocation-unavailable",
                "same frame ACK capacity unavailable",
            )
            .into());
        }
        _ => {}
    }
    if !state.outbox_repaired.load(Ordering::Acquire) || !state.acknowledged.load(Ordering::Acquire)
    {
        return Err(tobkiri_gate::GateError::Persistence(
            "same terminal acknowledgement is missing".into(),
        )
        .into());
    }
    state.delivered.store(true, Ordering::Release);
    Ok(())
}
fn retry_delivery(
    metadata: &Arc<HandleMetadata>,
    state: &Arc<RetryDeliveryState>,
    proposal: usize,
    dropped: &Arc<AtomicUsize>,
) -> RetryDelivery {
    RetryDelivery {
        state: state.clone(),
        proposal,
        _original_owner: metadata.clone(),
        owned: Vec::new(),
        dropped: dropped.clone(),
    }
}

#[tokio::test(flavor = "current_thread")]
async fn close_retries_original_terminal_delivery_without_external_metadata_run() {
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
    let state = Arc::new(RetryDeliveryState::default());
    let dropped = Arc::new(AtomicUsize::new(0));
    assert!(
        metadata
            .run_delivery(retry_delivery(&metadata, &state, 1, &dropped))
            .await
            .is_err()
    );
    assert_eq!(state.attempts.load(Ordering::Acquire), 1);
    let cleanup_calls = Arc::new(AtomicUsize::new(0));
    let physical_calls = cleanup_calls.clone();
    let report = source_cleanup_report(
        &crate::handles::HandleCloseReceipt {
            cleanup_started: true,
            cleanup_done: true,
            ..Default::default()
        },
        true,
    )
    .unwrap();
    metadata
        .spawn_reported_source_cleanup(async move {
            physical_calls.fetch_add(1, Ordering::AcqRel);
            Ok(Some(report))
        })
        .unwrap();
    // Close itself owns the next attempt. One failed observer cannot spin.
    assert!(registry.close().await.is_err());
    assert_eq!(state.attempts.load(Ordering::Acquire), 2);
    assert_eq!(cleanup_calls.load(Ordering::Acquire), 1);
    assert_eq!(control.usage().count(ControlKind::Task), 1);
    // A new proposal cannot replace the retained original winner/capsule.
    let losing_drop = Arc::new(AtomicUsize::new(0));
    metadata
        .queue_delivery(retry_delivery(&metadata, &state, 2, &losing_drop))
        .unwrap();
    assert_eq!(losing_drop.load(Ordering::Acquire), 1);
    state.mode.store(1, Ordering::Release);
    state.outbox_repaired.store(true, Ordering::Release);
    assert!(registry.close().await.is_err());
    assert_eq!(state.attempts.load(Ordering::Acquire), 3);
    assert!(!state.delivered.load(Ordering::Acquire));
    // Only the same delivery job's actual acknowledgement permits success.
    state.acknowledged.store(true, Ordering::Release);
    registry.close().await.unwrap();
    assert_eq!(state.attempts.load(Ordering::Acquire), 4);
    assert_eq!(state.winner.load(Ordering::Acquire), 1);
    assert!(state.delivered.load(Ordering::Acquire));
    assert_eq!(cleanup_calls.load(Ordering::Acquire), 1);
    assert_eq!(
        metadata
            .wait_source_cleanup()
            .await
            .unwrap()
            .unwrap()
            .observation(),
        SourceCleanupObservation::TerminalUnacknowledged
    );
    assert_eq!(dropped.load(Ordering::Acquire), 1);
    assert_eq!(control.usage().count(ControlKind::Task), 0);
    drop(metadata);
    drop(registry);
    assert_eq!(control.usage().bytes, 0);
}

#[tokio::test(flavor = "current_thread")]
async fn concurrent_waiters_share_failure_and_cancelled_close_keeps_the_one_retry_join() {
    let control = CoreControl::new(CoreControlLimits {
        max_tasks: 1,
        ..Default::default()
    })
    .unwrap();
    let registry = Arc::new(HandleMetadataRegistry::new(&control).unwrap());
    let metadata = registry.admit(&control).unwrap().register();
    let state = Arc::new(RetryDeliveryState::default());
    let first_blocking = Arc::new(BlockingMetadata::default());
    let _first_release = ReleaseOnDrop(first_blocking.clone());
    *state.blocking.lock().unwrap() = Some(first_blocking.clone());
    let dropped = Arc::new(AtomicUsize::new(0));
    let observer_metadata = metadata.clone();
    let delivery = retry_delivery(&metadata, &state, 1, &dropped);
    let observer = tokio::spawn(async move { observer_metadata.run_delivery(delivery).await });
    first_blocking.entered().await;
    let concurrent_metadata = metadata.clone();
    let (entered, saw_entered) = tokio::sync::oneshot::channel();
    let concurrent = tokio::spawn(async move {
        entered.send(()).unwrap();
        concurrent_metadata.wait().await
    });
    saw_entered.await.unwrap();
    tokio::task::yield_now().await;
    first_blocking.release();
    assert!(observer.await.unwrap().is_err());
    assert!(concurrent.await.unwrap().is_err());
    assert_eq!(state.attempts.load(Ordering::Acquire), 1);
    let retry_blocking = Arc::new(BlockingMetadata::default());
    let _retry_release = ReleaseOnDrop(retry_blocking.clone());
    *state.blocking.lock().unwrap() = Some(retry_blocking.clone());
    state.mode.store(1, Ordering::Release);
    state.outbox_repaired.store(true, Ordering::Release);
    state.acknowledged.store(true, Ordering::Release);
    let close_registry = registry.clone();
    let close = tokio::spawn(async move { close_registry.close().await });
    retry_blocking.entered().await;
    close.abort();
    assert!(close.await.unwrap_err().is_cancelled());
    assert_eq!(state.attempts.load(Ordering::Acquire), 2);
    assert_eq!(dropped.load(Ordering::Acquire), 0);
    assert_eq!(control.usage().count(ControlKind::Task), 1);
    let close = registry.close();
    tokio::pin!(close);
    assert!(futures_util::poll!(&mut close).is_pending());
    assert_eq!(state.attempts.load(Ordering::Acquire), 2);
    retry_blocking.release();
    close.await.unwrap();
    assert_eq!(dropped.load(Ordering::Acquire), 1);
    assert_eq!(control.usage().count(ControlKind::Task), 0);
}

#[tokio::test(flavor = "current_thread")]
async fn ordinary_errors_retry_the_same_capsule_but_panicked_attempt_stays_unknown() {
    for mode in [2, 3, 4, 5] {
        let control = CoreControl::default();
        let registry = HandleMetadataRegistry::new(&control).unwrap();
        let metadata = registry.admit(&control).unwrap().register();
        let weak_metadata = Arc::downgrade(&metadata);
        let state = Arc::new(RetryDeliveryState::default());
        state.mode.store(mode, Ordering::Release);
        let dropped = Arc::new(AtomicUsize::new(0));
        assert!(
            metadata
                .run_delivery(retry_delivery(&metadata, &state, 1, &dropped))
                .await
                .is_err()
        );
        state.mode.store(1, Ordering::Release);
        state.outbox_repaired.store(true, Ordering::Release);
        state.acknowledged.store(true, Ordering::Release);
        if mode == 4 {
            assert!(registry.close().await.is_err());
            assert!(registry.close().await.is_err());
            assert_eq!(state.attempts.load(Ordering::Acquire), 1);
            assert_eq!(control.usage().count(ControlKind::Task), 1);
            assert_eq!(dropped.load(Ordering::Acquire), 0);
        } else {
            // No external run/new future: original Gate and non-Gate ordinary
            // errors retain the old repeated-finish behavior, via close itself.
            registry.close().await.unwrap();
            assert_eq!(state.attempts.load(Ordering::Acquire), 2);
            assert_eq!(state.winner.load(Ordering::Acquire), 1);
            assert_eq!(control.usage().count(ControlKind::Task), 0);
            assert_eq!(dropped.load(Ordering::Acquire), 1);
        }
        drop(metadata);
        drop(registry);
        assert_eq!(dropped.load(Ordering::Acquire), 1);
        assert!(weak_metadata.upgrade().is_none());
        assert_eq!(control.usage().bytes, 0);
    }
}

#[tokio::test(flavor = "current_thread")]
async fn combined_retained_capsule_and_future_bound_rejects_before_delivery() {
    let control = CoreControl::default();
    let registry = HandleMetadataRegistry::new(&control).unwrap();
    let metadata = registry.admit(&control).unwrap().register();
    let state = Arc::new(RetryDeliveryState::default());
    let dropped = Arc::new(AtomicUsize::new(0));
    let mut delivery = retry_delivery(&metadata, &state, 1, &dropped);
    // Heap alone equals the old future ceiling; capsule + future makes the
    // total exceed it. No separate reservation/ceiling increase is allowed.
    delivery.owned = vec![0; 64 * 1024];
    assert!(metadata.queue_delivery(delivery).is_err());
    // A later valid capsule cannot repair the rejected consumed publication.
    assert!(
        metadata
            .queue_delivery(retry_delivery(&metadata, &state, 2, &dropped))
            .is_err()
    );
    assert!(registry.close().await.is_err());
    assert_eq!(state.attempts.load(Ordering::Acquire), 0);
    assert_eq!(dropped.load(Ordering::Acquire), 2);
    assert_eq!(control.usage().count(ControlKind::Task), 1);
}

#[tokio::test(flavor = "current_thread")]
async fn earlier_observer_cannot_join_a_later_replacement_attempt() {
    for replacement_succeeds in [false, true] {
        let control = CoreControl::default();
        let registry = HandleMetadataRegistry::new(&control).unwrap();
        let metadata = registry.admit(&control).unwrap().register();
        let state = Arc::new(RetryDeliveryState::default());
        let dropped = Arc::new(AtomicUsize::new(0));
        // Model a waiter paused after capturing the admission's original
        // failure stamp, before acquiring the state guard.
        let earlier_observation = 0;
        assert!(
            metadata
                .run_delivery(retry_delivery(&metadata, &state, 1, &dropped))
                .await
                .is_err()
        );
        let blocking = Arc::new(BlockingMetadata::default());
        let _release = ReleaseOnDrop(blocking.clone());
        *state.blocking.lock().unwrap() = Some(blocking.clone());
        if replacement_succeeds {
            state.mode.store(1, Ordering::Release);
            state.outbox_repaired.store(true, Ordering::Release);
            state.acknowledged.store(true, Ordering::Release);
        }
        let retry_metadata = metadata.clone();
        let retry = tokio::spawn(async move { retry_metadata.wait().await });
        blocking.entered().await;
        let earlier = metadata.wait_after(earlier_observation);
        tokio::pin!(earlier);
        // The retry holds the state guard across the actual Join. Cancelling
        // this stale observer does not take it; after completion it must still
        // return the older failure, regardless of the retry's result.
        assert!(futures_util::poll!(&mut earlier).is_pending());
        blocking.release();
        assert_eq!(retry.await.unwrap().is_ok(), replacement_succeeds);
        assert!(earlier.await.is_err());
        assert_eq!(state.attempts.load(Ordering::Acquire), 2);
        if replacement_succeeds {
            registry.close().await.unwrap();
        }
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn success_waits_for_capsule_retirement_outside_guards_before_refunding_task() {
    let control = CoreControl::new(CoreControlLimits {
        max_tasks: 1,
        ..Default::default()
    })
    .unwrap();
    let registry = Arc::new(HandleMetadataRegistry::new(&control).unwrap());
    let metadata = registry.admit(&control).unwrap().register();
    let state = Arc::new(RetryDeliveryState::default());
    state.mode.store(1, Ordering::Release);
    state.outbox_repaired.store(true, Ordering::Release);
    state.acknowledged.store(true, Ordering::Release);
    let dropped = Arc::new(AtomicUsize::new(0));
    let retirement = Arc::new(BlockingMetadata::default());
    let _release = ReleaseOnDrop(retirement.clone());
    let guards_free = Arc::new(AtomicBool::new(false));
    *state.retirement.lock().unwrap() = Some(RetirementProbe {
        blocking: retirement.clone(),
        registry: Arc::downgrade(&registry),
        guards_free: guards_free.clone(),
    });
    metadata
        .queue_delivery(retry_delivery(&metadata, &state, 1, &dropped))
        .unwrap();
    let close_registry = registry.clone();
    let close = tokio::spawn(async move { close_registry.close().await });
    retirement.entered().await;
    assert!(guards_free.load(Ordering::Acquire));
    assert_eq!(control.usage().count(ControlKind::Task), 1);
    let losing_drop = Arc::new(AtomicUsize::new(0));
    metadata
        .queue_delivery(retry_delivery(&metadata, &state, 2, &losing_drop))
        .unwrap();
    assert_eq!(losing_drop.load(Ordering::Acquire), 1);
    let waiter = metadata.wait();
    tokio::pin!(waiter);
    assert!(futures_util::poll!(&mut waiter).is_pending());
    assert!(!close.is_finished());
    assert_eq!(state.attempts.load(Ordering::Acquire), 1);
    retirement.release();
    close.await.unwrap().unwrap();
    waiter.await.unwrap();
    assert_eq!(dropped.load(Ordering::Acquire), 1);
    assert_eq!(state.winner.load(Ordering::Acquire), 1);
    assert_eq!(control.usage().count(ControlKind::Task), 0);
}

#[tokio::test(flavor = "current_thread")]
async fn repeated_finish_stays_successful_after_admission_prunes_the_retired_sidecar() {
    let control = CoreControl::new(CoreControlLimits {
        max_tasks: 1,
        ..Default::default()
    })
    .unwrap();
    let registry = HandleMetadataRegistry::new(&control).unwrap();
    let metadata = registry.admit(&control).unwrap().register();
    let state = Arc::new(RetryDeliveryState::default());
    state.mode.store(1, Ordering::Release);
    state.outbox_repaired.store(true, Ordering::Release);
    state.acknowledged.store(true, Ordering::Release);
    let dropped = Arc::new(AtomicUsize::new(0));
    metadata
        .run_delivery(retry_delivery(&metadata, &state, 1, &dropped))
        .await
        .unwrap();
    assert_eq!(dropped.load(Ordering::Acquire), 1);
    // Prune the completed original custody while its old lease/metadata lives.
    let next = registry.admit(&control).unwrap();
    drop(next);
    metadata
        .run_delivery(retry_delivery(&metadata, &state, 2, &dropped))
        .await
        .unwrap();
    registry.close().await.unwrap();
    assert_eq!(state.attempts.load(Ordering::Acquire), 1);
    assert_eq!(state.winner.load(Ordering::Acquire), 1);
    assert_eq!(dropped.load(Ordering::Acquire), 2);
    assert_eq!(control.usage().count(ControlKind::Task), 0);
}
