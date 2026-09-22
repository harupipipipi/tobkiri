from blocks._common import ok, error
from domain.subagent_team.service import SubagentTeamService

from ._helpers import company_id_from, denied, direct_lifecycle_denied, invalid, is_denied, lifecycle_actor, missing_team, normalize_action, require_dict


def run(input_data, context, *, settings_owner=None):
    if require_dict(input_data) is None:
        return invalid("input_data must be a dict")
    company_id = company_id_from(input_data)
    if not company_id:
        return invalid("company_id is required")
    action = normalize_action(input_data.get("action"), "list")
    service = SubagentTeamService(settings_owner=settings_owner)
    try:
        if action == "list":
            dms = service.list_dms(company_id)
            if dms is None:
                return missing_team(company_id)
            return ok({"dms": dms, "total": len(dms)})
        if action in {"create", "ensure"}:
            blocked = direct_lifecycle_denied(input_data, context if isinstance(context, dict) else {})
            if blocked is not None:
                return blocked
            dm = service.ensure_dm(
                company_id,
                input_data,
                actor_id=lifecycle_actor(input_data, context if isinstance(context, dict) else {}),
            )
            if is_denied(dm):
                return denied(dm)
            if dm is None:
                return missing_team(company_id)
            return ok(dm)
        if action in {"send", "message"}:
            result = service.send_dm(company_id, input_data, context=context if isinstance(context, dict) else {})
            if is_denied(result):
                return denied(result)
            if result is None:
                return missing_team(company_id)
            return ok(result)
        if action == "messages":
            channel_id = input_data.get("dm_id") or input_data.get("channel_id")
            if not channel_id:
                return invalid("dm_id is required")
            result = service.list_messages(
                company_id,
                {**input_data, "channel_id": str(channel_id)},
                context=context if isinstance(context, dict) else {},
            )
            if result is None:
                return missing_team(company_id)
            if is_denied(result):
                return denied(result)
            messages, total = result
            return ok({"messages": messages, "total": total})
        return invalid("unsupported dms action: " + action)
    except Exception as exc:
        return error("subagent team dms failed: " + str(exc), "SUBAGENT_TEAM_DMS_ERROR")
