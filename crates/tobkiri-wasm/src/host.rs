use anyhow::{Context, Result, bail};
use async_trait::async_trait;
use std::{
    collections::{BTreeMap, BTreeSet},
    sync::{
        Arc, Mutex as StdMutex, OnceLock, Weak,
        atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering},
    },
};
use tobkiri_canon::ids::{OpName, PackHash, PackId, ProfileId, ShapeRef};
use tobkiri_flow::{CoreStream, FlowRunner, PortInvoker, StepError, StepOutput, StreamSource};
use tobkiri_gate::{
    CallCx, CapacityLedger, EffectRequest, Gate, HostIo, Outcome, Principal, ResourceLimits,
    WasmEntryKind,
};
use tobkiri_manifest::{PackCoreErrorBindings, RuntimeSpec, ShapeDef, VerifiedPack, WitFunction};
use tobkiri_store::{AuditLog, AuditQuota, DataRoot, TieredStore, Vault};
use tokio::sync::Mutex;
use wasmtime::Store;
use wasmtime::component::{Component, Instance, Linker, Val};
use wasmtime_wasi::{WasiCtxView, WasiView};

use crate::child_census::{
    GenerationChildCensus, GenerationRetirement, NativeGenerationId, NativeWorkCompletion,
    NativeWorkError, NativeWorkId, NativeWorkResult, UnstartedCleanupPolicy,
};
use crate::{
    HandleKind, HandleTable, InvocationState, RestrictedWasi, WasmEngine, validate_imports,
};

/// One immutable Profile dispatch plan. No domain knowledge is present here.
#[derive(Clone)]
pub struct PackHost {
    pub(crate) shared: Arc<Shared>,
}
pub(crate) struct Shared {
    pub(crate) engine: WasmEngine,
    pub(crate) profile: ProfileId,
    pub(crate) gate: Arc<dyn Gate>,
    pub(crate) capacity: CapacityLedger,
    pub(crate) io: Arc<dyn HostIo>,
    pub(crate) root: Arc<DataRoot>,
    pub(crate) stores: TieredStore,
    // Retains the opened log for this host's lifetime; Gate owns delivery.
    _audit: Arc<AuditLog>,
    pub(crate) vault: StdMutex<Option<Arc<Vault>>>,
    pub(crate) bindings: BTreeMap<ShapeRef, Vec<PackId>>,
    pub(crate) views: BTreeMap<ShapeRef, ShapeDef>,
    // Exact caller/provider view operation provenance, resolved once from
    // verified WIT. Structural record equality never enables error projection.
    pub(crate) core_error_results: BTreeMap<ShapeRef, BTreeSet<OpName>>,
    // Independent nominal evidence for each actual immutable runtime artifact,
    // separated by import/export direction. Installed before any activation.
    pub(crate) pack_core_errors: OnceLock<BTreeMap<PackId, PackCoreErrorBindings>>,
    pub(crate) verified: BTreeMap<PackId, Arc<VerifiedPack>>,
    pub(crate) instances: StdMutex<BTreeMap<PackId, Arc<InstanceSlot>>>,
    pub(crate) handles: StdMutex<HandleTable>,
    pub(crate) effective_limits: BTreeMap<(PackId, PackHash), ResourceLimits>,
    pub(crate) flow_runner: OnceLock<Weak<dyn FlowRunner>>,
    pub(crate) provider_entry: OnceLock<Arc<dyn crate::ProviderEntryBridge>>,
    pub(crate) filesystem: OnceLock<Arc<dyn crate::FilesystemAccess>>,
    pub(crate) control: tobkiri_gate::CoreControl,
    _catalog_control: tobkiri_gate::ControlReservation,
}

pub(crate) struct PackState {
    pub(crate) shared: Weak<Shared>,
    pub(crate) wasi: RestrictedWasi,
    pub(crate) owners: crate::owned_values::ValueOwners,
    pub(crate) frame: Option<Arc<dyn crate::ProviderFrame>>,
    // Installed with the actual Store, never resolved from a principal label.
    native_generation: GenerationChildCensus,
    generation_lifetime: Weak<StdMutex<GenerationLifetime>>,
}
impl WasiView for PackState {
    fn ctx(&mut self) -> WasiCtxView<'_> {
        self.wasi.ctx()
    }
}
/// Receipt for destruction of this exact generation's actual VM Store. It
/// certifies no separately scheduled native work; that proof remains separate.
pub(crate) struct StoreDisposal {
    done: AtomicBool,
    // Observation only: original retirement/inline cleanup still owns this
    // exact Store's disposal. Never VM, native or terminal completion.
    custodians: AtomicUsize,
    changed: tokio::sync::Notify,
    census: Option<GenerationChildCensus>,
    retirement: StdMutex<Option<GenerationRetirement>>,
    legacy_children: Arc<AtomicBool>,
    _reservation: tobkiri_gate::ControlReservation,
}
impl StoreDisposal {
    fn new(
        control: &tobkiri_gate::CoreControl,
        census: Option<GenerationChildCensus>,
        legacy_children: Arc<AtomicBool>,
    ) -> Result<Self> {
        let reservation = control.reserve(tobkiri_gate::ControlKind::Frame, 512)?;
        Ok(Self {
            done: AtomicBool::new(false),
            custodians: AtomicUsize::new(0),
            changed: tokio::sync::Notify::new(),
            census,
            retirement: StdMutex::new(None),
            legacy_children,
            _reservation: reservation,
        })
    }
    fn complete(&self) {
        self.done.store(true, Ordering::Release);
        self.changed.notify_waiters();
    }
    pub(crate) fn is_complete(&self) -> bool {
        self.done.load(Ordering::Acquire)
    }
    fn claim_owner(self: &Arc<Self>) -> Result<StoreDisposalOwner> {
        self.custodians
            .compare_exchange(0, 1, Ordering::AcqRel, Ordering::Acquire)
            .map_err(|_| anyhow::anyhow!("exact generation already has a disposal owner"))?;
        Ok(StoreDisposalOwner(self.clone()))
    }
    // Only the initializer holding this exact PackInstance's mutex may add
    // inline custody. It may overlap the original retirement worker waiting
    // for that mutex; an observer can never acquire or extend custody.
    fn claim_inline_owner(self: &Arc<Self>) -> Result<StoreDisposalOwner> {
        self.custodians
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |owners| {
                owners.checked_add(1)
            })
            .map_err(|_| anyhow::anyhow!("exact Store inline custody exhausted"))?;
        Ok(StoreDisposalOwner(self.clone()))
    }
    fn dispose_inline<T>(
        self: &Arc<Self>,
        store: T,
        seal: impl FnOnce() -> Result<()>,
    ) -> Result<()> {
        // The caller retains the exact PackInstance mutex. Ownership of its
        // actual Store has moved here, not to an observing or replacement task.
        let owner = self.claim_inline_owner();
        // A sealing panic must also destroy the Store before releasing its
        // custodian. Function parameters otherwise outlive later local guards.
        let store = store;
        let sealed = if owner.is_ok() {
            seal()
        } else {
            // Unknown custody must not publish a retirement or wake children.
            Ok(())
        };
        drop(store);
        self.complete();
        // Keep custody through actual destruction even when sealing failed.
        let _owner = owner?;
        sealed
    }
    async fn wait_owned_vm(&self) -> Result<()> {
        tokio::time::timeout(std::time::Duration::from_secs(2), async {
            loop {
                let changed = self.changed.notified();
                tokio::pin!(changed);
                changed.as_mut().enable();
                if self.is_complete() {
                    return Ok(());
                }
                if self.custodians.load(Ordering::Acquire) == 0 {
                    // Recheck after observing owner release: actual Store
                    // destruction may have preceded that release.
                    if self.is_complete() {
                        return Ok(());
                    }
                    bail!("exact generation has no live disposal owner")
                }
                changed.await;
            }
        })
        .await
        .context("owned generation Store destruction remains unknown")?
    }
    async fn wait_vm(&self) -> Result<()> {
        tokio::time::timeout(std::time::Duration::from_secs(2), async {
            loop {
                let changed = self.changed.notified();
                tokio::pin!(changed);
                changed.as_mut().enable();
                if self.is_complete() {
                    return;
                }
                changed.await;
            }
        })
        .await
        .context("exact generation Store destruction remains unknown")?;
        Ok(())
    }
    async fn wait(&self) -> Result<()> {
        self.wait_vm().await?;
        if self.census.is_some() {
            let retirement = self
                .retirement
                .try_lock()
                .map_err(|_| anyhow::anyhow!("native retirement state unavailable"))?
                .clone()
                .context("native generation has not been sealed")?;
            retirement.wait_physical().await?;
        }
        if self.legacy_children.load(Ordering::Acquire) {
            bail!("full generation contains native work without physical proof")
        }
        Ok(())
    }
}
/// Kept by the exact inline retirement or its original worker. Dropping a
/// failed/cancelled owner wakes observers, but never marks the Store disposed.
struct StoreDisposalOwner(Arc<StoreDisposal>);
impl Drop for StoreDisposalOwner {
    fn drop(&mut self) {
        if self.0.custodians.fetch_sub(1, Ordering::AcqRel) == 1 {
            self.0.changed.notify_waiters();
        }
    }
}
struct RetiredGeneration {
    token: tokio_util::sync::CancellationToken,
    generation: u64,
    disposal: Arc<StoreDisposal>,
    native: Option<GenerationRetirement>,
    operations: Vec<ProviderOperationScope>,
    _owner: StoreDisposalOwner,
}

struct PreparedDisposalTask {
    runtime: tokio::runtime::Handle,
    control: tobkiri_gate::ControlReservation,
}
impl PreparedDisposalTask {
    /// Every fallible task admission precedes the caller's retirement commit.
    fn prepare<T>(
        control: &tobkiri_gate::CoreControl,
        retire: impl FnOnce() -> Result<T>,
    ) -> Result<(Self, T)> {
        let control = control
            .reserve(tobkiri_gate::ControlKind::Task, 4096)
            .context("generation disposal task capacity unavailable before retirement")?;
        let runtime = tokio::runtime::Handle::try_current()
            .context("generation disposal runtime unavailable before retirement")?;
        let retirement = retire()?;
        Ok((Self { runtime, control }, retirement))
    }
}

pub(crate) struct GenerationLifetime {
    token: tokio_util::sync::CancellationToken,
    // Legacy native work without an actual registered physical future remains
    // unknown. Fully registered work is counted by disposal's exact census.
    native_children: Arc<AtomicBool>,
    disposal: Arc<StoreDisposal>,
    controls: Vec<GenerationCloseRegistration>,
    // The fixed backing drops before its index funding. Each identity Weak
    // independently carries the original operation Frame through deallocation.
    operations: Vec<GenerationOperationRegistration>,
    operation_index: Arc<GenerationOperationIndex>,
    retiring: bool,
}
struct GenerationCloseRegistration {
    control: Weak<dyn tobkiri_flow::StreamCloseControl>,
    // Retained until this generation's vector is destroyed, including its
    // dead weak entries and backing allocation. It is not refunded on close.
    _reservation: tobkiri_gate::ControlReservation,
}
struct GenerationOperationRegistration {
    scope: Option<ProviderOperationWeak>,
    index: Arc<GenerationOperationIndex>,
}
struct ProviderOperationWeak {
    // Drop ordering matters even after the last strong identity owner is gone.
    identity: Weak<ProviderOperationIdentity>,
    frame_control: Arc<tobkiri_gate::ControlReservation>,
}
struct GenerationOperationIndex {
    occupied: AtomicUsize,
    _control: Arc<tobkiri_gate::ControlReservation>,
}
impl GenerationOperationIndex {
    fn storage_bytes() -> Result<u64> {
        let entry = std::mem::size_of::<GenerationOperationRegistration>();
        let index_block = operation_arc_bytes::<Self>()?;
        let funding_block = operation_arc_bytes::<tobkiri_gate::ControlReservation>()?;
        let bytes = MAX_PROVIDER_OPERATIONS
            .checked_mul(entry)
            .and_then(|bytes| {
                bytes.checked_add(std::mem::size_of::<Vec<GenerationOperationRegistration>>())
            })
            .and_then(|bytes| bytes.checked_add(index_block))
            .and_then(|bytes| bytes.checked_add(funding_block))
            // Cover an extracted index entry and fixed funding-block overlap.
            // Actual identities use their one dynamically admitted Frame.
            .and_then(|bytes| bytes.checked_mul(2))
            .context("provider operation index bound overflow")?;
        u64::try_from(bytes).context("provider operation index bound overflow")
    }

    fn new(control: &tobkiri_gate::CoreControl) -> Result<Arc<Self>> {
        let reservation = control.reserve(
            tobkiri_gate::ControlKind::Registration,
            Self::storage_bytes()?,
        )?;
        Ok(Arc::new(Self {
            occupied: AtomicUsize::new(0),
            _control: Arc::new(reservation),
        }))
    }
}
impl GenerationOperationRegistration {
    fn reserve(index: &Arc<GenerationOperationIndex>) -> Result<Self> {
        index
            .occupied
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |occupied| {
                (occupied < MAX_PROVIDER_OPERATIONS).then_some(occupied + 1)
            })
            .map_err(|_| anyhow::anyhow!("generation provider operation capacity exhausted"))?;
        Ok(Self {
            scope: None,
            index: index.clone(),
        })
    }

    fn upgrade(&self) -> Option<ProviderOperationScope> {
        let scope = self.scope.as_ref()?;
        scope
            .identity
            .upgrade()
            .map(|inner| ProviderOperationScope {
                inner,
                frame_control: scope.frame_control.clone(),
            })
    }

    fn is_dead(&self) -> bool {
        self.scope
            .as_ref()
            .is_none_or(|scope| scope.identity.strong_count() == 0)
    }
}
impl Drop for GenerationOperationRegistration {
    fn drop(&mut self) {
        // This index is the ONLY Weak owner of an identity. Destroy that
        // allocation before another admission may reuse its prepaid slot.
        drop(self.scope.take());
        self.index.occupied.fetch_sub(1, Ordering::AcqRel);
    }
}
impl GenerationLifetime {
    fn new(
        control: &tobkiri_gate::CoreControl,
        census: Option<GenerationChildCensus>,
    ) -> Result<Self> {
        // Prepay both this fixed generation metadata and the independently
        // retained receipt before constructing either shared control block.
        let operation_index = GenerationOperationIndex::new(control)?;
        let mut operations = Vec::new();
        operations.try_reserve_exact(MAX_PROVIDER_OPERATIONS)?;
        if operations.capacity() != MAX_PROVIDER_OPERATIONS {
            bail!("provider operation index backing exceeds its fixed control bound")
        }
        let native_children = Arc::new(AtomicBool::new(false));
        let disposal = Arc::new(StoreDisposal::new(
            control,
            census,
            native_children.clone(),
        )?);
        Ok(Self {
            token: tokio_util::sync::CancellationToken::new(),
            native_children,
            disposal,
            controls: Vec::new(),
            operations,
            operation_index,
            retiring: false,
        })
    }
}

type NativePayload = std::pin::Pin<Box<dyn std::future::Future<Output = NativeWorkResult> + Send>>;

async fn native_mailbox(
    mut receiver: tokio::sync::oneshot::Receiver<NativePayload>,
    mailbox_closed: tokio_util::sync::CancellationToken,
) -> NativeWorkResult {
    let payload = tokio::select! {
        biased;
        payload = &mut receiver => payload.ok(),
        _ = mailbox_closed.cancelled() => receiver.try_recv().ok(),
    };
    match payload {
        Some(payload) => payload.await,
        // Neither channel closure nor an empty mailbox proves physical cleanup.
        None => Err(NativeWorkError::CompletionUnknown),
    }
}

struct NativeCloseState {
    requested: Option<tobkiri_flow::StreamCloseReason>,
    stream: Option<Weak<dyn tobkiri_flow::StreamCloseControl>>,
    cancel: Option<tobkiri_gate::CancellationToken>,
}
struct NativeCloseControl {
    state: StdMutex<NativeCloseState>,
    mailbox_closed: tokio_util::sync::CancellationToken,
    // The actual mailbox task retains this companion through payload drop.
    _reservation: Arc<tobkiri_gate::ControlReservation>,
}
impl tobkiri_flow::StreamCloseControl for NativeCloseControl {
    fn record_close(&self, reason: tobkiri_flow::StreamCloseReason) -> Result<(), StepError> {
        let (reason, stream, cancel) = {
            let mut state = self
                .state
                .try_lock()
                .map_err(|_| StepError::new("unavailable", "native close state unavailable"))?;
            let reason = *state.requested.get_or_insert(reason);
            (
                reason,
                state.stream.as_ref().and_then(Weak::upgrade),
                state.cancel.clone(),
            )
        };
        // Wake only after releasing native metadata guards. A close request is
        // never physical proof; the actual submitted body remains awaited.
        self.mailbox_closed.cancel();
        let recorded = stream.map_or(Ok(()), |stream| stream.record_close(reason));
        if let Some(cancel) = cancel {
            cancel.cancel();
        }
        recorded
    }
}
const MAX_NATIVE_CHILD_LINKS: usize = 256;
struct NativeChildLinks {
    sealed: bool,
    children: Vec<Arc<NativeCompletion>>,
}
/// Original own Join plus immutable, host-constructed descendant links. The
/// body's paired wrapper cleanup is deliberately a sibling, never its child.
struct NativeCompletion {
    own: NativeWorkCompletion,
    paired_body: Option<NativeWorkId>,
    children: StdMutex<NativeChildLinks>,
    unknown: AtomicBool,
    started: AtomicBool,
    result: Mutex<Option<Result<(), StepError>>>,
    _reservation: Arc<tobkiri_gate::ControlReservation>,
}
#[async_trait]
impl tobkiri_flow::RunTaskCompletion for NativeCompletion {
    async fn wait_started(&self) -> Result<(), StepError> {
        self.own.wait_started().await.map_err(|_| {
            StepError::new("unavailable", "native task publication remains unknown")
        })?;
        self.started.store(true, Ordering::Release);
        Ok(())
    }
    async fn wait(&self) -> Result<(), StepError> {
        let mut cached = self.result.lock().await;
        if let Some(result) = &*cached {
            return result.clone();
        }
        let mut unknown = self.own.wait_joined().await.is_err();
        let length = {
            let mut links = self
                .children
                .try_lock()
                .map_err(|_| StepError::new("unavailable", "native child receipts unavailable"))?;
            links.sealed = true;
            links.children.len()
        };
        for index in 0..length {
            let child = {
                let links = self.children.try_lock().map_err(|_| {
                    StepError::new("unavailable", "native child receipts unavailable")
                })?;
                links.children[index].clone()
            };
            if tobkiri_flow::RunTaskCompletion::wait(child.as_ref())
                .await
                .is_err()
            {
                unknown = true;
            }
        }
        unknown |= self.unknown.load(Ordering::Acquire);
        let result = if unknown {
            Err(StepError::new(
                "unavailable",
                "native task or descendant physical completion remains unknown",
            ))
        } else {
            Ok(())
        };
        *cached = Some(result.clone());
        result
    }
}
const MAX_PROVIDER_OPERATIONS: usize = 256;
const PROVIDER_OPERATION_CONTROL: u64 = 8192;

fn operation_arc_bytes<T>() -> Result<usize> {
    Ok(std::alloc::Layout::new::<[AtomicUsize; 2]>()
        .extend(std::alloc::Layout::new::<T>())?
        .0
        .pad_to_align()
        .size())
}

struct ProviderOperationRecord {
    // Inline backing belongs to the root registry Registration. The original
    // operation Frame externally outlives this payload's native Vec/Arc drops.
    data: ProviderOperationRecordData,
    _frame_control: Arc<tobkiri_gate::ControlReservation>,
}
struct ProviderOperationRecordData {
    parent: Option<usize>,
    closed: bool,
    unknown: Arc<AtomicBool>,
    native: Vec<Arc<NativeCompletion>>,
}
impl std::ops::Deref for ProviderOperationRecord {
    type Target = ProviderOperationRecordData;
    fn deref(&self) -> &Self::Target {
        &self.data
    }
}
impl std::ops::DerefMut for ProviderOperationRecord {
    fn deref_mut(&mut self) -> &mut Self::Target {
        &mut self.data
    }
}
struct ProviderOperationRegistry {
    state: StdMutex<Vec<ProviderOperationRecord>>,
    _reservation: tobkiri_gate::ControlReservation,
}
struct ProviderOperationIdentity {
    shared: Weak<Shared>,
    generation: NativeGenerationId,
    execution: crate::NativeExecutionId,
    registry: Arc<ProviderOperationRegistry>,
    index: usize,
    unknown: Arc<AtomicBool>,
    disposal: Arc<StoreDisposal>,
}

/// Opaque lifetime inventory minted before an actual provider runs guest code.
/// Core may bind it only to that private provider activation and inherit it at
/// its verified SourceOpen transition. Neither identifiers nor this inventory
/// authorize dispatch, allocation, source creation or terminal settlement.
#[derive(Clone)]
pub struct ProviderOperationScope {
    // Keep the original external operation Frame until this exact data Arc has
    // actually dropped. This Arc never escapes through a public Arc/Weak API.
    inner: Arc<ProviderOperationIdentity>,
    frame_control: Arc<tobkiri_gate::ControlReservation>,
}
impl ProviderOperationScope {
    pub fn execution_id(&self) -> &crate::NativeExecutionId {
        &self.inner.execution
    }

    fn storage_bytes(generation: &NativeGenerationId) -> Result<u64> {
        let funding_block = operation_arc_bytes::<tobkiri_gate::ControlReservation>()?;
        let bytes = operation_arc_bytes::<ProviderOperationIdentity>()?
            .checked_add(operation_arc_bytes::<AtomicBool>()?)
            .and_then(|bytes| bytes.checked_add(std::mem::size_of::<ProviderOperationRecord>()))
            .and_then(|bytes| bytes.checked_add(std::mem::size_of::<Self>()))
            .and_then(|bytes| bytes.checked_add(generation.profile.clone_storage_bytes()))
            .and_then(|bytes| bytes.checked_add(generation.pack.clone_storage_bytes()))
            .and_then(|bytes| {
                bytes.checked_add(
                    MAX_NATIVE_CHILD_LINKS
                        .checked_mul(std::mem::size_of::<Arc<NativeCompletion>>())?,
                )
            })
            .and_then(|bytes| bytes.checked_add(funding_block))
            // Bound simultaneous construction/destruction overlap as well as
            // aligned Arc headers. Child work has its own existing custody.
            .and_then(|bytes| bytes.checked_mul(2))
            .context("provider operation control bound overflow")?;
        u64::try_from(bytes).context("provider operation control bound overflow")
    }

