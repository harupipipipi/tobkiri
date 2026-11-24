//! Owned audit event codec: stored StrictV1 event-body schema validation and
//! the legacy-compatible unsigned-row digest, computed only from owned,
//! caller-funded data. No SQL, routing, or authority lives here.
//!
//! The fixed Store scanner uses these owned components for both full-history
//! reads and append transactions. Wasm/Core supervisor wiring remains separate.

use super::{integrity, map_alloc};
use crate::StoreError;
use std::sync::Arc;
use tobkiri_canon::{
    AllocationBudget, AllocationError, AllocationKind, AllocationRequest, CoreObject, CoreValue,
    Digest, MAX_SAFE_INTEGER,
    ids::{InputSourceId, PackHash, PackId, ProfileId, RequestId, UiSessionId},
};
use tobkiri_gate::Principal;

#[cfg(test)]
use std::sync::atomic::{AtomicU64, Ordering};

/// Stored-data faults are Integrity; owner resource failures keep their real
/// class. In this adapter closure the Canon lease rejects depth > MAX_DEPTH
/// before consulting the budget and the pinned production budget never emits
/// Invalid/Depth for valid-depth work, so both mean corrupt persisted data
/// here — never generalized to arbitrary budgets.
fn stored_fault(error: AllocationError) -> StoreError {
    match error {
        AllocationError::Invalid | AllocationError::Depth => integrity(),
        other => map_alloc(other),
    }
}
fn id_fault(error: AllocationError) -> StoreError {
    match error {
        AllocationError::Invalid => integrity(),
        other => map_alloc(other),
    }
}
fn text_len(text: &str) -> Result<u64, StoreError> {
    u64::try_from(text.len()).map_err(|_| StoreError::Quota)
}

/// Zero-byte work charge for borrowed-source traversal: ID/digest spelling
/// scans, object key lookups and typed comparisons. No storage is reserved
/// and none is released.
pub(crate) fn charge_work(
    budget: &Arc<dyn AllocationBudget>,
    elements: u64,
    work: u64,
) -> Result<(), StoreError> {
    budget
        .reserve(AllocationRequest {
            kind: AllocationKind::Parse,
            bytes: 0,
            elements,
            depth: 0,
            work,
        })
        .map_err(stored_fault)
}

/// Grammar-derived work bound for `IdLedger::precharge`: counted full-text
/// passes of the real validator (per-byte class loops plus the fixed
/// substring/reserved-name probes) and the fallible copy pass, with a small
/// fixed bound for scalar checks. Not a blind multiplier.
enum IdGrammar {
    /// component(): up to four reserved-name `eq_ignore_ascii_case` probes
    /// (each bounded by `min(len,4)` bytes once lengths match), the two
    /// 3-byte COM/LPT prefix probes when len==4, the per-byte
    /// alnum/`_`/`-` class loop and the copy pass.
    Component,
    /// pack(): the `split_once('.')` scan plus two kebab() runs — each a
    /// per-byte class loop and a "--" substring scan — and the copy pass.
    Pack,
    /// token(): the per-byte alnum/`._:-` set probe, a ".." substring scan
    /// and the copy pass.
    Token,
}
impl IdGrammar {
    fn work(self, bytes: u64) -> Result<u64, StoreError> {
        let (passes, fixed): (u64, u64) = match self {
            Self::Component => (7, 32),
            Self::Pack => (7, 8),
            Self::Token => (9, 8),
        };
        bytes
            .checked_mul(passes)
            .and_then(|work| work.checked_add(fixed))
            .ok_or(StoreError::Quota)
    }
}

/// Test-only observation point inside `IdLedger::drop`: shares the test
/// budget's live-byte and op-order counters and records them after every
/// funded ID String is already destroyed, before the charge release. It
/// never replaces the real owned Strings or their Drop order.
#[cfg(test)]
pub(crate) struct LedgerProbe {
    track: Arc<tests::Track>,
    pub(crate) live_at_drop: AtomicU64,
    pub(crate) order_at_drop: AtomicU64,
}
#[cfg(test)]
impl LedgerProbe {
    fn new(track: Arc<tests::Track>) -> Self {
        Self {
            track,
            live_at_drop: AtomicU64::new(0),
            order_at_drop: AtomicU64::new(0),
        }
    }
}

