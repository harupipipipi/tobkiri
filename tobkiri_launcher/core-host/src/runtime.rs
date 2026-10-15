use std::{
    collections::{BTreeMap, BTreeSet},
    future::Future,
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};

use serde::Serialize;
use tobkiri_core::{
    CanonValue, Core, CoreBuffer, CoreConfig, CoreError, HostIo, Session, Variant,
    ids::{OpName, ProfileId, ShapeRef, UiSessionId},
    parse,
};
use tokio_util::sync::CancellationToken;

use crate::{
    HostCaller, HostError, PackAssetRequest,
    transport::{TransportTickets, copy_core_buffer, escaped_envelope_bound},
};

/// Trusted frontend metadata derived exclusively from a Core UI attachment.
#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct UiFrame {
    pub ui_session_id: UiSessionId,
    pub generation: u64,
    pub pack_hash: String,
    pub entry: String,
    pub platform: &'static str,
}

/// Host-minted flow ticket paired with a bounded CoreBuffer host copy.
/// `ticket_id` is exposed only to the trusted Launcher, never to the Pack frame.
#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct UiTransportResponse {
    pub namespace: String,
    pub generation: u64,
    pub receiver: UiSessionId,
    pub ticket_id: u64,
    pub payload: String,
}

/// Namespaced logical flow ticket for an operation whose payload is null.
#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct UiTransportTicket {
    pub namespace: String,
    pub generation: u64,
    pub receiver: UiSessionId,
    pub ticket_id: u64,
}

/// Exact operation identity accepted by the trusted native registration
/// handshake. The frame's request ID remains correlation metadata only.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum UiOperationKind {
    Invoke,
    StreamPull,
    StreamCancel,
}

impl UiOperationKind {
    pub fn parse(value: &str) -> Result<Self, HostError> {
        match value {
            "invoke" => Ok(Self::Invoke),
            "stream-pull" => Ok(Self::StreamPull),
            "stream-cancel" => Ok(Self::StreamCancel),
            _ => Err(HostError::Invalid),
        }
    }
}

/// ACK for a closed host-side registration handshake. It has no flow ticket
/// and does not account for Core allocation lifetime.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct UiOperationRegistration {
    namespace: String,
    generation: u64,
    receiver: UiSessionId,
    id: u64,
    kind: UiOperationKind,
    handle: Option<u64>,
}

#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
struct OperationScope {
    namespace: String,
    generation: u64,
    receiver: UiSessionId,
}

#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
struct OperationKey {
    scope: OperationScope,
    id: u64,
    kind: UiOperationKind,
    handle: Option<u64>,
}

impl OperationKey {
    fn new(
        namespace: &str,
        generation: u64,
        receiver: UiSessionId,
        id: u64,
        kind: UiOperationKind,
        handle: Option<u64>,
    ) -> Self {
        Self {
            scope: OperationScope {
                namespace: namespace.to_owned(),
                generation,
                receiver,
            },
            id,
            kind,
            handle,
        }
    }

    fn registration(&self) -> UiOperationRegistration {
        UiOperationRegistration {
            namespace: self.scope.namespace.clone(),
            generation: self.scope.generation,
            receiver: self.scope.receiver.clone(),
            id: self.id,
            kind: self.kind,
            handle: self.handle,
        }
    }
}

// Bound only the native registration-to-dispatch handshake, using the Pack UI
// SDK's maximum request timeout (its default is 30 seconds). Repeated ACKs do
// not renew it. Claimed Core work has no TTL: its slot and exact Session remain
// in custody until actual completion, including cancelled or unknown work.
const REGISTERED_OPERATION_TTL: Duration = Duration::from_secs(300);

enum OperationPhase {
    Registered {
        cancel_requested: bool,
        expires_at: Instant,
    },
    Running {
        cancel_requested: bool,
        cancel: CancellationToken,
    },
    Retired,
}

struct OperationRecord {
    phase: OperationPhase,
    // A claimed native task retains its exact Session even if its runtime is
    // lost. Such an unknown completion must never free a Running slot.
    session: Option<Arc<Session>>,
}

struct ActiveSession {
    generation: u64,
    session: Arc<Session>,
    runtime: tokio::runtime::Handle,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum CleanupPhase {
    Pending,
    Running,
    Failed,
}

struct RetiringSession {
    active: ActiveSession,
    phase: CleanupPhase,
}

#[derive(Default)]
struct SessionCustody {
    active: Option<ActiveSession>,
    // Publication requires this slot to be free. Moving active into it can
    // therefore never fail because another cleanup consumed its capacity.
    retiring: Option<RetiringSession>,
}

#[derive(Default)]
struct OperationState {
    active: BTreeMap<OperationKey, OperationRecord>,
    // One bounded high-water value per receiver seen in this generation.
    // Detach keeps its tombstone until generation retirement so a registration
    // already racing with detach cannot resurrect the receiver. A full
    // registration consumes its monotonic ID too, so it cannot be replayed.
    highwater: BTreeMap<OperationScope, u64>,
    closed: BTreeSet<OperationScope>,
    sessions: SessionCustody,
}

#[derive(Default)]
struct OperationRegistry {
    state: Mutex<OperationState>,
    cleanup_changed: tokio::sync::Notify,
}

/// The caller owns only cancellation and the response receiver. Dropping an
/// IPC future leaves the bounded native task and its Running record intact.
struct OperationWaiter<T> {
    cancel: CancellationToken,
    cancel_on_drop: bool,
    response: tokio::sync::oneshot::Receiver<Result<T, HostError>>,
}

impl<T> OperationWaiter<T> {
    async fn wait(mut self) -> Result<T, HostError> {
        let result = (&mut self.response).await.map_err(|_| HostError::Core)?;
        // A successful invocation may have transferred its token into a live
        // Core stream. Normal response delivery must not cancel that stream.
        if result.is_ok() {
            self.cancel_on_drop = false;
        }
        result
    }
}

impl<T> Drop for OperationWaiter<T> {
    fn drop(&mut self) {
        if self.cancel_on_drop {
            self.cancel.cancel();
        }
    }
}

struct CleanupGuard {
    registry: Arc<OperationRegistry>,
    session: Arc<Session>,
    generation: u64,
    completed: bool,
}

impl CleanupGuard {
    fn complete(&mut self, successful: bool) {
        if let Ok(mut state) = self.registry.state.lock()
            && let Some(retiring) = state.sessions.retiring.as_mut()
            && retiring.active.generation == self.generation
            && Arc::ptr_eq(&retiring.active.session, &self.session)
        {
            if successful {
                state.sessions.retiring.take();
            } else {
                retiring.phase = CleanupPhase::Failed;
            }
        }
        self.completed = true;
        self.registry.cleanup_changed.notify_one();
    }
}

impl Drop for CleanupGuard {
    fn drop(&mut self) {
        if !self.completed {
            self.complete(false);
        }
    }
}

impl OperationRegistry {
    fn expire_unclaimed(state: &mut OperationState) {
        let now = Instant::now();
        // At most MAX_HOST_INFLIGHT records are visited, without allocating or
        // scheduling an orphan cleanup task. Keep highwater and closed
        // tombstones so an expired registration can never be replayed.
        state.active.retain(|_, record| {
            !matches!(
                &record.phase,
                OperationPhase::Registered { expires_at, .. } if *expires_at <= now
            )
        });
    }

    fn register(
        &self,
        key: OperationKey,
        generation_is_active: impl FnOnce() -> bool,
    ) -> Result<UiOperationRegistration, HostError> {
        let mut state = self.state.lock().map_err(|_| HostError::Core)?;
        Self::expire_unclaimed(&mut state);
        if !generation_is_active() {
            return Err(HostError::Cancelled);
        }
        if state.closed.contains(&key.scope) {
            return Err(HostError::Cancelled);
        }
        if state.active.contains_key(&key) {
            return Ok(key.registration());
        }
        if state
            .active
            .keys()
            .any(|active| active.scope == key.scope && active.id == key.id)
        {
            return Err(HostError::Duplicate);
        }
        let previous = state.highwater.get(&key.scope).copied().unwrap_or(0);
        if key.id <= previous {
            return Err(HostError::Duplicate);
        }
        if !state.highwater.contains_key(&key.scope)
            && state.highwater.len() >= crate::transport::MAX_HOST_UI_ATTACHMENTS
        {
            return Err(HostError::RegistrationFull);
        }
        // Consume a valid monotonic ID before reporting backpressure. The UI
        // sequence advances on every structurally valid request and must not
        // become replayable if a slot later opens.
        state.highwater.insert(key.scope.clone(), key.id);
        if state.active.len() >= crate::transport::MAX_HOST_INFLIGHT {
            return Err(HostError::RegistrationFull);
        }
        state.active.insert(
            key.clone(),
            OperationRecord {
                phase: OperationPhase::Registered {
                    cancel_requested: false,
                    expires_at: Instant::now() + REGISTERED_OPERATION_TTL,
                },
                session: None,
            },
        );
        Ok(key.registration())
    }

    /// Atomically connects a real cancellation handle to a registration.
    /// A cancellation that won while Registered retires without starting the
    /// Core effect; a second claim never re-runs an operation.
    #[cfg(test)]
    fn claim(
        &self,
        key: &OperationKey,
        generation_is_active: impl FnOnce() -> bool,
    ) -> Result<CancellationToken, HostError> {
        let mut state = self.state.lock().map_err(|_| HostError::Core)?;
        Self::claim_locked(&mut state, key, generation_is_active)
    }

