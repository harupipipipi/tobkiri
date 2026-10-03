"""Host-owned opaque handles for exact chat approval continuation."""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from .ports import ChatApprovalContinuationCommand


class ChatApprovalContinuationController:
    """Retain execution tokens in Host memory and atomically claim them once."""

    def __init__(
        self,
        *,
        approve: Callable[
            [str, str, Mapping[str, Any], str], Mapping[str, Any]
        ],
        resume: Callable[[Mapping[str, Any], str, str], Mapping[str, Any]],
    ) -> None:
        self._approve = approve
        self._resume = resume
        self._handles: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def approve_chat_continuation(
        self,
        command: ChatApprovalContinuationCommand,
    ) -> Mapping[str, Any]:
        """Approve and retain an exact server snapshot without returning its token."""

        if not isinstance(command.ui_operator, Mapping):
            raise PermissionError("chat approval ui_operator is required")
        approved = dict(
            self._approve(
                command.request_id,
                command.conversation_id,
                command.ui_operator,
                command.turn_id,
            )
        )
        token = str(approved.pop("token", "") or "")
        binding = approved.pop("binding", None)
        expires_at = int(approved.get("expires_at") or 0)
        if (
            approved.get("approved") is not True
            or not token
            or not isinstance(binding, Mapping)
            or expires_at <= int(time.time())
        ):
            raise PermissionError("chat approval continuation is unavailable")
        expected = _binding(command, binding)
        handle = "host_resume_" + secrets.token_urlsafe(24)
        with self._lock:
            self._handles[handle] = {
                **expected,
                "token": token,
                "binding": dict(binding),
                "expires_at": expires_at,
            }
        return {
            **approved,
            "resume_id": handle,
            **_continuation_identity(command, expected["turn_id"]),
            "terminal": None,
        }

    def resume_chat_continuation(
        self,
        command: ChatApprovalContinuationCommand,
    ) -> Mapping[str, Any]:
        """Claim before execution and reject changed, stale, foreign, or reused state."""

        with self._lock:
            record = self._handles.pop(command.resume_id, None)
        if not isinstance(record, dict) or int(record.get("expires_at") or 0) <= int(time.time()):
            raise PermissionError("chat approval continuation is unavailable")
        binding = record.get("binding")
        if not isinstance(binding, Mapping):
            raise PermissionError("chat approval continuation is unavailable")
        expected = _binding(command, binding)
        if any(record.get(key) != value for key, value in expected.items()):
            raise PermissionError("chat approval continuation is unavailable")
        result = dict(
            self._resume(
                dict(binding),
                str(record.get("token") or ""),
                command.conversation_id,
            )
        )
        identity = _continuation_identity(command, expected["turn_id"])
        return {
            **result,
            **identity,
            "terminal": _continuation_terminal(identity, result),
        }


def _continuation_identity(
    command: ChatApprovalContinuationCommand,
    turn_id: str,
) -> dict[str, str]:
    """Project the canonical identities the bound continuation packet carries."""

    return {
        "turn_id": turn_id,
        "conversation_id": command.conversation_id,
        "operation_id": turn_id,
        "request_id": command.request_id,
    }


def _continuation_terminal(
    identity: Mapping[str, str],
    result: Mapping[str, Any],
) -> dict[str, Any]:
    """Project the continuation outcome as a canonical turn terminal receipt."""

    if result.get("resumed") is True:
        return {
            **identity,
            "status": "completed",
            "result_reference": {
                "tool": str(result.get("tool") or ""),
                "terminal_event": str(result.get("terminal_event") or ""),
            },
            "error": None,
        }
    return {
        **identity,
        "status": "failed",
        "result_reference": None,
        "error": dict(result),
    }


def _binding(
    command: ChatApprovalContinuationCommand,
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    context = command.context
    recorded_turn = str(snapshot.get("turn_id") or "")
    if (
        snapshot.get("request_id") != command.request_id
        or snapshot.get("conversation_id") != command.conversation_id
        or (recorded_turn and recorded_turn != command.turn_id)
    ):
        raise PermissionError("chat approval binding changed")
    return {
        "request_id": command.request_id,
        "conversation_id": command.conversation_id,
        "turn_id": recorded_turn or str(command.turn_id or ""),
        "operation": str(snapshot.get("operation") or ""),
        "args_hash": str(snapshot.get("args_hash") or ""),
        "tool_name": str(snapshot.get("tool_name") or ""),
        "tool_call_id": str(snapshot.get("tool_call_id") or ""),
        "profile_id": context.profile_id,
        "profile_revision": context.profile_revision,
        "activation_id": context.activation_id,
        "activation_digest": context.activation_digest,
        "plan_digest": context.plan_digest,
        "security_epoch": context.security_epoch,
        "fencing_token": context.fencing_token,
        "caller_principal": context.caller_principal.value,
        "caller_session_id": context.caller_session_id,
        "presentation_owner_principal_id": command.presentation_owner_principal_id,
        "presentation_owner_session_id": command.presentation_owner_session_id,
    }
