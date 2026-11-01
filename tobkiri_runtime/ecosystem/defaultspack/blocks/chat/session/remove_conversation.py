from blocks._common import ok, error, gen_id, timestamp

from domain.chat.session_manager import SessionManager


def run(input_data, context):
    manager = SessionManager()
    session_id = input_data.get("session_id")
    if not session_id:
        return error("session_id is required", "INVALID_INPUT")
    conversation_id = input_data.get("conversation_id")
    if not conversation_id:
        return error("conversation_id is required", "INVALID_INPUT")
    try:
        session = manager.remove_conversation(session_id, conversation_id)
    except ValueError as exc:
        msg = str(exc)
        if msg == "conversation_not_in_session":
            return error("Conversation not in this session", "NOT_FOUND")
        return error(msg, "ERROR")
    if session is None:
        return error("Session not found", "NOT_FOUND")
    return ok(session)
