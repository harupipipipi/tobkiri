"""Compose the global tool invocation pipeline without concrete tool branches."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, Callable, Mapping

from core_runtime.global_contract_dispatch import (
    GlobalContractClient,
    GlobalContractInvocationError,
)
from core_runtime.resolved_profile_scope import active_resolved_profile

DEFINITION = "rumi.resource.tool.definition.v1"
VALIDATE = "rumi.service.tool.arguments.validate.v1"
GUARD = "rumi.service.tool.guard.evaluate.v1"
POLICY = "rumi.service.tool.policy.evaluate.v1"
AUTHORIZE = "rumi.service.tool.authorize.v1"
SELECTOR = "rumi.service.tool.executor.select.v1"
EXECUTE = "rumi.service.tool.execute.v1"
NORMALIZE = "rumi.service.tool.result.normalize.v1"
AUDIT = "rumi.event.tool.invocation.v1"


def create_invoke_operation(
    client: GlobalContractClient,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Create the resolve-to-audit provider-neutral invocation pipeline."""

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name not in {"invoke", "execute"}:
            raise ValueError(f"unknown tool broker operation: {name}")
        return _invoke(client, payload)

    return operation


def _invoke(
    client: GlobalContractClient,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    plan = active_resolved_profile()
    if plan is None:
        raise GlobalContractInvocationError(
            "profile_unavailable", "profile is inactive"
        )
    tool_id = str(payload.get("tool_id") or payload.get("tool_name") or "").strip()
    caller_id = str(payload.get("caller_id") or "").strip()
    tool_call_id = str(payload.get("tool_call_id") or uuid.uuid4())
    arguments = payload.get("arguments")
    if not tool_id or not caller_id or not isinstance(arguments, Mapping):
        raise GlobalContractInvocationError(
            "invalid_request", "tool request is incomplete"
        )
    base = {
        "tool_call_id": tool_call_id,
        "tool_id": tool_id,
        "caller_id": caller_id,
        "profile_id": plan.profile_id,
        "args_hash": _hash(arguments),
    }
    resolved = client.invoke(
        DEFINITION,
        "resolve",
        {"profile_id": plan.profile_id, "tool_id": tool_id},
    )
    if not isinstance(resolved, Mapping) or not isinstance(
        resolved.get("definition"), Mapping
    ):
        _audit(client, "rejected", base, reason="tool_unknown")
        raise GlobalContractInvocationError("tool_unknown", "tool is not registered")
    definition = dict(resolved["definition"])
    tool_id = str(resolved.get("resolved_tool_id") or tool_id)
    base.update(
        {
            "tool_id": tool_id,
            "definition_hash": str(definition.get("definition_hash") or ""),
        }
    )
    _audit(client, "resolved", base)
    validation = client.invoke(
        VALIDATE,
        "validate",
        {"schema": definition.get("input_schema") or {}, "arguments": arguments},
    )
    if not isinstance(validation, Mapping) or not validation.get("valid"):
        _audit(client, "rejected", base, reason="arguments_invalid")
        return _error(client, base, "arguments_invalid", "tool arguments are invalid")
    normalized_arguments = validation.get("arguments")
    authority = str(definition.get("authority") or "")
    base["authority"] = authority
    permissions = set(plan.effective_permissions)
    now = time.time()
    deadline = _number(payload.get("deadline"))
    deadline = deadline if deadline is not None else now + 60.0
    guard = client.invoke(
        GUARD,
        "evaluate",
        {
            "definition_enabled": bool(definition.get("enabled", True)),
            "caller_id": caller_id,
            "profile_id": plan.profile_id,
            "profile_permission": authority in permissions,
            "cancelled": bool(payload.get("cancelled", False)),
            "decision_time": now,
            "deadline": deadline,
        },
    )
    if not isinstance(guard, Mapping) or not guard.get("allowed"):
        reason = str((guard or {}).get("reason") or "guard_rejected")
        _audit(client, "rejected", base, reason=reason)
        return _error(client, base, "guard_rejected", reason)
    _audit(client, "guarded", base)
    policy = client.invoke(
        POLICY,
        "evaluate",
        {
            "authority": authority,
            "granted_authorities": sorted(permissions),
            "denied_authorities": [],
        },
    )
    if not isinstance(policy, Mapping) or not policy.get("allowed"):
        reason = str((policy or {}).get("reason") or "policy_rejected")
        _audit(client, "rejected", base, reason=reason)
        return _error(client, base, "policy_rejected", reason)
    _audit(client, "policy_decided", base, decision="allow")
    authorization = client.invoke(
        AUTHORIZE,
        "authorize",
        {
            **base,
            "arguments": normalized_arguments,
            "approval_required": bool(policy.get("approval_required")),
            "risk": policy.get("risk"),
            "approval_token": payload.get("approval_token"),
            "approval_request_id": payload.get("approval_request_id"),
        },
    )
    if not isinstance(authorization, Mapping) or not authorization.get("authorized"):
        _audit(client, "approval_required", base, reason="approval_required")
        return {
            **_error_envelope(base, "approval_required", "approval is required"),
            "approval": dict(authorization or {}),
        }
    _audit(client, "authorized", base)
    execution = definition.get("execution")
    execution = execution if isinstance(execution, Mapping) else {}
    providers = [dict(item) for item in client.providers(EXECUTE)]
    selection = client.invoke(
        SELECTOR,
        "select",
        {
            "execution_kind": execution.get("kind"),
            "execution_contract_id": execution.get("contract_id"),
            "providers": providers,
        },
    )
    selected = selection.get("selected") if isinstance(selection, Mapping) else None
    if not isinstance(selected, Mapping):
        _audit(client, "rejected", base, reason="missing_executor")
        return _error(client, base, "missing_executor", "tool executor is unavailable")
    provider_instance_id = str(selected.get("provider_instance_id") or "")
    content_hash = str(selected.get("content_hash") or "")
    executor_fields = {
        "executor_provider_instance_id": provider_instance_id,
        "executor_content_hash": content_hash,
    }
    _audit(client, "executor_selected", {**base, **executor_fields})
    _audit(client, "started", {**base, **executor_fields})
    started = time.monotonic()
    try:
        raw = client.invoke(
            EXECUTE,
            "execute",
            {
                **base,
                "arguments": normalized_arguments,
                "definition": definition,
                "deadline": deadline,
                "authorization": dict(authorization),
            },
            provider_instance_id=provider_instance_id,
        )
        result = client.invoke(
            NORMALIZE,
            "normalize",
            {**base, **executor_fields, "value": raw},
        )
    except Exception as exc:
        result = client.invoke(
            NORMALIZE,
            "normalize",
            {
                **base,
                **executor_fields,
                "executor_error": type(exc).__name__,
                "error_code": "executor_failed",
            },
        )
    duration_ms = int((time.monotonic() - started) * 1000)
    failed = bool(isinstance(result, Mapping) and result.get("is_error"))
    _audit(
        client,
        "failed" if failed else "completed",
        {**base, **executor_fields},
        duration_ms=duration_ms,
        error_code="executor_failed" if failed else None,
    )
    return dict(result) if isinstance(result, Mapping) else _error_envelope(
        base, "invalid_result", "result normalizer returned an invalid value"
    )


def _error(
    client: GlobalContractClient,
    base: Mapping[str, Any],
    code: str,
    message: str,
) -> dict[str, Any]:
    value = client.invoke(
        NORMALIZE,
        "normalize",
        {**base, "executor_error": message, "error_code": code},
    )
    return dict(value) if isinstance(value, Mapping) else _error_envelope(
        base, code, message
    )


def _error_envelope(
    base: Mapping[str, Any], code: str, message: str
) -> dict[str, Any]:
    return {
        "tool_call_id": base.get("tool_call_id"),
        "tool_id": base.get("tool_id"),
        "status": "error",
        "is_error": True,
        "result": None,
        "error": {"code": code, "message": message},
        "widget": None,
    }


def _audit(
    client: GlobalContractClient,
    event: str,
    base: Mapping[str, Any],
    **fields: Any,
) -> None:
    client.invoke(AUDIT, "emit", {"event": event, **dict(base), **fields})


def _hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None

