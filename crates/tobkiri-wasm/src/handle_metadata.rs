//! Original handle terminal work, funded before source admission. Physical
//! source cleanup remains separate: it must finish before proposing metadata.
use anyhow::{Context, Result, bail};
use std::sync::{
    Arc, Mutex, OnceLock, Weak,
    atomic::{AtomicBool, AtomicU64, Ordering},
};
use tobkiri_flow::StepError;
use tobkiri_gate::{ControlKind, ControlReservation, CoreControl};

const MAX_SETTLEMENTS: usize = 1024;
const MAX_METADATA_FUTURE: usize = 64 * 1024;
const METADATA_TASK_BYTES: u64 = (MAX_METADATA_FUTURE + 4096) as u64;
const MAX_REAPER_FUTURE: usize = 256 * 1024;
pub(crate) type QueuedMetadata =
    std::pin::Pin<Box<dyn std::future::Future<Output = Result<()>> + Send + 'static>>;

enum MetadataState {
    Pending,
    Running(tokio::task::JoinHandle<Result<()>>),
    Finalizing,
    Complete {
        error: Option<anyhow::Error>,
        failed: bool,
        retryable: bool,
        failure_stamp: u64,
    },
}

/// Trusted, bounded constructor for a fresh delivery attempt. Implementations
/// retain authority objects but never a Host or its inventory. Constructing a
/// future only boxes captures; it cannot execute delivery or acquire Gate locks.
pub(crate) trait RetainedMetadata: Send + Sync + 'static {
    fn future(self: Arc<Self>) -> QueuedMetadata;
    fn future_bytes(&self) -> usize;
    fn owned_heap_bytes(&self) -> usize;
}

/// The exact original Gate/lease/frame and first proposal, without Entry's
/// source/control/Host edges. Outcome heap moves into the original receipt on
/// the first attempt; retries cannot propose another winner.
pub(crate) struct HandleDelivery {
    gate: Arc<dyn tobkiri_gate::Gate>,
    lease: Arc<crate::handles::HandleLease>,
    source_frame: Option<Arc<dyn crate::ProviderFrame>>,
    proposal: Mutex<Option<tobkiri_gate::Outcome>>,
}
impl HandleDelivery {
    pub(crate) fn new(
        gate: Arc<dyn tobkiri_gate::Gate>,
        entry: &crate::HandleEntry,
        outcome: tobkiri_gate::Outcome,
    ) -> Self {
        Self {
            gate,
            lease: entry.lease.clone(),
            source_frame: entry.source_frame.clone(),
            proposal: Mutex::new(Some(outcome)),
        }
    }

    pub(crate) async fn deliver(&self) -> Result<()> {
        let mut receipt = self.lease.close.lock().await;
        if receipt.outcome.is_none() {
            if self
                .lease
                .permit
                .as_ref()
                .is_some_and(|permit| std::time::Instant::now() >= permit.deadline())
            {
                receipt.deadline_settlement = true;
            }
            receipt.outcome = self.proposal.lock().map_err(|_| unavailable())?.take();
            if receipt.outcome.is_none() {
                bail!("terminal proposal missing")
            }
        }
        if !receipt.delivered {
            if let Some(id) = &self.lease.grant {
                if receipt.deadline_settlement {
                    let permit = self
                        .lease
                        .permit
                        .as_ref()
                        .context("deadline proof missing")?;
                    let terminal = self.gate.settle_deadline(permit).await?;
                    receipt.outcome = Some(terminal.decision.outcome);
                } else if let Err(error) = self
                    .gate
                    .record_outcome(
                        id.clone(),
                        receipt
                            .outcome
                            .clone()
                            .context("terminal proposal missing")?,
                    )
                    .await
                {
                    if matches!(error, tobkiri_gate::GateError::Deadline) {
                        // Durable wall-time expiry may win between the Instant
                        // check and CAS. Deliver that same winner without changing
                        // its disposition or replaying physical cleanup.
                        receipt.deadline_settlement = true;
                        let permit = self
                            .lease
                            .permit
                            .as_ref()
                            .context("deadline proof missing")?;
                        let terminal = self.gate.settle_deadline(permit).await?;
                        receipt.outcome = Some(terminal.decision.outcome);
                    } else {
                        return Err(error.into());
                    }
                }
            }
            if let (Some(frame), Some(permit)) = (&self.source_frame, &self.lease.permit) {
                let acknowledged = self.gate.acknowledged_terminal(permit)?;
                frame
                    .terminal_acknowledged(&acknowledged)
                    .map_err(anyhow::Error::new)?;
                receipt.outcome = Some(acknowledged.outcome().clone());
            }
            receipt.delivered = true;
        }
        Ok(())
    }
}
async fn deliver_retained(delivery: Arc<HandleDelivery>) -> Result<()> {
    delivery.deliver().await
}
impl RetainedMetadata for HandleDelivery {
    fn future(self: Arc<Self>) -> QueuedMetadata {
        Box::pin(deliver_retained(self))
    }
    fn future_bytes(&self) -> usize {
        fn size<F: std::future::Future>(_: fn(Arc<HandleDelivery>) -> F) -> usize {
            std::mem::size_of::<F>()
        }
        size(deliver_retained)
    }
    fn owned_heap_bytes(&self) -> usize {
        match self
            .proposal
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .as_ref()
        {
            // The first attempt moves this heap to the receipt; record_outcome
            // may hold one ordinary clone while it is live. Count both.
            Some(tobkiri_gate::Outcome::Failed { code }) => {
                code.capacity().checked_mul(2).unwrap_or(usize::MAX)
            }
            _ => 0,
        }
    }
}

