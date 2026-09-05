import Darwin
import CryptoKit
import Foundation
import Security
@preconcurrency import Virtualization

private let helperBackendID = "tobkiri.python-pack-v4"
private let helperSubstrateID = "macos-vz"
private let helperDesignatedIdentifier = "dev.tobkiri.launcher.packvm-vz-helper"
private let launchStartTimeout: DispatchTimeInterval = .seconds(30)
private let stopTimeout: DispatchTimeInterval = .seconds(15)
// The guest service is started by cloud-init after EFI has handed off to the
// image. A single immediate vsock request races that setup on a cold boot.
// The signed launch binding has no readiness-deadline field. Use a production
// default that covers a fresh Debian image's EFI, cloud-init, and systemd
// startup without creating an unbounded hidden launch phase.
let directGuestReadinessTimeout: TimeInterval = 300
private let directGuestOperationTimeout: TimeInterval = 30
private let directGuestAttemptTimeout: TimeInterval = 2
private let directGuestInitialRetryDelay: TimeInterval = 0.1
private let directGuestMaximumRetryDelay: TimeInterval = 1
// A stuck framework connect has no connection descriptor to close. Keep at
// most one abandoned attempt plus one fresh attempt so a cold boot can still
// become ready without unbounded blocked worker accumulation.
private let directGuestMaximumPendingConnects = 2
private let directSerialDiagnosticsMaximumBytes = 128 * 1024

public final class VZSupervisor {
    private struct Domain {
        let binding: LaunchBinding
        let validatedAssets: ValidatedLaunchAssets
        let machine: VZMachineHandle
        let queue: DispatchQueue
        var activeRequests: Set<String>
    }

    private struct DirectDomain {
        let binding: DirectLaunchBinding
        let validatedAssets: ValidatedDirectLaunchAssets
        let machine: VZMachineHandle
        let queue: DispatchQueue
        let diagnostics: DirectSerialDiagnostics
        let guestArtifactIdentity: String
        var activeRequests: Set<String>
    }

    private var domains: [String: Domain] = [:]
    private var directDomains: [String: DirectDomain] = [:]
    private var launchedDomainID: String?
    private var preparedEFI: (domainID: String, runRoot: String, store: PreparedEFIStore)?
    private var terminal = false
    private let lock = NSLock()
    private let helperDigest: String
    private let codeSignatureValid: Bool

    public init(executableURL: URL = URL(fileURLWithPath: CommandLine.arguments[0])) {
        helperDigest = (try? SecureLaunchAssetValidatorDigest.digest(executableURL.path))
            ?? "sha256:unavailable"
        codeSignatureValid = Self.hasValidCodeSignature(executableURL)
    }

    public func capability() -> [String: Any] {
        let runtimeSupported = VZVirtualMachine.isSupported
        let ready = runtimeSupported && codeSignatureValid
        return [
            "backend_id": helperBackendID,
            "substrate_id": helperSubstrateID,
            "platform": Self.platformID(),
            "backend_digest": helperDigest,
            "virtualization_supported": runtimeSupported,
            "code_signature_valid": codeSignatureValid,
            "ready": ready,
            "reason": ready ? NSNull() : Self.capabilityFailure(
                runtimeSupported: runtimeSupported,
                signatureValid: codeSignatureValid
            ),
            "network_devices": 0,
            "directory_shares": 0,
        ]
    }

    public func probe() -> [String: Any] {
        let capability = capability()
        let activeDomains = synchronized { domains.count }
        return capability.merging(
            [
                "active_domains": activeDomains,
                "active_requests": synchronized {
                    domains.values.reduce(0) { $0 + $1.activeRequests.count }
                },
                "protocol_max_bytes": maxProtocolLineBytes,
            ],
            uniquingKeysWith: { _, replacement in replacement }
        )
    }

    public func prepareEFIStore(_ request: [String: Any]) throws -> [String: Any] {
        try ProtocolAuthenticator.requireOnlyKeys(
            request,
            allowed: ["domain_id", "run_root", "efi_variable_store_path"]
        )
        guard let domainID = request["domain_id"] as? String,
              let runRoot = request["run_root"] as? String,
              let storePath = request["efi_variable_store_path"] as? String,
              ProtocolAuthenticator.isIdentifier(domainID) else {
            throw HelperError.invalidRequest("INVALID_EFI_STORE_REQUEST")
        }
        guard synchronized({
            launchedDomainID == nil && preparedEFI == nil && !terminal
        }) else {
            throw HelperError.invalidState("HELPER_ALREADY_BOUND_TO_DOMAIN")
        }
        let store = try SecureLaunchAssetValidator.prepareEFIStore(
            runRoot: runRoot,
            path: storePath
        )
        synchronized {
            preparedEFI = (domainID, runRoot, store)
        }
        return [
            "domain_id": domainID,
            "state": "prepared",
            "efi_variable_store": [
                "path": store.descriptor.path,
                "digest": store.descriptor.digest,
                "device": String(store.device),
                "inode": String(store.inode),
            ],
        ]
    }

    public func launch(_ binding: LaunchBinding, hostNonce: String) throws -> [String: Any] {
        guard VZVirtualMachine.isSupported else {
            throw HelperError.unavailable("VIRTUALIZATION_UNAVAILABLE")
        }
        guard codeSignatureValid else {
            throw HelperError.unavailable("HELPER_SIGNATURE_OR_ENTITLEMENT_INVALID")
        }
        guard binding.bootMode != "efi" || matchesPreparedEFI(binding) else {
            throw HelperError.invalidState("EFI_VARIABLE_STORE_NOT_PREPARED")
        }
        let validatedAssets = try SecureLaunchAssetValidator.validate(binding)
        guard synchronized({ launchedDomainID == nil && !terminal }) else {
            throw HelperError.invalidState("HELPER_ALREADY_BOUND_TO_DOMAIN")
        }
        synchronized { launchedDomainID = binding.domainID }
        var started: (machine: VZMachineHandle, queue: DispatchQueue)?
        do {
            let configuration = try makeConfiguration(binding)
            do {
                try configuration.validate()
            } catch {
                throw HelperError.invalidState("VZ_CONFIGURATION_REJECTED")
            }
            guard configuration.networkDevices.isEmpty,
                  configuration.directorySharingDevices.isEmpty else {
                throw HelperError.invalidState("NETWORK_OR_HOST_SHARE_FORBIDDEN")
            }
            let queue = DispatchQueue(label: "io.tobkiri.packvm-vz.\(binding.domainID)")
            let machine = VZMachineHandle(
                VZVirtualMachine(configuration: configuration, queue: queue)
            )
            try start(machine, on: queue)
            started = (machine, queue)
            let domain = Domain(
                binding: binding,
                validatedAssets: validatedAssets,
                machine: machine,
                queue: queue,
                activeRequests: []
            )
            let guestArtifactIdentity = try attestGuest(domain, hostNonce: hostNonce)
            synchronized {
                domains[binding.domainID] = domain
            }
            return attestationData(
                binding,
                state: "running",
                hostNonce: hostNonce,
                guestArtifactIdentity: guestArtifactIdentity
            )
        } catch {
            if let started {
                try? stop(started.machine, on: started.queue)
            }
            try? SecureLaunchAssetValidator.removeValidatedOverlay(
                binding,
                validated: validatedAssets
            )
            synchronized { preparedEFI = nil }
            synchronized { terminal = true }
            throw error
        }
    }

