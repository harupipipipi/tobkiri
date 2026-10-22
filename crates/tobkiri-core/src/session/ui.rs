use super::*;
use crate::ui_operation_preparation::UiOperationPreparation;
use tobkiri_canon::CoreBuffer;
use tobkiri_flow::UiInvocation;

#[path = "ui_source.rs"]
mod ui_source;

const MAX_UI_GENERATION: u64 = 9_007_199_254_740_991;

/// Unverified metadata supplied with a prepared UI invocation.
/// The separate `UiOperationPreparation` carries the unique operation owner.
pub struct UiInvokeRequest<'a> {
    pub session: &'a UiSessionId,
    pub host_session: &'a UiSessionId,
    pub generation: u64,
    pub receiver: &'a Digest,
    pub shape: ShapeRef,
    pub op: OpName,
    pub args: &'a CanonValue,
}

// Per-request metadata only; original source cause and physical ownership are
// independent. Every transition is monotonic: first cause, retirement, claim,
// then close start. Strong CAS can lose only to those four transitions.
const CLEANUP_REASON_MASK: u8 = 7;
const CLEANUP_RETIRED: u8 = 8;
const CLEANUP_CLAIMED: u8 = 16;
const CLEANUP_STARTED: u8 = 32;

struct UiStreamCleanupRequest {
    state: std::sync::atomic::AtomicU8,
}

/// Cloneable, funded cancellation metadata for one cleanup request. It owns no
/// source, source token, callback or execution authority. Acceptance never
/// changes the stream; the unique source owner separately commits cleanup.
#[derive(Clone)]
pub struct UiStreamCleanupControl {
    state: crate::funded_arc::FundedArc<UiStreamCleanupRequest>,
}

fn cleanup_request_code(cause: crate::UiOperationTermination) -> u8 {
    match cause {
        crate::UiOperationTermination::Manual => 1,
        crate::UiOperationTermination::Deadline => 2,
        crate::UiOperationTermination::Shutdown => 3,
        crate::UiOperationTermination::Retired => 4,
        crate::UiOperationTermination::Unknown => 5,
    }
}

fn cleanup_request_reason(word: u8) -> Option<crate::UiOperationTermination> {
    match word & CLEANUP_REASON_MASK {
        1 => Some(crate::UiOperationTermination::Manual),
        2 => Some(crate::UiOperationTermination::Deadline),
        3 => Some(crate::UiOperationTermination::Shutdown),
        4 => Some(crate::UiOperationTermination::Retired),
        5 => Some(crate::UiOperationTermination::Unknown),
        _ => None,
    }
}

fn cleanup_owner_unavailable() -> CoreError {
    StepError::new("unavailable", "UI cleanup ownership remains unresolved").into()
}

impl UiStreamCleanupControl {
    fn allocate(control: &tobkiri_gate::CoreControl) -> Result<Self, CoreError> {
        use crate::funded_arc::{FundedArc, Funding, new_funding};
        // Funding precedes the Arc and leased release-plan allocations. The
        // concrete owning-job future and simultaneous inline owner/carriers
        // are included before this control can be published to a registry.
        let bytes = crate::ui_operation_delivery::arc_owned_bytes::<UiStreamCleanupRequest>()?
            .checked_add(Funding::storage_bytes())
            .and_then(|n| n.checked_add(std::mem::size_of::<UiStreamCleanupOwner>()))
            .and_then(|n| n.checked_add(2 * std::mem::size_of::<Self>()))
            .and_then(|n| n.checked_add(ui_owned_cleanup_future_bytes()))
            .and_then(|n| u64::try_from(n).ok())
            .ok_or_else(cleanup_owner_unavailable)?;
        let funding = new_funding(control.reserve(ControlKind::Frame, bytes)?)?;
        Ok(Self {
            state: FundedArc::new(
                UiStreamCleanupRequest {
                    state: std::sync::atomic::AtomicU8::new(0),
                },
                funding,
            ),
        })
    }

    /// Fixed atomic receipt only. No source/token wake, lock, callback or
    /// allocation occurs here, including after the Running claim has won.
    pub fn accept(&self, cause: crate::UiOperationTermination) {
        let retired = if matches!(
            cause,
            crate::UiOperationTermination::Shutdown | crate::UiOperationTermination::Retired
        ) {
            CLEANUP_RETIRED
        } else {
            0
        };
        let mut word = self.state.state.load(Ordering::Acquire);
        for _ in 0..5 {
            let next = word
                | retired
                | if word & CLEANUP_REASON_MASK == 0 {
                    cleanup_request_code(cause)
                } else {
                    0
                };
            if next == word {
                return;
            }
            match self
                .state
                .state
                .compare_exchange(word, next, Ordering::AcqRel, Ordering::Acquire)
            {
                Ok(_) => return,
                Err(observed) => word = observed,
            }
        }
    }

    /// Request provenance only, never the original source's terminal cause.
    pub fn accepted(&self) -> Option<crate::UiOperationTermination> {
        cleanup_request_reason(self.state.state.load(Ordering::Acquire))
    }

    pub fn is_retired(&self) -> bool {
        self.state.state.load(Ordering::Acquire) & CLEANUP_RETIRED != 0
    }

    /// Exact metadata-owner identity, without exposing an Arc or authority.
    pub fn same_request(&self, other: &Self) -> bool {
        crate::funded_arc::FundedArc::ptr_eq(&self.state, &other.state)
    }

    fn claim(&self) -> Result<(), CoreError> {
        match self.state.state.compare_exchange(
            0,
            CLEANUP_CLAIMED,
            Ordering::AcqRel,
            Ordering::Acquire,
        ) {
            Ok(_) => Ok(()),
            Err(word) if word & CLEANUP_REASON_MASK != 0 => Err(GateError::Cancelled.into()),
            Err(_) => Err(GateError::Consumed.into()),
        }
    }

    fn start(&self) -> Result<(), CoreError> {
        let mut word = self.state.state.load(Ordering::Acquire);
        // Only first-cause and sticky retirement can race this unique start.
        for _ in 0..3 {
            if word & CLEANUP_CLAIMED == 0 || word & CLEANUP_STARTED != 0 {
                return Err(cleanup_owner_unavailable());
            }
            match self.state.state.compare_exchange(
                word,
                word | CLEANUP_STARTED,
                Ordering::AcqRel,
                Ordering::Acquire,
            ) {
                Ok(_) => return Ok(()),
                Err(observed) => word = observed,
            }
        }
        Err(cleanup_owner_unavailable())
    }
}

/// One existing source's cleanup capability. It retains the original Session,
/// entry lease, context and close control. It is not Clone and creates no new
/// invocation, deadline, candidate, source registration or physical proof.
pub struct UiStreamCleanupOwner {
    entry: UiStreamEntryLease,
    session: Session,
    handle: u64,
    request: UiStreamCleanupControl,
}

impl UiStreamCleanupOwner {
    pub fn request_control(&self) -> UiStreamCleanupControl {
        self.request.clone()
    }

    /// Metadata-only one-use Running claim, safe under the Host registry guard.
    /// Original source expiry/cancellation cannot disable its cleanup.
    pub fn claim(&mut self) -> Result<(), CoreError> {
        self.request.claim()
    }

    /// Close the retained original source, even after registry removal or UI
    /// retirement. A request cancellation after claim does not abandon cleanup.
    /// Call without outer guards; the existing owning job survives waiter loss.
    pub async fn close(self) -> Result<(), CoreError> {
        self.request.start()?;
        let Self {
            entry,
            session,
            handle,
            request,
        } = self;
        // Only the existing Manual source-close path runs here. Request cause
        // metadata is never copied to the original source's winner history.
        entry.close_control.cancel();
        entry.cx.cancel().cancel();
        let removed = {
            let mut registry = session
                .streams
                .lock()
                .map_err(|_| CoreError::Runtime("UI stream registry poisoned".into()))?;
            if registry
                .get(&handle)
                .is_some_and(|current| current.ptr_eq(&entry))
            {
                registry.remove(&handle)
            } else {
                None
            }
        };
        drop(removed);
        let source = { entry.state.lock().await.take() }.ok_or_else(cleanup_owner_unavailable)?;
        let custody = UiStreamCleanupCustody {
            _entry: entry,
            _request: request,
            _session: session.clone(),
        };
        session
            .finish_ui_stream_work(UiStreamCleanupWork {
                source,
                _custody: custody,
            })
            .await?
            .closed?;
        Ok(())
    }
}

impl Session {
    /// Retain one actual registered stream for cleanup. This checks its exact
    /// UI/handle binding but deliberately does not renew/check active authority.
    pub fn ui_stream_cleanup_owner(
        &self,
        ui: &UiSessionId,
        handle: u64,
    ) -> Result<UiStreamCleanupOwner, CoreError> {
        let entry = self.stream_entry(ui, handle)?;
        let request = UiStreamCleanupControl::allocate(entry.cx.control())?;
        Ok(UiStreamCleanupOwner {
            entry,
            session: self.clone(),
            handle,
            request,
        })
    }
}

// One owning job, with the source destroyed before the old entry charge,
// request metadata and final Session. No Join/task is retained by Session.
struct UiStreamCleanupCustody {
    _entry: UiStreamEntryLease,
    _request: UiStreamCleanupControl,
    _session: Session,
}
struct UiStreamCleanupWork<C> {
    source: UiStreamState,
    _custody: C,
}
// The legacy path's no-custody carrier does not enlarge its source payload.
const _: () =
    assert!(std::mem::size_of::<UiStreamCleanupWork<()>>() == std::mem::size_of::<UiStreamState>());

fn ui_owned_cleanup_future_bytes() -> usize {
    fn future_bytes<F>(_: fn(UiStreamCleanupWork<UiStreamCleanupCustody>) -> F) -> usize {
        std::mem::size_of::<F>()
    }
    future_bytes(run_ui_stream_cleanup::<UiStreamCleanupCustody>)
}