/// Inventory holds this sidecar strongly; metadata holds only Weak. In
/// particular, capsule -> original lease -> metadata never returns to capsule.
struct MetadataCustody {
    metadata: Arc<HandleMetadata>,
    delivery: Mutex<Option<Arc<dyn RetainedMetadata>>>,
}

enum CleanupPublication {
    Pending,
    NoPublishedHandle,
    Unknown,
    Submitted(Arc<tokio::sync::Mutex<CleanupJoin>>),
}
struct CleanupJoin {
    task: Option<tokio::task::JoinHandle<Result<Option<crate::core_imports::SourceCleanupReport>>>>,
    result: Option<Result<crate::core_imports::SourceCleanupReport, StepError>>,
}
/// The source reaper's actual Result is independent of terminal delivery.
/// Its own future may await metadata.run; only shutdown joins this receipt.
struct SourceCleanupOwner {
    publication: Mutex<CleanupPublication>,
    ready: tokio::sync::Notify,
    settled: AtomicBool,
    future_bytes: usize,
    task_funding: Mutex<Option<Arc<ControlReservation>>>,
    _receipt_funding: ControlReservation,
}
impl SourceCleanupOwner {
    fn prepare(control: &CoreControl, future_bytes: usize) -> Result<Arc<Self>> {
        if future_bytes > MAX_REAPER_FUTURE {
            bail!("source cleanup exceeds its fixed future bound")
        }
        let receipt_funding = control.reserve(ControlKind::Frame, 4096)?;
        let task_funding =
            control.reserve(ControlKind::Task, u64::try_from(future_bytes)? + 4096)?;
        Ok(Arc::new(Self {
            publication: Mutex::new(CleanupPublication::Pending),
            ready: tokio::sync::Notify::new(),
            settled: AtomicBool::new(false),
            future_bytes,
            task_funding: Mutex::new(Some(Arc::new(task_funding))),
            _receipt_funding: receipt_funding,
        }))
    }

    fn finish_unpublished(&self, published: bool) {
        let no_source = {
            let mut publication = self
                .publication
                .lock()
                .unwrap_or_else(|error| error.into_inner());
            if !matches!(*publication, CleanupPublication::Pending) {
                return;
            }
            *publication = if published {
                CleanupPublication::Unknown
            } else {
                CleanupPublication::NoPublishedHandle
            };
            !published
        };
        if no_source {
            let funding = self
                .task_funding
                .lock()
                .unwrap_or_else(|error| error.into_inner())
                .take();
            drop(funding);
            self.settled.store(true, Ordering::Release);
        }
        self.ready.notify_waiters();
    }

