import Foundation
import Testing
@testable import PackVMVZCore

struct DirectInvokePayloadTests {
    private var base: [String: Any] {
        [
            "request_id": "request-1",
            "request_digest": "sha256:" + String(repeating: "a", count: 64),
            "contract_id": "test.contract.v1",
            "contract_version": "1.0.0",
            "operation_id": "test",
            "deadline_monotonic": NSNull(),
        ]
    }

    private var extended: [String: Any] {
        var request = base
        request["payload_encoding"] = DirectInvokePayload.encoding
        request["payload_tokens"] = ["o", 1, "number", "f", "8000000000000000"] as [Any]
        return request
    }

    @Test
    func preservesLegacyObjectAndExtendedTokens() throws {
        var legacy = base
        legacy["payload"] = ["number": 1]
        let legacyFields = try DirectInvokePayload.fields(legacy)
        #expect(Set(legacyFields.keys) == ["payload"])
        #expect(try CanonicalJSON.data(legacyFields) == CanonicalJSON.data([
            "payload": ["number": 1],
        ]))

        let request = extended
        let fields = try DirectInvokePayload.fields(request)
        #expect(Set(fields.keys) == ["payload_encoding", "payload_tokens"])
        #expect(try CanonicalJSON.data(fields) == CanonicalJSON.data([
            "payload_encoding": request["payload_encoding"]!,
            "payload_tokens": request["payload_tokens"]!,
        ]))
    }

    @Test
    func rejectsAmbiguousUnknownMissingAndMalformedFields() throws {
        let mutations: [[String: Any]] = [
            ["payload": [:]],
            ["payload_encoding": NSNull()],
            ["payload_encoding": "tobkiri.flow-data.ieee754.v2"],
            ["payload_tokens": NSNull()],
            ["payload_tokens": ["number": 1]],
            ["extra": "field"],
        ]
        for mutation in mutations {
            let request = extended.merging(mutation) { _, value in value }
            #expect(throws: HelperError.invalidRequest("INVALID_DIRECT_INVOKE")) {
                _ = try DirectInvokePayload.fields(request)
            }
        }
        for field in ["payload_encoding", "payload_tokens"] {
            var request = extended
            request.removeValue(forKey: field)
            #expect(throws: HelperError.invalidRequest("INVALID_DIRECT_INVOKE")) {
                _ = try DirectInvokePayload.fields(request)
            }
        }
    }

    @Test
    func rejectsExtendedSpecialProtocolsBeforeAnyGuestExecution() throws {
        let mutations: [[String: Any]] = [
            ["contract_id": "conversation.saved-turn.v1"],
            ["contract_id": "tobkiri.service.mcp.tool.call.v1"],
        ]
        for mutation in mutations {
            let request = extended.merging(mutation) { _, value in value }
            #expect(throws: HelperError.invalidRequest("INVALID_DIRECT_INVOKE")) {
                _ = try DirectInvokePayload.fields(request)
            }
        }
    }

    @Test
    func preservesOpaqueOperationIdentifiersForGuestValidation() throws {
        for operation in ["extension.future-operation", "rumi_mcp_gateway_pack.mcp-tool-call"] {
            var request = extended
            request["operation_id"] = operation
            let fields = try DirectInvokePayload.fields(request)
            #expect(try CanonicalJSON.data(fields) == CanonicalJSON.data([
                "payload_encoding": request["payload_encoding"]!,
                "payload_tokens": request["payload_tokens"]!,
            ]))
        }
    }

    @Test
    func retainsTheSamePayloadByteBoundIncludingExtendedCarrierOverhead() throws {
        var legacy = base
        legacy["payload"] = ["value": String(repeating: "x", count: maxInvokePayloadBytes)]
        var encoded = extended
        encoded["payload_tokens"] = [
            "o", 1, "value", "s", String(repeating: "x", count: maxInvokePayloadBytes),
        ] as [Any]
        for request in [legacy, encoded] {
            #expect(throws: HelperError.invalidRequest("INVALID_DIRECT_INVOKE")) {
                _ = try DirectInvokePayload.fields(request)
            }
        }
    }
}