    /// Start the production EFI configuration and return the guest's signed
    /// response without interpreting or re-signing it.  The Python Host
    /// independently verifies that Ed25519 envelope against the allocation's
    /// measured public key; this helper only authenticates the outer response.
    public func directLaunch(
        _ binding: DirectLaunchBinding,
        hostNonce: String,
        guestChallenge: String
    ) throws -> [String: Any] {
        guard VZVirtualMachine.isSupported else {
            throw HelperError.unavailable("VIRTUALIZATION_UNAVAILABLE")
        }
        guard codeSignatureValid else {
            throw HelperError.unavailable("HELPER_SIGNATURE_OR_ENTITLEMENT_INVALID")
        }
        guard matchesPreparedEFI(binding) else {
            throw HelperError.invalidState("EFI_VARIABLE_STORE_NOT_PREPARED")
        }
        let validatedAssets = try SecureLaunchAssetValidator.validate(binding)
        guard synchronized({ launchedDomainID == nil && !terminal }) else {
            throw HelperError.invalidState("HELPER_ALREADY_BOUND_TO_DOMAIN")
        }
        synchronized { launchedDomainID = binding.domainID }
        var diagnostics: DirectSerialDiagnostics?
        var started: (
            machine: VZMachineHandle,
            queue: DispatchQueue,
            diagnostics: DirectSerialDiagnostics
        )?
        do {
            let createdDiagnostics = try DirectSerialDiagnostics.create(
                runRoot: binding.runRoot
            )
            diagnostics = createdDiagnostics
            let configuration = try makeDirectConfiguration(
                binding,
                diagnosticsFD: createdDiagnostics.writeFD
            )
            do {
                try configuration.validate()
            } catch {
                throw HelperError.invalidState("VZ_CONFIGURATION_REJECTED")
            }
            guard configuration.networkDevices.isEmpty,
                  configuration.directorySharingDevices.isEmpty else {
                throw HelperError.invalidState("NETWORK_OR_HOST_SHARE_FORBIDDEN")
            }
            let queue = DispatchQueue(label: "io.tobkiri.packvm-vz.\(binding.domainID)")
            let machine = VZMachineHandle(
                VZVirtualMachine(configuration: configuration, queue: queue),
                diagnostics: createdDiagnostics
            )
            try start(machine, on: queue)
            started = (machine, queue, createdDiagnostics)
            createdDiagnostics.record("HOST_VM_START_SUCCEEDED")
            createdDiagnostics.record("HOST_VSOCK_ATTEST_BEGIN")
            let guestResponse = try callDirectGuest(
                machine: machine,
                queue: queue,
                binding: binding,
                envelope: [
                    "protocol": helperProtocol,
                    "operation": "attest",
                    "request_id": "attest-\(binding.domainID)",
                    "domain_id": binding.domainID,
                    "binding_digests": binding.bindingDigests,
                    "attestation_nonce": hostNonce,
                    "guest_challenge": guestChallenge,
                ],
                expectedOperation: "attest",
                expectedRequestID: "attest-\(binding.domainID)",
                expectedChallenge: guestChallenge,
                attestationNonce: hostNonce,
                retryForReadiness: true
            )
            createdDiagnostics.record("HOST_VSOCK_ATTEST_RETURNED")
            if guestResponse["success"] as? Bool == false {
                try? stop(machine, on: queue)
                createdDiagnostics.close()
                synchronized { terminal = true }
                return guestResponse
            }
            guard guestResponse["success"] as? Bool == true,
                  let data = guestResponse["data"] as? [String: Any],
                  Set(data.keys) == ["guest_artifact_identity"],
                  let guestArtifactIdentity = data["guest_artifact_identity"] as? String,
                  ProtocolAuthenticator.isSHA256Digest(guestArtifactIdentity) else {
                throw HelperError.unauthenticated
            }
            synchronized {
                directDomains[binding.domainID] = DirectDomain(
                    binding: binding,
                    validatedAssets: validatedAssets,
                    machine: machine,
                    queue: queue,
                    diagnostics: createdDiagnostics,
                    guestArtifactIdentity: guestArtifactIdentity,
                    activeRequests: []
                )
            }
            return guestResponse
        } catch {
            if let started {
                try? stop(started.machine, on: started.queue)
                started.diagnostics.close()
            } else if let diagnostics {
                diagnostics.close()
            }
            synchronized { terminal = true }
            throw error
        }
    }

    public func directInvoke(
        domainID: String,
        request: [String: Any],
        guestChallenge: String
    ) throws -> [String: Any] {
        guard Set(request.keys) == [
            "request_id", "request_digest", "contract_id", "contract_version",
            "operation_id", "payload", "deadline_monotonic",
        ],
              let requestID = request["request_id"] as? String,
              ProtocolAuthenticator.isIdentifier(requestID),
              let requestDigest = request["request_digest"] as? String,
              ProtocolAuthenticator.isSHA256Digest(requestDigest),
              let payload = request["payload"] as? [String: Any],
              try CanonicalJSON.data(payload).count <= maxInvokePayloadBytes else {
            throw HelperError.invalidRequest("INVALID_DIRECT_INVOKE")
        }
        var domain = try activeDirectDomain(domainID)
        guard domain.activeRequests.insert(requestID).inserted else {
            throw HelperError.invalidState("REQUEST_ALREADY_ACTIVE")
        }
        synchronized { directDomains[domainID] = domain }
        var completed = false
        defer {
            // A transport or schema failure must not leave an unowned request
            // pinned in the per-domain helper. The Host has already failed the
            // outer invocation and will not be permitted to resume it.
            if !completed { removeDirectActiveRequest(requestID, from: domainID) }
        }
        let guestPayload: [String: Any] = [
            "operation": "invoke",
            "request_id": requestID,
            "target_domain": domainID,
            "artifact_digest": domain.binding.artifactDigest,
            "materialization_digest": domain.binding.materializationDigest,
            "guest_artifact_identity": domain.guestArtifactIdentity,
            "contract_id": request["contract_id"] as Any,
            "contract_version": request["contract_version"] as Any,
            "operation_id": request["operation_id"] as Any,
            "payload": payload,
            "request_digest": requestDigest,
            "deadline_monotonic": request["deadline_monotonic"] as Any,
            "cancel_token": Self.freshGuestChallenge(),
        ]
        let response = try callDirectGuest(
            machine: domain.machine,
            queue: domain.queue,
            binding: domain.binding,
            envelope: [
                "protocol": helperProtocol,
                "operation": "invoke",
                "request_id": requestID,
                "domain_id": domainID,
                "binding_digests": domain.binding.bindingDigests,
                "payload": guestPayload,
                "guest_challenge": guestChallenge,
            ],
            expectedOperation: "invoke",
            expectedRequestID: requestID,
            expectedChallenge: guestChallenge,
            attestationNonce: nil
        )
        let pending = (response["data"] as? [String: Any])?["state"] as? String == "pending"
        if !pending { removeDirectActiveRequest(requestID, from: domainID) }
        completed = pending
        return response
    }

    public func directBridgeResult(
        domainID: String,
        hostBridgeResult: [String: Any],
        guestChallenge: String
    ) throws -> [String: Any] {
        guard let requestID = hostBridgeResult["request_id"] as? String,
              ProtocolAuthenticator.isIdentifier(requestID),
              try CanonicalJSON.data(hostBridgeResult).count <= maxInvokePayloadBytes else {
            throw HelperError.invalidRequest("INVALID_DIRECT_BRIDGE_RESULT")
        }
        let domain = try activeDirectDomain(domainID)
        guard synchronized({ directDomains[domainID]?.activeRequests.contains(requestID) == true }) else {
            throw HelperError.invalidState("REQUEST_NOT_ACTIVE")
        }
        defer { removeDirectActiveRequest(requestID, from: domainID) }
        let response = try callDirectGuest(
            machine: domain.machine,
            queue: domain.queue,
            binding: domain.binding,
            envelope: [
                "protocol": helperProtocol,
                "operation": "bridge_result",
                "request_id": requestID,
                "domain_id": domainID,
                "binding_digests": domain.binding.bindingDigests,
                "host_bridge_result": hostBridgeResult,
                "guest_challenge": guestChallenge,
            ],
            expectedOperation: "bridge_result",
            expectedRequestID: requestID,
            expectedChallenge: guestChallenge,
            attestationNonce: nil
        )
        return response
    }

    public func directCancel(
        domainID: String,
        requestID: String,
        requestDigest: String,
        guestChallenge: String
    ) throws -> [String: Any] {
        let domain = try activeDirectDomain(domainID)
        guard synchronized({ directDomains[domainID]?.activeRequests.contains(requestID) == true }) else {
            throw HelperError.invalidState("REQUEST_NOT_ACTIVE")
        }
        defer { removeDirectActiveRequest(requestID, from: domainID) }
        let response = try callDirectGuest(
            machine: domain.machine,
            queue: domain.queue,
            binding: domain.binding,
            envelope: [
                "protocol": helperProtocol,
                "operation": "cancel",
                "request_id": requestID,
                "domain_id": domainID,
                "binding_digests": domain.binding.bindingDigests,
                "guest_challenge": guestChallenge,
            ],
            expectedOperation: "cancel",
            expectedRequestID: requestID,
            expectedChallenge: guestChallenge,
            attestationNonce: nil
        )
        return response
    }

