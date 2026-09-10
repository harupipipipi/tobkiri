"""Saved tool rounds with real root ledgers/store; AI and final tools are doubles."""

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from core_runtime.authority.v4 import AuthorityDenied
from core_runtime.bootstrap.saved_bridge import (
    ALLOWED_TARGETS, DEFINITION, READINESS, SavedBridgeCallbacks, project_saved_tool_result,
)
from ecosystem.defaultspack.runtime import saved_conversation as saved
from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationStore
from tobkiri_host.continuation_chain import ChainIdentity, ContinuationChains
from tobkiri_host.saved_guest_dispatch import SavedGuestTurns, INVOKE_RESULT
from tobkiri_host.saved_host_exchange import SavedHostExchange
from tobkiri_host.saved_turn_plan import AI, APPEND, READ, TOOL, SavedTurnPlan
from tobkiri_protocol.canonical import canonical_digest, canonical_json
from tobkiri_protocol.saved_tools import saved_tool_messages, saved_tool_logs


class ToolTurn:
    def __init__(self, path, *, rounds=1, store=None, turn="turn-1", selection=None):
        self.store = store or ConversationStore("defaults", user_data_root=path)
        if store is None:
            self.store.create({"id": "conversation-1", "model_reference": "model-1"}, expected_revision=0)
        self.request = {
            "turn_id": turn, "conversation_id": "conversation-1",
            "conversation_revision": self.store.get("conversation-1")["conversation_revision"],
            "content": "Read the selected information", "tool_selection": selection or {"mode": "auto"},
        }
        self.outer = SimpleNamespace(context=SimpleNamespace(request_id=turn), payload={"request": self.request})
        self.calls = []
        self.ai_calls = 0
        self.rounds = rounds
        self.callbacks = SavedBridgeCallbacks(self.dispatch, lambda _outer, targets: self.require(targets))
        self.guest = SavedGuestTurns(clock=lambda: 0)
        self.binding = "sha256:" + "a" * 64
        request_digest = canonical_digest(self.outer.payload)
        self.host = SavedHostExchange(
            ChainIdentity("domain", turn, self.binding, 60),
            request_digest=request_digest, artifact_identity="artifact", deadline_text="60",
            chains=ContinuationChains(clock=lambda: 0, max_hops=20), request=self.request,
        )
        self.transport = {
            "request_id": turn, "target_domain": "domain", "guest_artifact_identity": "artifact",
            "request_digest": request_digest, "deadline_monotonic": "60", "payload": self.outer.payload,
        }
        self.pending = None

    def require(self, targets):
        assert all(target in ALLOWED_TARGETS for target in targets)

    def dispatch(self, outer, target, payload):
        assert outer is self.outer
        self.calls.append((target, deepcopy(payload)))
        if target == READ:
            value = {"conversation": self.store.get("conversation-1")}
        elif target == APPEND:
            value = self.store.append_message(
                "conversation-1", payload["message"],
                expected_conversation_revision=payload["expected_conversation_revision"],
                saved_input=self.outer.payload,
            )
        elif target == DEFINITION:
            assert payload == {"operation": "select", "selection": self.request["tool_selection"]}
            value = {"tools": [{"type": "function", "function": {
                "name": "file_read", "description": "Read", "parameters": {"type": "object"},
            }}], "definitions": {"file_read": "b" * 64}}
        elif target == READINESS:
            assert payload["tool_calling"] is True
            value = {"ready": True, "model_profile_id": "model-1"}
        elif target == AI:
            assert payload["tools"][0]["function"]["name"] == "file_read"
            assert payload["requirements"]["tool_calling"] is True
            self.ai_calls += 1
            value = {"status": "ok", "output": "Done", "tool_intents": []}
            if self.ai_calls <= self.rounds:
                value.update(output="", tool_intents=[{
                    "operation": "file_read", "intent_id": f"call-{self.ai_calls}",
                    "arguments": {"path": "readme.txt"},
                }])
        else:
            assert target == TOOL
            assert payload["expected_definition_hash"] == "b" * 64
            value = project_saved_tool_result({
                "tool_id": "file_read", "tool_call_id": payload["tool_call_id"],
                "status": "success", "is_error": False, "result": {"text": "Hello", "elapsed": 0.25},
            })
        return {"status": "ok", "value": value}

    @staticmethod
    def execute(_request, payload, _deadline, guard):
        guard()
        result = saved.tobkiri_packvm_invoke("saved_complete", payload)
        if result.get("kind") == "tobkiri.packvm.continuation.intent.v2":
            return result
        return {"kind": INVOKE_RESULT, "outcome": result}

    def start(self):
        self.callbacks.preflight(self.outer)
        self.pending = self.guest.begin(self.transport, self.binding, self.execute)

    def step(self):
        frame = self.host.accept(self.pending["host_bridge_request"])
        outcome = self.callbacks(self.outer, self.host.callback_frame(frame))
        result = self.host.result(outcome)
        self.pending = self.guest.resume("domain", self.request["turn_id"], result, self.execute)
        return result

    def complete(self):
        self.start()
        while self.pending.get("state") == "pending":
            self.step()
        self.host.finish(self.pending["outcome"])
        return self.pending["outcome"]