    fn spawn(
        &self,
        runtime: &tokio::runtime::Handle,
        future: impl std::future::Future<
            Output = Result<Option<crate::core_imports::SourceCleanupReport>>,
        > + Send
        + 'static,
    ) -> Result<()> {
        if std::mem::size_of_val(&future) > self.future_bytes {
            self.finish_unpublished(true);
            bail!("source cleanup future exceeds its prepaid bound")
        }
        let funding = self
            .task_funding
            .lock()
            .map_err(|_| cleanup_unknown())?
            .as_ref()
            .context("source cleanup task funding missing")?
            .clone();
        let mut future = Some(future);
        let submitted = {
            let mut publication = self.publication.lock().map_err(|_| cleanup_unknown())?;
            if !matches!(*publication, CleanupPublication::Pending) {
                Err(anyhow::Error::new(cleanup_unknown()))
            } else {
                let future = future.take().expect("source cleanup taken twice");
                match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    runtime.spawn(async move {
                        let _funding = funding;
                        future.await
                    })
                })) {
                    Ok(task) => {
                        *publication = CleanupPublication::Submitted(Arc::new(
                            tokio::sync::Mutex::new(CleanupJoin {
                                task: Some(task),
                                result: None,
                            }),
                        ));
                        Ok(())
                    }
                    Err(_) => {
                        *publication = CleanupPublication::Unknown;
                        Err(anyhow::Error::new(cleanup_unknown()))
                    }
                }
            }
        };
        drop(future);
        self.ready.notify_waiters();
        submitted
    }

    async fn wait(&self) -> Result<Option<crate::core_imports::SourceCleanupReport>, StepError> {
        loop {
            let ready = self.ready.notified();
            tokio::pin!(ready);
            ready.as_mut().enable();
            let submitted = {
                let publication = self.publication.lock().map_err(|_| cleanup_unknown())?;
                match &*publication {
                    CleanupPublication::Pending => None,
                    CleanupPublication::NoPublishedHandle => return Ok(None),
                    CleanupPublication::Unknown => return Err(cleanup_unknown()),
                    CleanupPublication::Submitted(join) => Some(join.clone()),
                }
            };
            let Some(submitted) = submitted else {
                ready.await;
                continue;
            };
            let mut joined = submitted.lock().await;
            if let Some(result) = &joined.result {
                return result.clone().map(Some);
            }
            // Borrow the original Join across await. Dropping close drops
            // only this observer's guard, never the source reaper or receipt.
            let result = match joined.task.as_mut().ok_or_else(cleanup_unknown)?.await {
                Ok(Ok(Some(report))) => Ok(report),
                Ok(Ok(None)) | Ok(Err(_)) | Err(_) => Err(cleanup_unknown()),
            };
            drop(joined.task.take());
            joined.result = Some(result.clone());
            if result.is_ok() {
                let funding = self
                    .task_funding
                    .lock()
                    .map_err(|_| cleanup_unknown())?
                    .take();
                drop(funding);
                self.settled.store(true, Ordering::Release);
            }
            return result.map(Some);
        }
    }
}

fn cleanup_unknown() -> StepError {
    StepError::new(
        "unavailable",
        "source cleanup reaper did not complete successfully",
    )
}

/// Every observer borrows this SAME original Join. Cancelling its wait leaves
/// the Join in the lease/host inventory, rather than detaching it from close.
pub(crate) struct HandleMetadata {
    runtime: tokio::runtime::Handle,
    state: tokio::sync::Mutex<MetadataState>,
    ready: tokio::sync::Notify,
    settled: AtomicBool,
    unregistered: AtomicBool,
    queued: Mutex<Option<QueuedMetadata>>,
    task_funding: Mutex<Option<Arc<ControlReservation>>>,
    source_cleanup: Mutex<Option<Arc<SourceCleanupOwner>>>,
    custody: OnceLock<Weak<MetadataCustody>>,
    failure_stamp: AtomicU64,
    rejected: AtomicBool,
    retiring: AtomicBool,
    // Receipt storage outlives the actual task and is independently prepaid.
    _receipt_funding: ControlReservation,
}
impl HandleMetadata {
    pub(crate) fn prepare(control: &CoreControl) -> Result<Arc<Self>> {
        let runtime = tokio::runtime::Handle::try_current()
            .context("handle metadata runtime is unavailable before admission")?;
        // Both Arc headers and the sidecar live in the original receipt budget.
        if std::mem::size_of::<Self>()
            + std::mem::size_of::<MetadataCustody>()
            + 4 * std::mem::size_of::<usize>()
            > 4096
        {
            bail!("handle metadata receipt exceeds its prepaid bound")
        }
        let receipt_funding = control.reserve(ControlKind::Frame, 4096)?;
        let task_funding = control.reserve(ControlKind::Task, METADATA_TASK_BYTES)?;
        Ok(Arc::new(Self {
            runtime,
            state: tokio::sync::Mutex::new(MetadataState::Pending),
            ready: tokio::sync::Notify::new(),
            settled: AtomicBool::new(false),
            unregistered: AtomicBool::new(false),
            queued: Mutex::new(None),
            task_funding: Mutex::new(Some(Arc::new(task_funding))),
            source_cleanup: Mutex::new(None),
            custody: OnceLock::new(),
            failure_stamp: AtomicU64::new(0),
            rejected: AtomicBool::new(false),
            retiring: AtomicBool::new(false),
            _receipt_funding: receipt_funding,
        }))
    }

