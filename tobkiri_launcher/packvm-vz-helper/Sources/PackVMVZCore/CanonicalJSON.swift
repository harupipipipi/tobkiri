import CryptoKit
import Foundation

public let helperProtocol = "io.tobkiri.packvm-supervisor.v1"
public let maxProtocolLineBytes = 1024 * 1024
public let maxInvokePayloadBytes = 768 * 1024

public enum HelperError: Error, Equatable, LocalizedError {
    case invalidRequest(String)
    case unauthenticated
    case replayedNonce
    case unavailable(String)
    case invalidAsset(String)
    case invalidState(String)
    case protocolTooLarge

    public var errorDescription: String? {
        switch self {
        case let .invalidRequest(code), let .unavailable(code),
             let .invalidAsset(code), let .invalidState(code):
            return code
        case .unauthenticated:
            return "UNAUTHENTICATED"
        case .replayedNonce:
            return "NONCE_REPLAY"
        case .protocolTooLarge:
            return "PROTOCOL_TOO_LARGE"
        }
    }

    public var code: String {
        errorDescription ?? "INTERNAL_ERROR"
    }
}

public enum CanonicalJSON {
    public static func data(_ value: Any) throws -> Data {
        guard JSONSerialization.isValidJSONObject(value) else {
            throw HelperError.invalidRequest("INVALID_JSON_VALUE")
        }
        return try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
    }

    public static func object(_ data: Data) throws -> [String: Any] {
        guard let value = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw HelperError.invalidRequest("REQUEST_MUST_BE_OBJECT")
        }
        return value
    }

    public static func hmacHex(key: Data, object: [String: Any]) throws -> String {
        let payload = try data(object)
        let signature = HMAC<SHA256>.authenticationCode(
            for: payload,
            using: SymmetricKey(data: key)
        )
        return signature.map { String(format: "%02x", $0) }.joined()
    }

    public static func sha256(_ data: Data) -> String {
        "sha256:" + SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    public static func sha256Text(_ value: String) -> String {
        sha256(Data(value.utf8))
    }
}

public final class NonceReplayGuard {
    private var consumed: Set<String> = []
    private let lock = NSLock()

    public init() {}

    public func consume(_ nonce: String) throws {
        guard nonce.count == 64,
              nonce.allSatisfy({ $0.isHexDigit && !$0.isUppercase }) else {
            throw HelperError.invalidRequest("INVALID_NONCE")
        }
        lock.lock()
        defer { lock.unlock() }
        guard consumed.insert(nonce).inserted else {
            throw HelperError.replayedNonce
        }
    }
}

public struct AuthenticatedRequest {
    public let requestID: String
    public let operation: String
    public let nonce: String
    public let raw: [String: Any]
}

public enum ProtocolAuthenticator {
    private static let baseKeys: Set<String> = [
        "protocol", "request_id", "operation", "nonce", "request_hmac",
    ]

    public static func authenticate(
        _ value: [String: Any],
        key: Data,
        replayGuard: NonceReplayGuard
    ) throws -> AuthenticatedRequest {
        guard value["protocol"] as? String == helperProtocol,
              let requestID = value["request_id"] as? String,
              isIdentifier(requestID),
              let operation = value["operation"] as? String,
              supportedOperations.contains(operation),
              let nonce = value["nonce"] as? String,
              let receivedHmac = value["request_hmac"] as? String,
              isHexDigest(receivedHmac) else {
            throw HelperError.invalidRequest("INVALID_ENVELOPE")
        }

        var unsigned = value
        unsigned.removeValue(forKey: "request_hmac")
        let expectedHmac = try CanonicalJSON.hmacHex(key: key, object: unsigned)
        guard constantTimeEquals(expectedHmac, receivedHmac) else {
            throw HelperError.unauthenticated
        }
        try replayGuard.consume(nonce)
        return AuthenticatedRequest(
            requestID: requestID,
            operation: operation,
            nonce: nonce,
            raw: value
        )
    }