    public func directTerminate(
        domainID: String,
        leaseID: String,
        reservationID: String
    ) throws -> [String: Any] {
        // Keep the domain owned until every fallible cleanup step succeeds.
        // A binding mismatch must not evict another lease's VM, and a VZ stop
        // timeout/failure must remain retryable through this same helper.
        let domain = try activeDirectDomain(domainID)
        guard domain.binding.leaseID == leaseID,
              domain.binding.reservationID == reservationID else {
            throw HelperError.invalidState("DOMAIN_BINDING_MISMATCH")
        }
        try stop(domain.machine, on: domain.queue)
        domain.diagnostics.close()
        _ = try removeDirectDomain(domainID)
        synchronized {
            preparedEFI = nil
            terminal = true
        }
        return [
            "state": "terminated",
            "domain_id": domainID,
            "lease_id": leaseID,
            "reservation_id": reservationID,
            "cleanup": [
                "vm": "released",
                "cow_disk": "detached",
                "efi_store": "detached",
            ],
        ]
    }

    public func invoke(_ request: [String: Any]) throws -> [String: Any] {
        try ProtocolAuthenticator.requireOnlyKeys(
            request,
            allowed: ["domain_id", "invoke"]
        )
        guard let domainID = request["domain_id"] as? String,
              ProtocolAuthenticator.isIdentifier(domainID),
              let invoke = request["invoke"] as? [String: Any],
              Set(invoke.keys) == ["request_id", "payload"] ,
              let requestID = invoke["request_id"] as? String,
              ProtocolAuthenticator.isIdentifier(requestID),
              let payload = invoke["payload"] as? [String: Any] else {
            throw HelperError.invalidRequest("INVALID_INVOKE")
        }
        guard try CanonicalJSON.data(payload).count <= maxInvokePayloadBytes else {
            throw HelperError.protocolTooLarge
        }
        var domain = try activeDomain(domainID)
        guard domain.activeRequests.insert(requestID).inserted else {
            throw HelperError.invalidState("REQUEST_ALREADY_ACTIVE")
        }
        synchronized { domains[domainID] = domain }
        let agentEnvelope: [String: Any] = [
            "protocol": helperProtocol,
            "operation": "invoke",
            "request_id": requestID,
            "domain_id": domainID,
            "binding_digests": domain.binding.bindingDigests,
            "payload": payload,
        ]
        let response = try callGuest(
            machine: domain.machine,
            queue: domain.queue,
            binding: domain.binding,
            envelope: agentEnvelope
        )
        guard response["success"] as? Bool == true,
              let data = response["data"] as? [String: Any] else {
            removeActiveRequest(requestID, from: domainID)
            throw HelperError.unavailable("GUEST_AGENT_REJECTED_REQUEST")
        }
        do {
            try validatePendingBridgeRequest(
                data,
                requestID: requestID,
                domain: domain
            )
        } catch {
            removeActiveRequest(requestID, from: domainID)
            throw error
        }
        return [
            "domain_id": domainID,
            "request_id": requestID,
            "state": "pending",
            "host_bridge_request": data["host_bridge_request"] as Any,
            "binding_digests": domain.binding.bindingDigests,
        ]
    }

    public func bridgeResult(_ request: [String: Any]) throws -> [String: Any] {
        try ProtocolAuthenticator.requireOnlyKeys(
            request,
            allowed: ["domain_id", "bridge_result"]
        )
        guard let domainID = request["domain_id"] as? String,
              ProtocolAuthenticator.isIdentifier(domainID),
              let bridgeResult = request["bridge_result"] as? [String: Any],
              Set(bridgeResult.keys) == ["request_id", "host_bridge_result"],
              let requestID = bridgeResult["request_id"] as? String,
              ProtocolAuthenticator.isIdentifier(requestID),
              let hostBridgeResult = bridgeResult["host_bridge_result"] as? [String: Any] else {
            throw HelperError.invalidRequest("INVALID_BRIDGE_RESULT")
        }
        guard try CanonicalJSON.data(hostBridgeResult).count <= maxInvokePayloadBytes else {
            throw HelperError.protocolTooLarge
        }
        let domain = try activeDomain(domainID)
        guard synchronized({ domains[domainID]?.activeRequests.contains(requestID) == true }) else {
            throw HelperError.invalidState("REQUEST_NOT_ACTIVE")
        }
        try validateHostBridgeResult(
            hostBridgeResult,
            requestID: requestID,
            domain: domain
        )
        let agentEnvelope: [String: Any] = [
            "protocol": helperProtocol,
            "operation": "bridge_result",
            "request_id": requestID,
            "domain_id": domainID,
            "binding_digests": domain.binding.bindingDigests,
            "host_bridge_result": hostBridgeResult,
        ]
        let response = try callGuest(
            machine: domain.machine,
            queue: domain.queue,
            binding: domain.binding,
            envelope: agentEnvelope
        )
        guard response["success"] as? Bool == true,
              let data = response["data"] as? [String: Any] else {
            throw HelperError.unavailable("GUEST_AGENT_REJECTED_BRIDGE_RESULT")
        }
        removeActiveRequest(requestID, from: domainID)
        return [
            "domain_id": domainID,
            "request_id": requestID,
            "state": "completed",
            "result": data,
            "binding_digests": domain.binding.bindingDigests,
        ]
    }

    public func cancel(_ request: [String: Any]) throws -> [String: Any] {
        try ProtocolAuthenticator.requireOnlyKeys(
            request,
            allowed: ["domain_id", "cancel_request_id"]
        )
        guard let domainID = request["domain_id"] as? String,
              let requestID = request["cancel_request_id"] as? String,
              ProtocolAuthenticator.isIdentifier(domainID),
              ProtocolAuthenticator.isIdentifier(requestID) else {
            throw HelperError.invalidRequest("INVALID_CANCEL")
        }
        let domain = try activeDomain(domainID)
        guard synchronized({ domains[domainID]?.activeRequests.contains(requestID) == true }) else {
            throw HelperError.invalidState("REQUEST_NOT_ACTIVE")
        }
        let agentEnvelope: [String: Any] = [
            "protocol": helperProtocol,
            "operation": "cancel",
            "request_id": requestID,
            "domain_id": domainID,
            "binding_digests": domain.binding.bindingDigests,
        ]
        _ = try callGuest(
            machine: domain.machine,
            queue: domain.queue,
            binding: domain.binding,
            envelope: agentEnvelope
        )
        return [
            "domain_id": domainID,
            "request_id": requestID,
            "state": "cancelled",
            "signals": ["TERM"],
            "binding_digests": domain.binding.bindingDigests,
        ]
    }

    public func terminate(_ request: [String: Any]) throws -> [String: Any] {
        try ProtocolAuthenticator.requireOnlyKeys(request, allowed: ["domain_id"])
        guard let domainID = request["domain_id"] as? String,
              ProtocolAuthenticator.isIdentifier(domainID) else {
            throw HelperError.invalidRequest("INVALID_TERMINATE")
        }
        try requireBoundDomain(domainID)
        let binding = try removeDomain(domainID)
        try stop(binding.machine, on: binding.queue)
        synchronized { terminal = true }
        synchronized { preparedEFI = nil }
        // The Host transport owns unlinking the exact clone/store after it
        // observes this acknowledgement.  The helper has released every VZ
        // reference before returning, so neither mutable path remains open.
        return ["vm": "released", "cow_disk": "detached", "efi_store": "detached"]
    }

    public func cleanup(_ request: [String: Any]) throws -> [String: Any] {
        try ProtocolAuthenticator.requireOnlyKeys(request, allowed: ["domain_id"])
        guard let domainID = request["domain_id"] as? String,
              ProtocolAuthenticator.isIdentifier(domainID) else {
            throw HelperError.invalidRequest("INVALID_CLEANUP")
        }
        if let prepared = synchronized({ preparedEFI }), launchedDomainID == nil {
            guard prepared.domainID == domainID else {
                throw HelperError.invalidState("DOMAIN_BINDING_MISMATCH")
            }
            try SecureLaunchAssetValidator.removePreparedEFIStore(
                prepared.store,
                runRoot: prepared.runRoot
            )
            synchronized {
                preparedEFI = nil
                terminal = true
            }
            return [
                "domain_id": domainID,
                "state": "clean",
                "efi_variable_store_removed": true,
                "host_socket_residue": 0,
                "key_residue": 0,
            ]
        }
        try requireBoundDomain(domainID)
        guard let domain = synchronized({ domains.removeValue(forKey: domainID) }) else {
            synchronized { terminal = true }
            synchronized { preparedEFI = nil }
            return ["vm": "released", "cow_disk": "detached", "efi_store": "detached"]
        }
        try stop(domain.machine, on: domain.queue)
        synchronized { terminal = true }
        synchronized { preparedEFI = nil }
        return ["vm": "released", "cow_disk": "detached", "efi_store": "detached"]
    }

