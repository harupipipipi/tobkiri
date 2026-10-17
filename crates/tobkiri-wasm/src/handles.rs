use anyhow::{Result, bail};
use std::{
    collections::BTreeMap,
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
};
use tobkiri_canon::ids::ShapeRef;
use tobkiri_flow::CoreStream;
use tobkiri_gate::{CallCx, DispatchPermit, GrantId, Outcome};

#[derive(Clone, Copy)]
pub(crate) enum HandleCleanupError {
    CompletionUnknown,
    FilesystemCommitUnknown,
}

#[derive(Default)]
pub(crate) struct HandleCloseReceipt {
    pub(crate) cleanup_started: bool,
    pub(crate) cleanup_done: bool,
    // A closed native classification survives delivery retries without
    // retaining diagnostics or turning an uncertain mutation into success.
    pub(crate) cleanup_error: Option<HandleCleanupError>,
    pub(crate) outcome: Option<Outcome>,
    // Deadline settlement is a separate immutable terminal family. It must
    // survive a failed audit delivery and retry the same durable CAS winner.
    pub(crate) deadline_settlement: bool,
    pub(crate) delivered: bool,
}

pub(crate) struct HandleLease {
    pub(crate) grant: Option<GrantId>,
    pub(crate) permit: Option<DispatchPermit>,
    closed: AtomicBool,
    output: AtomicU64,
    pub(crate) close: tokio::sync::Mutex<HandleCloseReceipt>,
}
impl HandleLease {
    fn new(permit: Option<DispatchPermit>) -> Self {
        Self {
            grant: permit.as_ref().map(|permit| permit.id().clone()),
            permit,
            closed: AtomicBool::new(false),
            output: AtomicU64::new(0),
            close: tokio::sync::Mutex::new(HandleCloseReceipt::default()),
        }
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
        stream: CoreStream,
    ) -> Result<u64> {
        self.insert(
            HandleKind::Stream,
            cx.clone(),
            0,
            cx,
            authority,
            Arc::new(HandleLease::new(None)),
            Some(Arc::new(tokio::sync::Mutex::new(stream))),
            None,
            None,
            None,
        )
    }
    pub(crate) fn alias(&mut self, entry: HandleEntry, recipient: CallCx) -> Result<u64> {
        self.insert(
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
        )
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
}
