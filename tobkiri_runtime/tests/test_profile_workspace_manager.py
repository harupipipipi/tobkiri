from __future__ import annotations

from pathlib import Path

import pytest

from core_runtime.profile_workspace import ProfileWorkspaceManager


def _profile(profile_id: str = "default-profile") -> dict:
    return {
        "version": 4,
        "profile_id": profile_id,
        "name": "Default Profile",
        "base_pack": "defaultspack",
    }


def test_initialize_profile_workspace_creates_expected_tree(tmp_path: Path):
    manager = ProfileWorkspaceManager(tmp_path)
    paths = manager.initialize_profile_workspace(_profile())

    assert paths.root == tmp_path / "workspaces" / "default-profile"
    assert paths.state_dir.is_dir()
    assert paths.database_path.is_file()
    assert (paths.state_dir / "workspace.json").is_file()
    assert paths.artifacts_dir.is_dir()
    assert paths.snapshots_dir.is_dir()
    assert (paths.audit_dir / "events.jsonl").is_file()
    assert not list(paths.root.rglob("*.yaml"))


@pytest.mark.parametrize("profile_id", ["", "../x", "x/../y", "x\\y", "abc..def"])
def test_profile_id_rejects_path_traversal(tmp_path: Path, profile_id: str):
    manager = ProfileWorkspaceManager(tmp_path)
    with pytest.raises(ValueError):
        manager.paths_for_profile(profile_id)


def test_profile_database_path_is_profile_scoped(tmp_path: Path):
    manager = ProfileWorkspaceManager(tmp_path)
    assert manager.profile_database_path("p1") == tmp_path / "workspaces" / "p1" / "state" / "rumi.sqlite"


def test_profile_user_data_dir_is_profile_scoped(tmp_path: Path):
    manager = ProfileWorkspaceManager(tmp_path)
    assert manager.profile_user_data_dir("p1") == tmp_path / "workspaces" / "p1" / "state"
