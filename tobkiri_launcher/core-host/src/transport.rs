//! Bounded trusted-host copies and logical UI transport tickets.
//!
//! Tickets account only for Launcher flow control. Releasing one never proves
//! that Tauri, WebKit, or a Pack frame has dropped an internal IPC copy.

use std::{
    collections::BTreeMap,
    sync::{Arc, Mutex},
};

use tobkiri_core::{Core, CoreBuffer, Digest, ids::UiSessionId};

use crate::HostError;

pub(crate) const MAX_HOST_RESPONSE_BODY_BYTES: usize = 1024 * 1024;
pub const TRUSTED_HOST_RECEIVER: &str = "core-host";
const JSON_ESCAPE_FACTOR: usize = 6;
// Covers serialized field names, the 71-byte namespace, safe-integer
// generation/ticket IDs, and Core's fixed `ui_` + 26-byte ULID receiver.
const ENVELOPE_OVERHEAD_BYTES: usize = 256;
pub(crate) const MAX_HOST_RESPONSE_TICKET_BYTES: usize =
    MAX_HOST_RESPONSE_BODY_BYTES * JSON_ESCAPE_FACTOR + ENVELOPE_OVERHEAD_BYTES;
pub(crate) const MAX_HOST_INFLIGHT: usize = 4;
pub(crate) const MAX_HOST_UI_ATTACHMENTS: usize = 16;
pub(crate) const MAX_HOST_QUEUE_BYTES: usize = MAX_HOST_INFLIGHT * MAX_HOST_RESPONSE_TICKET_BYTES;

#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub(crate) struct TicketKey {
    namespace: Arc<String>,
    generation: u64,
    receiver: TicketReceiver,
    frame: TicketFrame,
    ticket_id: u64,
}

#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
enum TicketReceiver {
    PackUi(UiSessionId),
    TrustedHost,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
enum TicketFrame {
    PackUi,
    HostOutput,
}

impl TicketKey {
    fn new(namespace: Arc<String>, generation: u64, receiver: UiSessionId, ticket_id: u64) -> Self {
        Self {
            namespace,
            generation,
            receiver: TicketReceiver::PackUi(receiver),
            frame: TicketFrame::PackUi,
            ticket_id,
        }
    }

    fn host_output(namespace: Arc<String>, generation: u64, ticket_id: u64) -> Self {
        Self {
            namespace,
            generation,
            receiver: TicketReceiver::TrustedHost,
            frame: TicketFrame::HostOutput,
            ticket_id,
        }
    }
}

#[derive(Default)]
struct QueueState {
    tickets: BTreeMap<TicketKey, usize>,
    bytes: usize,
    next_ticket_id: u64,
}

/// Per-Core-process budget for bounded, caller-visible transport responses.
#[derive(Clone)]
pub struct TransportTickets {
    namespace: Arc<String>,
    state: Arc<Mutex<QueueState>>,
}

impl TransportTickets {
    /// Create one fresh transport namespace for this process from Core's
    /// operating-system CSPRNG. Randomness failure is an initialization error.
    pub fn new() -> Result<Self, HostError> {
        let namespace = Core::new_transport_namespace().map_err(|_| HostError::Core)?;
        Self::from_namespace(namespace)
    }

    fn from_namespace(namespace: Digest) -> Result<Self, HostError> {
        let value = namespace.as_str();
        if value.len() != 71
            || !value.starts_with("sha256:")
            || !value[7..]
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        {
            return Err(HostError::Invalid);
        }
        let mut owned = String::new();
        owned.try_reserve_exact(71).map_err(|_| HostError::Limit)?;
        owned.push_str(value);
        Ok(Self {
            namespace: Arc::new(owned),
            state: Arc::new(Mutex::new(QueueState::default())),
        })
    }

    /// The namespace is a fixed-format process identifier, not a bearer token.
    pub fn namespace(&self) -> &str {
        self.namespace.as_str()
    }

    pub fn copy_namespace(&self) -> Result<String, HostError> {
        let mut copy = String::new();
        copy.try_reserve_exact(71).map_err(|_| HostError::Limit)?;
        copy.push_str(self.namespace());
        Ok(copy)
    }

