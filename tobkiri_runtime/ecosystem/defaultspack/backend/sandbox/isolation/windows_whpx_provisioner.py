"""Consent-bound Windows QEMU/WHPX provisioning and private per-domain assets.

The release loader needs Launcher-sealed asset identity. Development callers
may pass an explicit build root and expected manifest hash. Missing WHPX,
untrusted assets, altered state and orphaned allocations all fail closed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import stat
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from core_runtime.hmac_key_manager import generate_or_load_signing_key
from core_runtime.process_identity import process_start_identity
from tobkiri_host.sparse_copy import copy_verified_stream
from tobkiri_host.windows_whpx_probe import whpx_capability
from tobkiri_host.windows_whpx_security import private_directory, stable_file
from tobkiri_protocol.canonical import canonical_digest, canonical_json
from tobkiri_protocol.secure_persistence import SecureDirectory

from .lima_runtime import PackVMDoctor, PackVMProvisioningPlan
from .macos_vz_provisioner import (
    _NOCLOUD_LOCAL_ONLY_NETWORK_CONFIG,
    PackVMGateBusyError,
    _validate_materialized_artifact,
)
from .windows_whpx_assets import WindowsWHPXAssets, file_digest, load_windows_whpx_assets
from .windows_whpx_seeds import _write_iso_seed, _write_materialized_artifact_seed

INSTANCE = "tobkiri-packvm-v4"
BACKEND = "tobkiri.python-pack-v4"
PLATFORM = "windows-amd64"
_RESERVE = 1536 * 1024 * 1024


@dataclass(frozen=True)
class WindowsWHPXProvisionedFacts:
    """Authenticated immutable Windows registration consumed by composition."""

    assets: WindowsWHPXAssets
    provisioner: WindowsWHPXProvisioner

    def build_backend(self) -> Any:
        """Construct the exact supported WHPX adapter, rechecking its evidence."""
        from tobkiri_host.macos_vz_supervisor import (
            MacOSVZAgentIdentity,
            MacOSVZHelperIdentity,
        )
        from tobkiri_host.platform_backends import WindowsWHPXBackend
        from tobkiri_host.windows_whpx_models import WindowsWHPXLaunchAssets
        from tobkiri_host.windows_whpx_supervisor import WindowsWHPXSupervisorDriver

        files = self.assets.files
        return WindowsWHPXBackend(
            WindowsWHPXSupervisorDriver(
                assets=self.assets,
                transport_factory=self.provisioner.transport_for_allocation,
                helper_path=files["qemu"].path,
                helper_identity=MacOSVZHelperIdentity(
                    files["qemu"].digest, "io.tobkiri.packvm.qemu", "", ""
                ),
                launch_assets=WindowsWHPXLaunchAssets(
                    files["image"].digest,
                    str(files["image"].path),
                    files["agent"].digest,
                    files["config"].digest,
                    True,
                ),
                agent_identity=MacOSVZAgentIdentity(files["agent"].digest),
                domain_allocator=self.provisioner,
            )
        )


class WindowsWHPXProvisioner:
    """Provision one approved bundle; allocate one real WHPX VM per domain."""

    runtime_surface_attestation_supported = False

    def __init__(
        self,
        *,
        state_dir: Path | None = None,
        bundle_root: Path | None = None,
        expected_manifest_digest: str | None = None,
    ) -> None:
        self._root = state_dir or (Path.home() / "AppData/Local/Tobkiri/packvm-whpx")
        if not self._root.is_absolute() or self._root.resolve() != self._root:
            raise ValueError("Windows PackVM state root must be absolute without symlinks")
        self._bundle_root = bundle_root
        self._expected_digest = expected_manifest_digest
        self._pending: dict[str, PackVMProvisioningPlan] = {}
        self._lock = threading.RLock()
        self._gate_local = threading.local()
        self._transports: dict[str, Any] = {}
        self._claimed: set[str] = set()

    @property
    def state_path(self) -> Path:
        """Return the private authenticated lifecycle record."""
        return self._root / "packvm-qemu-attestation.json"

    def _storage(self) -> SecureDirectory:
        return private_directory(self._root)

    @contextmanager
    def operation_gate(
        self, operation: str, binding: Mapping[str, Any], **_options: Any
    ) -> Iterator[None]:
        """Serialize processes and threads without stealing an active lease."""
        del operation, binding
        with self._lock:
            depth = getattr(self._gate_local, "depth", 0)
            if depth:
                self._gate_local.depth = depth + 1
                try:
                    yield
                finally:
                    self._gate_local.depth -= 1
                return
            self._storage()
            # The root DACL is owner-only before this lock or any secrets exist.
            path = self._root / "mutation.lock"
            fd = os.open(
                path,
                os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("Windows PackVM mutation lock is unsafe")
                with _exclusive_file_lock(fd):
                    self._gate_local.depth = 1
                    try:
                        yield
                    finally:
                        self._gate_local.depth = 0
            finally:
                os.close(fd)

    def recovery_identity(self) -> dict[str, int | str]:
        """Bind lifecycle recovery to this exact private directory and code."""
        self._storage()
        info = self._root.lstat()
        return {
            "qemu_state_root_digest": _text_digest(str(self._root)),
            "qemu_state_root_device": info.st_dev,
            "qemu_state_root_inode": info.st_ino,
            "qemu_provisioner_digest": file_digest(Path(__file__).resolve()),
        }

    def _assets(self) -> WindowsWHPXAssets:
        if self._bundle_root is None or self._expected_digest is None:
            raise ValueError(
                "Windows PackVM requires a Launcher-verified VM bundle; this build has none"
            )
        return load_windows_whpx_assets(self._bundle_root, self._expected_digest)

    def prepare(self) -> PackVMProvisioningPlan:
        """Display exact assets/storage and WHPX blockers without starting a VM."""
        ready, reason = whpx_capability()
        assets = None
        try:
            assets = self._assets()
        except (OSError, ValueError) as exc:
            reason = reason or str(exc)
        del ready
        zero = "sha256:" + "0" * 64
        files = assets.files if assets else {}
        size = files["image"].size_bytes if assets else 0
        available = shutil.disk_usage(
            self._root
            if self._root.exists()
            else self._root.parent
            if self._root.parent.exists()
            else Path.home()
        ).free
        required = size + _RESERVE
        space_reason = (
            None
            if available >= required
            else "Windows PackVM needs more free space for a private VM disk and seeds"
        )
        reason = reason or space_reason
        update = None
        if assets is not None and (self.state_path.exists() or self.state_path.is_symlink()):
            try:
                previous = self._state(allow_stopped=True, allow_asset_drift=True)
                if any(transport.alive() for transport in self._transports.values()):
                    reason = (
                        reason or "Stop the running Windows PackVM before updating its registration"
                    )
                else:
                    update = {
                        "previous_attestation_digest": previous["attestation_digest"],
                        "previous_config_digest": previous["config_digest"],
                        "previous_guest_runner_digest": previous["guest_runner_digest"],
                        "previous_host_build_digest": previous["host_build_digest"],
                    }
            except (OSError, ValueError, KeyError):
                reason = reason or "Windows PackVM existing registration requires recovery"
        nonce = secrets.token_hex(16)
        facts = {
            "bundle": assets.manifest_digest if assets else zero,
            "platform": PLATFORM,
            "accelerator": "whpx",
            "state": str(self._root),
            "required_space": required,
            "nonce": nonce,
            "registration_update": update,
        }
        digest = canonical_digest(facts)
        plan = PackVMProvisioningPlan(
            BACKEND,
            INSTANCE,
            None,
            reason,
            "amd64",
            assets.image_source if assets else "unavailable",
            files["image"].digest if assets else zero,
            size,
            False,
            0,
            "verified_source" if assets else "unsafe",
            reason,
            size,
            required,
            available,
            space_reason,
            files["config"].digest if assets else zero,
            files["agent"].digest if assets else zero,
            files["qemu"].digest if assets else zero,
            _text_digest(str(self._root)),
            "ready" if reason is None else "unsafe",
            reason,
            nonce,
            digest,
            f"PROVISION {INSTANCE} {digest[7:19]}",
            registration_update=update,
        )
        with self._lock:
            self._pending.clear()
            self._pending[nonce] = plan
        return plan

    def provision(
        self,
        request: Any,
        *,
        progress: Callable[..., Any] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> PackVMDoctor:
        """Consume exact consent and persist authenticated bundle registration."""
        del progress
        with self.operation_gate("provision", {}):
            plan = self._pending.pop(request.ceremony_nonce, None)
            if (
                plan is None
                or not hmac.compare_digest(plan.plan_digest, request.plan_digest)
                or not hmac.compare_digest(plan.confirmation, request.confirmation)
            ):
                raise ValueError("Windows PackVM consent is invalid or already consumed")
            if plan.launcher_reason:
                raise ValueError(plan.launcher_reason)
            if request.storage_rebind_digest is not None:
                raise ValueError("Windows PackVM storage rebinding is not supported")
            if plan.registration_update is None and request.previous_attestation_digest is not None:
                raise ValueError("Windows PackVM create consent cannot update a registration")
            if cancelled and cancelled():
                raise ValueError("Windows PackVM provisioning was cancelled")
            if plan.registration_update is not None:
                previous = self._state(allow_stopped=True, allow_asset_drift=True)
                if (
                    request.previous_attestation_digest != previous["attestation_digest"]
                    or plan.registration_update["previous_attestation_digest"]
                    != previous["attestation_digest"]
                ):
                    raise ValueError(
                        "Windows PackVM registration update consent is stale or missing"
                    )
                if any(transport.alive() for transport in self._transports.values()):
                    raise ValueError("Windows PackVM registration cannot change while a VM is live")
                for root, transport in list(self._transports.items()):
                    transport.close()
                    _remove_allocation(Path(root))
                    self._transports.pop(root)
                    self._claimed.discard(root)
                self._sweep_dead_allocations()
            elif self.state_path.exists() or self.state_path.is_symlink():
                raise ValueError("Windows PackVM is already registered; review a new plan")
            assets = self._assets()
            if (
                assets.files["image"].digest != plan.image_digest
                or assets.files["qemu"].digest != plan.host_build_digest
                or assets.files["agent"].digest != plan.guest_runner_digest
                or assets.files["config"].digest != plan.config_digest
            ):
                raise ValueError("Windows PackVM approved assets changed")
            ready, reason = whpx_capability()
            if not ready:
                raise ValueError(reason)
            self._write_state(
                {
                    "version": 1,
                    "backend_id": BACKEND,
                    "platform": PLATFORM,
                    "instance": INSTANCE,
                    "bundle_digest": assets.manifest_digest,
                    "plan_digest": plan.plan_digest,
                    "ceremony_nonce_digest": _text_digest(plan.ceremony_nonce),
                    "config_digest": plan.config_digest,
                    "executed_config_digest": plan.config_digest,
                    "image_digest": plan.image_digest,
                    "guest_runner_digest": plan.guest_runner_digest,
                    "host_build_digest": plan.host_build_digest,
                    "stopped": False,
                    "session_digest": request.session_digest
                    or _text_digest("direct-local-lifecycle"),
                    "operation_id": request.operation_id or "direct-local-lifecycle",
                    "created_unix": int(time.time()),
                    **(
                        {"previous_attestation_digest": request.previous_attestation_digest}
                        if request.previous_attestation_digest is not None
                        else {}
                    ),
                    **self.recovery_identity(),
                }
            )
            return self.doctor()

    def _write_state(self, value: Mapping[str, Any]) -> None:
        storage = self._storage()
        key = generate_or_load_signing_key(self._root / "state.key")
        core = dict(value)
        core.pop("authentication", None)
        core.pop("attestation_digest", None)
        core["attestation_digest"] = canonical_digest(core)
        state = {
            **core,
            "authentication": hmac.new(key, canonical_json(core), hashlib.sha256).hexdigest(),
        }
        storage.write_bytes_atomic(self.state_path.name, canonical_json(state))

    def _state(
        self, *, allow_stopped: bool = False, allow_asset_drift: bool = False
    ) -> dict[str, Any]:
        storage = self._storage()
        raw = storage.read_bytes_bounded(self.state_path.name, max_bytes=128 * 1024)
        state = json.loads(raw)
        if not isinstance(state, dict):
            raise ValueError("Windows PackVM state is invalid")  # noqa: TRY004 - invalid persisted JSON
        auth = state.pop("authentication", None)
        key = storage.read_bytes_bounded("state.key", max_bytes=128)
        if not isinstance(auth, str) or not hmac.compare_digest(
            auth, hmac.new(key, canonical_json(state), hashlib.sha256).hexdigest()
        ):
            raise ValueError("Windows PackVM state authentication failed")
        digest = state.pop("attestation_digest", None)
        if digest != canonical_digest(state):
            raise ValueError("Windows PackVM state digest changed")
        state["attestation_digest"] = digest
        if (
            state.get("version") != 1
            or state.get("backend_id") != BACKEND
            or state.get("platform") != PLATFORM
            or state.get("instance") != INSTANCE
            or any(
                state.get(key) != val
                for key, val in self.recovery_identity().items()
                if key != "qemu_provisioner_digest"
            )
        ):
            raise ValueError("Windows PackVM registration identity changed")
        if not allow_stopped and state.get("stopped") is not False:
            raise ValueError("Windows PackVM is administratively stopped")
        if not allow_asset_drift and state.get("bundle_digest") != self._assets().manifest_digest:
            raise ValueError("Windows PackVM registered assets changed; review a new plan")
        return state

    def doctor(self) -> PackVMDoctor:
        """Never promote a WHPX-less host or unauthenticated registration."""
        ready, reason = whpx_capability()
        if not ready:
            return PackVMDoctor(False, BACKEND, PLATFORM, INSTANCE, reason)
        try:
            state = self._state()
            domains = self._root / "domains"
            if domains.exists():
                for path in domains.iterdir():
                    if str(path) not in self._transports and not self._dead_allocation(path):
                        raise ValueError(
                            "Windows PackVM allocation owner is live or cannot be verified"
                        )
            return PackVMDoctor(
                True, BACKEND, PLATFORM, INSTANCE, attestation_digest=state["attestation_digest"]
            )
        except (OSError, ValueError) as exc:
            return PackVMDoctor(False, BACKEND, PLATFORM, INSTANCE, str(exc))

    def readiness_snapshot(self) -> dict[str, Any]:
        """Return diagnostics; each real operation still needs signed guest evidence."""
        from dataclasses import asdict

        return {
            **asdict(self.doctor()),
            "observed_unix": int(time.time()),
            "backend_substrate": "windows-whpx",
            "accelerator": "whpx",
        }

    def production_backend_registration(self) -> WindowsWHPXProvisionedFacts | None:
        """Expose typed Windows facts only after full local integrity validation."""
        if not self.doctor().ready:
            return None
        return WindowsWHPXProvisionedFacts(self._assets(), self)

    def allocate(
        self,
        *,
        domain_id: str,
        reservation_id: str,
        lease_id: str,
        artifact_digest: str,
        executable_digest: str,
        materialization_digest: str,
        artifact: Any,
        channel_key: bytes,
    ) -> Any:
        """Create fresh private disk/UEFI state/key/seeds, without launching yet."""
        from tobkiri_host.windows_whpx_models import WindowsWHPXDomainAllocation
        from tobkiri_host.windows_whpx_process import WindowsWHPXLaunchConfig, WindowsWHPXProcess
        from tobkiri_host.windows_whpx_supervisor import WindowsWHPXSupervisorTransport

        for identifier in (domain_id, reservation_id, lease_id):
            if (
                not isinstance(identifier, str)
                or not identifier
                or len(identifier) > 128
                or any(
                    c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.:"
                    for c in identifier
                )
            ):
                raise ValueError("Windows PackVM allocation identifier is invalid")
        _validate_materialized_artifact(
            artifact,
            artifact_digest=artifact_digest,
            executable_digest=executable_digest,
            materialization_digest=materialization_digest,
        )
        with self.operation_gate("allocate", {}):
            self._state()
            self._sweep_dead_allocations()
            assets = self._assets()
            if shutil.disk_usage(self._root).free < assets.files["image"].size_bytes + _RESERVE:
                raise ValueError("Windows PackVM lacks private allocation space")
            domains = self._root / "domains"
            private_directory(domains)
            root = domains / _text_digest(f"{domain_id}\0{reservation_id}\0{lease_id}")[7:39]
            if root.exists() or root.is_symlink():
                raise ValueError("Windows PackVM domain allocation already exists")
            private_directory(root)
            owner = process_start_identity(os.getpid())
            if owner.state != "live":
                root.rmdir()
                raise ValueError("Windows PackVM cannot prove its allocation owner identity")
            info = root.lstat()
            claim = {
                "version": 1,
                "domain_id": domain_id,
                "reservation_id": reservation_id,
                "lease_id": lease_id,
                "executable_digest": executable_digest,
                "owner_pid": os.getpid(),
                "owner_identity": owner.identity,
                "root": str(root),
                "root_device": info.st_dev,
                "root_inode": info.st_ino,
            }
            self._write_claim(root, claim)
            transport = None
            try:
                disk, variables = root / "boot.raw", root / "efi-vars.fd"
                _copy_private(assets.files["image"].path, disk, assets.files["image"].digest)
                _copy_private(
                    assets.files["firmware_vars"].path,
                    variables,
                    assets.files["firmware_vars"].digest,
                )
                seed = _make_seeds(
                    root,
                    domain_id,
                    reservation_id,
                    lease_id,
                    disk,
                    variables,
                    assets,
                    artifact,
                    artifact_digest,
                    executable_digest,
                    materialization_digest,
                )
                allocation = WindowsWHPXDomainAllocation(
                    domain_id,
                    reservation_id,
                    lease_id,
                    str(root),
                    str(disk),
                    file_digest(disk),
                    str(variables),
                    file_digest(variables),
                    str(root / "agent.iso"),
                    file_digest(root / "agent.iso"),
                    str(root / "config.iso"),
                    file_digest(root / "config.iso"),
                    seed,
                )
                process = WindowsWHPXProcess(
                    WindowsWHPXLaunchConfig(
                        qemu_path=assets.files["qemu"].path,
                        qemu_digest=assets.files["qemu"].digest,
                        run_root=root,
                        private_disk_path=disk,
                        agent_image_path=root / "agent.iso",
                        seed_image_path=root / "config.iso",
                        firmware_code_path=assets.files["firmware_code"].path,
                        private_firmware_vars_path=variables,
                        firmware_code_digest=assets.files["firmware_code"].digest,
                        private_disk_digest=allocation.cow_disk_digest,
                        agent_image_digest=allocation.agent_seed_digest,
                        seed_image_digest=allocation.config_seed_digest,
                        private_firmware_vars_digest=allocation.efi_variable_store_digest,
                        immutable_assets=tuple(
                            (item.path, item.digest)
                            for item in (*assets.files.values(), *assets.dependencies)
                        ),
                    )
                )
                transport = WindowsWHPXSupervisorTransport(
                    process=process, allocation=allocation, assets=assets, channel_key=channel_key
                )
                SecureDirectory(root).write_bytes_atomic(
                    "allocation.json", canonical_json(allocation.to_dict())
                )
                self._transports[str(root)] = transport
                return allocation
            except Exception:
                if transport is not None:
                    transport.close()
                _remove_allocation(root)
                raise

    def transport_for_allocation(self, allocation: Any) -> Any:
        """Transfer one exact private VM channel to the driver once."""
        with self._lock:
            if allocation.run_root in self._claimed:
                return None
            transport = self._transports.get(allocation.run_root)
            if transport is not None:
                self._claimed.add(allocation.run_root)
            return transport

    def release(self, allocation: Any) -> None:
        """Remove only a stopped, exact currently owned domain allocation."""
        with self.operation_gate("release", {}):
            root = Path(allocation.run_root)
            if root.parent != self._root / "domains":
                raise ValueError("Windows PackVM cleanup escaped the domain root")
            record = json.loads(
                SecureDirectory(root).read_bytes_bounded("allocation.json", max_bytes=128 * 1024)
            )
            if record != allocation.to_dict():
                raise ValueError("Windows PackVM cleanup identity changed")
            transport = self._transports.get(str(root))
            if transport is None:
                raise ValueError("Windows PackVM allocation is not owned by this process")
            transport.close()
            _remove_allocation(root)
            self._transports.pop(str(root), None)
            self._claimed.discard(str(root))

    def stop(self, confirmation: str) -> None:
        """Stop owned VMs and seal the stopped registration after exact consent."""
        if confirmation != f"STOP {INSTANCE}":
            raise ValueError("Windows PackVM stop confirmation does not match")
        with self.operation_gate("stop", {}):
            state = self._state(allow_stopped=True, allow_asset_drift=True)
            for transport in self._transports.values():
                transport.close()
            state["stopped"] = True
            self._write_state(state)

    def cleanup(self, confirmation: str) -> None:
        """Clean stopped owned state; unknown crash residue is retained safely."""
        if confirmation != f"DELETE {INSTANCE}":
            raise ValueError("Windows PackVM cleanup confirmation does not match")
        with self.operation_gate("cleanup", {}):
            self._state(allow_stopped=True, allow_asset_drift=True)
            domains = self._root / "domains"
            if domains.exists():
                for root in domains.iterdir():
                    transport = self._transports.get(str(root))
                    if transport is None:
                        if not self._dead_allocation(root):
                            raise ValueError(
                                "Windows PackVM interrupted resources need host recovery before cleanup"
                            )
                    else:
                        transport.close()
                    _remove_allocation(root)
            self._transports.clear()
            self._claimed.clear()
            self.state_path.unlink()

    def recover_provision_operation(self, expected_proof: Mapping[str, Any]) -> PackVMDoctor:
        """Reconcile only the exact authenticated completed provisioning record."""
        state = self._state()
        required = {
            "backend_id",
            "instance",
            "session_digest",
            "plan_digest",
            "ceremony_nonce_digest",
            "config_digest",
            "executed_config_digest",
            "image_digest",
            "guest_runner_digest",
            "host_build_digest",
            *self.recovery_identity(),
        }
        if "previous_attestation_digest" in state:
            required.add("previous_attestation_digest")
        if set(expected_proof) != required or any(
            state.get(key) != expected_proof[key] for key in required
        ):
            raise ValueError("Windows PackVM recovery proof does not match")
        return self.doctor()

    def cleanup_failed_provision(
        self, confirmation: str, expected_proof: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Reconcile a no-VM failed prepare only when exact private-root proof matches."""
        if confirmation != f"DELETE {INSTANCE}":
            raise ValueError("Windows PackVM failed cleanup confirmation does not match")
        with self.operation_gate("failed_cleanup", {}):
            if (
                expected_proof.get("backend_id") != BACKEND
                or expected_proof.get("instance") != INSTANCE
                or any(
                    expected_proof.get(key) != value
                    for key, value in self.recovery_identity().items()
                )
            ):
                raise ValueError("Windows PackVM failed cleanup proof does not match")
            domains = self._root / "domains"
            if (
                self.state_path.exists()
                or self.state_path.is_symlink()
                or (domains.exists() and any(domains.iterdir()))
            ):
                raise ValueError("Windows PackVM failed cleanup found resources requiring recovery")
            return {"missing": True}

    def _write_claim(self, root: Path, claim: Mapping[str, Any]) -> None:
        key = self._storage().read_bytes_bounded("state.key", max_bytes=128)
        core = dict(claim)
        signed = {
            **core,
            "authentication": hmac.new(key, canonical_json(core), hashlib.sha256).hexdigest(),
        }
        SecureDirectory(root).write_bytes_atomic("claim.json", canonical_json(signed))

    def _allocation_claim(self, root: Path) -> dict[str, Any]:
        if root.parent != self._root / "domains":
            raise ValueError("Windows PackVM recovery path escaped its allocation root")
        storage = private_directory(root, create=False)
        claim = json.loads(storage.read_bytes_bounded("claim.json", max_bytes=128 * 1024))
        if not isinstance(claim, dict):
            raise ValueError("Windows PackVM allocation claim is invalid")  # noqa: TRY004 - invalid persisted JSON
        auth = claim.pop("authentication", None)
        key = self._storage().read_bytes_bounded("state.key", max_bytes=128)
        info = root.lstat()
        if (
            not isinstance(auth, str)
            or not hmac.compare_digest(
                auth, hmac.new(key, canonical_json(claim), hashlib.sha256).hexdigest()
            )
            or claim.get("root") != str(root)
            or claim.get("root_device") != info.st_dev
            or claim.get("root_inode") != info.st_ino
            or claim.get("version") != 1
            or type(claim.get("owner_pid")) is not int
            or claim["owner_pid"] <= 0
            or not isinstance(claim.get("owner_identity"), str)
            or not claim["owner_identity"]
        ):
            raise ValueError("Windows PackVM allocation recovery authentication failed")
        return claim

    def _dead_allocation(self, root: Path) -> bool:
        try:
            claim = self._allocation_claim(root)
        except (OSError, ValueError):
            return False
        owner = process_start_identity(claim["owner_pid"])
        return owner.state == "dead" or (
            owner.state == "live" and owner.identity != claim["owner_identity"]
        )

    def _sweep_dead_allocations(self) -> None:
        domains = self._root / "domains"
        if not domains.exists():
            return
        for root in domains.iterdir():
            if str(root) not in self._transports:
                if not self._dead_allocation(root):
                    raise ValueError("Windows PackVM allocation owner is live or unknown")
                _remove_allocation(root)

    def recover_interrupted_allocation(
        self, *, domain_id: str, reservation_id: str, executable_digest: str
    ) -> bool:
        """Reclaim only signed resources whose exact owning process is proven dead."""
        with self.operation_gate("recover_allocation", {}):
            domains = self._root / "domains"
            if not domains.exists():
                return False
            recovered = False
            for root in domains.iterdir():
                claim = self._allocation_claim(root)
                if (
                    claim["domain_id"] == domain_id
                    and claim["reservation_id"] == reservation_id
                    and claim["executable_digest"] == executable_digest
                ):
                    if not self._dead_allocation(root):
                        return False
                    _remove_allocation(root)
                    recovered = True
            return recovered


