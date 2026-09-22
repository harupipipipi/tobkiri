import CryptoKit
import CoreFoundation
import Darwin
import Foundation
import Virtualization

public struct AssetDescriptor: Equatable, Sendable {
    public let path: String
    public let digest: String

    public init(path: String, digest: String) {
        self.path = path
        self.digest = digest
    }

    static func parse(_ value: Any, name: String) throws -> AssetDescriptor {
        guard let object = value as? [String: Any],
              Set(object.keys) == ["path", "digest"],
              let path = object["path"] as? String,
              let digest = object["digest"] as? String,
              ProtocolAuthenticator.isSHA256Digest(digest) else {
            throw HelperError.invalidRequest("INVALID_\(name.uppercased())_ASSET")
        }
        return AssetDescriptor(path: path, digest: digest)
    }
}

public struct LaunchBinding: Equatable {
    public let bootMode: String
    public let domainID: String
    public let domainDigest: String
    public let leaseID: String
    public let leaseDigest: String
    public let reservationID: String
    public let reservationDigest: String
    public let executableDigest: String
    public let artifactDigest: String
    public let materializationDigest: String
    public let isolationProfile: String
    public let runRoot: String
    public let cpuCount: Int
    public let memoryBytes: UInt64
    public let kernelCommandLine: String
    public let diagnosticsFD: Int32
    public let guestPort: UInt32
    public let guestPublicKey: Data
    public let guestPublicKeyDigest: String
    public let assets: [String: AssetDescriptor]
    public let efiVariableStore: AssetDescriptor?

    public static func assetNames(for bootMode: String) -> [String] {
        switch bootMode {
        case "efi":
            ["image", "agent", "config", "disk"]
        case "linux":
            ["image", "kernel", "initrd", "agent", "config", "disk"]
        default:
            []
        }
    }

