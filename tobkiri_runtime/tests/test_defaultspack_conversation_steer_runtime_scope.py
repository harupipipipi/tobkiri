from __future__ import annotations

import hashlib
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


def test_steer_status_uses_canonical_turn_operation(
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


def test_frontend_map_exposes_exact_turn_read_without_legacy_steer_route():
    """Keep browser callers on the finite captured turn surface."""

    from core_runtime.global_contracts.http_contract_dispatch import (
        HTTPContractBinding,
    )
    from ecosystem.defaultspack.defaultspack.frontend_contract_loader import (
        load_frontend_contract_bindings,
    )
    from ecosystem.defaultspack.defaultspack.http_surface_presentation import (
        DefaultspackHTTPPresentation,
    )

    map_path = (
        DEFAULTSPACK_ROOT
        / "defaultspack"
        / "frontend_contract_map.v4.json"
    )
    raw = map_path.read_bytes()
    bindings = load_frontend_contract_bindings(
        map_path,
        {
            "pack": {
                "id": "runtime.tauri.application.default",
                "kind": "application",
            },
            "artifacts": [
                {
                    "path": "defaultspack/frontend_contract_map.v4.json",
                    "kind": "asset",
                    "digest": (
                        "sha256:" + hashlib.sha256(raw).hexdigest()
                    ),
                }
            ],
        },
    )
    assert not any(binding.path == "/api/chat/steer" for binding in bindings)
    binding = next(
        item
        for item in bindings
        if item.method == "GET" and item.path == "/api/chat/turn"
    )
    assert isinstance(binding, HTTPContractBinding)
    assert len(binding.targets) == 1
    target = binding.targets[0]
    assert (
        target.contribution_id,
        target.contract_id,
        target.operation_id,
        target.provider_id,
        target.function_id,
        target.allowed_payload_keys,
    ) == (
        "defaults.conversations.turn.read",
        "tobkiri.resource.turn.v1",
        _generated_operation(
            "rumi_turn_runtime_pack", "tobkiri.resource.turn.v1"
        ),
        "rumi_turn_runtime_pack.turn-runtime.resource",
        "rumi_turn_runtime_pack.turn-runtime.resource",
        frozenset({"turn_id"}),
    )

    session = SimpleNamespace(
        profile_id="captured-profile",
        assert_current=lambda: None,
    )
    payload = DefaultspackHTTPPresentation().normalize_payload(
        target,
        {"turn_id": "turn-1"},
        session=session,
        workspace_binding_resolver=None,
    )
    assert payload == {
        "profile_id": "captured-profile",
        "operation": "get",
        "turn_id": "turn-1",
    }
    for substituted in (
        {"turn_id": "turn-1", "profile_id": "other"},
        {"turn_id": "turn-1", "operation": "list"},
    ):
        with pytest.raises(ValueError, match="one stable turn ID"):
            DefaultspackHTTPPresentation().normalize_payload(
                target,
                substituted,
                session=session,
                workspace_binding_resolver=None,
            )

    list_binding = next(
        item
        for item in bindings
        if item.method == "GET" and item.path == "/api/chat/turns"
    )
    assert list_binding.targets[0].operation_id == target.operation_id
    assert DefaultspackHTTPPresentation().normalize_payload(
        list_binding.targets[0],
        {"conversation_id": "conversation-1"},
        session=session,
        workspace_binding_resolver=None,
    ) == {
        "profile_id": "captured-profile",
        "operation": "list",
        "conversation_id": "conversation-1",
    }
    guidance_binding = next(
        item
        for item in bindings
        if item.method == "POST" and item.path == "/api/chat/turn/steer"
    )
    guidance_target = guidance_binding.targets[0]
    assert (
        guidance_target.contract_id,
        guidance_target.operation_id,
        guidance_target.provider_id,
        guidance_target.function_id,
        guidance_target.allowed_payload_keys,
    ) == (
        "tobkiri.action.turn.guidance.v1",
        "rumi_turn_runtime_pack.turn-guidance",
        "rumi_turn_runtime_pack.turn-runtime.guidance",
        "rumi_turn_runtime_pack.turn-runtime.guidance",
        frozenset({
            "turn_id", "expected_revision", "guidance_id", "guidance",
        }),
    )
    guidance = {
        "prompt": "Continue with the comparison.",
        "target_type": "conversation",
        "target_id": "conversation-1",
        "conversation_id": "conversation-1",
        "visible": True,
        "auto_send": True,
        "metadata": {},
    }
    assert DefaultspackHTTPPresentation().normalize_payload(
        guidance_target,
        {
            "turn_id": "turn-1",
            "expected_revision": 2,
            "guidance_id": "guidance-1",
            "guidance": guidance,
        },
        session=session,
        workspace_binding_resolver=None,
    ) == {
        "profile_id": "captured-profile",
        "turn_id": "turn-1",
        "expected_revision": 2,
        "guidance_id": "guidance-1",
        "guidance": guidance,
    }
    with pytest.raises(ValueError, match="guidance request"):
        DefaultspackHTTPPresentation().normalize_payload(
            guidance_target,
            {
                "turn_id": "turn-1",
                "expected_revision": 2,
                "guidance": guidance,
                "profile_id": "other",
            },
            session=session,
            workspace_binding_resolver=None,
        )

    intent = json.loads(
        (
            DEFAULTSPACK_ROOT
            / "v4"
            / "defaults.profile.intent.v1.json"
        ).read_text(encoding="utf-8")
    )
    lifecycle_edges = [
        edge
        for edge in intent["requested_edges"]
        if edge["contract_id"] == "tobkiri.action.turn.lifecycle.v1"
    ]
    assert [
        (
            edge["caller_function_id"],
            edge["operation_id"],
            edge["target_provider_id"],
        )
        for edge in lifecycle_edges
    ] == [
        (
            "rumi_turn_runtime_pack.turn-runtime.saved",
            _generated_operation(
                "rumi_turn_runtime_pack",
                "tobkiri.action.turn.lifecycle.v1",
            ),
            "rumi_turn_runtime_pack.turn-runtime.lifecycle",
        )
    ]
    guidance_edges = [
        edge
        for edge in intent["requested_edges"]
        if edge["contract_id"] == "tobkiri.action.turn.guidance.v1"
    ]
    assert [
        (
            edge["caller_function_id"],
            edge["operation_id"],
            edge["target_provider_id"],
        )
        for edge in guidance_edges
    ] == [
        (
            "shell.tauri.default",
            "rumi_turn_runtime_pack.turn-guidance",
            "rumi_turn_runtime_pack.turn-runtime.guidance",
        )
    ]


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


def test_guidance_identity_recovers_lost_ack_before_stale_revision():
    from ecosystem.rumi_turn_runtime_pack.runtime.turns import (
        TurnConflict,
        TurnRuntime,
    )

    runtime = TurnRuntime()
    turn = runtime.begin(
        {
            "profile_id": "defaults",
            "turn_id": "turn-1",
            "request_id": "request-1",
            "conversation_id": "conversation-1",
            "conversation_revision": 1,
        }
    )
    guidance = {
        "prompt": "Continue.",
        "target_type": "conversation",
        "target_id": "conversation-1",
        "conversation_id": "conversation-1",
        "visible": True,
        "auto_send": True,
        "metadata": {},
    }
    first = runtime.steer(
        "turn-1",
        guidance,
        expected_revision=turn["revision"],
        guidance_id="guidance-1",
    )
    recovered = runtime.steer(
        "turn-1",
        guidance,
        expected_revision=turn["revision"],
        guidance_id="guidance-1",
    )
    assert recovered == first
    assert len(recovered["guidance"]) == 1
    with pytest.raises(TurnConflict, match="rebound"):
        runtime.steer(
            "turn-1",
            {**guidance, "prompt": "Different."},
            expected_revision=first["revision"],
            guidance_id="guidance-1",
        )
    with pytest.raises(TurnConflict, match="stale"):
        runtime.steer(
            "turn-1",
            guidance,
            expected_revision=turn["revision"],
            guidance_id="guidance-2",
        )


def test_saved_receipt_atomically_preserves_and_reserves_guidance(tmp_path):
    from ecosystem.rumi_turn_runtime_pack.runtime.durable import (
        DurableTurnRuntime,
    )
    from tobkiri_protocol.canonical import canonical_digest

    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)
    initial = {
        "request": {
            "turn_id": "turn-1",
            "conversation_id": "conversation-1",
            "conversation_revision": 1,
            "content": "Hello",
        }
    }
    store.begin_saved(initial)
    claimed = store.claim_saved(initial)["turn"]
    store.mutate(
        "steer",
        "turn-1",
        expected_revision=claimed["revision"],
        guidance_id="guidance-1",
        guidance={
            "prompt": "Continue.",
            "target_type": "conversation",
            "target_id": "conversation-1",
            "conversation_id": "conversation-1",
            "visible": True,
            "auto_send": True,
            "metadata": {},
        },
    )
    reference = {
        "conversation_id": "conversation-1",
        "conversation_revision": 3,
        "user_message_id": "message:user",
        "assistant_message_id": "message:assistant",
        "outcome_digest": "sha256:" + "0" * 64,
    }
    settled = store.settle_saved_from_receipt(
        "turn-1",
        input_digest=claimed["input_digest"],
        result_reference=reference,
    )
    assert settled["status"] == "completed"
    assert settled["guidance"][0]["status"] == "queued"
    child_id = "steer:" + canonical_digest(
        {"parent_turn_id": "turn-1", "guidance_id": "guidance-1"}
    ).removeprefix("sha256:")
    followup = {
        "request": {
            "turn_id": child_id,
            "conversation_id": "conversation-1",
            "conversation_revision": 3,
            "content": "Continue.",
        }
    }
    reserved = store.reserve_guidance_followup(
        "turn-1", "guidance-1", "turn-1", followup,
    )
    recovered = store.reserve_guidance_followup(
        "turn-1", "guidance-1", "turn-1", followup,
    )
    assert recovered == reserved
    recovered_attachment = store.recover_guidance(
        "turn-1", "guidance-1", settled["guidance"][0]["value"],
    )
    assert recovered_attachment is not None
    assert recovered_attachment["id"] == "turn-1"
    assert store.active_guidance_followup("turn-1") == child_id
    parent = store.get("turn-1")
    assert parent is not None
    assert parent["guidance"][0]["followup_input"] == followup


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
