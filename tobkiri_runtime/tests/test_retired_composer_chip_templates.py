"""Retired tool/service rows cannot be reintroduced by template catalogs."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

DEFAULTSPACK_ROOT = Path(__file__).resolve().parents[1] / "ecosystem/defaultspack"
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from domain.templates import (  # noqa: E402
    ResolvedTemplate,
    parse_template,
    project_resolved_templates,
)


@pytest.mark.parametrize(
    "widget",
    [
        {"widget_kind": "tool_toggle", "tool_id": "web_search"},
        {"widgetKind": "tool_toggle", "sourceItemId": "web_search"},
        {"widget_kind": "service_reference", "service_id": "web"},
        {"type": "tool", "tool_id": "web_search"},
        {"type": "service", "service_id": "web"},
        {"tool_id": "web_search"},
        {"sourceItemId": "web_search"},
        {"source_item_id": "web_search"},
    ],
)
@pytest.mark.parametrize("nested", [True, False])
def test_custom_templates_cannot_advertise_retired_chips(
    widget: dict[str, str], nested: bool
) -> None:
    """Both historical payload shapes are excluded, with other pieces intact."""
    payload = {"widget": widget} if nested else widget
    parsed = parse_template(
        {
            "id": "custom.composer",
            "kind": "frontend",
            "version": "1.0.0",
            "status": "active",
            "pieces": [
                {"id": "retired", "kind": "composer_widget", **payload},
                {
                    "id": "custom_button",
                    "kind": "composer_widget",
                    "widget": {"widget_kind": "button", "label": "Custom"},
                },
                {
                    "id": "composer",
                    "kind": "composer_input",
                    "input": {"id": "composer", "region_id": "composer"},
                },
            ],
        }
    )
    assert parsed.ok, parsed.diagnostics
    assert parsed.template is not None
    catalog = project_resolved_templates([ResolvedTemplate(template=parsed.template)])
    assert [item["id"] for item in catalog["composer_widgets"]] == ["custom_button"]
    assert [item["id"] for item in catalog["composer_inputs"]] == ["composer"]
    # Preserve stored schema/pieces so reading a legacy template is non-destructive.
    assert len(parsed.template.pieces) == 3


def test_builtin_composer_does_not_reference_or_offer_web_search_toggle() -> None:
    """The default template no longer supplies the removed selection control."""
    payload = json.loads(
        (DEFAULTSPACK_ROOT / "templates/composer/default/template.json").read_text()
    )
    parsed = parse_template(payload)
    assert parsed.ok, parsed.diagnostics
    assert parsed.template is not None
    catalog = project_resolved_templates([ResolvedTemplate(template=parsed.template)])
    assert catalog["composer_widgets"] == []
    assert all("web_search_toggle" not in item.get("widgets", []) for item in catalog["ai_inputs"])
    assert all(piece["id"] != "web_search_toggle" for piece in payload["pieces"])


@pytest.mark.parametrize("role", [None, "action", "data_source"])
def test_retired_chip_cannot_be_reexported_through_conflicting_kind_or_role(
    role: str | None,
) -> None:
    """A nested generic kind cannot override retired metadata on the same piece."""
    piece = {
        "id": "retired",
        "kind": "composer_widget",
        "widget_kind": "tool_toggle",
        "widget": {"widget_kind": "button", "label": "Custom"},
    }
    if role is not None:
        piece["role"] = role
    parsed = parse_template({
        "id": "custom.conflict", "kind": "frontend", "version": "1.0.0",
        "status": "active", "pieces": [piece],
    })
    assert parsed.ok, parsed.diagnostics
    assert parsed.template is not None
    catalog = project_resolved_templates([ResolvedTemplate(template=parsed.template)])
    assert catalog["composer_widgets"] == []
    assert catalog["actions"] == []
    assert catalog["data_sources"] == []
