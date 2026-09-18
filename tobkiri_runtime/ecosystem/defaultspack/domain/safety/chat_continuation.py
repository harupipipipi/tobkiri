"""Narrow callbacks composed into the Host-owned chat continuation port."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..function_runtime.dispatcher import run_defaultspack_function
from . import approval
from .coding_ui_operator import verify_coding_ui_operator

_CODING_PREFIXES = ("file.", "git.", "shell.", "terminal.", "workspace.")
_COMPUTER_TOOLS = {"browser_computer", "browser_use", "computer_use"}
_EXACT_TOOLS = {"pack.approve": "coding_pack_approve"}


def approve_continuation(
    request_id: str,
    conversation_id: str,
    ui_operator: Mapping[str, Any],
    turn_id: str = "",
) -> Mapping[str, Any]:
    """Settle only an exact pending request and return a Host-only snapshot.

    ``turn_id`` is the canonical saved turn declared by the presentation
    owner.  When the request record itself carries a canonical turn the
    declared value must match it before any approval state settles; an
    unrecorded request still binds the declared turn into the Host handle so
    a later phase presenting a different turn is rejected there.
    """

    request = approval.get_approval_request(request_id)
    binding = _request_binding(request, conversation_id)
    recorded_turn = binding["turn_id"]
    if recorded_turn and recorded_turn != turn_id:
        raise PermissionError("approval continuation turn binding changed")
    verify_coding_ui_operator(
        dict(ui_operator),
        request_id=request_id,
        expected_digest=binding["args_hash"],
        decision="approve",
    )
    decision = approval.approve(request_id)
    if not decision.get("approved") or not decision.get("token"):
        raise PermissionError("approval request is not continuable")
    return {**decision, "binding": binding}


def resume_continuation(
    binding: Mapping[str, Any],
    token: str,
    conversation_id: str,
) -> Mapping[str, Any]:
    """Execute stored arguments once after rechecking request and token identity."""

    request = approval.get_approval_request(str(binding.get("request_id") or ""))
    if request is None:
        raise PermissionError("approval continuation request is unavailable")
    current = _request_binding(request, conversation_id)
    if current != dict(binding) or request.get("status") != "approved":
        raise PermissionError("approval continuation binding changed")
    verification = approval.verify_execution_token(
        token,
        current["operation"],
        current["args_hash"],
        consume=False,
    )
    if not verification.valid or verification.request_id != current["request_id"]:
        raise PermissionError("approval continuation token is invalid")
    details = request["details"]
    arguments = dict(details["arguments"])
    tool_name = current["tool_name"]
    result = run_defaultspack_function(
        tool_name,
        {**arguments, "approval_token": token},
        {
            "conversation_id": conversation_id,
            "turn_id": current["turn_id"],
            "approval_replay": True,
        },
    )
    if result.get("status") != "ok":
        return result
    settled = approval.get_approval_request(current["request_id"])
    if not isinstance(settled, dict) or settled.get("status") != "consumed":
        raise PermissionError("approval continuation did not consume its token")
    return {
        "resumed": True,
        "terminal_event": "tool_call_completed",
        "tool": tool_name,
    }


def _request_binding(
    request: object,
    conversation_id: str,
) -> dict[str, str]:
    if not isinstance(request, dict) or request.get("status") not in {"pending", "approved"}:
        raise PermissionError("approval request is unavailable")
    details = request.get("details")
    if not isinstance(details, dict) or not isinstance(details.get("arguments"), dict):
        raise PermissionError("approval request arguments are unavailable")
    operation = str(request.get("operation") or "")
    declared = str(details.get("tool_name") or "")
    function_id = str(details.get("function_id") or "")
    tool_name = declared if declared in _COMPUTER_TOOLS else function_id or declared
    expected = (
        tool_name
        if tool_name in _COMPUTER_TOOLS and operation.startswith(("browser.", "computer."))
        else _EXACT_TOOLS.get(
            operation,
            "coding_" + operation.replace(".", "_")
            if operation.startswith(_CODING_PREFIXES)
            else "",
        )
    )
    stored_conversation = str(
        request.get("conversation_id") or details.get("conversation_id") or ""
    )
    args_hash = str(request.get("args_hash") or "")
    if (
        not expected
        or tool_name != expected
        or stored_conversation != conversation_id
        or approval.hash_arguments(details["arguments"]) != args_hash
    ):
        raise PermissionError("approval continuation binding is invalid")
    return {
        "request_id": str(request.get("request_id") or ""),
        "conversation_id": stored_conversation,
        "turn_id": str(details.get("turn_id") or ""),
        "operation": operation,
        "args_hash": args_hash,
        "tool_name": tool_name,
        "tool_call_id": str(details.get("tool_call_id") or ""),
        "profile_id": str(request.get("profile_id") or details.get("profile_id") or ""),
    }