    fn new(
        control: &tobkiri_gate::CoreControl,
        shared: Weak<Shared>,
        generation: &NativeGenerationId,
        execution: crate::NativeExecutionId,
        disposal: Arc<StoreDisposal>,
        parent: Option<Self>,
    ) -> Result<Self> {
        if Self::storage_bytes(generation)? > PROVIDER_OPERATION_CONTROL {
            bail!("provider operation metadata exceeds its fixed control bound")
        }
        let frame_control = Arc::new(
            control.reserve(tobkiri_gate::ControlKind::Frame, PROVIDER_OPERATION_CONTROL)?,
        );
        let mut native = Vec::new();
        native.try_reserve_exact(MAX_NATIVE_CHILD_LINKS)?;
        if native.capacity() != MAX_NATIVE_CHILD_LINKS {
            bail!("provider native root backing exceeds its fixed control bound")
        }
        let registry = match &parent {
            Some(parent) => {
                if !parent.inner.shared.ptr_eq(&shared) {
                    bail!("provider operation parent belongs to another host")
                }
                parent.inner.registry.clone()
            }
            None => {
                let bytes = std::mem::size_of::<ProviderOperationRegistry>()
                    .checked_add(
                        MAX_PROVIDER_OPERATIONS
                            .checked_mul(std::mem::size_of::<ProviderOperationRecord>())
                            .context("provider operation inventory bound overflow")?,
                    )
                    .and_then(|bytes| bytes.checked_add(2048))
                    .context("provider operation inventory bound overflow")?;
                let reservation =
                    control.reserve(tobkiri_gate::ControlKind::Registration, bytes as u64)?;
                let mut records = Vec::new();
                records.try_reserve_exact(MAX_PROVIDER_OPERATIONS)?;
                Arc::new(ProviderOperationRegistry {
                    state: StdMutex::new(records),
                    _reservation: reservation,
                })
            }
        };
        let unknown = Arc::new(AtomicBool::new(false));
        let index = {
            let mut records = registry
                .state
                .try_lock()
                .map_err(|_| anyhow::anyhow!("provider operation inventory busy"))?;
            if records.len() == MAX_PROVIDER_OPERATIONS {
                bail!("provider operation inventory capacity exhausted")
            }
            let parent = parent.as_ref().map(|scope| scope.inner.index);
            if parent.is_some_and(|index| records[index].closed) {
                bail!("provider operation ancestor is closing")
            }
            let index = records.len();
            records.push(ProviderOperationRecord {
                data: ProviderOperationRecordData {
                    parent,
                    closed: false,
                    unknown: unknown.clone(),
                    native,
                },
                _frame_control: frame_control.clone(),
            });
            index
        };
        Ok(Self {
            inner: Arc::new(ProviderOperationIdentity {
                shared,
                generation: generation.clone(),
                execution,
                registry,
                index,
                unknown,
                disposal,
            }),
            frame_control,
        })
    }

    fn weak_for_index(&self) -> ProviderOperationWeak {
        ProviderOperationWeak {
            identity: Arc::downgrade(&self.inner),
            frame_control: self.frame_control.clone(),
        }
    }

    fn descendant(records: &[ProviderOperationRecord], mut index: usize, ancestor: usize) -> bool {
        loop {
            if index == ancestor {
                return true;
            }
            match records[index].parent {
                Some(parent) => index = parent,
                None => return false,
            }
        }
    }

    /// Only native admission metadata changes under this guard. Every actual
    /// submission takes this SAME registry guard, so seal cannot lose an
    /// accepted future or allow a descendant publication afterwards.
    fn seal(&self) -> Result<()> {
        let mut records = self
            .inner
            .registry
            .state
            .try_lock()
            .map_err(|_| anyhow::anyhow!("provider operation inventory busy"))?;
        if records[self.inner.index].closed {
            return Ok(());
        }
        for index in self.inner.index..records.len() {
            if Self::descendant(&records, index, self.inner.index) {
                records[index].closed = true;
            }
        }
        Ok(())
    }

    fn notify_native_close(&self) -> Result<()> {
        let length = self
            .inner
            .registry
            .state
            .try_lock()
            .map_err(|_| anyhow::anyhow!("provider operation inventory busy"))?
            .len();
        let mut failed = false;
        for index in self.inner.index..length {
            let count = {
                let records = self
                    .inner
                    .registry
                    .state
                    .try_lock()
                    .map_err(|_| anyhow::anyhow!("provider operation inventory busy"))?;
                if !records[self.inner.index].closed {
                    bail!("provider operation has not been sealed")
                }
                if Self::descendant(&records, index, self.inner.index) {
                    records[index].native.len()
                } else {
                    0
                }
            };
            for child in 0..count {
                let completion =
                    {
                        let records =
                            self.inner.registry.state.try_lock().map_err(|_| {
                                anyhow::anyhow!("provider operation inventory busy")
                            })?;
                        records[index].native[child].clone()
                    };
                // No generation, operation, Core, table or Store guard here.
                failed |= completion
                    .own
                    .notify_close(tobkiri_flow::StreamCloseReason::Retired)
                    .is_err();
            }
        }
        if failed {
            bail!("provider native close notification remains uncertain")
        }
        Ok(())
    }

    async fn wait_native(identity: &ProviderOperationIdentity) -> Result<(), StepError> {
        let length = identity
            .registry
            .state
            .try_lock()
            .map_err(|_| StepError::new("unavailable", "provider operation inventory busy"))?
            .len();
        let mut unknown = false;
        for index in identity.index..length {
            let count = {
                let records = identity.registry.state.try_lock().map_err(|_| {
                    StepError::new("unavailable", "provider operation inventory busy")
                })?;
                if !records[identity.index].closed {
                    return Err(StepError::new(
                        "unavailable",
                        "provider operation has not been sealed",
                    ));
                }
                if Self::descendant(&records, index, identity.index) {
                    unknown |= records[index].unknown.load(Ordering::Acquire);
                    records[index].native.len()
                } else {
                    0
                }
            };
            for child in 0..count {
                let completion = {
                    let records = identity.registry.state.try_lock().map_err(|_| {
                        StepError::new("unavailable", "provider operation inventory busy")
                    })?;
                    records[index].native[child].clone()
                };
                unknown |= tobkiri_flow::RunTaskCompletion::wait(completion.as_ref())
                    .await
                    .is_err();
            }
        }
        if unknown {
            Err(StepError::new(
                "unavailable",
                "provider native descendant completion remains unknown",
            ))
        } else {
            Ok(())
        }
    }

