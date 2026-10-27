"""Captured local dispatch refuses forged data and never invokes legacy code."""

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator, Mapping

import pytest

from core_runtime.host_provider_backend_v4 import HostProviderCaptureContextV4
from ecosystem.rumi_tool_local_executor_pack.runtime import executor
from tests.test_mcp_connection_owner import _Invocation
from tobkiri_host.models import OpaqueAuthorityRef
from tobkiri_protocol.canonical import canonical_digest


@pytest.fixture
def local_host(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[Any]:
    # These private ports exercise only the pure _Client routing fixture.
    # They neither mint authority nor prove production Saved/native admission.
    port_failure = getattr(request, "param", None)
    client = _Client()
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

    def assert_owned(invocation: Any) -> None:
        invocation.assert_current()
        assert invocation is client.invocation
        assert invocation.envelope.payload is client.payload
        assert binding.function.function_id == factory.function_id
        assert factory.function_id == f"{executor.PACK_ID}.tool-executor.local"
        assert invocation.envelope.target_principal == binding.principal_ref
        assert (invocation.envelope.contract_id, invocation.envelope.operation_id) == (
            executor.CONTRACT, executor.OPERATION,
        )

    def mode_admission(invocation: Any) -> str:
        assert_owned(invocation)
        client.mode_calls.append(invocation)
        if port_failure == "mode_denied":
            raise PermissionError("fixture mode admission denied")
        return "ask"

    def consent(
        invocation: Any, execution: Mapping[str, Any], payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        assert_owned(invocation)
        assert execution is client.definition["execution"]
        assert execution["contract_id"] == executor.LOCAL_OPERATION
        assert execution["operation"] == "owner.invoke"
        assert payload["arguments"] is client.payload["arguments"]
        assert payload == {
            key: client.payload[key]
            for key in ("tool_id", "tool_call_id", "arguments")
        }
        client.consent_calls.append(invocation)
        if port_failure == "consent_denied":
            raise PermissionError("fixture consent denied")
        return client.invoke(
            executor.LOCAL_OPERATION, "owner.invoke", payload,
            provider_instance_id="tool-adapter",
        )

    context = HostProviderCaptureContextV4(
        profile_id="defaults", plan_digest="plan", security_epoch=1,
        activation={"activation_id": "active"}, state_root=tmp_path,
        provider_bindings=(binding,), catalog_bindings=(),
        domain_ids={(executor.CONTRACT, executor.OPERATION, "local-executor"): "domain"},
        saved_tool_mode_admission=(
            None if port_failure == "mode_missing" else mode_admission
        ),
        saved_tool_consent_port=(
            None if port_failure == "consent_missing" else consent
        ),
    )
    captured = factory.capture(context)

    def invoke(payload: dict[str, Any] | None = None) -> Mapping[str, Any]:
        payload = payload if payload is not None else {
            "tool_id": "sample", "tool_call_id": "call-1", "arguments": {"value": 7},
            "definition": deepcopy(client.definition),
        }
        client.payload = payload
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
        self.mode_calls = []
        self.consent_calls = []

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
    assert client.mode_calls == []
    assert client.consent_calls == []


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
    result = invoke()
    assert result["is_error"] is True
    assert json.loads(result["result"])["error"]["code"] == (
        "ACTION_APPROVAL_NATIVE_DENIED"
    )
    assert len(client.calls) == 2
    assert len(client.consent_calls) == 1
    client.calls.clear()
    client.cancel_after_read = True
    with pytest.raises(PermissionError, match="stale"):
        invoke()
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "local_host", ["mode_missing", "mode_denied"], indirect=True,
)
def test_missing_or_denied_mode_stops_before_any_owner_read(local_host: Any) -> None:
    invoke, client, _ = local_host
    result = invoke()
    assert result["is_error"] is True
    assert json.loads(result["result"])["error"]["code"].startswith("ACTION_APPROVAL_")
    assert client.calls == []
    assert client.consent_calls == []


@pytest.mark.parametrize(
    "local_host", ["consent_missing", "consent_denied"], indirect=True,
)
def test_missing_or_denied_consent_stops_after_exact_read_without_target_call(
    local_host: Any,
) -> None:
    invoke, client, _ = local_host
    result = invoke()
    assert result["is_error"] is True
    assert json.loads(result["result"])["error"]["code"].startswith("ACTION_APPROVAL_")
    assert len(client.mode_calls) == 1
    assert len(client.calls) == 1
    assert client.calls[0][0] == executor.DEFINITION


def test_closed_local_capture_cannot_read_or_execute(local_host):
    invoke, client, captured = local_host
    captured.close()
    with pytest.raises(PermissionError, match="binding changed"):
        invoke()
    assert client.calls == []
