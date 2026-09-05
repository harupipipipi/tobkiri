import Darwin
import Foundation
import Testing
@testable import PackVMVZCore

struct LaunchAssetsTests {
    @Test
    func fixtureCleanupReleasesDescriptorOnlyOnce() throws {
        let fixture = try LaunchFixture()
        fixture.cleanup()
        #expect(fixture.diagnosticsFD == -1)
        let nextDescriptor = open("/dev/null", O_WRONLY | O_CLOEXEC)
        #expect(nextDescriptor >= 0)
        defer { _ = Darwin.close(nextDescriptor) }

        fixture.cleanup()

        #expect(fcntl(nextDescriptor, F_GETFD) >= 0)
        #expect(!FileManager.default.fileExists(atPath: fixture.root.path))
    }

    @Test
    func debugSerialCaptureIsPrivateAndBounded() throws {
        let root = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(
            ".tobkiri-packvm-vz-test-\(UUID().uuidString)",
            isDirectory: true
        )
        try FileManager.default.createDirectory(
            at: root,
            withIntermediateDirectories: false,
            attributes: [.posixPermissions: 0o700]
        )
        defer { try? FileManager.default.removeItem(at: root) }
        let diagnostics = try DirectSerialDiagnostics.create(runRoot: root.path)
        diagnostics.record("HOST_VM_START_SUCCEEDED")
        let writer = FileHandle(fileDescriptor: diagnostics.writeFD, closeOnDealloc: false)
        writer.write(Data(repeating: 0x61, count: 132 * 1024))
        diagnostics.close()

        let capture = root.appendingPathComponent("serial-console.log")
        let content = try Data(contentsOf: capture)
        #expect(content.count == 128 * 1024)
        #expect(content.starts(with: Data("TOBKIRI_HOST:HOST_VM_START_SUCCEEDED\n".utf8)))
        var metadata = stat()
        #expect(lstat(capture.path, &metadata) == 0)
        #expect(metadata.st_mode & 0o777 == 0o600)
    }

    @Test
    func preparesAndRemovesRegularEFIStoreInsidePrivateRunRoot() throws {
        let root = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(
            ".tobkiri-packvm-vz-test-\(UUID().uuidString)",
            isDirectory: true
        )
        try FileManager.default.createDirectory(
            at: root,
            withIntermediateDirectories: false,
            attributes: [.posixPermissions: 0o700]
        )
        defer { try? FileManager.default.removeItem(at: root) }
        let path = root.appendingPathComponent("prepared-efi-variable-store.bin")

        let prepared = try SecureLaunchAssetValidator.prepareEFIStore(
            runRoot: root.path,
            path: path.path
        )

        #expect(prepared.descriptor.path == path.path)
        #expect(FileManager.default.fileExists(atPath: path.path))
        try SecureLaunchAssetValidator.removePreparedEFIStore(
            prepared,
            runRoot: root.path
        )
        #expect(!FileManager.default.fileExists(atPath: path.path))
    }

