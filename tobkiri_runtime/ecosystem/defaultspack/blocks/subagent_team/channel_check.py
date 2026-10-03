from blocks._common import ok, error
from domain.subagent_team.service import SubagentTeamService

from ._helpers import company_id_from, invalid, missing_team, require_dict


def run(input_data, context, *, settings_owner=None):
    if require_dict(input_data) is None:
        return invalid("input_data must be a dict")
    company_id = company_id_from(input_data)
    if not company_id:
        return invalid("company_id is required")
    try:
        check = SubagentTeamService(settings_owner=settings_owner).channel_check(company_id, input_data)
        if check is None:
            return missing_team(company_id)
        return ok(check)
    except Exception as exc:
        return error("subagent team channel.check failed: " + str(exc), "SUBAGENT_TEAM_CHANNEL_CHECK_ERROR")
