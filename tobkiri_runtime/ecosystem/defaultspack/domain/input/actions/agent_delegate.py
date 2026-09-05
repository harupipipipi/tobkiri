from __future__ import annotations

from typing import Any

from domain.input.envelope import RumiInputEnvelope


_FAILED_DELEGATE_STATUSES = {"error", "failed", "failure", "timeout", "cancelled", "canceled"}
_AUTHORITY_APPROVAL_STATUSES = {"authority_approval_required", "approval_required"}
_PROVIDER_ERROR_HINTS = (
    "provider error",
    "provider is not configured",
    "model provider",
    "llm provider",
)
_DELEGATE_RUNTIME_CONTEXT_KEYS = (
    "conversation_id",
    "node_id",
    "graph_id",
    "agent_id",
    "company_id",
    "timezone",
)
_TRUSTED_PLACEMENT_CONTEXT_KEYS = (
    "agent_kind",
    "runtime_kind",
    "subagent_role",
    "placement_id",
    "placement_revision",
    "placement_map_id",
    "protocol_membership",
    "effective_subagent_plan",
    "effective_plan_hash",
    "root_scope_id",
    "parent_run_id",
    "root_run_id",
)


def handle(envelope: RumiInputEnvelope, context: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = _delegate_payload(envelope)
    task = str(payload.get("task") or payload.get("prompt") or envelope.input or "").strip()
    if not task:
        return {"status": "error", "code": "MISSING_INPUT", "error": "task is required", "assistant_text": ""}
    from blocks.agent.execute import run as execute_agent

    result = execute_agent(
        {
            "task": task,
            "tools": list(payload.get("tools") if isinstance(payload.get("tools"), list) else envelope.tools),
            "model": str(payload.get("model") or payload.get("profile_id") or payload.get("preferred_model") or ""),
            "system_prompt": payload.get("system_prompt"),
            "runtime_profile_key": payload.get("runtime_profile_key"),
            "capability_profile": payload.get("capability_profile"),
            "required_capabilities": payload.get("required_capabilities") or payload.get("capability"),
            "params": dict(payload.get("params") if isinstance(payload.get("params"), dict) else {}),
            "attachments": list(payload.get("attachments") if isinstance(payload.get("attachments"), list) else envelope.attachments),
            "target": dict(envelope.target if isinstance(envelope.target, dict) else {}),
            "delivery": dict(envelope.delivery if isinstance(envelope.delivery, dict) else {}),
            "timeout_seconds": payload.get("timeout_seconds"),
        },
        _delegate_context(envelope, context or {}),
    )
    if isinstance(result, dict) and result.get("status") == "ok":
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        delegate = _delegate_summary(data, payload, envelope)
        authority_approval = _find_authority_approval(data)
        if authority_approval is not None:
            assistant_text = _authority_approval_text(authority_approval)
            delegate["code"] = "authority_approval_required"
            delegate["error"] = assistant_text
            delegate["approval_required"] = True
            return {
                "status": "authority_approval_required",
                "assistant_text": assistant_text,
                "code": "authority_approval_required",
                "error": assistant_text,
                "approval_required": True,
                "requires_approval": True,
                "finish_reason": "authority_approval_required",
                "delegate": delegate,
                "result": _safe_authority_approval_result(data, authority_approval, assistant_text),
            }
        if _delegate_failed(data):
            code, assistant_text = _delegate_failure_summary(data)
            delegate["code"] = code
            delegate["error"] = assistant_text
            return {
                "status": "error",
                "assistant_text": assistant_text,
                "code": code,
                "error": assistant_text,
                "delegate": delegate,
                "result": _safe_failed_delegate_result(data, assistant_text, code),
            }
        return {
            "status": "ok",
            "assistant_text": "",
            "delegate": delegate,
            "result": data,
        }
    error_data = result.get("error") if isinstance(result, dict) and isinstance(result.get("error"), dict) else {}
    assistant_text = "The delegated agent could not start."
    return {
        "status": "error",
        "assistant_text": assistant_text,
        "code": str(error_data.get("code") or "INPUT_ACTION_FAILED"),
        "error": assistant_text,
    }


def _delegate_payload(envelope: RumiInputEnvelope) -> dict[str, Any]:
    payload = envelope.params.get("delegate") if isinstance(envelope.params.get("delegate"), dict) else {}
    if payload:
        return dict(payload)
    return dict(envelope.params if isinstance(envelope.params, dict) else {})


def _delegate_context(envelope: RumiInputEnvelope, context: dict[str, Any]) -> dict[str, Any]:
    updated = dict(context or {})
    target = envelope.target if isinstance(envelope.target, dict) else {}
    if target.get("conversation_id"):
        updated.setdefault("conversation_id", str(target.get("conversation_id")))
    payload = _delegate_payload(envelope)
    metadata = envelope.metadata if isinstance(envelope.metadata, dict) else {}
    for source in (payload.get("params") if isinstance(payload.get("params"), dict) else {}, payload, metadata, target):
        for key in _DELEGATE_RUNTIME_CONTEXT_KEYS:
            value = source.get(key) if isinstance(source, dict) else None
            if value in ("", None, [], {}):
                continue
            updated.setdefault(key, value)
    if isinstance(context, dict):
        for key in ("profile_id", "principal_id", "authority_principal_id"):
            if context.get(key) not in ("", None, [], {}):
                updated[key] = context.get(key)
        profile_id = str(context.get("profile_id") or "").strip()
        principal_id = str(context.get("principal_id") or context.get("authority_principal_id") or "").strip()
        if principal_id:
            updated.setdefault("principal_id", principal_id)
        if profile_id and not principal_id:
            updated["principal_id"] = "profile:" + profile_id
        # Placement identity is accepted only from the trusted dispatcher
        # context. Client payloads cannot select or forge an Effective Plan.
        for key in _TRUSTED_PLACEMENT_CONTEXT_KEYS:
            value = context.get(key)
            if value not in ("", None, [], {}):
                updated[key] = value
        parent_run_id = str(
            context.get("agent_run_id")
            or context.get("run_id")
            or context.get("parent_run_id")
            or ""
        ).strip()
        if parent_run_id:
            updated.setdefault("parent_run_id", parent_run_id)
            updated.setdefault(
                "root_run_id",
                str(context.get("root_run_id") or parent_run_id),
            )
            updated.setdefault(
                "root_scope_id",
                str(
                    context.get("root_scope_id")
                    or context.get("root_run_id")
                    or parent_run_id
                ),
            )
    if metadata:
        updated.setdefault("delegate_metadata", dict(metadata))
    if target:
        updated.setdefault("target", dict(target))
    if isinstance(envelope.delivery, dict) and envelope.delivery:
        updated.setdefault("delivery", dict(envelope.delivery))
    if isinstance(envelope.attachments, list) and envelope.attachments:
        updated.setdefault("attachments", list(envelope.attachments))
    required_capabilities = (
        payload.get("required_capabilities")
        or payload.get("capability")
        or envelope.params.get("required_capabilities")
        or envelope.params.get("capability")
    )
    if required_capabilities:
        updated.setdefault("required_capabilities", required_capabilities)
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    if params:
        updated.setdefault("params", dict(params))
    return updated


def _delegate_summary(data: dict[str, Any], payload: dict[str, Any], envelope: RumiInputEnvelope) -> dict[str, Any]:
    return {
        "execution_id": data.get("execution_id"),
        "status": data.get("status"),
        "required_capabilities": payload.get("required_capabilities") or payload.get("capability"),
        "tools": payload.get("tools") if isinstance(payload.get("tools"), list) else envelope.tools,
    }


def _delegate_failed(data: dict[str, Any]) -> bool:
    return bool(_delegate_failure_status(data))


def _delegate_failure_status(data: dict[str, Any]) -> str:
    status = str(data.get("status") or "").strip().lower()
    if status in _FAILED_DELEGATE_STATUSES:
        return status
    nested = data.get("result") if isinstance(data.get("result"), dict) else {}
    nested_status = str(nested.get("status") or "").strip().lower()
    return nested_status if nested_status in _FAILED_DELEGATE_STATUSES else ""


def _find_authority_approval(value: Any, *, depth: int = 0) -> dict[str, Any] | None:
    if depth > 5:
        return None
    if isinstance(value, dict):
        authority = value.get("authority")
        if isinstance(authority, dict):
            found = _find_authority_approval(authority, depth=depth + 1)
            if found is not None:
                return found
        status = str(value.get("status") or value.get("finish_reason") or "").strip().lower()
        code = str(value.get("code") or "").strip().lower()
        if (
            status in _AUTHORITY_APPROVAL_STATUSES
            or code == "authority_approval_required"
            or (value.get("approval_required") is True and value.get("authority") is True)
        ):
            approval = dict(value)
            approval.setdefault("status", "authority_approval_required")
            approval.setdefault("approval_required", True)
            approval.setdefault("requires_approval", True)
            approval.setdefault("finish_reason", "authority_approval_required")
            return approval
        for item in value.values():
            found = _find_authority_approval(item, depth=depth + 1)
            if found is not None:
                return found
    if isinstance(value, list):
        for item in value[:20]:
            found = _find_authority_approval(item, depth=depth + 1)
            if found is not None:
                return found
    return None


def _authority_approval_text(approval: dict[str, Any]) -> str:
    return str(
        approval.get("message")
        or approval.get("display_summary")
        or approval.get("reason")
        or "The delegated agent needs authority approval before it can continue."
    )


def _safe_authority_approval_result(data: dict[str, Any], approval: dict[str, Any], assistant_text: str) -> dict[str, Any]:
    safe = {
        "status": "authority_approval_required",
        "code": "authority_approval_required",
        "error": assistant_text,
        "approval_required": True,
        "requires_approval": True,
        "finish_reason": "authority_approval_required",
    }
    execution_id = _execution_id_from_delegate_data(data)
    if execution_id:
        safe["execution_id"] = execution_id
    for key in ("request_id", "approval_request_id", "permission_id", "principal_id", "risk_level", "resource"):
        value = approval.get(key)
        if value not in ("", None, [], {}):
            safe[key] = value
    return safe


def _delegate_failure_summary(data: dict[str, Any]) -> tuple[str, str]:
    if _contains_provider_error_hint(data):
        return (
            "DELEGATE_PROVIDER_ERROR",
            "The delegated agent could not complete because the model provider returned an error.",
        )
    return (
        "DELEGATE_RUN_FAILED",
        "The delegated agent could not complete before producing a response.",
    )


def _safe_failed_delegate_result(data: dict[str, Any], assistant_text: str, code: str) -> dict[str, Any]:
    safe: dict[str, Any] = {
        "status": _delegate_failure_status(data) or str(data.get("status") or "error"),
        "code": code,
        "error": assistant_text,
        "error_redacted": True,
    }
    execution_id = _execution_id_from_delegate_data(data)
    if execution_id:
        safe["execution_id"] = execution_id
    return safe


def _execution_id_from_delegate_data(data: dict[str, Any]) -> str:
    execution_id = str(data.get("execution_id") or "").strip()
    if execution_id:
        return execution_id
    nested = data.get("result") if isinstance(data.get("result"), dict) else {}
    return str(nested.get("execution_id") or "").strip()


def _contains_provider_error_hint(value: Any, *, depth: int = 0) -> bool:
    if depth > 4:
        return False
    if isinstance(value, str):
        lowered = value.lower()
        return any(hint in lowered for hint in _PROVIDER_ERROR_HINTS)
    if isinstance(value, dict):
        return any(_contains_provider_error_hint(item, depth=depth + 1) for item in value.values())
    if isinstance(value, list):
        return any(_contains_provider_error_hint(item, depth=depth + 1) for item in value[:20])
    return False
