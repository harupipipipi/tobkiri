"""Captured factory and real-child lifecycle; workspace contract is a double."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from core_runtime.host_provider_backend_v4 import HostProviderCaptureContextV4
from core_runtime.mcp.connection_owner import CALL, CONNECT, CONTRACT_ID, DISCONNECT, LIST, PREPARE
from ecosystem.tobkiri_mcp_connection_pack.runtime import host
from tests.test_mcp_connection_owner import _Invocation
from tests.test_mcp_connection_owner import connection_request as connection_request
from tobkiri_host.models import OpaqueAuthorityRef


class _Workspace:
    def __init__(self, root):
        self.mount = {"id": "workspace", "root_path": str(root), "mount_revision": 1}
        self.reads = []

    def invoke(self, contract_id, operation_id, payload):
        assert contract_id == "tobkiri.resource.workspace.v1"
        assert operation_id == "rumi_workspace_mount_pack.workspace-resource"
        assert payload["profile_id"] == "defaults"
        self.reads.append(dict(payload))
        if payload["operation"] == "list":
            return {"profile_id": "defaults", "selected_workspace_id": "workspace"}
        if self.mount is None:
            raise PermissionError("unmounted")
        return dict(self.mount)


@pytest.fixture
def captured(tmp_path):
    bindings = tuple(
        SimpleNamespace(
            function=SimpleNamespace(function_id=host.FUNCTION_ID, implementation_digest="impl"),
            operation=SimpleNamespace(
                contract_id=CONTRACT_ID,
                contract_version="1.0.0",
                operation_id=operation,
            ),
            artifact=SimpleNamespace(digest="artifact"),
            principal_ref=OpaqueAuthorityRef("gateway"),
        )
        for operation in (PREPARE, CONNECT, CALL, DISCONNECT, LIST)
    )
    context = HostProviderCaptureContextV4(
        profile_id="defaults",
        plan_digest="plan",
        security_epoch=1,
        activation={"activation_id": "active"},
        state_root=tmp_path,
        provider_bindings=bindings,
        catalog_bindings=bindings,
        domain_ids={(CONTRACT_ID, b.operation.operation_id, "gateway"): "domain" for b in bindings},
    )
    provider = host.HOST_PROVIDER_FACTORY.capture(context)
    workspace = _Workspace(tmp_path)

    def invoke(operation, payload, *, session="origin-session", context_change=None):
        invocation = _Invocation(operation, payload)
        invocation.presentation_owner_session_id = session
        if context_change:
            invocation.envelope = replace(invocation.envelope, **context_change)

        def contract_client(**kwargs):
            assert kwargs["consumer_pack_id"] == host.PACK_ID
            assert kwargs["include_credentials"] is False
            assert kwargs["allowed_contract_ids"] in (
                frozenset(),
                frozenset({"tobkiri.resource.workspace.v1"}),
            )
            return workspace

        invocation.contract_client = contract_client
        contribution = next(
            item for item in provider.contributions if item.operation_id == operation
        )
        return contribution.invoke(operation, payload, invocation)

    yield provider, workspace, invoke
    provider.close()


def _connect(invoke, connection_request):
    plan = invoke(PREPARE, connection_request)
    return invoke(CONNECT, {"request": connection_request, "plan": plan})["connection_id"]


def test_factory_preserves_owner_across_operations_and_disconnect_after_unmount(
    captured, connection_request
):
    provider, workspace, invoke = captured
    connection_id = _connect(invoke, connection_request)
    service = provider.contributions[0].invoke.__self__
    child = (
        service._owners["workspace"]
        .connections._connections._servers[connection_id]
        ._transport._proc
    )
    assert invoke(LIST, {})["connections"][0]["connection_id"] == connection_id
    assert invoke(LIST, {}, session="foreign") == {"connections": []}
    reads = len(workspace.reads)
    with pytest.raises(PermissionError):
        invoke(
            CALL,
            {"connection_id": connection_id, "tool": "ping", "arguments": {}},
            session="foreign",
        )
    assert len(workspace.reads) == reads and child.poll() is None
    result = invoke(CALL, {"connection_id": connection_id, "tool": "ping", "arguments": {}})
    assert result["is_error"] is False
    workspace.mount = None
    invoke(DISCONNECT, {"connection_id": connection_id})
    assert child.poll() is not None
    assert invoke(LIST, {}) == {"connections": []}


@pytest.mark.parametrize("change", ["revision", "root", "unmount", "boolean_revision"])
def test_changed_workspace_closes_owned_child_before_tool_call(
    captured, connection_request, tmp_path, change
):
    provider, workspace, invoke = captured
    connection_id = _connect(invoke, connection_request)
    service = provider.contributions[0].invoke.__self__
    child = (
        service._owners["workspace"]
        .connections._connections._servers[connection_id]
        ._transport._proc
    )
    if change == "revision":
        workspace.mount["mount_revision"] = 2
    elif change == "boolean_revision":
        workspace.mount["mount_revision"] = True
    elif change == "root":
        new_root = tmp_path / "new"
        new_root.mkdir()
        workspace.mount["root_path"] = str(new_root)
    else:
        workspace.mount = None
    with pytest.raises(PermissionError):
        invoke(CALL, {"connection_id": connection_id, "tool": "ping", "arguments": {}})
    assert child.poll() is not None
    assert not (tmp_path / "calls.jsonl").exists()
    assert not service._owners and not service._routes


def test_foreign_capture_fails_before_workspace_access(captured, connection_request):
    _, workspace, invoke = captured
    with pytest.raises(PermissionError):
        invoke(
            PREPARE,
            connection_request,
            context_change={"target_principal": OpaqueAuthorityRef("foreign")},
        )
    assert workspace.reads == []


def test_capture_retains_failed_cleanup_and_retries(captured, connection_request, monkeypatch):
    provider, _, invoke = captured
    connection_id = _connect(invoke, connection_request)
    service = provider.contributions[0].invoke.__self__
    owner = service._owners["workspace"].connections
    original = owner.close

    def fail():
        raise RuntimeError("fixture collection failure")

    monkeypatch.setattr(owner, "close", fail)
    with pytest.raises(RuntimeError, match="cleanup"):
        provider.close()
    assert connection_id in service._routes and service._owners
    with pytest.raises(PermissionError):
        invoke(LIST, {})
    monkeypatch.setattr(owner, "close", original)
    provider.close()
    assert not service._owners and not service._routes
