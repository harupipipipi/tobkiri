from __future__ import annotations

import hashlib
import tracemalloc
from pathlib import Path

import pytest

from tobkiri_protocol.platform_artifact import artifact_digest, verify_platform_artifact


def _pe_fixture(path: Path, *, machine: int, pe_offset: int = 0x80) -> Path:
    """Write a bounded PE fixture whose COFF header starts at ``pe_offset``."""
    size = max(64, pe_offset + 24)
    payload = bytearray(size)
    payload[:2] = b"MZ"
    payload[60:64] = pe_offset.to_bytes(4, "little")
    if pe_offset + 24 <= len(payload):
        payload[pe_offset : pe_offset + 4] = b"PE\0\0"
        payload[pe_offset + 4 : pe_offset + 6] = machine.to_bytes(2, "little")
    path.write_bytes(payload)
    path.chmod(0o755)
    return path


def _variant(root: Path, executable: Path, architecture: str) -> dict[str, str]:
    entrypoint_digest = hashlib.sha256()
    with executable.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            entrypoint_digest.update(chunk)
    return {
        "relative_path": executable.relative_to(root).as_posix(),
        "entrypoint": executable.relative_to(root).as_posix(),
        "artifact_digest": artifact_digest(executable),
        "entrypoint_digest": "sha256:" + entrypoint_digest.hexdigest(),
        "platform": "windows",
        "architecture": architecture,
        "bundle_identity": "io.tobkiri.shell.tauri",
    }


def _truncated_pe(path: Path) -> Path:
    """Write a PE claim whose COFF header is cut short."""
    executable = _pe_fixture(path, machine=0x8664)
    executable.write_bytes(executable.read_bytes()[:-1])
    return executable


def _out_of_range_pe(path: Path) -> Path:
    """Write a DOS header pointing beyond the file's end."""
    payload = bytearray(64)
    payload[:2] = b"MZ"
    payload[60:64] = (0x1000).to_bytes(4, "little")
    path.write_bytes(payload)
    path.chmod(0o755)
    return path


def test_pe_parser_accepts_dos_e_lfanew_at_normal_offset(tmp_path: Path) -> None:
    executable = _pe_fixture(tmp_path / "shell.exe", machine=0x8664)
    verify_platform_artifact(tmp_path, _variant(tmp_path, executable, "x86_64"))


@pytest.mark.parametrize(
    "fixture",
    [
        _truncated_pe,
        lambda path: _pe_fixture(path, machine=0x8664, pe_offset=32),
        _out_of_range_pe,
        lambda path: _pe_fixture(path, machine=0x14C),
    ],
)
def test_pe_parser_rejects_truncated_overlap_out_of_range_and_wrong_machine(
    tmp_path: Path, fixture,
) -> None:
    executable = tmp_path / "shell.exe"
    result = fixture(executable)
    if result is not None:
        executable = result
    variant = _variant(tmp_path, executable, "x86_64")
    with pytest.raises(Exception, match="PE|architecture"):
        verify_platform_artifact(tmp_path, variant)


def test_large_sparse_artifact_digest_uses_bounded_memory(tmp_path: Path) -> None:
    executable = tmp_path / "large-shell"
    size = 64 * 1024 * 1024
    with executable.open("wb") as handle:
        handle.write(b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 10 + b">\x00")
        handle.truncate(size)
    executable.chmod(0o755)
    variant = {
        "relative_path": "large-shell",
        "entrypoint": "large-shell",
        "artifact_digest": artifact_digest(executable),
        "entrypoint_digest": "",
        "platform": "linux",
        "architecture": "x86_64",
        "bundle_identity": "io.tobkiri.shell.tauri",
    }
    digest = hashlib.sha256()
    with executable.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    variant["entrypoint_digest"] = "sha256:" + digest.hexdigest()

    tracemalloc.start()
    verify_platform_artifact(tmp_path, variant)
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 8 * 1024 * 1024
