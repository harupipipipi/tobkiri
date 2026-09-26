from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def test_default_profile_exposes_deepthink_discovery_and_all_skills():
    from domain.ai_client.deepthink_extensions import (
        available_skill_catalog,
        deepthink_extension_contract,
    )
    from domain.extensions.runtime import get_extension_registry

    get_extension_registry(force_reload=True)
    contract = deepthink_extension_contract()
    assert contract["discovery_tools"] == ["tool_search", "skill_search"]
    assert [phase["id"] for phase in contract["phases"]] == [
        "grounding",
        "perspective_review",
    ]
    assert {item["id"] for item in contract["perspectives"]} >= {
        "affirmative",
        "critical",
        "user",
    }
    assert contract["presentation"]["id"] == "defaultspack.deepthink.v1"
    assert contract["presentation"]["motion"] == {
        "entry": "rise",
        "surface": "aurora",
        "indicator": "orbit",
        "active_phase": "signal",
    }
    assert [item["id"] for item in contract["presentation"]["phases"]][:4] == [
        "preflight",
        "planning",
        "capability_assessment",
        "integrations",
    ]
    deepthink_manifest = json.loads(
        (
            DEFAULTSPACK_ROOT
            / "extensions"
            / "deepthink"
            / "default"
            / "manifest.json"
        ).read_text(encoding="utf-8")
    )
    frontend_fallback = json.loads(
        (
            DEFAULTSPACK_ROOT
            / "webapp"
            / "src"
            / "features"
            / "chat"
            / "presentation"
            / "templates"
            / "deepthink.json"
        ).read_text(encoding="utf-8")
    )
    assert deepthink_manifest["config"]["presentation"] == frontend_fallback
    assert available_skill_catalog()
    assert (DEFAULTSPACK_ROOT / "tools" / "skill_search" / "manifest.json").is_file()
    template = json.loads(
        (
            DEFAULTSPACK_ROOT / "templates" / "model_selector" / "default" / "template.json"
        ).read_text(encoding="utf-8")
    )
    fields = {
        item.get("field_id") or item.get("id"): item
        for item in template["pieces"]
        if item.get("kind") == "settings_field"
    }
    sections = {
        item.get("section_id"): item
        for item in template["pieces"]
        if item.get("kind") == "settings_section"
    }
    assert "deepthink" in sections
    assert "deepthink_enabled" not in fields
    assert fields["model_source"]["default"] == "conversation"
    assert fields["model"]["type"] == "model_select"
    assert fields["allow_delegated_agents"]["default"] is False
    assert fields["allow_background_continuations"]["default"] is False
    discussion_manifest = json.loads(
        (DEFAULTSPACK_ROOT / "tools" / "discussion" / "manifest.json").read_text(encoding="utf-8")
    )
    assert discussion_manifest["loading"] == "always"


def test_skill_search_lists_every_visible_skill_and_can_include_instructions():
    from domain.tool.skill_search import run_skill_search

    overview = run_skill_search({}, {})
    assert overview["is_error"] is False
    assert overview["result"]["visibility"] == "all_enabled_in_active_profile"
    assert overview["result"]["count"] == len(overview["result"]["skills"])

    skill_id = overview["result"]["skills"][0]["id"]
    detail = run_skill_search(
        {"skill_ids": [skill_id], "include_instructions": True},
        {},
    )
    assert [item["id"] for item in detail["result"]["skills"]] == [skill_id]
    assert detail["result"]["skills"][0]["instructions"]


def test_selected_pack_can_add_deepthink_phases_and_perspectives(monkeypatch):
    from domain.ai_client import deepthink_extensions

    class DeepThinkRegistry:
        @staticmethod
        def list(*, enabled_only=True):
            assert enabled_only is True
            return [
                {
                    "source_pack_id": "example_pack",
                    "config": {
                        "discovery_tools": ["domain_lookup"],
                        "phases": [
                            {
                                "id": "legal_review",
                                "label": "法務確認",
                                "prompt": "Check applicable legal constraints.",
                            }
                        ],
                        "perspectives": [
                            {
                                "id": "legal",
                                "name": "法務視点",
                                "mission": "Check legal risks.",
                            }
                        ],
                    },
                }
            ]

    class Registry:
        @staticmethod
        def deepthink():
            return DeepThinkRegistry()

    monkeypatch.setattr(deepthink_extensions, "get_extension_registry", Registry)
    contract = deepthink_extensions.deepthink_extension_contract()
    assert contract["discovery_tools"] == ["domain_lookup"]
    assert contract["phases"] == [
        {
            "id": "legal_review",
            "label": "法務確認",
            "prompt": "Check applicable legal constraints.",
            "source_pack_id": "example_pack",
        }
    ]
    assert contract["perspectives"][0]["id"] == "legal"
    assert contract["presentation"] == {}


def test_deepthink_discovery_tools_are_attached_only_after_profile_authorization(
    monkeypatch,
):
    from domain.ai_client import deepthink_extensions
    from domain.chat.run_request import _with_deepthink_discovery_tools

    monkeypatch.setattr(
        deepthink_extensions,
        "deepthink_extension_contract",
        lambda: {"discovery_tools": ["tool_search", "skill_search"]},
    )
    tool_search = {
        "tool_id": "tool_search",
        "type": "function",
        "function": {"name": "tool_search"},
    }
    context = {}

    attached = _with_deepthink_discovery_tools(
        [],
        [tool_search],
        {"params": {"deepthink_enabled": True}},
        context,
    )

    assert [tool["tool_id"] for tool in attached] == ["tool_search"]
    assert context["deepthink_discovery_tools"] == {
        "requested": ["tool_search", "skill_search"],
        "attached": ["tool_search"],
        "unavailable": ["skill_search"],
    }


def test_deepthink_discovery_tools_respect_deepthink_and_explicit_tool_disable(
    monkeypatch,
):
    from domain.ai_client import deepthink_extensions
    from domain.chat.run_request import _with_deepthink_discovery_tools

    monkeypatch.setattr(
        deepthink_extensions,
        "deepthink_extension_contract",
        lambda: {"discovery_tools": ["tool_search"]},
    )
    context = {}
    attached = _with_deepthink_discovery_tools(
        [],
        [
            {
                "tool_id": "tool_search",
                "type": "function",
                "function": {"name": "tool_search"},
            }
        ],
        {"params": {"deepthink_enabled": False}},
        context,
    )

    assert attached == []
    assert "deepthink_discovery_tools" not in context

    attached = _with_deepthink_discovery_tools(
        [],
        [
            {
                "tool_id": "tool_search",
                "type": "function",
                "function": {"name": "tool_search"},
            }
        ],
        {"params": {"deepthink_enabled": True}},
        context,
        selection_mode="none",
    )

    assert attached == []
    assert "deepthink_discovery_tools" not in context
