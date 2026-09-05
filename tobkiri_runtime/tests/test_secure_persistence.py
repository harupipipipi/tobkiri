"""Adversarial contracts for pinned local persistence."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest

import tobkiri_protocol.secure_persistence as persistence
from tobkiri_protocol.secure_persistence import (
    SecureDirectory,
    SecurePersistenceError,
)


def test_missing_leaf_is_preserved_for_exists_and_read(tmp_path: Path) -> None:
    store = SecureDirectory(tmp_path / "root")

    assert store.exists("approval.json") is False
    with pytest.raises(FileNotFoundError):
        store.read_bytes("approval.json")


def test_bounded_read_rejects_oversized_entry_before_returning_bytes(
    tmp_path: Path,
) -> None:
    store = SecureDirectory(tmp_path / "root")
    store.write_bytes_atomic("artifact.bin", b"oversized")

    with pytest.raises(SecurePersistenceError, match="exceeds read limit"):
        store.read_bytes_bounded("artifact.bin", max_bytes=4)

    assert store.read_bytes_bounded("artifact.bin", max_bytes=9) == b"oversized"


def test_missing_nested_parent_is_not_treated_as_a_missing_leaf(
    tmp_path: Path,
) -> None:
    store = SecureDirectory(tmp_path / "root")

    with pytest.raises(SecurePersistenceError, match="directory.*unsafe"):
        store.exists("missing/approval.json")
    with pytest.raises(SecurePersistenceError, match="directory.*unsafe"):
        store.read_bytes("missing/approval.json")


def test_write_and_lock_create_missing_leaf_and_nested_parent(tmp_path: Path) -> None:
    store = SecureDirectory(tmp_path / "root")

    store.write_bytes_atomic("state/active.json", b"state")
    assert store.read_bytes("state/active.json") == b"state"
    descriptor = store.open_lock("locks/active.lock")
    try:
        store.validate_open_file("locks/active.lock", descriptor)
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("operation", ["exists", "read", "lock", "write"])
def test_all_operations_reject_replaced_captured_ancestor(
    tmp_path: Path,
    operation: str,
) -> None:
    parent = tmp_path / "owner"
    store = SecureDirectory(parent / "state")
    store.write_bytes_atomic("entry", b"old")
    displaced = tmp_path / "displaced"
    parent.rename(displaced)
    parent.mkdir()
    (parent / "state").mkdir()

    actions = {
        "exists": lambda: store.exists("entry"),
        "read": lambda: store.read_bytes("entry"),
        "lock": lambda: store.open_lock("entry"),
        "write": lambda: store.write_bytes_atomic("entry", b"new"),
    }
    with pytest.raises(SecurePersistenceError, match="ancestor identity changed"):
        actions[operation]()

    assert (displaced / "state" / "entry").read_bytes() == b"old"
    assert not (parent / "state" / "entry").exists()


def test_captured_ancestor_replacement_fails_closed(tmp_path: Path) -> None:
    parent = tmp_path / "owner"
    store = SecureDirectory(parent / "state")
    store.write_bytes_atomic("active.json", b"old")
    displaced = tmp_path / "displaced"
    parent.rename(displaced)
    parent.mkdir()
    (parent / "state").mkdir()

    with pytest.raises(SecurePersistenceError, match="ancestor identity changed"):
        store.write_bytes_atomic("active.json", b"new")

    assert (displaced / "state" / "active.json").read_bytes() == b"old"
    assert not (parent / "state" / "active.json").exists()


@pytest.mark.parametrize("entry", ["state.json", "approvals/pack.json", "lock"])
def test_hardlinked_entries_are_rejected(tmp_path: Path, entry: str) -> None:
    store = SecureDirectory(tmp_path / "root")
    outside = tmp_path / "outside"
    outside.write_bytes(b"untrusted")
    target = store.root / entry
    target.parent.mkdir(parents=True, exist_ok=True)
    os.link(outside, target)

    action = (
        (lambda: store.open_lock(entry)) if entry == "lock" else (lambda: store.read_bytes(entry))
    )
    with pytest.raises(SecurePersistenceError, match="identity is unsafe"):
        action()


def test_non_regular_and_non_owner_entries_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SecureDirectory(tmp_path / "root")
    fifo = store.root / "state.json"
    os.mkfifo(fifo)
    with pytest.raises(SecurePersistenceError, match="identity is unsafe"):
        store.read_bytes("state.json")

    fifo.unlink()
    fifo.write_bytes(b"state")
    actual_uid = os.getuid()
    monkeypatch.setattr(persistence.os, "getuid", lambda: actual_uid + 1)
    with pytest.raises(SecurePersistenceError, match="identity is unsafe"):
        store.read_bytes("state.json")


def test_entry_replacement_during_read_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SecureDirectory(tmp_path / "root")
    store.write_bytes_atomic("state.json", b"trusted")
    target = store.root / "state.json"
    displaced = store.root / "state.displaced"
    real_read = persistence.os.read
    replaced = False

    def read(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        result = real_read(descriptor, size)
        if result and not replaced:
            replaced = True
            target.rename(displaced)
            target.write_bytes(b"replacement")
        return result

    monkeypatch.setattr(persistence.os, "read", read)
    with pytest.raises(SecurePersistenceError, match="changed during read"):
        store.read_bytes("state.json")


def test_destination_replacement_before_commit_preserves_current_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SecureDirectory(tmp_path / "root")
    store.write_bytes_atomic("active.json", b"old")
    target = store.root / "active.json"
    real_stat = persistence.os.stat
    checks = 0

    def stat_entry(path: object, *args: object, **kwargs: object) -> os.stat_result:
        nonlocal checks
        if path == "active.json" and kwargs.get("dir_fd") is not None:
            checks += 1
            if checks == 2:
                target.replace(store.root / "old.displaced")
                target.write_bytes(b"attacker")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(persistence.os, "stat", stat_entry)
    with pytest.raises(
        SecurePersistenceError,
        match="destination changed before publication",
    ):
        store.write_bytes_atomic("active.json", b"new")

    assert target.read_bytes() == b"attacker"
    assert (store.root / "old.displaced").read_bytes() == b"old"
    assert not list(store.root.glob(".active.json.*.tmp"))


def test_nested_symlink_directory_is_rejected_without_touching_victim(
    tmp_path: Path,
) -> None:
    store = SecureDirectory(tmp_path / "root")
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "active.json").write_bytes(b"outside")
    try:
        (store.root / "activations").symlink_to(victim, target_is_directory=True)
    except OSError:
        if os.name == "nt":
            pytest.skip("directory symlink creation unavailable")
        raise

    with pytest.raises(SecurePersistenceError, match="unsafe|symlink|reparse"):
        store.write_bytes_atomic("activations/active.json", b"attacker")

    assert (victim / "active.json").read_bytes() == b"outside"


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin-only alias contract")
def test_darwin_temporary_directory_unresolved_path_supports_all_operations() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        unresolved = Path(temporary)
        store = SecureDirectory(unresolved / "state")
        store.write_bytes_atomic("nested/state.json", b"state")
        assert store.exists("nested/state.json")
        assert store.read_bytes("nested/state.json") == b"state"
        lock = store.open_lock("locks/state.lock")
        try:
            store.validate_open_file("locks/state.lock", lock)
        finally:
            os.close(lock)


@pytest.mark.skipif(os.name != "nt", reason="native Windows junction contract")
def test_windows_nested_junction_is_rejected_without_touching_victim(
    tmp_path: Path,
) -> None:
    store = SecureDirectory(tmp_path / "root")
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "active.json").write_bytes(b"outside")
    junction = store.root / "activations"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(victim)],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"junction creation unavailable: {result.stderr.strip()}")

    with pytest.raises(SecurePersistenceError, match="reparse"):
        store.write_bytes_atomic("activations/active.json", b"attacker")

    assert (victim / "active.json").read_bytes() == b"outside"


@pytest.mark.skipif(os.name != "nt", reason="native Windows handle race contract")
def test_windows_nested_directory_handle_blocks_replacement_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SecureDirectory(tmp_path / "root")
    nested = store.root / "activations"
    nested.mkdir()
    victim = tmp_path / "victim"
    victim.mkdir()
    displaced = store.root / "displaced"
    real_open = persistence._windows_open_directory
    replacement_blocked = False

    def racing_open(path: Path) -> tuple[object, tuple[int, int]]:
        nonlocal replacement_blocked
        opened = real_open(path)
        if path == nested and not replacement_blocked:
            try:
                nested.rename(displaced)
            except OSError:
                replacement_blocked = True
            else:
                persistence._windows_close_handle(opened[0])
                pytest.fail("pinned nested directory was replaceable")
        return opened

    monkeypatch.setattr(persistence, "_windows_open_directory", racing_open)
    store.write_bytes_atomic("activations/active.json", b"inside")

    assert replacement_blocked
    assert (nested / "active.json").read_bytes() == b"inside"
    assert not (victim / "active.json").exists()
