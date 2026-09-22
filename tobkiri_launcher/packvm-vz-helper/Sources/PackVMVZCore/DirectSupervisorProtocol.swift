import Foundation

/// The Host-to-helper envelope.  The inherited channel key authenticates the
/// response; the guest's Ed25519 evidence is an untouched, nested payload.
/// Keeping the two signatures in separate layers prevents this sidecar from
/// becoming a signing oracle for a Pack child.
public let directSupervisorRequestKind = "tobkiri.macos-vz.supervisor.request.v1"
public let directSupervisorResponseKind = "tobkiri.macos-vz.supervisor.response.v1"
public let directSupervisorProtocol = "io.tobkiri.macos-vz-supervisor.v1"

public struct DirectSupervisorRequest {
    public let operation: String
    public let hostNonce: String
    public let domainID: String
    public let launchBindingDigest: String
    public let raw: [String: Any]

    public static func parse(
        _ value: [String: Any],
        replayGuard: NonceReplayGuard
    ) throws -> DirectSupervisorRequest {
        let common: Set<String> = [
            "kind", "protocol", "version", "operation", "host_nonce",
            "domain_id", "launch_binding_digest",
        ]
        guard value["kind"] as? String == directSupervisorRequestKind,
              value["protocol"] as? String == directSupervisorProtocol,
              (value["version"] as? NSNumber)?.intValue == 1,
              let operation = value["operation"] as? String,
              let expectedFields = fieldsByOperation[operation],
              Set(value.keys) == common.union(expectedFields),
              let hostNonce = value["host_nonce"] as? String,
              let domainID = value["domain_id"] as? String,
              let bindingDigest = value["launch_binding_digest"] as? String,
              Self.isDomainIdentifier(domainID),
              ProtocolAuthenticator.isSHA256Digest(bindingDigest) else {
            throw HelperError.invalidRequest("INVALID_DIRECT_ENVELOPE")
        }
        try replayGuard.consume(hostNonce)

        switch operation {
        case "launch":
            guard let binding = value["launch_binding"] as? [String: Any],
                  try validGuestChallenge(value["guest_challenge"]),
                  binding["domain_id"] as? String == domainID,
                  try CanonicalJSON.sha256(CanonicalJSON.data(binding)) == bindingDigest else {
                throw HelperError.invalidRequest("INVALID_LAUNCH_BINDING")
            }
        case "invoke":
            guard try validGuestChallenge(value["guest_challenge"]),
                  value["request"] is [String: Any] else {
                throw HelperError.invalidRequest("INVALID_INVOKE")
            }
        case "bridge_result":
            guard try validGuestChallenge(value["guest_challenge"]),
                  value["host_bridge_result"] is [String: Any] else {
                throw HelperError.invalidRequest("INVALID_BRIDGE_RESULT")
            }
        case "cancel":
            guard try validGuestChallenge(value["guest_challenge"]),
                  let requestID = value["request_id"] as? String,
                  Self.isBoundedIdentifier(requestID),
                  let requestDigest = value["request_digest"] as? String,
                  ProtocolAuthenticator.isSHA256Digest(requestDigest) else {
                throw HelperError.invalidRequest("INVALID_CANCEL")
            }
        case "terminate":
            guard let leaseID = value["lease_id"] as? String,
                  let reservationID = value["reservation_id"] as? String,
                  Self.isBoundedIdentifier(leaseID),
                  Self.isBoundedIdentifier(reservationID) else {
                throw HelperError.invalidRequest("INVALID_TERMINATE")
            }
        default:
            throw HelperError.invalidRequest("INVALID_DIRECT_ENVELOPE")
        }
        return DirectSupervisorRequest(
            operation: operation,
            hostNonce: hostNonce,
            domainID: domainID,
            launchBindingDigest: bindingDigest,
            raw: value
        )
    }

    private static let fieldsByOperation: [String: Set<String>] = [
        "launch": ["launch_binding", "guest_challenge"],
        "invoke": ["request", "guest_challenge"],
        "bridge_result": ["host_bridge_result", "guest_challenge"],
        "cancel": ["request_id", "request_digest", "guest_challenge"],
        "terminate": ["lease_id", "reservation_id"],
    ]

    private static func isDomainIdentifier(_ value: String) -> Bool {
        isBoundedIdentifier(value, maximum: 512)
    }

    private static func isBoundedIdentifier(_ value: String, maximum: Int = 256) -> Bool {
        guard !value.isEmpty, value.utf8.count <= maximum, !value.contains("\0") else {
            return false
        }
        return value.unicodeScalars.allSatisfy {
            CharacterSet.alphanumerics.union(
                CharacterSet(charactersIn: "._-:")
            ).contains($0)
        }
    }

    private static func validGuestChallenge(_ value: Any?) throws -> Bool {
        guard let challenge = value as? String,
              challenge.count == 64,
              challenge.allSatisfy({ $0.isHexDigit && !$0.isUppercase }) else {
            throw HelperError.invalidRequest("INVALID_GUEST_CHALLENGE")
        }
        return true
    }
}

public enum DirectSupervisorAuthenticator {
    /// Build the exact outer response authenticated by the inherited per-domain
    /// key. `payload` is a complete guest-signed envelope and is never copied,
    /// normalized, or augmented by this helper.
    public static func makeResponse(
        request: DirectSupervisorRequest,
        payload: [String: Any],
        key: Data
    ) throws -> [String: Any] {
        var response: [String: Any] = [
            "kind": directSupervisorResponseKind,
            "protocol": directSupervisorProtocol,
            "version": 1,
            "operation": request.operation,
            "host_nonce": request.hostNonce,
            "domain_id": request.domainID,
            "launch_binding_digest": request.launchBindingDigest,
            "payload": payload,
        ]
        response["agent_mac"] = try CanonicalJSON.hmacHex(key: key, object: response)
        return response
    }

    public static func verifyResponse(_ value: [String: Any], key: Data) throws -> Bool {
        let required: Set<String> = [
            "kind", "protocol", "version", "operation", "host_nonce",
            "domain_id", "launch_binding_digest", "payload", "agent_mac",
        ]
        guard Set(value.keys) == required,
              value["kind"] as? String == directSupervisorResponseKind,
              value["protocol"] as? String == directSupervisorProtocol,
              (value["version"] as? NSNumber)?.intValue == 1,
              value["payload"] is [String: Any],
              let received = value["agent_mac"] as? String else {
            return false
        }
        var unsigned = value
        unsigned.removeValue(forKey: "agent_mac")
        let expected = try CanonicalJSON.hmacHex(key: key, object: unsigned)
        return constantTimeEquals(expected, received)
    }

    private static func constantTimeEquals(_ left: String, _ right: String) -> Bool {
        let leftBytes = Array(left.utf8)
        let rightBytes = Array(right.utf8)
        var difference = UInt8(leftBytes.count ^ rightBytes.count)
        for index in 0..<max(leftBytes.count, rightBytes.count) {
            difference |= (index < leftBytes.count ? leftBytes[index] : 0)
                ^ (index < rightBytes.count ? rightBytes[index] : 0)
        }
        return difference == 0
    }
}