    public func cleanupAll() {
        let domainsToClean: [Domain] = synchronized {
            let current = Array(domains.values)
            domains.removeAll()
            return current
        }
        for domain in domainsToClean {
            try? stop(domain.machine, on: domain.queue)
            try? SecureLaunchAssetValidator.removeValidatedOverlay(
                domain.binding,
                validated: domain.validatedAssets
            )
        }
        let directDomainsToClean: [DirectDomain] = synchronized {
            let current = Array(directDomains.values)
            directDomains.removeAll()
            return current
        }
        for domain in directDomainsToClean {
            try? stop(domain.machine, on: domain.queue)
            domain.diagnostics.close()
        }
        // A successful `prepare_efi_store` is deliberately durable across the
        // one-shot provisioning helper's clean EOF.  The Host owns that
        // freshly-created file until it has either supplied it to the bound
        // launch or explicitly issued `cleanup`; deleting it here would turn a
        // verified preparation acknowledgement into a TOCTOU race.  A launched
        // domain never reaches this branch with an unowned store: terminate
        // first closes every VZ reference, then the Host removes the exact
        // allocation after its authenticated acknowledgement.
        synchronized { preparedEFI = nil }
    }

    public var shouldExit: Bool {
        synchronized { terminal }
    }

    private func activeDomain(_ domainID: String) throws -> Domain {
        try requireBoundDomain(domainID)
        guard let domain = synchronized({ domains[domainID] }) else {
            throw HelperError.invalidState("DOMAIN_NOT_ACTIVE")
        }
        return domain
    }

    private func removeDomain(_ domainID: String) throws -> Domain {
        guard let domain = synchronized({ domains.removeValue(forKey: domainID) }) else {
            throw HelperError.invalidState("DOMAIN_NOT_ACTIVE")
        }
        return domain
    }

    private func requireBoundDomain(_ domainID: String) throws {
        guard synchronized({ launchedDomainID == domainID }) else {
            throw HelperError.invalidState("DOMAIN_BINDING_MISMATCH")
        }
    }

    private func matchesPreparedEFI(_ binding: LaunchBinding) -> Bool {
        guard let store = binding.efiVariableStore else { return false }
        return synchronized {
            preparedEFI?.domainID == binding.domainID
                && preparedEFI?.runRoot == binding.runRoot
                && preparedEFI?.store.descriptor == store
        }
    }

    private func matchesPreparedEFI(_ binding: DirectLaunchBinding) -> Bool {
        synchronized {
            preparedEFI?.domainID == binding.domainID
                && preparedEFI?.runRoot == binding.runRoot
                && preparedEFI?.store.descriptor == binding.efiVariableStore
        }
    }

    private func removeActiveRequest(_ requestID: String, from domainID: String) {
        synchronized {
            guard var active = domains[domainID] else { return }
            active.activeRequests.remove(requestID)
            domains[domainID] = active
        }
    }

    private func activeDirectDomain(_ domainID: String) throws -> DirectDomain {
        try requireBoundDomain(domainID)
        guard let domain = synchronized({ directDomains[domainID] }) else {
            throw HelperError.invalidState("DOMAIN_NOT_ACTIVE")
        }
        return domain
    }

    private func removeDirectDomain(_ domainID: String) throws -> DirectDomain {
        try requireBoundDomain(domainID)
        guard let domain = synchronized({ directDomains.removeValue(forKey: domainID) }) else {
            throw HelperError.invalidState("DOMAIN_NOT_ACTIVE")
        }
        return domain
    }

    private func removeDirectActiveRequest(_ requestID: String, from domainID: String) {
        synchronized {
            guard var domain = directDomains[domainID] else { return }
            domain.activeRequests.remove(requestID)
            directDomains[domainID] = domain
        }
    }

    private func makeConfiguration(_ binding: LaunchBinding) throws -> VZVirtualMachineConfiguration {
        let configuration = VZVirtualMachineConfiguration()
        if binding.bootMode == "efi" {
            guard let efiVariableStore = binding.efiVariableStore else {
                throw HelperError.invalidRequest("EFI_VARIABLE_STORE_REQUIRED")
            }
            let bootLoader = VZEFIBootLoader()
            bootLoader.variableStore = VZEFIVariableStore(
                url: URL(fileURLWithPath: efiVariableStore.path)
            )
            configuration.bootLoader = bootLoader
        } else {
            let bootLoader = VZLinuxBootLoader(
                kernelURL: URL(fileURLWithPath: binding.assets["kernel"]!.path)
            )
            bootLoader.initialRamdiskURL = URL(fileURLWithPath: binding.assets["initrd"]!.path)
            bootLoader.commandLine = binding.kernelCommandLine
            configuration.bootLoader = bootLoader
        }
        configuration.platform = VZGenericPlatformConfiguration()
        configuration.cpuCount = binding.cpuCount
        configuration.memorySize = binding.memoryBytes

        let overlay = try diskAttachment(
            path: binding.assets["disk"]!.path,
            readOnly: false
        )
        let agent = try diskAttachment(
            path: binding.assets["agent"]!.path,
            readOnly: true
        )
        let config = try diskAttachment(
            path: binding.assets["config"]!.path,
            readOnly: true
        )
        let readOnlySupportDevices = [
            VZVirtioBlockDeviceConfiguration(attachment: agent),
            VZVirtioBlockDeviceConfiguration(attachment: config),
        ]
        if binding.bootMode == "efi" {
            // `disk` is a Host-created APFS clone of the digest-pinned base
            // image.  It must be device zero so the EFI boot path can write
            // guest state without ever mounting the immutable image writable.
            configuration.storageDevices = [
                VZVirtioBlockDeviceConfiguration(attachment: overlay),
            ] + readOnlySupportDevices
        } else {
            let baseImage = try diskAttachment(
                path: binding.assets["image"]!.path,
                readOnly: true
            )
            configuration.storageDevices = [
                VZVirtioBlockDeviceConfiguration(attachment: baseImage),
                VZVirtioBlockDeviceConfiguration(attachment: overlay),
            ] + readOnlySupportDevices
        }
        configuration.socketDevices = [VZVirtioSocketDeviceConfiguration()]
        configuration.networkDevices = []
        configuration.directorySharingDevices = []
        configuration.graphicsDevices = []
        configuration.audioDevices = []
        configuration.keyboards = []
        configuration.pointingDevices = []
        if #available(macOS 15.0, *) {
            configuration.usbControllers = []
        }