    fn prepare_source_cleanup(&self, control: &CoreControl, future_bytes: usize) -> Result<()> {
        let cleanup = SourceCleanupOwner::prepare(control, future_bytes)?;
        let mut current = self.source_cleanup.lock().map_err(|_| cleanup_unknown())?;
        if current.is_some() {
            bail!("source cleanup already prepared")
        }
        *current = Some(cleanup);
        Ok(())
    }

    fn source_cleanup(&self) -> Option<Arc<SourceCleanupOwner>> {
        self.source_cleanup
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .clone()
    }

    pub(crate) fn spawn_reported_source_cleanup(
        &self,
        future: impl std::future::Future<
            Output = Result<Option<crate::core_imports::SourceCleanupReport>>,
        > + Send
        + 'static,
    ) -> Result<()> {
        self.source_cleanup()
            .context("source cleanup was not prepaid")?
            .spawn(&self.runtime, future)
    }

    #[cfg(test)]
    pub(crate) fn spawn_source_cleanup(
        &self,
        future: impl std::future::Future<Output = Result<()>> + Send + 'static,
    ) -> Result<()> {
        self.spawn_reported_source_cleanup(async move {
            future
                .await
                .map(|()| Some(crate::core_imports::SourceCleanupReport::joined_fixture_work()))
        })
    }

    fn finish_unpublished_source(&self, published: bool) {
        if let Some(cleanup) = self.source_cleanup() {
            cleanup.finish_unpublished(published);
        }
    }

    pub(crate) async fn wait_source_cleanup(
        &self,
    ) -> Result<Option<crate::core_imports::SourceCleanupReport>, StepError> {
        match self.source_cleanup() {
            Some(cleanup) => cleanup.wait().await,
            None => Ok(None),
        }
    }

    fn fully_settled(&self) -> bool {
        self.settled.load(Ordering::Acquire)
            && self
                .source_cleanup()
                .is_none_or(|cleanup| cleanup.settled.load(Ordering::Acquire))
    }

    fn poll_source_cleanup(&self) {
        // Admission maintenance borrows only existing receipts, outside the
        // inventory guard. A pending poll never takes an owning Join away.
        let future = self.wait_source_cleanup();
        tokio::pin!(future);
        let mut context = std::task::Context::from_waker(std::task::Waker::noop());
        let _ = std::future::Future::poll(future.as_mut(), &mut context);
    }

    fn retain<T: RetainedMetadata>(&self, delivery: T) -> Result<()> {
        // Admission may already have pruned the successful sidecar while an
        // old lease still owns metadata. Repeated finish remains idempotent;
        // it must not require reconstructing custody or a replacement task.
        if self.retiring.load(Ordering::Acquire) || self.settled.load(Ordering::Acquire) {
            drop(delivery);
            return Ok(());
        }
        let custody = match self.custody.get().and_then(Weak::upgrade) {
            Some(custody) => custody,
            None if self.retiring.load(Ordering::Acquire)
                || self.settled.load(Ordering::Acquire) =>
            {
                // Success/prune may race the entry check above. Recheck its
                // release-published receipt before interpreting inventory loss.
                drop(delivery);
                return Ok(());
            }
            None => bail!("handle delivery inventory is unavailable"),
        };
        let mut retained = custody.delivery.lock().map_err(|_| unavailable())?;
        if self.rejected.load(Ordering::Acquire) {
            drop(retained);
            bail!("handle delivery publication is permanently unknown")
        }
        if retained.is_some()
            || self.retiring.load(Ordering::Acquire)
            || self.settled.load(Ordering::Acquire)
        {
            // Never drop losing authority captures while a sidecar is locked.
            drop(retained);
            drop(delivery);
            return Ok(());
        }
        // The original Task allowance covers the capsule, its Arc header,
        // owned heap (including a Failed code's capacity), and ONE live boxed
        // future together. The existing extra 4096 remains runtime overhead.
        let bytes = std::mem::size_of::<T>()
            .checked_add(2 * std::mem::size_of::<usize>())
            .and_then(|bytes| bytes.checked_add(delivery.owned_heap_bytes()))
            .and_then(|bytes| bytes.checked_add(delivery.future_bytes()));
        if bytes.is_none_or(|bytes| bytes > MAX_METADATA_FUTURE) {
            // Latch under the SAME slot guard that admits a capsule. A later
            // valid proposal cannot race this rejection into a fresh start.
            self.rejected.store(true, Ordering::Release);
            drop(retained);
            bail!("handle delivery exceeds its prepaid bound")
        }
        *retained = Some(Arc::new(delivery));
        Ok(())
    }