async fn run_ui_stream_cleanup<C: Send + 'static>(
    mut work: UiStreamCleanupWork<C>,
) -> UiStreamCleanup {
    let source = &mut work.source;
    let physical = source.stream.physical_completion();
    let closed = source.stream.close().await;
    let mut report = source.stream.close_report();
    if report.is_some() {
        // The report classifies the exact known terminal/errors; it is
        // not physical proof. Independently observe the SAME retained
        // source receipt before exposing a secondary business failure.
        let confirmed = match physical {
            Some(physical) => matches!(
                tokio::time::timeout(std::time::Duration::from_secs(2), physical.wait()).await,
                Ok(Ok(()))
            ),
            None => false,
        };
        if !confirmed {
            report = None;
            return UiStreamCleanup {
                closed: Err(StepError::new(
                    "component",
                    "UI source physical completion remains unknown",
                )),
                report,
            };
        }
    }
    UiStreamCleanup { closed, report }
}

impl Session {
    /// Mint and bind a UI token after checking the owning Pack's manifest.
    pub(crate) fn register_ui(
        &self,
        id: UiSessionId,
        host_session: UiSessionId,
        pack: PackId,
        hash: PackHash,
        ports: BTreeSet<ShapeRef>,
    ) -> Result<(), CoreError> {
        let mut sessions = self
            .ui
            .lock()
            .map_err(|_| CoreError::Runtime("UI context poisoned".into()))?;
        if sessions.contains_key(&id) {
            return Err(CoreError::Invalid(
                "UI session identifier was reused".into(),
            ));
        }
        let receiver = super::ui_receiver_identity(&id);
        let retirement = crate::ui_operation_delivery::UiScopeRetirement::new(self.gate.control())?;
        sessions.insert(
            id,
            UiPrincipal {
                pack,
                hash,
                ports,
                host_session,
                generation: None,
                receiver,
                cancel: self.shutdown.child_with_control(self.gate.control())?,
                retirement,
            },
        );
        Ok(())
    }

    /// Bind the trusted Launcher generation to this Session's Core-minted UI
    /// attachments. The Launcher calls this once after Core startup and before
    /// publishing any frame or accepting a UI message.
    pub fn bind_ui_generation(&self, generation: u64) -> Result<(), CoreError> {
        if generation == 0 || generation > MAX_UI_GENERATION {
            return Err(CoreError::Invalid("invalid UI generation".into()));
        }
        self.check_snapshot()?;
        let mut sessions = self
            .ui
            .lock()
            .map_err(|_| CoreError::Runtime("UI context poisoned".into()))?;
        if sessions
            .values()
            .any(|principal| principal.generation.is_some())
        {
            return Err(CoreError::Invalid(
                "UI generation was already bound for this Session".into(),
            ));
        }
        let materials = self
            .materials
            .lock()
            .map_err(|_| CoreError::Runtime("UI materials poisoned".into()))?;
        for (session, principal) in sessions.iter() {
            let pin = self
                .compiled
                .profile
                .packs
                .iter()
                .find(|pin| pin.id == principal.pack)
                .ok_or_else(|| CoreError::Invalid("UI source Pack is not pinned".into()))?;
            if self.compiled.profile.startup.ui.as_ref() != Some(&principal.pack)
                || pin.sha256 != principal.hash
                || principal.receiver != super::ui_receiver_identity(session)
                || !materials.contains_key(&(principal.pack.clone(), principal.hash.clone()))
                || principal
                    .ports
                    .iter()
                    .any(|port| !self.compiled.views.contains_key(port))
            {
                return Err(CoreError::Invalid(
                    "UI attachment differs from the frozen verified source".into(),
                ));
            }
        }
        for principal in sessions.values_mut() {
            principal.generation = Some(generation);
        }
        Ok(())
    }

    /// The host binds each rendered frame's MessageChannel to its Core token.
    pub fn ui_attachments(&self) -> Result<Vec<UiAttachment>, CoreError> {
        let sessions = self
            .ui
            .lock()
            .map_err(|_| CoreError::Runtime("UI context poisoned".into()))?;
        let materials = self
            .materials
            .lock()
            .map_err(|_| CoreError::Runtime("UI materials poisoned".into()))?;
        sessions
            .iter()
            .map(|(session, principal)| {
                let generation = principal.generation.ok_or_else(|| {
                    CoreError::Invalid("UI generation has not been bound by Launcher".into())
                })?;
                if principal.receiver != super::ui_receiver_identity(session) {
                    return Err(CoreError::Invalid(
                        "UI receiver differs from its Core attachment".into(),
                    ));
                }
                let material = materials
                    .get(&(principal.pack.clone(), principal.hash.clone()))
                    .ok_or_else(|| CoreError::Invalid("verified UI entry is missing".into()))?;
                Ok(UiAttachment {
                    session: session.clone(),
                    host_session: principal.host_session.clone(),
                    pack: principal.pack.clone(),
                    hash: principal.hash.clone(),
                    entry: material.entry.clone(),
                    generation: Some(generation),
                    receiver: principal.receiver.clone(),
                })
            })
            .collect()
    }

    /// Read bytes from the immutable verified Pack, scoped to an attached frame.
    pub fn verified_ui_asset(
        &self,
        session: &UiSessionId,
        path: &str,
    ) -> Result<Vec<u8>, CoreError> {
        self.check_snapshot()?;
        if path.is_empty()
            || path.starts_with('/')
            || path
                .split('/')
                .any(|p| p.is_empty() || p == "." || p == "..")
            || path.contains('\\')
        {
            return Err(CoreError::Invalid("invalid UI asset path".into()));
        }
        let sessions = self
            .ui
            .lock()
            .map_err(|_| CoreError::Runtime("UI context poisoned".into()))?;
        let ui = sessions
            .get(session)
            .ok_or_else(|| CoreError::Invalid("unknown UI session".into()))?;
        let materials = self
            .materials
            .lock()
            .map_err(|_| CoreError::Runtime("UI materials poisoned".into()))?;
        materials
            .get(&(ui.pack.clone(), ui.hash.clone()))
            .and_then(|m| m.files.get(path))
            .cloned()
            .ok_or_else(|| CoreError::NotFound("verified UI asset is missing".into()))
    }

    /// Compatibility entry for pre-receive callers. This API has no received
    /// attachment evidence and therefore cannot invoke a UI operation.
    pub async fn ui_invoke(
        &self,
        _session: UiSessionId,
        _shape: ShapeRef,
        _op: OpName,
        _args: CanonValue,
    ) -> Result<UiCallResult, CoreError> {
        Err(CoreError::Invalid(
            "UI invocation requires an authenticated received attachment".into(),
        ))
    }
    /// Compatibility entry for pre-receive callers. Use
    /// `ui_invoke_received_with_cancel` for the source-backed invocation path.
    pub async fn ui_invoke_with_cancel(
        &self,
        _session: UiSessionId,
        _shape: ShapeRef,
        _op: OpName,
        _args: CanonValue,
        _cancel: TokioCancellationToken,
    ) -> Result<UiCallResult, CoreError> {
        Err(CoreError::Invalid(
            "UI invocation requires an authenticated received attachment".into(),
        ))
    }

    /// Accept one invocation after the native host has authenticated its
    /// namespace, operation and attachment. Core checks those claims against
    /// the Session record before it materializes the argument graph.
    // Preserve this public transport API's explicit receiver/generation,
    // operation and cancellation bindings; callers already hold each value.
    #[allow(clippy::too_many_arguments)]
    pub async fn ui_invoke_received(
        &self,
        session: &UiSessionId,
        host_session: &UiSessionId,
        generation: u64,
        receiver: &Digest,
        shape: ShapeRef,
        op: OpName,
        args: &CanonValue,
    ) -> Result<CoreBuffer, CoreError> {
        self.ui_invoke_received_with_cancel(
            session,
            host_session,
            generation,
            receiver,
            shape,
            op,
            args,
            TokioCancellationToken::new(),
        )
        .await
    }

    /// Received UI entry with request-scoped cancellation. Its source budget
    /// is prepared before `CanonValue` is copied into Core ownership.
    // Preserve this public transport API's explicit receiver/generation,
    // operation and cancellation bindings; callers already hold each value.
    #[allow(clippy::too_many_arguments)]
    pub async fn ui_invoke_received_with_cancel(
        &self,
        session: &UiSessionId,
        host_session: &UiSessionId,
        generation: u64,
        receiver: &Digest,
        shape: ShapeRef,
        op: OpName,
        args: &CanonValue,
        cancel: TokioCancellationToken,
    ) -> Result<CoreBuffer, CoreError> {
        let close_control = self.ui_operation_close_control(session, None, cancel.clone())?;
        self.ui_invoke_received_with_close_control(
            session,
            host_session,
            generation,
            receiver,
            shape,
            op,
            args,
            close_control,
        )
        .await
    }

