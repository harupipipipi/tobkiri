from __future__ import annotations

import json
import os
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

_PACK_ROOT = Path(__file__).resolve().parents[2]
_DEFAULTSPACK_ROOT = _PACK_ROOT.parent / "defaultspack"
for _path in (str(_PACK_ROOT), str(_DEFAULTSPACK_ROOT)):
    if _path in sys.path:
        sys.path.remove(_path)
for _path in (str(_PACK_ROOT), str(_DEFAULTSPACK_ROOT)):
    sys.path.insert(0, _path)

from blocks._common import timestamp
from domain.agent.org_manager import OrgManager
from domain.agent.role_registry import RoleRegistry


PROFILE_ID = "defaultspack.operations_company"
CONVERSATION_KIND = "operations_company"
DEFAULT_MODEL = "stub/default"


MODEL_ALLOWLIST = [
    "openrouter/cohere/north-mini-code:free",
    "google/gemini-2.5-flash",
    "google/gemini-2.5-pro",
    "openai/gpt-5.4",
    "openai/gpt-5.4-mini",
    "anthropic/claude-sonnet-4-6",
    "stub/default",
]


TOOL_ALLOWLIST = [
    "rumi_api",
    "todo",
    "subagent",
    "web_search",
    "reddit_search",
    "file_reader",
    "browser_use",
    "browser_computer",
    "computer_use",
    "calculator",
    "coding_file_read",
    "coding_file_search",
    "coding_file_list",
    "coding_file_write",
    "coding_file_create",
    "coding_file_patch",
    "coding_file_restore",
    "coding_git_status",
    "coding_git_diff",
    "coding_terminal_exec",
]