    fn retained_future(&self) -> Option<QueuedMetadata> {
        let custody = self.custody.get().and_then(Weak::upgrade)?;
        let delivery = custody
            .delivery
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .clone()?;
        let bytes = delivery.future_bytes();
        let future = delivery.future();
        // Check the actual boxed concrete future against the preadmission
        // accounting too. No second task/future may overlap the original Join.
        assert!(std::mem::size_of_val(future.as_ref().get_ref()) <= bytes);
        Some(future)
    }

    fn release_delivery(&self) {
        let retired = self
            .custody
            .get()
            .and_then(Weak::upgrade)
            .and_then(|custody| {
                custody
                    .delivery
                    .lock()
                    .unwrap_or_else(|error| error.into_inner())
                    .take()
            });
        drop(retired);
    }

    /// Synchronous consumed-custody transfer, also used by Drop. A pending
    /// close observes this SAME capsule and original funding without a Host
    /// capture or detached producer. Complete failures are retried by wait.
    pub(crate) fn queue_delivery<T: RetainedMetadata>(&self, delivery: T) -> Result<()> {
        if let Err(error) = self.retain(delivery) {
            // A consumed Drop cannot await or discard a failed publication.
            // Pending close must observe permanent uncertainty, not hang.
            self.rejected.store(true, Ordering::Release);
            self.ready.notify_waiters();
            return Err(error);
        }
        if let Ok(mut state) = self.state.try_lock()
            && !self.rejected.load(Ordering::Acquire)
            && matches!(*state, MetadataState::Pending)
            && let Some(future) = self.retained_future()
        {
            *state = self.start_queued(future);
        }
        self.ready.notify_waiters();
        Ok(())
    }

    pub(crate) async fn run_delivery<T: RetainedMetadata>(&self, delivery: T) -> Result<()> {
        let observed = self.failure_stamp.load(Ordering::Acquire);
        self.queue_delivery(delivery)?;
        self.wait_after(observed).await
    }

    // Kept only for tests of the original single-Join ownership primitive.
    // Production always retains a bounded original delivery capsule instead.
    #[cfg(test)]
    pub(crate) fn queue(
        &self,
        metadata: impl std::future::Future<Output = Result<()>> + Send + 'static,
    ) {
        assert!(std::mem::size_of_val(&metadata) <= MAX_METADATA_FUTURE);
        let mut metadata: Option<QueuedMetadata> = Some(Box::pin(metadata));
        {
            let mut queued = self
                .queued
                .lock()
                .unwrap_or_else(|error| error.into_inner());
            if !self.settled.load(Ordering::Acquire) && queued.is_none() {
                *queued = metadata.take();
            }
        }
        drop(metadata);
        if let Ok(mut state) = self.state.try_lock()
            && !self.rejected.load(Ordering::Acquire)
            && matches!(*state, MetadataState::Pending)
            && let Some(metadata) = self.take_queued()
        {
            *state = self.start_queued(metadata);
        }
        self.ready.notify_waiters();
    }

    fn take_queued(&self) -> Option<QueuedMetadata> {
        self.queued
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .take()
    }

    fn failed_state(&self, error: anyhow::Error, retryable: bool) -> MetadataState {
        let failure_stamp = self.failure_stamp.fetch_add(1, Ordering::AcqRel) + 1;
        MetadataState::Complete {
            error: Some(error),
            failed: true,
            retryable,
            failure_stamp,
        }
    }