    public static func parse(_ value: Any) throws -> LaunchBinding {
        guard let object = value as? [String: Any] else {
            throw HelperError.invalidRequest("INVALID_LAUNCH")
        }
        let baseKeys: Set<String> = [
            "boot_mode",
            "domain_id", "domain_digest", "lease_id", "lease_digest",
            "reservation_id", "reservation_digest", "executable_digest",
            "artifact_digest", "materialization_digest", "isolation_profile",
            "run_root", "cpu_count", "memory_bytes", "kernel_command_line",
            "diagnostics_fd", "guest_port", "guest_public_key",
            "guest_public_key_digest", "assets",
        ]
        guard let bootMode = object["boot_mode"] as? String,
              bootMode == "linux" || bootMode == "efi" else {
            throw HelperError.invalidRequest("INVALID_BOOT_MODE")
        }
        let expectedKeys = bootMode == "efi"
            ? baseKeys.union(["efi_variable_store"])
            : baseKeys
        guard Set(object.keys) == expectedKeys,
              let domainID = object["domain_id"] as? String,
              let domainDigest = object["domain_digest"] as? String,
              let leaseID = object["lease_id"] as? String,
              let leaseDigest = object["lease_digest"] as? String,
              let reservationID = object["reservation_id"] as? String,
              let reservationDigest = object["reservation_digest"] as? String,
              let executableDigest = object["executable_digest"] as? String,
              let artifactDigest = object["artifact_digest"] as? String,
              let materializationDigest = object["materialization_digest"] as? String,
              let isolationProfile = object["isolation_profile"] as? String,
              let runRoot = object["run_root"] as? String,
              let cpuNumber = object["cpu_count"] as? NSNumber,
              let memoryNumber = object["memory_bytes"] as? NSNumber,
              let kernelCommandLine = object["kernel_command_line"] as? String,
              let diagnosticsNumber = object["diagnostics_fd"] as? NSNumber,
              let guestPortNumber = object["guest_port"] as? NSNumber,
              let guestPublicKeyEncoded = object["guest_public_key"] as? String,
              let guestPublicKey = Data(base64Encoded: guestPublicKeyEncoded),
              guestPublicKey.count == 32,
              let guestPublicKeyDigest = object["guest_public_key_digest"] as? String,
              let rawAssets = object["assets"] as? [String: Any],
              Set(rawAssets.keys) == Set(assetNames(for: bootMode)),
              ProtocolAuthenticator.isIdentifier(domainID),
              ProtocolAuthenticator.isIdentifier(leaseID),
              ProtocolAuthenticator.isIdentifier(reservationID),
              ProtocolAuthenticator.isSHA256Digest(domainDigest),
              ProtocolAuthenticator.isSHA256Digest(leaseDigest),
              ProtocolAuthenticator.isSHA256Digest(reservationDigest),
              ProtocolAuthenticator.isSHA256Digest(executableDigest),
              ProtocolAuthenticator.isSHA256Digest(artifactDigest),
              ProtocolAuthenticator.isSHA256Digest(materializationDigest),
              ProtocolAuthenticator.isSHA256Digest(guestPublicKeyDigest),
              isolationProfile.utf8.count <= 128,
              !isolationProfile.isEmpty,
              kernelCommandLine.utf8.count <= 4 * 1024,
              cpuNumber.stringValue == String(cpuNumber.intValue),
              diagnosticsNumber.stringValue == String(diagnosticsNumber.intValue),
              guestPortNumber.stringValue == String(guestPortNumber.uint32Value) else {
            throw HelperError.invalidRequest("INVALID_LAUNCH")
        }
        let cpuCount = cpuNumber.intValue
        let diagnosticsFD = diagnosticsNumber.int32Value
        let guestPort = guestPortNumber.uint32Value
        guard diagnosticsNumber.int64Value == Int64(diagnosticsFD), guestPort > 1023 else {
            throw HelperError.invalidRequest("INVALID_LAUNCH")
        }
        let memoryBytes = memoryNumber.uint64Value
        guard memoryNumber.stringValue == String(memoryBytes) else {
            throw HelperError.invalidRequest("INVALID_MEMORY")
        }
        guard domainDigest == CanonicalJSON.sha256Text(domainID),
              leaseDigest == CanonicalJSON.sha256Text(leaseID),
              reservationDigest == CanonicalJSON.sha256Text(reservationID) else {
            throw HelperError.invalidRequest("BINDING_DIGEST_MISMATCH")
        }
        guard guestPublicKeyDigest == CanonicalJSON.sha256(guestPublicKey) else {
            throw HelperError.invalidRequest("GUEST_PUBLIC_KEY_DIGEST_MISMATCH")
        }
        var assets: [String: AssetDescriptor] = [:]
        for name in assetNames(for: bootMode) {
            assets[name] = try AssetDescriptor.parse(rawAssets[name] as Any, name: name)
        }
        let efiVariableStore = try bootMode == "efi"
            ? AssetDescriptor.parse(object["efi_variable_store"] as Any, name: "efi_variable_store")
            : nil
        return LaunchBinding(
            bootMode: bootMode,
            domainID: domainID,
            domainDigest: domainDigest,
            leaseID: leaseID,
            leaseDigest: leaseDigest,
            reservationID: reservationID,
            reservationDigest: reservationDigest,
            executableDigest: executableDigest,
            artifactDigest: artifactDigest,
            materializationDigest: materializationDigest,
            isolationProfile: isolationProfile,
            runRoot: runRoot,
            cpuCount: cpuCount,
            memoryBytes: memoryBytes,
            kernelCommandLine: kernelCommandLine,
            diagnosticsFD: diagnosticsFD,
            guestPort: guestPort,
            guestPublicKey: guestPublicKey,
            guestPublicKeyDigest: guestPublicKeyDigest,
            assets: assets,
            efiVariableStore: efiVariableStore
        )
    }

    public var bindingDigests: [String: String] {
        [
            "domain": domainDigest,
            "lease": leaseDigest,
            "reservation": reservationDigest,
            "image": assets["image"]!.digest,
            "agent": assets["agent"]!.digest,
            "config": assets["config"]!.digest,
            "disk": assets["disk"]!.digest,
            "guest_public_key": guestPublicKeyDigest,
            "artifact": artifactDigest,
            "executable": executableDigest,
            "materialization": materializationDigest,
        ].merging(
            bootMode == "linux"
                ? [
                    "kernel": assets["kernel"]!.digest,
                    "initrd": assets["initrd"]!.digest,
                ]
                : [:],
            uniquingKeysWith: { _, replacement in replacement }
        ).merging(
            efiVariableStore.map { ["efi_variable_store": $0.digest] } ?? [:],
            uniquingKeysWith: { _, replacement in replacement }
        )
    }
}

