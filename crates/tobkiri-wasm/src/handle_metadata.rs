//! Original handle terminal work, funded before source admission. Physical
//! source cleanup remains separate: it must finish before proposing metadata.
use anyhow::{Context, Result, bail};
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, Ordering},
};
use tobkiri_flow::StepError;
use tobkiri_gate::{ControlKind, ControlReservation, CoreControl};

const MAX_SETTLEMENTS: usize = 1024;
const MAX_METADATA_FUTURE: usize = 64 * 1024;
const METADATA_TASK_BYTES: u64 = (MAX_METADATA_FUTURE + 4096) as u64;
const MAX_REAPER_FUTURE: usize = 256 * 1024;
type QueuedMetadata =
    std::pin::Pin<Box<dyn std::future::Future<Output = Result<()>> + Send + 'static>>;

enum MetadataState {
    Pending,
    Running(tokio::task::JoinHandle<Result<()>>),
    Complete {
        error: Option<anyhow::Error>,
        failed: bool,
    },
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
    // Receipt storage outlives the actual task and is independently prepaid.
    _receipt_funding: ControlReservation,
}
impl HandleMetadata {
    pub(crate) fn prepare(control: &CoreControl) -> Result<Arc<Self>> {
        let runtime = tokio::runtime::Handle::try_current()
            .context("handle metadata runtime is unavailable before admission")?;
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

    /// Cancellation of a consumed-but-unpublished source transfers its exact
    /// permit/receipt here synchronously. A close already observing Pending
    /// is notified and drives this SAME funded work; there is no detached
    /// async task or success inferred from an absent producer.
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
        // Publish the actual blocking Join synchronously whenever its slot
        // is available. If an observer currently holds the slot, its original
        // Join or the queued owned future remains retained for close to join.
        if let Ok(mut state) = self.state.try_lock()
            && matches!(
                *state,
                MetadataState::Pending | MetadataState::Complete { failed: true, .. }
            )
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
            Err(_) => MetadataState::Complete {
                error: Some(unavailable().into()),
                failed: true,
            },
        }
    }

    pub(crate) async fn run(
        &self,
        metadata: impl std::future::Future<Output = Result<()>> + Send + 'static,
    ) -> Result<()> {
        if std::mem::size_of_val(&metadata) > MAX_METADATA_FUTURE {
            let mut state = self.state.lock().await;
            if matches!(*state, MetadataState::Pending) {
                *state = MetadataState::Complete {
                    error: Some(anyhow::anyhow!(
                        "handle metadata payload exceeds its prepaid bound"
                    )),
                    failed: true,
                };
            }
            drop(state);
            self.ready.notify_waiters();
            bail!("handle metadata payload exceeds its prepaid bound")
        }
        // Keep unused captures outside the state guard even on a losing
        // concurrent observer; their actual frame/lease destruction may lock.
        let mut metadata = Some(metadata);
        let launch = {
            let mut state = self.state.lock().await;
            match &*state {
                MetadataState::Pending | MetadataState::Complete { failed: true, .. } => {
                    let metadata = metadata.take().expect("metadata taken twice");
                    let runtime = self.runtime.clone();
                    let funding = self
                        .task_funding
                        .lock()
                        .map_err(|_| unavailable())?
                        .as_ref()
                        .context("handle metadata task funding is missing")?
                        .clone();
                    match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                        self.runtime.spawn_blocking(move || {
                            let _funding = funding;
                            runtime.block_on(metadata)
                        })
                    })) {
                        Ok(task) => *state = MetadataState::Running(task),
                        Err(_) => {
                            *state = MetadataState::Complete {
                                error: Some(anyhow::Error::new(unavailable())),
                                failed: true,
                            };
                        }
                    }
                    true
                }
                _ => false,
            }
        };
        drop(metadata);
        if launch {
            self.ready.notify_waiters();
        }
        self.wait().await
    }

    pub(crate) async fn wait(&self) -> Result<()> {
        loop {
            let ready = self.ready.notified();
            tokio::pin!(ready);
            ready.as_mut().enable();
            if self.unregistered.load(Ordering::Acquire) {
                return Ok(());
            }
            let mut state = self.state.lock().await;
            match &mut *state {
                MetadataState::Pending => {
                    if let Some(metadata) = self.take_queued() {
                        *state = self.start_queued(metadata);
                        continue;
                    }
                    drop(state);
                    ready.await;
                }
                MetadataState::Running(task) => {
                    // Await by mutable reference. An abandoned waiter drops
                    // only this mutex guard; the original Join stays here.
                    let result = task.await.unwrap_or_else(|_| Err(unavailable().into()));
                    let failed = result.is_err();
                    *state = MetadataState::Complete {
                        error: result.err(),
                        failed,
                    };
                    if !failed {
                        // The actual Join resolved, including destruction of
                        // the metadata future and its original Grant/lease
                        // captures. Only the separately funded receipt stays.
                        let funding = self.task_funding.lock().map_err(|_| unavailable())?.take();
                        drop(funding);
                        self.settled.store(true, Ordering::Release);
                    }
                    // Return this exact attempt before another observer can
                    // admit its retry into the same prepaid slot.
                    if let MetadataState::Complete { error, failed } = &mut *state {
                        let result = if *failed {
                            Err(error.take().unwrap_or_else(|| unavailable().into()))
                        } else {
                            Ok(())
                        };
                        drop(state);
                        if result.is_ok() {
                            drop(self.take_queued());
                        }
                        return result;
                    }
                    unreachable!("joined metadata must have a completion receipt");
                }
                MetadataState::Complete { error, failed } => {
                    if *failed && let Some(metadata) = self.take_queued() {
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

    fn abandon_unregistered(&self) {
        // The admission guard cannot coexist with a published handle. Close
        // may already be observing Pending, so publish without acquiring its
        // async mutex or attempting an asynchronous Drop.
        self.unregistered.store(true, Ordering::Release);
        self.finish_unpublished_source(false);
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
    entries: Vec<Arc<HandleMetadata>>,
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
            .checked_mul(std::mem::size_of::<Arc<HandleMetadata>>())
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
                metadata.poll_source_cleanup();
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
                    .position(|entry| entry.fully_settled())
                    .map(|index| state.entries.swap_remove(index))
            };
            let Some(removed) = removed else { break };
            drop(removed);
        }
        let metadata = HandleMetadata::prepare(control)?;
        {
            let mut state = self.state.lock().map_err(|_| unavailable())?;
            if state.closed || state.entries.len() == MAX_SETTLEMENTS {
                bail!("handle source registration is closed or full")
            }
            state.entries.push(metadata.clone());
        }
        Ok(HandleMetadataAdmission {
            metadata,
            registered: false,
        })
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
                state.entries[index].clone()
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
        let host = self.host.clone();
        let outcome = tobkiri_gate::Outcome::Failed {
            code: self.failure.into(),
        };
        self.metadata
            .run(async move {
                crate::core_imports::finish_handle_metadata(&host, &entry, outcome).await
            })
            .await?;
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
        let host = self.host.clone();
        let outcome = tobkiri_gate::Outcome::Failed {
            code: self.failure.into(),
        };
        self.metadata.queue(async move {
            crate::core_imports::finish_handle_metadata(&host, &entry, outcome).await
        });
    }
}
