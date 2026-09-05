import Darwin
import Foundation
import PackVMVZCore

private enum StartupError: Error {
    case missingAgentKeyFD
    case invalidAgentKeyFD
    case invalidAgentKey
}

@main
struct PackVMVZHelperMain {
    static func main() {
        do {
            var key = try inheritedAgentKey(arguments: CommandLine.arguments)
            defer { key.resetBytes(in: 0..<key.count) }
            let supervisor = VZSupervisor()
            let replayGuard = NonceReplayGuard()
            let reader = BoundedLineReader(handle: .standardInput)
            defer { supervisor.cleanupAll() }
            while let line = try reader.nextLine() {
                let response = handle(line: line, key: key, supervisor: supervisor, replayGuard: replayGuard)
                try writeCanonicalLine(response, to: .standardOutput)
                if supervisor.shouldExit {
                    break
                }
            }
        } catch {
            fputs("tobkiri-packvm-vz-helper failed to initialize\n", stderr)
            exit(EXIT_FAILURE)
        }
    }

    private static func handle(
        line: Data,
        key: Data,
        supervisor: VZSupervisor,
        replayGuard: NonceReplayGuard
    ) -> [String: Any] {
        do {
            let object = try CanonicalJSON.object(line)
            if object["kind"] as? String == directSupervisorRequestKind {
                return try handleDirect(
                    object: object,
                    key: key,
                    supervisor: supervisor,
                    replayGuard: replayGuard
                )
            }
            let request = try ProtocolAuthenticator.authenticate(
                object,
                key: key,
                replayGuard: replayGuard
            )
            let data: [String: Any]
            switch request.operation {
            case "capability":
                try ProtocolAuthenticator.requireOnlyKeys(request.raw, allowed: [])
                data = supervisor.capability()
            case "probe":
                try ProtocolAuthenticator.requireOnlyKeys(request.raw, allowed: [])
                data = supervisor.probe()
            case "prepare_efi_store":
                data = try supervisor.prepareEFIStore(request.raw)
            case "launch":
                try ProtocolAuthenticator.requireOnlyKeys(request.raw, allowed: ["launch"])
                data = try supervisor.launch(
                    LaunchBinding.parse(request.raw["launch"] as Any),
                    hostNonce: request.nonce
                )
            case "invoke":
                data = try supervisor.invoke(request.raw)
            case "bridge_result":
                data = try supervisor.bridgeResult(request.raw)
            case "cancel":
                data = try supervisor.cancel(request.raw)
            case "terminate":
                data = try supervisor.terminate(request.raw)
            case "cleanup":
                data = try supervisor.cleanup(request.raw)
            default:
                throw HelperError.invalidRequest("UNSUPPORTED_OPERATION")
            }
            return try ProtocolAuthenticator.makeResponse(
                requestID: request.requestID,
                operation: request.operation,
                nonce: request.nonce,
                success: true,
                data: data,
                key: key
            )
        } catch let error as HelperError {
            return unsignedFailure(error)
        } catch {
            return unsignedFailure(HelperError.invalidState("INTERNAL_ERROR"))
        }
    }

    private static func handleDirect(
        object: [String: Any],
        key: Data,
        supervisor: VZSupervisor,
        replayGuard: NonceReplayGuard
    ) throws -> [String: Any] {
        let request = try DirectSupervisorRequest.parse(object, replayGuard: replayGuard)
        let payload: [String: Any]
        switch request.operation {
        case "launch":
            guard let bindingRaw = request.raw["launch_binding"],
                  let guestChallenge = request.raw["guest_challenge"] as? String else {
                throw HelperError.invalidRequest("INVALID_DIRECT_LAUNCH")
            }
            payload = try supervisor.directLaunch(
                DirectLaunchBinding.parse(bindingRaw),
                hostNonce: request.hostNonce,
                guestChallenge: guestChallenge
            )
        case "invoke":
            guard let invocation = request.raw["request"] as? [String: Any],
                  let guestChallenge = request.raw["guest_challenge"] as? String else {
                throw HelperError.invalidRequest("INVALID_DIRECT_INVOKE")
            }
            payload = try supervisor.directInvoke(
                domainID: request.domainID,
                request: invocation,
                guestChallenge: guestChallenge
            )
        case "bridge_result":
            guard let bridgeResult = request.raw["host_bridge_result"] as? [String: Any],
                  let guestChallenge = request.raw["guest_challenge"] as? String else {
                throw HelperError.invalidRequest("INVALID_DIRECT_BRIDGE_RESULT")
            }
            payload = try supervisor.directBridgeResult(
                domainID: request.domainID,
                hostBridgeResult: bridgeResult,
                guestChallenge: guestChallenge
            )
        case "cancel":
            guard let requestID = request.raw["request_id"] as? String,
                  let requestDigest = request.raw["request_digest"] as? String,
                  let guestChallenge = request.raw["guest_challenge"] as? String else {
                throw HelperError.invalidRequest("INVALID_DIRECT_CANCEL")
            }
            payload = try supervisor.directCancel(
                domainID: request.domainID,
                requestID: requestID,
                requestDigest: requestDigest,
                guestChallenge: guestChallenge
            )
        case "terminate":
            guard let leaseID = request.raw["lease_id"] as? String,
                  let reservationID = request.raw["reservation_id"] as? String else {
                throw HelperError.invalidRequest("INVALID_DIRECT_TERMINATE")
            }
            payload = try supervisor.directTerminate(
                domainID: request.domainID,
                leaseID: leaseID,
                reservationID: reservationID
            )
        default:
            throw HelperError.invalidRequest("UNSUPPORTED_OPERATION")
        }
        return try DirectSupervisorAuthenticator.makeResponse(
            request: request,
            payload: payload,
            key: key
        )
    }

    private static func unsignedFailure(_ error: HelperError) -> [String: Any] {
        // An invalid or unauthenticated request cannot safely select an envelope
        // identity to sign.  It therefore receives a deliberately untrusted
        // fixed error and the process remains available for the next request.
        [
            "protocol": helperProtocol,
            "success": false,
            "error": error.code,
        ]
    }

    private static func inheritedAgentKey(arguments: [String]) throws -> Data {
        guard let index = arguments.firstIndex(of: "--agent-key-fd"),
              arguments.indices.contains(index + 1),
              let descriptor = Int32(arguments[index + 1]),
              descriptor >= 3,
              fcntl(descriptor, F_GETFD) != -1 else {
            throw StartupError.missingAgentKeyFD
        }
        var key = Data()
        var buffer = [UInt8](repeating: 0, count: 64)
        while key.count <= 64 {
            let count = Darwin.read(descriptor, &buffer, buffer.count)
            if count > 0 {
                key.append(buffer, count: count)
                continue
            }
            if count == -1, errno == EINTR { continue }
            break
        }
        _ = Darwin.close(descriptor)
        // The Host factory creates exactly one 256-bit channel key per helper
        // process. Accepting a prefix or an extended key would cause the two
        // endpoints to authenticate different canonical response bytes.
        guard key.count == 32 else {
            throw StartupError.invalidAgentKey
        }
        return key
    }
}
