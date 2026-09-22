from __future__ import annotations

import copy
import time
import uuid
from typing import Any

from domain.agent.placement_catalog import (
    DEFAULT_MODEL as PLACEMENT_DEFAULT_MODEL,
    DEFAULT_PLACEMENT_MAP_ID,
    legacy_agent_specs,
)


SCHEMA_VERSION = 1
DEFAULT_COMPANY_ID = "operations-company"
DEFAULT_COMPANY_NAME = "Tobkiri Operations Team"
DEFAULT_COMPANY_DESCRIPTION = (
    "Placement-backed Team workspace coordinated by the Main Agent."
)
DEFAULT_CONVERSATION_GROUP_ID = "company:operations-company"
DEFAULT_CHANNEL_ID = "ops-company"
DEFAULT_MODEL = PLACEMENT_DEFAULT_MODEL


def gen_id(prefix: str = "") -> str:
    return prefix + str(uuid.uuid4())


def timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


DEFAULT_SETTINGS: dict[str, Any] = {
    "task_policy": "queued",
    "dispatch_policy": "local_queue_only",
    "normal_status_silent": True,
    "mentions_create_tasks": True,
    "direct_tool_execution": False,
}


_LEGACY_DEFAULT_AGENT_SPECS: list[dict[str, Any]] = [
    {
        "agent_id": "client_manager",
        "role_key": "client_manager",
        "agent_name": "President",
        "display_name": "President",
        "model": DEFAULT_MODEL,
        "allowed_tools": ["rumi_api", "todo", "subagent"],
        "context_limit": 64000,
        "aliases": ["client", "president", "ceo"],
        "system_prompt": (
            "You are the president in the main Rumi chat. You do not write code or perform "
            "specialist work directly. Create, assign, and manage employee tasks, summarize "
            "their progress, and ask for approval only when the team needs authority, "
            "credentials, or judgment."
        ),
    },
    {
        "agent_id": "operations_manager",
        "role_key": "operations_manager",
        "agent_name": "Operations Manager",
        "display_name": "Operations Manager",
        "model": DEFAULT_MODEL,
        "allowed_tools": ["rumi_api", "todo", "subagent"],
        "context_limit": 96000,
        "aliases": ["ops_manager", "manager"],
        "system_prompt": (
            "You operate the asynchronous team workspace. Triage open tasks, stale runs, "
            "blocked work, waiting approvals, unresolved mentions, and dirty summaries. "
            "Route work through AgentEngine delegation and never execute specialist tools directly."
        ),
    },
    {
        "agent_id": "project_manager",
        "role_key": "project_manager",
        "agent_name": "Project Manager",
        "display_name": "Project Manager",
        "model": DEFAULT_MODEL,
        "allowed_tools": ["rumi_api", "todo", "subagent", "web_search"],
        "context_limit": 96000,
        "aliases": ["pm", "project_manager"],
        "system_prompt": (
            "You own task decomposition, ownership, milestones, blocker routing, and final "
            "handoff quality. You delegate work to specialists; you do not write production "
            "code, execute terminal commands, or perform deep research directly."
        ),
    },
    {
        "agent_id": "coding_engineer",
        "role_key": "coding_engineer",
        "agent_name": "Coding Engineer",
        "display_name": "Coding Engineer",
        "model": DEFAULT_MODEL,
        "allowed_tools": [
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
        "context_limit": 128000,
        "aliases": ["engineer", "coder"],
        "system_prompt": (
            "You implement bounded code changes in the current workspace. Follow local style, "
            "keep diffs scoped, and report changed paths and validation back to the PM."
        ),
    },
    {
        "agent_id": "research_specialist",
        "role_key": "research_specialist",
        "agent_name": "Research Specialist",
        "display_name": "Research Specialist",
        "model": DEFAULT_MODEL,
        "allowed_tools": ["rumi_api", "web_search", "reddit_search", "file_reader", "todo"],
        "context_limit": 96000,
        "aliases": ["researcher", "research"],
        "system_prompt": (
            "You research facts, docs, competitive behavior, and user voice. Prefer primary "
            "sources and note uncertainty, dates, and citations in reports."
        ),
    },
    {
        "agent_id": "reviewer",
        "role_key": "reviewer",
        "agent_name": "Reviewer",
        "display_name": "Reviewer",
        "model": DEFAULT_MODEL,
        "allowed_tools": [
            "rumi_api",
            "coding_file_read",
            "coding_file_search",
            "coding_git_status",
            "coding_git_diff",
            "coding_terminal_exec",
        ],
        "context_limit": 96000,
        "aliases": ["review"],
        "system_prompt": (
            "You review work for correctness, safety, missing tests, and drift from the user "
            "goal. Lead with actionable findings and residual risk."
        ),
    },
    {
        "agent_id": "operations_monitor",
        "role_key": "operations_monitor",
        "agent_name": "Operations Monitor",
        "display_name": "Operations Monitor",
        "model": DEFAULT_MODEL,
        "allowed_tools": ["rumi_api", "browser_use", "browser_computer", "web_search", "todo"],
        "context_limit": 64000,
        "aliases": ["monitor"],
        "system_prompt": (
            "You watch dashboards, queues, websites, and integrations. Stay silent on normal "
            "checks unless asked, and escalate incidents with evidence and next action."
        ),
    },
    {
        "agent_id": "scribe",
        "role_key": "scribe",
        "agent_name": "Scribe",
        "display_name": "Scribe",
        "model": DEFAULT_MODEL,
        "allowed_tools": ["rumi_api", "todo"],
        "context_limit": 64000,
        "aliases": ["summary", "summarizer"],
        "system_prompt": (
            "You maintain concise summaries for company, channel, thread, task, and run scopes. "
            "Capture decisions, blockers, owners, and current status without taking ownership of execution."
        ),
    },
    {
        "agent_id": "scheduler",
        "role_key": "scheduler",
        "agent_name": "Scheduler",
        "display_name": "Scheduler",
        "model": DEFAULT_MODEL,
        "allowed_tools": ["rumi_api", "todo", "subagent"],
        "context_limit": 48000,
        "aliases": ["schedule"],
        "system_prompt": (
            "You manage recurring tasks and heartbeat jobs. Avoid creating schedule loops, "
            "keep cadence explicit, and report only meaningful changes."
        ),
    },
]

# Compatibility consumers retain ``DEFAULT_AGENT_SPECS``, but its canonical
# values now come from Subagent Definitions and the default Placement Map.
DEFAULT_AGENT_SPECS: list[dict[str, Any]] = legacy_agent_specs()


def default_agents() -> list[dict[str, Any]]:
    return [normalize_agent(agent) for agent in DEFAULT_AGENT_SPECS]


def apply_builtin_placement_projection(
    agents: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Migrate known legacy members without discarding user model choices."""

    projected = {
        agent["agent_id"]: normalize_agent(agent)
        for agent in DEFAULT_AGENT_SPECS
    }
    result = copy.deepcopy(agents)
    for agent_id, canonical in projected.items():
        current = result.get(agent_id)
        if not isinstance(current, dict):
            result[agent_id] = canonical
            continue
        preserved = {
            key: current.get(key)
            for key in ("model", "status", "created_at")
            if current.get(key) not in (None, "")
        }
        current_metadata = (
            current.get("metadata")
            if isinstance(current.get("metadata"), dict)
            else {}
        )
        result[agent_id] = normalize_agent(
            {
                **current,
                **canonical,
                **preserved,
                "metadata": {
                    **current_metadata,
                    **canonical.get("metadata", {}),
                    "migrated_to_placement": True,
                },
            }
        )
    return result


def default_channel(now: str | None = None) -> dict[str, Any]:
    ts = now or timestamp()
    return {
        "id": DEFAULT_CHANNEL_ID,
        "name": DEFAULT_CHANNEL_ID,
        "description": "Internal company coordination channel.",
        "visibility": "team",
        "members": [agent["agent_id"] for agent in DEFAULT_AGENT_SPECS],
        "mentions": True,
        "append_only": True,
        "message_count": 0,
        "last_message_at": None,
        "metadata": {"default": True},
        "created_at": ts,
        "updated_at": ts,
    }


def normalize_agent(agent: dict[str, Any]) -> dict[str, Any]:
    item = copy.deepcopy(agent)
    agent_id = str(item.get("agent_id") or item.get("id") or item.get("role_key") or "").strip()
    if not agent_id:
        agent_id = gen_id("agent_")
    item["id"] = agent_id
    item["agent_id"] = agent_id
    item["role_key"] = str(item.get("role_key") or agent_id).strip()
    item["agent_name"] = str(item.get("agent_name") or item.get("display_name") or agent_id).strip()
    item["display_name"] = str(item.get("display_name") or item["agent_name"]).strip()
    item["model"] = str(item.get("model") or DEFAULT_MODEL).strip()
    item["allowed_tools"] = list(item.get("allowed_tools") or [])
    item["context_limit"] = int(item.get("context_limit") or 64000)
    item["aliases"] = [str(alias).strip().lstrip("@") for alias in item.get("aliases", []) if str(alias).strip()]
    item["agent_kind"] = str(
        item.get("agent_kind")
        or ("main" if agent_id == "client_manager" else "subagent")
    )
    if item["agent_kind"] not in {"main", "subagent"}:
        item["agent_kind"] = "subagent"
    item["runtime_kind"] = str(
        item.get("runtime_kind") or "agent_run"
    )
    item["subagent_role"] = str(
        item.get("subagent_role") or item["role_key"] or "custom"
    )
    item["placement_id"] = str(
        item.get("placement_id")
        or (
            agent_id
            if agent_id in {value["agent_id"] for value in DEFAULT_AGENT_SPECS}
            else f"{agent_id}-subagent"
        )
    )
    item["placement_map_id"] = str(
        item.get("placement_map_id") or DEFAULT_PLACEMENT_MAP_ID
    )
    item["protocol_membership"] = [
        str(value)
        for value in item.get("protocol_membership", [])
        if str(value).strip()
    ]
    item.setdefault("status", "idle")
    item.setdefault("metadata", {})
    item.setdefault("created_at", timestamp())
    item["updated_at"] = timestamp()
    return item


def normalize_company(company: dict[str, Any]) -> dict[str, Any]:
    item = copy.deepcopy(company)
    now = timestamp()
    item.setdefault("id", gen_id("company_"))
    item["id"] = str(item["id"])
    item.setdefault("name", DEFAULT_COMPANY_NAME)
    item.setdefault("description", "")
    item.setdefault("status", "active")
    item.setdefault("conversation_group_id", "company:" + item["id"])
    item.setdefault("settings", copy.deepcopy(DEFAULT_SETTINGS))
    item.setdefault("metadata", {})
    item.setdefault("agents", {})
    item.setdefault("channels", {})
    item.setdefault("messages", {})
    item.setdefault("tasks", {})
    item.setdefault("inbound_routes", {})
    item.setdefault("created_at", now)
    item.setdefault("updated_at", now)
    if isinstance(item["agents"], list):
        item["agents"] = {agent["agent_id"]: normalize_agent(agent) for agent in item["agents"] if isinstance(agent, dict)}
    elif isinstance(item["agents"], dict):
        item["agents"] = {
            str(agent_id): normalize_agent(agent if isinstance(agent, dict) else {"agent_id": str(agent_id)})
            for agent_id, agent in item["agents"].items()
        }
    else:
        item["agents"] = {}
    if item["id"] == DEFAULT_COMPANY_ID:
        item["agents"] = apply_builtin_placement_projection(item["agents"])
        item["metadata"] = {
            **item["metadata"],
            "placement_map_id": "default-operations",
            "canonical_agent_model": "main-subagent-placement",
        }
    if not isinstance(item["channels"], dict):
        item["channels"] = {}
    if not isinstance(item["messages"], dict):
        item["messages"] = {}
    if not isinstance(item["tasks"], dict):
        item["tasks"] = {}
    if not isinstance(item["inbound_routes"], dict):
        item["inbound_routes"] = {}
    return item


def public_company(company: dict[str, Any]) -> dict[str, Any]:
    item = normalize_company(company)
    item["agent_count"] = len(item.get("agents", {}))
    item["channel_count"] = len(item.get("channels", {}))
    item["message_count"] = len(item.get("messages", {}))
    item["task_count"] = len(item.get("tasks", {}))
    return item
