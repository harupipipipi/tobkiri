"""Exact selected native chat routes retain their own approval lifecycle."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

from ecosystem.rumi_tool_local_executor_pack.runtime import executor
from tests.test_authority_v4_lifecycle import _principal
from tests.test_saved_tool_consent_broker import make_artifact
from tobkiri_host.contracts import OperationCatalog, OperationRoute
from tobkiri_host.models import OpaqueAuthorityRef


@pytest.fixture
def native_route():
    function = "rumi_default_tools_pack.chat-message-tool"
    operation = "rumi_default_tools_pack.chat-message-operation"
    principal = replace(_principal("chat", operation=operation), function_id=function)
    artifact = make_artifact(principal, executor.LOCAL_OPERATION)
    route = OperationRoute(
        contract_id=executor.LOCAL_OPERATION,
        operation_id=operation,
        artifact_digest=artifact.digest,
        function_id=function,
        variant_id="provider.variant",
        execution_domain_profile="dedicated.provider",
        materialization_mode="on_demand",
        target_principal_ref=OpaqueAuthorityRef(principal.principal_id),
    )
    binding = OperationCatalog((artifact,), (route,)).resolve(
        executor.LOCAL_OPERATION, operation, None
    )
    selected = {
        "contract_id": executor.LOCAL_OPERATION,
        "function_id": function,
        "provider_instance_id": "chat-message-tool",
        "operation_id": operation,
        "backend_id": "tobkiri.python-host-v4",
        "principal_id": principal.principal_id,
        "artifact_digest": artifact.digest,
        "implementation_digest": binding.function.implementation_digest,
    }
    definition = {
        "tool_id": "chat_send_message",
        "execution": {
            "kind": "local",
            "contract_id": executor.LOCAL_OPERATION,
            "operation": operation,
            "provider_instance_id": function,
        },
    }
    payload = {
        "tool_id": "chat_send_message",
        "tool_call_id": "one-call",
        "arguments": {"target_kind": "chat", "target_id": "target", "content": "Exact"},
        "definition": deepcopy(definition),
    }
    calls = []
    context = NS(catalog_bindings=(binding,))
    context.saved_tool_mode_admission = lambda invocation: "ask"

    def consent(invocation, execution, body):
        calls.append("read_consent")
        raise PermissionError("native saved tool read route is unsupported")

    context.saved_tool_consent_port = consent

    def invoke(contract, operation, body, **kwargs):
        calls.append(operation)
        if contract == executor.DEFINITION:
            return {"found": True, "resolved_tool_id": payload["tool_id"], "definition": definition}
        assert kwargs == {"provider_instance_id": "chat-message-tool"}
        assert body == {key: payload[key] for key in ("tool_id", "tool_call_id", "arguments")}
        # The finite native handler remains the required next call. The
        # executor supplies no approved bit or replacement effect payload.
        return {"native_handler_entered": True}

    invocation = NS(
        assert_current=lambda: None,
        contract_client=lambda **kwargs: NS(invoke=invoke, providers=lambda contract: (selected,)),
    )
    return context, payload, definition, selected, invocation, calls


def test_ask_exact_selected_chat_delegates_only_to_native_handler(native_route):
    context, payload, _, _, invocation, calls = native_route
    assert executor._bind(context)(payload, invocation) == {"native_handler_entered": True}
    assert calls == [
        "rumi_tool_registry_pack.tool-definition-resource",
        "rumi_default_tools_pack.chat-message-operation",
    ]
    assert "approved" not in payload["arguments"]


@pytest.mark.parametrize("mode", ["agent", "full"])
def test_elevated_chat_still_uses_unsupported_consent_admission(native_route, mode):
    context, payload, _, _, invocation, calls = native_route
    context.saved_tool_mode_admission = lambda invocation: mode
    result = executor._bind(context)(payload, invocation)
    assert result["is_error"] is True
    assert calls[-1] == "read_consent"
    assert "rumi_default_tools_pack.chat-message-operation" not in calls


@pytest.mark.parametrize(
    "change",
    [
        "version",
        "contract",
        "principal",
        "artifact",
        "implementation",
        "backend",
        "function",
        "tool",
        "duplicate_binding",
        "missing_binding",
        "snapshot",
    ],
)
def test_lookalike_or_changed_chat_route_never_gets_native_exemption(native_route, change):
    context, payload, definition, selected, invocation, calls = native_route
    binding = context.catalog_bindings[0]
    if change == "version":
        context.catalog_bindings = (
            replace(binding, operation=replace(binding.operation, contract_version="2.0.0")),
        )
    elif change == "duplicate_binding":
        context.catalog_bindings *= 2
    elif change == "missing_binding":
        context.catalog_bindings = ()
    elif change == "tool":
        payload["tool_id"] = "lookalike"
    elif change == "snapshot":
        payload["definition"]["execution"]["operation"] = "foreign.operation"
    else:
        key = {
            "contract": "contract_id",
            "principal": "principal_id",
            "artifact": "artifact_digest",
            "implementation": "implementation_digest",
            "backend": "backend_id",
            "function": "function_id",
        }[change]
        selected[key] = "foreign"
        if change == "function":
            definition["execution"]["provider_instance_id"] = "chat-message-tool"
            payload["definition"] = deepcopy(definition)
    if change == "snapshot":
        with pytest.raises(PermissionError, match="definition changed"):
            executor._bind(context)(payload, invocation)
    else:
        result = executor._bind(context)(payload, invocation)
        assert result["is_error"] is True
        assert calls[-1] == "read_consent"
    assert "rumi_default_tools_pack.chat-message-operation" not in calls


@pytest.mark.parametrize("tool", ["calculator", "coding_file_read"])
def test_existing_read_tools_still_require_consumed_consent(native_route, tool):
    context, payload, _, _, invocation, calls = native_route
    payload["tool_id"] = tool
    context.saved_tool_consent_port = lambda *args: {"actual_read_consent_called": True}
    assert executor._bind(context)(payload, invocation) == {"actual_read_consent_called": True}
    assert calls == ["rumi_tool_registry_pack.tool-definition-resource"]


@pytest.mark.parametrize("reason", ["native approval pending", "native approval denied"])
def test_native_handler_denial_cannot_fall_back_to_read_consent(native_route, reason):
    context, payload, _, _, invocation, calls = native_route
    client = invocation.contract_client()
    original = client.invoke

    def native(contract, operation, body, **kwargs):
        if contract == executor.LOCAL_OPERATION:
            raise PermissionError(reason)
        return original(contract, operation, body, **kwargs)

    client.invoke = native
    invocation.contract_client = lambda **kwargs: client
    with pytest.raises(PermissionError, match=reason):
        executor._bind(context)(payload, invocation)
    assert "read_consent" not in calls