    fn claim_locked(
        state: &mut OperationState,
        key: &OperationKey,
        generation_is_active: impl FnOnce() -> bool,
    ) -> Result<CancellationToken, HostError> {
        Self::expire_unclaimed(state);
        if !generation_is_active() {
            return Err(HostError::Cancelled);
        }
        if state.closed.contains(&key.scope) {
            return Err(HostError::Cancelled);
        }
        let Some(record) = state.active.get(key) else {
            return if state
                .active
                .keys()
                .any(|active| active.scope == key.scope && active.id == key.id)
                || state
                    .highwater
                    .get(&key.scope)
                    .is_some_and(|last| key.id <= *last)
            {
                Err(HostError::Duplicate)
            } else {
                Err(HostError::Invalid)
            };
        };
        let registered_cancelled = matches!(
            &record.phase,
            OperationPhase::Registered {
                cancel_requested: true,
                ..
            }
        );
        let registered_ready = matches!(
            &record.phase,
            OperationPhase::Registered {
                cancel_requested: false,
                ..
            }
        );
        if registered_cancelled {
            if let Some(record) = state.active.get_mut(key) {
                record.phase = OperationPhase::Retired;
            }
            state.active.remove(key);
            Err(HostError::Cancelled)
        } else if registered_ready {
            let cancel = CancellationToken::new();
            if let Some(record) = state.active.get_mut(key) {
                record.phase = OperationPhase::Running {
                    cancel_requested: false,
                    cancel: cancel.clone(),
                };
            }
            Ok(cancel)
        } else {
            Err(HostError::Duplicate)
        }
    }

    /// Claim, bind the exact Session, and schedule its sole native task under
    /// the same boundary used by generation invalidation and publication.
    fn spawn_operation<T, F, Fut>(
        self: &Arc<Self>,
        key: OperationKey,
        generation_is_active: impl FnOnce() -> bool,
        operation: F,
    ) -> Result<OperationWaiter<T>, HostError>
    where
        T: Send + 'static,
        F: FnOnce(Arc<Session>, CancellationToken) -> Fut,
        Fut: Future<Output = Result<T, HostError>> + Send + 'static,
    {
        let mut state = self.state.lock().map_err(|_| HostError::Core)?;
        let cancel = Self::claim_locked(&mut state, &key, generation_is_active)?;
        let Some(active) = state.sessions.active.as_ref() else {
            state.active.remove(&key);
            return Err(HostError::NoSession);
        };
        if active.generation != key.scope.generation {
            state.active.remove(&key);
            return Err(HostError::Cancelled);
        }
        let session = active.session.clone();
        let runtime = active.runtime.clone();
        state.active.get_mut(&key).expect("claimed record").session = Some(session.clone());
        let (send, response) = tokio::sync::oneshot::channel();
        let registry = self.clone();
        let work = operation(session, cancel.clone());
        // Tokio does not synchronously poll a spawned future. A cancelled or
        // lost runtime leaves the record and its Session in custody, never
        // falsely advertising a free native task slot.
        let scheduled = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            runtime.spawn(async move {
                let completion = work.await;
                let result = match registry.retire(&key) {
                    Ok(true) => Err(HostError::Cancelled),
                    Ok(false) => completion,
                    Err(error) => Err(error),
                };
                // A gone IPC receiver drops its reservation and host copy here.
                let _ = send.send(result);
            })
        }));
        if scheduled.is_err() {
            drop(state);
            cancel.cancel();
            return Err(HostError::Core);
        }
        Ok(OperationWaiter {
            cancel,
            cancel_on_drop: true,
            response,
        })
    }

    /// Completion and cancellation share this lock. `true` means cancellation
    /// won before retirement; a later cancellation sees no record and is a
    /// no-op. This is a logical transport boundary only.
    fn retire(&self, key: &OperationKey) -> Result<bool, HostError> {
        let mut state = self.state.lock().map_err(|_| HostError::Core)?;
        let Some(mut record) = state.active.remove(key) else {
            return Ok(false);
        };
        let cancelled = match &mut record.phase {
            OperationPhase::Registered {
                cancel_requested, ..
            } => *cancel_requested,
            OperationPhase::Running {
                cancel_requested,
                cancel,
            } => *cancel_requested || cancel.is_cancelled(),
            OperationPhase::Retired => false,
        };
        record.phase = OperationPhase::Retired;
        Ok(cancelled)
    }

    fn cancel(
        &self,
        key: &OperationKey,
        generation_is_active: impl FnOnce() -> bool,
    ) -> Result<(), HostError> {
        let mut state = self.state.lock().map_err(|_| HostError::Core)?;
        Self::expire_unclaimed(&mut state);
        if !generation_is_active() {
            return Err(HostError::Cancelled);
        }
        if state.closed.contains(&key.scope) {
            return Err(HostError::Cancelled);
        }
        if let Some(record) = state.active.get_mut(key) {
            match &mut record.phase {
                OperationPhase::Registered {
                    cancel_requested, ..
                } => {
                    *cancel_requested = true;
                }
                OperationPhase::Running {
                    cancel_requested,
                    cancel,
                } => {
                    *cancel_requested = true;
                    let cancel = cancel.clone();
                    drop(state);
                    cancel.cancel();
                    return Ok(());
                }
                OperationPhase::Retired => {}
            }
            return Ok(());
        }
        if state
            .active
            .keys()
            .any(|active| active.scope == key.scope && active.id == key.id)
        {
            return Err(HostError::Duplicate);
        }
        if state
            .highwater
            .get(&key.scope)
            .is_some_and(|last| key.id <= *last)
        {
            // Exact late cancellation after a retired operation is harmless.
            return Ok(());
        }
        Err(HostError::Invalid)
    }

    fn retire_receiver(&self, scope: &OperationScope) -> Result<(), HostError> {
        let mut state = self.state.lock().map_err(|_| HostError::Core)?;
        state.closed.insert(scope.clone());
        let keys: Vec<_> = state
            .active
            .keys()
            .filter(|key| key.scope == *scope)
            .cloned()
            .collect();
        let mut cancellations = Vec::new();
        for key in keys {
            let unclaimed = state
                .active
                .get(&key)
                .is_some_and(|record| matches!(&record.phase, OperationPhase::Registered { .. }));
            if unclaimed {
                if let Some(record) = state.active.get_mut(&key) {
                    record.phase = OperationPhase::Retired;
                }
                state.active.remove(&key);
                continue;
            }
            if let Some(record) = state.active.get_mut(&key) {
                match &mut record.phase {
                    OperationPhase::Running {
                        cancel_requested,
                        cancel,
                    } => {
                        *cancel_requested = true;
                        cancellations.push(cancel.clone());
                    }
                    OperationPhase::Retired => {
                        unreachable!("retired records are removed under lock")
                    }
                    OperationPhase::Registered { .. } => unreachable!("checked above"),
                }
            }
        }
        drop(state);
        for cancel in cancellations {
            cancel.cancel();
        }
        Ok(())
    }

    #[cfg(test)]
    fn retire_generation(
        &self,
        namespace: &str,
        generation: u64,
        invalidate: impl FnOnce() -> Result<(), HostError>,
    ) -> Result<(), HostError> {
        let mut state = self.state.lock().map_err(|_| HostError::Core)?;
        invalidate()?;
        let cancellations = Self::retire_matching_generation(&mut state, namespace, generation);
        drop(state);
        for cancel in cancellations {
            cancel.cancel();
        }
        Ok(())
    }

    #[cfg(test)]
    fn retire_active_generation(
        &self,
        namespace: &str,
        active_generation: impl FnOnce() -> u64,
        invalidate: impl FnOnce(u64) -> Result<(), HostError>,
    ) -> Result<u64, HostError> {
        let mut state = self.state.lock().map_err(|_| HostError::Core)?;
        let generation = active_generation();
        invalidate(generation)?;
        let cancellations = Self::retire_matching_generation(&mut state, namespace, generation);
        drop(state);
        for cancel in cancellations {
            cancel.cancel();
        }
        Ok(generation)
    }

    fn retire_matching_generation(
        state: &mut OperationState,
        namespace: &str,
        generation: u64,
    ) -> Vec<CancellationToken> {
        let keys: Vec<_> = state
            .active
            .keys()
            .filter(|key| key.scope.namespace == namespace && key.scope.generation == generation)
            .cloned()
            .collect();
        let mut cancellations = Vec::new();
        for key in keys {
            let unclaimed = state
                .active
                .get(&key)
                .is_some_and(|record| matches!(&record.phase, OperationPhase::Registered { .. }));
            if unclaimed {
                if let Some(record) = state.active.get_mut(&key) {
                    record.phase = OperationPhase::Retired;
                }
                state.active.remove(&key);
                continue;
            }
            if let Some(record) = state.active.get_mut(&key) {
                match &mut record.phase {
                    OperationPhase::Running {
                        cancel_requested,
                        cancel,
                    } => {
                        *cancel_requested = true;
                        cancellations.push(cancel.clone());
                    }
                    OperationPhase::Retired => {
                        unreachable!("retired records are removed under lock")
                    }
                    OperationPhase::Registered { .. } => unreachable!("checked above"),
                }
            }
        }
        state
            .highwater
            .retain(|scope, _| scope.namespace != namespace || scope.generation != generation);
        state
            .closed
            .retain(|scope| scope.namespace != namespace || scope.generation != generation);
        cancellations
    }

    #[cfg(test)]
    fn commit_if(
        &self,
        still_current: impl FnOnce() -> bool,
        commit: impl FnOnce(),
    ) -> Result<(), HostError> {
        let _state = self.state.lock().map_err(|_| HostError::Core)?;
        if !still_current() {
            return Err(HostError::Cancelled);
        }
        commit();
        Ok(())
    }

    fn publish_session(
        &self,
        active: ActiveSession,
        still_current: impl FnOnce() -> bool,
        commit: impl FnOnce(),
    ) -> Result<(), HostError> {
        let mut state = self.state.lock().map_err(|_| HostError::Core)?;
        if !still_current() {
            return Err(HostError::Cancelled);
        }
        if state.sessions.active.is_some() || state.sessions.retiring.is_some() {
            return Err(HostError::Core);
        }
        state.sessions.active = Some(active);
        commit();
        Ok(())
    }

    fn retire_session(
        &self,
        namespace: &str,
        invalidate: impl FnOnce() -> Result<u64, HostError>,
    ) -> Result<(u64, Option<Arc<Session>>), HostError> {
        let mut state = self.state.lock().map_err(|_| HostError::Core)?;
        if state.sessions.active.is_some() && state.sessions.retiring.is_some() {
            return Err(HostError::Core);
        }
        let generation = invalidate()?;
        let cancellations = Self::retire_matching_generation(&mut state, namespace, generation);
        let session = state.sessions.active.take().map(|active| {
            let session = active.session.clone();
            state.sessions.retiring = Some(RetiringSession {
                active,
                phase: CleanupPhase::Pending,
            });
            session
        });
        drop(state);
        for cancel in cancellations {
            cancel.cancel();
        }
        Ok((generation, session))
    }

    fn retain_unpublished(&self, active: ActiveSession) -> Result<(), HostError> {
        let mut state = self.state.lock().map_err(|_| HostError::Core)?;
        if state.sessions.active.is_some() || state.sessions.retiring.is_some() {
            return Err(HostError::Core);
        }
        state.sessions.retiring = Some(RetiringSession {
            active,
            phase: CleanupPhase::Pending,
        });
        Ok(())
    }

    fn schedule_cleanup(self: &Arc<Self>) -> Result<(), HostError> {
        let pending = {
            let mut state = self.state.lock().map_err(|_| HostError::Core)?;
            let Some(retiring) = state.sessions.retiring.as_mut() else {
                return Ok(());
            };
            if retiring.phase != CleanupPhase::Pending {
                return Ok(());
            }
            retiring.phase = CleanupPhase::Running;
            (
                retiring.active.session.clone(),
                retiring.active.generation,
                retiring.active.runtime.clone(),
            )
        };
        let (session, generation, runtime) = pending;
        let mut guard = CleanupGuard {
            registry: self.clone(),
            session,
            generation,
            completed: false,
        };
        std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            runtime.spawn(async move {
                let successful = guard.session.close().await.is_ok();
                guard.complete(successful);
            })
        }))
        .map_err(|_| HostError::Core)?;
        Ok(())
    }

    async fn drain_retiring(self: &Arc<Self>) -> Result<(), HostError> {
        loop {
            let changed = self.cleanup_changed.notified();
            let phase = self
                .state
                .lock()
                .map_err(|_| HostError::Core)?
                .sessions
                .retiring
                .as_ref()
                .map(|retiring| retiring.phase);
            match phase {
                None => return Ok(()),
                Some(CleanupPhase::Failed) => return Err(HostError::Core),
                Some(CleanupPhase::Pending) => self.schedule_cleanup()?,
                Some(CleanupPhase::Running) => changed.await,
            }
        }
    }

    #[cfg(test)]
    fn active_len(&self) -> usize {
        self.state.lock().unwrap().active.len()
    }
}

