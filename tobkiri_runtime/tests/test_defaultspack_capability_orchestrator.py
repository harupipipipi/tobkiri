from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

PACK_ROOT = Path(__file__).resolve().parents[1] / "ecosystem" / "defaultspack"
sys.path.insert(0, str(PACK_ROOT))

from domain.capability.activity_registry import ActivityRegistry  # noqa: E402
from domain.capability.orchestrator import CapabilityOrchestrator  # noqa: E402
from domain.capability.policy import EffectPolicyEngine  # noqa: E402
from domain.capability.tool_scope import ToolScope, normalize_tool_scope  # noqa: E402
from domain.extensions.manifest import (  # noqa: E402
    ManifestValidationError,
    validate_manifest,
)
from domain.skill_trigger import RuntimeSkillTriggerService  # noqa: E402


def _manifest(path: str) -> dict:
    return json.loads((PACK_ROOT / path).read_text(encoding="utf-8"))


def _tool(tool_id: str, *, effect: str = "read") -> dict:
    return {
        "tool_id": tool_id,
        "name": tool_id,
        "summary": tool_id,
        "schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "effects": [{"class": effect, "operation": tool_id}],
        "trusted": True,
        "risk": "low",
    }


def _selector_handler(_: str, payload: dict) -> dict:
    candidate_text = payload["messages"][0]["content"]
    selected = []
    for tool_id in ("desktop_frame", "desktop_input"):
        if tool_id in candidate_text:
            selected.append(
                {
                    "tool_id": tool_id,
                    "confidence": 1,
                    "reason": "needed for the explicit computer activity",
                }
            )
    return {
        "status": "ok",
        "data": {
            "content": json.dumps({"selected_tools": selected}),
        },
    }


def test_versioned_activity_manifest_is_discovered_and_validated() -> None:
    raw = _manifest("extensions/activities/computer/manifest.json")

    normalized = validate_manifest(raw, expected_category="activity")

    assert normalized["schema_version"] == "tobkiri.activity/v1"
    assert normalized["members"]["skills"]["safety"] == ["computer_safety"]
    assert normalized["selection"]["max_attached_tools"] == 6


def test_tool_v3_manifest_cannot_self_assert_trust() -> None:
    raw = {
        "$schema": "https://schemas.tobkiri.dev/tool/v3.json",
        "schema_version": "tobkiri.tool/v3",
        "kind": "tool",
        "id": "example.read",
        "version": "1.0.0",
        "display_name": "Read",
        "description": "Read",
        "discovery": {},
        "contract": {"input_schema": {"type": "object"}},
        "effects": [{"class": "read", "operation": "example.read"}],
        "risk": {"level": "low"},
        "approval": {"default": "inherit", "minimum": "auto"},
        "execution": {
            "type": "capability",
            "permission_id": "example.read",
        },
        "requirements": {},
        "security": {"trusted": True},
        "ui": {},
    }

    with pytest.raises(ManifestValidationError, match="trusted"):
        validate_manifest(raw, expected_category="tool")


def test_activity_mentions_support_japanese_adjacency_and_namespaces() -> None:
    registry = ActivityRegistry(
        [
            validate_manifest(
                _manifest("extensions/activities/computer/manifest.json"),
                expected_category="activity",
            )
        ]
    )

    adjacent = registry.resolve_mentions("お願い@computer で操作して")
    namespaced = registry.resolve_mentions("@activity:computer")

    assert [item.activity_id for item in adjacent] == ["computer"]
    assert namespaced[0].source == "explicit_namespace"


def test_runtime_profile_tool_scope_has_explicit_three_states() -> None:
    assert normalize_tool_scope(None) == ToolScope(mode="inherit")
    assert normalize_tool_scope({"mode": "none", "ids": []}) == ToolScope(
        mode="none"
    )
    assert normalize_tool_scope(
        {"mode": "allowlist", "ids": ["a", "a", "b"]}
    ) == ToolScope(mode="allowlist", ids=("a", "b"))
    with pytest.raises(ValueError, match="requires at least one id"):
        normalize_tool_scope({"mode": "allowlist", "ids": []})
    assert normalize_tool_scope([]) == ToolScope(mode="none")
    assert normalize_tool_scope({"tools": []}) == ToolScope(mode="none")


def test_explicit_skill_does_not_remove_safety_skill() -> None:
    skills = [
        {
            "id": "safety",
            "instructions": "Stay safe.",
            "composition": {"class": "safety", "priority": 100},
        },
        {
            "id": "explicit",
            "instructions": "Do the requested workflow.",
        },
    ]

    result = RuntimeSkillTriggerService(skills).evaluate(
        user_text="@skill:explicit",
        context={"safety_skills": ["safety"]},
    )

    assert [item["id"] for item in result["matched"]] == [
        "safety",
        "explicit",
    ]


