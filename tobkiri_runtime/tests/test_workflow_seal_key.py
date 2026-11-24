"""Private Workflow keys retain binary identity on every supported platform."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path

import pytest

from core_runtime.workflow_v4.models import WorkflowDenied
from core_runtime.workflow_v4.seal_key import load_or_create_workflow_key


def load(root: Path) -> bytes:
    return load_or_create_workflow_key(root / "workflow.sqlite3.key", database_path=root / "workflow.sqlite3")


def test_key_survives_reload_without_changing_binary_material(tmp_path):
    first = load(tmp_path)
    assert len(first) == 32
    assert load(tmp_path) == first
    assert (tmp_path / "workflow.sqlite3.key").read_bytes() == first


def test_concurrent_creators_use_one_published_key(tmp_path):
    with ThreadPoolExecutor(max_workers=4) as workers:
        keys = list(workers.map(lambda _: load(tmp_path), range(8)))
    assert len(set(keys)) == 1
    assert not list(tmp_path.glob(".workflow-key-*.tmp"))


@pytest.mark.parametrize("length", [0, 31, 33, 1024])
def test_invalid_existing_key_is_not_replaced(tmp_path, length):
    load(tmp_path)
    path = tmp_path / "workflow.sqlite3.key"
    path.write_bytes(b"x" * length)
    with pytest.raises(WorkflowDenied, match="invalid"):
        load(tmp_path)
    assert path.read_bytes() == b"x" * length


def test_missing_key_with_existing_database_requires_recovery(tmp_path):
    (tmp_path / "workflow.sqlite3").write_bytes(b"existing database")
    with pytest.raises(WorkflowDenied, match="missing"):
        load(tmp_path)
    assert not (tmp_path / "workflow.sqlite3.key").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode check")
def test_broad_posix_permissions_fail_without_silent_chmod(tmp_path):
    load(tmp_path)
    path = tmp_path / "workflow.sqlite3.key"
    path.chmod(0o644)
    with pytest.raises(WorkflowDenied, match="permissions"):
        load(tmp_path)
    assert path.stat().st_mode & 0o077 == 0o044


@pytest.mark.skipif(os.name == "nt", reason="unprivileged POSIX symlink fixture")
def test_symlink_key_and_ancestor_are_rejected(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    load(target)
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(WorkflowDenied):
        load(alias)
    (tmp_path / "workflow.sqlite3.key").symlink_to(target / "workflow.sqlite3.key")
    with pytest.raises(WorkflowDenied):
        load(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="real Windows DACL proof")
def test_windows_existing_inherited_acl_is_rejected_without_repair(tmp_path):
    path = tmp_path / "workflow.sqlite3.key"
    path.write_bytes(b"x" * 32)
    with pytest.raises(WorkflowDenied, match="unsafe"):
        load(tmp_path)
    assert path.read_bytes() == b"x" * 32


def test_binary_edge_bytes_are_preserved(tmp_path):
    load(tmp_path)
    path = tmp_path / "workflow.sqlite3.key"
    value = b"\0\xff\n\r\t " + bytes(range(26))
    assert len(value) == 32
    path.write_bytes(value)
    assert load(tmp_path) == value


def test_sealed_workflow_record_survives_close_and_restart(tmp_path):
    from core_runtime.workflow_v4.store import WorkflowStoreV4

    path = tmp_path / "workflow.sqlite3"
    first = WorkflowStoreV4(path)
    original = first.create_definition("fixture", {"steps": []})
    first.close()
    with_key = path.with_suffix(".sqlite3.key").read_bytes()
    second = WorkflowStoreV4(path)
    try:
        assert second.get_definition("fixture") == original
    finally:
        second.close()
    assert path.with_suffix(".sqlite3.key").read_bytes() == with_key