    fn physical_completion(&self) -> tobkiri_flow::StreamPhysicalCompletion {
        tobkiri_flow::StreamPhysicalCompletion::with_control(
            self.inner.clone(),
            self.frame_control.clone(),
        )
    }
}
#[async_trait]
impl tobkiri_flow::RunTaskCompletion for ProviderOperationIdentity {
    async fn wait(&self) -> Result<(), StepError> {
        // This is independent VM destruction, NEVER full generation retirement.
        self.disposal.wait_vm().await.map_err(|_| {
            StepError::new("unavailable", "source Store destruction remains unknown")
        })?;
        ProviderOperationScope::wait_native(self).await
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum NativeTaskRole {
    Source,
    Interpreter,
    Cleanup,
}
struct NativeSpawnerInner {
    operation: ProviderOperationScope,
    generation: GenerationChildCensus,
    legacy_children: Arc<AtomicBool>,
    lifetime: Weak<StdMutex<GenerationLifetime>>,
    sender: StdMutex<Option<tokio::sync::oneshot::Sender<NativePayload>>>,
    close: Arc<NativeCloseControl>,
    original_cancel: tobkiri_gate::CancellationToken,
    completion: Arc<NativeCompletion>,
    work: NativeWorkId,
    execution: crate::NativeExecutionId,
    role: NativeTaskRole,
    parent: Option<Weak<NativeCompletion>>,
    paired_body: Option<NativeWorkId>,
    paired_cleanup: StdMutex<Option<Arc<NativeWorkSpawner>>>,
    paired_completion: StdMutex<Option<Arc<NativeCompletion>>>,
    cleanup_taken: AtomicBool,
    control: tobkiri_gate::CoreControl,
}

/// One pre-admitted mailbox bound to the actual Store generation and original
/// Core activation. It can submit only one actual native future.
#[derive(Clone)]
pub(crate) struct NativeWorkSpawner {
    inner: Arc<NativeSpawnerInner>,
}
impl NativeWorkSpawner {
    #[allow(clippy::too_many_arguments)]
    fn admit(
        control: &tobkiri_gate::CoreControl,
        operation: ProviderOperationScope,
        generation: GenerationChildCensus,
        lifetime: Weak<StdMutex<GenerationLifetime>>,
        legacy_children: Arc<AtomicBool>,
        execution: crate::NativeExecutionId,
        original_cancel: tobkiri_gate::CancellationToken,
        role: NativeTaskRole,
        parent: Option<Weak<NativeCompletion>>,
        paired_body: Option<NativeWorkId>,
    ) -> Result<Self> {
        let reservation = Arc::new(control.reserve(tobkiri_gate::ControlKind::Frame, 8192)?);
        let mut children = Vec::new();
        children.try_reserve_exact(MAX_NATIVE_CHILD_LINKS)?;
        let close = Arc::new(NativeCloseControl {
            state: StdMutex::new(NativeCloseState {
                requested: None,
                stream: None,
                cancel: (role != NativeTaskRole::Source).then(|| original_cancel.clone()),
            }),
            mailbox_closed: tokio_util::sync::CancellationToken::new(),
            _reservation: reservation.clone(),
        });
        let (sender, receiver) = tokio::sync::oneshot::channel::<NativePayload>();
        let work = generation.admit_and_spawn_prepared(
            execution.clone(),
            Some(close.clone()),
            UnstartedCleanupPolicy::RequiresCompletion,
            native_mailbox(receiver, close.mailbox_closed.clone()),
        )?;
        let completion = Arc::new(NativeCompletion {
            own: generation.work_completion(work)?,
            paired_body,
            children: StdMutex::new(NativeChildLinks {
                sealed: false,
                children,
            }),
            unknown: AtomicBool::new(false),
            started: AtomicBool::new(false),
            result: Mutex::new(None),
            _reservation: reservation,
        });
        Ok(Self {
            inner: Arc::new(NativeSpawnerInner {
                operation,
                generation,
                legacy_children,
                lifetime,
                sender: StdMutex::new(Some(sender)),
                close,
                original_cancel,
                completion,
                work,
                execution,
                role,
                parent,
                paired_body,
                paired_cleanup: StdMutex::new(None),
                paired_completion: StdMutex::new(None),
                cleanup_taken: AtomicBool::new(false),
                control: control.clone(),
            }),
        })
    }

    fn validate_generation(&self, generation: &GenerationLifetime) -> Result<()> {
        if generation.retiring
            || generation.token.is_cancelled()
            || !generation
                .disposal
                .census
                .as_ref()
                .is_some_and(|current| current.generation() == self.inner.generation.generation())
        {
            bail!("native generation retired")
        }
        Ok(())
    }

    fn execution_key(execution: tobkiri_flow::RunTaskExecution) -> crate::NativeExecutionId {
        crate::NativeExecutionId {
            frame: execution.frame,
            request: execution.request,
            activation: execution.activation,
        }
    }

    fn admit_child_pair(
        &self,
        execution: tobkiri_flow::RunTaskExecution,
        cancel: tobkiri_gate::CancellationToken,
    ) -> Result<Arc<dyn tobkiri_flow::RunTaskSpawner>, StepError> {
        // Core invokes this native-Flow lifetime entry outside Store and
        // authority/lifecycle guards. Never move maintenance into admit().
        self.inner
            .generation
            .reap_completed()
            .map_err(|_| StepError::new("unavailable", "native child maintenance unavailable"))?;
        let mut body = None;
        let mut cleanup = None;
        let admitted = (|| -> Result<Arc<dyn tobkiri_flow::RunTaskSpawner>> {
            if self.inner.role == NativeTaskRole::Cleanup
                || !cancel.inherits(&self.inner.original_cancel)
            {
                bail!("invalid native child lifetime")
            }
            let lifetime = self
                .inner
                .lifetime
                .upgrade()
                .context("native generation lost")?;
            let generation = lifetime
                .try_lock()
                .map_err(|_| anyhow::anyhow!("generation state busy"))?;
            self.validate_generation(&generation)?;
            let operations = self
                .inner
                .operation
                .inner
                .registry
                .state
                .try_lock()
                .map_err(|_| anyhow::anyhow!("provider operation inventory busy"))?;
            if operations[self.inner.operation.inner.index].closed {
                bail!("provider operation is closing")
            }
            let mut links = self
                .inner
                .completion
                .children
                .try_lock()
                .map_err(|_| anyhow::anyhow!("native child receipts busy"))?;
            if links.sealed || links.children.len() > MAX_NATIVE_CHILD_LINKS - 2 {
                bail!("native child receipt capacity exhausted")
            }
            let execution = Self::execution_key(execution);
            let parent = Some(Arc::downgrade(&self.inner.completion));
            body = Some(Arc::new(Self::admit(
                &self.inner.control,
                self.inner.operation.clone(),
                self.inner.generation.clone(),
                self.inner.lifetime.clone(),
                self.inner.legacy_children.clone(),
                execution.clone(),
                cancel.clone(),
                NativeTaskRole::Interpreter,
                parent.clone(),
                None,
            )?));
            let body_ref = body.as_ref().expect("native body absent");
            cleanup = Some(Arc::new(Self::admit(
                &self.inner.control,
                self.inner.operation.clone(),
                self.inner.generation.clone(),
                self.inner.lifetime.clone(),
                self.inner.legacy_children.clone(),
                execution,
                cancel,
                NativeTaskRole::Cleanup,
                parent,
                Some(body_ref.inner.work),
            )?));
            let cleanup_ref = cleanup.as_ref().expect("native cleanup absent");
            if body_ref.inner.work.get() <= self.inner.work.get()
                || cleanup_ref.inner.work.get() <= body_ref.inner.work.get()
            {
                bail!("native child identities are not strictly descending")
            }
            *body_ref
                .inner
                .paired_cleanup
                .try_lock()
                .map_err(|_| anyhow::anyhow!("native cleanup pair unavailable"))? =
                Some(cleanup_ref.clone());
            *body_ref
                .inner
                .paired_completion
                .try_lock()
                .map_err(|_| anyhow::anyhow!("native cleanup receipt unavailable"))? =
                Some(cleanup_ref.inner.completion.clone());
            // Both immutable siblings are linked BEFORE the body spawner can
            // escape/start. No public API can inject or reparent a receipt.
            links.children.push(body_ref.inner.completion.clone());
            links.children.push(cleanup_ref.inner.completion.clone());
            Ok(body_ref.clone())
        })();
        // Never release a candidate's last sender/control under these guards.
        drop(body);
        drop(cleanup);
        if admitted.is_err() {
            self.inner.completion.unknown.store(true, Ordering::Release);
        }
        admitted.map_err(|_| StepError::new("unavailable", "native child pair admission failed"))
    }

    fn take_cleanup_pair(
        &self,
        execution: tobkiri_flow::RunTaskExecution,
        cancel: tobkiri_gate::CancellationToken,
    ) -> Result<Arc<dyn tobkiri_flow::RunTaskSpawner>, StepError> {
        let mut candidate = None;
        let admitted = (|| -> Result<Arc<dyn tobkiri_flow::RunTaskSpawner>> {
            if self.inner.role == NativeTaskRole::Cleanup
                || !cancel.inherits(&self.inner.original_cancel)
            {
                bail!("invalid native cleanup lifetime")
            }
            let lifetime = self
                .inner
                .lifetime
                .upgrade()
                .context("native generation lost")?;
            let generation = lifetime
                .try_lock()
                .map_err(|_| anyhow::anyhow!("generation state busy"))?;
            self.validate_generation(&generation)?;
            let mut operations = self
                .inner
                .operation
                .inner
                .registry
                .state
                .try_lock()
                .map_err(|_| anyhow::anyhow!("provider operation inventory busy"))?;
            if operations[self.inner.operation.inner.index].closed {
                bail!("provider operation is closing")
            }
            let sender = self
                .inner
                .sender
                .try_lock()
                .map_err(|_| anyhow::anyhow!("native submission busy"))?;
            if sender.is_none() || self.inner.cleanup_taken.swap(true, Ordering::AcqRel) {
                bail!("native cleanup pair must be taken once before body start")
            }
            let execution = Self::execution_key(execution);
            match self.inner.role {
                NativeTaskRole::Interpreter => {
                    if execution != self.inner.execution {
                        bail!("native cleanup execution differs from paired body")
                    }
                    let paired = self
                        .inner
                        .paired_cleanup
                        .try_lock()
                        .map_err(|_| anyhow::anyhow!("native cleanup pair unavailable"))?
                        .take()
                        .context("native cleanup pair missing")?;
                    candidate = Some(paired.clone());
                    if paired.inner.paired_body != Some(self.inner.work) {
                        bail!("native cleanup body differs")
                    }
                    match (&self.inner.parent, &paired.inner.parent) {
                        (Some(body_parent), Some(cleanup_parent))
                            if Weak::ptr_eq(body_parent, cleanup_parent) => {}
                        _ => bail!("native cleanup sibling parent differs"),
                    }
                    Ok(paired)
                }
                NativeTaskRole::Source => {
                    if operations[self.inner.operation.inner.index].native.len()
                        == MAX_NATIVE_CHILD_LINKS
                    {
                        bail!("provider native root capacity exhausted")
                    }
                    // Initial guest body was admitted by the actual Store
                    // before Core entry. Its cleanup has no containing body
                    // parent; the exact generation still counts both records.
                    candidate = Some(Arc::new(Self::admit(
                        &self.inner.control,
                        self.inner.operation.clone(),
                        self.inner.generation.clone(),
                        self.inner.lifetime.clone(),
                        self.inner.legacy_children.clone(),
                        execution,
                        cancel,
                        NativeTaskRole::Cleanup,
                        None,
                        Some(self.inner.work),
                    )?));
                    *self
                        .inner
                        .paired_completion
                        .try_lock()
                        .map_err(|_| anyhow::anyhow!("native cleanup pair unavailable"))? =
                        candidate
                            .as_ref()
                            .map(|cleanup| cleanup.inner.completion.clone());
                    operations[self.inner.operation.inner.index].native.push(
                        candidate
                            .as_ref()
                            .expect("native cleanup absent")
                            .inner
                            .completion
                            .clone(),
                    );
                    Ok(candidate.as_ref().expect("native cleanup absent").clone())
                }
                NativeTaskRole::Cleanup => unreachable!(),
            }
        })();
        drop(candidate);
        if admitted.is_err() {
            self.inner.completion.unknown.store(true, Ordering::Release);
        }
        admitted.map_err(|_| StepError::new("unavailable", "native cleanup pair admission failed"))
    }

    pub(crate) fn bind_cancel(&self, cancel: tobkiri_gate::CancellationToken) -> Result<()> {
        if !cancel.inherits(&self.inner.original_cancel) {
            bail!("native work cancellation differs from original execution")
        }
        let requested = {
            let mut state = self
                .inner
                .close
                .state
                .try_lock()
                .map_err(|_| anyhow::anyhow!("native close state unavailable"))?;
            if state.cancel.is_some() {
                bail!("native cancellation already bound")
            }
            state.cancel = Some(cancel.clone());
            state.requested
        };
        if requested.is_some() {
            cancel.cancel();
        }
        Ok(())
    }
    pub(crate) fn spawn<F>(self, future: F) -> Result<()>
    where
        F: std::future::Future<Output = NativeWorkResult> + Send + 'static,
    {
        self.submit(future)
    }
    fn submit<F>(&self, future: F) -> Result<()>
    where
        F: std::future::Future<Output = NativeWorkResult> + Send + 'static,
    {
        let bytes = std::mem::size_of::<F>();
        if bytes > 256 * 1024 {
            bail!("native payload exceeds its fixed bound")
        }
        let reservation = self.inner.control.reserve(
            tobkiri_gate::ControlKind::Task,
            (bytes as u64)
                .checked_add(4096)
                .context("native payload bound overflow")?,
        )?;
        // Reserve before either box. The payload owns the reservation through
        // its actual destruction, including cancellation and rejected submit.
        let mut future = Box::pin(future);
        let operation = self.inner.operation.clone();
        let payload: NativePayload = Box::pin(async move {
            let result = future.as_mut().await;
            drop(future);
            drop(reservation);
            drop(operation);
            result
        });
        let mut payload = Some(payload);
        let submitted = (|| -> Result<()> {
            let lifetime = self
                .inner
                .lifetime
                .upgrade()
                .context("native generation lost")?;
            let generation = lifetime
                .try_lock()
                .map_err(|_| anyhow::anyhow!("generation state busy"))?;
            if generation.retiring
                || generation.token.is_cancelled()
                || !generation.disposal.census.as_ref().is_some_and(|current| {
                    current.generation() == self.inner.generation.generation()
                })
            {
                bail!("native generation retired before submission")
            }
            let operations = self
                .inner
                .operation
                .inner
                .registry
                .state
                .try_lock()
                .map_err(|_| anyhow::anyhow!("provider operation inventory busy"))?;
            if operations[self.inner.operation.inner.index].closed {
                bail!("provider operation closed before submission")
            }
            let mut sender = self
                .inner
                .sender
                .try_lock()
                .map_err(|_| anyhow::anyhow!("native submission state unavailable"))?;
            let sender = sender.take().context("native future already submitted")?;
            match sender.send(payload.take().expect("native payload taken twice")) {
                Ok(()) => Ok(()),
                Err(rejected) => {
                    payload = Some(rejected);
                    bail!("native mailbox unavailable")
                }
            }
        })();
        // Rejection destroys captured work only after ALL generation/channel
        // guards have gone. Default RequiresCompletion preserves uncertainty.
        drop(payload);
        submitted
    }
}
#[async_trait]
impl tobkiri_flow::RunTaskCompletion for NativeWorkCompletion {
    async fn wait_started(&self) -> Result<(), StepError> {
        NativeWorkCompletion::wait_started(self)
            .await
            .map_err(|_| StepError::new("unavailable", "native task publication remains unknown"))
    }
    async fn wait(&self) -> Result<(), StepError> {
        self.wait_joined()
            .await
            .map_err(|_| StepError::new("unavailable", "native interpreter Join remains unknown"))
    }
}
impl tobkiri_flow::RunTaskSpawner for NativeWorkSpawner {
    fn child(
        &self,
        execution: tobkiri_flow::RunTaskExecution,
        cancel: tobkiri_gate::CancellationToken,
    ) -> Result<Arc<dyn tobkiri_flow::RunTaskSpawner>, StepError> {
        self.admit_child_pair(execution, cancel)
    }
    fn cleanup_child(
        &self,
        execution: tobkiri_flow::RunTaskExecution,
        cancel: tobkiri_gate::CancellationToken,
    ) -> Result<Arc<dyn tobkiri_flow::RunTaskSpawner>, StepError> {
        self.take_cleanup_pair(execution, cancel)
    }
    fn completion(&self) -> Result<Arc<dyn tobkiri_flow::RunTaskCompletion>, StepError> {
        Ok(self.inner.completion.clone())
    }
    fn abandon_unstarted(&self) -> Result<(), StepError> {
        // This captured mailbox is one-use; no current-generation lookup or
        // new admission is needed to withdraw its still-unaccepted sender.
        // Submission takes this same mutex, so an already accepted payload is
        // untouched. Neither closing input nor its normal exit proves cleanup.
        let sender = self
            .inner
            .sender
            .try_lock()
            .map_err(|_| StepError::new("unavailable", "native submission state unavailable"))?
            .take();
        if let Some(sender) = sender {
            self.inner.completion.unknown.store(true, Ordering::Release);
            drop(sender);
            self.inner.close.mailbox_closed.cancel();
        }
        Ok(())
    }
    fn before_unregistered_child(&self) -> Result<(), StepError> {
        // This exact immutable-generation flag only loses proof. Latch before
        // ANY fallible lifecycle read so dropping a cancelled execute future
        // cannot escape uncertainty when retirement/lock contention won.
        self.inner.legacy_children.store(true, Ordering::Release);
        self.inner
            .operation
            .inner
            .unknown
            .store(true, Ordering::Release);
        self.inner.completion.unknown.store(true, Ordering::Release);
        let checked = (|| -> Result<()> {
            let lifetime = self
                .inner
                .lifetime
                .upgrade()
                .context("native generation lost")?;
            let generation = lifetime
                .try_lock()
                .map_err(|_| anyhow::anyhow!("generation state busy"))?;
            if generation.retiring
                || generation.token.is_cancelled()
                || !generation.disposal.census.as_ref().is_some_and(|current| {
                    current.generation() == self.inner.generation.generation()
                })
            {
                bail!("unregistered child generation retired")
            }
            Ok(())
        })();
        checked.map_err(|_| {
            StepError::new("unavailable", "unregistered child cleanup remains unknown")
        })
    }
    fn spawn(
        &self,
        task: tobkiri_flow::RunTask,
        close: Arc<dyn tobkiri_flow::StreamCloseControl>,
    ) -> Result<Arc<dyn tobkiri_flow::RunTaskCompletion>, StepError> {
        if self.inner.role != NativeTaskRole::Cleanup {
            let paired =
                self.inner.paired_completion.try_lock().map_err(|_| {
                    StepError::new("unavailable", "native cleanup pair unavailable")
                })?;
            if !self.inner.cleanup_taken.load(Ordering::Acquire)
                || !paired.as_ref().is_some_and(|cleanup| {
                    cleanup.paired_body == Some(self.inner.work)
                        && cleanup.started.load(Ordering::Acquire)
                })
            {
                return Err(StepError::new(
                    "unavailable",
                    "paired cleanup must own its actual Join before body start",
                ));
            }
        }
        let requested =
            {
                let mut state =
                    self.inner.close.state.try_lock().map_err(|_| {
                        StepError::new("unavailable", "native close state unavailable")
                    })?;
                if state.stream.is_some() {
                    return Err(StepError::new("invalid", "native task already bound"));
                }
                state.stream = Some(Arc::downgrade(&close));
                state.requested
            };
        if let Some(reason) = requested {
            close.record_close(reason)?;
        }
        self.submit(async move {
            let result = task.await.map_err(|_| NativeWorkError::CompletionUnknown);
            drop(close);
            result
        })
        .map_err(|_| StepError::new("unavailable", "native interpreter submission failed"))?;
        Ok(self.inner.completion.clone())
    }
}

struct PackInstance {
    pack: Arc<VerifiedPack>,
    component: Component,
    config: Vec<u8>,
    live: Option<(Store<InvocationState<PackState>>, Instance)>,
    // Field order is deliberate: Store is destroyed before this external
    // physical lease, including on ordinary PackInstance destruction.
    physical: Option<Arc<crate::instance_capacity::InstanceCapacity>>,
    retirement: Arc<StdMutex<GenerationLifetime>>,
    generation: Arc<AtomicU64>,
    current_generation: u64,
    store_disposal: Arc<StoreDisposal>,
}
pub(crate) struct InstanceSlot {
    instance: Mutex<PackInstance>,
    retirement: Arc<StdMutex<GenerationLifetime>>,
    sources: BTreeMap<HandleKind, &'static str>,
    generation: Arc<AtomicU64>,
}

fn checked_provider_edges(bindings: &BTreeMap<ShapeRef, Vec<PackId>>) -> Result<u64> {
    let mut edges = 0u64;
    for providers in bindings.values() {
        // A frozen catalog contains at most 64 distinct Packs. Check length
        // before examining an attacker-sized vector or allocating a set.
        if providers.len() > 64 {
            bail!("bounded binding provider count exceeded")
        }
        edges = edges
            .checked_add(providers.len() as u64)
            .context("binding provider edge count overflow")?;
        if edges > 16_384 {
            bail!("bounded binding provider edge catalog exceeded")
        }
        for (index, provider) in providers.iter().enumerate() {
            if providers[..index].contains(provider) {
                bail!("duplicate provider in frozen binding")
            }
        }
    }
    Ok(edges)
}

fn pack_wit_size(pack: &VerifiedPack) -> Result<(usize, usize)> {
    let mut documents = 0usize;
    let mut bytes = 0usize;
    for (_, input) in pack.files.iter().filter(|(path, _)| path.ends_with(".wit")) {
        documents = documents
            .checked_add(1)
            .context("artifact WIT document count overflow")?;
        bytes = bytes
            .checked_add(input.len())
            .context("artifact WIT input size overflow")?;
        if input.len() > 1024 * 1024 || documents > 32 || bytes > 4 * 1024 * 1024 {
            bail!("bounded verified WIT dependency input exceeded")
        }
    }
    Ok((documents, bytes))
}

impl PackHost {
    /// Borrow the already opened profile AuditLog. This mints no authority;
    /// guest execution still requires its original typed Audit dispatch.
    pub(crate) fn audit_log(&self) -> &Arc<AuditLog> {
        &self.shared._audit
    }

    /// Install the trusted Core root registry and current-policy token issuer.
    /// An absent registry leaves every guest filesystem operation unavailable.
    pub fn set_filesystem_access(
        &self,
        filesystem: Arc<dyn crate::FilesystemAccess>,
    ) -> Result<()> {
        self.shared
            .filesystem
            .set(filesystem)
            .map_err(|_| anyhow::anyhow!("filesystem registry already installed"))
    }
    /// Reserve actual factory/frame/control storage before its first owned
    /// allocation. Core supplies its private on-stack verifier for one real
    /// consumed entry; metadata or an owner descriptor cannot sponsor storage.
    ///
    /// The caller must retain this reservation outside the allocated payload
    /// through physical payload/control-block destruction. This does not cover
    /// the ledger/OS-proof registries' separately documented custody gaps.
    pub fn reserve_frame_control(
        &self,
        cx: &CallCx,
        prepared: &dyn tobkiri_gate::PreparedFrameVerifier,
        reason: tobkiri_gate::AllocationReason,
        bytes: u64,
    ) -> Result<tobkiri_gate::CapacityReservation> {
        if bytes == 0 {
            bail!("frame control reservation must fund real storage")
        }
        self.shared.capacity.check_domain()?;
        let mut reserved = None;
        self.shared
            .gate
            .with_frame_preparation(cx, prepared, reason, &mut |funding| {
                if reserved.is_some() {
                    return Err(tobkiri_gate::GateError::Unavailable);
                }
                let metadata = funding.metadata;
                let (proof, hash) = match metadata.owner {
                    tobkiri_gate::PreparedCodeOwner::Pack { pack, hash } => (
                        self.shared
                            .root
                            .owner_lease(cx.profile(), pack)
                            .map_err(|_| tobkiri_gate::GateError::Unavailable)?,
                        Some(hash.clone()),
                    ),
                    tobkiri_gate::PreparedCodeOwner::User { .. }
                    | tobkiri_gate::PreparedCodeOwner::UserDocument { .. } => (
                        self.shared
                            .root
                            .user_owner_lease(cx.profile())
                            .map_err(|_| tobkiri_gate::GateError::Unavailable)?,
                        None,
                    ),
                };
                // Real physical owner proof precedes the allocation. The
                // callback is fixed ledger work, with no Core reentry or await.
                let owner = tobkiri_gate::AllocationOwnerLease::owner(&proof).clone();
                let binding = self.shared.capacity.bind_owner(Arc::new(proof))?;
                let reservation = self.shared.capacity.reserve(
                    owner,
                    tobkiri_gate::CapacityProvenance {
                        hash,
                        revision: cx
                            .revision()
                            .cloned()
                            .ok_or(tobkiri_gate::GateError::Unavailable)?,
                        security_epoch: cx.security_epoch(),
                        request: metadata.caller.request().clone(),
                        store: None,
                        session: Some(metadata.frame.clone()),
                    },
                    tobkiri_gate::CapacityKind::Bookkeeping,
                    bytes,
                    funding.limit,
                )?;
                drop(binding);
                reserved = Some(reservation);
                Ok(())
            })?;
        reserved.context("Core preparation did not reserve control storage")
    }

    /// Install Core's prepaid same-factory entry bridge exactly once. Its
    /// concrete payload may retain only authentication/control state, catalog
    /// and Gate; it must not strongly retain PackHost or a PortInvoker. That
    /// direction avoids a Shared-to-factory ownership cycle without Weak or
    /// a second refcount/control allocation.
    pub fn set_provider_entry_bridge(
        &self,
        bridge: Arc<dyn crate::ProviderEntryBridge>,
    ) -> Result<()> {
        self.shared
            .provider_entry
            .set(bridge)
            .map_err(|_| anyhow::anyhow!("provider entry bridge already installed"))
    }

    /// Transfer a live Core scope once and expose both allocation adapters for
    /// that same physical owner. No descriptor-only allocation is supported.
    pub fn allocation_owners_for_frame(
        &self,
        cx: &CallCx,
        scope: tobkiri_gate::AllocationFrameScope,
    ) -> Result<crate::AllocationOwners> {
        crate::allocation::PackAllocationBudget::for_frame(&self.shared, cx, scope)
    }
    /// Take one actual reached producer scope. Funding retains this proof and
    /// its original effective principal; registered owners alone cannot mint it.
    pub fn allocation_budget_for_frame(
        &self,
        cx: &CallCx,
        scope: tobkiri_gate::AllocationFrameScope,
    ) -> Result<Arc<dyn tobkiri_canon::AllocationBudget>> {
        Ok(crate::allocation::PackAllocationBudget::for_frame(&self.shared, cx, scope)?.graph)
    }
    /// Reap only private owners whose payload/control block can actually be
    /// destroyed. Outstanding values and I/O retain their physical leases.
    pub fn reap_allocation_owners(&self) -> Result<usize> {
        crate::allocation::reap_custody()
    }

    // Routing and exact authority views are independently frozen bindings.
    #[allow(clippy::too_many_arguments)]
    pub async fn new(
        gate: Arc<dyn Gate>,
        capacity: CapacityLedger,
        io: Arc<dyn HostIo>,
        root: Arc<DataRoot>,
        bindings: BTreeMap<ShapeRef, Vec<PackId>>,
        views: BTreeMap<ShapeRef, ShapeDef>,
        packs: Vec<VerifiedPack>,
        effective_limits: BTreeMap<(PackId, PackHash), ResourceLimits>,
        init_cx: &CallCx,
    ) -> Result<Self> {
        init_cx.check_active()?;
        // Loading the frozen bounded catalog/compiled code is native Core
        // control work. Guest instance/data/ABI coverage is checked at the
        // actual entry, after Core can install its same-factory bridge.
        if packs.len() > 64 || views.len() > 4096 || bindings.len() > 4096 {
            bail!("bounded component catalog capacity exceeded")
        }
        let provider_edges = checked_provider_edges(&bindings)?;
        let mut artifact_operations = 0u64;
        let mut artifact_ports = 0u64;
        let mut artifact_documents = 0usize;
        let mut artifact_wit_bytes = 0usize;
        for pack in &packs {
            if !matches!(&pack.manifest.runtime, RuntimeSpec::Wasm { .. }) {
                continue;
            }
            let (documents, bytes) = pack_wit_size(pack)?;
            artifact_documents = artifact_documents
                .checked_add(documents)
                .context("artifact WIT document count overflow")?;
            artifact_wit_bytes = artifact_wit_bytes
                .checked_add(bytes)
                .context("artifact WIT input size overflow")?;
            let ports = &pack.manifest.ports;
            artifact_ports = artifact_ports
                .checked_add(ports.consumes.len() as u64)
                .and_then(|n| n.checked_add(ports.provides.len() as u64))
                .context("artifact binding count overflow")?;
            if artifact_ports > 16_384 {
                bail!("bounded artifact binding catalog exceeded")
            }
            for port in ports.consumes.iter().chain(&ports.provides) {
                let reference = ShapeRef::new(port.shape.clone(), port.version);
                if let Some(view) = views.get(&reference) {
                    artifact_operations = artifact_operations
                        .checked_add(view.functions.len() as u64)
                        .context("artifact operation evidence count overflow")?;
                }
            }
        }
        if artifact_documents > 256
            || artifact_wit_bytes > 32 * 1024 * 1024
            || artifact_operations > 32_768
        {
            bail!("bounded artifact WIT evidence catalog exceeded")
        }
        let source_bytes = packs.iter().try_fold(0usize, |total, pack| {
            let RuntimeSpec::Wasm { component, .. } = &pack.manifest.runtime else {
                return Ok(total);
            };
            let bytes = pack
                .files
                .get(component)
                .context("verified component bytes missing")?;
            if bytes.len() > 32 * 1024 * 1024 {
                bail!("component exceeds bounded compilation input")
            }
            total
                .checked_add(bytes.len())
                .context("component catalog source size overflow")
        })?;
        if source_bytes > 64 * 1024 * 1024 {
            bail!("component catalog compilation input exceeded")
        }
        let (operation_count, provenance_parse_bytes, max_provenance_document) =
            views.values().try_fold(
                (0u64, 0usize, 0usize),
                |(operations, bytes, largest), shape| -> Result<_> {
                    if shape.functions.is_empty() {
                        return Ok((operations, bytes, largest));
                    }
                    Ok((
                        operations
                            .checked_add(shape.functions.len() as u64)
                            .context("error provenance operation count overflow")?,
                        bytes
                            .checked_add(shape.wit.len())
                            .and_then(|n| n.checked_add(tobkiri_wit::CORE_WIT_SOURCE.len()))
                            .and_then(|n| n.checked_add(tobkiri_wit::CORE_V1_1_WIT_SOURCE.len()))
                            .context("error provenance parse size overflow")?,
                        largest.max(shape.wit.len()),
                    ))
                },
            )?;
        if operation_count > 16_384
            || provenance_parse_bytes > 32 * 1024 * 1024
            || max_provenance_document > 1024 * 1024
        {
            bail!("bounded error provenance catalog exceeded")
        }
        let catalog_control = init_cx.control().reserve(
            tobkiri_gate::ControlKind::Catalog,
            65536u64
                .checked_add((packs.len() as u64) * 4096)
                .and_then(|n| n.checked_add((views.len() as u64 + bindings.len() as u64) * 512))
                // Includes bounded IDs/set nodes for every possible operation
                // before constructing the retained provenance index.
                .and_then(|n| n.checked_add(operation_count * 512))
                .and_then(|n| n.checked_add(provider_edges * 512))
                .and_then(|n| n.checked_add(artifact_ports * 512))
                .and_then(|n| n.checked_add(artifact_operations * 1024))
                .context("catalog control size overflow")?,
        )?;
        // Parser/normalization scratch is bounded native loading work, not a
        // guest-value sponsor. The helper separately caps expanded nodes and
        // copied-name work BEFORE recursively normalizing a result signature.
        let parse_control = if operation_count == 0 {
            None
        } else {
            let input = max_provenance_document
                .checked_add(tobkiri_wit::CORE_WIT_SOURCE.len())
                .and_then(|n| n.checked_add(tobkiri_wit::CORE_V1_1_WIT_SOURCE.len()))
                .context("error provenance scratch size overflow")?;
            let bytes = input
                .checked_mul(16)
                .and_then(|n| {
                    n.checked_add(65_536 * std::mem::size_of::<tobkiri_manifest::WitType>() * 4)
                })
                .and_then(|n| n.checked_add(4 * 1024 * 1024))
                .context("error provenance scratch size overflow")?;
            Some(
                init_cx
                    .control()
                    .reserve(tobkiri_gate::ControlKind::Load, bytes as u64)?,
            )
        };
        let mut core_error_results = BTreeMap::new();
        for (reference, shape) in &views {
            if !shape.functions.is_empty() {
                let operations = tobkiri_manifest::core_error_result_operations(shape)?;
                if !operations.is_empty() {
                    core_error_results.insert(reference.clone(), operations);
                }
            }
        }
        drop(parse_control);
        let engine = WasmEngine::new_with_control(init_cx.control())?;
        let stores = TieredStore::open(&root, init_cx.profile())?;
        let audit = Arc::new(AuditLog::open(
            &root,
            init_cx.profile(),
            AuditQuota::default(),
        )?);
        let mut verified = BTreeMap::new();
        for pack in packs {
            if verified.contains_key(&pack.manifest.id) {
                bail!("duplicate Pack identity")
            }
            for store in &pack.manifest.stores {
                let tier = match store.tier {
                    tobkiri_manifest::StoreTier::Private => tobkiri_gate::StoreTier::Private,
                    tobkiri_manifest::StoreTier::Public => tobkiri_gate::StoreTier::Public,
                    tobkiri_manifest::StoreTier::ReadOnly => tobkiri_gate::StoreTier::ReadOnly,
                };
                stores.register(
                    &pack.manifest.id,
                    &store.name,
                    tier,
                    &store.data_shape.to_string(),
                )?;
            }
            verified.insert(pack.manifest.id.clone(), Arc::new(pack));
        }
        for (reference, providers) in &bindings {
            if !views.contains_key(reference) {
                bail!("binding has no exact frozen caller view")
            }
            for provider in providers {
                if !verified.contains_key(provider) {
                    bail!("binding points to a missing verified Pack")
                }
            }
        }
        let shared = Arc::new(Shared {
            engine,
            profile: init_cx.profile().clone(),
            gate,
            capacity,
            io,
            root,
            stores,
            _audit: audit,
            vault: StdMutex::new(None),
            bindings,
            views,
            core_error_results,
            pack_core_errors: OnceLock::new(),
            verified,
            instances: StdMutex::new(BTreeMap::new()),
            handles: StdMutex::new(HandleTable::new(1024)),
            effective_limits,
            flow_runner: OnceLock::new(),
            provider_entry: OnceLock::new(),
            filesystem: OnceLock::new(),
            control: init_cx.control().clone(),
            _catalog_control: catalog_control,
        });
        let mut pack_core_errors = BTreeMap::new();
        for (id, pack) in &shared.verified {
            let RuntimeSpec::Wasm { component, .. } = &pack.manifest.runtime else {
                continue;
            };
            if !shared
                .effective_limits
                .contains_key(&(id.clone(), pack.hash.clone()))
            {
                let denied_cx = init_cx.child(Principal::Pack {
                    pack: id.clone(),
                    hash: pack.hash.clone(),
                });
                shared
                    .gate
                    .record_denial(&denied_cx, "wasm-entry", "missing-budget")
                    .await?;
                bail!("effective Pack revision limits missing")
            }
            let bytes = pack
                .files
                .get(component)
                .context("verified component bytes missing")?;
            let compiled = Component::new(shared.engine.engine(), bytes)?;
            // One admitted inspection of this exact verified dependency set
            // supplies both import validation and directional nominal evidence.
            // Keep parser scratch bounded and release it before the next Pack.
            let (_, wit_bytes) = pack_wit_size(pack)?;
            let scratch = wit_bytes
                .checked_add(tobkiri_wit::CORE_WIT_SOURCE.len())
                .and_then(|n| n.checked_add(tobkiri_wit::CORE_V1_1_WIT_SOURCE.len()))
                .and_then(|n| n.checked_mul(16))
                .and_then(|n| {
                    n.checked_add(65_536 * std::mem::size_of::<tobkiri_manifest::WitType>() * 4)
                })
                .and_then(|n| n.checked_add(4 * 1024 * 1024))
                .context("artifact WIT scratch size overflow")?;
            let _parse_control = shared
                .control
                .reserve(tobkiri_gate::ControlKind::Load, scratch as u64)?;
            let inspection = tobkiri_manifest::inspect_pack_wit(pack, &shared.views)?;
            validate_imports(
                shared.engine.engine(),
                &compiled,
                inspection.allowed_imports(),
            )?;
            let shapes = shared.views.values().cloned().collect::<Vec<_>>();
            crate::abi::validate_abi(shared.engine.engine(), &compiled, pack, &shapes)?;
            pack_core_errors.insert(id.clone(), inspection.into_core_errors());
            let config = pack.init_config()?;
            let mut sources = BTreeMap::new();
            for (name, _) in compiled.component_type().exports(shared.engine.engine()) {
                let source = match name {
                    "tobkiri:core/stream-source@1.0.0" => Some((HandleKind::Stream, "1.0.0")),
                    "tobkiri:core/stream-source@1.1.0" => Some((HandleKind::Stream, "1.1.0")),
                    "tobkiri:core/callback-sink@1.0.0" => Some((HandleKind::Callback, "1.0.0")),
                    "tobkiri:core/callback-sink@1.1.0" => Some((HandleKind::Callback, "1.1.0")),
                    _ => None,
                };
                if let Some((kind, version)) = source
                    && sources.insert(kind, version).is_some()
                {
                    bail!("multiple exact source packages need an explicit source binding")
                }
            }
            let lifetime = GenerationLifetime::new(&shared.control, None)?;
            let store_disposal = lifetime.disposal.clone();
            let retirement = Arc::new(StdMutex::new(lifetime));
            let generation = Arc::new(AtomicU64::new(0));
            shared
                .instances
                .lock()
                .map_err(|_| anyhow::anyhow!("instance map poisoned"))?
                .insert(
                    id.clone(),
                    Arc::new(InstanceSlot {
                        instance: Mutex::new(PackInstance {
                            pack: pack.clone(),
                            component: compiled,
                            config,
                            live: None,
                            physical: None,
                            retirement: retirement.clone(),
                            generation: generation.clone(),
                            current_generation: 0,
                            store_disposal,
                        }),
                        retirement,
                        sources,
                        generation,
                    }),
                );
        }
        shared
            .pack_core_errors
            .set(pack_core_errors)
            .map_err(|_| anyhow::anyhow!("artifact WIT evidence is already frozen"))?;
        // Core installs the frozen Session bridge before any guest init can
        // reenter a Flow. Initialization remains gated immediately before use.
        Ok(Self { shared })
    }
    pub fn set_flow_runner(&self, runner: Weak<dyn FlowRunner>) -> Result<(), StepError> {
        self.shared
            .flow_runner
            .set(runner)
            .map_err(|_| StepError::new("conflict", "Flow runner is already frozen"))
    }
    /// Admit before Core consume or any native body can start. Both generation
    /// evidence and execution identity come from this actual installed Store.
    pub(crate) fn native_work_spawner(
        &self,
        state: &PackState,
    ) -> impl std::future::Future<Output = Result<NativeWorkSpawner>> + Send + 'static + use<> {
        // Extract only allocation-free owned handles synchronously. The Store's
        // PackState contains non-Sync WASI state and must not enter the future.
        struct OwnedPreparation {
            shared: Arc<Shared>,
            frame: Arc<dyn crate::ProviderFrame>,
            census: GenerationChildCensus,
            retirement: Weak<StdMutex<GenerationLifetime>>,
            // Last: captures retire before their physical metadata funding.
            _reservation: tobkiri_gate::ControlReservation,
        }
        let owned = (|| -> Result<OwnedPreparation> {
            if !state.shared.ptr_eq(&Arc::downgrade(&self.shared)) {
                bail!("native Store belongs to another host")
            }
            let frame = state.frame.as_ref().context("native Store frame missing")?;
            let reservation = frame.context().control().reserve(
                tobkiri_gate::ControlKind::Frame,
                u64::try_from(std::mem::size_of::<Result<OwnedPreparation>>())?,
            )?;
            Ok(OwnedPreparation {
                shared: self.shared.clone(),
                frame: frame.clone(),
                census: state.native_generation.clone(),
                retirement: state.generation_lifetime.clone(),
                _reservation: reservation,
            })
        })();
        async move {
            let owned = owned?;
            let frame = &owned.frame;
            // One bounded owner for this admission's identity phase; its fixed
            // attempt counter and precharged reservation retire with this call.
            let mut preparation =
                crate::native_preparation::NativePreparation::new(frame.context())?;
            let execution = preparation.native_execution_id(frame).await?;
            let operation = frame.provider_operation().map_err(anyhow::Error::new)?;
            let lifetime = owned
                .retirement
                .upgrade()
                .context("native generation lost")?;
            let generation = lifetime
                .try_lock()
                .map_err(|_| anyhow::anyhow!("generation state busy"))?;
            if generation.retiring
                || generation.token.is_cancelled()
                || !generation
                    .disposal
                    .census
                    .as_ref()
                    .is_some_and(|current| current.generation() == owned.census.generation())
            {
                bail!("native Store generation retired")
            }
            if !operation
                .inner
                .shared
                .ptr_eq(&Arc::downgrade(&owned.shared))
                || &operation.inner.generation != owned.census.generation()
            {
                bail!("native Store operation belongs to another generation")
            }
            let mut operations = operation
                .inner
                .registry
                .state
                .try_lock()
                .map_err(|_| anyhow::anyhow!("provider operation inventory busy"))?;
            let record = &mut operations[operation.inner.index];
            if record.closed || record.native.len() == MAX_NATIVE_CHILD_LINKS {
                bail!("provider operation is closing or full")
            }
            let native = NativeWorkSpawner::admit(
                &owned.shared.control,
                operation.clone(),
                owned.census,
                owned.retirement,
                generation.native_children.clone(),
                execution,
                frame.context().cancel().clone(),
                NativeTaskRole::Source,
                None,
                None,
            )?;
            record.native.push(native.inner.completion.clone());
            Ok(native)
        }
    }
    pub(crate) async fn register_core_stream(
        &self,
        cx: &CallCx,
        shape: ShapeRef,
        stream: CoreStream,
        frame: &Arc<dyn crate::ProviderFrame>,
    ) -> Result<u64> {
        if stream.physical_completion().is_none() {
            frame
                .provider_operation()
                .map_err(anyhow::Error::new)?
                .inner
                .unknown
                .store(true, Ordering::Release);
        }
        self.register_core_stream_with_native(cx, shape, stream, None)
            .await
    }
    pub(crate) async fn register_core_stream_with_native(
        &self,
        cx: &CallCx,
        shape: ShapeRef,
        stream: CoreStream,
        native: Option<&NativeWorkSpawner>,
    ) -> Result<u64> {
        cx.check_active()?;
        let (pack, _) = cx
            .principal()
            .pack()
            .context("native Flow child has no actual provider")?;
        let slot = self.instance(pack)?;
        let registration = self
            .shared
            .control
            .reserve(tobkiri_gate::ControlKind::Frame, 4096)?;
        // Keep outer owners until every generation/table guard has gone. A
        // rejected insertion must not destroy/cancel its real source there.
        let physical = stream.physical_completion();
        let stream = Arc::new(Mutex::new(stream));
        let lease = Arc::new(crate::handles::HandleLease::native(registration));
        let generation = slot
            .retirement
            .try_lock()
            .map_err(|_| anyhow::anyhow!("generation state unavailable"))?;
        if generation.retiring || generation.token.is_cancelled() {
            bail!("native Flow generation retired")
        }
        match native {
            Some(native)
                if generation.disposal.census.as_ref().is_some_and(|census| {
                    census.generation() == native.inner.generation.generation()
                }) && native
                    .inner
                    .sender
                    .try_lock()
                    .map_err(|_| anyhow::anyhow!("native submission state unavailable"))?
                    .is_none() => {}
            Some(_) => bail!("native stream differs from registered task generation"),
            None if physical.is_some() => (),
            None => generation.native_children.store(true, Ordering::Release),
        }
        let id = self
            .shared
            .handles
            .try_lock()
            .map_err(|_| anyhow::anyhow!("handle table unavailable during native publication"))?
            .register_core(cx.clone(), shape, stream.clone(), lease.clone())?;
        self.shared
            .handles
            .try_lock()
            .map_err(|_| anyhow::anyhow!("native physical binding table busy"))?
            .bind_native_physical(id, physical)?;
        drop(generation);
        self.reap_handle(cx, cx, id, HandleKind::Stream);
        Ok(id)
    }
    pub(crate) fn component_stream(&self, cx: &CallCx, id: u64) -> Result<CoreStream> {
        let entry = self
            .shared
            .handles
            .lock()
            .map_err(|_| anyhow::anyhow!("handle table poisoned"))?
            .resolve(id, HandleKind::Stream, cx)?;
        let registration = self
            .shared
            .control
            .reserve(tobkiri_gate::ControlKind::Frame, 4096)?;
        Ok(CoreStream::new(ComponentStream {
            host: self.clone(),
            cx: cx.clone(),
            handle: id,
            entry,
            ended: false,
            runtime: tokio::runtime::Handle::try_current()?,
            cleanup: None,
            cleanup_error: None,
            terminal_batch: None,
            terminal_buffer: None,
            cancelled_terminal: false,
            _registration: registration,
        }))
    }
    fn reap_handle(&self, owner: &CallCx, recipient: &CallCx, id: u64, kind: HandleKind) {
        let weak = Arc::downgrade(&self.shared);
        let owner = owner.clone();
        let recipient = recipient.clone();
        tokio::spawn(async move {
            let reason = tokio::select! {
                _=tokio::time::sleep_until(owner.deadline().into())=>crate::core_imports::ExpiryReason::Deadline,
                _=owner.cancel().cancelled()=>crate::core_imports::ExpiryReason::Unknown,
            };
            if let Some(shared) = weak.upgrade() {
                let host = PackHost { shared };
                // A durable expiry supervisor retries audit delivery. This
                // reaper closes actual resources without claiming success for
                // a deadline or silently turning it into caller cancellation.
                let _ =
                    crate::core_imports::expire_handle(&host, &recipient, id, kind, reason).await;
            }
        });
    }

    fn instance(&self, id: &PackId) -> Result<Arc<InstanceSlot>> {
        self.shared
            .instances
            .lock()
            .map_err(|_| anyhow::anyhow!("instance map poisoned"))?
            .get(id)
            .cloned()
            .context("Pack has no component runtime")
    }
    /// Astra Q32 prefund. While the original actual source frame is still
    /// live — before any stream body work can start — allocate the canonical
    /// empty batch and its b"[]" buffer under that same source CodeOwner
    /// budget, including their storage and metadata. Serving the cache later
    /// only clones this funded storage; no post-retire body allocation or
    /// reissued budget is ever created.
    fn prefund_terminal_empty(
        &self,
        frame: &Arc<dyn crate::ProviderFrame>,
    ) -> Result<crate::handles::TerminalEmptyCache> {
        let cx = frame.context();
        self.shared.gate.check_context(cx)?;
        let batch_owners = self.allocation_owners_for_frame(
            cx,
            frame
                .allocation_scope(tobkiri_gate::AllocationReason::StreamBatch)
                .map_err(anyhow::Error::new)?,
        )?;
        let batch = tobkiri_canon::CoreValue::array_builder(batch_owners.graph(), 0)
            .and_then(|builder| builder.finish())
            .map_err(|error| anyhow::anyhow!("terminal empty batch funding failed: {error}"))?;
        let output_owners = self.allocation_owners_for_frame(
            cx,
            frame
                .allocation_scope(tobkiri_gate::AllocationReason::OutputBuffer)
                .map_err(anyhow::Error::new)?,
        )?;
        let buffer = tobkiri_canon::CoreBuffer::copy_from(b"[]", output_owners.graph())
            .map_err(|error| anyhow::anyhow!("terminal empty buffer funding failed: {error}"))?;
        Ok(crate::handles::TerminalEmptyCache { batch, buffer })
    }
    pub(crate) async fn register_guest_handle(
        &self,
        kind: HandleKind,
        id: u64,
        expected_generation: Option<u64>,
        source: crate::handle_transfer::HandleSourceContext<'_>,
    ) -> Result<u64> {
        let crate::handle_transfer::HandleSourceContext {
            owner,
            recipient,
            authority,
            provider,
        } = source;
        let (pack, hash) = owner
            .principal()
            .pack()
            .context("guest handle owner is not a Pack")?;
        let source_slot = self.instance(pack)?;
        let Some(version) = source_slot.sources.get(&kind).copied() else {
            self.shared
                .gate
                .record_denial(owner, "wasm-entry", "missing-source")
                .await?;
            return Err(StepError::new("invalid", "guest handle source export is missing").into());
        };
        let generation =
            expected_generation.unwrap_or(self.instance(pack)?.generation.load(Ordering::Acquire));
        self.check_generation(pack, generation)?;
        let entry = match kind {
            HandleKind::Stream => WasmEntryKind::StreamOpen { handle: id },
            HandleKind::Callback => WasmEntryKind::CallbackOpen { handle: id },
        };
        let request = EffectRequest::WasmEntry {
            pack: pack.clone(),
            hash: hash.clone(),
            entry,
        };
        let args = request.digest()?;
        let grant = self
            .shared
            .gate
            .authorize(owner, request.clone(), args.clone())
            .await?;
        let grant_id = grant.id().clone();
        let mut permit = grant.consume(owner, &request, &args)?;
        let owner = owner.clone().narrow_limits(permit.limits());
        if let Err(error) = self.shared.gate.narrow_lease(&mut permit, &owner).await {
            self.shared
                .gate
                .record_outcome(
                    grant_id,
                    Outcome::Failed {
                        code: "lease-narrowing".into(),
                    },
                )
                .await?;
            return Err(error.into());
        }
        if let Err(error) = self.shared.gate.watch_lease(&owner, &permit) {
            self.shared
                .gate
                .record_outcome(
                    grant_id,
                    Outcome::Failed {
                        code: "lease-supervision".into(),
                    },
                )
                .await?;
            return Err(error.into());
        }
        if let Err(error) = self.check_generation(pack, generation) {
            self.shared
                .gate
                .record_outcome(
                    grant_id,
                    Outcome::Failed {
                        code: "source-retired".into(),
                    },
                )
                .await?;
            return Err(error);
        }
        let source_frame = match self
            .shared
            .provider_entry
            .get()
            .context("Core source bridge not installed")?
            .enter_handle_source(
                &crate::HandleSourceRequest {
                    control: self,
                    provider,
                    owner: &owner,
                    kind,
                    local_id: id,
                    authority,
                    generation,
                },
                &permit,
            ) {
            Ok(frame) => frame,
            Err(error) => {
                self.shared
                    .gate
                    .record_outcome(
                        grant_id,
                        Outcome::Failed {
                            code: "source-entry".into(),
                        },
                    )
                    .await?;
                return Err(anyhow::Error::new(error));
            }
        };
        let result = self
            .shared
            .handles
            .lock()
            .map_err(|_| anyhow::anyhow!("handle table poisoned"))?
            .register_authorized(
                kind,
                owner.clone(),
                id,
                recipient.clone(),
                authority.clone(),
                permit,
                generation,
                source_frame.clone(),
                version,
            );
        if result.is_err() {
            self.shared
                .gate
                .record_outcome(
                    grant_id,
                    Outcome::Failed {
                        code: "handle-quota".into(),
                    },
                )
                .await?;
        }
        if let Ok(id) = &result {
            let binding = (|| -> Result<()> {
                let registration = self
                    .shared
                    .control
                    .reserve(tobkiri_gate::ControlKind::Frame, 4096)?;
                let registry_reservation = self
                    .shared
                    .control
                    .reserve(tobkiri_gate::ControlKind::Registration, 512)?;
                let (children, disposal) = {
                    let lifetime = source_slot
                        .retirement
                        .try_lock()
                        .map_err(|_| anyhow::anyhow!("generation state unavailable"))?;
                    (lifetime.native_children.clone(), lifetime.disposal.clone())
                };
                let mut captured = self
                    .shared
                    .handles
                    .lock()
                    .map_err(|_| anyhow::anyhow!("handle table poisoned"))?
                    .resolve_cleanup(*id, kind, recipient)?;
                captured.native_children = Some(children.clone());
                captured.store_disposal = Some(disposal.clone());
                // The typed selector shares this exact Arc; it is built from
                // the entry clone BEFORE any binding so no
                // entry->selector->entry strong cycle can exist.
                let selector = Arc::new(ComponentCloseControl {
                    shared: Arc::downgrade(&self.shared),
                    entry: captured,
                    _registration: registration,
                });
                let control: Arc<dyn tobkiri_flow::StreamCloseControl> = selector.clone();
                {
                    let mut lifetime = source_slot
                        .retirement
                        .try_lock()
                        .map_err(|_| anyhow::anyhow!("generation state unavailable"))?;
                    if source_slot.generation.load(Ordering::Acquire) != generation
                        || lifetime.retiring
                    {
                        bail!("source generation retired before cleanup registration")
                    }
                    if lifetime.controls.len() >= 4096 {
                        bail!("generation cleanup registry full")
                    }
                    lifetime.controls.push(GenerationCloseRegistration {
                        control: Arc::downgrade(&control),
                        _reservation: registry_reservation,
                    });
                }
                self.shared
                    .handles
                    .lock()
                    .map_err(|_| anyhow::anyhow!("handle table poisoned"))?
                    .bind_cleanup(*id, control.clone(), children, disposal)?;
                self.shared
                    .handles
                    .lock()
                    .map_err(|_| anyhow::anyhow!("handle table poisoned"))?
                    .bind_cleanup_selector(*id, selector)?;
                let operation = source_frame
                    .provider_operation()
                    .map_err(anyhow::Error::new)?;
                self.shared
                    .handles
                    .lock()
                    .map_err(|_| anyhow::anyhow!("handle table poisoned"))?
                    .bind_operation(*id, operation.clone(), operation.physical_completion())?;
                // Even an already requested close is selected before any reaper
                // can observe the token wake. Failure leaves a quarantined lease.
                source_frame
                    .bind_cleanup_control(&control)
                    .map_err(anyhow::Error::new)?;
                // Q32: fund the canonical empty terminal pair under the
                // original source CodeOwner while its actual frame is live and
                // before any stream body work. Custody is already bound, so a
                // funding failure does not strand the real source body or its
                // consumed grant: the registered lease stays quarantined and
                // the outer reaper keeps exact-generation physical cleanup and
                // terminal custody. No completion is claimed without proof.
                if kind == HandleKind::Stream {
                    let cache = self.prefund_terminal_empty(&source_frame)?;
                    self.shared
                        .handles
                        .lock()
                        .map_err(|_| anyhow::anyhow!("handle table poisoned"))?
                        .bind_terminal_empty(*id, cache)?;
                }
                Ok(())
            })();
            // On registration failure the same consumed lease remains in the
            // bounded table, and its reaper retains cleanup/terminal custody.
            self.reap_handle(&owner, recipient, *id, kind);
            binding?;
        }
        result
    }
    pub(crate) fn resolve_for_admission(
        &self,
        cx: &CallCx,
        shape: &ShapeRef,
        op: &OpName,
        require_dynamic_consumes: bool,
    ) -> Result<(Arc<VerifiedPack>, ShapeDef, WitFunction)> {
        cx.check_active()?;
        if cx.profile() != &self.shared.profile {
            bail!("component Profile mismatch")
        }
        if let Some((caller, hash)) = cx.principal().pack() {
            let pack = self
                .shared
                .verified
                .get(caller)
                .context("unknown caller Pack")?;
            if &pack.hash != hash {
                bail!("caller revision mismatch")
            }
            if require_dynamic_consumes
                && !pack
                    .manifest
                    .ports
                    .consumes
                    .iter()
                    .any(|port| port.shape == shape.name && port.version == shape.version)
            {
                bail!("caller has not declared the consumed shape")
            }
        }
        let providers = self
            .shared
            .bindings
            .get(shape)
            .context("shape is unbound")?;
        if providers.len() != 1 {
            bail!("a many-provider shape needs an explicit selected dispatch")
        }
        let pack = self
            .shared
            .verified
            .get(&providers[0])
            .context("provider Pack missing")?
            .clone();
        let provided = pack
            .manifest
            .ports
            .provides
            .iter()
            .filter(|p| {
                p.shape == shape.name
                    && p.version.major == shape.version.major
                    && p.version.minor >= shape.version.minor
            })
            .max_by_key(|p| p.version.minor)
            .context("bound provider does not declare the shape")?;
        let def = self
            .shared
            .views
            .get(&ShapeRef::new(provided.shape.clone(), provided.version))
            .context("provider shape is not registered")?
            .clone();
        let function = def
            .functions
            .get(op)
            .context("shape operation is not declared")?
            .clone();
        if matches!(cx.principal(), Principal::Input { .. }) {
            // Source IDs are opaque. Only Core's explicit frozen startup input
            // record binds a source to this exact physical endpoint; neither
            // parsing the label as PackId nor a routing binding creates rights.
            self.shared.gate.input_capacity_limit(
                cx,
                shape,
                op,
                &tobkiri_gate::FrozenPortTarget {
                    pack: pack.manifest.id.clone(),
                    hash: pack.hash.clone(),
                    provided: def.reference.clone(),
                },
            )?;
        }
        Ok((pack, def, function))
    }

    pub(crate) fn check_handle_generation(&self, entry: &crate::HandleEntry) -> Result<()> {
        if let Some(generation) = entry.generation {
            self.check_generation(
                entry
                    .owner
                    .principal()
                    .pack()
                    .context("guest source owner missing")?
                    .0,
                generation,
            )?;
        }
        Ok(())
    }
    fn check_generation(&self, id: &PackId, generation: u64) -> Result<()> {
        let slot = self.instance(id)?;
        let lifetime = slot
            .retirement
            .lock()
            .map_err(|_| anyhow::anyhow!("retirement state poisoned"))?;
        if generation == 0
            || slot.generation.load(Ordering::Acquire) != generation
            || lifetime.retiring
            || lifetime.token.is_cancelled()
        {
            return Err(
                tobkiri_gate::GateError::Denied("handle source instance retired".into()).into(),
            );
        }
        Ok(())
    }
    #[allow(clippy::too_many_arguments)]
    pub(crate) async fn export_call_owned(
        &self,
        id: &PackId,
        cx: &CallCx,
        interface: &str,
        op: &str,
        args: &[Val],
        expected_generation: Option<u64>,
        owners: &crate::owned_values::ValueOwners,
        frame: Arc<dyn crate::ProviderFrame>,
        physical_scope: tobkiri_gate::AllocationFrameScope,
    ) -> Result<(wasmtime::component::BudgetedVec<Val>, u64)> {
        if cx.executing_pairs().is_empty() {
            GenerationChildCensus::maintain_executions();
        }
        let pack = self
            .shared
            .verified
            .get(id)
            .context("unknown execution Pack")?;
        let reentrant = cx
            .executing_pairs()
            .iter()
            .any(|pair| pair == &(id.clone(), pack.hash.clone()));
        let cx = cx
            .clone()
            .try_with_running_pack_precharged(id, &pack.hash)?;
        let slot = self.instance(id)?;
        let cleanup_control = frame.cleanup_control();
        if let Some(generation) = expected_generation {
            self.check_generation(id, generation)?;
        }
        let mut instance = if reentrant {
            slot.instance.try_lock().map_err(|_| {
                StepError::new("conflict", "nested component has no free instance capacity")
            })?
        } else {
            tokio::select! {
                _=cx.cancel().cancelled()=>bail!("component call cancelled while waiting"),
                _=tokio::time::sleep_until(cx.deadline().into())=>bail!("original component deadline elapsed while waiting"),
                instance=slot.instance.lock()=>instance,
            }
        };
        if let Some(physical) = &instance.physical {
            physical.enter(&cx, physical_scope)?;
        } else {
            instance.physical = Some(crate::instance_capacity::InstanceCapacity::new(
                &self.shared,
                &cx,
                physical_scope,
            )?);
        }
        if let Some(generation) = expected_generation {
            if instance.live.is_none() || instance.current_generation != generation {
                if let Some(physical) = &instance.physical {
                    physical.leave();
                }
                return Err(tobkiri_gate::GateError::Denied(
                    "handle source instance retired".into(),
                )
                .into());
            }
        } else {
            if let Err(error) = instance.initialize(&self.shared, &cx, owners, &frame).await {
                instance.live.take();
                instance.store_disposal.complete();
                instance.physical.take();
                return Err(error);
            }
        }
        ensure_provider_operation(&self.shared, &slot.retirement, &frame).await?;
        if let Some((store, _)) = instance.live.as_mut() {
            store.data_mut().data.frame = Some(frame);
        }
        let result = instance
            .call_owned(&cx, interface, op, args, owners)
            .await
            .map_err(|error| entry_phase_error(error, "export", &cx, owners));
        if let Some((store, _)) = instance.live.as_mut() {
            store.data_mut().data.frame.take();
        }
        if let Some(physical) = &instance.physical {
            physical.leave();
        }
        if let Err(error) = &result {
            if let Some(control) = &cleanup_control {
                // Record the actual source failure while its generation and
                // own Core source node still precede retirement. Never project
                // an unrelated provider failure onto another stream.
                let _ = control.record_failure(&step_error_ref(error));
            }
            let retirement = self.retire_generation(&slot, Some(instance.current_generation))?;
            retirement.token.cancel();
            instance.live.take();
            instance.store_disposal.complete();
            instance.physical.take();
        }
        let generation = instance.current_generation;
        if result.is_ok()
            && let Err(error) = self.check_generation(id, generation)
        {
            let retirement = self.retire_generation(&slot, Some(instance.current_generation))?;
            retirement.token.cancel();
            instance.live.take();
            instance.store_disposal.complete();
            return Err(error);
        }
        result.map(|values| (values, generation))
    }
    fn retire_generation(
        &self,
        slot: &InstanceSlot,
        expected: Option<u64>,
    ) -> Result<RetiredGeneration> {
        let (retirement, controls, _snapshot_reservation) = {
            let mut lifetime = slot
                .retirement
                .try_lock()
                .map_err(|_| anyhow::anyhow!("generation state busy"))?;
            let generation = slot.generation.load(Ordering::Acquire);
            if expected.is_some_and(|expected| expected != generation) || lifetime.retiring {
                bail!("requested component generation already retired")
            }
            let next_generation = generation
                .checked_add(1)
                .context("instance generations exhausted")?;
            let snapshot_bytes = lifetime
                .controls
                .len()
                .checked_add(lifetime.operations.len())
                .context("generation snapshot bound overflow")?
                .checked_mul(32)
                .and_then(|bytes| bytes.checked_add(256))
                .and_then(|bytes| u64::try_from(bytes).ok())
                .context("generation snapshot bound overflow")?;
            let reservation = self
                .shared
                .control
                .reserve(tobkiri_gate::ControlKind::Frame, snapshot_bytes)?;
            let owner = lifetime.disposal.claim_owner()?;
            let native = {
                let mut receipt = lifetime
                    .disposal
                    .retirement
                    .try_lock()
                    .map_err(|_| anyhow::anyhow!("native retirement state unavailable"))?;
                let native = lifetime
                    .disposal
                    .census
                    .as_ref()
                    .map(|census| census.seal(tobkiri_flow::StreamCloseReason::Retired))
                    .transpose()?;
                *receipt = native.clone();
                native
            };
            let operations: Vec<_> = lifetime
                .operations
                .iter()
                .filter_map(GenerationOperationRegistration::upgrade)
                .collect();
            for operation in &operations {
                operation.seal()?;
            }
            // Both writers of this counter hold the same generation mutex.
            // All fallible native admission/receipt work preceded retirement.
            lifetime.retiring = true;
            slot.generation.store(next_generation, Ordering::Release);
            (
                RetiredGeneration {
                    token: lifetime.token.clone(),
                    generation,
                    disposal: lifetime.disposal.clone(),
                    native,
                    operations,
                    _owner: owner,
                },
                lifetime
                    .controls
                    .iter()
                    .filter_map(|registration| registration.control.upgrade())
                    .collect::<Vec<_>>(),
                reservation,
            )
        };
        for control in controls {
            let _ = control.record_close(tobkiri_flow::StreamCloseReason::Retired);
        }
        for operation in &retirement.operations {
            let _ = operation.notify_native_close();
        }
        if let Some(native) = &retirement.native {
            // The exact census was sealed while generation admission was held.
            // No child close callback or token wake runs under those guards.
            let _ = native.notify_close();
        }
        Ok(retirement)
    }

    pub(crate) async fn discard_handle_source(&self, entry: &crate::HandleEntry) -> Result<()> {
        let operation = entry
            .operation
            .as_ref()
            .context("source operation scope missing")?;
        let disposal = entry
            .store_disposal
            .as_ref()
            .context("source Store receipt missing")?;
        if !Arc::ptr_eq(disposal, &operation.inner.disposal)
            || entry.generation != Some(operation.inner.generation.generation)
        {
            bail!("source operation and actual Store receipt differ")
        }
        operation.seal()?;
        let _ = operation.notify_native_close();
        if !disposal.is_complete() {
            let (pack, _) = entry
                .owner
                .principal()
                .pack()
                .context("source physical owner missing")?;
            if let Err(error) = self
                .discard_generation_mode(pack, entry.generation, false)
                .await
            {
                // A rejection does not establish that another cleanup worker
                // exists. Observe only this exact source receipt's live owner
                // (or its actual completed Store destruction), never a newer
                // generation or an unowned retirement flag.
                disposal.wait_owned_vm().await.map_err(|observation| {
                    error.context(format!("source disposal ownership: {observation}"))
                })?;
            }
        }
        disposal.wait_vm().await?;
        ProviderOperationScope::wait_native(&operation.inner)
            .await
            .map_err(anyhow::Error::new)
    }
    async fn discard_generation_mode(
        &self,
        id: &PackId,
        expected: Option<u64>,
        wait_native: bool,
    ) -> Result<()> {
        let slot = self.instance(id)?;
        let (prepared, retirement) = PreparedDisposalTask::prepare(&self.shared.control, || {
            self.retire_generation(&slot, expected)
        })?;
        retirement.token.cancel();
        let disposal = retirement.disposal.clone();
        let task = prepared.runtime.spawn(async move {
            let _control = prepared.control;
            // Move the whole original retirement owner into the task. Field
            // capture alone must not drop its ownership marker before work.
            let retirement = retirement;
            {
                let mut instance = slot.instance.lock().await;
                if instance.current_generation == retirement.generation
                    && instance.live.as_ref().is_some_and(|(store, _)| {
                        store
                            .data()
                            .retirement
                            .as_ref()
                            .is_some_and(|token| token.is_cancelled())
                    })
                {
                    instance.live.take();
                    instance.physical.take();
                    instance.store_disposal.complete();
                }
            }
            // This owning cleanup task survives an outer timeout. It retains
            // the exact census while every actual Join/custody receipt resolves.
            let result = retirement.disposal.wait().await;
            drop(retirement);
            result
        });
        if !wait_native {
            // The existing worker keeps full-generation custody independently.
            // A source observes only actual VM destruction here; waiting that
            // worker would include a native caller awaiting this very close.
            drop(task);
            return disposal.wait_owned_vm().await;
        }
        tokio::time::timeout(std::time::Duration::from_secs(2), task)
            .await
            .context("component retirement completion unknown")?
            .context("component retirement task did not complete")??;
        disposal.wait().await
    }
}

fn reap_dead_provider_operations(retirement: &Arc<StdMutex<GenerationLifetime>>) -> Result<()> {
    for _ in 0..MAX_PROVIDER_OPERATIONS {
        let removed = {
            let mut lifetime = retirement
                .try_lock()
                .map_err(|_| anyhow::anyhow!("provider operation generation busy"))?;
            let dead = lifetime
                .operations
                .iter()
                .position(GenerationOperationRegistration::is_dead);
            dead.map(|index| lifetime.operations.swap_remove(index))
        };
        let Some(removed) = removed else {
            break;
        };
        // No strong owner can appear after zero. The sole identity Weak is
        // destroyed outside the generation guard BEFORE its occupied count is
        // refunded; an overlapping admission cannot reuse undestroyed storage.
        drop(removed);
    }
    Ok(())
}

async fn ensure_provider_operation(
    shared: &Arc<Shared>,
    retirement: &Arc<StdMutex<GenerationLifetime>>,
    frame: &Arc<dyn crate::ProviderFrame>,
) -> Result<ProviderOperationScope> {
    let identity_control = frame
        .context()
        .control()
        .reserve(tobkiri_gate::ControlKind::Frame, 2048)?;
    let existing = frame.provider_operation().ok();
    // One owner: this preparation's single counter covers the identity and
    // bind phases combined. A repeated ensure returning its preexisting
    // valid scope manufactures no preparation and consumes no attempt.
    let mut preparation = if existing.is_none() {
        Some(crate::native_preparation::NativePreparation::new(
            frame.context(),
        )?)
    } else {
        None
    };
    let fresh = if existing.is_none() {
        Some((
            preparation
                .as_mut()
                .expect("fresh scope requires its preparation")
                .native_execution_id(frame)
                .await?,
            frame.provider_operation_parent(),
        ))
    } else {
        None
    };
    if existing.is_none() {
        reap_dead_provider_operations(retirement)?;
    }
    let scope = {
        let mut lifetime = retirement
            .try_lock()
            .map_err(|_| anyhow::anyhow!("provider operation generation busy"))?;
        if lifetime.retiring || lifetime.token.is_cancelled() {
            bail!("provider operation generation retired")
        }
        let census = lifetime
            .disposal
            .census
            .as_ref()
            .context("provider operation generation missing")?;
        if let Some(scope) = existing {
            if !scope.inner.shared.ptr_eq(&Arc::downgrade(shared))
                || &scope.inner.generation != census.generation()
                || !Arc::ptr_eq(&scope.inner.disposal, &lifetime.disposal)
            {
                bail!("provider operation belongs to another exact Store")
            }
            return Ok(scope);
        }
        let mut registration = GenerationOperationRegistration::reserve(&lifetime.operation_index)?;
        let (execution, parent) = fresh.context("provider operation identity missing")?;
        let scope = ProviderOperationScope::new(
            frame.context().control(),
            Arc::downgrade(shared),
            census.generation(),
            execution,
            lifetime.disposal.clone(),
            parent,
        )?;
        registration.scope = Some(scope.weak_for_index());
        lifetime.operations.push(registration);
        scope
    };
    // Core admission/lifecycle locks must never be taken under generation or
    // operation guards. A concurrent retirement leaves this same token closed;
    // the caller rechecks exact generation before running any guest code.
    let mut preparation = preparation.expect("fresh scope requires its preparation");
    preparation
        .bind_provider_operation(frame, scope.clone())
        .await?;
    drop(identity_control);
    Ok(scope)
}

impl PackInstance {
    fn dispose_failed_initialization(&mut self) -> Result<()> {
        // The actual instance mutex remains held by initialize. Keep this
        // original inline custodian through Store destruction even if sealing
        // fails, or an independently admitted retirement worker disappears.
        let disposal = self.store_disposal.clone();
        let store = self.live.take();
        disposal.dispose_inline(store, || self.seal_native_generation())
    }