/// The production Host binding.  Unlike the historical JSONL binding above,
/// the immutable base image is provenance only: EFI boots the Host-created
/// writable clone and the base never appears in a VZ storage device.
public struct DirectLaunchBinding: Equatable, Sendable {
    public let domainID: String
    public let leaseID: String
    public let reservationID: String
    public let runRoot: String
    public let cpuCount: Int
    public let memoryBytes: UInt64
    public let guestPort: UInt32
    public let agentSeed: AssetDescriptor
    public let configSeed: AssetDescriptor
    public let disk: AssetDescriptor
    public let efiVariableStore: AssetDescriptor
    public let baseImageDigest: String
    public let guestPublicKey: Data
    public let guestPublicKeyDigest: String
    public let bindingDigests: [String: String]
    public let artifactDigest: String
    public let executableDigest: String
    public let materializationDigest: String
    public let expiresMonotonicNanoseconds: Int64
    public let isolationProfile: String

    public static func parse(_ value: Any) throws -> DirectLaunchBinding {
        guard let object = value as? [String: Any],
              Set(object.keys) == [
                  "kind", "version", "backend_id", "backend_digest", "platform",
                  "helper", "launch_assets", "domain_allocation", "agent_code_digest",
                  "runtime", "binding_digests", "artifact", "domain_id",
                  "isolation_profile", "lease", "reservation_id",
              ],
              object["kind"] as? String == "tobkiri.macos-vz.launch-binding.v1",
              (object["version"] as? NSNumber)?.intValue == 1,
              let backendID = object["backend_id"] as? String,
              let backendDigest = object["backend_digest"] as? String,
              object["platform"] as? String == "macos-arm64",
              ProtocolAuthenticator.isIdentifier(backendID),
              ProtocolAuthenticator.isSHA256Digest(backendDigest),
              let domainID = object["domain_id"] as? String,
              let reservationID = object["reservation_id"] as? String,
              let isolationProfile = object["isolation_profile"] as? String,
              ProtocolAuthenticator.isIdentifier(domainID),
              ProtocolAuthenticator.isIdentifier(reservationID),
              !isolationProfile.isEmpty, isolationProfile.utf8.count <= 128,
              let assets = object["launch_assets"] as? [String: Any],
              let allocation = object["domain_allocation"] as? [String: Any],
              let runtime = object["runtime"] as? [String: Any],
              let bindingDigests = object["binding_digests"] as? [String: String],
              let artifact = object["artifact"] as? [String: Any],
              let lease = object["lease"] as? [String: Any] else {
            throw HelperError.invalidRequest("INVALID_DIRECT_LAUNCH_BINDING")
        }
        guard let helper = object["helper"] as? [String: Any],
              Set(helper.keys) == ["code_digest", "bundle_id", "team_id", "signing_identity"],
              let helperDigest = helper["code_digest"] as? String,
              ProtocolAuthenticator.isSHA256Digest(helperDigest),
              helper["bundle_id"] as? String == "dev.tobkiri.launcher.packvm-vz-helper",
              helper["team_id"] is String,
              helper["signing_identity"] is String,
              let agentCodeDigest = object["agent_code_digest"] as? String,
              ProtocolAuthenticator.isSHA256Digest(agentCodeDigest) else {
            throw HelperError.invalidRequest("INVALID_DIRECT_HELPER_BINDING")
        }
        guard Set(assets.keys) == [
            "base_image_digest", "agent_template_digest", "config_template_digest",
            "base_image_read_only", "boot_mode",
        ],
              assets["boot_mode"] as? String == "efi",
              assets["base_image_read_only"] as? Bool == true,
              let baseImageDigest = assets["base_image_digest"] as? String,
              ProtocolAuthenticator.isSHA256Digest(baseImageDigest),
              let agentTemplateDigest = assets["agent_template_digest"] as? String,
              let configTemplateDigest = assets["config_template_digest"] as? String,
              ProtocolAuthenticator.isSHA256Digest(agentTemplateDigest),
              ProtocolAuthenticator.isSHA256Digest(configTemplateDigest) else {
            throw HelperError.invalidRequest("INVALID_DIRECT_LAUNCH_ASSETS")
        }
        guard Set(allocation.keys) == [
            "domain_id", "reservation_id", "lease_id", "run_root", "cow_disk_path",
            "cow_disk_digest", "efi_store_path", "efi_variable_store_digest",
            "agent_seed_path", "agent_seed_digest", "config_seed_path",
            "config_seed_digest", "guest_public_key", "guest_public_key_digest",
        ],
              allocation["domain_id"] as? String == domainID,
              allocation["reservation_id"] as? String == reservationID,
              let leaseID = allocation["lease_id"] as? String,
              ProtocolAuthenticator.isIdentifier(leaseID),
              let runRoot = allocation["run_root"] as? String,
              let agentSeedPath = allocation["agent_seed_path"] as? String,
              let agentSeedDigest = allocation["agent_seed_digest"] as? String,
              let configSeedPath = allocation["config_seed_path"] as? String,
              let configSeedDigest = allocation["config_seed_digest"] as? String,
              let diskPath = allocation["cow_disk_path"] as? String,
              let diskDigest = allocation["cow_disk_digest"] as? String,
              let efiPath = allocation["efi_store_path"] as? String,
              let efiDigest = allocation["efi_variable_store_digest"] as? String,
              let publicKeyEncoded = allocation["guest_public_key"] as? String,
              let publicKey = decodeURLSafeBase64(publicKeyEncoded), publicKey.count == 32,
              let publicKeyDigest = allocation["guest_public_key_digest"] as? String,
              [agentSeedDigest, configSeedDigest, diskDigest, efiDigest, publicKeyDigest]
                .allSatisfy(ProtocolAuthenticator.isSHA256Digest) else {
            throw HelperError.invalidRequest("INVALID_DIRECT_DOMAIN_ALLOCATION")
        }
        guard Set(runtime.keys) == ["cpu_count", "memory_bytes", "guest_vsock_port"],
              let cpuNumber = runtime["cpu_count"] as? NSNumber,
              let memoryNumber = runtime["memory_bytes"] as? NSNumber,
              let portNumber = runtime["guest_vsock_port"] as? NSNumber,
              cpuNumber.stringValue == String(cpuNumber.intValue),
              memoryNumber.stringValue == String(memoryNumber.uint64Value),
              portNumber.stringValue == String(portNumber.uint32Value),
              portNumber.uint32Value == 19001 else {
            throw HelperError.invalidRequest("INVALID_DIRECT_RUNTIME")
        }
        let cpuCount = cpuNumber.intValue
        let memoryBytes = memoryNumber.uint64Value
        try SecureLaunchAssetValidator.validateResourceBounds(
            cpuCount: cpuCount,
            memoryBytes: memoryBytes
        )
        guard Set(artifact.keys) == [
            "artifact_digest", "executable_digest", "materialization_digest",
            "guest_payload_digest",
        ],
              let artifactDigest = artifact["artifact_digest"] as? String,
              let executableDigest = artifact["executable_digest"] as? String,
              let materializationDigest = artifact["materialization_digest"] as? String,
              let guestPayloadDigest = artifact["guest_payload_digest"] as? String,
              ProtocolAuthenticator.isSHA256Digest(artifactDigest),
              ProtocolAuthenticator.isSHA256Digest(executableDigest),
              ProtocolAuthenticator.isSHA256Digest(materializationDigest),
              ProtocolAuthenticator.isSHA256Digest(guestPayloadDigest),
              Set(lease.keys) == ["lease_id", "reservation_id", "expires_monotonic_ns"],
              lease["lease_id"] as? String == leaseID,
              lease["reservation_id"] as? String == reservationID,
              let expires = lease["expires_monotonic_ns"] as? NSNumber,
              let expiresNanoseconds = positiveInt64(expires) else {
            throw HelperError.invalidRequest("INVALID_DIRECT_ARTIFACT")
        }
        let expectedBindings: [String: String] = [
            "domain": CanonicalJSON.sha256Text(domainID),
            "lease": CanonicalJSON.sha256Text(leaseID),
            "reservation": CanonicalJSON.sha256Text(reservationID),
            "image": baseImageDigest,
            "agent": agentCodeDigest,
            "config": configTemplateDigest,
            "disk": diskDigest,
            "efi_variable_store": efiDigest,
            "guest_public_key": CanonicalJSON.sha256(publicKey),
            "artifact": artifactDigest,
            "executable": executableDigest,
            "materialization": materializationDigest,
        ]
        guard bindingDigests == expectedBindings,
              publicKeyDigest == expectedBindings["guest_public_key"] else {
            throw HelperError.invalidRequest("DIRECT_BINDING_DIGEST_MISMATCH")
        }
        return DirectLaunchBinding(
            domainID: domainID,
            leaseID: leaseID,
            reservationID: reservationID,
            runRoot: runRoot,
            cpuCount: cpuCount,
            memoryBytes: memoryBytes,
            guestPort: portNumber.uint32Value,
            agentSeed: AssetDescriptor(path: agentSeedPath, digest: agentSeedDigest),
            configSeed: AssetDescriptor(path: configSeedPath, digest: configSeedDigest),
            disk: AssetDescriptor(path: diskPath, digest: diskDigest),
            efiVariableStore: AssetDescriptor(path: efiPath, digest: efiDigest),
            baseImageDigest: baseImageDigest,
            guestPublicKey: publicKey,
            guestPublicKeyDigest: publicKeyDigest,
            bindingDigests: bindingDigests,
            artifactDigest: artifactDigest,
            executableDigest: executableDigest,
            materializationDigest: materializationDigest,
            expiresMonotonicNanoseconds: expiresNanoseconds,
            isolationProfile: isolationProfile
        )
    }