    /// Mint and register exact execution cleanup custody before starting the
    /// host task. Existing stream control is borrowed without its pull mutex.
    pub fn ui_operation_close_control(
        &self,
        ui: &UiSessionId,
        handle: Option<u64>,
        external: TokioCancellationToken,
    ) -> Result<UiOperationCloseHandle, CoreError> {
        self.ui_operation_close_control_inner(ui, handle, external, true)
    }
    /// Allocate one funded, process-pinned candidate without publishing Weak
    /// state or claiming work. The caller retains this same Arc across Busy.
    pub fn ui_operation_close_candidate(
        &self,
        ui: &UiSessionId,
        handle: Option<u64>,
        external: TokioCancellationToken,
    ) -> Result<UiOperationCloseHandle, CoreError> {
        self.ui_operation_close_control_inner(ui, handle, external, false)
    }
    fn ui_operation_close_control_inner(
        &self,
        ui: &UiSessionId,
        handle: Option<u64>,
        external: TokioCancellationToken,
        publish: bool,
    ) -> Result<UiOperationCloseHandle, CoreError> {
        let (token, generation, retirement) = {
            let sessions = self.ui.lock().map_err(|_| GateError::Unavailable)?;
            match sessions.get(ui) {
                Some(principal) => (
                    principal.cancel.child_with_control(self.gate.control())?,
                    principal.generation,
                    Some(principal.retirement.clone()),
                ),
                // A native Running record precedes received-entry validation.
                // Unknown claims get only shutdown-bound cleanup custody. None
                // cannot pass claim() as a UI generation or activate a source.
                None => (
                    self.shutdown.child_with_control(self.gate.control())?,
                    None,
                    None,
                ),
            }
        };
        let provider = self
            .execution_factory
            .as_ref()
            .ok_or(GateError::Unavailable)?
            .provider_authority();
        let control = if publish {
            UiOperationCloseControl::new_handle(
                provider,
                ui,
                generation,
                retirement,
                token,
                external.clone(),
            )?
        } else {
            UiOperationCloseControl::allocate(
                provider,
                ui,
                generation,
                retirement,
                token,
                external.clone(),
            )?
        };
        if let Some(handle) = handle
            && let Ok(entry) = self.stream_entry(ui, handle)
            && let Some(source) = &entry.source_control
        {
            control.bind(source)?.flush()?;
        }
        if external.is_cancelled() {
            control.cancel();
        }
        Ok(control)
    }

    /// Invoke using the same pre-task Core-minted execution cleanup record.
    // Preserve this public transport API's explicit receiver/generation,
    // operation and cancellation bindings; callers already hold each value.
    #[allow(clippy::too_many_arguments)]
    /// Compatibility wrapper: prepare the opaque owner (minting the ONE
    /// original invocation context) around an existing close control, then
    /// consume it through the same path a B1 prepared handoff uses.
    pub async fn ui_invoke_received_with_close_control(
        &self,
        session: &UiSessionId,
        host_session: &UiSessionId,
        generation: u64,
        receiver: &Digest,
        shape: ShapeRef,
        op: OpName,
        args: &CanonValue,
        close_control: UiOperationCloseHandle,
    ) -> Result<CoreBuffer, CoreError> {
        let preparation = self.ui_invoke_preparation(session, generation, close_control)?;
        self.ui_invoke_prepared(
            UiInvokeRequest {
                session,
                host_session,
                generation,
                receiver,
                shape,
                op,
                args,
            },
            preparation,
        )
        .await
    }

    /// Mint the ONE invoke CallCx before any publication retry, through the
    /// exact effective-permissions, narrowing and budget path dispatch uses.
    /// It is never rebuilt or refreshed after preparation.
    fn ui_invoke_preparation(
        &self,
        session: &UiSessionId,
        generation: u64,
        close_control: UiOperationCloseHandle,
    ) -> Result<UiOperationPreparation, CoreError> {
        // Only Core's stored attachment row supplies the pack/hash labels;
        // full match-claim validation still runs inside the consumed invoke.
        let (pack, hash) = {
            let sessions = self
                .ui
                .lock()
                .map_err(|_| CoreError::Runtime("UI context poisoned".into()))?;
            let principal = sessions
                .get(session)
                .ok_or_else(|| CoreError::Invalid("unknown UI session".into()))?;
            (principal.pack.clone(), principal.hash.clone())
        };
        let base_context = CallCx::try_root_with_control(
            self.compiled.profile.id.clone(),
            Principal::PackUi {
                pack,
                hash,
                ui: session.clone(),
            },
            close_control.request.clone(),
            close_control.token.clone(),
            deadline_after(self.compiled.profile.ceiling.wasm.wall_ms)?,
            self.compiled.profile.ceiling.wasm.clone(),
            self.gate.control().clone(),
        )?
        .with_security_epoch(self.epoch)
        .with_revision(self.compiled.plan_digest.clone());
        let source_permissions = self.gate.effective_permissions(&base_context)?;
        let context = base_context.narrow_limits(&source_permissions.wasm);
        // This received invocation is a top-level execution. Its argument
        // preparation and every provider/stream descendant must share the
        // actual UI caller's bounded ledger, just like User and Input entries.
        let budget = BudgetContext::new(source_permissions.loops, context.deadline())
            .map_err(|error| CoreError::Invalid(error.to_string()))?;
        let context = context.with_budget(budget)?;
        UiOperationPreparation::for_invoke(close_control, context, generation)
            .map_err(CoreError::from)
    }

    /// Allocate the unpublished candidate and return the opaque prepared
    /// owner carrying the original minted invoke context (B1 entry point).
    pub fn ui_operation_preparation(
        &self,
        session: &UiSessionId,
        host_session: &UiSessionId,
        generation: u64,
        receiver: &Digest,
        external: TokioCancellationToken,
    ) -> Result<UiOperationPreparation, CoreError> {
        // Host session/receiver/generation are match claims against Core's
        // stored attachment row, never authority to select a principal.
        {
            let sessions = self
                .ui
                .lock()
                .map_err(|_| CoreError::Runtime("UI context poisoned".into()))?;
            let principal = sessions
                .get(session)
                .ok_or_else(|| CoreError::Invalid("unknown UI session".into()))?;
            if principal.host_session != *host_session
                || principal.generation != Some(generation)
                || principal.receiver != *receiver
            {
                return Err(CoreError::Invalid(
                    "received UI invocation differs from its verified attachment".into(),
                ));
            }
        }
        let close_control = self.ui_operation_close_candidate(session, None, external)?;
        self.ui_invoke_preparation(session, generation, close_control)
    }

    /// Borrow the registered entry's original context, identity and close
    /// source into a prepared owner. The entry lease never leaves Session.
    pub fn ui_stream_pull_preparation(
        &self,
        ui: &UiSessionId,
        handle: u64,
        external: TokioCancellationToken,
    ) -> Result<UiOperationPreparation, CoreError> {
        let entry = self.stream_entry(ui, handle)?;
        let generation = self
            .ui
            .lock()
            .map_err(|_| CoreError::Runtime("UI context poisoned".into()))?
            .get(ui)
            .and_then(|principal| principal.generation);
        let candidate = self.ui_operation_close_candidate(ui, None, external)?;
        UiOperationPreparation::for_pull(
            candidate,
            entry.cx.clone(),
            self.clone(),
            ui,
            handle,
            Arc::as_ptr(&entry.entry) as usize,
            generation,
            entry.source_control.clone(),
        )
        .map_err(CoreError::from)
    }

    /// Registry-held identity recheck for a retained pull entry: reports
    /// removal, retirement and generation replacement, yields no lease.
    pub(crate) fn ui_stream_entry_live(
        &self,
        ui: &UiSessionId,
        handle: u64,
        key: usize,
        generation: Option<u64>,
    ) -> bool {
        let Ok(sessions) = self.ui.try_lock() else {
            return false;
        };
        if sessions
            .get(ui)
            .is_none_or(|principal| principal.generation != generation)
        {
            return false;
        }
        self.streams.try_lock().is_ok_and(|registry| {
            registry
                .get(&handle)
                .is_some_and(|entry| &entry.ui == ui && Arc::as_ptr(&entry.entry) as usize == key)
        })
    }

