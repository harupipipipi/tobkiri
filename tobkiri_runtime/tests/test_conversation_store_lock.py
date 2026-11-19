"""Exercise the shared lock protocol against the filesystem and legacy owner."""

from __future__ import annotations

import errno
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from core_runtime import conversation_store_lock as locking
from core_runtime.runtime_locks import FileLock, LockTimeout

pytestmark = pytest.mark.skipif(
    sys.platform not in {"darwin", "linux"}, reason="directory flock protocol"
)


def _payload(**changes: Any) -> bytes:
    return json.dumps(
        {
            "owner": "legacy",
            "pid": os.getpid(),
            "acquired_at": 1.0,
            "stale_after_seconds": None,
        }
        | changes
    ).encode()


def test_live_owner_keeps_new_and_legacy_callers_out(tmp_path: Path) -> None:
    path = tmp_path / "conversations.lock"
    with locking.conversation_store_lock(tmp_path, timeout_ms=0):
        original = path.read_bytes()
        assert json.loads(original)["lease"] == "directory-flock-v1"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        with pytest.raises(LockTimeout), locking.conversation_store_lock(
            tmp_path, timeout_ms=0
        ):
            pytest.fail("second directory lease acquired")
        with pytest.raises(LockTimeout), FileLock(path, timeout_ms=0):
            pytest.fail("legacy lock acquired during a directory lease")
        assert path.read_bytes() == original
    assert not path.exists()
    assert not list(tmp_path.iterdir())


def test_legacy_record_is_reclaimed_only_after_bare_process_exit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "conversations.lock"
    subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            "import os, sys; from core_runtime.runtime_locks import FileLock; "
            "FileLock(sys.argv[1]).acquire(); os._exit(0)",
            str(path),
        ],
        cwd=Path(__file__).resolve().parents[1],
        timeout=5,
        check=True,
    )
    legacy = json.loads(path.read_bytes())
    assert "lease" not in legacy
    with locking.conversation_store_lock(tmp_path, timeout_ms=0):
        replaced = json.loads(path.read_bytes())
        assert replaced["pid"] == os.getpid() != legacy["pid"]
    assert not path.exists()


def test_marked_record_is_reclaimed_even_when_its_pid_is_live(tmp_path: Path) -> None:
    path = tmp_path / "conversations.lock"
    path.write_bytes(_payload(lease="directory-flock-v1"))
    with locking.conversation_store_lock(tmp_path, timeout_ms=0):
        assert json.loads(path.read_bytes())["owner"] == f"pid:{os.getpid()}"
    assert not path.exists()


def test_legacy_permission_denied_is_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "conversations.lock"
    path.write_bytes(_payload())

    def denied(pid: int, sig: int) -> None:
        assert pid == os.getpid() and sig == 0
        raise PermissionError(errno.EPERM, "permission denied")

    monkeypatch.setattr(locking.os, "kill", denied)
    with pytest.raises(LockTimeout), locking.conversation_store_lock(
        tmp_path, timeout_ms=0
    ):
        pytest.fail("ambiguous lock acquired")
    assert path.read_bytes() == _payload()


