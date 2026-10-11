"""Digest-pinned, architecture-specific assets for Linux QEMU/KVM PackVM.

An adjacent JSON file is not a trust anchor. Callers must supply its expected
SHA-256 from a trusted build/Launcher binding. Development does so explicitly;
release discovery deliberately has no environment-variable fallback.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from tobkiri_host.linux_kvm_probe import (
    kvm_capability as kvm_capability,  # noqa: PLC0414 - compatibility export
)

SCHEMA = "io.tobkiri.packvm-qemu-provisioning.v1"
MANIFEST_NAME = "packvm-qemu-provisioning.v1.json"
SLOTS = frozenset(
    {
        "qemu",
        "firmware_code",
        "firmware_vars",
        "image",
        "agent",
        "config",
        "service",
        "bubblewrap",
        "bubblewrap_descriptor",
    }
)


@contextmanager
def pinned_asset(path: Path) -> Iterator[BinaryIO]:
    """Pin every path component without following links, then the input file."""
    if not path.is_absolute() or ".." in path.parts or path.resolve(strict=True) != path:
        raise ValueError("Linux PackVM asset path must be absolute and canonical")
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:-1]:
            child = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = child
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
    finally:
        os.close(directory)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("Linux PackVM asset is not a single-link regular file")
        if before.st_mode & 0o022 or before.st_uid not in (0, os.getuid()):
            raise ValueError("Linux PackVM asset ownership or permissions are unsafe")
        yield stream


def file_digest(path: Path) -> str:
    """Hash a stable single-link regular file without following symlinks."""
    with pinned_asset(path) as stream:
        before = os.fstat(stream.fileno())
        digest = hashlib.sha256()
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
        result = digest.hexdigest()
        after = os.fstat(stream.fileno())
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("Linux PackVM asset changed while hashing")
    return "sha256:" + result


@dataclass(frozen=True)
class LinuxQemuAsset:
    """One independently pinned immutable input."""

    path: Path
    digest: str
    size_bytes: int


@dataclass(frozen=True)
class LinuxQemuAssets:
    """Verified bundle plus its externally supplied trust anchor."""

    root: Path
    manifest_digest: str
    files: Mapping[str, LinuxQemuAsset]
    image_source: str

    def verify(self) -> None:
        """Recheck every code, firmware and guest input before promotion."""
        if file_digest(self.root / MANIFEST_NAME) != self.manifest_digest:
            raise ValueError("Linux PackVM bundle manifest changed")
        for asset in self.files.values():
            if (
                asset.path.stat().st_size != asset.size_bytes
                or file_digest(asset.path) != asset.digest
            ):
                raise ValueError("Linux PackVM bundle asset digest or size changed")
        if not os.access(self.files["qemu"].path, os.X_OK):
            raise ValueError("Linux PackVM QEMU is not executable")
        _verify_static_amd64_elf(self.files["qemu"].path)


def load_linux_qemu_assets(root: Path, expected_digest: str) -> LinuxQemuAssets:
    """Load a closed manifest only after verifying external expected identity."""
    from types import MappingProxyType

    from tobkiri_host.models import require_digest

    require_digest(expected_digest, "Linux PackVM trusted manifest")
    if not root.is_absolute() or root.resolve(strict=True) != root or not root.is_dir():
        raise ValueError("Linux PackVM bundle root must be a canonical directory")
    manifest = root / MANIFEST_NAME
    if manifest.stat().st_size > 128 * 1024 or file_digest(manifest) != expected_digest:
        raise ValueError("Linux PackVM bundle does not match its trusted manifest digest")
    value = json.loads(manifest.read_bytes())
    expected_keys = {
        "schema",
        "architecture",
        "accelerator",
        "files",
        "image_source",
        "qemu_dependencies",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected_keys
        or value["schema"] != SCHEMA
        or value["architecture"] != "amd64"
        or value["accelerator"] != "kvm"
        or value["qemu_dependencies"] != []
        or not isinstance(value["files"], dict)
        or set(value["files"]) != SLOTS
        or not isinstance(value["image_source"], str)
        or not value["image_source"].startswith("https://")
    ):
        raise ValueError("Linux PackVM bundle schema or isolation profile is invalid")
    files: dict[str, LinuxQemuAsset] = {}
    for slot, raw in value["files"].items():
        if (
            not isinstance(raw, dict)
            or set(raw) != {"path", "sha256", "size_bytes"}
            or not isinstance(raw["path"], str)
            or type(raw["size_bytes"]) is not int
            or raw["size_bytes"] <= 0
        ):
            raise ValueError("Linux PackVM asset record is invalid")
        relative = Path(raw["path"])
        if relative.is_absolute() or ".." in relative.parts or relative == Path("."):
            raise ValueError("Linux PackVM asset escapes the bundle")
        require_digest(raw["sha256"], "Linux PackVM asset")
        path = root / relative
        if path.resolve(strict=True) != path:
            raise ValueError("Linux PackVM asset path contains a symlink")
        files[slot] = LinuxQemuAsset(path, raw["sha256"], raw["size_bytes"])
    result = LinuxQemuAssets(root, expected_digest, MappingProxyType(files), value["image_source"])
    result.verify()
    return result


def _verify_static_amd64_elf(path: Path) -> None:
    """Use the same closed ELF contract as the distribution build pipeline."""
    from tobkiri_host.qemu_binary_validation import validate_static_amd64_elf

    validate_static_amd64_elf(path)