@pytest.mark.parametrize("rounds", [1, 3, 8])
def test_real_ledgers_save_tool_rounds_and_restore_provider_history(tmp_path, rounds):
    first = ToolTurn(tmp_path, rounds=rounds)
    outcome = first.complete()
    assert outcome["status"] == "ok"
    assert outcome["conversation_revision"] == 3
    messages = first.store.get("conversation-1")["messages"]
    assert len(messages) == 2
    trace = saved_tool_messages(messages[-1]["metadata"]["saved_tool_messages"])
    assert len(trace) == rounds * 2
    assert messages[-1]["tool_logs"] == saved_tool_logs(trace)
    assert sum(target == TOOL for target, _ in first.calls) == rounds
    assert first.ai_calls == rounds + 1
    second = ToolTurn(tmp_path, rounds=0, store=first.store, turn="turn-2")
    assert second.complete()["status"] == "ok"
    history = next(payload["messages"] for target, payload in second.calls if target == AI)
    assert history[1:1 + len(trace)] == trace
    assert len(first.store.get("conversation-1")["messages"]) == 4


@pytest.mark.parametrize("change", [None, "metadata", "logs", "unselected", "revision"])
def test_turn_completion_accepts_only_the_bound_saved_tool_transcript(tmp_path, change):
    from ecosystem.rumi_turn_runtime_pack.runtime.saved import _completed_reference

    turn = ToolTurn(tmp_path)
    outcome = turn.complete()
    request = deepcopy(turn.request)
    if change == "metadata":
        outcome["message"]["metadata"]["approved"] = True
    elif change == "logs":
        outcome["message"]["tool_logs"][0]["result"] = "forged"
    elif change == "unselected":
        request["tool_selection"] = {"mode": "none"}
    elif change == "revision":
        outcome["conversation_revision"] += 1
    if change is not None:
        with pytest.raises(ValueError):
            _completed_reference(request, outcome)
    else:
        assert _completed_reference(request, outcome) == turn.store.saved_receipt(
            request["turn_id"]
        )["result_reference"]


@pytest.mark.parametrize("change", ["arguments", "tool_id", "tool_call_id", "expected_definition_hash", "target"])
def test_authenticated_guest_cannot_rewrite_ai_tool_intent(tmp_path, change):
    turn = ToolTurn(tmp_path)
    turn.start()
    for _ in range(3):
        turn.step()
    wrapper = turn.pending["host_bridge_request"]
    frame = wrapper["bridge_request"]
    if change == "target":
        frame["target"]["operation_id"] = "unselected.operation"
    else:
        frame["payload"][change] = {"path": "secret"} if change == "arguments" else "changed"
    wrapper["bridge_request_digest"] = canonical_digest(frame)
    with pytest.raises(ValueError):
        turn.step()
    assert not any(target == TOOL for target, _ in turn.calls)


