"""Capture-boundary regressions; these do not claim native VM execution."""
from copy import deepcopy
from types import SimpleNamespace as NS

import pytest

from ecosystem.rumi_ai_modality_pack.runtime.gateway import HOST_PROVIDER_FACTORY
from tobkiri_protocol.canonical import canonical_digest


def capture_fixture(factory):
    principal = NS(value="principal:modality")
    binding = NS(
        function=NS(function_id=factory.function_id, implementation_digest="sha256:implementation"),
        operation=NS(contract_id=factory._contract_id, contract_version="1.0.0",
                     operation_id=factory._operation_id),
        principal_ref=principal, artifact=NS(digest="sha256:artifact"),
    )
    activation = {"activation_id": "activation:modality"}
    context = NS(provider_bindings=(binding,), profile_id="profile.fixture",
                 plan_digest="sha256:plan", security_epoch=3, activation=activation,
                 domain_ids={(factory._contract_id, factory._operation_id, principal.value): "domain.fixture"})
    payload = {"text": "fixture"}
    envelope = NS(contract_id=factory._contract_id, contract_version="1.0.0",
                  operation_id=factory._operation_id, target_principal=principal,
                  target_domain=NS(value="domain.fixture"), payload=deepcopy(payload),
                  context=NS(profile_id=context.profile_id, plan_digest=context.plan_digest,
                             security_epoch=3, activation_id=activation["activation_id"],
                             activation_digest=canonical_digest(activation)))
    return context, payload, envelope


@pytest.mark.parametrize("factory", list(HOST_PROVIDER_FACTORY.values()), ids=list(HOST_PROVIDER_FACTORY))
@pytest.mark.parametrize("change", [
    "version", "operation", "contract", "principal", "domain", "payload",
    "profile_id", "plan_digest", "security_epoch", "activation_id", "activation_digest", "closed",
])
def test_modality_rejects_changed_capture_before_provider_access(factory, change):
    context, payload, envelope = capture_fixture(factory)
    captured = factory.capture(context)
    accesses = []
    invocation = NS(envelope=envelope, assert_current=lambda: None,
                    contract_client=lambda **kwargs: accesses.append(kwargs))
    if change == "closed":
        captured.close()
    elif change == "version":
        envelope.contract_version = "2.0.0"
    elif change == "operation":
        envelope.operation_id = "other"
    elif change == "contract":
        envelope.contract_id = "other"
    elif change == "principal":
        envelope.target_principal = NS(value="other")
    elif change == "domain":
        envelope.target_domain = NS(value="other")
    elif change == "payload":
        envelope.payload = {"text": "changed"}
    else:
        setattr(envelope.context, change, 4 if change == "security_epoch" else "other")
    with pytest.raises(PermissionError):
        captured.contributions[0].invoke(factory._operation_id, payload, invocation)
    assert accesses == []


@pytest.mark.parametrize("factory", list(HOST_PROVIDER_FACTORY.values()), ids=list(HOST_PROVIDER_FACTORY))
def test_modality_rejects_wrong_version_and_ambiguous_capture(factory):
    context, _, _ = capture_fixture(factory)
    context.provider_bindings[0].operation.contract_version = "2.0.0"
    with pytest.raises(PermissionError):
        factory.capture(context)
    context.provider_bindings[0].operation.contract_version = "1.0.0"
    context.provider_bindings *= 2
    with pytest.raises(PermissionError):
        factory.capture(context)


@pytest.mark.parametrize("factory", list(HOST_PROVIDER_FACTORY.values()), ids=list(HOST_PROVIDER_FACTORY))
def test_modality_close_during_operation_rejects_result(factory, monkeypatch):
    context, payload, envelope = capture_fixture(factory)
    captured = factory.capture(context)
    accesses = []
    def execute(_operation, _payload):
        captured.close()
        return {"status": "ok"}
    monkeypatch.setattr(factory, "_operation_factory", lambda client: execute)
    invocation = NS(envelope=envelope, assert_current=lambda: None,
                    contract_client=lambda **kwargs: accesses.append(kwargs))
    with pytest.raises(PermissionError, match="closed"):
        captured.contributions[0].invoke(factory._operation_id, payload, invocation)
    assert len(accesses) == 1


@pytest.mark.parametrize("factory", list(HOST_PROVIDER_FACTORY.values()), ids=list(HOST_PROVIDER_FACTORY))
def test_modality_exact_capture_preserves_handler_and_client_authority(factory, monkeypatch):
    context, payload, envelope = capture_fixture(factory)
    calls = []
    client = object()
    def get_client(**kwargs):
        calls.append(("client", kwargs))
        return client
    def bind_handler(actual_client):
        assert actual_client is client
        def execute(operation, actual_payload):
            calls.append(("execute", operation, actual_payload))
            return {"status": "ok", "operation": operation}
        return execute
    monkeypatch.setattr(factory, "_operation_factory", bind_handler)
    captured = factory.capture(context)
    invocation = NS(envelope=envelope, assert_current=lambda: calls.append("current"),
                    contract_client=get_client)
    result = captured.contributions[0].invoke(factory._operation_id, payload, invocation)
    assert result == {"status": "ok", "operation": factory._operation_name}
    assert calls == [
        "current",
        ("client", {"allowed_contract_ids": factory._allowed_contract_ids,
                    "consumer_pack_id": "rumi_ai_modality_pack", "include_credentials": False}),
        ("execute", factory._operation_name, payload),
        "current",
    ]
