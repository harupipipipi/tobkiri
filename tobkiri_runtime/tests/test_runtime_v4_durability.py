"""Cross-platform durability contracts for Profile v4 activation state."""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from tobkiri_protocol import durability


def _temporary_files(path: Path) -> list[Path]:
    return list(path.glob(".state.json.*.tmp"))


def test_atomic_write_creates_and_replaces_only_after_file_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.json"
    events: list[str] = []
    real_fsync = durability.os.fsync
    real_replace = durability.os.replace

    def fsync(descriptor: int) -> None:
        events.append("file_fsync")
        real_fsync(descriptor)

    def replace_durable(source: Path, destination: Path) -> None:
        events.append("replace_durable")
        real_replace(source, destination)

    monkeypatch.setattr(durability.os, "fsync", fsync)
    monkeypatch.setattr(durability, "replace_file_durable", replace_durable)

    durability.write_bytes_atomic(target, b"first")
    durability.write_bytes_atomic(target, b"second")

    assert target.read_bytes() == b"second"
    assert events == [
        "file_fsync",
        "replace_durable",
        "file_fsync",
        "replace_durable",
    ]
    assert _temporary_files(tmp_path) == []


def test_atomic_write_failure_before_replace_preserves_target_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"old")

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise PermissionError("replace denied")

    monkeypatch.setattr(durability, "replace_file_durable", fail_replace)
    with pytest.raises(PermissionError, match="replace denied"):
        durability.write_bytes_atomic(target, b"new")

    assert target.read_bytes() == b"old"
    assert _temporary_files(tmp_path) == []


def test_atomic_write_file_flush_error_preserves_target_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"old")

    def fail_fsync(_descriptor: int) -> None:
        raise OSError("file flush failed")

    monkeypatch.setattr(durability.os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="file flush failed"):
        durability.write_bytes_atomic(target, b"new")

    assert target.read_bytes() == b"old"
    assert _temporary_files(tmp_path) == []


def test_atomic_write_failure_after_replace_is_reported_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"old")
    real_replace = durability.os.replace

    def replace_then_fail(source: Path, destination: Path) -> None:
        real_replace(source, destination)
        raise PermissionError("publication durability failed")

    monkeypatch.setattr(durability, "replace_file_durable", replace_then_fail)
    with pytest.raises(PermissionError, match="publication durability failed"):
        durability.write_bytes_atomic(target, b"new")

    assert target.read_bytes() == b"new"
    assert _temporary_files(tmp_path) == []


def test_posix_durable_replace_flushes_parent_after_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX durable replacement contract")

    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")
    events: list[str] = []
    real_replace = durability.os.replace

    def replace(source_path: Path, destination_path: Path) -> None:
        events.append("replace")
        real_replace(source_path, destination_path)

    def flush(path: Path) -> None:
        events.append(f"flush:{path.name}")

    monkeypatch.setattr(durability.os, "replace", replace)
    monkeypatch.setattr(durability, "flush_directory", flush)

    durability.replace_file_durable(source, destination)

    assert destination.read_bytes() == b"new"
    assert events == ["replace", f"flush:{tmp_path.name}"]


def test_posix_durable_publish_is_no_replace_and_consumes_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX durable publication contract")

    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_bytes(b"payload")
    flushed: list[Path] = []
    monkeypatch.setattr(durability, "flush_directory", flushed.append)

    durability.publish_file_durable(source, destination)

    assert not source.exists()
    assert destination.read_bytes() == b"payload"
    assert flushed == [tmp_path]

    second = tmp_path / "second"
    second.write_bytes(b"other")
    with pytest.raises(FileExistsError):
        durability.publish_file_durable(second, destination)
    assert second.exists()
    assert destination.read_bytes() == b"payload"


class _WindowsMoveApi:
    def __init__(self, *, succeeds: bool = True) -> None:
        self.succeeds = succeeds
        self.calls: list[tuple[str, str, int]] = []

    def MoveFileExW(self, source: str, destination: str, flags: int) -> bool:
        self.calls.append((source, destination, flags))
        return self.succeeds


def test_windows_replace_uses_replace_existing_and_write_through(tmp_path: Path) -> None:
    native = _WindowsMoveApi()
    source = tmp_path / "source"
    destination = tmp_path / "destination"

    durability._move_windows_file_write_through(
        source,
        destination,
        replace_existing=True,
        kernel32=native,
    )

    assert native.calls == [
        (
            str(source),
            str(destination),
            durability._MOVEFILE_REPLACE_EXISTING | durability._MOVEFILE_WRITE_THROUGH,
        )
    ]


def test_windows_publish_uses_write_through_without_replace(tmp_path: Path) -> None:
    native = _WindowsMoveApi()
    source = tmp_path / "source"
    destination = tmp_path / "destination"

    durability._move_windows_file_write_through(
        source,
        destination,
        replace_existing=False,
        kernel32=native,
    )

    assert native.calls == [
        (str(source), str(destination), durability._MOVEFILE_WRITE_THROUGH)
    ]


def test_windows_move_failure_propagates_native_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    native = _WindowsMoveApi(succeeds=False)
    monkeypatch.setattr(durability, "_windows_last_error", lambda: 5)

    with pytest.raises(OSError, match=r"MoveFileExW failed"):
        durability._move_windows_file_write_through(
            tmp_path / "source",
            tmp_path / "destination",
            replace_existing=True,
            kernel32=native,
        )


def test_windows_publish_maps_existing_destination_to_file_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    native = _WindowsMoveApi(succeeds=False)
    monkeypatch.setattr(durability, "_windows_last_error", lambda: 183)

    with pytest.raises(FileExistsError) as exc_info:
        durability._move_windows_file_write_through(
            tmp_path / "source",
            tmp_path / "destination",
            replace_existing=False,
            kernel32=native,
        )

    assert exc_info.value.errno == errno.EEXIST


def test_windows_flush_directory_fails_closed_instead_of_using_unsupported_api(
    tmp_path: Path,
) -> None:
    if os.name != "nt":
        pytest.skip("Windows directory durability contract")

    with pytest.raises(OSError) as exc_info:
        durability.flush_directory(tmp_path)
    assert exc_info.value.errno == errno.ENOTSUP


def test_posix_directory_open_error_is_not_silenced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX directory descriptor contract")

    def fail_open(_path: Path, _flags: int) -> int:
        raise PermissionError("directory open denied")

    monkeypatch.setattr(durability.os, "open", fail_open)
    with pytest.raises(PermissionError, match="directory open denied"):
        durability.flush_directory(tmp_path)
