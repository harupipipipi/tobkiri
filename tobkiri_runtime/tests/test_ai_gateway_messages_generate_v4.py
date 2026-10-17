"""Multi-message Flow nodes preserve history and expose canonical text only."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from core_runtime.global_contract_dispatch import GlobalContractInvocationError
from ecosystem.rumi_ai_gateway_pack.runtime import messages_generate as node
from tobkiri_protocol.canonical import canonical_json

MESSAGES = [
    {"role": "system", "content": "Be concise"},
    {"role": "user", "content": "First question"},
    {"role": "assistant", "content": "First answer"},
    {"role": "user", "content": "Follow-up"},
]


def _operation(monkeypatch, result=None, profile=None):
    calls = []
    def generate(_name, payload):
        calls.append(deepcopy(payload))
        return result if result is not None else {
            "status": "ok", "output": "Complete", "cost": 0.25,
            "credential_handle": "private-owner-handle", "tool_intents": [],
        }
    monkeypatch.setattr(node.gateway, "create_generate_operation", lambda _: generate)
    client = SimpleNamespace(invoke=lambda *_: profile if profile is not None else {
        "profile": {"enabled": True}, "resolved_profile_id": "registered.profile",
    })
    return node.create_messages_generate_operation(client), calls


def test_history_and_roles_preserved_without_leaking_telemetry(monkeypatch):
    operation, calls = _operation(monkeypatch)
    original = deepcopy(MESSAGES)
    result = operation(node.FUNCTION_ID, {"messages": MESSAGES, "model_profile_id": "alias"})
    assert MESSAGES == original
    assert calls[0]["messages"] == MESSAGES
    assert calls[0]["model_profile_id"] == "registered.profile"
    assert result == {"status": "ok", "text": "Complete", "model_profile_id": "registered.profile"}
    assert canonical_json(result)


@pytest.mark.parametrize("change", [
    {"messages": []}, {"messages": [{"role": "tool", "content": "x"}]},
    {"messages": [{"role": "user", "content": [{"type": "text", "text": "x"}]}]},
    {"messages": [{"role": "user", "content": "x", "approved": True}]},
    {"model_profile_id": " key "}, {"model_profile_id": ""},
    {"credential_handle": "not-accepted"},
    {"messages": [{"role": "user", "content": "界" * 262144}]},
])
def test_invalid_input_fails_before_any_owner_or_provider_call(monkeypatch, change):
    operation, calls = _operation(monkeypatch)
    with pytest.raises(ValueError):
        operation(node.FUNCTION_ID, {"messages": MESSAGES, "model_profile_id": "registered", **change})
    assert calls == []


@pytest.mark.parametrize("result", [
    {"status": "error", "output": "not success"},
    {"status": "ok", "output": ""}, {"status": "ok", "output": " "},
    {"status": "ok", "output": "text", "tool_intents": [{"name": "tool"}]},
])
def test_incomplete_or_tool_response_is_not_marked_complete(monkeypatch, result):
    operation, _ = _operation(monkeypatch, result=result)
    with pytest.raises(GlobalContractInvocationError):
        operation(node.FUNCTION_ID, {"messages": MESSAGES, "model_profile_id": "registered"})


@pytest.mark.parametrize("profile", [
    {"profile": {"enabled": False}}, {"profile": {}},
    {"profile": {"enabled": True}, "resolved_profile_id": {"not": "an-id"}},
])
def test_disabled_or_invalid_profile_stops_before_generation(monkeypatch, profile):
    operation, calls = _operation(monkeypatch, profile=profile)
    with pytest.raises(GlobalContractInvocationError):
        operation(node.FUNCTION_ID, {"messages": MESSAGES, "model_profile_id": "registered"})
    assert calls == []
