"""Production platform, Host Extension SDK, and OS wake contract tests."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from core_runtime.bootstrap.production_v4 import _authenticated_packvm_backend
from core_runtime.authority.v4 import (
    AuthorityMode,
    AuthorityScope,
    DomainBoundary,
    ExecutionDomain,
    FunctionPrincipal,
)
from tobkiri_host.artifact_materialization import (
    MaterializedArtifactFile,
    MaterializedPackArtifact,
)
from tobkiri_host.backends import production_backend_registry
from tobkiri_host.contracts import OperationCatalog, OperationRoute
from tobkiri_host.errors import (
    AuthorizationError,
    BackendUnavailableError,
    ResolutionError,
    TriggerError,
)
from tobkiri_host.extension_sdk import (
    CapabilityProviderRegistration,
    HostExtensionRegistration,
    HostExtensionSDK,
)
from tobkiri_host.models import (
    ArtifactVariant,
    ContractOperation,
    ExecutionKind,
    FunctionArtifact,
    OpaqueAuthorityRef,
    PackArtifact,
    PackageKind,
)
from tobkiri_host.platform_backends import (
    IsolationLaunch,
    IsolationLease,
    ManagedLimaPackVMDriver,
    PlatformAttestation,
    ProductionIsolationBackend,
    _platform_attestation_digest,
)
from tobkiri_host.ports import OpaqueInvocationLease
from tobkiri_host.triggers import (
    TriggerRegistration,
    TriggerWakeKernel,
    WakeAdapterStatus,
    WakeRegistrationLease,
)
from tobkiri_host.tauri_roles import validate_production_tauri_roles
from tobkiri_protocol.canonical import canonical_digest


def digest(seed: str) -> str:
    return f"sha256:{hashlib.sha256(seed.encode()).hexdigest()}"


SCHEMA = {"type": "object", "additionalProperties": False}


def pack_artifact(kind: PackageKind = PackageKind.HOST_EXTENSION) -> PackArtifact:
    operation = ContractOperation(
        contract_id="host.files.v1",
        contract_version="1.0.0",
        revision_digest=digest("contract"),
        operation_id="read",
        input_schema=SCHEMA,
        output_schema=SCHEMA,
    )
    function = FunctionArtifact(
        function_id="extension.files.read",
        implementation_digest=digest("function"),
        variant_id="macos.arm64",
        operations=(operation,),
    )
    variant = ArtifactVariant(
        variant_id="macos.arm64",
        digest=digest("variant"),
        execution_kind=ExecutionKind.PACK_VM,
        os="macos",
        architecture="arm64",
        runtime_abi="packvm-v1",
        backend="tobkiri.python-pack-v4",
    )
    return PackArtifact(
        pack_id="extension.files",
        version="1.0.0",
        digest=digest("artifact"),
        publisher_lineage="publisher.files",
        package_kind=kind,
        functions=(function,),
        variants=(variant,),
    )


def binding():
    artifact = pack_artifact()
    route = OperationRoute(
        contract_id="host.files.v1",
        operation_id="read",
        artifact_digest=artifact.digest,
        function_id="extension.files.read",
        variant_id="macos.arm64",
        execution_domain_profile="packvm.host-extension.v1",
        materialization_mode="on_demand",
        target_principal_ref=OpaqueAuthorityRef("authority:files"),
    )
    return OperationCatalog((artifact,), (route,)).resolve("host.files.v1", "read", ">=1,<2")


def materialized_artifact() -> MaterializedPackArtifact:
    artifact_file = MaterializedArtifactFile(
        path="runtime/handler.py",
        digest=digest("function"),
        executable=False,
        content=b"function",
    )
    identity = {
        "pack_id": "extension.files",
        "artifact_digest": digest("artifact"),
        "function_id": "extension.files.read",
        "implementation_digest": digest("function"),
        "implementation_path": "runtime/handler.py",
        "files": [
            {
                "path": artifact_file.path,
                "digest": artifact_file.digest,
                "executable": artifact_file.executable,
                "size": len(artifact_file.content),
            }
        ],
    }
    return MaterializedPackArtifact(
        pack_id="extension.files",
        artifact_digest=digest("artifact"),
        function_id="extension.files.read",
        implementation_digest=digest("function"),
        implementation_path="runtime/handler.py",
        materialization_digest=canonical_digest(identity),
        root_device=1,
        root_inode=2,
        files=(artifact_file,),
    )


class Driver:
    backend_id = "tobkiri.python-pack-v4"
    substrate_id = "macos-vz"
    backend_digest = digest("backend")
    platform = "macos-arm64"

    def __init__(self) -> None:
        self.last_launch: IsolationLaunch | None = None
        self.attestation_platform: str | None = None
        self.terminated: list[str] = []

    def capability(self) -> tuple[bool, str | None]:
        return True, None

    def launch(self, request: IsolationLaunch) -> PlatformAttestation:
        self.last_launch = request
        result = PlatformAttestation(
            domain_id=request.target_domain_id,
            backend_id=self.backend_id,
            backend_digest=self.backend_digest,
            platform=self.platform,
            executable_digest=request.executable_digest,
            artifact_digest=request.artifact_digest,
            materialization_digest=request.artifact.materialization_digest,
            guest_artifact_identity=digest("guest-artifact"),
            isolation_profile=request.isolation_profile,
            attestation_digest=digest("pending-attestation"),
            attestation_nonce="direct-vz-nonce-1",
            lease_id=request.lease.lease_id,
            reservation_id=request.reservation_id,
            authenticated_channel=True,
            nonce_fresh=True,
        )
        result = replace(
            result,
            attestation_digest=_platform_attestation_digest(result),
        )
        if self.attestation_platform is not None:
            return replace(result, platform=self.attestation_platform)
        return result

    def invoke(self, request: object) -> object:
        return request

    def cancel(self, request_id: str) -> None:
        return None

    def terminate(self, domain_id: str) -> None:
        self.terminated.append(domain_id)


def test_all_documented_platforms_register_exact_provider_with_controlled_driver() -> None:
    matrix = (
        ("Darwin", "arm64", "macos-vz", "macos-arm64"),
        ("Windows", "AMD64", "windows-whpx", "windows-amd64"),
        ("Linux", "x86_64", "linux-firecracker", "linux-amd64"),
    )
    for system, machine, backend_id, platform_id in matrix:
        driver = Driver()
        driver.substrate_id = backend_id
        driver.platform = platform_id
        registry = production_backend_registry(
            platform_system=system,
            machine=machine,
            drivers=(driver,),
        )
        assert registry.statuses[0].backend_id == "tobkiri.python-pack-v4"
        assert registry.statuses[0].ready_for_production


def test_platform_supervisor_accepts_only_explicit_portable_variant_alias() -> None:
    driver = Driver()
    registry = production_backend_registry(
        platform_system="Darwin", machine="arm64", drivers=(driver,)
    )
    selected = binding()
    portable = replace(
        selected,
        variant=replace(selected.variant, os="any", architecture="any"),
    )
    assert registry.select(portable).status.backend_id == "tobkiri.python-pack-v4"


def test_platform_selection_and_attestation_fail_closed() -> None:
    selected = binding()
    unavailable = production_backend_registry(platform_system="Darwin", machine="arm64")
    with pytest.raises(BackendUnavailableError, match="supervisor"):
        unavailable.select(selected)
    driver = Driver()
    backend = ProductionIsolationBackend(
        driver,
        artifact_resolver=lambda _binding: materialized_artifact(),
        target_domain_resolver=lambda _binding: "domain.vz.1",
    )
    evidence = backend.materialize(selected, "reservation-1")
    assert evidence.resource_reservation_id == "reservation-1"
    assert driver.last_launch is not None
    assert evidence.domain_lease_id == driver.last_launch.lease.lease_id
    driver.attestation_platform = "linux-arm64"
    with pytest.raises(BackendUnavailableError, match="attestation"):
        backend.materialize(selected, "reservation-2")
    wrong = replace(selected.variant, backend="other-packvm")
    with pytest.raises(BackendUnavailableError, match="wrong platform"):
        backend.materialize(replace(selected, variant=wrong), "reservation-3")


@pytest.mark.parametrize(
    ("system", "machine", "dependency", "substrate"),
    [
        ("Windows", "AMD64", "WinHvPlatform.dll", "windows-whpx"),
        ("Linux", "x86_64", "/dev/kvm", "linux-firecracker"),
    ],
)
def test_unregistered_cross_platform_python_packvm_fails_closed(
    system: str, machine: str, dependency: str, substrate: str
) -> None:
    registry = production_backend_registry(platform_system=system, machine=machine)
    assert registry.statuses[0].backend_id == "tobkiri.python-pack-v4"
    assert registry.statuses[0].ready_for_production is False
    reason = str(registry.statuses[0].unavailable_reason)
    assert dependency in reason or f"authenticated {substrate}" in reason


class Provisioner:
    def __init__(self) -> None:
        self.attestation = digest("lima-attestation")
        self.requests: list[object] = []

    def doctor(self):
        return SimpleNamespace(
            ready=True,
            reason=None,
            platform="macos-arm64",
            attestation_digest=self.attestation,
        )

    def invoke_guest(self, request):
        self.requests.append(request)
        return {
            "ok": True,
            "protocol": "io.tobkiri.packvm-supervisor.v1",
            "payload": {"inside_guest": True},
        }

    def materialize_artifact(self, request):
        self.requests.append(request)
        return {
            "ok": True,
            "protocol": "io.tobkiri.packvm-supervisor.v1",
            "artifact_digest": request["artifact_digest"],
            "materialization_digest": request["materialization_digest"],
            "guest_artifact_identity": digest("guest-artifact"),
        }


def test_managed_lima_driver_invokes_only_authenticated_guest_and_rejects_replay() -> None:
    provisioner = Provisioner()
    driver = ManagedLimaPackVMDriver(provisioner)
    lease = IsolationLease("lease-1", "reservation-1", 100.0)
    launch = IsolationLaunch(
        backend_id="tobkiri.python-pack-v4",
        platform="macos-arm64",
        artifact_digest=digest("artifact"),
        executable_digest=digest("executable"),
        isolation_profile="packvm.default.v1",
        target_domain_id="domain.provider.authority-owned",
        reservation_id="reservation-1",
        lease=lease,
        artifact=materialized_artifact(),
    )
    attestation = driver.launch(launch)
    request = SimpleNamespace(
        target_domain=SimpleNamespace(value=attestation.domain_id),
        context=SimpleNamespace(request_id="request-1"),
        contract_id="sample.v1",
        contract_version="1.0.0",
        operation_id="run",
        payload={"value": 1},
        request_digest=digest("request"),
        deadline_monotonic=50.0,
    )

    outcome = driver.invoke(request)
    assert outcome.payload == {"inside_guest": True}
    assert provisioner.requests[0]["operation"] == "materialize"
    assert provisioner.requests[1]["operation"] == "invoke"
    driver.terminate(attestation.domain_id)
    with pytest.raises(BackendUnavailableError, match="replay"):
        driver.launch(launch)
    provisioner.attestation = digest("tampered")
    assert driver.capability() == (False, "managed Lima PackVM attestation changed")


def test_production_composition_never_promotes_lima_to_direct_vz() -> None:
    provisioner = Provisioner()
    assert _authenticated_packvm_backend(provisioner) is None
    assert ManagedLimaPackVMDriver(provisioner).substrate_id == "lima"


def test_production_composition_registers_only_verified_direct_vz_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Composition pins each direct VZ constructor input to provisioned facts."""

    from ecosystem.defaultspack.backend.sandbox.isolation.macos_vz_provisioner import (
        MacOSVZProvisionedFacts,
    )
    import tobkiri_host.macos_vz_supervisor as macos_vz_supervisor
    import tobkiri_host.platform_backends as platform_backends

    transport_factory_calls: list[object] = []

    def transport_factory(_allocation: object) -> object:
        transport_factory_calls.append(_allocation)
        return object()

    facts = MacOSVZProvisionedFacts(
        helper_path=Path("/private/var/db/tobkiri/helper"),
        helper_identity=object(),
        launch_assets=object(),
        agent_identity=object(),
        domain_allocator=object(),
        instance_root=Path("/private/var/db/tobkiri/instance"),
        transport_factory=transport_factory,
        protocol_ready=True,
        reason=None,
    )

    class Lifecycle:
        def production_backend_registration(self) -> MacOSVZProvisionedFacts:
            return facts

        def prepare_direct_vz(self) -> None:
            raise AssertionError("lifecycle registration must be preferred")

    class CapturedDriver:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    class CapturedBackend:
        def __init__(self, driver: CapturedDriver) -> None:
            self.driver = driver

    monkeypatch.setattr(macos_vz_supervisor, "MacOSVZSupervisorDriver", CapturedDriver)
    monkeypatch.setattr(platform_backends, "MacOSVZBackend", CapturedBackend)

    backend = _authenticated_packvm_backend(Lifecycle())

    assert isinstance(backend, CapturedBackend)
    assert transport_factory_calls == []
    assert backend.driver.kwargs == {
        "transport_factory": transport_factory,
        "helper_path": facts.helper_path,
        "helper_identity": facts.helper_identity,
        "launch_assets": facts.launch_assets,
        "agent_identity": facts.agent_identity,
        "domain_allocator": facts.domain_allocator,
    }


