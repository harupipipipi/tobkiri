use anyhow::{Context, Result, bail};
use async_trait::async_trait;
use std::{
    collections::{BTreeMap, BTreeSet},
    sync::{
        Arc, Mutex as StdMutex, OnceLock, Weak,
        atomic::{AtomicU64, Ordering},
    },
};
use tobkiri_canon::{
    CanonValue,
    ids::{OpName, PackHash, PackId, ProfileId, ShapeRef},
};
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
    pub(crate) audit: Arc<AuditLog>,
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
    pub(crate) control: tobkiri_gate::CoreControl,
    pub(crate) filesystem: OnceLock<crate::filesystem::FsRegistry>,
    _catalog_control: tobkiri_gate::ControlReservation,
}

pub(crate) struct PackState {
    pub(crate) shared: Weak<Shared>,
    pub(crate) wasi: RestrictedWasi,
    pub(crate) owners: crate::owned_values::ValueOwners,
    pub(crate) frame: Option<Arc<dyn crate::ProviderFrame>>,
}
impl WasiView for PackState {
    fn ctx(&mut self) -> WasiCtxView<'_> {
        self.wasi.ctx()
    }
}
pub(crate) struct PackInstance {
    pack: Arc<VerifiedPack>,
    component: Component,
    config: Vec<u8>,
    live: Option<(
        Store<InvocationState<PackState>>,
        Option<Instance>,
        Arc<crate::generation_retirement::GenerationRetirement>,
    )>,
    // Field order is deliberate: Store is destroyed before this external
    // physical lease, including on ordinary PackInstance destruction.
    physical: Option<Arc<crate::instance_capacity::InstanceCapacity>>,
    retirement: Arc<StdMutex<Option<Arc<crate::generation_retirement::GenerationRetirement>>>>,
    generation: Arc<AtomicU64>,
    current_generation: u64,
}
pub(crate) struct InstanceSlot {
    instance: Mutex<PackInstance>,
    retirement: Arc<StdMutex<Option<Arc<crate::generation_retirement::GenerationRetirement>>>>,
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
    /// Install Core's trusted descriptor registry. Guest paths cannot create a
    /// registry or picker token. Protect the actual pinned authority directory
    /// before this registry can service any component filesystem operation.
    #[cfg(unix)]
    pub fn install_filesystem_registry(
        &self,
        registry: crate::filesystem::FsRegistry,
    ) -> Result<()> {
        if self.shared.filesystem.get().is_some() {
            bail!("filesystem registry is already installed")
        }
        registry.protect_root(self.shared.root.directory_capability().try_clone()?)?;
        self.shared
            .filesystem
            .set(registry)
            .map_err(|_| anyhow::anyhow!("filesystem registry is already installed"))
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
    /// Prepay only the synchronous fixed-KV conversion. Returned graphs retain
    /// their sealed physical owner; execution/output use their existing budgets.
    pub(crate) fn with_prepaid_frame_conversion(
        &self,
        cx: &CallCx,
        scope: tobkiri_gate::AllocationFrameScope,
        source: &Arc<dyn tobkiri_canon::AllocationBudget>,
        bound: tobkiri_canon::AllocationRequest,
        convert: impl FnOnce(&crate::AllocationOwners) -> Result<tobkiri_store::FixedDataRequest>,
    ) -> Result<tobkiri_store::FixedDataRequest> {
        crate::allocation::PackAllocationBudget::with_prepaid_frame_conversion(
            &self.shared,
            cx,
            scope,
            source,
            bound,
            convert,
        )
    }
    /// Prepay only a synchronous network argument digest. The closure returns
    /// no graph, handle or effect; the sealed carrier retains actual custody
    /// until every temporary payload and owner control block has disappeared.
    pub(crate) fn with_prepaid_network_digest(
        &self,
        cx: &CallCx,
        scope: tobkiri_gate::AllocationFrameScope,
        source: &Arc<dyn tobkiri_canon::AllocationBudget>,
        bound: tobkiri_canon::AllocationRequest,
        convert: impl FnOnce(&crate::AllocationOwners) -> Result<tobkiri_canon::Digest>,
    ) -> Result<tobkiri_canon::Digest> {
        crate::allocation::PackAllocationBudget::with_prepaid_frame_conversion(
            &self.shared,
            cx,
            scope,
            source,
            bound,
            convert,
        )
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
            audit,
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
            control: init_cx.control().clone(),
            filesystem: OnceLock::new(),
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
                if let Some((kind, version)) = source {
                    if sources.insert(kind, version).is_some() {
                        bail!("multiple exact source packages need an explicit source binding")
                    }
                }
            }
            let retirement = Arc::new(StdMutex::new(None));
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
    pub(crate) async fn register_core_stream(
        &self,
        cx: &CallCx,
        shape: ShapeRef,
        stream: CoreStream,
    ) -> Result<u64> {
        cx.check_active()?;
        let id = self
            .shared
            .handles
            .lock()
            .map_err(|_| anyhow::anyhow!("handle table poisoned"))?
            .register_core(cx.clone(), shape, stream)?;
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
                _=owner.cancel().cancelled()=>crate::core_imports::ExpiryReason::Revoked,
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

    pub(crate) fn vault(&self) -> Result<Arc<Vault>> {
        let mut vault = self
            .shared
            .vault
            .lock()
            .map_err(|_| anyhow::anyhow!("vault lock poisoned"))?;
        if vault.is_none() {
            *vault = Some(Arc::new(Vault::open_default(
                &self.shared.root,
                &self.shared.profile,
            )?));
        }
        Ok(vault.as_ref().expect("initialized vault").clone())
    }

    fn provider_context(&self, cx: &CallCx, pack: &VerifiedPack) -> Result<CallCx> {
        let limits = self
            .shared
            .effective_limits
            .get(&(pack.manifest.id.clone(), pack.hash.clone()))
            .context("effective Pack revision limits missing")?;
        Ok(cx.callee(
            Principal::Pack {
                pack: pack.manifest.id.clone(),
                hash: pack.hash.clone(),
            },
            limits.clone(),
        ))
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
    pub(crate) async fn register_guest_handle(
        &self,
        kind: HandleKind,
        owner: &CallCx,
        id: u64,
        recipient: &CallCx,
        authority: &ShapeRef,
        expected_generation: Option<&Arc<crate::generation_retirement::GenerationRetirement>>,
        provider: &Arc<dyn crate::ProviderFrame>,
    ) -> Result<u64> {
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
        let generation = match expected_generation {
            Some(generation) => generation.clone(),
            None => source_slot
                .retirement
                .lock()
                .map_err(|_| anyhow::anyhow!("retirement state poisoned"))?
                .clone()
                .context("guest source generation missing")?,
        };
        self.check_generation_identity(pack, hash, &generation)?;
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
        if let Err(error) = self.check_generation_identity(pack, hash, &generation) {
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
                    generation: generation.number(),
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
                source_frame,
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
            self.reap_handle(&owner, recipient, *id, kind);
        }
        result
    }
    fn resolve(
        &self,
        cx: &CallCx,
        shape: &ShapeRef,
        op: &OpName,
    ) -> Result<(Arc<VerifiedPack>, ShapeDef, WitFunction)> {
        self.resolve_for_admission(cx, shape, op, true)
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
        if entry.generation.is_some() {
            let (pack, hash) = entry
                .owner
                .principal()
                .pack()
                .context("guest source owner missing")?;
            let generation = self.captured_generation(entry)?;
            self.check_generation_identity(pack, hash, generation)?;
        } else if entry.generation_retirement.is_some() {
            bail!("guest source generation binding missing")
        }
        Ok(())
    }
    fn captured_generation<'a>(
        &self,
        entry: &'a crate::HandleEntry,
    ) -> Result<&'a Arc<crate::generation_retirement::GenerationRetirement>> {
        let (pack, hash) = entry
            .owner
            .principal()
            .pack()
            .context("guest source owner missing")?;
        let number = entry
            .generation
            .context("guest source generation missing")?;
        let generation = entry
            .generation_retirement
            .as_ref()
            .context("guest source retirement receipt missing")?;
        if !generation.matches(pack, hash, number) {
            bail!("guest source retirement binding mismatch")
        }
        Ok(generation)
    }
    fn check_generation_identity(
        &self,
        id: &PackId,
        hash: &PackHash,
        generation: &Arc<crate::generation_retirement::GenerationRetirement>,
    ) -> Result<()> {
        let slot = self.instance(id)?;
        let current = slot
            .retirement
            .lock()
            .map_err(|_| anyhow::anyhow!("retirement state poisoned"))?
            .clone();
        if !generation.matches(id, hash, generation.number())
            || !current
                .as_ref()
                .is_some_and(|current| Arc::ptr_eq(current, generation))
        {
            return Err(
                tobkiri_gate::GateError::Denied("handle source instance retired".into()).into(),
            );
        }
        self.check_generation(id, generation.number())
    }
    fn check_generation(&self, id: &PackId, generation: u64) -> Result<()> {
        let slot = self.instance(id)?;
        if generation == 0
            || slot.generation.load(Ordering::Acquire) != generation
            || slot
                .retirement
                .lock()
                .map_err(|_| anyhow::anyhow!("retirement state poisoned"))?
                .as_ref()
                .is_none_or(|current| current.number() != generation || !current.is_live())
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
    ) -> Result<(
        wasmtime::component::BudgetedVec<Val>,
        Arc<crate::generation_retirement::GenerationRetirement>,
    )> {
        self.export_call_owned_inner(
            id,
            cx,
            interface,
            op,
            args,
            expected_generation,
            owners,
            frame,
            physical_scope,
        )
        .await
        .map_err(|error| crate::filesystem::preserve_effect_error(cx, error))
    }
    #[allow(clippy::too_many_arguments)]
    async fn export_call_owned_inner(
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
    ) -> Result<(
        wasmtime::component::BudgetedVec<Val>,
        Arc<crate::generation_retirement::GenerationRetirement>,
    )> {
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
        if expected_generation.is_none() {
            // A replacement must not race the previous Store's destruction or
            // its retained retirement task. This does not wait under the lock.
            instance.prepare_generation()?;
        }
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
            if instance
                .live
                .as_ref()
                .is_none_or(|(_, instance, _)| instance.is_none())
                || instance.current_generation != generation
                || self.check_generation(id, generation).is_err()
            {
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
                instance.drop_store();
                return Err(error);
            }
        }
        if let Some((store, _, _)) = instance.live.as_mut() {
            store.data_mut().data.frame = Some(frame);
        }
        let result = instance.call_owned(&cx, interface, op, args, owners).await;
        let result = result.map_err(|error| {
            // The failed call's Store is still owned here. This observation
            // neither refuels it nor calls a guest; failure to observe is None.
            let fuel_remaining = instance
                .live
                .as_ref()
                .and_then(|(store, _, _)| crate::entry_diagnostics::remaining_fuel(&error, store));
            crate::entry_diagnostics::annotate(error, "export", &cx, owners, fuel_remaining)
        });
        if let Some((store, _, _)) = instance.live.as_mut() {
            store.data_mut().data.frame.take();
        }
        if let Some(physical) = &instance.physical {
            physical.leave();
        }
        if result.is_err() {
            instance.drop_store();
        }
        let values = result?;
        let generation = instance.current_retirement()?;
        if let Err(error) = self.check_generation_identity(id, &pack.hash, &generation) {
            instance.drop_store();
            return Err(error);
        }
        Ok((values, generation))
    }
    pub(crate) async fn export_call_scoped(
        &self,
        id: &PackId,
        cx: &CallCx,
        interface: &str,
        op: &str,
        args: &[Val],
        expected_generation: Option<u64>,
    ) -> Result<(Vec<Val>, u64)> {
        self.export_call_scoped_inner(id, cx, interface, op, args, expected_generation)
            .await
            .map_err(|error| crate::filesystem::preserve_effect_error(cx, error))
    }
    async fn export_call_scoped_inner(
        &self,
        id: &PackId,
        cx: &CallCx,
        interface: &str,
        op: &str,
        args: &[Val],
        expected_generation: Option<u64>,
    ) -> Result<(Vec<Val>, u64)> {
        let pack = self
            .shared
            .verified
            .get(id)
            .context("unknown execution Pack")?;
        let reentrant = cx
            .executing_pairs()
            .iter()
            .any(|(ancestor, hash)| ancestor == id && hash == &pack.hash);
        let cx = cx
            .clone()
            .try_with_running_pack_precharged(id, &pack.hash)?;
        let instance = self.instance(id)?;
        if let Some(generation) = expected_generation {
            self.check_generation(id, generation)?;
        }
        let mut instance = if reentrant {
            instance.instance.try_lock().map_err(|_| {
                StepError::new("conflict", "nested component has no free instance capacity")
            })?
        } else {
            tokio::select! {
                _=cx.cancel().cancelled()=>bail!("component call cancelled while waiting"),
                _=tokio::time::sleep_until(cx.deadline().into())=>bail!("original component deadline elapsed while waiting"),
                lock=instance.instance.lock()=>lock,
            }
        };
        if let Some(generation) = expected_generation {
            if instance
                .live
                .as_ref()
                .is_none_or(|(_, instance, _)| instance.is_none())
                || instance.current_generation != generation
                || self.check_generation(id, generation).is_err()
            {
                return Err(tobkiri_gate::GateError::Denied(
                    "handle source instance retired".into(),
                )
                .into());
            }
        } else {
            if instance.live.is_none() {
                return Err(StepError::new(
                    "unavailable",
                    "raw export requires an established producing frame",
                )
                .into());
            }
        }
        let result = instance.call(&cx, interface, op, args).await;
        if result.is_err() {
            instance.drop_store();
        }
        let generation = instance.current_generation;
        if result.is_ok()
            && let Err(error) = self.check_generation(id, generation)
        {
            instance.drop_store();
            return Err(error);
        }
        result.map(|values| (values, generation))
    }