    /// Decode the lease expiry without accepting JSON booleans, floats, or a
    /// lossy conversion. The direct driver sends monotonic nanoseconds as a
    /// positive signed 64-bit JSON integer so it remains canonical and exact.
    private static func positiveInt64(_ number: NSNumber) -> Int64? {
        guard CFGetTypeID(number) != CFBooleanGetTypeID(),
              !CFNumberIsFloatType(number),
              number.int64Value > 0,
              number.stringValue == String(number.int64Value) else {
            return nil
        }
        return number.int64Value
    }

    private static func decodeURLSafeBase64(_ value: String) -> Data? {
        // The Host allocation serializes this public key as standard padded
        // Base64. Accept URL-safe form too so the helper's parser remains
        // compatible with the sidecar transport, but never accept whitespace.
        if !value.contains(where: { $0.isWhitespace }), let decoded = Data(base64Encoded: value) {
            return decoded
        }
        guard !value.isEmpty,
              value.unicodeScalars.allSatisfy({
                  CharacterSet.alphanumerics.union(
                      CharacterSet(charactersIn: "-_")
                  ).contains($0)
              }) else {
            return nil
        }
        let standard = value.replacingOccurrences(of: "-", with: "+")
            .replacingOccurrences(of: "_", with: "/")
        return Data(base64Encoded: standard + String(repeating: "=", count: (4 - standard.count % 4) % 4))
    }
}

