"""Verification of selected packaged Shell/Application artifacts."""

from __future__ import annotations

import hashlib
import os
import plistlib
import stat
import subprocess
from pathlib import Path
from typing import Any, Mapping

from .errors import ProtocolError


def artifact_digest(path: Path) -> str:
    """Return the canonical v1 artifact-tree digest used by release packaging."""

    digest = hashlib.sha256()

    def visit(current: Path, relative: tuple[str, ...]) -> None:
        if current.is_symlink():
            raise ProtocolError("packaged artifact tree contains a symlink")
        if current.is_file():
            digest.update("/".join(relative).encode("utf-8"))
            digest.update(b"\0")
            _stable_update_digest(current, digest)
            return
        if not current.is_dir():
            raise ProtocolError("packaged artifact entry is unavailable")
        for child in sorted(current.iterdir(), key=lambda item: item.name):
            visit(child, (*relative, child.name))

    visit(path, ())
    return "sha256:" + digest.hexdigest()


def _identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    """Return the file identity fields used for TOCTOU checks."""
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
    )


def _stable_update_digest(path: Path, digest: Any) -> int:
    """Stream one regular file into a digest while preserving TOCTOU checks."""
    try:
        descriptor = os.open(
            os.fspath(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        )
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ProtocolError("packaged artifact member is not a regular file")
            size = 0
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
            after = os.fstat(handle.fileno())
    except OSError as exc:
        raise ProtocolError("packaged artifact member could not be read") from exc
    if _identity(before) != _identity(after) or size != after.st_size:
        raise ProtocolError("packaged artifact changed while it was verified")
    return size


def _read_bounded_header(path: Path) -> tuple[bytes, bytes | None]:
    """Read only the bounded header needed for architecture verification."""
    try:
        descriptor = os.open(
            os.fspath(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        )
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ProtocolError("packaged artifact entrypoint is not a regular file")
            prefix = handle.read(64)
            if prefix[:2] == b"MZ":
                if len(prefix) < 64:
                    raise ProtocolError("PE entrypoint DOS header is truncated")
                offset = int.from_bytes(prefix[60:64], "little")
                header_size = 4 + 20
                if offset < 64 or offset > before.st_size - header_size:
                    raise ProtocolError("PE entrypoint header is out of bounds")
                handle.seek(offset)
                pe_header = handle.read(header_size)
                if len(pe_header) < header_size or pe_header[:4] != b"PE\0\0":
                    raise ProtocolError("PE entrypoint signature is invalid or truncated")
                header = prefix
                header_tail = pe_header
            else:
                header = prefix
                header_tail = None
            after = os.fstat(handle.fileno())
    except OSError as exc:
        raise ProtocolError("packaged artifact entrypoint header could not be read") from exc
    if _identity(before) != _identity(after):
        raise ProtocolError("packaged artifact changed while its header was verified")
    return header, header_tail


def verify_platform_artifact(
    artifact_root: Path,
    variant: Mapping[str, Any],
    *,
    require_macos_code_signature: bool = False,
) -> Path:
    """Verify path, digest, entrypoint, architecture, and macOS bundle identity."""

    root = artifact_root.resolve(strict=True)
    if artifact_root.is_symlink() or not root.is_dir():
        raise ProtocolError("packaged artifact root must be a real directory")
    relative = Path(str(variant["relative_path"]))
    if relative.is_absolute() or ".." in relative.parts:
        raise ProtocolError("packaged artifact path escapes its root")
    artifact = root / relative
    for parent in (artifact, *artifact.parents):
        if parent == root.parent:
            break
        if parent.is_symlink():
            raise ProtocolError("packaged artifact path contains a symlink")
        if parent == root:
            break
    expected = str(variant["artifact_digest"])
    hexadecimal = expected.removeprefix("sha256:")
    if len(set(hexadecimal)) <= 1:
        raise ProtocolError("packaged artifact uses a sentinel digest")
    if artifact_digest(artifact) != expected:
        raise ProtocolError("packaged artifact digest does not match selected bytes")
    entrypoint = root / Path(str(variant["entrypoint"]))
    try:
        entrypoint.resolve(strict=True).relative_to(artifact.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ProtocolError("packaged artifact entrypoint is missing or outside artifact") from exc
    if entrypoint.is_symlink() or not entrypoint.is_file():
        raise ProtocolError("packaged artifact entrypoint is not a regular file")
    entrypoint_hasher = hashlib.sha256()
    _stable_update_digest(entrypoint, entrypoint_hasher)
    entrypoint_digest = "sha256:" + entrypoint_hasher.hexdigest()
    if entrypoint_digest != variant.get("entrypoint_digest"):
        raise ProtocolError("packaged artifact entrypoint digest does not match")
    if str(variant["platform"]) == "macos":
        info_path = artifact / "Contents" / "Info.plist"
        if info_path.is_symlink() or not info_path.is_file():
            raise ProtocolError("macOS packaged artifact has no safe Info.plist")
        try:
            info = plistlib.loads(info_path.read_bytes())
        except (OSError, plistlib.InvalidFileException) as exc:
            raise ProtocolError("macOS packaged artifact Info.plist is invalid") from exc
        if info.get("CFBundleIdentifier") != variant["bundle_identity"]:
            raise ProtocolError("macOS packaged artifact bundle identity does not match")
        if require_macos_code_signature:
            _verify_macos_code_signature(artifact)
    _verify_binary_architecture(entrypoint, str(variant["architecture"]))
    return artifact


def _verify_macos_code_signature(artifact: Path) -> None:
    """Require the installed macOS application signature to validate."""

    try:
        result = subprocess.run(
            ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(artifact)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProtocolError("macOS packaged artifact signature could not be verified") from exc
    if result.returncode != 0:
        raise ProtocolError("macOS packaged artifact signature is invalid")


def _verify_binary_architecture(path: Path, architecture: str) -> None:
    payload, pe_header = _read_bounded_header(path)
    actual: str | None = None
    if payload[:4] in {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf"} and len(payload) >= 8:
        machine = int.from_bytes(
            payload[4:8],
            "little" if payload[:4] == b"\xcf\xfa\xed\xfe" else "big",
        )
        actual = {0x01000007: "x86_64", 0x0100000C: "arm64"}.get(machine)
    elif payload[:2] == b"MZ":
        if pe_header is None:
            raise ProtocolError("PE entrypoint header is truncated")
        actual = {0x8664: "x86_64", 0xAA64: "arm64"}.get(
            int.from_bytes(pe_header[4:6], "little")
        )
    elif payload[:4] == b"\x7fELF" and len(payload) >= 20:
        actual = {62: "x86_64", 183: "arm64"}.get(
            int.from_bytes(
                payload[18:20],
                "little" if payload[5:6] == b"\x01" else "big",
            )
        )
    if actual != architecture:
        raise ProtocolError("packaged artifact architecture does not match selection")


__all__ = ["artifact_digest", "verify_platform_artifact"]
