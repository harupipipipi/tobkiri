"""Windows-aware path validation for shared signed PackVM allocation records."""

from __future__ import annotations

from pathlib import PurePath, PurePosixPath, PureWindowsPath

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .errors import BackendUnavailableError
from .macos_vz_supervisor import MacOSVZDomainAllocation, MacOSVZLaunchAssets
from .models import require_digest


def host_path(value: str) -> PurePath:
    """Validate local absolute DOS or POSIX paths without allowing UNC/devices."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 32760
        or any(c in value for c in ("\0", "\r", "\n"))
    ):
        raise BackendUnavailableError("Windows PackVM path is invalid")
    # Launcher canonicalization may add the Win32 extended local-drive prefix.
    # Normalize only that spelling; UNC and other device namespaces still fail.
    value = value.removeprefix("\\\\?\\")
    if value.startswith("/"):
        path = PurePosixPath(value)
    else:
        path = PureWindowsPath(value)
        if (
            not path.is_absolute()
            or len(path.drive) != 2
            or path.drive[1] != ":"
            or not path.drive[0].isalpha()
        ):
            raise BackendUnavailableError("Windows PackVM requires an absolute local drive path")
        reserved = {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10)),
        }
        for part in path.parts[1:]:
            if (
                part.endswith((".", " "))
                or any(c in part for c in '<>:"|?*')
                or part.split(".", 1)[0].upper() in reserved
            ):
                raise BackendUnavailableError(
                    "Windows PackVM path contains a device or alternate stream"
                )
    if ".." in path.parts:
        raise BackendUnavailableError("Windows PackVM path traverses outside its root")
    return path


class WindowsWHPXLaunchAssets(MacOSVZLaunchAssets):
    """Shared immutable launch facts with local Windows path validation."""

    def __post_init__(self) -> None:
        for value in (
            self.base_image_digest,
            self.agent_template_digest,
            self.config_template_digest,
        ):
            require_digest(value, "Windows PackVM launch asset")
        host_path(self.base_image_path)
        if self.base_image_read_only is not True or self.boot_mode != "efi":
            raise BackendUnavailableError(
                "Windows PackVM requires read-only provenance and EFI boot"
            )


class WindowsWHPXDomainAllocation(MacOSVZDomainAllocation):
    """Identical signed allocation wire fields, with Windows-safe path semantics."""

    def __post_init__(self) -> None:
        for value in (self.domain_id, self.reservation_id, self.lease_id):
            if not isinstance(value, str) or not value or len(value) > 512 or "\0" in value:
                raise BackendUnavailableError("Windows PackVM allocation identity is invalid")
        root = host_path(self.run_root)
        paths = [
            host_path(value)
            for value in (
                self.cow_disk_path,
                self.efi_store_path,
                self.agent_seed_path,
                self.config_seed_path,
            )
        ]
        if any(path.parent != root for path in paths) or len(set(paths)) != len(paths):
            raise BackendUnavailableError(
                "Windows PackVM allocation escaped or aliased its private root"
            )
        for digest in (
            self.cow_disk_digest,
            self.efi_variable_store_digest,
            self.agent_seed_digest,
            self.config_seed_digest,
        ):
            require_digest(digest, "Windows PackVM allocation asset")
        if not isinstance(self.guest_public_key, bytes) or len(self.guest_public_key) != 32:
            raise BackendUnavailableError("Windows PackVM guest public key is invalid")
        Ed25519PublicKey.from_public_bytes(self.guest_public_key)