    pub(crate) fn namespace_matches(&self, namespace: &str) -> bool {
        self.namespace() == namespace
    }

    pub(crate) fn reserve(
        &self,
        generation: u64,
        receiver: UiSessionId,
    ) -> Result<TicketReservation, HostError> {
        self.reserve_ticket(
            generation,
            TicketReceiver::PackUi(receiver),
            TicketFrame::PackUi,
        )
    }

    /// Reserve the single trusted-host output slot in the shared transport
    /// ledger. This shares the same four-ticket and byte caps as Pack UI.
    pub fn reserve_host_output(&self, generation: u64) -> Result<TicketReservation, HostError> {
        let mut state = self.state.try_lock().map_err(|_| HostError::Limit)?;
        self.reserve_locked(
            &mut state,
            generation,
            TicketReceiver::TrustedHost,
            TicketFrame::HostOutput,
        )
    }

    fn reserve_ticket(
        &self,
        generation: u64,
        receiver: TicketReceiver,
        frame: TicketFrame,
    ) -> Result<TicketReservation, HostError> {
        let mut state = self.state.lock().map_err(|_| HostError::Core)?;
        self.reserve_locked(&mut state, generation, receiver, frame)
    }

    fn reserve_locked(
        &self,
        state: &mut QueueState,
        generation: u64,
        receiver: TicketReceiver,
        frame: TicketFrame,
    ) -> Result<TicketReservation, HostError> {
        if frame == TicketFrame::HostOutput
            && state
                .tickets
                .keys()
                .any(|key| key.frame == TicketFrame::HostOutput)
        {
            return Err(HostError::Limit);
        }
        let next_bytes = state
            .bytes
            .checked_add(MAX_HOST_RESPONSE_TICKET_BYTES)
            .ok_or(HostError::Limit)?;
        if state.tickets.len() >= MAX_HOST_INFLIGHT || next_bytes > MAX_HOST_QUEUE_BYTES {
            return Err(HostError::Limit);
        }
        let ticket_id = state
            .next_ticket_id
            .checked_add(1)
            .filter(|id| *id <= 9_007_199_254_740_991)
            .ok_or(HostError::Limit)?;
        let key = match frame {
            TicketFrame::PackUi => {
                let TicketReceiver::PackUi(receiver) = receiver else {
                    return Err(HostError::Core);
                };
                TicketKey::new(self.namespace.clone(), generation, receiver, ticket_id)
            }
            TicketFrame::HostOutput => {
                if receiver != TicketReceiver::TrustedHost {
                    return Err(HostError::Core);
                }
                TicketKey::host_output(self.namespace.clone(), generation, ticket_id)
            }
        };
        state
            .tickets
            .insert(key.clone(), MAX_HOST_RESPONSE_TICKET_BYTES);
        state.bytes = next_bytes;
        state.next_ticket_id = ticket_id;
        Ok(TicketReservation {
            tickets: self.clone(),
            key,
            ticket_id,
            keep_until_ack: false,
        })
    }

    /// Complete one exact flow ticket. Stale and duplicate ACKs are inert.
    pub(crate) fn acknowledge(
        &self,
        namespace: &str,
        generation: u64,
        receiver: UiSessionId,
        ticket_id: u64,
    ) -> Result<bool, HostError> {
        if !self.namespace_matches(namespace) {
            return Ok(false);
        }
        self.release(&TicketKey::new(
            self.namespace.clone(),
            generation,
            receiver,
            ticket_id,
        ))
    }

    /// ACK one exact trusted-host output ticket. Duplicate/stale ids are inert.
    pub fn acknowledge_host_output(
        &self,
        namespace: &str,
        generation: u64,
        receiver: &str,
        ticket_id: u64,
    ) -> Result<bool, HostError> {
        if !self.namespace_matches(namespace) || receiver != TRUSTED_HOST_RECEIVER {
            return Ok(false);
        }
        self.release(&TicketKey::host_output(
            self.namespace.clone(),
            generation,
            ticket_id,
        ))
    }

    pub(crate) fn retire_receiver(
        &self,
        generation: u64,
        receiver: &UiSessionId,
    ) -> Result<usize, HostError> {
        self.retire_matching(|key| {
            key.generation == generation
                && key.frame == TicketFrame::PackUi
                && key.receiver == TicketReceiver::PackUi(receiver.clone())
        })
    }

