"""Forward redacted tool lifecycle events to the core audit owner."""

from __future__ import annotations

from typing import Any, Callable, Mapping

from core_runtime.audit_logger import get_audit_logger

_EVENTS = {
    "resolved",
    "validated",
    "guarded",
    "policy_decided",
    "approval_required",
    "authorized",
    "executor_selected",
    "started",
    "completed",
    "rejected",
    "cancelled",
    "failed",
}
_FIELDS = {
    "tool_call_id",
    "tool_id",
    "caller_id",
    "profile_id",
    "authority",
    "args_hash",
    "definition_hash",
    "executor_provider_instance_id",
    "executor_content_hash",
    "decision",
    "reason",
    "error_code",
    "duration_ms",
}


def create_emit_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Create an allowlisted, secret-free lifecycle event adapter."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name not in {"emit", "record"}:
            raise ValueError(f"unknown tool audit operation: {name}")
        event = str(payload.get("event") or "").strip()
        if event not in _EVENTS:
            raise ValueError("tool audit event is not allowed")
        details = {
            key: payload[key]
            for key in sorted(_FIELDS)
            if key in payload
            and isinstance(payload[key], (str, int, float, bool, type(None)))
        }
        success = event not in {"rejected", "cancelled", "failed"}
        get_audit_logger().log_system_event(
            f"tool_invocation_{event}",
            success=success,
            details=details,
            error=str(payload.get("error_code") or "") or None,
        )
        return {"recorded": True, "event": event}

    return operation

