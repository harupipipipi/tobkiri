"""Pack v4 setup regressions replacing the retired core_setup Profile tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core_runtime.app_lifecycle_manager import AppLifecycleManager
from core_runtime.bootstrap.profile_capture import (
    _bundle_root,
    capture_default_profile,
    prepare_default_profile_confirmation,
)
from core_runtime.profile_definition_store_v4 import ProfileDefinitionStore
from ecosystem.defaultspack.domain.runtime_v4 import BundledCatalog


def test_legacy_profile_json_is_not_setup_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid-looking v3 Profile cannot complete canonical setup."""

    user_data = tmp_path / "user-data"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    profile_path = user_data / "settings" / "profile.json"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "username": "legacy-user",
                "language": "ja",
                "setup_completed": True,
            }
        ),
        encoding="utf-8",
    )

    status = AppLifecycleManager(base_dir=tmp_path).check_setup_status()

    assert status["needs_setup"] is True
    assert status["reason"] == "explicit_bootstrap_confirmation_required"
    assert status["defaults_bootstrap_required"] is True
    assert status["host_catalog_verified"] is True
    assert status["profile_ceremony_available"] is False


def test_existing_named_catalog_needs_activation_not_defaults_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_data = tmp_path / "user-data"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    template = dict(BundledCatalog.load(_bundle_root()).profiles["defaults"])
    template["profile_id"] = "existing-profile"
    template["display_name"] = "Existing Profile"
    ProfileDefinitionStore(user_data).create_profile(template)

    status = AppLifecycleManager(base_dir=tmp_path).check_setup_status()

    assert status["needs_setup"] is False
    assert status["reason"] == "profile_activation_required"
    assert status["host_catalog_verified"] is True
    assert status["profile_ceremony_available"] is True
    assert status["defaults_bootstrap_required"] is False
    assert status["active_profile_ready"] is False
    assert status["launch_ready"] is False


def test_committed_v4_activation_is_the_only_setup_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setup status is derived from the immutable Authority-owned activation."""

    user_data = tmp_path / "user-data"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    active = capture_default_profile(
        confirmation=prepare_default_profile_confirmation()
    )

    status = AppLifecycleManager(base_dir=tmp_path).check_setup_status()

    assert status["needs_setup"] is False
    assert status["profile_id"] == active.resolved.profile["profile_id"]
    assert status["plan_digest"] == active.resolved.plan["plan_digest"]
    assert status["activation_id"] == active.activation["activation_id"]


def test_complete_setup_never_creates_legacy_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The compatibility method verifies v4 state without a Profile write."""

    user_data = tmp_path / "user-data"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    capture_default_profile(confirmation=prepare_default_profile_confirmation())
    legacy_path = user_data / "settings" / "profile.json"

    result = AppLifecycleManager(base_dir=tmp_path).complete_setup(
        {"username": "ignored", "language": "en"}
    )

    assert result["success"] is True
    assert result["setup_state"] == "complete"
    assert legacy_path.exists() is False


def test_complete_setup_rejects_invalid_compatibility_payload_without_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid compatibility input fails before touching either state model."""

    user_data = tmp_path / "user-data"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))

    result = AppLifecycleManager(base_dir=tmp_path).complete_setup(
        {"username": "", "language": "invalid"}
    )

    assert result["success"] is False
    assert result["setup_state"] == "invalid_request"
    assert list(user_data.rglob("*")) == []