    /// Consuming invoke handoff over the original prepared owner. The minted
    /// context, request, cancellation lineage and ledger are reused exactly;
    /// no second deadline_after, narrowing refresh or BudgetContext::new runs.
    pub async fn ui_invoke_prepared(
        &self,
        request: UiInvokeRequest<'_>,
        preparation: UiOperationPreparation,
    ) -> Result<CoreBuffer, CoreError> {
        let UiInvokeRequest {
            session,
            host_session,
            generation,
            receiver,
            shape,
            op,
            args,
        } = request;
        if preparation.is_pull() {
            return Err(CoreError::Invalid(
                "prepared UI pull owner cannot dispatch an invocation".into(),
            ));
        }
        let (context, close_control, claimed) = preparation.into_invoke();
        let cancel = close_control.external_token();
        self.ensure_started().await?;
        self.check_snapshot()?;
        let provider = self
            .execution_factory
            .as_ref()
            .ok_or(GateError::Unavailable)?
            .provider_authority();
        if claimed {
            // An already-spent claim still must name this Session's real
            // provider/UI/generation; the one-use claim is never replayed.
            close_control.verify_claimed(&provider, session, generation)?;
        } else {
            close_control.claim(&provider, session, generation)?;
        }
        if generation == 0 || generation > MAX_UI_GENERATION {
            return Err(CoreError::Invalid("invalid UI generation claim".into()));
        }

        // Resolve source identity exclusively from Core's attachment table.
        // The received fields are match claims; none can select a principal,
        // owner, provider target, or budget.
        {
            let sessions = self
                .ui
                .lock()
                .map_err(|_| CoreError::Runtime("UI context poisoned".into()))?;
            let principal = sessions
                .get(session)
                .ok_or_else(|| CoreError::Invalid("unknown UI session".into()))?;
            let pin = self
                .compiled
                .profile
                .packs
                .iter()
                .find(|pin| pin.id == principal.pack)
                .ok_or_else(|| CoreError::Invalid("UI source Pack is not pinned".into()))?;
            if principal.host_session != *host_session
                || principal.generation != Some(generation)
                || principal.receiver != *receiver
                || principal.receiver != super::ui_receiver_identity(session)
                || self.compiled.profile.startup.ui.as_ref() != Some(&principal.pack)
                || pin.sha256 != principal.hash
                || !principal.ports.contains(&shape)
                || !self
                    .compiled
                    .views
                    .get(&shape)
                    .is_some_and(|view| view.functions.contains_key(&op))
            {
                return Err(CoreError::Invalid(
                    "received UI invocation differs from its verified attachment".into(),
                ));
            }
            let materials = self
                .materials
                .lock()
                .map_err(|_| CoreError::Runtime("UI materials poisoned".into()))?;
            if !materials.contains_key(&(principal.pack.clone(), principal.hash.clone())) {
                return Err(CoreError::Invalid(
                    "verified UI source material is missing".into(),
                ));
            }
            if principal.cancel.is_cancelled() {
                return Err(CoreError::Invalid("UI attachment is closed".into()));
            }
        }
        if cancel.is_cancelled() || context.cancel().is_cancelled() {
            return Err(CoreError::Step(StepError::new(
                "cancelled",
                "UI request was cancelled before preparation",
            )));
        }

        // Preparation resolves the unique frozen endpoint and reserves the
        // actual UI source's RuntimeValue scope before CoreValue allocation.
        let factory = self.execution_factory.as_ref().ok_or_else(|| {
            CoreError::Step(StepError::new(
                "unavailable",
                "Core UI invocation factory is unavailable",
            ))
        })?;
        let prepared =
            factory.prepare_ui_invocation(&context, session, generation, receiver, &shape, &op)?;
        if cancel.is_cancelled() || context.cancel().is_cancelled() {
            return Err(CoreError::Step(StepError::new(
                "cancelled",
                "UI request was cancelled before argument materialization",
            )));
        }

        ui_source::validate_args(args).map_err(|error| match error {
            ui_source::Error::Invalid => {
                CoreError::Step(StepError::new("invalid", "UI arguments are not StrictV1"))
            }
            ui_source::Error::Limit => CoreError::Step(StepError::new(
                "limit-exceeded",
                "UI arguments exceed the native control bound",
            )),
        })?;

        let invocation = UiInvocation {
            invocation: prepared.invocation(),
            session,
            generation,
            receiver,
            view: &shape,
            op: &op,
            target: prepared.target(),
        };
        let preparation = prepared.preparation();
        let argument_scope = preparation.argument_scope()?;
        let budget = self
            .invoker
            .allocation_budget_for_frame(&context, argument_scope)?;
        let owned_args =
            CoreValue::try_from_borrowed(args, &budget).map_err(ui_allocation_error)?;
        // The same source budget verifies the exact StrictV1 digest before the
        // final port admission; provider entry will rehash under its callee.
        let arguments_digest = owned_args
            .digest_guest(&budget)
            .map_err(ui_allocation_error)?;

        // The immutable provided view resolved by preparation is the exact
        // compiled view the actual dispatcher uses. A missing view is an
        // invariant failure, never a silent scalar classification.
        let provided_view = self
            .compiled
            .views
            .get(&prepared.target().provided)
            .ok_or_else(|| CoreError::Runtime("compiled UI dispatch view is missing".into()))?;
        // Only a declared stream operation consumes a fresh monotonic
        // handle, precharges its funded control storage, and reserves its
        // pending slot under the ui->streams lock order before the final
        // call can authorize or execute. A rejected reservation never
        // reaches the provider, so it cannot release shared source state
        // under live streams. Scalar operations consume no handle and no
        // slot.
        let pending = if provided_view.streams.contains_key(op.as_str()) {
            // The output adapter may publish this fixed candidate only if
            // the actual provider result is a stream. Gaps are harmless;
            // IDs never exceed JavaScript's exact integer range.
            let handle = self
                .next_stream
                .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |id| {
                    (id < (1u64 << 53) - 1).then_some(id + 1)
                })
                .map_err(|_| CoreError::Invalid("UI handle space exhausted".into()))?;
            let reservation = Arc::new(
                context
                    .control()
                    .reserve(
                        ControlKind::Registration,
                        ui_stream_entry_control_bytes(session)?,
                    )
                    .map_err(|error| CoreError::Gate(error.into()))?,
            );
            let lease = UiStreamEntryLease::new(
                Arc::new(UiStreamEntry {
                    ui: session.clone(),
                    cx: context.clone(),
                    external_cancel: cancel.clone(),
                    close_control: close_control.clone(),
                    source_control: None,
                    state: tokio::sync::Mutex::new(None),
                }),
                reservation,
            );
            let reserved = {
                let sessions = self
                    .ui
                    .lock()
                    .map_err(|_| CoreError::Runtime("UI context poisoned".into()))?;
                let mut registry = self
                    .streams
                    .lock()
                    .map_err(|_| CoreError::Runtime("UI stream registry poisoned".into()))?;
                let still_attached = sessions.get(session).is_some_and(|principal| {
                    principal.host_session == *host_session
                        && principal.generation == Some(generation)
                        && principal.receiver == *receiver
                });
                if !still_attached
                    || registry.len() >= UI_STREAMS_TOTAL
                    || registry
                        .values()
                        .filter(|entry| &entry.ui == session)
                        .count()
                        >= UI_STREAMS_PER_FRAME
                    || context.cancel().is_cancelled()
                    || cancel.is_cancelled()
                {
                    false
                } else {
                    registry.insert(handle, lease.clone());
                    true
                }
            };
            if !reserved {
                return Err(CoreError::Invalid(
                    "UI stream capacity exceeded or attachment closed".into(),
                ));
            }
            Some(PendingUiStream {
                session: self,
                handle,
                lease,
            })
        } else {
            None
        };
        // Scalar operations pass a never-registered sentinel; the output
        // adapter can only publish the real handle for a stream result.
        let stream_handle = pending.as_ref().map_or(0, |pending| pending.handle);

        let dispatch = self.invoker.call_ui_final(
            &context,
            &invocation,
            &preparation,
            &arguments_digest,
            owned_args,
            FinalOutputKind::UiInvokeValue { stream_handle },
        );
        tokio::pin!(dispatch);
        let mut watch_external = true;
        let mut watch_context = true;
        let mut watch_deadline = true;
        let output = loop {
            tokio::select! {
                _ = cancel.cancelled(), if watch_external => {
                    watch_external = false;
                    // This dedicated request channel has not yet woken the
                    // broad Core owner token. Record its request first; an
                    // already recorded retirement remains authoritative.
                    close_control.record(tobkiri_flow::StreamCloseReason::Manual);
                    context.cancel().cancel();
                }
                _ = context.cancel().cancelled(), if watch_context => {
                    watch_context = false;
                }
                _ = tokio::time::sleep_until(context.deadline().into()), if watch_deadline => {
                    watch_deadline = false;
                    close_control.record(tobkiri_flow::StreamCloseReason::Deadline);
                    context.cancel().cancel();
                }
                result = &mut dispatch => break result?,
            }
        };

