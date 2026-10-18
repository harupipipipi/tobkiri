use anyhow::{Context, Result, bail};
use std::{
    collections::BTreeMap,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
};
use tobkiri_canon::ids::ShapeRef;
use tobkiri_flow::CoreStream;
use tobkiri_gate::{CallCx, DispatchPermit, GrantId, Outcome};

#[derive(Clone, Debug)]
pub(crate) enum SelectedCloseCause {
    Manual,
    Shutdown,
    ProducerFailure(&'static str),
    Deadline,
    Revoked,
    Retired,
    Unknown,
    Terminal(Outcome),
}

#[derive(Default)]
pub(crate) struct HandleCloseReceipt {
    pub(crate) cleanup_started: bool,
    pub(crate) cleanup_done: bool,
    pub(crate) cleanup_error: Option<&'static str>,
    // Copy-only closed classifications from the exact native source receipt.
    // Physical Unknown stays in cleanup_error; no guest message is cloned.
    pub(crate) source_report: Option<tobkiri_flow::StreamCloseReport>,
    pub(crate) close_report_confirmed: bool,
    pub(crate) outcome: Option<Outcome>,
    // Deadline settlement is a separate immutable terminal family. It must
    // survive a failed audit delivery and retry the same durable CAS winner.
    pub(crate) deadline_settlement: bool,
    pub(crate) delivered: bool,
}

/// Original native close intent, published synchronously before any wake.
/// The first intent wins and is provenance metadata only — it can never
/// select a cause itself; only the actual Gate snapshot callback does.
#[derive(Clone, Copy, Debug)]
pub(crate) struct PendingIntent {
    pub(crate) reason: tobkiri_flow::StreamCloseReason,
    pub(crate) failure: Option<&'static str>,
    /// Owner cancel state captured before this intent existed; a later wake
    /// cannot rewrite the original manual-intent provenance into Unknown.
    pub(crate) wake_before_intent: bool,
}

pub(crate) struct HandleLease {
    pub(crate) grant: Option<GrantId>,
    pub(crate) permit: Option<DispatchPermit>,
    closed: AtomicBool,
    output: AtomicU64,
    pub(crate) close: tokio::sync::Mutex<HandleCloseReceipt>,
    pub(crate) cause: Mutex<Option<SelectedCloseCause>>,
    pub(crate) cause_uncertain: AtomicBool,
    /// Published while an authenticated pre-callback busy retry may still be
    /// outstanding for this lease's single cleanup episode. An empty cause
    /// slot observed under a set pending flag is unconfirmed — never a
    /// settled unknown. Cleared only when a cause is actually selected or a
    /// post-callback fault latches uncertainty.
    pub(crate) cause_pending: AtomicBool,
    /// Original native close intent (first wins), published synchronously
    /// before any cancellation wake.
    pub(crate) pending_intent: Mutex<Option<PendingIntent>>,
    /// Original cleanup episode bound, initialized once by the first
    /// selecting/observing owner; later observers join the same window.
    cleanup_window: std::sync::OnceLock<std::time::Instant>,
    _native_registration: Option<tobkiri_gate::ControlReservation>,
}
impl HandleLease {
    fn new(permit: Option<DispatchPermit>) -> Self {
        Self {
            grant: permit.as_ref().map(|permit| permit.id().clone()),
            permit,
            closed: AtomicBool::new(false),
            output: AtomicU64::new(0),
            close: tokio::sync::Mutex::new(HandleCloseReceipt::default()),
            cause: Mutex::new(None),
            cause_uncertain: AtomicBool::new(false),
            cause_pending: AtomicBool::new(false),
            pending_intent: Mutex::new(None),
            cleanup_window: std::sync::OnceLock::new(),
            _native_registration: None,
        }
    }
    pub(crate) fn native(registration: tobkiri_gate::ControlReservation) -> Self {
        let mut lease = Self::new(None);
        lease._native_registration = Some(registration);
        lease
    }
    /// The single shared cleanup-episode bound for this lease: the first
    /// caller initializes it to min(owner effect deadline, now + 2s) before
    /// any wake; every later observer joins the same window instead of
    /// restarting it.
    pub(crate) fn cleanup_episode_bound(
        &self,
        owner_deadline: std::time::Instant,
    ) -> std::time::Instant {
        *self.cleanup_window.get_or_init(|| {
            (std::time::Instant::now() + std::time::Duration::from_secs(2)).min(owner_deadline)
        })
    }
    /// Publish the original close intent — first intent wins — and mark the
    /// selection pending. Runs synchronously before any cancellation wake so
    /// an empty cause slot reads as unconfirmed, never a settled unknown.
    /// Poisoned intent state is a distinct failure: it is never busy.
    pub(crate) fn publish_pending_intent(
        &self,
        reason: tobkiri_flow::StreamCloseReason,
        failure: Option<&'static str>,
        wake_before_intent: bool,
    ) -> Result<PendingIntent, ()> {
        let mut pending = self.pending_intent.lock().map_err(|_| ())?;
        let intent = *pending.get_or_insert(PendingIntent {
            reason,
            failure,
            wake_before_intent,
        });
        self.cause_pending.store(true, Ordering::Release);
        Ok(intent)
    }
    pub(crate) fn begin_close(&self) {
        // Close liveness immediately; durable completion is independently
        // latched only after same-proposal delivery succeeds.
        self.closed.store(true, Ordering::Release);
    }
    pub(crate) fn reserve_output(&self, bytes: u64, limit: u64) -> Result<()> {
        self.output
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |used| {
                used.checked_add(bytes).filter(|sum| *sum <= limit)
            })
            .map_err(|_| tobkiri_gate::GateError::Limit("handle output budget exceeded".into()))?;
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum HandleKind {
    Stream,
    Callback,
}

/// Canonical empty terminal pair prepaid under the original source CodeOwner
/// while its actual source frame is still active. Cloning either payload
/// shares the same funded storage; serving the cache allocates nothing.
#[derive(Clone)]
pub(crate) struct TerminalEmptyCache {
    pub(crate) batch: tobkiri_canon::CoreValue,
    pub(crate) buffer: tobkiri_canon::CoreBuffer,
}

/// Mapping between a provider-local handle and its permitted recipient.
#[derive(Clone)]
pub struct HandleEntry {
    pub kind: HandleKind,
    pub owner: CallCx,
    pub local_id: u64,
    pub authority: ShapeRef,
    recipient: CallCx,
    pub(crate) lease: Arc<HandleLease>,
    pub(crate) source: Option<Arc<tokio::sync::Mutex<CoreStream>>>,
    pub(crate) generation: Option<u64>,
    pub(crate) source_frame: Option<Arc<dyn crate::ProviderFrame>>,
    pub(crate) source_version: Option<&'static str>,
    pub(crate) close_control: Option<Arc<dyn tobkiri_flow::StreamCloseControl>>,
    /// The same Arc as `close_control` in its concrete type, for the private
    /// typed selection path used by the existing async cleanup owner. Built
    /// from an entry clone before binding: no entry->selector->entry cycle.
    pub(crate) close_selector: Option<Arc<crate::host::ComponentCloseControl>>,
    pub(crate) native_children: Option<Arc<AtomicBool>>,
    pub(crate) store_disposal: Option<Arc<crate::host::StoreDisposal>>,
    pub(crate) terminal_empty: Option<TerminalEmptyCache>,
    pub(crate) operation: Option<crate::ProviderOperationScope>,
    pub(crate) physical_completion: Option<tobkiri_flow::StreamPhysicalCompletion>,
}
impl HandleEntry {
    pub(crate) fn recipient_matches(&self, cx: &CallCx) -> bool {
        same_scope(&self.recipient, cx)
    }
}

/// Handles are request/Profile/principal scoped and never guest resource IDs.
pub struct HandleTable {
    next: u64,
    capacity: usize,
    entries: BTreeMap<u64, HandleEntry>,
}
impl HandleTable {
    pub fn new(capacity: usize) -> Self {
        Self {
            next: 1,
            capacity,
            entries: BTreeMap::new(),
        }
    }

