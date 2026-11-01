"""Canonical built-in Subagent definitions and Placement projections.

The generic Placement compiler lives in ``rumi_subagent_placement_pack``.
Defaultspack owns these built-in product roles and exposes legacy agent records
only as a compatibility projection for the existing Company/Team stores.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Iterable, Mapping


SCHEMA_DEFINITION = "tobkiri.subagent/v1"
SCHEMA_PLACEMENT = "tobkiri.subagent-placement/v1"
SCHEMA_PLACEMENT_MAP = "tobkiri.subagent-placement-map/v1"
SCHEMA_EFFECTIVE_PLAN = "tobkiri.effective-subagent/v1"
DEFAULT_PLACEMENT_MAP_ID = "default-operations"
DEFAULT_MODEL = "stub/default"


_ROLE_SPECS: tuple[dict[str, Any], ...] = (
    {
        "agent_id": "client_manager",
        "role": "main",
        "display_name": "Main Agent",
        "aliases": ["client", "president", "ceo", "main"],
        "tools": ["rumi_api", "todo", "subagent"],
        "context_limit": 64_000,
        "protocols": ["delegate", "supervisor"],
        "instructions": (
            "You are the Main Agent and the user-facing coordinator. Delegate "
            "specialist work, integrate Subagent results, and retain final "
            "responsibility for the task."
        ),
    },
    {
        "agent_id": "operations_manager",
        "role": "operations",
        "display_name": "operations-manager-subagent",
        "aliases": ["ops_manager", "manager"],
        "tools": ["rumi_api", "todo", "subagent"],
        "context_limit": 96_000,
        "protocols": ["delegate", "supervisor", "shared-task-list"],
        "instructions": (
            "Triage open tasks, stale runs, approvals, unresolved mentions, "
            "and dirty summaries. Route specialist work to the appropriate "
            "Subagent and report verified progress."
        ),
    },
    {
        "agent_id": "project_manager",
        "role": "planning",
        "display_name": "project-manager-subagent",
        "aliases": ["pm", "project_manager"],
        "tools": ["rumi_api", "todo", "subagent", "web_search"],
        "context_limit": 96_000,
        "protocols": ["delegate", "supervisor", "shared-task-list"],
        "instructions": (
            "Own task decomposition, ownership, milestones, blocker routing, "
            "and final handoff quality. Delegate specialist execution."
        ),
    },
    {
        "agent_id": "coding_engineer",
        "role": "coding",
        "display_name": "coding-subagent",
        "aliases": ["engineer", "coder", "coding"],
        "tools": [
            "rumi_api",
            "todo",
            "coding_file_read",
            "coding_file_search",
            "coding_file_list",
            "coding_file_write",
            "coding_file_create",
            "coding_file_patch",
            "coding_git_status",
            "coding_git_diff",
            "coding_terminal_exec",
        ],
        "context_limit": 128_000,
        "protocols": ["delegate", "agent-tool", "team-member"],
        "instructions": (
            "Implement bounded code changes in the assigned workspace. Follow "
            "local instructions, keep diffs scoped, test the result, and "
            "return changed paths and validation evidence."
        ),
    },
    {
        "agent_id": "research_specialist",
        "role": "research",
        "display_name": "research-subagent",
        "aliases": ["researcher", "research"],
        "tools": [
            "rumi_api",
            "web_search",
            "reddit_search",
            "file_reader",
            "todo",
        ],
        "context_limit": 96_000,
        "protocols": ["delegate", "agent-tool", "team-member"],
        "instructions": (
            "Research facts, documentation, competitive behavior, and user "
            "evidence. Prefer primary sources and report dates, citations, and "
            "uncertainty."
        ),
    },
    {
        "agent_id": "reviewer",
        "role": "review",
        "display_name": "review-subagent",
        "aliases": ["review", "reviewer"],
        "tools": [
            "rumi_api",
            "coding_file_read",
            "coding_file_search",
            "coding_git_status",
            "coding_git_diff",
            "coding_terminal_exec",
        ],
        "context_limit": 96_000,
        "protocols": ["review", "agent-tool", "team-member"],
        "instructions": (
            "Review work for correctness, safety, missing tests, and drift "
            "from the user goal. Lead with actionable findings and residual "
            "risk; do not silently modify the reviewed result."
        ),
    },
    {
        "agent_id": "operations_monitor",
        "role": "monitor",
        "display_name": "monitor-subagent",
        "aliases": ["monitor"],
        "tools": [
            "rumi_api",
            "browser_use",
            "browser_computer",
            "web_search",
            "todo",
        ],
        "context_limit": 64_000,
        "protocols": ["observer", "scheduled", "team-member"],
        "instructions": (
            "Monitor dashboards, queues, websites, and integrations. Stay "
            "quiet for normal checks and escalate changes with evidence."
        ),
    },
    {
        "agent_id": "scribe",
        "role": "memory",
        "display_name": "memory-subagent",
        "aliases": ["summary", "summarizer", "memory"],
        "tools": ["rumi_api", "todo"],
        "context_limit": 64_000,
        "protocols": ["observer", "team-member"],
        "instructions": (
            "Extract decisions, blockers, owners, and durable context. Propose "
            "bounded memory changes without treating the storage provider as "
            "an Agent."
        ),
    },
    {
        "agent_id": "scheduler",
        "role": "scheduler",
        "display_name": "scheduler-subagent",
        "aliases": ["schedule", "scheduler"],
        "tools": ["rumi_api", "todo", "subagent"],
        "context_limit": 48_000,
        "protocols": ["scheduled", "delegate", "team-member"],
        "instructions": (
            "Manage recurring tasks and heartbeat jobs. Avoid schedule loops, "
            "keep cadence explicit, and report only meaningful changes."
        ),
    },
)


def _hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _dedupe_strings(values: Iterable[Any]) -> list[str]:
    return sorted({str(value).strip() for value in values if str(value).strip()})


def _tool_ids(values: Iterable[Any]) -> list[str]:
    result = []
    for value in values:
        if isinstance(value, str):
            tool_id = value.strip()
        elif isinstance(value, Mapping):
            function = (
                value.get("function")
                if isinstance(value.get("function"), Mapping)
                else {}
            )
            tool_id = str(
                function.get("name")
                or value.get("tool_id")
                or value.get("name")
                or value.get("id")
                or ""
            ).strip()
        else:
            tool_id = ""
        if tool_id:
            result.append(tool_id)
    return _dedupe_strings(result)


def _definition(spec: Mapping[str, Any]) -> dict[str, Any]:
    role = str(spec["role"])
    is_main = role == "main"
    protocols = [
        f"tobkiri.protocol/{protocol}/v1"
        for protocol in spec.get("protocols", [])
    ]
    return {
        "schema_version": SCHEMA_DEFINITION,
        "kind": "subagent" if not is_main else "main",
        "id": f"defaultspack.{role}",
        "version": "1.0.0",
        "display_name": str(spec["display_name"]),
        "description": f"Built-in Tobkiri {role} execution capability.",
        "runtime": {
            "driver_contract": "rumi.service.subagent.runtime.v1",
            "driver_key": "model-agent",
            "supported_isolation": ["process", "sandbox"],
            "state_modes": ["ephemeral", "resumable"],
        },
        "interfaces": {
            "accepts": ["tobkiri.work-item/v1"],
            "produces": ["tobkiri.agent-result/v1"],
            "protocols": protocols,
        },
        "ports": {
            "model": {
                "contract": "rumi.service.ai.generate.v1",
                "binding_policy": "placement_required",
            },
            "tools": {
                "contract": "rumi.service.tool.invoke.v1",
                "binding_policy": "placement_required",
            },
            "skills": {"binding_policy": "placement"},
            "memory": {"binding_policy": "optional"},
            "workspace": {"binding_policy": "placement_required"},
        },
        "requirements": {
            "model_capabilities": (
                ["tool_calling"] if spec.get("tools") else []
            ),
            "runtime_capabilities": [],
            "required_capabilities": [],
            "optional_capabilities": [],
            "denied_capabilities": [],
            "minimum_pack_trust": "bundled",
        },
        "behavior": {
            "base_instructions": {
                "resource_ref": (
                    f"defaultspack://subagents/{spec['agent_id']}/base"
                ),
                "merge": "sealed_prefix",
            },
            "placement_role_instructions": "required",
        },
        "limits": {
            "context_token_budget": int(spec["context_limit"]),
        },
        "governance": {
            "approval": {"minimum": "confirm"},
        },
        "enforcement": {
            "tool_allowlist": "host_enforced",
            "workspace_scope": "host_enforced",
            "output_schema": "host_validated",
        },
    }


def _placement(spec: Mapping[str, Any]) -> dict[str, Any]:
    role = str(spec["role"])
    is_main = role == "main"
    return {
        "schema_version": SCHEMA_PLACEMENT,
        "id": str(spec["agent_id"]),
        "subject": {
            "exact_ref": f"pack://defaultspack/{role}@1.0.0",
        },
        "presentation": {
            "display_name": str(spec["display_name"]),
            "visible": True,
        },
        "role": {
            "contract_ref": f"tobkiri.role/{role}/v1",
            "instructions_ref": (
                f"defaultspack://placements/{spec['agent_id']}/instructions"
            ),
            "completion_contracts": ["tobkiri.agent-result/v1"],
            "behavior": {
                "instructions": str(spec["instructions"]),
            },
        },
        "bindings": [
            {
                "slot": "model",
                "provider_ref": "profile-model://default",
                "model_id": DEFAULT_MODEL,
            },
            {
                "slot": "tools",
                "allow_tool_ids": list(spec.get("tools", [])),
                "deny_effects": (
                    [] if role == "coding" else ["git.publish"]
                ),
            },
            {
                "slot": "skills",
                "required": [],
                "optional": [],
            },
            {
                "slot": "memory.private",
                "provider_ref": "memory://run",
                "access": "read_write",
            },
            {
                "slot": "workspace",
                "provider_ref": "workspace://current",
                "access": "read_write" if role == "coding" else "read_only",
            },
        ],
        "participation": [
            {
                "protocol_ref": f"tobkiri.protocol/{protocol}/v1",
                "participant_role": role,
            }
            for protocol in spec.get("protocols", [])
        ],
        "governance": {
            "maximum_parallel_runs": 1 if is_main else 2,
            "maximum_steps": 64,
            "maximum_tool_calls": 32,
            "context_token_budget": int(spec["context_limit"]),
            "spawn": {"allowed": role in {"main", "operations", "planning"}},
            "approval": {"minimum": "confirm"},
        },
        "features": [],
        "required_enforcement": {
            "tool_allowlist": "host_enforced",
            "workspace_scope": "host_enforced",
        },
    }


BUILTIN_SUBAGENT_DEFINITIONS: dict[str, dict[str, Any]] = {
    str(spec["agent_id"]): _definition(spec) for spec in _ROLE_SPECS
}
BUILTIN_SUBAGENT_PLACEMENTS: dict[str, dict[str, Any]] = {
    str(spec["agent_id"]): _placement(spec) for spec in _ROLE_SPECS
}


def default_operations_placement_map() -> dict[str, Any]:
    """Return the canonical built-in Team Placement Map."""

    placements = []
    for spec in _ROLE_SPECS:
        agent_id = str(spec["agent_id"])
        placements.append(
            {
                "placement_id": agent_id,
                "kind": (
                    "main"
                    if str(spec["role"]) == "main"
                    else "subagent"
                ),
                "role": str(spec["role"]),
            }
        )
    value = {
        "schema_version": SCHEMA_PLACEMENT_MAP,
        "id": DEFAULT_PLACEMENT_MAP_ID,
        "revision": 1,
        "main": {
            "placement_id": "client_manager",
            "mode": "interactive",
        },
        "placements": placements,
        "team_bindings": [
            {
                "slot": "communication",
                "provider_ref": "mailbox://default-operations",
            },
            {
                "slot": "artifacts",
                "provider_ref": "artifacts://default-operations",
            },
        ],
        "exposed_ports": {
            "input": ["tobkiri.work-item/v1"],
            "output": ["tobkiri.agent-result/v1"],
        },
        "governance": {
            "maximum_parallel_runs": 8,
            "approval": {"minimum": "confirm"},
        },
    }
    value["content_hash"] = _hash(value)
    return value


def get_builtin_definition(agent_id: str) -> dict[str, Any] | None:
    """Return one built-in Subagent Definition."""

    value = BUILTIN_SUBAGENT_DEFINITIONS.get(str(agent_id))
    return deepcopy(value) if value is not None else None


def get_builtin_placement(agent_id: str) -> dict[str, Any] | None:
    """Return one built-in Subagent Placement."""

    value = BUILTIN_SUBAGENT_PLACEMENTS.get(str(agent_id))
    return deepcopy(value) if value is not None else None


def _binding(
    placement: Mapping[str, Any],
    slot: str,
) -> dict[str, Any]:
    for value in placement.get("bindings", []):
        if isinstance(value, Mapping) and value.get("slot") == slot:
            return dict(value)
    return {}


def compile_builtin_effective_plan(
    agent_id: str,
    *,
    model: str = "",
    allowed_tools: Iterable[Any] | None = None,
    system_prompt: str | None = None,
    host_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile a host-enforced Effective Plan for a built-in role.

    This is Defaultspack's provider-side compilation of its own resources.
    The generic cross-Pack compiler remains authoritative for external Packs.
    """

    definition = get_builtin_definition(agent_id)
    placement = get_builtin_placement(agent_id)
    if definition is None or placement is None:
        raise ValueError(f"unknown built-in Subagent Placement: {agent_id}")
    tool_binding = _binding(placement, "tools")
    declared_tools = _dedupe_strings(tool_binding.get("allow_tool_ids", []))
    requested_tools = _tool_ids(allowed_tools or declared_tools)
    effective_tools = sorted(set(declared_tools).intersection(requested_tools))
    model_binding = _binding(placement, "model")
    selected_model = str(
        model
        or model_binding.get("model_id")
        or DEFAULT_MODEL
    ).strip()
    governance = dict(placement.get("governance") or {})
    policy = dict(host_policy or {})
    if policy.get("allowed_tool_ids"):
        effective_tools = sorted(
            set(effective_tools).intersection(
                _dedupe_strings(policy["allowed_tool_ids"])
            )
        )
    denied_tools = set(_dedupe_strings(policy.get("denied_tool_ids", [])))
    effective_tools = [
        tool_id for tool_id in effective_tools if tool_id not in denied_tools
    ]
    behavior_layers = [
        {
            "kind": "pack_base",
            "value": definition["behavior"],
        },
        {
            "kind": "placement_role",
            "value": (
                system_prompt
                if system_prompt is not None
                else placement["role"]["behavior"]["instructions"]
            ),
        },
        {
            "kind": "output_contract",
            "value": placement["role"]["completion_contracts"],
        },
    ]
    plan = {
        "schema_version": SCHEMA_EFFECTIVE_PLAN,
        "subagent": {
            "pack_id": "defaultspack",
            "id": definition["id"],
            "version": definition["version"],
            "content_hash": _hash(definition),
        },
        "placement": {
            "pack_id": "defaultspack",
            "id": placement["id"],
            "revision": _hash(placement),
            "map_id": DEFAULT_PLACEMENT_MAP_ID,
            "map_revision": default_operations_placement_map()["content_hash"],
        },
        "agent_kind": definition["kind"],
        "runtime_kind": "agent_run",
        "role": dict(placement["role"]),
        "bindings": deepcopy(placement["bindings"]),
        "model": {
            "provider_instance_id": "profile-model",
            "model_id": selected_model,
        },
        "tool_bindings": {
            "allow_tool_ids": effective_tools,
            "deny_tool_ids": sorted(denied_tools),
            "deny_effects": _dedupe_strings(
                tool_binding.get("deny_effects", [])
            ),
        },
        "skill_bindings": _binding(placement, "skills"),
        "memory_bindings": [
            value
            for value in placement["bindings"]
            if str(value.get("slot", "")).startswith("memory")
        ],
        "workspace_binding": _binding(placement, "workspace"),
        "protocol_bindings": list(placement["participation"]),
        "capability_plan_ref": str(
            policy.get("capability_plan_ref")
            or "defaultspack://compatibility-capability-plan"
        ),
        "effective_authority": _dedupe_strings(
            policy.get("effective_authority", [])
        ),
        "authority_envelope": {
            "source": "host_intersection",
            "tool_allowlist": effective_tools,
        },
        "budgets": {
            key: value
            for key, value in governance.items()
            if key
            in {
                "maximum_parallel_runs",
                "maximum_steps",
                "maximum_tool_calls",
                "maximum_cost",
                "timeout_seconds",
                "context_token_budget",
            }
        },
        "approval": dict(governance.get("approval") or {"minimum": "confirm"}),
        "behavior": {
            "layers": [
                {
                    **layer,
                    "sha256": _hash(layer["value"]),
                }
                for layer in behavior_layers
            ],
        },
        "enforcement": {
            "tool_allowlist": "host_enforced",
            "workspace_scope": "host_enforced",
            "output_schema": "host_validated",
            "system_prompt": "behavioral_only",
        },
        "revisions": {
            "subagent_content_hash": _hash(definition),
            "placement_revision": _hash(placement),
            "topology_revision": default_operations_placement_map()[
                "content_hash"
            ],
        },
    }
    plan["plan_hash"] = _hash(plan)
    return plan


