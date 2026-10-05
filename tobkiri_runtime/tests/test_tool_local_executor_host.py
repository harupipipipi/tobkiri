"""Captured local dispatch refuses forged data and never invokes legacy code."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from core_runtime.host_provider_backend_v4 import HostProviderCaptureContextV4
from ecosystem.rumi_tool_local_executor_pack.runtime import executor
from tests.test_mcp_connection_owner import _Invocation
from tobkiri_host.models import OpaqueAuthorityRef
from tobkiri_protocol.canonical import canonical_digest


@pytest.fixture
def local_host(tmp_path):
    factory = executor.HOST_PROVIDER_FACTORY
    binding = SimpleNamespace(
        function=SimpleNamespace(function_id=factory.function_id, implementation_digest="impl"),
        operation=SimpleNamespace(
            contract_id=executor.CONTRACT, operation_id=executor.OPERATION,
            contract_version="1.0.0",
        ),
        principal_ref=OpaqueAuthorityRef("local-executor"),
        artifact=SimpleNamespace(digest="artifact"),
    )
    context = HostProviderCaptureContextV4(
        profile_id="defaults", plan_digest="plan", security_epoch=1,
        activation={"activation_id": "active"}, state_root=tmp_path,
        provider_bindings=(binding,), catalog_bindings=(),
        domain_ids={(executor.CONTRACT, executor.OPERATION, "local-executor"): "domain"},
    )
    captured = factory.capture(context)
    client = _Client()

    def invoke(payload=None):
        payload = payload if payload is not None else {
            "tool_id": "sample", "tool_call_id": "call-1", "arguments": {"value": 7},
            "definition": deepcopy(client.definition),
        }
        invocation = _Invocation(executor.OPERATION, payload)
        invocation.envelope.context.activation_digest = canonical_digest(dict(context.activation))
        invocation.envelope = replace(
            invocation.envelope, contract_id=executor.CONTRACT,
            target_principal=binding.principal_ref,
        )
        client.invocation = invocation

        def contract_client(**kwargs):
            assert kwargs == {
                "allowed_contract_ids": frozenset({executor.DEFINITION, executor.LOCAL_OPERATION}),
                "consumer_pack_id": executor.PACK_ID, "include_credentials": False,
            }
            return client

        invocation.contract_client = contract_client
        return captured.contributions[0].invoke(executor.OPERATION, payload, invocation)

    yield invoke, client, captured
    captured.close()


class _Client:
    def __init__(self):
        self.definition = {
            "tool_id": "sample", "input_schema": {"enum": [True]},
            "execution": {
                "kind": "local", "contract_id": executor.LOCAL_OPERATION,
                "provider_instance_id": "owner.tool-adapter", "operation": "owner.invoke",
            },
        }
        self.metadata = [{
            "function_id": "owner.tool-adapter", "provider_instance_id": "tool-adapter",
            "operation_id": "owner.invoke", "backend_id": "test", "implementation_digest": "impl",
        }]
        self.calls = []
        self.found = True
        self.cancel_after_read = False
        self.failure = None

    def providers(self, contract):
        assert contract == executor.LOCAL_OPERATION
        return self.metadata

    def invoke(self, contract, operation, payload, **kwargs):
        self.calls.append((contract, operation, payload, kwargs))
        if contract == executor.DEFINITION:
            assert operation == "rumi_tool_registry_pack.tool-definition-resource"
            assert payload == {"operation": "resolve", "tool_id": "sample"}
            if self.cancel_after_read:
                self.invocation.stale = True
            return {"found": self.found, "resolved_tool_id": "sample", "definition": self.definition}
        assert contract == executor.LOCAL_OPERATION
        if self.failure:
            raise self.failure
        return {"result": payload["arguments"]["value"]}


@pytest.mark.parametrize("provider", ["owner.tool-adapter", "tool-adapter"])
def test_local_target_uses_exact_captured_function_or_instance_identity(local_host, provider):
    invoke, client, _ = local_host
    client.definition["execution"]["provider_instance_id"] = provider
    assert invoke() == {"result": 7}
    assert client.calls[-1] == (
        executor.LOCAL_OPERATION, "owner.invoke",
        {"tool_id": "sample", "tool_call_id": "call-1", "arguments": {"value": 7}},
        {"provider_instance_id": "tool-adapter"},
    )


@pytest.mark.parametrize("field", [
    "approved", "approval_token", "caller_id", "profile_id", "deadline",
    "cancelled", "_contract_consumer_pack_id",
])
def test_local_executor_rejects_serialized_authority_before_owner_read(local_host, field):
    invoke, client, _ = local_host
    with pytest.raises(ValueError, match="payload"):
        invoke({
            "tool_id": "sample", "tool_call_id": "call-1", "arguments": {},
            "definition": client.definition, field: True,
        })
    assert client.calls == []


@pytest.mark.parametrize("change", ["missing", "target", "boolean"])
def test_local_definition_must_still_match_what_the_broker_validated(local_host, change):
    invoke, client, _ = local_host
    supplied = deepcopy(client.definition)
    if change == "missing":
        client.found = False
    elif change == "target":
        client.definition["execution"]["operation"] = "owner.changed"
    else:
        client.definition["input_schema"]["enum"] = [1]
    with pytest.raises(PermissionError, match="definition changed"):
        invoke({"tool_id": "sample", "tool_call_id": "call-1", "arguments": {}, "definition": supplied})
    assert len(client.calls) == 1


@pytest.mark.parametrize("change", ["missing", "duplicate", "operation", "provider", "backend"])
def test_local_target_is_unavailable_without_one_exact_executable_route(local_host, change):
    invoke, client, _ = local_host
    if change == "missing":
        client.metadata.clear()
    elif change == "duplicate":
        client.metadata *= 2
    else:
        key = {"operation": "operation_id", "provider": "function_id", "backend": "backend_id"}[change]
        client.metadata[0][key] = "foreign" if change != "backend" else None
    with pytest.raises(PermissionError, match="local tool operation is unavailable"):
        invoke()
    assert len(client.calls) == 1


def test_local_denial_propagates_once_and_cancellation_prevents_dispatch(local_host):
    invoke, client, _ = local_host
    client.failure = PermissionError("target denied by Host")
    with pytest.raises(PermissionError, match="target denied"):
        invoke()
    assert len(client.calls) == 2
    client.calls.clear()
    client.cancel_after_read = True
    with pytest.raises(PermissionError, match="stale"):
        invoke()
    assert len(client.calls) == 1


def test_closed_local_capture_cannot_read_or_execute(local_host):
    invoke, client, captured = local_host
    captured.close()
    with pytest.raises(PermissionError, match="binding changed"):
        invoke()
    assert client.calls == []