    fn seal_native_generation(&self) -> Result<()> {
        let (retirement, token, operations, _snapshot) = {
            let mut lifetime = self
                .retirement
                .try_lock()
                .map_err(|_| anyhow::anyhow!("generation state busy"))?;
            // The old PackInstance receipt, never a replacement generation.
            if !Arc::ptr_eq(&lifetime.disposal, &self.store_disposal) {
                bail!("native generation receipt changed")
            }
            lifetime.retiring = true;
            let snapshot = lifetime
                .disposal
                .census
                .as_ref()
                .context("native generation census missing")?
                .control()
                .reserve(
                    tobkiri_gate::ControlKind::Frame,
                    (lifetime.operations.len() as u64)
                        .checked_mul(32)
                        .and_then(|bytes| bytes.checked_add(256))
                        .context("operation snapshot bound overflow")?,
                )?;
            let operations: Vec<_> = lifetime
                .operations
                .iter()
                .filter_map(GenerationOperationRegistration::upgrade)
                .collect();
            for operation in &operations {
                operation.seal()?;
            }
            let retirement = lifetime
                .disposal
                .census
                .as_ref()
                .map(|census| census.seal(tobkiri_flow::StreamCloseReason::Retired))
                .transpose()?;
            *lifetime
                .disposal
                .retirement
                .try_lock()
                .map_err(|_| anyhow::anyhow!("native retirement state unavailable"))? =
                retirement.clone();
            (retirement, lifetime.token.clone(), operations, snapshot)
        };
        for operation in &operations {
            let _ = operation.notify_native_close();
        }
        if let Some(retirement) = retirement {
            let _ = retirement.notify_close();
        }
        token.cancel();
        Ok(())
    }
    async fn initialize(
        &mut self,
        shared: &Arc<Shared>,
        cx: &CallCx,
        owners: &crate::owned_values::ValueOwners,
        frame: &Arc<dyn crate::ProviderFrame>,
    ) -> Result<()> {
        if self.generation.load(Ordering::Acquire) != self.current_generation
            || self.live.as_ref().is_some_and(|(store, _)| {
                store
                    .data()
                    .retirement
                    .as_ref()
                    .is_some_and(|token| token.is_cancelled())
            })
        {
            self.live.take();
            self.store_disposal.complete();
        }
        if self.live.is_some() {
            return Ok(());
        }
        let init_cx = cx.try_non_interactive_precharged()?;
        let request = EffectRequest::WasmEntry {
            pack: self.pack.manifest.id.clone(),
            hash: self.pack.hash.clone(),
            entry: WasmEntryKind::Init,
        };
        let args = request.digest()?;
        let grant = shared
            .gate
            .authorize(&init_cx, request.clone(), args.clone())
            .await?;
        let grant_id = grant.id().clone();
        let permit = grant.consume(&init_cx, &request, &args)?;
        let init_cx = init_cx.narrow_limits(permit.limits());
        let result=async {
        let retirement = {
            let mut lifetime = self.retirement.lock().map_err(|_|anyhow::anyhow!("generation state poisoned"))?;
            self.current_generation=self.generation.fetch_update(Ordering::AcqRel,Ordering::Acquire,|n|n.checked_add(1)).map_err(|_|anyhow::anyhow!("instance generations exhausted"))?+1;
            let identity_control = shared.control.reserve(tobkiri_gate::ControlKind::Frame, 1024)?;
            let census = GenerationChildCensus::new(shared.control.clone(), tokio::runtime::Handle::try_current()?, NativeGenerationId {
                profile: init_cx.profile().clone(), pack: self.pack.manifest.id.clone(), hash: self.pack.hash.clone(),
                revision: init_cx.revision().context("native generation revision missing")?.clone(),
                security_epoch: init_cx.security_epoch(), generation: self.current_generation,
            }, 256)?;
            *lifetime = GenerationLifetime::new(&shared.control, Some(census))?;
            drop(identity_control);
            self.store_disposal = lifetime.disposal.clone();
            lifetime.token.clone()
        };
        ensure_provider_operation(shared, &self.retirement, frame).await?;
        let wasi=RestrictedWasi::new_with_control(init_cx.limits().output_bytes,&shared.control)?;
        let physical=self.physical.as_ref().context("component physical reservation missing")?.clone();
        let mut store=shared.engine.store_owned(init_cx.clone(),PackState{shared:Arc::downgrade(shared),wasi,owners:owners.clone(),frame:Some(frame.clone()),
            native_generation:self.store_disposal.census.as_ref().context("Store native census missing")?.clone(),
            generation_lifetime:Arc::downgrade(&self.retirement)},physical)
            .map_err(|error|entry_phase_error(error,"store-admission",&init_cx,owners))?;
        store.data_mut().retirement=Some(retirement.clone());
        store.host_allocation_budget(owners.component().clone());
        let mut linker=Linker::new(shared.engine.engine());
        if self.component.component_type().imports(shared.engine.engine()).any(|(name,_)|name.starts_with("wasi:")) {
            crate::wasi::link_restricted_wasi(&mut linker).map_err(|error|entry_phase_error(error,"restricted-wasi-link",&init_cx,owners))?;
            crate::wasi_effects::link_gated_wasi(&mut linker,&self.component,shared.engine.engine()).map_err(|error|entry_phase_error(error,"gated-wasi-link",&init_cx,owners))?;
        }
        crate::core_imports::link_core_and_ports(&mut linker,shared,&self.pack,&self.component,owners).map_err(|error|entry_phase_error(error,"core-import-link",&init_cx,owners))?;
        let instance={
        let _output=store.data().data.wasi.activate_output(&init_cx,owners.graph())?;
        tokio::select!{
            _=retirement.cancelled()=>bail!("component instance retired during initialization"),
            _=cx.cancel().cancelled()=>bail!("component instantiation cancelled"),
            _=tokio::time::sleep_until(cx.deadline().into())=>bail!("component instantiation deadline elapsed"),
            result=linker.instantiate_async(&mut store,&self.component)=>result.map_err(|error|entry_phase_error(error.into(),"instantiate",&init_cx,owners))?,
        }};
        self.live=Some((store,instance));
        let config=Val::String(wasmtime::component::BudgetedString::copy_from(std::str::from_utf8(&self.config)?,owners.component(),0)
            .map_err(|error|entry_phase_error(error.into(),"lifecycle-config",&init_cx,owners))?);
        let mut lifecycle=None;
        for (name,_) in self.component.component_type().exports(shared.engine.engine()) {
            if matches!(name,"tobkiri:core/lifecycle@1.0.0"|"tobkiri:core/lifecycle@1.1.0")
                && lifecycle.replace(if name.ends_with("@1.0.0"){"tobkiri:core/lifecycle@1.0.0"}else{"tobkiri:core/lifecycle@1.1.0"}).is_some()
            {
                bail!("component exports ambiguous lifecycle package versions")
            }
        }
        let lifecycle=lifecycle.context("component lifecycle export missing")?;
        let result=self.call(&init_cx,lifecycle,"init",&[config]).await.map_err(|error|entry_phase_error(error,"lifecycle-init",&init_cx,owners));
        let results=result?;
        if !matches!(results.as_slice(),[Val::Result(Ok(None))]){bail!("component lifecycle initialization failed")}
        Ok(())
        }.await;
        if result.is_err() {
            // Preserve the original initialization error and its audit path.
            let _ = self.dispose_failed_initialization();
        }
        shared
            .gate
            .record_outcome(
                grant_id,
                if result.is_ok() {
                    Outcome::Succeeded
                } else if cx.cancel().is_cancelled() {
                    Outcome::Cancelled
                } else {
                    Outcome::Failed {
                        code: "wasm-init".into(),
                    }
                },
            )
            .await?;
        result
    }
    async fn call(
        &mut self,
        cx: &CallCx,
        interface: &str,
        op: &str,
        args: &[Val],
    ) -> Result<Vec<Val>> {
        let owners = self
            .live
            .as_ref()
            .context("component instance unavailable")?
            .0
            .data()
            .data
            .owners
            .clone();
        Ok(self
            .call_owned(cx, interface, op, args, &owners)
            .await?
            .as_slice()
            .to_vec())
    }
    async fn call_owned(
        &mut self,
        cx: &CallCx,
        interface: &str,
        op: &str,
        args: &[Val],
        owners: &crate::owned_values::ValueOwners,
    ) -> Result<wasmtime::component::BudgetedVec<Val>> {
        cx.check_active()?;
        let (store, instance) = self
            .live
            .as_mut()
            .context("component instance unavailable")?;
        store.data_mut().cx = cx.clone();
        store.data_mut().data.owners = owners.clone();
        store.host_allocation_budget(owners.component().clone());
        let _output = store.data().data.wasi.activate_output(cx, owners.graph())?;
        store.data_mut().apply_limits(cx.limits())?;
        store.set_fuel(cx.limits().fuel_per_call)?;
        store.set_epoch_deadline(1);
        let interface = instance
            .get_export_index(&mut *store, None, interface)
            .context("declared component interface missing")?;
        let export = instance
            .get_export_index(&mut *store, Some(&interface), op)
            .context("declared component operation missing")?;
        let function = instance
            .get_func(&mut *store, export)
            .context("component operation is not a function")?;
        let retirement = store
            .data()
            .retirement
            .clone()
            .context("component retirement authority missing")?;
        tokio::select! {
            _=retirement.cancelled()=>bail!("component instance retired"),
            _=cx.cancel().cancelled()=>bail!("component call cancelled"),
            _=tokio::time::sleep_until(cx.deadline().into())=>bail!("original component deadline elapsed"),
            result=function.call_async_owned(&mut *store,args)=>Ok(result?),
        }
    }
}

pub(crate) fn interface_name(shape: &ShapeRef) -> String {
    let owner = shape.name.owner();
    format!(
        "{}:{}/{}@{}.{}.0",
        owner.publisher(),
        owner.name(),
        shape.name.interface(),
        shape.version.major,
        shape.version.minor
    )
}

/// Closed entry diagnostics distinguish an unavailable lift path from expired
/// inherited authority. Never interpolate guest text, SQL or host diagnostics.
fn entry_phase_error(
    error: anyhow::Error,
    phase: &'static str,
    cx: &CallCx,
    owners: &crate::owned_values::ValueOwners,
) -> anyhow::Error {
    use wasmtime::component::HostAllocationError;
    let allocation = error
        .chain()
        .find_map(|cause| cause.downcast_ref::<HostAllocationError>());
    let kind = match allocation {
        Some(HostAllocationError::Unavailable) => "allocation-unavailable",
        Some(HostAllocationError::Limit) => "allocation-limit",
        Some(HostAllocationError::Overflow) => "allocation-overflow",
        Some(HostAllocationError::Depth) => "allocation-depth",
        Some(HostAllocationError::Allocator) => "allocator-failure",
        Some(HostAllocationError::Invalid) => "invalid-allocation",
        None => "execution-error",
    };
    let now = std::time::Instant::now();
    let abi_stage = error
        .downcast_ref::<wasmtime::component::CanonicalCallStage>()
        .or_else(|| {
            error
                .chain()
                .find_map(|cause| cause.downcast_ref::<wasmtime::component::CanonicalCallStage>())
        })
        .map_or("unknown", |stage| stage.as_str());
    let active = cx
        .check_active()
        .err()
        .map_or("active", |error| error.code());
    let wall_remaining_ms = cx.deadline().saturating_duration_since(now).as_millis();
    let loop_remaining_ms = cx
        .budget()
        .map(|budget| budget.deadline().saturating_duration_since(now).as_millis());
    error.context(format!(
        "component phase={phase} abi_stage={abi_stage} failure={kind} context={active} wall_remaining_ms={wall_remaining_ms} loop_remaining_ms={loop_remaining_ms:?} declared_wall_ms={} owner_failure={}",
        cx.limits().wall_ms, owners.allocation_failure_code(),
    ))
}
pub(crate) fn step_error(error: anyhow::Error) -> StepError {
    step_error_ref(&error)
}
/// Closed audit classification over the borrowed original error. It keeps
/// step_error_ref's precedence without cloning a StepError, copying a
/// message, or formatting Display/Debug/source chains for audit metadata.
pub(crate) fn step_audit_code(error: &anyhow::Error) -> &'static str {
    // Sealed storage codes stay outside the audit allowlist and classify as
    // internal, exactly as step_error_ref().audit_code() maps them.
    if crate::core_imports::storage_failure_metadata(error).is_some() {
        return "internal";
    }
    if let Some(error) = error.downcast_ref::<StepError>() {
        return error.audit_code();
    }
    match error.downcast_ref::<tobkiri_gate::GateError>() {
        Some(gate) => gate_audit_code(gate),
        None => "internal",
    }
}
fn step_error_ref(error: &anyhow::Error) -> StepError {
    if let Some((code, message)) = crate::core_imports::storage_failure_metadata(error) {
        return StepError::new(code, message);
    }
    if let Some(error) = error.downcast_ref::<StepError>() {
        return error.clone();
    }
    let code = match error.downcast_ref::<tobkiri_gate::GateError>() {
        Some(gate) => gate_audit_code(gate),
        None => "component",
    };
    StepError::new(code, error.to_string())
}
/// Closed Gate-to-audit mapping shared by the StepError projection and the
/// borrowed audit classifier. It intentionally differs from GateError::code().
fn gate_audit_code(error: &tobkiri_gate::GateError) -> &'static str {
    match error {
        tobkiri_gate::GateError::Limit(_) | tobkiri_gate::GateError::Deadline => "limit-exceeded",
        tobkiri_gate::GateError::Cancelled => "cancelled",
        tobkiri_gate::GateError::ApprovalRequired => "approval-required",
        _ => "denied",
    }
}