    // Source, recipient, authority, grant and generation stay explicit so an
    // alias cannot accidentally drop a security binding.
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn register_authorized(
        &mut self,
        kind: HandleKind,
        owner: CallCx,
        local_id: u64,
        recipient: CallCx,
        authority: ShapeRef,
        permit: DispatchPermit,
        generation: u64,
        frame: Arc<dyn crate::ProviderFrame>,
        version: &'static str,
    ) -> Result<u64> {
        self.insert(
            kind,
            owner,
            local_id,
            recipient,
            authority,
            Arc::new(HandleLease::new(Some(permit))),
            None,
            Some(generation),
            Some(frame),
            Some(version),
        )
    }
    pub(crate) fn register_core(
        &mut self,
        cx: CallCx,
        authority: ShapeRef,
        stream: Arc<tokio::sync::Mutex<CoreStream>>,
        lease: Arc<HandleLease>,
    ) -> Result<u64> {
        self.insert(
            HandleKind::Stream,
            cx.clone(),
            0,
            cx,
            authority,
            lease,
            Some(stream),
            None,
            None,
            None,
        )
    }
    pub(crate) fn alias(&mut self, entry: HandleEntry, recipient: CallCx) -> Result<u64> {
        let control = entry.close_control.clone();
        let selector = entry.close_selector.clone();
        let children = entry.native_children.clone();
        let disposal = entry.store_disposal.clone();
        let terminal_empty = entry.terminal_empty.clone();
        let operation = entry.operation.clone();
        let physical_completion = entry.physical_completion.clone();
        let id = self.insert(
            entry.kind,
            entry.owner,
            entry.local_id,
            recipient,
            entry.authority,
            entry.lease,
            entry.source,
            entry.generation,
            entry.source_frame,
            entry.source_version,
        )?;
        let alias = self.entries.get_mut(&id).expect("inserted alias");
        alias.close_control = control;
        alias.close_selector = selector;
        alias.native_children = children;
        alias.store_disposal = disposal;
        alias.terminal_empty = terminal_empty;
        alias.operation = operation;
        alias.physical_completion = physical_completion;
        Ok(id)
    }
    pub(crate) fn bind_cleanup(
        &mut self,
        id: u64,
        control: Arc<dyn tobkiri_flow::StreamCloseControl>,
        children: Arc<AtomicBool>,
        disposal: Arc<crate::host::StoreDisposal>,
    ) -> Result<()> {
        let entry = self
            .entries
            .get_mut(&id)
            .context("cleanup handle disappeared")?;
        entry.close_control = Some(control);
        entry.native_children = Some(children);
        entry.store_disposal = Some(disposal);
        Ok(())
    }
    /// Attach the concrete typed selector sharing the cleanup control Arc.
    /// The selector was constructed from an entry clone before binding, so
    /// no entry->selector->entry strong cycle can exist.
    pub(crate) fn bind_cleanup_selector(
        &mut self,
        id: u64,
        selector: Arc<crate::host::ComponentCloseControl>,
    ) -> Result<()> {
        self.entries
            .get_mut(&id)
            .context("cleanup selector handle disappeared")?
            .close_selector = Some(selector);
        Ok(())
    }
    /// Attach the prefunded canonical empty terminal pair to an already
    /// registered stream lease. Only the original live source frame may fund
    /// it; the stored value never mints later allocation or new authority.
    pub(crate) fn bind_terminal_empty(&mut self, id: u64, cache: TerminalEmptyCache) -> Result<()> {
        let entry = self
            .entries
            .get_mut(&id)
            .context("terminal-empty handle disappeared")?;
        if entry.kind != HandleKind::Stream {
            bail!("terminal empty cache is only valid for stream handles")
        }
        entry.terminal_empty = Some(cache);
        Ok(())
    }
    pub(crate) fn bind_operation(
        &mut self,
        id: u64,
        operation: crate::ProviderOperationScope,
        physical: tobkiri_flow::StreamPhysicalCompletion,
    ) -> Result<()> {
        let entry = self
            .entries
            .get_mut(&id)
            .context("operation handle disappeared")?;
        entry.operation = Some(operation);
        entry.physical_completion = Some(physical);
        Ok(())
    }
    pub(crate) fn bind_native_physical(
        &mut self,
        id: u64,
        physical: Option<tobkiri_flow::StreamPhysicalCompletion>,
    ) -> Result<()> {
        self.entries
            .get_mut(&id)
            .context("native handle disappeared")?
            .physical_completion = physical;
        Ok(())
    }
    // Internal insertion preserves every independently checked handle binding.
    #[allow(clippy::too_many_arguments)]
    fn insert(
        &mut self,
        kind: HandleKind,
        owner: CallCx,
        local_id: u64,
        recipient: CallCx,
        authority: ShapeRef,
        lease: Arc<HandleLease>,
        source: Option<Arc<tokio::sync::Mutex<CoreStream>>>,
        generation: Option<u64>,
        source_frame: Option<Arc<dyn crate::ProviderFrame>>,
        source_version: Option<&'static str>,
    ) -> Result<u64> {
        owner.check_active()?;
        recipient.check_active()?;
        if self.entries.len() >= self.capacity {
            bail!("core handle quota exceeded")
        }
        let id = self.next;
        self.next = id
            .checked_add(1)
            .ok_or_else(|| anyhow::anyhow!("handle IDs exhausted"))?;
        self.entries.insert(
            id,
            HandleEntry {
                kind,
                owner,
                local_id,
                recipient,
                authority,
                lease,
                source,
                generation,
                source_frame,
                source_version,
                close_control: None,
                close_selector: None,
                native_children: None,
                store_disposal: None,
                terminal_empty: None,
                operation: None,
                physical_completion: None,
            },
        );
        Ok(id)
    }

