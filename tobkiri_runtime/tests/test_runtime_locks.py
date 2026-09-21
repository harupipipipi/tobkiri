from __future__ import annotations


import pytest

from core_runtime.runtime_locks import FileLock, LockTimeout, NamedLock


def test_file_lock_acquire_release(tmp_path):
    lock_path = tmp_path / "session.lock"

    with FileLock(lock_path, owner="test", timeout_ms=100):
        assert lock_path.exists()

    assert not lock_path.exists()


def test_file_lock_times_out_when_held(tmp_path):
    lock_path = tmp_path / "session.lock"

    with FileLock(lock_path, owner="first", timeout_ms=100):
        with pytest.raises(LockTimeout):
            FileLock(
                lock_path, owner="second", timeout_ms=10, poll_interval=0.001
            ).acquire()


def test_file_lock_breaks_stale_lock(tmp_path):
    lock_path = tmp_path / "session.lock"

    lock_path.write_text(
        '{"owner":"old","pid":1,"acquired_at":1,"stale_after_seconds":0.001}',
        encoding="utf-8",
    )
    with FileLock(lock_path, owner="new", timeout_ms=100, stale_after_seconds=0.001):
        info = lock_path.read_text(encoding="utf-8")
        assert '"new"' in info


def test_named_lock_sanitizes_name(tmp_path):
    with NamedLock(tmp_path, "agent:main/channel", owner="test", timeout_ms=100):
        assert list(tmp_path.glob("*.lock"))


@pytest.mark.parametrize("secure_first", [True, False])
def test_pinned_lock_preserves_mutual_exclusion_with_legacy_writer(
    tmp_path, secure_first
):
    from tobkiri_protocol.secure_persistence import SecureDirectory

    directory = SecureDirectory(tmp_path / "locks")
    pinned = NamedLock(
        directory.root, "shared", owner="pinned", timeout_ms=0, directory=directory
    )
    legacy = NamedLock(directory.root, "shared", owner="legacy", timeout_ms=0)
    first, second = (pinned, legacy) if secure_first else (legacy, pinned)
    with first:
        before = (directory.root / "shared.lock").read_bytes()
        with pytest.raises(LockTimeout):
            second.acquire()
        assert (directory.root / "shared.lock").read_bytes() == before
    with second:
        assert (directory.root / "shared.lock").exists()
    assert not (directory.root / "shared.lock").exists()
