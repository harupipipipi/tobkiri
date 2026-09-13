import Foundation
import Testing
@testable import PackVMVZCore

struct GuestReadinessRetryTests {
    @Test
    func terminalMachineStateRemainsFailClosedAcrossCallbacks() {
        let state = VZMachineLifecycleState()

        #expect(state.failureCode == nil)
        state.markGuestStopped()
        #expect(state.failureCode == "VZ_GUEST_STOPPED")
        state.markGuestStoppedWithError()
        #expect(state.failureCode == "VZ_GUEST_STOPPED_WITH_ERROR")
        state.markGuestStopped()
        #expect(state.failureCode == "VZ_GUEST_STOPPED_WITH_ERROR")
    }

    @Test
    func usesFiveMinuteProductionDeadlineForFreshGuestBoot() {
        #expect(directGuestReadinessTimeout == 300)
    }

    @Test
    func neverCompletingConnectLeavesLifecycleUnblockedForRetryAndCancellation() throws {
        let attempts = GuestConnectAttemptLimiter(maximumInFlight: 2)
        let neverCompletingConnect = try #require(attempts.acquire())
        let retryConnect = try #require(attempts.acquire())
        let state = DirectGuestCallState<String>()

        // The first lease models a VZ `connect` call that never invokes its
        // completion handler, so no connection descriptor is available. The
        // transport state can still be cancelled, and the bounded second slot
        // permits a fresh readiness attempt without using the VZ lifecycle
        // queue.
        state.cancel()
        #expect(state.isCancelled)
        #expect(state.result == nil)
        #expect(
            VZSupervisor.isTransientGuestReadinessError(
                HelperError.unavailable("GUEST_AGENT_CONNECT_PENDING")
            )
        )
        #expect(attempts.inFlight == 2)
        #expect(attempts.acquire() == nil)

        retryConnect.release()
        let laterRetry = try #require(attempts.acquire())
        #expect(attempts.inFlight == 2)
        laterRetry.release()
        neverCompletingConnect.release()
        #expect(attempts.inFlight == 0)
    }

    @Test
    func retriesOnlyTransientReadinessFailuresUntilGuestConnects() throws {
        var currentTime: TimeInterval = 100
        var attemptCount = 0
        var sleeps: [TimeInterval] = []

        let result: String = try GuestReadinessRetry.run(
            policy: .init(deadline: 5, initialDelay: 0.1, maximumDelay: 1),
            now: { currentTime },
            sleep: { interval in
                sleeps.append(interval)
                currentTime += interval
            },
            attempt: { _ in
                attemptCount += 1
                if attemptCount < 3 {
                    throw HelperError.unavailable("GUEST_AGENT_UNAVAILABLE")
                }
                return "ready"
            },
            isTransient: VZSupervisor.isTransientGuestReadinessError
        )

        #expect(result == "ready")
        #expect(attemptCount == 3)
        #expect(sleeps == [0.1, 0.2])
    }

    @Test
    func doesNotRetryProtocolOrAuthenticationFailures() throws {
        let currentTime: TimeInterval = 100
        var attemptCount = 0
        var sleepCount = 0

        #expect(throws: HelperError.unauthenticated) {
            _ = try GuestReadinessRetry.run(
                policy: .init(deadline: 5, initialDelay: 0.1, maximumDelay: 1),
                now: { currentTime },
                sleep: { _ in sleepCount += 1 },
                attempt: { _ in
                    attemptCount += 1
                    throw HelperError.unauthenticated
                },
                isTransient: VZSupervisor.isTransientGuestReadinessError
            ) as String
        }

        #expect(attemptCount == 1)
        #expect(sleepCount == 0)
    }

    @Test
    func stopsAtTheOverallDeadlineAndNeverSleepsPastIt() throws {
        var currentTime: TimeInterval = 100
        var attemptCount = 0
        var sleeps: [TimeInterval] = []

        #expect(throws: HelperError.unavailable("GUEST_AGENT_TIMEOUT")) {
            _ = try GuestReadinessRetry.run(
                policy: .init(deadline: 0.25, initialDelay: 0.1, maximumDelay: 1),
                now: { currentTime },
                sleep: { interval in
                    sleeps.append(interval)
                    currentTime += interval
                },
                attempt: { remaining in
                    attemptCount += 1
                    #expect(remaining > 0)
                    throw HelperError.unavailable("GUEST_AGENT_READINESS_TIMEOUT")
                },
                isTransient: VZSupervisor.isTransientGuestReadinessError
            ) as String
        }

        #expect(attemptCount == 2)
        #expect(sleeps.count == 2)
        #expect(sleeps[0] == 0.1)
        #expect(abs(sleeps[1] - 0.15) < 0.000_001)
        #expect(abs(currentTime - 100.25) < 0.000_001)
    }
}
