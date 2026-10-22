//! The same prepaid owner and sealed inventory used by actual handle terminal
//! delivery. Core's source tests exercise Grant/lease and acknowledged outcome.
use super::*;
use crate::handle_metadata::{
    FailureCategory, FailureStage, HandleMetadata, HandleMetadataRegistry, QueuedMetadata,
    RetainedMetadata, metadata_failure,
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
    let source = metadata_failure(&failure).expect("same typed Gate failure receipt");
    assert_eq!(source.category, FailureCategory::Unavailable);
    assert_eq!(source._stage, FailureStage::Legacy);
    assert_eq!(source.audit_code(), "denied");
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
        // Pin actual participation before the first producer. Advancing the
        // inventory generation cannot substitute its later Result.
        let earlier_observation = metadata.pin_generation();
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
        let earlier = metadata
            .wait_pinned(earlier_observation.clone())
            .await
            .unwrap_err();
        assert_eq!(
            metadata_failure(&earlier).unwrap().category,
            FailureCategory::Persistence
        );
        assert!(
            earlier
                .to_string()
                .contains("original terminal delivery failed")
        );
        blocking.release();
        assert_eq!(retry.await.unwrap().is_ok(), replacement_succeeds);
        let still_earlier = metadata.wait_pinned(earlier_observation).await.unwrap_err();
        assert_eq!(still_earlier.to_string(), earlier.to_string());
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

#[tokio::test(flavor = "current_thread")]
async fn standing_reaper_and_caller_share_the_original_failure_and_old_result_survives_repair() {
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
    let blocked = Arc::new(BlockingMetadata::default());
    let _release = ReleaseOnDrop(blocked.clone());
    *state.blocking.lock().unwrap() = Some(blocked.clone());
    let old = metadata.pin_generation();
    let standing_metadata = metadata.clone();
    let standing = tokio::spawn(async move { standing_metadata.observe().await });
    tokio::task::yield_now().await;
    // Same publication/standing observer wiring as reap_captured_source.
    metadata
        .queue_delivery(retry_delivery(&metadata, &state, 1, &dropped))
        .unwrap();
    blocked.entered().await;
    let caller_metadata = metadata.clone();
    let caller_generation = old.clone();
    let caller = tokio::spawn(async move { caller_metadata.wait_pinned(caller_generation).await });
    tokio::task::yield_now().await;
    blocked.release();
    let reaper_failure = standing.await.unwrap().unwrap_err();
    let caller_failure = caller.await.unwrap().unwrap_err();
    for error in [&reaper_failure, &caller_failure] {
        let failure = metadata_failure(error).unwrap();
        assert_eq!(failure.category, FailureCategory::Persistence);
        assert_eq!(failure.audit_code(), "denied");
        assert!(
            failure
                .message()
                .contains("original terminal delivery failed")
        );
    }
    assert_eq!(reaper_failure.to_string(), caller_failure.to_string());
    assert_eq!(state.attempts.load(Ordering::Acquire), 1);
    // Neither notification nor the reaper's subsequent finish requests retry.
    assert!(metadata.observe().await.is_err());
    assert!(
        metadata
            .run_delivery_mode(retry_delivery(&metadata, &state, 2, &dropped), false)
            .await
            .is_err()
    );
    assert_eq!(state.attempts.load(Ordering::Acquire), 1);
    let cleanup_calls = Arc::new(AtomicUsize::new(0));
    let physical = cleanup_calls.clone();
    metadata
        .spawn_source_cleanup(async move {
            physical.fetch_add(1, Ordering::AcqRel);
            Ok(())
        })
        .unwrap();
    state.mode.store(1, Ordering::Release);
    state.outbox_repaired.store(true, Ordering::Release);
    state.acknowledged.store(true, Ordering::Release);
    registry.close().await.unwrap();
    assert_eq!(state.attempts.load(Ordering::Acquire), 2);
    assert_eq!(state.winner.load(Ordering::Acquire), 1);
    assert_eq!(cleanup_calls.load(Ordering::Acquire), 1);
    assert_eq!(control.usage().count(ControlKind::Task), 0);
    let old_failure = metadata.wait_pinned(old).await.unwrap_err();
    assert_eq!(old_failure.to_string(), caller_failure.to_string());
    assert_eq!(
        metadata_failure(&old_failure).unwrap().category,
        FailureCategory::Persistence
    );
}

#[tokio::test(flavor = "current_thread")]
async fn two_held_result_generations_defer_retry_and_release_the_original_frame_last() {
    let control = CoreControl::new(CoreControlLimits {
        max_tasks: 1,
        ..Default::default()
    })
    .unwrap();
    let registry = HandleMetadataRegistry::new(&control).unwrap();
    let metadata = registry.admit(&control).unwrap().register();
    let weak_metadata = Arc::downgrade(&metadata);
    let state = Arc::new(RetryDeliveryState::default());
    let dropped = Arc::new(AtomicUsize::new(0));
    let first = metadata
        .run_delivery(retry_delivery(&metadata, &state, 1, &dropped))
        .await
        .unwrap_err();
    let second = metadata.wait().await.unwrap_err();
    let deferred = metadata.wait().await.unwrap_err();
    let limit = deferred
        .downcast_ref::<StepError>()
        .expect("separate retry admission limit");
    assert_eq!(limit.code, "limit-exceeded");
    assert_eq!(state.attempts.load(Ordering::Acquire), 2);
    assert_eq!(
        metadata_failure(&first).unwrap().category,
        FailureCategory::Persistence
    );
    assert_eq!(
        metadata_failure(&second).unwrap().category,
        FailureCategory::Persistence
    );
    drop(first);
    state.mode.store(1, Ordering::Release);
    state.outbox_repaired.store(true, Ordering::Release);
    state.acknowledged.store(true, Ordering::Release);
    registry.close().await.unwrap();
    assert_eq!(state.attempts.load(Ordering::Acquire), 3);
    assert_eq!(control.usage().count(ControlKind::Task), 0);
    drop(metadata);
    drop(registry);
    assert!(weak_metadata.upgrade().is_none());
    // The old immutable error has no metadata/Host backlink, but keeps the
    // SAME prepaid Frame receipt while its bounded bytes remain observable.
    assert_eq!(control.usage().count(ControlKind::Frame), 1);
    assert!(
        second
            .to_string()
            .contains("original terminal delivery failed")
    );
    drop(second);
    assert_eq!(control.usage().count(ControlKind::Frame), 0);
    assert_eq!(control.usage().bytes, 0);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn replacement_is_atomic_for_a_concurrent_observer_after_closed_cell_retirement() {
    let control = CoreControl::default();
    let registry = HandleMetadataRegistry::new(&control).unwrap();
    let metadata = registry.admit(&control).unwrap().register();
    let state = Arc::new(RetryDeliveryState::default());
    let dropped = Arc::new(AtomicUsize::new(0));
    let first = metadata
        .run_delivery(retry_delivery(&metadata, &state, 1, &dropped))
        .await
        .unwrap_err();
    // Allow the completed current cell itself to be reused. The pause is
    // after its Arc AND inventory Weak have been retired, before allocation.
    drop(first);
    let transition = Arc::new(BlockingMetadata::default());
    let _release = ReleaseOnDrop(transition.clone());
    let replacing = metadata.clone();
    let paused = transition.clone();
    let replacement = tokio::task::spawn_blocking(move || {
        replacing.pin_retry_after_retirement(|| paused.run().unwrap())
    });
    transition.entered().await;
    assert!(
        !metadata.retirement_guards_available(),
        "the replacement state guard must cover retirement through publication"
    );
    let observing = metadata.clone();
    let entered = Arc::new(std::sync::Barrier::new(2));
    let observer_entered = entered.clone();
    let (sent, mut received) = tokio::sync::oneshot::channel();
    let observer = tokio::task::spawn_blocking(move || {
        observer_entered.wait();
        let attempt = observing.pin_generation();
        sent.send(attempt).unwrap();
    });
    entered.wait();
    // The observer must wait on the state guard; it cannot report a false
    // capacity error from a transient empty current slot.
    assert!(matches!(
        received.try_recv(),
        Err(tokio::sync::oneshot::error::TryRecvError::Empty)
    ));
    transition.release();
    let replacement = replacement.await.unwrap().unwrap();
    let observed = received.await.unwrap();
    observer.await.unwrap();
    assert!(Arc::ptr_eq(&replacement, &observed));
    state.mode.store(1, Ordering::Release);
    state.outbox_repaired.store(true, Ordering::Release);
    state.acknowledged.store(true, Ordering::Release);
    metadata.wait_pinned(observed).await.unwrap();
    assert_eq!(state.attempts.load(Ordering::Acquire), 2);
    assert_eq!(control.usage().count(ControlKind::Task), 0);
}

#[tokio::test(flavor = "current_thread")]
async fn concurrent_inventory_drop_retires_final_custody_after_the_state_guard() {
    struct GuardedDelivery {
        metadata: Arc<HandleMetadata>,
        dropped: Arc<AtomicBool>,
        guards_free: Arc<AtomicBool>,
    }
    impl GuardedDelivery {
        async fn fail() -> Result<()> {
            Err(tobkiri_gate::GateError::Unavailable.into())
        }
    }
    impl Drop for GuardedDelivery {
        fn drop(&mut self) {
            self.guards_free.store(
                self.metadata.retirement_guards_available(),
                Ordering::Release,
            );
            self.dropped.store(true, Ordering::Release);
        }
    }
    impl RetainedMetadata for GuardedDelivery {
        fn future(self: Arc<Self>) -> QueuedMetadata {
            Box::pin(Self::fail())
        }
        fn future_bytes(&self) -> usize {
            fn size<F: std::future::Future>(_: fn() -> F) -> usize {
                std::mem::size_of::<F>()
            }
            size(Self::fail)
        }
        fn owned_heap_bytes(&self) -> usize {
            0
        }
    }
    let control = CoreControl::default();
    let registry = HandleMetadataRegistry::new(&control).unwrap();
    let metadata = registry.admit(&control).unwrap().register();
    let dropped = Arc::new(AtomicBool::new(false));
    let guards_free = Arc::new(AtomicBool::new(false));
    assert!(
        metadata
            .run_delivery(GuardedDelivery {
                metadata: metadata.clone(),
                dropped: dropped.clone(),
                guards_free: guards_free.clone(),
            })
            .await
            .is_err()
    );
    let attempt = metadata
        .pin_retry_after_retirement(|| {
            drop(registry);
            assert!(!dropped.load(Ordering::Acquire));
            assert!(!metadata.retirement_guards_available());
        })
        .unwrap();
    assert!(dropped.load(Ordering::Acquire));
    assert!(guards_free.load(Ordering::Acquire));
    drop(attempt);
    drop(metadata);
    assert_eq!(control.usage().bytes, 0);
}

#[tokio::test(flavor = "current_thread")]
async fn ordinary_invariant_and_oversized_step_errors_preserve_retry_and_explicit_projection() {
    let control = CoreControl::default();
    let invariant = HandleMetadata::prepare(&control).unwrap();
    let error = invariant
        .run(async {
            Err(anyhow::Error::new(
                crate::handle_metadata::DeliveryError::Invariant("terminal proposal missing"),
            ))
        })
        .await
        .unwrap_err();
    let failure = metadata_failure(&error).unwrap();
    assert_eq!(failure.category, FailureCategory::Invariant);
    assert_eq!(failure.step_error().code, "component");
    assert_eq!(failure.audit_code(), "internal");
    assert_eq!(failure.wit_code(), "unavailable");
    assert_eq!(failure.message(), "terminal proposal missing");
    invariant.run(async { Ok(()) }).await.unwrap();
    // The old immutable error is still its first ordinary failure.
    assert_eq!(
        metadata_failure(&error).unwrap().category,
        FailureCategory::Invariant
    );

    let oversized = HandleMetadata::prepare(&control).unwrap();
    let code = "界".repeat(40);
    let code_length = code.len();
    let error = oversized
        .run(async move { Err(StepError::new(code, "small original message").into()) })
        .await
        .unwrap_err();
    let failure = metadata_failure(&error).unwrap();
    assert_eq!(failure.category, FailureCategory::Step);
    assert_eq!(failure._code_original_length, Some(code_length));
    assert!(failure._code_truncated);
    assert!(failure.truncated());
    assert_eq!(failure.step_error().code, "component");
    assert_eq!(failure.audit_code(), "internal");
    assert_eq!(failure.wit_code(), "internal");
    assert_eq!(failure.message(), "small original message [truncated]");
    oversized.run(async { Ok(()) }).await.unwrap();
    assert!(metadata_failure(&error).unwrap()._code_truncated);
}

#[tokio::test(flavor = "current_thread")]
async fn omitted_audit_sibling_causes_are_explicit_when_the_message_bound_is_reached() {
    let control = CoreControl::default();
    let metadata = HandleMetadata::prepare(&control).unwrap();
    let error = metadata
        .run(async {
            Err(tobkiri_gate::GateError::AuditFailure {
                denial: Box::new(tobkiri_gate::GateError::Persistence("x".repeat(1024))),
                audit: Box::new(tobkiri_gate::GateError::Cancelled),
            }
            .into())
        })
        .await
        .unwrap_err();
    let failure = metadata_failure(&error).unwrap();
    assert_eq!(failure.category, FailureCategory::AuditFailure);
    assert_eq!(failure.cause_count(), 2);
    assert!(failure._causes_truncated);
    assert!(failure.truncated());
    assert!(failure.message().len() <= 512);
}

#[tokio::test(flavor = "current_thread")]
async fn native_boundary_projection_keeps_small_gate_and_frame_error_classification() {
    let control = CoreControl::default();
    let gate = HandleMetadata::prepare(&control).unwrap();
    let error = gate
        .run(async {
            Err(anyhow::Error::new(
                crate::handle_metadata::DeliveryError::Gate(
                    FailureStage::RecordOutcome,
                    tobkiri_gate::GateError::Persistence("small physical sink failure".into()),
                ),
            ))
        })
        .await
        .unwrap_err();
    let failure = metadata_failure(&error).unwrap();
    assert_eq!(failure._stage, FailureStage::RecordOutcome);
    assert_eq!(failure.category, FailureCategory::Persistence);
    assert_eq!(failure.wit_code(), "denied");
    assert!(!failure.truncated());
    assert_eq!(
        failure.message(),
        "authority persistence unavailable: small physical sink failure"
    );
    assert_eq!(crate::host::step_audit_code(&error), "denied");
    let projected = crate::host::step_error(error);
    assert_eq!(projected.code, "denied");
    assert_eq!(
        projected.message,
        "authority persistence unavailable: small physical sink failure"
    );
    let frame = HandleMetadata::prepare(&control).unwrap();
    let error = frame
        .run(async {
            Err(anyhow::Error::new(
                crate::handle_metadata::DeliveryError::Frame(StepError::new(
                    "allocation-unavailable",
                    "original frame ACK capacity failure",
                )),
            ))
        })
        .await
        .unwrap_err();
    let failure = metadata_failure(&error).unwrap();
    assert_eq!(failure._stage, FailureStage::FrameAcknowledged);
    assert_eq!(failure.category, FailureCategory::Step);
    assert_eq!(failure.wit_code(), "internal");
    assert_eq!(crate::host::step_audit_code(&error), "internal");
    let projected = crate::host::step_error(error);
    assert_eq!(projected.code, "allocation-unavailable");
    assert_eq!(projected.message, "original frame ACK capacity failure");
}

#[tokio::test(flavor = "current_thread")]
async fn retained_diagnostic_bounds_utf8_and_cause_depth_without_formatting_unknown_errors() {
    let control = CoreControl::default();
    let metadata = HandleMetadata::prepare(&control).unwrap();
    let source = "界".repeat(600);
    let original_length = source.len();
    let error = metadata
        .run(async move {
            Err(anyhow::Error::new(
                crate::handle_metadata::DeliveryError::Gate(
                    FailureStage::SettleDeadline,
                    tobkiri_gate::GateError::Persistence(source),
                ),
            ))
        })
        .await
        .unwrap_err();
    let failure = metadata_failure(&error).unwrap();
    assert_eq!(failure._stage, FailureStage::SettleDeadline);
    assert_eq!(failure._original_length, Some(original_length));
    assert!(failure.truncated());
    assert!(failure.message().len() <= 512);
    assert!(failure.message().ends_with(" [truncated]"));
    assert!(failure.message().is_char_boundary(failure.message().len()));
    let deep = HandleMetadata::prepare(&control).unwrap();
    let mut source = tobkiri_gate::GateError::Persistence("leaf".into());
    for _ in 0..12 {
        source = tobkiri_gate::GateError::AuditFailure {
            denial: Box::new(source),
            audit: Box::new(tobkiri_gate::GateError::Cancelled),
        };
    }
    let error = deep
        .run(async move {
            Err(anyhow::Error::new(
                crate::handle_metadata::DeliveryError::Gate(FailureStage::RecordOutcome, source),
            ))
        })
        .await
        .unwrap_err();
    let failure = metadata_failure(&error).unwrap();
    assert_eq!(failure.category, FailureCategory::AuditFailure);
    assert_eq!(failure._original_length, None);
    assert!(failure._causes_truncated);
    assert!(failure.truncated());
    assert!(failure.cause_count() <= 7);
    #[derive(Debug)]
    struct UnknownDisplay;
    impl std::fmt::Display for UnknownDisplay {
        fn fmt(&self, _: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
            panic!("unknown Display must never be called")
        }
    }
    impl std::error::Error for UnknownDisplay {}
    let unknown = HandleMetadata::prepare(&control).unwrap();
    let error = unknown
        .run(async { Err(anyhow::Error::new(UnknownDisplay)) })
        .await
        .unwrap_err();
    assert_eq!(
        metadata_failure(&error).unwrap().category,
        FailureCategory::Unknown
    );
    assert_eq!(metadata_failure(&error).unwrap()._original_length, None);
    let ran = Arc::new(AtomicUsize::new(0));
    let retry_ran = ran.clone();
    assert_eq!(crate::host::step_audit_code(&error), "internal");
    assert_eq!(metadata_failure(&error).unwrap().wit_code(), "unavailable");
    assert_eq!(
        metadata_failure(&error).unwrap().step_error().code,
        "component"
    );
    assert!(metadata_failure(&error).unwrap()._diagnostic_unavailable);
    unknown
        .run(async move {
            retry_ran.fetch_add(1, Ordering::AcqRel);
            Ok(())
        })
        .await
        .unwrap();
    assert_eq!(ran.load(Ordering::Acquire), 1);
    assert_eq!(
        metadata_failure(&error).unwrap().category,
        FailureCategory::Unknown
    );
}
