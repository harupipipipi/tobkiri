from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def _generated_operation(pack_id: str, contract_id: str) -> str:
    artifact = (
        ROOT
        / "ecosystem"
        / pack_id
        / "executables.v4.json"
    )
    catalog = json.loads(artifact.read_text(encoding="utf-8"))
    operations = [
        operation["operation_id"]
        for variant in catalog["variants"]
        for operation in variant["operations"]
        if operation["contract_id"] == contract_id
    ]
    assert len(operations) == 1
    return operations[0]


def test_steer_recovers_persisted_profile_for_request_worker(monkeypatch):
    from domain.chat import steer

    registry = object()
    plan = SimpleNamespace(profile_id="default-profile")
    calls: list[tuple[object, str, str, dict]] = []

    monkeypatch.setattr(
        steer,
        "get_container",
        lambda: SimpleNamespace(get_or_none=lambda key: registry),
    )
    monkeypatch.setattr(
        steer,
        "captured_profile_id",
        lambda current_registry: plan.profile_id,
    )
    monkeypatch.setattr(
        steer,
        "invoke_global_contract",
        lambda current_registry, contract_id, operation, payload: calls.append(
            (current_registry, contract_id, operation, payload)
        )
        or {"ok": True},
    )

    result = steer._invoke(steer.TURN_RESOURCE, "list", {"conversation_id": "c1"})

    assert result == {"ok": True}
    assert calls == [
        (
            registry,
            "tobkiri.resource.turn.v1",
            "rumi_turn_runtime_pack.turn-resource",
            {
                "profile_id": "default-profile",
                "operation": "list",
                "conversation_id": "c1",
            },
        )
    ]


def test_turn_callers_match_captured_v4_contracts():
    from domain.chat import steer
    from ecosystem.rumi_agent_runtime_service_pack.runtime import runtime
    from ecosystem.rumi_conversation_store_pack.runtime.host import CONTRACT_ID
    from ecosystem.rumi_turn_runtime_pack.runtime.host import TurnHostFactoryV4

    assert steer.TURN_RESOURCE == TurnHostFactoryV4("resource").contract_id
    assert steer.TURN_ACTION == TurnHostFactoryV4("lifecycle").contract_id
    assert runtime.TURN_RESOURCE == TurnHostFactoryV4("resource").contract_id
    assert runtime.TURN_ACTION == TurnHostFactoryV4("lifecycle").contract_id
    assert steer.CONVERSATION_RESOURCE == CONTRACT_ID
    assert runtime.CONVERSATION_RESOURCE == CONTRACT_ID
    assert steer.TURN_RESOURCE_OPERATION == _generated_operation(
        "rumi_turn_runtime_pack", steer.TURN_RESOURCE
    )
    assert steer.TURN_ACTION_OPERATION == _generated_operation(
        "rumi_turn_runtime_pack", steer.TURN_ACTION
    )
    assert steer.CONVERSATION_RESOURCE_OPERATION == _generated_operation(
        "rumi_conversation_store_pack", steer.CONVERSATION_RESOURCE
    )
    assert runtime.TURN_RESOURCE_OPERATION == steer.TURN_RESOURCE_OPERATION
    assert runtime.TURN_ACTION_OPERATION == steer.TURN_ACTION_OPERATION
    assert (
        runtime.CONVERSATION_RESOURCE_OPERATION
        == steer.CONVERSATION_RESOURCE_OPERATION
    )