        match output {
            FinalPortOutput::Value(buffer) => {
                if pending.is_some() {
                    return Err(CoreError::Runtime(
                        "declared UI stream operation returned a scalar result".into(),
                    ));
                }
                Ok(buffer)
            }
            FinalPortOutput::Stream { envelope, stream } => {
                let source_control = stream.close_control();
                let mut candidate = Some(stream);
                let (inserted, replaced) = {
                    let sessions = self
                        .ui
                        .lock()
                        .map_err(|_| CoreError::Runtime("UI context poisoned".into()))?;
                    let mut registry = self
                        .streams
                        .lock()
                        .map_err(|_| CoreError::Runtime("UI stream registry poisoned".into()))?;
                    let still_attached = sessions.get(session).is_some_and(|principal| {
                        principal.host_session == *host_session
                            && principal.generation == Some(generation)
                            && principal.receiver == *receiver
                    });
                    let still_reserved = pending.as_ref().is_some_and(|pending| {
                        registry
                            .get(&pending.handle)
                            .is_some_and(|entry| entry.ptr_eq(&pending.lease))
                    });
                    if !still_attached
                        || !still_reserved
                        || context.cancel().is_cancelled()
                        || cancel.is_cancelled()
                    {
                        (false, None)
                    } else {
                        let replaced = registry.insert(
                            stream_handle,
                            UiStreamEntryLease::new(
                                Arc::new(UiStreamEntry {
                                    ui: session.clone(),
                                    cx: context.clone(),
                                    external_cancel: cancel.clone(),
                                    close_control: close_control.clone(),
                                    source_control,
                                    state: tokio::sync::Mutex::new(Some(UiStreamState {
                                        stream: candidate.take().expect("new stream"),
                                        output_bytes: 0,
                                        pending_buffer: None,
                                    })),
                                }),
                                pending
                                    .as_ref()
                                    .expect("reserved stream")
                                    .lease
                                    .reservation
                                    .clone(),
                            ),
                        );
                        (true, replaced)
                    }
                };
                // The replaced pending lease is dropped outside the registry
                // guard; its shared reservation clone is retained by the
                // published live lease.
                drop(replaced);
                if !inserted {
                    let source = UiStreamState {
                        stream: candidate.expect("unregistered stream"),
                        output_bytes: 0,
                        pending_buffer: None,
                    };
                    self.finish_ui_stream(source).await?;
                    if pending.is_none() {
                        return Err(CoreError::Runtime(
                            "scalar UI operation returned a stream result".into(),
                        ));
                    }
                    return Err(CoreError::Invalid(
                        "UI stream capacity exceeded or attachment closed".into(),
                    ));
                }
                Ok(envelope)
            }
        }
    }
    fn stream_entry(&self, ui: &UiSessionId, handle: u64) -> Result<UiStreamEntryLease, CoreError> {
        let entry = self
            .streams
            .lock()
            .map_err(|_| CoreError::Runtime("UI stream registry poisoned".into()))?
            .get(&handle)
            .cloned()
            .ok_or_else(|| CoreError::Invalid("unknown UI stream handle".into()))?;
        if &entry.ui != ui {
            return Err(CoreError::Invalid(
                "UI stream belongs to another frame".into(),
            ));
        }
        Ok(entry)
    }
    /// Test-only clone of the real context retained by a registered stream
    /// entry; the clone shares the original ledger and reservation.
    #[cfg(test)]
    pub(crate) fn ui_stream_entry_cx(&self, ui: &UiSessionId, handle: u64) -> Option<CallCx> {
        self.stream_entry(ui, handle)
            .ok()
            .map(|entry| entry.cx.clone())
    }
    /// Compatibility surface for callers that still request materialized
    /// CanonValue batches. Received UI streams use the producer-encoded
    /// CoreBuffer path below so guest output never crosses this Vec boundary.
    pub async fn ui_stream_pull(
        &self,
        _ui: &UiSessionId,
        _handle: u64,
        _max: u32,
    ) -> Result<Vec<CanonValue>, CoreError> {
        Err(CoreError::Step(StepError::new(
            "unavailable",
            "UI stream output requires the fixed CoreBuffer pull path",
        )))
    }

    /// Pull a producer-encoded StrictV1 batch for the trusted host transport.
    /// This path preserves CoreBuffer ownership through the bounded host copy
    /// and never materializes guest stream frames as CanonValue/Vec values.
    pub async fn ui_stream_pull_buffer(
        &self,
        ui: &UiSessionId,
        handle: u64,
        max: u32,
    ) -> Result<CoreBuffer, CoreError> {
        if max == 0 || max > UI_STREAM_MAX_BATCH {
            return Err(CoreError::Invalid("UI pull bound must be in 1..=32".into()));
        }
        self.ensure_started().await?;
        let entry = self.stream_entry(ui, handle)?;
        let authority_expired = self.check_snapshot().is_err() || entry.cx.check_active().is_err();
        if authority_expired || entry.external_cancel.is_cancelled() {
            entry.close_control.record(if authority_expired {
                tobkiri_flow::StreamCloseReason::Unknown
            } else {
                tobkiri_flow::StreamCloseReason::Manual
            });
            self.ui_stream_cancel(ui, handle).await?;
            return Err(CoreError::Invalid("UI stream authority expired".into()));
        }
        let mut state = entry.state.lock().await;
        let source = state
            .as_mut()
            .ok_or_else(|| CoreError::Invalid("UI stream closed".into()))?;
        let mut notify_context = false;
        let result = if let Some(buffer) = source.pending_buffer.take() {
            Ok(buffer)
        } else {
            tokio::select! {
                _=entry.external_cancel.cancelled()=>{entry.close_control.record(tobkiri_flow::StreamCloseReason::Manual);notify_context=true;Err(StepError::new("cancelled","UI request cancelled"))},
                _=entry.cx.cancel().cancelled()=>Err(StepError::new("cancelled","UI stream cancelled")),
                _=tokio::time::sleep_until(entry.cx.deadline().into())=>Err(StepError::new("cancelled","original UI deadline expired")),
                result=source.stream.pull_final(max)=>result,
            }
        };
        let result = match result {
            Ok(buffer) if buffer.len() > UI_STREAM_MAX_BUFFER_BYTES => Err(StepError::new(
                "batch-too-large",
                "encoded UI stream batch exceeds the host copy limit",
            )),
            Ok(buffer) if buffer.as_bytes() != b"[]" => match u64::try_from(buffer.len()) {
                Ok(bytes) => match source
                    .output_bytes
                    .checked_add(bytes)
                    .filter(|total| *total <= entry.cx.limits().output_bytes)
                {
                    Some(total) => {
                        source.output_bytes = total;
                        Ok(buffer)
                    }
                    None => Err(StepError::new(
                        "limit-exceeded",
                        "UI stream byte limit exceeded",
                    )),
                },
                Err(_) => Err(StepError::new(
                    "limit-exceeded",
                    "UI stream byte limit exceeded",
                )),
            },
            result => result,
        };
        let ended = matches!(&result, Ok(buffer) if buffer.as_bytes() == b"[]");
        if ended || result.is_err() {
            let source = state.take().expect("live stream");
            drop(state);
            if notify_context {
                entry.cx.cancel().cancel();
            }
            self.streams
                .lock()
                .map_err(|_| CoreError::Runtime("UI stream registry poisoned".into()))?
                .remove(&handle);
            let cleanup = self.finish_ui_stream_observed(source).await?;
            match result {
                Err(original) => return Err(cleanup.failure(original)),
                Ok(buffer) => {
                    cleanup.closed?;
                    return Ok(buffer);
                }
            }
        }
        Ok(result?)
    }

    /// Consuming pull handoff: the prepared owner must still match the same
    /// live registered entry before the existing bounded pull path runs.
    pub async fn ui_stream_pull_prepared(
        &self,
        ui: &UiSessionId,
        handle: u64,
        max: u32,
        preparation: UiOperationPreparation,
    ) -> Result<CoreBuffer, CoreError> {
        // The consuming pull must still name the same registered entry Arc in
        // this Session's own registry, keep the original context/identity
        // live, and spend the genuine one-use claim on the SAME published
        // candidate before any pull work may run.
        let entry = self.stream_entry(ui, handle)?;
        let Some((entry_key, generation)) = preparation.pull_identity() else {
            return Err(CoreError::Invalid("prepared UI owner is not a pull".into()));
        };
        if !preparation.validate_pull(ui, handle) || Arc::as_ptr(&entry.entry) as usize != entry_key
        {
            return Err(CoreError::Invalid(
                "prepared UI pull no longer matches its stream entry".into(),
            ));
        }
        drop(entry);
        let generation = generation.ok_or(GateError::Unavailable)?;
        let provider = self
            .execution_factory
            .as_ref()
            .ok_or(GateError::Unavailable)?
            .provider_authority();
        let close_control = preparation.close_handle();
        if preparation.is_claimed() {
            close_control.verify_claimed(&provider, ui, generation)?;
        } else {
            close_control.claim(&provider, ui, generation)?;
        }
        self.ui_stream_pull_buffer(ui, handle, max).await
    }

    /// Cancel exactly one handle bound to this frame.
    pub async fn ui_stream_cancel(&self, ui: &UiSessionId, handle: u64) -> Result<(), CoreError> {
        let entry = self.stream_entry(ui, handle)?;
        entry.close_control.cancel();
        entry.cx.cancel().cancel();
        self.streams
            .lock()
            .map_err(|_| CoreError::Runtime("UI stream registry poisoned".into()))?
            .remove(&handle);
        if let Some(source) = entry.state.lock().await.take() {
            self.finish_ui_stream(source).await?;
        }
        Ok(())
    }
    /// Detach a rendered frame and close every outstanding request and stream.
    pub async fn detach_ui(&self, ui: &UiSessionId) -> Result<(), CoreError> {
        let principal = {
            let mut sessions = self
                .ui
                .lock()
                .map_err(|_| CoreError::Runtime("UI context poisoned".into()))?;
            let principal = sessions
                .get(ui)
                .ok_or_else(|| CoreError::Invalid("unknown UI session".into()))?;
            // Publish under the same lock that excludes candidate mint/removal.
            // This carrier survives the last transport/control owner through
            // the process-pinned delivery record. No callback/wake occurs here.
            principal.retirement.retire();
            sessions.remove(ui).expect("checked UI principal")
        };
        if let Some(factory) = &self.execution_factory {
            factory.retire_ui_operations(ui);
        }
        let entries = {
            let mut registry = self
                .streams
                .lock()
                .map_err(|_| CoreError::Runtime("UI stream registry poisoned".into()))?;
            let handles: Vec<_> = registry
                .iter()
                .filter(|(_, entry)| &entry.ui == ui)
                .map(|(id, _)| *id)
                .collect();
            handles
                .into_iter()
                .filter_map(|id| registry.remove(&id))
                .collect::<Vec<_>>()
        };
        for entry in &entries {
            entry
                .close_control
                .record(tobkiri_flow::StreamCloseReason::Retired);
        }
        principal.cancel.cancel();
        let mut failure = None;
        for entry in entries {
            entry.close_control.retire();
            entry.cx.cancel().cancel();
            if let Some(source) = entry.state.lock().await.take()
                && let Err(error) = self.finish_ui_stream(source).await
            {
                failure.get_or_insert(error);
            }
        }
        match failure {
            Some(error) => Err(error),
            None => Ok(()),
        }
    }
    /// Idempotently stop new work and signal every child without awaiting
    /// physical cleanup. Hosts must retain this Session and await `close`.
    pub fn request_shutdown(&self) {
        if let Some(factory) = &self.execution_factory {
            factory.close();
        }
        self.shutdown.cancel();
        self.admission.request_shutdown();
    }
    /// Stop this Session without opening a replacement revision.
    pub async fn close(&self) -> Result<(), CoreError> {
        self.request_shutdown();
        let frames: Vec<_> = self
            .ui
            .lock()
            .map_err(|_| CoreError::Runtime("UI context poisoned".into()))?
            .keys()
            .cloned()
            .collect();
        let mut failure = None;
        for frame in frames {
            if let Err(error) = self.detach_ui(&frame).await {
                failure.get_or_insert(error);
            }
        }
        if let Some(factory) = &self.execution_factory
            && let Err(error) = crate::tracked_stream::TrackedStream::check_native_cleanup(
                factory.native_cleanup_identity(),
            )
            .await
        {
            failure.get_or_insert(error.into());
        }
        // A source can have been cancelled before a Flow cleanup receipt was
        // created. Join its original terminal owner before Gate snapshots the
        // final audit watermark. Timeout drops only this observer; the host
        // retains the same owner and its actual completion evidence.
        match tokio::time::timeout(
            std::time::Duration::from_secs(2),
            self.invoker.drain_source_settlements(),
        )
        .await
        {
            Ok(Ok(())) => {}
            Ok(Err(error)) => {
                failure.get_or_insert(error.into());
            }
            Err(_) => {
                failure.get_or_insert_with(|| {
                    StepError::new("unavailable", "source terminal cleanup did not finish").into()
                });
            }
        }
        // Cleanup producers must finish their durable handoff before Gate's
        // final watermark/drain. Attempt that drain even if cleanup is unknown.
        if let Err(error) = self.admission.close().await {
            failure.get_or_insert(error);
        }
        match failure {
            Some(error) => Err(error),
            None => Ok(()),
        }
    }
    async fn finish_ui_stream(&self, source: UiStreamState) -> Result<(), CoreError> {
        self.finish_ui_stream_observed(source).await?.closed?;
        Ok(())
    }
    async fn finish_ui_stream_observed(
        &self,
        source: UiStreamState,
    ) -> Result<UiStreamCleanup, CoreError> {
        self.finish_ui_stream_work(UiStreamCleanupWork {
            source,
            _custody: (),
        })
        .await
    }
    async fn finish_ui_stream_work<C: Send + 'static>(
        &self,
        work: UiStreamCleanupWork<C>,
    ) -> Result<UiStreamCleanup, CoreError> {
        // Exactly the existing owning job; legacy custody is zero-sized.
        let cleanup = run_ui_stream_cleanup(work);
        let task =
            std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| self.runtime.spawn(cleanup)))
                .map_err(|_| {
                    CoreError::Runtime("owning runtime could not schedule UI cleanup".into())
                })?;
        task.await
            .map_err(|_| CoreError::Runtime("owning runtime did not complete UI cleanup".into()))
    }
}