def verify_effective_plan(plan: Mapping[str, Any]) -> None:
    """Validate the immutable identity of an Effective Subagent Plan."""

    if plan.get("schema_version") != SCHEMA_EFFECTIVE_PLAN:
        raise ValueError("Effective Subagent Plan schema is invalid")
    plan_hash = str(plan.get("plan_hash") or "")
    if not plan_hash:
        raise ValueError("Effective Subagent Plan hash is required")
    unsigned = {
        key: value for key, value in plan.items() if key != "plan_hash"
    }
    if _hash(unsigned) != plan_hash:
        raise ValueError("Effective Subagent Plan hash does not match")
    placement = plan.get("placement")
    if not isinstance(placement, Mapping) or not placement.get("revision"):
        raise ValueError("Effective Subagent Plan placement revision is required")
    if not plan.get("capability_plan_ref"):
        raise ValueError("Effective Subagent Plan capability reference is required")


def runtime_assignment_for_plan(
    plan: Mapping[str, Any],
    *,
    run_id: str,
    root_scope_id: str,
    parent_run_id: str | None = None,
    root_run_id: str | None = None,
) -> dict[str, Any]:
    """Create Defaultspack's runtime-state projection for an immutable plan."""

    verify_effective_plan(plan)
    placement = dict(plan.get("placement") or {})
    assignment = {
        "schema_version": "tobkiri.subagent-runtime-assignment/v1",
        "assignment_id": f"assignment:{run_id}",
        "run_id": str(run_id),
        "instance_id": f"subagent:{run_id}",
        "agent_kind": str(plan.get("agent_kind") or "subagent"),
        "runtime_kind": str(plan.get("runtime_kind") or "agent_run"),
        "placement_id": str(placement.get("id") or ""),
        "placement_revision": str(placement.get("revision") or ""),
        "placement_map_id": str(placement.get("map_id") or ""),
        "effective_plan_hash": str(plan.get("plan_hash") or ""),
        "root_scope_id": str(root_scope_id),
        "parent_run_id": str(parent_run_id or "") or None,
        "root_run_id": str(root_run_id or run_id),
        "protocol_membership": [
            str(item.get("protocol_ref"))
            for item in plan.get("protocol_bindings", [])
            if isinstance(item, Mapping) and item.get("protocol_ref")
        ],
        "state": "assigned",
    }
    assignment["assignment_hash"] = _hash(assignment)
    return assignment