    pub fn retire_generation(&self, generation: u64) -> Result<usize, HostError> {
        self.retire_matching(|key| key.generation == generation)
    }

    fn retire_matching(
        &self,
        mut matches: impl FnMut(&TicketKey) -> bool,
    ) -> Result<usize, HostError> {
        let mut state = self.state.lock().map_err(|_| HostError::Core)?;
        let keys: Vec<_> = state
            .tickets
            .keys()
            .filter(|key| matches(key))
            .cloned()
            .collect();
        for key in &keys {
            if let Some(bytes) = state.tickets.remove(key) {
                state.bytes -= bytes;
            }
        }
        Ok(keys.len())
    }

    fn release(&self, key: &TicketKey) -> Result<bool, HostError> {
        let mut state = self.state.lock().map_err(|_| HostError::Core)?;
        let Some(bytes) = state.tickets.remove(key) else {
            return Ok(false);
        };
        state.bytes -= bytes;
        Ok(true)
    }

    #[cfg(test)]
    pub(crate) fn snapshot(&self) -> Result<(usize, usize), HostError> {
        let state = self.state.lock().map_err(|_| HostError::Core)?;
        Ok((state.tickets.len(), state.bytes))
    }

    #[cfg(test)]
    pub(crate) fn last_ticket_id(&self) -> Result<u64, HostError> {
        let state = self.state.lock().map_err(|_| HostError::Core)?;
        Ok(state.next_ticket_id)
    }
}

#[cfg(test)]
impl Default for TransportTickets {
    fn default() -> Self {
        Self::from_namespace(Digest::hash(b"launcher transport unit-test namespace"))
            .expect("fixed test namespace is valid")
    }
}

/// A reservation is automatically released on every failure path. Successful
/// command completion retains its logical slot until ACK, detach, or stop.
pub struct TicketReservation {
    tickets: TransportTickets,
    key: TicketKey,
    ticket_id: u64,
    keep_until_ack: bool,
}

impl TicketReservation {
    pub fn ticket_id(&self) -> u64 {
        self.ticket_id
    }

    pub fn keep_until_ack(mut self) {
        self.keep_until_ack = true;
    }
}

impl Drop for TicketReservation {
    fn drop(&mut self) {
        if !self.keep_until_ack {
            let _ = self.tickets.release(&self.key);
        }
    }
}

/// Copy Core-owned canonical JSON into a separate, bounded Launcher String.
/// The caller keeps its `CoreBuffer` alive for this entire borrowed copy.
pub(crate) fn copy_core_buffer(buffer: &CoreBuffer) -> Result<String, HostError> {
    let bytes = buffer.as_bytes();
    if bytes.len() > MAX_HOST_RESPONSE_BODY_BYTES {
        return Err(HostError::Limit);
    }
    let value = std::str::from_utf8(bytes).map_err(|_| HostError::Core)?;
    let mut output = String::new();
    output
        .try_reserve_exact(bytes.len())
        .map_err(|_| HostError::Limit)?;
    output.push_str(value);
    Ok(output)
}

/// Preflight and make one bounded final-host payload. `label` is serialized as
/// separate presentation text, never as a routing identity; its bytes count
/// against both the body cap and escaped event-envelope bound. The caller
/// retains the CoreBuffer through this borrowed copy.
pub fn copy_final_host_payload(
    buffer: &CoreBuffer,
    label: Option<&str>,
) -> Result<String, HostError> {
    let bytes = buffer.as_bytes();
    let body = std::str::from_utf8(bytes).map_err(|_| HostError::Core)?;
    if label.is_some_and(|label| label.len() > 256) {
        return Err(HostError::Limit);
    }
    let combined_bytes = label
        .map_or(0, |label| label.len())
        .checked_add(body.len())
        .filter(|length| *length <= MAX_HOST_RESPONSE_BODY_BYTES)
        .ok_or(HostError::Limit)?;
    escaped_envelope_bound(combined_bytes)?;
    let mut output = String::new();
    output
        .try_reserve_exact(body.len())
        .map_err(|_| HostError::Limit)?;
    output.push_str(body);
    Ok(output)
}

/// Upper bound for JSON string escaping plus either fixed trusted-host envelope.
/// Six bytes per input byte covers every JSON escape, including control chars.
pub(crate) fn escaped_envelope_bound(body_bytes: usize) -> Result<usize, HostError> {
    if body_bytes > MAX_HOST_RESPONSE_BODY_BYTES {
        return Err(HostError::Limit);
    }
    body_bytes
        .checked_mul(JSON_ESCAPE_FACTOR)
        .and_then(|bytes| bytes.checked_add(ENVELOPE_OVERHEAD_BYTES))
        .filter(|bytes| *bytes <= MAX_HOST_RESPONSE_TICKET_BYTES)
        .ok_or(HostError::Limit)
}

#[cfg(test)]
mod tests {
    use std::sync::{
        Arc,
        atomic::{AtomicU64, AtomicUsize, Ordering},
    };

