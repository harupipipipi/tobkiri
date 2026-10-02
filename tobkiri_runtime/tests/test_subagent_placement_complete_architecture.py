from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from ecosystem.rumi_subagent_placement_pack.runtime.compiler import (
    PlacementCompileError,
)
from ecosystem.rumi_subagent_placement_pack.runtime.topology import (
    SCHEMA_PLACEMENT_PATCH,
    adapt_remote_agent_card,
    apply_placement_patch,
    compile_placement_map,
    create_runtime_assignment,
    export_topology_as_subagent,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "ecosystem" / "defaultspack" / "schemas"
DEFAULTSPACK = ROOT / "ecosystem" / "defaultspack"
if str(DEFAULTSPACK) not in sys.path:
    sys.path.insert(0, str(DEFAULTSPACK))


def _domain_imports() -> tuple[object, object, object]:
    from domain.agent import placement_catalog
    from domain.agent.subagent_orchestrator import SubagentOrchestrator
    from domain.company import models

    return placement_catalog, SubagentOrchestrator, models


def _schema(name: str) -> dict:
    return json.loads((SCHEMAS / name).read_text(encoding="utf-8"))


def test_default_team_uses_main_and_role_named_subagents() -> None:
    placement_catalog, _, models = _domain_imports()

    placement_map = placement_catalog.default_operations_placement_map()
    members = {
        member["placement_id"]: member for member in placement_map["placements"]
    }

    assert placement_map["main"] == {
        "placement_id": "client_manager",
        "mode": "interactive",
    }
    assert members["client_manager"]["kind"] == "main"
    assert all(
        member["kind"] == "subagent"
        for key, member in members.items()
        if key != "client_manager"
    )
    projected = {agent["agent_id"]: agent for agent in models.default_agents()}
    assert projected["client_manager"]["display_name"] == "Main Agent"
    assert projected["coding_engineer"]["display_name"] == "coding-subagent"
    assert projected["reviewer"]["agent_kind"] == "subagent"
    assert projected["reviewer"]["placement_map_id"] == "default-operations"


def test_builtin_effective_plan_is_pinned_and_least_tool_authority() -> None:
    placement_catalog, _, _ = _domain_imports()

    plan = placement_catalog.compile_builtin_effective_plan(
        "coding_engineer",
        model="opencode-zen/mimo-v2.5-free",
        allowed_tools=[
            "coding_file_read",
            "coding_file_patch",
            "coding_git_diff",
            "not-declared",
        ],
        host_policy={
            "denied_tool_ids": ["coding_file_patch"],
            "capability_plan_ref": "plan://test",
        },
    )

    placement_catalog.verify_effective_plan(plan)
    assert plan["model"]["model_id"] == "opencode-zen/mimo-v2.5-free"
    assert plan["tool_bindings"]["allow_tool_ids"] == [
        "coding_file_read",
        "coding_git_diff",
    ]
    assert plan["placement"]["map_id"] == "default-operations"
    assert plan["revisions"]["placement_revision"]
    assert plan["plan_hash"].startswith("sha256:")


def test_compatibility_plan_normalizes_provider_tool_definitions() -> None:
    placement_catalog, _, _ = _domain_imports()

    plan = placement_catalog.compatibility_effective_plan(
        agent_id="custom",
        model="stub/default",
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "coding_file_read",
                    "parameters": {"type": "object"},
                },
            }
        ],
        system_prompt="Read only.",
    )

    assert plan["tool_bindings"]["allow_tool_ids"] == [
        "coding_file_read"
    ]


def test_team_topology_constrains_member_budgets_and_approval() -> None:
    placement_catalog, _, _ = _domain_imports()
    placement_map = placement_catalog.default_operations_placement_map()
    placement_map.pop("content_hash")
    placement_map["governance"] = {
        "maximum_steps": 5,
        "approval": {"minimum": "deny"},
    }
    placement_map["content_hash"] = (
        __import__(
            "ecosystem.rumi_subagent_placement_pack.runtime.topology",
            fromlist=["content_hash"],
        ).content_hash(
            {
                key: value
                for key, value in placement_map.items()
                if key != "content_hash"
            }
        )
    )
    plans = [
        placement_catalog.compile_builtin_effective_plan(
            member["placement_id"],
            host_policy={"capability_plan_ref": "plan://test"},
        )
        for member in placement_map["placements"]
    ]

    topology = compile_placement_map(
        placement_map,
        plans,
        registry_revision="registry:test",
    )

    assert topology["main"]["placement_id"] == "client_manager"
    assert len(topology["members"]) == len(plans)
    assert all(
        plan["budgets"]["maximum_steps"] == 5
        for plan in topology["plans"].values()
    )
    assert all(
        plan["approval"]["minimum"] == "deny"
        for plan in topology["plans"].values()
    )


def test_patch_creates_new_revision_while_old_assignment_stays_pinned() -> None:
    placement_catalog, _, _ = _domain_imports()
    current = placement_catalog.default_operations_placement_map()
    old_plan = placement_catalog.compile_builtin_effective_plan(
        "reviewer",
        host_policy={"capability_plan_ref": "plan://test"},
    )
    assignment = create_runtime_assignment(
        old_plan,
        run_id="run-old",
        root_scope_id="scope-main",
    )
    patch = {
        "schema_version": SCHEMA_PLACEMENT_PATCH,
        "placement_map_id": current["id"],
        "expected_revision": current["content_hash"],
        "operations": [
            {
                "op": "replace",
                "target": "placement",
                "key": "reviewer",
                "value": {
                    "placement_id": "reviewer",
                    "kind": "subagent",
                    "role": "security-review",
                },
            }
        ],
    }

    updated = apply_placement_patch(current, patch)

    assert updated["revision"] == current["revision"] + 1
    assert updated["content_hash"] != current["content_hash"]
    assert assignment["effective_plan_hash"] == old_plan["plan_hash"]
    assert assignment["state"] == "assigned"


