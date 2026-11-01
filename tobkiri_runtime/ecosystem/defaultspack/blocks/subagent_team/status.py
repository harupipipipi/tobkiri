from blocks._common import ok, error
from domain.subagent_team.service import SubagentTeamService

from ._helpers import company_id_from, invalid, missing_team, require_dict


def run(input_data, context, *, settings_owner=None):
    if input_data is None:
        input_data = {}
    if require_dict(input_data) is None:
        return invalid("input_data must be a dict")
    try:
        service = SubagentTeamService(settings_owner=settings_owner)
        company_id = company_id_from(input_data)
        result = service.status(company_id) if company_id else None
        if result is None:
            ensured = service.ensure_team({**input_data, "bootstrap": bool(input_data.get("bootstrap"))})
            if ensured.get("company") is None:
                return missing_team(company_id or "")
            result = service.status(str(ensured["company"]["id"]))
        return ok(result)
    except Exception as exc:
        return error("subagent team status failed: " + str(exc), "SUBAGENT_TEAM_STATUS_ERROR")
