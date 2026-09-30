"""Linux KVM transport for the common authenticated PackVM guest protocol.

The v1 wire names contain ``macos-vz`` for compatibility. They do not assert
macOS provenance: platform, substrate, ELF, firmware and bundle identities are
independently bound to Linux. QEMU never receives the Host channel MAC secret;
only the isolated root guest agent receives its fresh Ed25519 private key.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ecosystem.defaultspack.backend.sandbox.isolation.linux_qemu_assets import (
    LinuxQemuAssets,
    kvm_capability,
)
from tobkiri_protocol.canonical import canonical_digest

from .macos_vz_supervisor import MacOSVZHelperIdentity, MacOSVZSupervisorDriver
from .qemu_supervisor_transport import QemuRuntime, QemuSupervisorTransport


class LinuxQemuSupervisorDriver(MacOSVZSupervisorDriver):
    """Reuse guest verification and capability mediation, with Linux evidence."""

    substrate_id = "linux-qemu"
    _platform_prefix = "linux-"

    def __init__(self, *, assets: LinuxQemuAssets, **kwargs: Any) -> None:
        self._linux_assets = assets
        if "identity_verifier" in kwargs or "platform" in kwargs or "runtime" in kwargs:
            raise ValueError("Linux PackVM identity verification cannot be overridden")
        super().__init__(
            platform="linux-amd64",
            identity_verifier=self._verify_linux,
            runtime=QemuRuntime("kvm"),
            **kwargs,
        )
        self.backend_digest = canonical_digest(
            {
                "authenticated_driver": self.backend_digest,
                "linux_bundle": assets.manifest_digest,
                "firmware_code": assets.files["firmware_code"].digest,
                "isolation": "kvm",
                "transport": "virtio-serial",
            }
        )

    def _verify_linux(self, path: Path, identity: MacOSVZHelperIdentity) -> tuple[bool, str | None]:
        ready, reason = kvm_capability()
        if not ready:
            return ready, reason
        try:
            self._linux_assets.verify()
            qemu = self._linux_assets.files["qemu"]
            if (
                path != qemu.path
                or identity.expected_code_digest != qemu.digest
                or identity.bundle_id != "io.tobkiri.packvm.qemu"
                or identity.team_id
                or identity.signing_identity
            ):
                return False, "Linux PackVM QEMU identity does not match the trusted bundle"
        except (OSError, ValueError):
            return False, "Linux PackVM immutable bundle verification failed"
        return True, None


class LinuxQemuSupervisorTransport(QemuSupervisorTransport):
    """Linux platform binding for the common authenticated transport."""

    platform = "linux-amd64"