    @Test
    func rejectsWorldWritableLaunchAsset() throws {
        let fixture = try LaunchFixture()
        defer { fixture.cleanup() }
        guard chmod(fixture.assetPath("config"), S_IRUSR | S_IWUSR | S_IROTH | S_IWOTH) == 0 else {
            Issue.record("unable to make fixture world-writable")
            return
        }
        #expect(throws: (any Error).self) {
            _ = try SecureLaunchAssetValidator.validate(fixture.binding())
        }
    }

    @Test
    func rejectsSymlinkedLaunchAsset() throws {
        let fixture = try LaunchFixture()
        defer { fixture.cleanup() }
        let binding = try fixture.binding()
        let original = fixture.assetPath("agent")
        let link = fixture.root.appendingPathComponent("agent-link.raw").path
        guard unlink(original) == 0,
              symlink(link, original) == 0 else {
            Issue.record("unable to replace fixture asset with symlink")
            return
        }
        #expect(throws: (any Error).self) {
            _ = try SecureLaunchAssetValidator.validate(binding)
        }
    }

    @Test
    func launchBindingRequiresIdentityDigestsAndEfiStore() throws {
        let fixture = try LaunchFixture()
        defer { fixture.cleanup() }
        var raw = fixture.rawLaunch(bootMode: "efi")
        raw["efi_variable_store"] = fixture.descriptor("efi-variable-store.raw")
        let parsed = try LaunchBinding.parse(raw)
        #expect(parsed.bootMode == "efi")
        #expect(parsed.efiVariableStore?.digest == fixture.digest("efi-variable-store.raw"))
        raw["domain_digest"] = "sha256:" + String(repeating: "0", count: 64)
        #expect(throws: HelperError.invalidRequest("BINDING_DIGEST_MISMATCH")) {
            _ = try LaunchBinding.parse(raw)
        }
    }

    @Test
    func directBindingPinsEfiCloneSeedsAndNoBaseDevicePath() throws {
        let fixture = try LaunchFixture()
        defer { fixture.cleanup() }
        let binding = try DirectLaunchBinding.parse(fixture.directRawLaunch())
        #expect(binding.disk.path == fixture.assetPath("disk"))
        #expect(binding.agentSeed.path == fixture.assetPath("agent"))
        #expect(binding.configSeed.path == fixture.assetPath("config"))
        #expect(binding.bindingDigests["image"] == fixture.digest("image.raw"))
        #expect(binding.bindingDigests["disk"] == fixture.digest("disk.raw"))
        #expect(binding.bindingDigests["base_image"] == nil)
        #expect(binding.expiresMonotonicNanoseconds == 42)
    }

    @Test
    func directBindingRequiresPositiveIntegerNanosecondLeaseExpiry() throws {
        let fixture = try LaunchFixture()
        defer { fixture.cleanup() }
        for invalidExpiry: Any in [0, -1, 42.0, true, "42"] {
            var raw = fixture.directRawLaunch()
            var lease = try #require(raw["lease"] as? [String: Any])
            lease["expires_monotonic_ns"] = invalidExpiry
            raw["lease"] = lease
            #expect(throws: (any Error).self) {
                _ = try DirectLaunchBinding.parse(raw)
            }
        }
    }
}

private final class LaunchFixture {
    let root: URL
    private(set) var diagnosticsFD: Int32
    private let names = ["image", "kernel", "initrd", "agent", "config", "disk"]

    init() throws {
        root = FileManager.default.temporaryDirectory.appendingPathComponent(
            "tobkiri-packvm-vz-helper-\(UUID().uuidString)",
            isDirectory: true
        )
        try FileManager.default.createDirectory(
            at: root,
            withIntermediateDirectories: false,
            attributes: [.posixPermissions: 0o700]
        )
        for name in names {
            let body = Data(repeating: UInt8(name.utf8.first ?? 0), count: 512)
            try body.write(to: root.appendingPathComponent("\(name).raw"))
            guard chmod(root.appendingPathComponent("\(name).raw").path, S_IRUSR | S_IWUSR) == 0 else {
                throw HelperError.invalidState("FIXTURE_CHMOD_FAILED")
            }
        }
        try Data(repeating: 5, count: 512).write(
            to: root.appendingPathComponent("efi-variable-store.raw")
        )
        guard chmod(root.appendingPathComponent("efi-variable-store.raw").path, S_IRUSR | S_IWUSR) == 0 else {
            throw HelperError.invalidState("FIXTURE_CHMOD_FAILED")
        }
        diagnosticsFD = open("/dev/null", O_WRONLY | O_CLOEXEC)
        guard diagnosticsFD >= 3 else {
            throw HelperError.invalidState("FIXTURE_DIAGNOSTICS_FD_FAILED")
        }
    }

    deinit {
        cleanup()
    }

    func cleanup() {
        guard diagnosticsFD >= 0 else { return }
        let descriptor = diagnosticsFD
        diagnosticsFD = -1
        _ = Darwin.close(descriptor)
        try? FileManager.default.removeItem(at: root)
    }

    func assetPath(_ name: String) -> String {
        root.appendingPathComponent("\(name).raw").path
    }

    func digest(_ name: String) -> String {
        CanonicalJSON.sha256(try! Data(contentsOf: root.appendingPathComponent(name)))
    }

    func descriptor(_ name: String) -> [String: Any] {
        ["path": root.appendingPathComponent(name).path, "digest": digest(name)]
    }

