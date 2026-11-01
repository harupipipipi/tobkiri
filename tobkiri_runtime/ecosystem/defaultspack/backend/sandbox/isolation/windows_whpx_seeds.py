"""Portable guest seed writers using the established, signed guest framing.

ISO generation is host-tool-free. Platform persistence is private ACL-backed
on Windows, and POSIX owner-only on Linux test/build hosts.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tobkiri_host.windows_whpx_security import file_digest as _file_digest
from tobkiri_host.windows_whpx_security import private_directory, stable_file

from .macos_vz_provisioner import (
    _ARTIFACT_SEED_FORMAT,
    _ARTIFACT_SEED_MAGIC,
    _MAX_ARTIFACT_SEED_BYTES,
    _materialized_artifact_seed_framing,
)

_DIGEST_PREFIX = "sha256:"
MaterializedPackArtifact = Any


def _digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _ensure_private_directory(path: Path) -> None:
    private_directory(path)


def _open_private_file(path: Path, flags: int) -> int:
    if flags != os.O_RDONLY:
        raise ValueError("Portable seed readers only open read-only sources")
    with stable_file(path) as source:
        return os.dup(source.fileno())


def _validate_private_file(path: Path, expected_size: int) -> None:
    with stable_file(path) as source:
        if os.fstat(source.fileno()).st_size != expected_size:
            raise ValueError("PackVM private seed size changed")


def _write_all(fd: int, content: bytes) -> None:
    pending = memoryview(content)
    while pending:
        size = os.write(fd, pending)
        if size <= 0:
            raise OSError("PackVM seed write failed")
        pending = pending[size:]


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
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        write_hashed(_ARTIFACT_SEED_MAGIC)
        write_hashed(len(encoded_manifest).to_bytes(8, "big"))
        write_hashed(encoded_manifest)
        for item in artifact.files:
            if _digest_bytes(item.content) != item.digest:
                raise ValueError("PackVM artifact bytes changed before seed creation")
            write_hashed(item.content)
        if os.fstat(descriptor).st_size != total_size:
            raise ValueError("PackVM artifact seed size is invalid")
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
        raise ValueError("PackVM ISO seed content is invalid")
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
            raise ValueError("PackVM ISO seed content is invalid")
        if isinstance(data, bytes):
            source_facts[name] = (len(data), data, _digest_bytes(data))
        elif isinstance(data, Path):
            descriptor = _open_private_file(data, os.O_RDONLY)
            try:
                metadata = os.fstat(descriptor)
                if metadata.st_size < 0 or metadata.st_size > _MAX_ARTIFACT_SEED_BYTES:
                    raise ValueError("PackVM ISO seed source exceeds its bound")
            finally:
                os.close(descriptor)
            source_facts[name] = (metadata.st_size, data, _file_digest(data))
        else:
            raise ValueError("PackVM ISO seed content is invalid")  # noqa: TRY004
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
            raise ValueError("PackVM ISO seed directory exceeds its bound")
        root_directory[position : position + len(entry)] = entry
        position += len(entry)
    _ensure_private_directory(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        if hasattr(os, "fchmod"):
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
                    raise ValueError("PackVM ISO seed source changed while copying")
            finally:
                os.close(source_descriptor)
        if os.fstat(descriptor).st_size != total * block:
            raise ValueError("PackVM ISO seed size is invalid")
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
