"""Attested production PackVM adapters for the supported host substrates."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import platform as host_platform
import secrets
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Protocol

from .artifact_materialization import MaterializedPackArtifact
from .backends import BackendStatus, REQUIRED_PRODUCTION_GATES
from .contracts import ResolvedOperationBinding
from .errors import BackendUnavailableError
from .effects import ProviderOutcome
from .models import ExecutionKind, OpaqueAuthorityRef, RuntimeEvidence, require_digest


PYTHON_PACKVM_BACKEND = "tobkiri.python-pack-v4"
SUPPORTED_BACKENDS: Mapping[str, tuple[str, str]] = {
    "Darwin": ("macos-vz", "/System/Library/Frameworks/Virtualization.framework"),
    "Windows": ("windows-whpx", "C:/Windows/System32/WinHvPlatform.dll"),
    "Linux": ("linux-firecracker", "/dev/kvm"),
}


@dataclass(frozen=True)
class IsolationLease:
    """Finite Host-owned lease for one materialized domain."""

    lease_id: str
    reservation_id: str
    expires_monotonic: float

    def __post_init__(self) -> None:
        if not self.lease_id or not self.reservation_id:
            raise BackendUnavailableError("domain lease identity is missing")
        if self.expires_monotonic <= 0:
            raise BackendUnavailableError("domain lease expiry is invalid")


@dataclass(frozen=True)
class IsolationLaunch:
    """Exact launch request passed to a privileged platform supervisor."""

    backend_id: str
    platform: str
    artifact_digest: str
    executable_digest: str
    isolation_profile: str
    target_domain_id: str
    reservation_id: str
    lease: IsolationLease
    artifact: MaterializedPackArtifact


@dataclass(frozen=True)
class PlatformAttestation:
    """Host-authenticated evidence returned by the platform supervisor."""

    domain_id: str
    backend_id: str
    backend_digest: str
    platform: str
    executable_digest: str
    artifact_digest: str
    materialization_digest: str
    guest_artifact_identity: str
    isolation_profile: str
    attestation_digest: str
    attestation_nonce: str
    lease_id: str
    reservation_id: str
    authenticated_channel: bool
    nonce_fresh: bool

    def __post_init__(self) -> None:
        if not self.domain_id or not self.attestation_nonce:
            raise BackendUnavailableError("platform attestation identity is missing")
        require_digest(self.backend_digest, "attested backend")
        require_digest(self.executable_digest, "attested executable")
        require_digest(self.artifact_digest, "attested artifact")
        require_digest(self.materialization_digest, "attested materialization")
        require_digest(self.guest_artifact_identity, "guest artifact identity")
        require_digest(self.attestation_digest, "platform attestation")


class PlatformIsolationDriver(Protocol):
    """Privileged supervisor boundary implemented by VZ, WHPX, or Firecracker."""

    backend_id: str
    substrate_id: str
    backend_digest: str
    platform: str

    def capability(self) -> tuple[bool, str | None]:
        """Return deterministic dependency readiness without mutating the Host."""

    def launch(self, request: IsolationLaunch) -> PlatformAttestation:
        """Launch an exact artifact in the required isolation substrate."""

    def invoke(self, request: object) -> object:
        """Invoke over the authenticated supervisor channel."""

    def cancel(self, request_id: str) -> None:
        """Fence one request at the supervisor."""

    def terminate(self, domain_id: str) -> None:
        """Destroy one domain and release all platform resources."""


CapabilityBridge = Callable[[object, Mapping[str, Any]], Mapping[str, Any]]


class UnavailablePlatformDriver:
    """Deterministic fail-closed driver used when Host dependencies are absent."""

    def __init__(
        self,
        backend_id: str,
        platform: str,
        reason: str,
        *,
        substrate_id: str = "unavailable",
    ) -> None:
        self.backend_id = backend_id
        self.substrate_id = substrate_id
        self.platform = platform
        self.backend_digest = _digest(
            {"backend_id": backend_id, "platform": platform, "state": "unavailable"}
        )
        self._reason = reason

    def capability(self) -> tuple[bool, str | None]:
        return False, self._reason

    def launch(self, request: IsolationLaunch) -> PlatformAttestation:
        raise BackendUnavailableError(self._reason)

    def invoke(self, request: object) -> object:
        raise BackendUnavailableError(self._reason)

    def cancel(self, request_id: str) -> None:
        return None

    def terminate(self, domain_id: str) -> None:
        return None


class ProductionIsolationBackend:
    """Broker-facing adapter enforcing lifecycle, attestation, lease, and charge."""

    def __init__(
        self,
        driver: PlatformIsolationDriver,
        *,
        artifact_resolver: Callable[
            [ResolvedOperationBinding], MaterializedPackArtifact
        ]
        | None = None,
        target_domain_resolver: Callable[[ResolvedOperationBinding], str] | None = None,
        lease_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        ready, reason = driver.capability()
        self._driver = driver
        self._artifact_resolver = artifact_resolver
        self._target_domain_resolver = target_domain_resolver
        self._clock = clock
        self._lease_seconds = lease_seconds
        self._domains: dict[str, PlatformAttestation] = {}
        self._reservations: dict[str, str] = {}
        self._leases: dict[str, IsolationLease] = {}
        self._request_domains: dict[str, str] = {}
        self._request_lock = threading.RLock()
        self._capability_bridge: CapabilityBridge | None = None
        self.status = BackendStatus(
            backend_id=driver.backend_id,
            execution_kind=ExecutionKind.PACK_VM,
            platform=driver.platform,
            backend_digest=driver.backend_digest,
            production_enabled=ready,
            conformance_only=not ready,
            satisfied_gates=REQUIRED_PRODUCTION_GATES if ready else frozenset(),
            unavailable_reason=reason,
            enforces_platform=True,
            requires_platform_attestation=True,
        )

    def bind_artifact_resolver(
        self,
        resolver: Callable[
            [ResolvedOperationBinding], MaterializedPackArtifact
        ],
    ) -> None:
        """Bind the activation-captured artifact source before materialization."""

        if self._domains or self._reservations:
            raise BackendUnavailableError(
                "artifact resolver cannot change after materialization"
            )
        if self._artifact_resolver is not None and self._artifact_resolver is not resolver:
            raise BackendUnavailableError("artifact resolver is already bound")
        self._artifact_resolver = resolver

    def bind_target_domain_resolver(
        self,
        resolver: Callable[[ResolvedOperationBinding], str],
    ) -> None:
        """Bind the Authority-owned exact domain identity before launch."""

        if self._domains or self._reservations:
            raise BackendUnavailableError(
                "target domain resolver cannot change after materialization"
            )
        if (
            self._target_domain_resolver is not None
            and self._target_domain_resolver is not resolver
        ):
            raise BackendUnavailableError("target domain resolver is already bound")
        self._target_domain_resolver = resolver

    def bind_capability_bridge(self, callback: CapabilityBridge) -> None:
        """Bind verified PackVM-to-Host capability continuation handling.

        Only a direct platform driver that implements the explicit bridge
        method may receive this callback.  Lima and legacy drivers therefore
        cannot be promoted by merely accepting an arbitrary callable.
        """

        if not callable(callback):
            raise BackendUnavailableError("PackVM capability bridge is invalid")
        if self._domains or self._reservations:
            raise BackendUnavailableError(
                "PackVM capability bridge cannot change after materialization"
            )
        if self._capability_bridge is not None and self._capability_bridge is not callback:
            raise BackendUnavailableError("PackVM capability bridge is already bound")
        binder = getattr(self._driver, "bind_capability_bridge", None)
        if not callable(binder):
            raise BackendUnavailableError(
                "platform supervisor does not support a verified capability bridge"
            )
        binder(callback)
        self._capability_bridge = callback

    def materialize(
        self,
        binding: ResolvedOperationBinding,
        reservation_id: str,
    ) -> RuntimeEvidence:
        if not self.status.ready_for_production:
            raise BackendUnavailableError(
                self.status.unavailable_reason or "platform backend is unavailable"
            )
        if binding.variant.backend != self.status.backend_id:
            raise BackendUnavailableError("launch requested the wrong platform provider")
        if binding.variant.execution_kind is not ExecutionKind.PACK_VM:
            raise BackendUnavailableError("platform backend requires a PackVM variant")
        if reservation_id in self._reservations:
            raise BackendUnavailableError("resource reservation is already materialized")
        if self._artifact_resolver is None:
            raise BackendUnavailableError(
                "authenticated Pack artifact materializer is unavailable"
            )
        if self._target_domain_resolver is None:
            raise BackendUnavailableError(
                "Authority-owned target domain resolver is unavailable"
            )
        target_domain_id = self._target_domain_resolver(binding)
        if not isinstance(target_domain_id, str) or not target_domain_id:
            raise BackendUnavailableError("Authority-owned target domain is invalid")
        try:
            artifact = self._artifact_resolver(binding)
        except Exception as exc:
            raise BackendUnavailableError(
                "authenticated Pack artifact materialization failed"
            ) from exc
        if (
            artifact.pack_id != binding.artifact.pack_id
            or artifact.artifact_digest != binding.artifact.digest
            or artifact.function_id != binding.function.function_id
            or artifact.implementation_digest
            != binding.function.implementation_digest
        ):
            raise BackendUnavailableError(
                "authenticated Pack artifact does not match resolved binding"
            )
        lease = IsolationLease(
            lease_id=_digest(
                {
                    "reservation_id": reservation_id,
                    "executable": binding.function.implementation_digest,
                    "backend": self.status.backend_digest,
                }
            ),
            reservation_id=reservation_id,
            expires_monotonic=self._clock() + self._lease_seconds,
        )
        launch = IsolationLaunch(
            backend_id=self.status.backend_id,
            platform=self.status.platform,
            artifact_digest=binding.artifact.digest,
            executable_digest=binding.function.implementation_digest,
            isolation_profile=binding.route.execution_domain_profile,
            target_domain_id=target_domain_id,
            reservation_id=reservation_id,
            lease=lease,
            artifact=artifact,
        )
        attestation = self._driver.launch(launch)
        try:
            self._validate_attestation(launch, attestation)
        except BackendUnavailableError:
            self._driver.terminate(attestation.domain_id)
            raise
        if attestation.domain_id in self._domains:
            self._driver.terminate(attestation.domain_id)
            raise BackendUnavailableError("platform supervisor reused a live domain identity")
        self._domains[attestation.domain_id] = attestation
        self._reservations[reservation_id] = attestation.domain_id
        self._leases[attestation.domain_id] = lease
        return RuntimeEvidence(
            domain_ref=OpaqueAuthorityRef(attestation.domain_id),
            executable_digest=attestation.executable_digest,
            backend_digest=attestation.backend_digest,
            authenticated_channel=attestation.authenticated_channel,
            nonce_fresh=attestation.nonce_fresh,
            platform=attestation.platform,
            isolation_profile=attestation.isolation_profile,
            attestation_digest=attestation.attestation_digest,
            domain_lease_id=attestation.lease_id,
            resource_reservation_id=attestation.reservation_id,
        )

    def invoke(self, request: object) -> object:
        target = getattr(getattr(request, "target_domain", None), "value", None)
        if not isinstance(target, str):
            raise BackendUnavailableError("provider request has no Host domain identity")
        lease = self._leases.get(target)
        if lease is None or lease.expires_monotonic <= self._clock():
            self.terminate(target)
            raise BackendUnavailableError("provider domain lease is unavailable or expired")
        request_id = getattr(getattr(request, "context", None), "request_id", None)
        if not isinstance(request_id, str) or not request_id:
            raise BackendUnavailableError("provider request identity is unavailable")
        with self._request_lock:
            if request_id in self._request_domains:
                raise BackendUnavailableError("provider request identity is already active")
            self._request_domains[request_id] = target
        try:
            return self._driver.invoke(request)
        finally:
            with self._request_lock:
                if self._request_domains.get(request_id) == target:
                    self._request_domains.pop(request_id, None)

    def cancel(self, request_id: str) -> None:
        with self._request_lock:
            if request_id not in self._request_domains:
                raise BackendUnavailableError("cancel request does not own an active domain")
        self._driver.cancel(request_id)

    def terminate(self, domain_id: str) -> None:
        with self._request_lock:
            for request_id, target in tuple(self._request_domains.items()):
                if target == domain_id:
                    self._request_domains.pop(request_id, None)
        attestation = self._domains.pop(domain_id, None)
        if attestation is not None:
            self._reservations.pop(attestation.reservation_id, None)
            self._leases.pop(domain_id, None)
        self._driver.terminate(domain_id)

    def _validate_attestation(
        self,
        launch: IsolationLaunch,
        attestation: PlatformAttestation,
    ) -> None:
        if (
            attestation.domain_id != launch.target_domain_id
            or attestation.backend_id != launch.backend_id
            or attestation.backend_digest != self.status.backend_digest
            or attestation.platform != launch.platform
            or attestation.executable_digest != launch.executable_digest
            or attestation.artifact_digest != launch.artifact_digest
            or attestation.materialization_digest
            != launch.artifact.materialization_digest
            or attestation.isolation_profile != launch.isolation_profile
            or attestation.lease_id != launch.lease.lease_id
            or attestation.reservation_id != launch.reservation_id
            or not attestation.authenticated_channel
            or not attestation.nonce_fresh
            or attestation.attestation_digest
            != _platform_attestation_digest(attestation)
        ):
            raise BackendUnavailableError("platform attestation does not match launch")


class MacOSVZBackend(ProductionIsolationBackend):
    """macOS Virtualization.framework PackVM backend."""

    def __init__(
        self,
        driver: PlatformIsolationDriver,
        *,
        artifact_resolver: Callable[
            [ResolvedOperationBinding], MaterializedPackArtifact
        ]
        | None = None,
    ) -> None:
        if (
            driver.backend_id != PYTHON_PACKVM_BACKEND
            or driver.substrate_id != "macos-vz"
            or not driver.platform.startswith("macos-")
        ):
            raise BackendUnavailableError("macOS VZ driver identity mismatch")
        super().__init__(driver, artifact_resolver=artifact_resolver)


class WindowsWHPXBackend(ProductionIsolationBackend):
    """Windows Hypervisor Platform PackVM backend."""

    def __init__(
        self,
        driver: PlatformIsolationDriver,
        *,
        artifact_resolver: Callable[
            [ResolvedOperationBinding], MaterializedPackArtifact
        ]
        | None = None,
    ) -> None:
        if (
            driver.backend_id != PYTHON_PACKVM_BACKEND
            or driver.substrate_id != "windows-whpx"
            or not driver.platform.startswith("windows-")
        ):
            raise BackendUnavailableError("Windows WHPX driver identity mismatch")
        super().__init__(driver, artifact_resolver=artifact_resolver)


class LinuxFirecrackerBackend(ProductionIsolationBackend):
    """Linux Firecracker/KVM PackVM backend."""

    def __init__(
        self,
        driver: PlatformIsolationDriver,
        *,
        artifact_resolver: Callable[
            [ResolvedOperationBinding], MaterializedPackArtifact
        ]
        | None = None,
    ) -> None:
        if (
            driver.backend_id != PYTHON_PACKVM_BACKEND
            or driver.substrate_id != "linux-firecracker"
            or not driver.platform.startswith("linux-")
        ):
            raise BackendUnavailableError("Linux Firecracker driver identity mismatch")
        super().__init__(driver, artifact_resolver=artifact_resolver)


def build_platform_backend(
    *,
    platform_system: str | None = None,
    machine: str | None = None,
    drivers: Iterable[PlatformIsolationDriver] = (),
    artifact_resolver: Callable[
        [ResolvedOperationBinding], MaterializedPackArtifact
    ]
    | None = None,
) -> ProductionIsolationBackend:
    """Build exactly the documented backend for the selected Host platform."""
    system = platform_system or host_platform.system()
    architecture = _normalize_machine(machine or host_platform.machine())
    spec = SUPPORTED_BACKENDS.get(system)
    if spec is None:
        driver: PlatformIsolationDriver = UnavailablePlatformDriver(
            "unsupported-packvm", f"{system.lower()}-{architecture}", "unsupported Host platform"
        )
        return ProductionIsolationBackend(driver, artifact_resolver=artifact_resolver)
    substrate_id, dependency = spec
    platform_id = f"{system.lower().replace('darwin', 'macos')}-{architecture}"
    candidates = [
        item
        for item in drivers
        if getattr(item, "backend_id", None) == PYTHON_PACKVM_BACKEND
        and getattr(item, "substrate_id", None) == substrate_id
        and getattr(item, "platform", None) == platform_id
    ]
    if len(candidates) > 1:
        raise BackendUnavailableError("multiple platform supervisors registered")
    if candidates:
        backend_class = {
            "macos-vz": MacOSVZBackend,
            "windows-whpx": WindowsWHPXBackend,
            "linux-firecracker": LinuxFirecrackerBackend,
        }[substrate_id]
        return backend_class(candidates[0], artifact_resolver=artifact_resolver)
    reason = (
        f"required substrate dependency is unavailable: {dependency}"
        if not Path(dependency).exists()
        else f"authenticated {substrate_id} supervisor is not registered"
    )
    return ProductionIsolationBackend(
        UnavailablePlatformDriver(
            PYTHON_PACKVM_BACKEND,
            platform_id,
            reason,
            substrate_id=substrate_id,
        ),
        artifact_resolver=artifact_resolver,
    )


class ManagedLimaPackVMDriver:
    """Adapter from the explicit Lima provisioner to the v4 platform driver."""

    backend_id = PYTHON_PACKVM_BACKEND
    substrate_id = "lima"

    def __init__(self, provisioner: Any) -> None:
        self._provisioner = provisioner
        self._domains: dict[str, PlatformAttestation] = {}
        self._seen_launches: set[str] = set()
        self._requests: dict[str, tuple[str, str, str]] = {}
        self._request_lock = threading.RLock()
        doctor = provisioner.doctor()
        self.platform = str(doctor.platform)
        self.backend_digest = (
            str(doctor.attestation_digest)
            if doctor.ready and doctor.attestation_digest
            else _digest(
                {
                    "backend_id": self.backend_id,
                    "substrate_id": self.substrate_id,
                    "platform": self.platform,
                    "state": "unavailable",
                }
            )
        )

    def capability(self) -> tuple[bool, str | None]:
        doctor = self._provisioner.doctor()
        if not doctor.ready or doctor.attestation_digest != self.backend_digest:
            return False, doctor.reason or "managed Lima PackVM attestation changed"
        return True, None

    def launch(self, request: IsolationLaunch) -> PlatformAttestation:
        ready, reason = self.capability()
        if not ready:
            raise BackendUnavailableError(reason or "managed Lima PackVM is unavailable")
        if request.backend_id != self.backend_id or request.platform != self.platform:
            raise BackendUnavailableError("managed Lima PackVM launch identity mismatch")
        launch_key = _digest(
            {
                "reservation_id": request.reservation_id,
                "lease_id": request.lease.lease_id,
                "executable_digest": request.executable_digest,
                "artifact_digest": request.artifact_digest,
                "materialization_digest": request.artifact.materialization_digest,
                "backend_digest": self.backend_digest,
            }
        )
        if launch_key in self._seen_launches:
            raise BackendUnavailableError("managed Lima PackVM launch replay")
        self._seen_launches.add(launch_key)
        materialization_nonce = secrets.token_hex(32)
        try:
            staged = self._provisioner.materialize_artifact(
                request.artifact.request_payload(nonce=materialization_nonce)
            )
        except Exception as exc:
            raise BackendUnavailableError(
                "managed Lima PackVM artifact staging failed"
            ) from exc
        expected_staging = {
            "ok": True,
            "protocol": "io.tobkiri.packvm-supervisor.v1",
            "artifact_digest": request.artifact_digest,
            "materialization_digest": request.artifact.materialization_digest,
        }
        if any(staged.get(key) != value for key, value in expected_staging.items()):
            raise BackendUnavailableError(
                "managed Lima PackVM artifact staging identity mismatch"
            )
        guest_artifact_identity = str(staged.get("guest_artifact_identity") or "")
        try:
            require_digest(guest_artifact_identity, "guest artifact identity")
        except Exception as exc:
            raise BackendUnavailableError(
                "managed Lima PackVM artifact staging identity is invalid"
            ) from exc
        attestation_nonce = secrets.token_hex(32)
        attestation = PlatformAttestation(
            domain_id=request.target_domain_id,
            backend_id=self.backend_id,
            backend_digest=self.backend_digest,
            platform=self.platform,
            executable_digest=request.executable_digest,
            artifact_digest=request.artifact_digest,
            materialization_digest=request.artifact.materialization_digest,
            guest_artifact_identity=guest_artifact_identity,
            isolation_profile=request.isolation_profile,
            attestation_digest=_digest("pending-platform-attestation"),
            attestation_nonce=attestation_nonce,
            lease_id=request.lease.lease_id,
            reservation_id=request.reservation_id,
            authenticated_channel=True,
            nonce_fresh=True,
        )
        attestation = replace(
            attestation,
            attestation_digest=_platform_attestation_digest(attestation),
        )
        self._domains[attestation.domain_id] = attestation
        return attestation

    def invoke(self, request: object) -> object:
        domain = getattr(getattr(request, "target_domain", None), "value", None)
        if not isinstance(domain, str):
            raise BackendUnavailableError("managed Lima PackVM domain is invalid")
        attestation = self._domains.get(domain)
        if attestation is None:
            raise BackendUnavailableError("managed Lima PackVM domain is unavailable")
        context = getattr(request, "context", None)
        request_id = getattr(context, "request_id", None)
        if not isinstance(request_id, str) or not request_id:
            raise BackendUnavailableError("managed PackVM request identity is invalid")
        cancel_token = secrets.token_hex(32)
        with self._request_lock:
            if request_id in self._requests:
                raise BackendUnavailableError("managed PackVM request identity is already active")
            self._requests[request_id] = (
                domain,
                attestation.guest_artifact_identity,
                cancel_token,
            )
        payload = {
            "operation": "invoke",
            "request_id": request_id,
            "target_domain": domain,
            "artifact_digest": attestation.artifact_digest,
            "materialization_digest": attestation.materialization_digest,
            "guest_artifact_identity": attestation.guest_artifact_identity,
            "contract_id": getattr(request, "contract_id", None),
            "contract_version": getattr(request, "contract_version", None),
            "operation_id": getattr(request, "operation_id", None),
            "payload": getattr(request, "payload", None),
            "request_digest": getattr(request, "request_digest", None),
            "deadline_monotonic": getattr(request, "deadline_monotonic", None),
            "cancel_token": cancel_token,
        }
        try:
            response = self._provisioner.invoke_guest(payload)
        finally:
            with self._request_lock:
                if self._requests.get(request_id) == (
                    domain,
                    attestation.guest_artifact_identity,
                    cancel_token,
                ):
                    self._requests.pop(request_id, None)
        if response.get("ok") is not True:
            raise BackendUnavailableError(
                f"managed PackVM supervisor rejected invocation: {response.get('error', 'unknown')}"
            )
        result = response.get("payload")
        if not isinstance(result, Mapping):
            raise BackendUnavailableError("managed PackVM supervisor returned invalid payload")
        return ProviderOutcome(result)

    def cancel(self, request_id: str) -> None:
        with self._request_lock:
            ownership = self._requests.get(request_id)
        if ownership is None:
            raise BackendUnavailableError("managed PackVM cancel request is not active")
        domain, guest_artifact_identity, cancel_token = ownership
        try:
            response = self._provisioner.invoke_guest(
                {
                    "operation": "cancel",
                    "request_id": request_id,
                    "target_domain": domain,
                    "guest_artifact_identity": guest_artifact_identity,
                    "cancel_token": cancel_token,
                }
            )
        except Exception as exc:
            raise BackendUnavailableError("managed PackVM cancellation transport failed") from exc
        expected = {
            "ok": True,
            "protocol": "io.tobkiri.packvm-supervisor.v1",
            "operation": "cancel",
            "request_id": request_id,
            "target_domain": domain,
            "state": "cancelled",
        }
        if (
            set(response) != {*expected, "signals"}
            or any(response.get(key) != value for key, value in expected.items())
            or response.get("signals") not in ([], ["TERM"], ["TERM", "KILL"])
        ):
            raise BackendUnavailableError("managed PackVM cancellation ACK mismatch")

    def terminate(self, domain_id: str) -> None:
        with self._request_lock:
            for request_id, ownership in tuple(self._requests.items()):
                if ownership[0] == domain_id:
                    self._requests.pop(request_id, None)
        self._domains.pop(domain_id, None)


def _normalize_machine(value: str) -> str:
    return {"x86_64": "amd64", "AMD64": "amd64", "aarch64": "arm64"}.get(
        value, value.lower()
    )


def _digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _platform_attestation_digest(attestation: PlatformAttestation) -> str:
    """Digest every launch-bound field plus the supervisor freshness nonce."""

    return _digest(
        {
            "domain_id": attestation.domain_id,
            "backend_id": attestation.backend_id,
            "backend_digest": attestation.backend_digest,
            "platform": attestation.platform,
            "executable_digest": attestation.executable_digest,
            "artifact_digest": attestation.artifact_digest,
            "materialization_digest": attestation.materialization_digest,
            "guest_artifact_identity": attestation.guest_artifact_identity,
            "isolation_profile": attestation.isolation_profile,
            "lease_id": attestation.lease_id,
            "reservation_id": attestation.reservation_id,
            "authenticated_channel": attestation.authenticated_channel,
            "nonce_fresh": attestation.nonce_fresh,
            "attestation_nonce": attestation.attestation_nonce,
        }
    )


__all__ = [
    "CapabilityBridge",
    "IsolationLaunch",
    "IsolationLease",
    "LinuxFirecrackerBackend",
    "MacOSVZBackend",
    "ManagedLimaPackVMDriver",
    "PYTHON_PACKVM_BACKEND",
    "PlatformAttestation",
    "PlatformIsolationDriver",
    "ProductionIsolationBackend",
    "SUPPORTED_BACKENDS",
    "WindowsWHPXBackend",
    "build_platform_backend",
]