    fn start_queued(&self, metadata: QueuedMetadata) -> MetadataState {
        let funding = self
            .task_funding
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .as_ref()
            .expect("consumed metadata funding missing")
            .clone();
        let runtime = self.runtime.clone();
        match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            self.runtime.spawn_blocking(move || {
                let _funding = funding;
                runtime.block_on(metadata)
            })
        })) {
            Ok(task) => MetadataState::Running(task),
            // No proof that a submitted task did not run: permanent Unknown.
            Err(_) => self.failed_state(unavailable().into(), false),
        }
    }

    #[cfg(test)]
    pub(crate) async fn run(
        &self,
        metadata: impl std::future::Future<Output = Result<()>> + Send + 'static,
    ) -> Result<()> {
        if std::mem::size_of_val(&metadata) > MAX_METADATA_FUTURE {
            bail!("handle metadata payload exceeds its prepaid bound")
        }
        let observed = self.failure_stamp.load(Ordering::Acquire);
        let mut metadata: Option<QueuedMetadata> = Some(Box::pin(metadata));
        {
            let mut state = self.state.lock().await;
            if !self.rejected.load(Ordering::Acquire)
                && matches!(
                    *state,
                    MetadataState::Pending
                        | MetadataState::Complete {
                            failed: true,
                            retryable: true,
                            ..
                        }
                )
            {
                *state = self.start_queued(metadata.take().expect("metadata taken twice"));
            }
        }
        drop(metadata);
        self.ready.notify_waiters();
        self.wait_after(observed).await
    }

    pub(crate) async fn wait(&self) -> Result<()> {
        self.wait_after(self.failure_stamp.load(Ordering::Acquire))
            .await
    }

    pub(crate) async fn wait_after(&self, observed_failure: u64) -> Result<()> {
        loop {
            let ready = self.ready.notified();
            tokio::pin!(ready);
            ready.as_mut().enable();
            if self.unregistered.load(Ordering::Acquire) {
                return Ok(());
            }
            let mut state = self.state.lock().await;
            // A later observer may already have replaced Complete with its
            // retry Join before an older waiter reacquires this guard. The
            // older waiter still reports the failed attempt it began under,
            // never success (or another retry) from the replacement attempt.
            if self.failure_stamp.load(Ordering::Acquire) > observed_failure {
                return Err(unavailable().into());
            }
            match &mut *state {
                MetadataState::Pending => {
                    if self.rejected.load(Ordering::Acquire) {
                        *state = self.failed_state(unavailable().into(), false);
                        continue;
                    }
                    if let Some(metadata) = self.take_queued().or_else(|| self.retained_future()) {
                        *state = self.start_queued(metadata);
                        continue;
                    }
                    drop(state);
                    ready.await;
                }
                MetadataState::Running(task) => {
                    // Borrow the SAME actual Join. Observer cancellation leaves
                    // it here; no retry reuses funding until it has completed.
                    let (result, retryable) = match task.await {
                        // An ordinary returned error is a completed attempt,
                        // not lost execution. Preserve the existing ability to
                        // retry any such failure (including frame ACK capacity
                        // StepError) with this SAME capsule on a later observer.
                        Ok(result) => {
                            let retryable = result.is_err();
                            (result, retryable)
                        }
                        Err(_) => (Err(unavailable().into()), false),
                    };
                    let failed = result.is_err();
                    let complete = if let Err(error) = result {
                        self.failed_state(error, retryable)
                    } else {
                        // Seal retain before releasing the sidecar slot. No
                        // later proposal can enter the retired funded owner.
                        self.retiring.store(true, Ordering::Release);
                        MetadataState::Finalizing
                    };
                    let joined = std::mem::replace(&mut *state, complete);
                    let result = match &mut *state {
                        MetadataState::Complete { error, .. } if failed => {
                            Err(error.take().unwrap_or_else(|| unavailable().into()))
                        }
                        _ => Ok(()),
                    };
                    // The completed Join has no live future/captures left.
                    // Retire that task shell before another observer can
                    // enter the state guard and reuse its reservation.
                    drop(joined);
                    drop(state);
                    if !failed {
                        self.release_delivery();
                        drop(self.take_queued());
                        let funding = self.task_funding.lock().map_err(|_| unavailable())?.take();
                        drop(funding);
                        self.settled.store(true, Ordering::Release);
                    }
                    self.ready.notify_waiters();
                    // No failure loop: this observer returns its ONE attempt.
                    return result;
                }
                MetadataState::Finalizing => {
                    if self.settled.load(Ordering::Acquire) {
                        return Ok(());
                    }
                    drop(state);
                    ready.await;
                }
                MetadataState::Complete {
                    error,
                    failed,
                    retryable,
                    failure_stamp,
                } => {
                    // An observer that began while the failed attempt was
                    // running returns that attempt's error. Only a later
                    // observer can start one fresh delivery-only attempt.
                    if *failed
                        && *retryable
                        && observed_failure == *failure_stamp
                        && let Some(metadata) =
                            self.take_queued().or_else(|| self.retained_future())
                    {
                        *state = self.start_queued(metadata);
                        continue;
                    }
                    return if *failed {
                        Err(error.take().unwrap_or_else(|| unavailable().into()))
                    } else {
                        Ok(())
                    };
                }
            }
        }
    }

    #[cfg(test)]
    pub(crate) fn retirement_guards_available(&self) -> bool {
        self.state.try_lock().is_ok()
            && self.queued.try_lock().is_ok()
            && self.task_funding.try_lock().is_ok()
            && self
                .custody
                .get()
                .and_then(Weak::upgrade)
                .is_none_or(|custody| custody.delivery.try_lock().is_ok())
    }

    fn abandon_unregistered(&self) {
        // The admission guard cannot coexist with a published handle. Close
        // may already be observing Pending, so publish without acquiring its
        // async mutex or attempting an asynchronous Drop.
        self.retiring.store(true, Ordering::Release);
        self.unregistered.store(true, Ordering::Release);
        self.finish_unpublished_source(false);
        self.release_delivery();
        drop(self.take_queued());
        let funding = self
            .task_funding
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .take();
        drop(funding);
        self.settled.store(true, Ordering::Release);
        self.ready.notify_waiters();
    }
}