    use tobkiri_core::{
        AllocationBudget, AllocationError, AllocationRequest, CoreBuffer, ids::UiSessionId,
    };

    use super::*;

    struct Budget {
        bytes: AtomicU64,
        drops: Arc<AtomicUsize>,
    }

    impl AllocationBudget for Budget {
        fn reserve(&self, request: AllocationRequest) -> Result<(), AllocationError> {
            self.bytes.fetch_add(request.bytes, Ordering::SeqCst);
            Ok(())
        }

        fn release(&self, bytes: u64) {
            self.bytes.fetch_sub(bytes, Ordering::SeqCst);
            self.drops.fetch_add(1, Ordering::SeqCst);
        }
    }

    fn receiver() -> UiSessionId {
        UiSessionId::parse("frame_receiver").unwrap()
    }

    fn test_namespace(seed: &[u8]) -> TransportTickets {
        TransportTickets::from_namespace(Digest::hash(seed)).unwrap()
    }

    #[test]
    fn delayed_ack_holds_one_bounded_ticket_and_duplicate_is_inert() {
        let tickets = TransportTickets::default();
        let reservation = tickets.reserve(4, receiver()).unwrap();
        let ticket_id = reservation.ticket_id();
        reservation.keep_until_ack();
        assert_eq!(
            tickets.snapshot().unwrap(),
            (1, MAX_HOST_RESPONSE_TICKET_BYTES)
        );
        assert!(
            tickets
                .acknowledge(tickets.namespace(), 4, receiver(), ticket_id)
                .unwrap()
        );
        assert!(
            !tickets
                .acknowledge(tickets.namespace(), 4, receiver(), ticket_id)
                .unwrap()
        );
        assert_eq!(tickets.snapshot().unwrap(), (0, 0));
    }

    #[test]
    fn ticket_ids_are_host_minted_monotone_and_exhaust_without_reuse() {
        let tickets = TransportTickets::default();
        let receiver = receiver();
        let first = tickets.reserve(1, receiver.clone()).unwrap();
        assert_eq!(first.ticket_id(), 1);
        drop(first);
        let second = tickets.reserve(1, receiver.clone()).unwrap();
        assert_eq!(second.ticket_id(), 2);
        drop(second);

        tickets.state.lock().unwrap().next_ticket_id = 9_007_199_254_740_991;
        assert!(matches!(
            tickets.reserve(1, receiver),
            Err(HostError::Limit)
        ));
        assert_eq!(tickets.snapshot().unwrap(), (0, 0));
    }

    #[test]
    fn stale_generation_and_wrong_receiver_cannot_release_a_ticket() {
        let tickets = TransportTickets::default();
        let reservation = tickets.reserve(8, receiver()).unwrap();
        let ticket_id = reservation.ticket_id();
        reservation.keep_until_ack();
        assert!(
            !tickets
                .acknowledge(tickets.namespace(), 7, receiver(), ticket_id)
                .unwrap()
        );
        assert!(
            !tickets
                .acknowledge(
                    tickets.namespace(),
                    8,
                    UiSessionId::parse("another_receiver").unwrap(),
                    ticket_id,
                )
                .unwrap()
        );
        assert!(
            tickets
                .acknowledge(tickets.namespace(), 8, receiver(), ticket_id)
                .unwrap()
        );
    }

