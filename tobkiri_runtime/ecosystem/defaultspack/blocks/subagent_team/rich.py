from blocks._common import ok, error
from domain.subagent_team.service import SubagentTeamService

from ._helpers import company_id_from, denied, invalid, is_denied, missing_team, require_dict


def run(input_data, context, *, settings_owner=None):
    if require_dict(input_data) is None:
        return invalid("input_data must be a dict")
    try:
        service = SubagentTeamService(settings_owner=settings_owner)
        company_id = company_id_from(input_data)
        action = str(input_data.get("action") or "preview").lower()
        if action in {"get", "status", "read"}:
            if not company_id:
                return invalid("company_id is required")
            result = service.rich_status(company_id, requested_new_agents=int(input_data.get("requested_new_agents") or 0))
            if result is None:
                return missing_team(company_id)
            return ok(result)
        if action in {"set", "update", "enable", "disable"}:
            if not company_id:
                return invalid("company_id is required")
            payload = dict(input_data)
            if action == "enable":
                payload["enabled"] = True
            if action == "disable":
                payload["enabled"] = False
            result = service.update_rich_state(company_id, payload, context=context if isinstance(context, dict) else {})
            if is_denied(result):
                return denied(result)
            if result is None:
                return missing_team(company_id)
            return ok(result)
        return ok(service.rich_preview(input_data))
    except Exception as exc:
        return error("subagent team rich policy failed: " + str(exc), "SUBAGENT_TEAM_RICH_ERROR")
