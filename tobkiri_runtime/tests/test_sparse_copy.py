"""Logical-byte integrity and native allocation regressions for raw VM copies."""

import hashlib
import os

import pytest

from tobkiri_host import sparse_copy


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize(
    "data",
    [
        b"x",
        b"small nonzero payload",
        bytes(2 * 1024 * 1024),
        bytes(1024 * 1024) + b"x" * 65536 + bytes(1024 * 1024) + b"end" + bytes(1024 * 1024),
    ],
    ids=["one-byte", "small", "all-zero", "leading-interior-trailing"],
)
def test_sparse_copy_preserves_every_logical_byte(tmp_path, data):
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(data)
    with source.open("rb") as incoming, target.open("x+b") as output:
        assert sparse_copy.copy_verified_stream(
            incoming,
            output,
            expected_digest=digest(data),
            size_bytes=len(data),
            sparse=True,
        ) == len(data)
    assert target.read_bytes() == data
    assert source.read_bytes() == data
    assert target.stat().st_size == len(data)


def test_sparse_copy_rejects_nonempty_output(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"x")
    target.write_bytes(b"existing user data")
    with (
        source.open("rb") as incoming,
        target.open("r+b") as output,
        pytest.raises(ValueError, match="empty private"),
    ):
        sparse_copy.copy_verified_stream(
            incoming,
            output,
            expected_digest=digest(b"x"),
            size_bytes=1,
            sparse=True,
        )
    assert target.read_bytes() == b"existing user data"


def test_sparse_image_actual_allocated_size(tmp_path):
    data = bytes(4 * 1024 * 1024) + b"x" * 65536 + bytes(4 * 1024 * 1024)
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(data)
    with source.open("rb") as incoming, target.open("x+b") as output:
        sparse_copy.copy_verified_stream(
            incoming,
            output,
            expected_digest=digest(data),
            size_bytes=len(data),
            sparse=True,
        )
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        size = ctypes.WinDLL("kernel32", use_last_error=True).GetCompressedFileSizeW
        size.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        size.restype = wintypes.DWORD
        high = wintypes.DWORD()
        ctypes.set_last_error(0)
        low = size(str(target), ctypes.byref(high))
        if low == 0xFFFFFFFF and ctypes.get_last_error():
            raise ctypes.WinError(ctypes.get_last_error())
        allocated = (high.value << 32) | low
        assert target.stat().st_file_attributes & 0x200
    else:
        allocated = target.stat().st_blocks * 512
    assert allocated < len(data) // 2
    assert digest(target.read_bytes()) == digest(data)


@pytest.mark.parametrize("failure", ["digest", "unsupported", "write"])
def test_private_copy_failure_removes_only_new_output(tmp_path, monkeypatch, failure):
    from ecosystem.defaultspack.backend.sandbox.isolation import (
        windows_whpx_provisioner as provisioner,
    )

    source, target = tmp_path / "source", tmp_path / "boot.raw"
    source.write_bytes(b"data" + bytes(65536))
    expected = digest(source.read_bytes())
    if failure == "digest":
        expected = digest(b"wrong")
    elif failure == "unsupported":

        def unsupported(output):
            raise OSError("sparse unsupported")

        monkeypatch.setattr(sparse_copy, "_enable_sparse", unsupported)
    else:

        def failed_copy(incoming, output, **kwargs):
            output.write(b"partial")
            raise OSError("disk full")

        monkeypatch.setattr(provisioner, "copy_verified_stream", failed_copy)
    with pytest.raises((ValueError, OSError)):
        provisioner._copy_private(source, target, expected)
    assert not target.exists()
    assert source.read_bytes() == b"data" + bytes(65536)


def test_builder_sparse_image_and_failure_cleanup(tmp_path):
    from scripts import build_linux_packvm_bundle as builder

    source, target = tmp_path / "source", tmp_path / "target"
    data = bytes(1024 * 1024) + b"payload" + bytes(1024 * 1024)
    source.write_bytes(data)
    record = builder._copy_verified(source, target, digest(data), "image")
    assert record["size_bytes"] == len(data)
    assert target.read_bytes() == data
    failed = tmp_path / "failed"
    with pytest.raises(ValueError, match="digest mismatch"):
        builder._copy_verified(source, failed, digest(b"wrong"), "image")
    assert not failed.exists()
    with pytest.raises(FileExistsError):
        builder._copy_verified(source, target, digest(data), "image")
    assert target.read_bytes() == data


def test_sparse_copy_verifies_destination_readback(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"payload")

    class CorruptReadback:
        def __init__(self, stream):
            self.stream = stream

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def read(self, size):
            value = self.stream.read(size)
            return b"X" * len(value)

    with (
        source.open("rb") as incoming,
        target.open("x+b") as output,
        pytest.raises(ValueError, match="destination copy digest mismatch"),
    ):
        sparse_copy.copy_verified_stream(
            incoming,
            CorruptReadback(output),
            expected_digest=digest(b"payload"),
            size_bytes=7,
            sparse=True,
        )


def test_zero_ranges_coalesced_and_deallocated_after_flush(tmp_path, monkeypatch):
    block = 65536
    data = bytes(2 * block) + b"x" * block + bytes(3 * block) + b"y" * block + bytes(17)
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(data)
    calls = []

    def deallocate(output, ranges):
        assert os.fstat(output.fileno()).st_size == len(data)
        assert target.read_bytes() == data  # Buffered data was flushed already.
        calls.append(ranges)

    monkeypatch.setattr(sparse_copy, "_deallocate_zero_ranges", deallocate)
    with source.open("rb") as incoming, target.open("x+b") as output:
        sparse_copy.copy_verified_stream(
            incoming, output, expected_digest=digest(data), size_bytes=len(data), sparse=True
        )
    assert calls == [[(0, 2 * block), (3 * block, 6 * block), (7 * block, len(data))]]
    assert target.read_bytes() == source.read_bytes()


def test_private_copy_zero_deallocation_failure_cleans_output(tmp_path, monkeypatch):
    from ecosystem.defaultspack.backend.sandbox.isolation import (
        windows_whpx_provisioner as provisioner,
    )

    source, target = tmp_path / "source", tmp_path / "boot.raw"
    data = bytes(65536) + b"payload"
    source.write_bytes(data)

    def failed_deallocation(output, ranges):
        raise OSError("FSCTL_SET_ZERO_DATA failed")

    monkeypatch.setattr(sparse_copy, "_deallocate_zero_ranges", failed_deallocation)
    with pytest.raises(OSError, match="FSCTL_SET_ZERO_DATA"):
        provisioner._copy_private(source, target, digest(data))
    assert not target.exists()
    assert source.read_bytes() == data