#[async_trait]
impl PortInvoker for PackHost {
    fn reserve_frame_control(
        &self,
        cx: &CallCx,
        prepared: &dyn tobkiri_gate::PreparedFrameVerifier,
        reason: tobkiri_gate::AllocationReason,
        bytes: u64,
    ) -> Result<tobkiri_gate::CapacityReservation, StepError> {
        PackHost::reserve_frame_control(self, cx, prepared, reason, bytes).map_err(step_error)
    }

    fn allocation_budget_for_frame(
        &self,
        cx: &CallCx,
        scope: tobkiri_gate::AllocationFrameScope,
    ) -> Result<Arc<dyn tobkiri_canon::AllocationBudget>, StepError> {
        PackHost::allocation_budget_for_frame(self, cx, scope).map_err(step_error)
    }

    async fn call(
        &self,
        cx: &CallCx,
        shape: &ShapeRef,
        op: &OpName,
        args: tobkiri_canon::CoreValue,
    ) -> Result<StepOutput, StepError> {
        self.call_owned(cx, shape, op, args).await
    }
    async fn call_reached(
        &self,
        cx: &CallCx,
        frame: &tobkiri_canon::ControlHandle<dyn tobkiri_flow::ActiveCodeFrame>,
        node: &tobkiri_flow::ReachedNode,
        arguments_digest: &tobkiri_canon::Digest,
        args: tobkiri_canon::CoreValue,
    ) -> Result<StepOutput, StepError> {
        self.call_reached_owned(cx, frame, node, arguments_digest, args)
            .await
    }
    async fn emit_reached(
        &self,
        cx: &CallCx,
        frame: &tobkiri_canon::ControlHandle<dyn tobkiri_flow::ActiveCodeFrame>,
        node: &tobkiri_flow::ReachedNode,
        event_digest: &tobkiri_canon::Digest,
        event: tobkiri_canon::CoreValue,
    ) -> Result<(), StepError> {
        self.emit_reached_owned(cx, frame, node, event_digest, event)
            .await
    }
    async fn call_final(
        &self,
        cx: &CallCx,
        shape: &ShapeRef,
        op: &OpName,
        output: tobkiri_flow::FinalOutputKind,
        args: tobkiri_canon::CoreValue,
    ) -> Result<tobkiri_flow::FinalPortOutput, StepError> {
        self.call_owned_final(cx, shape, op, output, args).await
    }