fn unavailable() -> StepError {
    StepError::new("unavailable", "handle metadata task did not finish")
}

struct RegistryState {
    closed: bool,
    entries: Vec<Arc<MetadataCustody>>,
}

/// This inventory is owned by the actual Session's PackHost. Admission and
/// sealing use one guard, so close includes every source that can consume a
/// grant, including a source still being registered when shutdown starts.
pub(crate) struct HandleMetadataRegistry {
    state: Mutex<RegistryState>,
    _funding: ControlReservation,
}
impl HandleMetadataRegistry {
    pub(crate) fn new(control: &CoreControl) -> Result<Self> {
        let bytes = MAX_SETTLEMENTS
            .checked_mul(std::mem::size_of::<Arc<MetadataCustody>>())
            .and_then(|bytes| bytes.checked_add(1024))
            .context("handle terminal inventory bound overflow")?;
        let funding = control.reserve(ControlKind::Registration, u64::try_from(bytes)?)?;
        let mut entries = Vec::new();
        entries.try_reserve_exact(MAX_SETTLEMENTS)?;
        if entries.capacity() != MAX_SETTLEMENTS {
            bail!("handle terminal inventory backing exceeds its bound")
        }
        Ok(Self {
            state: Mutex::new(RegistryState {
                closed: false,
                entries,
            }),
            _funding: funding,
        })
    }

    pub(crate) fn admit(&self, control: &CoreControl) -> Result<HandleMetadataAdmission> {
        let length = {
            let state = self.state.lock().map_err(|_| unavailable())?;
            if state.closed {
                bail!("handle source registration is closed")
            }
            state.entries.len()
        };
        for index in 0..length {
            let metadata = {
                let state = self.state.lock().map_err(|_| unavailable())?;
                state.entries.get(index).cloned()
            };
            if let Some(metadata) = metadata {
                metadata.metadata.poll_source_cleanup();
            }
        }
        // Release completed original captures/funding outside the registry.
        loop {
            let removed = {
                let mut state = self.state.lock().map_err(|_| unavailable())?;
                if state.closed {
                    bail!("handle source registration is closed")
                }
                state
                    .entries
                    .iter()
                    .position(|entry| entry.metadata.fully_settled())
                    .map(|index| state.entries.swap_remove(index))
            };
            let Some(removed) = removed else { break };
            drop(removed);
        }
        let metadata = HandleMetadata::prepare(control)?;
        let custody = Arc::new(MetadataCustody {
            metadata: metadata.clone(),
            delivery: Mutex::new(None),
        });
        assert!(metadata.custody.set(Arc::downgrade(&custody)).is_ok());
        {
            let mut state = self.state.lock().map_err(|_| unavailable())?;
            if state.closed || state.entries.len() == MAX_SETTLEMENTS {
                bail!("handle source registration is closed or full")
            }
            state.entries.push(custody);
        }
        Ok(HandleMetadataAdmission {
            metadata,
            registered: false,
        })
    }

    #[cfg(test)]
    pub(crate) fn guard_available(&self) -> bool {
        self.state.try_lock().is_ok()
    }

    pub(crate) async fn close(&self) -> Result<(), StepError> {
        let length = {
            let mut state = self.state.lock().map_err(|_| unavailable())?;
            state.closed = true;
            state.entries.len()
        };
        let mut failure = None;
        for index in 0..length {
            let metadata = {
                let state = self.state.lock().map_err(|_| unavailable())?;
                state.entries[index].metadata.clone()
            };
            if metadata.wait().await.is_err() {
                failure.get_or_insert_with(unavailable);
            }
            if let Err(error) = metadata.wait_source_cleanup().await {
                failure.get_or_insert(error);
            }
        }
        failure.map_or(Ok(()), Err)
    }
}

