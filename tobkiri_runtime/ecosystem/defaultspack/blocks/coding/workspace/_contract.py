"""Finite compatibility projection for the workspace mount owner."""

from __future__ import annotations

from typing import Any, Mapping

from blocks._common import error, ok
from blocks.coding._approval import approval_required
from blocks.coding._workspace import canonical_mutation_guard
from domain.coding.contract_adapter import (
    WORKSPACE_ACTION,
    WORKSPACE_RESOURCE,
    authorize_legacy_coding_operation,
    invoke_coding_contract,
    service_payload,
)


def snapshot() -> dict[str, Any]:
    """Read the authoritative profile workspace snapshot."""

    return invoke_coding_contract(WORKSPACE_RESOURCE, "list", {})


def get_mount(workspace_id: str) -> dict[str, Any] | None:
    """Read one authoritative mount by exact identifier."""

    return invoke_coding_contract(
        WORKSPACE_RESOURCE,
        "get",
        {"workspace_id": workspace_id},
    )


def mutate(
    *,
    input_data: Mapping[str, Any],
    context: Mapping[str, Any] | None,
    legacy_operation: str,
    action: str,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    """Authorize and apply one exact revision-bound workspace action."""

    authorization = authorize_legacy_coding_operation(
        legacy_operation=legacy_operation,
        service_pack_id="rumi_workspace_mount_pack",
        service_operation=f"workspace.{action}",
        authority="workspace.mount.manage",
        arguments=arguments,
        input_data=input_data,
        context=dict(context) if isinstance(context, Mapping) else None,
        selected_workspace_id=str(arguments.get("workspace_id") or ""),
        mutation_guard=canonical_mutation_guard,
    )
    if not authorization.get("authorized"):
        if authorization.get("reason") == "approval_required":
            return ok(
                approval_required(
                    legacy_operation,
                    "medium",
                    args=dict(input_data),
                )
            )
        return error(
            str(authorization.get("message") or authorization.get("reason")),
            code=str(authorization.get("code") or "APPROVAL_INVALID"),
        )
    result = invoke_coding_contract(
        WORKSPACE_ACTION,
        action,
        service_payload(authorization, arguments),
    )
    return ok(result)


def project_mount(value: Mapping[str, Any]) -> dict[str, Any]:
    """Project canonical mount metadata into the finite legacy response."""

    metadata = value.get("metadata")
    metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
    user_metadata = metadata.get("metadata")
    return {
        "workspace_id": str(value.get("id") or ""),
        "label": str(metadata.get("label") or value.get("id") or ""),
        "root_path": str(value.get("root_path") or ""),
        "trusted": bool(metadata.get("trusted", False)),
        "trust_granted_at": metadata.get("trust_granted_at"),
        "last_used_at": value.get("updated_at"),
        "metadata": dict(user_metadata) if isinstance(user_metadata, Mapping) else {},
    }


def project_result(response: dict[str, Any]) -> dict[str, Any]:
    """Project a successful workspace action response."""

    if response.get("status") != "ok":
        return response
    data = response.get("data")
    if not isinstance(data, Mapping) or not isinstance(data.get("mount"), Mapping):
        return response
    projected = {**dict(data), "workspace": project_mount(data["mount"])}
    projected.pop("mount", None)
    return ok(projected)