    async fn call_ui_final(
        &self,
        cx: &CallCx,
        entry: &tobkiri_flow::UiInvocation<'_>,
        preparation: &Arc<dyn tobkiri_flow::UiInvocationPreparation>,
        arguments_digest: &tobkiri_canon::Digest,
        args: tobkiri_canon::CoreValue,
        output: tobkiri_flow::FinalOutputKind,
    ) -> Result<tobkiri_flow::FinalPortOutput, StepError> {
        let (pack, provided, _) = self
            .resolve_for_admission(cx, entry.view, entry.op, true)
            .map_err(step_error)?;
        if pack.manifest.id != entry.target.pack
            || pack.hash != entry.target.hash
            || provided.reference != entry.target.provided
        {
            return Err(StepError::new(
                "denied",
                "UI entry target differs from actual routing",
            ));
        }
        // Session computed this digest under the actual UI RuntimeValue scope.
        // The provider independently checks the same immutable args before any
        // guest execution; no unowned preclaim serialization is introduced.
        let admission = self
            .shared
            .gate
            .admit_port(
                cx,
                entry.view.clone(),
                entry.op.clone(),
                arguments_digest.clone(),
                tobkiri_gate::FrozenPortTarget {
                    pack: entry.target.pack.clone(),
                    hash: entry.target.hash.clone(),
                    provided: entry.target.provided.clone(),
                },
            )
            .await
            .map_err(|error| step_error(error.into()))?;
        self.dispatch_admitted_final(
            cx,
            admission,
            args,
            crate::ProviderOrigin::UiInvocation { entry, preparation },
            output,
        )
        .await
    }
    async fn call_user_final(
        &self,
        cx: &CallCx,
        entry: &tobkiri_flow::FlowUserInvocation<'_>,
        preparation: &Arc<dyn tobkiri_flow::FlowUserInvocationPreparation>,
        arguments_digest: &tobkiri_canon::Digest,
        args: tobkiri_canon::CoreValue,
        output: tobkiri_flow::FinalOutputKind,
    ) -> Result<tobkiri_flow::FinalPortOutput, StepError> {
        if !matches!(cx.principal(), Principal::User) {
            self.shared
                .gate
                .record_denial(cx, "user-entry", "principal-mismatch")
                .await
                .map_err(|error| step_error(error.into()))?;
            return Err(StepError::new(
                "denied",
                "User entry requires the actual User principal",
            ));
        }
        let (pack, provided, _) = self
            .resolve_for_admission(cx, entry.view, entry.op, true)
            .map_err(step_error)?;
        if pack.manifest.id != entry.target.pack
            || pack.hash != entry.target.hash
            || provided.reference != entry.target.provided
        {
            self.shared
                .gate
                .record_denial(cx, "user-entry", "target-mismatch")
                .await
                .map_err(|error| step_error(error.into()))?;
            return Err(StepError::new(
                "denied",
                "User entry target differs from actual routing",
            ));
        }
        // Core hashes actual arguments under this same User preparation before
        // this call. The exact private preparation survives both validation
        // and claim; the provider rehashes again before actual guest execution.
        let admission = self
            .shared
            .gate
            .admit_port(
                cx,
                entry.view.clone(),
                entry.op.clone(),
                arguments_digest.clone(),
                tobkiri_gate::FrozenPortTarget {
                    pack: entry.target.pack.clone(),
                    hash: entry.target.hash.clone(),
                    provided: entry.target.provided.clone(),
                },
            )
            .await
            .map_err(|error| step_error(error.into()))?;
        self.dispatch_admitted_final(
            cx,
            admission,
            args,
            crate::ProviderOrigin::UserInvocation { entry, preparation },
            output,
        )
        .await
    }
    async fn read_native_input(
        &self,
        cx: &CallCx,
        entry: &tobkiri_profile::CompiledInputEntry,
    ) -> Result<tobkiri_flow::NativeInputRead, StepError> {
        self.read_native_input_owned(cx, entry)
            .await
            .map_err(step_error)
    }
    async fn emit(
        &self,
        _cx: &CallCx,
        _out: &ShapeRef,
        _event: tobkiri_canon::CoreValue,
    ) -> Result<(), StepError> {
        // Reached Flow emission builds its argument object under the actual
        // reached node's budget and uses dispatch_admitted. This legacy seam
        // has no producing-node proof and must not invent a sponsor.
        Err(StepError::new(
            "unavailable",
            "output emission requires a reached Core node",
        ))
    }
}

pub(crate) struct ComponentCloseControl {
    shared: Weak<Shared>,
    // Captured before the companion is attached, so it cannot own itself.
    entry: crate::HandleEntry,
    _registration: tobkiri_gate::ControlReservation,
}
impl tobkiri_flow::StreamCloseControl for ComponentCloseControl {
    fn record_close(&self, reason: tobkiri_flow::StreamCloseReason) -> Result<(), StepError> {
        match self.select_typed(reason, None) {
            CleanupSelection::Selected | CleanupSelection::Vacant => Ok(()),
            CleanupSelection::Busy => Err(StepError::new(
                "unavailable",
                "cleanup authority snapshot is busy",
            )),
            CleanupSelection::Uncertain(error) => Err(error),
        }
    }
    fn record_failure(&self, error: &StepError) -> Result<(), StepError> {
        match self.select_typed(
            tobkiri_flow::StreamCloseReason::Unknown,
            Some(error.audit_code()),
        ) {
            CleanupSelection::Selected | CleanupSelection::Vacant => Ok(()),
            CleanupSelection::Busy => Err(StepError::new(
                "unavailable",
                "cleanup authority snapshot is busy",
            )),
            CleanupSelection::Uncertain(error) => Err(error),
        }
    }
}
/// Typed outcome of one native cause-selection attempt. `Busy` is only
/// authenticated pre-callback contention — the authority snapshot reported
/// its own connection/BEGIN busy before `inspect` ran, or a caller-held
/// guard was transiently unavailable — so the same cleanup owner may retry
/// it inside the original bounded episode. `Uncertain` carries the real
/// failure; it is never inferred from a StepError code or message.
pub(crate) enum CleanupSelection {
    /// No immutable cause is selected yet (internal observation only).
    Vacant,
    /// An immutable cause is selected (by this or a concurrent winner).
    Selected,
    /// Authenticated pre-callback contention; bounded same-owner retry.
    Busy,
    /// Settled failure or post-callback uncertainty.
    Uncertain(StepError),
}
impl ComponentCloseControl {
    fn observed(&self) -> CleanupSelection {
        // A selected immutable cause needs no new authority observation. Never
        // make a losing observer's Core-lock contention taint that winner.
        for _ in 0..64 {
            match self.entry.lease.cause.try_lock() {
                Ok(cause) => {
                    if self.entry.lease.cause_uncertain.load(Ordering::Acquire) {
                        return CleanupSelection::Uncertain(StepError::new(
                            "unavailable",
                            "earlier cleanup authority observation was uncertain",
                        ));
                    }
                    if cause.is_none() {
                        return CleanupSelection::Vacant;
                    }
                    return if self.entry.lease.cause_uncertain.load(Ordering::Acquire) {
                        CleanupSelection::Uncertain(StepError::new(
                            "unavailable",
                            "earlier cleanup authority observation was uncertain",
                        ))
                    } else {
                        CleanupSelection::Selected
                    };
                }
                // WouldBlock is a positive transient signal; the spin below
                // retries it. A poisoned slot is instead a distinct permanent
                // uncertainty and must never be masked as busy.
                Err(std::sync::TryLockError::WouldBlock) => std::hint::spin_loop(),
                Err(std::sync::TryLockError::Poisoned(_)) => {
                    return CleanupSelection::Uncertain(StepError::new(
                        "unavailable",
                        "cleanup cause state poisoned",
                    ));
                }
            }
        }
        CleanupSelection::Busy
    }
    /// One synchronous selection attempt; the bounded async retry belongs to
    /// the existing cleanup owner and happens strictly outside all guards
    /// held here.
    pub(crate) fn select_typed(
        &self,
        reason: tobkiri_flow::StreamCloseReason,
        failure: Option<&'static str>,
    ) -> CleanupSelection {
        use crate::handles::SelectedCloseCause as Cause;
        use tobkiri_flow::StreamCloseReason as Reason;
        let entry = &self.entry;
        // Publish the single original episode and the native pending intent
        // synchronously, before any wake: an empty cause slot is thereafter
        // unconfirmed rather than a settled unknown, and the first intent's
        // reason/failure/manual provenance survives busy retries.
        entry.lease.cleanup_episode_bound(entry.owner.deadline());
        let intent = match entry.lease.publish_pending_intent(
            reason,
            failure,
            entry.owner.cancel().is_cancelled(),
        ) {
            Ok(intent) => intent,
            Err(_) => {
                return CleanupSelection::Uncertain(StepError::new(
                    "unavailable",
                    "cleanup intent state poisoned",
                ));
            }
        };
        match self.observed() {
            CleanupSelection::Vacant => {}
            settled => {
                if !matches!(settled, CleanupSelection::Busy) {
                    entry.lease.cause_pending.store(false, Ordering::Release);
                }
                return settled;
            }
        }
        let Some(shared) = self.shared.upgrade() else {
            return CleanupSelection::Uncertain(StepError::new(
                "unavailable",
                "cleanup host unavailable",
            ));
        };
        let host = PackHost { shared };
        let Some(frame) = entry.source_frame.as_ref() else {
            return CleanupSelection::Uncertain(StepError::new(
                "unavailable",
                "cleanup source frame unavailable",
            ));
        };
        let Some((pack, _)) = entry.owner.principal().pack() else {
            return CleanupSelection::Uncertain(StepError::new(
                "unavailable",
                "cleanup owner unavailable",
            ));
        };
        let slot = match host.instance(pack).map_err(step_error) {
            Ok(slot) => slot,
            Err(error) => return CleanupSelection::Uncertain(error),
        };
        // Busy observed only inside the lifecycle callback; it is retryable
        // only if the outer lifecycle attempt itself completed Selected.
        let mut inner_busy = false;
        let attempt = frame.try_cleanup_lifecycle(&mut |lifecycle| {
            let generation = match slot.retirement.try_lock() {
                Ok(generation) => generation,
                Err(std::sync::TryLockError::WouldBlock) => {
                    inner_busy = true;
                    return Ok(());
                }
                Err(std::sync::TryLockError::Poisoned(_)) => {
                    return Err(StepError::new(
                        "unavailable",
                        "cleanup generation state poisoned",
                    ));
                }
            };
            let mut cause = match entry.lease.cause.try_lock() {
                Ok(cause) => cause,
                Err(std::sync::TryLockError::WouldBlock) => {
                    inner_busy = true;
                    return Ok(());
                }
                Err(std::sync::TryLockError::Poisoned(_)) => {
                    return Err(StepError::new(
                        "unavailable",
                        "cleanup cause state poisoned",
                    ));
                }
            };
            if cause.is_some() {
                return if entry.lease.cause_uncertain.load(Ordering::Acquire) {
                    Err(StepError::new(
                        "unavailable",
                        "earlier cleanup authority observation was uncertain",
                    ))
                } else {
                    Ok(())
                };
            }
            // The original intent was published before any wake; a later
            // owner-cancel wake can never rewrite a Manual intent into
            // Unknown. The intent is provenance only: epoch, deadline,
            // lifecycle, source and generation are still rechecked on every
            // coherent attempt before Manual may be selected.
            let requested = lifecycle.requested.unwrap_or(intent.reason);
            let manual_before_wake =
                lifecycle.requested == Some(Reason::Manual) || !intent.wake_before_intent;
            let source_live = lifecycle.live && !lifecycle.closing;
            let generation_live = entry.generation == Some(slot.generation.load(Ordering::Acquire))
                && !generation.retiring
                && !generation.token.is_cancelled();
            let original_epoch = entry.owner.security_epoch();
            let Some(permit) = entry.lease.permit.as_ref() else {
                return Err(StepError::new("unavailable", "cleanup permit unavailable"));
            };
            match host
                .shared
                .gate
                .try_cleanup_snapshot(permit, &mut |snapshot| {
                    *cause = Some(if let Some(terminal) = snapshot.authority.terminal {
                        Cause::Terminal(terminal.decision.outcome.clone())
                    } else if snapshot.observed_at >= snapshot.original_deadline
                        || snapshot.authority.now_unix_ms
                            >= snapshot.authority.reservation.expires_unix_ms
                    {
                        Cause::Deadline
                    } else if snapshot.authority.current_epoch != original_epoch {
                        Cause::Revoked
                    } else if !generation_live || !lifecycle.live {
                        Cause::Retired
                    } else if requested == Reason::Shutdown {
                        Cause::Shutdown
                    } else if !source_live {
                        Cause::Retired
                    } else if let Some(code) = intent.failure {
                        Cause::ProducerFailure(code)
                    } else {
                        match requested {
                            Reason::Manual if manual_before_wake => Cause::Manual,
                            Reason::Manual => Cause::Unknown,
                            Reason::Deadline => Cause::Deadline,
                            Reason::Shutdown | Reason::Retired => Cause::Retired,
                            Reason::Unknown => Cause::Unknown,
                        }
                    });
                    // Actual selection clears the outstanding pending marker.
                    entry.lease.cause_pending.store(false, Ordering::Release);
                    Ok(())
                }) {
                tobkiri_gate::CleanupSnapshotAttempt::Selected => Ok(()),
                tobkiri_gate::CleanupSnapshotAttempt::RetryableBusyBeforeCallback => {
                    // Authenticated: the snapshot callback provably never ran.
                    inner_busy = true;
                    Ok(())
                }
                tobkiri_gate::CleanupSnapshotAttempt::Uncertain(error) => {
                    // Still holding the cause mutex: another closer cannot
                    // mistake post-selection teardown failure for success.
                    entry.lease.cause_uncertain.store(true, Ordering::Release);
                    entry.lease.cause_pending.store(false, Ordering::Release);
                    Err(StepError::new(
                        "unavailable",
                        format!("cleanup authority snapshot failed: {error}"),
                    ))
                }
            }
        });
        match attempt {
            crate::CleanupLifecycleAttempt::RetryableBusyBeforeCallback => CleanupSelection::Busy,
            crate::CleanupLifecycleAttempt::Selected if inner_busy => CleanupSelection::Busy,
            crate::CleanupLifecycleAttempt::Selected => CleanupSelection::Selected,
            // Guards have ended. A concurrent selector may have established
            // the original winner; only a real snapshot error latches the
            // permanent uncertainty bit while holding the cause mutex.
            // Outer post-callback or guard failure is always authoritative;
            // no inner busy classification may override it.
            crate::CleanupLifecycleAttempt::Uncertain(error) => CleanupSelection::Uncertain(error),
        }
    }
}

struct ComponentStream {
    host: PackHost,
    cx: CallCx,
    handle: u64,
    entry: crate::HandleEntry,
    ended: bool,
    runtime: tokio::runtime::Handle,
    cleanup: Option<tokio::task::JoinHandle<Result<(), StepError>>>,
    cleanup_error: Option<StepError>,
    terminal_batch: Option<tobkiri_canon::CoreValue>,
    terminal_buffer: Option<tobkiri_canon::CoreBuffer>,
    cancelled_terminal: bool,
    _registration: tobkiri_gate::ControlReservation,
}
impl ComponentStream {
    async fn await_cleanup(&mut self) -> Result<(), StepError> {
        if let Some(cleanup) = self.cleanup.as_mut() {
            // Retain the task if the caller abandons this await. A later close
            // must still observe the same cleanup result, rather than success.
            let result = tokio::time::timeout(std::time::Duration::from_secs(2), cleanup)
                .await
                .map_err(|_| {
                    StepError::new(
                        "unavailable",
                        "component cleanup did not finish within its grace",
                    )
                })?
                .map_err(|_| {
                    StepError::new(
                        "unavailable",
                        "component stream cleanup task did not finish",
                    )
                })
                .and_then(|result| result);
            self.cleanup = None;
            if let Err(error) = &result {
                self.cleanup_error = Some(error.clone());
            }
            return result;
        }
        if let Some(error) = &self.cleanup_error {
            return Err(error.clone());
        }
        Ok(())
    }
    /// Only a proven manual terminal may serve the prefunded empty pair: the
    /// immutable selected cause is actually Manual (never Revoked, Shutdown,
    /// Deadline, retired or unknown — an authoritative Revoked winner stays
    /// Revoked even when its outcome settled as Cancelled), no earlier
    /// authority observation was uncertain, physical cleanup completed, and
    /// the terminal audit acknowledgement delivered that same Cancelled
    /// winner. EOF is a separate terminal family and cache presence alone is
    /// never Succeeded.
    async fn confirmed_manual_terminal(&mut self) -> bool {
        if self.cancelled_terminal {
            return true;
        }
        if self.terminal_batch.is_some() || self.terminal_buffer.is_some() {
            return false;
        }
        if self.entry.lease.cause_uncertain.load(Ordering::Acquire) {
            return false;
        }
        let completed = {
            let receipt = self.entry.lease.close.lock().await;
            receipt.cleanup_done
                && receipt.delivered
                && matches!(receipt.outcome, Some(Outcome::Cancelled))
        };
        // Reobserve the exact immutable cause; a transiently busy or pending
        // slot is never a settled unknown and wins no fresh grant or effect.
        let confirmed = completed
            && matches!(
                crate::core_imports::observe_cause_bounded(&self.entry).await,
                crate::core_imports::CauseObservation::Observed(Some(
                    crate::handles::SelectedCloseCause::Manual
                ))
            );
        self.cancelled_terminal = confirmed;
        confirmed
    }
    fn queue_cleanup(&mut self) {
        let host = self.host.clone();
        let cx = self.cx.clone();
        let handle = self.handle;
        let entry = self.entry.clone();
        let task_control = match self
            .host
            .shared
            .control
            .reserve(tobkiri_gate::ControlKind::Task, 4096)
        {
            Ok(control) => control,
            Err(_) => {
                self.cleanup_error = Some(StepError::new(
                    "limit-exceeded",
                    "component cleanup task capacity unavailable",
                ));
                return;
            }
        };
        let queued = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            self.runtime.spawn(async move {
                let _task_control = task_control;
                crate::core_imports::cancel_captured_stream(&host, &cx, handle, &entry)
                    .await
                    .map_err(step_error)
            })
        }));
        match queued {
            Ok(task) => self.cleanup = Some(task),
            Err(_) => {
                self.cleanup_error = Some(StepError::new(
                    "unavailable",
                    "component cleanup runtime is unavailable",
                ))
            }
        }
    }
}
#[async_trait]
impl StreamSource for ComponentStream {
    fn close_report(&self) -> Option<tobkiri_flow::StreamCloseReport> {
        if self.cleanup.is_some() || self.entry.lease.cause_uncertain.load(Ordering::Acquire) {
            return None;
        }
        let receipt = self.entry.lease.close.try_lock().ok()?;
        if !receipt.cleanup_done
            || !receipt.delivered
            || !receipt.close_report_confirmed
            || receipt.deadline_settlement
        {
            return None;
        }
        let outcome = receipt.outcome.as_ref()?;
        if self.entry.source.is_some() {
            // Same native source Arc/lease as release_stream's actual close and
            // physical wait. Its report is from the original FlowRun terminal;
            // this handle owns no substitute StreamOpen reservation.
            let report = receipt.source_report?;
            if !report.matches_outcome(outcome)
                || (self.cleanup_error.is_some()
                    && report.business_error().is_none()
                    && report.audit_error().is_none())
            {
                return None;
            }
            return Some(report);
        }
        // Guest sources require the exact operation/generation's independent
        // Store destruction and scoped children, never the whole generation.
        let operation = self.entry.operation.as_ref()?;
        let disposal = self.entry.store_disposal.as_ref()?;
        if self.entry.generation != Some(operation.inner.generation.generation)
            || !Arc::ptr_eq(disposal, &operation.inner.disposal)
            || !disposal.is_complete()
            || self.cleanup_error.is_some()
        {
            return None;
        }
        tobkiri_flow::StreamCloseReport::observed(
            outcome,
            None,
            None,
            tobkiri_flow::StreamCloseDelivery::Acknowledged,
        )
    }
    fn physical_completion(&self) -> Option<tobkiri_flow::StreamPhysicalCompletion> {
        self.entry.physical_completion.clone()
    }
    fn close_control(&self) -> Option<Arc<dyn tobkiri_flow::StreamCloseControl>> {
        self.entry.close_control.clone()
    }