pub(crate) struct HandleMetadataAdmission {
    metadata: Arc<HandleMetadata>,
    registered: bool,
}
impl HandleMetadataAdmission {
    pub(crate) fn prepare_source_cleanup(
        &self,
        control: &CoreControl,
        future_bytes: usize,
    ) -> Result<()> {
        self.metadata.prepare_source_cleanup(control, future_bytes)
    }
    pub(crate) fn register(mut self) -> Arc<HandleMetadata> {
        self.registered = true;
        self.metadata.clone()
    }
}
impl Drop for HandleMetadataAdmission {
    fn drop(&mut self) {
        if !self.registered {
            self.metadata.abandon_unregistered();
        }
    }
}

/// Cancellation-safe original custody immediately after consume, including
/// failure before a public handle can be inserted. Only pre-consume admission
/// may use NoHandle; this guard always delivers the real consumed terminal.
pub(crate) struct ConsumedHandleMetadata {
    pub(crate) permit: Option<tobkiri_gate::DispatchPermit>,
    pub(crate) frame: Option<Arc<dyn crate::ProviderFrame>>,
    pub(crate) failure: &'static str,
    host: crate::PackHost,
    metadata: Arc<HandleMetadata>,
    lease: Option<Arc<crate::handles::HandleLease>>,
    kind: crate::HandleKind,
    owner: tobkiri_gate::CallCx,
    recipient: tobkiri_gate::CallCx,
    authority: tobkiri_canon::ids::ShapeRef,
    local_id: u64,
    generation: u64,
    version: &'static str,
    transferred: bool,
    published: bool,
}
impl ConsumedHandleMetadata {
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn new(
        host: crate::PackHost,
        metadata: HandleMetadataAdmission,
        permit: tobkiri_gate::DispatchPermit,
        kind: crate::HandleKind,
        owner: tobkiri_gate::CallCx,
        recipient: tobkiri_gate::CallCx,
        authority: tobkiri_canon::ids::ShapeRef,
        local_id: u64,
        generation: u64,
        version: &'static str,
    ) -> Self {
        Self {
            permit: Some(permit),
            frame: None,
            failure: "source-entry",
            host,
            metadata: metadata.register(),
            lease: None,
            kind,
            owner,
            recipient,
            authority,
            local_id,
            generation,
            version,
            transferred: false,
            published: false,
        }
    }

    pub(crate) fn lease(&mut self) -> Arc<crate::handles::HandleLease> {
        self.lease
            .get_or_insert_with(|| {
                Arc::new(crate::handles::HandleLease::authorized(
                    self.permit
                        .take()
                        .expect("consumed handle permit taken twice"),
                    self.metadata.clone(),
                ))
            })
            .clone()
    }

    pub(crate) fn entry(&mut self) -> crate::HandleEntry {
        let lease = self.lease();
        crate::HandleEntry::unpublished(
            self.kind,
            self.owner.clone(),
            self.local_id,
            self.recipient.clone(),
            self.authority.clone(),
            lease,
            self.generation,
            self.frame.clone(),
            self.version,
        )
    }

    pub(crate) async fn settle_failure(&mut self) -> Result<()> {
        self.metadata.finish_unpublished_source(self.published);
        let entry = self.entry();
        let delivery = HandleDelivery::new(
            self.host.shared.gate.clone(),
            &entry,
            tobkiri_gate::Outcome::Failed {
                code: self.failure.into(),
            },
        );
        self.metadata.run_delivery(delivery).await?;
        self.transferred = true;
        Ok(())
    }

    pub(crate) fn transfer_to_handle(&mut self) {
        self.transferred = true;
    }

    pub(crate) fn handle_published(&mut self) {
        self.published = true;
    }
}
impl Drop for ConsumedHandleMetadata {
    fn drop(&mut self) {
        if self.transferred {
            return;
        }
        self.metadata.finish_unpublished_source(self.published);
        let entry = self.entry();
        let delivery = HandleDelivery::new(
            self.host.shared.gate.clone(),
            &entry,
            tobkiri_gate::Outcome::Failed {
                code: self.failure.into(),
            },
        );
        // This fixed-size native capsule is bounded before any submission.
        // If admission is unavailable, preserve failure rather than inventing
        // terminal acknowledgement or a detached fallback task.
        if self.metadata.queue_delivery(delivery).is_err() {
            self.metadata.ready.notify_waiters();
        }
    }
}