        let diagnostics = FileHandle(
            fileDescriptor: binding.diagnosticsFD,
            closeOnDealloc: false
        )
        let serial = VZVirtioConsoleDeviceSerialPortConfiguration()
        serial.attachment = VZFileHandleSerialPortAttachment(
            fileHandleForReading: nil,
            fileHandleForWriting: diagnostics
        )
        configuration.serialPorts = [serial]
        return configuration
    }

    private func makeDirectConfiguration(
        _ binding: DirectLaunchBinding,
        diagnosticsFD: Int32
    ) throws -> VZVirtualMachineConfiguration {
        let configuration = VZVirtualMachineConfiguration()
        let bootLoader = VZEFIBootLoader()
        bootLoader.variableStore = VZEFIVariableStore(
            url: URL(fileURLWithPath: binding.efiVariableStore.path)
        )
        configuration.bootLoader = bootLoader
        configuration.platform = VZGenericPlatformConfiguration()
        configuration.cpuCount = binding.cpuCount
        configuration.memorySize = binding.memoryBytes

        // EFI device zero is the unique writable APFS clone.  The pinned base
        // image is Host provenance only and is never attached, so the guest
        // cannot write it even by selecting a different boot target.
        let disk = try diskAttachment(path: binding.disk.path, readOnly: false)
        let agent = try diskAttachment(path: binding.agentSeed.path, readOnly: true)
        let config = try diskAttachment(path: binding.configSeed.path, readOnly: true)
        configuration.storageDevices = [
            VZVirtioBlockDeviceConfiguration(attachment: disk),
            VZVirtioBlockDeviceConfiguration(attachment: agent),
            VZVirtioBlockDeviceConfiguration(attachment: config),
        ]
        configuration.socketDevices = [VZVirtioSocketDeviceConfiguration()]
        configuration.networkDevices = []
        configuration.directorySharingDevices = []
        configuration.graphicsDevices = []
        configuration.audioDevices = []
        configuration.keyboards = []
        configuration.pointingDevices = []
        if #available(macOS 15.0, *) {
            configuration.usbControllers = []
        }
        let serial = VZVirtioConsoleDeviceSerialPortConfiguration()
        serial.attachment = VZFileHandleSerialPortAttachment(
            fileHandleForReading: nil,
            fileHandleForWriting: FileHandle(fileDescriptor: diagnosticsFD, closeOnDealloc: false)
        )
        configuration.serialPorts = [serial]
        return configuration
    }

    private func diskAttachment(
        path: String,
        readOnly: Bool
    ) throws -> VZDiskImageStorageDeviceAttachment {
        let attachment: VZDiskImageStorageDeviceAttachment
        do {
            attachment = try VZDiskImageStorageDeviceAttachment(
                url: URL(fileURLWithPath: path),
                readOnly: readOnly,
                cachingMode: .automatic,
                synchronizationMode: readOnly ? .full : .fsync
            )
        } catch {
            throw HelperError.invalidState("VZ_DISK_ATTACHMENT_FAILED")
        }
        return attachment
    }

    private func start(_ machine: VZMachineHandle, on queue: DispatchQueue) throws {
        let completion = DispatchSemaphore(value: 0)
        let result = LockedResult<Error>()
        queue.async {
            machine.value.start { startResult in
                if case let .failure(error) = startResult { result.value = error }
                completion.signal()
            }
        }
        guard completion.wait(timeout: .now() + launchStartTimeout) == .success else {
            throw HelperError.unavailable("VZ_START_TIMEOUT")
        }
        if result.value != nil {
            throw HelperError.unavailable("VZ_START_FAILED")
        }
    }

    private func stop(_ machine: VZMachineHandle, on queue: DispatchQueue) throws {
        let completion = DispatchSemaphore(value: 0)
        let result = LockedResult<Error>()
        queue.async {
            guard machine.value.canStop else {
                completion.signal()
                return
            }
            machine.value.stop { error in
                if let error { result.value = error }
                completion.signal()
            }
        }
        guard completion.wait(timeout: .now() + stopTimeout) == .success else {
            throw HelperError.invalidState("VZ_STOP_TIMEOUT")
        }
        if result.value != nil {
            throw HelperError.invalidState("VZ_STOP_FAILED")
        }
    }

    private func callGuest(
        machine: VZMachineHandle,
        queue: DispatchQueue,
        binding: LaunchBinding,
        envelope: [String: Any]
    ) throws -> [String: Any] {
        let challenge = Self.freshGuestChallenge()
        var challengedEnvelope = envelope
        challengedEnvelope["guest_challenge"] = challenge
        let requestData = try CanonicalJSON.data(challengedEnvelope) + Data([0x0A])
        let expectation = GuestCallExpectation(
            operation: challengedEnvelope["operation"] as! String,
            requestID: challengedEnvelope["request_id"] as! String,
            domainID: challengedEnvelope["domain_id"] as! String,
            bindingDigests: challengedEnvelope["binding_digests"] as! [String: String],
            guestChallenge: challenge,
            attestationNonce: challengedEnvelope["attestation_nonce"] as? String,
            publicKeyBytes: binding.guestPublicKey
        )
        let guestPort = binding.guestPort
        let completion = DispatchSemaphore(value: 0)
        let result = LockedResult<Result<[String: Any], Error>>()
        queue.async {
            guard let socket = machine.value.socketDevices.first as? VZVirtioSocketDevice else {
                result.value = .failure(HelperError.unavailable("VSOCK_UNAVAILABLE"))
                completion.signal()
                return
            }
            socket.connect(toPort: guestPort) { connectionResult in
                guard case let .success(connection) = connectionResult,
                      connection.fileDescriptor >= 0 else {
                    result.value = .failure(HelperError.unavailable("GUEST_AGENT_UNAVAILABLE"))
                    completion.signal()
                    return
                }
                defer { connection.close() }
                do {
                    try writeAll(requestData, to: connection.fileDescriptor)
                    let responseData = try readBoundedLine(from: connection.fileDescriptor)
                    let response = try CanonicalJSON.object(responseData)
                    try Self.validateGuestResponse(
                        response,
                        expectation: expectation
                    )
                    result.value = .success(response)
                } catch {
                    result.value = .failure(error)
                }
                completion.signal()
            }
        }
        guard completion.wait(timeout: .now() + .seconds(30)) == .success else {
            throw HelperError.unavailable("GUEST_AGENT_TIMEOUT")
        }
        guard let value = result.value else {
            throw HelperError.unavailable("GUEST_AGENT_UNAVAILABLE")
        }
        return try value.get()
    }

    private func callDirectGuest(
        machine: VZMachineHandle,
        queue: DispatchQueue,
        binding: DirectLaunchBinding,
        envelope: [String: Any],
        expectedOperation: String,
        expectedRequestID: String,
        expectedChallenge: String,
        attestationNonce: String?,
        retryForReadiness: Bool = false
    ) throws -> [String: Any] {
        let connectionAttempts = GuestConnectAttemptLimiter(
            maximumInFlight: directGuestMaximumPendingConnects
        )
        if retryForReadiness {
            return try GuestReadinessRetry.run(
                policy: .init(
                    deadline: directGuestReadinessTimeout,
                    initialDelay: directGuestInitialRetryDelay,
                    maximumDelay: directGuestMaximumRetryDelay
                ),
                attempt: { remaining in
                    try self.callDirectGuestOnce(
                        machine: machine,
                        queue: queue,
                        binding: binding,
                        envelope: envelope,
                        expectedOperation: expectedOperation,
                        expectedRequestID: expectedRequestID,
                        expectedChallenge: expectedChallenge,
                        attestationNonce: attestationNonce,
                        timeout: min(remaining, directGuestAttemptTimeout),
                        timeoutErrorCode: "GUEST_AGENT_READINESS_TIMEOUT",
                        connectionAttempts: connectionAttempts
                    )
                },
                isTransient: Self.isTransientGuestReadinessError
            )
        }
        return try callDirectGuestOnce(
            machine: machine,
            queue: queue,
            binding: binding,
            envelope: envelope,
            expectedOperation: expectedOperation,
            expectedRequestID: expectedRequestID,
            expectedChallenge: expectedChallenge,
            attestationNonce: attestationNonce,
            timeout: directGuestOperationTimeout,
            timeoutErrorCode: "GUEST_AGENT_TIMEOUT",
            connectionAttempts: connectionAttempts
        )
    }

    private func callDirectGuestOnce(
        machine: VZMachineHandle,
        queue: DispatchQueue,
        binding: DirectLaunchBinding,
        envelope: [String: Any],
        expectedOperation: String,
        expectedRequestID: String,
        expectedChallenge: String,
        attestationNonce: String?,
        timeout: TimeInterval,
        timeoutErrorCode: String,
        connectionAttempts: GuestConnectAttemptLimiter
    ) throws -> [String: Any] {
        if let code = machine.terminalFailureCode {
            throw HelperError.unavailable(code)
        }
        let requestData = try CanonicalJSON.data(envelope) + Data([0x0A])
        let state = DirectGuestCallState<[String: Any]>()
        guard let attemptLease = connectionAttempts.acquire() else {
            throw HelperError.unavailable("GUEST_AGENT_CONNECT_PENDING")
        }

        // Virtualization requires socket operations to start on the machine's
        // serial queue. `connect` is asynchronous and returns immediately;
        // only its initiation is performed there. Completion I/O below moves
        // off that queue, leaving it available for stop/cleanup if the guest
        // never answers a pending connect.
        queue.async {
            guard !state.isCancelled else {
                attemptLease.release()
                return
            }
            if let code = machine.terminalFailureCode {
                attemptLease.release()
                state.complete(.failure(HelperError.unavailable(code)))
                return
            }
            guard let socket = machine.value.socketDevices.first as? VZVirtioSocketDevice else {
                attemptLease.release()
                state.complete(.failure(HelperError.unavailable("VSOCK_UNAVAILABLE")))
                return
            }
            socket.connect(toPort: binding.guestPort) { connectionResult in
                guard !state.isCancelled else {
                    if case let .success(connection) = connectionResult {
                        connection.close()
                    }
                    attemptLease.release()
                    return
                }
                guard case let .success(connection) = connectionResult,
                      connection.fileDescriptor >= 0 else {
                    attemptLease.release()
                    state.complete(.failure(HelperError.unavailable("GUEST_AGENT_UNAVAILABLE")))
                    return
                }
                let connectionHandle = VZSocketConnectionHandle(connection)
                guard state.setActiveConnection(connectionHandle) else {
                    connection.close()
                    attemptLease.release()
                    return
                }
                // The completion handler may execute on a Virtualization
                // queue. Do not read or validate a guest frame there: both
                // can block until a deadline/cancel closes the descriptor.
                DispatchQueue.global(qos: .userInitiated).async {
                    defer {
                        state.clearActiveConnection(connectionHandle)
                        connectionHandle.value.close()
                        attemptLease.release()
                    }
                    do {
                        guard !state.isCancelled else { return }
                        try writeAll(requestData, to: connectionHandle.value.fileDescriptor)
                        let response = try CanonicalJSON.object(
                            readBoundedLine(from: connectionHandle.value.fileDescriptor)
                        )
                        try Self.validateDirectGuestResponse(
                            response,
                            operation: expectedOperation,
                            requestID: expectedRequestID,
                            domainID: binding.domainID,
                            bindingDigests: binding.bindingDigests,
                            guestChallenge: expectedChallenge,
                            attestationNonce: attestationNonce,
                            publicKeyBytes: binding.guestPublicKey
                        )
                        state.complete(.success(response))
                    } catch {
                        state.complete(.failure(error))
                    }
                }
            }
        }
        let waitMilliseconds = max(1, Int((timeout * 1_000).rounded(.up)))
        guard state.wait(timeout: .milliseconds(waitMilliseconds)) else {
            // This cancellation is also effective before a framework connect
            // callback exists: the asynchronous attempt is logically
            // abandoned, and a late successful connection is immediately
            // closed. `directLaunch` then releases the VM, diagnostics
            // descriptor, and allocation.
            state.cancel()
            if let code = machine.terminalFailureCode {
                throw HelperError.unavailable(code)
            }
            throw HelperError.unavailable(timeoutErrorCode)
        }
        guard let value = state.result else {
            throw HelperError.unavailable("GUEST_AGENT_UNAVAILABLE")
        }
        return try value.get()
    }

    static func isTransientGuestReadinessError(_ error: Error) -> Bool {
        guard case let .unavailable(code) = error as? HelperError else {
            // Schema, protocol-size, and authentication failures are evidence
            // of an invalid or hostile peer, not a guest boot race.
            return false
        }
        return [
            "GUEST_AGENT_UNAVAILABLE",
            "GUEST_AGENT_WRITE_FAILED",
            "GUEST_AGENT_READ_FAILED",
            "GUEST_AGENT_READINESS_TIMEOUT",
            "GUEST_AGENT_CONNECT_PENDING",
        ].contains(code)
    }

    private func attestationData(_ binding: LaunchBinding, state: String) -> [String: Any] {
        attestationData(
            binding,
            state: state,
            hostNonce: "",
            guestArtifactIdentity: ""
        )
    }

    private func attestationData(
        _ binding: LaunchBinding,
        state: String,
        hostNonce: String,
        guestArtifactIdentity: String
    ) -> [String: Any] {
        [
            "domain_id": binding.domainID,
            "backend_id": helperBackendID,
            "backend_digest": helperDigest,
            "platform": Self.platformID(),
            "executable_digest": binding.executableDigest,
            "artifact_digest": binding.artifactDigest,
            "materialization_digest": binding.materializationDigest,
            "isolation_profile": binding.isolationProfile,
            "lease_id": binding.leaseID,
            "reservation_id": binding.reservationID,
            "authenticated_channel": true,
            "nonce_fresh": true,
            "attestation_nonce": hostNonce,
            "guest_artifact_identity": guestArtifactIdentity,
            "state": state,
            "network_devices": 0,
            "directory_shares": 0,
            "binding_digests": binding.bindingDigests,
        ]
    }

    private func attestGuest(_ domain: Domain, hostNonce: String) throws -> String {
        let requestID = "attest-\(domain.binding.domainID)"
        let expectedIdentity = try CanonicalJSON.sha256(
            CanonicalJSON.data(domain.binding.bindingDigests)
        )
        let request: [String: Any] = [
            "protocol": helperProtocol,
            "operation": "attest",
            "request_id": requestID,
            "domain_id": domain.binding.domainID,
            "binding_digests": domain.binding.bindingDigests,
            "attestation_nonce": hostNonce,
        ]
        let response = try callGuest(
            machine: domain.machine,
            queue: domain.queue,
            binding: domain.binding,
            envelope: request
        )
        guard response["success"] as? Bool == true,
              let data = response["data"] as? [String: Any],
              let identity = data["guest_artifact_identity"] as? String,
              identity == expectedIdentity else {
            throw HelperError.unauthenticated
        }
        return identity
    }

    private func validatePendingBridgeRequest(
        _ data: [String: Any],
        requestID: String,
        domain: Domain
    ) throws {
        guard Set(data.keys) == ["state", "host_bridge_request"],
              data["state"] as? String == "pending",
              let bridgeRequest = data["host_bridge_request"] as? [String: Any],
              Set(bridgeRequest.keys) == [
                  "frame", "request_id", "domain_id", "binding_digests", "continuation", "request",
              ],
              bridgeRequest["frame"] as? String == "tobkiri.packvm.bridge.host-request.v1",
              bridgeRequest["request_id"] as? String == requestID,
              bridgeRequest["domain_id"] as? String == domain.binding.domainID,
              bridgeRequest["binding_digests"] as? [String: String]
                == domain.binding.bindingDigests,
              let continuation = bridgeRequest["continuation"] as? String,
              ProtocolAuthenticator.isIdentifier(continuation),
              bridgeRequest["request"] is [String: Any] else {
            throw HelperError.unavailable("INVALID_GUEST_BRIDGE_REQUEST")
        }
    }

    private func validateHostBridgeResult(
        _ result: [String: Any],
        requestID: String,
        domain: Domain
    ) throws {
        guard Set(result.keys) == [
            "frame", "request_id", "domain_id", "binding_digests", "continuation", "result",
        ],
              result["frame"] as? String == "tobkiri.packvm.bridge.host-result.v1",
              result["request_id"] as? String == requestID,
              result["domain_id"] as? String == domain.binding.domainID,
              result["binding_digests"] as? [String: String]
                == domain.binding.bindingDigests,
              let continuation = result["continuation"] as? String,
              ProtocolAuthenticator.isIdentifier(continuation),
              result["result"] is [String: Any] else {
            throw HelperError.invalidRequest("INVALID_HOST_BRIDGE_RESULT")
        }
    }

    private func synchronized<T>(_ body: () -> T) -> T {
        lock.lock()
        defer { lock.unlock() }
        return body()
    }

    private static func platformID() -> String {
        #if arch(arm64)
        return "macos-arm64"
        #elseif arch(x86_64)
        return "macos-amd64"
        #else
        return "macos-unknown"
        #endif
    }

    private static func freshGuestChallenge() -> String {
        var bytes = [UInt8](repeating: 0, count: 32)
        _ = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
        return bytes.map { String(format: "%02x", $0) }.joined()
    }

    private static func validateGuestResponse(
        _ response: [String: Any],
        expectation: GuestCallExpectation
    ) throws {
        guard response["protocol"] as? String == helperProtocol,
              response["operation"] as? String == expectation.operation,
              response["request_id"] as? String == expectation.requestID,
              response["domain_id"] as? String == expectation.domainID,
              response["guest_challenge"] as? String == expectation.guestChallenge,
              response["binding_digests"] as? [String: String]
                == expectation.bindingDigests,
              let signatureEncoded = response["agent_signature"] as? String,
              let signature = Data(base64Encoded: signatureEncoded),
              signature.count == 64 else {
            throw HelperError.unauthenticated
        }
        if let nonce = expectation.attestationNonce,
           response["attestation_nonce"] as? String != nonce {
            throw HelperError.unauthenticated
        }
        var unsigned = response
        unsigned.removeValue(forKey: "agent_signature")
        let key = try Curve25519.Signing.PublicKey(rawRepresentation: expectation.publicKeyBytes)
        guard key.isValidSignature(signature, for: try CanonicalJSON.data(unsigned)) else {
            throw HelperError.unauthenticated
        }
    }

    static func validateDirectGuestResponse(
        _ response: [String: Any],
        operation: String,
        requestID: String,
        domainID: String,
        bindingDigests: [String: String],
        guestChallenge: String,
        attestationNonce: String?,
        publicKeyBytes: Data
    ) throws {
        var expected: Set<String> = [
            "kind", "protocol", "version", "operation", "request_id", "domain_id",
            "binding_digests", "guest_challenge", "success", "agent_signature",
        ]
        guard response["success"] is Bool else {
            throw HelperError.unauthenticated
        }
        expected.insert(response["success"] as? Bool == true ? "data" : "error")
        if attestationNonce != nil { expected.insert("attestation_nonce") }
        guard Set(response.keys) == expected,
              response["kind"] as? String == "tobkiri.packvm.guest.response.v1",
              response["protocol"] as? String == directSupervisorProtocol,
              (response["version"] as? NSNumber)?.intValue == 1,
              response["operation"] as? String == operation,
              response["request_id"] as? String == requestID,
              response["domain_id"] as? String == domainID,
              response["binding_digests"] as? [String: String] == bindingDigests,
              response["guest_challenge"] as? String == guestChallenge,
              let signature = response["agent_signature"] as? String,
              let signatureData = Data(base64Encoded: signature), signatureData.count == 64,
              attestationNonce == nil || response["attestation_nonce"] as? String == attestationNonce else {
            throw HelperError.unauthenticated
        }
        var unsigned = response
        unsigned.removeValue(forKey: "agent_signature")
        guard let key = try? Curve25519.Signing.PublicKey(rawRepresentation: publicKeyBytes),
              let unsignedData = try? CanonicalJSON.data(unsigned),
              key.isValidSignature(signatureData, for: unsignedData) else {
            throw HelperError.unauthenticated
        }
    }

    private static func capabilityFailure(runtimeSupported: Bool, signatureValid: Bool) -> String {
        if !runtimeSupported { return "VIRTUALIZATION_UNAVAILABLE" }
        if !signatureValid { return "HELPER_SIGNATURE_OR_ENTITLEMENT_INVALID" }
        return "UNKNOWN"
    }

    private static func hasValidCodeSignature(_ executableURL: URL) -> Bool {
        var staticCode: SecStaticCode?
        let created = SecStaticCodeCreateWithPath(executableURL as CFURL, [], &staticCode)
        guard created == errSecSuccess, let staticCode else { return false }
        guard SecStaticCodeCheckValidity(staticCode, SecCSFlags(), nil) == errSecSuccess else {
            return false
        }
        var signingInfo: CFDictionary?
        guard SecCodeCopySigningInformation(
            staticCode,
            SecCSFlags(rawValue: kSecCSSigningInformation),
            &signingInfo
        ) == errSecSuccess,
            let info = signingInfo as? [String: Any],
            info[kSecCodeInfoIdentifier as String] as? String == helperDesignatedIdentifier,
            let entitlements = info[kSecCodeInfoEntitlementsDict as String] as? [String: Any],
            entitlements["com.apple.security.virtualization"] as? Bool == true else {
            return false
        }
        return true
    }
}

