import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import ok, error
from domain.agent.subagent_delegation import ERROR_CATEGORIES, actionable_hint
from domain.agent.subagent_orchestrator import run_subagent_compat


_PUBLIC_DELEGATION_DETAIL_KEYS = {
    "available_targets",
    "capability_error_type",
    "code",
    "exception_type",
    "matched_targets",
    "mentions",
    "route",
    "status",
    "target_agent_id",
    "unknown_targets",
}


def _public_delegation_error(result):
    """Project a delegated error into a stable, non-sensitive tool response."""
    source = result.get("delegation_error") if isinstance(result, dict) else {}
    source = source if isinstance(source, dict) else {}
    source_details = source.get("details") if isinstance(source.get("details"), dict) else {}
    details = {
        key: value
        for key, value in source_details.items()
        if key in _PUBLIC_DELEGATION_DETAIL_KEYS
    }
    category = str(source.get("category") or "unknown").strip().lower()
    if category not in ERROR_CATEGORIES:
        category = "unknown"
    code = str(source.get("code") or result.get("code") or "SUBAGENT_DELEGATION_FAILED").strip()
    code = code[:128] or "SUBAGENT_DELEGATION_FAILED"
    return {
        "type": "subagent_delegation_error",
        "category": category,
        "code": code,
        "details": details,
        "actionable_hint": actionable_hint(category, code, details),
    }


def run(input_data, context):
    data = input_data if isinstance(input_data, dict) else {}
    role_id = str(data.get("role_id") or data.get("role") or "").strip()
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else data
    if not role_id and isinstance(payload, dict) and any(payload.get(key) for key in ("task", "prompt")):
        role_id = "delegate"
    if not role_id:
        return error("role_id is required", "MISSING_PARAM")
    try:
        result = run_subagent_compat(
            role_id,
            payload,
            model=str(data.get("model") or ""),
            settings=data.get("settings") if isinstance(data.get("settings"), dict) else {},
            call_handler=(context or {}).get("call_handler") if isinstance(context, dict) else None,
            context=context if isinstance(context, dict) else {},
        )
        if isinstance(result, dict) and result.get("status") == "error":
            delegation_error = _public_delegation_error(result)
            code = delegation_error["code"]
            message = "Subagent delegation failed. " + delegation_error["actionable_hint"]
            return {
                "status": "error",
                "error": {
                    "code": code,
                    "message": message,
                    "details": {"delegation_error": delegation_error},
                },
            }
        return ok(result)
    except ValueError as exc:
        return error(str(exc), "INVALID_SUBAGENT_ROLE")