    async fn pull(&mut self, max: u32) -> Result<tobkiri_canon::CoreValue, StepError> {
        self.await_cleanup().await?;
        if self.ended {
            if let Some(batch) = &self.terminal_batch {
                return Ok(batch.clone());
            }
            // A confirmed EOF in the opposite mode reuses the prefunded pair
            // while remaining a Succeeded terminal; a proven Manual terminal
            // may serve it as a distinct Cancelled outcome. Everything else
            // retains the original closed error.
            let reusable = self.terminal_buffer.is_some() || self.confirmed_manual_terminal().await;
            if reusable && let Some(cache) = &self.entry.terminal_empty {
                return Ok(cache.batch.clone());
            }
            return Err(StepError::new("cancelled", "component source is closed"));
        }
        self.host
            .shared
            .gate
            .check_context(&self.cx)
            .map_err(|e| step_error(e.into()))?;
        let result = if let Some(source) = &self.entry.source {
            source.lock().await.pull(max).await
        } else {
            crate::core_imports::pull_guest_source(&self.host, &self.entry, max, false)
                .await
                .map(|result| result.0)
                .map_err(step_error)
        };
        let value = match result {
            Ok(value) => value,
            Err(error) => {
                if let Some(control) = &self.entry.close_control {
                    let _ = control.record_failure(&error);
                }
                self.cancel();
                let cleanup = self.await_cleanup().await;
                return match cleanup {
                    Ok(()) => Err(error),
                    Err(cleanup) => Err(StepError::new(
                        error.code,
                        format!("{}; cleanup: {}", error.message, cleanup.audit_code()),
                    )),
                };
            }
        };
        if value.array().is_some_and(|array| array.is_empty()) {
            crate::core_imports::finish_stream_eof(&self.host, &self.cx, self.handle, &self.entry)
                .await
                .map_err(step_error)?;
            self.terminal_batch = Some(value.clone());
            self.ended = true;
        }
        Ok(value)
    }
    async fn pull_final(&mut self, max: u32) -> Result<tobkiri_canon::CoreBuffer, StepError> {
        self.await_cleanup().await?;
        if self.ended {
            if let Some(buffer) = &self.terminal_buffer {
                return Ok(buffer.clone());
            }
            let reusable = self.terminal_batch.is_some() || self.confirmed_manual_terminal().await;
            if reusable && let Some(cache) = &self.entry.terminal_empty {
                return Ok(cache.buffer.clone());
            }
            return Err(StepError::new("cancelled", "component source is closed"));
        }
        self.host
            .shared
            .gate
            .check_context(&self.cx)
            .map_err(|e| step_error(e.into()))?;
        let (buffer, empty) = if let Some(source) = &self.entry.source {
            let buffer = source.lock().await.pull_final(max).await?;
            let empty = buffer.as_bytes() == b"[]";
            (buffer, empty)
        } else {
            let (values, buffer) =
                crate::core_imports::pull_guest_source(&self.host, &self.entry, max, true)
                    .await
                    .map_err(step_error)?;
            (
                buffer.ok_or_else(|| StepError::new("invalid", "producer final batch missing"))?,
                values.array().is_some_and(|array| array.is_empty()),
            )
        };
        if empty {
            crate::core_imports::finish_stream_eof(&self.host, &self.cx, self.handle, &self.entry)
                .await
                .map_err(step_error)?;
            self.terminal_buffer = Some(buffer.clone());
            self.ended = true;
        }
        Ok(buffer)
    }
    fn terminal_outcome(&self) -> Option<tobkiri_flow::StreamTerminal> {
        if self.terminal_batch.is_some() || self.terminal_buffer.is_some() {
            Some(tobkiri_flow::StreamTerminal::Succeeded)
        } else if self.cancelled_terminal {
            Some(tobkiri_flow::StreamTerminal::Cancelled)
        } else {
            None
        }
    }
    fn cancel(&mut self) {
        if self.ended {
            return;
        }
        self.ended = true;
        // The caller can drop/cancel from a plain thread. Queue on the runtime
        // that created this stream, and retain the result for close()/pull().
        self.queue_cleanup();
    }
    async fn close(&mut self) -> Result<(), StepError> {
        if let Some(control) = &self.entry.close_control {
            // An existing winner/uncertainty is immutable. This synchronous
            // intent precedes queuing the actual cleanup worker.
            let _ = control.record_close(tobkiri_flow::StreamCloseReason::Manual);
        }
        if self.ended && self.cleanup.is_none() && self.cleanup_error.is_some() {
            // Retry only retained terminal delivery; the handle receipt proves
            // whether physical cleanup already ran and forbids running it twice.
            self.cleanup_error = None;
            self.queue_cleanup();
        }
        self.cancel();
        let result = self.await_cleanup().await;
        if result.is_ok() {
            // A proven manual terminal stays a distinct Cancelled outcome,
            // never EOF success; only then may later pulls reuse the pair.
            let _ = self.confirmed_manual_terminal().await;
        }
        result
    }
}

#[cfg(test)]
#[path = "store_disposal_tests.rs"]
mod store_disposal_tests;

#[cfg(test)]
mod native_mailbox_tests {
    use super::*;
    use crate::ProviderFrame as _;
    use crate::child_census::{CensusError, NativeExecutionId};
    use tobkiri_canon::Digest;
    use tobkiri_flow::{RunTaskSpawner, StreamCloseControl, StreamCloseReason};
    use tobkiri_gate::CoreControl;

    fn indexed_operation(
        control: &CoreControl,
        lifetime: &Arc<StdMutex<GenerationLifetime>>,
        activation: u64,
    ) -> ProviderOperationScope {
        indexed_operation_with_parent(control, lifetime, activation, None)
    }

    fn indexed_operation_with_parent(
        control: &CoreControl,
        lifetime: &Arc<StdMutex<GenerationLifetime>>,
        activation: u64,
        parent: Option<ProviderOperationScope>,
    ) -> ProviderOperationScope {
        let mut lifetime = lifetime.lock().unwrap();
        let mut registration =
            GenerationOperationRegistration::reserve(&lifetime.operation_index).unwrap();
        let operation = ProviderOperationScope::new(
            control,
            Weak::new(),
            &NativeGenerationId {
                profile: ProfileId::parse("index-test").unwrap(),
                pack: PackId::parse("local.index").unwrap(),
                hash: PackHash::new(Digest::hash(b"component")),
                revision: Digest::hash(b"revision"),
                security_epoch: 1,
                generation: 1,
            },
            NativeExecutionId {
                frame: Digest::hash(&activation.to_le_bytes()),
                request: Digest::hash(b"index-test-request"),
                activation,
            },
            lifetime.disposal.clone(),
            parent,
        )
        .unwrap();
        registration.scope = Some(operation.weak_for_index());
        lifetime.operations.push(registration);
        operation
    }

    use std::collections::VecDeque;

    macro_rules! provider_frame_stubs {
        () => {
            fn allocation_scope(
                &self,
                _: tobkiri_gate::AllocationReason,
            ) -> Result<tobkiri_gate::AllocationFrameScope, StepError> {
                panic!("stub")
            }
            fn output_ready(
                &self,
                _: &tobkiri_canon::CoreValue,
                _: &Arc<dyn tobkiri_canon::AllocationBudget>,
            ) -> Result<(), StepError> {
                panic!("stub")
            }
            fn stream_ready(&self) -> Result<(), StepError> {
                panic!("stub")
            }
            fn terminal_acknowledged(
                &self,
                _: &tobkiri_gate::AcknowledgedTerminal,
            ) -> Result<(), StepError> {
                panic!("stub")
            }
            fn take_source_read_receipt(
                &self,
            ) -> Result<tobkiri_gate::SourceReadReceipt, StepError> {
                panic!("stub")
            }
        };
    }

    enum BindStep {
        Bound,
        Busy,
    }
    struct ScriptedFrame {
        cx: CallCx,
        ids: StdMutex<VecDeque<crate::NativeIdAttempt>>,
        binds: StdMutex<VecDeque<BindStep>>,
        id_calls: AtomicUsize,
        bind_calls: AtomicUsize,
        indexes: StdMutex<Vec<usize>>,
    }
    impl tobkiri_flow::GuestFlowFrame for ScriptedFrame {
        fn as_any(&self) -> &dyn std::any::Any {
            self
        }
    }
    impl crate::ProviderFrame for ScriptedFrame {
        fn context(&self) -> &CallCx {
            &self.cx
        }
        fn try_native_execution_id(&self) -> crate::NativeIdAttempt {
            self.id_calls.fetch_add(1, Ordering::AcqRel);
            self.ids.lock().unwrap().pop_front().expect("scripted id")
        }
        fn try_bind_provider_operation(
            &self,
            scope: ProviderOperationScope,
        ) -> crate::ProviderBindAttempt {
            self.bind_calls.fetch_add(1, Ordering::AcqRel);
            self.indexes.lock().unwrap().push(scope.inner.index);
            match self
                .binds
                .lock()
                .unwrap()
                .pop_front()
                .expect("scripted bind")
            {
                BindStep::Bound => crate::ProviderBindAttempt::Bound,
                BindStep::Busy => crate::ProviderBindAttempt::Busy(scope),
            }
        }
        provider_frame_stubs!();
    }

    struct LegacyFrame {
        cx: CallCx,
    }
    impl tobkiri_flow::GuestFlowFrame for LegacyFrame {
        fn as_any(&self) -> &dyn std::any::Any {
            self
        }
    }
    impl crate::ProviderFrame for LegacyFrame {
        fn context(&self) -> &CallCx {
            &self.cx
        }
        provider_frame_stubs!();
    }

    fn preparation_cx(control: &CoreControl, deadline: std::time::Instant) -> CallCx {
        CallCx::try_root_with_control(
            ProfileId::parse("prof_bind_preparation").unwrap(),
            Principal::User,
            tobkiri_canon::ids::RequestId::parse("req_bind_preparation").unwrap(),
            tobkiri_gate::CoreCancellation::new_with_control(control).unwrap(),
            deadline,
            ResourceLimits {
                fuel_per_call: 1,
                memory_mib: 1,
                wall_ms: 30_000,
                table_elements: 1,
                output_bytes: 1,
            },
            control.clone(),
        )
        .unwrap()
    }

    fn scripted(
        cx: &CallCx,
        ids: Vec<crate::NativeIdAttempt>,
        binds: Vec<BindStep>,
    ) -> (Arc<ScriptedFrame>, Arc<dyn crate::ProviderFrame>) {
        let stub = Arc::new(ScriptedFrame {
            cx: cx.clone(),
            ids: StdMutex::new(VecDeque::from(ids)),
            binds: StdMutex::new(VecDeque::from(binds)),
            id_calls: AtomicUsize::new(0),
            bind_calls: AtomicUsize::new(0),
            indexes: StdMutex::new(Vec::new()),
        });
        let frame: Arc<dyn crate::ProviderFrame> = stub.clone();
        (stub, frame)
    }

    fn identity(activation: u64) -> NativeExecutionId {
        NativeExecutionId {
            frame: Digest::hash(&activation.to_le_bytes()),
            request: Digest::hash(b"scripted-request"),
            activation,
        }
    }

    #[tokio::test]
    async fn busy_then_ready_consumes_each_attempt_and_retains_reservation() {
        let control = CoreControl::default();
        let cx = preparation_cx(
            &control,
            std::time::Instant::now() + std::time::Duration::from_secs(30),
        );
        let baseline = control.usage().count(tobkiri_gate::ControlKind::Frame);
        let (stub, frame) = scripted(
            &cx,
            vec![
                crate::NativeIdAttempt::Busy,
                crate::NativeIdAttempt::Busy,
                crate::NativeIdAttempt::Ready(identity(9)),
            ],
            vec![],
        );
        let mut preparation = crate::native_preparation::NativePreparation::new(&cx).unwrap();
        assert_eq!(
            control.usage().count(tobkiri_gate::ControlKind::Frame),
            baseline + 1
        );
        assert_eq!(
            preparation
                .native_execution_id(&frame)
                .await
                .unwrap()
                .activation,
            9
        );
        assert_eq!(stub.id_calls.load(Ordering::Acquire), 3);
        drop(preparation);
        assert_eq!(
            control.usage().count(tobkiri_gate::ControlKind::Frame),
            baseline
        );
    }

    #[tokio::test]
    async fn exhausted_and_uncertain_attempts_never_retry() {
        let control = CoreControl::default();
        let cx = preparation_cx(
            &control,
            std::time::Instant::now() + std::time::Duration::from_secs(30),
        );
        let (stub, frame) = scripted(&cx, vec![crate::NativeIdAttempt::Busy; 8], vec![]);
        let mut preparation = crate::native_preparation::NativePreparation::new(&cx).unwrap();
        let error = preparation.native_execution_id(&frame).await.err().unwrap();
        assert!(error.to_string().contains("busy"));
        assert_eq!(stub.id_calls.load(Ordering::Acquire), 8);
        let (stub, frame) = scripted(
            &cx,
            vec![
                crate::NativeIdAttempt::Uncertain(StepError::new("busy", "scripted")),
                crate::NativeIdAttempt::Ready(identity(2)),
            ],
            vec![],
        );
        let mut preparation = crate::native_preparation::NativePreparation::new(&cx).unwrap();
        let error = preparation.native_execution_id(&frame).await.err().unwrap();
        assert_eq!(error.downcast_ref::<StepError>().unwrap().code, "busy");
        assert_eq!(stub.id_calls.load(Ordering::Acquire), 1);
    }

    #[tokio::test]
    async fn original_context_guards_every_attempt_and_wait() {
        let control = CoreControl::default();
        let later = std::time::Instant::now() + std::time::Duration::from_secs(30);
        let elapsed = preparation_cx(&control, std::time::Instant::now());
        let (stub, frame) = scripted(
            &elapsed,
            vec![crate::NativeIdAttempt::Ready(identity(1))],
            vec![],
        );
        let mut preparation = crate::native_preparation::NativePreparation::new(&elapsed).unwrap();
        assert!(preparation.native_execution_id(&frame).await.is_err());
        assert_eq!(stub.id_calls.load(Ordering::Acquire), 0);
        let cancelled = preparation_cx(&control, later);
        cancelled.cancel().cancel();
        let (stub, frame) = scripted(
            &cancelled,
            vec![crate::NativeIdAttempt::Ready(identity(1))],
            vec![],
        );
        let mut preparation =
            crate::native_preparation::NativePreparation::new(&cancelled).unwrap();
        assert!(preparation.native_execution_id(&frame).await.is_err());
        assert_eq!(stub.id_calls.load(Ordering::Acquire), 0);
        let soon = preparation_cx(
            &control,
            std::time::Instant::now() + std::time::Duration::from_millis(10),
        );
        let (stub, frame) = scripted(&soon, vec![crate::NativeIdAttempt::Busy; 8], vec![]);
        let mut preparation = crate::native_preparation::NativePreparation::new(&soon).unwrap();
        let error = preparation.native_execution_id(&frame).await.err().unwrap();
        assert!(matches!(
            error.downcast_ref::<tobkiri_gate::GateError>(),
            Some(tobkiri_gate::GateError::Deadline)
        ));
        assert!(stub.id_calls.load(Ordering::Acquire) < 8);
    }

    #[tokio::test]
    async fn reservation_failure_precedes_every_attempt() {
        let limits = tobkiri_gate::CoreControlLimits::default();
        let control = CoreControl::new(limits).unwrap();
        let cx = preparation_cx(
            &control,
            std::time::Instant::now() + std::time::Duration::from_secs(30),
        );
        let (stub, _frame) = scripted(
            &cx,
            vec![crate::NativeIdAttempt::Ready(identity(1))],
            vec![],
        );
        let baseline = control.usage();
        let remaining = limits.total_bytes.checked_sub(baseline.bytes).unwrap();
        assert!(remaining > 0);
        let held = control
            .reserve(tobkiri_gate::ControlKind::Frame, remaining)
            .unwrap();
        assert_eq!(control.usage().bytes, limits.total_bytes);
        let error = crate::native_preparation::NativePreparation::new(&cx)
            .err()
            .expect("full original account must reject preparation before an attempt");
        assert!(matches!(
            error.downcast_ref::<tobkiri_gate::CoreControlError>(),
            Some(tobkiri_gate::CoreControlError::Limit)
        ));
        assert_eq!(stub.id_calls.load(Ordering::Acquire), 0);
        assert_eq!(stub.bind_calls.load(Ordering::Acquire), 0);
        assert_eq!(control.usage().bytes, limits.total_bytes);
        assert_eq!(
            control.usage().count(tobkiri_gate::ControlKind::Frame),
            baseline.count(tobkiri_gate::ControlKind::Frame) + 1
        );
        drop(held);
        assert_eq!(control.usage().bytes, baseline.bytes);
        let preparation = crate::native_preparation::NativePreparation::new(&cx).unwrap();
        drop(preparation);
        assert_eq!(control.usage().bytes, baseline.bytes);
    }

    #[tokio::test]
    async fn identity_and_bind_share_one_counter_and_one_candidate() {
        let control = CoreControl::default();
        let lifetime = Arc::new(StdMutex::new(
            GenerationLifetime::new(&control, None).unwrap(),
        ));
        let cx = preparation_cx(
            &control,
            std::time::Instant::now() + std::time::Duration::from_secs(30),
        );
        let (stub, frame) = scripted(
            &cx,
            vec![
                crate::NativeIdAttempt::Busy,
                crate::NativeIdAttempt::Ready(identity(3)),
            ],
            vec![BindStep::Busy, BindStep::Bound],
        );
        let mut preparation = crate::native_preparation::NativePreparation::new(&cx).unwrap();
        assert_eq!(
            preparation
                .native_execution_id(&frame)
                .await
                .unwrap()
                .activation,
            3
        );
        let scope = indexed_operation(&control, &lifetime, 3);
        let index = scope.inner.index;
        preparation
            .bind_provider_operation(&frame, scope)
            .await
            .unwrap();
        // Two identity plus two bind attempts on ONE shared counter; Busy
        // returned the unchanged candidate index for a single commit.
        assert_eq!(stub.id_calls.load(Ordering::Acquire), 2);
        assert_eq!(stub.bind_calls.load(Ordering::Acquire), 2);
        assert_eq!(*stub.indexes.lock().unwrap(), vec![index, index]);
    }

    #[test]
    fn legacy_defaults_never_classify_errors_as_busy() {
        let control = CoreControl::default();
        let lifetime = Arc::new(StdMutex::new(
            GenerationLifetime::new(&control, None).unwrap(),
        ));
        let frame = LegacyFrame {
            cx: preparation_cx(
                &control,
                std::time::Instant::now() + std::time::Duration::from_secs(30),
            ),
        };
        match frame.try_native_execution_id() {
            crate::NativeIdAttempt::Uncertain(error) => assert_eq!(error.code, "unavailable"),
            _ => panic!("legacy identity failure must not classify busy"),
        }
        match frame.try_bind_provider_operation(indexed_operation(&control, &lifetime, 5)) {
            crate::ProviderBindAttempt::Uncertain(error) => assert_eq!(error.code, "unavailable"),
            _ => panic!("legacy bind failure must not classify busy"),
        }
    }

    #[test]
    fn operation_index_reaps_only_dead_identities_with_fixed_funding() {
        let control = CoreControl::default();
        let lifetime = Arc::new(StdMutex::new(
            GenerationLifetime::new(&control, None).unwrap(),
        ));
        let operation = indexed_operation(&control, &lifetime, 1);
        assert_eq!(Arc::weak_count(&operation.inner), 1);
        let physical = operation.physical_completion();
        let physical_clone = physical.clone();
        drop(operation);
        drop(physical);
        reap_dead_provider_operations(&lifetime).unwrap();
        assert_eq!(lifetime.lock().unwrap().operations.len(), 1);
        assert_eq!(
            lifetime
                .lock()
                .unwrap()
                .operation_index
                .occupied
                .load(Ordering::Acquire),
            1,
        );
        drop(physical_clone);
        // The data's strong payload has gone, but the sole Weak still retains
        // its allocation and the SAME original operation Frame.
        assert_eq!(control.usage().count(tobkiri_gate::ControlKind::Frame), 2);
        reap_dead_provider_operations(&lifetime).unwrap();
        assert!(lifetime.lock().unwrap().operations.is_empty());
        assert_eq!(
            lifetime
                .lock()
                .unwrap()
                .operation_index
                .occupied
                .load(Ordering::Acquire),
            0,
        );
        let fixed_bytes = control.usage().bytes;
        assert_eq!(
            control
                .usage()
                .count(tobkiri_gate::ControlKind::Registration),
            1
        );
        for activation in 2..259 {
            let operation = indexed_operation(&control, &lifetime, activation);
            drop(operation);
            reap_dead_provider_operations(&lifetime).unwrap();
            assert_eq!(control.usage().bytes, fixed_bytes);
            assert!(lifetime.lock().unwrap().operations.is_empty());
        }
        drop(lifetime);
        assert_eq!(control.usage().bytes, 0);
    }

    #[test]
    fn operation_physical_lease_keeps_frame_funding_after_generation_drop() {
        let control = CoreControl::default();
        let lifetime = Arc::new(StdMutex::new(
            GenerationLifetime::new(&control, None).unwrap(),
        ));
        let operation = indexed_operation(&control, &lifetime, 1);
        let physical = operation.physical_completion();
        let physical_clone = physical.clone();
        drop(lifetime);
        assert_eq!(Arc::weak_count(&operation.inner), 0);
        drop(operation);
        drop(physical);
        // The fixed index is gone. This observer keeps the original operation
        // Frame externally through the identity's final actual deallocation.
        assert_eq!(
            control
                .usage()
                .count(tobkiri_gate::ControlKind::Registration),
            1
        );
        assert_eq!(control.usage().count(tobkiri_gate::ControlKind::Frame), 2);
        assert!(control.usage().bytes >= PROVIDER_OPERATION_CONTROL);
        drop(physical_clone);
        assert_eq!(control.usage().bytes, 0);
    }

