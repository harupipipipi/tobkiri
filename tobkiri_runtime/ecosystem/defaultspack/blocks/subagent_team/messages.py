from blocks._common import ok, error
from domain.subagent_team.service import SubagentTeamService

from ._helpers import company_id_from, denied, invalid, is_denied, missing_team, require_dict


def run(input_data, context, *, settings_owner=None):
    if require_dict(input_data) is None:
        return invalid("input_data must be a dict")
    company_id = company_id_from(input_data)
    if not company_id:
        return invalid("company_id is required")
    action = str(input_data.get("action") or "list").lower()
    service = SubagentTeamService(settings_owner=settings_owner)
    try:
        if action == "list":
            result = service.list_messages(company_id, input_data, context=context if isinstance(context, dict) else {})
            if result is None:
                return missing_team(company_id)
            if is_denied(result):
                return denied(result)
            messages, total = result
            return ok({"messages": messages, "total": total})
        if action in {"send", "create", "add"}:
            result = service.send_message(company_id, input_data, context=context if isinstance(context, dict) else {})
            if is_denied(result):
                return denied(result)
            if result is None:
                return missing_team(company_id)
            return ok(result)
        if action == "parse":
            return ok(service.parse_message(str(input_data.get("content") or input_data.get("message") or "")))
        return invalid("unsupported messages action: " + action)
    except Exception as exc:
        return error("subagent team messages failed: " + str(exc), "SUBAGENT_TEAM_MESSAGES_ERROR")
