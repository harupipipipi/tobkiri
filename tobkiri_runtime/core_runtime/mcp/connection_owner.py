"""Session-owned MCP connections behind an authenticated Host invocation.

This is the resource owner, not an authorization issuer. Its caller must be a
verified Host provider entered through the Broker. Production factory/catalog
wiring is separate; a saved legacy server registration never authorizes a
connection in this owner.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Any, Mapping

from core_runtime.host_provider_backend_v4 import HostProviderInvocationContextV4
from core_runtime.mcp.transport import McpConnections


CONTRACT_ID = "tobkiri.service.mcp.connection.v1"
CONNECT = "mcp.connection.connect"
CALL = "mcp.connection.call"
DISCONNECT = "mcp.connection.disconnect"
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")
_MAX_CONNECTIONS = 8
_MAX_PAYLOAD_BYTES = 64 * 1024


@dataclass
class _Connection:
    owner: tuple[str, str]
    server_id: str
    allowed_tools: frozenset[str]
    ready: bool = False


class CapturedMcpConnectionOwner:
    """Own transport/configuration lifetimes for one captured Profile plan.

    Connection identifiers do not confer permission: each operation checks the
    Host-preserved originating principal/session as well as the captured plan.
    A new approval is needed to reconnect; no method retries a remote effect.
    """

    def __init__(
        self,
        *,
        profile_id: str,
        activation_id: str,
        plan_digest: str,
        security_epoch: int,
        principal_id: str,
        consumer_pack_id: str,
        workspace_root: Path,
    ) -> None:
        if (
            not all((profile_id, activation_id, plan_digest, principal_id, consumer_pack_id))
            or type(security_epoch) is not int
        ):
            raise ValueError("MCP capture is incomplete")
        if security_epoch <= 0:
            raise ValueError("MCP capture epoch is invalid")
        root = workspace_root.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("MCP workspace is unavailable")
        self._capture = (profile_id, activation_id, plan_digest, security_epoch)
        self._principal_id = principal_id
        self._consumer_pack_id = consumer_pack_id
        self._workspace_root = root
        self._connections = McpConnections()
        self._records: dict[str, _Connection] = {}
        self._lock = threading.Lock()
        self._closed = False

    def invoke(
        self,
        invocation: HostProviderInvocationContextV4,
    ) -> Mapping[str, Any]:
        """Execute only the exact payload already admitted by the Broker."""
        invocation.assert_current()
        envelope = invocation.envelope
        context = envelope.context
        if (
            envelope.contract_id != CONTRACT_ID
            or envelope.contract_version != "1.0.0"
            or envelope.target_principal.value != self._principal_id
            or (
                context.profile_id,
                context.activation_id,
                context.plan_digest,
                context.security_epoch,
            )
            != self._capture
        ):
            raise PermissionError("MCP captured invocation does not match")
        owner = (
            invocation.presentation_owner_principal_id,
            invocation.presentation_owner_session_id,
        )
        if not all(isinstance(value, str) and value for value in owner):
            raise PermissionError("MCP originating session is unavailable")
        # Authenticate this provider's identity without acquiring ambient
        # credentials or an unrestricted contract client.
        invocation.contract_client(
            allowed_contract_ids=frozenset(),
            consumer_pack_id=self._consumer_pack_id,
            include_credentials=False,
        )
        payload = _payload(envelope.payload)
        operation = envelope.operation_id
        fields = {
            CONNECT: {"server_id", "config", "allowed_tools"},
            CALL: {"connection_id", "tool", "arguments"},
            DISCONNECT: {"connection_id"},
        }.get(operation)
        if fields is None or set(payload) != fields:
            raise ValueError("MCP operation payload is invalid")
        self._check_current(invocation)
        if operation == CONNECT:
            return self._connect(payload, owner, invocation)
        connection_id = _text(payload["connection_id"])
        with self._lock:
            record = self._records.get(connection_id)
            if record is None or record.owner != owner:
                raise PermissionError("MCP connection is unavailable")
            if operation == DISCONNECT:
                record.ready = False
            elif not record.ready:
                raise PermissionError("MCP connection is unavailable")
        if operation == DISCONNECT:
            self._connections.disconnect(connection_id)
            with self._lock:
                self._records.pop(connection_id, None)
            return {"connection_id": connection_id, "disconnected": True}
        tool = _text(payload["tool"])
        if tool not in record.allowed_tools:
            raise PermissionError("MCP tool is outside the connection scope")
        if not isinstance(payload["arguments"], dict):
            raise ValueError("MCP arguments must be an object")
        self._check_current(invocation)
        try:
            result = self._connections.invoke(
                connection_id,
                tool,
                payload["arguments"],
                deadline=envelope.deadline_monotonic,
                cancellation=envelope.cancellation_requested,
            )
            self._check_current(invocation)
        except Exception:
            # A cancelled or stale request may have reached the server. Fence
            # this connection, stop it, and never reconnect/replay the effect.
            with self._lock:
                record.ready = False
            try:
                self._connections.disconnect(connection_id)
            except Exception:
                raise RuntimeError("MCP connection cleanup is incomplete") from None
            with self._lock:
                self._records.pop(connection_id, None)
            raise
        return result

    def close(self) -> None:
        """Fence new operations and retain ownership until cleanup succeeds."""
        with self._lock:
            self._closed = True
            for record in self._records.values():
                record.ready = False
        self._connections.close()
        with self._lock:
            self._records.clear()

    def _check_current(self, invocation: HostProviderInvocationContextV4) -> None:
        invocation.assert_current()
        envelope = invocation.envelope
        if envelope.cancellation_requested.is_set():
            raise InterruptedError("MCP request was cancelled")
        if time.monotonic() >= envelope.deadline_monotonic:
            raise TimeoutError("MCP request deadline elapsed")
        with self._lock:
            if self._closed:
                raise PermissionError("MCP connection owner is closed")

    def _connect(
        self,
        payload: Mapping[str, Any],
        owner: tuple[str, str],
        invocation: HostProviderInvocationContextV4,
    ) -> Mapping[str, Any]:
        server_id = _text(payload["server_id"])
        tools = payload["allowed_tools"]
        if not isinstance(tools, list) or not 0 < len(tools) <= 64:
            raise ValueError("MCP tool scope must be a finite list")
        allowed_tools = frozenset(_text(tool) for tool in tools)
        if len(allowed_tools) != len(tools):
            raise ValueError("MCP tool scope contains duplicates")
        config = self._config(payload["config"])
        connection_id = "mcp_" + secrets.token_hex(16)
        record = _Connection(owner, server_id, allowed_tools)
        with self._lock:
            if self._closed or len(self._records) >= _MAX_CONNECTIONS:
                raise PermissionError("MCP connection capacity is unavailable")
            # Publish ownership before the first operation which can spawn.
            self._records[connection_id] = record
        envelope = invocation.envelope
        try:
            self._check_current(invocation)
            self._connections.connect(
                connection_id,
                config,
                deadline=envelope.deadline_monotonic,
                cancellation=envelope.cancellation_requested,
                inherit_environment=False,
            )
            discovered = self._connections.get_server_tools(connection_id)
            names = {tool.get("name") for tool in discovered if isinstance(tool, dict)}
            if not allowed_tools <= names:
                raise PermissionError("MCP approved tool is unavailable")
            self._check_current(invocation)
            with self._lock:
                if self._closed:
                    raise PermissionError("MCP connection owner is closed")
                record.ready = True
        except Exception:
            try:
                self._connections.disconnect(connection_id)
            except Exception:
                # Keep both owner records so close() can retry collection.
                raise RuntimeError("MCP connection cleanup is incomplete") from None
            with self._lock:
                self._records.pop(connection_id, None)
            raise RuntimeError("MCP connection failed") from None
        return {
            "connection_id": connection_id,
            "server_id": server_id,
            "tools": sorted(allowed_tools),
            "connected": True,
        }

    def _config(self, value: object) -> dict[str, Any]:
        # The initial owner uses explicit stdio snapshots. SSE is deliberately
        # unavailable here until in-flight POST cancellation/ownership is wired.
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "transport",
                "command",
                "env",
                "cwd",
            }
            or value["transport"] != "stdio"
        ):
            raise ValueError("MCP connection configuration is invalid")
        command = value["command"]
        env = value["env"]
        if not isinstance(command, list) or not 0 < len(command) <= 256:
            raise ValueError("MCP command must be a finite argument list")
        if any(not isinstance(arg, str) or "\0" in arg for arg in command):
            raise ValueError("MCP command argument is invalid")
        if not Path(command[0]).is_absolute():
            raise ValueError("MCP executable must be explicit")
        if not isinstance(env, dict) or any(
            not isinstance(key, str)
            or not key
            or "=" in key
            or "\0" in key
            or not isinstance(item, str)
            or "\0" in item
            for key, item in env.items()
        ):
            raise ValueError("MCP environment is invalid")
        if not isinstance(value["cwd"], str):
            raise ValueError("MCP workspace path is invalid")
        relative_cwd = Path(value["cwd"])
        if relative_cwd.is_absolute():
            raise PermissionError("MCP workspace path must be relative")
        cwd = (self._workspace_root / relative_cwd).resolve(strict=True)
        if not cwd.is_relative_to(self._workspace_root) or not cwd.is_dir():
            raise PermissionError("MCP workspace path is outside the capture")
        return {**value, "cwd": str(cwd)}


def _text(value: object) -> str:
    if not isinstance(value, str) or _NAME.fullmatch(value) is None:
        raise ValueError("MCP identifier is invalid")
    return value


def _payload(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        wire = json.dumps(dict(value), allow_nan=False)
        if len(wire.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
            raise ValueError("MCP payload exceeds limit")
        return json.loads(wire)
    except (TypeError, ValueError, RecursionError):
        raise ValueError("MCP payload is invalid") from None
