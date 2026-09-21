from blocks._common import ok, error
from domain.subagent_team.service import SubagentTeamService

from ._helpers import company_id_from, denied, invalid, is_denied, missing_team, normalize_action, require_dict


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
            result = service.list_goals(company_id, input_data)
            if result is None:
                return missing_team(company_id)
            goals, total = result
            return ok({"goals": goals, "total": total})
        if action == "get":
            goal_id = input_data.get("goal_id") or input_data.get("task_id") or input_data.get("id")
            if not goal_id:
                return invalid("goal_id is required")
            goal = service.get_goal(company_id, str(goal_id))
            if goal is None:
                return error("goal not found: " + str(goal_id), "NOT_FOUND")
            return ok(goal)
        if action in {"create", "add", "propose"}:
            goal = service.create_goal(company_id, input_data, context=context if isinstance(context, dict) else {})
            if is_denied(goal):
                return denied(goal)
            if goal is None:
                return missing_team(company_id)
            return ok(goal)
        if action in {"update", "close", "complete", "approve", "reject", "task_complete"}:
            goal_id = input_data.get("goal_id") or input_data.get("task_id") or input_data.get("id")
            if not goal_id:
                return invalid("goal_id is required")
            if action in {"close", "complete", "approve", "reject", "task_complete"}:
                goal = service.decide_goal(
                    company_id,
                    str(goal_id),
                    action,
                    input_data,
                    context=context if isinstance(context, dict) else {},
                )
                if is_denied(goal):
                    return denied(goal)
                if goal is None:
                    return error("goal not found: " + str(goal_id), "NOT_FOUND")
                return ok(goal)
            updates = input_data.get("updates") if isinstance(input_data.get("updates"), dict) else {
                key: value
                for key, value in input_data.items()
                if key not in {"company_id", "action", "goal_id", "task_id", "id"}
            }
            goal = service.update_goal(company_id, str(goal_id), updates)
            if is_denied(goal):
                return denied(goal)
            if goal is None:
                return error("goal not found: " + str(goal_id), "NOT_FOUND")
            return ok(goal)
        return invalid("unsupported goals action: " + action)
    except Exception as exc:
        return error("subagent team goals failed: " + str(exc), "SUBAGENT_TEAM_GOALS_ERROR")
