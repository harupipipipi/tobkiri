"""Ordered fail-closed guards before tool authorization and execution."""

from __future__ import annotations

from typing import Any, Callable, Mapping

_GUARD_ORDER = (
    "definition_enabled",
    "caller_bound",
    "profile_bound",
    "profile_permission",
    "not_cancelled",
    "deadline_remaining",
)


def create_guard_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Create the generic pre-authorization guard chain."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name not in {"evaluate", "guard"}:
            raise ValueError(f"unknown tool guard operation: {name}")
        decision_time = _number(payload.get("decision_time"))
        deadline = _number(payload.get("deadline"))
        checks = {
            "definition_enabled": bool(payload.get("definition_enabled", True)),
            "caller_bound": bool(str(payload.get("caller_id") or "").strip()),
            "profile_bound": bool(str(payload.get("profile_id") or "").strip()),
            "profile_permission": bool(payload.get("profile_permission", False)),
            "not_cancelled": not bool(payload.get("cancelled", False)),
            "deadline_remaining": (
                decision_time is not None
                and deadline is not None
                and decision_time < deadline
            ),
        }
        allowed = all(checks[item] for item in _GUARD_ORDER)
        return {
            "allowed": allowed,
            "guard_order": list(_GUARD_ORDER),
            "checks": checks,
            "reason": "allowed"
            if allowed
            else next(item for item in _GUARD_ORDER if not checks[item]),
        }

    return operation


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None