final class VZMachineLifecycleState: @unchecked Sendable {
    private let lock = NSLock()
    private var terminalCode: String?

    func markGuestStopped() {
        mark("VZ_GUEST_STOPPED")
    }

    func markGuestStoppedWithError() {
        mark("VZ_GUEST_STOPPED_WITH_ERROR")
    }

    var failureCode: String? {
        lock.lock()
        defer { lock.unlock() }
        return terminalCode
    }

    private func mark(_ code: String) {
        lock.lock()
        // Preserve a later error over a generic guest-stop callback, but
        // never clear any terminal state. The caller consequently fails
        // closed before it can issue another vsock retry.
        if terminalCode == nil || code == "VZ_GUEST_STOPPED_WITH_ERROR" {
            terminalCode = code
        }
        lock.unlock()
    }
}

private final class VZMachineLifecycleObserver: NSObject, VZVirtualMachineDelegate {
    private let state: VZMachineLifecycleState
    private let diagnostics: DirectSerialDiagnostics?

    init(state: VZMachineLifecycleState, diagnostics: DirectSerialDiagnostics?) {
        self.state = state
        self.diagnostics = diagnostics
    }

    func guestDidStop(_ virtualMachine: VZVirtualMachine) {
        _ = virtualMachine
        state.markGuestStopped()
        diagnostics?.record("HOST_VM_GUEST_STOPPED")
    }

