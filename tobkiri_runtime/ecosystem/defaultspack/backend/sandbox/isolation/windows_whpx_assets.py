"""Externally pinned Windows QEMU/WHPX executable, DLL, firmware and guest assets.

The manifest is an inventory, never its own trust authority. Its expected
identity comes from Launcher-sealed release binding or explicit development
inputs. Every redistributable DLL is pinned with QEMU; PATH is not a dependency.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from tobkiri_host.models import require_digest
from tobkiri_host.windows_pe_dependencies import (
    verify_amd64_pe as _verify_amd64_pe,
)
from tobkiri_host.windows_pe_dependencies import (
    verify_windows_qemu_dependency_closure as verify_windows_qemu_dependency_closure,  # noqa: PLC0414 - compatibility export
)
from tobkiri_host.windows_whpx_security import file_digest, stable_file

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


@dataclass(frozen=True)
class WindowsWHPXAsset:
    """One exact Windows or guest input with independently pinned identity."""

    path: Path
    digest: str
    size_bytes: int


@dataclass(frozen=True)
class WindowsWHPXAssets:
    """Verified immutable WHPX bundle and its external manifest trust anchor."""

    root: Path
    manifest_digest: str
    files: Mapping[str, WindowsWHPXAsset]
    image_source: str
    dependencies: tuple[WindowsWHPXAsset, ...] = ()

    def verify(self) -> None:
        """Recheck all code/guest bytes and the closed executable-directory set."""
        if file_digest(self.root / MANIFEST_NAME) != self.manifest_digest:
            raise ValueError("Windows PackVM manifest identity changed")
        all_assets = (*self.files.values(), *self.dependencies)
        for asset in all_assets:
            if (
                asset.path.stat().st_size != asset.size_bytes
                or file_digest(asset.path) != asset.digest
            ):
                raise ValueError("Windows PackVM asset digest or size changed")
        qemu = self.files["qemu"].path
        _verify_amd64_pe(qemu)
        expected = {qemu, *(a.path for a in self.dependencies)}
        if (
            any(path.parent != qemu.parent for path in expected)
            or set(qemu.parent.iterdir()) != expected
        ):
            raise ValueError("Windows PackVM executable directory has unpinned files")
        for dependency in self.dependencies:
            if dependency.path.suffix.lower() != ".dll":
                raise ValueError("Windows PackVM dependency must be an explicit DLL")
            _verify_amd64_pe(dependency.path)
        verify_windows_qemu_dependency_closure(qemu, tuple(a.path for a in self.dependencies))


def _record(root: Path, raw: object) -> WindowsWHPXAsset:
    if (
        not isinstance(raw, dict)
        or set(raw) != {"path", "sha256", "size_bytes"}
        or not isinstance(raw["path"], str)
        or type(raw["size_bytes"]) is not int
        or raw["size_bytes"] <= 0
    ):
        raise ValueError("Windows PackVM asset record is invalid")
    relative = PurePosixPath(raw["path"])
    if (
        relative.is_absolute()
        or not relative.parts
        or ".." in relative.parts
        or str(relative) != raw["path"]
        or "\\" in raw["path"]
        or any(c in raw["path"] for c in (":", "\0", "\r", "\n"))
        or any(part.endswith((".", " ")) for part in relative.parts)
    ):
        raise ValueError("Windows PackVM asset path escapes the bundle")
    require_digest(raw["sha256"], "Windows PackVM asset")
    return WindowsWHPXAsset(root.joinpath(*relative.parts), raw["sha256"], raw["size_bytes"])


def load_windows_whpx_assets(root: Path, expected_digest: str) -> WindowsWHPXAssets:
    """Load the closed WHPX manifest only after external identity verification."""
    require_digest(expected_digest, "Windows PackVM trusted manifest")
    if not root.is_absolute() or root.resolve(strict=True) != root or not root.is_dir():
        raise ValueError("Windows PackVM root must be a canonical directory")
    manifest = root / MANIFEST_NAME
    with stable_file(manifest) as source:
        content = source.read(128 * 1024 + 1)
    import hashlib

    if (
        len(content) > 128 * 1024
        or "sha256:" + hashlib.sha256(content).hexdigest() != expected_digest
    ):
        raise ValueError("Windows PackVM manifest does not match its trusted identity")
    value = json.loads(content)
    keys = {"schema", "architecture", "accelerator", "files", "image_source", "qemu_dependencies"}
    if (
        not isinstance(value, dict)
        or set(value) != keys
        or value["schema"] != SCHEMA
        or value["architecture"] != "amd64"
        or value["accelerator"] != "whpx"
        or not isinstance(value["files"], dict)
        or set(value["files"]) != SLOTS
        or not isinstance(value["qemu_dependencies"], list)
        or len(value["qemu_dependencies"]) > 128
        or not isinstance(value["image_source"], str)
        or not value["image_source"].startswith("https://")
    ):
        raise ValueError("Windows PackVM manifest isolation profile is invalid")
    files = {slot: _record(root, raw) for slot, raw in value["files"].items()}
    dependencies = tuple(_record(root, raw) for raw in value["qemu_dependencies"])
    names = [str(a.path).casefold() for a in (*files.values(), *dependencies)]
    if len(names) != len(set(names)):
        raise ValueError("Windows PackVM assets have duplicate or aliased paths")
    if files["qemu"].path.name.lower() != "qemu-system-x86_64.exe":
        raise ValueError("Windows PackVM executable name is invalid")
    result = WindowsWHPXAssets(
        root, expected_digest, MappingProxyType(files), value["image_source"], dependencies
    )
    result.verify()
    return result

