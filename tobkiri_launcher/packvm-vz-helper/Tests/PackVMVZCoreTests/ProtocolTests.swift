import Foundation
import Testing
@testable import PackVMVZCore

struct ProtocolTests {
    private let key = Data("01234567890123456789012345678901".utf8)

    @Test
    func authenticatesCanonicalRequestAndSignsResponse() throws {
        var request: [String: Any] = [
            "protocol": helperProtocol,
            "request_id": "request-1",
            "operation": "capability",
            "nonce": String(repeating: "a", count: 64),
        ]
        request["request_hmac"] = try CanonicalJSON.hmacHex(key: key, object: request)
        let guardValue = NonceReplayGuard()
        let authenticated = try ProtocolAuthenticator.authenticate(
            request,
            key: key,
            replayGuard: guardValue
        )
        #expect(authenticated.operation == "capability")
        let response = try ProtocolAuthenticator.makeResponse(
            requestID: authenticated.requestID,
            operation: authenticated.operation,
            nonce: authenticated.nonce,
            success: true,
            data: ["ready": false],
            key: key
        )
        #expect(try ProtocolAuthenticator.verifyResponse(response, key: key))
    }

    @Test
    func rejectsNonceReplayAfterValidAuthentication() throws {
        var request: [String: Any] = [
            "protocol": helperProtocol,
            "request_id": "request-1",
            "operation": "probe",
            "nonce": String(repeating: "b", count: 64),
        ]
        request["request_hmac"] = try CanonicalJSON.hmacHex(key: key, object: request)
        let guardValue = NonceReplayGuard()
        _ = try ProtocolAuthenticator.authenticate(request, key: key, replayGuard: guardValue)
        #expect(throws: HelperError.replayedNonce) {
            _ = try ProtocolAuthenticator.authenticate(request, key: key, replayGuard: guardValue)
        }
    }

    @Test
    func rejectsUnsignedOrUnexpectedFields() throws {
        var request: [String: Any] = [
            "protocol": helperProtocol,
            "request_id": "request-1",
            "operation": "capability",
            "nonce": String(repeating: "c", count: 64),
            "unexpected": true,
        ]
        request["request_hmac"] = try CanonicalJSON.hmacHex(key: key, object: request)
        let authenticated = try ProtocolAuthenticator.authenticate(
            request,
            key: key,
            replayGuard: NonceReplayGuard()
        )
        #expect(throws: HelperError.invalidRequest("UNEXPECTED_FIELD")) {
            try ProtocolAuthenticator.requireOnlyKeys(authenticated.raw, allowed: [])
        }
    }

    @Test
    func directEnvelopeBindsGuestChallengeAndSignsOnlyOuterResponse() throws {
        let binding: [String: Any] = [
            "domain_id": "domain.provider.conversation",
            "kind": "tobkiri.macos-vz.launch-binding.v1",
            "version": 1,
        ]
        let bindingDigest = try CanonicalJSON.sha256(CanonicalJSON.data(binding))
        let request: [String: Any] = [
            "kind": directSupervisorRequestKind,
            "protocol": directSupervisorProtocol,
            "version": 1,
            "operation": "launch",
            "host_nonce": String(repeating: "d", count: 64),
            "domain_id": "domain.provider.conversation",
            "launch_binding_digest": bindingDigest,
            "launch_binding": binding,
            "guest_challenge": String(repeating: "e", count: 64),
        ]
        let parsed = try DirectSupervisorRequest.parse(
            request,
            replayGuard: NonceReplayGuard()
        )
        let guestResponse: [String: Any] = [
            "kind": "tobkiri.packvm.guest.response.v1",
            "protocol": directSupervisorProtocol,
            "version": 1,
            "operation": "attest",
            "request_id": "attest-domain.provider.conversation",
            "domain_id": "domain.provider.conversation",
            "binding_digests": ["domain": "sha256:" + String(repeating: "a", count: 64)],
            "guest_challenge": String(repeating: "e", count: 64),
            "success": true,
            "data": ["guest_artifact_identity": "sha256:" + String(repeating: "b", count: 64)],
            "agent_signature": Data(repeating: 9, count: 64).base64EncodedString(),
        ]
        let response = try DirectSupervisorAuthenticator.makeResponse(
            request: parsed,
            payload: guestResponse,
            key: key
        )
        #expect(try DirectSupervisorAuthenticator.verifyResponse(response, key: key))
        #expect((response["payload"] as? [String: Any])?["agent_signature"] as? String
            == guestResponse["agent_signature"] as? String)
    }

    @Test
    func rejectsDirectReplayAndMissingGuestChallenge() throws {
        let request: [String: Any] = [
            "kind": directSupervisorRequestKind,
            "protocol": directSupervisorProtocol,
            "version": 1,
            "operation": "terminate",
            "host_nonce": String(repeating: "f", count: 64),
            "domain_id": "domain.provider.conversation",
            "launch_binding_digest": "sha256:" + String(repeating: "a", count: 64),
            "lease_id": "lease-1",
            "reservation_id": "reservation-1",
        ]
        let guardValue = NonceReplayGuard()
        _ = try DirectSupervisorRequest.parse(request, replayGuard: guardValue)
        #expect(throws: HelperError.replayedNonce) {
            _ = try DirectSupervisorRequest.parse(request, replayGuard: guardValue)
        }
        var incomplete = request
        incomplete["operation"] = "invoke"
        incomplete.removeValue(forKey: "lease_id")
        incomplete.removeValue(forKey: "reservation_id")
        incomplete["request"] = [:]
        incomplete["host_nonce"] = String(repeating: "c", count: 64)
        #expect(throws: HelperError.invalidRequest("INVALID_DIRECT_ENVELOPE")) {
            _ = try DirectSupervisorRequest.parse(incomplete, replayGuard: NonceReplayGuard())
        }
    }
}