public struct ValidatedLaunchAssets: Equatable {
    fileprivate let mutableFiles: [String: FileIdentity]
}

public struct ValidatedDirectLaunchAssets: Equatable {
    fileprivate let mutableFiles: [String: FileIdentity]
}

public struct PreparedEFIStore: Equatable {
    public let descriptor: AssetDescriptor
    public let device: UInt64
    public let inode: UInt64
    fileprivate let identity: FileIdentity
}

private struct FileIdentity: Equatable {
    let device: UInt64
    let inode: UInt64
}

public enum SecureLaunchAssetValidator {
    public static func prepareEFIStore(
        runRoot: String,
        path: String
    ) throws -> PreparedEFIStore {
        let root = try secureRunRoot(runRoot)
        guard path.hasPrefix("/"),
              URL(fileURLWithPath: path).standardizedFileURL.path == path,
              isWithin(path, root: root) else {
            throw HelperError.invalidAsset("EFI_VARIABLE_STORE_PATH_INVALID")
        }
        let parent = URL(fileURLWithPath: path).deletingLastPathComponent().path
        try rejectSymlinkComponents(parent)
        var existing = stat()
        guard lstat(path, &existing) == -1, errno == ENOENT else {
            throw HelperError.invalidAsset("EFI_VARIABLE_STORE_ALREADY_EXISTS")
        }
        do {
            _ = try VZEFIVariableStore(
                creatingVariableStoreAt: URL(fileURLWithPath: path),
                options: .allowOverwrite
            )
        } catch {
            throw HelperError.invalidState("EFI_VARIABLE_STORE_CREATE_FAILED")
        }
        do {
            try rejectSymlinkComponents(path)
            let created = try fileStat(path)
            guard (created.st_mode & S_IFMT) == S_IFREG,
                  created.st_nlink == 1,
                  created.st_uid == geteuid(),
                  (created.st_mode & S_IWOTH) == 0 else {
                throw HelperError.invalidAsset("EFI_VARIABLE_STORE_INSECURE")
            }
            let digest = try digestRegularFile(path, expected: created)
            let fileIdentity = identity(created)
            return PreparedEFIStore(
                descriptor: AssetDescriptor(path: path, digest: digest),
                device: fileIdentity.device,
                inode: fileIdentity.inode,
                identity: fileIdentity
            )
        } catch {
            _ = unlink(path)
            throw error
        }
    }

