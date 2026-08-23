

from blocks._common import error, ok
from blocks.agent._state import set_multi_session
from domain.agent.multi import MultiAgentOrchestrator
from domain.coding.frontend_precision import promote_coding_session_input


def run(input_data, context=None):
    if not isinstance(input_data, dict):
        return error("input_data must be a dict")
    context = context or {}
    input_data, frontend_precision = promote_coding_session_input(input_data, context)
    task = input_data.get("task") or "Coding subagent session"
    agents = input_data.get("agents") or [
        {"name": "worker", "role": "coding worker", "model": input_data.get("model", "stub/default"), "tools": []}
    ]
    if not isinstance(agents, list) or not agents:
        return error("agents must be a non-empty list", code="INVALID_INPUT")
    orchestration = input_data.get("orchestration", "round_robin")
    if orchestration not in ("round_robin", "directed", "free"):
        return error("orchestration must be one of: round_robin, directed, free", code="INVALID_INPUT")
    max_turns = input_data.get("max_turns", 10)
    if not isinstance(max_turns, int) or max_turns < 1:
        return error("max_turns must be a positive integer", code="INVALID_INPUT")
    workspace_root = input_data.get("workspace_root") or context.get("workspace_root") or context.get("conversation_workspace_dir")
    workspace_id = input_data.get("workspace_id") or context.get("workspace_id")
    result = MultiAgentOrchestrator().create_session(
        task=task,
        agent_dicts=agents,
        orchestration=orchestration,
        max_turns=max_turns,
        workspace_root=workspace_root,
        workspace_id=workspace_id,
        worktree_mode=input_data.get("worktree_mode") or "metadata_only",
        execution_attempt_id=(
            input_data.get("execution_attempt_id")
            or input_data.get("attempt_id")
            or context.get("execution_attempt_id")
            or context.get("attempt_id")
        ),
        base_commit=input_data.get("base_commit") or context.get("base_commit"),
        base_ref=input_data.get("base_ref") or context.get("base_ref"),
        context=context,
    )
    if result.get("status") == "error":
        return error(result.get("error", "session create failed"), code="AGENT_SESSION_CREATE_ERROR")
    session = result.get("session")
    if session is not None:
        if frontend_precision.get("enabled"):
            session.shared_context["frontend_precision"] = frontend_precision
            session.shared_context.setdefault("workspace", {})["frontend_precision"] = frontend_precision
        set_multi_session(result["session_id"], session)
    payload = {
        "session_id": result.get("session_id"),
        "status": result.get("status"),
        "session": session.to_dict() if session is not None else result.get("result"),
        "workspace": result.get("workspace", {}),
    }
    if frontend_precision.get("enabled"):
        payload["frontend_precision"] = frontend_precision
    return ok(payload)