def test_production_composition_rejects_missing_or_failed_direct_vz_facts() -> None:
    """A missing transport or lifecycle failure never promotes another backend."""

    from ecosystem.defaultspack.backend.sandbox.isolation.macos_vz_provisioner import (
        MacOSVZProvisionedFacts,
    )

    facts_without_transport = MacOSVZProvisionedFacts(
        helper_path=Path("/private/var/db/tobkiri/helper"),
        helper_identity=object(),
        launch_assets=object(),
        agent_identity=object(),
        domain_allocator=object(),
        instance_root=Path("/private/var/db/tobkiri/instance"),
        transport_factory=None,
        protocol_ready=True,
        reason="transport unavailable",
    )

    class DirectProvisioner:
        def prepare_direct_vz(self) -> MacOSVZProvisionedFacts:
            return facts_without_transport

    class FailingLifecycle:
        def production_backend_registration(self) -> object:
            raise RuntimeError("verified fact retrieval failed")

    assert _authenticated_packvm_backend(DirectProvisioner()) is None
    assert _authenticated_packvm_backend(FailingLifecycle()) is None


class RegistrationStore:
    security_epoch = 1

    def __init__(self) -> None:
        self.records: list[object] = []

    def put_records_atomically(self, records) -> None:
        self.records.extend(records)


