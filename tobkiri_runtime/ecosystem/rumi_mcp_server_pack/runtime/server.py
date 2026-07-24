"""Project global definitions and invocations to an MCP server boundary."""

from __future__ import annotations

import re
from typing import Any, Callable, Mapping

from core_runtime.global_contract_dispatch import GlobalContractClient

DEFINITIONS = "rumi.resource.tool.definition.v1"
INVOKE = "rumi.service.tool.invoke.v1"
_TOOL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")


def create_catalog_operation(
    client: GlobalContractClient,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Create a read-only MCP schema projection of global definitions."""

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name not in {"list", "catalog"}:
            raise ValueError(f"unknown MCP server catalog operation: {name}")
        profile_id = str(payload.get("profile_id") or "").strip()
        if not profile_id:
            raise ValueError("MCP server profile binding is required")
        snapshot = client.invoke(
            DEFINITIONS, "list", {"profile_id": profile_id}
        )
        definitions = (
            snapshot.get("definitions") if isinstance(snapshot, Mapping) else []
        )
        return {
            "tools": [
                {
                    "name": f"rumi.{item.get('tool_id')}",
                    "description": str(item.get("description") or ""),
                    "inputSchema": dict(item.get("input_schema") or {}),
                }
                for item in definitions or []
                if isinstance(item, Mapping)
            ]
        }

    return operation


def create_call_operation(
    client: GlobalContractClient,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Create an MCP call adapter that retains global broker enforcement."""

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name != "call":
            raise ValueError(f"unknown MCP server call operation: {name}")
        principal = str(payload.get("authenticated_principal_id") or "").strip()
        profile_id = str(payload.get("profile_id") or "").strip()
        external_name = str(payload.get("name") or "").strip()
        tool_id = external_name.removeprefix("rumi.")
        arguments = payload.get("arguments")
        if (
            not principal
            or not profile_id
            or external_name == tool_id
            or not _TOOL_ID.fullmatch(tool_id)
            or not isinstance(arguments, Mapping)
        ):
            raise ValueError("MCP server call scope is invalid")
        result = client.invoke(
            INVOKE,
            "invoke",
            {
                "tool_id": tool_id,
                "tool_call_id": payload.get("tool_call_id"),
                "arguments": dict(arguments),
                "caller_id": f"mcp-server:{principal}",
                "profile_id": profile_id,
                "deadline": payload.get("deadline"),
                "cancelled": bool(payload.get("cancelled", False)),
                "approval_token": payload.get("approval_token"),
                "approval_request_id": payload.get("approval_request_id"),
            },
        )
        if not isinstance(result, Mapping):
            raise RuntimeError("global tool broker returned an invalid result")
        return dict(result)

    return operation

