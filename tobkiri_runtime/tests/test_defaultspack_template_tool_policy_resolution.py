from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

pytestmark = pytest.mark.usefixtures("defaultspack_v4_tool_dispatch")

from domain.chat.run_request import _available_tools  # noqa: E402
from domain.templates import ResolvedTemplate, parse_template, project_resolved_templates  # noqa: E402
from domain.templates.tool_policy_resolution import resolve_template_tool_policy  # noqa: E402
from domain.tool.schema_adapter import tool_name_from_definition  # noqa: E402


def _catalog() -> dict:
    parsed = parse_template(
        {
            "id": "template.ai.input",
            "kind": "frontend",
            "version": "1.0.0",
            "status": "active",
            "pieces": [
                {
                    "id": "policy_piece",
                    "kind": "tool_policy",
                    "policy": {
                        "id": "chat_tools",
                        "allowed_tools": ["web_search"],
                        "disabled_tools": ["coding_terminal_exec"],
                        "default_enabled_tools": ["web_search"],
                        "default_disabled_tools": ["sandbox_exec"],
                        "tool_choice": "auto",
                        "parallel_tool_calls": False,
                    },
                },
                {
                    "id": "ai_input_piece",
                    "kind": "ai_input",
                    "input": {
                        "id": "chat_ai_input",
                        "tool_policy": "chat_tools",
                    },
                },
            ],
        }
    )
    assert parsed.ok
    assert parsed.template is not None
    return project_resolved_templates([ResolvedTemplate(template=parsed.template)])


def test_template_tool_policy_resolves_from_ai_input_and_ignores_request_lists():
    resolution = resolve_template_tool_policy(
        {
            "ai_input_id": "chat_ai_input",
            "template_tool_policy_id": "chat_tools",
            "tool_allowlist": ["coding_terminal_exec"],
            "tool_denylist": ["web_search"],
            "default_enabled_tools": ["coding_terminal_exec"],
            "default_disabled_tools": ["web_search"],
            "selected_tools": ["web_search"],
            "yolo_mode": True,
        },
        catalog=_catalog(),
    )

    assert resolution.applied is True
    assert resolution.policy["tool_allowlist"] == ["web_search"]
    assert resolution.policy["tool_denylist"] == ["coding_terminal_exec", "sandbox_exec"]
    assert resolution.policy["default_enabled_tools"] == ["web_search"]
    assert resolution.policy["default_disabled_tools"] == [
        "coding_terminal_exec",
        "sandbox_exec",
    ]
    assert resolution.policy["parallel_tool_calls"] is False
    assert resolution.policy["tool_choice"] == "auto"
    assert "selected_tools" not in resolution.policy
    assert resolution.policy["yolo_mode"] is True


def test_template_tool_policy_resolves_projected_policy_id():
    resolution = resolve_template_tool_policy(
        {
            "template_tool_policy_id": "template.ai.input:policy_piece",
            "tool_allowlist": ["coding_terminal_exec"],
        },
        catalog=_catalog(),
    )

    assert resolution.applied is True
    assert resolution.policy["tool_allowlist"] == ["web_search"]
    assert resolution.policy["template_tool_policy_id"] == "chat_tools"
    assert (
        resolution.policy["template_tool_policy_projected_id"] == "template.ai.input:policy_piece"
    )


def test_template_tool_policy_preserves_legacy_policy_without_ids_or_catalog():
    legacy_policy = {
        "tool_allowlist": ["coding_terminal_exec"],
        "default_disabled_tools": ["web_search"],
    }

    without_ids = resolve_template_tool_policy(legacy_policy, catalog=_catalog())
    without_catalog = resolve_template_tool_policy(
        {"template_tool_policy_id": "chat_tools", **legacy_policy},
        catalog={},
    )

    assert without_ids.id_requested is False
    assert without_ids.policy == legacy_policy
    assert without_catalog.catalog_available is False
    assert without_catalog.blocked is True
    assert without_catalog.policy["tool_allowlist"] == []
    assert without_catalog.policy["tool_choice"] == "none"
    assert without_catalog.policy["template_policy_blocked"] is True


def test_resolved_template_policy_filters_requested_tools_in_backend_pipeline():
    resolution = resolve_template_tool_policy(
        {
            "ai_input_id": "chat_ai_input",
            "tool_allowlist": ["coding_terminal_exec"],
        },
        catalog=_catalog(),
    )

    raw_tools, _provider_tools, _tool_context = _available_tools(
        {
            "profile_policy": resolution.policy,
            "principal_capabilities": ["developer"],
        },
        {
            "tools": ["web_search", "coding_terminal_exec"],
            "params": {"tool_policy": resolution.policy},
        },
    )

    assert [tool_name_from_definition(tool) for tool in raw_tools] == ["web_search"]


def test_template_tool_policy_empty_allowlist_disables_all_tools():
    parsed = parse_template(
        {
            "id": "template.ai.no_tools",
            "kind": "frontend",
            "version": "1.0.0",
            "status": "active",
            "pieces": [
                {
                    "id": "policy_piece",
                    "kind": "tool_policy",
                    "policy": {
                        "id": "no_tools",
                        "allowed_tools": [],
                    },
                },
            ],
        }
    )
    assert parsed.ok
    assert parsed.template is not None
    catalog = project_resolved_templates([ResolvedTemplate(template=parsed.template)])

    resolution = resolve_template_tool_policy(
        {"template_tool_policy_id": "no_tools"},
        catalog=catalog,
    )

    assert resolution.applied is True
    assert resolution.policy["tool_allowlist"] == []

    raw_tools, _provider_tools, _tool_context = _available_tools(
        {"profile_policy": resolution.policy},
        {
            "tools": ["web_search", "coding_terminal_exec"],
            "params": {"tool_policy": resolution.policy},
        },
    )

    assert raw_tools == []
