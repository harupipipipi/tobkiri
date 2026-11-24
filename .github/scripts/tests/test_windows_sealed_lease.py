"""Regression tests for the Launcher's real Windows shared lifetime lease."""

from __future__ import annotations

import ctypes
import importlib.util
import os
import sys
import types
from pathlib import Path
from typing import Any

import pytest


BOOTSTRAP = (
    Path(__file__).resolve().parents[1]
    / "sealed_python_sources"
    / "tobkiri_sealed"
    / "bootstrap.py"
)


@pytest.fixture
def bootstrap(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Load only the checked-in bootstrap without altering global sys.path."""
    package_name = "_sealed_windows_lease_contract"
    package = types.ModuleType(package_name)
    package.SCHEMA = "io.tobkiri.sealed-python-environment.v1"
    package.__path__ = [str(BOOTSTRAP.parent)]
    monkeypatch.setitem(sys.modules, package_name, package)
    spec = importlib.util.spec_from_file_location(f"{package_name}.bootstrap", BOOTSTRAP)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


class _Function:
    def __init__(self, result: int = 1) -> None:
        self.result = result
        self.calls: list[tuple[Any, ...]] = []

    def __call__(self, *args: Any) -> int:
        self.calls.append(args)
        return self.result


def _fake_windows_api(monkeypatch: pytest.MonkeyPatch) -> Any:
    kernel = types.SimpleNamespace(LockFileEx=_Function(), UnlockFileEx=_Function())
    monkeypatch.setitem(
        sys.modules, "msvcrt", types.SimpleNamespace(get_osfhandle=lambda fd: fd + 100)
    )

    def load(name: str, **kwargs: Any) -> Any:
        assert name == "kernel32.dll"
        assert kwargs == {"use_last_error": True, "winmode": 0x00000800}
        return kernel

    monkeypatch.setattr(ctypes, "WinDLL", load, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 33, raising=False)
    monkeypatch.setattr(ctypes, "WinError", lambda code: OSError(code, "locked"), raising=False)
    return kernel


def test_windows_lease_uses_shared_nonblocking_byte_zero(
    bootstrap: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CRT LK_RLCK is exclusive, so it cannot be used by this protocol."""
    kernel = _fake_windows_api(monkeypatch)
    lease = bootstrap._WindowsSharedLease(types.SimpleNamespace(fileno=lambda: 7))
    assert len(kernel.LockFileEx.calls) == 1
    call = kernel.LockFileEx.calls[0]
    assert call[:5] == (107, 1, 0, 1, 0)  # No EXCLUSIVE_LOCK bit.
    assert call[5]._obj.Offset == call[5]._obj.OffsetHigh == 0
    lease.release()
    assert len(kernel.UnlockFileEx.calls) == 1
    assert kernel.UnlockFileEx.calls[0][:4] == (107, 0, 1, 0)
    assert kernel.UnlockFileEx.calls[0][4]._obj is call[5]._obj


@pytest.mark.parametrize("operation", ["LockFileEx", "UnlockFileEx"])
def test_windows_lease_propagates_native_failures(
    bootstrap: Any, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    kernel = _fake_windows_api(monkeypatch)
    getattr(kernel, operation).result = 0
    with pytest.raises(OSError) as raised:
        lease = bootstrap._WindowsSharedLease(types.SimpleNamespace(fileno=lambda: 7))
        lease.release()
    assert raised.value.errno == 33


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows byte-lock semantics")
def test_native_shared_leases_coexist_and_exclude_updater(
    bootstrap: Any, tmp_path: Path
) -> None:
    """Exercise real LockFileEx, including the parent's exclusive lock probe."""
    import msvcrt

    path = tmp_path / "lease.v1"
    path.write_bytes(b"lease\n")
    with path.open("rb") as parent, path.open("rb") as child, path.open("rb") as probe:
        first = bootstrap._WindowsSharedLease(parent)
        second = bootstrap._WindowsSharedLease(child)
        overlapped = type(first._overlapped)()
        try:
            result = first._kernel.LockFileEx(
                msvcrt.get_osfhandle(probe.fileno()), 3, 0, 1, 0,
                ctypes.byref(overlapped),
            )
            assert result == 0 and ctypes.get_last_error() == 33
        finally:
            first.release()
            second.release()
        assert first._kernel.LockFileEx(
            msvcrt.get_osfhandle(probe.fileno()), 3, 0, 1, 0,
            ctypes.byref(overlapped),
        )
        assert first._kernel.UnlockFileEx(
            msvcrt.get_osfhandle(probe.fileno()), 0, 1, 0,
            ctypes.byref(overlapped),
        )