def test_steer_status_uses_canonical_route_and_legacy_route_is_absent(
    monkeypatch,
):
    from core_runtime.global_contract_dispatch import GlobalContractUnavailable
    from domain.chat import steer

    class Session:
        profile_id = "defaults"

        def invoke(self, contract_id, operation, payload):
            if (contract_id, operation) != (
                steer.TURN_RESOURCE,
                _generated_operation(
                    "rumi_turn_runtime_pack", steer.TURN_RESOURCE
                ),
            ):
                raise GlobalContractUnavailable("unknown contract")
            assert payload == {
                "profile_id": "defaults",
                "operation": "list",
            }
            return {"turns": []}

        def provider_metadata(self, contract_id):
            del contract_id
            return ()

    session = Session()
    monkeypatch.setattr(
        steer,
        "get_container",
        lambda: SimpleNamespace(get_or_none=lambda key: session),
    )

    assert steer.ConversationSteerStore().list() == []
    with pytest.raises(PermissionError, match="operation is unavailable"):
        steer._invoke(steer.TURN_RESOURCE, "steer", {})
    with pytest.raises(PermissionError, match="reserved fields"):
        steer._invoke(steer.TURN_RESOURCE, "list", {"profile_id": "other"})


def test_agent_steer_keeps_authority_and_turn_revision_fences():
    from ecosystem.rumi_agent_runtime_service_pack.runtime import runtime

    class Client:
        def __init__(self):
            self.calls: list[tuple[str, str, dict]] = []

        def invoke(self, contract_id, operation, payload):
            self.calls.append((contract_id, operation, payload))
            if (contract_id, operation) == (
                runtime.AGENT_STATE_RESOURCE,
                "run.list",
            ):
                return {"revision": 4}
            if (contract_id, operation) == (runtime.AUTHORITY, "authorize"):
                return {"authorized": True, "receipt": "receipt-1"}
            if (contract_id, operation) == (
                runtime.AGENT_STATE_ACTION,
                "run.steer",
            ):
                return {"run": {"id": "run-1", "turn_id": "turn-1"}}
            if (contract_id, operation) == (
                runtime.TURN_RESOURCE,
                runtime.TURN_RESOURCE_OPERATION,
            ):
                assert payload["operation"] == "get"
                return {"id": "turn-1", "revision": 7}
            if (contract_id, operation) == (
                runtime.TURN_ACTION,
                runtime.TURN_ACTION_OPERATION,
            ):
                assert payload["operation"] == "steer"
                return {"id": "turn-1", "revision": 8}
            raise AssertionError(f"unexpected call: {contract_id}/{operation}")

    client = Client()
    result = runtime.AgentRuntime(client, "defaults")._steer(
        {"run_id": "run-1", "guidance": {"prompt": "continue"}}
    )

    assert result["status"] == "ok"
    assert [call[:2] for call in client.calls] == [
        (runtime.AGENT_STATE_RESOURCE, "run.list"),
        (runtime.AUTHORITY, "authorize"),
        (runtime.AGENT_STATE_ACTION, "run.steer"),
        (runtime.TURN_RESOURCE, runtime.TURN_RESOURCE_OPERATION),
        (runtime.TURN_ACTION, runtime.TURN_ACTION_OPERATION),
    ]
    authority = client.calls[1][2]
    assert authority["approval_required"] is False
    assert authority["authority"] == "agent.state.manage"
    state_action = client.calls[2][2]
    assert state_action["authority_receipt"] == "receipt-1"
    turn_action = client.calls[4][2]
    assert turn_action == {
        "profile_id": "defaults",
        "operation": "steer",
        "turn_id": "turn-1",
        "expected_revision": 7,
        "guidance": {"prompt": "continue"},
    }


def test_agent_conversation_read_accepts_captured_host_envelope():
    from ecosystem.rumi_agent_runtime_service_pack.runtime import runtime

    class Client:
        def invoke(self, contract_id, operation, payload):
            assert (contract_id, operation) == (
                runtime.CONVERSATION_RESOURCE,
                _generated_operation(
                    "rumi_conversation_store_pack",
                    runtime.CONVERSATION_RESOURCE,
                ),
            )
            assert payload == {
                "profile_id": "defaults",
                "operation": "get",
                "conversation_id": "conversation-1",
            }
            return {
                "conversation": {
                    "id": "conversation-1",
                    "conversation_revision": 3,
                }
            }

    assert runtime.AgentRuntime(Client(), "defaults")._conversation(
        "conversation-1"
    ) == {
        "id": "conversation-1",
        "conversation_revision": 3,
    }