struct UiStreamCleanup {
    closed: Result<(), StepError>,
    report: Option<tobkiri_flow::StreamCloseReport>,
}

impl UiStreamCleanup {
    fn failure(self, original: StepError) -> CoreError {
        match (self.closed, self.report) {
            (Ok(()), _) => original.into(),
            (Err(secondary), Some(report)) => ui_stream_failure(original, secondary, report),
            // Absent/uncertain physical or terminal provenance dominates; it
            // cannot be relabelled as the original transport-size failure.
            (Err(unknown), None) => unknown.into(),
        }
    }
}

fn ui_stream_failure(
    original: StepError,
    secondary: StepError,
    report: tobkiri_flow::StreamCloseReport,
) -> CoreError {
    let mut cleanup = CoreError::Step(secondary);
    if let (Some(business), Some(_audit)) = (report.business_error(), report.audit_error()) {
        // Tracked close returns audit before business. Retain that moved audit
        // error plus the bounded closed business classification; never clone
        // arbitrary guest error text to construct this native diagnostic.
        cleanup = CoreError::Cleanup {
            original: Box::new(CoreError::Step(StepError::new(
                business,
                "source completed with a business failure",
            ))),
            cleanup: Box::new(cleanup),
        };
    }
    CoreError::Cleanup {
        original: Box::new(CoreError::Step(original)),
        cleanup: Box::new(cleanup),
    }
}

/// Custody of one pre-dispatch UI stream slot reservation. On drop it
/// removes only its own still-pending registry lease by exact entry
/// identity, releases the owned map entry outside the registry guard, then
/// signals the original request's cancellation. A published live
/// replacement never shares this pending entry's identity, so the guard
/// cannot detach or cancel it; in-flight native cleanup remains owned by
/// the operation close control, not by this refund.
struct PendingUiStream<'a> {
    session: &'a Session,
    handle: u64,
    lease: UiStreamEntryLease,
}
impl Drop for PendingUiStream<'_> {
    fn drop(&mut self) {
        let removed = self
            .session
            .streams
            .lock()
            .map(|mut registry| {
                if registry
                    .get(&self.handle)
                    .is_some_and(|entry| entry.ptr_eq(&self.lease))
                {
                    registry.remove(&self.handle)
                } else {
                    None
                }
            })
            .unwrap_or_default();
        let was_pending = removed.is_some();
        drop(removed);
        if was_pending {
            self.lease.close_control.cancel();
            self.lease.cx.cancel().cancel();
        }
    }
}

/// Conservative funded bound for one reserved stream handle: the pending
/// entry plus its live replacement during the atomic swap, each entry's
/// lease, Arc and map-node body, the reservation owner and guard, and two
/// UiSessionId copies. Stream and provider internals keep their own
/// separate accounting.
pub(crate) fn ui_stream_entry_control_bytes(ui: &UiSessionId) -> Result<u64, CoreError> {
    const BASE: u64 = (std::mem::size_of::<UiStreamEntry>()
        + std::mem::size_of::<UiStreamState>()
        + std::mem::size_of::<UiStreamEntryLease>()
        + std::mem::size_of::<(u64, UiStreamEntryLease)>()
        + 96) as u64
        * 2
        + std::mem::size_of::<PendingUiStream<'static>>() as u64
        + std::mem::size_of::<Arc<ControlReservation>>() as u64;
    u64::try_from(ui.as_str().len())
        .ok()
        .and_then(|len| len.checked_mul(2))
        .and_then(|strings| BASE.checked_add(strings))
        .ok_or_else(|| CoreError::Invalid("UI stream control bound overflow".into()))
}

fn ui_allocation_error(error: tobkiri_canon::AllocationError) -> CoreError {
    let (code, message) = match error {
        tobkiri_canon::AllocationError::Invalid => {
            ("invalid", "UI arguments are not valid StrictV1 data")
        }
        tobkiri_canon::AllocationError::Limit
        | tobkiri_canon::AllocationError::Overflow
        | tobkiri_canon::AllocationError::Depth => {
            ("limit-exceeded", "UI argument RuntimeValue limit exceeded")
        }
        tobkiri_canon::AllocationError::Unavailable | tobkiri_canon::AllocationError::Allocator => {
            ("unavailable", "UI source allocation is unavailable")
        }
    };
    CoreError::Step(StepError::new(code, message))
}

#[cfg(test)]
mod cleanup_observation_tests {
    use super::*;

    fn oversized() -> StepError {
        StepError::new(
            "batch-too-large",
            "encoded UI batch exceeds its original limit",
        )
    }

    #[test]
    fn unclassified_cleanup_failure_keeps_unknown_primary() {
        let error = UiStreamCleanup {
            closed: Err(StepError::new(
                "component",
                "actual cleanup remains unknown",
            )),
            report: None,
        }
        .failure(oversized());
        assert!(matches!(error, CoreError::Step(error) if error.code == "component"));
    }

    #[test]
    fn classified_business_failure_retains_transport_and_source_errors() {
        // Metadata selection only; actual completion is independently checked
        // by finish_ui_stream_observed and the real oversized Flow E2E test.
        let source = StepError::new("unavailable", "original source business failure");
        let report = tobkiri_flow::StreamCloseReport::observed(
            &Outcome::Failed {
                code: "unavailable".into(),
            },
            Some(&source),
            None,
            tobkiri_flow::StreamCloseDelivery::Acknowledged,
        )
        .unwrap();
        let error = UiStreamCleanup {
            closed: Err(source),
            report: Some(report),
        }
        .failure(oversized());
        let CoreError::Cleanup { original, cleanup } = error else {
            panic!("both errors must remain typed")
        };
        assert!(matches!(*original, CoreError::Step(error) if error.code == "batch-too-large"));
        assert!(
            matches!(*cleanup, CoreError::Step(error) if error.code == "unavailable" && error.message == "original source business failure")
        );
    }

    #[test]
    fn classified_audit_failure_keeps_both_secondary_origins() {
        let business = StepError::new("unavailable", "business text is not copied into report");
        let audit = StepError::new("persistence", "original terminal delivery failure");
        let report = tobkiri_flow::StreamCloseReport::observed(
            &Outcome::Failed {
                code: "unavailable".into(),
            },
            Some(&business),
            Some(&audit),
            tobkiri_flow::StreamCloseDelivery::DurablyHandedOff,
        )
        .unwrap();
        let error = UiStreamCleanup {
            closed: Err(audit),
            report: Some(report),
        }
        .failure(oversized());
        let CoreError::Cleanup { original, cleanup } = error else {
            panic!("transport error must remain typed")
        };
        assert!(matches!(*original, CoreError::Step(error) if error.code == "batch-too-large"));
        let CoreError::Cleanup {
            original: business,
            cleanup: audit,
        } = *cleanup
        else {
            panic!("both secondary origins must remain typed")
        };
        assert!(matches!(*business, CoreError::Step(error) if error.code == "unavailable"));
        assert!(
            matches!(*audit, CoreError::Step(error) if error.code == "persistence" && error.message == "original terminal delivery failure")
        );
    }

