"""Fail-closed lifecycle provisioning for the direct macOS VZ PackVM.

This module owns the *machine* ceremony, rather than an operation dispatch
path.  It intentionally does not expose Lima as a compatibility fallback:
the production implementation needs a signed native helper, fixed boot
assets, and an authenticated guest-supervisor transport before it can become
ready.  The legacy Lima provisioner remains useful only when explicitly
injected by development and conformance tests.

The image download is delegated to :class:`PackVMImageCache`.  That cache
already provides descriptor-pinned, resumable downloads with no redirects,
exact digest verification, and atomic publication.  This provisioner adds the
macOS-specific boundaries: signed helper/assets verification, a private
per-instance VZ run root, host-only agent key material, and residual-zero
cleanup.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import platform as host_platform
import secrets
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Iterator, Mapping, Protocol, TypedDict

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from core_runtime.hmac_key_manager import generate_or_load_signing_key
from ecosystem.defaultspack.backend.sandbox.isolation.lima_runtime import (
    PACKVM_BACKEND_ID,
    PACKVM_CLEANUP_PREFIX,
    PACKVM_CONFIRMATION_PREFIX,
    PACKVM_STOP_PREFIX,
    PackVMDoctor,
    PackVMProvisioningPlan,
    PackVMProvisioningRequest,
)
from ecosystem.defaultspack.backend.sandbox.isolation.packvm_image_cache import (
    PackVMImageAuthority,
    PackVMImageCache,
    PackVMImageCancelled,
    PackVMPinnedImage,
)

if TYPE_CHECKING:
    from tobkiri_host.artifact_materialization import MaterializedPackArtifact
    from tobkiri_host.macos_vz_supervisor import (
        MacOSVZAgentIdentity,
        MacOSVZDomainAllocation,
        MacOSVZDomainAllocator,
        MacOSVZHelperIdentity,
        MacOSVZLaunchAssets,
        MacOSVZSupervisorTransport,
    )


VZ_ASSET_MANIFEST_SCHEMA = "io.tobkiri.packvm-vz-provisioning.v1"
VZ_BUNDLE_MANIFEST_SCHEMA = "io.tobkiri.packvm-vz-bundle-manifest.v1"
VZ_STATE_VERSION = 1
VZ_INSTANCE = "tobkiri-packvm-v4"
VZ_PLATFORM = "macos-arm64"
VZ_RAW_EFI_IMAGE_DECLARED_BYTES = 3 * 1024 * 1024 * 1024
# ``clonefile`` creates a same-sized raw COW image and no Direct VZ path
# resizes it. Reserve its exact, pinned raw size rather than an invented
# larger sparse-disk ceiling; a missing cache is charged separately below.
VZ_HOST_STORAGE_RESERVE_BYTES = 512 * 1024 * 1024
_MAX_STATE_BYTES = 128 * 1024
_MAX_MANIFEST_BYTES = 128 * 1024
_MAX_HELPER_PROTOCOL_BYTES = 1024 * 1024
_MAX_ARTIFACT_SEED_MANIFEST_BYTES = 16 * 1024 * 1024
_MAX_ARTIFACT_SEED_PAYLOAD_BYTES = 512 * 1024 * 1024
_ARTIFACT_SEED_MAGIC = b"tobkiri-packvm-artifact-seed.v1\0"
_ARTIFACT_SEED_FORMAT = "io.tobkiri.packvm-artifact-seed.v1"
_MAX_ARTIFACT_SEED_BYTES = (
    len(_ARTIFACT_SEED_MAGIC)
    + 8
    + _MAX_ARTIFACT_SEED_MANIFEST_BYTES
    + _MAX_ARTIFACT_SEED_PAYLOAD_BYTES
)
# Allocation briefly owns both the private artifact seed and the CIDATA ISO
# into which that seed is copied.  Charge both maximum-size copies during the
# user-visible preflight; the ordinary Host reserve remains available for the
# EFI store, the agent seed, ISO metadata/alignment, and COW writes.
VZ_ARTIFACT_SEED_PEAK_RESERVE_BYTES = 2 * _MAX_ARTIFACT_SEED_BYTES
# The guest image starts cloud-final after network-online.target.  A VZ
# domain deliberately has no physical NIC, which makes an empty NoCloud
# configuration wait for the image's networkd-wait-online timeout.  This
# local-only dummy link is rendered from CIDATA before that dependency: it
# has one /32 address but no route, gateway, DNS, or Host attachment.
_NOCLOUD_LOCAL_ONLY_NETWORK_CONFIG = (
    b"version: 2\n"
    b"renderer: networkd\n"
    b"dummy-devices:\n"
    b"  tobkiri0:\n"
    b"    addresses: [192.0.2.1/32]\n"
)
_DIGEST_PREFIX = "sha256:"
_DIRECT_IMAGE_URL = (
    "https://gemmei.ftp.acc.umu.se/images/cloud/trixie/20260819-2575/"
    "debian-13-generic-arm64-20260819-2575.raw"
)
_DIRECT_IMAGE_SHA256 = "sha256:9440bc19285b9e0ccb217fd5ac818a253a3c0bfd46c9ac83241959c78f90ad71"
_DIRECT_IMAGE_SHA512 = (
    "f21843e29eade9747b1b7bb7d9622c30613eb3d875fbb6a7f9bd76acaadfdbfe"
    "0ef68137da4eb7520e440a6cd3bbb248db41aa322f58d11e71fea667eb569a2c"
)


class MacOSVZTransportFactory(Protocol):
    """Build an already authenticated Host adapter for one provisioned VM.

    The adapter may wrap the native JSONL helper protocol, but it must return
    raw guest MAC and Ed25519 evidence for the Python supervisor.  A helper
    status bit, or an adapter that fabricates those values, is never enough.
    """

    def __call__(self, allocation: MacOSVZDomainAllocation) -> MacOSVZSupervisorTransport | None:
        """Return the one live helper transport for an exact allocation."""


class _ImageDescriptorFacts(TypedDict):
    """Narrowed values extracted from the immutable image descriptor."""

    source: str
    size_bytes: int
    sha256: str
    sha512: str


class _ArtifactSeedFile(TypedDict):
    """One typed manifest entry for a materialized artifact-seed file."""

    path: str
    digest: str
    executable: bool
    size: int


@dataclass(frozen=True)
class _AuthenticatedPackVMBundleBinding:
    """Launcher-attested immutable resource identities for a packaged app.

    This is deliberately a private, normalized copy of the sealed launch
    binding.  The runtime-facing object is supplied by
    ``core_runtime.packaged_application_bundle``; retaining only its exact
    values here prevents a mutable resource manifest from becoming the source
    of helper identity or Team-ID expectations.
    """

    root: Path
    provisioning_sha256: str
    helper_manifest_sha256: str
    helper_team_id: str


@dataclass(frozen=True)
class MacOSVZAssetManifest:
    """Verified immutable build facts for the signed VZ helper and boot set."""

    helper_path: Path
    helper_digest: str
    helper_bundle_id: str
    helper_team_id: str
    helper_signing_identity: str
    agent_path: Path
    agent_digest: str
    guest_service_path: Path
    guest_service_digest: str
    bubblewrap_path: Path
    bubblewrap_digest: str
    bubblewrap_descriptor_path: Path
    bubblewrap_descriptor_digest: str
    config_path: Path
    config_digest: str
    image_source: str
    image_digest: str
    image_sha512: str | None
    image_size_bytes: int
    architecture: str
    manifest_digest: str


@dataclass(frozen=True)
class MacOSVZProvisionedFacts:
    """Immutable facts consumed by ``MacOSVZSupervisorDriver``.

    ``transport_or_factory`` deliberately returns ``None`` when the signed
    helper does not yet implement the full Host/guest authentication protocol.
    Callers must treat that as unavailable rather than falling back.
    """

    helper_path: Path
    helper_identity: MacOSVZHelperIdentity
    launch_assets: MacOSVZLaunchAssets
    agent_identity: MacOSVZAgentIdentity
    domain_allocator: MacOSVZDomainAllocator
    instance_root: Path
    transport_factory: MacOSVZTransportFactory | None
    protocol_ready: bool
    reason: str | None

    def transport_or_factory(self) -> MacOSVZTransportFactory | None:
        """Return an allocation-scoped transport factory without starting it."""

        if not self.protocol_ready or self.transport_factory is None:
            return None
        return self.transport_factory


class _MacOSVZHelperProcess:
    """One allocation-owned signed-helper subprocess and its private FD key.

    The helper reads the key exactly once from an inherited pipe, immediately
    closes that descriptor, and never receives it in JSON, an environment
    variable, or a Pack child.  This object deliberately does not fabricate a
    guest response: it only forwards the signed helper's bounded JSONL output
    for the Host supervisor to verify.
    """

    def __init__(self, helper_path: Path, run_root: Path, channel_key: bytes) -> None:
        if not isinstance(channel_key, bytes) or len(channel_key) != 32:
            raise ValueError("PackVM VZ helper channel key is invalid")
        if not helper_path.is_absolute() or not _safe_relative_file(
            helper_path.parent, helper_path
        ):
            raise ValueError("PackVM VZ helper path is unsafe")
        read_fd, write_fd = os.pipe()
        process: subprocess.Popen[bytes] | None = None
        try:
            os.set_inheritable(read_fd, True)
            process = subprocess.Popen(
                [str(helper_path), "--agent-key-fd", str(read_fd)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                pass_fds=(read_fd,),
                cwd=str(run_root),
                start_new_session=True,
            )
            os.close(read_fd)
            read_fd = -1
            _write_all(write_fd, channel_key)
        except Exception:
            if process is not None:
                process.kill()
                process.wait(timeout=5)
            raise
        finally:
            if read_fd >= 0:
                os.close(read_fd)
            os.close(write_fd)
        if process is None or process.stdin is None or process.stdout is None:
            raise ValueError("PackVM VZ helper pipes are unavailable")
        self._process = process
        self._key = bytearray(channel_key)
        self._lock = threading.RLock()
        self._closed = False
        self._domain_id: str | None = None
        self._launch_binding_digest: str | None = None

    def prepare_efi_store(
        self, *, domain_id: str, run_root: Path, efi_path: Path
    ) -> Mapping[str, object]:
        """Ask this exact helper process to create its future EFI store."""

        return self._legacy_request(
            "prepare_efi_store",
            {
                "domain_id": domain_id,
                "run_root": str(run_root),
                "efi_variable_store_path": str(efi_path),
            },
        )

    def enroll_launch_secret(
        self,
        *,
        domain_id: str,
        host_nonce: str,
        launch_binding_digest: str,
        secret: bytes,
    ) -> None:
        """Bind the FD-delivered key to one launch; do not serialize it."""

        if (
            not isinstance(secret, bytes)
            or not hmac.compare_digest(secret, bytes(self._key))
            or not isinstance(domain_id, str)
            or not domain_id
            or not isinstance(host_nonce, str)
            or len(host_nonce) != 64
            or not _is_digest(launch_binding_digest)
        ):
            raise ValueError("PackVM VZ helper channel enrollment is invalid")
        with self._lock:
            if self._closed or self._process.poll() is not None:
                raise ValueError("PackVM VZ helper process is unavailable")
            if self._domain_id is not None:
                raise ValueError("PackVM VZ helper channel is already enrolled")
            self._domain_id = domain_id
            self._launch_binding_digest = launch_binding_digest

    def exchange(self, envelope: Mapping[str, Any]) -> Mapping[str, Any]:
        """Forward one direct envelope without adding trust or credentials."""

        with self._lock:
            if self._closed or self._process.poll() is not None:
                raise ValueError("PackVM VZ helper process is unavailable")
            if (
                self._domain_id is None
                or envelope.get("domain_id") != self._domain_id
                or envelope.get("launch_binding_digest") != self._launch_binding_digest
            ):
                raise ValueError("PackVM VZ helper transport binding is invalid")
            return self._exchange_line(dict(envelope))

    def close(self) -> None:
        """Close the sole helper process for this allocation without shelling out."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                if self._process.stdin is not None:
                    self._process.stdin.close()
                self._process.wait(timeout=10)
            except (OSError, subprocess.SubprocessError):
                self._process.kill()
                try:
                    self._process.wait(timeout=5)
                except (OSError, subprocess.SubprocessError):
                    pass
            self._key[:] = b"\0" * len(self._key)

    def _legacy_request(self, operation: str, extra: Mapping[str, object]) -> Mapping[str, object]:
        """Use the helper's prelaunch HMAC envelope on the retained process."""

        request_id = "efi-" + secrets.token_hex(16)
        nonce = secrets.token_hex(32)
        request: dict[str, object] = {
            "protocol": "io.tobkiri.packvm-supervisor.v1",
            "request_id": request_id,
            "operation": operation,
            "nonce": nonce,
            **extra,
        }
        request["request_hmac"] = hmac.new(
            self._key, _canonical_bytes(request), hashlib.sha256
        ).hexdigest()
        response = self._exchange_line(request)
        received = response.pop("response_hmac", None)
        if not isinstance(received, str) or not hmac.compare_digest(
            received,
            hmac.new(self._key, _canonical_bytes(response), hashlib.sha256).hexdigest(),
        ):
            raise ValueError("PackVM VZ helper prelaunch response authentication failed")
        data = response.get("data")
        if (
            response.get("protocol") != "io.tobkiri.packvm-supervisor.v1"
            or response.get("request_id") != request_id
            or response.get("operation") != operation
            or response.get("nonce") != nonce
            or response.get("success") is not True
            or response.get("error") is not None
            or not isinstance(data, Mapping)
        ):
            raise ValueError("PackVM VZ helper prelaunch response is invalid")
        return dict(data)

    def _exchange_line(self, payload: Mapping[str, object]) -> dict[str, object]:
        if self._process.stdin is None or self._process.stdout is None:
            raise ValueError("PackVM VZ helper pipes are unavailable")
        encoded = _canonical_bytes(payload)
        if len(encoded) > _MAX_HELPER_PROTOCOL_BYTES:
            raise ValueError("PackVM VZ helper request exceeds its bound")
        self._process.stdin.write(encoded + b"\n")
        self._process.stdin.flush()
        response_line = self._process.stdout.readline(_MAX_HELPER_PROTOCOL_BYTES + 1)
        if not response_line or len(response_line) > _MAX_HELPER_PROTOCOL_BYTES:
            raise ValueError("PackVM VZ helper response exceeds its bound")
        try:
            response = json.loads(response_line)
        except json.JSONDecodeError as exc:
            raise ValueError("PackVM VZ helper response is invalid") from exc
        if not isinstance(response, dict):
            raise ValueError("PackVM VZ helper response is invalid")
        return response


