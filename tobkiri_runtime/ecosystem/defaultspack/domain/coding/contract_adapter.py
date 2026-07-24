"""Finite defaultspack compatibility adapter for Wave 8 coding contracts."""

from __future__ import annotations

from typing import Any, Mapping

from core_runtime.di_container import get_container
from core_runtime.global_contract_dispatch import invoke_global_contract
from core_runtime.resolved_profile_scope import active_resolved_profile
from domain.safety import approval
from domain.tool_policy.internal_context import (
    tool_server_approval_context_is_internal,
)


FILE_INSPECT = "rumi.service.file.inspect.v1"
FILE_MUTATE = "rumi.service.file.mutate.v1"
FILE_PATCH = "rumi.service.file.patch.v1"
SHELL_INSPECT = "rumi.service.shell.inspect.v1"
SHELL_EXECUTE = "rumi.service.shell.execute.v1"
TERMINAL_RESOURCE = "rumi.resource.terminal.session.v1"
TERMINAL_CONTROL = "rumi.action.terminal.session.v1"
SANDBOX_OBSERVE = "rumi.resource.coding.sandbox.v1"
SANDBOX_CONTROL = "rumi.action.coding.sandbox.v1"
WORKSPACE_RESOURCE = "rumi.resource.workspace.v1"
WORKSPACE_ACTION = "rumi.action.workspace.mount.v1"
GIT_READ = "rumi.service.git.read.v1"
GIT_WRITE = "rumi.service.git.write.v1"
GIT_PUBLISH = "rumi.service.git.publish.v1"
HOST_AUTHORITY = "rumi.service.host.authorize.v1"


def invoke_coding_contract(
    contract_id: str,
    operation: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Invoke exactly one selected coding provider for the active profile."""

    registry = get_container().get_or_none("interface_registry")
    plan = active_resolved_profile()
    if registry is None or plan is None:
        raise RuntimeError("global coding provider is unavailable")
    request = {
        "profile_id": plan.profile_id,
        **dict(payload),
        "_contract_consumer_pack_id": "defaultspack",
    }
    result = invoke_global_contract(registry, contract_id, operation, request)
    if not isinstance(result, dict):
        raise RuntimeError("coding provider returned an invalid result")
    return result


def workspace_id(input_data: Mapping[str, Any]) -> str:
    """Require the canonical workspace identifier; never infer a root path."""

    value = str(input_data.get("workspace_id") or "").strip()
    if not value:
        raise ValueError("workspace_id is required")
    return value


def authorize_legacy_coding_operation(
    *,
    legacy_operation: str,
    service_pack_id: str,
    service_operation: str,
    authority: str,
    arguments: Mapping[str, Any],
    input_data: Mapping[str, Any],
    context: Mapping[str, Any] | None,
    selected_workspace_id: str,
    allow_without_approval: bool = False,
) -> dict[str, Any]:
    """Consume one legacy approval and mint one exact service receipt."""

    request = dict(input_data)
    internal = tool_server_approval_context_is_internal(
        dict(context) if isinstance(context, Mapping) else None
    )
    verification = None
    if not internal and not allow_without_approval:
        token = _approval_token(request)
        if not token:
            return {"authorized": False, "reason": "approval_required"}
        verification = approval.verify_execution_token(
            token,
            legacy_operation,
            approval.hash_arguments(request),
            consume=True,
        )
        if not verification.valid:
            return {
                "authorized": False,
                "reason": "approval_invalid",
                "code": verification.code or "APPROVAL_INVALID",
                "message": verification.message or "approval token is invalid",
            }
    ctx = dict(context) if isinstance(context, Mapping) else {}
    caller_id = str(
        ctx.get("principal_id")
        or ctx.get("user_id")
        or "defaultspack.local_user"
    )
    scope = {
        "service_pack_id": service_pack_id,
        "operation": service_operation,
        "authority": authority,
        "caller_id": caller_id,
        "caller_pack_id": "defaultspack",
        "caller_function_id": legacy_operation,
        "profile_id": _profile_id(),
        "workspace_id": selected_workspace_id,
        "session_id": str(ctx.get("session_id") or ctx.get("conversation_id") or ""),
        "arguments": dict(arguments),
        "approval_required": False,
    }
    issued = invoke_coding_contract(HOST_AUTHORITY, "authorize", scope)
    if not issued.get("authorized"):
        return issued
    return {**issued, **scope, "approval_request_id": getattr(verification, "request_id", "")}


def service_payload(
    authorization: Mapping[str, Any],
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind an issued receipt and its exact scope to a service request."""

    return {
        **dict(arguments),
        "authority_receipt": str(authorization.get("receipt") or ""),
        "caller_id": str(authorization.get("caller_id") or ""),
        "caller_pack_id": "defaultspack",
        "caller_function_id": str(authorization.get("caller_function_id") or ""),
        "workspace_id": str(authorization.get("workspace_id") or ""),
        "session_id": str(authorization.get("session_id") or ""),
    }


def _approval_token(input_data: Mapping[str, Any]) -> str:
    token = str(input_data.get("approval_token") or "").strip()
    if token:
        return token
    headers = input_data.get("_headers")
    if isinstance(headers, Mapping):
        return str(
            headers.get("X-Rumi-Approval")
            or headers.get("x-rumi-approval")
            or ""
        ).strip()
    return ""


def _profile_id() -> str:
    plan = active_resolved_profile()
    if plan is None:
        raise RuntimeError("resolved profile is unavailable")
    return plan.profile_id
