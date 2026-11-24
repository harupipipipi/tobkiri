

from blocks._common import error, ok
from blocks.agent._state import get_multi_session
from domain.agent.multi import MultiAgentOrchestrator


def run(input_data, context=None):
    del context
    session_id = str(input_data.get("session_id") or "").strip()
    if not session_id:
        return error("session_id is required", code="INVALID_INPUT")
    session = get_multi_session(session_id)
    if session is None:
        return error("session not found: " + session_id, code="SESSION_NOT_FOUND")
    return ok(MultiAgentOrchestrator().get_status(session))