def compatibility_effective_plan(
    *,
    agent_id: str,
    model: str,
    tools: Iterable[Any],
    system_prompt: str | None,
    host_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile a built-in plan or a bounded legacy delegate projection."""

    if agent_id in BUILTIN_SUBAGENT_PLACEMENTS:
        return compile_builtin_effective_plan(
            agent_id,
            model=model,
            allowed_tools=tools,
            system_prompt=system_prompt,
            host_policy=host_policy,
        )
    clean_tools = _tool_ids(tools)
    role = str(agent_id or "delegate").strip() or "delegate"
    plan = {
        "schema_version": SCHEMA_EFFECTIVE_PLAN,
        "subagent": {
            "pack_id": "defaultspack",
            "id": f"defaultspack.compat.{role}",
            "version": "1.0.0",
            "content_hash": _hash({"role": role}),
        },
        "placement": {
            "pack_id": "defaultspack",
            "id": f"compat-{role}",
            "revision": _hash(
                {
                    "role": role,
                    "model": model,
                    "tools": clean_tools,
                    "system_prompt": system_prompt or "",
                }
            ),
            "map_id": "compatibility",
            "map_revision": "legacy-projection",
        },
        "agent_kind": "subagent",
        "runtime_kind": "agent_run",
        "role": {
            "contract_ref": f"tobkiri.role/{role}/v1",
            "completion_contracts": ["tobkiri.agent-result/v1"],
        },
        "bindings": [
            {
                "slot": "model",
                "provider_ref": "profile-model://default",
                "model_id": str(model or DEFAULT_MODEL),
            },
            {
                "slot": "tools",
                "allow_tool_ids": clean_tools,
                "deny_tool_ids": [],
                "deny_effects": [],
            },
        ],
        "model": {
            "provider_instance_id": "profile-model",
            "model_id": str(model or DEFAULT_MODEL),
        },
        "tool_bindings": {
            "allow_tool_ids": clean_tools,
            "deny_tool_ids": [],
            "deny_effects": [],
        },
        "skill_bindings": {},
        "memory_bindings": [],
        "workspace_binding": {},
        "protocol_bindings": [
            {
                "protocol_ref": "tobkiri.protocol/delegate/v1",
                "participant_role": role,
            }
        ],
        "capability_plan_ref": "defaultspack://compatibility-capability-plan",
        "effective_authority": [],
        "authority_envelope": {
            "source": "legacy_projection",
            "tool_allowlist": clean_tools,
        },
        "budgets": {},
        "approval": {"minimum": "confirm"},
        "behavior": {
            "layers": [
                {
                    "kind": "placement_role",
                    "value": system_prompt or "",
                    "sha256": _hash(system_prompt or ""),
                }
            ],
        },
        "enforcement": {
            "tool_allowlist": "host_enforced",
            "workspace_scope": "host_enforced",
            "system_prompt": "behavioral_only",
        },
        "revisions": {
            "subagent_content_hash": _hash({"role": role}),
            "placement_revision": "",
            "topology_revision": "legacy-projection",
        },
        "compatibility_projection": True,
    }
    plan["revisions"]["placement_revision"] = plan["placement"]["revision"]
    plan["plan_hash"] = _hash(plan)
    return plan


def compile_utility_effective_plan(
    role_id: str,
    *,
    model: str,
    output_schema: str,
    maximum_tokens: int,
) -> dict[str, Any]:
    """Compile a no-Tool Utility Subagent plan."""

    role = str(role_id or "").strip()
    if not role:
        raise ValueError("Utility Subagent role is required")
    plan = compatibility_effective_plan(
        agent_id=f"utility-{role}",
        model=model,
        tools=[],
        system_prompt=(
            "Return only the requested structured utility result for "
            f"{role}."
        ),
    )
    plan["subagent"]["id"] = f"defaultspack.utility.{role}"
    plan["subagent"]["content_hash"] = _hash(
        {"role": role, "output_schema": output_schema}
    )
    plan["placement"]["id"] = f"{role}-subagent"
    plan["placement"]["revision"] = _hash(
        {
            "role": role,
            "model": model,
            "output_schema": output_schema,
            "maximum_tokens": maximum_tokens,
        }
    )
    plan["agent_kind"] = "subagent"
    plan["runtime_kind"] = "utility_model_call"
    plan["role"] = {
        "contract_ref": f"tobkiri.role/{role}/v1",
        "completion_contracts": [output_schema],
    }
    plan["budgets"] = {
        "maximum_steps": 1,
        "maximum_tool_calls": 0,
        "context_token_budget": int(maximum_tokens),
    }
    plan["protocol_bindings"] = [
        {
            "protocol_ref": "tobkiri.protocol/agent-tool/v1",
            "participant_role": role,
        }
    ]
    plan["revisions"]["subagent_content_hash"] = plan["subagent"][
        "content_hash"
    ]
    plan["revisions"]["placement_revision"] = plan["placement"]["revision"]
    plan["compatibility_projection"] = False
    plan["plan_hash"] = _hash(
        {key: value for key, value in plan.items() if key != "plan_hash"}
    )
    return plan


def legacy_agent_specs() -> list[dict[str, Any]]:
    """Project canonical Placements into the legacy Company agent shape."""

    result = []
    for spec in _ROLE_SPECS:
        agent_id = str(spec["agent_id"])
        placement = get_builtin_placement(agent_id) or {}
        definition = get_builtin_definition(agent_id) or {}
        result.append(
            {
                "agent_id": agent_id,
                "role_key": str(spec["role"]),
                "agent_name": str(spec["display_name"]),
                "display_name": str(spec["display_name"]),
                "model": DEFAULT_MODEL,
                "allowed_tools": list(spec.get("tools", [])),
                "context_limit": int(spec["context_limit"]),
                "aliases": list(spec.get("aliases", [])),
                "system_prompt": str(spec["instructions"]),
                "agent_kind": (
                    "main" if str(spec["role"]) == "main" else "subagent"
                ),
                "runtime_kind": "agent_run",
                "subagent_role": str(spec["role"]),
                "placement_id": agent_id,
                "placement_revision": _hash(placement),
                "placement_map_id": DEFAULT_PLACEMENT_MAP_ID,
                "subagent_definition_ref": (
                    f"pack://defaultspack/{definition.get('id')}@"
                    f"{definition.get('version')}"
                ),
                "protocol_membership": [
                    value["protocol_ref"]
                    for value in placement.get("participation", [])
                ],
                "metadata": {
                    "compatibility_projection": "DEFAULT_AGENT_SPECS",
                    "canonical_source": "subagent_placement",
                },
            }
        )
    return result