    func virtualMachine(_ virtualMachine: VZVirtualMachine, didStopWithError error: Error) {
        _ = virtualMachine
        _ = error
        state.markGuestStoppedWithError()
        diagnostics?.record("HOST_VM_STOPPED_WITH_ERROR")
    }
}

private final class VZMachineHandle: @unchecked Sendable {
    let value: VZVirtualMachine
    private let lifecycleState: VZMachineLifecycleState
    // VZVirtualMachine.delegate is weak; retaining this observer is required
    // for terminal state to break a readiness wait rather than timing out.
    private let lifecycleObserver: VZMachineLifecycleObserver

    init(_ value: VZVirtualMachine, diagnostics: DirectSerialDiagnostics? = nil) {
        self.value = value
        lifecycleState = VZMachineLifecycleState()
        lifecycleObserver = VZMachineLifecycleObserver(
            state: lifecycleState,
            diagnostics: diagnostics
        )
        value.delegate = lifecycleObserver
    }

    var terminalFailureCode: String? {
        lifecycleState.failureCode
    }
}

final class VZSocketConnectionHandle: @unchecked Sendable {
    let value: VZVirtioSocketConnection

    init(_ value: VZVirtioSocketConnection) {
        self.value = value
    }
}

private struct GuestCallExpectation: Sendable {
    let operation: String
    let requestID: String
    let domainID: String
    let bindingDigests: [String: String]
    let guestChallenge: String
    let attestationNonce: String?
    let publicKeyBytes: Data
}

/// Test-build-only, bounded capture of the direct guest serial console.
///
/// Production continues to discard serial output.  Debug helper builds retain
/// at most 128 KiB in the already private allocation root so an integration
/// smoke can distinguish EFI/cloud-init/service failures without adding a
/// guest-to-host sharing channel or persisting secret-bearing output in a
/// release bundle.
final class DirectSerialDiagnostics: @unchecked Sendable {
    let writeFD: Int32
    private let lock = NSLock()
    private var writeClosed = false

#if DEBUG
    private let readerFD: Int32
    private let captureFD: Int32
    private let readerDone = DispatchSemaphore(value: 0)
#endif

#if DEBUG
    private init(
        writeFD: Int32,
        readerFD: Int32,
        captureFD: Int32
    ) {
        self.writeFD = writeFD
        self.readerFD = readerFD
        self.captureFD = captureFD
    }
#else
    private init(writeFD: Int32) {
        self.writeFD = writeFD
    }
#endif

    static func create(runRoot: String) throws -> DirectSerialDiagnostics {
#if DEBUG
        let root = try SecureLaunchAssetValidator.secureRunRoot(runRoot)
        let capturePath = root + "/serial-console.log"
        let captureFD = open(
            capturePath,
            O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW,
            S_IRUSR | S_IWUSR
        )
        guard captureFD >= 0 else {
            throw HelperError.invalidState("SERIAL_DIAGNOSTICS_CREATE_FAILED")
        }
        var descriptors: [Int32] = [0, 0]
        guard pipe(&descriptors) == 0 else {
            _ = Darwin.close(captureFD)
            _ = unlink(capturePath)
            throw HelperError.invalidState("SERIAL_DIAGNOSTICS_CREATE_FAILED")
        }
        let diagnostics = DirectSerialDiagnostics(
            writeFD: descriptors[1],
            readerFD: descriptors[0],
            captureFD: captureFD
        )
        DispatchQueue.global(qos: .utility).async {
            diagnostics.captureBoundedSerialOutput()
        }
        return diagnostics
#else
        let descriptor = open("/dev/null", O_WRONLY | O_CLOEXEC | O_NOFOLLOW)
        guard descriptor >= 0 else {
            throw HelperError.invalidState("SERIAL_DIAGNOSTICS_CREATE_FAILED")
        }
        return DirectSerialDiagnostics(writeFD: descriptor)
#endif
    }

