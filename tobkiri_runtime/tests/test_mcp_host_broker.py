"""Real Authority/Broker approval and MCP child under an isolated Profile.

The Profile and packaged Shell artifact are test fixtures. This is not native,
external-provider, process-containment, or ordinary Defaults UI acceptance.
"""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from core_runtime.authority.ui_operator import sign_ui_operator
from core_runtime.authority.v4 import AuthorityDenied
from core_runtime.mcp.connection_owner import CALL, CONNECT, CONTRACT_ID, DISCONNECT, LIST, PREPARE
from ecosystem.rumi_workspace_mount_pack.runtime.mounts import WorkspaceMountStore
from tests.test_mcp_connection_owner import connection_request as connection_request
from tests.test_production_frontend_contract_http import _ShellPolicyPackVmBackend
from ecosystem.defaultspack.backend.sandbox.isolation.resources import packvm_guest_runner as runner
from tobkiri_host.effects import ProviderOutcome
from tobkiri_host.errors import ProviderExecutionError, ResolutionError


_EFFECT = "tobkiri.service.interactive-effect.v1"
_APPROVAL = "tobkiri.service.interactive-approval.v1"
_COORDINATOR = "rumi_host_authority_bridge_pack.host-authority.interactive-effect"
_OWNER = {op: op + ".service" for op in (PREPARE, CONNECT, CALL, LIST, DISCONNECT)}
_RUNTIME_ROOT = Path(__file__).resolve().parents[1]
_GATEWAY = "rumi_mcp_gateway_pack.mcp-gateway.call"
_EXECUTOR = "rumi_tool_mcp_executor_pack.tool-executor.mcp"
_EXECUTE = "rumi_tool_mcp_executor_pack.tool-mcp-execute"
pytestmark = pytest.mark.skipif(os.name != "posix", reason="staged child fixture requires POSIX")


class _McpGatewayBackend(_ShellPolicyPackVmBackend):
    """Test transport: execute verified bytes in fresh isolated Python children.

    The production Broker/Host bridge and Pack entrypoint are real. This adapter
    does not claim the VM, attested channel, or full OS sandbox acceptance.
    """

    _PACK_ID = "rumi_mcp_gateway_pack"
    _FUNCTION_ID = _GATEWAY
    _CONTRACT_ID = runner.PACKVM_MCP_CONTRACT
    _OPERATION_ID = runner.PACKVM_MCP_OPERATION

    def __init__(self, root):
        super().__init__()
        self._staged = root / "gateway.py"
        self._bridge = None
        self._execute_abi = self._child

    def bind_capability_bridge(self, callback):
        assert self._bridge is None
        self._bridge = callback

    def materialize(self, binding, reservation_id):
        evidence = super().materialize(binding, reservation_id)
        artifact = self._artifact_resolver(binding)
        contents = next(item.content for item in artifact.files
                        if item.path == artifact.implementation_path)
        if self._staged.exists():
            self._staged.chmod(0o600)
        self._staged.write_bytes(contents)
        self._staged.chmod(0o400)
        return evidence

    def _child(self, operation_id, payload):
        child = subprocess.run(
            [sys.executable, "-I", "-S", runner.__file__, "--execute", str(self._staged)],
            input=json.dumps({"contract_id": self._CONTRACT_ID,
                              "operation_id": operation_id, "payload": payload}),
            text=True, capture_output=True, timeout=5, check=True,
        )
        return json.loads(child.stdout)

    def invoke(self, request):
        bridge = runner._validate_bridge_request(
            dict(super().invoke(request).payload), operation_id=request.operation_id,
        )
        response = self._bridge(request, bridge)
        checked = runner._validate_bridge_result(response, bridge["continuation"])
        terminal = self._child(request.operation_id, {
            "continuation": bridge["continuation"], "bridge_result": checked,
        })
        assert terminal["kind"] == runner.PACKVM_INVOKE_RESULT_KIND
        return ProviderOutcome(terminal["outcome"])


def _edge(caller, provider, contract, operation, mode="profile_grant"):
    return {
        "caller_function_id": caller,
        "target_provider_id": provider,
        "contract_id": contract,
        "operation_id": operation,
        "authority_mode": mode,
        "requested_scope_template": {
            "capability": "operation.invoke",
            "dimensions": {"contract": [contract], "operation": [operation]},
            "quotas": {},
            "exact_request_digest": None,
            "opaque": False,
        },
    }


