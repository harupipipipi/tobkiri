from __future__ import annotations

import uuid
from typing import Any

from tobkiri_protocol.settings_state import SettingsOwnerPort

from domain.company.message_router import CompanySlackRuntime
from domain.company.mention import CompanyMentionService
from domain.company.models import DEFAULT_CHANNEL_ID, timestamp
from domain.company.runtime_store import CompanyRuntimeStore
from domain.company.service import CompanyService
from domain.company.store import CompanyStore

from .creator_service import CreatorService
from .ids import slug_id, stable_short_id
from .mention_parser import parse_mentions
from .models import PM_THRESHOLD, TOOL_ID_ALIASES, team_metadata
from .normalizers import (
    enrich_short_ids,
    lifecycle_update,
    normalize_goal_request,
    normalize_message_request,
    normalize_team_agent,
    normalize_team_channel,
)
from .pm_gate import PM_AGENT_IDS, gated_content, pm_gate_decision
from .prompt_context import build_channel_check_context
from .rich_policy import evaluate_rich_payload, evaluate_rich_policy


class SubagentTeamService:
    """Slack/Discord-like Team Workspace facade over CompanySlackRuntime."""

    def __init__(
        self,
        *,
        company_store: CompanyStore | None = None,
        runtime_store: CompanyRuntimeStore | None = None,
        settings_owner: SettingsOwnerPort | None = None,
    ) -> None:
        self.company_store = company_store or CompanyStore()
        self.runtime_store = runtime_store or CompanyRuntimeStore()
        self.settings_owner = settings_owner

    def ensure_team(self, data: dict[str, Any] | None = None) -> dict[str, Any]:
        data = data if isinstance(data, dict) else {}
        company_id = str(data.get("company_id") or data.get("id") or "").strip()
        conversation_id = str(data.get("conversation_id") or "").strip()
        if company_id:
            company = self.company_store.get_company(company_id)
            if company is None and data.get("bootstrap"):
                company = CompanyService(
                    self.company_store,
                    settings_owner=self.settings_owner,
                ).create_company(
                    {
                        "id": company_id,
                        "name": data.get("name") or "Team Workspace",
                        "description": data.get("description") or "Creator-managed subagent team workspace.",
                        "metadata": {"subagent_team": True, **(data.get("metadata") if isinstance(data.get("metadata"), dict) else {})},
                    }
                )
            return {"company_id": company_id, "company": company, "bootstrapped": company is not None}
        if conversation_id:
            return CompanyService(
                self.company_store,
                settings_owner=self.settings_owner,
            ).status_for_conversation(
                conversation_id,
                bootstrap=bool(data.get("bootstrap", True)),
            )
        return CompanyService(
            self.company_store,
            settings_owner=self.settings_owner,
        ).status()

    def status(self, company_id: str) -> dict[str, Any] | None:
        company = self.company_store.get_company(company_id)
        if company is None:
            return None
        tasks, _ = self.runtime_store.list_tasks(company_id, limit=100)
        runs = self.runtime_store.list_run_links(company_id, limit=100)
        inbox = self.runtime_store.list_inbox(company_id, limit=100)
        return {
            "company": company,
            "runtime": "CompanySlackRuntime",
            "stats": self.runtime_store.stats(company_id),
            "open_tasks": [task for task in tasks if str(task.get("status")) not in {"completed", "cancelled", "failed"}],
            "runs": runs,
            "inbox": inbox,
            "policy": {
                "direct_tool_execution": False,
                "routing": "agent.delegate",
                "client_approved_flags_trusted": False,
                "rich": self.rich_status(company_id, requested_new_agents=0),
            },
            "actions": [
                {
                    "id": safe_id,
                    "provider_safe_id": safe_id,
                    "display_name": dotted,
                    "legacy_dot_name": dotted,
                    "direct_tool_execution": False,
                    "creator_managed": safe_id
                    in {"subagent_request", "subagent_status", "subagent_create", "subagent_dm_send", "subagent_channel_join"},
                }
                for safe_id, dotted in TOOL_ID_ALIASES.items()
            ],
        }

    def rich_status(self, company_id: str, *, requested_new_agents: int = 0) -> dict[str, Any] | None:
        if self.company_store.get_company(company_id) is None:
            return None
        settings = self.company_store.get_settings(company_id) or {}
        policy = evaluate_rich_policy(
            company_id,
            requested_new_agents=requested_new_agents,
            settings=settings,
            runtime_store=self.runtime_store,
        )
        return {
            "rich_enabled": bool(policy["enabled"]),
            "enabled": bool(policy["enabled"]),
            "cap": int(policy["cap"]),
            "active_agents": int(policy["active_agents"]),
            "requested_new_agents": int(policy["requested_new_agents"]),
            "available_slots": int(policy["available_slots"]),
            "allowed": bool(policy["allowed"]),
            "code": policy.get("code"),
            "reason": str(policy.get("reason") or ""),
        }

    def update_rich_state(self, company_id: str, data: dict[str, Any], *, context: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if self.company_store.get_company(company_id) is None:
            return None
        actor_id = trusted_actor_from_context(context)
        if not actor_id:
            return _deny("trusted context actor is required to update /rich", "ACTOR_REQUIRED")
        auth = self.authorize_pm_actor(company_id, actor_id, channel_id=data.get("channel_id"))
        if not auth["allowed"]:
            return _deny("only channel PM, project_manager, or operations_manager can update /rich", "FORBIDDEN", auth=auth)
        enabled = data.get("enabled", data.get("rich_enabled"))
        if enabled is None:
            return self.rich_status(company_id, requested_new_agents=int(data.get("requested_new_agents") or 0))
        settings = self.company_store.get_settings(company_id) or {}
        nested = settings.get("subagent_team") if isinstance(settings.get("subagent_team"), dict) else {}
        cap = data.get("cap", data.get("rich_agent_cap", nested.get("rich_agent_cap", 5)))
        try:
            cap = max(1, int(cap))
        except (TypeError, ValueError):
            cap = 5
        updated = self.company_store.update_settings(
            company_id,
            {
                **settings,
                "subagent_team": {
                    **nested,
                    "rich_enabled": bool(enabled),
                    "rich_agent_cap": cap,
                    "rich_updated_by": auth["actor_id"],
                    "rich_updated_at": timestamp(),
                },
            },
        )
        if updated is None:
            return None
        return self.rich_status(company_id, requested_new_agents=int(data.get("requested_new_agents") or 0))

    def list_channels(self, company_id: str) -> list[dict[str, Any]] | None:
        channels = self.company_store.list_channels(company_id)
        if channels is None:
            return None
        return enrich_short_ids(channels, prefix="ch")

    def get_channel(self, company_id: str, channel_id: str) -> dict[str, Any] | None:
        channel = self.company_store.get_channel(company_id, channel_id)
        if channel is None:
            return None
        return enrich_short_ids([channel], prefix="ch")[0]

    def upsert_channel(self, company_id: str, data: dict[str, Any], *, actor_id: str = "creator") -> dict[str, Any] | None:
        existing = self.list_channels(company_id) or []
        channel = normalize_team_channel(data, existing_short_ids=[str(item.get("short_id")) for item in existing])
        policy = self._channel_pm_policy(company_id, channel)
        if not policy["allowed"]:
            return _deny(str(policy["reason"]), str(policy["code"]), channel_policy=policy)
        channel["metadata"] = lifecycle_update(channel.get("metadata"), state="active", actor_id=actor_id)
        return self.company_store.upsert_channel(company_id, channel)

    def patch_channel(self, company_id: str, channel_id: str, updates: dict[str, Any], *, actor_id: str = "creator") -> dict[str, Any] | None:
        current = self.company_store.get_channel(company_id, str(channel_id))
        if current is None:
            return None
        patch = dict(updates or {})
        patch.pop("id", None)
        patch.pop("channel_id", None)
        merged = {**current, **patch, "id": str(channel_id)}
        if "members" not in patch:
            merged["members"] = list(current.get("members") or [])
        if isinstance(current.get("metadata"), dict) or isinstance(patch.get("metadata"), dict):
            merged["metadata"] = {
                **(current.get("metadata") if isinstance(current.get("metadata"), dict) else {}),
                **(patch.get("metadata") if isinstance(patch.get("metadata"), dict) else {}),
            }
        return self.upsert_channel(company_id, merged, actor_id=actor_id)

    def archive_channel(self, company_id: str, channel_id: str, *, actor_id: str = "creator") -> dict[str, Any] | None:
        channel = self.company_store.get_channel(company_id, channel_id)
        if channel is None:
            return None
        channel["metadata"] = lifecycle_update(channel.get("metadata"), state="archived", actor_id=actor_id)
        channel["visibility"] = "archived"
        return self.company_store.upsert_channel(company_id, channel)

    def join_channel(self, company_id: str, channel_id: str, agent_id: str, *, actor_id: str = "creator") -> dict[str, Any] | None:
        channel = self.company_store.get_channel(company_id, str(channel_id))
        if channel is None:
            return None
        resolved = self.resolve_agent_id(company_id, agent_id) or str(agent_id or "").strip().lstrip("@")
        if not resolved:
            return _deny("agent_id is required", "INVALID_INPUT")
        members = self._resolved_channel_members(company_id, channel)
        if resolved not in members:
            members.append(resolved)
        return self.patch_channel(
            company_id,
            str(channel_id),
            {"members": members},
            actor_id=actor_id or resolved,
        )

    def leave_channel(self, company_id: str, channel_id: str, agent_id: str, *, actor_id: str = "creator") -> dict[str, Any] | None:
        channel = self.company_store.get_channel(company_id, str(channel_id))
        if channel is None:
            return None
        resolved = self.resolve_agent_id(company_id, agent_id) or str(agent_id or "").strip().lstrip("@")
        if not resolved:
            return _deny("agent_id is required", "INVALID_INPUT")
        members = [member for member in self._resolved_channel_members(company_id, channel) if member != resolved]
        return self.patch_channel(
            company_id,
            str(channel_id),
            {"members": members},
            actor_id=actor_id or resolved,
        )

    def channel_check(self, company_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
        context = build_channel_check_context(
            company_id,
            channel_id=str(data.get("channel_id") or DEFAULT_CHANNEL_ID),
            thread_id=data.get("thread_id"),
            limit=int(data.get("limit") if isinstance(data.get("limit"), int) else 20),
            company_store=self.company_store,
            runtime_store=self.runtime_store,
        )
        if context is None:
            return None
        policy = self._channel_turn_policy(company_id, data)
        return {**context, **policy}

    def list_agents(self, company_id: str) -> list[dict[str, Any]] | None:
        agents = self.company_store.list_agents(company_id)
        if agents is None:
            return None
        return enrich_short_ids(agents, prefix="ag", id_key="agent_id")

    def get_agent(self, company_id: str, agent_id: str) -> dict[str, Any] | None:
        agent = self.company_store.get_agent(company_id, agent_id)
        if agent is None:
            return None
        return enrich_short_ids([agent], prefix="ag", id_key="agent_id")[0]

    def upsert_agent(self, company_id: str, data: dict[str, Any], *, actor_id: str = "creator") -> dict[str, Any] | None:
        existing = self.list_agents(company_id) or []
        agent = normalize_team_agent(data, existing_short_ids=[str(item.get("short_id")) for item in existing])
        agent["metadata"] = lifecycle_update(agent.get("metadata"), state="active", actor_id=actor_id)
        return self.company_store.upsert_agent(company_id, agent)

    def patch_agent(self, company_id: str, agent_id: str, updates: dict[str, Any], *, actor_id: str = "creator") -> dict[str, Any] | None:
        resolved = self.resolve_agent_id(company_id, agent_id) or str(agent_id or "").strip().lstrip("@")
        current = self.company_store.get_agent(company_id, resolved)
        if current is None:
            return None
        patch = dict(updates or {})
        patch.pop("id", None)
        patch.pop("agent_id", None)
        merged = {**current, **patch, "agent_id": resolved, "id": resolved}
        if isinstance(current.get("metadata"), dict) or isinstance(patch.get("metadata"), dict):
            merged["metadata"] = {
                **(current.get("metadata") if isinstance(current.get("metadata"), dict) else {}),
                **(patch.get("metadata") if isinstance(patch.get("metadata"), dict) else {}),
            }
        if "aliases" not in patch:
            merged["aliases"] = list(current.get("aliases") or [])
        return self.upsert_agent(company_id, merged, actor_id=actor_id)

    def set_agent_status(self, company_id: str, agent_id: str, status: str, *, actor_id: str = "creator") -> dict[str, Any] | None:
        return self.patch_agent(company_id, agent_id, {"status": str(status or "idle")}, actor_id=actor_id)

    def archive_agent(self, company_id: str, agent_id: str, *, actor_id: str = "creator") -> dict[str, Any] | None:
        agent = self.company_store.get_agent(company_id, agent_id)
        if agent is None:
            return None
        agent["status"] = "archived"
        agent["metadata"] = lifecycle_update(agent.get("metadata"), state="archived", actor_id=actor_id)
        return self.company_store.upsert_agent(company_id, agent)

    def list_messages(self, company_id: str, data: dict[str, Any], *, context: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], int] | dict[str, Any] | None:
        if self.company_store.get_company(company_id) is None:
            return None
        channel_id = str(data.get("channel_id") or DEFAULT_CHANNEL_ID)
        auth = self.authorize_channel_read(company_id, channel_id, context=context)
        if is_denial(auth):
            return auth
        limit = int(data.get("limit") if isinstance(data.get("limit"), int) else 50)
        offset = int(data.get("offset") if isinstance(data.get("offset"), int) else 0)
        return self.runtime_store.list_messages(
            company_id,
            channel_id=channel_id,
            thread_id=data.get("thread_id"),
            limit=max(1, limit),
            offset=max(0, offset),
        )

    def send_message(self, company_id: str, data: dict[str, Any], *, context: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if self.company_store.get_company(company_id) is None:
            return None
        message = normalize_message_request(data)
        sender_id = self._effective_actor_id(company_id, data, context=context, fallback="user")
        message["sender_id"] = sender_id
        rich = evaluate_rich_payload({**data, "content": message["content"]})
        target_agent_ids = self._resolve_target_agent_ids(
            company_id,
            explicit=message["target_agent_ids"],
            content=message["content"],
            channel_id=message["channel_id"],
        )
        unresolved_targets = self._unresolved_target_ids(company_id, message["target_agent_ids"])
        if unresolved_targets:
            return _deny(
                "target agent is not known: " + ", ".join(unresolved_targets),
                "TARGET_NOT_FOUND",
                unresolved_target_agent_ids=unresolved_targets,
            )
        check = self._channel_turn_policy(
            company_id,
            {
                **data,
                "channel_id": message["channel_id"],
                "agent_id": message["sender_id"],
                "sender_id": message["sender_id"],
                "target_agent_ids": target_agent_ids,
                "rich_requested": rich["requested"],
                "action": str(data.get("action") or "message"),
            },
        )
        if not check["allowed"]:
            return _deny(str(check["deny_reason"]), str(check["deny_code"]), channel_check=check)
        recorded_check = self.channel_check(
            company_id,
            {
                **data,
                "channel_id": message["channel_id"],
                "agent_id": message["sender_id"],
                "sender_id": message["sender_id"],
                "target_agent_ids": target_agent_ids,
                "rich_requested": rich["requested"],
                "action": str(data.get("action") or "message"),
            },
        ) or check
        gate = pm_gate_decision(
            sender_id=message["sender_id"],
            content=message["content"],
            target_agent_ids=target_agent_ids,
            rich_requested=rich["requested"],
            action=str(data.get("action") or "message"),
            project_manager_id=str(recorded_check.get("pm_agent_id") or "project_manager"),
        )
        routed_content = gated_content(content=rich["content"], sender_id=message["sender_id"], gate=gate)
        metadata = {
            **message["metadata"],
            "subagent_team": {
                "parsed": message["parsed"],
                "rich": rich,
                "pm_gate": gate,
                "channel_check": recorded_check,
                "task_completion_condition": recorded_check.get("task_completion_condition"),
                "original_content": message["content"],
                "client_approved_ignored": "approved" in data,
            },
        }
        if rich["rich_payload"]:
            metadata["rich_payload"] = rich["rich_payload"]
        if rich["attachments"]:
            metadata["attachments"] = rich["attachments"]
        result = CompanySlackRuntime(
            company_store=self.company_store,
            runtime_store=self.runtime_store,
            settings_owner=self.settings_owner,
        ).post_message(
            company_id,
            content=routed_content,
            sender_id=message["sender_id"],
            channel_id=message["channel_id"],
            thread_id=message["thread_id"],
            target_agent_ids=gate["target_agent_ids"],
            metadata=metadata,
            context=context or {},
        )
        if isinstance(result, dict):
            result["channel_check"] = recorded_check
        return result

    def _resolve_target_agent_ids(self, company_id: str, *, explicit: list[str], content: str, channel_id: str | None = None) -> list[str]:
        resolved: list[str] = []
        mention_service = CompanyMentionService(self.company_store)
        if explicit:
            explicit_resolution = mention_service.resolve(company_id, explicit) or {}
            resolved.extend(str(item) for item in explicit_resolution.get("resolved_agent_ids") or [])
        parsed = parse_mentions(content)
        special_mentions = {str(item).strip().lower() for item in parsed.get("agent_mentions", [])}
        if special_mentions.intersection({"team", "channel"}):
            channel = self.company_store.get_channel(company_id, str(channel_id or DEFAULT_CHANNEL_ID))
            if channel is not None:
                resolved.extend(self._resolved_channel_members(company_id, channel))
        content_resolution = mention_service.resolve(company_id, content) or {}
        resolved.extend(str(item) for item in content_resolution.get("resolved_agent_ids") or [])
        return _dedupe(resolved)

    def parse_message(self, content: str) -> dict[str, Any]:
        return parse_mentions(content)

    def rich_preview(self, data: dict[str, Any]) -> dict[str, Any]:
        return evaluate_rich_payload(data)

    def creator_preview(self, company_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
        return CreatorService(
            company_store=self.company_store,
            runtime_store=self.runtime_store,
            settings_owner=self.settings_owner,
        ).preview(company_id, data)

    def creator_request(self, company_id: str, data: dict[str, Any], *, context: dict[str, Any] | None = None) -> dict[str, Any] | None:
        return CreatorService(
            company_store=self.company_store,
            runtime_store=self.runtime_store,
            settings_owner=self.settings_owner,
        ).request(
            company_id,
            data,
            context=context or {},
        )

    def list_goals(self, company_id: str, data: dict[str, Any]) -> tuple[list[dict[str, Any]], int] | None:
        if self.company_store.get_company(company_id) is None:
            return None
        limit = int(data.get("limit") if isinstance(data.get("limit"), int) else 50)
        offset = int(data.get("offset") if isinstance(data.get("offset"), int) else 0)
        tasks, total = self.runtime_store.list_tasks(company_id, limit=max(1, limit * 2), offset=max(0, offset))
        goals = [task for task in tasks if _is_goal(task)]
        return enrich_short_ids(goals[:limit], prefix="goal", id_key="task_id"), total

    def get_goal(self, company_id: str, goal_id: str) -> dict[str, Any] | None:
        task = self.runtime_store.get_task(goal_id, company_id=company_id)
        if task is None or not _is_goal(task):
            return None
        return enrich_short_ids([task], prefix="goal", id_key="task_id")[0]

    def create_goal(self, company_id: str, data: dict[str, Any], *, context: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if self.company_store.get_company(company_id) is None:
            return None
        goal = normalize_goal_request(data)
        sender_id = self._effective_actor_id(company_id, data, context=context, fallback="user")
        targets = self._resolve_target_agent_ids(
            company_id,
            explicit=goal["target_agent_ids"],
            content=goal["description"] or goal["title"],
            channel_id=goal["channel_id"] or DEFAULT_CHANNEL_ID,
        )
        unresolved_targets = self._unresolved_target_ids(company_id, goal["target_agent_ids"])
        if unresolved_targets:
            return _deny(
                "target agent is not known: " + ", ".join(unresolved_targets),
                "TARGET_NOT_FOUND",
                unresolved_target_agent_ids=unresolved_targets,
            )
        check = self._channel_turn_policy(
            company_id,
            {
                **data,
                "channel_id": goal["channel_id"] or DEFAULT_CHANNEL_ID,
                "agent_id": sender_id,
                "sender_id": sender_id,
                "target_agent_ids": targets,
                "action": "create_goal",
            },
        )
        if not check["allowed"]:
            return _deny(str(check["deny_reason"]), str(check["deny_code"]), channel_check=check)
        recorded_check = self.channel_check(
            company_id,
            {
                **data,
                "channel_id": goal["channel_id"] or DEFAULT_CHANNEL_ID,
                "agent_id": sender_id,
                "sender_id": sender_id,
                "target_agent_ids": targets,
                "action": "create_goal",
            },
        ) or check
        gate = pm_gate_decision(
            sender_id=sender_id,
            content=goal["description"] or goal["title"],
            target_agent_ids=targets,
            action="create_goal",
            project_manager_id=str(check.get("pm_agent_id") or "project_manager"),
        )
        channel_policy = check.get("channel_policy") if isinstance(check.get("channel_policy"), dict) else {}
        pm_agent_id = str(channel_policy.get("pm_agent_id") or "")
        actor_auth = self.authorize_pm_actor(company_id, sender_id, channel_id=goal["channel_id"] or DEFAULT_CHANNEL_ID)
        pm_goal_approval_required = bool(pm_agent_id and not actor_auth.get("allowed"))
        description = goal["description"]
        if pm_goal_approval_required:
            gate = {
                **gate,
                "requires_pm": True,
                "reason": "/goal requires channel PM approval",
                "requested_target_agent_ids": targets,
                "target_agent_ids": [pm_agent_id],
                "route": "pm_gate",
                "project_manager_id": pm_agent_id,
            }
            description = gated_content(content=description or goal["title"], sender_id=sender_id, gate=gate)
            targets = [pm_agent_id]
            goal["status"] = "waiting_approval"
        elif gate["requires_pm"]:
            description = gated_content(content=description or goal["title"], sender_id=sender_id, gate=gate)
            targets = gate["target_agent_ids"]
            goal["status"] = "waiting_approval"
        return self.runtime_store.create_task(
            company_id,
            title=goal["title"],
            description=description,
            target_agent_ids=targets or ["project_manager"],
            source="goal",
            status=goal["status"],
            priority=goal["priority"],
            channel_id=goal["channel_id"] or DEFAULT_CHANNEL_ID,
            thread_id=goal["thread_id"],
            metadata={
                **goal["metadata"],
                "pm_gate": gate,
                "channel_check": recorded_check,
                "task_completion_condition": recorded_check.get("task_completion_condition"),
            },
        )

    def update_goal(self, company_id: str, goal_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        if self.company_store.get_company(company_id) is None:
            return None
        clean_or_denied = sanitize_goal_patch_updates(updates)
        if is_denial(clean_or_denied):
            return clean_or_denied
        clean = clean_or_denied
        if not clean:
            return self.get_goal(company_id, goal_id)
        return self.runtime_store.update_task(goal_id, clean, company_id=company_id)

    def decide_goal(
        self,
        company_id: str,
        goal_id: str,
        action: str,
        data: dict[str, Any],
        *,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if self.company_store.get_company(company_id) is None:
            return None
        actor_id = trusted_actor_from_context(context)
        if not actor_id:
            return _deny("trusted context actor is required", "ACTOR_REQUIRED")
        task = self.runtime_store.get_task(goal_id, company_id=company_id)
        if task is None:
            return None
        channel_id = str(data.get("channel_id") or task.get("channel_id") or DEFAULT_CHANNEL_ID)
        auth = self.authorize_pm_actor(company_id, actor_id, channel_id=channel_id)
        if not auth["allowed"]:
            return _deny(
                "only channel PM, project_manager, or operations_manager can decide this task",
                "FORBIDDEN",
                auth=auth,
            )
        check = self._channel_turn_policy(
            company_id,
            {
                **data,
                "channel_id": channel_id,
                "agent_id": actor_id,
                "sender_id": actor_id,
                "target_agent_ids": list(task.get("target_agent_ids") or []),
                "action": str(action or "update_goal"),
            },
        )
        recorded_check = self.channel_check(
            company_id,
            {
                **data,
                "channel_id": channel_id,
                "agent_id": actor_id,
                "sender_id": actor_id,
                "target_agent_ids": list(task.get("target_agent_ids") or []),
                "action": str(action or "update_goal"),
            },
        ) or check
        channel_policy = check.get("channel_policy") if isinstance(check.get("channel_policy"), dict) else {}
        if str(action or "").lower() in {"task_complete", "complete", "close"} and channel_policy.get("pm_required") and not channel_policy.get("pm_agent_id"):
            return _deny("channels with 5 or more members require a PM task_complete owner", "PM_REQUIRED", channel_check=recorded_check)
        updates = data.get("updates") if isinstance(data.get("updates"), dict) else {}
        normalized_action = str(action or "").lower()
        status = updates.get("status")
        approval = None
        if normalized_action == "approve":
            status = "queued"
            approval = "approved"
        elif normalized_action == "reject":
            status = "cancelled"
            approval = "rejected"
        elif normalized_action in {"task_complete", "complete", "close"}:
            status = "completed"
            approval = "task_completed"
        receipt_id = "pmr_" + uuid.uuid4().hex
        receipt = {
            "approval_receipt_id": receipt_id,
            "action": normalized_action,
            "actor_id": auth["actor_id"],
            "actor_role": auth["actor_role"],
            "company_id": str(company_id),
            "channel_id": channel_id,
            "task_id": str(goal_id),
            "decision": approval or str(status or "updated"),
            "grants_user_approval": False,
            "channel_check": recorded_check,
            "created_at": timestamp(),
        }
        current_metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
        receipts = current_metadata.get("approval_receipts") if isinstance(current_metadata.get("approval_receipts"), list) else []
        update_metadata = updates.get("metadata") if isinstance(updates.get("metadata"), dict) else {}
        clean = {
            **updates,
            "status": status or task.get("status") or "queued",
            "metadata": {
                **update_metadata,
                "approval": approval or update_metadata.get("approval"),
                "approval_receipt_id": receipt_id,
                "approval_receipt": receipt,
                "approval_receipts": [*receipts, receipt],
                "channel_check": recorded_check,
                "task_completion_condition": recorded_check.get("task_completion_condition"),
            },
        }
        return self.runtime_store.update_task(goal_id, clean, company_id=company_id)

    def list_dms(self, company_id: str) -> list[dict[str, Any]] | None:
        channels = self.list_channels(company_id)
        if channels is None:
            return None
        return [channel for channel in channels if _is_dm(channel) and not _is_archived_channel(channel)]

    def _dm_channel_candidate(self, company_id: str, data: dict[str, Any]) -> dict[str, Any]:
        participants = _dedupe([str(data.get("sender_id") or "user"), *list(data.get("participants") or data.get("target_agent_ids") or [])])
        if len(participants) < 2 and data.get("agent_id"):
            participants.append(str(data["agent_id"]))
        channel_id = str(data.get("dm_id") or data.get("channel_id") or _dm_channel_id(company_id, participants))
        existing = self.company_store.get_channel(company_id, channel_id)
        if existing is not None:
            existing_members = self._resolved_channel_members(company_id, existing)
            if len(participants) < 2:
                participants = existing_members
            else:
                participants = _dedupe([*existing_members, *participants])
        return {
            "id": channel_id,
            "name": str(data.get("name") or "dm-" + "-".join(participants[:3])),
            "description": str(data.get("description") or "Direct message channel"),
            "visibility": "dm",
            "members": participants,
            "metadata": {"dm": True, "participants": participants},
        }

    def ensure_dm(self, company_id: str, data: dict[str, Any], *, actor_id: str = "creator") -> dict[str, Any] | None:
        candidate = self._dm_channel_candidate(company_id, data)
        existing = self.company_store.get_channel(company_id, str(candidate.get("id") or ""))
        if existing is not None and _is_archived_channel(existing):
            return _deny("direct message is archived: " + str(candidate.get("id") or ""), "DM_ARCHIVED")
        return self.upsert_channel(company_id, candidate, actor_id=actor_id)

    def send_dm(self, company_id: str, data: dict[str, Any], *, context: dict[str, Any] | None = None) -> dict[str, Any] | None:
        sender_id = self._effective_actor_id(company_id, data, context=context, fallback="user")
        data = {**data, "sender_id": sender_id, "actor_id": sender_id}
        explicit_targets = data.get("target_agent_ids") if isinstance(data.get("target_agent_ids"), list) else []
        if data.get("agent_id"):
            explicit_targets = [*explicit_targets, str(data.get("agent_id"))]
        unresolved_targets = self._unresolved_target_ids(company_id, explicit_targets)
        if unresolved_targets:
            return _deny(
                "target agent is not known: " + ", ".join(unresolved_targets),
                "TARGET_NOT_FOUND",
                unresolved_target_agent_ids=unresolved_targets,
            )
        dm_candidate = self._dm_channel_candidate(company_id, data)
        if not explicit_targets:
            sender = self.resolve_agent_id(company_id, str(data.get("sender_id") or data.get("actor_id") or "user"))
            explicit_targets = [member for member in self._resolved_channel_members(company_id, dm_candidate) if member != sender]
        targets = self._resolve_target_agent_ids(
            company_id,
            explicit=explicit_targets,
            content=str(data.get("content") or data.get("message") or ""),
            channel_id=str(dm_candidate["id"]),
        )
        check = self._channel_turn_policy_for_channel(
            company_id,
            {
                **data,
                "channel_id": dm_candidate["id"],
                "agent_id": sender_id,
                "sender_id": sender_id,
                "target_agent_ids": targets,
                "action": "dm_send",
            },
            dm_candidate,
        )
        if not check["allowed"]:
            return _deny(str(check["deny_reason"]), str(check["deny_code"]), channel_check=check)
        dm = self.ensure_dm(company_id, data, actor_id=str(data.get("sender_id") or data.get("actor_id") or "creator"))
        if is_denial(dm):
            return dm
        if dm is None:
            return None
        return self.send_message(
            company_id,
            {
                **data,
                "channel_id": dm["id"],
                "target_agent_ids": targets,
                "metadata": {
                    **(data.get("metadata") if isinstance(data.get("metadata"), dict) else {}),
                    "dm_id": dm["id"],
                },
            },
            context=context or {},
        )

    def _effective_actor_id(
        self,
        company_id: str,
        data: dict[str, Any],
        *,
        context: dict[str, Any] | None = None,
        fallback: str = "user",
    ) -> str:
        trusted = trusted_actor_from_context(context)
        if trusted:
            return self.resolve_agent_id(company_id, trusted) or str(trusted).strip().lstrip("@")
        supplied = str(data.get("sender_id") or data.get("actor_id") or fallback).strip().lstrip("@")
        supplied_resolved = self.resolve_agent_id(company_id, supplied)
        if supplied_resolved and self.authorize_pm_actor(
            company_id,
            supplied_resolved,
            channel_id=data.get("channel_id") or DEFAULT_CHANNEL_ID,
        ).get("allowed"):
            return fallback
        return supplied_resolved or supplied or fallback

    def resolve_agent_id(self, company_id: str, value: str | None) -> str:
        needle = str(value or "").strip().lstrip("@").lower()
        if not needle:
            return ""
        agents = self.company_store.list_agents(company_id) or []
        for agent in agents:
            agent_id = str(agent.get("agent_id") or agent.get("id") or "")
            metadata = agent.get("metadata") if isinstance(agent.get("metadata"), dict) else {}
            team = team_metadata(agent)
            candidates = {
                agent_id.lower(),
                str(agent.get("id") or "").lower(),
                str(agent.get("role_key") or "").lower(),
                str(agent.get("display_name") or "").lower(),
                str(metadata.get("short_id") or "").lower(),
                str(team.get("short_id") or "").lower(),
                str(team.get("legacy_alias") or "").lower(),
            }
            candidates.update(str(alias).strip().lstrip("@").lower() for alias in agent.get("aliases", []) if str(alias).strip())
            if needle in candidates:
                return agent_id
        return ""

    def authorize_pm_actor(self, company_id: str, actor_id: str, *, channel_id: str | None = None) -> dict[str, Any]:
        actor = self.resolve_agent_id(company_id, actor_id) or str(actor_id or "").strip().lstrip("@").lower()
        agent = self.company_store.get_agent(company_id, actor)
        role = self._agent_role(agent) if agent else actor
        channel_pm_agent_id = None
        if channel_id:
            channel = self.company_store.get_channel(company_id, str(channel_id))
            if channel is not None:
                channel_pm_agent_id = self._channel_pm_agent_id(company_id, channel)
        allowed = actor in PM_AGENT_IDS or role in {"project_manager", "operations_manager"}
        if channel_pm_agent_id and actor == channel_pm_agent_id:
            allowed = True
        reason = "allowed" if allowed else "actor is not channel PM, project_manager, or operations_manager"
        return {
            "allowed": allowed,
            "actor_id": actor,
            "actor_role": role,
            "channel_id": channel_id,
            "channel_pm_agent_id": channel_pm_agent_id,
            "reason": reason,
        }

    def authorize_channel_read(self, company_id: str, channel_id: str, *, context: dict[str, Any] | None = None) -> dict[str, Any]:
        actor_id = trusted_actor_from_context(context)
        if not actor_id:
            return _deny("trusted context actor is required to read channel history", "ACTOR_REQUIRED")
        channel_id = str(channel_id or DEFAULT_CHANNEL_ID).strip() or DEFAULT_CHANNEL_ID
        channel = self.company_store.get_channel(company_id, channel_id)
        if channel is None:
            return _deny("channel not found: " + channel_id, "CHANNEL_NOT_FOUND")
        if _is_archived_channel(channel):
            code = "DM_ARCHIVED" if _is_dm(channel) else "CHANNEL_ARCHIVED"
            return _deny("channel is archived: " + channel_id, code, channel_id=channel_id)
        auth = self.authorize_pm_actor(company_id, actor_id, channel_id=channel_id)
        if auth["allowed"]:
            return {**auth, "allowed": True, "read_scope": "pm"}
        actor = self.resolve_agent_id(company_id, actor_id) or str(actor_id or "").strip().lstrip("@")
        members = self._resolved_channel_members(company_id, channel)
        if actor in members:
            return {**auth, "allowed": True, "read_scope": "member", "actor_id": actor}
        visibility = str(channel.get("visibility") or "")
        code = "DM_PARTICIPANT_REQUIRED" if _is_dm(channel) or visibility == "dm" else "CHANNEL_MEMBERSHIP_REQUIRED"
        return _deny(
            "actor is not a participant of channel history: " + actor,
            code,
            auth=auth,
            channel_id=channel_id,
        )

    def _unresolved_target_ids(self, company_id: str, target_agent_ids: list[str]) -> list[str]:
        if not target_agent_ids:
            return []
        mention_service = CompanyMentionService(self.company_store)
        resolution = mention_service.resolve(company_id, target_agent_ids) or {}
        return [str(item) for item in resolution.get("unresolved") or [] if str(item).strip()]

    def _channel_turn_policy(self, company_id: str, data: dict[str, Any]) -> dict[str, Any]:
        channel_id = str(data.get("channel_id") or DEFAULT_CHANNEL_ID)
        channel = self.company_store.get_channel(company_id, channel_id)
        if channel is None:
            return _channel_decision(
                allowed=False,
                deny_code="CHANNEL_NOT_FOUND",
                deny_reason="channel not found: " + channel_id,
                channel_id=channel_id,
            )
        if _is_archived_channel(channel):
            return _channel_decision(
                allowed=False,
                deny_code="DM_ARCHIVED" if _is_dm(channel) else "CHANNEL_ARCHIVED",
                deny_reason="channel is archived: " + channel_id,
                channel_id=channel_id,
            )
        return self._channel_turn_policy_for_channel(company_id, data, channel)

    def _channel_turn_policy_for_channel(self, company_id: str, data: dict[str, Any], channel: dict[str, Any]) -> dict[str, Any]:
        channel_id = str(data.get("channel_id") or channel.get("id") or DEFAULT_CHANNEL_ID)
        if _is_archived_channel(channel):
            return _channel_decision(
                allowed=False,
                deny_code="DM_ARCHIVED" if _is_dm(channel) else "CHANNEL_ARCHIVED",
                deny_reason="channel is archived: " + channel_id,
                channel_id=channel_id,
            )
        members = self._resolved_channel_members(company_id, channel)
        channel_policy = self._channel_pm_policy(company_id, channel)
        actor_raw = str(data.get("agent_id") or data.get("sender_id") or data.get("actor_id") or "").strip()
        actor_id = self.resolve_agent_id(company_id, actor_raw)
        agent_is_member = False
        if actor_id:
            agent_is_member = actor_id in members
            if not agent_is_member:
                return _channel_decision(
                    allowed=False,
                    deny_code="CHANNEL_MEMBERSHIP_REQUIRED",
                    deny_reason="agent is not a member of channel: " + actor_id,
                    channel_id=channel_id,
                    agent_id=actor_id,
                    agent_is_member=False,
                    channel_policy=channel_policy,
                )
        target_agent_ids = self._resolve_target_agent_ids(
            company_id,
            explicit=list(data.get("target_agent_ids") or []),
            content=str(data.get("content") or data.get("message") or data.get("text") or ""),
            channel_id=channel_id,
        )
        missing_members = [agent_id for agent_id in target_agent_ids if agent_id not in members]
        if missing_members:
            return _channel_decision(
                allowed=False,
                deny_code="TARGET_NOT_CHANNEL_MEMBER",
                deny_reason="target agent is not a member of channel: " + ", ".join(missing_members),
                channel_id=channel_id,
                agent_id=actor_id,
                agent_is_member=agent_is_member,
                target_agent_ids=target_agent_ids,
                missing_target_agent_ids=missing_members,
                channel_policy=channel_policy,
            )
        if not channel_policy["allowed"]:
            return _channel_decision(
                allowed=False,
                deny_code=str(channel_policy["code"]),
                deny_reason=str(channel_policy["reason"]),
                channel_id=channel_id,
                agent_id=actor_id,
                agent_is_member=agent_is_member,
                target_agent_ids=target_agent_ids,
                channel_policy=channel_policy,
            )
        rich_status = self.rich_status(company_id, requested_new_agents=_safe_int(data.get("requested_new_agents") or data.get("team_size") or 0)) or {}
        if not rich_status.get("allowed", True):
            return _channel_decision(
                allowed=False,
                deny_code="RICH_MODE_REQUIRED",
                deny_reason=str(rich_status.get("reason") or "rich mode required"),
                channel_id=channel_id,
                agent_id=actor_id,
                agent_is_member=agent_is_member,
                target_agent_ids=target_agent_ids,
                channel_policy=channel_policy,
                rich_status=rich_status,
            )
        gate = pm_gate_decision(
            sender_id=actor_id or actor_raw or "user",
            content=str(data.get("content") or data.get("message") or data.get("text") or ""),
            target_agent_ids=target_agent_ids,
            rich_requested=bool(data.get("rich_requested") or data.get("rich")),
            action=str(data.get("action") or "message"),
            project_manager_id=str(channel_policy.get("pm_agent_id") or "project_manager"),
        )
        if gate.get("requires_pm") and str(gate.get("project_manager_id") or "project_manager") not in members:
            return _channel_decision(
                allowed=False,
                deny_code="PM_REQUIRED",
                deny_reason="PM gate required but project_manager is not a channel member",
                channel_id=channel_id,
                agent_id=actor_id,
                agent_is_member=agent_is_member,
                target_agent_ids=target_agent_ids,
                channel_policy=channel_policy,
                rich_status=rich_status,
                pm_gate=gate,
            )
        return _channel_decision(
            allowed=True,
            deny_code=None,
            deny_reason=None,
            channel_id=channel_id,
            agent_id=actor_id,
            agent_is_member=agent_is_member,
            target_agent_ids=target_agent_ids,
            channel_policy=channel_policy,
            rich_status=rich_status,
            pm_gate=gate,
        )

    def _channel_pm_policy(self, company_id: str, channel: dict[str, Any]) -> dict[str, Any]:
        members = self._resolved_channel_members(company_id, channel)
        team = team_metadata(channel)
        pm_required = bool(team.get("pm_required", len(members) >= PM_THRESHOLD) or len(members) >= PM_THRESHOLD)
        pm_agent_id = self._channel_pm_agent_id(company_id, channel)
        allowed = not pm_required or bool(pm_agent_id)
        return {
            "allowed": allowed,
            "code": None if allowed else "PM_REQUIRED",
            "reason": "channel PM present" if allowed else "channels with 5 or more members require a PM",
            "pm_required": pm_required,
            "pm_agent_id": pm_agent_id,
            "member_count": len(members),
        }

    def _channel_pm_agent_id(self, company_id: str, channel: dict[str, Any]) -> str | None:
        members = self._resolved_channel_members(company_id, channel)
        team = team_metadata(channel)
        configured = self.resolve_agent_id(company_id, str(team.get("pm_agent_id") or ""))
        if configured and configured in members and self._agent_is_channel_pm(company_id, configured):
            return configured
        for agent_id in members:
            if self._agent_is_channel_pm(company_id, agent_id):
                return agent_id
        return None

    def _resolved_channel_members(self, company_id: str, channel: dict[str, Any]) -> list[str]:
        members = []
        for member in channel.get("members") or []:
            resolved = self.resolve_agent_id(company_id, str(member)) or str(member).strip().lstrip("@")
            if resolved and resolved not in members:
                members.append(resolved)
        return members

    def _agent_is_channel_pm(self, company_id: str, agent_id: str) -> bool:
        if str(agent_id) in PM_AGENT_IDS:
            return True
        agent = self.company_store.get_agent(company_id, str(agent_id))
        return self._agent_role(agent) == "pm" if agent else False

    @staticmethod
    def _agent_role(agent: dict[str, Any] | None) -> str:
        if not isinstance(agent, dict):
            return ""
        team = team_metadata(agent)
        return str(team.get("role") or agent.get("role_key") or "").strip().lower()


def _deny(message: str, code: str, **extra: Any) -> dict[str, Any]:
    return {
        "denied": True,
        "allowed": False,
        "code": str(code or "FORBIDDEN"),
        "message": str(message or "denied"),
        **extra,
    }


GOAL_PATCH_ALLOWED_KEYS = {"title", "description", "priority", "thread_id", "metadata"}
GOAL_PATCH_BLOCKED_KEYS = {
    "status",
    "target_agent_ids",
    "source",
    "channel_id",
    "created_at",
    "updated_at",
    "completed_at",
    "closed_at",
    "approved_at",
    "approval",
    "approved",
    "approval_token",
    "_tool_server_approved",
    "approval_receipt",
    "approval_receipt_id",
    "approval_receipts",
    "channel_check",
    "task_completion_condition",
    "pm_gate",
    "actor_id",
    "sender_id",
}
GOAL_PATCH_BLOCKED_METADATA_KEYS = {
    "approval",
    "approval_receipt",
    "approval_receipt_id",
    "approval_receipts",
    "channel_check",
    "task_completion_condition",
    "pm_gate",
    "lifecycle",
    "status",
    "state",
}


def sanitize_goal_patch_updates(updates: dict[str, Any] | None) -> dict[str, Any]:
    raw = dict(updates or {})
    blocked = sorted(
        key
        for key in raw
        if key in GOAL_PATCH_BLOCKED_KEYS
        or key.startswith("approval_")
        or key.startswith("metadata.approval")
        or key.startswith("lifecycle_")
    )
    if blocked:
        return _deny(
            "goal lifecycle, approval, and target changes must use the PM decision route",
            "GOAL_DECISION_REQUIRED",
            blocked_fields=blocked,
        )
    clean: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in GOAL_PATCH_ALLOWED_KEYS:
            continue
        if key == "metadata":
            if not isinstance(value, dict):
                continue
            metadata_blocked = sorted(
                meta_key
                for meta_key in value
                if meta_key in GOAL_PATCH_BLOCKED_METADATA_KEYS
                or str(meta_key).startswith("approval")
                or str(meta_key).startswith("lifecycle")
            )
            if metadata_blocked:
                return _deny(
                    "goal approval metadata must use the PM decision route",
                    "GOAL_DECISION_REQUIRED",
                    blocked_fields=["metadata." + key for key in metadata_blocked],
                )
            clean["metadata"] = dict(value)
            continue
        clean[key] = value
    return clean


def is_denial(value: Any) -> bool:
    return isinstance(value, dict) and bool(value.get("denied")) and value.get("allowed") is False


def trusted_actor_from_context(context: dict[str, Any] | None) -> str:
    if not isinstance(context, dict):
        return ""
    for key in (
        "trusted_actor_id",
        "server_actor_id",
        "actor_id",
        "authenticated_actor_id",
        "current_actor_id",
        "user_id",
        "authenticated_user_id",
        "current_user_id",
    ):
        actor = _clean_actor_id(context.get(key))
        if actor:
            return actor
    for key in ("actor", "user", "auth", "session", "request_context"):
        nested = context.get(key)
        if not isinstance(nested, dict):
            continue
        for nested_key in (
            "trusted_actor_id",
            "server_actor_id",
            "actor_id",
            "id",
            "user_id",
            "authenticated_user_id",
            "current_user_id",
        ):
            actor = _clean_actor_id(nested.get(nested_key))
            if actor:
                return actor
    return ""


def _clean_actor_id(value: Any) -> str:
    actor = str(value or "").strip().lstrip("@")
    if not actor:
        return ""
    return actor


def _channel_decision(
    *,
    allowed: bool,
    deny_code: str | None,
    deny_reason: str | None,
    channel_id: str,
    agent_id: str | None = None,
    agent_is_member: bool | None = None,
    target_agent_ids: list[str] | None = None,
    missing_target_agent_ids: list[str] | None = None,
    channel_policy: dict[str, Any] | None = None,
    rich_status: dict[str, Any] | None = None,
    pm_gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    channel_policy = channel_policy if isinstance(channel_policy, dict) else {}
    rich_status = rich_status if isinstance(rich_status, dict) else {}
    pm_gate = pm_gate if isinstance(pm_gate, dict) else {}
    targets = list(target_agent_ids or [])
    missing_targets = list(missing_target_agent_ids or [])
    pm_required = bool(channel_policy.get("pm_required") or pm_gate.get("requires_pm"))
    pm_agent_id = channel_policy.get("pm_agent_id")
    return {
        "allowed": bool(allowed),
        "deny_code": deny_code,
        "deny_reason": deny_reason,
        "channel_id": channel_id,
        "agent_id": agent_id,
        "agent_is_member": bool(agent_is_member) if agent_is_member is not None else False,
        "membership": {
            "agent_id": agent_id,
            "is_member": bool(agent_is_member) if agent_is_member is not None else False,
            "required": bool(agent_id),
        },
        "target_agent_ids": targets,
        "target_membership": {
            "target_agent_ids": targets,
            "missing_target_agent_ids": missing_targets,
            "all_targets_are_members": not missing_targets,
        },
        "pm_required": pm_required,
        "pm_agent_id": pm_agent_id,
        "rich_allowed": bool(rich_status.get("allowed", True)),
        "rich_policy": rich_status,
        "channel_policy": channel_policy,
        "pm_gate": pm_gate,
        "task_completion_condition": {
            "requires_pm_task_complete": bool(pm_required or pm_agent_id),
            "pm_agent_id": pm_agent_id,
            "worker_completion_is_final": not bool(pm_required or pm_agent_id),
            "pm_receipt_grants_user_approval": False,
        },
        "context_contract": {
            "channel_check_required": True,
            "must_precede": ["delegate", "message", "goal", "dm"],
            "direct_tool_execution": False,
            "client_approved_flags_trusted": False,
        },
    }


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _is_goal(task: dict[str, Any]) -> bool:
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    return str(task.get("source") or "") == "goal" or bool(metadata.get("subagent_team_goal"))


def _is_dm(channel: dict[str, Any]) -> bool:
    metadata = channel.get("metadata") if isinstance(channel.get("metadata"), dict) else {}
    return str(channel.get("visibility") or "") == "dm" or bool(metadata.get("dm"))


def _is_archived_channel(channel: dict[str, Any]) -> bool:
    metadata = channel.get("metadata") if isinstance(channel.get("metadata"), dict) else {}
    lifecycle = metadata.get("lifecycle") if isinstance(metadata.get("lifecycle"), dict) else {}
    return str(channel.get("visibility") or "").lower() == "archived" or str(lifecycle.get("state") or "").lower() == "archived"


def _dm_channel_id(company_id: str, participants: list[str]) -> str:
    seed = str(company_id) + ":" + ",".join(sorted(participants))
    return "dm_" + stable_short_id("dm", seed).split("_", 1)[-1]


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = slug_id(str(value or "").strip().lstrip("@"), fallback="")
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result
