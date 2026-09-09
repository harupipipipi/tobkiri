"""Captured MCP process owner; application gateways receive only contracts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import threading
from typing import Any, Mapping

from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)
from core_runtime.mcp.connection_owner import (
    CALL,
    CONNECT,
    CONTRACT_ID,
    DISCONNECT,
    LIST,
    PREPARE,
    CapturedMcpConnectionOwner,
)


PACK_ID = "tobkiri_mcp_connection_pack"
FUNCTION_ID = "tobkiri.mcp.connections"
_OPERATIONS = frozenset({PREPARE, CONNECT, CALL, DISCONNECT, LIST})
_WORKSPACE_CONTRACT = "tobkiri.resource.workspace.v1"
_WORKSPACE_OPERATION = "rumi_workspace_mount_pack.workspace-resource"
_CAPACITY = 8


@dataclass
class _WorkspaceOwner:
    workspace: dict[str, Any]
    connections: CapturedMcpConnectionOwner


class _CapturedConnections:
    """Route only to owners built from a captured workspace resource binding."""

    def __init__(self, context: HostProviderCaptureContextV4) -> None:
        self.context = context
        self._owners: dict[str, _WorkspaceOwner] = {}
        self._routes: dict[str, tuple[_WorkspaceOwner, tuple[str, str]]] = {}
        # Serialize startup admission, not tool calls on separate connections.
        self._starts = threading.RLock()
        self._lock = threading.RLock()
        self._closed = False

    def invoke(
        self,
        operation: str,
        payload: Mapping[str, Any],
        invocation: HostProviderInvocationContextV4,
    ) -> Mapping[str, Any]:
        invocation.assert_current()
        actual = invocation.envelope.context
        expected = self.context
        if (actual.profile_id, actual.activation_id, actual.plan_digest, actual.security_epoch) != (
            expected.profile_id,
            expected.activation["activation_id"],
            expected.plan_digest,
            expected.security_epoch,
        ) or invocation.envelope.target_principal != expected.provider_bindings[0].principal_ref:
            raise PermissionError("MCP capture binding changed")
        if (
            operation not in _OPERATIONS
            or operation != invocation.envelope.operation_id
            or dict(payload) != dict(invocation.envelope.payload)
        ):
            raise PermissionError("MCP operation binding changed")
        with self._lock:
            if self._closed:
                raise PermissionError("MCP capture is closed")
        if operation == LIST:
            if payload:
                raise ValueError("MCP list payload is invalid")
            with self._lock:
                owners = tuple(self._owners.values())
            connections = []
            for entry in owners:
                connections.extend(entry.connections.invoke(invocation)["connections"])
            return {"connections": connections}
        client = invocation.contract_client(
            allowed_contract_ids=frozenset({_WORKSPACE_CONTRACT}),
            consumer_pack_id=PACK_ID,
            include_credentials=False,
        )
        if operation in {PREPARE, CONNECT}:
            with self._starts:
                workspace = _workspace(client, self.context.profile_id)
                owner = self._owner(workspace)
                if (
                    operation == CONNECT
                    and sum(
                        len(entry.connections.connection_ids) for entry in self._owners.values()
                    )
                    >= _CAPACITY
                ):
                    raise PermissionError("MCP connection capacity is unavailable")
                result = owner.connections.invoke(invocation)
                if operation == CONNECT:
                    with self._lock:
                        self._routes[str(result["connection_id"])] = (owner, _origin(invocation))
                return result
        connection_id = payload.get("connection_id")
        if not isinstance(connection_id, str):
            raise ValueError("MCP connection is invalid")
        with self._lock:
            route = self._routes.get(connection_id)
        if route is None or route[1] != _origin(invocation):
            raise PermissionError("MCP connection is unavailable")
        owner = route[0]
        # Disconnect must remain available when a workspace has been unmounted.
        # It still passes the core owner's session/Profile checks.
        if operation == CALL:
            try:
                current = _workspace(
                    client, self.context.profile_id, workspace_id=owner.workspace["id"]
                )
                if current != owner.workspace:
                    raise PermissionError("MCP workspace binding changed")
            except Exception:
                with self._starts, self._lock:
                    self._release_owner(owner)
                raise PermissionError("MCP workspace is unavailable") from None
        try:
            return owner.connections.invoke(invocation)
        finally:
            with self._lock:
                if connection_id not in owner.connections.connection_ids:
                    self._routes.pop(connection_id, None)

    def _owner(self, workspace: dict[str, Any]) -> _WorkspaceOwner:
        with self._lock:
            if self._closed:
                raise PermissionError("MCP capture is closed")
            existing = self._owners.get(workspace["id"])
            if existing is not None and existing.workspace == workspace:
                return existing
            if existing is not None:
                self._release_owner(existing)
            if len(self._owners) >= _CAPACITY:
                unused = next(
                    (
                        key
                        for key, value in self._owners.items()
                        if not value.connections.connection_ids
                    ),
                    None,
                )
                if unused is None:
                    raise PermissionError("MCP workspace capacity is unavailable")
                self._release_owner(self._owners[unused])
            context = self.context
            owner = _WorkspaceOwner(
                workspace,
                CapturedMcpConnectionOwner(
                    profile_id=context.profile_id,
                    activation_id=str(context.activation["activation_id"]),
                    plan_digest=context.plan_digest,
                    security_epoch=context.security_epoch,
                    principal_id=context.provider_bindings[0].principal_ref.value,
                    consumer_pack_id=PACK_ID,
                    workspace_root=Path(workspace["root"]),
                    workspace_id=workspace["id"],
                    workspace_revision=workspace["revision"],
                ),
            )
            self._owners[workspace["id"]] = owner
            return owner

    def _release_owner(self, owner: _WorkspaceOwner) -> None:
        owner.connections.close()
        self._owners = {key: value for key, value in self._owners.items() if value is not owner}
        self._routes = {key: value for key, value in self._routes.items() if value[0] is not owner}

    def close(self) -> None:
        """Retain every failed owner until all its resources are collected."""
        with self._starts, self._lock:
            self._closed = True
            failed = False
            for owner in list(self._owners.values()):
                try:
                    self._release_owner(owner)
                except Exception:
                    failed = True
            if failed:
                raise RuntimeError("MCP capture cleanup is incomplete")


def _origin(invocation: HostProviderInvocationContextV4) -> tuple[str, str]:
    return (invocation.presentation_owner_principal_id, invocation.presentation_owner_session_id)


def _workspace(client: Any, profile_id: str, *, workspace_id: str | None = None) -> dict[str, Any]:
    if workspace_id is None:
        snapshot = client.invoke(
            _WORKSPACE_CONTRACT,
            _WORKSPACE_OPERATION,
            {"operation": "list", "profile_id": profile_id},
        )
        if not isinstance(snapshot, Mapping) or snapshot.get("profile_id") != profile_id:
            raise PermissionError("MCP workspace Profile is unavailable")
        workspace_id = snapshot.get("selected_workspace_id")
    if not isinstance(workspace_id, str) or not workspace_id:
        raise PermissionError("MCP selected workspace is unavailable")
    mount = client.invoke(
        _WORKSPACE_CONTRACT,
        _WORKSPACE_OPERATION,
        {
            "operation": "get",
            "profile_id": profile_id,
            "workspace_id": workspace_id,
        },
    )
    if not isinstance(mount, Mapping) or mount.get("id") != workspace_id:
        raise PermissionError("MCP workspace mount is unavailable")
    root = mount.get("root_path")
    revision = mount.get("mount_revision")
    if (
        not isinstance(root, str)
        or not root
        or type(revision) not in {str, int}
        or isinstance(revision, str)
        and not 0 < len(revision) <= 256
        or isinstance(revision, int)
        and revision < 0
    ):
        raise PermissionError("MCP workspace mount is invalid")
    path = Path(root)
    if not path.is_absolute() or path.is_symlink():
        raise PermissionError("MCP workspace root is invalid")
    path = path.resolve(strict=True)
    metadata = path.stat()
    if not path.is_dir():
        raise PermissionError("MCP workspace root is unavailable")
    return {
        "id": workspace_id,
        "revision": revision,
        "root": str(path),
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
    }


class McpConnectionsHostFactoryV4:
    """Contribute one finite lifecycle Function under exact Host verification."""

    function_id = FUNCTION_ID

    def capture(self, context: HostProviderCaptureContextV4) -> CapturedHostProviderV4:
        """Own one captured Profile's connections and close them at deactivation."""
        bindings = context.provider_bindings
        if (
            not bindings
            or not context.activation.get("activation_id")
            or any(
                binding.function.function_id != FUNCTION_ID
                or binding.operation.contract_id != CONTRACT_ID
                or binding.operation.operation_id not in _OPERATIONS
                for binding in bindings
            )
        ):
            raise PermissionError("MCP Host bindings are incomplete")
        service = _CapturedConnections(context)
        contributions = []
        for binding in bindings:
            key = (CONTRACT_ID, binding.operation.operation_id, binding.principal_ref.value)
            if key not in context.domain_ids:
                raise PermissionError("MCP Host domain is unavailable")
            contributions.append(
                HostProviderContributionV4(
                    contract_id=CONTRACT_ID,
                    contract_version=binding.operation.contract_version,
                    operation_id=binding.operation.operation_id,
                    principal_id=binding.principal_ref.value,
                    artifact_digest=binding.artifact.digest,
                    implementation_digest=binding.function.implementation_digest,
                    domain_id=context.domain_ids[key],
                    invoke=service.invoke,
                )
            )
        return CapturedHostProviderV4(tuple(contributions), service.close)


HOST_PROVIDER_FACTORY = McpConnectionsHostFactoryV4()
