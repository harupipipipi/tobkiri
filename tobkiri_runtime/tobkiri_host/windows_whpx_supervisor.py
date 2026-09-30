"""WHPX isolation evidence for the common signed PackVM guest protocol."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ecosystem.defaultspack.backend.sandbox.isolation.windows_whpx_assets import WindowsWHPXAssets
from tobkiri_protocol.canonical import canonical_digest

from .macos_vz_supervisor import MacOSVZHelperIdentity, MacOSVZSupervisorDriver
from .qemu_supervisor_transport import QemuRuntime, QemuSupervisorTransport
from .windows_whpx_probe import whpx_capability


class WindowsWHPXSupervisorDriver(MacOSVZSupervisorDriver):
    """Reuse guest signatures and grant mediation with independently pinned WHPX."""

    substrate_id = "windows-whpx"
    _platform_prefix = "windows-"

    def __init__(self, *, assets: WindowsWHPXAssets, **kwargs: Any) -> None:
        self._windows_assets = assets
        if "identity_verifier" in kwargs or "platform" in kwargs or "runtime" in kwargs:
            raise ValueError("Windows PackVM identity verifier cannot be overridden")
        super().__init__(
            platform="windows-amd64",
            identity_verifier=self._verify_windows,
            runtime=QemuRuntime("whpx"),
            **kwargs,
        )
        self.backend_digest = canonical_digest(
            {
                "authenticated_driver": self.backend_digest,
                "windows_bundle": assets.manifest_digest,
                "firmware_code": assets.files["firmware_code"].digest,
                "isolation": "whpx",
                "transport": "private-inherited-virtio-serial",
            }
        )

    def _verify_windows(
        self, path: Path, identity: MacOSVZHelperIdentity
    ) -> tuple[bool, str | None]:
        ready, reason = whpx_capability()
        if not ready:
            return ready, reason
        try:
            self._windows_assets.verify()
            qemu = self._windows_assets.files["qemu"]
            if (
                path != qemu.path
                or identity.expected_code_digest != qemu.digest
                or identity.bundle_id != "io.tobkiri.packvm.qemu"
                or identity.team_id
                or identity.signing_identity
            ):
                return False, "Windows PackVM executable identity differs from the sealed bundle"
        except (OSError, ValueError):
            return False, "Windows PackVM immutable bundle verification failed"
        return True, None

    def _verify_launch_assets(self, allocation: Any) -> None:
        """Re-measure Windows files using pinned, no-reparse native handles."""
        from .errors import BackendUnavailableError
        from .windows_whpx_security import file_digest

        measured = (
            (self._launch_assets.base_image_path, self._launch_assets.base_image_digest),
            (allocation.cow_disk_path, allocation.cow_disk_digest),
            (allocation.efi_store_path, allocation.efi_variable_store_digest),
            (allocation.agent_seed_path, allocation.agent_seed_digest),
            (allocation.config_seed_path, allocation.config_seed_digest),
        )
        for path, expected in measured:
            try:
                if file_digest(Path(path)) != expected:
                    raise ValueError("digest mismatch")
            except (OSError, ValueError) as exc:
                raise BackendUnavailableError(
                    "Windows PackVM launch asset identity changed"
                ) from exc


class WindowsWHPXSupervisorTransport(QemuSupervisorTransport):
    """Windows binding of the platform-neutral authenticated guest transport."""

    platform = "windows-amd64"
