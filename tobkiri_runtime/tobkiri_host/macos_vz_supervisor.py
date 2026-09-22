"""Fail-closed direct macOS Virtualization.framework PackVM supervision.

The native helper is deliberately an injected transport.  Python must never
turn a helper's ``authenticated_channel`` claim into production evidence: it
first pins the helper bytes and macOS code-signing identity, then requires a
fresh Host challenge authenticated by the measured PackVM agent for every
operation.

Native helper transport contract
===============================

``MacOSVZSupervisorTransport.exchange`` carries canonical JSON mappings over
an authenticated local transport owned by the signed helper (normally an XPC
endpoint).  It receives a request with ``kind``
``tobkiri.macos-vz.supervisor.request.v1`` and returns exactly:

* ``kind`` ``tobkiri.macos-vz.supervisor.response.v1``;
* ``protocol`` ``io.tobkiri.macos-vz-supervisor.v1`` and ``version`` ``1``;
* the operation, Host nonce, Authority domain, and launch-binding digest from
  the request;
* an operation-specific JSON ``payload``;
* ``agent_mac``: HMAC-SHA-256 over every other response field using the
  per-launch channel secret; and
The outer response is authenticated **only** by ``agent_mac``.  A guest cannot
see that Host--helper secret and the helper must not be a guest signing oracle.
When an operation needs guest evidence, ``payload`` is instead the guest's
signed ``tobkiri.packvm.guest.response.v1`` envelope.  The Host verifies that
inner Ed25519 signature itself against the public key placed by the Host
provisioner in the per-domain allocation.

The launch binding includes helper, immutable image provenance, dynamic COW
disk, per-domain seeds, guest public key, artifact, Authority domain, lease,
and resource reservation identities.  The allocator starts the one-domain
helper and delivers the HMAC key through an inherited 0600 FD/XPC credential
before returning its allocation--never in canonical JSON, logs, or the Pack
child.
No environment variable, Lima process, loopback HTTP listener, or helper
self-report is a substitute for this contract.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import math
import os
from pathlib import Path, PurePosixPath
import platform as host_platform
import plistlib
import re
import secrets
import stat
import struct
import subprocess
import threading
from typing import Any, Callable, Mapping, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from tobkiri_protocol.canonical import canonical_digest, canonical_json

from .effects import ProviderOutcome
from .errors import BackendUnavailableError
from .models import require_digest
from .platform_backends import (
    IsolationLaunch,
    PYTHON_PACKVM_BACKEND,
    PlatformAttestation,
    _platform_attestation_digest,
)


_REQUEST_KIND = "tobkiri.macos-vz.supervisor.request.v1"
_RESPONSE_KIND = "tobkiri.macos-vz.supervisor.response.v1"
_SUPERVISOR_PROTOCOL = "io.tobkiri.macos-vz-supervisor.v1"
_BRIDGE_PROTOCOL = "io.tobkiri.packvm.bridge.v1"
_GUEST_RESPONSE_KIND = "tobkiri.packvm.guest.response.v1"
_GUEST_RESPONSE_PROTOCOL = "io.tobkiri.macos-vz-supervisor.v1"
_HOST_NONCE = re.compile(r"^[a-f0-9]{64}$")
_GUEST_NONCE = re.compile(r"^[a-f0-9]{48}$")
_BRIDGE_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_CONVERSATION_BRIDGE_TARGET = {
    "contract_id": "tobkiri.service.ai.generate.v1",
    "operation_id": "rumi_ai_gateway_pack.ai-gateway.generate",
}


class MacOSVZSupervisorTransport(Protocol):
    """One request/response exchange with the direct signed native helper.

    Implementations must be an OS-authenticated local transport.  This Host
    layer independently verifies the helper on disk and every helper MAC, so a
    transport object returning ``authenticated=True`` is not accepted as
    evidence.  Signed guest envelopes are checked separately by the driver.
    """

    def enroll_launch_secret(
        self,
        *,
        domain_id: str,
        host_nonce: str,
        launch_binding_digest: str,
        secret: bytes,
    ) -> None:
        """Bind this FD-delivered key to one exact domain launch.

        The helper already received ``secret`` out-of-band from the allocator.
        This method may only compare it in memory and bind domain/nonce/digest;
        it must never serialize the secret or forward it to the Pack child.
        """

    def exchange(self, envelope: Mapping[str, Any]) -> Mapping[str, Any]:
        """Send one canonical supervisor request and return one response."""

    def close(self) -> None:
        """Close this one-domain helper only after verified cleanup."""


MacOSVZTransportFactory = Callable[
    ["MacOSVZDomainAllocation"], MacOSVZSupervisorTransport | None
]


class MacOSVZHelperIdentityVerifier(Protocol):
    """Independently verify the native helper file and macOS signing identity."""

    def verify(
        self,
        helper_path: Path,
        expected: "MacOSVZHelperIdentity",
    ) -> tuple[bool, str | None]:
        """Return success only after inspecting local bytes and codesign data."""


@dataclass(frozen=True)
class MacOSVZHelperIdentity:
    """The build-pinned identity of the direct native VZ helper.

    ``binary_digest`` is retained as a compatibility spelling for the
    code-bearing Mach-O digest published by existing package manifests.  It
    is never compared with the whole signed executable: a signature's final
    SuperBlob is intentionally mutable at packaging time.  New callers should
    provide ``code_digest`` as well; if both are supplied they must agree.
    """

    binary_digest: str
    bundle_id: str
    team_id: str
    signing_identity: str
    code_digest: str | None = None

    def __post_init__(self) -> None:
        require_digest(self.binary_digest, "macOS VZ helper code")
        if self.code_digest is not None:
            require_digest(self.code_digest, "macOS VZ helper code")
            if self.code_digest != self.binary_digest:
                raise BackendUnavailableError("macOS VZ helper code identities disagree")
        for label, value in (
            ("macOS VZ helper bundle identity", self.bundle_id),
        ):
            if not isinstance(value, str) or not value or len(value) > 512 or "\x00" in value:
                raise BackendUnavailableError(f"{label} is invalid")
        for label, value in (
            ("macOS VZ helper team identity", self.team_id),
            ("macOS VZ helper signing identity", self.signing_identity),
        ):
            if not isinstance(value, str) or len(value) > 512 or "\x00" in value:
                raise BackendUnavailableError(f"{label} is invalid")

    @property
    def expected_code_digest(self) -> str:
        """Return the stable Mach-O code identity pinned by packaging."""

        return self.code_digest or self.binary_digest

    def to_dict(self) -> dict[str, str]:
        """Return the exact identity included in every launch binding."""

        return {
            "code_digest": self.expected_code_digest,
            "bundle_id": self.bundle_id,
            "team_id": self.team_id,
            "signing_identity": self.signing_identity,
        }


@dataclass(frozen=True)
class MacOSVZLaunchAssets:
    """Measured direct-VZ assets selected before a domain is launched.

    The base image is *provisioning provenance*, not a VZ device.  Each
    domain boots only its unique writable COW disk.  The per-domain
    ``agent-seed.iso`` and ``config-seed.iso`` live in
    :class:`MacOSVZDomainAllocation`; template assets are provenance only and
    never reach launch.
    """

    base_image_digest: str
    base_image_path: str
    agent_template_digest: str
    config_template_digest: str
    base_image_read_only: bool
    boot_mode: str = "efi"

    def __post_init__(self) -> None:
        for label, value in (
            ("macOS VZ base image", self.base_image_digest),
            ("macOS VZ agent template", self.agent_template_digest),
            ("macOS VZ config template", self.config_template_digest),
        ):
            require_digest(value, label)
        for label, value in (
            ("macOS VZ base image path", self.base_image_path),
        ):
            if (
                not isinstance(value, str)
                or not value.startswith("/")
                or len(value) > 4096
                or "\x00" in value
                or ".." in PurePosixPath(value).parts
            ):
                raise BackendUnavailableError(f"{label} is invalid")
        if self.base_image_read_only is not True:
            raise BackendUnavailableError("macOS VZ base image must be read-only")
        if self.boot_mode != "efi":
            raise BackendUnavailableError("macOS VZ requires EFI boot mode")

    def to_dict(self) -> dict[str, str | bool]:
        """Return launch-bound identities without exposing the base path."""

        return {
            "base_image_digest": self.base_image_digest,
            "agent_template_digest": self.agent_template_digest,
            "config_template_digest": self.config_template_digest,
            "base_image_read_only": self.base_image_read_only,
            "boot_mode": self.boot_mode,
        }


@dataclass(frozen=True)
class MacOSVZRuntime:
    """Host-selected resource limits for one direct VZ guest."""

    cpu_count: int = 1
    memory_bytes: int = 512 * 1024 * 1024
    guest_vsock_port: int = 19001

    def __post_init__(self) -> None:
        available_cpus = os.cpu_count() or 1
        if (
            not isinstance(self.cpu_count, int)
            or isinstance(self.cpu_count, bool)
            or not 1 <= self.cpu_count <= min(available_cpus, 4)
        ):
            raise BackendUnavailableError("macOS VZ CPU limit is invalid")
        if (
            not isinstance(self.memory_bytes, int)
            or isinstance(self.memory_bytes, bool)
            or not 512 * 1024 * 1024 <= self.memory_bytes <= 4 * 1024 * 1024 * 1024
        ):
            raise BackendUnavailableError("macOS VZ memory limit is invalid")
        if self.guest_vsock_port != 19001:
            raise BackendUnavailableError("macOS VZ guest vsock port is invalid")

    def to_dict(self) -> dict[str, int]:
        """Return the exact runtime configuration handed to the helper."""

        return {
            "cpu_count": self.cpu_count,
            "memory_bytes": self.memory_bytes,
            "guest_vsock_port": self.guest_vsock_port,
        }


@dataclass(frozen=True)
class MacOSVZAgentIdentity:
    """Measured immutable guest-agent code identity.

    Guest response verification uses the fresh public key returned by each
    Host provisioner allocation, not a static bundle key.
    """

    agent_digest: str
    public_key: bytes | None = None

    def __post_init__(self) -> None:
        require_digest(self.agent_digest, "macOS VZ agent")
        if self.public_key is not None:
            if not isinstance(self.public_key, bytes) or len(self.public_key) != 32:
                raise BackendUnavailableError("macOS VZ agent public key is invalid")
            try:
                Ed25519PublicKey.from_public_bytes(self.public_key)
            except ValueError as exc:
                raise BackendUnavailableError(
                    "macOS VZ agent public key is invalid"
                ) from exc


@dataclass(frozen=True)
class MacOSVZDomainAllocation:
    """One Host-owned mutable COW disk/EFI allocation for a single domain."""

    domain_id: str
    reservation_id: str
    lease_id: str
    run_root: str
    cow_disk_path: str
    cow_disk_digest: str
    efi_store_path: str
    efi_variable_store_digest: str
    agent_seed_path: str
    agent_seed_digest: str
    config_seed_path: str
    config_seed_digest: str
    guest_public_key: bytes

    def __post_init__(self) -> None:
        for label, value in (
            ("macOS VZ allocation domain", self.domain_id),
            ("macOS VZ allocation reservation", self.reservation_id),
            ("macOS VZ allocation lease", self.lease_id),
        ):
            if not isinstance(value, str) or not value or len(value) > 512 or "\x00" in value:
                raise BackendUnavailableError(f"{label} is invalid")
        paths = (
            self.run_root,
            self.cow_disk_path,
            self.efi_store_path,
            self.agent_seed_path,
            self.config_seed_path,
        )
        if any(
            not isinstance(path, str)
            or not path.startswith("/")
            or len(path) > 4096
            or "\x00" in path
            or ".." in PurePosixPath(path).parts
            for path in paths
        ):
            raise BackendUnavailableError("macOS VZ allocation path is invalid")
        root = self.run_root.rstrip("/")
        if not all(path.startswith(root + "/") for path in paths[1:]):
            raise BackendUnavailableError("macOS VZ allocation escapes its run root")
        for label, value in (
            ("macOS VZ COW disk", self.cow_disk_digest),
            ("macOS VZ EFI variable store", self.efi_variable_store_digest),
            ("macOS VZ agent seed", self.agent_seed_digest),
            ("macOS VZ config seed", self.config_seed_digest),
        ):
            require_digest(value, label)
        if not isinstance(self.guest_public_key, bytes) or len(self.guest_public_key) != 32:
            raise BackendUnavailableError("macOS VZ allocation guest public key is invalid")
        try:
            Ed25519PublicKey.from_public_bytes(self.guest_public_key)
        except ValueError as exc:
            raise BackendUnavailableError(
                "macOS VZ allocation guest public key is invalid"
            ) from exc

    @property
    def guest_public_key_digest(self) -> str:
        """Return the digest of the Host-provisioned per-domain public key."""

        return "sha256:" + hashlib.sha256(self.guest_public_key).hexdigest()

    def to_dict(self) -> dict[str, str]:
        """Return the exact dynamic VZ facts passed to the trusted helper."""

        return {
            "domain_id": self.domain_id,
            "reservation_id": self.reservation_id,
            "lease_id": self.lease_id,
            "run_root": self.run_root,
            "cow_disk_path": self.cow_disk_path,
            "cow_disk_digest": self.cow_disk_digest,
            "efi_store_path": self.efi_store_path,
            "efi_variable_store_digest": self.efi_variable_store_digest,
            "agent_seed_path": self.agent_seed_path,
            "agent_seed_digest": self.agent_seed_digest,
            "config_seed_path": self.config_seed_path,
            "config_seed_digest": self.config_seed_digest,
            "guest_public_key": _b64encode(self.guest_public_key),
            "guest_public_key_digest": self.guest_public_key_digest,
        }


class MacOSVZDomainAllocator(Protocol):
    """Create/release one unique private COW disk and EFI store per domain."""

    def allocate(
        self,
        *,
        domain_id: str,
        reservation_id: str,
        lease_id: str,
        artifact_digest: str,
        executable_digest: str,
        materialization_digest: str,
        channel_key: bytes,
    ) -> MacOSVZDomainAllocation:
        """Return a new allocation with a live FD-enrolled helper channel.

        ``channel_key`` is sent only through the allocator's inherited
        owner-only FD/XPC credential.  It must never be retained in allocation
        state, serialized JSON, or a launch binding.
        """

    def release(self, allocation: MacOSVZDomainAllocation) -> None:
        """Remove allocation only after a verified guest cleanup acknowledgement."""


CapabilityBridge = Callable[[object, Mapping[str, Any]], Mapping[str, Any]]


@dataclass(frozen=True)
class _DomainSession:
    attestation: PlatformAttestation
    channel_key: bytes
    launch_binding_digest: str
    allocation: MacOSVZDomainAllocation
    binding_digests: Mapping[str, str]
    transport: MacOSVZSupervisorTransport


@dataclass(frozen=True)
class _ActiveRequest:
    domain_id: str
    request_digest: str
    channel_key: bytes
    launch_binding_digest: str
    guest_challenge: str
    transport: MacOSVZSupervisorTransport


class MacOSVZSupervisorDriver:
    """Direct VZ driver that authenticates every Host/guest transition.

    It intentionally has no Lima adapter, no HTTP fallback, and no path that
    treats a transport status bit as a channel proof.  The default identity
    verifier invokes ``codesign`` on macOS; tests may inject a trusted verifier
    only to model that OS-owned boundary.
    """

    backend_id = PYTHON_PACKVM_BACKEND
    substrate_id = "macos-vz"

    def __init__(
        self,
        *,
        transport_factory: MacOSVZTransportFactory | None,
        helper_path: Path,
        helper_identity: MacOSVZHelperIdentity,
        launch_assets: MacOSVZLaunchAssets,
        agent_identity: MacOSVZAgentIdentity,
        domain_allocator: MacOSVZDomainAllocator | None,
        runtime: MacOSVZRuntime | None = None,
        platform: str = "macos-arm64",
        identity_verifier: MacOSVZHelperIdentityVerifier | None = None,
        nonce_factory: Callable[[], str] | None = None,
        guest_challenge_factory: Callable[[], str] | None = None,
        channel_key_factory: Callable[[], bytes] | None = None,
        max_nonce_ledger_entries: int = 8192,
    ) -> None:
        if not platform.startswith("macos-"):
            raise BackendUnavailableError("direct VZ driver platform is invalid")
        if not isinstance(max_nonce_ledger_entries, int) or max_nonce_ledger_entries < 64:
            raise BackendUnavailableError("macOS VZ nonce ledger bound is invalid")
        self.platform = platform
        self._transport_factory = transport_factory
        self._helper_path = Path(helper_path)
        self._helper_identity = helper_identity
        self._launch_assets = launch_assets
        self._agent_identity = agent_identity
        self._domain_allocator = domain_allocator
        self._runtime = runtime or MacOSVZRuntime()
        self._identity_verifier = identity_verifier or verify_macos_vz_helper_identity
        self._nonce_factory = nonce_factory or (lambda: secrets.token_hex(32))
        self._guest_challenge_factory = guest_challenge_factory or (
            lambda: secrets.token_hex(32)
        )
        self._channel_key_factory = channel_key_factory or (lambda: secrets.token_bytes(32))
        self.backend_digest = canonical_digest(
            {
                "backend_id": self.backend_id,
                "substrate_id": self.substrate_id,
                "platform": self.platform,
                "helper": helper_identity.to_dict(),
                "launch_assets": launch_assets.to_dict(),
                "agent_code_digest": agent_identity.agent_digest,
                "runtime": self._runtime.to_dict(),
            }
        )
        self._domains: dict[str, _DomainSession] = {}
        self._transport_domains: dict[int, str] = {}
        self._active_requests: dict[str, _ActiveRequest] = {}
        self._host_nonce_ledger: set[str] = set()
        self._guest_challenge_ledger: dict[str, str] = {}
        self._guest_nonce_ledger: dict[tuple[str, str], tuple[str, str]] = {}
        self._max_nonce_ledger_entries = max_nonce_ledger_entries
        self._capability_bridge: CapabilityBridge | None = None
        self._compromised_reason: str | None = None
        self._lock = threading.RLock()

    def capability(self) -> tuple[bool, str | None]:
        """Check only local, independently-verifiable production prerequisites."""

        with self._lock:
            if self._compromised_reason is not None:
                return False, self._compromised_reason
        if self._transport_factory is None:
            return False, "macOS VZ native helper transport factory is unavailable"
        if self._domain_allocator is None:
            return False, "macOS VZ per-domain allocator is unavailable"
        try:
            verifier = self._identity_verifier
            if callable(verifier):
                verified, reason = verifier(self._helper_path, self._helper_identity)
            else:
                verified, reason = verifier.verify(
                    self._helper_path,
                    self._helper_identity,
                )
        except Exception:
            return False, "macOS VZ helper identity verification failed"
        if not verified:
            return False, reason or "macOS VZ helper identity verification failed"
        return True, None

    def bind_capability_bridge(self, callback: CapabilityBridge) -> None:
        """Bind the Host-owned bridge before a PackVM domain is materialized."""

        if not callable(callback):
            raise BackendUnavailableError("macOS VZ capability bridge is invalid")
        with self._lock:
            if self._domains or self._active_requests:
                raise BackendUnavailableError(
                    "macOS VZ capability bridge cannot change after launch"
                )
            if self._capability_bridge is not None and self._capability_bridge is not callback:
                raise BackendUnavailableError("macOS VZ capability bridge is already bound")
            self._capability_bridge = callback

    def launch(self, request: IsolationLaunch) -> PlatformAttestation:
        """Launch one exact, fully pinned PackVM domain."""

        ready, reason = self.capability()
        if not ready:
            raise BackendUnavailableError(reason or "macOS VZ PackVM is unavailable")
        if request.backend_id != self.backend_id or request.platform != self.platform:
            raise BackendUnavailableError("macOS VZ launch identity mismatch")
        with self._lock:
            if request.target_domain_id in self._domains:
                raise BackendUnavailableError("macOS VZ domain identity is already active")
        channel_key = self._new_channel_key()
        transport: MacOSVZSupervisorTransport | None = None
        try:
            if self._domain_allocator is None:
                raise RuntimeError("allocator unavailable")
            allocation = self._domain_allocator.allocate(
                domain_id=request.target_domain_id,
                reservation_id=request.reservation_id,
                lease_id=request.lease.lease_id,
                artifact_digest=request.artifact_digest,
                executable_digest=request.executable_digest,
                materialization_digest=request.artifact.materialization_digest,
                channel_key=channel_key,
            )
        except Exception as exc:
            raise BackendUnavailableError("macOS VZ domain allocation failed") from exc
        if (
            allocation.domain_id != request.target_domain_id
            or allocation.reservation_id != request.reservation_id
            or allocation.lease_id != request.lease.lease_id
        ):
            self._release_unlaunched_allocation(allocation, transport)
            raise BackendUnavailableError("macOS VZ domain allocation identity mismatch")
        try:
            self._verify_launch_assets(allocation)
            host_nonce = self._new_host_nonce()
            guest_challenge = self._new_guest_challenge(request.target_domain_id)
            guest_request_id = f"attest-{request.target_domain_id}"
            if len(guest_request_id) > 128:
                raise BackendUnavailableError("macOS VZ guest attestation request is invalid")
            binding_digests = self._binding_digests(request, allocation)
            binding = self._launch_binding(request, allocation, binding_digests)
            binding_digest = canonical_digest(binding)
            transport = self._new_transport(allocation)
            transport.enroll_launch_secret(
                domain_id=request.target_domain_id,
                host_nonce=host_nonce,
                launch_binding_digest=binding_digest,
                secret=channel_key,
            )
        except Exception:
            self._release_unlaunched_allocation(allocation, transport)
            raise
        try:
            response = self._exchange(
                {
                "kind": _REQUEST_KIND,
                "protocol": _SUPERVISOR_PROTOCOL,
                "version": 1,
                "operation": "launch",
                "host_nonce": host_nonce,
                "domain_id": request.target_domain_id,
                "launch_binding": binding,
                "launch_binding_digest": binding_digest,
                "guest_challenge": guest_challenge,
                },
                transport=transport,
                channel_key=channel_key,
                expected_operation="launch",
                expected_host_nonce=host_nonce,
                expected_domain_id=request.target_domain_id,
                expected_binding_digest=binding_digest,
            )
        except Exception:
            self._release_unlaunched_allocation(allocation, transport)
            raise
        try:
            launch_data = self._validated_guest_response(
                response["payload"],
                operation="attest",
                request_id=guest_request_id,
                domain_id=request.target_domain_id,
                binding_digests=binding_digests,
                guest_challenge=guest_challenge,
                public_key=allocation.guest_public_key,
                attestation_nonce=host_nonce,
            )
        except BackendUnavailableError:
            self._release_unlaunched_allocation(allocation, transport)
            raise
        if set(launch_data) != {"guest_artifact_identity"}:
            self._compromise("macOS VZ launch guest acknowledgement is invalid")
            self._release_unlaunched_allocation(allocation, transport)
            raise BackendUnavailableError("macOS VZ launch guest acknowledgement is invalid")
        guest_artifact_identity = launch_data.get("guest_artifact_identity")
        if not isinstance(guest_artifact_identity, str):
            self._compromise("macOS VZ guest artifact identity is invalid")
            self._release_unlaunched_allocation(allocation, transport)
            raise BackendUnavailableError("macOS VZ guest artifact identity is invalid")
        try:
            require_digest(guest_artifact_identity, "macOS VZ guest artifact")
            if guest_artifact_identity != canonical_digest(binding_digests):
                raise ValueError("guest artifact identity differs from launch binding")
        except Exception as exc:
            self._compromise("macOS VZ guest artifact identity is invalid")
            self._release_unlaunched_allocation(allocation, transport)
            raise BackendUnavailableError("macOS VZ guest artifact identity is invalid") from exc
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
            attestation_digest=canonical_digest({"state": "pending"}),
            attestation_nonce=host_nonce,
            lease_id=request.lease.lease_id,
            reservation_id=request.reservation_id,
            authenticated_channel=True,
            nonce_fresh=True,
        )
        attestation = PlatformAttestation(
            **{
                **attestation.__dict__,
                "attestation_digest": _platform_attestation_digest(attestation),
            }
        )
        with self._lock:
            self._domains[attestation.domain_id] = _DomainSession(
                attestation=attestation,
                channel_key=channel_key,
                launch_binding_digest=binding_digest,
                allocation=allocation,
                binding_digests=binding_digests,
                transport=transport,
            )
        return attestation

    def invoke(self, request: object) -> ProviderOutcome:
        """Invoke only within an authenticated, Host-owned active domain."""

        domain_id, request_id, request_digest = _request_identity(request)
        with self._lock:
            session = self._domains.get(domain_id)
            if session is None:
                raise BackendUnavailableError("macOS VZ domain is unavailable")
            if request_id in self._active_requests:
                raise BackendUnavailableError("macOS VZ request identity is already active")
            guest_challenge = self._new_guest_challenge(domain_id)
            self._active_requests[request_id] = _ActiveRequest(
                domain_id=domain_id,
                request_digest=request_digest,
                channel_key=session.channel_key,
                launch_binding_digest=session.launch_binding_digest,
                guest_challenge=guest_challenge,
                transport=session.transport,
            )
        try:
            response = self._exchange(
                self._invoke_envelope(
                    request,
                    domain_id,
                    session,
                    request_id,
                    request_digest,
                    guest_challenge,
                ),
                transport=session.transport,
                channel_key=session.channel_key,
                expected_operation="invoke",
                expected_host_nonce=None,
                expected_domain_id=domain_id,
                expected_binding_digest=session.launch_binding_digest,
            )
            guest_data = self._validated_guest_response(
                response["payload"],
                operation="invoke",
                request_id=request_id,
                domain_id=domain_id,
                binding_digests=session.binding_digests,
                guest_challenge=guest_challenge,
                public_key=session.allocation.guest_public_key,
            )
            if (
                set(guest_data) == {"state", "host_bridge_request"}
                and guest_data.get("state") == "pending"
                and isinstance(guest_data.get("host_bridge_request"), Mapping)
            ):
                return self._complete_bridge(
                    request,
                    domain_id,
                    session,
                    dict(guest_data["host_bridge_request"]),
                )
            return ProviderOutcome(_validated_invoke_outcome(guest_data))
        finally:
            with self._lock:
                self._active_requests.pop(request_id, None)

    def cancel(self, request_id: str) -> None:
        """Cancel only the currently owned request and verify the guest ACK."""

        with self._lock:
            active = self._active_requests.get(request_id)
            if active is None:
                raise BackendUnavailableError("macOS VZ cancel request is not active")
        host_nonce = self._new_host_nonce()
        guest_challenge = self._new_guest_challenge(active.domain_id)
        response = self._exchange(
            {
                "kind": _REQUEST_KIND,
                "protocol": _SUPERVISOR_PROTOCOL,
                "version": 1,
                "operation": "cancel",
                "host_nonce": host_nonce,
                "domain_id": active.domain_id,
                "launch_binding_digest": active.launch_binding_digest,
                "request_id": request_id,
                "request_digest": active.request_digest,
                "guest_challenge": guest_challenge,
            },
            transport=active.transport,
            channel_key=active.channel_key,
            expected_operation="cancel",
            expected_host_nonce=host_nonce,
            expected_domain_id=active.domain_id,
            expected_binding_digest=active.launch_binding_digest,
        )
        with self._lock:
            session = self._domains.get(active.domain_id)
        if session is None:
            raise BackendUnavailableError("macOS VZ cancel domain is unavailable")
        payload = self._validated_guest_response(
            response["payload"],
            operation="cancel",
            request_id=request_id,
            domain_id=active.domain_id,
            binding_digests=session.binding_digests,
            guest_challenge=guest_challenge,
            public_key=session.allocation.guest_public_key,
        )
        if (
            set(payload) != {"state", "request_id", "signals"}
            or payload.get("state") != "cancelled"
            or payload.get("request_id") != request_id
            or payload.get("signals") not in ([], ["TERM"], ["TERM", "KILL"])
        ):
            self._compromise("macOS VZ cancellation acknowledgement is invalid")
            raise BackendUnavailableError("macOS VZ cancellation acknowledgement is invalid")

    def terminate(self, domain_id: str) -> None:
        """Destroy a domain only after a signed cleanup acknowledgement."""

        with self._lock:
            session = self._domains.get(domain_id)
        if session is None:
            return
        host_nonce = self._new_host_nonce()
        response = self._exchange(
            {
                "kind": _REQUEST_KIND,
                "protocol": _SUPERVISOR_PROTOCOL,
                "version": 1,
                "operation": "terminate",
                "host_nonce": host_nonce,
                "domain_id": domain_id,
                "launch_binding_digest": session.launch_binding_digest,
                "lease_id": session.attestation.lease_id,
                "reservation_id": session.attestation.reservation_id,
            },
            transport=session.transport,
            channel_key=session.channel_key,
            expected_operation="terminate",
            expected_host_nonce=host_nonce,
            expected_domain_id=domain_id,
            expected_binding_digest=session.launch_binding_digest,
        )
        payload = response["payload"]
        expected_cleanup = {
            "state": "terminated",
            "domain_id": domain_id,
            "lease_id": session.attestation.lease_id,
            "reservation_id": session.attestation.reservation_id,
            "cleanup": {
                "vm": "released",
                "cow_disk": "detached",
                "efi_store": "detached",
            },
        }
        if payload != expected_cleanup:
            self._compromise("macOS VZ termination cleanup acknowledgement is invalid")
            raise BackendUnavailableError(
                "macOS VZ termination cleanup acknowledgement is invalid"
            )
        try:
            session.transport.close()
        except Exception as exc:
            self._compromise("macOS VZ helper transport cleanup failed")
            raise BackendUnavailableError("macOS VZ helper transport cleanup failed") from exc
        try:
            if self._domain_allocator is None:
                raise RuntimeError("allocator unavailable")
            self._domain_allocator.release(session.allocation)
        except Exception as exc:
            self._compromise("macOS VZ allocation cleanup failed")
            raise BackendUnavailableError("macOS VZ allocation cleanup failed") from exc
        with self._lock:
            self._domains.pop(domain_id, None)
            self._transport_domains.pop(id(session.transport), None)
            for request_id, active in tuple(self._active_requests.items()):
                if active.domain_id == domain_id:
                    self._active_requests.pop(request_id, None)
            for key in tuple(self._guest_nonce_ledger):
                if key[0] == domain_id:
                    self._guest_nonce_ledger.pop(key, None)
            for challenge, challenge_domain_id in tuple(
                self._guest_challenge_ledger.items()
            ):
                if challenge_domain_id == domain_id:
                    self._guest_challenge_ledger.pop(challenge, None)

    def _complete_bridge(
        self,
        outer_request: object,
        domain_id: str,
        session: _DomainSession,
        host_frame: Mapping[str, Any],
    ) -> ProviderOutcome:
        outer_domain_id, outer_request_id, outer_request_digest = _request_identity(
            outer_request
        )
        if outer_domain_id != domain_id:
            self._compromise("macOS VZ bridge domain binding mismatch")
            raise BackendUnavailableError("macOS VZ bridge domain binding mismatch")
        bridge_request = _validate_host_bridge_request(
            host_frame,
            request_id=outer_request_id,
            domain_id=domain_id,
            guest_artifact_identity=session.attestation.guest_artifact_identity,
            request_digest=outer_request_digest,
            deadline_monotonic=_deadline_value(
                getattr(outer_request, "deadline_monotonic", None)
            ),
        )
        continuation = _validate_bridge_request(bridge_request)
        guest_nonce = continuation["nonce"]
        binding = (
            canonical_digest(continuation["target"]),
            continuation["request_digest"],
        )
        with self._lock:
            if len(self._guest_nonce_ledger) >= self._max_nonce_ledger_entries:
                self._compromise("macOS VZ guest nonce ledger is exhausted")
                raise BackendUnavailableError("macOS VZ guest nonce ledger is exhausted")
            key = (domain_id, guest_nonce)
            if key in self._guest_nonce_ledger:
                self._compromise("macOS VZ guest bridge nonce replay")
                raise BackendUnavailableError("macOS VZ guest bridge nonce replay")
            self._guest_nonce_ledger[key] = binding
            callback = self._capability_bridge
        if callback is None:
            raise BackendUnavailableError("macOS VZ Host capability bridge is unavailable")
        try:
            bridge_result = callback(outer_request, bridge_request)
        except Exception as exc:
            raise BackendUnavailableError("macOS VZ Host capability bridge rejected request") from exc
        _validate_bridge_result(bridge_result, continuation)
        host_nonce = self._new_host_nonce()
        guest_challenge = self._new_guest_challenge(domain_id)
        response = self._exchange(
            {
                "kind": _REQUEST_KIND,
                "protocol": _SUPERVISOR_PROTOCOL,
                "version": 1,
                "operation": "bridge_result",
                "host_nonce": host_nonce,
                "domain_id": domain_id,
                "launch_binding_digest": session.launch_binding_digest,
                "guest_challenge": guest_challenge,
                "host_bridge_result": {
                    "kind": "tobkiri.packvm.bridge.host-result.v1",
                    "protocol": _BRIDGE_PROTOCOL,
                    "version": 1,
                    "request_id": outer_request_id,
                    "target_domain": domain_id,
                    "guest_artifact_identity": session.attestation.guest_artifact_identity,
                    "request_digest": outer_request_digest,
                    "bridge_request_digest": canonical_digest(dict(bridge_request)),
                    "continuation_nonce": continuation["nonce"],
                    "bridge_result": dict(bridge_result),
                    "bridge_result_digest": canonical_digest(dict(bridge_result)),
                },
            },
            transport=session.transport,
            channel_key=session.channel_key,
            expected_operation="bridge_result",
            expected_host_nonce=host_nonce,
            expected_domain_id=domain_id,
            expected_binding_digest=session.launch_binding_digest,
        )
        guest_data = self._validated_guest_response(
            response["payload"],
            operation="bridge_result",
            request_id=outer_request_id,
            domain_id=domain_id,
            binding_digests=session.binding_digests,
            guest_challenge=guest_challenge,
            public_key=session.allocation.guest_public_key,
        )
        return ProviderOutcome(_validated_invoke_outcome(guest_data))

    def _invoke_envelope(
        self,
        request: object,
        domain_id: str,
        session: _DomainSession,
        request_id: str,
        request_digest: str,
        guest_challenge: str,
    ) -> dict[str, Any]:
        payload = getattr(request, "payload", None)
        if not isinstance(payload, Mapping):
            raise BackendUnavailableError("macOS VZ provider payload is invalid")
        host_nonce = self._new_host_nonce()
        return {
            "kind": _REQUEST_KIND,
            "protocol": _SUPERVISOR_PROTOCOL,
            "version": 1,
            "operation": "invoke",
            "host_nonce": host_nonce,
            "domain_id": domain_id,
            "launch_binding_digest": session.launch_binding_digest,
            "guest_challenge": guest_challenge,
            "request": {
                "request_id": request_id,
                "request_digest": request_digest,
                "contract_id": _bounded_text(getattr(request, "contract_id", None), "contract"),
                "contract_version": _bounded_text(
                    getattr(request, "contract_version", None), "contract version"
                ),
                "operation_id": _bounded_text(
                    getattr(request, "operation_id", None), "operation"
                ),
                "payload": dict(payload),
                "deadline_monotonic": _deadline_value(
                    getattr(request, "deadline_monotonic", None)
                ),
            },
        }

    def _launch_binding(
        self,
        request: IsolationLaunch,
        allocation: MacOSVZDomainAllocation,
        binding_digests: Mapping[str, str],
    ) -> dict[str, Any]:
        return {
            "kind": "tobkiri.macos-vz.launch-binding.v1",
            "version": 1,
            "backend_id": self.backend_id,
            "backend_digest": self.backend_digest,
            "platform": self.platform,
            "helper": self._helper_identity.to_dict(),
            "launch_assets": self._launch_assets.to_dict(),
            "domain_allocation": allocation.to_dict(),
            "agent_code_digest": self._agent_identity.agent_digest,
            "runtime": self._runtime.to_dict(),
            "binding_digests": dict(binding_digests),
            "artifact": {
                "artifact_digest": request.artifact_digest,
                "executable_digest": request.executable_digest,
                "materialization_digest": request.artifact.materialization_digest,
                "guest_payload_digest": canonical_digest(
                    request.artifact.request_payload(nonce="0" * 64)
                ),
            },
            "domain_id": request.target_domain_id,
            "isolation_profile": request.isolation_profile,
            "lease": {
                "lease_id": request.lease.lease_id,
                "reservation_id": request.lease.reservation_id,
                "expires_monotonic_ns": _launch_deadline_ns(
                    request.lease.expires_monotonic
                ),
            },
            "reservation_id": request.reservation_id,
        }

    def _exchange(
        self,
        envelope: Mapping[str, Any],
        *,
        transport: MacOSVZSupervisorTransport,
        channel_key: bytes,
        expected_operation: str,
        expected_host_nonce: str | None,
        expected_domain_id: str,
        expected_binding_digest: str,
    ) -> dict[str, Any]:
        host_nonce = envelope.get("host_nonce")
        if not isinstance(host_nonce, str):
            raise BackendUnavailableError("macOS VZ Host nonce is invalid")
        if expected_host_nonce is not None and host_nonce != expected_host_nonce:
            raise BackendUnavailableError("macOS VZ Host nonce binding is invalid")
        try:
            raw = transport.exchange(dict(envelope))
        except Exception as exc:
            raise BackendUnavailableError("macOS VZ native helper transport failed") from exc
        if not isinstance(raw, Mapping):
            self._compromise("macOS VZ native helper response is invalid")
            raise BackendUnavailableError("macOS VZ native helper response is invalid")
        response = dict(raw)
        required = {
            "kind",
            "protocol",
            "version",
            "operation",
            "host_nonce",
            "domain_id",
            "launch_binding_digest",
            "payload",
            "agent_mac",
        }
        if set(response) != required or not isinstance(response.get("payload"), Mapping):
            self._compromise("macOS VZ native helper response is invalid")
            raise BackendUnavailableError("macOS VZ native helper response is invalid")
        if (
            response["kind"] != _RESPONSE_KIND
            or response["protocol"] != _SUPERVISOR_PROTOCOL
            or response["version"] != 1
            or response["operation"] != expected_operation
            or response["host_nonce"] != host_nonce
            or response["domain_id"] != expected_domain_id
            or response["launch_binding_digest"] != expected_binding_digest
        ):
            self._compromise("macOS VZ native helper response binding mismatch")
            raise BackendUnavailableError("macOS VZ native helper response binding mismatch")
        core = {
            key: value
            for key, value in response.items()
            if key != "agent_mac"
        }
        try:
            encoded = canonical_json(core)
        except Exception as exc:
            self._compromise("macOS VZ native helper response is not canonical")
            raise BackendUnavailableError("macOS VZ native helper response is not canonical") from exc
        expected_mac = hmac.new(channel_key, encoded, hashlib.sha256).hexdigest()
        mac = response["agent_mac"]
        if not isinstance(mac, str) or not hmac.compare_digest(mac, expected_mac):
            self._compromise("macOS VZ helper MAC verification failed")
            raise BackendUnavailableError("macOS VZ helper MAC verification failed")
        return response

    def _new_host_nonce(self) -> str:
        candidate = self._nonce_factory()
        if not isinstance(candidate, str) or _HOST_NONCE.fullmatch(candidate) is None:
            self._compromise("macOS VZ Host nonce source is invalid")
            raise BackendUnavailableError("macOS VZ Host nonce source is invalid")
        with self._lock:
            if len(self._host_nonce_ledger) >= self._max_nonce_ledger_entries:
                self._compromise("macOS VZ Host nonce ledger is exhausted")
                raise BackendUnavailableError("macOS VZ Host nonce ledger is exhausted")
            if candidate in self._host_nonce_ledger:
                self._compromise("macOS VZ Host nonce replay")
                raise BackendUnavailableError("macOS VZ Host nonce replay")
            self._host_nonce_ledger.add(candidate)
        return candidate

    def _new_channel_key(self) -> bytes:
        candidate = self._channel_key_factory()
        if not isinstance(candidate, bytes) or len(candidate) != 32:
            self._compromise("macOS VZ channel key source is invalid")
            raise BackendUnavailableError("macOS VZ channel key source is invalid")
        return candidate

    def _new_guest_challenge(self, domain_id: str) -> str:
        """Mint a one-shot Host challenge for a signed guest response."""

        candidate = self._guest_challenge_factory()
        if not isinstance(candidate, str) or _HOST_NONCE.fullmatch(candidate) is None:
            self._compromise("macOS VZ guest challenge source is invalid")
            raise BackendUnavailableError("macOS VZ guest challenge source is invalid")
        with self._lock:
            if len(self._guest_challenge_ledger) >= self._max_nonce_ledger_entries:
                self._compromise("macOS VZ guest challenge ledger is exhausted")
                raise BackendUnavailableError("macOS VZ guest challenge ledger is exhausted")
            if candidate in self._guest_challenge_ledger:
                self._compromise("macOS VZ guest challenge replay")
                raise BackendUnavailableError("macOS VZ guest challenge replay")
            self._guest_challenge_ledger[candidate] = domain_id
        return candidate

    def _verify_launch_assets(self, allocation: MacOSVZDomainAllocation) -> None:
        """Re-measure all Host-selected paths using no-follow stable hashes."""

        measured = (
            (self._launch_assets.base_image_path, self._launch_assets.base_image_digest,
             "macOS VZ base-image provenance"),
            (allocation.cow_disk_path, allocation.cow_disk_digest, "macOS VZ COW disk"),
            (allocation.efi_store_path, allocation.efi_variable_store_digest,
             "macOS VZ EFI variable store"),
            (allocation.agent_seed_path, allocation.agent_seed_digest,
             "macOS VZ agent seed"),
            (allocation.config_seed_path, allocation.config_seed_digest,
             "macOS VZ config seed"),
        )
        for raw_path, expected_digest, label in measured:
            try:
                _identity, observed = _secure_file_digest(Path(raw_path))
            except (OSError, ValueError) as exc:
                raise BackendUnavailableError(f"{label} identity verification failed") from exc
            if observed != expected_digest:
                raise BackendUnavailableError(f"{label} digest mismatch")

    def _binding_digests(
        self,
        request: IsolationLaunch,
        allocation: MacOSVZDomainAllocation,
    ) -> dict[str, str]:
        """Return the complete pinned fact set echoed by signed guest replies."""

        return {
            "domain": _text_digest(request.target_domain_id),
            "lease": _text_digest(request.lease.lease_id),
            "reservation": _text_digest(request.reservation_id),
            "image": self._launch_assets.base_image_digest,
            "agent": self._agent_identity.agent_digest,
            # The config seed embeds this map.  Bind its immutable template
            # identity here and bind the generated seed separately in the
            # launch allocation, avoiding a self-hash fixed point.
            "config": self._launch_assets.config_template_digest,
            "disk": allocation.cow_disk_digest,
            "efi_variable_store": allocation.efi_variable_store_digest,
            "guest_public_key": allocation.guest_public_key_digest,
            "artifact": request.artifact_digest,
            "executable": request.executable_digest,
            "materialization": request.artifact.materialization_digest,
        }

    def _validated_guest_response(
        self,
        payload: Mapping[str, Any],
        *,
        operation: str,
        request_id: str,
        domain_id: str,
        binding_digests: Mapping[str, str],
        guest_challenge: str,
        public_key: bytes,
        attestation_nonce: str | None = None,
    ) -> dict[str, Any]:
        """Verify the runner's exact signed reply without trusting the helper."""

        expected = {
            "kind",
            "protocol",
            "version",
            "operation",
            "request_id",
            "domain_id",
            "binding_digests",
            "guest_challenge",
            "success",
            "agent_signature",
        }
        if attestation_nonce is not None:
            expected.add("attestation_nonce")
        success = payload.get("success")
        if success is True:
            expected.add("data")
            outcome_valid = isinstance(payload.get("data"), Mapping)
        elif success is False:
            expected.add("error")
            error = payload.get("error")
            outcome_valid = (
                isinstance(error, Mapping)
                and set(error) == {"code", "message"}
                and isinstance(error.get("code"), str)
                and _BRIDGE_ERROR_CODE.fullmatch(error["code"]) is not None
                and isinstance(error.get("message"), str)
                and len(error["message"]) <= 512
            )
        else:
            outcome_valid = False
        if (
            set(payload) != expected
            or payload.get("kind") != _GUEST_RESPONSE_KIND
            or payload.get("protocol") != _GUEST_RESPONSE_PROTOCOL
            or payload.get("version") != 1
            or payload.get("operation") != operation
            or payload.get("request_id") != request_id
            or payload.get("domain_id") != domain_id
            or payload.get("binding_digests") != dict(binding_digests)
            or payload.get("guest_challenge") != guest_challenge
            or not outcome_valid
            or (
                attestation_nonce is not None
                and payload.get("attestation_nonce") != attestation_nonce
            )
        ):
            self._compromise("macOS VZ signed guest response binding mismatch")
            raise BackendUnavailableError("macOS VZ signed guest response binding mismatch")
        signature = _b64decode(payload.get("agent_signature"))
        if signature is None or len(signature) != 64:
            self._compromise("macOS VZ guest response signature is invalid")
            raise BackendUnavailableError("macOS VZ guest response signature is invalid")
        core = {key: value for key, value in payload.items() if key != "agent_signature"}
        try:
            encoded = canonical_json(core)
        except Exception as exc:
            self._compromise("macOS VZ guest response is not canonical")
            raise BackendUnavailableError("macOS VZ guest response is not canonical") from exc
        try:
            Ed25519PublicKey.from_public_bytes(public_key).verify(signature, encoded)
        except (InvalidSignature, ValueError) as exc:
            # InvalidSignature is deliberately not distinguished: a malicious
            # helper must not receive a signature oracle from this Host.
            self._compromise("macOS VZ guest response signature verification failed")
            raise BackendUnavailableError(
                "macOS VZ guest response signature verification failed"
            ) from exc
        if success is False:
            raise BackendUnavailableError("macOS VZ guest rejected the operation")
        return dict(payload["data"])

    def _compromise(self, reason: str) -> None:
        with self._lock:
            self._compromised_reason = reason

    def _new_transport(
        self, allocation: MacOSVZDomainAllocation
    ) -> MacOSVZSupervisorTransport:
        """Obtain the one live helper bound to this allocation exactly once."""

        try:
            if self._transport_factory is None:
                raise RuntimeError("factory unavailable")
            transport = self._transport_factory(allocation)
        except Exception as exc:
            raise BackendUnavailableError("macOS VZ native helper transport failed") from exc
        if (
            transport is None
            or not callable(getattr(transport, "enroll_launch_secret", None))
            or not callable(getattr(transport, "exchange", None))
            or not callable(getattr(transport, "close", None))
        ):
            raise BackendUnavailableError("macOS VZ native helper transport is invalid")
        with self._lock:
            transport_id = id(transport)
            if transport_id in self._transport_domains:
                self._compromise("macOS VZ helper transport was reused across domains")
                raise BackendUnavailableError(
                    "macOS VZ helper transport was reused across domains"
                )
            self._transport_domains[transport_id] = allocation.domain_id
        return transport

    def _release_unlaunched_allocation(
        self,
        allocation: MacOSVZDomainAllocation,
        transport: MacOSVZSupervisorTransport | None,
    ) -> None:
        """Best-effort cleanup of a COW/EFI allocation before a trusted launch."""

        try:
            if transport is not None:
                transport.close()
            if self._domain_allocator is not None:
                self._domain_allocator.release(allocation)
        except Exception:
            self._compromise("macOS VZ failed allocation cleanup")
        finally:
            if transport is not None:
                with self._lock:
                    self._transport_domains.pop(id(transport), None)