ROLE_DEFINITIONS = [
    {
        "agent_id": "client_manager",
        "role_key": "client_manager",
        "agent_name": "Client Manager",
        "display_name": "Client Manager",
        "model": DEFAULT_MODEL,
        "allowed_tools": ["rumi_api", "todo", "subagent"],
        "context_limit": 64000,
        "system_prompt": (
            "You are the client-facing manager. Keep one clear conversation with the user, "
            "translate user requests into company work, summarize internal progress, and ask "
            "for approval only when the company needs authority, credentials, or judgment."
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
        "system_prompt": (
            "You operate the asynchronous company workspace. Triage open tasks, stale runs, "
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
        "system_prompt": (
            "You own task decomposition, ownership, milestones, blocker routing, and final "
            "handoff quality. Mention specialists in the internal channel when assigning work. "
            "You delegate work; you do not write production code, execute terminal commands, "
            "or perform deep research directly."
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
        "system_prompt": (
            "You manage recurring tasks and heartbeat jobs. Avoid creating schedule loops, "
            "keep cadence explicit, and report only meaningful changes."
        ),
    },
]


class OperationsCompanyRuntime:
    """Runtime contract for the long-running company-style profile."""

    def __init__(self, pack_root: Path | None = None) -> None:
        self.pack_root = pack_root or Path(__file__).resolve().parents[2]
        self.state_path = self._resolve_state_path()

    def manifest(self) -> dict[str, Any]:
        return {
            "profile_id": PROFILE_ID,
            "name": "Rumi Operations Company",
            "conversation_kind": CONVERSATION_KIND,
            "non_stop": True,
            "can_run_24_7": True,
            "shared_resources": {
                "tool_node": "defaultspack.tool",
                "settings_source": "defaultspack.frontend_settings",
                "browser_profile_id": "defaultspack-shared",
                "chat_store": "defaultspack.user_data.shared.chat",
            },
            "memory": {
                "conversation_strategy": "one_agent_one_conversation",
                "compact_context": True,
                "compact_after_messages": 32,
                "protect_last_messages": 12,
                "session_search": True,
                "persist_decisions_incidents_handoffs": True,
            },
            "workspace": {
                "contract_version": "rumi.agent_workspace.v1",
                "mode": "isolated_per_agent",
                "write_scope": "agent_workspace_root",
                "shared_workspace": "operations_company/shared",
                "agent_workspace_template": "operations_company/agents/{agent_id}",
                "worktree": {
                    "supported": True,
                    "mode": "metadata_only",
                    "branch_template": "rumi/{agent_id}/{task_id}",
                },
            },
            "scheduler": {
                "enabled": True,
                "default_heartbeat_minutes": 15,
                "supports": ["interval", "cron", "once", "24_7_monitor"],
                "normal_status_silent": True,
            },
            "model_self_selection": {
                "enabled": True,
                "allowlist": list(MODEL_ALLOWLIST),
                "default_reasoning_effort": "medium",
                "max_switches_per_day": 12,
                "audit_required": True,
            },
            "tool_policy": {
                "allowlist": list(TOOL_ALLOWLIST),
                "denylist": [],
                "role_overrides": {
                    role["role_key"]: list(role["allowed_tools"])
                    for role in ROLE_DEFINITIONS
                },
            },
            "roles": deepcopy(ROLE_DEFINITIONS),
            "channels": [
                {
                    "id": "ops-company",
                    "name": "ops-company",
                    "visibility": "team",
                    "mentions": True,
                    "append_only": True,
                }
            ],
        }

    def status(self) -> dict[str, Any]:
        state = self._load_state()
        org_id = state.get("org_id")
        org = OrgManager().get_org(org_id) if org_id else None
        schedules = self._schedules_for_state(state)
        conversation_id = state.get("conversation_id")
        company = self._sync_company_record(state)
        return {
            "profile_id": PROFILE_ID,
            "bootstrapped": bool(org),
            "org_id": org_id,
            "conversation_id": conversation_id,
            "conversation_group_id": state.get("conversation_group_id"),
            "company": company,
            "org": org,
            "schedules": schedules,
            "state": state,
            "manifest": self.manifest(),
            "updated_at": timestamp(),
        }

    def bootstrap(
        self,
        *,
        start_nonstop: bool = True,
        heartbeat_minutes: int = 15,
        model: str | None = None,
    ) -> dict[str, Any]:
        self._define_roles()
        state = self._load_state()
        org_id = self._ensure_org(state)
        conversation_id = self._ensure_conversation(state, model=model)
        if start_nonstop:
            self._ensure_heartbeat_schedule(
                state,
                conversation_id=conversation_id,
                heartbeat_minutes=heartbeat_minutes,
                model=model,
            )
        state["org_id"] = org_id
        state["conversation_id"] = conversation_id
        state["conversation_group_id"] = self._conversation_group_id()
        state["last_bootstrapped_at"] = timestamp()
        self._save_state(state)
        return self.status()

    def _conversation_group_id(self) -> str:
        try:
            from domain.company.models import DEFAULT_CONVERSATION_GROUP_ID

            return DEFAULT_CONVERSATION_GROUP_ID
        except Exception:
            return "company:operations-company"

    def _sync_company_record(self, state: dict[str, Any]) -> dict[str, Any] | None:
        try:
            from domain.company.migration import migrate_operations_company_state
            from domain.company.service import CompanyService

            if state:
                state.setdefault("conversation_group_id", self._conversation_group_id())
                return migrate_operations_company_state(state)
            return CompanyService().bootstrap_default_company(
                metadata={
                    "profile_id": PROFILE_ID,
                    "conversation_group_id": self._conversation_group_id(),
                }
            )
        except Exception:
            return None

    def _resolve_state_path(self) -> Path:
        override = os.environ.get("RUMI_DEFAULTSPACK_OPERATIONS_STATE_PATH", "").strip()
        if override:
            return Path(override)
        return self.pack_root / "user_data" / "shared" / "operations_company" / "state.json"

    def _load_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_state(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": 1, **state, "updated_at": timestamp()}
        self.state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _define_roles(self) -> None:
        registry = RoleRegistry()
        for role in ROLE_DEFINITIONS:
            registry.define_role(
                role_key=role["role_key"],
                display_name=role["display_name"],
                system_prompt=role["system_prompt"],
                allowed_tools=role["allowed_tools"],
                context_limit=role["context_limit"],
            )

    def _ensure_org(self, state: dict[str, Any]) -> str:
        manager = OrgManager()
        org_id = state.get("org_id")
        org = manager.get_org(org_id) if org_id else None
        if org is None:
            org = manager.create_org(
                "Rumi Operations Company",
                "24/7 company-style AI organization for monitors and scheduled work.",
                created_by="defaultspack.operations_company",
            )
            org_id = org["org_id"]
        for role in ROLE_DEFINITIONS:
            manager.add_member(
                org_id,
                role["agent_id"],
                role["agent_name"],
                role["role_key"],
                role.get("model") or DEFAULT_MODEL,
            )
        return str(org_id)

    def _ensure_conversation(self, state: dict[str, Any], *, model: str | None = None) -> str:
        from domain.chat.store import ChatStore

        store = ChatStore()
        conversation_id = state.get("conversation_id")
        if conversation_id and store.get_conversation(conversation_id):
            conversation = store.get_conversation(conversation_id)
            metadata = conversation.get("metadata") if isinstance(conversation, dict) else {}
            if not isinstance(metadata, dict):
                metadata = {}
            store.update_conversation(
                conversation_id,
                {
                    "group_id": state.get("conversation_group_id") or self._conversation_group_id(),
                    "metadata": {
                        **metadata,
                        "profile_id": PROFILE_ID,
                        "client_manager_agent_id": "client_manager",
                        "one_agent_one_conversation": True,
                    },
                },
            )
            return str(conversation_id)
        conversation = store.create_conversation(
            model=model or DEFAULT_MODEL,
            system_prompt_id="operations_company",
            agent_id="client_manager",
            tags=["operations-company", "24-7", "company"],
            conversation_kind=CONVERSATION_KIND,
            group_id=state.get("conversation_group_id") or self._conversation_group_id(),
            metadata={
                "profile_id": PROFILE_ID,
                "client_manager_agent_id": "client_manager",
                "one_agent_one_conversation": True,
            },
        )
        store.update_conversation(conversation["id"], {"title": "Operations Company"})
        return str(conversation["id"])

    def _ensure_heartbeat_schedule(
        self,
        state: dict[str, Any],
        *,
        conversation_id: str,
        heartbeat_minutes: int,
        model: str | None = None,
    ) -> None:
        from domain.agent.scheduler import Scheduler

        safe_minutes = int(heartbeat_minutes) if isinstance(heartbeat_minutes, int) else 15
        safe_minutes = max(1, min(safe_minutes, 1440))
        schedule_ids = state.setdefault("schedule_ids", {})
        scheduler = Scheduler()
        existing_id = schedule_ids.get("heartbeat")
        if existing_id and scheduler.get_schedule(existing_id):
            return
        task_message = (
            "Run an Operations Company heartbeat. Check for pending monitor tasks, "
            "scheduled work, blockers, and incidents. If everything is normal, keep "
            "the result concise and mark it silent; if action is needed, report to "
            "@client_manager and @project_manager."
        )
        schedule = scheduler.create_schedule(
            "interval",
            {
                "message": task_message,
                "model": model or DEFAULT_MODEL,
                "conversation_id": conversation_id,
                "timeout": 300,
                "profile_id": PROFILE_ID,
                "agent_id": "operations_monitor",
                "tools": ["rumi_api", "todo", "browser_computer", "web_search", "subagent"],
                "tool_policy": {
                    "profile_id": PROFILE_ID,
                    "non_stop": True,
                    "allow_shell": False,
                    "allow_file_write": True,
                    "write_actions_require_approval": True,
                    "normal_status_silent": True,
                    "tool_allowlist": TOOL_ALLOWLIST,
                    "model_allowlist": MODEL_ALLOWLIST,
                },
                "metadata": {
                    "profile_id": PROFILE_ID,
                    "agent_id": "operations_monitor",
                    "internal_channel": "ops-company",
                },
            },
            {"value": safe_minutes, "unit": "minutes"},
            name="Operations Company heartbeat",
            description="Non-stop heartbeat for 24/7 monitoring and scheduled task pickup.",
        )
        schedule_ids["heartbeat"] = schedule["id"]

    def _schedules_for_state(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            from domain.agent.scheduler import Scheduler

            scheduler = Scheduler()
            schedule_ids = state.get("schedule_ids") if isinstance(state.get("schedule_ids"), dict) else {}
            schedules = []
            for schedule_id in schedule_ids.values():
                schedule = scheduler.get_schedule(schedule_id)
                if schedule:
                    schedules.append(schedule)
            return schedules
        except Exception:
            return []
