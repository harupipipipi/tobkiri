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
            channels = service.list_channels(company_id)
            if channels is None:
                return missing_team(company_id)
            return ok({"channels": channels, "total": len(channels)})
        if action == "get":
            channel_id = input_data.get("channel_id") or input_data.get("id")
            if not channel_id:
                return invalid("channel_id is required")
            channel = service.get_channel(company_id, str(channel_id))
            if channel is None:
                return error("channel not found: " + str(channel_id), "NOT_FOUND")
            return ok(channel)
        if action in {"create", "upsert"}:
            blocked = direct_lifecycle_denied(input_data, context if isinstance(context, dict) else {})
            if blocked is not None:
                return blocked
            channel = input_data.get("channel")
            if channel is None:
                channel = {key: value for key, value in input_data.items() if key not in {"company_id", "action"}}
            if not isinstance(channel, dict):
                return invalid("channel must be a dict")
            updated = service.upsert_channel(
                company_id,
                channel,
                actor_id=lifecycle_actor(input_data, context if isinstance(context, dict) else {}),
            )
            if is_denied(updated):
                return denied(updated)
            if updated is None:
                return missing_team(company_id)
            return ok(updated)
        if action in {"update", "patch"}:
            blocked = direct_lifecycle_denied(input_data, context if isinstance(context, dict) else {})
            if blocked is not None:
                return blocked
            channel_id = input_data.get("channel_id") or input_data.get("id")
            if not channel_id:
                return invalid("channel_id is required")
            updates = input_data.get("updates") if isinstance(input_data.get("updates"), dict) else input_data.get("channel")
            if updates is None:
                updates = {key: value for key, value in input_data.items() if key not in {"company_id", "action", "channel_id", "id"}}
            if not isinstance(updates, dict):
                return invalid("updates must be a dict")
            updated = service.patch_channel(
                company_id,
                str(channel_id),
                updates,
                actor_id=lifecycle_actor(input_data, context if isinstance(context, dict) else {}),
            )
            if is_denied(updated):
                return denied(updated)
            if updated is None:
                return error("channel not found: " + str(channel_id), "NOT_FOUND")
            return ok(updated)
        if action in {"archive", "delete", "remove"}:
            blocked = direct_lifecycle_denied(input_data, context if isinstance(context, dict) else {})
            if blocked is not None:
                return blocked
            channel_id = input_data.get("channel_id") or input_data.get("id")
            if not channel_id:
                return invalid("channel_id is required")
            archived = service.archive_channel(
                company_id,
                str(channel_id),
                actor_id=lifecycle_actor(input_data, context if isinstance(context, dict) else {}),
            )
            if archived is None:
                return error("channel not found: " + str(channel_id), "NOT_FOUND")
            return ok({"archived": True, "channel": archived})
        if action == "join":
            blocked = direct_lifecycle_denied(input_data, context if isinstance(context, dict) else {})
            if blocked is not None:
                return blocked
            channel_id = input_data.get("channel_id") or input_data.get("id")
            member_id = str(input_data.get("member_id") or input_data.get("agent_id") or input_data.get("sender_id") or "user")
            if not channel_id:
                return invalid("channel_id is required")
            updated = service.join_channel(
                company_id,
                str(channel_id),
                member_id,
                actor_id=lifecycle_actor(input_data, context if isinstance(context, dict) else {}),
            )
            if is_denied(updated):
                return denied(updated)
            if updated is None:
                return error("channel not found: " + str(channel_id), "NOT_FOUND")
            return ok(updated)
        if action == "leave":
            blocked = direct_lifecycle_denied(input_data, context if isinstance(context, dict) else {})
            if blocked is not None:
                return blocked
            channel_id = input_data.get("channel_id") or input_data.get("id")
            member_id = str(input_data.get("member_id") or input_data.get("agent_id") or input_data.get("sender_id") or "user")
            if not channel_id:
                return invalid("channel_id is required")
            updated = service.leave_channel(
                company_id,
                str(channel_id),
                member_id,
                actor_id=lifecycle_actor(input_data, context if isinstance(context, dict) else {}),
            )
            if is_denied(updated):
                return denied(updated)
            if updated is None:
                return error("channel not found: " + str(channel_id), "NOT_FOUND")
            return ok(updated)
        if action in {"check", "channel.check"}:
            check = service.channel_check(company_id, input_data)
            if check is None:
                return missing_team(company_id)
            return ok(check)
        return invalid("unsupported channels action: " + action)
    except Exception as exc:
        return error("subagent team channels failed: " + str(exc), "SUBAGENT_TEAM_CHANNELS_ERROR")
