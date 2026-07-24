"""Deterministic request normalization and replay-safe failover decisions."""

from __future__ import annotations

from typing import Any, Callable, Mapping

from core_runtime.global_contract_dispatch import GlobalContractInvocationError

_RETRYABLE = {"provider_unavailable", "quota", "invalid_response"}
_REQUIREMENT_KEYS = {
    "modalities",
    "capabilities",
    "tool_calling",
    "thinking",
    "minimum_context",
    "request_surface",
    "data_residency",
    "maximum_cost",
    "preferred_model_id",
    "preferred_provider_id",
    "preferred_provider_instance_id",
    "health_max_age",
}


def create_prepare_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Create deterministic provider-neutral request preparation."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name not in {"prepare", "normalize"}:
            raise ValueError(f"unknown AI pipeline operation: {name}")
        decision_time = _number(payload.get("decision_time"))
        if decision_time is None:
            raise ValueError("AI request decision_time is required")
        deadline = _number(payload.get("deadline"))
        deadline = deadline if deadline is not None else decision_time + 60.0
        if deadline <= decision_time:
            raise GlobalContractInvocationError(
                "deadline", "request deadline elapsed"
            )
        credential_handle = payload.get("credential_handle")
        if credential_handle is not None and not str(
            credential_handle
        ).startswith(("credential:", "opaque:")):
            raise GlobalContractInvocationError(
                "denied", "AI pipeline accepts only opaque credential handles"
            )
        requirements = payload.get("requirements")
        requirements = requirements if isinstance(requirements, Mapping) else {}
        request_id = str(payload.get("request_id") or "").strip()
        if not request_id:
            raise ValueError("AI request_id is required")
        return {
            "request_id": request_id,
            "decision_time": decision_time,
            "deadline": deadline,
            "messages": list(payload.get("messages") or []),
            "input": payload.get("input"),
            "parameters": dict(payload.get("parameters") or {}),
            "tools": list(payload.get("tools") or []),
            "requirements": {
                str(key): value
                for key, value in requirements.items()
                if key in _REQUIREMENT_KEYS
            },
            "credential_handle": credential_handle,
            "idempotency_key": payload.get("idempotency_key"),
            "allow_failover": bool(payload.get("allow_failover", False)),
            "policy_revision": str(payload.get("policy_revision") or ""),
            "conversation_id": payload.get("conversation_id"),
            "profile_id": payload.get("profile_id"),
            "model_profile_id": payload.get("model_profile_id"),
            "model_reference": payload.get("model_reference"),
        }

    return operation


def create_failover_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Create replay-safe retry/failover policy decisions."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name not in {"decide", "allow"}:
            raise ValueError(f"unknown AI failover operation: {name}")
        error_code = str(payload.get("error_code") or "")
        attempt = max(1, int(payload.get("attempt") or 1))
        candidate_count = max(0, int(payload.get("candidate_count") or 0))
        deadline = _number(payload.get("deadline"))
        decision_time = _number(payload.get("decision_time"))
        checks = {
            "explicitly_allowed": bool(payload.get("allow_failover", False)),
            "idempotency_bound": bool(payload.get("idempotency_key")),
            "tool_free": not bool(payload.get("tools")),
            "retryable_error": error_code in _RETRYABLE,
            "candidate_available": attempt < candidate_count,
            "deadline_remaining": (
                deadline is not None
                and decision_time is not None
                and decision_time < deadline
            ),
        }
        allowed = all(checks.values())
        return {
            "allowed": allowed,
            "checks": checks,
            "reason": "replay_safe_failover"
            if allowed else next(key for key, value in checks.items() if not value),
        }

    return operation


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