/// Byte ledger for simultaneously live typed-ID heap copies. Each String is
/// reserved before `try_parse_precharged` performs the real fallible copy.
/// `Drop` releases the accumulated charge only after every ID String it
/// funded was destroyed: callers declare the ledger before all ID locals and
/// it stays the last field of `OwnedAuditEvent`. No Clone, Arc or heap
/// wrapper.
struct IdLedger {
    budget: Arc<dyn AllocationBudget>,
    bytes: u64,
    #[cfg(test)]
    probe: Option<Arc<LedgerProbe>>,
}
impl IdLedger {
    fn new(budget: &Arc<dyn AllocationBudget>) -> Self {
        Self {
            budget: budget.clone(),
            bytes: 0,
            #[cfg(test)]
            probe: None,
        }
    }
    #[cfg(test)]
    fn new_probed(budget: &Arc<dyn AllocationBudget>, probe: Arc<LedgerProbe>) -> Self {
        Self {
            budget: budget.clone(),
            bytes: 0,
            probe: Some(probe),
        }
    }
    /// Reserve one ID's exact live heap bytes plus the grammar's real
    /// derived work over each source byte, before the fallible copy.
    fn precharge(&mut self, text: &str, grammar: IdGrammar) -> Result<(), StoreError> {
        let bytes = text_len(text)?;
        let work = grammar.work(bytes)?;
        self.budget
            .reserve(AllocationRequest {
                kind: AllocationKind::String,
                bytes,
                elements: 1,
                depth: 0,
                work,
            })
            .map_err(stored_fault)?;
        self.bytes = self.bytes.checked_add(bytes).ok_or(StoreError::Quota)?;
        Ok(())
    }
    /// Exact retained bytes of already validated original Core IDs. The real
    /// precharged clone copies these bytes plus at most one inline PackHash;
    /// it never reruns an allocating validation/error-formatting path.
    fn precharge_copy(&mut self, bytes: usize, inline: u64) -> Result<(), StoreError> {
        let bytes = u64::try_from(bytes).map_err(|_| StoreError::Quota)?;
        let total = self.bytes.checked_add(bytes).ok_or(StoreError::Quota)?;
        let work = bytes
            .checked_add(inline)
            .and_then(|n| n.checked_add(1))
            .ok_or(StoreError::Quota)?;
        self.budget
            .reserve(AllocationRequest {
                kind: AllocationKind::String,
                bytes,
                elements: 1,
                depth: 0,
                work,
            })
            .map_err(map_alloc)?;
        self.bytes = total;
        Ok(())
    }
    /// Inline PackHash spelling check funded as traversal work (no heap).
    /// The borrowed spelling is proven before the real parser runs, so the
    /// invalid branch's fixed CanonError allocation is never reached.
    fn hash(&mut self, text: &str) -> Result<PackHash, StoreError> {
        charge_digest_validation(text, &self.budget)?;
        if !digest_spelling(text) {
            return Err(integrity());
        }
        PackHash::parse(text).map_err(|_| integrity())
    }
}
impl Drop for IdLedger {
    fn drop(&mut self) {
        #[cfg(test)]
        if let Some(probe) = &self.probe {
            probe
                .live_at_drop
                .store(probe.track.live.load(Ordering::Acquire), Ordering::Release);
            probe
                .order_at_drop
                .store(probe.track.order.load(Ordering::Acquire), Ordering::Release);
        }
        if self.bytes != 0 {
            self.budget.release(self.bytes);
        }
    }
}

/// Charged required-key lookup: comparison work is the real binary-search
/// bound for this object's length and key size. A missing key is Integrity.
fn field(
    object: &CoreObject<'_>,
    key: &'static str,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreValue, StoreError> {
    let len = u64::try_from(object.len()).map_err(|_| StoreError::Quota)?;
    let work = u64::from(64 - (len | 1).leading_zeros())
        .checked_mul(text_len(key)?.saturating_add(1))
        .ok_or(StoreError::Quota)?;
    charge_work(budget, len, work)?;
    object.get(key).ok_or_else(integrity)
}
fn text_of(value: &CoreValue) -> Result<&str, StoreError> {
    value.as_str().ok_or_else(integrity)
}

