from __future__ import annotations

from typing import Any

from tobkiri_protocol.settings_state import SettingsOwnerPort

from domain.company.models import DEFAULT_CHANNEL_ID
from domain.company.runtime_store import CompanyRuntimeStore
from domain.company.store import CompanyStore

from .ids import channel_id_from_name
from .models import PM_THRESHOLD, make_agent_spec, make_channel_spec
from .normalizers import normalize_message_request
from .pm_gate import gated_content, pm_gate_decision
from .rich_policy import evaluate_rich_payload, evaluate_rich_policy


class CreatorService:
    """Decision previews and requests for creator-managed team lifecycle."""

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

    def _team_service(self):
        from .service import SubagentTeamService

        return SubagentTeamService(
            company_store=self.company_store,
            runtime_store=self.runtime_store,
            settings_owner=self.settings_owner,
        )

    def preview(self, company_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
        if self.company_store.get_company(company_id) is None:
            return None
        action = _normalize_action(str(data.get("action") or data.get("tool_id") or "message"))
        message = normalize_message_request(data)
        rich = evaluate_rich_payload({**data, "content": message["content"]})
        team_plan = self._team_plan(company_id, data, action=action)
        rich_policy = evaluate_rich_policy(
            company_id,
            requested_new_agents=int(team_plan.get("team_size") or 0),
            settings=self.company_store.get_settings(company_id) or {},
            runtime_store=self.runtime_store,
        )
        gate = pm_gate_decision(
            sender_id=message["sender_id"],
            content=message["content"],
            target_agent_ids=message["target_agent_ids"] or message["parsed"]["agent_mentions"],
            rich_requested=rich["requested"],
            action=action,
        )
        route_content = gated_content(content=rich["content"], sender_id=message["sender_id"], gate=gate)
        channel_check = _preview_channel_check(
            company_store=self.company_store,
            runtime_store=self.runtime_store,
            company_id=company_id,
            data={
                **data,
                "channel_id": message["channel_id"],
                "sender_id": message["sender_id"],
                "agent_id": message["sender_id"],
                "target_agent_ids": gate["target_agent_ids"],
                "content": message["content"],
                "rich_requested": rich["requested"],
                "requested_new_agents": int(team_plan.get("team_size") or 0),
                "action": action,
            },
        )
        return {
            "action": action,
            "provider_safe_action": _provider_safe_action(action),
            "company_id": str(company_id),
            "will_execute_tools": False,
            "routing": {
                "runtime": "CompanySlackRuntime",
                "route": "agent.delegate",
                "direct_tool_execution": False,
                "target_agent_ids": gate["target_agent_ids"],
            },
            "pm_gate": gate,
            "channel_check": channel_check,
            "rich": rich,
            "rich_policy": rich_policy,
            "team_plan": team_plan,
            "message": {
                "channel_id": message["channel_id"],
                "thread_id": message["thread_id"],
                "sender_id": message["sender_id"],
                "content": route_content,
                "original_content": message["content"],
            },
            "lifecycle": {
                "managed_by": "creator",
                "store": "CompanyStore/CompanyRuntimeStore",
                "approval_bypass": False,
            },
        }

    def request(self, company_id: str, data: dict[str, Any], *, context: dict[str, Any] | None = None) -> dict[str, Any] | None:
        preview = self.preview(company_id, data)
        if preview is None:
            return None
        action = str(preview["action"])
        if action == "status":
            return {
                "preview": preview,
                "result": self._team_service().status(company_id),
            }
        if action in {"create_agent", "create_agents", "agent", "agents", "create_team", "team"}:
            return self._request_team_create(company_id, data, preview=preview)
        if action in {"message", "request", "delegate", "route"}:
            return {
                "preview": preview,
                "result": self._team_service().send_message(company_id, data, context=context or {}),
            }
        if action == "dm_send":
            return {
                "preview": preview,
                "result": self._team_service().send_dm(company_id, data, context=context or {}),
            }
        if action == "channel_join":
            from .service import is_denial

            service = self._team_service()
            channel_id = str(data.get("channel_id") or data.get("id") or "")
            agent_id = str(data.get("agent_id") or data.get("member_id") or data.get("short_id") or "")
            result = service.join_channel(company_id, channel_id, agent_id, actor_id="subagent_creator")
            if is_denial(result):
                return {"preview": preview, **result}
            return {"preview": preview, "result": result}
        if action in {"create_goal", "goal"}:
            from .service import is_denial

            task = self._team_service().create_goal(
                company_id,
                {**data, "metadata": {**(data.get("metadata") if isinstance(data.get("metadata"), dict) else {}), "creator_preview": preview}},
                context=context or {},
            )
            if is_denial(task):
                return {"preview": preview, **task}
            return {"preview": preview, "goal": task}
        if action in {"goal_approve", "task_complete"}:
            from .service import is_denial

            goal_id = str(data.get("goal_id") or data.get("task_id") or data.get("id") or "")
            decision = "approve" if action == "goal_approve" else "task_complete"
            task = self._team_service().decide_goal(company_id, goal_id, decision, data, context=context or {})
            if is_denial(task):
                return {"preview": preview, **task}
            return {"preview": preview, "goal": task}
        if action == "channel_check":
            return {
                "preview": preview,
                "result": self._team_service().channel_check(company_id, data),
            }
        return {"preview": preview, "result": {"status": "preview_only", "action": action}}

    def _request_team_create(self, company_id: str, data: dict[str, Any], *, preview: dict[str, Any]) -> dict[str, Any]:
        from .service import is_denial

        plan = preview["team_plan"]
        rich_policy = preview["rich_policy"]
        if not rich_policy.get("allowed", True):
            return {
                "preview": preview,
                "denied": True,
                "allowed": False,
                "code": "RICH_MODE_REQUIRED",
                "message": str(rich_policy.get("reason") or "rich mode required"),
                "rich_policy": rich_policy,
            }
        service = self._team_service()
        created_agents: list[dict[str, Any]] = []
        existing_short_ids = [
            str((agent.get("metadata") or {}).get("short_id") or agent.get("short_id") or "")
            for agent in self.company_store.list_agents(company_id) or []
            if isinstance(agent, dict)
        ]
        channel_id = str(plan.get("channel_id") or DEFAULT_CHANNEL_ID)
        for index, item in enumerate(plan.get("agents") or [], start=1):
            spec = make_agent_spec(
                display_name=str(item.get("display_name") or item.get("role") or f"agent_{index}"),
                role=str(item.get("role") or "coder"),
                model=str(item.get("model") or data.get("model") or "default"),
                channels=[channel_id],
                existing_short_ids=existing_short_ids,
                system_prompt_profile=str(item.get("system_prompt_profile") or ""),
                agent_id=item.get("agent_id") if isinstance(item.get("agent_id"), str) else None,
            )
            updated = service.upsert_agent(company_id, spec, actor_id="subagent_creator")
            if is_denial(updated):
                return {"preview": preview, **updated}
            if updated is not None:
                created_agents.append(updated)
                metadata = updated.get("metadata") if isinstance(updated.get("metadata"), dict) else {}
                existing_short_ids.append(str(metadata.get("short_id") or updated.get("short_id") or ""))
        channel = None
        if bool(plan.get("create_channel")):
            member_ids = [str(agent.get("agent_id") or agent.get("id")) for agent in created_agents if agent.get("agent_id") or agent.get("id")]
            pm_agent_id = _first_pm_agent_id(created_agents)
            if len(member_ids) >= PM_THRESHOLD and not pm_agent_id:
                pm_agent_id = service.resolve_agent_id(company_id, "project_manager")
                if pm_agent_id:
                    member_ids.insert(0, pm_agent_id)
            channel = service.upsert_channel(
                company_id,
                make_channel_spec(
                    name=str(plan.get("channel_name") or channel_id),
                    kind=str(data.get("channel_kind") or "private"),
                    members=member_ids,
                    pm_required=len(member_ids) >= PM_THRESHOLD,
                    pm_agent_id=pm_agent_id,
                    rich_required=not bool(rich_policy.get("enabled")),
                ),
                actor_id="subagent_creator",
            )
            if is_denial(channel):
                return {"preview": preview, **channel}
        return {
            "preview": preview,
            "allowed": True,
            "status": "created",
            "team_size": len(created_agents),
            "agents": created_agents,
            "channel": channel,
            "rich_policy": rich_policy,
        }

    def _team_plan(self, company_id: str, data: dict[str, Any], *, action: str) -> dict[str, Any]:
        del company_id
        action = str(action or "").lower()
        is_create = action in {"create_agent", "create_agents", "agent", "agents", "create_team", "team"}
        agent_payloads = data.get("agents") if isinstance(data.get("agents"), list) else []
        single_agent = data.get("agent") if isinstance(data.get("agent"), dict) else None
        explicit_roles = [str(role).strip().lower() for role in data.get("roles", []) if str(role).strip()] if isinstance(data.get("roles"), list) else []
        requested_size = _requested_team_size(data, fallback=(len(agent_payloads) or (1 if single_agent or is_create else 0)))
        if explicit_roles:
            roles = explicit_roles[:requested_size or len(explicit_roles)]
            requested_size = len(roles)
        elif agent_payloads:
            roles = [str(item.get("role") or item.get("role_key") or "coder").strip().lower() for item in agent_payloads if isinstance(item, dict)]
            requested_size = len(roles)
        elif single_agent:
            roles = [str(single_agent.get("role") or single_agent.get("role_key") or data.get("role") or "coder").strip().lower()]
            requested_size = 1
        else:
            roles = _default_roles(requested_size)
        requested_size = len(roles)
        channel_name = str(data.get("channel_name") or data.get("channel_id") or ("team-" + "-".join(roles[:2]) if requested_size > 1 else DEFAULT_CHANNEL_ID))
        channel_id = str(data.get("channel_id") or channel_id_from_name(channel_name))
        agents: list[dict[str, Any]] = []
        for index, role in enumerate(roles, start=1):
            payload = agent_payloads[index - 1] if index - 1 < len(agent_payloads) and isinstance(agent_payloads[index - 1], dict) else {}
            if single_agent and index == 1:
                payload = single_agent
            display_name = str(
                payload.get("display_name")
                or payload.get("agent_name")
                or payload.get("name")
                or data.get("display_name")
                or f"{role}_{index}"
            )
            agents.append(
                {
                    "role": role,
                    "display_name": display_name,
                    "model": payload.get("model") or data.get("model") or "default",
                    "agent_id": payload.get("agent_id") or payload.get("id"),
                    "system_prompt_profile": payload.get("system_prompt_profile") or data.get("system_prompt_profile"),
                }
            )
        return {
            "action": action,
            "team_size": requested_size,
            "roles": roles,
            "agents": agents,
            "channel_id": channel_id,
            "channel_name": channel_name,
            "create_channel": bool(data.get("create_channel", requested_size > 1)),
            "pm_required": requested_size >= PM_THRESHOLD,
            "will_execute_tools": False,
        }


def _requested_team_size(data: dict[str, Any], *, fallback: int = 0) -> int:
    for key in ("team_size", "agent_count", "count", "requested_new_agents"):
        try:
            value = int(data.get(key))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return min(value, 24)
    return min(max(0, int(fallback or 0)), 24)


def _default_roles(size: int) -> list[str]:
    if size <= 0:
        return []
    if size == 1:
        return ["coder"]
    sequence = ["pm", "planner", "architect", "coder", "checker", "qa", "reviewer", "researcher", "documenter"]
    roles = sequence[: min(size, len(sequence))]
    while len(roles) < size:
        roles.append("coder")
    if size >= PM_THRESHOLD and "pm" not in roles:
        roles[0] = "pm"
    if size >= 2 and "checker" not in roles:
        roles[-1] = "checker"
    return roles


def _first_pm_agent_id(agents: list[dict[str, Any]]) -> str | None:
    for agent in agents:
        role = str(agent.get("role_key") or "").strip().lower()
        metadata = agent.get("metadata") if isinstance(agent.get("metadata"), dict) else {}
        nested = metadata.get("subagent_team") if isinstance(metadata.get("subagent_team"), dict) else {}
        if role == "pm" or str(nested.get("role") or "").strip().lower() == "pm":
            return str(agent.get("agent_id") or agent.get("id") or "")
    return None


_ACTION_ALIASES = {
    "subagent_request": "request",
    "subagent.request": "request",
    "subagent_status": "status",
    "subagent.status": "status",
    "subagent_create": "create_agent",
    "subagent.create": "create_agent",
    "subagent_dm_send": "dm_send",
    "subagent.dm.send": "dm_send",
    "subagent_channel_join": "channel_join",
    "subagent.channel.join": "channel_join",
    "subagent_goal_propose": "create_goal",
    "subagent.goal.propose": "create_goal",
    "subagent_goal_approve": "goal_approve",
    "subagent.goal.approve": "goal_approve",
    "subagent_task_complete": "task_complete",
    "subagent.task.complete": "task_complete",
    "channel.check": "channel_check",
}


def _normalize_action(action: str) -> str:
    key = str(action or "").strip().lower()
    return _ACTION_ALIASES.get(key, key or "message")


def _provider_safe_action(action: str) -> str:
    return str(action or "").strip().lower().replace(".", "_").replace("-", "_")


def _preview_channel_check(
    *,
    company_store: CompanyStore,
    runtime_store: CompanyRuntimeStore,
    company_id: str,
    data: dict[str, Any],
) -> dict[str, Any] | None:
    try:
        from .service import SubagentTeamService

        return SubagentTeamService(
            company_store=company_store,
            runtime_store=runtime_store,
        ).channel_check(company_id, data)
    except Exception:
        return None