class MacOSVZProvisioner:
    """Provision direct VZ resources without a Lima production path.

    Tests inject the asset manifest and image cache.  Production discovers
    only files inside the signed application bundle; ambient environment
    variables and user-selected helper paths are intentionally not accepted.
    """

    def __init__(
        self,
        *,
        state_dir: Path | None = None,
        bundle_root: Path | None = None,
        asset_manifest_path: Path | None = None,
        bundle_binding: object | None = None,
        image_cache: PackVMImageCache | None = None,
        disk_usage: Callable[[Path], Any] | None = None,
        platform_system: str | None = None,
        machine: str | None = None,
        transport_factory: MacOSVZTransportFactory | None = None,
        helper_identity_verifier: Callable[[MacOSVZAssetManifest], tuple[bool, str | None]]
        | None = None,
        allow_ad_hoc_helper_identity: bool = False,
        clone_file: Callable[[Path, Path], None] | None = None,
        efi_store_preparer: Callable[[Path, str, Path, bytes], Mapping[str, object]] | None = None,
    ) -> None:
        requested_state_dir = state_dir or _default_state_dir()
        self._requested_state_dir = Path(requested_state_dir)
        self._state_dir = self._requested_state_dir.resolve()
        self._bundle_root: Path | None
        self._asset_manifest_path: Path | None
        self._bundle_binding_error: str | None = None
        explicit_bundle_inputs = (
            bundle_root is not None or asset_manifest_path is not None or bundle_binding is not None
        )
        binding_value = bundle_binding
        if not explicit_bundle_inputs:
            try:
                binding_value = _packaged_packvm_bundle_binding()
            except Exception:
                self._bundle_binding_error = "packaged macOS VZ bundle binding is unavailable"
        try:
            self._bundle_binding = (
                None if binding_value is None else _normalise_packvm_bundle_binding(binding_value)
            )
        except ValueError:
            self._bundle_binding = None
            self._bundle_binding_error = "packaged macOS VZ bundle binding is invalid"
        if not explicit_bundle_inputs and self._bundle_binding is None:
            self._bundle_binding_error = (
                self._bundle_binding_error or "packaged macOS VZ bundle binding is unavailable"
            )
        if self._bundle_binding is not None:
            bound_root = self._bundle_binding.root
            bound_manifest = _default_assets_manifest_path(bound_root)
            if bound_manifest is None:
                self._bundle_binding_error = "packaged macOS VZ bundle binding is invalid"
            elif (
                asset_manifest_path is not None
                and Path(asset_manifest_path).resolve() != bound_manifest
            ):
                self._bundle_binding_error = (
                    "packaged macOS VZ bundle binding does not match manifest path"
                )
            elif bundle_root is not None and Path(bundle_root).resolve() != bound_root:
                self._bundle_binding_error = (
                    "packaged macOS VZ bundle binding does not match bundle root"
                )
            self._bundle_root = bound_root
            self._asset_manifest_path = bound_manifest
        else:
            self._bundle_root = (
                Path(bundle_root).resolve() if bundle_root is not None else _discover_bundle_root()
            )
            self._asset_manifest_path = (
                Path(asset_manifest_path).resolve()
                if asset_manifest_path is not None
                else (
                    None
                    if not explicit_bundle_inputs
                    else _default_assets_manifest_path(self._bundle_root)
                )
            )
        self._platform_system = (platform_system or host_platform.system()).casefold()
        self._machine = _normalise_machine(machine or host_platform.machine())
        self._disk_usage = disk_usage or shutil.disk_usage
        self._image_cache = image_cache or PackVMImageCache(
            self._state_dir / "image-cache", disk_usage=self._disk_usage
        )
        self._transport_factory = transport_factory
        self._helper_identity_verifier = helper_identity_verifier
        self._allow_ad_hoc_helper_identity = allow_ad_hoc_helper_identity
        self._clone_file = clone_file or _clone_file_apfs
        self._efi_store_preparer = efi_store_preparer
        self._pending: dict[str, PackVMProvisioningPlan] = {}
        self._allocation_transports: dict[str, _MacOSVZHelperProcess] = {}
        self._claimed_transport_roots: set[str] = set()
        self._lock = threading.RLock()

    @property
    def state_path(self) -> Path:
        """Return the sole authenticated state record for this provisioner."""

        return self._state_dir / "packvm-vz-attestation.json"

    @property
    def recovery_path(self) -> Path:
        """Return host-authenticated evidence left during a failed provision."""

        return self._state_dir / "packvm-vz-recovery.json"

    @property
    def mutation_lock_path(self) -> Path:
        """Return the cross-process lifecycle lock path."""

        return self._state_dir / "packvm-vz-mutation.lock"

    @property
    def mutation_claim_path(self) -> Path:
        """Return the durable owner claim path for a lifecycle mutation."""

        return self._state_dir / "packvm-vz-mutation-claim.json"

    @property
    def audit_path(self) -> Path:
        """Return the bounded append-only local lifecycle audit path."""

        return self._state_dir / "packvm-vz-audit.jsonl"

    @property
    def image_cache(self) -> PackVMImageCache:
        """Return the independently verified base-image cache."""

        return self._image_cache

    @contextmanager
    def operation_gate(
        self,
        operation: str,
        binding: Mapping[str, str | int],
        *,
        recover_claim: bool = False,
        preserve_claim_on_error: bool = False,
        retain_claim_on_success: bool = False,
    ) -> Iterator[None]:
        """Serialize a mutation and retain an exact owner claim durably."""

        self._ensure_state_root()
        claim = {
            "version": 1,
            "operation": operation,
            "instance": VZ_INSTANCE,
            "owner_pid": os.getpid(),
            "binding": dict(binding),
        }
        descriptor = _open_private_file(self.mutation_lock_path, os.O_CREAT | os.O_RDWR)
        locked = False
        succeeded = False
        try:
            _try_lock(descriptor)
            locked = True
            existing = _read_json_if_present(self.mutation_claim_path)
            if existing is not None:
                same_owner = hmac.compare_digest(
                    _canonical_bytes(existing), _canonical_bytes(claim)
                )
                stale_recovery = (
                    recover_claim
                    and _claim_binding_equal(existing, claim)
                    and not _process_is_alive(existing.get("owner_pid"))
                )
                if not same_owner and not stale_recovery:
                    raise ValueError("PackVM VZ mutation has an unresolved owner claim")
                if stale_recovery:
                    _atomic_private_json(self.mutation_claim_path, claim)
            else:
                _atomic_private_json(self.mutation_claim_path, claim)
            yield
            succeeded = True
        finally:
            if locked:
                should_remove = (succeeded and not retain_claim_on_success) or (
                    not succeeded and not preserve_claim_on_error
                )
                if should_remove:
                    current = _read_json_if_present(self.mutation_claim_path)
                    if current is not None and _canonical_bytes(current) == _canonical_bytes(claim):
                        self.mutation_claim_path.unlink(missing_ok=True)
                _unlock(descriptor)
            os.close(descriptor)

    def recovery_identity(self) -> dict[str, int | str]:
        """Return non-secret stable state-root identity for operation recovery."""

        self._ensure_state_root()
        metadata = self._state_dir.lstat()
        return {
            "vz_state_root_digest": _digest_text(str(self._state_dir)),
            "vz_state_root_device": int(metadata.st_dev),
            "vz_state_root_inode": int(metadata.st_ino),
            "vz_provisioner_digest": _file_digest(Path(__file__)),
        }

    def prepare(self) -> PackVMProvisioningPlan:
        """Display exact VZ image and helper facts before any mutation occurs."""

        with self._lock:
            manifest, issue = self._load_manifest_for_plan()
            nonce = secrets.token_hex(16)
            if manifest is None:
                image_source = "unavailable"
                image_digest = _zero_digest()
                image_size = VZ_RAW_EFI_IMAGE_DECLARED_BYTES
                cache_status, cache_reason = "unsafe", issue
                config_digest = _zero_digest()
                guest_digest = _zero_digest()
                helper_digest = _zero_digest()
            else:
                image_source = manifest.image_source
                image_digest = manifest.image_digest
                image_size = manifest.image_size_bytes
                cache_status, cache_reason = self._image_cache.status(
                    self._image_authority(manifest, _zero_digest(), _zero_digest(), "prepare")
                )
                config_digest = manifest.config_digest
                guest_digest = manifest.agent_digest
                helper_digest = manifest.helper_digest
            image_download_required = manifest is not None and cache_status != "verified_source"
            download_bytes = image_size if image_download_required else 0
            required_space = self._required_host_space(download_bytes)
            available, storage_reason = self._host_capacity(required_space)
            launcher_reason = issue or storage_reason
            runtime_status = "ready" if launcher_reason is None else "unsafe"
            facts = {
                "backend_id": PACKVM_BACKEND_ID,
                "instance": VZ_INSTANCE,
                "platform": VZ_PLATFORM,
                "architecture": self._machine,
                "image_source": image_source,
                "image_digest": image_digest,
                "image_size_bytes": image_size,
                "image_download_required": image_download_required,
                "image_download_bytes": download_bytes,
                "image_cache_status": cache_status,
                "disk_size_bytes": VZ_RAW_EFI_IMAGE_DECLARED_BYTES,
                "host_free_space_required_bytes": required_space,
                "config_digest": config_digest,
                "guest_runner_digest": guest_digest,
                "host_build_digest": helper_digest,
                "runtime_root_digest": _digest_text(str(self._state_dir)),
                "runtime_path_status": runtime_status,
                "ceremony_nonce": nonce,
            }
            plan_digest = _canonical_digest(facts)
            self._pending.clear()
            plan = PackVMProvisioningPlan(
                backend_id=PACKVM_BACKEND_ID,
                instance=VZ_INSTANCE,
                limactl=None,
                launcher_reason=launcher_reason,
                architecture=self._machine,
                image_source=image_source,
                image_digest=image_digest,
                image_size_bytes=image_size,
                image_download_required=image_download_required,
                image_download_bytes=download_bytes,
                image_cache_status=cache_status,
                image_cache_reason=cache_reason,
                disk_size_bytes=VZ_RAW_EFI_IMAGE_DECLARED_BYTES,
                host_free_space_required_bytes=required_space,
                host_free_space_available_bytes=available,
                host_free_space_reason=storage_reason,
                config_digest=config_digest,
                guest_runner_digest=guest_digest,
                host_build_digest=helper_digest,
                runtime_root_digest=_digest_text(str(self._state_dir)),
                runtime_path_status=runtime_status,
                runtime_path_reason=launcher_reason,
                ceremony_nonce=nonce,
                plan_digest=plan_digest,
                confirmation=f"{PACKVM_CONFIRMATION_PREFIX} {VZ_INSTANCE} {plan_digest[7:19]}",
            )
            self._pending[nonce] = plan
            return plan

    def provision(
        self,
        request: PackVMProvisioningRequest,
        *,
        progress: Callable[[Any], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> PackVMDoctor:
        """Fetch the exact raw EFI image and construct private VZ state once."""

        with self._lock:
            plan = self._pending.pop(request.ceremony_nonce, None)
        if plan is None or not hmac.compare_digest(plan.plan_digest, request.plan_digest):
            raise ValueError("PackVM VZ provisioning ceremony is invalid or already consumed")
        if not hmac.compare_digest(plan.confirmation, request.confirmation):
            raise ValueError("PackVM VZ provisioning plan changed; review it again")
        if plan.launcher_reason is not None:
            raise ValueError(plan.launcher_reason)
        if plan.image_download_required and not request.approve_image_download:
            raise ValueError(
                "PackVM image download requires explicit approval for the displayed source, size, and digest"
            )
        manifest = self._require_manifest()
        self._require_host_capacity(plan.image_download_bytes)
        authority = self._image_authority(
            manifest,
            plan.plan_digest,
            request.session_digest or _digest_text("direct-local-lifecycle"),
            request.operation_id or _digest_text(request.ceremony_nonce),
        )
        binding = {
            "session_digest": request.session_digest or _digest_text("direct-local-lifecycle"),
            "plan_digest": request.plan_digest,
            "ceremony_nonce_digest": _digest_text(request.ceremony_nonce),
        }
        with self._image_cache.provisioning_image(
            authority, progress=progress, cancelled=cancelled
        ) as image:
            with self.operation_gate("provision", binding, preserve_claim_on_error=True):
                return self._provision_verified_image(request, plan, manifest, image, cancelled)

    def doctor(self) -> PackVMDoctor:
        """Return ready only for a complete, authenticated direct VZ setup."""

        try:
            state = self._load_state()
            manifest = self._require_manifest()
            self._verify_state_bindings(state, manifest)
            facts = self.prepare_direct_vz()
            if facts is None:
                return PackVMDoctor(
                    False,
                    PACKVM_BACKEND_ID,
                    VZ_PLATFORM,
                    VZ_INSTANCE,
                    reason="macOS VZ signed supervisor protocol is unavailable",
                )
            transport = facts.transport_or_factory()
            if transport is None:
                return PackVMDoctor(
                    False,
                    PACKVM_BACKEND_ID,
                    VZ_PLATFORM,
                    VZ_INSTANCE,
                    reason=facts.reason or "macOS VZ signed supervisor protocol is unavailable",
                )
            return PackVMDoctor(
                True,
                PACKVM_BACKEND_ID,
                VZ_PLATFORM,
                VZ_INSTANCE,
                attestation_digest=str(state["attestation_digest"]),
            )
        except FileNotFoundError:
            return PackVMDoctor(
                False,
                PACKVM_BACKEND_ID,
                VZ_PLATFORM,
                VZ_INSTANCE,
                reason="PackVM VZ has not completed authenticated provisioning",
            )
        except OSError:
            return PackVMDoctor(
                False,
                PACKVM_BACKEND_ID,
                VZ_PLATFORM,
                VZ_INSTANCE,
                reason="PackVM VZ authenticated state could not be verified",
            )
        except ValueError as exc:
            return PackVMDoctor(False, PACKVM_BACKEND_ID, VZ_PLATFORM, VZ_INSTANCE, reason=str(exc))

    def readiness_snapshot(self) -> dict[str, Any]:
        """Project authenticated readiness without revealing private host paths."""

        doctor = self.doctor()
        result: dict[str, Any] = {
            "ready": doctor.ready,
            "backend_id": doctor.backend_id,
            "platform": doctor.platform,
            "instance": doctor.instance,
            "reason": doctor.reason,
            "attestation_digest": doctor.attestation_digest,
            "observed_unix": int(time.time()),
        }
        if doctor.ready:
            state = self._load_state()
            result.update(
                {
                    "image_digest": state["image_digest"],
                    "agent_digest": state["guest_runner_digest"],
                    "config_digest": state["cloud_template_digest"],
                    "helper_digest": state["helper_digest"],
                    "backend_substrate": "macos-vz",
                }
            )
        return result

    def prepare_direct_vz(self) -> MacOSVZProvisionedFacts | None:
        """Construct direct-driver facts from state only after all checks pass."""

        try:
            state = self._load_state()
            manifest = self._require_manifest()
            self._verify_state_bindings(state, manifest)
            if not bool(state.get("protocol_ready")):
                return None
            from tobkiri_host.macos_vz_supervisor import (
                MacOSVZAgentIdentity,
                MacOSVZHelperIdentity,
                MacOSVZLaunchAssets,
            )

            instance_root = Path(str(state["instance_root"]))
            if not _same_private_directory(instance_root, state):
                raise ValueError("PackVM VZ instance root changed")
            return MacOSVZProvisionedFacts(
                helper_path=manifest.helper_path,
                helper_identity=MacOSVZHelperIdentity(
                    binary_digest=manifest.helper_digest,
                    code_digest=manifest.helper_digest,
                    bundle_id=manifest.helper_bundle_id,
                    team_id=manifest.helper_team_id,
                    signing_identity=manifest.helper_signing_identity,
                ),
                launch_assets=MacOSVZLaunchAssets(
                    base_image_digest=manifest.image_digest,
                    base_image_path=str(state["base_image_path"]),
                    agent_template_digest=manifest.agent_digest,
                    config_template_digest=manifest.config_digest,
                    base_image_read_only=True,
                ),
                agent_identity=MacOSVZAgentIdentity(
                    agent_digest=manifest.agent_digest,
                ),
                domain_allocator=self,
                instance_root=instance_root,
                transport_factory=(self._transport_factory or self._transport_for_allocation),
                protocol_ready=True,
                reason=None,
            )
        except (OSError, ValueError, ImportError):
            return None

    def stop(self, confirmation: str) -> None:
        """Mark only the authenticated direct VZ instance as administratively stopped."""

        expected = f"{PACKVM_STOP_PREFIX} {VZ_INSTANCE}"
        if not hmac.compare_digest(confirmation, expected):
            raise ValueError(f"PackVM stop requires exact confirmation: {expected}")
        state = self._load_state()
        with self.operation_gate("stop", {"attestation_digest": str(state["attestation_digest"])}):
            self._verify_state_bindings(state, self._require_manifest())
            state["stopped"] = True
            state["authentication"] = self._sign_state(state)
            _atomic_private_json(self.state_path, state)
            self._audit("stopped", str(state["attestation_digest"]))

    def allocate(
        self,
        *,
        domain_id: str,
        reservation_id: str,
        lease_id: str,
        artifact_digest: str,
        executable_digest: str,
        materialization_digest: str,
        artifact: MaterializedPackArtifact,
        channel_key: bytes,
    ) -> MacOSVZDomainAllocation:
        """Create one unshared APFS COW boot disk and helper-made EFI store."""

        _validate_allocation_identifier(domain_id, "domain")
        _validate_allocation_identifier(reservation_id, "reservation")
        _validate_allocation_identifier(lease_id, "lease")
        launch_artifacts = {
            "artifact": artifact_digest,
            "executable": executable_digest,
            "materialization": materialization_digest,
        }
        if any(not _is_digest(value) for value in launch_artifacts.values()):
            raise ValueError("PackVM VZ allocation artifact bindings are invalid")
        _validate_materialized_artifact(
            artifact,
            artifact_digest=artifact_digest,
            executable_digest=executable_digest,
            materialization_digest=materialization_digest,
        )
        state = self._load_state()
        manifest = self._require_manifest()
        self._verify_state_bindings(state, manifest)
        allocation_name = _digest_text(f"{domain_id}\0{reservation_id}\0{lease_id}")[7:]
        root = self._state_dir / "domains" / allocation_name
        binding = {
            "domain_digest": _digest_text(domain_id),
            "reservation_digest": _digest_text(reservation_id),
            "lease_digest": _digest_text(lease_id),
        }
        with self.operation_gate("allocate", binding):
            if root.exists() or root.is_symlink():
                raise ValueError("PackVM VZ domain allocation already exists")
            # Recheck while the cross-process mutation gate is held.  The
            # user-visible plan reserves the maximum artifact, but actual free
            # space may have changed before this exact allocation begins.
            self._require_allocation_capacity(artifact)
            _ensure_private_directory(self._state_dir / "domains")
            _ensure_private_directory(root)
            cow = root / "boot-cow.raw"
            efi = root / "efi-variable-store.bin"
            helper_process: _MacOSVZHelperProcess | None = None
            try:
                source = Path(str(state["base_image_path"]))
                self._clone_file(source, cow)
                # Test fakes and clonefile both must yield the same
                # owner-only dynamic boot disk contract before it is hashed.
                os.chmod(cow, 0o600)
                _validate_private_file(cow, source.stat().st_size)
                if _file_digest(cow) != str(state["image_digest"]):
                    raise ValueError("PackVM VZ APFS boot clone digest differs from base")
                if self._efi_store_preparer is None:
                    if not isinstance(channel_key, bytes) or len(channel_key) != 32:
                        raise ValueError(
                            "PackVM VZ allocation requires a private per-domain channel key"
                        )
                    helper_process = _MacOSVZHelperProcess(manifest.helper_path, root, channel_key)
                self._prepare_efi_store(
                    root,
                    domain_id,
                    efi,
                    state,
                    helper_process=helper_process,
                )
                seed_facts = self._materialize_domain_seeds(
                    root=root,
                    domain_id=domain_id,
                    reservation_id=reservation_id,
                    lease_id=lease_id,
                    cow_path=cow,
                    efi_path=efi,
                    state=state,
                    manifest=manifest,
                    artifact_digest=artifact_digest,
                    executable_digest=executable_digest,
                    materialization_digest=materialization_digest,
                    artifact=artifact,
                )
                allocation = {
                    "domain_id": domain_id,
                    "reservation_id": reservation_id,
                    "lease_id": lease_id,
                    "run_root": str(root),
                    "cow_disk_path": str(cow),
                    "efi_store_path": str(efi),
                    **seed_facts,
                }
                _atomic_private_json(root / "allocation.json", allocation)
                if helper_process is not None:
                    with self._lock:
                        self._allocation_transports[str(root)] = helper_process
            except Exception:
                if helper_process is not None:
                    helper_process.close()
                self._remove_allocation_root(root)
                raise
        from tobkiri_host.macos_vz_supervisor import MacOSVZDomainAllocation

        return MacOSVZDomainAllocation(
            domain_id=domain_id,
            reservation_id=reservation_id,
            lease_id=lease_id,
            run_root=str(root),
            cow_disk_path=str(cow),
            cow_disk_digest=seed_facts["cow_disk_digest"],
            efi_store_path=str(efi),
            efi_variable_store_digest=seed_facts["efi_variable_store_digest"],
            agent_seed_path=seed_facts["agent_seed_path"],
            agent_seed_digest=seed_facts["agent_seed_digest"],
            config_seed_path=seed_facts["config_seed_path"],
            config_seed_digest=seed_facts["config_seed_digest"],
            guest_public_key=_decode_domain_public_key(seed_facts["guest_public_key_b64"]),
        )

    def release(self, allocation: MacOSVZDomainAllocation) -> None:
        """Remove only a driver-verified allocation after cleanup acknowledgement."""

        required = (
            "domain_id",
            "reservation_id",
            "lease_id",
            "run_root",
            "cow_disk_path",
            "efi_store_path",
        )
        values = {field: getattr(allocation, field, None) for field in required}
        if not all(isinstance(value, str) and value for value in values.values()):
            raise ValueError("PackVM VZ allocation is invalid")
        root = Path(str(values["run_root"]))
        expected_parent = self._state_dir / "domains"
        if root.parent != expected_parent or not _safe_private_domain_root(root):
            raise ValueError("PackVM VZ allocation is outside the managed root")
        record = _read_json_if_present(root / "allocation.json")
        if record is None or any(record.get(key) != value for key, value in values.items()):
            raise ValueError("PackVM VZ allocation binding changed")
        for key in (
            "agent_seed_path",
            "agent_seed_digest",
            "config_seed_path",
            "config_seed_digest",
            "guest_public_key_b64",
            "guest_public_key_digest",
            "cow_disk_digest",
            "efi_variable_store_digest",
        ):
            supplied = getattr(allocation, key, None)
            if key == "guest_public_key_b64" and supplied is None:
                raw_key = getattr(allocation, "guest_public_key", None)
                if isinstance(raw_key, bytes):
                    supplied = base64.urlsafe_b64encode(raw_key).decode("ascii").rstrip("=")
            if supplied is not None and record.get(key) != supplied:
                raise ValueError("PackVM VZ allocation dynamic binding changed")
        if (
            Path(str(values["cow_disk_path"])).parent != root
            or Path(str(values["efi_store_path"])).parent != root
        ):
            raise ValueError("PackVM VZ allocation resource is outside the managed root")
        with self.operation_gate(
            "release",
            {
                "domain_digest": _digest_text(str(values["domain_id"])),
                "reservation_digest": _digest_text(str(values["reservation_id"])),
                "lease_digest": _digest_text(str(values["lease_id"])),
            },
        ):
            with self._lock:
                transport = self._allocation_transports.pop(str(root), None)
                self._claimed_transport_roots.discard(str(root))
            if transport is not None:
                transport.close()
            self._remove_allocation_root(root)

    def _materialize_domain_seeds(
        self,
        *,
        root: Path,
        domain_id: str,
        reservation_id: str,
        lease_id: str,
        cow_path: Path,
        efi_path: Path,
        state: Mapping[str, Any],
        manifest: MacOSVZAssetManifest,
        artifact_digest: str,
        executable_digest: str,
        materialization_digest: str,
        artifact: MaterializedPackArtifact,
    ) -> dict[str, str]:
        """Create fresh domain-bound guest identity and no-cloud seed disks.

        A Host keypair is deliberately generated only after the domain, lease,
        APFS clone, and VZ-created EFI store are fixed.  The private component
        exists only inside the root-only CIDATA seed and is removed with this
        allocation; a base image or another VM never observes it.
        """

        agent_seed = root / "agent-seed.iso"
        config_seed = root / "config-seed.iso"
        artifact_seed = root / "artifact-seed.v1.bin"
        runner = _read_verified_bundle_file(
            manifest.agent_path, manifest.agent_digest, 8 * 1024 * 1024
        )
        service = _read_verified_bundle_file(
            manifest.guest_service_path,
            manifest.guest_service_digest,
            _MAX_MANIFEST_BYTES,
        )
        bubblewrap = _read_verified_bundle_file(
            manifest.bubblewrap_path, manifest.bubblewrap_digest, 128 * 1024
        )
        descriptor = _read_verified_bundle_file(
            manifest.bubblewrap_descriptor_path,
            manifest.bubblewrap_descriptor_digest,
            _MAX_MANIFEST_BYTES,
        )
        _write_iso_seed(
            agent_seed,
            "TOBKIRIAGENT",
            {
                "runner.py": runner,
                "guest_service_template.v1.json": service,
                "bubblewrap_arm64.deb": bubblewrap,
                "bubblewrap_descriptor.v1.json": descriptor,
            },
        )
        agent_seed_digest = _file_digest(agent_seed)
        cow_digest = _file_digest(cow_path)
        efi_digest = _file_digest(efi_path)
        private_key = Ed25519PrivateKey.generate()
        private_pem = private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
        public_key = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        public_key_b64 = base64.urlsafe_b64encode(public_key).decode("ascii").rstrip("=")
        public_key_digest = _digest_bytes(public_key)
        binding_digests = {
            "domain": _digest_text(domain_id),
            "lease": _digest_text(lease_id),
            "reservation": _digest_text(reservation_id),
            "image": str(state["image_digest"]),
            "agent": manifest.agent_digest,
            # The full CIDATA digest is separately measured and bound by the
            # driver allocation.  Its embedded config cannot self-hash, so the
            # guest-visible `config` fact binds the immutable template.
            "config": manifest.config_digest,
            "disk": cow_digest,
            "efi_variable_store": efi_digest,
            "guest_public_key": public_key_digest,
            "artifact": artifact_digest,
            "executable": executable_digest,
            "materialization": materialization_digest,
        }
        artifact_seed_binding = _write_materialized_artifact_seed(artifact_seed, artifact)
        agent_config = {
            "version": 1,
            "domain_id": domain_id,
            "binding_digests": binding_digests,
            "private_key_path": "/run/tobkiri-packvm/agent-ed25519.pem",
            "artifact_seed": artifact_seed_binding,
        }
        cloud_template = _read_verified_bundle_file(
            manifest.config_path, manifest.config_digest, _MAX_MANIFEST_BYTES
        )
        try:
            _write_iso_seed(
                config_seed,
                "cidata",
                {
                    "user-data": cloud_template,
                    "meta-data": (
                        f"instance-id: {domain_id}\nlocal-hostname: tobkiri-packvm\n"
                    ).encode("utf-8"),
                    "network-config": _NOCLOUD_LOCAL_ONLY_NETWORK_CONFIG,
                    "agent-ed25519.pem": private_pem,
                    "agent-config.json": _canonical_bytes(agent_config),
                    "artifact-seed.v1.bin": artifact_seed,
                },
            )
        finally:
            try:
                artifact_seed.unlink()
            except FileNotFoundError:
                pass
        config_seed_digest = _file_digest(config_seed)
        return {
            "agent_seed_path": str(agent_seed),
            "agent_seed_digest": agent_seed_digest,
            "config_seed_path": str(config_seed),
            "config_seed_digest": config_seed_digest,
            "guest_public_key_b64": public_key_b64,
            "guest_public_key_digest": public_key_digest,
            "cow_disk_digest": cow_digest,
            "efi_variable_store_digest": efi_digest,
        }

    def _prepare_efi_store(
        self,
        root: Path,
        domain_id: str,
        efi_path: Path,
        state: Mapping[str, Any],
        *,
        helper_process: _MacOSVZHelperProcess | None,
    ) -> None:
        del state
        if self._efi_store_preparer is not None:
            key = secrets.token_bytes(32)
            response = self._efi_store_preparer(root, domain_id, efi_path, key)
        else:
            if helper_process is None:
                raise ValueError("PackVM VZ helper process is unavailable")
            response = helper_process.prepare_efi_store(
                domain_id=domain_id, run_root=root, efi_path=efi_path
            )
            store = response.get("efi_variable_store") if isinstance(response, Mapping) else None
            if isinstance(store, Mapping):
                response = {
                    "domain_id": response.get("domain_id"),
                    "state": response.get("state"),
                    "path": store.get("path"),
                    "digest": store.get("digest"),
                    "device": store.get("device"),
                    "inode": store.get("inode"),
                }
        expected = {"path": str(efi_path), "domain_id": domain_id, "state": "prepared"}
        if not isinstance(response, Mapping) or any(
            response.get(key) != value for key, value in expected.items()
        ):
            raise ValueError("PackVM VZ helper EFI preparation acknowledgement is invalid")
        digest = response.get("digest")
        device = response.get("device")
        inode = response.get("inode")
        if not _is_digest(digest) or not isinstance(device, str) or not isinstance(inode, str):
            raise ValueError("PackVM VZ helper EFI preparation identity is invalid")
        _validate_private_file(efi_path, efi_path.stat().st_size)
        if _file_digest(efi_path) != digest:
            raise ValueError("PackVM VZ helper EFI preparation digest changed")

    def _transport_for_allocation(
        self, allocation: MacOSVZDomainAllocation
    ) -> MacOSVZSupervisorTransport | None:
        """Hand the driver the one persistent helper for this allocation once."""

        root = getattr(allocation, "run_root", None)
        if not isinstance(root, str):
            return None
        with self._lock:
            if root in self._claimed_transport_roots:
                return None
            process = self._allocation_transports.get(root)
            if process is None:
                return None
            self._claimed_transport_roots.add(root)
            return process

    def _remove_allocation_root(self, root: Path) -> None:
        if not root.exists():
            return
        if not _safe_private_domain_root(root):
            raise ValueError("PackVM VZ allocation cleanup target is unsafe")
        for child in root.iterdir():
            metadata = child.lstat()
            if child.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError("PackVM VZ allocation cleanup residue is unsafe")
            child.unlink()
        root.rmdir()
        if root.exists():
            raise ValueError("PackVM VZ allocation cleanup left residue")
        domains_root = root.parent
        try:
            domains_root.rmdir()
        except OSError:
            # Other live allocations keep the shared parent until their own
            # signed termination/release finishes.
            pass

    def _remove_empty_domains_root(self) -> None:
        """Refuse base cleanup while mutable per-domain resources remain."""

        domains_root = self._state_dir / "domains"
        if not domains_root.exists():
            return
        if not _safe_private_domain_root(domains_root):
            raise ValueError("PackVM VZ domain root is unsafe")
        try:
            domains_root.rmdir()
        except OSError as exc:
            raise ValueError("PackVM VZ has active or residual domain allocations") from exc

    def cleanup(self, confirmation: str) -> None:
        """Delete exactly the authenticated private VZ instance and verify zero residue."""

        expected = f"{PACKVM_CLEANUP_PREFIX} {VZ_INSTANCE}"
        if not hmac.compare_digest(confirmation, expected):
            raise ValueError(f"PackVM cleanup requires exact confirmation: {expected}")
        state = self._load_state()
        with self.operation_gate(
            "cleanup", {"attestation_digest": str(state["attestation_digest"])}
        ):
            self._verify_state_bindings(state, self._require_manifest())
            self._remove_empty_domains_root()
            self._remove_exact_instance(Path(str(state["instance_root"])), state)
            self._audit("deleted", str(state["attestation_digest"]))
            self.state_path.unlink(missing_ok=True)
            self.recovery_path.unlink(missing_ok=True)
            (self._state_dir / "packvm-vz-attestation.key").unlink(missing_ok=True)

    def cleanup_failed_provision(
        self, confirmation: str, expected_proof: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Recoverably remove only an orphan tied to a failed exact ceremony."""

        expected = f"{PACKVM_CLEANUP_PREFIX} {VZ_INSTANCE}"
        if not hmac.compare_digest(confirmation, expected):
            raise ValueError(f"PackVM cleanup requires exact confirmation: {expected}")
        recovery = self._load_recovery()
        for key in _recovery_fields():
            if not _secure_equal(recovery.get(key), expected_proof.get(key)):
                raise ValueError("PackVM VZ orphan recovery proof does not match")
        instance_root = Path(str(recovery["instance_root"]))
        bound_recovery = self._bind_legacy_empty_recovery_root(instance_root, recovery)
        with self.operation_gate(
            "provision", _recovery_binding(expected_proof), recover_claim=True
        ):
            self._remove_exact_instance(instance_root, bound_recovery)
            self.recovery_path.unlink(missing_ok=True)
            self._audit("failed_provision_deleted", None)
        return {"missing": False}

    def recover_provision_operation(self, expected_proof: Mapping[str, Any]) -> PackVMDoctor:
        """Reconcile a restart only when the exact state proof still verifies."""

        state = self._load_state()
        for key in _recovery_fields():
            if not _secure_equal(state.get(key), expected_proof.get(key)):
                raise ValueError("PackVM VZ provision recovery proof changed")
        doctor = self.doctor()
        if not doctor.ready:
            raise ValueError(doctor.reason or "PackVM VZ is unavailable")
        return doctor

    def _provision_verified_image(
        self,
        request: PackVMProvisioningRequest,
        plan: PackVMProvisioningPlan,
        manifest: MacOSVZAssetManifest,
        image: PackVMPinnedImage,
        cancelled: Callable[[], bool] | None,
    ) -> PackVMDoctor:
        if self.state_path.exists():
            raise ValueError("PackVM VZ is already provisioned; use explicit cleanup")
        if cancelled is not None and cancelled():
            raise PackVMImageCancelled(
                "packvm_image_cancelled", "PackVM VZ provisioning was cancelled"
            )
        self._ensure_state_root()
        root = self._state_dir / "instances" / VZ_INSTANCE
        recovery = self._recovery_record(request, plan, manifest, root)
        _atomic_private_json(self.recovery_path, self._signed_recovery(recovery))
        try:
            self._create_instance(root, image, manifest, cancelled)
            state = {
                "version": VZ_STATE_VERSION,
                "backend_id": PACKVM_BACKEND_ID,
                "platform": VZ_PLATFORM,
                "instance": VZ_INSTANCE,
                "session_digest": request.session_digest or _digest_text("direct-local-lifecycle"),
                "plan_digest": plan.plan_digest,
                "ceremony_nonce_digest": _digest_text(request.ceremony_nonce),
                "image_digest": manifest.image_digest,
                "image_source": manifest.image_source,
                "cloud_template_digest": manifest.config_digest,
                "helper_digest": manifest.helper_digest,
                "guest_runner_digest": manifest.agent_digest,
                "bubblewrap_digest": manifest.bubblewrap_digest,
                "host_build_digest": manifest.helper_digest,
                "instance_root": str(root),
                "instance_root_device": int(root.lstat().st_dev),
                "instance_root_inode": int(root.lstat().st_ino),
                "base_image_path": str(image.verified.path),
                # Provisioning a base image does not make a VM ready.  A
                # signed helper transport and a per-domain guest seed are
                # separately required at driver registration time.
                "protocol_ready": True,
                "stopped": False,
                "created_unix": int(time.time()),
                **self.recovery_identity(),
            }
            state["attestation_digest"] = _canonical_digest(state)
            state["authentication"] = self._sign_state(state)
            _atomic_private_json(self.state_path, state)
            self.recovery_path.unlink(missing_ok=True)
            self._audit("provisioned", str(state["attestation_digest"]))
            return self.doctor()
        except Exception:
            # Keep the signed recovery record for the exact cleanup ceremony.
            raise

    def _create_instance(
        self,
        root: Path,
        image: PackVMPinnedImage,
        manifest: MacOSVZAssetManifest,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        if root.exists() or root.is_symlink():
            raise ValueError("PackVM VZ instance root already exists")
        _ensure_private_directory(root)
        recovery = self._load_recovery()
        metadata = root.lstat()
        recovery.update(
            {
                "instance_root_device": int(metadata.st_dev),
                "instance_root_inode": int(metadata.st_ino),
            }
        )
        _atomic_private_json(self.recovery_path, self._signed_recovery(recovery))
        try:
            verified = image.verified
            source = verified.path
            if _file_digest(source) != manifest.image_digest:
                raise ValueError("PackVM VZ verified raw EFI image digest changed")
            if (
                manifest.image_sha512 is not None
                and _file_digest_algorithm(source, "sha512").removeprefix("sha512:")
                != manifest.image_sha512
            ):
                raise ValueError("PackVM VZ verified raw EFI image SHA-512 changed")
            # The cache owns the immutable base.  The per-instance metadata only
            # records a digest/path and never copies a 3 GiB base image.
            base_reference = {
                "image_digest": manifest.image_digest,
                "image_size_bytes": verified.size_bytes,
                "source_url": manifest.image_source,
            }
            _atomic_private_json(root / "base-image.json", base_reference)
            if cancelled is not None and cancelled():
                raise PackVMImageCancelled(
                    "packvm_image_cancelled", "PackVM VZ provisioning was cancelled"
                )
            self._verify_instance_files(root, manifest)
        except Exception:
            # Do not erase a potentially partially-created root here: the
            # signed recovery proof makes a later cleanup exact and auditable.
            raise

    def _verify_instance_files(self, root: Path, manifest: MacOSVZAssetManifest) -> None:
        for path in (root / "base-image.json",):
            _read_private_file(path, maximum=2 * 1024 * 1024)
        verified, reason = self._verify_helper_identity(manifest)
        if not verified:
            raise ValueError(reason or "PackVM VZ signed helper changed")
        for path, digest in (
            (manifest.agent_path, manifest.agent_digest),
            (manifest.guest_service_path, manifest.guest_service_digest),
            (manifest.bubblewrap_path, manifest.bubblewrap_digest),
            (manifest.config_path, manifest.config_digest),
        ):
            if _file_digest(path) != digest:
                raise ValueError("PackVM VZ packaged asset digest changed")

    def _load_manifest_for_plan(self) -> tuple[MacOSVZAssetManifest | None, str | None]:
        if self._platform_system != "darwin" or self._machine != "arm64":
            return None, "direct PackVM VZ production requires macOS on Apple Silicon"
        try:
            return self._require_manifest(), None
        except (OSError, ValueError) as exc:
            return None, str(exc)

    def _require_manifest(self) -> MacOSVZAssetManifest:
        if self._platform_system != "darwin" or self._machine != "arm64":
            raise ValueError("direct PackVM VZ production requires macOS on Apple Silicon")
        if self._bundle_binding_error is not None:
            raise ValueError(self._bundle_binding_error)
        path = self._asset_manifest_path
        if path is None:
            raise ValueError("packaged macOS VZ asset manifest is unavailable")
        self._verify_authenticated_bundle_binding(path)
        raw = _read_private_or_bundle_file(path, _MAX_MANIFEST_BYTES)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("packaged macOS VZ asset manifest is invalid") from exc
        if not isinstance(payload, Mapping):
            raise ValueError("packaged macOS VZ asset manifest is unsupported")
        if payload.get("schema") != VZ_ASSET_MANIFEST_SCHEMA:
            raise ValueError("packaged macOS VZ asset manifest is unsupported")
        manifest = self._parse_provisioning_manifest(path, payload)
        verified, reason = self._verify_helper_identity(manifest)
        if not verified:
            raise ValueError(reason or "macOS VZ native helper identity verification failed")
        return manifest

    def _verify_authenticated_bundle_binding(self, manifest_path: Path) -> None:
        """Re-read and match launcher-attested PackVM resource identities.

        The signed Launcher supplies this binding only over the sealed launch
        wire.  It is never reconstructed from an ambient path or the mutable
        resource manifests.  We nevertheless hash both manifest files for
        every readiness path, so replacing either after Python starts fails
        closed before helper metadata is consumed.
        """

        binding = self._bundle_binding
        if binding is None:
            return
        resource_root = binding.root / "Contents" / "Resources"
        expected_manifest = resource_root / "packvm-vz-provisioning.v1.json"
        expected_helper_manifest = resource_root / "packvm-vz-helper.manifest.v1.json"
        if (
            manifest_path != expected_manifest
            or not _safe_relative_file(binding.root, expected_manifest)
            or not _safe_relative_file(binding.root, expected_helper_manifest)
            or _file_digest(expected_manifest) != binding.provisioning_sha256
            or _file_digest(expected_helper_manifest) != binding.helper_manifest_sha256
        ):
            raise ValueError("packaged macOS VZ bundle binding changed")

    def _parse_provisioning_manifest(
        self, manifest_path: Path, payload: Mapping[str, Any]
    ) -> MacOSVZAssetManifest:
        """Bind the exact packaged provisioning inputs and helper provenance."""

        if (
            set(payload) != {"schema", "target", "boot_mode", "inputs"}
            or payload.get("target") != "aarch64-apple-darwin"
            or payload.get("boot_mode") != "efi"
            or not isinstance(payload.get("inputs"), list)
        ):
            raise ValueError("packaged macOS VZ provisioning manifest is invalid")
        resource_root = manifest_path.parent
        expected_names = {
            "image_descriptor",
            "bubblewrap_descriptor",
            "bubblewrap_package",
            "guest_runner",
            "guest_service_template",
            "cloud_init_template",
            "licenses",
        }
        inputs: dict[str, Path] = {}
        for entry in payload["inputs"]:
            if (
                not isinstance(entry, Mapping)
                or set(entry) != {"name", "path", "sha256"}
                or not isinstance(entry.get("name"), str)
                or entry["name"] in inputs
                or not _is_digest(entry.get("sha256"))
                or not isinstance(entry.get("path"), str)
            ):
                raise ValueError("packaged macOS VZ provisioning input is invalid")
            relative = str(entry["path"])
            if not relative.startswith("packvm-vz-provisioning/"):
                raise ValueError("packaged macOS VZ provisioning input path is unsafe")
            candidate = resource_root / relative
            if (
                not _safe_relative_file(resource_root, candidate)
                or _file_digest(candidate) != entry["sha256"]
            ):
                raise ValueError("packaged macOS VZ provisioning input changed")
            inputs[str(entry["name"])] = candidate
        if set(inputs) != expected_names:
            raise ValueError("packaged macOS VZ provisioning inputs are incomplete")
        image = _parse_image_descriptor(
            inputs["image_descriptor"],
            expected_digest=_input_digest(payload["inputs"], "image_descriptor"),
        )
        bubblewrap = _parse_bubblewrap_descriptor(
            inputs["bubblewrap_descriptor"],
            expected_digest=_input_digest(payload["inputs"], "bubblewrap_descriptor"),
        )
        bubblewrap_digest = _input_digest(payload["inputs"], "bubblewrap_package")
        if (
            _file_digest(inputs["bubblewrap_package"]) != bubblewrap_digest
            or bubblewrap["sha256"] != bubblewrap_digest
            or inputs["bubblewrap_package"].stat().st_size != bubblewrap["size_bytes"]
        ):
            raise ValueError("packaged macOS VZ bubblewrap package changed")
        _parse_guest_service_template(
            inputs["guest_service_template"],
            expected_digest=_input_digest(payload["inputs"], "guest_service_template"),
            guest_runner_digest=_input_digest(payload["inputs"], "guest_runner"),
        )
        bundle = self._parse_bundle_helper_manifest(resource_root, manifest_path)
        helper_path = bundle["helper_path"]
        helper_digest = bundle["helper_digest"]
        if not isinstance(helper_path, Path) or not isinstance(helper_digest, str):
            raise ValueError("packaged macOS VZ helper manifest is invalid")
        return MacOSVZAssetManifest(
            helper_path=helper_path,
            helper_digest=helper_digest,
            helper_bundle_id=str(bundle["helper_bundle_id"]),
            helper_team_id=str(bundle["helper_team_id"]),
            helper_signing_identity=str(bundle["helper_signing_identity"]),
            agent_path=inputs["guest_runner"],
            agent_digest=_file_digest(inputs["guest_runner"]),
            guest_service_path=inputs["guest_service_template"],
            guest_service_digest=_file_digest(inputs["guest_service_template"]),
            bubblewrap_path=inputs["bubblewrap_package"],
            bubblewrap_digest=bubblewrap_digest,
            bubblewrap_descriptor_path=inputs["bubblewrap_descriptor"],
            bubblewrap_descriptor_digest=_input_digest(payload["inputs"], "bubblewrap_descriptor"),
            config_path=inputs["cloud_init_template"],
            config_digest=_file_digest(inputs["cloud_init_template"]),
            image_source=image["source"],
            image_digest=image["sha256"],
            image_sha512=image["sha512"],
            image_size_bytes=image["size_bytes"],
            architecture="aarch64-apple-darwin",
            manifest_digest=_file_digest(manifest_path),
        )

    def _parse_bundle_helper_manifest(
        self, resource_root: Path, provisioning_manifest_path: Path
    ) -> dict[str, object]:
        path = resource_root / "packvm-vz-helper.manifest.v1.json"
        try:
            bundle = json.loads(_read_private_or_bundle_file(path, _MAX_MANIFEST_BYTES))
        except json.JSONDecodeError as exc:
            raise ValueError("packaged macOS VZ helper manifest is invalid") from exc
        if not isinstance(bundle, Mapping) or set(bundle) != {"schema", "helper", "provisioning"}:
            raise ValueError("packaged macOS VZ helper manifest is invalid")
        helper = bundle.get("helper")
        provisioning = bundle.get("provisioning")
        if (
            bundle.get("schema") != VZ_BUNDLE_MANIFEST_SCHEMA
            or not isinstance(helper, Mapping)
            or set(helper) != {"path", "code_sha256", "identifier", "entitlements", "signing"}
            or not isinstance(provisioning, Mapping)
            or set(provisioning) != {"path", "sha256"}
            or provisioning.get("path") != "Contents/Resources/packvm-vz-provisioning.v1.json"
            or provisioning.get("sha256") != _file_digest(provisioning_manifest_path)
        ):
            raise ValueError("packaged macOS VZ helper manifest binding is invalid")
        signing = helper.get("signing")
        if (
            helper.get("path") != "Contents/MacOS/tobkiri-packvm-vz-helper"
            or helper.get("identifier") != "dev.tobkiri.launcher.packvm-vz-helper"
            or helper.get("entitlements") != ["com.apple.security.virtualization"]
            or not _is_digest(helper.get("code_sha256"))
            or not isinstance(signing, Mapping)
            or set(signing) != {"signing_mode", "team_id", "authority"}
        ):
            raise ValueError("packaged macOS VZ helper production identity is unavailable")
        signing_mode = signing.get("signing_mode")
        if signing_mode == "ad-hoc":
            if signing.get("team_id") is not None or signing.get("authority") is not None:
                raise ValueError("packaged macOS VZ helper ad-hoc identity is invalid")
            team_id = ""
            authority = ""
        elif signing_mode == "developer-id":
            candidate_team_id = signing.get("team_id")
            candidate_authority = signing.get("authority")
            if (
                not isinstance(candidate_team_id, str)
                or not isinstance(candidate_authority, str)
                or len(candidate_team_id) != 10
                or not candidate_team_id.isascii()
                or not candidate_team_id.isalnum()
                or candidate_team_id != candidate_team_id.upper()
                or not candidate_authority.startswith("Developer ID Application: ")
                or not candidate_authority.endswith(f" ({candidate_team_id})")
                or len(candidate_authority) > 512
            ):
                raise ValueError("packaged macOS VZ helper production identity is invalid")
            team_id = candidate_team_id
            authority = candidate_authority
        else:
            raise ValueError("packaged macOS VZ helper signing mode is invalid")
        binding = self._bundle_binding
        if binding is not None and not hmac.compare_digest(
            team_id, binding.helper_team_id
        ):
            raise ValueError("packaged macOS VZ helper Team ID binding changed")
        bundle_root = resource_root.parent.parent
        helper_path = bundle_root / str(helper["path"])
        if not _safe_relative_file(bundle_root, helper_path):
            raise ValueError("packaged macOS VZ helper path is unsafe")
        if _macho_code_digest(helper_path) != helper["code_sha256"]:
            raise ValueError("packaged macOS VZ helper code identity changed")
        # The direct driver independently invokes codesign.  This initial
        # binding catches substitution before a helper is even constructed.
        return {
            "helper_path": helper_path,
            "helper_digest": str(helper["code_sha256"]),
            "helper_bundle_id": str(helper["identifier"]),
            "helper_team_id": team_id,
            "helper_signing_identity": authority,
        }

    def _verify_helper_identity(self, manifest: MacOSVZAssetManifest) -> tuple[bool, str | None]:
        if self._helper_identity_verifier is not None:
            return self._helper_identity_verifier(manifest)
        try:
            from tobkiri_host.macos_vz_supervisor import (
                MacOSVZHelperIdentity,
                verify_macos_vz_helper_identity,
            )

            return verify_macos_vz_helper_identity(
                manifest.helper_path,
                MacOSVZHelperIdentity(
                    binary_digest=manifest.helper_digest,
                    bundle_id=manifest.helper_bundle_id,
                    team_id=manifest.helper_team_id,
                    signing_identity=manifest.helper_signing_identity,
                ),
            )
        except (ImportError, OSError, ValueError):
            return False, "macOS VZ native helper identity verification failed"

    def _image_authority(
        self,
        manifest: MacOSVZAssetManifest,
        plan_digest: str,
        session_digest: str,
        operation_id: str,
    ) -> PackVMImageAuthority:
        return PackVMImageAuthority(
            source_url=manifest.image_source,
            digest=manifest.image_digest,
            size_bytes=manifest.image_size_bytes,
            platform="macos",
            architecture="arm64",
            plan_digest=plan_digest,
            session_digest=session_digest,
            operation_id=operation_id,
        )

    def _required_host_space(self, download_bytes: int) -> int:
        return (
            VZ_RAW_EFI_IMAGE_DECLARED_BYTES
            + VZ_HOST_STORAGE_RESERVE_BYTES
            + VZ_ARTIFACT_SEED_PEAK_RESERVE_BYTES
            + download_bytes
        )

    def _required_allocation_space(self, artifact: MaterializedPackArtifact) -> int:
        """Return peak bytes needed to allocate this already-validated artifact."""

        artifact_seed_bytes = _materialized_artifact_seed_size(artifact)
        return (
            VZ_RAW_EFI_IMAGE_DECLARED_BYTES
            + VZ_HOST_STORAGE_RESERVE_BYTES
            + (2 * artifact_seed_bytes)
        )

    def _host_capacity(self, required: int) -> tuple[int, str | None]:
        path = self._state_dir
        while not path.exists() and path != path.parent:
            path = path.parent
        try:
            available = int(self._disk_usage(path).free)
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            return 0, f"PackVM VZ host storage preflight failed: {exc}"
        if available < required:
            return available, (
                "PackVM VZ provisioning requires at least "
                f"{_format_gib(required)} free; only {_format_gib(available)} is available"
            )
        return available, None

    def _require_host_capacity(self, download_bytes: int) -> None:
        _available, reason = self._host_capacity(self._required_host_space(download_bytes))
        if reason is not None:
            raise ValueError(reason)

    def _require_allocation_capacity(self, artifact: MaterializedPackArtifact) -> None:
        """Recheck current free space for the exact artifact before mutation."""

        _available, reason = self._host_capacity(self._required_allocation_space(artifact))
        if reason is not None:
            raise ValueError(reason)

    def _ensure_state_root(self) -> None:
        if (
            not self._requested_state_dir.is_absolute()
            or self._requested_state_dir != self._state_dir
        ):
            raise ValueError("PackVM VZ state root must be absolute and contain no symlinks")
        _ensure_private_directory(self._state_dir)
        _ensure_private_directory(self._state_dir / "instances")

    def _sign_state(self, state: Mapping[str, Any]) -> str:
        key = generate_or_load_signing_key(self._state_dir / "packvm-vz-attestation.key")
        unsigned = {key: value for key, value in state.items() if key != "authentication"}
        return hmac.new(key, _canonical_bytes(unsigned), hashlib.sha256).hexdigest()

    def _load_state(self) -> dict[str, Any]:
        raw = _read_private_file(self.state_path, _MAX_STATE_BYTES)
        try:
            state = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("PackVM VZ attestation state is invalid") from exc
        if (
            not isinstance(state, dict)
            or state.get("version") != VZ_STATE_VERSION
            or state.get("backend_id") != PACKVM_BACKEND_ID
            or state.get("platform") != VZ_PLATFORM
            or state.get("instance") != VZ_INSTANCE
        ):
            raise ValueError("PackVM VZ attestation state is unsupported")
        authentication = state.get("authentication")
        if not isinstance(authentication, str):
            raise ValueError("PackVM VZ attestation authentication is missing")
        key = _read_private_file(self._state_dir / "packvm-vz-attestation.key", 256)
        unsigned = {key: value for key, value in state.items() if key != "authentication"}
        if not hmac.compare_digest(
            authentication, hmac.new(key, _canonical_bytes(unsigned), hashlib.sha256).hexdigest()
        ):
            raise ValueError("PackVM VZ attestation authentication failed")
        attestation = unsigned.pop("attestation_digest", None)
        if not isinstance(attestation, str) or not hmac.compare_digest(
            attestation, _canonical_digest(unsigned)
        ):
            raise ValueError("PackVM VZ attestation digest failed")
        return state

    def _verify_state_bindings(
        self, state: Mapping[str, Any], manifest: MacOSVZAssetManifest
    ) -> None:
        if state.get("stopped") is True:
            raise ValueError("PackVM VZ is stopped")
        expected = {
            "image_digest": manifest.image_digest,
            "image_source": manifest.image_source,
            "cloud_template_digest": manifest.config_digest,
            "helper_digest": manifest.helper_digest,
            "guest_runner_digest": manifest.agent_digest,
            "bubblewrap_digest": manifest.bubblewrap_digest,
            "host_build_digest": manifest.helper_digest,
        }
        for key, value in expected.items():
            if not _secure_equal(state.get(key), value):
                raise ValueError(f"PackVM VZ {key} changed")
        for recovery_key, recovery_value in self.recovery_identity().items():
            if state.get(recovery_key) != recovery_value:
                raise ValueError(f"PackVM VZ {recovery_key} changed")
        root = Path(str(state.get("instance_root") or ""))
        if not _same_private_directory(root, state):
            raise ValueError("PackVM VZ instance root changed")
        base_image = Path(str(state.get("base_image_path") or ""))
        if not base_image.is_absolute() or _file_digest(base_image) != manifest.image_digest:
            raise ValueError("PackVM VZ immutable base image changed")
        self._verify_instance_files(root, manifest)

    def _recovery_record(
        self,
        request: PackVMProvisioningRequest,
        plan: PackVMProvisioningPlan,
        manifest: MacOSVZAssetManifest,
        root: Path,
    ) -> dict[str, Any]:
        return {
            "version": VZ_STATE_VERSION,
            "backend_id": PACKVM_BACKEND_ID,
            "instance": VZ_INSTANCE,
            "session_digest": request.session_digest or _digest_text("direct-local-lifecycle"),
            "plan_digest": plan.plan_digest,
            "ceremony_nonce_digest": _digest_text(request.ceremony_nonce),
            "config_digest": manifest.config_digest,
            "image_digest": manifest.image_digest,
            "guest_runner_digest": manifest.agent_digest,
            "host_build_digest": manifest.helper_digest,
            "instance_root": str(root),
            **self.recovery_identity(),
        }

    def _signed_recovery(self, recovery: Mapping[str, Any]) -> dict[str, Any]:
        unsigned = dict(recovery)
        unsigned["authentication"] = self._sign_state(unsigned)
        return unsigned

    def _load_recovery(self) -> dict[str, Any]:
        recovery = _read_json_if_present(self.recovery_path)
        if recovery is None:
            raise ValueError("PackVM VZ failed-provision recovery evidence is unavailable")
        authentication = recovery.pop("authentication", None)
        if not isinstance(authentication, str) or not hmac.compare_digest(
            authentication, self._sign_state(recovery)
        ):
            raise ValueError("PackVM VZ failed-provision recovery authentication failed")
        return recovery

    def _remove_exact_instance(self, root: Path, state: Mapping[str, Any]) -> None:
        if not _same_private_directory(root, state):
            raise ValueError("PackVM VZ cleanup target changed")
        expected_parent = self._state_dir / "instances"
        if root.parent != expected_parent or root.name != VZ_INSTANCE:
            raise ValueError("PackVM VZ cleanup target is outside the managed root")
        for child in root.iterdir():
            metadata = child.lstat()
            if child.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError("PackVM VZ cleanup residue is unsafe")
            child.unlink()
        root.rmdir()
        if root.exists():
            raise ValueError("PackVM VZ cleanup left instance residue")

    def _bind_legacy_empty_recovery_root(
        self, root: Path, recovery: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Bind an empty root created before recovery recorded its inode."""

        device = recovery.get("instance_root_device")
        inode = recovery.get("instance_root_inode")
        if device is not None or inode is not None:
            if isinstance(device, int) and isinstance(inode, int):
                return recovery
            raise ValueError("PackVM VZ cleanup target binding is incomplete")
        expected = self._state_dir / "instances" / VZ_INSTANCE
        if root != expected or root.is_symlink():
            raise ValueError("PackVM VZ cleanup target changed")
        metadata = root.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_mode & 0o077
            or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
            or next(root.iterdir(), None) is not None
        ):
            raise ValueError("PackVM VZ cleanup target changed")
        bound = dict(recovery)
        bound["instance_root_device"] = int(metadata.st_dev)
        bound["instance_root_inode"] = int(metadata.st_ino)
        return bound

    def _audit(self, event: str, attestation_digest: str | None) -> None:
        self._ensure_state_root()
        record = {
            "event": event,
            "backend_id": PACKVM_BACKEND_ID,
            "instance": VZ_INSTANCE,
            "attestation_digest": attestation_digest,
            "timestamp_unix": int(time.time()),
        }
        descriptor = _open_private_file(self.audit_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY)
        try:
            os.write(descriptor, _canonical_bytes(record) + b"\n")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def default_packvm_provisioner() -> MacOSVZProvisioner:
    """Return direct VZ on supported macOS and no Lima default elsewhere."""

    if os.environ.get("RUMI_ENVIRONMENT") == "development":
        development_bundle = os.environ.get(
            "TOBKIRI_DEVELOPMENT_PACKVM_BUNDLE_ROOT", ""
        ).strip()
        if development_bundle:
            bundle_root = Path(development_bundle)
            return MacOSVZProvisioner(
                bundle_root=bundle_root,
                asset_manifest_path=(
                    bundle_root
                    / "Contents"
                    / "Resources"
                    / "packvm-vz-provisioning.v1.json"
                ),
                allow_ad_hoc_helper_identity=True,
            )
    return MacOSVZProvisioner()


def _packaged_packvm_bundle_binding() -> object | None:
    """Read the sealed Launcher PackVM binding without an env fallback."""

    from core_runtime.packaged_application_bundle import packvm_bundle_binding

    return packvm_bundle_binding()


def _normalise_packvm_bundle_binding(value: object) -> _AuthenticatedPackVMBundleBinding:
    """Validate the immutable binding shape before any resource is opened."""

    root = getattr(value, "root", None)
    provisioning_sha256 = getattr(value, "provisioning_sha256", None)
    helper_manifest_sha256 = getattr(value, "helper_manifest_sha256", None)
    helper_team_id = getattr(value, "helper_team_id", None)
    if (
        not isinstance(root, Path)
        or not root.is_absolute()
        or root.suffix != ".app"
        or root.is_symlink()
        or not root.is_dir()
        or not _is_digest(provisioning_sha256)
        or not _is_digest(helper_manifest_sha256)
        or not isinstance(helper_team_id, str)
    ):
        raise ValueError("packaged macOS VZ bundle binding is invalid")
    try:
        canonical_root = root.resolve(strict=True)
    except OSError as exc:
        raise ValueError("packaged macOS VZ bundle binding is invalid") from exc
    if canonical_root != root:
        raise ValueError("packaged macOS VZ bundle binding is invalid")
    if helper_team_id and (
        len(helper_team_id) != 10
        or not helper_team_id.isascii()
        or not helper_team_id.isalnum()
        or helper_team_id != helper_team_id.upper()
    ):
        raise ValueError("packaged macOS VZ bundle binding is invalid")
    return _AuthenticatedPackVMBundleBinding(
        root=canonical_root,
        provisioning_sha256=str(provisioning_sha256),
        helper_manifest_sha256=str(helper_manifest_sha256),
        helper_team_id=helper_team_id,
    )


def _default_state_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "Tobkiri" / "packvm-vz"


def _discover_bundle_root() -> Path | None:
    """Find only the enclosing ``.app`` bundle of the sealed Python runtime."""

    try:
        executable = Path(sys.executable).resolve()
    except OSError:
        return None
    for parent in (executable, *executable.parents):
        if parent.name == "Contents" and parent.parent.suffix == ".app":
            return parent.parent
    return None


def _default_assets_manifest_path(bundle_root: Path | None) -> Path | None:
    if bundle_root is None:
        return None
    return bundle_root / "Contents" / "Resources" / "packvm-vz-provisioning.v1.json"


def _normalise_machine(value: str) -> str:
    normalised = value.strip().casefold()
    if normalised in {"arm64", "aarch64"}:
        return "arm64"
    return normalised or "unknown"


def _decode_domain_public_key(value: object) -> bytes:
    """Decode the Host-generated per-domain Ed25519 public key."""

    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError("PackVM VZ domain public key is invalid")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, UnicodeError) as exc:
        raise ValueError("PackVM VZ domain public key is invalid") from exc
    canonical = base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=")
    if canonical != value.rstrip("=") or len(decoded) != 32:
        raise ValueError("PackVM VZ domain public key is invalid")
    return decoded


def _input_digest(entries: object, name: str) -> str:
    """Return one already shape-checked provisioning input digest."""

    if not isinstance(entries, list):
        raise ValueError("packaged macOS VZ provisioning inputs are invalid")
    for entry in entries:
        if isinstance(entry, Mapping) and entry.get("name") == name:
            digest = entry.get("sha256")
            if _is_digest(digest):
                return str(digest)
    raise ValueError("packaged macOS VZ provisioning input is missing")


def _parse_image_descriptor(
    path: Path, *, expected_digest: str | None = None
) -> _ImageDescriptorFacts:
    """Read the hashed official raw-image descriptor with exact semantics."""

    try:
        raw = _read_private_or_bundle_file(path, _MAX_MANIFEST_BYTES)
        if expected_digest is not None and _digest_bytes(raw) != expected_digest:
            raise ValueError("PackVM VZ image descriptor changed")
        descriptor = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("PackVM VZ image descriptor is invalid") from exc
    if (
        not isinstance(descriptor, Mapping)
        or set(descriptor) != {"schema", "boot_mode", "architecture", "format", "source", "license"}
        or descriptor.get("schema") != "io.tobkiri.packvm-vz-image-descriptor.v1"
        or descriptor.get("boot_mode") != "efi"
        or descriptor.get("architecture") != "arm64"
        or descriptor.get("format") != "raw"
        or not isinstance(descriptor.get("source"), Mapping)
        or not isinstance(descriptor.get("license"), Mapping)
    ):
        raise ValueError("PackVM VZ image descriptor is invalid")
    source = descriptor["source"]
    license_value = descriptor["license"]
    if (
        set(source) != {"url", "size_bytes", "sha256", "sha512"}
        or not isinstance(source.get("url"), str)
        or source.get("url") != _DIRECT_IMAGE_URL
        or not isinstance(source.get("size_bytes"), int)
        or isinstance(source.get("size_bytes"), bool)
        or int(source["size_bytes"]) != VZ_RAW_EFI_IMAGE_DECLARED_BYTES
        or source.get("sha256") != _DIRECT_IMAGE_SHA256
        or source.get("sha512") != _DIRECT_IMAGE_SHA512
        or set(license_value) != {"spdx_id", "url"}
        or not isinstance(license_value.get("spdx_id"), str)
        or not str(license_value["spdx_id"])
        or not isinstance(license_value.get("url"), str)
        or not str(license_value["url"]).startswith("https://")
    ):
        raise ValueError("PackVM VZ image descriptor is invalid")
    return {
        "source": str(source["url"]),
        "size_bytes": int(source["size_bytes"]),
        "sha256": str(source["sha256"]),
        "sha512": str(source["sha512"]),
    }


def _parse_bubblewrap_descriptor(
    path: Path, *, expected_digest: str | None = None
) -> dict[str, object]:
    """Validate the offline, digest-pinned bubblewrap package declaration."""

    try:
        raw = _read_private_or_bundle_file(path, _MAX_MANIFEST_BYTES)
        if expected_digest is not None and _digest_bytes(raw) != expected_digest:
            raise ValueError("PackVM VZ bubblewrap descriptor changed")
        descriptor = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("PackVM VZ bubblewrap descriptor is invalid") from exc
    source = descriptor.get("source") if isinstance(descriptor, Mapping) else None
    if (
        not isinstance(descriptor, Mapping)
        or set(descriptor) != {"schema", "package", "version", "architecture", "source"}
        or descriptor.get("schema") != "io.tobkiri.packvm-vz-bubblewrap-descriptor.v1"
        or descriptor.get("package") != "bubblewrap"
        or descriptor.get("version") != "0.11.0-2+deb13u1"
        or descriptor.get("architecture") != "arm64"
        or not isinstance(source, Mapping)
        or set(source) != {"url", "size_bytes", "sha256"}
        or source.get("url")
        != "https://deb.debian.org/debian/pool/main/b/bubblewrap/"
        "bubblewrap_0.11.0-2+deb13u1_arm64.deb"
        or source.get("size_bytes") != 50132
        or source.get("sha256")
        != "sha256:c838daebddb7fe169ebb461612e90b1fcb981de838f81bfbecf26d45ab5a71ee"
    ):
        raise ValueError("PackVM VZ bubblewrap descriptor is invalid")
    return {"size_bytes": 50132, "sha256": str(source["sha256"])}


def _parse_guest_service_template(
    path: Path,
    *,
    expected_digest: str | None = None,
    guest_runner_digest: str | None = None,
) -> None:
    """Validate the immutable guest service template before key injection."""

    try:
        raw = _read_private_or_bundle_file(path, _MAX_MANIFEST_BYTES)
        if expected_digest is not None and _digest_bytes(raw) != expected_digest:
            raise ValueError("PackVM VZ guest service template changed")
        template = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("PackVM VZ guest service template is invalid") from exc
    if (
        not isinstance(template, Mapping)
        or set(template) != {"schema", "protocol", "guest_runner_sha256", "service_unit"}
        or template.get("schema") != "io.tobkiri.packvm-vz-guest-service-template.v1"
        or template.get("protocol") != "io.tobkiri.macos-vz-supervisor.v1"
        or not _is_digest(template.get("guest_runner_sha256"))
        or (
            guest_runner_digest is not None
            and template.get("guest_runner_sha256") != guest_runner_digest
        )
        or not isinstance(template.get("service_unit"), str)
        or not str(template["service_unit"])
        or len(str(template["service_unit"])) > 128 * 1024
    ):
        raise ValueError("PackVM VZ guest service template is invalid")
    return None


def _macho_code_digest(path: Path) -> str:
    """Hash signing-independent code bytes of the thin helper Mach-O image."""

    data = bytearray(_read_private_or_bundle_file(path, 128 * 1024 * 1024))
    if len(data) < 32 or data[:4] != b"\xcf\xfa\xed\xfe":
        raise ValueError("PackVM VZ helper is not a thin arm64 Mach-O image")
    command_count, command_bytes = struct.unpack_from("<II", data, 16)
    offset = 32
    command_end = offset + command_bytes
    if command_end > len(data):
        raise ValueError("PackVM VZ helper Mach-O commands are invalid")
    signature: tuple[int, int, int] | None = None
    linkedit: int | None = None
    for _ in range(command_count):
        if offset + 8 > command_end:
            raise ValueError("PackVM VZ helper Mach-O command is truncated")
        command, command_size = struct.unpack_from("<II", data, offset)
        if command_size < 8 or offset + command_size > command_end:
            raise ValueError("PackVM VZ helper Mach-O command size is invalid")
        if command == 0x1D:
            if command_size != 16 or signature is not None:
                raise ValueError("PackVM VZ helper signature command is invalid")
            data_offset, data_size = struct.unpack_from("<II", data, offset + 8)
            signature = (offset, data_offset, data_size)
        elif command == 0x19 and data[offset + 8 : offset + 24].rstrip(b"\0") == b"__LINKEDIT":
            if command_size < 72 or linkedit is not None:
                raise ValueError("PackVM VZ helper linkedit command is invalid")
            linkedit = offset
        offset += command_size
    if offset != command_end or signature is None or linkedit is None:
        raise ValueError("PackVM VZ helper signature command is missing")
    signature_offset, data_offset, data_size = signature
    if data_offset < command_end or data_offset + data_size != len(data):
        raise ValueError("PackVM VZ helper signature region is invalid")
    data[signature_offset + 8 : signature_offset + 16] = b"\0" * 8
    data[linkedit + 32 : linkedit + 40] = b"\0" * 8
    data[linkedit + 48 : linkedit + 56] = b"\0" * 8
    return "sha256:" + hashlib.sha256(data[:data_offset]).hexdigest()


def _safe_relative_file(root: Path, candidate: Path) -> bool:
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return False
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        return False
    current = root
    try:
        root_metadata = root.lstat()
        if root.is_symlink() or not stat.S_ISDIR(root_metadata.st_mode):
            return False
        for part in relative.parts:
            current /= part
            metadata = current.lstat()
            if current.is_symlink():
                return False
        return stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1
    except OSError:
        return False


def _read_private_or_bundle_file(path: Path, maximum: int) -> bytes:
    """Read one bounded regular file through a stable no-follow descriptor."""

    if maximum < 1:
        raise ValueError("packaged macOS VZ asset read bound is invalid")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum:
            raise ValueError("packaged macOS VZ asset manifest is unsafe")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(descriptor)
        if len(data) > maximum or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError("packaged macOS VZ asset changed during read")
        return data
    finally:
        os.close(descriptor)


def _read_verified_bundle_file(path: Path, expected_digest: str, maximum: int) -> bytes:
    """Read and hash a signed bundle input through one no-follow descriptor."""

    if not _is_digest(expected_digest):
        raise ValueError("packaged macOS VZ asset digest is invalid")
    data = _read_private_or_bundle_file(path, maximum)
    if not hmac.compare_digest(_digest_bytes(data), expected_digest):
        raise ValueError("packaged macOS VZ asset changed")
    return data


def _ensure_private_directory(path: Path) -> None:
    if not path.is_absolute() or path != path.resolve(strict=False):
        raise ValueError("PackVM VZ managed directory is unsafe")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or (os.name == "posix" and metadata.st_mode & 0o077)
        or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
    ):
        raise ValueError("PackVM VZ managed directory is unsafe")
    if os.name == "posix":
        os.chmod(path, 0o700)