    #[test]
    fn operation_records_keep_original_frame_after_identity_reap() {
        let control = CoreControl::default();
        let lifetime = Arc::new(StdMutex::new(
            GenerationLifetime::new(&control, None).unwrap(),
        ));
        let parent = indexed_operation(&control, &lifetime, 1);
        let child = indexed_operation_with_parent(&control, &lifetime, 2, Some(parent.clone()));
        assert_eq!(control.usage().count(tobkiri_gate::ControlKind::Frame), 3);
        drop(parent);
        reap_dead_provider_operations(&lifetime).unwrap();
        assert_eq!(lifetime.lock().unwrap().operations.len(), 1);
        // The immutable parent record/native-Vec backing remains in the child's
        // registry. Reaping only its identity must not release its Frame.
        assert_eq!(control.usage().count(tobkiri_gate::ControlKind::Frame), 3);
        drop(child);
        reap_dead_provider_operations(&lifetime).unwrap();
        assert_eq!(control.usage().count(tobkiri_gate::ControlKind::Frame), 1);
        drop(lifetime);
        assert_eq!(control.usage().bytes, 0);
    }

    #[test]
    fn operation_frame_covers_maximum_identifier_metadata() {
        let generation = NativeGenerationId {
            profile: ProfileId::parse(&"p".repeat(128)).unwrap(),
            pack: PackId::parse(&format!("{}.{}", "p".repeat(128), "n".repeat(128))).unwrap(),
            hash: PackHash::new(Digest::hash(b"component")),
            revision: Digest::hash(b"revision"),
            security_epoch: 1,
            generation: 1,
        };
        assert!(
            ProviderOperationScope::storage_bytes(&generation).unwrap()
                <= PROVIDER_OPERATION_CONTROL
        );
    }

    // These are native lifetime tests, not Core frame/authority fixtures.
    fn spawner(control: &CoreControl) -> (NativeWorkSpawner, Arc<StdMutex<GenerationLifetime>>) {
        let census = GenerationChildCensus::new(
            control.clone(),
            tokio::runtime::Handle::current(),
            NativeGenerationId {
                profile: ProfileId::parse("mailbox-test").unwrap(),
                pack: PackId::parse("local.mailbox").unwrap(),
                hash: PackHash::new(Digest::hash(b"component")),
                revision: Digest::hash(b"revision"),
                security_epoch: 1,
                generation: 1,
            },
            8,
        )
        .unwrap();
        let lifetime = Arc::new(StdMutex::new(
            GenerationLifetime::new(control, Some(census.clone())).unwrap(),
        ));
        let disposal = lifetime.lock().unwrap().disposal.clone();
        let operation = ProviderOperationScope::new(
            control,
            Weak::new(),
            census.generation(),
            NativeExecutionId {
                frame: Digest::hash(b"frame"),
                request: Digest::hash(b"request"),
                activation: 1,
            },
            disposal,
            None,
        )
        .unwrap();
        let spawner = NativeWorkSpawner::admit(
            control,
            operation.clone(),
            census,
            Arc::downgrade(&lifetime),
            lifetime.lock().unwrap().native_children.clone(),
            NativeExecutionId {
                frame: Digest::hash(b"frame"),
                request: Digest::hash(b"request"),
                activation: 1,
            },
            tobkiri_gate::CancellationToken::new_with_control(control).unwrap(),
            NativeTaskRole::Source,
            None,
            None,
        )
        .unwrap();
        operation.inner.registry.state.lock().unwrap()[operation.inner.index]
            .native
            .push(spawner.inner.completion.clone());
        (spawner, lifetime)
    }

    fn seal(lifetime: &Arc<StdMutex<GenerationLifetime>>) -> GenerationRetirement {
        let mut lifetime = lifetime.lock().unwrap();
        lifetime.retiring = true;
        lifetime
            .disposal
            .census
            .as_ref()
            .unwrap()
            .seal(StreamCloseReason::Retired)
            .unwrap()
    }

    struct Close;
    impl StreamCloseControl for Close {
        fn record_close(&self, _: StreamCloseReason) -> Result<(), StepError> {
            Ok(())
        }
    }
    fn execution(activation: u64) -> tobkiri_flow::RunTaskExecution {
        tobkiri_flow::RunTaskExecution {
            frame: Digest::hash(&activation.to_le_bytes()),
            request: Digest::hash(b"actual-native-test-request"),
            activation,
        }
    }

    fn child_operation(parent: &NativeWorkSpawner, activation: u64) -> NativeWorkSpawner {
        let execution = NativeWorkSpawner::execution_key(execution(activation));
        let operation = ProviderOperationScope::new(
            &parent.inner.control,
            Weak::new(),
            parent.inner.generation.generation(),
            execution.clone(),
            parent.inner.operation.inner.disposal.clone(),
            Some(parent.inner.operation.clone()),
        )
        .unwrap();
        let child = NativeWorkSpawner::admit(
            &parent.inner.control,
            operation.clone(),
            parent.inner.generation.clone(),
            parent.inner.lifetime.clone(),
            parent.inner.legacy_children.clone(),
            execution,
            parent
                .inner
                .original_cancel
                .child_with_control(&parent.inner.control)
                .unwrap(),
            NativeTaskRole::Source,
            None,
            None,
        )
        .unwrap();
        operation.inner.registry.state.lock().unwrap()[operation.inner.index]
            .native
            .push(child.inner.completion.clone());
        child
    }

    #[tokio::test]
    async fn source_native_scope_excludes_pending_caller_and_sibling_work() {
        let control = CoreControl::default();
        let (caller, lifetime) = spawner(&control);
        let source = child_operation(&caller, 2);
        let sibling = child_operation(&caller, 3);
        let (release_caller, caller_blocked) = tokio::sync::oneshot::channel();
        let (release_sibling, sibling_blocked) = tokio::sync::oneshot::channel();
        caller
            .clone()
            .spawn(async move {
                let _ = caller_blocked.await;
                Ok(())
            })
            .unwrap();
        sibling
            .clone()
            .spawn(async move {
                let _ = sibling_blocked.await;
                Ok(())
            })
            .unwrap();
        source.clone().spawn(async { Ok(()) }).unwrap();
        source.inner.operation.seal().unwrap();
        // This asserts ONLY native-work subtree completion, never VM disposal
        // or authority. The source completes while its actual calling task and
        // sibling task still retain their original blocked Join receipts.
        ProviderOperationScope::wait_native(&source.inner.operation.inner)
            .await
            .unwrap();
        assert!(
            tokio::time::timeout(
                std::time::Duration::from_millis(1),
                caller.inner.completion.own.wait_joined()
            )
            .await
            .is_err()
        );
        assert!(
            tokio::time::timeout(
                std::time::Duration::from_millis(1),
                sibling.inner.completion.own.wait_joined()
            )
            .await
            .is_err()
        );
        release_caller.send(()).unwrap();
        release_sibling.send(()).unwrap();
        let retired = seal(&lifetime);
        assert_eq!(retired.wait_physical().await.unwrap().joined(), 3);
        drop(source);
        drop(sibling);
        drop(caller);
        drop(retired);
        drop(lifetime);
        assert_eq!(control.usage().bytes, 0);
    }

    #[tokio::test]
    async fn actual_store_drop_and_source_children_do_not_wait_for_native_caller() {
        let control = CoreControl::default();
        let (caller, lifetime) = spawner(&control);
        let source = child_operation(&caller, 2);
        let vm_control = control
            .reserve(tobkiri_gate::ControlKind::Frame, 32 * 1024)
            .unwrap();
        let engine = wasmtime::Engine::default();
        let store = wasmtime::Store::new(&engine, ());
        let (release, blocked) = tokio::sync::oneshot::channel();
        caller
            .clone()
            .spawn(async move {
                let _ = blocked.await;
                Ok(())
            })
            .unwrap();
        source.clone().spawn(async { Ok(()) }).unwrap();
        source.inner.operation.seal().unwrap();
        let physical = source.inner.operation.physical_completion();
        // A real Store still exists: even completed source-native work cannot
        // produce the composite source receipt. No Core authority is mocked.
        assert!(
            tokio::time::timeout(std::time::Duration::from_millis(1), physical.wait())
                .await
                .is_err()
        );
        drop(store);
        source.inner.operation.inner.disposal.complete();
        physical.wait().await.unwrap();
        assert!(
            tokio::time::timeout(
                std::time::Duration::from_millis(1),
                caller.inner.completion.own.wait_joined()
            )
            .await
            .is_err()
        );
        let retired = seal(&lifetime);
        *source
            .inner
            .operation
            .inner
            .disposal
            .retirement
            .lock()
            .unwrap() = Some(retired.clone());
        // Full retirement still awaits that same actual calling task, after
        // the independent VM+source receipt has already completed.
        assert!(
            tokio::time::timeout(
                std::time::Duration::from_millis(1),
                source.inner.operation.inner.disposal.wait()
            )
            .await
            .is_err()
        );
        release.send(()).unwrap();
        source.inner.operation.inner.disposal.wait().await.unwrap();
        drop(physical);
        drop(source);
        drop(caller);
        drop(retired);
        drop(lifetime);
        drop(vm_control);
        assert_eq!(control.usage().bytes, 0);
    }

    #[tokio::test]
    async fn source_native_scope_covers_transitive_work_and_rejects_late_publication() {
        let control = CoreControl::default();
        let (caller, lifetime) = spawner(&control);
        let source = child_operation(&caller, 2);
        let descendant = child_operation(&source, 3);
        let (release, blocked) = tokio::sync::oneshot::channel();
        caller.clone().spawn(async { Ok(()) }).unwrap();
        source.clone().spawn(async { Ok(()) }).unwrap();
        descendant
            .clone()
            .spawn(async move {
                let _ = blocked.await;
                Ok(())
            })
            .unwrap();
        source.inner.operation.seal().unwrap();
        assert!(
            tokio::time::timeout(
                std::time::Duration::from_millis(1),
                ProviderOperationScope::wait_native(&source.inner.operation.inner)
            )
            .await
            .is_err()
        );
        assert!(
            ProviderOperationScope::new(
                &control,
                Weak::new(),
                caller.inner.generation.generation(),
                NativeWorkSpawner::execution_key(execution(4)),
                caller.inner.operation.inner.disposal.clone(),
                Some(descendant.inner.operation.clone())
            )
            .is_err()
        );
        assert!(source.clone().spawn(async { Ok(()) }).is_err());
        release.send(()).unwrap();
        ProviderOperationScope::wait_native(&source.inner.operation.inner)
            .await
            .unwrap();
        let retired = seal(&lifetime);
        assert_eq!(retired.wait_physical().await.unwrap().joined(), 3);
        drop(source);
        drop(descendant);
        drop(caller);
        drop(retired);
        drop(lifetime);
        assert_eq!(control.usage().bytes, 0);
    }

    #[tokio::test]
    async fn caller_unknown_is_excluded_from_source_but_legacy_marker_is_retained() {
        let control = CoreControl::default();
        let (caller, lifetime) = spawner(&control);
        let source = child_operation(&caller, 2);
        RunTaskSpawner::before_unregistered_child(&caller).unwrap();
        caller.clone().spawn(async { Ok(()) }).unwrap();
        source.clone().spawn(async { Ok(()) }).unwrap();
        source.inner.operation.seal().unwrap();
        ProviderOperationScope::wait_native(&source.inner.operation.inner)
            .await
            .unwrap();
        caller.inner.operation.seal().unwrap();
        assert!(
            ProviderOperationScope::wait_native(&caller.inner.operation.inner)
                .await
                .is_err()
        );
        assert!(
            lifetime
                .lock()
                .unwrap()
                .native_children
                .load(Ordering::Acquire)
        );
        // The full generation is still a distinct proof; this native subtree
        // result never clears its original conservative missing-work marker.
        let retired = seal(&lifetime);
        retired.wait_physical().await.unwrap();
        drop(source);
        drop(caller);
        drop(retired);
        drop(lifetime);
        assert_eq!(control.usage().bytes, 0);
    }

    #[tokio::test]
    async fn sealed_operation_rejects_unaccepted_payload_before_it_can_run() {
        let control = CoreControl::default();
        let (caller, lifetime) = spawner(&control);
        let pending = child_operation(&caller, 2);
        caller.clone().spawn(async { Ok(()) }).unwrap();
        pending.inner.operation.seal().unwrap();
        let polled = Arc::new(AtomicBool::new(false));
        let actual = polled.clone();
        assert!(
            pending
                .clone()
                .spawn(async move {
                    actual.store(true, Ordering::Release);
                    Ok(())
                })
                .is_err()
        );
        assert!(!polled.load(Ordering::Acquire));
        RunTaskSpawner::abandon_unstarted(&pending).unwrap();
        assert!(
            ProviderOperationScope::wait_native(&pending.inner.operation.inner)
                .await
                .is_err()
        );
        let retired = seal(&lifetime);
        assert!(retired.wait_physical().await.is_err());
        drop(polled);
        drop(pending);
        drop(caller);
        drop(retired);
        drop(lifetime);
        assert_eq!(control.usage().bytes, 0);
    }

    #[tokio::test]
    async fn uncertain_close_companion_still_notifies_original_native_cancellation() {
        struct UncertainClose;
        impl StreamCloseControl for UncertainClose {
            fn record_close(&self, _: StreamCloseReason) -> Result<(), StepError> {
                Err(StepError::new(
                    "audit",
                    "original close selection is uncertain",
                ))
            }
        }
        let control = CoreControl::default();
        let (spawner, lifetime) = spawner(&control);
        let cancel = spawner
            .inner
            .original_cancel
            .child_with_control(&control)
            .unwrap();
        spawner.bind_cancel(cancel.clone()).unwrap();
        let companion: Arc<dyn StreamCloseControl> = Arc::new(UncertainClose);
        spawner.inner.close.state.lock().unwrap().stream = Some(Arc::downgrade(&companion));
        let error = spawner
            .inner
            .close
            .record_close(StreamCloseReason::Retired)
            .unwrap_err();
        assert_eq!(error.code, "audit");
        assert!(cancel.is_cancelled());
        let retired = seal(&lifetime);
        assert!(retired.wait_physical().await.is_err());
        drop(companion);
        drop(cancel);
        drop(spawner);
        drop(retired);
        drop(lifetime);
        assert_eq!(control.usage().bytes, 0);
    }

    #[tokio::test]
    async fn parent_receipt_covers_both_immutable_child_siblings_without_seal_or_self_wait() {
        let control = CoreControl::default();
        let (parent, lifetime) = spawner(&control);
        let cancel = parent
            .inner
            .original_cancel
            .child_with_control(&control)
            .unwrap();
        let child = RunTaskSpawner::child(&parent, execution(2), cancel.clone()).unwrap();
        let cleanup = child.cleanup_child(execution(2), cancel).unwrap();
        let body_receipt = child.completion().unwrap();
        let cleanup_receipt = cleanup.completion().unwrap();
        let (release, blocked) = tokio::sync::oneshot::channel();
        let body_for_cleanup = body_receipt.clone();
        cleanup
            .spawn(
                tobkiri_flow::RunTask::cleanup(
                    async move {
                        body_for_cleanup
                            .wait()
                            .await
                            .map_err(|_| tobkiri_flow::RunTaskPhysicalError::CompletionUnknown)?;
                        let _ = blocked.await;
                        Ok(())
                    },
                    &control,
                )
                .unwrap(),
                Arc::new(Close),
            )
            .unwrap();
        cleanup_receipt.wait_started().await.unwrap();
        child
            .spawn(
                tobkiri_flow::RunTask::cleanup(async { Ok(()) }, &control).unwrap(),
                Arc::new(Close),
            )
            .unwrap();
        parent.clone().spawn(async { Ok(()) }).unwrap();
        // The child's body never includes the cleanup sibling which awaits it.
        body_receipt.wait().await.unwrap();
        let full = RunTaskSpawner::completion(&parent).unwrap();
        assert!(
            tokio::time::timeout(std::time::Duration::from_millis(1), full.wait())
                .await
                .is_err()
        );
        release.send(()).unwrap();
        full.wait().await.unwrap();
        let retired = seal(&lifetime);
        assert_eq!(retired.wait_physical().await.unwrap().joined(), 3);
        drop(full);
        drop(body_receipt);
        drop(cleanup_receipt);
        drop(cleanup);
        drop(child);
        drop(parent);
        drop(retired);
        drop(lifetime);
        assert_eq!(control.usage().bytes, 0);
    }

    #[tokio::test]
    async fn child_body_rejects_start_without_its_exact_claimed_and_started_cleanup() {
        let control = CoreControl::default();
        let (parent, lifetime) = spawner(&control);
        let cancel = parent
            .inner
            .original_cancel
            .child_with_control(&control)
            .unwrap();
        let child = RunTaskSpawner::child(&parent, execution(2), cancel.clone()).unwrap();
        assert!(child.cleanup_child(execution(3), cancel).is_err());
        let polled = Arc::new(AtomicBool::new(false));
        let actual_polled = polled.clone();
        assert!(
            child
                .spawn(
                    tobkiri_flow::RunTask::cleanup(
                        async move {
                            actual_polled.store(true, Ordering::Release);
                            Ok(())
                        },
                        &control
                    )
                    .unwrap(),
                    Arc::new(Close)
                )
                .is_err()
        );
        assert!(!polled.load(Ordering::Acquire));
        let retired = seal(&lifetime);
        retired.notify_close().unwrap();
        assert!(retired.wait_physical().await.is_err());
        drop(polled);
        drop(child);
        drop(parent);
        drop(retired);
        drop(lifetime);
        assert_eq!(control.usage().bytes, 0);
    }

    #[tokio::test]
    async fn submitted_mailbox_retains_actual_body_and_join_after_close_and_abandoned_wait() {
        let control = CoreControl::default();
        let (spawner, lifetime) = spawner(&control);
        let cancel = spawner
            .inner
            .original_cancel
            .child_with_control(&control)
            .unwrap();
        spawner.bind_cancel(cancel.clone()).unwrap();
        let (started, observe_start) = tokio::sync::oneshot::channel();
        let (release, blocked) = tokio::sync::oneshot::channel();
        spawner
            .clone()
            .spawn(async move {
                let _ = started.send(());
                let _ = blocked.await;
                Ok(())
            })
            .unwrap();
        observe_start.await.unwrap();
        // A late prestart-abandon notification cannot cancel or detach this
        // already accepted original body. Its same Join still needs release.
        RunTaskSpawner::abandon_unstarted(&spawner).unwrap();
        let retirement = seal(&lifetime);
        retirement.notify_close().unwrap();
        assert!(cancel.is_cancelled());
        assert!(
            tokio::time::timeout(
                std::time::Duration::from_millis(1),
                spawner.inner.completion.own.wait_joined()
            )
            .await
            .is_err()
        );
        assert!(spawner.clone().spawn(async { Ok(()) }).is_err());
        release.send(()).unwrap();
        spawner.inner.completion.own.wait_joined().await.unwrap();
        assert_eq!(retirement.wait_physical().await.unwrap().joined(), 1);
        drop(spawner);
        drop(cancel);
        drop(retirement);
        drop(lifetime);
        assert_eq!(control.usage().bytes, 0);
    }

    #[tokio::test]
    async fn abandoned_unaccepted_mailbox_finishes_unknown_without_generation_seal() {
        let control = CoreControl::default();
        let (spawner, lifetime) = spawner(&control);
        RunTaskSpawner::abandon_unstarted(&spawner).unwrap();
        RunTaskSpawner::abandon_unstarted(&spawner).unwrap();
        assert!(matches!(
            tokio::time::timeout(
                std::time::Duration::from_secs(1),
                spawner.inner.completion.own.wait_joined()
            )
            .await
            .unwrap(),
            Err(CensusError::TaskUnknown)
        ));
        assert!(!lifetime.lock().unwrap().retiring);
        assert!(spawner.clone().spawn(async { Ok(()) }).is_err());
        assert!(control.usage().bytes > 0);
        let retired = seal(&lifetime);
        assert!(retired.wait_physical().await.is_err());
        drop(spawner);
        drop(retired);
        drop(lifetime);
        assert_eq!(control.usage().bytes, 0);
    }

    #[tokio::test]
    async fn sealed_unsubmitted_mailbox_wakes_but_keeps_physical_completion_unknown() {
        let control = CoreControl::default();
        let (spawner, lifetime) = spawner(&control);
        let retirement = seal(&lifetime);
        retirement.notify_close().unwrap();
        assert!(matches!(
            tokio::time::timeout(
                std::time::Duration::from_secs(1),
                spawner.inner.completion.own.wait_joined()
            )
            .await
            .unwrap(),
            Err(CensusError::TaskUnknown)
        ));
        assert!(spawner.clone().spawn(async { Ok(()) }).is_err());
        assert!(matches!(
            retirement.wait_physical().await,
            Err(CensusError::TaskUnknown)
        ));
        drop(spawner);
        drop(retirement);
        drop(lifetime);
        assert_eq!(control.usage().bytes, 0);
    }
}

#[cfg(test)]
mod binding_catalog_tests {
    use super::*;
    use tobkiri_canon::ids::{ShapeName, ShapeVersion};

    fn view(index: usize) -> ShapeRef {
        ShapeRef::new(
            ShapeName::parse(&format!("local.catalog.view{index}")).unwrap(),
            ShapeVersion::new(1, 0),
        )
    }

    #[test]
    fn provider_edges_are_counted_without_reordering_valid_routes() {
        let providers = ["local.third", "local.first", "local.second"]
            .map(|id| PackId::parse(id).unwrap())
            .to_vec();
        let bindings = BTreeMap::from([(view(0), providers.clone())]);
        assert_eq!(checked_provider_edges(&bindings).unwrap(), 3);
        assert_eq!(bindings[&view(0)], providers);
        let duplicate = BTreeMap::from([(view(0), vec![providers[0].clone(); 2])]);
        assert!(
            checked_provider_edges(&duplicate)
                .unwrap_err()
                .to_string()
                .contains("duplicate provider")
        );
    }

    #[test]
    fn provider_vectors_and_total_edges_are_bounded_independently_of_key_count() {
        let providers: Vec<_> = (0..65)
            .map(|index| PackId::parse(&format!("local.provider{index}")).unwrap())
            .collect();
        assert!(checked_provider_edges(&BTreeMap::from([(view(0), providers.clone())])).is_err());
        let mut bindings: BTreeMap<_, _> = (0..256)
            .map(|index| (view(index), providers[..64].to_vec()))
            .collect();
        assert_eq!(checked_provider_edges(&bindings).unwrap(), 16_384);
        bindings.insert(view(256), vec![providers[0].clone()]);
        assert!(
            checked_provider_edges(&bindings)
                .unwrap_err()
                .to_string()
                .contains("edge catalog")
        );
    }
}
