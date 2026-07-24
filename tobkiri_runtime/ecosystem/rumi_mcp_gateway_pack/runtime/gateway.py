"""Finite MCP execution adapter over the existing connection runtime."""

from __future__ import annotations

import re
from typing import Any, Callable, Mapping

from ecosystem.defaultspack.domain.tool.mcp_client import McpClient

_NAMESPACE = re.compile(r"^mcp\.([a-z0-9][a-z0-9._-]{0,127})$")
_OPERATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_EXPECTED_CONSUMER = "rumi_tool_mcp_executor_pack"


def create_call_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Create a gateway operation restricted to the MCP executor consumer."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name != "call":
            raise ValueError(f"unknown MCP gateway operation: {name}")
        consumer = str(payload.get("_contract_consumer_pack_id") or "")
        if consumer != _EXPECTED_CONSUMER:
            raise PermissionError("MCP call consumer is not authorized")
        namespace = str(payload.get("namespace") or "").strip()
        match = _NAMESPACE.fullmatch(namespace)
        remote_operation = str(payload.get("operation") or "").strip()
        caller_id = str(payload.get("caller_id") or "").strip()
        profile_id = str(payload.get("profile_id") or "").strip()
        arguments = payload.get("arguments")
        if (
            match is None
            or not _OPERATION.fullmatch(remote_operation)
            or not caller_id
            or not profile_id
            or not isinstance(arguments, Mapping)
        ):
            raise ValueError("MCP call scope is invalid")
        result = McpClient().invoke(
            match.group(1), remote_operation, dict(arguments)
        )
        if not isinstance(result, Mapping):
            raise RuntimeError("MCP client returned an invalid result")
        return dict(result)

    return operation
