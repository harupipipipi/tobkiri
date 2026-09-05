"""Finite compatibility helpers for the Wave 8 coding sandbox contracts."""

from __future__ import annotations

from typing import Any, Callable, Mapping

from blocks._common import error, ok
from blocks.coding._approval import approval_required
from blocks.coding._workspace import canonical_mutation_guard
from domain.coding.contract_adapter import (
    SANDBOX_CONTROL,
    SANDBOX_OBSERVE,
    authorize_legacy_coding_operation,
    invoke_coding_contract,
    service_payload,
)

_SERVICE_PACK_ID = "rumi_coding_sandbox_service_pack"
_AUTHORITY = "coding.sandbox.control"


def run_sandbox_observe(
    input_data: Mapping[str, Any] | None,
    context: Mapping[str, Any] | None,
    *,
    legacy_operation: str,
    observe_operation: str,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve or prepare one sandbox and perform an observe operation."""

    args = dict(input_data or {})
    try:
        scope = _sandbox_scope(args, context, legacy_operation, allow_prepare=True)
        if "response" in scope:
            return scope["response"]
        result = invoke_coding_contract(
            SANDBOX_OBSERVE,
            observe_operation,
            {
                "sandbox_id": scope["sandbox_id"],
                **dict(payload or {}),
            },
        )
        return ok({**result, "sandbox_only": True, "host_modified": False})
    except PermissionError as exc:
        return error(str(exc), code="PATH_RESTRICTED")
    except FileNotFoundError as exc:
        return error(str(exc), code="FILE_NOT_FOUND")
    except (KeyError, ValueError) as exc:
        return error(str(exc), code="INVALID_INPUT")
    except Exception as exc:
        return error(str(exc), code="SANDBOX_ERROR")


def run_sandbox_control(
    input_data: Mapping[str, Any] | None,
    context: Mapping[str, Any] | None,
    *,
    legacy_operation: str,
    control_operation: str,
    arguments: Callable[[str, str], Mapping[str, Any]],
) -> dict[str, Any]:
    """Resolve or prepare one sandbox and perform one receipt-gated control."""

    args = dict(input_data or {})
    try:
        scope = _sandbox_scope(args, context, legacy_operation, allow_prepare=False)
        if "response" in scope:
            return scope["response"]
        exact_arguments = dict(
            arguments(scope["sandbox_id"], scope["workspace_id"])
        )
        authorization = authorize_legacy_coding_operation(
            legacy_operation=legacy_operation,
            service_pack_id=_SERVICE_PACK_ID,
            service_operation=f"coding.sandbox.{control_operation}",
            authority=_AUTHORITY,
            arguments=exact_arguments,
            input_data=args,
            context=dict(context) if isinstance(context, Mapping) else None,
            selected_workspace_id=scope["workspace_id"],
            mutation_guard=canonical_mutation_guard,
        )
        denied = _authorization_response(
            authorization,
            legacy_operation,
            args,
        )
        if denied is not None:
            return denied
        result = invoke_coding_contract(
            SANDBOX_CONTROL,
            control_operation,
            service_payload(authorization, exact_arguments),
        )
        return ok({**result, "sandbox_only": True, "host_modified": False})
    except PermissionError as exc:
        return error(str(exc), code="PATH_RESTRICTED")
    except FileNotFoundError as exc:
        return error(str(exc), code="FILE_NOT_FOUND")
    except (KeyError, ValueError) as exc:
        return error(str(exc), code="INVALID_INPUT")
    except Exception as exc:
        return error(str(exc), code="SANDBOX_ERROR")


def _sandbox_scope(
    input_data: Mapping[str, Any],
    context: Mapping[str, Any] | None,
    legacy_operation: str,
    *,
    allow_prepare: bool,
) -> dict[str, Any]:
    sandbox_id = str(input_data.get("sandbox_id") or "").strip()
    if sandbox_id:
        observed = invoke_coding_contract(
            SANDBOX_OBSERVE,
            "get",
            {"sandbox_id": sandbox_id},
        )
        workspace_id = str(observed.get("workspace_id") or "").strip()
        supplied = str(input_data.get("workspace_id") or "").strip()
        if not workspace_id or (supplied and supplied != workspace_id):
            raise PermissionError("sandbox workspace scope does not match")
        return {"sandbox_id": sandbox_id, "workspace_id": workspace_id}

    if not allow_prepare:
        raise ValueError(
            "sandbox_id is required; prepare a sandbox with an observe action first"
        )
    workspace_id = str(input_data.get("workspace_id") or "").strip()
    if not workspace_id:
        raise ValueError("workspace_id or sandbox_id is required")
    prepare_arguments = {
        "workspace_id": workspace_id,
        "include_paths": _string_list(input_data.get("include_paths")),
    }
    authorization = authorize_legacy_coding_operation(
        legacy_operation=legacy_operation,
        service_pack_id=_SERVICE_PACK_ID,
        service_operation="coding.sandbox.prepare",
        authority=_AUTHORITY,
        arguments=prepare_arguments,
        input_data=input_data,
        context=dict(context) if isinstance(context, Mapping) else None,
        selected_workspace_id=workspace_id,
        mutation_guard=canonical_mutation_guard,
    )
    denied = _authorization_response(
        authorization,
        legacy_operation,
        input_data,
    )
    if denied is not None:
        return {"response": denied}
    prepared = invoke_coding_contract(
        SANDBOX_CONTROL,
        "prepare",
        service_payload(authorization, prepare_arguments),
    )
    sandbox_id = str(prepared.get("id") or "").strip()
    if not sandbox_id:
        raise RuntimeError("sandbox provider did not return an id")
    return {"sandbox_id": sandbox_id, "workspace_id": workspace_id}


def _authorization_response(
    authorization: Mapping[str, Any],
    legacy_operation: str,
    input_data: Mapping[str, Any],
) -> dict[str, Any] | None:
    if authorization.get("authorized"):
        return None
    if authorization.get("reason") == "approval_required":
        return ok(
            approval_required(
                legacy_operation,
                "medium",
                args=dict(input_data),
                sandbox_only=True,
                host_modified=False,
            )
        )
    return error(
        str(authorization.get("message") or authorization.get("reason")),
        code=str(authorization.get("code") or "APPROVAL_INVALID"),
    )


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError("include_paths must be a list")
    return [str(item) for item in value if str(item)]