    func close() {
        lock.lock()
        let shouldClose = !writeClosed
        writeClosed = true
        lock.unlock()
        guard shouldClose else { return }
        _ = Darwin.close(writeFD)
#if DEBUG
        _ = readerDone.wait(timeout: .now() + .seconds(2))
#endif
    }

    /// Write a fixed, non-secret Host milestone into the debug-only capture.
    ///
    /// The same pipe is attached to the guest console, so a single short
    /// write preserves bounded framing with ordinary serial output.  Release
    /// helpers intentionally do not retain any diagnostic output.
    func record(_ marker: StaticString) {
#if DEBUG
        let data = Data("TOBKIRI_HOST:\(marker)\n".utf8)
        _ = data.withUnsafeBytes { bytes in
            Darwin.write(writeFD, bytes.baseAddress, bytes.count)
        }
#else
        _ = marker
#endif
    }

#if DEBUG
    private func captureBoundedSerialOutput() {
        defer {
            _ = Darwin.close(readerFD)
            _ = fsync(captureFD)
            _ = Darwin.close(captureFD)
            readerDone.signal()
        }
        var remaining = directSerialDiagnosticsMaximumBytes
        var bytes = [UInt8](repeating: 0, count: 4096)
        while true {
            let received = bytes.withUnsafeMutableBufferPointer { buffer in
                Darwin.read(readerFD, buffer.baseAddress, buffer.count)
            }
            if received > 0 {
                let count = min(Int(received), remaining)
                if count > 0 {
                    var offset = 0
                    while offset < count {
                        let written = bytes.withUnsafeBytes { buffer in
                            Darwin.write(
                                captureFD,
                                buffer.baseAddress!.advanced(by: offset),
                                count - offset
                            )
                        }
                        guard written > 0 else { return }
                        offset += Int(written)
                    }
                    remaining -= count
                }
                continue
            }
            if received == -1, errno == EINTR { continue }
            return
        }
    }
#endif
}

/// Bounded retry policy for the guest service's first vsock connection.
///
/// This deliberately retries only errors selected by the caller. In
/// particular, response authentication and protocol validation failures must
/// be returned to the Host immediately rather than being hidden by a retry.
struct GuestReadinessRetry {
    struct Policy {
        let deadline: TimeInterval
        let initialDelay: TimeInterval
        let maximumDelay: TimeInterval

        init(
            deadline: TimeInterval,
            initialDelay: TimeInterval,
            maximumDelay: TimeInterval
        ) {
            self.deadline = deadline
            self.initialDelay = initialDelay
            self.maximumDelay = maximumDelay
        }
    }

    static func run<Value>(
        policy: Policy,
        now: () -> TimeInterval = { ProcessInfo.processInfo.systemUptime },
        sleep: (TimeInterval) -> Void = { Thread.sleep(forTimeInterval: $0) },
        attempt: (TimeInterval) throws -> Value,
        isTransient: (Error) -> Bool
    ) throws -> Value {
        precondition(policy.deadline > 0)
        precondition(policy.initialDelay > 0)
        precondition(policy.maximumDelay >= policy.initialDelay)

        let deadline = now() + policy.deadline
        var nextDelay = policy.initialDelay

        while true {
            let remaining = deadline - now()
            guard remaining > 0 else {
                throw HelperError.unavailable("GUEST_AGENT_TIMEOUT")
            }

            do {
                return try attempt(remaining)
            } catch {
                guard isTransient(error) else { throw error }

                let remainingAfterAttempt = deadline - now()
                guard remainingAfterAttempt > 0 else {
                    throw HelperError.unavailable("GUEST_AGENT_TIMEOUT")
                }
                sleep(min(nextDelay, remainingAfterAttempt))
                nextDelay = min(nextDelay * 2, policy.maximumDelay)
            }
        }
    }
}

/// Tracks framework connects that have been issued but have not yet invoked
/// their completion handler. A VZ connect has no cancellation API before it
/// returns a `VZVirtioSocketConnection`; retaining a small second slot lets a
/// cold boot recover from one abandoned framework call without creating an
/// unbounded number of blocked transport workers.
final class GuestConnectAttemptLimiter: @unchecked Sendable {
    final class Lease: @unchecked Sendable {
        private let owner: GuestConnectAttemptLimiter
        private let lock = NSLock()
        private var released = false

        fileprivate init(owner: GuestConnectAttemptLimiter) {
            self.owner = owner
        }

        func release() {
            lock.lock()
            defer { lock.unlock() }
            guard !released else { return }
            released = true
            owner.releaseOne()
        }
    }

    private let maximumInFlight: Int
    private let lock = NSLock()
    private var active = 0

    init(maximumInFlight: Int) {
        precondition(maximumInFlight > 0)
        self.maximumInFlight = maximumInFlight
    }

    func acquire() -> Lease? {
        lock.lock()
        defer { lock.unlock() }
        guard active < maximumInFlight else { return nil }
        active += 1
        return Lease(owner: self)
    }

    var inFlight: Int {
        lock.lock()
        defer { lock.unlock() }
        return active
    }

    private func releaseOne() {
        lock.lock()
        defer { lock.unlock() }
        precondition(active > 0)
        active -= 1
    }
}

/// One direct guest request's completion and cancellation state.
///
/// The state is deliberately independent of the VZ machine queue. A timeout
/// can therefore abandon a pending framework connect before a connection
/// object exists, while a late successful connection is closed without guest
/// I/O. Once a connection exists, cancellation closes its descriptor to wake
/// the isolated read worker.
final class DirectGuestCallState<Value>: @unchecked Sendable {
    private let lock = NSLock()
    private let completion = DispatchSemaphore(value: 0)
    private var storedResult: Result<Value, Error>?
    private var cancelled = false
    private var activeConnection: VZSocketConnectionHandle?

    var isCancelled: Bool {
        lock.lock()
        defer { lock.unlock() }
        return cancelled
    }

    var result: Result<Value, Error>? {
        lock.lock()
        defer { lock.unlock() }
        return storedResult
    }

    func setActiveConnection(_ connection: VZSocketConnectionHandle) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard !cancelled, storedResult == nil else { return false }
        activeConnection = connection
        return true
    }

    func clearActiveConnection(_ connection: VZSocketConnectionHandle) {
        lock.lock()
        defer { lock.unlock() }
        if activeConnection === connection {
            activeConnection = nil
        }
    }

    func complete(_ value: Result<Value, Error>) {
        lock.lock()
        guard !cancelled, storedResult == nil else {
            lock.unlock()
            return
        }
        storedResult = value
        lock.unlock()
        completion.signal()
    }

    func wait(timeout: DispatchTimeInterval) -> Bool {
        completion.wait(timeout: .now() + timeout) == .success
    }

    func cancel() {
        lock.lock()
        guard !cancelled else {
            lock.unlock()
            return
        }
        cancelled = true
        let connection = activeConnection
        activeConnection = nil
        lock.unlock()
        connection?.value.close()
    }
}

private final class LockedResult<Value>: @unchecked Sendable {
    private let lock = NSLock()
    private var stored: Value?

    var value: Value? {
        get {
            lock.lock()
            defer { lock.unlock() }
            return stored
        }
        set {
            lock.lock()
            stored = newValue
            lock.unlock()
        }
    }
}

private enum SecureLaunchAssetValidatorDigest {
    static func digest(_ path: String) throws -> String {
        let data = try Data(contentsOf: URL(fileURLWithPath: path), options: [.mappedIfSafe])
        return CanonicalJSON.sha256(data)
    }
}

private func writeAll(_ data: Data, to descriptor: Int32) throws {
    try data.withUnsafeBytes { rawBuffer in
        guard let start = rawBuffer.baseAddress else { return }
        var offset = 0
        while offset < rawBuffer.count {
            let written = Darwin.write(descriptor, start.advanced(by: offset), rawBuffer.count - offset)
            if written > 0 {
                offset += written
                continue
            }
            if written == -1, errno == EINTR { continue }
            throw HelperError.unavailable("GUEST_AGENT_WRITE_FAILED")
        }
    }
}

private func readBoundedLine(from descriptor: Int32) throws -> Data {
    var output = Data()
    var byte: UInt8 = 0
    while output.count <= maxProtocolLineBytes {
        let readCount = Darwin.read(descriptor, &byte, 1)
        if readCount == 1 {
            if byte == 0x0A { return output }
            output.append(byte)
            continue
        }
        if readCount == -1, errno == EINTR { continue }
        throw HelperError.unavailable("GUEST_AGENT_READ_FAILED")
    }
    throw HelperError.protocolTooLarge
}