    #[test]
    fn cancellation_failure_releases_only_after_reservation_owner_drops() {
        let tickets = TransportTickets::default();
        let reservation = tickets.reserve(3, receiver()).unwrap();
        assert_eq!(tickets.snapshot().unwrap().0, 1);
        drop(reservation);
        assert_eq!(tickets.snapshot().unwrap(), (0, 0));
    }

    #[test]
    fn receiver_crash_keeps_capacity_until_detach_then_retires_only_its_tickets() {
        let tickets = TransportTickets::default();
        tickets.reserve(5, receiver()).unwrap().keep_until_ack();
        tickets.reserve(5, receiver()).unwrap().keep_until_ack();
        tickets.reserve(6, receiver()).unwrap().keep_until_ack();
        assert_eq!(tickets.retire_receiver(5, &receiver()).unwrap(), 2);
        assert_eq!(tickets.snapshot().unwrap().0, 1);
    }

    #[test]
    fn saturation_rejects_without_growing_a_retry_queue() {
        let tickets = TransportTickets::default();
        for _ in 1..=MAX_HOST_INFLIGHT {
            tickets.reserve(1, receiver()).unwrap().keep_until_ack();
        }
        assert!(matches!(
            tickets.reserve(1, receiver()),
            Err(HostError::Limit)
        ));
        assert_eq!(tickets.snapshot().unwrap().0, MAX_HOST_INFLIGHT);
        tickets.retire_generation(1).unwrap();
        assert_eq!(tickets.snapshot().unwrap(), (0, 0));
    }

    #[test]
    fn host_output_has_one_shared_flow_ticket_and_exact_stale_safe_ack() {
        let tickets = TransportTickets::default();
        let output = tickets.reserve_host_output(7).unwrap();
        let ticket_id = output.ticket_id();
        output.keep_until_ack();
        assert!(matches!(
            tickets.reserve_host_output(7),
            Err(HostError::Limit)
        ));
        assert!(
            !tickets
                .acknowledge_host_output(tickets.namespace(), 6, TRUSTED_HOST_RECEIVER, ticket_id)
                .unwrap()
        );
        assert!(
            tickets
                .acknowledge_host_output(tickets.namespace(), 7, TRUSTED_HOST_RECEIVER, ticket_id)
                .unwrap()
        );
        assert!(
            !tickets
                .acknowledge_host_output(tickets.namespace(), 7, TRUSTED_HOST_RECEIVER, ticket_id)
                .unwrap()
        );
        let next = tickets.reserve_host_output(7).unwrap();
        assert!(next.ticket_id() > ticket_id);
        let next_id = next.ticket_id();
        next.keep_until_ack();
        assert_eq!(tickets.retire_generation(7).unwrap(), 1);
        assert!(
            !tickets
                .acknowledge_host_output(tickets.namespace(), 7, TRUSTED_HOST_RECEIVER, next_id)
                .unwrap()
        );
        assert_eq!(tickets.snapshot().unwrap(), (0, 0));
    }

    #[test]
    fn prior_process_namespace_cannot_ack_a_new_generation_one_ticket_one() {
        let previous_process = test_namespace(b"previous process");
        let current_process = test_namespace(b"current process");
        let old = previous_process.reserve_host_output(1).unwrap();
        let new = current_process.reserve_host_output(1).unwrap();
        assert_eq!(old.ticket_id(), 1);
        assert_eq!(new.ticket_id(), 1);
        old.keep_until_ack();
        new.keep_until_ack();

        assert!(
            !current_process
                .acknowledge_host_output(previous_process.namespace(), 1, TRUSTED_HOST_RECEIVER, 1)
                .unwrap()
        );
        assert_eq!(current_process.snapshot().unwrap().0, 1);
        assert!(
            !current_process
                .acknowledge_host_output(current_process.namespace(), 1, "foreign-receiver", 1)
                .unwrap()
        );
        assert_eq!(current_process.snapshot().unwrap().0, 1);
        assert!(
            current_process
                .acknowledge_host_output(current_process.namespace(), 1, TRUSTED_HOST_RECEIVER, 1)
                .unwrap()
        );
    }

