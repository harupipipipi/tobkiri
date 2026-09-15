"""Server-owned deterministic replay for delegated CLI coding approvals."""

from __future__ import annotations

from typing import Any

from blocks._common import error, ok
from domain.function_runtime.dispatcher import run_defaultspack_function
from domain.safety import approval

_CODING_OPERATION_PREFIXES = ("file.", "git.", "shell.", "terminal.", "workspace.")
_EXACT_REPLAY_TOOLS = {"pack.approve": "coding_pack_approve"}


def run(input_data: dict[str, Any], context: dict[str, Any] | None = None):
    request_id = str(input_data.get("request_id") or "").strip()
    resume_id = str(input_data.get("resume_id") or "").strip()
    conversation_id = str(input_data.get("conversation_id") or "").strip()
    if not request_id or not resume_id or not conversation_id:
        return error("resume binding is incomplete", code="DEBUG_RESUME_INVALID")

    request = approval.get_approval_request(request_id)
    if not isinstance(request, dict) or request.get("status") != "approved":
        return error("approval request is not resumable", code="DEBUG_RESUME_INVALID")
    raw_details = request.get("details")
    details = raw_details if isinstance(raw_details, dict) else {}
    operation = str(request.get("operation") or "").strip()
    tool_name = str(details.get("function_id") or details.get("tool_name") or "").strip()
    arguments = details.get("arguments")
    expected_tool = _EXACT_REPLAY_TOOLS.get(
        operation,
        "coding_" + operation.replace(".", "_")
        if operation.startswith(_CODING_OPERATION_PREFIXES)
        else "",
    )
    if (
        not expected_tool
        or tool_name != expected_tool
        or str(details.get("conversation_id") or "").strip() != conversation_id
        or not isinstance(arguments, dict)
        or approval.hash_arguments(arguments) != str(request.get("args_hash") or "")
    ):
        return error("approval replay binding changed", code="DEBUG_RESUME_MISMATCH")

    token = approval.resolve_debug_resume_handle(resume_id, request_id)
    verification = approval.verify_execution_token(
        token,
        operation,
        str(request.get("args_hash") or ""),
        consume=False,
    )
    if not verification.valid or verification.request_id != request_id:
        return error(
            verification.message or "approval resume credential is invalid",
            code=verification.code or "DEBUG_RESUME_INVALID",
        )

    replay_input = {**arguments, "approval_token": token}
    replay_context = {
        **dict(context or {}),
        "conversation_id": conversation_id,
        "approval_replay": True,
    }
    if operation == "pack.approve":
        from blocks.coding.pack_approve import run as approve_pack

        result = approve_pack(replay_input, replay_context)
    else:
        result = run_defaultspack_function(tool_name, replay_input, replay_context)
    if result.get("status") != "ok":
        return result
    settled = approval.get_approval_request(request_id)
    if not isinstance(settled, dict) or settled.get("status") != "consumed":
        return error(
            "approved operation did not consume its one-shot token",
            code="DEBUG_RESUME_NOT_CONSUMED",
        )
    return ok(
        {
            "resumed": True,
            "terminal_event": "tool_call_completed",
            "tool": tool_name,
        }
    )