class Authority:
    def __init__(self) -> None:
        self.store = RegistrationStore()
        self.revocations: list[tuple[str, str]] = []

    def revoke(self, *, target_kind: str, target_id: str, reason: str) -> str:
        self.revocations.append((target_kind, target_id))
        return digest(reason)


def extension_registration(
    kind: PackageKind = PackageKind.HOST_EXTENSION,
) -> HostExtensionRegistration:
    artifact = pack_artifact(kind)
    operation = artifact.functions[0].operations[0]
    principal = FunctionPrincipal(
        parent_artifact_digest=artifact.digest,
        function_implementation_digest=artifact.functions[0].implementation_digest,
        function_id=artifact.functions[0].function_id,
        contract_revision_digest=operation.revision_digest,
        operation_id=operation.operation_id,
    )
    domain = ExecutionDomain(
        domain_id="domain.extension.files",
        profile_id="host.extensions",
        activation_id="extension.files.activation",
        boot_epoch=1,
        process_identity="signed.helper.files",
        authenticated_channel_digest=digest("channel"),
        sandbox_profile_digest=digest("sandbox"),
        resource_namespace="extension.files.resources",
        principals=(principal,),
        boundary=DomainBoundary.DEDICATED_PROCESS,
        security_epoch=1,
    )
    scope = AuthorityScope(
        capability="host.files.read",
        semantics_digest=digest("scope"),
        dimensions={"root": ("workspace",)},
    )
    provider = CapabilityProviderRegistration(
        provider_id="extension.files.read",
        function_id="extension.files.read",
        contract_id="host.files.v1",
        operation_id="read",
        capability="host.files.read",
        scope_semantics_digest=scope.semantics_digest,
        provider_ceiling=scope,
        authority_mode=AuthorityMode.LEASE_ONLY,
        execution_domain=domain,
        input_schema=SCHEMA,
        output_schema=SCHEMA,
        error_schema=None,
        progress_schema=None,
        attenuation_definition={"kind": "path_root"},
        approval_metadata={"risk": "read"},
        audit_metadata={"redact": []},
        conformance_vectors=({"root": "workspace"},),
        host_broker_binding="resource-handle.files.v1",
    )
    return HostExtensionRegistration(
        registration_id="registration.files.v1",
        host_extension_id="extension.files",
        trust_id="trust.extension.files.v1",
        artifact=artifact,
        trust_provenance_digest=digest("trust"),
        providers=(provider,),
        valid_from=1.0,
    )


