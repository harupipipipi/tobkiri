from blocks._common import ok, error, gen_id, timestamp

from domain.chat.session_manager import SessionManager


def run(input_data, context):
    manager = SessionManager()
    limit = input_data.get("limit", 50)
    offset = input_data.get("offset", 0)
    sessions, total = manager.list_sessions(limit=limit, offset=offset)
    active_session_id = manager.get_active_session_id()
    return ok({
        "sessions": sessions,
        "total": total,
        "active_session_id": active_session_id,
    })