#[derive(Debug)]
struct UiRequest {
    id: u64,
    shape: ShapeRef,
    op: OpName,
    args: CanonValue,
}

fn copy_request_json(json: &str) -> Result<String, HostError> {
    if json.len() > crate::transport::MAX_HOST_RESPONSE_BODY_BYTES {
        return Err(HostError::Limit);
    }
    let mut owned = String::new();
    owned
        .try_reserve_exact(json.len())
        .map_err(|_| HostError::Limit)?;
    owned.push_str(json);
    Ok(owned)
}

fn ui_request(json: &str) -> Result<UiRequest, HostError> {
    // StrictV1 rejects duplicate keys, unsafe numbers and oversized/deep input
    // before the lossy JSON/typed command layer can erase those distinctions.
    if json.len() > crate::transport::MAX_HOST_RESPONSE_BODY_BYTES {
        return Err(HostError::Limit);
    }
    let CanonValue::Obj(mut fields) =
        parse(json.as_bytes(), Variant::StrictV1).map_err(|_| HostError::Invalid)?
    else {
        return Err(HostError::Invalid);
    };
    if fields.len() != 4 {
        return Err(HostError::Invalid);
    }
    let id = match fields.remove("id") {
        Some(CanonValue::Int(id)) if id > 0 && id <= 9_007_199_254_740_991 => id as u64,
        _ => return Err(HostError::Invalid),
    };
    let shape = match fields.remove("shape") {
        Some(CanonValue::Str(shape)) => ShapeRef::parse(&shape).map_err(|_| HostError::Invalid)?,
        _ => return Err(HostError::Invalid),
    };
    let op = match fields.remove("op") {
        Some(CanonValue::Str(op)) => OpName::parse(&op).map_err(|_| HostError::Invalid)?,
        _ => return Err(HostError::Invalid),
    };
    let args = fields.remove("args").ok_or(HostError::Invalid)?;
    Ok(UiRequest {
        id,
        shape,
        op,
        args,
    })
}

fn wire(buffer: &CoreBuffer, receiver: &UiSessionId) -> Result<String, HostError> {
    // Core currently mints UI receivers as `ui_` plus a 26-character ULID.
    // Reject an unexpected identity format rather than exceed the fixed
    // envelope allowance used when reserving the host copy.
    if receiver.as_str().len() != 29 {
        return Err(HostError::Invalid);
    }
    escaped_envelope_bound(buffer.len())?;
    copy_core_buffer(buffer)
}

fn core_error(error: CoreError) -> HostError {
    let code = match error {
        CoreError::Invalid(_) | CoreError::Json(_) | CoreError::Canon(_) => "invalid",
        CoreError::Conflicts(_) => "conflict",
        CoreError::NotFound(_) => "not-found",
        CoreError::Gate(error) => error.code(),
        CoreError::Step(error) => error.audit_code(),
        _ => "unavailable",
    };
    HostError::Rejected(code)
}

/// The desktop process embeds one Core. The adapter owns no Pack registry,
/// permissions, provider resolution, original CallCx, or stream handles.
pub struct LauncherCore {
    core: Core,
    startup: tokio::sync::Mutex<()>,
    lifecycle: Arc<Lifecycle>,
    // This mutex owns the only active/retiring Session state as well as the
    // bounded native task ledger. No async lock is needed by lifecycle events.
    operations: Arc<OperationRegistry>,
    tickets: TransportTickets,
}

#[derive(Default)]
struct Lifecycle {
    lifecycle: AtomicU64,
    lifecycle_exhausted: std::sync::atomic::AtomicBool,
    generation: AtomicU64,
    active_lifecycle: AtomicU64,
    active_generation: AtomicU64,
}

impl Lifecycle {
    fn generation_is_active(&self, generation: u64) -> bool {
        !self.lifecycle_exhausted.load(Ordering::SeqCst)
            && generation != 0
            && generation == self.generation.load(Ordering::SeqCst)
            && generation == self.active_generation.load(Ordering::SeqCst)
            && self.active_lifecycle.load(Ordering::SeqCst) == self.lifecycle.load(Ordering::SeqCst)
    }
}

impl LauncherCore {
    pub fn open(host: Arc<dyn HostIo>) -> Result<Self, HostError> {
        Self::open_with_tickets(host, TransportTickets::new()?)
    }

    pub fn open_with_tickets(
        host: Arc<dyn HostIo>,
        tickets: TransportTickets,
    ) -> Result<Self, HostError> {
        Ok(Self::from_core_with_tickets(
            Core::open(CoreConfig::default(), host).map_err(|_| HostError::Core)?,
            tickets,
        ))
    }

    #[cfg(test)]
    fn from_core(core: Core) -> Self {
        Self::from_core_with_tickets(core, TransportTickets::default())
    }

    fn from_core_with_tickets(core: Core, tickets: TransportTickets) -> Self {
        Self {
            core,
            startup: tokio::sync::Mutex::new(()),
            lifecycle: Arc::new(Lifecycle::default()),
            operations: Arc::new(OperationRegistry::default()),
            tickets,
        }
    }

    /// Return the active Core generation for trusted host-side event routing.
    pub fn current_generation(&self) -> Option<u64> {
        let generation = self.lifecycle.active_generation.load(Ordering::SeqCst);
        self.generation_is_active(generation).then_some(generation)
    }

    pub async fn start(
        &self,
        caller: HostCaller<'_>,
        profile: &str,
    ) -> Result<Vec<UiFrame>, HostError> {
        caller.check()?;
        let profile = ProfileId::parse(profile).map_err(|_| HostError::Invalid)?;
        let generation = self.next_generation()?;
        let lifecycle = self.next_lifecycle()?;
        let _startup = self.startup.lock().await;
        self.close_current().await?;
        if !self.startup_is_current(lifecycle, generation) {
            return Err(HostError::Cancelled);
        }
        let session = Arc::new(
            self.core
                .start(&profile)
                .await
                .map_err(|_| HostError::Core)?,
        );
        let runtime = tokio::runtime::Handle::current();
        // Bind before any attachment is visible to the renderer. The received
        // UI entry verifies this generation against the Core source record.
        let attachments = match session
            .bind_ui_generation(generation)
            .and_then(|()| session.ui_attachments())
        {
            Ok(attachments) => attachments,
            Err(_) => {
                self.close_unpublished(session, generation, runtime).await?;
                return Err(HostError::Core);
            }
        };
        if attachments.len() > crate::transport::MAX_HOST_UI_ATTACHMENTS {
            self.close_unpublished(session, generation, runtime).await?;
            return Err(HostError::Limit);
        }
        let frames = attachments
            .into_iter()
            .map(|attachment| UiFrame {
                ui_session_id: attachment.session,
                generation,
                pack_hash: attachment.hash.to_string(),
                entry: attachment.entry,
                platform: if cfg!(windows) { "windows" } else { "native" },
            })
            .collect();
        let commit = self.operations.publish_session(
            ActiveSession {
                session: session.clone(),
                generation,
                runtime: runtime.clone(),
            },
            || self.startup_is_current(lifecycle, generation),
            || {
                self.lifecycle
                    .active_lifecycle
                    .store(lifecycle, Ordering::SeqCst);
                self.lifecycle
                    .active_generation
                    .store(generation, Ordering::SeqCst);
            },
        );
        if commit.is_err() {
            self.close_unpublished(session, generation, runtime).await?;
            return Err(HostError::Cancelled);
        }
        Ok(frames)
    }

