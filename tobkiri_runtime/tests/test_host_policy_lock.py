"""Policy locks pin directory and lock identity, not unrelated directory entries."""

import os

import pytest

from core_runtime.pack_artifact_integrity import (
    _write_private_policy,
    exclusive_host_policy_lock,
    host_policy_identity,
    read_host_policy_snapshot,
)

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Host policy traversal is POSIX")


def test_sibling_locks_and_artifact_directories_do_not_invalidate_policy_lock(tmp_path):
    policy = tmp_path / "publisher_trust.v4.json"
    with (
        exclusive_host_policy_lock(policy) as held,
        exclusive_host_policy_lock(tmp_path / "external_normal_pack_catalog.v4.json"),
    ):
        assert read_host_policy_snapshot(policy, allow_missing=True, policy_lock=held) == {}
        (tmp_path / "artifacts").mkdir()
        assert read_host_policy_snapshot(policy, allow_missing=True, policy_lock=held) == {}
        sibling = tmp_path / "catalog-temporary"
        sibling.write_text("temporary", encoding="utf-8")
        sibling.replace(tmp_path / "catalog-final")
        assert read_host_policy_snapshot(policy, allow_missing=True, policy_lock=held) == {}


@pytest.mark.parametrize("replacement", ["directory", "symlink"])
def test_replaced_policy_parent_is_rejected(tmp_path, replacement):
    parent = tmp_path / "policy"
    parent.mkdir(mode=0o700)
    policy = parent / "trust.json"
    with exclusive_host_policy_lock(policy) as held:
        moved = tmp_path / "displaced"
        parent.rename(moved)
        if replacement == "symlink":
            parent.symlink_to(moved, target_is_directory=True)
        else:
            parent.mkdir(mode=0o700)
        with pytest.raises((OSError, ValueError)):
            read_host_policy_snapshot(policy, allow_missing=True, policy_lock=held)


def test_symlinked_ancestor_to_same_pinned_parent_is_rejected(tmp_path):
    ancestor = tmp_path / "ancestor"
    parent = ancestor / "policy"
    parent.mkdir(parents=True, mode=0o700)
    policy = parent / "trust.json"
    with exclusive_host_policy_lock(policy) as held:
        moved = tmp_path / "moved"
        ancestor.rename(moved)
        ancestor.symlink_to(moved, target_is_directory=True)
        with pytest.raises((OSError, ValueError)):
            read_host_policy_snapshot(policy, allow_missing=True, policy_lock=held)


def test_policy_parent_permission_change_is_rejected(tmp_path):
    parent = tmp_path / "policy"
    parent.mkdir(mode=0o700)
    policy = parent / "trust.json"
    with exclusive_host_policy_lock(policy) as held:
        parent.chmod(0o777)
        with pytest.raises((OSError, ValueError)):
            read_host_policy_snapshot(policy, allow_missing=True, policy_lock=held)


@pytest.mark.parametrize("replacement", ["file", "symlink", "hardlink"])
def test_replaced_lock_file_is_rejected(tmp_path, replacement):
    policy = tmp_path / "trust.json"
    with exclusive_host_policy_lock(policy) as held:
        lock_file = tmp_path / ".trust.json.lock"
        moved = tmp_path / "old-lock"
        lock_file.rename(moved)
        if replacement == "symlink":
            lock_file.symlink_to(moved)
        elif replacement == "hardlink":
            os.link(moved, lock_file)
        else:
            lock_file.touch(mode=0o600)
        with pytest.raises((OSError, ValueError)):
            read_host_policy_snapshot(policy, allow_missing=True, policy_lock=held)


def test_same_lock_remains_valid_after_its_own_atomic_policy_write(tmp_path):
    policy = tmp_path / "trust.json"
    with exclusive_host_policy_lock(policy) as held:
        _write_private_policy(
            policy, {"publishers": {}}, expected_file_identity=None,
            expected_policy_identity=host_policy_identity({}), policy_lock=held,
        )
        assert read_host_policy_snapshot(policy, policy_lock=held) == {"publishers": {}}


def test_lock_cannot_be_used_for_another_policy_target(tmp_path):
    with (
        exclusive_host_policy_lock(tmp_path / "trust.json") as held,
        pytest.raises(ValueError, match="target does not match"),
    ):
        read_host_policy_snapshot(
            tmp_path / "other.json", allow_missing=True, policy_lock=held
        )