def test_patch_fails_closed_for_stale_revision() -> None:
    placement_catalog, _, _ = _domain_imports()
    current = placement_catalog.default_operations_placement_map()

    with pytest.raises(PlacementCompileError, match="stale"):
        apply_placement_patch(
            current,
            {
                "schema_version": SCHEMA_PLACEMENT_PATCH,
                "placement_map_id": current["id"],
                "expected_revision": "sha256:" + "0" * 64,
                "operations": [
                    {
                        "op": "remove",
                        "target": "placement",
                        "key": "reviewer",
                    }
                ],
            },
        )


def test_utility_subagent_has_a_no_tool_effective_plan() -> None:
    _, orchestrator_cls, _ = _domain_imports()

    result = orchestrator_cls().run(
        "context_summarizer",
        {"text": "bounded summary"},
    )

    assert result["agent_kind"] == "subagent"
    assert result["runtime_kind"] == "utility_model_call"
    assert result["placement_id"] == "context_summarizer-subagent"
    assert result["effective_subagent_plan"]["budgets"][
        "maximum_tool_calls"
    ] == 0
    assert result["effective_plan_hash"].startswith("sha256:")


def test_new_subagent_schemas_accept_canonical_resources() -> None:
    placement_catalog, _, _ = _domain_imports()
    placement_map = placement_catalog.default_operations_placement_map()
    plan = placement_catalog.compile_builtin_effective_plan(
        "coding_engineer",
        host_policy={"capability_plan_ref": "plan://test"},
    )
    assignment = create_runtime_assignment(
        plan,
        run_id="run-schema",
        root_scope_id="scope-schema",
    )

    Draft202012Validator(
        _schema("subagent-placement-map.v1.schema.json")
    ).validate(placement_map)
    Draft202012Validator(
        _schema("effective-subagent.v1.schema.json")
    ).validate(plan)
    Draft202012Validator(
        _schema("subagent-runtime-assignment.v1.schema.json")
    ).validate(assignment)


def test_agent_run_store_persists_placement_identity(tmp_path: Path) -> None:
    placement_catalog, _, _ = _domain_imports()
    from domain.agent.execution import AgentExecution
    from domain.agent_runtime.run_store import AgentRunStore

    plan = placement_catalog.compile_builtin_effective_plan(
        "reviewer",
        host_policy={"capability_plan_ref": "plan://store-test"},
    )
    execution = AgentExecution(
        "run-placement",
        "review",
        [],
        "stub/default",
        "Review carefully.",
    )
    execution.context = {
        "agent_id": "reviewer",
        "agent_kind": "subagent",
        "runtime_kind": "agent_run",
        "subagent_role": "review",
        "placement_id": plan["placement"]["id"],
        "placement_revision": plan["placement"]["revision"],
        "placement_map_id": plan["placement"]["map_id"],
        "effective_plan_hash": plan["plan_hash"],
        "protocol_membership": [
            item["protocol_ref"] for item in plan["protocol_bindings"]
        ],
        "root_scope_id": "scope-placement",
        "root_run_id": "run-root",
        "parent_run_id": "run-parent",
    }
    store = AgentRunStore(tmp_path / "state.db")

    store.save_execution(execution)
    persisted = store.get_run("run-placement")

    assert persisted is not None
    assert persisted["agent_kind"] == "subagent"
    assert persisted["runtime_kind"] == "agent_run"
    assert persisted["placement_id"] == "reviewer"
    assert persisted["effective_plan_hash"] == plan["plan_hash"]
    assert persisted["root_scope_id"] == "scope-placement"
    assert persisted["protocol_membership_json"]


def test_remote_agent_card_is_adapted_without_fake_host_enforcement() -> None:
    definition = adapt_remote_agent_card(
        {
            "id": "external-reviewer",
            "name": "External Reviewer",
            "version": "2.1.0",
            "endpoint": "https://agents.example.test/review",
            "capabilities": {"reasoning": True, "tool_calling": True},
            "skills": [
                {
                    "produces": ["tobkiri.review-result/v1"],
                }
            ],
        },
        pack_id="remote-adapter",
    )

    Draft202012Validator(
        _schema("subagent.v1.schema.json")
    ).validate(definition)
    assert definition["runtime"]["driver_key"] == "remote_agent"
    assert (
        definition["enforcement"]["remote_internal_tools"]
        == "remote_attested"
    )
    assert "host_enforced" not in definition["enforcement"].values()


def test_compiled_team_can_be_exported_as_composite_subagent() -> None:
    placement_catalog, _, _ = _domain_imports()
    placement_map = placement_catalog.default_operations_placement_map()
    plans = [
        placement_catalog.compile_builtin_effective_plan(
            member["placement_id"],
            host_policy={"capability_plan_ref": "plan://nested"},
        )
        for member in placement_map["placements"]
    ]
    topology = compile_placement_map(
        placement_map,
        plans,
        registry_revision="registry:nested",
    )

    definition = export_topology_as_subagent(
        topology,
        subagent_id="defaultspack.operations-team",
        display_name="operations-team-subagent",
    )

    Draft202012Validator(
        _schema("subagent.v1.schema.json")
    ).validate(definition)
    assert definition["runtime"]["driver_key"] == "composite_team"
    assert definition["topology_hash"] == topology["topology_hash"]