/// Fallible typed-ID copy behind an exact ledger precharge; real grammar and
/// allocation failures keep their class, invalid spellings are Integrity.
fn typed<T>(
    ledger: &mut IdLedger,
    value: &CoreValue,
    grammar: IdGrammar,
    parse: fn(&str) -> Result<T, AllocationError>,
) -> Result<T, StoreError> {
    let text = text_of(value)?;
    ledger.precharge(text, grammar)?;
    parse(text).map_err(id_fault)
}

/// The exact `Digest::parse` accepted spelling over borrowed text: fixed
/// length, `sha256:` prefix and lowercase hex only. Malformed stored text
/// becomes typed Integrity here, before the real parser's invalid branch
/// could allocate its fixed CanonError String.
fn digest_spelling(value: &str) -> bool {
    value.len() == 71
        && value.starts_with("sha256:")
        && value[7..]
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

/// Both spelling validators can each perform four byte probes per source
/// byte (prefix plus digit/range tests); Digest::parse also initializes and
/// copies its inline byte array. Thus 2*4+2 full passes plus fixed scalar
/// checks cover the real valid path, before either validator runs. PackHash
/// delegates to the same Digest parser. Invalid spelling never enters the
/// parser's allocating CanonError branch.
fn charge_digest_validation(
    text: &str,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<(), StoreError> {
    let work = text_len(text)?
        .checked_mul(10)
        .and_then(|work| work.checked_add(32))
        .ok_or(StoreError::Quota)?;
    charge_work(budget, 1, work)
}

/// Owned Digest spelling check over stored row text, funded as traversal
/// work (Digest storage is inline). Malformed text is Integrity.
pub(crate) fn digest_text(
    text: &str,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Digest, StoreError> {
    charge_digest_validation(text, budget)?;
    if !digest_spelling(text) {
        return Err(integrity());
    }
    Digest::parse(text).map_err(|_| integrity())
}

/// One shared five-field event schema for stored validation and the typed
/// append constructor. The caller selects its established allocation mapping.
fn event_value(
    kind: CoreValue,
    payload: CoreValue,
    principal: CoreValue,
    profile: CoreValue,
    request: CoreValue,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreValue, AllocationError> {
    let fields: [Result<(&'static str, CoreValue), AllocationError>; 5] = [
        Ok(("kind", kind)),
        Ok(("payload", payload)),
        Ok(("principal", principal)),
        Ok(("profile", profile)),
        Ok(("request", request)),
    ];
    CoreValue::object_shared(fields.into_iter(), budget)
}

/// Exact serde Principal tags/fields, derived solely from an already typed
/// original Core principal. No payload string can select a principal variant.
fn principal_value(
    principal: &Principal,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreValue, AllocationError> {
    match principal {
        Principal::Pack { pack, hash } => {
            let fields: [Result<(&'static str, CoreValue), AllocationError>; 3] = [
                CoreValue::string("pack", budget).map(|v| ("kind", v)),
                CoreValue::string(pack.as_str(), budget).map(|v| ("pack", v)),
                CoreValue::string(hash.as_str(), budget).map(|v| ("hash", v)),
            ];
            CoreValue::object_shared(fields.into_iter(), budget)
        }
        Principal::PackUi { pack, hash, ui } => {
            let fields: [Result<(&'static str, CoreValue), AllocationError>; 4] = [
                CoreValue::string("pack-ui", budget).map(|v| ("kind", v)),
                CoreValue::string(pack.as_str(), budget).map(|v| ("pack", v)),
                CoreValue::string(hash.as_str(), budget).map(|v| ("hash", v)),
                CoreValue::string(ui.as_str(), budget).map(|v| ("ui", v)),
            ];
            CoreValue::object_shared(fields.into_iter(), budget)
        }
        Principal::Input { source } => {
            let fields: [Result<(&'static str, CoreValue), AllocationError>; 2] = [
                CoreValue::string("input", budget).map(|v| ("kind", v)),
                CoreValue::string(source.as_str(), budget).map(|v| ("source", v)),
            ];
            CoreValue::object_shared(fields.into_iter(), budget)
        }
        Principal::User => {
            let fields: [Result<(&'static str, CoreValue), AllocationError>; 1] =
                [CoreValue::string("user", budget).map(|v| ("kind", v))];
            CoreValue::object_shared(fields.into_iter(), budget)
        }
    }
}

/// Schema-validated owned audit event. `event` is the exact canonical owned
/// object (`kind`,`payload`,`principal`,`profile`,`request`) consumed by the
/// unsigned-row digest; typed IDs are real validated values, never borrowed
/// text. `ledger` stays last so every ID String is destroyed before its byte
/// charge releases.
pub(crate) struct OwnedAuditEvent {
    pub(crate) event: CoreValue,
    pub(crate) profile: ProfileId,
    pub(crate) principal: Principal,
    pub(crate) request: RequestId,
    ledger: IdLedger,
}
impl OwnedAuditEvent {
    /// Build the exact event schema from the ORIGINAL typed Core context and
    /// owned guest data. Typed IDs already passed their real validators; their
    /// exact fallible retained copies are prepaid before cloning. Caller
    /// graph/depth/allocation errors retain map_alloc, never stored Integrity.
    pub(crate) fn for_append(
        cx: &tobkiri_gate::CallCx,
        kind: &CoreValue,
        payload: &CoreValue,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Self, StoreError> {
        if kind.as_str().is_none() {
            return Err(StoreError::Invalid(
                "audit guest kind must be a string".into(),
            ));
        }
        // The ledger precedes ALL fallible ID locals and stays the last
        // returned field: no String can outlive the exact charge it needs.
        let mut ledger = IdLedger::new(budget);
        ledger.precharge_copy(cx.profile().clone_storage_bytes(), 0)?;
        let profile = cx.profile().try_clone_precharged().map_err(map_alloc)?;
        let inline = match cx.principal() {
            Principal::Pack { .. } | Principal::PackUi { .. } => 71,
            Principal::Input { .. } | Principal::User => 0,
        };
        let principal_bytes = cx.principal().clone_storage_bytes().map_err(map_alloc)?;
        ledger.precharge_copy(principal_bytes, inline)?;
        let principal = cx.principal().try_clone_precharged().map_err(map_alloc)?;
        ledger.precharge_copy(cx.request().clone_storage_bytes(), 0)?;
        let request = cx.request().try_clone_precharged().map_err(map_alloc)?;
        let principal_value = principal_value(&principal, budget).map_err(map_alloc)?;
        let profile_value = CoreValue::string(profile.as_str(), budget).map_err(map_alloc)?;
        let request_value = CoreValue::string(request.as_str(), budget).map_err(map_alloc)?;
        let fields: [Result<(&'static str, CoreValue), AllocationError>; 2] =
            [Ok(("kind", kind.share())), Ok(("payload", payload.share()))];
        let payload = CoreValue::object_shared(fields.into_iter(), budget).map_err(map_alloc)?;
        let outer_kind = CoreValue::string("guest-event", budget).map_err(map_alloc)?;
        let event = event_value(
            outer_kind,
            payload,
            principal_value,
            profile_value,
            request_value,
            budget,
        )
        .map_err(map_alloc)?;
        Ok(Self {
            event,
            profile,
            principal,
            request,
            ledger,
        })
    }

    /// Recheck the retained typed provenance against the ORIGINAL append
    /// context before SQL. Nested caller JSON remains data and cannot choose
    /// either identity. Both typed string comparisons are funded first.
    pub(crate) fn check_append_context(&self, cx: &tobkiri_gate::CallCx) -> Result<(), StoreError> {
        let work = self
            .profile
            .as_str()
            .len()
            .checked_add(self.request.as_str().len())
            .and_then(|bytes| u64::try_from(bytes).ok())
            .ok_or(StoreError::Quota)?;
        charge_work(&self.ledger.budget, 2, work)?;
        if self.profile != *cx.profile() || self.request != *cx.request() {
            return Err(StoreError::Denied("audit owned provenance mismatch".into()));
        }
        Ok(())
    }

    /// Canonical writer key on the same retained funding owner as the IDs it
    /// reads. This real use of the ledger preserves custody rather than
    /// manufacturing an unused-field suppression.
    pub(crate) fn writer_key(&self) -> Result<tobkiri_canon::CoreBuffer, StoreError> {
        let budget = &self.ledger.budget;
        budget
            .reserve(AllocationRequest {
                kind: AllocationKind::String,
                bytes: 0,
                elements: 1,
                depth: 0,
                work: 8,
            })
            .map_err(map_alloc)?;
        let (prefix, suffix) = match &self.principal {
            Principal::Pack { pack, .. } | Principal::PackUi { pack, .. } => {
                ("pack:", pack.as_str())
            }
            Principal::Input { source } => ("input:", source.as_str()),
            Principal::User => ("user", ""),
        };
        let len = prefix
            .len()
            .checked_add(suffix.len())
            .ok_or(StoreError::Quota)?;
        let mut writer = tobkiri_canon::CoreBuffer::builder(budget, len);
        writer.append(prefix.as_bytes()).map_err(map_alloc)?;
        writer.append(suffix.as_bytes()).map_err(map_alloc)?;
        writer.finish().map_err(map_alloc)
    }

    /// Parse stored StrictV1 event bytes into owned data only: the full
    /// AuditEvent/Principal schema and every typed ID is validated, and the
    /// typed profile must equal `expected_profile`.
    pub(crate) fn parse(
        body: &[u8],
        expected_profile: &ProfileId,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Self, StoreError> {
        Self::parse_ledger(body, expected_profile, budget, IdLedger::new(budget))
    }
    #[cfg(test)]
    pub(crate) fn parse_probed(
        body: &[u8],
        expected_profile: &ProfileId,
        budget: &Arc<dyn AllocationBudget>,
        probe: Arc<LedgerProbe>,
    ) -> Result<Self, StoreError> {
        Self::parse_ledger(
            body,
            expected_profile,
            budget,
            IdLedger::new_probed(budget, probe),
        )
    }
    /// Shared parse body. `ledger` arrives predeclared so a test probe can
    /// observe the drop boundary; parameters drop after locals, preserving
    /// the real payload-destruction-before-release order.
    fn parse_ledger(
        body: &[u8],
        expected_profile: &ProfileId,
        budget: &Arc<dyn AllocationBudget>,
        mut ledger: IdLedger,
    ) -> Result<Self, StoreError> {
        let value = CoreValue::parse_guest(body, budget).map_err(stored_fault)?;
        let object = value.object().ok_or_else(integrity)?;
        if object.len() != 5 {
            return Err(integrity());
        }
        let kind = field(&object, "kind", budget)?;
        text_of(&kind)?;
        let payload = field(&object, "payload", budget)?;
        let principal_v = field(&object, "principal", budget)?;
        let profile_v = field(&object, "profile", budget)?;
        let request_v = field(&object, "request", budget)?;
        let pobject = principal_v.object().ok_or_else(integrity)?;
        let pkind = field(&pobject, "kind", budget)?;
        let pkind_text = text_of(&pkind)?;
        // The four closed principal-kind alternatives may each compare the
        // borrowed spelling; fund every alternative before matching it.
        let kind_work = text_len(pkind_text)?
            .checked_mul(4)
            .and_then(|work| work.checked_add(4))
            .ok_or(StoreError::Quota)?;
        charge_work(budget, 1, kind_work)?;
        let profile = typed(
            &mut ledger,
            &profile_v,
            IdGrammar::Component,
            ProfileId::try_parse_precharged,
        )?;
        charge_work(budget, 1, text_len(expected_profile.as_str())?)?;
        if profile != *expected_profile {
            return Err(integrity());
        }
        let request = typed(
            &mut ledger,
            &request_v,
            IdGrammar::Token,
            RequestId::try_parse_precharged,
        )?;
        let principal = match pkind_text {
            "pack" => {
                if pobject.len() != 3 {
                    return Err(integrity());
                }
                let pack = typed(
                    &mut ledger,
                    &field(&pobject, "pack", budget)?,
                    IdGrammar::Pack,
                    PackId::try_parse_precharged,
                )?;
                let hash = ledger.hash(text_of(&field(&pobject, "hash", budget)?)?)?;
                Principal::Pack { pack, hash }
            }
            "pack-ui" => {
                if pobject.len() != 4 {
                    return Err(integrity());
                }
                let pack = typed(
                    &mut ledger,
                    &field(&pobject, "pack", budget)?,
                    IdGrammar::Pack,
                    PackId::try_parse_precharged,
                )?;
                let hash = ledger.hash(text_of(&field(&pobject, "hash", budget)?)?)?;
                let ui = typed(
                    &mut ledger,
                    &field(&pobject, "ui", budget)?,
                    IdGrammar::Token,
                    UiSessionId::try_parse_precharged,
                )?;
                Principal::PackUi { pack, hash, ui }
            }
            "input" => {
                if pobject.len() != 2 {
                    return Err(integrity());
                }
                let source = typed(
                    &mut ledger,
                    &field(&pobject, "source", budget)?,
                    IdGrammar::Token,
                    InputSourceId::try_parse_precharged,
                )?;
                Principal::Input { source }
            }
            "user" => {
                if pobject.len() != 1 {
                    return Err(integrity());
                }
                Principal::User
            }
            _ => return Err(integrity()),
        };
        let event = event_value(kind, payload, principal_v, profile_v, request_v, budget)
            .map_err(stored_fault)?;
        Ok(Self {
            event,
            profile,
            principal,
            request,
            ledger,
        })
    }
    /// Canonical owned unsigned row: numeric `seq`, `prev_digest`, shared
    /// `event`, plus `event_id` only when `Some` — `None` omits the key and
    /// never writes `null`.
    pub(crate) fn unsigned_row(
        &self,
        seq: u64,
        prev_digest: &Digest,
        event_id: Option<&Digest>,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<CoreValue, StoreError> {
        if seq == 0 || seq > MAX_SAFE_INTEGER {
            return Err(integrity());
        }
        self.unsigned_value(seq, prev_digest, event_id, budget)
            .map_err(stored_fault)
    }

    fn unsigned_value(
        &self,
        seq: u64,
        prev_digest: &Digest,
        event_id: Option<&Digest>,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<CoreValue, AllocationError> {
        let seq = CoreValue::int(seq as i64, budget)?;
        let prev = CoreValue::string(prev_digest.as_str(), budget)?;
        match event_id {
            Some(id) => {
                let id = CoreValue::string(id.as_str(), budget)?;
                let fields: [Result<(&'static str, CoreValue), AllocationError>; 4] = [
                    Ok(("event", self.event.share())),
                    Ok(("event_id", id)),
                    Ok(("prev_digest", prev)),
                    Ok(("seq", seq)),
                ];
                CoreValue::object_shared(fields.into_iter(), budget)
            }
            None => {
                let fields: [Result<(&'static str, CoreValue), AllocationError>; 3] = [
                    Ok(("event", self.event.share())),
                    Ok(("prev_digest", prev)),
                    Ok(("seq", seq)),
                ];
                CoreValue::object_shared(fields.into_iter(), budget)
            }
        }
    }

    /// New caller event depth/resource failures are caller Quota/Invalid,
    /// while stored verification retains its existing Integrity mapping.
    pub(crate) fn append_digest(
        &self,
        seq: u64,
        prev: &Digest,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Digest, StoreError> {
        if seq == 0 || seq > MAX_SAFE_INTEGER {
            return Err(StoreError::Quota);
        }
        self.unsigned_value(seq, prev, None, budget)
            .map_err(map_alloc)?
            .digest_guest(budget)
            .map_err(map_alloc)
    }
    /// Legacy-compatible unsigned-row digest over the owned components.
    pub(crate) fn unsigned_digest(
        &self,
        seq: u64,
        prev_digest: &Digest,
        event_id: Option<&Digest>,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Digest, StoreError> {
        self.unsigned_row(seq, prev_digest, event_id, budget)?
            .digest_guest(budget)
            .map_err(stored_fault)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::canonical;
    use tobkiri_gate::IntegrityFaultCode;

    /// Byte/work tracker with a monotonic op order so probes can assert the
    /// release actually happens after the observed drop boundary.
    pub(super) struct Track {
        pub(super) live: AtomicU64,
        pub(super) order: AtomicU64,
        work: AtomicU64,
        released: AtomicU64,
        release_order: AtomicU64,
    }
    impl Track {
        fn new() -> (Arc<Self>, Arc<dyn AllocationBudget>) {
            let track = Arc::new(Self {
                live: AtomicU64::new(0),
                order: AtomicU64::new(0),
                work: AtomicU64::new(0),
                released: AtomicU64::new(0),
                release_order: AtomicU64::new(0),
            });
            (track.clone(), track)
        }
    }
    impl AllocationBudget for Track {
        fn reserve(&self, request: AllocationRequest) -> Result<(), AllocationError> {
            self.order.fetch_add(1, Ordering::AcqRel);
            self.work
                .fetch_add(request.work.max(request.elements), Ordering::AcqRel);
            self.live.fetch_add(request.bytes, Ordering::AcqRel);
            Ok(())
        }
        fn release(&self, bytes: u64) {
            self.release_order.store(
                self.order.fetch_add(1, Ordering::AcqRel) + 1,
                Ordering::Release,
            );
            self.released.fetch_add(bytes, Ordering::AcqRel);
            self.live.fetch_sub(bytes, Ordering::AcqRel);
        }
    }
    fn body(profile: &str, request: &str, principal: serde_json::Value) -> Vec<u8> {
        canonical(&serde_json::json!({
            "kind": "probe",
            "payload": null,
            "principal": principal,
            "profile": profile,
            "request": request,
        }))
        .unwrap()
    }

    #[test]
    fn id_payloads_destroyed_before_charge_release() {
        let (track, budget) = Track::new();
        let profile = ProfileId::parse("prof_owned_audit").unwrap();
        let (pack, ui, request) = ("local.guest-audit", "ui.1", "req_owned_audit");
        let hash = PackHash::new(Digest::hash(b"pack"));
        let bytes = body(
            profile.as_str(),
            request,
            serde_json::json!({"kind":"pack-ui","pack":pack,"hash":hash.as_str(),"ui":ui}),
        );
        let probe = Arc::new(LedgerProbe::new(track.clone()));
        let expected = (profile.as_str().len() + request.len() + pack.len() + ui.len()) as u64;
        let owned =
            OwnedAuditEvent::parse_probed(&bytes, &profile, &budget, probe.clone()).unwrap();
        assert!(track.live.load(Ordering::Acquire) >= expected);
        let work_at_parse = track.work.load(Ordering::Acquire);
        assert!(work_at_parse > 0);
        drop(owned);
        // Recorded after every funded ID String was destroyed and before the
        // charge release; the order counter proves release came later.
        assert_eq!(probe.live_at_drop.load(Ordering::Acquire), expected);
        assert!(
            probe.order_at_drop.load(Ordering::Acquire)
                < track.release_order.load(Ordering::Acquire)
        );
        assert_eq!(track.live.load(Ordering::Acquire), 0);
        assert!(track.released.load(Ordering::Acquire) >= expected);
        assert_eq!(track.work.load(Ordering::Acquire), work_at_parse);
    }

    #[test]
    fn partial_id_failure_releases_after_parsed_payloads() {
        let (track, budget) = Track::new();
        let profile = ProfileId::parse("prof_owned_audit").unwrap();
        let hash = PackHash::new(Digest::hash(b"pack"));
        // profile+request prepaid; pack spelling invalid -> Integrity.
        for (request, principal, expected) in [
            (
                "bad request",
                serde_json::json!({"kind":"pack","pack":"local.guest-audit","hash":hash.as_str()}),
                (profile.as_str().len() + "bad request".len()) as u64,
            ),
            (
                "req_owned_audit",
                serde_json::json!({"kind":"pack","pack":"BAD.Pack","hash":hash.as_str()}),
                (profile.as_str().len() + "req_owned_audit".len() + "BAD.Pack".len()) as u64,
            ),
        ] {
            let bytes = body(profile.as_str(), request, principal);
            let probe = Arc::new(LedgerProbe::new(track.clone()));
            let work_before = track.work.load(Ordering::Acquire);
            assert!(matches!(
                OwnedAuditEvent::parse_probed(&bytes, &profile, &budget, probe.clone()),
                Err(StoreError::Integrity(IntegrityFaultCode::AuditChain))
            ));
            assert_eq!(probe.live_at_drop.load(Ordering::Acquire), expected);
            assert!(
                probe.order_at_drop.load(Ordering::Acquire)
                    < track.release_order.load(Ordering::Acquire)
            );
            assert_eq!(track.live.load(Ordering::Acquire), 0);
            assert!(track.work.load(Ordering::Acquire) >= work_before);
        }
    }
}