@pytest.fixture
def mcp_session(tmp_path, monkeypatch):
    from tests.conformance_support.host_profile import captured_host_profile

    edges = (
        [
            _edge("shell.tauri.default", _GATEWAY,
                  runner.PACKVM_MCP_CONTRACT, runner.PACKVM_MCP_OPERATION),
            _edge("shell.tauri.default", _EXECUTOR, "tobkiri.service.tool.execute.v1", _EXECUTE),
            _edge(_EXECUTOR, _GATEWAY, runner.PACKVM_MCP_CONTRACT, runner.PACKVM_MCP_OPERATION),
            _edge(_GATEWAY, _OWNER[CALL], CONTRACT_ID, CALL),
            _edge(_COORDINATOR, _OWNER[PREPARE], CONTRACT_ID, PREPARE),
            _edge(_COORDINATOR, _OWNER[CONNECT], CONTRACT_ID, CONNECT, "interactive_only"),
            _edge("shell.tauri.default", _OWNER[LIST], CONTRACT_ID, LIST),
            _edge("shell.tauri.default", _OWNER[CALL], CONTRACT_ID, CALL),
            _edge("shell.tauri.default", _OWNER[DISCONNECT], CONTRACT_ID, DISCONNECT),
            _edge(
                _OWNER[PREPARE],
                "rumi_workspace_mount_pack.workspace-mount.resource",
                "tobkiri.resource.workspace.v1",
                "rumi_workspace_mount_pack.workspace-resource",
            ),
            _edge(
                _OWNER[CONNECT],
                "rumi_workspace_mount_pack.workspace-mount.resource",
                "tobkiri.resource.workspace.v1",
                "rumi_workspace_mount_pack.workspace-resource",
            ),
            _edge(
                _OWNER[CALL],
                "rumi_workspace_mount_pack.workspace-mount.resource",
                "tobkiri.resource.workspace.v1",
                "rumi_workspace_mount_pack.workspace-resource",
            ),
        ]
    )
    with captured_host_profile(
        tmp_path, monkeypatch,
        packs=("tobkiri_mcp_connection_pack", "rumi_mcp_gateway_pack", "rumi_tool_mcp_executor_pack"),
        edges=edges, backends=(_McpGatewayBackend(tmp_path),),
    ) as (session, store):
        mounts = WorkspaceMountStore("defaults", user_data_root=tmp_path / "user-data")
        mounted = mounts.mount("workspace", str(tmp_path), expected_revision=0)
        mounts.select("workspace", expected_revision=mounted["revision"])
        yield session, store


def test_real_broker_approves_one_owned_mcp_start_and_rejects_foreign_resume(
    mcp_session,
    connection_request,
    tmp_path,
):
    session, authority = mcp_session

    def invoke(contract, operation, payload, *, owner="mcp-owner-session"):
        return session.invoke(contract, operation, {**payload, "_session_id": owner})

    pending = invoke(
        _EFFECT,
        "interactive_effect.manage",
        {
            "phase": "prepare",
            "effect_kind": "mcp_connect",
            "request": connection_request,
        },
    )
    assert pending["state"] == "approval_pending"
    assert invoke(CONTRACT_ID, LIST, {}) == {"connections": []}
    resume = {"phase": "resume", "effect_id": pending["effect_id"]}
    assert invoke(_EFFECT, "interactive_effect.manage", resume)["state"] == "approval_pending"
    with pytest.raises(ProviderExecutionError, match="provider execution failed"):
        invoke(_EFFECT, "interactive_effect.manage", resume, owner="foreign-session")
    # An identifier or a request-shaped payload does not grant the execute edge.
    with pytest.raises(AuthorityDenied, match="captured Shell caller edge"):
        invoke(CONTRACT_ID, CONNECT, {"request": connection_request, "plan": {}})
    approval_id = pending["approval_request_id"]
    approval = invoke(_APPROVAL, "interactive_approval.get", {"request_id": approval_id})
    invoke(
        _APPROVAL,
        "interactive_approval.approve",
        {
            "request_id": approval_id,
            "confirmation_text": "EXECUTE",
            "ui_operator": sign_ui_operator(
                approval_id,
                nonce="mcp-connection-approval",
                decision="approve",
                request_snapshot_digest=approval["request_snapshot_digest"],
                typed_confirmation_digest=approval["typed_confirmation_digest"],
            ),
        },
    )
    assert invoke(_EFFECT, "interactive_effect.manage", resume)["state"] == "succeeded"
    connections = invoke(CONTRACT_ID, LIST, {})["connections"]
    assert len(connections) == 1 and connections[0]["status"] == "connected"
    assert invoke(CONTRACT_ID, LIST, {}, owner="foreign-session") == {"connections": []}
    assert invoke(_EFFECT, "interactive_effect.manage", resume)["state"] == "succeeded"
    assert invoke(CONTRACT_ID, LIST, {})["connections"] == connections
    call = {
        "connection_id": connections[0]["connection_id"],
        "tool": "ping",
        "arguments": {"value": 42},
    }
    result = invoke(CONTRACT_ID, CALL, call)
    assert result["is_error"] is False
    assert json.loads(result["result"])["arguments"] == {"value": 42}
    calls = tmp_path / "calls.jsonl"
    assert len(calls.read_text().splitlines()) == 1
    for payload, owner in (
        (call, "foreign-session"),
        ({**call, "tool": "blocked"}, "mcp-owner-session"),
    ):
        with pytest.raises(ProviderExecutionError, match="provider execution failed"):
            invoke(CONTRACT_ID, CALL, payload, owner=owner)
        assert len(calls.read_text().splitlines()) == 1
    with pytest.raises(ResolutionError, match="input schema validation failed"):
        invoke(CONTRACT_ID, CALL, {**call, "approved": True})
    assert len(calls.read_text().splitlines()) == 1
    assert invoke(CONTRACT_ID, LIST, {})["connections"] == connections
    for count, (contract, operation) in enumerate((
        (runner.PACKVM_MCP_CONTRACT, runner.PACKVM_MCP_OPERATION),
        ("tobkiri.service.tool.execute.v1", _EXECUTE),
    ), start=2):
        nested = invoke(contract, operation, call)
        assert nested["is_error"] is False, nested
        assert json.loads(nested["result"])["arguments"] == {"value": 42}
        assert len(calls.read_text().splitlines()) == count
        denied = invoke(contract, operation, call, owner="foreign-session")
        assert denied["is_error"] is True
        assert len(calls.read_text().splitlines()) == count
    invoke(CONTRACT_ID, DISCONNECT, {"connection_id": connections[0]["connection_id"]})
    assert invoke(CONTRACT_ID, LIST, {}) == {"connections": []}
    assert "committed" in {event["event_state"] for event in authority.audit_events()}
