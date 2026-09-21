"""Finite MCP continuation validation and one-use guest supervisor state."""

import copy

import pytest

from ecosystem.defaultspack.backend.sandbox.isolation.resources import packvm_guest_runner as runner
from ecosystem.rumi_mcp_gateway_pack.runtime import gateway
from tests.test_packvm_guest_bridge_runner import (
    _FakeSigner, _config, _digest, _envelope, _guest_artifact_identity,
    _host_result, _invoke_payload, _roundtrip,
)


def _call():
    return {"connection_id": "mcp_fixture", "tool": "ping", "arguments": {"value": 42}}


@pytest.mark.parametrize("change", ["target", "operation", "digest", "arguments", "approved"])
def test_mcp_gateway_bridge_rejects_changed_request_before_host_dispatch(change):
    bridge = gateway.tobkiri_packvm_invoke(gateway.OPERATION_ID, _call())
    assert runner._validate_bridge_request(bridge, operation_id=gateway.OPERATION_ID) == bridge
    if change == "target":
        bridge["target"] = dict(runner.PACKVM_BRIDGE_TARGET)
    elif change == "operation":
        bridge["continuation"]["operation_id"] = "complete"
    elif change == "digest":
        bridge["request_digest"] = "sha256:" + "0" * 64
    elif change == "arguments":
        bridge["request"]["arguments"]["value"] = 43
    else:
        bridge["request"]["approved"] = True
    with pytest.raises(ValueError):
        runner._validate_bridge_request(bridge, operation_id=gateway.OPERATION_ID)


def test_mcp_request_cannot_be_returned_by_another_outer_operation():
    bridge = gateway.tobkiri_packvm_invoke(gateway.OPERATION_ID, _call())
    with pytest.raises(ValueError, match="outer operation changed"):
        runner._validate_bridge_request(bridge, operation_id="complete")


@pytest.mark.parametrize("payload", [
    {"continuation": {}, "bridge_result": {}},
    {**_call(), "profile_id": "foreign"},
    {**_call(), "arguments": {"number": 2**60}},
])
def test_initial_mcp_input_is_rejected_before_artifact_access(monkeypatch, payload):
    def forbidden(*args):
        pytest.fail("invalid MCP input must not inspect an artifact")

    monkeypatch.setattr(runner, "_verify_invocation_artifact", forbidden)
    request = _invoke_payload("mcp")
    request.update(contract_id=runner.PACKVM_MCP_CONTRACT,
                   operation_id=runner.PACKVM_MCP_OPERATION, payload=payload)
    with pytest.raises(ValueError):
        runner._invoke(request)


def test_mcp_guest_supervisor_consumes_one_result_and_preserves_deadline(monkeypatch):
    config, signer = _config(), _FakeSigner()
    ledger = runner._PendingBridgeLedger()
    request = _invoke_payload("mcp", config)
    request.update(contract_id=runner.PACKVM_MCP_CONTRACT,
                   operation_id=runner.PACKVM_MCP_OPERATION, payload=_call())
    steps = []

    def execute(captured, arguments, deadline, **kwargs):
        steps.append((copy.deepcopy(arguments), deadline))
        return {"ok": True, "protocol": runner.PROTOCOL,
                "guest_artifact_identity": _guest_artifact_identity(config),
                "payload": runner._host_invoke_result(gateway.tobkiri_packvm_invoke(
                    captured["operation_id"], arguments,
                ))}

    def initial(captured, *, guest_deadline):
        return execute(captured, captured["payload"], guest_deadline)

    monkeypatch.setattr(runner, "_invoke", initial)
    monkeypatch.setattr(runner, "_execute_invocation_step", execute)
    pending = _roundtrip(_envelope(config, "invoke", "mcp", "0" * 64, payload=request),
                         config, ledger, signer)
    assert pending["success"] is True
    bridge = pending["data"]["host_bridge_request"]["bridge_request"]
    result = {**bridge["continuation"], "kind": runner.PACKVM_BRIDGE_RESULT_KIND,
              "result": {"status": "ok", "value": {"is_error": False, "result": "ok"}}}
    result["result_digest"] = _digest(result["result"])
    host_result = _host_result(config, "mcp", bridge)
    host_result.update(continuation_nonce=bridge["continuation"]["nonce"],
                       bridge_result=result, bridge_result_digest=_digest(result))
    completed = _roundtrip(_envelope(config, "bridge_result", "mcp", "1" * 64,
                                     host_bridge_result=host_result), config, ledger, signer)
    assert completed["success"] is True, completed
    assert completed["data"]["outcome"] == {"is_error": False, "result": "ok"}
    assert len(steps) == 2 and steps[0][1] == steps[1][1]
    replay = _roundtrip(_envelope(config, "bridge_result", "mcp", "2" * 64,
                                  host_bridge_result=host_result), config, ledger, signer)
    assert replay["success"] is False
    assert len(steps) == 2