    async fn active(&self) -> Result<Arc<Session>, HostError> {
        let generation = self.current_generation().ok_or(HostError::NoSession)?;
        self.active_for_generation_now(generation)
    }

    fn generation_is_active(&self, generation: u64) -> bool {
        self.lifecycle.generation_is_active(generation)
    }

    fn startup_is_current(&self, lifecycle: u64, generation: u64) -> bool {
        !self.lifecycle.lifecycle_exhausted.load(Ordering::SeqCst)
            && lifecycle == self.lifecycle.lifecycle.load(Ordering::SeqCst)
            && generation == self.lifecycle.generation.load(Ordering::SeqCst)
    }

    fn next_generation(&self) -> Result<u64, HostError> {
        let mut current = self.lifecycle.generation.load(Ordering::SeqCst);
        loop {
            let next = current
                .checked_add(1)
                .filter(|value| *value <= 9_007_199_254_740_991)
                .ok_or(HostError::Limit)?;
            match self.lifecycle.generation.compare_exchange_weak(
                current,
                next,
                Ordering::SeqCst,
                Ordering::SeqCst,
            ) {
                Ok(_) => return Ok(next),
                Err(observed) => current = observed,
            }
        }
    }

    fn next_lifecycle(&self) -> Result<u64, HostError> {
        if self.lifecycle.lifecycle_exhausted.load(Ordering::SeqCst) {
            return Err(HostError::Limit);
        }
        let mut current = self.lifecycle.lifecycle.load(Ordering::SeqCst);
        loop {
            let Some(next) = current.checked_add(1) else {
                self.lifecycle
                    .lifecycle_exhausted
                    .store(true, Ordering::SeqCst);
                return Err(HostError::Limit);
            };
            match self.lifecycle.lifecycle.compare_exchange_weak(
                current,
                next,
                Ordering::SeqCst,
                Ordering::SeqCst,
            ) {
                Ok(_) => return Ok(next),
                Err(observed) => current = observed,
            }
        }
    }

    fn active_for_generation_now(&self, generation: u64) -> Result<Arc<Session>, HostError> {
        if !self.generation_is_active(generation) {
            return Err(HostError::Cancelled);
        }
        let state = self
            .operations
            .state
            .try_lock()
            .map_err(|_| HostError::Limit)?;
        let active = state.sessions.active.as_ref().ok_or(HostError::NoSession)?;
        if active.generation != generation {
            return Err(HostError::Cancelled);
        }
        if !self.generation_is_active(generation) {
            return Err(HostError::Cancelled);
        }
        Ok(active.session.clone())
    }

    fn receiver_is_attached(session: &Session, ui: &UiSessionId) -> Result<bool, HostError> {
        Ok(session
            .ui_attachments()
            .map_err(|_| HostError::Core)?
            .iter()
            .any(|attachment| &attachment.session == ui))
    }

    pub fn register_ui_operation(
        &self,
        caller: HostCaller<'_>,
        namespace: &str,
        generation: u64,
        ui: &str,
        id: u64,
        kind: &str,
        handle: Option<u64>,
    ) -> Result<UiOperationRegistration, HostError> {
        caller.check()?;
        if !self.tickets.namespace_matches(namespace) {
            return Err(HostError::Cancelled);
        }
        if id == 0 || id > 9_007_199_254_740_991 {
            return Err(HostError::Invalid);
        }
        let kind = UiOperationKind::parse(kind)?;
        match (kind, handle) {
            (UiOperationKind::Invoke, None) => {}
            (UiOperationKind::StreamPull | UiOperationKind::StreamCancel, Some(handle))
                if handle > 0 && handle <= 9_007_199_254_740_991 => {}
            _ => return Err(HostError::Invalid),
        }
        let ui = UiSessionId::parse(ui).map_err(|_| HostError::Invalid)?;
        let session = self.active_for_generation_now(generation)?;
        if !Self::receiver_is_attached(&session, &ui)? {
            return Err(HostError::Cancelled);
        }
        if !self.generation_is_active(generation) {
            return Err(HostError::Cancelled);
        }
        self.operations.register(
            OperationKey::new(self.tickets.namespace(), generation, ui, id, kind, handle),
            || self.generation_is_active(generation),
        )
    }

    async fn close_unpublished(
        &self,
        session: Arc<Session>,
        generation: u64,
        runtime: tokio::runtime::Handle,
    ) -> Result<(), HostError> {
        self.operations.retain_unpublished(ActiveSession {
            session: session.clone(),
            generation,
            runtime,
        })?;
        session.request_shutdown();
        self.operations.schedule_cleanup()?;
        self.operations.drain_retiring().await
    }

    fn retire_current(&self, advance_lifecycle: bool) -> Result<(), HostError> {
        let mut lifecycle_result = Ok(());
        let (generation, session) =
            self.operations
                .retire_session(self.tickets.namespace(), || {
                    if advance_lifecycle {
                        lifecycle_result = self.next_lifecycle().map(|_| ());
                    }
                    let generation = self.lifecycle.active_generation.swap(0, Ordering::SeqCst);
                    self.lifecycle.active_lifecycle.store(0, Ordering::SeqCst);
                    Ok(generation)
                })?;
        // Factory closure and cancellation may wake Core tasks, so neither is
        // performed while holding the native operation/session mutex.
        if let Some(session) = session {
            session.request_shutdown();
        }
        let cleanup_result = self.operations.schedule_cleanup();
        if generation != 0 {
            self.tickets.retire_generation(generation)?;
        }
        cleanup_result?;
        lifecycle_result
    }

    async fn close_current(&self) -> Result<(), HostError> {
        self.retire_current(false)?;
        self.operations.drain_retiring().await
    }

    /// Invalidate navigation synchronously, promptly stop new Session work,
    /// and transfer its exact Arc to the single bounded native cleanup task.
    pub fn retire_active_generation(&self) -> Result<(), HostError> {
        self.retire_current(true)
    }

    pub async fn stop(&self, caller: HostCaller<'_>) -> Result<(), HostError> {
        caller.check()?;
        let _ = self.next_lifecycle();
        let _startup = self.startup.lock().await;
        self.close_current().await
    }

    pub async fn invoke(
        &self,
        caller: HostCaller<'_>,
        namespace: &str,
        generation: u64,
        ui: &str,
        id: u64,
        json: &str,
    ) -> Result<UiTransportResponse, HostError> {
        caller.check()?;
        if !self.tickets.namespace_matches(namespace) {
            return Err(HostError::Cancelled);
        }
        if id == 0 || id > 9_007_199_254_740_991 {
            return Err(HostError::Invalid);
        }
        let ui = UiSessionId::parse(ui).map_err(|_| HostError::Invalid)?;
        let key = OperationKey::new(
            self.tickets.namespace(),
            generation,
            ui.clone(),
            id,
            UiOperationKind::Invoke,
            None,
        );
        let tickets = self.tickets.clone();
        let lifecycle = self.lifecycle.clone();
        let completion = self.operations.spawn_operation(
            key,
            || self.generation_is_active(generation),
            |session, cancel| {
                // The bounded Running slot is claimed before retaining the
                // request's host copy. Parsing then belongs to its native task.
                let request_json = copy_request_json(json);
                async move {
                    if cancel.is_cancelled() {
                        return Err(HostError::Cancelled);
                    }
                    let attachment = session
                        .ui_attachments()
                        .map_err(|_| HostError::Core)?
                        .into_iter()
                        .find(|attachment| attachment.session == ui)
                        .filter(|attachment| attachment.generation == Some(generation))
                        .ok_or(HostError::Cancelled)?;
                    let request = ui_request(&request_json?)?;
                    if request.id != id {
                        return Err(HostError::Invalid);
                    }
                    let reservation = tickets.reserve(generation, ui.clone())?;
                    let ticket_id = reservation.ticket_id();
                    let result = session
                        .ui_invoke_received_with_cancel(
                            &ui,
                            &attachment.host_session,
                            generation,
                            &attachment.receiver,
                            request.shape,
                            request.op,
                            &request.args,
                            cancel.clone(),
                        )
                        .await;
                    if !lifecycle.generation_is_active(generation)
                        || !Self::receiver_is_attached(&session, &ui)?
                    {
                        return Err(HostError::Cancelled);
                    }
                    let buffer = result.map_err(core_error)?;
                    let output = wire(&buffer, &ui)?;
                    let namespace = tickets.copy_namespace()?;
                    Ok((
                        UiTransportResponse {
                            namespace,
                            generation,
                            receiver: ui,
                            ticket_id,
                            payload: output,
                        },
                        reservation,
                    ))
                }
            },
        )?;
        let (response, reservation) = completion.wait().await?;
        reservation.keep_until_ack();
        Ok(response)
    }