    public static func removePreparedEFIStore(
        _ prepared: PreparedEFIStore,
        runRoot: String
    ) throws {
        let root = try secureRunRoot(runRoot)
        guard isWithin(prepared.descriptor.path, root: root) else {
            throw HelperError.invalidAsset("EFI_VARIABLE_STORE_OUTSIDE_RUN_ROOT")
        }
        try rejectSymlinkComponents(prepared.descriptor.path)
        try removeMutableFile(
            prepared.descriptor.path,
            expected: prepared.identity,
            code: "EFI_VARIABLE_STORE"
        )
    }
    @discardableResult
    public static func validate(_ binding: LaunchBinding) throws -> ValidatedLaunchAssets {
        let root = try secureRunRoot(binding.runRoot)
        let disk = binding.assets["disk"]!
        guard isWithin(disk.path, root: root) else {
            throw HelperError.invalidAsset("OVERLAY_OUTSIDE_RUN_ROOT")
        }
        for name in LaunchBinding.assetNames(for: binding.bootMode) {
            try validate(asset: binding.assets[name]!, name: name)
        }
        let efiVariableStore = binding.efiVariableStore
        if let efiVariableStore {
            guard isWithin(efiVariableStore.path, root: root) else {
                throw HelperError.invalidAsset("EFI_VARIABLE_STORE_OUTSIDE_RUN_ROOT")
            }
            try validate(asset: efiVariableStore, name: "efi_variable_store")
        }
        let diskStat = try fileStat(disk.path)
        guard diskStat.st_size > 0, diskStat.st_size % 512 == 0 else {
            throw HelperError.invalidAsset("INVALID_COW_OVERLAY")
        }
        guard binding.assets["image"]!.path != disk.path else {
            throw HelperError.invalidAsset("COW_OVERLAY_MUST_DIFFER_FROM_IMAGE")
        }
        for name in ["image", "agent", "config"] {
            let statValue = try fileStat(binding.assets[name]!.path)
            guard statValue.st_size > 0, statValue.st_size % 512 == 0 else {
                throw HelperError.invalidAsset("\(name.uppercased())_MUST_BE_RAW_DISK")
            }
        }
        if binding.bootMode == "efi", binding.assets["disk"]!.digest != binding.assets["image"]!.digest {
            throw HelperError.invalidAsset("EFI_OVERLAY_MUST_MATCH_BASE_IMAGE")
        }
        guard binding.diagnosticsFD >= 3, fcntl(binding.diagnosticsFD, F_GETFD) != -1 else {
            throw HelperError.invalidRequest("INVALID_DIAGNOSTICS_FD")
        }
        try validateResourceBounds(binding)
        var mutableFiles = ["disk": identity(diskStat)]
        if let efiVariableStore {
            mutableFiles["efi_variable_store"] = identity(try fileStat(efiVariableStore.path))
        }
        return ValidatedLaunchAssets(mutableFiles: mutableFiles)
    }