    pub fn resolve(&self, id: u64, kind: HandleKind, cx: &CallCx) -> Result<HandleEntry> {
        cx.check_active()?;
        let entry = self
            .entries
            .get(&id)
            .ok_or_else(|| anyhow::anyhow!("unknown core handle"))?;
        if entry.kind != kind || !same_scope(&entry.recipient, cx) {
            bail!("core handle is outside the caller's scope")
        }
        if entry.lease.closed.load(Ordering::Acquire) {
            bail!("core handle is closed")
        }
        entry.owner.check_active()?;
        Ok(entry.clone())
    }

    pub(crate) fn native_input_capture_bytes(
        &self,
        handles: &[(HandleKind, u64)],
        cx: &CallCx,
    ) -> Result<usize> {
        cx.check_active()?;
        handles.iter().try_fold(0usize, |bytes, (kind, id)| {
            let entry = self
                .entries
                .get(id)
                .context("unknown Input source handle")?;
            if entry.kind != *kind
                || !same_scope(&entry.recipient, cx)
                || entry.lease.closed.load(Ordering::Acquire)
            {
                bail!("Input handle is outside its original recipient scope")
            }
            entry.owner.check_active()?;
            bytes
                .checked_add(entry.authority.clone_storage_bytes())
                .context("Input source metadata overflow")
        })
    }