def _open_private_file(path: Path, flags: int) -> int:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), 0o600)
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_mode & 0o077
        or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
    ):
        os.close(descriptor)
        raise ValueError("PackVM VZ private file is unsafe")
    return descriptor


def _read_private_file(path: Path, maximum: int) -> bytes:
    descriptor = _open_private_file(path, os.O_RDONLY)
    try:
        metadata = os.fstat(descriptor)
        if metadata.st_size > maximum:
            raise ValueError("PackVM VZ private file exceeds its bound")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > maximum:
            raise ValueError("PackVM VZ private file exceeds its bound")
        after = os.fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError("PackVM VZ private file changed during read")
        return data
    finally:
        os.close(descriptor)


def _validate_private_file(path: Path, expected_size: int) -> None:
    """Check a large private sparse file without reading its entire payload."""

    descriptor = _open_private_file(path, os.O_RDONLY)
    try:
        metadata = os.fstat(descriptor)
        if metadata.st_size != expected_size or metadata.st_nlink != 1:
            raise ValueError("PackVM VZ private disk identity is invalid")
    finally:
        os.close(descriptor)


def _atomic_private_bytes(path: Path, payload: bytes) -> None:
    _ensure_private_directory(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(payload)
        while view:
            count = os.write(descriptor, view)
            if count <= 0:
                raise OSError("PackVM VZ private write failed")
            view = view[count:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _write_all(descriptor: int, payload: bytes) -> None:
    """Write one private pipe/file payload completely or fail closed."""

    view = memoryview(payload)
    while view:
        count = os.write(descriptor, view)
        if count <= 0:
            raise OSError("PackVM VZ private write failed")
        view = view[count:]


def _atomic_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_private_bytes(path, _canonical_bytes(payload) + b"\n")


def _validate_materialized_artifact(
    artifact: MaterializedPackArtifact,
    *,
    artifact_digest: str,
    executable_digest: str,
    materialization_digest: str,
) -> None:
    """Reject an artifact that differs from the Host-resolved launch binding."""

    from tobkiri_host.artifact_materialization import MaterializedPackArtifact

    if not isinstance(artifact, MaterializedPackArtifact):
        raise ValueError("PackVM VZ artifact payload is invalid")
    if (
        artifact.artifact_digest != artifact_digest
        or artifact.implementation_digest != executable_digest
        or artifact.materialization_digest != materialization_digest
    ):
        raise ValueError("PackVM VZ artifact payload binding changed")
    if sum(len(item.content) for item in artifact.files) > _MAX_ARTIFACT_SEED_PAYLOAD_BYTES:
        raise ValueError("PackVM VZ artifact payload exceeds the seed limit")


def _write_materialized_artifact_seed(
    path: Path,
    artifact: MaterializedPackArtifact,
) -> dict[str, object]:
    """Serialize already Host-verified Pack bytes into one bounded seed file.

    The compact binary framing deliberately avoids base64 expansion: a valid
    512 MiB Host artifact therefore remains admissible.  The guest replays the
    exact manifest and raw file stream into its normal materialization path.
    """

    encoded_manifest, total_payload = _materialized_artifact_seed_framing(artifact)
    total_size = len(_ARTIFACT_SEED_MAGIC) + 8 + len(encoded_manifest) + total_payload
    _ensure_private_directory(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    digest = hashlib.sha256()

    def write_hashed(data: bytes) -> None:
        _write_all(descriptor, data)
        digest.update(data)

    try:
        os.fchmod(descriptor, 0o600)
        write_hashed(_ARTIFACT_SEED_MAGIC)
        write_hashed(len(encoded_manifest).to_bytes(8, "big"))
        write_hashed(encoded_manifest)
        for item in artifact.files:
            if _digest_bytes(item.content) != item.digest:
                raise ValueError("PackVM VZ artifact bytes changed before seed creation")
            write_hashed(item.content)
        if os.fstat(descriptor).st_size != total_size:
            raise ValueError("PackVM VZ artifact seed size is invalid")
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        _validate_private_file(path, total_size)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return {
        "format": _ARTIFACT_SEED_FORMAT,
        "digest": _DIGEST_PREFIX + digest.hexdigest(),
        "size_bytes": total_size,
    }


def _materialized_artifact_seed_framing(
    artifact: MaterializedPackArtifact,
) -> tuple[bytes, int]:
    """Return the canonical manifest and payload size used by seed framing."""

    files: list[_ArtifactSeedFile] = [
        {
            "path": item.path,
            "digest": item.digest,
            "executable": item.executable,
            "size": len(item.content),
        }
        for item in artifact.files
    ]
    manifest = {
        "schema": _ARTIFACT_SEED_FORMAT,
        "pack_id": artifact.pack_id,
        "artifact_digest": artifact.artifact_digest,
        "function_id": artifact.function_id,
        "implementation_digest": artifact.implementation_digest,
        "implementation_path": artifact.implementation_path,
        "materialization_digest": artifact.materialization_digest,
        "files": files,
    }
    encoded_manifest = _canonical_bytes(manifest)
    if len(encoded_manifest) > _MAX_ARTIFACT_SEED_MANIFEST_BYTES:
        raise ValueError("PackVM VZ artifact seed manifest exceeds its bound")
    total_payload = sum(item["size"] for item in files)
    if total_payload > _MAX_ARTIFACT_SEED_PAYLOAD_BYTES:
        raise ValueError("PackVM VZ artifact seed payload exceeds its bound")
    return encoded_manifest, total_payload


def _materialized_artifact_seed_size(artifact: MaterializedPackArtifact) -> int:
    """Return exact artifact-seed bytes without writing allocation state."""

    encoded_manifest, total_payload = _materialized_artifact_seed_framing(artifact)
    return len(_ARTIFACT_SEED_MAGIC) + 8 + len(encoded_manifest) + total_payload


def _write_iso_seed(
    path: Path,
    volume_label: str,
    files: Mapping[str, bytes | Path],
) -> None:
    """Write a deterministic ISO9660 seed disk aligned to 2048/512 bytes.

    Cloud-init's NoCloud datasource recognizes ``cidata`` ISO volumes and the
    guest agent can mount the separate agent volume.  We build the narrow ISO
    subset ourselves to avoid a mutable host toolchain or a shell invocation.
    Every content byte is supplied by a verified template or generated for the
    exact VM ceremony.
    """

    if not files or len(files) > 16:
        raise ValueError("PackVM VZ ISO seed content is invalid")
    source_facts: dict[str, tuple[int, bytes | Path, str | None]] = {}
    for name, data in files.items():
        if (
            not isinstance(name, str)
            or not name
            or len(name.encode("ascii", errors="ignore")) != len(name)
            or len(name) > 96
            or "/" in name
            or "\x00" in name
        ):
            raise ValueError("PackVM VZ ISO seed content is invalid")
        if isinstance(data, bytes):
            source_facts[name] = (len(data), data, _digest_bytes(data))
        elif isinstance(data, Path):
            descriptor = _open_private_file(data, os.O_RDONLY)
            try:
                metadata = os.fstat(descriptor)
                if metadata.st_size < 0 or metadata.st_size > _MAX_ARTIFACT_SEED_BYTES:
                    raise ValueError("PackVM VZ ISO seed source exceeds its bound")
            finally:
                os.close(descriptor)
            source_facts[name] = (metadata.st_size, data, _file_digest(data))
        else:
            raise ValueError("PackVM VZ ISO seed content is invalid")
    block = 2048
    root_sector = 20
    root_blocks = 1
    file_sectors: dict[str, tuple[int, int]] = {}
    cursor = root_sector + root_blocks
    for name, (size, _data, _digest) in sorted(source_facts.items()):
        sectors = max(1, (size + block - 1) // block)
        file_sectors[name] = (cursor, sectors)
        cursor += sectors
    total = max(cursor, 32)

    def both_endian(image: bytearray, offset: int, value: int, width: int) -> None:
        image[offset : offset + width] = value.to_bytes(width, "little")
        image[offset + width : offset + 2 * width] = value.to_bytes(width, "big")

    def record(identifier: bytes, sector: int, size: int, flags: int) -> bytes:
        timestamp = bytes((126, 1, 1, 0, 0, 0, 0))
        length = 33 + len(identifier) + (len(identifier) % 2 == 0)
        result = bytearray(length)
        result[0] = length
        result[1] = 0
        result[2:6] = sector.to_bytes(4, "little")
        result[6:10] = sector.to_bytes(4, "big")
        result[10:14] = size.to_bytes(4, "little")
        result[14:18] = size.to_bytes(4, "big")
        result[18:25] = timestamp
        result[25] = flags
        result[26] = 0
        result[27] = 0
        result[28:30] = (1).to_bytes(2, "little")
        result[30:32] = (1).to_bytes(2, "big")
        result[32] = len(identifier)
        result[33 : 33 + len(identifier)] = identifier
        return bytes(result)

    primary = bytearray(block)
    primary[0] = 1
    primary[1:6] = b"CD001"
    primary[6] = 1
    primary[8:40] = b"TOBKIRI".ljust(32, b" ")
    primary[40:72] = volume_label.upper().encode("ascii").ljust(32, b" ")
    both_endian(primary, 80, total, 4)
    both_endian(primary, 120, 1, 2)
    both_endian(primary, 124, 1, 2)
    both_endian(primary, 128, block, 2)
    root_record = record(b"\x00", root_sector, root_blocks * block, 2)
    primary[156 : 156 + len(root_record)] = root_record
    terminator = bytearray(block)
    terminator[0] = 255
    terminator[1:6] = b"CD001"
    terminator[6] = 1
    root_directory = bytearray(block)
    entries = [
        record(b"\x00", root_sector, root_blocks * block, 2),
        record(b"\x01", root_sector, root_blocks * block, 2),
    ]
    for name, (size, _data, _digest) in sorted(source_facts.items()):
        sector, _sectors = file_sectors[name]
        entries.append(record((name + ";1").encode("ascii"), sector, size, 0))
    position = 0
    for entry in entries:
        if position + len(entry) > block:
            raise ValueError("PackVM VZ ISO seed directory exceeds its bound")
        root_directory[position : position + len(entry)] = entry
        position += len(entry)
    _ensure_private_directory(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        os.ftruncate(descriptor, total * block)

        def write_at(offset: int, content: bytes) -> None:
            os.lseek(descriptor, offset, os.SEEK_SET)
            _write_all(descriptor, content)

        write_at(16 * block, bytes(primary))
        write_at(17 * block, bytes(terminator))
        write_at(root_sector * block, bytes(root_directory))
        for name, (size, data, expected_digest) in sorted(source_facts.items()):
            sector, _sectors = file_sectors[name]
            os.lseek(descriptor, sector * block, os.SEEK_SET)
            if isinstance(data, bytes):
                _write_all(descriptor, data)
                continue
            source_descriptor = _open_private_file(data, os.O_RDONLY)
            try:
                before = os.fstat(source_descriptor)
                copied = 0
                digest = hashlib.sha256()
                while chunk := os.read(source_descriptor, min(1024 * 1024, size - copied)):
                    _write_all(descriptor, chunk)
                    digest.update(chunk)
                    copied += len(chunk)
                after = os.fstat(source_descriptor)
                if (
                    copied != size
                    or _DIGEST_PREFIX + digest.hexdigest() != expected_digest
                    or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                ):
                    raise ValueError("PackVM VZ ISO seed source changed while copying")
            finally:
                os.close(source_descriptor)
        if os.fstat(descriptor).st_size != total * block:
            raise ValueError("PackVM VZ ISO seed size is invalid")
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        _validate_private_file(path, total * block)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _clone_file_apfs(source: Path, target: Path) -> None:
    """Clone the verified raw image with APFS CoW; never silently byte-copy."""

    if host_platform.system() != "Darwin":
        raise ValueError("PackVM VZ APFS clonefile is unavailable")
    source_descriptor = _open_private_file(source, os.O_RDONLY)
    try:
        source_identity = os.fstat(source_descriptor)
        if source_identity.st_nlink != 1:
            raise ValueError("PackVM VZ APFS clone source is unsafe")
    finally:
        os.close(source_descriptor)
    if target.exists() or target.is_symlink():
        raise ValueError("PackVM VZ APFS clone target already exists")
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    clonefile = library.clonefile
    clonefile.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32)
    clonefile.restype = ctypes.c_int
    result = clonefile(os.fsencode(source), os.fsencode(target), ctypes.c_uint32(0))
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, "PackVM VZ APFS clonefile failed")
    os.chmod(target, 0o600)
    target_descriptor = _open_private_file(target, os.O_RDONLY)
    try:
        target_identity = os.fstat(target_descriptor)
        if target_identity.st_size != source_identity.st_size:
            raise ValueError("PackVM VZ APFS clone size differs from base")
    finally:
        os.close(target_descriptor)


def _validate_allocation_identifier(value: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or "\x00" in value
        or value != value.strip()
    ):
        raise ValueError(f"PackVM VZ allocation {label} is invalid")


def _safe_private_domain_root(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return (
        path.is_absolute()
        and not path.is_symlink()
        and stat.S_ISDIR(metadata.st_mode)
        and metadata.st_mode & 0o077 == 0
        and (not hasattr(os, "getuid") or metadata.st_uid == os.getuid())
    )


def _file_digest(path: Path) -> str:
    return _file_digest_algorithm(path, "sha256")


def _file_digest_algorithm(path: Path, algorithm: str) -> str:
    """Hash one no-follow regular file with replacement detection."""

    if algorithm not in {"sha256", "sha512"}:
        raise ValueError("PackVM VZ digest algorithm is invalid")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("PackVM VZ asset is not a regular unlinked-safe file")
        digest = hashlib.new(algorithm)
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError("PackVM VZ asset changed during verification")
        return f"{algorithm}:" + digest.hexdigest()
    finally:
        os.close(descriptor)


def _read_json_if_present(path: Path) -> dict[str, Any] | None:
    try:
        raw = _read_private_file(path, _MAX_STATE_BYTES)
    except FileNotFoundError:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("PackVM VZ private JSON is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("PackVM VZ private JSON is invalid")
    return value


def _try_lock(descriptor: int) -> None:
    if os.name != "posix":
        return
    import fcntl

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise ValueError("PackVM VZ lifecycle operation is already active") from exc


def _unlock(descriptor: int) -> None:
    if os.name == "posix":
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)


def _same_private_directory(root: Path, state: Mapping[str, Any]) -> bool:
    try:
        metadata = root.lstat()
    except OSError:
        return False
    return (
        root.is_absolute()
        and not root.is_symlink()
        and stat.S_ISDIR(metadata.st_mode)
        and metadata.st_mode & 0o077 == 0
        and int(state.get("instance_root_device", -1)) == int(metadata.st_dev)
        and int(state.get("instance_root_inode", -1)) == int(metadata.st_ino)
    )


def _recovery_fields() -> tuple[str, ...]:
    return (
        "backend_id",
        "instance",
        "session_digest",
        "plan_digest",
        "ceremony_nonce_digest",
        "config_digest",
        "image_digest",
        "guest_runner_digest",
        "host_build_digest",
        "vz_state_root_digest",
        "vz_state_root_device",
        "vz_state_root_inode",
        "vz_provisioner_digest",
    )


def _recovery_binding(proof: Mapping[str, Any]) -> dict[str, str]:
    fields = ("session_digest", "plan_digest", "ceremony_nonce_digest")
    result: dict[str, str] = {}
    for field in fields:
        value = proof.get(field)
        if not isinstance(value, str):
            raise ValueError("PackVM VZ recovery proof is incomplete")
        result[field] = value
    return result


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _claim_binding_equal(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    fields = ("version", "operation", "instance", "binding")
    return hmac.compare_digest(
        _canonical_bytes({field: left.get(field) for field in fields}),
        _canonical_bytes({field: right.get(field) for field in fields}),
    )


def _process_is_alive(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return False
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _digest_bytes(value: bytes) -> str:
    return _DIGEST_PREFIX + hashlib.sha256(value).hexdigest()


def _digest_text(value: str) -> str:
    return _digest_bytes(value.encode("utf-8"))


def _canonical_digest(value: object) -> str:
    return _digest_bytes(_canonical_bytes(value))


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith(_DIGEST_PREFIX)
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _is_sha512(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 135
        and value.startswith("sha512:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _zero_digest() -> str:
    return _DIGEST_PREFIX + "0" * 64


def _format_gib(value: int) -> str:
    return f"{value / (1024**3):.2f} GiB"


def _secure_equal(left: object, right: object) -> bool:
    if isinstance(left, str) and isinstance(right, str):
        return hmac.compare_digest(left, right)
    if (
        isinstance(left, int)
        and not isinstance(left, bool)
        and isinstance(right, int)
        and not isinstance(right, bool)
    ):
        return hmac.compare_digest(str(left).encode(), str(right).encode())
    return False


__all__ = [
    "MacOSVZAssetManifest",
    "MacOSVZProvisionedFacts",
    "MacOSVZProvisioner",
    "MacOSVZTransportFactory",
    "VZ_ASSET_MANIFEST_SCHEMA",
    "VZ_INSTANCE",
    "VZ_RAW_EFI_IMAGE_DECLARED_BYTES",
    "default_packvm_provisioner",
]