    @discardableResult
    public static func validate(_ binding: DirectLaunchBinding) throws -> ValidatedDirectLaunchAssets {
        let root = try secureRunRoot(binding.runRoot)
        for (name, asset) in [
            ("agent_seed", binding.agentSeed),
            ("config_seed", binding.configSeed),
            ("disk", binding.disk),
            ("efi_variable_store", binding.efiVariableStore),
        ] {
            guard isWithin(asset.path, root: root) else {
                throw HelperError.invalidAsset("\(name.uppercased())_OUTSIDE_RUN_ROOT")
            }
            try validate(asset: asset, name: name)
        }
        let diskStat = try fileStat(binding.disk.path)
        guard diskStat.st_size > 0, diskStat.st_size % 512 == 0 else {
            throw HelperError.invalidAsset("INVALID_COW_OVERLAY")
        }
        for (name, asset) in [("agent_seed", binding.agentSeed), ("config_seed", binding.configSeed)] {
            let statValue = try fileStat(asset.path)
            guard statValue.st_size > 0, statValue.st_size % 512 == 0 else {
                throw HelperError.invalidAsset("\(name.uppercased())_MUST_BE_RAW_DISK")
            }
        }
        return ValidatedDirectLaunchAssets(mutableFiles: [
            "disk": identity(diskStat),
            "efi_variable_store": identity(try fileStat(binding.efiVariableStore.path)),
        ])
    }

    public static func secureRunRoot(_ path: String) throws -> String {
        guard path.hasPrefix("/") else {
            throw HelperError.invalidAsset("RUN_ROOT_MUST_BE_ABSOLUTE")
        }
        let resolved = URL(fileURLWithPath: path).standardizedFileURL.path
        guard resolved == path, !path.contains("/../") else {
            throw HelperError.invalidAsset("RUN_ROOT_NOT_CANONICAL")
        }
        var statValue = stat()
        guard lstat(path, &statValue) == 0,
              (statValue.st_mode & S_IFMT) == S_IFDIR,
              (statValue.st_mode & S_IFLNK) == 0,
              statValue.st_uid == geteuid(),
              (statValue.st_mode & S_IWGRP) == 0,
              (statValue.st_mode & S_IWOTH) == 0 else {
            throw HelperError.invalidAsset("INSECURE_RUN_ROOT")
        }
        return path
    }

    public static func validate(asset: AssetDescriptor, name: String) throws {
        guard asset.path.hasPrefix("/") else {
            throw HelperError.invalidAsset("\(name.uppercased())_PATH_NOT_ABSOLUTE")
        }
        let normalized = URL(fileURLWithPath: asset.path).standardizedFileURL.path
        guard normalized == asset.path, !asset.path.contains("/../") else {
            throw HelperError.invalidAsset("\(name.uppercased())_PATH_NOT_CANONICAL")
        }
        try rejectSymlinkComponents(asset.path)
        let before = try fileStat(asset.path)
        guard (before.st_mode & S_IFMT) == S_IFREG,
              before.st_nlink == 1,
              (before.st_mode & S_IWOTH) == 0 else {
            throw HelperError.invalidAsset("\(name.uppercased())_INSECURE_FILE")
        }
        let actualDigest = try digestRegularFile(asset.path, expected: before)
        guard actualDigest == asset.digest else {
            throw HelperError.invalidAsset("\(name.uppercased())_DIGEST_MISMATCH")
        }
    }

    public static func removeValidatedOverlay(
        _ binding: LaunchBinding,
        validated: ValidatedLaunchAssets
    ) throws {
        let root = try secureRunRoot(binding.runRoot)
        let disk = binding.assets["disk"]!
        guard isWithin(disk.path, root: root) else {
            throw HelperError.invalidAsset("OVERLAY_OUTSIDE_RUN_ROOT")
        }
        try rejectSymlinkComponents(disk.path)
        try removeMutableFile(
            disk.path,
            expected: validated.mutableFiles["disk"],
            code: "OVERLAY"
        )
        if let variableStore = binding.efiVariableStore {
            guard isWithin(variableStore.path, root: root) else {
                throw HelperError.invalidAsset("EFI_VARIABLE_STORE_OUTSIDE_RUN_ROOT")
            }
            try removeMutableFile(
                variableStore.path,
                expected: validated.mutableFiles["efi_variable_store"],
                code: "EFI_VARIABLE_STORE"
            )
        }
    }