    pub fn remove(&mut self, id: u64, kind: HandleKind, cx: &CallCx) -> Result<HandleEntry> {
        // Cancellation may revoke an otherwise valid recipient; identity must
        // still be checked so that dropping its own handle can release effects.
        let entry = self
            .entries
            .get(&id)
            .ok_or_else(|| anyhow::anyhow!("unknown core handle"))?;
        if entry.kind != kind || !same_scope(&entry.recipient, cx) {
            bail!("core handle is outside the caller's scope")
        }
        let entry = entry.clone();
        self.entries
            .retain(|_, other| !Arc::ptr_eq(&entry.lease, &other.lease));
        Ok(entry)
    }
    /// Only a native wrapper holding the original scoped entry can make a
    /// completed cleanup idempotent after table removal. Unknown guest IDs
    /// still fail through ordinary remove/resolve.
    pub(crate) fn remove_captured(
        &mut self,
        id: u64,
        cx: &CallCx,
        captured: &HandleEntry,
    ) -> Result<()> {
        if !captured.recipient_matches(cx) {
            bail!("captured handle scope mismatch")
        }
        if let Some(actual) = self.entries.get(&id) {
            if actual.kind != captured.kind || !Arc::ptr_eq(&actual.lease, &captured.lease) {
                bail!("captured handle identity mismatch")
            }
            self.remove(id, captured.kind, cx)?;
        }
        Ok(())
    }
    pub(crate) fn resolve_cleanup(
        &self,
        id: u64,
        kind: HandleKind,
        cx: &CallCx,
    ) -> Result<HandleEntry> {
        let entry = self
            .entries
            .get(&id)
            .ok_or_else(|| anyhow::anyhow!("unknown core handle"))?;
        if entry.kind != kind || !same_scope(&entry.recipient, cx) {
            bail!("core handle is outside the caller's scope")
        }
        Ok(entry.clone())
    }
}

fn same_scope(a: &CallCx, b: &CallCx) -> bool {
    a.profile() == b.profile()
        && a.principal() == b.principal()
        && a.request() == b.request()
        && a.security_epoch() == b.security_epoch()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{Duration, Instant};
    use tobkiri_canon::{
        Digest,
        ids::{PackHash, PackId, ProfileId, RequestId},
    };
    use tobkiri_gate::CoreCancellation as CancellationToken;
    use tobkiri_gate::{Principal, ResourceLimits};

    fn cx(profile: &str, pack: &str, request: &str) -> CallCx {
        CallCx::root(
            ProfileId::parse(profile).unwrap(),
            Principal::Pack {
                pack: PackId::parse(pack).unwrap(),
                hash: PackHash::new(Digest::hash(pack.as_bytes())),
            },
            RequestId::parse(request).unwrap(),
            CancellationToken::new(),
            Instant::now() + Duration::from_secs(5),
            ResourceLimits {
                fuel_per_call: 100000,
                memory_mib: 8,
                wall_ms: 5000,
                table_elements: 1024,
                output_bytes: 1024,
            },
        )
    }
    fn cx_deadline(profile: &str, pack: &str, request: &str, deadline: Instant) -> CallCx {
        CallCx::root(
            ProfileId::parse(profile).unwrap(),
            Principal::Pack {
                pack: PackId::parse(pack).unwrap(),
                hash: PackHash::new(Digest::hash(pack.as_bytes())),
            },
            RequestId::parse(request).unwrap(),
            CancellationToken::new(),
            deadline,
            ResourceLimits {
                fuel_per_call: 100000,
                memory_mib: 8,
                wall_ms: 5000,
                table_elements: 1024,
                output_bytes: 1024,
            },
        )
    }
    fn entry(owner_deadline: Instant) -> HandleEntry {
        HandleEntry {
            kind: HandleKind::Stream,
            owner: cx_deadline("profile-a", "local.owner", "owner-request", owner_deadline),
            local_id: 1,
            authority: ShapeRef::parse("local.contract.events@1.0").unwrap(),
            recipient: cx("profile-a", "local.recipient", "recipient-request"),
            lease: Arc::new(HandleLease::new(None)),
            source: None,
            generation: None,
            source_frame: None,
            source_version: None,
            close_control: None,
            close_selector: None,
            native_children: None,
            store_disposal: None,
            terminal_empty: None,
            operation: None,
            physical_completion: None,
        }
    }
    #[test]
    fn handles_cannot_cross_request_profile_principal_kind_or_cancellation_scope() {
        let owner = cx("profile-a", "local.owner", "owner-request");
        let recipient = cx("profile-a", "local.recipient", "recipient-request");
        let mut table = HandleTable::new(1);
        let authority = ShapeRef::parse("local.contract.events@1.0").unwrap();
        let id = table
            .insert(
                HandleKind::Stream,
                owner.clone(),
                777,
                recipient.clone(),
                authority.clone(),
                Arc::new(HandleLease::new(None)),
                None,
                None,
                None,
                None,
            )
            .unwrap();
        assert_eq!(
            table
                .resolve(id, HandleKind::Stream, &recipient)
                .unwrap()
                .local_id,
            777
        );
        for other in [
            cx("profile-b", "local.recipient", "recipient-request"),
            cx("profile-a", "local.recipient", "another-request"),
            cx("profile-a", "local.other", "recipient-request"),
        ] {
            assert!(table.resolve(id, HandleKind::Stream, &other).is_err());
        }
        assert!(table.resolve(id, HandleKind::Callback, &recipient).is_err());
        recipient.cancel().cancel();
        assert!(table.resolve(id, HandleKind::Stream, &recipient).is_err());
        assert_eq!(
            table
                .remove(id, HandleKind::Stream, &recipient)
                .unwrap()
                .local_id,
            777
        );
        assert!(table.resolve(id, HandleKind::Stream, &recipient).is_err());
    }
    #[test]
    fn cleanup_episode_is_initialized_once_and_bounded_by_owner_deadline() {
        let lease = HandleLease::new(None);
        let soon = Instant::now() + Duration::from_millis(50);
        let bound = lease.cleanup_episode_bound(soon);
        assert_eq!(bound, soon);
        // Later observers join the same window; a later deadline cannot
        // restart or widen the original episode.
        let later = Instant::now() + Duration::from_secs(60);
        assert_eq!(lease.cleanup_episode_bound(later), bound);
        let lease = HandleLease::new(None);
        let far = Instant::now() + Duration::from_secs(60);
        let bound = lease.cleanup_episode_bound(far);
        assert!(bound <= Instant::now() + Duration::from_secs(2));
        assert!(bound <= far);
        assert_eq!(lease.cleanup_episode_bound(soon), bound);
    }
    #[test]
    fn pending_intent_is_published_once_and_preserves_original_provenance() {
        let lease = HandleLease::new(None);
        assert!(!lease.cause_pending.load(Ordering::Acquire));
        let first = lease
            .publish_pending_intent(tobkiri_flow::StreamCloseReason::Manual, None, false)
            .unwrap();
        assert!(lease.cause_pending.load(Ordering::Acquire));
        assert_eq!(first.reason, tobkiri_flow::StreamCloseReason::Manual);
        assert!(!first.wake_before_intent);
        // A second intent never rewrites the original reason or its
        // manual-before-wake provenance.
        let second = lease
            .publish_pending_intent(
                tobkiri_flow::StreamCloseReason::Retired,
                Some("producer-failure"),
                true,
            )
            .unwrap();
        assert_eq!(second.reason, tobkiri_flow::StreamCloseReason::Manual);
        assert!(second.failure.is_none());
        assert!(!second.wake_before_intent);
    }
    #[test]
    fn poisoned_pending_intent_is_uncertain_never_busy() {
        let lease = HandleLease::new(None);
        let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let _guard = lease.pending_intent.lock().unwrap();
            panic!("poison pending intent");
        }));
        assert!(
            lease
                .publish_pending_intent(tobkiri_flow::StreamCloseReason::Manual, None, false)
                .is_err()
        );
        assert!(!lease.cause_pending.load(Ordering::Acquire));
    }
    #[tokio::test]
    async fn pending_cause_observation_is_unconfirmed_never_settled_unknown() {
        let entry = entry(Instant::now() + Duration::from_millis(30));
        entry
            .lease
            .publish_pending_intent(tobkiri_flow::StreamCloseReason::Manual, None, false)
            .unwrap();
        // Empty slot + outstanding authenticated busy retry = pending, not
        // absent: observation waits inside the original episode and reports
        // unconfirmed only after the bound.
        let observed = crate::core_imports::observe_cause_bounded(&entry).await;
        assert!(matches!(
            observed,
            crate::core_imports::CauseObservation::Pending
        ));
    }
    #[tokio::test]
    async fn transiently_held_cause_observation_resolves_actual_contents() {
        let entry = entry(Instant::now() + Duration::from_secs(60));
        let lease = entry.lease.clone();
        let (held_tx, held_rx) = std::sync::mpsc::sync_channel(0);
        let releaser = std::thread::spawn(move || {
            let guard = lease.cause.lock().unwrap();
            held_tx.send(()).unwrap();
            std::thread::sleep(Duration::from_millis(10));
            drop(guard);
        });
        held_rx.recv().unwrap();
        let observed = crate::core_imports::observe_cause_bounded(&entry).await;
        releaser.join().unwrap();
        assert!(matches!(
            observed,
            crate::core_imports::CauseObservation::Observed(None)
        ));
    }
    #[tokio::test]
    async fn poisoned_cause_observation_is_uncertain_not_retryable() {
        let entry = entry(Instant::now() + Duration::from_secs(60));
        let lease = entry.lease.clone();
        let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let _guard = lease.cause.lock().unwrap();
            panic!("poison cause");
        }));
        let observed = crate::core_imports::observe_cause_bounded(&entry).await;
        assert!(matches!(
            observed,
            crate::core_imports::CauseObservation::Uncertain
        ));
    }
}