    pub(crate) async fn discard_handle_instance(&self, entry: &crate::HandleEntry) -> Result<()> {
        // The captured actual generation is the only cleanup target. A newer
        // Store under this Pack ID is never cancelled, dropped, or evidence.
        let generation = self.captured_generation(entry)?.clone();
        let slot = self.instance(
            entry
                .owner
                .principal()
                .pack()
                .context("guest source owner missing")?
                .0,
        )?;
        let expected = generation.clone();
        generation
            .retire(move || async move {
                let mut instance = slot.instance.lock().await;
                if Arc::ptr_eq(&instance.current_retirement()?, &expected) {
                    instance.drop_store();
                }
                if !expected.destruction_observed() {
                    bail!("captured Store destruction unconfirmed")
                }
                Ok(())
            })
            .await
    }
}

impl PackInstance {
    fn current_retirement(
        &self,
    ) -> Result<Arc<crate::generation_retirement::GenerationRetirement>> {
        let generation = self
            .retirement
            .lock()
            .map_err(|_| anyhow::anyhow!("retirement state poisoned"))?
            .clone()
            .context("Store generation missing")?;
        if !generation.matches(
            &self.pack.manifest.id,
            &self.pack.hash,
            self.current_generation,
        ) {
            bail!("Store generation binding mismatch")
        }
        if let Some((_, _, actual)) = &self.live {
            if !Arc::ptr_eq(actual, &generation) {
                bail!("actual Store generation mismatch")
            }
        }
        Ok(generation)
    }
    fn drop_store(&mut self) {
        let live = self.live.take();
        let generation = live
            .as_ref()
            .map(|(_, _, actual)| actual.clone())
            .or_else(|| self.current_retirement().ok());
        let had_store = live.is_some();
        // The receipt is published only after both actual resources drop.
        drop(live);
        drop(self.physical.take());
        if let Some(generation) = generation {
            if had_store {
                generation.destroyed();
            } else {
                generation.unstarted();
            }
        }
    }
    fn prepare_generation(&mut self) -> Result<()> {
        if self
            .live
            .as_ref()
            .is_some_and(|(_, instance, _)| instance.is_some())
            && self.current_retirement()?.is_live()
            && self.generation.load(Ordering::Acquire) == self.current_generation
        {
            return Ok(());
        }
        self.drop_store();
        let previous = self
            .retirement
            .lock()
            .map_err(|_| anyhow::anyhow!("retirement state poisoned"))?
            .clone();
        if let Some(previous) = previous {
            if !previous.replacement_ready()? {
                return Err(StepError::new(
                    "unavailable",
                    "previous Store retirement is not joined",
                )
                .into());
            }
        }
        Ok(())
    }
    async fn initialize(
        &mut self,
        shared: &Arc<Shared>,
        cx: &CallCx,
        owners: &crate::owned_values::ValueOwners,
        frame: &Arc<dyn crate::ProviderFrame>,
    ) -> Result<()> {
        if self.live.is_some() {
            if self
                .live
                .as_ref()
                .is_some_and(|(_, instance, _)| instance.is_some())
                && self.current_retirement()?.is_live()
            {
                return Ok(());
            }
            bail!("component instance retired during entry")
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
        let previous=self.retirement.lock().map_err(|_|anyhow::anyhow!("retirement state poisoned"))?.clone();
        if let Some(previous)=previous { if !previous.replacement_ready()? { bail!("previous Store retirement is not joined") } }
        let next=self.generation.load(Ordering::Acquire).checked_add(1).context("instance generations exhausted")?;
        let generation=crate::generation_retirement::GenerationRetirement::new(&shared.control,&self.pack.manifest.id,&self.pack.hash,next)?;
        let retirement=generation.token();
        *self.retirement.lock().map_err(|_|anyhow::anyhow!("retirement state poisoned"))?=Some(generation.clone());
        self.current_generation=next;
        self.generation.store(next,Ordering::Release);
        let wasi=RestrictedWasi::new_with_control(init_cx.limits().output_bytes,&shared.control)?;
        let physical=self.physical.as_ref().context("component physical reservation missing")?.clone();
        let mut store=shared.engine.store_owned(init_cx.clone(),PackState{shared:Arc::downgrade(shared),wasi,owners:owners.clone(),frame:Some(frame.clone())},physical)
            .map_err(|error|entry_phase_error(error,"store-admission",&init_cx,owners))?;
        store.data_mut().retirement=Some(retirement.clone());
        store.host_allocation_budget(owners.component().clone());
        // Own a partially initialized Store before the first await. A dropped
        // entry future leaves it available to the exact generation retirement.
        self.live=Some((store,None,generation));
        let mut linker=Linker::new(shared.engine.engine());
        if self.component.component_type().imports(shared.engine.engine()).any(|(name,_)|name.starts_with("wasi:")) {
            crate::wasi::link_restricted_wasi(&mut linker).map_err(|error|entry_phase_error(error,"restricted-wasi-link",&init_cx,owners))?;
            crate::wasi_effects::link_gated_wasi(&mut linker,&self.component,shared.engine.engine()).map_err(|error|entry_phase_error(error,"gated-wasi-link",&init_cx,owners))?;
        }
        crate::core_imports::link_core_and_ports(&mut linker,shared,&self.pack,&self.component,owners).map_err(|error|entry_phase_error(error,"core-import-link",&init_cx,owners))?;
        let instance={
        let (store,_,_)=self.live.as_mut().context("initializing Store missing")?;
        let _output=store.data().data.wasi.activate_output(&init_cx,owners.graph())?;
        tokio::select!{
            _=retirement.cancelled()=>bail!("component instance retired during initialization"),
            _=cx.cancel().cancelled()=>bail!("component instantiation cancelled"),
            _=tokio::time::sleep_until(cx.deadline().into())=>bail!("component instantiation deadline elapsed"),
            result=linker.instantiate_async(&mut *store,&self.component)=>result.map_err(|error|entry_phase_error(error.into(),"instantiate",&init_cx,owners))?,
        }};
        self.live.as_mut().context("initialized Store missing")?.1=Some(instance);
        let config=Val::String(wasmtime::component::BudgetedString::copy_from(std::str::from_utf8(&self.config)?,owners.component(),0)
            .map_err(|error|entry_phase_error(error.into(),"lifecycle-config",&init_cx,owners))?);
        let mut lifecycle=None;
        for (name,_) in self.component.component_type().exports(shared.engine.engine()) {
            if matches!(name,"tobkiri:core/lifecycle@1.0.0"|"tobkiri:core/lifecycle@1.1.0") {
                if lifecycle.replace(if name.ends_with("@1.0.0"){"tobkiri:core/lifecycle@1.0.0"}else{"tobkiri:core/lifecycle@1.1.0"}).is_some(){bail!("component exports ambiguous lifecycle package versions")}
            }
        }
        let lifecycle=lifecycle.context("component lifecycle export missing")?;
        let result=self.call(&init_cx,lifecycle,"init",&[config]).await.map_err(|error|entry_phase_error(error,"lifecycle-init",&init_cx,owners));
        if result.is_err(){self.drop_store();}
        let results=result?;
        if !matches!(results.as_slice(),[Val::Result(Ok(None))]){self.drop_store();bail!("component lifecycle initialization failed")}
        Ok(())
        }.await;
        if result.is_err() {
            self.drop_store();
        }
        shared
            .gate
            .record_outcome(
                grant_id,
                if result.is_ok() {
                    Outcome::Succeeded
                } else if cx.filesystem_mutation_possible() {
                    Outcome::Failed {
                        code: "filesystem-commit-unknown".into(),
                    }
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
        let (store, instance, _) = self
            .live
            .as_mut()
            .context("component instance unavailable")?;
        let instance = instance
            .as_ref()
            .context("component initialization incomplete")?;
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

impl Drop for PackInstance {
    fn drop(&mut self) {
        self.drop_store();
    }
}

/// Closed entry diagnostics distinguish an unavailable lift path from expired
/// inherited authority. Never interpolate guest text, SQL or host diagnostics.
fn entry_phase_error(
    error: anyhow::Error,
    phase: &'static str,
    cx: &CallCx,
    owners: &crate::owned_values::ValueOwners,
) -> anyhow::Error {
    crate::entry_diagnostics::annotate(error, phase, cx, owners, None)
}
pub(crate) fn step_error(error: anyhow::Error) -> StepError {
    if let Some((code, message)) = crate::core_imports::storage_failure_metadata(&error) {
        return StepError::new(code, message);
    }
    if let Some(error) = error.downcast_ref::<StepError>() {
        return error.clone();
    }
    let code = match error.downcast_ref::<tobkiri_gate::GateError>() {
        Some(tobkiri_gate::GateError::Limit(_) | tobkiri_gate::GateError::Deadline) => {
            "limit-exceeded"
        }
        Some(tobkiri_gate::GateError::Cancelled) => "cancelled",
        Some(tobkiri_gate::GateError::ApprovalRequired) => "approval-required",
        Some(_) => "denied",
        None => "component",
    };
    StepError::new(code, error.to_string())
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
    ) -> Result<(tobkiri_canon::CoreValue, tobkiri_gate::SourceReadReceipt), StepError> {
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

fn extract_stream_id(value: &CanonValue) -> Result<u64> {
    let mut value = value;
    if let CanonValue::Obj(fields) = value
        && let Some(CanonValue::Str(tag)) = fields.get("tag")
    {
        if tag != "ok" {
            bail!("provider declined the stream")
        }
        value = fields.get("val").context("stream result missing")?;
    }
    let CanonValue::Obj(fields) = value else {
        bail!("stream result is not a handle")
    };
    match fields.get("id") {
        Some(CanonValue::Int(id)) => Ok(u64::try_from(*id)?),
        Some(CanonValue::Str(id)) => Ok(id.parse()?),
        _ => bail!("stream handle ID missing"),
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
    async fn pull(&mut self, max: u32) -> Result<tobkiri_canon::CoreValue, StepError> {
        self.await_cleanup().await?;
        if self.ended {
            return self
                .terminal_batch
                .clone()
                .ok_or_else(|| StepError::new("cancelled", "component source is closed"));
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
                self.cancel();
                let cleanup = self.await_cleanup().await;
                return Err(crate::core_imports::stream_error_after_cleanup(
                    error, cleanup,
                ));
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
            return self
                .terminal_buffer
                .clone()
                .ok_or_else(|| StepError::new("cancelled", "component source is closed"));
        }
        self.host
            .shared
            .gate
            .check_context(&self.cx)
            .map_err(|e| step_error(e.into()))?;
        let result = if let Some(source) = &self.entry.source {
            source.lock().await.pull_final(max).await.map(|buffer| {
                let empty = buffer.as_bytes() == b"[]";
                (buffer, empty)
            })
        } else {
            crate::core_imports::pull_guest_source(&self.host, &self.entry, max, true)
                .await
                .map_err(step_error)
                .and_then(|(values, buffer)| {
                    Ok((
                        buffer.ok_or_else(|| {
                            StepError::new("invalid", "producer final batch missing")
                        })?,
                        values.array().is_some_and(|array| array.is_empty()),
                    ))
                })
        };
        let (buffer, empty) = match result {
            Ok(output) => output,
            Err(error) => {
                self.cancel();
                let cleanup = self.await_cleanup().await;
                return Err(crate::core_imports::stream_error_after_cleanup(
                    error, cleanup,
                ));
            }
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
        if self.ended && self.cleanup.is_none() && self.cleanup_error.is_some() {
            // Retry only retained terminal delivery; the handle receipt proves
            // whether physical cleanup already ran and forbids running it twice.
            self.cleanup_error = None;
            self.queue_cleanup();
        }
        self.cancel();
        self.await_cleanup().await
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