def test_capability_orchestrator_compiles_activity_tools_skills_and_approval() -> None:
    activities = [
        validate_manifest(
            _manifest("extensions/activities/computer/manifest.json"),
            expected_category="activity",
        )
    ]
    skills = [
        {
            "id": "desktop_operator",
            "instructions": "Observe then act.",
            "composition": {"class": "required", "priority": 100},
        },
        {
            "id": "computer_safety",
            "instructions": "Keep approval boundaries.",
            "composition": {"class": "safety", "priority": 1000},
        },
    ]
    tools = [
        _tool("desktop_list"),
        _tool("desktop_frame"),
        _tool("desktop_input", effect="computer"),
    ]
    orchestrator = CapabilityOrchestrator(
        activities=activities,
        skills=skills,
        call_handler=_selector_handler,
    )

    plan = orchestrator.resolve(
        user_text="お願い@computer で画面を操作して",
        tools=tools,
        settings={"tools": {"selector_model": "stub/default"}},
        dry_run=True,
    )

    assert plan["activities"][0]["id"] == "computer"
    assert {"desktop_frame", "desktop_input"}.issubset(
        plan["tools"]["selected"]
    )
    assert len(plan["tools"]["selected"]) <= 6
    assert plan["skills"]["selected"] == [
        "computer_safety",
        "desktop_operator",
    ]
    assert any(
        effect["tool_id"] == "desktop_input"
        and effect["mode"] == "confirm"
        for effect in plan["approval"]["effects"]
    )


def test_compiled_model_input_is_exactly_bound_to_plan_hashes() -> None:
    tools = [_tool("desktop_frame"), _tool("desktop_input", effect="computer")]
    skills = [{"id": "explicit", "instructions": "Use the exact workflow."}]
    result = CapabilityOrchestrator(
        activities=[],
        skills=skills,
    ).compile_selected(
        user_text="@skill:explicit",
        selected_tools=[tools[1]],
        eligible_tools=tools,
    )
    compiled = result.pop("_compiled_model_input")

    assert result["tools"]["selected"] == ["desktop_input"]
    assert compiled["tool_ids"] == result["tools"]["selected"]
    assert compiled["tool_schema_hashes"] == result["tools"]["schema_hashes"]
    assert compiled["skill_ids"] == result["skills"]["selected"]
    assert compiled["skill_instruction_hashes"] == result["skills"][
        "instruction_hashes"
    ]


def test_repository_context_connection_compiles_external_share_authority() -> None:
    tool = _tool("repository_context_prepare")
    tool["capability_requirements"] = {
        "connections": ["rumi.service.repository.context.prepare.v1"],
        "env": [],
    }
    tool["requires_runtime_capabilities"] = ["runtime.workspace"]

    result = CapabilityOrchestrator(
        activities=[],
        skills=[],
    ).compile_selected(
        user_text="@tool:repository_context_prepare",
        selected_tools=[tool],
        eligible_tools=[tool],
    )

    assert tool["requires_runtime_capabilities"] == ["runtime.workspace"]
    assert "repository.content.external_share" in result["tools"][
        "capability_grants"
    ]["repository_context_prepare"]


def test_client_selected_skill_metadata_is_not_an_explicit_authority() -> None:
    skills = [
        {"id": "safe", "instructions": "Safe instructions."},
        {
            "id": "hidden",
            "instructions": "Hidden instructions.",
            "triggers": ["never-match"],
        },
    ]

    result = RuntimeSkillTriggerService(skills).evaluate(
        user_text="plain text",
        context={"selected_skills": ["hidden"]},
    )

    assert "hidden" not in [item["id"] for item in result["matched"]]


def test_activity_with_missing_safety_skill_fails_closed() -> None:
    activity = {
        "id": "unsafe-activity",
        "members": {
            "tool_ids": ["desktop_input"],
            "skills": {"safety": ["missing-safety"], "required": []},
        },
    }
    result = CapabilityOrchestrator(
        activities=[activity],
        skills=[],
    ).compile_selected(
        user_text="@activity:unsafe-activity",
        selected_tools=[_tool("desktop_input", effect="computer")],
        eligible_tools=[_tool("desktop_input", effect="computer")],
        context={"capability_activity_ids": ["unsafe-activity"]},
    )
    result.pop("_compiled_model_input")

    assert result["activities"] == []
    assert any(
        item["code"] == "activity_required_skill_missing"
        for item in result["diagnostics"]
    )


def test_persisted_skill_cannot_read_symlinked_or_configured_external_file(
    tmp_path: Path,
) -> None:
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    manifest = skill_root / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("DO NOT LOAD", encoding="utf-8")
    (skill_root / "SKILL.md").symlink_to(secret)
    skill = {
        "id": "unsafe",
        "source_path": str(manifest),
        "instructions_path": str(secret),
        "description": "must not become an instruction",
    }

    result = RuntimeSkillTriggerService([skill]).evaluate(
        user_text="@skill:unsafe",
    )

    assert result == {"matched": [], "instructions": ""}


def test_full_access_does_not_bypass_untrusted_write_minimum() -> None:
    settings = {
        "capabilities": {
            "approval": {"actions": {"send": "auto"}},
            "tools": {"overrides": {}},
        }
    }
    tool = {
        "tool_id": "mail.send",
        "trusted": False,
        "risk": "high",
        "effects": [{"class": "send", "operation": "mail.send"}],
    }

    decision = EffectPolicyEngine().resolve(
        tool,
        settings,
        full_access=True,
    )[0]

    assert decision.mode == "confirm"
    assert decision.hard_minimum == "confirm"