    #[tokio::test]
    async fn prepared_pull_requires_live_published_same_entry_claim() {
        let fixture = crate::test_support::RuntimeFixture::new(&["fixture"], true).await;
        let session = fixture.session().await;
        session.bind_ui_generation(7).unwrap();
        let attachment = session
            .ui_attachments()
            .unwrap()
            .into_iter()
            .next()
            .unwrap();
        let shape = ShapeRef::parse("local.fixture.input@1.0").unwrap();
        let envelope = session
            .ui_invoke_received(
                &attachment.session,
                &attachment.host_session,
                7,
                &attachment.receiver,
                shape.clone(),
                tobkiri_canon::ids::OpName::parse("stream").unwrap(),
                &CanonValue::Obj(BTreeMap::new()),
            )
            .await
            .unwrap();
        let handle =
            serde_json::from_slice::<serde_json::Value>(envelope.as_bytes()).unwrap()["handle"]
                .as_u64()
                .unwrap();
        // An unpublished pull owner must never execute a pull.
        let unpublished = session
            .ui_stream_pull_preparation(
                &attachment.session,
                handle,
                tokio_util::sync::CancellationToken::new(),
            )
            .unwrap();
        assert!(
            session
                .ui_stream_pull_prepared(&attachment.session, handle, 16, unpublished)
                .await
                .is_err()
        );
        // An invoke-kind owner cannot be consumed as a pull.
        let invoke_owner = session
            .ui_operation_preparation(
                &attachment.session,
                &attachment.host_session,
                7,
                &attachment.receiver,
                tokio_util::sync::CancellationToken::new(),
            )
            .unwrap();
        assert!(
            session
                .ui_stream_pull_prepared(&attachment.session, handle, 16, invoke_owner)
                .await
                .is_err()
        );
        // A pull-kind owner cannot be consumed as an invoke.
        let pull_owner = session
            .ui_stream_pull_preparation(
                &attachment.session,
                handle,
                tokio_util::sync::CancellationToken::new(),
            )
            .unwrap();
        assert!(
            session
                .ui_invoke_prepared(
                    UiInvokeRequest {
                        session: &attachment.session,
                        host_session: &attachment.host_session,
                        generation: 7,
                        receiver: &attachment.receiver,
                        shape,
                        op: tobkiri_canon::ids::OpName::parse("read").unwrap(),
                        args: &CanonValue::Obj(BTreeMap::new()),
                    },
                    pull_owner,
                )
                .await
                .is_err()
        );
        // This is the actual principal's irreversible scope-retirement stamp,
        // not physical detach or a replacement generation. Context and the
        // same registered entry remain live, so refusal must reach the real
        // candidate's unspent retirement-aware claim.
        let mut stale = session
            .ui_stream_pull_preparation(
                &attachment.session,
                handle,
                tokio_util::sync::CancellationToken::new(),
            )
            .unwrap();
        let (entry_key, generation) = stale.pull_identity().unwrap();
        assert_eq!(generation, Some(7));
        stale.publish().await.unwrap();
        assert!(stale.is_live());
        assert!(!stale.is_claimed());
        assert!(session.ui_stream_entry_live(&attachment.session, handle, entry_key, generation,));
        {
            let principals = session.ui.lock().unwrap();
            let principal = principals.get(&attachment.session).unwrap();
            assert_eq!(principal.generation, Some(7));
            principal.retirement.retire();
        }
        assert_eq!(stale.close_handle().accepted(), (None, true));
        assert!(session.ui_stream_entry_live(&attachment.session, handle, entry_key, generation,));
        assert!(stale.is_live());
        assert!(stale.claim().is_err());
        assert!(!stale.is_claimed());
        let error = session
            .ui_stream_pull_prepared(&attachment.session, handle, 16, stale)
            .await
            .err()
            .unwrap();
        assert!(matches!(error, CoreError::Gate(GateError::Unavailable)));
        session.close().await.unwrap();
    }
}

#[cfg(test)]
mod owned_cleanup_tests {
    use super::*;
    use crate::UiOperationTermination;
    use crate::test_support::RuntimeFixture;
    use std::sync::atomic::AtomicUsize;

    async fn stream() -> (RuntimeFixture, Session, UiAttachment, u64) {
        let fixture = RuntimeFixture::new(&["fixture"], true).await;
        let session = fixture.session().await;
        session.bind_ui_generation(7).unwrap();
        let attachment = session
            .ui_attachments()
            .unwrap()
            .into_iter()
            .next()
            .unwrap();
        let output = session
            .ui_invoke_received(
                &attachment.session,
                &attachment.host_session,
                7,
                &attachment.receiver,
                ShapeRef::parse("local.fixture.input@1.0").unwrap(),
                OpName::parse("stream").unwrap(),
                &CanonValue::Obj(BTreeMap::new()),
            )
            .await
            .unwrap();
        let UiCallResult::Stream { handle } = serde_json::from_slice(output.as_bytes()).unwrap()
        else {
            panic!("real component did not return its stream handle")
        };
        (fixture, session, attachment, handle)
    }

    #[tokio::test]
    async fn preclaim_request_cancellation_and_unclaimed_drop_leave_source_untouched() {
        let (_fixture, session, attachment, handle) = stream().await;
        let entry = session.stream_entry(&attachment.session, handle).unwrap();
        let original = entry.close_control.accepted();
        let mut owner = session
            .ui_stream_cleanup_owner(&attachment.session, handle)
            .unwrap();
        let control = owner.request_control();
        control.accept(UiOperationTermination::Manual);
        control.accept(UiOperationTermination::Retired);
        assert_eq!(control.accepted(), Some(UiOperationTermination::Manual));
        assert!(control.is_retired());
        assert!(matches!(
            owner.claim(),
            Err(CoreError::Gate(GateError::Cancelled))
        ));
        assert!(owner.close().await.is_err());
        assert_eq!(entry.close_control.accepted(), original);
        assert!(!entry.cx.cancel().is_cancelled());
        assert!(!entry.external_cancel.is_cancelled());
        assert!(entry.state.lock().await.is_some());
        assert!(
            session
                .streams
                .lock()
                .unwrap()
                .get(&handle)
                .unwrap()
                .ptr_eq(&entry)
        );
        let untouched = session
            .ui_stream_cleanup_owner(&attachment.session, handle)
            .unwrap();
        drop(untouched);
        assert_eq!(entry.close_control.accepted(), original);
        assert!(!entry.cx.cancel().is_cancelled());
        session
            .ui_stream_cancel(&attachment.session, handle)
            .await
            .unwrap();
        session.close().await.unwrap();
    }

