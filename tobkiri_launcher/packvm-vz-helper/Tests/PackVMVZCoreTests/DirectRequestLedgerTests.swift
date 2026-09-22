import Foundation
import Testing
@testable import PackVMVZCore

struct DirectRequestLedgerTests {
    @Test
    func savedTurnRetainsExactlyFourBridgesAndOneDeadline() throws {
        let ledger = DirectRequestLedger()
        var ticket = try ledger.begin("saved", maximumBridges: 4, now: 100)
        for hop in 0..<4 {
            try ledger.settle(ticket, pending: true, now: 101 + Double(hop))
            ticket = try ledger.resume("saved", now: 102 + Double(hop))
            #expect(ticket.deadline == 160)
        }
        try ledger.settle(ticket, pending: false, now: 110)
        #expect(throws: HelperError.self) { try ledger.resume("saved", now: 111) }
    }

    @Test(arguments: [1, 4])
    func excessHopRetiresBothLegacyAndSavedRequests(limit: Int) throws {
        let ledger = DirectRequestLedger()
        var ticket = try ledger.begin("turn", maximumBridges: limit, now: 0)
        for _ in 0..<limit {
            try ledger.settle(ticket, pending: true, now: 1)
            ticket = try ledger.resume("turn", now: 2)
        }
        #expect(throws: HelperError.self) { try ledger.settle(ticket, pending: true, now: 3) }
        #expect(throws: HelperError.self) { try ledger.resume("turn", now: 4) }
    }

    @Test
    func concurrentExchangeAndStaleTicketCannotConsumeCurrentExchange() throws {
        let ledger = DirectRequestLedger()
        let initial = try ledger.begin("turn", maximumBridges: 4, now: 0)
        #expect(throws: HelperError.self) { try ledger.resume("turn", now: 1) }
        #expect(throws: HelperError.self) { try ledger.begin("turn", maximumBridges: 4, now: 1) }
        try ledger.settle(initial, pending: true, now: 1)
        let resumed = try ledger.resume("turn", now: 2)
        #expect(throws: HelperError.self) { try ledger.resume("turn", now: 2) }
        #expect(throws: HelperError.self) { try ledger.settle(initial, pending: true, now: 2) }
        ledger.abandon(initial)
        try ledger.settle(resumed, pending: false, now: 3)
    }

    @Test
    func cancelPreventsLateResultResurrectionAndOldCleanupCannotRemoveNewRequest() throws {
        let ledger = DirectRequestLedger()
        let initial = try ledger.begin("turn", maximumBridges: 4, now: 0)
        try ledger.cancel("turn")
        #expect(throws: HelperError.self) { try ledger.settle(initial, pending: true, now: 1) }
        let replacement = try ledger.begin("turn", maximumBridges: 4, now: 2)
        ledger.abandon(initial)
        #expect(throws: HelperError.self) { try ledger.settle(initial, pending: false, now: 3) }
        try ledger.settle(replacement, pending: false, now: 3)
    }

    @Test
    func originalExpiryRejectsBothResumeAndLateCompletion() throws {
        let ledger = DirectRequestLedger()
        let ticket = try ledger.begin("turn", maximumBridges: 4, now: 100)
        try ledger.settle(ticket, pending: true, now: 159)
        #expect(throws: HelperError.self) { try ledger.resume("turn", now: 160) }
        let next = try ledger.begin("next", maximumBridges: 4, now: 200)
        #expect(throws: HelperError.self) { try ledger.settle(next, pending: false, now: 260) }
        #expect(throws: HelperError.self) { try ledger.resume("next", now: 260) }
    }

    @Test
    func abandonedTransportCannotResume() throws {
        let ledger = DirectRequestLedger()
        let ticket = try ledger.begin("turn", maximumBridges: 4, now: 0)
        ledger.abandon(ticket)
        #expect(throws: HelperError.self) { try ledger.resume("turn", now: 1) }
    }

    @Test
    func capacityIsBoundedAndExpiredEntriesAreReclaimed() throws {
        let ledger = DirectRequestLedger()
        for index in 0..<128 {
            _ = try ledger.begin("turn-\(index)", maximumBridges: 1, now: 0)
        }
        #expect(throws: HelperError.self) { try ledger.begin("extra", maximumBridges: 1, now: 1) }
        _ = try ledger.begin("extra", maximumBridges: 1, now: 60)
        #expect(throws: HelperError.self) { try ledger.begin("bad", maximumBridges: 5, now: 60) }
    }
}