@pytest.mark.parametrize(
    "record",
    [
        _payload(),  # A live legacy PID remains authoritative regardless of age.
        b"",
        b'{"owner":',
        _payload(pid=0, lease="directory-flock-v1"),
        _payload(pid=True, lease="directory-flock-v1"),
        _payload(owner="\ud800", lease="directory-flock-v1"),
        _payload(acquired_at=float("inf"), lease="directory-flock-v1"),
        _payload(stale_after_seconds=1, lease="directory-flock-v1"),
        _payload(lease="unknown"),
        _payload(lease="directory-flock-v1", extra=True),
        _payload(lease="directory-flock-v1")[:-1] + b',"owner":"duplicate"}',
        _payload(lease="directory-flock-v1") + b" " * 4096,
    ],
    ids=[
        "live-legacy",
        "empty",
        "partial",
        "pid-zero",
        "pid-bool",
        "owner-surrogate",
        "nonfinite-clock",
        "expiry",
        "unknown-marker",
        "extra-field",
        "duplicate-field",
        "over-limit",
    ],
)
def test_unproved_records_are_left_intact(tmp_path: Path, record: bytes) -> None:
    path = tmp_path / "conversations.lock"
    path.write_bytes(record)
    with pytest.raises(LockTimeout), locking.conversation_store_lock(
        tmp_path, timeout_ms=0
    ):
        pytest.fail("unproved record was reclaimed")
    assert path.read_bytes() == record


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "group-writable", "fifo"])
def test_unsafe_record_is_left_intact(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "conversations.lock"
    target = tmp_path / "target"
    target.write_bytes(_payload(lease="directory-flock-v1"))
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, path)
    elif kind == "group-writable":
        target.rename(path)
        path.chmod(0o620)
    else:
        os.mkfifo(path)
    before = path.lstat()
    with pytest.raises(LockTimeout), locking.conversation_store_lock(
        tmp_path, timeout_ms=0
    ):
        pytest.fail("unsafe record was reclaimed")
    assert path.lstat() == before


@pytest.mark.parametrize("kind", ["symlink", "group-writable"])
def test_unsafe_lock_directory_rejects_admission(tmp_path: Path, kind: str) -> None:
    root = tmp_path / "locks"
    if kind == "symlink":
        root.symlink_to(tmp_path, target_is_directory=True)
    else:
        root.mkdir()
        root.chmod(0o770)
    with pytest.raises(OSError), locking.conversation_store_lock(root, timeout_ms=0):
        pytest.fail("unsafe directory admitted owner")
    assert not (root / "conversations.lock").exists()


def test_atomic_publication_preserves_legacy_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "conversations.lock"
    legacy = FileLock(path, timeout_ms=0)
    rename = locking._rename_noreplace

    def race(directory: int, source: str, destination: str) -> None:
        assert not path.exists()
        staged = tmp_path / source
        assert json.loads(staged.read_bytes())["lease"] == "directory-flock-v1"
        assert stat.S_IMODE(staged.stat().st_mode) == 0o600
        legacy.acquire()
        rename(directory, source, destination)

    monkeypatch.setattr(locking, "_rename_noreplace", race)
    try:
        with pytest.raises(LockTimeout), locking.conversation_store_lock(
            tmp_path, timeout_ms=0
        ):
            pytest.fail("legacy winner was replaced")
        assert "lease" not in json.loads(path.read_bytes())
        assert list(tmp_path.iterdir()) == [path]
    finally:
        legacy.release()


def test_release_leaves_replacement_inode_intact(tmp_path: Path) -> None:
    path = tmp_path / "conversations.lock"
    legacy = _payload()
    with locking.conversation_store_lock(tmp_path, timeout_ms=0):
        path.unlink()
        path.write_bytes(legacy)
    assert path.read_bytes() == legacy


def test_recovery_leaves_replacement_inode_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "conversations.lock"
    path.write_bytes(_payload(lease="directory-flock-v1"))
    legacy = _payload()
    reclaimable = locking._reclaimable

    def replace(raw: bytes) -> bool:
        path.unlink()
        path.write_bytes(legacy)
        return reclaimable(raw)

    monkeypatch.setattr(locking, "_reclaimable", replace)
    with pytest.raises(LockTimeout), locking.conversation_store_lock(
        tmp_path, timeout_ms=0
    ):
        pytest.fail("new inode was reclaimed using the old inode's record")
    assert path.read_bytes() == legacy


def test_recovery_leaves_record_changed_in_place_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "conversations.lock"
    path.write_bytes(_payload(lease="directory-flock-v1"))
    legacy = _payload()
    reclaimable = locking._reclaimable

    def rewrite(raw: bytes) -> bool:
        path.write_bytes(legacy)
        return reclaimable(raw)

    monkeypatch.setattr(locking, "_reclaimable", rewrite)
    with pytest.raises(LockTimeout), locking.conversation_store_lock(
        tmp_path, timeout_ms=0
    ):
        pytest.fail("changed inode was reclaimed using the old record")
    assert path.read_bytes() == legacy