    #[tokio::test]
    async fn original_account_prepays_metadata_until_last_control_destruction() {
        let (_fixture, session, attachment, handle) = stream().await;
        let entry = session.stream_entry(&attachment.session, handle).unwrap();
        let account = entry.cx.control().clone();
        let baseline = account.usage();
        let owner = session
            .ui_stream_cleanup_owner(&attachment.session, handle)
            .unwrap();
        let control = owner.request_control();
        let other = control.clone();
        assert!(control.same_request(&other));
        assert_eq!(
            account.usage().count(ControlKind::Frame),
            baseline.count(ControlKind::Frame) + 1
        );
        let charged = account.usage().bytes;
        assert!(charged > baseline.bytes);
        let unwind = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let _owner = owner;
            panic!("deterministic preclaim owner unwind")
        }));
        assert!(unwind.is_err());
        assert_eq!(account.usage().bytes, charged);
        assert!(entry.state.lock().await.is_some());
        drop(control);
        assert_eq!(account.usage().bytes, charged);
        drop(other);
        assert_eq!(account.usage().bytes, baseline.bytes);
        assert_eq!(
            account.usage().count(ControlKind::Frame),
            baseline.count(ControlKind::Frame)
        );
        session
            .ui_stream_cancel(&attachment.session, handle)
            .await
            .unwrap();
        session.close().await.unwrap();
    }

    #[tokio::test]
    async fn failed_original_metadata_reservation_is_unpublished_and_has_no_source_effect() {
        let (_fixture, session, attachment, handle) = stream().await;
        let entry = session.stream_entry(&attachment.session, handle).unwrap();
        let account = entry.cx.control().clone();
        let baseline = account.usage();
        // RuntimeFixture opens the actual default Core account. Occupy its
        // remaining bytes through that SAME account, not a substitute sponsor.
        let remaining = tobkiri_gate::CoreControlLimits::default().total_bytes - baseline.bytes;
        let full = account.reserve(ControlKind::Frame, remaining).unwrap();
        let occupied = account.usage();
        let error = session
            .ui_stream_cleanup_owner(&attachment.session, handle)
            .err()
            .unwrap();
        assert!(matches!(error, CoreError::Gate(GateError::Limit(_))));
        assert_eq!(account.usage().bytes, occupied.bytes);
        assert_eq!(
            account.usage().count(ControlKind::Frame),
            occupied.count(ControlKind::Frame)
        );
        assert!(!entry.cx.cancel().is_cancelled());
        assert!(entry.state.lock().await.is_some());
        drop(full);
        assert_eq!(account.usage().bytes, baseline.bytes);
        session
            .ui_stream_cancel(&attachment.session, handle)
            .await
            .unwrap();
        session.close().await.unwrap();
    }

    #[tokio::test]
    async fn running_claim_wins_before_request_cancel_and_original_cleanup_still_runs() {
        let (_fixture, session, attachment, handle) = stream().await;
        let entry = session.stream_entry(&attachment.session, handle).unwrap();
        let mut owner = session
            .ui_stream_cleanup_owner(&attachment.session, handle)
            .unwrap();
        let request = entry.cx.request().clone();
        let control = owner.request_control();
        owner.claim().unwrap();
        assert!(matches!(
            owner.claim(),
            Err(CoreError::Gate(GateError::Consumed))
        ));
        control.accept(UiOperationTermination::Retired);
        assert!(!entry.cx.cancel().is_cancelled());
        assert!(entry.state.lock().await.is_some());
        owner.close().await.unwrap();
        assert_eq!(entry.cx.request(), &request);
        assert_eq!(
            entry.close_control.accepted().0,
            Some(tobkiri_flow::StreamCloseReason::Manual)
        );
        assert!(entry.cx.cancel().is_cancelled());
        assert!(entry.state.lock().await.is_none());
        assert!(!session.streams.lock().unwrap().contains_key(&handle));
        session.close().await.unwrap();
    }

    #[tokio::test]
    async fn concurrent_request_causes_keep_first_reason_and_retirement_without_source_wake() {
        let (_fixture, session, attachment, handle) = stream().await;
        let entry = session.stream_entry(&attachment.session, handle).unwrap();
        let mut owner = session
            .ui_stream_cleanup_owner(&attachment.session, handle)
            .unwrap();
        let control = owner.request_control();
        let barrier = std::sync::Barrier::new(3);
        std::thread::scope(|scope| {
            let control = &control;
            let barrier = &barrier;
            scope.spawn(move || {
                barrier.wait();
                control.accept(UiOperationTermination::Manual);
            });
            scope.spawn(move || {
                barrier.wait();
                control.accept(UiOperationTermination::Retired);
            });
            barrier.wait();
        });
        assert!(matches!(
            control.accepted(),
            Some(UiOperationTermination::Manual | UiOperationTermination::Retired)
        ));
        assert!(control.is_retired());
        assert!(owner.claim().is_err());
        drop(owner);
        assert!(!entry.cx.cancel().is_cancelled());
        assert!(entry.state.lock().await.is_some());
        session
            .ui_stream_cancel(&attachment.session, handle)
            .await
            .unwrap();
        session.close().await.unwrap();
    }

    #[tokio::test]
    async fn cancelled_original_context_still_allows_actual_owned_cleanup() {
        let (_fixture, session, attachment, handle) = stream().await;
        let entry = session.stream_entry(&attachment.session, handle).unwrap();
        let request = entry.cx.request().clone();
        entry.cx.cancel().cancel();
        let mut owner = session
            .ui_stream_cleanup_owner(&attachment.session, handle)
            .unwrap();
        owner.claim().unwrap();
        let result = owner.close().await;
        assert!(entry.state.lock().await.is_none());
        assert!(!session.streams.lock().unwrap().contains_key(&handle));
        assert_eq!(entry.cx.request(), &request);
        // The real source's close/terminal result is preserved. Rejection at
        // an active-context check would leave Some(source) and fail above.
        assert!(!matches!(
            result,
            Err(CoreError::Gate(GateError::Cancelled | GateError::Deadline))
        ));
        session.close().await.unwrap();
    }

    #[tokio::test]
    async fn expired_original_entry_deadline_is_not_refreshed_or_used_to_deny_cleanup() {
        let (_fixture, session, attachment, handle) = stream().await;
        let deadline = Instant::now();
        {
            let mut entries = session.streams.lock().unwrap();
            let lease = entries.get_mut(&handle).unwrap();
            let entry =
                Arc::get_mut(&mut lease.entry).expect("only actual registry owns this entry");
            // Deterministic UI-boundary expiry, sharing original ledger/request
            // and cancel lineage; this does not forge a provider frame/deadline.
            entry.cx = entry.cx.clone().cap_deadline(deadline);
        }
        let entry = session.stream_entry(&attachment.session, handle).unwrap();
        let mut owner = session
            .ui_stream_cleanup_owner(&attachment.session, handle)
            .unwrap();
        assert_eq!(owner.entry.cx.deadline(), deadline);
        owner.claim().unwrap();
        let result = owner.close().await;
        assert!(entry.state.lock().await.is_none());
        assert_eq!(entry.cx.deadline(), deadline);
        assert!(!matches!(
            result,
            Err(CoreError::Gate(GateError::Deadline | GateError::Cancelled))
        ));
        session.close().await.unwrap();
    }

    #[tokio::test]
    async fn removed_entry_cleanup_leaves_a_replacement_with_same_map_key_untouched() {
        let (_fixture, session, attachment, handle) = stream().await;
        let (_replacement_fixture, replacement_session, replacement_attachment, second) =
            stream().await;
        assert!(!Arc::ptr_eq(&session.state, &replacement_session.state));
        let original = session.stream_entry(&attachment.session, handle).unwrap();
        let replacement = replacement_session
            .stream_entry(&replacement_attachment.session, second)
            .unwrap();
        let (Some(original_source), Some(replacement_source)) =
            (&original.source_control, &replacement.source_control)
        else {
            panic!("expected independently retained actual native source controls")
        };
        assert!(!Arc::ptr_eq(original_source, replacement_source));
        let mut owner = session
            .ui_stream_cleanup_owner(&attachment.session, handle)
            .unwrap();
        owner.claim().unwrap();
        // Explicit internal identity-replacement boundary, not a claim that
        // normal monotonic handle allocation reuses keys or grants authority.
        // Separate real Sessions keep source Store destruction independent.
        let removed = {
            let mut entries = session.streams.lock().unwrap();
            entries.insert(handle, replacement.clone())
        };
        drop(removed);
        owner.close().await.unwrap();
        assert!(original.state.lock().await.is_none());
        assert!(replacement.state.lock().await.is_some());
        assert!(
            session
                .streams
                .lock()
                .unwrap()
                .get(&handle)
                .unwrap()
                .ptr_eq(&replacement)
        );
        let alias = { session.streams.lock().unwrap().remove(&handle) };
        drop(alias);
        replacement_session
            .ui_stream_cancel(&replacement_attachment.session, second)
            .await
            .unwrap();
        session.close().await.unwrap();
        replacement_session.close().await.unwrap();
    }

    #[tokio::test]
    async fn already_taken_source_is_unknown_never_another_callers_success() {
        let (_fixture, session, attachment, handle) = stream().await;
        let mut winner = session
            .ui_stream_cleanup_owner(&attachment.session, handle)
            .unwrap();
        let mut later = session
            .ui_stream_cleanup_owner(&attachment.session, handle)
            .unwrap();
        winner.claim().unwrap();
        later.claim().unwrap();
        winner.close().await.unwrap();
        let error = later.close().await.err().unwrap();
        assert!(matches!(error, CoreError::Step(error) if error.code == "unavailable"));
        session.close().await.unwrap();
    }

    #[tokio::test]
    async fn actual_detach_then_owned_close_does_not_fabricate_a_completion_receipt() {
        let (_fixture, session, attachment, handle) = stream().await;
        let entry = session.stream_entry(&attachment.session, handle).unwrap();
        let mut owner = session
            .ui_stream_cleanup_owner(&attachment.session, handle)
            .unwrap();
        owner.claim().unwrap();
        // Actual retirement takes the original source and preserves its
        // uncertain cleanup result; registry removal is not a receipt.
        let detach_error = session.detach_ui(&attachment.session).await.err().unwrap();
        assert!(matches!(detach_error, CoreError::Step(error) if error.code == "component"));
        assert!(!session.ui.lock().unwrap().contains_key(&attachment.session));
        assert!(!session.streams.lock().unwrap().contains_key(&handle));
        assert!(entry.state.lock().await.is_none());
        let retired = (Some(tobkiri_flow::StreamCloseReason::Retired), true);
        assert_eq!(entry.close_control.accepted(), retired);
        let error = owner.close().await.err().unwrap();
        assert!(matches!(error, CoreError::Step(error) if error.code == "unavailable"));
        assert_eq!(entry.close_control.accepted(), retired);
        session.close().await.unwrap();
    }

    struct PausedClose {
        inner: CoreStream,
        started: Arc<tokio::sync::Notify>,
        release: Arc<tokio::sync::Notify>,
        calls: Arc<AtomicUsize>,
        finished: Option<tokio::sync::oneshot::Sender<bool>>,
    }
    #[async_trait::async_trait]
    impl tobkiri_flow::StreamSource for PausedClose {
        async fn pull(&mut self, max: u32) -> Result<CoreValue, StepError> {
            self.inner.pull(max).await
        }
        async fn pull_final(&mut self, max: u32) -> Result<CoreBuffer, StepError> {
            self.inner.pull_final(max).await
        }
        fn cancel(&mut self) {
            self.inner.cancel();
        }
        fn close_control(&self) -> Option<Arc<dyn tobkiri_flow::StreamCloseControl>> {
            self.inner.close_control()
        }
        fn physical_completion(&self) -> Option<tobkiri_flow::StreamPhysicalCompletion> {
            self.inner.physical_completion()
        }
        fn close_report(&self) -> Option<tobkiri_flow::StreamCloseReport> {
            self.inner.close_report()
        }
        async fn close(&mut self) -> Result<(), StepError> {
            self.calls.fetch_add(1, Ordering::AcqRel);
            self.started.notify_one();
            self.release.notified().await;
            let result = self.inner.close().await;
            if let Some(finished) = self.finished.take() {
                let _ = finished.send(result.is_ok());
            }
            result
        }
    }

    #[tokio::test]
    async fn original_owning_job_retains_custody_after_cleanup_waiter_is_abandoned() {
        let (_fixture, session, attachment, handle) = stream().await;
        let entry = session.stream_entry(&attachment.session, handle).unwrap();
        let started = Arc::new(tokio::sync::Notify::new());
        let release = Arc::new(tokio::sync::Notify::new());
        let calls = Arc::new(AtomicUsize::new(0));
        let (finished, observed) = tokio::sync::oneshot::channel();
        {
            let mut state = entry.state.lock().await;
            let source = state.take().unwrap();
            *state = Some(UiStreamState {
                stream: CoreStream::new(PausedClose {
                    inner: source.stream,
                    started: started.clone(),
                    release: release.clone(),
                    calls: calls.clone(),
                    finished: Some(finished),
                }),
                output_bytes: source.output_bytes,
                pending_buffer: source.pending_buffer,
            });
        }
        let mut owner = session
            .ui_stream_cleanup_owner(&attachment.session, handle)
            .unwrap();
        let control = owner.request_control();
        let weak = control.state.downgrade();
        owner.claim().unwrap();
        let mut close = Box::pin(owner.close());
        tokio::select! {
            result = &mut close => panic!("cleanup resolved before barrier: {result:?}"),
            _ = started.notified() => {}
        }
        assert!(entry.state.lock().await.is_none());
        assert_eq!(calls.load(Ordering::Acquire), 1);
        drop(close); // Drop the actual boxed future, not only a Pin reference.
        drop(control);
        assert_eq!(
            weak.strong_count(),
            1,
            "the existing owning job retains request custody"
        );
        assert!(
            Arc::strong_count(&entry.entry) >= 2,
            "the same job retains original entry lease"
        );
        release.notify_one();
        assert!(observed.await.unwrap(), "original delegated close result");
        session.close().await.unwrap();
        drop(weak);
    }
}