def test_cancel_after_ai_prevents_tool_dispatch_and_resume_replay(tmp_path):
    turn = ToolTurn(tmp_path)
    turn.start()
    for _ in range(3):
        result = turn.step()
    assert turn.guest.cancel("domain", "turn-1")
    with pytest.raises(ValueError, match="unavailable"):
        turn.guest.resume("domain", "turn-1", result, turn.execute)
    assert not any(target == TOOL for target, _ in turn.calls)


def test_serialized_frame_is_not_host_checked_tool_scope(tmp_path):
    turn = ToolTurn(tmp_path)
    turn.start()
    frame = turn.pending["host_bridge_request"]["bridge_request"]
    with pytest.raises(AuthorityDenied, match="Host-checked"):
        turn.callbacks(turn.outer, frame)


@pytest.mark.parametrize("change", ["content", "tool_logs", "saved_tool_messages"])
def test_final_assistant_cannot_forge_tool_results(tmp_path, change):
    turn = ToolTurn(tmp_path)
    turn.start()
    for _ in range(5):
        turn.step()
    wrapper = turn.pending["host_bridge_request"]
    message = wrapper["bridge_request"]["payload"]["message"]
    if change == "saved_tool_messages":
        message["metadata"][change][-1]["content"] = "forged"
    elif change == "tool_logs":
        message[change][0]["result"] = "forged"
    else:
        message[change] = "forged"
    wrapper["bridge_request_digest"] = canonical_digest(wrapper["bridge_request"])
    with pytest.raises(ValueError):
        turn.step()
    assert len(turn.store.get("conversation-1")["messages"]) == 1


def test_ninth_tool_and_reused_call_id_fail_before_execution():
    for repeated in (False, True):
        plan = SavedTurnPlan({"tool_selection": {"mode": "auto"}})
        plan.stage = "ai"
        for i in range(9):
            plan.receive({"status": "ok", "value": {"status": "ok", "output": "", "tool_definitions": {"read": "a" * 64}, "tool_intents": [
                {"operation": "read", "intent_id": "same" if repeated else f"call-{i}", "arguments": {}},
            ]}})
            if plan.failed:
                assert i == (1 if repeated else 8)
                break
            plan.receive({"status": "ok", "value": {"tool_id": "read", "tool_call_id": "same" if repeated else f"call-{i}", "content": "ok"}})
        assert plan.failed


def test_float_result_is_text_and_not_an_abi_exception():
    value = project_saved_tool_result({"status": "success", "is_error": False, "result": 0.25})
    canonical_json(value)
    assert json.loads(value["content"])["result"] == 0.25


def test_result_object_mutation_does_not_change_authenticated_tool_scope(tmp_path):
    turn = ToolTurn(tmp_path)
    turn.start()
    turn.step()
    turn.step()
    frame = turn.host.accept(turn.pending["host_bridge_request"])
    outcome = turn.callbacks(turn.outer, turn.host.callback_frame(frame))
    acknowledged = deepcopy(turn.host.result(outcome))
    # The guest receives the already acknowledged bytes, while a caller retaining
    # the original callback object changes it after Host.result has returned.
    outcome["value"]["tool_intents"][0]["arguments"]["path"] = "changed-after-ack"
    turn.pending = turn.guest.resume("domain", "turn-1", acknowledged, turn.execute)
    checked = turn.host.accept(turn.pending["host_bridge_request"])
    assert checked is not None


@pytest.mark.parametrize("output", [{"image": "unsupported"}, [{"type": "image", "url": "unsupported"}]])
def test_invalid_ai_transcript_is_rejected_before_selecting_tool_effect(output):
    plan = SavedTurnPlan({"tool_selection": {"mode": "auto"}})
    plan.stage = "ai"
    try:
        plan.receive({"status": "ok", "value": {
            "status": "ok", "output": output,
            "tool_definitions": {"read": "b" * 64},
            "tool_intents": [{"operation": "read", "intent_id": "call-1", "arguments": {}}],
        }})
    except ValueError:
        return
    assert plan.failed, "invalid transcript selected a tool before validation"
