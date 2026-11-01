from blocks._common import ok, error, gen_id, timestamp

from domain.chat.session_manager import SessionManager


def run(input_data, context):
    manager = SessionManager()
    session_id = input_data.get("session_id")
    if not session_id:
        return error("session_id is required", "INVALID_INPUT")
    session = manager.get_session(session_id)
    if session is None:
        return error("Session not found", "NOT_FOUND")
    # Also fetch the conversations within this session
    conversations = manager.list_conversations(session_id)
    session["conversations"] = conversations if conversations is not None else []
    return ok(session)