    pub fn cancel(
        &self,
        caller: HostCaller<'_>,
        namespace: &str,
        generation: u64,
        ui: &str,
        id: u64,
        kind: &str,
        handle: Option<u64>,
    ) -> Result<(), HostError> {
        caller.check()?;
        if !self.tickets.namespace_matches(namespace) {
            return Err(HostError::Cancelled);
        }
        if id == 0 || id > 9_007_199_254_740_991 {
            return Err(HostError::Invalid);
        }
        let kind = UiOperationKind::parse(kind)?;
        match (kind, handle) {
            (UiOperationKind::Invoke, None) => {}
            (UiOperationKind::StreamPull | UiOperationKind::StreamCancel, Some(handle))
                if handle > 0 && handle <= 9_007_199_254_740_991 => {}
            _ => return Err(HostError::Invalid),
        }
        let ui = UiSessionId::parse(ui).map_err(|_| HostError::Invalid)?;
        let key = OperationKey::new(self.tickets.namespace(), generation, ui, id, kind, handle);
        self.operations
            .cancel(&key, || self.generation_is_active(generation))
    }

    pub fn acknowledge(
        &self,
        caller: HostCaller<'_>,
        namespace: &str,
        generation: u64,
        ui: &str,
        ticket_id: u64,
    ) -> Result<(), HostError> {
        caller.check()?;
        if !self.tickets.namespace_matches(namespace) {
            return Err(HostError::Cancelled);
        }
        if ticket_id == 0 || ticket_id > 9_007_199_254_740_991 {
            return Err(HostError::Invalid);
        }
        let ui = UiSessionId::parse(ui).map_err(|_| HostError::Invalid)?;
        if !self.generation_is_active(generation) {
            return Ok(());
        }
        let _ = self
            .tickets
            .acknowledge(namespace, generation, ui, ticket_id)?;
        Ok(())
    }

    pub async fn detach(
        &self,
        caller: HostCaller<'_>,
        namespace: &str,
        generation: u64,
        ui: &str,
    ) -> Result<(), HostError> {
        caller.check()?;
        if !self.tickets.namespace_matches(namespace) {
            return Err(HostError::Cancelled);
        }
        let ui = UiSessionId::parse(ui).map_err(|_| HostError::Invalid)?;
        let session = self.active_for_generation_now(generation)?;
        if !Self::receiver_is_attached(&session, &ui)? {
            return Err(HostError::Cancelled);
        }
        let scope = OperationScope {
            namespace: self.tickets.namespace().to_owned(),
            generation,
            receiver: ui.clone(),
        };
        self.operations.retire_receiver(&scope)?;
        self.tickets.retire_receiver(generation, &ui)?;
        session.detach_ui(&ui).await.map_err(|_| HostError::Core)?;
        Ok(())
    }

    pub async fn stream_pull(
        &self,
        caller: HostCaller<'_>,
        namespace: &str,
        generation: u64,
        ui: &str,
        id: u64,
        handle: u64,
        max: u32,
    ) -> Result<UiTransportResponse, HostError> {
        caller.check()?;
        if !self.tickets.namespace_matches(namespace) {
            return Err(HostError::Cancelled);
        }
        if id == 0 || id > 9_007_199_254_740_991 {
            return Err(HostError::Invalid);
        }
        let ui = UiSessionId::parse(ui).map_err(|_| HostError::Invalid)?;
        let key = OperationKey::new(
            self.tickets.namespace(),
            generation,
            ui.clone(),
            id,
            UiOperationKind::StreamPull,
            Some(handle),
        );
        let tickets = self.tickets.clone();
        let lifecycle = self.lifecycle.clone();
        let completion = self.operations.spawn_operation(
            key,
            || self.generation_is_active(generation),
            move |session, cancel| async move {
                if cancel.is_cancelled() {
                    return Err(HostError::Cancelled);
                }
                if !Self::receiver_is_attached(&session, &ui)? {
                    return Err(HostError::Cancelled);
                }
                if handle == 0 || handle > 9_007_199_254_740_991 || !(1..=32).contains(&max) {
                    return Err(HostError::Invalid);
                }
                let reservation = tickets.reserve(generation, ui.clone())?;
                let ticket_id = reservation.ticket_id();
                let pull = session.ui_stream_pull_buffer(&ui, handle, max);
                tokio::pin!(pull);
                let result = tokio::select! {
                    biased;
                    _ = cancel.cancelled() => {
                        // The Core cancel signals the pull's context before
                        // waiting for its lock. Drain both actual futures; a
                        // transport cancellation is not physical completion.
                        let close = session.ui_stream_cancel(&ui, handle);
                        let _ = tokio::join!(&mut pull, close);
                        return Err(HostError::Cancelled);
                    }
                    result = &mut pull => result.map_err(core_error)?,
                };
                if !lifecycle.generation_is_active(generation)
                    || !Self::receiver_is_attached(&session, &ui)?
                {
                    return Err(HostError::Cancelled);
                }
                let output = wire(&result, &ui)?;
                let namespace = tickets.copy_namespace()?;
                Ok((
                    UiTransportResponse {
                        namespace,
                        generation,
                        receiver: ui.clone(),
                        ticket_id,
                        payload: output,
                    },
                    reservation,
                ))
            },
        )?;
        let (response, reservation) = completion.wait().await?;
        reservation.keep_until_ack();
        Ok(response)
    }

    pub async fn stream_cancel(
        &self,
        caller: HostCaller<'_>,
        namespace: &str,
        generation: u64,
        ui: &str,
        id: u64,
        handle: u64,
        abort_pull: bool,
    ) -> Result<Option<UiTransportTicket>, HostError> {
        caller.check()?;
        if !self.tickets.namespace_matches(namespace) {
            return Err(HostError::Cancelled);
        }
        if id == 0 || id > 9_007_199_254_740_991 {
            return Err(HostError::Invalid);
        }
        let ui = UiSessionId::parse(ui).map_err(|_| HostError::Invalid)?;
        if abort_pull {
            self.cancel(
                caller,
                namespace,
                generation,
                ui.as_str(),
                id,
                "stream-pull",
                Some(handle),
            )?;
            // Pull abort is an out-of-band control signal, never a response
            // ticket. The running operation sends the actual Core cancel.
            return Ok(None);
        }
        let key = OperationKey::new(
            self.tickets.namespace(),
            generation,
            ui.clone(),
            id,
            UiOperationKind::StreamCancel,
            Some(handle),
        );
        let tickets = self.tickets.clone();
        let lifecycle = self.lifecycle.clone();
        let completion = self.operations.spawn_operation(
            key,
            || self.generation_is_active(generation),
            move |session, cancel| async move {
                if cancel.is_cancelled() {
                    return Err(HostError::Cancelled);
                }
                if !Self::receiver_is_attached(&session, &ui)? {
                    return Err(HostError::Cancelled);
                }
                if handle == 0 || handle > 9_007_199_254_740_991 {
                    return Err(HostError::Invalid);
                }
                let reservation = tickets.reserve(generation, ui.clone())?;
                let ticket_id = reservation.ticket_id();
                // Once started, Core close stays owned and polled through its
                // actual terminal result even if this IPC caller disappears.
                session
                    .ui_stream_cancel(&ui, handle)
                    .await
                    .map_err(core_error)?;
                if !lifecycle.generation_is_active(generation)
                    || !Self::receiver_is_attached(&session, &ui)?
                {
                    return Err(HostError::Cancelled);
                }
                let namespace = tickets.copy_namespace()?;
                Ok((
                    UiTransportTicket {
                        namespace,
                        generation,
                        receiver: ui,
                        ticket_id,
                    },
                    reservation,
                ))
            },
        )?;
        let (ticket, reservation) = completion.wait().await?;
        reservation.keep_until_ack();
        Ok(Some(ticket))
    }

    pub async fn asset(&self, uri: &str) -> Result<(Vec<u8>, &'static str), HostError> {
        let request = PackAssetRequest::parse(uri)?;
        let mime = request.path.mime().ok_or(HostError::AssetPath)?;
        let session = self.active().await?;
        let attachment = session
            .ui_attachments()
            .map_err(|_| HostError::Core)?
            .into_iter()
            .find(|attachment| attachment.hash.hex() == request.hash_hex)
            .ok_or(HostError::AssetPath)?;
        let bytes = session
            .verified_ui_asset(&attachment.session, request.path.as_str())
            .map_err(|_| HostError::AssetPath)?;
        Ok((bytes, mime))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use async_trait::async_trait;
    use std::sync::{Barrier, atomic::AtomicU64};
    use tobkiri_core::{
        ApprovalAnswer, ApprovalPrompt, HostError as IoError, TrustedPrompt, UiEmbed,
    };

    fn caller() -> HostCaller<'static> {
        HostCaller {
            label: "core-host",
            url: "tauri://localhost/",
        }
    }