    public static func makeResponse(
        requestID: String,
        operation: String,
        nonce: String,
        success: Bool,
        data: [String: Any] = [:],
        errorCode: String? = nil,
        key: Data
    ) throws -> [String: Any] {
        var response: [String: Any] = [
            "protocol": helperProtocol,
            "request_id": requestID,
            "operation": operation,
            "nonce": nonce,
            "success": success,
            "data": data,
            "error": errorCode as Any,
        ]
        response["response_hmac"] = try CanonicalJSON.hmacHex(key: key, object: response)
        return response
    }

    public static func verifyResponse(_ value: [String: Any], key: Data) throws -> Bool {
        guard let responseHmac = value["response_hmac"] as? String else {
            return false
        }
        var unsigned = value
        unsigned.removeValue(forKey: "response_hmac")
        return constantTimeEquals(
            try CanonicalJSON.hmacHex(key: key, object: unsigned),
            responseHmac
        )
    }

    public static func requireOnlyKeys(_ value: [String: Any], allowed: Set<String>) throws {
        guard Set(value.keys).isSubset(of: baseKeys.union(allowed)) else {
            throw HelperError.invalidRequest("UNEXPECTED_FIELD")
        }
    }

    public static let supportedOperations: Set<String> = [
        "capability", "probe", "prepare_efi_store", "launch", "invoke", "bridge_result", "cancel", "terminate", "cleanup",
    ]

    public static func isIdentifier(_ value: String) -> Bool {
        guard !value.isEmpty, value.utf8.count <= 128 else { return false }
        return value.unicodeScalars.allSatisfy {
            CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "._-")).contains($0)
        }
    }

    public static func isSHA256Digest(_ value: String) -> Bool {
        guard value.count == 71, value.hasPrefix("sha256:") else { return false }
        return value.dropFirst(7).allSatisfy { $0.isHexDigit && !$0.isUppercase }
    }

    private static func isHexDigest(_ value: String) -> Bool {
        value.count == 64 && value.allSatisfy { $0.isHexDigit && !$0.isUppercase }
    }

    private static func constantTimeEquals(_ left: String, _ right: String) -> Bool {
        let leftBytes = Array(left.utf8)
        let rightBytes = Array(right.utf8)
        var difference = UInt8(leftBytes.count ^ rightBytes.count)
        let count = max(leftBytes.count, rightBytes.count)
        for index in 0..<count {
            let leftByte = index < leftBytes.count ? leftBytes[index] : 0
            let rightByte = index < rightBytes.count ? rightBytes[index] : 0
            difference |= leftByte ^ rightByte
        }
        return difference == 0
    }
}

public final class BoundedLineReader {
    private let handle: FileHandle
    private var buffered = Data()

    public init(handle: FileHandle) {
        self.handle = handle
    }

    public func nextLine() throws -> Data? {
        while true {
            if let newline = buffered.firstIndex(of: 0x0A) {
                let line = buffered.prefix(upTo: newline)
                buffered.removeSubrange(...newline)
                guard line.count <= maxProtocolLineBytes else {
                    throw HelperError.protocolTooLarge
                }
                return Data(line)
            }
            guard buffered.count <= maxProtocolLineBytes else {
                throw HelperError.protocolTooLarge
            }
            guard let chunk = try handle.read(upToCount: 4096), !chunk.isEmpty else {
                guard !buffered.isEmpty else { return nil }
                let line = buffered
                buffered.removeAll(keepingCapacity: false)
                return line
            }
            buffered.append(chunk)
        }
    }
}

public func writeCanonicalLine(_ value: [String: Any], to handle: FileHandle) throws {
    let data = try CanonicalJSON.data(value)
    guard data.count < maxProtocolLineBytes else {
        throw HelperError.protocolTooLarge
    }
    handle.write(data)
    handle.write(Data([0x0A]))
}