def test_host_extension_sdk_exact_registration_revoke_and_normal_pack_denial() -> None:
    authority = Authority()
    database = sqlite3.connect(":memory:")
    sdk = HostExtensionSDK(authority, database, clock=lambda: 2.0)
    ids = sdk.register(extension_registration())
    assert ids == ("provider-authority.registration.files.v1.0",)
    assert len(authority.store.records) == 3
    restarted = HostExtensionSDK(authority, database, clock=lambda: 3.0)
    restarted.revoke("registration.files.v1", reason="operator revoke")
    assert authority.revocations == [
        ("provider_authority", ids[0]),
        ("host_extension", "trust.extension.files.v1"),
    ]
    assert [event["event_type"] for event in restarted.audit_events("registration.files.v1")] == [
        "registered",
        "revoked",
    ]
    with pytest.raises(AuthorizationError, match="normal Pack/Profile"):
        sdk.register(extension_registration(PackageKind.NORMAL))


class WakeAuthority:
    def issue_trigger_lease(
        self,
        registration_id: str,
        occurrence_id: str,
        target: OpaqueAuthorityRef,
        security_epoch: int,
    ) -> OpaqueInvocationLease:
        return OpaqueInvocationLease(f"{registration_id}:{occurrence_id}".encode())


class WakeAdapter:
    status = WakeAdapterStatus("macos.backgroundtasks", "macos", True)

    def __init__(self) -> None:
        self.armed: list[str] = []
        self.revoked: list[str] = []

    def register(self, registration: TriggerRegistration) -> WakeRegistrationLease:
        return WakeRegistrationLease(
            "wake-lease-1", registration.registration_id, registration.security_epoch
        )

    def arm(
        self,
        lease: WakeRegistrationLease,
        occurrence_id: str,
        due_monotonic: float,
    ) -> None:
        self.armed.append(occurrence_id)

    def revoke(self, lease: WakeRegistrationLease) -> None:
        self.revoked.append(lease.registration_id)


