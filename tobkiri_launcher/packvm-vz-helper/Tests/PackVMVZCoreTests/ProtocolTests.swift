import CryptoKit
import Foundation
import Darwin
import Testing
@testable import PackVMVZCore

struct ProtocolTests {
    private let key = Data("01234567890123456789012345678901".utf8)

    @Test
    func canonicalBytesMatchUtf8HostEncodingForPathsAndUnicode() throws {
        let payload = ["path": "/Users/é"]
        let encoded = try CanonicalJSON.data(payload)

        #expect(String(data: encoded, encoding: .utf8) == "{\"path\":\"/Users/é\"}")
        #expect(
            try CanonicalJSON.hmacHex(key: key, object: payload)
                == "fd5fa037f203e451dd633ca6810db1125a6760b57851c0518ff963368d5d36cc"
        )
    }

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
    func directOperationalFailureUsesTheBoundAuthenticatedEnvelope() throws {
        let binding: [String: Any] = [
            "domain_id": "domain.provider.conversation",
            "kind": "tobkiri.macos-vz.launch-binding.v1",
            "version": 1,
        ]
        let request = try DirectSupervisorRequest.parse(
            [
                "kind": directSupervisorRequestKind,
                "protocol": directSupervisorProtocol,
                "version": 1,
                "operation": "launch",
                "host_nonce": String(repeating: "d", count: 64),
                "domain_id": "domain.provider.conversation",
                "launch_binding_digest": try CanonicalJSON.sha256(
                    CanonicalJSON.data(binding)
                ),
                "launch_binding": binding,
                "guest_challenge": String(repeating: "e", count: 64),
            ],
            replayGuard: NonceReplayGuard()
        )

        let response = try DirectSupervisorAuthenticator.makeFailure(
            request: request,
            error: HelperError.invalidAsset("DISK_DIGEST_MISMATCH"),
            key: key
        )

        #expect(try DirectSupervisorAuthenticator.verifyResponse(response, key: key))
        #expect((response["payload"] as? [String: Any])?["kind"] as? String
            == directSupervisorFailureKind)
        #expect((response["payload"] as? [String: Any])?["code"] as? String
            == "DISK_DIGEST_MISMATCH")
    }

    @Test
    func directGuestResponseRequiresTheAllocationBoundEd25519Signature() throws {
        let key = Curve25519.Signing.PrivateKey()
        let bindings = ["domain": "sha256:" + String(repeating: "a", count: 64)]
        let response = try signedDirectGuestResponse(key: key, bindings: bindings)

        try VZSupervisor.validateDirectGuestResponse(
            response,
            operation: "attest",
            requestID: "attest-domain.provider.conversation",
            domainID: "domain.provider.conversation",
            bindingDigests: bindings,
            guestChallenge: String(repeating: "e", count: 64),
            attestationNonce: String(repeating: "f", count: 64),
            publicKeyBytes: key.publicKey.rawRepresentation
        )
    }

    @Test
    func directGuestResponseRejectsTamperingAndWrongAllocationKey() throws {
        let key = Curve25519.Signing.PrivateKey()
        let bindings = ["domain": "sha256:" + String(repeating: "a", count: 64)]
        let response = try signedDirectGuestResponse(key: key, bindings: bindings)
        var tampered = response
        tampered["data"] = [
            "guest_artifact_identity": "sha256:" + String(repeating: "c", count: 64),
        ]

        #expect(throws: HelperError.unauthenticated) {
            try VZSupervisor.validateDirectGuestResponse(
                tampered,
                operation: "attest",
                requestID: "attest-domain.provider.conversation",
                domainID: "domain.provider.conversation",
                bindingDigests: bindings,
                guestChallenge: String(repeating: "e", count: 64),
                attestationNonce: String(repeating: "f", count: 64),
                publicKeyBytes: key.publicKey.rawRepresentation
            )
        }
        #expect(throws: HelperError.unauthenticated) {
            try VZSupervisor.validateDirectGuestResponse(
                response,
                operation: "attest",
                requestID: "attest-domain.provider.conversation",
                domainID: "domain.provider.conversation",
                bindingDigests: bindings,
                guestChallenge: String(repeating: "e", count: 64),
                attestationNonce: String(repeating: "f", count: 64),
                publicKeyBytes: Curve25519.Signing.PrivateKey().publicKey.rawRepresentation
            )
        }
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

    private func signedDirectGuestResponse(
        key: Curve25519.Signing.PrivateKey,
        bindings: [String: String]
    ) throws -> [String: Any] {
        var response: [String: Any] = [
            "kind": "tobkiri.packvm.guest.response.v1",
            "protocol": directSupervisorProtocol,
            "version": 1,
            "operation": "attest",
            "request_id": "attest-domain.provider.conversation",
            "domain_id": "domain.provider.conversation",
            "binding_digests": bindings,
            "guest_challenge": String(repeating: "e", count: 64),
            "attestation_nonce": String(repeating: "f", count: 64),
            "success": true,
            "data": ["guest_artifact_identity": "sha256:" + String(repeating: "b", count: 64)],
        ]
        response["agent_signature"] = try key.signature(
            for: CanonicalJSON.data(response)
        ).base64EncodedString()
        return response
    }

    @Test
    func readsShortPipeLineWithoutWaitingForTheBufferToFill() throws {
        var descriptors: [Int32] = [0, 0]
        #expect(pipe(&descriptors) == 0)
        let reader = SendableLineReader(
            BoundedLineReader(
                handle: FileHandle(fileDescriptor: descriptors[0], closeOnDealloc: true)
            )
        )
        let writer = FileHandle(fileDescriptor: descriptors[1], closeOnDealloc: true)
        defer { try? writer.close() }

        // This is the exact short JSONL framing used after the separate
        // 32-byte inherited channel-key pipe is consumed by the helper.
        try writer.write(contentsOf: Data("{}\n".utf8))
        let completion = DispatchSemaphore(value: 0)
        let result = LockedLineResult()
        DispatchQueue.global(qos: .userInitiated).async {
            defer { completion.signal() }
            result.store(Result { try reader.value.nextLine() })
        }

        #expect(completion.wait(timeout: .now() + .seconds(1)) == .success)
        #expect(try result.load().get() == Data("{}".utf8))
    }

    @Test
    func rejectsOversizedUnterminatedLineWithoutGrowingTheBuffer() throws {
        // Pipe framing itself is covered above with a short request.  Use a
        // finite regular-file fixture for the one-megabyte limit: once the
        // reader rejects the full buffer, a concurrent pipe writer can race
        // the closing descriptor and turn an intended assertion into SIGPIPE.
        let path = FileManager.default.temporaryDirectory.appendingPathComponent(
            "tobkiri-packvm-vz-oversized-line-\(UUID().uuidString)"
        )
        defer { try? FileManager.default.removeItem(at: path) }
        try Data(repeating: 0x61, count: maxProtocolLineBytes).write(to: path)
        let handle = try FileHandle(forReadingFrom: path)
        defer { try? handle.close() }
        let reader = BoundedLineReader(handle: handle)

        #expect(throws: HelperError.protocolTooLarge) {
            _ = try reader.nextLine()
        }
    }
}

private final class LockedLineResult: @unchecked Sendable {
    private let lock = NSLock()
    private var value: Result<Data?, Error>?

    func store(_ result: Result<Data?, Error>) {
        lock.lock()
        value = result
        lock.unlock()
    }

    func load() throws -> Result<Data?, Error> {
        lock.lock()
        defer { lock.unlock() }
        guard let value else {
            throw HelperError.invalidState("TEST_RESULT_MISSING")
        }
        return value
    }
}

private final class SendableLineReader: @unchecked Sendable {
    let value: BoundedLineReader

    init(_ value: BoundedLineReader) {
        self.value = value
    }
}
