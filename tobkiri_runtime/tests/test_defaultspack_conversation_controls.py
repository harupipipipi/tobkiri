from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


@pytest.fixture
def steer_runtime(monkeypatch):
    from domain.chat import steer as steer_module
    from ecosystem.rumi_turn_runtime_pack.runtime.turns import TurnRuntime

    runtime = TurnRuntime()

    def invoke(contract_id, operation, payload):
        if contract_id == steer_module.CONVERSATION_RESOURCE:
            assert operation == "get"
            return {
                "id": payload["conversation_id"],
                "conversation_revision": 1,
            }
        if contract_id == steer_module.TURN_RESOURCE:
            if operation == "get":
                return runtime.get(payload["turn_id"])
            assert operation == "list"
            return {
                "turns": runtime.list(
                    conversation_id=payload.get("conversation_id")
                )
            }
        if contract_id == steer_module.TURN_ACTION:
            if operation == "begin":
                return runtime.begin(payload)
            if operation == "steer":
                return runtime.steer(
                    payload["turn_id"],
                    payload["guidance"],
                    expected_revision=payload["expected_revision"],
                )
            if operation == "consume_guidance":
                return runtime.consume_guidance(
                    payload["turn_id"],
                    expected_revision=payload["expected_revision"],
                    guidance_ids=payload.get("guidance_ids"),
                )
            if operation == "cancel_guidance":
                return runtime.cancel_guidance(
                    payload["turn_id"],
                    payload["guidance_id"],
                    expected_revision=payload["expected_revision"],
                )
        raise AssertionError(f"unexpected contract call: {contract_id}/{operation}")

    monkeypatch.setattr(steer_module, "_invoke", invoke)
    return runtime


def test_conversation_steer_queues_and_processes_followup(
    monkeypatch, steer_runtime
):
    from blocks.conversation.steer import run as steer

    calls: list[dict] = []

    def fake_send(payload, context):
        calls.append({"payload": payload, "context": context})
        return {"status": "ok", "data": {"id": "assistant-steer"}}

    monkeypatch.setattr("blocks.chat.send.run", fake_send)
    queued = steer({"conversation_id": "conv-1", "prompt": "next step"}, {})

    processed = steer({"action": "process", "conversation_id": "conv-1"}, {})

    assert queued["status"] == "ok"
    assert processed["status"] == "ok"
    assert processed["data"]["processed"][0]["status"] == "sent"
    assert calls[0]["payload"]["message"]["content"] == "next step"
    assert calls[0]["context"]["_conversation_steer_autosend"] is True


def test_conversation_steer_can_be_consumed_for_running_turn(
    steer_runtime,
):
    from blocks.conversation.steer import run as steer
    from domain.chat.steer import ConversationSteerStore

    queued = steer({"conversation_id": "conv-1", "prompt": "change course"}, {})
    consumed = ConversationSteerStore().consume_for_conversation("conv-1")
    processed = steer({"action": "process", "conversation_id": "conv-1"}, {})

    assert queued["status"] == "ok"
    assert consumed[0]["status"] == "injected"
    assert consumed[0]["prompt"] == "change course"
    assert processed["status"] == "ok"
    assert processed["data"]["processed"] == []


def test_conversation_steer_consume_respects_auto_send_false(
    steer_runtime,
):
    from blocks.conversation.steer import run as steer
    from domain.chat.steer import ConversationSteerStore
    from domain.chat.stream_engine import ChatRunEngine

    queued = steer({"conversation_id": "conv-1", "prompt": "do not auto inject", "auto_send": False}, {})
    working_messages: list[dict] = [{"role": "user", "content": "original request"}]

    events = list(ChatRunEngine()._inject_conversation_steer("conv-1", working_messages))
    remaining = ConversationSteerStore().list(status="queued")

    assert queued["status"] == "ok"
    assert queued["data"]["auto_send"] is False
    assert events == []
    assert working_messages == [{"role": "user", "content": "original request"}]
    assert len(remaining) == 1
    assert remaining[0]["status"] == "queued"
    assert remaining[0]["prompt"] == "do not auto inject"


def test_conversation_handoff_creates_move_card_without_initial_send(monkeypatch):
    from blocks.conversation.handoff import run as handoff
    from domain.chat import store as store_module

    def invoke(contract_id, operation, payload):
        if contract_id == store_module.CONVERSATION and operation == "list":
            return {"revision": 0, "conversations": []}
        if contract_id == store_module.CONVERSATION_MANAGE and operation == "create":
            return {
                "conversation": {
                    **payload["conversation"],
                    "conversation_revision": 1,
                    "messages": [],
                },
                "store_revision": 1,
            }
        raise AssertionError(f"unexpected contract call: {contract_id}/{operation}")

    monkeypatch.setattr(store_module, "_invoke", invoke)

    result = handoff({"model": "stub/default", "prompt": "seed", "send": False, "conversation_id": "source-1"}, {})

    assert result["status"] == "ok"
    data = result["data"]
    assert data["conversation_id"]
    assert data["widget"]["kind"] == "conversation_handoff"
    assert data["widget"]["url_path"] == f"?chat={data['conversation_id']}"
    assert data["external_reply"]["handoff_token"] == data["conversation_id"]
