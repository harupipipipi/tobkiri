from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from _defaultspack_test_isolation import is_pack_test_child, run_pack_test


class _FakeFrontendRegistry:
    updated_values = None

    def get_settings(self, lightweight=False):
        assert lightweight is False
        return {
            "sections": [
                {
                    "id": "general",
                    "label": "General",
                    "description": "Everyday settings",
                    "fields": [
                        {"id": "compact", "label": "Compact", "type": "toggle", "default": False},
                        {
                            "id": "theme",
                            "label": "Theme",
                            "type": "select",
                            "options": [{"value": "dark"}, {"value": "light"}],
                            "default": "dark",
                        },
                        {"id": "api_key", "label": "API key", "type": "api_key_setup"},
                        {"id": "advanced", "label": "Advanced", "type": "json", "default": {}},
                    ],
                }
            ],
            "values": {
                "general": {
                    "compact": False,
                    "theme": "dark",
                    "api_key": "must-not-leak",
                    "advanced": {"nested_token": "must-not-leak", "visible": "yes"},
                }
            },
        }

    def update_settings(self, values):
        type(self).updated_values = values
        return values


def test_settings_inspect_excludes_protected_fields_and_redacts_nested_values() -> None:
    if not is_pack_test_child():
        run_pack_test(
            Path(__file__),
            "test_settings_inspect_excludes_protected_fields_and_redacts_nested_values",
        )
        return

    from domain.tool import settings_tools

    with patch.object(settings_tools, "FrontendRegistry", _FakeFrontendRegistry):
        result = settings_tools.settings_inspect({"section_ids": ["general"]})

    assert result["is_error"] is False
    fields = result["widget"]["data"]["sections"][0]["fields"]
    assert {field["field_id"] for field in fields} == {"compact", "theme", "advanced"}
    advanced = next(field for field in fields if field["field_id"] == "advanced")
    assert advanced["current"] == {"nested_token": "[redacted]", "visible": "yes"}
    assert "must-not-leak" not in result["result"]


def test_settings_update_applies_only_valid_safe_fields() -> None:
    if not is_pack_test_child():
        run_pack_test(
            Path(__file__),
            "test_settings_update_applies_only_valid_safe_fields",
        )
        return

    from domain.tool import settings_tools

    _FakeFrontendRegistry.updated_values = None
    with patch.object(settings_tools, "FrontendRegistry", _FakeFrontendRegistry):
        result = settings_tools.settings_update({
            "changes": [
                {"section_id": "general", "field_id": "compact", "value": True},
                {"section_id": "general", "field_id": "theme", "value": "light"},
            ]
        })

    assert result["is_error"] is False
    assert result["widget"]["data"]["count"] == 2
    assert _FakeFrontendRegistry.updated_values["general"]["compact"] is True
    assert _FakeFrontendRegistry.updated_values["general"]["theme"] == "light"


def test_settings_update_rejects_secret_fields_and_nested_secret_keys() -> None:
    if not is_pack_test_child():
        run_pack_test(
            Path(__file__),
            "test_settings_update_rejects_secret_fields_and_nested_secret_keys",
        )
        return

    from domain.tool import settings_tools

    _FakeFrontendRegistry.updated_values = None
    with patch.object(settings_tools, "FrontendRegistry", _FakeFrontendRegistry):
        protected_field = settings_tools.settings_update({
            "changes": [{"section_id": "general", "field_id": "api_key", "value": "nope"}]
        })
        protected_value = settings_tools.settings_update({
            "changes": [{"section_id": "general", "field_id": "advanced", "value": {"access_token": "nope"}}]
        })

    assert protected_field["widget"]["error"]["code"] == "PROTECTED_SETTINGS_CHANGE"
    assert protected_value["widget"]["error"]["code"] == "PROTECTED_SETTINGS_VALUE"
    assert _FakeFrontendRegistry.updated_values is None
