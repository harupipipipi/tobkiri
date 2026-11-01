"""Finite sandbox adapter for a captured Host-owned MCP connection.

The staged module uses only the standard library. It owns no transport, secret,
session, or approval; the authenticated guest supervisor seals its continuation.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from typing import Any, Callable, Mapping

CONTRACT_ID = "tobkiri.service.mcp.tool.call.v1"
OPERATION_ID = "rumi_mcp_gateway_pack.mcp-tool-call"
_TARGET = {
    "contract_id": "tobkiri.service.mcp.connection.v1",
    "operation_id": "mcp.connection.call",
}
_PROTOCOL = "io.tobkiri.packvm.bridge.v1"
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > 64 * 1024:
        raise ValueError("MCP gateway request is too large")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _request(payload: Mapping[str, Any]) -> dict[str, Any]:
    if set(payload) != {"connection_id", "tool", "arguments"}:
        raise ValueError("MCP gateway input fields are invalid")
    if any(
        not isinstance(payload[field], str) or not _NAME.fullmatch(payload[field])
        for field in ("connection_id", "tool")
    ) or not isinstance(payload["arguments"], dict):
        raise ValueError("MCP gateway connection or tool is invalid")
    result = dict(payload)
    _digest(result)
    return result


def create_call_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Keep the injected-client entrypoint without ambient connection lookup."""

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name != "call":
            raise ValueError("unknown MCP gateway operation")
        result = client.invoke(
            _TARGET["contract_id"], _TARGET["operation_id"], _request(payload),
        )
        if not isinstance(result, Mapping):
            raise RuntimeError("MCP owner returned an invalid result")
        return dict(result)

    return operation


def tobkiri_packvm_invoke(
    operation_id: str, payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Request one Host call or return its supervisor-validated outcome.

    The guest supervisor owns signature verification, deadline, request binding
    and one-use continuation state. A connection identifier grants no authority.
    """
    if operation_id != OPERATION_ID or not isinstance(payload, Mapping):
        raise ValueError("MCP gateway operation is invalid")
    if set(payload) == {"continuation", "bridge_result"}:
        continuation = payload["continuation"]
        result = payload["bridge_result"]
        if not isinstance(continuation, dict) or not isinstance(result, dict):
            raise ValueError("MCP gateway continuation is invalid")
        expected = {
            "protocol": _PROTOCOL, "version": 1,
            "operation_id": OPERATION_ID, "target": _TARGET,
        }
        if (
            continuation.get("kind") != "tobkiri.packvm.continuation.v1"
            or result.get("kind") != "tobkiri.packvm.bridge.result.v1"
            or any(continuation.get(key) != value or result.get(key) != value
                   for key, value in expected.items())
            or result.get("nonce") != continuation.get("nonce")
            or result.get("request_digest") != continuation.get("request_digest")
        ):
            raise ValueError("MCP gateway result binding is invalid")
        outcome = result.get("result")
        if not isinstance(outcome, dict):
            raise ValueError("MCP gateway outcome is invalid")
        if outcome.get("status") == "ok" and isinstance(outcome.get("value"), dict):
            return dict(outcome["value"])
        if outcome.get("status") == "error":
            return {"is_error": True, "result": "MCP tool is unavailable"}
        raise ValueError("MCP gateway outcome is invalid")
    request = _request(payload)
    continuation = {
        "kind": "tobkiri.packvm.continuation.v1", "protocol": _PROTOCOL,
        "version": 1, "operation_id": OPERATION_ID,
        "nonce": secrets.token_hex(24), "target": dict(_TARGET),
        "request_digest": _digest(request),
    }
    return {
        "kind": "tobkiri.packvm.bridge.request.v1", "protocol": _PROTOCOL,
        "version": 1, "target": dict(_TARGET), "request": request,
        "request_digest": continuation["request_digest"],
        "continuation": continuation,
    }
