from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock
import sys

import pytest

# The legacy domain package is rooted inside Defaultspack for these tests.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ecosystem/defaultspack"))

from domain.frontend import registry as registry_module  # noqa: E402
from domain.frontend.registry import FrontendRegistry  # noqa: E402
from domain.frontend_settings_catalog import SettingsCatalogInputs, SettingsSections  # noqa: E402
from ecosystem.tobkiri_ui_settings_pack.runtime.store import FrontendSettingsStore  # noqa: E402


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FrontendRegistry:
    """Keep unrelated derived presentation data isolated and deterministic."""
    for name in (
        "provider_key_status",
        "provider_oauth_statuses",
        "codex_connection_status",
        "codex_app_server_status",
    ):
        monkeypatch.setattr(registry_module, name, lambda **kwargs: {})
    model = Mock()
    model.refresh_models_settings.side_effect = deepcopy
    monkeypatch.setattr(registry_module, "ModelRuntimeSettingsService", lambda *args: model)
    for name, method in (
        ("WebhookEndpointStore", "list_endpoints"),
        ("InputProfileRegistry", "list_profiles"),
        ("OutputProfileRegistry", "list_profiles"),
    ):
        fake = Mock()
        getattr(fake, method).return_value = []
        monkeypatch.setattr(registry_module, name, lambda *args, fake=fake: fake)
    value = FrontendRegistry(
        tmp_path, settings_owner=FrontendSettingsStore(tmp_path / "owned.json")
    )
    monkeypatch.setattr(value, "_template_catalog_metadata", lambda: {})
    monkeypatch.setattr(value, "_external_sources_summary", lambda: "")
    return value


def test_missing_preferences_default_to_visible_human_approval(registry: FrontendRegistry) -> None:
    tools = registry._refresh_derived_settings({})["tools"]
    assert tools["show_action_approval_control"] is True
    assert tools["action_approval_mode"] == "ask"
    assert tools["fixed_action_approval_mode"] == "ask"
    assert tools["approval_reviewer_model"] == ""


def test_hidden_fixed_mode_survives_actual_owner_serialization_reload(
    registry: FrontendRegistry,
    tmp_path: Path,
) -> None:
    original = {
        "tools": {
            "settings_version": 3,
            "show_action_approval_control": False,
            "action_approval_mode": "full",
            "fixed_action_approval_mode": "agent",
            "approval_reviewer_model": "registered-reviewer",
            "standard_permissions": {"execute": "confirm"},
            "legacy_marker": "keep",
        }
    }
    normalized = registry._refresh_derived_settings(original)
    path = tmp_path / "roundtrip.json"
    store = FrontendSettingsStore(path)
    saved = store.compare_and_swap_document(normalized, expected_revision=0)
    assert saved["tools"]["show_action_approval_control"] is False
    reloaded = registry._refresh_derived_settings(FrontendSettingsStore(path).read_snapshot())
    assert reloaded["tools"]["fixed_action_approval_mode"] == "agent"
    assert reloaded["tools"]["action_approval_mode"] == "full"
    assert reloaded["tools"]["approval_reviewer_model"] == "registered-reviewer"
    assert reloaded["tools"]["standard_permissions"]["execute"] == "confirm"
    assert reloaded["tools"]["legacy_marker"] == "keep"
    assert original["tools"]["standard_permissions"] == {"execute": "confirm"}


@pytest.mark.parametrize("invalid", [None, "unknown", [], {}, 1, True])
def test_malformed_modes_normalize_to_human_without_permission_change(
    registry: FrontendRegistry,
    invalid: object,
) -> None:
    tools = registry._refresh_derived_settings(
        {
            "tools": {
                "action_approval_mode": invalid,
                "fixed_action_approval_mode": invalid,
                "approval_reviewer_model": invalid,
                "standard_permissions": {"execute": "confirm", "delete": "block"},
            }
        }
    )["tools"]
    assert tools["action_approval_mode"] == tools["fixed_action_approval_mode"] == "ask"
    assert tools["approval_reviewer_model"] == (invalid.strip() if isinstance(invalid, str) else "")
    assert tools["standard_permissions"]["execute"] == "confirm"
    assert tools["standard_permissions"]["delete"] == "block"


def test_valid_unsupported_preference_is_preserved_without_authority(
    registry: FrontendRegistry,
) -> None:
    tools = registry._refresh_derived_settings(
        {
            "tools": {
                "show_action_approval_control": False,
                "fixed_action_approval_mode": "full",
                "standard_permissions": {"execute": "confirm", "delete": "block"},
            }
        }
    )["tools"]
    assert tools["fixed_action_approval_mode"] == "full"
    assert tools["standard_permissions"]["execute"] == "confirm"
    assert tools["standard_permissions"]["delete"] == "block"


def test_catalog_defaults_are_preferences_and_reviewer_is_not_authority() -> None:
    inputs = SettingsCatalogInputs([], [], [], [], [], [], [])
    sections = SettingsSections().build([], [], template_catalog={}, inputs=inputs)
    fields = {
        field["id"]: field
        for section in sections
        if section["id"] == "tools"
        for field in section["fields"]
    }
    assert fields["show_action_approval_control"]["default"] is True
    assert fields["action_approval_mode"]["default"] == "ask"
    assert fields["fixed_action_approval_mode"]["default"] == "ask"
    assert fields["approval_reviewer_model"]["default"] == ""
    assert "設定だけでは代理承認は有効になりません" in fields["approval_reviewer_model"]["help"]
    assert (
        "危険な操作や審査できない操作は、理由を知らせて停止します。"
        in fields["approval_reviewer_model"]["help"]
    )
