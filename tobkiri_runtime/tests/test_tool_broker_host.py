"""Captured tool composition; these doubles do not claim native acceptance."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from core_runtime.host_provider_backend_v4 import HostProviderCaptureContextV4
from ecosystem.rumi_tool_broker_pack.runtime import broker
from ecosystem.rumi_tool_registry_pack.runtime.registry import _definition
from ecosystem.rumi_tool_result_pack.runtime.normalizer import create_normalize_operation
from ecosystem.rumi_tool_validation_pack.runtime.validator import create_validate_operation
from tests.test_mcp_connection_owner import _Invocation
from tobkiri_host.models import OpaqueAuthorityRef
from tobkiri_protocol.canonical import canonical_digest


@pytest.fixture
def tool_host(tmp_path):
    factory = broker.HOST_PROVIDER_FACTORY
    binding = SimpleNamespace(
        function=SimpleNamespace(function_id=factory.function_id, implementation_digest="impl"),
        operation=SimpleNamespace(
            contract_id=broker.CONTRACT, operation_id=broker.OPERATION, contract_version="1.0.0",
        ),
        principal_ref=OpaqueAuthorityRef("tool-broker"),
        artifact=SimpleNamespace(digest="artifact"),
    )
    context = HostProviderCaptureContextV4(
        profile_id="defaults", plan_digest="plan", security_epoch=1,
        activation={"activation_id": "active"}, state_root=tmp_path,
        provider_bindings=(binding,), catalog_bindings=(),
        domain_ids={(broker.CONTRACT, broker.OPERATION, "tool-broker"): "domain"},
    )
    provider = factory.capture(context)
    client = _Client()

    def invoke(payload=None, *, change=None, stale=False):
        payload = payload if payload is not None else {
            "tool_id": "alias", "tool_call_id": "call-1", "arguments": {"value": 3},
        }
        invocation = _Invocation(broker.OPERATION, payload)
        invocation.envelope.context.activation_digest = canonical_digest(dict(context.activation))
        invocation.envelope = replace(
            invocation.envelope, contract_id=broker.CONTRACT,
            target_principal=binding.principal_ref,
        )
        if change:
            invocation.envelope = replace(invocation.envelope, **change)
        invocation.stale = stale

        def contract_client(**kwargs):
            assert kwargs == {
                "allowed_contract_ids": frozenset({
                    broker.DEFINITION, broker.VALIDATE, broker.EXECUTE, broker.NORMALIZE,
                }),
                "consumer_pack_id": broker.PACK_ID, "include_credentials": False,
            }
            return client

        invocation.contract_client = contract_client
        return provider.contributions[0].invoke(broker.OPERATION, payload, invocation)

    yield invoke, client, provider, context
    provider.close()


class _Client:
    def __init__(self):
        self.calls = []
        self.selected = True
        self.failure = None
        self.execute_callback = None
        self.definition = _definition({
            "tool_id": "sample", "authority": "file.read",
            "input_schema": {
                "type": "object", "properties": {"value": {"type": "integer"}},
                "required": ["value"], "additionalProperties": False,
            },
            "execution": {"kind": "local", "contract_id": "tobkiri.service.tool.local.operation.v1"},
        })

    def providers(self, contract):
        assert contract == broker.EXECUTE
        return ({
            "function_id": "rumi_tool_local_executor_pack.tool-executor.local",
            "operation_id": "rumi_tool_local_executor_pack.tool-local-execute",
            "provider_instance_id": "tool-executor.local", "backend_id": "test",
            "implementation_digest": "selected-impl",
        },) if self.selected else ()

    def invoke(self, contract, operation, payload, **kwargs):
        self.calls.append((contract, operation, payload, kwargs))
        if contract == broker.DEFINITION:
            assert payload == {"operation": "resolve", "tool_id": "alias"}
            return {"found": True, "resolved_tool_id": "sample", "definition": self.definition}
        if contract == broker.VALIDATE:
            return create_validate_operation(None)("validate", payload)
        if contract == broker.EXECUTE:
            assert kwargs == {"provider_instance_id": "tool-executor.local"}
            if self.execute_callback:
                self.execute_callback()
            if self.failure:
                raise self.failure
            return {"result": {"value": payload["arguments"]["value"], "token": "private"}}
        assert contract == broker.NORMALIZE
        return create_normalize_operation(None)("normalize", payload)


def test_owner_resolved_arguments_reach_exact_executor_and_redacted_result(tool_host):
    invoke, client, _, _ = tool_host
    result = invoke()
    assert result["tool_id"] == "sample" and result["tool_call_id"] == "call-1"
    assert result["result"] == {"value": 3, "token": "[REDACTED]"}
    assert [item[0] for item in client.calls] == [
        broker.DEFINITION, broker.VALIDATE, broker.EXECUTE, broker.NORMALIZE,
    ]
    request = client.calls[2][2]
    assert set(request) == {"tool_id", "tool_call_id", "arguments", "definition"}
    assert result["executor"]["content_hash"] == "selected-impl"


def test_selected_definition_hash_is_rechecked_before_validation_or_execution(tool_host):
    invoke, client, _, _ = tool_host
    payload = {"tool_id": "alias", "tool_call_id": "call-1", "arguments": {"value": 3},
               "expected_definition_hash": client.definition["definition_hash"]}
    invoke(payload)
    client.calls.clear()
    client.definition = _definition({**client.definition, "description": "Changed after selection"})
    with pytest.raises(PermissionError):
        invoke(payload)
    assert [item[0] for item in client.calls] == [broker.DEFINITION]


@pytest.mark.parametrize("field", [
    "approved", "approval_token", "approval_request_id", "caller_id", "profile_id",
    "deadline", "cancelled", "definition", "provider_instance_id", "_contract_consumer_pack_id",
])
def test_serialized_authority_cannot_enter_tool_composition(tool_host, field):
    invoke, client, _, _ = tool_host
    with pytest.raises(ValueError, match="payload"):
        invoke({"tool_id": "alias", "tool_call_id": "call-1", "arguments": {}, field: True})
    assert client.calls == []


def test_authority_denial_is_not_retried_or_hidden_as_tool_success(tool_host):
    invoke, client, _, _ = tool_host
    client.failure = PermissionError("actual Host denial")
    with pytest.raises(PermissionError, match="actual Host denial"):
        invoke()
    assert sum(item[0] == broker.EXECUTE for item in client.calls) == 1
    assert all(item[0] != broker.NORMALIZE for item in client.calls)


def test_unknown_executor_has_no_legacy_fallback(tool_host):
    invoke, client, _, _ = tool_host
    client.selected = False
    with pytest.raises(PermissionError, match="executor is unavailable"):
        invoke()
    assert all(item[0] != broker.EXECUTE for item in client.calls)


def test_argument_validation_failure_precedes_executor(tool_host):
    invoke, client, _, _ = tool_host
    with pytest.raises(ValueError, match="arguments"):
        invoke({"tool_id": "alias", "tool_call_id": "call-1", "arguments": {"value": True}})
    assert all(item[0] != broker.EXECUTE for item in client.calls)


def test_recursive_tool_composition_fails_without_blocking_broker_workers(tool_host):
    invoke, client, _, _ = tool_host
    def nested():
        with pytest.raises(PermissionError, match="busy"):
            invoke()
    client.execute_callback = nested
    assert invoke()["status"] == "success"
    assert sum(item[0] == broker.EXECUTE for item in client.calls) == 1


@pytest.mark.parametrize("change", [
    {"contract_id": "foreign"}, {"contract_version": "2.0.0"},
    {"operation_id": "foreign"}, {"target_principal": OpaqueAuthorityRef("foreign")},
    {"target_domain": OpaqueAuthorityRef("foreign")},
    {"payload": {}},
])
def test_changed_host_envelope_never_enters_owner_code(tool_host, change):
    invoke, client, _, _ = tool_host
    with pytest.raises(PermissionError, match="binding changed"):
        invoke(change=change)
    assert client.calls == []


def test_closed_and_cancelled_invocations_do_not_enter_owner_code(tool_host):
    invoke, client, provider, _ = tool_host
    with pytest.raises(PermissionError, match="stale"):
        invoke(stale=True)
    provider.close()
    with pytest.raises(PermissionError, match="binding changed"):
        invoke()
    assert client.calls == []


@pytest.mark.parametrize("schema", [
    {"pattern": ".*"}, {"$ref": "https://example.test/schema"},
    {"anyOf": [{"type": "integer"}]}, {"type": "unknown"},
    {"properties": {"child": {"format": "email"}}}, {"required": "name"},
    {"items": False}, {"maxLength": True},
])
def test_unsupported_schema_constraints_fail_before_execution(schema):
    with pytest.raises(ValueError, match="schema"):
        create_validate_operation(None)("validate", {"schema": schema, "arguments": {}})


def test_boolean_enum_does_not_accept_integer_and_mcp_identity_is_preserved():
    validate = create_validate_operation(None)
    assert not validate("validate", {"schema": {"enum": [True]}, "arguments": 1})["valid"]
    source = {
        "tool_id": "mcp.ping", "authority": "mcp.invoke",
        "execution": {"kind": "mcp", "contract_id": "tobkiri.service.mcp.tool.call.v1",
                      "connection_id": "connection-1"},
    }
    assert _definition(source)["execution"]["connection_id"] == "connection-1"


@pytest.mark.parametrize("change", [
    {}, {"provider_instance_id": "foreign"}, {"contract_id": "foreign"},
    {"connection_id": ""}, {"namespace": "foreign"}, {"operation": ""},
])
def test_mcp_execution_keeps_captured_gateway_and_connection_identity(tool_host, change):
    _, _, _, context = tool_host
    gateway = SimpleNamespace(
        operation=SimpleNamespace(
            contract_id="tobkiri.service.mcp.tool.call.v1",
            operation_id="rumi_mcp_gateway_pack.mcp-tool-call",
        ),
        function=SimpleNamespace(function_id="rumi_mcp_gateway_pack.gateway"),
        artifact=SimpleNamespace(pack_id="rumi_mcp_gateway_pack"),
    )
    context = replace(context, catalog_bindings=(gateway,))
    execution = {
        "contract_id": "tobkiri.service.mcp.tool.call.v1", "provider_instance_id": "gateway",
        "namespace": "mcp.sample", "connection_id": "connection-1", "operation": "ping",
        **change,
    }
    if change:
        with pytest.raises(ValueError, match="descriptor"):
            broker._mcp_request(context, execution, {"value": 3})
    else:
        assert broker._mcp_request(context, execution, {"value": 3}) == {
            "connection_id": "connection-1", "tool": "ping", "arguments": {"value": 3},
        }
