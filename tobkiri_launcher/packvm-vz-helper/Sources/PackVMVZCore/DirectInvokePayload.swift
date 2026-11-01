import Foundation

/// Validate the two exact invoke transport schemas without interpreting data
/// tokens. The guest adapter owns decoding; the helper preserves their bytes'
/// canonical representation and does not change logical request identity.
public enum DirectInvokePayload {
    public static let encoding = "tobkiri.flow-data.ieee754.v1"

    public static func fields(_ request: [String: Any]) throws -> [String: Any] {
        let common: Set<String> = [
            "request_id", "request_digest", "contract_id", "contract_version",
            "operation_id", "deadline_monotonic",
        ]
        let fields: [String: Any]
        let boundedValue: Any
        if Set(request.keys) == common.union(["payload"]),
           let payload = request["payload"] as? [String: Any] {
            fields = ["payload": payload]
            boundedValue = payload
        } else if Set(request.keys) == common.union(["payload_encoding", "payload_tokens"]),
                  request["payload_encoding"] as? String == encoding,
                  let tokens = request["payload_tokens"] as? [Any],
                  request["contract_id"] as? String != "conversation.saved-turn.v1",
                  request["contract_id"] as? String != "tobkiri.service.mcp.tool.call.v1" {
            // Public contracts select wire protocols. The guest validates any
            // legacy ABI aliases; operation identifiers stay opaque here.
            // Omitting `payload` is intentional: removing the marker cannot
            // turn a new request into an accepted legacy request.
            fields = ["payload_encoding": encoding, "payload_tokens": tokens]
            boundedValue = fields
        } else {
            throw HelperError.invalidRequest("INVALID_DIRECT_INVOKE")
        }
        guard try CanonicalJSON.data(boundedValue).count <= maxInvokePayloadBytes else {
            throw HelperError.invalidRequest("INVALID_DIRECT_INVOKE")
        }
        return fields
    }
}
