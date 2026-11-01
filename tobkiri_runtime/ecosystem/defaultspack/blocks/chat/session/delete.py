from blocks._common import ok, error, gen_id, timestamp

from domain.chat.session_manager import SessionManager


def run(input_data, context):
    manager = SessionManager()
    session_id = input_data.get("session_id")
    if not session_id:
        return error("session_id is required", "INVALID_INPUT")
    result = manager.delete_session(session_id)
    if not result:
        return error("Session not found", "NOT_FOUND")
    return ok({"success": True})