    func rawLaunch(bootMode: String) -> [String: Any] {
        let domainID = "domain-1"
        let leaseID = "lease-1"
        let reservationID = "reservation-1"
        var assets: [String: Any] = [:]
        for name in LaunchBinding.assetNames(for: bootMode) {
            assets[name] = descriptor("\(name).raw")
        }
        if bootMode == "efi" {
            assets["disk"] = descriptor("image.raw")
        }
        return [
            "boot_mode": bootMode,
            "domain_id": domainID,
            "domain_digest": CanonicalJSON.sha256Text(domainID),
            "lease_id": leaseID,
            "lease_digest": CanonicalJSON.sha256Text(leaseID),
            "reservation_id": reservationID,
            "reservation_digest": CanonicalJSON.sha256Text(reservationID),
            "executable_digest": CanonicalJSON.sha256Text("executable"),
            "artifact_digest": CanonicalJSON.sha256Text("artifact"),
            "materialization_digest": CanonicalJSON.sha256Text("materialization"),
            "isolation_profile": "pack-v4",
            "run_root": root.path,
            "cpu_count": 1,
            "memory_bytes": 512 * 1024 * 1024,
            "kernel_command_line": "console=hvc0",
            "diagnostics_fd": Int(diagnosticsFD),
            "guest_port": 19001,
            "guest_public_key": Data(repeating: 7, count: 32).base64EncodedString(),
            "guest_public_key_digest": CanonicalJSON.sha256(Data(repeating: 7, count: 32)),
            "assets": assets,
        ]
    }

    func binding() throws -> LaunchBinding {
        try LaunchBinding.parse(rawLaunch(bootMode: "linux"))
    }

    func directRawLaunch() -> [String: Any] {
        let domainID = "domain-1"
        let leaseID = "lease-1"
        let reservationID = "reservation-1"
        let publicKey = Data(repeating: 7, count: 32)
        let imageDigest = digest("image.raw")
        let agentDigest = digest("agent.raw")
        let configDigest = digest("config.raw")
        let diskDigest = digest("disk.raw")
        let efiDigest = digest("efi-variable-store.raw")
        let agentCodeDigest = CanonicalJSON.sha256Text("agent-code")
        let configTemplateDigest = CanonicalJSON.sha256Text("config-template")
        let artifactDigest = CanonicalJSON.sha256Text("artifact")
        let executableDigest = CanonicalJSON.sha256Text("executable")
        let materializationDigest = CanonicalJSON.sha256Text("materialization")
        let bindings: [String: String] = [
            "domain": CanonicalJSON.sha256Text(domainID),
            "lease": CanonicalJSON.sha256Text(leaseID),
            "reservation": CanonicalJSON.sha256Text(reservationID),
            "image": imageDigest,
            "agent": agentCodeDigest,
            "config": configTemplateDigest,
            "disk": diskDigest,
            "efi_variable_store": efiDigest,
            "guest_public_key": CanonicalJSON.sha256(publicKey),
            "artifact": artifactDigest,
            "executable": executableDigest,
            "materialization": materializationDigest,
        ]
        return [
            "kind": "tobkiri.macos-vz.launch-binding.v1",
            "version": 1,
            "backend_id": "tobkiri.python-pack-v4",
            "backend_digest": CanonicalJSON.sha256Text("backend"),
            "platform": "macos-arm64",
            "helper": [
                "code_digest": CanonicalJSON.sha256Text("helper"),
                "bundle_id": "dev.tobkiri.launcher.packvm-vz-helper",
                "team_id": "",
                "signing_identity": "",
            ],
            "launch_assets": [
                "base_image_digest": imageDigest,
                "agent_template_digest": agentCodeDigest,
                "config_template_digest": configTemplateDigest,
                "base_image_read_only": true,
                "boot_mode": "efi",
            ],
            "domain_allocation": [
                "domain_id": domainID,
                "reservation_id": reservationID,
                "lease_id": leaseID,
                "run_root": root.path,
                "cow_disk_path": assetPath("disk"),
                "cow_disk_digest": diskDigest,
                "efi_store_path": root.appendingPathComponent("efi-variable-store.raw").path,
                "efi_variable_store_digest": efiDigest,
                "agent_seed_path": assetPath("agent"),
                "agent_seed_digest": agentDigest,
                "config_seed_path": assetPath("config"),
                "config_seed_digest": configDigest,
                "guest_public_key": publicKey.base64EncodedString(),
                "guest_public_key_digest": CanonicalJSON.sha256(publicKey),
            ],
            "agent_code_digest": agentCodeDigest,
            "runtime": [
                "cpu_count": 1,
                "memory_bytes": 512 * 1024 * 1024,
                "guest_vsock_port": 19001,
            ],
            "binding_digests": bindings,
            "artifact": [
                "artifact_digest": artifactDigest,
                "executable_digest": executableDigest,
                "materialization_digest": materializationDigest,
                "guest_payload_digest": CanonicalJSON.sha256Text("payload"),
            ],
            "domain_id": domainID,
            "isolation_profile": "pack-v4",
            "lease": [
                "lease_id": leaseID,
                "reservation_id": reservationID,
                "expires_monotonic_ns": 42,
            ],
            "reservation_id": reservationID,
        ]
    }
}
