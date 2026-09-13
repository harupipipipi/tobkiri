from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "cleanup_macos_dmg_workspace.py"


def _load_helper():
    spec = importlib.util.spec_from_file_location("dmg_workspace_tests", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


HELPER = _load_helper()


def _create(parent: Path) -> tuple[Path, int, int]:
    parent = parent.resolve()
    workspace = parent / f"{HELPER.WORKSPACE_PREFIX}fixture"
    workspace.mkdir(mode=0o700)
    metadata = workspace.lstat()
    return workspace, metadata.st_dev, metadata.st_ino


def test_cleanup_removes_identity_bound_read_only_tree_and_is_idempotent(
    tmp_path: Path,
) -> None:
    workspace, device, inode = _create(tmp_path)
    nested = workspace / "sealed" / "nested"
    nested.mkdir(parents=True)
    artifact = nested / "artifact.py"
    artifact.write_text("sealed", encoding="utf-8")
    artifact.chmod(0o444)
    nested.chmod(0o555)
    nested.parent.chmod(0o555)

    HELPER.cleanup_workspace(tmp_path.resolve(), workspace, device, inode)
    HELPER.cleanup_workspace(tmp_path.resolve(), workspace, device, inode)

    assert not workspace.exists()


def test_cleanup_rejects_nested_symlink_and_preserves_external_victim(
    tmp_path: Path,
) -> None:
    workspace, device, inode = _create(tmp_path)
    victim = tmp_path / "victim"
    victim.mkdir()
    marker = victim / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    link = workspace / "external"
    link.symlink_to(victim, target_is_directory=True)

    with pytest.raises(Exception, match="symlink|linked"):
        HELPER.cleanup_workspace(tmp_path.resolve(), workspace, device, inode)

    assert marker.read_text(encoding="utf-8") == "keep"
    link.unlink()
    HELPER.cleanup_workspace(tmp_path.resolve(), workspace, device, inode)


def test_cleanup_rejects_root_swap_wrong_identity_and_symlink_parent(
    tmp_path: Path,
) -> None:
    workspace, device, inode = _create(tmp_path)
    victim = tmp_path / "victim"
    victim.mkdir()
    marker = victim / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    held = workspace.with_name(f"{workspace.name}.held")
    workspace.rename(held)
    workspace.symlink_to(victim, target_is_directory=True)

    with pytest.raises(Exception, match="creation-time identity"):
        HELPER.cleanup_workspace(tmp_path.resolve(), workspace, device, inode)

    alias = tmp_path / "parent-alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="canonical real directory"):
        HELPER.cleanup_workspace(alias, held, device, inode)

    assert marker.read_text(encoding="utf-8") == "keep"
    workspace.unlink()
    HELPER.cleanup_workspace(tmp_path.resolve(), held, device, inode)


def test_cleanup_rejects_hardlinked_external_file(tmp_path: Path) -> None:
    workspace, device, inode = _create(tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep", encoding="utf-8")
    os.link(victim, workspace / "hardlink.txt")

    with pytest.raises(Exception, match="hard-linked"):
        HELPER.cleanup_workspace(tmp_path.resolve(), workspace, device, inode)

    assert victim.read_text(encoding="utf-8") == "keep"