    #[test]
    fn host_output_shares_global_inflight_capacity_and_never_waits_for_the_ledger() {
        let tickets = TransportTickets::default();
        for _ in 0..MAX_HOST_INFLIGHT - 1 {
            tickets.reserve(2, receiver()).unwrap().keep_until_ack();
        }
        let host = tickets.reserve_host_output(2).unwrap();
        host.keep_until_ack();
        assert!(matches!(
            tickets.reserve_host_output(2),
            Err(HostError::Limit)
        ));
        assert!(matches!(
            tickets.reserve(2, receiver()),
            Err(HostError::Limit)
        ));
        assert_eq!(
            tickets.snapshot().unwrap(),
            (MAX_HOST_INFLIGHT, MAX_HOST_QUEUE_BYTES)
        );

        let contended = TransportTickets::default();
        let _ledger = contended.state.lock().unwrap();
        assert!(matches!(
            contended.reserve_host_output(2),
            Err(HostError::Limit)
        ));
    }

    #[test]
    fn escaped_expansion_is_preflighted_and_core_buffer_drops_after_copy() {
        let drops = Arc::new(AtomicUsize::new(0));
        let budget = Arc::new(Budget {
            bytes: AtomicU64::new(0),
            drops: drops.clone(),
        });
        let budget_owner: Arc<dyn AllocationBudget> = budget.clone();
        let buffer = CoreBuffer::copy_from(b"\\u0000\\u0001", &budget_owner).unwrap();
        let input_bytes = buffer.len();
        let leased_bytes = budget.bytes.load(Ordering::SeqCst);
        let releases_before = drops.load(Ordering::SeqCst);
        assert!(leased_bytes >= input_bytes as u64);
        assert_eq!(
            escaped_envelope_bound(input_bytes).unwrap(),
            input_bytes * JSON_ESCAPE_FACTOR + ENVELOPE_OVERHEAD_BYTES
        );
        let host_copy = copy_core_buffer(&buffer).unwrap();
        assert_eq!(host_copy, "\\u0000\\u0001");
        let surface = "surface";
        let display_copy = copy_final_host_payload(&buffer, Some(surface)).unwrap();
        assert_eq!(display_copy, "\\u0000\\u0001");
        assert_eq!(
            escaped_envelope_bound(input_bytes + surface.len()).unwrap(),
            (input_bytes + surface.len()) * JSON_ESCAPE_FACTOR + ENVELOPE_OVERHEAD_BYTES
        );
        assert_eq!(budget.bytes.load(Ordering::SeqCst), leased_bytes);
        assert_eq!(drops.load(Ordering::SeqCst), releases_before);
        drop(buffer);
        assert_eq!(budget.bytes.load(Ordering::SeqCst), 0);
        assert!(drops.load(Ordering::SeqCst) > releases_before);
    }

    #[test]
    fn oversized_core_buffer_is_rejected_before_host_string_allocation() {
        let drops = Arc::new(AtomicUsize::new(0));
        let budget = Arc::new(Budget {
            bytes: AtomicU64::new(0),
            drops,
        });
        let budget_owner: Arc<dyn AllocationBudget> = budget.clone();
        let bytes = vec![b'x'; MAX_HOST_RESPONSE_BODY_BYTES + 1];
        let buffer = CoreBuffer::copy_from(&bytes, &budget_owner).unwrap();
        assert!(matches!(copy_core_buffer(&buffer), Err(HostError::Limit)));
    }

    #[test]
    fn presentation_label_is_bounded_and_counts_toward_the_event_body() {
        let budget = Arc::new(Budget {
            bytes: AtomicU64::new(0),
            drops: Arc::new(AtomicUsize::new(0)),
        });
        let budget_owner: Arc<dyn AllocationBudget> = budget;
        let bytes = vec![b'x'; MAX_HOST_RESPONSE_BODY_BYTES];
        let buffer = CoreBuffer::copy_from(&bytes, &budget_owner).unwrap();
        let too_long_surface = "s".repeat(257);
        assert!(matches!(
            copy_final_host_payload(&buffer, Some("surface")),
            Err(HostError::Limit)
        ));
        assert!(matches!(
            copy_final_host_payload(&buffer, Some(too_long_surface.as_str())),
            Err(HostError::Limit)
        ));
    }
}
