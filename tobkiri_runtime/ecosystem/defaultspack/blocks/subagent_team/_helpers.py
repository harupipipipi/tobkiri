from __future__ import annotations

from typing import Any

from blocks._common import error
from domain.company.models import DEFAULT_COMPANY_ID


def require_dict(input_data: Any) -> dict[str, Any] | None:
    return input_data if isinstance(input_data, dict) else None


def company_id_from(input_data: dict[str, Any], default: str | None = DEFAULT_COMPANY_ID) -> str | None:
    value = input_data.get("company_id") or input_data.get("id") or default
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def invalid(message: str):
    return error(message, "INVALID_INPUT")


def missing_team(company_id: str):
    return error("subagent team not found: " + str(company_id), "NOT_FOUND")


def is_denied(result: Any) -> bool:
    return isinstance(result, dict) and bool(result.get("denied")) and result.get("allowed") is False


def denied(result: dict[str, Any]):
    return error(str(result.get("message") or "denied"), str(result.get("code") or "FORBIDDEN"))


def normalize_action(value: Any, default: str = "list") -> str:
    action = str(value or default).strip().lower()
    aliases = {
        "subagent_request": "request",
        "subagent.request": "request",
        "subagent_status": "status",
        "subagent.status": "status",
        "subagent_create": "create",
        "subagent.create": "create",
        "subagent_dm_send": "send",
        "subagent.dm.send": "send",
        "subagent_channel_join": "join",
        "subagent.channel.join": "join",
        "subagent_goal_propose": "propose",
        "subagent.goal.propose": "propose",
        "subagent_goal_approve": "approve",
        "subagent.goal.approve": "approve",
        "subagent_task_complete": "task_complete",
        "subagent.task.complete": "task_complete",
        "channel.check": "check",
    }
    return aliases.get(action, action)


def lifecycle_actor(input_data: dict[str, Any], context: dict[str, Any] | None = None) -> str:
    del context
    supplied = str(input_data.get("actor_id") or input_data.get("sender_id") or "").strip().lower()
    if supplied in {"main", "main_agent", "user", "human", "client_manager", "president"}:
        return "subagent_creator"
    return "subagent_creator"


def direct_lifecycle_denied(
    input_data: dict[str, Any],
    context: dict[str, Any] | None = None,
    *,
    settings_owner: Any = None,
):
    context = context if isinstance(context, dict) else {}
    actor = str(
        context.get("trusted_actor_id")
        or context.get("server_actor_id")
        or context.get("actor_id")
        or context.get("current_actor_id")
        or ""
    ).strip().lower()
    if not actor:
        return error(
            "trusted server context is required for direct subagent lifecycle mutation; use Subagent Creator",
            "ACTOR_REQUIRED",
        )
    if actor in {"main", "main_agent", "user", "human", "client_manager", "president"}:
        return error(
            "main/user context cannot directly manage subagent lifecycle; use Subagent Creator",
            "CREATOR_REQUIRED",
        )
    if actor == "subagent_creator":
        return None
    company_id = company_id_from(input_data, default=None)
    if not company_id:
        return error("company_id is required", "INVALID_INPUT")
    from domain.subagent_team.service import SubagentTeamService

    auth = SubagentTeamService(settings_owner=settings_owner).authorize_pm_actor(
        company_id,
        actor,
        channel_id=input_data.get("channel_id") or input_data.get("id"),
    )
    if not auth.get("allowed"):
        return error(
            "only channel PM, project_manager, operations_manager, or Subagent Creator can directly manage subagent lifecycle",
            "FORBIDDEN",
        )
    return None


def limit_offset(input_data: dict[str, Any]) -> tuple[int, int]:
    limit = input_data.get("limit", 50)
    offset = input_data.get("offset", 0)
    if not isinstance(limit, int) or limit < 1:
        limit = 50
    if not isinstance(offset, int) or offset < 0:
        offset = 0
    return limit, offset
