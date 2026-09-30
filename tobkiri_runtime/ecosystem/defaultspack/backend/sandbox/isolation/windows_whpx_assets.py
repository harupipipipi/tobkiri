"""Externally pinned Windows QEMU/WHPX executable, DLL, firmware and guest assets.

The manifest is an inventory, never its own trust authority. Its expected
identity comes from Launcher-sealed release binding or explicit development
inputs. Every redistributable DLL is pinned with QEMU; PATH is not a dependency.
"""

from __future__ import annotations

import json
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from tobkiri_host.models import require_digest
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


def _verify_amd64_pe(path: Path) -> None:
    with stable_file(path) as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:2] != b"MZ":
            raise ValueError("Windows PackVM binary must be an amd64 PE file")
        offset = struct.unpack_from("<I", header, 60)[0]
        if not 64 <= offset <= 16 * 1024 * 1024:
            raise ValueError("Windows PackVM PE offset is invalid")
        stream.seek(offset)
        pe = stream.read(26)
        if (
            len(pe) != 26
            or pe[:4] != b"PE\0\0"
            or struct.unpack_from("<H", pe, 4)[0] != 0x8664
            or struct.unpack_from("<H", pe, 24)[0] != 0x20B
        ):
            raise ValueError("Windows PackVM binary must use the amd64 PE32+ architecture")


_SYSTEM_DLLS = frozenset(
    {
        "advapi32.dll",
        "avrt.dll",
        "bcrypt.dll",
        "bcryptprimitives.dll",
        "cabinet.dll",
        "cfgmgr32.dll",
        "comctl32.dll",
        "comdlg32.dll",
        "crypt32.dll",
        "d3d11.dll",
        "d3d12.dll",
        "dbghelp.dll",
        "dnsapi.dll",
        "dsound.dll",
        "dwmapi.dll",
        "dxgi.dll",
        "gdi32.dll",
        "hid.dll",
        "imm32.dll",
        "iphlpapi.dll",
        "kernel32.dll",
        "msimg32.dll",
        "msvcrt.dll",
        "netapi32.dll",
        "normaliz.dll",
        "ntdll.dll",
        "ole32.dll",
        "oleaut32.dll",
        "opengl32.dll",
        "pdh.dll",
        "powrprof.dll",
        "propsys.dll",
        "psapi.dll",
        "rpcrt4.dll",
        "secur32.dll",
        "setupapi.dll",
        "shell32.dll",
        "shlwapi.dll",
        "ucrtbase.dll",
        "user32.dll",
        "userenv.dll",
        "usp10.dll",
        "uxtheme.dll",
        "version.dll",
        "virtdisk.dll",
        "winhttp.dll",
        "winhvplatform.dll",
        "wininet.dll",
        "winmm.dll",
        "winnsi.dll",
        "winspool.drv",
        "winsta.dll",
        "wintrust.dll",
        "wldap32.dll",
        "ws2_32.dll",
        "wtsapi32.dll",
    }
)


def verify_windows_qemu_dependency_closure(
    executable: Path,
    dependencies: tuple[Path, ...],
) -> None:
    """Check PE architecture and transitive imports without loading any code.

    Known operating-system DLL/API-set names resolve from Windows. Every other
    normal or delay-loaded import must be provided adjacent to QEMU and pinned
    in the manifest. Compiler runtimes are not assumed to be installed globally.
    """
    binaries = (executable, *dependencies)
    supplied = {path.name.casefold() for path in dependencies}
    if len(supplied) != len(dependencies):
        raise ValueError("Windows PackVM dependency names are ambiguous")
    if any(
        path.parent != executable.parent or path.suffix.lower() != ".dll" for path in dependencies
    ):
        raise ValueError("Windows PackVM DLLs must be adjacent to QEMU")
    for path in binaries:
        for name in _pe_imports(path):
            if name in supplied or name in _SYSTEM_DLLS:
                continue
            if name.startswith(("api-ms-win-", "ext-ms-win-")) and name.endswith(".dll"):
                continue
            raise ValueError(f"Windows PackVM has an unbundled DLL import: {name}")


def _pe_imports(path: Path) -> set[str]:
    """Read bounded PE import and delay-import tables using file-backed RVAs."""
    _verify_amd64_pe(path)
    with stable_file(path) as stream:
        import os

        size = os.fstat(stream.fileno()).st_size

        def read(offset: int, length: int) -> bytes:
            if offset < 0 or length < 0 or offset + length > size:
                raise ValueError("Windows PackVM PE data is outside the file")
            stream.seek(offset)
            result = stream.read(length)
            if len(result) != length:
                raise ValueError("Windows PackVM PE data is truncated")
            return result

        pe_offset = struct.unpack("<I", read(60, 4))[0]
        coff = read(pe_offset + 4, 20)
        sections_count = struct.unpack_from("<H", coff, 2)[0]
        optional_size = struct.unpack_from("<H", coff, 16)[0]
        if not 1 <= sections_count <= 96 or not 112 <= optional_size <= 4096:
            raise ValueError("Windows PackVM PE header limits are invalid")
        optional = read(pe_offset + 24, optional_size)
        directory_count = struct.unpack_from("<I", optional, 108)[0]
        if directory_count > (optional_size - 112) // 8:
            raise ValueError("Windows PackVM PE directory count is invalid")
        sections = []
        for index in range(sections_count):
            raw = read(pe_offset + 24 + optional_size + index * 40, 40)
            virtual_size, address, raw_size, offset = struct.unpack_from("<IIII", raw, 8)
            sections.append((address, max(virtual_size, raw_size), offset, raw_size))

        def rva_offset(rva: int, length: int) -> int:
            for address, virtual_size, offset, raw_size in sections:
                if address <= rva and rva + length <= address + min(virtual_size, raw_size):
                    return offset + rva - address
            raise ValueError("Windows PackVM PE RVA is not backed by file bytes")

        def name_at(rva: int) -> str:
            content = bytearray()
            for index in range(256):
                char = read(rva_offset(rva + index, 1), 1)
                if char == b"\0":
                    break
                content.extend(char)
            else:
                raise ValueError("Windows PackVM DLL name is too long")
            try:
                name = content.decode("ascii").casefold()
            except UnicodeError as exc:
                raise ValueError("Windows PackVM DLL name is not ASCII") from exc
            if not name or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_." for c in name):
                raise ValueError("Windows PackVM DLL name contains a path")
            return name

        names: set[str] = set()
        for directory, entry_size, name_index in ((1, 20, 3), (13, 32, 1)):
            if directory_count <= directory:
                continue
            address, length = struct.unpack_from("<II", optional, 112 + directory * 8)
            if address == 0 and length == 0:
                continue
            if address == 0 or not entry_size <= length <= 1024 * 1024:
                raise ValueError("Windows PackVM PE import directory is invalid")
            for index in range(min(length // entry_size, 4096)):
                raw = read(rva_offset(address + index * entry_size, entry_size), entry_size)
                values = struct.unpack("<" + "I" * (entry_size // 4), raw)
                if not any(values):
                    break
                if directory == 13 and values[0] != 1:
                    raise ValueError("Windows PackVM delay imports must use RVA addressing")
                names.add(name_at(values[name_index]))
            else:
                raise ValueError("Windows PackVM PE import list is not terminated")
        return names
