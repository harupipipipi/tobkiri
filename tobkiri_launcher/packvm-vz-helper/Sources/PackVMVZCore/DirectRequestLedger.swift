import Foundation

/// Helper-local transport ownership, not a guest nonce ledger or execution grant.
/// Each exchange has an exclusive ticket so late results cannot revive cancellation.
final class DirectRequestLedger {
    struct Ticket: Equatable {
        let requestID: String
        let generation: UUID
        let exchange: UUID
        let deadline: TimeInterval
    }

    private struct Entry {
        var ticket: Ticket
        var remainingBridges: Int
        var inFlight: Bool
    }

    private let lock = NSLock()
    private var entries: [String: Entry] = [:]
    private var cancelled: [String: TimeInterval] = [:]
    /// Fail-closed deadline covering a tombstone which could not be recorded
    /// because ``cancelled`` was saturated; new identities stay refused until
    /// the dropped cancellation would have expired.
    private var tombstoneOverflowUntil: TimeInterval = 0

    func begin(
        _ requestID: String, maximumBridges: Int,
        deadline: TimeInterval? = nil,
        now: TimeInterval = ProcessInfo.processInfo.systemUptime
    ) throws -> Ticket {
        lock.lock()
        defer { lock.unlock() }
        guard maximumBridges == 1 || maximumBridges == 4 else {
            throw HelperError.invalidRequest("INVALID_BRIDGE_LIMIT")
        }
        entries = entries.filter { $0.value.ticket.deadline > now }
        cancelled = cancelled.filter { $0.value > now }
        // A cancellation delivered before this request registered must still
        // win: concurrent dispatch means ordering between invoke and cancel is
        // not guaranteed, and a tombstoned identity can never become active.
        if cancelled.removeValue(forKey: requestID) != nil {
            throw HelperError.invalidState("REQUEST_CANCELLED")
        }
        guard entries[requestID] == nil else {
            throw HelperError.invalidState("REQUEST_ALREADY_ACTIVE")
        }
        // A cancellation tombstone must never be silently droppable: while
        // the tombstone set is saturated, or a tombstone dropped at the cap
        // would still be live, a new identity could not be cancelled safely.
        guard cancelled.count < 128, tombstoneOverflowUntil <= now else {
            throw HelperError.invalidState("CANCEL_TOMBSTONE_LIMIT")
        }
        guard entries.count < 128 else {
            throw HelperError.invalidState("ACTIVE_REQUEST_LIMIT")
        }
        let ticket = Ticket(
            requestID: requestID,
            generation: UUID(),
            exchange: UUID(),
            deadline: deadline ?? (now + 60)
        )
        entries[requestID] = Entry(
            ticket: ticket, remainingBridges: maximumBridges, inFlight: true
        )
        return ticket
    }

    func resume(
        _ requestID: String, now: TimeInterval = ProcessInfo.processInfo.systemUptime
    ) throws -> Ticket {
        lock.lock()
        defer { lock.unlock() }
        guard var entry = entries[requestID], entry.ticket.deadline > now else {
            entries.removeValue(forKey: requestID)
            throw HelperError.invalidState("REQUEST_NOT_ACTIVE")
        }
        guard !entry.inFlight else {
            throw HelperError.invalidState("REQUEST_EXCHANGE_IN_FLIGHT")
        }
        entry.ticket = Ticket(
            requestID: requestID, generation: entry.ticket.generation,
            exchange: UUID(), deadline: entry.ticket.deadline
        )
        entry.inFlight = true
        entries[requestID] = entry
        return entry.ticket
    }

    /// Call only after signed guest response validation. Pending consumes one hop.
    func settle(
        _ ticket: Ticket, pending: Bool,
        now: TimeInterval = ProcessInfo.processInfo.systemUptime
    ) throws {
        lock.lock()
        defer { lock.unlock() }
        guard var entry = entries[ticket.requestID], entry.ticket == ticket,
              entry.inFlight else {
            throw HelperError.invalidState("REQUEST_EXCHANGE_NOT_ACTIVE")
        }
        guard ticket.deadline > now else {
            entries.removeValue(forKey: ticket.requestID)
            throw HelperError.invalidState("REQUEST_DEADLINE_EXCEEDED")
        }
        if !pending {
            entries.removeValue(forKey: ticket.requestID)
            return
        }
        guard entry.remainingBridges > 0 else {
            entries.removeValue(forKey: ticket.requestID)
            throw HelperError.invalidState("REQUEST_BRIDGE_LIMIT")
        }
        entry.remainingBridges -= 1
        entry.inFlight = false
        entries[ticket.requestID] = entry
    }

    func abandon(_ ticket: Ticket) {
        lock.lock()
        defer { lock.unlock() }
        if entries[ticket.requestID]?.ticket == ticket {
            entries.removeValue(forKey: ticket.requestID)
        }
    }

    /// Retire before sending cancellation; late transport completions stay retired.
    /// A request which already finished or has not registered yet leaves a
    /// tombstone so cancellation remains idempotent and still wins a later
    /// racing ``begin`` for the same identity. When the tombstone set is
    /// saturated the tombstone cannot be stored, so its expiry window is
    /// enforced fail-closed by refusing new ``begin`` calls instead.
    func cancel(
        _ requestID: String,
        now: TimeInterval = ProcessInfo.processInfo.systemUptime
    ) {
        lock.lock()
        defer { lock.unlock() }
        if entries.removeValue(forKey: requestID) != nil {
            return
        }
        cancelled = cancelled.filter { $0.value > now }
        if cancelled[requestID] != nil || cancelled.count < 128 {
            cancelled[requestID] = now + 60
        } else {
            tombstoneOverflowUntil = max(tombstoneOverflowUntil, now + 60)
        }
    }
}