def _text_digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _read_asset(asset: Any, maximum: int) -> bytes:
    with stable_file(asset.path) as source:
        content = source.read(maximum + 1)
    if len(content) > maximum or "sha256:" + hashlib.sha256(content).hexdigest() != asset.digest:
        raise ValueError("Windows PackVM bounded asset identity changed")
    return content


def _copy_private(source: Path, target: Path, expected: str) -> None:
    fd = os.open(
        target,
        os.O_CREAT
        | os.O_EXCL
        | os.O_RDWR
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0),
        0o600,
    )
    try:
        with os.fdopen(fd, "w+b") as output, stable_file(source) as incoming:
            before = os.fstat(incoming.fileno())
            copy_verified_stream(
                incoming, output, expected_digest=expected,
                size_bytes=before.st_size, sparse=target.name == "boot.raw",
            )
            after = os.fstat(incoming.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise ValueError("Windows PackVM immutable source changed during copy")
    except Exception:
        target.unlink(missing_ok=True)
        raise


def _remove_allocation(root: Path) -> None:
    private_directory(root, create=False)
    allowed = {
        "boot.raw",
        "efi-vars.fd",
        "agent.iso",
        "config.iso",
        "artifact-seed.v1.bin",
        "allocation.json",
        "claim.json",
    }
    entries = list(root.iterdir())
    if any(
        path.name not in allowed
        or not stat.S_ISREG(path.lstat().st_mode)
        or getattr(path.lstat(), "st_file_attributes", 0) & 0x400
        or path.lstat().st_nlink != 1
        for path in entries
    ):
        raise ValueError("Windows PackVM cleanup found unsafe or unexpected residue")
    for path in entries:
        path.unlink()
    root.rmdir()


def _make_seeds(
    root: Path,
    domain: str,
    reservation: str,
    lease: str,
    disk: Path,
    variables: Path,
    assets: WindowsWHPXAssets,
    artifact: Any,
    artifact_digest: str,
    executable_digest: str,
    materialization_digest: str,
) -> bytes:
    files = assets.files
    _write_iso_seed(
        root / "agent.iso",
        "TOBKIRIAGENT",
        {
            "runner.py": _read_asset(files["agent"], 8 * 1024 * 1024),
            "guest_service_template.v1.json": _read_asset(files["service"], 128 * 1024),
            "bubblewrap_amd64.deb": _read_asset(files["bubblewrap"], 16 * 1024 * 1024),
            "bubblewrap_descriptor.v1.json": _read_asset(
                files["bubblewrap_descriptor"], 128 * 1024
            ),
        },
    )
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    bindings = {
        "domain": _text_digest(domain),
        "lease": _text_digest(lease),
        "reservation": _text_digest(reservation),
        "image": files["image"].digest,
        "agent": files["agent"].digest,
        "config": files["config"].digest,
        "disk": file_digest(disk),
        "efi_variable_store": file_digest(variables),
        "guest_public_key": "sha256:" + hashlib.sha256(public).hexdigest(),
        "artifact": artifact_digest,
        "executable": executable_digest,
        "materialization": materialization_digest,
    }
    artifact_path = root / "artifact-seed.v1.bin"
    artifact_binding = _write_materialized_artifact_seed(artifact_path, artifact)
    config = {
        "version": 1,
        "domain_id": domain,
        "binding_digests": bindings,
        "private_key_path": "/run/tobkiri-packvm/agent-ed25519.pem",
        "artifact_seed": artifact_binding,
    }
    try:
        _write_iso_seed(
            root / "config.iso",
            "CIDATA",
            {
                "user-data": _read_asset(files["config"], 128 * 1024),
                "meta-data": f"instance-id: {domain}\nlocal-hostname: tobkiri-packvm\n".encode(),
                "network-config": _NOCLOUD_LOCAL_ONLY_NETWORK_CONFIG,
                "agent-ed25519.pem": key.private_bytes(
                    Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
                ),
                "agent-config.json": canonical_json(config),
                "artifact-seed.v1.bin": artifact_path,
            },
        )
    finally:
        artifact_path.unlink(missing_ok=True)
    return public


def default_windows_packvm_provisioner() -> WindowsWHPXProvisioner:
    """Select explicit development assets, or fail closed without sealed release assets."""
    if os.environ.get("RUMI_ENVIRONMENT") == "development":
        root = Path(os.environ.get("RUMI_USER_DATA", ""))
        if not root.is_absolute():
            raise ValueError("development PackVM requires an absolute Launcher user-data root")
        bundle = os.environ.get("TOBKIRI_DEVELOPMENT_PACKVM_BUNDLE_ROOT", "").strip()
        digest = os.environ.get("TOBKIRI_DEVELOPMENT_PACKVM_BUNDLE_SHA256", "").strip()
        return WindowsWHPXProvisioner(
            state_dir=root / "packvm-qemu",
            bundle_root=Path(bundle) if bundle else None,
            expected_manifest_digest=digest or None,
        )
    from core_runtime.packaged_application_bundle import (
        PortablePackVMBundleBinding,
        packvm_bundle_binding,
    )

    binding = packvm_bundle_binding()
    if type(binding) is not PortablePackVMBundleBinding or binding.platform != "windows":
        return WindowsWHPXProvisioner()
    return WindowsWHPXProvisioner(
        bundle_root=binding.root, expected_manifest_digest=binding.provisioning_sha256
    )


@contextmanager
def _exclusive_file_lock(fd: int) -> Iterator[None]:
    """Nonblocking native lock, without stealing another lifecycle operation."""
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise PackVMGateBusyError("Windows PackVM lifecycle operation is busy") from exc
        try:
            yield
        finally:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        # Cross-platform deterministic tests only. The production capability
        # check above never admits a non-Windows host to this provisioner.
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PackVMGateBusyError("Windows PackVM lifecycle operation is busy") from exc
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
