"""Pure catalog regression for the optional Composer tool control."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SOURCE = (
    Path(__file__).resolve().parents[1]
    / "ecosystem/defaultspack/domain/frontend_settings_catalog.py"
)
spec = importlib.util.spec_from_file_location("isolated_composer_catalog", SOURCE)
assert spec is not None and spec.loader is not None
catalog = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = catalog
spec.loader.exec_module(catalog)


def _tool_fields() -> dict[str, dict]:
    """Read the actual catalog projection with no account data or services."""
    inputs = catalog.SettingsCatalogInputs([], [], [], [], [], [], [])
    sections = catalog.SettingsSections().build(
        [], [], template_catalog=None, inputs=inputs
    )
    tools = next(section for section in sections if section["id"] == "tools")
    return {field["id"]: field for field in tools["fields"]}


def test_optional_tool_control_is_a_hidden_by_default_settings_toggle() -> None:
    """The same saved key can be turned on through the normal settings owner."""
    field = _tool_fields()["show_tool_selection_control"]
    assert field["type"] == "toggle"
    assert field["default"] is False
    assert "機能の使い方" in field["label"]


def test_candidate_selection_stays_auto_with_all_existing_modes() -> None:
    """Control visibility does not force a manual selection or grant access."""
    field = _tool_fields()["default_mode"]
    assert field["default"] == "auto"
    assert [item["value"] for item in field["options"]] == [
        "auto", "review", "manual", "none"
    ]
