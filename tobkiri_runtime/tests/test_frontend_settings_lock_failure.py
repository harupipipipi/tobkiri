"""Settings recovery and transactions require an actual OS lock."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import sys
import threading
import time

import pytest

from ecosystem.tobkiri_ui_settings_pack.runtime import store as settings


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock semantics")
def test_posix_lock_wait_cancellation_never_enters_transaction(tmp_path: Path) -> None:
    import fcntl

    store = settings.FrontendSettingsStore(tmp_path / "settings.json")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    cancellation = threading.Event()
    with store.lock_path.open("a+b") as held_lock:
        fcntl.flock(held_lock.fileno(), fcntl.LOCK_EX)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                store.compare_and_swap_document,
                {},
                expected_revision=0,
                lock_cancellation=cancellation,
                lock_deadline=time.monotonic() + 10,
            )
            time.sleep(0.05)
            cancellation.set()
            with pytest.raises(InterruptedError):
                future.result(timeout=1)
    assert not store.path.exists()


@pytest.mark.parametrize("platform", ["posix", "nt"])
@pytest.mark.parametrize("failure", ["missing", "denied"])
@pytest.mark.parametrize("operation", ["read", "update", "mutate_state"])
def test_lock_failure_never_enters_recovery_or_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    platform: str, failure: str, operation: str,
) -> None:
    store = settings.FrontendSettingsStore(tmp_path / "settings.json")
    store.path.write_bytes(b"{broken")
    store.backup_path.write_bytes(b'{"unrecognized": {"keep": true}}')
    before = (store.path.read_bytes(), store.backup_path.read_bytes())
    calls = []

    def denied(*args: object) -> None:
        calls.append(args)
        raise PermissionError("lock unavailable")

    # Override this module's platform selection, not global sys.platform/Path.
    monkeypatch.setattr(settings, "sys", SimpleNamespace(platform="win32" if platform == "nt" else "linux"))
    monkeypatch.setattr(settings, "_ensure_lock_byte", lambda _: None)
    monkeypatch.setattr(settings.time, "sleep", lambda _: None)
    module = "msvcrt" if platform == "nt" else "fcntl"
    backend = SimpleNamespace(
        flock=denied, locking=denied, LOCK_EX=1, LK_NBLCK=1,
    )
    monkeypatch.setitem(sys.modules, module, None if failure == "missing" else backend)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("transaction entered without its OS lock")

    monkeypatch.setattr(store, "_read_locked", forbidden)
    monkeypatch.setattr(store, "_atomic_write", forbidden)
    with pytest.raises((ImportError, PermissionError, TimeoutError)):
        if operation == "read":
            store.read()
        elif operation == "update":
            store.update(forbidden)
        else:
            store.mutate_state("models.preference", forbidden)
    assert (store.path.read_bytes(), store.backup_path.read_bytes()) == before
    assert len(calls) == (0 if failure == "missing" else 400 if platform == "nt" else 1)


@pytest.mark.parametrize("platform", ["posix", "nt"])
def test_unlock_error_is_reported_and_file_descriptor_is_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str,
) -> None:
    store = settings.FrontendSettingsStore(tmp_path / "settings.json")
    handles = []

    def acquired(handle: object, **_: object) -> None:
        handles.append(handle)

    def denied(*args: object) -> None:
        raise OSError("unlock failed")

    monkeypatch.setattr(settings, "sys", SimpleNamespace(platform="win32" if platform == "nt" else "linux"))
    monkeypatch.setattr(settings, "_lock_file_handle", acquired)
    monkeypatch.setitem(sys.modules, "msvcrt" if platform == "nt" else "fcntl",
                        SimpleNamespace(flock=denied, locking=denied, LOCK_UN=2, LK_UNLCK=2))
    with pytest.raises(OSError, match="unlock failed"):
        with store._locked():
            pass
    assert len(handles) == 1
    assert handles[0].closed