def verify_macos_vz_helper_identity(
    helper_path: Path,
    expected: MacOSVZHelperIdentity,
) -> tuple[bool, str | None]:
    """Verify helper bytes plus code-sign identity with macOS system tooling."""

    try:
        initial_identity, initial_digest = _secure_macho_code_digest(Path(helper_path))
    except OSError:
        return False, "macOS VZ native helper is missing"
    except ValueError:
        return False, "macOS VZ native helper path is unsafe"
    path = Path(helper_path)
    if initial_digest != expected.expected_code_digest:
        return False, "macOS VZ native helper code digest mismatch"
    if host_platform.system() != "Darwin":
        return False, "macOS codesign verification is unavailable"
    try:
        verified = subprocess.run(
            ["/usr/bin/codesign", "--verify", "--strict", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        described = subprocess.run(
            [
                "/usr/bin/codesign",
                "--display",
                "--verbose=4",
                "--entitlements",
                ":-",
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False, "macOS codesign verification failed"
    if verified.returncode != 0 or described.returncode != 0:
        return False, "macOS VZ native helper signature verification failed"
    try:
        final_identity, final_digest = _secure_macho_code_digest(path)
    except (OSError, ValueError):
        return False, "macOS VZ native helper changed during verification"
    if final_identity != initial_identity or final_digest != expected.expected_code_digest:
        return False, "macOS VZ native helper changed during verification"
    fields: dict[str, list[str]] = {}
    for line in (described.stdout + "\n" + described.stderr).splitlines():
        key, separator, value = line.partition("=")
        if separator:
            fields.setdefault(key.strip(), []).append(value.strip())
    if fields.get("Identifier") != [expected.bundle_id]:
        return False, "macOS VZ native helper signing identity mismatch"
    if expected.team_id and fields.get("TeamIdentifier") != [expected.team_id]:
        return False, "macOS VZ native helper signing identity mismatch"
    if expected.signing_identity and expected.signing_identity not in fields.get(
        "Authority", []
    ):
        return False, "macOS VZ native helper signing identity mismatch"
    entitlement_source = described.stdout + "\n" + described.stderr
    start = entitlement_source.find("<?xml")
    if start < 0:
        return False, "macOS VZ native helper virtualization entitlement is missing"
    try:
        entitlements = plistlib.loads(entitlement_source[start:].encode("utf-8"))
    except (ValueError, TypeError):
        return False, "macOS VZ native helper virtualization entitlement is invalid"
    if not isinstance(entitlements, Mapping) or entitlements.get(
        "com.apple.security.virtualization"
    ) is not True:
        return False, "macOS VZ native helper virtualization entitlement is missing"
    return True, None


def _request_identity(request: object) -> tuple[str, str, str]:
    domain_id = getattr(getattr(request, "target_domain", None), "value", None)
    request_id = getattr(getattr(request, "context", None), "request_id", None)
    request_digest = getattr(request, "request_digest", None)
    if not isinstance(domain_id, str) or not domain_id:
        raise BackendUnavailableError("macOS VZ provider domain identity is invalid")
    if not isinstance(request_id, str) or not request_id:
        raise BackendUnavailableError("macOS VZ provider request identity is invalid")
    if not isinstance(request_digest, str):
        raise BackendUnavailableError("macOS VZ provider request digest is invalid")
    try:
        require_digest(request_digest, "macOS VZ provider request")
    except Exception as exc:
        raise BackendUnavailableError("macOS VZ provider request digest is invalid") from exc
    return domain_id, request_id, request_digest


def _validated_invoke_outcome(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    if set(payload) != {"kind", "outcome"} or payload.get("kind") != "tobkiri.packvm.invoke.result.v1":
        raise BackendUnavailableError("macOS VZ invocation result is invalid")
    outcome = payload.get("outcome")
    if not isinstance(outcome, Mapping):
        raise BackendUnavailableError("macOS VZ invocation outcome is invalid")
    try:
        canonical_json(dict(outcome))
    except Exception as exc:
        raise BackendUnavailableError("macOS VZ invocation outcome is invalid") from exc
    return dict(outcome)


def _validate_bridge_request(value: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
        "kind",
        "protocol",
        "version",
        "target",
        "request",
        "request_digest",
        "continuation",
    }
    if set(value) != expected or value.get("protocol") != _BRIDGE_PROTOCOL or value.get("version") != 1:
        raise BackendUnavailableError("macOS VZ guest bridge request is invalid")
    target = value.get("target")
    request = value.get("request")
    continuation = value.get("continuation")
    request_digest = value.get("request_digest")
    if (
        value.get("kind") != "tobkiri.packvm.bridge.request.v1"
        or not _valid_bridge_target(target)
        or not _valid_bridge_payload(request)
        or not isinstance(request_digest, str)
        or not isinstance(continuation, Mapping)
    ):
        raise BackendUnavailableError("macOS VZ guest bridge request is invalid")
    # The two structural validators above establish these mappings at runtime;
    # retain the local checks so static callers cannot weaken this boundary.
    if not isinstance(request, Mapping) or not isinstance(target, Mapping):
        raise BackendUnavailableError("macOS VZ guest bridge request is invalid")
    try:
        require_digest(request_digest, "macOS VZ bridge request")
    except Exception as exc:
        raise BackendUnavailableError("macOS VZ guest bridge request is invalid") from exc
    if request_digest != canonical_digest(dict(request)):
        raise BackendUnavailableError("macOS VZ guest bridge request digest mismatch")
    expected_continuation = {
        "kind": "tobkiri.packvm.continuation.v1",
        "protocol": _BRIDGE_PROTOCOL,
        "version": 1,
        "operation_id": "complete",
        "nonce": continuation.get("nonce"),
        "target": dict(target),
        "request_digest": request_digest,
    }
    if (
        set(continuation) != set(expected_continuation)
        or continuation != expected_continuation
        or not isinstance(continuation.get("nonce"), str)
        or _GUEST_NONCE.fullmatch(continuation["nonce"]) is None
    ):
        raise BackendUnavailableError("macOS VZ guest bridge continuation is invalid")
    return dict(continuation)


def _validate_host_bridge_request(
    value: Mapping[str, Any],
    *,
    request_id: str,
    domain_id: str,
    guest_artifact_identity: str,
    request_digest: str,
    deadline_monotonic: str | None,
) -> dict[str, Any]:
    """Validate the runner's private-FD request before entering Host dispatch."""

    expected = {
        "kind",
        "protocol",
        "version",
        "request_id",
        "target_domain",
        "guest_artifact_identity",
        "request_digest",
        "bridge_request_digest",
        "bridge_request",
        "deadline_monotonic",
    }
    if (
        set(value) != expected
        or value.get("kind") != "tobkiri.packvm.bridge.host-request.v1"
        or value.get("protocol") != _BRIDGE_PROTOCOL
        or value.get("version") != 1
        or value.get("request_id") != request_id
        or value.get("target_domain") != domain_id
        or value.get("guest_artifact_identity") != guest_artifact_identity
        or value.get("request_digest") != request_digest
        or value.get("deadline_monotonic") != deadline_monotonic
        or not isinstance(value.get("bridge_request"), Mapping)
        or not isinstance(value.get("bridge_request_digest"), str)
    ):
        raise BackendUnavailableError("macOS VZ guest Host bridge frame is invalid")
    bridge_request = dict(value["bridge_request"])
    if value["bridge_request_digest"] != canonical_digest(bridge_request):
        raise BackendUnavailableError("macOS VZ guest Host bridge digest mismatch")
    return bridge_request


def _validate_bridge_result(
    value: Mapping[str, Any],
    continuation: Mapping[str, Any],
) -> None:
    expected = {
        "kind",
        "protocol",
        "version",
        "operation_id",
        "nonce",
        "target",
        "request_digest",
        "result",
        "result_digest",
    }
    if set(value) != expected or value.get("kind") != "tobkiri.packvm.bridge.result.v1":
        raise BackendUnavailableError("macOS VZ Host bridge result is invalid")
    if (
        value.get("protocol") != _BRIDGE_PROTOCOL
        or value.get("version") != 1
        or value.get("operation_id") != "complete"
        or value.get("nonce") != continuation.get("nonce")
        or value.get("target") != continuation.get("target")
        or value.get("request_digest") != continuation.get("request_digest")
    ):
        raise BackendUnavailableError("macOS VZ Host bridge result binding mismatch")
    result = value.get("result")
    if not isinstance(result, Mapping) or not _valid_bridge_result(result):
        raise BackendUnavailableError("macOS VZ Host bridge result is invalid")
    digest = value.get("result_digest")
    if not isinstance(digest, str) or digest != canonical_digest(dict(result)):
        raise BackendUnavailableError("macOS VZ Host bridge result digest mismatch")


def _valid_bridge_target(value: object) -> bool:
    if not isinstance(value, Mapping) or set(value) != {"contract_id", "operation_id"}:
        return False
    return dict(value) == _CONVERSATION_BRIDGE_TARGET


def _valid_bridge_payload(value: object) -> bool:
    if not isinstance(value, Mapping) or set(value) != {"messages", "requirements"}:
        return False
    messages = value.get("messages")
    requirements = value.get("requirements")
    if not isinstance(messages, list) or not messages or len(messages) > 128:
        return False
    if requirements != {"request_surface": "defaultspack.conversation"}:
        return False
    try:
        canonical_json(dict(value))
    except Exception:
        return False
    return True


def _valid_bridge_result(value: Mapping[str, Any]) -> bool:
    if value.get("status") == "ok":
        return set(value) == {"status", "value"} and isinstance(value.get("value"), Mapping)
    if value.get("status") == "error":
        error = value.get("error")
        return (
            set(value) == {"status", "error"}
            and isinstance(error, Mapping)
            and set(error) == {"code", "message"}
            and isinstance(error.get("code"), str)
            and _BRIDGE_ERROR_CODE.fullmatch(error["code"]) is not None
            and isinstance(error.get("message"), str)
            and len(error["message"]) <= 512
        )
    return False


def _bounded_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise BackendUnavailableError(f"macOS VZ provider {label} is invalid")
    return value


def _deadline_value(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BackendUnavailableError("macOS VZ provider deadline is invalid")
    if isinstance(value, float) and not math.isfinite(value):
        raise BackendUnavailableError("macOS VZ provider deadline is invalid")
    return format(value, ".17g")


def _launch_deadline_ns(value: object) -> int:
    """Return a canonical integral monotonic deadline in nanoseconds.

    Canonical Host JSON rejects floating point values.  Ceil conversion avoids
    truncating the valid Host lease; the Host remains the sole lease authority
    and the helper binds this exact integer as a launch fact.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BackendUnavailableError("macOS VZ launch lease expiry is invalid")
    if isinstance(value, float) and not math.isfinite(value):
        raise BackendUnavailableError("macOS VZ launch lease expiry is invalid")
    if value <= 0:
        raise BackendUnavailableError("macOS VZ launch lease expiry is invalid")
    bounded = value * 1_000_000_000
    if isinstance(bounded, float):
        try:
            bounded = math.ceil(bounded)
        except OverflowError as exc:
            raise BackendUnavailableError(
                "macOS VZ launch lease expiry is invalid"
            ) from exc
    if not isinstance(bounded, int) or bounded <= 0 or bounded > 2**63 - 1:
        raise BackendUnavailableError("macOS VZ launch lease expiry is invalid")
    return bounded


def _secure_file_digest(path: Path) -> tuple[tuple[int, int], str]:
    """Hash one regular, no-follow file descriptor and detect a path swap."""

    initial = path.lstat()
    if stat.S_ISLNK(initial.st_mode) or not stat.S_ISREG(initial.st_mode):
        raise ValueError("unsafe helper path")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        identity = (opened.st_dev, opened.st_ino)
        if (
            not stat.S_ISREG(opened.st_mode)
            or identity != (initial.st_dev, initial.st_ino)
        ):
            raise ValueError("helper identity changed before open")
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    final = path.lstat()
    if (final.st_dev, final.st_ino) != identity:
        raise ValueError("helper identity changed while hashing")
    return identity, "sha256:" + digest.hexdigest()


def _secure_macho_code_digest(path: Path) -> tuple[tuple[int, int], str]:
    """Hash the package-pinned Mach-O code region through a no-follow FD.

    The release attestation normalizes only the offsets changed by the final
    code-signature SuperBlob.  Keeping the same algorithm here makes a helper
    identity stable across signing without mistaking a whole-file SHA-256 for
    build provenance.
    """

    initial = path.lstat()
    if stat.S_ISLNK(initial.st_mode) or not stat.S_ISREG(initial.st_mode):
        raise ValueError("unsafe helper path")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        identity = (opened.st_dev, opened.st_ino)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or identity != (initial.st_dev, initial.st_ino)
            or opened.st_size > 128 * 1024 * 1024
        ):
            raise ValueError("unsafe helper identity")
        chunks: list[bytes] = []
        while block := os.read(descriptor, 1024 * 1024):
            chunks.append(block)
        final_opened = os.fstat(descriptor)
        if (
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        ) != (
            final_opened.st_size,
            final_opened.st_mtime_ns,
            final_opened.st_ctime_ns,
        ):
            raise ValueError("helper changed while hashing")
    finally:
        os.close(descriptor)
    final = path.lstat()
    if (final.st_dev, final.st_ino) != identity:
        raise ValueError("helper identity changed while hashing")
    data = bytearray(b"".join(chunks))
    if len(data) < 32 or data[:4] != b"\xcf\xfa\xed\xfe":
        raise ValueError("helper is not a thin 64-bit Mach-O")
    command_count, command_bytes = struct.unpack_from("<II", data, 16)
    command_offset = 32
    command_end = command_offset + command_bytes
    if command_end > len(data):
        raise ValueError("Mach-O load commands exceed helper")
    signature: tuple[int, int, int] | None = None
    linkedit_command: int | None = None
    for _ in range(command_count):
        if command_offset + 8 > command_end:
            raise ValueError("Mach-O load command is truncated")
        command, command_size = struct.unpack_from("<II", data, command_offset)
        if command_size < 8 or command_offset + command_size > command_end:
            raise ValueError("Mach-O load command is invalid")
        if command == 0x1D:
            if command_size != 16 or signature is not None:
                raise ValueError("Mach-O code signature command is invalid")
            data_offset, data_size = struct.unpack_from("<II", data, command_offset + 8)
            signature = (command_offset, data_offset, data_size)
        elif (
            command == 0x19
            and data[command_offset + 8 : command_offset + 24].rstrip(b"\0")
            == b"__LINKEDIT"
        ):
            if command_size < 72 or linkedit_command is not None:
                raise ValueError("Mach-O __LINKEDIT command is invalid")
            linkedit_command = command_offset
        command_offset += command_size
    if command_offset != command_end or signature is None or linkedit_command is None:
        raise ValueError("Mach-O code-signature command is missing")
    signature_command, data_offset, data_size = signature
    if data_offset < command_end or data_offset + data_size != len(data):
        raise ValueError("Mach-O code-signature blob is invalid")
    data[signature_command + 8 : signature_command + 16] = b"\0" * 8
    data[linkedit_command + 32 : linkedit_command + 40] = b"\0" * 8
    data[linkedit_command + 48 : linkedit_command + 56] = b"\0" * 8
    return identity, "sha256:" + hashlib.sha256(data[:data_offset]).hexdigest()


def _text_digest(value: str) -> str:
    """Return the exact raw UTF-8 digest used by the native launch helper."""

    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _b64encode(value: bytes) -> str:
    """Encode fixed binary launch facts for ``Data(base64Encoded:)``."""

    return base64.b64encode(value).decode("ascii")


def _b64decode(value: object) -> bytes | None:
    if not isinstance(value, str) or not value or len(value) > 4096:
        return None
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, UnicodeEncodeError):
        return None


__all__ = [
    "CapabilityBridge",
    "MacOSVZAgentIdentity",
    "MacOSVZDomainAllocation",
    "MacOSVZDomainAllocator",
    "MacOSVZHelperIdentity",
    "MacOSVZHelperIdentityVerifier",
    "MacOSVZLaunchAssets",
    "MacOSVZRuntime",
    "MacOSVZSupervisorDriver",
    "MacOSVZSupervisorTransport",
    "MacOSVZTransportFactory",
    "verify_macos_vz_helper_identity",
]