    private static func validateResourceBounds(_ binding: LaunchBinding) throws {
        try validateResourceBounds(cpuCount: binding.cpuCount, memoryBytes: binding.memoryBytes)
    }

    fileprivate static func validateResourceBounds(cpuCount: Int, memoryBytes: UInt64) throws {
        let maximumCPU = min(Int(ProcessInfo.processInfo.activeProcessorCount), 4)
        guard cpuCount >= 1, cpuCount <= maximumCPU else {
            throw HelperError.invalidRequest("CPU_OUT_OF_BOUNDS")
        }
        let mib = UInt64(1024 * 1024)
        let minimum = UInt64(512) * mib
        let maximum = UInt64(4 * 1024) * mib
        guard memoryBytes >= minimum,
              memoryBytes <= maximum,
              memoryBytes % mib == 0 else {
            throw HelperError.invalidRequest("MEMORY_OUT_OF_BOUNDS")
        }
    }

    private static func rejectSymlinkComponents(_ path: String) throws {
        var current = "/"
        for component in path.split(separator: "/") {
            current += String(component)
            var statValue = stat()
            guard lstat(current, &statValue) == 0,
                  (statValue.st_mode & S_IFLNK) == 0 else {
                throw HelperError.invalidAsset("SYMLINK_OR_MISSING_ASSET_COMPONENT")
            }
            current += "/"
        }
    }

    private static func fileStat(_ path: String) throws -> stat {
        var statValue = stat()
        guard lstat(path, &statValue) == 0 else {
            throw HelperError.invalidAsset("ASSET_UNAVAILABLE")
        }
        return statValue
    }

    private static func identity(_ value: stat) -> FileIdentity {
        FileIdentity(device: UInt64(value.st_dev), inode: UInt64(value.st_ino))
    }

    private static func removeMutableFile(
        _ path: String,
        expected: FileIdentity?,
        code: String
    ) throws {
        let value = try fileStat(path)
        guard let expected,
              identity(value) == expected,
              (value.st_mode & S_IFMT) == S_IFREG,
              value.st_nlink == 1,
              value.st_uid == geteuid() else {
            throw HelperError.invalidAsset("\(code)_CLEANUP_REJECTED")
        }
        guard unlink(path) == 0 else {
            throw HelperError.invalidState("\(code)_CLEANUP_FAILED")
        }
    }

    private static func digestRegularFile(_ path: String, expected: stat) throws -> String {
        let descriptor = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW)
        guard descriptor >= 0 else {
            throw HelperError.invalidAsset("ASSET_OPEN_FAILED")
        }
        defer { _ = close(descriptor) }
        var opened = stat()
        guard fstat(descriptor, &opened) == 0,
              opened.st_dev == expected.st_dev,
              opened.st_ino == expected.st_ino,
              opened.st_nlink == 1,
              (opened.st_mode & S_IFMT) == S_IFREG else {
            throw HelperError.invalidAsset("ASSET_CHANGED_DURING_OPEN")
        }
        let handle = FileHandle(fileDescriptor: descriptor, closeOnDealloc: false)
        var hasher = SHA256()
        while true {
            let chunk = try handle.read(upToCount: 1024 * 1024) ?? Data()
            if chunk.isEmpty { break }
            hasher.update(data: chunk)
        }
        var finalStat = stat()
        guard fstat(descriptor, &finalStat) == 0,
              finalStat.st_dev == expected.st_dev,
              finalStat.st_ino == expected.st_ino,
              finalStat.st_size == expected.st_size else {
            throw HelperError.invalidAsset("ASSET_CHANGED_DURING_HASH")
        }
        return "sha256:" + hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }

    private static func isWithin(_ path: String, root: String) -> Bool {
        path.hasPrefix(root + "/") && path != root
    }
}
