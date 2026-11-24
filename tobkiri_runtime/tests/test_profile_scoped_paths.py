from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core_runtime.profile_paths import (
    active_profile_id,
    profile_database_path,
    profile_user_data_dir,
    resolve_runtime_database_path,
    resolve_runtime_user_data_dir,
)


def test_profile_scoped_paths_use_only_verified_v4_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(tmp_path))
    monkeypatch.setenv("RUMI_ACTIVE_PROFILE_ID", "forged-environment-profile")
    monkeypatch.setattr(
        "core_runtime.active_profile_store_v4.ActiveProfileStore.require",
        lambda _store, **_kwargs: SimpleNamespace(profile_id="defaults"),
    )

    assert active_profile_id() == "defaults"
    assert resolve_runtime_user_data_dir() == tmp_path / "workspaces" / "defaults"
    assert resolve_runtime_database_path() == (
        tmp_path / "workspaces" / "defaults" / "state" / "rumi.sqlite"
    )


def test_profile_scoped_paths_fail_closed_without_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(tmp_path))
    monkeypatch.setattr(
        "core_runtime.active_profile_store_v4.ActiveProfileStore.require",
        lambda _store, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("not activated")
        ),
    )

    assert active_profile_id() is None
    with pytest.raises(RuntimeError, match="v4 Profile activation"):
        resolve_runtime_user_data_dir()
    with pytest.raises(RuntimeError, match="v4 Profile activation"):
        resolve_runtime_database_path()
    assert not (tmp_path / "rumi.sqlite").exists()


def test_explicit_profile_helpers_are_workspace_scoped(tmp_path: Path) -> None:
    assert profile_user_data_dir("p2", tmp_path) == tmp_path / "workspaces" / "p2"
    assert profile_database_path("p2", tmp_path) == (
        tmp_path / "workspaces" / "p2" / "state" / "rumi.sqlite"
    )