    async fn launcher_fixture() -> (LauncherCore, ProfileId, tempfile::TempDir) {
        let directory = tempfile::tempdir().unwrap();
        let core = Core::open(
            CoreConfig::for_test_data_root(directory.path().canonicalize().unwrap()),
            Arc::new(ClosedHost),
        )
        .unwrap();
        // An ordinary deny-by-default Profile goes through Core's real
        // persistence, approval, compiler, factory, and admission paths.
        let profile = tobkiri_core::profile::parse_profile(
            br#"{
                "format":"tobkiri.profile/1", "id":"prof_launcher_lifecycle",
                "name":"Launcher lifecycle fixture", "packs":[], "publishers":{},
                "ceiling":{
                    "wasm":{"fuel_per_call":100000,"memory_mib":8,"wall_ms":1000},
                    "loops":{"max_iterations":8,"max_wall_ms":1000,"max_cost_units":100}
                },
                "pack_limits":{}, "bindings":{}, "overlay_order":[], "user_overlays":[],
                "startup":{"inputs":[],"ui":null}
            }"#,
        )
        .unwrap();
        let id = profile.id.clone();
        core.save_profile(profile, None).await.unwrap();
        (LauncherCore::from_core(core), id, directory)
    }

    fn operation_key(
        namespace: &str,
        generation: u64,
        receiver: &str,
        id: u64,
        kind: UiOperationKind,
        handle: Option<u64>,
    ) -> OperationKey {
        OperationKey::new(
            namespace,
            generation,
            UiSessionId::parse(receiver).unwrap(),
            id,
            kind,
            handle,
        )
    }

    fn expire_registration(registry: &OperationRegistry, key: &OperationKey) {
        let mut state = registry.state.lock().unwrap();
        let record = state.active.get_mut(key).unwrap();
        let OperationPhase::Registered { expires_at, .. } = &mut record.phase else {
            panic!("only an unclaimed registration can expire");
        };
        *expires_at = Instant::now();
    }

    struct ClosedHost;
    #[async_trait]
    impl TrustedPrompt for ClosedHost {
        async fn prompt_approval(&self, _: ApprovalPrompt) -> ApprovalAnswer {
            ApprovalAnswer::Unavailable
        }
    }
    #[async_trait]
    impl HostIo for ClosedHost {
        fn display(&self, _: &str, _: &CanonValue) {}
        async fn terminal_read_line(&self, _: &str) -> Result<String, IoError> {
            Err(IoError("unavailable".into()))
        }
        fn terminal_write(&self, _: &str) {}
        fn embed_ui(&self, _: UiEmbed) -> Result<UiSessionId, IoError> {
            Err(IoError("unavailable".into()))
        }
        async fn pick_directory(&self, _: &str) -> Option<std::path::PathBuf> {
            None
        }
    }

    #[test]
    fn wire_rejects_duplicate_identity_and_authority_fields_before_dispatch() {
        for json in [
            r#"{"id":1,"id":2,"shape":"owner.pack.port@1.0","op":"read","args":{}}"#,
            r#"{"id":1,"shape":"owner.pack.port@1.0","op":"read","args":{},"approved":true}"#,
            r#"{"id":1,"shape":"owner.pack.port@1.0","op":"read","args":{},"ui_session_id":"forged"}"#,
            r#"{"id":1,"shape":"owner.pack.port@1.0","op":"read","args":{"n":9007199254740992}}"#,
        ] {
            assert!(ui_request(json).is_err());
        }
        let valid = ui_request(
            r#"{"id":1,"shape":"owner.pack.port@1.0","op":"read","args":{"float":"-0e0"}}"#,
        )
        .unwrap();
        assert_eq!(valid.id, 1);
        assert_eq!(valid.args.get("float").unwrap().as_str(), Some("-0e0"));
    }

    #[test]
    fn ui_request_checks_host_json_byte_limit_before_parsing() {
        let oversized = " ".repeat(crate::transport::MAX_HOST_RESPONSE_BODY_BYTES + 1);
        assert!(matches!(ui_request(&oversized), Err(HostError::Limit)));

        let at_limit = " ".repeat(crate::transport::MAX_HOST_RESPONSE_BODY_BYTES);
        assert!(matches!(ui_request(&at_limit), Err(HostError::Invalid)));
    }

    #[test]
    fn malformed_request_retires_its_registration_without_a_flow_ticket() {
        let tickets = crate::transport::TransportTickets::default();
        let registry = OperationRegistry::default();
        let key = operation_key(
            tickets.namespace(),
            1,
            "frame_receiver",
            1,
            UiOperationKind::Invoke,
            None,
        );
        registry.register(key.clone(), || true).unwrap();
        registry.claim(&key, || true).unwrap();
        assert!(matches!(ui_request("{}"), Err(HostError::Invalid)));
        assert_eq!(registry.retire(&key).unwrap(), false);
        assert_eq!(tickets.last_ticket_id().unwrap(), 0);
        assert_eq!(registry.active_len(), 0);
    }

    #[tokio::test]
    async fn missing_exact_session_retires_all_registered_dispatch_kinds() {
        let directory = tempfile::tempdir().unwrap();
        let core = Core::open(
            CoreConfig::for_test_data_root(directory.path().canonicalize().unwrap()),
            Arc::new(ClosedHost),
        )
        .unwrap();
        let launcher = LauncherCore::from_core(core);
        launcher.lifecycle.generation.store(1, Ordering::SeqCst);
        launcher
            .lifecycle
            .active_generation
            .store(1, Ordering::SeqCst);
        launcher.lifecycle.lifecycle.store(1, Ordering::SeqCst);
        launcher
            .lifecycle
            .active_lifecycle
            .store(1, Ordering::SeqCst);
        let namespace = launcher.tickets.namespace().to_owned();
        let ui = format!("ui_{}", ulid::Ulid::new());
        for (index, (kind, handle)) in [
            (UiOperationKind::Invoke, None),
            (UiOperationKind::StreamPull, Some(1)),
            (UiOperationKind::StreamCancel, Some(1)),
        ]
        .into_iter()
        .enumerate()
        {
            let id = index as u64 + 1;
            let key = operation_key(&namespace, 1, &ui, id, kind, handle);
            launcher.operations.register(key, || true).unwrap();
            let result = match kind {
                UiOperationKind::Invoke => launcher
                    .invoke(caller(), &namespace, 1, &ui, id, "{}")
                    .await
                    .map(|_| ()),
                UiOperationKind::StreamPull => launcher
                    .stream_pull(caller(), &namespace, 1, &ui, id, 1, 1)
                    .await
                    .map(|_| ()),
                UiOperationKind::StreamCancel => launcher
                    .stream_cancel(caller(), &namespace, 1, &ui, id, 1, false)
                    .await
                    .map(|_| ()),
            };
            assert!(matches!(result, Err(HostError::NoSession)));
            assert_eq!(launcher.operations.active_len(), 0);
            assert_eq!(launcher.tickets.snapshot().unwrap(), (0, 0));
        }
    }

    #[tokio::test]
    async fn navigation_stops_exact_session_promptly_and_reuses_one_cleanup_slot() {
        let (launcher, profile, _directory) = launcher_fixture().await;
        launcher.start(caller(), profile.as_str()).await.unwrap();
        let old = launcher.active().await.unwrap();
        let generation = launcher.current_generation().unwrap();

        // A current-thread runtime cannot poll cleanup until this synchronous
        // callback returns, so these checks observe request_shutdown itself.
        launcher.retire_active_generation().unwrap();
        assert!(launcher.current_generation().is_none());
        assert!(old.bind_ui_generation(generation).is_err());
        for _ in 0..16 {
            launcher.retire_active_generation().unwrap();
            let state = launcher.operations.state.lock().unwrap();
            assert!(state.sessions.active.is_none());
            let retiring = state.sessions.retiring.as_ref().unwrap();
            assert!(Arc::ptr_eq(&retiring.active.session, &old));
            assert!(retiring.phase == CleanupPhase::Running);
        }

        // Restart must await that exact cleanup before publishing a new Arc.
        launcher.start(caller(), profile.as_str()).await.unwrap();
        let new = launcher.active().await.unwrap();
        assert!(!Arc::ptr_eq(&new, &old));
        assert!(
            launcher
                .operations
                .state
                .lock()
                .unwrap()
                .sessions
                .retiring
                .is_none()
        );
        new.bind_ui_generation(launcher.current_generation().unwrap())
            .unwrap();

        // A delayed observation of the old task cannot clear the new Session.
        let mut stale = CleanupGuard {
            registry: launcher.operations.clone(),
            session: old,
            generation,
            completed: false,
        };
        stale.complete(true);
        assert!(Arc::ptr_eq(&launcher.active().await.unwrap(), &new));
        new.bind_ui_generation(launcher.current_generation().unwrap())
            .unwrap();
        launcher.stop(caller()).await.unwrap();
    }

    #[tokio::test]
    async fn navigation_between_core_start_and_publication_closes_unpublished_arc() {
        let (launcher, profile, _directory) = launcher_fixture().await;
        let generation = launcher.next_generation().unwrap();
        let lifecycle = launcher.next_lifecycle().unwrap();
        let session = Arc::new(launcher.core.start(&profile).await.unwrap());
        launcher.retire_active_generation().unwrap();
        assert!(matches!(
            launcher.operations.publish_session(
                ActiveSession {
                    session: session.clone(),
                    generation,
                    runtime: tokio::runtime::Handle::current(),
                },
                || launcher.startup_is_current(lifecycle, generation),
                || panic!("retired startup must never publish"),
            ),
            Err(HostError::Cancelled)
        ));
        launcher
            .close_unpublished(
                session.clone(),
                generation,
                tokio::runtime::Handle::current(),
            )
            .await
            .unwrap();
        assert!(session.bind_ui_generation(generation).is_err());
        let state = launcher.operations.state.lock().unwrap();
        assert!(state.sessions.active.is_none());
        assert!(state.sessions.retiring.is_none());
    }

    #[tokio::test]
    async fn unknown_cleanup_keeps_exact_arc_and_blocks_new_publication() {
        let (launcher, profile, _directory) = launcher_fixture().await;
        let session = Arc::new(launcher.core.start(&profile).await.unwrap());
        launcher
            .operations
            .retain_unpublished(ActiveSession {
                session: session.clone(),
                generation: 1,
                runtime: tokio::runtime::Handle::current(),
            })
            .unwrap();
        session.request_shutdown();
        let guard = CleanupGuard {
            registry: launcher.operations.clone(),
            session: session.clone(),
            generation: 1,
            completed: false,
        };
        // This is the same Drop path as a cancelled owning runtime or a
        // panicking cleanup task; unknown completion never releases capacity.
        drop(guard);
        assert!(matches!(
            launcher.operations.drain_retiring().await,
            Err(HostError::Core)
        ));
        assert!(matches!(
            launcher.start(caller(), profile.as_str()).await,
            Err(HostError::Core)
        ));
        let state = launcher.operations.state.lock().unwrap();
        let retiring = state.sessions.retiring.as_ref().unwrap();
        assert!(Arc::ptr_eq(&retiring.active.session, &session));
        assert!(retiring.phase == CleanupPhase::Failed);
        assert!(state.sessions.active.is_none());
    }

    #[tokio::test]
    async fn dropped_ipc_waiter_cancels_but_keeps_native_task_and_ticket_until_terminal() {
        let (launcher, profile, _directory) = launcher_fixture().await;
        launcher.start(caller(), profile.as_str()).await.unwrap();
        let generation = launcher.current_generation().unwrap();
        let namespace = launcher.tickets.namespace().to_owned();
        let key = operation_key(
            &namespace,
            generation,
            "native_task_fixture",
            1,
            UiOperationKind::Invoke,
            None,
        );
        launcher.operations.register(key.clone(), || true).unwrap();
        let (started_send, started) = tokio::sync::oneshot::channel();
        let (cancelled_send, cancelled) = tokio::sync::oneshot::channel();
        let (release, released) = tokio::sync::oneshot::channel();
        let (finished_send, finished) = tokio::sync::oneshot::channel();
        let tickets = launcher.tickets.clone();
        let receiver = key.scope.receiver.clone();
        let waiter = launcher
            .operations
            .spawn_operation(
                key.clone(),
                || true,
                move |session, cancel| async move {
                    let reservation = tickets.reserve(generation, receiver)?;
                    let _ = started_send.send(());
                    cancel.cancelled().await;
                    let _ = cancelled_send.send(());
                    released.await.map_err(|_| HostError::Core)?;
                    session.close().await.map_err(core_error)?;
                    let _ = finished_send.send(());
                    Ok(reservation)
                },
            )
            .unwrap();
        started.await.unwrap();
        drop(waiter);
        cancelled.await.unwrap();
        assert_eq!(launcher.operations.active_len(), 1);
        assert_eq!(launcher.tickets.snapshot().unwrap().0, 1);
        {
            let state = launcher.operations.state.lock().unwrap();
            let record = state.active.get(&key).unwrap();
            assert!(record.session.is_some());
            assert!(
                matches!(&record.phase, OperationPhase::Running { cancel, .. } if cancel.is_cancelled())
            );
        }
        for id in 2..=crate::transport::MAX_HOST_INFLIGHT as u64 {
            launcher
                .operations
                .register(
                    operation_key(
                        &namespace,
                        generation,
                        "native_task_fixture",
                        id,
                        UiOperationKind::Invoke,
                        None,
                    ),
                    || true,
                )
                .unwrap();
        }
        assert!(matches!(
            launcher.operations.register(
                operation_key(
                    &namespace,
                    generation,
                    "native_task_fixture",
                    5,
                    UiOperationKind::Invoke,
                    None
                ),
                || true
            ),
            Err(HostError::RegistrationFull)
        ));
        release.send(()).unwrap();
        finished.await.unwrap();
        assert_eq!(
            launcher.operations.active_len(),
            crate::transport::MAX_HOST_INFLIGHT - 1
        );
        assert_eq!(launcher.tickets.snapshot().unwrap(), (0, 0));
        launcher.stop(caller()).await.unwrap();
    }

    #[tokio::test]
    async fn successful_response_delivery_keeps_transferred_stream_token_active() {
        let cancel = CancellationToken::new();
        let (send, response) = tokio::sync::oneshot::channel();
        let waiter = OperationWaiter {
            cancel: cancel.clone(),
            cancel_on_drop: true,
            response,
        };
        send.send(Ok(())).unwrap();
        waiter.wait().await.unwrap();
        assert!(!cancel.is_cancelled());
    }

    #[tokio::test]
    async fn actual_core_adapter_has_no_ambient_ui_session_or_origin_fallback() {
        let root = tempfile::tempdir().unwrap();
        let core = Core::open(
            CoreConfig::for_test_data_root(root.path().canonicalize().unwrap()),
            Arc::new(ClosedHost),
        )
        .unwrap();
        let launcher = LauncherCore::from_core(core);
        let namespace = launcher.tickets.namespace().to_owned();
        let valid = r#"{"id":1,"shape":"owner.pack.port@1.0","op":"read","args":{}}"#;
        assert!(matches!(
            launcher
                .invoke(
                    HostCaller {
                        label: "core-host",
                        url: "http://127.0.0.1:8765/"
                    },
                    &namespace,
                    1,
                    "forged_ui",
                    1,
                    valid
                )
                .await,
            Err(HostError::Caller)
        ));
        assert!(matches!(
            launcher
                .invoke(
                    HostCaller {
                        label: "core-host",
                        url: "tauri://localhost/"
                    },
                    &namespace,
                    1,
                    "forged_ui",
                    1,
                    valid
                )
                .await,
            Err(HostError::Cancelled)
        ));
        assert!(matches!(
            launcher
                .start(
                    HostCaller {
                        label: "core-host",
                        url: "tauri://localhost/"
                    },
                    "../profile"
                )
                .await,
            Err(HostError::Invalid)
        ));
        assert!(
            launcher
                .start(
                    HostCaller {
                        label: "core-host",
                        url: "tauri://localhost/"
                    },
                    "missing-profile"
                )
                .await
                .is_err()
        );
        launcher
            .stop(HostCaller {
                label: "core-host",
                url: "tauri://localhost/",
            })
            .await
            .unwrap();
        assert!(matches!(
            launcher
                .stream_pull(
                    HostCaller {
                        label: "core-host",
                        url: "tauri://localhost/"
                    },
                    &namespace,
                    1,
                    "forged_ui",
                    1,
                    1,
                    32
                )
                .await,
            Err(HostError::Cancelled)
        ));
    }

    #[test]
    fn registered_pull_cancel_before_claim_creates_no_ack_ticket_or_core_effect() {
        let tickets = crate::transport::TransportTickets::default();
        let registry = OperationRegistry::default();
        let key = operation_key(
            tickets.namespace(),
            12,
            "frame_pending",
            17,
            UiOperationKind::StreamPull,
            Some(99),
        );
        registry.register(key.clone(), || true).unwrap();
        registry.cancel(&key, || true).unwrap();
        assert!(matches!(
            registry.claim(&key, || true),
            Err(HostError::Cancelled)
        ));
        assert_eq!(registry.active_len(), 0);
        assert_eq!(tickets.snapshot().unwrap(), (0, 0));
    }

    #[test]
    fn registration_is_idempotent_bounded_global_and_never_replays_a_claim() {
        let registry = OperationRegistry::default();
        let namespace = format!("sha256:{}", "a".repeat(64));
        let first = operation_key(&namespace, 1, "frame_one", 1, UiOperationKind::Invoke, None);
        let ack = registry.register(first.clone(), || true).unwrap();
        assert_eq!(registry.register(first.clone(), || true).unwrap(), ack);
        assert_eq!(registry.active_len(), 1);
        let cancel = registry.claim(&first, || true).unwrap();
        assert!(matches!(
            registry.claim(&first, || true),
            Err(HostError::Duplicate)
        ));

        for index in 2..=4 {
            let key = operation_key(
                &namespace,
                1,
                &format!("frame_{index}"),
                1,
                UiOperationKind::Invoke,
                None,
            );
            registry.register(key, || true).unwrap();
        }
        let full = operation_key(
            &namespace,
            1,
            "frame_five",
            1,
            UiOperationKind::Invoke,
            None,
        );
        assert!(matches!(
            registry.register(full.clone(), || true),
            Err(HostError::RegistrationFull)
        ));
        assert!(matches!(
            registry.register(full, || true),
            Err(HostError::Duplicate)
        ));
        assert_eq!(registry.active_len(), crate::transport::MAX_HOST_INFLIGHT);

        registry.cancel(&first, || true).unwrap();
        assert!(cancel.is_cancelled());
        assert_eq!(registry.retire(&first).unwrap(), true);
        registry.cancel(&first, || true).unwrap(); // exact late cancel is a no-op
        assert!(matches!(
            registry.register(first, || true),
            Err(HostError::Duplicate)
        ));
    }

    #[test]
    fn lost_registrations_expire_before_admission_but_cancelled_running_keeps_its_slot() {
        let registry = OperationRegistry::default();
        let namespace = format!("sha256:{}", "a".repeat(64));
        let running = operation_key(&namespace, 1, "frame_one", 1, UiOperationKind::Invoke, None);
        registry.register(running.clone(), || true).unwrap();
        let cancel = registry.claim(&running, || true).unwrap();
        registry.cancel(&running, || true).unwrap();

        let max = crate::transport::MAX_HOST_INFLIGHT as u64;
        let mut lost = Vec::new();
        for id in 2..=max {
            let key = operation_key(
                &namespace,
                1,
                "frame_one",
                id,
                UiOperationKind::Invoke,
                None,
            );
            registry.register(key.clone(), || true).unwrap();
            lost.push(key);
        }
        assert_eq!(registry.active_len(), max as usize);
        for key in &lost {
            expire_registration(&registry, key);
        }

        let first_new_id = max + 1;
        registry
            .register(
                operation_key(
                    &namespace,
                    1,
                    "frame_one",
                    first_new_id,
                    UiOperationKind::Invoke,
                    None,
                ),
                || true,
            )
            .unwrap();
        assert_eq!(registry.active_len(), 2);
        assert!(cancel.is_cancelled());
        assert!(matches!(
            &registry
                .state
                .lock()
                .unwrap()
                .active
                .get(&running)
                .unwrap()
                .phase,
            OperationPhase::Running {
                cancel_requested: true,
                ..
            }
        ));
        for key in lost {
            assert!(matches!(
                registry.register(key.clone(), || true),
                Err(HostError::Duplicate)
            ));
            assert!(matches!(
                registry.claim(&key, || true),
                Err(HostError::Duplicate)
            ));
        }

        for id in first_new_id + 1..first_new_id + max - 1 {
            registry
                .register(
                    operation_key(
                        &namespace,
                        1,
                        "frame_one",
                        id,
                        UiOperationKind::Invoke,
                        None,
                    ),
                    || true,
                )
                .unwrap();
        }
        assert_eq!(registry.active_len(), max as usize);
        assert!(matches!(
            registry.register(
                operation_key(
                    &namespace,
                    1,
                    "frame_one",
                    first_new_id + max - 1,
                    UiOperationKind::Invoke,
                    None,
                ),
                || true,
            ),
            Err(HostError::RegistrationFull)
        ));
        assert!(registry.retire(&running).unwrap());
        assert_eq!(registry.active_len(), max as usize - 1);
    }

    #[test]
    fn repeated_ack_does_not_renew_expiry_and_expired_dispatch_never_starts_work() {
        let registry = Arc::new(OperationRegistry::default());
        let namespace = format!("sha256:{}", "b".repeat(64));
        let key = operation_key(&namespace, 1, "frame_one", 1, UiOperationKind::Invoke, None);
        let ack = registry.register(key.clone(), || true).unwrap();
        let expires_at = match &registry
            .state
            .lock()
            .unwrap()
            .active
            .get(&key)
            .unwrap()
            .phase
        {
            OperationPhase::Registered { expires_at, .. } => *expires_at,
            _ => unreachable!(),
        };
        assert_eq!(registry.register(key.clone(), || true).unwrap(), ack);
        assert!(matches!(
            &registry.state.lock().unwrap().active.get(&key).unwrap().phase,
            OperationPhase::Registered { expires_at: actual, .. } if *actual == expires_at
        ));
        expire_registration(&registry, &key);

        let started = AtomicU64::new(0);
        let result = registry.spawn_operation(
            key.clone(),
            || true,
            |_, _| {
                started.fetch_add(1, Ordering::SeqCst);
                async { Ok::<(), HostError>(()) }
            },
        );
        assert!(matches!(result, Err(HostError::Duplicate)));
        assert_eq!(started.load(Ordering::SeqCst), 0);
        assert_eq!(registry.active_len(), 0);
        assert!(matches!(
            registry.register(key, || true),
            Err(HostError::Duplicate)
        ));
        registry
            .register(
                operation_key(&namespace, 1, "frame_one", 2, UiOperationKind::Invoke, None),
                || true,
            )
            .unwrap();
    }

    #[test]
    fn cancel_sweeps_expired_unclaimed_records_and_keeps_replay_tombstones() {
        let registry = OperationRegistry::default();
        let namespace = format!("sha256:{}", "c".repeat(64));
        let lost = operation_key(&namespace, 1, "frame_one", 1, UiOperationKind::Invoke, None);
        let running = operation_key(&namespace, 1, "frame_two", 1, UiOperationKind::Invoke, None);
        registry.register(lost.clone(), || true).unwrap();
        registry.register(running.clone(), || true).unwrap();
        let cancel = registry.claim(&running, || true).unwrap();
        expire_registration(&registry, &lost);

        registry.cancel(&lost, || true).unwrap();
        assert_eq!(registry.active_len(), 1);
        assert_eq!(
            registry.state.lock().unwrap().highwater.get(&lost.scope),
            Some(&lost.id)
        );
        assert!(matches!(
            registry.claim(&lost, || true),
            Err(HostError::Duplicate)
        ));
        assert!(!cancel.is_cancelled());
        registry.cancel(&running, || true).unwrap();
        assert!(cancel.is_cancelled());
        assert_eq!(registry.active_len(), 1);
    }

    #[test]
    fn foreign_namespace_generation_receiver_and_kind_cannot_cancel_an_active_record() {
        let registry = OperationRegistry::default();
        let namespace_a = format!("sha256:{}", "a".repeat(64));
        let namespace_b = format!("sha256:{}", "b".repeat(64));
        let key = operation_key(
            &namespace_a,
            7,
            "frame_alpha",
            11,
            UiOperationKind::StreamPull,
            Some(4),
        );
        registry.register(key.clone(), || true).unwrap();
        let cancel = registry.claim(&key, || true).unwrap();
        for foreign in [
            operation_key(
                &namespace_b,
                7,
                "frame_alpha",
                11,
                UiOperationKind::StreamPull,
                Some(4),
            ),
            operation_key(
                &namespace_a,
                8,
                "frame_alpha",
                11,
                UiOperationKind::StreamPull,
                Some(4),
            ),
            operation_key(
                &namespace_a,
                7,
                "frame_beta",
                11,
                UiOperationKind::StreamPull,
                Some(4),
            ),
            operation_key(
                &namespace_a,
                7,
                "frame_alpha",
                11,
                UiOperationKind::StreamPull,
                Some(5),
            ),
            operation_key(
                &namespace_a,
                7,
                "frame_alpha",
                11,
                UiOperationKind::StreamCancel,
                Some(4),
            ),
        ] {
            assert!(registry.cancel(&foreign, || true).is_err());
            assert!(!cancel.is_cancelled());
        }
        registry.cancel(&key, || true).unwrap();
        assert!(cancel.is_cancelled());
        assert_eq!(registry.retire(&key).unwrap(), true);
    }

    #[test]
    fn detach_and_generation_retirement_cancel_running_but_keep_its_custody() {
        let registry = OperationRegistry::default();
        let namespace = format!("sha256:{}", "c".repeat(64));
        let unclaimed = operation_key(
            &namespace,
            3,
            "frame_alpha",
            1,
            UiOperationKind::Invoke,
            None,
        );
        let running = operation_key(
            &namespace,
            3,
            "frame_alpha",
            2,
            UiOperationKind::Invoke,
            None,
        );
        registry.register(unclaimed.clone(), || true).unwrap();
        registry.register(running.clone(), || true).unwrap();
        let cancel = registry.claim(&running, || true).unwrap();
        let scope = running.scope.clone();
        registry.retire_receiver(&scope).unwrap();
        assert!(cancel.is_cancelled());
        assert!(matches!(
            registry.cancel(&running, || true),
            Err(HostError::Cancelled)
        ));
        assert_eq!(registry.active_len(), 1);
        assert!(matches!(
            registry.claim(&unclaimed, || true),
            Err(HostError::Cancelled)
        ));
        assert_eq!(registry.retire(&running).unwrap(), true);
        assert_eq!(registry.active_len(), 0);

        let old_running = operation_key(
            &namespace,
            4,
            "frame_beta",
            1,
            UiOperationKind::Invoke,
            None,
        );
        registry.register(old_running.clone(), || true).unwrap();
        let old_cancel = registry.claim(&old_running, || true).unwrap();
        registry
            .retire_generation(&namespace, 4, || Ok(()))
            .unwrap();
        assert!(old_cancel.is_cancelled());
        assert!(matches!(
            registry.cancel(&old_running, || false),
            Err(HostError::Cancelled)
        ));
        assert_eq!(registry.active_len(), 1);
        assert_eq!(registry.retire(&old_running).unwrap(), true);
    }

    #[test]
    fn lifecycle_retirement_races_start_publication_without_leaving_a_live_generation() {
        let registry = Arc::new(OperationRegistry::default());
        let lifecycle = Arc::new(AtomicU64::new(1));
        let active_generation = Arc::new(AtomicU64::new(0));
        let barrier = Arc::new(Barrier::new(2));

        let publish_registry = registry.clone();
        let publish_lifecycle = lifecycle.clone();
        let publish_generation = active_generation.clone();
        let publish_barrier = barrier.clone();
        let publish = std::thread::spawn(move || {
            publish_barrier.wait();
            publish_registry.commit_if(
                || publish_lifecycle.load(Ordering::SeqCst) == 1,
                || publish_generation.store(4, Ordering::SeqCst),
            )
        });

        let retire_registry = registry.clone();
        let retire_lifecycle = lifecycle.clone();
        let retire_generation = active_generation.clone();
        let retire_barrier = barrier.clone();
        let retire = std::thread::spawn(move || {
            retire_barrier.wait();
            retire_registry.retire_active_generation(
                "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                || retire_generation.load(Ordering::SeqCst),
                |generation| {
                    retire_lifecycle.fetch_add(1, Ordering::SeqCst);
                    if retire_generation.load(Ordering::SeqCst) == generation {
                        retire_generation.store(0, Ordering::SeqCst);
                    }
                    Ok(())
                },
            )
        });

        let publish_result = publish.join().unwrap();
        let retired_generation = retire.join().unwrap().unwrap();
        assert!(publish_result.is_ok() || matches!(publish_result, Err(HostError::Cancelled)));
        assert!(retired_generation == 0 || retired_generation == 4);
        assert_eq!(lifecycle.load(Ordering::SeqCst), 2);
        assert_eq!(active_generation.load(Ordering::SeqCst), 0);
    }

    #[test]
    fn cancel_claim_race_never_loses_the_registered_cancellation() {
        let registry = Arc::new(OperationRegistry::default());
        let namespace = format!("sha256:{}", "a".repeat(64));
        for id in 1..=32 {
            let key = operation_key(
                &namespace,
                9,
                "frame_race",
                id,
                UiOperationKind::Invoke,
                None,
            );
            registry.register(key.clone(), || true).unwrap();
            let barrier = Arc::new(Barrier::new(2));
            let claim_registry = registry.clone();
            let claim_key = key.clone();
            let claim_barrier = barrier.clone();
            let claim = std::thread::spawn(move || {
                claim_barrier.wait();
                claim_registry.claim(&claim_key, || true)
            });
            barrier.wait();
            registry.cancel(&key, || true).unwrap();
            match claim.join().unwrap() {
                Ok(token) => {
                    assert!(token.is_cancelled());
                    assert!(registry.retire(&key).unwrap());
                }
                Err(HostError::Cancelled) => assert_eq!(registry.active_len(), 0),
                other => panic!("unexpected claim result: {other:?}"),
            }
        }
    }
}