def test_production_wake_requires_adapter_lease_and_current_epoch() -> None:
    epoch = [1]
    adapter = WakeAdapter()
    kernel = TriggerWakeKernel(
        sqlite3.connect(":memory:"),
        WakeAuthority(),
        clock=lambda: 10.0,
        wake_adapter=adapter,
        current_security_epoch=lambda: epoch[0],
        production=True,
    )
    registration = TriggerRegistration(
        "daily", "trigger.v1", "deliver", OpaqueAuthorityRef("target.daily"), digest("a"), 1
    )
    kernel.register(registration)
    assert kernel.schedule("daily", "occurrence-1", 9.0)
    assert adapter.armed == ["occurrence-1"]
    epoch[0] = 2
    with pytest.raises(TriggerError, match="stale"):
        kernel.claim_due()
    epoch[0] = 1
    kernel.revoke("daily")
    with pytest.raises(TriggerError, match="unknown or disabled"):
        kernel.schedule("daily", "occurrence-2", 11.0)
    unavailable = TriggerWakeKernel(
        sqlite3.connect(":memory:"),
        WakeAuthority(),
        current_security_epoch=lambda: 1,
        production=True,
    )
    with pytest.raises(TriggerError, match="not registered"):
        unavailable.register(registration)


def test_generated_tauri_roles_are_separate_and_production_selects_runtime_only() -> None:
    bundle = Path(__file__).parents[1] / "ecosystem" / "defaultspack" / "v4"
    runtime = json.loads(
        (bundle / "packs" / "runtime.tauri.application.default.pack.v4.json").read_text(
            encoding="utf-8"
        )
    )
    toolchain = json.loads(
        (bundle / "packs" / "dev.tauri.toolchain.default.pack.v4.json").read_text(encoding="utf-8")
    )
    profile = json.loads((bundle / "defaults.profile.v4.json").read_text(encoding="utf-8"))
    assert runtime["pack"]["kind"] == "application"
    assert runtime["contracts"][0]["contract_id"] == "runtime.tauri.application.v1"
    assert toolchain["pack"]["kind"] == "host_extension"
    assert toolchain["contracts"][0]["contract_id"] == "dev.tauri.toolchain.v1"
    selected = {item["pack_id"] for item in profile["packs"]}
    assert "runtime.tauri.application.default" in selected
    assert not any(item.startswith("dev.tauri.toolchain.") for item in selected)


def test_production_tauri_roles_reject_missing_runtime_and_development_toolchain() -> None:
    profile = {
        "shell": {"pack_id": "shell.tauri.default"},
        "packs": [{"pack_id": "runtime.tauri.application.default"}],
    }
    lock = {
        "effective_set": [
            {"identity": "runtime.tauri.application.default"},
        ]
    }
    validate_production_tauri_roles(profile, lock)
    with pytest.raises(ResolutionError, match="exactly one selected runtime"):
        validate_production_tauri_roles(profile, {"effective_set": []})
    with pytest.raises(ResolutionError, match="Development Realm"):
        validate_production_tauri_roles(
            profile,
            {
                "effective_set": [
                    {"identity": "runtime.tauri.application.default"},
                    {"identity": "dev.tauri.toolchain.default"},
                ]
            },
        )
